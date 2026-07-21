
## ADR-065：進出場機制複查 —— 冷卻誤擋出場單、平倉污染冷卻時鐘導致反手失效、條件評估失敗永遠靜默

### 背景

使用者要求「再次確認量化交易進/退場機制是否正確，會不會進/退場條件到了，
卻沒有動作」。這不是回報具體症狀，是主動要求的複查——逐一檢查
`core/strategy_engine.py` 的 `evaluate_strategy` / `risk_check` / `apply_fill`
與 `stock_app_pro.py` 的 `_quant_eval_pass` 之後，找到 3 個會讓「條件到了卻
沒有動作」真實發生的問題，其中第一個屬於嚴重等級。

### 一、【嚴重】冷卻 (cooldown) 會誤擋出場單，跟本函式自己的文件互相矛盾

**根因**：`risk_check()` 的冷卻檢查寫在 OPEN/CLOSE 共用的區塊之外：

```python
def risk_check(strategy, runtime, intent, now_ts):
    if intent['kind'] == 'OPEN':
        ...  # 每日次數、虧損熔斷 —— 只管 OPEN
    cooldown = ...
    if cooldown > 0 and (now_ts - last_order_ts) < cooldown:   # ← OPEN/CLOSE 都會擋
        return False, "冷卻中..."
```

但 `risk_check` 自己的 docstring、ADR-035 §7 都明確寫「出場單不受...限制
(持倉一定要能出得去)」，且另外兩項風控（每日次數、虧損熔斷）也確實只套用在
`OPEN`。冷卻是唯一一個沒有照這個規則寫的——這是實作疏漏，不是刻意設計。

**實際影響有多嚴重**：`new_strategy()` 的 `cooldown_sec` 預設值是 **300 秒**，
剛好等於 5分K 的一根K棒週期。目前啟用中的兩個策略（TXF、555555，皆 5分K
MA(5,20) 交叉）都是這個預設值。當進場後下一根K棒馬上出現出場訊號（快線
均線交叉策略很常見)，「距離上次下單」通常只比 300 秒多一點點（K棒週期 +
runner 延遲 2~4 秒),只要 API 延遲、時脈誤差、或訊號提前一點點，就會小於
300 秒而被冷卻擋下——部位卡在場上出不去，直到下一根K棒才會被重新評估
(若出場訊號是「交叉」這種一次性條件，屆時可能已經不成立，等於這次出場
機會永久錯過)。這正是使用者擔心的「退場條件到了，卻沒有動作」。

**為什麼回測從沒抓到這個問題**：`core/backtest.py` 的 `_relax_realtime_guards()`
在跑回測前無條件把 `cooldown_sec` 設成 0——回測本來就刻意关掉冷卻（只想看
策略訊號本身的績效），所以這個 bug 只存在於「即時 (模擬/實單)」路徑，
用回測驗證策略完全看不出來。

**修法**：把冷卻檢查移進 `if intent['kind'] == 'OPEN':` 區塊，跟每日次數/
虧損熔斷同一個位置——出場單完全不看冷卻。

### 二、平倉會污染冷卻時鐘，讓「反手」策略的開倉腿被自己的平倉腿冷卻擋下

**根因**：`apply_fill()` 原本無條件在函式最上面 `runtime['last_order_ts'] =
float(now_ts)`，OPEN/CLOSE 都會更新。ADR-053 的 `decision_to_intents`
（自訂策略的「停損反手」語意）在同一輪評估會回傳 `[平倉, 開倉]` 兩個
intent，`_quant_eval_pass` 逐一處理：先處理平倉 → `apply_fill` 把
`last_order_ts` 撥成「現在」→ 緊接著處理開倉 → `risk_check` 算出
「距離上次下單 0 秒」→ 冷卻條件必定成立（只要 `cooldown_sec > 0`）→
反手的開倉腿被擋下。**這等於重演 ADR-053 當初想解決的問題**（「持多遇
SELL 只平倉、不反手，少做一半的單」），只是換了冷卻這個機制觸發，
而不是原本的狀態判斷缺陷。

