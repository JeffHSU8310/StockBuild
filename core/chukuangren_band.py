# -*- coding: utf-8 -*-
"""
core/chukuangren_band.py — 「楚狂人之終極波段」內建策略 (純邏輯層)

【新ADR】設計原則 (比照 ADR-035 strategy_engine.py 的規範):
1. 零 tkinter / 零 shioaji 依賴,所有狀態機邏輯都是純函式,可離線單元測試。
2. 這是一個獨立的策略「kind」(不是 strategy_engine.CONDITIONS 條件組合的產物),
   因為它的邏輯是「隔天中午12:00 二次確認」+「點數移動停利→SMA20移動停利」的
   專屬狀態機,無法用既有的 AND/OR 條件庫拼出來。

策略邏輯摘要 (看加權指數(A)訊號,做使用者自選商品(B)):
  參數 X:多空進出場分界點位。Y/Z:多單停損觸發/確認。S1/S2:空單停損觸發/確認。
  C:停利啟動門檻(獲利點數)。F:停利步幅。H:移動停利基準起始值(使用者輸入)。
  D/E:多單/空單進場點位 (系統自動記錄,存進 runtime['entry_index_price'])。

  每日 (加權指數日K收盤) 評估一次:
    無部位:收盤>X → 記一筆待確認多單訊號;收盤<X → 待確認空單訊號。
    有多單:跌破Y → 待確認停損;若已進入 SMA20 模式且跌破SMA20 → 待確認SMA20停利;
            否則走點數移動停利 (獲利超過C才啟動,之後每漲F、trail_base 棘輪式
            上調F,跌破 trail_base → 待確認點數停利);若收盤漲過SMA20 → 切換
            進入 SMA20 模式 (一旦切換,不會切回點數模式;固定停損Y仍持續監控)。
    空單對稱處理 (S1/S2、trail_base 棘輪式下調)。

  隔天中午12:00 (呼叫端傳入加權指數當下的5分K收盤價) 二次確認:
    待確認進場:仍符合方向條件才真正進場 (記錄 D/E),否則作廢等下一次觸發。
    待確認出場 (停損/點數停利/SMA20停利):仍符合條件才真正出場,否則作廢、
    維持原部位並繼續每日監控。

本模組不做的事:合約解析、抓K棒、下單、風控守門 (由 stock_app_pro.py
的 _quant_eval_pass 呼叫既有的 strategy_engine.risk_check/apply_fill 負責,
本模組只負責產生跟 strategy_engine 相同格式的 intents)。
"""
import pandas as pd

# 使用者輸入參數的 key 與中文標籤 (UI 與驗證共用同一份,避免各處各寫一份不同步)
PARAM_KEYS = ('x', 'y', 'z', 's1', 's2', 'c', 'f', 'h')
PARAM_LABELS = {
    'x': 'X — 多空進出場分界點位',
    'y': 'Y — 多單停損觸發點位 (跌破)',
    'z': 'Z — 多單停損確認點位 (隔天12點仍需低於)',
    's1': 'S1 — 空單停損觸發點位 (突破)',
    's2': 'S2 — 空單停損確認點位 (隔天12點仍需高於)',
    'c': 'C — 停利啟動門檻 (獲利點數)',
    'f': 'F — 停利步幅 (獲利每上升此點數,基準跟著上調)',
    'h': 'H — 移動停利基準起始值',
}

KIND = 'chukuangren_band'
STRATEGY_NAME = '楚狂人之終極波段'


def params_of(strategy):
    """從策略 dict 讀出 ck_x/ck_y/... 組成 {key: float} 給純函式使用。"""
    out = {}
    for k in PARAM_KEYS:
        try:
            out[k] = float(strategy.get(f'ck_{k}', 0) or 0)
        except (TypeError, ValueError):
            out[k] = 0.0
    return out


def default_strategy():
    """建立一份帶安全預設值的策略 dict:base 沿用 strategy_engine.new_strategy()
    (共用 symbol/qty/mode/enabled/session_gate/看A做B 等既有欄位與持久化格式),
    只額外疊加 ck_* 參數與固定的看A做B設定 (A 固定看加權指數)。"""
    from . import strategy_engine
    s = strategy_engine.new_strategy()
    s['kind'] = KIND
    s['name'] = STRATEGY_NAME
    s['watch_enabled'] = True
    s['watch_symbol'] = '^TWII'
    s['watch_trade_type'] = '指數'
    s['watch_timeframe'] = '5分K'   # 驅動 _quant_eval_pass 每5分鐘評估一次 (隔日中午確認需要這個頻率)
    for k in PARAM_KEYS:
        s[f'ck_{k}'] = 0.0
    return s


