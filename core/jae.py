# -*- coding: utf-8 -*-
"""
core/jae.py — JAE 指標 (ADR-134)

【這是使用者自創的指標,定義由使用者給】
    A = RSI 的那一條線
    J = KDJ 的 J 那一條線
    E = 一條長期趨勢線 ← **這條的設計由使用者交給我決定**
    用這三條線的黃金交叉 / 死亡交叉看進出場,三個都可以自行設定參數。

---------------------------------------------------------------------------
【E 這條線我為什麼這樣設計】

要跟 A、J 畫在同一個副圖裡互相交叉,E 就必須滿足三件事:
  1. **跟 A/J 同一個尺度**。A 是 RSI (0~100),J = 3K-2D (大致 -50~150)。
     如果 E 用價格或均線,數值差好幾個數量級,三條線根本不會相交,
     「黃金交叉」就變成沒有意義的字。
  2. **要夠慢**,才配得上「長期趨勢」四個字 —— 快線跟快線交叉只是雜訊。
  3. **要有一個天然的中性基準**,否則「向上/向下」沒有參照點。

同時滿足這三件事的最簡單選擇:**E = RSI 的長期 EMA**(預設 60 期)。
  · 尺度天生跟 A 一樣 (都是 0~100),J 圍著它上下穿越很自然。
  · 60 期 EMA 對 14 期的 A 來說夠慢。
  · **50 是 RSI 公認的多空中性線**,直接拿來當基準,不必自己發明一個。

【趨勢怎麼判斷】**位置為主、斜率為輔**:

    E ≥ 50 + band  → 向上          (明確站在中性線之上)
    E ≤ 50 - band  → 向下
    在 band 之內 (中線附近的模糊區) → 才看斜率:
        斜率 > eps → 向上 / 斜率 < -eps → 向下 / 否則 → 盤整

【為什麼不是「位置與斜率兩個都要成立」】我第一版就是那樣寫的,**而且是錯的**,
被單元測試抓出來:RSI 量的是**動能**不是價格方向,所以一段「等速下跌」會讓
RSI 穩定在某個低點(例如 34),E 跟著收斂成一條水平線 —— **斜率趨近 0**。
兩個條件都要求的話,這種「最典型的持續下跌」反而被判成「盤整」,完全違反直覺。

位置為主就沒有這個問題:E 遠離中線本身就是趨勢的證據;只有在中線附近
(方向真的不明時)才需要斜率來決定偏哪一邊。band 與 eps 都可以調。

【交叉怎麼看】三條線兩兩配對,語意不同,所以分開報:
    J × A  快 × 中 → 短線進出訊號 (最靈敏,也最容易假訊號)
    A × E  中 × 慢 → 中期趨勢轉折 (最重要的一組)
    J × E  快 × 慢 → 極短線相對長期趨勢的位置
「黃金交叉」一律定義為「**比較快的那條由下往上穿過比較慢的那條**」,
死亡交叉反之 —— 三組都照這個定義,不會有的組別反過來。

【設計原則】純函式、零 tkinter / 零 shioaji 依賴 (鐵則 11),可離線測試。
本模組**只產生線與訊號,不下單** —— 要不要接進自動交易是另一筆決定。
"""
import pandas as pd

from core import indicators

# 使用者可調參數 (UI 與驗證共用同一份,避免各處各寫一份不同步)
DEFAULT_PARAMS = {
    'a_period': 14,        # A = RSI 期數
    'j_n': 9,              # J = KDJ 的 N
    'j_m1': 3,             # J = KDJ 的 M1 (K 的平滑)
    'j_m2': 3,             # J = KDJ 的 M2 (D 的平滑)
    'e_period': 60,        # E = RSI 的長期 EMA 期數
    'e_slope_bars': 5,     # E 的斜率看幾根
    'midline': 50.0,       # 中性基準 (RSI 的公認中線)
    'neutral_band': 5.0,   # 中線上下這個範圍內算「方向不明」,才改看斜率
    'slope_eps': 0.05,     # 斜率小於這個值視為持平
}

PARAM_LABELS = {
    'a_period': 'A — RSI 期數',
    'j_n': 'J — KDJ 的 N',
    'j_m1': 'J — KDJ 的 M1 (K)',
    'j_m2': 'J — KDJ 的 M2 (D)',
    'e_period': 'E — 長期趨勢線期數 (RSI 的 EMA)',
    'e_slope_bars': 'E — 斜率判斷看幾根',
    'midline': '中性基準線 (RSI 慣例 50)',
    'neutral_band': '中線模糊帶 (±此範圍內才看斜率)',
    'slope_eps': '斜率視為持平的門檻',
}

# 各參數的合法範圍 (超出就夾回來,壞設定不可以讓圖畫不出來)
PARAM_RANGES = {
    'a_period': (2, 500), 'j_n': (2, 500), 'j_m1': (1, 100), 'j_m2': (1, 100),
    'e_period': (5, 1000), 'e_slope_bars': (1, 200), 'midline': (0.0, 100.0),
    'neutral_band': (0.0, 50.0), 'slope_eps': (0.0, 50.0),
}

COL_A, COL_J, COL_E = 'JAE_A', 'JAE_J', 'JAE_E'

TREND_UP = '向上'
TREND_DOWN = '向下'
TREND_FLAT = '盤整'

CROSS_GOLDEN = '黃金交叉'
CROSS_DEATH = '死亡交叉'

# (快線, 慢線, 這一組的中文說明)。順序 = 報告裡的排列順序。
CROSS_PAIRS = (
    (COL_J, COL_A, 'J×A 短線'),
    (COL_A, COL_E, 'A×E 中期'),
    (COL_J, COL_E, 'J×E 極短線'),
)


