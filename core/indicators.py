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
) -> pd.DataFrame:
    df = df.copy()

    for i in range(6):
        if ma_flags[i]:
            try:
                p = int(ma_periods[i])
                t = ma_types[i]
                col = f"MA_CUSTOM_{i}"
                if t == "SMA":
                    df[col] = df['Close'].rolling(window=p).mean()
                elif t == "EMA":
                    df[col] = df['Close'].ewm(span=p, adjust=False).mean()
                elif t == "WMA":
                    df[col] = _calc_wma(df['Close'], p)
            except Exception:
                pass

    if bb_show or bbw_show:
        # 【ADR-029】布林通道參數化:期間與第一組標準差可自訂;第二組標準差
        # (bb_std2) > 0 時額外產出 BB_UPPER2/BB_LOWER2 (上下限各兩組)。
        # 參數轉換失敗時退回 20/2.0/不畫第二組,維持本模組「靜默略過」慣例。
        try:
            _p = max(2, int(float(str(bb_period))))
        except (TypeError, ValueError):
            _p = 20
        try:
            _s1 = float(str(bb_std1))
            if _s1 <= 0: _s1 = 2.0
        except (TypeError, ValueError):
            _s1 = 2.0
        try:
            _s2 = float(str(bb_std2))
        except (TypeError, ValueError):
            _s2 = 0.0
        df['BB_MID'] = df['Close'].rolling(window=_p).mean()
        df['BB_STD'] = df['Close'].rolling(window=_p).std()
        df['BB_UPPER'] = df['BB_MID'] + (_s1 * df['BB_STD'])
        df['BB_LOWER'] = df['BB_MID'] - (_s1 * df['BB_STD'])
        df['BB_WIDTH'] = (df['BB_UPPER'] - df['BB_LOWER']) / df['BB_MID'] * 100
        if _s2 > 0 and abs(_s2 - _s1) > 1e-9:
            df['BB_UPPER2'] = df['BB_MID'] + (_s2 * df['BB_STD'])
            df['BB_LOWER2'] = df['BB_MID'] - (_s2 * df['BB_STD'])

    try:
        if macd_show:
            f, s, sig = int(macd_f), int(macd_s), int(macd_sig)
            exp1 = df['Close'].ewm(span=f, adjust=False).mean()
            exp2 = df['Close'].ewm(span=s, adjust=False).mean()
            df['MACD'] = exp1 - exp2
            df['Signal'] = df['MACD'].ewm(span=sig, adjust=False).mean()
            df['Hist'] = df['MACD'] - df['Signal']
        if rsi_show:
            p = int(rsi_p)
            delta = df['Close'].diff()
            gain = delta.clip(lower=0).ewm(com=p - 1, adjust=False).mean()
            loss = (-1 * delta.clip(upper=0)).ewm(com=p - 1, adjust=False).mean()
            df['RSI'] = 100 - (100 / (1 + (gain / loss)))
        if kdj_show:
            n, m1, m2 = int(kd_n), int(kd_m1), int(kd_m2)
            low_min = df['Low'].rolling(window=n).min()
            high_max = df['High'].rolling(window=n).max()
            df['RSV'] = 100 * (df['Close'] - low_min) / (high_max - low_min)
            df['K'] = df['RSV'].ewm(com=m1 - 1, adjust=False).mean()
            df['D'] = df['K'].ewm(com=m2 - 1, adjust=False).mean()
            df['J'] = 3 * df['K'] - 2 * df['D']
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
