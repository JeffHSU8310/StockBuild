# -*- coding: utf-8 -*-
"""
core/regime_panel.py — 主圖【盤勢判斷】面板的純邏輯 (ADR-120)

【這個模組要解決什麼】原本「盤勢/型態偵測」掛在終極波段策略的編輯器裡,
只有把那個策略打開、而且策略在跑的時候才會評估;「支撐壓力」則是主圖上
另一顆獨立的勾選鈕。使用者要的是**看盤的時候就直接在主圖上看到判斷結果**
—— 兩者都是「對著現在這張 K 線圖做出的盤勢判斷」,所以合併成主圖上一個
【盤勢判斷】,底下兩項各自可勾選。

這裡只放**不碰 tkinter / 不碰 matplotlib** 的部分 (鐵則 11):
1. 面板設定的預設值與正規化 (設定檔可能被手改壞、可能是舊版本存的)。
2. 「這次偵測到的訊號裡,哪些是**新的**、值得通知使用者」的去重規則。

【為什麼去重規則一定要獨立成純函式】主圖每縮放/平移一次就重畫一次,
若照著 evaluate_all() 的結果直接通知,同一個型態會在同一根 K 棒上被通知
幾十次 —— 這種 bug 在 GUI 裡幾乎測不到,抽成純函式才能離線驗證。
"""
from core import market_pattern
from core import volume_profile

# ---------------------------------------------------------------------------
# 訊號去重
# ---------------------------------------------------------------------------
# 這幾種訊號代表「目前的持續狀態」(盤勢分類/是否接近區間邊界/三角形楔形),
# 不是一次性事件 —— 狀態沒變之前,每次重新評估都會再算出同一個結果。
# 其餘型態 (M頭/W底/頭肩頂底/破底翻/假突破/島型反轉/區間突破跌破/N字形)
# 在 market_pattern 裡本來就設計成「只在確認的那一根 K 棒觸發」,所以只需要
# 「同一根 K 棒內不重複」這層去重。
#
# (ADR-120 之前這份清單住在 stock_app_pro.QT_PATTERN_PERSISTENT_IDS,
#  跟著終極波段策略一起搬過來。)
PERSISTENT_PATTERN_IDS = frozenset({
    'regime', 'near_range_high', 'near_range_low',
    'triangle_symmetrical', 'triangle_ascending', 'triangle_descending',
    'wedge_rising', 'wedge_falling',
})

# 主圖預設要判斷的標的:加權指數。使用者要求「打開程式預設就是加權指數日K」,
# 盤勢判斷也預設只對它做 (避免翻閱自選股時被一堆個股型態通知洗版)。
DEFAULT_INDEX_SYMBOL = '^TWII'

# 型態判斷只在日 K 上成立 —— 這些型態的定義 (頸線/缺口/區間) 都是以「一天
# 一根」為前提,套到 5 分 K 上算出來的東西沒有意義。
PATTERN_TIMEFRAME = '日K'

PANEL_DEFAULTS = {
    'enabled': False,            # 【盤勢判斷】總開關 (主圖那顆勾選鈕)
    'sr_enabled': True,          # 子項:量價支撐壓力 (ADR-102)
    'sr_range_mode': volume_profile.RANGE_VISIBLE,
    'sr_fixed_bars': 120,
    'sr_max_levels': 6,
    'pattern_enabled': True,     # 子項:盤勢/型態提醒
    'pattern_lookback': market_pattern.DEFAULT_PARAMS['lookback'],
    'pattern_near_pct': market_pattern.DEFAULT_PARAMS['near_pct'],
    'pattern_list': list(market_pattern.DEFAULT_PARAMS['enabled_patterns']),
    'index_only': True,          # 只在加權指數上做型態判斷
}

PATTERN_IDS = frozenset(item['id'] for item in market_pattern.PATTERN_CATALOG)