def validate(strategy):
    """存檔/啟用前的驗證。回傳 (ok, 錯誤訊息)。不沿用 strategy_engine.validate_strategy
    (那是給條件組合策略用的,entry/exit_signals 等欄位對本策略沒有意義)。"""
    from . import strategy_engine
    if not str(strategy.get('name', '')).strip():
        return False, "策略名稱不可空白"
    if not str(strategy.get('symbol', '')).strip():
        return False, "執行商品 (做B) 代碼不可空白"
    if strategy_engine.looks_like_index_symbol(strategy.get('symbol', '')):
        return False, "執行商品 (做B) 不可為指數 (加權/櫃買等),指數只能當看盤(A)訊號來源"
    tt = strategy_engine.trade_type_of(strategy)
    if tt not in strategy_engine.TRADE_TYPES:
        return False, "交易種類僅支援 股票 / 零股 / 期貨"
    try:
        q = int(strategy.get('qty', 0))
        if q <= 0:
            return False, "數量必須為正整數"
    except (TypeError, ValueError):
        return False, "數量必須為正整數"
    if not str(strategy.get('watch_symbol', '')).strip():
        return False, "看盤 (A) 商品代碼不可空白"
    p = params_of(strategy)
    if p['y'] >= p['x']:
        return False, "多單停損觸發點位 Y 必須小於進出場分界 X"
    if p['s1'] <= p['x']:
        return False, "空單停損觸發點位 S1 必須大於進出場分界 X"
    if p['c'] < 0 or p['f'] < 0:
        return False, "停利啟動門檻 C 與停利步幅 F 不可為負"
    return True, ""


def ensure_runtime(rt):
    """確保 runtime dict 帶有本策略需要的額外欄位 (缺的就補預設值,不覆蓋既有值)。
    沿用 strategy_engine.new_runtime() 的 'state'/'entry_price'/'qty' 等通用欄位
    (由 apply_fill 維護),這裡只補本策略專屬的擴充欄位。"""
    rt.setdefault('pending_entry', None)     # {'dir': 'LONG'/'SHORT', 'date': 'YYYY-MM-DD'}
    rt.setdefault('pending_exit', None)      # {'reason': 'SL'/'TP_POINT'/'TP_SMA20', 'date': ..., 'sma20_ref': float}
    rt.setdefault('trail_armed', False)
    rt.setdefault('trail_base', 0.0)
    rt.setdefault('sma20_mode', False)
    rt.setdefault('entry_index_price', 0.0)  # D 或 E:進場當時的加權指數點位
    rt.setdefault('last_daily_bar_date', '')
    rt.setdefault('last_confirm_date', '')
    return rt


def _position_of(rt):
    st = rt.get('state', 'FLAT')
    return st if st in ('LONG', 'SHORT') else 'FLAT'


