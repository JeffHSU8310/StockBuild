
## ADR-070：自動交易加入「交易時段閘門」— 非交易時間自動待命、進入盤中自動運作

### 背景 / 需求

使用者:「非交易時間,自動交易程式就不動作;交易時間,程式就自己運作,完全
不用擔心、不用人工去開啟。時間設定要定義清楚,要有機制。」

時段 (使用者確認):
- 台股 (股票/零股) : 09:00 ~ 13:30
- 台期貨 日盤       : 08:45 ~ 13:45
- 台期貨 夜盤       : 15:00 ~ 次日 05:00

### 原本的狀況 (問題)

`quant_runner_worker` 每 2 秒呼叫 `_quant_eval_pass`,只被「總開關 `_qt_running`」
與「K 棒邊界」把關,**沒有任何交易時段概念**。非交易時間之所以「大致上沒動作」,
只是因為 `_qt_fetch_closed_bars` 抓不到新資料 → 附帶效果,不是明確的閘門;
也沒有「休市待命 / 開盤自動接手」的清楚語意與提示。

### 做法

**一、時段判斷抽成純函式 `core/market_session.py` (單一真相來源,可離線測試)**
- `is_stock_open / is_futures_day_open / is_futures_night_open / is_futures_open /
  is_market_open(trade_type, include_night) / session_label(...)`。
- 夜盤跨午夜正確歸屬:傍晚段 15:00~24:00 需當天為週一~週五;凌晨段
  00:00~05:00 屬前一交易日的夜盤 (故當天為週二~週六,含週五夜盤跨到週六 05:00;
  週一凌晨沒有夜盤)。
- 零 tkinter / 零 shioaji 依賴;`tests/test_core.py` 新增 `TestMarketSession`
  9 個測試,用固定 datetime 驗所有邊界 (開/收、跨日、週末),共 252 個測試全過。

**二、把閘門接進 `_quant_eval_pass` (每檔策略評估前先問「這個市場現在開盤沒」)**
- 依 `strategy_engine.trade_type_of(s)` 對應台股/台期貨;期貨依策略的
  `futures_session` ('day' / 'day_night') 決定要不要把夜盤算進交易時間。
- 休市 → `continue` 不評估、不下單。手動觸發 (`_forced`) 或策略關掉閘門
  (`session_gate=False`) 才略過此檢查。
- **開盤自動接手**:runner 每 2 秒醒著,時鐘一進盤中,閘門就放行;總開關
  `_qt_running` 全程不動,不需要任何人工重開。
- **不洗版的提示**:`_qt_log_session_closed / _qt_note_session_open` 只在
  「開盤↔休市」轉換那一刻各記一次日誌 (待命中 / 已進入盤中自動接手),讓
  使用者看得到狀態,而不是每 2 秒印一行。

**三、每檔策略可設定 (新欄位,寫進 `new_strategy()` 預設)**
- `session_gate` (預設 True):非交易時間自動待命 (建議);取消才會 24 小時
  只看 K 棒邊界。
- `futures_session` (預設 'day_night'):期貨要不要含夜盤。
- 內建策略編輯器新增「期貨時段 (日盤+夜盤 / 只做日盤)」下拉 + 「非交易時間
  自動待命」勾選;自訂策略沿用預設 (day+night 待命),日後要 UI 再補。

### 相容 / 安全

- 既有策略沒有這兩個欄位 → `.get(default)` 一律回「待命 + 含夜盤」,行為更保守
  (非交易時間本來就不該送單),不會有既有策略突然亂動的風險。
- **只影響即時自動交易 (`_quant_eval_pass`)**;回測 (`evaluate_strategy`
  逐根歷史 K 棒) 完全不受閘門影響,歷史績效照跑。
- 已知限制:只看「星期 + 時刻」,不含國定假日行事曆 (假日券商無新 K 棒,
  評估時抓不到資料自然不動作;`market_session.HOLIDAYS` 預留掛勾,日後可補)。

### 相關程式位置

- `core/market_session.py` (新檔)。
- `tests/test_core.py`:`TestMarketSession` (+9)。
- `core/strategy_engine.py`:`new_strategy()` 加 `session_gate`/`futures_session`。
- `stock_app_pro.py`:import market_session;`_quant_eval_pass` 加閘門;
  `_qt_log_session_closed`/`_qt_note_session_open`;內建策略編輯器 UI 與 `_save`。

### 需使用者實機驗證

1. 交易時段內啟用一檔模擬策略:日誌出現「已進入交易時段…開始自動評估」,
   訊號照常。
2. 收盤後 (或週末) 啟用:日誌出現「非交易時間…待命中,開盤會自動接手」,
   期間不下任何單。
3. 掛著跨過開盤時刻 (例如 08:44→08:45 期貨日盤):不用動它,應自動開始評估。
4. 期貨策略切「只做日盤」:13:45 後 (夜盤) 應待命不動作。
