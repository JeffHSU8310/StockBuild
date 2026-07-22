
## ADR-073：記住憑證並開機自動登入 (加密存本機,可選) — 補足 ADR-071 的跨重開自動化

### 背景 / 需求

使用者:「需要把密碼加密存到本機,做成選項。」ADR-071 的自動重連是「記憶體
暫存憑證」,程式整個關掉重開後仍要手動登入一次。本 ADR 補上「開機自動登入」:
把憑證加密存本機,連重開機都不用人工。做成勾選選項,預設關閉。

### 加密設計 (純標準函式庫,不依賴第三方)

`core/secure_store.py`:encrypt-then-MAC 串流加密,只用 hashlib/hmac/secrets。
- PBKDF2-HMAC-SHA256(金鑰材料, salt, 200000) → enc_key/mac_key。
- keystream = HMAC-SHA256(enc_key, nonce‖counter) 逐塊 (HKDF-CTR 風格) 與明文 XOR。
- tag = HMAC-SHA256(mac_key, salt‖nonce‖ct);解密先常數時間比對防竄改。
- 刻意不用 `cryptography`/Fernet:那類套件在部分環境 (含本專案無畫面測試環境)
  裝不起來,登入路徑不能因缺套件就掛。純 stdlib 保證到哪都能跑、能離線測試。
- `tests/test_core.py` 新增 `TestSecureStore` 6 案 (往返/字典往返/金鑰不符/
  竄改偵測/空金鑰/密文每次不同),共 260 測試通過。

### 金鑰材料與誠實的安全邊界

`_device_key_material()` = hostname + 使用者名 + 一次性隨機裝置碼 (device_id.bin)。
- 效果:加密憑證檔複製到「別台機器」解不開 (裝置碼不同)。
- 限制 (誠實告知):要「免輸入自動登入」,金鑰就必須能無人取得,所以擋不了
  「能完整存取你這台電腦、這個使用者帳號的人」。這是所有自動登入的共同取捨。
  UI 與日誌都明講這點,讓使用者知情選用。

### 流程

1. 頂部新增勾選「🔐 記住憑證(自動登入)」(`remember_creds`,預設關,偏好持久化)。
2. 勾選 + 登入成功 → `_save_secure_creds` 把憑證加密寫 `broker_secure.json`。
   勾選當下若已登入,也立刻存一次;取消勾選 → 立即刪檔。
3. 開機:`_try_auto_login_on_start` (啟動後 1.5 秒) 若設定開著且解得出憑證,
   背景自動 `process_broker_login`,不用人工。
4. 解不開 (換過機器/檔案損毀) → 記日誌提示「需手動登入一次」,不會卡死。

### 與 ADR-071 的關係

- ADR-071 (記憶體 + 斷線自動重連):處理「程式運行期間的斷線」。
- ADR-073 (加密存檔 + 開機自動登入):處理「程式重開/重開機」。
- 兩者疊加 = 早上開一次、之後連斷線與重開都自動。手動登出仍優先 (清記憶體
  憑證、不自動重連;加密檔是否保留由勾選狀態決定)。

### 相關程式位置

- `core/secure_store.py` (新)、`tests/test_core.py` `TestSecureStore` (+6)。
- `stock_app_pro.py`:`_device_key_material`/`_save|_load|_delete_secure_creds`/
  `_on_remember_creds_toggle`/`_try_auto_login_on_start`;登入成功時存檔;
  頂部勾選框;`__init__` 末端排程開機自動登入。

### 需使用者實機驗證

1. 勾「🔐 記住憑證」→ 登入一次 → 關程式重開:應在啟動後自動登入,無需人工。
2. 取消勾選:`broker_secure.json` 應被刪除,重開需手動登入。
3. 把 `broker_secure.json` 複製到另一台電腦:應解不開 (需重新登入),驗證裝置綁定。
