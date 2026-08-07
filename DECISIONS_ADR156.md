# ADR-156:診斷偶發紅的根因(背景 worker)+ 執行期狀態檔移出版控

- **狀態**:已實作,離線驗證通過,**未經使用者實機驗證**
- **日期**:2026-08-07
- **關聯**:P-138(新增)、P-94、ADR-100
- **使用者指示**:「1、要不要我把這個偶發紅當成獨立一筆來查?**這件事情要查清楚。**
  2、**移出版控。**」

---

# 第一部分:診斷偶發紅的根因

## 1. 症狀

`python diag_repro_issues.py` 整份偶發紅,8 次跑出 2 次,而且**每次紅的案例
不一樣**:

| 次 | 紅的案例 | 訊息 |
|---|---|---|
| A | ADR-133 | 同一格不該重抓,實際又抓了 `['日K', '60分K', '60分K']` |
| B | ADR-145 | 還沒進場時也要監看 A 的即時價,實際問了 `['TXFR1', '001']` |

B 的內容是決定性的線索:ADR-145 那個案例掛上去的策略只有 `2330` / `^TWII`,
`TXFR1`(主圖當時的期貨合約)和 `001`(加權指數)**根本不屬於它**。

## 2. 根因

`StockTradingAppPro.__init__` 會起 **6 條** daemon thread,而診斷全程共用
**同一個 app 物件** —— 這 6 條在後面每一個案例執行期間都還活著:

| worker | 它會做什麼 | 對得上哪個症狀 |
|---|---|---|
| `fetch_realtime_worker` | 每輪 `snapshots([主圖當前合約])` | B 的 `TXFR1` |
| `fetch_market_indices_worker` | 每輪 `snapshots([加權, 櫃買])` | B 的 `001` |
| `chart_auto_refresh_worker` | 自動重抓主圖 K 線 | A 的「同一格重抓」 |
| `watchlist_quote_worker` | 每 10 秒批次 `snapshots(自選股)` | |
| `quant_runner_worker` | 每 2 秒跑策略評估/停損/定時下單 | |
| `regime_daily_notify_worker` | 盤勢判斷每日推播 | |

案例的做法是「臨時把 `app.sj_api` 換成假的、記下它被問了什麼、然後斷言」。
背景 worker 拿的是**同一個** `app.sj_api`,於是它們也會去呼叫那個假 API,
把案例記錄用的變數蓋掉。案例用「最後一次呼叫」當依據時,就是在跟背景執行緒
搶寫同一個變數。

**這個坑診斷自己早就寫過**。ADR-100 的案例裡有這一段:

> 「`fetch_realtime_worker` 是背景 daemon thread,會不定時自己 `snapshots(1檔)`,
> 用「最後一筆」斷言會隨執行緒時序時而 PASS 時而 FAIL(實測 [1,2,1])。」

但當時只在**那一個案例**局部繞過(改成「只看這次呼叫新增的批次」)。
繞過一個,剩下的每一個案例都還踩得到 —— 而且新寫的案例完全不知道有這回事。

## 3. 決定

診斷期間**不啟動**這 6 條 worker。做法是在 `StockTradingAppPro()` 建構的那一
瞬間把 `stock_app_pro.threading.Thread` 換成 `_DiagNoWorkerThread`,對名單內的
target 讓 `.start()` 變成 no-op,建構完立刻換回去。

**產品程式碼一行都不用改** —— 不加測試專用的旗標,不在 worker 裡塞
`if diag: return`。

被擋掉的只有「無窮迴圈的 worker」。案例要驗某一輪的行為時一律直接呼叫對應的
單次函式(`_wl_fetch_quotes_once()`、`eval_pass()` …),完全不受影響。
也有案例是刻意在**主執行緒同步**呼叫 worker(把 `time.sleep` 換成跑 N 圈就
中斷)去驗它的節流間隔 —— 那是受控的、跑完就結束,一樣不受影響。

## 4. 守門員:新增一個診斷自我檢查案例

偶發紅最貴的地方是**它會訓練人去忽略紅燈**。所以攔截機制本身要有東西守著:

| 斷言 | 守什麼 |
|---|---|
| 1 | 整份跑完,不可以有任何 worker 在**背景執行緒**上跑過(主執行緒同步呼叫不算) |
| 2 | 攔截真的有發生:6 條每一條都被擋在 `start()` |
| 3 | 反向對照:攔截只針對 worker,一般執行緒不可以被一起擋掉(有案例自己開 5 條執行緒驗 kbars 鎖) |
| 4 | 原始碼層級:`__init__` 起了新 worker 時名單要跟著補;名單裡也不可以留 `__init__` 根本沒起的東西 |

斷言 4 的範圍**刻意只看 `__init__`**:全檔還有二十幾個 `_xxx_worker`,
但那些是一次性的(按鈕按下去才起、跑完就結束),不會活過整個診斷。

## 5. 開發過程中被自我檢查抓到的兩件事

這兩件都是我自己寫錯、被剛寫好的斷言當場抓出來的,值得記下來:

1. **我憑印象列了 5 條就以為列完了。** 斷言 4 立刻報出第 6 條
   `chart_auto_refresh_worker` —— 而它正好就是症狀 A(「同一格不該重抓」)
   的元兇。少了自我檢查,我會宣稱修好了,然後 ADR-133 繼續偶發紅。
