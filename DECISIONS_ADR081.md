# ADR-081：期貨全系列 (近月/次月/遠月) 連續月份數據抓取與期交所歷史全對應

- **日期**：2026-07-22
- **狀態**：已實作並通過單元測試 (265/265 pass)

## 背景與需求
使用者回報：期貨抓取數據時，無論是一般日盤或全日盤，近月、次月及遠月均需為「連續月份 (Continuous Contracts)」數據，而非受限於單一特定當月合約。

原機制限制：
1. `core/taifex_daily.py` 舊版僅支援最小到期月份 (`month_rank=1`, 近一/R1)，無法自動衍生並儲存 `R2` (次月連續) 日K資料。
2. `_extend_with_taifex` 原先硬性限定僅延伸 R1，若使用者查詢 `TXFR2` / `MXFR2` 或選取次月/特定月份進行回測時，期交所歷史延伸無法生效。

## 變更項目
1. **`core/taifex_daily.py`**：
   - `build_front_month_daily` 新增 `month_rank` 參數 (預設 `1`)：
     - `month_rank=1`：建構近一連續月 (R1)。
     - `month_rank=2`：建構次月連續月 (R2)。
     - `month_rank=3`：建構遠月連續月 (R3)。

2. **`data/taifex_store.py`**：
   - `store_path` / `has_daily` / `load_daily` / `save_daily` 支援 `month_rank`：
     - `TX.csv` / `TX_day.csv` (R1 近一連續, 全時段/日盤)
     - `TX_R2.csv` / `TX_R2_day.csv` (R2 次月連續, 全時段/日盤)

3. **`stock_app_pro.py`**：
   - 新增 `_month_rank_of(contract)` 自動識別合約連續等級 (R1 / R2 / R3)。
   - `_taifex_merge_save` 匯入時自動並行產出與更新 R1 近一及 R2 次月的全時段/日盤共 4 個歷史檔案。
   - `_extend_with_taifex` 與 `_taifex_plan_download` 全面對應 R1/R2 合約，無縫前接歷史連續數據。

## 驗證
- `python tests/test_core.py` 跑 265 個單元測試全數 OK (包含新增的 `test_month_rank_r1_and_r2_build`)。
