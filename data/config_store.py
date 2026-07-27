"""
data.config_store — 券商設定檔與自選股清單的讀寫 (純檔案 I/O，不依賴 tkinter)。

原本是 StockTradingAppPro 的方法，讀寫時用 self.config_file / self.wl_file
當路徑；抽出後路徑改為顯式參數傳入，方便測試時指向暫存檔案，不會動到
使用者實際的設定檔。
"""
import json
import os

DEFAULT_WATCHLISTS = {
    "台股庫存": ["2330", "0050", "00679B"],
    "期貨與美股": ["TXF", "NVDA", "AAPL"],
}


def load_broker_config(path: str):
    """回傳 (api_key, secret_key, pid, ca_path)，讀不到就回傳全空字串。"""
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
                return (
                    data.get('api_key', ''),
                    data.get('secret_key', ''),
                    data.get('pid', ''),
                    data.get('ca_path', ''),
                )
        except Exception:
            pass
    return '', '', '', ''


def save_broker_config(path: str, api_key: str, secret_key: str, pid: str, ca_path: str):
    with open(path, 'w') as f:
        json.dump(
            {'api_key': api_key, 'secret_key': secret_key, 'pid': pid, 'ca_path': ca_path},
            f,
        )


def load_watchlists(path: str, default: dict = None) -> dict:
    watchlists = None
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                watchlists = json.load(f)
        except Exception:
            pass
    if not watchlists:
        watchlists = dict(default) if default is not None else dict(DEFAULT_WATCHLISTS)
    return watchlists


def save_watchlists(path: str, watchlists: dict):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(watchlists, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


# 【使用者調整】圖表版面邊界設定。前兩次由 Claude 猜測 subplots_adjust 的
# 邊界比例都沒有解決留白問題，使用者要求改成可以自己調整、調好後鎖定保存，
# 不用再靠猜的。這裡的預設值只是初始起點，實際數值以使用者調整並儲存後的
# 設定檔內容為準。
DEFAULT_CHART_LAYOUT = {
    'margin_left': 0.045,
    'margin_right': 0.995,
    'margin_top': 0.93,
    'margin_bottom': 0.07,
    'hspace': 0.14,
    'canvas_width_delta': 0,
    'canvas_height_delta': 0,
}


def load_chart_layout(path: str) -> dict:
    layout = dict(DEFAULT_CHART_LAYOUT)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            for k in DEFAULT_CHART_LAYOUT:
                if k in saved:
                    layout[k] = saved[k]
        except Exception:
            pass
    return layout


def save_chart_layout(path: str, layout: dict):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({k: layout.get(k, DEFAULT_CHART_LAYOUT[k]) for k in DEFAULT_CHART_LAYOUT}, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 【ADR-056】主圖/副圖指標參數持久化
# ---------------------------------------------------------------------------
# 使用者反映「指標參數改過就會被套用,下次開啟卻要重設」——舊版這些設定
# (MA週期/型態/顏色、布林、MACD、RSI、KDJ、DMI) 全部只存在 tk.Variable 記憶體
# 裡,程式關掉就消失,每次重開都是寫死在程式碼裡的預設值。現在比照
# chart_layout 的模式,存成一份 JSON,使用者在「主圖指標參數設定」/
# 「XX 參數設定」對話框按「確認並套用」時才寫檔 (明確動作觸發,不是每次
# 打字就默默存檔),下次開啟直接載入上次的設定。
DEFAULT_INDICATOR_SETTINGS = {
    'ma_shows': [True, True, False, False, False, False],
    'ma_types': ['SMA'] * 6,
    'ma_periods': ['5', '10', '20', '60', '120', '240'],
    'ma_colors': [],  # 空 = 沿用程式預設色盤 (第一次啟動,尚未存檔)
    'bb_show': False, 'bb_color': '青 (#00E5FF)',
    'bb_period': 20, 'bb_std1': 2.0, 'bb_std2': 3.0,
    'var_bbw': False,
    'var_macd': True, 'macd_f': '12', 'macd_s': '26', 'macd_sig': '9',
    'var_rsi': False, 'rsi_p': '14',
    'var_kdj': False, 'kd_n': '9', 'kd_m1': '3', 'kd_m2': '3',
    'var_dmi': False, 'dmi_n': '14',
}


def load_indicator_settings(path: str) -> dict:
    """讀取指標參數設定。檔案不存在/壞掉/欄位不完整都回傳預設值 (絕不因為
    設定檔壞掉就讓圖表畫不出來——這條路徑只影響「用什麼參數」,不影響
    「畫不畫得出圖」)。"""
    settings = json.loads(json.dumps(DEFAULT_INDICATOR_SETTINGS))  # 深拷貝
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            for k in DEFAULT_INDICATOR_SETTINGS:
                if k in saved:
                    settings[k] = saved[k]
        except Exception:
            pass
    return settings


def save_indicator_settings(path: str, settings: dict):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({k: settings.get(k, DEFAULT_INDICATOR_SETTINGS[k]) for k in DEFAULT_INDICATOR_SETTINGS},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 【ADR-049】AI 策略助手設定 (Anthropic API key + 模型)
# ---------------------------------------------------------------------------
def load_ai_config(path: str) -> dict:
    """讀取 AI 助手設定。檔案不存在/壞掉回傳空 dict。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_ai_config(path: str, api_key: str, model: str):
    """儲存 AI 助手設定 (key 僅存本機,注意檔案不要外流)。"""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'api_key': str(api_key or ''), 'model': str(model or '')},
                  f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 【新ADR】Telegram 通知設定 (bot token + chat id + 開關)
# ---------------------------------------------------------------------------
def load_telegram_config(path: str) -> dict:
    """讀取 Telegram 通知設定。檔案不存在/壞掉回傳空 dict (呼叫端補預設值)。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_telegram_config(path: str, bot_token: str, chat_id: str, enabled: bool):
    """儲存 Telegram 通知設定 (token 僅存本機,注意檔案不要外流)。"""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'bot_token': str(bot_token or ''), 'chat_id': str(chat_id or ''),
                  'enabled': bool(enabled)}, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 【ADR-072】一般 App 設定 (通用鍵值,例如盤中零股開盤時刻、自動重連/自動登入
# 偏好等)。用一份 JSON 存放,讀不到/壞掉一律回傳預設,絕不因設定檔壞掉就當掉。
# ---------------------------------------------------------------------------
DEFAULT_APP_SETTINGS = {
    # 【ADR-112】凱基子行程用的直譯器路徑。空字串 = 自動找 python3.13。
    # 主程式的 Python 版本載不動 kgisuperpy 時才會用到 (見 P-59)。
    'kgi_python': '',
    'odd_lot_open': '09:10',       # 盤中零股開盤時刻 (現制 09:10;未來改 09:00 自己切)
    'auto_reconnect': False,       # 斷線自動重連開關 (記憶體憑證)
    'remember_creds': False,       # 記住憑證並開機自動登入 (加密存本機)
}


def load_app_settings(path: str) -> dict:
    settings = dict(DEFAULT_APP_SETTINGS)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k in DEFAULT_APP_SETTINGS:
                    if k in saved:
                        settings[k] = saved[k]
        except Exception:
            pass
    return settings


def save_app_settings(path: str, settings: dict):
    try:
        merged = dict(DEFAULT_APP_SETTINGS)
        merged.update({k: settings[k] for k in settings if k in DEFAULT_APP_SETTINGS})
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
