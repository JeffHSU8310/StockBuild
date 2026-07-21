# DECISIONS.md — 架構決策紀錄 (ADR)

> 格式：每筆決策包含「背景 / 決定 / 理由 / 替代方案 / 後果」。
> 只記重大或容易被後人重新踩坑的決定，不記瑣碎的排版調整。

> 本 Project 空間目前只找得到 `stock_app_pro.py`，尚未找到 ADR-001~004
> (記憶中提到的 PySide6/pyqtgraph/yfinance/三層架構 那組決策)。
> 如果那四筆決策也適用於這個 tkinter+shioaji 專案，請把原始 ADR-001~004
> 內容貼給我，我幫你併進這份文件；否則本文件從 ADR-005 開始記錄，
> 專屬於 `stock_app_pro.py` 這條 tkinter+shioaji 主線。

---

## ADR-005：shioaji 零股/五檔資料修正 — 停用 v0 callback、五檔改誠實顯示、snapshot 節流

- **日期**：2026-07-11
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，待使用者盤中實測驗證)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者回報：零股行情資料一直有問題，五檔委買賣資料也不正常。
檢視 `stock_app_pro.py` 後定位出四個獨立但會疊加放大的問題：

1. 同時註冊了 shioaji v0 (`set_quote_callback`，字典格式) 與 v1
   (`set_on_tick_stk_v1_callback` 等，typed 物件格式) 兩組 callback，
   兩邊都寫入同一組暫存變數 (`current_tick_odd/normal` 等)。
2. v0 callback 用 topic 字串裡有沒有出現 "ODD" 來判斷是否為零股，
   這個判斷方式不可靠，會把整股訊息誤判寫進零股暫存 (或相反)。
3. 盤後 fallback 邏輯假設 `Snapshot` 物件有 `bid_ask` 屬性可以解出真實五檔，
   但 shioaji 的 `Snapshot` 實際上**沒有這個屬性**，只有最佳一檔的
   `buy_price/sell_price/buy_volume/sell_volume`；原本的「優先讀真實五檔」
   分支永遠走不到，最終都是用總成交量除以固定比例「演算」出五檔量——
   這是假數據。
4. 零股模式下，若沒有零股串流，會直接拿**整股**的 snapshot 冒充零股報價
   顯示在零股五檔區，資訊具有誤導性。
5. 無串流時的 fallback 迴圈每 0.5 秒呼叫一次 `self.sj_api.snapshots()`，
   沒有任何節流；shioaji 有每日 API 流量配額，長時間掛著會把當天配額
   用盡，導致之後全部查詢失效 (這可能是使用者感覺「數據一直有問題」
   的部分原因，且和上面幾點症狀混在一起難以分辨)。
6. 四路訂閱 (整股 Tick / 整股 BidAsk / 零股 Tick / 零股 BidAsk) 包在同一個
   `try/except` 裡，任何一路失敗會讓整批訂閱中斷且無記錄，事後無法排查
   到底是哪一路沒訂閱成功。

### 決定

1. **移除 v0 `set_quote_callback`，只保留 v1 typed callbacks**
   (`set_on_tick_stk_v1_callback` / `set_on_bidask_stk_v1_callback` /
   `set_on_tick_fop_v1_callback` / `set_on_bidask_fop_v1_callback`)。
   分流零股/整股一律讀取物件上的 `intraday_odd` 布林欄位，不再用字串比對。

2. **所有 callback 寫入暫存、worker 讀取暫存都透過 `self.quote_lock`
   (`threading.Lock`) 保護**，避免 callback 執行緒與 UI worker 執行緒
   之間的競態。

3. **`generate_5_levels_from_snapshot()` 改為誠實版**：
   - 第 1 檔顯示 snapshot 真實的 `buy_price/sell_price/buy_volume/sell_volume`。
   - 第 2~5 檔僅依 `get_tick()` 規則推導「參考價位」，成交量固定顯示 `--`，
     不再用總量演算法生成假數量。
   - 回傳物件帶 `is_simulated = True` 標記，UI 顯示時標註「(參考)」/
     「盤後快照(參考)」等字樣，不讓使用者誤以為是真實五檔。

4. **零股模式下，若沒有零股串流，不再拿整股 snapshot 冒充零股報價**：
   改為清空零股五檔 (`--`)，行情列顯示「無串流，整股參考價：X」，
   並在系統日誌提示一次「零股僅盤中 09:00–13:30 提供即時串流」。

5. **`snapshots()` 呼叫節流至每 5 秒一次**
   (`self.last_fallback_snap_time`)，避免無限制高頻呼叫吃光每日流量配額。
   原本零股模式下每 3 秒額外呼叫一次 snapshot 算價差的邏輯一併移除，
   改由整股 v1 tick callback 直接快取最新整股收盤價 (`self.last_norm_close`)，
   零股價差計算不再需要額外的 API 呼叫。

6. **訂閱邏輯拆成逐路獨立 try/except**，並指定 `version=sj.constant.QuoteVersion.v1`
   (若舊版簽名不吃 `version` 關鍵字則自動退回不帶版本參數的呼叫)；
   每路成功/失敗都印到系統日誌，格式類似：
   `【訂閱結果】整股Tick:✓ 整股五檔:✓ 零股Tick:✗ 零股五檔:✗`。

7. **新增零股下單本地防呆**：盤中零股僅接受限價 ROD、數量 1~999 股，
   不符合規則者在送出前於本地端擋下並提示，不送去給券商退單。

8. **價格顯示統一透過新增的 `fmt_price()`**，依 `get_tick()` 規則決定小數位數，
   避免各處各自寫格式化邏輯造成不一致。

### 替代方案 (已考慮但未採用)

- **繼續保留 v0 + v1 雙軌，只加強 topic 字串判斷的準確度**：
  被否決。字串判斷本質上不可靠，即使加強 heuristic 仍可能在 shioaji
  未來調整 topic 格式時再次失效；v1 的 `intraday_odd` 欄位是官方結構化
  資料，可信度更高，沒有理由繼續維護一套不可靠的 v0 parsing。

- **盤後五檔繼續用演算法生成完整五檔量，只是換一個比較「像樣」的比例公式**：
  被否決。無論公式多細緻，本質上都是编造數據，會讓使用者誤判真實委買賣力道，
  對一個要走向程式化下單的系統來說風險太高。誠實顯示 `--` 遠比精緻的假數據安全。

- **snapshot 節流時間定更短 (例如 1~2 秒)**：
  暫未採用。5 秒是保守起始值，避免流量問題重演；如果之後盤中測試發現
  5 秒太粗導致 UI 更新明顯延遲，可以再開一筆 ADR 調整，但需要先查
  shioaji 官方流量配額文件並附上依據，不要單憑感覺調整。

### 後果 / 影響

- **正面**：零股與整股資料不再互相污染；五檔顯示不再有假數據；不會再有
  「盤中用著用著整個報價系統突然失效一整天」的流量耗盡風險；訂閱失敗
  現在有明確的日誌可以排查；零股下單不會再被券商因規則不符退單。

- **需要使用者配合驗證的部分**：
  - 盤中 (09:00–13:30) 實測零股串流是否確實有資料進來
    (若「零股Tick」「零股五檔」訂閱結果顯示 ✗，代表該檔券商端可能本來
    就沒有開放零股即時報價，需要另外排查，不是這次程式修正能解決的範圍)。
  - 5 秒的 snapshot 節流間隔是否符合實際使用體驗，太慢或太快都可以回饋
    再調整。

- **待補**：本次修正沒有改動歷史 K 線資料抓取 (yfinance / FinMind / shioaji
  kbars) 的邏輯，也沒有改動繪圖層 (`draw_chart`)，這兩塊仍照舊。

### 相關程式位置 (`stock_app_pro.py`)

- `on_tick_stk_v1` / `on_bidask_stk_v1` / `on_tick_fop_v1` / `on_bidask_fop_v1`
- `process_broker_login`（callback 註冊）
- `generate_5_levels_from_snapshot`
- `update_quote_ui` / `fmt_price`
- `fetch_realtime_worker`
- `fetch_data_worker` 內的訂閱區塊
- `execute_order`（零股下單防呆）

---

## ADR-006：零股收盤價取得與整股價差顯示

- **日期**：2026-07-11
- **狀態**：已採納（已寫入 `stock_app_pro.py`，待盤中/盤後實測驗證）
- **對應 shioaji 版本**：1.5.6

### 背景

使用者盤後（21:30 冷登入）在零股看板看到行情列顯示「整股參考價：105.80」，
但當日零股實際收盤為 106，兩者對不上，懷疑零股資料有問題。

查證 shioaji 官方文件後確認這不是 bug，而是資料來源的先天限制：

1. `snapshots()` 回傳的 `Snapshot` 物件**只有整股欄位**
   （open/high/low/close/buy_price/sell_price/buy_volume/sell_volume 等），
   **沒有任何零股收盤價欄位**。盤後用 snapshot 撈到的 close 永遠是整股價，
   拿不到零股價 —— 這就是 105.80 ≠ 106 的原因。

2. 零股收盤價唯一的官方來源是 **整股 v1 tick 串流物件（TickSTKv1）** 上的
   `closing_oddlot_close`（盤後零股成交價）、`closing_oddlot_shares`（成交股數）、
   `closing_oddlot_bid_price` / `closing_oddlot_ask_price` 等欄位。
   這些欄位在**盤後零股（13:40–14:30）那盤 14:30 收完後**，透過整股 tick 串流推送進來。

3. shioaji **沒有**盤中零股的歷史 tick 查詢 API（官方 Gitter 已說明，需自行即時接收後保存）。
   因此「盤後冷登入想回補當日零股收盤價」在 shioaji 端**無解**，任何程式改動都補不了。

### 決定

1. **在 `on_tick_stk_v1` 快取盤後零股收盤資料**：
   整股（`intraday_odd == False`）tick 進來時，若 `closing_oddlot_close > 0`，
   將其連同 `closing_oddlot_shares`、日期一併快取到
   `self.last_odd_close` / `self.last_odd_shares` / `self.last_odd_date`。
   這是唯一能取得真實零股收盤價的來源，收到就存。

2. **零股看板的價差顯示（本次使用者主要需求）**：
   - **盤中零股有即時串流時**：行情列顯示零股成交價，並同時顯示與整股最新價
     （`self.last_norm_close`，由整股 tick 串流即時快取）的價差與百分比，
     格式：`[零股/股] 即時串流: 106.00  (整股: 105.80  價差: +0.20 / +0.19%)`。
   - **盤後無串流、但當日已快取到零股收盤價時**：顯示真實零股收盤價與整股價差，
     格式：`[零股] 盤後零股收: 106.00  (整股: 105.80  價差: +0.20 / +0.19%)`。
   - **盤後無串流、且未快取到零股收盤（冷登入）時**：退回顯示整股參考價，
     並明確註明「今日零股收盤 shioaji 盤後無法回補」，不讓使用者誤會程式漏抓。

3. **零股五檔在盤後一律清空為 `--`**（沿用 ADR-005 原則，不捏造）。

4. **換股時清空 `last_norm_close` / `last_odd_close` 等快取**，
   避免把 A 股的整股價或零股收盤價錯套到 B 股。

5. **修正日誌時段提示**：原本只寫「零股僅盤中 09:00–13:30」，
   補上盤後零股 13:40–14:30，避免使用者誤以為只有盤中一盤。

### 替代方案（已考慮未採用）

- **盤後改用某個 API 回補當日零股收盤價**：查無此 API。snapshot 無零股欄位、
  盤中零股無歷史 tick，shioaji 端確實做不到，故不嘗試，改以誠實標註取代。

- **價差只顯示絕對數字、不顯示百分比**：使用者未特別要求，但百分比對判讀更直覺，
  且成本極低，故一併顯示。若嫌雜可再拿掉。

### 後果 / 影響

- **正面**：零股看板現在會顯示與整股的價差（含百分比）；只要 App 從盤後零股
  14:30 掛到收盤，就能顯示真實零股收盤價而非整股頂替；冷登入情境明確標註限制，
  不再讓使用者誤判為 bug。

- **限制（需使用者知悉）**：盤後冷登入（如當晚 21:30 才開 App）仍**無法**顯示當日
  零股收盤 106，因為 shioaji 無回補管道。要拿到真實零股收盤價，App 需在盤後零股
  時段（13:40–14:30）保持連線接收串流。

- **待驗證**：
  - 盤中（09:00–13:30）零股即時串流時，價差是否正確顯示。
  - 盤後零股（14:30 後）`closing_oddlot_close` 是否確實由整股 tick 串流帶入並快取成功。

### 相關程式位置（`stock_app_pro.py`）

- `__init__`：新增 `last_odd_close` / `last_odd_shares` / `last_odd_date`
- `on_tick_stk_v1`：快取 `closing_oddlot_close` / `closing_oddlot_shares`
- `update_quote_ui`：盤中零股即時串流價差（含 %）
- `fetch_realtime_worker`：盤後零股收盤價差 / 冷登入退回整股參考價
- `start_fetch_thread`：換股清空快取

---

## ADR-007：期貨日/週/月K改用「交易日」聚合 — 修正夜盤污染日盤問題

- **日期**：2026-07-11
- **狀態**：已採納 (已寫入 `stock_app_pro.py`,待使用者以歷史資料實測驗證)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者回報期貨報價異常:台指期近全日盤實際上漲 633 點,收盤價 46281 點,
漲幅 1.39%,但 App 顯示的數字對不上。查證當日台指期公開行情資料
(開盤 45,800、最高 46,495、最低 45,701、收盤 46,281、昨收 45,648、漲跌 +633、
+1.39%),確認使用者提供的數字是正確的,問題出在程式處理上。

檢視 `stock_app_pro.py` 的 `fetch_data_worker()` 後定位出根因:

1. shioaji 的 `kbars()` 對期貨只回傳**分K**,日/週/月K是程式自己用
   `sj_df.resample('D', label='left', closed='left')` 聚合出來的。

2. 台指期一天分兩段:日盤 08:45–13:45、夜盤 15:00–隔日 05:00。
   `resample('D')` 是依「自然日 00:00」切割,這對股票/指數沒問題 (沒有夜盤),
   但對期貨是錯的——會把「當天傍晚 15:00 起的夜盤」與「隔天的日盤」
   混進同一個自然日分組,或是把「當天凌晨 00:00–05:00 的夜盤延續」
   與「當天日盤」混在一起,兩種情況都會讓開盤價、收盤價、最高、最低通通跑掉。

3. 實測驗證:用一組模擬的 7/9 夜盤→7/10 日盤(收 46281)→7/10 傍晚夜盤 的分K,
   餵給原本的 `resample('D')`,7/10 這根日K的收盤被算成夜盤最新價 46500,
   而不是日盤實際收盤的 46281——證實夜盤污染確實會發生,且量體不小
   (台指期夜盤交易量約佔全日三分之一,不是可忽略的雜訊)。

### 決定

新增 `_resample_future_session()`,期貨的日/週/月K改用「交易日
(session date)」聚合，取代 `resample('D')`：

1. **交易日判斷規則**：每根分K依時間分類——
   - 時間 **> 13:45**（即 15:00 起的夜盤）→ 歸屬到**下一個交易日**。
   - 時間 **≤ 13:45**（凌晨 00:00–05:00 夜盤延續、或日盤 08:45–13:45）
     → 維持當天日期不變。
   - 這一條規則同時處理了夜盤跨兩個自然日的情況：15:00–23:59 那段
     （屬於前一自然日）與 00:00–05:00 那段（屬於後一自然日），
     兩段都會正確歸到同一個交易日分組。

2. **收盤價定義（依使用者確認）採「全時段收盤」（近全）**：
   由於日盤（08:45–13:45）在交易日分組內的時間順序上排在夜盤兩段之後，
   分組取 `'last'` 當 Close 會自然落在日盤 13:45 那筆——這正是使用者
   要的「近全」日K收盤定義，同時 Open 是夜盤 15:00 的開盤，
   涵蓋整個近全時段的高低範圍。

3. **夜盤處理（依使用者確認）採「標準交易日邏輯」**：
   夜盤 15:00 起的部分併入下一個交易日，不會被自然日切割打散成兩截。

4. **週K/月K**：先用上述交易日規則算出正確的「交易日日K」，
   再對這個日K序列做 `W-MON` / `MS` 的二次聚合，避免直接對分K做
   自然日週期聚合造成同樣的夜盤污染問題往上傳遞。

5. **股票與指數不受影響**：`asset_type == "future"` 才走新邏輯，
   股票/`index_tw` 維持原本的 `resample('D', ...)`，因為它們沒有夜盤，
   自然日切割本來就是對的，不需要改。

6. **例外保護**：交易日聚合過程如果拋例外，記錄日誌並退回原本的
   自然日 `resample()` 當備援，避免整個資料流程中斷；但退回時會在
   日誌註明「可能不準確」，提醒使用者留意。

### 替代方案（已考慮但未採用）

- **改用「日盤收盤價（13:45收，不含夜盤）」當日K收盤定義**：
  被否決（使用者明確選擇「全時段收盤」）。這個做法會讓 Open/High/Low
  只反映日盤 5 小時的區間，遺漏夜盤約三分之一成交量的價格資訊，
  不符合使用者想看到的「台指近全」視角。

- **夜盤獨立算一根「夜盤K」，不與任何日盤合併**：
  被否決（使用者明確選擇夜盤併入下一交易日的標準邏輯）。
  獨立夜盤K會讓日K數量變成日盤的兩倍，且不符合券商軟體「近全」
  單一日K的呈現慣例，會讓使用者需要额外理解兩套K棒對應關係。

- **直接呼叫 shioaji 是否有內建的日K/週K/月K API，跳過自行 resample**：
  已於 ADR-007 討論過程確認 `kbars()` 只提供分K，沒有更高週期的
  內建介面，故仍需自行聚合，只是聚合規則從自然日改為交易日。

### 後果 / 影響

- **正面**：期貨日/週/月K的開盤、收盤、最高、最低不再被夜盤污染，
  用模擬資料驗證後，7/10 交易日的收盤已能正確算出 46281（與使用者
  回報的實際數字吻合）。股票與指數的處理邏輯完全不受影響。

- **需要使用者配合驗證的部分**：
  - 用實際的歷史期貨資料（例如抓近一個月的 TXF 日K）比對本次修正
    後的結果，是否與券商看盤軟體的「台指近全」日K逐日吻合。
  - 週K/月K 的二次聚合結果是否符合預期（尤其是月初/月底、週一週五
    夜盤跨月/跨週的邊界情況）。
  - 若使用者之後想改回「日盤收盤（不含夜盤）」的定義，需要另開一筆
    ADR 記錄取捨理由，不要直接改參數而不留紀錄。

- **待補**：本次修正只處理期貨的日/週/月K聚合邏輯，沒有改動：
  - 分K（1/5/15/30/60分K）的抓取與顯示，這些本來就是 shioaji 原生
    分K，不受自然日/交易日切割問題影響。
  - 即時報價、五檔、零股相關邏輯（見 ADR-005、ADR-006）。
  - `draw_chart()` 繪圖層與技術指標計算，這兩塊維持原樣。

### 相關程式位置（`stock_app_pro.py`）

- `_resample_future_session`（新增方法，交易日聚合核心邏輯）
- `fetch_data_worker` 內的 resample 呼叫區塊（依 `asset_type == "future"`
  分流呼叫新方法或維持原本的自然日 resample）

---

