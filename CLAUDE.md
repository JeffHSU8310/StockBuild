# CLAUDE.md — 專案憲法

> 台股自動交易系統 (tkinter + shioaji)。任何人 (包含 Claude) 開工前必讀
> 本文件，再讀 `PITFALLS.md` 與 `ARCHITECTURE.md`；重大決策一律記 ADR。
> 本文件的規則優先於「看起來更方便」的做法。
>
> 本文件只列「規則本身 + 出處」。每條規則背後的症狀、根因、完整做法都在
> `PITFALLS.md` 的對應 P 編號條目，需要細節時再去查 (不要憑印象動手)。

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

## 專案現況速覽

- **介面**：tkinter + ttk；**繪圖**：matplotlib / mplfinance
- **行情與下單**：永豐金證券 `shioaji` API。**1.5.6 與 1.7 都支援**
  (ADR-114 相容層:指數代碼、輕量合約型別、login 參數差異都收斂在
  `core/sj_compat.py` + `brokers/sinopac.py`,不要在其他地方寫版本判斷)
- **資料源政策 (ADR-011)**：台股一律 shioaji，未登入直接報錯；美股自動用
  yfinance。禁止 yfinance/FinMind 台股備援 (見鐵則 12)。
- **分層** (完整說明見 `ARCHITECTURE.md`)：
  - `stock_app_pro.py` — GUI 本體，唯一可碰 tkinter widget 與 shioaji 連線的地方
  - `core/`、`data/` — 純邏輯與設定 I/O，零 tkinter/shioaji，離線可測 (ADR-009)
  - `brokers/` — 券商 adapter (ADR-097 起)，允許依賴券商 SDK，為未來多券商預留
- **測試**：`python tests/test_core.py` — 離線、秒級跑完，改 `core/`/`data/` 後必跑
- **執行環境**：Python 3.14

> ⚠️ 若你手邊另一份記憶/文件提到 PySide6 + pyqtgraph + 三層 core/data/chart
> 架構，那是**另一條技術路線的討論**，與本專案不是同一套；要合併或取捨需
> 先開 ADR，不要私自假設哪個才是主線。

---

## 鐵則

每條的症狀/根因/完整做法見 `PITFALLS.md` 對應條目。

1. **紅漲綠跌，絕不可換。** 台股慣例紅=漲、綠=跌，寫反一律視為 bug 立即修正 (P-19)。

2. **shioaji 串流只用 v1 typed callback，不用 v0 字典 callback**；
   以物件上的 `intraday_odd` 欄位分流零股/整股 (P-01，ADR-005)。

3. **零股與整股的報價暫存永遠分開，讀寫一律經 `self.quote_lock`**，
   不可裸讀寫 (P-04)。

4. **盤後/無串流的五檔絕不捏造**：沒有真實資料的檔位顯示 `--` 並在 UI
   標示「(參考)」/「快照」(P-02)。

5. **`snapshots()` 必須節流**：無串流 fallback 間隔 ≥ 3 秒 (ADR-094 查證
   官方流量上限後定案)；要再調整必須先查 shioaji 官方文件並記新 ADR (P-03)。

6. **零股下單只能限價 ROD、數量 1~999 股，送出前在本地擋下**，
   不要送去給券商退單 (P-09)。

7. **價格顯示一律走 `fmt_price()` 的台股 tick 規則，不可無腦 `.2f`** (P-20)。

8. **每一路訂閱獨立 try/except 並把成敗記到系統日誌**，
   不可包成一個大 try 讓失敗無聲無息 (P-05)。

9. **實盤下單前的關鍵欄位 (價格/數量/限市價/整零股) 先在本地驗證**
   (`core/order_rules.py`)，不依賴券商回傳錯誤 (P-09)。

10. **影響「資料正確性」或「架構走向」的改動必須記 ADR**，即使是小修正，
    讓未來的 session 不重新踩一次坑 (記錄方式見文末「專案文件」)。

11. **`core/`、`data/` 維持零 tkinter、零 shioaji 依賴 (ADR-009)**：
    純計算/純規則優先寫成 `core/` 純函式並補測試；改完必跑
    `python tests/test_core.py` 全數通過才能交付 (P-27)。

12. **台股資料一律 shioaji，不可加回 yfinance/FinMind 備援 (ADR-011)**：
    未登入就直接報錯，不安靜退化；要恢復須使用者確認 + 新 ADR (P-26)。

13. **背景執行緒排回 UI 一律用 `self.safe_after(...)`**，
    不可直接呼叫 `self.after(...)` (P-22，ADR-012)。

14. **下單一定先跳確認視窗；只有 `_confirm_and_place_order()` 可以呼叫
    `place_order()`** (P-10，ADR-013)。委託數量上限常數在
    `core/order_rules.py`，是本系統自訂防呆，調整前先確認使用者真的要調。

15. **關閉視窗依序：logout → destroy → `os._exit(0)` 保底**，
    三步都要有 (P-23/P-46，ADR-014)。

16. **現沖 checkbox 只在換新標的當下決定一次起始值；使用者手動取消勾選後，
    不可被其他操作悄悄勾回** (P-13，ADR-015)。

---

## 每次開工流程 (Session Workflow)

1. 讀本文件 → `PITFALLS.md` (查要改的區塊有沒有已知坑) → `ARCHITECTURE.md`
   (確認不破壞模組界線)。
2. 動工前，先用一句話跟使用者確認改動類別：
   - 純 bug 修正 → 不需新 ADR (牽涉資料正確性仍建議記錄)。
   - 架構/資料源/協定層級 → 先寫 ADR 草案，使用者同意後才動工。
3. 改 `core/`/`data/` → 跑 `python tests/test_core.py` 全數通過，
   新增純邏輯要補對應測試，不要只交程式碼不交測試。
4. 改 `stock_app_pro.py` 的 GUI/shioaji 耦合部分 → 此工作環境常無畫面，
   headless 驗證用 diag 腳本 (見 ARCHITECTURE.md「驗證方式速查」)，
   交付時明確附上「請使用者實機驗證哪些操作」。
5. 交付附：改了什麼、為什麼改、怎麼驗證。重大決策附 ADR 草案，
   使用者確認後才寫入。

---

## 專案文件

- `PITFALLS.md`：已知陷阱清單，格式「症狀→根因→正確做法→出處」，
  涵蓋報價/資料、K 線聚合、下單、繪圖版面、生命週期/執行緒、資料源政策、
  開發/測試七大區。改到哪一區先查對應的坑。
- `ARCHITECTURE.md`：分層界線、執行緒模型、三大資料流 (歷史 K 線/即時報價/
  下單)、各範圍的驗證方式、目錄結構。界線變動先開 ADR。
- **ADR 紀錄**：ADR-005~044 在 `DECISIONS.md` 本體；**ADR-057 起每筆獨立
  一個 `DECISIONS_ADR0XX.md` 檔案**，新 ADR 沿用獨立檔案慣例。
  **取新編號前，先查檔名與程式碼註解中已用掉的最大編號** (兩處都要查，
  曾發生只查其中一處而撞號的實例)。

> 踩到新坑/修掉舊坑 → 同步更新 `PITFALLS.md`；界線/資料流變動 → 同步更新
> `ARCHITECTURE.md`；重大決策 → 新 ADR。

---

## 架構重構第二階段 (ADR-009 backlog，暫緩)

拆 `create_widgets()` 為獨立建構函式/Mixin、把網路 worker 抽成不依賴
tkinter 的 client 類別，這兩項需要能開視窗的環境實測才能動工；
動工前先跟使用者確認環境，不要在 headless 環境動 GUI 層。
