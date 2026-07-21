# 策略名稱: test
# 商品: TXF  週期: 日K  數量: 1
# 自訂參數: fast_period=7, slow_period=30
# 由 StockBuild 匯出於 2026-07-18 21:06:51

def on_bar(ctx):
    """
    長線30SMA，短線7EMA
    黃金交叉買進，死亡交叉賣出
    空手時黃金交叉做多，死亡交叉反手做空，不斷循環
    """
    
    # 捨棄 ctx.param，直接寫死長短線參數，確保回測平台不會抓錯數值
    fast_period = ctx.param('fast', 20)
    slow_period = ctx.param('slow', 60)

    fast_ema = ctx.ema(fast_period)
    slow_sma = ctx.sma(slow_period)

    # 黃金交叉: 短線(20EMA)上穿長線(60SMA)
    golden_cross = ctx.cross_up(fast_ema, slow_sma)

    # 死亡交叉: 短線(20EMA)下穿長線(60SMA)
    death_cross = ctx.cross_down(fast_ema, slow_sma)

    # 狀態 1: 空手時，黃金交叉買進做多
    if ctx.position == 'FLAT':
        if golden_cross:
            return ctx.buy()

    # 狀態 2: 持有多頭部位時，死亡交叉平倉並反手做空
    elif ctx.position == 'LONG':
        if death_cross:
            return ctx.sell()

    # 狀態 3: 持有空頭部位時，黃金交叉平倉並反手做多
    elif ctx.position == 'SHORT':
        if golden_cross:
            return ctx.buy()

    # 官方規範: 若無任何動作，回傳 None
    return None