## ADR-008：下單面板全面重構 — 交易別/種類/條件/取價/成交明細/當沖

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`,待使用者以券商 App 下單介面截圖比對驗證)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者提供手機券商 App 的下單介面截圖 (整股/證券交易、零股交易兩張),要求參考其
功能配置重新設計桌面版下單面板。比對截圖與現有 `stock_app_pro.py` 後,確認舊版
下單面板缺少多項券商 App 標配功能,且部分邏輯若不查證 shioaji 官方文件與交易所
規則就直接實作,容易做出「表面看起來對、實際送單會被退單」的介面。

查證 shioaji 官方文件與交易所公開規則後,確認以下關鍵事實：

1. **`order_lot` 實際上是 4 種,不是 2 種**：`{Common: 整股, Fixing: 定盤(盤後定價),
   Odd: 盤後零股, IntradayOdd: 盤中零股}`。舊版程式只支援 Common/IntradayOdd 兩種,
   完全沒有「盤後定價」這個獨立模式。
2. **`order_cond`(現股/融資/融券)舊版完全沒有**,只能下現股。
3. **`order_type`(ROD/IOC/FOK)舊版寫死 ROD**,沒有讓使用者選擇的介面。
4. **零股類 (盤中零股/盤後零股) 交易所規定**：僅能現股、僅能限價、僅能 ROD,
   **不可融資融券、不可現股當沖**。
5. **盤後定價 (Fixing) 交易所規定**：以當日 (上午) 收盤價為成交價,**使用者不能
   自訂價格**,14:30 集合競價撮合;現股/融資/融券的下單種類選項是否開放,
   查證時**沒有找到明確禁止的規定**,但也沒有找到「可與當日盤中交易資券相抵
   計入當沖額度」的明確依據 —— 這句原本寫得太肯定,已改為保守表述,實際是否
   支援需要使用者送模擬單實測後回報 (見下方「需要使用者配合驗證的部分」)。
6. **點擊五檔價格可直接帶入下單價**,是券商 App 的標準操作,舊版沒有做。
7. **取價快捷 (漲停/平盤/跌停/最佳買/最佳賣)** 是券商 App 標配,舊版沒有。
8. **成交明細跳動列表 (時間/價格/漲跌/量)** 舊版完全沒有,只有五檔,沒有逐筆成交;
   其漲跌欄位經比對截圖數值反推,是「當筆成交價 − 參考價(昨收/平盤價)」,
   不是逐筆價差。
9. **現沖/禁現沖 badge** 舊版沒有顯示,現股當沖需要商品本身可現沖
   (`contract.day_trade == 'Yes'`) 才能勾選。

### 決定

1. **交易別改為 4 選 1 按鈕群組**：整股(Common)／盤中零股(IntradayOdd)／
   盤後定價(Fixing)／盤後零股(Odd)，取代舊版的整股/零股二選一。
   切換時透過 `set_trade_mode()` 連動鎖住/開放種類、條件、限價市價、當沖等欄位，
   不讓使用者組出違規委託：
   - 零股類 (IntradayOdd/Odd)：種類鎖現股、條件鎖 ROD、限價市價鎖限價、
     當沖鎖不可勾選。
   - 盤後定價 (Fixing)：條件鎖 ROD、限價市價鎖限價，價格欄鎖定為當日收盤價
     並設為 `disabled` (不可自行輸入)，種類仍開放現股/融資/融券。
   - 整股 (Common)：種類、條件、限價市價、當沖皆可自由選擇。

2. **新增種類 (現股/融資/融券) 與條件 (ROD/IOC/FOK) 按鈕群組**，
   分別對應 shioaji 的 `order_cond` 與 `order_type`，依上述規則動態鎖定。

3. **新增取價快捷下拉選單**：漲停/平盤/跌停 (取自 `contract.limit_up` /
   `limit_down` / `reference`)、最佳買/最佳賣 (取自目前五檔第一檔)、
   最新成交 (取自 tick 快取)，選擇後直接帶入價格欄。盤後定價模式下停用
   (價格已鎖定，取價快捷無意義)。

4. **五檔價格改為可點擊**：點買價或賣價任一檔位，直接把該價格帶入下單價格欄
   (盤後定價模式下不受影響，因價格已鎖定)。

5. **新增成交明細跳動列表**：在整股 tick 與零股 tick callback 中，各自把
   (時間、成交價、與參考價的漲跌、成交量) 寫入獨立的 `deque(maxlen=20)`，
   顯示時依「紅漲綠跌」鐵則上色。整股/零股各自累積，切換看盤模式
   (「看盤:整股/零股」按鈕，與交易別的下單邏輯分開) 時顯示對應的一份。

6. **新增現沖/禁現沖 badge 與現股當沖(先賣後買)勾選框**：
   載入商品時擷取 `contract.day_trade` 判斷是否可現沖並更新 badge；
   當沖勾選框只有「整股 + 現股 + 該股可現沖」時才會被啟用，
   下單時只在「整股 + 現股 + 賣出 + 可現沖 + 有勾選」才附加 `daytrade_short=True`
   (先嘗試新版參數名，`TypeError` 時退回舊版 `first_sell` 參數，兩者都失敗
   則照一般委託送出並提示使用者)。

7. **下單所有規則在 `execute_order()` 二次防呆**：即使 UI 已經鎖住不合規的
   組合，送出前仍會再檢查一次「零股類是否現股/限價/ROD/1-999股」「盤後定價
   是否限價/ROD」，避免任何管道 (例如程式意外改到狀態變數) 繞過 UI 鎖定
   送出違規委託。

8. **委託送出後的日誌**內容擴充為包含交易別/種類/條件/是否當沖，方便事後追查
   每一筆委託當時的確切參數組合。

9. **【本次追加】期貨模式下鎖住「交易別」與「種類」整組按鈕**
   (`update_order_panel_for_asset_type`)：檢視完整實作時發現，切換到期貨合約
   (TXF/MXF) 時，原本「交易別」(整股/盤中零股/盤後定價/盤後零股) 與「種類」
   (現股/融資/融券) 這兩組按鈕仍是可以點的，但期貨完全沒有這些概念——雖然
   `execute_order()` 的期貨分支本來就不會讀取這兩個狀態，點了不影響下單結果，
   但畫面上留著可點的按鈕會讓使用者誤以為期貨也要選零股或融資融券。修正後，
   換股後若新合約是期貨，這兩組按鈕整組 disable，數量單位改顯示「口」；
   若換回股票，則重新解鎖並套用目前交易別對應的鎖定狀態。

10. **【本次追加】`set_trade_mode()` 新增 `user_initiated` 參數**：原本的實作中，
    `set_trade_mode()` 只設計給使用者點按鈕時呼叫，但補上「期貨模式鎖定/解鎖」
    這個追加功能後，換股或換 K 線週期時也需要呼叫它來重新同步鎖定狀態；如果
    直接呼叫，每次換股都會把使用者已輸入的委託數量重置成 1、並在系統日誌
    多印一行「交易別切換」，等於每次刷新報價都洗版。改為新增
    `user_initiated=True` (預設值，按鈕點擊時的行為不變) /
    `user_initiated=False` (背景重新整理時使用，跳過重置數量與日誌) 兩種模式。

### 替代方案 (已考慮但未採用)

- **維持整股/零股二選一，盤後定價另外做一個獨立分頁**：被否決。單一分頁、
  四個交易別平鋪呈現，使用者在同一個畫面就能看到所有選項與目前鎖定狀態，
  比切分頁更符合這是「單機瀏覽 + 下單合一」桌面應用的定位。

- **盤後定價價格欄仍開放使用者輸入，只在送出前檢查是否等於收盤價**：
  被否決。與其讓使用者輸入後才告知「這個價格不對」，不如直接把欄位鎖定
  顯示唯一合法值，體驗更直覺也更不容易誤解。

- **成交明細改用逐筆價差 (與前一筆比較) 而非與參考價比較**：
  被否決。比對使用者提供的截圖數值後 (106.00/-0.05、105.95/-0.10 等)，
  確認券商 App 顯示的是「與參考價(昨收/平盤)的差」，採用逐筆價差會與
  券商 App 顯示不一致，容易誤導使用者判斷當日漲跌方向。

### 後果 / 影響

- **正面**：下單面板功能對齊主流券商 App 應有的水準；零股類與盤後定價的
  委託規則由介面主動鎖住，大幅降低「組出違規委託被券商退單」的機率；
  新增的成交明細與取價快捷提升下單效率；現沖 badge 讓使用者在下單前就能
  確認商品是否可當沖，避免誤勾當沖選項；期貨模式不會再誤導使用者去點
  零股或融資融券按鈕；換股/換週期刷新報價不會再洗版日誌或蓋掉使用者
  正在輸入的數量。

- **【本次追加】已完成的靜態驗證**：因為工作環境沒有安裝 tkinter 也沒有
  顯示器，無法實際開窗測試，改用以下方式代替：
  1. AST 掃描整個 `StockTradingAppPro` 類別，確認所有 `self.xxx` 屬性/方法
     引用都能追溯到明確的定義來源 (賦值或函式定義)，排除 `AttributeError`
     風險，結果：0 筆可疑引用。
  2. AST 掃描 `create_widgets()` 內的執行順序，確認沒有「先讀取後定義」的
     widget 建構順序問題，結果：無異常。
  3. `python -m py_compile` 語法編譯全數通過，且確認沒有重複定義的方法。

- **需要使用者配合驗證的部分 (無法在此環境完成，請在有螢幕、有 tkinter 的
  機器上實測)**：
  - 實際開啟視窗，確認四種交易別切換時，種類/條件/限價市價/數量單位/當沖
    勾選框的鎖定與解鎖行為跟畫面預期一致；期貨合約載入時交易別/種類按鈕
    確實整組變灰。
  - 盤中實際測試四種交易別各自送出委託 (**建議先用模擬帳號**)，確認 shioaji
    沒有退單。
  - 確認 `daytrade_short` 是否為目前 shioaji 1.5.6 版本正確的當沖參數名稱；
    程式已做 `TypeError` 情況下的相容退路，但若两种参数名都不對，當沖旗標
    會送不出去 (委託仍會送出，只是不含當沖標記)，需要使用者用小額單實測確認。
  - **特別驗證「盤後定價 + 融資」或「盤後定價 + 融券」是否真的能送出去**，
    因為這點沒有查到明確依據 (見上方第 5 點已改為保守表述)，需要實測確認，
    若被退單請回報以便更新這筆決策。
  - 成交明細的漲跌基準 (`contract.reference`) 在除權息、盤中特殊調整等情況下
    是否仍然正確，需要實際盤中比對。
  - 本次修改未内建對 `sj.constant.StockOrderLot.Fixing` 送出後的實際委託回報
    格式做過完整測試 (盤後定價交易量較小，官方範例較少)，若送出後發現欄位
    對應有誤，需要另開 ADR 記錄調整。

- **待補**：本次修正只處理下單面板 UI 與 `execute_order()` 組裝邏輯，
  沒有改動：K 線繪圖 (`draw_chart`)、零股/五檔即時報價 (ADR-005/006)、
  期貨交易日聚合 (ADR-007)。這幾塊維持原樣不受影響。

### 相關程式位置 (`stock_app_pro.py`)

- `set_trade_mode` / `set_order_cond` / `set_order_type`（交易別/種類/條件狀態機）
- `update_order_panel_for_asset_type`（【本次追加】期貨模式鎖定交易別/種類）
- `update_daytrade_checkbox_state` / `update_daytrade_badge`（現沖 badge 與當沖鎖定）
- `on_quick_price_select` / `on_ladder_price_click`（取價快捷／五檔點擊改價）
- `_record_trade_tick` / `_refresh_trade_feed_ui`（成交明細跳動列表）
- `step_qty`（依交易別調整單位換算與上限）
- `execute_order`（委託組裝與二次防呆，含當沖 `daytrade_short`/`first_sell` 相容處理）
- `on_tick_stk_v1` / `on_tick_fop_v1`（新增呼叫 `_record_trade_tick`）
- `fetch_data_worker`（新增擷取 `contract.day_trade` 與 `contract.reference`；
  `update_ui()` 內【本次追加】呼叫 `update_order_panel_for_asset_type`)
- `create_widgets`（下單面板 UI 整段重寫）

---


---

## ADR-009：架構重構第一階段 — 抽出 core/data 純邏輯層，補上真正可執行的單元測試

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入專案，測試已在本機環境實際執行通過)
- **對應 shioaji 版本**：1.5.6（本次重構不涉及 shioaji API 呼叫本身）

### 背景

使用者提出：`stock_app_pro.py` 單檔已來到約 2000 行，且後續還有很多功能要加，
擔心會越來越難維護，請 Claude 自行判斷是否需要調整架構。

盤點目前類別的 65 個方法後，職責大致分四類：
1. **純邏輯計算**：`get_tick`/`fmt_price` (tick 規則)、`calculate_custom_indicators`
   (技術指標)、`_resample_future_session` (期貨交易日聚合，見 ADR-007)、
   `execute_order` 內的委託規則驗證 (見 ADR-008)。這幾塊完全不依賴 tkinter 或
   shioaji 物件本身，只是目前寫成類別方法、讀取 `self.xxx` 狀態，才「看起來」
   跟 GUI 綁在一起。
2. **檔案 I/O**：`load_config`/`save_config`/`load_watchlists`/`save_watchlists`，
   單純讀寫 JSON 檔案，不依賴 tkinter。
3. **網路/broker I/O**：`fetch_data_worker`、`fetch_market_indices_worker`、
   `fetch_taiwan_chips`、shioaji 登入與 quote callback 等，這些需要呼叫
   `self.after()` 把結果排回 UI 執行緒、需要讀寫 `self.current_symbol` 等
   即時狀態，屬於中度耦合。
4. **GUI 本體**：`create_widgets` 與所有事件處理、繪圖 (`draw_chart`)、
   下單面板互動 (`set_trade_mode` 等)，這些深度依賴 tkinter widget 物件本身。

CLAUDE.md 從一開始就寫著「目前本專案沒有獨立 core 模組與 `tests/test_core.py`，
因為 GUI/資料/下單邏輯目前都寫在同一個檔案裡」——這是已知缺口，這次剛好是
處理它的時機。

### 決定

**只處理第 1、2 類 (純邏輯 + 檔案 I/O)，第 3、4 類這次不動。**

理由：這個工作環境沒有安裝 tkinter、也沒有顯示器，無法真的開窗測試 GUI。
把深度依賴 tkinter widget 或 threading/self.after 時序的程式碼搬動，
沒辦法在這裡驗證搬完之後行為是否還一致，風險與可驗證性不成比例。
純邏輯與檔案 I/O 則不同：可以直接拿真實資料跑，錯了測試會紅，
是這次適合动手、也真正能交出「已驗證」成果的範圍。

1. **新增 `core/` 套件**（零 tkinter、零 shioaji 依賴，只靠 pandas/numpy/stdlib）：
   - `core/tick_rules.py`：`get_tick()`、`fmt_price()`，改成顯式吃
     `(price, asset_type, raw_symbol)` 參數，不再讀 `self`。
   - `core/indicators.py`：`calculate_indicators()`，改成顯式吃 MA/BB/MACD/
     RSI/KDJ/DMI 的各項參數 (布林值、字串)，不再讀 `self.ma_shows[i].get()`
     這類 tkinter Variable。**刻意保留原本看似意外的耦合**：MACD/RSI/KDJ/DMI
     四塊算式共用一個 `try/except`，任一參數轉換失敗會連帶跳過其餘幾個——
     這是重構前就有的行為，這次是純結構搬移，不夾帶邏輯修正；如果之後
     想拆成互不影響的獨立 `try/except`，請另開一筆 ADR 記錄，不要在重構
     裡面順手改掉。
   - `core/futures_session.py`：`resample_future_session()` (ADR-007 的核心
     邏輯) 與 `resample_natural_day_fallback()`。純函式版本不吞例外，
     例外直接往上拋；原本的 `try/except` + 日誌 + 退回自然日聚合的動作
     留在 GUI 層的 `_resample_future_session()` 薄封裝裡。
   - `core/order_rules.py`：`validate_stock_order()` 與 `is_daytrade_eligible()`
     (ADR-008 的委託規則驗證)，回傳 `(ok, reason)`，不做任何日誌或 I/O。

2. **新增 `data/` 套件**：
   - `data/config_store.py`：`load_broker_config()`/`save_broker_config()`/
     `load_watchlists()`/`save_watchlists()`，路徑改為顯式參數傳入
     (原本用 `self.config_file`/`self.wl_file`)。

3. **`stock_app_pro.py` 內對應的方法改為薄封裝**：呼叫 `core.xxx`/`data.xxx`，
   自己只負責「從 `self` 讀值 → 呼叫純函式 → 把結果寫回 `self` 或印日誌」。
   類別對外的方法名稱與呼叫方式完全不變 (`self.get_tick(price)` 還是一樣
   呼叫)，因此**這次重構不影響檔案裡任何其他呼叫這些方法的地方，不需要
   額外修改呼叫端**。

4. **新增 `tests/test_core.py`**：使用 Python 標準庫 `unittest`，
   不需要額外安裝 pytest，涵蓋：
   - `get_tick`/`fmt_price`：ETF/一般股票的價格帶邊界值、期貨固定 1 點、
     symbol 後綴 (`.TW`/`.TWO`) 正確剝離、無效輸入回傳 `"--"`。
   - `resample_future_session`：**直接重用 ADR-007 驗證用的模擬資料**，
     斷言 7/10 交易日收盤必須是日盤的 46281、不能被夜盤污染；夜盤正確
     歸屬到下一交易日；空 DataFrame 與週K聚合的基本情況。
   - `validate_stock_order`/`is_daytrade_eligible`：零股類/盤後定價的每一條
     拒絕規則各自獨立測試 (市價/融資融券/非ROD/數量超界/非數字)，
     整股模式無限制、當沖資格的五種組合。
   - `calculate_indicators`：SMA 計算結果與 `pandas.rolling().mean()` 直接
     數值比對；未開啟的 MA 不應該出現對應欄位；BB 開啟時對應欄位齊全；
     MACD/RSI/KDJ/DMI 全部計算無例外且 RSI 落在合理範圍；週期欄位打錯字
     時只跳過該指標、不拋例外 (驗證前述「刻意保留」的行為)。
   - `config_store`：設定檔與自選股清單的讀寫 round-trip、檔案不存在時
     回傳正確的預設值。

   **這份測試已經在這次工作階段實際執行，30 個測試全數通過**
   (`python tests/test_core.py`，執行時間約 0.03~0.05 秒)。

5. **額外驗證 (不只是「測試通過」，而是「重構前後行為等價」)**：
   - 手抄一份重構前的 `calculate_custom_indicators` 原始邏輯當作 golden
     reference，餵同一組 300 筆模擬 K 線資料 (6 條 MA + BB + MACD + RSI +
     KDJ + DMI 全開)，用 `pandas.testing.assert_frame_equal` 逐欄位逐列
     比對重構前後的輸出，**結果完全一致，零誤差**。
   - 對 `get_tick` 用重構前的邏輯當 golden reference，跑 5000 組隨機
     `(asset_type, symbol, price)` 組合比對，**零不一致**。
   - AST 靜態掃描整個 `StockTradingAppPro` 類別，確認所有 `self.xxx`
     屬性/方法引用都能追溯到明確定義來源，重構後依然 0 筆可疑引用；
     並確認五個新模組 (`tick_rules`/`core_indicators`/`futures_session`/
     `order_rules`/`config_store`) 都有被實際呼叫到，沒有「抽出來但沒接上」
     的遺漏。

6. **順手清理**：`stock_app_pro.py` 原本 import 的 `json`、`os` 兩個模組，
   邏輯搬到 `data/config_store.py` 之後在主檔案裡完全沒有其他地方使用，
   一併移除。

### 替代方案 (已考慮但未採用)

- **這次順便把 GUI 也拆成多個檔案 (例如 order_panel.py / chart_panel.py /
  watchlist_panel.py)**：
  被否決 (暫緩，不是永久放棄)。這個工作環境沒有 tkinter 也沒有顯示器，
  沒辦法在拆完後實際開窗驗證行為一致；GUI 深度依賴 widget 建構順序、
  事件綁定、`self.after()` 時序，搬動風險遠高於這次抽出的純邏輯層，
  且錯了不容易被靜態分析抓到 (需要真的跑起來看畫面)。列為「第二階段」
  路線圖 (見下方)，等使用者在有畫面的機器上能配合驗證時再進行。

- **用 dataclass 封裝所有指標參數，取代目前一長串位置參數**：
  暫不採用。目前 `calculate_indicators()` 參數雖多，但都是原本
  `self.xxx.get()` 的直接對應，改成 dataclass 會讓這次重構的 diff
  範圍變大、也增加了一層需要驗證的轉換邏輯。等到有更多指標要加、
  參數列表真的變得難以維護時，再開一筆 ADR 評估用 dataclass 或
  TypedDict 封裝。

- **把 core 函式的「拒絕理由」訊息也做成 i18n/常數表**：
  暫不採用，目前只有繁體中文一種語言需求，做 i18n 是過度工程，
  之後真的需要多語系時再處理。

### 後果 / 影響

- **正面**：
  - 專案第一次有真正「零 tkinter、零 shioaji、零網路」依賴、可以在任何
    機器 (包括 CI) 直接跑的測試套件，且已確認 30 個測試全數通過。
  - 往後新增或修改技術指標、tick 規則、期貨聚合邏輯、下單規則時，
    可以先在 `core/` 裡改完、跑 `tests/test_core.py` 確認邏輯正確，
    再考慮要不要動 GUI 那層，兩件事可以分開驗證，降低「改一個地方
    要擔心牽動別的地方」的風險 (使用者提出這次調整的原始動機)。
  - `stock_app_pro.py` 減少約 100 行的重複計算邏輯，剩下的內容更聚焦在
    「這是 GUI 元件、這是事件處理、這是跟 shioaji/tkinter 互動的膠水程式碼」，
    職責更單純。
  - 這次抽出的每一塊都用 golden-reference 數值比對或大量隨機案例驗證過
    「重構前後行為完全一致」，不是只有「語法能過、測試自己寫自己過」
    這種較弱的保證。

- **需要使用者配合驗證的部分**：
  - 這次修改雖然有前後行為等價的驗證，但終究沒有在真實 tkinter 視窗裡
    跑過；請在有畫面的機器上開啟 App，正常操作一輪 (查股票、切K線、
    開技術指標、下單面板各種交易別、期貨 K 線)，確認顯示與行為跟
    重構前一致。
  - 依專案規則，請執行 `python tests/test_core.py` 確認 30 個測試在你的
    環境也全數通過 (需要 pandas/numpy，不需要 tkinter/shioaji)。

- **待補 (第二階段路線圖，這次不做，供之後排入)**：
  1. 把 `create_widgets()` 拆成幾個各自獨立的建構函式或 Mixin
     (例如下單面板、圖表面板、自選股面板)，讓 `StockTradingAppPro`
     本體只負責組裝，不是一個 700 行的巨型方法。
  2. 把 `fetch_data_worker`/`fetch_market_indices_worker` 這類「網路 I/O +
     排回 UI」的邏輯，抽成一個不依賴 tkinter 的 `BrokerClient`/
     `MarketDataClient` 類別，GUI 層只負責呼叫並把結果透過 callback
     排進 `self.after()`，讓資料抓取邏輯也能離線測試 (目前受限於
     `self.after`/`self.log_message` 呼叫穿插其中，這次沒有動)。
  3. 上述兩項動工前，都需要先在有畫面的環境實際驗證，不建議在只能
     跑 headless 測試的環境進行，風險太高。

### 相關程式位置

- 新增：`core/__init__.py`、`core/tick_rules.py`、`core/indicators.py`、
  `core/futures_session.py`、`core/order_rules.py`
- 新增：`data/__init__.py`、`data/config_store.py`
- 新增：`tests/test_core.py`
- 修改 (`stock_app_pro.py`)：`get_tick`/`fmt_price`/`calculate_custom_indicators`/
  `_resample_future_session`/`load_config`/`save_config`/`load_watchlists`/
  `save_watchlists`/`execute_order` 內的驗證區塊，全部改為呼叫上述新模組的
  薄封裝；移除不再使用的 `json`/`os` import。

---

## ADR-010：修正啟動崩潰 — `set_trade_mode()` 在 `log_txt` 建立前就被呼叫

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用模擬 tkinter 環境實際重現並驗證修正)
- **對應 shioaji 版本**：1.5.6（本次修正與 shioaji 無關，純屬 tkinter widget 建構順序問題）

### 背景

使用者回報：執行 `stock_app_pro.py` 後，畫面只剩下單面板，原本的 K 線圖
與系統日誌區塊都消失了，而且無法登入券商 API。從截圖看：`main_pane`
(左右分割的 PanedWindow) 只剩左側面板內容，且該內容被拉伸填滿整個視窗
寬度、右側 K 線圖區域完全不存在——這是 tkinter 應用「建構到一半就
崩潰」的典型畫面 (視窗殘留部分畫面，但底層 Python 進程可能已經崩潰，
所以按任何按鈕都沒反應，包含登入按鈕)。

這個工作環境沒有安裝 tkinter、也沒有顯示器 (且網路關閉裝不了)，
沒辦法直接開真的視窗重現。改用以下方式在無畫面環境重現並定位問題：

1. **寫一個假的 `tkinter` 模組**，把所有 widget 換成空殼實作 (`.pack()`/
   `.grid()`/`.config()`/`.bind()` 等呼叫變成 no-op)，但 `StringVar`/
   `BooleanVar` 保留真實的 `get()`/`set()` 行為，`Entry`/`Text`/`Listbox`
   也做了忠實模擬 `insert()`/`delete()`/`get()` 的假類別 (因為
   `step_price()`/`execute_order()` 等函式會直接對這些回傳值呼叫字串方法，
   太陽春的 dummy 會產生假警報)。
2. 把 `sys.modules['tkinter']` 換成這個假模組，`matplotlib.backends.backend_tkagg`
   換成假的 `FigureCanvasTkAgg`，`yfinance`/`mplfinance` (網路關閉也裝不了)
   換成最小空殼，讓 `stock_app_pro.py` 的 **真實 Python 邏輯** 可以照原本
   的執行順序真的跑一遍。
3. 執行 `StockTradingAppPro()`，讓例外原封不動地噴出來，看 traceback。

第一次執行就重現出來了：

```
File "stock_app_pro.py", line 648, in create_widgets
    self.set_trade_mode("Common")
File "stock_app_pro.py", line 335, in set_trade_mode
    self.log_message(f"【交易別切換】{labels.get(mode, mode)}")
File "stock_app_pro.py", line 1897, in log_message
    self.log_txt.config(state=tk.NORMAL)
