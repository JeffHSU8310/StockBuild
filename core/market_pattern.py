# -*- coding: utf-8 -*-
"""
core/market_pattern.py — 加權指數盤勢/型態偵測 (純邏輯層,提醒用,不下單)

【新ADR 盤勢型態提醒】使用者要求「終極波段策略」除了既有的進出場邏輯外,
再加一個獨立的盤勢/型態偵測功能:抓一段區間(預設近60天)的收盤最高/最低,
接近邊界就提醒;並偵測市場上常見的技術型態 (M頭/W底/頭肩頂/頭肩底/
破底翻/假突破/島型反轉/區間突破/區間跌破/N字形/三角形收斂/楔形)。

設計原則 (比照 core/chukuangren_band.py 與 ADR-009):
1. 零 tkinter / 零 shioaji,純函式,可離線單元測試。
2. 只做「提醒」,不產生任何買賣 intent、不影響既有進出場邏輯——這個模組
   完全不知道帳戶/部位/委託是什麼,只吃 OHLC DataFrame、吐型態描述。
3. 每個型態的判斷規則都刻意簡化、透明、可測試,不是精確複刻教科書上的
   幾何定義,也不是用機器學習「像不像」——例如頭肩頂的頸線用兩個波谷
   收盤的平均值,不是真正兩點連線的斜線頸線。這是誠實的取捨:規則越
   複雜越貼近教科書,但也越難用日K資料穩定判斷、越難寫測試驗證,寧可
   規則簡單清楚、使用者知道我在算什麼,也不要看起來很厲害其實是黑箱。
4. 除了「盤勢分類」「三角形/楔形」這種本質是持續一段時間的『狀態』之外,
   其餘型態 (M頭/W底/頭肩頂/頭肩底/破底翻/假突破/島型反轉/區間突破/
   區間跌破/N字形) 都設計成「只在型態confirm的那一天(=傳入df的最後一根)
   才觸發一次」,而不是「過去 lookback 天內只要出現過就一直回報」——
   避免呼叫端 (GUI 層) 每天重複收到同一個舊訊號的通知轟炸。「盤勢分類」
   /「三角形/楔形」這兩種本質是持續狀態,呼叫端 (GUI 層) 自行比對『這次
   跟上次通知的內容是否相同』來決定要不要重新通知 (不在這個模組處理,
   這裡只負責算出『現在』的真實狀態)。

呼叫端使用方式:抓到日K DataFrame (欄位 Open/High/Low/Close,DatetimeIndex,
由新到舊或舊到新皆可,内部一律用 .tail() 取最新的在最後),呼叫
evaluate_all(df, params) 拿到這次 (以最後一根為「今天」) 觸發的訊號 list。
"""
import numpy as np

DEFAULT_PARAMS = {
    'lookback': 60,          # 【使用者需求】盤勢/區間偵測的天數,預設60天
    'near_pct': 3.0,         # 收盤跟區間高/低點的距離 <= 此百分比,視為「接近」
    'swing_window': 3,       # 波段高/低點偵測:左右各幾根K棒內最高/最低才算波段點
    'tolerance_pct': 3.0,    # M頭/W底/N字形:兩個高點(或低點)高度誤差容許%
    'head_margin_pct': 1.0,  # 頭肩頂/底:頭必須比兩肩至少高(低)出的%
    'recover_bars': 5,       # 破底翻/假突破:往前抓幾天判斷「最近是否跌破/突破過」
    'gap_pct': 0.5,          # 島型反轉:跳空缺口至少要有的%幅度
    'max_island_bars': 5,    # 島型反轉:兩個反向缺口之間最多間隔幾天,超過不算同一個島
    'flat_pct': 0.05,        # 三角形/楔形:波段高低點連線斜率,相對價位小於此%視為「走平」
    'enabled_patterns': (
        'regime', 'double_top', 'double_bottom', 'head_shoulders_top',
        'head_shoulders_bottom', 'undercut_reverse', 'false_breakout',
        'island_reversal', 'range_breakout', 'range_breakdown',
        'n_pattern', 'triangle_wedge',
    ),
}

