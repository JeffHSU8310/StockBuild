"""
diag_repro_issues.py — 重現使用者第五輪回報的三個問題 (修正前基準)。

1. 版面微調滑桿完全無效: 驗證 fig.subplots_adjust 對 mplfinance (add_axes) 面板無效。
2. 委託單清單空白: 用假 Trade (id 空字串, PendingSubmit) 走 _confirm_and_place_order,
   檢查 my_orders 與 tree_orders 的實際內容。
3. 版面數值變更後重繪,面板位置有沒有真的改變。
"""
import sys, os, time, atexit, shutil, tempfile
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diag_mock_tkinter
diag_mock_tkinter.install_mock_tkinter()

import numpy as np
import pandas as pd
import stock_app_pro
from stock_app_pro import StockTradingAppPro

# ---------------------------------------------------------------------------
# 【ADR-115】診斷腳本不可以動到使用者的真實資料檔
#
# 診斷案例會建策略、記模擬成交,而這些最後都會寫進 quant_strategies.json /
# quant_state.json / paper_account.json —— 那是使用者**真正在用**的策略清單與
# 模擬帳戶。跑一次診斷就把它們覆蓋成測試資料,是會實際造成損失的 (策略設定
# 沒了、模擬帳戶的績效紀錄也沒了)。
#
# 這整段時間都是靠「跑完記得 git checkout 還原」在擋,但那只在這個 repo 裡
# 有效:使用者在自己機器上跑診斷時沒有這層保護,而且忘記還原就會直接進版控。
#
# 解法是在建立 App 之前就把這三個檔案改指到暫存目錄。改的是類別屬性,所以
# 之後所有實例都吃到暫存路徑,不必逐一修改診斷案例。
# ---------------------------------------------------------------------------
_diag_tmp = tempfile.mkdtemp(prefix='stockbuild_diag_')
atexit.register(shutil.rmtree, _diag_tmp, True)
for _attr, _fn in (('QT_STRATEGY_FILE', 'quant_strategies.json'),
                   ('QT_STATE_FILE', 'quant_state.json'),
                   ('QT_PAPER_FILE', 'paper_account.json')):
    # 先把使用者真實檔案的內容複製進暫存區:有些診斷案例預期「載入得到既有
    # 策略」,完全空白會讓它們的前提不成立。
    _real = getattr(StockTradingAppPro, _attr)
    _tmp = os.path.join(_diag_tmp, _fn)
    try:
        if os.path.exists(_real):
            shutil.copyfile(_real, _tmp)
    except Exception:
        pass
    setattr(StockTradingAppPro, _attr, _tmp)

# 【ADR-119】關掉「按 X 之後強制結束行程」的保底看門狗:診斷腳本會呼叫
# on_app_close() 驗證關閉流程,但它自己還要繼續跑完其餘案例。
StockTradingAppPro.CLOSE_FORCE_EXIT = False
# 【ADR-122】大範圍分段下載改成原地做完,讓既有的同步斷言 (呼叫一次
# _quant_eval_pass 就檢查結果) 維持有效。ADR-122 自己的案例會臨時關掉它,
# 去驗真正的「背景預抓」時序。
StockTradingAppPro.QT_PREFETCH_SYNC = True
app = StockTradingAppPro()
app.flush_after = getattr(app, "flush_after")  # 來自 _Tk mock
# 【ADR-115 延伸 / ADR-120】app_settings.json 也是使用者的真實設定檔
# (盤勢判斷面板的偏好存在裡面),診斷案例會存檔,同樣改指到暫存目錄。
app.app_settings_file = os.path.join(_diag_tmp, "app_settings.json")



def eval_pass():
    """【ADR-123】跑一輪策略評估,但先避開「K棒邊界後 2 秒」那個窗口。

    _quant_eval_pass() 對分K策略有一道「給資料源 2 秒緩衝」的閘門:

        if (now_dt - boundary).total_seconds() < 2: continue

    1分K 的 boundary 就是「當下這一分鐘」,所以只要診斷剛好在某一分鐘的前
    2 秒跑到這裡,策略就完全不會被評估,斷言於是莫名其妙紅一次。實測約
    3% 的機率,而且每次紅的案例不一定一樣 —— 這種偶發紅最消耗人:會讓人
    去懷疑剛改的東西,而真正的原因是時鐘。

    診斷的既定原則是「與時鐘無關」(ADR-099 才為此把 session_gate 關掉),
    這個窗口是漏網的一個。等過去再跑就好;等待最多 2 秒,不影響總時間。

    只包**不帶參數**的呼叫:帶 now_ts/today_str 的是 _forced,本來就跳過
    邊界閘門,不受影響。
    """
    now = datetime.now()
    if now.second < 2:
        time.sleep(2 - now.second + 0.05)
    return app._quant_eval_pass()


def place_and_settle(ctx, timeout=5.0):
    """【ADR-099】送單 + 等背景執行緒完成 + 沖 after 佇列。

    ADR-096 把 place_order() 改成背景執行緒 (避免同步網路呼叫卡死主執行緒),
    因此 _confirm_and_place_order() 一回來時委託「還沒」寫進 my_orders——
    真正的寫入發生在背景 thread 跑完、safe_after 把 _apply_order_result
    排回主執行緒之後。診斷腳本必須等這兩步都完成才能斷言,否則測到的是
    「還沒寫入」的中間狀態 (這正是本腳本三個下單案例一度失效的原因)。
    """
    import time as _t
    before = len(app._after_queue)
    app._confirm_and_place_order(ctx)
    deadline = _t.time() + timeout
    # 背景 thread 完成的判準:它一定會 safe_after 排入 _apply_order_result
    while _t.time() < deadline and len(app._after_queue) <= before:
        _t.sleep(0.01)
    app.flush_after()


results = []
def run_case(name, fn):
    """【ADR-126】跑一個案例,並且**把時鐘相依的表面凍結住**。

    為什麼要在這裡做:ADR-124 給 `_qt_check_realtime_futures_stops` 補上交易
    時段閘門之後,ADR-123 那個「一般期貨策略仍應被即時停損平倉」的案例就
    **悄悄變成看真實時鐘**了 —— 期貨日盤 (08:45~13:45) 或夜盤 (15:00~05:00)
    跑就過,卡在 13:45~15:00 的空檔跑就紅。我當時連跑 5 次全綠並把它併進
    main,正是因為那 5 次都落在日盤內(P-94 早就寫過:「跑很多次都沒紅」
    只是沒有反證,不是證明)。

    所以改成由 harness 統一預設:市場「開著」、且「不在開盤暖機窗口內」。
    需要休市 / 需要暖機的案例自己在案例內再 patch 一次(它們的 patch 會蓋過
    這裡的預設,`finally` 也還原得回來)。這樣**任何新案例都不會不小心又
    依賴真實時鐘**。

    【ADR-131 追加:同一個教訓的第三次】ADR-127 把日K 快取的新鮮度從「TTL」
    改成「這段期間有沒有跨過開盤」(`any_session_opens_between`) 之後,
    **兩個既有案例又悄悄變成時鐘相依**:
      * ADR-122「日K 類快取放了 10 分鐘仍應命中」
      * ADR-127「35 分鐘內沒有跨過開盤,不可以重抓」
    兩者都用「把快取時間戳往前撥」模擬時間經過,而往前撥出來的區間若剛好
    跨過真實的開盤時刻 (08:45/09:00/09:10/15:00),就會被判定為過期 → 紅。
    也就是**每天 08:45~09:45 與 15:00~15:35 這兩段時間跑診斷必紅**,
    其餘時間全綠 —— 而我先前的驗證全部落在 02:4x / 11:5x / 12:2x。

    這正是 P-97 寫過的那句話:「加了閘門/前置條件之後,回頭看既有測試有沒有
    因此變成時鐘相依」。我寫下來了,卻沒有在改 ADR-127 時真的回頭看。

    修法一樣放在 harness:預設「期間內沒有跨過任何開盤」(= False),
    需要相反值的案例自己 patch(ADR-127 的正反對照本來就自己 patch)。
    """
    # 別名刻意不叫 _ms —— 這個檔案裡 _ms 已經是 core.market_screener
    # (diag_crossref 會把同名別名混在一起看,撞名就誤報跨模組斷鏈;
    #  ADR-122 也踩過一次同樣的事,那次是 _cs)。
    _msess = stock_app_pro.market_session
    _orig_open, _orig_just = _msess.is_market_open, _msess.just_opened
    _orig_between = _msess.any_session_opens_between
    _msess.is_market_open = lambda *a, **k: True
    _msess.just_opened = lambda *a, **k: False
    _msess.any_session_opens_between = lambda *a, **k: False
    try:
        fn()
        results.append((name, "PASS", ""))
    except AssertionError as e:
        results.append((name, "FAIL", str(e)))
    except Exception as e:
        results.append((name, "ERROR", f"{type(e).__name__}: {e}"))
    finally:
        _msess.is_market_open, _msess.just_opened = _orig_open, _orig_just
        _msess.any_session_opens_between = _orig_between


def _make_df(n=200):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    base = np.linspace(60, 110, n) + np.random.RandomState(0).randn(n)
    return pd.DataFrame({
        "Open": base, "High": base + 1, "Low": base - 1,
        "Close": base + 0.3, "Volume": np.random.RandomState(1).randint(1000, 90000, n),
    }, index=idx)


# ---------- 問題1(修正後): 版面數值改變 -> 面板位置真的跟著改變 ----------
def _layout_change_now_repositions_panels():
    app.current_symbol = "0050"; app.current_stock_name = "元大台灣50"
    app.asset_type = "stock"; app.current_df = _make_df()
    app.var_macd.set(True)
    app.chart_layout['margin_left'] = 0.045; app.chart_layout['margin_bottom'] = 0.07
    app.chart_layout['margin_top'] = 0.965; app.chart_layout['hspace'] = 0.14
    app.draw_chart(app.current_df)
    assert app.axlist, "draw_chart 應該要建出 axlist"
    pos1 = [tuple(np.round(ax.get_position().bounds, 4)) for ax in app.axlist[::2]]
    # 模擬使用者把邊界滑桿拉大
    app.chart_layout['margin_left'] = 0.165
    app.chart_layout['margin_bottom'] = 0.25
    app.chart_layout['margin_top'] = 0.99
    app.chart_layout['hspace'] = 0.30
    app.draw_chart(app.current_df)
    pos2 = [tuple(np.round(ax.get_position().bounds, 4)) for ax in app.axlist[::2]]
    # 修正後: 位置必須真的改變 (set_position 生效)
    assert pos1 != pos2, f"版面調整後面板位置竟然沒變 (set_position 沒生效): {pos1}"
    assert abs(app.axlist[0].get_position().bounds[0] - 0.165) < 1e-6, \
        f"主圖左邊界應=0.165,實際 {app.axlist[0].get_position().bounds[0]}"

def _layout_live_preview_applies_instantly():
    # 即時預覽:只改 chart_layout + _apply_chart_margins,不做完整重繪,面板要立刻移動
    app.chart_layout['margin_left'] = 0.05
    app._apply_chart_margins(app.current_fig, app.axlist, app.current_panel_ratios)
    assert abs(app.axlist[0].get_position().bounds[0] - 0.05) < 1e-6, \
        "即時預覽 set_position 應該立刻把左邊界搬到 0.05"

# ---------- 問題2基準: 假 PendingSubmit Trade 走一次下單 ----------
class _FakeStatus:
    def __init__(self):
        class _S: name = "PendingSubmit"
        self.status = _S()
        self.msg = "委託處理中, 請於交易時間確認委託狀態!"

class _FakeTradeOrder:
    id = ""   # PendingSubmit 時 id 還是空字串 (ADR-019 已知情境)
    seqno = "123456"

class _FakeTrade:
    def __init__(self):
        self.order = _FakeTradeOrder()
        self.status = _FakeStatus()

class _FakeApi:
    def place_order(self, contract, order): return _FakeTrade()

def _order_appears_in_my_orders_and_tree():
    app.sj_api = _FakeApi()
    app.my_orders.clear()
    for iid in app.tree_orders.get_children(): app.tree_orders.delete(iid)
    ctx = dict(
        contract=object(), order=stock_app_pro.sj.Order(price=98.15, quantity=1),
        action="買進", raw_sym="0050", mode="Common",
        mode_labels={"Common": "整股"}, cond_labels={"Cash": "現股"},
        effective_cond="Cash", effective_tif="ROD",
        is_lot_restricted=False, use_daytrade=False,
        qty=1, qty_unit="張", price_disp="98.15", order_type_str="限價",
    )
    place_and_settle(ctx)
    assert len(app.my_orders) == 1, f"my_orders 應有1筆,實際 {len(app.my_orders)}"
    key = next(iter(app.my_orders))
    assert key.startswith("_pending_"), f"應該用 _pending_ 暫時key,實際 {key}"
    rows = app.tree_orders.get_children()
    assert len(rows) == 1, f"tree_orders 應有1列,實際 {len(rows)} 列 => 這就是使用者看到的空清單!"
    vals = app.tree_orders.item(rows[0], "values")
    # 【第九輪】欄位加入「交易別」(index 2),買賣位移到 [3]
    assert vals[1] == "0050" and vals[2] == "整股" and vals[3] == "買進", f"列內容不對: {vals}"

def _order_seed_prints_explicit_success_log():
    app.sj_api = _FakeApi()
    app.my_orders.clear()
    app._log_capture = []
    _orig_log = app.log_message
    def _cap(m):
        app._log_capture.append(m); _orig_log(m)
    app.log_message = _cap
    try:
        ctx = dict(
            contract=object(), order=stock_app_pro.sj.Order(price=98.15, quantity=1),
            action="買進", raw_sym="0050", mode="Common",
            mode_labels={"Common": "整股"}, cond_labels={"Cash": "現股"},
            effective_cond="Cash", effective_tif="ROD",
            is_lot_restricted=False, use_daytrade=False,
            qty=1, qty_unit="張", price_disp="98.15", order_type_str="限價",
        )
        place_and_settle(ctx)
    finally:
        app.log_message = _orig_log
    assert any("已加入清單" in m for m in app._log_capture), \
        f"下單後應印出明確的『已加入清單』日誌,實際日誌: {app._log_capture}"

def _decimal_realtime_normalizes_fullwidth():
    # 全形句號即時轉半形
    app.entry_price.delete(0, "end")
    app.entry_price.insert(0, "98。15")
    app._normalize_decimal_realtime()
    assert app.entry_price.get() == "98.15", f"全形句號應轉半形,實際 {app.entry_price.get()}"

def _order_event_replaces_pending_without_dup_or_wipe():
    """【第六輪】shioaji 官方格式委託回報:暫時項目要被正式替換 (不重複),
    且即使 exchange_ts 型別怪異,清單也絕不能被清空。"""
    def _fresh_seed():
        app.my_orders.clear()
        for i in app.tree_orders.get_children(): app.tree_orders.delete(i)
        app._last_pending_order_key = None; app._last_pending_order_info = None
        ctx = dict(contract=object(), order=stock_app_pro.sj.Order(price=106.45, quantity=10),
            action="買進", raw_sym="0050", mode="IntradayOdd",
            mode_labels={"IntradayOdd": "盤中零股"}, cond_labels={"Cash": "現股"},
            effective_cond="Cash", effective_tif="ROD", is_lot_restricted=True,
            use_daytrade=False, qty=10, qty_unit="股", price_disp="106.45", order_type_str="限價")
        place_and_settle(ctx)
    def _msg(ts):
        return {'operation': {'op_type': 'New', 'op_code': '00', 'op_msg': ''},
            'order': {'id': 'c21b876d', 'action': 'Buy', 'price': 106.45, 'quantity': 10,
                      'order_cond': 'Cash', 'order_lot': 'IntradayOdd', 'order_type': 'ROD'},
            'status': {'id': 'c21b876d', 'exchange_ts': ts, 'modified_price': 0, 'cancel_quantity': 0},
            'contract': {'code': '0050'}}
    app.sj_api = _FakeApi()
    # 情境A:官方 float ts -> 恰好 1 列、中文買進、正式 id
    _fresh_seed()
    app.on_order_deal_callback("OrderState.StockOrder", _msg(1783908420.4734)); app.flush_after()
    rows = [app.tree_orders.item(i, "values") for i in app.tree_orders.get_children()]
    assert len(rows) == 1, f"應恰好1列(不重複不消失),實際 {len(rows)}"
    # 【第九輪】欄位加入「交易別」(index 2),action 位移到 [3]、狀態到 [7]
    assert rows[0][2] == '盤中零股' and rows[0][3] == '買進' and rows[0][7] == '已委託', f"顯示錯誤: {rows[0]}"
    assert list(app.my_orders.keys()) == ['c21b876d'], f"暫時key應被替換: {list(app.my_orders.keys())}"
    # 情境B:字串 ts (原本會把清單清空) -> 清單必須仍有列
    _fresh_seed()
    app.on_order_deal_callback("OrderState.StockOrder", _msg("2026-07-13 10:07:00.123")); app.flush_after()
    assert len(app.tree_orders.get_children()) >= 1, "清單被清空 (先刪後插的舊病復發)"

run_case("問題1(修正後): 版面數值改變後面板位置真的重新定位 (set_position 生效)", _layout_change_now_repositions_panels)
run_case("問題1(修正後): 即時預覽 _apply_chart_margins 立即搬動面板", _layout_live_preview_applies_instantly)
run_case("問題2: PendingSubmit 假單 -> my_orders 與 tree_orders 內容", _order_appears_in_my_orders_and_tree)
run_case("問題2(修正後): 下單後印出明確『已加入清單』成功日誌", _order_seed_prints_explicit_success_log)
run_case("問題3(修正後): 價格欄位全形句號即時轉半形小數點", _decimal_realtime_normalizes_fullwidth)
def _order_modification_calls_correct_shioaji_api():
    """【ADR-023】刪改端到端:雙擊解析 + _send_order_modification 呼叫對的 shioaji API。"""
    class RecTrade:
        pass
    class RecApi:
        def __init__(self): self.calls = []
        def place_order(self, c, o):
            t = type('T', (), {'order': type('O', (), {'id': 'ord123'})(),
                               'status': type('S', (), {'status': type('SS', (), {'name': 'PendingSubmit'})(), 'msg': 'ok'})()})()
            return t
        def cancel_order(self, trade): self.calls.append(('cancel', trade))
        def update_order(self, trade, price=None, qty=None): self.calls.append(('update', price, qty))
    api = RecApi()
    app.sj_api = api; app.api_logged_in = True
    # 手動放一筆整股委託 (帶 trade 物件)
    tr = RecTrade()
    app.my_orders.clear()
    app.my_orders['ord123'] = {'id': 'ord123', 'code': '2330', 'action': '買進', 'price': 1000.0,
                               'quantity': 10, 'filled_quantity': 3, 'order_cond': 'Cash',
                               'order_lot': 'Common', 'status_display': '部分成交',
                               'ts': 1.0, 'time_str': '10:00:00', 'trade': tr}
    # 改量 10 -> 7 (應呼叫 update_order(trade, qty=3),即減 3)
    app._send_order_modification(app.my_orders['ord123'], 'qty', 7)
    assert ('update', None, 3) in api.calls, f"改量應傳減量 qty=3,實際 {api.calls}"
    # 改價 -> 1005 (應呼叫 update_order(trade, price=1005.0))
    app._send_order_modification(app.my_orders['ord123'], 'price', 1005.0)
    assert ('update', 1005.0, None) in api.calls, f"改價應傳 price=1005.0,實際 {api.calls}"
    # 刪單 (應呼叫 cancel_order(trade))
    app._send_order_modification(app.my_orders['ord123'], 'cancel', None)
    assert any(c[0] == 'cancel' and c[1] is tr for c in api.calls), f"刪單應呼叫 cancel_order(該trade),實際 {api.calls}"

def _order_modification_blocks_illegal():
    """零股不可改價;改量不可增量 — 規則層要擋。"""
    from core import order_rules as _or
    ok, _ = _or.validate_price_change('IntradayOdd', 100, 101)
    assert not ok, "零股改價應被擋"
    ok, _ = _or.validate_qty_change('Common', '已委託', 10, 0, 15)
    assert not ok, "增量應被擋"

run_case("第六輪: 委託回報正確替換暫時項目(不重複)且清單絕不清空", _order_event_replaces_pending_without_dup_or_wipe)
def _perf_cache_progressive_and_seq_guard():
    """【ADR-024】效能三保證:快取秒開不重下載、TXF首載兩段式、過期序號不蓋圖。"""
    import numpy as _np
    import pandas as _pd
    def make_kbars(start_str, end_str, per_day=30):
        start=_pd.Timestamp(start_str); end=_pd.Timestamp(end_str); idx=[]; d=start
        while d<=end:
            if d.weekday()<5: idx.extend(_pd.date_range(d+_pd.Timedelta(hours=9),periods=per_day,freq='min'))
            d+=_pd.Timedelta(days=1)
        n=len(idx); base=_np.linspace(100,110,n) if n else []
        return {'ts':list(idx),'open':list(base),'high':[b+.5 for b in base],
                'low':[b-.5 for b in base],'close':[b+.1 for b in base],
                'volume':[100]*n,'amount':[b*100 for b in base]}
    class FC:
        def __init__(s,code,symbol=None,name=""):
            s.code=code; s.symbol=symbol or code; s.name=name; s.day_trade='Yes'; s.reference=100.0
    class FQ:
        """【ADR-099】記錄每次訂閱是從哪條路徑來的。

        自選股報價 worker (watchlist_quote_worker) 是獨立的背景 daemon
        thread,會在測試執行期間自行訂閱期貨/指數 (ADR-042)。舊版斷言直接比
        全域 sub 計數,會把這條無關路徑的訂閱算進來,誤判成「主圖換週期重訂閱」。
        這裡改為可依來源過濾,讓斷言只針對主圖 (fetch_data_worker) 路徑。"""
        def __init__(s): s.sub=0; s.unsub=0; s.subsrc=[]
        def subscribe(s,c,**k):
            s.sub+=1
            import traceback as _tb
            s.subsrc.append(''.join(_tb.format_stack()))
        def unsubscribe(s,c,**k): s.unsub+=1
        def chart_subs(s):
            """只數主圖路徑的訂閱 (排除自選股 worker)。"""
            return sum(1 for t in s.subsrc if 'watchlist_quote_worker' not in t)
    class FS:
        def __init__(s): s.m={'0050':FC('0050')}
        def get(s,k): return s.m.get(k)
        def __iter__(s): return iter(s.m.values())
    class FG:
        def __init__(s,c): s.m={f'{c}R1':FC(c,symbol=f'{c}R1')}
        def get(s,k): return s.m.get(k)
    class FApi:
        def __init__(s):
            s.quote=FQ(); s.calls=[]; s.call_src=[]
            class C: pass
            s.Contracts=C(); s.Contracts.Stocks=FS()
            class F: pass
            s.Contracts.Futures=F(); s.Contracts.Futures.TXF=FG('TXF')
            class IT: pass
            class TSE: TSE001=FC('001',symbol='TSE001')
            s.Contracts.Indexs=IT(); s.Contracts.Indexs.TSE=TSE
        def kbars(s,c,start=None,end=None):
            # 【ADR-100】記錄呼叫來源:背景 daemon thread (主圖自動更新/報價
            # worker) 也會呼叫 kbars,把它們算進「下載次數」會讓斷言隨執行緒
            # 時序時而 PASS 時而 FAIL。chart_calls() 只數手動查詢路徑。
            import traceback as _tb
            s.calls.append((getattr(c,'code','?'),start))
            s.call_src.append(''.join(_tb.format_stack()))
            return make_kbars(start,end)
        def chart_calls(s):
            """只數 fetch_data_worker (手動查詢) 觸發的下載。"""
            return [c for c,t in zip(s.calls,s.call_src) if 'fetch_data_worker' in t]
    api=FApi(); app.sj_api=api; app.api_logged_in=True
    app._kbars_raw_cache.clear(); app.current_contract=None; app._last_fetch_raw_sym=None
    draws={'n':0}; orig=app.draw_chart
    app.draw_chart=lambda df:(draws.__setitem__('n',draws['n']+1), orig(df))[1]
    try:
        # 首載 0050 日K → 搶先出圖 1 段 + 分段補全 (ADR-046/047/048
        # _download_kbars_chunked,chunk_days=90);換 5分K → 快取秒開、不重訂閱。
        # 【ADR-099】本斷言原本寫死「首載只下載1次」,那是分段下載功能加入之前的
        # 預期;分段補全是刻意的優化 (P-36:kbars 只回分K,長週期一次抓太多天會慢
        # 到不行),所以這裡改驗「有下載且每段起點不重複」,而不是寫死次數。
        app._fetch_seq=1; app.fetch_data_worker('0050','日K',1); app.flush_after()
        assert len(api.chart_calls())>=1, f"首載應至少下載1次,實際{len(api.chart_calls())}"
        starts=[c[1] for c in api.chart_calls()]
        assert len(starts)==len(set(starts)), f"分段下載不應重複抓同一起點: {starts}"
        n_first=len(api.chart_calls())
        subs=api.quote.chart_subs()
        app._fetch_seq=2; app.fetch_data_worker('0050','5分K',2); app.flush_after()
        assert len(api.chart_calls())==n_first, \
            f"快取涵蓋時不應重新下載 (手動查詢路徑 {n_first}→{len(api.chart_calls())})"
        assert api.quote.chart_subs()==subs and api.quote.unsub==0, \
            f"同商品換週期不應重訂閱/退訂 (主圖訂閱 {subs}→{api.quote.chart_subs()}, 退訂 {api.quote.unsub})"
        # TXF 日K 首載 → 兩段式 (2次下載,2次出圖)
        k0=len(api.chart_calls()); d0=draws['n']
        app._fetch_seq=3; app.fetch_data_worker('TXF','日K',3); app.flush_after()
        assert len(api.chart_calls())-k0==2, f"期貨首載應兩段式下載2次,實際{len(api.chart_calls())-k0}"
        assert draws['n']-d0==2, f"應出圖2次(搶先+補全),實際{draws['n']-d0}"
        # 過期序號不可蓋圖
        d0=draws['n']; app._fetch_seq=99
        app.fetch_data_worker('0050','日K',5); app.flush_after()
        assert draws['n']==d0, "過期查詢竟然出圖 (序號防護失效)"
    finally:
        app.draw_chart=orig

run_case("ADR-023: 刪改呼叫正確的 shioaji API (改量傳減量/改價傳新價/刪單)", _order_modification_calls_correct_shioaji_api)
run_case("ADR-023: 非法刪改被規則層擋下 (零股改價/增量)", _order_modification_blocks_illegal)
def _hover_blitting_and_pan_throttle():
    """【ADR-025】hover 卡頓修正:animated 物件 + 真實 Agg blit + 換K棒 gating + 平移節流。"""
    import time as _t
    import numpy as _np
    import pandas as _pd
    app.current_symbol = "0050"; app.current_stock_name = "T"; app.asset_type = "stock"
    idx = _pd.date_range("2026-01-01", periods=100, freq="D")
    base = _np.linspace(90, 110, 100)
    df = _pd.DataFrame({"Open": base, "High": base+1, "Low": base-1, "Close": base+.2,
                        "Volume": [1000]*100}, index=idx)
    app.current_df = df; app.var_macd.set(True)
    app.draw_chart(df)
    # 1) hover 物件必須 animated (否則會被烙進底圖,blit 疊加會出現殘影)
    assert all(l.get_animated() for l in app.vlines), "十字線未設 animated"
    all_txt = list(app.txt_main_segments) + [s for ss in app.sub_texts.values() for s in ss]
    assert all_txt and all(s['obj'].get_animated() for s in all_txt), "hover 文字未設 animated"
    # 2) 真實 Agg canvas 上底圖快取 + blit 成功
    real_canvas = app.current_fig.canvas
    app.current_canvas = real_canvas; real_canvas.draw(); app._on_canvas_draw()
    assert app._hover_bg is not None, "底圖未快取"
    for l in app.vlines: l.set_xdata([50, 50]); l.set_visible(True)
    assert app._blit_hover() is True, "真實 Agg canvas 上 blit 應成功"
    # 3) 換K棒 gating:同K棒內微移不觸發任何更新
    class Ev:
        def __init__(s, xdata, inaxes, canvas): s.xdata=xdata; s.inaxes=inaxes; s.canvas=canvas; s.x=0; s.button=None
    ax0 = app.axlist[0]; app.last_hover_idx = -1
    n = {'c': 0}; orig_cfg = app.lbl_hover_info.config
    app.lbl_hover_info.config = lambda **k: (n.__setitem__('c', n['c']+1), orig_cfg(**k))[1]
    try:
        app.on_mouse_move(Ev(30.2, ax0, real_canvas))
        app.on_mouse_move(Ev(30.4, ax0, real_canvas))
        assert n['c'] == 1, f"同K棒內移動不應重組資訊列,實際更新 {n['c']} 次"
    finally:
        app.lbl_hover_info.config = orig_cfg
    # 4) 平移節流:連續 20 個像素事件只重繪 1 次
    d = {'n': 0}
    class CC:
        def draw_idle(s): d['n'] += 1
    app.is_panning=True; app.press_x_pixel=100; app.pan_axes=ax0
    app.press_xlim=ax0.get_xlim(); app._last_pan_draw=0.0
    for px in range(101, 121):
        e = Ev(50, ax0, CC()); e.x = px
        app.on_mouse_move(e)
    assert d['n'] == 1, f"20個連續平移事件應只重繪1次,實際 {d['n']}"
    app.is_panning=False; app.press_x_pixel=None

run_case("ADR-024: 快取秒開/同商品不重訂閱/期貨兩段式/序號防race", _perf_cache_progressive_and_seq_guard)
def _relogin_builds_fresh_session():
    """【ADR-026】重登死循環修正:誤判收斂 + 斷線釋放舊連線 + 重登建全新物件。"""
    import time as _t
    # 誤判收斂
    assert app._looks_like_session_dead(Exception("SessionNotEstablished")), "真斷線要能判斷"
    assert not app._looks_like_session_dead(Exception("contracts not ready")), "暫時性錯誤不可誤判斷線"
    assert not app._looks_like_session_dead(Exception("http session pool timeout")), "泛用 session 字樣不可誤判"
    # 斷線時釋放舊連線
    class OldApi:
        def __init__(s): s.logged_out=False
        def logout(s): s.logged_out=True
    old = OldApi(); app.sj_api = old; app.api_logged_in = True
    app._mark_session_dead(); _t.sleep(0.1); app.flush_after()
    assert app.api_logged_in is False and old.logged_out, "斷線應撥回False並釋放舊連線"
    # 重新登入:舊物件 logout + 建全新物件
    created = []
    class FreshApi:
        def __init__(s, simulation=False):
            created.append(s)
            class Q:
                def set_on_tick_stk_v1_callback(s2,f): pass
                def set_on_bidask_stk_v1_callback(s2,f): pass
                def set_on_tick_fop_v1_callback(s2,f): pass
                def set_on_bidask_fop_v1_callback(s2,f): pass
            s.quote=Q()
        def login(s, **k): pass
        def activate_ca(s, **k): pass
        def set_order_callback(s, f): pass
        def logout(s): pass
    zombie = OldApi(); app.sj_api = zombie
    # 【第十二輪修正】此案例模擬「曾經登入成功、現在要重新登入」情境——這種
    # 情況才需要 logout 舊連線;若是從未登入過的物件,ADR-032 規定不呼叫
    # logout (見 _round12_login_freeze_mitigation),兩案例分工驗證不同前提。
    app.api_logged_in = True; app.current_contract = object()
    orig_shioaji = getattr(stock_app_pro.sj, 'Shioaji', None)
    stock_app_pro.sj.Shioaji = FreshApi
    try:
        app.process_broker_login("k","s","A123456789","ca.pfx","pw"); app.flush_after()
        assert zombie.logged_out, "重登前應先釋放舊殭屍連線"
        assert len(created)==1 and app.sj_api is created[0], "重登應建立全新 Shioaji 物件"
        assert app.current_contract is None, "舊 contract 應作廢"
        assert app.api_logged_in is True, "重登應成功"
    finally:
        if orig_shioaji is not None:
            stock_app_pro.sj.Shioaji = orig_shioaji
        app.api_logged_in = False

run_case("ADR-025: hover blit毫秒級/換K棒gating/平移節流", _hover_blitting_and_pan_throttle)
def _modify_entry_points_and_race_guard():
    """【ADR-027】刪改可見入口 (按鈕+雙擊) + 回報先到race防重複。"""
    import time as _t
    class S:
        class status: name='PendingSubmit'
        msg='ok'
    class O: id=''
    class T: order=O(); status=S()
    class Api:
        def place_order(s,c,o): return T()
    app.sj_api=Api(); app.api_logged_in=True
    app.asset_type="stock"; app.current_symbol="0050"
    # 按鈕/雙擊入口
    app.my_orders.clear()
    for i in app.tree_orders.get_children(): app.tree_orders.delete(i)
    app.my_orders['ord1']={'id':'ord1','code':'0050','action':'買進','price':102.0,
        'quantity':50,'filled_quantity':0,'order_cond':'Cash','order_lot':'Common',
        'status_display':'已委託','ts':_t.time(),'time_str':'12:20:31','trade':object()}
    app._refresh_my_orders_ui()
    opened={'n':0}; orig=app._open_order_modify_dialog
    app._open_order_modify_dialog=lambda o: opened.__setitem__('n',opened['n']+1)
    try:
        app.tree_orders._focus=None; app._on_modify_button_click()
        assert opened['n']==0, "未選取不應開啟"
        app.tree_orders._focus='ord1'; app._on_modify_button_click()
        assert opened['n']==1, "按鈕選取後應開啟"
        class Ev: y=10
        app._on_order_row_double_click(Ev())
        assert opened['n']==2, "雙擊應開啟"
    finally:
        app._open_order_modify_dialog=orig
    # 回報先到 race:先有正式項目,seed 不建重複
    app.my_orders.clear()
    for i in app.tree_orders.get_children(): app.tree_orders.delete(i)
    app._last_pending_order_key=None; app._last_pending_order_info=None
    app.my_orders['real99']={'id':'real99','code':'0050','action':'買進','price':102.0,
        'quantity':50,'filled_quantity':0,'order_lot':'Common','status_display':'已委託',
        'ts':_t.time(),'time_str':'12:20:31'}
    ctx=dict(contract=object(), order=stock_app_pro.sj.Order(price=102.0,quantity=50),
        action="買進", raw_sym="0050", mode="Common",
        mode_labels={"Common":"整股"}, cond_labels={"Cash":"現股"},
        effective_cond="Cash", effective_tif="ROD", is_lot_restricted=False,
        use_daytrade=False, qty=50, qty_unit="股", price_disp="102.0", order_type_str="限價")
    place_and_settle(ctx)
    assert len(app.my_orders)==1, f"回報先到時不應重複,實際{len(app.my_orders)}筆"
    assert not any(str(k).startswith('_pending_') for k in app.my_orders), "不應建暫時項目"

run_case("ADR-026: 斷線誤判收斂/釋放舊連線/重登建全新物件", _relogin_builds_fresh_session)
def _market_selector_and_watchlist_quotes():
    """【ADR-028】市場切換 (台股/台期貨/美股) + 自選股即時報價。"""
    import numpy as _np
    import pandas as _pd
    def _mk(start,end,per_day=30):
        s=_pd.Timestamp(start); e=_pd.Timestamp(end); idx=[]; d=s
        while d<=e:
            if d.weekday()<5: idx.extend(_pd.date_range(d+_pd.Timedelta(hours=9),periods=per_day,freq='min'))
            d+=_pd.Timedelta(days=1)
        n=len(idx); b=_np.linspace(100,110,n) if n else []
        return {'ts':list(idx),'open':list(b),'high':[x+.5 for x in b],'low':[x-.5 for x in b],
                'close':[x+.1 for x in b],'volume':[100]*n,'amount':[x*100 for x in b]}
    class FC:
        def __init__(s,code,symbol=None,name="",category=""):
            s.code=code; s.symbol=symbol or code; s.name=name; s.category=category or code
            s.day_trade='Yes'; s.reference=100.0
    class FGrp:
        def __init__(s,code,name): s._m={f'{code}R1':FC(code,symbol=f'{code}R1',name=name,category=code)}
        def get(s,k): return s._m.get(k)
        def __iter__(s): return iter(s._m.values())
    class FFut:
        def __init__(s):
            s.TXF=FGrp('TXF','臺股期貨'); s.MXF=FGrp('MXF','小型臺指'); s.ZEF=FGrp('ZEF','台積電期貨')
        def __iter__(s): return iter([s.TXF,s.MXF,s.ZEF])
    class FStk:
        def __init__(s): s._m={'0050':FC('0050',name='元大台灣50')}
        def get(s,k): return s._m.get(k)
        def __iter__(s): return iter(s._m.values())
    class Snap:
        def __init__(s,c,g,r): s.close=c; s.change_price=g; s.change_rate=r
    class FApi:
        def __init__(s):
            class Q:
                def subscribe(s2,*a,**k): pass
                def unsubscribe(s2,*a,**k): pass
            s.quote=Q(); s.snap_calls=[]
            class C: pass
            s.Contracts=C(); s.Contracts.Stocks=FStk(); s.Contracts.Futures=FFut()
            class IT: pass
            class TSE: TSE001=FC('001',symbol='TSE001',name='加權指數')
            class OTC: OTC101=FC('101',symbol='OTC101',name='櫃買指數')
            s.Contracts.Indexs=IT(); s.Contracts.Indexs.TSE=TSE; s.Contracts.Indexs.OTC=OTC
        def kbars(s,c,start=None,end=None): return _mk(start,end)
        def snapshots(s,cs):
            s.snap_calls.append(len(cs))

            return [Snap(106.65,0.90,0.85) if getattr(c,'code','')=='0050' else Snap(23150.0,120.0,0.52) for c in cs]
    api=FApi(); app.sj_api=api; app.api_logged_in=True
    app._kbars_raw_cache.clear(); app._wl_contract_cache.clear(); app.current_contract=None
    # 台期貨模式解析任意期貨代號
    app._fetch_seq=101; app.fetch_data_worker('ZEF','日K',101,market='台期貨'); app.flush_after()
    assert app.asset_type=='future' and app.current_symbol=='ZEFR1', f"ZEF 應解析為期貨,實際 {app.asset_type}/{app.current_symbol}"
    # 台期貨模式查無 → 列候選
    logs=[]; ol=app.log_message; app.log_message=lambda m:(logs.append(m), ol(m))[0]
    try:
        app._fetch_seq=102; app.fetch_data_worker('XXX','日K',102,market='台期貨'); app.flush_after()
    finally:
        app.log_message=ol
    assert any('期貨代號查詢' in m and 'TXF' in m for m in logs), "查無代號應列出候選"
    # 美股模式:TXF 走 yfinance
    app._fetch_seq=103; app.fetch_data_worker('TXF','日K',103,market='美股'); app.flush_after()
    assert app.data_source=='yfinance', "美股模式應走 yfinance"
    # 自選股報價:批次 snapshot、值與顏色正確、更新不重建
    app.watchlists={'T':['0050','TXF']}; app.current_wl_name.set('T')
    app.on_wl_change(); app.flush_after()
    # 【ADR-100】只檢查「這次呼叫」新增的批次,不看 snap_calls[-1]。
    # fetch_realtime_worker 是背景 daemon thread,會不定時自己 snapshots(1檔),
    # 用「最後一筆」斷言會隨執行緒時序時而 PASS 時而 FAIL (實測 [1,2,1])。
    _n0 = len(api.snap_calls)
    app._wl_fetch_quotes_once(); app.flush_after()
    _mine = api.snap_calls[_n0:]
    assert 2 in _mine, f"應一次批次抓 2 檔,本次呼叫實際批次: {_mine}"
    rows={i: app.tree_wl.item(i,'values') for i in app.tree_wl.get_children()}
    # 【第九輪】自選股加「名稱」欄 (index 1),報價位移到 [2:]
    assert rows['0050'][2:]==('106.65','+0.90','+0.85%'), f"0050 顯示錯誤: {rows['0050']}"
    assert rows['TXF'][2]=='23,150', f"TXF 顯示錯誤: {rows['TXF']}"