AttributeError: 'NoneType'/'function' object has no attribute 'config'
```

**根本原因**：`create_widgets()` 在建完「實盤閃電下單」面板 (交易別/種類/
條件/五檔等) 之後，緊接著呼叫 `self.set_trade_mode("Common")` 做初始狀態
連動；但 `set_trade_mode()` 內部 (當 `user_initiated=True`，也就是預設值)
最後會呼叫 `self.log_message(...)`，而 `log_message()` 需要 `self.log_txt`
(系統日誌文字方塊) 已經存在。問題是 `self.log_txt` 是在**更後面**的
「系統日誌與回報」區塊 (`report_box`) 才建立的——也就是說，程式一啟動、
執行到這一行就會直接 `AttributeError` 崩潰，`report_box`/`log_txt` 與
整個右側 K 線圖區塊 (`right_frame` 及其所有子元件) 都沒有機會被建構，
只留下左側面板內容，且因為 `main_pane` (水平 `PanedWindow`) 裡只有
`left_frame` 一個 pane，被拉伸填滿整個視窗寬度——這正是使用者截圖看到
的畫面。

這個 bug 是在 ADR-008 (下單面板重構) 那次工作階段引入的：新增
`self.set_trade_mode("Common")` 初始化呼叫時，只確認了它依賴的下單面板
widget (交易別/種類/條件按鈕等) 都已經建立，卻沒注意到它會經由
`log_message()` 間接依賴一個**更後面**才建立的 widget。先前的 AST 靜態
分析 (檢查「先讀取後定義」) 只在 `create_widgets()` 內部逐行檢查直接的
`self.xxx` 讀寫順序，**沒有追蹤跨方法呼叫鏈**——也就是沒有模擬
「呼叫 A 函式，A 函式內部呼叫 B 函式，B 函式讀取某個屬性」這種間接依賴，
所以完全沒抓到這個問題。這次是靠真的執行程式碼才抓出來，值得記一筆
教訓：**純靜態掃描對這類跨函式呼叫鏈的順序問題是有盲點的，之後只要
環境許可，能實際執行 (哪怕是用假 tkinter) 就要盡量實際執行，不要只靠
靜態分析當作已經驗證過。**

### 決定

1. **把 `self.set_trade_mode("Common")` 的呼叫，從「下單面板建完就立刻呼叫」
   移到 `create_widgets()` 函式的最尾端** (在 `report_box`/`log_txt`、
   `right_frame`/K線圖區塊等後續所有 widget 都建立完成之後)。這樣不管
   `set_trade_mode()` 未來又新增了依賴哪個 widget 的邏輯，只要那個 widget
   是在 `create_widgets()` 裡建立的，都保證已經存在。

2. **新增一套可重複使用的診斷工具** (`diag_mock_tkinter.py`、
   `diag_interaction_paths.py`，僅供開發除錯用，不隨 App 一起發布)：
   用假 tkinter/matplotlib backend/yfinance/mplfinance 模組，讓
   `StockTradingAppPro` 的建構與常見互動路徑 (四種交易別切換、期貨/股票
   資產類型切換、下單防呆、登入按鈕、五檔點擊、取價快捷、時間週期切換等
   共 20 項) 可以在沒有畫面的環境下實際跑過一遍，及早抓出「呼叫某個函式時
   它依賴的 widget 其實還沒建好」這類跨方法呼叫鏈的問題。這次順便用這套
   工具跑了一輪額外的互動路徑，確認沒有其他類似的地雷 (20 項全數通過)。

### 替代方案 (已考慮但未採用)

- **在 `log_message()` 裡加一個「`self.log_txt` 不存在就跳過」的防呆**：
  被否決。這樣做只是把症狀蓋住，`create_widgets()` 執行順序本身的問題
  沒有解決，之後如果又有其他函式在錯的時間點被呼叫、依賴到還沒建立的
  widget，一樣會用類似的方式默默失敗或產生難以排查的行為，不如直接把
  呼叫順序改對。

- **把 `report_box`/`log_txt` 的建立整個往前搬到 `set_trade_mode` 呼叫之前**：
  也是可行的修法，但需要調整 widget 在畫面上的擺放順序 (log 區塊會變成
  在下單面板「之前」還是要維持「之後」顯示，需要額外調整 `.pack()` 的
  順序參數)，改動範圍比「把初始化呼叫移到最後」更大，風險更高。改呼叫
  順序不影響任何畫面外觀，是更小、更安全的修法。

### 後果 / 影響

- **正面**：App 啟動不再崩潰，K 線圖區塊與系統日誌都能正常顯示；
  「無法登入券商 API」這個症狀，經確認並非獨立的第二個 bug，而是前述
  崩潰導致整個 Python 進程死掉、視窗變成無回應殘影，按任何按鈕都不會有
  反應的連帶症狀——這次修正後應該會一併解決。

- **已完成的驗證**：
  1. 用假 tkinter 環境重現了完全一樣的 `AttributeError` 崩潰。
  2. 修正後，同一個假 tkinter 環境下 `StockTradingAppPro()` 建構成功、
     無例外。
  3. 額外跑了 20 項互動路徑 (四種交易別切換、期貨/股票資產類型切換、
     下單防呆的允許/拒絕路徑、登入按鈕、五檔點擊、取價快捷、時間週期
     切換等)，全數通過，沒有發現其他類似的地雷。
  4. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法。
  5. `tests/test_core.py` (ADR-009 新增的 30 個核心邏輯單元測試) 依然
     全數通過，本次修正沒有影響 `core/`/`data/` 那一層。

- **需要使用者配合驗證的部分 (無法在此環境完成，請在有真實 tkinter 與
  畫面的機器上實測)**：
  - 實際啟動 App，確認視窗版面恢復正常——左側下單面板維持原本寬度、
    右側能看到 K 線圖時間週期按鈕與圖表區域、左下角能看到「系統日誌與
    回報」區塊並且開機訊息有正常顯示。
  - 確認登入券商 API 按鈕點擊後能正常開啟登入對話框 (在裝有 shioaji
    的環境測試；這個工作環境沒有 shioaji，只確認了未安裝時的分支
    `HAS_SJ=False` 不會讓程式崩潰，沒有測試真正登入流程本身)。

- **待補**：這次新增的 `diag_mock_tkinter.py`/`diag_interaction_paths.py`
  是診斷用的一次性工具，不是正式測試套件的一部分，沒有隨程式一起附上；
  如果覺得這種「假 tkinter 跑互動路徑」的做法有價值，可以考慮之後正式化
  成 `tests/test_gui_smoke.py` 之類的煙霧測試，長期保留在專案裡，
  每次改動 GUI 層後都能快速跑一遍抓這類啟動即崩潰的問題，但這需要先
  跟使用者確認是否要花這個工夫維護一套假 tkinter 環境。

### 相關程式位置 (`stock_app_pro.py`)

- `create_widgets()`：`self.set_trade_mode("Common")` 呼叫位置從函式中段
  (五檔區塊之後) 移到函式最尾端 (K線圖區塊之後)。

---

## ADR-011：移除 FinMind、券商登入按鈕移位、台股資料源改為一律 shioaji

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用模擬 tkinter 環境驗證不崩潰)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者提出三項要求：
1. 把「登入券商實盤 API」按鈕從左側「實盤閃電下單」面板，移到頂部大盤
   指數列 (加權指數/櫃買指數所在那一列) 的右側，常駐顯示。
2. 刪除「登入 FinMind (擴充籌碼)」這個功能，不需要了。
3. 往後的資料源政策：**台股 (含 ETF、指數、期貨) 一律使用 shioaji**，
   不再有 yfinance/FinMind 備援；只有美股會自動使用 yfinance
   (shioaji 本來就不支援美股)。

檢視現有程式碼後，確認 FinMind 在這個專案裡有兩個用途：
- 歷史 K 線資料的備援來源 (`fetch_data_worker` 裡，yfinance 抓不到台股資料時
  的第二層備援，以及大盤指數 Volume 的 `Trading_money` 校正)。
- 法人買賣超與資券餘額籌碼資料的**唯一**來源 (`fetch_taiwan_chips`，對應
  「法人」「資券」兩個副圖 checkbox)。

第 3 點政策一旦落實，第一個用途自然被取代 (改成台股只用 shioaji kbars)；
第二個用途沒有替代資料源，且使用者的指示是「不需要了」，故一併移除，
不做成半殘的停用狀態。

### 決定

1. **券商登入按鈕/狀態移到頂部大盤指數列右側**：
   `self.btn_login`/`self.lbl_api_status` 從 `info_box` (左側下單面板)
   搬到 `market_panel` (頂部與加權指數/櫃買指數同一列)，`side=tk.RIGHT`。
   這兩個 widget 物件本身沒有改名，`process_broker_login`/`toggle_login`
   等既有邏輯完全不用改，只是換了掛載的父容器與位置。

2. **FinMind 登入功能整個刪除**：
   - UI：移除 `btn_fm_login`/`lbl_fm_status` 兩個 widget。
   - 邏輯：移除 `toggle_fm_login()`/`open_fm_login_dialog()`/
     `process_fm_login()` 三個方法。
   - 狀態：移除 `self.fm_token`、`self.saved_fm_email`；
     `config_store.load_broker_config()`/`save_broker_config()` 的簽名從
     5 個欄位 (含 `fm_email`) 改為 4 個欄位 (`api_key/secret_key/pid/ca_path`)。
     **讀取舊版設定檔時，多出來的 `fm_email` 欄位會被安靜忽略**(用
     `.get()` 只挑需要的 4 個欄位)，不會因為欄位對不上而讀取失敗；
     這點有寫測試驗證 (`test_broker_config_backward_compatible_with_old_fm_email_field`)。

3. **法人/資券籌碼指標整個移除** (連帶決定，因為其唯一資料源是 FinMind)：
   - 移除 `fetch_taiwan_chips()` 方法。
   - 移除 `var_inst`/`var_margin` 兩個 checkbox 變數與對應 UI。
   - 移除 `draw_chart()` 裡繪製法人 (`Foreign`/`Trust`/`Dealer`) 與資券
     (`MarginBal`/`ShortBal`) 副圖的區塊，以及 hover 顯示裡對應的文字行。
   - 若之後想恢復這兩個指標，需要先找到 shioaji 或其他資料源能提供
     法人買賣超/資券餘額資料，並另開一筆 ADR 記錄新的資料來源，
     不要直接把 FinMind 呼叫加回來。

4. **`fetch_data_worker()` 整段重寫，貫徹「台股一律 shioaji」**：
   - 新增 `is_taiwan_instrument` 判斷 (涵蓋一般股票/ETF、`^TWII`/`^TWOII`
     指數、`TXF`/`MTX`/`FITX`/`MXF` 期貨)。
   - 若 `is_taiwan_instrument` 為真但未登入 shioaji (`not (self.api_logged_in
     and HAS_SJ)`)，**直接記錄明確錯誤訊息並返回**，不再嘗試 yfinance 或
     FinMind，也不會去碰 `self.sj_api` (未登入/未裝 shioaji 時這個屬性
     根本不存在，碰了就是 `AttributeError`)。
   - 移除原本「yfinance 抓台股 → 抓不到再試 FinMind TaiwanStockPrice →
     大盤指數再用 FinMind Trading_money 校正 Volume」這一整條備援鏈。
   - shioaji kbars 的回溯天數 (`sj_days`)，原本依「是否有 YF 備援」分成
     兩組 (較短/較長)，現在 shioaji 是唯一來源，統一採用原本較長的那組
     (`日K 730 天 / 周K 1825 天 / 月K 3650 天`)。
   - 美股 (非 `is_taiwan_instrument`) 分支：`self.asset_type = "us_stock"`，
     直接用 `yfinance` 抓取，不需要登入 shioaji、不受台股邏輯影響。
   - **「還原權息」(`var_adjusted`) 的已知限制**：這個勾選框原本能生效，
     唯一機制是 yfinance 的 `auto_adjust=True` 參數；shioaji kbars 在這個
     專案裡沒有實作還原權息的計算方式。台股既然一律走 shioaji，勾選
     「還原權息」對台股**暫時不會生效**，程式會在日誌明確提示這件事
     (`【提示】「還原權息」目前僅 yfinance 資料源支援...`)，而不是靜默
     忽略讓使用者誤以為已經套用。美股不受影響 (yfinance 的 `auto_adjust`
     依然照常運作)。

5. **`fetch_market_indices_worker()` 移除 YF 備援分支**：加權指數/櫃買指數
   顯示現在也是「未登入就顯示等待連線的初始文字，登入才更新」，不再有
   YF 備援可以在未登入時顯示替代資料。

6. **`start_fetch_thread()` 移除過時的前置擋檢查**：原本「未登入且未開
   YF 備援就整個擋下查詢」的檢查已經不適用 (美股本來就不需要登入)，
   改由 `fetch_data_worker()` 依商品類型 (`is_taiwan_instrument`) 個別判斷
   並給出對應訊息。

7. **移除「備用 YF 報價」手動切換按鈕** (`btn_yf_switch`/`toggle_yf_mode`/
   `use_yf_backup`)：新政策下，台股永遠不會用到 YF、美股永遠自動用 YF，
   這個手動切換已經沒有意義。

8. **順手清理**：`stock_app_pro.py` 的 `requests` import 移除 (原本只有
   FinMind 的兩個 HTTP 呼叫在用，兩個呼叫都已刪除)。

### 替代方案 (已考慮但未採用)

- **保留 FinMind 呼叫但去掉登入介面，改成永遠用匿名 (無 token) 模式呼叫**：
  被否決。使用者的第三點指示「台股一律都用券商API資料數據」語氣明確、
  沒有例外，繼續呼叫 FinMind (即使匿名) 違背這個指示；且匿名模式的
  流量限制更嚴格，容易在使用中途無預警失敗，不如乾脆拿掉。

- **法人/資券 checkbox 保留在畫面上但改成 disabled (灰階)，附註「已停用」**：
  被否決。這樣會讓畫面留著一個看起來像沒做完的功能，使用者已經明確說
  FinMind 不需要了，乾淨移除比留著半殘的 UI 更清楚，之後真的要恢復
  再重新設計。

- **台股未登入時，仍然嘗試用 yfinance 顯示「僅供參考」的資料，並標註
  「非即時/非券商資料」**：被否決。這正是使用者想避免的「安靜地退化成
  其他資料源」，容易讓使用者誤判資料新鮮度或來源可信度，尤其這是一個
  以真實下單為目標的系統，資料源的確定性比「總是有資料可看」更重要。

- **還原權息對台股改成用 shioaji 的股利資料自己算調整係數**：
  超出這次要求的範圍 (使用者只要求資料源政策調整，沒有要求實作新的
  還原權息演算法)。這次先誠實標示「暫不生效」，之後如果使用者需要，
  可以另開一筆 ADR 評估怎麼用 shioaji 的股利/除權息資料自己實作。

### 後果 / 影響

- **正面**：資料源行為變得單純、可預期——台股永遠是券商即時資料，不會
  有「明明有 shioaji 卻不知道為什麼看到 yfinance 資料」這種混淆；
  券商連線狀態常駐在頂部，不受左側面板內容多寡影響隨時看得到；程式碼
  減少約 130 行 (移除多層備援與籌碼資料的邏輯)。

- **已完成的驗證**：
  1. 用 ADR-010 建立的假 tkinter 環境重新跑過 `StockTradingAppPro()` 建構，
     成功無例外。
  2. 額外新增兩項針對本次修改的驗證：台股 (2330) 在未登入 shioaji 時呼叫
     `fetch_data_worker`，確認不會碰觸不存在的 `self.sj_api` 而崩潰；
     美股 (AAPL) 呼叫 `fetch_data_worker` 確認不受台股限定邏輯誤擋。
  3. 用類別命名空間檢查 (`'toggle_fm_login' in StockTradingAppPro.__dict__`
     這類判斷，避免被 mock 的 `__getattr__` 誤判成方法還存在) 明確驗證
     `toggle_fm_login`/`open_fm_login_dialog`/`process_fm_login`/
     `fetch_taiwan_chips`/`toggle_yf_mode` 五個方法確實已從類別移除，
     且 `fm_token`/`use_yf_backup`/`var_inst`/`var_margin` 等屬性確實
     不再存在於實例上。
  4. 額外驗證 `btn_login`/`lbl_api_status` 這兩個「移動位置」而非「刪除」
     的 widget 確實還存在。
  5. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法。
  6. `tests/test_core.py` 更新了 `config_store` 的測試以符合新的 4 欄位
     簽名，並新增一筆「舊設定檔多出 `fm_email` 欄位時仍能正常讀取」的
     回溯相容測試，共 31 個測試全數通過。

- **需要使用者配合驗證的部分 (無法在此環境完成，請在有真實 tkinter、
  shioaji 與畫面的機器上實測)**：
  - 確認頂部大盤指數列右側的登入按鈕位置與外觀符合預期。
  - 用真實 shioaji 帳號登入後，查詢台股 (股票/ETF/期貨/指數) 確認資料
    正常載入；登出或未登入狀態下查詢台股，確認會看到清楚的錯誤訊息
    而不是安靜地顯示舊資料或當機。
  - 查詢美股 (例如 AAPL/NVDA) 確認不需要登入 shioaji 就能正常顯示。
  - 勾選「還原權息」查詢台股個股，確認日誌有出現「暫不生效」的提示，
    且畫面顯示的價格是原始價 (非還原)；查詢美股確認還原權息依然正常
    運作。
  - 若舊的 `broker_config.json` 檔案裡還留著 `fm_email` 欄位，確認程式
    仍能正常讀取帳密資訊 (這點在本環境已用單元測試驗證過欄位會被
    正確忽略，但建議實機也順手確認一次)。

- **待補**：如果之後想恢復法人/資券籌碼指標，需要先確認新的資料來源
  (shioaji 本身是否有相關 API、或其他付費/免費資料源)，並另開一筆 ADR
  記錄選型理由，不要直接把這次刪除的 FinMind 呼叫加回來。

### 相關程式位置 (`stock_app_pro.py` / `data/config_store.py` / `tests/test_core.py`)

- `create_widgets()`：`btn_login`/`lbl_api_status` 移到 `market_panel`；
  移除 `btn_fm_login`/`lbl_fm_status`；移除「法人」「資券」checkbox；
  移除 `btn_yf_switch`。
- 移除方法：`toggle_fm_login`、`open_fm_login_dialog`、`process_fm_login`、
  `fetch_taiwan_chips`、`toggle_yf_mode`。
- `fetch_data_worker()`：整段重寫，貫徹台股一律 shioaji、美股自動 yfinance。
- `fetch_market_indices_worker()`：移除 YF 備援分支。
- `start_fetch_thread()`：移除過時的登入前置檢查。
- `draw_chart()`：移除法人/資券副圖繪製與 hover 顯示文字。
- `load_config()`/`save_config()`：簽名同步移除 `fm_email`。
- `data/config_store.py`：`load_broker_config()`/`save_broker_config()`
  簽名從 5 欄位改為 4 欄位。
- `tests/test_core.py`：更新 `TestConfigStore` 對應新簽名，新增回溯相容測試。

---

## ADR-012：修正關閉視窗後崩潰 — `TclError: invalid command name`

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用模擬 tkinter 環境重現機制並驗證修正)
- **對應 shioaji 版本**：1.5.6（本次修正與 shioaji 無關，純屬 tkinter 背景執行緒生命週期問題）

### 背景

使用者回報執行時出現 `_tkinter.TclError: invalid command name ".!frame.!label2"`。

這是 tkinter 常見錯誤類型：對一個**已經被銷毀**的 widget 呼叫方法 (最常見是
`.config()`)，Tcl 直譯器找不到對應的底層物件，就會噴出「invalid command name」。
從路徑名稱 `.!frame.!label2` 判斷 (tkinter 對同類型 widget 在同一個父容器下
會依序命名為 `.!label`、`.!label2`、`.!label3`...)，這對應到 `market_panel`
底下**第二個** `Label`——依 ADR-011 把券商登入按鈕/狀態移到這個位置後的
建立順序是：`lbl_api_status` (第1個 label) → `btn_login` (按鈕，不算)
→ `lbl_twii` (第2個 label) → `lbl_twoii` (第3個 label)，因此高度懷疑是
`self.lbl_twii` (加權指數顯示)。

檢視程式碼確認根本原因：

1. `fetch_market_indices_worker()` 與 `fetch_realtime_worker()` 都是
   `daemon=True` 的背景執行緒，內部是 `while True: ... time.sleep(30)` /
   `time.sleep(0.5)` 的**永久迴圈**，從程式啟動就開始跑，直到整個 Python
   行程結束才會停止。
2. 這兩條執行緒會透過 `self.after(0, lambda: self.lbl_twii.config(...))`
   這類呼叫，把 GUI 更新排入 tkinter 的事件佇列。
3. **整個程式完全沒有處理視窗關閉時的收尾**：沒有綁定
   `protocol("WM_DELETE_WINDOW", ...)`，使用者點右上角 X 關閉視窗時，
   tkinter 預設行為會銷毀視窗與所有 widget，但這兩條背景執行緒對此一無所知，
   還是會繼續執行迴圈、繼續呼叫 `self.after(...)` 想更新已經不存在的 widget。
4. 當背景執行緒在視窗銷毀之後 (或銷毀前一刻，事件佇列還沒清空時) 排入的
   `self.after()` callback 真正被 Tcl 處理時，就會對著一個已經不存在的
   widget 路徑送命令，噴出 `invalid command name`。

這個 bug 本質上跟這次的 ADR-011 改動沒有直接關係——`fetch_market_indices_worker`
背景執行緒與缺乏關閉處理，是這個專案從很早期就存在的設計，只是這次移動
`lbl_twii`/`lbl_twoii` 到新位置後，使用者實際操作到「關閉視窗」這個路徑，
才第一次把這個潛在問題暴露出來並回報。

### 決定

1. **新增 `self._closing` 旗標**，在 `__init__` 一開始就設為 `False`。

2. **綁定 `self.protocol("WM_DELETE_WINDOW", self.on_app_close)`**，新增
   `on_app_close()` 方法：先把 `self._closing` 設成 `True`，再呼叫
   `self.destroy()` 真正關閉視窗。不等待背景執行緒結束 (它們是 daemon
   thread，本來就不會阻擋行程退出)，只是盡量縮小它們在視窗銷毀後還嘗試
   更新 widget 的機率。

3. **新增集中式的 `self.safe_after(delay, func, *args)`**，取代所有原本
   直接呼叫 `self.after(...)` 的地方 (全檔案共 33 處呼叫全部替換)。
   做兩層防護：
   - 排程前檢查 `self._closing`，是的話直接不排程。
   - 排程進去的 callback 真正執行時，再檢查一次 `self._closing`，並把
     實際呼叫包在 `try/except tk.TclError` 裡。
   
   兩層都做的原因：單靠「排程前檢查」仍有極窄的競態窗口——執行緒檢查
   當下 `_closing` 還是 `False`，但排入佇列後、Tcl 真正處理這個 callback
   之前，使用者恰好關閉了視窗。這種情況下，「執行時再檢查一次」加上
   `try/except TclError` 就是最後一道防線。

4. **`fetch_market_indices_worker()`/`fetch_realtime_worker()` 的迴圈開頭
   加上 `if self._closing: return`**，讓背景執行緒在視窗關閉後盡快自然
   結束，而不是繼續空轉直到行程退出。

### 替代方案 (已考慮但未採用)

- **只在兩個 worker 迴圈加 `_closing` 檢查，不做 `safe_after` 集中包裝**：
  被否決。迴圈開頭的檢查只能防住「下一輪迴圈」，防不住「這一輪已經在
  處理中、即將呼叫 `self.after()`」的情況，也防不住已經排入佇列、
  即將被 Tcl 執行的 callback。只有在「callback 真正執行的那一刻」
  也做防護，才能完整堵住這個競態視窗。

- **在每個呼叫端各自加 `try/except TclError`，不做集中包裝**：
  被否決。全檔案有 33 處 `self.after()` 呼叫，分散加 try/except 容易漏掉，
  且往後新增功能時很容易忘記要包這一層。集中成 `safe_after()` 之後，
  之後所有新增的排程呼叫只要用 `self.safe_after(...)` 就自動獲得保護，
  不需要每個開發者/每次修改都想著要包例外處理。

- **等待背景執行緒真正結束才關閉視窗 (`thread.join()`)**：
  被否決。`fetch_realtime_worker` 內部有 `time.sleep(0.5)`、
  `fetch_market_indices_worker` 有 `time.sleep(30)`，用 `join()` 等待
  會讓使用者點擊關閉視窗後卡住最多 30 秒才真正關閉，體驗很差。
  兩層防護的 `safe_after` 已經足以避免崩潰，不需要用犧牲關閉速度的方式
  換取「完全沒有殘留執行緒」這種非必要的保證。

### 後果 / 影響

- **正面**：使用者關閉視窗後，背景執行緒不會再噴出未捕捉的
  `TclError: invalid command name` 例外；`safe_after()` 是集中式包裝，
  之後新增任何背景執行緒或排程更新，只要沿用這個方法呼叫就自動獲得
  同樣的保護，不需要每次都重新設計防護邏輯。

- **已完成的驗證**：
  1. 用 ADR-010 建立的假 tkinter 環境，新增了 5 項專門針對 `safe_after`
     機制本身的測試 (不只是「不崩潰」，而是直接模擬真實的競態情境)：
     - 視窗開啟時，`safe_after` 正常排程。
     - `_closing=True` 時，`safe_after` 不會再排程。
     - **直接讓排程進去的 callback 真正拋出 `TclError`**，驗證
       wrapped callback 會把它吞下、不會往外傳播。
     - 模擬「排程當下視窗還開著，但執行前視窗被關閉」的競態情境，
       驗證 wrapped callback 執行時真的會再檢查一次 `_closing` 並跳過。
     - 驗證 `on_app_close()` 確實會把 `_closing` 設成 `True`。
     以上連同先前所有互動路徑測試，共 29 項全數通過。
  2. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法 (這次也把 `protocol`
     加入 tkinter 內建方法白名單，避免誤判)。
  3. `tests/test_core.py` (`core/`/`data/` 層) 不受本次修改影響，
     31 個測試依然全數通過。
  4. 用文字取代時不慎誤換了兩處程式註解裡的文字說明 (把註解裡描述
     "self.after(...)" 的地方也錯換成 "self.safe_after(...)"，讀起來
     語意不通)，已人工檢查並修正回正確的文字，避免文件性質的程式碼
     (docstring/comment) 品質下降。

- **需要使用者配合驗證的部分 (無法在此環境完成，請在有真實 tkinter 與
  畫面的機器上實測)**：
  - 正常啟動 App，操作一輪後**點右上角 X 關閉視窗**，確認終端機/主控台
    不再出現 `TclError: invalid command name` 或其他未捕捉的例外，
    視窗能乾淨地關閉。
  - 特別測試「剛登入券商 API、大盤指數正在更新的當下立刻關閉視窗」
    這種時間點，這是最容易命中競態窗口的情境。
  - 確認關閉視窗的反應速度沒有變慢 (不應該有任何等待或卡頓)。

- **待補**：這次只處理「視窗關閉」這個生命週期事件；如果之後有其他
  會讓 widget 提前被銷毀的操作 (例如將來若把某些面板做成可以動態關閉/
  重建的 Toplevel 視窗)，需要比照這個模式，在對應的關閉事件也检查
  `self._closing` 或建立類似的區域性旗標，不要只依賴這次的全域旗標。

### 相關程式位置 (`stock_app_pro.py`)

- `__init__`：新增 `self._closing = False`、
  `self.protocol("WM_DELETE_WINDOW", self.on_app_close)`。
- 新增方法：`on_app_close()`、`safe_after()`。
- `fetch_market_indices_worker()`/`fetch_realtime_worker()`：迴圈開頭
  加上 `_closing` 檢查。
- 全檔案 33 處 `self.after(...)` 呼叫全部改為 `self.safe_after(...)`。

---

## ADR-013：版面重排、下單確認視窗、委託數量上限

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用模擬 tkinter 環境驗證不崩潰)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者提供一張目前執行畫面的截圖，手繪標註了想調整的位置，並列出 7 項要求：

1. 版面依圖片配置調整。
2. 下單後要有確認視窗，確認後才真正送出委託。
3. 委託數量要有上限：整股/盤後定價最高 499 張、零股/盤後零股最高 999 股，
   系統預設都是 1。
4. 「實盤閃電下單」改名為「實盤下單」。
5. 詢問「成交明細」是做什麼用的。
6. 「系統日誌與回報」移到 K 線圖最下面，並且要能上下捲動看之前的訊息。
7. 五檔委買賣的位置要置中。

比對截圖與手繪標註後確認：這張圖是目前實際執行畫面的截圖 (不是全新設計稿)，
標註主要指向三處：五檔區塊畫圈寫「置中」(對應第7項)；成交明細旁畫問號
(對應第5項的提問)；從五檔位置畫一個大方框延伸到畫面最下方、圈住
「系統日誌與回報」(對應第6項，畫出目的地是整個畫面最底部)。

### 決定

1. **第5項回答 (非程式異動)**：「成交明細」是逐筆成交跳動列表 (Time &
   Sales)，跟五檔是兩回事——五檔顯示「還沒成交、掛著等撮合」的委買委賣量；
   成交明細顯示「已經真的成交」的每一筆交易 (時間/成交價/與參考價的漲跌/
   成交量)，最新排最上面，保留最近 20 筆，紅漲綠跌上色，整股/零股分開
   累積。用途是看短線撮合節奏與力道，跟五檔的委託掛單資訊互補。

2. **「實盤閃電下單」→「實盤下單」**：`info_box` 的 `LabelFrame` 標題文字
   直接改掉，相關程式註解也一併更新用詞。

3. **五檔委買賣置中**：`five_level_frame.pack(fill=tk.X, pady=2)` 改為
   `five_level_frame.pack(pady=2)`——拿掉 `fill=tk.X` 後，frame 縮回其
   grid 內容的自然寬度，並吃到 `pack()` 預設的 `anchor='center'`，
   在 `info_box` 裡水平置中，而不是被拉伸貼齊左邊。

4. **系統日誌與回報移到 K 線圖下方，加捲軸**：
   - 從 `left_frame` 底部整個移除 (原本的 `report_box`)。
   - 在 `self.right_frame` 裡新增一個固定高度 (130px) 的區塊，用
     `side=tk.BOTTOM` 卡在 K 線圖區域最下方；用 `pack_propagate(False)`
     固定高度，不被內容撐開或壓縮。
   - `self.log_txt` (原本的 `tk.Text`) 加上 `tk.Scrollbar` (垂直方向)，
     `yscrollcommand`/`command=self.log_txt.yview` 互相綁定，可以上下
     捲動看之前的訊息，不再只能看最新那幾行。
   - `log_message()` 本身完全沒改 (還是 `insert` + `see(tk.END)` 自動
     捲到最新)，只是外層容器換了位置跟加了捲軸。

5. **委託數量上限 (整股/盤後定價 499 張、零股類 999 股)**：
   - `core/order_rules.py` 的 `validate_stock_order()` 新增數量範圍檢查，
     依 `is_lot_restricted` 決定上限是 `MAX_QTY_ODD=999` 股還是
     `MAX_QTY_LOT=499` 張，兩個常數都定義在模組層級方便之後調整。
     這條檢查在零股類/盤後定價原有的其他規則檢查**之前**先做，任何模式
     的委託都會先過一次數量檢查。
   - `step_qty()` 的 +/- 按鈕同步套用這個上限 (原本整股類是「無上限，
     只擋最小值 1」，現在改成「上限 499」)。
   - **明確這是本系統自訂的保守防呆上限，不是交易所規則本身**——零股的
     1~999 股上限才是真正的交易所規則 (股數達到 1000 就該用整股下單)；
     499 張這個數字是為了避免打錯數字誤送巨量委託，如果之後要調整這個
     上限，直接改 `core/order_rules.py` 的 `MAX_QTY_LOT` 常數即可，
     但建議先確認過使用者真的要調整，因為這牽涉到防呆機制的鬆緊。
   - 期貨的數量目前只擋「必須是正整數」，沒有設本系統自訂上限——期貨
     一口的保證金與風險跟股票張數不是同一個量級概念，沒有比照套用
     499 這個數字的道理；如果之後想幫期貨也設上限，需要先確認合理的
     數字，不要直接沿用股票的 499。

6. **下單前彈出確認視窗，確認後才真正送出委託**：
   - `execute_order()` 原本「驗證 → 組裝 shioaji Order 物件 → 呼叫
     place_order()」一路到底的流程，改成「驗證 → 組裝 Order 物件 →
     打包成 `confirm_ctx` → 呼叫 `_show_order_confirmation(confirm_ctx)`」，
     實際送出的動作抽成新方法 `_confirm_and_place_order(ctx)`。
   - `_show_order_confirmation()` 開一個 `Toplevel` 對話框，逐行列出：
     商品代碼與名稱、買賣方向 (紅買綠賣，跟買進/賣出按鈕配色一致)、
     交易別 (整股/盤中零股/盤後定價/盤後零股，期貨顯示「期貨」)、
     種類 (現股/融資/融券，期貨不顯示)、條件 (ROD/IOC/FOK)、類別
     (限價/市價)、數量、價格、若有勾當沖則額外顯示「當沖:是」。
   - 「確認送出」按鈕關閉對話框並呼叫 `_confirm_and_place_order(ctx)`
     (這裡才真的呼叫 `self.sj_api.place_order(...)` 並記錄結果日誌，
     邏輯跟原本 `execute_order()` 尾段完全相同，只是搬過來)；
     「取消」按鈕只關閉對話框並記一筆「【已取消下單】...使用者取消，
     未送出委託」的日誌，不會呼叫 `place_order()`。

### 替代方案 (已考慮但未採用)

- **確認視窗用 `messagebox.askyesno()` 這種內建的簡單對話框，不自己刻
  `Toplevel`**：被否決。內建對話框只能顯示一段純文字，沒辦法排版成
  一行一行的欄位對照 (商品/買賣/交易別/數量/價格...)，可讀性遠不如
  自訂的逐行版面，尤其是要一眼看出「買賣方向」跟「當沖」這種關鍵風險
  欄位時，顏色與排版的重要性比省事更高。

- **委託數量上限做成可以在設定裡調整的參數，而不是寫死常數**：
  暫不採用。使用者這次給的是明確數字 (499/999)，先滿足這個需求；
  如果之後真的需要依帳戶/商品別調整上限，可以另開 ADR 討論要不要做成
  可設定項目，避免這次順便加一個使用者沒要求的設定介面。

- **系統日誌區塊的高度做成可以讓使用者自己拖曳調整**：
  被否決 (至少這次不做)。tkinter 要做「使用者可拖曳調整兩個 pack 區塊
  分界」通常需要換成 `PanedWindow` 或自己刻拖曳邏輯，比固定高度加捲軸
  複雜得多；使用者只要求「可以捲動看之前的訊息」，捲軸已經滿足這個
  需求，可調整高度是更進一步的功能，之後如果有需要再談。

### 後果 / 影響

- **正面**：下單前多一道確認關卡，降低手滑誤按買進/賣出或看錯欄位的
  風險；委託數量有上限防呆，不會因為打錯一個零而誤送出去；系統日誌
  移到下方並可捲動，之前的訊息不會因為區塊太小而看不到；五檔置中、
  面板改名，畫面更貼近使用者想要的配置。

- **已完成的驗證**：
  1. 用 ADR-010/012 建立的假 tkinter 環境重新跑過 `StockTradingAppPro()`
     建構，成功無例外 (驗證版面搬動後 widget 建構順序沒有問題)。
  2. 新增兩項測試直接驗證確認視窗流程本身：`_show_order_confirmation()`
     用假資料建構視窗不崩潰；`_confirm_and_place_order()` 在
     `HAS_SJ=False` (沒有 `self.sj_api`) 的情況下，呼叫會被內部
     `try/except` 接住記成【下單異常】日誌，不會讓 `AttributeError`
     往外傳播炸掉整個程式。連同先前所有測試，共 31 項全數通過。
  3. `core/order_rules.py` 新增 5 個測試：整股模式數量上限 499 張的
     邊界值 (剛好 499 通過、500 拒絕、0 或負數拒絕)、盤後定價同樣受
     499 上限限制；連同既有測試共 35 個測試全數通過。
  4. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法。

- **需要使用者配合驗證的部分 (無法在此環境完成，請在有真實 tkinter 與
  畫面的機器上實測)**：
  - 確認系統日誌區塊確實出現在 K 線圖下方、高度合理、捲軸能正常上下
    捲動看到之前的訊息。
  - 確認五檔委買賣區塊在畫面上看起來是置中的，不是貼左。
  - 用**模擬帳號**實際點擊買進/賣出，確認會先跳出確認視窗且欄位資訊
    正確，按「取消」不會送出委託、按「確認送出」才會真正呼叫
    shioaji 下單。
  - 測試數量欄位直接打字輸入超過上限的數字 (例如整股輸入 500)，確認
    送出時會被擋下並顯示對應的錯誤訊息。
  - 確認「實盤下單」新名稱、五檔置中、日誌捲動等版面調整整體視覺上
    符合預期，必要時可以再回饋微調 (例如日誌區塊的高度)。

- **待補**：這次沒有處理「系統日誌區塊高度可由使用者拖曳調整」這種
  更進階的版面彈性，如果之後有需要可以再討論怎麼做 (例如換成
  `PanedWindow` 垂直分割右側區域)。

### 相關程式位置 (`stock_app_pro.py` / `core/order_rules.py` / `tests/test_core.py`)

- `create_widgets()`：`info_box` 標題改名；`five_level_frame` 置中；
  系統日誌區塊從 `left_frame` 移到 `right_frame` 底部並加捲軸。
- `step_qty()`：整股/盤後定價上限改為 499。
- `execute_order()`：拆分為「驗證+組裝」與「確認視窗」兩段，尾段送單
  邏輯移到新方法。
- 新增方法：`_show_order_confirmation()`、`_confirm_and_place_order()`。
- `core/order_rules.py`：`validate_stock_order()` 新增數量上限檢查，
  新增 `MAX_QTY_LOT`/`MAX_QTY_ODD` 常數。
- `tests/test_core.py`：新增/更新 `TestOrderRules` 對應數量上限的測試案例。

---

## ADR-014：修正關閉視窗後終端機卡住 — 補上 shioaji 登出與強制結束行程

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用模擬 tkinter 環境驗證登出呼叫與強制結束行為)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者從 Anaconda Prompt (`stock_env` 環境) 執行 `stock_app_pro.py`，回報
「關閉程式後，沒有辦法跳回 `(stock_env) G:\StockBuild>`」——也就是說，
視窗關閉之後，終端機沒有回到可以輸入指令的提示字元，代表 Python 行程
根本沒有真正結束，還在背景卡著。

這跟 ADR-012 處理的 `TclError: invalid command name` 崩潰問題**表面上是同一次
「關閉視窗」動作觸發的，但其實是兩個不同層次的問題**：ADR-012 修的是「背景
daemon thread 想更新已銷毀的 widget 而噴例外」；這次的問題是「Python 行程
本身沒有真正退出」。兩者關聯是：修好 ADR-012 之前，視窗關閉後那個
`TclError` 例外沒有被捕捉，會讓程式帶著錯誤訊息**意外崩潰**，而崩潰通常會
連帶把整個行程弄死，順便讓終端機跳回提示字元——這其實是「用一個問題
（崩潰）掩蓋另一個問題（行程真的關不掉）」。ADR-012 把崩潰修好之後，
程式不再意外死掉，這個原本被蓋住的「行程關不掉」問題才會浮現出來，
使用者才第一次觀察到「終端機跳不回提示字元」這個現象。

檢視程式碼確認根本原因：

1. `self.on_app_close()` (ADR-012 新增) 只有把 `_closing` 設成 `True` 再呼叫
   `self.destroy()`，從來沒有呼叫 `self.sj_api.logout()`。
2. `shioaji.Shioaji()` 內部維護一條 WebSocket 連線，從截圖的終端機輸出可以
   看到連線建立時的 `Response Code: 0 | Event Code: 0 | Info: host ...`、
   `Session up`、`Session Property modification ok` 等訊息，代表這是一個
   持續運作的連線，背後很可能有自己的執行緒在處理封包收送、心跳等工作。
3. 這些 shioaji 內部的執行緒**不是我們自己用 `threading.Thread(daemon=True)`
   開出來的**，我們沒有原始碼、無法確認它們是不是 daemon thread。Python
   的行程結束機制是「等所有非 daemon 執行緒都結束才真正退出」，如果
   shioaji 內部有任何非 daemon 執行緒在連線建立後持續運作，且從未被
   明確登出/關閉，整個 Python 行程就會卡住，永遠不會自然結束。
4. `toggle_login()` (使用者手動點「登出券商API」按鈕時) 原本就有呼叫
   `self.sj_api.logout()`，但這條路徑只有在使用者「先登出、再關視窗」
   時才會走到；如果使用者是「登入著就直接關視窗」(截圖裡的操作方式)，
   就完全沒有機會呼叫到 `logout()`。

### 決定

1. **`on_app_close()` 補上嘗試呼叫 `self.sj_api.logout()`**：只有在
   `HAS_SJ` 為真且目前 `self.api_logged_in` 為真時才呼叫，包在
   `try/except` 裡避免 `logout()` 本身出錯連帶讓關閉流程卡住。這是
   「盡量做對的事」——給 shioaji 機會正常釋放連線與內部資源，而不是
   完全不告而別。

2. **在 `self.destroy()` 之後，用 `os._exit(0)` 保底強制結束整個行程**：
   即使呼叫了 `logout()`，我們仍然無法保證 shioaji 內部所有執行緒會在
   合理時間內自行結束 (這是我們看不到原始碼、無法控制的第三方套件行為，
   也可能因為版本不同而有差異)。與其讓使用者每次關閉視窗都要賭
   shioaji 會不會乖乖把執行緒收乾淨，不如在做完「該做的收尾」
   (呼叫 logout) 之後，直接用 `os._exit(0)` 強制結束整個 Python 行程，
   保證「關視窗 = 程式真的結束、終端機一定跳回提示字元」這個使用者
   合理預期的行為。

3. **`self.destroy()` 也包一層 `try/except`**：避免它本身丟出例外時
   讓後面的 `os._exit(0)` 沒機會執行到，確保無論如何最終都會走到
   強制結束這一步。

4. **重新加回 `import os`**：這個 import 在 ADR-011 因為當時全檔案沒有
   任何地方用到 `os.xxx` 而被移除，這次因為 `os._exit()` 又需要用到，
   加回來。

### 替代方案 (已考慮但未採用)

- **只呼叫 `logout()`，不強制 `os._exit(0)`，賭 shioaji 執行緒都是 daemon**：
  被否決。這正是目前問題的成因——我們沒有把握，賭錯了使用者就會再次
  遇到「關視窗但行程關不掉」。`os._exit(0)` 是很小的程式碼成本，換來
  「保證關得掉」這個確定性，值得。

- **不呼叫 `logout()`，只靠 `os._exit(0)` 強制結束**：被否決。雖然
  `os._exit(0)` 確實能保證行程結束，但完全跳過 `logout()` 等於每次
  關閉視窗都是「斷線而非正常登出」，對券商那端的連線階段(session)
  管理不是好習慣 (可能留下未正常關閉的 session 記錄)，能做的收尾
  還是要先做，`os._exit()` 只當最後一道保底手段，不是取代正常收尾
  流程的理由。

- **用 `thread.join(timeout=...)` 等待 shioaji 內部執行緒結束**：
  不可行——我們沒有 shioaji 內部執行緒的物件參照 (它們是 shioaji
  套件自己管理的，不是我們 `threading.Thread(...)` 建立出來、
  存在 `self.xxx` 上的執行緒物件)，沒有東西可以 `join()`。

### 後果 / 影響

- **正面**：使用者關閉視窗後，終端機會確實跳回命令提示字元，不會再
  卡住；關閉前會先嘗試正常登出券商連線，比完全不告而別更妥當。

- **已完成的驗證**：
  1. 用假 tkinter 環境新增兩項測試 (呼叫真正的 `os._exit()` 會殺掉
     測試腳本自己的行程，所以測試時暫時把 `stock_app_pro.os._exit`
     換成假函式，測完再還原)：
     - 驗證 `on_app_close()` 執行後 `_closing` 確實被設成 `True`，
       且 `os._exit()` 確實被呼叫到 (而不是漏掉這一步)。
     - 驗證已登入狀態下呼叫 `on_app_close()`，`self.sj_api.logout()`
       確實被呼叫到 (用假的 `sj_api` 物件確認 `logout()` 方法真的
       被觸發)。
     連同先前所有測試，共 32 項全數通過，且測試腳本本身沒有被意外
     終止 (證明 mock 替換 `os._exit` 生效，後續測試案例仍正常執行)。
  2. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法。
  3. `tests/test_core.py` (`core/`/`data/` 層) 不受本次修改影響，
     35 個測試依然全數通過。

- **需要使用者配合驗證的部分 (無法在此環境完成，因為這裡沒有真正的
  shioaji 連線與終端機環境)**：
  - 用真實 shioaji 帳號登入後，直接關閉視窗 (不要先按登出)，確認
    終端機這次會正常跳回 `(stock_env) G:\StockBuild>` 提示字元。
  - 確認關閉視窗的反應時間沒有變得很慢 (`logout()` 呼叫本身有一次
    網路往返，理論上應該很快，但建議實測確認沒有明顯卡頓)。
  - 若之後還是遇到關閉視窗後終端機卡住的情況，請把當下的終端機畫面
    截圖回報，這樣才能判斷是不是 shioaji 版本更新後行為有變化，
    或是有其他我們還沒發現的執行緒來源。

- **待補**：`os._exit(0)` 是相對激烈的手段，如果之後 shioaji 官方
  文件或 release notes 明確說明有提供更完整的 `disconnect()`/
  `shutdown()` 之類的 API 可以確實停止所有內部執行緒，可以考慮
  改用官方建議的方式並拿掉 `os._exit()` 這道保底，但在那之前，
  這道保底不建議拿掉。

### 相關程式位置 (`stock_app_pro.py`)

- 檔案開頭：重新加回 `import os`。
- `on_app_close()`：新增 `self.sj_api.logout()` 呼叫 (包 try/except)，
  結尾新增 `os._exit(0)` 強制結束行程。

---

## ADR-015：版面優化 (圖表填滿版面、日期縮小、換股殘留資訊清除)、量價欄位重排、現沖 checkbox 系統自動判斷

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用模擬 tkinter 環境驗證關鍵行為)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者提供一張目前執行畫面的截圖並手繪標註，提出 5 項調整：

1. 版面有一處空白，讓主圖可以向左延伸，不要浪費空白。
2. 下方日期佔用版面過多，需要縮小。
3. 股票名稱部分被遮住/顯示不正確，需要修正。
4. 「實盤下單」面板裡，價往上調整、量往右調整。
5. 「可現股當沖」與「現股當沖(先賣後買)」要依股票是否開放現沖由系統判斷，
   不是人去手動勾選。

第 5 項在動工前，先跟使用者確認清楚：目前「✅可現股當沖」徽章本來就是
純顯示、不可互動的系統判斷結果；「現股當沖(先賣後買)」checkbox 本身的
**鎖定/解鎖**也早已是系統判斷 (不合格時鎖住並強制清空)，唯一還留給人手動
操作的部分是「合格時，勾選本身要不要也由系統自動打勾」。這牽涉到真實
下單行為 (勾選會讓委託多帶 `daytrade_short=True`)，先提出疑慮並詢問
使用者：如果自動勾起來，是不是代表點買進/賣出就會直接送出現沖單？
使用者確認「對，就是要這樣，系統自動幫我勾起來，點買進/賣出就送出現沖單」，
在取得明確確認後才動工，不是自己假設後直接實作。

### 決定

1. **圖表動態填滿版面 (對應第1項)**：
   - 根本原因：`draw_chart()` 原本用寫死的 `figsize=(11, 8)` (英吋)，
     matplotlib 的 Figure 畫完之後不會自動跟著 tkinter 容器一起變大——
     `chart_frame` 這個容器本身會被 `pack(fill=tk.BOTH, expand=True)`
     撐開，但裡面嵌入的圖表內容還是維持原本 figsize 換算出來的固定
     像素大小，於是視窗夠寬時，圖表周圍就會出現沒有用到的空白。
   - 修法：`draw_chart()` 改成呼叫前先用 `self.chart_frame.winfo_width()`/
     `winfo_height()` 讀取容器目前實際的像素尺寸，換算成英吋當作
     `figsize` 傳給 `mpf.plot()`。若視窗剛啟動、容器還沒有實際尺寸
     (`winfo_width()`/`winfo_height()` 回傳很小的值)，退回原本 (11, 8)
     這組保底預設值，避免算出畸形的 figsize。
   - 額外處理：新增 `chart_frame` 的 `<Configure>` 事件綁定
     (`_on_chart_frame_resize`)，使用者拖曳視窗邊框改變大小時，圖表也會
     跟著重新繪製、套用新的 figsize，而不是只有第一次載入才會是正確
     尺寸。用 `_debounced_resize_redraw` 搭配 `self.after_cancel()` 做
     debounce (300ms)，避免拖曳過程中 `<Configure>` 連續觸發造成頻繁
     重繪、畫面頓卡。
   - 連帶修正：`safe_after()` 原本沒有回傳底層 `tkinter.after()` 的排程
     id，這次補上回傳值，讓 `_on_chart_frame_resize` 的 debounce 邏輯
     才有東西可以呼叫 `after_cancel()` 取消。

2. **日期標籤縮小 (對應第2項)**：
   - `mpf.plot()` 新增 `xrotation=0` (原本 mplfinance 預設是較大的旋轉
     角度，旋轉角度越大文字佔用的垂直高度越多)。日期標籤本來就間隔得
     夠開，水平顯示不會互相重疊，卻能明顯縮小底部日期區塊佔用的版面。
   - `xq_style` 的 `rc` 字典新增 `'xtick.labelsize': 8`、
     `'ytick.labelsize': 8`，讓刻度文字本身也縮小一點，進一步節省空間。

3. **換股後清除殘留的舊股票 hover 資訊 (對應第3項)**：
   - 根本原因：`lbl_hover_info` (畫面上方顯示「代碼 名稱 | 時間 | 開高低收
     | 漲跌 | 量」的那一列) 只有在滑鼠移到 K 線圖上時才會更新內容；
     換股後如果使用者還沒把滑鼠移到新圖表上，這個標籤會停留在**上一檔
     股票**最後一次 hover 到的資料，跟新載入的圖表標題 (`0050 元大台灣50
     旗艦操盤圖`，顯示正確的新股票) 不一致，造成「股票名稱好像被蓋掉/
     顯示錯誤」的觀感——這正是使用者截圖裡看到「2330 台積電」跟
     「0050 元大台灣50」同時出現、卻是兩檔不同股票的原因。
   - 修法：`update_ui()` (換股資料載入完成後的 UI 更新函式) 裡，在呼叫
     `draw_chart()` 之前，把 `lbl_hover_info` 重置回預設提示文字
     「滑鼠游標移至 K 線圖上方以顯示詳細資訊...」，並把 `last_hover_idx`
     重置為 `-1`，確保不會有上一檔股票的殘留資料留在畫面上。

4. **實盤下單面板：價與量合併同一列，價在左、量在右 (對應第4項)**：
   原本「量」與「價」是各自獨立的兩列 (量在上、價在下)，改成合併成
   同一列，價格欄位放在左側 (視覺上優先)、數量欄位放在右側，符合
   使用者「價往上調整,量往右調整」的具體指示。功能邏輯完全不變，
   只是版面排列方式調整。

5. **現沖 checkbox 系統自動判斷 (對應第5項，經使用者明確確認)**：
   - **換新標的時**：在 `fetch_data_worker()` 判斷完 `self.current_day_trade`
     之後，立即把 `self.daytrade_var` 設為 `bool(self.current_day_trade)`——
     這檔股票可以現沖就自動打勾，不行就不勾，這是「這檔新標的的起始值」，
     由系統決定，不需要使用者手動點擊。
   - **`update_daytrade_checkbox_state()` 改為只負責鎖定/解鎖 + 不合格時
     強制清空**：合格時**不再**主動把 `daytrade_var` 設回 `True`。原因：
     這個函式會在使用者切換交易別/種類等按鈕時也被呼叫到；如果合格時
     也順手把勾選狀態設回 `True`，會導致使用者在同一檔股票內手動取消
     勾選 (例如這一筆單特別不想現沖) 之後，只要點了其他按鈕就被悄悄
     改回勾選，變成「怎麼勾不掉」的困擾。現在的設計是：**系統決定
     「起始預設值」，使用者仍保有在單筆委託上手動取消的空間**，這個
     取消不會被其他操作意外覆蓋回去，直到換了新標的才會依新標的的
     資格重新設定一次起始值。

### 替代方案 (已考慮但未採用)

- **合格時每次呼叫 `update_daytrade_checkbox_state()` 都強制設回 `True`**：
  被否決 (詳見上方決定第5點的說明)——這會讓使用者的手動取消勾選在
  同一檔股票內隨時可能被其他操作意外覆蓋，造成困擾。

- **圖表容器改用固定寫死的較大 figsize (例如 (16, 9))，而不是動態讀取
  容器尺寸**：被否決。使用者的視窗大小、分割窗格比例都可能不同，
  寫死一個「夠大」的尺寸沒辦法真正貼合每個人的實際視窗，動態讀取
  `chart_frame` 的實際像素尺寸才能真正做到「填滿可用空間、不浪費版面」，
  這也是這次問題的根本解法而不是治標。

- **日期標籤旋轉角度改成中間值 (例如 30 度) 而不是完全水平 (0 度)**：
  考慮過，但日期字串本身間隔夠開，完全水平也不會重疊，選擇 0 度可以
  最大幅度縮小垂直空間，符合使用者「縮小日期版面」的明確訴求；如果
  之後發現水平顯示在某些週期 (例如密集的分K) 造成標籤重疊，可以再
  調整成一個較小的非零角度當折衷方案。

### 後果 / 影響

- **正面**：圖表填滿可用寬度、視窗縮放時圖表也會跟著重新適應版面；
  日期區塊縮小、多出來的垂直空間可以讓圖表本體顯示更多內容；換股後
  不會再看到上一檔股票的殘留名稱/資料；下單面板價量欄位排列符合
  使用者期望的視覺優先順序；現沖 checkbox 在使用者確認過的前提下，
  現在能自動反映每檔股票的現沖資格，減少每次下單都要手動確認/勾選
  的操作負擔，同時保留使用者在單筆委託上手動關閉現沖的彈性。

- **已完成的驗證**：
  1. 用假 tkinter 環境重新跑過 `StockTradingAppPro()` 建構，成功無例外
     (驗證版面調整後 widget 建構順序沒有問題)。
  2. 新增並執行以下針對本次修改的測試 (共 36 項全部通過，含先前所有
     累積測試)：
     - 圖表縮放 debounce 機制：連續觸發兩次 `<Configure>`，驗證第二次
       確實會呼叫 `after_cancel()` 取消第一次排程的重繪，而不是兩次
       重繪都真的執行。
     - hover 資訊重置邏輯：驗證重置後的文字與索引值正確。
     - 現沖 checkbox：驗證不合格時強制清空且鎖住；驗證合格狀態下，
       使用者手動取消勾選後，模擬呼叫 `update_daytrade_checkbox_state()`
       (對應切換其他按鈕的連帶呼叫) 不會覆蓋使用者的取消勾選。
  3. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法 (這次也把 `after_cancel`
     加入 tkinter 內建方法白名單)。
  4. `tests/test_core.py` (`core/`/`data/` 層) 不受本次修改影響，
     35 個測試依然全數通過。

- **需要使用者配合驗證的部分 (無法在此環境完成，請在有真實 tkinter 與
  畫面的機器上實測)**：
  - 確認圖表確實填滿了原本空白的區域，視覺上不再有明顯的浪費空間；
    拖曳視窗邊框改變大小，確認圖表會跟著重新適應、且拖曳過程中沒有
    明顯頓卡 (debounce 是否設定得宜)。
  - 確認日期標籤縮小後，在密集的分K模式下 (例如 1分K/5分K) 標籤彼此
    之間有沒有互相重疊；如果有，需要再調整旋轉角度或字體大小。
  - 換股後 (不要移動滑鼠) 確認上方資訊列會顯示預設提示文字，而不是
    上一檔股票的殘留資料；移動滑鼠到圖表上，確認會正確顯示新股票的
    hover 資訊。
  - 確認「價」「量」欄位新的排列順序符合預期。
  - **這是最需要注意實測的部分**：用**模擬帳號**測試一檔確定可以現股
    當沖的股票，載入後確認「現股當沖(先賣後買)」checkbox 自動是勾選
    狀態；手動取消勾選後，切換「種類」或「交易別」按鈕幾次，確認
    勾選狀態維持在「未勾選」，不會被悄悄改回勾選；换到另一檔不能
    現沖的股票，確認 checkbox 會被鎖住且清空；再換回原本那檔可以
    現沖的股票，確認又會重新自動打勾 (因為換了新標的，起始值重新
    決定一次)。點擊「賣出」前務必再次確認 checkbox 目前的勾選狀態
    是自己想要的，避免非預期送出現沖單。

- **待補**：這次的 debounce 時間 (300ms) 與圖表 DPI (100) 都是憑經驗
  選擇的合理預設值，如果實測後覺得縮放反應太慢/太快、或圖表文字大小
  不合適，可以再微調這兩個數字，不需要為此另開 ADR (屬於數值微調，
  不是架構或行為變更)。

### 相關程式位置 (`stock_app_pro.py`)

- 檔案開頭：`xq_style` 的 `rc` 字典新增 `xtick.labelsize`/`ytick.labelsize`。
- `draw_chart()`：動態 figsize 計算、`mpf.plot()` 新增 `xrotation=0`。
- `create_widgets()`：新增 `chart_frame` 的 `<Configure>` 綁定；
  「量」「價」合併成同一列，價在左、量在右。
- 新增方法：`_on_chart_frame_resize()`、`_debounced_resize_redraw()`。
- `safe_after()`：補上回傳底層 `after()` 排程 id。
- `fetch_data_worker()` 內 `update_ui()`：換股時重置 `lbl_hover_info`/
  `last_hover_idx`；換股後依 `current_day_trade` 設定 `daytrade_var` 初始值。
- `update_daytrade_checkbox_state()`：合格時不再主動覆蓋 `daytrade_var`。

---

## ADR-016：修正小數點無法輸入、確認委託書號 00000 為正常現象、偵測連線階段掉線

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用模擬 tkinter 環境驗證關鍵行為)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者實際下單測試後回報三件事：

1. 自己在價格欄位輸入時打不出小數點，懷疑是 bug。
2. 實際下單成功，官網帳務有看到這筆單，但「委託書號」欄位顯示 `00000`，
   詢問這是否正常。
3. 帳號同時登入官網後，API 這邊出現
   `place_order: Shioaji error Session error SolClient send request
   api/v1/order/place_order, code: NotReady, Error ErrorInfo
   { sub_code: SubCode(SessionNotEstablished), error_str: "Unable to wait
   for session '(c3,s1)_sinopac' to be established" }`，接著連線就斷了，
   之後重新登入又出現 `【API 登入或憑證失敗】: login: connection error`，
   要先登出官網才能讓 API 重新登入成功。

第 1 項檢查 `entry_price` 這個 tkinter Entry 元件，確認程式碼裡完全沒有
`validatecommand` 或任何字元層級的輸入限制，代表這不是我們自己寫的邏輯
在擋輸入。查證後找到一個 Windows 上很經典的 Tk 已知怪癖：數字鍵盤的小數點鍵
在 **NumLock 關閉** 時，作業系統會把它當成 Delete 鍵送出 (Tk 收到的 keysym
是 `KP_Delete` 而不是句點字元)，這是實體鍵盤本身小數點鍵與 Delete 鍵共用同一
顆按鍵、由 NumLock 狀態決定送出哪一個，不是輸入法或本程式邏輯的問題。

第 2 項查證 shioaji 官方教學文件 (iThome 鐵人賽系列文章，內容取自官方
`sinotrade.github.io` 教學) 裡的**真實 SDK 呼叫範例**，確認委託剛送出、
狀態還是 `PendingSubmit` (對應永豐金網頁介面顯示的「委託預約中」) 時，
`ordno` (委託書號) 欄位本來就是 `'00000'` 這個預留值，要等交易所真的
處理撮合、狀態轉為已受理/成交等後續狀態，才會回填真正的委託書號。
這是官方文件自己展示的範例輸出，不是我們程式或使用者操作有問題。

第 3 項查證 shioaji 官方「使用限制」文件，確認 **「同一永豐金證券
person_id 僅可使用最多 5 個連線 (api.login() 即建立一個連線)」**——
shioaji 本身允許多重連線，不是嚴格限定「只能一個連線」。但使用者描述
的現象 (官網登入導致 API 斷線、且要先登出官網才能讓 API 重新連上)，
符合許多券商的常見風控機制：**同一帳號的「交易/下單階段」同時間只能
有一個生效，不論是透過官網、App 或 API**，這是為了避免同一帳號從多個
管道同時送出衝突或重複委託。這點沒有查到 Sinotrade 官方文件對「網頁
登入與 API 連線互相搶佔」這個具體情境的明確說明，只能確認這是與現有
查證結果相符的合理解釋，屬於券商後端的連線/風控政策，不是我們程式
能控制或修正的範圍。

### 決定

1. **修正小數點輸入問題 (NumLock 關閉的 Windows 已知怪癖)**：
   `entry_price` 新增綁定 `<KP_Delete>` 事件，新增方法
   `_insert_decimal_point_workaround()`：偵測到這個按鍵時，手動在目前
   游標位置插入 `"."` 字元，並回傳 `"break"` 阻止 Entry 預設的刪除行為
   繼續執行 (不然會變成「插入句點的同時還多刪一個字元」)。NumLock 開啟
   時小數點鍵走正常的 `KP_Decimal`/`period` 路徑，不受這個修正影響。

2. **回答第2項 (非程式異動)**：委託書號顯示 `00000` 在委託狀態還是
   「預約中/PendingSubmit」時是正常現象，是 shioaji 官方 SDK 範例本身
   展示的行為，等交易所實際處理撮合後委託書號就會回填成真正的號碼。
   不需要修改程式，這不是 bug。

3. **偵測連線階段掉線，讓畫面誠實反映斷線狀態 (回應第3項)**：
   - 新增 `_looks_like_session_dead(exc)`：檢查例外訊息裡有沒有出現
     `SessionNotEstablished`、`Session error`、`NotReady`、
     `connection error` 等已知字樣，判斷這個例外看起來像不像「shioaji
     連線階段已經斷了」。
   - 新增 `_mark_session_dead()`：偵測到疑似斷線時，把 `self.api_logged_in`
     撥回 `False`、更新登入按鈕與頂部狀態列文字為「🔴 連線中斷 (請重新
     登入)」，並在系統日誌提示最常見的成因 (同一帳號同時在官網/App
     登入) 與建議動作 (先確認登出其他地方，再回來重新登入)。
   - 套用到三個地方：`_confirm_and_place_order()` 下單失敗時、
     `fetch_data_worker()` 抓歷史 K 線失敗時、`process_broker_login()`
     登入失敗時 (登入失敗這裡額外多印一行更明確的提示，因為這正是
     使用者回報的「要先登出官網才能重新登入 API」這個情境)。
   - **這不是「修正 bug」，是「讓畫面在遇到我們無法控制的外部斷線原因時，
     至少誠實反映現狀，並給使用者可以採取行動的明確提示」**，實際的
     連線階段搶佔行為是券商後端的政策，我們無法阻止它發生，只能偵測
     並妥善呈現。

### 替代方案 (已考慮但未採用)

- **針對第1項，改成完全自訂數字輸入元件 (例如用 Spinbox 或限制只能輸入
  數字與一個句點的 validatecommand)**：暫不採用。使用者回報的症狀
  很明確指向 NumLock 開關的鍵盤行為，用最小、針對性的修正 (只處理
  `KP_Delete` 這一種按鍵) 風險最低；如果之後發現還有其他輸入相關的
  問題，再考慮要不要做更完整的輸入驗證機制。

- **針對第3項，嘗試在程式裡自動偵測「官網是否已登入」並主動阻止衝突**：
  不可行。我們的程式只能透過 shioaji API 存取這個帳號，沒有辦法得知
  使用者是否同時開著瀏覽器登入官網，這是外部系統的狀態，我們沒有
  管道查詢或控制。只能在「已經發生斷線」之後偵測並提示，沒辦法
  事前預防。

- **針對第3項，每隔一段時間主動呼叫某個 API 驗證連線是否還活著
  (心跳檢查)**：這次沒有做，因為目前偵測時機 (下單/抓資料/登入失敗時)
  已經涵蓋了使用者最常會遇到這個問題的操作路徑；主動心跳檢查會增加
  額外的 API 呼叫頻率，可能受限於每日流量配額 (ADR-005 已經討論過
  這個風險)，如果之後發現現有的偵測時機不夠即時，可以再評估要不要
  加上主動心跳檢查。

### 後果 / 影響

- **正面**：NumLock 關閉時價格欄位可以正常輸入小數點；使用者現在知道
  委託書號 00000 是正常現象，不用擔心是不是下單出了問題；連線階段
  被外部因素 (例如同時登入官網) 打斷時，畫面會正確顯示「連線中斷」
  而不是繼續騙使用者說「已連線」，且會提示最常見的排查方向。

- **已完成的驗證**：
  1. 用假 tkinter 環境新增三項測試：小數點 workaround 正確在游標位置
     插入句點且回傳 `"break"`；斷線關鍵字偵測邏輯對已知的三種錯誤訊息
     (Session error、connection error、Unable to wait for session) 都能
     正確判定為疑似斷線，且對無關的例外 (例如「找不到合約資訊」) 不會
     誤判；偵測到疑似斷線後 `api_logged_in` 確實會被撥回 `False`。
     連同先前所有測試，共 39 項全數通過。
  2. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法。
  3. `tests/test_core.py` (`core/`/`data/` 層) 不受本次修改影響，
     35 個測試依然全數通過。

- **需要使用者配合驗證的部分 (無法在此環境完成，因為這裡沒有真正的
  shioaji 連線、也沒有真實鍵盤可以測試 NumLock 行為)**：
  - 請先確認目前鍵盤的 NumLock 狀態，關閉 NumLock 後在價格欄位測試
    數字鍵盤小數點鍵，確認能正常打出句點；也請測試一般鍵盤 (非數字
    鍵盤區) 的句點鍵是否原本就正常 (如果一般鍵盤區的句點也打不出來，
    代表還有其他我們沒抓到的原因，需要再回報細節)。
  - 用**模擬帳號**重現「同時登入官網」的情境，確認斷線發生時，畫面
    的登入按鈕與狀態列會正確變回「連線中斷」，且系統日誌有出現
    提示訊息；登出官網後重新登入 API，確認能正常恢復連線。

- **待補**：如果之後想更即時地偵測連線狀態 (不等到使用者實際下單/
  抓資料才發現斷線)，可以評估加上定期心跳檢查機制，但需要先確認
  合適的檢查頻率 (避免消耗過多每日流量配額)。

### 相關程式位置 (`stock_app_pro.py`)

- `create_widgets()`：`entry_price` 新增 `<KP_Delete>` 綁定。
- 新增方法：`_insert_decimal_point_workaround()`、
  `_looks_like_session_dead()`、`_mark_session_dead()`。
- `_confirm_and_place_order()`、`fetch_data_worker()`、
  `process_broker_login()`：例外處理新增呼叫 `_looks_like_session_dead()`/
  `_mark_session_dead()`。

---

## ADR-017：圖表版面二次修正 (改用 subplots_adjust)、指標文字逐項獨立上色、左側欄位寬度調整

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用真實 matplotlib Axes 執行 `draw_chart()` 驗證)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者提供截圖並列出 10 項問題，其中**第 2、3 項是第二次提出**——上一版
(ADR-015) 嘗試用「依 `chart_frame` 實際像素尺寸動態計算 `figsize`」解決
圖表留白問題，顯然沒有真正解決。這次不能再用同一個思路碰運氣，必須換一個
更直接、更能保證結果的做法。