def _int_in(v, lo, hi, default):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _float_in(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                       # NaN
        return default
    return max(lo, min(hi, f))


def normalize(raw):
    """把設定檔讀到的東西整理成一份完整、值域安全的面板設定。

    設定檔可能是空的 (第一次啟動)、可能是舊版本存的 (缺欄位)、也可能被
    手動改壞 (型別不對、數值離譜)。任何一種情況都必須回一份能用的設定,
    絕不可以讓「設定檔壞掉」變成「主圖畫不出來」。
    """
    src = raw if isinstance(raw, dict) else {}
    out = dict(PANEL_DEFAULTS)
    out['pattern_list'] = list(PANEL_DEFAULTS['pattern_list'])

    for key in ('enabled', 'sr_enabled', 'pattern_enabled', 'index_only'):
        if key in src:
            out[key] = bool(src[key])

    mode = src.get('sr_range_mode')
    if mode in volume_profile.RANGE_MODES:
        out['sr_range_mode'] = mode

    out['sr_fixed_bars'] = _int_in(src.get('sr_fixed_bars'), 20, 5000,
                                   PANEL_DEFAULTS['sr_fixed_bars'])
    out['sr_max_levels'] = _int_in(src.get('sr_max_levels'), 1, 20,
                                   PANEL_DEFAULTS['sr_max_levels'])
    out['pattern_lookback'] = _int_in(src.get('pattern_lookback'), 10, 2000,
                                      PANEL_DEFAULTS['pattern_lookback'])
    out['pattern_near_pct'] = _float_in(src.get('pattern_near_pct'), 0.1, 50.0,
                                        PANEL_DEFAULTS['pattern_near_pct'])

    # 【刻意區分】沒有這個欄位 (舊設定檔) → 用預設的全部型態;
    # 有這個欄位但是空的 → 使用者真的把型態全部取消勾選了,要尊重他的選擇,
    # 不可以「好心」幫他變回全開。
    if 'pattern_list' in src:
        got = src.get('pattern_list')
        if isinstance(got, (list, tuple, set)):
            out['pattern_list'] = [pid for pid in market_pattern.DEFAULT_PARAMS['enabled_patterns']
                                   if pid in set(got)]
    return out


def pattern_params(settings):
    """把面板設定翻成 market_pattern.evaluate_all() 吃的 params。"""
    s = normalize(settings)
    return {
        'lookback': s['pattern_lookback'],
        'near_pct': s['pattern_near_pct'],
        'enabled_patterns': tuple(s['pattern_list']),
    }


def should_evaluate(settings, symbol, timeframe):
    """現在主圖上這個商品/週期,要不要跑盤勢型態判斷。

    三個條件都成立才跑:總開關開、型態子項開、週期是日K;若設定為
    「只在加權指數」則商品也要對得上。
    """
    s = normalize(settings)
    if not (s['enabled'] and s['pattern_enabled']):
        return False
    if str(timeframe or '') != PATTERN_TIMEFRAME:
        return False
    if s['index_only'] and not is_index_symbol(symbol):
        return False
    return True


def is_index_symbol(symbol):
    """是不是加權指數。^TWII 是本程式內部代碼,TSE/IX0001 是 shioaji 側的寫法。"""
    s = str(symbol or '').strip().upper()
    return s in ('^TWII', 'TWII', 'TSE', 'TSE001', 'IX0001', '001')


def format_signal(sig):
    """一個訊號 → 一行給人看的字串。"""
    if not isinstance(sig, dict):
        return ''
    label = str(sig.get('label') or sig.get('pattern') or '').strip()
    bias = str(sig.get('bias') or '').strip()
    return f"{label}[{bias}]" if bias else label


def plan_notifications(signals, state, bar_date):
    """挑出這次「真的要通知」的訊號,並回傳更新後的去重狀態。

    回傳 (msgs, new_state);**不會改動傳入的 state** (呼叫端要自己接回去)。

    去重規則:
    - 一次性型態 (M頭/W底/破底翻/…):同一根 K 棒 (bar_date) 內只通知一次。
      主圖每縮放/平移一次就重畫一次,沒有這層就會被同一個訊號洗版。
    - 持續狀態 (盤勢分類/接近區間邊界/三角形楔形):內容 (label) 跟上次通知
      的一樣就不再通知,跨到新的一根 K 棒也一樣 —— 盤勢連續 20 天都是
      「區間整理」不該連發 20 則通知。
    """
    prev = state if isinstance(state, dict) else {}
    same_bar = (prev.get('date') == bar_date)
    seen = set(prev.get('oneshot') or ()) if same_bar else set()
    prev_persistent = dict(prev.get('persistent') or {})

    msgs = []
    new_oneshot = set(seen)
    new_persistent = {}
    for sig in (signals or []):
        if not isinstance(sig, dict):
            continue
        pid = str(sig.get('pattern') or '').strip()
        if not pid:
            continue
        label = str(sig.get('label') or pid)
        text = format_signal(sig)
        if pid in PERSISTENT_PATTERN_IDS:
            new_persistent[pid] = label
            if prev_persistent.get(pid) == label:
                continue
        else:
            if pid in seen:
                continue
            new_oneshot.add(pid)
        if text:
            msgs.append(text)
    return msgs, {'date': bar_date, 'oneshot': sorted(new_oneshot),
                  'persistent': new_persistent}
