# -*- coding: utf-8 -*-
"""
core/custom_strategy.py — 使用者自訂 Python 策略的執行器

【ADR-040】設計與安全立場 (務必誠實理解):
  * Python 在「同一個行程內」沒有辦法做到真正安全的沙盒 —— 網路上用 exec()
    擋關鍵字的做法都能被繞過。本模組因此「不假裝」提供防惡意程式的沙盒。
  * 實際防護是務實的三道:
      1. 子行程隔離 + 逾時 (由 stock_app_pro.py 的 subprocess 端負責):
         策略崩潰/無窮迴圈/卡死,只會殺掉那個子行程,不會拖垮看盤主程式。
      2. 下單與否 100% 仍由既有三層防護 + strategy_engine.risk_check 決定:
         策略只能「回傳想做什麼」,即使寫錯狂喊買進,每日次數上限/冷卻/
         熔斷照樣擋住。這是比假沙盒更實在的保護。
      3. 鐵則:只執行「使用者自己寫的、看得懂的」策略,絕不貼陌生程式。
         這條會顯示在 GUI 編輯器最上方。

介面約定 —— 使用者的 .py 只需實作一個函式:

    def on_bar(ctx):
        # ctx.df       : 已收盤K棒 DataFrame (index=時間, 欄位 Open/High/Low/Close/Volume)
        # ctx.close    : 最新收盤價 (float)
        # ctx.position : 'FLAT' / 'LONG' / 'SHORT'
        # ctx.sma(n) / ctx.ema(n) / ctx.rsi(n) / ctx.kd(n,m1,m2) / ctx.macd(f,s,sig)
        #   / ctx.bb(n,std) / ctx.highest(n) / ctx.lowest(n) : 現成指標 (回傳 pandas.Series 或值)
        # ctx.param(key, default) : 讀取策略參數 (編輯器裡填的自訂參數)
        # 回傳: ctx.buy() / ctx.sell() / ctx.close_position() / ctx.hold() (或 None)
        ...

本模組的純邏輯部分 (Ctx、decide_from_source) 可離線測試;真正的子行程啟動
在 stock_app_pro.py。
"""
import pandas as pd

from . import strategy_engine as se


class StrategyError(Exception):
    pass


