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

    # ------------------------------------------------------------------
    # 【ADR-139】永豐 API 測試(模擬環境的登入測試 + 證券/期貨下單測試)
    # ------------------------------------------------------------------
    # 這一段**完全不碰** self.api。它自己開一個 simulation=True 的臨時連線,
    # 用完就丟。理由:使用者可能正登著正式環境在跑策略,測試若共用同一個
    # 連線物件,等於把他從實盤踢下線 —— 那是絕對不能發生的副作用。
    #
    # 規格出處:https://sinotrade.github.io/zh/tutor/prepare/terms/

    def signed_rows(self):
        """回傳 [(帳號, 種類, signed), ...] 給 core.api_test.signed_summary()。

        **要用正式模式登入的 self.api 查**才有意義(官方就是這樣說的),
        所以這個方法讀的是既有的正式連線,不另外建連線。
        """
        rows = []
        for acc in (self._accounts() or []):
            kind = getattr(getattr(acc, 'account_type', None), 'value', None) \
                or str(getattr(acc, 'account_type', '') or '')
            rows.append((self.account_id(acc), kind, bool(getattr(acc, 'signed', False))))
        return rows


class SinopacApiTestSession:
    """一次性的模擬環境連線,專門用來跑永豐要求的 API 測試。

    刻意做成獨立類別而不是 SinopacBroker 的方法:
      - 它的生命週期跟正式連線完全無關(建立 → 測完 → 丟掉);
      - `simulation=True` 這件事被關在這個型別裡,不可能被誤用到正式連線上。

    ## 與鐵則 14 的關係(這一點必須講清楚)

    鐵則 14 說「只有 `_confirm_and_place_order()` 可以呼叫 `place_order()`」。
    這裡出現了第二個呼叫點,是 ADR-139 明確記錄的例外,而且用兩道更強的閘門
    換取這個例外:

      1. **送出前逐次檢查 `self.simulation is True`**,不是只在建構時檢查。
         非模擬連線走到這裡一律 raise,不會有「設定跑掉就打到正式環境」。
      2. GUI 那邊仍然**先跳確認視窗**才會走到這裡 —— 鐵則 14 真正要保護的
         「沒有委託在使用者不知情下送出去」完全沒有被放寬。

    也就是說:例外的是「哪個函式可以呼叫」,不是「要不要確認」。
    """

    def __init__(self):
        if not HAS_SJ:
            raise RuntimeError("沒有安裝 shioaji,無法進行 API 測試")
        # simulation=True 是這個型別存在的唯一理由。
        self.api = sj.Shioaji(simulation=True)
        self.simulation = True
        self.accounts = []

    # ---- 步驟一:登入測試 ----
    def login(self, api_key, secret_key):
        """官方要求的第一項測試。回傳 accounts。"""
        self._assert_simulation()
        self.accounts = self.api.login(api_key=api_key, secret_key=secret_key) or []
        return self.accounts

    def account_rows(self):
        """把登入回傳的帳戶攤平成 [(帳號, 種類, signed), ...] 純資料。

        排版留給 core.api_test.accounts_text() —— adapter 只負責取欄位。
        """
        rows = []
        for acc in (self.accounts or []):
            kind = getattr(getattr(acc, 'account_type', None), 'value', None) \
                or str(getattr(acc, 'account_type', '') or '')
            rows.append((str(getattr(acc, 'account_id', '') or ''), kind,
                         bool(getattr(acc, 'signed', False))))
        return rows

    def stock_account(self):
        return getattr(self.api, 'stock_account', None)

    def futopt_account(self):
        return getattr(self.api, 'futopt_account', None)

    # ---- 步驟二:下單測試 ----
    def stock_contract(self, code):
        """模擬環境的合約查詢。TSE 找不到就找 OTC,再找不到回 None。"""
        self._assert_simulation()
        stocks = getattr(getattr(self.api, 'Contracts', None), 'Stocks', None)
        if stocks is None:
            return None
        for board in ('TSE', 'OTC'):
            b = getattr(stocks, board, None)
            if b is None:
                continue
            c = None
            try:
                c = b[code]
            except Exception:
                c = getattr(b, f"{board}{code}", None)
            if c is not None:
                return c
        return None

    def futures_months(self, symbol):
        """回傳 [(代碼, 交割日), ...],交給 core.api_test.pick_near_month() 挑。

        挑「哪一個月份」是純規則,放 core/ 才測得到;這裡只負責把 SDK 物件
        攤平成純資料(P-67:規則不要在 adapter 裡再寫一份)。
        """
        self._assert_simulation()
        futs = getattr(getattr(self.api, 'Contracts', None), 'Futures', None)
        group = getattr(futs, symbol, None) if futs is not None else None
        rows = []
        for c in (group or []):
            rows.append((getattr(c, 'code', ''), getattr(c, 'delivery_date', '')))
        return rows

    def futures_contract(self, symbol, code):
        self._assert_simulation()
        futs = getattr(getattr(self.api, 'Contracts', None), 'Futures', None)
        group = getattr(futs, symbol, None) if futs is not None else None
        for c in (group or []):
            if getattr(c, 'code', '') == code:
                return c
        return None

    def place_stock_test_order(self, contract, price, qty):
        """證券下單測試。逐欄位照官方範例:限價 ROD、整股、現股、買進。"""
        self._assert_simulation()
        order = self.api.Order(
            action=sj.constant.Action.Buy,
            price=float(price),
            quantity=int(qty),
            price_type=sj.constant.StockPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            order_lot=sj.constant.StockOrderLot.Common,
            order_cond=sj.constant.StockOrderCond.Cash,
            account=self.api.stock_account,
        )
        return self.api.place_order(contract, order)

    def place_futures_test_order(self, contract, price, qty):
        """期貨下單測試。逐欄位照官方範例:限價 ROD、OCType.Auto、買進。"""
        self._assert_simulation()
        order = self.api.Order(
            action=sj.constant.Action.Buy,
            price=float(price),
            quantity=int(qty),
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.Auto,
            account=self.api.futopt_account,
        )
        return self.api.place_order(contract, order)

    def close(self):
        try:
            self.api.logout()
        except Exception:
            pass

    # ---- 閘門 ----
    def _assert_simulation(self):
        """每一次動作都重驗一遍,不是只在 __init__ 驗。

        「建構時驗過就好」的假設在這裡不成立:這個物件會被丟進背景執行緒、
        跨好幾秒的流程,中間任何人動到 self.simulation 都必須立刻擋下。
        這道檢查很便宜,而它擋的是「測試單打到正式環境」。
        """
        if self.simulation is not True:
            raise RuntimeError(
                "API 測試連線不是模擬模式,已拒絕送出 —— 這道檢查是為了確保"
                "測試單絕不可能打到正式環境(ADR-139)")
