# CLAUDE.md — 專案憲法

> 台股自動交易系統。任何人 (包含 Claude) 開始工作前，必須先讀完本文件，
> 再讀 `ARCHITECTURE.md` 與 `PITFALLS.md`，重大決策一律記入 `DECISIONS.md`。
> 本文件的規則優先於「看起來更方便」的做法。

---

## 溝通與 Git 工作流程規則

- **所有回應一律使用繁體中文**，不可以用簡體中文或英文回覆使用者 (程式碼內的
  英文變數/函式名稱、註解慣例不受此限，這條規則只規範「跟使用者對話的文字」)。
- **開發一律先在暫時的獨立分支進行** (例如 `claude/...-fxr392`)，修改完成後
  在該分支測試/驗證。**不可以自行合併回 `main`**，一定要等使用者在有畫面的
  機器上實機驗證沒問題、明確要求合併，才把該分支的修改併入 `main`。
- 使用者要 Claude 抓取「最新版本」時，直接讀取 `main` 分支即可 (不是暫時分支)，
  因為 `main` 有可能被使用者用其他工具 (例如 Antigravity) 直接修改過。

---

## 專案現況速覽 (2026-07 版本)

- **介面框架**：tkinter + ttk
- **繪圖**：matplotlib / mplfinance (`mpf.plot`, `FigureCanvasTkAgg` 嵌入)
- **即時行情與下單**：永豐金證券 `shioaji` API (目前對接版本 **1.5.6**)
- **資料源政策 (ADR-011，2026-07-12 起)**：台股 (股票/ETF/指數/期貨) **一律使用
  shioaji**，未登入券商 API 時直接報錯，不再有 yfinance/FinMind 備援；美股自動
  使用 yfinance (shioaji 本來就不支援美股)。FinMind 登入功能已整個移除，
  法人/資券籌碼指標 (原本唯一資料源是 FinMind) 也一併移除。
- **執行檔**：`stock_app_pro.py` (GUI 本體、資料抓取、繪圖、下單面板互動)
- **核心邏輯層** (ADR-009，2026-07-12 起)：`core/` 套件，零 tkinter/shioaji 依賴，
  可離線單元測試：
  - `core/tick_rules.py` — 台股 tick 規則與價格格式化 (`get_tick`/`fmt_price`)
  - `core/indicators.py` — 技術指標計算 (MA/BB/MACD/RSI/KDJ/DMI)
  - `core/futures_session.py` — 期貨交易日聚合 (ADR-007)
  - `core/order_rules.py` — 委託規則驗證 (ADR-008)
- **設定存取層**：`data/config_store.py` — 券商設定檔與自選股清單讀寫
- **測試**：`tests/test_core.py` — 涵蓋上述 core/ 全部模組，30 個測試案例，
  執行 `python tests/test_core.py` 即可 (不需要 tkinter/shioaji/網路)
- **執行環境**：Python 3.14

> ⚠️ 注意：若你手邊還有另一份記憶/文件提到 PySide6 + pyqtgraph + 三層 core/data/chart
> 架構，那是**另一個技術路線的討論**，與目前 `stock_app_pro.py` 的實作不是同一套。
> 兩者若要合併或取捨，需先開一條 ADR 決策再動工，不要私自假設哪個才是主線。
> (本專案的 `core/`/`data/` 分層是 ADR-009 獨立決定的，命名雖然相似，但範圍
> 目前只涵蓋純邏輯與設定檔 I/O，GUI 本體仍在 `stock_app_pro.py`，見下方鐵則11。)

---

## 10 條鐵則

1. **紅漲綠跌，絕不可換。**
   台股顯示慣例：紅色=上漲，綠色=下跌。所有 K 線、五檔買賣價、漲跌文字顏色都必須遵守，
   即使某段程式碼手滑寫反，一律視為 bug 立即修正。

