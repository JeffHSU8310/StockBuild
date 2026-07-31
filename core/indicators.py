"""
core.indicators — 技術指標計算 (MA/BB/MACD/RSI/KDJ/DMI)。

原本是 StockTradingAppPro.calculate_custom_indicators()，直接讀取
self.ma_shows[i].get() 等 tkinter Variable。抽出後改為顯式參數，
GUI 層呼叫前自行從 tkinter Variable 取值 (.get())，這裡只處理純運算。

刻意保留與原本完全相同的行為，包括看起來像是意外耦合的地方：
MACD/RSI/KDJ/DMI 四塊算式包在同一個 try/except 裡，任一個的參數轉換
失敗 (例如週期欄位打錯字) 會連帶讓後面幾個也不計算。這是原本就有的行為，
這次是結構重構不是邏輯修正，所以照樣保留；如果之後要拆開四個獨立
try/except 讓彼此不互相影響，應該另開一筆 ADR 記錄這個改動，不要
在「純重構」的這次改動裡夾帶進去。
"""
import numpy as np
import pandas as pd


def _calc_wma(series: pd.Series, period: int) -> pd.Series:
    if len(series) < period:
        return np.nan
    weights = np.arange(1, period + 1)
    return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


# 【ADR-131】均線類型的單一出處。原本這段內嵌在 MA1~MA6 的迴圈裡,布林中線
# 要支援 SMA/EMA/WMA 時只能再抄一份 —— 兩份各自維護遲早分歧 (P-67)。
MA_TYPES = ('SMA', 'EMA', 'WMA')


def moving_average(series: pd.Series, period: int, kind: str = 'SMA'):
    """回傳指定類型的均線;類型不認得或期間不合法回 None (呼叫端自行略過)。"""
    try:
        p = int(period)
    except (TypeError, ValueError):
        return None
    if p < 1:
        return None
    k = str(kind or 'SMA').strip().upper()
    if k == 'EMA':
        return series.ewm(span=p, adjust=False).mean()
    if k == 'WMA':
        return _calc_wma(series, p)
    if k == 'SMA':
        return series.rolling(window=p).mean()
    return None


def bollinger_set(df: pd.DataFrame, period, std_up, std_dn, ma_type='SMA',
                  prefix='BB', with_width=False) -> pd.DataFrame:
    """【ADR-131 → ADR-138】算一組布林通道,就地寫進 df。

    欄位名:{prefix}_MID / _STD / _UPPER / _LOWER。

    【ADR-138 語意變更】兩個 σ 參數從「第一對 / 第二對上下線」改成
    **「上線的 σ / 下線的 σ」** —— 使用者要求:「我要上線一個參數,下線一個
    參數,兩個要分開」。

        UPPER = MID + std_up * STD
        LOWER = MID - std_dn * STD

    所以一組布林只畫**一條上線 + 一條下線**,但兩邊可以不對稱 (例如上線 2σ、
    下線 3σ)。要畫第二條通道請開「第2組」—— 它本來就是完整獨立的一組。

    參數轉換失敗一律退回安全值 (期間 20、σ 2.0),維持本模組
    「壞參數不可以讓整張圖畫不出來」的慣例 (見 ADR-029 的降級處理)。

    ※ 中線可以是 SMA/EMA/WMA,但**標準差一律取收盤價的 rolling std** ——
      那是布林通道的定義,不隨中線類型改變。
    """
    try:
        p = max(2, int(float(str(period))))
    except (TypeError, ValueError):
        p = 20

    def _sigma(v):
        try:
            f = float(str(v))
        except (TypeError, ValueError):
            return 2.0
        return f if f > 0 else 2.0

    s_up, s_dn = _sigma(std_up), _sigma(std_dn)

    mid = moving_average(df['Close'], p, ma_type)
    if mid is None:
        mid = df['Close'].rolling(window=p).mean()
    df[f'{prefix}_MID'] = mid
    df[f'{prefix}_STD'] = df['Close'].rolling(window=p).std()
    df[f'{prefix}_UPPER'] = df[f'{prefix}_MID'] + (s_up * df[f'{prefix}_STD'])
    df[f'{prefix}_LOWER'] = df[f'{prefix}_MID'] - (s_dn * df[f'{prefix}_STD'])
    if with_width:
        df[f'{prefix}_WIDTH'] = (
            (df[f'{prefix}_UPPER'] - df[f'{prefix}_LOWER']) / df[f'{prefix}_MID'] * 100)
    return df


def rsi(close: pd.Series, period) -> pd.Series:
    """【ADR-134】RSI。算式與 calculate_indicators 內原本那段**逐字相同**
    (Wilder 平滑 = ewm(com=p-1)),抽出來是為了讓 JAE 指標共用同一份 ——
    兩份各自維護遲早分歧 (P-67)。"""
    p = int(period)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(com=p - 1, adjust=False).mean()
    loss = (-1 * delta.clip(upper=0)).ewm(com=p - 1, adjust=False).mean()
    return 100 - (100 / (1 + (gain / loss)))


