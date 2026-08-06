# -*- coding: utf-8 -*-
"""
core/telegram_notify.py — Telegram 通知的純邏輯層 (新ADR)

【設計原則,比照 core/ai_helper.py (ADR-049)】
1. 零 tkinter / 零 shioaji 依賴,只負責「判斷該不該通知」「組 HTTP 請求」
   「解析 Telegram API 回應」三件純邏輯,可離線單元測試。
2. 真正的 urllib HTTP 呼叫在 GUI 層背景執行緒 (stock_app_pro.py),本模組
   不發任何網路請求——理由跟 ai_helper.py 一樣:核心層要能離線完整測試,
   一旦混進真的網路呼叫,測試就會依賴外部服務、變慢、變不穩定。

用途:使用者要求「啟動量化交易時,任何成交訊息或系統訊息都要用 Telegram
傳送」。系統既有的 log_message() 已經用「【自動交易-xxx】」這個統一前綴
標記所有量化交易產生的訊息 (模擬/實單成交、風控擋單、休市待命、條件錯誤、
時間窗跳過、例外、待下單...等),不需要另外列舉——只要訊息帶這個前綴,
就代表是「量化交易相關的成交或系統訊息」,判斷邏輯因此非常單純。
"""
import json
import urllib.parse

TELEGRAM_TAG_PREFIX = '【自動交易'
API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
# Telegram sendMessage 文字上限 4096 字元,保留一點餘裕給截斷提示。
MAX_MESSAGE_LEN = 4000


def is_quant_message(msg):
    """判斷這則 log_message() 的文字是不是「量化交易相關」的訊息 (成交/
    風控/休市待命/例外...等)。系統既有慣例:這類訊息一律以
    「【自動交易-xxx】」開頭,直接檢查前綴即可,不需要窮舉每一種子類別
    (新增子類別時,只要沿用既有前綴慣例,這裡完全不用改)。"""
    return str(msg or '').startswith(TELEGRAM_TAG_PREFIX)


def config_ready(cfg):
    """判斷 Telegram 設定是否備妥可以發送 (bot_token 與 chat_id 都不可空白)。"""
    if not isinstance(cfg, dict):
        return False
    return bool(str(cfg.get('bot_token', '')).strip()) and bool(str(cfg.get('chat_id', '')).strip())


def build_send_request(bot_token, chat_id, text):
    """組出 urllib 可直接使用的 HTTP 請求描述 (dict:url/data/headers)。
    純函式,不發送——呼叫端 (GUI 層背景執行緒) 用這個 dict 呼叫
    urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers))。

    文字超過 Telegram 上限就截斷並附上省略提示,不讓一則過長訊息整個送不出去。
    不使用 parse_mode (Markdown/HTML) —— 訊息內容含有大量使用者自訂的策略
    名稱/符號,貿然套用 Markdown 解析容易因為特殊字元 (例如 `_`、`*`) 送出
    400 錯誤,純文字最不容易出錯。
    """
    token = str(bot_token or '').strip()
    cid = str(chat_id or '').strip()
    if not token or not cid:
        raise ValueError("Telegram bot_token 或 chat_id 不可空白")
    t = str(text or '')
    if len(t) > MAX_MESSAGE_LEN:
        t = t[:MAX_MESSAGE_LEN] + "…(訊息過長已截斷)"
    url = API_URL_TEMPLATE.format(token=token)
    payload = {'chat_id': cid, 'text': t, 'disable_web_page_preview': True}
    data = urllib.parse.urlencode(payload).encode('utf-8')
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    return {'url': url, 'data': data, 'headers': headers}


# ======================================================================
# 【ADR-148】同一個根因的訊息合併(burst 節流)
#
# 使用者 16:47 的實測:券商連線斷一次,手機在同一分鐘收到
#   · 1 則 ShioajiConnectionError(真正的根因)
#   · 3 則「策略「X」第 1 次錯誤: 執行商品(做B)合約解析失敗: TXF / MXFR1」
# 四則訊息、一個事件,而且策略數愈多就愈多則。手機被洗版的下場是**真正重要的
# 那一則被淹掉** —— 這跟 P-05「失敗要看得見」是同一個目標,只是反方向的失敗。
#
# 【絕對不節流成交訊息】實單/模擬成交每一則都必須送達,一則都不能合併掉:
# 那是「錢動了」的紀錄,不是狀態提醒。ALWAYS_SEND_TAGS 就是這條紅線,
# 對應的反向對照測試也釘在 tests/test_core.py。
# ======================================================================

