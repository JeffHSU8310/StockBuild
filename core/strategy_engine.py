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
import uuid
import pandas as pd

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
        'enabled': False,
        # 【ADR-070】交易時段閘門:session_gate=True 時,非交易時間自動待命,
        # 進入盤中才評估下單 (無需人工開啟);futures_session 決定期貨要不要含夜盤。
        'session_gate': True,         # True=非交易時間不動作 (建議);False=只看K棒邊界不看時段
        'futures_session': 'day_night',  # 'day'=只做日盤 08:45-13:45;'day_night'=含夜盤 15:00-次日05:00

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

def new_runtime():
    return {
        'state': 'FLAT',              # FLAT / LONG / SHORT
        'entry_price': 0.0,
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

VALID_TIMEFRAMES = ("1分K", "5分K", "15分K", "30分K", "60分K", "日K")

def validate_strategy(s):
    """存檔/啟用前的完整驗證。回傳 (ok, 錯誤訊息)。"""
    if not str(s.get('name', '')).strip():
        return False, "策略名稱不可空白"
    if not str(s.get('symbol', '')).strip():
        return False, "商品代碼不可空白"
    tt = trade_type_of(s)
    if tt not in TRADE_TYPES:
        return False, "交易種類僅支援 股票 / 零股 / 期貨"
    if s.get('timeframe') not in VALID_TIMEFRAMES:
        return False, f"週期僅支援 {'/'.join(VALID_TIMEFRAMES)}"
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
    """
    if df_closed is None or len(df_closed) < 3:
        return []
    # 換日重置每日計數
    if runtime.get('day') != today_str:
        runtime['day'] = today_str
        runtime['trades_today'] = 0
        runtime['realized_pnl_today'] = 0.0
    # 同一根K棒只評估一次 (杜絕重複訊號/重複下單的第一道閘門)
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
    entry = float(runtime.get('entry_price', 0) or 0)
    direction_mult = 1.0 if state == 'LONG' else -1.0
    if entry > 0:
        move_pct = (close - entry) / entry * 100.0 * direction_mult
        move_abs = (close - entry) * direction_mult  # 【ADR-043】絕對價差 (股票元/期貨點)
        sl = float(strategy.get('stop_loss_pct', 0) or 0)
        tp = float(strategy.get('take_profit_pct', 0) or 0)
        sl_abs = float(strategy.get('stop_loss_abs', 0) or 0)
        tp_abs = float(strategy.get('take_profit_abs', 0) or 0)
        unit = '點' if is_futures(strategy) else '元'
        # 停損:百分比或絕對值任一觸發即出場 (取先到者)
        if sl > 0 and move_pct <= -sl:
            intents.append(_close_intent(runtime, close, f"停損出場 (損益 {move_pct:.2f}% ≤ -{sl}%)"))
            return intents
        if sl_abs > 0 and move_abs <= -sl_abs:
            intents.append(_close_intent(runtime, close, f"停損出場 (損益 {move_abs:+.2f}{unit} ≤ -{sl_abs}{unit})"))
            return intents
        if tp > 0 and move_pct >= tp:
            intents.append(_close_intent(runtime, close, f"停利出場 (損益 {move_pct:.2f}% ≥ {tp}%)"))
            return intents
        if tp_abs > 0 and move_abs >= tp_abs:
            intents.append(_close_intent(runtime, close, f"停利出場 (損益 {move_abs:+.2f}{unit} ≥ {tp_abs}{unit})"))
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


def apply_fill(strategy, runtime, intent, now_ts):
    """下單送出後更新狀態 (Phase1 採「委託視同成交」的樂觀模型,詳見 ADR-035 侷限)。"""
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
        prev_qty = int(runtime.get('qty', 0) or 0)
        if (strategy.get('buy_and_hold') and prev_qty > 0
                and runtime.get('state') == new_state):
            add_qty = int(intent['qty'])
            prev_px = float(runtime.get('entry_price', 0) or 0)
            total_qty = prev_qty + add_qty
            runtime['entry_price'] = ((prev_px * prev_qty + float(intent['price']) * add_qty)
                                      / total_qty) if total_qty else 0.0
            runtime['qty'] = total_qty
        else:
            runtime['entry_price'] = float(intent['price'])
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
        runtime['qty'] = 0
