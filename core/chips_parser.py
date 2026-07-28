# -*- coding: utf-8 -*-
"""
core/chips_parser.py — 台股籌碼資料解析 (ADR-100)

【資料源】全部是官方一手來源,不經第三方彙整 (呼應 ADR-011 移除 FinMind 時
「要恢復籌碼需先找到新資料源並另開 ADR」的要求):

  1. 證交所 TWSE `fund/BFI82U`      — 市場三大法人買賣金額 (加權指數用) JSON
  2. 證交所 TWSE `fund/T86`         — 上市個股三大法人買賣超           JSON
  3. 證交所 TWSE `marginTrading/MI_MARGN` — 市場信用交易 (融資融券)     JSON
  4. 櫃買  TPEx  `insti/dailyTrade` — 上櫃個股三大法人買賣超           JSON
  5. 期交所 TAIFEX `futContractsDateDown` — 期貨三大法人未平倉      CSV(cp950)

【本模組職責】只做「原始回應 → 正規化 DataFrame」的純轉換:
零網路、零 tkinter、零 shioaji,可離線單元測試 (鐵則 11)。
下載動作在 GUI 層 (背景執行緒),檔案存取在 data/chips_store.py——
這個分工沿用 ADR-049 (期交所日K) 已經驗證過的模式,不另發明。

【櫃買欄位語意的查證方式】TPEx 回傳的 fields 有 7 組重複的
「買進股數/賣出股數/買賣超股數」,JSON 本身沒有提供分組名稱。本模組採用的
對應是用「資料自身的加總關係」反推並全量驗證出來的 (895/895 列吻合):
    組2 = 組0 + 組1   (外資合計   = 外資及陸資 + 外資自營商)
    組6 = 組4 + 組5   (自營商合計 = 自行買賣   + 避險)
    合計 = 組2 + 組3 + 組6  (三大法人 = 外資 + 投信 + 自營商)
若日後櫃買改版導致這些恆等式不成立,`verify_tpex_layout()` 會回報,
不要盲目沿用舊索引 (這正是籌碼資料最容易無聲出錯的地方)。
"""
import csv
import io
import re

import pandas as pd

# --- 正規化後的統一欄位名 (四個來源最終都轉成這套詞彙) ---------------------
COL_DATE = 'Date'
COL_CODE = 'Code'
COL_NAME = 'Name'
COL_FOREIGN = 'Foreign'      # 外資及陸資 (含外資自營商) 買賣超
COL_TRUST = 'Trust'          # 投信買賣超
COL_DEALER = 'Dealer'        # 自營商買賣超 (自行 + 避險)
COL_TOTAL = 'InstTotal'      # 三大法人合計買賣超

STOCK_CHIP_COLS = [COL_DATE, COL_CODE, COL_NAME, COL_FOREIGN, COL_TRUST, COL_DEALER, COL_TOTAL]


def to_num(v):
    """把 '1,234' / '-1,234' / '--' / '' / None 轉成 int;無法解析一律回 0。

    證交所/櫃買的數字都是帶千分位的字串,且「無資料」有多種寫法
    ('--'、'---'、空字串)。這裡統一吃掉,不讓上層每個欄位各寫一次。
    """
    if v is None:
        return 0
    s = str(v).replace(',', '').replace('　', '').strip()
    if not s or set(s) <= set('-'):        # '' / '-' / '--' / '---'
        return 0
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def roc_to_iso(s):
    """各來源的日期寫法 → ISO 'YYYY-MM-DD';無法解析回 None。

    實測要吃下的格式 (混在同一批 API 裡,不能只處理其中一種):
      '20260724'          證交所 payload['date'] (西元 8 碼,無分隔符)
      '115/07/24'         櫃買 tables[0]['date'] (民國,斜線)
      '115年07月24日 ...'  證交所 title (民國,中文)
      '2026/07/23'        期交所 CSV 日期欄 (西元,斜線)
    """
    if not s:
        return None
    txt = str(s).strip()
    # 先處理「連續 8 碼純數字」的西元日期 (證交所 date 欄):無分隔符,
    # 下面的通用 regex 需要 \D+ 當分隔,吃不到這種寫法。
    m8 = re.match(r'^(\d{4})(\d{2})(\d{2})$', txt)
    if m8:
        return f"{m8.group(1)}-{m8.group(2)}-{m8.group(3)}"
    m = re.search(r'(\d{2,4})\D+(\d{1,2})\D+(\d{1,2})', txt)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:            # 民國年才加 1911;已是西元年就原樣用
        y += 1911
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def is_warrant(code):
    """權證判斷:6 碼且以 03~08 開頭。

    T86 單日回傳約 13600 檔,其中九成是權證 (實測 1336/13639 為非權證)。
    權證籌碼對本專案沒有意義,且全存會讓一年份資料從 37MB 膨脹到 420MB,
    故在解析階段就濾掉。用「排除權證」而非「只留 4 碼數字」是刻意的:
    後者會漏掉特別股 (2883B) 與含字母的債券 ETF (00945B)。
    """
    c = str(code).strip().upper()
    return len(c) == 6 and c[:2] in ('03', '04', '05', '06', '07', '08')


