# ARCHITECTURE.md — 架構現況

> 台股自動交易系統 (`stock_app_pro.py`) 的分層、界線、執行緒模型與資料流。
> 開工前先讀 `CLAUDE.md`、`PITFALLS.md`，改動不要破壞這裡描述的界線。
> 界線變動（例如把 GUI 邏輯搬進 `core/`，或反過來）一律先開 ADR 再動工。

---

## 一、分層總覽

本專案分成三層，界線的判準是**「有沒有 tkinter / shioaji 依賴」**：

```
┌─────────────────────────────────────────────────────────────┐
│  GUI 本體 (stock_app_pro.py)                                  │
│  StockTradingAppPro(tk.Tk)                                    │
│  · 深度依賴 tkinter widget / matplotlib / mplfinance          │
│  · 資料抓取 worker、即時報價 worker、shioaji callback         │
│  · 繪圖 draw_chart、下單面板互動、版面微調                     │
│  · 驗證方式:假 tkinter 診斷 (diag_*) + 實機                    │
│                                                               │
│   ├── 呼叫 ──►  core/  (純邏輯,零 tkinter / 零 shioaji)        │
│   │            · tick_rules   台股 tick 規則、價格格式化        │
│   │            · indicators   MA/BB/MACD/RSI/KDJ/DMI 計算       │
│   │            · futures_session  期貨交易日聚合 (ADR-007)      │
│   │            · order_rules  委託規則驗證 (ADR-008)            │
│   │            · 驗證方式:tests/test_core.py (離線,無需網路)    │
│   │                                                            │
│   └── 呼叫 ──►  data/  (設定 I/O,零 tkinter)                   │
│                · config_store  券商設定 / 自選股 / 版面設定讀寫 │
│                · 驗證方式:tests/test_core.py                    │
│                                                                │
│   ├── 呼叫 ──►  brokers/  (券商 adapter,零 tkinter,可依賴券商 SDK) │
│                · base      BrokerClient 共用介面骨架 (ADR-097)  │
│                · sinopac   永豐 shioaji adapter — 目前只涵蓋   │
│                  連線生命週期 (login/logout/callback 註冊);    │
│                  委託/報價/部位等呼叫仍在 stock_app_pro.py 直接 │
│                  使用 self.sj_api (見 ADR-097 階段0 範圍聲明)   │
└─────────────────────────────────────────────────────────────┘
```

> `brokers/` 跟 `core/`/`data/` 不同：它允許依賴各券商第三方 SDK (如
> shioaji)，存在理由是「封裝券商 SDK 差異」而不是「離線可測的純邏輯」。
> 未來群益/兆豐/凱基 adapter 會陸續加進這個套件 (見 DECISIONS_ADR097.md)。

**鐵律**：`core/` 與 `data/` 必須維持零 tkinter、零 shioaji 依賴（鐵則 11 / ADR-009 /
PITFALLS P-27）。它們存在的唯一理由就是「可離線單元測試」。

> ⚠️ 若別處記憶提到 PySide6 + pyqtgraph + 三層 core/data/chart 架構，那是**另一條
> 技術路線**，與本專案 tkinter+shioaji 主線不是同一套。本專案的 `core/`/`data/`
> 是 ADR-009 獨立決定的，命名雖相似，範圍目前只涵蓋純邏輯與設定 I/O，GUI 本體
> 仍在 `stock_app_pro.py`。兩者要合併須先開 ADR。

---

## 二、各層職責與界線

### GUI 本體：`stock_app_pro.py`（`StockTradingAppPro`）
唯一可以碰 tkinter widget、matplotlib、shioaji 連線的地方。內部再概分四類
（耦合度由低到高）：

1. **純邏輯薄封裝**：`get_tick`/`fmt_price`（→ `tick_rules`）、
   `calculate_custom_indicators`（→ `indicators`）、`_resample_future_session`
   （→ `futures_session`，並保留例外處理 + 自然日退回）、`execute_order` 內的
   委託驗證（→ `order_rules`）。這些方法只負責「從 `self` 讀值 → 呼叫純函式 →
   把結果寫回 `self` 或印日誌」。
