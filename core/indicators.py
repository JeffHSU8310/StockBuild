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


def bollinger_set(df: pd.DataFrame, period, std_a, std_b, ma_type='SMA',
                  prefix='BB', with_width=False) -> pd.DataFrame:
    """【ADR-131】算一組布林通道 (中線 + 兩條上下線),就地寫進 df。

    欄位名:{prefix}_MID / _STD / _UPPER / _LOWER,第二條上下線是
    {prefix}_UPPER2 / _LOWER2。prefix='BB' 時產生的欄位名與 ADR-029 完全相同,
    所以第1組不需要任何呼叫端配合修改。

    參數轉換失敗一律退回安全值 (期間 20、σ 2.0、不畫第二條),維持本模組
    「壞參數不可以讓整張圖畫不出來」的慣例 (P-?? / 見 ADR-029 的降級處理)。

    ※ 中線可以是 SMA/EMA/WMA,但**標準差一律取收盤價的 rolling std** ——
      那是布林通道的定義,不隨中線類型改變。
    """
    try:
        p = max(2, int(float(str(period))))
    except (TypeError, ValueError):
        p = 20
    try:
        s_a = float(str(std_a))
        if s_a <= 0:
            s_a = 2.0
    except (TypeError, ValueError):
        s_a = 2.0
    try:
        s_b = float(str(std_b))
    except (TypeError, ValueError):
        s_b = 0.0

    mid = moving_average(df['Close'], p, ma_type)
    if mid is None:
        mid = df['Close'].rolling(window=p).mean()
    df[f'{prefix}_MID'] = mid
    df[f'{prefix}_STD'] = df['Close'].rolling(window=p).std()
    df[f'{prefix}_UPPER'] = df[f'{prefix}_MID'] + (s_a * df[f'{prefix}_STD'])
    df[f'{prefix}_LOWER'] = df[f'{prefix}_MID'] - (s_a * df[f'{prefix}_STD'])
    if with_width:
        df[f'{prefix}_WIDTH'] = (
            (df[f'{prefix}_UPPER'] - df[f'{prefix}_LOWER']) / df[f'{prefix}_MID'] * 100)
    # 第二條上下線:σ<=0 或跟第一條一樣就不畫 (畫了也是重疊的兩條線)
    if s_b > 0 and abs(s_b - s_a) > 1e-9:
        df[f'{prefix}_UPPER2'] = df[f'{prefix}_MID'] + (s_b * df[f'{prefix}_STD'])
        df[f'{prefix}_LOWER2'] = df[f'{prefix}_MID'] - (s_b * df[f'{prefix}_STD'])
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
    bb_period=20, bb_std1=2.0, bb_std2=0.0,  # 【ADR-029】布林自訂:期間+兩組標準差 (std2<=0 不算第二組)
    bb_type='SMA',                            # 【ADR-131】中線類型 SMA/EMA/WMA
    # 【ADR-131】第2組完整布林 (自己的中線 + 自己的兩條上下線)。
    # 全部給預設值,舊呼叫端不傳也能跑。
    bb2_show=False, bb2_period=60, bb2_std1=2.0, bb2_std2=0.0, bb2_type='SMA',
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
        bollinger_set(df, bb_period, bb_std1, bb_std2, ma_type=bb_type,
                      prefix='BB', with_width=True)

    if bb2_show:
        # 【ADR-131】第2組布林:自己的中線期間/類型 + 自己的兩條上下線。
        # BB_WIDTH 只由第1組產生 (副圖只有一條,不改副圖語意)。
        bollinger_set(df, bb2_period, bb2_std1, bb2_std2, ma_type=bb2_type,
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
