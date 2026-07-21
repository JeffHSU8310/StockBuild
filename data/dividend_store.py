import os
import json
import io
from datetime import datetime
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(__file__), 'dividends_cache')

def _ensure_cache_dir():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

def _fetch_yfinance_history(symbol):
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()
        
    df = pd.DataFrame()
    for suffix in [".TW", ".TWO"]:
        try:
            # 使用 auto_adjust=False 以取得未除權息(但已除權)的 Close，以及完全還原的 Adj Close
            df = yf.Ticker(f"{symbol}{suffix}").history(period="20y", auto_adjust=False)
            if not df.empty:
                break
        except Exception:
            continue
            
    if not df.empty:
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()
    return df

def adjust_dataframe(df, symbol):
    """
    【ADR-063】利用 Yahoo Finance (Adj Close) 進行全自動向過去還原 K 線。
    不僅能還原除權息，也能完美還原「股票分割」與「減資」，徹底解決圖表斷層與回測失真問題。
    此算法動態比對目標 df 的收盤價與 Yahoo 還原價，精準產生每日調整乘數。
    """
    if df.empty or not symbol.isdigit():
        return False
        
    _ensure_cache_dir()
    cache_file = os.path.join(CACHE_DIR, f"{symbol}_yf.json")
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    df_yf = pd.DataFrame()
    # 1. 嘗試從本地快取讀取
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cdata = json.load(f)
                if cdata.get('last_update') == today_str:
                    df_yf = pd.read_json(io.StringIO(cdata['data']), orient='split')
                    df_yf.index = pd.to_datetime(df_yf.index)
        except Exception:
            pass
            
    # 2. 若無快取或過期，從網路抓取並寫入快取
    if df_yf.empty:
        df_yf = _fetch_yfinance_history(symbol)
        if not df_yf.empty and 'Close' in df_yf.columns and 'Adj Close' in df_yf.columns:
            try:
                # 只存需要的欄位以節省空間
                cache_df = df_yf[['Close', 'Adj Close']]
                cdata = {
                    'last_update': today_str,
                    'data': cache_df.to_json(orient='split', date_format='iso')
                }
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(cdata, f)
            except Exception:
                pass
                
    if df_yf.empty or 'Adj Close' not in df_yf.columns:
        return False

    # 3. 準備目標 dataframe 的每日收盤價
    # 無論 df 是日K、周K 或 1分K，我們都用日期對應
    if df.index.tz is not None:
        df_dates = df.index.tz_localize(None).normalize()
    else:
        df_dates = df.index.normalize()
        
    temp_df = pd.DataFrame({'Close': df['Close']}, index=df_dates)
    # 取每日最後一筆 (收盤價) 當作當天的比較基準
    daily_close = temp_df.groupby(temp_df.index).last()
    
    # 4. 計算還原乘數 (Adj Ratio) = Yahoo 完全還原價 / 目標 K 線收盤價
    # 這裡的交集會自動彌平 Shioaji 與 Yahoo 的斷層
    merged = daily_close.join(df_yf[['Adj Close']], rsuffix='_yf')
    merged['ratio'] = merged['Adj Close'] / merged['Close']
    
    # 針對沒有 Yahoo 資料的日期 (例如剛上市、或是美股休市台股有開盤的補班日)
    # 先 forward-fill (沿用昨天的乘數)，若最前面有缺失再 backward-fill
    merged['ratio'] = merged['ratio'].ffill().bfill().fillna(1.0)
    
    # 確保基準點錨定於最新一筆 = 1.0 (現在的價格不動)
    current_ratio = merged['ratio'].iloc[-1]
    if current_ratio > 0:
        merged['ratio'] = merged['ratio'] / current_ratio
        
    # 5. 將每日的還原乘數廣播 (Map) 回原始 df
    ratio_series = pd.Series(merged['ratio'], index=merged.index)
    df['adj_ratio'] = df_dates.map(ratio_series).fillna(1.0)
    
    # 6. 套用還原乘數
    for col in ['Open', 'High', 'Low', 'Close']:
        if col in df.columns:
            df[col] = df[col] * df['adj_ratio']
            
    if 'Volume' in df.columns:
        df['Volume'] = df['Volume'] / df['adj_ratio']
        
    df.drop(columns=['adj_ratio'], inplace=True)
    return True
