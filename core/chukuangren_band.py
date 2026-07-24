# -*- coding: utf-8 -*-
"""
core/chukuangren_band.py — 「終極波段策略」內建策略 (純邏輯層,原名「楚狂人之終極波段」)

【ADR-084/ADR-085】設計原則 (比照 ADR-035 strategy_engine.py 的規範):
1. 零 tkinter / 零 shioaji 依賴,所有狀態機邏輯都是純函式,可離線單元測試。
2. 這是一個獨立的策略「kind」(不是 strategy_engine.CONDITIONS 條件組合的產物),
   因為它的邏輯是「隔天中午12:00 二次確認」+「點數移動停利→SMA20移動停利」的
   專屬狀態機,無法用既有的 AND/OR 條件庫拼出來。

策略邏輯摘要 (看加權指數(A)訊號,做使用者自選商品(B)):
  方向 (direction):做多 或 做空,一個策略只做單一方向 (ADR-085)。
    做多 → 只在收盤突破 X 時進場;做空 → 只在收盤跌破 X 時進場。
  參數 X:進出場分界點位。Y/Z:多單停損觸發/確認 (做多才用)。
        S1/S2:空單停損觸發/確認 (做空才用)。
        C:停利啟動門檻(獲利點數)。F:停利步幅。
  進場後**一定會記錄進場當時的加權指數點位** (D=多單/E=空單,存進
  runtime['entry_index_price']),之後所有停損/停利計算全部以這個點位為基準
  (ADR-085 使用者強調:「之後都是看加權指數點位來計算」)。

  每日 (加權指數日K收盤) 評估一次:
    無部位:做多且收盤>X → 待確認多單訊號;做空且收盤<X → 待確認空單訊號。
    有多單:跌破Y → 待確認停損;若已進入 SMA20 模式且跌破SMA20 → 待確認SMA20停利;
            否則走點數移動停利 (獲利(收盤-D)超過C才啟動,停利線從進場點D起算,
            獲利每上升F、停利線就往上鎖F(棘輪式,只升不降),跌破停利線 →
            待確認點數停利);獲利超過C 且收盤漲過SMA20 → 切換進 SMA20 模式
            (一旦切換,不會切回;固定停損Y仍持續監控)。
    空單對稱處理 (S1/S2、停利線從進場點E往下鎖;獲利超過C 且收盤跌破SMA20
    才切換進 SMA20 模式)。

  【ADR-085 移除 H】舊版有一個「移動停利基準起始值 H」是絕對指數點位,但進場點
  D 要進場當下才知道,預先填一個絕對值 (甚至填 0) 會讓點數移動停利失效。改成
  停利線一律「從進場點 D 自動起算」,獲利每 +F 就往上鎖 F —— 等於「獲利超過 C
  點就開始保護,回撤到接近成本就出場」,C 同時決定了回撤容忍度,少一個容易填
  錯的參數。

  隔天中午12:00 (呼叫端傳入加權指數當下的5分K收盤價) 二次確認:
    待確認進場:仍符合方向條件才真正進場 (記錄 D/E),否則作廢等下一次觸發。
    待確認出場 (停損/點數停利/SMA20停利):仍符合條件才真正出場,否則作廢、
    維持原部位並繼續每日監控。

  【新ADR 確認與下單分兩個時間點】12:00確認成立不會立刻送單,而是記進
  armed_intent 並等 60 秒 (約12:01) 後才真正下單 (on_execute_armed),下單
  用的價格是執行當下 (B) 的最新價,不是12:00確認當下的價——這是使用者
  明確要求「12:00確認,條件成立,12:01才下單」,不要兩件事在同一瞬間發生。

本模組不做的事:合約解析、抓K棒、下單、風控守門 (由 stock_app_pro.py
的 _quant_eval_pass 呼叫既有的 strategy_engine.risk_check/apply_fill 負責,
本模組只負責產生跟 strategy_engine 相同格式的 intents)。
"""
import pandas as pd

