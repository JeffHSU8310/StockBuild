"""主圖長週期歷史的單一政策來源（ADR-141）。"""

MIN_HISTORY_YEARS = 10
# 10 * 365 會在跨閏年時少 2~3 天，因此留三天餘裕。
DEEP_HISTORY_DAYS = MIN_HISTORY_YEARS * 365 + 3
US_HISTORY_PERIOD = "20y"
SHIOAJI_MAX_REQUEST_DAYS = 7300  # 首次建庫向前請求 20 年，API 回多少就存多少
DEEP_TIMEFRAMES = frozenset({"日K", "周K", "月K"})


def needs_deep_history(timeframe):
    return str(timeframe or "").strip() in DEEP_TIMEFRAMES
