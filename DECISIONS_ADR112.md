
## ADR-112：凱基跑獨立 Python 3.13 子行程，主程式用 IPC 溝通

### 使用者的決定

ADR-111 查出 `kgisuperpy` 在 Python 3.14 載不起來（自帶 C 擴充只編到 cp313，
且載入時會拉進沒有 3.14 版的 numba，見 P-59）。兩條路擺出來後，使用者選擇：

> 凱基跑獨立 3.13 行程，主程式用 IPC 溝通（隔離性好，要多寫一層）

### 架構

```
主程式 (Python 3.14)                    子行程 (Python 3.13)
─────────────────────                   ────────────────────
KGIProcessBroker                        brokers/kgi_worker.py
  (brokers/kgi_proxy.py)   ── stdin ──►   讀 JSON 請求
  實作 BrokerClient        ◄── stdout ──  寫 JSON 回應
                                          ↓
                                        KGIBroker (brokers/kgi.py)
                                          ↓
                                        kgisuperpy
```

對 `stock_app_pro.py` 而言，`brokers['kgi']` 跟 `SinopacBroker` 沒有差別 ——
這是 ADR-110 把介面切出來的回報：多一層行程邊界，上層一行都不用改。

### 最重要的設計：送出委託後失聯，結果是「不確定」

主程式把 `place_order` 寫進管線之後，如果逾時或管線斷掉，**無法知道委託
到底有沒有送到券商**。兩種直覺猜法都會賠錢：

| 猜法 | 後果 |
|---|---|
| 當成「失敗」 | 策略維持 FLAT，下一根 K 棒再進場一次 → **可能重複下單** |
| 當成「成功」 | 記了一個可能不存在的部位 → 之後平倉會平到空氣 |

正確答案是**第三種狀態**，而且分三層落實：

1. `core/broker_ipc.py` 把操作分成冪等（查詢類，失聯可重送）與**非冪等**
   （`place_order`）。非冪等操作失聯時拋獨立的例外型別
   `OrderOutcomeUnknown` —— 刻意不用回傳值或一般錯誤，讓呼叫端**被迫**
   分開處理，不可能不小心混進 `except Exception` 當成「沒送出」。
2. `KGIProcessBroker._call()` 在非冪等操作失聯時**絕不自動重送、也絕不
   自動重啟行程後再送一次**。（寫入 stdin 就失敗是例外：那代表請求根本
   沒出去，重來是安全的。）
3. `_place_strategy_order()` 收到 `OrderOutcomeUnknown` 時**自動停用該策略**
   並在系統日誌標 🚨。停用是刻意的：在人確認券商端狀態之前，這個策略的
   持倉認知已經不可信，讓它繼續跑只會把錯誤放大。

訊息也明講該做什麼：「無法確認券商是否已收到這張單，系統不會自動重送，
請立刻到券商端確認」。

### 其他幾個非顯而易見的決定

**stdout 只准有協定 JSON。** `kgisuperpy` 內部會直接 `print`（例如
`print('輸入賬號錯誤')`）。不隔開的話，第一次登入就會讓主程式收到一行不是
JSON 的東西，協定當場錯位。所以 worker 一啟動就把 `sys.stdout` 換成
`sys.stderr`，真正的 stdout 只留給協定。有測試守著這一條。

**回應要比對 id。** 上一個請求逾時、它的回應晚到時，沒有比對就會把舊回應
當成新請求的答案 —— 在下單這條路上等於「拿 A 單的結果判斷 B 單成功與否」。

**壞行拋例外，不安靜略過。** 安靜略過會變成「主程式永遠在等一個不會來的
回應」。

**worker 重用 `brokers/kgi.py` 的 `KGIBroker`，不重寫委託轉換。** 委託意圖
→ 凱基參數的對照表只能有一份；若在 worker 裡另抄一份，ADR-111 那 19 個
測試就測不到真正跑在 production 的那份程式碼了。

**密碼有遮蔽層。** IPC 內容很適合寫進日誌排查問題，而登入請求裡有身分證
字號與密碼。`broker_ipc.redact()` 把密碼類欄位換成 `***`（帳號本身不遮，
否則查不出是誰）。少了這一層，除錯日誌就會變成外洩管道。

**用背景執行緒讀 stdout，不用 select。** Windows 的 `select` 不吃 pipe
handle，而使用者的主要環境是 Windows。

**關閉程式時要明確收掉子行程。** 它是獨立行程，不會隨 `os._exit(0)` 一起
死；留著會變成孤兒行程，還握著券商 session，下次啟動可能因此登不進去。
已加進 `on_app_close` 的收尾序列（背景執行緒 + 限時 3 秒，不拖住關閉）。

