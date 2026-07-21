# -*- coding: utf-8 -*-
"""
core/fut_catalog.py — 台灣期貨商品目錄:代號樣式判斷 + 名稱正規化

【ADR-046】兩個實際問題催生本模組:

1. 週契約代號被誤判成台股:
   舊版 _looks_like_futures_symbol 要求「前 3 碼全英文字母」,但臺灣期交所
   小型臺指「週到期契約」的商品代號含數字 (MX1/MX2/MX4/MX5 = W1/W2/W4/W5
   週),完整代號如 MX4R1、MX1202607 —— 前 3 碼含數字 → 被判為台股 →
   合約查無 / 自選股報價 '--'。正確樣式:首碼必為英文字母,第 2~3 碼可為
   英數,尾碼為 R1/R2 或 6 位月份。

2. shioaji 合約檔的中文名稱污染 (使用者實例):
   MXFR1 的 name 顯示「小型臺指W2近月」、MXF202608 顯示「小型臺指W208」,
   與 TXFR1「臺股期貨近月」的正常格式不一致 —— 名稱被週契約系列污染。
   合約解析靠 symbol 不受影響,但顯示會嚴重誤導 (使用者以為載到週契約)。
   解法:對「確定無疑」的指數期貨家族,用本模組的官方名稱表重建顯示名稱;
   不在表內的商品 (股票期貨等) 一律原樣沿用 shioaji 名稱,不亂猜。

純邏輯、零 UI、零 shioaji 依賴,可離線測試。
"""
import re

# 完整期貨代號樣式:1 英文字母 + 2 英數 + (R1/R2 或 6 位月份數字)
_FUT_SYMBOL_RE = re.compile(r'^[A-Z][A-Z0-9]{2}(R[12]|\d{6})$')

# 確定無疑的指數期貨家族官方名稱 (依臺灣期交所商品名稱)。
# 刻意只收錄有把握的:名稱正規化「寧缺勿錯」,不在表內就沿用 shioaji 原名。
INDEX_FUT_NAMES = {
    'TXF': '臺股期貨',
    'MXF': '小型臺指期貨',
    'TMF': '微型臺指期貨',
    'EXF': '電子期貨',
    'FXF': '金融期貨',
    'ZEF': '小型電子期貨',
    'ZFF': '小型金融期貨',
    'MX1': '小型臺指W1週契約',
    'MX2': '小型臺指W2週契約',
    'MX4': '小型臺指W4週契約',
    'MX5': '小型臺指W5週契約',
}


def looks_like_futures_symbol(sym):
    """判斷代號長得像「期貨完整代號」:TXFR2、MXF202608、MX4R1、MX1202607。
    首碼必為英文字母 (排除台股數字代碼),第 2~3 碼容許數字 (週契約)。"""
    s = str(sym or '').strip().upper()
    return bool(_FUT_SYMBOL_RE.match(s))


def product_code(sym):
    """取完整代號的商品碼 (前 3 碼);不像期貨完整代號時,若整串為 2~3 碼
    英數且首碼為字母,視為商品碼本身,否則回傳空字串。"""
    s = str(sym or '').strip().upper()
    if looks_like_futures_symbol(s):
        return s[:3]
    if 2 <= len(s) <= 3 and s[0].isalpha() and s.isalnum():
        return s
    return ''


def display_name(symbol, sj_name=''):
    """
    期貨顯示名稱正規化:
      - 商品碼在 INDEX_FUT_NAMES 表內 → 用官方名稱 + 尾碼語意重建:
          R1 → 近月、R2 → 次月、YYYYMM → 取 MM (與 shioaji 慣例「臺股期貨08」一致)
      - 不在表內 (股票期貨/商品期貨等) → 原樣回傳 sj_name (不猜)。
    """
    s = str(symbol or '').strip().upper()
    code = product_code(s)
    base = INDEX_FUT_NAMES.get(code)
    if not base:
        return sj_name or s
    tail = s[3:] if len(s) > 3 else ''
    if tail == 'R1':
        return f"{base}近月"
    if tail == 'R2':
        return f"{base}次月"
    if len(tail) == 6 and tail.isdigit():
        return f"{base}{tail[4:6]}"
    return base