# ---------------------------------------------------------------------------
# 1. 證交所 BFI82U:市場三大法人買賣金額 (加權指數層級)
# ---------------------------------------------------------------------------
def parse_twse_market_inst(payload, date_iso=None):
    """回傳單列 DataFrame:Date/Foreign/Trust/Dealer/InstTotal (單位:元)。

    payload 的 data 是 6 列 (自營商自行/自營商避險/投信/外資及陸資/
    外資自營商/合計),各列第 3 欄是買賣差額。這裡把它攤平成一列,
    方便跟個股籌碼用同一套欄位詞彙串接。
    """
    if not isinstance(payload, dict) or payload.get('stat') != 'OK':
        return pd.DataFrame(columns=[COL_DATE, COL_FOREIGN, COL_TRUST, COL_DEALER, COL_TOTAL])
    d = date_iso or roc_to_iso(payload.get('date') or payload.get('title'))
    acc = {'foreign': 0, 'trust': 0, 'dealer': 0}
    for row in payload.get('data') or []:
        if len(row) < 4:
            continue
        unit, net = str(row[0]), to_num(row[3])
        if '外資' in unit:
            acc['foreign'] += net          # 外資及陸資 + 外資自營商
        elif '投信' in unit:
            acc['trust'] += net
        elif '自營商' in unit:
            acc['dealer'] += net           # 自行買賣 + 避險
        # '合計' 不累加:改用三者相加,避免官方合計口徑變動時無聲不一致
    return pd.DataFrame([{
        COL_DATE: d,
        COL_FOREIGN: acc['foreign'],
        COL_TRUST: acc['trust'],
        COL_DEALER: acc['dealer'],
        COL_TOTAL: acc['foreign'] + acc['trust'] + acc['dealer'],
    }])


# ---------------------------------------------------------------------------
# 2. 證交所 T86:上市個股三大法人買賣超 (單位:股)
# ---------------------------------------------------------------------------
# T86 欄位索引 (實測 2026-07-24,共 19 欄):
#   [0]代號 [1]名稱 [4]外陸資買賣超(不含外資自營商) [7]外資自營商買賣超
#   [10]投信買賣超 [11]自營商買賣超 [18]三大法人買賣超
_T86 = dict(code=0, name=1, foreign_ex=4, foreign_dlr=7, trust=10, dealer=11, total=18)


def parse_twse_stock_inst(payload, date_iso=None, drop_warrants=True):
    """證交所 T86 → 個股籌碼 DataFrame (STOCK_CHIP_COLS)。"""
    if not isinstance(payload, dict) or payload.get('stat') != 'OK':
        return pd.DataFrame(columns=STOCK_CHIP_COLS)
    d = date_iso or roc_to_iso(payload.get('date') or payload.get('title'))
    out = []
    for r in payload.get('data') or []:
        if len(r) <= _T86['total']:
            continue
        code = str(r[_T86['code']]).strip()
        if drop_warrants and is_warrant(code):
            continue
        foreign = to_num(r[_T86['foreign_ex']]) + to_num(r[_T86['foreign_dlr']])
        trust = to_num(r[_T86['trust']])
        dealer = to_num(r[_T86['dealer']])
        out.append({
            COL_DATE: d, COL_CODE: code, COL_NAME: str(r[_T86['name']]).strip(),
            COL_FOREIGN: foreign, COL_TRUST: trust, COL_DEALER: dealer,
            COL_TOTAL: to_num(r[_T86['total']]),
        })
    return pd.DataFrame(out, columns=STOCK_CHIP_COLS)


