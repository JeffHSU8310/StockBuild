
## ADR-114：升級 shioaji 1.5.6 → 1.7（做成相容層，兩版都能跑）

### 決定

**不是「改成 1.7」，是「同時支援 1.5.6 與 1.7」。**

升級券商 SDK 動到的是整個系統的資料入口（鐵則 12：台股一律 shioaji）。
一次改死成 1.7，萬一實機發現問題，退回的成本是「再改一次全部呼叫點」；
做成相容層則是**降版套件就能退回，程式一行都不用動**，而且同一份程式碼在
兩個版本下都有測試覆蓋。

### 事實來源：讀 wheel，不是讀升級指南

以下變更全部來自**直接讀 shioaji 1.7.0 wheel 裡的 `shioaji/_core.pyi`**：

| 項目 | 1.5.6 | 1.7.0 |
|---|---|---|
| `login()` | `(api_key, secret_key, contracts_timeout=10000)` | `(api_key, secret_key, subscribe_trade=True, receive_window=30000, force_refresh=False)` |
| 指數代碼 | `TSE001` / `OTC101` | 交易所代碼（加權 `IX0001`） |
| 合約型別 | `Contract`（含 symbol/category/unit…） | 新增輕量型別 `StockInfo`/`IndexInfo`/`FuturesInfo`/`OptionInfo`/`WarrantInfo` |

**升級指南有三處與實作不符**，記下來免得未來被誤導：

| 指南聲稱 | 1.7.0 `.pyi` 實際 |
|---|---|
| `fetch_contracts()` 已移除 | 仍存在 |
| `Contracts.status` 已移除 | 仍存在 |
| `SecurityType.Future` → `Futures` | 仍是 `Future` |

本專案沒用到這三個，所以不影響——但這說明**不能照指南寫版本分支**（P-62）。

另外要修正一個容易誤解的說法：「`symbol` 被移除」的正確理解是
**「列舉合約時拿到的可能是沒有 symbol 的輕量型別」**，而不是「`Contract`
沒有 symbol 了」——完整的 `Contract` 仍然有 symbol/category/unit/reference。

### 兩個核心設計

#### 一、不問版本號，問簽名

```python
kw, dropped = sj_compat.supported_kwargs(self.api.login, {'contracts_timeout': 10000})
self.api.login(api_key=..., secret_key=..., **kw)
```

版本號是宣告，簽名是事實。這樣連「某個中間版本部分支援」也自然處理掉。
被丟掉的參數會回報給呼叫端**寫進系統日誌**——升級後排查問題時，第一個要
確認的就是「現在到底跑在哪一版、有沒有走相容路徑」。

取不到簽名時（C 擴充常見，shioaji 是 Rust 編的）**原樣送出**：寧可看到真正
的 `TypeError`，也不要靜悄悄丟掉使用者指定的參數。

#### 二、不猜代碼，依序試候選

```python
INDEX_CANDIDATES = {
    'TSE': ('IX0001', 'TSE001', '001'),
    'OTC': ('IX0002', 'OTC101', 'OTC001', '101'),
}
```

新代碼在前，哪個拿得到就用哪個。過渡期兩種代碼並存也能自動處理。
存取形狀同樣試兩種：`Indexs.get('IX0001')`（1.7 的 ContractCategory）與
`Indexs.TSE.TSE001`（1.5.6 的巢狀群組）。

**一個容易踩的陷阱**：1.5.6 的 `Indexs.TSE` 是**群組**不是合約。若不擋掉，
`resolve_index` 會把群組當合約回傳，拿去訂閱/下單會出現非常難懂的錯誤。
用「有沒有 `code` 字串屬性」區分（合約一定有，群組沒有）。

### 改動的呼叫點

| 位置 | 處數 | 改法 |
|---|---|---|
| `login(contracts_timeout=…)` | 2 | 走 `supported_kwargs` 過濾 |
| `Contracts.Indexs.TSE.TSE001` 等 | 6 | 改呼叫 `broker.index_contract('TSE'/'OTC')` |
| `Contracts.Stocks` 掃描比對 `c.symbol` | 2 | 改呼叫 `broker.stock_contract(code)`，symbol/code 都比 |
| `strategy_engine.looks_like_index_symbol` 硬編碼指數代碼 | 1 | 改讀 `sj_compat.INDEX_SYMBOLS`（含 IX0001/IX0002） |

`.category` / `.unit` / `SecurityType.Future` / `fetch_contracts()` /
`Contracts.status` 本專案**都沒有使用**，所以 1.7 的其餘變更不影響。

### 已驗證（headless）

- `tests/test_core.py` **540 個全過**（新增 16 個相容層測試）
- `tests/test_brokers.py` 42 全過、`diag_repro_issues.py` **41 案例全過**
- 新增診斷案例用**假物件模擬 1.5.6 與 1.7 兩種容器形狀**，走真正的 GUI 路徑
- **突變測試**（確認斷言不是空的）：
  - 拿掉 `IX0001`/`IX0002` 候選 → 案例 ERROR
  - `match_contract_code` 改回只比 `symbol` → 案例 ERROR
- `diag_crossref.py` 乾淨、`py_compile` 全過

### 明確**沒有**驗證的部分

- **真實的 1.7 連線**。指數的實際代碼（`IX0001` 來自升級指南，`IX0002` 是
  推論）、列舉 `Contracts.Stocks` 實際回傳哪一種型別、合約自動載入的時序，
  這三項只能實機確認。相容層的設計就是為了讓這些**猜錯也不會壞**——候選
  清單沒中就往下試，型別不同就兩個欄位都比。
