"""
brokers.base — 券商 adapter 共用介面 (ADR-097)。

【階段 0 範圍】目前只涵蓋「連線生命週期」：建立連線物件、登入、啟用憑證、
註冊 callback、登出。委託組裝、報價訂閱、部位查詢、K 線下載等方法尚未
抽象化——這些呼叫目前在 stock_app_pro.py 裡有上百處、且深度依賴 GUI 端
的狀態 (self.current_contract / self._wl_contract_cache / self.quote_lock
等)，此工作環境沒有畫面可以實機驗證 shioaji 真實行為，貿然搬動風險過高。

等群益/兆豐/凱基其中一家 adapter 真的要動工時，才會知道這層介面該怎麼設計
才不會用猜的，屆時再依實際需求擴充這裡的 BrokerClient 與各家 adapter，
並同步更新 ARCHITECTURE.md / DECISIONS.md。
"""


class BrokerClient:
    """所有券商 adapter 的共用基底類別。子類別至少要實作 login/logout。"""

    name = "base"

    def __init__(self):
        self.api = None
        self.logged_in = False

    def new_session(self):
        """捨棄目前連線物件、建立全新的底層 SDK 實例，回傳新實例。"""
        raise NotImplementedError

    def login(self, **credentials):
        raise NotImplementedError

    def logout(self):
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 【ADR-110 階段 1】下單:各家 adapter 把「券商中立的委託意圖」
    # (core/order_intent.build_live_order 的輸出) 翻成自家 SDK 的委託物件。
    #
    # 切在這裡的理由:讓價、檔位對齊、零股限價 ROD 這些是**交易所層級**的
    # 規則,四家券商完全一樣,所以留在 core/;只有「這家 SDK 用什麼常數表示
    # 買進/限價/ROD」才是各家的差異,由 adapter 負責。
    # ------------------------------------------------------------------
    def build_order(self, order_intent):
        """委託意圖 dict → 自家 SDK 的委託物件。不送出,只組裝。

        拆成獨立方法是為了讓「組出來的委託長什麼樣」可以在沒有連線、
        沒有真錢的情況下被驗證——這是唯一能在 headless 環境確認
        「換了抽象層之後送出去的東西完全沒變」的方法。
        """
        raise NotImplementedError

    def place_order(self, contract, order_intent):
        """組裝並送出。回傳 (成功與否, 給人看的訊息或 trade 物件)。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 【ADR-110 階段 2】帳號:同一家券商底下可能有多個帳號 (證券戶/期貨戶/
    # 不同分公司),策略要能指定送到哪一個。
    # ------------------------------------------------------------------
    display_name = "未命名券商"

    def list_accounts(self):
        """回傳 [(account_id, 顯示名稱), ...];未登入回空 list。

        給策略編輯器填下拉選單用。回空 list 而不是拋例外,是因為使用者很可能
        在還沒登入時就打開編輯器,那不是錯誤。
        """
        return []

    def account_object(self, account_id):
        """account_id → 自家 SDK 的帳號物件;找不到回 None。

        找不到時**必須**回 None 讓呼叫端擋下,絕不可退回「預設帳號」——
        使用者指定了 A 帳戶,系統卻默默送到 B 帳戶,是這個功能最嚴重的
        失效模式 (真錢下錯戶頭)。
        """
        return None