class Ctx:
    """傳給使用者 on_bar 的環境物件。指標方法直接復用 strategy_engine 的實作。"""

    BUY = 'BUY'
    SELL = 'SELL'
    CLOSE = 'CLOSE'
    HOLD = 'HOLD'

    def __init__(self, df, position, params=None, state=None, entry_price=0.0, bars_in_position=0,
                full_df=None):
        self.df = df
        self.position = position
        self._params = params or {}
        # 【ADR-055】key -> {'value','from_params','default'};on_bar 每次呼叫 param 就記一筆
        self.param_reads = {}
        # 【ADR-044】自由度升級:
        self.state = state if isinstance(state, dict) else {}   # 跨K棒持久狀態 (可做移動停損等)
        self.entry_price = float(entry_price or 0.0)             # 目前持倉進場價 (FLAT=0)
        self.bars_in_position = int(bars_in_position or 0)       # 已持倉K棒數
        self._logs = []
        # 【ADR-056】full_df = 這次回測「全部」的歷史資料 (含尚未走到的未來列)。
        # self.df 仍是「只到目前這根」的截斷視窗 (回測防未來函數的既有保證,
        # P-49 不變)——策略程式碼永遠只透過 sma()/ema()/... 這些方法拿資料,
        # 而這些方法回傳前一律裁到 len(self.df),絕不會把 full_df 較晚的列
        # 洩露給策略。full_df 只用來讓「同一個指標、同一組參數」在整段回測
        # 只需算一次 (O(n)),不必每根K棒都對著越長的視窗重算一次 (原本
        # O(n²):2500根日K的回測就要算 ~310萬個指標點,參數最佳化掃100組
        # 參數就是 3億點,這是使用者回報「最佳化跑很久」的根本原因)。
        # 不傳就退回舊行為 (full_df=df,等於沒有快取效益但結果完全相同)。
        self.full_df = full_df if full_df is not None else df

    def log(self, msg):
        """除錯輸出:試跑/實盤日誌會顯示 (每根最多前5條)。"""
        if len(self._logs) < 5:
            self._logs.append(str(msg)[:200])

    @property
    def close(self):
        return float(self.df['Close'].iloc[-1])

    @property
    def open(self):
        return float(self.df['Open'].iloc[-1])

    @property
    def high(self):
        return float(self.df['High'].iloc[-1])

    @property
    def low(self):
        return float(self.df['Low'].iloc[-1])

    @property
    def volume(self):
        return float(self.df['Volume'].iloc[-1]) if 'Volume' in self.df else 0.0

    @property
    def time(self):
        return self.df.index[-1]

    # --- 【ADR-044】更多指標 (ADR-056:一併套用快取) ---
    def atr(self, n=14):
        def _calc(d):
            h, l, c = d['High'], d['Low'], d['Close']
            tr = (h - l).combine((h - c.shift()).abs(), max).combine((l - c.shift()).abs(), max)
            return tr.rolling(int(n)).mean()
        return self._cached(('atr', int(n)), _calc)

    def roc(self, n=10):
        return self._cached(('roc', int(n)), lambda d: d['Close'].pct_change(int(n)) * 100.0)

    def stddev(self, n=20):
        return self._cached(('stddev', int(n)), lambda d: d['Close'].rolling(int(n)).std())

    def vwap(self):
        def _calc(d):
            v = d['Volume'].replace(0, 1)
            tp = (d['High'] + d['Low'] + d['Close']) / 3.0
            return (tp * v).cumsum() / v.cumsum()
        return self._cached(('vwap',), _calc)

    def param(self, key, default=None):
        """讀取參數視窗設定的參數;沒設定就用 default。

        【ADR-055】同時把「這支程式實際讀了哪些參數、值從哪來」記錄到
        self.param_reads,讓回測報告能誠實顯示「策略程式真正吃到的參數」,
        使用者不必靠猜的判斷參數視窗有沒有正確代入 (例如 key 拼錯時,
        報告會標示該參數來源是「程式預設」而不是「參數視窗」)。
        """
        hit = key in self._params
        val = self._params.get(key, default)
        self.param_reads[key] = {'value': val, 'from_params': hit, 'default': default}
        return val

    # --- 【ADR-056】指標結果快取:同一組 (指標,參數) 全回測只算一次 ---
    def _cached(self, cache_key, compute):
        """
        compute(full_df) → Series 或 (Series, Series, ...) 的 tuple。

        安全性 (不可違反 P-49「絕不用未來K棒」):compute 永遠餵 self.full_df
        (完整資料) 去算,但回傳給策略程式碼前一律裁到 `len(self.df)`——也就是
        「只到目前這根」。裁切在每次呼叫都做,不會因為快取命中就跳過,所以
        策略程式碼看到的數值與『每根都只用截斷視窗重算』完全一致,只是不用
        真的重算。快取存在 self.state,跨K棒存活 (state 本身就是回測/實盤
        共用的跨根持久狀態,ADR-044)。
        """
        cache = self.state.setdefault('_ind_cache', {})
        n = len(self.df)
        hit = cache.get(cache_key)
        if hit is None or self._cached_len(hit) < n:
            hit = compute(self.full_df)
            cache[cache_key] = hit
        return self._slice_to(hit, n)

    @staticmethod
    def _cached_len(v):
        if isinstance(v, tuple):
            return len(v[0])
        return len(v)

    @staticmethod
    def _slice_to(v, n):
        if isinstance(v, tuple):
            return tuple(x.iloc[:n] for x in v)
        return v.iloc[:n]

    # --- 指標 (復用引擎,確保與內建策略/回測同一套計算) ---
    def sma(self, n):
        return self._cached(('sma', int(n)), lambda d: se._sma(d['Close'], n))

    def ema(self, n):
        return self._cached(('ema', int(n)), lambda d: se._ema(d['Close'], n))

    def rsi(self, n=14):
        return self._cached(('rsi', int(n)), lambda d: se._rsi(d['Close'], n))

    def kd(self, n=9, m1=3, m2=3):
        return self._cached(('kd', int(n), int(m1), int(m2)), lambda d: se._kd(d, n, m1, m2))  # (K, D)

    def macd(self, f=12, s=26, sig=9):
        return self._cached(('macd', int(f), int(s), int(sig)),
                            lambda d: se._macd(d['Close'], f, s, sig))  # (dif, signal, hist)

    def bb(self, n=20, std=2.0):
        return self._cached(('bb', int(n), float(std)), lambda d: se._bb(d['Close'], n, std))  # (upper, mid, lower)

    def highest(self, n):
        return self._cached(('highest', int(n)), lambda d: d['High'].rolling(int(n)).max())

    def lowest(self, n):
        return self._cached(('lowest', int(n)), lambda d: d['Low'].rolling(int(n)).min())

    def cross_up(self, a, b):
        return se._cross_up(a, b)

    def cross_down(self, a, b):
        return se._cross_down(a, b)

    # --- 決策回傳 ---
    def buy(self):
        return self.BUY

    def sell(self):
        return self.SELL

    def close_position(self):
        return self.CLOSE

    def hold(self):
        return self.HOLD


