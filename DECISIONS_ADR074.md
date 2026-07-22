
## ADR-074：看A做B — 用 A 的訊號 (可為指數) 下單到 B,且看A/做B 週期可不同

### 背景 / 需求

使用者:
- 「增加看A做B的功能。看A包含指數 (加權&櫃買&台指期貨等,各項商品皆可);
  做B:除了加權&櫃買指數不行外,其餘商品皆可。寫成可以勾選,及使用自訂策略,
  有勾選也可以看A做B。」
- 「還要有看A的週期、做B的週期,比如看A的30分K,要做B的5分K。」

### 設計

一句話:**條件/指標看 A;下單、損益看 B。** 兩者商品與週期都能不同。

**核心洞察 (讓引擎改動極小)**:`evaluate_strategy` 內所有純量 `close` 的用途都是
「被交易商品的價格」(下單價、停損停利損益基準),而「條件判斷」讀的是傳入的
`df_closed`。因此只要:
- `df_closed` 傳 **A** 的 K 棒 (訊號/指標算在 A 上),
- 新增參數 `exec_close` 傳 **B** 的最新收盤價,函式內把價格基準 `close` 換成它。

`exec_close=None` 時 = 看A做A (一般模式),`close` 就是 A 自己的收盤,**既有行為
完全不變** (所有舊測試照過)。

**新欄位 (`new_strategy()`)**:`watch_enabled` / `watch_symbol` / `watch_trade_type`
(股票/期貨/**指數**) / `watch_timeframe`。未啟用時 A=B。輔助函式
`watch_enabled/watch_symbol_of/watch_trade_type_of/watch_timeframe_of` 與
`looks_like_index_symbol` 收斂判斷。

**限制 (驗證擋下)**:B (執行商品) 不可為指數 (加權/櫃買不能下單);A 可為任何
商品含指數。內建與自訂編輯器的驗證都擋。

**執行迴圈 (`_quant_eval_pass`)**:
- 邊界節奏依 **A 的週期** (`watch_timeframe_of`) —— 訊號來自 A。
- `_qt_resolve` 解 B (下單/損益);新增 `_qt_resolve_watch` 解 A (支援指數
  ^TWII→TSE001、^TWOII→OTC101/OTC001)。
- 抓 **A** 的已收盤 K 棒 (A 的週期/代碼) 給條件;看A做B 時另抓 **B** 的最新
  已收盤價當 `exec_close`。B 當下無價就先不動作。
- 內建 → `evaluate_strategy(..., exec_close=)`;自訂 → `decision_to_intents`
  的 price 帶 B 的價 (on_bar 仍看 A 的 df)。下單一律走 B 的合約/種類。
- 交易時段閘門 (ADR-070) 仍以 **B 的市場** 判斷 (只有 B 能下單時才動作)。

**UI (內建 + 自訂共用 `_qt_build_watch_panel`)**:勾選「👁 看A做B」、看A 商品代碼、
種類 (股票/期貨/指數)、看A週期;即時查名確認 A 代碼。上方原本的「商品代碼/週期」
即為做B (執行)。

**快取 key** 併入週期 (`QT|市場|代碼|週期`):A/B 可能同代碼不同週期,分開快取更安全。

### 測試

`tests/test_core.py` 新增:輔助函式與驗證 (B 不可指數、A 週期非法擋下)、
`exec_close` 讓下單價與停損損益都以 B 計 (與 A 收盤脫鉤)。共 262 測試通過。

### 已知限制 (誠實告知,之後可再開 ADR 補)

- **回測尚未支援看A做B**:回測仍以策略自身 (B) 的 K 棒同時當訊號與執行 (等於
  看B做B),不會套用 A 的訊號。實盤/模擬自動交易 (本次主要需求) 才是完整看A做B。
  回測要一致需要「A 訊號序列 + 每根對齊 B 價格」的改動,量體較大,另案處理。
- A 與 B 的 K 棒是各自抓取對齊「最後一根已收盤」;極端情況 (某一邊資料延遲)
  該輪先不動作,下一根再評估。

### 相關程式位置

- `core/strategy_engine.py`:`evaluate_strategy(exec_close=)`;watch 輔助函式;
  `new_strategy()` 新欄位;`validate_strategy` 的 B-不可指數/看A設定檢查。
- `stock_app_pro.py`:`_qt_resolve_watch`;`_qt_fetch_closed_bars` 加 tf/代碼/市場
  覆寫;`_quant_eval_pass` 看A做B 分流;`_qt_build_watch_panel` 共用面板 + 兩個
  編輯器接線 + 存檔;自訂編輯器 `_validate_custom` 的 B-不可指數檢查。
- `tests/test_core.py`:看A做B 相關測試 (+2)。

### 需使用者實機驗證

1. 建一檔內建策略:看 A=加權(^TWII)/30分K,做 B=某股票/5分K,勾「看A做B」→
   存檔應成功;試跑/模擬時,訊號依加權 30分K,下單價用該股票最新價。
2. 把 B 設成 ^TWII → 存檔應被擋 (指數不能做B)。
3. 自訂 Python 策略勾看A做B:on_bar 收到的是 A 的 K 棒,下單到 B。
4. 一般 (不勾看A做B) 策略行為應與以前完全一致。
