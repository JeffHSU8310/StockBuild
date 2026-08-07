# -*- coding: utf-8 -*-
"""
core/backtest.py — 策略回測引擎 (純邏輯層)

【ADR-039】核心設計原則:回測與實盤共用同一套策略評估邏輯
    (strategy_engine.evaluate_strategy / apply_fill)。逐根K棒「重放」歷史,
    引擎回什麼意圖就照樣成交——這樣才能保證「回測會賺、實盤會賠」不是因為
    兩套邏輯不同。絕不另寫一套平行的判斷。

【ADR-064】實際成交時機是「T+1 開盤」,不是 ADR-039 原文描述的「同一根收盤
    即成交」:第 i 根的訊號用「i 之前」已收盤的資料判定 (eval_window,防止用
    到當根還沒走完的高低價/收盤價,見 P-49),但真正下單成交發生在第 i 根的
    「開盤」。這個改動本身沒有問題 (更貼近真實下單流程),但先前沒有任何 ADR
    記錄,導致 ADR-039 的舊測試假設與現況不符;ADR-064 補上這筆記錄。

零 tkinter / 零 shioaji / 零 matplotlib 依賴,可用 tests/test_core.py 完整驗證。
繪圖 (資金曲線、K線標點) 由 GUI 層負責,這裡只回傳純數據。
"""
import copy
import pandas as pd

from . import strategy_engine
# 【ADR-151】回測要套用與實盤同一道交易時段閘門 (零股 09:10 才開盤)
from . import market_session as _msess