# 【使用者需求 2】完整型態清單 (UI 顯示用,讓使用者知道會判斷哪些型態)
PATTERN_CATALOG = [
    {'id': 'regime', 'category': '盤勢', 'label': '盤勢分類 (區間整理/上升趨勢/下降趨勢)',
     'desc': '抓近N天收盤最高/最低,判斷目前是區間整理還是有方向的趨勢;收盤接近區間邊界時額外提醒。'},
    {'id': 'double_top', 'category': '反轉', 'label': 'M頭 (雙重頂)',
     'desc': '兩個高度相近的波段高點夾一個波段低點(頸線),第二個高點後收盤跌破頸線才確認,偏空。'},
    {'id': 'double_bottom', 'category': '反轉', 'label': 'W底 (雙重底)',
     'desc': 'M頭的鏡像:兩個相近波段低點夾一個波段高點(頸線),突破頸線才確認,偏多。'},
    {'id': 'head_shoulders_top', 'category': '反轉', 'label': '頭肩頂',
     'desc': '三個波段高點,中間(頭)明顯高於左右兩肩且兩肩高度相近,跌破頸線(兩波谷均價)才確認,偏空。'},
    {'id': 'head_shoulders_bottom', 'category': '反轉', 'label': '頭肩底',
     'desc': '頭肩頂的鏡像,突破頸線才確認,偏多。'},
    {'id': 'undercut_reverse', 'category': '反轉', 'label': '破底翻',
     'desc': '近期收盤/盤中跌破先前低點(假跌破),當天收盤又收復到低點之上,偏多。'},
    {'id': 'false_breakout', 'category': '反轉', 'label': '假突破',
     'desc': '破底翻的鏡像:突破先前高點後又收回高點之下,偏空。'},
    {'id': 'island_reversal', 'category': '反轉', 'label': '島型反轉',
     'desc': '一段整理被前後兩個同方向跳空缺口夾住,形成孤立的「島」,回補缺口當天確認,依方向偏多/偏空。'},
    {'id': 'range_breakout', 'category': '突破', 'label': '區間突破',
     'desc': '收盤站上先前(不含當天)區間最高收盤價,偏多。'},
    {'id': 'range_breakdown', 'category': '突破', 'label': '區間跌破',
     'desc': '區間突破的鏡像,收盤跌破先前區間最低收盤價,偏空。'},
    {'id': 'n_pattern', 'category': '趨勢延續', 'label': 'N字形 (上升/下降)',
     'desc': '上升N字:低點墊高後再突破前波高點,呈現「下切後創高」的N形結構,偏多;下降N字對稱,偏空。'},
    {'id': 'triangle_wedge', 'category': '整理', 'label': '三角形收斂 / 楔形',
     'desc': '用波段高/低點的連線斜率粗分類:高降低升=對稱三角形,高平低升=上升三角形,高降低平=下降三角形,'
             '高低同向上升=上升楔形(常見於漲勢末端,偏空),同向下降=下降楔形(常見於跌勢末端,偏多)。'},
]


def _ohlc_arrays(df):
    return (df['Open'].astype(float).to_numpy(), df['High'].astype(float).to_numpy(),
            df['Low'].astype(float).to_numpy(), df['Close'].astype(float).to_numpy())


def _find_swings(highs, lows, window):
    """波段高/低點偵測:第 i 根K棒左右各 window 根範圍內同時是最高/最低,才算
    波段高/低點。回傳 [(idx, 'high'/'low', price), ...] 依 idx 排序、且清成
    嚴格交替 high/low/high/low... (連續同型態只留最極端那個,濾掉雜訊)。"""
    n = len(highs)
    raw = []
    for i in range(window, n - window):
        seg_h = highs[i - window:i + window + 1]
        seg_l = lows[i - window:i + window + 1]
        if highs[i] >= seg_h.max():
            raw.append((i, 'high', float(highs[i])))
        if lows[i] <= seg_l.min():
            raw.append((i, 'low', float(lows[i])))
    raw.sort(key=lambda t: t[0])
    cleaned = []
    for s in raw:
        if cleaned and cleaned[-1][1] == s[1]:
            if (s[1] == 'high' and s[2] > cleaned[-1][2]) or (s[1] == 'low' and s[2] < cleaned[-1][2]):
                cleaned[-1] = s
        else:
            cleaned.append(s)
    return cleaned


