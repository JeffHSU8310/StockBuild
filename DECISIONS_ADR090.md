
## ADR-090：量化交易 Telegram 通知 — 成交/系統訊息即時推播

### 背景

使用者要求:「當啟動量化交易時，有任何成交訊息，或是系統訊息我要用
Telegram 傳送成交訊息給我，把這個功能寫進系統」。

### 範圍界定 (誠實記錄設計判斷,非使用者逐字規格)

系統既有的 `log_message()` 已經用統一前綴「【自動交易-xxx】」標記所有
量化交易產生的訊息 (模擬/實單成交、風控擋單、休市待命/開盤、條件錯誤、
時間窗跳過、例外、待下單...等,含 ADR-087/089 新增的即時停損與延遲下單
訊息),不需要額外列舉「哪些算成交/哪些算系統訊息」——只要訊息帶這個
前綴,就代表是量化交易相關,直接檢查前綴即可,新增的子類別未來也會自動
被涵蓋,不用每次都回來改判斷邏輯。「當啟動量化交易時」解讀為:只在
`self._qt_running` (自動交易總開關) 為 True 時才推播——這跟「【自動交易】」
訊息實際上也只會在總開關開啟時大量產生一致 (`_quant_eval_pass` 等在總
開關關閉時提早返回)。

### 設計 (比照 core/ai_helper.py 的 ADR-049 模式)

**`core/telegram_notify.py` (新檔,零 tkinter/shioaji,可離線測試)**:
- `is_quant_message(msg)`:訊息是否以「【自動交易」開頭。
- `config_ready(cfg)`:bot_token 與 chat_id 是否都已填。
- `build_send_request(bot_token, chat_id, text)`:組出 urllib 可直接用的
  請求描述 (url/data/headers),純函式、不發送。文字超過 Telegram
  4096 字元上限就截斷 (4000+提示);不用 Markdown/HTML parse_mode
  (訊息含大量使用者自訂名稱/符號,貿然解析容易因特殊字元送出400)。
- `parse_response(body)`:解析 Telegram API 回應 JSON,回傳 (ok, 訊息)。
- **本模組不做真正的 HTTP 呼叫**——跟 `ai_helper.py` 同樣的理由:核心層
  要能離線完整測試,混進真網路呼叫會讓測試依賴外部服務、變慢、不穩定。

**`data/config_store.py`**:新增 `load_telegram_config`/`save_telegram_config`
(比照既有 `load_ai_config`/`save_ai_config` 的模式),存 `telegram_config.json`
(bot_token/chat_id/enabled)。

**`stock_app_pro.py`**:
1. `__init__` 啟動時把設定快取進 `self.telegram_cfg` (量化 runner 訊息很
   密集,`log_message()` 每次都要檢查,不能每次都重新讀檔案)。
2. `log_message()` 尾端加一段:`_qt_running` 為 True 且訊息符合
   `is_quant_message` 且設定備妥,就呼叫 `_send_telegram_async()`——實際
   HTTP 呼叫丟進背景執行緒 (daemon thread + urllib.request),不擋 UI。
   送出失敗只記一行系統日誌 (前綴「【Telegram通知】」,刻意不用「【自動
   交易」開頭,避免被自己的判斷式抓到造成無窮推播失敗訊息的迴圈)。
3. 新增設定視窗 `_qt_open_telegram_settings()` (量化交易面板新增
   「📱 Telegram通知」按鈕開啟):填 Bot Token/Chat ID、啟用開關、
   「🧪 測試發送」按鈕 (用畫面上當下的值直接測試,不需要先存檔)、儲存/取消。
   對話框內附上「跟 @BotFather 建立 Bot 拿 Token、用 @userinfobot 查
   Chat ID」的操作指引,降低使用者第一次設定的門檻。

### 效果

- 使用者按下「🟢 啟動自動交易」後,任何成交 (模擬/實單、一般K棒收盤出場/
  ADR-087 期貨即時停損停利/ADR-089 楚狂人延遲下單)、風控擋單、休市待命、
  條件錯誤、例外等系統既有會記進日誌的量化交易訊息,都會同步推播到
  Telegram,不用一直盯著畫面。
- 按「⛔ 全部停止」後自動停止推播 (`_qt_running` 變 False)。
- 沒設定或沒啟用時完全不影響既有行為 (`config_ready` 擋掉,`log_message`
  的其餘邏輯不變)。

### 已知限制 (誠實)

- Telegram Bot API 在部分網路環境可能需要代理才能連上 (中國大陸等地區的
  網路限制),本機連線狀況需使用者自行確認;送出失敗只會記系統日誌,不會
  重試 (訊息量大時重試機制容易造成訊息風暴,先簡單處理,若使用者實際
  用起來覺得漏訊息太多再考慮加重試佇列)。
- 沒有針對「【自動交易-xxx】」底下的子類別做細緻開關 (例如只要成交、
  不要條件錯誤/時間窗跳過這類雜訊) ——目前是全部一起推播,若使用者覺得
  太吵,之後可以再開 ADR 加分類開關。

### 測試

`tests/test_core.py` 新增:
- `TestTelegramConfigStore`(2 個):設定檔存讀/預設值。
- `TestTelegramNotify`(8 個):訊息判斷前綴、設定備妥判斷、組請求
  (含拒絕空白憑證、超長文字截斷)、解析回應 (成功/失敗/非法JSON)。
連同既有測試共 331 個全數通過。

### 需使用者實機驗證 (這項無法離線測試,務必實機確認)

1. 「📱 Telegram通知」設定視窗:填入真實 Bot Token/Chat ID,按「測試發送」
   應能在 Telegram 收到「【測試訊息】StockBuild Telegram 通知設定成功。」。
2. 存檔並勾選啟用,啟動自動交易總開關,讓策略跑出至少一筆模擬成交:
   系統日誌出現的那則「【自動交易-模擬】...」訊息應該同步出現在 Telegram。
3. 按「⛔ 全部停止」後,即使系統日誌又出現「【自動交易-xxx】」開頭的訊息
   (例如收尾清理訊息),也不應該再推播到 Telegram。
4. 不填 Bot Token/Chat ID 或不啟用時,量化交易照常運作,系統日誌照常記錄,
   只是不會推播,確認沒有因為這個新功能而影響既有量化交易行為。

### 相關程式位置

- `core/telegram_notify.py` (新檔)。
- `data/config_store.py`:`load_telegram_config`/`save_telegram_config`。
- `stock_app_pro.py`:`TELEGRAM_CONFIG_FILE`;`__init__` 載入
  `self.telegram_cfg`;`log_message()` 推播鉤子;`_send_telegram_async`/
  `_telegram_test_send`;`_qt_open_telegram_settings`;
  `_build_quant_panel` 新增按鈕。
- `tests/test_core.py`:`TestTelegramConfigStore`、`TestTelegramNotify`。
