import pandas as pd
import mplfinance as mpf
import numpy as np

df = pd.DataFrame({
    'Open': [100, 101, 102], 'High': [102, 103, 104], 'Low': [99, 100, 101], 'Close': [101, 102, 103],
    'Volume': [1000, 2000, 3000]
}, index=pd.date_range('2023-01-01', periods=3))

mc = mpf.make_marketcolors(up='#FF1744', down='#00E676', edge='inherit', wick='inherit', volume='inherit', ohlc='inherit')
xq_style = mpf.make_mpf_style(marketcolors=mc, facecolor='#12161A', figcolor='#1A2026')

fig, axlist = mpf.plot(df, type='candle', volume=True, style=xq_style, returnfig=True)
for p in axlist[2].patches:
    print(f"Volume patch: facecolor={p.get_facecolor()}, edgecolor={p.get_edgecolor()}, alpha={p.get_alpha()}")
