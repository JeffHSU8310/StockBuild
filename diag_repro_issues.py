"""
diag_repro_issues.py — 重現使用者第五輪回報的三個問題 (修正前基準)。

1. 版面微調滑桿完全無效: 驗證 fig.subplots_adjust 對 mplfinance (add_axes) 面板無效。
2. 委託單清單空白: 用假 Trade (id 空字串, PendingSubmit) 走 _confirm_and_place_order,
   檢查 my_orders 與 tree_orders 的實際內容。
3. 版面數值變更後重繪,面板位置有沒有真的改變。
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diag_mock_tkinter
diag_mock_tkinter.install_mock_tkinter()

import numpy as np
import pandas as pd
import stock_app_pro
from stock_app_pro import StockTradingAppPro

app = StockTradingAppPro()
app.flush_after = getattr(app, "flush_after")  # 來自 _Tk mock


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
    try:
        fn()
        results.append((name, "PASS", ""))
    except AssertionError as e:
        results.append((name, "FAIL", str(e)))
    except Exception as e:
        results.append((name, "ERROR", f"{type(e).__name__}: {e}"))


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
    app._quant_eval_pass(); app.flush_after()
    assert api.placed==[] and app.strategy_runtimes[s['id']]['state']=='FLAT', "總開關關閉時不可有任何動作"
    # 啟動+模擬:有訊號、無真實單
    app._qt_running=True
    logs=[]; ol=app.log_message; app.log_message=lambda m:(logs.append(m), ol(m))[0]
    try:
        app._quant_eval_pass(); app.flush_after()
        assert any('自動交易-模擬' in m for m in logs) and api.placed==[], "模擬模式不可下真實單"
        assert app.strategy_runtimes[s['id']]['state']=='LONG', "模擬應建立虛擬持倉"
        # 同一根K棒不重複
        n=sum(1 for m in logs if '自動交易-模擬' in m)
        app._quant_eval_pass(); app.flush_after()
        assert sum(1 for m in logs if '自動交易-模擬' in m)==n, "同一根K棒不可重複觸發"
        # 實單參數鏡射
        s2=_se.new_strategy()
        s2.update({'name':'診斷實單','symbol':'TXF','market':'台期貨','timeframe':'1分K','qty':1,
                   'cooldown_sec':0,'mode':'實單','enabled':True,'stop_loss_pct':1.0,
                   'session_gate':False,   # 【ADR-099】診斷需與時鐘無關,理由同上
                   'entry':[{'type':'ma_cross_up','params':{'fast':3,'slow':10}}]})
        app.strategies.append(s2); app.strategy_runtimes[s2['id']]=_se.new_runtime()
        app._quant_eval_pass(); app.flush_after()
        assert len(api.placed)==1 and api.placed[0][0]=='TXFR1', "實單應送出期貨委託"
        kw=api.placed[0][1]
        assert str(kw.get('price_type')).endswith('LMT') and kw.get('quantity')==1, "下單參數鏡射錯誤"
        # 急停
        app._qt_stop_all()
        n_placed=len(api.placed)
        app._quant_eval_pass(); app.flush_after()
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
        app._quant_eval_pass(); app.flush_after()
        n1=len(calls)
        app._quant_eval_pass(); app.flush_after()
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

        # 關閉時不應有任何支撐壓力線
        app.sr_enabled_var.set(False)
        app.draw_chart(df)
        assert app._sr_last_result is None, "關閉時不該計算支撐壓力"

        # 開啟後應畫出水平線
        app.sr_enabled_var.set(True)
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

        # 兩種區間模式都要能運作
        app._sr_set_range_mode(app.SR_RANGE_FIXED)
        assert app._sr_last_result and app._sr_last_result['levels'], "固定N根模式應可運作"
        app._sr_set_range_mode(app.SR_RANGE_VISIBLE)
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

print(f"{'案例':60s} 結果")
print("-" * 76)
for name, st, msg in results:
    print(f"{name:58s} {st}  {msg}")
