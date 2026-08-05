# -*- coding: utf-8 -*-
"""
core/strategy_engine.py — 量化自動交易引擎 (純邏輯層)

【ADR-035】設計原則:
1. 零 tkinter / 零 shioaji 依賴:所有條件評估、狀態機、風控守門都是純函式,
   可用 tests/test_core.py 離線完整驗證——自動下單的邏輯絕不允許「沒測過就上」。
2. 只用「已收盤的K棒」評估 (呼叫端負責把最後一根未完成K棒剔除):
   未完成K棒的值會不斷變動,用它評估會產生大量假訊號 (俗稱 repaint)。
3. 條件自帶指標計算 (SMA/EMA/RSI/KD/MACD/BB/N期高低),不依賴圖表上使用者
   勾了哪些指標——策略邏輯與畫面設定完全脫鉤。
4. 「交叉」類條件只在交叉發生的那一根K棒為 True (前一根關係相反),
   天生一次性,搭配「同一根K棒不重複評估」的閘門,杜絕重複下單。
5. 風控守門 (risk_check) 是最後一道關卡:每日次數上限、冷卻時間、
   每日虧損熔斷,任何一關不過就擋單並回傳可讀原因。

本模組不做的事 (由 GUI 層負責):合約解析、實際下單、執行緒、UI 更新。
"""
import re
import uuid
import pandas as pd

from core import sj_compat as _sjc

# 【ADR-101】籌碼條件:籌碼在資料準備階段被併成 df 的 Chip* 欄位,
# 條件函式簽名維持 func(df, params) 不變,實盤與回測因此自動共用同一條路。
from core import chips_features as _cf
# 【ADR-143】當日漲跌幅要照「交易日」分組 (期貨夜盤歸下一個交易日, ADR-007)。
from core import futures_session as _fs


class KBarsFetchError(Exception):
    """【ADR-121】抓 K 線資料失敗 (逾時/券商端暫時性管制等)。

    存在的理由是**型別區分**,不是為了帶什麼額外資訊:呼叫端必須能分辨
    「抓不到資料」與「策略邏輯壞掉」——前者是外部環境問題,重試就會過,
    而且策略在沒有資料時本來就不會產生任何 intent;後者才代表這檔策略
    真的不能再讓它動作。兩者混在同一個錯誤計數裡,會讓一次網路瞬斷把
    一個**持有部位**的策略自動停用,連帶把即時停損也一起關掉。

    放在 core/ 而不是 GUI 層:純例外型別,零依賴,離線測試要 import 得到。
    """
    pass


class KBarsPending(KBarsFetchError):
    """【ADR-122】資料**正在背景補**,還不能評估 —— 這**不是錯誤**。

    大範圍請求 (日/周/月K) 改由背景執行緒分段預抓,runner 這一輪拿不到資料
    是預期中的事,幾秒後就會有。跟真正的失敗分開才不會把「正在下載」記成
    「抓不到資料」,也不該累加任何錯誤計數。
    """
    pass


class KBarsUnavailable(KBarsFetchError):
    """【ADR-122】真的抓不到 (逾時/券商端拒絕/分段有段失敗)。要記錄與診斷。"""
    pass


# ============================================================
# 指標計算 (自帶,與圖表設定脫鉤)
# ============================================================

def _sma(s, n):
    return s.rolling(window=int(n)).mean()

def _ema(s, n):
    return s.ewm(span=int(n), adjust=False).mean()

def _rsi(close, n):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(int(n)).mean()
    loss = (-delta.clip(upper=0)).rolling(int(n)).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))

def _kd(df, n=9, m1=3, m2=3):
    low_n = df['Low'].rolling(int(n)).min()
    high_n = df['High'].rolling(int(n)).max()
    rsv = (df['Close'] - low_n) / (high_n - low_n).replace(0, pd.NA) * 100
    k = rsv.ewm(com=int(m1) - 1, adjust=False).mean()
    d = k.ewm(com=int(m2) - 1, adjust=False).mean()
    return k, d

def _macd(close, f=12, s=26, sig=9):
    dif = _ema(close, f) - _ema(close, s)
    signal = _ema(dif, sig)
    hist = dif - signal
    return dif, signal, hist

def _bb(close, n=20, std=2.0):
    mid = _sma(close, n)
    sd = close.rolling(int(n)).std()
    return mid + float(std) * sd, mid, mid - float(std) * sd


def _cross_up(a, b):
    """a 是否在最後一根K棒「向上穿越」b (前一根 a<=b,這一根 a>b)。"""
    if len(a) < 2 or pd.isna(a.iloc[-1]) or pd.isna(b.iloc[-1]) or pd.isna(a.iloc[-2]) or pd.isna(b.iloc[-2]):
        return False
    return a.iloc[-2] <= b.iloc[-2] and a.iloc[-1] > b.iloc[-1]

def _cross_down(a, b):
    if len(a) < 2 or pd.isna(a.iloc[-1]) or pd.isna(b.iloc[-1]) or pd.isna(a.iloc[-2]) or pd.isna(b.iloc[-2]):
        return False
    return a.iloc[-2] >= b.iloc[-2] and a.iloc[-1] < b.iloc[-1]


# ============================================================
# 條件庫:key -> (中文名, 參數規格 [(參數key, 中文標籤, 預設值)], 評估函式)
# 評估函式簽名: fn(df, params) -> bool,一律在「最後一根 (已收盤) K棒」評估。
# ============================================================

def _c_ma_cross_up(df, p):
    return _cross_up(_sma(df['Close'], p.get('fast', 5)), _sma(df['Close'], p.get('slow', 20)))

def _c_ma_cross_down(df, p):
    return _cross_down(_sma(df['Close'], p.get('fast', 5)), _sma(df['Close'], p.get('slow', 20)))

def _c_price_break_high(df, p):
    n = int(p.get('n', 20))
    if len(df) < n + 1:
        return False
    return df['Close'].iloc[-1] > df['High'].iloc[-(n + 1):-1].max()

def _c_price_break_low(df, p):
    n = int(p.get('n', 20))
    if len(df) < n + 1:
        return False
    return df['Close'].iloc[-1] < df['Low'].iloc[-(n + 1):-1].min()

def _c_price_above(df, p):
    return float(df['Close'].iloc[-1]) > float(p.get('value', 0))

def _c_price_below(df, p):
    v = float(p.get('value', 0))
    return v > 0 and float(df['Close'].iloc[-1]) < v

def _c_bb_cross_upper(df, p):
    upper, _, _ = _bb(df['Close'], p.get('n', 20), p.get('std', 2.0))
    return _cross_up(df['Close'], upper)

def _c_bb_cross_lower(df, p):
    _, _, lower = _bb(df['Close'], p.get('n', 20), p.get('std', 2.0))
    return _cross_down(df['Close'], lower)

def _c_kd_cross_up(df, p):
    k, d = _kd(df, p.get('n', 9), p.get('m1', 3), p.get('m2', 3))
    return _cross_up(k, d)

def _c_kd_cross_down(df, p):
    k, d = _kd(df, p.get('n', 9), p.get('m1', 3), p.get('m2', 3))
    return _cross_down(k, d)

def _c_kd_above(df, p):
    k, _ = _kd(df, p.get('n', 9), 3, 3)
    return (not pd.isna(k.iloc[-1])) and k.iloc[-1] > float(p.get('value', 80))

def _c_kd_below(df, p):
    k, _ = _kd(df, p.get('n', 9), 3, 3)
    return (not pd.isna(k.iloc[-1])) and k.iloc[-1] < float(p.get('value', 20))

def _c_macd_cross_up(df, p):
    dif, sig, _ = _macd(df['Close'], p.get('f', 12), p.get('s', 26), p.get('sig', 9))
    return _cross_up(dif, sig)

def _c_macd_cross_down(df, p):
    dif, sig, _ = _macd(df['Close'], p.get('f', 12), p.get('s', 26), p.get('sig', 9))
    return _cross_down(dif, sig)

def _c_rsi_above(df, p):
    r = _rsi(df['Close'], p.get('n', 14))
    return (not pd.isna(r.iloc[-1])) and r.iloc[-1] > float(p.get('value', 70))

def _c_rsi_below(df, p):
    r = _rsi(df['Close'], p.get('n', 14))
    return (not pd.isna(r.iloc[-1])) and r.iloc[-1] < float(p.get('value', 30))


# ============================================================
# 【ADR-057】條件庫大擴充 (使用者需求 #3/#4:收盤突破/跌破均線、均線參數可調、
# 條件要更豐富自由)
#
# 設計原則:
#  * 每個條件都只用「已收盤K棒」評估 (P-49),與既有條件一致,不開特例。
#  * 均線一律支援 SMA/EMA 切換 (kind 參數),期間可自由設定 —— 使用者原本
#    只能用寫死 SMA 的黃金/死亡交叉,這是最常被要求的自由度。
#  * 「交叉 (cross)」與「狀態 (above/below)」刻意分開:交叉只在穿越那一根
#    成立 (適合當進場訊號,不會每根都觸發);狀態每根都可能成立 (適合當
#    過濾條件,例如「站上季線才做多」)。兩者語意不同,不要混用。
# ============================================================

def _ma_of(series, n, kind='SMA'):
    """依 kind 回傳 SMA 或 EMA。kind 認不得就退回 SMA (安全預設,不拋例外)。"""
    return _ema(series, n) if str(kind).upper() == 'EMA' else _sma(series, n)


def _c_price_cross_up_ma(df, p):
    """收盤價「向上突破」均線 (前一根在均線之下/等於,這一根站上去)。"""
    return _cross_up(df['Close'], _ma_of(df['Close'], p.get('n', 20), p.get('kind', 'SMA')))

def _c_price_cross_down_ma(df, p):
    """收盤價「向下跌破」均線。"""
    return _cross_down(df['Close'], _ma_of(df['Close'], p.get('n', 20), p.get('kind', 'SMA')))

def _c_price_above_ma(df, p):
    """收盤價「位於」均線之上 (狀態,每根都可能成立,適合當過濾條件)。"""
    ma = _ma_of(df['Close'], p.get('n', 20), p.get('kind', 'SMA'))
    return (not pd.isna(ma.iloc[-1])) and float(df['Close'].iloc[-1]) > float(ma.iloc[-1])

def _c_price_below_ma(df, p):
    ma = _ma_of(df['Close'], p.get('n', 20), p.get('kind', 'SMA'))
    return (not pd.isna(ma.iloc[-1])) and float(df['Close'].iloc[-1]) < float(ma.iloc[-1])

def _c_ma_slope_up(df, p):
    """均線上彎:目前均線值 > N根之前的均線值 (趨勢過濾用)。"""
    n = int(p.get('n', 20)); look = int(p.get('look', 3))
    ma = _ma_of(df['Close'], n, p.get('kind', 'SMA'))
    if len(ma) < look + 1 or pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-1 - look]):
        return False
    return float(ma.iloc[-1]) > float(ma.iloc[-1 - look])

def _c_ma_slope_down(df, p):
    n = int(p.get('n', 20)); look = int(p.get('look', 3))
    ma = _ma_of(df['Close'], n, p.get('kind', 'SMA'))
    if len(ma) < look + 1 or pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-1 - look]):
        return False
    return float(ma.iloc[-1]) < float(ma.iloc[-1 - look])

def _c_ma_align_bull(df, p):
    """均線多頭排列:短均 > 中均 > 長均 (三線同時成立才算)。"""
    c = df['Close']; kind = p.get('kind', 'SMA')
    a = _ma_of(c, p.get('short', 5), kind); b = _ma_of(c, p.get('mid', 20), kind); d = _ma_of(c, p.get('long', 60), kind)
    if pd.isna(a.iloc[-1]) or pd.isna(b.iloc[-1]) or pd.isna(d.iloc[-1]):
        return False
    return float(a.iloc[-1]) > float(b.iloc[-1]) > float(d.iloc[-1])

def _c_ma_align_bear(df, p):
    c = df['Close']; kind = p.get('kind', 'SMA')
    a = _ma_of(c, p.get('short', 5), kind); b = _ma_of(c, p.get('mid', 20), kind); d = _ma_of(c, p.get('long', 60), kind)
    if pd.isna(a.iloc[-1]) or pd.isna(b.iloc[-1]) or pd.isna(d.iloc[-1]):
        return False
    return float(a.iloc[-1]) < float(b.iloc[-1]) < float(d.iloc[-1])