其餘 8 項：
1. 股票代號/名稱字體太大顯示不完整。
4. 左側欄位 (交易別按鈕等) 預設寬度不夠，文字顯示不完整。
5. 即時行情/盤後快照參考文字列，欄寬不足時應該要能自動換行而不是被裁切。
6. 使用者對「成交明細」的功能還是不理解，直接追問三個具體問題。
7. MACD 副圖顯示位置跑掉，所有技術指標副圖都要置中。
8. 所有技術指標副圖顯示的數據文字顏色，要跟副圖裡對應的線條顏色一致。
9. 主圖 (均線/布林) 顯示的數據文字顏色，也要跟主圖裡對應的線條顏色一致。
10. 重申「台股一律用券商 API，只有美股才自動用 YF」這條鐵律 (已經是
    ADR-011 的既有規則，這裡確認沒有被後續改動影響)。

### 決定

1. **圖表留白問題改用 `fig.subplots_adjust()` 強制指定邊界比例，取代
   `tight_layout=True` 的自動計算 (回應第2、3項)**：
   - 上一版思路是「figsize 算對了，tight_layout 自然會算出合理邊界」，
     但這個假設顯然不成立——多層副圖疊加旋轉 y 軸標籤時，`tight_layout`
     的自動邊界計算可能過於保守，算出遠比實際需要更大的留白。
   - 這次直接關閉 `tight_layout` (`tight_layout=False`)，在 `mpf.plot()`
     回傳 `fig` 之後，明確呼叫
     `fig.subplots_adjust(left=0.045, right=0.995, top=0.93, bottom=0.07,
     hspace=0.14)`，用寫死的比例值強制軸域填滿畫布，不再讓 matplotlib
     自己「猜」要留多少空白。這是決定性、可預期的做法：不管內容是什麼，
     邊界比例都固定，不會再出現「明明加大了 figsize，邊界卻還是很大」
     這種難以預期的行為。

2. **圖表標題字體縮小 (回應第1項)**：`fontsize` 從 12 調降為 10，同時
   受惠於第1點的邊界修正，軸域變寬後標題有更多空間可以完整顯示，
   兩者合併應該能解決代號/名稱顯示不全的問題。