run_case("ADR-027: 刪改可見入口(按鈕+雙擊) + 回報先到防重複", _modify_entry_points_and_race_guard)
def _round9_six_items():
    """【ADR-029 第九輪】布林雙組/期貨tick前綴比對/自選股名稱欄/中文搜尋/完整合約代號。"""
    import numpy as _np
    import pandas as _pd
    class FC:
        def __init__(s,code,symbol=None,name="",month=""):
            s.code=code; s.symbol=symbol or code; s.name=name; s.category=code[:3]
            s.delivery_month=month; s.day_trade='Yes'; s.reference=100.0
    class FGrp:
        def __init__(s,code,name):
            s._m={f'{code}R1':FC(f'{code}R1',name=name),
                  f'{code}202609':FC(f'{code}202609',name=name,month='202609')}
        def get(s,k): return s._m.get(k)
        def __iter__(s): return iter(s._m.values())
    class FFut:
        def __init__(s): s.TXF=FGrp('TXF','臺股期貨'); s.CDF=FGrp('CDF','台積電期貨')
        def __iter__(s): return iter([s.TXF,s.CDF])
    class FStk:
        def __init__(s): s._m={'2330':FC('2330',name='台積電'),'0050':FC('0050',name='元大台灣50')}
        def get(s,k): return s._m.get(k)
        def __iter__(s): return iter(s._m.values())
    class FApi:
        def __init__(s):
            class Q:
                def subscribe(s2,*a,**k): pass
                def unsubscribe(s2,*a,**k): pass
            s.quote=Q()
            class C: pass
            s.Contracts=C(); s.Contracts.Stocks=FStk(); s.Contracts.Futures=FFut()
            class IT: pass
            class TSE: TSE001=FC('001',symbol='TSE001',name='加權指數')
            class OTC: OTC101=FC('101',symbol='OTC101',name='櫃買指數')
            s.Contracts.Indexs=IT(); s.Contracts.Indexs.TSE=TSE; s.Contracts.Indexs.OTC=OTC
        def snapshots(s,cs): 
            class Snap:
                close=106.65; change_price=0.90; change_rate=0.85
            return [Snap() for _ in cs]
    app.sj_api=FApi(); app.api_logged_in=True
    # 布林雙組
    idx=_pd.date_range("2026-01-01",periods=60,freq="D"); b=_np.linspace(100,110,60)
    df=_pd.DataFrame({"Open":b,"High":b+1,"Low":b-1,"Close":b+.2,"Volume":[1]*60},index=idx)
    app.bb_show.set(True); app.bb_period.set(10); app.bb_std1.set(1.5); app.bb_std2.set(2.5)
    r=app.calculate_custom_indicators(df)
    assert 'BB_UPPER2' in r.columns and r['BB_MID'].first_valid_index()==idx[9], "布林自訂/雙組失效"
    app.bb_show.set(False)
    # 期貨 tick 前綴比對 (第4項根因)
    app.current_contract=FC('TXFR1',name='臺股期貨')
    class Tick:
        def __init__(s,code): s.code=code; s.close=23150.0; s.volume=1
    with app.quote_lock: app.current_tick_normal=None
    app.on_tick_fop_v1(None, Tick('TXFG6'))
    with app.quote_lock: got=app.current_tick_normal
    assert got is not None, "R1 訂閱的月份合約 tick 被丟棄 (報價慢根因復發)"
    assert not app._fop_code_match('MXFG6','TXFR1'), "不同商品不可誤收"
    # 自選股名稱欄
    app.watchlists={'T':['0050','TXF']}; app.current_wl_name.set('T')
    app._wl_contract_cache.clear(); app.on_wl_change(); app.flush_after()
    rows={i: app.tree_wl.item(i,'values') for i in app.tree_wl.get_children()}
    assert rows['0050'][1]=='元大台灣50' and rows['TXF'][1]=='臺股期貨', f"名稱欄錯誤: {rows}"
    # 中文搜尋 + 完整合約代號
    res=app._search_contracts_by_keyword('台積電')
    assert any(m=='台股' and c=='2330' for m,ls,c,n,e in res), "中文搜尋漏了股票"
    assert any(m=='台期貨' and c=='CDF202609' for m,ls,c,n,e in res), "中文搜尋漏了期貨月份合約"
    c=app._resolve_futures_contract('TXF202609')
    assert c is not None and c.code=='TXF202609', "完整合約代號解析失敗"

run_case("ADR-028: 市場切換(台股/台期貨/美股) + 自選股即時報價", _market_selector_and_watchlist_quotes)
def _round10_stale_module_and_close_hang():
    """【ADR-030】舊版 core 模組降級不掛圖 + logout 卡死也能限時關閉。"""
    import time as _t
    import os as _os
    import numpy as _np
    import pandas as _pd
    idx=_pd.date_range("2026-01-01",periods=60,freq="D"); b=_np.linspace(100,110,60)
    df=_pd.DataFrame({"Open":b,"High":b+1,"Low":b-1,"Close":b+.2,"Volume":[1]*60},index=idx)
    # 舊版簽名降級
    def old_sig(df, ma_flags, ma_types, ma_periods, bb_show, bbw_show,
                macd_show, macd_f, macd_s, macd_sig, rsi_show, rsi_p,
                kdj_show, kd_n, kd_m1, kd_m2, dmi_show, dmi_n):
        out=df.copy(); out['BB_MID']=out['Close'].rolling(20).mean(); return out
    orig=stock_app_pro.core_indicators.calculate_indicators
    stock_app_pro.core_indicators.calculate_indicators=old_sig
    app.bb_show.set(True); app._bb_param_warned=False
    try:
        r=app.calculate_custom_indicators(df)
        assert r is not None and 'BB_MID' in r.columns, "舊版 core 應降級成功而非掛圖"
    finally:
        stock_app_pro.core_indicators.calculate_indicators=orig
        app.bb_show.set(False)
    # logout 卡死限時關閉
    class HangApi:
        def logout(s): _t.sleep(60)
    app.sj_api=HangApi(); app.api_logged_in=True
    _oe=_os._exit; _od=app.destroy
    calls=[]
    _os._exit=lambda c: calls.append(('exit',c))
    app.destroy=lambda: calls.append(('destroy',))
    try:
        t0=_t.monotonic(); app.on_app_close(); el=_t.monotonic()-t0
    finally:
        _os._exit=_oe; app.destroy=_od
        app._closing=False; app.api_logged_in=False  # 還原,避免影響後續案例
    assert el < 3.5, f"logout 卡死時關閉應 3 秒內完成,實際 {el:.1f}s"
    assert ('exit',0) in calls, "os._exit 保底未執行"
    # 五檔 pack 優先權 (程式碼層)
    s=open('stock_app_pro.py',encoding='utf-8').read()
    assert s.index("five_level_frame.pack(side=tk.BOTTOM") < s.index("self.listbox_trade_feed.pack"), \
        "五檔必須先 pack 且 side=BOTTOM,否則面板變高時會被擠出視窗"

run_case("ADR-029: 布林雙組/期貨tick前綴/自選股名稱/中文搜尋/完整代號", _round9_six_items)
def _round11_positions_and_symbol_routing():
    """【ADR-031】hover漲跌點數/我的庫存/TXFR2含數字誤判/美股自選股報價。"""
    import time as _t
    # hover 資訊列含漲跌點數 (程式碼層:字串樣板)
    s=open('stock_app_pro.py',encoding='utf-8').read()
    assert 'abs(chg_val)' in s and '({chg_sign} {abs(chg_pct):.2f}%)' in s, "hover 缺漲跌點數"
    # 期貨完整代號樣式判斷 (含數字不可誤判台股)
    assert app._looks_like_futures_symbol('TXFR2') and app._looks_like_futures_symbol('CDF202607'), "期貨完整代號樣式失效"
    assert not app._looks_like_futures_symbol('2330') and not app._looks_like_futures_symbol('SPYM'), "誤判非期貨"
    # 我的庫存:查詢/畫面/明細
    class Pos:
        def __init__(s2): 
            s2.id=0; s2.code='0050'; s2.direction='Buy'; s2.quantity=5
            s2.price=100.0; s2.last_price=106.0; s2.pnl=30000.0; s2.yd_quantity=5
    class FApi:
        def __init__(s2):
            s2.stock_account=object(); s2.futopt_account=None
            class C: pass
            s2.Contracts=C()
            class FStk:
                def get(s3,k): return None
            s2.Contracts.Stocks=FStk()
        def list_positions(s2,a): return [Pos()]
    app.sj_api=FApi(); app.api_logged_in=True
    app._positions_loading=False
    rows,raws=app._positions_fetch_once()
    assert len(rows)==1 and rows[0]['pnl']==30000.0, "庫存查詢失敗"
    app._apply_positions(rows,raws); app.flush_after()
    assert len(app.tree_positions.get_children())==1, "庫存表格未填"
    vals=app.tree_positions.item(app.tree_positions.get_children()[0],'values')
    # 【ADR-057】金額一律無條件捨去小數 (使用者需求 #2);% 保留小數。
    # (ADR-056 曾誤把需求讀成「要保留小數」而改成 .2f,ADR-057 已更正)
    assert vals[7]=='+30,000' and vals[8]=='+6.00%', f"庫存損益/報酬率錯誤: {vals}"
    assert any('yd_quantity' in d for d in app._positions_raw), "明細原始欄位不完整"
    app._open_positions_detail_window()  # 不拋例外即可
    # 美股自選股報價 (yfinance)
    class FI: last_price=88.18; previous_close=87.18
    class FT:
        fast_info=FI(); info={'shortName':'SPDR Portfolio'}
    orig_tk=stock_app_pro.yf.Ticker
    stock_app_pro.yf.Ticker=lambda s2: FT()
    try:
        app._wl_us_names={}
        app.watchlists={'美股':['SPYM']}; app.current_wl_name.set('美股')
        app.on_wl_change(); app.flush_after()
        app._wl_us_cycle=99
        app._wl_fetch_quotes_once(); app.flush_after()
        r=app.tree_wl.item('SPYM','values')
        assert r[2]=='88.18' and r[4]=='+1.15%', f"美股報價錯誤: {r}"
        assert r[1].startswith('SPDR'), f"美股名稱錯誤: {r}"
    finally:
        stock_app_pro.yf.Ticker=orig_tk
    app.api_logged_in=False

run_case("ADR-030: 舊版core降級不掛圖/關閉不卡死/五檔pack優先", _round10_stale_module_and_close_hang)
def _round12_login_freeze_mitigation():
    """【ADR-032】登入凍結三修正:virgin物件不logout/防重複點擊/watchdog提示/按鈕復原。"""
    import time as _t
    class FreshApi:
        created=[]
        def __init__(s, simulation=False):
            FreshApi.created.append(s); s.logout_called=False
            class Q:
                def set_on_tick_stk_v1_callback(s2,f): pass
                def set_on_bidask_stk_v1_callback(s2,f): pass
                def set_on_tick_fop_v1_callback(s2,f): pass
                def set_on_bidask_fop_v1_callback(s2,f): pass
            s.quote=Q()
        def login(s, **k): pass
        def activate_ca(s, **k): pass
        def set_order_callback(s, f): pass
        def logout(s): s.logout_called=True
    orig_shioaji = getattr(stock_app_pro.sj, 'Shioaji', None)
    try:
        # virgin 物件不呼叫 logout
        class Virgin:
            def __init__(s): s.logout_called=False
            def logout(s): s.logout_called=True
        v=Virgin(); app.sj_api=v; app.api_logged_in=False
        FreshApi.created=[]; stock_app_pro.sj.Shioaji=FreshApi
        app.process_broker_login("k","s","A123456789","ca.pfx","pw"); app.flush_after()
        assert v.logout_called is False, "從未登入的物件不應被 logout (可能引發額外卡住)"
        assert app.api_logged_in is True and app._login_in_progress is False, "應成功登入且旗標清除"
        # 曾登入過的物件會被 logout
        class Logged:
            def __init__(s): s.logout_called=False
            def logout(s): s.logout_called=True
        old=Logged(); app.sj_api=old; app.api_logged_in=True
        FreshApi.created=[]
        app.process_broker_login("k","s","A123456789","ca.pfx","pw"); app.flush_after()
        _t.sleep(0.05)
        assert old.logout_called is True, "曾登入的舊物件應被 logout 釋放"
        # 登入中防止重複點擊
        app.api_logged_in=False; app._login_in_progress=True
        logs=[]; ol=app.log_message
        app.log_message=lambda m:(logs.append(m), ol(m))[0]
        try:
            app.toggle_login()
        finally:
            app.log_message=ol
        assert any('登入正在進行中' in m for m in logs), "登入中應擋下重複點擊並提示"
        app._login_in_progress=False
        # 登入失敗按鈕與旗標復原
        class FailApi(FreshApi):
            def login(s, **k): raise Exception("boom")
        stock_app_pro.sj.Shioaji=FailApi
        app.api_logged_in=False; app._login_in_progress=True
        app.btn_login.config(text="⏳ 連線中...請稍候", bg="#8A99AD", fg="black")
        app.process_broker_login("k","s","A123456789","ca.pfx","pw"); app.flush_after()
        assert app._login_in_progress is False, "失敗後應清除進行中旗標"
        assert app.btn_login['text']=="🔒 登入券商實盤 API", "失敗後按鈕應復原可再次點擊"
    finally:
        if orig_shioaji is not None:
            stock_app_pro.sj.Shioaji = orig_shioaji
        app.api_logged_in=False; app._login_in_progress=False

run_case("ADR-031: hover漲跌點數/我的庫存/TXFR2路由/美股自選股報價", _round11_positions_and_symbol_routing)
def _round14_positions_detail_chinese():
    """【ADR-034】庫存明細視窗欄位標題與方向值全面中文化。"""
    class Pos:
        def __init__(s): 
            s.id=0; s.code='0050'; s.direction='Action.Buy'; s.quantity=21
            s.price=74.84; s.last_price=106.3; s.pnl=663161.0; s.yd_quantity=21
    class FStk:
        def get(s,k): return None
    class FApi:
        def __init__(s):
            s.stock_account=object(); s.futopt_account=None
            class C: pass
            s.Contracts=C(); s.Contracts.Stocks=FStk()
        def list_positions(s,a): return [Pos()]
    app.sj_api=FApi(); app.api_logged_in=True
    rows, raws = app._positions_fetch_once()
    app._apply_positions(rows, raws); app.flush_after()
    assert app._position_field_label('code')=='代碼', "code 應顯示為代碼"
    assert app._position_field_label('direction')=='方向', "direction 應顯示為方向"
    assert app._position_field_label('quantity')=='庫存量', "quantity 應顯示為庫存量"
    assert app._position_field_label('last_price')=='現價', "last_price 應顯示為現價"
    assert app._position_field_label('pnl')=='損益', "pnl 應顯示為損益"
    assert app._position_field_label('yd_quantity')=='昨日庫存', "yd_quantity 應顯示為昨日庫存"
    assert app._position_field_display('direction','Action.Buy')=='買進', "方向值應轉為買進/賣出"
    assert app._position_field_label('未知全新欄位')=='未知全新欄位', "未知欄位應保留原key不遺漏資料"
    app._open_positions_detail_window()  # 不拋例外即可
    app.api_logged_in=False

run_case("ADR-032: 登入凍結三修正(virgin不logout/防重複/watchdog)", _round12_login_freeze_mitigation)
def _round15_quant_trading():
    """【ADR-035/036】量化自動交易核心安全行為 + 美股漲跌口徑 + 期貨帳戶406。"""
    import time as _t
    import numpy as _np
    import pandas as _pd
    from core import strategy_engine as _se
    def _cross_kbars():
        closes=[100-i*0.5 for i in range(30)]+[86+i*2.0 for i in range(6)]
        sr=_pd.Series(closes); f=sr.rolling(3).mean(); sl=sr.rolling(10).mean()
        cut=next(i for i in range(1,len(sr)) if f[i-1]<=sl[i-1] and f[i]>sl[i])
        closes=closes[:cut+1]+[closes[cut]]
        idx=_pd.date_range("2026-07-16 09:00",periods=len(closes),freq="1min")
        c=_np.array(closes,dtype=float)
        return {'ts':list(idx),'open':list(c),'high':list(c+0.5),'low':list(c-0.5),
                'close':list(c),'volume':[100]*len(c),'amount':list(c*100)}
    class FC:
        def __init__(s,code,name=""): s.code=code; s.symbol=code; s.name=name; s.category=code[:3]
    class FGrp:
        def __init__(s): s._m={'TXFR1':FC('TXFR1','臺股期貨')}
        def get(s,k): return s._m.get(k)
        def __iter__(s): return iter(s._m.values())
    class FApi:
        def __init__(s):
            s.placed=[]
            class C: pass
            s.Contracts=C()
            class FStk:
                def __init__(s2): s2._m={'2330':FC('2330','台積電')}
                def get(s2,k): return s2._m.get(k)
            s.Contracts.Stocks=FStk()
            class FFut:
                TXF=FGrp()
                def __iter__(s2): return iter([s2.TXF])
            s.Contracts.Futures=FFut()
            s.stock_account=object(); s.futopt_account=object()
        def kbars(s,c,start=None,end=None): return _cross_kbars()
        def Order(s,**kw):
            class O: pass
            o=O(); o.kw=kw; return o
        def place_order(s,c,o):
            s.placed.append((getattr(c,'code',''),o.kw))
            class T:
                class status: status='PendingSubmit'
            return T()
        def list_positions(s,a):
            if a is s.futopt_account:
                raise Exception("ServerError: code: 406, detail: Account Not Acceptable.")
            return []
    api=FApi(); app.sj_api=api; app.api_logged_in=True
    app.strategies=[]; app.strategy_runtimes={}; app._kbars_raw_cache.clear()
    s=_se.new_strategy()
    # 【ADR-099】session_gate=False,理由同上 (診斷需與時鐘無關)。
    s.update({'name':'診斷金叉','symbol':'2330','market':'台股','timeframe':'1分K','qty':1,
              'cooldown_sec':0,'enabled':True,'stop_loss_pct':2.0,'session_gate':False,
              'entry':[{'type':'ma_cross_up','params':{'fast':3,'slow':10}}]})
    app.strategies.append(s); app.strategy_runtimes[s['id']]=_se.new_runtime()
    # 總開關關閉:完全不動作 (最重要的安全行為)
    app._qt_running=False
    eval_pass(); app.flush_after()
    assert api.placed==[] and app.strategy_runtimes[s['id']]['state']=='FLAT', "總開關關閉時不可有任何動作"
    # 啟動+模擬:有訊號、無真實單
    app._qt_running=True
    logs=[]; ol=app.log_message; app.log_message=lambda m:(logs.append(m), ol(m))[0]
    try:
        eval_pass(); app.flush_after()
        assert any('自動交易-模擬' in m for m in logs) and api.placed==[], "模擬模式不可下真實單"
        assert app.strategy_runtimes[s['id']]['state']=='LONG', "模擬應建立虛擬持倉"
        # 同一根K棒不重複
        n=sum(1 for m in logs if '自動交易-模擬' in m)
        eval_pass(); app.flush_after()
        assert sum(1 for m in logs if '自動交易-模擬' in m)==n, "同一根K棒不可重複觸發"
        # 實單參數鏡射
        s2=_se.new_strategy()
        s2.update({'name':'診斷實單','symbol':'TXF','market':'台期貨','timeframe':'1分K','qty':1,
                   'cooldown_sec':0,'mode':'實單','enabled':True,'stop_loss_pct':1.0,
                   'session_gate':False,   # 【ADR-099】診斷需與時鐘無關,理由同上
                   'entry':[{'type':'ma_cross_up','params':{'fast':3,'slow':10}}]})
        app.strategies.append(s2); app.strategy_runtimes[s2['id']]=_se.new_runtime()
        eval_pass(); app.flush_after()
        assert len(api.placed)==1 and api.placed[0][0]=='TXFR1', "實單應送出期貨委託"
        kw=api.placed[0][1]
        assert str(kw.get('price_type')).endswith('LMT') and kw.get('quantity')==1, "下單參數鏡射錯誤"
        # 急停
        app._qt_stop_all()
        n_placed=len(api.placed)
        eval_pass(); app.flush_after()
        assert app._qt_running is False and len(api.placed)==n_placed, "急停後不可再有任何動作"
        # 期貨帳戶406只提示一次 (ADR-036)
        app._fut_positions_unavailable=False
        app._positions_fetch_once(); app.flush_after()
        app._positions_fetch_once(); app.flush_after()
        assert sum(1 for m in logs if '406' in m)==1, "期貨406應只提示一次"
    finally:
        app.log_message=ol
        app._qt_running=False; app.api_logged_in=False
        app.strategies=[]; app.strategy_runtimes={}
    # 美股漲跌口徑 (ADR-036):未還原日K最後兩收盤
    class FT:
        def history(s2, period=None, interval=None, auto_adjust=None):
            assert auto_adjust is False
            return _pd.DataFrame({'Close':[88.50,88.84]}, index=_pd.date_range("2026-07-15",periods=2,freq="D"))
        fast_info=None; info={'shortName':'X'}
    orig_tk=stock_app_pro.yf.Ticker
    stock_app_pro.yf.Ticker=lambda s2: FT()
    try:
        app._wl_us_names={}
        q=app._wl_fetch_us_quotes(['SPYM'])
        assert abs(q['SPYM'][1]-0.34)<1e-9, f"美股漲跌口徑錯誤: {q}"
    finally:
        stock_app_pro.yf.Ticker=orig_tk

run_case("ADR-034: 庫存明細視窗欄位標題與方向值中文化", _round14_positions_detail_chinese)
def _round16_daytrading_pack():
    """【ADR-037】庫存股數unit=Share/期指自選股串流/水平虛線/主圖自動更新/靜音警告。"""
    import numpy as _np
    import pandas as _pd
    # 1. 庫存 unit=Share
    class Pos:
        def __init__(s,q): s.id=0; s.code='0050'; s.direction='Buy'; s.quantity=q; s.price=74.84; s.last_price=106.4; s.pnl=665286.0
    class FApi1:
        def __init__(s):
            s.stock_account=object(); s.futopt_account=None
            class C: pass
            s.Contracts=C()
            class FStk:
                def get(s2,k): return None
            s.Contracts.Stocks=FStk()
        def list_positions(s, acct, unit=None):
            assert unit is not None, "證券帳戶應以 unit=Share 查詢"
            return [Pos(21080)]
    app.sj_api=FApi1(); app.api_logged_in=True
    rows,_=app._positions_fetch_once()
    assert rows[0]['qty']=='21080股', f"股數顯示錯誤: {rows[0]['qty']}"
    # 2. 期指/指數串流:訂閱+tick路由 (股票不訂閱)
    class FC:
        def __init__(s,code,name="",ref=0): s.code=code; s.symbol=code; s.name=name; s.category=code[:3]; s.reference=ref
    class FGrp:
        def __init__(s): s._m={'TXFR1':FC('TXFR1','臺股期貨',46000)}
        def get(s,k): return s._m.get(k)
        def __iter__(s): return iter(s._m.values())
    class FApi2:
        def __init__(s):
            s.subs=[]
            class Q:
                def __init__(s2,o): s2.o=o
                def subscribe(s2,c,**kw): s2.o.subs.append(getattr(c,'code',''))
                def unsubscribe(s2,*a,**k): pass
            s.quote=Q(s)
            class C: pass
            s.Contracts=C()
            class FStk:
                def get(s2,k): return None
            s.Contracts.Stocks=FStk()
            class FFut:
                TXF=FGrp()
                def __iter__(s2): return iter([s2.TXF])
            s.Contracts.Futures=FFut()
            class IT: pass
            class TSE: TSE001=FC('001',name='加權指數',ref=45000)
            class OTC: OTC101=FC('101',name='櫃買指數',ref=415)
            s.Contracts.Indexs=IT(); s.Contracts.Indexs.TSE=TSE; s.Contracts.Indexs.OTC=OTC
        def snapshots(s,cs): return []
    app.sj_api=FApi2()
    app._wl_contract_cache.clear(); app._wl_subscribed.clear()
    app._wl_fut_code_map.clear(); app._wl_idx_code_map.clear(); app._wl_stream_quotes={}
    app.watchlists={'T':['TXF','^TWII','0050']}; app.current_wl_name.set('T')
    app.on_wl_change(); app.flush_after()
    app._wl_ensure_stream_subs()
    assert len(app.sj_api.subs)==2, f"應只訂閱期貨+指數 (股票不訂閱),實際 {app.sj_api.subs}"
    class Tick:
        def __init__(s,code,close,chg=None,pct=None):
            s.code=code; s.close=close; s.volume=1
            if chg is not None: s.price_chg=chg; s.pct_chg=pct
    app.current_contract=None
    app.on_tick_fop_v1(None, Tick('TXFG6', 46022.0, -44.0, -0.10))
    assert app._wl_stream_quotes.get('TXF')==(46022.0,-44.0,-0.10), "期貨 tick 路由失敗"
    app.on_tick_stk_v1(None, Tick('001', 45631.59))
    assert '^TWII' in app._wl_stream_quotes, "指數 tick 路由失敗"
    # 3+8. 水平虛線與靜音警告 (程式碼層)
    s=open('stock_app_pro.py',encoding='utf-8').read()
    assert 'self.hline_main = axlist[0].axhline' in s and "set_ydata([row['Close'], row['Close']])" in s, "水平虛線缺失"
    assert 'warn_too_much_data=2000000' in s, "大量資料警告未靜音"
    # 6. 主圖自動更新:新K棒重繪+視野跟隨/無變化不重繪
    class FApi3:
        def kbars(s2,c,start=None,end=None):
            idx=_pd.date_range("2026-07-16 09:00",periods=12,freq="1min")
            cl=list(_np.linspace(100,111,12))
            return {'ts':list(idx),'open':cl,'high':[x+.5 for x in cl],'low':[x-.5 for x in cl],
                    'close':cl,'volume':[100]*12,'amount':[x*100 for x in cl]}
    app.sj_api=FApi3()
    idx=_pd.date_range("2026-07-16 09:00",periods=11,freq="1min")
    c=_np.linspace(100,110,11)
    app.current_df=_pd.DataFrame({'Open':c,'High':c+0.5,'Low':c-0.5,'Close':c,'Volume':[100]*11},index=idx)
    app.current_contract=FC('TXFR1'); app.current_symbol='TXFR1'
    app.current_timeframe='1分K'; app.asset_type='future'
    draws=[]
    orig_draw=app.draw_chart; app.draw_chart=lambda df: draws.append(len(df))
    class Ax:
        def get_xlim(s2): return (1.0,10.5)
    orig_ax=app.axlist; app.axlist=[Ax()]
    try:
        app._fetch_seq=999
        app._chart_auto_refresh_once(); app.flush_after()
        assert draws==[13] and app.saved_xlim==(3.0,12.5), f"自動更新失敗: {draws}, {app.saved_xlim}"
        draws.clear()
        app._chart_auto_refresh_once(); app.flush_after()
        assert draws==[], "無變化不應重繪"
    finally:
        app.draw_chart=orig_draw; app.axlist=orig_ax
        app.current_df=None; app.current_contract=None; app.current_timeframe=None
        app.api_logged_in=False

run_case("ADR-035/036: 量化自動交易安全行為/美股漲跌口徑/期貨406", _round15_quant_trading)
def _round17_autorefresh_race_and_quant_btn():
    """【ADR-038】主圖自動更新競態防護 (kbars鎖/讓路/df身分守衛) + 量化按鈕列可見。"""
    import time as _t
    import threading as _th
    import numpy as _np
    import pandas as _pd
    class FC:
        def __init__(s,code): s.code=code; s.symbol=code; s.category=code[:3]; s.reference=0
    def _mk(closes):
        idx=_pd.date_range("2026-07-16 09:00",periods=len(closes),freq="1min")
        c=_np.array(closes,dtype=float)
        return _pd.DataFrame({'Open':c,'High':c+0.5,'Low':c-0.5,'Close':c,'Volume':[100]*len(c)},index=idx)
    # 防護1:kbars 鎖串行化
    class SlowApi:
        def __init__(s): s.concurrent=0; s.max_concurrent=0
        def kbars(s,c,start=None,end=None):
            s.concurrent+=1; s.max_concurrent=max(s.max_concurrent,s.concurrent)
            _t.sleep(0.03); s.concurrent-=1
            idx=_pd.date_range("2026-07-16 09:00",periods=10,freq="1min")
            cl=list(_np.linspace(100,109,10))
            return {'ts':list(idx),'open':cl,'high':[x+.5 for x in cl],'low':[x-.5 for x in cl],
                    'close':cl,'volume':[100]*10,'amount':[x*100 for x in cl]}
    app.sj_api=SlowApi(); app.api_logged_in=True
    c=FC('TXFR1')
    ths=[_th.Thread(target=lambda: app._download_kbars_raw(c, stock_app_pro.datetime.now()-stock_app_pro.timedelta(days=4), stock_app_pro.datetime.now())) for _ in range(5)]
    for t in ths: t.start()
    for t in ths: t.join()
    assert app.sj_api.max_concurrent==1, f"kbars 應被鎖串行化,實際最高併發 {app.sj_api.max_concurrent}"
    # 防護2:查詢進行中自動更新讓路
    class FApi:
        def kbars(s,c,start=None,end=None):
            idx=_pd.date_range("2026-07-16 09:00",periods=12,freq="1min")
            cl=list(_np.linspace(200,211,12))
            return {'ts':list(idx),'open':cl,'high':[x+.5 for x in cl],'low':[x-.5 for x in cl],
                    'close':cl,'volume':[100]*12,'amount':[x*100 for x in cl]}
    app.sj_api=FApi()
    app.current_contract=FC('TMFR1'); app.current_symbol='TMFR1'
    app.current_timeframe='1分K'; app.asset_type='future'
    app.current_df=_mk(list(_np.linspace(100,110,11)))
    draws=[]
    orig_draw=app.draw_chart; app.draw_chart=lambda df: draws.append(1)
    class Ax:
        def get_xlim(s): return (1.0,10.5)
    orig_ax=app.axlist; app.axlist=[Ax()]
    try:
        app._fetch_in_progress=True
        app._chart_auto_refresh_once(); app.flush_after()
        assert draws==[], "查詢進行中自動更新必須讓路"
        app._fetch_in_progress=False
        # 防護3:期間 current_df 物件被換掉 → 作廢
        orig_dl=app._download_kbars_raw
        def swap(c2,s2,e2):
            r=orig_dl(c2,s2,e2); app.current_df=_mk(list(_np.linspace(300,310,11))); return r
        app._download_kbars_raw=swap
        draws.clear()
        app._chart_auto_refresh_once(); app.flush_after()
        app._download_kbars_raw=orig_dl
        assert draws==[], "期間 df 物件換過必須作廢本次合併 (防止黏錯商品)"
        # 正常情況能更新
        app.current_df=_mk(list(_np.linspace(100,110,11)))
        draws.clear(); app._fetch_seq += 1
        app._chart_auto_refresh_once(); app.flush_after()
        assert len(draws)==1, "無競態時應正常自動更新"
    finally:
        app.draw_chart=orig_draw; app.axlist=orig_ax
        app.current_df=None; app.current_contract=None; app.current_timeframe=None
        app.api_logged_in=False; app._fetch_in_progress=False
    # 第4項:量化按鈕列先 pack
    s=open('stock_app_pro.py',encoding='utf-8').read()
    # 【ADR-057】量化 UI 已抽成 _build_quant_panel (供分頁 + 獨立視窗共用),
    # 檢查意圖不變:按鈕列必須先 pack(side=BOTTOM),否則面板變矮時
    # 「新增策略」會被擠出可視範圍 (P-44)。
    _pan = s.index("def _build_quant_panel")
    _seg = s[_pan:s.index("def _qt_alive_uis")]
    assert _seg.index("btns.pack(side=tk.BOTTOM") < _seg.index("tree.pack(side=tk.LEFT"), \
        "量化按鈕列必須先 pack,否則面板變矮時「新增策略」被擠出看不到"
    # 獨立視窗入口必須存在 (ADR-057 使用者需求 #1)
    assert "def open_quant_window" in s, "缺少量化交易獨立視窗"
    assert "🗔 開啟量化交易視窗" in s, "底部分頁缺少開啟獨立視窗的入口按鈕"

run_case("ADR-037: 庫存股數/期指串流/水平線/主圖自動更新/靜音警告", _round16_daytrading_pack)
def _round18_backtest():
    """【ADR-039】回測引擎:重用實盤邏輯逐根重放,產生完整報告 (數字+markers+交易)。"""
    import numpy as _np
    import pandas as _pd
    from core import backtest as _bt
    from core import strategy_engine as _se
    def _mk():
        closes=[100-i for i in range(20)]+[80+i*3 for i in range(10)]+[107-i*2 for i in range(10)]
        n=len(closes)
        idx=_pd.date_range("2026-05-01 09:00",periods=n,freq="1D")
        c=_np.array(closes,dtype=float)
        return {'ts':list(idx),'open':list(c),'high':list(c+1),'low':list(c-1),
                'close':list(c),'volume':[100]*n,'amount':list(c*100)}
    class FC:
        def __init__(s,code,name=""): s.code=code; s.symbol=code; s.name=name; s.category=code[:3]; s.reference=100; s.delivery_month=''
    class FApi:
        def __init__(s): s.n=0
        def kbars(s,c,start=None,end=None): s.n+=1; return _mk()
        class Contracts:
            class Stocks:
                @staticmethod
                def get(k): return FC('2330','台積電') if k=='2330' else None
    app.sj_api=FApi(); app.api_logged_in=True
    app.strategies=[]; app.strategy_runtimes={}
    s=_se.new_strategy()
    s.update({'name':'回測診斷','symbol':'2330','market':'台股','timeframe':'日K','qty':1,
              'direction':'做多','entry':[{'type':'ma_cross_up','params':{'fast':3,'slow':10}}],
              'exit_signals':[{'type':'ma_cross_down','params':{'fast':3,'slow':10}}],'stop_loss_pct':0})
    app.strategies.append(s); app.strategy_runtimes[s['id']]=_se.new_runtime()
    # 純引擎回測:直接算,驗證報告結構與「回測=實盤同邏輯」
    rawdf=app._download_kbars_raw(FC('2330'), stock_app_pro.datetime.now()-stock_app_pro.timedelta(days=100), stock_app_pro.datetime.now())
    df=app._resample_sj_df(rawdf,'日K',asset_type='stock')
    result=_bt.run_backtest(s, df, slippage_ticks=2, tick_size=0.05)
    assert all(k in result for k in ('trades','equity','markers','metrics')), "回測報告結構不完整"
    assert result['metrics']['trades']>=1 and result['trades'][0]['pnl']>0, "金叉大漲段應獲利"
    assert any(m['kind']=='buy_open' for m in result['markers']), "缺進場 marker"
    # 回測進場點=引擎金叉點的下一根 (ADR-064:T+1 開盤成交模型——訊號用金叉當根
    # 收盤判定,成交延到下一根開盤;這支診斷案例在 ADR-064 之前寫的是「同根即成交」
    # 的舊假設，ADR-064 已把 tests/test_core.py 對應的斷言改成 cross_i+1，但沒有
    # 同步改到這裡，導致這個診斷案例本身變成一個過期的假警報 [P-57])。
    f=df['Close'].rolling(3).mean(); sl=df['Close'].rolling(10).mean()
    cross_i=next(i for i in range(1,len(df)) if f.iloc[i-1]<=sl.iloc[i-1] and f.iloc[i]>sl.iloc[i])
    first_open=next(m['ts'] for m in result['markers'] if m['kind']=='buy_open')
    assert first_open==df.index[cross_i+1], "回測進場點必須等於實盤引擎金叉點的下一根 (T+1開盤成交,同一套邏輯)"
    # 背景 worker 全流程 + 報告視窗建構
    reports=[]
    orig=app._qt_show_backtest_report
    app._qt_show_backtest_report=lambda st,d,r: reports.append(r)
    try:
        app._qt_backtest_worker(s)
        import time as _t; _t.sleep(0.05); app.flush_after()
        assert len(reports)==1 and reports[0]['metrics']['trades']>=1, "背景回測 worker 未產生報告"
    finally:
        app._qt_show_backtest_report=orig
    # 報告視窗真實建構不拋例外
    import matplotlib; matplotlib.use('Agg')
    app._qt_show_backtest_report(s, df, result)
    app.api_logged_in=False

