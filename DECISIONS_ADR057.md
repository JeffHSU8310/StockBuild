
## ADR-057：量化交易獨立視窗、內建策略條件大擴充、報告可驗算，以及 GC 導致的行程崩潰根治

### 背景

使用者一次回報 11 項問題。其中第 11 項（程式直接崩潰退出）最嚴重，且證明
ADR-056 的修法找錯了地方；第 2 項則是 ADR-056 把使用者需求讀反了。本 ADR
一併更正。

---

### 一、【最嚴重】參數最佳化跑到一半整個程式跳掉（使用者需求 #11）

**使用者提供的崩潰訊息（關鍵證據）**

```
File "...\tkinter\__init__.py", line 416, in __del__
RuntimeError: main thread is not in main loop
Tcl_AsyncDelete: async handler deleted by the wrong thread
```

**根因**

那個 `__del__` 是 `tkinter.Variable.__del__`，它會呼叫 `self._tk.call(...)`
回 Tcl。完整因果鏈：

1. tk 物件（Variable / Widget）幾乎必然參與**參照循環**
   （widget → command 閉包 → Variable → widget），不會被 refcount 立即釋放，
   而是交給 Python 的**循環 GC**。
2. 循環 GC 會在**任何一條執行緒**配置記憶體超過門檻時觸發。參數最佳化 worker
   每組參數都 `deepcopy` 策略、產生新的 DataFrame，是全程式最會配置記憶體的
   地方，於是 GC 幾乎必然在 worker 執行緒被觸發。
3. 它回收到某個已關閉對話框留下的 `tk.Variable` → `__del__` 在非主執行緒
   呼叫 Tcl → `RuntimeError` → Tcl 的 C 執行期直接 abort 整個行程。

**為什麼 ADR-056 的修法沒有用（誠實記錄）**

ADR-056 把 `safe_after` 的 except 從 `TclError` 擴大到
`(TclError, RuntimeError)`。但崩潰**根本不在 `safe_after` 裡**，而是在 GC
觸發的 `__del__` 裡 —— 那是 Python 直譯器自己呼叫的，應用層 try/except
攔不到。這是一次「症狀相似就當成同一個 bug」的誤判。

**修法**

`__init__` 呼叫 `gc.disable()` 關閉自動循環 GC，改由主執行緒每 20 秒
（`_gc_tick`）主動 `gc.collect()`。這樣循環垃圾一律在主執行緒被回收，
`tk.Variable.__del__` 也就一定在主執行緒執行 —— 從結構上消除，不是碰運氣。

**代價（誠實說明）**：關閉自動 GC 後，兩次主執行緒回收之間的循環垃圾會暫時
累積（非循環物件仍靠 refcount 立即釋放，不受影響）。20 秒間隔對本應用的
配置速率而言記憶體增量很小，`draw_chart` 也仍會在重繪時主動 collect。
若未來記憶體吃緊，**調短間隔即可，不要改回自動 GC** —— 那會把崩潰帶回來。

---

### 二、量化交易改為獨立視窗（使用者需求 #1）

底部分頁高度就那幾行，策略一多完全不夠看。改為：

* 新增 `_build_quant_panel(parent, tree_height, compact)`，同一套 UI 可同時
  建在「底部分頁（精簡，height=4）」與「獨立視窗（完整，height=20，欄寬加大）」。
* 所有面板登記進 `self._qt_uis`；`_qt_refresh_tree` / `_qt_update_status_label`
  / `log_message` 鏡射一律**逐一更新所有存活面板**，兩邊永遠同步。
* `_qt_selected()` 以「使用者實際在操作的面板」為準（獨立視窗開著就看視窗那份），
  否則使用者在視窗裡點的策略會被分頁那份空的選取狀態蓋掉。
* 刷新時保留各面板原本的選取項，避免清單一刷新選取就跑掉。
* `_qt_alive_uis()` 用 `winfo_exists()` 自動清掉已銷毀的面板。
* **關閉獨立視窗不影響自動交易**（使用者明確要求）：`_on_close` 只移除 UI 登記，
  完全不碰 runner；自動交易是否運轉只由總開關決定。
* 底部分頁**保留**而非砍掉：總開關狀態（🔴/🟢）屬於安全資訊，即使沒開視窗
  也應該在主畫面看得到。

---

### 三、金額一律無條件捨去小數（使用者需求 #2）—— 更正 ADR-056

**ADR-056 把需求讀反了。** 使用者原話「損益不會有小數點，小數點後面的數字
無條件刪除」是**指令**（要截斷），ADR-056 誤讀成 bug 回報，反而把 `.0f`
改成 `.2f`，讓問題更嚴重。

修法：新增 `_fmt_amt()` / `_fmt_amt_signed()`，用 `int()` **往零截斷**。
不用 `f"{v:,.0f}"` 是因為它會四捨五入（-53438.54 → -53,439），憑空多算 0.46 元。

**刻意不套用的三類數字（取捨，需要時可改）**：

