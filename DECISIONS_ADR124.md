# ADR-124:即時停損要看交易時段;終極波段結構上只做日盤

## 狀態

已實作,**尚未實機驗證**(分支 `claude/session-gate-realtime-stops`)。

## 背景:使用者的模擬帳戶截圖把時間軸講清楚了

| 時間 | 動作 | 價格 | 已實現 |
|---|---|---|---|
| 2026-07-29 **12:01:05** | 做空 賣出 **開倉** 5 口 TXF | 39720 | — |
| 2026-07-29 **15:00:01** | 做空 買進 **平倉** 5 口 TXF | 40688 | **-968,250** |

開倉在 12:01:05 —— 那是終極波段設計中的「12:00 確認、12:01 執行」,**正確**。
平倉在 **15:00:01** —— 那是**夜盤開盤的第一秒**。使用者:

> 「終極波段不會在夜盤做任何動作的」

### 先確認 ADR-123 有沒有修掉這個症狀:有

用截圖的原始數字實跑 ADR-123 之後的程式:

```
虧損%: 2.44                                       ← 與截圖的 -2.44% 完全吻合
終極波段 check_intrabar_futures_stop → None       ← 不再被砍
一般期貨策略 → 即時停損出場 (損益 -2.44% ≤ -2.0%)   ← 反向對照仍正常
```

(−968 點 × 5 口 × 200 + 250 手續費 = −968,250,與截圖分毫不差。)

**那一刀在 ADR-123 之後不會再發生。** 但使用者要的是「再詳細檢查一次」,
而查下去發現同一個家族還有兩個洞。

---

## 洞一:即時停損路徑完全沒有交易時段閘門

`_qt_check_realtime_futures_stops()` 從頭到尾只有 5 個 `continue`:
沒啟用 / 不是期貨 / 沒有即時價 / 沒有部位 / intent 是 None。
**沒有 `is_market_open`、沒有 `session_gate`、沒有 `futures_session`。**
而它掛在 `_qt_update_realtime_pnl()` 底下,由 runner **每 3 秒**呼叫一次。

三條會下單的路徑裡,只有這一條沒有閘門:

| 路徑 | 時段閘門(修正前)|
|---|---|
| `_quant_eval_pass` | ✅ 有 |
| `_qt_check_realtime_futures_stops` | ❌ **沒有** |
| `_qt_chukuangren_execute_pass` | ⚠️ 沒有,但被 `armed_intent` + 10 分鐘時效鎖在 12:01 前後 |

這正是 15:00:01 那一刀的**發生機制**:日盤 13:45 收盤後價格不再更新,
夜盤 15:00:00 第一筆真實報價一進來,3 秒輪詢立刻拿它去比停損。

**這個洞在 ADR-123 之後對一般期貨策略仍然是活的**:使用者把某檔策略明確設成
**只做日盤**,它照樣會在夜盤被平倉 —— 直接違反使用者的設定。

## 洞二:終極波段預設 `futures_session='day_night'`

`chukuangren_band.default_strategy()` 疊在 `new_strategy()` 上,繼承了
`day_night`,所以夜盤時段閘門是放行的,策略照樣在評估。

但這檔策略的設計從頭到尾都是日盤的事:看A 是**加權指數日K**(指數沒有夜盤)、
12:00 二次確認、12:01 執行。**夜盤對它沒有任何意義** —— 使用者說的那句話
不是偏好,是策略的結構事實。

---

## 決定

### A. `include_night_of()` 收斂成 core 的單一判斷

```python
# core/strategy_engine.py
DAY_SESSION_ONLY_KINDS = frozenset({'chukuangren_band'})

def include_night_of(strategy):
    """結構上只做日盤的種類一律 False;其餘照 futures_session。"""
```

覆寫存檔值,所以**既有策略不必遷移就正確**(同 `OWN_EXIT_KINDS` 的哲學)。
一樣用字串避免循環 import,靠單元測試把它跟 `chukuangren_band.KIND` 釘住。

`_quant_eval_pass`、`_qt_check_realtime_futures_stops`、
`_qt_chukuangren_execute_pass` **三處共用這一個函式** —— 原本評估迴圈自己算
一次、即時停損根本沒算,這就是根因。

