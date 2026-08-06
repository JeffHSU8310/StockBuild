# -*- coding: utf-8 -*-
"""
core/order_intent.py — 券商中立的「委託意圖」(ADR-110 階段 1)

【這個模組要解決什麼】原本 `_place_strategy_order()` 裡「算讓價、對齊檔位、
決定限價/市價、組委託」這幾件事,跟 shioaji 的常數 (`sj.constant.Action.Buy`、
`StockPriceType.LMT`、`StockOrderLot.IntradayOdd` …) 綁在同一段程式裡。
只要想讓策略把單送到別家券商,就得把整段複製一份改常數——那等於同一份
下單規則存在四份,遲早分歧 (P-67 的教訓)。

拆法是把它切成兩層:
  1. **算出「要下什麼單」** ← 本模組。純計算,零 shioaji、零 tkinter,
     輸出一個各家券商都看得懂的 dict。
  2. **把這個 dict 翻成某一家券商的委託物件** ← `brokers/<券商>.py`。

第 1 層是**交易所層級的規則**,四家券商完全一樣:檔位怎麼算 (鐵則 7)、
零股只能限價 ROD (鐵則 6)、讓價幾檔。這些不該因為換券商而有第二種寫法。
第 2 層才是各家 SDK 的差異。

【刻意不做的事】本模組不檢查「這張單合不合法」——那是
`core/order_rules.py` 的職責 (鐵則 9),兩者分開是因為驗證規則要能單獨
套用在手動下單路徑上,而那條路徑不經過這裡。
"""
from core import order_rules
from core import strategy_engine
from core import tick_rules

# 委託意圖的欄位值域 (各家 adapter 要對照翻譯的就是這幾組字串)
ACTIONS = ('買進', '賣出')
TRADE_TYPES = ('股票', '零股', '期貨')
PRICE_TYPES = ('限價', '市價', '範圍市價')

TIF_ROD = 'ROD'
LOT_COMMON = '整股'
LOT_ODD = '零股'
COND_CASH = '現股'


DEFAULT_BROKER = 'sinopac'


def broker_of(strategy):
    """策略要用哪一家券商下單。

    【ADR-110】沒設定時一律回永豐——舊策略檔沒有 `broker` 欄位,不能因此
    變成「沒有券商可用」而全部停擺。這個預設值就是「行為與加這個功能之前
    完全相同」的保證。
    """
    return str((strategy or {}).get('broker', '') or '').strip() or DEFAULT_BROKER


def account_of(strategy):
    """策略指定的券商帳號 id;沒指定回 None = 用該券商 SDK 的預設帳號。

    刻意用 None 而不是空字串:呼叫端要能分辨「使用者沒選」與「使用者選了
    一個叫空字串的帳號」,前者要沿用舊行為,後者是設定壞掉。
    """
    v = str((strategy or {}).get('broker_account', '') or '').strip()
    return v or None


def describe_target(strategy, broker_label=None, account_label=None):
    """「這張單會下到哪裡」的一行描述,給確認視窗與策略清單共用。

    多券商最大的新風險是**下錯帳戶**——策略設定改一個字,單就送到別家去了,
    而且是真錢。所以凡是會讓實單開始運作的畫面,都必須顯示這一行。
    """
    b = broker_label or broker_of(strategy)
    a = account_label or account_of(strategy) or '預設帳號'
    return f"{b} / {a}"


def apply_slippage(base_price, action, ticks, asset_type, symbol):
    """讓價後對齊到合法檔位。回傳 (讓價後價格, 這個價位的 tick)。

    【鐵則 7】價格一定要落在台股/期貨的合法檔位上,不可用 `.2f` 之類的
    無腦四捨五入——送一個非法價位過去,券商會直接退單。

    買進往上讓、賣出往下讓:讓價的用意是「寧可貴一點也要成交」,方向寫反
    會變成「掛一個更不可能成交的價」,策略看起來就像從來不進場。
    """
    base = float(base_price)
    tick = tick_rules.get_tick(base, asset_type, symbol)
    n = int(ticks or 0)
    px = base + n * tick if action == '買進' else base - n * tick
    return round(round(px / tick) * tick, 4), tick


