"""
brokers.sinopac — 永豐金 shioaji adapter (ADR-097 階段 0)。

把原本寫在 stock_app_pro.py 裡「怎麼跟 shioaji 建立連線/登入/啟用憑證/
註冊 callback/登出」的邏輯搬到這裡，stock_app_pro.py 改成透過
self.brokers['sinopac'] 這個實例操作，而不是自己直接 `sj.Shioaji(...)`。

【階段 0 範圍聲明】這裡的方法都是「一對一」包住對應的 shioaji 呼叫，本身
不吞例外、不做任何日誌——原本 stock_app_pro.py 裡每個呼叫點各自的
try/except 與日誌訊息一律保留在呼叫端，確保這次搬動是「零行為改變」。
委託組裝、報價訂閱 (per-symbol subscribe)、部位查詢、K 線下載等其餘上百處
shioaji 呼叫暫不搬動，見 brokers/base.py 開頭的說明與 DECISIONS_ADR097.md。
"""
from brokers.base import BrokerClient
from core import sj_compat

try:
    import shioaji as sj
    HAS_SJ = True
except ImportError:
    HAS_SJ = False


class SinopacBroker(BrokerClient):
    name = "sinopac"
    display_name = "永豐金"

    def __init__(self):
        super().__init__()
        if HAS_SJ:
            self.new_session()

    def new_session(self):
        """捨棄目前連線物件、建立全新的 Shioaji 實例 (對應原本重登前的重建邏輯)。"""
        self.api = sj.Shioaji(simulation=False)
        self.logged_in = False
        return self.api

    def login(self, api_key, secret_key, contracts_timeout=10000):
        """【ADR-114】1.7 移除了 `contracts_timeout` (改成查詢時自動載入合約)。

        不偵測版本號,直接問 `login` 這個函式收不收這個參數 —— 版本號與實際
        行為不一定同步 (1.7 的升級指南就與它自己的型別定義有出入),而簽名
        不會騙人。丟掉的參數會回報給呼叫端記進系統日誌,不靜悄悄地忽略。
        """
        kw, dropped = sj_compat.supported_kwargs(
            self.api.login, {'contracts_timeout': contracts_timeout})
        self.api.login(api_key=api_key, secret_key=secret_key, **kw)
        self.dropped_login_kwargs = dropped
        return dropped

    # ---- 【ADR-114】合約查詢:1.5.6 與 1.7 的形狀不同,收斂在這裡 ----
    def index_contract(self, market):
        """加權(TSE)/櫃買(OTC)指數合約。找不到回 None。"""
        return sj_compat.resolve_index(
            getattr(getattr(self.api, 'Contracts', None), 'Indexs', None), market)

    def stock_contract(self, code):
        """個股合約。先用 .get() 直接查,查不到才整批掃描比對代碼。

        掃描時 symbol 與 code 都比對:1.7 列舉合約可能回傳只有 code 的輕量
        型別,只比 symbol 會變成「查無此代碼」。
        """
        stocks = getattr(getattr(self.api, 'Contracts', None), 'Stocks', None)
        if stocks is None:
            return None
        c = sj_compat._try_get(stocks, str(code))
        if c is not None:
            return c
        try:
            return next((x for x in stocks
                         if sj_compat.match_contract_code(x, code)), None)
        except Exception:
            return None

    def sdk_version(self):
        return getattr(sj, '__version__', '?') if HAS_SJ else '(未安裝)'

    def activate_ca(self, ca_path, ca_pw, pid):
        self.api.activate_ca(ca_path=ca_path, ca_passwd=ca_pw, person_id=pid)

    def set_quote_callbacks(self, on_tick_stk, on_bidask_stk, on_tick_fop, on_bidask_fop):
        self.api.quote.set_on_tick_stk_v1_callback(on_tick_stk)
        self.api.quote.set_on_bidask_stk_v1_callback(on_bidask_stk)
        self.api.quote.set_on_tick_fop_v1_callback(on_tick_fop)
        self.api.quote.set_on_bidask_fop_v1_callback(on_bidask_fop)

    def set_order_callback(self, on_order_deal):
        self.api.set_order_callback(on_order_deal)

    def logout(self):
        self.api.logout()
        self.logged_in = False

    # ------------------------------------------------------------------
    # 【ADR-110 階段 1】委託意圖 → shioaji Order
    #
    # 這段是從 stock_app_pro.py 的 _place_strategy_order() 原封不動搬過來的,
    # 每一個常數與參數都保持一致 —— 這次搬動的驗收標準是「組出來的 Order
    # 跟搬動前逐欄位相同」,有診斷案例守著 (見 diag_repro_issues.py)。
    # ------------------------------------------------------------------
    # ---- 帳號 (ADR-110 階段2) ----
    @staticmethod
    def account_id(acc):
        """shioaji Account → 穩定的字串 id。

        用「分公司代碼-帳號」而不是物件本身,因為這個 id 要存進策略檔;
        重新登入後 Account 物件是新的,只有這組數字不會變。
        """
        return f"{getattr(acc, 'broker_id', '')}-{getattr(acc, 'account_id', '')}"

    def _accounts(self):
        try:
            return list(self.api.list_accounts() or [])
        except Exception:
            # 未登入 / 連線異常時回空 list:編輯器只是填不出下拉選單,
            # 不該因此拋例外把整個視窗打掉。
            return []

    def list_accounts(self):
        """[(account_id, 顯示文字)]。

        【ADR-117】顯示文字的組法搬到 core/sj_compat.account_label():
        1.7 把所有帳戶統一成同一個 Account 類別 (改用 account_type 欄位),
        原本靠類別名稱判斷種類的寫法會讓三個帳戶顯示成完全一樣的字。
        """
        out = []
        for a in self._accounts():
            aid = self.account_id(a)
            out.append((aid, sj_compat.account_label(a, getattr(a, 'account_id', ''))))
        return out

    def account_object(self, account_id):
        want = str(account_id or '').strip()
        if not want:
            return None
        for a in self._accounts():
            if self.account_id(a) == want:
                return a
        return None

    def build_order(self, oi):
        """把 core.order_intent 的輸出翻成 shioaji Order。

        零股與整股分開組,是因為 shioaji 的零股單要多帶 order_lot=IntradayOdd;
        期貨則是完全另一組常數 (FuturesPriceType),連欄位都不一樣 —— 這正是
        「委託組裝必須各家 adapter 自己實作」的具體理由。
        """
        action = sj.constant.Action.Buy if oi['action'] == '買進' else sj.constant.Action.Sell
        px = float(oi['price'])
        qty = int(oi['qty'])
        ptype = oi['price_type']

        # 【ADR-110 階段2】策略沒指定帳號時**完全不帶** account 參數,讓 shioaji
        # 沿用它自己的預設帳號 —— 這樣舊策略送出的委託跟加這個功能之前逐欄位
        # 相同。指定了卻找不到,是呼叫端 (place_order) 要擋的錯誤,不在這裡默默
        # 退回預設帳號。
        extra = {}
        if oi.get('account'):
            acc = self.account_object(oi['account'])
            if acc is not None:
                extra['account'] = acc

        if oi['trade_type'] == '期貨':
            fut_ptype = {'限價': sj.constant.FuturesPriceType.LMT,
                         '市價': sj.constant.FuturesPriceType.MKT,
                         '範圍市價': sj.constant.FuturesPriceType.MKP}[ptype]
            return self.api.Order(price=px, quantity=qty, action=action,
                                  price_type=fut_ptype,
                                  order_type=sj.constant.OrderType.ROD, **extra)
        if oi['trade_type'] == '零股':
            # 【鐵則6】盤中零股單,數量單位=股,只能限價 (order_intent 已保證)。
            return self.api.Order(price=px, quantity=qty, action=action,
                                  price_type=sj.constant.StockPriceType.LMT,
                                  order_type=sj.constant.OrderType.ROD,
                                  order_lot=sj.constant.StockOrderLot.IntradayOdd,
                                  order_cond=sj.constant.StockOrderCond.Cash, **extra)
        stk_ptype = (sj.constant.StockPriceType.MKT if ptype == '市價'
                     else sj.constant.StockPriceType.LMT)
        return self.api.Order(price=px, quantity=qty, action=action,
                              price_type=stk_ptype,
                              order_type=sj.constant.OrderType.ROD,
                              order_lot=sj.constant.StockOrderLot.Common,
                              order_cond=sj.constant.StockOrderCond.Cash, **extra)

    def place_order(self, contract, oi):
        """送出委託,回傳 shioaji trade 物件。

        不吞例外 —— 呼叫端要能分辨「送出失敗」與「送出成功但被退單」,
        在這裡包成 False 會讓上游失去例外型別與訊息 (階段 0 已定的原則)。
        """
        # 【ADR-110 階段2】指定了帳號卻找不到 → 直接拒單。絕不可退回預設帳號:
        # 使用者指定 A 戶、系統默默送到 B 戶,是這個功能最嚴重的失效模式。
        if oi.get('account') and self.account_object(oi['account']) is None:
            raise ValueError(f"找不到指定的永豐帳號 {oi['account']} (請重新登入或在策略中重選帳號)")
        return self.api.place_order(contract, self.build_order(oi))

    def order_status_text(self, trade):
        """從 trade 物件取出狀態字串。各家 SDK 的回傳結構不同,所以放 adapter。"""
        st = getattr(getattr(trade, 'status', None), 'status', '')
        return getattr(st, 'name', st) or '送出'
