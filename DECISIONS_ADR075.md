
## ADR-075：看A做B 語意修正 (停損停利也看A) + 回測/最佳化支援看A做B

### 背景 / 需求

使用者兩點:
1. 「接著補回測的看A做B支援。」
2. 「修改:條件/指標看A(可為加權/櫃買/台指期/股票等),**停損停利條件看A**,下單看B。」

第 2 點修正了 ADR-074 的語意:ADR-074 把停損停利做在 B 的價格上;使用者要的是
**訊號、指標、停損、停利全部看 A,只有下單/成交在 B**。

### 一、語意修正 (停損停利看A,下單看B) — ADR-074 的修訂

**引擎 (`core/strategy_engine.py`)**:`evaluate_strategy` 移除 `exec_close` 參數,
**一律看 A**:條件/指標/停損/停利都用傳入的 `df_closed` (A),`intent['price']`
就是 A 的收盤 → `apply_fill` 寫進 `runtime['entry_price']` → 停損停利以 A 的價格
判定。引擎完全不需要知道 B 的存在 (乾淨)。

**下單/記帳層 (`stock_app_pro._quant_eval_pass` / `_place_strategy_order`)**:
看A做B 時,實際成交價 `exec_px` = B 的最新已收盤價;
- `_place_strategy_order(..., exec_price=exec_px)`:限價/讓價/tick 都用 B。
- `paper_account.apply_fill(..., exec_px)` 與 `mark_price(B, exec_px)`:模擬帳戶
  記的是 B 的真實部位與損益。
- `strategy_engine.apply_fill(intent)` 仍用 A 的價 → 停損停利 state 看 A。
- 日誌標註 `[看A訊號→做B@價]`,一眼看懂訊號來源與實際成交。

一句話:**A 決定「何時進出場、何時停損停利」;B 決定「用什麼價成交、賺賠多少」。**

### 二、回測支援看A做B

**引擎 (`core/backtest.py`)**:`run_backtest(..., exec_df=None)`。
- `df` = A (訊號/指標/停損停利);`exec_df` = B (成交價)。
- 逐根:A 第 i 根訊號 → 成交價取 B 中「時間 >= A 該根時間」的第一根開盤
  (`searchsorted` 對齊,支援 A/B 週期不同,如 A 30分K、B 5分K);同 T+1 模型。
- 進出場價、損益、資金曲線浮動、期末結算一律用 B;`runtime['entry_price']`
  (停損停利基準) 用 A。`exec_df=None` → B=A,與舊版逐位元一致。

**回測 worker (`_qt_backtest_worker`)**:抽出 `_qt_bt_load_df()` 共用載入器
(下載+快取+重採樣+期交所/yahoo 延伸+裁切)。看A做B 時載入 A 當 signal_df、
B 當 exec_df,tick/滑價用 B。報告 K 線圖用 B 顯示 (標點落在實際交易商品)。

**參數最佳化 (`_qt_optimize_worker`)**:條件參數是看 A 的,故掃描改在 A 的歷史上
進行 (找對的訊號參數)。掃描顯示的絕對損益是「看A做A」近似 (最佳化只需相對
排名),並明確提示真實看A做B 損益請用「🔬 回測」確認。

### 測試

`tests/test_core.py`:
- `test_evaluate_all_on_A_sl_tp_on_A` (取代舊 exec_close 測試):驗證引擎一律看A、
  entry_price=A、停損以A判定。
- `test_backtest_watch_ab_exec_df`:A 決定筆數/時機、成交價來自 B (B=10×A → 進場價
  ×10、損益放大)。
- `test_backtest_exec_df_none_equals_plain`:exec_df=None 與不帶完全一致 (相容)。
共 264 測試通過。

### 已知限制 (誠實)

- 「策略比較 (compare)」與「即時風控的每日損益熔斷 (runtime.realized_pnl_today)」
  仍以 A 的價數字為基礎 (熔斷是粗略防護;compare 用執行商品)。模擬帳戶的
  真實損益是 B,不受影響。日後要精算 B 的熔斷可再開 ADR。

### 相關程式位置

- `core/strategy_engine.py`:`evaluate_strategy` 去掉 exec_close,一律看A。
- `core/backtest.py`:`run_backtest(exec_df=)` + `_exec_at()` 時間對齊 + 期末結算用B。
- `stock_app_pro.py`:`_quant_eval_pass` 下單/記帳用 B 的 exec_px;
  `_place_strategy_order(exec_price=)`;`_qt_bt_load_df()` 共用載入器;
  `_qt_backtest_worker`/`_qt_optimize_worker` 看A做B 接線。
- `tests/test_core.py`:上述 3 個測試調整/新增。

### 需使用者實機驗證

1. 建看A做B 策略 (A=加權30分K,B=某股5分K),按「🔬 回測」:報告應出現
   「訊號看加權/30分K…成交做某股/5分K」,交易明細的進出場價是該股的價。
2. 同策略跑「🎯 參數最佳化」:日誌應提示掃描在看A上進行、真實損益請用回測確認。
3. 一般 (不勾看A做B) 策略的回測/最佳化結果應與以前完全一致。