def build_live_order(strategy, intent, asset_type, exec_price=None):
    """把策略的 intent 組成券商中立的委託意圖 dict。

    【ADR-075 看A做B】exec_price 是「實際要下單的商品 B」的成交基準價;
    有帶就用它,否則用 intent['price'] (一般模式 A=B)。asset_type / symbol
    一律是 B,因為檔位與讓價都要以真正送出去的那個商品計算。

    回傳的 dict 欄位:
      symbol / action / qty        標的、買賣別、數量
      trade_type                   股票 / 零股 / 期貨
      price_type                   限價 / 市價 / 範圍市價
      price                        真正要送出的價 (市價與範圍市價一律 0.0)
      limit_price                  讓價並對齊檔位後的價 (市價單也算給日誌看)
      tick                         這個價位的檔位大小
      time_in_force / order_lot / order_cond
      price_label                  給日誌與確認視窗看的人話 ("限價600.5")
    """
    sym = str(strategy.get('symbol', '')).upper()
    qty = int(intent['qty'])
    action = intent['action']
    base_px = float(exec_price) if exec_price is not None else float(intent['price'])
    ticks = int(strategy.get('slippage_ticks', 2) or 0)
    px, tick = apply_slippage(base_px, action, ticks, asset_type, sym)

    tt = strategy_engine.trade_type_of(strategy)
    ptype = strategy_engine.price_type_of(strategy)
    # 【鐵則 6】零股只能限價 ROD。實際擋下來的是 price_type_of() (它一開頭
    # 就對零股回傳「限價」),這一行是**重複的保險**,不是唯一防線:這個 dict
    # 未來要交給四家 adapter 各自翻譯,把不變量寫在交出去的邊界上,可以讓
    # 「日後有人改動 price_type_of」不會直接變成券商退單。
    # 診斷案例對零股送市價的情境有斷言,兩層任一層失效都會被抓到。
    if tt == '零股':
        ptype = '限價'
    is_lmt = (ptype == '限價')

    # 【ADR-146 / 鐵則 9】送出前在本地擋掉不合法的數量,不依賴券商回退單。
    #
    # 這是**第二道**(第一道是存檔時的 validate_strategy)。兩道都要有的理由很
    # 具體:策略存檔之後,數量還可能經由「舊策略檔的殘留值」「手動改 JSON」
    # 「日後新增的批次調整」變動,而這裡是所有委託離開系統的最後一個關口。
    # 零股的 1~999 股是**交易所規則**,超過一定被退 —— 被退的當下策略已經
    # 以為自己成交了(ADR-035 的樂觀模型),部位認知就此對不上。
    _cap, _unit, _is_rule = order_rules.strategy_qty_limit(tt)
    if qty < 1 or qty > _cap:
        raise ValueError(
            f"{tt}委託數量 {qty} {_unit} 不合法,須為 1~{_cap} {_unit}"
            + ("(交易所規則)" if _is_rule else "(本系統防呆上限)"))

    return {
        'symbol': sym,
        'action': action,
        'qty': qty,
        'trade_type': tt,
        'price_type': ptype,
        'price': px if is_lmt else 0.0,
        'limit_price': px,
        'tick': tick,
        'time_in_force': TIF_ROD,
        'order_lot': LOT_ODD if tt == '零股' else LOT_COMMON,
        'order_cond': COND_CASH,
        'price_label': f"限價{px:g}" if is_lmt else ptype,
        # 【ADR-110 階段2】要下到哪一家、哪一個帳號。account 為 None 時
        # adapter 不可帶任何帳號參數 —— 那才等於「沿用 SDK 預設帳號」,
        # 也就是加這個功能之前的行為。
        'broker': broker_of(strategy),
        'account': account_of(strategy),
    }


def describe(order_intent):
    """一行人話描述,給確認視窗與系統日誌共用。

    確認視窗顯示的字串必須跟真正送出去的內容來自**同一個來源**,否則會出現
    「畫面寫限價、實際送市價」這種最危險的落差 (鐵則 14 的精神)。
    """
    oi = order_intent or {}
    return (f"{oi.get('action', '?')} {oi.get('symbol', '?')} "
            f"{oi.get('qty', 0)} ({oi.get('trade_type', '?')}) "
            f"{oi.get('price_label', '?')}")
