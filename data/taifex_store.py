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


def store_path(base_dir, taifex_prod, session='all', month_rank=1):
    """【ADR-058/ADR-081】兩種盤別口徑及不同連續月份 (R1/R2/R3) 分開儲存:
        TX.csv        = 近一連續 (R1), 近全 (session='all')
        TX_day.csv    = 近一連續 (R1), 只有日盤 (session='day')
        TX_R2.csv     = 次月連續 (R2), 近全 (session='all')
        TX_R2_day.csv = 次月連續 (R2), 只有日盤 (session='day')
    """
    name = str(taifex_prod).strip().upper()
    rank = int(month_rank)
    if rank > 1:
        name += f"_R{rank}"
    if str(session) == 'day':
        name += '_day'
    return os.path.join(base_dir, SUBDIR, f"{name}.csv")


def has_daily(base_dir, taifex_prod, session='all', month_rank=1):
    """該商品/口徑/連續月份的本地檔案是否存在。"""
    return os.path.exists(store_path(base_dir, taifex_prod, session, month_rank=month_rank))


def load_daily(base_dir, taifex_prod, session='all', month_rank=1):
    """載入某商品的期交所日K (支援 R1/R2/R3)。"""
    path = store_path(base_dir, taifex_prod, session, month_rank=month_rank)
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


def save_daily(base_dir, taifex_prod, df, session='all', month_rank=1):
    """寫入某商品的期交所日K (支援 R1/R2/R3, 整檔覆蓋)。回傳實際寫入路徑。"""
    path = store_path(base_dir, taifex_prod, session, month_rank=month_rank)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = df[[c for c in _COLS if c in df.columns]].sort_index()
    out.to_csv(path, index_label='Date')
    return path