3. **左側欄位寬度調整 (回應第4項)**：
   - 「交易別」四個按鈕 (整股/盤中零股/盤後定價/盤後零股) 原本擠在
     同一排、每個只分到約 1/4 寬度，4 字標籤在過窄的按鈕裡容易顯示
     不完整。改成 2x2 網格，每個按鈕拿到約 2 倍寬度。
   - 自選股群組下拉選單寬度從 `width=9` 增加到 `width=13`，避免較長的
     群組名稱顯示不完整。
   - 取價快捷下拉選單寬度從 `width=6` 增加到 `width=8`，「最新成交」
     這類 4 字選項才能完整顯示。

4. **即時行情文字改為自動換行 (回應第5項)**：`lbl_rt_quote` 新增
   `wraplength=220`、`justify="left"`，欄寬不足時文字自動換行到下一行，
   而不是被裁切或超出面板邊界；同時 `pack()` 加上 `fill=tk.X` 讓換行後的
   多行文字能正確撐開顯示。

5. **回答第6項 (非程式異動)**：直接針對使用者三個具體提問回答——
   - 「是我送出去的委託單會顯示在這嗎？」**不是**。成交明細顯示的是
     **市場上所有人**這檔股票的成交紀錄 (Time & Sales)，不是專屬於
     使用者自己的委託。
   - 「有沒有成交也會顯示在這嗎？」**不會**，這欄位只顯示「已經成交」
     的市場紀錄，不會告訴你「你的委託有沒有成交」。
   - 「尚未完全成交的也會顯示在這嗎？」**不會**，部分成交/未成交狀態
     跟這個欄位完全無關。
   - 使用者真正想知道「自己的委託單狀態」，目前**沒有**專屬的畫面元件
     顯示這個資訊，只有下單後 `_confirm_and_place_order()` 印在系統
     日誌裡的文字紀錄 (委託成功/狀態/委託書號等)。如果需要一個專門
     顯示「我自己委託單清單與狀態」的畫面，這是目前缺少、值得另外
     開發的功能，需要另外規劃 (呼叫 shioaji 的 `list_trades()`/
     `update_status()` 之類的 API)。

6. **技術指標文字改為逐項獨立上色 (回應第7、8、9項)**：
   - 原本主圖 (`self.txt_main`) 與各副圖 (`self.sub_texts[name]`) 都是
     **單一個** text 物件、單一固定顏色，把好幾個數值串成一長串字串
     (例如 `"MACD: 1.78  Signal: 1.08  Hist: 0.70"` 全部都是同一個藍色)。
   - 改為 `self.txt_main_segments`(列表) 與 `self.sub_texts[name]`(列表)：
     每個數值都是獨立的 text 物件，各自設定跟該數值在圖上對應線條
     **相同的顏色** (MA 依 `self.ma_colors[i]`、BB 依 `self.bb_color`、
     MACD/Signal/RSI/K/D/J/+DI/-DI/ADX/BBW 都各自對應 `draw_chart()`
     裡 `mpf.make_addplot()` 設定的線條顏色)。
   - **Hist 是特例**：Hist 是長條圖，顏色本來就依數值正負變化 (紅漲
     綠跌)，不是單一固定線條顏色。用 `dynamic_color_key` 標記這個
     segment，在 `on_mouse_move()` 裡依當下 hover 到的數值正負，
     呼叫 `.set_color()` 動態切換紅/綠，而不是寫死一種顏色。
   - 每個 text 物件用不同的 x 軸座標 (axes 相對座標，例如 0.01、0.15、
     0.30) 水平排開，避免互相重疊；配合第1點的邊界修正 (軸域變寬)，
     連帶也有機會改善使用者反映「MACD 顯示位置跑掉」的問題——原本
     擁擠的邊界與不可預期的 `tight_layout` 計算，很可能就是造成副圖
     視覺跑位的根源之一。

### 替代方案 (已考慮但未採用)

- **繼續嘗試調整動態 figsize 的計算方式 (例如改用不同 DPI 或加上安全
  係數)**：被否決。這條路已經試過一次沒有解決問題，再猜一次數字不是
  負責任的做法；改用 `subplots_adjust()` 直接控制邊界比例，才是決定性
  地排除「邊界由誰計算、算得對不對」這個不確定因素。

- **主圖/副圖文字用單一 text 物件、但用 LaTeX 富文本語法做多色顯示**：
  被否決。matplotlib 的富文本色彩語法支援有限且版本間差異大，不如
  用多個獨立 text 物件直接指定顏色來得直接可靠，程式邏輯也更容易
  理解與維護。

- **成交明細功能：既然使用者持續誤解，乾脆拿掉或改名**：
  這次沒有這樣做，因為這個功能本身 (市場成交明細/Time & Sales) 是
  有效、常見的看盤資訊，問題在於使用者真正想要的是「自己委託單的
  狀態」，這是一個**目前還沒開發的不同功能**，不是現有功能設計錯誤，
  不需要為此拿掉或改名現有功能；如果之後要開發「我的委託單狀態」
  這個新功能，應該另外規劃、另開 ADR，不要跟現有的市場成交明細混在
  一起。

### 後果 / 影響

- **正面**：圖表邊界改用明確比例強制控制，不再依賴難以預期的自動計算；
  標題字體縮小、軸域變寬，代號名稱能更完整顯示；左側欄位寬度加大，
  文字不易被截斷；即時行情文字支援自動換行；技術指標數據文字顏色
  跟線條顏色一致，一眼就能對應是哪條線的數值；使用者對「成交明細」
  的具體疑問得到直接回答，且釐清「自己委託單狀態」目前是缺少的功能，
  不是被誤解的既有功能。

- **已完成的驗證 (這次特別加強)**：
  1. 過去的假 tkinter 環境用空殼 `mpf.plot()` (回傳 `(None, [])`)，
     導致 `draw_chart()` 內部所有動到 `axlist` 的程式碼 (`set_title`/
     `.text()`/`axvline` 等) 從來沒有被真正執行測試過——這是這次
     診斷工具本身的一個重大改進：**改用真正的 matplotlib Figure +
     Axes 建構假的 `mpf.plot()` 回傳值**，讓 `draw_chart()` 可以對著
     「真的」Axes 物件執行，才能抓到方法名稱打錯、參數不對這類真實
     例外。這個改進後的 mock 統一放進 `diag_mock_tkinter.py` 的
     `install_mock_tkinter()` 函式內，讓所有診斷腳本共用，不會再有
     「同一個 mock 兩份程式碼、其中一份沒同步更新」的問題 (這次
     開發過程中就實際踩到這個坑，已修正)。
  2. 新增測試：開啟 MA (SMA5/SMA10)、布林通道、MACD、RSI、KDJ、DMI、
     布林寬度全部指標，實際呼叫 `draw_chart()`，驗證：
     - 主圖與各副圖產生的獨立文字物件數量正確 (MACD/KDJ/DMI 各 3 段，
       RSI/BBW 各 1 段)。
     - 模擬滑鼠移到圖表上、觸發 `on_mouse_move()`，驗證每段文字都能
       正確更新且不崩潰。
     - **驗證顏色確實正確**：SMA5 的文字顏色確實等於使用者設定的
       均線顏色；Hist 的動態上色邏輯確實會依正負變成紅色或綠色。
     連同先前所有測試，共 40 項全數通過。
  3. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法。
  4. `tests/test_core.py` (`core/`/`data/` 層) 不受本次修改影響，
     35 個測試依然全數通過。
  5. 確認第10項 (台股一律 shioaji、美股自動 YF 的 `is_taiwan_instrument`
     邏輯，ADR-011 建立) 沒有被本次或先前任何修改動到，規則仍然完整。

- **需要使用者配合驗證的部分 (無法在此環境完成，因為這裡沒有真實螢幕
  可以肉眼確認排版與顏色的視覺效果)**：
  - **這是最需要確認的部分**：實際啟動 App，確認圖表左側與下方的空白
    是否確實消除、股票代號名稱是否完整顯示、MACD 等副圖位置是否恢復
    正常，不再跑位。如果 `subplots_adjust()` 這組邊界比例數值在你的
    實際螢幕解析度/視窗大小下還是不夠理想 (太擠或還有空白)，請具體
    回報是「太擠」還是「還有空白」、大概在哪個邊 (上下左右)，這樣才能
    精準調整對應的數值，而不是整組重猜。
  - 確認技術指標文字顏色是否正確對應到線條顏色 (例如 MACD 紅色文字
    對應紅色 MACD 線、Signal 藍色文字對應藍色 Signal 線)，尤其 Hist
    在正值/負值時文字顏色是否正確切換紅綠。
  - 確認交易別按鈕的 2x2 排列、自選股群組與取價下拉選單的文字是否
    完整顯示。
  - 確認即時行情文字過長時是否正確換行，而不是被裁切或跑出面板外。

- **待補**：如果之後真的需要「顯示使用者自己委託單狀態」這個功能
  (回應第6項使用者的真正需求)，需要另外規劃並開一筆新的 ADR，
  評估要用 shioaji 的 `list_trades()`/`update_status()` 等 API 怎麼
  整合進畫面。

### 相關程式位置 (`stock_app_pro.py` / `diag_mock_tkinter.py` / `diag_interaction_paths.py`)

- `draw_chart()`：`tight_layout` 改為 `False`，新增
  `fig.subplots_adjust(...)`；標題 `fontsize` 調整；主圖/副圖文字建立
  邏輯整段重寫為逐項獨立上色 (`self.txt_main_segments`/
  `self.sub_texts[name]` 改為列表結構)。
- `on_mouse_move()`：清空與更新 hover 文字的邏輯改為對應新的列表結構，
  新增 Hist 的動態紅綠上色。
- `__init__`：`self.txt_main` 改為 `self.txt_main_segments = []`。
- `create_widgets()`：交易別按鈕改 2x2 網格；`cb_wl`/`cb_quick_price`
  寬度加大；`lbl_rt_quote` 新增 `wraplength`。
- `diag_mock_tkinter.py`：`install_mock_tkinter()` 整合 yfinance/mplfinance
  的假模組設定 (含改用真實 matplotlib Axes 的 `mpf.plot`)，讓所有診斷
  腳本共用同一套、避免重複維護造成不同步。
- `diag_interaction_paths.py`：移除重複的舊版 yfinance/mplfinance 設定，
  改用共用版本；新增 `draw_chart()` 全指標開啟 + 模擬 hover 的測試。

---

## ADR-018：強制畫布像素尺寸、副圖Y軸依可見範圍重算、委託回報主動推播、價格自動對齊tick

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用真實 matplotlib Axes 與模擬 callback 驗證)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者第三次提出圖表版面留白問題，並新增四項：副圖指標數字被遮住、
0050 的 MACD 副圖持續顯示異常、手動輸入不合規價格應自動修正、
新增「我的委託單」「我的已成交」清單。

第2、3項 (留白) 前兩次的修法都沒解決；這次改用強制畫布像素尺寸的方式
(見決定1)。第3項 (MACD 異常) 這次深入排查找到具體根因 (見決定3)。
第5項 (委託單清單) 動工前查證 shioaji 官方文件，確認：
`api.list_trades()` 取得本地快取的 Trade 物件、`api.update_status()`
才能真正更新狀態；**但官方「使用限制」文件明確要求「委託狀態請使用
主動回報（callback 或 SSE order event），避免以 update_status() 輪詢」**，
故本功能設計上完全不做輪詢，改用 `api.set_order_callback()` 註冊的
push callback (`OrderState.StockOrder`/`StockDeal`/`FuturesOrder`/
`FuturesDeal` 等事件) 驅動。

### 決定

1. **圖表留白第三次修正:強制 canvas widget 的實際像素尺寸 (回應第1項)**：
   前兩次都只從「figsize 該抓多大」下手，這次改成：算出 `frame_w`/
   `frame_h` 之後，除了照樣換算成 figsize 傳給 `mpf.plot()`，額外在
   `canvas_widget.pack()` **之前**呼叫 `canvas_widget.config(width=frame_w,
   height=frame_h)`，直接命令 Tk widget 本身的像素佔位等於容器的實際
   像素，不再只靠 figsize 換算後讓它自然決定大小。這樣不管 Windows DPI
   縮放係數是多少 (這是前兩次沒抓到的可能根因：Tk 回報的 `winfo_width()`
   與 matplotlib 實際渲染像素之間，在有 DPI 縮放的系統上可能存在落差)，
   canvas widget 一定會等於容器尺寸，不會再因為兩邊計算落差在四周留下
   空白。

2. **指標文字提高 zorder 到 10000 並加上 `clip_on=False` (回應第2項)**：
   原本 `zorder=999` 理論上已經夠高，但為了絕對確保「永遠顯示在最上層」，
   直接拉高到 `10000`，並明確加上 `clip_on=False` 避免文字被自身所在
   axes 的邊界裁切。十字準線 (`self.vlines`) 也明確設定較低的
   `zorder=50`，確保排列順序上文字一定在準線之上。

3. **根本排查並修正 MACD (及其他副圖) 顯示異常 (回應第3項)**：
   - **根因確認**：`auto_scale_y()` 原本只套用在主圖 (價格) 的 Y 軸，
     依「目前縮放/平移後實際看得到的 X 範圍」重新計算 Y 軸上下限；
     但 MACD/RSI/KDJ/DMI/布林寬度這些副圖，Y 軸範圍完全是 mplfinance
     依「整個資料集」算出來的固定值，從來沒有跟著使用者的縮放/平移
     操作重新計算過。如果歷史資料裡 (即使是已經捲動到畫面外看不到的
     那一段) 某個時間點的數值特別大，會撐開整個副圖的 Y 軸範圍；
     使用者目前實際看得到的這一小段資料，數值相對這個被撐大的範圍
     顯得極小，畫出來就會被壓縮成一條貼在底部或某一側的扁平線，
     看起來像「跑掉/壞掉」——這正是使用者反映的現象，且跟資料長度
     (查詢天數越長，出現極端值的機率越高) 相關，可以解釋「總是」
     這個字眼。
   - **修法**：新增 `auto_scale_indicator_panels(xmin, xmax)`，讓每個
     有開啟的副圖都依「目前實際看得到的 X 範圍」內的資料重新計算 Y
     軸上下限，取代原本 mplfinance 用整個資料集算出來的固定範圍。
     需要 `draw_chart()` 設定的 `self.active_panels`/`self.panel_columns`
     才知道每個副圖對應哪些欄位 (MACD→MACD/Signal/Hist、KDJ→K/D/J...)。
   - 套用到全部 4 個會改變可見範圍的地方：`draw_chart()` 初始載入、
     `draw_chart()` 還原上次縮放位置、滾輪縮放 (`on_scroll_zoom`)、
     拖曳平移 (`on_mouse_move`)。拖曳平移原本只有「在價格面板上拖曳」
     才會重算 Y 軸，這次改成不論在哪個面板上拖曳都會重算 (因為
     mplfinance 各面板共用同一個 x 軸，拖曳任一面板都會影響全部面板
     顯示的資料範圍)。

4. **手動輸入價格自動對齊 tick (回應第4項)**：
   - `core/tick_rules.py` 新增 `round_to_tick(price, asset_type,
     raw_symbol)`：把價格四捨五入到最接近的合法 tick 位置，並處理
     「捨入後跨到不同價格帶」的邊界情況 (先用原始價格對應的 tick 捨入
     一次，如果捨入結果落入不同價格帶，再用新價格帶的 tick 重新對齊)。
   - `entry_price` 新增 `<FocusOut>` 綁定 (`_round_price_entry_to_tick`)：
     使用者輸入完價格、游標離開輸入框時，如果價格不符合 tick 規則，
     自動修正並在系統日誌註明修正前後的數值。
   - `execute_order()` 送單前**再做一次**保底修正：即使 `FocusOut` 事件
     因為某些操作時機沒有觸發到，送單前這一道防線一定會把價格修正到
     合法範圍才組裝委託，確保絕對不會送出違規價格。

5. **新增「我的委託單」「我的已成交」清單，完全用主動回報驅動、不做輪詢
   (回應第5項)**：
   - **UI**：把底部原本單一的「系統日誌與回報」區塊，改成分頁式，
     新增「我的委託單」「我的已成交」兩個分頁 (用按鈕切換顯示哪一個)，
     共用同一塊底部空間，不需要另外找地方塞新表格。兩個新分頁都用
     `ttk.Treeview` 做成欄位化表格 (委託單：時間/商品/買賣/價格/數量/
     已成交/狀態；已成交：時間/商品/買賣/成交價/成交量)，比純文字
     Listbox 更適合結構化資料。
   - **後端**：登入成功後呼叫 `self.sj_api.set_order_callback(
     self.on_order_deal_callback)` 註冊一次 push callback。
     `on_order_deal_callback(stat, msg)` 依 `stat` 字串裡有沒有出現
     "Deal"/"Order" 分流到 `_handle_deal_event()`/`_handle_order_event()`
     (用字串比對而不是精確比對列舉值，避免不同 shioaji 版本的
     `FOrder`/`FuturesOrder`/`TFTOrder` 這類命名差異造成漏接)。
   - `_handle_order_event()` 解析委託回報 (`operation`/`order`/`status`/
     `contract` 四個子欄位)，用 `order.id` 當 key 更新/插入
     `self.my_orders`；`_handle_deal_event()` 解析成交回報 (`trade_id`/
     `code`/`action`/`price`/`quantity`/`ts` 等欄位)，插入
     `self.my_fills` (上限 200 筆)，並累加對應委託單的 `filled_quantity`、
     依累計成交量是否達到委託量更新狀態顯示為「部分成交」或「全部成交」。
   - `_confirm_and_place_order()` 送單成功後，**立即**把這筆單塞進
     `self.my_orders` 給即時回饋，不用等 callback 推播 (推播可能有些微
     延遲)；之後 callback 收到這筆單的後續回報時，用同一個 `order_id`
     覆蓋/更新這筆資料，兩邊資料自然對齊，不會重複。
   - 這個 callback 是 shioaji 內部執行緒呼叫的，所有畫面更新都透過
     `self.safe_after()` 排回主執行緒 (沿用 ADR-012 的機制)。

### 替代方案 (已考慮但未採用)

- **圖表留白繼續在 figsize/DPI 計算上打轉 (例如改抓不同的 DPI 查詢方式)**：
  被否決。這條路已經連續兩次沒解決問題，改用「直接命令 widget 的
  實際像素尺寸」是更直接、跟 DPI 計算細節完全脫鉤的做法，不管背後
  是什麼原因造成 figsize 換算落差，強制設定寬高都能繞過這個問題。

- **「我的委託單/已成交」用 `update_status()` 定期輪詢刷新**：
  明確被官方文件否決 (「委託狀態請使用主動回報，避免以 update_status()
  輪詢」)。改用 `set_order_callback()` 的 push 事件驅動，完全不輪詢，
  符合官方建議的正確用法。

- **副圖 Y 軸重新計算時，用整個 `plot_df` 的範圍加上一點 buffer，而不是
  嚴格只看目前可見範圍**：被否決。這樣做只是把問題的嚴重程度降低，
  沒有真正解決「歷史極端值撐開範圍」這個根因；嚴格只看目前可見範圍
  內的資料，才能確保使用者看到的圖形真正對應「這段資料的相對大小」，
  這才是技術指標圖表應有的行為。

### 後果 / 影響

- **正面**：圖表應該不再因為 DPI 落差留下空白 (改用更直接、更能保證
  結果的强制像素設定)；副圖 Y 軸會依可見範圍動態調整，MACD 等指標
  被歷史極端值壓縮成扁平線的問題應該解決；技術指標文字保證顯示在
  最上層；手動輸入的不合規價格會自動修正，降低送出違規價格被券商
  退單的風險；新增的委託單/成交清單完全用官方建議的主動回報方式
  驅動，不會有輪詢造成的流量風險。

- **已完成的驗證**：
  1. 用 ADR-017 建立的真實 matplotlib Axes 測試環境，確認 `draw_chart()`
     搭配新的 canvas 尺寸強制設定、zorder 調整、`auto_scale_indicator_panels`
     呼叫都不會崩潰。
  2. 新增 `auto_scale_indicator_panels` 在「沒有開啟任何副圖」情況下
     不會崩潰的測試。
  3. 新增 `round_to_tick` 的完整單元測試 (含跨價格帶邊界情況、期貨固定
     整數點)，以及透過模擬 `FocusOut` 事件驗證整合行為 (105.83 自動
     修正為 105.85)。
  4. 新增底部分頁切換測試；新增模擬 `OrderState.StockOrder`/
     `OrderState.StockDeal` 兩種事件呼叫 `on_order_deal_callback()`，
     驗證委託回報正確填入 `my_orders`、成交回報正確填入 `my_fills`
     並正確累加對應委託單的已成交量、狀態正確變成「全部成交」；
     也驗證未知的 `stat` 類型不會讓程式崩潰。
  5. 為了讓上述測試能真正執行，`diag_mock_tkinter.py` 新增
     `_MockTreeview` (支援 `heading`/`column`/`insert`/`delete`/
     `get_children`)。
  6. 連同先前所有測試，共 46 項全數通過。
  7. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法。
  8. `tests/test_core.py` 新增 5 個 `round_to_tick` 測試，連同既有測試
     共 40 個全數通過。

- **需要使用者配合驗證的部分 (無法在此環境完成，因為這裡沒有真實螢幕
  可以肉眼確認排版、也沒有真實 shioaji 連線可以測試委託回報)**：
  - **這是最需要確認的部分**：實際啟動 App，確認圖表四周的空白是否
    真的消除。如果強制 canvas 尺寸這個做法在你的環境還是有落差，
    請提供更多細節 (例如：Windows 顯示設定裡的縮放比例是多少%)，
    這樣才能判斷是不是還有其他 DPI 相關因素需要處理。
  - 開啟 MACD 副圖，縮放/平移 K 線圖，確認 MACD 線不再被壓縮成扁平線、
    數值變化在畫面上看得出明顯的高低起伏。
  - 確認技術指標文字不會再被任何內容遮住。
  - 手動在價格欄位輸入不合規的價格 (例如 100 元以上輸入到小數點後
    兩位)，點掉輸入框確認自動修正是否正確、系統日誌是否有提示。
  - **用模擬帳號**下單後，切到「我的委託單」分頁確認這筆單有出現；
    等撮合成交後 (或用可以快速成交的商品/價位測試)，確認「我的已成交」
    分頁有出現對應紀錄，且委託單分頁的「已成交」欄位與狀態有正確更新。

- **待補**：「我的委託單」目前沒有實作「刪單/改單」的操作按鈕，如果
  之後需要直接在這個清單上刪單或改價改量，需要另外規劃 UI 與呼叫
  `api.cancel_order()`/`api.update_order()`，並另開 ADR 記錄。

### 相關程式位置 (`stock_app_pro.py` / `core/tick_rules.py` / `tests/test_core.py` / `diag_mock_tkinter.py`)

- `draw_chart()`：canvas widget 強制像素尺寸；文字 zorder 提高並加
  `clip_on=False`；新增 `self.active_panels`/`self.panel_columns` 設定。
- 新增方法：`auto_scale_indicator_panels()`。
- `on_scroll_zoom()`/`on_mouse_move()`：新增呼叫 `auto_scale_indicator_panels()`；
  平移邏輯移除「只在價格面板上才重算」的限制。
- `core/tick_rules.py`：新增 `round_to_tick()`。
- `create_widgets()`：`entry_price` 新增 `<FocusOut>` 綁定；底部日誌區塊
  改為分頁式，新增委託單/成交 Treeview。
- 新增方法：`_round_price_entry_to_tick()`、`set_bottom_tab()`、
  `_refresh_my_orders_ui()`、`_refresh_my_fills_ui()`、
  `on_order_deal_callback()`、`_handle_order_event()`、`_handle_deal_event()`。
- `__init__`：新增 `self.my_orders`/`self.my_fills`。
- `process_broker_login()`：新增 `set_order_callback` 註冊。
- `execute_order()`：送單前新增價格保底修正。
- `_confirm_and_place_order()`：送單成功後立即塞進 `my_orders`。
- `tests/test_core.py`：新增 `round_to_tick` 相關測試。
- `diag_mock_tkinter.py`：新增 `_MockTreeview`。

---

## ADR-019：圖表版面改為使用者可調整並持久化、修正委託單靜默漏失、小數點輸入保底方案

- **日期**：2026-07-12
- **狀態**：已採納 (已寫入 `stock_app_pro.py`，已用模擬情境重現原始 bug 並驗證修正)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者回報三件事：

1. 圖表空白第四次確認仍未解決，並明確要求：「還是可以讓我自行調整？
   調整好就鎖定，你就不用一直在猜。」
2. 「價格 NumLock 輸入小數點的功能又沒了」——ADR-016 的修法在使用者
   實際環境裡沒有生效。
3. 送單後系統日誌顯示「委託成功...📌 狀態: PendingSubmit」，但「我的
   委託單」清單裡完全沒有這筆資料。

第3項是最關鍵的功能性 bug，直接用使用者提供的真實日誌內容重現：
`_confirm_and_place_order()` 送單成功後，用
`order_id = getattr(trade.order, 'id', '') or getattr(order, 'id', '')`
取得委託 id，再用 `if order_id:` 判斷要不要塞進 `self.my_orders`。
**委託剛送出、狀態還是 `PendingSubmit` (尚未被交易所完全確認) 時，
`trade.order.id` 很可能還是空字串**，而我們自己組的 Order 物件也沒有
設定 `id`，兩者都是空的，`order_id` 就是空字串，`if order_id:` 判斷
為否，**整段程式碼安靜地什麼都不做，且外層包著 `except Exception: pass`
連例外都不會有**——這完全對得上使用者「日誌顯示成功但清單是空的」
這個症狀，且用模擬情境成功重現。

### 決定

1. **圖表版面改為使用者可調整、可持久化保存 (回應第1項)**：
   - `data/config_store.py` 新增 `DEFAULT_CHART_LAYOUT`、
     `load_chart_layout()`、`save_chart_layout()`，管理
     `margin_left/right/top/bottom`、`hspace`、
     `canvas_width_delta`/`canvas_height_delta` 這組數值，讀寫
     `chart_layout.json`。
   - `draw_chart()` 的 `fig.subplots_adjust()` 與 canvas 尺寸計算，
     改成讀取 `self.chart_layout` 而不是寫死的數字。
   - 新增「📐 版面微調」按鈕，開啟 `open_chart_layout_dialog()`：
     用 7 條滑桿 (5 個邊界比例 + 2 個像素微調值) 即時預覽——拖動滑桿
     用 150ms debounce 立即重繪，讓使用者能親眼看到調整效果，不用再
     靠 Claude 猜數值。「儲存目前設定」寫入 `chart_layout.json` 永久
     保存 (下次啟動自動套用)；「還原預設值」重置回程式內建起始值。
   - **這是明確回應使用者「你不用一直在猜」的訴求**：把最終決定權交給
     使用者，Claude 不再繼續在看不到畫面的情況下猜邊界數值。

2. **修正委託單靜默漏失的核心 bug (回應第3項)**：
   - `_confirm_and_place_order()` 裡，`order_id` 沒有取到真實值時，
     不再整段跳過，改為用「時間+商品+買賣+價格+數量」組一個
     `_pending_` 開頭的本地暫時 key，讓這筆單立刻顯示在「我的委託單」
     清單裡；同時記錄 `self._last_pending_order_key`/
     `self._last_pending_order_info` 供後續清理比對用。
   - `_handle_order_event()` (委託回報 callback) 收到帶真實 id 的委託
     事件時，如果商品代碼/買賣方向/數量都跟剛剛記錄的暫時項目吻合，
     會把暫時項目移除、換成帶正式 id 的一筆，減少重複顯示的機率
     (無法保證 100% 不重複，但至少不會再完全消失不見)。
   - 原本包住整段邏輯的 `except Exception: pass` 改成
     `except Exception as e: self.log_message(f"【我的委託單更新異常】{e}")`
     ——**任何例外都要被看見，不能再無聲無息地吞掉**，之後如果還有
     類似問題，系統日誌會直接告訴我們真正的原因，不用再靠猜。

3. **小數點輸入問題改用保證有效的替代輸入方式 (回應第2項)**：
   - ADR-016 綁定的 `<KP_Delete>` 在使用者實際環境裡沒有生效——可能
     是這台機器/這個 Windows 版本的鍵盤驅動在 NumLock 關閉時，回報的
     keysym 跟預期的 `KP_Delete` 不同 (不同 Windows 版本、鍵盤 layout、
     locale 組合，回報的鍵盤事件細節其實有相當差異，這是這次深入search
     才確認到的：Tk 在 Windows 上對數字鍵盤事件的處理「經常沒有明確
     文件、而且因環境而異」)。
   - 與其繼續猜測這台機器上小數點鍵到底送出哪個 keysym，改為**提供一顆
     一定有效的「.」按鈕**，放在價格輸入框旁邊：不管鍵盤事件細節為何，
     點這顆按鈕都能在游標位置插入句點。`<KP_Delete>` 綁定保留當作額外
     的便利 (如果剛好命中就直接生效)，但不再是唯一的解法，使用者永遠
     有一個保證能用的替代方案。
   - **明確不綁定 `<Delete>` (一般鍵盤的刪除鍵)**：原本考慮過把
     `<Delete>` 也綁進去增加命中機率，但這樣會讓使用者想用一般 Delete
     鍵刪除字元時被攔截、變成插入句點，是真正會造成新問題的做法，
     已經在動工過程中自己抓到並改正，沒有放進最終版本。

