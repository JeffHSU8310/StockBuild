
## ADR-055：回測/最佳化「沒反應」根因修正 — decision_to_intents 補漏、參數可追溯性、背景例外必須看得見

### 背景

使用者回報三個現象：(1) 按「回測」完全沒有畫面反應；(2)「參數最佳化」跑完
沒有任何數據；(3) 不確定參數視窗設定的值有沒有真的被策略程式讀到。

### 根因（三個現象、同一個斷點）

ADR-053（反手 Stop-and-Reverse）那一輪把 `core/backtest.py`、
`stock_app_pro.py`（實盤路徑）、`tests/test_core.py` 都改成呼叫
`custom_strategy.decision_to_intents`（複數版，一次可回傳「平多+反手開空」
兩個 intent），但 `core/custom_strategy.py` 本身只補了單數版
`decision_to_intent`，複數版沒有同步寫進去。三個呼叫端各自看起來合理、
`py_compile` 也過（靜態編譯抓不到跨模組屬性缺漏），直到真正執行到那一行
才拋 `AttributeError: module 'core.custom_strategy' has no attribute
'decision_to_intents'`：

- 回測：例外被 `except Exception` 接住只寫進系統日誌，報告視窗不出現 →
  使用者觀感是「按了沒反應」。
- 最佳化：`optimizer.optimize()` 把每組例外塞進 `metrics['error']` 繼續跑
  下一組，所有組合都不合格、`best=None`，結果表一片空白且不說明原因。
- 實盤自訂策略同樣會在訊號觸發時炸掉（同一斷點），只是要等訊號才會現形。

### 修正

1. **補回 `decision_to_intents`**（`core/custom_strategy.py`）：依目前倉位
  ×決策組出 0～2 個 intent；`FLAT→LONG/SHORT` 照舊；`LONG+SELL`→
  `[平多, 開空]`（僅期貨允許反手）；`LONG+CLOSE`→只平不反（CLOSE 是
  「單純出場」語意，不觸發反手）；`SHORT+BUY`同理。單數版
  `decision_to_intent` 原樣保留，向下相容。
2. **參數可追溯性**（`Ctx.param()` / `backtest.run_backtest()` /
  `custom_strategy.describe_param_usage()`）：`ctx.param(key, default)`
  每次被呼叫都記一筆「值、是否來自參數視窗、預設值為何」，回測結果新增
  `param_given`（餵進引擎的參數）與 `param_usage`（策略程式實際讀到的
  參數）。報告視窗與系統日誌顯示三種標記：`✓ 參數視窗`、
  `○ 程式預設,參數視窗未設`、`⚠ 參數視窗有設但程式沒讀到（多半是 key
  拼錯）`——這是使用者「怎麼確定參數有沒有正確代入」的直接答案。
3. **背景 worker 的致命例外一律「彈窗＋完整堆疊入日誌」**：回測失敗改
  `messagebox.showerror` 並把 `traceback.format_exc()` 寫進日誌；最佳化
  結果新增 `errors`/`error_summary`，掃描完成時若全數失敗會直接說明
  「幾組失敗、最常見的錯誤是什麼」，不再是沉默的空表格。

### 需使用者實機驗證

1. `python tests/test_core.py` 全過。
2. 量化分頁選反手策略 →「🔬 回測」，報告要能正常出現，交易明細方向多空
  交替。
3. 報告底部「※ 參數實際代入」對照參數視窗填的值；故意打錯一個 key 再
  回測一次，應出現紅色 `⚠` 提示。
4. 實盤/模擬掛一支自訂策略，訊號觸發時不再拋 AttributeError。

### 相關程式位置

- `core/custom_strategy.py`：新增 `decision_to_intents`、`Ctx.param_reads`、
  `describe_param_usage`。
- `core/backtest.py`：回傳新增 `param_given`/`param_usage`。
- `core/optimizer.py`：回傳新增 `errors`/`error_summary`。
- `stock_app_pro.py`：回測失敗彈窗＋完整堆疊；最佳化無結果時說明原因；
  報告視窗「參數實際代入」列。
