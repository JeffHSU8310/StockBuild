# -*- coding: utf-8 -*-
"""
core/broker_ipc.py — 券商子行程 IPC 的協定層 (ADR-112)

【背景】凱基的 `kgisuperpy` 在 Python 3.14 載不起來 (自帶 C 擴充只編到
cp313,且載入時會拉進沒有 3.14 版的 numba,見 P-59)。使用者選擇「凱基跑
獨立 3.13 行程,主程式用 IPC 溝通」,所以需要一套跨行程的請求/回應協定。

本模組是**純協定**:編碼、解碼、錯誤分類。零網路、零 subprocess、零 SDK,
可離線完整測試 —— 因為底下那條「送出委託但沒收到回應」的判斷,是整套
設計裡最危險的一段,不能只靠實機碰運氣驗證。

================================================================
【最重要的一條:送出委託後失聯,結果是「不確定」,不是「失敗」】
================================================================

主程式把 place_order 送進管線之後,如果逾時或管線斷掉,我們**無法知道**
委託到底有沒有送到券商:

  - 當成「失敗」→ 策略維持 FLAT,下一根 K 棒再進場一次 → **可能重複下單**
  - 當成「成功」→ 記了一個可能不存在的部位 → 之後平倉會平到空氣

兩種猜法都會賠錢。正確答案是**第三種狀態:不確定**,而且處理方式是
「停掉那個策略、大聲告訴使用者去券商端確認」,不是自動重試。

因此本模組把操作分成兩類:
  - 冪等 (idempotent):查詢類,失聯了重送沒有副作用
  - 非冪等 (non-idempotent):下單類,**任何情況下都不可自動重送**
"""
import json

PROTOCOL_VERSION = 1

# 非冪等操作:失聯後絕不可自動重送 (見檔頭)
NON_IDEMPOTENT_OPS = frozenset({'place_order'})

# 回應狀態
ST_OK = 'ok'
ST_ERROR = 'error'

# 送出委託後失聯時的結果分類
OUTCOME_UNKNOWN = 'unknown'

# 記錄用途要遮蔽的欄位 (密碼絕不可進系統日誌)
SECRET_KEYS = frozenset({'person_pwd', 'password', 'pwd', 'ca_passwd', 'secret_key'})


class BrokerIPCError(RuntimeError):
    """子行程回報的一般錯誤 (已知失敗,沒有送出委託)。"""


class OrderOutcomeUnknown(RuntimeError):
    """送出委託後失聯:**不知道**券商收到了沒有。

    刻意用獨立的例外型別而不是回傳 False —— 呼叫端必須被迫分開處理這種
    情況,不能跟「明確失敗」混在同一個 except 裡當成沒送出。
    """


def is_idempotent(op):
    """這個操作失聯後能不能安全地重送。"""
    return str(op) not in NON_IDEMPOTENT_OPS


def redact(args):
    """把密碼類欄位換成 '***',給日誌用。

    這個函式存在的唯一理由是:IPC 的請求內容很適合寫進日誌來排查問題,
    而登入請求裡有身分證字號與密碼。少了這一層,除錯日誌就會變成外洩管道。
    """
    if not isinstance(args, dict):
        return args
    return {k: ('***' if k in SECRET_KEYS else redact(v) if isinstance(v, dict) else v)
            for k, v in args.items()}


def encode_request(req_id, op, args=None):
    """組一行請求 (JSON + 換行)。用 line-delimited JSON 是因為它在
    stdin/stdout 上不需要額外的長度框架,兩邊都用 readline 就能對齊。"""
    payload = {'v': PROTOCOL_VERSION, 'id': int(req_id), 'op': str(op),
               'args': args or {}}
    return json.dumps(payload, ensure_ascii=False) + '\n'


def encode_response(req_id, ok, result=None, error=None):
    payload = {'v': PROTOCOL_VERSION, 'id': int(req_id),
               'status': ST_OK if ok else ST_ERROR}
    if ok:
        payload['result'] = result
    else:
        payload['error'] = str(error or '未知錯誤')
    return json.dumps(payload, ensure_ascii=False, default=str) + '\n'


def decode_line(line):
    """解析一行 JSON。空行回 None (呼叫端略過);壞掉的行拋例外。

    壞行拋例外而不是安靜略過,是因為它代表兩邊的協定對不上——安靜略過會
    變成「主程式一直在等一個永遠不會來的回應」。
    """
    s = (line or '').strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError) as e:
        raise BrokerIPCError(f"子行程回傳了無法解析的內容: {s[:120]}") from e
    if not isinstance(obj, dict):
        raise BrokerIPCError(f"子行程回傳了非物件內容: {s[:120]}")
    return obj


def parse_response(obj, expect_id):
    """檢查回應並取出結果。回應 id 對不上一律視為錯誤。

    id 比對是必要的:如果上一個請求逾時、它的回應晚到了,沒有比對就會把
    舊回應當成新請求的答案——在下單這條路上,那等於「拿 A 單的結果去
    判斷 B 單成功與否」。
    """
    if obj is None:
        raise BrokerIPCError("子行程沒有回應 (管線已關閉)")
    got = obj.get('id')
    if got != expect_id:
        raise BrokerIPCError(f"回應序號不符 (期望 {expect_id},收到 {got})")
    if obj.get('status') == ST_OK:
        return obj.get('result')
    raise BrokerIPCError(str(obj.get('error') or '子行程回報未知錯誤'))


def unknown_outcome_message(op, detail=''):
    """失聯時給使用者看的訊息。必須明講「不確定」與「該做什麼」。"""
    tail = f" ({detail})" if detail else ''
    if is_idempotent(op):
        return f"與券商子行程失聯{tail};此為查詢類操作,可直接重試。"
    return (f"⚠️ 委託已送進子行程但沒有收到回應{tail}。\n"
            f"**無法確認券商是否已收到這張單**,系統不會自動重送。\n"
            f"請立刻到券商端 (App / 網頁) 確認委託狀態,再決定是否手動補單。")