2. **斷言 1 的第一版下錯條件。** 第一版寫「一條都不可以執行過」,結果抓到
   `watchlist_quote_worker` —— 但那是某個案例在**主執行緒同步**呼叫它去驗
   節流間隔,是受控的、跑完就結束,不會外洩。改成「只看背景執行緒」才對。

另外還踩到兩個工具層面的坑:

- 我第一版把新案例**加在印出結果表格的程式碼之後**,所以它跑了但不會出現在
  報表裡 —— 案例數還是 75 不是 76。當時的「6 次全綠」其實根本沒驗到它。
- 包裝 worker 方法去記錄「有沒有跑過」時,`inspect.getsource()` 會讀到我的
  包裝函式。有案例是用 `getsource` 讀 worker 內容去驗「production 有沒有真的
  呼叫某個函式」(P-64),當場紅了。用 `functools.wraps` 設 `__wrapped__`,
  `inspect` 會自己 unwrap 回原函式。

## 6. 驗證 —— 為什麼「跑很多次都綠」不算數

依 P-94,我**不用**「連跑 N 次都沒紅」當證明。真正的證據是**機制**:

| 攔截 | 實際在背景執行緒上跑起來的 worker |
|---|---|
| 打開 | **0 條** |
| 關閉(突變) | **6 條全部**:`chart_auto_refresh_worker`、`fetch_market_indices_worker`、`fetch_realtime_worker`、`quant_runner_worker`、`regime_daily_notify_worker`、`watchlist_quote_worker` |

這是確定性的,不是機率性的。再加上症狀對得上(`TXFR1` 只可能來自
`fetch_realtime_worker`、`001` 只可能來自 `fetch_market_indices_worker`、
「同一格重抓」只可能來自 `chart_auto_refresh_worker`),歸因是站得住的。

**誠實的邊界**:我證明的是「那 6 條 worker 確實會在案例執行期間動作、
而且它們動作的內容正好就是紅訊息裡出現的東西」,以及「現在它們一條都不會跑」。
我**沒有**證明「診斷從此不可能再有任何偶發紅」—— 如果還有別的共用狀態問題,
它會另外冒出來。攔截打開後連跑 4 次 76/76 全綠,那是佐證,不是證明。

| 項目 | 結果 |
|---|---|
| `python tests/test_core.py` | 978 案 |
| `python tests/test_brokers.py` | 42 案 |
| `python diag_repro_issues.py` | **76** 案例(+1),連跑 4 次 0 FAIL / 0 ERROR |
| `python diag_crossref.py` | 乾淨 |
| `py_compile` | 全通過 |

---

# 第二部分:執行期狀態檔移出版控

## 7. 問題

`quant_strategies.json`(策略清單)、`quant_state.json`(部位/風控計數)、
`paper_account.json`(模擬帳戶)三個檔在版控裡。它們是**程式每次存檔都會改寫
的執行期資料**,後果:

- 使用者本機 `E:\StockBuild` **每次 `git pull` 都撞衝突** —— 實測就是被這三個
  檔擋下來的。
- 「A 機器的部位覆蓋 B 機器」的風險:合併衝突解錯邊,部位紀錄就錯了。

## 8. 決定

`git rm --cached` 三個檔 + 加進 `.gitignore`。

讀取端(`stock_app_pro.py` 的 `_qt_load()` / `_qt_load_paper()`)三處都有
`os.path.exists` 保護,檔案不存在時走預設值。實測把三個檔搬走後跑完整診斷:
**76 案例全綠,而且診斷不會自己生出這些檔**(P-65 的保護仍有效)。

代價:新機器 clone 下來**不會帶著策略檔**,要自己複製一份過去。這是刻意的
—— 策略是每台機器自己的資料,不是專案的一部分。

## 9. ⚠️ 使用者本機拉取前必須先備份

`git rm --cached` 之後,這是一個**刪除檔案的 commit**。在 `E:\StockBuild`
`git pull` 時,git 會把工作目錄裡那三個檔**一併刪掉**(或因為本機有修改而
拒絕合併)。所以順序不能錯:

```cmd
cd /d E:\StockBuild
rem 1) 先備份 (這一步不能省)
copy quant_strategies.json quant_strategies.json.bak
copy quant_state.json      quant_state.json.bak
copy paper_account.json    paper_account.json.bak

rem 2) 丟掉本機對這三個檔的版控修改,讓 pull 過得去
git checkout -- quant_strategies.json quant_state.json paper_account.json
git pull origin main

rem 3) 把備份放回來 (此時它們已經是 gitignore 的檔案,不會再擋 pull)
copy /y quant_strategies.json.bak quant_strategies.json
copy /y quant_state.json.bak      quant_state.json
copy /y paper_account.json.bak    paper_account.json
```

第 3 步做完之後,以後 `git pull` 不會再被這三個檔擋住,它們也不會再被推上去。

---

## 10. 請使用者實機驗證

1. **照第 9 節的順序**在 `E:\StockBuild` 拉取,拉完之後**打開程式確認策略還在**
   (策略清單、模擬帳戶餘額、持倉)。
2. 之後再 `git pull` 一次,確認**不會再被這三個檔擋住**。
3. 程式跑起來後改一下策略再關掉,確認 `git status` **看不到**這三個檔
   (它們已經被忽略了)。
