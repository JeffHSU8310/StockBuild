# -*- coding: utf-8 -*-
"""
core/chips_features.py — 把籌碼資料併進 K 棒 DataFrame,供策略條件使用 (ADR-101)

【為什麼要有這一層】策略條件函式的簽名是 `func(df, params)`,只吃 K 棒 df。
與其改動所有既有條件的簽名 (侵入性大),不如在資料準備階段把籌碼併成 df 的
額外欄位——這樣實盤 (evaluate_strategy) 與回測 (run_backtest) 自動走同一條
路,不會出現「回測對、實盤錯」的雙軌問題。

【⚠️ 未來函數防護:本模組最重要的職責】
三大法人買賣超、融資融券餘額都是**當日盤後 (約 15:00 後) 才公布**的。
如果讓 T 日的 K 棒讀到 T 日的籌碼,回測會嚴重高估績效——因為實盤在 T 日
盤中根本拿不到那個數字,這種策略無法複製。這是回測最經典的致命錯誤
(look-ahead bias)。

因此預設 `allow_same_day=False`:**T 日的 K 棒只看得到 T-1 日(含)以前的
籌碼**。使用者可在策略裡開啟進階選項改成允許當日,但那條路徑會被標記
(`chips_lookahead`),由 GUI 在回測報告醒目提示「此結果含未來函數」。

純 DataFrame 運算,零網路、零 tkinter、零 shioaji,可離線單元測試 (鐵則 11)。
"""
import pandas as pd

# 併進 df 後的欄位名 (一律 Chip 前綴,避免與 OHLCV 或指標欄位撞名)
COL_FOREIGN = 'ChipForeign'          # 外資及陸資買賣超 (個股:股;大盤:元)
COL_TRUST = 'ChipTrust'              # 投信買賣超
COL_DEALER = 'ChipDealer'            # 自營商買賣超
COL_INST_TOTAL = 'ChipInstTotal'     # 三大法人合計買賣超
COL_MARGIN = 'ChipMarginBalance'     # 融資餘額 (張)
COL_SHORT = 'ChipShortBalance'       # 融券餘額 (張)
COL_FUT_FOREIGN_OI = 'ChipFutForeignOI'   # 期貨外資未平倉淨額 (口)
COL_FUT_TRUST_OI = 'ChipFutTrustOI'       # 期貨投信未平倉淨額 (口)
COL_FUT_DEALER_OI = 'ChipFutDealerOI'     # 期貨自營商未平倉淨額 (口)

STOCK_COLS = [COL_FOREIGN, COL_TRUST, COL_DEALER, COL_INST_TOTAL]
MARGIN_COLS = [COL_MARGIN, COL_SHORT]
FUT_COLS = [COL_FUT_FOREIGN_OI, COL_FUT_TRUST_OI, COL_FUT_DEALER_OI]
ALL_CHIP_COLS = STOCK_COLS + MARGIN_COLS + FUT_COLS

# 個股籌碼原始單位是「股」;使用者與下單思考單位是「張」。
# 資料保持原始的股 (不失真),條件參數用張,比較時乘上這個常數。
SHARES_PER_LOT = 1000

# 籌碼是日頻資料,分K策略用它意義不大 (當日所有分K只會讀到同一筆前一日的值)。
# 【ADR-101】使用者決定:日K 及以上才允許使用籌碼條件。
CHIP_ALLOWED_TIMEFRAMES = ('日K', '週K', '月K')


def timeframe_supports_chips(timeframe):
    """這個週期能不能用籌碼條件。分K 一律不行 (見上方常數說明)。"""
    return str(timeframe or '') in CHIP_ALLOWED_TIMEFRAMES


def _as_date_str(idx):
    """DatetimeIndex → 'YYYY-MM-DD' 字串 Series (籌碼一律以日為單位對齊)。"""
    return pd.Series(pd.to_datetime(idx).strftime('%Y-%m-%d'), index=range(len(idx)))