2. **shioaji 串流資料只能用 v1 typed callback，不用 v0 字典 callback。**
   `set_quote_callback` (v0) 用 topic 字串判斷零股/整股不可靠，已證實會造成資料污染
   (2026-07-11 ADR-005)。一律用 `set_on_tick_stk_v1_callback` /
   `set_on_bidask_stk_v1_callback` 等 v1 callback，並讀取物件上的 `intraday_odd`
   布林欄位分流零股與整股。

3. **零股與整股的資料暫存永遠分開，不共用同一組變數，且讀寫必須加鎖。**
   `current_tick_normal` / `current_tick_odd`、`current_bidask_normal` /
   `current_bidask_odd` 這兩組不可以互相覆蓋。因為 shioaji callback 在獨立的
   背景執行緒觸發，而 UI worker (`fetch_realtime_worker`) 在另一條執行緒讀取，
   兩邊都要透過 `self.quote_lock` 存取，不可以裸讀寫。

4. **盤後 / 無串流時的五檔絕不能捏造成交量。**
   shioaji 的 `Snapshot` 物件**沒有** `bid_ask` 屬性，只有最佳一檔的
   `buy_price/sell_price/buy_volume/sell_volume`。任何「用總量除以 10 展開五檔」
   之類的算法都是假數據，一律禁止。沒有真實資料的檔位就顯示 `--`，並在 UI 上
   明確標示「(參考)」或「快照」字樣，不可以讓使用者誤以為是真實五檔。

5. **對 `snapshots()` 的呼叫必須節流，不可以在迴圈裡無限制猛打。**
   shioaji 有每日 API 流量配額，盲目高頻呼叫會在盤中把配額用光，導致整個系統
   當天報價全部失效。目前定案：無串流 fallback 快照間隔 ≥ 5 秒。若要調整這個
   數字，必須先查 shioaji 官方文件的流量限制並記錄依據到 DECISIONS.md。

6. **零股下單只能限價 ROD，數量 1~999 股；違反者在送出前擋下，不要送去給券商退單。**
   這是交易所規則，不是我們可以放寬的選項。`execute_order()` 裡的防呆檢查
   不可以被移除或繞過。

7. **所有價格顯示必須依照 `get_tick()` 的台股跳動單位規則格式化，不可以無腦 `.2f`。**
   ETF (00 開頭) 與一般股票的 tick 表不同，價格帶 (10/50/100/500/1000) 交界處
   容易出現非法價位，一律呼叫 `fmt_price()` 之類的共用函式，不要在各處各自
   寫一份格式化邏輯。

8. **訂閱 (subscribe) 每一路要獨立 try/except 並記錄成功或失敗，不可以包成一個大 try 讓失敗無聲無息。**
   四路訂閱 (整股 Tick / 整股 BidAsk / 零股 Tick / 零股 BidAsk) 各自失敗互不影響，
   且必須把結果印到系統日誌，方便排查「為什麼零股沒資料」。

9. **實盤下單前的關鍵欄位 (價格、數量、限價/市價、整股/零股) 都要在本地端先驗證一次，不要完全依賴券商回傳的錯誤訊息。**
   本地擋得下來的錯誤 (如零股掛市價、數量超界) 不要浪費一次 API 呼叫送到券商才被拒。

10. **任何會影響「資料正確性」或「架構走向」的改動，必須在 `DECISIONS.md` 留一筆 ADR，即使是小修正。**
    未來的 session (不管是不是同一顆模型) 都要能看 DECISIONS.md 就知道「這個地方為什麼是這樣寫，不要重新踩一次坑」。

11. **`core/` 與 `data/` 套件必須維持零 tkinter、零 shioaji 依賴 (ADR-009)。**
    這兩個套件存在的唯一理由是「可以離線單元測試」；一旦裡面出現
    `import tkinter` 或 `import shioaji`，或是函式簽名開始依賴 `self`/
    tkinter Variable，這個保證就破功了。新增功能時，純計算/純規則邏輯
    (技術指標、tick 規則、委託驗證、資料聚合) 優先寫成 `core/` 底下的
    純函式並補上 `tests/test_core.py` 的對應測試；需要 `self.after()`、
    UI 元件、或 shioaji callback 的部分才留在 `stock_app_pro.py`。
    修改 `core/`/`data/` 任何檔案後，必須執行 `python tests/test_core.py`
    確認全數通過才能交付。

