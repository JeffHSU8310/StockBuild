import pandas as pd
import mplfinance as mpf

df = pd.DataFrame({
    'Open': [100, 101], 'High': [102, 103], 'Low': [99, 100], 'Close': [101, 102],
    'Volume': [1000, 2000]
}, index=pd.date_range('2023-01-01', periods=2))

apds = [
    mpf.make_addplot([1, 2], panel=2)
]

fig, axlist = mpf.plot(df, type='candle', volume=True, addplot=apds, returnfig=True, panel_ratios=[5, 1, 1])
print("len(axlist):", len(axlist))
for i, ax in enumerate(axlist):
    print(f"ax[{i}]:", ax.get_ylabel(), "pos:", ax.get_position())
