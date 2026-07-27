# -*- coding: utf-8 -*-
"""
core/paper_account.py — 內建虛擬模擬帳戶 (紙上交易記帳引擎)

【ADR-041】用途:量化策略「模擬模式」的成交不再只是日誌一行,而是記進一個
完整的虛擬帳戶 (虛擬資金/持倉/每筆交易史/已實現損益),讓使用者能以
「模擬帳戶跑一段時間績效沒問題 → 才改實單」的流程驗證策略。

為何不用永豐官方 simulation 模式 (ADR 有完整說明):
  1. 官方模擬環境的撮合「必定成交」,比實際樂觀,驗證價值有限;
  2. 需要第二條登入連線,會加倍 P-48 的 GIL 登入凍結問題;
  3. 本沙盒無法驗證官方模擬連線行為;內建帳戶可 100% 離線測試。

計價模型 (誠實簡化,常數可調):
  - 台股:1 張 = 1000 股。買進成本 = 價 ×1000×張 + 手續費 (0.1425%);
    賣出入帳 = 價 ×1000×張 − 手續費 − 證交稅 (0.3%)。
  - 台期貨:以「點數 × 契約乘數 × 口數」計已實現損益,平倉時直接加減現金;
    不建模保證金占用 (虛擬帳戶重點是策略損益,不是保證金管理)。
    乘數表:TXF=200, MXF=50, TMF=10;不在表中的商品乘數=1 並在交易備註標示。
  - 未實現損益用「最後標記價」(mark) 計算;equity = 現金 + 未實現。

零 tkinter / 零 shioaji,tests/test_core.py 可完整驗證。

【新ADR 多帳戶】使用者要求「多個模擬帳戶 (可自訂增加),避免同標的在同一個
帳戶多策略時產生衝突」——根因是 apply_fill() 的 positions 只用 symbol
當 key,兩個不同策略同時交易同一檔標的、方向不同時,後開倉的會直接覆蓋
前一筆的部位記錄 (股數/均價全部消失),不是累加也不是分開記。多帳戶的解法
是「把會衝突的策略分到不同帳戶」,而不是在單一帳戶內部用 (symbol, 策略)
複合 key 拆分——後者會讓「一個帳戶的權益/現金」失去意義 (兩個策略共用
一份現金,卻各自以為自己在獨立記帳)。

本模組維持「一個 dict = 一個帳戶」的既有設計不變 (帳戶字典的形狀、
apply_fill/mark_price/unrealized_pnl/equity 的簽名全部不動);「多帳戶」
純粹是 GUI 層維護一個 {account_id: 帳戶dict} 的容器,呼叫端各自傳對的
帳戶 dict 進來,本模組完全不需要知道「有多個帳戶」這件事——這樣才能繼續
保持零 tkinter/shioaji、可離線單元測試的既有保證 (ADR-009/011)。
"""
import uuid

STOCK_FEE_RATE = 0.001425     # 券商手續費 (單邊)
STOCK_TAX_RATE = 0.003        # 證交稅 (賣出)
FUTURES_FEE_PER_LOT = 50.0    # 期貨手續費估計 (單邊每口,含期交稅概估)
FUTURES_MULTIPLIER = {'TXF': 200.0, 'MXF': 50.0, 'TMF': 10.0}

DEFAULT_ACCOUNT_ID = 'default'


def new_account(initial_cash=1000000.0, name='預設帳戶', account_id=None):
    """【新ADR 多帳戶】account_id 不帶時自動產生 (uuid 短碼);呼叫端 (GUI 層)
    負責把回傳的 dict 存進自己維護的 {account_id: 帳戶} 容器。"""
    return {
        'id': account_id or uuid.uuid4().hex[:10],
        'name': str(name or '').strip() or '未命名帳戶',
        'initial_cash': float(initial_cash),
        'cash': float(initial_cash),
        'positions': {},   # key=symbol -> {market, direction(多/空), qty, avg_price, mark_price}
        'history': [],     # 每筆:{ts, symbol, market, action, kind, qty, price, fee, pnl, note}
        'realized_pnl': 0.0,
    }


def _fut_multiplier(symbol):
    sym = str(symbol).upper()
    for prefix, mult in FUTURES_MULTIPLIER.items():
        if sym.startswith(prefix):
            return mult, ''
    return 1.0, '(未知期貨乘數,以1計,損益僅點數)'