12. **台股 (股票/ETF/指數/期貨) 資料一律使用 shioaji，不可以加回 yfinance/FinMind
    備援 (ADR-011)。**
    未登入券商 API 時，台股相關查詢要直接報錯退出並提示使用者先登入，
    不可以安靜地退化成其他資料源；美股才自動使用 yfinance。FinMind 登入
    功能與法人/資券籌碼指標已整個移除，不要因為某個功能「以前有」就
    順手加回來，這是使用者明確要求的政策，如果之後要恢復，需要先確認
    使用者真的要改回來，並且用新的 ADR 記錄理由。

13. **任何背景執行緒排回 UI 更新，一律用 `self.safe_after(...)`，不可以直接
    呼叫 `self.after(...)` (ADR-012)。**
    `fetch_market_indices_worker`/`fetch_realtime_worker` 這類 daemon
    thread 會一直跑到程式行程結束；使用者關閉視窗時，如果背景執行緒還在
    嘗試呼叫 `self.after()` 更新已經被銷毀的 widget，會噴出
    `_tkinter.TclError: invalid command name`。`safe_after()` 做了兩層
    防護 (排程前檢查 `self._closing`、callback 真正執行時再檢查一次並
    包 `try/except TclError`)，新增任何背景執行緒或排程更新都要沿用這個
    方法，不要圖方便直接寫 `self.after(...)`。

14. **下單前一定要跳出確認視窗，「確認送出」才能真正呼叫 `place_order()`
    (ADR-013)。**
    `execute_order()` 只負責驗證與組裝 shioaji Order 物件，組好之後交給
    `_show_order_confirmation()` 顯示欄位讓使用者最後確認一次；只有
    `_confirm_and_place_order()` 這個方法可以呼叫 `self.sj_api.place_order(...)`。
    不可以為了「方便測試」或「趕功能」就把送單邏輯繞過確認視窗直接接回
    `execute_order()`。委託數量上限 (整股/盤後定價 499 張、零股類 999 股)
    定義在 `core/order_rules.py` 的 `MAX_QTY_LOT`/`MAX_QTY_ODD`，這是本
    系統自訂的保守防呆上限，要調整請先確認使用者真的要調整。

15. **關閉視窗一定要先嘗試登出 shioaji、再用 `os._exit(0)` 保底強制結束
    整個行程 (ADR-014)。**
    shioaji 底層的 WebSocket 連線與內部執行緒不是我們自己開的，無法保證
    是 daemon thread；只呼叫 `self.destroy()` 不夠，行程可能永遠不會自然
    結束，導致終端機關了視窗也跳不回提示字元。`on_app_close()` 必須依序：
    (1) 已登入時嘗試 `self.sj_api.logout()`，(2) `self.destroy()`，
    (3) `os._exit(0)` 保底強制結束。這三步都要有，不要因為「看起來已經
    關閉視窗」就以為程式真的結束了——沒有 `os._exit()` 保底時，行程可能
    卡在背景關不掉。

16. **現沖 (先賣後買) checkbox 的「合格時預設打勾」只在換新標的當下決定
    一次，不可以在使用者手動取消勾選後被其他操作悄悄覆蓋回去 (ADR-015)。**
    換新標的時依 `current_day_trade` 自動設定 `daytrade_var` 的起始值
    (可以現沖就自動打勾)，這是使用者明確要求且確認過的行為。但
    `update_daytrade_checkbox_state()` (交易別/種類等按鈕切換時會呼叫到)
    只負責「不合格時強制清空並鎖住」，合格時不可以主動把 `daytrade_var`
    設回 `True`——這樣才能讓使用者在同一檔股票內手動取消勾選的選擇持續
    有效，不會因為點了其他按鈕就被悄悄改回勾選。新增任何會呼叫
    `update_daytrade_checkbox_state()` 的地方，都要注意不要破壞這個
    「系統只決定起始值，使用者能在單筆委託上覆蓋」的設計。

