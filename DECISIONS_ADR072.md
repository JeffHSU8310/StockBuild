
## ADR-072：盤中零股開盤時刻可設定 (預設 09:10) + 一般 App 設定持久化

### 背景 / 需求

使用者:「零股目前交易時間是 09:10 (但之後會改成 09:00),關於零股交易開盤時間,
要可以選擇,但預設先設定成 09:10。」

### 做法

1. **`core/market_session.py`**:把「盤中零股」從「整股」拆出來獨立判斷。
   - 新增模組層級可變設定 `ODD_LOT_OPEN_MIN`,預設 `09:10`;
     `set_odd_lot_open_minute()` / `set_odd_lot_open_hhmm()` 供 GUI 覆寫。
   - 新增 `is_odd_lot_open(dt, open_minute=None)`:週一~週五、開盤時刻 ~ 13:30;
     `open_minute` 顯式帶入時優先 (單元測試固定邊界用)。
   - `is_market_open('零股')` 與 `session_label('零股')` 改走零股判斷 (整股維持
     09:00);未來交易所改 09:00 時,使用者自己在設定切一下即可,不動程式。
   - 整股 09:00 開、零股 09:10 才開的差異有測試守住 (共 254 測試通過)。

2. **`data/config_store.py`**:新增通用 App 設定持久化
   `load_app_settings/save_app_settings` (`app_settings.json`),鍵值含
   `odd_lot_open` / `auto_reconnect` / `remember_creds`;讀不到/壞掉一律回預設。

3. **`stock_app_pro.py`**:
   - 啟動載入 `app_settings`,把 `odd_lot_open` 套進 `market_session`。
   - 頂部大盤列新增「零股開盤 09:10 / 09:00」下拉,切換即時套用並存檔
     (`_on_odd_open_changed`);「斷線自動重連」開關的初始值也改由設定還原、
     切換時存回 (`_save_app_settings`)。

### 相容

- 預設 09:10 = 現制,既有行為不變;整股不受影響。
- 設定檔壞掉/不存在都回預設,不會因此當掉。

### 需使用者實機驗證

1. 頂部「零股開盤」預設顯示 09:10;09:00~09:10 之間,零股策略應仍待命 (未開盤),
   09:10 才開始評估。
2. 切成 09:00 後重開程式,設定應被記住 (09:00 生效)。
