# -*- coding: utf-8 -*-
"""
core/market_screener.py — 全市場選股引擎 (ADR-103)

【核心設計:重用,不重寫】
技術面與籌碼面的條件**已經全部存在**於 strategy_engine.CONDITIONS
(60+ 個技術條件 + ADR-101 的 15 個籌碼條件)。選股要做的事就是「把這些
條件套用到全市場每一檔」,因此本模組直接呼叫 strategy_engine.eval_conditions,
不另寫一套判斷邏輯。

這個決定的好處:
  1. 使用者在策略裡熟悉的條件,選股時是**完全相同的語意**,不會出現
     「策略的黃金交叉」與「選股的黃金交叉」定義不一致這種最惱人的落差。
  2. 條件只有一份實作、一份測試,不會改了一邊忘了另一邊。
  3. 新增策略條件時,選股自動就有了。

基本面條件則是本模組新增的:它們是單純的「欄位 vs 門檻」數值比較
(本益比 < 15、殖利率 > 5%...),不需要 K 棒序列,用宣告式的
(欄位, 運算子, 門檻) 結構表達即可,不必為每個條件各寫一個函式。

【篩選順序 = 效能關鍵】先用基本面 (單一 DataFrame 向量化比較,極快) 把
候選縮小,再對存活者做技術面/籌碼面 (需逐檔組 K 棒,較慢)。全市場約 2300
檔,基本面通常能先篩掉九成以上。

純邏輯:零網路、零 tkinter、零 shioaji,可離線單元測試 (鐵則 11)。
"""
import pandas as pd

from core import strategy_engine
from core import chips_features
from core import fundamental_parser as fp

# ---------------------------------------------------------------------------
# 基本面條件:宣告式定義 (欄位, 中文標籤, 單位, 預設門檻)
# ---------------------------------------------------------------------------
OP_GTE = '>='
OP_LTE = '<='
OPS = (OP_GTE, OP_LTE)

FUNDAMENTAL_FIELDS = {
    'pe':          (fp.COL_PE, '本益比', '倍', 15.0),
    'pb':          (fp.COL_PB, '股價淨值比', '倍', 1.5),
    'yield':       (fp.COL_YIELD, '殖利率', '%', 5.0),
    'eps':         (fp.COL_EPS, '每股盈餘 EPS', '元', 1.0),
    'gross_margin': (fp.COL_GROSS_MARGIN, '毛利率', '%', 20.0),
    'revenue_yoy': (fp.COL_REV_YOY, '月營收年增率', '%', 10.0),
    'revenue_mom': (fp.COL_REV_MOM, '月營收月增率', '%', 0.0),
    'roe':         (fp.COL_ROE, 'ROE (推算)', '%', 10.0),
    'close':       (fp.COL_CLOSE, '股價', '元', 100.0),
}


def fundamental_label(cond):
    """把基本面條件轉成可讀文字,例如「本益比 <= 15 倍」。"""
    meta = FUNDAMENTAL_FIELDS.get(cond.get('field'))
    if not meta:
        return f"未知基本面條件 {cond.get('field')}"
    _col, label, unit, _dv = meta
    return f"{label} {cond.get('op', OP_GTE)} {cond.get('value')} {unit}"


def industries_of(fundamental_df):
    """【ADR-105】列出資料裡實際出現的產業別 (排序後),供 UI 下拉使用。

    不寫死清單:產業分類會隨官方調整增減 (實測上市 33 種、加上櫃共 36 種),
    寫死會在官方新增分類時無聲漏掉那些股票。
    """
    if fundamental_df is None or len(fundamental_df) == 0:
        return []
    if fp.COL_INDUSTRY not in fundamental_df.columns:
        return []
    vals = fundamental_df[fp.COL_INDUSTRY].dropna().astype(str).str.strip()
    return sorted({v for v in vals if v})


def match_industry(row, industries):
    """產業別是否符合。industries 為空/None 代表「不限產業」。

    產業別為空的個股在「有指定產業」時視為不符合——理由同決策三:
    「沒有資料」不可當成「符合」,否則指定產業時會混進分類不明的股票。
    """
    if not industries:
        return True
    val = row.get(fp.COL_INDUSTRY)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    return str(val).strip() in set(industries)


