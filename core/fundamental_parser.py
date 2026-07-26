# -*- coding: utf-8 -*-
"""
core/fundamental_parser.py — 基本面資料解析 (ADR-103)

【資料源】全部官方一手來源,沿用 ADR-100 的原則 (不經第三方彙整):

  上市 (TWSE):
    BWIBBU_d                     本益比/股價淨值比/殖利率   (日更)
    STOCK_DAY_ALL                全市場日 OHLCV (CSV)       (日更)
    openapi t187ap05_L           月營收 (含官方年增率)      (月更)
    openapi t187ap06_L_ci        綜合損益表 (EPS/毛利)      (季更)
    openapi t187ap07_L_ci        資產負債表 (權益)          (季更)

  上櫃 (TPEx):
    tpex_mainboard_peratio_analysis   本益比/淨值比/殖利率  (日更)
    tpex_mainboard_daily_close_quotes 全市場日 OHLCV        (日更)
    mopsfin_t187ap05_O                月營收                (月更)
    mopsfin_t187ap07_O_ci             資產負債表            (季更)

【已知缺口 (誠實聲明)】上櫃**沒有**可用的綜合損益表端點 (t187ap06_O_ci 及
數個變體實測皆無回應),因此上櫃股票的 EPS 改由 `收盤價 ÷ 本益比` 反推——
這是本益比的定義本身,是精確的數學關係而非估計;但**毛利率無法取得**,
上櫃股票的毛利率一律為空,篩選時該條件會直接視為不符合 (不會誤判成通過)。

純解析,零網路、零 tkinter,可離線單元測試 (鐵則 11)。
"""
import csv
import io
import json

import pandas as pd

# 正規化後的統一欄位
COL_CODE = 'Code'
COL_NAME = 'Name'
COL_PE = 'PE'                  # 本益比
COL_PB = 'PB'                  # 股價淨值比
COL_YIELD = 'YieldPct'         # 殖利率 %
COL_CLOSE = 'Close'
COL_EPS = 'EPS'                # 每股盈餘 (元)
COL_GROSS_MARGIN = 'GrossMarginPct'
COL_REV_YOY = 'RevenueYoYPct'  # 月營收年增率 %
COL_REV_MOM = 'RevenueMoMPct'  # 月營收月增率 %
COL_REVENUE = 'MonthRevenue'
COL_EQUITY = 'Equity'
COL_ROE = 'ROEPct'

FUNDAMENTAL_COLS = [COL_CODE, COL_NAME, COL_CLOSE, COL_PE, COL_PB, COL_YIELD,
                    COL_EPS, COL_GROSS_MARGIN, COL_REV_YOY, COL_REV_MOM,
                    COL_REVENUE, COL_EQUITY, COL_ROE]


def to_float(v):
    """'12.34' / '1,234' / '-' / '' / None → float 或 None (無資料)。

    刻意回 None 而不是 0:本益比「-」代表公司虧損 (沒有本益比),與「本益比
    等於 0」意義完全不同。用 0 代替會讓「本益比 < 15」這種條件把虧損公司
    也選進來——那是嚴重的選股錯誤。
    """
    if v is None:
        return None
    s = str(v).replace(',', '').replace('%', '').strip()
    if not s or set(s) <= set('-') or s.upper() in ('NA', 'N/A', 'NULL', '--'):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _norm_code(v):
    return str(v or '').strip().upper()


def is_listed_equity(code):
    """只收「一般股票/ETF」代號:4 碼數字,或 00 開頭的 5~6 碼 (ETF/受益證券)。

    排除權證 (6 碼 03~08 開頭)、可轉債等,理由同 ADR-100:它們佔筆數多、
    對選股沒有意義。這裡用白名單而非黑名單,因為基本面資料的代號分佈比
    籌碼單純 (不含特別股大量出現的情形)。
    """
    c = _norm_code(code)
    if len(c) == 4 and c.isdigit():
        return True
    if c.startswith('00') and 5 <= len(c) <= 6:
        return True
    return False