def apply_fill(acct, ts, market, symbol, action, kind, qty, price, trade_type=None):
    """
    記一筆成交。market='台股'/'台期貨';action='買進'/'賣出';kind='OPEN'/'CLOSE'。
    【ADR-043】trade_type='股票'/'零股'/'期貨':零股以 1 股計 (不×1000)。
    回傳這筆的摘要 dict (同時 append 進 history)。
    """
    qty = int(qty); price = float(price)
    sym = str(symbol).upper()
    note = ''
    fee = 0.0
    pnl = 0.0
    pos = acct['positions'].get(sym)
    # 台股每單位股數:整股 1 張=1000 股;零股=1 股
    share_per_unit = 1 if trade_type == '零股' else 1000

    if market == '台股':
        gross = price * share_per_unit * qty
        if action == '買進':
            fee = gross * STOCK_FEE_RATE
            acct['cash'] -= (gross + fee)
        else:
            fee = gross * STOCK_FEE_RATE + gross * STOCK_TAX_RATE
            acct['cash'] += (gross - fee)
    else:  # 台期貨
        mult, note = _fut_multiplier(sym)
        fee = FUTURES_FEE_PER_LOT * qty
        acct['cash'] -= fee

    if kind == 'OPEN':
        direction = '多' if action == '買進' else '空'
        if pos and pos.get('direction') == direction:
            total = pos['qty'] + qty
            pos['avg_price'] = (pos['avg_price'] * pos['qty'] + price * qty) / total
            pos['qty'] = total
        else:
            acct['positions'][sym] = {'market': market, 'direction': direction,
                                       'qty': qty, 'avg_price': price, 'mark_price': price,
                                       'share_per_unit': share_per_unit}
    else:  # CLOSE
        if pos:
            close_qty = min(qty, pos['qty'])
            d_mult = 1.0 if pos['direction'] == '多' else -1.0
            diff = (price - pos['avg_price']) * d_mult
            if market == '台股':
                spu = pos.get('share_per_unit', share_per_unit)
                pnl = diff * spu * close_qty - fee
            else:
                mult, note = _fut_multiplier(sym)
                pnl = diff * mult * close_qty - fee
                acct['cash'] += diff * mult * close_qty  # 期貨平倉損益直接進出現金
            acct['realized_pnl'] += pnl
            pos['qty'] -= close_qty
            if pos['qty'] <= 0:
                acct['positions'].pop(sym, None)
        else:
            note = (note + ' 無對應持倉的平倉 (忽略部位)').strip()

    rec = {'ts': str(ts), 'symbol': sym, 'market': market, 'action': action,
           'kind': kind, 'qty': qty, 'price': price, 'fee': round(fee, 2),
           'pnl': round(pnl, 2), 'note': note}
    acct['history'].append(rec)
    return rec


def mark_price(acct, symbol, price):
    pos = acct['positions'].get(str(symbol).upper())
    if pos:
        pos['mark_price'] = float(price)


def realized_pnl_on(acct, day):
    """【ADR-108】某一天已實現損益 = 該日 history 裡所有 pnl 相加。

    刻意用「ts 字串前綴比對」而不是解析日期:ts 由呼叫端以
    `'%Y-%m-%d %H:%M:%S'` 寫入 (見 apply_fill 的呼叫點),前綴比對不會因為
    時區或 strptime 失敗而整個炸掉——查詢損益不該有機會讓程式當掉。
    格式不符的舊記錄只是不被計入,不影響 realized_pnl 累計值。
    """
    d = str(day or '').strip()
    if not d:
        return 0.0
    total = 0.0
    for rec in (acct or {}).get('history') or []:
        if str(rec.get('ts', '')).startswith(d):
            try:
                total += float(rec.get('pnl') or 0.0)
            except (TypeError, ValueError):
                continue
    return total


def unrealized_pnl(acct):
    total = 0.0
    for sym, pos in acct['positions'].items():
        d = 1.0 if pos['direction'] == '多' else -1.0
        diff = (float(pos.get('mark_price', pos['avg_price'])) - pos['avg_price']) * d
        if pos['market'] == '台股':
            total += diff * pos.get('share_per_unit', 1000) * pos['qty']
        else:
            mult, _ = _fut_multiplier(sym)
            total += diff * mult * pos['qty']
    return total


def equity(acct):
    """權益數 = 現金 + 台股持倉市值 + 期貨未實現。"""
    stock_value = 0.0
    for sym, pos in acct['positions'].items():
        if pos['market'] == '台股':
            stock_value += float(pos.get('mark_price', pos['avg_price'])) * pos.get('share_per_unit', 1000) * pos['qty']
    fut_unreal = 0.0
    for sym, pos in acct['positions'].items():
        if pos['market'] != '台股':
            d = 1.0 if pos['direction'] == '多' else -1.0
            mult, _ = _fut_multiplier(sym)
            fut_unreal += (float(pos.get('mark_price', pos['avg_price'])) - pos['avg_price']) * d * mult * pos['qty']
    return acct['cash'] + stock_value + fut_unreal