def normalize_decision(ret):
    """把 on_bar 的回傳值正規化成 BUY/SELL/CLOSE/HOLD;無法辨識一律 HOLD (安全)。"""
    if ret is None:
        return Ctx.HOLD
    s = str(ret).strip().upper()
    if s in (Ctx.BUY, Ctx.SELL, Ctx.CLOSE, Ctx.HOLD):
        return s
    # 容忍中文/常見字
    mapping = {'買': Ctx.BUY, '買進': Ctx.BUY, 'LONG': Ctx.BUY,
               '賣': Ctx.SELL, '賣出': Ctx.SELL, 'SHORT': Ctx.SELL,
               '平倉': Ctx.CLOSE, 'EXIT': Ctx.CLOSE, 'FLAT': Ctx.CLOSE,
               '': Ctx.HOLD, 'NONE': Ctx.HOLD, 'PASS': Ctx.HOLD}
    return mapping.get(s, Ctx.HOLD)


# 【ADR-056】策略原始碼 compile+exec 只需做一次:回測逐根K棒呼叫 run_on_bar,
# 舊版每根都重新 compile(source_code) + exec() 再取一次 on_bar,對 2500+ 根
# 的日K回測是純浪費 (程式碼從沒變過)。用 source_code 全文當 key 快取編譯結果;
# 不同策略 (原始碼不同) 各自有獨立的 key,不會互相污染。有界大小防止無限增長
# (同一支程式在最佳化裡對很多參數組合重複呼叫,參數是走 ctx.param() 動態讀,
# 不影響原始碼文字,所以同一策略掃 100 組參數只需編譯 1 次)。
_COMPILE_CACHE = {}
_COMPILE_CACHE_MAX = 64


def _compile_on_bar(source_code):
    """回傳 (fn, error_msg)。fn 為 None 時 error_msg 說明原因。"""
    fn = _COMPILE_CACHE.get(source_code)
    if fn is not None:
        return fn, None
    ns = {}
    try:
        compiled = compile(source_code, '<custom_strategy>', 'exec')
        exec(compiled, ns)
    except Exception as e:
        return None, f"策略程式碼無法載入 (語法錯誤?): {type(e).__name__}: {e}"
    fn = ns.get('on_bar')
    if not callable(fn):
        return None, "策略檔必須定義 on_bar(ctx) 函式"
    if len(_COMPILE_CACHE) >= _COMPILE_CACHE_MAX:
        _COMPILE_CACHE.pop(next(iter(_COMPILE_CACHE)))
    _COMPILE_CACHE[source_code] = fn
    return fn, None


def run_on_bar(source_code, df, position, params=None, state=None, entry_price=0.0, bars_in_position=0,
              return_ctx=False, full_df=None):
    """
    在「本行程」執行使用者策略碼並取得決策 (BUY/SELL/CLOSE/HOLD)。
    ⚠ 這是純執行,不含子行程隔離;子行程端 (stock_app_pro) 會呼叫這個函式。
    直接離線測試時也用它。

    full_df:【ADR-056】回測時傳入「這次回測全部」的資料,讓 Ctx 內的指標
    計算可以整段只算一次而非每根重算 (見 Ctx._cached 說明)。不傳 (實盤/
    舊呼叫端) 就等於 full_df=df,行為與快取前完全相同。

    任何例外都包成 StrategyError 往上拋 (呼叫端決定如何處理:記錄+停用該策略)。
    """
    if df is None or len(df) < 2:
        return Ctx.HOLD
    fn, err = _compile_on_bar(source_code)
    if fn is None:
        raise StrategyError(err)
    ctx = Ctx(df, position, params, state=state, entry_price=entry_price, bars_in_position=bars_in_position,
             full_df=full_df)
    try:
        ret = fn(ctx)
    except Exception as e:
        raise StrategyError(f"on_bar 執行出錯: {type(e).__name__}: {e}")
    d = normalize_decision(ret)
    if return_ctx:
        return d, ctx
    return d


