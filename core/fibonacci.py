# -*- coding: utf-8 -*-
"""
core/fibonacci.py — 黃金切割律 / 費波南希回撤 (ADR-133)

【這是什麼】用一段行情的「擺盪高點 ↔ 擺盪低點」把區間切成幾個比例位置,
那些位置常常成為回檔的支撐或反彈的壓力。比例來自費波南希數列衍生的
0.236 / 0.382 / 0.5 / 0.618 / 0.786,其中 **0.618 就是黃金比例**,
台股習慣稱「黃金切割律」。

【比例的出處與台股慣例】(依據公開資料整理,見 ADR-133 的來源清單)
- 主要回撤:0.236 / 0.382 / 0.5 / 0.618 / 0.786。0.5 嚴格說不是費波南希比例,
  但實務上一律列入 (半分位在市場上同樣有效),各家軟體都這樣做。
- **次級分割律 (台股/波浪理論常用)**:0.191 (0 與 0.382 的中間值) 與
  0.809 (0.618 與 1 的中間值),在判斷波浪第 2/4 波時常被引用。
  台股的說法是「弱勢反彈 0.191 / 0.382,強勢反彈 0.809」。
- 延伸:1.272 / 1.382 / 1.618 / 2.0 / 2.618,用來估「跌破/突破起點之後」的目標。

【比例的定義:百分比是「已經回撤掉多少」】
- 上升段 (低 → 高):0% 在**高點**(完全沒回撤),100% 在**低點**(全部回撤掉)。
  某比例 r 的價位 = high - (high - low) * r
- 下降段 (高 → 低):0% 在**低點**,100% 在**高點**。
  某比例 r 的價位 = low + (high - low) * r

這個定義是各家軟體的共同慣例,兩個方向都用同一條算式,只差起算端 ——
所以 r > 1 自然就落在「起點的另一側」,正好是延伸目標的意義。

【設計原則】純函式、零 tkinter / 零 shioaji 依賴 (鐵則 11),可離線測試。
"""

# 主要回撤比率 (0 與 1 是端點,一起畫出來才看得懂區間)
DEFAULT_LEVELS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)

# 次級分割律:0~0.382 的中間值 0.191、0.618~1 的中間值 0.809 (台股常用)
SECONDARY_LEVELS = (0.191, 0.809)

# 延伸 (跌破/突破起點之後的目標價)
DEFAULT_EXT_LEVELS = (1.272, 1.382, 1.618, 2.0, 2.618)

# 全部可選的比率 (UI 的勾選清單依這個順序排)
ALL_LEVELS = tuple(sorted(set(DEFAULT_LEVELS) | set(SECONDARY_LEVELS) | set(DEFAULT_EXT_LEVELS)))

TREND_UP = '上升段'
TREND_DOWN = '下降段'

DEFAULT_LOOKBACK = 120
MIN_LOOKBACK = 20
MAX_LOOKBACK = 5000

# 0.618 與 0.382 是實務上最常看的兩條,畫粗一點讓它們一眼認得出來。
KEY_RATIOS = (0.382, 0.618)


def ratio_label(r):
    """比率 → 顯示文字。整數比例不要拖一串 0。"""
    try:
        f = float(r)
    except (TypeError, ValueError):
        return ''
    s = f"{f:.3f}".rstrip('0').rstrip('.')
    return f"{s}"


def is_key_ratio(r):
    try:
        return any(abs(float(r) - k) < 1e-9 for k in KEY_RATIOS)
    except (TypeError, ValueError):
        return False


def normalize_ratios(raw):
    """把設定檔讀到的比率清單整理成乾淨、排序、去重的 tuple。

    壞資料一律略過而不是拋例外 —— 主圖不可以因為設定檔壞掉就畫不出來
    (同 regime_panel.normalize 的哲學)。整份都不能用時回預設值。
    """
    out = []
    if isinstance(raw, (list, tuple, set)):
        for v in raw:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if 0.0 <= f <= 5.0 and not any(abs(f - o) < 1e-9 for o in out):
                out.append(f)
    if not out:
        return tuple(DEFAULT_LEVELS)
    return tuple(sorted(out))