def eval_fundamental(row, conds):
    """對一列基本面資料評估所有基本面條件。回傳 (是否全部通過, 未通過原因)。

    【關鍵決定】欄位為空 (NaN) 一律視為**不通過**,不是跳過。
    理由:本益比「-」代表公司虧損 (沒有本益比),若當成「跳過此條件」,
    使用者設「本益比 <= 15」時會把虧損公司選進來——那是嚴重的選股錯誤。
    毛利率同理 (上櫃無此資料,見 fundamental_parser 的缺口說明)。
    """
    for c in (conds or []):
        meta = FUNDAMENTAL_FIELDS.get(c.get('field'))
        if not meta:
            return False, f"未知條件 {c.get('field')}"
        col = meta[0]
        val = row.get(col)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return False, f"{meta[1]} 無資料"
        try:
            thr = float(c.get('value'))
            v = float(val)
        except (TypeError, ValueError):
            return False, f"{meta[1]} 門檻或數值格式錯誤"
        if c.get('op', OP_GTE) == OP_GTE:
            if not (v >= thr):
                return False, f"{meta[1]} {v:g} < {thr:g}"
        else:
            if not (v <= thr):
                return False, f"{meta[1]} {v:g} > {thr:g}"
    return True, ''


def _attach_chips_if_needed(ohlcv, code, chip_conds, stock_chips, margin_chips,
                            allow_same_day=False):
    """技術/籌碼條件裡若含籌碼條件,才把籌碼併進 K 棒 (省掉不必要的 merge)。

    未來函數防護沿用 ADR-101:預設 T 日只讀得到 T-1 日(含)以前的籌碼。
    選股同樣需要這個保證——用「今天盤後才知道的籌碼」選出今天要買的股票,
    在實務上是做不到的。
    """
    if not chip_conds:
        return ohlcv
    sub = None
    if stock_chips is not None and len(stock_chips):
        sub = stock_chips[stock_chips['Code'].astype(str).str.upper() == str(code).upper()]
    return chips_features.attach_chips(ohlcv, stock_chips=sub, margin_chips=margin_chips,
                                       allow_same_day=allow_same_day)


def screen(fundamental_df, daily_df, conditions=None, fundamental_conds=None,
           logic='AND', stock_chips=None, margin_chips=None,
           allow_same_day_chips=False, min_bars=30, max_results=200,
           to_ohlcv=None, progress_cb=None, should_stop=None, industries=None):
    """執行選股。回傳 dict:{'rows': [...], 'scanned': n, 'passed': m, 'errors': [...]}。

    參數:
      fundamental_df : 基本面快照 (data/market_store.load_fundamental)
      daily_df       : 全市場日K長表 (data/market_store.load_daily_range)
      conditions     : 技術/籌碼條件 list,格式與策略完全相同
                       [{'type': 'ma_cross_up', 'params': {...}}, ...]
      fundamental_conds: [{'field': 'pe', 'op': '<=', 'value': 15}, ...]
      logic          : 技術/籌碼條件之間 AND / OR
      to_ohlcv       : callable(daily_df, code) → OHLCV DataFrame;
                       預設由呼叫端注入 (data/market_store.to_ohlcv_frame),
                       讓本模組不必依賴 data 層 (維持 core 的單向依賴)。
      should_stop    : callable() → bool,回 True 立即中止 (長時間掃描要能停)

    每個結果列包含代號、名稱、收盤、基本面欄位、通過的條件明細。
    """
    result = {'rows': [], 'scanned': 0, 'passed': 0, 'errors': []}
    if fundamental_df is None or len(fundamental_df) == 0:
        return result
    conds = list(conditions or [])
    f_conds = list(fundamental_conds or [])
    chip_conds = [c for c in conds if c.get('type') in strategy_engine.CHIP_CONDITION_IDS]

    # 第一階段:基本面 (向量化前先逐列判斷,量小且邏輯要與空值規則一致)
    stage1 = []
    for _, row in fundamental_df.iterrows():
        # 【ADR-105】產業別先擋:它是最便宜的判斷 (單純字串比對),放在最前面
        # 可以讓後面的數值比較與技術面掃描少跑很多檔。
        if not match_industry(row, industries):
            continue
        ok, _why = eval_fundamental(row, f_conds)
        if ok:
            stage1.append(row)
    result['scanned'] = len(fundamental_df)

    if not conds:
        # 只有基本面條件:直接輸出
        for row in stage1[:max_results]:
            result['rows'].append(_pack_row(row, [], f_conds))
        result['passed'] = len(stage1)
        return result

    if daily_df is None or len(daily_df) == 0:
        result['errors'].append(('資料', '本地沒有全市場日K,無法評估技術面條件'))
        return result

    total = len(stage1)
    for i, row in enumerate(stage1, 1):
        if should_stop is not None and should_stop():
            result['errors'].append(('中止', f'使用者中止 (已掃描 {i - 1}/{total})'))
            break
        if progress_cb is not None and (i % 20 == 0 or i == total):
            progress_cb(i, total)
        code = str(row.get(fp.COL_CODE))
        ohlcv = to_ohlcv(daily_df, code) if to_ohlcv else None
        if ohlcv is None or len(ohlcv) < min_bars:
            continue
        ohlcv = _attach_chips_if_needed(ohlcv, code, chip_conds, stock_chips,
                                        margin_chips, allow_same_day_chips)
        errs = []
        ok, details = strategy_engine.eval_conditions(ohlcv, conds, logic, errors=errs)
        if errs:
            # 條件評估出錯 (最常見:本地缺籌碼) 只記錄一次代表性訊息,
            # 不讓幾百檔各報一次把日誌洗掉
            for label, msg in errs[:1]:
                if not any(e[1] == msg for e in result['errors']):
                    result['errors'].append((label, msg))
        if ok:
            result['rows'].append(_pack_row(row, details, f_conds, ohlcv))
            if len(result['rows']) >= max_results:
                break
    result['passed'] = len(result['rows'])
    return result