def run_backtest(strategy, df, fee_rate=0.0, slippage_ticks=0, tick_size=None,
                 cost_params=None, apply_cost_model=True, should_stop=None,
                 settle_open_at_end=True, exec_df=None, apply_risk_guards=True):
    """
    對單一策略在歷史 df 上回測。

    【ADR-075 看A做B 回測】exec_df:實際「要下單的商品 B」的歷史 K 棒。傳入時:
      * 訊號、指標、停損/停利 一律看 df (訊號來源 A);
      * 成交價 (進出場、損益、資金曲線浮動) 一律看 exec_df (B),依時間戳對齊
        (A 第 i 根的時間 → B 中「>= 該時間」的第一根開盤價成交,同 T+1 模型)。
      * A 與 B 週期可不同 (如 A 30分K、B 5分K),用 searchsorted 對齊。
    exec_df=None (預設) → B=A,行為與舊版完全一致 (對齊會取到同一根)。

    參數:
      strategy: 策略 dict (與實盤同結構;direction/entry/exit_signals/stop_loss_pct...)
      df: 含 DatetimeIndex 與 Open/High/Low/Close/Volume 的歷史K棒 (由最舊到最新)
      fee_rate: 【已棄用,保留相容】單邊手續費率;apply_cost_model=True 時忽略
      cost_params: 成本參數 dict (覆寫 core/cost_model 預設費率,如 fee_discount)
      apply_cost_model: True=用 core/cost_model 算真實手續費+交易稅 (ADR-050);
                        False=退回舊的 fee_rate 行為 (僅供回歸比對)
      slippage_ticks: 成交滑價檔數 (買方成本+、賣方收入-),需搭配 tick_size
      tick_size: 一檔的價格;None 則 slippage 以「絕對價差 = slippage_ticks」計

    回傳 dict:
      trades: [{entry_ts, entry_price, exit_ts, exit_price, direction, qty,
                pnl, pnl_pct, bars_held, exit_reason}]
      equity: [(ts, cumulative_pnl)]  每根K棒的累積已實現損益 (含未平倉浮動)
      metrics: {total_pnl, total_return_pct, win_rate, max_drawdown, trades,
                wins, losses, profit_factor, avg_bars_held, avg_win, avg_loss}
      markers: [{ts, price, kind}]  kind: buy_open/sell_open/buy_close/sell_close
                (給 K 線圖標點用)
    """
    if df is None or len(df) < 3:
        return _empty_result()

    # 【ADR-075】看A做B:df=A(訊號), exec_df=B(成交)。未帶就 B=A。
    _use_watch = exec_df is not None and len(exec_df) > 0
    _bdf = exec_df if _use_watch else df

    def _exec_at(ts):
        """回傳對齊到 A 時間 ts 的 B (open, close)。B 中第一根 index>=ts 的那根;
        超出範圍就取最後一根收盤 (退而求其次,避免無價可成交)。"""
        try:
            pos = _bdf.index.searchsorted(ts, side='left')
            if pos >= len(_bdf):
                last = float(_bdf['Close'].iloc[-1])
                return last, last
            return float(_bdf['Open'].iloc[pos]), float(_bdf['Close'].iloc[pos])
        except Exception:
            a = float(df['Open'].iloc[-1]) if len(df) else 0.0
            return a, a

    s = copy.deepcopy(strategy)
    qty = int(s.get('qty', 1) or 1)
    # 【ADR-043】每單位口/張換算成「幾股 or 幾點乘數」——回測金額才跟真實一致:
    #   股票 1 張 = 1000 股;零股 = 1 股;期貨 = 契約乘數 (TXF=200/MXF=50/TMF=10)。
    from . import strategy_engine as _se
    from . import cost_model as _cost_model
    tt = _se.trade_type_of(s)
    # 【ADR-062】改用 strategy_engine.unit_size():定期定額要把金額換算成張數,
    # 也需要同一個數字。兩處各寫一份遲早分歧 (P-67),統一到共用函式。
    contract_size = _se.unit_size(s)
    rt = strategy_engine.new_runtime()
    _tt_bt = _se.trade_type_of(s)          # 【ADR-151】時段閘門要看交易種類
    # 【ADR-151】時段閘門**只對盤中資料有意義**。日K/周K/月K 的一根代表一整天
    # (含),沒有「盤中第幾分鐘」這回事;拿去比「盤中 09:00~13:30」會把每一根
    # 都判成休市,整份回測一筆交易都不會有(第一版就是這樣,單元測試當場紅了
    # 33 個)。
    #
    # 判斷依據刻意用**實際的 K 棒間距**,不是策略宣告的 timeframe:兩者可能不
    # 一致(測試餵日線給宣告 5分K 的策略就是一例),而會不會有盤中時刻這件事
    # 是**資料的性質**,不是設定的性質。用中位數而不是頭兩根,避免資料中間有
    # 缺口時判錯。
    _intraday_bt = False
    try:
        if len(df) >= 3:
            _steps = df.index.to_series().diff().dropna().dt.total_seconds()
            _med = float(_steps.median())
            _intraday_bt = 0 < _med < 86400
    except Exception:
        _intraday_bt = False
    # 【ADR-150】回測是否套用「每日進場上限 / 冷卻 / 熔斷」。
    #
    # 舊版一律**不套用**,理由寫的是「回測要看策略訊號本身的績效」,並註明
    # 「若要看含風控的結果,未來可加參數」。實際後果是:使用者把「每日進場
    # 上限」設成 1,回測卻一天買很多次 —— **回測數字與實盤行為對不上**,
    # 而回測的用途正是預估實盤。使用者原話:「這不符合我的需求。不論是幾分K,
    # 只要是在一天之中,有成交就算數,就不能再重複買進。」
    #
    # 所以預設改成**照策略設定跑**;要看純訊號績效才傳 apply_risk_guards=False。
    if not apply_risk_guards:
        s = _relax_realtime_guards(s)

    # 【ADR-040】自訂 Python 策略:逐根呼叫 on_bar,決策轉 intent 後與內建同路。
    is_custom = s.get('kind') == 'custom' and s.get('source_code')
    custom_fn = None
    custom_state = {}
    if is_custom:
        from . import custom_strategy as _cs
        custom_fn = _cs

    trades = []
    equity = []
    markers = []
    # 【ADR-061】買進持有 (累積) 模式:每次進場條件成立就記一筆 lot,永不賣出
    is_bnh = bool(s.get('buy_and_hold'))
    bnh_lots = []
    # 【ADR-055】自訂策略「實際讀到的參數」追蹤:回測跑完把最後一次 on_bar
    # 的 ctx.param_reads 帶回報告,使用者才能確認參數視窗有沒有正確代入。
    param_usage = {}
    total_fee_sum = [0.0]
    total_tax_sum = [0.0]
    total_gross_sum = [0.0]
    # 【ADR-144】一段部位可以由**多筆**進場組成 (分批建倉/加碼)。
    # 舊版只有一個 open_trade 槽位,所以「買了就不准再買」——那正是使用者說的
    # 「回測一年只買一次」的元兇。改成 lot 清單:每一次進場一筆,平倉時逐筆
    # 結算成獨立的交易明細列 (使用者才看得到自己買在哪幾個價位)。
    # max_entries 預設 1 → 清單最多一筆 → 與舊行為逐筆相同。
    open_lots = []
    # 回測的「訊息紀錄」(ADR-144):逐根記下發生了什麼、以及**什麼沒發生**。
    log_rows = []
    realized = 0.0

    def _log(ts, kind, text, collapse=False):
        """寫一列訊息紀錄。

        collapse=True 時,**連續重複的同一句話會合併成一列並累計次數** ——
        「條件成立但已持倉」在死腦筋的舊行為下每一根都會發生一次,逐根印出來
        會有幾萬列,使用者反而看不到重點。合併之後看到的是
        「(連續 237 根) 已持倉,且可分批進場次數是 1…」,一眼就懂。
        """
        text = str(text)
        if collapse and log_rows:
            last = log_rows[-1]
            if last.get('kind') == kind and last.get('_raw') == text:
                last['n'] = int(last.get('n', 1)) + 1
                last['ts_end'] = ts
                last['text'] = f"(連續 {last['n']} 根) {text}"
                return
        if len(log_rows) < LOG_MAX_ROWS:
            log_rows.append({'ts': ts, 'kind': kind, 'text': text,
                             '_raw': text, 'n': 1})
        elif len(log_rows) == LOG_MAX_ROWS:
            log_rows.append({'ts': ts, 'kind': '截斷', 'n': 1, '_raw': '',
                             'text': f'訊息過多,只保留前 {LOG_MAX_ROWS} 筆。'})

    def _fill_price(intent_price, action):
        px = float(intent_price)
        if slippage_ticks and tick_size:
            px += slippage_ticks * float(tick_size) * (1 if action == '買進' else -1)
        elif slippage_ticks:
            px += slippage_ticks * (1 if action == '買進' else -1)
        return px

    # 【ADR-064】逐根重放:訊號用「i 之前」已收盤的資料判定 (eval_window),
    # 實際成交發生在第 i 根的開盤 (T+1),不是同一根的收盤。
    n = len(df)
    for i in range(2, n):
        # 【ADR-057】使用者需求 #9:可強制終止。每根K棒檢查一次取消訊號,
        # 看到就中止並回傳「已完成的部分」(不是丟例外) —— 使用者按下強制
        # 終止是刻意操作,不該再收到一個錯誤彈窗。每 64 根才檢查一次以免
        # 回呼本身成為熱路徑負擔。
        if should_stop is not None and (i & 63) == 0:
            try:
                if should_stop():
                    break
            except Exception:
                pass
        # 【ADR-064】當天判定，隔天開盤執行 (T+1)
        # i 代表「今天」(執行日)，eval_window 則是「昨天以前」的歷史 (條件判定基準)
        eval_window = df.iloc[:i]
        ts_eval = eval_window.index[-1]
        today_eval = str(getattr(ts_eval, 'date', lambda: ts_eval)())
        now_ts_eval = float(i - 1)
        
        # 今天的時間與開盤價，作為實際成交的依據
        ts = df.index[i]
        today = str(getattr(ts, 'date', lambda: ts)())
        now_ts = float(i)
        # 【ADR-150】風控用的時鐘要是**真實秒數**,不能用 K 棒序號。
        # `now_ts` 是序號 (float(i)),拿它去比「冷卻秒數 10800」等於把 10800
        # 當成 10800 根 K 棒 —— 冷卻會變成永遠擋著。每日上限與熔斷不看時鐘,
        # 但冷卻看,所以這裡另外算一個真實時間戳給 risk_check / apply_fill 用
        # (兩者必須用**同一個時鐘**,否則 last_order_ts 與 now 對不起來)。
        try:
            now_clock = float(ts.timestamp())
            _clock_ok = True
        except Exception:
            now_clock = now_ts
            _clock_ok = False
        # 【ADR-150】自訂策略那條路不經過 evaluate_strategy,每日計數不會換日
        # 重置 —— 沒有這一段,trades_today 會一路累加,跑幾天之後全部被擋。
        if is_custom:
            strategy_engine.reset_daily_counters(rt, today_eval)
        # 【ADR-151】交易時段閘門:回測原本**完全沒有**這一道 (`market_session`
        # 在這個檔案裡一次都沒出現),所以零股會在 09:03「成交」—— 而盤中零股
        # 09:10 才開盤,那筆交易在實盤根本不可能發生。使用者實測回報的正是這個。
        # 與實盤同一道 `market_session.is_market_open()`,也同樣尊重 session_gate。
        _in_session = True
        if apply_risk_guards and s.get('session_gate', True) and _intraday_bt:
            try:
                _in_session = _msess.is_market_open(
                    _tt_bt, dt=ts, include_night=strategy_engine.include_night_of(s))
            except Exception:
                _in_session = True      # 判斷不出來就不擋 (不憑空製造拒絕)
        
        if is_custom:
            try:
                decision, _ctx = custom_fn.run_on_bar(
                    s['source_code'], eval_window, rt.get('state', 'FLAT'), s.get('custom_params', {}),
                    state=custom_state, entry_price=rt.get('entry_price', 0.0),
                    bars_in_position=(now_ts_eval - open_lots[0]['entry_i']) if open_lots else 0,
                    return_ctx=True, full_df=df)
                custom_state = _ctx.state
                param_usage.update(getattr(_ctx, 'param_reads', {}) or {})
            except custom_fn.StrategyError:
                decision = 'HOLD'
            intents = custom_fn.decision_to_intents(decision, s, rt, float(eval_window['Close'].iloc[-1]), str(df.index[i]))
            # 【ADR-136】自訂策略的停損/停利本來**完全沒有被套用**:
            # decision_to_intents 只把 on_bar 的 BUY/SELL/CLOSE 轉成 intent,
            # 而停損停利住在 evaluate_strategy() 裡 —— 自訂策略那條路根本不經過。
            # 結果是使用者在編輯器設了停損,回測卻當作沒設,回測數字與實際行為
            # 對不上 (使用者:「可以有效執行回測,這一點非常重要」)。
            # 這裡補上同一份判斷 (strategy_engine.check_bar_stop),讓「條件寫在
            # 程式碼、風控交給系統」真的成立。
            # 程式碼自己已經要出場時就不重複 —— 尊重策略作者的決定。
            if not any(it.get('kind') == 'CLOSE' for it in intents):
                _stop_it = strategy_engine.check_bar_stop(
                    s, rt, float(eval_window['Close'].iloc[-1]))
                if _stop_it is not None:
                    intents = [_stop_it] + [it for it in intents if it.get('kind') != 'OPEN']
        else:
            intents = strategy_engine.evaluate_strategy(s, rt, eval_window, now_ts_eval, today_eval)

        # 【ADR-136】即時停損的回測模型:勾了「停損停利即時觸發」的策略,實盤是
        # **盤中觸價就走**;回測若只看收盤價會低估停損次數,兩邊對不上。
        # 這裡在成交那一根 K 棒上用高低點判斷觸價,優先於當根的其他 intent。
        if (open_lots
                and strategy_engine.intrabar_stop_enabled(s)
                and not any(it.get('kind') == 'CLOSE' for it in intents)):
            _touch = strategy_engine.bar_touch_exit(
                s, rt, float(df['Open'].iloc[i]), float(df['High'].iloc[i]),
                float(df['Low'].iloc[i]))
            if _touch is not None:
                _tp, _treason = _touch
                _ti = strategy_engine._close_intent(rt, _tp, _treason)
                _ti['_touch_price'] = _tp      # 成交價用觸價,不用開盤價 (見下)
                intents = [_ti] + [it for it in intents if it.get('kind') != 'OPEN']

        # 【ADR-151】定時下單:回測原本**完全不知道有這個功能**(`timed_entry`
        # 在這個檔案裡一次都沒出現),所以照舊在條件一成立的那根 K 棒就進場。
        # 使用者設「指定時刻 10:30」,回測每一筆卻都在 09:03。
        #
        # 這裡補上與實盤同一份判斷:`should_fire_timed()` 決定「這一根落不落在
        # 窗口裡」,`check_timed_entry()` 決定「條件成不成立」。上面
        # evaluate_strategy 已經把 OPEN 濾掉了 (定時下單是唯一的進場觸發),
        # 自訂策略那條路不經過它,所以這裡再濾一次。
        if strategy_engine.timed_entry_enabled(s):
            intents = [it for it in intents if it.get('kind') != 'OPEN']
            _fire, _why_t = strategy_engine.should_fire_timed(s, rt, ts)
            if _fire:
                _timed_it = strategy_engine.check_timed_entry(s, rt, eval_window)
                if _timed_it is not None:
                    # 送出去才算今天處理完
                    strategy_engine.mark_timed_done(rt, ts)
                    intents = intents + [_timed_it]
                else:
                    # 【ADR-154】條件不成立**不收尾** —— 使用者原話:「在設定的
                    # 時間之後有符合策略條件,還是要下單。」指定時刻是「從這裡
                    # 開始看」,不是「只看這一眼」。窗口內的每一根 K 棒都要再問
                    # 一次,窗口過了才由下面的 timed_window_expired 收尾。
                    #
                    # 原本這裡無條件 mark_timed_done,等於一次定生死;而實盤那條
                    # 路(_qt_check_timed_entries)本來就是 continue 重試 ——
                    # 同一份規則兩份實作各走各的(P-67),回測會比實盤少進場。
                    _log(ts, '未進場',
                         f"{strategy_engine.timed_entry_time_of(s)} 起算的窗口內,"
                         f"此刻進場條件不成立 (或已達可分批進場次數上限),"
                         f"窗口內下一根再看", collapse=True)
            elif strategy_engine.timed_window_expired(s, rt, ts):
                strategy_engine.mark_timed_done(rt, ts)
                _log(ts, '未進場',
                     f"定時下單:{strategy_engine.timed_entry_time_of(s)} 起算的窗口已過,"
                     f"今天不再送", collapse=True)

        # 【ADR-151】休市時段一律不成交 —— 進場、出場都是。與實盤一致:
        # session_gate 打開時,非交易時間整檔策略不評估。
        if not _in_session:
            intents = []

        # 【ADR-064】強制將所有訊號的預期成交價改為「隔天(今日)開盤價」
        # 【ADR-075】intent['price'] 用 A 的開盤 (→ apply_fill 的 entry_price,
        # 停損停利以 A 判定);實際成交價另取 B 對齊到同一時間的開盤 (exec_open)。
        open_px = float(df['Open'].iloc[i])
        for intent in intents:
            # 【ADR-136】盤中觸價出場的成交價是「觸價」(或跳空時的開盤價),
            # 不可以被 ADR-064 這條「一律用開盤價」蓋掉 —— 那會讓即時停損在
            # 回測裡永遠成交在開盤價,失去模擬的意義。
            intent['price'] = intent.get('_touch_price', open_px)
        exec_open, exec_close_px = _exec_at(ts) if _use_watch else (open_px, float(df['Close'].iloc[i]))

        for intent in intents:
            action = intent['action']
            # 【ADR-075】成交在 B 的開盤價。
            # 【ADR-136】例外:盤中觸價出場要成交在觸價。但**看A做B 時不適用** ——
            # 觸價是在 A 的 K 棒上算出來的,拿 A 的價格當 B 的成交價是錯的,
            # 那種情況仍退回 B 的開盤價 (保守,且與既有模型一致)。
            _px_src = exec_open
            if not _use_watch and intent.get('_touch_price') is not None:
                _px_src = float(intent['_touch_price'])
            fill = _fill_price(_px_src, action)
            if intent['kind'] == 'OPEN':
                # 【ADR-150】與實盤**同一份** risk_check:每日進場上限、冷卻、
                # 熔斷。被擋下要記進訊息紀錄 —— 否則又是「條件到了卻沒動作,
                # 查無原因」(ADR-144 加訊息紀錄就是為了這個)。
                if apply_risk_guards:
                    _ok_rk, _why_rk = strategy_engine.risk_check(s, rt, intent, now_clock)
                    if not _ok_rk:
                        _log(ts, '風控擋單', f"{action} 被擋 — {_why_rk}", collapse=True)
                        continue
                strategy_engine.apply_fill(s, rt, intent, now_clock)
                # 【ADR-053 重大修正】方向必須取自「實際開倉動作」,不能讀
                # s['direction'] 這個靜態欄位:
                #   (1) 自訂 Python 策略的 direction 只是佔位值 (ADR-045),
                #       多空由 on_bar 回傳 BUY/SELL 決定 —— 舊版讓「反手策略」
                #       的每一筆都被記成「做多」(使用者實例:7EMA/25SMA 反手
                #       策略回測明細方向全是做多);
                #   (2) 更嚴重的是下面平倉的 mult 也依賴這個欄位,空單損益
                #       會用做多的符號計算 → 賺賠完全相反,回測結果失真。
                # 買進開倉=做多、賣出開倉=做空,這才是事實。
                open_dir = '做多' if action == '買進' else '做空'
                if is_bnh:
                    # 【ADR-061】累積買進:每次成立就記一「筆」(lot),不覆蓋前一筆。
                    # 期末逐筆結算,交易明細因此看得到每一次買在什麼價位。
                    bnh_lots.append({'entry_ts': ts, 'entry_price': fill,
                                     'direction': open_dir, 'qty': int(intent['qty']),
                                     'entry_i': i})
                    markers.append({'ts': ts, 'price': fill,
                                    'kind': 'buy_open' if action == '買進' else 'sell_open'})
                    continue
                # 【ADR-144】分批進場:每一次進場都是一筆獨立的 lot,
                # 不覆蓋前一筆。平倉時逐筆結算成獨立的交易明細列。
                open_lots.append({
                    'entry_ts': ts, 'entry_price': fill,
                    'direction': open_dir,
                    'qty': int(intent.get('qty', qty) or qty), 'entry_i': i,
                })
                _log(ts, '進場' if len(open_lots) == 1 else '加碼',
                     f"{action} @ {fill:g} (第 {len(open_lots)} 筆) — {intent.get('reason', '')}")
                markers.append({'ts': ts, 'price': fill,
                                'kind': 'buy_open' if action == '買進' else 'sell_open'})
            else:  # CLOSE
                # 【ADR-144】一次平掉所有 lot,逐筆算損益 —— 使用者才看得到
                # 自己是買在哪幾個價位、各賺賠多少。
                _closed_n = len(open_lots)
                _sum_pnl = 0.0
                for _lot in open_lots:
                    lot_qty = int(_lot['qty'])
                    mult = 1.0 if _lot['direction'] == '做多' else -1.0
                    entry_px = _lot['entry_price']
                    # 【ADR-043】乘上單位規模:股票×1000、零股×1、期貨×契約乘數
                    gross = (fill - entry_px) * mult * lot_qty * contract_size
                    # 【ADR-050】真實成本:手續費 + 交易稅。
                    if apply_cost_model:
                        _c = _cost_model.round_trip_cost(
                            tt, str(s.get('symbol', '')), entry_px, fill, lot_qty,
                            contract_size, direction=_lot['direction'], params=cost_params)
                        fee, tax_ = _c['fee'], _c['tax']
                    else:
                        fee = (abs(entry_px) + abs(fill)) * lot_qty * contract_size * float(fee_rate)
                        tax_ = 0.0
                    pnl = gross - (fee + tax_)
                    realized += pnl
                    _sum_pnl += pnl
                    total_fee_sum[0] += fee
                    total_tax_sum[0] += tax_
                    total_gross_sum[0] += gross
                    denom = abs(entry_px) * lot_qty * contract_size
                    trades.append({
                        'entry_ts': _lot['entry_ts'], 'entry_price': entry_px,
                        'exit_ts': ts, 'exit_price': fill,
                        'direction': _lot['direction'], 'qty': lot_qty,
                        'pnl': pnl, 'pnl_pct': (pnl / denom * 100.0) if denom else 0.0,
                        'bars_held': i - _lot['entry_i'],
                        'exit_reason': intent.get('reason', ''),
                    })
                strategy_engine.apply_fill(s, rt, intent, now_clock)
                if _closed_n:
                    _log(ts, '出場',
                         f"{action} @ {fill:g} 平掉 {_closed_n} 筆,合計損益 {_sum_pnl:,.0f}"
                         f" — {intent.get('reason', '')}")
                markers.append({'ts': ts, 'price': fill,
                                'kind': 'buy_close' if action == '買進' else 'sell_close'})
                open_lots = []
        # 記錄權益曲線:已實現 + 目前未平倉浮動損益
        # 【ADR-075】看A做B 時浮動損益用 B 的收盤 (持有的是 B)。
        floating = 0.0
        cur_px = exec_close_px if _use_watch else float(df['Close'].iloc[i])
        if is_bnh:
            # 【ADR-061】累積持有:浮動損益要把「所有已買進的 lot」逐筆算進去,
            # 否則資金曲線只反映最後一筆,長期累積的部位完全看不出來。
            for lot in bnh_lots:
                m = 1.0 if lot['direction'] == '做多' else -1.0
                floating += (cur_px - lot['entry_price']) * m * int(lot['qty']) * contract_size
        else:
            # 【ADR-144】浮動損益要涵蓋**每一筆** lot,不是只算最後一筆。
            for _lot in open_lots:
                m = 1.0 if _lot['direction'] == '做多' else -1.0
                floating += (cur_px - _lot['entry_price']) * m * int(_lot['qty']) * contract_size
        equity.append((ts, realized + floating))
        # 【ADR-144】條件成立卻沒進場 —— 把原因記進訊息紀錄。
        # 這正是「為什麼一年只買一次」最需要的那一行字。
        _blocked = rt.get('blocked_entry') or ''
        if _blocked:
            _log(ts, '未進場', _blocked, collapse=True)

    # ============================================================
    # 【ADR-059】回測期末結算 (settle_open_at_end)
    # ============================================================
    # 舊版跑到最後一根若還有未平倉部位,就直接忽略 —— trades 是空的、
    # metrics 全是 0。「買進後持有不賣」的策略因此完全跑不出績效
    # (使用者需求)。而且這對「任何」在期末仍持倉的策略都是系統性偏差:
    # 最後那筆未實現損益 (可能是大賺也可能是大賠) 完全不會出現在報告上。
    #
    # 修法:期末用最後一根的收盤價做「市價結算 (mark-to-market)」,
    # 產生一筆 exit_reason 標明「回測期末結算(未平倉)」的交易。
    # 成本比照真實平倉計算 —— 因為要跟主動策略比成本,若這裡不算出場成本,
    # Buy & Hold 的成本會被系統性低估,比較就失真了。
    # ============================================================
    # 【ADR-061】買進持有 (累積) 的期末結算:逐筆 lot 以最後收盤價結算
    # ============================================================
    settled_open = 0
    if settle_open_at_end and is_bnh and bnh_lots and len(df) > 0:
        last_ts = df.index[-1]
        last_px = float(_bdf['Close'].iloc[-1])  # 【ADR-075】看A做B 期末以 B 的最後收盤結算
        for lot in bnh_lots:
            lot_qty = int(lot['qty'])
            mult = 1.0 if lot['direction'] == '做多' else -1.0
            entry_px = lot['entry_price']
            gross = (last_px - entry_px) * mult * lot_qty * contract_size
            if apply_cost_model:
                _c = _cost_model.round_trip_cost(
                    tt, str(s.get('symbol', '')), entry_px, last_px, lot_qty, contract_size,
                    direction=lot['direction'], params=cost_params)
                fee, tax_ = _c['fee'], _c['tax']
            else:
                fee = (abs(entry_px) + abs(last_px)) * lot_qty * contract_size * float(fee_rate)
                tax_ = 0.0
            pnl = gross - (fee + tax_)
            realized += pnl
            total_fee_sum[0] += fee
            total_tax_sum[0] += tax_
            total_gross_sum[0] += gross
            denom = abs(entry_px) * lot_qty * contract_size
            trades.append({
                'entry_ts': lot['entry_ts'], 'entry_price': entry_px,
                'exit_ts': last_ts, 'exit_price': last_px,
                'direction': lot['direction'], 'qty': lot_qty,
                'pnl': pnl, 'pnl_pct': (pnl / denom * 100.0) if denom else 0.0,
                'bars_held': (len(df) - 1) - lot['entry_i'],
                'exit_reason': '買進持有-期末結算(未賣出,以最後收盤價計)',
            })
        markers.append({'ts': last_ts, 'price': last_px, 'kind': 'sell_close'})
        settled_open = len(bnh_lots)
        if equity:
            equity[-1] = (equity[-1][0], realized)
        open_lots = []

    if settle_open_at_end and open_lots and len(df) > 0:
        last_ts = df.index[-1]
        last_px = float(_bdf['Close'].iloc[-1])  # 【ADR-075】看A做B 期末以 B 的最後收盤結算
        # 【ADR-144】期末也要逐筆結算,不是只結算「最後一筆」。
        for _lot in open_lots:
            lot_qty = int(_lot['qty'])
            mult = 1.0 if _lot['direction'] == '做多' else -1.0
            entry_px = _lot['entry_price']
            gross = (last_px - entry_px) * mult * lot_qty * contract_size
            if apply_cost_model:
                _c = _cost_model.round_trip_cost(
                    tt, str(s.get('symbol', '')), entry_px, last_px, lot_qty, contract_size,
                    direction=_lot['direction'], params=cost_params)
                fee, tax_ = _c['fee'], _c['tax']
            else:
                fee = (abs(entry_px) + abs(last_px)) * lot_qty * contract_size * float(fee_rate)
                tax_ = 0.0
            pnl = gross - (fee + tax_)
            realized += pnl
            total_fee_sum[0] += fee
            total_tax_sum[0] += tax_
            total_gross_sum[0] += gross
            denom = abs(entry_px) * lot_qty * contract_size
            trades.append({
                'entry_ts': _lot['entry_ts'], 'entry_price': entry_px,
                'exit_ts': last_ts, 'exit_price': last_px,
                'direction': _lot['direction'], 'qty': lot_qty,
                'pnl': pnl, 'pnl_pct': (pnl / denom * 100.0) if denom else 0.0,
                'bars_held': (len(df) - 1) - _lot['entry_i'],
                'exit_reason': '回測期末結算(未平倉,以最後收盤價計)',
            })
            markers.append({'ts': last_ts, 'price': last_px,
                            'kind': 'buy_close' if _lot['direction'] == '做空' else 'sell_close'})
        _log(last_ts, '期末結算', f"未平倉 {len(open_lots)} 筆以最後收盤價 {last_px:g} 結算")
        settled_open = len(open_lots)
        if equity:
            equity[-1] = (equity[-1][0], realized)
        open_lots = []
    _base_override = None
    if is_bnh and bnh_lots:
        # 累積買進:報酬率的分母 = 全部買進的總投入資金
        _base_override = sum(abs(l['entry_price']) * int(l['qty']) * contract_size for l in bnh_lots)
    metrics = _compute_metrics(trades, equity, df, qty, contract_size, total_fee_sum[0],
                               total_tax=total_tax_sum[0], gross_pnl=total_gross_sum[0],
                               cost_desc=_cost_model.describe(tt, cost_params) if apply_cost_model else '未套用成本模型',
                               base_override=_base_override)
    metrics['settled_open_at_end'] = settled_open
    metrics['buy_and_hold_mode'] = is_bnh
    # 【ADR-061】累積買進的專屬指標:使用者要看「總共投入多少、平均成本多少」
    if is_bnh and bnh_lots:
        _tot_qty = sum(int(l['qty']) for l in bnh_lots)
        _invested = sum(abs(l['entry_price']) * int(l['qty']) * contract_size for l in bnh_lots)
        metrics['bnh_buys'] = len(bnh_lots)
        metrics['bnh_total_qty'] = _tot_qty
        metrics['bnh_avg_cost'] = (_invested / (_tot_qty * contract_size)) if _tot_qty else 0.0
        metrics['bnh_total_invested'] = _invested          # 總持有成本 (不含手續費)
        metrics['bnh_total_invested_with_cost'] = _invested + metrics.get('total_cost', 0.0)
        metrics['bnh_final_price'] = float(df['Close'].iloc[-1])
        metrics['bnh_final_value'] = float(df['Close'].iloc[-1]) * _tot_qty * contract_size
    else:
        metrics['bnh_buys'] = 0
        metrics['bnh_total_qty'] = 0
        metrics['bnh_avg_cost'] = 0.0
        metrics['bnh_total_invested'] = 0.0
        metrics['bnh_total_invested_with_cost'] = 0.0
        metrics['bnh_final_price'] = 0.0
        metrics['bnh_final_value'] = 0.0
    # 【ADR-144】跑完沒有任何交易時,訊息紀錄要能回答「為什麼」——
    # 那正是使用者最需要看到的一行字,不是空白的分頁。
    if not trades:
        if not log_rows:
            _log(df.index[-1] if len(df) else None, '總結',
                 '整段回測沒有任何一根 K 棒讓進場條件成立 —— 條件可能太嚴,'
                 '或資料範圍不含符合的行情。')
        else:
            _log(df.index[-1] if len(df) else None, '總結',
                 '整段回測沒有完成任何一筆交易 (有進場但沒平倉,或全被擋下)。')
    # 【ADR-150】把「這次回測有沒有照策略的風控設定跑」寫進訊息紀錄。
    # 沒有這一行,使用者無法分辨「策略本來就只買一次」與「回測忽略了我的設定」——
    # 而那正是這一筆 ADR 要修的那種看不見的落差。
    # 【ADR-151】零股沒有 K 線,回測吃的一定是**整股**的 K 線 —— 這是先天限制
    # (P-06:shioaji 沒有零股 K 線,也沒有零股 snapshot)。不講明的話,使用者會
    # 以為回測的價格就是零股的成交價。
    if _tt_bt == '零股':
        _log(df.index[-1] if len(df) else None, '總結',
             '⚠ 零股**沒有 K 線**,本次回測用的是**整股**的歷史 K 棒 —— '
             '盤中零股是獨立市場、有自己的成交價,實際成交價會與這裡不同。'
             '實盤的零股掛價走的是零股五檔 (ADR-147/149),回測無法模擬 '
             '(沒有歷史五檔資料)。')
    _log(df.index[-1] if len(df) else None, '總結',
         ('本次回測**已套用**策略的風控設定:每日進場上限 '
          f"{s.get('max_trades_per_day')} 次、冷卻 {s.get('cooldown_sec')} 秒、"
          f"每日虧損熔斷 {s.get('daily_loss_limit')}(與實盤同一份 risk_check)。"
          if apply_risk_guards else
          '本次回測**未套用**每日進場上限/冷卻/熔斷 (純訊號績效模式),'
          '結果會比實盤樂觀。'))
    return {'trades': trades, 'equity': equity, 'metrics': metrics, 'markers': markers,
            # 【ADR-144】回測訊息紀錄:每一列 {ts, kind, text}
            'log': log_rows,
            # 【ADR-055】param_given=餵進引擎的參數;param_usage=程式碼實際讀到的參數
            'param_given': dict(s.get('custom_params') or {}) if is_custom else {},
            'param_usage': param_usage}


