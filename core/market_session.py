"""
core.market_session — 台股 / 台期貨「交易時段」判斷 (單一真相來源)。

【為什麼獨立成一個 core 模組 (ADR-070)】
自動交易要「非交易時間不動作、交易時間自己運作」,就必須有一個「現在這個
市場開盤了沒」的權威判斷。把它寫成純函式放 core/,理由與 ADR-009 一致:
零 tkinter / 零 shioaji 依賴,可以離線單元測試,時間邊界不用開盤也能驗。

時段定義 (使用者確認):
  * 台股 (股票/零股)     : 09:00 ~ 13:30   (週一~週五)
  * 台期貨 日盤          : 08:45 ~ 13:45   (週一~週五)
  * 台期貨 夜盤 (盤後)   : 15:00 ~ 次日 05:00 (週一~週五「當晚」開,故跨到週六05:00)

夜盤跨午夜的歸屬 (重要,別踩坑):
  - 傍晚段 15:00~24:00:當天是週一~週五才有 (該晚開盤)。
  - 凌晨段 00:00~05:00:屬於「前一個交易日晚上」那一盤,所以當天是
    週二~週六才有 (前一天週一~週五)。週日與週一凌晨沒有夜盤。

【已知限制】此模組只看「星期 + 時刻」,不含國定假日行事曆。假日雖然時刻落在
時段內會被判為 open,但實務上假日券商不會有新 K 棒進來 → 自動交易評估時抓不到
新資料自然不動作;若日後要精準擋假日,可在 HOLIDAYS 補上日期集合 (預留掛勾)。
"""
from datetime import datetime, timedelta

# 時刻以「當日 0 點起算的分鐘數」表示,邊界比較才不會被 time 物件比較搞混。
STOCK_OPEN_MIN = 9 * 60          # 09:00 (整股)
STOCK_CLOSE_MIN = 13 * 60 + 30   # 13:30

# 【ADR-072】盤中零股開盤時刻「可設定」,預設 09:10 (現制);未來交易所改成
# 09:00 時,使用者自己在設定切一下即可,不必改程式。收盤與整股同為 13:30。
# 這是模組層級的「可變設定」,由 GUI 讀持久化設定後呼叫 set_odd_lot_open_minute()
# 覆寫;純函式本身仍可離線測試 (測試會顯式帶入 dt,不依賴這個全域值的預設)。
ODD_LOT_OPEN_MIN = 9 * 60 + 10   # 09:10 (預設)


def set_odd_lot_open_minute(minute):
    """設定盤中零股開盤時刻 (0 點起算的分鐘數)。GUI 載入使用者設定後呼叫。"""
    global ODD_LOT_OPEN_MIN
    try:
        m = int(minute)
        if 0 <= m < 24 * 60:
            ODD_LOT_OPEN_MIN = m
    except (TypeError, ValueError):
        pass


def set_odd_lot_open_hhmm(hhmm):
    """以 'HH:MM' 字串設定盤中零股開盤時刻;格式不對就不動作。"""
    try:
        parts = str(hhmm).strip().split(':')
        set_odd_lot_open_minute(int(parts[0]) * 60 + int(parts[1]))
    except (TypeError, ValueError, IndexError):
        pass

FUT_DAY_OPEN_MIN = 8 * 60 + 45   # 08:45
FUT_DAY_CLOSE_MIN = 13 * 60 + 45 # 13:45

FUT_NIGHT_OPEN_MIN = 15 * 60     # 15:00 (傍晚段起點)
FUT_NIGHT_MORNING_CLOSE_MIN = 5 * 60  # 05:00 (凌晨段終點)

# 【預留掛勾】國定假日 (yyyy-mm-dd 字串集合)。目前留空,呼叫端可自行灌入。
HOLIDAYS = set()


def _minutes(dt):
    return dt.hour * 60 + dt.minute


def _is_weekday(dt):
    return dt.weekday() < 5  # 0=週一 ... 4=週五


def _is_holiday(dt):
    try:
        return dt.strftime('%Y-%m-%d') in HOLIDAYS
    except Exception:
        return False


def is_stock_open(dt=None):
    """台股整股是否在盤中。週一~週五 09:00~13:30。"""
    if dt is None:
        dt = datetime.now()
    if _is_holiday(dt) or not _is_weekday(dt):
        return False
    m = _minutes(dt)
    return STOCK_OPEN_MIN <= m <= STOCK_CLOSE_MIN


