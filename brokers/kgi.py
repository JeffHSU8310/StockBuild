"""
brokers.kgi — 凱基證券 SUPER PY adapter (ADR-111 / ADR-110 階段3)

【本檔的簽名來源】不是照文件猜的:從 PyPI 取得 `kgisuperpy` 的 wheel 後,
直接讀它的原始碼確認每一個函式簽名與列舉值 —— 已核對 **2.0.8 與 2.1.0**,
本檔用到的部分兩版完全相同 (查證方式見 DECISIONS_ADR111.md)。凡是無法從
原始碼確認的行為 (例如送出後的實際回報時序),本檔一律標明「需實機驗證」,
不假裝知道。

【與永豐 (shioaji) 的三個關鍵差異】——這些差異就是 adapter 存在的理由:

1. **凱基一次只綁定一個帳號**。`api._set_Account(acc)` 會重新指派
   `api.Order`;shioaji 則是每張單各自帶 `account=`。因此本 adapter 必須
   「綁定 → 送單」成對進行,而且**整段要上鎖**,否則兩個策略同時下單時,
   A 策略綁好帳號後被 B 策略改掉,A 的單就送到 B 的帳戶去了。
   這是本檔最重要的一段程式。

2. **凱基沒有「預設帳號」可用**。`api.Order` 在 `_set_Account()` 之前根本
   不存在,所以策略若沒指定帳號,這裡只能明確拒單 —— 不像永豐可以沿用
   SDK 預設。這個差異已在 place_order() 裡擋下並回明確訊息。

3. **證券與期貨是兩套物件**:`api.Order` / `api.FutOrder`,帳號也分開
   (`_set_Account` / `_set_FutAccount`),且期貨的 create_order 沒有
   order_cond / odd_lot 參數。
"""
import threading

from brokers.base import BrokerClient

try:
    import kgisuperpy as kgi
    HAS_KGI = True
except Exception:
    # 【ADR-111】刻意連 Exception 都吃:kgisuperpy 在 Python 3.14 會在
    # import 階段就失敗 (它載入時會拉進 numba,而 numba 沒有 3.14 版;
    # 套件自帶的 C 擴充也只編到 cp313)。那不是 ImportError 一種而已,
    # 不能讓「沒裝/裝不起來凱基」把整個主程式打掉。
    kgi = None      # 名字一定要存在:否則其他地方寫 kgi.X 會是 NameError
    HAS_KGI = False # 而不是「這家券商沒接上」這種看得懂的失敗