#: 【ADR-144】回測「訊息紀錄」最多留幾列。
# 十年 5分K 有二十幾萬根,全留會吃光記憶體、Treeview 也塞不動;
# 超過就截斷並在最後一列明說(不是靜默丟掉)。
LOG_MAX_ROWS = 5000


def _relax_realtime_guards(s):
    s = copy.deepcopy(s)
    s['max_trades_per_day'] = 10 ** 9
    s['cooldown_sec'] = 0
    s['daily_loss_limit'] = 0
    return s


def _empty_result():
    return {'trades': [], 'equity': [], 'markers': [], 'log': [],
            'param_given': {}, 'param_usage': {},
            'metrics': {'total_pnl': 0.0, 'total_return_pct': 0.0, 'win_rate': 0.0,
                        'max_drawdown': 0.0, 'trades': 0, 'wins': 0, 'losses': 0,
                        'profit_factor': 0.0, 'avg_bars_held': 0.0,
                        'avg_win': 0.0, 'avg_loss': 0.0,
                        # 【ADR-050】新增成本/擴充指標:無交易時也要齊全,
                        # 否則報告視窗取值會 KeyError。
                        'total_fee': 0.0, 'total_tax': 0.0, 'total_cost': 0.0,
                        'gross_pnl': 0.0, 'cost_desc': '', 'cost_ratio_pct': 0.0,
                        'avg_cost_per_trade': 0.0, 'best_trade': 0.0, 'worst_trade': 0.0,
                        'gross_profit': 0.0, 'gross_loss': 0.0,
                        'long_trades': 0, 'short_trades': 0,
                        'long_pnl': 0.0, 'short_pnl': 0.0,
                        'long_win_rate': 0.0, 'short_win_rate': 0.0,
                        'max_single_loss': 0.0, 'max_single_win': 0.0,
                        'avg_bars_held_all': 0.0, 'exposure_pct': 0.0,
                        'ann_return_pct': 0.0, 'sharpe': 0.0, 'max_drawdown_pct': 0.0,
                        'expectancy': 0.0, 'win_loss_ratio': 0.0,
                        'max_consec_wins': 0, 'max_consec_losses': 0,
                        'max_consec_win_amount': 0.0, 'max_consec_loss_amount': 0.0,
                        'buy_hold_pnl': 0.0, 'buy_hold_pct': 0.0, 'max_bars_held': 0,
                        'cost_basis_first': 0.0, 'cost_basis_total': 0.0, 'cost_basis_max': 0.0,
                        'trades_per_year': 0.0, 'cost_per_year': 0.0, 'years': 0.0,
                        'settled_open_at_end': 0, 'buy_and_hold_mode': False}}