def is_odd_lot_open(dt=None, open_minute=None):
    """盤中零股是否在盤中。週一~週五,開盤 = ODD_LOT_OPEN_MIN (預設 09:10,可設定)
    ~ 13:30。open_minute 顯式帶入時優先 (方便單元測試固定邊界)。"""
    if dt is None:
        dt = datetime.now()
    if _is_holiday(dt) or not _is_weekday(dt):
        return False
    m = _minutes(dt)
    o = ODD_LOT_OPEN_MIN if open_minute is None else int(open_minute)
    return o <= m <= STOCK_CLOSE_MIN


def is_futures_day_open(dt=None):
    """台期貨日盤是否在盤中。週一~週五 08:45~13:45。"""
    if dt is None:
        dt = datetime.now()
    if _is_holiday(dt) or not _is_weekday(dt):
        return False
    m = _minutes(dt)
    return FUT_DAY_OPEN_MIN <= m <= FUT_DAY_CLOSE_MIN


def is_futures_night_open(dt=None):
    """台期貨夜盤是否在盤中 (跨午夜)。
    傍晚段 15:00~24:00:當天為週一~週五。
    凌晨段 00:00~05:00:屬前一交易日的夜盤,故當天為週二~週六 (前一天週一~週五)。
    """
    if dt is None:
        dt = datetime.now()
    if _is_holiday(dt):
        return False
    m = _minutes(dt)
    # 傍晚段:今天是交易日 (週一~週五) 才會在今晚開盤
    if m >= FUT_NIGHT_OPEN_MIN and _is_weekday(dt):
        return True
    # 凌晨段:歸屬前一天的夜盤,需前一天是交易日
    if m < FUT_NIGHT_MORNING_CLOSE_MIN:
        prev = dt - timedelta(days=1)
        if _is_weekday(prev) and not _is_holiday(prev):
            return True
    return False


def is_futures_open(dt=None, include_night=True):
    """台期貨是否在盤中 (日盤;include_night=True 時也含夜盤)。"""
    if is_futures_day_open(dt):
        return True
    if include_night and is_futures_night_open(dt):
        return True
    return False


def is_market_open(trade_type, dt=None, include_night=True):
    """依交易種類判斷對應市場是否開盤。
    trade_type: '股票'/'零股' → 台股;'期貨' → 台期貨 (含/不含夜盤看 include_night)。
    未知種類保守回 False (寧可不動作,也不要在不確定時亂送單)。
    """
    if trade_type == '零股':
        return is_odd_lot_open(dt)
    if trade_type == '股票':
        return is_stock_open(dt)
    if trade_type == '期貨':
        return is_futures_open(dt, include_night=include_night)
    return False


def session_label(trade_type, dt=None, include_night=True):
    """回傳目前所屬盤別的中文標籤 (給日誌用);休市回 '休市'。"""
    if dt is None:
        dt = datetime.now()
    if trade_type == '零股':
        return '零股盤中' if is_odd_lot_open(dt) else '休市'
    if trade_type == '股票':
        return '台股盤中' if is_stock_open(dt) else '休市'
    if trade_type == '期貨':
        if is_futures_day_open(dt):
            return '期貨日盤'
        if include_night and is_futures_night_open(dt):
            return '期貨夜盤'
        return '休市'
    return '休市'


# ---------------------------------------------------------------------------
# 【ADR-121】開盤暖機
# ---------------------------------------------------------------------------
# 使用者實測:量化策略在 08:45 與 09:00 各出現一次
# `ShioajiTimeoutError: Timeout Topic: api/v1/data/kbars ... 30000ms`。
# 這兩個時刻正好是 FUT_DAY_OPEN_MIN 與 STOCK_OPEN_MIN —— 也就是時段閘門
# 打開的那一秒。量化 runner 每 2 秒醒一次,閘門一開就立刻發出當天第一個
# kbars 請求,而那一秒全台灣所有 shioaji 用戶端都在做同一件事,券商的
# kbars 服務同時還在為新交易日翻檔。
#
# 解法是「別在鐘響那一秒去要 K 線」。這個延遲**不損失任何資訊**:呼叫端
# (_qt_fetch_closed_bars) 本來就會丟掉最後一根視為未完成,開盤後第一根
# **新的已收盤 K 棒**要等到 08:50 (5分K) / 08:46 (1分K) 才存在。
#
# 放在這個模組而不是 GUI 層的理由同 ADR-070:時間邊界要能離線測試,不能
# 只在真的開盤時才驗得到。
QT_OPEN_WARMUP_SEC = 30