def find_swings(df, window=3):
    """對外版本:回傳 [{'idx','date','type','price'}, ...]。"""
    _, highs, lows, _ = _ohlc_arrays(df)
    dates = [str(x) for x in df.index]
    return [{'idx': i, 'date': dates[i], 'type': t, 'price': p}
            for i, t, p in _find_swings(highs, lows, window)]


def classify_regime(df, lookback=60, near_pct=3.0):
    """盤勢分類:抓近 lookback 天收盤的最高/最低,判斷目前是「區間整理」
    「上升趨勢」還是「下降趨勢」,以及今天收盤是否接近區間邊界。

    趨勢判斷用粗略但透明的規則:把視窗切成前後兩半,比較兩半平均收盤的
    漲跌幅,超過 near_pct 的 2 倍才算有方向,否則視為區間整理——這不是
    嚴謹的統計檢定,只是一個誠實、可預期、可測試的經驗法則。"""
    if df is None or len(df) < 10:
        return None
    sub = df.tail(int(lookback))
    closes = sub['Close'].astype(float)
    if len(closes) < 10:
        return None
    range_high = float(closes.max())
    range_low = float(closes.min())
    today_close = float(closes.iloc[-1])
    span = range_high - range_low
    near_high = span > 0 and (range_high - today_close) / span * 100.0 <= near_pct
    near_low = span > 0 and (today_close - range_low) / span * 100.0 <= near_pct
    half = len(closes) // 2
    first_avg = float(closes.iloc[:half].mean())
    second_avg = float(closes.iloc[half:].mean())
    chg_pct = (second_avg - first_avg) / first_avg * 100.0 if first_avg else 0.0
    if chg_pct > near_pct * 2:
        regime = '上升趨勢'
    elif chg_pct < -near_pct * 2:
        regime = '下降趨勢'
    else:
        regime = '區間整理'
    return {
        'regime': regime, 'range_high': range_high, 'range_low': range_low,
        'today_close': today_close, 'near_high': near_high, 'near_low': near_low,
        'lookback': int(lookback), 'near_pct': float(near_pct),
    }


def detect_double_top(df, swing_window=3, tolerance_pct=3.0, lookback=60):
    """M頭(雙重頂):今天(最後一根)收盤第一次跌破頸線才觸發。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    if n < swing_window * 2 + 3:
        return None
    _, highs, lows, closes = _ohlc_arrays(sub)
    swings = _find_swings(highs, lows, swing_window)
    hi = [s for s in swings if s[1] == 'high']
    if len(hi) < 2:
        return None
    i1, _, p1 = hi[-2]
    i2, _, p2 = hi[-1]
    if p1 <= 0 or i2 <= i1 + 1 or abs(p2 - p1) / p1 * 100.0 > tolerance_pct:
        return None
    neckline = float(lows[i1 + 1:i2].min())
    if closes[-1] >= neckline:
        return None
    if np.any(closes[i2 + 1:-1] < neckline):
        return None  # 更早就已經跌破過,今天不是第一次
    return {'pattern': 'double_top', 'label': 'M頭(雙重頂)', 'bias': '偏空',
            'peak1': p1, 'peak2': p2, 'neckline': neckline, 'today_close': float(closes[-1])}


def detect_double_bottom(df, swing_window=3, tolerance_pct=3.0, lookback=60):
    """W底(雙重底):M頭的鏡像。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    if n < swing_window * 2 + 3:
        return None
    _, highs, lows, closes = _ohlc_arrays(sub)
    swings = _find_swings(highs, lows, swing_window)
    lo = [s for s in swings if s[1] == 'low']
    if len(lo) < 2:
        return None
    i1, _, p1 = lo[-2]
    i2, _, p2 = lo[-1]
    if p1 <= 0 or i2 <= i1 + 1 or abs(p2 - p1) / p1 * 100.0 > tolerance_pct:
        return None
    neckline = float(highs[i1 + 1:i2].max())
    if closes[-1] <= neckline:
        return None
    if np.any(closes[i2 + 1:-1] > neckline):
        return None
    return {'pattern': 'double_bottom', 'label': 'W底(雙重底)', 'bias': '偏多',
            'trough1': p1, 'trough2': p2, 'neckline': neckline, 'today_close': float(closes[-1])}