def find_swing(df, lookback=DEFAULT_LOOKBACK):
    """在最近 lookback 根 K 棒裡找擺盪高低點,並判斷這一段是上升還是下降。

    回傳 dict {'high','low','high_pos','low_pos','trend','bars'};資料不足回 None。

    **方向的判斷方式**:高點與低點誰**比較晚出現**,就代表最近這一段是往那個
    方向走 —— 低點在前、高點在後 = 上升段 (回撤要往下量);反過來是下降段。
    這是各家「自動費波」的通用作法,也是唯一不需要使用者手動拉線的判準。
    """
    if df is None:
        return None
    try:
        n = int(lookback)
    except (TypeError, ValueError):
        n = DEFAULT_LOOKBACK
    n = max(MIN_LOOKBACK, min(MAX_LOOKBACK, n))
    if len(df) < 2:
        return None
    if 'High' not in df.columns or 'Low' not in df.columns:
        return None
    seg = df.iloc[-n:] if len(df) > n else df
    highs = seg['High'].dropna()
    lows = seg['Low'].dropna()
    if highs.empty or lows.empty:
        return None
    try:
        hi = float(highs.max())
        lo = float(lows.min())
    except (TypeError, ValueError):
        return None
    if not (hi > lo):
        return None                     # 完全沒有波動 → 沒有可切割的區間
    # 用「位置」而不是時間戳比較,避免 index 型別不一致 (診斷用整數 index)
    hi_pos = int(seg.index.get_indexer([highs.idxmax()])[0]) if hasattr(seg.index, 'get_indexer') \
        else list(seg.index).index(highs.idxmax())
    lo_pos = int(seg.index.get_indexer([lows.idxmin()])[0]) if hasattr(seg.index, 'get_indexer') \
        else list(seg.index).index(lows.idxmin())
    trend = TREND_UP if hi_pos > lo_pos else TREND_DOWN
    return {'high': hi, 'low': lo, 'high_pos': hi_pos, 'low_pos': lo_pos,
            'trend': trend, 'bars': len(seg)}


def level_price(high, low, trend, ratio):
    """某個比率對應的價位。百分比的意義是「已經回撤掉多少」(見模組說明)。"""
    span = float(high) - float(low)
    r = float(ratio)
    if trend == TREND_DOWN:
        return float(low) + span * r
    return float(high) - span * r


def levels(swing, ratios=DEFAULT_LEVELS):
    """把 swing 與比率清單換算成一組可以直接畫的水平線。

    回傳 list[dict]:{'ratio','price','label','key','kind'}
    kind:'retrace' (0~1 之間) / 'endpoint' (0 或 1) / 'extend' (>1)
    """
    if not swing:
        return []
    hi, lo, trend = swing['high'], swing['low'], swing['trend']
    out = []
    for r in normalize_ratios(ratios):
        price = level_price(hi, lo, trend, r)
        if r <= 1e-9 or abs(r - 1.0) < 1e-9:
            kind = 'endpoint'
        elif r > 1.0:
            kind = 'extend'
        else:
            kind = 'retrace'
        out.append({'ratio': r, 'price': price, 'label': ratio_label(r),
                    'key': is_key_ratio(r), 'kind': kind})
    return out


def compute(df, lookback=DEFAULT_LOOKBACK, ratios=DEFAULT_LEVELS):
    """一次算完:找擺盪點 + 換算所有比例線。資料不足回 None。"""
    swing = find_swing(df, lookback)
    if not swing:
        return None
    return {'swing': swing, 'levels': levels(swing, ratios)}


def summarize(result):
    """一行中文摘要,給系統日誌 / 狀態列用。"""
    if not result or not result.get('swing'):
        return "資料不足,無法計算黃金切割"
    sw = result['swing']
    key = [lv for lv in result['levels'] if lv['key']]
    parts = " / ".join(f"{lv['label']}={lv['price']:.2f}" for lv in key)
    return (f"{sw['trend']} 高 {sw['high']:.2f} / 低 {sw['low']:.2f}"
            + (f"|關鍵 {parts}" if parts else ""))
