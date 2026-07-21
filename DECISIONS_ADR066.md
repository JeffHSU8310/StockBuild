
## ADR-066：進場時間窗在內建策略上完全不生效、時間窗排除補上可見性、修正兩處被 ADR-064 遺漏同步的過期診斷斷言

### 背景

使用者要求「再次確認量化交易進/退場機制是否正確，會不會進/退場條件到了，卻沒有
動作」。這是主動要求的複查（不是回報具體症狀），逐一檢查
`core/strategy_engine.py` 的 `evaluate_strategy`/`filter_intents_by_time`/
`risk_check`/`apply_fill`、`core/custom_strategy.py` 的
`decision_to_intent(s)`，以及 `stock_app_pro.py` 的 `_quant_eval_pass` 之後，
找到 1 個會讓「使用者設定的進場限制被靜默忽略」成立的問題，並在收尾跑
`diag_repro_issues.py` 時額外發現 2 處與本次修正無關、但同樣是「條件其實沒問題，
卻被過期斷言誤判成失敗」的既有缺陷（ADR-064 遺留）。

---

### 一、【主要修正】進場時間窗 (entry_time_start/end、specific_entry_time) 在內建策略上是死碼

**症狀**：兩個策略編輯器（內建策略、自訂 Python 策略）都能設定「進場時間窗」
與「出場時間窗」（`entry_time_start`/`entry_time_end`/`specific_entry_time`/
`exit_time_start`/`exit_time_end`），但內建策略設了進場時間窗之後，只要進場
條件成立，不管是不是在時間窗內都照樣進場——設定形同虛設，使用者不會收到任何
錯誤或提示，因為根本沒有任何程式碼路徑檢查這個設定。

**根因**：`filter_intents_by_time()` 這個函式本身寫得是對的（能正確過濾 OPEN/
CLOSE 兩種 intent），且 `core/custom_strategy.py` 的 `decision_to_intents()`
三個分支（FLAT/LONG/SHORT）都有正確呼叫它。但 `core/strategy_engine.py` 的
`evaluate_strategy()`：

* `state == 'FLAT'`（一般策略的進場分支）：組好 OPEN intent 後直接
  `return intents`，從未呼叫 `filter_intents_by_time`。
* `buy_and_hold` 分支（單筆長抱/累積加碼/定期定額三種模式）：組好 OPEN intent
  後同樣直接 `return intents`，也從未呼叫。
* 只有「持倉中的出場訊號」分支（`exit_signals` 判定為 True 之後）在函式最尾端
  呼叫了一次 `filter_intents_by_time`；停損/停利兩種出場則在判定成立時直接
  `return`，比呼叫點還早，同樣繞過了時間窗過濾。

也就是說,整個引擎裡「會產生 OPEN intent」的 3 個分支，一個都沒有真的套用進場
時間窗；只有「出場訊號」這一種出場路徑有套用出場時間窗，停損/停利則不受任何
時間窗限制（這部分是刻意的，見下方「維持不變的部分」）。`tests/test_core.py`
也從未替這個功能寫過任何測試案例，這正是 P-57 那種「功能寫了，但沒有工具/測試
把關，呼叫端悄悄漏掉」的又一次重演。

**影響**：使用者若是為了「只在開盤前 15 分鐘找機會」或「盤中特定時段才進場」
這類需求設定進場時間窗，內建策略會在時間窗之外一樣進場，且不會有任何提示——
這是本次複查中唯一一個「設定被忽略而使用者無從得知」的真實 bug。

**修正**：在 `evaluate_strategy()` 的三個 OPEN 生成點（FLAT 分支、buy_and_hold
的 dca 分支、buy_and_hold 的 single/accumulate 分支）補上
`return filter_intents_by_time(intents, strategy, bar_ts, runtime=runtime)`。
`filter_intents_by_time` 本身沒有設定任何時間窗欄位時是 no-op（函式一開頭就
檢查 `if not (en_start or en_end or ex_start or ex_end or sp_entry): return
intents`），所以這個修正對所有沒用到這個功能的既有策略**零行為改變**。

**維持不變（刻意，不是漏改）**：停損/停利仍然不經過任何時間窗過濾——保護性出場
不該被使用者設定的時間窗擋住，這與 ADR-065 P-81「風控只管 OPEN，出場永遠要
出得去」是同一種設計哲學的延伸；只有「訊號類」的出場（`exit_signals`）才會被
`exit_time_start/end` 限制，這是使用者主動設定的「只在某時段才依訊號出場」，
與保護性停損停利的地位不同，不應混為一談。

---

### 二、被時間窗排除時，補上可見性 (呼應 P-58/P-70/P-83「背景失敗必須看得見」)

**問題**：即使前項修正上線，若使用者設定的時間窗把一次原本會成立的進場/出場
擋下來，使用者仍然完全看不到「這一根其實條件成立，只是被你設定的時間窗擋下」
——跟 P-83 描述的「隔離之後如果完全不留痕跡，等於把『真錯誤』偽裝成『條件不
成立』」是同一個模式，只是這次的「隔離」換成了時間窗過濾。

