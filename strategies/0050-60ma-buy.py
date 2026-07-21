# 策略名稱: 0050
# 商品: 0050  週期: 日K  數量: 1
# 自訂參數: fast_period=60
# 由 StockBuild 匯出於 2026-07-18 21:57:08

def on_bar(ctx):
    """
    策略：當股價跌破60 SMA 之後，就買進
    """
    fast_period = ctx.param('fast', 60)
    
    #取得60日均線
    
    fast_sma = ctx.sma(fast_period)

    # 當股價跌破60 SMA時買進
    if ctx.position == 'FLAT' and ctx.close < fast_sma.iloc[-1]:
        return ctx.buy()

    return ctx.hold()