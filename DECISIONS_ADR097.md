
## ADR-097：多券商下單抽象層 — 階段 0 (brokers/ 套件 + 永豐 adapter 殼子)

### 背景

使用者提出未來要新增群益、兆豐、凱基三個實體券商帳戶 (三家都可以申請 API
服務，且都是 Python SDK 型態)，運作模式要求是「四家同時在線、依帳戶分流
下單」，而不是「一次只用一家、手動切換」。

目前 `stock_app_pro.py` 對永豐 shioaji 是**直接耦合**：全檔案有上百處直接
呼叫 `self.sj_api.Contracts`、`self.sj_api.quote.subscribe`、
`self.sj_api.Order(...)`、`sj.constant.*` 等 shioaji 原生物件與列舉，中間
沒有任何跟券商無關的介面層。多券商不是加 if-else 就能做到，必須先插入一層
抽象，讓四家各自成為這層介面的一個 adapter。

### 決策

1. **新增頂層套件 `brokers/`**。不放進 `core/`/`data/`，因為鐵則 11
   (ADR-009) 規定那兩個套件必須維持零第三方券商 SDK 依賴；`brokers/`
   的存在理由正好相反，就是要封裝各家 SDK 差異，因此允許 `import shioaji`
   之類的依賴。這條界線寫進 `ARCHITECTURE.md` 分層圖。

2. **`brokers/base.py`** 定義 `BrokerClient` 共用基底 (目前只有
   `new_session`/`login`/`logout` 骨架)；**`brokers/sinopac.py`** 的
   `SinopacBroker` 是永豐 shioaji 的第一個 adapter。

3. **階段 0 範圍刻意縮小**：只把「連線生命週期」搬進 `SinopacBroker` ——
   建立 `sj.Shioaji()` 實例、`login`、`activate_ca`、註冊四路 v1 quote
   callback + 一路 order callback、`logout`。`stock_app_pro.py` 這幾處
   改成呼叫 `self.brokers['sinopac']`。

   **委託組裝、報價訂閱 (per-symbol subscribe)、部位查詢、`kbars()`/
   `snapshots()` 等其餘上百處 shioaji 呼叫這次完全沒有搬動**，
   `self.sj_api` 仍然指向跟 `self.brokers['sinopac'].api` 完全相同的
   物件，所有既有呼叫點行為不變。

4. **adapter 方法一律「一對一」包住對應的 shioaji 呼叫**，本身不吞例外、
   不寫日誌——原本每個呼叫點各自的 try/except 與日誌訊息全部留在呼叫端，
   確保這次搬動是零行為改變的重構。

### 為什麼刻意不一次做完

- **無法實機驗證**：此工作環境沒有畫面、也沒有真實永豐帳號，那上百處呼叫
  深度依賴 GUI 端狀態 (`self.current_contract`/`self._wl_contract_cache`/
  `self.quote_lock` 等)，貿然搬動卻無法驗證，風險遠大於效益。
- **過早抽象會猜錯**：在還沒實作任何第二家券商之前，不知道統一介面
  (`place_order`/`subscribe_quote`/`Contract`/`Position` 等資料形狀)
  該怎麼設計。等階段 1 (群益) 動工、看到第二家 SDK 的真實樣貌後，才會
  知道這層介面該長什麼樣，屆時再回頭擴充 `BrokerClient` 與
  `SinopacBroker`，並更新 `ARCHITECTURE.md`。

### 與 ADR-096 的關係 (對後續階段有利)

ADR-096 已經把 `place_order()` 改成背景執行緒 + `safe_after()` 排回 UI。
這對多券商是正面訊號：未來統一介面的下單方法本來就該設計成非同步，階段 1
可以直接沿用這個既有模式，不需要另外發明一套。

### 測試

- `python tests/test_core.py`：**339 個測試全數通過**。`core/`/`data/`
  完全沒有改動，符合鐵則 11。
- `python diag_repro_issues.py`：改動前後逐一對照，結果**完全一致** ——
  ADR-024 / ADR-035-036 / ADR-041 三個案例在改動前 (乾淨的 main) 就已經
  FAIL，屬於既有問題、與本次改動無關；其餘全數 PASS。這是「零行為改變」
  的實際證據。
- `python diag_crossref.py`：無跨模組斷鏈 (無孤兒 `self.xxx`)。
- `python -m py_compile stock_app_pro.py brokers/*.py`：語法通過。

### 需使用者實機驗證

這次改動的路徑 (登入 / 登出 / callback 註冊) 是深度 shioaji 耦合，此環境
無法實測真實連線。請在有畫面、有永豐帳號的機器上驗證：

1. 登入券商實盤 API 正常：合約下載完成、憑證啟用成功、五檔報價與委託回報
   callback 都正常運作 (看系統日誌有沒有「五檔流初始化異常」或「委託回報
   callback初始化異常」)。
2. 重新登入流程正常：先登出再登入、以及斷線自動重連 (ADR-071) 都要試一次，
   確認會建立全新連線物件而不是重用壞掉的舊物件 (ADR-026 的坑)。
3. 關閉視窗時仍會先嘗試登出再強制結束行程 (ADR-014)，終端機會跳回提示字元。

### 待辦 (階段 1-3，需使用者先提供資訊才能動工)

- **群益 / 兆豐 / 凱基三家 SDK 的官方文件或 PyPI 套件名稱**。這是真實交易
  系統，下單邏輯絕不能用猜的寫，沒有文件就不動工。
- **報價來源決策**：同一檔股票四家都有報價時，固定用某一家顯示，還是可以
  每檔指定？
- **下單帳戶選擇的 UI 設計**：每次手動選，還是可以設定「這檔股票預設用哪個
  帳戶」？
- **各家零股 / tick 規則確認**：`core/tick_rules.py`、`core/order_rules.py`
  目前假設是交易所統一規則，理論上四家共用沒問題，但需確認沒有各家自訂的
  額外限制 (例如某家對零股數量或委託方式有更嚴格的規定)。