* 百分比（勝率、報酬率）—— 使用者也明確說「只要不是 %」。
* 比率類（獲利因子 1.83、賺賠比、夏普 -0.01）—— 這些落在 0~3 之間，截成
  整數後 1.83 和 1.02 都變成「1」，報告會失去判讀能力。
* 價格（進場價 100.65）—— 台股價格本來就有小數，截掉會變成錯的價格。

平均持有 K 棒數改為整數（`624.0 根` → `624 根`）。

---

### 四、內建策略條件大擴充（使用者需求 #3、#4）

條件數 16 → **41**。新增 25 種，重點是使用者指名的「收盤價突破/跌破均線，
且均線參數可自行設定」：

* 均線類：`price_cross_up_ma` / `price_cross_down_ma`（突破/跌破，交叉語意）、
  `price_above_ma` / `price_below_ma`（站上/跌破，狀態語意）、
  `ma_slope_up` / `ma_slope_down`（上彎/下彎）、
  `ma_align_bull` / `ma_align_bear`（多頭/空頭排列）。
  **全部支援 SMA/EMA 切換 + 期間自由設定。**
* 量能：`volume_above_ma` / `volume_below_ma`（N 日均量 × 倍數）。
* 型態：`consecutive_up` / `consecutive_down`、`inside_bar`、`gap_up` / `gap_down`。
* 幅度：`pct_change_above` / `pct_change_below`。
* 指標：`rsi_cross_up` / `rsi_cross_down`（穿越，與既有的 above/below 狀態不同）、
  `macd_hist_above_zero` / `macd_hist_below_zero`、
  `bb_squeeze` / `bb_expand`、`kd_cross_up_low` / `kd_cross_down_high`。

**設計原則**：

* 「交叉（cross）」與「狀態（above/below）」刻意分開：交叉只在穿越那一根成立
  （適合當進場訊號，不會每根都觸發）；狀態每根都可能成立（適合當過濾條件，
  例如「站上季線才做多」）。兩者語意不同，不要混用。
* 一律只用已收盤 K 棒評估（P-49），不開特例。
* **參數規格格式擴充**：原本 `(key, label, default)` 三元素，新增可選的第 4
  元素 `choices`（下拉候選值，例如 `['SMA','EMA']`）。新增
  `strategy_engine.spec_parts()` 統一正規化，所有讀 spec 的地方都改走它。
* GUI `_rebuild_params` 依 `choices` 決定渲染 Entry 或 Combobox；
  `_collect_cond` 改為「數字轉數字、轉不動保留字串」—— 舊版轉不動就 `pass`
  把整個參數丟掉，會讓 `SMA`/`EMA` 被靜默吃掉而永遠退回預設值。

---

### 五、回測報告可以「自我驗算」（使用者需求 #5 後半）

使用者問「要怎麼去確認是對的回測報告？」—— 這是很好的問題，答案不該是
「請相信我」。新增 `core/backtest.audit_result()` 與報告視窗的「🧮 驗算這份
報告」按鈕：**完全不看 `_compute_metrics` 怎麼算的，直接從最原始的 trades
明細用最直白的方式重算一次**，再跟報告對帳。兩條獨立路徑得到同一個答案，
才有理由相信報告。

10 項檢查：淨損益加總、交易次數、勝負筆數、勝率、獲利因子、最大連續虧損金額、
最大回撤 ≥ 最大單筆虧損、不利價格方向不可能獲利、出場不早於進場、持有 K 棒 ≥ 1。

**誠實界定能力邊界（也寫進 UI）**：

* ✅ 能抓到：彙總層計算錯誤、明細內部矛盾。
* ❌ 不能保證：訊號判定是否符合你的策略意圖（那要看策略邏輯）；成本模型費率
  是否與你的券商實際收費一致。**全部通過 ≠ 這個策略在真實市場一定會這樣成交。**

開發過程中這個函式自己被抓出兩個 bug，都由測試/診斷發現：

1. **單位錯誤**：第一版的方向檢查拿「損益（金額，已乘契約乘數）」跟
   「進出場價差（點數）」比大小，期貨乘數 200 讓它對真實回測必然誤報。
   改為只檢查單位無關的必然矛盾（不利方向不可能獲利）；「有利方向卻小虧」
   是成本造成的正常現象，不再誤報。
2. **全勝誤判**：第一版用 `min(所有損益)` 當「最大單筆虧損」，全勝時那是
   最小獲利（正數），會讓正確的報告被判定不一致。改為只取虧損筆。

---

### 六、其餘各項