2. **檔案 I/O 薄封裝**：`load_config`/`save_config`/`load_watchlists`/
   `save_watchlists`/版面設定（→ `data/config_store`）。
3. **網路 / broker I/O**：`fetch_data_worker`、`fetch_realtime_worker`、
   `fetch_market_indices_worker`、shioaji 登入與 quote/order callback。需要
   `safe_after` 排回 UI、需讀寫即時狀態，屬中度耦合。
4. **GUI 本體**：`create_widgets` 與所有事件處理、`draw_chart`、下單面板互動、
   `_apply_chart_margins`/版面微調對話框。深度依賴 widget 物件本身。

### 核心邏輯層：`core/`
- `tick_rules.py`：`get_tick(price, asset_type, raw_symbol)`、
  `fmt_price(...)`、`round_to_tick(...)`。顯式吃參數，不讀 `self`。
- `indicators.py`：`calculate_indicators(...)`，顯式吃 MA/BB/MACD/RSI/KDJ/DMI
  參數。**刻意保留** MACD/RSI/KDJ/DMI 共用一個 try/except 的既有耦合（PITFALLS
  P-29），要拆需另開 ADR。
- `futures_session.py`：`resample_future_session(...)`、
  `resample_natural_day_fallback(...)`。純函式版**不吞例外**，例外往上拋，由 GUI
  層決定要不要退回自然日與記日誌。
- `order_rules.py`：`validate_stock_order(...)`、`is_daytrade_eligible(...)`，
  回傳 `(ok, reason)`，不做任何日誌或 I/O。常數 `MAX_QTY_LOT`（499 張）、
  `MAX_QTY_ODD`（999 股）。
- `kbars_plan.py`（ADR-122）：「這個 kbars 請求要不要分段、每段幾天」的
  **單一出處**。shioaji 的 `kbars()` 一律回 1 分 K，所以「天數」直接等於
  資料量，範圍一大單次請求就容易逾時。門檻與段長原本只寫在
  `fetch_data_worker` 裡的字面值，策略路徑要用同一套規則時只能再抄一份 ——
  收斂到這裡，並由 diag 的原始碼層級斷言確保兩邊不會改岔。
  注意 `chunk_plan()` 以**切出來幾段**為準，不是以門檻為準（PITFALLS P-91）。
- `market_session.py`（ADR-070/121）：「現在這個市場開盤了沒」的單一真相來源。
  `is_market_open()` 是自動交易的開/收盤閘門；`just_opened()`（ADR-121）另外
  回答「是不是剛開盤 N 秒內」，讓策略不要在鐘響那一秒去打券商 API。
  時刻常數（`STOCK_OPEN_MIN` / `FUT_DAY_OPEN_MIN` / `ODD_LOT_OPEN_MIN` …）
  都在這裡，其他地方不可以另寫一份。
- `regime_panel.py`（ADR-120）：主圖【盤勢判斷】面板的純邏輯。
  `normalize(raw)` 把設定檔讀到的東西整理成一份值域安全的設定（設定檔壞掉
  絕不可以變成主圖畫不出來）；`should_evaluate(settings, symbol, timeframe)`
  是型態偵測的三道閘門（總開關+子項／限日K／限加權指數）；
  `plan_notifications(signals, state, bar_date)` 是通知去重 —— 主圖每縮放/
  平移一次就重畫一次，沒有這層會被同一個型態洗版（PITFALLS P-87）。
  它與 `market_pattern.py`（型態偵測）、`volume_profile.py`（量價支撐壓力）
  的分工是：後兩者算「現在是什麼狀態」，`regime_panel` 決定「要不要算、
  要不要說」。

### 設定存取層：`data/`
- `config_store.py`：`load_broker_config`/`save_broker_config`、
  `load_watchlists`/`save_watchlists`、`DEFAULT_CHART_LAYOUT` +
  `load_chart_layout`/`save_chart_layout`。路徑顯式參數傳入，不讀 `self`。

### 測試與診斷
- `tests/test_core.py`：涵蓋 `core/`/`data/` 全部模組（目前 40 個案例），
  離線、不需 tkinter/shioaji/網路，`python tests/test_core.py` 秒級跑完。
  **改 `core/`/`data/` 後必跑**。