def decision_to_intent(decision, strategy, runtime, close_price):
    """
    把自訂策略的決策 (BUY/SELL/CLOSE/HOLD) + 目前持倉,轉成與內建策略同格式的
    intent (OPEN/CLOSE),以便下游 risk_check / apply_fill / 回測完全同路。

    語意 (與內建狀態機一致):
      FLAT + BUY  -> 開多 (OPEN 買進);  FLAT + SELL -> 開空 (OPEN 賣出, 僅期貨)
      LONG + (SELL 或 CLOSE) -> 平多 (CLOSE 賣出)
      SHORT+ (BUY  或 CLOSE) -> 平空 (CLOSE 買進)
      其餘 (含 HOLD) -> 無動作
    回傳 intent dict 或 None。
    """
    state = runtime.get('state', 'FLAT')
    qty = int(strategy.get('qty', 1) or 1)
    allow_short = strategy.get('market') == '台期貨'
    if state == 'FLAT':
        if decision == Ctx.BUY:
            return {'kind': 'OPEN', 'action': '買進', 'qty': qty, 'price': close_price,
                    'reason': '自訂策略: 開多訊號'}
        if decision == Ctx.SELL and allow_short:
            return {'kind': 'OPEN', 'action': '賣出', 'qty': qty, 'price': close_price,
                    'reason': '自訂策略: 開空訊號'}
        return None
    if state == 'LONG':
        if decision in (Ctx.SELL, Ctx.CLOSE):
            return {'kind': 'CLOSE', 'action': '賣出', 'qty': int(runtime.get('qty', qty)),
                    'price': close_price, 'reason': '自訂策略: 平多訊號'}
        return None
    if state == 'SHORT':
        if decision in (Ctx.BUY, Ctx.CLOSE):
            return {'kind': 'CLOSE', 'action': '買進', 'qty': int(runtime.get('qty', qty)),
                    'price': close_price, 'reason': '自訂策略: 平空訊號'}
        return None
    return None


def decision_to_intents(decision, strategy, runtime, close_price, bar_ts_str=None):
    """
    【ADR-053】decision_to_intent 的複數版:支援「反手 (Stop and Reverse)」。

    存在理由:單數版在 LONG 遇到 SELL 時只回一個「平多」intent,持倉變 FLAT
    就結束,同一根K棒不會反向開空 —— 對反手策略而言等於少做一半的單,
    回測與實盤都會與策略作者的意圖不符。複數版把同一根K棒該做的動作
    依序全部回傳,呼叫端逐一 apply_fill 即可。

    語意:
      FLAT  + BUY            -> [開多]
      FLAT  + SELL (限期貨)  -> [開空]
      LONG  + SELL (限期貨)  -> [平多, 開空]   ← 反手
      LONG  + SELL (非期貨)  -> [平多]         ← 台股不可放空,只平不反
      LONG  + CLOSE          -> [平多]         ← CLOSE 是「單純出場」,不反手
      SHORT + BUY  (限期貨)  -> [平空, 開多]   ← 反手
      SHORT + CLOSE          -> [平空]
      其餘 (含 HOLD)         -> []

    CLOSE 與 SELL/BUY 的差別是刻意的:策略作者寫 ctx.close_position() 表示
    「我要出場」,寫 ctx.sell() 表示「我看空」——後者在期貨上才有反手意義。

    回傳 list[intent];單數版 decision_to_intent 保留不動 (向下相容,
    仍供只需要單一動作的呼叫端使用)。
    """
    state = runtime.get('state', 'FLAT')
    qty = int(strategy.get('qty', 1) or 1)
    allow_short = strategy.get('market') == '台期貨'
    hold_qty = int(runtime.get('qty', qty) or qty)

    from . import strategy_engine
    if state == 'FLAT':
        i = decision_to_intent(decision, strategy, runtime, close_price)
        out = [i] if i else []
        return strategy_engine.filter_intents_by_time(out, strategy, bar_ts_str, runtime=runtime)

    if state == 'LONG':
        if decision not in (Ctx.SELL, Ctx.CLOSE):
            return []
        out = [{'kind': 'CLOSE', 'action': '賣出', 'qty': hold_qty,
                'price': close_price, 'reason': '自訂: 多單平倉'}]
        if decision == Ctx.SELL and allow_short:
            out.append({'kind': 'OPEN', 'action': '賣出', 'qty': qty,
                        'price': close_price, 'reason': '自訂: 反手開空'})
        return strategy_engine.filter_intents_by_time(out, strategy, bar_ts_str, runtime=runtime)

    if state == 'SHORT':
        if decision not in (Ctx.BUY, Ctx.CLOSE):
            return []
        out = [{'kind': 'CLOSE', 'action': '買進', 'qty': hold_qty,
                'price': close_price, 'reason': '自訂: 空單平倉'}]
        if decision == Ctx.BUY:
            if allow_short:
                out.append({'kind': 'OPEN', 'action': '買進', 'qty': qty,
                            'price': close_price, 'reason': '自訂: 反手開多'})
        return strategy_engine.filter_intents_by_time(out, strategy, bar_ts_str, runtime=runtime)

    return []