run_case("ADR-038: 主圖自動更新競態防護 + 量化按鈕列可見", _round17_autorefresh_race_and_quant_btn)
def _round19_custom_strategy():
    """【ADR-040】自訂 Python 策略:子行程執行/決策轉intent/回測同路/錯誤停用。"""
    import numpy as _np
    import pandas as _pd
    from core import custom_strategy as _cs
    from core import strategy_engine as _se
    from core import backtest as _bt
    def _mk():
        closes=[100-i for i in range(20)]+[80+i*3 for i in range(10)]+[107-i*2 for i in range(20)]
        n=len(closes); idx=_pd.date_range("2026-04-01 09:00",periods=n,freq="1D")
        c=_np.array(closes,dtype=float)
        return {'ts':list(idx),'open':list(c),'high':list(c+1),'low':list(c-1),'close':list(c),'volume':[100]*n,'amount':list(c*100)}
    class FC:
        def __init__(s,code,name=""): s.code=code; s.symbol=code; s.name=name; s.category=code[:3]; s.reference=100; s.delivery_month=''
    class FApi:
        def kbars(s,c,start=None,end=None): return _mk()
        class Contracts:
            class Stocks:
                @staticmethod
                def get(k): return FC('2330','台積電') if k=='2330' else None
    app.sj_api=FApi(); app.api_logged_in=True
    app.strategies=[]; app.strategy_runtimes={}
    # 純邏輯:決策正規化與轉intent
    assert _cs.normalize_decision('買進')=='BUY' and _cs.normalize_decision('亂寫')=='HOLD', "決策正規化失效"
    rt=_se.new_runtime()
    i=_cs.decision_to_intent('BUY', {'qty':2,'market':'台股','direction':'做多'}, rt, 100.0)
    assert i and i['kind']=='OPEN' and i['qty']==2, "決策轉intent失效"
    assert _cs.decision_to_intent('SELL', {'qty':1,'market':'台股'}, _se.new_runtime(), 100) is None, "股票不可放空"
    # 子行程執行 (真實 subprocess + 逾時保護)
    s=_se.new_strategy()
    s.update({'kind':'custom','name':'自訂金叉','symbol':'2330','market':'台股','timeframe':'日K','qty':1,
              'direction':'做多','mode':'模擬','enabled':True,'source_code':_cs.EXAMPLE_SOURCE,
              'custom_params':{'fast':3,'slow':10},'stop_loss_pct':0,'entry':[{'type':'ma_cross_up','params':{}}],'exit_signals':[]})
    app.strategies.append(s); app.strategy_runtimes[s['id']]=_se.new_runtime()
    df=app._resample_sj_df(app._download_kbars_raw(FC('2330'), stock_app_pro.datetime.now()-stock_app_pro.timedelta(days=90), stock_app_pro.datetime.now()), '日K', asset_type='stock')
    d=app._run_custom_in_subprocess(s, df, 'FLAT')
    assert d in ('BUY','SELL','CLOSE','HOLD'), f"子行程決策異常: {d}"
    # 回測:自訂=等價內建進場點
    custom={'kind':'custom','name':'C','symbol':'2330','market':'台股','qty':1,'direction':'做多',
            'source_code':_cs.EXAMPLE_SOURCE,'custom_params':{'fast':3,'slow':10},'stop_loss_pct':0}
    rc=_bt.run_backtest(custom, df)
    builtin=_se.new_strategy()
    builtin.update({'name':'B','symbol':'2330','qty':1,'direction':'做多',
                    'entry':[{'type':'ma_cross_up','params':{'fast':3,'slow':10}}],
                    'exit_signals':[{'type':'ma_cross_down','params':{'fast':3,'slow':10}}],'stop_loss_pct':0})
    rb=_bt.run_backtest(builtin, df)
    assert rc['metrics']['trades']>=1, "自訂策略回測無交易"
    c_open=next(m['ts'] for m in rc['markers'] if m['kind']=='buy_open')
    b_open=next(m['ts'] for m in rb['markers'] if m['kind']=='buy_open')
    assert c_open==b_open, "自訂策略進場點應=等價內建策略"
    # 壞策略連錯自動停用,不影響其他策略
    s_bad=_se.new_strategy()
    s_bad.update({'kind':'custom','name':'壞','symbol':'2330','market':'台股','timeframe':'日K','qty':1,'mode':'模擬',
                  'enabled':True,'source_code':"def on_bar(ctx): return 1/0",'custom_params':{},'stop_loss_pct':0,'entry':[{'type':'ma_cross_up','params':{}}]})
    app.strategies.append(s_bad); app.strategy_runtimes[s_bad['id']]=_se.new_runtime()
    app._qt_running=True
    for _ in range(3):
        app._quant_eval_pass(now_ts=5000.0, today_str='2026-06-20'); app.flush_after()
    assert s_bad['enabled'] is False and s['enabled'] is True, "壞自訂策略應自動停用且不影響其他策略"
    app._qt_running=False; app.api_logged_in=False

run_case("ADR-039: 策略回測引擎 (重用實盤邏輯/完整報告/K線標點)", _round18_backtest)
def _round20_paper_livebar_speed():
    """【ADR-041】虛擬模擬帳戶/邊界排程/股票串流/活K棒/完整段重試階梯。"""
    import numpy as _np
    import pandas as _pd
    from core import strategy_engine as _se
    from core import paper_account as _pa
    class FC:
        def __init__(s,code,name="",ref=100): s.code=code; s.symbol=code; s.name=name; s.category=code[:3]; s.reference=ref; s.delivery_month=''
    def _cross_kbars():
        closes=[100-i*0.5 for i in range(30)]+[86+i*2.0 for i in range(6)]
        sr=_pd.Series(closes); f=sr.rolling(3).mean(); sl=sr.rolling(10).mean()
        cut=next(i for i in range(1,len(sr)) if f[i-1]<=sl[i-1] and f[i]>sl[i])
        closes=closes[:cut+1]+[closes[cut]]
        idx=_pd.date_range("2026-05-01 09:00",periods=len(closes),freq="1D")
        c=_np.array(closes,dtype=float)
        return {'ts':list(idx),'open':list(c),'high':list(c+1),'low':list(c-1),'close':list(c),'volume':[100]*len(c),'amount':list(c*100)}
    class FApi:
        def kbars(s,c,start=None,end=None): return _cross_kbars()
        class Contracts:
            class Stocks:
                @staticmethod
                def get(k): return FC('2330','台積電') if k=='2330' else None
    # 1. 虛擬帳戶純邏輯
    a=_pa.new_account(1000000)
    _pa.apply_fill(a,'t','台股','0050','買進','OPEN',1,100.0)
    _pa.mark_price(a,'0050',106.0)
    assert abs(_pa.equity(a)-(1000000-100000*_pa.STOCK_FEE_RATE+6000))<0.01, "權益計算錯誤"
    rec=_pa.apply_fill(a,'t','台股','0050','賣出','CLOSE',1,106.0)
    assert '0050' not in a['positions'] and rec['pnl']>0, "平倉記帳錯誤"
    b=_pa.new_account(500000)
    _pa.apply_fill(b,'t','台期貨','TMFR1','賣出','OPEN',1,46000)
    _pa.apply_fill(b,'t','台期貨','TMFR1','買進','CLOSE',1,45900)
    assert abs(b['cash']-(500000+1000-100))<0.01, "期貨乘數/手續費記帳錯誤"
    # 2. 模擬成交進虛擬帳戶 (GUI流)
    app.sj_api=FApi(); app.api_logged_in=True
    app._kbars_raw_cache.clear()  # 前案例同商品(2330)的K棒快取會蓋掉本案例假資料
    app.strategies=[]; app.strategy_runtimes={}
    app.paper_accts={'default':_pa.new_account(account_id='default')}
    s=_se.new_strategy()
    # 【ADR-099】session_gate=False:診斷腳本必須能在任何時間跑,不受台股開盤
    # 時段影響 (ADR-070 的時段閘門在非交易時間會直接跳過評估,導致這些案例
    # 只有在盤中執行才會通過——等於平常完全失去保護作用)。
    s.update({'name':'診斷模擬','symbol':'2330','market':'台股','timeframe':'日K','qty':1,'mode':'模擬',
              'enabled':True,'direction':'做多','cooldown_sec':0,'session_gate':False,
              'entry':[{'type':'ma_cross_up','params':{'fast':3,'slow':10}}],'stop_loss_pct':2.0})
    app.strategies.append(s); app.strategy_runtimes[s['id']]=_se.new_runtime()
    app._qt_running=True
    app._quant_eval_pass(now_ts=100.0, today_str='2026-06-05'); app.flush_after()
    assert len(app.paper_accts['default']['positions'])==1 and len(app.paper_accts['default']['history'])==1, "模擬成交未記入虛擬帳戶"
    app._qt_open_paper_window()  # 視窗建構不拋例外
    # 3. 邊界感知:runner自然輪詢同邊界不重複
    calls=[]
    orig=app._qt_fetch_closed_bars
    # 【ADR-099】用 *a/**k 轉發:程式後來新增了 tf/cache_sym/cache_market 關鍵字
    # 參數 (ADR-074 看A做B),舊的三位置參數 lambda 會拋 TypeError 被外層 except
    # 吞掉,導致 calls 永遠是空的、這個案例形同虛設。
    app._qt_fetch_closed_bars=lambda *a, **k:(calls.append(1), orig(*a, **k))[1]
    try:
        app._qt_last_boundary={}
        eval_pass(); app.flush_after()
        n1=len(calls)
        eval_pass(); app.flush_after()
        assert n1>=1 and len(calls)==n1, "同一K棒邊界內不應重複評估"
    finally:
        app._qt_fetch_closed_bars=orig
    # 4. 活K棒狀態機
    app.current_timeframe='1分K'; app._live_bar=None
    app._live_bar_on_tick(46000.0); app._live_bar_on_tick(46010.0); app._live_bar_on_tick(45995.0)
    lb=app._live_bar
    assert lb['o']==46000.0 and lb['h']==46010.0 and lb['l']==45995.0 and lb['c']==45995.0, "活K棒累積錯誤"
    app.current_timeframe='日K'; app._live_bar=None
    app._live_bar_on_tick(46000.0)
    assert app._live_bar is None, "日K不應啟用活K棒"
    # 5. 完整段下載降級 (程式碼層)【ADR-046 改版】:舊「365→180→90 重試階梯」
    #    已被「單次下載優先,失敗改分段補救」取代 —— 驗證新保證:
    #    分段下載函式存在、失敗路徑會呼叫它、例外證據仍進日誌。
    src=open('stock_app_pro.py',encoding='utf-8').read()
    assert '_download_kbars_chunked' in src and '改分段下載補救' in src, "分段下載補救路徑缺失"
    assert '{err_detail}' in src and '【分段下載】' in src, "下載失敗例外證據缺失"
    app._qt_running=False; app.api_logged_in=False; app.current_timeframe=None

run_case("ADR-040: 自訂Python策略 (子行程執行/決策轉intent/回測同路/錯誤停用)", _round19_custom_strategy)
def _round21_tradetype_backtest():
    """【ADR-043】交易種類/回測計價/絕對停損/期貨解析/多策略並行。"""
    import numpy as _np
    import pandas as _pd
    from core import strategy_engine as _se
    from core import backtest as _bt
    from core import paper_account as _pa
    class FC:
        def __init__(s,code,name="",dm=""): s.code=code; s.symbol=code; s.name=name; s.category=code[:3]; s.reference=100; s.delivery_month=dm
    # 1. TMF 近月無 R1 → 取最近月份
    class TMFGrp:
        def __init__(s): s._m={'TMF202608':FC('TMF202608','微型臺指2608',dm='202608'),'TMF202609':FC('TMF202609','微型臺指2609',dm='202609')}
        def get(s,k): return s._m.get(k)
        def __iter__(s): return iter(s._m.values())
    class FApiF:
        class Contracts:
            class Futures:
                TMF=TMFGrp()
                def __iter__(s): return iter([FApiF.Contracts.Futures.TMF])
    app.sj_api=FApiF(); app.api_logged_in=True
    c=app._resolve_futures_contract('TMF')
    assert c is not None and c.symbol=='TMF202608', "TMF無R1應取最近月份合約"
    # 2. 回測計價單位
    def _mk():
        closes=[100-i for i in range(20)]+[80+i*3 for i in range(10)]+[107-i*2 for i in range(20)]
        idx=_pd.date_range("2026-01-01",periods=len(closes),freq="1D"); c2=_np.array(closes,dtype=float)
        return _pd.DataFrame({'Open':c2,'High':c2+1,'Low':c2-1,'Close':c2,'Volume':[100]*len(closes)},index=idx)
    df=_mk()
    base={'name':'T','symbol':'X','qty':1,'direction':'做多',
          'entry':[{'type':'ma_cross_up','params':{'fast':3,'slow':10}}],
          'exit_signals':[{'type':'ma_cross_down','params':{'fast':3,'slow':10}}],'stop_loss_pct':0}
    # 【ADR-050】此段驗「單位換算」,須關閉成本模型 (預設已扣手續費+交易稅);
    # 成本模型本身另有 ADR-050 測試與下方第 2b 項驗證。
    r_stk=_bt.run_backtest(dict(base, trade_type='股票'), df, apply_cost_model=False)
    diff=r_stk['trades'][0]['exit_price']-r_stk['trades'][0]['entry_price']
    assert abs(r_stk['trades'][0]['pnl']-diff*1000)<1e-6, "股票回測應×1000"
    r_odd=_bt.run_backtest(dict(base, trade_type='零股'), df, apply_cost_model=False)
    assert abs(r_odd['trades'][0]['pnl']-diff*1)<1e-6, "零股回測應×1"
    r_fut=_bt.run_backtest(dict(base, trade_type='期貨', symbol='TXF', market='台期貨'), df, apply_cost_model=False)
    d2=r_fut['trades'][0]['exit_price']-r_fut['trades'][0]['entry_price']
    assert abs(r_fut['trades'][0]['pnl']-d2*200)<1e-6, "TXF回測應×200"
    # 2b.【ADR-050】預設必須套用成本模型:總成本 > 0 且 毛損益-成本=淨損益
    r_cost=_bt.run_backtest(dict(base, trade_type='期貨', symbol='TXF', market='台期貨'), df)
    mc=r_cost['metrics']
    assert mc['total_cost']>0, "回測預設應扣真實成本 (手續費+交易稅),不可為 0"
    assert abs((mc['gross_pnl']-mc['total_cost'])-mc['total_pnl'])<1e-6, "毛損益-成本≠淨損益"
    # 3. 絕對停損
    import time as _t
    sa=_se.new_strategy()
    sa.update({'name':'A','symbol':'2330','trade_type':'股票','qty':1,'direction':'做多',
               'entry':[{'type':'ma_cross_up','params':{}}],'stop_loss_pct':0,'stop_loss_abs':5.0})
    rt=_se.new_runtime(); rt.update({'state':'LONG','entry_price':100.0,'qty':1,'day':'2026-01-15'})
    dfa=_pd.DataFrame({'Open':[100]*16,'High':[100]*16,'Low':[100]*16,'Close':[100]*15+[94.0],'Volume':[1]*16},
                      index=_pd.date_range('2026-01-01',periods=16,freq='D'))
    ia=_se.evaluate_strategy(sa,rt,dfa,_t.time(),'2026-01-16')
    assert len(ia)==1 and '停損' in ia[0]['reason'] and '元' in ia[0]['reason'], "股票絕對停損應觸發且以元計"
    # 4. 零股記帳
    acct=_pa.new_account(1000000)
    _pa.apply_fill(acct,'t','台股','2330','買進','OPEN',10,600.0,trade_type='零股')
    assert acct['cash']>993000, "零股10股應只扣約6000"
    # 5. 多策略多標的並行
    def _cross():
        closes=[100-i*0.5 for i in range(30)]+[86+i*2.0 for i in range(6)]
        sr=_pd.Series(closes); f=sr.rolling(3).mean(); sl=sr.rolling(10).mean()
        cut=next(i for i in range(1,len(sr)) if f[i-1]<=sl[i-1] and f[i]>sl[i])
        closes=closes[:cut+1]+[closes[cut]]
        idx=_pd.date_range("2026-04-01",periods=len(closes),freq="1D"); c2=_np.array(closes,dtype=float)
        return {'ts':list(idx),'open':list(c2),'high':list(c2+1),'low':list(c2-1),'close':list(c2),'volume':[100]*len(closes),'amount':list(c2*100)}
    class FApiM:
        def kbars(s,c,start=None,end=None): return _cross()
        class Contracts:
            class Stocks:
                @staticmethod
                def get(k): return FC(k) if k in ('2330','2317') else None
    app.sj_api=FApiM(); app.api_logged_in=True
    app.strategies=[]; app.strategy_runtimes={}; app._kbars_raw_cache.clear()
    for sym in ('2330','2317'):
        st=_se.new_strategy()
        st.update({'name':f'S{sym}','symbol':sym,'trade_type':'股票','market':'台股','timeframe':'日K','qty':1,
                   'mode':'模擬','enabled':True,'direction':'做多','cooldown_sec':0,
                   'entry':[{'type':'ma_cross_up','params':{'fast':3,'slow':10}}],'stop_loss_pct':2.0})
        app.strategies.append(st); app.strategy_runtimes[st['id']]=_se.new_runtime()
    app.paper_accts={'default':_pa.new_account(account_id='default')}; app._qt_running=True
    app._quant_eval_pass(now_ts=100.0, today_str='2026-06-05'); app.flush_after()
    states=[app.strategy_runtimes[st['id']]['state'] for st in app.strategies]
    assert states.count('LONG')==2, "兩個不同標的策略應同時各自進場"
    assert len(app.paper_accts['default']['positions'])==2, "模擬帳戶應同時記錄兩檔"
    app._qt_running=False; app.api_logged_in=False


run_case("ADR-041: 虛擬模擬帳戶/邊界排程/股票串流/活K棒/重試階梯", _round20_paper_livebar_speed)
run_case("ADR-043: 交易種類/回測計價/絕對停損/TMF解析/多策略並行", _round21_tradetype_backtest)

def _adr057_quant_window_and_report():
    """【ADR-057】量化獨立視窗多面板同步 / 金額捨去 / 20年 / 驗算 / GC 策略。"""
    import stock_app_pro as M
    # 1) 金額一律無條件捨去 (不是四捨五入)
    assert M._fmt_amt(-53438.54) == "-53,438", M._fmt_amt(-53438.54)
    assert M._fmt_amt_signed(1234.99) == "+1,234", M._fmt_amt_signed(1234.99)
    assert M._fmt_amt(53438.99) == "53,438"
    # 2) 日K 回測預設 20 年
    assert M.StockTradingAppPro.QT_BACKTEST_DAYS["日K"] == 7300

    # 3) 分頁面板已登記,且可再開一份「獨立視窗」面板 → 兩份同步更新
    assert len(app._qt_uis) >= 1, "量化分頁面板未登記"
    n_before = len(app._qt_uis)
    holder = stock_app_pro.tk.Frame(app)
    app._build_quant_panel(holder, tree_height=20, compact=False)
    assert len(app._qt_uis) == n_before + 1, "第二份面板未登記"

    app.strategies = [{'id': 'aa1', 'name': 'T1', 'symbol': '2330', 'timeframe': '日K',
                       'direction': '做多', 'entry': [], 'exit_signals': [], 'mode': '模擬',
                       'enabled': False, 'kind': 'builtin', 'trade_type': '股票'}]
    app._qt_refresh_tree(); app.flush_after()
    for ui in app._qt_uis:
        assert len(ui['tree'].get_children()) == 1, "多面板未同步刷新"

    # 4) 選取以「使用者實際在操作的面板」為準 (非 compact 那份優先)
    win_ui = [u for u in app._qt_uis if not u.get('compact')][0]
    win_ui['tree'].selection_set('aa1'); win_ui['tree'].focus('aa1')
    got = app._qt_selected()
    assert got and got['id'] == 'aa1', f"獨立視窗的選取沒有被採用: {got}"

    # 5) log_message 鏡射會更新「所有」面板
    app.log_message("測試訊息ABC")
    for ui in app._qt_uis:
        assert "測試訊息ABC" in ui['lastlog'].cget('text'), "面板未鏡射最新訊息"

    # 6) 面板銷毀後會被自動清掉,不留下已死的 widget
    holder.destroy()
    alive = app._qt_alive_uis()
    assert len(alive) == n_before, f"已銷毀面板未被清除: {len(alive)} vs {n_before}"

    # 7) 回測報告驗算:一致的結果全過、被竄改的會被抓到
    from core import backtest as _bt
    trades = [{'pnl': 100.0, 'direction': '做多', 'entry_price': 100.0, 'exit_price': 101.0,
               'qty': 1, 'bars_held': 3, 'entry_ts': pd.Timestamp('2024-01-01'),
               'exit_ts': pd.Timestamp('2024-01-05')}]
    good = {'trades': trades, 'metrics': {'total_pnl': 100.0, 'trades': 1, 'wins': 1, 'losses': 0,
                                          'win_rate': 100.0, 'profit_factor': float('inf'),
                                          'max_drawdown': 0.0, 'max_consec_loss_amount': 0.0}}
    assert all(c['ok'] for c in _bt.audit_result(good)), "一致的報告不該有失敗項"
    bad = {'trades': trades, 'metrics': dict(good['metrics'], total_pnl=999.0)}
    assert any(not c['ok'] for c in _bt.audit_result(bad)), "被竄改的淨損益沒被抓到"

    # 8) GC 策略:自動循環 GC 必須關閉 (否則背景執行緒回收 tk 物件會讓 Tcl abort)
    src = open('stock_app_pro.py', encoding='utf-8').read()
    assert "gc.disable()" in src, "缺少 gc.disable(),第11項崩潰會復發"
    assert "def _gc_tick" in src, "缺少主執行緒定期回收"

    # 9) 強制終止回測的入口存在
    assert "def _qt_offer_abort_backtest" in src

run_case("ADR-057: 量化獨立視窗/金額捨去/20年/報告驗算/GC策略", _adr057_quant_window_and_report)

def _adr058_session_basis_and_skip_download():
    """【ADR-058】盤別口徑 / 期交所涵蓋即跳過下載 / 期間快選。"""
    import stock_app_pro as M
    from core import taifex_daily as _td, futures_session as _fs
    from data import taifex_store as _ts
    import tempfile, os, datetime as _dt

    # 1) 日盤 vs 近全:兩種口徑必須真的不同
    idx, px = [], []
    base = pd.Timestamp('2024-01-02')
    for d in range(3):
        day = base + pd.Timedelta(days=d)
        for h, m in [(8,45),(13,45)]:
            idx.append(day + pd.Timedelta(hours=h, minutes=m)); px.append(100.0)
        idx.append(day + pd.Timedelta(hours=15)); px.append(300.0)   # 夜盤
    mdf = pd.DataFrame({'Open':px,'High':px,'Low':px,'Close':px,'Volume':[1.0]*len(px)},
                       index=pd.DatetimeIndex(idx)).sort_index()
    agg = {'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}
    a = _fs.resample_future_session(mdf, "日K", agg, session_basis='all')
    d = _fs.resample_future_session(mdf, "日K", agg, session_basis='day')
    assert (d['High'] < 200).all(), "只用日盤不該吃到夜盤資料"
    assert (a['High'] >= 300).any(), "近全應包含夜盤資料"
    # 預設值必須等同 'all' (既有行為不可變)
    pd.testing.assert_frame_equal(_fs.resample_future_session(mdf, "日K", agg), a)

    # 2) store 雙口徑互不覆蓋
    tmp = tempfile.mkdtemp()
    d1 = pd.DataFrame({'Open':[1.0],'High':[1.0],'Low':[1.0],'Close':[1.0],'Volume':[1.0]},
                      index=[pd.Timestamp('2024-01-02')])
    d2 = d1 * 2
    _ts.save_daily(tmp,'TX',d1,session='all'); _ts.save_daily(tmp,'TX',d2,session='day')
    assert _ts.load_daily(tmp,'TX',session='all').iloc[0]['Open'] == 1.0
    assert _ts.load_daily(tmp,'TX',session='day').iloc[0]['Open'] == 2.0

    # 3) 期交所涵蓋 → 跳過券商下載 (使用者需求 #1 的核心)
    os.makedirs(os.path.join(tmp,'taifex_daily'), exist_ok=True)
    long_idx = pd.date_range('2010-01-01', periods=3000, freq='B')
    hist = pd.DataFrame({'Open':1.0,'High':1.0,'Low':1.0,'Close':1.0,'Volume':1.0}, index=long_idx)
    _ts.save_daily(tmp,'TX',hist,session='all')
    old_base = app.TAIFEX_BASE_DIR; old_cache = app._taifex_mem_cache
    app.TAIFEX_BASE_DIR = tmp; app._taifex_mem_cache = {}
    try:
        class _C: symbol = "TXFR1"
        nf, nt, note = app._taifex_plan_download(_C(), "future", "日K",
                                                 _dt.datetime(2012,1,1), long_idx[-1].to_pydatetime())
        assert nf is None, f"完整涵蓋時應完全跳過下載,卻回傳 {nf}"
        assert "略過券商下載" in note
        nf2, _, note2 = app._taifex_plan_download(_C(), "future", "日K",
                                                  _dt.datetime(2012,1,1),
                                                  (long_idx[-1] + pd.Timedelta(days=30)).to_pydatetime())
        assert nf2 is not None and pd.Timestamp(nf2) > long_idx[-1], "應只補尾巴"
        # 非期貨不介入
        nf3, _, note3 = app._taifex_plan_download(_C(), "stock", "日K",
                                                  _dt.datetime(2020,1,1), _dt.datetime(2021,1,1))
        assert nf3 == _dt.datetime(2020,1,1) and note3 == "", "非期貨不該被最佳化路徑攔截"
    finally:
        app.TAIFEX_BASE_DIR = old_base; app._taifex_mem_cache = old_cache

    # 4) 口徑偵測:混合口徑要被抓出來並建議改用日盤
    rs = np.random.RandomState(7)
    pre_i = pd.date_range('2015-01-01', periods=150, freq='B')
    post_i = pd.date_range('2018-01-01', periods=150, freq='B')
    pc = 9000 + np.cumsum(rs.randn(150)*20); po = pc*(1+rs.choice([-1,1],150)*0.004)
    qc = 11000 + np.cumsum(rs.randn(150)*20); qo = qc*(1+rs.choice([-1,1],150)*0.0005)
    o = np.concatenate([po,qo]); c = np.concatenate([pc,qc])
    mixed = pd.DataFrame({'Open':o,'High':np.maximum(o,c)+5,'Low':np.minimum(o,c)-5,
                          'Close':c,'Volume':1.0}, index=pre_i.append(post_i))
    r = _td.detect_session_regime(mixed)
    assert r['regime'] == 'mixed' and r['ratio'] > 2.5, r
    assert '只用日盤' in r['note']

    # 5) 期間快選鈕與盤別選項存在
    src = open('stock_app_pro.py', encoding='utf-8').read()
    for lab in ('"3M"','"6M"','"1Y"','"2Y"','"3Y"','"5Y"','"7Y"','"10Y"','"15Y"','"20Y"'):
        assert lab in src, f"缺少期間快選鈕 {lab}"
    assert "只用日盤" in src and "session_basis" in src

run_case("ADR-058: 盤別口徑/期交所涵蓋跳過下載/期間快選", _adr058_session_basis_and_skip_download)

def _adr059_buy_and_hold_and_range():
    """【ADR-059】買進持有回測 / 期末結算 / 持有成本 / 報酬率分母修正 / 報告期間。"""
    from core import backtest as _bt, strategy_engine as _se
    idx = pd.date_range('2020-01-02', periods=400, freq='B')
    c = 100 + np.cumsum(np.random.RandomState(4).randn(400) * 1.2)
    df = pd.DataFrame({'Open': c, 'High': c+1, 'Low': c-1, 'Close': c, 'Volume': 1000.0}, index=idx)
    bnh = {'kind':'builtin','trade_type':'股票','market':'台股','symbol':'0050','name':'BH',
           'qty':1,'direction':'做多','timeframe':'日K','buy_and_hold':True,
           'entry':[{'type':'always_true','params':{}}],'exit_signals':[],
           'stop_loss_pct':0,'take_profit_pct':0,'stop_loss_abs':0,'take_profit_abs':0}
    # 1) 沒有出場方式,但勾了 buy_and_hold → 必須放行
    ok, msg = _se.validate_strategy(bnh)
    assert ok, f"Buy&Hold 應可存檔: {msg}"
    # 沒勾就必須擋下,而且訊息要指路
    nb = dict(bnh); nb['buy_and_hold'] = False
    ok2, msg2 = _se.validate_strategy(nb)
    assert (not ok2) and "買進後持有不賣" in msg2, msg2
    # 勾了又設停損 → 矛盾,要擋
    conflict = dict(bnh); conflict['stop_loss_pct'] = 2.0
    ok3, msg3 = _se.validate_strategy(conflict)
    assert (not ok3) and "矛盾" in msg3, msg3
    # 實單不可用
    live = dict(bnh); live['mode'] = '實單'
    ok4, msg4 = _se.validate_strategy(live)
    assert not ok4, "Buy&Hold 不可用於實單"

    # 2) 【ADR-061 語意更正】條件成立就「再買一次」,不是只買一次
    r = _bt.run_backtest(bnh, df); m = r['metrics']
    assert m['buy_and_hold_mode'] is True
    assert m['bnh_buys'] > 300, f"always_true 應每根都買,實得 {m['bnh_buys']}"
    assert m['trades'] == m['bnh_buys'], "每次買進 = 明細一列"
    assert m['settled_open_at_end'] == m['bnh_buys']
    assert all('期末結算' in t['exit_reason'] for t in r['trades']), "永不賣出"
    # 關掉結算 → 沒有已完成交易
    assert _bt.run_backtest(bnh, df, settle_open_at_end=False)['metrics']['trades'] == 0

    # 3) 累積彙總必須與逐筆明細對得起來 (這就是使用者要的「總持有成本」)
    inv = sum(abs(t['entry_price']) * t['qty'] * 1000.0 for t in r['trades'])
    assert abs(m['bnh_total_invested'] - inv) < 1e-4, (m['bnh_total_invested'], inv)
    assert m['bnh_total_qty'] == sum(t['qty'] for t in r['trades'])
    assert abs(m['bnh_avg_cost'] - inv / (m['bnh_total_qty'] * 1000.0)) < 1e-6
    manual = ((m['bnh_final_price'] - m['bnh_avg_cost']) * m['bnh_total_qty'] * 1000.0
              - m['total_cost'])
    assert abs(manual - m['total_pnl']) < 0.5, (manual, m['total_pnl'])

    # 4) 報酬率分母:累積模式要用「總投入」,不是每筆平均 (否則會變上千%)
    assert abs(m['total_return_pct'] - m['total_pnl'] / m['bnh_total_invested'] * 100.0) < 1e-6
    assert abs(m['total_return_pct']) < 500.0, f"報酬率不該荒謬: {m['total_return_pct']}"

    # 5) 引擎加權平均成本 (累積) vs 一般策略 (覆蓋)
    rt = _se.new_runtime()
    _se.apply_fill(bnh, rt, {'kind':'OPEN','action':'買進','qty':1,'price':100.0}, 1.0)
    _se.apply_fill(bnh, rt, {'kind':'OPEN','action':'買進','qty':3,'price':200.0}, 2.0)
    assert rt['qty'] == 4 and abs(rt['entry_price'] - 175.0) < 1e-9, rt
    rt2 = _se.new_runtime(); normal = dict(bnh); normal['buy_and_hold'] = False
    _se.apply_fill(normal, rt2, {'kind':'OPEN','action':'買進','qty':1,'price':100.0}, 1.0)
    _se.apply_fill(normal, rt2, {'kind':'OPEN','action':'買進','qty':3,'price':200.0}, 2.0)
    assert rt2['qty'] == 3 and abs(rt2['entry_price'] - 200.0) < 1e-9, rt2

    # 5) 報告視窗可開啟且標題含期間
    app._qt_show_backtest_report(bnh, df, r); app.flush_after()
    src = open('stock_app_pro.py', encoding='utf-8').read()
    assert "※ 回測期間:" in src, "報告缺少回測期間顯示"
    assert "buy_and_hold" in src and "建倉成本(首筆)" in src

run_case("ADR-059/061: 累積買進持有/期末結算/總持有成本/報酬率修正", _adr059_buy_and_hold_and_range)

def _adr060_paths_auth_and_taifex_only():
    """【ADR-060】絕對路徑 / 登出即停下載 / 純期交所資料路徑 / 讀取狀態可見。"""
    import stock_app_pro as M
    from core import taifex_daily as _td
    from data import taifex_store as _ts
    import tempfile, os, datetime as _dt

    # 1) 所有資料檔必須是絕對路徑 (不依賴啟動時的工作目錄)
    assert os.path.isabs(M.APP_DIR), M.APP_DIR
    for attr in ('config_file', 'wl_file', 'chart_layout_file', 'indicator_settings_file'):
        v = getattr(app, attr)
        assert os.path.isabs(v), f"{attr} 仍是相對路徑: {v}"
    for attr in ('QT_STRATEGY_FILE', 'QT_STATE_FILE', 'QT_PAPER_FILE'):
        v = getattr(M.StockTradingAppPro, attr)
        assert os.path.isabs(v), f"{attr} 仍是相對路徑: {v}"
    assert os.path.isabs(M.StockTradingAppPro.TAIFEX_BASE_DIR)

    # 2) AuthError / 未登入 必須被判定為「連線已死」→ 立刻中止整批下載
    for msg in ("AuthError: Not authenticated", "Unauthorized", "please login",
                "SessionNotEstablished"):
        assert app._looks_like_session_dead(Exception(msg)), f"未認出: {msg}"

    # 3) 登出 / 強制終止 / 關閉程式 → 背景下載要停手
    old_login = app.api_logged_in
    app.api_logged_in = False
    assert app._downloads_should_abort(), "登出後應停止下載"
    app.api_logged_in = True
    app._backtest_cancel = True
    assert app._downloads_should_abort(), "強制終止後應停止下載"
    app._backtest_cancel = False
    assert not app._downloads_should_abort()
    app.api_logged_in = old_login

    # 4) 純期交所資料路徑:shioaji 空也要產得出K線 (ADR-058 引入的 bug)
    idx = pd.date_range('2015-01-01', periods=800, freq='B')
    c = 9000 + np.cumsum(np.random.RandomState(2).randn(800) * 30)
    hist = pd.DataFrame({'Open': c, 'High': c+20, 'Low': c-20, 'Close': c, 'Volume': 1000.0}, index=idx)
    empty = pd.DataFrame(columns=['Open','High','Low','Close','Volume'])
    for tf, mn in (("日K", 700), ("周K", 100), ("月K", 25)):
        out = _td.extend_shioaji_df(empty, hist, tf)
        assert len(out) >= mn, f"{tf} 純期交所路徑產不出資料: {len(out)}"

    # 5) 讀取狀態必須寫進日誌 (使用者要能看出有沒有讀到)
    tmp = tempfile.mkdtemp()
    _ts.save_daily(tmp, 'TX', hist, session='all')
    old_base, old_cache = app.TAIFEX_BASE_DIR, app._taifex_mem_cache
    app.TAIFEX_BASE_DIR = tmp; app._taifex_mem_cache = {}
    try:
        logs = []
        real_log = app.log_message
        app.log_message = lambda m: logs.append(m)
        app._taifex_load_hist('TX'); app.flush_after()
        assert any('✓ 已讀取 TX' in l for l in logs), f"讀到資料卻沒寫日誌: {logs}"
        logs.clear()
        app._taifex_load_hist('MTX'); app.flush_after()
        assert any('✗ 找不到 MTX' in l and tmp in l for l in logs), \
            f"找不到檔案時要說明完整路徑: {logs}"
        app.log_message = real_log
    finally:
        app.TAIFEX_BASE_DIR = old_base; app._taifex_mem_cache = old_cache

    # 6) 主圖與狀態按鈕
    src = open('stock_app_pro.py', encoding='utf-8').read()
    assert "def show_taifex_status" in src and "🔎 期交所資料狀態" in src
    assert src.count("_taifex_plan_download(") >= 4, "主圖/回測/最佳化都要接上跳過下載"

run_case("ADR-060: 絕對路徑/登出即停/純期交所路徑/讀取狀態可見", _adr060_paths_auth_and_taifex_only)

def _adr062_bnh_modes_and_compare():
    """【ADR-062】三種買進持有模式 / 定期定額 / 條件點選編輯 / 策略比較。"""
    from core import backtest as _bt, strategy_engine as _se
    idx = pd.date_range('2022-01-03', periods=750, freq='B')
    c = 100 + np.cumsum(np.random.RandomState(11).randn(750) * 0.9)
    df = pd.DataFrame({'Open': c, 'High': c+1, 'Low': c-1, 'Close': c, 'Volume': 1000.0}, index=idx)

    def mk(mode, **kw):
        d = {'kind':'builtin','trade_type':'零股','market':'台股','symbol':'0050','name':mode,
             'qty':1,'direction':'做多','timeframe':'日K','buy_and_hold':True,'bnh_mode':mode,
             'entry':[{'type':'always_true','params':{}}],'exit_signals':[],
             'stop_loss_pct':0,'take_profit_pct':0,'stop_loss_abs':0,'take_profit_abs':0}
        d.update(kw); return d

    # 1) 三種模式行為明確不同
    m1 = _bt.run_backtest(mk('single'), df)['metrics']
    m2 = _bt.run_backtest(mk('accumulate'), df)['metrics']
    m3 = _bt.run_backtest(mk('dca', dca_amount=10000.0, dca_interval='month'), df)['metrics']
    assert m1['bnh_buys'] == 1, f"單筆長抱應只買一次: {m1['bnh_buys']}"
    assert m2['bnh_buys'] > 500, f"累積加碼應每根都買: {m2['bnh_buys']}"
    assert 30 <= m3['bnh_buys'] <= 40, f"定期定額(每月)約36期: {m3['bnh_buys']}"
    # 只斷言「必然成立」的關係:同樣每次買 1 單位,買越多次投入越多。
    # 不可假設 dca 與 accumulate 的大小關係 —— 那取決於「每期金額」與
    # 「每次張數」怎麼設定 (本例累積每次只買 1 股≈100元、定期定額每月 10,000 元)。
    assert m1['bnh_total_invested'] < m2['bnh_total_invested'], (m1, m2)
    assert m1['bnh_total_invested'] < m3['bnh_total_invested'], (m1, m3)
    assert m2['bnh_total_qty'] > m3['bnh_buys'], "累積加碼的買進次數應遠多於定期定額" 

    # 2) 定期定額:數量隨價格變動、投入接近計畫、餘額累積不蒸發
    r3 = _bt.run_backtest(mk('dca', dca_amount=10000.0, dca_interval='month'), df)
    assert len({t['qty'] for t in r3['trades']}) > 1, "定期定額數量應隨價格變動"
    planned = m3['bnh_buys'] * 10000.0
    # ADR-064:sizing 用「決策當根收盤價」換算張數,但 T+1 模型的實際成交價是
    # 「下一根開盤價」,隔夜跳空會讓單期實際成本略高於/低於預算——tests/test_core.py
    # 的對應斷言已改成 1% 容忍度 (實測超支約 0.0994%),這裡沒同步改,曾經是嚴格
    # 不等式 `<= planned + 1e-6` 誤報 (P-57:同一個修正沒有同批交付所有呼叫端)。
    assert m3['bnh_total_invested'] <= planned * 1.01
    assert m3['bnh_total_invested'] > planned * 0.9

    # 3) 週期越短買越多次
    wk = _bt.run_backtest(mk('dca', dca_amount=10000.0, dca_interval='week'), df)['metrics']
    assert wk['bnh_buys'] > m3['bnh_buys']

    # 4) 單位規模改用共用函式 (backtest 與 engine 不可各算一份)
    assert _se.unit_size({'trade_type':'股票'}) == 1000.0
    assert _se.unit_size({'trade_type':'零股'}) == 1.0
    src_bt = open('core/backtest.py', encoding='utf-8').read()
    assert 'contract_size = _se.unit_size(s)' in src_bt, "backtest 應改用共用的 unit_size"

    # 5) 驗算全過
    for st in (mk('single'), mk('accumulate'), mk('dca', dca_amount=10000.0, dca_interval='month')):
        bad = [x['name'] for x in _bt.audit_result(_bt.run_backtest(st, df)) if not x['ok']]
        assert not bad, f"{st['bnh_mode']} 驗算失敗: {bad}"

    # 6) GUI:條件點選編輯 + 策略比較入口
    src = open('stock_app_pro.py', encoding='utf-8').read()
    assert '<<ListboxSelect>>' in src and '<Double-Button-1>' in src, "缺少條件點選/雙擊編輯"
    assert 'def _edit_cond_dialog' in src and 'def _sync_builder_from' in src
    assert 'def _qt_compare_dialog' in src and 'def _qt_compare_worker' in src
    assert 'def _qt_prepare_df' in src, "策略比較應與回測共用取資料流程"
    assert '📊 策略比較' in src

    # 7) 比較視窗可開啟
    stgs = [mk('single'), mk('accumulate'), mk('dca', dca_amount=10000.0)]
    for i, x in enumerate(stgs):
        x['id'] = f'cmp{i}'
    old = app.strategies
    app.strategies = stgs
    try:
        app._qt_compare_dialog(); app.flush_after()
    finally:
        app.strategies = old

run_case("ADR-062: 買進持有三模式/定期定額/條件點選編輯/策略比較", _adr062_bnh_modes_and_compare)

def _quant_tree_running_column():
    """量化策略清單新增「運轉狀態」欄:啟用的策略仍要看總開關 (_qt_running)
    是不是真的開著,才算「運轉中」,不能只看策略本身的「啟用」勾選。"""
    from core import strategy_engine as _se
    s_on = _se.new_strategy(); s_on.update({'name': 'RunOn', 'symbol': '2330', 'enabled': True, 'mode': '模擬'})
    s_off = _se.new_strategy(); s_off.update({'name': 'RunOff', 'symbol': '2330', 'enabled': False, 'mode': '模擬'})
    old_strats, old_rts, old_running = app.strategies, app.strategy_runtimes, app._qt_running
    app.strategies = [s_on, s_off]
    app.strategy_runtimes = {s_on['id']: _se.new_runtime(), s_off['id']: _se.new_runtime()}
    try:
        assert 'running' in stock_app_pro.StockTradingAppPro.QT_COLS, "策略清單缺少運轉狀態欄"
        app._qt_running = False
        app._qt_refresh_tree(); app.flush_after()
        # 用目前實際存活的面板 (而非 self.tree_quant),避免前面案例開過的獨立
        # 視窗已關閉、self.tree_quant 停留在舊視窗參照上 (該視窗不再被
        # _qt_refresh_tree 更新,會讀到過期或找不到的列)。
        tree = app._qt_primary_ui()['tree']
        cols = tree['columns']
        vals_on = dict(zip(cols, tree.item(s_on['id'], 'values')))
        vals_off = dict(zip(cols, tree.item(s_off['id'], 'values')))
        assert '停止' in vals_on['running'], "總開關未開時,啟用的策略也不該顯示運轉中"
        assert '停止' in vals_off['running']
        app._qt_running = True
        app._qt_refresh_tree(); app.flush_after()
        vals_on = dict(zip(cols, tree.item(s_on['id'], 'values')))
        vals_off = dict(zip(cols, tree.item(s_off['id'], 'values')))
        assert '運轉中' in vals_on['running'], "總開關開啟且策略啟用時應顯示運轉中"
        assert '停止' in vals_off['running'], "停用的策略即使總開關開啟也不該顯示運轉中"
    finally:
        app.strategies, app.strategy_runtimes, app._qt_running = old_strats, old_rts, old_running

run_case("運轉狀態欄: 需同時看總開關與策略啟用旗標", _quant_tree_running_column)


def _chips_tab_and_views():
    """【ADR-100】籌碼分頁:四種檢視都能在無資料/有資料下正常填表,
    切分頁不觸發任何網路下載,買超紅/賣超綠 (鐵則1)。"""
    import tempfile as _tf
    from data import chips_store as _chipstore
    import pandas as _pd

    # 切到籌碼分頁不可觸發任何 HTTP (下載只能由使用者按鈕發動)
    net_calls = []
    orig_json, orig_taifex = app._chips_http_json, app._chips_http_taifex
    app._chips_http_json = lambda *a, **k: net_calls.append('json')
    app._chips_http_taifex = lambda *a, **k: net_calls.append('taifex')
    old_base = app.CHIPS_BASE_DIR
    tmp = _tf.mkdtemp()
    try:
        app.CHIPS_BASE_DIR = tmp
        app.set_bottom_tab("chips")
        assert not net_calls, f"切到籌碼分頁不應發出網路請求,實際: {net_calls}"
        # 無資料時四種檢視都要能顯示提示而不是拋例外
        for key, _label in app.CHIPS_VIEWS:
            app._chips_view.set(key)
            app._chips_refresh_view()
            assert '請按' in app.lbl_chips_status.cget('text') or \
                   app.lbl_chips_status.cget('text') != '', f"{key} 無資料時應顯示提示"

        # 寫入兩檔個股籌碼:一檔買超、一檔賣超
        day = _pd.DataFrame([
            {'Date': '2026-07-24', 'Code': '2330', 'Name': '台積電',
             'Foreign': 1000, 'Trust': 200, 'Dealer': 300, 'InstTotal': 1500},
            {'Date': '2026-07-24', 'Code': '2317', 'Name': '鴻海',
             'Foreign': -800, 'Trust': -100, 'Dealer': -100, 'InstTotal': -1000},
        ])
        _chipstore.upsert(_chipstore.stock_inst_path(tmp, '2026-07-24'), day)
        app._chips_view.set('stock')
        app.entry_chips_code.delete(0, 'end')
        app._chips_refresh_view()
        rows = app.tree_chips.get_children()
        assert len(rows) == 2, f"個股檢視應顯示 2 列,實際 {len(rows)}"
        # 【鐵則1】買超紅、賣超綠
        tags = {app.tree_chips.item(r, 'values')[1]: app.tree_chips.item(r, 'tags') for r in rows}
        assert 'buy' in tuple(tags['2330']), f"買超應標紅 (buy),實際 {tags['2330']}"
        assert 'sell' in tuple(tags['2317']), f"賣超應標綠 (sell),實際 {tags['2317']}"

        # 代號查詢只回該檔
        app.entry_chips_code.delete(0, 'end'); app.entry_chips_code.insert(0, '2330')
        app._chips_refresh_view()
        rows = app.tree_chips.get_children()
        assert len(rows) == 1 and app.tree_chips.item(rows[0], 'values')[1] == '2330', "代號查詢應只回該檔"

        # 已抓過的日期不可重複下載 (「日後不用重複抓取」的核心保證)
        from datetime import datetime as _dt
        missing = app._chips_missing_days(_dt(2026, 7, 24), _dt(2026, 7, 24))
        assert missing == [], f"已存在的日期不應再列入待抓,實際 {missing}"
        missing2 = app._chips_missing_days(_dt(2026, 7, 20), _dt(2026, 7, 24))
        assert _dt(2026, 7, 24) not in missing2, "已抓過的 7/24 不該再出現"
        assert all(d.weekday() < 5 for d in missing2), "週末不應列入待抓"
        assert not net_calls, "整個流程都不該發出網路請求"
    finally:
        app._chips_http_json, app._chips_http_taifex = orig_json, orig_taifex
        app.CHIPS_BASE_DIR = old_base
        app.set_bottom_tab("log")


run_case("ADR-100: 籌碼分頁四檢視/紅漲綠跌/切頁不下載/不重複抓取", _chips_tab_and_views)


def _chips_as_strategy_condition():
    """【ADR-101】籌碼條件接進策略:GUI 端要能從本地讀籌碼併進 df,
    且未來函數防護 (T日只讀T-1日) 在 GUI 這條路上同樣生效。"""
    import tempfile as _tf
    import pandas as _pd
    from data import chips_store as _chipstore
    from core import chips_features as _cf
    from core import strategy_engine as _se

    old_base = app.CHIPS_BASE_DIR
    tmp = _tf.mkdtemp()
    try:
        app.CHIPS_BASE_DIR = tmp
        idx = _pd.date_range('2026-07-01', periods=6, freq='D')
        # 每天外資買超遞增,方便驗證「讀到的是前一日」
        rows = [{'Date': d.strftime('%Y-%m-%d'), 'Code': '2330', 'Name': '台積電',
                 'Foreign': 1000 * (i + 1), 'Trust': 0, 'Dealer': 0,
                 'InstTotal': 1000 * (i + 1)} for i, d in enumerate(idx)]
        for r in rows:
            _chipstore.upsert(_chipstore.stock_inst_path(tmp, r['Date']), _pd.DataFrame([r]))

        df = _pd.DataFrame({'Open': 100.0, 'High': 101.0, 'Low': 99.0,
                            'Close': 100.0, 'Volume': 1_000_000.0}, index=idx)
        s = _se.new_strategy()
        s.update({'name': '籌碼診斷', 'symbol': '2330', 'market': '台股',
                  'timeframe': '日K', 'qty': 1, 'direction': '做多',
                  'stop_loss_pct': 2.0,
                  'entry': [{'type': 'chip_foreign_buy_streak', 'params': {'n': 3}}]})
        assert _se.strategy_uses_chips(s), "應偵測到策略用了籌碼條件"

        out = app._qt_attach_chips(df, s, cache_sym='2330', cache_market='台股')
        col = out[_cf.COL_FOREIGN]
        assert _pd.isna(col.iloc[0]), "第一根沒有前一日籌碼,應為 NaN"
        assert col.iloc[1] == 1000, f"第二根應讀到第一天的 1000,實際 {col.iloc[1]}"
        assert col.iloc[3] == 3000, f"第四根應讀到第三天的 3000 (不可是當日 4000),實際 {col.iloc[3]}"

        # 進階選項開啟才讀當日
        s2 = dict(s); s2['chips_allow_same_day'] = True
        out2 = app._qt_attach_chips(df, s2, cache_sym='2330', cache_market='台股')
        assert out2[_cf.COL_FOREIGN].iloc[0] == 1000, "允許當日時第一根應讀到當日籌碼"

        # 條件在 GUI 併好的 df 上能正確評估
        assert _se.CONDITIONS['chip_foreign_buy_streak'][2](out, {'n': 3}) is True

        # 分K 策略用籌碼條件應被擋下
        s3 = dict(s); s3['timeframe'] = '5分K'
        ok3, why3 = _se.validate_strategy(s3)
        assert not ok3 and '籌碼條件只能用於' in why3, f"分K應被擋下,實際: {ok3} {why3}"
    finally:
        app.CHIPS_BASE_DIR = old_base


run_case("ADR-101: 籌碼條件接策略/未來函數防護/分K擋下", _chips_as_strategy_condition)


def _sr_levels_drawn_on_chart():
    """【ADR-102】量價支撐壓力:開啟後主圖真的畫出水平線、顏色分壓力紅支撐綠、
    兩種區間模式都可運作、計算失敗不可影響 K 線圖本身。

    這裡用真的 matplotlib Axes (見 diag_mock_tkinter 的假 mplfinance),所以
    ax.lines 是真實的 artist——不是空殼斷言 (P-28 教訓)。"""
    import numpy as _np
    import pandas as _pd
    from core import volume_profile as _vp

    rng = _np.random.RandomState(11)
    rows = []
    for i in range(220):
        if i % 3 == 0:
            base, vol = 104.5, 5_000_000      # 刻意製造成交密集區
        else:
            base, vol = 100 + rng.rand() * 10, 600_000
        rows.append({'Open': base, 'High': base + 0.6, 'Low': base - 0.6,
                     'Close': base, 'Volume': vol})
    df = _pd.DataFrame(rows, index=_pd.date_range('2026-01-01', periods=220, freq='B'))

    old_sym, old_at = app.current_symbol, app.asset_type
    try:
        app.current_symbol, app.asset_type = '2330', 'stock'
        # 【ADR-120】支撐壓力現在是【盤勢判斷】底下的子項:總開關 + 子項
        # 都要開才會畫。總開關開著但子項關掉,一樣不可以畫出線來。
        app.regime_enabled_var.set(True)

        # 子項關閉時不應有任何支撐壓力線
        app.sr_enabled_var.set(False)
        app.draw_chart(df)
        assert app._sr_last_result is None, "支撐壓力子項關閉時不該計算"

        # 總開關關閉時,即使子項是開的也不該計算 (ADR-120)
        app.sr_enabled_var.set(True)
        app.regime_enabled_var.set(False)
        app.draw_chart(df)
        assert app._sr_last_result is None, "【盤勢判斷】總開關關閉時不該計算支撐壓力"
        app.regime_enabled_var.set(True)

        # 兩個都開才畫出水平線
        app.draw_chart(df)
        r = app._sr_last_result
        assert r and r['levels'], "開啟後應算出支撐壓力點位"
        ax = app.axlist[0]
        ys = {round(float(l.get_ydata()[0]), 4) for l in ax.lines
              if len(set(l.get_ydata())) == 1}
        for lv in r['levels']:
            assert round(lv['price'], 4) in ys, f"點位 {lv['price']} 沒有被畫成水平線"

        # 壓力紅、支撐綠 (鐵則1 的延伸)
        colors = {}
        for l in ax.lines:
            yd = l.get_ydata()
            if len(set(yd)) == 1:
                colors.setdefault(round(float(yd[0]), 4), l.get_color())
        for lv in r['levels']:
            c = str(colors.get(round(lv['price'], 4), '')).upper()
            if lv['role'] == _vp.ROLE_RESISTANCE:
                assert c == '#FF1744', f"壓力 {lv['price']} 應為紅色,實際 {c}"
            elif lv['role'] == _vp.ROLE_SUPPORT:
                assert c == '#00E676', f"支撐 {lv['price']} 應為綠色,實際 {c}"

        # POC 應落在刻意製造的密集區附近
        assert abs(r['profile']['poc'] - 104.5) < 2.0, \
            f"POC 應接近成交密集區 104.5,實際 {r['profile']['poc']}"

        # 兩種區間模式都要能運作 (ADR-120 後改由 regime_settings 決定)
        app.regime_settings['sr_range_mode'] = app.SR_RANGE_FIXED
        app.draw_chart(df)
        assert app._sr_last_result and app._sr_last_result['levels'], "固定N根模式應可運作"
        app.regime_settings['sr_range_mode'] = app.SR_RANGE_VISIBLE
        app.draw_chart(df)
        assert app._sr_last_result and app._sr_last_result['levels'], "可見範圍模式應可運作"

        # 計算失敗時不可影響 K 線圖 (支撐壓力只是輔助資訊)
        orig = _vp.find_levels
        try:
            stock_app_pro.volume_profile.find_levels = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError('診斷用假錯誤'))
            app.draw_chart(df)          # 不可拋例外
            assert app.current_fig is not None, "支撐壓力出錯時 K 線圖仍應正常畫出"
        finally:
            stock_app_pro.volume_profile.find_levels = orig
    finally:
        app.sr_enabled_var.set(False)
        app.regime_enabled_var.set(False)
        app.current_symbol, app.asset_type = old_sym, old_at