def _c_volume_above_ma(df, p):
    """成交量放大:本根量 > N日均量 × 倍數 (預設 20 日均量的 1.5 倍)。"""
    n = int(p.get('n', 20)); mult = float(p.get('mult', 1.5))
    if 'Volume' not in df.columns or len(df) < n + 1:
        return False
    v = df['Volume']; vma = v.rolling(n).mean()
    if pd.isna(vma.iloc[-1]) or float(vma.iloc[-1]) <= 0:
        return False
    return float(v.iloc[-1]) > float(vma.iloc[-1]) * mult

def _c_volume_below_ma(df, p):
    """成交量萎縮:本根量 < N日均量 × 倍數 (預設 0.7 倍,量縮盤整過濾用)。"""
    n = int(p.get('n', 20)); mult = float(p.get('mult', 0.7))
    if 'Volume' not in df.columns or len(df) < n + 1:
        return False
    v = df['Volume']; vma = v.rolling(n).mean()
    if pd.isna(vma.iloc[-1]) or float(vma.iloc[-1]) <= 0:
        return False
    return float(v.iloc[-1]) < float(vma.iloc[-1]) * mult

def _c_consecutive_up(df, p):
    """連續 N 根收紅 (收 > 開)。"""
    n = int(p.get('n', 3))
    if len(df) < n:
        return False
    seg = df.iloc[-n:]
    return bool((seg['Close'] > seg['Open']).all())

def _c_consecutive_down(df, p):
    n = int(p.get('n', 3))
    if len(df) < n:
        return False
    seg = df.iloc[-n:]
    return bool((seg['Close'] < seg['Open']).all())

def _c_pct_change_above(df, p):
    """單根漲幅 >= 指定 %(相對前一根收盤)。"""
    v = float(p.get('value', 3.0))
    if len(df) < 2 or float(df['Close'].iloc[-2]) == 0:
        return False
    chg = (float(df['Close'].iloc[-1]) / float(df['Close'].iloc[-2]) - 1.0) * 100.0
    return chg >= v

def _c_pct_change_below(df, p):
    """單根跌幅 <= 指定 %(填 -3 表示跌 3% 以上)。"""
    v = float(p.get('value', -3.0))
    if len(df) < 2 or float(df['Close'].iloc[-2]) == 0:
        return False
    chg = (float(df['Close'].iloc[-1]) / float(df['Close'].iloc[-2]) - 1.0) * 100.0
    return chg <= v

# ---------------------------------------------------------------------------
# 【ADR-143】當日(盤中)漲跌幅
#
# 使用者要的策略:「當 A 盤中跌幅超過 X% 就買進 B;當 A 盤中漲幅超過 Y%
# 就賣出 B」。這是**逆勢**的一組條件,配合既有的看A做B機制就成立 ——
# 引擎不必知道 B 的存在,這裡只負責回答「A 今天跌/漲了多少」。
#
# 與既有的 pct_change_above/below 完全不同:那兩個是「**單根**相對前一根」,
# 在 5分K 上算的是 5 分鐘的變化;這裡算的是「**當日相對參考價**」,也就是
# 台股講「漲跌幅」時的那個數字(漲跌停也是以它 ±10%)。
# ---------------------------------------------------------------------------

#: 當日漲跌幅的參考價基準
DAY_PCT_BASES = ('昨收', '今開')
#: 用哪個價格跟參考價比
DAY_PCT_USES = ('盤中觸價', '收盤價')


def _bar_dates(index):
    """索引 → 每根 K 棒的「交易日」清單。轉不出來的位置放 None。

    期貨夜盤要照 ADR-007 的交易日規則歸屬(15:00 之後算下一個交易日),
    否則「當日」會從半夜切成兩半,跌幅整個算錯。是否套用這條規則由資料
    本身決定(見 futures_session.has_night_session_bars)。
    """
    try:
        use_session = _fs.has_night_session_bars(index)
    except Exception:
        use_session = False
    out = []
    for t in index:
        try:
            out.append(_fs.session_date_of(t) if use_session
                       else pd.Timestamp(t).normalize().date())
        except Exception:
            out.append(None)
    return out


def _day_ref_price(df, base='昨收'):
    """當日漲跌幅的參考價。找不到回 None。

    昨收 = **前一個交易日最後一根**的收盤價。台股講「漲跌幅」就是相對這個
           價格,所以這是預設值。
    今開 = 當日**第一根**的開盤價。

    找不到(df 只有當天、索引不是時間)一律回 None,呼叫端當條件不成立。
    **不可以退而求其次拿「前一根收盤」充數**:在 5分K 上那是 5 分鐘前的價格,
    算出來的「當日跌幅」跟使用者想的完全是兩回事,而且他不會發現
    (鐵則 4 的精神:沒有的東西不要編一個出來)。

    ※ 日K 上這個定義會自然退化成「前一根收盤」—— 前一根就是前一個交易日,
       不需要特別處理。
    """
    if df is None or len(df) < 1 or 'Close' not in df.columns:
        return None
    dates = _bar_dates(df.index)
    today = dates[-1] if dates else None
    if today is None:
        return None
    if base == '今開':
        if 'Open' not in df.columns:
            return None
        for i, d in enumerate(dates):
            if d == today:
                v = float(df['Open'].iloc[i])
                return v if v > 0 else None
        return None
    for i in range(len(dates) - 1, -1, -1):
        if dates[i] is not None and dates[i] != today:
            v = float(df['Close'].iloc[i])
            return v if v > 0 else None
    return None


def day_pct_change(df, base='昨收', use='盤中觸價', side='down'):
    """當日漲跌幅 %(負數=跌)。算不出來回 None。

    use='盤中觸價' → 拿**當日到目前為止**的最低價(side='down')或最高價
        (side='up')去比。這是「盤中跌幅超過 X%」的字面意思:今天只要曾經
        跌超過 X%,這件事就成立了,而不是「剛好收在 -X% 以下的那一根」。
        它是一個**狀態**,不是事件 —— 重複進場由既有的「每日進場上限」
        與「冷卻秒數」控制,不必在這裡再發明一套。
    use='收盤價' → 拿當根收盤價比。想要「收盤確認」的人用這個。

    兩種都只讀**已收盤的 K 棒**(呼叫端傳進來的就是 df_closed),
    所以回測與實盤看到的是同一件事,沒有未來函數。
    """
    ref = _day_ref_price(df, base)
    if ref is None or ref <= 0:
        return None
    if use == '收盤價':
        px = float(df['Close'].iloc[-1])
    else:
        col = 'Low' if side == 'down' else 'High'
        if col not in df.columns:
            return None
        dates = _bar_dates(df.index)
        today = dates[-1] if dates else None
        vals = [float(df[col].iloc[i]) for i, d in enumerate(dates) if d == today]
        if not vals:
            return None
        px = min(vals) if side == 'down' else max(vals)
    return (px / ref - 1.0) * 100.0


def _c_day_drop_over(df, p):
    """當日跌幅 >= X%。X 一律取絕對值,填 3 或 -3 都是「跌 3%」。

    刻意吃絕對值:既有的 pct_change_below 要求使用者「填負數」,那是個
    已知會踩到的坑(填 3 的話條件永遠成立,而且沒有任何提示)。
    使用者說的是「跌幅超過 X%」,X 在他腦中就是正數。
    """
    v = abs(float(p.get('value', 3.0)))
    chg = day_pct_change(df, p.get('base', '昨收'), p.get('use', '盤中觸價'), 'down')
    return chg is not None and chg <= -v


def _c_day_rise_over(df, p):
    """當日漲幅 >= Y%。Y 一律取絕對值。"""
    v = abs(float(p.get('value', 3.0)))
    chg = day_pct_change(df, p.get('base', '昨收'), p.get('use', '盤中觸價'), 'up')
    return chg is not None and chg >= v


def _c_rsi_cross_up(df, p):
    """RSI 由下往上「穿越」門檻 (只在穿越那一根成立,與 rsi_above 狀態不同)。"""
    r = _rsi(df['Close'], p.get('n', 14))
    thr = pd.Series([float(p.get('value', 50))] * len(r), index=r.index)
    return _cross_up(r, thr)

def _c_rsi_cross_down(df, p):
    r = _rsi(df['Close'], p.get('n', 14))
    thr = pd.Series([float(p.get('value', 50))] * len(r), index=r.index)
    return _cross_down(r, thr)

def _c_macd_hist_above_zero(df, p):
    """MACD 柱狀圖由負轉正 (動能翻多的那一根)。"""
    _, _, hist = _macd(df['Close'], p.get('f', 12), p.get('s', 26), p.get('sig', 9))
    zero = pd.Series([0.0] * len(hist), index=hist.index)
    return _cross_up(hist, zero)

def _c_macd_hist_below_zero(df, p):
    _, _, hist = _macd(df['Close'], p.get('f', 12), p.get('s', 26), p.get('sig', 9))
    zero = pd.Series([0.0] * len(hist), index=hist.index)
    return _cross_down(hist, zero)

def _c_bb_squeeze(df, p):
    """布林通道收窄 (帶寬 % 低於門檻) —— 突破前的盤整過濾。"""
    upper, mid, lower = _bb(df['Close'], p.get('n', 20), p.get('std', 2.0))
    if pd.isna(upper.iloc[-1]) or pd.isna(mid.iloc[-1]) or float(mid.iloc[-1]) == 0:
        return False
    width = (float(upper.iloc[-1]) - float(lower.iloc[-1])) / float(mid.iloc[-1]) * 100.0
    return width < float(p.get('value', 8.0))

def _c_bb_expand(df, p):
    """布林通道擴張 (帶寬 % 高於門檻) —— 波動放大。"""
    upper, mid, lower = _bb(df['Close'], p.get('n', 20), p.get('std', 2.0))
    if pd.isna(upper.iloc[-1]) or pd.isna(mid.iloc[-1]) or float(mid.iloc[-1]) == 0:
        return False
    width = (float(upper.iloc[-1]) - float(lower.iloc[-1])) / float(mid.iloc[-1]) * 100.0
    return width > float(p.get('value', 15.0))

def _c_kd_cross_up_low(df, p):
    """KD 低檔黃金交叉:K 上穿 D 且 K 值仍在門檻以下 (避免高檔追價)。"""
    k, d = _kd(df, p.get('n', 9), p.get('m1', 3), p.get('m2', 3))
    if not _cross_up(k, d):
        return False
    return (not pd.isna(k.iloc[-1])) and float(k.iloc[-1]) < float(p.get('value', 30))

def _c_kd_cross_down_high(df, p):
    k, d = _kd(df, p.get('n', 9), p.get('m1', 3), p.get('m2', 3))
    if not _cross_down(k, d):
        return False
    return (not pd.isna(k.iloc[-1])) and float(k.iloc[-1]) > float(p.get('value', 70))

def _c_always_true(df, p):
    """【ADR-059/061】永遠成立 (每根K棒都成立)。
    搭配「買進後持有不賣」時 = 每根K棒都買一次 = 定期定額累積。
    若只想買一次,請改用有明確觸發時機的條件 (例如均線交叉),
    或改用會出場的一般策略。"""
    return True


def _c_inside_bar(df, p):
    """內包K棒 (本根高低都被前一根包住) —— 盤整/變盤前兆。"""
    if len(df) < 2:
        return False
    return (float(df['High'].iloc[-1]) <= float(df['High'].iloc[-2])
            and float(df['Low'].iloc[-1]) >= float(df['Low'].iloc[-2]))

def _c_gap_up(df, p):
    """跳空上漲:本根開盤 > 前一根最高 × (1 + 門檻%)。"""
    if len(df) < 2 or float(df['High'].iloc[-2]) == 0:
        return False
    thr = float(p.get('value', 0.0)) / 100.0
    return float(df['Open'].iloc[-1]) > float(df['High'].iloc[-2]) * (1.0 + thr)

def _c_gap_down(df, p):
    if len(df) < 2 or float(df['Low'].iloc[-2]) == 0:
        return False
    thr = float(p.get('value', 0.0)) / 100.0
    return float(df['Open'].iloc[-1]) < float(df['Low'].iloc[-2]) * (1.0 - thr)