- `tests/test_core.py` `TestParamTraceability`（6 案例）。

---

## ADR-056：量化交易易用性與正確性總修正 — 儲存靜默失敗、回測範圍受限、
績效顯示失真、最佳化崩潰與提速、指標設定持久化

### 背景

使用者一次回報十個問題，逐一診斷後分成四類根因，**其中第 1、4 項是同一個
根因**：

### 1／4．內建條件策略「儲存沒反應」＝ 個股「無法回測」

**根因**：使用者的策略停損%＝0、停利%＝0（停用）、停損/停利(元)皆為 0、
且沒有加任何出場訊號 → `strategy_engine.validate_strategy()` 判定
「至少要有一種出場方式，否則持倉永遠不會出場」而拒絕儲存；但舊版驗證
失敗只寫 `log_message`，使用者當下人在「量化交易」分頁（見第 2 項），
系統日誌分頁被切換掉根本看不到，於是策略其實**沒有存進清單**——「個股沒
辦法回測」不是回測本身的 bug，是清單裡壓根沒有那個策略可以選。

**修正**：
- 儲存失敗一律 `messagebox.showerror` 明確列出原因，儲存成功也彈窗確認
  存放位置與下一步。
- 對話框裡加一行黃色提示：「停損%/停利%/停損(元)/停利(元)/出場訊號　
  至少要有一種不為 0，否則無法儲存」，在使用者按下去之前就看得到規則。

### 2．「量化交易分頁」感覺像獨立視窗、看不到操作回饋

**釐清**：量化交易本來就是主視窗內嵌的分頁（非獨立 Toplevel），但它與
「系統日誌」共用同一塊底部空間、兩者互斥顯示（切到量化就把日誌
`pack_forget`）。這正是第 1 項「儲存了但看不到失敗原因」的介面根因。

**修正**：除了關鍵失敗改彈窗，量化分頁按鈕列新增「最新系統訊息」鏡射
標籤，`log_message()` 同步把最新一行寫進這裡——不必切分頁也看得到剛剛
操作的結果。

### 3．參數設定搬進回測對話框

**修正**：自訂策略的「回測期間設定」對話框新增「本次回測參數」欄位，
預填目前的 `custom_params`，可直接修改後按「開始回測」立即套用；勾選
「同時更新已儲存的策略參數」才會連帶覆寫策略本身，預設不勾（僅套用於
這次回測，不影響已儲存設定）。

### 5．日K回測範圍卡在 shioaji 深度，即使已匯入期交所歷史（2000年）

**根因**：ADR-049 的期交所歷史延伸（`_extend_with_taifex`）只掛在主圖
`_publish` 這一條路徑，回測 worker／最佳化 worker 下載完 K 棒、重採樣後
從未呼叫它——兩邊完全是平行世界，匯入的歷史只有「看圖」看得到，回測
永遠用不到。此外 `_extend_with_taifex` 舊版寫死讀 `self.current_contract`
（主圖目前顯示的商品），即使掛上呼叫，回測**別的**商品時依然抓錯合約。

**修正**：
- `_extend_with_taifex` 新增 `contract` 參數，回測/最佳化 worker 一律
  傳入自己解析出來的合約；不傳（主圖呼叫）才 fallback 用
  `self.current_contract`，行為不變。
- 回測、最佳化兩個 worker 都在「重採樣後、依使用者起訖日裁切前」呼叫
  延伸（順序很重要：裁切在延伸之前做，會把延伸出來的更早資料切光）。
- 日K預設回測天數由 1500（~4.1年）拉長到 3650（10年），反映「匯入期交所
  歷史後應該撐得住多久」。個股/沒有期交所歷史的商品照常回測，只是延伸
  不生效，範圍仍受 shioaji 深度限制（下載會如實回報實際抓到幾根）。

### 6．損益不留小數點

**根因**：純粹是顯示格式選擇——所有金額欄位（損益/成本/手續費/交易稅/
權益/現金等）用 `.0f` 格式化，底層計算本身沒有整數截斷。