### 替代方案 (已考慮但未採用)

- **圖表留白第四次繼續調整 subplots_adjust 的固定數值**：明確被使用者
  否決 (「你就不用一直在猜」)，改為交給使用者自己調整並持久化保存。

- **委託單清單改成定期呼叫 `update_status()` 補齊可能漏掉的委託**：
  被否決，違反 shioaji 官方「使用限制」文件明確要求的「避免以
  update_status() 輪詢」。改用更穩健的「暫時 key 立即顯示 + callback
  到來後清理」機制，維持完全不輪詢的設計。

- **小數點輸入問題繼續嘗試綁定更多可能的 keysym 變體 (例如同時綁
  `<Delete>`、`<Decimal>`、各種可能名稱)**：部分採用 (保留
  `<KP_Delete>` 當額外便利)，但主要解法改為保證有效的按鈕，因為
  继续猜測鍵盤 keysym 在使用者實際機器上到底是什麼，已經猜錯兩次，
  不是值得繼續投入的方向；按鈕是不依賴猜測、百分之百可靠的方案。

### 後果 / 影響

- **正面**：圖表版面問題的解決權交還給使用者，不再需要 Claude 反覆
  猜測數值卻猜不中；委託單清單不會再有「送單成功但清單是空的」這種
  嚴重影響使用信心的 bug，且往後如果還有類似問題，例外會被記錄下來
  而不是無聲消失；價格輸入的小數點問題有了保證有效的解法，不再依賴
  對特定 Windows 環境鍵盤行為的猜測。

- **已完成的驗證 (這次特別針對回報的具體症狀做重現測試，而非只驗證
  "不崩潰")**：
  1. **直接重現使用者回報的 bug**：建構一個 `trade.order.id` 為空字串
     的假 Trade 物件 (模擬 PendingSubmit 狀態的真實情境)，呼叫
     `_confirm_and_place_order()`，驗證修正前的邏輯會導致
     `my_orders` 維持空字典 (重現 bug)，修正後的邏輯會用 `_pending_`
     開頭的暫時 key 讓這筆單正確顯示。
  2. 驗證暫時 key 收到吻合的真實委託回報後，會被正確清除並換成帶
     正式 id 的一筆。
  3. 驗證小數點保底按鈕正確在游標位置插入句點。
  4. 驗證版面微調對話框 (含 7 條滑桿) 建構不會崩潰；驗證
     `chart_layout.json` 的儲存/讀取 round-trip 正確。
  5. 連同先前所有測試，共 51 項全數通過。
  6. `python -m py_compile` 語法編譯通過；AST 掃描確認沒有找不到定義
     來源的 `self.xxx` 引用、沒有重複定義的方法。
  7. `tests/test_core.py` (`core/`/`data/` 層) 不受本次修改影響，
     40 個測試依然全數通過。

- **需要使用者配合驗證的部分 (無法在此環境完成)**：
  - **這是最重要的部分**：用模擬帳號重新下單測試，確認這次「我的委託
    單」清單真的會顯示這筆單 (即使一開始用的是暫時 key，之後應該會
    被替換成正式的一筆，不應該重複太久)。
  - 開啟「📐 版面微調」對話框，拖動滑桿確認即時預覽有反應；調整到
    滿意的版面後點「儲存目前設定」，重新啟動 App 確認設定有被記住。
  - 測試「.」按鈕能不能正常在價格欄位插入句點；如果方便的話，也請
    確認一下您的 NumLock 狀態與鍵盤類型 (筆電內建數字鍵區 / 外接
    數字鍵盤 / 其他)，這個資訊如果之後還有類似問題會很有幫助。

- **待補**：如果版面微調對話框的滑桿範圍 (例如寬度微調 ±150px) 不夠
  用，或者需要更精細的調整單位，可以再放寬滑桿的 `from_`/`to`/
  `resolution` 參數，這屬於數值微調，不需要另開 ADR。

### 相關程式位置 (`stock_app_pro.py` / `data/config_store.py` / `diag_interaction_paths.py`)

- `data/config_store.py`：新增 `DEFAULT_CHART_LAYOUT`、
  `load_chart_layout()`、`save_chart_layout()`。
- `__init__`：新增 `self.chart_layout_file`、`self.chart_layout`、
  `self._last_pending_order_key`、`self._last_pending_order_info`。
- `draw_chart()`：`subplots_adjust`/canvas 尺寸改讀 `self.chart_layout`。
- 新增方法：`open_chart_layout_dialog()`、`_insert_decimal_point_button()`。
- `create_widgets()`：新增「📐 版面微調」按鈕；`entry_price` 新增
  「.」按鈕。
- `_confirm_and_place_order()`：修正委託 id 為空時的靜默漏失。
- `_handle_order_event()`：新增暫時 key 清理邏輯。
- `diag_interaction_paths.py`：新增重現原始 bug 的測試案例。

---

## ADR-026：重新登入死循環修正 — 斷線誤判收斂、斷線即釋放舊連線、重登一律建立全新 Shioaji 物件

- **日期**：2026-07-13
- **狀態**：已採納 (假環境 17 項驗證通過;實際重登需實機確認)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者回報:已登出券商官網,回到系統重新登入,卻一直出現「【連線中斷】...
同一帳號同時間只能有一個生效」與「查無 00692 資料」,無法登入。

### 根因 (兩個互相加乘)

1. **重登用同一個壞掉的 Shioaji 物件**:`self.sj_api` 在程式啟動時建立一次,
   之後所有重登都對同一物件呼叫 `login()`。session 死掉後
   `_mark_session_dead` 只撥旗標,既沒 logout 釋放券商端舊 session,也沒換
   新物件——重登是對「客戶端內部狀態已壞 + 券商端舊 session 還佔名額」的
   殭屍物件操作,券商回「同一帳號僅一個生效」拒絕。不重開整個程式繞不出來。
2. **斷線誤判太寬鬆**:`_looks_like_session_dead` 關鍵字含泛用的
   `"session"` / `"NotReady"` / `"not ready"` / `"connection error"`。
   登入後暫時性錯誤 (如合約未就緒) 也被判成斷線 → api_logged_in 撥回
   False → 一秒後自動重載 00692 又報「查無資料」→ 使用者截圖的兩行訊息。

### 決定

1. **誤判收斂**:只認明確斷線字樣 (`SessionNotEstablished` /
   `session error` / `session expired` / `session not established` /
   `token expired` / `connection lost` / `disconnected`);泛用字樣移除。
2. **斷線即釋放**:`_mark_session_dead` 背景 best-effort `logout()` 舊連線,
   主動釋放券商端名額;訊息同步更新 (含「重登會建立全新連線」與
   「立即重登失敗請等 1-2 分鐘」指引)。
3. **重登一律全新連線** (`process_broker_login`):先 `logout()` 舊物件
   (失敗屬預期,吞掉) → `sj.Shioaji(simulation=False)` 建全新物件 →
   `current_contract` 作廢 (強制下次查詢完整重訂閱) → 登入。
4. **錯誤分段回報**:login 失敗 (含三種常見原因指引) / activate_ca 失敗
   (不擋報價,僅提示下單需憑證) / callback 初始化失敗,各自明確訊息,
   不再混成一句「登入或憑證失敗」。

### 替代方案（已考慮未採用）

- **登入失敗自動重試 N 次**:券商端釋放舊 session 需要時間,盲目快速重試
  可能觸發風控;改為明確指引使用者等 1-2 分鐘,由人決定重試時機。
- **保留同一物件、只呼叫 logout 再 login**:shioaji 物件內部 (WebSocket/
  token) 在異常斷線後狀態不可信,全新物件才乾淨;成本只是重新下載合約。

### 後果 / 影響

- **正面**:斷線後點一次「登入券商實盤 API」即可復原,不必重開程式;
  暫時性錯誤不再被誤標成斷線;每種失敗都有明確原因與下一步指引。
- **注意**:重建物件會重新下載合約 (數秒);舊連線的 Trade 物件作廢,
  跨連線刪改由 `_find_trade_for_order` 的 list_trades 備援處理 (ADR-023)。
- **已完成的驗證** (17 項專項 + 診斷 11 案例全過):真斷線字樣仍判斷/
  暫時性錯誤不誤判;斷線時舊連線被 logout;重登建立全新物件、殭屍被釋放、
  contract 作廢、登入完成;login 失敗訊息含等待指引且旗標維持 False;
  憑證失敗不擋報價。
- **需使用者實機驗證**:重現「官網互踢」後,確認在本系統點一次重新登入
  即可恢復 (若立即重登失敗,等 1-2 分鐘再試一次應成功)。

### 相關程式位置（`stock_app_pro.py`）

- `_looks_like_session_dead()`:關鍵字收斂。
- `_mark_session_dead()`:背景釋放舊連線 + 指引訊息。
- `process_broker_login()`:釋放舊連線 → 全新 Shioaji → contract 作廢 →
  分段錯誤回報;憑證失敗不擋報價。

---

## ADR-027：刪改功能可發現性 (加可見按鈕+雙擊提示)、對話框防呆、委託回報先到 race 防重複、渲染狀態診斷

- **日期**：2026-07-13
- **狀態**：已採納 (假環境 8 項 + 診斷 12 案例驗證通過)
- **對應 shioaji 版本**：1.5.6

### 背景

使用者回報「我的委託單」看不到刪改功能,並要求特別確認之前發生過的
「字體與底色同色導致看不到」狀況。另附截圖:委託回報日誌顯示「已委託」,
但表格狀態欄仍停在 PendingSubmit。

### 排查

