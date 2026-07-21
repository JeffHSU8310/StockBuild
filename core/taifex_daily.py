# -*- coding: utf-8 -*-
"""
core/taifex_daily.py — 臺灣期交所 (TAIFEX) 官方「每日交易行情」解析與連續日K建構

【ADR-049】需求:shioaji kbars 只回分K且歷史深度有限 (日K約 1~2 年),使用者
要更長的期貨日K歷史。期交所官網免費提供每日行情下載 (日K可回溯到商品上市,
臺股期貨 TX 自 1998/07/21 起),本模組負責:

  1. 解析期交所 CSV (兩種來源、同一套欄位):
     - 「期貨每日交易行情下載」查詢 (futDataDown,日期區間) 回傳的 CSV
     - 「每日行情下載專區」的 Daily_YYYY_MM_DD.zip 內的 CSV
     欄位:交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,...,
     成交量,...,交易時段(一般/盤後),...。舊年度資料沒有「交易時段」欄
     (夜盤 2017/05 才上線),視為全部「一般」。編碼可能是 Big5(cp950) 或
     UTF-8(含BOM),逐一嘗試。

  2. 建構「前月連續」日K序列 (對應 shioaji 的 R1 連續合約視角):
     - 每個交易日取「到期月份為純 6 碼數字」中最小的月份 (= 近月),
       排除週契約 (202607W2) 與價差單 (202607/202609)。
     - 近全 (全時段) 合併,與 ADR-007 的交易日聚合定義一致:
       期交所把「前一日傍晚 15:00 起的夜盤」記在交易日 D 的「盤後」列,
       日盤記在「一般」列。故 Open=盤後開盤(無夜盤則一般開盤)、
       Close=一般收盤、High/Low=兩時段極值、Volume=兩時段加總。

  3. 與 shioaji 聚合出的日K銜接:shioaji 為權威資料源 (鐵則 12),重疊日期
     一律以 shioaji 為準,期交所資料只用來「往前補更早的歷史」。

純邏輯、零 tkinter、零 shioaji、零網路依賴;下載動作在 GUI 層,
檔案存取在 data/taifex_store.py。可離線單元測試。
"""
import io
import re
import zipfile
from datetime import date, timedelta

import pandas as pd

# shioaji 商品碼 (前3碼) → 期交所 commodity_id / CSV「契約」欄值。
# 寧缺勿錯:只收錄有把握的指數期貨家族;不在表內的商品不支援匯入。
PRODUCT_MAP = {
    'TXF': 'TX',    # 臺股期貨
    'MXF': 'MTX',   # 小型臺指期貨
    'TMF': 'TMF',   # 微型臺指期貨
    'EXF': 'TE',    # 電子期貨
    'FXF': 'TF',    # 金融期貨
}

# 期交所各商品「每日行情」最早可回溯日期 (官網公告的資料起始日)
PRODUCT_EARLIEST = {
    'TX': date(1998, 7, 21),
    'MTX': date(2001, 4, 9),
    'TMF': date(2024, 7, 29),
    'TE': date(1999, 7, 21),
    'TF': date(1999, 7, 21),
}

_PURE_MONTH_RE = re.compile(r'^\d{6}$')
_DATE_RE = re.compile(r'^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$')

# 欄位名稱對照 (期交所 CSV 標頭,去除空白後比對)
_COL_KEYS = {
    '交易日期': 'date',
    '契約': 'prod',
    '到期月份(週別)': 'month',
    '開盤價': 'open',
    '最高價': 'high',
    '最低價': 'low',
    '收盤價': 'close',
    '成交量': 'volume',
    '交易時段': 'session',
}


def decode_csv_bytes(b):
    """期交所 CSV 編碼不定 (Big5 / UTF-8 BOM),逐一嘗試解碼;都失敗最後用
    cp950 以 replace 硬解,確保至少能看到內容 (數字欄位不受影響)。"""
    for enc in ('utf-8-sig', 'cp950', 'utf-8'):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return b.decode('cp950', errors='replace')