run_case("ADR-102: 量價支撐壓力畫線/壓力紅支撐綠/兩種區間/失敗不影響K線", _sr_levels_drawn_on_chart)


def _screener_end_to_end():
    """【ADR-103】選股:切分頁不下載、基本面門檻生效、虧損股不可通過本益比條件、
    技術面重用策略條件、結果可填表。"""
    import tempfile as _tf
    import pandas as _pd
    import numpy as _np
    from data import market_store as _mkt

    net = []
    orig_json = app._chips_http_json
    old_base = app.SCREENER_BASE_DIR
    tmp = _tf.mkdtemp()
    try:
        app._chips_http_json = lambda *a, **k: net.append('json')
        app.SCREENER_BASE_DIR = tmp

        app.set_bottom_tab("screener")
        assert not net, f"切到選股分頁不應發出網路請求,實際 {net}"

        # 準備基本面:一檔好股、一檔虧損股(本益比無資料)
        fund = _pd.DataFrame([
            {'Code': '2330', 'Name': '台積電', 'Close': 100.0, 'PE': 12.0, 'PB': 1.2,
             'YieldPct': 6.0, 'EPS': 8.0, 'GrossMarginPct': 50.0, 'RevenueYoYPct': 30.0,
             'RevenueMoMPct': 1.0, 'MonthRevenue': 1.0, 'Equity': 1.0, 'ROEPct': 12.0},
            {'Code': '9999', 'Name': '虧損股', 'Close': 10.0, 'PE': None, 'PB': 0.5,
             'YieldPct': 0.0, 'EPS': None, 'GrossMarginPct': None, 'RevenueYoYPct': -10.0,
             'RevenueMoMPct': 0.0, 'MonthRevenue': 1.0, 'Equity': 1.0, 'ROEPct': None},
        ])
        _mkt.save_fundamental(tmp, fund)

        # 全市場日K:2330 持續上漲
        idx = _pd.date_range('2026-01-01', periods=60, freq='B')
        rows = []
        for i, d in enumerate(idx):
            c = 100.0 + i
            rows.append({'Date': d.strftime('%Y-%m-%d'), 'Code': '2330', 'Name': '台積電',
                         'Open': c, 'High': c, 'Low': c, 'Close': c, 'Volume': 1000.0})
        _mkt.upsert_daily(tmp, _pd.DataFrame(rows))

        # 只用基本面:本益比<=15 → 虧損股(無本益比)絕不可入選
        app._sc_entries['pe'][0].delete(0, 'end'); app._sc_entries['pe'][0].insert(0, '15')
        conds = app._sc_collect_fundamental_conds()
        assert conds and conds[0]['field'] == 'pe', f"應收集到本益比條件,實際 {conds}"
        from core import market_screener as _ms
        res = _ms.screen(fund, None, fundamental_conds=conds)
        codes = [r['code'] for r in res['rows']]
        assert '2330' in codes, "符合條件的股票應入選"
        assert '9999' not in codes, "本益比無資料的虧損股絕不可通過本益比條件"

        # 技術面:重用策略引擎條件
        daily = _mkt.load_daily_range(tmp, '2026-01-01', '2026-12-31')
        assert daily is not None and len(daily) == 60, "日K應可讀回"
        res2 = _ms.screen(fund, daily,
                          conditions=[{'type': 'price_above_ma',
                                       'params': {'n': 20, 'kind': 'SMA'}}],
                          fundamental_conds=[],
                          to_ohlcv=_mkt.to_ohlcv_frame)
        assert '2330' in [r['code'] for r in res2['rows']], "持續上漲應通過站上20MA"

        # 填表
        app._sc_fill_tree(res2)
        assert len(app.tree_sc.get_children()) == len(res2['rows']), "結果應填入表格"

        # 範本都要是有效條件
        for name, p in _ms.PRESETS.items():
            for c in p.get('conditions', []):
                assert c['type'] in stock_app_pro.strategy_engine.CONDITIONS, \
                    f"範本 {name} 用了不存在的條件"

        assert not net, "整個選股流程都不該發出網路請求 (資料來自本地)"
    finally:
        app._chips_http_json = orig_json
        app.SCREENER_BASE_DIR = old_base
        try:
            app._sc_entries['pe'][0].delete(0, 'end')
        except Exception:
            pass
        app.set_bottom_tab("log")


run_case("ADR-103: 選股/基本面門檻/虧損股不誤選/技術面重用/切頁不下載", _screener_end_to_end)


def _screener_industry_and_backtest():
    """【ADR-105/106】產業篩選 + 選股回測:GUI 端能跑完整流程,
    且回測的未來函數防護在 GUI 這條路同樣生效。"""
    import tempfile as _tf
    import pandas as _pd
    import numpy as _np
    from data import market_store as _mkt
    from core import market_screener as _ms

    old_base = app.SCREENER_BASE_DIR
    tmp = _tf.mkdtemp()
    net = []
    orig_json = app._chips_http_json
    try:
        app._chips_http_json = lambda *a, **k: net.append('x')
        app.SCREENER_BASE_DIR = tmp

        # 12 檔 × 80 天:前 4 檔上漲
        rng = _np.random.RandomState(5)
        idx = _pd.date_range('2026-01-01', periods=80, freq='B')
        rows = []
        for k in range(12):
            px, trend = 100.0, (1.0 if k < 4 else -0.8)
            for d in idx:
                px = max(5.0, px + trend + rng.randn() * 0.15)
                rows.append({'Date': d.strftime('%Y-%m-%d'), 'Code': f'{1000+k}',
                             'Name': f'股{k}', 'Open': px, 'High': px*1.01,
                             'Low': px*0.99, 'Close': px, 'Volume': 1e6})
        _mkt.upsert_daily(tmp, _pd.DataFrame(rows))
        fund = _pd.DataFrame([{
            'Code': f'{1000+k}', 'Name': f'股{k}',
            'Industry': ('半導體業' if k < 4 else '水泥工業'),
            'Close': 100.0, 'PE': 10.0, 'PB': 1.0, 'YieldPct': 5.0, 'EPS': 2.0,
            'GrossMarginPct': 30.0, 'RevenueYoYPct': 20.0, 'RevenueMoMPct': 1.0,
            'MonthRevenue': 1.0, 'Equity': 1.0, 'ROEPct': 10.0} for k in range(12)])
        _mkt.save_fundamental(tmp, fund)

        # --- 產業篩選 (ADR-105) ---
        app.set_bottom_tab("screener")
        app._sc_refresh_industries()
        vals = list(app.cb_sc_industry['values'])
        assert '半導體業' in vals and '水泥工業' in vals, f"產業下拉未填入,實際 {vals}"
        assert vals[0] == app.SC_INDUSTRY_ALL, "第一項應是「全部產業」"
        r = _ms.screen(fund, None, industries=['半導體業'])
        assert {x['code'] for x in r['rows']} == {'1000','1001','1002','1003'}, \
            "產業篩選結果不正確"
        assert r['rows'][0]['industry'] == '半導體業', "結果應帶產業別"

        # --- 選股回測 (ADR-106) ---
        daily = _mkt.load_daily_range(tmp, '2026-01-01', '2026-12-31')
        from core import screener_backtest as _sb
        res = _sb.run_screener_backtest(
            daily, conditions=[{'type': 'price_above_ma', 'params': {'n': 20}}],
            fundamental_df=fund,
            fundamental_conds=[{'field': 'pe', 'op': '<=', 'value': 15}],
            rebalance_days=10, top_n=5, min_bars=25,
            to_ohlcv=_mkt.to_ohlcv_frame)
        assert res['fundamental_skipped'] is True, \
            "基本面只有當前快照,回測預設必須略過 (否則是未來函數)"
        assert res['has_lookahead'] is False
        assert res['periods'], "應該要有調倉期"
        for p in res['periods']:
            assert p['entry_date'] > p['signal_date'], "進場日必須晚於訊號日"
        picked = {h['code'] for h in res['holdings']}
        assert picked == {'1000','1001','1002','1003'}, f"應選中上漲股,實際 {sorted(picked)}"

        # GUI 顯示不可拋例外,且要能標示紅綠
        app._sc_show_backtest(res, 10, 25)
        assert len(app.tree_sc.get_children()) == len(res['periods']), "每期都要顯示一列"
        tags = [app.tree_sc.item(i, 'tags') for i in app.tree_sc.get_children()]
        assert any('buy' in tuple(t) or 'sell' in tuple(t) for t in tags), \
            "本期損益應依紅漲綠跌上色"

        assert not net, "整個流程都不該發出網路請求 (資料全在本地)"
    finally:
        app._chips_http_json = orig_json
        app.SCREENER_BASE_DIR = old_base
        app.set_bottom_tab("log")


run_case("ADR-105/106: 產業篩選 + 選股回測/未來函數防護/紅綠上色", _screener_industry_and_backtest)


def _telegram_remote_control():
    """【ADR-108】手機遠端控制:授權、二次確認、啟用/停用策略的完整路徑。

    這條路能讓一個「不在電腦前的人」改變會真實下單的系統狀態,所以每個
    安全關卡都要在 GUI 這條路上實測,不能只測 core 的純函式。
    """
    from core import strategy_engine as _se
    from core import paper_account as _pa

    sent = []
    orig_reply = app._tg_reply
    orig_cfg = getattr(app, 'telegram_cfg', None)
    orig_strats = app.strategies
    orig_running = app._qt_running
    orig_save = app._qt_save
    orig_save_state = app._qt_save_state
    orig_accts = app.paper_accts
    orig_rts = app.strategy_runtimes
    try:
        app._tg_reply = lambda t: sent.append(str(t))
        app._qt_save = lambda: None          # 診斷不要動到使用者的策略檔
        app._qt_save_state = lambda: None
        # 前面的案例會在共用帳戶留下部位,會誤觸「持倉核對」而擋下啟用。
        # 這裡用乾淨帳戶,持倉核對本身在下面第 7 項單獨測。
        app.paper_accts = {_pa.DEFAULT_ACCOUNT_ID:
                           _pa.new_account(account_id=_pa.DEFAULT_ACCOUNT_ID)}
        app.strategy_runtimes = {}
        app.telegram_cfg = {'bot_token': '123:ABC', 'chat_id': '999888',
                            'enabled': True, 'remote_control': True}
        app._tg_pending = None
        app._qt_running = False
        s = {'id': 'tg1', 'name': '遠端測試策略', 'enabled': False, 'mode': '模擬',
             'symbol': '2330', 'timeframe': '日K', 'direction': '做多', 'qty': 1,
             'account_id': 'default', 'stop_loss_pct': 3.0,
             'entry': [{'type': 'ma_cross_up', 'params': {'fast': 5, 'slow': 20}}],
             'exit_signals': []}
        ok, why = _se.validate_strategy(s)
        assert ok, f"測試策略本身要是合法的,否則測不到後面的路徑:{why}"
        app.strategies = [s]

        # --- 1. 未授權的 chat_id 一律無效,且不回覆 (不確認 Bot 存在) ---
        sent.clear()
        app._tg_handle_command('123456', '/stop_all')
        assert not sent, "未授權的指令不該有任何回覆"
        app._tg_handle_command('123456', '/on 1')
        assert s['enabled'] is False and not sent, "未授權的指令絕不可改變任何狀態"

        # --- 2. 唯讀指令 ---
        sent.clear()
        app._tg_handle_command('999888', '/status')
        assert '系統狀態' in sent[-1]
        app._tg_handle_command('999888', '/list')
        assert '遠端測試策略' in sent[-1]
        app._tg_handle_command('999888', '/positions')
        assert '實單庫存' in sent[-1], "持倉回覆必須講明看不到實單"
        app._tg_handle_command('999888', '/pnl')
        assert '模擬帳戶' in sent[-1]
        app._tg_handle_command('999888', '/help')
        assert '不提供下單' in sent[-1]
        assert s['enabled'] is False, "唯讀指令不可改變任何狀態"

        # --- 3. 啟用策略必須二次確認 ---
        sent.clear()
        app._tg_handle_command('999888', '/on 1')
        assert s['enabled'] is False, "只下 /on 不該直接啟用"
        assert '/yes' in sent[-1] and '遠端測試策略' in sent[-1]
        assert '2330' in sent[-1], "確認訊息要講明啟用的是哪一個標的"
        code = app._tg_pending['code']

        # 錯的確認碼不放行
        app._tg_handle_command('999888', '/yes ZZZZ')
        assert s['enabled'] is False, "確認碼錯誤仍被啟用 = 安全破口"

        # 正確的確認碼才生效
        app._tg_handle_command('999888', f'/yes {code}')
        assert s['enabled'] is True, "正確確認碼後應啟用"
        assert '已啟用' in sent[-1]
        assert '總開關目前是關閉' in sent[-1], "總開關沒開時要提醒策略不會實際運作"
        assert app._tg_pending is None, "確認碼用過就要作廢"

        # 用過的碼不能重播
        s['enabled'] = False
        app._tg_handle_command('999888', f'/yes {code}')
        assert s['enabled'] is False, "確認碼被重播成功 = 安全破口"
        s['enabled'] = True

        # --- 4. 停用不需確認 (往安全方向,不可拖延) ---
        sent.clear()
        app._tg_handle_command('999888', '/off 1')
        assert s['enabled'] is False, "/off 應立刻生效,不需確認"
        assert app._tg_pending is None

        # --- 5. 總開關:開需確認、關不需要 ---
        s['enabled'] = True
        sent.clear()
        app._tg_handle_command('999888', '/start_all')
        assert app._qt_running is False, "/start_all 不該直接開啟總開關"
        assert '/yes' in sent[-1] and '模擬 1' in sent[-1]
        app._tg_handle_command('999888', '/yes ' + app._tg_pending['code'])
        assert app._qt_running is True, "確認後應開啟總開關"

        sent.clear()
        app._tg_handle_command('999888', '/stop_all')
        assert app._qt_running is False, "/stop_all 必須立刻關閉,不需確認"

        # --- 6. 過期的確認碼失效 ---
        s['enabled'] = False
        app._tg_handle_command('999888', '/on 1')
        app._tg_pending['expire'] = time.time() - 1
        app._tg_handle_command('999888', '/yes ' + app._tg_pending['code'])
        assert s['enabled'] is False, "過期確認碼仍生效 = 安全破口"
        assert app._tg_pending is None, "過期的待確認指令要丟掉,不可一直掛著"

        # --- 7. 設定不完整的策略,遠端也不得啟用 (與畫面同一套檢查) ---
        bad = dict(s); bad['id'] = 'tg2'; bad['name'] = '壞策略'
        bad['entry'] = []; bad['enabled'] = False
        app.strategies = [bad]
        sent.clear()
        app._tg_handle_command('999888', '/on 1')
        assert bad['enabled'] is False and app._tg_pending is None, \
            "設定不合格的策略不該進到確認階段"
        assert '❌' in sent[-1]

        # --- 7b. 持倉狀態與模擬帳戶對不上時,遠端不得啟用 (手機上做不了核對) ---
        s['enabled'] = False
        app.strategies = [s]
        acct = app.paper_accts[_pa.DEFAULT_ACCOUNT_ID]
        _pa.apply_fill(acct, '2026-07-26 09:05:00', '台股', '2330',
                       '買進', 'OPEN', 1, 600.0)   # 帳戶有倉、策略以為 FLAT
        sent.clear()
        app._tg_handle_command('999888', '/on 1')
        assert '持倉核對' in sent[-1], f"應直接擋下並說明原因,實際:{sent[-1]}"
        assert app._tg_pending is None, "註定失敗的操作不該還要使用者確認"
        app._tg_handle_command('999888', '/yes ' + (app._tg_pending or {}).get('code', 'X'))
        assert s['enabled'] is False, "持倉對不上仍被遠端啟用 = 起點就錯的策略"
        acct['positions'].clear()

        # --- 8. 沒有任何下單指令 ---
        sent.clear()
        for bad_cmd in ('/buy 2330 1', '/sell 2330 1', '/order 2330'):
            app._tg_handle_command('999888', bad_cmd)
        assert not sent, "遠端介面不得存在任何下單指令 (鐵則14)"

        # --- 9. 沒開遠端控制時,輪詢執行緒不會做事 ---
        app.telegram_cfg = dict(app.telegram_cfg, remote_control=False)
        assert app._tg_control_enabled() is False
        app.telegram_cfg = dict(app.telegram_cfg, remote_control=True, bot_token='')
        assert app._tg_control_enabled() is False, "token 沒填也不能啟動遠端控制"
    finally:
        app._tg_reply = orig_reply
        app.telegram_cfg = orig_cfg
        app.strategies = orig_strats
        app._qt_running = orig_running
        app._qt_save = orig_save
        app._qt_save_state = orig_save_state
        app.paper_accts = orig_accts
        app.strategy_runtimes = orig_rts
        app._tg_pending = None


run_case("ADR-108: Telegram 遠端控制授權/二次確認/啟用停用策略", _telegram_remote_control)


def _volume_axis_starts_at_zero():
    """【ADR-109】成交量副圖的 Y 軸必須從 0 起算。

    這條原本有寫,但因為 auto_scale_indicator_panels 被重複定義了兩次、
    後面那份把前面那份整個蓋掉而靜默失效 (量能長條被從底部裁掉一截)。
    修好之後補這個案例,讓它再壞掉時會被抓到而不是又靜悄悄地錯。
    """
    class _Ax:
        def __init__(s): s.ylim = None
        def set_ylim(s, lo, hi): s.ylim = (float(lo), float(hi))

    old_df, old_axl = app.plot_df, getattr(app, 'axlist', None)
    old_ap = getattr(app, 'active_panels', None)
    old_pc = getattr(app, 'panel_columns', None)
    try:
        n = 40
        idx = pd.date_range('2026-01-01', periods=n, freq='D')
        # 量刻意「全部都很大且彼此接近」(80萬~100萬):最小值遠離 0,
        # 若用一般副圖的 low-padding 當下限,長條就會被從底部裁掉。
        vol = np.linspace(800000, 1000000, n)
        macd = np.linspace(-5, 5, n)
        app.plot_df = pd.DataFrame({
            'Open': np.linspace(100, 110, n), 'High': np.linspace(101, 111, n),
            'Low': np.linspace(99, 109, n), 'Close': np.linspace(100, 110, n),
            'Volume': vol, 'MACD': macd}, index=idx)
        ax_vol, ax_macd = _Ax(), _Ax()
        # axlist 的排列是 panel_index * 2 (mplfinance 每個 panel 有主/次兩個軸)
        app.axlist = [_Ax(), _Ax(), ax_vol, _Ax(), ax_macd, _Ax()]
        app.active_panels = {'Volume': 1, 'MACD': 2}
        app.panel_columns = {'Volume': ['Volume'], 'MACD': ['MACD']}

        app.auto_scale_indicator_panels(0, n)

        assert ax_vol.ylim is not None, "成交量副圖的 Y 軸沒有被設定"
        lo, hi = ax_vol.ylim
        assert lo == 0.0, f"成交量 Y 軸下限必須是 0,實際 {lo} (長條會被從底部裁掉)"
        assert hi >= float(vol.max()), f"成交量 Y 軸上限要容得下最大量,實際 {hi}"

        # 其他副圖不適用「從 0 起算」:MACD 有負值,硬從 0 起算會看不到負的那半
        assert ax_macd.ylim is not None
        mlo, mhi = ax_macd.ylim
        assert mlo < float(macd.min()) and mhi > float(macd.max()), \
            "一般副圖仍應依視角內極值上下留白"
        assert mlo < 0, "MACD 有負值,Y 軸不可從 0 起算"

        # 主圖:視角內價格完全沒波動時,仍要給得出一個範圍 (漲停鎖死/單根K棒)
        flat = app.plot_df.copy()
        flat['High'] = flat['Low'] = flat['Close'] = flat['Open'] = 100.0
        app.plot_df = flat
        ax_main = _Ax()
        app.auto_scale_y(ax_main, 0, n)
        assert ax_main.ylim is not None, "價格完全沒波動時仍要設定 Y 軸,否則K棒會被壓成一條線"
        assert ax_main.ylim[0] < 100.0 < ax_main.ylim[1]
    finally:
        app.plot_df = old_df
        app.axlist = old_axl
        app.active_panels = old_ap
        app.panel_columns = old_pc


run_case("ADR-109: 成交量Y軸從0起算 + 無波動時仍給範圍 (重複定義修正)",
         _volume_axis_starts_at_zero)

def _order_intent_preserves_shioaji_order():
    """【ADR-110 階段1】委託抽象化必須是「零行為改變」。

    這個案例的驗收標準只有一條:**送給 shioaji 的 Order,每一個欄位都跟
    重構前一模一樣**。下面的期望值是照著重構前 `_place_strategy_order()`
    那段程式碼逐行抄出來的硬編碼常數,不是從新程式反推的——若從新程式
    反推,這個測試就只是在確認「新程式等於新程式」,證明不了任何事。
    """
    class _RecOrder:
        def __init__(s, **kw):
            s.kw = dict(kw)

    class _Trade:
        class status:
            status = 'Submitted'

    class _FakeApi:
        def __init__(s):
            s.sent = []
        def Order(s, **kw):
            return _RecOrder(**kw)
        def place_order(s, contract, order):
            s.sent.append((contract, order))
            return _Trade()

    broker = app.brokers['sinopac']
    orig_api = broker.api
    fake = _FakeApi()
    broker.api = fake
    try:
        def _mk(tt, ptype, action='買進', symbol='2330', price=600.0, qty=2, ticks=2):
            s = {'id': 'oi1', 'name': '委託抽象化測試', 'symbol': symbol,
                 'trade_type': tt, 'price_type': ptype, 'slippage_ticks': ticks,
                 'qty': qty, 'mode': '實單'}
            intent = {'action': action, 'qty': qty, 'price': price}
            asset = 'future' if tt == '期貨' else 'stock'
            ok, msg = app._place_strategy_order(s, intent, object(), asset)
            assert ok, f"送單應成功,實際:{msg}"
            return fake.sent[-1][1].kw, msg

        # --- 1. 股票 / 限價:600 買進讓 2 檔,500~1000 的檔位是 1 元 → 602 ---
        kw, msg = _mk('股票', '限價')
        assert kw == {'price': 602.0, 'quantity': 2, 'action': 'Buy',
                      'price_type': 'LMT', 'order_type': 'ROD',
                      'order_lot': 'Common', 'order_cond': 'Cash'}, kw
        assert '限價602' in msg, msg

        # --- 2. 股票 / 市價:價格一律送 0,但讓價後的價仍要出現在訊息裡嗎? ---
        # 重構前市價的 label 就只是「市價」(不帶價格),這裡確認沒有變。
        kw, msg = _mk('股票', '市價')
        assert kw['price'] == 0.0 and kw['price_type'] == 'MKT', kw
        assert '市價 x' in msg and '限價' not in msg, msg

        # --- 3. 賣出要往下讓價 (讓錯方向 = 掛一個不可能成交的價) ---
        kw, _ = _mk('股票', '限價', action='賣出')
        assert kw['price'] == 598.0 and kw['action'] == 'Sell', kw

        # --- 4. 零股:IntradayOdd,且【鐵則6】強制限價 ---
        kw, _ = _mk('零股', '限價', qty=100)
        assert kw == {'price': 602.0, 'quantity': 100, 'action': 'Buy',
                      'price_type': 'LMT', 'order_type': 'ROD',
                      'order_lot': 'IntradayOdd', 'order_cond': 'Cash'}, kw
        # 就算策略被改成市價,零股也必須退回限價,不可送市價給券商
        kw, _ = _mk('零股', '市價', qty=100)
        assert kw['price_type'] == 'LMT' and kw['price'] == 602.0, \
            f"零股送出市價單 = 違反鐵則6,實際 {kw}"

        # --- 5. 期貨:另一組常數,且沒有 order_lot/order_cond 欄位 ---
        kw, _ = _mk('期貨', '限價', symbol='TXFR1', price=18000.0)
        assert kw == {'price': 18002.0, 'quantity': 2, 'action': 'Buy',
                      'price_type': 'LMT', 'order_type': 'ROD'}, kw
        kw, _ = _mk('期貨', '範圍市價', symbol='TXFR1', price=18000.0)
        assert kw['price'] == 0.0 and kw['price_type'] == 'MKP', kw

        # --- 6. 檔位跨價格帶時要用對的 tick (鐵則7) ---
        # 99.9 元買進讓 2 檔:<100 的檔位是 0.1 → 100.1 (不是 101.9)
        kw, _ = _mk('股票', '限價', price=99.9)
        assert abs(kw['price'] - 100.1) < 1e-9, f"檔位算錯,實際 {kw['price']}"

        # --- 7. 看A做B:exec_price 帶進來時要用 B 的價,不是訊號商品的價 ---
        s = {'id': 'oi2', 'name': 'AB', 'symbol': '2330', 'trade_type': '股票',
             'price_type': '限價', 'slippage_ticks': 0, 'qty': 1, 'mode': '實單'}
        app._place_strategy_order(s, {'action': '買進', 'qty': 1, 'price': 999.0},
                                  object(), 'stock', exec_price=600.0)
        assert fake.sent[-1][1].kw['price'] == 600.0, \
            "看A做B時應以 B 的 exec_price 為準"

        # --- 8. 策略沒設 broker 欄位時要落到永豐,不能變成「找不到券商」---
        assert app._broker_key_of({}) == 'sinopac'
        assert app._broker_key_of({'broker': 'kgi'}) == 'kgi'
        # 'mega' (兆豐) 還沒接 adapter:指定一家沒註冊的券商要明確報錯,
        # 而且訊息要講出是哪一家 (用 kgi 測不到這件事——它已經註冊了)。
        ok, msg = app._place_strategy_order(
            {'id': 'x', 'symbol': '2330', 'trade_type': '股票', 'price_type': '限價',
             'qty': 1, 'broker': 'mega'},
            {'action': '買進', 'qty': 1, 'price': 600.0}, object(), 'stock')
        assert not ok and 'mega' in msg, f"未接上的券商應明確報錯,實際:{msg}"
    finally:
        broker.api = orig_api


