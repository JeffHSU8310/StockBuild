
## ADR-084：新增內建策略「楚狂人之終極波段」— 看加權指數訊號、隔天中午12點二次確認、點數移動停利→SMA20移動停利

### 背景 / 需求

使用者要新增一個內建策略「楚狂人之終極波段」，邏輯特色：
1. 看加權指數 (A) 的訊號，實際下單使用者自選的股票/期貨 (B) — 屬於「看A做B」，
   但跟現有看A做B不同的是：這個策略是**雙時間週期**的狀態機（日K判斷訊號、
   5分K做隔天中午12:00二次確認），且有「點數移動停利→SMA20移動停利」的模式
   切換，無法用既有 `strategy_engine.CONDITIONS` 的 AND/OR 條件庫拼出來。
2. 進場：某天加權指數收盤突破/跌破分界點位 X，隔天中午12:00 (5分K收盤價)
   仍在同一側 → 才真正進場，記錄進場時的指數點位 (多單D/空單E)。
3. 停損：跌破/突破觸發點位 (多單Y/空單S1)，隔天中午12:00 仍在同一側才真正出場。
4. 停利：獲利超過門檻C才啟動，之後獲利每上升F、移動停利基準點跟著上調F
   (棘輪式，只升不降；空單反向)；直到指數漲/跌過SMA20，改用SMA20當移動停利
   標準 (一旦切換不會切回)；固定停損線切換後仍持續監控。停利觸發同樣要隔天
   中午12:00二次確認。

### 與使用者確認過的關鍵設計決策

開工前逐項跟使用者確認，以下是採用的版本 (詳見對話記錄)：
- 空單停利段落原文寫「以(D)計算」是筆誤，改用空單自己的進場點位 (E)。
- 「隔天中午12:00」的確認基準 = **當天到12:00為止的5分K收盤價** (不是即時報價)。
- 切換SMA20模式後的停利出場，**也要隔天中午12:00二次確認**，跟其他停損/停利
  規則一致 (不是當天觸及就立刻出場)。
- 停損參數原文的 (A)(B) 跟「看A做B」的 A/B 語意衝突，改名為 **S1/S2**
  (空單停損觸發/確認)，維持原文的 X/Y/Z/C/D/E/F/H。
- 移動停利基準點 (H) 是**使用者輸入的初始值**，不是系統從 D+C 自動算出來的。
- 同一時間**多空互斥**，只能持有一個方向的部位。
- 可以像其他策略一樣**建立多組** (不同參數、不同執行商品B)。
- 12點確認用的K棒週期選 **5分K**。
- 點數移動停利模式下跌破/漲過移動停利基準(H)，**也要隔天12點確認**才出場。
- 切換成SMA20模式後，**原本的固定停損線 (Y/S1) 繼續同時監控**，不會失效
  (SMA20是取代點數移動停利，不是取代固定停損)。

### 架構決策

**不是條件組合策略，是獨立的策略 `kind`**：現有「內建條件策略」本質是
`strategy_engine.CONDITIONS` 的 AND/OR 拼裝器，這個策略的邏輯 (雙時間週期
狀態機 + 模式切換) 拼不出來。比照 `core/custom_strategy.py` (自訂 Python
策略) 的模式，寫成一個新的 `kind='chukuangren_band'`，在
`stock_app_pro._quant_eval_pass` 裡新增第三個分派分支，跟 `custom`/一般
條件策略並列，但**共用**既有的風控守門 (`strategy_engine.risk_check`)、
成交記帳 (`strategy_engine.apply_fill`)、下單管道
(`_place_strategy_order`/`paper_account.apply_fill`)、模擬/實單切換、
三層安全防護 — 不另外造一條下單路徑。

**`core/chukuangren_band.py`** (零 tkinter/shioaji 依賴，可離線測試，遵循
ADR-009)：
- `PARAM_KEYS`/`PARAM_LABELS`：X/Y/Z/S1/S2/C/F/H 八個使用者輸入參數。
- `default_strategy()`：base 沿用 `strategy_engine.new_strategy()` (共用
  symbol/qty/mode/enabled/session_gate/看A做B 等既有欄位與持久化格式)，疊加
  `kind`/`ck_*` 參數/固定的看A設定 (`watch_enabled=True`、
  `watch_symbol='^TWII'`、`watch_trade_type='指數'`、`watch_timeframe='5分K'`)。
- `validate(strategy)`：獨立驗證函式 (不沿用 `strategy_engine.validate_strategy`，
  那是給條件組合策略用的，`entry`/`exit_signals` 欄位對本策略沒有意義)；額外
  檢查 Y<X、S1>X 的合理性。
- `ensure_runtime(rt)`：確保 runtime 帶有 `pending_entry`/`pending_exit`/
  `trail_armed`/`trail_base`/`sma20_mode`/`entry_index_price`/
  `last_daily_bar_date`/`last_confirm_date` 這些擴充欄位 (跟
  `strategy_engine.new_runtime()` 的 `state`/`entry_price`/`qty` 等通用欄位
  共用同一個 dict，通用欄位仍由 `apply_fill` 維護)。
- `on_daily_close(params, rt, daily_df)`：加權指數日K新收一根時呼叫，更新
  `pending_entry`/`pending_exit` 與移動停利狀態。**冪等**設計 (用
  `last_daily_bar_date` 擋同一天重複呼叫)，可以放心每次評估都呼叫，不用
  自己額外做「今天是不是收盤了」的判斷。只修改 runtime，不下單 (進出場一律
  要等隔天12點確認)。