def _session_open_minute(trade_type, dt, include_night=True):
    """這個交易種類在 dt 當下所屬盤別的**開盤分鐘數**;判不出來回 None。

    只回「開盤」的時刻,不回收盤 —— 夜盤的 05:00 是收盤不是開盤,凌晨段
    不算「剛開盤」(那一盤是前一天 15:00 開的)。
    """
    if trade_type == '零股':
        return ODD_LOT_OPEN_MIN if is_odd_lot_open(dt) else None
    if trade_type == '股票':
        return STOCK_OPEN_MIN if is_stock_open(dt) else None
    if trade_type == '期貨':
        if is_futures_day_open(dt):
            return FUT_DAY_OPEN_MIN
        if include_night and is_futures_night_open(dt) and _minutes(dt) >= FUT_NIGHT_OPEN_MIN:
            return FUT_NIGHT_OPEN_MIN
    return None


def just_opened(trade_type, dt=None, include_night=True, warmup_sec=None):
    """現在是不是「剛開盤 warmup_sec 秒內」。休市 / 判不出盤別一律回 False。

    區間是**左閉右開** [開盤, 開盤+warmup_sec):開盤當下算「剛開盤」,
    暖機秒數一到就放行。warmup_sec <= 0 等於關閉這個機制。
    """
    if dt is None:
        dt = datetime.now()
    if warmup_sec is None:
        warmup_sec = QT_OPEN_WARMUP_SEC
    try:
        warmup_sec = float(warmup_sec)
    except (TypeError, ValueError):
        return False
    if warmup_sec <= 0:
        return False
    open_min = _session_open_minute(trade_type, dt, include_night=include_night)
    if open_min is None:
        return False
    elapsed = (_minutes(dt) - open_min) * 60 + dt.second
    return 0 <= elapsed < warmup_sec


# ---------------------------------------------------------------------------
# 【ADR-127】「日K 類的快取還新鮮嗎」—— 用『有沒有跨過開盤』判斷,不用牆上時鐘
# ---------------------------------------------------------------------------
# 背景:日K/周K/月K 的策略資料,呼叫端 (_qt_fetch_closed_bars) 會**丟掉最後
# 一根**視為未完成,所以「已收盤」的集合**只在新的一盤開始時才會多一根**。
# 盤中重抓一定拿到一模一樣的東西 —— 用牆上時鐘 (例如 30 分鐘 TTL) 去判斷
# 新鮮度,結果就是整天每 30 分鐘白抓一次 300 天的資料、並且推播一次通知
# (使用者實測:09:20 與 09:55 各收到一則「背景補齊中」)。
#
# 保守設計刻意寫在這裡講明白:
#   * 開盤時刻取**所有商品的聯集**(整股/零股/期貨日盤/期貨夜盤)
#   * **不看國定假日**
# 兩者都讓快取「早一點過期」——那是安全方向 (多抓一次資料,無害);
# 少列一個開盤時刻才危險 (拿舊資料去評估,會下錯單)。

def session_open_minutes():
    """所有可能的開盤時刻 (當日 0 點起算的分鐘數),取聯集後排序。"""
    return sorted({STOCK_OPEN_MIN, ODD_LOT_OPEN_MIN,
                   FUT_DAY_OPEN_MIN, FUT_NIGHT_OPEN_MIN})


MAX_CACHE_SPAN_DAYS = 7          # 超過這麼久一律視為過期 (不必逐日枚舉)


def any_session_opens_between(t0, t1):
    """(t0, t1] 之間有沒有跨過任何一個交易時段的開盤時刻。

    回傳 True = 期間內有開盤發生過 → 日K 類快取應視為過期。
    參數顛倒 (t1 早於 t0) 或跨太久一律回 True (寧可過期,不可誤判新鮮)。
    """
    if t0 is None or t1 is None:
        return True
    if t1 <= t0:
        return t1 < t0          # 時鐘倒退 (校時/夏令時) → 當成過期
    if (t1 - t0).days > MAX_CACHE_SPAN_DAYS:
        return True
    opens = session_open_minutes()
    day = t0.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = t1.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end_day:
        if _is_weekday(day) and not _is_holiday(day):
            for m in opens:
                op = day + timedelta(minutes=m)
                if t0 < op <= t1:
                    return True
        day += timedelta(days=1)
    return False
