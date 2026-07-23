
## ADR-087：期貨內建停損/停利改為即時觸發，不再等K棒收盤

### 背景

使用者實機使用「楚狂人之終極波段」以外的一般「內建條件策略」(看A商品TXF做B
商品MXFR1,5分K),模擬帳戶畫面顯示 MXFR1 未實現損益已達 -6,250 (均價44402、
市價44277,跌125點),但策略設定的停損(元/點)=70 完全沒有觸發,持倉繼續掛著。
使用者判定「這是重大失誤」,要求檢查系統內建策略的停損停利機制。

### 根因 (兩個疊加,查證後確認)

1. **K棒收盤才判定**:`strategy_engine.evaluate_strategy()` 只在
   `_quant_eval_pass()` 判定「該策略訊號週期 (這裡是A=TXF的5分K) 剛收盤」時
   才評估一次 (`core/strategy_engine.py` `last_bar_ts` 閘門 +
   `stock_app_pro.py` 邊界感知),不是逐 tick/逐秒監控。即使真的觸及停損,
   最慢也要等下一根5分K收盤 (+2秒緩衝) 才會產生 CLOSE intent。
2. **看A做B 時,停損/停利比對基準是A、不是B (ADR-075既有設計)**:
   `runtime['entry_price']` 存的是A(TXF) 的收盤價,`evaluate_strategy()` 拿
   A 目前的收盤價跟A的進場價比對停損/停利,跟畫面上B(MXFR1)的帳面損益完全
   是兩套數字。TXF 跟 MXFR1 存在基差、走勢不會逐點同步,只要A還沒跌到70點,
   即使B已經跌超過70點很多,系統一樣不會觸發——這是ADR-075「訊號/停損/停利
   全部看A、只有下單看B」的既有設計 (使用者先前明確要求),不是bug,但這次
   實測讓使用者發現這個設計在「拿B的帳面損益當風控依據」時非常危險:畫面
   顯示的風險跟引擎實際判斷的風險是脫鉤的。

使用者確認後的修改方向:**期貨標的**用內建「停利/停損(%)」或「停利/停損
點數(元)」時,不必等K棒收盤,只要即時價一觸及條件就立刻出場;股票/零股
維持原本K棒收盤才判定的行為不變 (使用者這次只要求期貨即時)。

### 修改內容

**`core/strategy_engine.py`**:
1. `new_runtime()` 新增 `exec_entry_price` 欄位:記錄「實際下單商品 (看A做B
   時是B) 」的成交均價,跟 `entry_price` (A的價,ADR-075既有欄位、完全不動)
   分開存放,兩者尺度不同不能混用。
2. `apply_fill(strategy, runtime, intent, now_ts, exec_price=None)` 新增
   `exec_price` 參數,把 B 的實際成交價寫進 `exec_entry_price`
   (buy_and_hold 累積模式一併做加權平均,對稱 `entry_price` 的既有邏輯)。
   未帶 `exec_price` 時 (既有測試/回測/一般模式呼叫點) 退回用
   `intent['price']`,`exec_entry_price` 此時等於 `entry_price`,行為不變、
   完全相容舊呼叫方式。
3. 新增 `check_intrabar_futures_stop(strategy, runtime, live_price)`:純函式,
   只在 `trade_type_of(strategy)=='期貨'` 且目前有部位 (LONG/SHORT) 時檢查,
   拿 `exec_entry_price` (B的均價) 跟傳入的 `live_price` (B的即時價) 比對
   `stop_loss_pct`/`take_profit_pct`/`stop_loss_abs`/`take_profit_abs`,
   觸發就回傳 CLOSE intent (reason 標「即時停損/即時停利」)。跟
   `evaluate_strategy()` 內建的K棒收盤停損/停利 (以A為準,ADR-075不動) 完全
   獨立、互不排斥——誰先觸發都行,觸發後 state 變 FLAT,另一邊下次評估自然
   no-op。

**`stock_app_pro.py`**:
1. `_quant_eval_pass()` 的兩處 `strategy_engine.apply_fill(...)` 呼叫都補上
   `exec_price=exec_px` (原本就有算出來的B的實際成交價,只是沒往下傳)。
