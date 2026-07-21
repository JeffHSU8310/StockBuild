import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib.pyplot as plt

# Generate 1150 candles
N = 1150
np.random.seed(42)
close = 100 + np.random.randn(N).cumsum()
open_ = close + np.random.randn(N)
high = np.maximum(close, open_) + np.random.rand(N)
low = np.minimum(close, open_) - np.random.rand(N)
volume = np.random.randint(10000, 150000000, N)

df = pd.DataFrame({
    'Open': open_, 'High': high, 'Low': low, 'Close': close, 'Volume': volume
}, index=pd.date_range('2020-01-01', periods=N))

mc = mpf.make_marketcolors(up='#FF1744', down='#00E676', edge='inherit', wick='inherit', volume='inherit', ohlc='inherit')
xq_style = mpf.make_mpf_style(marketcolors=mc, facecolor='#12161A', figcolor='#1A2026')

fig, axlist = mpf.plot(df, type='candle', volume=True, style=xq_style, returnfig=True, figsize=(10, 6))

fig.savefig('g:/StockBuild-Antigravity/test_vol.png')
print("Saved test_vol.png")
