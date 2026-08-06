"""
core.order_rules — 台股委託送出前的本地規則驗證 (見 DECISIONS.md ADR-008/ADR-013)。

純函式，不做任何 I/O、不呼叫 self.log_message，回傳 (ok, reason)。
GUI 層 (execute_order) 呼叫這裡拿到結果後，自行決定要不要印日誌、要不要
真的呼叫 shioaji 下單。這樣規則本身 (什麼組合合法/不合法) 可以被單元測試
直接驗證，不需要起一個 tkinter 視窗或 mock shioaji。
"""

MODE_LABELS = {"Common": "整股", "IntradayOdd": "盤中零股", "Fixing": "盤後定價", "Odd": "盤後零股"}

# 【ADR-013】單筆委託數量上限:整股/盤後定價最多 499 張、零股類最多 999 股。
# 這是本系統自訂的保守上限 (防止打錯數字誤送巨量委託)，不是交易所規則本身；
# 零股的 1~999 股上限則確實是交易所規則 (股數超過 999 就該用整股下單)。
MAX_QTY_LOT = 499     # 整股/盤後定價,單位:張
MAX_QTY_ODD = 999     # 盤中零股/盤後零股,單位:股

# 【ADR-146】自動交易策略的單筆數量上限。手動下單與策略下單的上限**刻意不同**:
# 手動是人盯著按的,策略是無人看管、可能一天觸發好幾次,所以整股/期貨壓得更保守。
# 零股那條是**交易所規則**(超過 999 股就該用整股下單),兩邊必須一致 ——
# 這正是原本出錯的地方:策略路徑拿「張/口」的防呆上限 100 去卡「股」,
# 於是零股策略最多只能買 100 股,而 500 股是完全合法的。
MAX_STRATEGY_QTY_LOT = 100    # 策略的整股/期貨,單位:張/口 (本系統防呆)
MAX_STRATEGY_QTY_ODD = MAX_QTY_ODD   # 策略的零股,單位:股 (交易所規則,與手動同一份)


def strategy_qty_limit(trade_type):
    """【ADR-146】策略單筆數量上限 → (上限, 單位, 是不是交易所規則)。

    回傳第三個值是要讓錯誤訊息能講對話:交易所規則不可以叫使用者「去改程式碼
    上限」(原本的訊息就是這樣寫的,會把人引導去改一個不該改的常數)。
    """
    if str(trade_type) == '零股':
        return MAX_STRATEGY_QTY_ODD, '股', True
    return MAX_STRATEGY_QTY_LOT, ('口' if str(trade_type) == '期貨' else '張'), False


def validate_stock_order(mode: str, order_type_str: str, order_cond: str, order_type_tif: str, qty_str: str):
    """
    驗證台股委託是否符合交易所規則與本系統的數量上限。回傳 (ok: bool, reason: str)。
    ok=True 時 reason 為空字串；ok=False 時 reason 是可以直接顯示給使用者的說明。

    規則來源 (ADR-005/ADR-008/ADR-013 查證與決定)：
      - 零股類 (IntradayOdd/Odd)：僅能限價、僅能現股、僅能 ROD、數量 1~999 股 (交易所規則)。
      - 盤後定價 (Fixing)：僅能限價 (價格鎖定收盤價)、僅能 ROD。
      - 整股/盤後定價：數量上限 499 張 (本系統自訂的保守防呆上限)。
    """
    is_lot_restricted = mode in ("IntradayOdd", "Odd")
    label = MODE_LABELS.get(mode, mode)

    # 數量驗證:零股類與整股類都要做,上限不同 (零股是交易所規則,整股是本系統防呆上限)。
    try:
        q = int(qty_str)
    except ValueError:
        unit = "股" if is_lot_restricted else "張"
        return False, f"數量請輸入有效整數 (單位:{unit})。"

    if is_lot_restricted:
        if not (1 <= q <= MAX_QTY_ODD):
            return False, f"{label}數量須為 1~{MAX_QTY_ODD} 股。"
    else:
        if not (1 <= q <= MAX_QTY_LOT):
            return False, f"{label}單筆委託數量須為 1~{MAX_QTY_LOT} 張。"

    if is_lot_restricted:
        if order_type_str == "市價":
            return False, f"{label}僅接受「限價」委託 (交易所規則),請切換為限價。"
        if order_cond != "Cash":
            return False, f"{label}僅能現股交易,不可融資融券 (交易所規則)。"
        if order_type_tif != "ROD":
            return False, f"{label}僅能使用 ROD (交易所規則)。"

    if mode == "Fixing":
        if order_type_str == "市價":
            return False, "盤後定價沒有市價委託 (成交價固定為當日收盤價)。"
        if order_type_tif != "ROD":
            return False, "盤後定價僅能使用 ROD。"

    return True, ""