run_case("ADR-110階段1: 委託抽象化後送出的 shioaji Order 逐欄位不變",
         _order_intent_preserves_shioaji_order)


def _strategy_level_account_routing():
    """【ADR-110 階段2】策略指定帳號要真的送到那個帳號。

    這個功能最嚴重的失效模式不是「送不出去」(那會報錯,看得見),而是
    **默默送到別的帳戶** —— 沒有錯誤訊息,對帳時才發現真錢跑錯戶頭。
    所以下面每一條都在確認「錯的時候會擋下來並說清楚」。
    """
    from core import order_intent as _oi

    class _Acc:
        def __init__(s, bid, aid, user):
            s.broker_id, s.account_id, s.username = bid, aid, user

    class _RecOrder:
        def __init__(s, **kw):
            s.kw = dict(kw)

    class _Trade:
        class status:
            status = 'Submitted'

    class _FakeApi:
        def __init__(s, accounts):
            s._accs = accounts
            s.sent = []
        def list_accounts(s):
            return list(s._accs)
        def Order(s, **kw):
            return _RecOrder(**kw)
        def place_order(s, contract, order):
            s.sent.append(order)
            return _Trade()

    a1 = _Acc('9A95', '1234567', '證券戶')
    a2 = _Acc('F031', '7654321', '期貨戶')
    broker = app.brokers['sinopac']
    orig_api = broker.api
    fake = _FakeApi([a1, a2])
    broker.api = fake
    try:
        def _s(**kw):
            s = {'id': 'r1', 'name': '路由測試', 'symbol': '2330', 'trade_type': '股票',
                 'price_type': '限價', 'slippage_ticks': 0, 'qty': 1, 'mode': '實單'}
            s.update(kw)
            return s

        def _send(s):
            return app._place_strategy_order(
                s, {'action': '買進', 'qty': 1, 'price': 600.0}, object(), 'stock')

        # --- 1. 帳號清單能列出來,id 用「分公司-帳號」(重登後不會變) ---
        accs = dict(broker.list_accounts())
        assert '9A95-1234567' in accs and 'F031-7654321' in accs, accs
        assert '證券戶' in accs['9A95-1234567'], accs

        # --- 2. 沒指定帳號 → 完全不帶 account 參數 (等同加功能之前) ---
        ok, _ = _send(_s())
        assert ok
        assert 'account' not in fake.sent[-1].kw, \
            f"沒指定帳號時不該帶 account,實際 {fake.sent[-1].kw}"

        # --- 3. 指定帳號 → 帶對的那個 Account 物件 ---
        ok, _ = _send(_s(broker_account='F031-7654321'))
        assert ok
        assert fake.sent[-1].kw.get('account') is a2, \
            "指定的帳號沒有被帶進委託,單會下到預設帳戶去"

        # --- 4. 指定了一個不存在的帳號 → 必須拒單,絕不可退回預設帳號 ---
        n = len(fake.sent)
        ok, msg = _send(_s(broker_account='XXXX-0000'))
        assert not ok, "找不到指定帳號卻仍然送出 = 真錢下錯戶頭"
        assert len(fake.sent) == n, "拒單時不可有任何委託被送出"
        assert 'XXXX-0000' in msg, f"錯誤訊息要指出是哪個帳號找不到,實際:{msg}"

        # --- 5. 指定一家還沒接上的券商 → 擋下並說清楚是哪一家 ---
        # 用 'mega' 而不是 'kgi':凱基已經有 adapter (ADR-111/112) 會真的
        # 去啟動子行程,那是 tests/test_brokers.py 的守備範圍,不是這裡。
        ok, msg = _send(_s(broker='mega'))
        assert not ok and 'mega' in msg, msg

        # --- 6. 畫面上要看得出「這張單會下到哪裡」---
        t = app._qt_live_target_text(_s(broker_account='F031-7654321'))
        assert '永豐金' in t and '期貨戶' in t, t
        assert '預設帳號' in app._qt_live_target_text(_s()), \
            "沒指定帳號時要明講是預設帳號,不能空白讓人以為沒設定好"
        # 設定了但現在找不到 → 要看得出異常,不可顯示成正常的預設帳號
        bad = app._qt_live_target_text(_s(broker_account='XXXX-0000'))
        assert '找不到' in bad and '預設帳號' not in bad, bad

        # --- 7b. 策略編輯器的「實單帳戶」下拉 ---
        choices = app._qt_live_account_choices()
        assert choices[0][0] == ('', ''), "第一項必須是「用券商預設帳號」"
        refs = [r for r, _l in choices]
        assert ('sinopac', 'F031-7654321') in refs, refs
        # 讀既有設定:選到的就是策略存的那一個
        get1 = app._qt_build_live_account_row(app, _s(broker_account='F031-7654321'))
        assert get1() == ('sinopac', 'F031-7654321'), get1()
        # 沒設定的策略要落在預設項 (不可自作主張挑第一個真實帳號)
        get2 = app._qt_build_live_account_row(app, _s())
        assert get2() == ('', ''), get2()
        # 設定了一個現在不存在的帳號:不可默默選到別的帳號
        get3 = app._qt_build_live_account_row(app, _s(broker_account='XXXX-0000'))
        assert get3() == ('', ''), \
            "找不到原設定時應退回預設項並在畫面上警示,不可選到別人的帳號"

        # --- 7c. 存進策略的欄位,build_live_order 讀得回來 (端到端) ---
        st = _s(); st['broker'], st['broker_account'] = ('sinopac', 'F031-7654321')
        oi = _oi.build_live_order(st, {'action': '買進', 'qty': 1, 'price': 600.0}, 'stock')
        assert oi['broker'] == 'sinopac' and oi['account'] == 'F031-7654321', oi

        # --- 7. sj_api 與 adapter.api 必須是同一個東西 (不可能不同步) ---
        assert app.sj_api is fake, "self.sj_api 應直接反映 adapter 的連線"
        probe = object()
        app.sj_api = probe
        assert broker.api is probe, "寫 self.sj_api 要同步寫進 adapter"
        broker.api = fake
        assert app.sj_api is fake, "寫 adapter 要同步反映到 self.sj_api"
    finally:
        broker.api = orig_api


run_case("ADR-110階段2: 策略層級帳號路由/找不到帳號拒單/連線不分岔",
         _strategy_level_account_routing)

def _shioaji_17_compat():
    """【ADR-114】shioaji 1.5.6 / 1.7 相容:同一份 GUI 程式碼兩版都要能解析
    指數與個股合約,而且 login 不會因為多傳參數而炸掉。

    指數解析錯了不會報錯,只會讓加權/櫃買指數安靜地不動作 —— 那是主圖與
    「看A做B」策略的核心,所以兩種版本形狀都要走一次真正的 GUI 路徑。
    """
    class _C:
        def __init__(s, code, symbol=None, name=''):
            s.code = code
            if symbol is not None:
                s.symbol = symbol
            s.name = name
            s.reference = 100.0

    class _Grp:            # 1.5.6:群組容器
        def __init__(s, items):
            for k, v in items.items():
                setattr(s, k, v)

    class _Cat:            # 1.7:ContractCategory,直接用代碼查
        def __init__(s, items): s._m = dict(items)
        def get(s, k, default=None): return s._m.get(k, default)
        def __getitem__(s, k): return s._m[k]
        def __iter__(s): return iter(s._m.values())

    from core import sj_compat

    broker = app.brokers['sinopac']
    orig_api = broker.api

    class _Api:
        def __init__(s, indexs, stocks):
            class _Cs: pass
            s.Contracts = _Cs()
            s.Contracts.Indexs = indexs
            s.Contracts.Stocks = stocks

    try:
        # --- 1.5.6 形狀 ---
        idx156 = _Grp({'TSE': _Grp({'TSE001': _C('TSE001', 'TSE001', '加權指數')}),
                       'OTC': _Grp({'OTC101': _C('OTC101', 'OTC101', '櫃買指數')})})
        stk156 = _Cat({'2330': _C('2330', '2330', '台積電')})
        broker.api = _Api(idx156, stk156)
        assert broker.index_contract('TSE').code == 'TSE001', "1.5.6 加權指數解析失敗"
        assert broker.index_contract('OTC').code == 'OTC101', "1.5.6 櫃買指數解析失敗"
        assert broker.stock_contract('2330').code == '2330', "1.5.6 個股解析失敗"

        # --- 1.7 形狀:代碼改 IX0001/IX0002,列舉合約只有 code (無 symbol) ---
        idx17 = _Cat({'IX0001': _C('IX0001', name='加權指數'),
                      'IX0002': _C('IX0002', name='櫃買指數')})
        stk17 = _Cat({'2330': _C('2330', name='台積電')})     # 刻意不給 symbol
        broker.api = _Api(idx17, stk17)
        assert broker.index_contract('TSE').code == 'IX0001', "1.7 加權指數解析失敗"
        assert broker.index_contract('OTC').code == 'IX0002', "1.7 櫃買指數解析失敗"
        assert broker.stock_contract('2330').code == '2330', "1.7 個股 .get 解析失敗"

        # 1.7 只有 code 的輕量型別,掃描比對也要找得到 (.get 失效時的退路)
        class _NoGet:
            def __init__(s, items): s._m = list(items)
            def __iter__(s): return iter(s._m)
        broker.api = _Api(idx17, _NoGet([_C('2317', name='鴻海')]))
        assert broker.stock_contract('2317').code == '2317', \
            "1.7 輕量型別 (無 symbol) 掃描比對失敗 → 會變成「查無此代碼」"

        # --- 找不到時回 None,不可拋例外 (未登入很常見) ---
        broker.api = _Api(_Cat({}), _Cat({}))
        assert broker.index_contract('TSE') is None
        assert broker.stock_contract('2330') is None

        # --- login:1.7 不吃 contracts_timeout,要自動略過而不是 TypeError ---
        class _Api17(_Api):
            def __init__(s):
                super().__init__(_Cat({}), _Cat({}))
                s.calls = []
            def login(s, api_key, secret_key, subscribe_trade=True,
                      receive_window=30000, force_refresh=False):
                s.calls.append({'api_key': api_key, 'secret_key': secret_key})
        a17 = _Api17(); broker.api = a17
        dropped = broker.login(api_key='k', secret_key='s', contracts_timeout=10000)
        assert a17.calls, "1.7 的 login 沒有被呼叫到"
        assert dropped == ['contracts_timeout'], f"應回報被略過的參數,實際 {dropped}"

        class _Api156(_Api):
            def __init__(s):
                super().__init__(_Cat({}), _Cat({}))
                s.calls = []
            def login(s, api_key, secret_key, contracts_timeout=10000):
                s.calls.append(contracts_timeout)
        a156 = _Api156(); broker.api = a156
        dropped = broker.login(api_key='k', secret_key='s', contracts_timeout=10000)
        assert a156.calls == [10000], "1.5.6 應照舊傳入 contracts_timeout"
        assert dropped == [], f"1.5.6 不該略過任何參數,實際 {dropped}"

        # --- 1.7 官方確認:Indexs.TSE["IX0001"] 這種「群組 + 新代碼」形狀 ---
        # 升級指南明載舊寫法 Indexs.TSE["001"] → 新寫法 Indexs.TSE["IX0001"],
        # 也就是 1.7 仍然保留交易所群組,只是代碼變了。這個形狀一定要能解析。
        idx_grp17 = _Grp({'TSE': _Cat({'IX0001': _C('IX0001', name='加權指數')}),
                          'OTC': _Cat({'IX0043': _C('IX0043', name='櫃買指數')})})
        broker.api = _Api(idx_grp17, _Cat({}))
        assert broker.index_contract('TSE').code == 'IX0001', \
            "官方指南的 Indexs.TSE['IX0001'] 形狀解析失敗"
        assert broker.index_contract('OTC').code == 'IX0043', \
            "櫃買 IX0043 解析失敗 —— 這是實際的櫃買指數代碼"

        # 先前推論過的代碼保留當保險 (沒中只是多試一次,不會壞)
        for _alt in ('IX0101', 'IX0002', 'OTC101'):
            broker.api = _Api(_Grp({'OTC': _Cat({_alt: _C(_alt)})}), _Cat({}))
            assert broker.index_contract('OTC').code == _alt, f"櫃買備選代碼 {_alt} 解析失敗"

        # 兩個代碼並存時要選對的那個 (IX0043),不可挑到舊的
        broker.api = _Api(_Grp({'OTC': _Cat({'IX0043': _C('IX0043'),
                                             'OTC101': _C('OTC101')})}), _Cat({}))
        assert broker.index_contract('OTC').code == 'IX0043', "並存時應優先取 IX0043"

        # --- 1.7 期貨合約沒有 symbol,主圖查詢要改用 code ---
        fut17 = _C('TXFR1', name='臺股期貨')          # 刻意不給 symbol
        assert sj_compat.contract_symbol(fut17) == 'TXFR1', \
            "FuturesInfo 無 symbol 時要退回 code,否則期貨查詢整個失效"
        # 上面只測到純函式。真正的 bug 是 GUI 裡「直接讀 contract.symbol」那一行,
        # 而那條路徑要有真連線才走得到——改用原始碼斷言守住:主程式裡不可再有
        # 裸讀 .symbol 的地方 (1.7 的 *Info 型別都沒有這個屬性)。
        import re as _re
        _src = open(stock_app_pro.__file__, encoding='utf-8').read()
        _bare = [m for m in _re.findall(r'\b\w+\.symbol\b', _src)
                 if not m.startswith(('s.', 's2.', 'oi.'))]
        assert not _bare, f"主程式仍有裸讀 .symbol 的地方 (1.7 會取不到): {sorted(set(_bare))}"

        # --- 策略層要認得新舊指數代碼 ---
        from core import strategy_engine as _se
        for code in ('TSE001', 'OTC101', 'IX0001', 'IX0043', 'IX0101', '^TWII'):
            assert _se.looks_like_index_symbol(code), f"{code} 應被認成指數"
        assert not _se.looks_like_index_symbol('2330')
    finally:
        broker.api = orig_api


run_case("ADR-114: shioaji 1.5.6/1.7 相容 (指數代碼/輕量合約/login參數)",
         _shioaji_17_compat)

def _screener_columns_and_accounts():
    """【ADR-117】使用者實機回報的三個問題。

    1. 跑過回測之後再選股,表頭還是回測的 → 12 個值塞進 7 欄,完全對不起來
    2. 結果出現字面上的 'nan'
    3. 實單帳戶下拉三個帳號長得一模一樣,無從選起
    """
    def heads():
        return [app.tree_sc.heading(c).get('text') if isinstance(app.tree_sc.heading(c), dict)
                else app.tree_sc.heading(c) for c in app.tree_sc['columns']]

    screen_res = {'rows': [{'code': '2330', 'name': '台積電', 'industry': '半導體業',
                            'close': 600.0, 'pe': 15.0, 'pb': 3.0, 'yield': 2.0,
                            'eps': 40.0, 'gross_margin': 55.0, 'revenue_yoy': 20.0,
                            'roe': 30.0, 'matched': ['測試']}],
                  'warnings': [], 'scanned': 1, 'passed': 1}
    bt_res = {'metrics': {'periods': 2, 'total_return_pct': 7.07, 'buy_hold_pct': 7.25,
                          'excess_pct': -0.18, 'max_drawdown_pct': 2.21,
                          'win_rate': 0.5, 'period_win_rate': 0.5, 'total_pnl': 66843,
                          'trades': 20, 'avg_picks': 10.0},
              'periods': [{'signal_date': '2026-04-07', 'entry_date': '2026-04-08',
                           'exit_date': '2026-05-06', 'picks': 10, 'pnl': -22123.0,
                           'return_pct': -2.21}],
              'holdings': [], 'warnings': [], 'fundamental_skipped': True,
              'has_lookahead': False}

    # --- 1. 選股 → 回測 → 再選股:表頭必須跟著切回來 ---
    app._sc_fill_tree(screen_res)
    h_screen = list(app.tree_sc['columns'])
    assert len(h_screen) == 12, f"選股應有 12 欄,實際 {len(h_screen)}"

    app._sc_show_backtest(bt_res, 20, 25)
    assert len(app.tree_sc['columns']) == 7, "回測應切成 7 欄"

    app._sc_fill_tree(screen_res)
    assert len(app.tree_sc['columns']) == 12, \
        "跑過回測後再選股,表頭沒有切回選股的 12 欄 —— 資料會塞錯欄位"
    assert list(app.tree_sc['columns']) == h_screen, "選股欄位不一致"

    # 資料列的欄位數要跟表頭一致 (對不起來的直接證據)
    for iid in app.tree_sc.get_children():
        vals = app.tree_sc.item(iid, 'values')
        assert len(vals) == len(app.tree_sc['columns']), \
            f"資料 {len(vals)} 欄 vs 表頭 {len(app.tree_sc['columns'])} 欄,對不起來"

    # 反向:回測後資料列也要對齊
    app._sc_show_backtest(bt_res, 20, 25)
    for iid in app.tree_sc.get_children():
        vals = app.tree_sc.item(iid, 'values')
        assert len(vals) == len(app.tree_sc['columns']), "回測的資料與表頭欄位數不符"

    # --- 2. 畫面上不可出現字面 'nan' ---
    from core import market_screener as _ms
    import numpy as _np
    fund = _pd_for_nan()
    r = _ms.screen(fund, None, conditions=[], fundamental_conds=[])
    app._sc_fill_tree(r)
    for iid in app.tree_sc.get_children():
        for v in app.tree_sc.item(iid, 'values'):
            assert str(v).strip().lower() != 'nan', f"表格出現 nan:{app.tree_sc.item(iid,'values')}"

    # --- 3. 三個帳戶必須看得出差別 ---
    class _AT:
        def __init__(s, n): s.name = n
    class _Acc:
        def __init__(s, t, aid, user='許super', signed=True):
            s.account_type = _AT(t); s.account_id = aid; s.username = user
            s.signed = signed; s.broker_id = '9A95'
    broker = app.brokers['sinopac']
    orig = broker.api
    class _Api:
        def list_accounts(s):
            return [_Acc('Stock', '1234567'), _Acc('Future', '7654321'),
                    _Acc('Intl', '9999999', signed=False)]
    try:
        broker.api = _Api()
        labels = [lab for _id, lab in broker.list_accounts()]
        assert len(set(labels)) == 3, f"三個帳戶顯示成一樣的字,無從選起:{labels}"
        assert any('證券' in l for l in labels), labels
        assert any('期貨' in l for l in labels), labels
        assert any('複委託' in l for l in labels), labels
        assert all(any(ch.isdigit() for ch in l) for l in labels), \
            "顯示文字一定要帶帳號 —— 種類與戶名都可能重複,只有帳號是唯一的"
        assert any('未簽署' in l for l in labels), "未簽署的帳戶要標示出來 (送單會被退)"
        ids = [i for i, _l in broker.list_accounts()]
        assert len(set(ids)) == 3, f"account_id 必須唯一:{ids}"
    finally:
        broker.api = orig


def _pd_for_nan():
    return pd.DataFrame([
        {'Code': '1591', 'Name': float('nan'), 'Industry': float('nan'),
         'Close': 41.6, 'PE': None, 'PB': None, 'YieldPct': None, 'EPS': None,
         'GrossMarginPct': None, 'RevenueYoYPct': None, 'MonthRevenue': None,
         'Equity': None, 'ROEPct': None},
        {'Code': '2072', 'Name': '世紀風電', 'Industry': '綠能環保', 'Close': 153.0,
         'PE': 11.71, 'PB': 1.26, 'YieldPct': 4.63, 'EPS': 13.0,
         'GrossMarginPct': 20.0, 'RevenueYoYPct': 5.0, 'MonthRevenue': 1.0,
         'Equity': 1.0, 'ROEPct': 10.0},
    ])


run_case("ADR-117: 選股欄位切換/nan/帳戶可辨識 (實機回報)",
         _screener_columns_and_accounts)
def _chips_screener_full_window():
    """【ADR-116】籌碼/選股的「開啟完整視窗」:搬出去、搬回來、內容不遺失。

    這兩個面板的 widget 直接掛在 self.*(單一實例設計),所以是用「搬家」而不是
    「複製」。搬家最容易出的錯是**搬完之後 self.* 指向已被銷毀的舊 widget**——
    畫面看起來還在,一按按鈕就炸,或是更新永遠不生效。
    """
    for kind, tab_attr, tree_attr in (('chips', 'chips_tab_frame', 'tree_chips'),
                                      ('screener', 'screener_tab_frame', 'tree_sc')):
        tab = getattr(app, tab_attr)
        tree_before = getattr(app, tree_attr)
        assert tree_before.winfo_exists(), f"{kind}:分頁的表格一開始應該是活的"

        # --- 開啟獨立視窗 ---
        win = app.open_panel_window(kind)
        assert win is not None and win.winfo_exists(), f"{kind}:視窗沒開起來"
        assert app.open_panel_window(kind) is win, \
            f"{kind}:重複開啟不可新建第二個視窗 (兩份 panel 會有一份變孤兒)"

        tree_in_win = getattr(app, tree_attr)
        assert tree_in_win is not tree_before, f"{kind}:panel 沒有真的搬到視窗裡"
        assert tree_in_win.winfo_exists(), f"{kind}:視窗裡的表格不是活的"
        assert not tree_before.winfo_exists(), \
            f"{kind}:分頁的舊表格應已銷毀 (留著會變成永遠不更新的孤兒)"

        # --- 關閉視窗:應搬回分頁 ---
        closer = win._bindings.get('WM_DELETE_WINDOW') if hasattr(win, '_bindings') else None
        closer = closer or app._panel_close_for_test(kind)
        closer()
        tree_back = getattr(app, tree_attr)
        assert tree_back.winfo_exists(), f"{kind}:搬回分頁後表格必須是活的"
        assert tree_back is not tree_in_win, f"{kind}:panel 沒有真的搬回分頁"
        assert not win.winfo_exists(), f"{kind}:視窗應已銷毀"
        assert kind not in getattr(app, '_panel_wins', {}), f"{kind}:視窗參照沒清掉"

        # --- 關掉之後還能再開一次 (第二輪不可壞) ---
        win2 = app.open_panel_window(kind)
        assert win2 is not win, f"{kind}:重開應該是新的視窗物件"
        app._panel_close_for_test(kind)()
        assert getattr(app, tree_attr).winfo_exists(), f"{kind}:第二輪搬回後表格要是活的"

    # --- 選股結果要跟著搬 ---
    res = {'rows': [{'code': '2330', 'name': '台積電', 'industry': '半導體業',
                     'close': 600.0, 'pe': 15.0, 'pb': 3.0, 'yield': 2.0,
                     'eps': 40.0, 'gross_margin': 55.0, 'revenue_yoy': 20.0,
                     'roe': 30.0, 'matched': ['測試條件']}],
           'warnings': [], 'scanned': 1, 'passed': 1}
    app._sc_fill_tree(res)
    n_before = len(app.tree_sc.get_children())
    assert n_before == 1, f"前置:結果應有 1 列,實際 {n_before}"
    app.open_panel_window('screener')
    assert len(app.tree_sc.get_children()) == n_before, \
        "搬到視窗後選股結果不見了 (使用者剛跑完的結果會白跑)"
    app._panel_close_for_test('screener')()
    assert len(app.tree_sc.get_children()) == n_before, \
        "搬回分頁後選股結果不見了"


run_case("ADR-116: 籌碼/選股 完整視窗 (搬出搬回/不重複開/結果不遺失)",
         _chips_screener_full_window)

def _chips_units_and_history():
    """【ADR-118】籌碼單位換算走 GUI 這條路,以及年數可設定。"""
    from data import chips_store as _cs2
    import tempfile as _tf2, os as _os2
    from core import unit_format as _uf

    old_base = app.CHIPS_BASE_DIR
    tmp = _tf2.mkdtemp()
    orig_view = app._chips_view.get()
    try:
        app.CHIPS_BASE_DIR = tmp

        # --- 大盤法人:元 → 億 ---
        mk = pd.DataFrame([{'Date': '2026-07-28', 'Foreign': -87484625299,
                            'Trust': 1611819893, 'Dealer': -31729733512,
                            'InstTotal': -117602538918}])
        _cs2.upsert(_cs2.market_inst_path(tmp), mk)
        app._chips_view.set('market')
        app._chips_refresh_view()
        heads = list(app.tree_chips['columns'])
        assert all('(億)' in h for h in heads if h != '日期'), f"表頭沒標億:{heads}"
        vals = app.tree_chips.item(app.tree_chips.get_children()[0], 'values')
        assert vals[1] == '-874.85', f"外資應為 -874.85 億,實際 {vals[1]}"
        assert vals[4] == '-1,176.03', f"三大法人合計換算錯誤:{vals[4]}"

        # --- 融資融券:仟元 → 億,且要有「融資增減」 ---
        mg = pd.DataFrame([
            {'Date': '2026-07-27', 'MarginBalance': 9272006, 'MarginPrevBalance': 9354810,
             'ShortBalance': 186233, 'ShortPrevBalance': 204754,
             'MarginAmountBalance': 568663454},
            {'Date': '2026-07-28', 'MarginBalance': 9096008, 'MarginPrevBalance': 9272006,
             'ShortBalance': 219259, 'ShortPrevBalance': 186233,
             'MarginAmountBalance': 545534811},
        ])
        _cs2.upsert(_cs2.margin_path(tmp), mg)
        app._chips_view.set('margin')
        app._chips_refresh_view()
        heads = list(app.tree_chips['columns'])
        assert '融資金額(億)' in heads, heads
        assert '融資增減(億)' in heads, f"缺少融資增減欄位:{heads}"
        # 最新一列在最上面 (日期新到舊)
        v0 = app.tree_chips.item(app.tree_chips.get_children()[0], 'values')
        assert v0[0] == '2026-07-28', v0
        assert v0[5] == '5,455.35', f"融資金額應為 5455.35 億,實際 {v0[5]}"
        # 545534811 - 568663454 = -23128643 仟元 = -231.29 億
        assert v0[6] == '-231.29', f"融資增減算錯,實際 {v0[6]} (應為 -231.29)"
        # 最舊那一列沒有前值 → 顯示 --
        v_last = app.tree_chips.item(app.tree_chips.get_children()[-1], 'values')
        assert v_last[6] == _uf.MISSING, f"最舊一期沒有前值,應顯示 --,實際 {v_last[6]}"

        # --- 個股法人:股 → 張 ---
        st = pd.DataFrame([{'Date': '2026-07-28', 'Code': '2610', 'Name': '華航',
                            'Foreign': 52784372, 'Trust': 664000, 'Dealer': 990333,
                            'InstTotal': 54438705}])
        _cs2.upsert(_cs2.stock_inst_path(tmp, '2026-07'), st)
        app._chips_view.set('stock')
        app.entry_chips_code.delete(0, 'end')
        app._chips_refresh_view()
        heads = list(app.tree_chips['columns'])
        assert any('(張)' in h for h in heads), f"表頭沒標張:{heads}"
        kids = app.tree_chips.get_children()
        if kids:
            v = app.tree_chips.item(kids[0], 'values')
            assert v[3] == '52,784', f"外資應為 52,784 張,實際 {v[3]}"
            assert v[6] == '54,439', f"三大法人合計應為 54,439 張,實際 {v[6]}"

        # --- 年數可設定 ---
        assert app._chips_years() == 1, "預設應為 1 年"
        app.cb_chips_years.current(3)          # '5 年'
        assert app._chips_years() == 5, f"選 5 年時應回 5,實際 {app._chips_years()}"
        app.cb_chips_years.current(0)

        assert app._sc_history_days() == 365, "選股預設應為 1 年"
        app.cb_sc_years.current(5)             # '10 年'
        assert app._sc_history_days() == 3650, f"選 10 年應為 3650 天,實際 {app._sc_history_days()}"
        app.cb_sc_years.current(1)
    finally:
        app.CHIPS_BASE_DIR = old_base
        app._chips_view.set(orig_view)


run_case("ADR-118: 籌碼單位(張/億)/融資增減/資料年數可設定",
         _chips_units_and_history)

def _stability_and_units_119():
    """【ADR-119】使用者實機回報的四項。"""
    from core import chips_parser as _cp
    from core import unit_format as _uf
    from data import chips_store as _cs3
    import tempfile as _tf3

    # --- 2. 「沒有資料」與「版面壞了」要分得開 ---
    assert _cp.verify_tpex_layout({})[0] == _cp.NO_DATA, \
        "空 payload 應回 NO_DATA 而不是版面失敗 (否則每個沒資料的日子都洗一次紅字)"
    assert _cp.verify_tpex_layout({'tables': [{'data': []}]})[0] == _cp.NO_DATA
    # 欄位數不足 = 真的版面問題
    bad = {'tables': [{'data': [['1101', '台泥', 1, 2]]}]}
    assert _cp.verify_tpex_layout(bad)[0] == _cp.BAD_LAYOUT, "欄位數不足應報版面問題"

    # --- 3. 期貨未平倉金額:千元 → 億 ---
    old_base = app.CHIPS_BASE_DIR
    tmp = _tf3.mkdtemp()
    orig_view = app._chips_view.get()
    try:
        app.CHIPS_BASE_DIR = tmp
        fut = pd.DataFrame([{'Date': '2026-07-28', 'Product': '臺股期貨',
                             'Investor': '外資及陸資', 'NetOI': -82255,
                             'NetOIAmount': -684077301, 'NetTrade': -2544}])
        _cs3.upsert(_cs3.futures_inst_path(tmp, '2026'), fut)
        app._chips_view.set('futures')
        app.entry_chips_code.delete(0, 'end')
        app._chips_refresh_view()
        heads = list(app.tree_chips['columns'])
        assert '未平倉淨額(億)' in heads, f"期貨金額欄位沒換成億:{heads}"
        assert '未平倉淨額(千元)' not in heads, heads
        kids = app.tree_chips.get_children()
        assert kids, "期貨籌碼沒有資料列"
        v = app.tree_chips.item(kids[0], 'values')
        # -684,077,301 仟元 = -6840.77 億
        assert v[4] == '-6,840.77', f"未平倉金額換算錯誤:{v[4]}"
        assert v[3] == '-82,255', f"口數不該被換算:{v[3]}"
    finally:
        app.CHIPS_BASE_DIR = old_base
        app._chips_view.set(orig_view)

    # --- 1. 點上方指數也要能帶進策略編輯器 ---
    captured = {}
    class _FakeDlg:
        def winfo_exists(self): return True
    class _FakeEntry:
        def delete(self, *a): captured['cleared'] = True
        def insert(self, i, v): captured['sym'] = v
    class _FakeCb:
        def set(self, v): captured['tt'] = v
    orig_target = getattr(app, '_qt_editor_symbol_target', None)
    orig_fetch = app.start_fetch_thread
    try:
        app.start_fetch_thread = lambda *a, **k: None
        app._qt_editor_symbol_target = (_FakeDlg(), _FakeEntry(), _FakeCb(),
                                        lambda: captured.setdefault('looked_up', True), 'A')
        app.load_index_chart('^TWII')
        assert captured.get('sym') == '^TWII', \
            f"點上方指數沒有帶進編輯器 (實際 {captured.get('sym')!r})"
        assert captured.get('tt') == '指數', f"種類應自動判斷成指數,實際 {captured.get('tt')!r}"
        assert captured.get('looked_up'), "帶入後應觸發商品查詢"
    finally:
        app.start_fetch_thread = orig_fetch
        app._qt_editor_symbol_target = orig_target

    # --- 4. 關閉保底:看門狗常數存在且合理 ---
    assert 0 < app.CLOSE_HARD_DEADLINE <= 30, \
        f"關閉保底時限要有且合理,實際 {app.CLOSE_HARD_DEADLINE}"
    src = open(stock_app_pro.__file__, encoding='utf-8').read()
    assert 'CHIPS_FLUSH_EVERY' in src, "籌碼下載應改成批次寫檔 (逐日 read-modify-write 會餓死 UI)"
    # 逐日寫檔的舊寫法不可再出現
    assert 'chips_store.upsert(chips_store.market_inst_path(self.CHIPS_BASE_DIR), df)' not in src, \
        "市場法人仍在逐日寫檔"
    assert 'chips_store.upsert(chips_store.stock_inst_path(self.CHIPS_BASE_DIR, iso), merged)' not in src, \
        "個股法人仍在逐日寫檔 (10年=2364次整檔重寫,GIL 會把 UI 餓死)"


run_case("ADR-119: 櫃買無資料/期貨金額(億)/指數帶入編輯器/關閉保底",
         _stability_and_units_119)


def _regime_panel_120():
    """【ADR-120】主圖【盤勢判斷】:盤勢/型態從終極波段策略搬到主圖。

    要驗四件事 (每一件都是「錯了不會有錯誤訊息、只會行為怪怪的」那種):
      1. 加權指數日K 出現型態時會自動通知。
      2. **重畫不可以重複通知** —— 主圖每縮放/平移一次就重畫一次。
      3. 週期不是日K、或設定成「只在加權指數」時看的是個股 → 不評估。
      4. 型態偵測出錯不可以害 K 線圖畫不出來。
    另外驗終極波段策略那一區真的被移除了 (不是只是隱藏)。
    """
    import pandas as _pd

    # 穩定上升的日K:必定會被判成「上升趨勢」且收盤貼著區間高點
    n = 120
    closes = [10000 + i * 60 for i in range(n)]   # 斜率要夠陡才會被判成「趨勢」而非「區間整理」
    df = _pd.DataFrame({'Open': closes, 'High': [c + 15 for c in closes],
                        'Low': [c - 15 for c in closes], 'Close': closes,
                        'Volume': [100000] * n},
                       index=_pd.date_range('2026-01-01', periods=n, freq='B'))

    logs = []
    orig_log = app.log_message
    old = (app.current_symbol, app.asset_type, app.current_stock_name,
           app.timeframe_var.get(), dict(app.regime_settings))
    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        app.current_symbol, app.asset_type = '^TWII', 'index'
        app.current_stock_name = '加權指數'
        app.timeframe_var.set('日K')
        app.regime_settings = stock_app_pro.regime_panel.normalize(
            {'enabled': True, 'sr_enabled': False, 'pattern_enabled': True,
             'index_only': True})
        app.regime_enabled_var.set(True)
        app.sr_enabled_var.set(False)
        app._regime_notify_state = {}

        # 1) 加權指數日K 應自動通知
        logs.clear(); app.draw_chart(df)
        hits = [m for m in logs if m.startswith('【盤勢判斷】')]
        assert hits, f"加權指數日K 出現型態時應自動通知,實際日誌: {logs[-5:]}"
        assert '上升趨勢' in hits[0], f"應判成上升趨勢,實際 {hits[0]!r}"

        # 2) 重畫不可重複通知 (縮放/平移會重畫;沒去重就會被洗版)
        for _ in range(3):
            logs.clear(); app.draw_chart(df)
            assert not [m for m in logs if m.startswith('【盤勢判斷】')], \
                "同一根K棒重畫不可重複通知"

        # 3a) 週期要在允許清單內才評估。
        # 【ADR-132】使用者要求加入 60分K,所以這裡從「只有日K」改成
        # 「日K 與 60分K 都要評估、其餘一律不評估」。分K 仍然不可以放行 ——
        # 5分K 會產生大量沒有意義的型態訊號,而 ADR-132 之後它們會推播到手機上。
        app._regime_notify_state = {}
        app.timeframe_var.set('60分K')
        logs.clear(); app.draw_chart(df)
        assert [m for m in logs if m.startswith('【盤勢判斷】')], \
            "60分K 應該要評估型態 (ADR-132 新增)"
        for _bad_tf in ('5分K', '15分K', '30分K', '周K'):
            app._regime_notify_state = {}
            app.timeframe_var.set(_bad_tf)
            logs.clear(); app.draw_chart(df)
            assert not [m for m in logs if m.startswith('【盤勢判斷】')], \
                f"{_bad_tf} 不該評估型態 (只有日K/60分K 在允許清單內)"
        app.timeframe_var.set('日K')

        # 3b) 「只在加權指數」時,看個股不評估
        app._regime_notify_state = {}
        app.current_symbol, app.asset_type = '2330', 'stock'
        logs.clear(); app.draw_chart(df)
        assert not [m for m in logs if m.startswith('【盤勢判斷】')], \
            "設定成只在加權指數判斷時,個股不該通知"
        # 取消該設定後,個股也要能判斷
        app.regime_settings['index_only'] = False
        app._regime_notify_state = {}
        logs.clear(); app.draw_chart(df)
        assert [m for m in logs if m.startswith('【盤勢判斷】')], \
            "取消「只在加權指數」後,個股日K也該判斷"
        app.regime_settings['index_only'] = True
        app.current_symbol, app.asset_type = '^TWII', 'index'

        # 3c) 總開關關掉 → 完全不評估
        app._regime_notify_state = {}
        app.regime_enabled_var.set(False)
        app.regime_settings['enabled'] = False
        logs.clear(); app.draw_chart(df)
        assert not [m for m in logs if m.startswith('【盤勢判斷】')], \
            "【盤勢判斷】總開關關閉時不該評估型態"
        app.regime_enabled_var.set(True)
        app.regime_settings['enabled'] = True

        # 4) 型態偵測出錯不可影響 K 線圖
        app._regime_notify_state = {}
        orig_eval = stock_app_pro.market_pattern.evaluate_all
        try:
            stock_app_pro.market_pattern.evaluate_all = lambda *a, **k: (
                _ for _ in ()).throw(RuntimeError('診斷用假錯誤'))
            logs.clear()
            app.draw_chart(df)                       # 不可拋例外
            assert app.current_fig is not None, "型態偵測出錯時 K 線圖仍應正常畫出"
        finally:
            stock_app_pro.market_pattern.evaluate_all = orig_eval
    finally:
        app.log_message = orig_log
        (app.current_symbol, app.asset_type, app.current_stock_name,
         _tf, app.regime_settings) = old
        app.timeframe_var.set(_tf)
        app.regime_enabled_var.set(bool(app.regime_settings.get('enabled')))
        app.sr_enabled_var.set(bool(app.regime_settings.get('sr_enabled')))

    # 5) ⚙ 設定對話框能建得起來,而且設定真的存得回設定檔
    #    (版面美不美 headless 測不到,但「打開就爆掉」測得到)
    # 別名刻意不叫 _cs —— 這個檔案裡 _cs 已經是 core.custom_strategy
    # (diag_crossref 會把同名別名混在一起看,撞名會誤報跨模組斷鏈)
    from data import config_store as _cfgstore
    old_settings = dict(app.regime_settings)
    try:
        app.open_regime_settings()
        app.flush_after()
        app.regime_settings = stock_app_pro.regime_panel.normalize(
            {'enabled': True, 'sr_enabled': False, 'sr_fixed_bars': 250,
             'pattern_list': ['regime', 'double_top'], 'index_only': False})
        app._save_regime_settings()
        back = stock_app_pro.regime_panel.normalize(
            _cfgstore.load_app_settings(app.app_settings_file).get('regime_panel'))
        assert back == app.regime_settings, f"設定沒有正確存回:{back}"
        assert back['sr_fixed_bars'] == 250 and back['pattern_list'] == ['regime', 'double_top'], \
            "存回來的設定內容不對"
    finally:
        app.regime_settings = old_settings

    # 6) 終極波段策略那一區必須真的被移除 (不是只是不顯示)
    src = open(stock_app_pro.__file__, encoding='utf-8').read()
    for gone in ('_qt_pattern_check', '_qt_pattern_intraday_preview',
                 'QT_PATTERN_PERSISTENT_IDS = frozenset', "s['pattern_enabled'] = bool("):
        assert gone not in src, f"終極波段的盤勢功能應已移除,但還找得到 {gone}"

    # 7) 預設主圖 = 加權指數 日K (使用者需求2)
    assert stock_app_pro.regime_panel.DEFAULT_INDEX_SYMBOL == '^TWII'
    assert 'self.current_symbol = regime_panel.DEFAULT_INDEX_SYMBOL' in src, \
        "啟動預設商品應為加權指數"
    assert 'self.entry_symbol.insert(0, regime_panel.DEFAULT_INDEX_SYMBOL)' in src, \
        "代碼輸入框的預設值應為加權指數"
    assert "self.timeframe_var = tk.StringVar(value=\"日K\")" in src, \
        "啟動預設週期應為日K"