def _parse_num(s):
    """'46,281' / '46281.0' / '-' / '' → float 或 None。"""
    s = str(s or '').strip().replace(',', '')
    if not s or s in ('-', '--'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(s):
    m = _DATE_RE.match(str(s or '').strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_csv_text(text):
    """解析一份期交所每日行情 CSV 內文 → list[dict]。

    只保留能辨識出 交易日期/契約/到期月份 的資料列;價量解析失敗以 None 保留
    (盤後列偶有 '-' 無成交)。無「交易時段」欄的舊資料一律視為 '一般'。
    非行情列 (免責聲明、空行) 自然被過濾。
    """
    rows = []
    reader = io.StringIO(text)
    header_map = None
    for line in reader:
        line = line.rstrip('\r\n')
        if not line.strip():
            continue
        cells = [c.strip().strip('"') for c in line.split(',')]
        # 期交所價格欄可能帶千分位逗號並以引號包住 → 先粗切再靠引號重組太複雜;
        # 實測期交所 CSV 的數值不加千分位引號,粗切即可。萬一未來加了引號,
        # _parse_num 已把逗號去除,只會多切出空欄,靠 header 索引仍取得到值。
        if header_map is None:
            hm = {}
            for i, c in enumerate(cells):
                key = _COL_KEYS.get(c.replace(' ', ''))
                if key:
                    hm[key] = i
            if 'date' in hm and 'prod' in hm and 'month' in hm:
                header_map = hm
            continue
        try:
            d = _parse_date(cells[header_map['date']])
            if d is None:
                continue
            prod = cells[header_map['prod']].strip().upper()
            month = cells[header_map['month']].replace(' ', '')
            sess_idx = header_map.get('session')
            session = cells[sess_idx].strip() if (sess_idx is not None and sess_idx < len(cells)) else '一般'
            if not session:
                session = '一般'
            rows.append({
                'date': d, 'prod': prod, 'month': month, 'session': session,
                'open': _parse_num(cells[header_map['open']]) if header_map['open'] < len(cells) else None,
                'high': _parse_num(cells[header_map['high']]) if header_map['high'] < len(cells) else None,
                'low': _parse_num(cells[header_map['low']]) if header_map['low'] < len(cells) else None,
                'close': _parse_num(cells[header_map['close']]) if header_map['close'] < len(cells) else None,
                'volume': _parse_num(cells[header_map['volume']]) if header_map['volume'] < len(cells) else None,
            })
        except (IndexError, KeyError):
            continue
    return rows


def extract_rows_from_bytes(b, filename=''):
    """從下載回應或使用者選檔的位元組取出行情列;.zip 會展開所有 .csv 成員。
    回應若是 HTML (查詢錯誤頁) 會因找不到標頭而回空列表,由呼叫端判定失敗。"""
    name = (filename or '').lower()
    if name.endswith('.zip') or b[:2] == b'PK':
        rows = []
        with zipfile.ZipFile(io.BytesIO(b)) as zf:
            for member in zf.namelist():
                if member.lower().endswith('.csv'):
                    rows.extend(parse_csv_text(decode_csv_bytes(zf.read(member))))
        return rows
    return parse_csv_text(decode_csv_bytes(b))


def build_front_month_daily(rows, taifex_prod, session='all'):
    """由行情列建構「前月連續、近全合併」日K DataFrame (index=Timestamp,
    欄位 Open/High/Low/Close/Volume,與 _resample_sj_df 期貨日K同構)。

    規則:
      - 只取 契約 == taifex_prod 且 到期月份為純 6 碼 (排除週契約/價差單)。
      - 每個交易日的近月 = 該日出現的最小月份。
      - 一般列連收盤價都沒有的日子 (整日無成交) 跳過。

    【ADR-058】session 參數:
      * 'all' (預設,近全合併):Open=盤後(夜盤)開盤,無則一般;Close=一般收盤,
        無則盤後;High/Low 取兩時段極值;Volume 兩時段加總。與 ADR-007 的
        交易日定義一致。
      * 'day' (只取日盤):完全忽略「盤後」列,只用「一般」列。
        存在理由:期交所夜盤 2017-05-15 才上線,在那之前根本沒有盤後列,
        所以 'all' 序列的定義會在 2017-05-15 中途改變 (實測臺指期隔夜跳空
        中位數從 0.38% 掉到 0.064%,差 5.9 倍)。要做橫跨該日的長期回測時,
        用 'day' 才有前後一致的口徑。
    """
    taifex_prod = str(taifex_prod).strip().upper()
    by_day = {}
    for r in rows:
        if r['prod'] != taifex_prod or not _PURE_MONTH_RE.match(r['month']):
            continue
        by_day.setdefault(r['date'], []).append(r)

    recs = []
    for d in sorted(by_day):
        day_rows = by_day[d]
        front = min(r['month'] for r in day_rows)
        reg = next((r for r in day_rows if r['month'] == front and r['session'] == '一般'), None)
        aft = next((r for r in day_rows if r['month'] == front and r['session'] == '盤後'), None)
        if str(session) == 'day':
            aft = None   # 【ADR-058】只取日盤:盤後列直接不看
        close = (reg and reg['close']) or (aft and aft['close'])
        if close is None:
            continue
        o = (aft and aft['open']) or (reg and reg['open']) or close
        highs = [v for v in ((reg and reg['high']), (aft and aft['high'])) if v is not None]
        lows = [v for v in ((reg and reg['low']), (aft and aft['low'])) if v is not None]
        vol = sum(v for v in ((reg and reg['volume']), (aft and aft['volume'])) if v is not None)
        recs.append({'ts': pd.Timestamp(d), 'Open': o,
                     'High': max(highs) if highs else max(o, close),
                     'Low': min(lows) if lows else min(o, close),
                     'Close': close, 'Volume': float(vol)})
    if not recs:
        return pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
    df = pd.DataFrame(recs).set_index('ts')
    df.index.name = None
    return df.sort_index()


def merge_daily(old_df, new_df):
    """合併既有儲存與新匯入的日K;同日期以「新匯入」為準 (重匯修正資料)。"""
    if old_df is None or old_df.empty:
        return new_df.copy() if new_df is not None else pd.DataFrame()
    if new_df is None or new_df.empty:
        return old_df.copy()
    out = pd.concat([old_df, new_df])
    return out[~out.index.duplicated(keep='last')].sort_index()


def extend_shioaji_df(sj_tf_df, taifex_daily_df, tf):
    """把期交所日K「往前接」在 shioaji 聚合結果之前 (只補更早的歷史)。

    - 日K:直接前接 (期交所日期 < shioaji 第一根日期)。
    - 周K/月K:期交所日K先做與 ADR-007 相同的 W-MON / MS 二次聚合,
      再前接 (期交所聚合 label < shioaji 第一根 label)。邊界那一根以
      shioaji 為準 (即使 shioaji 端只涵蓋部分週/月),維持鐵則 12 的
      「重疊處 shioaji 權威」原則。
    - 【ADR-060 修正】shioaji 端為空時,直接回傳「期交所資料本身」(依 tf 聚合),
      而不是回傳那個空表。
      根因:ADR-058 讓「期交所已完整涵蓋」的情況完全跳過券商下載,shioaji 端
      因此合法地是空的;但這裡舊寫法看到空就原樣回傳,結果圖表與回測都變成
      「取不到資料」—— 等於最佳化把功能弄壞了。空的 shioaji 不代表沒有資料,
      只代表這次不需要它。
    - 期交所端為空則原樣回傳 shioaji 的結果。
    """
    agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}

    def _to_tf(daily):
        if tf == "周K":
            return daily.resample('W-MON', label='left', closed='left').agg(agg).dropna()
        if tf == "月K":
            return daily.resample('MS', label='left', closed='left').agg(agg).dropna()
        if tf == "日K":
            return daily
        return None   # 分K不適用官方日行情

    if taifex_daily_df is None or taifex_daily_df.empty:
        return sj_tf_df
    if sj_tf_df is None or sj_tf_df.empty:
        only = _to_tf(taifex_daily_df)
        return sj_tf_df if only is None else only.copy()
    if tf == "周K":
        ext = taifex_daily_df.resample('W-MON', label='left', closed='left').agg(agg).dropna()
    elif tf == "月K":
        ext = taifex_daily_df.resample('MS', label='left', closed='left').agg(agg).dropna()
    elif tf == "日K":
        ext = taifex_daily_df
    else:
        return sj_tf_df  # 分K不適用官方日行情
    first = sj_tf_df.index[0]
    ext = ext[ext.index < first]
    if ext.empty:
        return sj_tf_df
    cols = [c for c in sj_tf_df.columns if c in ext.columns]
    out = pd.concat([ext[cols], sj_tf_df])
    return out[~out.index.duplicated(keep='last')].sort_index()


def month_chunks(start_d, end_d, max_days=28):
    """把日期區間切成 ≤max_days 天的小段 (期交所查詢介面限制單次區間長度,
    官網標示「日期區間不可超過1個月」,取 28 天保守值)。回傳 [(s,e), ...]。"""
    chunks = []
    s = start_d
    while s <= end_d:
        e = min(s + timedelta(days=max_days - 1), end_d)
        chunks.append((s, e))
        s = e + timedelta(days=1)
    return chunks


# ============================================================
# 【ADR-058】盤別口徑偵測 (使用者需求 #3)
# ============================================================

NIGHT_SESSION_START = pd.Timestamp('2017-05-15')   # 期交所盤後交易時段 (夜盤) 上線日


def detect_session_regime(df, night_start=None):
    """用「隔夜跳空幅度」實證判斷一份日K序列是「含夜盤」還是「只有日盤」。

    原理 (不需要相信任何欄位標示,直接從價格行為看):
      * 含夜盤 (近全):交易日 D 的 Open 是前一天 15:00 開始的夜盤開盤,距離
        前一日 13:45 收盤只隔 75 分鐘 → |Open(D) - Close(D-1)| 很小。
      * 只有日盤:Open 是當天 08:45,距離前一日 13:45 收盤隔了約 19 小時,
        期間國際市場照常波動 → 隔夜跳空明顯大得多。

    實測臺指期 (使用者提供的期交所 TX.csv,6947 根):
        夜盤上線前 隔夜跳空中位數 0.3794%
        夜盤上線後 隔夜跳空中位數 0.0644%   → 相差 5.89 倍
    小台 (MTX) 為 0.3465% → 0.0631%,同樣約 5.5 倍。差異極為顯著,
    因此用「上線日前後中位數比值」判斷非常可靠。

    回傳 dict:
      {'regime': 'day_only' | 'all_session' | 'mixed' | 'unknown',
       'pre_gap': 上線日前中位跳空%, 'post_gap': 上線日後中位跳空%,
       'ratio': 前/後 比值, 'spans_night_start': 是否橫跨上線日,
       'note': 給使用者看的白話說明}
    """
    ns = pd.Timestamp(night_start) if night_start is not None else NIGHT_SESSION_START
    out = {'regime': 'unknown', 'pre_gap': None, 'post_gap': None, 'ratio': None,
           'spans_night_start': False, 'note': ''}
    if df is None or len(df) < 30 or 'Open' not in df.columns or 'Close' not in df.columns:
        out['note'] = "資料量不足,無法判斷盤別口徑。"
        return out

    gap = (df['Open'] - df['Close'].shift(1)).abs() / df['Close'].shift(1) * 100.0
    gap = gap.replace([float('inf'), float('-inf')], pd.NA).dropna()
    if gap.empty:
        out['note'] = "無法計算隔夜跳空,無法判斷盤別口徑。"
        return out

    pre = gap[gap.index < ns]
    post = gap[gap.index >= ns]
    out['spans_night_start'] = bool(len(pre) >= 20 and len(post) >= 20)

    if out['spans_night_start']:
        pre_m, post_m = float(pre.median()), float(post.median())
        out['pre_gap'], out['post_gap'] = pre_m, post_m
        ratio = (pre_m / post_m) if post_m > 1e-9 else None
        out['ratio'] = ratio
        if ratio is not None and ratio >= 2.5:
            out['regime'] = 'mixed'
            out['note'] = (f"⚠ 這段資料橫跨 2017-05-15 夜盤上線日,而且前後口徑不同:"
                           f"上線前隔夜跳空中位數 {pre_m:.3f}%、上線後 {post_m:.3f}% (差 {ratio:.1f} 倍)。"
                           f"代表上線前只有日盤、上線後是含夜盤的近全序列 —— "
                           f"策略在前後兩段面對的其實是不同口徑的商品。"
                           f"要做一致口徑的長期回測,請改用「只用日盤」。")
        else:
            out['regime'] = 'day_only'
            out['note'] = (f"這段資料橫跨夜盤上線日,但前後隔夜跳空相近 "
                           f"({pre_m:.3f}% vs {post_m:.3f}%),整段都是同一口徑 (只有日盤)。")
        return out

    # 未橫跨上線日:只能用絕對值粗判 (門檻取兩者中間 0.2%)
    m = float(gap.median())
    if df.index[-1] < ns:
        out['regime'] = 'day_only'
        out['note'] = f"整段都早於 2017-05-15 夜盤上線日,必然只有日盤 (中位跳空 {m:.3f}%)。"
    else:
        out['post_gap'] = m
        out['regime'] = 'all_session' if m < 0.2 else 'day_only'
        out['note'] = (f"中位隔夜跳空 {m:.3f}% → 研判為"
                       f"{'含夜盤的近全序列' if m < 0.2 else '只有日盤的序列'}。")
    return out


def split_coverage(hist_df, start_dt, end_dt):
    """【ADR-058】回傳 (期交所已涵蓋的結束日, 仍需向券商下載的起始日)。

    用途:回測期間若大部分已經被本地期交所歷史涵蓋,就不必再向券商重抓那一段
    —— 這是使用者回報「下載一直被券商擋」的最有效解法 (不是想辦法閃過流量
    管制,而是根本不要發那些請求)。

    回傳 (covered_until, need_from):
      * covered_until: 期交所資料涵蓋到哪一天 (None = 完全沒有可用資料)
      * need_from    : 還需要向券商抓的起始日 (None = 完全不需要下載)
    """
    if hist_df is None or hist_df.empty:
        return None, start_dt
    s, e = pd.Timestamp(start_dt).normalize(), pd.Timestamp(end_dt).normalize()
    lo, hi = hist_df.index[0].normalize(), hist_df.index[-1].normalize()
    if lo > s:
        # 期交所資料起點比要求的還晚 → 前段補不了,整段仍需下載
        return hi, s
    if hi >= e:
        return hi, None            # 完全涵蓋,不必下載
    return hi, (hi + pd.Timedelta(days=1))   # 只需補「期交所結束日之後」的尾巴
