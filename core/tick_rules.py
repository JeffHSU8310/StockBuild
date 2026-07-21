"""
core.tick_rules — 台股/期貨跳動單位 (tick) 規則與價格格式化。

這個模組刻意不依賴 tkinter、shioaji 或任何網路/檔案 I/O，
只吃基本型別、回傳基本型別，方便離線單元測試 (見 tests/test_core.py)。

原本這兩個函式是 StockTradingAppPro 的方法 (self.get_tick / self.fmt_price)，
讀取 self.asset_type 與 self.current_symbol；抽出後改為顯式參數傳入，
呼叫端 (GUI 層) 負責從 self 讀值後呼叫這裡。
"""


def get_tick(price: float, asset_type: str, raw_symbol: str) -> float:
    """
    回傳台股/期貨在該價格下的合法跳動單位。

    - 期貨：固定 1.0 點。
    - 股票/指數：ETF (代碼開頭 00) 與一般股票的價格帶跳動表不同，
      交界處 (10/50/100/500/1000) 容易出現非法價位，所有需要跳動單位的地方
      都必須呼叫這個函式，不要各處各自寫格式化邏輯 (CLAUDE.md 鐵則7)。
    """
    if asset_type == "future":
        return 1.0
    elif asset_type in ("stock", "index_tw"):
        p = float(price)
        sym = raw_symbol.replace('.TW', '').replace('.TWO', '')
        if sym.startswith('00'):
            if p < 50:
                return 0.01
            else:
                return 0.05
        else:
            if p < 10:
                return 0.01
            elif p < 50:
                return 0.05
            elif p < 100:
                return 0.1
            elif p < 500:
                return 0.5
            elif p < 1000:
                return 1.0
            else:
                return 5.0
    return 0.01


def fmt_price(price, asset_type: str, raw_symbol: str) -> str:
    """
    依 get_tick() 決定的跳動單位，回傳正確小數位數的價格字串。
    price 若無法轉為 float (例如 None 或空字串)，回傳 "--"。
    """
    try:
        p = float(price)
        t = get_tick(p, asset_type, raw_symbol)
        if t >= 1:
            return f"{p:.0f}"
        if t in (0.5, 0.1):
            return f"{p:.1f}"
        return f"{p:.2f}"
    except Exception:
        return "--"


def round_to_tick(price: float, asset_type: str, raw_symbol: str) -> float:
    """
    【使用者調整】使用者手動輸入的價格如果沒有對齊合法的 tick 跳動單位
    (例如台股 100 元以上 tick 是 0.5，使用者卻打了 105.3)，這裡把價格
    四捨五入到最接近的合法 tick 位置。

    邊界情況處理：價格四捨五入之後可能會跨到下一個價格帶 (例如 tick 規則
    在 100 元有斷點，99.8 用 100 元以下的 tick=0.1 去算可能捨入成 99.8→
    沒問題；但如果原始價格已經很接近價格帶交界，捨入結果可能落在
    「用原本那個 tick 算出來的值」跟「用新價格帶的 tick 重新算一次」
    有些微差異)——這裡採用簡單、保守的做法：先用原始價格對應的 tick
    四捨五入一次；如果捨入後的價格落入了不同的價格帶，再用新價格帶的
    tick 重新對齊一次，確保最終結果一定是合法的 tick 價位。
    """
    try:
        p = float(price)
    except (TypeError, ValueError):
        return price

    tick = get_tick(p, asset_type, raw_symbol)
    if tick <= 0:
        return p
    rounded = round(round(p / tick) * tick, 4)

    # 捨入後可能跨到不同的價格帶 (tick 大小改變)，用新價格帶的 tick 再對齊一次
    tick2 = get_tick(rounded, asset_type, raw_symbol)
    if tick2 != tick:
        rounded = round(round(rounded / tick2) * tick2, 4)

    return rounded
