# 策略名稱: 5555
# 商品: TXF  週期: 日K  數量: 1
# 自訂參數: fast=20, slow=60
# 由 StockBuild 匯出於 2026-07-18 22:19:13

def on_bar(ctx):
    
    # 透過 ctx.param() 讀取或設定參數，短線預設為 20，長線預設為 60
    fast_period = ctx.param('fast', 20)
    slow_period = ctx.param('slow', 60)
    
    # 取得短線 20 EMA 與長線 60 SMA 指標
    fast_ema = ctx.ema(fast_period)
    slow_sma = ctx.sma(slow_period)

    # 黃金交叉: 短線(20EMA)上穿長線(60SMA)
    golden_cross = ctx.cross_up(fast_ema, slow_sma)

    # 死亡交叉: 短線(20EMA)下穿長線(60SMA)
    death_cross = ctx.cross_down(fast_ema, slow_sma)

    # 空手時，黃金交叉買進
    if ctx.position == 'FLAT':
        if golden_cross:
            return ctx.buy()
        return ctx.hold()

    # 持有多頭部位時，死亡交叉平倉並反手做空
    if ctx.position == 'LONG':
        if death_cross:
            return ctx.sell()
        return ctx.hold()

    # 持有空頭部位時，黃金交叉平倉並反手做多
    if ctx.position == 'SHORT':
        if golden_cross:
            return ctx.buy()
        return ctx.hold()

    return ctx.hold()
