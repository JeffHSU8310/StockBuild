
## ADR-060：期交所資料讀不到的真正原因（相對路徑）、登出後仍狂發下載、以及 ADR-058 引入的「跳過下載反而沒資料」bug

使用者連續三輪回報同一組問題沒被解決，且明確要求嚴謹看待。本 ADR 逐條追到
根因，並補上「使用者自己看得出狀況」的診斷能力——因為前幾輪最大的失敗
不是功能沒做，而是**做了卻沒有任何方式讓使用者確認它有沒有生效**。

---

### 一、期交所資料到底有沒有被讀到？該放哪？（使用者需求 #1）

**根因：`TAIFEX_BASE_DIR = "."` 是相對路徑。**

`"."` 解析成「啟動程式時的**當前工作目錄 (CWD)**」，不是程式所在資料夾。
從 Anaconda Prompt、桌面捷徑、或任何其他資料夾啟動時，CWD 都不是
`G:\StockBuild`，於是 `taifex_daily/MTX.csv` 根本找不到。

**而且失敗是安靜的**——`taifex_store.load_daily()` 找不到檔案就回傳空
DataFrame，不拋例外、不寫日誌。結果就是使用者實測到的畫面：
明明匯入過 MTX，MXFR1 還是照樣狂發分段下載，**而且沒有任何訊息說明原因**。

同樣問題存在於 `broker_config.json`、`watchlists.json`、`chart_layout.json`、
`indicator_settings.json`、`quant_strategies.json`、`quant_state.json`、
`paper_account.json`——全部都是相對路徑。

**修法**

1. 模組層級新增 `APP_DIR = os.path.dirname(os.path.abspath(__file__))` 與
   `app_path(*parts)`，**所有**資料檔一律以程式所在資料夾為基準。
   不論你怎麼啟動，路徑都指向同一個地方。
2. **正確存放位置**：`G:\StockBuild\taifex_daily\`
   （`TX.csv` / `TX_day.csv` / `MTX.csv` / `MTX_day.csv` / `TMF.csv` …）。
   用「📥 期交所歷史」匯入會自動存到這裡，**不需要手動搬檔案**。
3. **讓讀取結果看得見**：每個 (商品, 盤別) 第一次查詢一定寫一行日誌——
   `✓ 已讀取 MTX (近全) 6,224 根,涵蓋 2001-04-09 ~ 2026-07-17。(檔案:…)`
   或 `✗ 找不到 MTX (近全) 的本地資料。我找的路徑是:… `。
4. **新增「🔎 期交所資料狀態」按鈕**：一次列出所有商品/盤別的檔案存在與否、
   涵蓋範圍、以及資料夾完整路徑。使用者不必翻資料夾猜。

**順帶確認：代號對應鏈本來就是對的**
`MXFR1 → product_code 'MXF' → 期交所 'MTX'`、`TXFR1 → TXF → TX`、
`TMFR1 → TMF → TMF`（R2 與特定月份合約也都能解析出商品，但延伸只對 R1 生效，
因為期交所序列是「每日近月」建構的連續日K，語意只對應 R1）。
所以「期貨標的代號」不是失敗原因，路徑才是。

---

### 二、已登出，系統還一直分段下載（使用者需求 #2）

**根因：`AuthError: Not authenticated` 不在 `_looks_like_session_dead` 的
關鍵字裡。**

該函式只認 `sessionnotestablished` / `session expired` / `disconnected` 等。
登出後每一段都拋 AuthError，但因為不被認定為「連線已死」，`_try_seg` 照樣
重試 1~2 次，然後繼續下一段——**整批 21 段全部跑完才收工**，日誌被洗版，
還白白對券商送出幾十次必定失敗的請求。

**修法**

1. 關鍵字加入 `not authenticated` / `autherror` / `unauthorized` /
   `not login` / `not logged in` / `please login`。未登入在語意上就是
   「這條連線不能用了」，必須立刻中止整批。
2. 新增 `_downloads_should_abort()`：程式關閉中、已登出
   (`api_logged_in=False`)、或使用者按了強制終止，任一成立就停手。
   **分段下載每段開始前檢查一次、每次重試前再檢查一次**。
3. 中止時日誌明確區分「已中止（登出/關閉/取消）」與「失敗（流量管制）」，
   不要讓使用者以為是券商在擋。

---

### 三、【ADR-058 引入的 bug】完全跳過下載時，反而變成「取不到資料」

ADR-058 讓「期交所已完整涵蓋」的情況完全跳過券商下載——這是對的。但
`extend_shioaji_df()` 的舊寫法是：

```python
if sj_tf_df is None or sj_tf_df.empty:
    return sj_tf_df        # ← 回傳空表
```

跳過下載後 shioaji 端**合法地是空的**，這裡卻原樣回傳空表，於是圖表與回測
都變成「取不到歷史資料」。**等於這個最佳化把功能弄壞了。**

**修法**：shioaji 端為空且期交所端有資料時，直接回傳「期交所資料本身」
（依 tf 做日/周/月聚合）。空的 shioaji 不代表沒有資料，只代表這次不需要它。
分K 仍然回傳空（官方日行情無法產生分K，不可假裝有）。

實測（使用者的 MTX.csv，6224 根）：日K 6224 根、周K 1301 根、月K 304 根，
全部由期交所資料獨力產出。

---

### 四、主圖也套用「期交所已涵蓋就別下載」

ADR-058 只把 `_taifex_plan_download` 接到回測與最佳化。使用者純粹看圖
（`[日K] 載入成功 (MXFR1)` 之後那些分段下載）時照樣被流量管制洗版。
現在主圖的背景補全也走同一條路徑。

---

### 需使用者實機驗證（請照順序）

1. `python tests/test_core.py` → **219 項全過**；`python diag_repro_issues.py` → 全 PASS。
2. **先按「🔎 期交所資料狀態」**——這一步最重要，它會告訴你：
   * 資料夾完整路徑（應該是 `G:\StockBuild\taifex_daily`）
   * 每個商品/盤別讀到幾根、涵蓋到哪，或者哪個檔案不存在
3. 若顯示 `✗ 無檔案`，按「📥 期交所歷史」重新匯入一次（會自動存到正確位置，
   並同時產生 `_day.csv`）。**不要手動搬檔案**。
4. 查詢 MXFR1 日K → 日誌應出現
   `✓ 已讀取 MTX (近全) …` 與 `完全略過券商下載`，
   **不該再看到成排的分段下載失敗**。
5. **登出**後，日誌應立刻出現「已停止 (連線已登出或使用者中止)，剩餘 N 段不再嘗試」，
   而不是繼續刷 AuthError。
6. 回測 MXFR1 日K 選 20Y，確認能跑出結果（純期交所資料路徑）。

### 相關程式位置

* `stock_app_pro.py`：`APP_DIR`/`app_path()`、七個資料檔改絕對路徑、
  `TAIFEX_BASE_DIR = APP_DIR`、`_taifex_load_hist` 的讀取日誌、
  `show_taifex_status()` 與「🔎 期交所資料狀態」按鈕、
  `_looks_like_session_dead` 新增認證關鍵字、`_downloads_should_abort()`、
  分段下載迴圈與重試的中止檢查、主圖背景補全接上 `_taifex_plan_download`。
* `core/taifex_daily.py`：`extend_shioaji_df` 的「shioaji 端為空」分支修正。
* `tests/test_core.py`：`TestTaifexOnlyPathADR060`（214 → 219）。
* `diag_repro_issues.py`：新增 ADR-060 案例。