**刻意不把 `OWN_EXIT_KINDS` 與 `DAY_SESSION_ONLY_KINDS` 合併成一份清單**:
兩者目前內容相同,但語意不同(一個講出場邏輯、一個講交易時段)。日後多一個
「自己管出場但要做夜盤」的策略種類時,合併過的寫法會一起把夜盤關掉。
有一條單元測試守著這個區分。

### B/C. 即時停損與延遲下單補上同樣的閘門

```python
if s.get('session_gate', True) and not market_session.is_market_open(
        strategy_engine.trade_type_of(s),
        include_night=strategy_engine.include_night_of(s)):
    continue
```

- 尊重 `session_gate=False`:使用者說「不管時間都要跑」,即時停損也照跑。
- **不會削弱 ADR-087 的原意**:那是要解決「盤中帳面已經虧超過停損點卻沒動作」,
  而這個閘門只在「市場關閉 / 使用者關掉該盤別」時擋 —— 那種時候本來就不可能
  成交。
- `_qt_chukuangren_execute_pass` 原本靠時效**間接**安全,補上閘門讓
  「終極波段不在夜盤動作」變成結構保證而不是巧合(12:01 在日盤內,行為不變)。

---

## 突變測試抓到一個空殼斷言(值得記)

第一版跑突變測試,「`DAY_SESSION_ONLY_KINDS` 清空」這一項**診斷是綠的**。
查下去有兩個原因,第二個是真問題:

1. 終極波段其實有**兩層**保護:ADR-123 的 `OWN_EXIT_KINDS`(即時停損不適用)
   與 ADR-124 的時段閘門。只拿掉第二層,第一層還擋著 —— 這是預期中的
   defence in depth,不是問題。
2. **但「終極波段在夜盤不該評估」那條斷言是空殼**:終極波段分支會先呼叫
   `_qt_resolve_watch()` 解析看A(加權指數),而診斷沒有 stub 它 ——
   於是在那裡就拋例外,`_download_kbars_raw` 根本沒機會被呼叫。
   斷言「沒抓K線」因此**永遠成立**,跟閘門有沒有生效完全無關。

修法:stub `_qt_resolve_watch`,並補一條**正控** —— 把時段打開後,同一檔
策略**必須真的去抓K線**。補完再跑同一個突變,就紅了
(`終極波段在夜盤不該評估 (實際抓了 13 次K線)`)。

> 教訓同 P-28:**斷言「什麼都沒發生」時,一定要配一條「條件放開後真的會
> 發生」的正控**,否則測到的可能是「因為別的原因而沒發生」。

## 驗證

- `python tests/test_core.py` → **602 個全過**(原 597,新增 5)
- `python tests/test_brokers.py` → 42 個全過
- `python diag_repro_issues.py` → **50 案例全過,0 FAIL**
- `python diag_crossref.py` → 乾淨

### 突變測試

| 把程式改成 | 結果 |
|---|---|
| `DAY_SESSION_ONLY_KINDS` 清空 | 單元 + 診斷都紅(**補正控後才紅**,見上)|
| `include_night_of` 永遠回 False | 單元 + 診斷都紅(夜盤停損被誤關)|
| `include_night_of` 永遠回 True | 單元測試紅 |
| 即時停損不加時段閘門 | 診斷紅 |
| 閘門忽略 `session_gate=False` | 診斷紅 |

診斷的時鐘一律用 patch 過的 `is_market_open` 控制,**不依賴真實時間**(P-94)。

## 需使用者實機驗證

1. **終極波段在 15:00 之後完全安靜**:不評估、不下單、不出現任何平倉。
2. **一般期貨策略設成「只做日盤」→ 夜盤不會被停損**;設成「日盤+夜盤」的
   **夜盤仍會停損**。兩個方向都要看,不能只驗一邊。
3. **日盤時段的即時停損仍然正常運作**(ADR-087 的功能沒有被弄壞)。

## 不在這次範圍

- 不動 `paper_account.json`(使用者已決定保留紀錄)。
- 兩檔同名策略在日誌上分不出來(ADR-123 已記,仍不動)。