def detect_head_shoulders_top(df, swing_window=3, tolerance_pct=5.0, head_margin_pct=1.0, lookback=90):
    """頭肩頂:三個波段高點,中間(頭)明顯高於左右兩肩,兩肩高度相近;
    今天收盤第一次跌破頸線(兩波谷收盤均值)才觸發。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    if n < swing_window * 2 + 5:
        return None
    _, highs, lows, closes = _ohlc_arrays(sub)
    swings = _find_swings(highs, lows, swing_window)
    hi = [s for s in swings if s[1] == 'high']
    if len(hi) < 3:
        return None
    (i1, _, ls), (i2, _, head), (i3, _, rs) = hi[-3], hi[-2], hi[-1]
    if not (i1 < i2 < i3):
        return None
    if ls <= 0 or rs <= 0:
        return None
    if head <= ls * (1 + head_margin_pct / 100.0) or head <= rs * (1 + head_margin_pct / 100.0):
        return None
    if abs(rs - ls) / ls * 100.0 > tolerance_pct:
        return None
    lo_left = [s for s in swings if s[1] == 'low' and i1 < s[0] < i2]
    lo_right = [s for s in swings if s[1] == 'low' and i2 < s[0] < i3]
    if not lo_left or not lo_right:
        return None
    neckline = (lo_left[-1][2] + lo_right[0][2]) / 2.0
    if closes[-1] >= neckline:
        return None
    if np.any(closes[i3 + 1:-1] < neckline):
        return None
    return {'pattern': 'head_shoulders_top', 'label': '頭肩頂', 'bias': '偏空',
            'left_shoulder': ls, 'head': head, 'right_shoulder': rs,
            'neckline': neckline, 'today_close': float(closes[-1])}


def detect_head_shoulders_bottom(df, swing_window=3, tolerance_pct=5.0, head_margin_pct=1.0, lookback=90):
    """頭肩底:頭肩頂的鏡像。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    if n < swing_window * 2 + 5:
        return None
    _, highs, lows, closes = _ohlc_arrays(sub)
    swings = _find_swings(highs, lows, swing_window)
    lo = [s for s in swings if s[1] == 'low']
    if len(lo) < 3:
        return None
    (i1, _, ls), (i2, _, head), (i3, _, rs) = lo[-3], lo[-2], lo[-1]
    if not (i1 < i2 < i3):
        return None
    if head <= 0:
        return None
    if head >= ls * (1 - head_margin_pct / 100.0) or head >= rs * (1 - head_margin_pct / 100.0):
        return None
    if ls <= 0 or abs(rs - ls) / ls * 100.0 > tolerance_pct:
        return None
    hi_left = [s for s in swings if s[1] == 'high' and i1 < s[0] < i2]
    hi_right = [s for s in swings if s[1] == 'high' and i2 < s[0] < i3]
    if not hi_left or not hi_right:
        return None
    neckline = (hi_left[-1][2] + hi_right[0][2]) / 2.0
    if closes[-1] <= neckline:
        return None
    if np.any(closes[i3 + 1:-1] > neckline):
        return None
    return {'pattern': 'head_shoulders_bottom', 'label': '頭肩底', 'bias': '偏多',
            'left_shoulder': ls, 'head': head, 'right_shoulder': rs,
            'neckline': neckline, 'today_close': float(closes[-1])}


def detect_undercut_reverse(df, lookback=60, recover_bars=5):
    """破底翻:近 recover_bars 天(含今天)內曾經跌破先前支撐,今天收盤第一次
    收復到支撐之上。支撐 = 這段近期窗口之前的最低點。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    recover_bars = int(recover_bars)
    if n < recover_bars + 5:
        return None
    ref = sub.iloc[:-recover_bars]
    recent = sub.iloc[-recover_bars:]
    support = float(ref['Low'].min())
    lows = recent['Low'].astype(float).to_numpy()
    closes = recent['Close'].astype(float).to_numpy()
    today_close = closes[-1]
    if today_close <= support:
        return None
    if len(closes) > 1 and closes[-2] > support:
        return None  # 昨天就已經收復,今天不是第一次
    if not np.any(lows < support):
        return None  # 這段期間根本沒跌破過
    return {'pattern': 'undercut_reverse', 'label': '破底翻', 'bias': '偏多',
            'support': support, 'today_close': float(today_close)}


def detect_false_breakout(df, lookback=60, recover_bars=5):
    """假突破:破底翻的鏡像。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    recover_bars = int(recover_bars)
    if n < recover_bars + 5:
        return None
    ref = sub.iloc[:-recover_bars]
    recent = sub.iloc[-recover_bars:]
    resistance = float(ref['High'].max())
    highs = recent['High'].astype(float).to_numpy()
    closes = recent['Close'].astype(float).to_numpy()
    today_close = closes[-1]
    if today_close >= resistance:
        return None
    if len(closes) > 1 and closes[-2] < resistance:
        return None
    if not np.any(highs > resistance):
        return None
    return {'pattern': 'false_breakout', 'label': '假突破', 'bias': '偏空',
            'resistance': resistance, 'today_close': float(today_close)}


