
### P-57　跨模組改名／新增函式，必須「呼叫端＋被呼叫模組」同批交付，且要有工具把關
- **症狀**：`backtest.py`／`stock_app_pro.py`／`tests/test_core.py` 都改成
  呼叫 `custom_strategy.decision_to_intents`（複數版），但
  `core/custom_strategy.py` 本身沒有同步補上這個函式。三個呼叫端各自
  看起來合理、`py_compile` 全過（靜態編譯抓不到跨模組屬性缺漏），直到
  真正執行到那一行才拋 `AttributeError`——使用者感受到的是「回測按下去
  沒反應」「參數最佳化沒有任何數據」，完全查不出來是哪裡斷的。
- **根因**：多檔案協同修改時，若「呼叫端」與「定義端」分屬不同檔案，
  很容易只顧著改呼叫端（因為那裡是這次任務的焦點），漏了同步定義端；
  `py_compile`／語法檢查對這種錯誤完全無感，只有真正執行到那一行的
  離線測試或診斷案例才抓得到。
- **正確做法**：(1) 收尾檢查除了既有的 `py_compile` + `test_core.py` +
  `diag_repro_issues.py`，再加一步 `python diag_crossref.py`——這支腳本
  掃描專案內所有 `.py` 檔的跨模組屬性存取（`module.attr`），檢查 `attr`
  是否真的存在於被參照的模組，抓不到來源的（第三方套件、動態 import）
  一律跳過不誤報；(2) 這支腳本也認得住「函式內 lazy import 給局部變數、
  再賦值成另一個名字往下傳」這種寫法（例如
  `from . import custom_strategy as _cs; custom_fn = _cs`），因為第一版
  只認 import 語句本身，反而抓不到原本這次真實發生的斷鏈；(3) 收尾流程
  發現任何一步失敗，都要先懷疑「是不是改名字/加函式時漏了同步某個檔案」，
  而不是只看最後改的那個檔案有沒有問題。
- **出處**：ADR-053 遺漏、ADR-055 修正、`diag_crossref.py` 新增。

### P-58　背景 worker 的失敗只寫系統日誌 = 使用者眼中的「按了沒反應」
- **症狀**：自訂策略回測遇到例外，只把錯誤訊息寫進「系統日誌與回報」
  分頁；使用者當下若在別的分頁（尤其量化交易分頁與日誌分頁互斥顯示，
  切到量化就把日誌 pack_forget），完全看不到那行日誌，體感就是「按了
  儲存/回測完全沒反應」。同一模式也出現在「策略儲存失敗」：
  `validate_strategy` 判斷不通過只 log，不彈窗，策略其實沒存進清單，
  使用者卻以為存了、下一步去回測時自然找不到策略。
- **根因**：GUI 觸發的背景工作（下載、回測、最佳化）一旦失敗，若只有
  「寫日誌」這一種回饋管道，就假設了使用者一定看得到系統日誌分頁——
  但底部分頁是互斥式切換，使用者操作的當下極可能人根本不在那個分頁。
- **正確做法**：任何「使用者主動觸發、有可能失敗」的操作（儲存策略、
  回測、最佳化…），失敗一律要有**視覺上主動彈出**的回饋
  （`messagebox.showerror`／`showinfo`），日誌只當作「事後可查的完整
  記錄」，不能是唯一管道。如果操作發生在某個分頁情境下，該分頁本身
  最好也要有一份「最新狀態」的鏡射（本專案在量化分頁加了
  `lbl_qt_last_log`），讓使用者不需要切分頁確認結果。
- **出處**：ADR-055、ADR-056。

### P-59　`safe_after` 只接 `TclError` 不夠——Tk 收尾期間丟的是 `RuntimeError`
- **症狀**：「參數最佳化」跑很久（數百組回測）時把整個程式關掉，出現
  `RuntimeError: main thread is not in main loop`，接著
  `Tcl_AsyncDelete: async handler deleted by the wrong thread`，程式
  整個崩潰退出。
- **根因**：本專案所有背景執行緒都透過 `safe_after()` 排程 GUI 更新，
  舊版只用 `except tk.TclError` 防護「視窗已關閉」的情況。但當 Tk 的
  mainloop 已經完全停止（root 已 destroy）時，tkinter 對 `.after()`
  拋的是 `RuntimeError`，不是 `TclError`，原本的 except 接不住，例外
  未被捕捉就讓背景執行緒（甚至整個程式）崩潰。某些 Tcl 組建在這個競態
  窗口下，C 執行期層級還會直接印出 `Tcl_AsyncDelete` 訊息甚至中止進程
  ——這一層 Python 完全攔不到，只能靠縮小競態窗口去降低機率，無法
  100% 杜絕。
- **正確做法**：(1) `safe_after` 排程呼叫與回呼執行兩處都要接
  `(tk.TclError, RuntimeError)`，而不是只接 `TclError`；(2) 長時間跑
  很多輪的背景迴圈（最佳化網格/隨機搜索），`should_stop` 判斷除了看
  使用者主動取消，也要一併看 `self._closing`，程式進入關閉流程就讓
  迴圈盡快跳出，不要繼續嘗試碰觸 Tk；(3) 任何新增的「會跑很久、且會
  頻繁呼叫 `safe_after` 回報進度」的背景工作，都要照這個模式處理，
  不能假設 `safe_after` 已經把所有收尾情境都擋掉了。
- **出處**：ADR-056。
