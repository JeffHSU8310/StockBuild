# ADR-123:終極波段策略不受泛用停損影響 + 開盤暖機不被時段閘門關掉

## 狀態

已實作,**尚未實機驗證**(分支 `claude/chukuangren-own-exits`)。

## 背景:使用者 15:00 的實測回報

```
【自動交易-模擬】🧪 策略「楚狂人之終極波段」買進 5 TXF @ 40688 [即時]
| 即時停損出場 (損益 -2.44% ≤ -2.0%) → 已記入模擬帳戶,此筆已實現 -968,250
```

> 「此時不應該出現即時停損的情況」

同一則訊息**出現兩次**,以及 15:00 夜盤又噴一次
`ShioajiTimeoutError: api/v1/data/kbars`。

---

## 問題一:終極波段被一個看不見的 2% 停損砍倉

### 根因

`chukuangren_band.default_strategy()` 疊在 `strategy_engine.new_strategy()`
上面,而後者的預設值是:

```python
'stop_loss_pct': 2.0,      # core/strategy_engine.py new_strategy()
```

`default_strategy()` 從來沒有把它清掉,而終極波段的**專屬編輯器根本沒有
這個欄位** —— 使用者從頭到尾看不到、也沒設定過。截圖裡的 `-2.44% ≤ -2.0%`
就是這個看不見的預設值。

而 `_qt_check_realtime_futures_stops()` 只過濾「有沒有啟用」與「是不是
期貨」,**完全不看 `kind`**,於是把它套用到了終極波段上。

**這不是「多一則通知」,是策略的風控被整個換掉**:終極波段的出場是
X/C/F/Y/Z 那套(以加權指數點位為準、還要隔天 12:00 二次確認),被一個
intrabar 的 2% 停損搶先執行。

> K 棒收盤的那條泛用停損**沒有**這個問題:`_quant_eval_pass` 對
> `kind == chukuangren_band.KIND` 走獨立分支,不會呼叫 `evaluate_strategy`。
> **只有即時停損這條漏了** —— 它是後來 ADR-087 為了解決「帳面已經虧超過
> 停損點系統卻沒動作」而新增的**獨立通道**,加的時候沒有考慮到
> 「有些策略自己管出場」。

### 決定

核心防線放在 `core/strategy_engine.check_intrabar_futures_stop()`(純函式、
可離線測試),不是放在 GUI 的迴圈裡 —— 這樣**任何呼叫端都自動正確**:

```python
OWN_EXIT_KINDS = frozenset({'chukuangren_band'})

def has_own_exit_logic(strategy): ...
```

用**字串**而不是 `from . import chukuangren_band`:那個模組反過來 import
`strategy_engine`,會變成循環 import。兩邊靠 `tests/test_core.py` 的一條
斷言釘在一起(`KIND in OWN_EXIT_KINDS`),改名了不會無聲脫鉤。

另外兩項讓**資料本身**也乾淨:

- `chukuangren_band.default_strategy()` 把四個泛用停損停利欄位歸零
- 終極波段編輯器的 `_collect()` 同樣歸零 —— 舊策略**下次儲存時**就洗乾淨

**既有存檔不做自動遷移**:核心防線已經讓那個 2.0 完全不生效,而動使用者的
策略檔是不可逆的,沒必要為了美觀去改。

### 反向對照是這次測試的重點

只驗「終極波段不會被砍」是不夠的 —— 把守門寫成「所有 kind 都擋」也會一片
綠,但那等於把整個即時停損功能關掉,對一般期貨策略是**更嚴重**的問題
(ADR-087 當初就是為了補這個缺口)。所以單元測試與診斷都各有一條
**一般期貨策略在同樣條件下仍然要出場**的斷言。突變測試證實:把守門改成
`return True`,就是這條先紅。

---

## 問題二:開盤暖機被 `session_gate` 關掉

### 先講清楚:15:00 本來就在涵蓋範圍內

```
just_opened('期貨', 15:00:00) → True      (FUT_NIGHT_OPEN_MIN = 15*60)
just_opened('期貨', 15:00:29) → True
just_opened('期貨', 15:00:31) → False
```

ADR-121 的 `just_opened()` 一開始就處理了夜盤 15:00。所以使用者在 15:00
還會看到錯誤,只有兩種可能:

