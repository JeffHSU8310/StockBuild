
## ADR-111：凱基證券 adapter（ADR-110 階段 3 的第一家新券商）

### 為什麼先做凱基而不是兆豐

查證兩家的 API 型態後，凱基是唯一能在動工前就把介面確定下來的：

| | 凱基 SUPER PY | 兆豐 Speedy API |
|---|---|---|
| 取得方式 | PyPI 公開套件 `kgisuperpy` | 下載安裝檔（分 32/64 位元） |
| 型態 | Python 套件，原始碼可讀 | DLL/COM 風格，`Logon(IP, Port, ID, Password, …)` |
| 動工前能否確定簽名 | ✅ 可以（見下） | ❌ 只能看網頁教學片段 |

群益則照 ADR-110 的判斷先跳過（SDK 型態仍未查證）。

### 查證方式：讀原始碼，不是照文件猜

`pip download kgisuperpy --no-deps` 取得 2.0.8 的 wheel 後解開，直接讀原始碼
確認每一個簽名與列舉值：

| 事實 | 出處 |
|---|---|
| `create_order(action, symbol, qty, price, time_in_force, order_cond, odd_lot, name)` | `trading/Order.py:59` |
| 期貨版 `create_order(action, symbol, qty, price, time_in_force)`（**沒有** order_cond/odd_lot） | `trading/FutOrder.py` |
| `Action.Buy='B'` / `Sell='S'`；`TimeInForce.ROD=0` | `trading/_trade_base.py:15,19` |
| `PriceType.MKT='1'`、`RangeMarket='4'`、`LMT=0` | `trading/_trade_base.py:24` |
| `OddLot.Odd=4`（盤中零股）、`Odd_AfterMarket=1`（盤後零股） | `trading/_trade_base.py:46` |
| `_set_Account()` 會**重新指派** `api.Order` | `main.py:123` |
| `_acc = {account: broker_id}`、`account_flag` 為 證券/期貨/複委託 | `CA.py:100-109` |

`tests/test_brokers.py` 的假 SDK 就是照這張表複刻的，檔頭逐項標了出處——
凱基改版時可以一項一項重新核對。**真實連線的回報時序仍然只能實機驗證**，
本 ADR 不宣稱驗證過那一段。

### 與永豐的三個關鍵差異（adapter 存在的理由）

#### 1. 凱基一次只綁定一個帳號 → 必須上鎖

shioaji 是每張單各自帶 `account=`；凱基是 `api._set_Account(acc)` **重新指派**
`api.Order`，之後的 `create_order` 都走那個帳號。

這代表「綁定 → 送單」必須是不可分割的一段。否則：A 策略綁好帳號、還沒送單，
B 策略把帳號改掉 → **A 的單記到 B 的帳戶**，而且沒有任何錯誤訊息。多策略
同時觸發時這是會真的發生的競態，不是理論風險。

`place_order()` 因此用 `threading.RLock` 把綁定與送單包在一起。

**這條有測試守著，而且驗證過它抓得到**：`test_concurrent_orders_never_cross_accounts`
起兩條執行緒對不同帳號連續下單，拿掉鎖就會 FAIL（實測訊息：
「委託 2330 被送到帳戶 7654321」）。

> 補記一個過程中的錯誤：這個測試**第一版是假的**。延遲原本放在「綁定之前」，
> 但真正的競態窗口在「綁定完成 → 呼叫端讀 `api.Order`」之間，所以拿掉鎖
> 也照樣全綠。改成綁定**之後**才延遲才真的抓得到。凡是宣稱「這個測試守著
> 併發」的，都應該先拿掉保護跑一次確認它會紅。

#### 2. 凱基沒有「預設帳號」可用

`api.Order` 在 `_set_Account()` 之前根本不存在。所以策略沒指定帳號時，
adapter 只能**明確拒單**，不像永豐可以沿用 SDK 預設。

這也是為什麼 ADR-110 把「沒指定 = `None`」和「指定了但找不到」分成兩種語意
——前者對永豐是合法的，對凱基不是。

#### 3. 證券與期貨是兩套物件、兩個帳號

`api.Order` / `api.FutOrder`，帳號也分開綁。因此：
- `list_accounts()` 的顯示名稱帶上「(證券)」「(期貨)」，讓使用者在下拉選單
  就看得出類別
- 用證券戶下期貨單（或反過來）在**本地擋下**，不送去給券商退單（鐵則 9 的精神）

### Python 3.14 的阻擋點（實測結論）

`kgisuperpy` 的 wheel 標示是 `py3-none-any`（看起來與版本無關），METADATA 也
**沒有 `Requires-Python`**，所以 pip 不會擋。但實際上裝不起來也跑不動：

1. 套件內含預編譯的 C 擴充，只有 **cp39 / cp310 / cp311 / cp312 / cp313**
   （`kgisuperpy/msmp/*.so`、`*.pyd`），**沒有 cp314**。
2. 更關鍵的是 `main.py` 載入時會 `from .backtest.BT import backtest`，
   而 `backtest/_BT_helper.py:1` 直接 `import numba`。**numba 沒有 3.14 版**，
   所以 `import kgisuperpy` 在 3.14 會在 import 階段就失敗。

也就是說阻擋點是**傳遞依賴**，不只是凱基自己的程式碼。

處理方式：`brokers/kgi.py` 的 import 包在 `try/except Exception`（不只
`ImportError`——載入失敗的型態不只一種），`HAS_KGI=False` 時主程式照常運作，
只是券商註冊表裡不會有凱基，策略編輯器的下拉自然也不會出現凱基帳號——
使用者不會選到一個根本不能用的目標。

