"""SQLite K 線歷史儲存（ADR-142）。

每次操作開獨立 connection，可從背景 worker 呼叫；以
`(symbol, asset_type, timeframe, ts)` 為主鍵做增量 upsert。
"""
import os
import sqlite3

import pandas as pd


def initialize(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with sqlite3.connect(path, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kbars (
                symbol TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, asset_type, timeframe, ts)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kbars_lookup "
                     "ON kbars(symbol, asset_type, timeframe, ts)")
    return path


def upsert(path, symbol, asset_type, timeframe, df):
    if df is None or df.empty:
        return 0
    initialize(path)
    rows = []
    for ts, row in df.iterrows():
        try:
            rows.append((str(symbol), str(asset_type), str(timeframe),
                         pd.Timestamp(ts).isoformat(), float(row['Open']),
                         float(row['High']), float(row['Low']), float(row['Close']),
                         float(row.get('Volume', 0) or 0)))
        except (TypeError, ValueError, KeyError):
            continue
    with sqlite3.connect(path, timeout=30) as conn:
        conn.executemany("""
            INSERT INTO kbars(symbol, asset_type, timeframe, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, asset_type, timeframe, ts) DO UPDATE SET
              open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, volume=excluded.volume,
              updated_at=CURRENT_TIMESTAMP
        """, rows)
    return len(rows)


def load(path, symbol, asset_type, timeframe, start=None, end=None):
    if not os.path.exists(path):
        return None
    where = ["symbol=?", "asset_type=?", "timeframe=?"]
    args = [str(symbol), str(asset_type), str(timeframe)]
    if start is not None:
        where.append("ts>=?"); args.append(pd.Timestamp(start).isoformat())
    if end is not None:
        where.append("ts<=?"); args.append(pd.Timestamp(end).isoformat())
    with sqlite3.connect(path, timeout=30) as conn:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM kbars WHERE "
            + " AND ".join(where) + " ORDER BY ts", args).fetchall()
    if not rows:
        return None
    out = pd.DataFrame(rows, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume'])
    out.index = pd.to_datetime(out.pop('ts'))
    return out


def coverage(path, symbol, asset_type, timeframe):
    if not os.path.exists(path):
        return None, None, 0
    with sqlite3.connect(path, timeout=30) as conn:
        row = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM kbars "
            "WHERE symbol=? AND asset_type=? AND timeframe=?",
            (str(symbol), str(asset_type), str(timeframe))).fetchone()
    return row if row and row[2] else (None, None, 0)