# ============================================================
# 【ADR-101】籌碼條件
#
# 資料來源:core/chips_features.attach_chips() 在資料準備階段把籌碼併成
# df 的 Chip* 欄位。**T 日預設只讀得到 T-1 日(含)以前的籌碼** (籌碼盤後
# 才公布,詳見 chips_features 模組 docstring 的未來函數說明)。
#
# 沒有籌碼資料時,series_of() 會拋 ChipDataMissing;eval_conditions 的
# try/except 會把訊息收進 errors,GUI 就能提示「請先更新籌碼」,而不是讓
# 使用者對著永遠不成立的條件猜原因。這是刻意的:「沒資料」與「條件沒到」
# 對使用者的意義完全不同,不可混為一談。
#
# 個股籌碼原始單位是股,使用者參數用「張」,比較時乘 SHARES_PER_LOT。
# ============================================================
def _c_chip_foreign_buy_streak(df, p):
    return _cf.consecutive_net(_cf.series_of(df, _cf.COL_FOREIGN),
                               int(p.get('n', 3)), positive=True)


def _c_chip_foreign_sell_streak(df, p):
    return _cf.consecutive_net(_cf.series_of(df, _cf.COL_FOREIGN),
                               int(p.get('n', 3)), positive=False)


def _c_chip_trust_buy_streak(df, p):
    return _cf.consecutive_net(_cf.series_of(df, _cf.COL_TRUST),
                               int(p.get('n', 3)), positive=True)


def _c_chip_trust_sell_streak(df, p):
    return _cf.consecutive_net(_cf.series_of(df, _cf.COL_TRUST),
                               int(p.get('n', 3)), positive=False)


def _c_chip_inst_buy_over(df, p):
    """三大法人合計買超 ≥ X 張。"""
    s = _cf.series_of(df, _cf.COL_INST_TOTAL)
    v = s.iloc[-1]
    if pd.isna(v):
        return False
    return float(v) >= float(p.get('value', 500)) * _cf.SHARES_PER_LOT


def _c_chip_inst_sell_over(df, p):
    """三大法人合計賣超 ≤ X 張 (參數填負數,例如 -500)。"""
    s = _cf.series_of(df, _cf.COL_INST_TOTAL)
    v = s.iloc[-1]
    if pd.isna(v):
        return False
    return float(v) <= float(p.get('value', -500)) * _cf.SHARES_PER_LOT


def _c_chip_foreign_turn_buy(df, p):
    return _cf.turned_positive(_cf.series_of(df, _cf.COL_FOREIGN))


def _c_chip_foreign_turn_sell(df, p):
    return _cf.turned_negative(_cf.series_of(df, _cf.COL_FOREIGN))


def _c_chip_foreign_vol_ratio(df, p):
    """外資買超佔當根成交量 ≥ X%。

    用相對比例而非絕對張數,避免大型股與中小型股用同一個門檻失真
    (外資買 500 張對台積電是雜訊,對小型股可能是關鍵買盤)。
    """
    s = _cf.series_of(df, _cf.COL_FOREIGN)
    if 'Volume' not in df.columns or len(df) == 0:
        return False
    fv, vol = s.iloc[-1], pd.to_numeric(df['Volume'], errors='coerce').iloc[-1]
    if pd.isna(fv) or pd.isna(vol) or float(vol) <= 0:
        return False
    return (float(fv) / float(vol)) * 100.0 >= float(p.get('value', 10.0))


def _c_chip_margin_decrease_streak(df, p):
    """融資餘額連續 N 日減少 (散戶退場,籌碼趨於安定)。"""
    return _cf.consecutive_decreasing(_cf.series_of(df, _cf.COL_MARGIN), int(p.get('n', 3)))


def _c_chip_margin_increase_streak(df, p):
    """融資餘額連續 N 日增加 (散戶進場,籌碼較亂)。"""
    return _cf.consecutive_increasing(_cf.series_of(df, _cf.COL_MARGIN), int(p.get('n', 3)))


def _c_chip_fut_foreign_oi_above(df, p):
    """期貨外資未平倉淨額 ≥ X 口 (正=淨多)。"""
    s = _cf.series_of(df, _cf.COL_FUT_FOREIGN_OI)
    v = s.iloc[-1]
    if pd.isna(v):
        return False
    return float(v) >= float(p.get('value', 0))


def _c_chip_fut_foreign_oi_below(df, p):
    """期貨外資未平倉淨額 ≤ X 口 (負=淨空)。"""
    s = _cf.series_of(df, _cf.COL_FUT_FOREIGN_OI)
    v = s.iloc[-1]
    if pd.isna(v):
        return False
    return float(v) <= float(p.get('value', 0))


def _c_chip_fut_foreign_turn_long(df, p):
    """期貨外資未平倉由淨空翻淨多 (大盤轉折訊號)。"""
    return _cf.turned_positive(_cf.series_of(df, _cf.COL_FUT_FOREIGN_OI))


def _c_chip_fut_foreign_turn_short(df, p):
    """期貨外資未平倉由淨多翻淨空。"""
    return _cf.turned_negative(_cf.series_of(df, _cf.COL_FUT_FOREIGN_OI))


# 籌碼條件的 id 集合:GUI 用它判斷「這個策略有沒有用到籌碼」,
# 進而決定要不要載入籌碼資料、要不要擋下分K週期。
CHIP_CONDITION_IDS = {
    'chip_foreign_buy_streak', 'chip_foreign_sell_streak',
    'chip_trust_buy_streak', 'chip_trust_sell_streak',
    'chip_inst_buy_over', 'chip_inst_sell_over',
    'chip_foreign_turn_buy', 'chip_foreign_turn_sell',
    'chip_foreign_vol_ratio',
    'chip_margin_decrease_streak', 'chip_margin_increase_streak',
    'chip_fut_foreign_oi_above', 'chip_fut_foreign_oi_below',
    'chip_fut_foreign_turn_long', 'chip_fut_foreign_turn_short',
}


def strategy_uses_chips(strategy):
    """這個策略的進出場條件裡有沒有用到籌碼條件。"""
    if not isinstance(strategy, dict):
        return False
    for key in ('entry', 'exit_signals'):
        for c in (strategy.get(key) or []):
            if isinstance(c, dict) and c.get('type') in CHIP_CONDITION_IDS:
                return True
    return False


CONDITIONS = {
    'ma_cross_up':     ("均線黃金交叉 (快線上穿慢線)", [('fast', '快線期間', 5), ('slow', '慢線期間', 20)], _c_ma_cross_up),
    'ma_cross_down':   ("均線死亡交叉 (快線下穿慢線)", [('fast', '快線期間', 5), ('slow', '慢線期間', 20)], _c_ma_cross_down),
    'price_break_high': ("收盤突破前N期最高", [('n', 'N期', 20)], _c_price_break_high),
    'price_break_low':  ("收盤跌破前N期最低", [('n', 'N期', 20)], _c_price_break_low),
    'price_above':     ("收盤價高於指定價位", [('value', '價位', 0)], _c_price_above),
    'price_below':     ("收盤價低於指定價位", [('value', '價位', 0)], _c_price_below),
    'bb_cross_upper':  ("收盤上穿布林上軌", [('n', '期間', 20), ('std', '標準差', 2.0)], _c_bb_cross_upper),
    'bb_cross_lower':  ("收盤下穿布林下軌", [('n', '期間', 20), ('std', '標準差', 2.0)], _c_bb_cross_lower),
    'kd_cross_up':     ("KD黃金交叉 (K上穿D)", [('n', 'RSV期間', 9), ('m1', 'K平滑', 3), ('m2', 'D平滑', 3)], _c_kd_cross_up),
    'kd_cross_down':   ("KD死亡交叉 (K下穿D)", [('n', 'RSV期間', 9), ('m1', 'K平滑', 3), ('m2', 'D平滑', 3)], _c_kd_cross_down),
    'kd_above':        ("K值高於門檻 (超買)", [('n', 'RSV期間', 9), ('value', '門檻', 80)], _c_kd_above),
    'kd_below':        ("K值低於門檻 (超賣)", [('n', 'RSV期間', 9), ('value', '門檻', 20)], _c_kd_below),
    'macd_cross_up':   ("MACD黃金交叉 (DIF上穿Signal)", [('f', '快線', 12), ('s', '慢線', 26), ('sig', '訊號', 9)], _c_macd_cross_up),
    'macd_cross_down': ("MACD死亡交叉 (DIF下穿Signal)", [('f', '快線', 12), ('s', '慢線', 26), ('sig', '訊號', 9)], _c_macd_cross_down),
    'rsi_above':       ("RSI高於門檻", [('n', '期間', 14), ('value', '門檻', 70)], _c_rsi_above),
    'rsi_below':       ("RSI低於門檻", [('n', '期間', 14), ('value', '門檻', 30)], _c_rsi_below),

    # ---- 【ADR-057】以下為新增條件 ----
    # 參數規格第 4 欄 (可省略) = 下拉選單的候選值;GUI 看到就渲染成 Combobox,
    # 沒有第 4 欄就照舊渲染成可自由輸入的文字框。
    'price_cross_up_ma':   ("收盤價突破均線 (上穿)", [('n', '均線期間', 20), ('kind', '均線型態', 'SMA', ['SMA', 'EMA'])], _c_price_cross_up_ma),
    'price_cross_down_ma': ("收盤價跌破均線 (下穿)", [('n', '均線期間', 20), ('kind', '均線型態', 'SMA', ['SMA', 'EMA'])], _c_price_cross_down_ma),
    'price_above_ma':      ("收盤價站上均線 (狀態,可當過濾)", [('n', '均線期間', 20), ('kind', '均線型態', 'SMA', ['SMA', 'EMA'])], _c_price_above_ma),
    'price_below_ma':      ("收盤價位於均線之下 (狀態)", [('n', '均線期間', 20), ('kind', '均線型態', 'SMA', ['SMA', 'EMA'])], _c_price_below_ma),
    'ma_slope_up':         ("均線上彎 (趨勢向上)", [('n', '均線期間', 20), ('look', '比較幾根前', 3), ('kind', '均線型態', 'SMA', ['SMA', 'EMA'])], _c_ma_slope_up),
    'ma_slope_down':       ("均線下彎 (趨勢向下)", [('n', '均線期間', 20), ('look', '比較幾根前', 3), ('kind', '均線型態', 'SMA', ['SMA', 'EMA'])], _c_ma_slope_down),
    'ma_align_bull':       ("均線多頭排列 (短>中>長)", [('short', '短均', 5), ('mid', '中均', 20), ('long', '長均', 60), ('kind', '均線型態', 'SMA', ['SMA', 'EMA'])], _c_ma_align_bull),
    'ma_align_bear':       ("均線空頭排列 (短<中<長)", [('short', '短均', 5), ('mid', '中均', 20), ('long', '長均', 60), ('kind', '均線型態', 'SMA', ['SMA', 'EMA'])], _c_ma_align_bear),
    'volume_above_ma':     ("成交量放大 (>N日均量×倍數)", [('n', '均量期間', 20), ('mult', '倍數', 1.5)], _c_volume_above_ma),
    'volume_below_ma':     ("成交量萎縮 (<N日均量×倍數)", [('n', '均量期間', 20), ('mult', '倍數', 0.7)], _c_volume_below_ma),
    'consecutive_up':      ("連續N根收紅", [('n', '根數', 3)], _c_consecutive_up),
    'consecutive_down':    ("連續N根收黑", [('n', '根數', 3)], _c_consecutive_down),
    'pct_change_above':    ("單根漲幅≥指定%", [('value', '漲幅%', 3.0)], _c_pct_change_above),
    'pct_change_below':    ("單根跌幅≤指定% (填負數)", [('value', '跌幅%', -3.0)], _c_pct_change_below),
    'rsi_cross_up':        ("RSI上穿門檻 (穿越那根)", [('n', '期間', 14), ('value', '門檻', 50)], _c_rsi_cross_up),
    'rsi_cross_down':      ("RSI下穿門檻 (穿越那根)", [('n', '期間', 14), ('value', '門檻', 50)], _c_rsi_cross_down),
    'macd_hist_above_zero': ("MACD柱由負轉正", [('f', '快線', 12), ('s', '慢線', 26), ('sig', '訊號', 9)], _c_macd_hist_above_zero),
    'macd_hist_below_zero': ("MACD柱由正轉負", [('f', '快線', 12), ('s', '慢線', 26), ('sig', '訊號', 9)], _c_macd_hist_below_zero),
    'bb_squeeze':          ("布林通道收窄 (帶寬%<門檻)", [('n', '期間', 20), ('std', '標準差', 2.0), ('value', '帶寬%門檻', 8.0)], _c_bb_squeeze),
    'bb_expand':           ("布林通道擴張 (帶寬%>門檻)", [('n', '期間', 20), ('std', '標準差', 2.0), ('value', '帶寬%門檻', 15.0)], _c_bb_expand),
    'kd_cross_up_low':     ("KD低檔黃金交叉 (K<門檻)", [('n', 'RSV期間', 9), ('m1', 'K平滑', 3), ('m2', 'D平滑', 3), ('value', 'K值上限', 30)], _c_kd_cross_up_low),
    'kd_cross_down_high':  ("KD高檔死亡交叉 (K>門檻)", [('n', 'RSV期間', 9), ('m1', 'K平滑', 3), ('m2', 'D平滑', 3), ('value', 'K值下限', 70)], _c_kd_cross_down_high),
    'always_true':         ("永遠成立 (每根都成立;配合買進持有=定期定額)", [], _c_always_true),
    'inside_bar':          ("內包K棒 (高低被前根包住)", [], _c_inside_bar),
    'gap_up':              ("跳空上漲 (開盤>前根最高)", [('value', '額外門檻%', 0.0)], _c_gap_up),
    'gap_down':            ("跳空下跌 (開盤<前根最低)", [('value', '額外門檻%', 0.0)], _c_gap_down),

    # ---- 【ADR-143】當日(盤中)漲跌幅:逆勢的「跌深買進 / 漲多賣出」 ----
    # 與 pct_change_above/below 不同 —— 那兩個是「單根相對前一根」,
    # 這兩個是「當日相對昨收」,也就是台股講漲跌幅的那個數字。
    'day_drop_over':       ("當日跌幅≥X% (盤中,相對昨收) —— 跌深買進",
                            [('value', '跌幅%(填正數)', 3.0),
                             ('base', '參考價', '昨收', list(DAY_PCT_BASES)),
                             ('use', '用哪個價', '盤中觸價', list(DAY_PCT_USES))],
                            _c_day_drop_over),
    'day_rise_over':       ("當日漲幅≥Y% (盤中,相對昨收) —— 漲多賣出",
                            [('value', '漲幅%(填正數)', 3.0),
                             ('base', '參考價', '昨收', list(DAY_PCT_BASES)),
                             ('use', '用哪個價', '盤中觸價', list(DAY_PCT_USES))],
                            _c_day_rise_over),

    # ---- 【ADR-101】籌碼條件 (僅日K以上;T日預設只讀得到T-1日以前的籌碼) ----
    'chip_foreign_buy_streak':  ("[籌碼] 外資連續買超N日", [('n', '連續日數', 3)], _c_chip_foreign_buy_streak),
    'chip_foreign_sell_streak': ("[籌碼] 外資連續賣超N日", [('n', '連續日數', 3)], _c_chip_foreign_sell_streak),
    'chip_trust_buy_streak':    ("[籌碼] 投信連續買超N日", [('n', '連續日數', 3)], _c_chip_trust_buy_streak),
    'chip_trust_sell_streak':   ("[籌碼] 投信連續賣超N日", [('n', '連續日數', 3)], _c_chip_trust_sell_streak),
    'chip_inst_buy_over':       ("[籌碼] 三大法人買超≥X張", [('value', '張數門檻', 500)], _c_chip_inst_buy_over),
    'chip_inst_sell_over':      ("[籌碼] 三大法人賣超≤X張 (填負數)", [('value', '張數門檻', -500)], _c_chip_inst_sell_over),
    'chip_foreign_turn_buy':    ("[籌碼] 外資由賣轉買 (翻多那日)", [], _c_chip_foreign_turn_buy),
    'chip_foreign_turn_sell':   ("[籌碼] 外資由買轉賣 (翻空那日)", [], _c_chip_foreign_turn_sell),
    'chip_foreign_vol_ratio':   ("[籌碼] 外資買超佔成交量≥X%", [('value', '佔比%', 10.0)], _c_chip_foreign_vol_ratio),
    'chip_margin_decrease_streak': ("[籌碼] 融資餘額連續N日減少 (散戶退場)", [('n', '連續日數', 3)], _c_chip_margin_decrease_streak),
    'chip_margin_increase_streak': ("[籌碼] 融資餘額連續N日增加 (散戶進場)", [('n', '連續日數', 3)], _c_chip_margin_increase_streak),
    'chip_fut_foreign_oi_above':   ("[籌碼] 期貨外資未平倉≥X口 (淨多)", [('value', '口數門檻', 0)], _c_chip_fut_foreign_oi_above),
    'chip_fut_foreign_oi_below':   ("[籌碼] 期貨外資未平倉≤X口 (淨空)", [('value', '口數門檻', 0)], _c_chip_fut_foreign_oi_below),
    'chip_fut_foreign_turn_long':  ("[籌碼] 期貨外資未平倉由空翻多", [], _c_chip_fut_foreign_turn_long),
    'chip_fut_foreign_turn_short': ("[籌碼] 期貨外資未平倉由多翻空", [], _c_chip_fut_foreign_turn_short),
}