def _pack_row(row, details, f_conds, ohlcv=None):
    """組出一列選股結果 (含基本面數值與通過的條件說明)。"""
    def _g(col):
        v = row.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return v
    last_close = _g(fp.COL_CLOSE)
    if last_close is None and ohlcv is not None and len(ohlcv):
        last_close = float(ohlcv['Close'].iloc[-1])
    industry = row.get(fp.COL_INDUSTRY)
    if industry is None or (isinstance(industry, float) and pd.isna(industry)):
        industry = ''
    return {
        'code': str(row.get(fp.COL_CODE)),
        'name': str(row.get(fp.COL_NAME) or ''),
        'industry': str(industry),
        'close': last_close,
        'pe': _g(fp.COL_PE), 'pb': _g(fp.COL_PB), 'yield': _g(fp.COL_YIELD),
        'eps': _g(fp.COL_EPS), 'gross_margin': _g(fp.COL_GROSS_MARGIN),
        'revenue_yoy': _g(fp.COL_REV_YOY), 'roe': _g(fp.COL_ROE),
        'matched': [lab for lab, hit in (details or []) if hit],
        'fundamental_matched': [fundamental_label(c) for c in (f_conds or [])],
    }


# ---------------------------------------------------------------------------
# 預設選股範本 (讓使用者一按就有結果,不必從零組條件)
# ---------------------------------------------------------------------------
PRESETS = {
    '價值股 (低本益比+高殖利率)': {
        'fundamental': [{'field': 'pe', 'op': OP_LTE, 'value': 15},
                        {'field': 'yield', 'op': OP_GTE, 'value': 4},
                        {'field': 'pb', 'op': OP_LTE, 'value': 2}],
        'conditions': [],
    },
    '成長股 (營收年增+高毛利)': {
        'fundamental': [{'field': 'revenue_yoy', 'op': OP_GTE, 'value': 20},
                        {'field': 'gross_margin', 'op': OP_GTE, 'value': 25},
                        {'field': 'eps', 'op': OP_GTE, 'value': 1}],
        'conditions': [],
    },
    '技術轉強 (均線黃金交叉+放量)': {
        'fundamental': [{'field': 'close', 'op': OP_GTE, 'value': 10}],
        'conditions': [{'type': 'ma_cross_up', 'params': {'fast': 5, 'slow': 20}},
                       {'type': 'volume_above_ma', 'params': {'n': 20, 'mult': 1.5}}],
    },
    '外資買超+站上季線': {
        'fundamental': [{'field': 'close', 'op': OP_GTE, 'value': 10}],
        'conditions': [{'type': 'chip_foreign_buy_streak', 'params': {'n': 3}},
                       {'type': 'price_above_ma', 'params': {'n': 60, 'kind': 'SMA'}}],
    },
    '基本面+技術面雙優': {
        'fundamental': [{'field': 'roe', 'op': OP_GTE, 'value': 5},
                        {'field': 'revenue_yoy', 'op': OP_GTE, 'value': 10},
                        {'field': 'pe', 'op': OP_LTE, 'value': 30}],
        'conditions': [{'type': 'price_above_ma', 'params': {'n': 20, 'kind': 'SMA'}}],
    },
}
