# ADR-139:永豐 API 測試功能(模擬環境的登入測試 + 證券/期貨下單測試)

> 狀態:已實作、離線驗證通過、**尚未經使用者實機驗證**
> 規格出處:<https://sinotrade.github.io/zh/tutor/prepare/terms/>
> 相關:ADR-013(鐵則 14)/ ADR-097(brokers 分層)/ ADR-011
> 新增坑:P-117

---

## 起因

使用者給了永豐官方文件的網址,要求「額外幫我寫一個證券&期貨下單測試功能」。

永豐的規則是:簽署 API 條款之後,**必須在模擬環境跑過「登入測試」與「下單
測試」,而且證券、期貨兩個帳戶要分別測**,通過並經審核之後,正式環境的
下單權限才會開通。沒跑過這個流程,實盤下單就是不會動。

---

## 官方規格(逐條抄下來,不是推測的)

| 項目 | 規格 |
|---|---|
| 環境 | `sj.Shioaji(simulation=True)` |
| 必測項目 | ① 登入 `login` ② 下單 `place_order`(證券、期貨各一) |
| 服務時間 | 星期一至五 **08:00 ~ 20:00** |
| 地域限制 | 08:00–18:00 無限制;**18:00–20:00 僅允許台灣 IP** |
| 間隔 | 「證券/期貨下單測試,**需間隔 1 秒以上**,以利系統留存測試紀錄」 |
| 版本 | shioaji **>= 1.2** |
| 權限 | API key 須開通「交易」權限 |
| 時序 | **API 簽署時間須早於測試時間**,否則不算 |
| 審核 | 約 5 分鐘 |
| 查結果 | 簽署中心網站,或**正式模式**登入後看 `accounts` 的 `signed` 欄位 |

官方範例的委託內容(本功能的預設值就照抄這兩組):

```python
# 證券
contract = api.Contracts.Stocks.TSE.TSE2890
sj.StockOrder(action=Buy, price=28, quantity=1,
              price_type=LMT, order_type=ROD,
              order_lot=Common, order_cond=Cash, account=api.stock_account)
# 期貨
contract = api.Contracts.Futures.TXF.TXFE6
sj.FuturesOrder(action=Buy, price=37000, quantity=1,
                price_type=LMT, order_type=ROD,
                octype=Auto, account=api.futopt_account)
```

---

## 這一筆最需要說清楚的事:與鐵則 14 的關係

鐵則 14 / P-10 寫得很明白:

> 下單一定先跳確認視窗;**只有 `_confirm_and_place_order()` 可以呼叫
> `place_order()`**。

這個功能無可避免會產生**第二個** `place_order()` 呼叫點。這是 ADR-139 明確
記錄的例外,而且用兩道**更強**的閘門換取:

1. **`simulation=True` 的一次性連線,而且每次送出前重驗。**
   `SinopacApiTestSession._assert_simulation()` 在 `login` /
   `place_stock_test_order` / `place_futures_test_order` 每一個方法的第一行
   都跑一次,不是只在 `__init__` 驗。理由:這個物件會被丟進背景執行緒、
   跨好幾秒的流程,「建構時驗過就好」的假設在這裡不成立。
2. **GUI 那邊仍然先跳確認視窗**,而且把「實際會送出什麼」逐欄位攤開
   (商品/買賣別/價格/數量/委託條件)給使用者看過才會走到 broker 層。

也就是說:**例外的是「哪個函式可以呼叫」,不是「要不要確認」。**
鐵則 14 真正要保護的「沒有委託在使用者不知情下送出去」完全沒有被放寬,
反而多了一層「連環境都不可能接錯」的保護。

診斷案例對這兩件事各有一條斷言,而且突變測試證明拿掉任何一道都會紅。

---

## 設計

### 分層(照 ARCHITECTURE.md 的界線)

