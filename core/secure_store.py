"""
core.secure_store — 純標準函式庫的「加密存放」小工具 (ADR-073)。

用途:讓「記住券商憑證、開機自動登入」可以把憑證加密後存在本機,而不是明碼
落地。刻意只用 Python 標準函式庫 (hashlib/hmac/secrets/base64),不依賴
`cryptography` 之類第三方套件——因為那些套件在部分環境 (含本專案的無畫面
測試環境) 不一定裝得起來,而登入這條路一旦因為缺套件就掛掉,使用者連盤都看
不了。純 stdlib 保證到哪都能跑,也能離線單元測試。

【安全性誠實說明】
這是「加密儲存」,金鑰由「本機裝置種子」(device_key_material) 推導。要達成
「開機不用人工輸入就自動登入」,金鑰材料就必須能在無人輸入的情況下取得,
因此本質上是「綁定這台機器/這個帳號」的保護:能擋掉「檔案被複製到別台機器
就能解密」與「純文字被人一眼看到」,但擋不了「能完整存取你這台電腦、這個
使用者帳號的人」。這是所有「免輸入自動登入」的共同取捨,使用者需知情選用。

加密構造:encrypt-then-MAC 串流加密。
  * PBKDF2-HMAC-SHA256(金鑰材料, salt, 200000) → 64 bytes,拆成 enc_key/mac_key。
  * keystream 以 HMAC-SHA256(enc_key, nonce || counter區塊) 逐塊產生 (HKDF-CTR
    風格,以 HMAC 當 PRF),與明文 XOR。
  * tag = HMAC-SHA256(mac_key, salt || nonce || ciphertext);解密先做常數時間
    比對,驗不過就拋例外 (防竄改)。
輸出/輸入為 base64 字串,方便存進 JSON。
"""
import base64
import hashlib
import hmac
import json
import secrets

_PBKDF2_ITERS = 200_000
_SALT_LEN = 16
_NONCE_LEN = 16
_TAG_LEN = 32
_MAGIC = b'SS1'  # 版本標記,日後換構造可辨識


def _derive_keys(key_material: bytes, salt: bytes):
    dk = hashlib.pbkdf2_hmac('sha256', key_material, salt, _PBKDF2_ITERS, dklen=64)
    return dk[:32], dk[32:]  # enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, nbytes: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < nbytes:
        block = hmac.new(enc_key, nonce + counter.to_bytes(8, 'big'), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:nbytes])


def encrypt(plaintext: str, key_material: bytes) -> str:
    """把字串加密成 base64 blob。key_material 為裝置金鑰材料 (bytes)。"""
    if not isinstance(key_material, (bytes, bytearray)) or not key_material:
        raise ValueError("key_material 不可為空")
    data = plaintext.encode('utf-8')
    salt = secrets.token_bytes(_SALT_LEN)
    nonce = secrets.token_bytes(_NONCE_LEN)
    enc_key, mac_key = _derive_keys(bytes(key_material), salt)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(_MAGIC + salt + nonce + ct + tag).decode('ascii')


def decrypt(blob: str, key_material: bytes) -> str:
    """把 encrypt() 產生的 blob 解回字串;竄改或金鑰不符會拋 ValueError。"""
    raw = base64.b64decode(blob)
    if len(raw) < len(_MAGIC) + _SALT_LEN + _NONCE_LEN + _TAG_LEN or raw[:len(_MAGIC)] != _MAGIC:
        raise ValueError("格式不符或非本工具產生的資料")
    body = raw[len(_MAGIC):]
    salt = body[:_SALT_LEN]
    nonce = body[_SALT_LEN:_SALT_LEN + _NONCE_LEN]
    ct = body[_SALT_LEN + _NONCE_LEN:-_TAG_LEN]
    tag = body[-_TAG_LEN:]
    enc_key, mac_key = _derive_keys(bytes(key_material), salt)
    expected = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("驗證失敗:資料被竄改、或金鑰材料 (裝置) 不符")
    data = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
    return data.decode('utf-8')


def encrypt_dict(d: dict, key_material: bytes) -> str:
    return encrypt(json.dumps(d, ensure_ascii=False), key_material)


def decrypt_dict(blob: str, key_material: bytes) -> dict:
    obj = json.loads(decrypt(blob, key_material))
    if not isinstance(obj, dict):
        raise ValueError("解出的內容不是 dict")
    return obj