- `diag_mock_tkinter.py`：假 tkinter / matplotlib backend / yfinance / mplfinance
  模組（假 mplfinance 用**真 matplotlib `add_axes`** 建面板，見 PITFALLS P-28），
  讓 `StockTradingAppPro` 能在無畫面環境建構並實跑 `draw_chart` 等路徑。
- `diag_repro_issues.py`：重現/驗證使用者回報問題的診斷腳本（版面 set_position、
  委託 seed、小數點即時轉換等）。
- 兩個 diag 是開發除錯用，不隨 App 發布。

---

## 三、執行緒模型

```
主執行緒 (tk mainloop)
  └─ 所有 widget 建立、事件處理、繪圖、下單確認視窗

背景 daemon 執行緒 (自行 threading.Thread(daemon=True) 開)
  ├─ fetch_data_worker          抓歷史 K 線 → 排回 UI 畫圖
  ├─ fetch_realtime_worker      讀報價暫存 → 排回 UI 更新五檔/行情列
  └─ fetch_market_indices_worker 抓大盤指數 → 排回 UI

shioaji 內部執行緒 (我們沒開、無法保證是 daemon)
  ├─ v1 quote callback (tick / bidask, 整股 / 零股 / 期貨)
  │     → 經 self.quote_lock 寫入報價暫存
  └─ order callback (on_order_deal_callback)
        → _handle_order_event / _handle_deal_event → safe_after 排回 UI
```

**量化 runner (`quant_runner_worker`) 是單執行緒**：同一條迴圈上依序跑
所有策略的評估、`_qt_chukuangren_execute_pass`、以及每 3 秒的
`_qt_update_realtime_pnl`（期貨即時停損停利靠它）。因此**這條迴圈裡絕對
不可以 `time.sleep()` 做退避重試** —— 睡多久就等於即時停損停擺多久。
需要重試時用迴圈自己的 2 秒節奏（ADR-121 的做法，見 PITFALLS P-90）。
同理，**大範圍 K 線的分段下載也不在這條迴圈上做**：改由背景預抓執行緒
（ADR-122 `_qt_start_kbars_prefetch`）補進 `_kbars_raw_cache`，runner 這一輪
拿不到就照 ADR-121 的 boundary 還原機制等下一個 tick。

三條規則（違反就會踩 PITFALLS P-04 / P-22 / P-23）：
1. **報價暫存跨執行緒讀寫一律經 `self.quote_lock`**；零股/整股暫存永遠分開。
2. **任何背景執行緒要更新 UI，一律 `self.safe_after(...)`**，不裸用 `self.after`。
3. **關閉視窗**：`on_app_close()` → `logout()` → `destroy()` → `os._exit(0)`
   保底，因為 shioaji 內部執行緒可能不是 daemon、行程不會自然結束。

---

## 四、三大資料流

### (A) 歷史 K 線：查詢 → 繪圖
```
使用者輸入代碼 / 切換週期
  → start_fetch_thread (遞增 _fetch_seq;換商品才清報價暫存)
  → fetch_data_worker (background, 帶 seq)
      · is_taiwan_instrument? 台股一律 shioaji;未登入直接報錯 (P-26)
        美股才 yfinance
      · 同商品換週期:跳過退訂/重訂閱,串流不中斷 (ADR-024)
      · 【三路擇一,全部經 _publish + seq 防護 (P-36/ADR-024)】
        a. 快取涵蓋 → 重採樣秒開;stale 則先畫再背景刷新
        b. 期貨/指數日K以上首載 → 小範圍搶先出圖,背景補全並平移 xlim
        c. 一般 → 完整下載 (SJ_DAYS 控制天數) → 存快取 → 出圖
      · 期貨:_resample_future_session (交易日聚合, P-07)
        股票/指數:自然日 resample
  → safe_after 排回 update_ui (過期 seq 直接放棄)
      · 重置 hover 列 (P-18)
      · draw_chart(current_df)
          · calculate_custom_indicators (→ core/indicators)
          · mpf.plot(returnfig=True, tight_layout=False)
          · _apply_chart_margins → set_position 逐面板定位 (P-15)
          · 主圖/副圖逐項獨立上色文字、vlines
          · FigureCanvasTkAgg → pack(fill=BOTH, expand=True) (P-16)
```

