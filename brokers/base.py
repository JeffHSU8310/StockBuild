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