def _compute_metrics(trades, equity, df, qty=1, contract_size=1.0, total_fee=0.0,
                     total_tax=0.0, gross_pnl=0.0, cost_desc='', base_override=None):
    """【ADR-044】專業級回測指標。年化/夏普採簡化模型 (見各欄註解),誠實標示。"""
    if not trades:
        m = _empty_result()['metrics']
        return m
    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # 最大回撤:從權益曲線的歷史高點往下的最大跌幅 (絕對金額)
    peak = None
    max_dd = 0.0
    for _, eq in equity:
        if peak is None or eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    # 報酬率基準:用進場價×數量×單位規模的平均當本金
    # (簡化;期貨無「本金」概念僅供參考)
    #
    # 【ADR-059 修正】舊版這裡漏乘 contract_size,分母因此小了「股票 1000 倍 /
    # 期貨 200 倍」,導致 total_return_pct、ann_return_pct、max_drawdown_pct
    # 三個百分比全部被系統性放大。使用者實例:0050 的報酬率顯示 -53093.43%,
    # 正確值是 -53.09% (正好差 1000 倍);同一份報告裡「每筆交易明細」的
    # 報酬% 欄卻是對的 —— 因為那一欄的分母 (denom) 有乘 contract_size。
    # 兩處算法不一致正是這個 bug 潛伏這麼久沒被發現的原因。
    #
    # 【ADR-061】base_override:累積買進 (Buy & Hold) 的部位是一筆一筆疊上去的,
    # 分母必須用「總投入資金」而不是「每筆的平均」—— 否則 total_pnl 是 119 筆
    # 的總和、分母卻只有 1 筆的規模,報酬率會被放大成 1473% 這種荒謬數字。
    base = 0.0
    for t in trades:
        base += abs(t['entry_price']) * t.get('qty', qty) * contract_size
    base = base / len(trades) if trades else 0.0
    if base_override is not None and base_override > 0:
        base = float(base_override)
    # --- 【ADR-044】進階指標 ---
    # 期間年數 (依 df 時間跨度);年化=總報酬/年數 (線性簡化,非複利)
    try:
        years = max((df.index[-1] - df.index[0]).days, 1) / 365.0
    except Exception:
        years = 1.0
    # 夏普比率:每根K棒權益變化的均值/標準差×sqrt(每年K棒數) (無風險利率取0)
    sharpe = 0.0
    try:
        eqs = [e for _, e in equity]
        rets = [eqs[i] - eqs[i - 1] for i in range(1, len(eqs))]
        if len(rets) > 2:
            import statistics
            mu = statistics.mean(rets)
            sd = statistics.pstdev(rets)
            bars_per_year = len(eqs) / years if years > 0 else len(eqs)
            if sd > 1e-12:
                sharpe = mu / sd * (bars_per_year ** 0.5)
    except Exception:
        pass
    # 最大回撤 %:相對「本金基準+歷史高點」
    peak2 = None
    max_dd_pct = 0.0
    cap_base = base if base else 1.0
    for _, eq in equity:
        v = cap_base + eq
        if peak2 is None or v > peak2:
            peak2 = v
        if peak2 > 0:
            ddp = (peak2 - v) / peak2 * 100.0
            if ddp > max_dd_pct:
                max_dd_pct = ddp
    # 連續勝敗 (筆數) 與【ADR-057】連續勝敗「累積金額」
    # 兩者意義不同,要分開看:「最大連敗 5 筆」講的是次數,「最大連續虧損
    # 金額 -12,340」講的是那段連敗總共賠掉多少錢 —— 後者才是實際撐不撐得住
    # 的關鍵 (次數少但單筆巨額的連虧,比次數多卻都小賠的連虧更危險)。
    max_cw = max_cl = cw = cl = 0
    max_cw_amt = 0.0   # 最大連續獲利金額 (正數)
    max_cl_amt = 0.0   # 最大連續虧損金額 (負數)
    run_win_amt = 0.0
    run_loss_amt = 0.0
    for p in pnls:
        if p > 0:
            cw += 1; cl = 0
            run_win_amt += p; run_loss_amt = 0.0
        elif p < 0:
            cl += 1; cw = 0
            run_loss_amt += p; run_win_amt = 0.0
        else:
            cw = cl = 0
            run_win_amt = run_loss_amt = 0.0
        max_cw = max(max_cw, cw); max_cl = max(max_cl, cl)
        max_cw_amt = max(max_cw_amt, run_win_amt)
        max_cl_amt = min(max_cl_amt, run_loss_amt)
    avg_win_v = (gross_win / len(wins)) if wins else 0.0
    avg_loss_v = (gross_loss / len(losses)) if losses else 0.0
    # 買進持有對照:第一根買、最後一根賣 (同數量同單位)
    try:
        bh_pnl = (float(df['Close'].iloc[-1]) - float(df['Close'].iloc[2])) * qty * contract_size
        bh_pct = (bh_pnl / (float(df['Close'].iloc[2]) * qty * contract_size) * 100.0)
    except Exception:
        bh_pnl, bh_pct = 0.0, 0.0
    return {
        'ann_return_pct': ((total_pnl / base * 100.0) / years) if (base and years > 0) else 0.0,
        'sharpe': sharpe,
        'max_drawdown_pct': max_dd_pct,
        'expectancy': total_pnl / len(trades),
        'win_loss_ratio': (avg_win_v / avg_loss_v) if avg_loss_v > 1e-12 else (float('inf') if avg_win_v > 0 else 0.0),
        # ============================================================
        # 【ADR-059】持有成本 / 週轉率 (使用者需求:要能跟 Buy & Hold 比較)
        # ============================================================
        # 「總持有成本」在中文語境有兩種常見意思,兩個都給,標籤講清楚:
        #   1. 建倉成本 (cost_basis):買進部位本身佔用的資金
        #      = 進場價 × 數量 × 單位規模。Buy & Hold 只建倉一次,
        #        主動策略每次進場都要動用這筆資金 —— 這是「要準備多少錢」。
        #   2. 交易總成本 (total_cost,既有欄位):手續費 + 交易稅的總和。
        #        這是「摩擦成本」,也是 Buy & Hold 相對主動策略最大的優勢來源。
        # 另外給「每年交易次數 (週轉率)」:Buy & Hold 一年 0.x 次、
        # 當沖策略一年上百次,這個數字最能一眼看出兩者的成本結構差異。
        'cost_basis_first': (abs(trades[0]['entry_price']) * trades[0].get('qty', qty) * contract_size)
                            if trades else 0.0,
        'cost_basis_total': sum(abs(t['entry_price']) * t.get('qty', qty) * contract_size for t in trades),
        'cost_basis_max': max((abs(t['entry_price']) * t.get('qty', qty) * contract_size for t in trades),
                              default=0.0),
        'trades_per_year': (len(trades) / years) if years > 0 else 0.0,
        'cost_per_year': ((total_fee + total_tax) / years) if years > 0 else 0.0,
        'years': years,
        'max_consec_wins': max_cw,
        'max_consec_losses': max_cl,
        # 【ADR-057】連續勝/敗期間的累積金額 (與筆數並列顯示)
        'max_consec_win_amount': max_cw_amt,
        'max_consec_loss_amount': max_cl_amt,
        'total_fee': total_fee,
        'total_tax': total_tax,
        'total_cost': total_fee + total_tax,
        'gross_pnl': gross_pnl,
        'cost_desc': cost_desc,
        'cost_ratio_pct': ((total_fee + total_tax) / abs(gross_pnl) * 100.0) if abs(gross_pnl) > 1e-9 else 0.0,
        'avg_cost_per_trade': ((total_fee + total_tax) / len(trades)) if trades else 0.0,
        'best_trade': max(t['pnl'] for t in trades),
        'worst_trade': min(t['pnl'] for t in trades),
        'gross_profit': sum(t['pnl'] for t in trades if t['pnl'] > 0),
        'gross_loss': abs(sum(t['pnl'] for t in trades if t['pnl'] < 0)),
        'long_trades': sum(1 for t in trades if t.get('direction') == '做多'),
        'short_trades': sum(1 for t in trades if t.get('direction') == '做空'),
        # 【ADR-053】多空分開統計:反手 (Stop and Reverse) 策略必須看得出
        # 多單與空單各自的貢獻,否則無法判斷是哪一邊在賺/賠。
        'long_pnl': sum(t['pnl'] for t in trades if t.get('direction') == '做多'),
        'short_pnl': sum(t['pnl'] for t in trades if t.get('direction') == '做空'),
        'long_win_rate': (sum(1 for t in trades if t.get('direction') == '做多' and t['pnl'] > 0)
                          / max(1, sum(1 for t in trades if t.get('direction') == '做多')) * 100.0),
        'short_win_rate': (sum(1 for t in trades if t.get('direction') == '做空' and t['pnl'] > 0)
                           / max(1, sum(1 for t in trades if t.get('direction') == '做空')) * 100.0),
        'max_single_loss': min([t['pnl'] for t in trades if t['pnl'] < 0], default=0.0),
        'max_single_win': max([t['pnl'] for t in trades if t['pnl'] > 0], default=0.0),
        'avg_bars_held_all': (sum(t['bars_held'] for t in trades) / len(trades)) if trades else 0.0,
        'exposure_pct': (sum(t['bars_held'] for t in trades) / len(df) * 100.0) if len(df) else 0.0,
        'buy_hold_pnl': bh_pnl,
        'buy_hold_pct': bh_pct,
        'max_bars_held': max(t['bars_held'] for t in trades),
        'total_pnl': total_pnl,
        'total_return_pct': (total_pnl / base * 100.0) if base else 0.0,
        'win_rate': len(wins) / len(trades) * 100.0,
        'max_drawdown': max_dd,
        'trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'profit_factor': (gross_win / gross_loss) if gross_loss > 0 else (float('inf') if gross_win > 0 else 0.0),
        'avg_bars_held': sum(t['bars_held'] for t in trades) / len(trades),
        'avg_win': (gross_win / len(wins)) if wins else 0.0,
        'avg_loss': (-gross_loss / len(losses)) if losses else 0.0,
    }