run_case("ADR-120: 主圖盤勢判斷 (自動通知/重畫不洗版/日K限定/移出終極波段/預設加權日K)",
         _regime_panel_120)



def _qt_kbars_resilience_121():
    """【ADR-121】量化策略抓 K 線的開盤韌性。

    使用者實測:策略在 08:45 與 09:00 各噴一次
    `ShioajiTimeoutError: Timeout Topic: api/v1/data/kbars`,而那兩個時刻正是
    期貨/台股時段閘門打開的那一秒 —— 開盤瞬間全市場的用戶端同時打同一支 API。

    這裡驗五件事,每一件錯了都不會有錯誤訊息,只會「行為怪怪的」:
      1. 開盤暖機期間**完全不抓 K 線**;暖機過了才抓。
      2. 抓失敗會**重試**(靠還原 boundary,下一個 runner tick 重跑同一根K棒),
         而且重試期間**不記錯誤訊息**。
      3. 重試用完才記**一次**,而且**停手**(不可以無限每 2 秒重打券商 API)。
      4. 斷線類例外**不當成可重試的資料錯誤**。
      5. 資料錯誤**永不自動停用策略**;但策略邏輯錯誤照樣 3 次停用。
    """
    import pandas as _pd
    from core import strategy_engine as _se2

    # 原始分K:80 個營業日 × 5 根,重採樣成日K 後夠算 MA10
    _idx, _rows = [], []
    _px = 100.0
    for _d in _pd.bdate_range('2026-01-01', periods=80):
        for _h in (9, 10, 11, 12, 13):
            _px += 0.1
            _idx.append(_d + _pd.Timedelta(hours=_h))
            _rows.append({'Open': _px, 'High': _px + 0.5, 'Low': _px - 0.5,
                          'Close': _px, 'Volume': 1000})
    RAW = _pd.DataFrame(_rows, index=_pd.DatetimeIndex(_idx))

    class FakeContract:
        code = '2330'
        symbol = '2330'

    logs = []
    orig_log = app.log_message
    orig_dl = app._download_kbars_raw
    orig_resolve = app._qt_resolve
    orig_open = stock_app_pro.market_session.is_market_open
    orig_just = stock_app_pro.market_session.just_opened
    # 【ADR-122】這個案例驗的是「單次下載失敗後的重試」,不是分段。把日K 的
    # 取數天數壓到分段門檻以下,讓它維持一次請求一次呼叫,斷言才數得準。
    app.QT_TF_DAYS = dict(StockTradingAppPro.QT_TF_DAYS, **{'日K': 30})
    orig_running, orig_login = app._qt_running, app.api_logged_in
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes
    orig_api = app.sj_api

    calls = {'n': 0, 'fail_until': 0}

    def _fake_dl(*_a, **_k):
        calls['n'] += 1
        if calls['n'] <= calls['fail_until']:
            raise RuntimeError('診斷用假逾時: Timeout Topic: api/v1/data/kbars')
        return RAW

    def _reset(fail_until=0, session_gate=True):
        """每個子案例都從乾淨狀態開始 (清快取/boundary/重試計數)。"""
        calls['n'] = 0
        calls['fail_until'] = fail_until
        logs.clear()
        app._kbars_raw_cache.clear()
        app._qt_last_boundary = {}
        app._qt_fetch_attempts = {}
        app._qt_warmup_noted = {}
        app._qt_usage_logged_date = 'x'      # 診斷不去打真的 usage()
        st = _se2.new_strategy()
        st.update({'name': '診斷ADR121', 'symbol': '2330', 'market': '台股',
                   'timeframe': '日K', 'qty': 1, 'cooldown_sec': 0, 'enabled': True,
                   'session_gate': session_gate, 'mode': '模擬',
                   'entry': [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}]})
        app.strategies = [st]
        app.strategy_runtimes = {st['id']: _se2.new_runtime()}
        return st, app.strategy_runtimes[st['id']]

    def _err_logs():
        return [m for m in logs if '自動交易-資料異常' in m]

    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        app._download_kbars_raw = _fake_dl
        app._qt_resolve = lambda _s: (FakeContract(), 'stock')
        app.sj_api = object()
        app.api_logged_in = True
        app._qt_running = True

        # ---- 1. 開盤暖機期間完全不抓 K 線 ----
        stock_app_pro.market_session.is_market_open = lambda *a, **k: True
        stock_app_pro.market_session.just_opened = lambda *a, **k: True
        _reset(session_gate=True)
        eval_pass(); app.flush_after()
        assert calls['n'] == 0, f"開盤暖機期間不該抓K線,實際抓了 {calls['n']} 次"
        assert any('避開開盤尖峰' in m for m in logs), \
            f"暖機時應記一次說明,實際日誌: {logs[-3:]}"
        # 暖機只記一次,不可每 2 秒洗版
        n_note = sum(1 for m in logs if '避開開盤尖峰' in m)
        eval_pass(); app.flush_after()
        assert sum(1 for m in logs if '避開開盤尖峰' in m) == n_note, "暖機提示不可重複記錄"

        # 暖機過了就要抓 —— 而且是**同一根K棒**,沒有被暖機吃掉
        stock_app_pro.market_session.just_opened = lambda *a, **k: False
        eval_pass(); app.flush_after()
        assert calls['n'] >= 1, "暖機結束後應正常抓K線 (該K棒不可被暖機吃掉)"

        # ---- 2. 抓失敗會重試,重試期間不記錯誤 ----
        _reset(fail_until=2, session_gate=False)
        eval_pass(); app.flush_after()
        assert calls['n'] == 1 and not _err_logs(), "第1次失敗不該立刻記錯誤"
        eval_pass(); app.flush_after()
        assert calls['n'] == 2, "boundary 應被還原,下一輪要重試同一根K棒"
        assert not _err_logs(), "第2次失敗仍不該記錯誤 (還沒用完重試)"
        eval_pass(); app.flush_after()
        assert calls['n'] == 3, "第3輪應再試一次"
        assert not _err_logs(), "第3次成功了,不該有任何錯誤訊息"

        # ---- 3. 重試用完:只記一次,而且停手 ----
        _reset(fail_until=999, session_gate=False)
        for _ in range(5):
            eval_pass(); app.flush_after()
        assert calls['n'] == app.QT_KBARS_MAX_ATTEMPTS, \
            f"重試用完後必須停手,實際打了 {calls['n']} 次 API"
        assert len(_err_logs()) == 1, f"錯誤訊息應只記一次,實際 {len(_err_logs())} 則"

        # ---- 4. 斷線類例外不當成可重試的資料錯誤 ----
        _reset(session_gate=False)
        app._download_kbars_raw = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError('AuthError: Not authenticated'))
        st4 = app.strategies[0]
        eval_pass(); app.flush_after()
        assert not _err_logs(), "斷線不是『抓不到資料』,不該走重試那條"
        assert any('自動交易-異常' in m for m in logs), "斷線應照舊記一般異常"
        app.api_logged_in = True          # _mark_session_dead 會把它撥掉,還原給後續子案例
        app._download_kbars_raw = _fake_dl

        # ---- 5a. 資料錯誤永不自動停用策略 ----
        st5, rt5 = _reset(fail_until=999, session_gate=False)
        for _ in range(12):
            app._qt_last_boundary = {}    # 模擬時間往前走 (每輪都是新的K棒邊界)
            eval_pass(); app.flush_after()
        assert st5['enabled'] is True, \
            "抓不到K線不可以自動停用策略 —— 停用會連即時停損一起關掉"
        assert rt5.get('data_error_count', 0) > 0, "資料錯誤應有自己的計數"
        assert rt5.get('error_count', 0) == 0, "資料錯誤不可以累加到『會停用』的那個計數"

        # ---- 5b. 策略邏輯錯誤照樣 3 次自動停用 (保護沒有被整個拿掉) ----
        st6, rt6 = _reset(session_gate=False)
        app._qt_resolve = lambda _s: (None, None)     # 合約解析失敗 → 邏輯錯誤
        for _ in range(3):
            app._qt_last_boundary = {}
            eval_pass(); app.flush_after()
        assert st6['enabled'] is False, "策略邏輯連續 3 次錯誤仍應自動停用"
    finally:
        app.__dict__.pop('QT_TF_DAYS', None)
        app.log_message = orig_log
        app._download_kbars_raw = orig_dl
        app._qt_resolve = orig_resolve
        stock_app_pro.market_session.is_market_open = orig_open
        stock_app_pro.market_session.just_opened = orig_just
        app._qt_running, app.api_logged_in = orig_running, orig_login
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts
        app.sj_api = orig_api
        app._qt_last_boundary = {}
        app._qt_fetch_attempts = {}
        app._kbars_raw_cache.clear()
        app._qt_usage_logged_date = None


run_case("ADR-121: 開盤暖機/抓K線重試/資料錯誤不停用策略", _qt_kbars_resilience_121)



def _qt_chunked_prefetch_122():
    """【ADR-122】大範圍 K 線改走分段下載,而且分段做在背景執行緒上。

    要驗六件事:
      1. runner 這一輪**完全不下載**(只起預抓),也不算任何錯誤。
      2. 預抓跑完後,下一輪就能正常評估;而且下載真的是**分段**的
         (每次請求 ≤ 90 天,次數等於 chunk_plan 算出來的段數)。
      3. 小請求(5分K)**維持原地下載**,行為與 ADR-122 之前一致。
      4. 預抓在途不重複開執行緒。
      5. 預抓失敗才算資料錯誤(而且策略仍不被停用)。
      6. 主圖的分段門檻與 core/kbars_plan 的常數一致(原始碼層級)。
    """
    import pandas as _pd
    from core import strategy_engine as _se3
    from core import kbars_plan as _kp

    # 【重要】日期一定要涵蓋「現在往前推 QT_TF_DAYS 天」那段區間 —— 用寫死的
    # 舊日期會讓每一段都切出空 DataFrame,快取永遠填不進去,而下游斷言
    # (「第二輪不可再下載」) 在那種情況下**照樣會過**,變成空殼測試 (P-28)。
    _idx, _rows = [], []
    _px = 100.0
    for _d in _pd.bdate_range(end=stock_app_pro.datetime.now(), periods=600):
        for _h in (9, 11, 13):
            _px += 0.05
            _idx.append(_d + _pd.Timedelta(hours=_h))
            _rows.append({'Open': _px, 'High': _px + 0.5, 'Low': _px - 0.5,
                          'Close': _px, 'Volume': 1000})
    RAW = _pd.DataFrame(_rows, index=_pd.DatetimeIndex(_idx))

    class FakeContract:
        code = '2330'
        symbol = '2330'

    logs = []
    orig_log = app.log_message
    orig_dl = app._download_kbars_raw
    orig_resolve = app._qt_resolve
    orig_running, orig_login, orig_api = app._qt_running, app.api_logged_in, app.sj_api
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes
    calls = {'spans': [], 'idents': [], 'fail': False,
             'fail_after': None,      # 前 N 次成功、之後失敗 → 製造「部分成功」
             'gate': None}            # 設了 Event 就在下載裡等,讓預抓確定停在途中

    import threading as _th
    _caller_ident = _th.get_ident()      # 診斷腳本自己這條 = 「runner 執行緒」

    def _fake_dl(_c, s0, s1, *_a, **_k):
        # 記下是哪一條執行緒打的 —— 這個案例的重點就是「大範圍不可以在
        # runner 那條執行緒上下載」,只數次數分不出來 (背景那條也會累加)。
        calls['spans'].append((s1 - s0).days)
        calls['idents'].append(_th.get_ident())
        if calls['gate'] is not None:
            calls['gate'].wait(timeout=20)
        n = len(calls['spans'])
        if calls['fail'] or (calls['fail_after'] is not None and n > calls['fail_after']):
            raise RuntimeError('診斷用假逾時: Timeout Topic: api/v1/data/kbars')
        return RAW[(RAW.index >= s0) & (RAW.index <= s1)]

    def _inline_calls():
        return [i for i in calls['idents'] if i == _caller_ident]

    def _reset(tf='日K'):
        calls['spans'] = []
        calls['idents'] = []
        calls['fail_after'] = None
        calls['gate'] = None
        logs.clear()
        app._kbars_raw_cache.clear()
        app._qt_last_boundary = {}
        app._qt_fetch_attempts = {}
        app._qt_prefetch_inflight = set()
        app._qt_prefetch_errors = {}
        app._qt_usage_logged_date = 'x'
        st = _se3.new_strategy()
        st.update({'name': '診斷ADR122', 'symbol': '2330', 'market': '台股',
                   'timeframe': tf, 'qty': 1, 'cooldown_sec': 0, 'enabled': True,
                   'session_gate': False, 'mode': '模擬',
                   'entry': [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}]})
        app.strategies = [st]
        app.strategy_runtimes = {st['id']: _se3.new_runtime()}
        return st, app.strategy_runtimes[st['id']]

    def _wait_prefetch():
        t = getattr(app, '_qt_prefetch_thread', None)
        if t is not None:
            t.join(timeout=30)

    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        app._download_kbars_raw = _fake_dl
        app._qt_resolve = lambda _s: (FakeContract(), 'stock')
        app.sj_api = object()
        app.api_logged_in = True
        app._qt_running = True
        app.QT_PREFETCH_SYNC = False        # 這個案例就是要驗真正的背景時序
        app.QT_PREFETCH_RETRIES = 0         # 診斷不需要等退避重試
        app.QT_PREFETCH_PACE_SEC = 0

        # ---- 1. runner 這一輪不下載,只起預抓 ----
        st1, rt1 = _reset('日K')
        eval_pass(); app.flush_after()
        assert _inline_calls() == [], \
            f"大範圍請求不可以在 runner 執行緒裡下載,實際在該執行緒打了 {len(_inline_calls())} 次"
        assert rt1.get('data_error_count', 0) == 0, "『資料背景補齊中』不是錯誤,不可計數"
        assert not [m for m in logs if '資料異常' in m], "背景補資料中不該記資料異常"
        assert any('背景補齊中' in m for m in logs), \
            f"應記一行讓使用者知道在補資料,實際: {logs[-3:]}"

        # ---- 2. 預抓完成後下一輪就能評估,而且下載是分段的 ----
        _wait_prefetch(); app.flush_after()
        plan = _kp.chunk_plan('日K', app.QT_TF_DAYS['日K'])
        assert calls['spans'], "預抓執行緒應該真的有下載"
        assert max(calls['spans']) <= plan['chunk_days'], \
            f"每段不可超過 {plan['chunk_days']} 天,實際 {max(calls['spans'])}"
        assert len(calls['spans']) >= 2, f"300 天應該被切成多段,實際 {len(calls['spans'])} 段"
        # 直接證明「快取真的被填進去了」—— 只驗「第二輪沒再下載」是不夠的:
        # 預抓失敗時第二輪同樣不會下載 (走的是回報失敗那條),兩種情況分不出來。
        cached = [k for k in app._kbars_raw_cache if k.endswith('|日K')]
        assert cached, f"預抓成功後快取應該有資料,實際 keys: {list(app._kbars_raw_cache)}"
        n_before = len(calls['spans'])
        eval_pass(); app.flush_after()
        assert len(calls['spans']) == n_before, "第二輪應命中快取,不可再下載"
        assert rt1.get('data_error_count', 0) == 0, "補完之後不該有任何資料錯誤"
        assert st1['enabled'] is True

        # ---- 2b. 日K 類快取要放到夠長,10 分鐘的評估間隔才命中得到 ----
        # 把快取時間戳往前撥 10 分鐘 (= 日K 類的評估間隔)。舊的 300 秒 TTL
        # 在這裡必定已過期 → 會重新下載;ADR-122 的 1800 秒才命中得到。
        for _k in list(app._kbars_raw_cache):
            app._kbars_raw_cache[_k]['t'] -= 600
        n_before2 = len(calls['spans'])
        app._qt_last_boundary = {}
        eval_pass(); app.flush_after()
        assert len(calls['spans']) == n_before2, \
            "日K 類快取放了 10 分鐘仍應命中 (TTL 若短於評估間隔,快取等於完全沒作用)"

        # ---- 3. 小請求維持原地下載 ----
        _reset('5分K')
        eval_pass(); app.flush_after()
        assert len(_inline_calls()) == 1 and len(calls['spans']) == 1, \
            f"5分K (7天) 應維持在 runner 執行緒單次原地下載,實際 {calls['spans']}"

        # ---- 4. 預抓在途不重複開執行緒 ----
        _reset('月K')
        eval_pass(); app.flush_after()
        first = getattr(app, '_qt_prefetch_thread', None)
        for _ in range(3):
            app._qt_last_boundary = {}
            eval_pass(); app.flush_after()
        assert getattr(app, '_qt_prefetch_thread', None) is first, \
            "同一個 key 在途時不可以再開一條預抓執行緒"
        _wait_prefetch()

        # ---- 4b. 預抓在途連跑多輪,不可以被算成資料錯誤 ----
        # 用閘門把預抓確定卡在途中,再連跑到超過重試上限的輪數:若「補資料中」
        # 被誤當成失敗,錯誤計數就會累積、日誌也會冒出資料異常。
        st4b, rt4b = _reset('日K')
        calls['gate'] = _th.Event()
        eval_pass(); app.flush_after()
        for _ in range(app.QT_KBARS_MAX_ATTEMPTS + 2):
            app._qt_last_boundary = {}
            eval_pass(); app.flush_after()
        assert rt4b.get('data_error_count', 0) == 0, \
            f"預抓在途不可以累積成資料錯誤 (實際 {rt4b.get('data_error_count')})"
        assert not [m for m in logs if '資料異常' in m], \
            f"預抓在途不該記資料異常,實際: {[m for m in logs if '資料異常' in m][:2]}"
        calls['gate'].set(); _wait_prefetch(); app.flush_after()
        calls['gate'] = None

        # ---- 4c. 部分成功不可以進快取 ----
        # 缺的若是「最近」那一段,策略會拿過期資料去評估並據此下單 —— 寧可
        # 下一輪重來,也不要拿殘缺資料冒充完整。
        _reset('日K')
        calls['fail_after'] = 2          # 前 2 段成功、其餘失敗
        eval_pass(); app.flush_after()
        _wait_prefetch(); app.flush_after()
        assert not [k for k in app._kbars_raw_cache if k.endswith('|日K')], \
            "分段有段失敗時不可以把殘缺資料寫進快取"
        calls['fail_after'] = None

        # ---- 5. 預抓失敗才算資料錯誤,而且不停用策略 ----
        st5, rt5 = _reset('日K')
        calls['fail'] = True
        for _ in range(8):
            app._qt_last_boundary = {}
            eval_pass(); app.flush_after()
            _wait_prefetch(); app.flush_after()
        assert rt5.get('data_error_count', 0) > 0, "預抓失敗必須被記成資料錯誤"
        assert st5['enabled'] is True, "抓不到資料仍然不可以自動停用策略 (ADR-121)"
        calls['fail'] = False
    finally:
        app.QT_PREFETCH_SYNC = True
        app.__dict__.pop('QT_PREFETCH_RETRIES', None)
        app.__dict__.pop('QT_PREFETCH_PACE_SEC', None)
        app.log_message = orig_log
        app._download_kbars_raw = orig_dl
        app._qt_resolve = orig_resolve
        app._qt_running, app.api_logged_in, app.sj_api = orig_running, orig_login, orig_api
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts
        app._qt_last_boundary = {}
        app._qt_fetch_attempts = {}
        app._qt_prefetch_inflight = set()
        app._qt_prefetch_errors = {}
        app._kbars_raw_cache.clear()
        app._qt_usage_logged_date = None

    # ---- 6. 主圖的分段門檻必須與 core/kbars_plan 的常數一致 ----
    # 主圖那段是已驗證過的路徑,ADR-122 刻意不動它;但兩邊的數字一旦改岔,
    # 策略與主圖就會用不同規則,而且沒有任何症狀 (P-67 的老problem)。
    src = open(stock_app_pro.__file__, encoding='utf-8').read()
    for frag, why in (
            (f"is_min_tf and (_t - _f).days > {_kp.MIN_TF_THRESHOLD_DAYS}", '分K門檻'),
            (f"chunk_days={_kp.MIN_TF_CHUNK_DAYS}, subsplit_days={_kp.MIN_TF_SUBSPLIT_DAYS}", '分K段長'),
            (f"(not is_min_tf) and (_t - _f).days > {_kp.DAY_TF_THRESHOLD_DAYS}", '日K門檻'),
            (f"chunk_days={_kp.DAY_TF_CHUNK_DAYS}, abort_cb=", '日K段長')):
        assert frag in src, f"主圖的{why}與 core/kbars_plan 不一致 (找不到 {frag!r})"


run_case("ADR-122: 大範圍K線背景分段預抓 (runner不阻塞/小請求不變/門檻單一出處)",
         _qt_chunked_prefetch_122)



def _chukuangren_own_exits_123():
    """【ADR-123】使用者實測回報的兩件事。

    A. 終極波段策略被泛用的即時停損砍倉。截圖:
       「策略「楚狂人之終極波段」買進 5 TXF @ 40688 [即時] | 即時停損出場
        (損益 -2.44% ≤ -2.0%) → 已記入模擬帳戶,此筆已實現 -968,250」
       那個 2.0% 是 new_strategy() 的預設值,終極波段的編輯器根本沒有這個
       欄位。這裡走**完整的 GUI 路徑** (_qt_check_realtime_futures_stops),
       純函式的單元測試擋不到「呼叫端有沒有真的用到守門」這一層。
    B. 開盤暖機被 session_gate 關掉:關閉時段閘門的策略在開盤瞬間完全沒有
       保護,而它是 24 小時都在評估的,撞上開盤的機率反而更高。
    """
    from core import strategy_engine as _se4
    from core import chukuangren_band as _ck

    class FakeContract:
        code = 'TXF'
        symbol = 'TXFR1'

    logs = []
    orig_log = app.log_message
    orig_resolve = app._qt_resolve
    orig_dl = app._download_kbars_raw
    orig_running, orig_login, orig_api = app._qt_running, app.api_logged_in, app.sj_api
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes
    orig_just = stock_app_pro.market_session.just_opened
    orig_open = stock_app_pro.market_session.is_market_open

    def _mount(s, state='LONG', entry=41700.0, qty=5):
        rt = _se4.new_runtime()
        rt.update({'state': state, 'exec_entry_price': entry, 'entry_price': entry,
                   'qty': qty})
        app.strategies = [s]
        app.strategy_runtimes = {s['id']: rt}
        return rt

    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        app._qt_resolve = lambda _s: (FakeContract(), 'futures')
        app.sj_api = object()
        app.api_logged_in = True
        app._qt_running = True

        # ---- A1. 終極波段:即使殘留 stop_loss_pct=2.0 也不可被砍倉 ----
        ck = _ck.default_strategy()
        ck.update({'name': '診斷終極波段', 'symbol': 'TXF', 'trade_type': '期貨',
                   'market': '台期貨', 'qty': 5, 'mode': '模擬', 'enabled': True,
                   'stop_loss_pct': 2.0})          # 舊存檔殘留值
        rt = _mount(ck)
        logs.clear()
        app._qt_check_realtime_futures_stops({'TXF': 40688.0})   # -2.44%
        app.flush_after()
        assert rt['state'] == 'LONG', \
            f"終極波段不可被泛用即時停損平倉 (state 變成 {rt['state']})"
        assert not [m for m in logs if '即時停損' in m], \
            f"不該出現即時停損訊息,實際: {[m for m in logs if '即時停損' in m][:1]}"

        # ---- A2. 反向對照:一般期貨策略在同樣條件下**仍然要**出場 ----
        # 少了這條,把守門寫成「全部都擋」也會一片綠。
        norm = _se4.new_strategy()
        norm.update({'name': '診斷一般期貨', 'symbol': 'TXF', 'trade_type': '期貨',
                     'market': '台期貨', 'qty': 5, 'mode': '模擬', 'enabled': True,
                     'stop_loss_pct': 2.0})
        rt2 = _mount(norm)
        logs.clear()
        app._qt_check_realtime_futures_stops({'TXF': 40688.0})
        app.flush_after()
        assert rt2['state'] == 'FLAT', "一般期貨策略仍應被即時停損平倉 (守門不可過度攔截)"
        assert [m for m in logs if '即時停損' in m], "一般期貨策略應記錄即時停損"

        # ---- B. 開盤暖機不可以被 session_gate 關掉 ----
        calls = {'n': 0}
        app._download_kbars_raw = lambda *a, **k: (calls.__setitem__('n', calls['n'] + 1), None)[1]
        stock_app_pro.market_session.is_market_open = lambda *a, **k: True
        stock_app_pro.market_session.just_opened = lambda *a, **k: True
        gated = _se4.new_strategy()
        gated.update({'name': '診斷關閘門', 'symbol': 'TXF', 'trade_type': '期貨',
                      'market': '台期貨', 'timeframe': '5分K', 'qty': 1, 'mode': '模擬',
                      'enabled': True, 'session_gate': False,   # ← 關鍵:閘門關掉
                      'entry': [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}]})
        _mount(gated, state='FLAT', qty=0)
        app._qt_last_boundary = {}
        app._qt_warmup_noted = {}
        logs.clear()
        eval_pass(); app.flush_after()
        assert calls['n'] == 0, \
            f"關閉時段閘門的策略在開盤瞬間也要被暖機擋住,實際抓了 {calls['n']} 次K線"
        assert any('避開開盤尖峰' in m for m in logs), \
            f"暖機應記一行,實際: {logs[-2:]}"
        # 暖機過了就要照常評估 (不可以把這根K棒吃掉)
        stock_app_pro.market_session.just_opened = lambda *a, **k: False
        app._qt_last_boundary = {}
        eval_pass(); app.flush_after()
        assert calls['n'] >= 1, "暖機結束後應照常評估"
    finally:
        app.log_message = orig_log
        app._qt_resolve = orig_resolve
        app._download_kbars_raw = orig_dl
        stock_app_pro.market_session.just_opened = orig_just
        stock_app_pro.market_session.is_market_open = orig_open
        app._qt_running, app.api_logged_in, app.sj_api = orig_running, orig_login, orig_api
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts
        app._qt_last_boundary = {}
        app._qt_warmup_noted = {}

    # 【ADR-126】原本這裡直接斷言 market_session.just_opened(15:00:05) —— 那是
    # **純函式的行為**,已經由 tests/test_core.py 的 TestMarketSession 完整覆蓋
    # (含 15:00 夜盤那條)。而 run_case 現在會把時鐘相依的表面凍結住,這種
    # 直接打真實函式的斷言放在診斷裡只會互相打架。純函式歸單元測試、
    # GUI 接線歸診斷,各司其職。


run_case("ADR-123: 終極波段不受泛用即時停損 + 暖機不被時段閘門關掉",
         _chukuangren_own_exits_123)



def _session_gate_realtime_stops_124():
    """【ADR-124】即時停損要看交易時段;終極波段結構上只做日盤。

    重現使用者的模擬帳戶截圖:
        12:01:05  做空 賣出 開倉 5 口 TXF @ 39720
        15:00:01  做空 買進 平倉 5 口 TXF @ 40688   已實現 -968,250
    15:00:01 正好是**夜盤開盤第一秒**,而使用者說「終極波段不會在夜盤做任何
    動作的」。

    時鐘一律用 patch 過的 is_market_open 控制,**不依賴真實時間** (P-94)。
    """
    from core import strategy_engine as _se5
    from core import chukuangren_band as _ck2

    class FakeContract:
        code = 'TXF'
        symbol = 'TXFR1'

    logs = []
    orig_log = app.log_message
    orig_resolve = app._qt_resolve
    orig_resolve_watch = app._qt_resolve_watch
    orig_dl = app._download_kbars_raw
    orig_open = stock_app_pro.market_session.is_market_open
    orig_just = stock_app_pro.market_session.just_opened
    orig_running, orig_login, orig_api = app._qt_running, app.api_logged_in, app.sj_api
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes

    # 假時段:夜盤開著、日盤關著 (= 15:00 之後的狀態)
    def _night_only(trade_type, dt=None, include_night=True):
        return bool(include_night)

    def _mount(s, state='SHORT', entry=39720.0, qty=5):
        rt = _se5.new_runtime()
        rt.update({'state': state, 'exec_entry_price': entry, 'entry_price': entry,
                   'qty': qty})
        app.strategies = [s]
        app.strategy_runtimes = {s['id']: rt}
        return rt

    def _mk(kind_default=False, **over):
        s = _ck2.default_strategy() if kind_default else _se5.new_strategy()
        s.update({'symbol': 'TXF', 'trade_type': '期貨', 'market': '台期貨',
                  'qty': 5, 'direction': '做空', 'mode': '模擬', 'enabled': True,
                  'stop_loss_pct': 2.0})
        s.update(over)
        return s

    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        app._qt_resolve = lambda _s: (FakeContract(), 'futures')
        # 終極波段分支會先解析「看A」(加權指數);沒 stub 的話會在那裡就拋例外,
        # 後面的斷言就變成「因為別的原因而沒抓K線」的空殼 (突變測試抓到過)。
        app._qt_resolve_watch = lambda _s: (FakeContract(), 'index_tw', '^TWII', '台股')
        app.sj_api = object()
        app.api_logged_in = True
        app._qt_running = True
        stock_app_pro.market_session.just_opened = lambda *a, **k: False   # 排除暖機干擾
        stock_app_pro.market_session.is_market_open = _night_only

        # ---- 1. 重現截圖:終極波段在夜盤不可以被平倉 ----
        ck = _mk(True, name='診斷終極波段')
        rt1 = _mount(ck)
        logs.clear()
        app._qt_check_realtime_futures_stops({'TXF': 40688.0})
        app.flush_after()
        assert rt1['state'] == 'SHORT', \
            f"終極波段在夜盤不可以被平倉 (state 變成 {rt1['state']})"
        assert not [m for m in logs if '即時停損' in m], "不該出現即時停損訊息"

        # ---- 2. 一般期貨策略設「只做日盤」→ 夜盤也不可以被停損 ----
        # (這是跟終極波段無關的另一個洞:使用者明確設定被無視)
        day_only = _mk(False, name='診斷只做日盤', futures_session='day')
        rt2 = _mount(day_only)
        logs.clear()
        app._qt_check_realtime_futures_stops({'TXF': 40688.0})
        app.flush_after()
        assert rt2['state'] == 'SHORT', "設成只做日盤的策略,夜盤不可以被停損"

        # ---- 3. 反向對照:設「日盤+夜盤」→ 夜盤**仍然要**停損 ----
        # 少了這條,把閘門寫成「夜盤一律不停損」也會一片綠。
        both = _mk(False, name='診斷日夜盤', futures_session='day_night')
        rt3 = _mount(both)
        logs.clear()
        app._qt_check_realtime_futures_stops({'TXF': 40688.0})
        app.flush_after()
        assert rt3['state'] == 'FLAT', "設成日盤+夜盤的策略,夜盤仍應被停損"

        # ---- 4. 反向對照:日盤時段一般策略仍要停損 (ADR-087 沒被弄壞) ----
        stock_app_pro.market_session.is_market_open = lambda *a, **k: True
        day_only2 = _mk(False, name='診斷日盤中', futures_session='day')
        rt4 = _mount(day_only2)
        logs.clear()
        app._qt_check_realtime_futures_stops({'TXF': 40688.0})
        app.flush_after()
        assert rt4['state'] == 'FLAT', "日盤時段的即時停損仍應正常運作"

        # ---- 5. session_gate=False → 休市也照跑 (尊重使用者設定) ----
        stock_app_pro.market_session.is_market_open = lambda *a, **k: False
        nogate = _mk(False, name='診斷關閘門', session_gate=False)
        rt5 = _mount(nogate)
        logs.clear()
        app._qt_check_realtime_futures_stops({'TXF': 40688.0})
        app.flush_after()
        assert rt5['state'] == 'FLAT', \
            "session_gate=False 代表使用者要求不管時間都跑,即時停損也該照跑"

        # ---- 6. 終極波段在夜盤連評估都不該發生 ----
        stock_app_pro.market_session.is_market_open = _night_only
        calls = {'n': 0}
        app._download_kbars_raw = lambda *a, **k: (calls.__setitem__('n', calls['n'] + 1), None)[1]
        ck2 = _mk(True, name='診斷終極波段2', timeframe='5分K')
        _mount(ck2, state='FLAT', qty=0)
        app._qt_last_boundary = {}
        app._qt_session_state = {}
        eval_pass(); app.flush_after()
        assert calls['n'] == 0, \
            f"終極波段在夜盤不該評估 (實際抓了 {calls['n']} 次K線)"
        # 正控:把時段打開,同一檔策略**必須**真的去抓K線 —— 沒有這條,
        # 上面那句在「因為別的原因而沒抓」時也會是綠的 (P-28 那一類的空殼)。
        stock_app_pro.market_session.is_market_open = lambda *a, **k: True
        app._qt_last_boundary = {}
        app._qt_session_state = {}
        eval_pass(); app.flush_after()
        assert calls['n'] > 0, \
            "時段打開後終極波段必須真的評估 (否則上面那條斷言是空殼)"
    finally:
        app.log_message = orig_log
        app._qt_resolve = orig_resolve
        app._qt_resolve_watch = orig_resolve_watch
        app._download_kbars_raw = orig_dl
        stock_app_pro.market_session.is_market_open = orig_open
        stock_app_pro.market_session.just_opened = orig_just
        app._qt_running, app.api_logged_in, app.sj_api = orig_running, orig_login, orig_api
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts
        app._qt_last_boundary = {}
        app._qt_session_state = {}


run_case("ADR-124: 即時停損看交易時段 + 終極波段只做日盤",
         _session_gate_realtime_stops_124)



def _enabled_strategy_locked_125():
    """【ADR-125】啟用中的策略不可刪除;編輯只能唯讀檢視。

    重現使用者截圖:策略「狀態=啟用、運轉狀態=運轉中」、持倉 `--` (FLAT),
    卻被 🗑 刪除 直接刪掉了 —— 舊程式唯一那道檢查是「仍有持倉」,FLAT 就放行。

    這裡走**完整的 GUI 路徑** (_qt_delete_strategy / _qt_edit_strategy),
    純函式測不到「呼叫端有沒有真的用到守門」那一層 (P-64)。
    """
    from core import strategy_engine as _se6

    logs = []
    orig_log = app.log_message
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes
    orig_sel = app._qt_selected
    orig_open_editor = app._qt_open_editor
    orig_save, orig_save_state = app._qt_save, app._qt_save_state
    orig_refresh = app._qt_refresh_tree
    opened = {}

    def _mount(enabled=True, state='FLAT'):
        st = _se6.new_strategy()
        st.update({'name': '診斷ADR125', 'symbol': 'MXFR1', 'trade_type': '期貨',
                   'market': '台期貨', 'timeframe': '5分K', 'qty': 1,
                   'direction': '做空', 'mode': '模擬', 'enabled': enabled,
                   'entry': [{'type': 'ma_cross_down', 'params': {'fast': 5, 'slow': 20}}]})
        rt = _se6.new_runtime(); rt['state'] = state
        if state in ('LONG', 'SHORT'):
            rt.update({'qty': 1, 'entry_price': 20000.0, 'exec_entry_price': 20000.0})
        app.strategies = [st]
        app.strategy_runtimes = {st['id']: rt}
        app._qt_selected = lambda: st
        return st

    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        # 存檔/畫面刷新在這個案例無關,擋掉避免動到暫存檔與 mock widget
        app._qt_save = lambda *a, **k: None
        app._qt_save_state = lambda *a, **k: None
        app._qt_refresh_tree = lambda *a, **k: None

        # ---- 1. 啟用中 + FLAT (重現截圖) → 不可刪除 ----
        st1 = _mount(enabled=True, state='FLAT')
        logs.clear()
        app._qt_delete_strategy(); app.flush_after()
        assert any(x['id'] == st1['id'] for x in app.strategies), \
            "啟用中的策略不可以被刪除 (使用者實測的問題)"
        assert any('停用' in m for m in logs), \
            f"應提示要先停用,實際日誌: {logs[-2:]}"

        # ---- 2. 反向對照:停用後**確實刪得掉** ----
        # 少了這條,把守門寫成「永遠不可刪」也會一片綠,那等於功能被鎖死。
        st1['enabled'] = False
        logs.clear()
        app._qt_delete_strategy(); app.flush_after()
        assert not any(x['id'] == st1['id'] for x in app.strategies), \
            "停用且無持倉的策略應該要刪得掉"

        # ---- 3. 停用但有持倉 → 仍刪不掉 (既有行為沒被弄壞) ----
        st3 = _mount(enabled=False, state='SHORT')
        logs.clear()
        app._qt_delete_strategy(); app.flush_after()
        assert any(x['id'] == st3['id'] for x in app.strategies), \
            "仍有持倉的策略不可以被刪除"
        assert any('持倉' in m for m in logs), "應提示仍有持倉"

        # ---- 4. 啟用中按編輯 → 只能唯讀檢視 ----
        st4 = _mount(enabled=True, state='FLAT')
        opened.clear()
        app._qt_open_editor = lambda _s, readonly=False: opened.update(readonly=readonly)
        app._qt_edit_strategy(); app.flush_after()
        assert opened.get('readonly') is True, \
            f"啟用中的策略只能唯讀檢視,實際 readonly={opened.get('readonly')!r}"
        # 反向對照:停用後編輯器要是可寫的
        st4['enabled'] = False
        opened.clear()
        app._qt_edit_strategy(); app.flush_after()
        assert opened.get('readonly') is False, \
            "停用的策略應該可以正常編輯"
    finally:
        app.log_message = orig_log
        app._qt_selected = orig_sel
        app._qt_open_editor = orig_open_editor
        app._qt_save, app._qt_save_state = orig_save, orig_save_state
        app._qt_refresh_tree = orig_refresh
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts


run_case("ADR-125: 啟用中的策略不可刪除 / 編輯只能唯讀",
         _enabled_strategy_locked_125)



def _live_chart_refresh_126():
    """【ADR-126】主圖活K棒不會即時更新 + 報價上屏加速。

    使用者實測回報:「主圖的K線不會即時更新,以前會,現在又不會了」,而且
    **白天盤中也一樣**、**手動按週期按鈕重載會跳到最新**。兩件事合起來說明
    下載那層是好的,壞的是「自動更新」那一層。

    根因:`_live_bar_painter` 的所有前置條件寫成一個大 if,任何一條不成立就
    **安靜地什麼都不做**,而 `_hover_bg` (blit 底圖) 會被縮放/平移/視野變動/
    尚未觸發 draw_event 等好幾條路作廢 —— 一旦作廢又沒有下一次完整重繪,
    活K棒就永遠停住,而且日誌一個字都沒有。
    """
    import pandas as _pd
    import numpy as _np

    n = 60
    base = _np.linspace(40000, 40500, n)
    df = _pd.DataFrame({'Open': base, 'High': base + 30, 'Low': base - 30,
                        'Close': base + 5, 'Volume': [1000] * n},
                       index=_pd.date_range('2026-07-29 09:00', periods=n, freq='5min'))

    logs = []
    orig_log = app.log_message
    old = (app.current_symbol, app.current_stock_name, app.asset_type,
           app.current_timeframe, app.current_df)
    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        app.current_symbol = 'TXFR1'; app.current_stock_name = '臺股期貨近月'
        app.asset_type = 'futures'; app.current_timeframe = '5分K'
        app.timeframe_var.set('5分K')
        app.current_df = df
        app.draw_chart(df)
        app.flush_after()

        # --- 1. tick 進來要累積成活K棒 ---
        app._live_bar = None
        app._live_bar_on_tick(40776.0)
        assert app._live_bar and app._live_bar.get('dirty'), "tick 應累積進活K棒並標記 dirty"
        assert app._live_bar['c'] == 40776.0

        # --- 2. blit 底圖不可用時,必須有退路 (原本是安靜地什麼都不做) ---
        app._hover_bg = None
        app._live_bar_fallback_ts = 0.0
        app._live_bar_fallback_noted = None
        reason = app._live_bar_blocked_reason()
        assert reason == 'no_blit_bg', f"應能指出卡在 blit 底圖,實際 {reason!r}"
        drawn = {'n': 0}
        cv = app.current_canvas
        orig_idle = cv.draw_idle
        try:
            cv.draw_idle = lambda *a, **k: drawn.__setitem__('n', drawn['n'] + 1)
            logs.clear()
            app._live_bar_painter(); app.flush_after()
            assert drawn['n'] >= 1, \
                "blit 底圖不可用時必須退回完整重繪,不可以安靜地什麼都不做"
            assert any('活K棒改用完整重繪' in m for m in logs), \
                f"第一次退回時應記一行原因,實際: {logs[-2:]}"
            # 節流:同一秒內不可以連續重繪 (完整重繪很貴)
            n_before = drawn['n']
            app._live_bar_painter(); app.flush_after()
            assert drawn['n'] == n_before, "退路必須節流,不可以每 200ms 就重繪一次"
        finally:
            cv.draw_idle = orig_idle

        # --- 3. 沒有新 tick 時不可以亂重繪 ---
        app._live_bar = None
        assert app._live_bar_blocked_reason() == 'no_tick'

        # --- 4. 上屏加速:活K棒間隔要比原本的 400ms 快 ---
        assert app.LIVE_BAR_PAINT_MS < 400, \
            f"活K棒上屏應比原本 400ms 快,實際 {app.LIVE_BAR_PAINT_MS}"

        # --- 5. 【鐵則5】加快上屏**不可以**連帶把快照 API 打快 ---
        # 這裡刻意**真的把 worker 跑起來數**,而不是在診斷裡重算一次同樣的
        # 除法 —— 第一版就是那樣寫的,結果把 worker 裡的 `every` 改成寫死的
        # 10 (等於上屏變快、API 也跟著變快),診斷照樣是綠的:那只測到我自己
        # 的公式,沒測到程式。突變測試當場抓到。
        assert app.WL_STREAM_UI_SEC < 0.25, \
            f"串流上屏應比原本 0.25s 快,實際 {app.WL_STREAM_UI_SEC}"

        class _StopLoop(Exception):
            pass

        counters = {'snap': 0, 'rounds': 0}
        ROUNDS = 200
        orig_fetch_once = app._wl_fetch_quotes_once
        orig_ensure = app._wl_ensure_stream_subs
        orig_sleep = stock_app_pro.time.sleep

        def _fake_sleep(sec):
            counters['rounds'] += 1
            counters['sec'] = sec
            if counters['rounds'] >= ROUNDS:
                raise _StopLoop

        try:
            app._wl_fetch_quotes_once = lambda: counters.__setitem__('snap', counters['snap'] + 1)
            app._wl_ensure_stream_subs = lambda: None
            stock_app_pro.time.sleep = _fake_sleep
            try:
                app.watchlist_quote_worker()
            except _StopLoop:
                pass
        finally:
            stock_app_pro.time.sleep = orig_sleep
            app._wl_fetch_quotes_once = orig_fetch_once
            app._wl_ensure_stream_subs = orig_ensure

        assert counters['snap'] > 0, "worker 應該有打過快照"
        assert abs(counters.get('sec', 0) - app.WL_STREAM_UI_SEC) < 1e-9, \
            f"worker 的睡眠間隔應為 WL_STREAM_UI_SEC,實際 {counters.get('sec')}"
        sim_sec = ROUNDS * app.WL_STREAM_UI_SEC          # 模擬經過的秒數
        snap_interval = sim_sec / counters['snap']
        assert abs(snap_interval - 2.5) < 0.15, \
            (f"股票批次快照必須維持 2.5 秒一次 (鐵則5/ADR-094),"
             f"實際 {snap_interval:.2f}s ({counters['snap']} 次 / {sim_sec:.1f}s)")
    finally:
        app.log_message = orig_log
        (app.current_symbol, app.current_stock_name, app.asset_type,
         app.current_timeframe, app.current_df) = old
        app._live_bar = None
        app._live_bar_fallback_ts = 0.0
        app._live_bar_fallback_noted = None


run_case("ADR-126: 活K棒 blit 失效要有退路 + 報價上屏加速 (快照節流不變)",
         _live_chart_refresh_126)



def _day_cache_session_freshness_127():
    """【ADR-127】重現使用者的 Telegram 截圖:同一則「背景補齊中」推播兩次。

        09:20  【自動交易-資料】^TWII 日K 歷史資料分 4 段在背景補齊中...
        09:55  【自動交易-資料】^TWII 日K 歷史資料分 4 段在背景補齊中...

    35 分鐘 = ADR-122 的 30 分鐘 TTL 過期之後的下一個 5 分鐘評估點(終極波段
    的看A 是 5分K,所以每 5 分鐘評估一次,但抓的是**日K**)。那段期間 ^TWII
    的日K「已收盤」集合根本沒變 —— 白抓一次 300 天資料、白推播一次。

    這裡走**完整的 GUI 路徑**,並用「把快取時間戳往前撥」模擬時間經過,
    不依賴真實時鐘 (P-94/P-97)。
    """
    import pandas as _pd
    from core import strategy_engine as _se7
    from core import kbars_plan as _kp2

    class FakeContract:
        code = 'TSE'
        symbol = '^TWII'

    _idx, _rows = [], []
    _px = 20000.0
    for _d in _pd.bdate_range(end=stock_app_pro.datetime.now(), periods=600):
        for _h in (9, 11, 13):
            _px += 1.0
            _idx.append(_d + _pd.Timedelta(hours=_h))
            _rows.append({'Open': _px, 'High': _px + 5, 'Low': _px - 5,
                          'Close': _px, 'Volume': 1000})
    RAW = _pd.DataFrame(_rows, index=_pd.DatetimeIndex(_idx))

    logs = []
    orig_log = app.log_message
    orig_dl = app._download_kbars_raw
    orig_running, orig_login, orig_api = app._qt_running, app.api_logged_in, app.sj_api
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes
    orig_sync = app.QT_PREFETCH_SYNC
    calls = {'n': 0}

    def _fake_dl(_c, s0, s1, *_a, **_k):
        calls['n'] += 1
        return RAW[(RAW.index >= s0) & (RAW.index <= s1)]

    def _age_cache(seconds):
        """把所有快取的時間戳往前撥,模擬「經過了這麼久」。"""
        for _k in list(app._kbars_raw_cache):
            app._kbars_raw_cache[_k]['t'] -= seconds

    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        app._download_kbars_raw = _fake_dl
        app.sj_api = object(); app.api_logged_in = True; app._qt_running = True
        # 「背景補齊中」那則通知只存在於**非同步預抓**那條路 (_qt_start_kbars_prefetch),
        # 所以這個案例必須走真正的背景路徑 (第一版設成 SYNC=True,訊息根本不會
        # 出現,斷言當場紅)。retries/pace 調 0 只是讓診斷快一點。
        app.QT_PREFETCH_SYNC = False
        app.QT_PREFETCH_RETRIES = 0
        app.QT_PREFETCH_PACE_SEC = 0
        app._kbars_raw_cache.clear()
        app._qt_prefetch_noted = {}
        app._qt_prefetch_inflight = set()
        app._qt_prefetch_errors = {}

        st = _se7.new_strategy()
        st.update({'name': '診斷ADR127', 'symbol': '2330', 'market': '台股',
                   'timeframe': '日K', 'qty': 1, 'enabled': True,
                   'session_gate': False, 'mode': '模擬'})
        contract = FakeContract()

        def _fetch():
            try:
                return app._qt_fetch_closed_bars(st, contract, 'index_tw', tf='日K',
                                                 cache_sym='^TWII', cache_market='台股')
            except _se7.KBarsPending:
                return None      # 預抓在途是預期中的,不是錯誤

        def _fetch_and_wait():
            r = _fetch()
            t = getattr(app, '_qt_prefetch_thread', None)
            if t is not None:
                t.join(timeout=30)
            app.flush_after()
            return r

        # --- 1. 第一次抓:起背景預抓、通知一次、分段下載 ---
        logs.clear()
        _fetch_and_wait()
        n_first = calls['n']
        plan = _kp2.chunk_plan('日K', app.QT_TF_DAYS['日K'])
        assert n_first >= plan['segments'], f"應分 {plan['segments']} 段下載,實際 {n_first}"
        assert sum(1 for m in logs if '背景補齊中' in m) == 1, \
            f"第一次應通知一次,實際 {sum(1 for m in logs if '背景補齊中' in m)} 次"
        df1 = _fetch()
        assert df1 is not None and not df1.empty, "預抓完成後應該拿得到資料"
        n_first = calls['n']

        # --- 2. 重現截圖:35 分鐘後(沒有跨過任何開盤)不可以重抓、不可以再推播 ---
        # ADR-122 的 30 分鐘 TTL 在這裡必定過期 → 舊行為會重抓 + 再推播一次。
        _age_cache(35 * 60)
        logs.clear()
        _fetch_and_wait()
        assert calls['n'] == n_first, \
            f"35 分鐘內沒有跨過開盤,日K 資料不會變,不可以重抓 (實際多抓了 {calls['n'] - n_first} 次)"
        assert not [m for m in logs if '背景補齊中' in m], \
            f"不可以再推播一次「背景補齊中」(這就是使用者截圖的問題): {logs[:2]}"

        # --- 3. 反向對照:跨過開盤就**必須**重抓 ---
        # 少了這條,把新鮮度寫成「永遠新鮮」也會一片綠 —— 那是拿舊資料去評估。
        orig_between = stock_app_pro.market_session.any_session_opens_between
        try:
            stock_app_pro.market_session.any_session_opens_between = lambda *a, **k: True
            logs.clear()
            _fetch_and_wait()
            assert calls['n'] > n_first, "跨過開盤後必須重抓 (否則會拿舊資料評估)"
            # 但通知仍然每天只一次 —— 重抓是必要的,重複推播不是
            assert not [m for m in logs if '背景補齊中' in m], \
                "同一天同一個 key 不可以重複推播,即使真的重抓了"
        finally:
            stock_app_pro.market_session.any_session_opens_between = orig_between

        # --- 4. 絕對上限:週末沒有開盤,快取仍不可以無限期活著 ---
        # 【第一版是空殼斷言,突變測試抓到的】原本只是「往前撥 24 小時 + 1 分鐘
        # 然後斷言會重抓」—— 但往前撥 24 小時的區間**本來就跨過了好幾個真實
        # 開盤** (昨天 15:00、今天 08:45...),`any_session_opens_between` 自己
        # 就回 True 了。拿掉絕對上限那個突變因此一片綠:斷言成立的理由跟上限
        # 完全無關 (同 P-28)。
        #
        # 要真的測到上限,必須先模擬「期間內沒有任何開盤」(= 週末),再看上限
        # 有沒有把快取判成過期。而且要配一條**反向對照**:沒超過上限時
        # 不可以重抓 —— 否則「永遠重抓」也會通過。
        try:
            stock_app_pro.market_session.any_session_opens_between = lambda *a, **k: False
            # (a) 沒跨開盤 + 沒超過上限 → 不可以重抓 (反向對照)
            n_before = calls['n']
            _age_cache(20 * 3600)                 # 20 小時 < 24 小時上限
            _fetch_and_wait()
            assert calls['n'] == n_before, \
                f"週末沒有開盤且未超過絕對上限,不該重抓 (實際多抓了 {calls['n'] - n_before} 次)"
            # (b) 同樣沒跨開盤,但超過上限 → 必須重抓 (只有上限能造成這個差異)
            _age_cache(app.QT_CACHE_MAX_AGE_SEC + 60 - 20 * 3600)
            _fetch_and_wait()
            assert calls['n'] > n_before, "超過絕對上限應重抓 (週末保險)"
        finally:
            stock_app_pro.market_session.any_session_opens_between = orig_between

        # --- 5. 分K 類**不可以**改成 session 判斷 (資料每根K棒真的會變) ---
        st5 = _se7.new_strategy()
        st5.update({'name': '診斷ADR127分K', 'symbol': '2330', 'market': '台股',
                    'timeframe': '5分K', 'qty': 1, 'enabled': True,
                    'session_gate': False, 'mode': '模擬'})
        app._kbars_raw_cache.clear()
        app._qt_fetch_closed_bars(st5, contract, 'stock', tf='5分K',
                                  cache_sym='2330', cache_market='台股')
        n5 = calls['n']
        _age_cache(60)                        # 60 秒 > 分K 的 30 秒 TTL
        app._qt_fetch_closed_bars(st5, contract, 'stock', tf='5分K',
                                  cache_sym='2330', cache_market='台股')
        assert calls['n'] > n5, \
            "分K 類必須維持短 TTL (每根K棒資料真的會變),不可以套用 session 判斷"
    finally:
        app.log_message = orig_log
        app._download_kbars_raw = orig_dl
        app.QT_PREFETCH_SYNC = orig_sync
        app.__dict__.pop('QT_PREFETCH_RETRIES', None)
        app.__dict__.pop('QT_PREFETCH_PACE_SEC', None)
        app._qt_running, app.api_logged_in, app.sj_api = orig_running, orig_login, orig_api
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts
        app._kbars_raw_cache.clear()
        app._qt_prefetch_noted = {}
        app._qt_prefetch_inflight = set()
        app._qt_prefetch_errors = {}


run_case("ADR-127: 日K快取用『有沒有跨過開盤』判斷 + 補資料通知每天只一次",
         _day_cache_session_freshness_127)


def _chukuangren_eval_cadence_128():
    """【ADR-128】終極波段的「5分K」不是訊號週期,是被借用的評估節拍器。

    使用者實機回報:「我明明設定就是看日K,為什麼哪裡有看5分K?到底是哪裡有問題?」

    查證結果:
      * 訊號一直是日K —— `tf='日K'` 寫死在 _quant_eval_pass 的呼叫端。
      * 但 watch_timeframe 被寫死成 '5分K' (core 與編輯器各一處),用途是驅動
        K 棒邊界節拍,好讓迴圈踩進 12:00~12:04 的二次確認窗口。
      * 終極波段編輯器裡沒有這個欄位 → 使用者看不到也改不了。

    這個案例走**完整的 GUI 路徑**,守住三件事:
      1. 節拍改走「日K 類」(10 分鐘對齊),不再是 5 分鐘。
      2. 12:00 二次確認搬到獨立通道後**仍然會發生**,而且**當天不再只有一次
         機會** —— 這是舊做法真正的隱患:5分K 邊界在 12:00 觸發一次就被
         _qt_last_boundary 記住,下一次是 12:05 (窗口外),那一次抓不到資料
         就整天無聲丟掉。
      3. 反向對照:窗口外不可以確認;一般策略的節拍與週期不可以被改壞。

    時鐘一律用 patch 控制,不依賴真實時間 (P-94/P-97)。
    """
    import pandas as _pd
    from core import strategy_engine as _se8
    from core import chukuangren_band as _ck8

    class FakeContract:
        code = 'TXF'
        symbol = 'TXFR1'

    # 加權指數日K:收盤一路走高到 120 > X=100 → on_daily_close 會記 pending_entry
    _n = 40
    _daily = _pd.DataFrame(
        {'Open': [80.0 + i for i in range(_n)], 'High': [85.0 + i for i in range(_n)],
         'Low': [78.0 + i for i in range(_n)], 'Close': [81.0 + i for i in range(_n)],
         'Volume': [1000] * _n},
        index=_pd.bdate_range(end=stock_app_pro.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0), periods=_n))

    logs = []
    orig_log = app.log_message
    orig_resolve = app._qt_resolve
    orig_resolve_watch = app._qt_resolve_watch
    orig_fetch = app._qt_fetch_closed_bars
    orig_open = stock_app_pro.market_session.is_market_open
    orig_just = stock_app_pro.market_session.just_opened
    orig_window = _ck8.in_noon_confirm_window
    orig_running, orig_login, orig_api = app._qt_running, app.api_logged_in, app.sj_api
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes

    fetches = []          # 每次 _qt_fetch_closed_bars 的週期,用來斷言「抓了什麼」
    intraday_ok = {'v': True}     # False = 模擬 5分K 還抓不到 (背景補齊中/空表)
    today_str_dummy = f"{stock_app_pro.datetime.now():%Y-%m-%d}"

    def _fake_fetch(_s, _c, _a, tf=None, cache_sym=None, cache_market=None, **_k):
        fetches.append(tf)
        if tf == '日K':
            return _daily
        if not intraday_ok['v']:
            return None                     # 模擬資料還沒到
        return _pd.DataFrame({'Close': [120.0]},
                             index=[stock_app_pro.datetime.now()])

    def _mk():
        s = _ck8.default_strategy()
        s.update({'symbol': 'TXF', 'trade_type': '期貨', 'market': '台期貨',
                  'qty': 1, 'direction': '做多', 'mode': '模擬', 'enabled': True,
                  'ck_x': 100.0, 'ck_y': 90.0, 'ck_z': 92.0, 'ck_c': 5.0, 'ck_f': 2.0})
        return s

    def _mount(s):
        rt = _se8.new_runtime()
        app.strategies = [s]
        app.strategy_runtimes = {s['id']: rt}
        app._qt_last_boundary = {}
        app._qt_session_state = {}
        return rt

    try:
        app.log_message = lambda m: (logs.append(m), orig_log(m))[0]
        app._qt_resolve = lambda _s: (FakeContract(), 'futures')
        app._qt_resolve_watch = lambda _s: (FakeContract(), 'index_tw', '^TWII', '台股')
        app._qt_fetch_closed_bars = _fake_fetch
        app.sj_api = object(); app.api_logged_in = True; app._qt_running = True
        stock_app_pro.market_session.is_market_open = lambda *a, **k: True
        stock_app_pro.market_session.just_opened = lambda *a, **k: False

        # ---- 1. 介面回答真話:訊號週期是日K,即使存檔殘留 '5分K' ----
        ck = _mk()
        ck['watch_timeframe'] = '5分K'          # 模擬使用者現有的舊策略檔
        assert _se8.watch_timeframe_of(ck) == '日K', \
            f"終極波段的訊號週期應是日K,實際 {_se8.watch_timeframe_of(ck)}"

        # ---- 2. 節拍走「日K 類」(10 分鐘對齊),不是 5 分鐘 ----
        rt = _mount(ck)
        fetches.clear()
        eval_pass(); app.flush_after()
        _bkey = app._qt_last_boundary.get(ck['id'])
        assert _bkey, "評估後應該記下邊界"
        _bmin = stock_app_pro.datetime.fromisoformat(_bkey).minute
        assert _bmin % 10 == 0, \
            f"日K 類的邊界應對齊 10 分鐘 (5分K 節拍的殘留會對齊 5 分鐘): {_bkey}"
        assert '日K' in fetches, f"終極波段的訊號一定要抓日K,實際抓了 {fetches}"
        assert rt.get('pending_entry'), \
            f"日K 收盤 120 > X=100 應記下待確認進場訊號 (實際 rt={ {k: rt[k] for k in ('pending_entry','last_daily_bar_date')} })"

        # ---- 3. 反向對照:窗口外**不可以**確認 ----
        _ck8.in_noon_confirm_window = lambda _dt: False
        fetches.clear()
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert not rt.get('last_confirm_date'), \
            "12:00 窗口外不可以做二次確認 (否則等於隨時都在確認)"
        assert '5分K' not in fetches, \
            f"窗口外連 5分K 都不該去抓 (省 API),實際 {fetches}"

        # ---- 4. 窗口內:獨立通道**必須**真的完成確認 ----
        # 少了這條,把 in_noon_confirm_window 寫成「永遠 False」也會讓第 3 條綠。
        _ck8.in_noon_confirm_window = lambda _dt: True
        fetches.clear(); logs.clear()
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert '5分K' in fetches, f"窗口內必須抓 5分K 當確認價,實際 {fetches}"
        assert rt.get('last_confirm_date'), "窗口內必須真的呼叫 on_noon_confirm"
        assert rt.get('armed_intent'), \
            "指數 120 > X=100,進場確認應成立並記下 armed_intent"
        assert [m for m in logs if '12:00確認成立' in m], \
            f"確認成立要有日誌讓使用者看得到: {logs[:3]}"

        # ---- 5. 同一天不可以重複確認 (既有防重複沒被弄壞) ----
        # 【第一版是空殼斷言,突變測試抓到的】原本只是「確認成功後再呼叫一次,
        # 斷言沒有多抓」—— 但 on_noon_confirm 成立後會把 pending_entry 清成 None,
        # 於是第二次在**最前面那道「有沒有待確認訊號」**就 continue 了,根本走不到
        # 防重複那一行。把兩層 last_confirm_date 檢查全部拿掉,斷言照樣綠
        # (同 P-28:要問「它是因為我想測的那個原因才通過的嗎」)。
        # 所以這裡刻意**手動把狀態擺回**「有待確認訊號 + 當天已確認過」,
        # 直接測那道守門。
        rt['pending_entry'] = {'dir': 'LONG', 'date': today_str_dummy}
        _n_before = len(fetches)
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert len(fetches) == _n_before, \
            f"同一天已確認過就不該再抓資料 (實際多抓了 {len(fetches) - _n_before} 次)"
        rt['pending_entry'] = None

        # ---- 6. 【本次真正的隱患】資料還沒到時,當天必須還有第二次機會 ----
        # 舊做法:5分K 邊界在 12:00 觸發一次 → _qt_last_boundary 記住 → 下一次
        # 是 12:05,已在窗口外 → 當天的二次確認整個無聲丟掉。
        ck2 = _mk(); ck2['name'] = '診斷ADR128重試'
        rt2 = _mount(ck2)
        eval_pass(); app.flush_after()               # 先讓 on_daily_close 記下 pending
        assert rt2.get('pending_entry'), "前置條件:應先有待確認進場訊號"
        intraday_ok['v'] = False                     # 第一次:5分K 還抓不到
        fetches.clear()
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert '5分K' in fetches, "第一次應該有試著抓 5分K"
        assert not rt2.get('last_confirm_date'), \
            "資料還沒到就不可以算「已確認」(否則當天的機會被吃掉)"
        intraday_ok['v'] = True                      # 下一個 runner tick:資料到了
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert rt2.get('last_confirm_date'), \
            "資料到了之後,同一天必須還能完成確認 (舊做法一天只有一次機會)"
        assert rt2.get('armed_intent'), "重試成功後 armed_intent 應該記下來"

        # ---- 7. 【ADR-124】時段閘門對這條新通道也要有效 ----
        ck3 = _mk(); ck3['name'] = '診斷ADR128閘門'
        rt3 = _mount(ck3)
        eval_pass(); app.flush_after()
        assert rt3.get('pending_entry'), "前置條件:應先有待確認進場訊號"
        stock_app_pro.market_session.is_market_open = lambda *a, **k: False
        fetches.clear()
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert not rt3.get('last_confirm_date'), \
            "市場關閉時這條路徑也不該確認 (三條會動到部位的路徑要同一套閘門)"
        # 正控:閘門打開後同一檔策略必須真的確認 —— 否則上面那條是空殼
        stock_app_pro.market_session.is_market_open = lambda *a, **k: True
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert rt3.get('last_confirm_date'), \
            "閘門打開後必須真的確認 (否則第 7 條的前半是空殼斷言)"

        # ---- 8. 反向對照:一般策略的週期與節拍不可以被改壞 ----
        gen = _se8.new_strategy()
        gen.update({'watch_enabled': True, 'watch_symbol': '^TWII',
                    'watch_timeframe': '5分K', 'timeframe': '5分K'})
        assert _se8.watch_timeframe_of(gen) == '5分K', \
            f"一般策略的訊號週期不可以被覆寫成日K (實際 {_se8.watch_timeframe_of(gen)})"

        # ---- 9. 原始碼層級:寫死的 '5分K' 不可以再出現在這兩處 ----
        # 純函式測不到「編輯器存檔時有沒有真的寫對」(P-64)。
        _src = open('stock_app_pro.py', encoding='utf-8').read()
        assert "s['watch_timeframe'] = '5分K'" not in _src, \
            "終極波段編輯器不可以再把 watch_timeframe 寫死成 '5分K'"
        _csrc = open('core/chukuangren_band.py', encoding='utf-8').read()
        assert "s['watch_timeframe'] = '5分K'" not in _csrc, \
            "chukuangren_band.default_strategy() 不可以再寫死 '5分K'"

        # ---- 10. runner 必須真的接上這條通道 ----
        # 上面每一條都是**直接呼叫** _qt_chukuangren_confirm_pass(),所以完全測不到
        # 「production 有沒有人去呼叫它」—— 把 runner 那一行刪掉,1~9 全都還是綠,
        # 但 12:00 確認一輩子不會發生 (P-64 的教訓)。
        import inspect as _insp8
        _runner_src = _insp8.getsource(stock_app_pro.StockTradingAppPro.quant_runner_worker)
        assert '_qt_chukuangren_confirm_pass' in _runner_src, \
            "quant_runner_worker 必須呼叫 _qt_chukuangren_confirm_pass,否則這條通道永遠不會跑"
        # 而且必須排在送單之前 (確認 → 60 秒後送單)
        assert _runner_src.index('_qt_chukuangren_confirm_pass') \
            < _runner_src.index('_qt_chukuangren_execute_pass'), \
            "確認要排在送單之前"
    finally:
        app.log_message = orig_log
        app._qt_resolve = orig_resolve
        app._qt_resolve_watch = orig_resolve_watch
        app._qt_fetch_closed_bars = orig_fetch
        _ck8.in_noon_confirm_window = orig_window
        stock_app_pro.market_session.is_market_open = orig_open
        stock_app_pro.market_session.just_opened = orig_just
        app._qt_running, app.api_logged_in, app.sj_api = orig_running, orig_login, orig_api
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts
        app._qt_last_boundary = {}
        app._qt_session_state = {}
        app.__dict__.pop('_qt_confirm_err_noted', None)


run_case("ADR-128: 終極波段的訊號週期回到日K + 12:00確認獨立通道 (當天可重試)",
         _chukuangren_eval_cadence_128)


def _merged_stop_level_129():
    """【ADR-129】停損的「觸發」與「隔天12:00 確認」合併成同一個點位。

    使用者實機回報 (截圖:做空 X=42500、S1=41701、S2=41700):
        「這兩個應該是要一樣,不需要分兩個,只需填一個 S1 就可以。(多單也是一樣)」

    這個案例走**完整的 GUI 路徑** (_quant_eval_pass → _qt_chukuangren_confirm_pass),
    因為 core 的單元測試是拿手寫的 params dict 測狀態機,**測不到「GUI 有沒有經過
    params_of() 去拿參數」** —— 而覆寫就發生在 params_of() 裡 (P-64)。
    """
    import pandas as _pd
    from core import strategy_engine as _se9
    from core import chukuangren_band as _ck9

    class FakeContract:
        code = 'TXF'
        symbol = 'TXFR1'

    # 加權指數日K:最後一根收 85,低於 Y=90 → on_daily_close 記下待確認停損
    _n = 40
    _daily = _pd.DataFrame(
        {'Open': [100.0] * _n, 'High': [102.0] * _n, 'Low': [84.0] * _n,
         'Close': [100.0] * (_n - 1) + [85.0], 'Volume': [1000] * _n},
        index=_pd.bdate_range(end=stock_app_pro.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0), periods=_n))

    confirm_px = {'v': 91.0}
    orig_log = app.log_message
    orig_resolve = app._qt_resolve
    orig_resolve_watch = app._qt_resolve_watch
    orig_fetch = app._qt_fetch_closed_bars
    orig_open = stock_app_pro.market_session.is_market_open
    orig_just = stock_app_pro.market_session.just_opened
    orig_window = _ck9.in_noon_confirm_window
    orig_running, orig_login, orig_api = app._qt_running, app.api_logged_in, app.sj_api
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes

    def _fake_fetch(_s, _c, _a, tf=None, cache_sym=None, cache_market=None, **_k):
        if tf == '日K':
            return _daily
        return _pd.DataFrame({'Close': [confirm_px['v']]},
                             index=[stock_app_pro.datetime.now()])

    def _mount(**over):
        """掛一檔『舊策略檔』:Y=90 但殘留 Z=92 (合併前使用者可能填的兩個值)。"""
        s = _ck9.default_strategy()
        s.update({'name': '診斷ADR129', 'symbol': 'TXF', 'trade_type': '期貨',
                  'market': '台期貨', 'qty': 2, 'direction': '做多', 'mode': '模擬',
                  'enabled': True, 'session_gate': False,
                  'ck_x': 100.0, 'ck_y': 90.0, 'ck_z': 92.0,
                  'ck_s1': 110.0, 'ck_s2': 108.0, 'ck_c': 5.0, 'ck_f': 2.0})
        s.update(over)
        rt = _se9.new_runtime()
        rt.update({'state': 'LONG', 'qty': 2, 'entry_price': 100.0,
                   'exec_entry_price': 100.0, 'entry_index_price': 100.0})
        app.strategies = [s]
        app.strategy_runtimes = {s['id']: rt}
        app._qt_last_boundary = {}
        app._qt_session_state = {}
        return s, rt

    try:
        app.log_message = lambda m: (None, orig_log(m))[0]
        app._qt_resolve = lambda _s: (FakeContract(), 'futures')
        app._qt_resolve_watch = lambda _s: (FakeContract(), 'index_tw', '^TWII', '台股')
        app._qt_fetch_closed_bars = _fake_fetch
        app.sj_api = object(); app.api_logged_in = True; app._qt_running = True
        stock_app_pro.market_session.is_market_open = lambda *a, **k: True
        stock_app_pro.market_session.just_opened = lambda *a, **k: False
        _ck9.in_noon_confirm_window = lambda _dt: True

        # ---- 1. 【行為差異的核心】指數 91:站回 Y=90 之上 → 不可以平倉 ----
        # 舊行為:91 < Z=92 → 平倉。這是合併前後唯一看得出差別的地方。
        s1_, rt1 = _mount()
        confirm_px['v'] = 91.0
        eval_pass(); app.flush_after()
        assert rt1.get('pending_exit'), \
            f"前置條件:日K 收 85 < Y=90,應記下待確認停損 (實際 {rt1.get('pending_exit')})"
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert rt1.get('last_confirm_date'), "前置條件:確認流程應該有跑到"
        assert not rt1.get('armed_intent'), \
            "指數 91 已站回 Y=90 之上,不可以平倉 (舊行為會因為 91 < 殘留的 Z=92 而平倉)"
        assert rt1['state'] == 'LONG', f"不該平倉,state 應維持 LONG (實際 {rt1['state']})"

        # ---- 2. 正控:真的跌破 Y 就必須平倉 ----
        # 少了這條,把停損整個關掉也會讓第 1 條綠 —— 那是嚴重得多的問題。
        s2_, rt2 = _mount()
        confirm_px['v'] = 89.0
        eval_pass(); app.flush_after()
        assert rt2.get('pending_exit'), "前置條件:應記下待確認停損"
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert rt2.get('armed_intent'), \
            "指數 89 < Y=90,停損確認要成立 (否則等於把停損關掉了)"
        assert rt2['armed_intent']['action'] == '賣出'

        # ---- 3. 舊檔的殘留值不可以有任何影響 (不做資料遷移) ----
        # 把 Z 改成極端值,結果必須跟第 1 條完全一樣。
        s3_, rt3 = _mount(ck_z=99999.0)
        confirm_px['v'] = 91.0
        eval_pass(); app.flush_after()
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert not rt3.get('armed_intent'), \
            "殘留的 Z=99999 不可以生效 (若生效,91 < 99999 會誤平倉)"

        # ---- 4. 原始碼層級:編輯器只留一個停損欄位 + 有鏡射 ----
        _src = open('stock_app_pro.py', encoding='utf-8').read()
        assert "_mk_param_rows(pcontainer, ('y',))" in _src, \
            "做多方向的編輯器應只剩一個停損欄位 Y"
        assert "_mk_param_rows(pcontainer, ('s1',))" in _src, \
            "做空方向的編輯器應只剩一個停損欄位 S1"
        assert "chukuangren_band.MERGED_STOP_PAIRS" in _src, \
            "_collect() 要把確認點位鏡射成觸發點位,存檔資料才不自相矛盾"
        # 而且不可以再用 param_entries[k] 直取 —— z/s2 已經沒有輸入框,會 KeyError
        assert "param_entries[k].get()" not in _src, \
            "z/s2 沒有輸入框了,_collect() 不可以再用 param_entries[k] 直取 (會 KeyError)"

        # ---- 5. 【ADR-130】使用者截圖那組參數現在必須存得起來 ----
        # ADR-129 時這裡斷言的是「應該被擋」;使用者接著指出那個限制本身是錯的
        # (實際進場價不是 X,停損要貼著實際進場價),所以 ADR-130 拿掉了相對關係
        # 的檢查。同一組數字、相反的期待 —— 這一行就是那次規則變更的紀錄。
        s5 = _ck9.default_strategy()
        s5.update({'symbol': 'TXF', 'trade_type': '期貨', 'market': '台期貨', 'qty': 5,
                   'direction': '做空', 'ck_x': 42500.0, 'ck_s1': 41701.0,
                   'ck_c': 1000.0, 'ck_f': 200.0})
        s5.pop('ck_s2', None)
        _ok, _msg = _ck9.validate(s5)
        assert _ok, f"停損點位與 X 的高低不該再被限制 (使用者要求自行設定): {_msg}"
        # 反向對照:「有沒有填」還是要擋 —— S1=0 會讓系統每天誤判待確認停損
        s5['ck_s1'] = 0.0
        _ok2, _msg2 = _ck9.validate(s5)
        assert not _ok2 and 'S1' in _msg2, \
            f"S1=0 等於壞資料,必須擋下來 (ok={_ok2}, msg={_msg2})"
    finally:
        app.log_message = orig_log
        app._qt_resolve = orig_resolve
        app._qt_resolve_watch = orig_resolve_watch
        app._qt_fetch_closed_bars = orig_fetch
        _ck9.in_noon_confirm_window = orig_window
        stock_app_pro.market_session.is_market_open = orig_open
        stock_app_pro.market_session.just_opened = orig_just
        app._qt_running, app.api_logged_in, app.sj_api = orig_running, orig_login, orig_api
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts
        app._qt_last_boundary = {}
        app._qt_session_state = {}
        app.__dict__.pop('_qt_confirm_err_noted', None)


run_case("ADR-129: 停損觸發/確認合併成一個點位 (舊檔殘留值不生效)",
         _merged_stop_level_129)


