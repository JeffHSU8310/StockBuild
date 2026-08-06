# -*- coding: utf-8 -*-
"""
core/quote_book.py — 五檔報價的正規化與取價 (ADR-147)

【這個模組要解決什麼】「12:01 用當時的五檔下一張零股」這件事,真正危險的
不是排程,是**取價**:五檔是唯一的價格來源(零股沒有 K 線,`snapshots()`
的價又永遠是整股價,見 P-06),一旦取錯就是一張真錢的錯價單,而且定時下單
只有一次機會,沒有「下一根 K 棒再修正」這種事。

所以取價規則獨立成純函式放在 core:零 tkinter、零 shioaji (鐵則 11),
GUI 只負責把券商推來的物件攤平成下面這個 dict,規則本身離線就測得完。

【book 的形狀】
    {'bid': [(價, 量), ...],   # 由高到低 (買一 > 買二 > …)
     'ask': [(價, 量), ...],   # 由低到高 (賣一 < 賣二 < …)
     'ts': 這份五檔的時間戳 (epoch 秒),
     'symbol': 代碼, 'odd': 是不是零股}

【刻意不做的事】不補值、不推估、不拿別的價來頂。檔位缺了就是缺了,
回傳 None 讓呼叫端**不要下單** —— 鐵則 4 的精神:沒有真實資料的檔位
絕不捏造。捏一個價出來的後果是「使用者以為照五檔成交,其實是我們編的」。
"""

MAX_LEVELS = 5
DEFAULT_LEVEL = 1

# 五檔多久沒更新就當它不能用。盤中零股每筆撮合都會推,10 秒沒動通常代表
# 串流斷了或這檔根本沒人掛單 —— 兩種情況都不該拿它去下單。
DEFAULT_MAX_AGE_SEC = 10.0

BUY = '買進'
SELL = '賣出'


def make(bid_prices, bid_volumes, ask_prices, ask_volumes,
         ts=None, symbol='', odd=False):
    """把四條平行陣列攤平成標準 book。長度不齊時取較短的那一邊。

    價 <= 0 的檔位直接截斷 —— 券商在檔位不足時會補 0,那些 0 不是「價格
    是零」,是「沒有這一檔」。把它們留著,`pick_price()` 就會挑到 0.0,
    變成一張 0 元的委託。
    """
    def _pairs(prices, volumes):
        out = []
        for i in range(min(len(prices or []), len(volumes or []), MAX_LEVELS)):
            try:
                p = float(prices[i])
                v = int(float(volumes[i]))
            except (TypeError, ValueError):
                break
            if p <= 0:
                break
            out.append((p, v))
        return out

    return {
        'bid': _pairs(bid_prices, bid_volumes),
        'ask': _pairs(ask_prices, ask_volumes),
        'ts': float(ts) if ts is not None else None,
        'symbol': str(symbol or ''),
        'odd': bool(odd),
    }


def depth(book, side):
    """某一邊真正有幾檔可用。"""
    return len((book or {}).get(side) or [])


def is_fresh(book, now_ts, max_age_sec=DEFAULT_MAX_AGE_SEC):
    """→ (新鮮嗎, 原因)。沒有時間戳一律當作不新鮮。

    「沒有時間戳就放行」是很誘人的寫法(反正大部分時候都有),但它讓
    「串流從來沒推過、book 是初始化出來的空殼」跟「剛剛才更新」變成同一件事。
    """
    if not book:
        return False, "沒有五檔資料"
    ts = book.get('ts')
    if ts is None:
        return False, "五檔沒有時間戳(還沒收到任何一筆推送)"
    age = float(now_ts) - float(ts)
    if age < 0:
        # 時鐘倒退或時間戳來自另一個時區 —— 寧可不下單也不要用一份看不懂的資料
        return False, f"五檔時間戳在未來 ({-age:.1f} 秒),不採用"
    if age > float(max_age_sec):
        return False, f"五檔已經 {age:.1f} 秒沒更新 (上限 {max_age_sec:g} 秒)"
    return True, ""


def clamp_level(level):
    """把使用者填的檔數收進 1~5。0 或負數當 1,超過 5 當 5。"""
    try:
        n = int(level)
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return max(1, min(MAX_LEVELS, n))


def pick_price(book, action, level=DEFAULT_LEVEL):
    """挑出「要成交就得付的那個價」。→ (價格 or None, 原因)。

    買進吃**賣方**掛單、賣出吃**買方**掛單 —— 反過來取(買進去看買價)
    等於掛一個排在自己後面的價,單會掛著不動,而使用者以為已經送出去成交了。

    `level` 是「我願意追到第幾檔」:
      買進 level=3 → 用**賣三**的價 (賣價由低到高,所以越深越貴 = 越積極)
      賣出 level=3 → 用**買三**的價 (買價由高到低,所以越深越便宜 = 越積極)

    這跟 `slippage_ticks` 的讓價是**兩個不同的旋鈕**,可以疊加:
    先用這裡決定「基準價在五檔的哪一檔」,再由 `order_intent.apply_slippage()`
    在那個基準上讓 N 檔。前者是「吃到第幾層掛單」,後者是「再多給一點價差」。
    """
    if not book:
        return None, "沒有五檔資料"
    side = 'ask' if action == BUY else 'bid'
    rows = list((book.get(side) or []))
    if not rows:
        label = '賣方' if side == 'ask' else '買方'
        return None, f"{label}五檔是空的 (這檔目前沒有掛單,或串流還沒推過)"
    n = clamp_level(level)
    if n > len(rows):
        return None, (f"要用第 {n} 檔,但目前只有 {len(rows)} 檔"
                      f"({'賣' if side == 'ask' else '買'}方掛單不夠深)")
    px = rows[n - 1][0]
    if px <= 0:
        return None, f"第 {n} 檔的價格是 {px},不是合法報價"

    # 一致性守門:挑出來的價一定要「不優於」最佳檔。
    #
    # 正常的 book 一定滿足這件事 (賣價遞增、買價遞減),所以這道檢查平常
    # 不會擋到任何東西;它擋的是**資料本身亂掉**的情況 —— 陣列沒對齊、
    # 買賣兩邊接反、券商補了奇怪的值。那時候若照樣取值,會取到一個離譜的
    # 價格然後真的送出去。與其猜哪裡壞了,不如不下單。
    best = rows[0][0]
    if side == 'ask' and px < best:
        return None, f"五檔資料不一致:賣{n} ({px:g}) 比賣一 ({best:g}) 還低"
    if side == 'bid' and px > best:
        return None, f"五檔資料不一致:買{n} ({px:g}) 比買一 ({best:g}) 還高"
    return px, ""


def describe(book):
    """一行人話,給系統日誌用。取價失敗時要看得出來當下的五檔長什麼樣。"""
    if not book:
        return "(無五檔)"
    b = (book.get('bid') or [])
    a = (book.get('ask') or [])
    b_txt = " ".join(f"{p:g}x{v}" for p, v in b[:MAX_LEVELS]) or "--"
    a_txt = " ".join(f"{p:g}x{v}" for p, v in a[:MAX_LEVELS]) or "--"
    return f"買[{b_txt}] 賣[{a_txt}]"
