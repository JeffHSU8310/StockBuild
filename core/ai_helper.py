# -*- coding: utf-8 -*-
"""
core/ai_helper.py — AI 策略助手 (Claude API) 的純邏輯層

【ADR-049】在看盤軟體內用中文描述 → 產生 on_bar(ctx) 策略程式碼。

設計立場 (誠實且務實):
  * 本模組只負責「組請求 payload」與「從回應抽出程式碼」兩件純邏輯,
    可離線測試;真正的 HTTP 呼叫在 GUI 層背景執行緒 (urllib,零新依賴)。
  * AI 產生的程式碼「不會」被特殊對待:一樣要過 validate_source 靜態檢查、
    一樣在獨立子行程執行、一樣受總開關+每日次數+冷卻+熔斷防護。
    鐵則「只執行你看得懂的程式」對 AI 產生的程式碼同樣適用 ——
    產生後請先讀懂、試跑、回測、跑模擬,再考慮實單。
  * 需要使用者自備 Anthropic API key (console.anthropic.com,依 token 計費,
    與 claude.ai 訂閱是兩回事);key 只存在本機設定檔。
"""
import json

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"

# 系統提示:把本系統的 ctx 介面規格完整交代給模型,並強制「只輸出程式碼」。
SYSTEM_PROMPT = """你是台股量化交易策略工程師。使用者會用中文描述策略,你要輸出可直接執行的 Python 策略程式碼。

嚴格規則:
1. 只輸出 Python 程式碼本身,不要 markdown 圍欄、不要任何說明文字。
2. 必須在最外層定義 def on_bar(ctx): 函式,這是唯一入口,系統每根K棒收盤自動呼叫。
3. 不要寫 import、登入、排程、while 迴圈、print — 系統全部代勞;需要輸出用 ctx.log(訊息)。
4. 可加中文註解說明邏輯。可定義輔助函式。可調參數一律用 ctx.param('名稱', 預設值) 讀取。

ctx 介面 (只能用這些):
- ctx.df: 已收盤K棒 DataFrame (index=時間, 欄位 Open/High/Low/Close/Volume)
- ctx.close / ctx.open / ctx.high / ctx.low / ctx.volume / ctx.time: 最新一根的值
- ctx.position: 'FLAT'(空手)/'LONG'(持多)/'SHORT'(持空); ctx.entry_price: 進場價; ctx.bars_in_position: 已持倉K棒數
- ctx.state: dict,跨K棒持久保存 (可做移動停損等)
- 指標 (回傳 pandas.Series,取最新值用 .iloc[-1]): ctx.sma(n), ctx.ema(n), ctx.rsi(n=14), ctx.atr(n=14), ctx.roc(n=10), ctx.stddev(n=20), ctx.vwap(), ctx.highest(n), ctx.lowest(n)
- ctx.kd(n=9,m1=3,m2=3) 回傳 (K序列, D序列); ctx.macd(fast=12,slow=26,signal=9) 回傳 (DIF, MACD, OSC); ctx.bb(n=20,std=2) 回傳 (上軌, 中軌, 下軌)
- 交叉判斷: ctx.cross_up(a, b) / ctx.cross_down(a, b) (a、b 為 Series)
- 決策回傳 (必須 return 其中之一): ctx.buy()=開多(或平空), ctx.sell()=開空(僅期貨,或平多), ctx.close_position()=平倉, ctx.hold()=不動作

慣例:
- 開空只有期貨可用;股票策略只用 buy/close_position。
- 指標序列開頭會有 NaN,取值前先確認資料量足夠 (len(ctx.df) 太短就 return ctx.hold())。
- 停損停利自己在 on_bar 裡用 ctx.entry_price 判斷。"""


def build_messages_payload(description, model=None, max_tokens=2000):
    """組 Anthropic Messages API 的請求 payload (dict)。純函式,可測。"""
    desc = str(description or '').strip()
    if not desc:
        raise ValueError("策略描述不可空白")
    return {
        "model": model or DEFAULT_MODEL,
        "max_tokens": int(max_tokens),
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": desc}],
    }


def extract_code(response):
    """
    從 API 回應抽出程式碼。接受 dict (已解析 JSON) 或 str (原始 JSON / 純文字)。
    - 串接所有 type='text' 內容區塊
    - 剝除 markdown 圍欄 (```python ... ``` 或 ``` ... ```),模型偶爾不聽話還是會加
    回傳程式碼字串;抽不到內容時拋 ValueError。
    """
    if isinstance(response, (bytes, bytearray)):
        response = response.decode('utf-8', errors='replace')
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return _strip_fences(response)
    if isinstance(response, dict):
        if response.get('type') == 'error' or 'error' in response:
            err = response.get('error') or {}
            raise ValueError(f"API 回應錯誤: {err.get('type', '')} {err.get('message', '')}".strip())
        blocks = response.get('content') or []
        text = "\n".join(b.get('text', '') for b in blocks
                         if isinstance(b, dict) and b.get('type') == 'text')
        text = text.strip()
        if not text:
            raise ValueError("API 回應中沒有文字內容")
        return _strip_fences(text)
    raise ValueError(f"無法解析的回應型別: {type(response).__name__}")


def _strip_fences(text):
    """剝除 markdown 程式碼圍欄;沒有圍欄就原樣回傳 (去頭尾空白)。"""
    t = str(text).strip()
    if t.startswith('```'):
        first_nl = t.find('\n')
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.rstrip().endswith('```'):
            t = t.rstrip()[:-3]
    return t.strip()