1. **刪改功能其實存在且完整** (ADR-023),但入口只有「雙擊委託列」一個,
   介面上**沒有任何可見提示或按鈕**——這是可發現性 (discoverability) 問題,
   不是顏色問題。逐一核對對話框配色 (深底 #1A2026 / 白字 / 橘紅按鈕),
   沒有 P-33 那種同色隱形。
2. 截圖「日誌已委託但表格 PendingSubmit」的疑點:既有「畫面已更新」日誌
   只印列數,無法判斷是資料層還是渲染層,需要更強的診斷。
3. 分析發現一個真實 race:shioaji 委託回報可能比 `place_order()` 回傳
   更早抵達 (callback 在網路等待期間先到),此時正式項目已入清單,seed 再
   建暫時項目會永久重複兩筆。

### 決定

1. **可見入口** (可發現性):委託單分頁上方加操作列——明顯的
   「🛠 刪改選取委託 (刪單/改量/改價)」橘色按鈕 (選取列後點擊) + 「💡 也可
   直接雙擊委託列」提示文字。按鈕與雙擊共用 `_open_modify_for_iid`。
2. **雙擊強化**:改用 `identify_row(event.y)` 取「滑鼠實際點到的列」,
   比 focus/selection 不易抓錯;抓不到才退回 focus/selection。
3. **對話框防呆**:改量/改價 Entry 明確設 `bg=#2A323D fg=#FFFFFF
   insertbackground=#FFFFFF` (不倚賴預設,杜絕同色隱形);對話框與確認視窗
   都 `lift()+focus_force()`,不會默默開在主視窗後面。開啟時印日誌
   「開啟刪改視窗 (雙擊/按鈕): ...」,確認入口有觸發。
4. **回報先到 race 防重複**:seed 取得空 order_id 要建暫時項目前,先掃描
   清單有無「同商品/同買賣/同量、10 秒內」的正式項目,有就不建暫時項目、
   直接刷新返回。
5. **渲染狀態診斷**:「畫面已更新」日誌附上「實際渲染的最新一列」的商品與
   狀態。下次若「日誌已委託、畫面卻 PendingSubmit」,一看即知是渲染層
   (最新列已是已委託但畫面沒更新) 還是資料層。

### 後果 / 影響

- **正面**:刪改功能一眼可見 (按鈕+提示雙入口);對話框欄位不再有同色風險、
  必定浮於最上;委託回報無論比 place_order 早到或晚到都不重複;診斷日誌
  能決定性區分資料層/渲染層問題。
- **已完成的驗證** (8 項專項 + 診斷 12 案例全過):未選取不開/選取按鈕開/
  雙擊開;診斷日誌含最新列商品與狀態;回報先到時不建重複 (清單 1 筆)、
  回報未到時正常建暫時項目。`tests/test_core.py` 52 項、py_compile、AST 全過。
- **需使用者實機驗證**:
  1. 「我的委託單」分頁是否看到橘色「🛠 刪改選取委託」按鈕與雙擊提示。
  2. 點選一筆委託→按按鈕 (或雙擊列)→對話框是否正常彈出且欄位文字清晰可見。
  3. 若截圖那筆仍顯示 PendingSubmit:看日誌「畫面已更新: ... 最新列: 0050
     XXX」——若 XXX 已是「已委託」但畫面仍 PendingSubmit,回報該行以鎖定
     渲染層;若最新列也還是 PendingSubmit,代表該筆的正式回報 op_type 非
     New 或未帶對應 id,回報整行日誌續查。

### 相關程式位置（`stock_app_pro.py`）

- `create_widgets()`:委託單分頁加操作列 (按鈕 + 提示)。
- 新增:`_on_modify_button_click()`、`_open_modify_for_iid()`。
- `_on_order_row_double_click()`:改用 identify_row。
- `_open_order_modify_dialog()` / `_confirm_modification()`:Entry 明確配色、
  視窗 lift+focus_force。
- `_confirm_and_place_order()` seed 段:回報先到 race 防重複。
- `_refresh_my_orders_ui()`:診斷日誌附最新列狀態。
- `diag_repro_issues.py`:新增 ADR-027 案例。

---

## ADR-028：市場切換 (台股/台期貨/美股) 與自選股即時報價欄

- **日期**：2026-07-14
- **狀態**：已採納 (假環境 20 項驗證通過;實際報價與新期貨代號需實機確認)
- **對應 shioaji 版本**：1.5.6

### 背景 (使用者兩項需求)

1. 自選股欄位要顯示即時報價、漲跌點、漲跌幅;收盤後顯示最終報價。
2. 期貨英文代號 (TXF 以外,如 ZEF/CDF) 輸入後被誤判成美股;使用者提議
   加「台現貨/台期貨/美現貨」切換按鈕,並詢問其他期貨商品代號要怎麼找尋。

### 決定

**A. 市場切換 (頂欄「市場: [台股][台期貨][美股]」segmented 按鈕,預設台股)**
1. 判斷邏輯 (`fetch_data_worker` 依 UI 執行緒讀取後傳入的 market 參數):
   - **台股**:維持原自動判斷 (數字代碼→股票/ETF、^TWII/^TWOII→指數、
     TXF/MTX/FITX/MXF→期貨,向下相容)。
   - **台期貨**:任何英文代號一律走新的 `_resolve_futures_contract()` 通用
     解析——`Contracts.Futures.<代號>` 取 `<代號>R1` 連續合約,無 R1 退近月;
     別名表 `FUT_ALIASES` (MTX→MXF、FITX→TXF)。
   - **美股**:直接走 yfinance,純英文代號不再被其他規則攔截。
2. **期貨代號找尋** (`_log_futures_candidates`):台期貨模式查無代號時,
   走訪 `Contracts.Futures` 列出可用商品代號與名稱 (輸入有部分吻合就只列
   相近的),直接回應「其他期貨商品代號要怎麼找尋」。
3. **下單合約解析同步通用化**:`execute_order` 原本硬編四個期貨代號,
   改為 `asset_type=='future'` 時直接用目前圖表的 `current_contract`
   (查詢時已解析好的 R1),否則新期貨「能看圖不能下單」。
4. 點自選股列自動切市場模式:含數字/^開頭→台股;期貨合約存在或舊代號→
   台期貨;其餘→美股。

**B. 自選股即時報價 (Listbox → Treeview:代碼|成交|漲跌|幅度%)**
1. `watchlist_quote_worker` 每 10 秒一輪:目前群組代碼 (UI 執行緒維護的
   `_wl_current_syms` 快照) → `_resolve_wl_contract` 解析 (股票/期貨/指數
   通吃,含登入世代快取 `_wl_contract_cache`,重登清空) → **一次批次**
   `snapshots()` (符合 P-03 流量節流) → `safe_after` 回 UI。
2. `_apply_wl_quotes`:只更新既有列的值與紅漲綠跌 tag (鐵則1),不重建
   列表、不動使用者的選取。收盤後 snapshot 回傳的就是最終收盤價,
   自然滿足「收盤後顯示最終報價」。未登入時各欄顯示 `--`。
3. 操作全面改寫為 Treeview API (上移/下移/刪除/群組切換);程式化選取
   (上移/下移/重建) 以 `_wl_select_suppress` 抑制 `<<TreeviewSelect>>`,
   避免誤觸查詢 (Listbox 的 selection_set 不發事件、Treeview 會發,
   這是兩元件的行為差異)。

### 替代方案（已考慮未採用）

- **自選股報價改用訂閱串流**:同時訂閱整個群組會佔用訂閱額度、且換群組要
  大量退訂/重訂;10 秒批次 snapshot 對「清單總覽」已足夠即時,主圖商品
  另有專屬串流。
- **watchlists.json 改存 (市場,代碼) 結構**:會破壞既有檔案相容;改用
  「點選時自動判斷市場」達到同樣效果,零遷移成本。

### 後果 / 影響

- **正面**:自選股一眼看到全群組現價/漲跌/幅度 (紅漲綠跌);任何台灣期貨
  商品都能用英文代號查詢、看圖、下單;查無代號時系統直接列出可用商品;
  美股模式不再被誤判。
- **已完成的驗證** (14 項專項 + 診斷 13 案例全過):ZEF 在台期貨模式解析為
  期貨並出圖;查無代號列出候選;美股模式 TXF 走 yfinance;台股模式 TXF
  向下相容;4 檔混合 (股/期/指數) 一次批次 snapshot、值與紅綠正確、
  更新不清選取;點列自動切市場;抑制旗標防誤觸;未登入安靜跳過。
  `tests/test_core.py` 52 項、py_compile、AST 全過。
- **需使用者實機驗證**:
  1. 頂欄「市場」三鍵是否顯示;台期貨模式輸入 TXF/MXF 以外的代號
     (例如 ZEF) 是否能出圖;故意輸入錯的代號看候選清單。
  2. 自選股各列的報價/漲跌/幅度是否在登入後 10 秒內出現並持續更新;
     紅漲綠跌;收盤後顯示收盤價。
  3. 點自選股列:市場模式是否自動切對、圖表正確載入;上移/下移不會
     誤觸重新查詢。
- **注意**:自選股報價最快 10 秒更新一輪 (P-03 節流);要更即時可調
  `watchlist_quote_worker` 的 sleep,但需留意 snapshots 流量配額。

### 相關程式位置（`stock_app_pro.py`）

- 新增:`FUT_ALIASES`、`_resolve_futures_contract`、`_log_futures_candidates`、
  `_resolve_wl_contract`、`_wl_fetch_quotes_once`、`_apply_wl_quotes`、
  `watchlist_quote_worker`、`_wl_selected_sym`、`_wl_select_programmatic`、
  `_wl_fmt_quote`。
- 修改:頂欄市場 segmented 按鈕;`start_fetch_thread`/`fetch_data_worker`
  帶 market 參數與新判斷;`execute_order` 期貨合約解析;自選股 Treeview
  與全部操作方法;`process_broker_login` 清 `_wl_contract_cache`。
- `diag_mock_tkinter.py`:Treeview `item()` 支援 values 更新 (忠實模擬)。
- `diag_repro_issues.py`:新增 ADR-028 案例。

---

## ADR-029：第九輪六項 — 價量列自適應、委託單交易別欄、布林雙組自訂、期貨/指數報價延遲根因修正、自選股中文名稱、中文名稱搜尋

- **日期**：2026-07-14
- **狀態**：已採納 (離線 18 項專項 + 診斷 14 案例驗證通過)
- **對應 shioaji 版本**：1.5.6

### 使用者六項需求與決定

**1. 量欄位被視窗截掉** — 「量」原本與「價」擠同一列,面板窄就看不到單位。
拆成獨立一列 (qty_row_frame),各自 fill=X,任何寬度下完整可見。

**2. 委託單加「交易別」欄** — orders_cols 於商品與買賣之間插入 "lot" 欄,
顯示 `MODE_LABELS[order_lot]` (整股/盤中零股/盤後定價/盤後零股)。診斷日誌
「最新列」索引隨欄位位移更新;diag 既有案例斷言同步修正。

**3. 布林通道自訂參數 + 上下限各兩組** — `bb_period`/`bb_std1`/`bb_std2`
三個 tk 變數,主圖設定對話框提供期間/σ1/σ2 欄位 (σ2=0 不畫第二組,預設
20/2.0/3.0)。`core/indicators.calculate_indicators` 簽名尾端加
`bb_period=20, bb_std1=2.0, bb_std2=0.0` (預設值向下相容既有呼叫),σ2>0 時
產出 `BB_UPPER2/BB_LOWER2`;繪圖以同色點線疊加;hover 文字含上2/下2。

**4. 指數/台指期報價太慢 (當沖來不及) — 找到真正根因**:
`on_tick_fop_v1` 用 `tick.code == current_contract.code` 過濾,但訂閱 R1
連續合約 (TXFR1) 時 shioaji 推送的 tick.code 是**實際月份合約** (如 TXFG6),
比對永遠失敗 → 期貨串流全被丟棄 → 永遠退 5 秒快照 fallback。修正:
`_fop_code_match()` 商品前綴 (前3碼) 比對 (一次只訂一檔期貨,不會誤收;
完全相等亦接受)。修正後台指期回到 **0.5 秒串流更新**。另頂欄大盤/櫃買
指數快照從 30 秒縮到 **5 秒** (一次批次 2 檔,符合 P-03 ≥5 秒底線)。
指數若有串流走 0.5 秒,無串流最差 5 秒。

**5. 自選股加中文名稱欄** — tree_wl 插入「名稱」欄,`_wl_display_name()`
從 `_resolve_wl_contract` 的合約 name 取 (指數固定名;未登入 '--',登入後
報價更新時自動補上);報價刷新只更新報價三欄,名稱保留。

**6. 中文名稱搜尋 (股票+期貨)** — 查尋/載入輸入含 CJK 字元即走搜尋流程:
`_search_contracts_by_keyword` 走訪 Contracts.Stocks 與 Contracts.Futures
全部合約 (各月份+R1/R2,股票期貨同在 Futures 內一併涵蓋),比對名稱與代號;
多筆結果開卷軸 Treeview 對話框 (市場|代碼|名稱|說明) 雙擊/按鈕選用,
唯一結果直接載入;選用即自動切市場模式。`_resolve_futures_contract` 擴充
支援完整合約代號 (TXF202609 特定月份、TXFR2 次月連續:前3碼找群組再取
完整代號)。註:「一般盤/全日盤」非合約屬性,K線與串流本就含日盤+夜盤
全日資料 (期貨聚合依 ADR-007 交易日切割)。

### 後果 / 影響

- **已完成的驗證** (18 項專項 + 診斷 14 案例全過):交易別欄顯示正確;
  布林自訂期間/σ比例/σ2=0 關閉/向下相容;TXFG6 tick 被 TXFR1 訂閱接受、
  MXF 不誤收;自選股名稱正確且報價更新後保留;中文搜尋同時命中股票與
  期貨各合約、說明正確;TXF202609/TXFR2 解析正確;唯一結果自動載入;
  含中文輸入觸發搜尋。`tests/test_core.py` 52 項、py_compile、AST 全過。
- **需使用者實機驗證**:
  1. **第4項最重要**:盤中看台指期,報價是否已達每秒級跳動 (修正前是
     5 秒一跳);頂欄大盤指數 5 秒一跳。
  2. 視窗縮窄,量的輸入框與單位仍完整可見。
  3. 委託單新欄「交易別」;布林設定期間/σ1/σ2 並確認第二組點線與 hover。
  4. 自選股名稱欄;中文搜尋 (如輸入「台積電」) 出現股票+期貨各月份合約
     選單,選期貨月份合約能載入並下單。
- **注意**:第4項的前綴比對假設「同時只訂閱一檔期貨」——若未來做多檔
  期貨同時串流,需改為 per-symbol 暫存,屆時另開 ADR。

### 相關程式位置

- `stock_app_pro.py`:qty_row_frame;orders_cols+lot;bb_period/std1/std2 與
  設定 UI 與繪圖/hover;`_fop_code_match`+fop callbacks;指數 worker 5s;
  tree_wl name 欄+`_wl_display_name`;`_search_contracts_by_keyword`/
  `_symbol_search_worker`/`_open_symbol_search_dialog`/`_load_search_result`;
  `_resolve_futures_contract` 完整代號;`start_fetch_thread` CJK 偵測。
- `core/indicators.py`:bb 參數化 (簽名尾端,向下相容)。
- `diag_repro_issues.py`:欄位位移斷言修正 + 第九輪回歸案例。

---

## ADR-030：第十輪四問題 — 舊版 core 模組導致整圖不畫 (降級保護)、五檔被擠出視窗 (pack 優先權)、關閉卡死 (logout 限時)

- **日期**：2026-07-14
- **狀態**：已採納 (離線 9 項專項 + 診斷 15 案例驗證通過)
- **對應 shioaji 版本**：1.5.6

### 使用者四個問題與根因

**問題1+3 (畫布繪製失敗 bb_period / 主圖無K線)**:使用者機器上的
`core/indicators.py` 仍是舊版 (上輪交付的新版 indicators.py 需放到
`G:\StockBuild\core\` 覆蓋,使用者只更新了主程式)。新主程式以新參數呼叫
舊簽名 → TypeError → draw_chart 中斷 → 整張圖空白,每次重繪都報錯。
**教訓:跨檔案的簽名變更,主程式必須能容忍附屬模組版本落後**——絕不能
因一個附屬模組舊,讓主功能全掛。

**問題2 (五檔不見)**:五檔是左側面板「最後 pack」的元件;tkinter 空間
不足時**後 pack 的先被擠出**。第九輪把價/量拆兩列後面板長高,五檔資料列
被擠出視窗下緣 (標題列剛好卡在邊緣,與截圖完全吻合)。

**問題4 (程式無法關閉,「沒有回應」)**:`on_app_close` 在主執行緒**同步**
呼叫 `sj_api.logout()`;session 殭屍/網路異常時 logout 永不返回,主執行緒
被卡死,視窗 Not Responding、連關閉都不行。

### 決定

1. **布林參數 fail-safe 降級** (`calculate_custom_indicators`):先以新參數
   呼叫;`TypeError` 時降級用舊簽名 (布林回固定 20,2),圖表照常運作,
   並印一次性「版本提示」指引使用者把新版 indicators.py 覆蓋到 core/。
2. **五檔 pack 優先權**:`five_level_frame` 改為「先 pack + side=BOTTOM」
   ——pack 順序決定空間分配優先權、side 決定位置,五檔永遠貼底可見;
   視覺順序不變 (成交明細在上)。空間不足時被壓縮的改為成交明細滾動列表
   (height 同步 5→4,取回第九輪加高的量列空間)。
3. **關閉限時**:logout 改背景執行緒 + `join(timeout=3)`;逾時直接放行,
   `os._exit(0)` 保底強制結束 (shioaji 連線隨行程消滅;重登流程 ADR-026
   會先 logout 舊連線,不受殭屍 session 影響)。

### 後果 / 影響

- **已完成的驗證** (9 項專項 + 診斷 15 案例全過):舊簽名降級成功回傳、
  版本提示只印一次;新版路徑自訂參數不受影響;logout 卡死 60 秒的假 API
  下,關閉 3.0 秒完成且 destroy/os._exit 都執行;五檔 pack 順序與
  side=BOTTOM 程式碼層驗證。`tests/test_core.py` 52 項、py_compile
  (無 SyntaxWarning)、AST 全過。
- **交付安裝注意 (本輪問題的直接起因)**:兩個檔案、兩個路徑——
  `stock_app_pro.py → G:\StockBuild\`;`indicators.py → G:\StockBuild\core\`
  (覆蓋)。就算忘了第二個,降級保護也會讓圖表照畫並在日誌明確指引。
- **需使用者實機驗證**:(1) 覆蓋兩檔後重啟,K線圖恢復、不再出現 bb_period
  錯誤;(2) 視窗任意縮放,五檔永遠可見 (成交明細變矮是預期行為);
  (3) 按 X 關閉,3 秒內必定關閉。

### 相關程式位置（`stock_app_pro.py`）

- `calculate_custom_indicators()`:TypeError 降級 + `_bb_param_warned` 一次性提示。
- `create_widgets()`:five_level_frame 先 pack + side=BOTTOM;成交明細 height 4。
- `on_app_close()`:logout 背景執行緒 + join(3s)。
- `diag_repro_issues.py`:新增 ADR-030 回歸案例。

---

## ADR-031：第十一輪 — hover 漲跌點數、「我的庫存」分頁與完整明細視窗、TXFR2 含數字誤判修正、美股自選股報價 (yfinance)

- **日期**：2026-07-15
- **狀態**：已採納 (離線 13+13 項專項 + 診斷 16 案例驗證通過)
- **對應 shioaji 版本**：1.5.6

### 需求/問題與決定

**1. hover 資訊列補漲跌點數**:原本只有漲跌%。新格式
「漲跌: ▲ 523.45 (▲ 1.61%)」——點數與百分比並列。

**2. 「我的庫存」分頁 + 完整明細視窗**:
- 底部第四分頁「我的庫存」:🔄更新庫存按鈕、🔍完整明細視窗按鈕、摘要列
  (檔數|總未實現損益|更新時間)。表格欄位:帳戶|代碼|名稱|方向|庫存量|
  均價|現價|未實現損益|報酬率% (紅賺綠賠)。
- 資料來源 `list_positions`:證券帳戶 (單位張) 與期貨帳戶 (單位口) 各查
  一次,**按需查詢** (按鈕/切入分頁時),不做背景輪詢;查詢跑背景執行緒
  (防重入 `_positions_loading`),套用走「先備妥再刪插」(P-31)。
- 報酬率 = (現價-均價)/均價×100,賣方向反號 (與數量單位無關,證/期通用)。
- 完整明細視窗:`_position_to_dict` 防禦式讀出 Position 物件**全部欄位**
  (`__dict__` 或 dir() 掃描),欄位=各檔 key 聯集 (常用排前),雙向卷軸
  Treeview,底部總損益 + 重新查詢按鈕——「看到全部詳細的數字」。

**3. TXFR2 載入失敗 / 自選股整列 '--' (使用者截圖)** — 根因:市場自動判斷
與自選股報價解析用「含數字=台股」,但**期貨完整代號本來就含數字**
(TXFR2 的 2、TXF202609)。被誤判成台股 → Contracts.Stocks 查無 → 報錯;
報價解析同路徑 → 整列 '--'。修正:新增 `_looks_like_futures_symbol()`
(3 英文字母 + R1/R2 或 6 位月份數字),在四處**先於**「含數字」判斷:
點自選股市場自動切換、`_resolve_wl_contract`、台股模式的台灣商品判定、
台股模式的期貨分支。台股模式手動輸入 TXFR2 也能直接載入。
另:兩段式載入「快速段已出圖、完整段失敗」時,訊息改為「背景補全失敗,
先顯示近期資料」,不再誤導成整個載入失敗。

**4. 美股自選股無報價 (使用者截圖 SPYM)** — 自選股報價 worker 原本只走
shioaji snapshot,美股不在券商合約內被跳過。修正:分類原則「登入時以
shioaji 解析結果為準——解析得到=台灣商品 (TXF/CDF 等期貨代號也是純英文,
不能用字元特徵猜,P-42 同源);解析不到的純英文=美股」,美股走 yfinance
fast_info (現價/昨收→漲跌),約 30 秒一輪 (每 3 輪抓一次,首次立即),
名稱取 shortName 快取 (失敗顯示「美股」佔位,取得後自動補上)。
未登入時僅對 4 碼以上純英文抓美股 (排除 3 碼期貨商品代號的誤抓風險)。

### 後果 / 影響

- **已完成的驗證** (庫存 13 項 + 路由/美股 13 項 + 診斷 16 案例全過):
  兩帳戶各查一次;證/期列內容、賣方向報酬率反號、摘要總損益;防重入;
  未登入擋下;明細視窗含全部原始欄位;背景執行緒完整流程;TXFR2/
  TXF202609 樣式判斷、點選自動切台期貨、台股模式手動輸入可載入;
  TXFR2 報價走 shioaji、SPYM 走 yfinance 且互不干擾 (TXF 不會被誤送
  yfinance、名稱各自正確)。`tests/test_core.py` 52 項、py_compile、AST 全過。
- **需使用者實機驗證**:
  1. 游標資訊列顯示「漲跌: ▲ 點數 (▲ %)」。
  2. 「我的庫存」分頁:切入自動查詢;內容與券商 App 核對 (特別是**證券
     庫存量單位是否為張**——shioaji 預設 Common 單位,若顯示的是股數,
     回報我改單位換算);明細視窗欄位齊全。
  3. 點自選股 TXFR2:圖表載入、報價出現,不再報「合約查無」。
  4. 美股自選股 (SPYM 等):約 30 秒內出現報價與名稱。
- **注意**:美股報價為 yfinance 免費源 (延遲行情),頻率刻意壓在 30 秒;
  庫存為按需查詢,要最新數字請按 🔄。

### 相關程式位置（`stock_app_pro.py`）

- hover:`on_mouse_move` chg_val。
- 庫存:分頁 UI、`refresh_positions`/`_positions_refresh_worker`/
  `_positions_fetch_once`/`_position_to_dict`/`_apply_positions`/
  `_open_positions_detail_window`、`set_bottom_tab`。
- 路由:`_looks_like_futures_symbol` + 四處判斷順序、兩段式失敗訊息。
- 美股:`_is_us_symbol`/`_wl_fetch_us_quotes`、`_wl_fetch_quotes_once` 分類、
  `_wl_display_name` 先合約後美股。
- `diag_mock_tkinter.py`:winfo_x/winfo_y。`diag_repro_issues.py`:ADR-031 案例。

---

## ADR-032：登入流程凍結 (「無法連線」/「程式無反應」/「無法關閉」) — 根因分析與緩解

- **日期**：2026-07-15
- **狀態**：已採納 (離線 10 項專項 + 診斷 17 案例驗證通過;GIL 層根因無法在
  沙盒環境對真實 shioaji 驗證,詳見下方「誠實的侷限」)
- **對應 shioaji 版本**：1.5.6

### 使用者回報的三個症狀 (同一次事件的三個面向)

日誌顯示登入卡在「連線至券商伺服器並下載最新合約檔中...」之後就不再前進,
視窗被 Windows 標記「沒有回應」,連關閉都關不掉 (使用者截圖的對話框是
**Windows 系統原生的「應用程式沒有回應」對話框**,不是本程式跳出的視窗)。

### 根因分析

`process_broker_login` 已經是在**背景執行緒**執行 (登入按鈕點下去就丟給
`threading.Thread`,不是主執行緒直接呼叫)。逐行核對程式碼,沒有找到我們
自己的死鎖或同步阻塞呼叫——問題出在再往下一層:`self.sj_api.login(...)`
這一步,shioaji 需要重新下載/解析全部合約資料 (股票+ETF+期貨各月份等)
時,是一段**CPU 密集、同步執行的 Python/C 混合解析工作**。CPython 的 GIL
(全域直譯器鎖) 若被這段解析長時間佔用而沒有適時釋放控制權,其他 Python
執行緒 (包含 Tk 主事件迴圈) 就完全排不到執行機會——這與「是不是背景
執行緒」無關,是同一個行程內所有執行緒共用一把鎖的本質限制。Tk 事件迴圈
排不到執行機會,無法回應 Windows 訊息,Windows 偵測到視窗數秒沒有回應
訊息,就跳出系統對話框；若 GIL 真的被完全佔滿不放,連我們自己綁的
`on_app_close` (WM_DELETE_WINDOW 處理器) 也一樣排不到執行機會，這解釋了
「連關閉都關不掉」——此時 Windows 對話框裡的「關閉程式」(OS 層級
TerminateProcess) 才是唯一能立即生效的手段，這與我們的程式碼無關，是
作業系統本來就提供的安全閥 (使用者截圖中已經看得到這個選項)。

這是使用第三方編譯 SDK (如 shioaji) 常見的一類已知限制，無法單靠在同一
行程內開執行緒來完全避免；真正結構性的解法是把整段登入流程隔離到**獨立
行程** (multiprocessing) 執行。考量到:
1. 后续所有操作 (报价串流 callback、下单、K 线) 都依赖同一个 in-process
   的 `self.sj_api` 物件与其绑定的 Python callback,搬到独立行程需要重新
   设计整个即时数据管线的跨行程通讯,风险与工作量都远超本轮範圍；
2. 这个沙盒环境无法连上真实 shioaji 服务器，任何"修好了 GIL 卡顿"的宣称
   都无法验证到底有没有真的解决 (只能验证我们自己程式码层面的行为)；

本轮采取「诚实分层」的作法:先做**可验证、低风险、确定有帮助**的修正,
并对使用者说明清楚这个限制的本质，而不是假装"已完全修好"一个我们无法
在沙盒验证的第三方函式库行为。

### 决定 (三项确定可验证的修正)

1. **移除一个真实的、我们自己程式码里的潜在卡住来源**:`process_broker_login`
   原本每次都对「上一个 sj_api 物件」呼叫 `logout()`，包含程式刚启动、
   从来没有登入成功过的全新物件。若 shioaji 对「从未建立过连线」的物件
   呼叫 logout() 处理不够干净 (例如等待一个永远不会来的中断确认)，反而
   会增加卡住的风险且难以排查。修正为：**只有 `self.api_logged_in` 为
   True (代表这个物件真的曾经登入成功过) 才呼叫 logout()**，且一律包在
   「背景执行绪 + 3 秒逾时」内 (与 ADR-030 的 on_app_close 手法一致)。
2. **防止使用者在「看似没反应」时误触，叠加第二个 login 执行绪抢同一组
   shioaji 资源**:新增 `_login_in_progress` 旗标。登入进行中：
   - `toggle_login` 直接挡下重复点击 (不重开对话框、不起第二个执行绪)，
     并提示「登入正在进行中，请耐心等候」；
   - 登入按钮文字改成「⏳ 连线中...请稍候」的视觉状态；
   - 无论成功/失败/例外，`process_broker_login` 的 `finally` 保证清除旗标，
     失败时按钮也保证复原成可再次点击 (不会卡在「连线中」状态永远点不动)。
   两个几乎同时触发的 login 会让 GIL 争用倍增、冻结更久，这是我们程式码
   层面可以确定预防、也确定有帮助的修正。
3. **及早且持续设定正确的使用者预期** (`_start_login_watchdog`)：登入一
   开始就提示「首次登入可能需要 30 秒到 2 分钟，画面可能显示没有回应，
   这是 shioaji 下载合约时的已知现象，请耐心等候、不要强制关闭」；之后
   每 30 秒补一次「已等待约 N 秒」的提示，直到登入结束 (成功/失败) 自动
   停止。这则消息本身也要等 GIL 有空档时才排得进去，如果真的是长时间
   完全冻结，这则提示可能会delay，但至少一旦有任何空档就会立刻显示，
   把「看起来像当机」转换成「已知道在等什么、要等多久」。

### 诚实的局限 (无法在本轮验证/解决的部分)

- 若 shioaji 的 `login()` 内部真的是长时间**完全不释放 GIL** 的纯 CPU/C
  阻塞调用，以上三项修正**不能让画面在那段期间维持流畅**——它们能做到
  的是：不再有「我们自己代码造成的额外卡住」、不会因为重复点击而冻结
  更久、使用者会看到清楚的等待预期讯息 (只要 GIL 有释放空档)。真正
  「登入期间画面完全不冻结」需要把 shioaji 会话隔离到独立行程，这是
  一个大得多的架构改动，建议先按本轮修正观察实机的实际冻结时长与频率
  (是数十秒的可恢复停顿，还是数分钟以上真的卡死)，再决定是否值得投入
  独立行程的重构。
- 因为本沙盒环境无法连线真实 shioaji 服务器，本轮所有验证都是针对**我们
  自己程式码的行为** (是否呼叫 logout、是否防重复点击、旗标/按钮是否
  复原、watchdog 是否发出/停止提示)，不是针对 shioaji 本身的 GIL 行为。

### 后果 / 影响

- **已完成的验证** (10 项专项 + 诊断 17 案例全过)：从未登入的物件不被
  logout、曾登入的物件会被 logout (背景执行绪+限时)；登入中重复点击被
  挡下并提示、不会启动第二个执行绪；登入失败时旗标与按钮文字都复原；
  watchdog 启动立即提示一次，旗标清除后自我停止不再增加提示。
  `tests/test_core.py` 52 项、py_compile、AST 全过；既有 ADR-026 案例
  同步更新为「曾登入过」情境 (与 ADR-032 的新前提区分两种案例)。
- **需使用者实机验证并回报**：这轮修正后，下次遇到登入卡顿时，请注意
  记录：(1) 系统日志与回报分页此时是否已经出现「登入中」的等待提示；
  (2) 大约卡多久会自己恢复 (还是真的完全没恢复、只能强制关闭)；这个
  实际数据能帮助判断是否值得投入独立行程重构。

### 相关程式位置（`stock_app_pro.py`）

- `__init__`：`_login_in_progress`/`_login_watchdog_id`。
- `toggle_login`：挡重复点击；登出改背景执行绪+限时。
- `open_login_dialog`/`do_login`：设定旗标、按钮视觉状态、启动 watchdog。
- 新增：`_start_login_watchdog`。
- `process_broker_login`：改为薄包装 (finally 清旗标+复原按钮)，实际逻辑
  移到 `_process_broker_login_impl` (只在 api_logged_in 为 True 时才
  logout 舊物件)。
- `diag_repro_issues.py`：新增 ADR-032 案例；既有 ADR-026 案例前提修正。

---

## ADR-033：庫存分頁證券數量單位改為「股」

- **日期**：2026-07-15
- **狀態**：已採納

### 背景

使用者核對「我的庫存」分頁數字與券商 App 後回報:數量單位要改成「股」。

### 決定

`_positions_fetch_once` 中證券帳戶的顯示單位由 `'張'` 改為 `'股'`。
shioaji `list_positions` 對證券帳戶回傳的 `quantity` 欄位本身就是股數
(不是張數,1張=1000股的換算不適用於這個欄位),ADR-031 當初標「張」是
單位誤植,數值本身沒有錯——只需改標籤,不需要任何數值換算。期貨帳戶
的「口」不受影響。

### 相關程式位置

- `_positions_fetch_once()`:證券帳戶 unit 由 '張' 改 '股'。

---

## ADR-034：庫存完整明細視窗欄位標題與數值全面中文化

- **日期**：2026-07-15
- **狀態**：已採納 (離線 10 項專項 + 診斷 18 案例驗證通過)

### 背景

使用者截圖:「庫存完整明細」視窗的欄位標題 (code/direction/quantity/
price/last_price/pnl/yd_quantity/id) 與方向欄位的值 (Action.Buy) 都是
shioaji 原始英文/enum 字串,要求全部改成中文。

### 決定

新增 `POSITION_FIELD_LABELS` 對照表,涵蓋 shioaji StockPosition/
FuturePosition 常見欄位 (code→代碼、direction→方向、quantity→庫存量、
price→均價、last_price→現價、pnl→損益、yd_quantity→昨日庫存、id→序號,
另預先涵蓋融資/融券/成本價等可能出現的欄位)。`_open_positions_detail_window`
的表頭改用 `_position_field_label()` 查表顯示;儲存格數值改用
`_position_field_display()`,目前只有「方向」欄位需要轉換 (借用既有的
`_normalize_action`,Action.Buy/Sell → 買進/賣出),其餘欄位原樣顯示數字。
**未列在對照表中的欄位** (不同帳戶類型/shioaji 版本可能有差異欄位) 保留
原始 key 當標題——寧可少一個中文名稱,也不能讓資料整欄消失,這與明細視窗
「看到全部詳細數字」的初衷 (ADR-031) 一致。

### 後果 / 影響

- **已完成的驗證** (10 項專項 + 診斷 18 案例全過):七個常見欄位標題正確
  轉換;方向值 Action.Buy→買進;未知欄位保留原 key 不遺漏;視窗建構不
  拋例外。`tests/test_core.py` 52 項、py_compile、AST 全過。
- **需使用者實機驗證**:開啟「我的庫存」→「開啟完整明細視窗」,確認
  欄位標題與方向欄位都已是中文;若看到某欄位仍是英文 (代表該欄位不在
  對照表裡,可能是期貨帳戶特有欄位),回報欄位名稱以便補進對照表。

### 相關程式位置（`stock_app_pro.py`）

- 新增:`POSITION_FIELD_LABELS`、`_position_field_label()`、`_position_field_display()`。
- 修改:`_open_positions_detail_window()` 表頭與儲存格改用上述兩個方法。
- `diag_repro_issues.py`:新增 ADR-034 案例。

---

## ADR-035：量化自動交易系統 (Phase 1) — 策略引擎、安全架構與 GUI

- **日期**：2026-07-16
- **狀態**：已採納 (引擎 26 項 + 整併 unittest 7 個測試方法 + GUI 端到端 21 項
  + 診斷 19 案例全過;實單路徑需依「實機驗證階梯」逐步上線,見下)
- **對應 shioaji 版本**：1.5.6

### 需求

使用者:策略寫好後,系統自動判斷買賣條件並自動下單 (含期貨);要求
(1) 好好規劃、(2) 不可讓現有系統崩潰、(3) 防亂下單 (自動下單無人工確認),
需要切換總開關防人為錯誤。

### 架構決定

**分層** (沿用專案既有紀律):
- `core/strategy_engine.py` (新檔,純邏輯,零 tkinter/零 shioaji):
  - **條件庫** 16 種:均線金叉/死叉、突破前N期高/跌破前N期低、價位上/下、
    布林上/下軌穿越、KD 金叉/死叉/超買/超賣、MACD 金叉/死叉、RSI 上/下門檻。
    指標自帶計算 (SMA/EMA/RSI/KD/MACD/BB),與圖表上使用者勾了什麼指標
    **完全脫鉤**。「交叉」類條件只在交叉那根K棒為 True,天生一次性。
  - **策略資料結構** `new_strategy()`:商品/市場/週期/方向/數量/讓價檔數/
    進場條件(AND)/出場訊號(OR)/停損%/停利%/每日次數上限/冷卻秒數/
    每日虧損熔斷/模式(模擬|實單)/enabled。`validate_strategy()` 完整驗證
    (含:至少一種出場方式、股票不可做空、數量 1~100 防呆)。
  - **狀態機** `evaluate_strategy()`:FLAT→(進場)→LONG/SHORT→(停損/停利/
    出場訊號)→FLAT;**只吃已收盤K棒** (呼叫端剔除最後一根,杜絕未完成
    K棒的假訊號/repaint);「同一根K棒只評估一次」閘門 (last_bar_ts)。
  - **風控守門** `risk_check()`:每日進場上限、冷卻、每日虧損熔斷;
    **出場單不受每日次數限制** (持倉一定要出得去)。
  - `apply_fill()`:狀態轉移 + 當日已實現損益累計 (價差×數量)。
- `stock_app_pro.py` GUI 層:
  - 底部第五分頁「量化交易」:狀態燈 (🔴未啟動/🟢運轉中全模擬/🔥含實單)、
    [🟢啟動自動交易(需確認)]、[⛔全部停止]、策略清單 (名稱|商品|週期|方向|
    進場條件|模式|狀態|今日次數|持倉,實單紅/模擬藍/停用灰)、
    ➕新增/✏️編輯/🗑刪除/▶啟用/⏸停用。
  - **策略編輯器**:基本參數 + 條件建構器 (下拉選條件、動態參數欄、
    加入進場/出場、листbox 顯示與移除)。
  - **Runner** `quant_runner_worker` 背景執行緒每 10 秒一輪
    `_quant_eval_pass()`:解析合約 (股票 Contracts.Stocks / 期貨
    `_resolve_futures_contract` 通用解析) → `_qt_fetch_closed_bars`
    (重用 `_download_kbars_raw` + K棒快取 + `_resample_sj_df`,後者本輪
    加入 asset_type 參數與圖表商品解耦) → 引擎評估 → 分流。
  - **下單** `_place_strategy_order`:鏡射 execute_order 組單 (股票=現股/
    整股/限價/ROD;期貨=限價/ROD);限價 = 訊號K棒收盤 ± 讓價檔數×tick
    (tick 用 core/tick_rules,四捨五入回合法檔位)。
  - 持久化:`quant_strategies.json` (策略) + `quant_state.json` (持倉狀態,
    每次成交即落地——重啟後不會忘記持倉而重複進場)。

### 安全設計 (需求第 2、3 點,全部有自動化測試背書)

1. **總開關 `_qt_running` 每次啟動一律 False,絕不持久化「開」**。
2. 啟動需經確認對話框:列出將啟動的策略與模式、實單策略紅字警告、
   **必須打字輸入「啟動」二字** 才能啟動——防誤觸。
3. **新策略一律以「模擬」模式儲存** (即使編輯器選了實單也強制改回),
   必須先觀察模擬訊號、再編輯改實單——防止沒驗證過的策略直接上實單。
4. 模擬模式:完整走評估/風控/狀態機,只記錄【自動交易-模擬】🧪 日誌與
   虛擬持倉,**絕不呼叫 place_order** (測試明確驗證 placed==[]).
5. **⛔全部停止**:立即關總開關,不再評估/下單 (持倉保留,由使用者決定)。
6. **單一策略連續 3 次錯誤自動停用**,其他策略與主系統完全不受影響
   (per-strategy try/except,runner 最外層再包一層)——回應「不可讓現有
   系統崩潰」。
7. 出場單不受每日次數限制;進場受次數上限+虧損熔斷+冷卻三重限制。
8. `on_app_close` 第一步就關總開關,關閉過程絕不下單。

### Phase 1 的誠實侷限 (實機使用前必讀)

- **「委託視同成交」的樂觀模型**:實單送出後即更新持倉狀態,未對帳實際
  成交回報。限價已含讓價檔數 (預設 2 檔) 提高成交率,但仍可能未成交——
  此時引擎以為有倉、實際沒有 (出場單會變成反向新倉風險低,因為出場也是
  限價;但務必在「我的委託單」確認)。Phase 2 應接成交回報對帳。
- 停損/停利在「K棒收盤」評估,盤中瞬間刺穿不會即時觸發 (最快下一根
  收盤才反應)。週期越短反應越快;Phase 2 可加 tick 級停損。
- 期貨損益/虧損熔斷單位=價差×數量,未乘契約乘數 (TXF 一點 200 元請自行
  換算熔斷值)。
- 沙盒無法連真實 shioaji:實單路徑的參數與流程經 mock 驗證,實際送單
  行為需按下方階梯實機驗證。

### 實機驗證階梯 (務必依序,絕不跳級)

1. 建 1 個策略 (模擬),啟用 → 打「啟動」開總開關 → **觀察數個交易日**:
   訊號時機/價位/停損停利是否符合預期;確認同一根K棒不重複、風控訊息合理。
2. 模擬滿意後,把數量降到**最小單位** (股票 1 張、期貨 1 口小台 MXF),
   編輯改「實單」→ 重新啟動總開關 (會出現紅字警告) → 盯著「我的委託單」
   核對每一筆自動單。
3. 確認成交/出場/停損都正確後,才逐步放大數量。任何異常先按 ⛔ 全部停止。

### Phase 2 展望

成交回報對帳 (取代樂觀模型)、tick 級停損/移動停損、歷史回測、
策略績效統計、更多條件 (量能/乖離/時間窗)、multiprocessing 隔離登入。

### 相關程式位置

- 新增:`core/strategy_engine.py`;`tests/test_core.py` TestStrategyEngine
  (7 測試方法,總數 52→59)。
- `stock_app_pro.py`:量化分頁 UI、`_qt_load/_qt_save/_qt_save_state/
  _qt_runtime/_qt_refresh_tree/_qt_selected/_qt_update_status_label/
  _qt_open_arm_dialog/_qt_stop_all/_qt_new_strategy/_qt_edit_strategy/
  _qt_delete_strategy/_qt_set_enabled/_qt_resolve/_qt_fetch_closed_bars/
  _place_strategy_order/_quant_eval_pass/quant_runner_worker/_qt_open_editor`;
  `_resample_sj_df` 加 asset_type 參數;`__init__`/`set_bottom_tab`/
  `on_app_close` 掛接。
- `diag_repro_issues.py`:ADR-035/036 案例。

---

## ADR-036：兩項附帶修正 — 美股漲跌口徑、期貨帳戶 406 友善處理

- **日期**：2026-07-16
- **狀態**：已採納 (與 ADR-035 同輪交付,診斷案例涵蓋)

### 1. 美股自選股漲跌與現實不符 (使用者實例 SPYM +0.16/+0.19%,實際 +0.34/+0.38%)

根因:yfinance `fast_info.previous_close` 口徑不穩 (可能拿到還原調整值或
錯誤交易日基準)。修正:改用 `history(period="10d", interval="1d",
**auto_adjust=False**)` 的最後兩個收盤價計算 (未還原價=券商顯示口徑);
盤中時最後一列即即時價,自然得到正確當日漲跌。history 取不到才退回
fast_info。驗證:mock 回傳 88.50→88.84,顯示 +0.34/+0.38% 與使用者
提供的真實數字完全一致。

### 2. 期貨帳戶查詢失敗洗版 (ServerError 406 Account Not Acceptable)

根因:使用者的期貨帳戶不可用 (未開通或未簽署 API 查詢同意書),這不是
程式錯誤,但每次更新庫存都重複報錯。修正:`_positions_fetch_once` 辨識
406/Account Not Acceptable → 印一次友善說明 (含「洽永豐確認 API 權限」
指引) → 設 `_fut_positions_unavailable`,本次連線內不再嘗試期貨帳戶;
重新登入 (新連線世代) 會重試一次。驗證:三次查詢只呼叫期貨一次、
只提示一次。

---

## ADR-037：當沖體驗強化包 — 庫存股數、期指秒級報價、十字準星、主圖K棒自動更新、警告靜音

- **日期**：2026-07-16
- **狀態**：已採納 (離線 17+6 項專項 + 診斷 20 案例驗證通過)
- **對應 shioaji 版本**：1.5.6

### 1. 庫存數量與實際不符 (修正 ADR-033 的錯誤判斷)

**承認錯誤**:ADR-033 認定「shioaji 證券 quantity 本來就是股數」是**錯的**
——預設單位其實是張 (使用者實測 21≠實際持股)。正確做法:證券帳戶改用
`list_positions(acct, unit=sj.constant.Unit.Share)` 要求券商**直接回股數**
(含零股,例如 21 張+80 股回 21080)。舊版 shioaji 不支援 unit 參數時
(AttributeError/TypeError) 退回預設呼叫並把單位標回「張」——寧可標對
單位,也不自行猜換算。期貨帳戶維持口。

### 2. 期指自選股報價 10 秒太慢 (當沖不可用)

自選股的期貨/指數改**訂閱 tick 串流** (與主圖同等級):
- `_wl_ensure_stream_subs`:目前群組的期貨/指數逐檔訂閱 (上限 20 檔防呆);
  **股票不訂閱**,維持 10 秒批次快照 (不佔訂閱額度、符合 P-03)。
- tick 路由 `_wl_route_stream_tick`:期貨用商品前綴 3 碼對照 (R1 訂閱推回
  月份合約 code,P-43 同款)、指數用 code ('001'/'101') 對照;漲跌優先用
  tick 的 price_chg/pct_chg,沒有就用合約平盤價 (reference) 計算。
- `watchlist_quote_worker` 改 1 秒節奏:串流報價**每秒上屏**,快照每 10 輪
  一次。重新登入清訂閱世代。

### 3. 主圖十字準星 (水平虛線)

hover 新增水平虛線 `hline_main` (只在主圖):y **對準游標所在K棒的收盤價**
(不是滑鼠像素位置),與既有垂直線構成十字準星;走 ADR-025 blitting 管線
(animated + draw_artist),毫秒級無卡頓。

### 4. 主圖K棒自動更新 (分時K免手動重載)

`chart_auto_refresh_worker` 每 2 秒檢查:分K週期 (1/5/15/30/60分) 跨過
「K棒收盤邊界」(+3 秒資料緩衝) 即自動刷新 `_chart_auto_refresh_once`:
抓最近 4 天分K → 與既有資料按時間合併 → **保留視野重繪**。
- 視野規則:停在最右側看最新盤勢 → 視窗跟著新K棒平移;翻到歷史區 →
  畫面完全不動。
- 資料無變化 (連最後一根收盤都沒動) 不重繪,避免無謂閃動。
- 序號守衛 (ADR-024 同款):刷新期間使用者手動查了別的商品,本次結果作廢。
- **誠實取捨**:K棒本體在「每根收盤」自動長出來 (1分K=每分鐘一次重繪,
  約 0.2-0.5 秒);盤中當根的即時跳動請看即時串流報價/五檔——mplfinance
  逐 tick 全圖重繪會造成使用者明確不要的持續停頓,故不做。

### 5. 關閉時的 mplfinance WARNING

365 天 1 分K資料量超過 mplfinance 預設門檻,在主控台印大段 WARNING
(關閉程式後才看到,誤以為錯誤)。`mpf.plot` 加 `warn_too_much_data=2000000`
靜音;顯示效能由既有視窗縮放機制處理。

### 另三項使用者提問 (不涉程式修改,回覆於對話)

- **策略撰寫方式**:不用寫 Python,用「量化交易」分頁的圖形化編輯器
  (ADR-035);策略以 JSON 存於 quant_strategies.json。
- **手機下單成交是否可見**:「我的已成交」資料來源是 API 推播回報;
  其他通路 (手機 App) 的回報是否推播無法在沙盒驗證,請實測回報 (詳見
  對話中的驗證步驟);「我的庫存」查詢必然涵蓋所有通路的部位變化。
- **多券商帳戶 (凱基)**:shioaji 是永豐專屬 API,凱基需接其自家 API,
  屬跨券商 adapter 架構的大工程,列為 backlog;同一永豐憑證下的多帳戶
  切換技術上可行 (list_accounts),需要再開需求。

### 需使用者實機驗證

1. 庫存股數與券商 App 完全一致 (含零股)。
2. 自選股的 TXF/MXF/^TWII 秒級跳動;股票仍 10 秒。
3. 十字準星水平線貼齊K棒收盤價、移動不卡。
4. 開 1 分K放著不動:每分鐘自動長出新K棒、視野不亂跳;翻到歷史區
   確認畫面不被拉走。
5. 關閉程式主控台不再出現 WARNING。

### 相關程式位置（`stock_app_pro.py`）

- `_positions_fetch_once` (unit=Share);`_wl_ensure_stream_subs`/
  `_wl_route_stream_tick`/`watchlist_quote_worker` 1 秒節奏/fop+stk callback
  路由/登入清世代;`hline_main` 建立/blit/移動;`AUTO_REFRESH_TFS`/
  `_chart_auto_refresh_once`/`chart_auto_refresh_worker`/`current_timeframe`;
  `mpf.plot warn_too_much_data`。
- `diag_repro_issues.py`:ADR-037 案例。

---

## ADR-038：主圖自動更新競態防護 + 量化「新增策略」按鈕被擠出修正

- **日期**：2026-07-16
- **狀態**：已採納 (離線 6 項專項 + 診斷 21 案例驗證通過)
- **對應 shioaji 版本**：1.5.6

### 問題1:切換期貨商品時「背景補全失敗、K線圖異常」(ADR-037 引入的競態)

使用者切換到微型臺指等其他期貨,日誌出現「完整歷史下載失敗」且 K 線圖
有問題。**根因是 ADR-037 新增的主圖自動更新 worker 造成的競態**:
1. 查詢流程分兩段 (快速段出圖 → 背景補完整歷史)。自動更新 worker 每 2 秒
   檢查K棒邊界,可能在**手動查詢還在進行中**時也去抓 kbars。
2. 兩條執行緒 (甚至加上量化 runner) **同時對同一條 shioaji 連線呼叫
   kbars**,互相干擾 → 背景補全下載失敗。
3. 更嚴重:自動更新讀到的 `current_contract` 可能已是新商品、`current_df`
   卻還是舊商品 (發布在後),把兩商品K線合併 → 圖異常。

**三層防護**:
1. **`_kbars_lock` 串接鎖**:`_download_kbars_raw` 全程持鎖,手動查詢/
   自動更新/量化 runner 三個執行緒共用的單一連線,kbars 呼叫強制串行化。
   驗證:5 執行緒同時下載,最高併發=1。
2. **`_fetch_in_progress` 讓路旗標**:`fetch_data_worker` 改薄包裝
   (try/finally 設清旗標,實體移到 `_fetch_data_worker_impl`);
   `_chart_auto_refresh_once` 開頭若偵測到手動查詢進行中 (或登入中)
   **完全讓路不動作**。
3. **df 身分守衛**:自動更新一開始記下 `df_ref = current_df`,套用前若
   `current_df is not df_ref` (物件換過=有新發布) 一律作廢本次合併——
   確保**絕不把 A 商品的資料黏進 B 商品的圖**。序號守衛 (ADR-024) 併用。

正常無競態時自動更新照常運作 (已驗證)。

### 問題2:量化交易分頁看不到「新增策略」按鈕

**根因是 P-44 老坑**:量化分頁的策略 Treeview 先 pack 且 `expand=True`,
按鈕列後 pack,分頁高度不足時**後 pack 的按鈕列被整條擠出可視範圍**
(不是顏色問題)。修正:按鈕列改「先 pack + side=BOTTOM」優先保留空間、
永遠可見,策略清單改為可被壓縮者;順帶把深灰底的「刪除」鈕文字改白字
(其餘維持黑字),對比更清楚。

### 需使用者實機驗證

1. 連續切換多個期貨商品 (TXF→MXF→TMF...),不再出現「完整歷史下載失敗」,
   K線圖正確、不會黏到別的商品。
2. 開著 1 分K自動更新的同時切換商品,圖表正常。
3. 量化交易分頁底部可看到「➕ 新增策略 / ✏️ 編輯 / 🗑 刪除 / ▶ 啟用 /
   ⏸ 停用」整排按鈕。

### 相關程式位置（`stock_app_pro.py`）

- `__init__`:`_kbars_lock`/`_fetch_in_progress`。
- `_download_kbars_raw`:全程持鎖。
- `fetch_data_worker` 薄包裝 + `_fetch_data_worker_impl`。
- `_chart_auto_refresh_once`:讓路檢查 + df 身分守衛。
- 量化分頁 `qt_btns` 先 pack + side=BOTTOM,刪除鈕白字。
- `diag_repro_issues.py`:ADR-038 案例。

---

## ADR-039：策略回測引擎與完整回測報告 (自訂策略規劃 Phase A)

- **日期**：2026-07-16
- **狀態**：已採納 (引擎 6 unittest + GUI 端到端 8 項 + 診斷 22 案例通過)
- **對應 shioaji 版本**：1.5.6

### 需求脈絡

使用者要「自訂策略程式 (完整 Python) + 完整回測報告 + 自訂策略也能實單」。
規劃分兩階段:**Phase A (本輪) 回測引擎與報告** (對現有策略回測,作為自訂
策略上線前的驗證基礎);Phase B (下輪) 自訂 Python 策略載入 (on_bar 介面 +
子行程執行 + 接進回測與實盤)。

### 核心設計:回測與實盤共用同一套邏輯

`core/backtest.py` 的 `run_backtest` **逐根重放歷史**:第 i 根收盤時,把
`df[:i+1]` 當成「已收盤到此」餵給 `strategy_engine.evaluate_strategy`,
引擎回什麼意圖就照 `apply_fill` 成交。**絕不另寫一套平行判斷**——這樣
「回測賺、實盤賠」不會是因為兩套邏輯不同。以自動化測試證明:回測的第一個
進場點 == 引擎金叉點。

回測時放寬即時風控 (每日次數/冷卻/熔斷,那是防人為狂點的當日控制,不是
策略訊號本身的績效);換日計數重置仍由引擎內部處理。

### 回測報告 (完整版)

- **績效數字**:總損益、報酬率、交易次數、勝率、獲利因子、最大回撤、
  平均持有K棒數、勝/負筆數、平均獲利/虧損。
- **資金曲線圖**:每根K棒的累積損益 (含未平倉浮動)。
- **K線+進出場標點**:買進場▲紅、賣進場▽綠、平倉用淺色,標在收盤價位置。
- **每筆交易明細表**:方向/進出場時間價位/損益/報酬%/持有K棒/出場原因,
  獲利紅、虧損綠 (台股慣例)。
- 成本模型:滑價用該商品 tick × 策略讓價檔數 (貼近實際成交);手續費率可設。

### 誠實侷限 (報告已標註)

回測沿用 Phase 1 的「委託視同成交、僅收盤評估」模型:不模擬盤中刺穿停損、
不模擬掛單未成交、不含滑價以外的衝擊成本;報告明確標「僅供參考,不代表
未來績效」。這些與實盤引擎的侷限一致 (ADR-035),所以回測與實盤同調——
要改善需 Phase 2 同時升級兩邊。

### GUI

量化分頁按鈕列加「🔬 回測」:選策略 → 背景下載歷史 (依週期 30~1500 天) →
`_qt_resolve` 解析合約 → `_resample_sj_df` (asset_type 參數) → `run_backtest`
→ 報告視窗 (Toplevel,雙 matplotlib 子圖 + 交易明細 Treeview)。防重入
`_backtest_running`。

### 需使用者實機驗證

1. 選一個策略按「🔬 回測」,確認報告視窗出現績效數字、K線標點、資金曲線、
   交易明細。
2. 對照 K線標點與你認知的進出場時機是否合理。
3. 調整策略參數 (如均線期間) 重新回測,觀察績效變化,作為調參依據。

### Phase B 預告 (下輪:自訂 Python 策略)

介面約定 `on_bar(ctx)`:ctx 提供已收盤 df、現成指標 (sma/ema/rsi/kd/macd/bb)、
持倉狀態,回傳 buy/sell/close_position/None。**在獨立子行程執行 + 逾時保護**
(崩潰/卡死不拖垮主程式);**風險仍由既有三層防護 + risk_check 把關**
(不是靠沙盒——Python 同行程無法真正沙盒化,會誠實告知使用者「只跑自己
看得懂的策略」)。自訂策略同樣接進本輪的回測引擎與實盤 runner。

### 相關程式位置

- 新增 `core/backtest.py`;`tests/test_core.py` TestBacktest (6 方法,59→65)。
- `stock_app_pro.py`:`_qt_backtest_selected`/`_qt_backtest_worker`/
  `_qt_show_backtest_report`;量化按鈕列加回測鈕;import backtest/copy。
- `diag_repro_issues.py`:ADR-039 案例。

---

## ADR-040：自訂 Python 策略 (自訂策略 Phase B) — on_bar 介面、子行程執行、與內建同路

- **日期**：2026-07-16
- **狀態**：已採納 (純邏輯 16+ unittest 6 + GUI/子行程端到端 + 診斷 23 案例通過)
- **對應 shioaji 版本**：1.5.6

### 需求

使用者要「自己寫 Python 策略讓系統讀取執行」,且自訂策略也要能回測、也能實單
(比照三層安全防護)。

### 誠實的安全立場 (最重要,已寫進 GUI 與程式註解)

**Python 在同一行程內無法做到真正安全的沙盒** —— 用 exec() 擋關鍵字都能被
繞過。本專案「不假裝」提供防惡意程式的沙盒,而是採三道務實防線:
1. **子行程隔離 + 逾時**:策略在 `python -m core.custom_runner` 子行程執行,
   無窮迴圈/崩潰/卡死只殺子行程,主看盤程式不受影響 (實測:while True
   策略被 8 秒 timeout 砍掉)。
2. **下單與否 100% 由既有三層防護 + risk_check 決定**:策略只能「回傳想做
   什麼」,即使寫錯狂喊買進,總開關/每日次數/冷卻/熔斷照樣擋住。
3. **鐵則**:只執行自己寫的、看得懂的策略,絕不貼陌生程式。這條紅字顯示在
   自訂策略編輯器最上方。

### 介面約定 (使用者只需實作一個函式)

    def on_bar(ctx):
        # ctx.df/close/position;ctx.sma/ema/rsi/kd/macd/bb/highest/lowest/
        #   cross_up/cross_down;ctx.param(key, default)
        return ctx.buy() / ctx.sell() / ctx.close_position() / None

指標方法直接復用 strategy_engine 的實作 —— 自訂策略、內建策略、回測三者
用同一套指標計算,不會有「回測與實盤指標算法不同」的坑。

### 與內建策略完全同路 (關鍵設計)

`custom_strategy.decision_to_intent` 把 on_bar 的決策 (BUY/SELL/CLOSE/HOLD)
+ 目前持倉,轉成與內建策略「同格式的 intent」(OPEN/CLOSE)。之後 risk_check /
apply_fill / 下單 / 回測全部走同一條路 —— 自訂策略自動享有所有既有防護與
回測能力,不需要為它另寫一套。以測試證明:等價自訂策略的回測進場點 == 內建
策略進場點。

決策正規化:無法辨識的回傳值一律當 HOLD (不動作) —— 安全預設。

### 元件

- `core/custom_strategy.py` (純邏輯):Ctx 環境物件、run_on_bar (載入+執行)、
  normalize_decision、decision_to_intent、EXAMPLE_SOURCE 範例。
- `core/custom_runner.py`:子行程入口 (stdin JSON → 執行 → stdout JSON 決策)。
- `core/backtest.py`:run_backtest 認得 kind='custom',逐根呼叫 on_bar。
- `stock_app_pro.py`:`_run_custom_in_subprocess` (子行程+逾時);runner
  的 `_quant_eval_pass` 對自訂策略走子行程;新增策略先問類型;
  `_qt_open_custom_editor` (程式碼編輯器 + 🧪試跑 + 安全警語);清單顯示
  🐍標記;回測/啟用驗證對自訂策略分流。

### 安全流程 (與 Phase 1 一致)

新自訂策略一律先存「模擬」;要先「🧪試跑」(子行程當場驗證能不能跑、現在會
回什麼決策) → 回測看績效 → 觀察模擬訊號 → 才改實單。子行程失敗被 runner
既有 per-strategy try/except 接住 → 計 error_count → 連 3 次自動停用,
不影響主程式與其他策略 (已驗證)。

### 需使用者實機驗證

1. 量化分頁「➕新增策略」→ 選「🐍 自訂 Python 策略」→ 編輯器出現範例碼與
   紅字安全警語;改個參數按「🧪試跑」,日誌顯示「執行成功,會回傳決策 X」。
2. 對自訂策略按「🔬回測」,看完整報告 (與內建策略同格式)。
3. 存成模擬、啟用、開總開關,觀察【自動交易-模擬】日誌;確認故意寫錯的
   策略會在 3 次錯誤後自動停用、主程式不當。
4. (進階) 確認自訂策略無窮迴圈時,主程式不會卡死 (子行程逾時保護)。

### 相關程式位置

- 新增 `core/custom_strategy.py`、`core/custom_runner.py`;
  `tests/test_core.py` TestCustomStrategy (6 方法,65→71)。
- `stock_app_pro.py`:import sys/json (先前遺漏,本輪補上);
  `_run_custom_in_subprocess`/`_qt_open_custom_editor`/`_qt_custom_test_worker`/
  `_qt_new_strategy` 選類型;runner/回測/清單/驗證分流。
- `diag_repro_issues.py`:ADR-040 案例。

---

## ADR-041：虛擬模擬帳戶、當沖級報價強化 (活K棒/邊界排程/股票串流)、完整段重試階梯

- **日期**：2026-07-17
- **狀態**：已採納 (帳戶引擎 13 + GUI 流 7 + 速度強化 15 項專項 + 診斷 24 案例通過)
- **對應 shioaji 版本**：1.5.6

### 1. 虛擬模擬帳戶 (需求:策略先在模擬帳號跑沒問題才上真實)

新增 `core/paper_account.py` 紙上交易記帳引擎:虛擬資金 (預設 100 萬,可
重置設定)、持倉 (均價/標記價/加碼攤平)、每筆交易史、已實現/未實現損益、
權益數。計價模型誠實簡化:台股含手續費 0.1425% 與證交稅 0.3% (1張=1000股);
期貨以契約乘數計損益 (TXF=200/MXF=50/TMF=10,未知乘數=1並標註)、每口單邊
估 50 元,不建模保證金占用。量化策略「模擬模式」的成交自動記入
(`_quant_eval_pass` 模擬分支),日誌顯示權益;量化分頁「💰 模擬帳戶」視窗
呈現資金/持倉/交易史,可重置。持久化 `paper_account.json`。

**為何不用永豐官方 simulation 模式**:(1) 官方模擬撮合「必定成交」,比實際
樂觀,驗證價值有限;(2) 需第二條登入連線,加倍 P-48 GIL 登入凍結;
(3) 沙盒無法驗證官方模擬連線;內建帳戶可 100% 離線測試。若未來仍想接官方
模擬,架構上可加 (獨立 ADR)。

### 2. 當沖級報價強化 (參考 MultiCharts/XQ 的做法)

**(a) 主圖活K棒 (核心)**:專業軟體的即時感來自「本地端用 tick 堆出形成中
的K棒」,不是反覆重載整張圖。實作:tick callback 累積 `_live_bar`
(o/h/l/c,跨K棒邊界自動開新棒);畫家 `_live_bar_painter` 每 400ms 用
**既有 blitting 管線** (ADR-025) 疊畫形成中K棒 (漲紅跌綠) + 即時價虛線
——毫秒級、零全圖重繪、不停頓。活K棒畫在資料最後一根位置 (盤中下載到的
最後一根即形成中那根),K棒收盤時既有的自動更新 (ADR-037) 把它固化。
已知小瑕疵 (誠實):K棒實體縮小時,底下靜態舊影像可能短暫露出,至下次
邊界重繪即消失。

**(b) 量化 runner 邊界感知排程**:舊版每 10 秒盲輪,分K訊號最慢延遲 13 秒
——對程式交易致命 (使用者原話:報價延遲會造成重大虧損)。改 2 秒醒來、
只在「該策略週期的K棒剛收盤+2秒緩衝」才評估:延遲縮到 2~4 秒,API 呼叫
反而更少。日K每 10 分鐘檢查。測試/手動觸發 (帶參數) 不受閘門限制。

**(c) 自選股股票也串流**:原本只有期貨/指數串流、股票 10 秒快照;現在
台股股票同樣訂閱 tick (上限 20 檔),1 秒級跳動;快照每 10 輪仍跑一次
補名稱與漏接;美股維持 yfinance 快照。

### 3. 「小型臺指近月」完整歷史下載失敗 (同秒立即失敗)

根因:近月合約上市往往才 1~2 個月,一次要 365 天歷史,券商端直接回錯
(與 ADR-038 的競態是不同根因——那次是被併發干擾,這次是資料本來就不存在)。
修正:完整段下載失敗自動縮短範圍重試 (365→180→90 天),成功會提示
「該合約可能上市未滿一年」;最終失敗的訊息**附上例外型別與內容** (根因要
證據,不再吞掉)。session 死亡類例外不重試直接走原重連機制。

### 需使用者實機驗證

1. 建模擬策略跑幾天 → 「💰 模擬帳戶」看權益/持倉/交易史與報酬率;確認
   「先模擬後實單」流程順手。
2. 開 1 分K盯盤:最後一根K棒隨 tick 即時跳動 (活K棒)、有即時價虛線、
   畫面不停頓;K棒收盤瞬間固化並長出新棒。
3. 自選股的台股股票秒級跳動。
4. 切「小型臺指近月」等新掛牌合約:不再出現下載失敗,或至少訊息含明確
   例外內容 (請回報該內容)。
5. 分K量化策略:訊號在K棒收盤後約 2~4 秒內出現。

### 相關程式位置

- 新增 `core/paper_account.py`。
- `stock_app_pro.py`:paper 帳戶載入/存檔/`_qt_open_paper_window`/模擬分支
  記帳;`quant_runner_worker` 2秒+`_quant_eval_pass` 邊界閘門 (_forced 旁路);
  `_wl_ensure_stream_subs` 股票納入+stk tick 路由;`_live_bar_on_tick`/
  `_live_bar_reset_artists`/`_live_bar_painter`+blit 整合+兩處 tick 掛接;
  完整段下載重試階梯。
- `diag_repro_issues.py`:ADR-041 案例 (含前案例快取殘留教訓)。

---

## ADR-042：ADR-041 效能回退修正 — 股票串流退場、節流回歸

- **日期**：2026-07-17
- **狀態**：已採納 (5 項專項 + 診斷 24 案例 + 核心 71 測試通過)

### 問題

ADR-041 上線後使用者回報「整個畫面又變得很遲鈍」。

### 根因分析

ADR-041 把自選股的「台股股票」也改成 tick 串流訂閱。熱門股 (如台積電)
每秒可產生數十筆 tick,最多 20 檔全訂閱 = **每秒數百次 Python callback**,
在 CPython 下這些 callback 與 GUI 主執行緒共搶同一把 GIL,畫面因此全面
變鈍。期貨/指數串流自第十六輪以來都沒問題,是因為檔數少 (1~3 檔);
股票 20 檔是壓垮駱駝的稻草。次要開銷:worker 每 1 秒跑一次訂閱檢查
(內含合約解析),群組含美股時是每秒的無謂開銷。

### 決定 (速度與流暢的誠實折衷)

1. **股票退出串流、退回批次快照,但節奏由 10 秒加快到 5 秒** (P-03 允許的
   下限):股票報價延遲砍半,而 callback 洪流完全消失。
2. **期貨/指數維持串流** (檔數少、當沖主力,1 秒級不變)。
3. 訂閱檢查與快照同步降為每 5 秒一次。
4. 活K棒畫家加 `_login_in_progress` 防護 (登入期間不 blit)。
5. **主圖活K棒不受影響**:圖表商品的 tick 本來就有訂閱,活K棒照常逐 tick
   跳動——當沖最關鍵的「主圖即時性」完整保留。

### 教訓 (併入 P-48 認知)

「更多串流=更即時」在 CPython GUI 程式裡不成立:每一筆推播都是一次
Python callback、一次 GIL 競爭。串流要留給「檔數少、真正需要秒級」的
標的;大量標的用批次快照才是對整體流暢度負責的做法。

### 需使用者實機驗證

1. 畫面恢復流暢 (操作、hover、切分頁無遲滯)。
2. 主圖活K棒仍逐 tick 跳動;期指自選股仍秒級;股票自選股約 5 秒更新。

### 相關程式位置（`stock_app_pro.py`）

- `_wl_ensure_stream_subs`:股票不訂閱;`_wl_route_stream_tick`:股票不路由;
  `watchlist_quote_worker`:5 秒節奏;`_live_bar_painter`:登入防護。

---

## ADR-043：策略系統大擴充 — 交易種類、回測計價修正、代碼查名稱、絕對停損停利、自訂回測期間、期貨近月解析

- **日期**：2026-07-17
- **狀態**：已採納 (核心 16 + GUI 端到端 15 項 + 診斷 25 案例 + 核心 76 測試通過)

### 使用者九項需求對應

**1. 微型臺指 TMF 近月抓不到**:根因是某些期貨商品 (如微型臺指) 沒有
`R1` 連續合約。`_resolve_futures_contract` 改:R1 取不到時遍歷群組挑
「最近到期月份」的實體合約 (依 delivery_month 最小),而非隨便第一個。

**2. 輸入代號顯示中文名稱**:編輯器商品欄旁即時查詢並顯示
「✓ 已確認商品:台積電」(綠) 或「✗ 查無此代碼」(紅),依交易種類查
Stocks 或期貨合約——輸入當下就知道抓對沒。

**3. 回測金額用「股」不是「張」(核心 bug)**:回測損益原本只算單股價差
(如 +10),與策略用張 (×1000) 不符。`backtest.run_backtest` 依交易種類
乘上單位規模:股票×1000、零股×1、期貨×契約乘數 (TXF=200/MXF=50/TMF=10);
權益曲線與 pnl_pct 同步修正。使用者截圖的 +1,187.50 這類數字現在會×1000。

**4/5. 交易種類三選項 + 數量單位**:策略新增 `trade_type` = 股票/零股/期貨
(取代較粗的 market,舊策略自動由 market 推導相容)。數量單位標籤動態顯示
張/股/口;清單「商品」欄顯示「代碼 (種類)」防交易錯誤。下單依種類組單:
股票=整股 Common、零股=盤中零股 IntradayOdd、期貨=期貨單。

**6. 絕對價格/點數停損停利**:策略新增 `stop_loss_abs`/`take_profit_abs`
(股票=元、期貨=點),與 % 版並存、任一先到就出場;引擎與編輯器都支援。

**7. 回測期間自訂**:回測前彈對話框輸入起訖日 (預設週期對應天數),
`_qt_backtest_worker` 依範圍下載並精確裁切。

**8. 多策略多標的並行**:runner 本就迴圈跑「所有 enabled 策略」,每個
獨立標的/runtime/持倉,天然支援 (已加測試證明兩檔不同標的同時進場、
模擬帳戶同時記兩檔)。啟動確認對話框列出全部。

**9. 期貨報價 1 秒更新**:期貨走 tick 串流即時更新 current_tick,報價
worker 每 0.5 秒上屏 (已優於 1 秒);活K棒畫家 400ms;確認期貨路徑
無被 ADR-042 的股票節流影響。

### 誠實侷限

- 回測分K資料受限於券商保留範圍 (通常只近期);自訂很早的起始日可能
  資料不足,已在訊息提示改用較長週期或縮短範圍。
- 契約乘數表列 TXF/MXF/TMF;其他期貨以 1 計並在模擬帳戶備註標示,回測
  數字僅點數 (需自行乘乘數解讀)。
- 零股撮合實務上不保證成交,模擬帳戶採樂觀成交 (與整股/期貨一致,見 ADR-035)。

### 需使用者實機驗證

1. 新增策略選「期貨」輸入 TMF/MXF,商品欄顯示中文名稱且能查到近月。
2. 回測報告金額量級正確 (股票是幾千~幾萬,不再是個位數價差)。
3. 建股票/零股/期貨三種策略,清單商品欄顯示種類、數量單位正確。
4. 設絕對停損 (股票填元/期貨填點),觸發時日誌顯示對應單位。
5. 回測時輸入自訂起訖日。
6. 同時啟用多個不同標的策略,各自獨立進出場。

### 相關程式位置

- `core/strategy_engine.py`:trade_type/qty_unit_of/is_futures/TRADE_TYPES;
  絕對停損停利;validate 改用 trade_type。
- `core/backtest.py`:contract_size 依交易種類計價。
- `core/paper_account.py`:apply_fill 加 trade_type (零股 1 股計);
  equity/unrealized 用 share_per_unit。
- `stock_app_pro.py`:兩個編輯器 (內建/自訂) 市場→交易種類+名稱查詢+
  絕對停損停利;_resolve_strategy_symbol_name;_place_strategy_order 依
  種類組單;_resolve_futures_contract 近月 fallback;回測日期對話框
  _qt_backtest_ask_range + worker 收日期;清單顯示種類;模擬記帳傳種類。
- `tests/test_core.py` TestTradeTypeAndAbsStops (5 方法,71→76)。
- `diag_repro_issues.py`:ADR-043 案例。

---

## ADR-044：自訂策略自由度升級、回測進階指標、報價再加速、平移殘影修正

- **日期**：2026-07-18
- **狀態**：已採納 (11 項端到端 + 核心 78 測試 + 診斷 25 案例通過)

### 1. 自訂策略多組並行 (使用者第1項)

與內建策略同一 runner 迴圈,本就天然支援;已加測試證明兩個自訂策略
(不同標的) 同時各自進場。每個策略獨立子行程執行、獨立 state。

### 2. 自由度升級,防護不變 (第2項)

Ctx 新增能力 (防護三道不動:子行程隔離+逾時、risk_check、只跑自己的程式):
- **`ctx.state`(dict)**:跨K棒持久狀態——可實作移動停損、計數器等有記憶
  策略。子行程協定 round-trip (JSON 序列化,不可序列化內容安全捨棄);
  實盤存 runtime['custom_state'],回測在記憶體內跨根傳遞,語意一致。
- `ctx.entry_price`/`ctx.bars_in_position`:持倉資訊。
- `ctx.open/high/low/volume/time`:當根完整資訊。
- 新指標:`ctx.atr(n)`/`ctx.vwap()`/`ctx.roc(n)`/`ctx.stddev(n)`。
- `ctx.log(msg)`:除錯輸出 (每根最多5條),試跑/實盤日誌顯示。
- 範例更新為含「移動停損」的進階示範。

### 3. 回測進階指標 (前輪需求)

新增:年化報酬 (線性簡化,誠實標示)、夏普比率 (每根權益變化×√年K棒數,
無風險利率取0)、最大回撤%、期望值/筆、賺賠比、最大連勝/連敗、總費用、
**買進持有對照** (策略 vs 躺著不動)、最長持有K棒。報告視窗第二排顯示。

### 4. 報價再加速 (第3項)

- 報價面板上屏 0.5→0.25 秒 (tick 為推播,加快上屏零 API 成本)。
- 期指/指數自選股串流上屏 1→0.5 秒。
- 台股股票自選股維持 5 秒快照——誠實侷限:這是 ADR-042 效能教訓的紅線
  (20 檔股票串流曾造成全畫面遲鈍),5 秒已是 API 配額與流暢度的平衡點;
  但**圖表中的股票**本來就是 tick 串流即時 (活K棒),不受此限。

### 5. 平移/縮放殘影 (第4項)

根因:活K棒/十字線用 blitting 疊畫在「背景快取」上;平移/縮放後視野變了
但快取還是舊視野,新座標疊舊背景 = 殘影。修正:背景快取時記錄
(xlim, ylim);每次 blit 前比對目前視野,不一致 → 作廢快取、跳過本次
blit、draw_idle() 排程完整重繪 (重繪後自動重新快取)。徹底消除殘影。

### 需使用者實機驗證

1. 1分K盤中拖曳/縮放畫面:不再有殘影,活K棒在重繪後正常續跳。
2. 報價面板數字更新更即時 (0.25秒)。
3. 自訂策略用 `ctx.state` 寫移動停損 (參考範例),試跑+回測+模擬。
4. 回測報告第二排新指標,特別看「買進持有對照」判斷策略價值。

### 相關程式位置

- `core/custom_strategy.py`:Ctx state/entry_price/bars_in_position/新指標/
  log;run_on_bar 新參數+return_ctx;範例更新。
- `core/custom_runner.py`:協定含 state/logs round-trip。
- `core/backtest.py`:_compute_metrics 進階指標;自訂路徑 state 跨根。
- `stock_app_pro.py`:_run_custom_in_subprocess(runtime=)/log顯示;報告
  第二排;fetch_realtime_worker 0.25s;watchlist worker 0.5s;
  _hover_bg_view 視野守衛。
- `tests/test_core.py` TestCustomFreedomAndMetrics (76→78)。