**要真的用凱基，必須先解決 Python 版本**，兩條路：
- 專案降到 Python 3.13（最省事，但要確認 shioaji 與其餘相依套件都支援）
- 凱基跑在獨立的 3.13 行程，主程式用 IPC 溝通（隔離性好，但要多寫一層）

這個選擇需要使用者決定，本 ADR 不替他選。

### 涉及檔案

| 檔案 | 內容 |
|---|---|
| `brokers/kgi.py`（新） | KGIBroker：登入/登出、帳號列舉與類別判斷、委託翻譯、帳號綁定上鎖 |
| `tests/test_brokers.py`（新） | 19 個離線測試，用照原始碼複刻的假 SDK |
| `stock_app_pro.py` | `HAS_KGI` 時註冊 `brokers['kgi']`（其餘 GUI 完全不用改） |

**GUI 一行都沒改**（除了註冊那三行）——策略編輯器的「實單帳戶」下拉、
策略清單的下單目標、確認視窗的顯示，全部是 ADR-110 階段 2 就做好的，
新券商接上就自動出現。這是階段 1/2 抽象化的實際回報。

### 已驗證（headless）

- `tests/test_brokers.py` **19 個全過**
- **突變測試**：拿掉帳號綁定的鎖 → 併發測試 FAIL（確認不是空測試）
- `tests/test_core.py` 505 全過、`diag_repro_issues.py` 38 案例全過、
  `diag_crossref.py` 乾淨
- `HAS_KGI=False`（本環境沒裝）時主程式照常編譯與執行

### 明確**沒有**驗證的部分（誠實聲明）

- 真實登入、憑證（凱基要求電子簽章）、真實下單與回報時序
- `_set_Account` 在真實連線下的耗時（本 adapter 假設「同帳號不重綁」是有價值的
  最佳化，若實際上很快則此最佳化多餘，但不影響正確性）
- 期貨商品代碼格式（文件寫 `"TXFG5"`，與本系統慣用的 `TXFR1` 不同——
  **這一項很可能需要一層代碼轉換**，等實機確認後再補）

### 需使用者實機驗證

**前置**
1. 決定 Python 版本方案（降 3.13 或獨立行程），並完成凱基的 API 申請與憑證。
2. `pip install kgisuperpy` 後啟動，確認系統日誌沒有異常、主程式正常開啟。

**功能**
3. 登入凱基後開策略編輯器，確認「實單帳戶」下拉出現凱基的帳號，且括號內
   正確標示（證券）／（期貨）。
4. 建一個**模擬**策略指定凱基帳號先跑一段，確認不會誤送真單。
5. 改實單、**零股 1 股**送一張真單，確認：委託有到凱基、成交價與數量正確。
6. 故意把期貨策略指定成證券戶，確認**在送出前就被擋下**並顯示清楚的原因。
7. 確認 `TXFG5` 這類期貨代碼要不要轉換——若送不出去，回報實際的錯誤訊息，
   我再補代碼轉換層。

**最重要的一條**
8. 同時啟用「一個走永豐、一個走凱基」的兩個策略，確認各自的單真的分別
   進到對應券商的帳戶——這是整個多券商功能的驗收點。

---

## 追記（2026-07-28）：對 kgisuperpy 2.1.0 重新核對

使用者指出 PyPI 上的 `kgisuperpy`。查證後發現當天（2026-07-28）剛發布
**2.1.0**，比本 ADR 當初分析的 2.0.8 新，因此重新核對一次。

### 結論：adapter 與假 SDK 對 2.1.0 仍然有效

用 AST 逐項比對（不看行號，避免行號位移造成誤判），本 adapter 依賴的每一項
在兩版之間**完全相同**：

| 項目 | 2.0.8 | 2.1.0 |
|---|---|---|
| `Action.Buy` / `.Sell` | `'B'` / `'S'` | 相同 |
| `TimeInForce.ROD` | `0` | 相同 |
| `PriceType.MKT` / `RangeMarket` / `LMT` | `'1'` / `'4'` / `0` | 相同 |
| `OddLot.Common` / `Odd` / `Odd_AfterMarket` | `0` / `4` / `1` | 相同 |
| `OrderCond.CASH` | `0` | 相同 |
| 證券 `create_order(action, symbol, qty, price, time_in_force, order_cond, odd_lot, name)` | — | 相同 |
| 期貨 `create_order(action, symbol, qty, price, time_in_force)` | — | 相同 |
| `_set_Account(account)` / `_set_FutAccount(account)` | — | 相同 |
| `_show_account()`、`_acc[account] = broker_id`、`account_flag` 的 F/O/S → 期貨/複委託/證券 | — | 相同 |
| `login(person_id, person_pwd, simulation)` | — | 相同 |

沒有新增或消失的列舉類別。

### 2.1.0 改了什麼

改動集中在**報價**與**選擇權**，與下單路徑無關：
- 新增 `Quote_sw.py` 與 `marketdata/quote_starwave.*`（新的報價來源）
- 某個 dataclass 增加 `cp` / `com_ym` / `strike_price` 欄位（選擇權相關）
- 一個 `__repr__` 從註解狀態改為啟用

順帶一提，`quote_starwave` 只提供 `win_amd64` 的 `.pyd`，沒有 Linux 版 ——
不過本專案的報價一律走永豐（ADR-011），用不到這部分。

### Python 3.14 的阻擋點**沒有**解除

這是最重要的一項：2.1.0 仍然
- 自帶的 C 擴充只編到 **cp39～cp313**（`msmp/*.so`、`*.pyd`），**沒有 cp314**
- METADATA 仍**沒有** `Requires-Python`（所以 pip 照樣不會擋）
- 仍 `Requires-Dist: numba`，且 `backtest/_BT_helper.py:1` 仍在載入時 `import numba`

因此 **ADR-112 的獨立 3.13 子行程方案仍然必要**，不是可以省掉的一層。