**修法**：`last_order_ts` 只在 OPEN 分支更新，語意改成「距離上次**進場**
多久」——這才是冷卻真正該防的事（避免訊號雜訊造成的過度進場），CLOSE
不該影響這個時鐘。搭配一的修法（冷卻只檢查 OPEN），兩者疊加後：
反手的平倉腿不受冷卻影響、緊接著的開倉腿看到的是「上一次真正進場」的
時間（通常已經過了夠久），能正常送出。

### 三、條件函式拋例外時，`eval_conditions` 隔離設計是對的，但完全沒有留下痕跡

**背景**：`eval_conditions()` 對每一條件獨立包 `try/except`，任何一條拋例外
只讓那一條算 `False`，不會連累其他條件、也不會讓整組評估中斷——這個隔離
設計本身沒有問題（避免一條寫壞的條件拖垮整個策略）。但舊版 `except
Exception: ok = False` 完全沒有任何記錄，代表一旦某個進場或出場條件的
參數設定有問題（型別錯、資料筆數不足、除零等)，那條條件會**從此永遠評估
為 False**，使用者看到的是「條件到了卻沒有動作」，卻連一行錯誤日誌都
查不到——這跟本專案一貫的「背景失敗必須看得見」原則（P-58、P-70）矛盾。

**修法**：`eval_conditions(df, conds, logic, errors=None)` 新增可選的
`errors` 參數，條件拋例外時 append `(標籤, 例外訊息)` 進去；
`evaluate_strategy` 的三個呼叫點都傳入同一個 `cond_errors` list，並把結果
存進 `runtime['condition_errors']`（沒有錯誤時蓋成空 list，不會讓已經修好
的舊錯誤一直殘留誤導人）。`_quant_eval_pass` 呼叫 `evaluate_strategy` 後
檢查這個欄位，非空就用 `【自動交易-條件錯誤】` 明確記錄策略名稱、哪個
條件、什麼錯誤——不改變「一條壞掉不連累其他條件」的隔離行為，只是讓
使用者看得見。

### 需使用者實機驗證

1. `python tests/test_core.py` → **240 項全過**（本輪新增 3 項:
   `test_cooldown_never_blocks_close_ADR065`、
   `test_apply_fill_close_does_not_touch_cooldown_clock_ADR065`、
   `test_condition_error_is_captured_not_silent_ADR065`）。
2. `python diag_crossref.py` → 無斷鏈。
3. 目前啟用中的兩個模擬策略（TXF、555555，皆 MXFR1 5分K）本輪修正後應該
   不會再出現「持倉在下一根K棒理應出場，卻卡住不動」的現象；請留意
   系統日誌，若日後出現 `【自動交易-條件錯誤】`，代表該策略的某條件參數
   設定有誤，需要進去檢查。
4. 本輪只修改 `core/strategy_engine.py` 與 `stock_app_pro.py` 的
   `_quant_eval_pass`，沒有動到下單面板、繪圖、報價等其他部分。

### 相關程式位置

* `core/strategy_engine.py`：`risk_check()` 冷卻檢查移入 `OPEN` 區塊；
  `apply_fill()` 的 `last_order_ts` 只在 `OPEN` 分支更新；
  `eval_conditions(errors=)`；`evaluate_strategy` 的 `cond_errors` 收集與
  三處 `runtime['condition_errors']` 賦值；`new_runtime()` 新增
  `condition_errors` 預設欄位。
* `stock_app_pro.py`：`_quant_eval_pass` 呼叫 `evaluate_strategy` 後檢查
  `rt['condition_errors']` 並記錄日誌。
* `tests/test_core.py`：`TestStrategyEngine` 新增 3 項測試（237 → 240）。