class KGIBroker(BrokerClient):
    name = "kgi"
    display_name = "凱基"

    def __init__(self):
        super().__init__()
        self.api = None
        # 帳號綁定 → 送單 必須是不可分割的一段 (見檔頭差異 1)。
        self._order_lock = threading.RLock()
        self._bound = None          # (kind, account_id);kind = 'stock' / 'futures'

    # ---- 連線生命週期 ----
    def new_session(self):
        """凱基沒有「先建物件再登入」的兩段式,登入本身就會產生連線物件。"""
        self.api = None
        self.logged_in = False
        self._bound = None
        return self.api

    def login(self, person_id, person_pwd, simulation=False):
        if not HAS_KGI:
            raise RuntimeError(
                "沒有可用的 kgisuperpy。凱基的套件自帶 C 擴充只編到 cp313,"
                "且載入時會拉進 numba —— 在 Python 3.14 無法安裝/載入。"
                "請改用 Python 3.13 以下,或把凱基放進獨立行程。")
        self.api = kgi.login(person_id=person_id, person_pwd=person_pwd,
                             simulation=bool(simulation))
        self.logged_in = True
        self._bound = None
        return self.api

    def logout(self):
        if self.api is not None:
            self.api._logout()
        self.logged_in = False
        self._bound = None

    # ---- 帳號 ----
    def _acc_map(self):
        """{account_id: broker_id}。未登入或查詢失敗一律回空 dict。"""
        try:
            obj = getattr(self.api, '_ObjOrder', None)
            return dict(getattr(obj, '_acc', {}) or {})
        except Exception:
            return {}

    def _acc_rows(self):
        """帳號明細 (含 account_flag:證券/期貨/複委託)。查不到回空 list。"""
        try:
            return list(self.api._show_account() or [])
        except Exception:
            return []

    def list_accounts(self):
        """回傳 [(account_id, 顯示名稱), ...]。

        顯示名稱帶上「證券/期貨」,因為凱基的證券戶與期貨戶要走不同的下單
        物件,選錯類別的帳號會直接送不出去 —— 使用者在下拉選單就要看得出來。
        """
        rows = {str(r.get('account')): r for r in self._acc_rows()
                if isinstance(r, dict)}
        out = []
        for acc in self._acc_map():
            flag = str((rows.get(str(acc)) or {}).get('account_flag', '')).strip()
            out.append((str(acc), f"{acc} ({flag})" if flag else str(acc)))
        return out

    def account_object(self, account_id):
        """凱基用帳號字串本身當識別,不像 shioaji 有 Account 物件。

        找得到回帳號字串,找不到回 None —— 回 None 的語意跟 base 一致
        (呼叫端要據此拒單,絕不可退回別的帳號)。
        """
        want = str(account_id or '').strip()
        return want if want and want in self._acc_map() else None

    def _account_kind(self, account_id):
        """這個帳號是證券戶還是期貨戶。查不到 flag 時回 None (由呼叫端決定)。"""
        for r in self._acc_rows():
            if isinstance(r, dict) and str(r.get('account')) == str(account_id):
                flag = str(r.get('account_flag', ''))
                if '期貨' in flag:
                    return 'futures'
                if '證券' in flag:
                    return 'stock'
        return None

    def _bind(self, kind, account_id):
        """把 api.Order / api.FutOrder 綁到指定帳號。呼叫端必須已持有鎖。"""
        if self._bound == (kind, account_id):
            return                      # 已經綁在這個帳號,不必重綁
        if kind == 'futures':
            self.api._set_FutAccount(account_id)
        else:
            self.api._set_Account(account_id)
        self._bound = (kind, account_id)

    # ---- 委託 ----
    def _order_kwargs(self, oi):
        """委託意圖 → kgisuperpy create_order 的參數。

        對照表 (值取自 kgisuperpy/trading/_trade_base.py):
          限價     → price 直接給 float
          市價     → price = PriceType.MKT
          範圍市價 → price = PriceType.RangeMarket   (凱基只有期貨用得到)
          零股     → odd_lot = OddLot.Odd (盤中零股;不是盤後的 Odd_AfterMarket)
        """
        action = kgi.Action.Buy if oi['action'] == '買進' else kgi.Action.Sell
        price = {'限價': float(oi['price']),
                 '市價': kgi.PriceType.MKT,
                 '範圍市價': kgi.PriceType.RangeMarket}[oi['price_type']]
        kw = {'action': action, 'symbol': oi['symbol'], 'qty': int(oi['qty']),
              'price': price, 'time_in_force': kgi.TimeInForce.ROD}
        if oi['trade_type'] != '期貨':
            kw['order_cond'] = kgi.OrderCond.CASH
            kw['odd_lot'] = (kgi.OddLot.Odd if oi['trade_type'] == '零股'
                             else kgi.OddLot.Common)
        return kw

    def build_order(self, oi):
        """凱基沒有「先組委託物件、再送出」的兩段式 —— create_order 本身就會
        送出。因此這裡回傳的是「準備好的呼叫參數」,真正送出在 place_order。
        這樣仍然保留了「不連線也能檢查組出來的委託對不對」的能力。
        """
        return self._order_kwargs(oi)

    def place_order(self, contract, oi):
        """送出委託。contract 參數為介面相容而保留 —— 凱基是用 symbol 字串
        下單,不需要合約物件 (由 order_intent 帶進來的 oi['symbol'])。
        """
        acc = oi.get('account')
        if not acc:
            # 見檔頭差異 2:凱基沒有可沿用的預設帳號。
            raise ValueError("凱基下單必須在策略中指定帳號 (凱基沒有預設帳號可用)")
        if self.account_object(acc) is None:
            raise ValueError(f"找不到指定的凱基帳號 {acc} (請重新登入或在策略中重選帳號)")

        kind = 'futures' if oi['trade_type'] == '期貨' else 'stock'
        flag_kind = self._account_kind(acc)
        if flag_kind is not None and flag_kind != kind:
            raise ValueError(
                f"帳號 {acc} 是{'期貨' if flag_kind == 'futures' else '證券'}戶,"
                f"不能用來下{oi['trade_type']}單")

        kw = self._order_kwargs(oi)
        # 綁定與送單必須不可分割 (見檔頭差異 1)。
        with self._order_lock:
            self._bind(kind, acc)
            api_order = self.api.FutOrder if kind == 'futures' else self.api.Order
            return api_order.create_order(**kw)

    def order_status_text(self, trade):
        st = getattr(getattr(trade, 'order_status', None), 'status', '')
        return getattr(st, 'name', None) or str(st) or '送出'