- `on_noon_check(params, rt, confirm_price, today_key, qty)`：隔天中午12:00
  呼叫，確認 `pending_entry`/`pending_exit` 是否仍成立，回傳跟
  `strategy_engine.evaluate_strategy` 相同格式的 intents (`kind`/`action`/
  `qty`/`price`/`reason`)，可以直接丟給既有的 `risk_check`/`apply_fill`。同一
  交易日只會真正確認一次 (`last_confirm_date` 防重複)。
- 移動停利用「棘輪」(ratchet) 實作：多單只允許上調不倒退，空單只允許下調
  不倒退 —— 這是「移動停利」語意上必然的性質，屬於工程實作細節，未特別
  跟使用者確認 (若行為跟預期不符，之後可以再調整)。
- SMA20 模式的出場確認：`on_daily_close` 觸發待確認出場時，把當時的 SMA20
  值存進 `pending_exit['sma20_ref']`，隔天 `on_noon_check` 直接比對這個存好
  的值，不用重新抓一次 SMA20 序列 (次日中午的日K SMA20 本來就還沒變，因為
  新的一根日K要等當天收盤才产生)。

**`stock_app_pro.py` 接線**：
- `_quant_eval_pass`：新增 `kind == chukuangren_band.KIND` 的分派分支——固定
  抓 A (指數) 的日K呼叫 `on_daily_close`；在 12:00~12:04 視窗內 (且有
  待確認訊號、當天還沒確認過) 才額外抓 A 的5分K呼叫 `on_noon_check`，並用 B
  的5分K收盤價當實際下單執行價 (跟一般看A做B「用B最新收盤K棒」不同，這裡
  特意讓B的取價時間點對齊A的12點確認時間點)。產生的 intents 之後完全走
  既有的 `risk_check`→`_place_strategy_order`/`apply_fill`→模擬帳戶記帳流程，
  跟其他策略類型共用同一段程式碼，沒有另外複製一份。
- `_qt_open_chukuangren_editor`：新的獨立編輯器對話框 (執行商品B/交易種類/
  數量/模式 + 看盤A指數代碼 + 8個參數輸入框)，沿用既有的「點自選股帶入」
  機制 (`_qt_editor_symbol_target`)。
- `_qt_new_strategy`：新增第三個類型選項「🎯 楚狂人之終極波段」。
- `_qt_edit_strategy`/`_qt_set_enabled`/`_qt_refresh_tree`/
  `_qt_open_arm_dialog`：依 `kind` 分流到專屬編輯器/驗證函式/顯示文字。

### 已知限制 (誠實告知，v1 尚未支援)

**回測/最佳化/策略比較尚未支援這個策略類型**。既有的 `core/backtest.py`
是「單一 df、單一時間週期」的回測迴圈，這個策略是「日K判斷 + 5分K隔天中午
確認」的雙時間週期狀態機，硬套進去會產生錯誤的結果 (回測沒有「隔天中午
12點」這個概念)，所以刻意**不**接上去，`_qt_backtest_selected`/
`_qt_compare_worker` 遇到這個 kind 會直接顯示「尚未支援回測」並跳過，不會
假裝跑出一個誤導的數字。若之後要支援，需要另外設計一個逐日+逐5分K的回測
迴圈 (複雜度不低，屬於下一階段的工作，需要使用者確認要不要投入)。

### 測試

`core/chukuangren_band.py` 30 個新單元測試 (`TestChukuangrenBand`)，涵蓋：
`default_strategy`/`params_of`/`validate` 的基本行為與參數合理性檢查、
多空進場訊號偵測與隔天確認/作廢、固定停損觸發與隔天確認/作廢、點數移動
停利的啟動/棘輪上調(下調)/只升不降(只降不升)/觸發、SMA20模式切換與固定
停損仍同時監控、SMA20停利確認/作廢、待確認出場期間暫停繼續評估、以及
一個貫穿「進場→移動停利啟動→切換SMA20→SMA20停利出場」全流程的整合測試
(含實際呼叫 `strategy_engine.apply_fill` 驗證跟既有引擎的接線正確)。

`python tests/test_core.py` 全數 295 個測試通過 (原有 265 + 新增 30)。

### 需使用者實機驗證

這個工作環境沒有 tkinter/顯示器，無法實測 GUI，請在有畫面的機器上驗證：
1. 「➕ 新增策略」應該多一個「🎯 楚狂人之終極波段」按鈕，點開後應該能看到
   執行商品(B)/看盤指數(A)/X~H 八個參數輸入框。
2. 點左側自選股：先點一下「執行商品(做B)」欄再點自選股 → 應該帶入做B並判斷
   股票/期貨；點一下「指數代碼」欄再點自選股裡的指數 → 應該帶入看A。
3. 儲存後應該出現在「量化交易」策略清單，「條件」欄應顯示
   「🎯 楚狂人之終極波段 (看^TWII,X=...)」，方向欄顯示「多空自動判斷」。
4. 啟用後啟動自動交易 (建議先用模擬模式)：系統日誌應該能看到策略每 5
   分鐘評估一次；加權指數日K收盤後應該能看到 pending 狀態的變化 (可考慮
   之後加一個系統日誌訊息讓使用者看到「待確認」狀態，這次先不做，如果
   你想要之後可以加)。
5. 選取這個策略按「🔬 回測」或勾進「📊 策略比較」：應該顯示「尚未支援
   回測」的訊息，而不是報錯或給出誤導的數字。