| # | 需求 | 作法 |
|---|---|---|
| 5 前半 | 最大連續虧損/獲利 | `_compute_metrics` 新增 `max_consec_win_amount` / `max_consec_loss_amount`。與既有的「筆數」並列：連敗 5 筆講次數，連續虧損 -12,340 講金額，後者才是撐不撐得住的關鍵 |
| 6 | 報告最大化/縮小 | 拿掉 `dlg.transient(self)` —— transient 視窗在 Windows 上不會有自己的最大化鈕，這正是原本做不出來的原因。改獨立 Toplevel + 自製工具列（最大化/還原/最小化/關閉），`state('zoomed')` 失敗時退回 `attributes('-zoomed')` 相容非 Windows |
| 7 | 參數表格式顯示 | 改成一列一參數的「名稱 = 值」表格（對應使用者圖 5），可動態增列；策略沒設過參數時，用 regex 從 `ctx.param('x', ...)` 掃出可調參數名稱預先列出 |
| 8 | 日K 回測 20 年 | `QT_BACKTEST_DAYS['日K']` 3650 → 7300，周K/月K 一併。**誠實提醒**：20 年只是「預設帶入的起始日」，實際能回測多長仍取決於資料源（期貨 R1 要先匯入期交所歷史；個股沒有這個延伸來源） |
| 9 | 強制終止回測 | `run_backtest` 新增 `should_stop` 回呼，每 64 根 K 棒檢查一次，看到就中止並回傳已完成的部分（不丟例外）。GUI 端在「已有回測進行中」時改為彈出可選「強制終止」的對話框。**誠實說明**：Python 沒有安全的 thread kill，實質是送取消訊號 + 立刻解鎖；若舊工作卡在券商下載 API 上，要等該次呼叫結束才真正停止 —— 這句話寫進對話框 |
| 10 | 最佳化結果字體 | 新增專屬 `Optim.Treeview` 樣式（白底黑字），未達門檻列改深灰字 + 淺灰底。排序本來就是「目標指標由好到壞、第 1 名在最上」，另加說明列讓它顯而易見 |

---

### 診斷工具的兩個既有缺陷（連帶修正）

`diag_mock_tkinter.py` 被發現兩個潛伏問題，都會讓測試**靜默失效**：

1. **`_Treeview` 有兩個 `selection` 定義**，後者覆蓋前者，導致
   `selection_set()` 寫進去的值永遠讀不回來。已刪除重複定義並合併語意
   （有明確 selection 就用它，否則退回 focus，不破壞既有案例）。
2. **`destroy()` 是 no-op、`winfo_exists()` 永遠回 True**，等於完全測不到
   「元件被銷毀後不可再碰」這整類 bug（本專案最常見的崩潰來源之一）。
   已改為忠實模擬：destroy 連帶銷毀子元件、從父容器移除、winfo_exists 反映狀態。

修完第 2 點之後，ADR-057 的診斷案例才真的驗證到「獨立視窗關閉後面板會被清除」。

---

### 需使用者實機驗證

1. `python tests/test_core.py` → 191 項全過。
2. `python diag_repro_issues.py` → 全 PASS（含新增的 ADR-057 案例）。
3. `python diag_crossref.py` → 無斷鏈。
4. **第 11 項（最重要）**：開參數最佳化，用「隨機」模式跑 60~100 次，
   中途切換視窗、開關對話框，程式不該再崩潰退出。
5. 量化分頁按「🗔 開啟量化交易視窗」，在視窗裡新增/編輯/選取策略，
   確認分頁那份清單同步更新；關閉視窗後自動交易若在運轉不受影響。
6. 回測報告：金額欄位無小數（-53,438 而非 -53,438.54）；按「🗖 最大化」
   可放大；按「🧮 驗算這份報告」應顯示 10/10 一致。
7. 新增策略 → 條件類型選「收盤價突破均線 (上穿)」→ 均線期間填 60、
   型態選 EMA → 加入進場 → 條件清單應顯示 `(均線期間=60, 均線型態=EMA)`。
8. 回測期間對話框：參數應是一列一個「名稱 = 值」，起始日預設約 20 年前。
9. 回測跑到一半再按一次回測，應跳出可「強制終止」的對話框。

### 相關程式位置

* `core/strategy_engine.py`：`_ma_of` + 25 個新條件函式、`spec_parts()`、
  `condition_label` 相容 3/4 元素規格與無參數條件。
* `core/backtest.py`：`max_consec_win_amount`/`max_consec_loss_amount`、
  `run_backtest(should_stop=)`、`audit_result()`。
* `stock_app_pro.py`：`gc.disable()` + `_gc_tick`、`_fmt_amt`/`_fmt_amt_signed`、
  `_build_quant_panel`/`_qt_alive_uis`/`_qt_primary_ui`/`open_quant_window`、
  `_qt_refresh_tree`/`_qt_selected`/`_qt_update_status_label`/`log_message` 多面板化、
  `_rebuild_params` 支援 Combobox、`_collect_cond` 保留字串、
  報告視窗工具列與 `_qt_show_audit`、回測期間對話框表格式參數、
  `QT_BACKTEST_DAYS`、`_qt_offer_abort_backtest`、`Optim.Treeview` 樣式。
* `diag_mock_tkinter.py`：`selection`/`selection_set`/`exists`、忠實 `destroy`。
* `tests/test_core.py`：`TestNewConditionsADR057`、`TestBacktestAuditADR057`、
  `TestConsecAmountsADR057`、`TestBacktestCancelADR057`（163 → 191）。
* `diag_repro_issues.py`：新增 ADR-057 案例；ADR-031/038 預期值同步更新。