def describe_param_usage(param_given, param_usage):
    """
    【ADR-055】把「參數視窗設了什麼」與「策略程式實際讀到什麼」對照成一句話,
    供回測報告與系統日誌顯示。使用者藉此直接看出參數有沒有正確代入,
    不需要靠猜或在程式裡自己插 print。

    三種狀況會分別標示:
      ✓ 有代入   —— 參數視窗有設,程式也讀到了 (顯示值)
      ⚠ 未使用   —— 參數視窗有設,但程式從頭到尾沒讀這個 key (常見是拼錯字)
      ○ 程式預設 —— 程式讀了,但參數視窗沒設,吃 ctx.param 的 default

    回傳字串;非自訂策略 (兩者皆空) 回傳空字串。
    """
    given = dict(param_given or {})
    usage = dict(param_usage or {})
    if not given and not usage:
        return ""
    parts = []
    for k in sorted(usage):
        u = usage[k]
        if u.get('from_params'):
            parts.append(f"✓ {k}={u.get('value')} (參數視窗)")
        else:
            parts.append(f"○ {k}={u.get('value')} (程式預設,參數視窗未設)")
    for k in sorted(given):
        if k not in usage:
            parts.append(f"⚠ {k}={given[k]} (參數視窗有設,但策略程式沒讀到,請檢查 ctx.param 的 key 是否拼錯)")
    if not parts:
        return "策略程式未使用任何 ctx.param 參數。"
    return " ; ".join(parts)


# 一份可直接用的範例策略 (GUI 新增自訂策略時預填,教使用者怎麼寫)
EXAMPLE_SOURCE = '''# 自訂策略範例:5 日均線上穿 20 日均線做多,下穿平倉。
# 只需實作 on_bar(ctx)，回傳 ctx.buy() / ctx.sell() / ctx.close_position() / None。
# 可用指標: ctx.sma(n) ctx.ema(n) ctx.rsi(n) ctx.kd() ctx.macd() ctx.bb()
#           ctx.highest(n) ctx.lowest(n) ctx.cross_up(a,b) ctx.cross_down(a,b)
# 狀態: ctx.position 為 'FLAT'/'LONG'/'SHORT';ctx.close 為最新收盤價。
# 參數: ctx.param('fast', 5) 讀編輯器裡自訂的參數。

def on_bar(ctx):
    fast = ctx.sma(ctx.param('fast', 5))
    slow = ctx.sma(ctx.param('slow', 20))
    if ctx.position == 'FLAT':
        if ctx.cross_up(fast, slow):
            return ctx.buy()
    elif ctx.position == 'LONG':
        # 進階範例:移動停損 —— 用 ctx.state 記錄持倉期間最高價,回檔 3% 出場
        hi = max(ctx.state.get('hi', 0.0), ctx.close)
        ctx.state['hi'] = hi
        if ctx.entry_price > 0 and ctx.close < hi * 0.97:
            ctx.log(f"移動停損: 高點 {hi:.2f} 回檔至 {ctx.close:.2f}")
            ctx.state['hi'] = 0.0
            return ctx.close_position()
        if ctx.cross_down(fast, slow):
            ctx.state['hi'] = 0.0
            return ctx.close_position()
    return None

# 其他可用: ctx.open/high/low/volume/time, ctx.entry_price, ctx.bars_in_position,
#           ctx.atr(14), ctx.vwap(), ctx.roc(10), ctx.stddev(20), ctx.log('訊息')
# ctx.state 是 dict,跨K棒保存 (實盤/回測皆可用),可自由讀寫。
'''