def _stop_level_free_of_x_130():
    """【ADR-130】停損點位與進出場分界 X 的相對關係不再限制。

    使用者的要求:
      「有時候停損點的位置不一定會高於進場點,因為實際進場的位置,已經離一開始
        設定的進場位置太遠,導致虧損會很大。所以你說停損點必須大於進出場分界,
        這一點不要被限制,是允許我自行設定,你只要依照我設定的停損位置去判斷
        是否符合條件即可。」

    情境 (做空):X=42500 設好之後,指數跌破 42500、隔天 12:00 確認時已經到
    41800 才真正進場。停損若被迫設在 42500 之上,等於容忍 700 點以上的虧損;
    使用者要設 42000 (在 X 之下、在實際進場價之上) 才貼著實際風險。

    走**完整的 GUI 路徑**:核心單元測試只能證明狀態機照 S1 判斷,證明不了
    「GUI 的啟用閘門會不會又擋一次」(P-64)。
    """
    import pandas as _pd
    from core import strategy_engine as _se10
    from core import chukuangren_band as _ck10

    class FakeContract:
        code = 'TXF'
        symbol = 'TXFR1'

    X_LEVEL, S1_LEVEL, ENTRY_IDX = 42500.0, 42000.0, 41800.0
    confirm_px = {'v': 42100.0}
    daily_close = {'v': 42100.0}

    orig_log = app.log_message
    orig_resolve = app._qt_resolve
    orig_resolve_watch = app._qt_resolve_watch
    orig_fetch = app._qt_fetch_closed_bars
    orig_open = stock_app_pro.market_session.is_market_open
    orig_just = stock_app_pro.market_session.just_opened
    orig_window = _ck10.in_noon_confirm_window
    orig_running, orig_login, orig_api = app._qt_running, app.api_logged_in, app.sj_api
    orig_strats, orig_rts = app.strategies, app.strategy_runtimes

    def _daily():
        _n = 40
        _c = [daily_close['v']] * _n
        return _pd.DataFrame(
            {'Open': _c, 'High': [c + 20 for c in _c], 'Low': [c - 20 for c in _c],
             'Close': _c, 'Volume': [1000] * _n},
            index=_pd.bdate_range(end=stock_app_pro.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0), periods=_n))

    def _fake_fetch(_s, _c, _a, tf=None, cache_sym=None, cache_market=None, **_k):
        if tf == '日K':
            return _daily()
        return _pd.DataFrame({'Close': [confirm_px['v']]},
                             index=[stock_app_pro.datetime.now()])

    def _mk(**over):
        s = _ck10.default_strategy()
        s.update({'name': '診斷ADR130', 'symbol': 'TXF', 'trade_type': '期貨',
                  'market': '台期貨', 'qty': 5, 'direction': '做空', 'mode': '模擬',
                  'enabled': True, 'session_gate': False,
                  'ck_x': X_LEVEL, 'ck_s1': S1_LEVEL, 'ck_c': 1000.0, 'ck_f': 200.0})
        s.update(over)
        return s

    def _mount(s):
        rt = _se10.new_runtime()
        rt.update({'state': 'SHORT', 'qty': 5, 'entry_price': ENTRY_IDX,
                   'exec_entry_price': ENTRY_IDX, 'entry_index_price': ENTRY_IDX})
        app.strategies = [s]
        app.strategy_runtimes = {s['id']: rt}
        app._qt_last_boundary = {}
        app._qt_session_state = {}
        return rt

    try:
        app.log_message = lambda m: (None, orig_log(m))[0]
        app._qt_resolve = lambda _s: (FakeContract(), 'futures')
        app._qt_resolve_watch = lambda _s: (FakeContract(), 'index_tw', '^TWII', '台股')
        app._qt_fetch_closed_bars = _fake_fetch
        app.sj_api = object(); app.api_logged_in = True; app._qt_running = True
        stock_app_pro.market_session.is_market_open = lambda *a, **k: True
        stock_app_pro.market_session.just_opened = lambda *a, **k: False
        _ck10.in_noon_confirm_window = lambda _dt: True

        # ---- 1. 存檔驗證:S1 在 X 之下不可以再被擋 ----
        s1_ = _mk()
        _ok, _msg = _ck10.validate(s1_)
        assert _ok, f"S1({S1_LEVEL:g}) < X({X_LEVEL:g}) 不該再被擋: {_msg}"

        # ---- 2. 啟用閘門 (含 Telegram /on 走的同一條) 也不可以再擋 ----
        _mount(s1_)
        _ok2, _why2, _pend2 = app._qt_enable_blockers(s1_)
        assert _ok2, f"啟用閘門不該再擋這組參數: {_why2}"

        # ---- 3. 停損判斷只看 S1:指數 42100 > S1=42000 → 停損 (不必漲過 X) ----
        rt = _mount(_mk())
        daily_close['v'] = 42100.0
        confirm_px['v'] = 42100.0
        eval_pass(); app.flush_after()
        assert rt.get('pending_exit') and rt['pending_exit']['reason'] == 'SL', \
            f"42100 已突破 S1={S1_LEVEL:g},要記待確認停損 (實際 {rt.get('pending_exit')})"
        app._qt_chukuangren_confirm_pass(); app.flush_after()
        assert rt.get('armed_intent'), "隔天12:00 仍高於 S1 → 應確認平倉"
        assert rt['armed_intent']['action'] == '買進', "空單平倉是買進"

        # ---- 4. 反向對照:還在 S1 之下就不可以停損 ----
        # 少了這條,把停損寫成「永遠觸發」也會讓第 3 條綠 —— 那會每天亂平倉。
        rt2 = _mount(_mk())
        daily_close['v'] = 41900.0
        confirm_px['v'] = 41900.0
        eval_pass(); app.flush_after()
        assert not rt2.get('pending_exit'), \
            f"41900 還在 S1={S1_LEVEL:g} 之下,不該停損 (實際 {rt2.get('pending_exit')})"
        assert rt2['state'] == 'SHORT'

        # ---- 5. 進場仍然只看 X (這次只放寬停損,不可以連進場一起弄鬆) ----
        s5 = _mk()
        rt5 = _se10.new_runtime()          # FLAT
        app.strategies = [s5]; app.strategy_runtimes = {s5['id']: rt5}
        app._qt_last_boundary = {}; app._qt_session_state = {}
        daily_close['v'] = 42600.0         # 在 X 之上 → 做空不該進場
        eval_pass(); app.flush_after()
        assert not rt5.get('pending_entry'), \
            "42600 在 X 之上,做空不該記進場訊號 (進場條件不可以被一起放寬)"
        rt5b = _se10.new_runtime()
        app.strategy_runtimes = {s5['id']: rt5b}
        app._qt_last_boundary = {}
        daily_close['v'] = 42400.0         # 跌破 X → 該記進場
        eval_pass(); app.flush_after()
        assert rt5b.get('pending_entry'), "42400 跌破 X,做空應記進場訊號 (正控)"

        # ---- 6. 「有沒有填」仍然要擋 (0 是壞資料,不是「不設停損」) ----
        _ok6, _msg6 = _ck10.validate(_mk(ck_s1=0.0))
        assert not _ok6 and 'S1' in _msg6, \
            f"S1=0 會讓「收盤 > 0」永遠成立 → 每天誤判待確認停損,必須擋 ({_msg6})"
        _ok7, _msg7 = _ck10.validate(_mk(direction='做多', ck_y=0.0, ck_s1=S1_LEVEL))
        assert not _ok7 and 'Y' in _msg7, \
            f"Y=0 等於完全沒有停損,必須擋 ({_msg7})"

        # ---- 7. 原始碼層級:相對關係的檢查必須真的被拿掉 ----
        _csrc = open('core/chukuangren_band.py', encoding='utf-8').read()
        for _dead in ("p['y'] >= p['x']", "p['s1'] <= p['x']"):
            assert _dead not in _csrc, f"停損與 X 的相對關係檢查應已移除,但還找得到 {_dead}"
    finally:
        app.log_message = orig_log
        app._qt_resolve = orig_resolve
        app._qt_resolve_watch = orig_resolve_watch
        app._qt_fetch_closed_bars = orig_fetch
        _ck10.in_noon_confirm_window = orig_window
        stock_app_pro.market_session.is_market_open = orig_open
        stock_app_pro.market_session.just_opened = orig_just
        app._qt_running, app.api_logged_in, app.sj_api = orig_running, orig_login, orig_api
        app.strategies, app.strategy_runtimes = orig_strats, orig_rts
        app._qt_last_boundary = {}
        app._qt_session_state = {}
        app.__dict__.pop('_qt_confirm_err_noted', None)


run_case("ADR-130: 停損點位不受進出場分界 X 限制 (但仍要真的填)",
         _stop_level_free_of_x_130)


def _bollinger_two_sets_131():
    """【ADR-131】主圖布林:參數列不見了 + 改成兩組完整通道。

    使用者實機截圖:「主圖指標參數設定」裡看得到 MA1~MA6,**布林那一整列
    卻不見了**,只剩一行說明文字、旁邊露出半截「(σ2=0不畫第二組)」跟一個
    沒有標題的色彩下拉。

    根因是 grid 版面撞格:說明文字那一行寫成 `row=8, columnspan=4`,跟布林的
    checkbox / 期間 / σ1 / σ2 **同一個 grid 儲存格**,而 tkinter 的 grid 是
    後放的蓋前放的。從第一版就是這樣。

    這個案例守兩件事:
      A. **版面**:用 AST 靜態算出 open_main_settings 裡每個 grid 呼叫佔用的
         儲存格,任何兩個 widget 不可以佔到同一格。這是那個 bug 的**類別**,
         不是那一行字串 —— 換個位置再撞一次照樣會抓到。
      B. **功能**:兩組布林各自獨立 (中線類型/期間 + 兩條上下線),而且第1組
         的欄位名沒有被改掉。
    """
    import ast as _ast
    import pandas as _pd
    from core import indicators as _ind

    # ---- A. grid 儲存格不可以重疊 ----
    _src = open('stock_app_pro.py', encoding='utf-8').read()
    _tree = _ast.parse(_src)
    _fn = None
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.FunctionDef) and _node.name == 'open_main_settings':
            _fn = _node
            break
    assert _fn is not None, "找不到 open_main_settings"

    def _lit(kw, name, default):
        for k in kw:
            if k.arg == name:
                if isinstance(k.value, _ast.Constant) and isinstance(k.value.value, int):
                    return k.value.value
                return None          # 非字面值 (例如迴圈裡的 row=i+1) → 跳過
        return default

    occupied = {}
    for _node in _ast.walk(_fn):
        if not (isinstance(_node, _ast.Call) and isinstance(_node.func, _ast.Attribute)
                and _node.func.attr == 'grid'):
            continue
        row = _lit(_node.keywords, 'row', None)
        col = _lit(_node.keywords, 'column', 0)
        cspan = _lit(_node.keywords, 'columnspan', 1)
        rspan = _lit(_node.keywords, 'rowspan', 1)
        if row is None or col is None or cspan is None or rspan is None:
            continue                  # 迴圈產生的位置不在這條規則的範圍內
        for r in range(row, row + rspan):
            for c in range(col, col + cspan):
                if (r, c) in occupied:
                    raise AssertionError(
                        f"主圖指標設定的 grid 撞格:第 {_node.lineno} 行與第 "
                        f"{occupied[(r, c)]} 行都佔到 (row={r}, column={c}) —— "
                        "tkinter 會讓後放的蓋掉前放的,參數就會像使用者截圖那樣不見")
                occupied[(r, c)] = _node.lineno
    assert occupied, "應該解析得到 grid 呼叫 (解析不到就等於這條檢查是空殼)"

    # ---- B. 兩組布林的功能 ----
    for _v in ('bb_type', 'bb2_show', 'bb2_type', 'bb2_period', 'bb2_std1',
               'bb2_std2', 'bb2_color'):
        assert hasattr(app, _v), f"缺少第2組布林的設定變數 {_v}"

    _n = 90
    _close = [100.0 + (i % 7) - 3 for i in range(_n)]
    _df = _pd.DataFrame(
        {'Open': _close, 'High': [c + 2 for c in _close],
         'Low': [c - 2 for c in _close], 'Close': _close,
         'Volume': [1000] * _n},
        index=_pd.date_range('2026-01-01', periods=_n, freq='D'))

    _orig = {k: getattr(app, k).get() for k in
             ('bb_show', 'bb_period', 'bb_std1', 'bb_std2', 'bb_type',
              'bb2_show', 'bb2_period', 'bb2_std1', 'bb2_std2', 'bb2_type')}
    try:
        # 只開第1組:第2組欄位不可以出現 (反向對照 —— 否則圖上會多出線)
        app.bb_show.set(True); app.bb_period.set(20)
        app.bb_std1.set(2.0); app.bb_std2.set(3.0); app.bb_type.set('SMA')
        app.bb2_show.set(False)
        out1 = app.calculate_custom_indicators(_df)
        for c in ('BB_MID', 'BB_UPPER', 'BB_LOWER', 'BB_UPPER2', 'BB_LOWER2'):
            assert c in out1.columns, f"第1組布林欄位 {c} 不見了 (繪圖與十字線都靠它)"
        assert 'BB2_MID' not in out1.columns, "沒開第2組就不該有 BB2 欄位"

        # 兩組都開:各自獨立的中線
        app.bb2_show.set(True); app.bb2_period.set(60)
        app.bb2_std1.set(1.5); app.bb2_std2.set(2.5); app.bb2_type.set('EMA')
        out2 = app.calculate_custom_indicators(_df)
        for c in ('BB2_MID', 'BB2_UPPER', 'BB2_LOWER', 'BB2_UPPER2', 'BB2_LOWER2'):
            assert c in out2.columns, f"第2組布林欄位 {c} 沒有算出來"
        assert not out2['BB_MID'].equals(out2['BB2_MID']), \
            "兩組中線 (SMA20 / EMA60) 算出來一樣,等於第2組的參數沒有生效"
        # 上下線確實照各自的 σ 展開
        import numpy as _np
        _i = -1
        assert abs((out2['BB_UPPER'].iloc[_i] - out2['BB_MID'].iloc[_i])
                   - 2.0 * out2['BB_STD'].iloc[_i]) < 1e-6, "第1組 σa 不對"
        assert abs((out2['BB2_UPPER'].iloc[_i] - out2['BB2_MID'].iloc[_i])
                   - 1.5 * out2['BB2_STD'].iloc[_i]) < 1e-6, "第2組 σa 不對"

        # σb=0 → 該組只畫一對上下線
        app.bb2_std2.set(0.0)
        out3 = app.calculate_custom_indicators(_df)
        assert 'BB2_UPPER2' not in out3.columns, "σb=0 不該畫第二條上下線"
        assert 'BB2_UPPER' in out3.columns, "σb=0 只影響第二條,第一條仍要在"

        # 只開第2組:第1組關著也要能單獨畫
        app.bb_show.set(False)
        app.__dict__.pop('_bb_param_warned', None)
        out4 = app.calculate_custom_indicators(_df)
        assert 'BB2_MID' in out4.columns, "第1組關著時,第2組仍要能畫"
    finally:
        for k, v in _orig.items():
            getattr(app, k).set(v)
        app.__dict__.pop('_bb_param_warned', None)


run_case("ADR-131: 主圖布林參數列被蓋掉 (grid撞格) + 兩組完整布林通道",
         _bollinger_two_sets_131)


def _regime_daily_notify_132():
    """【ADR-132】每天收盤後把盤勢判斷推播到手機,含 60分K 且要標明週期。

    使用者要求:
      「主圖盤勢判斷,如果有出現相關盤勢,要用傳訊息到我的手機通知我,跟我說
        是什麼型態或是支撐壓力。每天一次,收盤後通知即可。另外,也額外增加對
        60分K 的盤勢判斷 ... 也一併要通知我,而且要註明是 60分K 出現。」

    走**完整的 GUI 路徑** (_regime_daily_notify_pass → _qt_resolve_watch →
    _qt_fetch_closed_bars → _send_telegram_async)。純函式測不到:
      * 推播有沒有真的被送出去 (而不是只寫進日誌)
      * 有沒有繞過「自動交易總開關」那道推播閘門
      * 收盤後有沒有把**今天**那根K棒算進去
    """
    import pandas as _pd
    from core import regime_panel as _rp

    class FakeContract:
        code = 'TSE'
        symbol = '^TWII'

    sent = []
    logs = []
    fetched = []
    orig_log = app.log_message
    orig_send = app._send_telegram_async
    orig_resolve_watch = app._qt_resolve_watch
    orig_fetch = app._qt_fetch_closed_bars
    orig_cfg = getattr(app, 'telegram_cfg', None)
    orig_login, orig_api = app.api_logged_in, app.sj_api
    orig_running = app._qt_running
    orig_settings = app.regime_settings
    orig_last = getattr(app, '_regime_notify_last_date', None)

    # 造一段走勢明確的加權指數資料,確保 evaluate_all 一定生得出「盤勢」訊號
    def _mk(n=160, base=20000.0, step=25.0):
        closes = [base + i * step for i in range(n)]
        return _pd.DataFrame(
            {'Open': closes, 'High': [c + 30 for c in closes],
             'Low': [c - 30 for c in closes], 'Close': closes,
             'Volume': [10000 + (i % 5) * 100 for i in range(n)]},
            index=_pd.date_range('2026-01-01', periods=n, freq='D'))

    DF = _mk()

    def _fake_fetch(_s, _c, _a, tf=None, cache_sym=None, cache_market=None,
                    allow_blocking=False, drop_last=True):
        fetched.append((tf, drop_last))
        return DF

    try:
        app.log_message = lambda m: (logs.append(str(m)), orig_log(m))[0]
        app._send_telegram_async = lambda t: sent.append(str(t))
        app._qt_resolve_watch = lambda _s: (FakeContract(), 'index_tw', '^TWII', '台股')
        app._qt_fetch_closed_bars = _fake_fetch
        app.telegram_cfg = {'bot_token': 'T', 'chat_id': 'C'}
        app.api_logged_in = True
        app.sj_api = object()
        # 【關鍵】自動交易總開關**關著** —— 盤勢推播是看盤輔助,不該綁在
        # 自動交易上。這一行就是在守那件事。
        app._qt_running = False
        app.regime_settings = _rp.normalize({
            'enabled': True, 'pattern_enabled': True, 'sr_enabled': True,
            'notify_enabled': True, 'notify_hhmm': '14:00',
        })

        # ---- 1. 收盤後應該送出一則,而且兩個週期都標明 ----
        app._regime_notify_last_date = None
        sent.clear(); logs.clear(); fetched.clear()
        app._regime_daily_notify_pass(now_dt=stock_app_pro.datetime(2026, 3, 10, 14, 0))
        assert len(sent) == 1, f"收盤後應該送出剛好一則推播,實際 {len(sent)} 則"
        _txt = sent[0]
        assert '[日K]' in _txt, f"訊息要標明日K: {_txt[:120]}"
        assert '[60分K]' in _txt, f"訊息要標明 60分K (使用者明確要求): {_txt[:120]}"
        assert ('盤勢/型態' in _txt) or ('支撐壓力' in _txt), \
            f"要講出是什麼型態或支撐壓力: {_txt[:120]}"
        # 兩個週期都真的去抓資料了,而且**沒有丟掉最後一根**
        assert ('日K', False) in fetched and ('60分K', False) in fetched, \
            f"兩個週期都要抓、且 drop_last=False (收盤後今天那根已定案): {fetched}"

        # ---- 2. 同一天不可以再送第二則 ----
        sent.clear()
        app._regime_daily_notify_pass(now_dt=stock_app_pro.datetime(2026, 3, 10, 15, 30))
        assert not sent, f"一天只能一則,實際又送了 {len(sent)} 則"

        # ---- 3. 隔天要恢復 (反向對照:不可以只送一次就再也不送) ----
        app._regime_daily_notify_pass(now_dt=stock_app_pro.datetime(2026, 3, 11, 14, 0))
        assert len(sent) == 1, "隔天必須再送一則"

        # ---- 4. 收盤前不可以送 ----
        app._regime_notify_last_date = None
        sent.clear(); fetched.clear()
        app._regime_daily_notify_pass(now_dt=stock_app_pro.datetime(2026, 3, 12, 11, 0))
        assert not sent, "收盤前 (11:00) 不可以推播"
        assert not fetched, "時間沒到連資料都不該抓 (省 API)"

        # ---- 5. 關掉推播開關就完全不送 ----
        app.regime_settings = _rp.normalize({
            'enabled': True, 'pattern_enabled': True, 'sr_enabled': True,
            'notify_enabled': False, 'notify_hhmm': '14:00'})
        app._regime_notify_last_date = None
        sent.clear()
        app._regime_daily_notify_pass(now_dt=stock_app_pro.datetime(2026, 3, 12, 14, 0))
        assert not sent, "推播開關關著就不可以送"

        # ---- 6. 只勾 60分K → 訊息只能有 60分K ----
        app.regime_settings = _rp.normalize({
            'enabled': True, 'pattern_enabled': True, 'sr_enabled': True,
            'notify_enabled': True, 'notify_hhmm': '14:00',
            'notify_timeframes': ['60分K']})
        app._regime_notify_last_date = None
        sent.clear(); fetched.clear()
        app._regime_daily_notify_pass(now_dt=stock_app_pro.datetime(2026, 3, 12, 14, 0))
        assert len(sent) == 1, "只勾 60分K 仍然要送"
        assert '[60分K]' in sent[0] and '[日K]' not in sent[0], \
            f"只勾 60分K 時訊息不該出現日K: {sent[0][:120]}"
        assert all(tf == '60分K' for tf, _d in fetched), f"不該去抓沒勾的週期: {fetched}"

        # ---- 7. 沒登入就不推播 (抓不到指數資料),而且不可以吃掉當天的機會 ----
        app.regime_settings = _rp.normalize({
            'enabled': True, 'pattern_enabled': True, 'sr_enabled': True,
            'notify_enabled': True, 'notify_hhmm': '14:00'})
        app.api_logged_in = False
        app._regime_notify_last_date = None
        sent.clear()
        app._regime_daily_notify_pass(now_dt=stock_app_pro.datetime(2026, 3, 13, 14, 0))
        assert not sent, "沒登入時不該送 (資料抓不到)"
        assert app._regime_notify_last_date is None, \
            "沒登入不可以記成『今天已處理』,否則登入後整天都不會補送"
        # 登入後同一天要補送 (正控)
        app.api_logged_in = True
        app._regime_daily_notify_pass(now_dt=stock_app_pro.datetime(2026, 3, 13, 15, 0))
        assert len(sent) == 1, "登入之後同一天要補送"
    finally:
        app.log_message = orig_log
        app._send_telegram_async = orig_send
        app._qt_resolve_watch = orig_resolve_watch
        app._qt_fetch_closed_bars = orig_fetch
        app.telegram_cfg = orig_cfg
        app.api_logged_in, app.sj_api = orig_login, orig_api
        app._qt_running = orig_running
        app.regime_settings = orig_settings
        app._regime_notify_last_date = orig_last


run_case("ADR-132: 盤勢判斷每日收盤後推播到手機 (含60分K,標明週期)",
         _regime_daily_notify_132)


def _fib_and_auto_regime_133():
    """【ADR-133】(1) 主圖黃金切割律 (2) 盤勢判斷不必人工切換週期就自動判斷。

    使用者要求:
      「1.主圖指標,再增加黃金切割律(費波南係數)這個功能。
        2.主圖切到 60分K 時系統日誌才出現盤勢判斷 —— 我要不需要我人工切換,
          你就可以自動判斷。(日K也是希望可以做到這一點)」

    走**完整的 GUI 路徑**:純函式測不到「畫圖有沒有真的呼叫」與「背景掃描
    有沒有真的不看主圖現在顯示什麼」(P-64)。
    """
    import pandas as _pd
    from core import fibonacci as _fib
    from core import regime_panel as _rp

    class FakeContract:
        code = 'TSE'
        symbol = '^TWII'

    # ---------- A. 黃金切割:畫圖路徑 ----------
    _n = 160
    _closes = [20000.0 + i * 30 for i in range(_n)]          # 明確的上升段
    DF = _pd.DataFrame(
        {'Open': _closes, 'High': [c + 40 for c in _closes],
         'Low': [c - 40 for c in _closes], 'Close': _closes,
         'Volume': [10000] * _n},
        index=_pd.date_range('2026-01-01', periods=_n, freq='D'))

    drawn = []

    class FakeAx:
        def axhline(self, y=None, **k):
            drawn.append(float(y))
        def text(self, *a, **k):
            pass
        def get_yaxis_transform(self):
            return None

    orig_show = app.fib_show.get()
    orig_ratios = list(app.fib_ratios)
    orig_lb = app.fib_lookback.get()
    orig_log = app.log_message
    logs = []
    try:
        app.log_message = lambda m: (logs.append(str(m)), orig_log(m))[0]

        # 1. 沒勾就什麼都不畫 (反向對照 —— 否則「永遠畫」也會讓下一條綠)
        app.fib_show.set(False)
        drawn.clear()
        app._draw_fib_levels(FakeAx(), DF)
        assert not drawn, f"沒勾黃金切割不該畫任何線,實際畫了 {len(drawn)} 條"
        assert app._fib_last_result is None

        # 2. 勾了要畫出每一條選取的比率
        app.fib_show.set(True)
        app.fib_ratios = list(_fib.DEFAULT_LEVELS)
        app.fib_lookback.set(120)
        drawn.clear()
        app._draw_fib_levels(FakeAx(), DF)
        assert len(drawn) == len(_fib.DEFAULT_LEVELS), \
            f"應該畫 {len(_fib.DEFAULT_LEVELS)} 條,實際 {len(drawn)}"
        r = app._fib_last_result
        assert r and r['swing']['trend'] == _fib.TREND_UP, \
            f"這段資料是上升段,實際判成 {r and r['swing']['trend']}"
        # 價位必須落在區間內,而且 0.618 的位置要對
        hi, lo = r['swing']['high'], r['swing']['low']
        for y in drawn:
            assert lo - 1e-6 <= y <= hi + 1e-6, f"回撤價位 {y} 落在高低點之外"
        _g = [lv for lv in r['levels'] if abs(lv['ratio'] - 0.618) < 1e-9][0]
        assert abs(_g['price'] - (hi - (hi - lo) * 0.618)) < 1e-6, "0.618 的價位算錯"

        # 3. 只勾兩條就只畫兩條 (設定真的有生效)
        app.fib_ratios = [0.382, 0.618]
        drawn.clear()
        app._draw_fib_levels(FakeAx(), DF)
        assert len(drawn) == 2, f"只勾兩條就該只畫兩條,實際 {len(drawn)}"

        # 4. 摘要要講得出關鍵價位
        logs.clear()
        app._fib_log_summary()
        assert [m for m in logs if m.startswith('【黃金切割】')], "應寫入系統日誌"
        assert any('0.618' in m for m in logs), f"摘要要含 0.618: {logs[:2]}"

        # 5. 壞資料不可以害整張圖畫不出來
        #
        # 【注意語意】黃金切割沿用支撐壓力的 _sr_source_df():「可見範圍」模式下
        # 它回傳的是 self.plot_df (使用者縮放後看得到的那一段),**不是**傳進來的
        # raw_df —— 這是刻意的,切割線要跟著畫面上的區間走。所以要驗「資料不足」
        # 必須把 plot_df 也一起清掉,否則測到的是上一個案例殘留的資料
        # (第一版就是這樣紅的)。
        app.fib_ratios = list(_fib.DEFAULT_LEVELS)
        _orig_plot = getattr(app, 'plot_df', None)
        try:
            drawn.clear()
            app._draw_fib_levels(FakeAx(), None)     # 不可以拋例外
            assert not drawn, "raw_df=None 不該畫線"
            app.plot_df = None
            app._draw_fib_levels(FakeAx(), DF.iloc[:3])
            assert not drawn, "資料不足時不該畫線,但也不該爆炸"
        finally:
            app.plot_df = _orig_plot
    finally:
        app.fib_show.set(orig_show)
        app.fib_ratios = orig_ratios
        app.fib_lookback.set(orig_lb)
        app.log_message = orig_log

    # ---------- B. 盤勢判斷自動掃描 (不看主圖現在顯示什麼) ----------
    scanned = []
    logs2 = []
    orig_log2 = app.log_message
    orig_fetch = app._qt_fetch_closed_bars
    orig_resolve_watch = app._qt_resolve_watch
    orig_login, orig_api = app.api_logged_in, app.sj_api
    orig_settings = app.regime_settings
    orig_state = app._regime_notify_state
    orig_tf = app.timeframe_var.get()
    orig_sym = app.current_symbol
    orig_slots = getattr(app, '_regime_scan_slots', None)
    _msess = stock_app_pro.market_session
    orig_stock_open = _msess.is_stock_open

    def _fake_fetch(_s, _c, _a, tf=None, **_k):
        scanned.append(tf)
        return DF

    try:
        app.log_message = lambda m: (logs2.append(str(m)), orig_log2(m))[0]
        app._qt_resolve_watch = lambda _s: (FakeContract(), 'index_tw', '^TWII', '台股')
        app._qt_fetch_closed_bars = _fake_fetch
        app.api_logged_in = True; app.sj_api = object()
        app.regime_settings = _rp.normalize({'enabled': True, 'pattern_enabled': True})
        app._regime_notify_state = {}
        app._regime_scan_slots = {}
        _msess.is_stock_open = lambda *a, **k: True
        # 【關鍵】主圖刻意停在一個**完全無關**的商品與週期上 ——
        # 自動掃描不該受它影響,這正是使用者要的「不用人工切換」。
        app.timeframe_var.set('5分K')
        app.current_symbol = '2330'

        _t1 = stock_app_pro.datetime(2026, 3, 10, 10, 5)
        scanned.clear(); logs2.clear()
        app._regime_auto_scan_pass(now_dt=_t1)
        app.flush_after()
        assert set(scanned) == set(_rp.PATTERN_TIMEFRAMES), \
            f"日K 與 60分K 都要自動掃,實際掃了 {scanned}"
        _hits = [m for m in logs2 if m.startswith('【盤勢判斷】')]
        assert _hits, "自動掃描到型態要寫進系統日誌"
        assert any('日K' in m for m in _hits) or any('60分K' in m for m in _hits), \
            f"日誌要標明週期: {_hits[:2]}"

        # 同一根K棒內不可以重掃 (省 API,同 ADR-127 的教訓)
        scanned.clear()
        app._regime_auto_scan_pass(now_dt=_t1)
        assert not scanned, f"同一格不該重抓,實際又抓了 {scanned}"

        # 跨到下一根 60分K 就要重掃 (反向對照:不可以掃一次就再也不掃)
        scanned.clear()
        app._regime_auto_scan_pass(now_dt=stock_app_pro.datetime(2026, 3, 10, 11, 5))
        assert '60分K' in scanned, f"跨到新的一根 60分K 要重掃,實際 {scanned}"

        # 收盤後不掃 (資料不再變動;收盤那一輪由每日推播收尾)
        _msess.is_stock_open = lambda *a, **k: False
        app._regime_scan_slots = {}
        scanned.clear()
        app._regime_auto_scan_pass(now_dt=stock_app_pro.datetime(2026, 3, 10, 15, 5))
        assert not scanned, f"收盤後不該持續掃,實際 {scanned}"

        # 總開關關掉就完全不掃
        _msess.is_stock_open = lambda *a, **k: True
        app.regime_settings = _rp.normalize({'enabled': False, 'pattern_enabled': True})
        app._regime_scan_slots = {}
        scanned.clear()
        app._regime_auto_scan_pass(now_dt=_t1)
        assert not scanned, "盤勢判斷總開關關著就不該掃"
    finally:
        app.log_message = orig_log2
        app._qt_fetch_closed_bars = orig_fetch
        app._qt_resolve_watch = orig_resolve_watch
        app.api_logged_in, app.sj_api = orig_login, orig_api
        app.regime_settings = orig_settings
        app._regime_notify_state = orig_state
        app.timeframe_var.set(orig_tf)
        app.current_symbol = orig_sym
        app._regime_scan_slots = orig_slots or {}
        _msess.is_stock_open = orig_stock_open


run_case("ADR-133: 主圖黃金切割律 + 盤勢判斷自動掃描 (不必人工切換週期)",
         _fib_and_auto_regime_133)


def _sub_indicators_and_jae_134():
    """【ADR-134】(1) 副圖指標整合成一個入口 (2) JAE 新指標 (3) KDJ 超買超賣線。

    使用者要求:
      「1.把主圖上方列的各種技術指數全部整合到一個【副圖技術指標】中。
        **切記,只有移動整合,沒有改其他的功能。**
        2.再新增一個 KDJ 指標 & JAE 指標。」

    「只有移動整合」是這個案例最重要的守門:**整合前後,同一組設定畫出來的
    副圖必須一模一樣**。所以第一段是拿整合前的行為當基準去比對。
    """
    import pandas as _pd
    from core import jae as _jae
    from core import indicators as _ind

    _n = 200
    _c = [100.0 + i * 0.5 + (3.0 if i % 4 == 0 else -2.0) for i in range(_n)]
    DF = _pd.DataFrame(
        {'Open': _c, 'High': [x + 2 for x in _c], 'Low': [x - 2 for x in _c],
         'Close': _c, 'Volume': [1000 + (i % 7) * 50 for i in range(_n)]},
        index=_pd.date_range('2026-01-01', periods=_n, freq='D'))

    _saved = {}
    for _v in ('var_macd', 'var_rsi', 'var_kdj', 'var_dmi', 'var_bbw', 'var_jae'):
        _saved[_v] = getattr(app, _v).get()
    _saved_jae = {k: v.get() for k, v in app.jae_params.items()}
    orig_log = app.log_message
    logs = []

    try:
        app.log_message = lambda m: (logs.append(str(m)), orig_log(m))[0]

        # ---- 1. 【只有移動整合】既有指標的計算結果不可以變 ----
        # 用同一組設定,拿 core 的算式當基準逐欄比對。
        for _v in ('var_macd', 'var_rsi', 'var_kdj', 'var_dmi', 'var_bbw'):
            getattr(app, _v).set(True)
        app.var_jae.set(False)
        out = app.calculate_custom_indicators(DF)
        for _col in ('MACD', 'Signal', 'Hist', 'RSI', 'K', 'D', 'J',
                     '+DI', '-DI', 'ADX', 'BB_WIDTH'):
            assert _col in out.columns, f"整合後 {_col} 不見了 (只該搬位置,不該改功能)"
        # 【基準必須獨立】不可以拿 _ind.rsi() 當基準去比 out['RSI'] ——
        # 兩邊都是同一個函式,把那個函式改壞這條斷言照樣綠 (突變測試抓到的)。
        # 這裡把算式**原地重寫一遍**,才是真的「整合前後結果一樣」的證據。
        _p_rsi = int(app.rsi_p.get())
        _delta = DF['Close'].diff()
        _gain = _delta.clip(lower=0).ewm(com=_p_rsi - 1, adjust=False).mean()
        _loss = (-1 * _delta.clip(upper=0)).ewm(com=_p_rsi - 1, adjust=False).mean()
        _pd.testing.assert_series_equal(
            out['RSI'], 100 - (100 / (1 + (_gain / _loss))), check_names=False)
        _kn, _km1, _km2 = int(app.kd_n.get()), int(app.kd_m1.get()), int(app.kd_m2.get())
        _lo = DF['Low'].rolling(window=_kn).min()
        _hi = DF['High'].rolling(window=_kn).max()
        _rsv = 100 * (DF['Close'] - _lo) / (_hi - _lo)
        _k = _rsv.ewm(com=_km1 - 1, adjust=False).mean()
        _d = _k.ewm(com=_km2 - 1, adjust=False).mean()
        _pd.testing.assert_series_equal(out['K'], _k, check_names=False)
        _pd.testing.assert_series_equal(out['D'], _d, check_names=False)
        _pd.testing.assert_series_equal(out['J'], 3 * _k - 2 * _d, check_names=False)
        # JAE 沒開就不可以在 df 裡多塞欄位
        assert _jae.COL_A not in out.columns, "沒開 JAE 就不該算它 (省時間也不留垃圾欄位)"

        # ---- 2. 整合入口存在,而且涵蓋每一個既有指標 ----
        _names = [n for n, _v, _f in app.SUB_INDICATORS]
        for _need in ('MACD', 'RSI', 'KDJ', 'DMI'):
            assert _need in _names, f"整合清單漏了 {_need}"
        assert any('布林' in n for n in _names), "整合清單漏了布林通道寬"
        # 清單裡的變數/欄位名都要真的存在 (打錯字會在開視窗時才爆炸)
        for _n2, _var, _fields in app.SUB_INDICATORS:
            assert hasattr(app, _var), f"{_n2} 的開關變數 {_var} 不存在"
            for _lbl, _fv in _fields:
                assert hasattr(app, _fv), f"{_n2} 的參數變數 {_fv} 不存在"
        assert hasattr(app, 'open_sub_indicators'), "缺少整合視窗的入口"

        # ---- 3. 工具列不可以再有各自的勾選鈕 (那就是「整合」的定義) ----
        _src = open('stock_app_pro.py', encoding='utf-8').read()
        assert 'open_sub_indicators' in _src
        assert '("MACD", self.var_macd, lambda: self.open_sub_settings("MACD"))' not in _src, \
            "工具列的舊指標清單應已移除 (整合到【副圖技術指標】)"

        # ---- 4. KDJ 超買/超賣線:設定存在且可調 ----
        assert hasattr(app, 'kd_ob') and hasattr(app, 'kd_os'), "KDJ 應有超買/超賣設定"
        assert ('KDJ', 'var_kdj') == tuple(
            [x[:2] for x in app.SUB_INDICATORS if x[0] == 'KDJ'][0][:2])
        _kdj_fields = [f for n, v, fs in app.SUB_INDICATORS if n == 'KDJ' for f in fs]
        assert any(fv == 'kd_ob' for _l, fv in _kdj_fields), "超買線要能在整合視窗調"
        assert any(fv == 'kd_os' for _l, fv in _kdj_fields), "超賣線要能在整合視窗調"

        # ---- 5. JAE:打開才算,三條線都要有 ----
        app.var_jae.set(True)
        out2 = app.calculate_custom_indicators(DF)
        for _col in (_jae.COL_A, _jae.COL_J, _jae.COL_E):
            assert _col in out2.columns, f"JAE 缺 {_col}"
        # A 必須就是 RSI、J 必須就是 KDJ 的 J (使用者的定義,不可以自己另算一份)
        _p = app._jae_params_now()
        _pd.testing.assert_series_equal(
            out2[_jae.COL_A], _ind.rsi(DF['Close'], _p['a_period']), check_names=False)
        _, _, _, _j2 = _ind.kdj(DF, _p['j_n'], _p['j_m1'], _p['j_m2'])
        _pd.testing.assert_series_equal(out2[_jae.COL_J], _j2, check_names=False)

        # ---- 6. JAE 參數真的有作用 (否則「可自行設定」是假的) ----
        app.jae_params['a_period'].set('5')
        out3 = app.calculate_custom_indicators(DF)
        # 先把欄位名取成區域變數再用 —— 直接寫 out3[_jae.COL_A].equals(...) 會被
        # diag_crossref 的啟發式誤判成「呼叫 _jae.COL_A()」而誤報跨模組斷鏈。
        _ca = _jae.COL_A
        assert not out3[_ca].equals(out2[_ca]), "改了 A 的期數,A 這條線必須跟著變"
        app.jae_params['a_period'].set(str(_jae.DEFAULT_PARAMS['a_period']))

        # ---- 7. 壞參數不可以害整張 K 線圖畫不出來 ----
        app.jae_params['a_period'].set('abc')
        out4 = app.calculate_custom_indicators(DF)
        assert 'RSI' in out4.columns and 'MACD' in out4.columns, \
            "JAE 參數打錯字,其他指標仍然要算得出來"
        app.jae_params['a_period'].set(str(_jae.DEFAULT_PARAMS['a_period']))

        # ---- 8. JAE 摘要寫進系統日誌 ----
        app.full_calculated_df = app.calculate_custom_indicators(DF)
        logs.clear()
        app._jae_log_summary()
        _hit = [m for m in logs if m.startswith('【JAE】')]
        assert _hit, "JAE 應該寫一行摘要到系統日誌"
        assert '趨勢' in _hit[0], f"摘要要講出趨勢: {_hit[0]}"

        # ---- 9. 工具列的「目前開了哪些」標籤要跟著更新 ----
        app._refresh_sub_active_label()
        _txt = app.lbl_sub_active.cget('text')
        assert 'JAE' in _txt and 'MACD' in _txt, f"標籤要列出已開啟的副圖: {_txt}"
        app.var_jae.set(False); app.var_macd.set(False)
        for _v in ('var_rsi', 'var_kdj', 'var_dmi', 'var_bbw'):
            getattr(app, _v).set(False)
        app._refresh_sub_active_label()
        assert '未選' in app.lbl_sub_active.cget('text'), \
            f"全部關掉要顯示未選: {app.lbl_sub_active.cget('text')}"
    finally:
        app.log_message = orig_log
        for _v, _val in _saved.items():
            getattr(app, _v).set(_val)
        for _k, _val in _saved_jae.items():
            app.jae_params[_k].set(_val)
        app.__dict__.pop('_jae_warned', None)
        try:
            app._refresh_sub_active_label()
        except Exception:
            pass


run_case("ADR-134: 副圖指標整合入口 + JAE 新指標 + KDJ 超買超賣線",
         _sub_indicators_and_jae_134)


print(f"{'案例':60s} 結果")
print("-" * 76)
for name, st, msg in results:
    print(f"{name:58s} {st}  {msg}")
