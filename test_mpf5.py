import pandas as pd
import mplfinance as mpf
import numpy as np

df = pd.DataFrame({
    'Open': [100, 101, 102], 'High': [102, 103, 104], 'Low': [99, 100, 101], 'Close': [101, 102, 103],
    'Volume': [1000, np.nan, 3000]
}, index=pd.date_range('2023-01-01', periods=3))

fig, axlist = mpf.plot(df, type='candle', volume=True, returnfig=True)
print("Volume patches:", len(axlist[2].patches))