1. **程式還沒重開** —— ADR-121/122 是當天稍早才併進 main 的,App 從早上跑到
   現在用的是舊碼。這個不用改程式。
2. **那檔策略把時段閘門關掉了(`session_gate=False`)** —— 這是**真的漏洞**。

### 根因

暖機檢查被**巢狀在 `session_gate` 的 if 裡面**:

```python
if (not _forced) and s.get('session_gate', True):
    ...
    if market_session.just_opened(...):     # ← session_gate=False 就走不到
        continue
```

但暖機要解決的是「**別在開盤鐘響那一秒去打券商 API**」,那跟使用者想不想要
時段閘門是**兩件事**。而且 `session_gate=False` 的策略是 24 小時都在評估的,
**撞上開盤瞬間的機率反而更高**,結果卻完全沒有保護 —— 保護的覆蓋範圍跟
需要保護的程度剛好相反。

### 決定

把 `tt` / `include_night` 的計算與暖機檢查移出 `session_gate` 區塊。
行為差異只有一處:`session_gate=False` 的策略在開盤後 30 秒內會等一下;
休市時 `just_opened` 本來就回 False,其餘時間完全不變。

---

## 同一則訊息出現兩次:兩檔同名策略

查過程式:`_qt_check_realtime_futures_stops` 只有一個呼叫點,
`_qt_runtime()` 回傳同一個 dict,`apply_fill` 之後 `state` 變 FLAT,
**同一檔策略不可能連續觸發兩次**;Telegram 那段也沒有重試。

**使用者確認:策略清單裡有兩檔同名的終極波段策略**,各自持有 5 口、各自被
砍。所以沒有第三個 bug,本次修正同時解掉兩邊。

順帶記一筆(**不在這次動**):日誌與 Telegram 的 label 只用策略名稱,兩檔
同名策略在訊息上完全分不出來,排查時會誤導。若日後要改,做法是名稱重複時
附上 `id` 前幾碼 —— 這次不動,因為它會改到每一行日誌的格式。

使用者選擇**不修正模擬帳戶**那兩筆誤觸發的平倉,保留原樣當作痕跡。
本次不碰 `paper_account.json`。

---

## 驗證

- `python tests/test_core.py` → **597 個全過**(原 591,新增 6)
- `python tests/test_brokers.py` → 42 個全過
- `python diag_repro_issues.py` → **49 案例全過,0 FAIL**
- `python diag_crossref.py` → 乾淨

### 突變測試(每項都確認「改壞就會紅」)

| 把程式改成 | 結果 |
|---|---|
| `OWN_EXIT_KINDS` 清空(等於沒修)| 單元 + 診斷都紅 |
| 守門改成「所有 kind 都擋」 | 單元 + 診斷都紅(反向對照先紅)|
| `check_intrabar_futures_stop` 不呼叫守門 | 單元 + 診斷都紅 |
| `default_strategy()` 不歸零 | 單元測試紅 |
| 暖機搬回 `session_gate` 裡面 | 診斷紅 |

**診斷案例刻意走完整的 GUI 路徑**(`_qt_check_realtime_futures_stops`),
不是只測純函式 —— 純函式測不到「呼叫端有沒有真的用到守門」那一層
(P-64 的教訓)。

---

## 需使用者實機驗證

1. **終極波段策略**:持有部位時價格上下震盪,**不可以再被砍倉**;
   進出場只能由 X/C/F/Y/Z 那套決定。
2. 開啟終極波段編輯器**按一次儲存**,兩檔同名策略的殘留 `stop_loss_pct`
   就會被洗成 0(不洗也不會生效,只是資料乾淨)。
3. **明天 08:45 / 09:00 / 15:00** 三個開盤時刻都不再出現 kbars 逾時 ——
   **記得先把程式重開**,ADR-121/122/123 才會生效。
4. 把「交易時段閘門」關掉的策略,在開盤瞬間也應看到暖機那一行日誌。

## 不在這次範圍

- `new_strategy()` 的其他預設值(`max_trades_per_day=3`、`cooldown_sec=300`)
  同樣會套用到終極波段,理論上也可能擋掉合法訊號。但使用者沒回報,而且
  它們不像停損那樣會**主動平倉**(只是不進場),風險性質不同。先記著不改。
- 兩檔同名策略在日誌上分不出來(見上)。