# ---------------------------------------------------------------------------
# 【ADR-045】自訂策略程式碼「靜態」檢查 — 絕不 exec 使用者程式碼
# ---------------------------------------------------------------------------
# 背景:舊版 GUI 的 _validate_custom 用 exec() 執行整份使用者程式碼來檢查
# 有沒有 on_bar,這有兩個嚴重問題:
#   1. 與安全承諾矛盾 —— 警語說「策略在獨立子行程執行」,驗證卻在主行程
#      執行使用者模組級程式碼;若頂層有 while True 會直接凍死整個 GUI。
#   2. 使用者貼「獨立腳本」(含 if __name__ == "__main__" / import schedule)
#      時,exec 的命名空間沒有 __name__ → NameError,錯誤訊息難以理解。
# 改為 AST 靜態分析:compile 檢語法 + 掃描頂層節點,零執行、可離線測試。

# 頂層出現這些呼叫,幾乎可以斷定使用者貼的是「獨立腳本」而不是 on_bar 策略
_SCRIPT_HINT_CALLS = ('run_pending', 'sleep', 'login', 'login_api', 'schedule')


def validate_source(source_code):
    """
    靜態檢查自訂策略程式碼。回傳 (ok, msg):
      ok=True  → msg 為空字串。
      ok=False → msg 為給使用者看的中文說明 (語法錯誤位置 / 缺 on_bar /
                 偵測到獨立腳本模式的教學提示)。
    只用 compile+ast 分析,絕不執行使用者程式碼。
    """
    import ast as _ast
    src = (source_code or '').strip()
    if not src:
        return False, "程式碼不可空白,至少要定義 on_bar(ctx) 函式。"
    try:
        tree = _ast.parse(src)
    except SyntaxError as e:
        line = getattr(e, 'lineno', '?')
        return False, f"程式碼語法錯誤 (第 {line} 行): {e.msg}"

    has_on_bar = False
    script_hints = []
    for node in tree.body:  # 只看「模組頂層」節點
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name == 'on_bar':
            args = node.args
            n_args = len(args.args) + len(args.posonlyargs)
            if n_args < 1 and not args.vararg:
                return False, "on_bar 必須接受一個參數 (建議寫成 def on_bar(ctx):)。"
            has_on_bar = True
        elif isinstance(node, _ast.While):
            script_hints.append("頂層 while 迴圈")
        elif isinstance(node, _ast.If):
            t = node.test
            if (isinstance(t, _ast.Compare) and isinstance(t.left, _ast.Name)
                    and t.left.id == '__name__'):
                script_hints.append('if __name__ == "__main__" 區塊')
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, _ast.Import) else [node.module or '']
            if any(n and n.split('.')[0] in ('schedule', 'shioaji') for n in names):
                script_hints.append(f"import {names[0]}")
        elif isinstance(node, _ast.Expr) and isinstance(node.value, _ast.Call):
            fn = node.value.func
            name = getattr(fn, 'attr', None) or getattr(fn, 'id', '')
            if name in _SCRIPT_HINT_CALLS:
                script_hints.append(f"頂層呼叫 {name}(...)")

    if not has_on_bar:
        msg = "程式碼必須在「最外層」定義 on_bar(ctx) 函式,系統每根K棒收盤會自動呼叫它。"
        if script_hints:
            msg += ("\n偵測到獨立腳本的寫法 (" + "、".join(script_hints[:3]) + ")。"
                    "\n本系統不需要自己寫登入/排程/迴圈 —— 抓K線、排程觸發、下單防護都由系統代勞;"
                    "\n請只保留策略邏輯,包成 def on_bar(ctx): ... 並回傳 ctx.buy()/ctx.sell()/"
                    "ctx.close_position()/ctx.hold()。可參考編輯器預設的範例程式。")
        return False, msg
    if script_hints:
        return False, ("已找到 on_bar,但程式碼頂層還有獨立腳本的成分 ("
                       + "、".join(script_hints[:3]) +
                       "),這些在系統內不會被執行、還可能造成子行程卡死。"
                       "請移除頂層的登入/排程/迴圈,只保留 on_bar 與它需要的輔助函式。")
    return True, ""