# ---------------------------------------------------------------------------
# 1. 本益比 / 股價淨值比 / 殖利率
# ---------------------------------------------------------------------------
def parse_twse_valuation(payload):
    """TWSE BWIBBU_d → DataFrame[Code,Name,Close,PE,PB,YieldPct]。"""
    if not isinstance(payload, dict) or payload.get('stat') != 'OK':
        return pd.DataFrame(columns=[COL_CODE, COL_NAME, COL_CLOSE, COL_PE, COL_PB, COL_YIELD])
    out = []
    for r in payload.get('data') or []:
        if len(r) < 7:
            continue
        code = _norm_code(r[0])
        if not is_listed_equity(code):
            continue
        out.append({COL_CODE: code, COL_NAME: str(r[1]).strip(),
                    COL_CLOSE: to_float(r[2]), COL_YIELD: to_float(r[3]),
                    COL_PE: to_float(r[5]), COL_PB: to_float(r[6])})
    return pd.DataFrame(out, columns=[COL_CODE, COL_NAME, COL_CLOSE, COL_PE, COL_PB, COL_YIELD])


def parse_tpex_valuation(rows):
    """TPEx tpex_mainboard_peratio_analysis (list of dict) → 同上欄位。

    櫃買這個端點沒有收盤價,Close 留 None,由呼叫端用日行情補。
    """
    out = []
    for d in (rows or []):
        if not isinstance(d, dict):
            continue
        code = _norm_code(d.get('SecuritiesCompanyCode'))
        if not is_listed_equity(code):
            continue
        out.append({COL_CODE: code, COL_NAME: str(d.get('CompanyName', '')).strip(),
                    COL_CLOSE: None,
                    COL_YIELD: to_float(d.get('YieldRatio')),
                    COL_PE: to_float(d.get('PriceEarningRatio')),
                    COL_PB: to_float(d.get('PriceBookRatio'))})
    return pd.DataFrame(out, columns=[COL_CODE, COL_NAME, COL_CLOSE, COL_PE, COL_PB, COL_YIELD])


# ---------------------------------------------------------------------------
# 2. 全市場日 OHLCV
# ---------------------------------------------------------------------------
DAILY_COLS = ['Date', COL_CODE, COL_NAME, 'Open', 'High', 'Low', 'Close', 'Volume']