# 使用者輸入參數的 key 與中文標籤 (UI 與驗證共用同一份,避免各處各寫一份不同步)
PARAM_KEYS = ('x', 'y', 'z', 's1', 's2', 'c', 'f')
# 依方向只顯示/驗證對應的參數 (ADR-085)
LONG_PARAM_KEYS = ('x', 'y', 'z', 'c', 'f')
SHORT_PARAM_KEYS = ('x', 's1', 's2', 'c', 'f')
PARAM_LABELS = {
    'x': 'X — 進出場分界點位 (突破做多 / 跌破做空)',
    'y': 'Y — 多單停損觸發點位 (跌破)',
    'z': 'Z — 多單停損確認點位 (隔天12點仍需低於)',
    's1': 'S1 — 空單停損觸發點位 (突破)',
    's2': 'S2 — 空單停損確認點位 (隔天12點仍需高於)',
    'c': 'C — 停利啟動門檻 (獲利點數;同時是回撤容忍度)',
    'f': 'F — 停利步幅 (獲利每上升此點數,停利線跟著上移)',
}

# 每個參數的完整中文說明 (UI 顯示在輸入框下方,讓使用者一看就懂要填什麼)
PARAM_HELP = {
    'x': ("多空的分界線 (填加權指數點位)。做多:加權指數某天收盤突破 X,且隔天中午"
          "12:00 仍在 X 之上,才真正進場;做空:收盤跌破 X,且隔天中午仍在 X 之下才進場。"),
    'y': ("多單的『停損觸發線』(加權指數點位)。手上有多單時,某天加權指數收盤跌破 Y,"
          "就先列為『待確認停損』,等隔天中午12:00 再確認。"),
    'z': ("多單的『停損確認線』(加權指數點位)。跌破 Y 之後,隔天中午12:00 加權指數仍"
          "低於 Z,多單才真的全部平倉;若已站回 Z 之上就作廢、繼續持有。一般設在 Y 附近。"),
    's1': ("空單的『停損觸發線』(加權指數點位)。手上有空單時,某天加權指數收盤突破 S1,"
           "就先列為『待確認停損』,等隔天中午12:00 再確認。"),
    's2': ("空單的『停損確認線』(加權指數點位)。突破 S1 之後,隔天中午12:00 加權指數仍"
           "高於 S2,空單才真的全部平倉;若已回到 S2 之下就作廢、繼續持有。一般設在 S1 附近。"),
    'c': ("移動停利的『啟動門檻』(點數)。獲利 (目前加權指數與進場點位的差) 超過 C 點,"
          "才開始用移動停利保護獲利;C 也大約等於允許的回撤點數 (回撤到接近成本就出場)。"
          "同時也是切換到 SMA20 移動停利模式的前提:獲利要先超過 C,且收盤站上/跌破"
          "SMA20,才會切換,獲利還沒過 C 時即使站上/跌破 SMA20 也不會切換。"),
    'f': ("移動停利的『步幅』(點數)。啟動後 (獲利超過C點),獲利每再增加 F 點,停利線"
          "就往上 (多單) /往下 (空單) 鎖 F 點,鎖住的獲利只增不減;F 越小,停利線跟得"
          "越緊。這條『停利線』起點是進場點位,不是C本身——C只是決定何時開始用這條線,"
          "F才是線之後怎麼移動。注意:一旦成立C同時切換進SMA20模式的條件 (獲利超過C"
          "且站上/跌破SMA20),F就不再參與判斷,之後全部改用SMA20的位置決定出場,"
          "且不會切回來。"),
}

KIND = 'chukuangren_band'
STRATEGY_NAME = '終極波段策略'
DIRECTIONS = ('做多', '做空')


def param_keys_for(direction):
    """依方向回傳該顯示/驗證的參數 key。"""
    return SHORT_PARAM_KEYS if direction == '做空' else LONG_PARAM_KEYS


def params_of(strategy):
    """從策略 dict 讀出 ck_x/ck_y/... 組成 {key: float} 給純函式使用。"""
    out = {}
    for k in PARAM_KEYS:
        try:
            out[k] = float(strategy.get(f'ck_{k}', 0) or 0)
        except (TypeError, ValueError):
            out[k] = 0.0
    return out


def direction_of(strategy):
    d = strategy.get('direction')
    return d if d in DIRECTIONS else '做多'


