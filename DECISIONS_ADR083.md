
## ADR-083：修正「切換標的後K棒被異常放大」與分段下載的 NameError

### 背景

使用者切換標的後回報兩個問題,並附兩張截圖:
1. 主圖只剩 6 根巨大K棒撐滿整個畫面 (2330台積電 日K)。
2. TXFR1 期貨日K 圖表更破碎,且系統日誌出現
   `【背景補全】完整歷史下載失敗 (NameError: name 'last' is not defined)`。

---

### 問題一:切換標的後K棒被異常放大 (ADR-068~080 引入的迴歸)

**根因**:ADR-068 起,日/周/月K 也加上「快速段先出圖、完整段背景補全」的
兩段式設計。快速段抓的是 `QUICK_DAYS` 極小範圍 (日K 只有 7 天 ≈ 5~7 根)。
`draw_chart()` 在 `saved_xlim` 為 `None` 時的預設視角邏輯是:

```python
if tot > n_c: x_min, x_max = tot - n_c, tot   # 資料夠多,顯示最近 n_c 根
else:         x_min, x_max = 0, max(1, tot)   # 資料太少,全部顯示 (寬度=tot)
```

快速段的 `tot` (5~7) 遠小於 `n_c` (日K=120),於是走到 `else` 分支,視角寬度
被鎖在個位數根。背景完整段資料量變大後 (SJ_DAYS 範圍或 ADR-080 的深歷史),
`update_ui()` 原本的邏輯是「平移相同根數、保持視角不動」:

```python
added = len(pub_df) - prev_len
x0, x1 = self.axlist[0].get_xlim()
self.saved_xlim = (x0 + added, x1 + added)   # 只平移位置,寬度完全不變!
```

這段邏輯的原始設計目的是「期交所/yahoo 歷史往前延伸時,使用者正在看的那幾根
K棒位置要跟著往右移,肉眼看起來畫面不動」——這個假設在「快速段視角本來就是
正常寬度 (如分K的快速段 tot > n_c)」時成立,但當快速段視角是**因為資料太少
被迫全螢幕塞滿的窄視角**時,平移不會「修好」寬度,只會把這個過窄的視角原封
不動地搬到新位置——這正是使用者看到「只剩 6 根巨大K棒」的原因。

**修正**:新增 `self._view_is_auto_default` (True=目前視角是系統算出的預設值,
使用者還沒手動調過;False=使用者已手動縮放/平移過)。
- 每次新查詢 (換標的/換週期) 一律重置為 `True` (與既有的 `saved_xlim=None`
  重置同步)。
- 使用者真的動手縮放 (`on_scroll_zoom`) 或平移 (`on_mouse_move`) 才設為 `False`。
- 背景完整段交接時 (`update_ui` 的 `elif prev_len is not None` 分支):
  - `_view_is_auto_default=True` (使用者還沒碰過) → 直接把 `saved_xlim` 設回
    `None`,讓 `draw_chart` 用完整段的**實際根數**重新跑一次預設視角邏輯,
    寬度會正確算成 `n_c` (或全部,如果還是不夠)。
  - `_view_is_auto_default=False` (使用者已手動調整過) → 維持原本的平移邏輯,
    尊重使用者自己選的位置,不會被系統重算蓋掉。

這個修正對所有標的都適用 (使用者要求「所有標的都是這樣的規定」),因為問題出
在 `draw_chart`/`update_ui` 這條共用路徑,不是特定商品的邏輯。

---

### 問題二:分段下載的 NameError (既有 bug,與本次任何效能改動無關)

**根因**:`_download_kbars_chunked` 內的 `_try_seg()` 巢狀函式定義了區域變數
`last` 記錄最近一次的下載例外;但外層迴圈 (subsplit 判斷那段) 也寫了
`if last is not None and "ServerError..." in str(last):`——**這個 `last` 是
外層作用域看不到的巢狀函式區域變數**,Python 不會把它當成「還沒賦值就用」
以外的任何魔法,外層的 `last` 根本沒有定義過,一旦某個區段整段失敗 (重試
用完) 且區間長度 > `subsplit_days`,就會直接噴 `NameError: name 'last' is
not defined`,把真正的下載失敗原因蓋掉,使用者只看到這個誤導的錯誤訊息、
完全不知道實際是什麼問題導致抓不到資料。

這是**既有 bug**,推測從 ADR-046/047/048 引入這段分段下載邏輯以來就存在,
只是要「某區段整段失敗且區間 > subsplit_days」這個條件同時成立才會觸發,
過去比較少踩到,這次剛好在 TXFR1 期貨日K 的下載中觸發。

**修正**:改用一個外層也看得到的容器 `last_err = {'e': None}`,`_try_seg`
內把最近一次例外寫進 `last_err['e']`,外層改讀 `last_err['e']`,徹底解決
作用域問題,使用者之後看到的會是真正的下載失敗原因,不再被 NameError 蓋掉。

### 相關程式位置 (stock_app_pro.py)

- 問題一:`__init__` 新增 `_view_is_auto_default`;`start_fetch_thread` 同步
  重置;`on_mouse_move`/`on_scroll_zoom` 使用者操作時設 False;`_publish` 內
  `update_ui` 的 `elif prev_len is not None` 分支改用該旗標決定重算或平移。
- 問題二:`_download_kbars_chunked` 內 `_try_seg`/外層迴圈,`last` 改
  `last_err` 容器。

### 需使用者實機驗證

1. 連續切換多檔不同標的的日K/周K/月K:圖表應顯示正常寬度的K棒 (約 120 根
   左右,不會整張圖只剩幾根巨大K棒)。
2. 切換後手動縮放/平移一下,再等背景完整段補完:應該維持你調整的位置 (不會
   被重設)。
3. TXFR1 或其他期貨標的的日K,若之前會出現 NameError,現在應該改成看到真正
   的下載失敗原因 (若確實失敗) 或正常補齊歷史。