# ============================================================
# 【ADR-057】回測報告自我驗算 (使用者需求 #5:「要怎麼確認是對的回測報告?」)
# ============================================================

def audit_result(result, strategy=None):
    """對一份回測結果做「獨立重算對帳」,回傳檢查項目清單。

    為什麼需要這個:使用者沒有辦法用肉眼判斷一份報告是不是算對的,只能選擇
    相信。這個函式的作法是「**不看** _compute_metrics 怎麼算的,直接從最原始
    的 trades 明細用最直白的方式重算一次」,再跟報告上的數字對帳 —— 兩條
    獨立路徑算出同一個答案,才有理由相信報告;不一致就把差異直接指出來。

    誠實界定它能保證什麼、不能保證什麼:
      ✅ 能抓到:彙總層的計算錯誤 (加總、勝率、獲利因子、連續勝敗、最大回撤
         與交易明細不一致),以及明細本身的內部矛盾 (損益方向與進出場價相反、
         出場時間早於進場時間)。
      ❌ 不能保證:訊號判定本身是否符合你的策略意圖 (那要看策略邏輯),
         也不能保證成本模型的費率設定與你的券商實際收費一致。
         換句話說,全部通過 = 「報告忠實反映了這些交易」,不等於
         「這個策略在真實市場一定會這樣成交」。

    回傳: [{'name':檢查項, 'ok':bool, 'detail':說明}, ...]
    """
    checks = []
    trades = result.get('trades') or []
    m = result.get('metrics') or {}

    def _add(name, ok, detail=""):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    if not trades:
        _add("有交易可驗算", False, "這份報告沒有任何完成的交易,無法對帳。")
        return checks

    tol = 0.01  # 浮點容差:金額對到小數點後兩位即視為相同

    # 1) 淨損益 = 每筆損益直接加總
    recomputed = sum(float(t.get('pnl', 0.0)) for t in trades)
    reported = float(m.get('total_pnl', 0.0))
    _add("淨損益 = 每筆損益加總", abs(recomputed - reported) <= tol,
         f"重算 {recomputed:,.2f} vs 報告 {reported:,.2f}")

    # 2) 交易次數 / 勝負筆數
    wins = [t for t in trades if float(t.get('pnl', 0)) > 0]
    losses = [t for t in trades if float(t.get('pnl', 0)) < 0]
    _add("交易次數一致", int(m.get('trades', -1)) == len(trades),
         f"重算 {len(trades)} vs 報告 {m.get('trades')}")
    _add("勝/負筆數一致",
         int(m.get('wins', -1)) == len(wins) and int(m.get('losses', -1)) == len(losses),
         f"重算 {len(wins)}/{len(losses)} vs 報告 {m.get('wins')}/{m.get('losses')}")

    # 3) 勝率 = 獲利筆數 / 總筆數
    wr = len(wins) / len(trades) * 100.0
    _add("勝率 = 獲利筆數 ÷ 總筆數", abs(wr - float(m.get('win_rate', 0))) <= 0.05,
         f"重算 {wr:.2f}% vs 報告 {float(m.get('win_rate', 0)):.2f}%")

    # 4) 獲利因子 = 總獲利 / 總虧損
    gp = sum(float(t['pnl']) for t in wins)
    gl = abs(sum(float(t['pnl']) for t in losses))
    pf = (gp / gl) if gl > 0 else (float('inf') if gp > 0 else 0.0)
    rep_pf = float(m.get('profit_factor', 0))
    pf_ok = (pf == rep_pf) if (pf in (0.0, float('inf'))) else abs(pf - rep_pf) <= 0.01
    _add("獲利因子 = 總獲利 ÷ 總虧損", pf_ok,
         f"重算 {pf:.4f} vs 報告 {rep_pf:.4f} (總獲利 {gp:,.2f} / 總虧損 {gl:,.2f})")

    # 5) 最大連續虧損金額 (獨立重算一次)
    run = 0.0; worst = 0.0
    for t in trades:
        p = float(t.get('pnl', 0))
        run = run + p if p < 0 else 0.0
        worst = min(worst, run)
    _add("最大連續虧損金額一致",
         abs(worst - float(m.get('max_consec_loss_amount', 0.0))) <= tol,
         f"重算 {worst:,.2f} vs 報告 {float(m.get('max_consec_loss_amount', 0.0)):,.2f}")

    # 6) 最大回撤 ≥ 最大單筆虧損的絕對值 (定義上的必然關係)
    #    ⚠ 這裡必須只看「虧損的那些筆」。第一版寫成 min(所有 pnl),當策略
    #    全勝時 min() 會回傳「最小的獲利」(正數),被當成虧損拿去比較,
    #    於是全勝的報告反而被判定不一致 —— 由 diag 案例抓到。
    mdd = float(m.get('max_drawdown', 0.0))
    loss_pnls = [float(t['pnl']) for t in trades if float(t.get('pnl', 0)) < 0]
    worst_single = abs(min(loss_pnls)) if loss_pnls else 0.0
    _add("最大回撤 ≥ 最大單筆虧損", mdd + tol >= worst_single,
         f"最大回撤 {mdd:,.2f} vs 最大單筆虧損 {worst_single:,.2f} "
         f"(回撤是資金曲線的累積跌幅,理論上不會小於任何單筆虧損)")

    # 7) 每筆明細內部一致性:價格往「不利方向」走時不可能賺錢
    #    ⚠ 這裡刻意只檢查「不利方向卻獲利」這個單向的矛盾。
    #    反過來的「有利方向卻虧損」是完全合理的 —— 小幅獲利被手續費與交易稅
    #    吃成淨虧損,每天都在發生。而且損益是「金額」(已乘上口數與契約乘數),
    #    進出場價差是「點數」,兩者單位不同,不能直接比大小來判斷「虧太多」;
    #    第一版就是這樣寫才會對真實回測結果誤報 (期貨契約乘數 200 讓金額比
    #    點數大兩個數量級)。寧可少抓,也不要給假警報。
    bad_dir = []
    for i, t in enumerate(trades, 1):
        try:
            ep, xp = float(t['entry_price']), float(t['exit_price'])
            pnl = float(t['pnl'])
            if ep == xp:
                continue  # 平盤出場,損益完全由成本決定,不判斷
            favorable = (xp > ep) if t.get('direction') == '做多' else (xp < ep)
            if (not favorable) and pnl > 0:
                bad_dir.append(i)
        except (KeyError, TypeError, ValueError):
            continue
    _add("不利價格方向不可能獲利", not bad_dir,
         "全部相符" if not bad_dir else f"第 {bad_dir[:10]} 筆:價格往不利方向走卻是獲利,明細有矛盾")

    # 8) 時序合理性:出場不得早於進場
    bad_ts = []
    for i, t in enumerate(trades, 1):
        try:
            if t.get('exit_ts') is not None and t.get('entry_ts') is not None:
                if t['exit_ts'] < t['entry_ts']:
                    bad_ts.append(i)
        except Exception:
            continue
    _add("出場時間不早於進場時間", not bad_ts,
         "全部合理" if not bad_ts else f"第 {bad_ts[:10]} 筆時序異常")

    # 9) 持有K棒數不可為負
    #    ⚠ 不可要求 ≥1:累積買進模式下,最後一根K棒才成立的那次買進,
    #    期末隨即結算,持有根數就是 0 —— 這完全合法。第一版寫成 ≥1 會對
    #    正確的報告誤報 (由實測抓到)。
    bad_bars = [i for i, t in enumerate(trades, 1) if int(t.get('bars_held', 0)) < 0]
    _add("持有K棒數 ≥ 0", not bad_bars,
         "全部正常" if not bad_bars else f"第 {bad_bars[:10]} 筆持有根數為負")

    return checks