# ---------------------------------------------------------------------------
# 3. 櫃買 dailyTrade:上櫃個股三大法人買賣超 (單位:股)
# ---------------------------------------------------------------------------
# 7 組 × 3 欄,每組的「買賣超」欄位索引 (組別語意見模組 docstring):
_TPEX_NET = [4, 7, 10, 13, 16, 19, 22]
_TPEX_FOREIGN_TOTAL = _TPEX_NET[2]   # 外資合計
_TPEX_TRUST = _TPEX_NET[3]           # 投信
_TPEX_DEALER_TOTAL = _TPEX_NET[6]    # 自營商合計
_TPEX_TOTAL = 23                     # 三大法人合計


def _tpex_table(payload):
    if not isinstance(payload, dict):
        return None
    tabs = payload.get('tables') or []
    return tabs[0] if tabs and isinstance(tabs[0], dict) else None


# verify_tpex_layout 的三種結果。刻意用字串而不是 True/False:
# 「沒有資料」與「版面壞了」的處理方式完全不同 (前者靜靜跳過,後者要大聲警告),
# 用 bool 只能表達兩種狀態,呼叫端就被迫把它們當成同一件事。
LAYOUT_OK = 'ok'
NO_DATA = 'no_data'
BAD_LAYOUT = 'bad'


def verify_tpex_layout(payload):
    """驗證櫃買欄位分組的三條恆等式是否仍成立。

    回傳 (狀態, 說明);狀態為 LAYOUT_OK / NO_DATA / BAD_LAYOUT。

    櫃買改版時欄位順序若變動,靠人眼很難發現 (數字照樣填得滿滿的,只是
    對應錯欄位)。這個檢查讓改版變成「看得見的失敗」而不是無聲的錯誤資料。
    """
    tb = _tpex_table(payload)
    rows = (tb or {}).get('data') or []
    if not rows:
        # 【ADR-119】「沒有資料」不等於「版面壞了」。回溯很久以前的日期時,
        # 櫃買端經常就是沒有那一天的資料 (端點涵蓋範圍有限、或該日無交易),
        # 這是預期中的情況。原本跟版面驗證失敗共用同一個 False,呼叫端就會
        # 對著每一個沒資料的日子印一次紅字警告 —— 使用者看到滿螢幕的
        # 「版面驗證失敗」,反而會忽略真正的版面問題 (實機回報)。
        return NO_DATA, "該日櫃買沒有資料 (通常是日期太早或當日無交易)"
    bad = []
    for r in rows:
        if len(r) <= _TPEX_TOTAL:
            return BAD_LAYOUT, f"欄位數不足 (應 >{_TPEX_TOTAL},實際 {len(r)})"
        g = [to_num(r[i]) for i in _TPEX_NET]
        if g[2] != g[0] + g[1]:
            bad.append((r[0], '外資合計≠外資及陸資+外資自營商'))
        elif g[6] != g[4] + g[5]:
            bad.append((r[0], '自營商合計≠自行+避險'))
        elif to_num(r[_TPEX_TOTAL]) != g[2] + g[3] + g[6]:
            bad.append((r[0], '三大法人合計≠外資+投信+自營'))
    if bad:
        return BAD_LAYOUT, f"{len(bad)}/{len(rows)} 列不符恆等式,首例: {bad[0]}"
    return LAYOUT_OK, f"{len(rows)} 列全部符合"


def parse_tpex_stock_inst(payload, date_iso=None, drop_warrants=True):
    """櫃買 dailyTrade → 個股籌碼 DataFrame (STOCK_CHIP_COLS)。"""
    tb = _tpex_table(payload)
    if not tb:
        return pd.DataFrame(columns=STOCK_CHIP_COLS)
    d = date_iso or roc_to_iso(tb.get('date') or payload.get('date'))
    out = []
    for r in tb.get('data') or []:
        if len(r) <= _TPEX_TOTAL:
            continue
        code = str(r[0]).strip()
        if drop_warrants and is_warrant(code):
            continue
        out.append({
            COL_DATE: d, COL_CODE: code, COL_NAME: str(r[1]).strip(),
            COL_FOREIGN: to_num(r[_TPEX_FOREIGN_TOTAL]),
            COL_TRUST: to_num(r[_TPEX_TRUST]),
            COL_DEALER: to_num(r[_TPEX_DEALER_TOTAL]),
            COL_TOTAL: to_num(r[_TPEX_TOTAL]),
        })
    return pd.DataFrame(out, columns=STOCK_CHIP_COLS)