**管線要關。** 主程式長時間執行，每次重連漏掉幾個 fd，跑一整天就會累積成
「開不了新行程」。（這是實測 `ResourceWarning` 抓到的。）

### 追記（2026-07-28）：對 2.1.0 重新確認，阻擋點沒有解除

`kgisuperpy` 2.1.0（當天發布）仍然只編到 cp313、仍相依 numba、METADATA 仍
沒有 `Requires-Python`。也就是說 **`pip install kgisuperpy` 在 Python 3.14
會「安裝成功」但 import 就炸** —— pip 不會擋，錯誤要到執行時才出現。
本 ADR 的子行程方案因此仍然必要。詳細比對見 `DECISIONS_ADR111.md` 的追記。

### 直譯器路徑

`app_settings.json` 新增 `kgi_python`（空字串 = 自動偵測）。自動偵測依序找
`python3.13` → `python3.12` → `python3.11`，**刻意不退回 `sys.executable`**：
主程式若是 3.14，退回去只會得到一個 import 就失敗的子行程，錯誤訊息還更難懂，
不如明確地說「找不到」並告訴使用者去哪裡設定。

### 涉及檔案

| 檔案 | 內容 |
|---|---|
| `core/broker_ipc.py`（新） | 協定：編解碼、id 比對、冪等分類、密碼遮蔽、失聯訊息 |
| `brokers/kgi_worker.py`（新） | 子行程主程式（**用 3.13 執行**）；stdout 隔離；重用 KGIBroker |
| `brokers/kgi_proxy.py`（新） | 主程式側的 BrokerClient；行程生命週期、逾時、不重送保證 |
| `stock_app_pro.py` | 註冊 proxy、`OrderOutcomeUnknown` 時停用策略、關閉時收子行程 |
| `data/config_store.py` | `kgi_python` 設定 |
| `tests/test_brokers.py` | 19 → **42** 個測試 |

### 已驗證（headless）

- `tests/test_brokers.py` **42 個全過**，其中包含：
  - **真的開子行程**跑完整鏈路（Popen → 寫 stdin → 讀 stdout → 解析）。
    這台環境有 `/usr/bin/python3.13`，所以這幾個測試是真的跑起來的，不是 skip。
  - 假 proc 模擬失聯／EOF／回應 id 錯亂（真行程不好重現的路徑）
  - worker 端到端：壞資料不會殺死 worker、未知操作回錯誤而非崩潰、
    多個請求的 id 一一對應、stdout 沒有非協定內容
- **突變測試**：把「非冪等不重送」的判斷改掉 → 測試 FAIL（確認斷言不是空的）
- `ResourceWarning` 歸零（fd 沒有外洩）
- `tests/test_core.py` 505 全過、`diag_repro_issues.py` 38 案例全過、
  `diag_crossref.py` 乾淨

### 明確**沒有**驗證的部分

- 真實的凱基登入、憑證、下單與回報（這台環境沒有 kgisuperpy 也沒有帳號）
- Windows 上的行程與管線行為（測試跑在 Linux）
- 長時間執行後子行程的穩定性（例如凱基端 session 逾時後 worker 的反應）
- ADR-111 提過的期貨代碼格式（`TXFG5` vs `TXFR1`）仍未確認

### 需使用者實機驗證

**環境**
1. 裝一個 Python 3.13，`/path/to/python3.13 -m pip install kgisuperpy`。
2. 在 `app_settings.json` 設 `kgi_python` 為那個直譯器的完整路徑
   （或確認自動偵測找得到）。
3. 手動先跑一次：`/path/to/python3.13 -m brokers.kgi_worker`，貼一行
   `{"v":1,"id":1,"op":"ping","args":{}}` 進去，應該回一行含 `"pong": true`
   與 3.13 版號的 JSON。**這一步能單獨確認環境沒問題**，不必先動主程式。

**功能**
4. 啟動主程式 → 登入凱基 → 開策略編輯器，確認「實單帳戶」下拉出現凱基帳號
   且標示（證券）／（期貨）。
5. 用**模擬**策略指定凱基帳號跑一段，確認不會誤送真單。
6. 改實單、**零股 1 股**送一張真單，確認委託有到凱基。
7. 關閉主程式後用工作管理員確認**沒有殘留的 python 子行程**。

**失聯行為（重要，但請在收盤後做）**
8. 下單前先用工作管理員把子行程強制結束，再讓策略觸發一次委託。
   應該看到系統日誌出現 🚨「結果不明」，而且**那個策略被自動停用**。
   確認它不會在下一根 K 棒又送一次。
