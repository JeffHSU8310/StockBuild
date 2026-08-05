"""K 線主圖的可視資料視窗規則。

這裡只決定「畫布應收到多少根 K 棒」，不碰 tkinter 或
matplotlib，因此可以離線單元測試。完整歷史仍由呼叫端保留，
這個模組絕不改變技術指標的計算範圍。
"""

INTRADAY_CAP = 1200
# 長週期的上限不能共用一個「看起來快」的 1000 根：台股一年
# 約 250 個交易日，1000 根只有約 4 年。ADR-141 改成依週期保證
# 畫布本身就能容納至少 10 年，不再把已載入的舊資料切掉。
LONG_TERM_LIMITS = {
    "日K": (2600, 3000),
    "周K": (530, 650),
    "月K": (125, 180),
}
MIN_RENDER_BARS = 300
BARS_PER_PIXEL = 1.25


def is_intraday(timeframe):
    """只把分 K 視為盤中週期；日／週／月 K 沿用較低上限。"""
    text = str(timeframe or "")
    return "分" in text and "K" in text


def render_limit(timeframe, pixel_width=None):
    """依畫布寬度回傳有上限的 K 棒數。

    Tk 在尚未排版時常回傳 1px；寬度小於 200 時代表尚未取得
    可信尺寸，這時回到 ADR-082 的固定上限。正常時每像素最多
    保留 1.25 根，避免在實際無法分辨的寬度上建立太多 artist。
    """
    text = str(timeframe or "")
    minimum, cap = ((MIN_RENDER_BARS, INTRADAY_CAP) if is_intraday(text)
                    else LONG_TERM_LIMITS.get(text, (MIN_RENDER_BARS, 1000)))
    try:
        width = int(pixel_width)
    except (TypeError, ValueError, OverflowError):
        return cap
    if width < 200:
        return cap
    adaptive = max(minimum, int(width * BARS_PER_PIXEL))
    return min(cap, adaptive)


def tail_window(df, timeframe, pixel_width=None):
    """回傳畫布用的尾端視窗副本與實際上限。"""
    limit = render_limit(timeframe, pixel_width)
    if len(df) > limit:
        return df.iloc[-limit:].copy(), limit
    return df.copy(), limit
