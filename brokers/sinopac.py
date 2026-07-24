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

try:
    import shioaji as sj
    HAS_SJ = True
except ImportError:
    HAS_SJ = False


class SinopacBroker(BrokerClient):
    name = "sinopac"

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
        self.api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=contracts_timeout)

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