def kdj(df: pd.DataFrame, n, m1, m2):
    """【ADR-134】KDJ,回傳 (rsv, k, d, j)。算式與原本那段逐字相同:
    RSV = (C - Ln) / (Hn - Ln) * 100;K/D 用 ewm(com=m-1)(m=3 時 α=1/3,
    即 K = (1/3)RSV + (2/3)K_prev,標準 KDJ);J = 3K - 2D。"""
    n, m1, m2 = int(n), int(m1), int(m2)
    low_min = df['Low'].rolling(window=n).min()
    high_max = df['High'].rolling(window=n).max()
    rsv = 100 * (df['Close'] - low_min) / (high_max - low_min)
    k = rsv.ewm(com=m1 - 1, adjust=False).mean()
    d = k.ewm(com=m2 - 1, adjust=False).mean()
    return rsv, k, d, 3 * k - 2 * d


def calculate_indicators(
    df: pd.DataFrame,
    ma_flags,        # list[bool] 長度6, 對應 MA1~MA6 是否啟用
    ma_types,         # list[str] 長度6, 每個是 "SMA"/"EMA"/"WMA"
    ma_periods,       # list[str] 長度6, 週期 (字串，內部才轉 int，故意保留轉換失敗時的靜默略過行為)
    bb_show: bool,
    bbw_show: bool,
    macd_show: bool, macd_f: str, macd_s: str, macd_sig: str,
    rsi_show: bool, rsi_p: str,
    kdj_show: bool, kd_n: str, kd_m1: str, kd_m2: str,
    dmi_show: bool, dmi_n: str,
    # 【ADR-029 → ADR-138】布林自訂:期間 + **上線 σ / 下線 σ 各一個**。
    # ADR-138 之前這兩個是「內圈那對 / 外圈那對」,現在改成上下線各自獨立。
    bb_period=20, bb_std_up=2.0, bb_std_dn=2.0,
    bb_type='SMA',                            # 【ADR-131】中線類型 SMA/EMA/WMA
    # 【ADR-131】第2組完整布林 (自己的中線 + 自己的上下線)。
    # 全部給預設值,舊呼叫端不傳也能跑。
    bb2_show=False, bb2_period=60, bb2_std_up=2.0, bb2_std_dn=2.0, bb2_type='SMA',
) -> pd.DataFrame:
    df = df.copy()

    for i in range(6):
        if ma_flags[i]:
            try:
                col = f"MA_CUSTOM_{i}"
                out = moving_average(df['Close'], int(ma_periods[i]), ma_types[i])
                if out is not None:
                    df[col] = out
            except Exception:
                pass

    if bb_show or bbw_show:
        # 第1組布林:**沿用原本的欄位名** (BB_MID/BB_UPPER/...),所以既有的
        # 繪圖、十字線提示、BBW 副圖完全不用改 —— 這是 ADR-131 刻意的選擇,
        # 把新功能的迴歸風險壓在「只有第2組是新的」。
        bollinger_set(df, bb_period, bb_std_up, bb_std_dn, ma_type=bb_type,
                      prefix='BB', with_width=True)

    if bb2_show:
        # 【ADR-131】第2組布林:自己的中線期間/類型 + 自己的上下線。
        # BB_WIDTH 只由第1組產生 (副圖只有一條,不改副圖語意)。
        bollinger_set(df, bb2_period, bb2_std_up, bb2_std_dn, ma_type=bb2_type,
                      prefix='BB2', with_width=False)

    try:
        if macd_show:
            f, s, sig = int(macd_f), int(macd_s), int(macd_sig)
            exp1 = df['Close'].ewm(span=f, adjust=False).mean()
            exp2 = df['Close'].ewm(span=s, adjust=False).mean()
            df['MACD'] = exp1 - exp2
            df['Signal'] = df['MACD'].ewm(span=sig, adjust=False).mean()
            df['Hist'] = df['MACD'] - df['Signal']
        if rsi_show:
            # 【ADR-134】改呼叫抽出來的 rsi()/kdj();算式一字未改,
            # 仍然待在原本這個共用的 try/except 裡 (P-29 刻意保留的既有耦合)。
            df['RSI'] = rsi(df['Close'], rsi_p)
        if kdj_show:
            df['RSV'], df['K'], df['D'], df['J'] = kdj(df, kd_n, kd_m1, kd_m2)
        if dmi_show:
            n = int(dmi_n)
            up_m = df['High'].diff()
            dn_m = -df['Low'].diff()
            df['+DM'] = np.where((up_m > dn_m) & (up_m > 0), up_m, 0)
            df['-DM'] = np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0)
            tr1 = df['High'] - df['Low']
            tr2 = abs(df['High'] - df['Close'].shift(1))
            tr3 = abs(df['Low'] - df['Close'].shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.ewm(span=n, adjust=False).mean()
            df['+DI'] = 100 * (df['+DM'].ewm(span=n, adjust=False).mean() / atr)
            df['-DI'] = 100 * (df['-DM'].ewm(span=n, adjust=False).mean() / atr)
            dx = 100 * abs(df['+DI'] - df['-DI']) / (df['+DI'] + df['-DI'])
            df['ADX'] = dx.ewm(span=n, adjust=False).mean()
    except Exception:
        pass
    return df