| 層 | 檔案 | 負責 |
|---|---|---|
| 純規則 | `core/api_test.py`(新增) | 測試時段、版本下限、欄位驗證、最近月合約挑選、報告排版。零 tkinter / 零 shioaji(鐵則 11),離線可測 |
| 券商 | `brokers/sinopac.SinopacApiTestSession`(新增) | 建立 `simulation=True` 連線、查合約、送測試單。唯一可以碰 SDK 的層(ADR-097) |
| GUI | `stock_app_pro.open_api_test_dialog()` 等 | 收輸入 → 確認視窗 → 背景執行緒 → `safe_after` 回 UI(鐵則 13) |

### 為什麼是**獨立的一次性連線**,不是共用 `self.api`

使用者很可能**正登著正式環境在跑策略**。若測試共用同一個連線物件,
`sj.Shioaji(simulation=True)` 等於把他從實盤踢下線 —— 那是絕對不能發生的
副作用。所以做成獨立類別:建立 → 測完 → `close()` 丟掉,正式連線
(`self.brokers['sinopac']`)全程沒被碰過(診斷有一條專門守這件事)。

### 期貨月份**動態挑最近月**,不照抄 `TXFE6`

官方範例寫死 `TXFE6`(2026 年 5 月),那是**會過期的**。照抄到程式裡,
幾個月後就變成「查無此合約」而測試失敗,而失敗訊息完全不會告訴你原因是
合約過期。

`api_test.pick_near_month()` 從 `Contracts.Futures.TXF` 挑今天之後交割日最近
的那一個,並**排除 R1/R2** —— 那是報價用的連續合約,不是可下單的月份合約
(而且它常常是日期最近的那一個,不排除就會被選中)。挑選規則放 core/
才測得到,adapter 只負責把 SDK 物件攤平成 `[(代碼, 交割日), ...]`。

### 間隔取 1.5 秒而不是 1.0

官方說的是「間隔 1 秒**以上**」,取剛好 1.0 會落在邊界上;而這件事失敗的
代價是「測試紀錄沒留成、要重跑、還要再等 5 分鐘審核」,多等 0.5 秒毫無成本。
單元測試 `assertGreater(ORDER_INTERVAL_SEC, 1.0)` 釘住這個決定。

### 「僅限台灣 IP」誠實回報,不猜

`window_status()` 回傳三個值,第三個是「這個時段是否僅限台灣 IP」。
程式判斷不出使用者的對外 IP 在哪,所以把這件事當成旗標交給 UI 提醒,
**不自己猜一個答案** —— 猜錯的後果是使用者以為程式壞了,其實是他人在國外
(鐵則 4 的精神:沒有的資訊要標示,不要編)。

### 刻意**不**自動帶入現價

價格欄位預設用官方範例的值(2890 → 28、TXF → 37000),使用者可改。
不去抓當下報價,因為那會踩到**鐵則 5 的快照節流**。對話框直接寫明
「請填當日漲跌停之內的合理價,否則會被交易所退單」,把這件事講開,
而不是偷偷打一次快照。

### 數量上限鎖死在 1

`validate_order()` 擋掉數量 > 1。這是測試不是交易,量大沒有意義,
只會讓萬一環境接錯時的後果變嚴重。

---

## 使用方式

「🔒 登入券商實盤 API」對話框 → 「🧪 API 測試 (證券/期貨下單測試)」。

入口放在登入視窗裡,是因為使用者會在「登入了卻不能下單」的時候來開這個
視窗,答案就在旁邊。

視窗提供:
- 開始前檢查(版本、時段、地域限制提醒)
- API Key / Secret Key(預設帶入已存的)
- 證券 / 期貨兩項可各自勾選,代碼/價格/數量可改
- 「▶ 開始測試」→ 確認視窗 → 背景執行 → 逐步結果 + 完整報告
- 「🔍 查詢測試狀態」→ 讀**正式模式**登入後的 `accounts.signed`

---

## 檔案