def spec_parts(item):
    """【ADR-057】把參數規格項正規化成 (key, label, default, choices)。
    舊格式是 3 元素 (key, label, default);新增下拉選單的條件會多帶第 4 個
    元素 choices (候選值 list)。這個小函式讓所有讀 spec 的地方 (標籤產生、
    GUI 渲染、參數收集) 都不必各自寫 len() 判斷。"""
    if len(item) >= 4:
        return item[0], item[1], item[2], item[3]
    return item[0], item[1], item[2], None


def condition_label(cond):
    """把一個條件 dict 轉成可讀文字,例如「均線黃金交叉 (fast=5, slow=20)」。"""
    meta = CONDITIONS.get(cond.get('type'))
    if not meta:
        return str(cond.get('type', '?'))
    name, spec, _ = meta
    p = cond.get('params', {})
    if not spec:
        return name  # 無參數條件 (例如「內包K棒」) 不要印出空括號
    detail = ", ".join(f"{lab}={p.get(k, dv)}" for k, lab, dv, _ch in (spec_parts(x) for x in spec))
    return f"{name} ({detail})"


# ============================================================
# 策略資料結構
# ============================================================

def new_strategy():
    """回傳一份帶安全預設值的策略:預設模擬模式、預設未啟用。"""
    return {
        'id': uuid.uuid4().hex[:10],
        'name': '',
        'symbol': '',
        'market': '台股',            # 台股 / 台期貨 (由 trade_type 推導,保留相容)
        'trade_type': '股票',        # 【ADR-043】股票 / 零股 / 期貨 —— 交易種類 (防交易錯誤)
        'timeframe': '5分K',
        'direction': '做多',          # 做多 / 做空 (做空僅限期貨)
        'qty': 1,                     # 單位依 trade_type:股票=張, 零股=股, 期貨=口
        'slippage_ticks': 2,          # 限價單相對收盤價讓價的檔數 (提高成交率)
        'price_type': '限價',         # 【新ADR】限價/市價/範圍市價 (範圍市價僅期貨);
                                       # 限價才會套用 slippage_ticks 讓價,市價/範圍市價
                                       # 直接以0價送出 (依市場當下成交,不受讓價檔數影響)
        'entry': [],                  # 進場條件 list (全部成立才進場, AND)
        'exit_signals': [],           # 出場訊號 list (任一成立即出場, OR)
        'stop_loss_pct': 2.0,         # 停損 % (0=停用)
        'take_profit_pct': 0.0,       # 停利 % (0=停用)
        'stop_loss_abs': 0.0,         # 【ADR-043】停損 絕對價格(股票)/點數(期貨) (0=停用)
        'take_profit_abs': 0.0,       # 【ADR-043】停利 絕對價格/點數 (0=停用)
        'max_trades_per_day': 3,      # 每日最多進場次數
        'cooldown_sec': 300,          # 兩次下單間最短間隔 (秒)
        'daily_loss_limit': 0.0,      # 每日虧損熔斷 (價差×數量, 0=停用)
        'mode': '模擬',               # 模擬 / 實單
        'account_id': 'default',      # 【新ADR 多帳戶】模擬成交要記進哪個模擬帳戶
                                       # (帳戶容器由 GUI 層維護,這裡只存 id 不存帳戶
                                       # 本身;'default' 對應遷移舊版單一帳戶檔的那個)
        'enabled': False,
        # 【ADR-070】交易時段閘門:session_gate=True 時,非交易時間自動待命,
        # 進入盤中才評估下單 (無需人工開啟);futures_session 決定期貨要不要含夜盤。
        'session_gate': True,         # True=非交易時間不動作 (建議);False=只看K棒邊界不看時段
        # 【ADR-101】籌碼未來函數開關。False (預設) = T 日只讀得到 T-1 日(含)
        # 以前的籌碼,與實盤可取得的資訊一致;True 僅供研究,回測會虛高。
        'chips_allow_same_day': False,
        'futures_session': 'day_night',  # 'day'=只做日盤 08:45-13:45;'day_night'=含夜盤 15:00-次日05:00
        # 【ADR-136】停損停利即時觸發 (不等K棒收盤)。新策略預設開啟 ——
        # 使用者明確要這個功能。舊策略沒有這個 key,由 intrabar_stop_enabled()
        # 依商品別沿用它原本的行為 (期貨 True / 其他 False),不會被悄悄改掉。
        'intrabar_stop': True,
        # 【ADR-074 看A做B】訊號來源 (A) 與執行商品 (B) 分離。watch_enabled=False 時
        # A=B (看自己做自己,一般模式)。A 可為指數 (加權/櫃買/台指期等,各項商品皆可);
        # B 為上面的 symbol/trade_type,不可為加權/櫃買指數 (指數不能下單)。
        # 週期也分離:條件看 watch_timeframe (A);下單價/邊界節奏依 A 的訊號 K 棒,
        # 執行價取 B 的最新收盤 (B 的 timeframe)。
        'watch_enabled': False,
        'watch_symbol': '',           # A 的商品代碼 (空=沿用 symbol)
        'watch_trade_type': '指數',    # A 的種類:股票 / 期貨 / 指數
        'watch_timeframe': '30分K',    # A 的訊號週期

        # 【ADR-059】買進後持有不賣 (Buy & Hold):當作比較基準用,只能回測/模擬。
        # 勾選後 validate_strategy 不再要求「至少一種出場方式」。
        'buy_and_hold': False,
        # 【ADR-062】買進持有的模式與定期定額參數
        'bnh_mode': 'accumulate',     # single / accumulate / dca
        'dca_amount': 10000.0,        # 定期定額:每期投入金額
        'dca_interval': 'month',      # bar / week / month / quarter
    }


# 【ADR-043】交易種類 → (市場, 數量單位, 是否零股, 是否期貨)
TRADE_TYPES = ('股票', '零股', '期貨')

def trade_type_of(strategy):
    """相容舊策略:沒有 trade_type 的用 market 推導 (台期貨→期貨, 否則→股票)。"""
    tt = strategy.get('trade_type')
    if tt in TRADE_TYPES:
        return tt
    return '期貨' if strategy.get('market') == '台期貨' else '股票'

def qty_unit_of(strategy):
    return {'股票': '張', '零股': '股', '期貨': '口'}[trade_type_of(strategy)]


def account_id_of(strategy):
    """【新ADR 多帳戶】相容舊策略:沒有 account_id 的一律回傳 'default'
    (對應遷移舊版單一帳戶檔案時建立的那個帳戶)。"""
    aid = str(strategy.get('account_id', '') or '').strip()
    return aid or 'default'

def unit_size(strategy):
    """【ADR-062】一「張/口」等於多少股或多少點乘數 —— 金額換算的共用來源。

    股票 1 張 = 1000 股;零股 = 1 股;期貨 = 契約乘數 (TXF=200/MXF=50/TMF=10)。
    原本只寫在 core/backtest.py 裡,定期定額要把「金額」換算成「張數」時
    也需要同一個數字;各寫一份遲早分歧 (P-67 的教訓),故抽成共用函式。
    期貨乘數查詢失敗時退回 1.0 並不拋例外 —— 呼叫端至少還能跑,
    數字會偏但不會整個掛掉。
    """
    tt = trade_type_of(strategy)
    if tt == '股票':
        return 1000.0
    if tt == '零股':
        return 1.0
    try:
        from . import paper_account as _pa
        mult, _note = _pa._fut_multiplier(str(strategy.get('symbol', '')).upper())
        return float(mult)
    except Exception:
        return 1.0