**修正**：報告視窗、系統日誌、庫存/委託/成交表格、模擬帳戶視窗，凡是
「金額」一律改 `.2f`（保留兩位小數）；數量欄位（口/股/張）維持整數不變。

### 7．最佳化新增「隨機搜索」模式

**背景**：網格搜尋要求使用者自己列出候選值或窄範圍，範圍一寬（例如
`fast=3:50` × `slow=10:200` = 8930 組）會直接因超過 500 組上限被拒絕，
逼使用者手動窄化——這正是「不是我先預設好要的參數」的根源。

**修正**：`core/optimizer.py` 新增 `parse_param_ranges()` /
`random_search()`：使用者只給「下限:上限」，系統在範圍內隨機抽樣 N 次
（預設 60，上限 300）各跑一次回測，依目標指標排名，回傳結構與網格搜尋
完全相同、GUI 共用同一套顯示/套用邏輯。對話框新增「搜尋模式」下拉
（網格／隨機）。**誠實聲明**：隨機搜索不保證找到範圍內的全域最佳，只保證
「抽到的裡面最好的」；次數越多越接近窮舉、也越慢，這是準確度與時間的
取捨，不是免費的午餐——樣本外檢定（walk-forward）依然是judge最終是否
可用的關鍵，不能只看隨機搜索排第一名就套用。

### 8．最佳化跑很久時關閉程式 → RuntimeError 崩潰

**根因**：`safe_after()` 只攔截 `tk.TclError`。「參數最佳化」單次可能跑
數百組回測，期間若使用者把整個程式關掉，Tk 的 mainloop 已停止，背景
執行緒排程 `self.after()` 這一步 tkinter 拋的是
`RuntimeError('main thread is not in main loop')`，不是 TclError，原本
的 `except` 接不住，例外讓背景執行緒崩潰、進而拖垮整個程式；某些 Tcl
組建下還會在極端競態下印出 `Tcl_AsyncDelete: async handler deleted by
the wrong thread`（這是 Tcl C 執行期訊息，Python 層攔不到）。

**修正**：`safe_after()` 排程與回呼執行兩處都改接
`(tk.TclError, RuntimeError)`；最佳化的 `should_stop` 除了看使用者按
「⛔ 停止」，也看 `self._closing`，程式一進入關閉流程就讓迴圈盡快跳出，
縮小「還在呼叫 Tk 但視窗已經在銷毀」的競態窗口。**誠實聲明**：Tcl
C 執行期層級的崩潰無法在 Python 這層 100% 杜絕，這個修正把發生機率壓到
最低，不是理論上的絕對保證——長時間背景工作的通用原則見 PITFALLS P-59。

### 9．主／副圖指標參數持久化

**釐清**：舊版 MA/布林/MACD/RSI/KDJ/DMI 這些參數只存在 tk.Variable
記憶體裡，程式關掉就消失，並非「不小心存了不該存的」，而是「想存卻沒有
存」。比照 `chart_layout.json` 的模式，新增 `data/config_store.py` 的
`load_indicator_settings`/`save_indicator_settings`：讀不到/壞掉一律
回退程式碼預設值，絕不因設定檔問題讓圖表畫不出來。**存檔時機是明確動作
觸發**——只有在「主圖指標參數設定」/「XX 參數設定」對話框按下
「確認並套用（並記住此設定）」才寫檔，不是每次打字或每次畫圖就默默
存檔，對話框內也加了提示文字說明會記住。

### 10．最佳化「要下載什麼」＋ 效能優化

**釐清**：下載只發生一次（原始 K 棒，10 分鐘內同商品同範圍重跑免下載），
不是每組參數重下載一次；真正慢的是 CPU：自訂策略的 `on_bar` 每根K棒都
用「只到目前這根」的截斷視窗重新計算 SMA/EMA/RSI/MACD/KD/布林等指標，
對 n 根K棒的完整回測是 O(n²)（2500 根日K單次回測要算約 310 萬個指標點；
最佳化掃 500 組參數就是 15 億點）。這是為了「絕不用未來K棒」
（P-49）刻意付出的代價，不是隨便能省的。