def detect_island_reversal(df, lookback=30, gap_pct=0.5, max_island_bars=5):
    """島型反轉:今天向下跳空(今高 < 昨低),往前 max_island_bars 天內若能
    找到一次向上跳空(那天低 > 前一天高),中間視為一個孤立的島 → 反轉向下;
    今天向上跳空 + 往前找向下跳空,則反轉向上。只在今天跳空的當下觸發。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    max_island_bars = int(max_island_bars)
    if n < max_island_bars + 3:
        return None
    _, highs, lows, _ = _ohlc_arrays(sub)
    dates = [str(x) for x in sub.index]
    gap_ratio = float(gap_pct) / 100.0

    gap_down_today = highs[-1] < lows[-2] * (1 - gap_ratio)
    gap_up_today = lows[-1] > highs[-2] * (1 + gap_ratio)

    if gap_down_today:
        for k in range(2, min(max_island_bars, n - 2) + 1):
            idx = n - 1 - k
            if idx < 1:
                break
            if lows[idx] > highs[idx - 1] * (1 + gap_ratio):
                return {'pattern': 'island_reversal_down', 'label': '島型反轉(向下)', 'bias': '偏空',
                        'up_gap_date': dates[idx], 'down_gap_date': dates[-1]}
    if gap_up_today:
        for k in range(2, min(max_island_bars, n - 2) + 1):
            idx = n - 1 - k
            if idx < 1:
                break
            if highs[idx] < lows[idx - 1] * (1 - gap_ratio):
                return {'pattern': 'island_reversal_up', 'label': '島型反轉(向上)', 'bias': '偏多',
                        'down_gap_date': dates[idx], 'up_gap_date': dates[-1]}
    return None


def detect_range_breakout(df, lookback=60):
    """區間突破:今天收盤第一次站上『不含今天、也不含昨天』的近 lookback
    天最高收盤——參考區間一定要排除最近兩天,不然「今天突破」隔天還會
    因為參考區間已經把突破那天算進去、高點跟著墊高,而被誤判成「還沒
    突破」或「重複突破」;排除最近兩天才能正確判斷「今天是不是第一次」。"""
    sub = df.tail(int(lookback) + 2)
    if len(sub) < int(lookback) + 2:
        return None
    ref = sub.iloc[:-2]
    closes = sub['Close'].astype(float).to_numpy()
    range_high = float(ref['Close'].astype(float).max())
    today_close = closes[-1]
    prev_close = closes[-2]
    if today_close <= range_high:
        return None
    if prev_close > range_high:
        return None  # 昨天就已經突破過,今天不是第一次 (避免重複通知)
    return {'pattern': 'range_breakout', 'label': '區間突破', 'bias': '偏多',
            'range_high': range_high, 'today_close': float(today_close)}


def detect_range_breakdown(df, lookback=60):
    """區間跌破:區間突破的鏡像。"""
    sub = df.tail(int(lookback) + 2)
    if len(sub) < int(lookback) + 2:
        return None
    ref = sub.iloc[:-2]
    closes = sub['Close'].astype(float).to_numpy()
    range_low = float(ref['Close'].astype(float).min())
    today_close = closes[-1]
    prev_close = closes[-2]
    if today_close >= range_low:
        return None
    if prev_close < range_low:
        return None  # 昨天就已經跌破過,今天不是第一次
    return {'pattern': 'range_breakdown', 'label': '區間跌破', 'bias': '偏空',
            'range_low': range_low, 'today_close': float(today_close)}


def detect_n_pattern_up(df, swing_window=3, lookback=90):
    """N字上升:低點墊高(低2 > 低1),今天收盤第一次突破夾在兩低點之間的
    波段高點A,走出「下切後創高」的N形結構。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    if n < swing_window * 2 + 3:
        return None
    _, highs, lows, closes = _ohlc_arrays(sub)
    swings = _find_swings(highs, lows, swing_window)
    lo = [s for s in swings if s[1] == 'low']
    hi = [s for s in swings if s[1] == 'high']
    if len(lo) < 2 or not hi:
        return None
    ilo1, _, plo1 = lo[-2]
    ilo2, _, plo2 = lo[-1]
    if ilo2 <= ilo1:
        return None
    between = [s for s in hi if ilo1 < s[0] < ilo2]
    if not between:
        return None
    A = between[-1][2]
    if plo2 <= plo1:
        return None
    if closes[-1] <= A:
        return None
    if np.any(closes[ilo2 + 1:-1] > A):
        return None
    return {'pattern': 'n_up', 'label': 'N字上升', 'bias': '偏多',
            'low1': plo1, 'high_a': A, 'low2': plo2, 'today_close': float(closes[-1])}


