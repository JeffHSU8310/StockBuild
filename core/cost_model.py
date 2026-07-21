# -*- coding: utf-8 -*-
"""
core/cost_model.py — 台股/台期貨交易成本模型 (回測與模擬帳戶共用)

【ADR-050】回測報告「總成本(費用)」恆為 0 的根因:
  stock_app_pro.py 呼叫 backtest.run_backtest(..., fee_rate=0.0) —— 費率寫死 0,
  等於假設零手續費、零交易稅。這會系統性高估績效 (使用者實例:TXF 日K 回測
  27 筆交易顯示總成本 0),對高頻策略尤其失真。

本模組把台灣實際費用結構寫成純函式,可離線測試:

  股票 (整股/零股):
    手續費 = 成交金額 × 0.1425% × 折扣  (單邊,買賣皆收)
    最低手續費 20 元 (多數券商;零股常見 1 元,可由參數調整)
    證交稅 = 賣出金額 × 0.3%  (ETF 為 0.1%,當沖減半此處不模擬)

  期貨:
    手續費 = 每口固定金額 × 口數  (單邊,依券商;預設 50 元)
    期交稅 = 契約金額 × 0.002%  (十萬分之二,買賣皆收)

預設值可由呼叫端覆寫 (不同券商折扣不同);所有數字集中在此,不散落各處。
"""

# --- 預設費率 (可由 params 覆寫) ---
STOCK_FEE_RATE = 0.001425      # 券商手續費率 (單邊)
STOCK_FEE_DISCOUNT = 1.0       # 折扣 (0.6 = 六折;預設不打折)
STOCK_FEE_MIN = 20.0           # 單筆最低手續費 (整股)
ODD_FEE_MIN = 1.0              # 零股單筆最低手續費
STOCK_TAX_RATE = 0.003         # 證交稅 (賣出)
ETF_TAX_RATE = 0.001           # ETF 證交稅 (賣出)
FUT_FEE_PER_LOT = 50.0         # 期貨手續費 (單邊每口)
FUT_TAX_RATE = 0.00002         # 期交稅 (十萬分之二,買賣皆收)


def is_etf(symbol):
    """台股 ETF 代碼判斷:00 開頭 (0050/006208/00878/00631L...)。"""
    s = str(symbol or '').strip().upper()
    return s.startswith('00') and len(s) >= 4


def _p(params, key, default):
    if not params:
        return default
    v = params.get(key)
    return default if v is None else v


def side_cost(trade_type, symbol, price, qty, contract_size, is_sell, params=None):
    """
    單邊 (一次買進 或 一次賣出) 的成本。回傳 (fee, tax)。

      trade_type   : '股票' / '零股' / '期貨'
      price        : 成交價
      qty          : 張/股/口 數
      contract_size: 每單位規模 (股票=1000、零股=1、期貨=契約乘數)
      is_sell      : 是否為賣出方向 (證交稅只在賣出收)
    """
    price = abs(float(price)); qty = int(qty); contract_size = float(contract_size)
    amount = price * qty * contract_size
    if trade_type == '期貨':
        fee = _p(params, 'fut_fee_per_lot', FUT_FEE_PER_LOT) * qty
        tax = amount * _p(params, 'fut_tax_rate', FUT_TAX_RATE)
        return float(fee), float(tax)
    # 股票 / 零股
    rate = _p(params, 'stock_fee_rate', STOCK_FEE_RATE) * _p(params, 'fee_discount', STOCK_FEE_DISCOUNT)
    fee = amount * rate
    fee_min = _p(params, 'odd_fee_min', ODD_FEE_MIN) if trade_type == '零股' \
        else _p(params, 'stock_fee_min', STOCK_FEE_MIN)
    if amount > 0:
        fee = max(fee, float(fee_min))
    tax = 0.0
    if is_sell:
        tax_rate = _p(params, 'etf_tax_rate', ETF_TAX_RATE) if is_etf(symbol) \
            else _p(params, 'stock_tax_rate', STOCK_TAX_RATE)
        tax = amount * tax_rate
    return float(fee), float(tax)


def round_trip_cost(trade_type, symbol, entry_price, exit_price, qty, contract_size,
                    direction='做多', params=None):
    """
    一趟完整交易 (進場 + 出場) 的成本明細。回傳 dict:
      {entry_fee, exit_fee, fee, tax, total}
    做多 = 買進進場 / 賣出出場;做空 = 賣出進場 / 買進出場 (期貨)。
    """
    entry_is_sell = (direction == '做空')
    e_fee, e_tax = side_cost(trade_type, symbol, entry_price, qty, contract_size,
                             entry_is_sell, params)
    x_fee, x_tax = side_cost(trade_type, symbol, exit_price, qty, contract_size,
                             not entry_is_sell, params)
    return {
        'entry_fee': e_fee, 'exit_fee': x_fee,
        'fee': e_fee + x_fee,
        'tax': e_tax + x_tax,
        'total': e_fee + x_fee + e_tax + x_tax,
    }


def describe(trade_type, params=None):
    """回傳目前套用費率的中文說明 (顯示在回測報告,讓使用者知道成本怎麼算的)。"""
    if trade_type == '期貨':
        return (f"期貨:手續費 {_p(params,'fut_fee_per_lot',FUT_FEE_PER_LOT):g} 元/口(單邊)"
                f" + 期交稅 {_p(params,'fut_tax_rate',FUT_TAX_RATE)*100:.3f}%(買賣皆收)")
    disc = _p(params, 'fee_discount', STOCK_FEE_DISCOUNT)
    rate = _p(params, 'stock_fee_rate', STOCK_FEE_RATE)
    return (f"{trade_type}:手續費 {rate*100:.4f}%×{disc:g}折(單邊,最低"
            f"{_p(params,'odd_fee_min',ODD_FEE_MIN) if trade_type=='零股' else _p(params,'stock_fee_min',STOCK_FEE_MIN):g}元)"
            f" + 證交稅 {_p(params,'stock_tax_rate',STOCK_TAX_RATE)*100:.2f}%(賣出;ETF {ETF_TAX_RATE*100:.2f}%)")