**優化（不改變任何結果，只改變快慢，已用測試證明數值完全一致）**：
- `Ctx` 新增 `full_df` 參數與指標結果快取（`state['_ind_cache']`）：
  同一組（指標,參數）整段回測只計算一次（用完整資料算，但回傳給策略前
  永遠裁到「只到目前這根」），把 O(n²) 降到 O(n)。因果性/無未來函數
  的安全保證完全不受影響（rolling/EMA/RSI 等本來就只往回看，用完整
  資料算出來的第 i 個值與只用前 i+1 列算出來的第 i 個值數學上完全相同，
  已用測試證明：竄改「未來」列的值不會改變過去任何一根的指標輸出）。
- 策略原始碼 `compile()`＋`exec()` 改為模組級快取（同一段程式碼文字只編譯
  一次），避免回測每根K棒都重新編譯使用者程式碼。
- 實測：2500 根日K、2 個 SMA 呼叫的回測，優化前 1.11 秒、優化後 0.43 秒
  （約 2.6 倍）；指標越多、K棒越長、參數組合越多，加速比例越大。

### 需使用者實機驗證

1. `python tests/test_core.py` 全過（163 項）。
2. 個股 2330 策略：停損%/停利%都填 0 且不加出場訊號，按「儲存策略」應
  跳出紅字錯誤說明；改填合理的停損%後應能成功儲存並看到成功彈窗，且
  清單裡出現這支策略，可以正常回測。
3. 已匯入期交所歷史的期貨 R1（如 TXFR1）：回測起始日填 2015-01-01（或
  更早），應能真的抓到期交所延伸的資料，日誌出現
  「【期交所歷史】... 已用官方每日行情往前延伸至 ...」。
4. 回測/最佳化報告的金額欄位應顯示兩位小數。
5. 「參數最佳化」切換到「隨機」模式，填 `fast=3:50; slow=10:200`，
  嘗試次數用預設 60，應能正常掃描出結果（網格模式對同樣範圍應該會被
  500 組上限擋下）。
6. 主圖「⚙」指標設定按「確認並套用（並記住此設定）」後重開程式，設定
  應該還在。
7. 量化分頁存策略/回測時，不需要切到「系統日誌」分頁也能看到操作結果
  （分頁按鈕列的「最新:」那行）。

### 相關程式位置

- `core/custom_strategy.py`：`Ctx.full_df`/`_cached`/指標方法改快取、
  `_compile_on_bar`/編譯快取。
- `core/backtest.py`：呼叫 `run_on_bar` 時傳入 `full_df=df`。
- `core/optimizer.py`：新增 `parse_param_ranges`/`random_search`/
  `_eval_combo`/`_summarize_errors`（網格與隨機共用）。
- `data/config_store.py`：新增 `DEFAULT_INDICATOR_SETTINGS`/
  `load_indicator_settings`/`save_indicator_settings`。
- `stock_app_pro.py`：內建策略儲存彈窗＋提示文字；量化分頁「最新訊息」
  鏡射；`_qt_backtest_ask_range` 加參數欄位＋`_parse_kv_params`；
  `_extend_with_taifex` 加 `contract` 參數；回測/最佳化 worker 掛上
  延伸呼叫；`QT_BACKTEST_DAYS['日K']` 1500→3650；金額格式 `.0f`→`.2f`；
  `safe_after` 攔截 `(TclError, RuntimeError)`；最佳化 `should_stop` 併入
  `self._closing`；最佳化對話框新增搜尋模式/嘗試次數；
  `_collect_indicator_settings`/`_apply_indicator_settings`/
  `_save_indicator_settings`；`open_main_settings`/`open_sub_settings`
  的「確認並套用」按鈕接上存檔。
- `tests/test_core.py`：`TestIndicatorCachePerf`、`TestCompileCache`、
  `TestRandomSearch`、`TestIndicatorSettingsPersistence`、
  `TestTaifexExtendExplicitContract`（共 20+ 案例）。
- `diag_repro_issues.py`：庫存損益案例的預期字串同步更新為兩位小數。