**修正**：`filter_intents_by_time` 新增可選參數 `runtime`；有傳入時，每次呼叫
都會把「這次被時間窗排除的 intent」說明清單寫進
`runtime['time_window_skips']`（沒有排除任何東西時蓋成空 list，不會讓舊訊息
一直殘留）。`core/strategy_engine.py` 的 4 個呼又點與 `core/custom_strategy.py`
的 3 個呼叫點都補上 `runtime=runtime`。`stock_app_pro.py` 的 `_quant_eval_pass`
比照既有的 `condition_errors` 處理方式，非空時印出
`【自動交易-時間窗跳過】策略「X」: ...` 日誌——使用者看到這行就知道「不是 bug，
是我自己設定的時間窗生效了」，而不是又要來回報一次「條件到了卻沒動作」。

`new_runtime()` 新增預設欄位 `time_window_skips: []`，比照 `condition_errors`
的既有慣例。

---

### 三、【收尾檢查意外發現，與本次修正無關】diag_repro_issues.py 兩處過期斷言

跑 `python diag_repro_issues.py` 收尾時，30 個案例裡有 2 個 FAIL，追查後確認
與本次改動完全無關，而是 **ADR-064** 修正 `core/backtest.py`/`tests/test_core.py`
時，忘了同步這支診斷腳本裡兩個等價的斷言（P-57「同一個修正沒有同批交付所有
呼叫端」的又一次真實案例，這次的呼叫端是診斷腳本而非程式邏輯）：

1. **ADR-039 案例**（`_round18_backtest`）：`assert first_open==df.index[cross_i]`
   仍然是 ADR-064 之前「同根收盤即成交」的舊假設。ADR-064 已經把
   `tests/test_core.py` 的等價斷言改成 `cross_i + 1`（T+1 開盤成交模型），但這支
   診斷腳本沒有跟著改，於是變成一個穩定重現的假警報。修正：比照改成
   `df.index[cross_i + 1]`。
2. **ADR-062 案例**（`_adr062_bnh_modes_and_compare`）：
   `assert m3['bnh_total_invested'] <= planned + 1e-6` 是定期定額「絕不超支預算」
   的嚴格版本。ADR-064 已經在 `tests/test_core.py` 的
   `test_dca_invests_close_to_planned` 說明「sizing 用決策當根收盤價、成交價
   卻是下一根開盤價，隔夜跳空必然導致預算只能是『目標』不是『上限保證』」，
   並把斷言鬆綁成 `<= planned * 1.01`，但同樣沒有同步這支診斷腳本。實測（本次
   重跑的隨機序列，35 期、每期 10,000）：預算 350,000，實際投入 350,348.12，
   超出 0.0995%——與 ADR-064 原文的實測數字（超出 0.0994%）幾乎一致，佐證這
   是同一個已知且可接受的現象，不是新 bug。修正：比照改成
   `<= planned * 1.01`。

這兩處都只是診斷腳本本身過期，不代表 `core/backtest.py` 或
`core/strategy_engine.py` 的邏輯有問題——事實上這兩個案例驗證的正是 ADR-064
已經記錄過、且判斷為「正確設計、不建議 revert」的 T+1 行為。修正後
`diag_repro_issues.py` 30 案例、`diag_crossref.py`、`tests/test_core.py`
（240→243，含本次新增的 3 個 ADR-066 案例）全部乾淨通過（`diag_repro_issues.py`
另有一次性的 ADR-024 案例 FAIL，重跑三次後穩定 PASS，判斷為與本次改動無關的
既有 timing 相關偶發現象，非本次修正引入，暫不深究）。

---

### 需使用者實機驗證

1. `python tests/test_core.py` → 243 項全過；`python diag_repro_issues.py` →
   30 項全過；`python diag_crossref.py` → 無斷鏈。
2. 內建策略編輯器：設定「進場時間窗」（例如 09:00~09:05）與一個容易觸發的進場
   條件，在時間窗外不應該進場；系統日誌應出現
   `【自動交易-時間窗跳過】策略「X」: OPEN ... 因設定的進/出場時間窗被跳過`。
3. 不設定任何時間窗欄位的既有策略，行為應與修正前完全一致（no-op 保證）。
4. 買進持有（累積加碼／定期定額）策略若設定了進場時間窗，同樣要遵守。

### 相關程式位置

* `core/strategy_engine.py`：`evaluate_strategy` 的 FLAT 分支與 buy_and_hold
  兩個 OPEN 生成點補上 `filter_intents_by_time`；`filter_intents_by_time`
  新增 `runtime` 參數與 `time_window_skips` 收集；`new_runtime()` 新增預設欄位。
* `core/custom_strategy.py`：`decision_to_intents` 三處呼叫補上 `runtime=runtime`。
* `stock_app_pro.py`：`_quant_eval_pass` 新增 `time_window_skips` 的日誌輸出。
* `tests/test_core.py`：新增
  `test_entry_time_window_blocks_open_outside_window_ADR066`、
  `test_entry_time_window_allows_open_inside_window_ADR066`、
  `test_buy_and_hold_accumulate_respects_entry_time_window_ADR066`（240→243）。
* `diag_repro_issues.py`：ADR-039 案例斷言改 `cross_i+1`；ADR-062 案例斷言改
  `<= planned * 1.01`（兩處皆為同步 ADR-064 的收尾遺漏，與本次主修正無關）。