def _prepare_chip_frame(chips_df, date_col='Date'):
    """把籌碼表整理成「日期由舊到新、日期為字串」的乾淨表。空表回 None。"""
    if chips_df is None or len(chips_df) == 0 or date_col not in chips_df.columns:
        return None
    out = chips_df.copy()
    out[date_col] = out[date_col].astype(str)
    out = out.dropna(subset=[date_col])
    if out.empty:
        return None
    return out.sort_values(date_col, kind='stable').reset_index(drop=True)


def attach_chips(df, stock_chips=None, margin_chips=None, futures_chips=None,
                 allow_same_day=False, futures_product=None):
    """把籌碼併成 df 的額外欄位,回傳新的 df (不修改原物件)。

    參數:
      df            : K 棒 DataFrame,需有 DatetimeIndex。
      stock_chips   : 已篩選到「單一標的」的個股籌碼 (或大盤籌碼),
                      需有 Date/Foreign/Trust/Dealer/InstTotal 欄。
      margin_chips  : 市場融資融券,需有 Date/MarginBalance/ShortBalance。
      futures_chips : 期貨三大法人,需有 Date/Product/Investor/NetOI。
      allow_same_day: **預設 False = T 日只看得到 T-1 日(含)以前的籌碼**。
                      設 True 會讓 T 日讀到 T 日籌碼 = 未來函數,僅供研究,
                      呼叫端必須把這件事標示給使用者 (見模組 docstring)。
      futures_product: 只取這個期貨商品 (例如 '臺股期貨');None 表示不併期貨。

    沒有對應籌碼的 K 棒,欄位值為 NaN——條件函式必須把 NaN 視為「不成立」,
    不可當成 0 或 True (那會讓「沒資料」被誤判成「條件達成」)。
    """
    if df is None or len(df) == 0:
        return df
    out = df.copy()
    bar_dates = _as_date_str(out.index)

    def _merge(chip_frame, value_map):
        """把 chip_frame 的欄位依「最後一筆不晚於門檻日」對齊到每根 K 棒。"""
        prepared = _prepare_chip_frame(chip_frame)
        if prepared is None:
            for col in value_map.values():
                out[col] = float('nan')
            return
        # 【未來函數防護的實作點】
        # allow_same_day=False → 門檻是「K棒日期的前一天」,用 asof 找 <= 門檻
        #   的最後一筆,等同「T 日只能看到 T-1 日(含)以前公布的籌碼」。
        # allow_same_day=True  → 門檻就是 K 棒當日 (研究用,含未來函數)。
        left = pd.DataFrame({'_bar_date': bar_dates.values})
        left['_key'] = pd.to_datetime(left['_bar_date'])
        if not allow_same_day:
            left['_key'] = left['_key'] - pd.Timedelta(days=1)
        left = left.reset_index().rename(columns={'index': '_pos'})
        right = prepared.copy()
        right['_key'] = pd.to_datetime(right['Date'])
        cols_needed = [c for c in value_map if c in right.columns]
        if not cols_needed:
            for col in value_map.values():
                out[col] = float('nan')
            return
        merged = pd.merge_asof(
            left.sort_values('_key'), right[['_key'] + cols_needed].sort_values('_key'),
            on='_key', direction='backward')
        merged = merged.sort_values('_pos')
        for src, dst in value_map.items():
            out[dst] = (pd.to_numeric(merged[src], errors='coerce').values
                        if src in merged.columns else float('nan'))

    _merge(stock_chips, {'Foreign': COL_FOREIGN, 'Trust': COL_TRUST,
                         'Dealer': COL_DEALER, 'InstTotal': COL_INST_TOTAL})
    _merge(margin_chips, {'MarginBalance': COL_MARGIN, 'ShortBalance': COL_SHORT})

    if futures_chips is not None and futures_product:
        fc = _prepare_chip_frame(futures_chips)
        if fc is not None and 'Product' in fc.columns and 'Investor' in fc.columns:
            fc = fc[fc['Product'].astype(str) == str(futures_product)]
            for investor, col in (('外資及陸資', COL_FUT_FOREIGN_OI),
                                  ('投信', COL_FUT_TRUST_OI),
                                  ('自營商', COL_FUT_DEALER_OI)):
                part = fc[fc['Investor'].astype(str) == investor]
                _merge(part[['Date', 'NetOI']].rename(columns={'NetOI': 'Foreign'})
                       if not part.empty else None, {'Foreign': col})
        else:
            for col in FUT_COLS:
                out[col] = float('nan')
    else:
        for col in FUT_COLS:
            out[col] = float('nan')
    return out


