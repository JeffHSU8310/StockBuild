# -*- coding: utf-8 -*-
"""
core/unit_format.py — 台股慣用單位換算與顯示 (ADR-118)

【為什麼要有這一層】官方籌碼資料的原始單位跟台灣投資人平常講的不一樣:
個股法人買賣超是「股」、大盤法人是「元」、融資金額是「仟元」。原樣顯示會
變成 `102,513,033` 這種一長串數字 —— 看得懂,但要在腦中除以 1000 或 1 億才
知道是多少,而且欄位之間單位還不一致,一眼比不出大小。

台灣習慣的講法:張(1000股)、億元。這個模組負責換算與格式化。

【設計原則】
1. **純函式,零依賴** —— 顯示邏輯錯了不會有錯誤訊息,只會讓使用者看到錯的
   數字然後據此下判斷,所以一定要能離線測試 (鐵則 11)。
2. **換不了就誠實顯示 `--`**,不要回 0 —— 「沒有資料」跟「數值是 0」在籌碼
   上是完全不同的意思 (同 core/fundamental_parser.to_float 的理由)。
3. **保留正負號**:買超/賣超、增/減都靠符號分辨,而畫面上還要靠它決定
   紅漲綠跌 (鐵則 1)。
"""

SHARES_PER_LOT = 1000        # 台股 1 張 = 1000 股
YI = 100_000_000             # 1 億
QIAN = 1_000                 # 1 仟

MISSING = '--'


def _num(v):
    """轉成 float;轉不了 (含 None / NaN / '--' / 空字串) 回 None。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.replace(',', '').strip()
        if not s or s in ('--', '-') or s.lower() == 'nan':
            return None
        try:
            return float(s)
        except ValueError:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:               # NaN != NaN
        return None
    return f


def fmt_lots(shares, nd=0):
    """股 → 張。個股法人買賣超用。

    台股買賣超原始資料的單位是「股」,但沒有人用股在講量 —— 講的是張。
    """
    v = _num(shares)
    if v is None:
        return MISSING
    return f"{v / SHARES_PER_LOT:,.{nd}f}"


def fmt_yi(value, from_unit='元', nd=2):
    """金額 → 億元。

    from_unit:
      '元'   大盤法人買賣超金額 (官方原始單位)
      '仟元' 融資金額 (官方原始單位就是仟元)
    """
    v = _num(value)
    if v is None:
        return MISSING
    scale = {'元': YI, '仟元': YI / QIAN}.get(from_unit)
    if scale is None:
        raise ValueError(f"不支援的來源單位: {from_unit}")
    return f"{v / scale:,.{nd}f}"


def fmt_int(value):
    """整數加千分位。單位本來就對的欄位 (口數、張數) 用這個。"""
    v = _num(value)
    if v is None:
        return MISSING
    return f"{v:,.0f}"


def fmt_signed_yi(value, from_unit='元', nd=2):
    """帶正負號的億元 —— 增減類欄位用,一眼看得出方向。"""
    v = _num(value)
    if v is None:
        return MISSING
    scale = {'元': YI, '仟元': YI / QIAN}[from_unit]
    return f"{v / scale:+,.{nd}f}"


def diff_series(values):
    """相鄰兩期的差 (今日 − 前一日)。第一期沒有前值,回 None。

    輸入必須是**時間由舊到新**排好的序列 —— 順序顛倒會讓增減的正負號整個
    相反,而使用者是靠正負號判斷「融資在增加還是減少」的,方向錯了結論就錯。
    """
    out = []
    prev = None
    for v in values:
        cur = _num(v)
        out.append(None if (prev is None or cur is None) else cur - prev)
        prev = cur
    return out


def is_positive(text):
    """格式化後的字串代表正數嗎 (給紅漲綠跌上色用)。看不出來回 None。"""
    s = str(text or '').strip()
    if not s or s == MISSING:
        return None
    if s.startswith('+'):
        return True
    if s.startswith('-'):
        return False
    v = _num(s)
    return None if v is None else v > 0
