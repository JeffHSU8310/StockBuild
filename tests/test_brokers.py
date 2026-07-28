# -*- coding: utf-8 -*-
"""
tests/test_brokers.py — 券商 adapter 離線測試 (ADR-111)

【為什麼要獨立一個測試檔】`tests/test_core.py` 測的是 `core/`、`data/` 那種
零第三方依賴的純邏輯 (鐵則 11)。`brokers/` 不一樣:它的存在理由就是封裝
各家券商 SDK 的差異,一定會依賴 SDK。這裡用「照真實 SDK 原始碼複刻的假
模組」來測,讓 adapter 在沒有帳號、沒有連線、甚至沒裝套件的環境也能驗證。

【假 SDK 的簽名來源】不是憑印象寫的。是把 PyPI 上的 `kgisuperpy` wheel
解開後,逐一對照下列檔案抄出來的:

**已核對的版本:2.0.8 與 2.1.0 (2026-07-28 發布)。** 下列每一個簽名與列舉
值在兩版之間**完全相同**,所以這份假 SDK 對最新版仍然有效。2.1.0 的改動
集中在報價 (新增 Quote_sw / quote_starwave) 與選擇權欄位,與下單路徑無關。
  kgisuperpy/trading/_trade_base.py   Action/TimeInForce/PriceType/OddLot/OrderCond 的值
  kgisuperpy/trading/Order.py         create_order(action, symbol, qty, price,
                                      time_in_force, order_cond, odd_lot, name)
  kgisuperpy/trading/FutOrder.py      create_order(action, symbol, qty, price,
                                      time_in_force)   ← 沒有 order_cond/odd_lot
  kgisuperpy/main.py                  _set_Account / _set_FutAccount 會重新指派
                                      api.Order / api.FutOrder
  kgisuperpy/CA.py                    _acc = {account: broker_id};
                                      _show_account() 的 account_flag 是 證券/期貨/複委託

假 SDK 若與真 SDK 不符,這裡全綠也沒有意義——所以上面每一行都標了出處,
日後凱基改版時可以逐項重新核對。真實連線行為仍然只能實機驗證。
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import broker_ipc
from core import order_intent
import brokers.kgi as kgi_mod
from brokers import kgi_proxy


# ---------------------------------------------------------------------------
# 假 kgisuperpy (簽名與列舉值全部照 2.0.8 原始碼)
# ---------------------------------------------------------------------------
class Action(Enum):
    Buy = "B"
    Sell = "S"


class TimeInForce(Enum):
    ROD = 0
    IOC = 1
    FOK = 2


class PriceType(Enum):
    LMT = 0
    MKT = '1'
    LimitDown = 1
    LimitUp = 9
    Reference = 5
    StopLossMarket = '2'
    StopLossLimit = '3'
    RangeMarket = '4'


class OddLot(Enum):
    Common = 0
    Fixing = 2
    Odd_AfterMarket = 1
    Odd = 4


class OrderCond(Enum):
    CASH = 0
    MARGIN = 3
    SHORT_SELLING = 4


class _Status(Enum):
    Submitted = "Submitted"


class _OrderStatus:
    def __init__(self):
        self.status = _Status.Submitted


class _Trade:
    def __init__(self):
        self.order_status = _OrderStatus()


class _OrderAPI:
    """對應 kgisuperpy.trading.Order.OrderAPI (證券)。"""

    def __init__(self, owner, account):
        self.owner, self.account = owner, account

    def create_order(self, action, symbol, qty, price=None,
                     time_in_force=TimeInForce.ROD, order_cond=OrderCond.CASH,
                     odd_lot=False, name=''):
        self.owner.sent.append({
            'kind': 'stock', 'account': self.account, 'action': action,
            'symbol': symbol, 'qty': qty, 'price': price,
            'time_in_force': time_in_force, 'order_cond': order_cond,
            'odd_lot': odd_lot, 'name': name})
        return _Trade()


class _FutOrderAPI:
    """對應 kgisuperpy.trading.FutOrder (期貨:沒有 order_cond / odd_lot)。"""

    def __init__(self, owner, account):
        self.owner, self.account = owner, account

    def create_order(self, action, symbol, qty, price,
                     time_in_force=TimeInForce.ROD):
        self.owner.sent.append({
            'kind': 'futures', 'account': self.account, 'action': action,
            'symbol': symbol, 'qty': qty, 'price': price,
            'time_in_force': time_in_force})
        return _Trade()


class _ObjOrder:
    def __init__(self, acc):
        self._acc = dict(acc)


class FakeLogin:
    """對應 kgisuperpy.main.login。重點是 _set_Account 會**重新指派** Order。"""

    def __init__(self, accounts):
        # accounts: [(account_id, broker_id, flag)]
        self._ObjOrder = _ObjOrder({a: b for a, b, _f in accounts})
        self._rows = [{'account': a, 'broker_id': b, 'account_flag': f}
                      for a, b, f in accounts]
        self.sent = []
        self.binds = []
        self.Order = None
        self.FutOrder = None
        self.on_bind = None          # 測試用:綁定時的 hook

    def _show_account(self):
        return list(self._rows)

    def _set_Account(self, account):
        # 【重要】hook 要在指派**之後**呼叫。競態窗口是「綁定完成 → 呼叫端
        # 讀 api.Order」這一段:另一條執行緒在這個空檔重新綁定,先前那條就
        # 會讀到別人的 Order。把延遲放在指派之前撐不開這個窗口,測試會變成
        # 永遠綠燈的假測試 (實測過:拿掉鎖也照樣通過)。
        self.binds.append(('stock', account))
        self.Order = _OrderAPI(self, account)
        if self.on_bind:
            self.on_bind()

    def _set_FutAccount(self, account):
        self.binds.append(('futures', account))
        self.FutOrder = _FutOrderAPI(self, account)
        if self.on_bind:
            self.on_bind()

    def _logout(self):
        self.Order = self.FutOrder = None


class _FakeKgiModule:
    Action = Action
    TimeInForce = TimeInForce
    PriceType = PriceType
    OddLot = OddLot
    OrderCond = OrderCond


# ---------------------------------------------------------------------------
class KGIBrokerTestBase(unittest.TestCase):
    ACCOUNTS = [('1234567', '9A95', '證券'), ('7654321', 'F031', '期貨')]

    def setUp(self):
        self._orig_kgi = kgi_mod.kgi
        self._orig_has = kgi_mod.HAS_KGI
        kgi_mod.kgi = _FakeKgiModule
        kgi_mod.HAS_KGI = True
        self.b = kgi_mod.KGIBroker()
        self.api = FakeLogin(self.ACCOUNTS)
        self.b.api = self.api
        self.b.logged_in = True

    def tearDown(self):
        kgi_mod.kgi = self._orig_kgi
        kgi_mod.HAS_KGI = self._orig_has

    def _oi(self, **kw):
        s = {'symbol': '2330', 'trade_type': '股票', 'price_type': '限價',
             'slippage_ticks': 0, 'broker': 'kgi', 'broker_account': '1234567'}
        s.update(kw.pop('strategy', {}))
        oi = order_intent.build_live_order(
            s, {'action': kw.pop('action', '買進'), 'qty': kw.pop('qty', 1),
                'price': kw.pop('price', 600.0)},
            kw.pop('asset', 'stock'))
        oi.update(kw)
        return oi


class TestKGIAccounts(KGIBrokerTestBase):
    def test_list_accounts_shows_type(self):
        """證券戶與期貨戶要走不同的下單物件,選錯類別直接送不出去——
        使用者在下拉選單就得看得出來是哪一種。"""
        accs = dict(self.b.list_accounts())
        self.assertIn('1234567', accs)
        self.assertIn('證券', accs['1234567'])
        self.assertIn('期貨', accs['7654321'])

    def test_account_object_rejects_unknown(self):
        self.assertEqual(self.b.account_object('1234567'), '1234567')
        for bad in ('9999999', '', None, '  '):
            self.assertIsNone(self.b.account_object(bad))

    def test_list_accounts_empty_when_not_logged_in(self):
        """未登入時只是列不出帳號,不可拋例外把編輯器視窗打掉。"""
        self.b.api = None
        self.assertEqual(self.b.list_accounts(), [])
        self.assertIsNone(self.b.account_object('1234567'))


class TestKGIOrderMapping(KGIBrokerTestBase):
    def test_limit_order_maps_to_float_price(self):
        self.b.place_order(None, self._oi())
        s = self.api.sent[-1]
        self.assertEqual(s['action'], Action.Buy)
        self.assertEqual(s['symbol'], '2330')
        self.assertEqual(s['price'], 600.0)
        self.assertEqual(s['time_in_force'], TimeInForce.ROD)
        self.assertEqual(s['order_cond'], OrderCond.CASH)
        self.assertEqual(s['odd_lot'], OddLot.Common)

    def test_market_order_maps_to_pricetype_mkt(self):
        """凱基的市價不是「price 送 0」而是 price 傳 PriceType.MKT ——
        送 0 會被當成限價 0 元,是最危險的誤譯之一。"""
        oi = self._oi(strategy={'price_type': '市價'})
        self.b.place_order(None, oi)
        self.assertEqual(self.api.sent[-1]['price'], PriceType.MKT)

    def test_sell_action(self):
        self.b.place_order(None, self._oi(action='賣出'))
        self.assertEqual(self.api.sent[-1]['action'], Action.Sell)

    def test_odd_lot_uses_intraday_odd(self):
        """OddLot.Odd(4)=盤中零股;Odd_AfterMarket(1)=盤後零股。
        用錯會變成掛到盤後,當天不會成交。"""
        oi = self._oi(strategy={'trade_type': '零股'}, qty=100)
        self.b.place_order(None, oi)
        s = self.api.sent[-1]
        self.assertEqual(s['odd_lot'], OddLot.Odd)
        self.assertNotEqual(s['odd_lot'], OddLot.Odd_AfterMarket)
        self.assertEqual(s['price'], 600.0)      # 零股一律限價 (鐵則6)

    def test_futures_uses_futorder_without_stock_only_fields(self):
        """期貨的 create_order 沒有 order_cond / odd_lot 參數,
        多傳會直接 TypeError。"""
        oi = self._oi(strategy={'trade_type': '期貨', 'symbol': 'TXFG5'},
                      price=18000.0, asset='future')
        oi['account'] = '7654321'
        self.b.place_order(None, oi)
        s = self.api.sent[-1]
        self.assertEqual(s['kind'], 'futures')
        self.assertNotIn('order_cond', s)
        self.assertNotIn('odd_lot', s)
        self.assertEqual(s['price'], 18000.0)

    def test_range_market_maps_to_rangemarket(self):
        oi = self._oi(strategy={'trade_type': '期貨', 'symbol': 'TXFG5',
                                'price_type': '範圍市價'},
                      price=18000.0, asset='future')
        oi['account'] = '7654321'
        self.b.place_order(None, oi)
        self.assertEqual(self.api.sent[-1]['price'], PriceType.RangeMarket)

    def test_build_order_does_not_send(self):
        """build_order 只組參數不送出——這是「不連線也能檢查委託對不對」
        的前提。"""
        self.b.build_order(self._oi())
        self.assertEqual(self.api.sent, [])


class TestKGISafety(KGIBrokerTestBase):
    def test_missing_account_is_refused(self):
        """凱基沒有可沿用的預設帳號 (api.Order 在 _set_Account 前不存在),
        所以沒指定就必須明確拒單,不能猜一個。"""
        oi = self._oi()
        oi['account'] = None
        with self.assertRaises(ValueError) as cm:
            self.b.place_order(None, oi)
        self.assertIn('指定帳號', str(cm.exception))
        self.assertEqual(self.api.sent, [])

    def test_unknown_account_is_refused(self):
        """最嚴重的失效模式是默默送到別的帳戶,所以找不到就拒單。"""
        oi = self._oi()
        oi['account'] = '9999999'
        with self.assertRaises(ValueError) as cm:
            self.b.place_order(None, oi)
        self.assertIn('9999999', str(cm.exception))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.api.binds, [], "拒單時不可先綁定帳號")

    def test_wrong_account_type_is_refused(self):
        """用證券戶下期貨單 (或反過來) 要在本地擋下,不送去給券商退單。"""
        oi = self._oi(strategy={'trade_type': '期貨', 'symbol': 'TXFG5'},
                      price=18000.0, asset='future')
        oi['account'] = '1234567'          # 證券戶
        with self.assertRaises(ValueError) as cm:
            self.b.place_order(None, oi)
        self.assertIn('證券', str(cm.exception))
        self.assertEqual(self.api.sent, [])

    def test_login_without_package_raises_clear_message(self):
        kgi_mod.HAS_KGI = False
        with self.assertRaises(RuntimeError) as cm:
            kgi_mod.KGIBroker().login('A123', 'pw')
        self.assertIn('3.14', str(cm.exception))

    def test_order_status_text(self):
        t = self.b.place_order(None, self._oi())
        self.assertEqual(self.b.order_status_text(t), 'Submitted')


class TestKGIAccountBindingAtomicity(KGIBrokerTestBase):
    """凱基一次只綁一個帳號,綁定與送單之間若被插隊,單會送到別人的帳戶。

    這是本 adapter 最重要的一段:shioaji 每張單各自帶 account=,沒有這個
    問題;凱基有。多策略同時觸發時這是會真的發生的競態,不是理論風險。
    """

    def test_binds_before_sending(self):
        self.b.place_order(None, self._oi())
        self.assertEqual(self.api.binds[-1], ('stock', '1234567'))

    def test_rebinds_when_account_changes(self):
        self.b.place_order(None, self._oi())
        oi2 = self._oi(strategy={'trade_type': '期貨', 'symbol': 'TXFG5'},
                       price=18000.0, asset='future')
        oi2['account'] = '7654321'
        self.b.place_order(None, oi2)
        self.assertEqual(self.api.binds,
                         [('stock', '1234567'), ('futures', '7654321')])

    def test_does_not_rebind_when_same_account(self):
        """同一個帳號連續下單不必重綁——重綁是網路往返,盤中每次都做會拖慢。"""
        self.b.place_order(None, self._oi())
        self.b.place_order(None, self._oi())
        self.assertEqual(len(self.api.binds), 1)

    def test_concurrent_orders_never_cross_accounts(self):
        """兩條執行緒同時對不同帳號下單,每一筆送出的帳號都必須跟它自己
        綁定的那個一致。沒有鎖的話,A 綁好帳號後被 B 改掉,A 的單就會
        記在 B 的帳戶上——而且不會有任何錯誤訊息。"""
        import time

        # 讓綁定變慢,把競態窗口撐開;沒有鎖的實作幾乎必然失敗。
        self.api.on_bind = lambda: time.sleep(0.005)

        errors = []

        def run(acct, symbol, n):
            for _ in range(n):
                try:
                    oi = self._oi(strategy={'symbol': symbol})
                    oi['account'] = acct
                    self.b.place_order(None, oi)
                except Exception as e:      # pragma: no cover
                    errors.append(e)

        # 兩個都用證券戶類型的帳號,才不會被「帳號類別不符」提早擋下
        self.api._ObjOrder._acc['7654321'] = 'F031'
        self.api._rows[1]['account_flag'] = '證券'

        t1 = threading.Thread(target=run, args=('1234567', '2330', 25))
        t2 = threading.Thread(target=run, args=('7654321', '2317', 25))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.api.sent), 50)
        for s in self.api.sent:
            expect = '1234567' if s['symbol'] == '2330' else '7654321'
            self.assertEqual(s['account'], expect,
                             f"委託 {s['symbol']} 被送到帳戶 {s['account']} (應為 {expect})")


class TestBrokerIPCProtocol(unittest.TestCase):
    """【ADR-112】IPC 協定的純邏輯。"""

    def test_request_response_roundtrip(self):
        line = broker_ipc.encode_request(7, 'ping', {'a': 1})
        obj = broker_ipc.decode_line(line)
        self.assertEqual((obj['id'], obj['op'], obj['args']), (7, 'ping', {'a': 1}))
        r = broker_ipc.decode_line(broker_ipc.encode_response(7, True, result={'x': 2}))
        self.assertEqual(broker_ipc.parse_response(r, 7), {'x': 2})

    def test_response_id_must_match(self):
        """上一個請求逾時、回應晚到時,不可被當成新請求的答案——在下單這條路
        上那等於「拿 A 單的結果判斷 B 單成功與否」。"""
        r = broker_ipc.decode_line(broker_ipc.encode_response(7, True, result={}))
        with self.assertRaises(broker_ipc.BrokerIPCError):
            broker_ipc.parse_response(r, 8)

    def test_error_response_raises(self):
        r = broker_ipc.decode_line(broker_ipc.encode_response(1, False, error='帳號錯誤'))
        with self.assertRaises(broker_ipc.BrokerIPCError) as cm:
            broker_ipc.parse_response(r, 1)
        self.assertIn('帳號錯誤', str(cm.exception))

    def test_missing_response_raises(self):
        with self.assertRaises(broker_ipc.BrokerIPCError):
            broker_ipc.parse_response(None, 1)

    def test_garbage_line_raises_not_silently_skipped(self):
        """壞行安靜略過會變成「主程式永遠在等一個不會來的回應」。"""
        with self.assertRaises(broker_ipc.BrokerIPCError):
            broker_ipc.decode_line('this is not json')
        self.assertIsNone(broker_ipc.decode_line('   '))

    def test_place_order_is_non_idempotent(self):
        """下單絕不可自動重送;查詢類才可以。"""
        self.assertFalse(broker_ipc.is_idempotent('place_order'))
        for op in ('ping', 'list_accounts', 'login', 'logout'):
            self.assertTrue(broker_ipc.is_idempotent(op), op)

    def test_redact_hides_password(self):
        """IPC 內容很適合寫進日誌排查,但登入請求裡有身分證與密碼。"""
        out = broker_ipc.redact({'person_id': 'A123456789', 'person_pwd': 'secret',
                                 'nested': {'ca_passwd': 'p'}})
        self.assertEqual(out['person_pwd'], '***')
        self.assertEqual(out['nested']['ca_passwd'], '***')
        self.assertEqual(out['person_id'], 'A123456789')   # 帳號本身不遮,才查得出是誰

    def test_unknown_outcome_message_tells_user_what_to_do(self):
        m = broker_ipc.unknown_outcome_message('place_order')
        self.assertIn('無法確認', m)
        self.assertIn('不會自動重送', m)
        self.assertIn('券商', m)


class _FakeProc:
    """假子行程:可以設定「回應要不要來」以測試失聯路徑。"""

    def __init__(self, behaviour='ok'):
        self.behaviour = behaviour
        self.written = []
        self._pending = []
        self._alive = True
        self.stdin = self
        self.stdout = self
        self.stderr = self
        self.killed = 0

    # stdin
    def write(self, data):
        self.written.append(data)
        obj = broker_ipc.decode_line(data)
        if self.behaviour == 'ok':
            self._pending.append(broker_ipc.encode_response(
                obj['id'], True, result={'status_text': 'Submitted',
                                         'accounts': [['1', 'a (證券)']]}))
        elif self.behaviour == 'wrong_id':
            self._pending.append(broker_ipc.encode_response(
                obj['id'] + 99, True, result={}))
        # 'silent' → 什麼都不回 (模擬子行程卡住)

    def flush(self):
        pass

    def close(self):
        self._alive = False

    # stdout
    def readline(self):
        if self._pending:
            return self._pending.pop(0)
        if self.behaviour == 'eof':
            return ''
        time.sleep(5)          # 模擬卡住,讓 proxy 的逾時生效
        return ''

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self.killed += 1
        self._alive = False


class KGIProxyTestBase(unittest.TestCase):
    def _proxy(self, behaviour='ok'):
        p = kgi_proxy.KGIProcessBroker(python_path='/usr/bin/false')
        proc = _FakeProc(behaviour)
        p._proc = proc
        p._start = lambda: None          # 不真的開行程
        return p, proc


class TestKGIProxy(KGIProxyTestBase):
    def test_place_order_success(self):
        p, proc = self._proxy('ok')
        t = p.place_order(None, {'symbol': '2330', 'qty': 1, 'account': '1'})
        self.assertEqual(p.order_status_text(t), 'Submitted')

    def test_place_order_no_reply_raises_unknown_not_failure(self):
        """本檔最重要的一條:委託送出後失聯,必須是「不確定」這個獨立型別,
        不可混進一般錯誤——呼叫端會把一般錯誤當成「沒送出」而重試。"""
        p, proc = self._proxy('silent')
        kgi_proxy.ORDER_TIMEOUT = 0.3
        with self.assertRaises(broker_ipc.OrderOutcomeUnknown) as cm:
            p.place_order(None, {'symbol': '2330', 'qty': 1, 'account': '1'})
        self.assertIn('無法確認', str(cm.exception))

    def test_place_order_never_resent_after_timeout(self):
        """失聯後絕不可自動重送 —— 重送就是重複下單。"""
        p, proc = self._proxy('silent')
        kgi_proxy.ORDER_TIMEOUT = 0.3
        with self.assertRaises(broker_ipc.OrderOutcomeUnknown):
            p.place_order(None, {'symbol': '2330', 'qty': 1, 'account': '1'})
        sends = [w for w in proc.written if '"place_order"' in w]
        self.assertEqual(len(sends), 1, f"下單被送了 {len(sends)} 次 (應為 1 次)")

    def test_idempotent_op_may_retry_once(self):
        """查詢類失聯可以重試 (重送沒有副作用),但不可無限重試。"""
        p, proc = self._proxy('eof')
        p.logged_in = True
        p._start = lambda: None
        p.list_accounts()                      # 失敗但不拋 (回快取)
        pings = [w for w in proc.written if '"list_accounts"' in w]
        self.assertLessEqual(len(pings), 2, "冪等操作最多重試一次")

    def test_wrong_id_response_is_error(self):
        p, proc = self._proxy('wrong_id')
        with self.assertRaises(broker_ipc.BrokerIPCError):
            p.ping()

    def test_list_accounts_empty_when_not_logged_in(self):
        p, _ = self._proxy('ok')
        self.assertEqual(p.list_accounts(), [])

    def test_missing_interpreter_gives_actionable_error(self):
        p = kgi_proxy.KGIProcessBroker(python_path='')
        p.python_path = ''
        with self.assertRaises(RuntimeError) as cm:
            p._start()
        msg = str(cm.exception)
        self.assertIn('3.13', msg)
        self.assertIn('設定', msg)

    def test_build_order_refuses_in_proxy_mode(self):
        """委託轉換只能有一份 (在子行程裡),proxy 不可自己再抄一份。"""
        p, _ = self._proxy('ok')
        with self.assertRaises(NotImplementedError):
            p.build_order({})


class TestKGIWorkerEndToEnd(unittest.TestCase):
    """真的開一個子行程跑 brokers.kgi_worker,驗證協定兩端能對上。

    用主程式的直譯器跑就好 —— 這裡測的是**協定與 stdout 隔離**,不是
    kgisuperpy 本身 (沒裝也要能通過:worker 會回一個明確的錯誤訊息)。
    """

    def _run(self, requests):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, '-m', 'brokers.kgi_worker'],
            input=''.join(requests), capture_output=True, text=True,
            timeout=60, cwd=repo)
        return proc

    def test_ping_roundtrip_and_stdout_is_pure_protocol(self):
        """stdout 只准有協定 JSON。kgisuperpy 內部會 print,混進來就會把
        協定弄壞 —— 所以 worker 啟動時就把 sys.stdout 換掉。"""
        proc = self._run([broker_ipc.encode_request(1, 'ping')])
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, f"stdout 有非協定內容: {proc.stdout[:300]}")
        obj = broker_ipc.decode_line(lines[0])
        # 沒裝 kgisuperpy 時 ping 會回錯誤,有裝則回 pong;兩者都算協定正常
        self.assertEqual(obj['id'], 1)
        self.assertIn(obj['status'], (broker_ipc.ST_OK, broker_ipc.ST_ERROR))

    def test_unknown_op_returns_error_not_crash(self):
        proc = self._run([broker_ipc.encode_request(5, 'rm_rf_everything')])
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        obj = broker_ipc.decode_line(lines[-1])
        self.assertEqual(obj['id'], 5)
        self.assertEqual(obj['status'], broker_ipc.ST_ERROR)

    def test_garbage_input_does_not_kill_worker(self):
        """一行壞資料不該讓 worker 死掉,後續請求要照樣有回應。"""
        proc = self._run(['{not json\n', broker_ipc.encode_request(9, 'ping')])
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        self.assertTrue(lines, f"worker 沒有任何回應;stderr={proc.stderr[:300]}")
        self.assertEqual(broker_ipc.decode_line(lines[-1])['id'], 9)

    def test_ids_are_matched_per_request(self):
        proc = self._run([broker_ipc.encode_request(i, 'ping') for i in (3, 4, 5)])
        ids = [broker_ipc.decode_line(l)['id']
               for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual(ids, [3, 4, 5])


def _find_real_313():
    """找一個真的 3.13 直譯器;沒有就回空字串 (該測試會 skip)。"""
    for cand in ('python3.13', 'python3.12', 'python3.11'):
        for d in (os.environ.get('PATH') or '').split(os.pathsep):
            p = os.path.join(d, cand)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return ''


class TestKGIProxyRealSubprocess(unittest.TestCase):
    """用真的直譯器跑一次完整鏈路:Popen → 寫 stdin → 讀 stdout → 解析。

    前面的 TestKGIProxy 用假 proc 測失聯路徑 (那是真行程不好重現的);這裡
    反過來,測「一切正常時,proxy 與 worker 真的能對話」。兩者都要有,少了
    這個就可能出現「假 proc 全綠、真的一開就掛」的情況。
    """

    def setUp(self):
        self.py = _find_real_313()
        if not self.py:
            self.skipTest('環境沒有 3.11~3.13 直譯器')
        self.p = kgi_proxy.KGIProcessBroker(python_path=self.py)

    def tearDown(self):
        try:
            self.p._stop()
        except Exception:
            pass

    def test_ping_through_real_process(self):
        try:
            r = self.p.ping()
        except broker_ipc.BrokerIPCError as e:
            # 沒裝 kgisuperpy 時 worker 會回明確錯誤 —— 協定本身仍算通了
            self.assertIn('kgisuperpy', str(e) + ' ' + str(e.__cause__ or ''))
            return
        self.assertTrue(r.get('pong'))
        self.assertTrue(r.get('python', '').startswith('3.'))

    def test_process_is_reused_across_calls(self):
        """常駐行程:第二次呼叫不可重開一個新行程 (登入 session 會斷)。"""
        try:
            self.p.ping()
            first = self.p._proc
            self.p.ping()
            self.assertIs(self.p._proc, first, '子行程被重開了,登入狀態會遺失')
        except broker_ipc.BrokerIPCError:
            self.skipTest('worker 無法初始化 (未安裝 kgisuperpy)')

    def test_stop_terminates_process(self):
        try:
            self.p.ping()
        except broker_ipc.BrokerIPCError:
            pass
        proc = self.p._proc
        self.p._stop()
        self.assertIsNone(self.p._proc)
        if proc is not None:
            self.assertIsNotNone(proc.poll(), '子行程沒有結束,會變成殭屍行程')


if __name__ == "__main__":
    unittest.main(verbosity=2)