---

## 每次開工流程 (Session Workflow)

1. 讀 `CLAUDE.md` (本文件)。
2. 讀 `PITFALLS.md`，確認要改的區塊有沒有已知陷阱。
3. 讀 `ARCHITECTURE.md`，確認改動不會破壞既有的模組界線。
4. 動工前，先用一句話跟使用者確認「這次改動屬於哪一類」：
   - 純 bug 修正 → 不需要新 ADR，但若牽涉資料正確性仍建議記錄。
   - 架構/資料源/協定層級的改動 → 一定要先寫 ADR 草案，使用者同意後才動工。
5. 修改完成後：
   - 若改動 `core/`/`data/` 底下的純邏輯，**執行 `python tests/test_core.py`**
     確認全數通過 (30 個測試，不需要 tkinter/shioaji/網路，約 0.03~0.05 秒
     跑完)；若新增了新的純邏輯函式，補上對應的測試案例，不要只交程式碼
     不交測試。
   - 若改動 `stock_app_pro.py` 裡跟 tkinter/shioaji 深度耦合的部分 (GUI 元件、
     即時報價 worker、下單流程)，這個工作環境可能沒有畫面可以實測，
     提醒使用者在有畫面的機器上手動驗證 (啟動 App + 檢查系統日誌 + 走過
     相關操作流程)。
   - 交付完整可覆蓋的檔案，並附上這次改了什麼、為什麼改、怎麼驗證。
6. 若這次改動屬於重大決策 (資料源變更、callback 機制變更、下單流程變更等)，
   在交付的同時附上 ADR 草案，等使用者確認後才寫入 `DECISIONS.md`。

---

## 專案文件 (已補齊)

本 Project 的治理文件已到位，開工前依序讀：
- `CLAUDE.md`（本文件）：鐵則 + 開工流程。
- `PITFALLS.md`（ADR-020 補上）：已知陷阱清單，格式為「症狀→根因→正確做法
  →出處 ADR」，涵蓋 shioaji 報價/資料、K 線聚合、下單、繪圖/版面、生命週期/
  執行緒、資料源政策、開發/測試七大區。改到哪一區先查對應的坑。
- `ARCHITECTURE.md`（ADR-020 補上）：分層現況（`core/`/`data/` 零 tkinter/shioaji
  純邏輯與設定 I/O、`stock_app_pro.py` GUI 本體）、執行緒模型、三大資料流
  （歷史 K 線 / 即時報價 / 下單）、各自的驗證方式與目錄結構。界線變動先開 ADR。
- `DECISIONS.md`：架構決策紀錄（ADR-005 起）。

> 三份文件會隨新的修正持續更新：踩到新坑或修掉舊坑時，同步更新 `PITFALLS.md`；
> 界線/資料流變動時，同步更新 `ARCHITECTURE.md`；重大決策一律記 `DECISIONS.md`。

## 架構重構第二階段路線圖 (ADR-009，尚未執行)

以下項目已在 ADR-009 評估過，因為需要在有畫面的環境驗證而暫緩，
排入之後有空檔且能實測時再做：

1. 把 `create_widgets()` 拆成幾個獨立的建構函式或 Mixin (下單面板/圖表面板/
   自選股面板)，讓 `StockTradingAppPro` 本體只負責組裝。
2. 把 `fetch_data_worker`/`fetch_market_indices_worker` 這類「網路 I/O + 排回
   UI」的邏輯抽成不依賴 tkinter 的 client 類別，透過 callback 跟 GUI 層互動。
3. 這兩項動工前都要先跟使用者確認「這次要在能開視窗的環境進行」，不要在
   只能跑 headless 測試的環境裡動 GUI 層的程式碼。