def default_strategy():
    """建立一份帶安全預設值的策略 dict:base 沿用 strategy_engine.new_strategy()
    (共用 symbol/qty/mode/enabled/session_gate/看A做B 等既有欄位與持久化格式),
    只額外疊加 ck_* 參數與固定的看A做B設定 (A 固定看加權指數)。"""
    from . import strategy_engine
    from . import market_pattern
    s = strategy_engine.new_strategy()
    s['kind'] = KIND
    s['name'] = STRATEGY_NAME
    s['direction'] = '做多'
    s['watch_enabled'] = True
    s['watch_symbol'] = '^TWII'
    s['watch_trade_type'] = '指數'
    s['watch_timeframe'] = '5分K'   # 驅動 _quant_eval_pass 每5分鐘評估一次 (隔日中午確認需要這個頻率)
    for k in PARAM_KEYS:
        s[f'ck_{k}'] = 0.0
    # 【新ADR 盤勢型態提醒】預設關閉 (不要沒問過使用者就多送通知);
    # 使用者在編輯器打開後,才會依 pattern_lookback/pattern_near_pct/
    # pattern_list 跑 core/market_pattern.py 的判斷並用 Telegram/日誌提醒。
    s['pattern_enabled'] = False
    s['pattern_lookback'] = market_pattern.DEFAULT_PARAMS['lookback']
    s['pattern_near_pct'] = market_pattern.DEFAULT_PARAMS['near_pct']
    s['pattern_list'] = list(market_pattern.DEFAULT_PARAMS['enabled_patterns'])
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
    pt = strategy.get('price_type', '限價')
    if pt not in strategy_engine.PRICE_TYPES:
        return False, f"委託方式僅支援 {'/'.join(strategy_engine.PRICE_TYPES)}"
    if pt in strategy_engine.FUTURES_ONLY_PRICE_TYPES and tt != '期貨':
        return False, "範圍市價僅期貨支援 (股票/零股交易所規則沒有這個委託方式)"
    if tt == '零股' and pt != '限價':
        return False, "零股依交易所規則只能限價 (鐵則6,不可用市價/範圍市價)"
    direction = direction_of(strategy)
    if strategy.get('direction') not in DIRECTIONS:
        return False, "方向僅支援 做多 / 做空"
    if direction == '做空' and tt != '期貨':
        return False, "做空僅支援期貨 (股票/零股融券暫不在自動交易範圍)"
    try:
        q = int(strategy.get('qty', 0))
        if q <= 0:
            return False, "數量必須為正整數"
    except (TypeError, ValueError):
        return False, "數量必須為正整數"
    if not str(strategy.get('watch_symbol', '')).strip():
        return False, "看盤 (A) 商品代碼不可空白"
    p = params_of(strategy)
    if direction == '做多':
        if p['y'] >= p['x']:
            return False, "多單停損觸發點位 Y 必須小於進出場分界 X"
    else:
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
    rt.setdefault('entry_index_price', 0.0)  # D 或 E:進場當時的加權指數點位 (ADR-085 一定記錄)
    rt.setdefault('last_daily_bar_date', '')
    rt.setdefault('last_confirm_date', '')
    # 【新ADR 確認/下單分兩個時間點】armed_intent:12:00確認成立、還沒真正
    # 下單的動作 ({'kind','action','qty'(僅OPEN用),'reason'});armed_at_ts:
    # 確認當下的時間戳,on_execute_armed 要等過了 delay_sec 秒才會真正組出
    # intent (預設60秒,即約12:00確認→12:01下單)。
    rt.setdefault('armed_intent', None)
    rt.setdefault('armed_at_ts', 0.0)
    return rt


def _position_of(rt):
    st = rt.get('state', 'FLAT')
    return st if st in ('LONG', 'SHORT') else 'FLAT'


