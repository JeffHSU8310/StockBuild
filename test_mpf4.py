import pandas as pd
import mplfinance as mpf
import numpy as np

df = pd.DataFrame({
    'Open': [100, 101, 102], 'High': [102, 103, 104], 'Low': [99, 100, 101], 'Close': [101, 102, 103],
    'Volume': [1000, 2000, 3000]
}, index=pd.date_range('2023-01-01', periods=3))

df['MACD'] = [1, 2, 3]
df['Signal'] = [1.5, 2.5, 3.5]
df['Hist'] = [-0.5, -0.5, -0.5]

apds = [
    mpf.make_addplot(df['MACD'], panel=2, color='red', secondary_y=False),
    mpf.make_addplot(df['Signal'], panel=2, color='blue', secondary_y=False),
    mpf.make_addplot(df['Hist'], type='bar', panel=2, color='green', secondary_y=False, ylabel='MACD')
]

fig, axlist = mpf.plot(df, type='candle', volume=True, addplot=apds, returnfig=True, panel_ratios=[5, 1, 1.2])

for i, ax in enumerate(axlist):
    print(f"ax[{i}]: ylabel={ax.get_ylabel()}, lines={len(ax.lines)}, patches={len(ax.patches)}")
