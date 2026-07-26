# -*- coding: utf-8 -*-
"""
data/market_store.py — 全市場日K與基本面的本地 CSV 儲存 (ADR-103)

沿用 data/chips_store.py (ADR-100) 已驗證的模式:純檔案 I/O、零網路、
零 tkinter,路徑由呼叫端顯式傳入,可離線單元測試。

【檔案配置】base_dir/market/ 底下:
    daily_YYYY-MM.csv      全市場日 OHLCV (按年月分檔)
    fundamental.csv        基本面快照 (每檔一列,覆蓋式更新)

日K 按年月分檔的理由與籌碼相同:全市場一天約 2300 檔,單日約 150KB,
一年 36MB;切成月檔後增量更新只碰當月那一檔。

基本面則是「每檔一列的當前快照」而非時間序列:本益比/EPS/營收年增率
這些欄位使用者關心的是「現在的值」,保留歷史對選股沒有實質幫助,卻會讓
檔案膨脹數十倍。需要歷史時可從日K重算價格類指標。
"""
import os

import pandas as pd

SUBDIR = 'market'
FUNDAMENTAL_FILE = 'fundamental.csv'


def market_dir(base_dir):
    return os.path.join(base_dir, SUBDIR)


def daily_path(base_dir, date_iso):
    return os.path.join(market_dir(base_dir), f'daily_{str(date_iso)[:7]}.csv')


def fundamental_path(base_dir):
    return os.path.join(market_dir(base_dir), FUNDAMENTAL_FILE)


def _ensure_dir(path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _read(path):
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, dtype={'Code': str})
    except Exception:
        return None
    return df if not df.empty else None


def load(path):
    return _read(path)


def upsert_daily(base_dir, df):
    """把一批全市場日K寫入對應年月檔;同 (Date, Code) 覆蓋、其餘保留。"""
    if df is None or df.empty or 'Date' not in df.columns:
        return None
    written = []
    for ym, part in df.groupby(df['Date'].astype(str).str[:7]):
        path = os.path.join(market_dir(base_dir), f'daily_{ym}.csv')
        _ensure_dir(path)
        old = _read(path)
        if old is not None and {'Date', 'Code'} <= set(old.columns):
            keys = set(zip(part['Date'].astype(str), part['Code'].astype(str)))
            mask = [(d, c) not in keys for d, c in
                    zip(old['Date'].astype(str), old['Code'].astype(str))]
            merged = pd.concat([old[mask], part], ignore_index=True)
        else:
            merged = part.copy()
        merged = merged.sort_values(['Date', 'Code'], kind='stable').reset_index(drop=True)
        merged.to_csv(path, index=False, encoding='utf-8')
        written.append(path)
    return written


def save_fundamental(base_dir, df):
    """基本面快照:整批覆蓋 (它本來就是「現在的值」,不保留歷史)。"""
    if df is None or df.empty:
        return None
    path = fundamental_path(base_dir)
    _ensure_dir(path)
    df.to_csv(path, index=False, encoding='utf-8')
    return path


def load_fundamental(base_dir):
    return _read(fundamental_path(base_dir))


def _months_between(start_iso, end_iso):
    try:
        sy, sm = int(str(start_iso)[:4]), int(str(start_iso)[5:7])
        ey, em = int(str(end_iso)[:4]), int(str(end_iso)[5:7])
    except (ValueError, IndexError):
        return []
    out, y, m = [], sy, sm
    while (y, m) <= (ey, em):
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def load_daily_range(base_dir, start_iso, end_iso, codes=None):
    """讀區間內的全市場日K;codes 可只取部分標的 (省記憶體)。"""
    frames = []
    for ym in _months_between(start_iso, end_iso):
        df = _read(os.path.join(market_dir(base_dir), f'daily_{ym}.csv'))
        if df is None:
            continue
        if codes is not None:
            df = df[df['Code'].astype(str).isin(set(codes))]
        if not df.empty:
            frames.append(df)
    if not frames:
        return None
    all_df = pd.concat(frames, ignore_index=True)
    d = all_df['Date'].astype(str)
    out = all_df[(d >= str(start_iso)) & (d <= str(end_iso))]
    return out.reset_index(drop=True) if not out.empty else None


def available_daily_dates(base_dir, start_iso, end_iso):
    """區間內已經抓過的日期集合,供增量下載判斷 (同 ADR-100 的不重抓機制)。"""
    got = set()
    for ym in _months_between(start_iso, end_iso):
        df = _read(os.path.join(market_dir(base_dir), f'daily_{ym}.csv'))
        if df is not None and 'Date' in df.columns:
            got |= set(df['Date'].astype(str))
    return got


def to_ohlcv_frame(daily_df, code):
    """把全市場長表轉成單一標的的 OHLCV DataFrame (DatetimeIndex)。

    技術面條件 (strategy_engine.CONDITIONS) 吃的就是這個格式,
    因此選股可以直接重用策略引擎的全部條件,不必另寫一套。
    """
    if daily_df is None or daily_df.empty:
        return None
    sub = daily_df[daily_df['Code'].astype(str) == str(code)]
    if sub.empty:
        return None
    sub = sub.sort_values('Date', kind='stable')
    out = pd.DataFrame({
        'Open': pd.to_numeric(sub['Open'], errors='coerce'),
        'High': pd.to_numeric(sub['High'], errors='coerce'),
        'Low': pd.to_numeric(sub['Low'], errors='coerce'),
        'Close': pd.to_numeric(sub['Close'], errors='coerce'),
        'Volume': pd.to_numeric(sub['Volume'], errors='coerce').fillna(0.0),
    })
    out.index = pd.to_datetime(sub['Date'], errors='coerce')
    out = out[out.index.notna()].dropna(subset=['Close'])
    return out if not out.empty else None