def has_chip_data(df, cols=None):
    """df 裡是否真的有可用的籌碼數值 (至少一根不是 NaN)。

    條件函式用它做前置檢查:沒有資料時直接回 False 並讓上層記錄原因,
    而不是拿一堆 NaN 去比大小 (NaN 比較永遠 False,看起來像「條件沒到」,
    但實際是「根本沒資料」——這兩件事對使用者的意義完全不同)。
    """
    if df is None or len(df) == 0:
        return False
    for c in (cols or ALL_CHIP_COLS):
        if c in df.columns and pd.to_numeric(df[c], errors='coerce').notna().any():
            return True
    return False


class ChipDataMissing(Exception):
    """籌碼欄位不存在或全為 NaN。

    刻意用例外而不是回傳 False:eval_conditions 會把例外訊息收進 errors,
    GUI 就能明確告訴使用者「本地沒有這段期間的籌碼,請先到籌碼分頁更新」,
    而不是讓使用者對著一個永遠不成立的條件猜原因 (ADR-065 的教訓)。
    """


def series_of(df, col):
    """取出籌碼欄位的數值 Series;欄位不存在或全 NaN 就拋 ChipDataMissing。"""
    if df is None or col not in df.columns:
        raise ChipDataMissing(f"df 沒有 {col} 欄位 (可能未載入籌碼)")
    s = pd.to_numeric(df[col], errors='coerce')
    if not s.notna().any():
        raise ChipDataMissing(f"{col} 全部為空 (本地缺這段期間的籌碼,請先更新籌碼)")
    return s


def consecutive_net(series, n, positive=True):
    """最後 n 根是否連續買超 (positive=True) 或連續賣超。

    NaN 一律視為不成立 (沒資料 ≠ 條件達成)。
    """
    if n <= 0 or len(series) < n:
        return False
    tail = series.iloc[-n:]
    if tail.isna().any():
        return False
    return bool((tail > 0).all()) if positive else bool((tail < 0).all())


def turned_positive(series):
    """由賣超(<=0) 轉為買超(>0) 的那一根 (前一根<=0 且這根>0)。"""
    if len(series) < 2:
        return False
    prev, cur = series.iloc[-2], series.iloc[-1]
    if pd.isna(prev) or pd.isna(cur):
        return False
    return bool(prev <= 0 < cur)


def turned_negative(series):
    """由買超(>=0) 轉為賣超(<0) 的那一根。"""
    if len(series) < 2:
        return False
    prev, cur = series.iloc[-2], series.iloc[-1]
    if pd.isna(prev) or pd.isna(cur):
        return False
    return bool(prev >= 0 > cur)


def consecutive_decreasing(series, n):
    """最後 n 根是否連續下降 (用於融資餘額連減 = 散戶退場)。

    需要 n+1 個點才能算出 n 次變化。
    """
    if n <= 0 or len(series) < n + 1:
        return False
    tail = series.iloc[-(n + 1):]
    if tail.isna().any():
        return False
    return bool((tail.diff().dropna() < 0).all())


def consecutive_increasing(series, n):
    """最後 n 根是否連續上升。"""
    if n <= 0 or len(series) < n + 1:
        return False
    tail = series.iloc[-(n + 1):]
    if tail.isna().any():
        return False
    return bool((tail.diff().dropna() > 0).all())