### (B) 即時報價：callback → 暫存 → UI
```
shioaji v1 callback (背景執行緒)
  → 經 quote_lock 寫入 current_tick_normal/odd、current_bidask_normal/odd
     整股 tick 順便快取 last_norm_close、closing_oddlot_close (P-06)
fetch_realtime_worker (背景執行緒, 節流)
  → 經 quote_lock 讀暫存;無串流時 snapshots() 每 ≥5 秒一次 (P-03)
  → safe_after 排回 update_quote_ui
      · 五檔:真實一檔 + 參考檔位(--);盤後標「(參考)」(P-02)
      · 紅漲綠跌 (P-19);價格走 fmt_price (P-20)
```

### (C) 下單：驗證 → 確認 → 送出 → 回報
```
按 買進/賣出 → execute_order(action)
  · 即時全形→半形小數點已在 KeyRelease 處理 (P-21)
  · round_to_tick 保底修正 (P-20)
  · validate_stock_order (→ core/order_rules):模式/條件/數量上限/零股規則 (P-08/P-09)
  · 組 shioaji Order (當沖旗標多版本 try, P-14)
  → _show_order_confirmation(ctx)  ← 一定跳確認視窗 (P-10)
      ├─ 取消 → 只記日誌,不送出
      └─ 確認送出 → _confirm_and_place_order(ctx)   ← 唯一可 place_order 的方法
            · sj_api.place_order
            · seed my_orders:取 id 獨立防護,空 id 用 _pending_ 暫時 key
              成功/失敗都印明確日誌 (P-11)
            · _refresh_my_orders_ui (例外不靜默吞)
set_order_callback → on_order_deal_callback (背景執行緒)
  ├─ Order 事件 → _handle_order_event:_pending_ 暫時 key 換成正式 id、更新狀態
  └─ Deal 事件  → _handle_deal_event:累加已成交量、更新「我的已成交」
  (全程主動回報,不輪詢 update_status, P-12;更新 UI 經 safe_after)
```

**【ADR-116】籌碼與選股分頁可「開啟完整視窗」**：用「搬家」模型 —— 開窗時把
面板從分頁拆掉、在視窗重建，關窗時再搬回去，全程只有一份 panel（這兩個面板
的 widget 直接掛在 `self.*`，複製成兩份會讓其中一份變孤兒，見 P-67）。
量化交易則是另一種模型：分頁與視窗兩份同時存在，靠 `_qt_uis` 清單維護。

底部區塊為分頁式：`系統日誌與回報` / `我的委託單` / `我的已成交`
（`set_bottom_tab` 切換）。**排查委託問題時要看「系統日誌」分頁**，seed 的
成功/失敗日誌印在那裡（P-11）。

### (D) 遠端控制：手機指令 → 主執行緒 → 狀態變更（ADR-108）
```
_tg_poll_worker (背景 daemon,getUpdates long polling)
  · remote_control 沒開就不建立這個執行緒 (沒用這功能 = 行為與加它之前相同)
  → safe_after(0, _tg_handle_command, chat_id, text)   ← 一律排回主執行緒 (鐵則13)
      · is_authorized:只認設定檔那一個 chat_id;未授權不回覆、只記日誌
      · 唯讀 (/status /list /positions /pnl /help) → 直接回覆
      · 停用 (/off /stop_all) → _tg_apply 立即生效,不需確認
      · 啟用 (/on /start_all) → _qt_enable_blockers 先擋 → 發確認碼
            → /yes <碼> → _tg_confirm → _tg_apply (再檢查一次)
      · 狀態變更一律走 _qt_finish_set_enabled / _qt_stop_all,與畫面同一條路
  ※ 沒有任何下單/改單指令 —— 買賣一律回主程式走 (C) 的確認視窗 (鐵則14)
```

---

## 五、驗證方式速查