def normalize_params(raw):
    """把設定讀到的東西整理成一份值域安全的參數。壞值一律夾回合法範圍。"""
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_PARAMS)
    for key, default in DEFAULT_PARAMS.items():
        if key not in src:
            continue
        lo, hi = PARAM_RANGES[key]
        try:
            v = float(src[key])
        except (TypeError, ValueError):
            continue
        if v != v:                      # NaN
            continue
        v = max(lo, min(hi, v))
        out[key] = v if isinstance(default, float) else int(v)
    return out


def compute(df, params=None):
    """算出 A / J / E 三條線,就地寫進 df 的 JAE_A / JAE_J / JAE_E 欄位。

    回傳 df。資料不足時欄位仍會建立 (值是 NaN),呼叫端用 dropna 自行判斷 ——
    這樣繪圖端不必為「有沒有這個欄位」寫兩套分支。
    """
    p = normalize_params(params)
    df[COL_A] = indicators.rsi(df['Close'], p['a_period'])
    _, _, _, j = indicators.kdj(df, p['j_n'], p['j_m1'], p['j_m2'])
    df[COL_J] = j
    # E 用「A 的長期 EMA」而不是「再算一次長期 RSI」:前者是同一條線的慢速版,
    # 交叉的語意乾淨 (同一個東西的快與慢);後者是另一條獨立的線,兩者交叉
    # 代表的意義說不清楚。
    df[COL_E] = df[COL_A].ewm(span=int(p['e_period']), adjust=False).mean()
    return df


def trend_of(e_series, params=None):
    """依 E 判斷長期趨勢:**位置為主、斜率為輔**(理由見模組說明)。

        E ≥ 中線 + band → 向上
        E ≤ 中線 - band → 向下
        在 band 之內      → 看斜率 (超過 eps 才算有方向,否則盤整)

    回傳 (trend, e_now, slope);算不出來回 (TREND_FLAT, None, None)。
    """
    p = normalize_params(params)
    if e_series is None:
        return TREND_FLAT, None, None
    s = e_series.dropna() if hasattr(e_series, 'dropna') else None
    if s is None or len(s) < 2:
        return TREND_FLAT, None, None
    bars = int(p['e_slope_bars'])
    now = float(s.iloc[-1])
    prev = float(s.iloc[-(bars + 1)]) if len(s) > bars else float(s.iloc[0])
    slope = now - prev
    mid = float(p['midline'])
    band = float(p['neutral_band'])
    eps = float(p['slope_eps'])
    if now >= mid + band:
        return TREND_UP, now, slope
    if now <= mid - band:
        return TREND_DOWN, now, slope
    # 中線附近:位置說不準,才由斜率決定偏哪一邊
    if slope > eps:
        return TREND_UP, now, slope
    if slope < -eps:
        return TREND_DOWN, now, slope
    return TREND_FLAT, now, slope


def _cross_at(fast, slow, i):
    """第 i 根是不是交叉點。回傳 CROSS_GOLDEN / CROSS_DEATH / None。

    定義:前一根 fast <= slow、這一根 fast > slow → 黃金交叉 (快線由下往上穿)。
    用 <= / > 的不對稱寫法,是為了讓「剛好相等後再分開」也只算一次交叉。
    """
    try:
        f0, s0 = float(fast.iloc[i - 1]), float(slow.iloc[i - 1])
        f1, s1 = float(fast.iloc[i]), float(slow.iloc[i])
    except (TypeError, ValueError, IndexError):
        return None
    if any(v != v for v in (f0, s0, f1, s1)):     # NaN
        return None
    if f0 <= s0 and f1 > s1:
        return CROSS_GOLDEN
    if f0 >= s0 and f1 < s1:
        return CROSS_DEATH
    return None


def crosses(df, lookback=1):
    """找出最後 lookback 根 K 棒內發生的所有交叉。

    回傳 list[dict] {'pair','fast','slow','kind','pos','label'},由舊到新。
    """
    out = []
    if df is None or len(df) < 2:
        return out
    for col in (COL_A, COL_J, COL_E):
        if col not in df.columns:
            return out
    n = len(df)
    start = max(1, n - int(max(1, lookback)))
    for i in range(start, n):
        for fast, slow, label in CROSS_PAIRS:
            kind = _cross_at(df[fast], df[slow], i)
            if kind:
                out.append({'pair': label, 'fast': fast, 'slow': slow,
                            'kind': kind, 'pos': i, 'label': f"{label} {kind}"})
    return out


def evaluate(df, params=None, lookback=1):
    """一次算完:三條線的現值 + 長期趨勢 + 最近的交叉。

    回傳 dict;資料不足回 None。**這是給人看的摘要,不產生委託。**
    """
    if df is None or len(df) < 2:
        return None
    for col in (COL_A, COL_J, COL_E):
        if col not in df.columns:
            return None
    trend, e_now, slope = trend_of(df[COL_E], params)
    def _last(col):
        s = df[col].dropna()
        return float(s.iloc[-1]) if len(s) else None
    return {
        'a': _last(COL_A), 'j': _last(COL_J), 'e': e_now,
        'trend': trend, 'slope': slope,
        'crosses': crosses(df, lookback),
        'midline': float(normalize_params(params)['midline']),
    }


def summarize(result):
    """一行中文摘要,給系統日誌 / 狀態列用。"""
    if not result:
        return "資料不足,無法計算 JAE"
    def _f(v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else "--"
    head = (f"趨勢 {result['trend']}｜A {_f(result['a'])}"
            f"｜J {_f(result['j'])}｜E {_f(result['e'])}")
    if result.get('crosses'):
        head += "｜" + "、".join(c['label'] for c in result['crosses'])
    return head