| 檔案 | 變更 |
|---|---|
| `core/api_test.py` | **新增**:`parse_version` / `version_ok` / `window_status` / `validate_order` / `pick_near_month` / `preflight` / `format_report` / `signed_summary` + 常數 |
| `brokers/sinopac.py` | **新增** `SinopacApiTestSession`;`SinopacBroker.signed_rows()` |
| `stock_app_pro.py` | `open_api_test_dialog()` 與 5 個輔助方法;登入視窗加入口 |
| `tests/test_core.py` | +21 |
| `diag_repro_issues.py` | ADR-139 案例(走完整 GUI 送單路徑) |
| `PITFALLS.md` | P-117 |
| `ARCHITECTURE.md` | 新模組與驗證方式 |

**編號檢查**:檔名與程式碼註解兩處都查過,最大 ADR-138、最大 P-116。

---

## 驗證

### 離線(全綠)

- `python tests/test_core.py` — **784 通過**(原 763,+21)
- `python tests/test_brokers.py` — 42 通過
- `python diag_repro_issues.py` — **63 案例全 PASS**(原 62,+1)
- `python diag_crossref.py` — 無斷鏈
- `py_compile` 全過

### 突變測試

| 把程式改成 | 預期 / 結果 |
|---|---|
| 拿掉兩筆單之間的 `time.sleep` | 診斷紅「兩筆測試單之間要等…」 |
| `ORDER_INTERVAL_SEC` 改 1.0(邊界) | 單元紅 + 診斷紅 |
| 拿掉 `place_stock_test_order` 的 `_assert_simulation()` | 診斷紅 |
| 測試連線改成 `simulation=False` | 診斷紅「API 測試連線必須是模擬模式」 |
| 拿掉確認視窗(鐵則 14) | 診斷紅 |
| 期貨月份改成照抄 `R1` 連續合約 | 診斷紅 |
| 登入失敗後照樣送單 | 診斷紅 |

### 沒辦法離線驗證的部分(誠實列出)

**真實的模擬環境連線與送單完全沒有跑過。** 這個工作環境沒有永豐帳號、
沒有網路連到 `api.sinotrade.com.tw`,診斷用的是假的 session 物件。
所以以下只能由使用者實機確認:

- `sj.Shioaji(simulation=True)` 在你的 shioaji 版本上真的能登入模擬環境
- 模擬環境的 `Contracts.Stocks.TSE` / `Contracts.Futures.TXF` 結構與正式環境
  相同(`stock_contract()` / `futures_months()` 的取法是照正式環境寫的)
- `api.Order(...)` 帶 `order_lot` / `octype` 在模擬環境被接受
- 永豐端真的有留下測試紀錄、審核後 `signed` 變成 True

### 請使用者實機驗證

**前提**:週一~五 08:00~20:00(18:00 後要在台灣);API 條款已簽署且簽署時間
早於現在;API key 有「交易」權限。

1. 「🔒 登入券商實盤 API」→「🧪 API 測試」→ 視窗開得起來,
   最上面的「開始前檢查」顯示版本與時段判斷。
2. 填好金鑰 → 按「▶ 開始測試」→ **應該先跳確認視窗**,上面寫明會送出
   哪兩筆單。按「否」→ 什麼都不會送。
3. 按「是」→ 逐步顯示:建立模擬連線 → 登入測試 → 證券下單 → 等 1.5 秒
   → 期貨最近月合約代碼 → 期貨下單 → 完整報告。
4. **測完之後,你原本的正式連線要還在**(如果測試前已登入實盤,
   右上角應仍是綠色已連線,策略照跑)。
5. 等約 5 分鐘 → 以正式模式登入 → 回這個視窗按「🔍 查詢測試狀態」→
   證券與期貨帳戶應顯示「✅ 已通過」。也可到永豐簽署中心網站對照。
6. 若價格被退單(超出當日漲跌停),改成當日合理價再測一次 ——
   重測沒有副作用。