def parse_twse_daily_all(raw, date_iso=None):
    """TWSE STOCK_DAY_ALL (CSV bytes/str) → 全市場日 OHLCV。

    這個端點是整個選股工具能成立的關鍵:一次請求就拿到全上市股票的當日
    OHLCV,技術面篩選因此完全不需要逐檔打 shioaji (那會直接撞上鐵則 5 的
    API 配額上限)。
    """
    text = raw.decode('utf-8-sig', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for r in rows[1:]:
        if len(r) < 9:
            continue
        code = _norm_code(r[1])
        if not is_listed_equity(code):
            continue
        d = date_iso
        if not d:
            raw_d = str(r[0]).strip()
            if len(raw_d) == 7 and raw_d.isdigit():      # 民國 1150724
                d = f"{int(raw_d[:3]) + 1911:04d}-{raw_d[3:5]}-{raw_d[5:7]}"
        close = to_float(r[8])
        if close is None:
            continue
        out.append({'Date': d, COL_CODE: code, COL_NAME: str(r[2]).strip(),
                    'Open': to_float(r[5]), 'High': to_float(r[6]),
                    'Low': to_float(r[7]), 'Close': close,
                    'Volume': to_float(r[3]) or 0.0})
    return pd.DataFrame(out, columns=DAILY_COLS)


def parse_twse_mi_index(payload, date_iso=None):
    """TWSE MI_INDEX (type=ALLBUT0999) → 全市場日 OHLCV。

    【為什麼用這個而不是 STOCK_DAY_ALL】STOCK_DAY_ALL 只回「最新一日」,
    沒有日期參數,拿它建歷史要等 N 天才有 N 根 K 棒,技術面篩選等於不能用。
    MI_INDEX 吃 date 參數 (實測可回溯數月),逐日抓就能一次補齊歷史。

    回傳的表在 payload['tables'] 裡,欄位數最多的那張才是個股行情
    (其餘是大盤統計/漲跌家數等小表),因此用「筆數最大」挑表而不是寫死索引
    ——證交所調整表格順序時,寫死索引會無聲抓錯表。
    """
    if not isinstance(payload, dict) or str(payload.get('stat', '')).upper() != 'OK':
        return pd.DataFrame(columns=DAILY_COLS)
    best, best_n = None, 0
    for tb in payload.get('tables') or []:
        rows = tb.get('data') or []
        fields = str(tb.get('fields') or '')
        if len(rows) > best_n and '收盤價' in fields:
            best, best_n = tb, len(rows)
    if best is None:
        return pd.DataFrame(columns=DAILY_COLS)
    d = date_iso
    if not d:
        raw_d = str(payload.get('date') or '')
        if len(raw_d) == 8 and raw_d.isdigit():
            d = f"{raw_d[:4]}-{raw_d[4:6]}-{raw_d[6:8]}"
    fields = best.get('fields') or []
    idx = {name: i for i, name in enumerate(fields)}
    ci, ni = idx.get('證券代號', 0), idx.get('證券名稱', 1)
    oi, hi = idx.get('開盤價', 5), idx.get('最高價', 6)
    li, cli = idx.get('最低價', 7), idx.get('收盤價', 8)
    vi = idx.get('成交股數', 2)
    out = []
    for r in best.get('data') or []:
        if len(r) <= max(ci, ni, oi, hi, li, cli, vi):
            continue
        code = _norm_code(r[ci])
        if not is_listed_equity(code):
            continue
        close = to_float(r[cli])
        if close is None:
            continue
        out.append({'Date': d, COL_CODE: code, COL_NAME: str(r[ni]).strip(),
                    'Open': to_float(r[oi]), 'High': to_float(r[hi]),
                    'Low': to_float(r[li]), 'Close': close,
                    'Volume': to_float(r[vi]) or 0.0})
    return pd.DataFrame(out, columns=DAILY_COLS)


def roc_date_to_iso(v):
    """民國 7 碼日期 '1150724' → '2026-07-24';無法解析回 None。

    櫃買 OpenAPI 的 Date 欄是這種格式。第一版沒有解析它、只靠呼叫端傳
    date_iso,結果呼叫端沒傳時整批資料的日期是 None,存檔後永遠讀不回來。
    """
    s = str(v or '').strip()
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def parse_tpex_daily_all(rows, date_iso=None):
    """TPEx tpex_mainboard_daily_close_quotes (list of dict) → 全市場日 OHLCV。"""
    out = []
    for d in (rows or []):
        if not isinstance(d, dict):
            continue
        code = _norm_code(d.get('SecuritiesCompanyCode') or d.get('Code'))
        if not is_listed_equity(code):
            continue
        close = to_float(d.get('Close') or d.get('ClosingPrice'))
        if close is None:
            continue
        day = date_iso or roc_date_to_iso(d.get('Date'))
        out.append({'Date': day, COL_CODE: code,
                    COL_NAME: str(d.get('CompanyName') or d.get('Name') or '').strip(),
                    'Open': to_float(d.get('Open') or d.get('OpeningPrice')),
                    'High': to_float(d.get('High') or d.get('HighestPrice')),
                    'Low': to_float(d.get('Low') or d.get('LowestPrice')),
                    'Close': close,
                    'Volume': to_float(d.get('TradingShares') or d.get('Volume')) or 0.0})
    return pd.DataFrame(out, columns=DAILY_COLS)


# ---------------------------------------------------------------------------
# 3. 月營收 (官方直接提供年增率,不需自己算)
# ---------------------------------------------------------------------------
def parse_monthly_revenue(rows):
    """t187ap05_L / mopsfin_t187ap05_O → DataFrame[Code,MonthRevenue,YoY,MoM]。

    官方欄位已含「營業收入-去年同月增減(%)」,直接採用,不自行計算——
    自行計算要處理去年同月為 0/負值等邊界,官方數字才是對帳基準。
    """
    out = []
    for d in (rows or []):
        if not isinstance(d, dict):
            continue
        code = _norm_code(d.get('公司代號') or d.get('SecuritiesCompanyCode'))
        if not is_listed_equity(code):
            continue
        out.append({
            COL_CODE: code,
            COL_NAME: str(d.get('公司名稱') or d.get('CompanyName') or '').strip(),
            COL_REVENUE: to_float(d.get('營業收入-當月營收')),
            COL_REV_YOY: to_float(d.get('營業收入-去年同月增減(%)')),
            COL_REV_MOM: to_float(d.get('營業收入-上月比較增減(%)')),
        })
    return pd.DataFrame(out, columns=[COL_CODE, COL_NAME, COL_REVENUE, COL_REV_YOY, COL_REV_MOM])


# ---------------------------------------------------------------------------
# 4. 綜合損益表 (EPS / 毛利率) — 僅上市有此端點
# ---------------------------------------------------------------------------
def _code_of(d):
    """取公司代號。

    【實測踩到的坑】上市 (TWSE openapi) 用中文欄名「公司代號」,上櫃 (TPEx
    openapi) 用英文欄名「SecuritiesCompanyCode」——同一張財報、兩種欄名。
    只讀其中一種會**整批漏掉另一個市場**且完全看不出來 (筆數少了但不報錯),
    這正是本模組第一版把上櫃損益表全部濾掉的原因。
    """
    return _norm_code(d.get('公司代號') or d.get('SecuritiesCompanyCode')
                      or d.get('公司代號 ') or d.get('Code'))


def parse_income_statement(rows):
    """綜合損益表 (上市 t187ap06_L_* / 上櫃 mopsfin_t187ap06_O_*)
    → DataFrame[Code,EPS,GrossMarginPct]。

    【六種產業別】財報依產業分成 ci/basi/bd/fh/ins/mim 六個端點,必須全部抓
    才完整:只抓 ci 會漏掉全部金控股 (富邦金/國泰金/玉山金都在 fh)。
    金控與保險沒有「營業收入/營業毛利」欄位 (損益結構本就不同),此時毛利率
    為空——那是正確的,不是資料缺失。EPS 則六種都有。

    毛利率 = 營業毛利 / 營業收入 × 100。營收為 0 或缺值時回 None
    (不可回 0——那會讓「毛利率 > 20%」的判斷變成「不符合」而不是「無資料」,
    兩者在選股上意義不同,見 to_float 的說明)。
    """
    out = []
    for d in (rows or []):
        if not isinstance(d, dict):
            continue
        code = _code_of(d)
        if not is_listed_equity(code):
            continue
        rev = to_float(d.get('營業收入'))
        gross = to_float(d.get('營業毛利（毛損）淨額'))
        if gross is None:
            gross = to_float(d.get('營業毛利（毛損）'))
        gm = (gross / rev * 100.0) if (rev and gross is not None and rev != 0) else None
        out.append({COL_CODE: code,
                    COL_EPS: to_float(d.get('基本每股盈餘（元）')),
                    COL_GROSS_MARGIN: round(gm, 2) if gm is not None else None})
    return pd.DataFrame(out, columns=[COL_CODE, COL_EPS, COL_GROSS_MARGIN])


def parse_balance_sheet(rows):
    """資產負債表 (上市 t187ap07_L_* / 上櫃 mopsfin_t187ap07_O_*)
    → DataFrame[Code,Equity]。

    【欄位差異】上市有「權益總額」;上櫃**沒有**這個欄位,只有「資產總計」與
    「負債總計」,因此用會計恆等式 權益 = 資產 − 負債 推導 (這是定義,不是估計)。
    """
    out = []
    for d in (rows or []):
        if not isinstance(d, dict):
            continue
        code = _code_of(d)
        if not is_listed_equity(code):
            continue
        eq = to_float(d.get('權益總額'))
        if eq is None:
            eq = to_float(d.get('歸屬於母公司業主之權益合計'))
        if eq is None:
            assets = to_float(d.get('資產總計')) or to_float(d.get('資產總額'))
            debts = to_float(d.get('負債總計')) or to_float(d.get('負債總額'))
            if assets is not None and debts is not None:
                eq = assets - debts
        out.append({COL_CODE: code, COL_EQUITY: eq})
    return pd.DataFrame(out, columns=[COL_CODE, COL_EQUITY])


# ---------------------------------------------------------------------------
# 5. 合併 + 推導欄位
# ---------------------------------------------------------------------------
def derive_eps_from_pe(close, pe):
    """EPS = 收盤價 ÷ 本益比。

    這是本益比的**定義本身**,不是估計。上櫃沒有損益表端點時用它補 EPS。
    pe <= 0 (虧損公司沒有有意義的本益比) 一律回 None。
    """
    if close is None or pe is None or pe <= 0:
        return None
    return round(close / pe, 4)


def derive_roe(eps, pb, close):
    """ROE% ≈ EPS ÷ 每股淨值 × 100,其中 每股淨值 = 收盤價 ÷ 股價淨值比。

    【誠實標示】這是由 EPS/PB/收盤價推導的近似值,不是財報上的 ROE:
      * EPS 是季更、PB 是日更,兩者期別不一致;
      * EPS 用的是單季或累計視資料源而定,與年化 ROE 定義有落差。
    用於初步篩選足夠,但不應拿它跟財報公告的 ROE 直接對帳。
    """
    if eps is None or pb is None or close is None or pb <= 0 or close <= 0:
        return None
    bps = close / pb
    if bps <= 0:
        return None
    return round(eps / bps * 100.0, 2)


def merge_fundamentals(valuation=None, revenue=None, income=None, balance=None,
                       daily=None):
    """把各來源依 Code 合併成一張基本面表 (FUNDAMENTAL_COLS)。

    缺的欄位一律留 None,絕不填 0——「沒有資料」與「數值為 0」在選股條件上
    的意義完全不同 (見 to_float 說明)。
    """
    base = None
    for part in (valuation, revenue, income, balance):
        if part is None or len(part) == 0:
            continue
        p = part.copy()
        if base is None:
            base = p
        else:
            dup = [c for c in p.columns if c in base.columns and c != COL_CODE]
            base = base.merge(p.drop(columns=dup), on=COL_CODE, how='outer')
    if base is None:
        return pd.DataFrame(columns=FUNDAMENTAL_COLS)
    # 用日行情補收盤價 (櫃買的估值端點沒有收盤價)
    if daily is not None and len(daily) and 'Close' in daily.columns:
        d = daily[[COL_CODE, 'Close']].drop_duplicates(COL_CODE)
        if COL_CLOSE in base.columns:
            base = base.merge(d.rename(columns={'Close': '_c2'}), on=COL_CODE, how='left')
            base[COL_CLOSE] = base[COL_CLOSE].where(base[COL_CLOSE].notna(), base['_c2'])
            base = base.drop(columns=['_c2'])
        else:
            base = base.merge(d.rename(columns={'Close': COL_CLOSE}), on=COL_CODE, how='left')
    for c in FUNDAMENTAL_COLS:
        if c not in base.columns:
            base[c] = None
    # EPS 缺 → 用 收盤價/本益比 反推 (上櫃唯一取得 EPS 的途徑)
    need_eps = base[COL_EPS].isna()
    if need_eps.any():
        base.loc[need_eps, COL_EPS] = [
            derive_eps_from_pe(c, p) for c, p in
            zip(base.loc[need_eps, COL_CLOSE], base.loc[need_eps, COL_PE])]
    base[COL_ROE] = [derive_roe(e, pb, c) for e, pb, c in
                     zip(base[COL_EPS], base[COL_PB], base[COL_CLOSE])]
    return base[FUNDAMENTAL_COLS].reset_index(drop=True)