2. `_qt_update_realtime_pnl()`:原本只更新模擬帳戶的標記價,現在額外收集
   `live_price_by_symbol` (symbol→即時價),沿用**同一次**已經打過的
   `snapshots()` 結果 (鐵則5:不額外呼叫 API,節流不受影響),呼叫新方法
   `_qt_check_realtime_futures_stops()`。
3. 新增 `_qt_check_realtime_futures_stops(live_price_by_symbol)`:遍歷有部位
   的期貨策略,呼叫 `check_intrabar_futures_stop`,觸發就立刻走跟
   `_quant_eval_pass` 相同的下單/記帳路徑 (實單走 `_place_strategy_order`、
   模擬走 `paper_account.apply_fill`),reason/log 標「[即時]」跟一般K棒收盤
   出場區分開來,方便事後從日誌判斷是哪個機制觸發的。

這個即時檢查搭在 `quant_runner_worker` 既有的 3 秒 `_qt_update_realtime_pnl`
節奏上 (該節奏本身早於本次修改就已存在),不是新開一條輪詢迴圈。

### 效果

- 期貨策略設了「停損/停利 %」或「停損/停利 點數(元)」,只要 B (實際下單、
  畫面上算損益的那個商品) 的即時價觸及條件,最慢 3 秒內就會出場,不必再等
  K棒收盤 (可能長達5分鐘甚至更久)。
- 判斷基準改用 B 的即時價 vs B 的實際成交均價,跟模擬帳戶畫面上顯示的
  未實現損益完全一致,徹底解決「畫面顯示已經虧超過停損點、系統卻沒有動作」
  的落差。
- 股票/零股完全不受影響 (`trade_type_of` 閘門擋掉),原本K棒收盤才判定的
  行為維持原樣;`evaluate_strategy()` 裡看A的K棒收盤停損/停利 (ADR-075)
  也完全不動,兩套機制並存。
- 「楚狂人之終極波段」與自訂 Python 策略都沒有設定
  `stop_loss_pct`/`stop_loss_abs`/`take_profit_pct`/`take_profit_abs`
  這幾個欄位,`check_intrabar_futures_stop` 自然回傳 None,不受影響
  (它們各自有自己的停損停利機制,不在本次修改範圍)。

### 已知限制 (誠實)

- 即時觸發的節奏綁在既有的 3 秒快照節奏上,不是逐 tick——`quant_runner_worker`
  目前是每 3 秒才呼叫一次 `_qt_update_realtime_pnl`,這個數字早於本次修改
  就已存在 (且嚴格來說已經比鐵則5建議的「無串流 fallback ≥5秒」更頻繁),
  本次沒有調整這個既有間隔,只是把新的停損檢查掛在同一次快照結果上,沒有
  額外增加 API 呼叫次數。若日後要調整這個間隔,需要另外查 shioaji 流量限制
  並記錄依據。
- 這個即時通道只在 `self.paper_acct` 存在且已登入時才會跑 (沿用
  `quant_runner_worker` 既有的判斷式),跟原本的模擬損益更新機制共用同一個
  前提條件,沒有新增額外的登入/連線要求。

### 測試

`tests/test_core.py` `TestTradeTypeAndAbsStops` 新增 7 個測試:
`exec_entry_price` 正確記錄且跟 `entry_price` 分開/CLOSE後歸零、未帶
`exec_price` 時正確退回相容行為、即時停損觸發、未達門檻不觸發、股票不受
影響、無部位不觸發、SHORT即時停利(%)。連同既有測試共 307 個全數通過。

### 需使用者實機驗證

1. 期貨策略設「停損(元/點)」,持有部位期間即時價觸及停損點:應在約3秒內
   (不必等K棒收盤) 看到系統日誌出現「【自動交易-模擬/實單】...即時停損出場」
   字樣,模擬帳戶隨即平倉。
2. 同一策略設「停利(元/點)」或「停損/停利%」,漲跌到門檻時比照上一點驗證。
3. 股票/零股策略的停損停利應維持原本「K棒收盤才判定」的行為,不應提早
   觸發 (確認本次修改沒有波及股票路徑)。
4. 「看A做B」策略:即時停損/停利應以B(實際下單商品) 的即時價跟均價為準,
   跟模擬帳戶畫面上的未實現損益數字對得起來。