# 這幾類前綴永遠逐則送出,不參與任何合併
ALWAYS_SEND_TAGS = ('【自動交易-實單', '【自動交易-模擬', '【自動交易-保護',
                    # 【ADR-152】終極波段的每日晨間狀態報告:一天一則,
                    # 不可能洗版;被合併掉就是整天沒看到。
                    '【自動交易-終極波段')

# 同一個 key 在這個窗口內只送第一則,其餘累計後併進下一則
BURST_WINDOW_SEC = 120.0

# 指紋取正規化後的前 N 個字元。為什麼是「截斷」而不是精確比對:同一個根因的
# 訊息只差在**策略名稱**與**商品代碼**,前者被引號剝除處理掉、後者落在訊息
# 尾端被截斷切掉,剩下的「類別 + 例外型別 + 錯誤語意」正好是我們要分組的東西。
# 調這個數字會改變分組粗細,測試裡有正反對照把預期行為釘住。
BURST_KEY_LEN = 45

_QUOTED = ('「', '」')


def _strip_quoted(text):
    """把「...」裡的內容拿掉(策略名稱)。同一個根因的訊息只差在這裡。"""
    out = []
    depth = 0
    for ch in str(text or ''):
        if ch == _QUOTED[0]:
            depth += 1
            continue
        if ch == _QUOTED[1]:
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return ''.join(out)


def burst_key(msg):
    """同一個根因的訊息要算出同一把 key。

    步驟:剝掉「策略名稱」→ 數字換成空(「第 1 次」「第 2 次」要同組)→
    收斂空白 → 取前 BURST_KEY_LEN 個字元。
    """
    t = _strip_quoted(msg)
    t = ''.join((' ' if ch.isdigit() else ch) for ch in t)
    t = ' '.join(t.split())
    return t[:BURST_KEY_LEN]


def always_send(msg):
    """這則訊息是不是「一則都不能合併」的那種(成交/自動停用)。"""
    m = str(msg or '')
    return any(m.startswith(tag) for tag in ALWAYS_SEND_TAGS)


def should_send(msg, now_ts, state, window_sec=None):
    """要不要把這則訊息送出去。回傳 (send: bool, text: str)。

    `state` 是呼叫端保管的 dict(GUI 層持有,本模組不存任何狀態,才能離線測):
        {key: {'ts': 上次送出時間, 'held': 這個窗口內被壓下的則數}}

    被壓下的則數**不會消失** —— 下一則同 key 通過時會附在文字後面
    (「同類訊息 N 則已合併」)。安靜吞掉才是這個專案禁止的事;
    這裡吞的是「重複」,不是「資訊」。
    """
    m = str(msg or '')
    if always_send(m):
        return True, m
    win = BURST_WINDOW_SEC if window_sec is None else float(window_sec)
    k = burst_key(m)
    ent = state.get(k)
    if ent is None or (float(now_ts) - float(ent.get('ts', 0))) >= win:
        held = int((ent or {}).get('held', 0))
        state[k] = {'ts': float(now_ts), 'held': 0}
        if held > 0:
            return True, f"{m}\n(過去 {win:.0f} 秒內另有 {held} 則同類訊息已合併)"
        return True, m
    ent['held'] = int(ent.get('held', 0)) + 1
    return False, m


def parse_response(body):
    """解析 Telegram sendMessage 的回應 body (bytes 或 str)。
    回傳 (ok: bool, message: str)。Telegram 成功回傳 {"ok":true,...},
    失敗回傳 {"ok":false,"description":"..."}。JSON 解析失敗一律視為失敗,
    不拋例外 (呼叫端只需要一個好懂的錯誤字串)。"""
    try:
        if isinstance(body, (bytes, bytearray)):
            body = body.decode('utf-8', errors='replace')
        obj = json.loads(body)
    except Exception as e:
        return False, f"回應解析失敗: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return False, "回應格式不是 JSON 物件"
    if obj.get('ok'):
        return True, "已送出"
    return False, str(obj.get('description', '未知錯誤'))
