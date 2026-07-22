
## ADR-067：切換標的時,報價退訂/訂閱移到背景執行緒,不再擋住 K 線出圖

### 背景

使用者回報「標的切換的速度很慢」,要求「點擊後主畫面 K 線圖可以馬上切換」。
逐一檢視 `on_watchlist_select → start_fetch_thread → fetch_data_worker
(_fetch_data_worker_impl)` 這條路徑後,確認慢的關鍵不在下載本身,而是
**報價訂閱的網路呼叫卡在 K 線出圖前面**。

---

### 症狀

點自選股換一檔新商品時,即使該商品的分 K 已經在 `_kbars_raw_cache` 裡
(理論上可以「秒開」),K 線圖仍要等一小段時間才換掉。

### 根因

`_fetch_data_worker_impl` 裡,換到新合約 (`contract_changed`) 時的執行順序是:

1. **退訂舊合約** — `self.sj_api.quote.unsubscribe(...)` × 2~4 路
2. **訂閱新合約** — `self.sj_api.quote.subscribe(...)` × 2~4 路 (整股 Tick/五檔
   + 股票還要零股 Tick/五檔)
3. 才輪到「讀快取 → `_publish` → `draw_chart`」出圖

第 1、2 步是 shioaji 的 WebSocket 訂閱網路呼叫,每一路都要一次來回;整股加
零股最多 4 路訂閱 + 4 路退訂,全部串在同一條 worker 執行緒上、又排在出圖
前面。結果就是:**K 線圖被迫等所有訂閱來回跑完才畫**,快取秒開的優勢被
訂閱延遲整個吃掉,使用者感覺「換圖很慢」。

### 正確做法 (本 ADR)

把「畫面/狀態需要、且是純記憶體操作」的部分留在 worker 同步做,把「真正
慢的 shioaji 退訂/訂閱網路呼叫」丟到獨立背景執行緒,兩者解耦:

* **同步 (快,留在 worker,擋不到出圖)**:
  * 切換 `self.current_contract`、清空零股/整股報價暫存與成交明細
    (`quote_lock` 保護,維持鐵則 3)。
  * 讀合約物件上既有屬性 `day_trade`/`reference` (非網路呼叫)、重設現沖
    checkbox 起始值 (ADR-015)、更新現沖 badge。
* **背景 (慢,丟新執行緒 `_resubscribe_quotes_worker`)**:
  * 退訂舊合約 + 訂閱新合約的所有 shioaji 網路呼叫。
  * 出圖不再等它;worker 設完狀態就直接往下走讀快取 `_publish → draw_chart`。

於是「點自選股 → 主圖馬上換」:快取新鮮時幾乎即時出圖,報價訂閱在背景
默默補上,補完照樣有即時五檔/成交明細。

### 為維持正確性做的三件事

1. **序列化 (`self.subscribe_lock`)**:快速連點時,多路退訂/訂閱不能互相
   交錯,否則「退舊→訂新」的順序會亂。用一把新的 `subscribe_lock` 把每一次
   resubscribe 整段包起來,確保一次只有一條在對 shioaji 下訂閱指令。
   (原本的實作其實也有「兩條 worker 併發訂閱」的同種風險,本 ADR 反而
   用鎖把它收斂得更安全。)
2. **過期跳訂**:在鎖內、退訂完成後,若 `self.current_contract` 已經不是
   這一路要訂的合約 (使用者已切到別檔),就 `return` 不再花配額訂這個過期
   合約——退訂已做完,下一路 worker 會接手訂新的。避免鐵則 5 在意的
   「API 流量配額被無謂的訂閱吃掉」。
3. **不污染顯示**:即使有殘留的過期訂閱短暫存在,tick/bidask callback
   本來就用 `tick.code == self.current_contract.code`
   (`on_tick_stk_v1`/`on_bidask_stk_v1`) 與
   `_fop_code_match(...)` (`on_tick_fop_v1`/`on_bidask_fop_v1`) 過濾,
   對不上目前合約的資料一律丟棄,不會把上一檔的報價套到現在這一檔
   (維持鐵則 3 的資料分流)。

### 維持不變的部分

* **同商品換週期** (`contract_changed == False`):完全沿用既有訂閱,一路
  退訂/訂閱都不做——與 ADR-024 相同,不受本次改動影響。
* 每一路訂閱仍各自 `try/except` 並把成功/失敗印到系統日誌 (鐵則 8),
  訊息格式 (`【訂閱結果】...`/`【訂閱失敗】...`) 原封不動。
* 訂閱一律 v1 typed callback (鐵則 2)、零股/整股報價暫存分開加鎖
  (鐵則 3),都沒有變。

### 相關程式位置

* `stock_app_pro.py`:
  * `__init__`:新增 `self.subscribe_lock`。
  * 新增 `_resubscribe_quotes_worker(prev_contract, contract, asset_type)`:
    背景退訂+訂閱,含 `subscribe_lock` 序列化與過期跳訂守衛。
  * `_fetch_data_worker_impl`:`contract_changed` 分支改為「同步設狀態 +
    背景 `threading.Thread(target=self._resubscribe_quotes_worker, ...)`」,
    不再把訂閱網路呼叫排在出圖前。

### 需使用者實機驗證 (此環境為 headless,無法實測 GUI/shioaji)

1. 登入券商後,在自選股清單連續點不同標的:主圖 K 線是否「點一下就換」,
   尤其是剛看過、已在快取內的商品應接近即時。
2. 換到新標的後,稍待背景訂閱完成,五檔/成交明細/活 K 棒是否照常即時跳動
   (系統日誌會出現 `【訂閱結果】...`)。
3. 股票 vs 期貨都測一次 (股票多零股兩路訂閱)。
4. 快速連點多檔再停在某一檔:最後停留那檔的報價要正確,不應收到別檔的
   tick (看系統日誌與五檔數字)。