| 改動範圍 | 驗證方式 |
|---|---|
| `core/`、`data/` 純邏輯 | `python tests/test_core.py`（必跑）+ 補對應測試 |
| 「每次重畫都會跑」的主圖附加判斷（盤勢判斷等） | diag 要**連續 `draw_chart()` 多次**再斷言日誌沒有增加（PITFALLS P-87）|
| `brokers/` 券商 adapter | `python tests/test_brokers.py`（必跑）；真實連線只能實機 |
| `draw_chart`/版面/下單流程等 GUI 耦合 | `diag_repro_issues.py` 等假 tkinter 診斷 |
| 任何檔案 | `python -m py_compile` + `python diag_crossref.py`（跨模組斷鏈 **與重複定義**，ADR-109） |
| shioaji 連線、即時報價、實鍵盤輸入法、實機排版顏色 | **只能請使用者實機驗證**，交付時附「怎麼驗」 |
| Telegram 遠端控制 | `core/telegram_control.py` 測純邏輯 + diag 走 GUI 派送路徑；真實 Bot 收發只能實機驗證 |

---

## 六、目錄結構

```
G:\StockBuild\
├─ stock_app_pro.py        GUI 本體 (主程式)
├─ CLAUDE.md               專案憲法 (鐵則 + 開工流程)
├─ ARCHITECTURE.md         本文件
├─ PITFALLS.md             已知陷阱清單
├─ DECISIONS.md            架構決策紀錄 (ADR-005 起)
├─ core/                   純邏輯 (零 tkinter/shioaji)
│   ├─ tick_rules.py
│   ├─ indicators.py
│   ├─ futures_session.py
│   ├─ telegram_control.py  遠端控制:授權/確認碼/指令解析 (ADR-108)
│   ├─ order_intent.py      券商中立的委託意圖 (ADR-110)
│   ├─ broker_ipc.py        券商子行程 IPC 協定 (ADR-112)
│   ├─ sj_compat.py        shioaji 1.5.6/1.7 相容 (指數代碼/合約型別/簽名, ADR-114)
│   ├─ market_pattern.py    加權指數盤勢/型態偵測 (只提醒,不下單)
│   ├─ volume_profile.py    量價支撐壓力 (POC/價值區/高量節點, ADR-102)
│   ├─ regime_panel.py      主圖【盤勢判斷】:設定正規化 + 通知去重 (ADR-120)
│   ├─ market_session.py    交易時段/開盤暖機 (ADR-070/121)
│   ├─ kbars_plan.py        kbars 分段門檻/段長的單一出處 (ADR-122)
│   └─ order_rules.py
├─ data/
│   └─ config_store.py     設定 / 自選股 / 版面 I/O
├─ brokers/                券商 adapter (零 tkinter,可依賴券商 SDK,ADR-097)
│   ├─ base.py              BrokerClient 介面:連線/下單/帳號 (ADR-110)
│   ├─ sinopac.py           永豐 shioaji adapter (連線 + 委託翻譯 + 帳號解析)
│   ├─ kgi.py               凱基 kgisuperpy adapter (ADR-111;帳號綁定需上鎖)
│   ├─ kgi_proxy.py         凱基「獨立 3.13 子行程」代理 (ADR-112)
│   └─ kgi_worker.py        凱基子行程本體 (**用 Python 3.13 執行**)
├─ tests/
│   ├─ test_core.py        core/ + data/ 離線單元測試
│   └─ test_brokers.py     brokers/ 離線測試 (照 SDK 原始碼複刻的假模組,ADR-111)
├─ diag_mock_tkinter.py    假 tkinter/mplfinance 環境 (開發用)
├─ diag_repro_issues.py    問題重現/驗證腳本 (開發用)
├─ broker_config.json      券商設定
├─ watchlists.json         自選股清單
└─ chart_layout.json       圖表版面設定 (可由「📐 版面微調」調整並儲存)
```

> 註：`chart_layout.json` 內殘留的 `canvas_width_delta`/`canvas_height_delta`
> 兩個 key 已無作用（ADR-020 移除了對應滑桿），為維持舊檔載入相容暫時保留；
> 日後清理需同步更新 `config_store.DEFAULT_CHART_LAYOUT` 與測試。