# 【ADR-062】買進持有的三種模式 (使用者需求:三種都要,才能互相比較)
BNH_MODES = ('single', 'accumulate', 'dca')
BNH_MODE_LABELS = {
    'single': '單筆長抱 (只買一次,之後不再進場)',
    'accumulate': '累積加碼 (每次條件成立就再買固定數量)',
    'dca': '定期定額 (每期投入固定金額,數量隨價格變動)',
}
DCA_INTERVALS = ('bar', 'week', 'month', 'quarter')
DCA_INTERVAL_LABELS = {
    'bar': '每根K棒', 'week': '每週', 'month': '每月', 'quarter': '每季',
}


def _dca_period_key(ts, interval):
    """把時間戳歸到「投入週期」的代表值;同一週期只投入一次。"""
    iv = str(interval)
    if iv == 'bar':
        return str(ts)
    if iv == 'week':
        y, w, _ = ts.isocalendar()
        return f"{y}-W{w}"
    if iv == 'quarter':
        return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"
    return f"{ts.year}-{ts.month:02d}"   # month (預設)


def is_futures(strategy):
    return trade_type_of(strategy) == '期貨'


# 【新ADR】委託價格類型:限價/市價/範圍市價。範圍市價 (TAIFEX的MKP) 只有
# 期貨才有這個委託方式,股票交易所規則裡沒有這個選項。
# 【鐵則6】零股 (IntradayOdd) 依交易所規則只能限價ROD,市價/範圍市價 一律不可,
# 這是既有的硬性規定 (core/order_rules.py),不因本次新增委託方式選項而放寬。
PRICE_TYPES = ('限價', '市價', '範圍市價')
FUTURES_ONLY_PRICE_TYPES = ('範圍市價',)


def price_type_of(strategy):
    """讀策略的委託價格類型,沒設定 (舊策略相容) 或不合法值一律退回「限價」
    (=既有行為,永遠安全的預設)。零股一律強制限價 (鐵則6,交易所規則不可放寬);
    範圍市價只對期貨有意義,非期貨策略若存了「範圍市價」(理論上 validate_strategy
    會擋,這裡防呆再擋一次) 一樣退回限價。"""
    if trade_type_of(strategy) == '零股':
        return '限價'
    pt = strategy.get('price_type', '限價')
    if pt not in PRICE_TYPES:
        return '限價'
    if pt in FUTURES_ONLY_PRICE_TYPES and not is_futures(strategy):
        return '限價'
    return pt


def validate_price_type(strategy):
    """【ADR-135】委託方式的規則,回傳 (ok, 原因)。

    抽出來的理由:自訂策略的編輯器也要用同一套規則檢查 (ADR-135 讓自訂策略
    可以選委託方式)。兩份各自維護遲早分歧,而這一份的依據是**交易所規則**
    (範圍市價僅期貨、零股只能限價/鐵則6) —— 分歧的後果是送出會被退的委託。
    """
    tt = trade_type_of(strategy)
    pt = (strategy or {}).get('price_type', '限價')
    if pt not in PRICE_TYPES:
        return False, f"委託方式僅支援 {'/'.join(PRICE_TYPES)}"
    if pt in FUTURES_ONLY_PRICE_TYPES and tt != '期貨':
        return False, "範圍市價僅期貨支援 (股票/零股交易所規則沒有這個委託方式)"
    if tt == '零股' and pt != '限價':
        return False, "零股依交易所規則只能限價 (鐵則6,不可用市價/範圍市價)"
    return True, ""


# 【ADR-074 看A做B】A (訊號來源) 可選的種類:比交易種類多一個「指數」。
WATCH_TRADE_TYPES = ('股票', '期貨', '指數')


def looks_like_index_symbol(sym):
    """代碼看起來像不像指數 (加權/櫃買等)。^ 開頭 (^TWII/^TWOII) 或常見指數代號。"""
    s = str(sym or '').strip().upper()
    if not s:
        return False
    if s.startswith('^'):
        return True
    # 【ADR-114】1.7 起指數改用交易所代碼 (加權 IX0001/櫃買 IX0002),
    # 清單集中在 core/sj_compat.py,新舊代碼都要認得——只認舊代碼的話,
    # 升級後策略會判斷「這不是指數」而走錯解析路徑。
    return s in _sjc.INDEX_SYMBOLS


def watch_enabled(strategy):
    """是否啟用「看A做B」。未啟用時 A=B (看自己做自己)。"""
    return bool(strategy.get('watch_enabled'))


def watch_symbol_of(strategy):
    """A (訊號來源) 的代碼;啟用看A做B 但沒填就退回 symbol。"""
    if not watch_enabled(strategy):
        return str(strategy.get('symbol', '')).strip().upper()
    w = str(strategy.get('watch_symbol', '')).strip().upper()
    return w or str(strategy.get('symbol', '')).strip().upper()


def watch_trade_type_of(strategy):
    """A 的種類 (股票/期貨/指數);未啟用看A做B 就等於執行種類。"""
    if not watch_enabled(strategy):
        return trade_type_of(strategy)
    tt = strategy.get('watch_trade_type')
    return tt if tt in WATCH_TRADE_TYPES else '指數'


def watch_timeframe_of(strategy):
    """A 的訊號週期;未啟用或沒填就等於執行週期。

    【ADR-128】DAY_SIGNAL_KINDS 的種類一律回 DAY_SIGNAL_TIMEFRAME,忽略存檔值
    —— 那些策略的訊號週期是結構上固定的,存檔裡的殘留值 (終極波段舊版是 '5分K')
    不代表訊號真的看那個週期。這個覆寫同時決定了 _quant_eval_pass 的邊界節拍,
    所以「介面回答真話」與「節拍正確」是同一件事。
    """
    if str((strategy or {}).get('kind') or '') in DAY_SIGNAL_KINDS:
        return DAY_SIGNAL_TIMEFRAME
    if not watch_enabled(strategy):
        return strategy.get('timeframe', '5分K')
    return strategy.get('watch_timeframe') or strategy.get('timeframe', '5分K')

def new_runtime():
    return {
        'state': 'FLAT',              # FLAT / LONG / SHORT
        'entry_price': 0.0,
        'exec_entry_price': 0.0,      # 【新ADR】實際下單商品(看A做B時是B)的成交均價,
                                       # 給期貨即時停損/停利用 (entry_price 是A的價,
                                       # 看A做B時跟B不同尺度,不能拿來跟B的即時價比較)
        'qty': 0,
        'last_bar_ts': '',            # 同一根K棒不重複評估的閘門
        'trades_today': 0,
        'day': '',
        'last_order_ts': 0.0,
        'realized_pnl_today': 0.0,    # 價差×數量 (期貨未乘契約乘數)
        'error_count': 0,
        'condition_errors': [],       # 【ADR-065】[(條件標籤, 例外訊息), ...];最近一次評估時哪些條件拋了例外
        'time_window_skips': [],      # 【ADR-066】最近一次評估時,有哪些 intent 被進/出場時間窗設定排除
    }

VALID_TIMEFRAMES = ("1分K", "5分K", "15分K", "30分K", "60分K", "日K", "周K", "月K")

_CUSTOM_TIMEFRAME_RE = re.compile(r'^(\d+)(分|時)K$')

def timeframe_minutes(tf):
    """【新增 自訂週期】把週期字串換算成分鐘數:涵蓋 VALID_TIMEFRAMES 既有的
    「N分K」預設值,也涵蓋使用者自訂的任意「N分K」/「N時K」(N為正整數)——
    後者統一先換算成分鐘 (N時K = N*60分K),盤中 resample 本來就是以分鐘為
    最小單位,不需要另外處理小時級的 resample 規則。日K/周K/月K 或無法
    解析回傳 None (呼叫端用這個分辨「這是日以上的週期,還是分鐘週期」)。"""
    m = _CUSTOM_TIMEFRAME_RE.match(str(tf or ''))
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    return n * 60 if m.group(2) == '時' else n

def is_valid_timeframe(tf):
    """【新增 自訂週期】取代舊版「只能是 VALID_TIMEFRAMES 裡的固定字串」——
    日K/周K/月K,或任何正整數的自訂 N分K/N時K (含 VALID_TIMEFRAMES 既有的
    預設分鐘週期,本身就符合 N分K 格式),都視為合法週期。"""
    if tf in ('日K', '周K', '月K'):
        return True
    return timeframe_minutes(tf) is not None

def validate_strategy(s):
    """存檔/啟用前的完整驗證。回傳 (ok, 錯誤訊息)。"""
    if not str(s.get('name', '')).strip():
        return False, "策略名稱不可空白"
    if not str(s.get('symbol', '')).strip():
        return False, "商品代碼不可空白"
    tt = trade_type_of(s)
    if tt not in TRADE_TYPES:
        return False, "交易種類僅支援 股票 / 零股 / 期貨"
    ok, why = validate_price_type(s)
    if not ok:
        return False, why
    if not is_valid_timeframe(s.get('timeframe')):
        return False, f"週期格式不正確 (可選 {'/'.join(VALID_TIMEFRAMES)},或自訂 N分K/N時K,例如 45分K、3時K)"
    if s.get('direction') not in ('做多', '做空'):
        return False, "方向僅支援 做多 / 做空"
    if s.get('direction') == '做空' and tt != '期貨':
        return False, "做空僅支援期貨 (股票/零股融券暫不在自動交易範圍)"
    try:
        q = int(s.get('qty', 0))
        if q <= 0:
            return False, "數量必須為正整數"
        if q > 100:
            return False, "單筆數量超過 100 (防呆上限),請確認後調整程式碼上限"
    except (TypeError, ValueError):
        return False, "數量必須為正整數"
    if not s.get('entry'):
        return False, "至少要有一個進場條件"
    for c in list(s.get('entry', [])) + list(s.get('exit_signals', [])):
        if c.get('type') not in CONDITIONS:
            return False, f"未知的條件類型: {c.get('type')}"
    # 【ADR-101】籌碼是日頻資料 (盤後公布),分K 策略用它意義不大:當日所有
    # 分K 只會讀到同一筆前一交易日的值。與其讓使用者以為「分K 也有籌碼訊號」,
    # 不如在存檔時就擋下並說明原因。
    if strategy_uses_chips(s) and not _cf.timeframe_supports_chips(s.get('timeframe')):
        return False, (f"籌碼條件只能用於 {'/'.join(_cf.CHIP_ALLOWED_TIMEFRAMES)} "
                       f"(目前週期:{s.get('timeframe')})。籌碼是每日盤後公布的日頻資料,"
                       "分K 策略的當日每一根都只會讀到同一筆前一交易日的值,沒有意義。")
    sl = float(s.get('stop_loss_pct', 0) or 0)
    tp = float(s.get('take_profit_pct', 0) or 0)
    sl_abs = float(s.get('stop_loss_abs', 0) or 0)
    tp_abs = float(s.get('take_profit_abs', 0) or 0)
    if sl < 0 or tp < 0 or sl_abs < 0 or tp_abs < 0:
        return False, "停損/停利 不可為負"
    has_exit = bool(s.get('exit_signals')) or sl > 0 or tp > 0 or sl_abs > 0 or tp_abs > 0
    # 【ADR-059】「買進後持有不賣 (Buy & Hold)」是刻意要沒有出場的:使用者要拿它
    # 當基準線,跟主動策略比較績效與交易成本。勾了這個旗標就放行,但兩者不可
    # 並存 —— 既然標明永不出場,又設停損/停利/出場訊號會自相矛盾,寧可擋下來
    # 要使用者自己想清楚,也不要讓他以為在跑 Buy & Hold 其實中途被停損出場了。
    if s.get('buy_and_hold'):
        mode = str(s.get('bnh_mode', 'accumulate'))
        if mode not in BNH_MODES:
            return False, f"買進持有模式僅支援 {'/'.join(BNH_MODES)}"
        if mode == 'dca':
            try:
                amt = float(s.get('dca_amount', 0) or 0)
            except (TypeError, ValueError):
                return False, "定期定額的每期投入金額必須是數字"
            if amt <= 0:
                return False, "定期定額的每期投入金額必須大於 0"
            if str(s.get('dca_interval', 'month')) not in DCA_INTERVALS:
                return False, f"定期定額週期僅支援 {'/'.join(DCA_INTERVALS)}"
        if has_exit:
            return False, ("已勾選「買進後持有不賣」,就不可以同時設定停損/停利/出場訊號 "
                           "(兩者矛盾)。請擇一:要嘛取消勾選,要嘛把停損停利歸零並清空出場訊號。")
        if s.get('mode') == '實單':
            return False, ("「買進後持有不賣」只能用於回測/模擬,不可用於實單 —— "
                           "永不出場的實單等於把風險完全交給市場,沒有任何保護。")
        return True, ""
    if not has_exit:
        return False, ("至少要有一種出場方式 (出場訊號 / 停損 / 停利),否則持倉永遠不會出場。"
                       "若你就是要「買進後持有不賣」來當比較基準,請勾選該選項。")
    # 【ADR-074 看A做B】驗證:B (執行商品) 不可為指數 (加權/櫃買不能下單);
    # A (訊號來源) 可為任何商品含指數。啟用看A做B 時 A 的代碼/週期要合法。
    if looks_like_index_symbol(s.get('symbol', '')):
        return False, "執行商品 (做B) 不可為指數 (加權/櫃買等)。指數只能當『看A』的訊號來源,不能下單。"
    if watch_enabled(s):
        if not str(watch_symbol_of(s)).strip():
            return False, "已啟用「看A做B」,但『看A』的商品代碼不可空白"
        if watch_trade_type_of(s) not in WATCH_TRADE_TYPES:
            return False, f"『看A』的種類僅支援 {'/'.join(WATCH_TRADE_TYPES)}"
        if not is_valid_timeframe(watch_timeframe_of(s)):
            return False, f"『看A』的週期格式不正確 (可選 {'/'.join(VALID_TIMEFRAMES)},或自訂 N分K/N時K)"
    return True, ""


