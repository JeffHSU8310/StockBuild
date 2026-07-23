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