- 1.7 改成「查詢時自動載入合約」對登入流程的影響。我們在 ADR-032／P-48 為
  「登入凍結」做過一整套防護（背景執行緒、watchdog、GIL 爭用），那套防護的
  前提是「登入時整批下載合約」。1.7 之後登入應該更快，防護不會出錯但可能
  變得多餘——**先不動**，等實機看到實際登入耗時再決定。

### 升級步驟（使用者操作）

```bash
# 1. 先備份目前可用的版本 (退路)
pip download shioaji==1.5.6 -d ./sj_backup --no-deps

# 2. 升級
pip install --upgrade shioaji==1.7.0

# 3. 確認版本
python -c "import shioaji; print(shioaji.__version__)"
```

**要退回時**：`pip install shioaji==1.5.6`，程式碼不用動。

### 需使用者實機驗證

**升級後第一次開程式**
1. 登入後看系統日誌，應出現
   `【shioaji 1.7.0】此版 login() 不接受 contracts_timeout,已略過…`
   —— 這行是「相容層有生效」的直接證據。若顯示的是「沿用舊版行為」，代表
   還在跑 1.5.6。
2. 記一下**登入花多久**（1.7 應該明顯變快）。若反而變慢或卡住，回報我，
   那代表 ADR-032 的登入防護需要跟著調整。

**核心功能（指數是最高風險項）**
3. 主圖查 `^TWII`（加權指數）與 `^TWOII`（櫃買指數），確認**畫得出 K 線**。
   這是整個升級最可能壞的地方——若查無，請把系統日誌貼給我，我會依實際的
   指數代碼調整候選清單。
4. 上方的大盤/櫃買指數即時報價要正常跳動。
5. 主圖查一檔**上市股**（如 2330）與一檔**上櫃股**，確認都找得到。
6. 期貨（如 `TXFR1`）查詢與訂閱正常。

**策略**
7. 有用「看A做B」看指數的策略，確認訊號來源仍解析得到。
8. 用**模擬**模式跑一段，確認委託流程正常，再考慮實單。

**退路**
9. 任何一項不對，`pip install shioaji==1.5.6` 就能回到原狀，程式碼不用改。

---

## 追記：讀完官方指南全文後的修正

使用者已升級到 1.7，並要求細讀指南。用 `curl` 取原始 HTML 自己讀（前次是
透過摘要，漏掉細節），發現兩件**先前做錯/漏掉**的事。

### 一、修掉一個真的 bug：期貨查詢仍在裸讀 `contract.symbol`

指南明載 `Stock → StockInfo`、`Future → FuturesInfo`，**`symbol` 已移除，
改用 `code`**。前一版我只改了「掃描個股」那兩處，**主圖的期貨查詢還有兩處
直接讀 `contract.symbol`** —— 在 1.7 會取不到，**期貨查詢會整個失效**。

已改用 `sj_compat.contract_symbol()`（symbol 取不到就退回 code）。

順帶一提：舊的 `symbol` 是交易所前綴形式（`'TSE2330'`），而 `code` 是
`'2330'`。我們這幾處是拿來跟使用者輸入的代號比對與顯示，**用 `code` 其實
比舊的 `symbol` 更正確**。

### 二、櫃買指數代碼：從瞎猜改成有根據的推論

指南寫的是：

| 1.5.x | 1.7.0 |
|---|---|
| `api.Contracts.Indexs.TSE["001"]` | `api.Contracts.Indexs.TSE["IX0001"]` |

兩個重點：

1. **1.7 仍然保留交易所群組**（`Indexs.TSE[...]`），只是代碼變了。不是像我
   原先猜的那樣一定要走 `api.contracts.get()`。診斷案例已補上這個形狀。
2. 規則是 **`IX` + 舊代碼補零到 4 位**：加權舊代碼 `001` → `IX0001`。
   櫃買舊代碼是 `101`，依同一規則應為 **`IX0101`** —— 這比我原先猜的
   `IX0002` 有根據得多。

候選清單改成 `IX0101` → `IX0002` → `OTC101` → `OTC001` → `101`，兩種可能都
留著。**櫃買代碼在官方指南中確實沒有列出**，實機確認後可以把沒中的拿掉。

### 三、指南確認我們不受影響的項目

指南的「是否必須改」欄逐項對照本專案：

| 變更項目 | 是否必須改 | 本專案 |
|---|---|---|
| 登入參數（三個移除） | 是，若有使用 | ✅ 已處理（簽名過濾） |
| `fetch_contracts()` / `Contracts.status` | 是，若有輪詢 | 沒有使用 |
| 合約物件欄位（symbol 移除、update_date 型別變更） | 是，若讀取已變動欄位 | ✅ 已處理；`update_date` 沒有使用 |
| 指數代碼 | 是，若有使用指數 | ✅ 已處理 |
| `SecurityType.Future` → `Futures` | 是，若有使用 | 沒有使用 |
| HTTP 列出商品 | 僅 HTTP | 不適用（用 Python binding） |

`category` → `root`、`unit` 移除（期貨/選擇權）本專案也都沒有使用。

### 新增的防護

診斷案例補了一條**原始碼層級**的斷言：主程式裡不可再出現裸讀 `.symbol`。
理由是真正的 bug 在「有真連線才走得到」的 GUI 路徑上，純函式測試守不住它
——這次就是這樣漏掉的。已用突變測試確認：把任一處改回 `contract.symbol`，
診斷案例會 FAIL。

### 仍未實機驗證

- 櫃買指數的實際代碼（`IX0101` 為推論）
- 1.7 實際回傳的 `FuturesInfo` 的 `code` 格式是否等同舊的 `TXFR1`