# ============================================================
# 評估與狀態機
# ============================================================

def eval_conditions(df, conds, logic='AND', errors=None):
    """評估一組條件。回傳 (是否成立, [每條的(標籤,結果)])。空清單: AND=False, OR=False。

    【ADR-065】單一條件拋例外時 (如參數型別錯、資料筆數不足) 這裡刻意只讓
    「那一條」變成 False,不讓整組評估跟著失敗——這個隔離設計是對的,一條
    寫壞的條件不該連累其他條件。但舊版完全不留下任何痕跡:那條條件會從此
    永遠評估為 False,使用者看到的是「條件到了卻沒有動作」,卻沒有任何錯誤
    可查。errors (可選,list):條件拋例外時 append (標籤, 例外訊息) 進去,
    讓呼叫端 (evaluate_strategy) 轉存進 runtime,GUI 層再決定要不要提醒使用者。
    """
    results = []
    for c in conds:
        meta = CONDITIONS.get(c.get('type'))
        ok = False
        if meta is not None:
            try:
                ok = bool(meta[2](df, c.get('params', {})))
            except Exception as e:
                ok = False
                if errors is not None:
                    errors.append((condition_label(c), f"{type(e).__name__}: {e}"))
        results.append((condition_label(c), ok))
    if not results:
        return False, results
    if logic == 'AND':
        return all(r for _, r in results), results
    return any(r for _, r in results), results