# ---------------------------------------------------------------------------
# 4. 證交所 MI_MARGN:市場信用交易 (融資融券)
# ---------------------------------------------------------------------------
MARGIN_COLS = [COL_DATE, 'MarginBalance', 'MarginPrevBalance',
               'ShortBalance', 'ShortPrevBalance', 'MarginAmountBalance']


def parse_twse_margin(payload, date_iso=None):
    """證交所 MI_MARGN → 市場融資融券餘額 DataFrame。

    融資/融券「交易單位」= 張;融資金額單位為仟元。取「今日餘額」(第5欄)
    與「前日餘額」(第4欄),讓上層可以直接算增減。
    """
    if not isinstance(payload, dict) or payload.get('stat') != 'OK':
        return pd.DataFrame(columns=MARGIN_COLS)
    d = date_iso or roc_to_iso(payload.get('date'))
    rec = {c: 0 for c in MARGIN_COLS}
    rec[COL_DATE] = d
    for tb in payload.get('tables') or []:
        for r in (tb.get('data') or []):
            if len(r) < 6:
                continue
            item = str(r[0])
            prev, today = to_num(r[4]), to_num(r[5])
            if item.startswith('融資(交易單位)'):
                rec['MarginBalance'], rec['MarginPrevBalance'] = today, prev
            elif item.startswith('融券(交易單位)'):
                rec['ShortBalance'], rec['ShortPrevBalance'] = today, prev
            elif item.startswith('融資金額'):
                rec['MarginAmountBalance'] = today
    return pd.DataFrame([rec], columns=MARGIN_COLS)


# ---------------------------------------------------------------------------
# 5. 期交所 futContractsDateDown:期貨三大法人未平倉
# ---------------------------------------------------------------------------
FUT_CHIP_COLS = [COL_DATE, 'Product', 'Investor',
                 'NetOI', 'NetOIAmount', 'NetTrade', 'NetTradeAmount']

# 期交所 CSV 欄位 (實測 2026-07-24,15 欄):
#   [0]日期 [1]商品名稱 [2]身份別 [7]多空交易口數淨額 [8]多空交易契約金額淨額
#   [13]多空未平倉口數淨額 [14]多空未平倉契約金額淨額
_FUT = dict(date=0, product=1, investor=2, net_trade=7, net_trade_amt=8,
            net_oi=13, net_oi_amt=14)


def decode_taifex(raw):
    """期交所 CSV 位元組 → 文字。編碼可能是 Big5(cp950) 或 UTF-8(含BOM)。

    與 core/taifex_daily.py 的處理一致 (ADR-049 已踩過這個坑):逐一嘗試,
    最後用 cp950 + errors='replace' 保底,不讓單一怪字元讓整批解析失敗。
    """
    if isinstance(raw, str):
        return raw
    for enc in ('cp950', 'utf-8-sig', 'utf-8'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return raw.decode('cp950', errors='replace')


def parse_taifex_inst(raw, products=None):
    """期交所三大法人 CSV → DataFrame (FUT_CHIP_COLS)。

    products:只保留這些商品名稱 (例如 ('臺股期貨','小型臺指期貨'));
    None 表示全留。身份別為 自營商/投信/外資及陸資。
    """
    text = decode_taifex(raw)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return pd.DataFrame(columns=FUT_CHIP_COLS)
    out = []
    for r in rows[1:]:                       # 跳過標題列
        if len(r) <= _FUT['net_oi_amt']:
            continue
        d = roc_to_iso(r[_FUT['date']]) or str(r[_FUT['date']]).strip().replace('/', '-')
        product = str(r[_FUT['product']]).strip()
        if products and product not in products:
            continue
        investor = str(r[_FUT['investor']]).strip()
        if not investor:
            continue
        out.append({
            COL_DATE: d, 'Product': product, 'Investor': investor,
            'NetOI': to_num(r[_FUT['net_oi']]),
            'NetOIAmount': to_num(r[_FUT['net_oi_amt']]),
            'NetTrade': to_num(r[_FUT['net_trade']]),
            'NetTradeAmount': to_num(r[_FUT['net_trade_amt']]),
        })
    return pd.DataFrame(out, columns=FUT_CHIP_COLS)