def is_daytrade_eligible(mode: str, order_cond: str, action: str, current_day_trade: bool) -> bool:
    """
    現股當沖 (先賣後買) 只在「整股 + 現股 + 賣出 + 該股可現沖」同時成立時才有意義。
    其餘情況即使使用者勾選了當沖框，也不應該真的附加 daytrade_short=True 送出。
    """
    return mode == "Common" and order_cond == "Cash" and action == "賣出" and bool(current_day_trade)


# ============================================================================
# 【ADR-023】委託刪改 (刪單 / 改量 / 改價) 的規則驗證。
#
# 交易所 + 本系統規則 (刪改)：
#   - 整股 (Common)：可刪單、改量 (只能減少)、改價。
#   - 零股 (IntradayOdd / Odd)：可刪單、改量 (只能減少)，不可改價。
#   - 盤後定價 (Fixing)：價格鎖定收盤價,不可改價;可刪單、改量 (只能減少)。
#   - 只有「有未成交數量」的委託可以操作;已全部成交或已取消的不能刪改。
#   - 改量是「減少未成交部分」:新總量必須 < 目前委託量,且 >= 已成交量、>= 1。
#   - 改價必須為正、與原價不同,且對齊 tick (tick 對齊由 GUI 端 round_to_tick 處理)。
#
# 一樣是純函式、回傳 (ok, reason)，方便離線單元測試。
# ============================================================================

def _safe_int(v, default=0):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def price_change_allowed(order_lot: str) -> bool:
    """只有整股 (Common) 可以改價;零股與盤後定價都不行。"""
    return order_lot == "Common"


def order_is_modifiable(status_display, current_qty, filled_qty):
    """
    這筆委託目前是否還能刪改。回傳 (ok, reason)。
    條件:未被取消、且還有未成交數量 (current - filled > 0)。
    """
    if "取消" in str(status_display):
        return False, "此委託已取消,無法再操作。"
    outstanding = _safe_int(current_qty) - _safe_int(filled_qty)
    if outstanding <= 0:
        return False, "此委託已全部成交,沒有未成交數量可刪改。"
    return True, ""


def validate_cancel(status_display, current_qty, filled_qty):
    """刪單驗證:等同於「這筆是否還能操作」。回傳 (ok, reason)。"""
    return order_is_modifiable(status_display, current_qty, filled_qty)


def validate_qty_change(order_lot, status_display, current_qty, filled_qty, new_qty):
    """
    改量驗證 (只能減少)。回傳 (ok, reason)。
    new_qty 是「新的委託總量」,必須:為有效整數、< 目前委託量 (只能減)、
    >= 已成交量 (不能減到比已成交還少)、>= 1。
    """
    ok, reason = order_is_modifiable(status_display, current_qty, filled_qty)
    if not ok:
        return ok, reason
    cur = _safe_int(current_qty)
    filled = _safe_int(filled_qty)
    unit = "股" if order_lot in ("IntradayOdd", "Odd") else "張"
    try:
        n = int(str(new_qty).strip())
    except (TypeError, ValueError):
        return False, f"新數量請輸入有效整數 (單位:{unit})。"
    if n >= cur:
        return False, f"改量只能減少:新數量須小於目前委託量 {cur}{unit}。"
    if n < 1:
        return False, f"新數量至少 1{unit};若要全部取消請用「刪單」。"
    if n < filled:
        return False, f"新數量不可小於已成交量 {filled}{unit}。"
    return True, ""


def validate_price_change(order_lot, current_price, new_price):
    """
    改價驗證。回傳 (ok, reason)。tick 對齊由 GUI 端先用 round_to_tick 處理,
    這裡只驗:此類型可否改價、新價為正、且與原價不同。
    """
    if not price_change_allowed(order_lot):
        label = MODE_LABELS.get(order_lot, order_lot)
        return False, f"{label}不可改價 (零股不可改價;盤後定價鎖定收盤價)。"
    try:
        np_ = float(new_price)
    except (TypeError, ValueError):
        return False, "新價格請輸入有效數字。"
    if np_ <= 0:
        return False, "新價格必須大於 0。"
    try:
        cp = float(current_price)
    except (TypeError, ValueError):
        cp = None
    if cp is not None and abs(np_ - cp) < 1e-9:
        return False, "新價格與原委託價相同,不需改價。"
    return True, ""
