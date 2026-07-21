# -*- coding: utf-8 -*-
"""
data/taifex_store.py — 期交所官方日K的本地儲存 (ADR-049)

每個期交所商品 (TX/MTX/TMF/TE/TF) 一個 CSV 檔,存放於基底目錄下的
`taifex_daily/` 子目錄,欄位 Date,Open,High,Low,Close,Volume。
零 tkinter、零 shioaji、零網路;僅 pandas + stdlib,可離線單元測試。
"""
import os

import pandas as pd

SUBDIR = "taifex_daily"
_COLS = ['Open', 'High', 'Low', 'Close', 'Volume']


def store_path(base_dir, taifex_prod, session='all'):
    """【ADR-058】兩種盤別口徑各存一個檔:
        TX.csv      = 近全 (含夜盤,session='all')
        TX_day.csv  = 只有日盤 (session='day')
    分開存而不是存一個檔多幾欄,是為了讓既有的 TX.csv 完全不受影響
    (使用者已經匯入的資料不會失效),新口徑只是「多一個檔」。"""
    name = str(taifex_prod).strip().upper()
    if str(session) == 'day':
        name += '_day'
    return os.path.join(base_dir, SUBDIR, f"{name}.csv")


def has_daily(base_dir, taifex_prod, session='all'):
    """該商品/口徑的本地檔案是否存在 (用來提示使用者需不需要重新匯入)。"""
    return os.path.exists(store_path(base_dir, taifex_prod, session))


def load_daily(base_dir, taifex_prod, session='all'):
    """載入某商品的期交所日K;檔案不存在或壞掉回傳空 DataFrame (不拋例外,
    呼叫端把「沒有匯入過」與「檔案毀損」都當成沒有延伸資料可用)。"""
    path = store_path(base_dir, taifex_prod, session)
    if not os.path.exists(path):
        return pd.DataFrame(columns=_COLS)
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df = df[[c for c in _COLS if c in df.columns]]
        df.index = pd.to_datetime(df.index)
        df.index.name = None
        return df.sort_index()
    except Exception:
        return pd.DataFrame(columns=_COLS)


def save_daily(base_dir, taifex_prod, df, session='all'):
    """寫入某商品的期交所日K (整檔覆蓋)。回傳實際寫入路徑。"""
    path = store_path(base_dir, taifex_prod, session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = df[[c for c in _COLS if c in df.columns]].sort_index()
    out.to_csv(path, index_label='Date')
    return path
