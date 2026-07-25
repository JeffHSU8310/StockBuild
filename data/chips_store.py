# -*- coding: utf-8 -*-
"""
data/chips_store.py — 籌碼資料的本地 CSV 儲存 (ADR-100)

沿用 data/taifex_store.py (ADR-049) 的模式:純檔案 I/O,零 tkinter、零網路,
可離線單元測試。路徑一律由呼叫端顯式傳入 base_dir,不自己決定放哪。

【檔案配置】base_dir/chips/ 底下:
    market_inst.csv          市場三大法人買賣金額 (每日一列,累積)
    margin.csv               市場融資融券餘額     (每日一列,累積)
    futures_inst_YYYY.csv    期貨三大法人未平倉   (按年分檔)
    stock_inst_YYYY-MM.csv   個股三大法人買賣超   (按年月分檔)

個股籌碼按「年月」分檔而不是像 taifex 那樣單一大檔,是因為量級差兩個數量級:
期貨法人一天約 69 列,個股一天約 2200 列 (上市 1336 + 上櫃 895)。單日 CSV
約 160KB,一年 37MB——放進單一檔案會讓每次增量更新都要讀寫整個大檔。
按年月切成 ~3.5MB 的檔案,增量更新只碰當月那一檔。

【冪等性】所有寫入都是「同日期覆蓋、不同日期追加」。重複匯入同一天不會
產生重複列,這是「日後不用重複抓取」能成立的前提 (見 available_dates())。
"""
import os

import pandas as pd

SUBDIR = 'chips'

MARKET_INST_FILE = 'market_inst.csv'
MARGIN_FILE = 'margin.csv'


def chips_dir(base_dir):
    return os.path.join(base_dir, SUBDIR)


def _ensure_dir(path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def market_inst_path(base_dir):
    return os.path.join(chips_dir(base_dir), MARKET_INST_FILE)


def margin_path(base_dir):
    return os.path.join(chips_dir(base_dir), MARGIN_FILE)


def futures_inst_path(base_dir, year):
    return os.path.join(chips_dir(base_dir), f'futures_inst_{int(year)}.csv')


def stock_inst_path(base_dir, date_iso):
    """個股籌碼按年月分檔;date_iso 形如 '2026-07-24' → stock_inst_2026-07.csv。"""
    ym = str(date_iso)[:7]
    return os.path.join(chips_dir(base_dir), f'stock_inst_{ym}.csv')


def _read_csv(path):
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, dtype={'Code': str})
    except Exception:
        return None
    return df if not df.empty else None


def load(path):
    """讀單一 CSV;不存在或壞掉回傳 None (不拋例外,讓上層決定要不要重抓)。"""
    return _read_csv(path)


def upsert(path, df, date_col='Date'):
    """把 df 併進 path 的 CSV:df 帶到的日期整批覆蓋,其餘日期保留。

    回傳寫入後的完整 DataFrame。df 為空時不動檔案、直接回傳既有內容。
    """
    if df is None or df.empty:
        return _read_csv(path)
    _ensure_dir(path)
    old = _read_csv(path)
    if old is not None and date_col in old.columns:
        incoming = set(df[date_col].astype(str))
        old = old[~old[date_col].astype(str).isin(incoming)]
        merged = pd.concat([old, df], ignore_index=True)
    else:
        merged = df.copy()
    sort_cols = [c for c in (date_col, 'Code', 'Product', 'Investor') if c in merged.columns]
    if sort_cols:
        merged = merged.sort_values(sort_cols, kind='stable').reset_index(drop=True)
    merged.to_csv(path, index=False, encoding='utf-8')
    return merged


def available_dates(path, date_col='Date'):
    """回傳這個檔案裡已經有的日期集合 (字串)。檔案不存在回空集合。

    增量下載靠這個判斷「哪幾天還沒抓」,避免重複打官方網站——這是使用者
    要求的「存起來、日後不用重複抓取」的實作核心。
    """
    df = _read_csv(path)
    if df is None or date_col not in df.columns:
        return set()
    return set(df[date_col].astype(str))


def stock_inst_available_dates(base_dir, start_iso, end_iso):
    """掃過區間涵蓋的所有年月分檔,收集個股籌碼已有的日期。"""
    got = set()
    for ym in _months_between(start_iso, end_iso):
        p = os.path.join(chips_dir(base_dir), f'stock_inst_{ym}.csv')
        got |= available_dates(p)
    return got


def _months_between(start_iso, end_iso):
    """'2025-07-25','2026-07-24' → ['2025-07','2025-08',...,'2026-07']。"""
    try:
        sy, sm = int(str(start_iso)[:4]), int(str(start_iso)[5:7])
        ey, em = int(str(end_iso)[:4]), int(str(end_iso)[5:7])
    except (ValueError, IndexError):
        return []
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def load_stock_inst_range(base_dir, start_iso, end_iso):
    """讀取區間內的個股籌碼 (跨年月分檔合併)。無資料回傳 None。"""
    frames = []
    for ym in _months_between(start_iso, end_iso):
        df = _read_csv(os.path.join(chips_dir(base_dir), f'stock_inst_{ym}.csv'))
        if df is not None:
            frames.append(df)
    if not frames:
        return None
    all_df = pd.concat(frames, ignore_index=True)
    d = all_df['Date'].astype(str)
    return all_df[(d >= str(start_iso)) & (d <= str(end_iso))].reset_index(drop=True)


def load_futures_inst_range(base_dir, start_iso, end_iso):
    """讀取區間內的期貨籌碼 (跨年分檔合併)。無資料回傳 None。"""
    try:
        sy, ey = int(str(start_iso)[:4]), int(str(end_iso)[:4])
    except (ValueError, IndexError):
        return None
    frames = []
    for y in range(sy, ey + 1):
        df = _read_csv(futures_inst_path(base_dir, y))
        if df is not None:
            frames.append(df)
    if not frames:
        return None
    all_df = pd.concat(frames, ignore_index=True)
    d = all_df['Date'].astype(str)
    return all_df[(d >= str(start_iso)) & (d <= str(end_iso))].reset_index(drop=True)
