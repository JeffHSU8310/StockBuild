
## ADR-089：楚狂人之終極波段 — 確認(12:00)與下單(12:01)分兩個時間點、新增委託方式選項

### 背景

使用者看「楚狂人之終極波段」編輯器截圖,提出兩項要求:
1. 「改下單時間規則:中午12:00確認,條件成立,12:01就下單」—— 原本
   ADR-084 的設計是「隔天中午12:00 (5分K收盤價) 二次確認」成立後,在同一
   個評估流程裡立刻送單,確認跟下單是同一瞬間發生。使用者要求把這兩件事
   拆成前後相差約1分鐘的兩個時間點。
2. 「一樣,要有 限價單/市價單/範圍市價 可以選擇」—— ADR-088 已經在
   「內建條件策略」編輯器加了委託方式選項,這次要求楚狂人編輯器也要有。

### 修改一:確認(12:00)與下單(12:01)分兩個時間點

**根因/設計考量**:原本 `on_noon_check()` 一次做完「確認」與「組出可執行
intent」兩件事,`_quant_eval_pass` 拿到 intent 後在同一輪評估內立刻送單。
使用者要的是:12:00 確認訊號是否仍然成立 (沿用A的5分K收盤價,ADR-084/085
邏輯不變),但**不要立刻下單**,而是等大約60秒 (即12:01附近) 之後,再用
執行商品 (B) 當時最新的價格真正送出委託。

**`core/chukuangren_band.py`**:
1. `ensure_runtime` 新增 `armed_intent` (確認成立、待執行的動作) 與
   `armed_at_ts` (確認當下的時間戳) 兩個 runtime 欄位。
2. 原本的 `on_noon_check(params, rt, confirm_price, today_key, qty)` 拆成
   兩個函式:
   - `on_noon_confirm(params, rt, confirm_price, today_key, now_ts, qty=1)`:
     只做確認,不回傳 intent。確認成立就把動作記進 `armed_intent`
     (`{'kind','action','qty'(僅OPEN)/reason}`) 並記錄 `armed_at_ts=now_ts`。
     `entry_index_price`/`trail_armed`/`trail_base`/`sma20_mode` 這些狀態
     依然在**確認當下**就處理好 (ADR-085 語意不變:記錄的是12:00確認時的
     加權指數點位,不是1分鐘後執行時的點位)。
   - `on_execute_armed(rt, exec_price, now_ts, delay_sec=60.0)`:如果
     `armed_intent` 存在且 `now_ts - armed_at_ts >= delay_sec`,才組出真正
     要送出的 intent (`intent['price'] = exec_price`,即執行當下B的最新價,
     不是12:00確認當下的A價),清空 `armed_intent`/`armed_at_ts`。還沒到
     時間就回傳空 list,呼叫端每次都能放心呼叫 (冪等)。

**`stock_app_pro.py`**:
1. `_quant_eval_pass` 的楚狂人分支:改呼叫 `on_noon_confirm` (只確認),
   確認成立時記一筆系統日誌 (「12:00確認成立,約1分鐘後自動送單」),但
   不再組 `b_exec_price`/送單——這部分完全移到下一點的新方法。
2. 新增 `_qt_chukuangren_execute_pass()`:獨立於 `_quant_eval_pass` 的5分K
   邊界閘門之外 (楚狂人5分K邊界只在整5分鐘觸發,不會剛好落在12:01,無法
   沿用同一套機制),掛在 `quant_runner_worker` 既有的2秒輪詢節奏上。遍歷
   有 `armed_intent` 的楚狂人策略,取執行商品 (B) 當下最新的**1分K**收盤價
   (比5分K更能反映「1分鐘後」這件事在價格上有實際差異),呼叫
   `on_execute_armed`,過了60秒才會拿到真正的 intent,拿到後走跟一般
   策略相同的送單/風控/記帳流程 (`risk_check`→`_place_strategy_order`或
   `paper_account.apply_fill`),日誌標「[12:01延遲下單]」跟一般K棒收盤
   出場區分開來。
3. `quant_runner_worker` 每輪 (2秒) 除了 `_quant_eval_pass()` 也呼叫
   `_qt_chukuangren_execute_pass()`;沒有 `armed_intent` 的策略在這個新
   方法裡會立刻 `continue`,成本可忽略,不影響既有節奏與API配額。
4. 編輯器頂部說明文字同步更新,明確寫出「確認成立後不會立刻下單,會等約
   1分鐘 (12:01) 依當時最新價才真正送單」。

### 修改二:楚狂人編輯器新增「委託方式」選項

沿用 ADR-088 已經在 `core/strategy_engine.py` 建立的
`PRICE_TYPES`/`FUTURES_ONLY_PRICE_TYPES`/`price_type_of()`
(`_place_strategy_order` 已經是通用邏輯,楚狂人策略本來就會經過它,不需要
再改)。這次只需要:
1. `core/chukuangren_band.py` `validate()` 補上跟
   `strategy_engine.validate_strategy()` 相同的委託方式檢查 (合法值、範圍
   市價僅期貨、零股強制限價鐵則6) —— 因為楚狂人有自己獨立的 `validate()`,
   不會經過 `strategy_engine.validate_strategy()`。
2. `stock_app_pro.py` `_qt_open_chukuangren_editor()`:在「模式」下拉下方
   新增「委託方式」下拉,選項依「交易種類」動態切換 (跟 ADR-088 內建條件
   策略編輯器完全同樣的邏輯與交互),存檔寫入 `s['price_type']`。

### 測試

`tests/test_core.py` `TestChukuangrenBand`:
- 新增測試輔助 `_confirm_and_execute()`,包成「confirm→(過60秒)→execute」
  兩步驟,讓既有測試 (只關心確認/作廢邏輯,不特別關心延遲) 用跟以前
  `on_noon_check` 一樣的呼叫方式與斷言繼續通過,不用大改既有測試。
- 新增 4 個測試直接驗證新的兩段式 API:確認後不會立刻執行、過了60秒才
  執行且用執行價 (不是確認價)、沒有 armed_intent 時是 no-op、CLOSE 執行時
  用 runtime 當下的 qty。
- 新增 3 個測試驗證委託方式:預設限價、範圍市價搭期貨合法、範圍市價/市價
  搭股票/零股被擋。
連同既有測試共 321 個全數通過。

### 需使用者實機驗證

1. 楚狂人策略隔天中午12:00 訊號確認成立:系統日誌應先出現「12:00確認
   成立...約1分鐘後自動送單」,大約1分鐘後 (12:01附近) 才出現真正的下單/
   模擬成交日誌 (標「[12:01延遲下單]」),兩則日誌時間應相差約1分鐘,不是
   同一秒。
2. 楚狂人編輯器:交易種類選「期貨」時,「委託方式」應可選 限價/市價/範圍
   市價;選「股票」只剩限價/市價;選「零股」鎖死限價。
3. 實單模式下確認12:01送出的委託單,委託方式應正確對應畫面選的限價/市價/
   範圍市價。
