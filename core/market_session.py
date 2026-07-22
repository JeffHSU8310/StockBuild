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