def on_daily_close(params, rt, daily_df):
    """加權指數日K新收一根時呼叫,更新 pending_entry/pending_exit 與移動停利狀態。
    冪等:同一天重複呼叫 (同一根日K還沒變) 是 no-op,可以放心每次評估都呼叫。
    只修改 rt (in-place),不產生 intents (進出場一律要等隔天12點確認)。"""
    ensure_runtime(rt)
    if daily_df is None or len(daily_df) < 20:
        return rt
    today_date = str(daily_df.index[-1]).split(' ')[0]
    if rt.get('last_daily_bar_date') == today_date:
        return rt
    rt['last_daily_bar_date'] = today_date

    close = float(daily_df['Close'].iloc[-1])
    sma20 = float(daily_df['Close'].rolling(20).mean().iloc[-1])
    position = _position_of(rt)
    X, Y, Z, S1, S2, C, F, H = (params[k] for k in ('x', 'y', 'z', 's1', 's2', 'c', 'f', 'h'))

    if position == 'FLAT':
        if rt.get('pending_entry') is None:
            if close > X:
                rt['pending_entry'] = {'dir': 'LONG', 'date': today_date}
            elif close < X:
                rt['pending_entry'] = {'dir': 'SHORT', 'date': today_date}
        return rt

    if rt.get('pending_exit') is not None:
        return rt  # 已有待確認出場,先等隔天12點結果揭曉,暫停繼續評估新的出場條件

    entry_px = float(rt.get('entry_index_price', 0) or 0)
    if position == 'LONG':
        if close < Y:
            rt['pending_exit'] = {'reason': 'SL', 'date': today_date}
            return rt
        if rt.get('sma20_mode'):
            if not pd.isna(sma20) and close < sma20:
                rt['pending_exit'] = {'reason': 'TP_SMA20', 'date': today_date, 'sma20_ref': sma20}
            return rt
        # 點數移動停利模式
        if entry_px > 0:
            profit = close - entry_px
            if not rt.get('trail_armed') and profit > C:
                rt['trail_armed'] = True
                rt['trail_base'] = H
            if rt.get('trail_armed') and F > 0:
                n = int((profit - C) // F)
                if n > 0:
                    candidate = H + n * F
                    if candidate > rt.get('trail_base', H):
                        rt['trail_base'] = candidate
            if rt.get('trail_armed') and close < rt.get('trail_base', H):
                rt['pending_exit'] = {'reason': 'TP_POINT', 'date': today_date}
                return rt
        if not pd.isna(sma20) and close > sma20:
            rt['sma20_mode'] = True
    else:  # SHORT
        if close > S1:
            rt['pending_exit'] = {'reason': 'SL', 'date': today_date}
            return rt
        if rt.get('sma20_mode'):
            if not pd.isna(sma20) and close > sma20:
                rt['pending_exit'] = {'reason': 'TP_SMA20', 'date': today_date, 'sma20_ref': sma20}
            return rt
        if entry_px > 0:
            profit = entry_px - close
            if not rt.get('trail_armed') and profit > C:
                rt['trail_armed'] = True
                rt['trail_base'] = H
            if rt.get('trail_armed') and F > 0:
                n = int((profit - C) // F)
                if n > 0:
                    candidate = H - n * F
                    if candidate < rt.get('trail_base', H):
                        rt['trail_base'] = candidate
            if rt.get('trail_armed') and close > rt.get('trail_base', H):
                rt['pending_exit'] = {'reason': 'TP_POINT', 'date': today_date}
                return rt
        if not pd.isna(sma20) and close < sma20:
            rt['sma20_mode'] = True
    return rt


_REASON_LABEL = {'SL': '固定停損', 'TP_POINT': '點數移動停利', 'TP_SMA20': 'SMA20移動停利'}


def on_noon_check(params, rt, confirm_price, today_key, qty=1):
    """隔天中午12:00 呼叫 (confirm_price=加權指數當下的5分K收盤價)。
    確認 pending_entry / pending_exit 是否仍然成立,回傳 intents list
    (格式與 strategy_engine.evaluate_strategy 相同,可直接丟給
    strategy_engine.risk_check / apply_fill 處理)。同一個交易日只會真正
    確認一次 (用 rt['last_confirm_date'] 防重複)。"""
    ensure_runtime(rt)
    intents = []
    if rt.get('last_confirm_date') == today_key:
        return intents
    rt['last_confirm_date'] = today_key
    X, Z, S2 = params['x'], params['z'], params['s2']

    pe = rt.get('pending_entry')
    if pe is not None:
        d = pe.get('dir')
        ok = (d == 'LONG' and confirm_price > X) or (d == 'SHORT' and confirm_price < X)
        if ok:
            rt['entry_index_price'] = float(confirm_price)
            rt['trail_armed'] = False
            rt['trail_base'] = 0.0
            rt['sma20_mode'] = False
            action = '買進' if d == 'LONG' else '賣出'
            intents.append({'kind': 'OPEN', 'action': action, 'qty': int(qty),
                            'price': float(confirm_price),
                            'reason': f"楚狂人進場確認 (隔日12點指數{confirm_price:g} "
                                      f"{'>' if d == 'LONG' else '<'} X={X:g})"})
        rt['pending_entry'] = None

    pex = rt.get('pending_exit')
    if pex is not None:
        reason = pex.get('reason')
        position = _position_of(rt)
        confirmed = False
        if reason == 'SL':
            confirmed = (confirm_price < Z) if position == 'LONG' else (confirm_price > S2)
        elif reason == 'TP_POINT':
            base = float(rt.get('trail_base', 0.0))
            confirmed = (confirm_price <= base) if position == 'LONG' else (confirm_price >= base)
        elif reason == 'TP_SMA20':
            ref = float(pex.get('sma20_ref', 0) or 0)
            confirmed = (confirm_price < ref) if position == 'LONG' else (confirm_price > ref)
        if confirmed:
            action = '賣出' if position == 'LONG' else '買進'
            intents.append({'kind': 'CLOSE', 'action': action, 'qty': int(rt.get('qty', qty) or qty),
                            'price': float(confirm_price),
                            'reason': f"楚狂人出場確認 ({_REASON_LABEL.get(reason, reason)})"})
            rt['trail_armed'] = False
            rt['trail_base'] = 0.0
            rt['sma20_mode'] = False
            rt['entry_index_price'] = 0.0
        rt['pending_exit'] = None
    return intents