def detect_n_pattern_down(df, swing_window=3, lookback=90):
    """N字下降:N字上升的鏡像。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    if n < swing_window * 2 + 3:
        return None
    _, highs, lows, closes = _ohlc_arrays(sub)
    swings = _find_swings(highs, lows, swing_window)
    lo = [s for s in swings if s[1] == 'low']
    hi = [s for s in swings if s[1] == 'high']
    if len(hi) < 2 or not lo:
        return None
    ihi1, _, phi1 = hi[-2]
    ihi2, _, phi2 = hi[-1]
    if ihi2 <= ihi1:
        return None
    between = [s for s in lo if ihi1 < s[0] < ihi2]
    if not between:
        return None
    B = between[-1][2]
    if phi2 >= phi1:
        return None
    if closes[-1] >= B:
        return None
    if np.any(closes[ihi2 + 1:-1] < B):
        return None
    return {'pattern': 'n_down', 'label': 'N字下降', 'bias': '偏空',
            'high1': phi1, 'low_b': B, 'high2': phi2, 'today_close': float(closes[-1])}


def _slope(points):
    """points: [(idx, price), ...]。回傳簡單線性回歸斜率 (price/根),
    點數不足或 idx 全相同回傳 None。"""
    if len(points) < 2:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    if xs.std() == 0:
        return None
    slope, _ = np.polyfit(xs, ys, 1)
    return float(slope)


def detect_triangle_wedge(df, swing_window=3, lookback=90, flat_pct=0.05):
    """三角形收斂/楔形:取最近各3個波段高點/低點,分別做線性回歸抓斜率
    方向 (走平的門檻:斜率絕對值相對價位水準小於 flat_pct%)。
    高降低升→對稱三角形;高平低升→上升三角形;高降低平→下降三角形;
    高低同向上升→上升楔形;高低同向下降→下降楔形。這是簡化版分類
    (只看斜率方向粗分,不是精確的收斂速度/框線判斷),供參考用。"""
    sub = df.tail(int(lookback))
    n = len(sub)
    if n < swing_window * 2 + 6:
        return None
    _, highs, lows, _ = _ohlc_arrays(sub)
    swings = _find_swings(highs, lows, swing_window)
    hi = [(s[0], s[2]) for s in swings if s[1] == 'high'][-4:]
    lo = [(s[0], s[2]) for s in swings if s[1] == 'low'][-4:]
    if len(hi) < 3 or len(lo) < 3:
        return None
    sh = _slope(hi)
    sl = _slope(lo)
    if sh is None or sl is None:
        return None
    level = float(np.mean([p for _, p in hi] + [p for _, p in lo]))
    if level <= 0:
        return None
    thr = level * float(flat_pct) / 100.0
    hi_dir = 'flat' if abs(sh) <= thr else ('up' if sh > 0 else 'down')
    lo_dir = 'flat' if abs(sl) <= thr else ('up' if sl > 0 else 'down')
    if hi_dir == 'down' and lo_dir == 'up':
        return {'pattern': 'triangle_symmetrical', 'label': '對稱三角形', 'bias': '待突破方向', 'slope_high': sh, 'slope_low': sl}
    if hi_dir == 'flat' and lo_dir == 'up':
        return {'pattern': 'triangle_ascending', 'label': '上升三角形', 'bias': '偏多', 'slope_high': sh, 'slope_low': sl}
    if hi_dir == 'down' and lo_dir == 'flat':
        return {'pattern': 'triangle_descending', 'label': '下降三角形', 'bias': '偏空', 'slope_high': sh, 'slope_low': sl}
    if hi_dir == 'up' and lo_dir == 'up':
        return {'pattern': 'wedge_rising', 'label': '上升楔形', 'bias': '偏空(常見於漲勢末端)', 'slope_high': sh, 'slope_low': sl}
    if hi_dir == 'down' and lo_dir == 'down':
        return {'pattern': 'wedge_falling', 'label': '下降楔形', 'bias': '偏多(常見於跌勢末端)', 'slope_high': sh, 'slope_low': sl}
    return None


def evaluate_all(df, params=None):
    """一次跑完所有啟用的判斷,回傳這次 (以 df 最後一根為「今天」) 觸發的
    訊號 list (可能是空 list)。params 未提供的欄位一律用 DEFAULT_PARAMS。
    df 資料不足 (常見於剛登入、歷史還沒補齊) 時安靜回傳空 list,不拋例外。"""
    if df is None or len(df) < 10:
        return []
    p = dict(DEFAULT_PARAMS)
    if params:
        for k, v in params.items():
            if v is not None:
                p[k] = v
    enabled = set(p.get('enabled_patterns') or DEFAULT_PARAMS['enabled_patterns'])
    signals = []
    try:
        if 'regime' in enabled:
            regime = classify_regime(df, lookback=p['lookback'], near_pct=p['near_pct'])
            if regime:
                signals.append({'pattern': 'regime', 'label': f"目前盤勢:{regime['regime']}",
                                'bias': '', **regime})
                if regime['near_high']:
                    signals.append({'pattern': 'near_range_high',
                                    'label': f"收盤接近{regime['lookback']}天區間高點", 'bias': '留意壓力', **regime})
                if regime['near_low']:
                    signals.append({'pattern': 'near_range_low',
                                    'label': f"收盤接近{regime['lookback']}天區間低點", 'bias': '留意支撐', **regime})
        detectors = {
            'double_top': lambda: detect_double_top(df, p['swing_window'], p['tolerance_pct'], p['lookback']),
            'double_bottom': lambda: detect_double_bottom(df, p['swing_window'], p['tolerance_pct'], p['lookback']),
            'head_shoulders_top': lambda: detect_head_shoulders_top(
                df, p['swing_window'], max(p['tolerance_pct'], 5.0), p['head_margin_pct'], max(p['lookback'], 90)),
            'head_shoulders_bottom': lambda: detect_head_shoulders_bottom(
                df, p['swing_window'], max(p['tolerance_pct'], 5.0), p['head_margin_pct'], max(p['lookback'], 90)),
            'undercut_reverse': lambda: detect_undercut_reverse(df, p['lookback'], p['recover_bars']),
            'false_breakout': lambda: detect_false_breakout(df, p['lookback'], p['recover_bars']),
            'island_reversal': lambda: detect_island_reversal(df, min(p['lookback'], 30), p['gap_pct'], p['max_island_bars']),
            'range_breakout': lambda: detect_range_breakout(df, p['lookback']),
            'range_breakdown': lambda: detect_range_breakdown(df, p['lookback']),
            'n_pattern': lambda: [detect_n_pattern_up(df, p['swing_window'], max(p['lookback'], 90)),
                                  detect_n_pattern_down(df, p['swing_window'], max(p['lookback'], 90))],
            'triangle_wedge': lambda: detect_triangle_wedge(df, p['swing_window'], max(p['lookback'], 90), p['flat_pct']),
        }
        for pid, fn in detectors.items():
            if pid not in enabled:
                continue
            r = fn()
            if isinstance(r, list):
                signals.extend(x for x in r if x)
            elif r:
                signals.append(r)
    except Exception:
        return signals
    return signals