def on_daily_close(params, rt, daily_df, direction='做多'):
    """加權指數日K新收一根時呼叫,更新 pending_entry/pending_exit 與移動停利狀態。
    冪等:同一天重複呼叫 (同一根日K還沒變) 是 no-op,可以放心每次評估都呼叫。
    只修改 rt (in-place),不產生 intents (進出場一律要等隔天12點確認)。

    direction:'做多' 或 '做空' —— 只在該方向的進場條件成立才記待確認訊號 (ADR-085)。"""
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
    X, Y, Z, S1, S2, C, F = (params[k] for k in ('x', 'y', 'z', 's1', 's2', 'c', 'f'))

    if position == 'FLAT':
        if rt.get('pending_entry') is None:
            if direction == '做多' and close > X:
                rt['pending_entry'] = {'dir': 'LONG', 'date': today_date}
            elif direction == '做空' and close < X:
                rt['pending_entry'] = {'dir': 'SHORT', 'date': today_date}
        return rt

    if rt.get('pending_exit') is not None:
        return rt  # 已有待確認出場,先等隔天12點結果揭曉,暫停繼續評估新的出場條件

    # 停利線一律從「進場點 D/E」起算 (ADR-085,不再用絕對值 H)
    entry_px = float(rt.get('entry_index_price', 0) or 0)
    if position == 'LONG':
        if close < Y:
            rt['pending_exit'] = {'reason': 'SL', 'date': today_date}
            return rt
        if rt.get('sma20_mode'):
            if not pd.isna(sma20) and close < sma20:
                rt['pending_exit'] = {'reason': 'TP_SMA20', 'date': today_date, 'sma20_ref': sma20}
            return rt
        # 點數移動停利模式:停利線從進場點 entry_px 起算,獲利每 +F 往上鎖 F
        profit = (close - entry_px) if entry_px > 0 else 0.0
        if entry_px > 0:
            if not rt.get('trail_armed') and profit > C:
                rt['trail_armed'] = True
                rt['trail_base'] = entry_px
            if rt.get('trail_armed') and F > 0:
                n = int((profit - C) // F)
                if n > 0:
                    candidate = entry_px + n * F
                    if candidate > rt.get('trail_base', entry_px):
                        rt['trail_base'] = candidate
            if rt.get('trail_armed') and close < rt.get('trail_base', entry_px):
                rt['pending_exit'] = {'reason': 'TP_POINT', 'date': today_date}
                return rt
        # 【使用者需求】獲利要先超過 C,且收盤站上 SMA20,才切換進 SMA20 模式;
        # 獲利還沒過 C 時即使站上 SMA20 也不切換,避免剛進場、獲利極小甚至還沒
        # 啟動點數移動停利保護,就被 SMA20 雜訊提早切走。
        if profit > C and not pd.isna(sma20) and close > sma20:
            rt['sma20_mode'] = True
    else:  # SHORT
        if close > S1:
            rt['pending_exit'] = {'reason': 'SL', 'date': today_date}
            return rt
        if rt.get('sma20_mode'):
            if not pd.isna(sma20) and close > sma20:
                rt['pending_exit'] = {'reason': 'TP_SMA20', 'date': today_date, 'sma20_ref': sma20}
            return rt
        profit = (entry_px - close) if entry_px > 0 else 0.0
        if entry_px > 0:
            if not rt.get('trail_armed') and profit > C:
                rt['trail_armed'] = True
                rt['trail_base'] = entry_px
            if rt.get('trail_armed') and F > 0:
                n = int((profit - C) // F)
                if n > 0:
                    candidate = entry_px - n * F
                    if candidate < rt.get('trail_base', entry_px):
                        rt['trail_base'] = candidate
            if rt.get('trail_armed') and close > rt.get('trail_base', entry_px):
                rt['pending_exit'] = {'reason': 'TP_POINT', 'date': today_date}
                return rt
        if profit > C and not pd.isna(sma20) and close < sma20:
            rt['sma20_mode'] = True
    return rt


_REASON_LABEL = {'SL': '固定停損', 'TP_POINT': '點數移動停利', 'TP_SMA20': 'SMA20移動停利'}


def on_noon_confirm(params, rt, confirm_price, today_key, now_ts, qty=1):
    """隔天中午12:00 呼叫 (confirm_price=加權指數當下的5分K收盤價) —— 只做
    『確認』,不下單。確認 pending_entry / pending_exit 是否仍然成立;成立
    就把要執行的動作記進 rt['armed_intent'] 並記下確認時間 rt['armed_at_ts']。
    同一個交易日只會真正確認一次 (用 rt['last_confirm_date'] 防重複)。

    【新ADR 確認/下單分兩個時間點】真正的下單延後到 on_execute_armed() 判斷
    『已過 delay_sec 秒 (預設60秒,即約12:00確認→12:01下單)』後才發生——
    使用者要求確認與下單分開,不要12:00確認成立就立刻在同一瞬間送單。

    【ADR-085】進場確認成立時,一定把 confirm_price 記進 rt['entry_index_price']
    (=D 多單 / E 空單) —— 之後的停損/停利全部以這個加權指數點位為基準
    (記錄的是12:00確認當下的點位,不是1分鐘後下單當下的點位,語意不變)。
    不回傳任何東西,純粹修改 rt (in-place)。"""
    ensure_runtime(rt)
    if rt.get('last_confirm_date') == today_key:
        return
    rt['last_confirm_date'] = today_key
    X, Z, S2 = params['x'], params['z'], params['s2']

    pe = rt.get('pending_entry')
    if pe is not None:
        d = pe.get('dir')
        ok = (d == 'LONG' and confirm_price > X) or (d == 'SHORT' and confirm_price < X)
        if ok:
            # ADR-085:記錄進場當時的加權指數點位 (D/E),之後計算全看這個點位
            rt['entry_index_price'] = float(confirm_price)
            rt['trail_armed'] = False
            rt['trail_base'] = 0.0
            rt['sma20_mode'] = False
            action = '買進' if d == 'LONG' else '賣出'
            dname = '多單D' if d == 'LONG' else '空單E'
            rt['armed_intent'] = {
                'kind': 'OPEN', 'action': action, 'qty': int(qty),
                'reason': f"終極波段策略進場確認 (隔日12點指數{confirm_price:g} "
                          f"{'>' if d == 'LONG' else '<'} X={X:g}),"
                          f"記錄進場加權指數點位 {dname}={confirm_price:g}",
            }
            rt['armed_at_ts'] = float(now_ts)
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
            rt['armed_intent'] = {
                'kind': 'CLOSE', 'action': action,
                'reason': f"終極波段策略出場確認 ({_REASON_LABEL.get(reason, reason)})",
            }
            rt['armed_at_ts'] = float(now_ts)
            rt['trail_armed'] = False
            rt['trail_base'] = 0.0
            rt['sma20_mode'] = False
            rt['entry_index_price'] = 0.0
        rt['pending_exit'] = None


def on_execute_armed(rt, exec_price, now_ts, delay_sec=60.0, max_age_sec=600.0):
    """confirm 後延遲下單:rt['armed_intent'] 存在且已經過了 delay_sec 秒
    (預設60秒,即12:00確認→12:01下單) 才真正組出要送出的 intent (格式與
    strategy_engine.evaluate_strategy 相同,可直接丟給 risk_check/apply_fill)。

    exec_price:真正下單那一刻 (執行商品B) 的最新價,不是12:00確認當下的A價
    (confirm_price)——確認與下單本來就是兩個時間點,價格自然也各自取當下的。
    還沒到時間就回傳空 list,呼叫端每次評估都可以放心呼叫 (冪等)。

    【找到的bug/修正】max_age_sec (預設600秒=10分鐘):armed_intent 存在超過
    這個秒數還沒執行,視為過期作廢 (清掉 armed_intent,不送單)。根因:如果
    策略在12:00確認成立、還沒到12:01真正執行前被停用、或總開關被關閉、或
    程式重啟,評估會整段暫停;使用者晚一點 (甚至隔天) 才重新啟用/開總開關
    時,原本的判斷只看「有沒有超過 delay_sec」,會把一個用「當下」全新價格
    執行的委託,冠上一個其實是好幾小時甚至隔天才確認的舊訊號——跟這個策略
    『12:00確認、12:01立刻執行』的設計語意完全不符,可能用一個已經過時、
    市場條件早就變了的訊號送出一筆使用者沒預期到的委託。過期作廢後,若原本
    是出場確認,部位會維持原狀,等下一次日K收盤重新評估出場條件 (正常機制
    接手,不需要人工處理);若是進場確認,就等下一次日K收盤重新出現訊號。
    呼叫端可以用 rt.pop('last_discarded_stale_intent', None) 讀取被作廢的
    intent (讀取後即消失,只會通知一次) 來記錄警告訊息。"""
    ensure_runtime(rt)
    ai = rt.get('armed_intent')
    if ai is None:
        return []
    age = float(now_ts) - float(rt.get('armed_at_ts', 0) or 0)
    if age >= float(max_age_sec):
        rt['armed_intent'] = None
        rt['armed_at_ts'] = 0.0
        rt['last_discarded_stale_intent'] = ai
        return []
    if age < float(delay_sec):
        return []
    if ai['kind'] == 'OPEN':
        qty = int(ai.get('qty', 1))
    else:
        qty = int(rt.get('qty', 0) or 0)
    intent = {'kind': ai['kind'], 'action': ai['action'], 'qty': qty,
              'price': float(exec_price), 'reason': ai['reason']}
    rt['armed_intent'] = None
    rt['armed_at_ts'] = 0.0
    return [intent]