def evaluate_strategy(strategy, runtime, df_closed, now_ts, today_str):
    """
    策略主評估:傳入「只含已收盤K棒」的 df。
    回傳 intents list,每個 intent:
      {'kind': 'OPEN'/'CLOSE', 'action': '買進'/'賣出', 'qty': int,
       'price': float(該根收盤價), 'reason': str}
    這裡只產生「意圖」,不下單;呼叫端要先過 risk_check 再執行,
    成交後呼叫 apply_fill 更新狀態。

    【ADR-075 看A做B (修正語意)】本函式一律「看 A」:進出場條件、指標、
    **停損/停利** 全部以傳入的 df_closed (訊號來源 A) 計算,intent['price']
    也是 A 的收盤價 (成為 runtime['entry_price'],讓停損停利以 A 的價格判斷)。
    「做 B」只發生在下單/記帳那一層:呼叫端把實際成交價換成 B 的價格 (見
    stock_app_pro._quant_eval_pass / _place_strategy_order)。這樣「停損停利
    條件看 A、下單看 B」就成立,引擎本身完全不需要知道 B 的存在。
    """
    if df_closed is None or len(df_closed) < 3:
        return []
    # 換日重置每日計數
    if runtime.get('day') != today_str:
        runtime['day'] = today_str
        runtime['trades_today'] = 0
        runtime['realized_pnl_today'] = 0.0
    # 同一根K棒只評估一次 (杜絕重複訊號/重複下單的第一道閘門)。用 A 的 K 棒時間,
    # 一根 A 訊號 K 棒只評估一次。
    bar_ts = str(df_closed.index[-1])
    if bar_ts == runtime.get('last_bar_ts'):
        return []
    runtime['last_bar_ts'] = bar_ts

    close = float(df_closed['Close'].iloc[-1])
    state = runtime.get('state', 'FLAT')
    intents = []
    # 【ADR-065】條件評估拋例外時 eval_conditions 只讓那一條變 False,不中斷
    # 整組評估——但也不能完全無聲無息,否則「條件到了卻沒有動作」查無可查。
    # 收在 runtime,GUI 層看到非空就提醒使用者;沒有錯誤時個別呼叫點會蓋成
    # 空 list,不會讓已經修好的舊錯誤一直留著誤導人。
    cond_errors = []

    # ============================================================
    # 【ADR-061】買進後持有不賣 (Buy & Hold) = 「條件成立就買、永不賣出」
    # ============================================================
    # ADR-059 把它做成「只進場一次」,是我對需求的誤解。使用者要的是:
    # 只要進場條件成立就再買一次 (累積加碼),而且永遠不賣 —— 這是
    # 定期定額/逢低承接那一類的累積模型,用來當長期持有的比較基準。
    #
    # 因此這裡不看 state:FLAT 會買、已經 LONG 也照樣再買。
    # 也永遠不產生 CLOSE intent (停損停利/出場訊號在 validate_strategy
    # 已經被擋掉不可能存在,這裡再明確地不走那條路)。
    #
    # 安全性:validate_strategy 禁止 buy_and_hold 用於實單,所以這條
    # 「會無限累積部位」的路徑不可能出現在真實下單流程;它只服務回測與模擬。
    if strategy.get('buy_and_hold'):
        # 【ADR-062】三種模式,使用者要能互相比較:
        #   single     : 只買一次 (ADR-059 的原始行為,保留)
        #   accumulate : 每次條件成立就再買固定數量 (ADR-061)
        #   dca        : 每期投入固定金額,數量隨價格變動 (定期定額)
        mode = str(strategy.get('bnh_mode', 'accumulate'))
        if mode not in BNH_MODES:
            mode = 'accumulate'
        # 單筆長抱:已經有部位就不再進場
        if mode == 'single' and state != 'FLAT':
            return intents

        bar_ts_obj = df_closed.index[-1]
        if mode == 'dca':
            # 定期定額:同一個投入週期只買一次 (第一個符合條件的交易日)
            key = _dca_period_key(bar_ts_obj, strategy.get('dca_interval', 'month'))
            if runtime.get('dca_last_period') == key:
                return intents

        ok, details = eval_conditions(df_closed, strategy.get('entry', []), 'AND', errors=cond_errors)
        runtime['condition_errors'] = list(cond_errors)
        if not ok:
            return intents
        action = '買進' if strategy.get('direction') == '做多' else '賣出'

        if mode == 'dca':
            # 把「金額」換算成「張/口」:數量必須是整數,無法整除的餘額
            # 累積到下一期 (真實定期定額就是這樣運作,不會憑空多買也不會蒸發)。
            amount = float(strategy.get('dca_amount', 0) or 0)
            if amount <= 0:
                return intents
            budget = float(runtime.get('dca_carry', 0.0)) + amount
            usz = unit_size(strategy)
            per_unit = close * usz
            if per_unit <= 0:
                return intents
            n_units = int(budget // per_unit)
            if n_units <= 0:
                # 這期買不起一個單位 → 全數留到下一期
                runtime['dca_carry'] = budget
                runtime['dca_last_period'] = _dca_period_key(
                    bar_ts_obj, strategy.get('dca_interval', 'month'))
                return intents
            runtime['dca_carry'] = budget - n_units * per_unit
            runtime['dca_last_period'] = _dca_period_key(
                bar_ts_obj, strategy.get('dca_interval', 'month'))
            reason = (f"定期定額({DCA_INTERVAL_LABELS.get(str(strategy.get('dca_interval', 'month')), '每月')}"
                      f") 投入 {amount:,.0f} → {n_units} 單位")
            intents.append({'kind': 'OPEN', 'action': action,
                            'qty': n_units, 'price': close, 'reason': reason})
            return filter_intents_by_time(intents, strategy, bar_ts, runtime=runtime)

        prefix = "單筆長抱進場: " if mode == 'single' else "累積買進: "
        reason = prefix + " 且 ".join(lab for lab, _ in details)
        intents.append({'kind': 'OPEN', 'action': action,
                        'qty': int(strategy.get('qty', 1)), 'price': close, 'reason': reason})
        return filter_intents_by_time(intents, strategy, bar_ts, runtime=runtime)

    if state == 'FLAT':
        ok, details = eval_conditions(df_closed, strategy.get('entry', []), 'AND', errors=cond_errors)
        runtime['condition_errors'] = list(cond_errors)
        if ok:
            action = '買進' if strategy.get('direction') == '做多' else '賣出'
            reason = "進場: " + " 且 ".join(lab for lab, _ in details)
            intents.append({'kind': 'OPEN', 'action': action,
                            'qty': int(strategy.get('qty', 1)), 'price': close, 'reason': reason})
        # 【ADR-066】entry_time_start/end、specific_entry_time 這幾個進場時間窗欄位
        # 兩個策略編輯器都能設定,但這裡舊版從未呼叫 filter_intents_by_time——內建
        # 策略的進場時間限制形同虛設 (自訂 Python 策略的 decision_to_intents 有
        # 正確套用,兩邊不一致)。空清單或未設定時間窗時這裡是 no-op,不影響既有行為。
        return filter_intents_by_time(intents, strategy, bar_ts, runtime=runtime)

    # 持倉中:先看停損/停利,再看出場訊號
    _stop = check_bar_stop(strategy, runtime, close)
    if _stop is not None:
        intents.append(_stop)
        return intents
    ok, details = eval_conditions(df_closed, strategy.get('exit_signals', []), 'OR', errors=cond_errors)
    runtime['condition_errors'] = list(cond_errors)
    if ok:
        hit = " 或 ".join(lab for lab, r in details if r)
        intents.append(_close_intent(runtime, close, f"出場訊號: {hit}"))
    intents = filter_intents_by_time(intents, strategy, bar_ts, runtime=runtime)
    return intents


def _normalize_time(t_str):
    if not t_str: return ''
    parts = str(t_str).strip().split(':')
    if len(parts) >= 3:
        try: return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
        except: return ''
    elif len(parts) >= 2:
        try: return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
        except: return ''
    return ''

def _is_time_in_window(bar_t, t_start, t_end):
    if not t_start and not t_end: return True
    if t_start and not t_end: return bar_t >= t_start
    if not t_start and t_end: return bar_t <= t_end
    if t_start <= t_end:
        return t_start <= bar_t <= t_end
    else:
        return bar_t >= t_start or bar_t <= t_end

def filter_intents_by_time(intents, strategy, bar_ts_str, runtime=None):
    """依 entry_time_start/end、exit_time_start/end、specific_entry_time 排除
    落在時間窗外的 intent。

    【ADR-066】runtime (可選):有傳入時,每次呼叫都會把 runtime['time_window_skips']
    重設成這次「被時間窗排除的 intent」說明清單 (沒有就是空 list)。這是為了不讓
    這類排除變成又一種「條件到了卻沒有動作,但完全查不到原因」——呼叫端 (GUI)
    可以比照 condition_errors 的方式,看到非空就提醒使用者「這不是 bug,是你設定
    的時間窗擋下了這筆」。"""
    if runtime is not None:
        runtime['time_window_skips'] = []
    if not intents or not bar_ts_str or ' ' not in str(bar_ts_str):
        return intents

    try: bar_time_str = str(bar_ts_str).split(' ')[1][:8]
    except: return intents
    if len(bar_time_str) == 5:
        bar_time_str += ":00"

    en_start = _normalize_time(strategy.get('entry_time_start', ''))
    en_end = _normalize_time(strategy.get('entry_time_end', ''))
    ex_start = _normalize_time(strategy.get('exit_time_start', ''))
    ex_end = _normalize_time(strategy.get('exit_time_end', ''))
    sp_entry = _normalize_time(strategy.get('specific_entry_time', ''))

    if not (en_start or en_end or ex_start or ex_end or sp_entry):
        return intents

    filtered = []
    skips = []
    for intent in intents:
        blocked = False
        if intent.get('kind') == 'OPEN':
            if sp_entry and bar_time_str != sp_entry:
                blocked = True
            elif not _is_time_in_window(bar_time_str, en_start, en_end):
                blocked = True
        elif intent.get('kind') == 'CLOSE':
            if not _is_time_in_window(bar_time_str, ex_start, ex_end):
                blocked = True
        if blocked:
            skips.append(f"{intent.get('kind')} {intent.get('action')} 因設定的進/出場時間窗被跳過 "
                          f"(K棒時間 {bar_time_str})")
            continue
        filtered.append(intent)
    if runtime is not None:
        runtime['time_window_skips'] = skips
    return filtered

def _close_intent(runtime, price, reason):
    action = '賣出' if runtime.get('state') == 'LONG' else '買進'
    return {'kind': 'CLOSE', 'action': action, 'qty': int(runtime.get('qty', 0)), 'price': price, 'reason': reason}


def risk_check(strategy, runtime, intent, now_ts):
    """最後一道守門。回傳 (ok, 擋單原因)。
    出場單不受「每日次數/冷卻/虧損熔斷」任何一項限制 (持倉一定要能出得去,
    這三項風控的存在理由是防止「過度進場」,不是限制出場;見 ADR-035 §7、
    ADR-065)。"""
    if intent['kind'] == 'OPEN':
        if runtime.get('trades_today', 0) >= int(strategy.get('max_trades_per_day', 3)):
            return False, f"已達每日進場上限 {strategy.get('max_trades_per_day')} 次"
        limit = float(strategy.get('daily_loss_limit', 0) or 0)
        if limit > 0 and runtime.get('realized_pnl_today', 0.0) <= -limit:
            return False, f"觸發每日虧損熔斷 (今日已實現 {runtime.get('realized_pnl_today', 0.0):+.2f} ≤ -{limit})"
        # 【ADR-065 修正】冷卻舊版寫在 OPEN/CLOSE 共用的區塊之外,連出場單
        # 也一併被擋——跟本函式文件與 ADR-035 §7 講的「出場單不受限制」互相
        # 矛盾,且與 daily 次數/熔斷兩項風控不一致 (那兩項一直都只管 OPEN)。
        # 持倉中若剛好在冷卻視窗內出現出場訊號 (常見:冷卻秒數與K棒週期
        # 接近時),舊版會讓部位卡住出不去,直到冷卻過了才補評估——這正是
        # 「條件到了卻沒動作」的真實成因之一。
        cooldown = float(strategy.get('cooldown_sec', 0) or 0)
        if cooldown > 0 and (now_ts - float(runtime.get('last_order_ts', 0) or 0)) < cooldown:
            remain = cooldown - (now_ts - float(runtime.get('last_order_ts', 0) or 0))
            return False, f"冷卻中 (還剩 {remain:.0f} 秒)"
    if int(intent.get('qty', 0)) <= 0:
        return False, "數量為 0"
    return True, ""


def apply_fill(strategy, runtime, intent, now_ts, exec_price=None):
    """下單送出後更新狀態 (Phase1 採「委託視同成交」的樂觀模型,詳見 ADR-035 侷限)。

    【新ADR】exec_price:實際下單商品 (看A做B時是B) 的成交價,寫進
    runtime['exec_entry_price'] 供期貨即時停損/停利 (check_intrabar_futures_stop)
    比對用。未提供時 (測試/回測/一般不看A做B) 退回用 intent['price']
    (此時 exec_entry_price 就等於 entry_price,行為不變)。"""
    if intent['kind'] == 'OPEN':
        # 【ADR-065】last_order_ts 只在「進場」時更新,語意是「距離上次進場
        # 多久」——冷卻本來就只該限制進場頻率 (見 risk_check)。若這裡連 CLOSE
        # 也更新,會讓緊接在平倉之後的反手開倉 (同一輪評估、ADR-053 的
        # decision_to_intents 一次回傳 [平倉, 開倉]) 因為 (now_ts - 剛更新的
        # last_order_ts) == 0 而被冷卻擋下,反手永遠只完成一半。
        runtime['last_order_ts'] = float(now_ts)
        # 【ADR-053 重大修正】持倉狀態必須由「實際成交動作」決定,不能讀
        # strategy['direction']:自訂 Python 策略的 direction 只是佔位值
        # (ADR-045),多空由 on_bar 回傳 BUY/SELL 決定。舊版讓「賣出開倉」
        # 也被記成 LONG,造成連鎖失真:
        #   (a) 下一根傳給 on_bar 的 ctx.position 是錯的 → 反手策略的
        #       SHORT 分支永遠不會觸發,只能單向操作;
        #   (b) 平倉時 mult 用 LONG 的符號 → 空單已實現損益正負相反,
        #       每日虧損熔斷會據此誤判。
        # 買進開倉=LONG、賣出開倉=SHORT,內建策略行為不變 (其 action 本就
        # 與 direction 一致)。
        new_state = 'LONG' if intent.get('action') == '買進' else 'SHORT'
        # 【ADR-061】買進持有 (累積) 模式:同方向再次開倉要「加碼」,
        # 也就是數量累加、進場價改成加權平均成本,而不是把前一筆蓋掉。
        # 舊版直接覆蓋 entry_price/qty,在累積模型下會讓成本基礎完全失真。
        exec_px = float(exec_price) if exec_price is not None else float(intent['price'])
        prev_qty = int(runtime.get('qty', 0) or 0)
        if (strategy.get('buy_and_hold') and prev_qty > 0
                and runtime.get('state') == new_state):
            add_qty = int(intent['qty'])
            prev_px = float(runtime.get('entry_price', 0) or 0)
            prev_exec_px = float(runtime.get('exec_entry_price', 0) or 0)
            total_qty = prev_qty + add_qty
            runtime['entry_price'] = ((prev_px * prev_qty + float(intent['price']) * add_qty)
                                      / total_qty) if total_qty else 0.0
            runtime['exec_entry_price'] = ((prev_exec_px * prev_qty + exec_px * add_qty)
                                           / total_qty) if total_qty else 0.0
            runtime['qty'] = total_qty
        else:
            runtime['entry_price'] = float(intent['price'])
            runtime['exec_entry_price'] = exec_px
            runtime['qty'] = int(intent['qty'])
        runtime['state'] = new_state
        runtime['trades_today'] = int(runtime.get('trades_today', 0)) + 1
    else:
        entry = float(runtime.get('entry_price', 0) or 0)
        mult = 1.0 if runtime.get('state') == 'LONG' else -1.0
        pnl = (float(intent['price']) - entry) * mult * int(runtime.get('qty', 0))
        runtime['realized_pnl_today'] = float(runtime.get('realized_pnl_today', 0.0)) + pnl
        runtime['state'] = 'FLAT'
        runtime['entry_price'] = 0.0
        runtime['exec_entry_price'] = 0.0
        runtime['qty'] = 0


def position_mismatch(strategy, runtime, acct_position):
    """【新ADR 持倉核對】策略要重新啟用前,比對策略自己記錄的持倉狀態
    (runtime['state']/['qty']) 跟它指定的模擬帳戶內「實際」的部位
    (acct_position,即 acct['positions'].get(symbol),可能是 None) 是否一致。

    背景:模擬帳戶檔可能因為使用者手動重置/刪除帳戶、或策略中途被改指到
    別的帳戶等操作,跟策略以為的持倉狀態不同步——這種情況下若讓策略悄悄
    重新啟用,可能會用一個「以為有部位」但帳戶其實沒有 (或反過來) 的錯誤
    起點繼續交易,例如平不到根本不存在的倉、或帳戶裡有一筆沒人管的孤兒
    部位。啟用前先比對一次,不一致就交由呼叫端 (GUI) 通知使用者手動處理。

    一致 (含兩邊都沒有部位) 回傳 None;不一致回傳描述 dict:
    {'rt_state': 'FLAT'/'LONG'/'SHORT', 'rt_qty': int,
     'acct_direction': '多'/'空'/None, 'acct_qty': int}。"""
    rt_state = runtime.get('state', 'FLAT')
    rt_qty = int(runtime.get('qty', 0) or 0)
    acct_direction = acct_position.get('direction') if acct_position else None
    acct_qty = int(acct_position.get('qty', 0)) if acct_position else 0
    if rt_state == 'FLAT':
        if not acct_position or acct_qty <= 0:
            return None
        return {'rt_state': rt_state, 'rt_qty': 0,
                'acct_direction': acct_direction, 'acct_qty': acct_qty}
    rt_direction = '多' if rt_state == 'LONG' else '空'
    if acct_direction != rt_direction or acct_qty != rt_qty:
        return {'rt_state': rt_state, 'rt_qty': rt_qty,
                'acct_direction': acct_direction, 'acct_qty': acct_qty}
    return None


# 【ADR-123】這些策略種類**自己有完整的出場邏輯**,泛用的停損/停利
# (stop_loss_pct / take_profit_pct / stop_loss_abs / take_profit_abs)
# 不可以插手 —— 插手等於把使用者選定的那套風控整個換掉。
#
# 實例:終極波段 (chukuangren_band) 的出場是 X/C/F/Y/Z 那套,以加權指數
# 點位為準、還要隔天 12:00 二次確認。但它的 default_strategy() 疊在
# new_strategy() 上面,把 stop_loss_pct=2.0 這個**編輯器裡看不到**的預設值
# 一起帶了進來,於是部位被一個 intrabar 的 2% 停損搶先砍掉。
#
# 這裡刻意用**字串**而不是 `from . import chukuangren_band`:那個模組反過來
# import 本模組,會變成循環 import。兩邊靠 tests/test_core.py 的一條斷言
# 釘在一起 (KIND in OWN_EXIT_KINDS),改名了不會無聲脫鉤。
OWN_EXIT_KINDS = frozenset({'chukuangren_band'})


def has_own_exit_logic(strategy):
    """這檔策略是不是自己管出場 (泛用停損停利不得插手)。"""
    return str((strategy or {}).get('kind') or '') in OWN_EXIT_KINDS


# 【ADR-124】這些 kind **結構上就只做日盤**,不是使用者偏好。
#
# 終極波段:看A 是加權指數**日K** (指數沒有夜盤)、12:00 二次確認、12:01 執行
# —— 夜盤對它沒有任何意義。使用者的原話是「終極波段不會在夜盤做任何動作的」,
# 那是這個策略的設計事實,所以這裡直接覆寫 futures_session 的存檔值,
# 既有策略不必遷移就正確 (同 OWN_EXIT_KINDS 的處理哲學)。
#
# 一樣用字串避免循環 import,靠測試把它跟 chukuangren_band.KIND 釘在一起。
DAY_SESSION_ONLY_KINDS = frozenset({'chukuangren_band'})


# 【ADR-128】這些 kind 的「看A 訊號週期」是**結構上固定**的,存檔值不算。
#
# 使用者實機回報:「我明明設定就是看日K,為什麼哪裡有看5分K?」—— 因為終極波段
# 的 watch_timeframe 被寫死成 '5分K',而那個值的用途根本不是訊號週期,是拿去
# 驅動 _quant_eval_pass 的 K 棒邊界節拍 (為了讓迴圈能踩進 12:00~12:04 的二次
# 確認窗口)。訊號本身一直是日K (`tf='日K'` 寫死在呼叫端),所以:
#   * 使用者的設定沒錯、清單顯示的「日K」也沒錯,
#   * 但 watch_timeframe_of() 這個「A 的訊號週期」介面會回答 5分K —— 語意錯了。
#
# ADR-128 把節拍交給獨立的確認通道 (_qt_chukuangren_confirm_pass),這裡則讓
# 介面回答真話。覆寫存檔值,所以**既有策略不必遷移就正確**
# (同 OWN_EXIT_KINDS / DAY_SESSION_ONLY_KINDS 的處理哲學)。
#
# 刻意**不**跟上面兩個清單合併:三者目前內容相同,但語意是三件事 (出場邏輯 /
# 交易時段 / 訊號週期)。日後多一個「自己管出場但訊號看分K」的種類時,合併過的
# 寫法會一起把週期改錯。有單元測試守著這個區分。
DAY_SIGNAL_KINDS = frozenset({'chukuangren_band'})
DAY_SIGNAL_TIMEFRAME = '日K'


def is_locked(strategy):
    """【ADR-125】啟用中的策略一律鎖住:**不可編輯、不可刪除**。

    使用者實測回報:一檔「狀態=啟用、運轉狀態=運轉中」的策略被直接刪掉了。
    理由不只是使用者要求 —— 策略正在跑,底下有 runtime 狀態、隨時可能成交,
    從腳底下把它抽掉是資料競爭。要動它就先停用。

    「不可編輯」的實作方式是**唯讀檢視**(編輯器把儲存鈕換成停用狀態,而且
    在深拷貝上操作),不是把編輯器整個擋掉 —— 能看正在跑的策略掛了什麼條件
    是有價值的。
    """
    return bool((strategy or {}).get('enabled'))


def can_delete(strategy, runtime):
    """【ADR-125】這檔策略現在可不可以刪除。回傳 (ok, 理由)。

    檢查順序刻意是「先看啟用、再看持倉」:兩者都不成立時,使用者要做的
    第一件事都是「先停用」,訊息就該直接指向那一步,不要先叫他去處理持倉。
    """
    s = strategy or {}
    name = s.get('name', '')
    if is_locked(s):
        return False, (f"策略「{name}」啟用中,不可刪除。請先按「停用」再刪除 "
                       f"(執行中的策略隨時可能成交,直接刪掉會留下對不上的紀錄)。")
    if (runtime or {}).get('state') in ('LONG', 'SHORT'):
        return False, (f"策略「{name}」仍有持倉 ({(runtime or {}).get('state')}),"
                       f"請先手動處理持倉後再刪除。")
    return True, ""


def include_night_of(strategy):
    """這檔策略要不要做期貨夜盤。

    結構上只做日盤的種類一律 False;其餘照使用者設定的 futures_session
    ('day' = 只做日盤,其餘含缺值 = 日盤+夜盤,維持既有預設)。

    所有「要不要在這個時段動作」的判斷都要走這一個函式 —— 評估迴圈與即時
    停損原本各自算一次,結果即時停損那條根本沒算 (ADR-124 的根因)。
    """
    s = strategy or {}
    if str(s.get('kind') or '') in DAY_SESSION_ONLY_KINDS:
        return False
    return s.get('futures_session', 'day_night') != 'day'


def intrabar_stop_enabled(strategy):
    """【ADR-136】停損停利要不要「即時觸發」(不等K棒收盤)。

    使用者要求把它變成一個**可勾選的選項**,而且「不分商品」——
    勾了就即時,沒勾就維持K棒收盤才判定。

    **沒有這個欄位的舊策略,一律沿用它原本的行為**,不可以因為新增欄位就
    悄悄改變既有策略:
      · 期貨 → True  (ADR-087 起本來就是即時)
      · 其他 → False (本來就是K棒收盤才判定)
    """
    s = strategy or {}
    if 'intrabar_stop' in s:
        return bool(s.get('intrabar_stop'))
    return trade_type_of(s) == '期貨'


def stop_levels(strategy, runtime):
    """【ADR-136】把停損/停利換算成**價位**。回傳 (stop_price, take_price),
    沒設定的那一邊是 None;沒有部位或沒有進場價回 (None, None)。

    為什麼要有這個:回測要判斷「這根K棒的高低點有沒有觸及停損價」,
    需要的是價位而不是百分比。**與 check_bar_stop 用同一組欄位換算**,
    兩邊才不會一個有觸發、一個沒觸發。
    """
    state = (runtime or {}).get('state', 'FLAT')
    if state not in ('LONG', 'SHORT'):
        return None, None
    entry = float((runtime or {}).get('entry_price', 0) or 0)
    if entry <= 0:
        return None, None
    mult = 1.0 if state == 'LONG' else -1.0
    sl = float(strategy.get('stop_loss_pct', 0) or 0)
    tp = float(strategy.get('take_profit_pct', 0) or 0)
    sl_abs = float(strategy.get('stop_loss_abs', 0) or 0)
    tp_abs = float(strategy.get('take_profit_abs', 0) or 0)
    stops, takes = [], []
    if sl > 0:
        stops.append(entry - mult * entry * sl / 100.0)
    if sl_abs > 0:
        stops.append(entry - mult * sl_abs)
    if tp > 0:
        takes.append(entry + mult * entry * tp / 100.0)
    if tp_abs > 0:
        takes.append(entry + mult * tp_abs)
    # 多個設定同時存在時取「先被碰到的那一個」= 離進場價最近的
    stop_p = (max(stops) if state == 'LONG' else min(stops)) if stops else None
    take_p = (min(takes) if state == 'LONG' else max(takes)) if takes else None
    return stop_p, take_p


def check_bar_stop(strategy, runtime, close):
    """【ADR-136】K棒收盤價的停損/停利判斷,回傳 CLOSE intent 或 None。

    這段原本內嵌在 evaluate_strategy() 裡,**自訂策略完全走不到**
    (自訂策略走 custom_strategy.decision_to_intents,不經過 evaluate_strategy)
    —— 於是使用者在自訂策略編輯器設的停損停利,在回測裡形同虛設。
    抽出來讓兩條路共用同一份判斷 (P-67)。
    """
    state = (runtime or {}).get('state', 'FLAT')
    if state not in ('LONG', 'SHORT'):
        return None
    entry = float((runtime or {}).get('entry_price', 0) or 0)
    if entry <= 0:
        return None
    close = float(close)
    direction_mult = 1.0 if state == 'LONG' else -1.0
    move_pct = (close - entry) / entry * 100.0 * direction_mult
    move_abs = (close - entry) * direction_mult  # 【ADR-043】絕對價差 (股票元/期貨點)
    sl = float(strategy.get('stop_loss_pct', 0) or 0)
    tp = float(strategy.get('take_profit_pct', 0) or 0)
    sl_abs = float(strategy.get('stop_loss_abs', 0) or 0)
    tp_abs = float(strategy.get('take_profit_abs', 0) or 0)
    unit = '點' if is_futures(strategy) else '元'
    # 停損:百分比或絕對值任一觸發即出場 (取先到者)
    if sl > 0 and move_pct <= -sl:
        return _close_intent(runtime, close, f"停損出場 (損益 {move_pct:.2f}% ≤ -{sl}%)")
    if sl_abs > 0 and move_abs <= -sl_abs:
        return _close_intent(runtime, close, f"停損出場 (損益 {move_abs:+.2f}{unit} ≤ -{sl_abs}{unit})")
    if tp > 0 and move_pct >= tp:
        return _close_intent(runtime, close, f"停利出場 (損益 {move_pct:.2f}% ≥ {tp}%)")
    if tp_abs > 0 and move_abs >= tp_abs:
        return _close_intent(runtime, close, f"停利出場 (損益 {move_abs:+.2f}{unit} ≥ {tp_abs}{unit})")
    return None


def bar_touch_exit(strategy, runtime, bar_open, bar_high, bar_low):
    """【ADR-136】回測用:這根K棒的**高低點**有沒有觸及停損/停利價。

    回傳 (成交價, 原因) 或 None。這是「即時停損」在回測裡的對應模型 ——
    勾了即時觸發,實盤是盤中觸價就走,回測若只看收盤價就會**低估停損次數**,
    回測結果跟實際行為對不上 (使用者:「可以有效執行回測,這一點非常重要」)。

    成交價的取法刻意保守:**跳空時用開盤價**(比停損價更差),不做
    「一定成交在停損價」的樂觀假設。同一根同時碰到停損與停利時**算停損**
    —— 分不出誰先到,取對使用者不利的那一邊才不會高估績效。
    """
    state = (runtime or {}).get('state', 'FLAT')
    if state not in ('LONG', 'SHORT'):
        return None
    stop_p, take_p = stop_levels(strategy, runtime)
    if stop_p is None and take_p is None:
        return None
    try:
        o, hi, lo = float(bar_open), float(bar_high), float(bar_low)
    except (TypeError, ValueError):
        return None
    unit = '點' if is_futures(strategy) else '元'
    if state == 'LONG':
        if stop_p is not None and lo <= stop_p:
            return (min(o, stop_p), f"停損出場 (盤中觸及 {stop_p:.2f}{unit})")
        if take_p is not None and hi >= take_p:
            return (max(o, take_p), f"停利出場 (盤中觸及 {take_p:.2f}{unit})")
    else:
        if stop_p is not None and hi >= stop_p:
            return (max(o, stop_p), f"停損出場 (盤中觸及 {stop_p:.2f}{unit})")
        if take_p is not None and lo <= take_p:
            return (min(o, take_p), f"停利出場 (盤中觸及 {take_p:.2f}{unit})")
    return None


def check_intrabar_stop(strategy, runtime, live_price):
    """【新ADR / ADR-135】即時停損停利:不等K棒收盤,即時價 (呼叫端傳入下單商品B的
    最新市價快照) 一觸及使用者設定的停損%/停利%/停損點數/停利點數,立刻回傳
    CLOSE intent (不等下一根K棒)。

    背景:evaluate_strategy() 的停損/停利只在該策略訊號週期 (可能是5分K甚至
    更長) K棒收盤時評估一次,且比對基準是「看A做B」的A的價格 (ADR-075) ——
    兩者都會讓「畫面上B的帳面已經虧超過停損點,系統卻遲遲沒有動作」。這裡改用
    runtime['exec_entry_price'] (B的實際成交均價,apply_fill 寫入) 對比 B 的
    即時價,基準與畫面上的損益完全一致,且不受K棒收盤節奏限制。

    【ADR-135】原本只對**期貨**生效,股票/零股維持「K棒收盤才判定」。
    使用者要求:「停損停利點數到了,就立即執行,不需要等待到收盤。」——
    沒有限定商品別,所以這裡拿掉交易種類的限制,**股票/零股/期貨一律適用**。

    > 這是刻意的行為變更:既有的股票策略若設了停損%/停利%/停損元/停利元,
    > 從此會在盤中觸價就出場,而不是等該週期K棒收盤。這正是使用者要的,
    > 但它會改變既有股票策略的成交時點,所以在 ADR-135 明確記錄。

    與 evaluate_strategy() 內建的K棒收盤停損/停利 (以A為準) 各自獨立、
    互不影響——不管哪一邊先觸發,觸發後 state 變 FLAT,另一邊下次評估時
    state!=LONG/SHORT 自然是 no-op。

    回傳 intent dict 或 None (未觸發/不適用)。"""
    # 【ADR-123】自己管出場的策略種類一律不適用 —— 見 OWN_EXIT_KINDS 的說明。
    # 放在最前面:這是「這條規則對這檔策略根本不適用」,不是「條件沒觸發」。
    if has_own_exit_logic(strategy):
        return None
    # 【ADR-136】使用者要求改成可勾選的選項:沒勾就維持「K棒收盤才判定」。
    if not intrabar_stop_enabled(strategy):
        return None
    state = runtime.get('state', 'FLAT')
    if state not in ('LONG', 'SHORT'):
        return None
    entry = float(runtime.get('exec_entry_price', 0) or 0)
    live_price = float(live_price or 0)
    if entry <= 0 or live_price <= 0:
        return None
    direction_mult = 1.0 if state == 'LONG' else -1.0
    move_pct = (live_price - entry) / entry * 100.0 * direction_mult
    move_abs = (live_price - entry) * direction_mult
    sl = float(strategy.get('stop_loss_pct', 0) or 0)
    tp = float(strategy.get('take_profit_pct', 0) or 0)
    sl_abs = float(strategy.get('stop_loss_abs', 0) or 0)
    tp_abs = float(strategy.get('take_profit_abs', 0) or 0)
    if sl > 0 and move_pct <= -sl:
        return _close_intent(runtime, live_price, f"即時停損出場 (損益 {move_pct:.2f}% ≤ -{sl}%)")
    if sl_abs > 0 and move_abs <= -sl_abs:
        return _close_intent(runtime, live_price, f"即時停損出場 (損益 {move_abs:+.2f}點 ≤ -{sl_abs}點)")
    if tp > 0 and move_pct >= tp:
        return _close_intent(runtime, live_price, f"即時停利出場 (損益 {move_pct:.2f}% ≥ {tp}%)")
    if tp_abs > 0 and move_abs >= tp_abs:
        return _close_intent(runtime, live_price, f"即時停利出場 (損益 {move_abs:+.2f}點 ≥ {tp_abs}點)")
    return None


# 【ADR-135】舊名保留成別名:這個函式從「只做期貨」變成「所有商品別」之後,
# 名字裡的 futures 已經名不副實 (P-100 講過的坑)。新程式碼請用
# check_intrabar_stop;舊名留著讓既有呼叫端不必一次全改。
check_intrabar_futures_stop = check_intrabar_stop
