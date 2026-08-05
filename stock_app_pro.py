import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import tkinter.simpledialog as sd
import threading
import time
import copy
import os
import sys
import json
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib.pyplot as plt
import mplfinance as mpf
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from datetime import datetime, timedelta
from collections import deque
import gc
import re
import platform
import getpass
import secrets
import traceback
import urllib.request
import urllib.parse

# 【架構重構 ADR-009】純邏輯層抽出到 core/ 與 data/,詳見 DECISIONS.md。
# 這裡 import 進來的函式取代了原本寫在 StockTradingAppPro 類別內、
# 跟 tkinter/shioaji 完全無關的計算與檔案存取邏輯。
from core import tick_rules
from core import indicators as core_indicators
from core import chart_viewport
from core import history_policy
from core import futures_session
from core import order_rules
# 【ADR-110 階段1】券商中立的委託意圖:讓價/檔位/限市價的判斷從這裡出來,
# 各家券商 adapter 再把它翻成自家 SDK 的委託物件。
from core import order_intent
from core import sj_compat
from core import unit_format
from core import strategy_engine
from core import backtest
from core import custom_strategy
from core import fut_catalog
from core import ai_helper
from core import optimizer
from core import paper_account
from core import taifex_daily
from core import market_session
from core import secure_store
from core import chukuangren_band
from core import telegram_notify
from core import telegram_control
from core import market_pattern
# 【ADR-122】kbars 請求要不要分段、每段幾天 (門檻與段長的單一出處)。
from core import kbars_plan
# 【ADR-120】主圖【盤勢判斷】面板的純邏輯 (設定正規化 + 通知去重)。
from core import fibonacci
from core import jae
from core import regime_panel
# 【ADR-138】指標線色盤 (舊 8 色 + 255 系統色)
from core import palette
# 【ADR-139】永豐 API 測試的純規則 (測試時段/版本/欄位/最近月合約)。
from core import api_test
# 【ADR-100】籌碼資料:解析純邏輯在 core/,本地 CSV 存取在 data/。
from core import chips_parser
# 【ADR-101】籌碼併進策略 df (含未來函數防護)。
from core import chips_features
# 【ADR-102】量價關係推算支撐/壓力點位 (Volume Profile)。
from core import volume_profile
# 【ADR-103】全方位選股:基本面解析 + 全市場篩選引擎 + 全市場資料儲存。
from core import fundamental_parser
from core import market_screener
from core import screener_backtest
from data import market_store
from data import kbars_store
from data import config_store
from data import taifex_store
from data import chips_store
# 【ADR-097 階段0】券商連線生命週期抽到 brokers/ 套件,詳見 DECISIONS_ADR097.md。
from brokers.sinopac import SinopacBroker
# 【ADR-139】永豐 API 測試 (模擬環境的登入/下單測試) 用的一次性連線型別。
# 刻意整個模組匯入而不是只匯入類別:診斷要能 monkeypatch
# sinopac.SinopacApiTestSession,只匯入名稱的話換不掉。
from brokers import sinopac
# 【ADR-111】凱基 adapter。HAS_KGI 為 False 代表套件沒裝或載不起來
# (Python 3.14 就是這種情況),主程式照常運作,只是沒有凱基這個選項。
from brokers.kgi import KGIBroker, HAS_KGI
# 【ADR-112】主程式的 Python 版本載不了 kgisuperpy 時,凱基改用獨立
# 3.13 子行程 + IPC (見 DECISIONS_ADR112.md)。
from brokers.kgi_proxy import KGIProcessBroker
from core import broker_ipc

# 嘗試載入永豐金 API
try:
    import shioaji as sj
    HAS_SJ = True
except ImportError:
    HAS_SJ = False

plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei'] 
plt.rcParams['axes.unicode_minus'] = False

mc = mpf.make_marketcolors(up='#FF1744', down='#00E676', edge='inherit', wick='inherit', volume='inherit', ohlc='inherit')
xq_style = mpf.make_mpf_style(
    marketcolors=mc, facecolor='#12161A', figcolor='#1A2026', gridcolor='#2A323D', gridstyle=':',
    rc={'text.color': 'white', 'axes.labelcolor': 'white', 'xtick.color': '#8A99AD', 'ytick.color': '#8A99AD', 
        'font.sans-serif': 'Microsoft JhengHei', 'axes.unicode_minus': False,
        # 【使用者調整】縮小 x 軸日期刻度字體,搭配 draw_chart() 裡 xrotation 改小角度,
        # 讓底部日期標籤佔用的版面縮小 (原本旋轉角度較大時,文字會佔用較多垂直高度)。
        'xtick.labelsize': 8, 'ytick.labelsize': 8}
)

# ============================================================
# 【ADR-057】金額顯示:無條件捨去小數 (使用者需求 #2)
# ============================================================
# 使用者要的是「小數點後面的數字不要」= 無條件刪除,不是四捨五入。
# `f"{v:,.0f}"` 是四捨五入 (-53438.54 → -53,439),會憑空多算 0.46 元,
# 所以不能直接用;這裡用 int() 往零截斷後再格式化 (-53438.54 → -53,438)。
#
# ⚠ 刻意「不」套用到這幾類數字,因為截掉小數會直接毀掉它們的意義:
#   * 百分比 (勝率 24.7%、報酬率 -53.09%) —— 使用者也明確說「只要不是%」。
#   * 比率類 (獲利因子 1.83、賺賠比 0.97、夏普比率 -0.01) —— 這些本來就
#     落在 0~3 之間,截成整數後 1.83 和 1.02 會變成同一個「1」,等於報告
#     失去判讀能力。這是刻意的取捨,若確認要一起截,改這裡兩個函式即可。
#   * 價格 (進場價 100.65) —— 台股價格本來就有小數,截掉會變成錯的價格。

# ============================================================
# 【ADR-060】所有資料檔一律以「程式所在資料夾」為基準,不用相對路徑
# ============================================================
# 【根因】原本 broker_config.json / watchlists.json / taifex_daily/ … 全部寫成
# 相對路徑 ("." 或純檔名),實際解析成「啟動程式時的當前工作目錄 (CWD)」。
# 從 Anaconda Prompt、桌面捷徑、或其他資料夾啟動時 CWD 並不是 G:\StockBuild,
# 於是:
#   * 期交所歷史 (taifex_daily/TX.csv) 找不到 → load_daily 安靜回傳空表
#     → 延伸沒生效、也不會跳過券商下載,使用者卻完全看不出原因
#     (使用者實測:MXFR1 明明匯入過 MTX.csv,系統照樣狂發分段下載)
#   * 自選股/策略/版面設定也可能存到別的資料夾去
# 改成以 __file__ 所在目錄為基準,不論怎麼啟動都指向同一個地方。
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def app_path(*parts):
    """把相對檔名解析成「程式所在資料夾」底下的絕對路徑。"""
    return os.path.join(APP_DIR, *parts)


def _fmt_amt(v):
    """金額 → '1,234' (無條件捨去小數)。無法轉數字就原樣回傳。"""
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_amt_signed(v):
    """金額 → '+1,234' / '-1,234' (無條件捨去小數)。"""
    try:
        n = int(float(v))
        return f"{n:+,}"
    except (TypeError, ValueError):
        return str(v)


class StockTradingAppPro(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("XQ 旗艦全週期互動版 - 全 API 實盤五檔報價系統")
        self.geometry("1700x950") 
        self.configure(bg="#12161A") 

        # ============ 【ADR-057】關閉自動循環 GC,改由主執行緒定期回收 ============
        # 【根因】使用者實測崩潰訊息:
        #     File "...\tkinter\__init__.py", line 416, in __del__
        #     RuntimeError: main thread is not in main loop
        #     Tcl_AsyncDelete: async handler deleted by the wrong thread
        # 那個 __del__ 是 tkinter.Variable.__del__ —— 它會呼叫 self._tk.call(...)
        # 回 Tcl。tk 物件 (Variable/Widget) 只要參與了「參照循環」(widget →
        # command 閉包 → Variable → widget,tkinter 幾乎必然形成循環),就不會被
        # refcount 立即釋放,而是丟給 Python 的循環 GC。而循環 GC 會在「任何一條
        # 執行緒」配置記憶體超過門檻時觸發 —— 參數最佳化 worker 每組參數都
        # deepcopy 策略、產生新的 DataFrame,是全程式最會配置記憶體的地方,
        # 於是 GC 幾乎一定在 worker 執行緒被觸發,回收到某個已關閉對話框留下的
        # tk.Variable → __del__ 在非主執行緒呼叫 Tcl → RuntimeError,接著
        # Tcl 的 C 執行期直接 abort 整個行程 (Tcl_AsyncDelete)。
        # 這也解釋了為什麼 ADR-056 只把 safe_after 改成攔 RuntimeError 沒有解決:
        # 崩潰根本不是發生在 safe_after 裡,而是在 GC 觸發的 __del__ 裡,
        # 那是 Python 直譯器自己呼叫的,應用層 try/except 攔不到。
        #
        # 【修法】把自動循環 GC 關掉 (gc.disable()),改成主執行緒每 20 秒
        # 主動 gc.collect() 一次。這樣循環垃圾一律在主執行緒被回收,
        # tk.Variable.__del__ 也就一定在主執行緒執行 —— 從結構上消除這個崩潰,
        # 而不是碰運氣。
        # 【代價 (誠實說明)】關閉自動 GC 後,兩次主執行緒回收之間的循環垃圾會
        # 暫時累積 (非循環物件仍靠 refcount 立即釋放,不受影響)。20 秒的間隔
        # 對這個應用的配置速率而言記憶體增量很小;draw_chart 也仍會在重繪時
        # 主動 collect 一次。若未來發現記憶體吃緊,調短間隔即可,不要改回
        # 自動 GC —— 那會把這個崩潰帶回來。
        gc.disable()
        self._gc_interval_ms = 20000

        # ================= 系統與 API 變數 =================
        # 【ADR-120】使用者要求:打開程式預設主圖就是加權指數日K
        # (週期預設值見 self.timeframe_var,本來就是「日K」)。
        self.current_symbol = regime_panel.DEFAULT_INDEX_SYMBOL
        self.current_stock_name = "" 
        self.asset_type = "stock" 
        self.data_source = ""
        self.position = 0 
        
        self.api_logged_in = False
        # 【ADR-011】fm_token / use_yf_backup 已移除:FinMind 登入功能刪除,
        # 台股資料一律使用 shioaji,不再有 YF/FinMind 備援可切換。
        
        # ================= 串流 Quote 五檔報價暫存 =================
        self.current_contract = None
        self.current_bidask_normal = None  
        self.current_bidask_odd = None     
        self.current_tick_normal = None    
        self.current_tick_odd = None       
        self.is_odd_lot = False
        
        # 用於零股價差即時運算
        self.last_snap_time = 0
        self.last_norm_close = 0.0
        # 盤後零股 (13:40-14:30) 收盤資料快取:由整股 v1 tick 串流的 closing_oddlot_* 欄位帶入。
        # snapshot 沒有零股欄位,只有這條路能拿到真實零股收盤價;冷登入 (無串流) 時仍會是 0。
        self.last_odd_close = 0.0
        self.last_odd_shares = 0
        self.last_odd_date = ""

        # ================= 下單面板 (ADR-008):交易別/種類/條件/當沖 =================
        # trade_mode 對應 shioaji StockOrderLot:
        #   "Common"=整股 "IntradayOdd"=盤中零股 "Fixing"=盤後定價 "Odd"=盤後零股
        self.trade_mode = "Common"
        self.order_cond = "Cash"   # Cash=現股 MarginTrading=融資 ShortSelling=融券
        self.order_type_tif = "ROD"  # ROD/IOC/FOK,僅整股模式可切換
        self.daytrade_var = tk.BooleanVar(value=False)  # 現股當沖(先賣後買)
        self.current_day_trade = False   # 目前商品是否為可現沖標的 (contract.day_trade == 'Yes')
        self.current_reference_price = 0.0  # 昨收/平盤價,成交明細漲跌與取價「平盤」都靠這個

        # 【使用者調整#5】我的委託單 / 我的已成交。依 shioaji 官方「使用限制」文件
        # 明確建議「委託狀態請使用主動回報，避免以 update_status() 輪詢」，這兩份
        # 清單完全由 set_order_callback() 註冊的 push callback 更新，不做輪詢。
        # my_orders: order_id -> dict(...)；my_fills: list of dict(...)，最新的在前面。
        self.my_orders = {}
        self.my_fills = []
        self._last_pending_order_key = None
        self._last_pending_order_info = None

        # 成交明細跳動列表暫存 (整股/零股分開,各存最近 20 筆)
        self.trade_feed_normal = deque(maxlen=20)
        self.trade_feed_odd = deque(maxlen=20)
        
        # 串流資料執行緒鎖 (shioaji callback 與 UI worker 分屬不同執行緒)
        self.quote_lock = threading.Lock()
        # 【切換加速】報價退訂/訂閱的 shioaji 網路呼叫改丟背景執行緒,不再擋住
        # K 線出圖 (使用者點自選股要「馬上換圖」)。此鎖確保快速連點時多路
        # 退訂/訂閱不會互相交錯,維持「退舊 → 訂新」的先後順序。
        self.subscribe_lock = threading.Lock()
        # 盤後備援快照節流 (避免每 0.5 秒狂打 snapshots 吃光每日 API 流量配額)
        self.last_fallback_snap_time = 0
        self.odd_no_stream_warned = False
        
        # 【ADR-097 階段0】self.brokers 是未來多券商註冊表 (key 為券商代號)。
        # self.sj_api 目前仍指向跟 self.brokers['sinopac'].api 完全相同的
        # shioaji 實例,現有上百處直接呼叫 self.sj_api.xxx 的地方不受影響。
        self.brokers = {}
        if HAS_SJ:
            self.brokers['sinopac'] = SinopacBroker()
            self.sj_api = self.brokers['sinopac'].api
        self.config_file = app_path("broker_config.json")
        self.wl_file = app_path("watchlists.json")
        # 【ADR-072】一般 App 設定 (零股開盤時刻、自動重連/自動登入偏好)。
        self.app_settings_file = app_path("app_settings.json")
        self.app_settings = config_store.load_app_settings(self.app_settings_file)
        # ADR-142:SQLite 自動建庫；後續每次載入的長週期 K 線都做增量 upsert。
        self.kbars_db_file = app_path("data", "kbars.sqlite3")
        kbars_store.initialize(self.kbars_db_file)
        # 套用盤中零股開盤時刻到 market_session (預設 09:10,可設定)。
        market_session.set_odd_lot_open_hhmm(self.app_settings.get('odd_lot_open', '09:10'))
        # 【ADR-120】主圖【盤勢判斷】的設定。正規化在 core/regime_panel,
        # 設定檔缺欄位/被改壞一律回一份能用的設定,不會讓主圖畫不出來。
        self.regime_settings = regime_panel.normalize(self.app_settings.get('regime_panel'))
        # 盤勢型態通知的去重狀態:symbol -> {'date','oneshot','persistent'}
        self._regime_notify_state = {}

        # 【ADR-111/112】(註冊時機要在 app_settings 載入之後:子行程模式
        # 需要讀使用者設定的 3.13 直譯器路徑)
        # 【ADR-111/112】凱基:主程式的直譯器載得動 kgisuperpy 就直接用
        # (少一層行程邊界);載不動 (例如 Python 3.14) 就改用獨立 3.13 子行程
        # + IPC。兩者對上層是同一個介面,策略設定完全不用改。
        if HAS_KGI:
            self.brokers['kgi'] = KGIBroker()
        else:
            self.brokers['kgi'] = KGIProcessBroker(
                python_path=self.app_settings.get('kgi_python', ''))

        # 【ADR-071/073】加密憑證存放檔 (只在勾選「記住憑證」時才會有內容)。
        self.secure_creds_file = app_path("broker_secure.json")
        # 【第十二輪修正】登入進行中旗標:防止使用者在「畫面看起來沒反應」時
        # 誤以為沒點到而連續點擊,同時觸發第二個 process_broker_login 背景執行緒
        # 去搶同一組 shioaji 資源——這只會讓 GIL 爭用更嚴重、凍結更久 (見下方
        # process_broker_login 的說明)。
        self._login_in_progress = False
        self._login_watchdog_id = None
        # 【ADR-071】斷線自動重連:登入成功時把「這次登入用的憑證」暫存在記憶體
        # (只在 RAM,不寫磁碟),供斷線後背景重連使用;_reconnecting 防止同時
        # 跑多個重連工作。auto_reconnect_var (勾選開關) 在 create_widgets 才建立。
        self._login_creds_mem = None   # dict(api_key/secret_key/pid/ca_path/ca_pw) or None
        self._reconnecting = False
        # 【第十七輪修正】kbars 串接鎖:手動查詢/主圖自動更新/量化 runner 三個
        # 執行緒共用同一條 shioaji 連線,「同時」呼叫 kbars 會互相干擾 (使用者
        # 實例:切換商品瞬間自動更新也在抓 → 背景補全下載失敗、K線圖異常)。
        # 所有 kbars 下載一律先取得這把鎖,強制串行化。
        self._kbars_lock = threading.Lock()
        self._fetch_in_progress = False  # 手動查詢進行中 (自動更新此時必須讓路)
        # 【ADR-028】自選股即時報價狀態
        self._wl_quotes = {}          # sym -> (close, chg, pct)
        self._wl_current_syms = []    # 目前群組代碼快照 (UI 執行緒寫入,worker 讀取)
        self._wl_contract_cache = {}  # sym -> contract (登入世代內有效,重登清空)
        self._wl_select_suppress = False  # 程式化選取時抑制 select 事件觸發查詢
        # 【第十六輪 第2項】期貨/指數自選股串流:10秒快照對當沖太慢,改訂閱推播
        self._wl_stream_quotes = {}   # sym -> (close, chg, pct) 由 tick callback 寫入
        self._wl_fut_code_map = {}    # 期貨商品前綴(3碼) -> 自選股代碼
        self._wl_idx_code_map = {}    # 指數 tick code ('001'/'101') -> '^TWII'/'^TWOII'
        self._wl_stream_refs = {}     # sym -> 平盤參考價 (tick 無漲跌欄位時計算用)
        self._wl_subscribed = set()   # 已訂閱的自選股代碼 (連線世代內有效)
        self.chart_layout_file = app_path("chart_layout.json")
        self.saved_api_key, self.saved_secret_key, self.saved_pid, self.saved_ca_path = self.load_config()
        self.load_watchlists()
        # 【使用者調整#1】圖表邊界改成可以自己調整、調好後存起來，不用再靠猜的。
        # 讀不到設定檔就用 config_store.DEFAULT_CHART_LAYOUT 這組初始值。
        self.chart_layout = config_store.load_chart_layout(self.chart_layout_file)

        self.current_wl_name = tk.StringVar(value=list(self.watchlists.keys())[0] if self.watchlists else "台股庫存")
        
        # ================= 快取與互動變數 =================
        self.current_df = None 
        self.plot_df = None
        self.axlist = None
        self.current_panel_ratios = [5, 1.2]  # 【第五輪修正】面板高度比例,供 _apply_chart_margins 重定位使用
        # 【ADR-024 效能】kbars 原始分K快取: symbol -> {'t':抓取時間,'start':涵蓋起點,'df':分K}
        # 換週期/換回剛看過的商品時直接用快取重採樣秒開,過期則先畫快取再背景刷新。
        self._kbars_raw_cache = {}
        # 【ADR-049】期交所官方日K延伸:taifex_prod -> 日K DataFrame 記憶體快取
        # (磁碟來源 data/taifex_store,匯入完成後由 UI 執行緒清快取重載)。
        self._taifex_mem_cache = {}
        self._taifex_import_running = False   # 匯入/下載進行中防重入
        self._taifex_extend_noted = set()     # 每商品只提示一次「已延伸」的日誌
        # 【ADR-024 效能/正確性】fetch 序號:每次 start_fetch_thread 遞增。快速連續切換
        # 商品時,舊 worker (例如很慢的台指期) 若最後才完成,發布前發現序號已過期就
        # 直接放棄,不會把「後切商品」的圖蓋掉。
        self._fetch_seq = 0
        self._last_fetch_raw_sym = None
        self.vlines = [] 
        self.txt_main_segments = []  # 【使用者調整#9】主圖 MA/BB 逐項獨立上色文字物件清單
        self.sub_texts = {}        
        self.last_hover_idx = -1
        
        self.timeframe_var = tk.StringVar(value="日K")
        self.var_adjusted = tk.BooleanVar(value=False) 
        
        self.is_panning = False
        self.press_x_pixel = None
        self.press_xlim = None
        self.saved_xlim = None
        self._view_is_auto_default = True  # 追蹤當前圖表視角是否為系統預設值

        self.current_fig = None
        self.current_canvas = None
        
        # ================= 指標參數狀態 =================
        # 【ADR-138】色盤搬到 core/palette.py:舊有 8 色原封不動排最前面
        # (標籤字串一字未改,使用者存過的顏色設定照樣對得上),後面接
        # 20 色相 × 12 階 + 15 灰 = 255 色。使用者要求「最好有 255 種」。
        self.color_map = palette.color_map()
        self.ma_shows = [tk.BooleanVar(value=True if i < 2 else False) for i in range(6)]
        self.ma_types = [tk.StringVar(value="SMA") for i in range(6)]
        self.ma_periods = [tk.StringVar(value=p) for p in ["5", "10", "20", "60", "120", "240"]]
        self.ma_colors = [tk.StringVar(value=c) for c in list(self.color_map.keys())[:6]]
        self.bb_show, self.bb_color = tk.BooleanVar(value=False), tk.StringVar(value="青 (#00E5FF)")
        # 【ADR-138】布林通道:期間 + **上線 σ / 下線 σ 各一個,兩者分開**。
        # ADR-029~131 這兩個參數是「內圈那一對 / 外圈那一對」,使用者要的是
        # 「上線一個參數、下線一個參數」,所以語意在 ADR-138 整個換掉:
        #   UPPER = 中線 + bb_std_up × STD;LOWER = 中線 − bb_std_dn × STD
        # 要第二條通道請開「第2組」—— 它本來就是完整獨立的一組。
        self.bb_period = tk.IntVar(value=20)
        self.bb_std_up = tk.DoubleVar(value=2.0)
        self.bb_std_dn = tk.DoubleVar(value=2.0)
        # 【ADR-131】布林改成「兩組完整通道」:每組有自己的中線 (類型+期間) 與
        # 自己的上下線。第1組沿用上面既有的變數與欄位名 (零迴歸),
        # 這裡只新增「中線類型」與整個第2組。
        self.bb_type = tk.StringVar(value="SMA")
        self.bb2_show, self.bb2_color = tk.BooleanVar(value=False), tk.StringVar(value="紫 (#E040FB)")
        self.bb2_type = tk.StringVar(value="SMA")
        self.bb2_period = tk.IntVar(value=60)
        self.bb2_std_up = tk.DoubleVar(value=2.0)
        self.bb2_std_dn = tk.DoubleVar(value=2.0)
        # 【ADR-133】黃金切割律 (費波南希回撤)。擺盪高低點由最近 N 根自動抓,
        # 不必手動拉線;比率清單可勾選 (含台股常用的次級分割律 0.191/0.809)。
        self.fib_show = tk.BooleanVar(value=False)
        self.fib_lookback = tk.IntVar(value=fibonacci.DEFAULT_LOOKBACK)
        self.fib_color = tk.StringVar(value="黃 (#FFCA28)")
        self.fib_ratios = list(fibonacci.DEFAULT_LEVELS)
        self._fib_last_result = None

        self.var_macd = tk.BooleanVar(value=True)
        self.macd_f, self.macd_s, self.macd_sig = tk.StringVar(value="12"), tk.StringVar(value="26"), tk.StringVar(value="9")
        self.var_rsi = tk.BooleanVar(value=False)
        self.rsi_p = tk.StringVar(value="14")
        self.var_kdj = tk.BooleanVar(value=False)
        self.kd_n, self.kd_m1, self.kd_m2 = tk.StringVar(value="9"), tk.StringVar(value="3"), tk.StringVar(value="3")
        self.var_dmi = tk.BooleanVar(value=False)
        self.dmi_n = tk.StringVar(value="14")
        # 【ADR-134】KDJ 的超買/超賣參考線 —— 隔壁 RSI 副圖有 80/20 兩條虛線,
        # KDJ 一直沒有,是兩個面板之間唯一客觀的落差。
        self.kd_ob = tk.StringVar(value="80")
        self.kd_os = tk.StringVar(value="20")
        # 【ADR-134】JAE 指標 (使用者自創:A=RSI、J=KDJ的J、E=長期趨勢線)
        self.var_jae = tk.BooleanVar(value=False)
        self.jae_params = {k: tk.StringVar(value=str(v))
                           for k, v in jae.DEFAULT_PARAMS.items()}
        self.var_bbw = tk.BooleanVar(value=False)
        # 【ADR-011】var_inst/var_margin (法人/資券) 已移除:
        # 這兩個指標原本的資料來源是 FinMind,FinMind 已停用，故一併移除。

        # 【ADR-056】主/副圖指標參數持久化:讀上次「確認並套用」存下的設定,
        # 蓋掉上面剛剛寫死的程式碼預設值。讀不到 (第一次啟動/檔案不存在/壞掉)
        # 就維持程式碼預設值不受影響——這條路徑只會讓畫面「更貼近你上次調的
        # 樣子」,絕不會因為設定檔問題而讓圖表畫不出來。
        self.indicator_settings_file = app_path("indicator_settings.json")
        self._apply_indicator_settings(config_store.load_indicator_settings(self.indicator_settings_file))

        # 【ADR-012】視窗關閉旗標:fetch_market_indices_worker/fetch_realtime_worker
        # 是背景 daemon thread，永遠迴圈執行直到程式行程結束；如果使用者關掉視窗，
        # 這些執行緒還是會繼續跑，並試圖透過 self.after(...) 更新已經被銷毀的
        # widget (例如 lbl_twii)，導致 _tkinter.TclError: invalid command name。
        # 加這個旗標讓背景執行緒與所有排程更新都能提早退出，見 safe_after()。
        self._closing = False
        self.protocol("WM_DELETE_WINDOW", self.on_app_close)

        self.setup_styles()
        self.create_widgets()
        
        self.log_message("【系統啟動】已阻斷初始 YF 加載。請先完成券商實盤 API 驗證登入。")
        # 【ADR-035】量化自動交易:總開關每次啟動一律關閉 (絕不持久化「開」狀態)
        self._qt_running = False
        # 【新ADR】Telegram 通知設定:快取進記憶體,log_message() 每次呼叫都會
        # 檢查,不要每次都重新讀檔案 (量化 runner 訊息很密集)。
        self.telegram_cfg = config_store.load_telegram_config(self.TELEGRAM_CONFIG_FILE)
        self._qt_load()
        self._qt_refresh_tree()
        # 【ADR-108】手機遠端控制:設定檔有開才真的啟動輪詢執行緒。沒開就
        # 完全不建立執行緒 (不是建立後閒置),讓「沒開這個功能的人」的行為
        # 與加這個功能之前一模一樣。
        #
        # 【ADR-138】但**狀態一定要說出口**。使用者回報「手機打 /help 完全沒
        # 反應,可是系統傳給我的訊息收得到」—— 原因是通知與遠端控制是兩個
        # 獨立的勾選框,他只勾了通知。當時這裡沒開就靜悄悄什麼都不做,
        # 連一行日誌都沒有,使用者無從得知自己少勾一個框。
        self._tg_log_control_state()
        if (self.telegram_cfg or {}).get('remote_control'):
            self.start_telegram_control()
        threading.Thread(target=self.fetch_market_indices_worker, daemon=True).start()
        threading.Thread(target=self.fetch_realtime_worker, daemon=True).start()
        # 【ADR-028】自選股即時報價 worker:每 10 秒批次抓一次目前群組的快照
        threading.Thread(target=self.watchlist_quote_worker, daemon=True).start()
        # 【ADR-035】量化 runner:總開關關閉時完全閒置
        threading.Thread(target=self.quant_runner_worker, daemon=True).start()
        # 【ADR-132】每日收盤後的盤勢判斷推播 (不受自動交易總開關影響)
        threading.Thread(target=self.regime_daily_notify_worker, daemon=True).start()
        # 【第十六輪 第6項】主圖K棒自動更新:分K跨收盤邊界自動長新K棒,免手動重載
        self.current_timeframe = None
        threading.Thread(target=self.chart_auto_refresh_worker, daemon=True).start()
        # 【ADR-041】活K棒:tick 驅動的形成中K棒 (畫家每 400ms blit,不重繪全圖)
        self._live_bar = None
        self._live_bar_artists = None
        self.safe_after(1000, self._live_bar_painter)
        # 【ADR-073】開機自動登入:若已勾「記住憑證」且解得出加密憑證,啟動後
        # 稍等 1.5 秒 (等視窗/元件都就緒) 再背景自動登入,達成「開一次不用管」。
        self.safe_after(1500, self._try_auto_login_on_start)

    # 【ADR-119】按下 X 之後最多等這麼久,時間到一律強制結束 (見 on_app_close)。
    CLOSE_HARD_DEADLINE = 8.0
    # 診斷腳本要呼叫 on_app_close() 驗證關閉流程,但它自己還要繼續跑完其餘
    # 案例 —— 看門狗會在時限到達時把診斷行程一起殺掉 (實測踩到:診斷輸出
    # 整個空白、結束碼卻是 0)。留一個明確的開關讓診斷關掉它;production
    # 一律是 True,不會有人去改。
    CLOSE_FORCE_EXIT = True

    def on_app_close(self):
        """
        【ADR-012/ADR-014】視窗關閉時的收尾處理。

        先把 _closing 設成 True，讓背景 daemon thread 在下一次迴圈檢查時
        自然退出、不再呼叫 self.safe_after()。

        【ADR-014】接著若目前已登入券商 API，嘗試呼叫 self.sj_api.logout()
        釋放連線——shioaji 底層維護一條 WebSocket 連線與自己的內部執行緒，
        這些執行緒不是我們自己 threading.Thread(daemon=True) 開出來的，
        我們無法保證它們是 daemon thread；如果從不登出就直接關視窗，這些
        執行緒可能會讓整個 Python 行程卡住不結束，導致終端機視窗關閉後
        「跳不回命令提示字元」(使用者實測回報過這個現象；修好 ADR-012 的
        TclError 崩潰之後，行程不再意外崩潰退出，這個原本被崩潰順便蓋掉的
        問題才浮現出來)。

        最後呼叫 self.destroy() 關閉視窗，並且用 os._exit(0) 保底強制結束
        整個行程——因為就算呼叫了 logout()，我們仍然無法百分之百保證
        shioaji 內部所有執行緒都會在很短時間內自行結束 (那是我們看不到
        原始碼、無法控制的第三方套件行為)。os._exit() 會跳過 Python 正常
        的收尾機制 (atexit/緩衝區 flush 等)，但這裡可以接受：真正重要的
        收尾動作 (呼叫 shioaji logout) 已經在這之前明確做過了，犧牲的只是
        「乾淨結束」這個形式，換來的是「使用者關視窗後終端機一定會跳回
        提示字元」這個更重要的實際體驗保證。
        """
        self._closing = True
        self._qt_running = False  # 【ADR-035】關閉程式前先停自動交易,絕不在關閉過程下單
        # 【ADR-119】關閉保底看門狗:使用者回報「執行一陣子後按 X 沒反應」。
        # 這條路徑上每一步都已經有時限,但只要有任何一個第三方呼叫 (shioaji
        # logout、子行程 wait) 卡在不會回來的地方,整個關閉就停在那裡,而使用者
        # 只能去工作管理員砍。
        #
        # 這個執行緒不管主流程走到哪裡,時間到就強制結束行程。它是**保底**,
        # 不是正常路徑 —— 正常情況下面的 os._exit(0) 會先執行到,看門狗根本
        # 不會醒來 (daemon thread 隨行程一起消滅)。
        def _force_exit():
            time.sleep(self.CLOSE_HARD_DEADLINE)
            os._exit(0)
        if self.CLOSE_FORCE_EXIT:
            try:
                threading.Thread(target=_force_exit, daemon=True).start()
            except Exception:
                pass
        if HAS_SJ and self.api_logged_in:
            # 【第十輪修正 問題4】logout 原本在主執行緒同步呼叫:session 卡死/
            # 網路異常時 logout 會永遠不回來,視窗直接「沒有回應」、連關都關
            # 不掉。改成背景執行緒跑 logout,最多等 3 秒;沒等到就放行——
            # 反正下一步 os._exit(0) 會強制結束整個行程,shioaji 連線隨行程
            # 消滅,券商端 session 由伺服器逾時回收 (重登流程 ADR-026 也會
            # 先 logout 舊連線,不會被殭屍 session 卡住)。
            try:
                t = threading.Thread(target=lambda: self.brokers['sinopac'].logout(), daemon=True)
                t.start()
                t.join(timeout=3.0)
            except Exception:
                pass
        # 【ADR-112】券商子行程 (凱基) 必須明確收掉。它是獨立行程,不會隨
        # os._exit(0) 一起死 —— 留著會變成孤兒行程,還握著券商 session,
        # 下次啟動可能因此登不進去。同樣丟背景執行緒 + 限時,不讓它拖住關閉。
        for _bk, _b in (getattr(self, 'brokers', None) or {}).items():
            _stop = getattr(_b, '_stop', None)
            if _stop is None:
                continue
            try:
                t = threading.Thread(target=_stop, daemon=True)
                t.start()
                t.join(timeout=3.0)
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)

    def safe_after(self, delay, func, *args):
        """
        【ADR-012】取代所有直接呼叫 self.after(...) 的地方。
        背景 daemon thread (fetch_market_indices_worker/fetch_realtime_worker
        等) 在視窗已經關閉或正在關閉時，仍可能想排入 GUI 更新；這裡做兩層防護：
          1. 排程前檢查 self._closing，是的話直接不排程。
          2. 排程的 callback 真正執行時，再檢查一次 self._closing，並把
             實際呼叫包在 try/except 裡——因為單靠第 1 層仍有極窄
             的競態窗口 (排程當下 _closing 還是 False，但真正執行前視窗
             已經被關閉/銷毀)。
        兩層都做，才能確保背景執行緒不會在使用者關閉視窗後讓程式印出
        'invalid command name' 這類未捕捉例外。

        【ADR-056 修正】原本只接 tk.TclError,但「參數最佳化」跑很久 (數百組
        參數 × 完整回測) 期間使用者把整個程式關掉時,mainloop 已經停止,
        背景執行緒排程 self.after() 這一步 tkinter 拋的是
        RuntimeError('main thread is not in main loop'),不是 TclError,
        原本的 except 接不住 → 例外直接讓背景執行緒崩潰、進而把整個程式
        帶走。現在排程呼叫與回呼執行兩處都改接 (TclError, RuntimeError)。
        （Tcl 執行環境本身在極端競態下仍可能印出
        'Tcl_AsyncDelete: async handler deleted by the wrong thread' 這行
        訊息到主控台——這是 Tcl C 執行期的訊息,Python 這層攔不到,但只要
        我們自己不再排程新的 after(),機率已降到最低；長時間背景工作也應
        頻繁檢查 _closing 提早結束,見 P-59。）

        【使用者調整#1 延伸】回傳底層 tkinter after() 的排程 id (取不到就回傳
        None)，讓呼叫端 (例如 _on_chart_frame_resize 的 debounce 邏輯) 可以
        搭配 self.after_cancel(id) 取消還沒執行的排程。
        """
        if self._closing:
            return None
        def _wrapped(*a):
            if self._closing:
                return
            try:
                func(*a)
            except (tk.TclError, RuntimeError):
                pass
        try:
            return tk.Tk.after(self, delay, _wrapped, *args)
        except (tk.TclError, RuntimeError):
            return None

    def setup_styles(self):
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure('.', background='#12161A', foreground='#FFFFFF')
        self.style.configure('TButton', background='#1E242B', foreground='#FFFFFF', borderwidth=0, font=('微軟正黑體', 9, 'bold'))
        self.style.map('TButton', background=[('active', '#2A323D')])
        self.style.configure('BlackText.TCombobox', foreground='black', fieldbackground='white', background='white', font=('Arial', 9))
        self.style.map('BlackText.TCombobox', fieldbackground=[('readonly','white')], foreground=[('readonly','black')])
        self.option_add('*TCombobox*Listbox.foreground', 'black')
        self.option_add('*TCombobox*Listbox.background', 'white')
        # 【第七輪修正:委託單看不到列的根因 — ttk Treeview 資料列樣式】
        # 前幾輪把資料層 (seed / 回報 / refresh) 全部修對、日誌也證明「已加入清單」
        # 與「委託回報 已委託」都有進來、且完全沒有例外,但「我的委託單」分頁
        # 仍空白。根因是 clam 主題下,只設 '.' 的 foreground 只讓「標題列」有色
        # (所以標題看得到),但「資料列」的文字色/背景色/列高必須針對 Treeview
        # 這個 style 明確設定,否則資料列會以預設 (可能與底色相近或列高不足)
        # 渲染,看起來就像「明明插了列卻什麼都沒有」。這裡建立專用的
        # 'Trades.Treeview' style,明確指定資料列前景白、背景深、列高、字型,
        # 並設定選取色;委託單與已成交兩個 Treeview 都套用它。
        self.style.configure('Trades.Treeview',
                             background='#12161A', fieldbackground='#12161A',
                             foreground='#FFFFFF', rowheight=24,
                             font=('微軟正黑體', 9), borderwidth=0)
        self.style.map('Trades.Treeview',
                       background=[('selected', '#29B6F6')],
                       foreground=[('selected', 'black')])
        self.style.configure('Trades.Treeview.Heading',
                             background='#1E242B', foreground='#FFFFFF',
                             font=('微軟正黑體', 9, 'bold'))
        self.style.map('Trades.Treeview.Heading', background=[('active', '#2A323D')])

    def center_window(self, win, width, height):
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - (width // 2)
        y = self.winfo_y() + (self.winfo_height() // 2) - (height // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")

    def _make_scrollable(self, parent, bg="#1A2026"):
        """【新增】建立一個可垂直捲動的容器,回傳「內容要放進去的 Frame」。
        用途:編輯器類對話框內容會隨功能增加越疊越高,若都直接塞進固定
        高度的視窗,超出視窗高度的部分會被擠壓甚至跟底部固定的按鈕列
        (儲存/取消) 疊在一起看不到——這裡讓「中間內容」可以用滑鼠滾輪/
        捲軸捲動,底部固定的按鈕列永遠不會被擠出視窗外。呼叫端要先把
        底部固定的按鈕列 (foot/lbl_status 之類) 用 side=BOTTOM pack 好,
        再呼叫這個函式建立可捲動區塊 (內部用 side=TOP, fill=BOTH,
        expand=True 填滿剩餘空間),回傳的 Frame 才把其餘內容當作父容器
        塞進去——pack 呼叫順序必須底部固定的先、可捲動的後,底部才能
        真的固定佔住空間,不會被 expand=True 的捲動區塊擠壓。"""
        outer = tk.Frame(parent, bg=bg)
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = tk.Frame(canvas, bg=bg)
        win_id = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _on_inner_configure(_e=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass
        inner.bind("<Configure>", _on_inner_configure)

        def _on_canvas_configure(e):
            try:
                canvas.itemconfig(win_id, width=e.width)
            except Exception:
                pass
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(e):
            try:
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        # 滑鼠移進這個捲動區域才綁全域滾輪事件,移出就解綁——不然開著這個
        # 視窗時,滾輪會連主視窗或其他視窗的內容都一起捲動。
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def load_config(self):
        # 【ADR-009】檔案 I/O 移到 data/config_store.py。
        return config_store.load_broker_config(self.config_file)

    def save_config(self, api_key, secret_key, pid, ca_path):
        config_store.save_broker_config(self.config_file, api_key, secret_key, pid, ca_path)

    def load_watchlists(self):
        self.watchlists = config_store.load_watchlists(self.wl_file)

    def save_watchlists(self):
        config_store.save_watchlists(self.wl_file, self.watchlists)

    def get_tick(self, price):
        # 【ADR-009】實際規則移到 core/tick_rules.py,這裡只是把 self 的狀態轉成參數傳進去。
        return tick_rules.get_tick(price, self.asset_type, self.current_symbol)

    def _insert_decimal_point_workaround(self, event):
        """
        【使用者回報bug#1】NumLock 關閉時數字鍵盤小數點鍵送出 KP_Delete 而非
        句點字元，這裡手動在目前游標位置插入 "."，並回傳 "break" 阻止
        Entry 預設的刪除行為繼續執行 (不然會變成插入句點的同時還多刪一個字元)。
        """
        widget = event.widget
        try:
            if widget.selection_present():
                widget.delete("sel.first", "sel.last")
        except Exception:
            pass
        widget.insert("insert", ".")
        return "break"

    def _insert_decimal_point_button(self):
        """
        【使用者回報bug#1，第二次仍然發生】不再猜測使用者機器上數字鍵盤小數點鍵
        送出的確切 keysym 是什麼，直接提供一顆按鈕當保證一定有效的替代輸入方式:
        不管鍵盤事件細節為何，點這顆按鈕都能在目前游標位置插入句點。
        """
        widget = self.entry_price
        try:
            if str(widget['state']) == 'disabled':
                return
            if widget.selection_present():
                widget.delete("sel.first", "sel.last")
        except Exception:
            pass
        widget.insert("insert", ".")
        widget.focus_set()

    # 全形→半形對照表:中文輸入法啟用時句號鍵可能送出這些全形字元,一律視為小數點。
    _FULLWIDTH_DOT_MAP = str.maketrans({
        "。": ".",   # U+3002 表意句號 (最常見:中文輸入法直接打句號鍵)
        "．": ".",   # U+FF0E 全形句號
        "﹒": ".",   # U+FE52 小型句號
        "·": ".",    # U+00B7 間隔號 (部分輸入法)
    })

    def _normalize_decimal_realtime(self, event=None):
        """
        【第五輪修正:小數點輸入法問題】打字當下 (KeyRelease) 就把價格欄位裡的
        全形句號/全形小數點轉成半形「.」,讓中文輸入法狀態下也能正常輸入小數點,
        不必切換成英文輸入法。只在內容真的含有全形字元時才改寫欄位,並盡量
        保留游標位置,避免每次按鍵都重寫造成游標亂跳。盤後定價模式 (欄位
        disabled) 不處理。
        """
        try:
            if str(self.entry_price['state']) == 'disabled':
                return
            raw = self.entry_price.get()
            fixed = raw.translate(self._FULLWIDTH_DOT_MAP)
            if fixed != raw:
                try:
                    cursor = self.entry_price.index("insert")
                except Exception:
                    cursor = len(fixed)
                self.entry_price.delete(0, tk.END)
                self.entry_price.insert(0, fixed)
                try:
                    self.entry_price.icursor(cursor)
                except Exception:
                    pass
        except Exception:
            pass

    def _round_price_entry_to_tick(self, event=None):
        """
        【使用者調整#4】使用者手動輸入的價格如果沒有對齊合法的 tick 跳動單位，
        離開輸入框時自動四捨五入修正成最接近的合法價位。盤後定價模式下
        entry_price 是 disabled (價格鎖定收盤價)，不需要處理；限價以外
        (市價) 或欄位是空的也不處理。
        """
        try:
            if str(self.entry_price['state']) == 'disabled':
                return
            if self.cb_order_type.get() == "市價":
                return
            raw = self.entry_price.get().strip().replace("。", ".").replace("．", ".")
            if not raw:
                return
            price = float(raw)
            rounded = tick_rules.round_to_tick(price, self.asset_type, self.current_symbol)
            if abs(rounded - price) > 1e-9:
                new_str = self.fmt_price(rounded)
                self.entry_price.delete(0, tk.END)
                self.entry_price.insert(0, new_str)
                self.log_message(f"【價格自動修正】輸入的 {price} 不符合跳動單位規則，已自動調整為 {new_str}。")
        except (ValueError, Exception):
            pass  # 輸入不是有效數字時不處理，讓後續下單驗證去擋

    def step_price(self, direction):
        try:
            if self.cb_order_type.get() == "市價": return 
            if str(self.entry_price['state']) == 'disabled': return  # 盤後定價模式:價格鎖定,不可調整
            current_p = float(self.entry_price.get().strip().replace("。", ".").replace("．", "."))
            tick = self.get_tick(current_p)
            new_p = round(current_p + (tick * direction), 2)
            if new_p < 0: new_p = 0
            
            new_tick = self.get_tick(new_p)
            if new_tick >= 1: formatted_p = f"{int(new_p)}"
            elif new_tick == 0.5 or new_tick == 0.1: formatted_p = f"{new_p:.1f}"
            else: formatted_p = f"{new_p:.2f}"
            
            self.entry_price.delete(0, tk.END)
            self.entry_price.insert(0, formatted_p)
        except ValueError: pass

    def step_qty(self, direction):
        # 【ADR-013】交易別為零股類 (盤中零股/盤後零股) 時,單位是股,上限 999;
        # 其餘 (整股/盤後定價) 單位是張,上限 499 (單筆委託上限,避免打錯數字誤送巨量委託)。
        try:
            current_q = int(self.entry_qty.get().strip())
        except ValueError:
            current_q = 1
        new_q = current_q + direction
        if self.trade_mode in ("IntradayOdd", "Odd"):
            new_q = max(1, min(999, new_q))
        else:
            new_q = max(1, min(499, new_q))
        self.entry_qty.delete(0, tk.END)
        self.entry_qty.insert(0, str(new_q))

    def toggle_lot_type(self, is_odd):
        self.is_odd_lot = is_odd
        if is_odd:
            self.btn_whole_lot.config(bg="#2A323D", fg="white")
            self.btn_odd_lot.config(bg="#29B6F6", fg="black")
        else:
            self.btn_whole_lot.config(bg="#29B6F6", fg="black")
            self.btn_odd_lot.config(bg="#2A323D", fg="white")
            
        self.clear_5_level_ui()
        self.odd_no_stream_warned = False
        self.last_fallback_snap_time = 0  # 允許切換後立即補一次快照
        self._refresh_trade_feed_ui(is_odd)  # 立即刷新成交明細,顯示對應的整股/零股群組
        self.log_message(f"【檢視切換】當前看板模式:{'盤中零股' if self.is_odd_lot else '一般整股'}")

    # ================= 下單面板 (ADR-008):交易別/種類/條件/當沖/取價/成交明細 =================
    def update_order_panel_for_asset_type(self):
        """
        期貨沒有零股/盤後定價/現股融資融券的概念,只有 限價/市價 + ROD/IOC/FOK。
        商品切換為期貨時,把「交易別」與「種類」整組鎖住,避免使用者誤以為期貨
        也能選零股或融資融券 (點了也不會生效,但畫面上不該讓人誤解)。
        """
        is_future = (self.asset_type == "future")
        if is_future:
            for btn in self.trade_mode_buttons.values(): btn.config(state="disabled")
            for btn in self.order_cond_buttons.values(): btn.config(state="disabled")
            self.chk_daytrade.config(state="disabled")
            self.lbl_qty_unit.config(text="口")
            # 條件(ROD/IOC/FOK)、限價/市價 對期貨仍適用,維持可操作
            for btn in self.order_type_buttons.values(): btn.config(state="normal")
            self.cb_order_type.config(state="readonly", values=["限價", "市價"])
        else:
            for btn in self.trade_mode_buttons.values(): btn.config(state="normal")
            # 種類/當沖/單位/條件的可用狀態交回 set_trade_mode 依目前交易別重新套用;
            # user_initiated=False 代表這是背景重新整理,不重置數量、不印日誌。
            self.set_trade_mode(self.trade_mode, user_initiated=False)

    def set_trade_mode(self, mode, user_initiated=True):
        """
        交易別切換:Common=整股 IntradayOdd=盤中零股 Fixing=盤後定價 Odd=盤後零股
        依查證過的交易所規則連動限制其他欄位,避免使用者組出送不出去或會被券商退單的委託。

        user_initiated=False 用於「換股/換週期後重新同步鎖定狀態」這種背景呼叫
        (見 update_order_panel_for_asset_type),此時不重置使用者已輸入的數量、
        也不印出交易別切換日誌,避免每次刷新報價都洗版跟蓋掉使用者輸入到一半的量。
        """
        self.trade_mode = mode
        labels = {"Common": "整股", "IntradayOdd": "盤中零股", "Fixing": "盤後定價", "Odd": "盤後零股"}
        for k, btn in self.trade_mode_buttons.items():
            if k == mode: btn.config(bg="#29B6F6", fg="black")
            else: btn.config(bg="#2A323D", fg="white")

        is_lot_restricted = mode in ("IntradayOdd", "Odd")  # 零股類:只能現股、限價、ROD
        is_fixing = (mode == "Fixing")

        # 種類 (現股/融資/融券):零股類強制現股並鎖住其餘兩個按鈕
        if is_lot_restricted:
            for k, btn in self.order_cond_buttons.items():
                btn.config(state=("normal" if k == "Cash" else "disabled"))
            self.set_order_cond("Cash")
        else:
            for k, btn in self.order_cond_buttons.items():
                btn.config(state="normal")
            self.set_order_cond(self.order_cond)

        # 條件 (ROD/IOC/FOK):只有整股能切換,其餘強制 ROD 並鎖住
        if mode == "Common":
            for k, btn in self.order_type_buttons.items(): btn.config(state="normal")
        else:
            for k, btn in self.order_type_buttons.items():
                btn.config(state=("normal" if k == "ROD" else "disabled"))
        self.set_order_type("ROD" if mode != "Common" else self.order_type_tif)

        # 限價/市價:零股與定價只能限價,整股才能選市價
        if mode == "Common":
            self.cb_order_type.config(state="readonly", values=["限價", "市價"])
        else:
            self.cb_order_type.set("限價")
            self.cb_order_type.config(state="disabled", values=["限價"])

        # 價格輸入:盤後定價的成交價鎖定為當日 (上午) 收盤價,不可自行輸入 (交易所規則)。
        # 【備註】盤後定價是否允許融資/融券搭配尚無查到明確禁止規定 (僅確認零股明確禁止),
        # 故種類欄在此模式下不強制鎖現股;若送單被券商退回,代表該帳戶/該檔不允許,
        # 請洽營業員確認,不要因為程式允許選擇就假設券商一定接受。
        if is_fixing:
            close_p = self.last_norm_close if self.last_norm_close > 0 else 0.0
            if close_p <= 0 and self.current_df is not None and not self.current_df.empty:
                try: close_p = float(self.current_df['Close'].iloc[-1])
                except Exception: close_p = 0.0
            self.entry_price.config(state="normal")
            self.entry_price.delete(0, tk.END)
            if close_p > 0: self.entry_price.insert(0, self.fmt_price(close_p))
            self.entry_price.config(state="disabled")
        else:
            self.entry_price.config(state="normal")

        # 單位:零股類是「股」(上限999),整股/定價是「張」
        self.lbl_qty_unit.config(text="股" if is_lot_restricted else "張")
        if user_initiated:
            self.entry_qty.delete(0, tk.END)
            self.entry_qty.insert(0, "1")

        # 當沖只有整股+現股+可現沖標的才能勾選
        self.update_daytrade_checkbox_state()

        # 五檔/成交明細看盤同步:零股類自動切到零股看盤板,整股/定價切回整股看盤板
        target_is_odd = is_lot_restricted
        if self.is_odd_lot != target_is_odd:
            self.toggle_lot_type(target_is_odd)

        if user_initiated:
            self.log_message(f"【交易別切換】{labels.get(mode, mode)}")

    def set_order_cond(self, cond):
        self.order_cond = cond
        for k, btn in self.order_cond_buttons.items():
            if str(btn['state']) == 'disabled': continue
            btn.config(bg=("#29B6F6" if k == cond else "#2A323D"), fg=("black" if k == cond else "white"))
        self.update_daytrade_checkbox_state()

    def set_order_type(self, t):
        self.order_type_tif = t
        for k, btn in self.order_type_buttons.items():
            if str(btn['state']) == 'disabled': continue
            btn.config(bg=("#29B6F6" if k == t else "#2A323D"), fg=("black" if k == t else "white"))

    def update_daytrade_checkbox_state(self):
        """
        【使用者調整#5】只負責「鎖定/解鎖」checkbox 本身，以及「不合格時強制清空」；
        合格時**不會**主動把 daytrade_var 設回 True——那個「合格時的預設打勾」是在
        換新標的當下 (fetch_data_worker 裡) 就決定好的一次性初始值。這裡如果合格時
        也順手把它設回 True，會導致使用者在同一檔股票內手動取消勾選後，只要切換
        交易別/種類等按鈕 (也會呼叫到這個函式) 就被悄悄改回勾選，使用者會覺得
        「怎麼勾不掉」，這不是我們要的行為。
        """
        can_daytrade = (self.trade_mode == "Common" and self.order_cond == "Cash" and self.current_day_trade)
        if can_daytrade:
            self.chk_daytrade.config(state="normal")
        else:
            self.daytrade_var.set(False)
            self.chk_daytrade.config(state="disabled")

    def update_daytrade_badge(self):
        if self.current_day_trade:
            self.lbl_daytrade_badge.config(text="✅ 可現股當沖", fg="#FFCA28")
        else:
            self.lbl_daytrade_badge.config(text="🚫 禁止現沖", fg="#8A99AD")
        self.update_daytrade_checkbox_state()

    def on_quick_price_select(self, event=None):
        # 取價快捷:漲停/跌停/平盤 來自合約欄位;最佳買/最佳賣 來自目前五檔;最新成交來自 tick 快取。
        if self.trade_mode == "Fixing":
            return  # 定價模式價格鎖定為收盤價,取價快捷不適用
        sel = self.cb_quick_price.get()
        price = None
        try:
            c = self.current_contract
            if sel == "漲停" and c is not None:
                price = float(getattr(c, 'limit_up', 0) or 0)
            elif sel == "跌停" and c is not None:
                price = float(getattr(c, 'limit_down', 0) or 0)
            elif sel == "平盤":
                price = self.current_reference_price
            elif sel == "最佳買":
                txt = self.lbl_bid_prices[0]['text']
                price = float(txt) if txt != "--" else None
            elif sel == "最佳賣":
                txt = self.lbl_ask_prices[0]['text']
                price = float(txt) if txt != "--" else None
            elif sel == "最新成交":
                price = self.last_odd_close if (self.is_odd_lot and self.last_odd_close > 0) else self.last_norm_close
        except Exception:
            price = None
        if price and price > 0:
            self.entry_price.delete(0, tk.END)
            self.entry_price.insert(0, self.fmt_price(price))
        else:
            self.log_message("【取價】目前無法取得此參考價,請確認商品已載入且有報價。")

    def on_ladder_price_click(self, side, i):
        # 點五檔價格直接帶入下單價格欄 (盤後定價模式下價格鎖定,不受影響)
        if self.trade_mode == "Fixing":
            return
        lbls = self.lbl_bid_prices if side == 'bid' else self.lbl_ask_prices
        txt = lbls[i]['text']
        if txt == "--": return
        try:
            price = float(txt)
            self.entry_price.delete(0, tk.END)
            self.entry_price.insert(0, self.fmt_price(price))
        except Exception: pass

    def _record_trade_tick(self, tick_close, tick_vol, is_odd):
        # 由 tick callback (背景執行緒) 呼叫,只做資料寫入,UI 更新透過 self.after 排回主執行緒。
        try:
            c_val = float(tick_close)
            if c_val <= 0: return
            ref = self.current_reference_price
            chg = c_val - ref if ref > 0 else 0.0
            now_str = datetime.now().strftime('%H:%M:%S')
            row = (now_str, c_val, chg, int(tick_vol or 0))
            with self.quote_lock:
                (self.trade_feed_odd if is_odd else self.trade_feed_normal).appendleft(row)
            self.safe_after(0, self._refresh_trade_feed_ui, is_odd)
        except Exception: pass

    def _refresh_trade_feed_ui(self, is_odd):
        try:
            if is_odd != self.is_odd_lot: return  # 非目前看盤群組,先不刷新畫面 (資料仍已存好)
            feed = list(self.trade_feed_odd if is_odd else self.trade_feed_normal)
            vol_unit = "股" if is_odd else "張"
            self.listbox_trade_feed.delete(0, tk.END)
            for now_str, price, chg, vol in feed:
                sign = "+" if chg > 0 else ("" if chg < 0 else " ")
                row_txt = f"{now_str}  {self.fmt_price(price)}  {sign}{chg:.2f}  {vol}{vol_unit}"
                self.listbox_trade_feed.insert(tk.END, row_txt)
                idx = self.listbox_trade_feed.size() - 1
                color = "#FF1744" if chg > 0 else ("#00E676" if chg < 0 else "white")  # 紅漲綠跌 (鐵則1)
                self.listbox_trade_feed.itemconfig(idx, fg=color)
        except Exception: pass

    def clear_5_level_ui(self):
        for i in range(5):
            self.lbl_bid_prices[i].config(text="--")
            self.lbl_bid_vols[i].config(text="--")
            self.lbl_ask_prices[i].config(text="--")
            self.lbl_ask_vols[i].config(text="--")

    def create_widgets(self):
        market_panel = tk.Frame(self, bg="#0D1115", height=35)
        market_panel.pack(fill=tk.X, side=tk.TOP)

        # 【ADR-011】券商 API 登入按鈕與狀態,從左側「實盤下單」面板
        # 移到頂部大盤指數列右側,常駐顯示、不受左側面板高度影響。
        self.lbl_api_status = tk.Label(market_panel, text="🔴 券商未連線", bg="#0D1115", fg="#FF5252", font=('微軟正黑體', 9, 'bold'))
        self.lbl_api_status.pack(side=tk.RIGHT, padx=(5, 15), pady=5)
        self.btn_login = tk.Button(market_panel, text="🔒 登入券商實盤 API", bg="#FF9100", fg="black", font=("微軟正黑體", 9, "bold"), relief="flat", command=self.toggle_login)
        self.btn_login.pack(side=tk.RIGHT, padx=5, pady=5)
        # 【ADR-071】斷線自動重連開關:勾選後,偵測到券商連線中斷會自動用「本次
        # 登入時輸入的憑證」在背景重試登入 (退避間隔),交易時段內尤其重要,讓
        # 「早上開一次、整天不用管」成立。憑證密碼只留在記憶體、不寫進磁碟。
        # 開關的初始值從 app_settings 還原 (上次勾選的偏好會被記住)。
        self.auto_reconnect_var = tk.BooleanVar(value=bool(self.app_settings.get('auto_reconnect', False)))
        self.chk_auto_reconnect = tk.Checkbutton(
            market_panel, text="🔄 斷線自動重連", variable=self.auto_reconnect_var,
            bg="#0D1115", fg=("#00E676" if self.auto_reconnect_var.get() else "#8A99AD"),
            selectcolor="#2A323D", activebackground="#0D1115",
            font=('微軟正黑體', 9), command=self._on_auto_reconnect_toggle)
        self.chk_auto_reconnect.pack(side=tk.RIGHT, padx=5, pady=5)
        # 【ADR-073】記住憑證並開機自動登入 (加密存本機)。勾選後登入成功會把憑證
        # 加密存檔,下次開程式自動登入;取消即刪檔。誠實提醒:裝置金鑰加密,擋
        # 檔案外流但擋不了能完整存取本機本帳號的人。
        self.remember_creds_var = tk.BooleanVar(value=bool(self.app_settings.get('remember_creds', False)))
        self.chk_remember_creds = tk.Checkbutton(
            market_panel, text="🔐 記住憑證(自動登入)", variable=self.remember_creds_var,
            bg="#0D1115", fg=("#00E676" if self.remember_creds_var.get() else "#8A99AD"),
            selectcolor="#2A323D", activebackground="#0D1115",
            font=('微軟正黑體', 9), command=self._on_remember_creds_toggle)
        self.chk_remember_creds.pack(side=tk.RIGHT, padx=5, pady=5)
        # 【ADR-072】盤中零股開盤時刻選擇 (預設 09:10;未來交易所改 09:00 自己切)。
        tk.Label(market_panel, text="零股開盤", bg="#0D1115", fg="#8A99AD",
                 font=('微軟正黑體', 9)).pack(side=tk.RIGHT, padx=(8, 2), pady=5)
        self.odd_open_var = tk.StringVar(value=self.app_settings.get('odd_lot_open', '09:10'))
        self.cb_odd_open = ttk.Combobox(market_panel, values=['09:10', '09:00'], width=6,
                                        state='readonly', style="BlackText.TCombobox",
                                        textvariable=self.odd_open_var)
        self.cb_odd_open.pack(side=tk.RIGHT, padx=(0, 4), pady=5)
        self.cb_odd_open.bind('<<ComboboxSelected>>', self._on_odd_open_changed)

        self.lbl_twii = tk.Label(market_panel, text="加權指數: 等待連線API...", bg="#0D1115", fg="#FFCA28", font=('微軟正黑體', 10, 'bold'), cursor="hand2")
        self.lbl_twii.pack(side=tk.LEFT, padx=15, pady=5)
        self.lbl_twii.bind("<Button-1>", lambda e: self.load_index_chart("^TWII"))
        self.lbl_twoii = tk.Label(market_panel, text="櫃買指數: 等待連線API...", bg="#0D1115", fg="#FFCA28", font=('微軟正黑體', 10, 'bold'), cursor="hand2")
        self.lbl_twoii.pack(side=tk.LEFT, padx=15, pady=5)
        self.lbl_twoii.bind("<Button-1>", lambda e: self.load_index_chart("^TWOII"))

        top_panel = tk.Frame(self, bg="#1A2026", height=45)
        top_panel.pack(fill=tk.X, side=tk.TOP, padx=5, pady=5)
        tk.Label(top_panel, text="股票代碼:", bg="#1A2026", fg="white", font=('微軟正黑體', 10)).pack(side=tk.LEFT, padx=5)
        self.entry_symbol = tk.Entry(top_panel, bg="#2A323D", fg="#FFFFFF", insertbackground="white", width=12)
        self.entry_symbol.insert(0, regime_panel.DEFAULT_INDEX_SYMBOL)  # 【ADR-120】預設加權指數
        self.entry_symbol.pack(side=tk.LEFT, padx=5)
        self.entry_symbol.bind("<Return>", lambda e: self.start_fetch_thread())
        ttk.Button(top_panel, text="查尋 / 載入", command=self.start_fetch_thread).pack(side=tk.LEFT, padx=10)
        # 【ADR-028】市場切換:期貨英文代號 (如 ZEF、CDF) 過去會被誤判成美股,
        # 使用者明確指定市場後,系統不再用「有沒有數字」猜。台股模式仍保留
        # TXF/MXF 等舊代號的自動判斷,向下相容。
        self.market_mode = tk.StringVar(value="台股")
        tk.Label(top_panel, text="市場:", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(side=tk.LEFT, padx=(8, 2))
        for m in ("台股", "台期貨", "美股"):
            tk.Radiobutton(top_panel, text=m, variable=self.market_mode, value=m,
                           indicatoron=0, width=6, bg="#2A323D", fg="white",
                           selectcolor="#29B6F6", relief="flat",
                           font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=1)
        tk.Label(top_panel, text="(台股:代碼/^TWII | 台期貨:TXF/MXF等英文代號 | 美股:代碼)", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=5)

        self.main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.main_pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 【使用者調整】自選股 Treeview 的 5 欄寬度 (56+76+62+52+54=300px) 加上
        # LabelFrame 內距 (padx=5*2) 本來就超過舊預設值 240px,導致「名稱」欄
        # 這類文字被自動壓縮/截斷,使用者得手動拖曳 PanedWindow 分隔線才看得到
        # 完整內容——但拖曳分隔線本身會觸發圖表持續重繪 (ADR-036 已知效能問題),
        # 越拉越卡。改成預設值直接夠寬,不需要使用者手動拖曳:這是啟動時一次性
        # 決定的靜態寬度,跟拖曳當下的重繪節流是兩件事,不會有 ADR-036 的效能疑慮。
        left_frame = tk.Frame(self.main_pane, bg="#12161A", width=330)
        left_frame.pack_propagate(False)
        self.main_pane.add(left_frame, weight=0) 

        wl_box = tk.LabelFrame(left_frame, text=" 多群組自選股 ", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 10, 'bold'), padx=5, pady=3)
        wl_box.pack(fill=tk.X, side=tk.TOP, pady=3)
        wl_top = tk.Frame(wl_box, bg="#1A2026")
        wl_top.pack(fill=tk.X, pady=2)
        self.cb_wl = ttk.Combobox(wl_top, textvariable=self.current_wl_name, values=list(self.watchlists.keys()), width=13, state="readonly", style="BlackText.TCombobox")
        self.cb_wl.pack(side=tk.LEFT, padx=1)
        self.cb_wl.bind("<<ComboboxSelected>>", self.on_wl_change)
        tk.Button(wl_top, text="+群組", bg="#2A323D", fg="white", relief="flat", command=self.add_watchlist_group).pack(side=tk.LEFT, padx=1)
        tk.Button(wl_top, text="✎名", bg="#2A323D", fg="white", relief="flat", command=self.rename_watchlist_group).pack(side=tk.LEFT, padx=1)
        
        # 【ADR-028】自選股從純代碼 Listbox 升級為含即時報價的 Treeview:
        # 代碼 | 成交 | 漲跌 | 幅度%。報價由 watchlist_quote_worker 每 10 秒批次
        # snapshot 更新 (一次一批,符合 P-03 流量節流);收盤後 snapshot 回傳的
        # 就是最終收盤價,自然滿足「收盤後顯示最終報價」。紅漲綠跌 (鐵則1)。
        # 【第九輪 圖3需求 第5項】加「名稱」欄:台股/台期貨顯示中文名稱。
        wl_cols = ("sym", "name", "price", "chg", "pct")
        self.tree_wl = ttk.Treeview(wl_box, columns=wl_cols, show="headings", height=6, style='Trades.Treeview')
        for c, txt, w, anchor in (("sym", "代碼", 56, "center"), ("name", "名稱", 76, "center"),
                                    ("price", "成交", 62, "e"),
                                    ("chg", "漲跌", 52, "e"), ("pct", "幅度%", 54, "e")):
            self.tree_wl.heading(c, text=txt)
            self.tree_wl.column(c, width=w, anchor=anchor, stretch=True)
        self.tree_wl.tag_configure('wl_up', foreground='#FF1744', background='#12161A')
        self.tree_wl.tag_configure('wl_down', foreground='#00E676', background='#12161A')
        self.tree_wl.tag_configure('wl_flat', foreground='#FFFFFF', background='#12161A')
        self.tree_wl.pack(fill=tk.X, pady=2)
        self.tree_wl.bind("<<TreeviewSelect>>", self.on_watchlist_select)
        self.tree_wl.bind("<Delete>", lambda e: self.del_from_wl())
        
        wl_sort_frame = tk.Frame(wl_box, bg="#1A2026")
        wl_sort_frame.pack(fill=tk.X, pady=1)
        tk.Button(wl_sort_frame, text="↑ 上移", bg="#2A323D", fg="white", relief="flat", command=self.move_wl_up).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        tk.Button(wl_sort_frame, text="↓ 下移", bg="#2A323D", fg="white", relief="flat", command=self.move_wl_down).pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=1)

        wl_bot = tk.Frame(wl_box, bg="#1A2026")
        wl_bot.pack(fill=tk.X, pady=2)
        tk.Button(wl_bot, text="加入當前", bg="#00E676", fg="black", relief="flat", command=self.add_to_wl).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        tk.Button(wl_bot, text="刪除選取", bg="#FF5252", fg="white", relief="flat", command=self.del_from_wl).pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=1)
        self.on_wl_change(None) 

        info_box = tk.LabelFrame(left_frame, text=" 實盤下單 ", bg="#1A2026", fg="#00E676", font=('微軟正黑體', 10, 'bold'), padx=5, pady=5)
        info_box.pack(fill=tk.X, side=tk.TOP, pady=3)
        # 【ADR-011】券商登入按鈕/狀態已移到頂部大盤指數列 (見 market_panel)。
        # FinMind 登入功能已整個移除:法人/資券籌碼資料原本唯一來源是 FinMind，
        # 台股資料改為一律使用 shioaji，不再需要這個登入。

        # --- 現沖/禁現沖 badge (ADR-008) ---
        self.lbl_daytrade_badge = tk.Label(info_box, text="現沖狀態:未知", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9, 'bold'))
        self.lbl_daytrade_badge.pack(anchor="w", pady=(0,4))

        # --- 交易別:整股 / 盤中零股 / 盤後定價 / 盤後零股 ---
        # 【使用者調整#4】原本 4 個按鈕擠在同一排,每個只分到約 1/4 寬度,
        # 「盤中零股」「盤後定價」「盤後零股」這些 4 字標籤在窄按鈕裡容易顯示
        # 不完整。改成 2x2 網格,每個按鈕拿到約 2 倍寬度。
        tk.Label(info_box, text="交易別:", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor="w")
        trade_mode_frame = tk.Frame(info_box, bg="#1A2026")
        trade_mode_frame.pack(fill=tk.X, pady=2)
        trade_mode_row1 = tk.Frame(trade_mode_frame, bg="#1A2026")
        trade_mode_row1.pack(fill=tk.X)
        trade_mode_row2 = tk.Frame(trade_mode_frame, bg="#1A2026")
        trade_mode_row2.pack(fill=tk.X, pady=(2, 0))
        self.trade_mode_buttons = {}
        for i, (key, label) in enumerate([("Common", "整股"), ("IntradayOdd", "盤中零股"), ("Fixing", "盤後定價"), ("Odd", "盤後零股")]):
            row = trade_mode_row1 if i < 2 else trade_mode_row2
            btn = tk.Button(row, text=label, font=('微軟正黑體', 8, 'bold'), relief="flat",
                             command=lambda k=key: self.set_trade_mode(k))
            btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
            self.trade_mode_buttons[key] = btn

        # --- 種類:現股 / 融資 / 融券 ---
        tk.Label(info_box, text="種類:", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).pack(anchor="w", pady=(4,0))
        order_cond_frame = tk.Frame(info_box, bg="#1A2026")
        order_cond_frame.pack(fill=tk.X, pady=2)
        self.order_cond_buttons = {}
        for key, label in [("Cash", "現股"), ("MarginTrading", "融資"), ("ShortSelling", "融券")]:
            btn = tk.Button(order_cond_frame, text=label, font=('微軟正黑體', 8, 'bold'), relief="flat",
                             command=lambda k=key: self.set_order_cond(k))
            btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
            self.order_cond_buttons[key] = btn

        # --- 條件:ROD / IOC / FOK ---
        tk.Label(info_box, text="條件:", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).pack(anchor="w", pady=(4,0))
        order_type_frame = tk.Frame(info_box, bg="#1A2026")
        order_type_frame.pack(fill=tk.X, pady=2)
        self.order_type_buttons = {}
        for key in ["ROD", "IOC", "FOK"]:
            btn = tk.Button(order_type_frame, text=key, font=('微軟正黑體', 8, 'bold'), relief="flat",
                             command=lambda k=key: self.set_order_type(k))
            btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
            self.order_type_buttons[key] = btn

        # --- 類別 (限價/市價) + 取價快捷 ---
        order_frame1 = tk.Frame(info_box, bg="#1A2026")
        order_frame1.pack(fill=tk.X, pady=(4,2))
        tk.Label(order_frame1, text="類別:", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        self.cb_order_type = ttk.Combobox(order_frame1, values=["限價", "市價"], width=4, state="readonly", style="BlackText.TCombobox")
        self.cb_order_type.set("限價")
        self.cb_order_type.pack(side=tk.LEFT, padx=1)
        tk.Label(order_frame1, text="取價:", bg="#1A2026", fg="white").pack(side=tk.LEFT, padx=(6,0))
        self.cb_quick_price = ttk.Combobox(order_frame1, values=["漲停", "平盤", "跌停", "最佳買", "最佳賣", "最新成交"], width=8, state="readonly", style="BlackText.TCombobox")
        self.cb_quick_price.pack(side=tk.LEFT, padx=1)
        self.cb_quick_price.bind("<<ComboboxSelected>>", self.on_quick_price_select)

        # --- 當沖 (現股當沖,先賣後買) ---
        order_frame_dt = tk.Frame(info_box, bg="#1A2026")
        order_frame_dt.pack(fill=tk.X, pady=2)
        self.chk_daytrade = tk.Checkbutton(order_frame_dt, text="現股當沖(先賣後買)", variable=self.daytrade_var, bg="#1A2026", fg="#FFCA28", selectcolor="#2A323D", font=('微軟正黑體', 8), state="disabled")
        self.chk_daytrade.pack(side=tk.LEFT)

        # --- 【使用者調整】價與量合併成同一列:價在左 (原本量在價之前,現在價調到最前面)、
        # 量調到右側，盤後定價模式仍會鎖定價格欄 (見 set_trade_mode)。
        qty_price_frame = tk.Frame(info_box, bg="#1A2026")
        qty_price_frame.pack(fill=tk.X, pady=2)

        tk.Label(qty_price_frame, text="價:", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        btn_minus = tk.Button(qty_price_frame, text="－", bg="#2A323D", fg="white", relief="flat", padx=2, pady=0, command=lambda: self.step_price(-1))
        btn_minus.pack(side=tk.LEFT, padx=1)
        self.entry_price = tk.Entry(qty_price_frame, width=6, bg="#2A323D", fg="white", justify="center")
        self.entry_price.pack(side=tk.LEFT, padx=1)
        # 【使用者回報bug#1，第二次仍然發生】Windows 數字鍵盤在 NumLock 關閉時，
        # 小數點鍵可能被系統當成 Delete 鍵送出 (keysym 變成 KP_Delete)。
        # 之前只綁 <KP_Delete> 一種 keysym，但不同 Windows 版本/鍵盤驅動在特定
        # locale 下，回報的 keysym 可能不是 KP_Delete (例如可能是不帶 KP_ 前綴的
        # 純 "Delete"，或其他變化)，導致綁定沒有命中、修法沒有真的生效。
        # 與其繼續猜測鍵盤在使用者機器上到底送出哪個 keysym，這次額外加一顆
        # 「.」按鈕當作保證一定有效的替代輸入方式，不管鍵盤事件細節為何，
        # 點這顆按鈕都能在游標位置插入句點。<KP_Delete> 綁定保留當作額外的
        # 便利 (如果剛好命中就直接生效)，但不再是唯一的解法。
        self.entry_price.bind("<KP_Delete>", self._insert_decimal_point_workaround)
        # 【第五輪修正:小數點輸入法問題根因】使用者實測發現真正原因是「輸入法
        # 不在英文模式時打不出小數點」——中文輸入法啟用時,句號鍵送出的是全形
        # 「。」而不是半形「.」。原本只在 FocusOut 才做全形轉半形,打字當下畫面
        # 顯示的還是錯的、也無法直接組成合法數字。這裡改成打字當下 (KeyRelease)
        # 就即時把全形句號/全形小數點/全形逗號轉成半形「.」,使用者完全不必切換
        # 輸入法、也不用每次都點「.」按鈕,打起來跟英文模式一樣順。
        self.entry_price.bind("<KeyRelease>", self._normalize_decimal_realtime)
        btn_decimal = tk.Button(qty_price_frame, text=".", bg="#2A323D", fg="#FFCA28", relief="flat",
                                  font=("微軟正黑體", 9, "bold"), padx=3, pady=0,
                                  command=self._insert_decimal_point_button)
        btn_decimal.pack(side=tk.LEFT, padx=(1, 0))
        # 【使用者調整#4】離開價格輸入框時，如果輸入的價格沒有對齊合法的 tick
        # 跳動單位，自動四捨五入修正成最接近的合法價位，不用等到送出委託
        # 才被拒絕，體驗上更即時。
        self.entry_price.bind("<FocusOut>", self._round_price_entry_to_tick)
        btn_plus = tk.Button(qty_price_frame, text="＋", bg="#2A323D", fg="white", relief="flat", padx=2, pady=0, command=lambda: self.step_price(1))
        btn_plus.pack(side=tk.LEFT, padx=1)

        # 【第九輪修正 圖1】「量」原本跟「價」擠同一列,左側面板窄一點,
        # 量的輸入框與單位文字 (張/股/口) 就被截掉看不到。拆成獨立一列,
        # 各自 fill=X,不論視窗多窄都完整可見。
        qty_row_frame = tk.Frame(info_box, bg="#1A2026")
        qty_row_frame.pack(fill=tk.X, pady=2)
        tk.Label(qty_row_frame, text="量:", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        btn_qty_minus = tk.Button(qty_row_frame, text="－", bg="#2A323D", fg="white", relief="flat", padx=2, pady=0, command=lambda: self.step_qty(-1))
        btn_qty_minus.pack(side=tk.LEFT, padx=1)
        self.entry_qty = tk.Entry(qty_row_frame, width=5, bg="#2A323D", fg="white", justify="center")
        self.entry_qty.insert(0, "1")
        self.entry_qty.pack(side=tk.LEFT, padx=1)
        btn_qty_plus = tk.Button(qty_row_frame, text="＋", bg="#2A323D", fg="white", relief="flat", padx=2, pady=0, command=lambda: self.step_qty(1))
        btn_qty_plus.pack(side=tk.LEFT, padx=1)
        self.lbl_qty_unit = tk.Label(qty_row_frame, text="張", bg="#1A2026", fg="#8A99AD")
        self.lbl_qty_unit.pack(side=tk.LEFT, padx=(4, 0))

        btn_frame = tk.Frame(info_box, bg="#1A2026")
        btn_frame.pack(fill=tk.X, pady=5)
        tk.Button(btn_frame, text="買進", bg="#FF1744", fg="white", font=("微軟正黑體", 10, "bold"), command=lambda: self.execute_order("買進")).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        tk.Button(btn_frame, text="賣出", bg="#00E676", fg="black", font=("微軟正黑體", 10, "bold"), command=lambda: self.execute_order("賣出")).pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=1)

        ttk.Separator(info_box, orient='horizontal').pack(fill=tk.X, pady=5)
        
        self.lbl_rt_quote = tk.Label(info_box, text="即時行情: 等待串流中...", bg="#1A2026", fg="#00E676", font=('微軟正黑體', 9, 'bold'),
                                       wraplength=220, justify="left", anchor="w")
        self.lbl_rt_quote.pack(anchor="w", pady=2, fill=tk.X)

        # --- 看盤模式切換 (只控制五檔/成交明細顯示整股或零股,與上面「交易別」下單選擇分開) ---
        view_toggle_frame = tk.Frame(info_box, bg="#1A2026")
        view_toggle_frame.pack(fill=tk.X, pady=2)
        tk.Label(view_toggle_frame, text="看盤:", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(side=tk.LEFT)
        self.btn_whole_lot = tk.Button(view_toggle_frame, text="整股", bg="#29B6F6", fg="black", relief="flat", font=("微軟正黑體", 8, "bold"), command=lambda: self.toggle_lot_type(False))
        self.btn_whole_lot.pack(side=tk.LEFT, padx=2)
        self.btn_odd_lot = tk.Button(view_toggle_frame, text="零股", bg="#2A323D", fg="white", relief="flat", font=("微軟正黑體", 8, "bold"), command=lambda: self.toggle_lot_type(True))
        self.btn_odd_lot.pack(side=tk.LEFT, padx=2)

        # --- Quote 串接五檔報價區 (價格可點擊直接帶入下單價,ADR-008 新增) ---
        # 【第十輪修正 問題2】五檔原本是左側面板「最後 pack」的元件——tkinter
        # 空間不足時,後 pack 的先被擠出視窗;第九輪把價/量拆成兩列後左側面板
        # 長高,五檔資料列就被擠到視窗外「不見了」(標題列剛好卡在邊緣)。
        # 修正:五檔改「先 pack + side=BOTTOM」,空間分配優先權最高、永遠貼底
        # 可見;成交明細改為空間不足時被壓縮的那一個 (它是滾動列表,矮一點
        # 只是少看幾筆,不影響功能)。視覺順序不變:成交明細在上、五檔在下。
        five_level_frame = tk.Frame(info_box, bg="#1A2026")
        five_level_frame.pack(side=tk.BOTTOM, pady=2)

        # --- 成交明細跳動列表 (ADR-008 新增;第十輪:height 5→4 取回加高的量列空間) ---
        tk.Label(info_box, text="成交明細:", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor="w", pady=(4,0))
        self.listbox_trade_feed = tk.Listbox(info_box, bg="#12161A", fg="white", height=4, font=('Courier New', 9), selectbackground="#2A323D", activestyle="none")
        self.listbox_trade_feed.pack(fill=tk.X, pady=2)
        
        self.lbl_bid_prices = []
        self.lbl_bid_vols = []
        self.lbl_ask_prices = []
        self.lbl_ask_vols = []

        tk.Label(five_level_frame, text="買量", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=0, column=0, padx=2)
        tk.Label(five_level_frame, text="買價", bg="#1A2026", fg="#FF1744", font=('微軟正黑體', 9)).grid(row=0, column=1, padx=2)
        tk.Label(five_level_frame, text="賣價", bg="#1A2026", fg="#00E676", font=('微軟正黑體', 9)).grid(row=0, column=2, padx=2)
        tk.Label(five_level_frame, text="賣量", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=0, column=3, padx=2)

        for i in range(5):
            bv = tk.Label(five_level_frame, text="--", bg="#1A2026", fg="white", font=('Arial', 9))
            bv.grid(row=i+1, column=0)
            bp = tk.Label(five_level_frame, text="--", bg="#1A2026", fg="#FF1744", font=('Arial', 9, 'bold'), cursor="hand2")
            bp.grid(row=i+1, column=1)
            bp.bind("<Button-1>", lambda e, idx=i: self.on_ladder_price_click('bid', idx))
            
            ap = tk.Label(five_level_frame, text="--", bg="#1A2026", fg="#00E676", font=('Arial', 9, 'bold'), cursor="hand2")
            ap.grid(row=i+1, column=2)
            ap.bind("<Button-1>", lambda e, idx=i: self.on_ladder_price_click('ask', idx))
            av = tk.Label(five_level_frame, text="--", bg="#1A2026", fg="white", font=('Arial', 9))
            av.grid(row=i+1, column=3)
            
            self.lbl_bid_vols.append(bv)
            self.lbl_bid_prices.append(bp)
            self.lbl_ask_prices.append(ap)
            self.lbl_ask_vols.append(av)

        # 【ADR-013】系統日誌與回報已從左側面板移到右側 K 線圖下方 (見 self.right_frame 建構區塊)。

        # =========== 右側圖表區 ===========
        self.right_frame = tk.Frame(self.main_pane, bg="#12161A")
        self.main_pane.add(self.right_frame, weight=1) 

        self.tf_frame = tk.Frame(self.right_frame, bg="#12161A")
        self.tf_frame.pack(fill=tk.X, pady=2)
        row_frame = tk.Frame(self.tf_frame, bg="#12161A")
        row_frame.pack(fill=tk.X, pady=2)
        
        self.tf_buttons = {}
        for tf in ["1分K", "5分K", "15分K", "30分K", "60分K", "日K", "周K", "月K"]:
            btn = tk.Button(row_frame, text=tf, font=('微軟正黑體', 9, 'bold'), bg="#1E242B", fg="white", relief="flat", cursor="hand2", padx=8, pady=2, command=lambda t=tf: self.set_timeframe(t))
            btn.pack(side=tk.LEFT, padx=1)
            self.tf_buttons[tf] = btn
        self.set_timeframe("日K", fetch=False)

        tk.Checkbutton(row_frame, text="還原權息", variable=self.var_adjusted, bg="#12161A", fg="#00E676", selectcolor="#2A323D", command=self.start_fetch_thread).pack(side=tk.LEFT, padx=(10,2))

        tk.Label(row_frame, text=" || 主圖:", bg="#12161A", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(side=tk.LEFT, padx=(5,2))
        tk.Button(row_frame, text="⚙ 均線/布林/黃金切割", bg="#2A323D", fg="white", relief="flat", command=self.open_main_settings).pack(side=tk.LEFT, padx=2)
        # 【ADR-133】黃金切割律 (費波南希回撤):勾了就畫,細部參數在上面的 ⚙。
        tk.Checkbutton(row_frame, text="黃金切割", variable=self.fib_show,
                       bg="#12161A", fg="#FFCA28", selectcolor="#2A323D",
                       command=self._fib_toggle).pack(side=tk.LEFT, padx=(4, 0))

        # 【ADR-134】副圖指標整合成一顆按鈕。原本工具列上一字排開 5 個勾選鈕
        # + 4 個 ⚙,佔掉整條工具列,而且要調參數得先認得哪顆 ⚙ 是哪個指標。
        # 使用者要求「全部整合到一個【副圖技術指標】中,要用哪一個就到這裡選」。
        # **只有搬位置,計算與繪圖邏輯一個字都沒改** (使用者明確交代)。
        tk.Label(row_frame, text=" || 副圖:", bg="#12161A", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(side=tk.LEFT, padx=(10,2))
        tk.Button(row_frame, text="⚙ 副圖技術指標", bg="#2A323D", fg="white", relief="flat",
                  command=self.open_sub_indicators).pack(side=tk.LEFT, padx=2)
        self.lbl_sub_active = tk.Label(row_frame, text="", bg="#12161A", fg="#00E676",
                                       font=('微軟正黑體', 8))
        self.lbl_sub_active.pack(side=tk.LEFT, padx=(4, 0))
        self._refresh_sub_active_label()

        # 【ADR-120】主圖【盤勢判斷】:把原本分散兩處的「盤勢/型態偵測」
        # (原本在終極波段策略編輯器裡) 與「量價支撐壓力」(ADR-102) 合成
        # 主圖上同一顆勾選鈕,底下兩個子項各自可勾選,細部參數在 ⚙ 對話框。
        # 勾選後:支撐壓力畫在主圖 (壓力紅、支撐綠,鐵則1 的延伸);盤勢/型態
        # 在加權指數日K 出現型態時自動寫進系統日誌通知。
        self.regime_enabled_var = tk.BooleanVar(value=bool(self.regime_settings['enabled']))
        self.sr_enabled_var = tk.BooleanVar(value=bool(self.regime_settings['sr_enabled']))
        self._sr_last_result = None
        tk.Checkbutton(row_frame, text="盤勢判斷", variable=self.regime_enabled_var,
                       bg="#12161A", fg="#FFCA28", selectcolor="#2A323D",
                       command=self._regime_toggle).pack(side=tk.LEFT, padx=(10, 0))
        tk.Button(row_frame, text="⚙", bg="#2A323D", fg="white", relief="flat",
                  padx=2, pady=0, command=self.open_regime_settings).pack(side=tk.LEFT)

        # 【使用者調整#1】圖表邊界前兩次都是 Claude 猜測數值，使用者要求改成
        # 可以自己調整、調好就鎖定保存。這顆按鈕開一個對話框，用滑桿即時
        # 調整邊界比例與畫布像素微調值，「套用」立即重繪、「儲存」寫入設定檔
        # 持久化保存，之後不用再靠猜的。
        tk.Button(row_frame, text="📐 版面微調", bg="#2A323D", fg="#FFCA28", relief="flat", command=self.open_chart_layout_dialog).pack(side=tk.RIGHT, padx=10)

        # 【ADR-049】期交所官方每日行情匯入:給期貨 R1 日/周/月K更長的歷史。
        tk.Button(row_frame, text="📥 期交所歷史", bg="#2A323D", fg="#29B6F6", relief="flat",
                  command=self.open_taifex_import_dialog).pack(side=tk.RIGHT, padx=(0, 2))
        # 【ADR-060】使用者要能直接回答「到底有沒有讀到期交所資料、放對地方沒」
        tk.Button(row_frame, text="🔎 期交所資料狀態", bg="#2A323D", fg="#00E676", relief="flat",
                  command=self.show_taifex_status).pack(side=tk.RIGHT, padx=(0, 2))

        # 【ADR-011】備用 YF 報價切換按鈕已移除:台股一律使用 shioaji，
        # 美股自動使用 yfinance (asset_type == "us_stock" 時)，不再需要手動切換。

        # 【ADR-013/使用者調整#5】系統日誌與回報移到這裡:K線圖下方,固定高度並附垂直卷軸,
        # 可以上下捲動看之前的訊息。用 side=tk.BOTTOM 先卡住底部固定高度的位置,
        # 之後 chart_frame 用 fill=tk.BOTH, expand=True 自動吃掉剩餘的中間空間。
        #
        # 【使用者調整#5】這裡改成分頁式,新增「我的委託單」「我的已成交」兩個分頁,
        # 跟「系統日誌與回報」共用同一塊底部空間 (用按鈕切換顯示哪一個),
        # 不需要再額外找地方塞新的表格,避免讓本來就已經很緊繃的版面更擁擠。
        log_outer = tk.Frame(self.right_frame, bg="#1A2026", height=170)
        log_outer.pack(side=tk.BOTTOM, fill=tk.X, pady=(3, 0))
        log_outer.pack_propagate(False)  # 固定高度,不被裡面的內容撐開或壓縮

        bottom_tab_bar = tk.Frame(log_outer, bg="#1A2026")
        bottom_tab_bar.pack(fill=tk.X, side=tk.TOP)
        self.bottom_tab_buttons = {}
        for key, label in [("log", "系統日誌與回報"), ("orders", "我的委託單"), ("fills", "我的已成交"), ("positions", "我的庫存"), ("quant", "量化交易"), ("chips", "籌碼"), ("screener", "選股")]:
            btn = tk.Button(bottom_tab_bar, text=label, font=('微軟正黑體', 9, 'bold'), relief="flat",
                             command=lambda k=key: self.set_bottom_tab(k))
            btn.pack(side=tk.LEFT, padx=(2, 0), pady=1)
            self.bottom_tab_buttons[key] = btn

        self.bottom_content_frame = tk.Frame(log_outer, bg="#1A2026")
        self.bottom_content_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

        # --- 分頁1:系統日誌與回報 (既有內容原封不動搬過來) ---
        self.log_tab_frame = tk.Frame(self.bottom_content_frame, bg="#1A2026")
        log_scrollbar = tk.Scrollbar(self.log_tab_frame, orient=tk.VERTICAL)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_txt = tk.Text(self.log_tab_frame, bg="#12161A", fg="#00FF00", font=('Courier New', 8), state=tk.DISABLED, yscrollcommand=log_scrollbar.set)
        self.log_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.config(command=self.log_txt.yview)

        # --- 分頁2:我的委託單 (Treeview 表格,依 shioaji set_order_callback 主動回報更新) ---
        self.orders_tab_frame = tk.Frame(self.bottom_content_frame, bg="#1A2026")
        # 【ADR-027:可發現性】刪改功能 (ADR-023) 原本只有「雙擊列」一個入口,
        # 介面上沒有任何可見提示,使用者根本不知道功能存在。加一條操作列:
        # 明顯的「刪改」按鈕 (選取列後點擊) + 雙擊提示文字,兩種入口共用同一個
        # 對話框。
        orders_toolbar = tk.Frame(self.orders_tab_frame, bg="#1A2026")
        orders_toolbar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 0))
        tk.Button(orders_toolbar, text="🛠 刪改選取委託 (刪單/改量/改價)", bg="#FB8C00", fg="black",
                  relief="flat", font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
                  command=self._on_modify_button_click).pack(side=tk.LEFT)
        tk.Label(orders_toolbar, text="💡 也可直接「雙擊」委託列開啟刪改視窗",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=10)
        # 【第九輪修正 圖2】加「交易別」欄:每筆委託顯示整股/盤中零股/盤後定價/
        # 盤後零股 (資料來源:委託的 order_lot,經 MODE_LABELS 轉中文)。
        orders_cols = ("time", "code", "lot", "action", "price", "quantity", "filled", "status")
        orders_headings = {"time": "時間", "code": "商品", "lot": "交易別", "action": "買賣",
                            "price": "價格", "quantity": "數量", "filled": "已成交", "status": "狀態"}
        self.tree_orders = ttk.Treeview(self.orders_tab_frame, columns=orders_cols, show="headings", height=5, style='Trades.Treeview')
        for c in orders_cols:
            self.tree_orders.heading(c, text=orders_headings[c])
            self.tree_orders.column(c, width=80, anchor="center")
        # 【第七輪】明確設定資料列前景色 tag,插入時逐列套用,雙保險確保列文字可見。
        self.tree_orders.tag_configure('visible_row', foreground='#FFFFFF', background='#12161A')
        # 【ADR-023】雙擊某列開啟「委託刪改」統一對話框 (刪單/改量/改價)。
        self.tree_orders.bind("<Double-1>", self._on_order_row_double_click)
        orders_scrollbar = tk.Scrollbar(self.orders_tab_frame, orient=tk.VERTICAL, command=self.tree_orders.yview)
        self.tree_orders.configure(yscrollcommand=orders_scrollbar.set)
        orders_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_orders.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- 分頁3:我的已成交 (Treeview 表格,依 shioaji set_order_callback 主動回報更新) ---
        self.fills_tab_frame = tk.Frame(self.bottom_content_frame, bg="#1A2026")
        fills_cols = ("time", "code", "action", "price", "quantity")
        fills_headings = {"time": "時間", "code": "商品", "action": "買賣", "price": "成交價", "quantity": "成交量"}
        self.tree_fills = ttk.Treeview(self.fills_tab_frame, columns=fills_cols, show="headings", height=5, style='Trades.Treeview')
        for c in fills_cols:
            self.tree_fills.heading(c, text=fills_headings[c])
            self.tree_fills.column(c, width=90, anchor="center")
        self.tree_fills.tag_configure('visible_row', foreground='#FFFFFF', background='#12161A')
        fills_scrollbar = tk.Scrollbar(self.fills_tab_frame, orient=tk.VERTICAL, command=self.tree_fills.yview)
        self.tree_fills.configure(yscrollcommand=fills_scrollbar.set)
        fills_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_fills.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- 分頁4:我的庫存 (【第十一輪 第2項】list_positions 按需查詢,不輪詢) ---
        self.positions_tab_frame = tk.Frame(self.bottom_content_frame, bg="#1A2026")
        pos_toolbar = tk.Frame(self.positions_tab_frame, bg="#1A2026")
        pos_toolbar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 0))
        tk.Button(pos_toolbar, text="🔄 更新庫存", bg="#29B6F6", fg="black",
                  relief="flat", font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
                  command=self.refresh_positions).pack(side=tk.LEFT)
        tk.Button(pos_toolbar, text="🔍 開啟完整明細視窗", bg="#FB8C00", fg="black",
                  relief="flat", font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
                  command=self._open_positions_detail_window).pack(side=tk.LEFT, padx=6)
        self.lbl_positions_summary = tk.Label(pos_toolbar, text="尚未查詢 (切到此分頁或按「更新庫存」)",
                                              bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9))
        self.lbl_positions_summary.pack(side=tk.LEFT, padx=10)
        pos_cols = ("acct", "code", "name", "direction", "qty", "avg", "last", "pnl", "ret")
        pos_headings = {"acct": "帳戶", "code": "代碼", "name": "名稱", "direction": "方向",
                         "qty": "庫存量", "avg": "均價", "last": "現價", "pnl": "未實現損益", "ret": "報酬率%"}
        self.tree_positions = ttk.Treeview(self.positions_tab_frame, columns=pos_cols, show="headings", height=5, style='Trades.Treeview')
        for c in pos_cols:
            self.tree_positions.heading(c, text=pos_headings[c])
            self.tree_positions.column(c, width=76, anchor="center")
        # 紅賺綠賠 (台股慣例:紅=正)
        self.tree_positions.tag_configure('pos_up', foreground='#FF1744', background='#12161A')
        self.tree_positions.tag_configure('pos_down', foreground='#00E676', background='#12161A')
        self.tree_positions.tag_configure('pos_flat', foreground='#FFFFFF', background='#12161A')
        pos_scrollbar = tk.Scrollbar(self.positions_tab_frame, orient=tk.VERTICAL, command=self.tree_positions.yview)
        self.tree_positions.configure(yscrollcommand=pos_scrollbar.set)
        pos_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_positions.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._positions_raw = []       # 完整原始欄位 (明細視窗用)
        self._positions_loading = False

        # --- 分頁5:量化交易 (【ADR-035】;【ADR-057】改為「精簡面板 + 獨立視窗」) ---
        # 使用者反映:底部分頁高度就那幾行,策略一多完全不夠看。量化交易的完整
        # 操作介面改到獨立視窗 (open_quant_window),底部分頁保留一份精簡面板
        # 當入口與狀態顯示 —— 不直接砍掉分頁,是因為總開關狀態 (🔴/🟢) 屬於
        # 安全資訊,即使沒開視窗也應該在主畫面看得到。
        self.quant_tab_frame = tk.Frame(self.bottom_content_frame, bg="#1A2026")
        self._qt_uis = []   # 所有存活中的量化面板 (分頁一份、獨立視窗一份)
        self._quant_win = None
        self._build_quant_panel(self.quant_tab_frame, tree_height=4, compact=True)

        # --- 分頁6:籌碼 (ADR-100) ---
        self.chips_tab_frame = tk.Frame(self.bottom_content_frame, bg="#1A2026")
        self._build_chips_panel(self.chips_tab_frame)

        # --- 分頁7:選股 (ADR-103) ---
        self.screener_tab_frame = tk.Frame(self.bottom_content_frame, bg="#1A2026")
        self._build_screener_panel(self.screener_tab_frame)

        self.bottom_tab = "log"
        self.set_bottom_tab("log")
        # 【ADR-057】自動 GC 已關閉,啟動主執行緒定期回收 (見 __init__ 註解)。
        self.safe_after(self._gc_interval_ms, self._gc_tick)

        self.lbl_hover_info = tk.Label(self.right_frame, text="滑鼠游標移至 K 線圖上方以顯示詳細資訊...", bg="#12161A", fg="#29B6F6", font=('微軟正黑體', 11, 'bold'), anchor="w")
        self.lbl_hover_info.pack(fill=tk.X, pady=2)

        self.chart_frame = tk.Frame(self.right_frame, bg="#12161A")
        self.chart_frame.pack(fill=tk.BOTH, expand=True)
        # 【使用者調整#1 延伸】視窗縮放時,chart_frame 的實際像素尺寸會跟著變，
        # 綁定 <Configure> 讓圖表也跟著重繪、套用新的 figsize，而不是只有第一次
        # 載入時才會用到正確尺寸。用 _resize_after_id 做 debounce (拖曳視窗邊框時
        # <Configure> 會連續觸發很多次)，取消前一個排程、只在使用者停止拖曳
        # 300ms 後才真的重繪一次，避免拖曳過程中因為頻繁重繪 mplfinance 圖表
        # 造成明顯頓卡。
        self._resize_after_id = None
        # 【ADR-036 效能】拖曳左側自選欄的 PanedWindow 分隔線 (sash) 時,
        # chart_frame 的 <Configure> 會連續觸發;舊版 300ms debounce 只能合併
        # 「連續快速」的事件,使用者拖一下→停一下→再拖,每次停頓超過 300ms
        # 就完整重繪一次 mplfinance 圖 (系統日誌連續出現「[日K] 載入成功」),
        # 非常耗資源。修正分兩層:
        #   (1) sash 拖曳期間 (滑鼠按住 PanedWindow 本體=分隔線) 完全不排程
        #       重繪,只記 pending;放開滑鼠才做「一次」重繪。
        #   (2) 重繪前比對 chart_frame 目前尺寸與上一次實際繪製時的尺寸,
        #       沒有實質變化 (≤2px) 就直接跳過,杜絕無意義的完整重繪。
        # 注意:ButtonPress/Release 綁在 main_pane 上只會在點到 PanedWindow
        # 本體 (也就是 sash) 時觸發,點到左右子框架內的元件不會冒泡上來,
        # 因此不會誤傷其他互動。
        self._sash_dragging = False
        self._resize_pending = False
        self._last_drawn_chart_size = None  # (w, h) 上一次 draw_chart 實際使用的像素尺寸
        self.main_pane.bind("<ButtonPress-1>", self._on_pane_sash_press, add="+")
        self.main_pane.bind("<ButtonRelease-1>", self._on_pane_sash_release, add="+")
        self.chart_frame.bind("<Configure>", self._on_chart_frame_resize)

        # 【修正】面板初始狀態:預設整股模式,套用一次連動邏輯 (種類/條件/單位/當沖等)。
        # 這行必須放在 create_widgets() 最後——set_trade_mode() 內部會呼叫
        # log_message()，而 log_message() 需要 self.log_txt 已經建立好；
        # 之前誤放在 log_txt 建立之前，導致一啟動就 AttributeError，
        # 後面的系統日誌區塊與整個右側K線圖都沒機會建起來 (使用者回報的
        # 「畫面只剩下單面板」正是這個原因)。
        self.set_trade_mode("Common")

    # ================= 視窗與券商登入 =================
    def _looks_like_session_dead(self, exc):
        """
        【使用者回報#3 / 第八輪修正】判斷例外像不像「shioaji 連線階段已斷」。
        已知情境:同一帳號在官網/App 也登入會佔用交易階段連線,API 端後續呼叫
        會出現 'SessionNotEstablished' 等字樣。
        【第八輪修正】舊關鍵字包含泛用的 "session"/"NotReady"/"not ready"/
        "connection error",太寬鬆——登入後合約還沒就緒等暫時性錯誤也被誤判成
        斷線,api_logged_in 被撥回 False,使用者陷入「一直登不進去」的循環。
        收斂為只認明確的斷線訊息。
        """
        msg = str(exc).lower()
        keywords = ["sessionnotestablished", "session error", "session expired",
                    "session not established", "token expired", "connection lost",
                    "disconnected",
                    # 【ADR-060】使用者實測:登出後背景分段下載仍逐段重試,整批
                    # 21 段全部噴 "AuthError: Not authenticated" 才收工 (畫面被洗版
                    # 且白白打了幾十次 API)。未登入/未授權在語意上就是「這條連線
                    # 已經不能用了」,必須立刻中止整批下載,而不是一段一段重試。
                    "not authenticated", "autherror", "unauthorized",
                    "not login", "not logged in", "please login"]
        return any(k in msg for k in keywords)

    def _downloads_should_abort(self):
        """【ADR-060】背景下載是否該立刻停手。
        任一成立就停:程式正在關閉、已登出 (api_logged_in=False)、
        使用者按了強制終止。分段下載每段開始前都會檢查。"""
        if getattr(self, '_closing', False):
            return True
        if not getattr(self, 'api_logged_in', False):
            return True
        if getattr(self, '_backtest_cancel', False):
            return True
        return False

    def _mark_session_dead(self, reason=""):
        """
        【使用者回報#3 / 第八輪修正】偵測到連線階段疑似斷線時:把 api_logged_in
        撥回 False、更新畫面,並【第八輪新增】背景對舊連線做一次 best-effort
        logout()——這一步是解開死循環的關鍵:券商端的舊 session 若不主動釋放,
        會一直佔著「同一帳號同時間只能一個生效」的名額,使用者不論怎麼重新
        登入都會被拒絕,直到重開整個程式或等券商端逾時。
        """
        if not self.api_logged_in:
            return  # 已經是斷線狀態，不用重複處理
        self.api_logged_in = False
        # 背景釋放舊 session (可能會 block 或再丟例外,放 daemon thread 且全吞)
        def _release_old():
            try:
                self.sj_api.logout()
            except Exception:
                pass
        try:
            threading.Thread(target=_release_old, daemon=True).start()
        except Exception:
            pass
        self.safe_after(0, lambda: self.lbl_api_status.config(text="🔴 連線中斷 (請重新登入)", fg="#FF5252"))
        self.safe_after(0, lambda: self.btn_login.config(text="🔒 登入券商實盤 API", bg="#FF9100", fg="black"))
        self.safe_after(0, self.log_message,
                         "【連線中斷】偵測到券商連線階段疑似已斷線。最常見原因：同一組帳號同時"
                         "在永豐金官網或 App 登入 (許多券商規定同一帳號的交易階段同時間只能有一個"
                         "生效)。系統已嘗試釋放舊連線；請確認官網/App 已登出後，回來這裡重新點擊"
                         "「登入券商實盤 API」(重新登入會建立全新連線)。若立即重登仍失敗，"
                         "請等約 1-2 分鐘讓券商端釋放舊連線後再試。")
        # 【ADR-071】斷線自動重連:開關開著、且記憶體裡有本次登入用的憑證,就在
        # 背景自動重試登入,不用人工。券商端釋放舊 session 需要時間,故用退避間隔。
        try:
            if getattr(self, 'auto_reconnect_var', None) and self.auto_reconnect_var.get() \
                    and self._login_creds_mem and not self._reconnecting:
                self._reconnecting = True
                threading.Thread(target=self._reconnect_worker, daemon=True).start()
        except Exception:
            pass

    def _save_app_settings(self):
        """【ADR-072】把目前的 App 設定寫回持久化檔 (零股開盤時刻、自動重連等)。"""
        try:
            self.app_settings['odd_lot_open'] = self.odd_open_var.get()
            self.app_settings['auto_reconnect'] = bool(self.auto_reconnect_var.get())
            if getattr(self, 'remember_creds_var', None) is not None:
                self.app_settings['remember_creds'] = bool(self.remember_creds_var.get())
            config_store.save_app_settings(self.app_settings_file, self.app_settings)
        except Exception:
            pass

    def _on_odd_open_changed(self, event=None):
        """【ADR-072】使用者切換盤中零股開盤時刻:即時套用到 market_session 並存檔。"""
        hhmm = self.odd_open_var.get()
        market_session.set_odd_lot_open_hhmm(hhmm)
        self._save_app_settings()
        self.log_message(f"【設定】盤中零股開盤時刻已設為 {hhmm}"
                         f"{'(現制)' if hhmm == '09:10' else '(未來改制)'};"
                         "自動交易的零股策略會依此判斷開盤與否。")

    def _on_auto_reconnect_toggle(self):
        """【ADR-071】使用者切換「斷線自動重連」開關時的提示與狀態更新。"""
        on = bool(self.auto_reconnect_var.get())
        try:
            self.chk_auto_reconnect.config(fg="#00E676" if on else "#8A99AD")
        except Exception:
            pass
        self._save_app_settings()
        if on:
            if not self._login_creds_mem:
                self.log_message("【自動重連】已開啟,但目前尚未登入 (或本次尚未輸入憑證);"
                                 "請先登入一次券商 API,之後若斷線就會用這次的憑證自動重連。"
                                 "憑證密碼只留在記憶體、不會寫進磁碟;程式整個關掉再開需要再登入一次。")
            else:
                self.log_message("【自動重連】已開啟:偵測到斷線會自動用本次登入的憑證在背景重試"
                                 "連線 (退避間隔,交易時段內尤其會積極重連),不需要人工重新登入。")
        else:
            self.log_message("【自動重連】已關閉:斷線後不會自動重連,需要你手動按「登入券商實盤 API」。")

    def _reconnect_worker(self):
        """【ADR-071】斷線後背景自動重連。退避間隔重試;成功、使用者關閉開關、
        程式關閉、或已在其他路徑重新登入,都會結束。為避免半夜全休市時空轉,
        全市場休市時改用長間隔慢慢等 (仍會定期試,以防券商端提早恢復)。"""
        import time as _time
        backoffs = [15, 30, 60, 120, 300]  # 秒:逐步拉長,最長 5 分鐘一次
        attempt = 0
        try:
            while True:
                if getattr(self, '_closing', False):
                    return
                # 開關被關掉、已經重新登入成功、或憑證被清掉 → 收工
                if not (getattr(self, 'auto_reconnect_var', None) and self.auto_reconnect_var.get()):
                    self.safe_after(0, self.log_message, "【自動重連】開關已關閉,停止重連。")
                    return
                if self.api_logged_in:
                    return  # 已由其他路徑 (或上一輪) 連上
                creds = self._login_creds_mem
                if not creds:
                    self.safe_after(0, self.log_message, "【自動重連】找不到可用憑證 (可能已手動登出),停止重連。")
                    return
                if self._login_in_progress:
                    _time.sleep(5); continue  # 有登入流程在跑,等它

                # 全市場 (台股 + 期貨日夜盤) 都休市時,重連沒有急迫性 → 長間隔慢等
                any_open = market_session.is_stock_open() or market_session.is_futures_open()
                wait = backoffs[min(attempt, len(backoffs) - 1)] if any_open else 300
                attempt += 1
                self.safe_after(0, self.log_message,
                                f"【自動重連】第 {attempt} 次嘗試重新連線券商 API"
                                f"{'' if any_open else ' (目前全市場休市,改為每 5 分鐘試一次)'}...")
                self._login_in_progress = True
                try:
                    self._start_login_watchdog()
                except Exception:
                    pass
                try:
                    self.process_broker_login(creds['api_key'], creds['secret_key'],
                                              creds['pid'], creds['ca_path'], creds['ca_pw'])
                except Exception as e:
                    self.safe_after(0, self.log_message, f"【自動重連】本次嘗試失敗: {type(e).__name__}: {e}")
                if self.api_logged_in:
                    self.safe_after(0, self.log_message, "【自動重連】✅ 已重新連線成功,自動交易將自行恢復運作。")
                    return
                _time.sleep(wait)
        finally:
            self._reconnecting = False

    # ============================================================
    # 【ADR-073】記住憑證並開機自動登入 (加密存本機)
    # ============================================================
    def _device_key_material(self):
        """推導「綁這台機器/這個帳號」的金鑰材料 (bytes)。由 hostname + 使用者名 +
        一份一次性隨機裝置碼 (device_id.bin) 組成;裝置碼不存在就產生一份。
        這保證加密憑證檔複製到別台機器解不開,但無法防「能完整存取本機本帳號的人」
        ——這是免輸入自動登入的固有取捨 (見 secure_store 說明)。"""
        try:
            did_path = os.path.join(os.path.dirname(os.path.abspath(self.secure_creds_file)), 'device_id.bin')
        except Exception:
            did_path = 'device_id.bin'
        try:
            if os.path.exists(did_path):
                with open(did_path, 'rb') as f:
                    did = f.read()
            else:
                did = secrets.token_bytes(32)
                with open(did_path, 'wb') as f:
                    f.write(did)
        except Exception:
            did = b'fallback-device-seed'
        seed = f"{platform.node()}|{getpass.getuser()}|StockBuild".encode('utf-8', 'ignore')
        return seed + did

    def _save_secure_creds(self, creds):
        """把憑證 (含密碼) 加密後寫到 secure_creds_file。"""
        try:
            blob = secure_store.encrypt_dict(creds, self._device_key_material())
            with open(self.secure_creds_file, 'w', encoding='utf-8') as f:
                json.dump({'blob': blob}, f)
            self.log_message("【自動登入】已把憑證加密存到本機 (綁定這台機器),下次開程式會自動登入。"
                             "注意:此為裝置金鑰加密,擋得住檔案外流,但擋不了能完整存取這台電腦"
                             "本帳號的人;不想存了可取消勾選即刪除。")
        except Exception as e:
            self.log_message(f"【自動登入】憑證加密儲存失敗 (不影響本次連線): {type(e).__name__}: {e}")

    def _load_secure_creds(self):
        """讀回並解密憑證;沒有檔案/解不開一律回 None。"""
        try:
            if not os.path.exists(self.secure_creds_file):
                return None
            with open(self.secure_creds_file, 'r', encoding='utf-8') as f:
                blob = json.load(f).get('blob', '')
            if not blob:
                return None
            return secure_store.decrypt_dict(blob, self._device_key_material())
        except Exception as e:
            self.log_message(f"【自動登入】讀取加密憑證失敗 (可能換過機器或檔案損毀),需手動登入一次: {type(e).__name__}: {e}")
            return None

    def _delete_secure_creds(self):
        try:
            if os.path.exists(self.secure_creds_file):
                os.remove(self.secure_creds_file)
        except Exception:
            pass

    def _on_remember_creds_toggle(self):
        """勾選「記住憑證」:已登入就立刻把記憶體憑證加密存檔;取消就刪檔。"""
        on = bool(self.remember_creds_var.get())
        try:
            self.chk_remember_creds.config(fg="#00E676" if on else "#8A99AD")
        except Exception:
            pass
        if on:
            if self._login_creds_mem:
                self._save_secure_creds(self._login_creds_mem)
            else:
                self.log_message("【自動登入】已勾選「記住憑證」,但目前尚未登入;"
                                 "請先登入一次,登入成功後會自動把憑證加密存檔。")
        else:
            self._delete_secure_creds()
            self.log_message("【自動登入】已取消「記住憑證」,本機加密憑證檔已刪除,下次開程式需手動登入。")
        self._save_app_settings()

    def _try_auto_login_on_start(self):
        """開機自動登入:若設定記住憑證且解得出加密憑證,背景自動登入一次。"""
        try:
            if not (getattr(self, 'remember_creds_var', None) and self.remember_creds_var.get()):
                return
            if self.api_logged_in or self._login_in_progress or not HAS_SJ:
                return
            creds = self._load_secure_creds()
            if not creds:
                return
            self.log_message("【自動登入】偵測到已記住的加密憑證,正在背景自動登入券商 API...")
            self._login_in_progress = True
            try:
                self.btn_login.config(text="⏳ 自動登入中...", bg="#8A99AD", fg="black")
            except Exception:
                pass
            try:
                self._start_login_watchdog()
            except Exception:
                pass
            threading.Thread(
                target=self.process_broker_login,
                args=(creds['api_key'], creds['secret_key'], creds['pid'], creds['ca_path'], creds['ca_pw']),
                daemon=True).start()
        except Exception as e:
            self.log_message(f"【自動登入】啟動自動登入時發生狀況: {type(e).__name__}: {e}")

    def toggle_login(self):
        if self._login_in_progress:
            # 【第十二輪修正】登入中 (shioaji 下載合約可能需要 30 秒~2 分鐘,
            # 期間畫面可能顯示「沒有回應」屬正常現象) 再點一次不會重開對話框,
            # 避免第二個 login 執行緒同時搶同一組 shioaji 資源、讓凍結更久。
            self.log_message("【提示】登入正在進行中 (合約下載可能需要 1-2 分鐘),請耐心等候,不要重複點擊或強制關閉。")
            return
        if self.api_logged_in:
            self.api_logged_in = False
            # 【ADR-071】手動登出 = 明確不想連線,清掉記憶體憑證,避免自動重連把
            # 剛登出的連線又接回去 (使用者主動登出的意圖優先於自動重連)。
            self._login_creds_mem = None
            self.lbl_api_status.config(text="🔴 券商未連線", fg="#FF5252")
            self.btn_login.config(text="🔒 登入券商實盤 API", bg="#FF9100", fg="black")
            self.log_message("【系統】已中斷券商實盤連線。" +
                             ("(自動重連已開啟,但手動登出不會自動重連;要恢復請再登入一次。)"
                              if getattr(self, 'auto_reconnect_var', None) and self.auto_reconnect_var.get() else ""))
            if HAS_SJ:
                # 【第十二輪修正】背景執行緒+限時,避免「已登入時按登出」也卡住主執行緒。
                try:
                    t = threading.Thread(target=lambda: self.sj_api.logout(), daemon=True)
                    t.start(); t.join(timeout=3.0)
                except Exception:
                    pass
        else:
            self.open_login_dialog()

    def _start_login_watchdog(self):
        """
        【第十二輪修正 問題1/2/3 根因】shioaji 的 login() 在需要重新下載/解析
        合約資料時 (例如當天第一次登入),是純 CPU 密集的同步解析工作;雖然我們
        已經把它丟到背景執行緒執行,但 CPython 的 GIL 若被這段解析長時間佔用
        而沒有釋放,GUI 主執行緒 (Tk 事件迴圈) 就完全排不到執行機會——這正是
        「明明是背景執行緒在做事,畫面卻整個沒反應」的成因,Windows 偵測到視窗
        超過幾秒沒有回應訊息,就會跳出「Python 沒有回應」的系統對話框 (使用者
        截圖中那個對話框來自 Windows,不是我們的程式)。這是 shioaji SDK 本身的
        已知限制,無法從我們的 Python 執行緒層完全避免;能做的是:
          1. 及早且持續提醒使用者這是正常現象、預期要等多久、不要強制關閉。
          2. 絕不能在这段期間讓使用者誤觸而疊加第二個 login (見 toggle_login)。
        這個 watchdog 每 10 秒檢查一次,只要 _login_in_progress 還是 True 就
        持續提示;一旦 process_broker_login 結束 (成功或失敗) 就會停止。
        """
        def _tick(count=0):
            if not self._login_in_progress:
                self._login_watchdog_id = None
                return
            if count == 0:
                self.log_message("【登入中】正在連線並下載/驗證合約資料,首次登入(或合約需要更新時)可能需要 30 秒到 2 分鐘。"
                                 "此期間畫面可能顯示「沒有回應」，這是 shioaji 下載合約時的已知現象，請耐心等候，不要強制關閉程式。")
            elif count % 3 == 0:
                self.log_message(f"【登入中】仍在等待券商連線回應 (已等待約 {count*10} 秒)...如已超過 2-3 分鐘仍無回應，"
                                 "可能是網路異常，請等它結束或視情況強制關閉後重開程式再試。")
            self._login_watchdog_id = self.safe_after(10000, _tick, count + 1)
        self._login_watchdog_id = self.safe_after(0, _tick, 0)

    def open_login_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("登入永豐金實盤 API (含憑證)")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 450, 400)
        dlg.transient(self) 
        dlg.grab_set()

        tk.Label(dlg, text="身分證字號:", bg="#1A2026", fg="white").pack(pady=(10,2))
        ent_pid = tk.Entry(dlg, bg="#2A323D", fg="white", justify="center", width=40)
        ent_pid.insert(0, self.saved_pid); ent_pid.pack()

        tk.Label(dlg, text="API Key:", bg="#1A2026", fg="white").pack(pady=(10,2))
        ent_api_key = tk.Entry(dlg, bg="#2A323D", fg="white", show="*", justify="center", width=40)
        ent_api_key.insert(0, self.saved_api_key); ent_api_key.pack()

        tk.Label(dlg, text="Secret Key:", bg="#1A2026", fg="white").pack(pady=(10,2))
        ent_secret = tk.Entry(dlg, bg="#2A323D", fg="white", show="*", justify="center", width=40)
        ent_secret.insert(0, self.saved_secret_key); ent_secret.pack()
        
        tk.Label(dlg, text="憑證檔案路徑 (.pfx):", bg="#1A2026", fg="white").pack(pady=(10,2))
        ent_ca_path = tk.Entry(dlg, bg="#2A323D", fg="white", justify="center", width=40)
        ent_ca_path.insert(0, self.saved_ca_path); ent_ca_path.pack()
        
        tk.Label(dlg, text="憑證密碼:", bg="#1A2026", fg="white").pack(pady=(10,2))
        ent_ca_pw = tk.Entry(dlg, bg="#2A323D", fg="white", show="*", justify="center", width=40)
        ent_ca_pw.pack()

        def do_login():
            pid, api, sec = ent_pid.get().strip(), ent_api_key.get().strip(), ent_secret.get().strip()
            ca_p, ca_pw = ent_ca_path.get().strip(), ent_ca_pw.get().strip()
            if not api or not sec or not pid or not ca_p or not ca_pw:
                messagebox.showerror("錯誤", "所有欄位皆必填才能進行實盤下單！")
                return
            self.save_config(api, sec, pid, ca_p)
            self.saved_api_key, self.saved_secret_key, self.saved_pid, self.saved_ca_path = api, sec, pid, ca_p
            dlg.destroy()
            self._login_in_progress = True
            self.btn_login.config(text="⏳ 連線中...請稍候", bg="#8A99AD", fg="black")
            self._start_login_watchdog()
            threading.Thread(target=self.process_broker_login, args=(api, sec, pid, ca_p, ca_pw), daemon=True).start()

        tk.Button(dlg, text="驗證憑證並連線", bg="#FF9100", fg="black", font=('微軟正黑體', 10, 'bold'), command=do_login).pack(pady=15)
        # 【ADR-139】永豐要求先在模擬環境跑過登入測試 + 證券/期貨下單測試,
        # 正式環境的下單權限才會開通。入口放在這裡是因為使用者會在「登入不成功
        # /沒有下單權限」的時候來開這個視窗,答案就在旁邊。
        tk.Button(dlg, text="🧪 API 測試 (證券/期貨下單測試)", bg="#00ACC1", fg="black",
                  font=('微軟正黑體', 9, 'bold'), relief="flat",
                  command=self.open_api_test_dialog).pack(pady=(0, 10))

    # ==================================================================
    # 【ADR-139】永豐 API 測試:模擬環境的登入測試 + 證券/期貨下單測試
    # ==================================================================
    # 規格出處:https://sinotrade.github.io/zh/tutor/prepare/terms/
    #
    # 判斷規則全部在 core/api_test.py(離線可測);模擬連線與送單在
    # brokers/sinopac.SinopacApiTestSession(唯一可以碰 SDK 的層)。
    # 這裡只負責:收使用者輸入 → 跳確認視窗 → 丟背景執行緒 → safe_after 回 UI。

    def open_api_test_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("永豐 API 測試 (模擬環境)")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 640, 620)
        dlg.transient(self)

        tk.Label(dlg, text="永豐 API 測試 (模擬環境,不會有真實委託)",
                 bg="#1A2026", fg="#00E5FF", font=('微軟正黑體', 11, 'bold')).pack(pady=(10, 2))
        tk.Label(dlg, text=("永豐要求:簽署條款後要在模擬環境跑過「登入測試」與「下單測試」,"
                            "證券、期貨兩個帳戶分別測,通過後正式環境的下單權限才會開通。\n"
                            "測試時間:週一~五 08:00~20:00(18:00 之後僅限台灣 IP)。"
                            "兩筆測試單之間會自動間隔 1.5 秒(官方要求 1 秒以上)。"),
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 8), wraplength=600,
                 justify='left').pack(fill=tk.X, padx=12, pady=(2, 8))

        # 【ADR-139 追記】金鑰一律**手動輸入**,不自動帶入已存的那組。
        # 使用者的原話:「關於相關的帳號&API KEY,還是要我手動先輸入」。
        # 這也比較合理 —— 這個視窗要測的是「這一組金鑰能不能連上」,
        # 自動帶入等於在測另一組,而且測完你不會知道剛剛測的到底是哪一組。

        form = tk.Frame(dlg, bg="#1A2026"); form.pack(fill=tk.X, padx=12)

        def _lbl(parent, text, fg="white"):
            return tk.Label(parent, text=text, bg="#1A2026", fg=fg, font=('微軟正黑體', 9))

        def _ent(parent, value, width=22, show=None):
            e = tk.Entry(parent, bg="#2A323D", fg="white", justify="center",
                         width=width, show=show)
            e.insert(0, str(value))
            return e

        _lbl(form, "API Key:").grid(row=0, column=0, sticky='w', pady=3)
        e_key = _ent(form, "", 34, show="*")      # 空的 —— 手動輸入
        e_key.grid(row=0, column=1, columnspan=2, sticky='w', padx=4, pady=3)
        _lbl(form, "Secret Key:").grid(row=1, column=0, sticky='w', pady=3)
        e_sec = _ent(form, "", 34, show="*")      # 空的 —— 手動輸入
        e_sec.grid(row=1, column=1, columnspan=2, sticky='w', padx=4, pady=3)

        def _fill_saved():
            """把已存的那組帶進來 —— **只在使用者按這個按鈕時**才發生。

            這是明確的使用者動作,不是「打開視窗就自動填好」,所以不違反
            「手動輸入」這件事,只是省去重打一次。沒存過就什麼都不做並說明。
            """
            if not (self.saved_api_key and self.saved_secret_key):
                _write("目前沒有已存的金鑰可以帶入,請直接手動輸入。")
                return
            e_key.delete(0, tk.END); e_key.insert(0, self.saved_api_key)
            e_sec.delete(0, tk.END); e_sec.insert(0, self.saved_secret_key)
            _write("已帶入你先前存過的那組金鑰。")

        tk.Button(form, text="帶入已存的金鑰", bg="#2A323D", fg="#8A99AD", relief="flat",
                  font=('微軟正黑體', 8), command=_fill_saved).grid(
                  row=1, column=3, sticky='w', padx=(4, 0))
        tk.Label(form, text=("※ 金鑰請**手動輸入**(不會自動帶入),測完不留在這個視窗裡。\n"
                             "※ 此 API Key 必須已開通「交易」權限,否則下單測試會被拒絕。\n"
                             "※ 證券/期貨**帳號不必填** —— 登入成功後由永豐回傳,下面會列出來。"),
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8), justify='left').grid(
                 row=2, column=0, columnspan=4, sticky='w', pady=(0, 6))

        ttk.Separator(form, orient='horizontal').grid(row=3, column=0, columnspan=4,
                                                      sticky='ew', pady=6)

        # 【ADR-139 追記】只測連線(登入)不送單。使用者的原話:「這個功能主要
        # 測試連線功能是否正常,可以讓券商那邊收到正確的連線訊息」。
        # 先確認連得上、帳號抓得到,再決定要不要送那兩筆測試單 —— 這一段
        # 完全不會有任何委託送出去,是最安全的第一步。
        var_conn_only = tk.BooleanVar(value=False)
        tk.Checkbutton(form, text="只測連線 (只做登入測試,完全不送任何委託)",
                       variable=var_conn_only, bg="#1A2026", fg="#29B6F6",
                       selectcolor="#2A323D", activebackground="#1A2026",
                       font=('微軟正黑體', 9, 'bold')).grid(
                       row=9, column=0, columnspan=4, sticky='w', pady=(8, 0))

        var_stk = tk.BooleanVar(value=True)
        tk.Checkbutton(form, text="證券下單測試", variable=var_stk, bg="#1A2026",
                       fg="#00E676", selectcolor="#2A323D", activebackground="#1A2026",
                       font=('微軟正黑體', 9, 'bold')).grid(row=4, column=0, sticky='w')
        _lbl(form, "代碼").grid(row=4, column=1, sticky='e')
        e_stk_code = _ent(form, api_test.DEFAULT_STOCK['code'], 8)
        e_stk_code.grid(row=4, column=2, sticky='w', padx=4)
        _lbl(form, "價格 / 數量(張)").grid(row=5, column=1, sticky='e')
        _stk_px = tk.Frame(form, bg="#1A2026"); _stk_px.grid(row=5, column=2, columnspan=2, sticky='w', padx=4)
        e_stk_price = _ent(_stk_px, api_test.DEFAULT_STOCK['price'], 8); e_stk_price.pack(side=tk.LEFT)
        e_stk_qty = _ent(_stk_px, api_test.DEFAULT_STOCK['qty'], 5); e_stk_qty.pack(side=tk.LEFT, padx=(6, 0))

        var_fut = tk.BooleanVar(value=True)
        tk.Checkbutton(form, text="期貨下單測試", variable=var_fut, bg="#1A2026",
                       fg="#00E676", selectcolor="#2A323D", activebackground="#1A2026",
                       font=('微軟正黑體', 9, 'bold')).grid(row=6, column=0, sticky='w', pady=(6, 0))
        _lbl(form, "商品").grid(row=6, column=1, sticky='e', pady=(6, 0))
        e_fut_code = _ent(form, api_test.DEFAULT_FUTURES['code'], 8)
        e_fut_code.grid(row=6, column=2, sticky='w', padx=4, pady=(6, 0))
        tk.Label(form, text="(月份自動抓最近月)", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 8)).grid(row=6, column=3, sticky='w')
        _lbl(form, "價格 / 數量(口)").grid(row=7, column=1, sticky='e')
        _fut_px = tk.Frame(form, bg="#1A2026"); _fut_px.grid(row=7, column=2, columnspan=2, sticky='w', padx=4)
        e_fut_price = _ent(_fut_px, api_test.DEFAULT_FUTURES['price'], 8); e_fut_price.pack(side=tk.LEFT)
        e_fut_qty = _ent(_fut_px, api_test.DEFAULT_FUTURES['qty'], 5); e_fut_qty.pack(side=tk.LEFT, padx=(6, 0))

        tk.Label(form, text=("※ 價格請填「當日漲跌停之內」的合理價,否則會被交易所退單。"
                             "這裡刻意不自動抓現價 —— 那會打到報價快照的流量上限(鐵則5)。"),
                 bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 8), wraplength=600,
                 justify='left').grid(row=8, column=0, columnspan=4, sticky='w', pady=(6, 0))

        out = tk.Text(dlg, height=13, bg="#0D1115", fg="#D0D8E0",
                      font=('Consolas', 9), wrap='word')
        out.pack(fill=tk.BOTH, expand=True, padx=12, pady=(10, 4))

        def _write(text, clear=False):
            try:
                if not dlg.winfo_exists():
                    return
                if clear:
                    out.delete('1.0', tk.END)
                out.insert(tk.END, str(text) + "\n")
                out.see(tk.END)
            except Exception:
                pass

        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(pady=(0, 10))
        btn_run = tk.Button(btns, text="▶ 開始測試", bg="#00ACC1", fg="black", relief="flat",
                            font=('微軟正黑體', 10, 'bold'), padx=16, pady=3)
        btn_run.pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="🔍 查詢測試狀態", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=14, pady=3,
                  command=lambda: _write(self._api_test_signed_text(), clear=True)).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=18, pady=3,
                  command=dlg.destroy).pack(side=tk.LEFT, padx=6)

        def _start():
            plan = {
                'api_key': e_key.get().strip(),
                'secret_key': e_sec.get().strip(),
                'conn_only': bool(var_conn_only.get()),
                'stock': {'on': bool(var_stk.get()), 'code': e_stk_code.get().strip(),
                          'price': e_stk_price.get().strip(), 'qty': e_stk_qty.get().strip()},
                'futures': {'on': bool(var_fut.get()), 'code': e_fut_code.get().strip().upper(),
                            'price': e_fut_price.get().strip(), 'qty': e_fut_qty.get().strip()},
            }
            ok, why = self._api_test_validate(plan)
            if not ok:
                _write(f"❌ {why}", clear=True)
                return
            # 【鐵則14】即使是模擬環境,送出任何委託之前一律先跳確認視窗,
            # 而且把「實際會送出什麼」逐欄位攤開給使用者看過。
            if not messagebox.askyesno("確認 API 測試", self._api_test_confirm_text(plan)):
                _write("已取消,沒有送出任何東西。", clear=True)
                return
            btn_run.config(state='disabled', text="⏳ 測試中…")
            _write("開始測試…", clear=True)
            threading.Thread(target=self._api_test_worker,
                             args=(plan, _write, btn_run), daemon=True).start()

        btn_run.config(command=_start)
        _write(self._api_test_preflight_text())

    def _api_test_validate(self, plan):
        """送出前的本地驗證(鐵則 9:不靠券商回錯誤才知道欄位不對)。"""
        if not plan['api_key'] or not plan['secret_key']:
            return False, "API Key 與 Secret Key 都要填(這兩項一律手動輸入)"
        if plan.get('conn_only'):
            # 只測連線:不會送出任何委託,所以委託欄位完全不看。
            return True, ""
        if not (plan['stock']['on'] or plan['futures']['on']):
            return False, "至少要勾一項測試(或改勾「只測連線」)"
        for kind, key in (('證券', 'stock'), ('期貨', 'futures')):
            leg = plan[key]
            if not leg['on']:
                continue
            ok, why = api_test.validate_order(kind, leg['code'], leg['price'], leg['qty'])
            if not ok:
                return False, f"{kind}:{why}"
        return True, ""

    def _api_test_confirm_text(self, plan):
        if plan.get('conn_only'):
            # 只測連線這條路不會送出任何委託,但確認視窗照跳 ——
            # 讓「走到送單流程之前一定問過使用者」這個不變式沒有例外,
            # 不必再去分辨哪一條路需要問、哪一條不用(鐵則14 的精神)。
            return ("即將對**模擬環境**做連線(登入)測試。\n\n"
                    "只會登入並列出永豐回傳的帳戶,**不會送出任何委託**。\n"
                    "也不會影響你目前的正式連線。\n\n確定開始?")
        lines = ["即將在**模擬環境**送出以下測試委託:", ""]
        if plan['stock']['on']:
            lines.append(f"  證券  買進  {plan['stock']['code']}  "
                         f"限價 {plan['stock']['price']}  {plan['stock']['qty']} 張  ROD/現股")
        if plan['futures']['on']:
            lines.append(f"  期貨  買進  {plan['futures']['code']}(最近月)  "
                         f"限價 {plan['futures']['price']}  {plan['futures']['qty']} 口  ROD/自動")
        lines += ["", "這是 simulation=True 的連線,不會有真實成交,",
                  "也不會影響你目前的正式連線(另開一條臨時連線,測完即斷)。",
                  "", "確定送出?"]
        return "\n".join(lines)

    def _api_test_preflight_text(self):
        sdk = ""
        try:
            sdk = self.brokers['sinopac'].sdk_version()
        except Exception:
            sdk = getattr(sj, '__version__', '') if HAS_SJ else ''
        _ok, lines = api_test.preflight(sdk, True, True)
        head = ["【開始前檢查】", ""]
        tail = ["", "(帳戶有沒有真的存在,要登入模擬環境之後才知道 —— 上面兩項",
                " 先當作有,實際跑的時候會照真實結果跳過對應的測試。)"]
        return "\n".join(head + lines + tail)

    def _api_test_signed_text(self):
        """查詢是否通過 API 測試 —— 官方指定看**正式模式**登入後的 accounts.signed。"""
        if not (self.api_logged_in and HAS_SJ):
            return ("請先以正式模式登入券商 API,再查詢測試狀態。\n"
                    "(官方的判定依據是正式環境 accounts 的 signed 欄位,"
                    "模擬環境查不到這個結果。)")
        try:
            return api_test.signed_summary(self.brokers['sinopac'].signed_rows())
        except Exception as e:
            return f"查詢失敗:{e}"

    def _api_test_worker(self, plan, write, btn):
        """背景執行緒跑完整個測試流程。所有 UI 更新一律走 safe_after(鐵則 13)。"""
        results = []

        def say(text):
            self.safe_after(0, write, text)

        session = None
        try:
            session = sinopac.SinopacApiTestSession()
            say("已建立模擬環境連線 (simulation=True)。")

            # ---- 步驟一:登入測試 ----
            try:
                accounts = session.login(plan['api_key'], plan['secret_key'])
                results.append({'step': '登入測試 login', 'ok': True,
                                'detail': f"取得 {len(accounts)} 個帳戶"})
                say("✅ 登入測試通過(券商端已收到這次連線)。")
                # 帳號是**登入後由永豐回傳的**,不必手動填 —— 列出來讓使用者
                # 確認「券商那邊認得的是這些帳號」。
                say(api_test.accounts_text(session.account_rows()))
            except Exception as e:
                results.append({'step': '登入測試 login', 'ok': False, 'detail': str(e)})
                say(f"❌ 登入測試失敗:{e}")
                return

            if plan.get('conn_only'):
                say("「只測連線」已完成,沒有送出任何委託。")
                say("要讓正式環境的下單權限開通,還需要取消勾選「只測連線」"
                    "再跑一次證券/期貨下單測試。")
                return

            has_stk = session.stock_account() is not None
            has_fut = session.futopt_account() is not None
            if plan['stock']['on'] and not has_stk:
                results.append({'step': '證券下單測試', 'ok': False,
                                'detail': '這組金鑰底下沒有證券帳戶'})
                say("⚠️ 找不到證券帳戶,跳過證券下單測試。")
            if plan['futures']['on'] and not has_fut:
                results.append({'step': '期貨下單測試', 'ok': False,
                                'detail': '這組金鑰底下沒有期貨帳戶'})
                say("⚠️ 找不到期貨帳戶,跳過期貨下單測試。")

            sent_any = False

            # ---- 步驟二:證券下單測試 ----
            if plan['stock']['on'] and has_stk:
                try:
                    contract = session.stock_contract(plan['stock']['code'])
                    if contract is None:
                        raise ValueError(f"模擬環境查無證券合約 {plan['stock']['code']}")
                    trade = session.place_stock_test_order(
                        contract, float(plan['stock']['price']), int(plan['stock']['qty']))
                    results.append({'step': '證券下單測試 place_order', 'ok': True,
                                    'detail': self._api_test_trade_text(trade)})
                    say("✅ 證券下單測試已送出。")
                    sent_any = True
                except Exception as e:
                    results.append({'step': '證券下單測試 place_order', 'ok': False,
                                    'detail': str(e)})
                    say(f"❌ 證券下單測試失敗:{e}")

            # ---- 官方:兩筆測試單需間隔 1 秒以上 ----
            if sent_any and plan['futures']['on'] and has_fut:
                say(f"等待 {api_test.ORDER_INTERVAL_SEC} 秒"
                    "(官方要求兩筆測試單間隔 1 秒以上,以利系統留存測試紀錄)…")
                time.sleep(api_test.ORDER_INTERVAL_SEC)

            # ---- 步驟三:期貨下單測試 ----
            if plan['futures']['on'] and has_fut:
                try:
                    symbol = plan['futures']['code']
                    code = api_test.pick_near_month(session.futures_months(symbol))
                    if not code:
                        raise ValueError(f"模擬環境挑不出 {symbol} 的最近月合約")
                    contract = session.futures_contract(symbol, code)
                    if contract is None:
                        raise ValueError(f"模擬環境查無期貨合約 {code}")
                    say(f"期貨最近月合約:{code}")
                    trade = session.place_futures_test_order(
                        contract, float(plan['futures']['price']), int(plan['futures']['qty']))
                    results.append({'step': f'期貨下單測試 place_order ({code})', 'ok': True,
                                    'detail': self._api_test_trade_text(trade)})
                    say("✅ 期貨下單測試已送出。")
                except Exception as e:
                    results.append({'step': '期貨下單測試 place_order', 'ok': False,
                                    'detail': str(e)})
                    say(f"❌ 期貨下單測試失敗:{e}")
        except Exception as e:
            results.append({'step': '建立模擬連線', 'ok': False, 'detail': str(e)})
            say(f"❌ 無法建立模擬環境連線:{e}")
        finally:
            if session is not None:
                session.close()
            report = api_test.format_report(results)
            self.safe_after(0, write, "\n" + report)
            self.safe_after(0, self.log_message,
                            "【API測試】" + report.replace("\n", " / "))
            self.safe_after(0, lambda: btn.config(state='normal', text="▶ 開始測試"))

    def _api_test_trade_text(self, trade):
        try:
            return self.brokers['sinopac'].order_status_text(trade)
        except Exception:
            return '已送出'

    # ================= ✨ v1 串流監聽 (單軌架構,以官方 intraday_odd 欄位精準分流) =================
    # 【修正說明】原先同時註冊 v0 set_quote_callback 與 v1 callbacks,
    # v0 以 topic 字串 "ODD" 判斷零股極不可靠,常把整股訊息寫進零股暫存 (反之亦然),
    # 造成零股與五檔資料互相污染。現改為只用 v1 typed callbacks,
    # 直接讀取官方 tick/bidask 物件上的 intraday_odd 布林欄位分流,並加鎖確保執行緒安全。

    def on_tick_stk_v1(self, exchange, tick):
        try:
            self._wl_route_stream_tick(tick, is_fop=False)  # 【第十六輪】自選股指數串流
            if self.current_contract and tick.code == self.current_contract.code:
                is_odd = bool(getattr(tick, 'intraday_odd', False))
                with self.quote_lock:
                    if is_odd:
                        self.current_tick_odd = tick
                    else:
                        self.current_tick_normal = tick
                        try:
                            self._live_bar_on_tick(float(tick.close))  # 【ADR-041】活K棒
                        except Exception:
                            pass
                        # 整股最新成交價直接快取,零股價差計算不必再打 snapshot
                        try:
                            c = float(tick.close)
                            if c > 0: self.last_norm_close = c
                        except Exception: pass
                        # 盤後零股 (13:40-14:30) 收盤後,整股 tick 串流會帶入 closing_oddlot_* 欄位。
                        # 這是 snapshot 拿不到、唯一能取得真實零股收盤價的來源,收到就快取起來。
                        try:
                            oc = float(getattr(tick, 'closing_oddlot_close', 0) or 0)
                            if oc > 0:
                                self.last_odd_close = oc
                                self.last_odd_shares = int(getattr(tick, 'closing_oddlot_shares', 0) or 0)
                                dt = getattr(tick, 'datetime', None)
                                self.last_odd_date = dt.strftime('%Y-%m-%d') if dt else ""
                        except Exception: pass
                # 【ADR-008】把這筆成交寫進成交明細跳動列表 (放在鎖外,避免佔用 quote_lock 太久)
                try:
                    self._record_trade_tick(tick.close, getattr(tick, 'volume', 0), is_odd)
                except Exception: pass
        except Exception: pass

    def on_bidask_stk_v1(self, exchange, bidask):
        try:
            if self.current_contract and bidask.code == self.current_contract.code:
                with self.quote_lock:
                    if bool(getattr(bidask, 'intraday_odd', False)):
                        self.current_bidask_odd = bidask
                    else:
                        self.current_bidask_normal = bidask
        except Exception: pass

    @staticmethod
    def _fop_code_match(tick_code, contract_code):
        """
        【第九輪修正 第4項:台指期報價慢的根因】訂閱 R1 連續合約 (如 TXFR1) 時,
        shioaji 推送的 tick.code 是「實際月份合約」(如 TXFG6),不會等於 'TXFR1',
        原本的相等比對永遠失敗 → 期貨串流全被丟掉 → 永遠退到 5 秒快照 fallback,
        這就是「台指期報價太慢、當沖來不及」的原因。
        改為「商品前綴 (前3碼) 比對」:一次只訂閱一檔期貨,前綴相同即屬同商品,
        不會誤收;完全相等當然也接受 (訂閱特定月份合約時)。
        """
        tc = str(tick_code or ''); cc = str(contract_code or '')
        if not tc or not cc:
            return False
        return tc == cc or tc[:3] == cc[:3]

    def on_tick_fop_v1(self, exchange, tick):
        try:
            self._wl_route_stream_tick(tick, is_fop=True)  # 【第十六輪】自選股期貨串流
            if self.current_contract and self._fop_code_match(tick.code, self.current_contract.code):
                with self.quote_lock:
                    self.current_tick_normal = tick
                try:
                    self._live_bar_on_tick(float(tick.close))  # 【ADR-041】活K棒
                except Exception:
                    pass
                try:
                    self._record_trade_tick(tick.close, getattr(tick, 'volume', 0), False)
                except Exception: pass
        except Exception: pass

    def on_bidask_fop_v1(self, exchange, bidask):
        try:
            if self.current_contract and self._fop_code_match(bidask.code, self.current_contract.code):
                with self.quote_lock:
                    self.current_bidask_normal = bidask
        except Exception: pass

    def process_broker_login(self, api_key, secret_key, pid, ca_path, ca_pw):
        """背景執行緒進入點:確保無論成功/失敗/例外,_login_in_progress 都會清除、
        按鈕都會復原成可再次點擊 (否則 watchdog 會誤判成永遠登入中,且使用者
        看到按鈕卡在「連線中」,以為程式真的壞掉、永遠點不動)。"""
        try:
            self._process_broker_login_impl(api_key, secret_key, pid, ca_path, ca_pw)
        finally:
            self._login_in_progress = False
            if not self.api_logged_in:
                self.safe_after(0, lambda: self.btn_login.config(text="🔒 登入券商實盤 API", bg="#FF9100", fg="black"))

    def _process_broker_login_impl(self, api_key, secret_key, pid, ca_path, ca_pw):
        if not HAS_SJ: 
            self.safe_after(0, self.log_message, "【錯誤】未安裝 shioaji 套件！")
            return
        # 【第八輪修正:登入死循環的關鍵解法】不論上一個連線是正常登出、被踢斷、
        # 還是誤判斷線,重新登入一律「先釋放舊連線 → 建全新 Shioaji 物件 → 登入」。
        # 舊寫法對「同一個物件」重複呼叫 login():物件內部狀態已壞 (WebSocket/
        # token 殘留) 且券商端舊 session 可能還佔著名額,重登必失敗,使用者只能
        # 重開整個程式。全新物件 + 舊連線 best-effort logout 徹底解掉這個循環。
        #
        # 【第十二輪修正】只有「這個物件真的曾經登入成功過」(self.api_logged_in
        # 為 True) 才需要 logout;程式剛啟動的第一次登入,self.sj_api 是從未
        # 連線過的全新物件,對它呼叫 logout() 沒有意義,若 shioaji 內部對「從未
        # 建立過連線」的 logout 呼叫處理不夠乾淨 (例如試圖等一個永遠不會來的
        # 中斷確認),反而可能額外增加卡住的風險——跳過它，直接進全新物件。
        # 就算真的需要 logout，也一律包在「背景執行緒+3秒逾時」，不讓它有機會
        # 拖住這個本來就已經在背景執行緒裡的登入流程。
        if self.api_logged_in:
            self.safe_after(0, self.log_message, "正在釋放舊連線並建立全新連線...")
            old_api = self.sj_api
            try:
                t = threading.Thread(target=lambda: old_api.logout(), daemon=True)
                t.start(); t.join(timeout=3.0)
            except Exception:
                pass
        try:
            self.sj_api = self.brokers['sinopac'].new_session()
            # 舊 contract 物件屬於舊連線,一併作廢,強制下次查詢走完整重新訂閱
            self.current_contract = None
            self._wl_contract_cache.clear()  # 【ADR-028】自選股合約快取也隨連線世代作廢
            self._fut_positions_unavailable = False  # 【第十五輪】新連線重新嘗試期貨庫存一次
            self._wl_subscribed.clear(); self._wl_fut_code_map.clear()
            self._wl_idx_code_map.clear(); self._wl_stream_quotes.clear()  # 【第十六輪】串流隨世代作廢
        except Exception as e:
            self.safe_after(0, self.log_message, f"【提示】重建連線物件時發生非預期狀況 ({e}),仍嘗試繼續登入。")

        self.safe_after(0, self.log_message, "連線至券商伺服器並下載最新合約檔中...")
        try:
            _bk = self.brokers['sinopac']
            _dropped = _bk.login(api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
            # 【ADR-114】把實際生效的 SDK 版本與相容處理寫進日誌:升級 shioaji
            # 之後排查問題時,第一個要確認的就是「現在到底跑在哪一版」。
            if _dropped:
                self.safe_after(0, self.log_message,
                                f"【shioaji {_bk.sdk_version()}】此版 login() 不接受 "
                                f"{'/'.join(_dropped)},已略過 (1.7 起合約改為查詢時自動載入)。")
            else:
                self.safe_after(0, self.log_message, f"【shioaji {_bk.sdk_version()}】登入參數沿用舊版行為。")
        except Exception as e:
            self.safe_after(0, self.log_message, f"【API 登入失敗】: {e}")
            self.safe_after(0, self.log_message,
                             "【提示】常見原因:(1) 同一帳號同時在永豐金官網/App 登入,佔用交易"
                             "階段名額——請登出該處後再試;(2) 舊連線尚未被券商端釋放——請等約"
                             " 1-2 分鐘再點一次「登入券商實盤 API」;(3) API Key/Secret 有誤。")
            return
        try:
            self.brokers['sinopac'].activate_ca(ca_path=ca_path, ca_pw=ca_pw, pid=pid)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【憑證啟用失敗】: {e}")
            self.safe_after(0, self.log_message, "【提示】請確認憑證路徑、憑證密碼與身分證字號;報價查詢可用,但實盤下單需要憑證通過。")
            # 憑證失敗不 return:登入本身成功,報價功能仍可用;下單時會再被券商擋

        try:
            try:
                # 【修正】只註冊 v1 typed callbacks。
                # 舊版同時掛 v0 set_quote_callback 會造成零股/整股資料互相覆蓋污染。
                self.brokers['sinopac'].set_quote_callbacks(
                    self.on_tick_stk_v1, self.on_bidask_stk_v1,
                    self.on_tick_fop_v1, self.on_bidask_fop_v1)
            except Exception as cb_e:
                self.safe_after(0, self.log_message, f"五檔流初始化異常: {cb_e}")

            try:
                # 【使用者調整#5】委託/成交主動回報:依 shioaji 官方「使用限制」
                # 文件明確建議「委託狀態請使用主動回報，避免以 update_status() 輪詢」，
                # 這裡註冊一次 push callback，「我的委託單」「我的已成交」兩個清單
                # 完全靠這個 callback 更新，不做輪詢查詢。
                self.brokers['sinopac'].set_order_callback(self.on_order_deal_callback)
            except Exception as cb_e:
                self.safe_after(0, self.log_message, f"委託回報callback初始化異常: {cb_e}")

            self.api_logged_in = True
            # 【ADR-071】登入成功 → 把這次的憑證暫存在記憶體 (只 RAM,不落地),
            # 供「斷線自動重連」使用。這是達成「開一次、整天不用管」的關鍵材料。
            self._login_creds_mem = {'api_key': api_key, 'secret_key': secret_key,
                                     'pid': pid, 'ca_path': ca_path, 'ca_pw': ca_pw}
            # 【ADR-073】若使用者勾了「記住憑證」,登入成功即把憑證加密存本機,
            # 供下次開程式自動登入 (只在勾選時才落地,且是加密的)。
            try:
                if getattr(self, 'remember_creds_var', None) and self.remember_creds_var.get():
                    self.safe_after(0, self._save_secure_creds, dict(self._login_creds_mem))
            except Exception:
                pass
            self.safe_after(0, lambda: self.btn_login.config(text="🔓 登出券商 API", bg="#FF1744", fg="white"))
            self.safe_after(0, lambda: self.lbl_api_status.config(text="🟢 券商 API 已連線 (實盤模式)", fg="#00E676"))
            self.safe_after(0, self.log_message, "【登入成功】連線建立完成，合約下載完畢，實盤功能已啟用！")
            self.safe_after(1000, self.start_fetch_thread)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【API 登入後初始化失敗】: {e}")

    # ================= 主副圖設定 =================
    def open_chart_layout_dialog(self):
        """
        【使用者調整#1】圖表邊界調整對話框。前兩次都是 Claude 猜測 subplots_adjust
        的邊界比例，都沒有解決留白問題；使用者明確要求「讓我自己調整,調整好
        就鎖定,你就不用一直在猜」。這裡用一組滑桿即時調整邊界比例與畫布像素
        微調值,拖動滑桿會立即重繪 (debounce 150ms 避免拖曳過程中過度頻繁重繪),
        「儲存目前設定」寫入 chart_layout.json 持久化保存 (下次啟動自動套用),
        「還原預設值」則重置回程式內建的起始值。
        """
        dlg = tk.Toplevel(self)
        dlg.title("圖表版面微調")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 420, 340)
        dlg.transient(self)

        tk.Label(dlg, text="拖動滑桿即時預覽 (畫布已自動填滿視窗,這裡調的是圖表四周留白)",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9), wraplength=380, justify="left").pack(pady=(12, 8))

        sliders_frame = tk.Frame(dlg, bg="#1A2026")
        sliders_frame.pack(fill=tk.X, padx=20)

        self._layout_dialog_after_id = None

        def _on_slider_change(_=None):
            # debounce:拖曳過程中連續觸發，取消前一個排程只在停下來 150ms 後才真的重繪
            if self._layout_dialog_after_id is not None:
                try: self.after_cancel(self._layout_dialog_after_id)
                except Exception: pass
            self._layout_dialog_after_id = self.safe_after(150, _apply_preview)

        def _apply_preview():
            self.chart_layout['margin_left'] = var_left.get()
            self.chart_layout['margin_right'] = var_right.get()
            self.chart_layout['margin_top'] = var_top.get()
            self.chart_layout['margin_bottom'] = var_bottom.get()
            self.chart_layout['hspace'] = var_hspace.get()
            # 【第五輪修正】原本每拖一次滑桿就呼叫 self.trigger_redraw() 做「完整重繪」
            # (砍掉整個 canvas、重算所有技術指標、重建 FigureCanvasTkAgg)，這對一張
            # 有多個副圖的即時圖來說很重，加上 150ms debounce 會在拖曳過程中一直
            # 被 after_cancel 取消，導致拖曳當下幾乎看不到任何變化——使用者實測
            # 回報「完全沒有反應」。而且更根本的問題是:過去用 subplots_adjust 對
            # mplfinance 的 add_axes 面板無效。這次改成:如果目前已經有繪好的
            # figure/canvas，就用 self._apply_chart_margins() 對每個面板軸域直接
            # set_position() 重新定位 + draw_idle()，不重算指標、不重建 canvas，
            # 邊界變化會即時、順暢、而且「真的有效」地反映在畫面上。只有在還沒
            # 任何 figure 時才退回完整重繪。
            fig = getattr(self, 'current_fig', None)
            canvas = getattr(self, 'current_canvas', None)
            axlist = getattr(self, 'axlist', None)
            if fig is not None and canvas is not None and axlist:
                try:
                    self._apply_chart_margins(fig, axlist, getattr(self, 'current_panel_ratios', [5, 1.2]))
                    canvas.draw_idle()
                except Exception as e:
                    self.log_message(f"【版面微調預覽異常】{e}")
            else:
                self.trigger_redraw()

        def _make_slider(label, frm, to, resolution, initial):
            row = tk.Frame(sliders_frame, bg="#1A2026")
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=label, bg="#1A2026", fg="white", width=12, anchor="w").pack(side=tk.LEFT)
            var = tk.DoubleVar(value=initial)
            scale = tk.Scale(row, from_=frm, to=to, resolution=resolution, orient=tk.HORIZONTAL,
                              variable=var, bg="#1A2026", fg="white", troughcolor="#2A323D",
                              highlightthickness=0, command=_on_slider_change, length=220)
            scale.pack(side=tk.LEFT)
            return var

        var_left = _make_slider("左邊界", 0.0, 0.20, 0.005, self.chart_layout['margin_left'])
        var_right = _make_slider("右邊界", 0.80, 1.0, 0.005, self.chart_layout['margin_right'])
        var_top = _make_slider("上邊界", 0.80, 0.99, 0.005, self.chart_layout['margin_top'])
        var_bottom = _make_slider("下邊界", 0.02, 0.25, 0.005, self.chart_layout['margin_bottom'])
        var_hspace = _make_slider("面板間距", 0.02, 0.35, 0.01, self.chart_layout['hspace'])
        # 【第五輪修正】原本這裡還有「寬度微調(px)」「高度微調(px)」兩條滑桿，
        # 但它們是壞的:draw_chart 裡 canvas_widget.config(width,height) 設定的像素
        # 尺寸，會馬上被緊接著的 pack(fill=BOTH, expand=True) 覆蓋掉 (fill+expand
        # 會強制 widget 撐滿容器,忽略 config 指定的尺寸)，所以不管怎麼拉都沒有
        # 效果——使用者實測把兩條都拉到最大 150 完全沒反應,就是這個原因。畫布
        # 本來就會自動填滿 chart_frame 容器 (這正是我們要的)，真正控制圖表四周
        # 留白的是上面那幾條「邊界」滑桿 (subplots_adjust)，所以這兩條多餘且會
        # 誤導,直接移除。

        btn_frame = tk.Frame(dlg, bg="#1A2026")
        btn_frame.pack(fill=tk.X, padx=20, pady=15)

        def _save():
            config_store.save_chart_layout(self.chart_layout_file, self.chart_layout)
            self.log_message("【版面微調】目前設定已儲存,下次啟動會自動套用。")

        def _reset_defaults():
            defaults = config_store.DEFAULT_CHART_LAYOUT
            var_left.set(defaults['margin_left']); var_right.set(defaults['margin_right'])
            var_top.set(defaults['margin_top']); var_bottom.set(defaults['margin_bottom'])
            var_hspace.set(defaults['hspace'])
            _apply_preview()

        tk.Button(btn_frame, text="還原預設值", bg="#2A323D", fg="white", relief="flat",
                  command=_reset_defaults).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(btn_frame, text="儲存目前設定", bg="#00E676", fg="black", font=('微軟正黑體', 10, 'bold'),
                  relief="flat", command=_save).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(btn_frame, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  command=dlg.destroy).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

    # ================= 【ADR-057】量化交易面板 (分頁精簡版 + 獨立視窗完整版) =================
    QT_COLS = ("name", "symbol", "symbol_name", "tf", "direction", "conds", "mode", "acct", "status", "running", "today", "pos", "unreal")
    QT_HEADINGS = {"name": "策略名稱", "symbol": "商品代號", "symbol_name": "商品名稱", "tf": "週期", "direction": "方向",
                   "conds": "進場條件", "mode": "模式", "acct": "模擬帳戶", "status": "狀態", "running": "運轉狀態",
                   "today": "今日次數", "pos": "持倉", "unreal": "未實現損益"}

    def _build_quant_panel(self, parent, tree_height=4, compact=False):
        """把整套量化交易 UI 建在 parent 底下,並登記到 self._qt_uis。

        同一份 UI 可以同時存在於「底部分頁 (compact=True,精簡)」與
        「獨立視窗 (compact=False,完整)」;所有更新方法 (_qt_refresh_tree /
        _qt_update_status_label / log_message 鏡射) 都改成走 self._qt_uis
        逐一更新,兩邊永遠同步,不會出現「視窗改了分頁沒變」的不一致。
        回傳這份 UI 的 dict。"""
        ui = {'root': parent, 'compact': compact}

        bar = tk.Frame(parent, bg="#1A2026")
        bar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 0))
        ui['status'] = tk.Label(bar, text="🔴 自動交易未啟動 (安全)", bg="#1A2026",
                                fg="#FF5252", font=('微軟正黑體', 10, 'bold'))
        ui['status'].pack(side=tk.LEFT, padx=(2, 10))
        ui['arm'] = tk.Button(bar, text="🟢 啟動自動交易 (需確認)", bg="#00C853", fg="black",
                              relief="flat", font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
                              command=self._qt_open_arm_dialog)
        ui['arm'].pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="⛔ 全部停止", bg="#FF1744", fg="white",
                  relief="flat", font=('微軟正黑體', 9, 'bold'), padx=12, pady=2,
                  command=self._qt_stop_all).pack(side=tk.LEFT, padx=6)
        tk.Button(bar, text="💰 模擬帳戶", bg="#FFB300", fg="black",
                  relief="flat", font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
                  command=self._qt_open_paper_window).pack(side=tk.LEFT, padx=6)
        tk.Button(bar, text="📊 全部帳戶", bg="#26C6DA", fg="black",
                  relief="flat", font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
                  command=self._qt_open_all_accounts_window).pack(side=tk.LEFT, padx=6)
        tk.Button(bar, text="📱 Telegram通知", bg="#26A5E4", fg="white",
                  relief="flat", font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
                  command=self._qt_open_telegram_settings).pack(side=tk.LEFT, padx=6)
        if compact:
            # 分頁版:最重要的是「把完整視窗叫出來」這顆按鈕
            tk.Button(bar, text="🗔 開啟量化交易視窗 (完整畫面)", bg="#7E57C2", fg="white",
                      relief="flat", font=('微軟正黑體', 9, 'bold'), padx=12, pady=2,
                      command=self.open_quant_window).pack(side=tk.LEFT, padx=10)
            
            # 將最新一行系統訊息鏡射移到上方 bar，節省底部空間
            ui['lastlog'] = tk.Label(bar, text="", bg="#1A2026", fg="#8A99AD",
                                     font=('微軟正黑體', 8), anchor='w')
            ui['lastlog'].pack(side=tk.LEFT, padx=(12, 2), fill=tk.X, expand=True)
            
            # 分頁版不建立底部的 8 顆策略操作按鈕，讓出空間放大圖表！
        else:
            tk.Label(bar, text="每次開啟程式總開關都是關閉;新策略預設「模擬」→ 成交記入模擬帳戶",
                     bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=8)

            # 按鈕列先 pack 且 side=BOTTOM:高度不足時優先保留按鈕列可見 (P-44)
            btns = tk.Frame(parent, bg="#1A2026")
            btns.pack(side=tk.BOTTOM, fill=tk.X, padx=4, pady=(2, 3))
            for txt, bg, cmd in (("➕ 新增策略", "#29B6F6", self._qt_new_strategy),
                                  ("✏️ 編輯", "#FB8C00", self._qt_edit_strategy),
                                  ("🗑 刪除", "#5A6472", self._qt_delete_strategy),
                                  ("⬆ 上移", "#546E7A", lambda: self._qt_move_strategy(-1)),
                                  ("⬇ 下移", "#546E7A", lambda: self._qt_move_strategy(1)),
                                  ("▶ 啟用", "#00C853", lambda: self._qt_set_enabled(True)),
                                  ("⏸ 停用", "#8A99AD", lambda: self._qt_set_enabled(False)),
                                  ("🔬 回測", "#AB47BC", self._qt_backtest_selected),
                                  ("🎯 參數最佳化", "#00ACC1", self._qt_optimize_selected),
                                  ("📊 策略比較", "#7E57C2", self._qt_compare_dialog)):
                fg = "white" if bg in ("#5A6472", "#AB47BC", "#7E57C2", "#546E7A") else "black"
                tk.Button(btns, text=txt, bg=bg, fg=fg, relief="flat",
                          font=('微軟正黑體', 9, 'bold'), padx=10, pady=3, command=cmd).pack(side=tk.LEFT, padx=2)
            
            # 完整視窗版的系統訊息鏡射
            ui['lastlog'] = tk.Label(btns, text="", bg="#1A2026", fg="#8A99AD",
                                     font=('微軟正黑體', 8), anchor='w')
            ui['lastlog'].pack(side=tk.LEFT, padx=(12, 2), fill=tk.X, expand=True)

        tree_frame = tk.Frame(parent, bg="#1A2026")
        tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=2)
        tree = ttk.Treeview(tree_frame, columns=self.QT_COLS, show="headings",
                            height=tree_height, style='Trades.Treeview')
        widths = ({"name": 110, "symbol": 70, "symbol_name": 100, "tf": 55, "direction": 50, "conds": 240,
                   "mode": 50, "acct": 80, "status": 70, "running": 80, "today": 65, "pos": 90, "unreal": 75} if compact else
                  {"name": 180, "symbol": 120, "symbol_name": 150, "tf": 70, "direction": 80, "conds": 460,
                   "mode": 70, "acct": 110, "status": 80, "running": 100, "today": 80, "pos": 140, "unreal": 90})
        for c in self.QT_COLS:
            tree.heading(c, text=self.QT_HEADINGS[c])
            tree.column(c, width=widths[c], anchor="center")
        tree.tag_configure('qt_on', foreground='#FF1744', background='#12161A')     # 實單紅
        tree.tag_configure('qt_sim', foreground='#29B6F6', background='#12161A')    # 模擬藍
        tree.tag_configure('qt_off', foreground='#8A99AD', background='#12161A')    # 停用灰
        tree.tag_configure('qt_mismatch', foreground='#FFB300', background='#2A1F0A')  # 持倉與帳戶不同步警示黃
        sb = tk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ui['tree'] = tree

        self._qt_uis.append(ui)
        # 向下相容:既有程式碼 (以及未來的 diag 腳本) 仍可能直接讀這些屬性,
        # 一律指向「目前最後建立的那份」面板。
        self.tree_quant = tree
        self.lbl_qt_status = ui['status']
        self.btn_qt_arm = ui['arm']
        self.lbl_qt_last_log = ui['lastlog']
        return ui

    def _qt_alive_uis(self):
        """回傳仍然存活 (widget 還沒被銷毀) 的量化面板,順手清掉死掉的。
        獨立視窗關閉後,它那份 UI 的 widget 全部失效,必須從清單移除,
        否則之後每次 refresh 都會對已銷毀的 widget 操作而拋 TclError。"""
        alive = []
        for ui in self._qt_uis:
            try:
                if ui['tree'].winfo_exists():
                    alive.append(ui)
            except Exception:
                pass
        self._qt_uis = alive
        return alive

    def _qt_primary_ui(self):
        """取「使用者目前實際在操作」的那份面板:獨立視窗開著就用它,
        否則用底部分頁。選取狀態 (_qt_selected) 必須看這一份,不然使用者
        在視窗裡點的策略,會被分頁那份空的選取狀態蓋掉。"""
        uis = self._qt_alive_uis()
        if not uis:
            return None
        for ui in uis:
            if not ui.get('compact'):
                return ui
        return uis[0]

    def _qt_dialog_parent(self):
        """【修正】量化交易面板 (_build_quant_panel) 同時可能嵌在主視窗底部
        分頁、也可能嵌在獨立視窗 (open_quant_window),同一段程式碼兩邊共用。
        從這段共用程式碼跳出的 messagebox 若寫死 parent=self (主視窗根),
        使用者實際在看的卻是獨立視窗時,訊息框會以主視窗為中心產生、可能被
        獨立視窗擋在後面而『看不見』——但 messagebox 是整個應用程式共用的
        modal grab,看不見不代表沒有跳出來,使用者會覺得所有視窗 (含獨立
        量化視窗的關閉鈕) 通通沒反應,像是卡死關不掉,其實只是有一個藏在
        後面的訊息框在等你按確定。回傳目前真正該當 parent 的視窗:獨立
        視窗還開著就用它,否則用主視窗。"""
        win = getattr(self, '_quant_win', None)
        try:
            if win is not None and win.winfo_exists():
                return win
        except Exception:
            pass
        return self

    def open_quant_window(self):
        """【ADR-057】開啟量化交易獨立視窗 (完整畫面)。

        已經開著就只把它帶到最前面,不重複建立 (重複建立會讓 _qt_uis 累積
        多份指向同一個視窗的 UI,refresh 時做白工)。"""
        try:
            if self._quant_win is not None and self._quant_win.winfo_exists():
                self._quant_win.deiconify(); self._quant_win.lift(); self._quant_win.focus_force()
                return
        except Exception:
            pass
        win = tk.Toplevel(self)
        self._quant_win = win
        win.title("🤖 量化交易 — 策略管理 / 回測 / 參數最佳化")
        win.configure(bg="#1A2026")
        # 開得夠大才有意義 (這正是使用者要獨立視窗的理由:分頁空間太小);
        # 但不要超過螢幕可視範圍,否則按鈕列會被推出畫面外 (P-44 的變形)。
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w, h = min(1500, sw - 80), min(850, sh - 100)
            win.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}")
        except Exception:
            win.geometry("1400x800")
        tk.Label(win, text="策略清單 (可調整視窗大小;此視窗關閉不影響已啟動的自動交易)",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).pack(anchor='w', padx=8, pady=(6, 0))
        body = tk.Frame(win, bg="#1A2026")
        body.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._build_quant_panel(body, tree_height=20, compact=False)

        def _on_close():
            # 只關視窗、不動 runner:自動交易是否運轉由總開關決定,與這個
            # 視窗的開關完全無關 (使用者明確要求的行為)。
            try:
                self._qt_uis = [u for u in self._qt_uis if u.get('root') is not body]
            except Exception:
                pass
            self._quant_win = None
            try:
                win.destroy()
            except Exception:
                pass
            # 視窗銷毀後,把 self.tree_quant 等相容屬性指回分頁那一份
            prim = self._qt_primary_ui()
            if prim:
                self.tree_quant = prim['tree']
                self.lbl_qt_status = prim['status']
                self.btn_qt_arm = prim['arm']
                self.lbl_qt_last_log = prim['lastlog']
            self._qt_update_status_label()
        win.protocol("WM_DELETE_WINDOW", _on_close)
        self._qt_refresh_tree()
        self._qt_update_status_label()
        self.log_message("【量化交易】已開啟獨立視窗 (關閉此視窗不會停止自動交易)。")

    # ======================================================================
    # 【ADR-116】籌碼 / 選股:開啟完整視窗
    #
    # 底部分頁的高度只夠顯示兩三列,而這兩個功能的輸出動輒數十列 —— 使用者
    # 得一直捲動,等於看不到全貌。量化交易早就有獨立視窗 (ADR-057),這裡把
    # 同樣的能力補給籌碼與選股。
    #
    # 【與量化交易不同的作法:搬家,不是複製】量化面板支援「分頁一份、視窗
    # 一份」同時存在,代價是要維護 _qt_uis 清單、每次刷新都要走訪所有存活的
    # UI。籌碼/選股的建構函式把 widget 直接掛在 self.*(單一實例設計),硬要
    # 複製會讓分頁那份變成孤兒 —— self.tree_sc 指到視窗那份,分頁的表格就再
    # 也不會更新,而且看不出哪裡壞掉。
    #
    # 所以改成「搬家」:開視窗時把分頁的內容拆掉、在視窗裡重建;關視窗時再
    # 搬回分頁。永遠只有一份 panel,self.* 永遠指向它,沒有孤兒的可能。
    # 代價是重建會清空表格,因此各自提供 restore 把最後的內容重播回來。
    # ======================================================================
    PANEL_WINDOWS = {
        'chips': {
            'title': '📊 籌碼分析 — 個股法人 / 期貨法人 / 大盤法人 / 融資融券',
            'hint': '資料全部讀本機檔案,開關此視窗不會觸發下載。',
            'size': (1400, 800),
        },
        'screener': {
            'title': '🔍 選股 — 技術面 / 籌碼 / 基本面 條件篩選與回測',
            'hint': '關閉此視窗會把面板搬回底部分頁,選股結果會一併帶回。',
            'size': (1500, 820),
        },
    }

    def _panel_close_for_test(self, kind):
        """取得某個面板視窗的關閉函式 (診斷用;正式路徑走 WM_DELETE_WINDOW)。"""
        return self._panel_closers.get(kind, lambda: None)

    def _panel_spec(self, kind):
        """回傳 (分頁容器, 建構函式, 還原內容的函式)。"""
        if kind == 'chips':
            return (self.chips_tab_frame, self._build_chips_panel,
                    self._chips_restore_view)
        return (self.screener_tab_frame, self._build_screener_panel,
                self._sc_restore_view)

    def _chips_restore_view(self):
        """搬家後重播籌碼表格 —— 純讀本機檔案,不會觸發下載 (ADR-100 的保證)。"""
        try:
            self._chips_refresh_view()
        except Exception:
            pass

    def _sc_restore_view(self):
        """搬家後重播選股/回測結果。沒跑過就維持空表。"""
        kind_args = getattr(self, '_sc_last_render', None)
        if not kind_args:
            return
        kind, args = kind_args
        try:
            if kind == 'screen':
                self._sc_fill_tree(*args)
            else:
                self._sc_show_backtest(*args)
        except Exception:
            pass

    def _clear_children(self, frame):
        for ch in list(getattr(frame, 'winfo_children', lambda: [])()):
            try:
                ch.destroy()
            except Exception:
                pass

    def open_panel_window(self, kind):
        """把籌碼/選股面板搬到獨立視窗。已開著就帶到最前面。"""
        wins = getattr(self, '_panel_wins', None)
        if wins is None:
            wins = self._panel_wins = {}
        if not hasattr(self, '_panel_closers'):
            self._panel_closers = {}
        win = wins.get(kind)
        if win is not None:
            # 「還活著嗎」與「帶到最前面」要分開包 try:合在一起的話,只要
            # deiconify/lift 任何一個失敗,就會掉進下面的「新建」分支而開出
            # 第二個視窗 —— 兩個視窗各有一份 panel,self.* 只指向其中一個,
            # 另一個變成永遠不會更新的孤兒 (ADR-057 提過的同一類問題)。
            alive = False
            try:
                alive = bool(win.winfo_exists())
            except Exception:
                alive = False
            if alive:
                for _fn in ('deiconify', 'lift', 'focus_force'):
                    try:
                        getattr(win, _fn)()
                    except Exception:
                        pass
                return win

        spec = self.PANEL_WINDOWS[kind]
        tab_frame, build, restore = self._panel_spec(kind)

        win = tk.Toplevel(self)
        wins[kind] = win
        win.title(spec['title'])
        win.configure(bg="#1A2026")
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            pw, ph = spec['size']
            w, h = min(pw, sw - 80), min(ph, sh - 100)
            win.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}")
        except Exception:
            win.geometry("1400x800")

        # 分頁那邊換成說明文字,讓使用者知道面板去哪了 (不是壞掉)
        self._clear_children(tab_frame)
        tk.Label(tab_frame,
                 text=f"此面板已在獨立視窗開啟。\n關閉那個視窗就會搬回這裡。\n\n{spec['hint']}",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 11),
                 justify='center').pack(expand=True)

        body = tk.Frame(win, bg="#1A2026")
        body.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        build(body)
        restore()

        def _on_close():
            # 搬回分頁:先拆視窗內容,再在分頁重建,最後重播內容。
            try:
                self._clear_children(tab_frame)
                build(tab_frame)
                restore()
            except Exception as e:
                self.log_message(f"【面板】搬回分頁時發生問題: {type(e).__name__}: {e}")
            self._panel_wins.pop(kind, None)
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_close)
        # 診斷腳本沒有真正的視窗管理員可以按「X」,留一個取得關閉函式的入口,
        # 讓「搬出去→搬回來」這條路徑能被完整測到 (不是為了 production 用)。
        self._panel_closers[kind] = _on_close
        self.log_message(f"【面板】已開啟{'籌碼' if kind == 'chips' else '選股'}獨立視窗"
                         f" (關閉後會自動搬回底部分頁)。")
        return win

    def _gc_tick(self):
        """【ADR-057】主執行緒定期循環回收。

        自動 GC 已在 __init__ 關閉 (原因見該處註解:避免 tk 物件的 __del__ 在
        背景執行緒被觸發而讓 Tcl abort 整個行程)。這裡固定在主執行緒回收,
        讓所有 tkinter 循環垃圾的 __del__ 都在主執行緒執行。
        回收本身很快 (毫秒等級),但仍包在 try 裡,絕不讓它影響主迴圈。"""
        try:
            gc.collect()
        except Exception:
            pass
        finally:
            if not self._closing:
                self.safe_after(self._gc_interval_ms, self._gc_tick)

    def _collect_indicator_settings(self):
        """把目前所有主/副圖指標 tk.Variable 的值收集成一份可存檔的 dict。"""
        return {
            'ma_shows': [v.get() for v in self.ma_shows],
            'ma_types': [v.get() for v in self.ma_types],
            'ma_periods': [v.get() for v in self.ma_periods],
            'ma_colors': [v.get() for v in self.ma_colors],
            'bb_show': self.bb_show.get(), 'bb_color': self.bb_color.get(),
            # 【ADR-138】上線 σ / 下線 σ 分開兩個 key
            'bb_period': self.bb_period.get(),
            'bb_std_up': self.bb_std_up.get(), 'bb_std_dn': self.bb_std_dn.get(),
            # 【ADR-131】中線類型 + 第2組完整布林
            'bb_type': self.bb_type.get(),
            'bb2_show': self.bb2_show.get(), 'bb2_color': self.bb2_color.get(),
            'bb2_type': self.bb2_type.get(), 'bb2_period': self.bb2_period.get(),
            'bb2_std_up': self.bb2_std_up.get(), 'bb2_std_dn': self.bb2_std_dn.get(),
            # 【ADR-133】黃金切割律
            'fib_show': self.fib_show.get(), 'fib_lookback': self.fib_lookback.get(),
            'fib_color': self.fib_color.get(), 'fib_ratios': list(self.fib_ratios),
            'var_bbw': self.var_bbw.get(),
            'var_macd': self.var_macd.get(), 'macd_f': self.macd_f.get(),
            'macd_s': self.macd_s.get(), 'macd_sig': self.macd_sig.get(),
            'var_rsi': self.var_rsi.get(), 'rsi_p': self.rsi_p.get(),
            'var_kdj': self.var_kdj.get(), 'kd_n': self.kd_n.get(),
            'kd_m1': self.kd_m1.get(), 'kd_m2': self.kd_m2.get(),
            'var_dmi': self.var_dmi.get(), 'dmi_n': self.dmi_n.get(),
            # 【ADR-134】KDJ 超買/超賣線 + JAE 指標
            'kd_ob': self.kd_ob.get(), 'kd_os': self.kd_os.get(),
            'var_jae': self.var_jae.get(),
            'jae_params': {k: v.get() for k, v in self.jae_params.items()},
        }

    def _apply_indicator_settings(self, d):
        """把讀出來的設定 dict 套回 tk.Variable。任何一個欄位缺漏/型別不對都
        個別 try/except 略過該項,絕不因為一項壞掉就讓整批套用失敗。"""
        def _set_list(varlist, vals):
            for i, v in enumerate(vals or []):
                if i < len(varlist):
                    try: varlist[i].set(v)
                    except Exception: pass
        try:
            if d.get('ma_shows'): _set_list(self.ma_shows, d['ma_shows'])
            if d.get('ma_types'): _set_list(self.ma_types, d['ma_types'])
            if d.get('ma_periods'): _set_list(self.ma_periods, d['ma_periods'])
            if d.get('ma_colors'): _set_list(self.ma_colors, d['ma_colors'])
            self.bb_show.set(d.get('bb_show', self.bb_show.get()))
            self.bb_color.set(d.get('bb_color', self.bb_color.get()))
            self.bb_period.set(d.get('bb_period', self.bb_period.get()))
            # 【ADR-138】舊檔的 bb_std1/bb_std2 由 config_store.migrate_indicator_settings()
            # 在讀檔時就換成 bb_std_up/bb_std_dn,這裡只認新 key。
            self.bb_std_up.set(d.get('bb_std_up', self.bb_std_up.get()))
            self.bb_std_dn.set(d.get('bb_std_dn', self.bb_std_dn.get()))
            # 【ADR-131】舊設定檔沒有這幾個 key → 用 .get(預設) 沿用程式碼預設值,
            # 等於「第1組維持原樣、第2組預設關閉」,使用者的既有設定不受影響。
            self.bb_type.set(d.get('bb_type', self.bb_type.get()))
            self.bb2_show.set(d.get('bb2_show', self.bb2_show.get()))
            self.bb2_color.set(d.get('bb2_color', self.bb2_color.get()))
            self.bb2_type.set(d.get('bb2_type', self.bb2_type.get()))
            self.bb2_period.set(d.get('bb2_period', self.bb2_period.get()))
            self.bb2_std_up.set(d.get('bb2_std_up', self.bb2_std_up.get()))
            self.bb2_std_dn.set(d.get('bb2_std_dn', self.bb2_std_dn.get()))
            # 【ADR-133】舊設定檔沒有這幾個 key → 沿用預設 (黃金切割預設關閉)
            self.fib_show.set(d.get('fib_show', self.fib_show.get()))
            self.fib_lookback.set(d.get('fib_lookback', self.fib_lookback.get()))
            self.fib_color.set(d.get('fib_color', self.fib_color.get()))
            if 'fib_ratios' in d:
                self.fib_ratios = list(fibonacci.normalize_ratios(d.get('fib_ratios')))
            self.var_bbw.set(d.get('var_bbw', self.var_bbw.get()))
            self.var_macd.set(d.get('var_macd', self.var_macd.get()))
            self.macd_f.set(d.get('macd_f', self.macd_f.get()))
            self.macd_s.set(d.get('macd_s', self.macd_s.get()))
            self.macd_sig.set(d.get('macd_sig', self.macd_sig.get()))
            self.var_rsi.set(d.get('var_rsi', self.var_rsi.get()))
            self.rsi_p.set(d.get('rsi_p', self.rsi_p.get()))
            self.var_kdj.set(d.get('var_kdj', self.var_kdj.get()))
            self.kd_n.set(d.get('kd_n', self.kd_n.get()))
            self.kd_m1.set(d.get('kd_m1', self.kd_m1.get()))
            self.kd_m2.set(d.get('kd_m2', self.kd_m2.get()))
            self.var_dmi.set(d.get('var_dmi', self.var_dmi.get()))
            self.dmi_n.set(d.get('dmi_n', self.dmi_n.get()))
            # 【ADR-134】舊設定檔沒有這幾個 key → 沿用預設 (JAE 預設關閉)
            self.kd_ob.set(d.get('kd_ob', self.kd_ob.get()))
            self.kd_os.set(d.get('kd_os', self.kd_os.get()))
            self.var_jae.set(d.get('var_jae', self.var_jae.get()))
            _jp = d.get('jae_params')
            if isinstance(_jp, dict):
                for _k, _v in jae.normalize_params(_jp).items():
                    if _k in self.jae_params:
                        self.jae_params[_k].set(str(_v))
        except Exception:
            pass  # 讀檔壞掉不影響已經是程式碼預設值的變數,圖表照樣畫得出來

    def _save_indicator_settings(self):
        """【ADR-056】明確動作觸發的存檔:對話框按「確認並套用」時呼叫,
        不是每次打字或每次畫圖都存檔。下次開啟程式會自動載入這份設定。"""
        config_store.save_indicator_settings(self.indicator_settings_file, self._collect_indicator_settings())

    def open_main_settings(self):
        # 【ADR-131】視窗放大:布林從 1 行變成「標題 + 欄位名 + 兩組」共 4 行,
        # 沿用 400x350 會把「確認並套用」按鈕擠出視窗外 (等於設定存不了)。
        dlg = tk.Toplevel(self); dlg.title("主圖指標參數設定"); dlg.configure(bg="#1A2026"); self.center_window(dlg, 660, 700); dlg.transient(self); dlg.grab_set()
        tk.Label(dlg, text="開關", bg="#1A2026", fg="white").grid(row=0, column=0, pady=10); tk.Label(dlg, text="類型", bg="#1A2026", fg="white").grid(row=0, column=1); tk.Label(dlg, text="週期", bg="#1A2026", fg="white").grid(row=0, column=2); tk.Label(dlg, text="色彩", bg="#1A2026", fg="white").grid(row=0, column=3)
        for i in range(6):
            tk.Checkbutton(dlg, text=f"MA{i+1}", variable=self.ma_shows[i], bg="#1A2026", fg="white", selectcolor="#2A323D").grid(row=i+1, column=0, sticky="w", padx=15, pady=2)
            ttk.Combobox(dlg, textvariable=self.ma_types[i], values=["SMA", "EMA", "WMA"], width=6, state="readonly", style="BlackText.TCombobox").grid(row=i+1, column=1, padx=5)
            tk.Entry(dlg, textvariable=self.ma_periods[i], width=5, bg="#2A323D", fg="white", justify="center").grid(row=i+1, column=2, padx=5)
            ttk.Combobox(dlg, textvariable=self.ma_colors[i], values=list(self.color_map.keys()), width=10, state="readonly", style="BlackText.TCombobox").grid(row=i+1, column=3, padx=5)
        ttk.Separator(dlg, orient='horizontal').grid(row=7, column=0, columnspan=4, sticky='ew', pady=10)
        # 【ADR-131】布林通道:兩組完整通道,每組 = 中線 (類型 + 期間) + 兩條上下線。
        #
        # 這一區原本整個看不見:說明文字那一行被寫成 row=8、columnspan=4,
        # 跟布林的 checkbox / 期間 / σ1 / σ2 **同一個 grid 儲存格**,而 tkinter 的
        # grid 是後放的蓋前放的 —— 於是使用者只看得到說明文字、旁邊露出半截
        # 「(σ2=0不畫第二組)」跟一個沒有標題的色彩下拉 (實機截圖)。
        # 從第一版就是這樣,不是這幾輪改出來的。
        tk.Label(dlg, text="布林通道 (中線 + 上下線,可開兩組)", bg="#1A2026", fg="#00E5FF",
                 font=('微軟正黑體', 9, 'bold')).grid(row=8, column=0, columnspan=4, sticky='w', padx=15)
        bb_hdr = tk.Frame(dlg, bg="#1A2026"); bb_hdr.grid(row=9, column=0, columnspan=4, sticky='w', padx=15)
        # 【ADR-138】原本這兩欄是「上下線σa / 上下線σb」= 內圈那一對 / 外圈那一對,
        # 使用者要的是「上線一個參數、下線一個參數,兩個要分開」。
        for _t, _w in (("開關", 9), ("中線類型", 9), ("中線期間", 9), ("上線σ", 9), ("下線σ", 9), ("色彩", 11)):
            tk.Label(bb_hdr, text=_t, bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8),
                     width=_w, anchor='w').pack(side=tk.LEFT, padx=1)

        def _bb_row(parent, label, v_show, v_type, v_period, v_up, v_dn, v_color):
            fr = tk.Frame(parent, bg="#1A2026")
            tk.Checkbutton(fr, text=label, variable=v_show, bg="#1A2026", fg="white",
                           selectcolor="#2A323D", width=7, anchor='w').pack(side=tk.LEFT)
            ttk.Combobox(fr, textvariable=v_type, values=list(core_indicators.MA_TYPES), width=6,
                         state="readonly", style="BlackText.TCombobox").pack(side=tk.LEFT, padx=(2, 8))
            tk.Entry(fr, textvariable=v_period, width=5, bg="#2A323D", fg="white",
                     justify="center").pack(side=tk.LEFT, padx=(6, 12))
            tk.Entry(fr, textvariable=v_up, width=5, bg="#2A323D", fg="white",
                     justify="center").pack(side=tk.LEFT, padx=(6, 12))
            tk.Entry(fr, textvariable=v_dn, width=5, bg="#2A323D", fg="white",
                     justify="center").pack(side=tk.LEFT, padx=(6, 10))
            ttk.Combobox(fr, textvariable=v_color, values=list(self.color_map.keys()), width=10,
                         state="readonly", style="BlackText.TCombobox").pack(side=tk.LEFT, padx=2)
            return fr

        _bb_row(dlg, "第1組", self.bb_show, self.bb_type, self.bb_period,
                self.bb_std_up, self.bb_std_dn, self.bb_color).grid(
                row=10, column=0, columnspan=4, sticky='w', padx=15, pady=1)
        _bb_row(dlg, "第2組", self.bb2_show, self.bb2_type, self.bb2_period,
                self.bb2_std_up, self.bb2_std_dn, self.bb2_color).grid(
                row=11, column=0, columnspan=4, sticky='w', padx=15, pady=1)
        tk.Label(dlg, text="※ 上線σ 與 下線σ 各自獨立 (可不對稱,例如上 2、下 3);兩組可各自開關,中線類型/期間互相獨立。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).grid(
                 row=12, column=0, columnspan=4, sticky='w', padx=15, pady=(2, 0))
        ttk.Separator(dlg, orient='horizontal').grid(row=13, column=0, columnspan=4, sticky='ew', pady=8)
        # 【ADR-133】黃金切割律 (費波南希回撤)
        tk.Label(dlg, text="黃金切割律 (費波南希回撤)", bg="#1A2026", fg="#FFCA28",
                 font=('微軟正黑體', 9, 'bold')).grid(row=14, column=0, columnspan=4, sticky='w', padx=15)
        fib_top = tk.Frame(dlg, bg="#1A2026"); fib_top.grid(row=15, column=0, columnspan=4, sticky='w', padx=15)
        tk.Checkbutton(fib_top, text="顯示", variable=self.fib_show, bg="#1A2026", fg="white",
                       selectcolor="#2A323D", width=6, anchor='w').pack(side=tk.LEFT)
        tk.Label(fib_top, text="取樣根數", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(8, 2))
        tk.Entry(fib_top, textvariable=self.fib_lookback, width=6, bg="#2A323D", fg="white",
                 justify="center").pack(side=tk.LEFT)
        tk.Label(fib_top, text="色彩", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Combobox(fib_top, textvariable=self.fib_color, values=list(self.color_map.keys()),
                     width=10, state="readonly", style="BlackText.TCombobox").pack(side=tk.LEFT)
        fib_vars = {}
        fib_grid = tk.Frame(dlg, bg="#1A2026"); fib_grid.grid(row=16, column=0, columnspan=4, sticky='w', padx=15, pady=(2, 0))
        _fib_on = set(self.fib_ratios)
        for _i, _r in enumerate(fibonacci.ALL_LEVELS):
            _v = tk.BooleanVar(value=any(abs(_r - o) < 1e-9 for o in _fib_on))
            fib_vars[_r] = _v
            _txt = fibonacci.ratio_label(_r)
            if fibonacci.is_key_ratio(_r):
                _txt += '★'
            elif _r > 1.0:
                _txt += '(延伸)'
            elif _r in fibonacci.SECONDARY_LEVELS:
                _txt += '(次級)'
            tk.Checkbutton(fib_grid, text=_txt, variable=_v, bg="#1A2026", fg="white",
                           selectcolor="#2A323D", font=('微軟正黑體', 8)).grid(
                           row=_i // 5, column=_i % 5, sticky='w', padx=4)
        tk.Label(dlg, text="※ 高低點自動取最近 N 根 (誰比較晚出現就往那個方向量);"
                          "★=0.382/0.618 最常看,次級=台股波浪常用,延伸=跌破/突破起點後的目標。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).grid(
                 row=17, column=0, columnspan=4, sticky='w', padx=15, pady=(2, 0))

        tk.Label(dlg, text="※ 按下方按鈕會記住這些設定,下次開啟程式直接沿用。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).grid(
                 row=18, column=0, columnspan=4, sticky='w', padx=15)

        def _apply_main():
            self.fib_ratios = list(fibonacci.normalize_ratios(
                [r for r, v in fib_vars.items() if v.get()]))
            self._save_indicator_settings()
            self.trigger_redraw()
            if self.fib_show.get():
                self.safe_after(300, self._fib_log_summary)
            dlg.destroy()

        tk.Button(dlg, text="確認並套用 (並記住此設定)", bg="#29B6F6", fg="black", font=('微軟正黑體', 10, 'bold'), command=_apply_main).grid(row=19, column=0, columnspan=4, pady=15)

    # 【ADR-134】副圖指標清單:(顯示名, 開關變數名, [(標籤, 變數名), ...])。
    # 這一份同時決定「整合對話框裡有哪幾列」與「工具列旁邊顯示哪幾個已開啟」,
    # 新增副圖指標時只要動這裡 —— 兩份各自維護遲早分歧 (P-67)。
    SUB_INDICATORS = (
        ('MACD', 'var_macd', (('快線', 'macd_f'), ('慢線', 'macd_s'), ('訊號', 'macd_sig'))),
        ('RSI', 'var_rsi', (('天數', 'rsi_p'),)),
        ('KDJ', 'var_kdj', (('N 天', 'kd_n'), ('M1 (K)', 'kd_m1'), ('M2 (D)', 'kd_m2'),
                            ('超買', 'kd_ob'), ('超賣', 'kd_os'))),
        ('DMI', 'var_dmi', (('週期 (N)', 'dmi_n'),)),
        ('布林通道寬', 'var_bbw', ()),
    )

    def _refresh_sub_active_label(self):
        """工具列上顯示目前開了哪幾個副圖 —— 勾選鈕收進對話框之後,
        不顯示的話使用者得開對話框才知道自己開了什麼。"""
        try:
            on = [name for name, var, _ in self.SUB_INDICATORS
                  if getattr(self, var).get()]
            if self.var_jae.get():
                on.append('JAE')
            self.lbl_sub_active.config(text=("：" + "、".join(on)) if on else "：(未選)")
        except Exception:
            pass

    def open_sub_indicators(self):
        """【ADR-134】副圖技術指標整合視窗:開關 + 參數都在這裡。

        使用者要求「全部整合到一個【副圖技術指標】中,要使用哪一個就到這裡面
        去選擇」,並且明確交代「**只有移動整合,沒有改其他的功能**」——
        所以這裡用的是**原本那些 tk 變數**,計算與繪圖一個字都沒改;
        新增的只有 JAE 這個新指標,以及 KDJ 的超買/超賣參考線。
        """
        dlg = tk.Toplevel(self)
        dlg.title("副圖技術指標")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 620, 560)
        dlg.transient(self); dlg.grab_set()

        tk.Label(dlg, text="勾選要顯示的副圖指標,右邊直接調參數", bg="#1A2026",
                 fg="#FFCA28", font=('微軟正黑體', 10, 'bold')).pack(anchor='w', padx=14, pady=(10, 6))

        body = tk.Frame(dlg, bg="#1A2026"); body.pack(fill=tk.X, padx=14)
        for r, (name, var_name, fields) in enumerate(self.SUB_INDICATORS):
            fr = tk.Frame(body, bg="#1A2026")
            fr.grid(row=r, column=0, sticky='w', pady=3)
            tk.Checkbutton(fr, text=name, variable=getattr(self, var_name),
                           bg="#1A2026", fg="white", selectcolor="#2A323D",
                           font=('微軟正黑體', 9, 'bold'), width=11, anchor='w').pack(side=tk.LEFT)
            for lbl, v_name in fields:
                tk.Label(fr, text=lbl, bg="#1A2026", fg="#8A99AD",
                         font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=(6, 2))
                tk.Entry(fr, textvariable=getattr(self, v_name), width=5, bg="#2A323D",
                         fg="white", justify="center").pack(side=tk.LEFT)
            if not fields:
                tk.Label(fr, text="(無參數;寬度取主圖第1組布林)", bg="#1A2026", fg="#8A99AD",
                         font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=(6, 0))

        # ---- JAE (新指標) ----
        ttk.Separator(dlg, orient='horizontal').pack(fill=tk.X, padx=14, pady=(10, 6))
        jae_fr = tk.LabelFrame(dlg, text=" JAE 指標 (A=RSI／J=KDJ的J／E=長期趨勢線) ",
                               bg="#12181F", fg="#00E5FF", font=('微軟正黑體', 9, 'bold'))
        jae_fr.pack(fill=tk.X, padx=14)
        tk.Checkbutton(jae_fr, text="顯示 JAE 副圖", variable=self.var_jae,
                       bg="#12181F", fg="white", selectcolor="#2A323D",
                       font=('微軟正黑體', 9, 'bold')).grid(row=0, column=0, columnspan=4,
                                                            sticky='w', padx=6, pady=(4, 2))
        _keys = list(jae.DEFAULT_PARAMS.keys())
        for i, k in enumerate(_keys):
            tk.Label(jae_fr, text=jae.PARAM_LABELS[k], bg="#12181F", fg="white",
                     font=('微軟正黑體', 8)).grid(row=1 + i // 2, column=(i % 2) * 2,
                                                  sticky='e', padx=(6, 2), pady=1)
            tk.Entry(jae_fr, textvariable=self.jae_params[k], width=6, bg="#2A323D",
                     fg="white", justify="center").grid(row=1 + i // 2, column=(i % 2) * 2 + 1,
                                                        sticky='w', padx=(0, 10), pady=1)
        tk.Label(jae_fr, text="※ E = A(RSI) 的長期 EMA,跟 A/J 同尺度才交叉得起來;"
                             "趨勢要「E 在中線之上」且「E 這幾根在上升」兩個都成立才算向上。\n"
                             "　 交叉:J×A 短線、A×E 中期、J×E 極短線;"
                             "快線由下往上穿慢線=黃金交叉。判讀用,不會自動下單。",
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 8), justify='left').grid(
                 row=1 + (len(_keys) + 1) // 2, column=0, columnspan=4, sticky='w',
                 padx=6, pady=(4, 6))

        status = tk.Label(dlg, text="", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9))
        status.pack(anchor='w', padx=14, pady=(6, 0))

        def _apply(close=False):
            self._save_indicator_settings()
            self._refresh_sub_active_label()
            self.trigger_redraw()
            if self.var_jae.get():
                self.safe_after(300, self._jae_log_summary)
            if close:
                dlg.destroy()
            else:
                status.config(text="✓ 已套用並儲存", fg="#00E676")

        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(fill=tk.X, padx=14, pady=10)
        tk.Button(btns, text="套用", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=16, pady=3,
                  command=lambda: _apply(False)).pack(side=tk.LEFT)
        tk.Button(btns, text="確認並套用 (並記住此設定)", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=14, pady=3,
                  command=lambda: _apply(True)).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=16, pady=3,
                  command=dlg.destroy).pack(side=tk.RIGHT)

    def _jae_params_now(self):
        """從 tk 變數讀出 JAE 參數 (值域由 core 夾)。"""
        return jae.normalize_params({k: v.get() for k, v in self.jae_params.items()})

    def _jae_log_summary(self):
        """把 JAE 的現值、趨勢與最近的交叉寫進系統日誌。"""
        df = getattr(self, 'full_calculated_df', None)
        if df is None or jae.COL_A not in getattr(df, 'columns', []):
            return
        r = jae.evaluate(df, self._jae_params_now(), lookback=3)
        if r:
            self.log_message(f"【JAE】{jae.summarize(r)}")

    def open_sub_settings(self, ind):
        """【ADR-134 起不再由工具列呼叫】保留單一指標的參數視窗:
        整合視窗才是入口,但這個函式沒有壞掉的理由,留著給日後需要時用。"""
        dlg = tk.Toplevel(self); dlg.title(f"{ind} 參數設定"); dlg.configure(bg="#1A2026"); self.center_window(dlg, 250, 180); dlg.transient(self); dlg.grab_set()
        frame = tk.Frame(dlg, bg="#1A2026"); frame.pack(expand=True, pady=10)
        if ind == "MACD":
            tk.Label(frame, text="快線:", bg="#1A2026", fg="white").grid(row=0, column=0, pady=5); tk.Entry(frame, textvariable=self.macd_f, width=5, bg="#2A323D", fg="white").grid(row=0, column=1)
            tk.Label(frame, text="慢線:", bg="#1A2026", fg="white").grid(row=1, column=0, pady=5); tk.Entry(frame, textvariable=self.macd_s, width=5, bg="#2A323D", fg="white").grid(row=1, column=1)
            tk.Label(frame, text="訊號:", bg="#1A2026", fg="white").grid(row=2, column=0, pady=5); tk.Entry(frame, textvariable=self.macd_sig, width=5, bg="#2A323D", fg="white").grid(row=2, column=1)
        elif ind == "RSI":
            tk.Label(frame, text="天數:", bg="#1A2026", fg="white").grid(row=0, column=0, pady=5); tk.Entry(frame, textvariable=self.rsi_p, width=5, bg="#2A323D", fg="white").grid(row=0, column=1)
        elif ind == "KDJ":
            tk.Label(frame, text="N 天:", bg="#1A2026", fg="white").grid(row=0, column=0, pady=5); tk.Entry(frame, textvariable=self.kd_n, width=5, bg="#2A323D", fg="white").grid(row=0, column=1)
            tk.Label(frame, text="M1 (K):", bg="#1A2026", fg="white").grid(row=1, column=0, pady=5); tk.Entry(frame, textvariable=self.kd_m1, width=5, bg="#2A323D", fg="white").grid(row=1, column=1)
            tk.Label(frame, text="M2 (D):", bg="#1A2026", fg="white").grid(row=2, column=0, pady=5); tk.Entry(frame, textvariable=self.kd_m2, width=5, bg="#2A323D", fg="white").grid(row=2, column=1)
        elif ind == "DMI":
            tk.Label(frame, text="週期 (N):", bg="#1A2026", fg="white").grid(row=0, column=0, pady=5); tk.Entry(frame, textvariable=self.dmi_n, width=5, bg="#2A323D", fg="white").grid(row=0, column=1)
        tk.Label(dlg, text="※ 套用後會記住,下次開啟沿用。", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack()
        tk.Button(dlg, text="確認並套用 (並記住此設定)", bg="#29B6F6", fg="black", font=('微軟正黑體', 10, 'bold'), command=lambda: [self._save_indicator_settings(), self.trigger_redraw(), dlg.destroy()]).pack(pady=10)

    # ================= 【ADR-011】fetch_taiwan_chips 已移除 =================
    # 法人買賣超/資券餘額的唯一資料來源是 FinMind，FinMind 已停用，
    # 台股資料改為一律使用 shioaji，故此功能連同「法人」「資券」副圖
    # checkbox 一併移除。若之後想恢復，需要先找到 shioaji 或其他資料源
    # 能提供這兩項籌碼資料，並另開一筆 ADR 記錄新的資料來源。

    def calculate_custom_indicators(self, df):
        # 【ADR-009】實際運算移到 core/indicators.py,這裡只負責從 tkinter Variable
        # 讀值 (.get()) 轉成純值傳進去,運算邏輯本身完全沒有 tkinter 依賴。
        ma_flags = [v.get() for v in self.ma_shows]
        ma_types = [v.get() for v in self.ma_types]
        ma_periods = [v.get() for v in self.ma_periods]
        common_kwargs = dict(
            bb_show=self.bb_show.get(), bbw_show=self.var_bbw.get(),
            macd_show=self.var_macd.get(), macd_f=self.macd_f.get(), macd_s=self.macd_s.get(), macd_sig=self.macd_sig.get(),
            rsi_show=self.var_rsi.get(), rsi_p=self.rsi_p.get(),
            kdj_show=self.var_kdj.get(), kd_n=self.kd_n.get(), kd_m1=self.kd_m1.get(), kd_m2=self.kd_m2.get(),
            dmi_show=self.var_dmi.get(), dmi_n=self.dmi_n.get())

        SJ_DAYS = {"1分K": 15, "5分K": 45, "15分K": 90, "30分K": 120, "60分K": 180,
                   "日K": 7300, "周K": 180, "月K": 180}
        QUICK_DAYS = {"日K": 45, "周K": 240, "月K": 600}   # 快取第一段小範圍

        try:
            out = core_indicators.calculate_indicators(
                df, ma_flags, ma_types, ma_periods,
                bb_period=self.bb_period.get(),
                bb_std_up=self.bb_std_up.get(), bb_std_dn=self.bb_std_dn.get(),
                bb_type=self.bb_type.get(),
                bb2_show=self.bb2_show.get(), bb2_period=self.bb2_period.get(),
                bb2_std_up=self.bb2_std_up.get(), bb2_std_dn=self.bb2_std_dn.get(),
                bb2_type=self.bb2_type.get(),
                **common_kwargs)
            return self._attach_jae(out)
        except TypeError:
            # 【第十輪修正 問題1】使用者機器上的 core/indicators.py 若還是舊版
            # (沒有 bb_period 等新參數),新主程式傳新參數會 TypeError,導致
            # 「整張 K 線圖畫不出來」。這裡降級改用舊簽名呼叫 (布林回到固定
            # 20/2),圖表照常運作,並明確提示使用者要把新版 indicators.py
            # 放到 core\ 資料夾——絕不能因為一個附屬模組版本舊,就讓主功能全掛。
            if not getattr(self, '_bb_param_warned', False):
                self._bb_param_warned = True
                self.log_message("【版本提示】core/indicators.py 是舊版:布林自訂參數暫不生效 (固定20,2)。"
                                 "請把新版 indicators.py 覆蓋到 G:/StockBuild/core/indicators.py 後重啟。")
            return self._attach_jae(core_indicators.calculate_indicators(
                df, ma_flags, ma_types, ma_periods, **common_kwargs))

    def _attach_jae(self, df):
        """【ADR-134】JAE 沒開就完全不算 (省時間,也不會在 df 裡多塞欄位)。

        算不出來只記一次日誌就放過 —— 一個副圖指標壞掉不可以害整張 K 線圖
        畫不出來 (同支撐壓力/黃金切割的處理)。"""
        try:
            if self.var_jae.get():
                jae.compute(df, self._jae_params_now())
        except Exception as e:
            if not getattr(self, '_jae_warned', False):
                self._jae_warned = True
                self.log_message(f"【JAE】計算失敗 ({type(e).__name__}: {e}),本次略過。")
        return df

    # ================= 🚀 大盤與櫃買指數 API 數據滿載化 =================
    def fetch_market_indices_worker(self):
        while True:
            if self._closing:
                return  # 【ADR-012】視窗已關閉,提早結束這條 daemon thread,不再繼續迴圈或排程更新
            try:
                if self.api_logged_in and HAS_SJ:
                    try:
                        # 【ADR-114】1.7 指數代碼改成 IX0001/IX0002,存取形狀也不同,
                        # 統一走 adapter 的候選解析,1.5.6 與 1.7 都能拿到。
                        c_twii = self.brokers['sinopac'].index_contract('TSE')
                        c_twoii = self.brokers['sinopac'].index_contract('OTC')
                        
                        if c_twii and c_twoii:
                            snaps = self.sj_api.snapshots([c_twii, c_twoii])
                            if snaps and len(snaps) == 2:
                                # 加權指數資訊整合
                                s1 = snaps[0]
                                c1, p1, r1 = float(s1.close), float(s1.change_price), float(s1.change_rate)
                                amt1 = float(s1.total_amount) / 100000000 
                                color1 = "#FF1744" if p1 > 0 else ("#00E676" if p1 < 0 else "white")
                                text1 = f"加權指數: {c1:.2f}  漲跌: {p1:+.2f} ({r1:+.2f}%)  總成交量: {amt1:,.1f} 億"
                                self.safe_after(0, lambda t=text1, c=color1: self.lbl_twii.config(text=t, fg=c))
                                
                                # 櫃買指數資訊整合
                                s2 = snaps[1]
                                c2, p2, r2 = float(s2.close), float(s2.change_price), float(s2.change_rate)
                                amt2 = float(s2.total_amount) / 100000000
                                color2 = "#FF1744" if p2 > 0 else ("#00E676" if p2 < 0 else "white")
                                text2 = f"櫃買指數: {c2:.2f}  漲跌: {p2:+.2f} ({r2:+.2f}%)  總成交量: {amt2:,.1f} 億"
                                self.safe_after(0, lambda t=text2, c=color2: self.lbl_twoii.config(text=t, fg=c))
                                # 【第九輪修正 第4項】30 秒 → 5 秒:當沖看大盤 30 秒一跳
                                # 完全來不及。一次批次 2 檔快照、5 秒一輪,符合 P-03
                                # 「fallback 快照 ≥5 秒」的流量底線。
                                time.sleep(5)
                                continue 
                    except Exception: pass 
                # 【ADR-011】YF 備援已移除:大盤指數也一律使用 shioaji，
                # 未登入時維持顯示初始的「等待連線API...」文字，不再退化成 YF 資料。
            except Exception: pass
            time.sleep(5)

    # ================= 🗜️ 盤後參考五檔產生器 (誠實版) =================
    # 【修正說明】shioaji Snapshot 物件「沒有」bid_ask 屬性 (只有最佳一檔 buy_price/sell_price
    # 與 buy_volume/sell_volume),原程式的解構分支永遠不會執行,實際上每次都用總量
    # 演算法「捏造」五檔量 —— 這就是盤後五檔看起來怪異的主因之一。
    # 現改為:第一檔顯示 snapshot 的真實最佳買賣價量,第 2~5 檔僅依台股 tick 規則
    # 推導參考價位、成交量一律顯示 "--",且物件帶 is_simulated 標記供 UI 註明。
    def generate_5_levels_from_snapshot(self, s):
        class RefBidAsk:
            def __init__(self):
                self.bid_price = ["--"]*5; self.bid_volume = ["--"]*5
                self.ask_price = ["--"]*5; self.ask_volume = ["--"]*5
                self.is_simulated = True
        ref = RefBidAsk()
        try:
            b1 = float(getattr(s, 'buy_price', 0) or 0)
            a1 = float(getattr(s, 'sell_price', 0) or 0)
            base = b1 if b1 > 0 else float(getattr(s, 'close', 0) or 0)
            if base <= 0: return ref

            tick = self.get_tick(base)
            b1 = round(round(base / tick) * tick, 2)
            if a1 <= 0: a1 = round(b1 + self.get_tick(b1), 2)

            bv1 = getattr(s, 'buy_volume', 0) or 0
            av1 = getattr(s, 'sell_volume', 0) or 0

            curr_b, curr_a = b1, a1
            for i in range(5):
                ref.bid_price[i] = curr_b
                ref.ask_price[i] = curr_a
                # 向下跳檔須以「略低於當前價」的價格帶決定 tick (處理 10/50/100/500/1000 交界)
                down_tick = self.get_tick(max(curr_b - 0.0001, 0.01))
                curr_b = round(curr_b - down_tick, 2)
                if curr_b <= 0: curr_b = 0.01
                curr_a = round(curr_a + self.get_tick(curr_a), 2)
            # 只有第一檔的量是真實資料,其餘保持 "--"
            if bv1 and int(bv1) > 0: ref.bid_volume[0] = int(bv1)
            if av1 and int(av1) > 0: ref.ask_volume[0] = int(av1)
        except Exception: pass
        return ref

    # ================= 🚀 五檔與即時行情更新 (零股/整股完全隔離) =================
    def fmt_price(self, p):
        # 【ADR-009】實際規則移到 core/tick_rules.py。
        return tick_rules.fmt_price(p, self.asset_type, self.current_symbol)

    def update_quote_ui(self, bidask, tick, is_odd_mode):
        try:
            is_sim = bool(getattr(bidask, 'is_simulated', False))
            if bidask:
                b_p, b_v, a_p, a_v = bidask.bid_price, bidask.bid_volume, bidask.ask_price, bidask.ask_volume
                for i in range(5):
                    try:
                        if i < len(b_p) and b_p[i] != "--" and float(b_p[i]) > 0:
                            self.lbl_bid_prices[i].config(text=self.fmt_price(b_p[i]))
                            bv = b_v[i] if i < len(b_v) else "--"
                            self.lbl_bid_vols[i].config(text=str(int(bv)) if bv != "--" else "--")
                        else:
                            self.lbl_bid_prices[i].config(text="--")
                            self.lbl_bid_vols[i].config(text="--")
                    except Exception:
                        self.lbl_bid_prices[i].config(text="--"); self.lbl_bid_vols[i].config(text="--")
                    try:
                        if i < len(a_p) and a_p[i] != "--" and float(a_p[i]) > 0:
                            self.lbl_ask_prices[i].config(text=self.fmt_price(a_p[i]))
                            av = a_v[i] if i < len(a_v) else "--"
                            self.lbl_ask_vols[i].config(text=str(int(av)) if av != "--" else "--")
                        else:
                            self.lbl_ask_prices[i].config(text="--")
                            self.lbl_ask_vols[i].config(text="--")
                    except Exception:
                        self.lbl_ask_prices[i].config(text="--"); self.lbl_ask_vols[i].config(text="--")
                        
            if tick:
                mode_str = "零股" if is_odd_mode else "整股"
                unit_str = "股" if is_odd_mode else "張"
                c_val = float(tick.close)
                close_str = self.fmt_price(c_val) if c_val > 0 else "--"
                
                diff_str = ""
                if is_odd_mode and self.last_norm_close > 0 and c_val > 0:
                    diff = c_val - self.last_norm_close
                    pct = diff / self.last_norm_close * 100
                    sign = "+" if diff > 0 else ""
                    norm_str = self.fmt_price(self.last_norm_close)
                    diff_str = f"  (整股: {norm_str}  價差: {sign}{diff:.2f} / {sign}{pct:.2f}%)"
                
                # snapshot 物件才有 change_price;串流 tick (v1) 沒有
                if hasattr(tick, 'change_price'):
                    src = "盤後快照(參考)" if is_sim else "快照行情"
                else:
                    src = "即時串流"
                txt = f"[{mode_str}/{unit_str}] {src}: {close_str}{diff_str}"
                self.lbl_rt_quote.config(text=txt)
        except Exception: pass

    # ================= 【ADR-028】自選股即時報價 =================
    def _resolve_wl_contract(self, sym):
        """把自選股代碼解析成 shioaji 合約 (股票/期貨/指數通吃),含登入世代快取。"""
        if sym in self._wl_contract_cache:
            return self._wl_contract_cache[sym]
        c = None
        try:
            if sym == "^TWII":
                c = self.brokers['sinopac'].index_contract('TSE')   # 【ADR-114】
            elif sym == "^TWOII":
                c = self.brokers['sinopac'].index_contract('OTC')   # 【ADR-114】
            elif self._looks_like_futures_symbol(sym):
                # 【第十一輪修正】期貨完整代號含數字,要先於「含數字=股票」判斷
                c = self._resolve_futures_contract(sym)
            elif any(ch.isdigit() for ch in sym):
                c = self.sj_api.Contracts.Stocks.get(sym)
            else:
                c = self._resolve_futures_contract(sym)
        except Exception:
            c = None
        self._wl_contract_cache[sym] = c  # 查無也快取 (None),避免每輪重查
        return c

    @staticmethod
    def _is_us_symbol(sym):
        """【第十一輪修正】自選股裡的美股代號:純英文字母 (1-5碼),且不是期貨樣式。"""
        s = str(sym or '').upper()
        return s.isalpha() and 1 <= len(s) <= 5 and not s.startswith('^')

    def _wl_fetch_us_quotes(self, us_syms):
        """美股報價走 yfinance (shioaji 沒有美股)。fast_info 取現價與昨收算漲跌;
        名稱嘗試 shortName 並快取。任何一檔失敗只跳過該檔。回傳 quotes dict。"""
        quotes = {}
        if not hasattr(self, '_wl_us_names'):
            self._wl_us_names = {}
        for sym in us_syms:
            try:
                t = yf.Ticker(sym)
                # 【第十五輪修正】漲跌基準改用「未還原 (auto_adjust=False) 日K」的
                # 最後兩個收盤價:fast_info.previous_close 的口徑不穩 (可能拿到
                # 還原調整值或錯誤交易日),與券商/看盤軟體顯示對不上 (使用者實例:
                # SPYM 顯示 +0.16/+0.19%,實際 +0.34/+0.38%)。美股盤中時日K最後
                # 一列就是今日即時價,前一列是昨收,算出來就是正確的當日漲跌。
                last, prev = 0.0, 0.0
                try:
                    hist = t.history(period="10d", interval="1d", auto_adjust=False)
                    closes = hist['Close'].dropna()
                    if len(closes) >= 2:
                        last = float(closes.iloc[-1]); prev = float(closes.iloc[-2])
                except Exception:
                    pass
                if not (last > 0 and prev > 0):
                    # 歷史取不到才退回 fast_info (至少有報價,漲跌可能略有口徑差)
                    fi = getattr(t, 'fast_info', None)
                    last = float(getattr(fi, 'last_price', None) or (fi.get('lastPrice') if hasattr(fi, 'get') else 0) or 0)
                    prev = float(getattr(fi, 'previous_close', None) or (fi.get('previousClose') if hasattr(fi, 'get') else 0) or 0)
                if last > 0 and prev > 0:
                    quotes[sym] = (last, last - prev, (last - prev) / prev * 100.0)
                if sym not in self._wl_us_names:
                    try:
                        nm = (t.info or {}).get('shortName', '')
                        self._wl_us_names[sym] = str(nm)[:12] if nm else '美股'
                    except Exception:
                        self._wl_us_names[sym] = '美股'
            except Exception:
                continue
        return quotes

    def _month_rank_of(self, contract):
        """【ADR-081】判斷期貨合約對應的連續月份等級:
        - R2 / 次月合約 (如 TXFR2, MXFR2) → 2 (次月連續)
        - R3 / 遠月合約 (如 TXFR3) → 3 (遠月連續)
        - 其餘 (如 TXFR1, TXF, 股票期貨, 特定月份合約) → 1 (近一連續)
        """
        if not contract:
            return 1
        sym = str(getattr(contract, 'symbol', '') or getattr(contract, 'code', '') or '').upper()
        if 'R2' in sym:
            return 2
        if 'R3' in sym:
            return 3
        return 1



    def _wl_ensure_stream_subs(self):
        """【第十六輪 第2項】確保目前群組的期貨/指數都已訂閱 tick 串流。
        股票不訂閱 (維持10秒批次快照,不佔訂閱額度);訂閱上限 20 檔防呆。"""
        if not (self.api_logged_in and HAS_SJ and self.sj_api):
            return
        for sym in list(self._wl_current_syms):
            if sym in self._wl_subscribed or len(self._wl_subscribed) >= 20:
                continue
            is_idx = sym in ('^TWII', '^TWOII')
            # 【ADR-042 效能回退修正】股票「不」訂閱串流,退回快照 (5秒):
            # 熱門股每秒數十 tick,20 檔全訂閱=每秒數百次 Python callback 跟
            # GUI 搶 GIL,整個畫面變鈍 (使用者實測回報)。期貨/指數檔數少維持
            # 串流;股票快照節奏由 10 秒加快到 5 秒 (P-03 下限) 作為折衷。
            if not (is_idx or sym.isalpha() or self._looks_like_futures_symbol(sym)):
                continue
            c = self._resolve_wl_contract(sym)
            if c is None:
                continue
            code = str(getattr(c, 'code', '') or '')
            if not code or (not is_idx and code.isdigit()):
                continue  # 純數字=股票 → 快照
            try:
                try:
                    self.sj_api.quote.subscribe(c, quote_type=sj.constant.QuoteType.Tick, version=sj.constant.QuoteVersion.v1)
                except TypeError:
                    self.sj_api.quote.subscribe(c, quote_type='tick')
                self._wl_subscribed.add(sym)
                self._wl_stream_refs[sym] = float(getattr(c, 'reference', 0) or 0)
                if is_idx:
                    self._wl_idx_code_map[code] = sym
                else:
                    self._wl_fut_code_map[code[:3]] = sym
            except Exception:
                continue

    def _wl_route_stream_tick(self, tick, is_fop):
        """tick callback 的自選股路由:把期貨/指數 tick 換算成 (close,chg,pct) 暫存。
        在 callback 執行緒被呼叫,只寫 dict (GIL 原子),不碰 UI。"""
        try:
            code = str(getattr(tick, 'code', '') or '')
            if is_fop:
                sym = self._wl_fut_code_map.get(code[:3])
            else:
                sym = self._wl_idx_code_map.get(code)  # 指數對照;股票不串流 (ADR-042)
            if not sym:
                return
            close = float(getattr(tick, 'close', 0) or 0)
            if close <= 0:
                return
            chg = getattr(tick, 'price_chg', None)
            pct = getattr(tick, 'pct_chg', None)
            if chg is None or pct is None:
                ref = self._wl_stream_refs.get(sym, 0)
                if ref > 0:
                    chg = close - ref
                    pct = chg / ref * 100.0
                else:
                    chg, pct = 0.0, 0.0
            self._wl_stream_quotes[sym] = (close, float(chg), float(pct))
        except Exception:
            pass

    def _wl_fetch_quotes_once(self):
        """
        抓一輪目前群組的即時報價:台股/期貨/指數一次批次 snapshots (P-03 節流);
        美股走 yfinance (每3輪≈30秒一次,避免對免費源打太兇)。抽成獨立方法方便測試。
        """
        syms = list(self._wl_current_syms)
        if not syms:
            return
        # 【第十一輪修正】分類原則:登入時以 shioaji 解析結果為準——解析得到
        # 就是台灣商品 (TXF/CDF 等期貨代號也是純英文,不能用字元特徵猜是美股,
        # P-42 同源);解析不到的純英文代號才視為美股走 yfinance。
        # 未登入時無法解析,僅對 4 碼以上純英文 (排除 3 碼期貨商品代號) 抓美股。
        logged = bool(self.api_logged_in and HAS_SJ and self.sj_api)
        pairs, us_syms = [], []
        for s in syms:
            c = self._resolve_wl_contract(s) if logged else None
            if c is not None:
                pairs.append((s, c))
            elif self._is_us_symbol(s) and (logged or len(s) >= 4):
                us_syms.append(s)
        # --- 美股 (yfinance,不需券商登入;每3輪≈30秒抓一次,首次立即) ---
        if us_syms:
            self._wl_us_cycle = getattr(self, '_wl_us_cycle', 0) + 1
            if self._wl_us_cycle >= 3 or not any(s in self._wl_quotes for s in us_syms):
                self._wl_us_cycle = 0
                us_quotes = self._wl_fetch_us_quotes(us_syms)
                if us_quotes:
                    self.safe_after(0, self._apply_wl_quotes, us_quotes)
        # --- 台股/期貨/指數 (shioaji 批次 snapshot) ---
        if not pairs:
            return
        try:
            snaps = self.sj_api.snapshots([c for _, c in pairs])
        except Exception as e:
            if self._looks_like_session_dead(e):
                self._mark_session_dead()
            return
        quotes = {}
        for (sym, _), snap in zip(pairs, snaps or []):
            try:
                close = float(getattr(snap, 'close', 0) or 0)
                chg = float(getattr(snap, 'change_price', 0) or 0)
                pct = float(getattr(snap, 'change_rate', 0) or 0)
                if close > 0:
                    quotes[sym] = (close, chg, pct)
            except Exception:
                continue
        if quotes:
            self.safe_after(0, self._apply_wl_quotes, quotes)

    def _apply_wl_quotes(self, quotes):
        """在 UI 執行緒把報價寫進自選股表格:只更新既有列的值與顏色,不重建、不動選取。"""
        try:
            self._wl_quotes.update(quotes)
            for sym in self.tree_wl.get_children():
                q = self._wl_quotes.get(sym)
                if q is None:
                    continue
                p, c, r, tag = self._wl_fmt_quote(q)
                try:
                    # 保留既有名稱欄 (index 1),只更新報價三欄與顏色
                    cur = self.tree_wl.item(sym, "values")
                    name = cur[1] if len(cur) > 1 else self._wl_display_name(sym)
                    if name in ("--", "美股"):
                        name = self._wl_display_name(sym)  # 登入/取得 shortName 後補上名稱
                    self.tree_wl.item(sym, values=(sym, name, p, c, r), tags=(tag,))
                except Exception:
                    continue
        except Exception:
            pass

    # 【ADR-126】自選股 worker 的兩個節奏,刻意拆成兩個獨立常數。
    #
    # 串流上屏是**本地端**的事 (tick 是券商推播過來的,已經在記憶體裡),加快
    # 完全沒有 API 成本;股票批次快照是**真的打 API**,受鐵則 5 管。原本兩者
    # 綁在同一個 `i % 10` 上,改快上屏就會連帶把 API 打快 —— 這次把「幾秒打
    # 一次快照」寫成秒數、輪數由除法算出來,以後只調上屏速度不會誤傷 API 節流。
    WL_STREAM_UI_SEC = 0.1      # 串流報價上屏 (原 0.25;純本地,無 API 成本)
    WL_SNAPSHOT_SEC = 2.5       # 股票批次快照 (維持原值,鐵則 5 / ADR-094)

    def watchlist_quote_worker(self):
        """【ADR-126】期貨/指數串流報價每 WL_STREAM_UI_SEC 秒上屏 (當沖等級);
        股票批次快照維持 WL_SNAPSHOT_SEC 秒一次。未登入時安靜等待。

        【鐵則5】快照間隔調整前已查 shioaji 官方文件的次數限制
        (snapshots/ticks/kbars 等合計 10 秒 50 次上限),2.5 秒一次的批次
        快照仍遠低於這個上限,詳見 DECISIONS_ADR094.md。**這次只加快了上屏,
        快照間隔一秒都沒有動。**"""
        i = 0
        every = max(1, int(round(self.WL_SNAPSHOT_SEC / self.WL_STREAM_UI_SEC)))
        while True:
            try:
                if i % every == 0:
                    self._wl_fetch_quotes_once()
                    self._wl_ensure_stream_subs()
                if self._wl_stream_quotes:
                    snap = dict(self._wl_stream_quotes)
                    self._wl_stream_quotes = {}
                    self.safe_after(0, self._apply_wl_quotes, snap)
            except Exception:
                pass
            i += 1
            time.sleep(self.WL_STREAM_UI_SEC)

    def fetch_realtime_worker(self):
        while True:
            if self._closing:
                return  # 【ADR-012】視窗已關閉,提早結束這條 daemon thread
            if self.api_logged_in and HAS_SJ and self.current_symbol:
                try:
                    with self.quote_lock:
                        bidask = self.current_bidask_odd if self.is_odd_lot else self.current_bidask_normal
                        tick = self.current_tick_odd if self.is_odd_lot else self.current_tick_normal
                        contract = self.current_contract
                    
                    if bidask or tick:
                        # 有真實串流:直接更新。整股最新價已在 on_tick_stk_v1 內快取,
                        # 不再於零股模式下每 3 秒額外打 snapshot (省 API 流量)。
                        self.safe_after(0, self.update_quote_ui, bidask, tick, self.is_odd_lot)
                    elif contract:
                        # 無串流 (盤後/剛切換商品):以 snapshot 補參考資料。
                        # 【修正1】節流至每 3 秒一次 (鐵則5:原本5秒,調整前已查
                        #          shioaji 官方文件 snapshots/ticks/kbars 等合計
                        #          10秒50次的次數上限,3秒一次仍遠低於上限,詳見
                        #          DECISIONS_ADR094.md) —— 原本每 0.5 秒打一次
                        #          snapshots,會快速耗盡 shioaji 流量配額。
                        # 【修正2】shioaji snapshot「只有整股資料」,零股模式下不再拿整股
                        #          快照冒充零股報價 (這正是零股數據看起來錯亂的主因)。
                        now_time = time.time()
                        if now_time - self.last_fallback_snap_time >= 3:
                            self.last_fallback_snap_time = now_time
                            try:
                                snaps = self.sj_api.snapshots([contract])
                            except Exception:
                                snaps = None
                            if snaps and len(snaps) > 0:
                                s = snaps[0]
                                try: self.last_norm_close = float(s.close)
                                except Exception: pass
                                if not self.is_odd_lot:
                                    ref_bidask = self.generate_5_levels_from_snapshot(s)
                                    self.safe_after(0, self.update_quote_ui, ref_bidask, s, False)
                                else:
                                    # 零股模式無串流:五檔一律清空 (盤後沒有真實零股五檔)
                                    if not self.odd_no_stream_warned:
                                        self.odd_no_stream_warned = True
                                        self.safe_after(0, self.log_message, "【零股提示】目前無零股即時串流。盤中零股 09:00-13:30、盤後零股 13:40-14:30 才有即時資料;此時段外五檔以 -- 顯示。")
                                    self.safe_after(0, self.clear_5_level_ui)
                                    norm_c = float(s.close)
                                    # 若整股 tick 串流曾帶入今日盤後零股收盤價,優先顯示真實零股收盤 + 價差;
                                    # 否則 (冷登入無串流) 退回整股參考價,並註明零股收盤 shioaji 無法回補。
                                    if self.last_odd_close > 0:
                                        odd_c = self.last_odd_close
                                        diff = odd_c - norm_c
                                        pct = diff / norm_c * 100 if norm_c > 0 else 0
                                        sign = "+" if diff > 0 else ""
                                        odd_str = self.fmt_price(odd_c)
                                        norm_str = self.fmt_price(norm_c)
                                        txt = f"[零股] 盤後零股收: {odd_str}  (整股: {norm_str}  價差: {sign}{diff:.2f} / {sign}{pct:.2f}%)"
                                        self.safe_after(0, lambda t=txt: self.lbl_rt_quote.config(text=t))
                                    else:
                                        norm_str = self.fmt_price(norm_c)
                                        txt = f"[零股] 無串流,整股參考價: {norm_str}  (今日零股收盤 shioaji 盤後無法回補)"
                                        self.safe_after(0, lambda t=txt: self.lbl_rt_quote.config(text=t))
                except Exception: pass
            time.sleep(0.1)  # 【新ADR 報價加速】報價上屏 0.25→0.1 秒 (tick 是推播,加快上屏無 API 成本)

    def start_fetch_thread(self):
        # 【ADR-011】移除「未登入且未開YF備援就整個擋下」的舊檢查:
        # 美股本來就不需要登入 shioaji (自動用 yfinance)；台股是否需要登入
        # 交給 fetch_data_worker 依商品類型判斷並給出對應的錯誤訊息，
        # 這裡不用先猜測使用者輸入的是哪種商品。
        raw_sym = self.entry_symbol.get().strip().upper()
        if not raw_sym: return
        # 【第九輪 第6項】輸入含中文 → 走「中文名稱搜尋」流程,不直接查代碼:
        # 背景搜尋股票+期貨合約名稱,多筆結果開卷軸清單讓使用者挑選。
        if any('\u4e00' <= ch <= '\u9fff' for ch in raw_sym):
            if not (self.api_logged_in and HAS_SJ):
                self.log_message("【搜尋】中文名稱搜尋需要先登入券商 API (要有合約檔才能比對名稱)。")
                return
            self.log_message(f"【搜尋】以中文名稱搜尋「{raw_sym}」...")
            threading.Thread(target=self._symbol_search_worker, args=(raw_sym,), daemon=True).start()
            return
        self.saved_xlim = None
        self._view_is_auto_default = True  # 新查詢時重置為系統預設視角
        tf = self.timeframe_var.get()
        # 【ADR-024】每次查詢遞增序號;舊 worker 發布前會檢查,過期就放棄。
        self._fetch_seq += 1
        seq = self._fetch_seq
        # 【ADR-024】只有「換了商品」才清報價暫存/五檔;同商品換週期沿用既有
        # 串流資料,畫面不會閃一下空白,也不用等重新訂閱。
        sym_changed = (raw_sym != self._last_fetch_raw_sym)
        self._last_fetch_raw_sym = raw_sym
        if sym_changed:
            self.clear_5_level_ui()
            with self.quote_lock:
                self.current_bidask_normal = None
                self.current_bidask_odd = None
                self.current_tick_normal = None
                self.current_tick_odd = None
                # 換股時清掉上一檔的整股價與盤後零股收盤快取,避免把 A 股的價套到 B 股
                self.last_norm_close = 0.0
                self.last_odd_close = 0.0
                self.last_odd_shares = 0
                self.last_odd_date = ""
            self.odd_no_stream_warned = False
            self.last_fallback_snap_time = 0
        market = self.market_mode.get()  # 在 UI 執行緒讀取 tk 變數,傳給 worker
        # 【ADR-080】MA240 是否開啟也要在 UI 執行緒讀 tk 變數 (self.ma_shows/
        # self.ma_periods),傳給 worker 決定主圖要不要延伸深歷史 (見 _publish)。
        try:
            ma240_on = any(self.ma_shows[i].get() and str(self.ma_periods[i].get()).strip() == '240'
                          for i in range(len(self.ma_shows)))
        except Exception:
            ma240_on = False
        threading.Thread(target=self.fetch_data_worker, args=(raw_sym, tf, seq, market, ma240_on), daemon=True).start()

    def trigger_redraw(self):
        if self.current_df is not None and self.axlist is not None:
            try: self.saved_xlim = self.axlist[0].get_xlim()
            except: pass
            self.draw_chart(self.current_df)
        else: self.start_fetch_thread()

    def _on_chart_frame_resize(self, event=None):
        """
        【使用者調整#1】chart_frame 尺寸改變時 (使用者拖曳視窗邊框或
        PanedWindow 分隔線) 重新用目前的實際像素尺寸繪製圖表，讓圖表持續
        填滿可用空間。用 debounce 避免拖曳過程中 <Configure> 連續觸發造成
        頻繁重繪；【ADR-036】拖曳 sash 期間 (self._sash_dragging) 完全不
        排程重繪，只記 pending，放開滑鼠時才由 _on_pane_sash_release 觸發
        一次重繪，避免「拖一下停一下」把每個 300ms 停頓都變成一次完整重繪。
        """
        if getattr(self, '_sash_dragging', False):
            self._resize_pending = True
            return
        if self._resize_after_id is not None:
            try: self.after_cancel(self._resize_after_id)
            except Exception: pass
        self._resize_after_id = self.safe_after(300, self._debounced_resize_redraw)

    def _on_pane_sash_press(self, event=None):
        """【ADR-036】滑鼠按住 PanedWindow 分隔線:進入拖曳狀態,期間抑制重繪。"""
        self._sash_dragging = True
        self._resize_pending = False
        # 取消已排入佇列的重繪,避免按住當下前一個 debounce 還是燒出一次重繪
        if self._resize_after_id is not None:
            try: self.after_cancel(self._resize_after_id)
            except Exception: pass
            self._resize_after_id = None

    def _on_pane_sash_release(self, event=None):
        """【ADR-036】放開分隔線:若拖曳期間尺寸有變,補排「一次」debounce 重繪。"""
        was_dragging = getattr(self, '_sash_dragging', False)
        self._sash_dragging = False
        if was_dragging and self._resize_pending:
            self._resize_pending = False
            if self._resize_after_id is not None:
                try: self.after_cancel(self._resize_after_id)
                except Exception: pass
            self._resize_after_id = self.safe_after(300, self._debounced_resize_redraw)

    def _debounced_resize_redraw(self):
        self._resize_after_id = None
        pass

    def auto_scale_y(self, ax, xmin, xmax):
        try:
            if self.plot_df is None or len(self.plot_df) == 0: return
            imin, imax = max(0, int(np.floor(xmin))), min(len(self.plot_df), int(np.ceil(xmax)))
            if imin >= imax: return
            sub_df = self.plot_df.iloc[imin:imax]
            low = sub_df['Low'].min()
            high = sub_df['High'].max()
            if not (pd.notna(low) and pd.notna(high)):
                return
            if high <= low:
                # 【ADR-109】視角內價格完全沒有波動 (漲停鎖死一整天、或只剩
                # 一根K棒)。原本這裡直接 return,結果是沿用 mplfinance 依
                # 「整個資料集」算出來的 Y 軸範圍,那根K棒會被壓成畫面中間
                # 一條看不出來的線。給一個對稱小範圍讓它至少畫得出來。
                low, high = float(low) - 1.0, float(high) + 1.0
            padding = (high - low) * 0.05
            ax.set_ylim(low - padding, high + padding)
        except Exception: pass

    def auto_scale_indicator_panels(self, xmin, xmax):
        """
        【使用者第三次反映#3:MACD副圖顯示異常】根因排查：`auto_scale_y()`
        原本只套用在主圖 (價格) 的 Y 軸，副圖 (MACD/RSI/KDJ/DMI/布林寬度)
        從來沒有依「目前實際看得到的 X 範圍」重新計算過 Y 軸——它們的 Y 軸
        是用 mplfinance 依「整個資料集」算出來的固定範圍。如果歷史資料裡
        (即使是已經被縮放/平移到畫面外看不到的那一段) 某個時間點的 MACD
        數值特別大，仍然會撐開整個副圖的 Y 軸範圍；使用者目前實際看得到的
        這一小段資料，數值相對這個被撐大的範圍顯得極小，畫出來就會被壓縮
        成一條貼在底部或某一側的扁平線，看起來像「跑掉/壞掉」。

        這個方法讓每個有開啟的副圖都依「目前實際看得到的 X 範圍」內的資料
        重新計算 Y 軸上下限，而不是沿用 mplfinance 用整個資料集算出來的
        固定範圍。需要搭配 draw_chart() 裡設定的 self.active_panels /
        self.panel_columns 才能知道每個副圖對應哪些欄位。
        """
        if self.plot_df is None or len(self.plot_df) == 0 or not self.axlist:
            return
        try:
            imin, imax = max(0, int(np.floor(xmin))), min(len(self.plot_df), int(np.ceil(xmax)))
            if imin >= imax:
                return
            sub_df = self.plot_df.iloc[imin:imax]
            for name, p_idx in getattr(self, 'active_panels', {}).items():
                cols = getattr(self, 'panel_columns', {}).get(name)
                if not cols:
                    continue
                cols = [c for c in cols if c in sub_df.columns]
                if not cols:
                    continue
                ax_idx = p_idx * 2
                if ax_idx >= len(self.axlist):
                    continue
                ax = self.axlist[ax_idx]
                values = sub_df[cols].to_numpy(dtype=float)
                valid = values[~np.isnan(values)]
                if valid.size == 0:
                    continue
                low, high = float(valid.min()), float(valid.max())
                # 【ADR-109】成交量必須從 0 起算。量是「長條圖」,底邊代表 0;
                # 若照其他副圖那樣用 low - padding 當下限,軸底會落在「視角內
                # 最小的那根量」之下一點點,於是每一根長條都被從底部裁掉一截,
                # 看起來像是所有成交量都差不多大 —— 量能的相對關係整個失真。
                if name == 'Volume':
                    padding = max(high * 0.15, 1.0)
                    ax.set_ylim(0.0, max(high + padding, 10.0))
                    continue
                if high <= low:
                    # 資料是常數 (例如全部都是0),給一個對稱的小範圍避免 set_ylim 出錯
                    low, high = low - 1, high + 1
                padding = (high - low) * 0.12
                ax.set_ylim(low - padding, high + padding)
        except Exception:
            pass

    # ================= 期貨「交易日」K線聚合 (ADR-007) =================
    # 【背景】shioaji kbars 只回傳分K,期貨的日/週/月K靠程式自己 resample。
    # 原本用 resample('D')「自然日 00:00」切割,會把當天 15:00 起的夜盤混進
    # 隔天的日盤,導致開盤/收盤/最高/最低全部錯誤 (已用模擬資料實測驗證)。
    #
    # 台指期交易時段:日盤 08:45-13:45、夜盤 15:00-隔日05:00。
    # 依使用者確認的規則:
    #   1. 日K收盤採「全時段收盤」(近全) —— 涵蓋夜盤到隔日05:00。
    #   2. 夜盤併入「下一個交易日」(標準交易日邏輯):
    #      當天 15:00 起的夜盤 -> 算下一個交易日;
    #      隔天 00:00-05:00 (夜盤延續) 與隔天日盤 08:45-13:45 -> 都算同一個交易日。
    #   由於日盤 (08:45-13:45) 在時間順序上排在夜盤兩段之後,
    #   分組後取 'last' 當 Close,會自然落在日盤 13:45 那筆 —— 這正是券商
    #   軟體「台指近全」日K的收盤定義,同時 Open 會是夜盤 15:00 的開盤,
    #   涵蓋近全時段的高低範圍。
    def _resample_future_session(self, sj_df, tf, agg_dict, session_basis='all'):
        # 【ADR-009】交易日聚合的核心邏輯移到 core/futures_session.py (純函式,不吞例外)。
        # 這裡保留原本的例外處理與日誌記錄 + 自然日退回機制,因為這兩件事本質上是
        # GUI 層的關注點 (要不要跟使用者說一聲、要不要用比較不準的資料頂著繼續跑)。
        try:
            return futures_session.resample_future_session(sj_df, tf, agg_dict, session_basis=session_basis)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【期貨交易日聚合異常】{e},退回自然日聚合 (可能不準確)")
            return futures_session.resample_natural_day_fallback(sj_df, tf, agg_dict)

    # ================= 【ADR-024】K線抓取效能:快取 + 漸進載入 =================
    # 慢的根本原因:shioaji kbars 回傳「一分K」。台指期一天交易近19小時 (約1140根/天),
    # 舊表日K抓730天 ≈ 55萬根分K,下載+重採樣動輒數秒到十幾秒;指數也有十幾萬根。
    # 且完全沒有快取:換週期、換回剛看過的商品都整套重下載。
    # 對策:
    #   1. 縮短下載範圍 (下表);2. 原始分K快取 (同商品換週期/短時間內切回→秒開);
    #   3. 期貨/指數的日K以上採「兩段式」:先抓小範圍秒出圖,背景補全歷史再更新;
    #   4. fetch 序號防 race (見 start_fetch_thread)。
    SJ_DAYS = {"1分K": 15, "5分K": 60, "15分K": 120, "30分K": 120, "60分K": 180,
               "日K": history_policy.SHIOAJI_MAX_REQUEST_DAYS,
               "周K": history_policy.SHIOAJI_MAX_REQUEST_DAYS,
               "月K": history_policy.SHIOAJI_MAX_REQUEST_DAYS}
    # 【ADR-069/142】兩段式第一段仍用小範圍搶先出圖；首次建庫時
    # 背景段會向 shioaji 請求最長實用範圍，並用 Yahoo/期交所補充。
    # 建庫後只回抓最新一週，舊歷史由 SQLite 提供。
    QUICK_DAYS = {"1分K": 2, "5分K": 5, "15分K": 10, "30分K": 15, "60分K": 20, "日K": 7, "周K": 45, "月K": 120}
    CACHE_TTL_MIN_TF = 30    # 分K類快取視為新鮮的秒數
    CACHE_TTL_DAY_TF = 300   # 日K以上快取視為新鮮的秒數
    KBARS_CACHE_MAX = 6      # 快取最多保留幾檔商品的原始分K (LRU 淘汰最舊)

    def _download_kbars_chunked(self, contract, start_dt, end_dt, chunk_days=90, progress_cb=None,
                                retries=2, pace_sec=0.35, subsplit_days=30, abort_cb=None,
                                stats=None):
        """
        【ADR-046/047 → ADR-048 強化】分段下載歷史K線。

        ADR-048 根因 (使用者實例 MXFR1/TMFR1「抓不到以前的歷史」):日誌顯示
        三個失敗分段的時間戳在「同一秒」、緊接前一檔下載之後,錯誤為
        ServerError: kbars: request $P2P/... (Solace 路由層拒絕) —— 這不是
        資料不存在 (小台歷史在券商端存在多年),而是連發 kbars 觸發券商端
        流量管制被瞬間拒絕。舊版分段失敗「立刻跳段、零重試、零間隔」,
        整批歷史一秒內全數放棄。官方文件明示:超限會暫停服務,收到錯誤
        應查明 (api.usage()) 再重試。因此:

          1. 段與段之間強制間隔 pace_sec 秒 (預設 0.35s),不再連發。
          2. 失敗段重試 retries 次,退避 1.5s→3s (流量管制多為短暫)。
          3. 重試仍失敗且區段夠大 → 再切成 subsplit_days 天的小段各試一次
             (部分商品對大範圍請求較敏感)。
          4. 第一次遇到失敗時呼叫 api.usage() 把流量用量寫進日誌 (證據:
             區分「流量耗盡」與「暫時性管制」)。
          5. 結束時輸出總結:成功/失敗段數與實得資料起點,失敗段提示稍後
             重新查詢可補抓 (會與快取合併)。

        progress_cb(done, total, seg_start, seg_end, n_rows) 每段完成後回呼。
        回傳串接後的 DataFrame (可能為空);session dead 直接往上拋。

        【ADR-122】stats:傳一個 dict 進來,結束時會被填上
        {'ok_segs','fail_segs','aborted','total_segs'}。**部分成功也是回傳
        DataFrame**,呼叫端光看回傳值分不出「完整」與「缺了最近那一段」——
        後者拿去給策略評估會用到過期的資料,所以策略路徑一定要看 stats。
        不傳則行為完全不變 (主圖既有呼叫端不受影響)。
        """
        segs = []
        cur = start_dt
        while cur < end_dt:
            seg_end = min(cur + timedelta(days=chunk_days), end_dt)
            segs.append((cur, seg_end))
            cur = seg_end + timedelta(days=1)

        # 【ADR-068】除了登出/關閉/回測取消 (_downloads_should_abort),額外接受
        # 呼叫端傳入的 abort_cb:主圖切商品時用它帶「本次查詢序號是否已過期」,
        # 讓使用者切走後,舊商品剩下的分段下載立刻停手,不再霸佔 _kbars_lock
        # 拖慢新商品的搶先出圖 (使用者連點不同標的時的卡頓主因之一)。
        def _should_abort():
            if self._downloads_should_abort():
                return True
            if abort_cb is not None:
                try:
                    return bool(abort_cb())
                except Exception:
                    return False
            return False

        usage_logged = [False]
        last_err = {'e': None}  # 【ADR-083】共用容器讓 nested _try_seg 與外層迴圈都能存取最後一次異常

        def _log_usage_once():
            if usage_logged[0]:
                return
            usage_logged[0] = True
            try:
                u = self.sj_api.usage()
                used = getattr(u, 'bytes', None); limit = getattr(u, 'limit_bytes', None)
                remain = getattr(u, 'remaining_bytes', None)
                if used is not None:
                    self.safe_after(0, self.log_message,
                                    f"【分段下載-診斷】API 流量:已用 {used/1048576:.1f}MB / 上限 "
                                    f"{(limit or 0)/1048576:.0f}MB / 剩餘 {(remain or 0)/1048576:.1f}MB "
                                    f"(剩餘充足=暫時性管制,稍後重試可補;趨近 0=今日流量耗盡)。")
            except Exception:
                pass

        def _try_seg(s0, s1, n_retries):
            """單一區段:含退避重試。回傳 df 或 None (失敗);session dead 上拋。"""
            for att in range(n_retries + 1):
                # 【ADR-060】退避等待期間使用者可能已登出/關程式,重試前再確認一次
                if _should_abort():
                    return None
                if att > 0:
                    time.sleep(1.5 * att)  # 1.5s → 3s 退避
                try:
                    return self._download_kbars_raw(contract, s0, s1)
                except Exception as e:
                    if self._looks_like_session_dead(e):
                        raise
                    last_err['e'] = e
            if last_err['e'] is not None:
                _log_usage_once()
                err_str = str(last_err['e'])
                if "ServerError: kbars: request" in err_str:
                    err_brief = "券商該期間尚無資料或商品未上市"
                else:
                    err_brief = f"{type(last_err['e']).__name__}: {err_str[:60]}"
                self.safe_after(0, self.log_message,
                                f"【分段下載】{s0:%Y-%m-%d}~{s1:%Y-%m-%d} ({err_brief})。")
            return None

        parts, ok_segs, fail_segs = [], 0, 0
        aborted = False
        for i, (s0, s1) in enumerate(segs, 1):
            # 【ADR-060】每段開始前先問「現在還該繼續嗎」。使用者登出/關閉程式/
            # 按下強制終止之後,剩下的段一律不要再打 —— 舊版會把整批跑完才停,
            # 登出後照樣送出幾十個必定失敗的請求,日誌被 AuthError 洗版。
            # 【ADR-068】abort_cb 額外涵蓋「使用者已切到別檔」,舊查詢立即讓路。
            if _should_abort():
                aborted = True
                self.safe_after(0, self.log_message,
                                f"【分段下載】已停止 (連線登出/使用者中止/已切換其他標的),"
                                f"剩餘 {len(segs) - i + 1} 段不再嘗試。")
                break
            if i > 1 and pace_sec > 0:
                time.sleep(pace_sec)  # 段間隔:不連發,避免觸發券商流量管制
            n_rows = 0
            part = _try_seg(s0, s1, retries)
            if part is None and (s1 - s0).days > subsplit_days:
                if last_err['e'] is not None and "ServerError: kbars: request" in str(last_err['e']):
                    fail_segs += 1
                    continue
                self.safe_after(0, self.log_message,
                                f"【分段下載】{s0:%Y-%m-%d}~{s1:%Y-%m-%d} 改切 {subsplit_days} 天小段搶救...")
                sub_parts = []
                sub = s0
                while sub < s1:
                    sub_end = min(sub + timedelta(days=subsplit_days), s1)
                    if pace_sec > 0:
                        time.sleep(pace_sec)
                    # 【ADR-122】原本寫死 1 次重試,等於 retries=0 的呼叫端
                    # 「說不要重試」在這條搶救路徑上不被尊重 (每個小段照樣
                    # 睡 1.5 秒)。min() 讓既有呼叫端行為完全不變 (retries=2
                    # → 仍是 1),但 retries=0 現在真的是 0。
                    sp = _try_seg(sub, sub_end, min(1, retries))
                    if sp is not None and not sp.empty:
                        sub_parts.append(sp)
                    sub = sub_end + timedelta(days=1)
                part = pd.concat(sub_parts) if sub_parts else None
            if part is not None and not part.empty:
                parts.append(part)
                n_rows = len(part)
                ok_segs += 1
            elif part is None:
                fail_segs += 1
            else:
                ok_segs += 1  # 成功但該期間無交易 (空段不算失敗)
            if progress_cb:
                try: progress_cb(i, len(segs), s0, s1, n_rows)
                except Exception: pass

        if stats is not None:
            stats.update({'ok_segs': ok_segs, 'fail_segs': fail_segs,
                          'aborted': aborted, 'total_segs': len(segs)})
        if not parts:
            return pd.DataFrame()
        out = pd.concat(parts)
        out = out[~out.index.duplicated(keep='last')].sort_index()
        if fail_segs or aborted:
            reason = ("已中止 (登出/關閉程式/使用者取消)" if aborted
                      else "失敗段多為券商端暫時性流量管制,稍候幾分鐘重新查詢同商品即可補抓 (會與已載入資料合併)")
            self.safe_after(0, self.log_message,
                            f"【分段下載-總結】成功 {ok_segs} 段 / 失敗 {fail_segs} 段,"
                            f"實得資料起點 {out.index[0]:%Y-%m-%d}。{reason}。")
        return out

    def _download_kbars_raw(self, contract, start_dt, end_dt):
        """下載並正規化 shioaji 原始分K (ts index / OHLCV 欄名)。失敗回傳空 df,例外往上拋。
        【第十七輪修正】全程持有 _kbars_lock:單一連線不可併發呼叫 kbars。"""
        with self._kbars_lock:
            kbars = self.sj_api.kbars(contract, start=start_dt.strftime("%Y-%m-%d"), end=end_dt.strftime("%Y-%m-%d"))
        if kbars is None:
            return pd.DataFrame()
        sj_df = pd.DataFrame({**kbars})
        if sj_df.empty:
            return sj_df
        sj_df['ts'] = pd.to_datetime(sj_df['ts'])
        if sj_df['ts'].dt.tz is not None:
            sj_df['ts'] = sj_df['ts'].dt.tz_convert('Asia/Taipei').dt.tz_localize(None)
        sj_df.set_index('ts', inplace=True)
        sj_df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close',
                              'volume': 'Volume', 'amount': 'Amount'}, inplace=True)
        return sj_df

    def _resample_sj_df(self, sj_df, tf, asset_type=None, session_basis='all'):
        """把原始分K依週期重採樣 (含指數 Amount→Volume、期貨交易日聚合)。不改動輸入 df。
        【ADR-035】asset_type 可由參數指定:量化策略要處理「非目前圖表商品」的K線,
        不能再隱式讀 self.asset_type (那是圖表商品的類型);不傳=沿用圖表 (行為不變)。
        【ADR-058】session_basis:期貨日/周/月K的盤別口徑 ('all' 近全 / 'day' 只用日盤),
        預設 'all' 完全維持既有行為;主圖一律用 'all',只有回測/最佳化可以選。"""
        if asset_type is None:
            asset_type = self.asset_type
        sj_df = sj_df.copy()
        if asset_type == "index_tw" and 'Amount' in sj_df.columns:
            sj_df['Volume'] = sj_df['Amount'] / 100000000
        
        # 濾除異常價格 (例如 0),避免 K 線圖 Y 軸範圍被拉大導致看起像一條死魚線
        for col in ['Close', 'Low', 'High', 'Open']:
            if col in sj_df.columns:
                sj_df = sj_df[sj_df[col] > 0]

        # 【新ADR 自訂週期】日/周/月K 是固定的 3 個特例規則;分鐘級週期一律靠
        # strategy_engine.timeframe_minutes() 換算,不再各寫一份「1分K→1,
        # 5分K→5,...」的對照表——這樣自訂的 N分K/N時K (量化策略編輯器新增
        # 的功能) 不用另外加規則就能直接算,也不會有兩份清單各自維護、遲早
        # 分歧的風險 (P-67 教訓)。主圖既有的固定週期下拉選單不受影響:
        # 傳進來的 tf 字串本來就都在 timeframe_minutes() 涵蓋範圍內。
        resample_map = {"日K": 'D', "周K": 'W-MON', "月K": 'MS'}
        rule = resample_map.get(tf)
        mins = 0
        if not rule:
            mins = strategy_engine.timeframe_minutes(tf) or 0
            if mins > 0:
                rule = f'{mins}min'
        if not rule:
            return sj_df
        agg_dict = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        if tf in ["日K", "周K", "月K"]:
            if asset_type == "future":
                # 期貨:用「交易日(session date)」聚合,不能用 resample('D') (ADR-007)。
                return self._resample_future_session(sj_df, tf, agg_dict, session_basis=session_basis)
            return sj_df.resample(rule, label='left', closed='left').agg(agg_dict).dropna()
        out = sj_df.resample(rule, label='left', closed='left').agg(agg_dict).dropna()
        if mins > 0:
            out.index = out.index + pd.Timedelta(minutes=mins)
        return out

    def _kbars_cache_get(self, key, need_start, ttl=None, fresh_check=None):
        """取快取。回傳 (raw_df涵蓋need_start之後 或 None, 是否新鮮)。

        【ADR-122】ttl 可由呼叫端覆寫。不傳 = 沿用類別常數 (主圖行為不變);
        策略路徑的日K 類會傳一個長很多的值,理由見 QT_CACHE_TTL_DAY_TF。
        """
        c = self._kbars_raw_cache.get(key)
        if not c or c['start'] > need_start:
            return None, False
        if fresh_check is not None:
            # 【ADR-127】呼叫端自己決定新鮮度 (日K 類改用「有沒有跨過開盤」)
            try:
                fresh = bool(fresh_check(c['t']))
            except Exception:
                fresh = False        # 判斷不出來一律當過期 (寧可重抓)
        else:
            if ttl is None:
                ttl = self.CACHE_TTL_DAY_TF if c.get('tf_class') == 'day' else self.CACHE_TTL_MIN_TF
            fresh = (time.time() - c['t']) <= ttl
        try:
            return c['df'].loc[c['df'].index >= need_start], fresh
        except Exception:
            return None, False

    def _kbars_cache_put(self, key, start_dt, raw_df, tf):
        try:
            tf_class = 'day' if tf in ("日K", "周K", "月K") else 'min'
            self._kbars_raw_cache[key] = {'t': time.time(), 'start': start_dt, 'df': raw_df, 'tf_class': tf_class}
            if len(self._kbars_raw_cache) > self.KBARS_CACHE_MAX:
                oldest = min(self._kbars_raw_cache, key=lambda k: self._kbars_raw_cache[k]['t'])
                self._kbars_raw_cache.pop(oldest, None)
        except Exception:
            pass

    # ================= 【ADR-049】期交所官方每日行情:更長期貨日K歷史 =================
    # shioaji kbars 只回分K且深度有限;期交所官網免費提供日行情 (TX 自 1998 起)。
    # 解析/合併純邏輯在 core/taifex_daily.py,儲存在 data/taifex_store.py;
    # 這裡只做:下載 (背景執行緒 + urllib)、匯入檔案、把儲存的日K接在圖表前面。
    # 【ADR-060】改為絕對路徑 (見 APP_DIR 說明);保留類別屬性是為了讓
    # 診斷腳本可以覆寫成暫存目錄測試。
    TAIFEX_BASE_DIR = APP_DIR                  # 與 broker_config.json 同層,存 taifex_daily/ 子目錄
    TAIFEX_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
    TAIFEX_PACE_SEC = 1.0                      # 分段下載間隔,禮貌節流,不要猛打官網
    TAIFEX_PROD_LABELS = {'TX': '臺股期貨 TX', 'MTX': '小型臺指 MTX', 'TMF': '微型臺指 TMF',
                          'TE': '電子期貨 TE', 'TF': '金融期貨 TF'}

    def _taifex_load_hist(self, tx_id, session='all', month_rank=1):
        """讀取某期交所商品/盤別/連續月份的本地日K (經記憶體快取)。沒匯入過回傳空 df。
        【ADR-058/ADR-081】快取 key 加上盤別與連續月份等級 (R1/R2)。
        """
        rank = int(month_rank)
        key = (tx_id, str(session), rank)
        hist = self._taifex_mem_cache.get(key)
        if hist is None:
            path = taifex_store.store_path(self.TAIFEX_BASE_DIR, tx_id, session=session, month_rank=rank)
            hist = taifex_store.load_daily(self.TAIFEX_BASE_DIR, tx_id, session=session, month_rank=rank)
            self._taifex_mem_cache[key] = hist
            sess_txt = '只用日盤' if session == 'day' else '近全'
            rank_txt = f"R{rank}" if rank > 1 else "R1近月"
            if hist is None or hist.empty:
                self.safe_after(0, self.log_message,
                                f"【期交所歷史】✗ 找不到 {tx_id} ({rank_txt},{sess_txt}) 的本地資料。"
                                f"我找的路徑是:{path} —— 請確認檔案就在這裡 "
                                f"(用「📥 期交所歷史」匯入會自動存到正確位置)。")
            else:
                self.safe_after(0, self.log_message,
                                f"【期交所歷史】✓ 已讀取 {tx_id} ({rank_txt},{sess_txt}) {len(hist):,} 根,"
                                f"涵蓋 {hist.index[0]:%Y-%m-%d} ~ {hist.index[-1]:%Y-%m-%d}。"
                                f"(檔案:{path})")
        return hist

    def show_taifex_status(self):
        """【ADR-060/ADR-081】一次列出所有期交所商品的本地資料狀態 (含 R1 近月與 R2 次月連續)。"""
        lines = [f"期交所歷史資料夾:{os.path.join(self.TAIFEX_BASE_DIR, taifex_store.SUBDIR)}", ""]
        any_found = False
        for tx_id in sorted(set(taifex_daily.PRODUCT_MAP.values())):
            for rank, rlabel in ((1, 'R1近月'), (2, 'R2次月'), (3, 'R3遠月')):
                for sess, label in (('all', '近全'), ('day', '日盤')):
                    path = taifex_store.store_path(self.TAIFEX_BASE_DIR, tx_id, session=sess, month_rank=rank)
                    if os.path.exists(path):
                        df = taifex_store.load_daily(self.TAIFEX_BASE_DIR, tx_id, session=sess, month_rank=rank)
                        if df is not None and not df.empty:
                            any_found = True
                            lines.append(f"✓ {tx_id} {rlabel} ({label}):{len(df):,} 根,"
                                         f"{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}")
                        else:
                            lines.append(f"⚠ {tx_id} {rlabel} ({label}):檔案存在但讀不出內容 → {path}")
                    elif rank == 1:
                        lines.append(f"✗ {tx_id} {rlabel} ({label}):無檔案 → {os.path.basename(path)}")
        if not any_found:
            lines += ["", "目前一份都沒有。請按「📥 期交所歷史」下載或匯入,",
                      "系統會自動存到上面那個資料夾 (不需要你手動搬檔案)。"]
        else:
            lines += ["", "回測/最佳化時,只要期交所資料涵蓋了你選的期間,",
                      "就會完全略過券商下載 (日誌會出現「完全略過券商下載」)。",
                      "只有「只用日盤」那份存在,才能在回測選「只用日盤」口徑。"]
        msg = "\n".join(lines)
        for ln in lines:
            if ln.strip():
                self.log_message(f"【期交所狀態】{ln}")
        messagebox.showinfo("期交所歷史資料狀態", msg, parent=self)

    def _taifex_prod_of(self, contract):
        if not contract:
            return None
        sym = str(getattr(contract, 'symbol', '') or getattr(contract, 'code', '') or '').upper()
        if 'R1' in sym or 'R2' in sym or 'R3' in sym:
            return taifex_daily.PRODUCT_MAP.get(fut_catalog.product_code(sym))
        return None

    def _taifex_plan_download(self, contract, asset_type, tf, start_dt, end_dt, session='all', tag="回測"):
        """【ADR-058/ADR-081】主圖與回測通用:判斷這段歷史是否「已經被期交所本地資料完整涵蓋」。"""
        try:
            if asset_type != "future" or tf not in ("日K", "周K", "月K"):
                return start_dt, end_dt, ""
            tx_id = self._taifex_prod_of(contract)
            if not tx_id:
                return start_dt, end_dt, ""
            rank = getattr(self, '_month_rank_of', lambda x: 1)(contract)
            hist = self._taifex_load_hist(tx_id, session=session, month_rank=rank)
            if (hist is None or hist.empty) and session == 'day':
                hist = self._taifex_load_hist(tx_id, session='all', month_rank=rank)
            if hist is None or hist.empty:
                return start_dt, end_dt, ""
            covered_until, need_from = taifex_daily.split_coverage(hist, start_dt, end_dt)
            if need_from is None:
                return None, None, (f"期交所本地歷史已完整涵蓋 {start_dt:%Y-%m-%d}~{end_dt:%Y-%m-%d},"
                                    f"完全略過券商下載 (不會再被流量管制擋)。")
            if pd.Timestamp(need_from) > pd.Timestamp(start_dt):
                days_saved = (pd.Timestamp(need_from) - pd.Timestamp(start_dt)).days
                return need_from, end_dt, (f"期交所本地歷史已涵蓋到 {covered_until:%Y-%m-%d},"
                                           f"券商只需補 {need_from:%Y-%m-%d} 之後 "
                                           f"(省下約 {days_saved} 天、大幅降低被流量管制的機會)。")
            return start_dt, end_dt, ""
        except Exception as e:
            self.safe_after(0, self.log_message, f"【{tag}下載】期交所涵蓋判斷失敗,改為全段下載: {e}")
            return start_dt, end_dt, ""

    def _extend_with_yahoo(self, pub_df, tf, sym=None, max_days=None):
        """對於股票/ETF，若 shioaji 資料不夠長，向 yfinance 請求深層歷史補齊。

        【ADR-080】max_days:選填,合併完成後只保留「最後一根往前 max_days 天」
        的範圍。回測/最佳化呼叫這個函式時不帶這個參數 (None=不裁切,維持原本
        的完整深度,不受影響);主圖 (_publish) 只在需要深歷史時 (MA240 開啟
        或週期為周K/月K) 才呼叫。ADR-141 後主圖固定帶 max_days
        保留至少 10 年；渲染成本不再藉刪掉歷史解決。"""
        try:
            if pub_df is None or pub_df.empty: return pub_df
            sym = sym or getattr(self, 'current_symbol', '')
            if not sym: return pub_df

            yf_params = {"日K": ("20y", "1d"), "周K": ("20y", "1wk"), "月K": ("20y", "1mo")}
            if tf not in yf_params: return pub_df
            period, interval = yf_params[tf]

            import yfinance as yf
            df_yf = pd.DataFrame()
            candidates = ([f"{sym}.TW", f"{sym}.TWO"] if sym[0].isdigit()
                          else [sym] if sym in ("^TWII", "^TWOII") else [])
            for yf_symbol in candidates:
                df_yf = yf.Ticker(yf_symbol).history(period=period, interval=interval, auto_adjust=False)
                if not df_yf.empty:
                    break
            if df_yf.empty: return pub_df

            df_yf.index = pd.to_datetime(df_yf.index)
            if df_yf.index.tz is not None: df_yf.index = df_yf.index.tz_localize(None)

            earliest_sj = pub_df.index[0]
            df_yf = df_yf[df_yf.index < earliest_sj]
            if df_yf.empty: return pub_df

            df_yf = df_yf[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
            # Convert yfinance Volume (Shares) to Shioaji Volume (Lots) for Taiwan stocks
            df_yf['Volume'] = df_yf['Volume'] / 1000
            merged = pd.concat([df_yf, pub_df]).sort_index()
            merged = merged[~merged.index.duplicated(keep='last')]
            if max_days is not None and not merged.empty:
                cutoff = merged.index[-1] - pd.Timedelta(days=int(max_days))
                merged = merged[merged.index >= cutoff]
            return merged
        except Exception as e:
            self.safe_after(0, self.log_message, f"【深層歷史】由 yfinance 補齊失敗: {e}")
            return pub_df

    def _extend_with_taifex(self, pub_df, tf, contract=None, session='all', max_days=None):
        """把期交所官方日K往前接在 shioaji 聚合結果之前。

        只延伸 R1 (近月連續) 合約:期交所序列是「每日近月」建構的連續日K,
        語意正好對應 R1;特定月份合約 (TXF202608) 或 R2 的歷史本來就不是
        這條序列,不硬接。任何失敗一律原樣回傳,絕不讓延伸功能弄壞主流程。

        【ADR-056 修正】contract 參數:舊版永遠讀 self.current_contract (主圖
        目前顯示的商品),回測/參數最佳化呼叫時「要回測的策略商品」常常不是
        主圖正在看的商品 (使用者可能圖表開著 TXFR1、卻在回測 MXFR1 策略),
        結果回測永遠延伸錯商品甚至延伸不到——這正是使用者回報「已經匯入
        期交所歷史,回測範圍卻還是只有~5年 (shioaji 深度)」的根因。
        現在呼叫端 (回測/最佳化 worker) 一律明確傳入自己解析出來的 contract;
        不傳 (主圖 _publish 呼叫) 才 fallback 用 self.current_contract,行為
        不變。"""
        try:
            if contract is None:
                contract = getattr(self, 'current_contract', None)
            tx_id = self._taifex_prod_of(contract)
            if not tx_id:
                return pub_df
            
            rank = getattr(self, '_month_rank_of', lambda x: 1)(contract)
            
            # 【ADR-058】依回測選定的盤別口徑取用對應的那一份期交所資料
            hist = self._taifex_load_hist(tx_id, session=session, month_rank=rank)
            if (hist is None or hist.empty) and session == 'day':
                self.safe_after(0, self.log_message,
                                f"【期交所歷史】{tx_id} 尚無「只用日盤」的資料檔,"
                                f"請重新匯入一次期交所歷史即可同時產生。本次改用近全序列。")
                hist = self._taifex_load_hist(tx_id, session='all', month_rank=rank)
            if hist is None or hist.empty:
                return pub_df
            out = taifex_daily.extend_shioaji_df(pub_df, hist, tf)
            if len(out) > len(pub_df) and (tx_id, session) not in self._taifex_extend_noted:
                self._taifex_extend_noted.add((tx_id, session))
                self.safe_after(0, self.log_message,
                                f"【期交所歷史】{tx_id} 已用官方每日行情往前延伸至 {out.index[0]:%Y-%m-%d}"
                                f" (盤別:{'只用日盤' if session == 'day' else '近全'};重疊日期以 shioaji 為準)。")
            # 【ADR-080/141】max_days:主圖保留使用者要求的至少 10 年，
            # 但不必無上限放入期交所 20+ 年全 series。
            # 回測/最佳化呼叫時不帶這個參數,拿到的仍是完整延伸序列,不受影響。
            if max_days is not None and not out.empty:
                cutoff = out.index[-1] - pd.Timedelta(days=int(max_days))
                out = out[out.index >= cutoff]
            return out
        except Exception as e:
            self.safe_after(0, self.log_message, f"【期交所歷史】延伸失敗 (不影響原圖): {e}")
            return pub_df

    def _taifex_default_prod(self):
        """依目前圖表商品推預設的期交所商品代碼;推不出來就 TX。"""
        try:
            sym = str(getattr(getattr(self, 'current_contract', None), 'symbol', '') or '').upper()
            return taifex_daily.PRODUCT_MAP.get(fut_catalog.product_code(sym)) or 'TX'
        except Exception:
            return 'TX'

    def open_taifex_import_dialog(self):
        """「📥 期交所歷史」對話框:選商品 + 日期區間 → 自動下載;或手動匯入
        官網下載的 CSV/ZIP。下載/解析都在背景執行緒,UI 用 safe_after 更新。"""
        if self._taifex_import_running:
            messagebox.showinfo("期交所歷史", "已有一個下載/匯入正在進行,請等它完成。")
            return
        win = tk.Toplevel(self); win.title("📥 期交所官方每日行情匯入 (更長日K歷史)")
        win.configure(bg="#1A2026"); win.geometry("560x300"); win.transient(self)

        tk.Label(win, text="資料來源:臺灣期交所官網免費「每日交易行情」。匯入後,期貨 R1 連續合約的\n"
                           "日/周/月K會自動往前延伸 (重疊日期仍以 shioaji 為準,鐵則 12 不變)。",
                 bg="#1A2026", fg="#8A99AD", justify=tk.LEFT).pack(anchor="w", padx=10, pady=(8, 4))

        row1 = tk.Frame(win, bg="#1A2026"); row1.pack(fill=tk.X, padx=10, pady=4)
        tk.Label(row1, text="商品:", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        prod_var = tk.StringVar(value=self.TAIFEX_PROD_LABELS.get(self._taifex_default_prod(), '臺股期貨 TX'))
        cb = ttk.Combobox(row1, textvariable=prod_var, state="readonly", width=18,
                          values=list(self.TAIFEX_PROD_LABELS.values()))
        cb.pack(side=tk.LEFT, padx=6)

        row2 = tk.Frame(win, bg="#1A2026"); row2.pack(fill=tk.X, padx=10, pady=4)
        tk.Label(row2, text="下載區間:", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        e_start = tk.Entry(row2, width=12, bg="#12161A", fg="white", insertbackground="white")
        e_start.insert(0, (datetime.now() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")); e_start.pack(side=tk.LEFT, padx=4)
        tk.Label(row2, text="~", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        e_end = tk.Entry(row2, width=12, bg="#12161A", fg="white", insertbackground="white")
        e_end.insert(0, datetime.now().strftime("%Y-%m-%d")); e_end.pack(side=tk.LEFT, padx=4)
        tk.Label(row2, text="(TX 最早 1998-07-21;區間越長下載越久)", bg="#1A2026", fg="#8A99AD").pack(side=tk.LEFT, padx=4)

        lbl_status = tk.Label(win, text="就緒。", bg="#1A2026", fg="#29B6F6", anchor="w", justify=tk.LEFT, wraplength=530)
        lbl_status.pack(fill=tk.X, padx=10, pady=6)

        def _set_status(msg, color="#29B6F6"):
            try:
                if lbl_status.winfo_exists():
                    lbl_status.config(text=msg, fg=color)
            except Exception:
                pass

        def _picked_prod():
            label = prod_var.get()
            for k, v in self.TAIFEX_PROD_LABELS.items():
                if v == label:
                    return k
            return 'TX'

        def _do_download():
            if self._taifex_import_running:
                return
            try:
                s = datetime.strptime(e_start.get().strip(), "%Y-%m-%d").date()
                e = datetime.strptime(e_end.get().strip(), "%Y-%m-%d").date()
            except ValueError:
                _set_status("日期格式錯誤,請用 YYYY-MM-DD。", "#FF1744"); return
            if s > e:
                _set_status("起日不可晚於迄日。", "#FF1744"); return
            tx_id = _picked_prod()
            earliest = taifex_daily.PRODUCT_EARLIEST.get(tx_id)
            if earliest and s < earliest:
                s = earliest
                _set_status(f"{tx_id} 最早資料為 {earliest},起日已自動調整。")
            self._taifex_import_running = True
            threading.Thread(target=self._taifex_download_worker,
                             args=(tx_id, s, e, _set_status), daemon=True).start()

        def _do_import_files():
            if self._taifex_import_running:
                return
            paths = filedialog.askopenfilenames(parent=win, title="選取期交所每日行情檔 (CSV/ZIP,可多選)",
                                                filetypes=[("期交所行情檔", "*.csv *.zip"), ("所有檔案", "*.*")])
            if not paths:
                return
            tx_id = _picked_prod()
            self._taifex_import_running = True
            threading.Thread(target=self._taifex_import_files_worker,
                             args=(tx_id, list(paths), _set_status), daemon=True).start()

        row3 = tk.Frame(win, bg="#1A2026"); row3.pack(fill=tk.X, padx=10, pady=8)
        tk.Button(row3, text="⬇ 自動下載 (官網逐段抓取)", bg="#29B6F6", fg="black", relief="flat",
                  command=_do_download).pack(side=tk.LEFT, padx=4)
        tk.Button(row3, text="📂 匯入已下載的 CSV/ZIP", bg="#FB8C00", fg="black", relief="flat",
                  command=_do_import_files).pack(side=tk.LEFT, padx=4)
        tk.Button(row3, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  command=win.destroy).pack(side=tk.RIGHT, padx=4)

    def _taifex_http_chunk(self, tx_id, s, e):
        """下載單一日期區段的期貨每日行情 CSV (官方 futDataDown 查詢)。回傳 bytes。
        失敗拋例外由呼叫端決定重試/略過。"""
        body = urllib.parse.urlencode({
            'down_type': '1', 'commodity_id': tx_id,
            'queryStartDate': s.strftime('%Y/%m/%d'), 'queryEndDate': e.strftime('%Y/%m/%d'),
        }).encode('utf-8')
        req = urllib.request.Request(self.TAIFEX_URL, data=body, headers={
            'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def _taifex_download_worker(self, tx_id, s, e, set_status):
        """背景執行緒:分段 (≤28 天,官網區間限制) 下載 → 解析 → 合併存檔。
        逐段禮貌節流 + 失敗重試一次;個別段失敗略過其餘照收,總結進日誌。"""
        try:
            chunks = taifex_daily.month_chunks(s, e)
            all_rows = []; fail = 0
            for i, (cs, ce) in enumerate(chunks, 1):
                self.safe_after(0, set_status, f"下載中 {i}/{len(chunks)} 段:{cs} ~ {ce} ...")
                rows = []
                for attempt in (1, 2):
                    try:
                        raw = self._taifex_http_chunk(tx_id, cs, ce)
                        rows = taifex_daily.extract_rows_from_bytes(raw)
                        break
                    except Exception:
                        if attempt == 2:
                            fail += 1
                        else:
                            time.sleep(2.0)
                all_rows.extend(rows)
                time.sleep(self.TAIFEX_PACE_SEC)
            n_days, span = self._taifex_merge_save(tx_id, all_rows)
            msg = (f"完成:{tx_id} 本次解析 {n_days} 個交易日,本地累計涵蓋 {span}。"
                   + (f" (有 {fail} 段下載失敗,可重按下載補抓)" if fail else ""))
            self.safe_after(0, set_status, msg, "#FF1744" if fail else "#00E676")
            self.safe_after(0, self.log_message, f"【期交所歷史】{msg} 重新查詢期貨商品即可看到延伸後的K線。")
        except Exception as ex:
            self.safe_after(0, set_status, f"下載失敗:{ex}", "#FF1744")
            self.safe_after(0, self.log_message, f"【期交所歷史】下載失敗: {ex}")
        finally:
            self._taifex_import_running = False

    def _taifex_import_files_worker(self, tx_id, paths, set_status):
        """背景執行緒:解析使用者自行從官網下載的 CSV/ZIP → 合併存檔。"""
        try:
            all_rows = []
            for i, p in enumerate(paths, 1):
                self.safe_after(0, set_status, f"解析檔案 {i}/{len(paths)}:{os.path.basename(p)} ...")
                with open(p, 'rb') as f:
                    all_rows.extend(taifex_daily.extract_rows_from_bytes(f.read(), filename=p))
            n_days, span = self._taifex_merge_save(tx_id, all_rows)
            if n_days == 0:
                self.safe_after(0, set_status,
                                f"檔案裡找不到 {tx_id} 的行情列 (請確認商品選對、檔案是期交所每日行情格式)。", "#FF1744")
                return
            msg = f"完成:{tx_id} 本次解析 {n_days} 個交易日,本地累計涵蓋 {span}。"
            self.safe_after(0, set_status, msg, "#00E676")
            self.safe_after(0, self.log_message, f"【期交所歷史】{msg} 重新查詢期貨商品即可看到延伸後的K線。")
        except Exception as ex:
            self.safe_after(0, set_status, f"匯入失敗:{ex}", "#FF1744")
            self.safe_after(0, self.log_message, f"【期交所歷史】匯入失敗: {ex}")
        finally:
            self._taifex_import_running = False

    def _taifex_merge_save(self, tx_id, rows):
        """解析列 → 前月連續日K → 與既有儲存合併 → 落地 + 清記憶體快取。
        回傳 (本次解析交易日數, 合併後涵蓋範圍字串)。

        【ADR-058】同一批原始列會產生「兩份」日K並各自存檔:
          * 近全 (all):含夜盤,與 ADR-007 交易日定義一致 → TX.csv
          * 只取日盤 (day):忽略盤後列 → TX_day.csv
        兩份都存,使用者回測時才能自由選口徑,不必為了換口徑重新下載一次。
        """
        n_new = 0
        span = "(尚無資料)"
        for sess in ('all', 'day'):
            new_df = taifex_daily.build_front_month_daily(rows, tx_id, session=sess)
            old = taifex_store.load_daily(self.TAIFEX_BASE_DIR, tx_id, session=sess)
            if new_df.empty:
                if sess == 'all' and not old.empty:
                    span = f"{old.index[0]:%Y-%m-%d} ~ {old.index[-1]:%Y-%m-%d}"
                continue
            merged = taifex_daily.merge_daily(old, new_df)
            taifex_store.save_daily(self.TAIFEX_BASE_DIR, tx_id, merged, session=sess)
            self._taifex_mem_cache.pop((tx_id, sess), None)
            if sess == 'all':
                n_new = len(new_df)
                span = f"{merged.index[0]:%Y-%m-%d} ~ {merged.index[-1]:%Y-%m-%d}"
        self._taifex_extend_noted.discard(tx_id)
        return n_new, span

    # ======================================================================
    # 【ADR-100】籌碼資料:下載 / 儲存 / 顯示
    #
    # 資料源全部是官方一手來源 (證交所/櫃買/期交所),不經第三方彙整——這是
    # ADR-011 移除 FinMind 時留下的條件 (「要恢復籌碼需先找到新資料源並另開
    # ADR」)。解析在 core/chips_parser.py、存檔在 data/chips_store.py,
    # 這裡只負責「HTTP 下載 + 背景執行緒 + 排回 UI」,分工同 ADR-049。
    #
    # 【與 shioaji 的關係】完全無關:籌碼與行情是兩條獨立路徑,不動 shioaji
    # 任何呼叫,鐵則 12「台股行情一律 shioaji」不受影響。
    # ======================================================================
    CHIPS_BASE_DIR = APP_DIR        # 與 broker_config.json 同層,存 chips/ 子目錄
    CHIPS_TWSE_MARKET_URL = 'https://www.twse.com.tw/rwd/zh/fund/BFI82U'
    CHIPS_TWSE_STOCK_URL = 'https://www.twse.com.tw/rwd/zh/fund/T86'
    CHIPS_TWSE_MARGIN_URL = 'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN'
    CHIPS_TPEX_STOCK_URL = 'https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade'
    CHIPS_TAIFEX_URL = 'https://www.taifex.com.tw/cht/3/futContractsDateDown'
    # 禮貌節流:證交所/櫃買只能單日查詢,抓一年 = 每個來源約 240 次請求。
    # 間隔太短有被官方端擋掉的風險 (且對公共服務不禮貌);1 秒是保守值。
    # 這與鐵則 5 (shioaji snapshots 節流) 是同一個精神,只是對象換成官方網站。
    CHIPS_REQUEST_INTERVAL = 1.0

    def _chips_http_json(self, url, params, timeout=30):
        """GET 官方 JSON 端點,回傳 dict。失敗拋例外,由呼叫端決定重試/略過。"""
        full = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 用 utf-8-sig:證交所 OpenAPI 的部分端點帶 BOM,純 utf-8 解碼後
            # 開頭會多一個 \ufeff 讓 json.loads 直接失敗。utf-8-sig 對「沒有
            # BOM 的 UTF-8」行為完全相同,所以一律用它比較安全。
            return json.loads(resp.read().decode('utf-8-sig'))

    def _chips_http_taifex(self, start_d, end_d, timeout=60):
        """POST 期交所三大法人 CSV (支援日期區間,一次可抓一整個月)。回傳 bytes。"""
        body = urllib.parse.urlencode({
            'down_type': '1', 'commodity_id': '',
            'queryStartDate': start_d.strftime('%Y/%m/%d'),
            'queryEndDate': end_d.strftime('%Y/%m/%d'),
        }).encode('utf-8')
        req = urllib.request.Request(self.CHIPS_TAIFEX_URL, data=body, headers={
            'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _chips_missing_days(self, start_dt, end_dt):
        """回傳區間內「還沒抓過」的日期清單 (跳過週末與本地已有的日期)。

        這是使用者要求的「存起來、日後不用重複抓取」的核心:每次更新只抓
        缺的那幾天。首次抓一年約 240 天,之後每天只補 1 天。
        (國定假日無法先驗判斷,抓到空資料時視為該日無交易,一樣記為已處理。)
        """
        s_iso, e_iso = start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d')
        have = chips_store.stock_inst_available_dates(self.CHIPS_BASE_DIR, s_iso, e_iso)
        out, cur = [], start_dt
        while cur <= end_dt:
            if cur.weekday() < 5 and cur.strftime('%Y-%m-%d') not in have:
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def _chips_update_worker(self, start_dt, end_dt, set_status):
        """背景執行緒:逐日抓證交所/櫃買,期交所按月批次抓,存檔後刷新畫面。

        設計重點:
        - 個別日期失敗不中斷整批 (官方偶發 5xx 很常見),最後統整報告失敗數。
        - 每個來源、每一天都獨立 try/except,呼應鐵則 8「不可包成一個大 try
          讓失敗無聲無息」。
        - 全程檢查 _closing 與 _chips_cancel,使用者關視窗或按停止能立刻收手
          (P-59:長時間背景工作要頻繁檢查,不可跑到底)。
        """
        try:
            days = self._chips_missing_days(start_dt, end_dt)
            total = len(days)
            if total == 0:
                self.safe_after(0, set_status, "本地已涵蓋此區間,無需下載。")
                self.safe_after(0, self.log_message, "【籌碼】本地資料已涵蓋指定區間,未發出任何請求。")
                return
            self.safe_after(0, self.log_message,
                            f"【籌碼】開始更新:{total} 個交易日待抓 "
                            f"(每日 4 個端點,間隔 {self.CHIPS_REQUEST_INTERVAL} 秒,預估約 "
                            f"{total * self.CHIPS_REQUEST_INTERVAL * 4 / 60:.0f} 分鐘)。")
            ok_days = 0
            fails = {'market': 0, 'stock': 0, 'margin': 0, 'tpex': 0}
            # 【ADR-119】累積緩衝:見下方寫檔次數的說明
            pend_market, pend_margin, pend_stock = [], [], {}
            no_tpex_days = 0        # 【ADR-119】櫃買沒資料的天數,最後彙總一次

            def _flush_simple():
                """把累積的市場法人/融資融券一次寫檔。"""
                if pend_market:
                    chips_store.upsert(chips_store.market_inst_path(self.CHIPS_BASE_DIR),
                                       pd.concat(pend_market, ignore_index=True))
                    pend_market.clear()
                if pend_margin:
                    chips_store.upsert(chips_store.margin_path(self.CHIPS_BASE_DIR),
                                       pd.concat(pend_margin, ignore_index=True))
                    pend_margin.clear()

            def _flush_stock(ym=None):
                """把累積的個股法人寫檔 (ym=None 代表全部)。"""
                keys = [ym] if ym else list(pend_stock.keys())
                for k in keys:
                    frames = pend_stock.pop(k, None)
                    if frames:
                        chips_store.upsert(
                            chips_store.stock_inst_path(self.CHIPS_BASE_DIR, k + '-01'),
                            pd.concat(frames, ignore_index=True))

            for i, day in enumerate(days, 1):
                if self._closing or self._chips_cancel:
                    _flush_simple(); _flush_stock()      # 中止前先把抓到的存起來
                    self.safe_after(0, self.log_message, f"【籌碼】已中止 (完成 {i - 1}/{total} 日)。")
                    self.safe_after(0, set_status, f"已中止 ({i - 1}/{total})")
                    return
                iso = day.strftime('%Y-%m-%d')
                ymd = day.strftime('%Y%m%d')
                self.safe_after(0, set_status, f"下載中 {i}/{total}:{iso} ...")
                got_any = False

                # (1) 市場三大法人 (加權指數層級)
                try:
                    payload = self._chips_http_json(self.CHIPS_TWSE_MARKET_URL,
                                                    {'dayDate': ymd, 'type': 'day', 'response': 'json'})
                    df = chips_parser.parse_twse_market_inst(payload, date_iso=iso)
                    if not df.empty:
                        pend_market.append(df)      # 【ADR-119】累積,不逐日寫檔
                        got_any = True
                except Exception:
                    fails['market'] += 1
                time.sleep(self.CHIPS_REQUEST_INTERVAL)

                # (2) 融資融券 (市場層級)
                try:
                    payload = self._chips_http_json(self.CHIPS_TWSE_MARGIN_URL,
                                                    {'date': ymd, 'selectType': 'MS', 'response': 'json'})
                    df = chips_parser.parse_twse_margin(payload, date_iso=iso)
                    if not df.empty:
                        pend_margin.append(df)      # 【ADR-119】累積,不逐日寫檔
                        got_any = True
                except Exception:
                    fails['margin'] += 1
                time.sleep(self.CHIPS_REQUEST_INTERVAL)

                # (3) 上市個股三大法人
                stock_frames = []
                try:
                    payload = self._chips_http_json(self.CHIPS_TWSE_STOCK_URL,
                                                    {'date': ymd, 'selectType': 'ALL', 'response': 'json'})
                    df = chips_parser.parse_twse_stock_inst(payload, date_iso=iso)
                    if not df.empty:
                        stock_frames.append(df)
                        got_any = True
                except Exception:
                    fails['stock'] += 1
                time.sleep(self.CHIPS_REQUEST_INTERVAL)

                # (4) 上櫃個股三大法人
                try:
                    payload = self._chips_http_json(self.CHIPS_TPEX_STOCK_URL,
                                                    {'type': 'Daily', 'sect': 'EW',
                                                     'date': day.strftime('%Y/%m/%d'),
                                                     'id': '', 'response': 'json'})
                    # 櫃買欄位分組沒有官方名稱,是靠恆等式反推的;改版時要看得見
                    status, why = chips_parser.verify_tpex_layout(payload)
                    if status == chips_parser.NO_DATA:
                        # 【ADR-119】沒有資料是預期中的 (日期太早/當日無交易),
                        # 靜靜跳過就好 —— 對每個沒資料的日子印一次紅字警告,
                        # 只會讓真正的版面問題被淹沒在洗版裡。
                        no_tpex_days += 1
                    elif status != chips_parser.LAYOUT_OK:
                        self.safe_after(0, self.log_message,
                                        f"【籌碼-警告】{iso} 櫃買欄位版面驗證失敗 ({why}),"
                                        f"該日上櫃資料已略過,不寫入可能錯誤的籌碼。")
                    else:
                        df = chips_parser.parse_tpex_stock_inst(payload, date_iso=iso)
                        if not df.empty:
                            stock_frames.append(df)
                            got_any = True
                except Exception:
                    fails['tpex'] += 1
                time.sleep(self.CHIPS_REQUEST_INTERVAL)

                if stock_frames:
                    # 【ADR-119】依「年月」分桶累積。原本每天都對月檔做一次
                    # read → concat → 去重 → write;月檔會長到數 MB,10 年就是
                    # 2364 次整檔重寫,而 pandas 這些運算是握著 GIL 做的 ——
                    # 背景執行緒把 UI 執行緒餓死,使用者按 X 都沒反應 (P-48)。
                    # 改成一個月只寫一次,寫檔次數從 2364 降到約 120。
                    pend_stock.setdefault(iso[:7], []).append(
                        pd.concat(stock_frames, ignore_index=True))
                if got_any:
                    ok_days += 1
                # 累積夠一個月就落地一次:全部堆到最後才寫的話,中途按停止或
                # 關程式,這一整批就白抓了 (下載本身很貴,不能有這種風險)。
                if len(pend_stock.get(iso[:7], [])) >= self.CHIPS_FLUSH_EVERY:
                    _flush_stock(iso[:7])
                if len(pend_market) >= self.CHIPS_FLUSH_EVERY:
                    _flush_simple()

            _flush_simple(); _flush_stock()      # 【ADR-119】日線段結束,把剩下的寫檔

            # (5) 期貨三大法人:支援日期區間,按月批次抓 (240 次 → 約 12 次)
            self.safe_after(0, set_status, "下載期貨籌碼 ...")
            fut_fail = 0
            for m_start, m_end in self._chips_month_chunks(start_dt, end_dt):
                if self._closing or self._chips_cancel:
                    break
                try:
                    raw = self._chips_http_taifex(m_start, m_end)
                    df = chips_parser.parse_taifex_inst(raw)
                    if not df.empty:
                        for year, part in df.groupby(df['Date'].str[:4]):
                            chips_store.upsert(
                                chips_store.futures_inst_path(self.CHIPS_BASE_DIR, year), part)
                except Exception:
                    fut_fail += 1
                time.sleep(self.CHIPS_REQUEST_INTERVAL)

            msg = f"【籌碼】更新完成:{ok_days}/{total} 個交易日有資料"
            bad = [f"{k}×{v}" for k, v in fails.items() if v] + ([f"期貨×{fut_fail}"] if fut_fail else [])
            if bad:
                msg += f";失敗 {', '.join(bad)} (可再按一次更新,已成功的不會重抓)"
            self.safe_after(0, self.log_message, msg)
            if no_tpex_days:
                # 彙總成一行,而不是每天印一次 —— 使用者要知道「有幾天沒上櫃資料」,
                # 但不需要看 2000 行一模一樣的訊息。
                self.safe_after(0, self.log_message,
                                f"【籌碼】其中 {no_tpex_days} 個交易日櫃買端沒有資料 "
                                f"(多為較早的日期,屬正常;上市資料不受影響)。")
            self.safe_after(0, set_status, f"完成:{ok_days}/{total} 日")
            self.safe_after(0, self._chips_refresh_view)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【籌碼】更新發生非預期錯誤:{type(e).__name__}: {e}")
            self.safe_after(0, set_status, "更新失敗 (詳見系統日誌)")
        finally:
            self._chips_running = False
            self.safe_after(0, self._chips_set_buttons_idle)

    @staticmethod
    def _chips_month_chunks(start_dt, end_dt):
        """把區間切成「每月一段」,供期交所區間查詢使用。"""
        out = []
        cur = start_dt.replace(day=1)
        while cur <= end_dt:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            seg_s = max(cur, start_dt)
            seg_e = min(nxt - timedelta(days=1), end_dt)
            if seg_s <= seg_e:
                out.append((seg_s, seg_e))
            cur = nxt
        return out

    # ---------------- 籌碼分頁 UI ----------------
    CHIPS_VIEWS = [('stock', '個股法人'), ('futures', '期貨法人'),
                   ('market', '大盤法人'), ('margin', '融資融券')]

    def _build_chips_panel(self, parent):
        """建立籌碼分頁:工具列 (更新/檢視切換/查詢) + Treeview 表格。"""
        self._chips_running = False
        self._chips_cancel = False
        self._chips_view = tk.StringVar(value='stock')

        bar = tk.Frame(parent, bg="#1A2026")
        bar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 0))

        self.btn_chips_update = tk.Button(
            bar, text="⬇ 更新籌碼 (近一年)", bg="#00C853", fg="black", relief="flat",
            font=('微軟正黑體', 9, 'bold'), padx=10, pady=2, command=self._chips_start_update)
        self.btn_chips_update.pack(side=tk.LEFT)
        self.btn_chips_stop = tk.Button(
            bar, text="■ 停止", bg="#2A323D", fg="white", relief="flat",
            font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
            state=tk.DISABLED, command=self._chips_stop_update)
        self.btn_chips_stop.pack(side=tk.LEFT, padx=(4, 10))
        # 【ADR-116】底部分頁只看得到兩三列,籌碼動輒數十列 —— 給一個開大視窗的入口。
        tk.Button(bar, text="🗖 開啟完整視窗", bg="#455A64", fg="white", relief="flat",
                  font=('微軟正黑體', 9), padx=8, pady=2,
                  command=lambda: self.open_panel_window('chips')).pack(side=tk.LEFT, padx=(0, 10))
        # 【ADR-118】抓多久的籌碼。下載是增量的,調大只會補以前沒抓過的那一段。
        tk.Label(bar, text="年數", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        self.cb_chips_years = ttk.Combobox(bar, width=6, state='readonly',
                                           style="BlackText.TCombobox",
                                           values=list(self.CHIPS_YEAR_CHOICES))
        self.cb_chips_years.current(0)
        self.cb_chips_years.pack(side=tk.LEFT, padx=(4, 10))

        for key, label in self.CHIPS_VIEWS:
            tk.Radiobutton(bar, text=label, variable=self._chips_view, value=key,
                           bg="#1A2026", fg="#FFFFFF", selectcolor="#12161A",
                           activebackground="#1A2026", activeforeground="#29B6F6",
                           font=('微軟正黑體', 9), command=self._chips_refresh_view
                           ).pack(side=tk.LEFT, padx=2)

        tk.Label(bar, text="代號:", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(10, 2))
        self.entry_chips_code = tk.Entry(bar, bg="#2A323D", fg="white", width=10,
                                         justify="center", insertbackground="white")
        self.entry_chips_code.pack(side=tk.LEFT)
        self.entry_chips_code.bind("<Return>", lambda e: self._chips_refresh_view())
        tk.Button(bar, text="查詢", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=8,
                  command=self._chips_refresh_view).pack(side=tk.LEFT, padx=4)

        self.lbl_chips_status = tk.Label(bar, text="", bg="#1A2026", fg="#8A99AD",
                                         font=('微軟正黑體', 9), anchor="w")
        self.lbl_chips_status.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        table = tk.Frame(parent, bg="#1A2026")
        table.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 2))
        self.tree_chips = ttk.Treeview(table, show="headings", height=5, style='Trades.Treeview')
        self.tree_chips.tag_configure('visible_row', foreground='#FFFFFF', background='#12161A')
        # 【鐵則 1】紅漲綠跌:買超紅、賣超綠 (籌碼的「買超」等同做多力道)
        self.tree_chips.tag_configure('buy', foreground='#FF1744', background='#12161A')
        self.tree_chips.tag_configure('sell', foreground='#00E676', background='#12161A')
        sb = tk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree_chips.yview)
        self.tree_chips.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_chips.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _chips_set_buttons_idle(self):
        """下載結束 (完成/中止/失敗) 後恢復按鈕狀態。"""
        try:
            self.btn_chips_update.config(state=tk.NORMAL, text="⬇ 更新籌碼 (近一年)")
            self.btn_chips_stop.config(state=tk.DISABLED)
        except (tk.TclError, AttributeError):
            pass

    # 【ADR-119】累積幾天才寫一次檔。太大→中途停止會白抓;太小→退回逐日
    # read-modify-write 把 UI 餓死。一個月左右是兩者的平衡點。
    CHIPS_FLUSH_EVERY = 20

    def _chips_start_update(self):
        """使用者按「更新籌碼」:抓近一年缺的日期。下載一律在背景執行緒。"""
        if self._chips_running:
            return
        end_dt = datetime.now()
        # 【ADR-118】年數可選:回測要有意義,一年的籌碼太短 (只有 ~240 個交易日,
        # 扣掉暖身根本沒幾期)。下載本身是增量的 —— 已經有的日期不會重抓,
        # 所以把年數調大只會多抓「以前沒抓過」的那一段。
        start_dt = end_dt - timedelta(days=365 * self._chips_years())
        self._chips_running = True
        self._chips_cancel = False
        self.btn_chips_update.config(state=tk.DISABLED, text="更新中 ...")
        self.btn_chips_stop.config(state=tk.NORMAL)

        def set_status(msg):
            try:
                self.lbl_chips_status.config(text=msg)
            except (tk.TclError, AttributeError):
                pass
        threading.Thread(target=self._chips_update_worker,
                         args=(start_dt, end_dt, set_status), daemon=True).start()

    def _chips_stop_update(self):
        self._chips_cancel = True
        self.lbl_chips_status.config(text="停止中 ...")

    CHIPS_YEAR_CHOICES = ('1 年', '2 年', '3 年', '5 年', '10 年')

    def _chips_years(self):
        """使用者選的籌碼年數 (預設 1 年,讀不到就退回 1)。"""
        try:
            return max(1, int(str(self.cb_chips_years.get()).split()[0]))
        except (AttributeError, ValueError, IndexError):
            return 1

    def _chips_refresh_view(self):
        """依目前檢視模式從本地檔案載入並填表 (純讀檔,不觸發下載)。"""
        view = self._chips_view.get()
        end_iso = datetime.now().strftime('%Y-%m-%d')
        start_iso = (datetime.now() - timedelta(days=365 * self._chips_years())).strftime('%Y-%m-%d')
        try:
            if view == 'stock':
                cols, rows, note = self._chips_rows_stock(start_iso, end_iso)
            elif view == 'futures':
                cols, rows, note = self._chips_rows_futures(start_iso, end_iso)
            elif view == 'market':
                cols, rows, note = self._chips_rows_simple(
                    chips_store.market_inst_path(self.CHIPS_BASE_DIR),
                    ['Date', 'Foreign', 'Trust', 'Dealer', 'InstTotal'],
                    ['日期', '外資(億)', '投信(億)', '自營商(億)', '三大法人合計(億)'],
                    net_col='InstTotal', money_unit='元')   # 【ADR-118】原始單位是元
            else:
                cols, rows, note = self._chips_rows_margin()   # 【ADR-118】
        except Exception as e:
            cols, rows, note = ['訊息'], [(f"讀取籌碼資料失敗:{type(e).__name__}: {e}",)], ''
        self._chips_fill_tree(cols, rows)
        try:
            self.lbl_chips_status.config(text=note)
        except (tk.TclError, AttributeError):
            pass

    def _chips_fill_tree(self, headings, rows):
        """【P-31】先備妥再刪再插:避免中途出錯留下永久空白清單。"""
        prepared = []
        for r in rows:
            vals = tuple('' if v is None else str(v) for v in r)
            tag = 'visible_row'
            # 最後一欄若是可解析的淨額,依紅漲綠跌上色 (鐵則 1)
            try:
                n = int(str(r[-1]).replace(',', ''))
                tag = 'buy' if n > 0 else ('sell' if n < 0 else 'visible_row')
            except (ValueError, TypeError, IndexError):
                pass
            prepared.append((vals, tag))
        try:
            self.tree_chips['columns'] = tuple(headings)
            for h in headings:
                self.tree_chips.heading(h, text=h)
                self.tree_chips.column(h, width=110, anchor="center")
            for i in self.tree_chips.get_children():
                self.tree_chips.delete(i)
            for vals, tag in prepared:
                self.tree_chips.insert("", "end", values=vals, tags=(tag,))
        except (tk.TclError, AttributeError):
            pass

    @staticmethod
    def _chips_fmt_int(v):
        try:
            return f"{int(v):,}"
        except (ValueError, TypeError):
            return str(v)

    def _chips_rows_stock(self, start_iso, end_iso):
        """個股法人:有填代號就看該股歷史,否則看最新一日的買超排行。"""
        df = chips_store.load_stock_inst_range(self.CHIPS_BASE_DIR, start_iso, end_iso)
        # 【ADR-118】買賣超原始單位是「股」,台股習慣講「張」(1張=1000股)。
        heads = ['日期', '代號', '名稱', '外資(張)', '投信(張)', '自營商(張)', '三大法人合計(張)']
        if df is None or df.empty:
            return heads, [], "本地尚無個股籌碼,請按「更新籌碼」下載。"
        code = (self.entry_chips_code.get() or '').strip().upper()
        if code:
            sub = df[df['Code'].astype(str).str.upper() == code].sort_values('Date', ascending=False)
            note = f"{code} 共 {len(sub)} 個交易日 (資料 {df['Date'].min()} ~ {df['Date'].max()})"
            if sub.empty:
                return heads, [], f"查無 {code} 的籌碼資料 (本地涵蓋 {df['Date'].min()} ~ {df['Date'].max()})"
        else:
            latest = df['Date'].max()
            sub = df[df['Date'] == latest].sort_values('InstTotal', ascending=False).head(100)
            note = f"最新 {latest} 三大法人買超前 100 名 (輸入代號可查個股歷史)"
        rows = [(r['Date'], r['Code'], r['Name'], unit_format.fmt_lots(r['Foreign']),
                 unit_format.fmt_lots(r['Trust']), unit_format.fmt_lots(r['Dealer']),
                 unit_format.fmt_lots(r['InstTotal'])) for _, r in sub.iterrows()]
        return heads, rows, note

    def _chips_rows_futures(self, start_iso, end_iso):
        """期貨法人:預設看臺股期貨,可用代號欄過濾商品名稱關鍵字。"""
        df = chips_store.load_futures_inst_range(self.CHIPS_BASE_DIR, start_iso, end_iso)
        # 【ADR-119】未平倉金額原始單位是千元,換成台灣慣用的億 (同 ADR-118)。
        heads = ['日期', '商品', '身份別', '未平倉淨額(口)', '未平倉淨額(億)', '交易淨額(口)']
        if df is None or df.empty:
            return heads, [], "本地尚無期貨籌碼,請按「更新籌碼」下載。"
        kw = (self.entry_chips_code.get() or '').strip()
        sub = df[df['Product'].astype(str).str.contains(kw, na=False)] if kw else \
            df[df['Product'].astype(str) == '臺股期貨']
        if sub.empty:
            return heads, [], f"查無商品含「{kw}」的期貨籌碼"
        sub = sub.sort_values(['Date', 'Investor'], ascending=[False, True]).head(300)
        note = (f"{'含「' + kw + '」' if kw else '臺股期貨'} 共 {len(sub)} 列"
                f" (資料 {df['Date'].min()} ~ {df['Date'].max()};留空預設臺股期貨)")
        rows = [(r['Date'], r['Product'], r['Investor'], unit_format.fmt_int(r['NetOI']),
                 unit_format.fmt_yi(r['NetOIAmount'], '仟元'), unit_format.fmt_int(r['NetTrade']))
                for _, r in sub.iterrows()]
        return heads, rows, note

    def _chips_rows_simple(self, path, cols, heads, net_col=None, money_unit=None):
        """大盤法人:單一 CSV,依日期新到舊列出。

        【ADR-118】money_unit 帶入原始單位 ('元'/'仟元') 時,數值換算成「億」;
        不帶就照原樣加千分位 (口數、張數這類單位本來就對的欄位)。
        """
        df = chips_store.load(path)
        if df is None or df.empty:
            return heads, [], "本地尚無此類籌碼,請按「更新籌碼」下載。"
        df = df.sort_values('Date', ascending=False).head(300)
        rows = []
        for _, r in df.iterrows():
            vals = []
            for c in cols:
                v = r.get(c, '')
                if c == 'Date':
                    vals.append(v)
                elif money_unit:
                    vals.append(unit_format.fmt_yi(v, money_unit))
                else:
                    vals.append(unit_format.fmt_int(v))
            rows.append(tuple(vals))
        note = f"共 {len(df)} 個交易日 (最新 {df['Date'].max()})"
        return heads, rows, note

    def _chips_rows_margin(self):
        """融資融券:金額換成億,並算出「融資增減」。

        【ADR-118】官方只給當日融資金額 (仟元),沒有前一日金額,所以增減要
        自己用相鄰兩個交易日相減。算之前一定要先**由舊到新**排序 —— 順序
        顛倒會讓增減的正負號整個相反,而使用者正是靠正負號判斷「融資在增加
        還是減少」,方向錯了結論就跟著錯。
        """
        heads = ['日期', '融資餘額(張)', '前日融資(張)', '融券餘額(張)', '前日融券(張)',
                 '融資金額(億)', '融資增減(億)']
        df = chips_store.load(chips_store.margin_path(self.CHIPS_BASE_DIR))
        if df is None or df.empty:
            return heads, [], "本地尚無融資融券資料,請按「更新籌碼」下載。"
        asc = df.sort_values('Date', ascending=True)
        deltas = unit_format.diff_series(list(asc.get('MarginAmountBalance', [])))
        asc = asc.assign(_MarginDelta=deltas)
        sub = asc.sort_values('Date', ascending=False).head(300)
        rows = [(r['Date'],
                 unit_format.fmt_int(r.get('MarginBalance')),
                 unit_format.fmt_int(r.get('MarginPrevBalance')),
                 unit_format.fmt_int(r.get('ShortBalance')),
                 unit_format.fmt_int(r.get('ShortPrevBalance')),
                 unit_format.fmt_yi(r.get('MarginAmountBalance'), '仟元'),
                 unit_format.fmt_signed_yi(r.get('_MarginDelta'), '仟元'))
                for _, r in sub.iterrows()]
        note = f"共 {len(df)} 個交易日 (最新 {df['Date'].max()});融資增減 = 當日金額 − 前一交易日"
        return heads, rows, note

    # 【ADR-028】期貨舊代號別名 (向下相容:MTX/FITX 是常見俗稱)
    FUT_ALIASES = {'MTX': 'MXF', 'FITX': 'TXF'}

    @staticmethod
    def _looks_like_futures_symbol(sym):
        """
        【第十一輪修正 → ADR-046 擴充】判斷代號「長得像期貨完整代號」。
        舊版要求前 3 碼全英文字母,導致小型臺指「週契約」(MX1/MX2/MX4/MX5,
        完整代號 MX4R1、MX1202607 等,商品碼含數字) 被誤判成台股 → 合約
        查無、報價 '--'。改委派 core/fut_catalog:首碼必為英文字母,
        第 2~3 碼容許英數,尾碼 R1/R2 或 6 位月份。純邏輯已入 core 並有測試。
        """
        return fut_catalog.looks_like_futures_symbol(sym)

    def _resolve_futures_contract(self, raw):
        """
        通用期貨解析:任何期貨商品英文代號 (TXF/MXF/ZEF/CDF...) → R1 連續合約。
        找不到 R1 就退而求其次拿該商品第一個合約 (近月)。查無此商品回傳 None。
        """
        try:
            raw_u = str(raw).upper()
            code = self.FUT_ALIASES.get(raw_u, raw_u)
            # 【第九輪 第6項】支援完整合約代號:TXF202609 (特定月份)、TXFR2 (次月連續)
            # → 取前 3 碼找商品群組,再用完整代號取該合約。
            if len(code) > 3:
                grp3 = getattr(self.sj_api.Contracts.Futures, code[:3], None)
                if grp3 is not None:
                    try:
                        exact = grp3.get(code)
                        if exact:
                            return exact
                    except Exception:
                        pass
                    try:
                        for cand in grp3:
                            if str(getattr(cand, 'symbol', '')).upper() == code:
                                return cand
                    except Exception:
                        pass
                # 完整代號查不到就繼續走一般流程 (前3碼 R1)
                code = code[:3]
            grp = getattr(self.sj_api.Contracts.Futures, code, None)
            if grp is None:
                return None
            c = None
            try:
                c = grp.get(f"{code}R1")
            except Exception:
                c = None
            if not c:
                # 【ADR-043 第1項】R1 連續合約不存在時 (某些商品如微型臺指 TMF
                # 可能沒有 R1),改取「最近到期月份」的實體合約:遍歷群組,挑
                # delivery_month/交割日最小 (最近) 的那個,而不是隨便第一個。
                try:
                    cands = [cand for cand in grp]
                    r1s = [x for x in cands if str(getattr(x, 'symbol', '')).upper().endswith('R1')]
                    if r1s:
                        return r1s[0]
                    def _month_key(x):
                        dm = str(getattr(x, 'delivery_month', '') or getattr(x, 'delivery_date', '') or '')
                        return dm or '999999'
                    dated = [x for x in cands if str(getattr(x, 'symbol', '')).upper()[3:4].isdigit()]
                    pool = dated if dated else cands
                    if pool:
                        nearest = min(pool, key=_month_key)
                        return nearest
                except Exception:
                    pass
            return c
        except Exception:
            return None

    def _trade_type_for_symbol(self, sym):
        """依代碼判斷策略編輯器「交易種類」應帶入股票或期貨,規則與
        on_watchlist_select 的市場模式判斷一致,只是把結果收斂成
        strategy_engine.TRADE_TYPES 看得懂的值 (股票/期貨)。"""
        sym = (sym or '').strip().upper()
        if not sym:
            return '股票'
        if self._looks_like_futures_symbol(sym):
            return '期貨'
        if any(ch.isdigit() for ch in sym):
            return '股票'
        if sym in self.FUT_ALIASES or sym in ("TXF", "MXF") or (self.api_logged_in and HAS_SJ and self._resolve_futures_contract(sym)):
            return '期貨'
        return '股票'

    def _watch_trade_type_for_symbol(self, sym):
        """【ADR-077】判斷「看A」商品的種類:指數/期貨/股票 (比 B 多一個指數)。
        指數優先 (^TWII/^TWOII 等),其餘沿用 _trade_type_for_symbol。"""
        sym = (sym or '').strip().upper()
        if not sym:
            return '指數'
        if strategy_engine.looks_like_index_symbol(sym):
            return '指數'
        return self._trade_type_for_symbol(sym)

    def _log_futures_candidates(self, raw):
        """
        【ADR-028】台期貨模式查無代號時,列出可用/相近的期貨商品代號,
        回應使用者「其他期貨商品代號要怎麼找尋」的需求。
        """
        try:
            cats = []
            for grp in self.sj_api.Contracts.Futures:
                try:
                    first = next(iter(grp), None)
                    if first is None:
                        continue
                    code = getattr(first, 'category', '') or str(getattr(first, 'code', ''))[:3]
                    name = getattr(first, 'name', '')
                    if code and (code, name) not in cats:
                        cats.append((code, name))
                except Exception:
                    continue
            key = str(raw).upper()
            hits = [(c, n) for c, n in cats if key in c.upper() or (n and key in str(n).upper())]
            show = hits if hits else cats[:20]
            if show:
                listing = "、".join(f"{c} {n}" for c, n in show[:20])
                more = f" ...共 {len(cats)} 種" if not hits and len(cats) > 20 else ""
                self.safe_after(0, self.log_message,
                    f"【期貨代號查詢】查無「{raw}」。{'相近' if hits else '可用'}商品代號: {listing}{more}")
            else:
                self.safe_after(0, self.log_message,
                    f"【錯誤】期貨合約查無 {raw},且無法列出商品清單,請確認已登入且合約下載完成。")
        except Exception:
            self.safe_after(0, self.log_message, f"【錯誤】期貨合約查無 {raw},請確認代號 (如 TXF 臺股期貨、MXF 小型臺指)。")

    # ================= 【第九輪 第6項】中文名稱搜尋 =================
    def _search_contracts_by_keyword(self, kw):
        """
        以關鍵字搜尋股票與期貨合約 (比對中文名稱與代號)。回傳結果列表,每筆:
        (market, load_sym, code, name, extra)。期貨會列出同商品的全部合約
        (近月/遠月各月份 + R1/R2 連續),股票期貨也在 Contracts.Futures 內一併涵蓋。
        純資料處理、不碰 UI,可在背景執行緒跑、也可離線測試。
        """
        kw = str(kw).strip()
        kw_u = kw.upper()
        results = []
        LIMIT = 300
        # --- 股票/ETF ---
        try:
            for c in self.sj_api.Contracts.Stocks:
                try:
                    name = str(getattr(c, 'name', '') or '')
                    code = str(getattr(c, 'code', '') or '')
                    if kw in name or (kw_u and kw_u == code.upper()):
                        results.append(("台股", code, code, name, "股票/ETF"))
                        if len(results) >= LIMIT:
                            return results
                except Exception:
                    continue
        except Exception:
            pass
        # --- 期貨 (每個商品列出全部合約:各月份 + R1/R2;含股票期貨) ---
        try:
            for grp in self.sj_api.Contracts.Futures:
                try:
                    for c in grp:
                        try:
                            name = str(getattr(c, 'name', '') or '')
                            sym = str(getattr(c, 'symbol', '') or getattr(c, 'code', '') or '')
                            month = str(getattr(c, 'delivery_month', '') or '')
                            if kw in name or (kw_u and kw_u in sym.upper()):
                                # 【ADR-046】shioaji 合約檔名稱有污染實例 (MXFR1 顯示
                                # 「小型臺指W2近月」);已知指數期貨家族用官方名稱表重建。
                                name = fut_catalog.display_name(sym, name)
                                extra = "連續(近月)" if sym.upper().endswith("R1") else (
                                        "連續(次月)" if sym.upper().endswith("R2") else (f"{month} 月份合約" if month else "月份合約"))
                                results.append(("台期貨", sym, sym, name, extra))
                                if len(results) >= LIMIT:
                                    return results
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass
        return results

    def _symbol_search_worker(self, kw):
        """背景搜尋,完成後排回 UI 開啟結果選擇視窗。"""
        try:
            results = self._search_contracts_by_keyword(kw)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【搜尋】搜尋失敗: {type(e).__name__}: {e}")
            return
        if not results:
            self.safe_after(0, self.log_message, f"【搜尋】找不到名稱含「{kw}」的股票或期貨,請換個關鍵字 (例如公司簡稱)。")
            return
        if len(results) == 1:
            m, load_sym, code, name, extra = results[0]
            self.safe_after(0, self.log_message, f"【搜尋】唯一符合: {code} {name},直接載入。")
            self.safe_after(0, self._load_search_result, m, load_sym)
            return
        self.safe_after(0, self._open_symbol_search_dialog, kw, results)

    def _open_symbol_search_dialog(self, kw, results):
        """搜尋結果卷軸選單:市場|代碼|名稱|說明,雙擊或按「載入」選用。"""
        dlg = tk.Toplevel(self)
        dlg.title(f"搜尋「{kw}」— 共 {len(results)} 筆,請選擇")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 560, 420)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass
        tk.Label(dlg, text="雙擊或選取後按「載入」。期貨含各月份與 R1/R2 連續合約 (K線含一般盤+夜盤全日資料)。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).pack(padx=10, pady=(8, 2), anchor="w")
        frame = tk.Frame(dlg, bg="#1A2026"); frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        cols = ("market", "code", "name", "extra")
        tv = ttk.Treeview(frame, columns=cols, show="headings", style='Trades.Treeview')
        for c, txt, w in (("market", "市場", 70), ("code", "代碼", 110), ("name", "名稱", 170), ("extra", "說明", 150)):
            tv.heading(c, text=txt); tv.column(c, width=w, anchor="center")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        idx_map = {}
        for i, (m, load_sym, code, name, extra) in enumerate(results):
            iid = f"r{i}"
            idx_map[iid] = (m, load_sym)
            tv.insert("", tk.END, iid=iid, values=(m, code, name, extra), tags=('visible_row',))
        tv.tag_configure('visible_row', foreground='#FFFFFF', background='#12161A')

        def _pick(event=None):
            iid = tv.focus() or ((tv.selection() or [None])[0])
            if not iid or iid not in idx_map:
                return
            m, load_sym = idx_map[iid]
            dlg.destroy()
            self._load_search_result(m, load_sym)
        tv.bind("<Double-1>", _pick)
        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(fill=tk.X, padx=10, pady=(4, 10))
        tk.Button(btns, text="載入選取標的", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=14, pady=3, command=_pick).pack(side=tk.LEFT)
        tk.Button(btns, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=16, pady=3, command=dlg.destroy).pack(side=tk.RIGHT)

    def _load_search_result(self, market, load_sym):
        """把搜尋選到的標的載入:切市場模式 → 填代碼 → 查詢。"""
        try:
            self.market_mode.set(market)
            self.entry_symbol.delete(0, tk.END)
            self.entry_symbol.insert(0, load_sym)
            self.start_fetch_thread()
        except Exception as e:
            self.log_message(f"【搜尋】載入失敗: {type(e).__name__}: {e}")

    def _resubscribe_quotes_worker(self, prev_contract, contract, asset_type):
        """【切換加速】退訂舊合約 + 訂閱新合約的 shioaji 網路呼叫,獨立於
        K 線出圖在背景執行,讓「點自選股 → 主圖馬上換」不用等訂閱來回。

        用 self.subscribe_lock 序列化,確保快速連點時不會多路訂閱交錯
        (退舊 → 訂新 的先後順序要維持)。每一路訂閱各自 try/except 並記錄
        成功/失敗 (鐵則 8),不可包成一個大 try 讓失敗無聲無息。
        """
        with self.subscribe_lock:
            try:
                if prev_contract is not None:
                    for odd_flag in [True, False]:
                        try: self.sj_api.quote.unsubscribe(prev_contract, quote_type=sj.constant.QuoteType.Tick, intraday_odd=odd_flag)
                        except: pass
                        try: self.sj_api.quote.unsubscribe(prev_contract, quote_type=sj.constant.QuoteType.BidAsk, intraday_odd=odd_flag)
                        except: pass

                # 【切換加速】快速連點時,若在排隊等鎖期間使用者已經切到別檔
                # (current_contract 已不是我們這一路要訂的合約),就別再花配額訂
                # 這個過期合約——退訂已做完,下一路 worker 會接手訂新的。
                if getattr(self, 'current_contract', None) is not contract:
                    return

                def _sub(qtype, odd, label):
                    try:
                        try:
                            self.sj_api.quote.subscribe(contract, quote_type=qtype, version=sj.constant.QuoteVersion.v1, intraday_odd=odd)
                        except TypeError:
                            # 舊版簽名不吃 version 關鍵字時的相容退路
                            self.sj_api.quote.subscribe(contract, quote_type=qtype, intraday_odd=odd)
                        return True
                    except Exception as sub_e:
                        self.safe_after(0, self.log_message, f"【訂閱失敗】{label}: {sub_e}")
                        return False

                ok_t = _sub(sj.constant.QuoteType.Tick, False, "整股Tick")
                ok_b = _sub(sj.constant.QuoteType.BidAsk, False, "整股五檔")
                if asset_type == "stock":
                    ok_ot = _sub(sj.constant.QuoteType.Tick, True, "零股Tick")
                    ok_ob = _sub(sj.constant.QuoteType.BidAsk, True, "零股五檔")
                    self.safe_after(0, self.log_message, f"【訂閱結果】整股Tick:{'✓' if ok_t else '✗'} 整股五檔:{'✓' if ok_b else '✗'} 零股Tick:{'✓' if ok_ot else '✗'} 零股五檔:{'✓' if ok_ob else '✗'}")
                else:
                    self.safe_after(0, self.log_message, f"【訂閱結果】Tick:{'✓' if ok_t else '✗'} 五檔:{'✓' if ok_b else '✗'}")
            except Exception as e:
                self.safe_after(0, self.log_message, f"訂閱報價串流異常: {e}")

    def fetch_data_worker(self, raw_sym, tf, seq=None, market="台股", ma240_on=False):
        """
        【ADR-011】資料源政策改版:
          - 台股 (股票/ETF/指數/期貨) 一律使用 shioaji，不再有 yfinance/FinMind 備援。
            未登入券商 API 時直接報錯並退出，不會安靜地退化成其他資料源。
          - 美股自動使用 yfinance (shioaji 本來就不支援美股)，不需要手動切換。
        """
        # 【第十七輪修正】標記查詢進行中:主圖自動更新在這段期間必須完全讓路,
        # 否則會 (1) 併發呼叫 kbars 干擾下載、(2) 把新商品資料合併進舊商品 df。
        self._fetch_in_progress = True
        try:
            self._fetch_data_worker_impl(raw_sym, tf, seq, market, ma240_on)
        finally:
            self._fetch_in_progress = False

    def _fetch_data_worker_impl(self, raw_sym, tf, seq=None, market="台股", ma240_on=False):
        try:
            yf_params = {"1分K": ("5d", "1m"), "5分K": ("30d", "5m"), "15分K": ("30d", "15m"), "30分K": ("30d", "30m"), "60分K": ("90d", "60m"), "日K": (history_policy.US_HISTORY_PERIOD, "1d"), "周K": (history_policy.US_HISTORY_PERIOD, "1wk"), "月K": (history_policy.US_HISTORY_PERIOD, "1mo")}
            period, interval = yf_params.get(tf, ("1y", "1d"))

            contract = None; search_sym = raw_sym; stock_name = ""
            # 【ADR-028】依使用者指定的市場模式判斷,不再只靠「有沒有數字」猜:
            #   台股:數字代碼 (股票/ETF)、^TWII/^TWOII 指數、TXF/MXF 等舊代號 (向下相容)
            #   台期貨:任何期貨英文代號 (TXF/MXF/ZEF/CDF...),一律走期貨合約解析
            #   美股:直接走 yfinance,不再被「純英文=美股」以外的規則干擾
            if market == "美股":
                is_taiwan_instrument = False
            elif market == "台期貨":
                is_taiwan_instrument = True
            else:
                is_tw = any(c.isdigit() for c in raw_sym) and not raw_sym.startswith('^')
                is_taiwan_instrument = is_tw or raw_sym in ("^TWII", "^TWOII", "TXF", "MTX", "FITX", "MXF") or self._looks_like_futures_symbol(raw_sym)

            if is_taiwan_instrument:
                self.asset_type = "stock"  # 預設值,下面依實際合約類型覆蓋 (index_tw/future)
                self.data_source = ""

                if not (self.api_logged_in and HAS_SJ):
                    self.safe_after(0, self.log_message, f"【錯誤】{raw_sym} 是台股/期貨/指數，資料僅使用券商 shioaji API，請先登入券商實盤 API 再查詢。")
                    return

                try:
                    if market == "台期貨":
                        contract = self._resolve_futures_contract(raw_sym)
                        # 【ADR-114 追修】1.7 的 FuturesInfo 移除了 symbol,要改用 code。
                        # 這裡原本直接讀 .symbol,升級後會取不到而讓期貨查詢整個失效。
                        if contract:
                            _fs = sj_compat.contract_symbol(contract)
                            search_sym = _fs; stock_name = fut_catalog.display_name(_fs, contract.name); self.asset_type = "future"
                    elif raw_sym == "^TWII":
                        contract = self.brokers['sinopac'].index_contract('TSE')   # 【ADR-114】
                        if contract: search_sym = raw_sym; stock_name = "加權指數"; self.asset_type = "index_tw"
                    elif raw_sym == "^TWOII":
                        contract = self.brokers['sinopac'].index_contract('OTC')   # 【ADR-114】
                        if contract: search_sym = raw_sym; stock_name = "櫃買指數"; self.asset_type = "index_tw"
                    elif raw_sym in ['TXF', 'MTX', 'FITX', 'MXF'] or self._looks_like_futures_symbol(raw_sym):
                        # 【第十一輪修正】台股模式下輸入/點選期貨完整代號 (TXFR2 等)
                        # 也走期貨解析,不再掉進股票查詢報「合約查無」。
                        contract = self._resolve_futures_contract(raw_sym)
                        # 【ADR-114 追修】1.7 的 FuturesInfo 移除了 symbol,要改用 code。
                        # 這裡原本直接讀 .symbol,升級後會取不到而讓期貨查詢整個失效。
                        if contract:
                            _fs = sj_compat.contract_symbol(contract)
                            search_sym = _fs; stock_name = fut_catalog.display_name(_fs, contract.name); self.asset_type = "future"
                    else:
                        contract = self.brokers['sinopac'].stock_contract(raw_sym)  # 【ADR-114】
                        if contract: search_sym = raw_sym; stock_name = contract.name; self.asset_type = "stock"
                except Exception:
                    pass

                if not contract:
                    if market == "台期貨":
                        # 【ADR-028】幫使用者「找尋期貨代號」:列出可用/相近的商品代號
                        self._log_futures_candidates(raw_sym)
                    else:
                        self.safe_after(0, self.log_message, f"【錯誤】券商合約查無 {raw_sym}，請確認代碼是否正確 (股票/ETF代碼、TXF/MTX 期貨、^TWII/^TWOII 指數)。")
                    return

                # 【ADR-024】同商品換週期不需要退訂/重訂閱報價,也不清成交明細——
                # 沿用既有串流,換週期立即有報價,不會白等重新訂閱。
                prev_contract = getattr(self, 'current_contract', None)
                prev_code = getattr(prev_contract, 'code', None)
                new_code = getattr(contract, 'code', None)
                contract_changed = (prev_code is None) or (prev_code != new_code)
                if not contract_changed:
                    self.safe_after(0, self.log_message, "【快速切換】同商品換週期,沿用既有報價訂閱。")
                try:
                    # 【切換加速】換商品時,先「同步」把畫面/狀態需要的東西更新掉
                    # (這些都是純記憶體操作,很快),再把真正慢的 shioaji 退訂/訂閱
                    # 網路呼叫丟到背景執行緒。這樣主 worker 可以立刻往下走去讀快取
                    # 出圖,K 線圖點一下就換,不用再等 4 路訂閱來回。
                    if contract_changed:
                        with self.quote_lock:
                            self.current_contract = contract
                            self.current_bidask_normal = None
                            self.current_bidask_odd = None
                            self.current_tick_normal = None
                            self.current_tick_odd = None
                            # 換股清空成交明細,避免把上一檔的跳動記錄殘留在畫面上
                            self.trade_feed_normal.clear()
                            self.trade_feed_odd.clear()
                        self.odd_no_stream_warned = False

                        # 【ADR-008】擷取現沖狀態 (day_trade) 與參考價 (reference,即昨收/平盤價)。
                        # 這兩個值分別用來畫「現沖/禁現沖」badge,以及成交明細跳動列表的漲跌計算基準。
                        # (讀的是合約物件上既有屬性,非網路呼叫,留在此同步做。)
                        try:
                            self.current_day_trade = (str(getattr(contract, 'day_trade', '')) in ('Yes', 'DayTrade.Yes', 'DayTrade.Yes: Yes') or getattr(getattr(contract, 'day_trade', None), 'value', '') == 'Yes')
                        except Exception:
                            self.current_day_trade = False
                        try:
                            self.current_reference_price = float(getattr(contract, 'reference', 0) or 0)
                        except Exception:
                            self.current_reference_price = 0.0
                        # 【使用者調整#5】換新標的時,由系統依這檔股票是否開放現沖，重新決定
                        # 「現股當沖(先賣後買)」checkbox 的預設勾選狀態 (詳見 ADR-015/018)。
                        self.safe_after(0, lambda: self.daytrade_var.set(bool(self.current_day_trade)))
                        self.safe_after(0, self.update_daytrade_badge)

                        # 【切換加速】退訂舊合約 + 訂閱新合約 (共 2~4 路 shioaji 網路呼叫)
                        # 移到背景執行緒,不擋 K 線出圖。asset_type 先在此定住傳進去,
                        # 避免背景執行緒讀到之後又被別檔覆蓋的值。
                        threading.Thread(
                            target=self._resubscribe_quotes_worker,
                            args=(prev_contract, contract, self.asset_type),
                            daemon=True).start()
                except Exception as e:
                    self.safe_after(0, self.log_message, f"報價訂閱排程異常: {e}")
            else:
                # 美股:自動使用 yfinance,shioaji 本來就不支援美股。
                self.asset_type = "us_stock"
                self.data_source = ""

            df = pd.DataFrame()
            adjust_flag = self.var_adjusted.get()

            # 【ADR-024】發布函式:worker 的每一次「出圖」都經過這裡。
            # - seq 防護:發布前檢查序號,若使用者已切到別的商品/週期就放棄,
            #   避免慢的舊查詢 (台指期) 最後才回來把新圖蓋掉。
            # - full_ui:第一次發布更新價格欄/面板/hover;背景補全時只換圖,
            #   並在 UI 執行緒讀取目前視角、依「前面新增了幾根K」平移 xlim,
            #   使用者看到的視窗不會跳動。
            _pub_state = {'n': None}  # 上次實際發布的K棒根數

            # 【ADR-141】主圖日/周/月K一律保留至少 10 年。近期段仍由
            # shioaji 保證新鮮度，早期股票/指數由既有 Yahoo 深層歷史、
            # 期貨由期交所官方日行情接齊；不把 10 年份 1 分K全壓給券商 API。
            # 絕對不能略過期交所資料的拼接,因為 _taifex_plan_download 已經把券商下載
            # 範圍縮減到最近幾天了 (依靠期交所補足);若略過拼接,圖上會只剩下 3 根 K 棒！
            EXT_MAX_DAYS = history_policy.DEEP_HISTORY_DAYS

            def _publish(pub_df, full_ui=True, prev_len=None, note="", allow_extend=False):
                if seq is not None and seq != self._fetch_seq:
                    return False  # 已有更新的查詢,放棄發布
                is_deep_tf = history_policy.needs_deep_history(tf)
                # 所有週期都合併 SQLite：分K 也是高成本資料，不應重複下載。
                stored = kbars_store.load(self.kbars_db_file, search_sym,
                                          self.asset_type, tf)
                if stored is not None and not stored.empty:
                    pub_df = pd.concat([stored, pub_df]).sort_index()
                    pub_df = pub_df[~pub_df.index.duplicated(keep='last')]
                if allow_extend:
                    target_start = pd.Timestamp.now().tz_localize(None) - pd.Timedelta(
                        days=history_policy.DEEP_HISTORY_DAYS)
                    has_ten_years = (is_deep_tf and not pub_df.empty
                                     and pd.Timestamp(pub_df.index[0]).tz_localize(None) <= target_start)
                    if self.asset_type == "future" and is_deep_tf and not has_ten_years:
                        pub_df = self._extend_with_taifex(pub_df, tf, max_days=EXT_MAX_DAYS)
                    elif self.asset_type == "stock" and is_deep_tf and not has_ten_years:
                        pub_df = self._extend_with_yahoo(pub_df, tf, sym=search_sym, max_days=EXT_MAX_DAYS)
                    elif self.asset_type == "index_tw" and is_deep_tf and not has_ten_years:
                        pub_df = self._extend_with_yahoo(pub_df, tf, sym=search_sym, max_days=EXT_MAX_DAYS)
                pub_df = pub_df.dropna(subset=['Open', 'High', 'Low', 'Close'])
                if pub_df.empty:
                    return False
                try:
                    kbars_store.upsert(self.kbars_db_file, search_sym,
                                       self.asset_type, tf, pub_df)
                except Exception as e:
                    self.safe_after(0, self.log_message,
                                    f"【SQLite】K線增量儲存失敗: {e}")
                # 【ADR-049】記錄實際發布根數。背景補全的 prev_len 必須用
                # 「上次實際發布的根數」計算平移量,xlim 才不會跳離原視角。
                _pub_state['n'] = len(pub_df)
                pub_df = pub_df.copy()
                if pub_df.index.tz is not None:
                    pub_df.index = pub_df.index.tz_localize(None)
                pub_df.index = pd.to_datetime(pub_df.index)
                
                if adjust_flag and self.asset_type == "stock" and is_taiwan_instrument:
                    try:
                        from data import dividend_store
                        adjusted = dividend_store.adjust_dataframe(pub_df, search_sym)
                        if adjusted and full_ui:
                            self.safe_after(0, self.log_message, f"【還原權息】已套用 {search_sym} 的除權息價格調整。")
                    except Exception as e:
                        self.safe_after(0, self.log_message, f"【還原權息】調整失敗: {e}")

                self.current_symbol = search_sym; self.current_stock_name = stock_name; self.current_df = pub_df
                self.current_timeframe = tf  # 【第十六輪 第6項】自動更新 worker 依此判斷K棒邊界

                latest_p = pub_df['Close'].iloc[-1]
                tick = self.get_tick(latest_p)
                if tick >= 1: format_str = f"{int(latest_p)}"
                elif tick == 0.5 or tick == 0.1: format_str = f"{latest_p:.1f}"
                else: format_str = f"{latest_p:.2f}"

                def update_ui():
                    try:
                        if seq is not None and seq != self._fetch_seq:
                            return
                        if full_ui:
                            # 【ADR-008】entry_price 在「盤後定價」模式下是 disabled,
                            # 寫入前先暫時解鎖,再依目前交易別鎖回。
                            was_disabled = (str(self.entry_price['state']) == 'disabled')
                            if was_disabled: self.entry_price.config(state="normal")
                            self.entry_price.delete(0, tk.END)
                            self.entry_price.insert(0, format_str)
                            if was_disabled or self.trade_mode == "Fixing":
                                self.entry_price.config(state="disabled")
                            # 換股後 asset_type 可能從股票變期貨或反之,同步下單面板
                            self.update_order_panel_for_asset_type()
                            # 換股清 hover 資訊列 (詳見 ADR-018)
                            self.lbl_hover_info.config(text="滑鼠游標移至 K 線圖上方以顯示詳細資訊...", fg="#29B6F6")
                            self.last_hover_idx = -1
                        elif prev_len is not None and self.axlist:
                            # 背景補全:歷史K是「往前面加」,positional x 索引整體右移。
                            # ADR-083:區分「系統預設視角」與「使用者手調視角」:
                            # - 預設視角(_view_is_auto_default=True):新資料進來時重算視角寬度
                            # - 手調視角(_view_is_auto_default=False):保留使用者選擇的位置
                            try:
                                added = len(pub_df) - prev_len
                                if added > 0:
                                    if self._view_is_auto_default:
                                        # 使用者還沒手調過,重置 saved_xlim 讓 draw_chart 用實際根數重新計算視角
                                        self.saved_xlim = None
                                    else:
                                        # 使用者已手調過,保留位置平移
                                        x0, x1 = self.axlist[0].get_xlim()
                                        self.saved_xlim = (x0 + added, x1 + added)
                            except Exception:
                                pass
                        if note:
                            self.log_message(note)
                        self.draw_chart(pub_df)
                    except Exception as e:
                        self.log_message(f"介面更新異常: {e}")
                self.safe_after(0, update_ui)
                return True

            if is_taiwan_instrument:
                self.data_source = "shioaji"
                _t0 = time.time()  # 【ADR-068】計時起點:量「點下去→出圖」實際耗時,寫進日誌方便定位慢在哪段
                days = self.SJ_DAYS.get(tf, 180)
                end_dt = datetime.now(); start_dt = end_dt - timedelta(days=days)
                if self.asset_type == "future":
                    tx_id = self._taifex_prod_of(contract)
                    earliest = taifex_daily.PRODUCT_EARLIEST.get(tx_id)
                    if earliest:
                        earliest_dt = datetime.combine(earliest, datetime.min.time())
                        if start_dt < earliest_dt:
                            start_dt = earliest_dt
                cache_key = search_sym
                # ADR-142:DB 已有歷史後，券商只回抓最新一週（含重疊
                # 修正形成中 K 棒）；舊歷史由 _publish 從 SQLite 合併。
                _db_first, _db_last, _db_count = kbars_store.coverage(
                    self.kbars_db_file, search_sym, self.asset_type, tf)
                if _db_count and _db_last:
                    start_dt = max(start_dt, pd.Timestamp(_db_last).to_pydatetime()
                                   - timedelta(days=7))

                # ---- 第一優先:快取涵蓋範圍 → 立即重採樣出圖 (秒開) ----
                cached_raw, cache_fresh = self._kbars_cache_get(cache_key, start_dt)
                published_from_cache = False
                cached_len = None
                if cached_raw is not None and not cached_raw.empty:
                    try:
                        df_cached = self._resample_sj_df(cached_raw, tf)
                        # 【ADR-080】快取命中且新鮮時會直接 return (下面 cache_fresh 分支),
                        # 這是唯一一次出圖,若 want_deep 成立必須在這裡就延伸,不能靠後面
                        # 的完整段補 (那段在 cache_fresh 時根本不會執行)。
                        if _publish(df_cached, full_ui=True, allow_extend=True):
                            published_from_cache = True
                            cached_len = _pub_state['n']
                    except Exception:
                        pass
                if published_from_cache and cache_fresh:
                    return  # 快取新鮮,直接完工——這就是「換週期/切回商品秒開」的路徑

                # ---- 兩段式第一段:先抓小範圍搶先出圖 ----
                # 【ADR-068/069】先抓 QUICK_DAYS 的小範圍「單次」下載搶先出圖,K 線
                # 馬上可看可 hover;完整的 SJ_DAYS 範圍 (日K 365天/周K 1095天/
                # 月K 1825天,見 ADR-079) 在同一背景執行緒接著補全 (補完就地換圖,
                # 視角不動)。這裡加速的是「shioaji 下載本身」(大範圍分K單次下載
                # 慢,ADR-069 的根因);【ADR-079】已把主圖的歷史延伸整個拿掉,所以
                # 就算是「完整段」,現在也只是 SJ_DAYS 這個有界範圍,不是以前
                # 上千~上萬根的 yahoo/期交所延伸資料。
                quick_len = None
                want_quick = self.asset_type in ("future", "index_tw", "stock")
                # 【ADR-142 修正】SQLite 建庫之後,上面那道
                # `start_dt = max(start_dt, db_last - 7天)` 會把「完整段」縮成
                # 最近一週 —— 那跟快速段的窗口幾乎一樣 (日K 的 QUICK_DAYS 也是 7)。
                # 結果是**每一次載入都對同一個窗口打兩次 kbars**:快速段一次、
                # 完整段再一次,兩次拿回來的東西完全相同。診斷的
                # 「分段下載不應重複抓同一起點」就是抓到這件事。
                #
                # 完整段沒有比快速段更深的時候,快速段就沒有存在的理由 ——
                # 它的用途是「先給一小段搶先出圖,大範圍在背景慢慢補」,
                # 而現在完整段本身就只有一小段,直接跑它就好。
                # 比的是**日期**不是 datetime:送給 kbars 的是 '%Y-%m-%d' 字串,
                # 兩個時間點只要落在同一天,對券商而言就是同一個請求。
                if want_quick and tf in self.QUICK_DAYS:
                    _q_start_probe = end_dt - timedelta(days=self.QUICK_DAYS[tf])
                    if start_dt.date() >= _q_start_probe.date():
                        want_quick = False
                if (not published_from_cache) and want_quick and tf in self.QUICK_DAYS:
                    try:
                        q_days = self.QUICK_DAYS[tf]
                        q_start = end_dt - timedelta(days=q_days)
                        self.safe_after(0, self.log_message, f"⚡ 先載入近 {q_days} 天搶先出圖,完整歷史背景補全中...")
                        quick_raw = self._download_kbars_raw(contract, q_start, end_dt)
                        if quick_raw is not None and not quick_raw.empty:
                            df_quick = self._resample_sj_df(quick_raw, tf)
                            if _publish(df_quick, full_ui=True):
                                quick_len = _pub_state['n']
                                self.safe_after(0, self.log_message, f"⚡ 已搶先出圖 (近 {q_days} 天,可開始看盤/hover),耗時 {time.time()-_t0:.1f} 秒;完整歷史背景補全中...")
                    except Exception as e:
                        if self._looks_like_session_dead(e):
                            self._mark_session_dead()

                # ---- 完整下載 (或 stale 快取的背景刷新) ----
                # 【ADR-068】已搶先出圖 (快取或快速段) 而使用者又切到別檔時,這一段
                # 完整歷史 (最耗時的 6+ 段分段下載) 就不必再打了——直接讓路,把
                # _kbars_lock 與 API 配額留給新商品,新商品才能「馬上出圖」。
                already_shown = published_from_cache or (quick_len is not None)
                if already_shown and seq is not None and seq != self._fetch_seq:
                    return
                # 這一路查詢是否已過期 (使用者切走) — 傳給分段下載,可在中途停手。
                _abort_if_superseded = (lambda: seq is not None and seq != self._fetch_seq)
                self.safe_after(0, self.log_message, f"⚡ 極速引擎：透過永豐金 API 抓取 {stock_name} 歷史 K 線...")
                try:
                    # 【第二十輪修正 → ADR-046 改版】單次完整下載優先 (正常商品
                    # 一次就成,維持既有速度);失敗才改「分段下載」補救:90 天
                    # 一段逐段抓,壞段跳過其餘照收。舊法「整段失敗 → 縮短成
                    # 180/90 天」會把還拿得到的早期資料整批放棄 (使用者實例:
                    # MXFR1 載入後沒有之前的歷史)。每段失敗都有例外證據進日誌。
                    # 【ADR-060】主圖也套用「期交所已涵蓋就別下載」(原本只有回測/
                    # 最佳化有做,所以使用者純粹看圖時照樣被流量管制洗版)。
                    # _f is None 代表整段都有期交所資料 → 完全不呼叫券商,
                    # raw 留空,後面 _publish 會用 _extend_with_taifex 把歷史接上。
                    _f, _t, _note = self._taifex_plan_download(
                        contract, self.asset_type, tf, start_dt, end_dt, tag="背景補全")
                    if _note:
                        self.safe_after(0, self.log_message, f"【背景補全】{_note}")
                    if _f is None:
                        raw = pd.DataFrame()
                    else:
                        is_min_tf = tf in ["1分K", "5分K", "15分K", "30分K", "60分K"]
                        if is_min_tf and (_t - _f).days > 5:
                            raw = self._download_kbars_chunked(contract, _f, _t, chunk_days=10, subsplit_days=5, abort_cb=_abort_if_superseded)
                        elif (not is_min_tf) and (_t - _f).days > 90:
                            # 【ADR-069】日/周/月K 的完整段原本是「單一 365~1825 天大請求」,
                            # 會把整段 1 分 K 抓回來 (最慢那步,又整段獨佔 _kbars_lock ~15 秒),
                            # 使用者切走也停不下來,是連點日K還是卡的殘留主因。改走可中止的
                            # 分段下載 (90 天一段):段間釋放鎖並檢查 abort_cb,一切走就停手把
                            # 資源讓給新商品。深歷史仍由 yahoo/期交所延伸補上,不受影響。
                            raw = self._download_kbars_chunked(contract, _f, _t, chunk_days=90, abort_cb=_abort_if_superseded)
                        else:
                            try:
                                raw = self._download_kbars_raw(contract, _f, _t)
                            except Exception as e1:
                                if self._looks_like_session_dead(e1):
                                    raise
                                self.safe_after(0, self.log_message,
                                                f"【背景補全】完整範圍單次下載失敗 ({type(e1).__name__}: {str(e1)[:100]}),改分段下載補救...")
                                raw = self._download_kbars_chunked(contract, _f, _t, chunk_days=90, abort_cb=_abort_if_superseded)
                        if raw is None or raw.empty:
                            raise RuntimeError("完整範圍與分段下載皆無資料 (合約可能剛上市或該期間無交易)")
                except Exception as e:
                    # 【第十一輪修正】兩段式載入時,快速段已出圖、只是完整段失敗
                    # ——訊息要講清楚,不要讓使用者以為整個載入失敗 (TXFR2 實例)。
                    err_detail = f"{type(e).__name__}: {str(e)[:150]}"
                    if quick_len is not None or published_from_cache:
                        self.safe_after(0, self.log_message,
                                        f"【背景補全】完整歷史下載失敗 ({err_detail}),目前先顯示已載入的近期資料;切換週期或重新查詢會再嘗試。")
                    else:
                        self.safe_after(0, self.log_message, f"【提示】無法取得歷史 K 線報價 (shioaji): {err_detail}")
                    if self._looks_like_session_dead(e):
                        self._mark_session_dead()
                    raw = pd.DataFrame()

                # 【ADR-068】使用者在完整下載期間切到別檔:分段下載會提早中止並回傳
                # 部分/空資料。這是「刻意讓路」,不是失敗——安靜結束,不要記誤導的
                # 「下載失敗/查無資料」訊息,也不要拿殘缺資料去污染快取。
                if seq is not None and seq != self._fetch_seq:
                    return

                if raw is None or raw.empty:
                    if not (published_from_cache or quick_len):
                        self.safe_after(0, self.log_message, f"券商 API 查無 {raw_sym} 資料，請確認代碼、連線狀態，或該檔是否已完成合約下載。")
                    return

                if seq is not None and seq != self._fetch_seq:
                    return  # 下載期間使用者已切走,連快取都不必污染? 快取仍可留:資料本身沒錯
                self._kbars_cache_put(cache_key, start_dt, raw, tf)
                df_full = self._resample_sj_df(raw, tf)
                prev = quick_len if quick_len is not None else cached_len
                # 【ADR-141】完整段一律 allow_extend=True，日/周/月K 由各商品
                # 對應的深層資料源補到至少 10 年；分K 不延伸。
                if prev is not None:
                    _publish(df_full, full_ui=False, prev_len=prev, allow_extend=True,
                             note=f"【背景補全】完整歷史已更新 (共 {len(df_full)} 根,總耗時 {time.time()-_t0:.1f} 秒)。")
                else:
                    _publish(df_full, full_ui=True, allow_extend=True)
                    self.safe_after(0, self.log_message, f"⚡ 完整歷史載入完成 (共 {len(df_full)} 根),耗時 {time.time()-_t0:.1f} 秒。")
                return
            else:
                # 美股:自動使用 yfinance
                self.data_source = "yfinance"
                self.safe_after(0, self.log_message, f"正在透過 YFinance 載入 {raw_sym} 歷史數據 (美股)...")
                try:
                    _db_first, _db_last, _db_count = kbars_store.coverage(
                        self.kbars_db_file, raw_sym, self.asset_type, tf)
                    if _db_count and _db_last:
                        _inc_start = (pd.Timestamp(_db_last) - pd.Timedelta(days=7)).date()
                        df = yf.Ticker(raw_sym).history(
                            start=str(_inc_start), interval=interval,
                            auto_adjust=adjust_flag)
                    else:
                        df = yf.Ticker(raw_sym).history(
                            period=period, interval=interval, auto_adjust=adjust_flag)
                    if not df.empty and df.index.tz is not None:
                        df.index = df.index.tz_convert('Asia/Taipei').tz_localize(None)
                except Exception:
                    pass

                if df.empty:
                    self.safe_after(0, self.log_message, f"YFinance 查無 {raw_sym} 資料，請確認美股代碼是否正確。")
                    return

                search_sym = raw_sym
                try: stock_name = yf.Ticker(search_sym).info.get('shortName', '')
                except Exception: pass
                _publish(df, full_ui=True)
        except Exception as e: self.safe_after(0, self.log_message, f"數據處理異常: {e}")

    def _apply_chart_margins(self, fig, axlist, panel_ratios):
        """
        【第五輪修正:白邊真正的解法】mplfinance 面板是 fig.add_axes([固定矩形])
        建立的,fig.subplots_adjust() 動不了它們,必須對每個面板軸域直接
        set_position() 才能真正搬動。這裡依 self.chart_layout 的邊界比例
        (margin_left/right/top/bottom + hspace) 與各面板高度比例 panel_ratios,
        由上到下重新計算每個面板的矩形位置並套用。

        axlist 的排列是 [面板0主軸, 面板0孿生軸, 面板1主軸, 面板1孿生軸, ...]
        (mplfinance 每個面板都配一個 secondary_y 的 twinx),所以第 i 個面板
        對應 axlist[2i] 與 axlist[2i+1],兩個都要搬到同一個矩形。

        hspace 採「每個面板間隙 = hspace × 平均面板高度」的近似,貼近
        matplotlib subplots_adjust 的 hspace 語意,讓 0.02~0.35 的滑桿範圍
        對應到合理的面板間距。
        """
        try:
            layout = self.chart_layout
            left = float(layout['margin_left']); right = float(layout['margin_right'])
            top = float(layout['margin_top']); bottom = float(layout['margin_bottom'])
            hspace = float(layout['hspace'])
            n = len(panel_ratios)
            if n <= 0 or not axlist:
                return
            avail_w = max(right - left, 0.05)
            span_h = max(top - bottom, 0.05)
            total_ratio = float(sum(panel_ratios)) or 1.0
            gap = hspace * (span_h / n)
            drawable_h = span_h - gap * (n - 1)
            if drawable_h <= 0.02:  # 間距過大會把面板壓成負高度,退回無間距
                gap = 0.0
                drawable_h = span_h
            unit = drawable_h / total_ratio
            y_cursor = top
            for i, ratio in enumerate(panel_ratios):
                h = unit * ratio
                rect = [left, y_cursor - h, avail_w, h]
                for j in (2 * i, 2 * i + 1):
                    if j < len(axlist) and axlist[j] is not None:
                        try:
                            axlist[j].set_position(rect)
                        except Exception:
                            pass
                y_cursor = y_cursor - h - gap
        except Exception as e:
            self.log_message(f"【版面套用異常】{e}")

    def draw_chart(self, raw_df):
        try:
            _draw_total_t0 = time.perf_counter()
            _phase_t0 = _draw_total_t0
            df = self.calculate_custom_indicators(raw_df)
            _indicator_ms = (time.perf_counter() - _phase_t0) * 1000.0
            for i in range(6):
                col = f"MA_CUSTOM_{i}"
                if col in df.columns:
                    trend_col = f"{col}_TREND"
                    diff = df[col].diff()
                    df[trend_col] = '→'
                    df.loc[diff > 0, trend_col] = '↗'
                    df.loc[diff < 0, trend_col] = '↘'
                    df.loc[df[col].isna() | diff.isna(), trend_col] = ''
            self.full_calculated_df = df
            txt_fmt_char = self.timeframe_var.get()
            # 【ADR-082/140】限制畫布一次渲染的 K 棒數量上限，再依
            # 畫布實際像素寬度自適應。完整 df 仍用來算指標，不用畫面
            # 切片回算，所以 MA240 等長週期指標不會有 warm-up 誤差。
            # 原本傳 9399+ 根K棒到 matplotlib, 單次渲染需 1657 ms (1.65秒/幀);
            # 限制渲染根數至 1200 根後, 單次渲染降至 ~80 ms, 縮放與平移流暢度提升 15+ 倍!
            try:
                _chart_px = self.chart_frame.winfo_width()
            except Exception:
                _chart_px = None
            self.plot_df, max_bars = chart_viewport.tail_window(
                df, txt_fmt_char, _chart_px)
            df = self.plot_df 
            
            if getattr(self, 'current_canvas', None) is not None:
                try: self.current_canvas.get_tk_widget().destroy()
                except: pass
                self.current_canvas = None

            if getattr(self, 'current_fig', None) is not None:
                plt.close(self.current_fig)
                self.current_fig = None

            for widget in self.chart_frame.winfo_children(): widget.destroy()
            plt.close('all')
            # 【效能】gc.collect() 很貴 (大 heap 可達數十~上百 ms),而 draw_chart 在
            # 「切商品/背景補全/縮放平移改版面」都會跑到,每次都 full GC 是切換與
            # 重繪變慢的主因之一。plt.close(fig) 已釋放 matplotlib 圖物件;改成每
            # 隔幾次才 collect 一次,把記憶體回收挪出熱路徑,又不至於長期堆積。
            self._draw_count = getattr(self, '_draw_count', 0) + 1
            if self._draw_count % 12 == 0:
                gc.collect()
            
            apds = []
            panel_ratios = [5, 1.2] 
            current_panel = 2
            active_panels = {'Volume': 1}

            for i in range(6):
                col_name = f"MA_CUSTOM_{i}"
                if self.ma_shows[i].get() and col_name in df.columns:
                    c_hex = palette.resolve(self.ma_colors[i].get(), "#FFFFFF")
                    apds.append(mpf.make_addplot(df[col_name], panel=0, color=c_hex, width=1.2, secondary_y=False))

            # 【ADR-131】兩組布林共用同一段畫法,只差前綴/開關/顏色 ——
            # 抄第二份的話,日後改線寬/線型只改到一組 (P-67)。
            def _add_bb(prefix, show_var, color_var, default_hex):
                if not (show_var.get() and f'{prefix}_UPPER' in df.columns):
                    return
                _hex = palette.resolve(color_var.get(), default_hex)
                apds.append(mpf.make_addplot(df[f'{prefix}_UPPER'], panel=0, color=_hex, linestyle='--', width=1.0, secondary_y=False))
                apds.append(mpf.make_addplot(df[f'{prefix}_MID'], panel=0, color=_hex, linestyle='-', width=1.0, secondary_y=False))
                apds.append(mpf.make_addplot(df[f'{prefix}_LOWER'], panel=0, color=_hex, linestyle='--', width=1.0, secondary_y=False))

            # 第2組先畫、第1組後畫:重疊時第1組 (使用者的主要設定) 蓋在上面。
            # 每組內部:σa 虛線、σb 點線 (【第九輪 圖3需求】的區隔方式沿用)。
            _add_bb('BB2', self.bb2_show, self.bb2_color, "#E040FB")
            _add_bb('BB', self.bb_show, self.bb_color, "#00E5FF")

            if self.var_macd.get() and 'MACD' in df.columns:
                macd_color = ['#FF1744' if v > 0 else '#00E676' for v in df['Hist']]
                apds.append(mpf.make_addplot(df['MACD'], panel=current_panel, color='#FF1744', secondary_y=False))
                apds.append(mpf.make_addplot(df['Signal'], panel=current_panel, color='#29B6F6', secondary_y=False))
                apds.append(mpf.make_addplot(df['Hist'], type='bar', panel=current_panel, color=macd_color, secondary_y=False, ylabel='MACD'))
                active_panels['MACD'] = current_panel; panel_ratios.append(1.2); current_panel += 1

            if self.var_rsi.get() and 'RSI' in df.columns:
                apds.append(mpf.make_addplot(df['RSI'], panel=current_panel, color='#FFCA28', ylabel='RSI'))
                apds.append(mpf.make_addplot([80]*len(df), panel=current_panel, color='#333333', linestyle='--'))
                apds.append(mpf.make_addplot([20]*len(df), panel=current_panel, color='#333333', linestyle='--'))
                active_panels['RSI'] = current_panel; panel_ratios.append(1.2); current_panel += 1

            if self.var_kdj.get() and 'J' in df.columns:
                apds.append(mpf.make_addplot(df['K'], panel=current_panel, color='#FFFFFF', ylabel='KDJ'))
                apds.append(mpf.make_addplot(df['D'], panel=current_panel, color='#FFCA28'))
                apds.append(mpf.make_addplot(df['J'], panel=current_panel, color='#E040FB'))
                # 【ADR-134】超買/超賣參考線。隔壁 RSI 副圖一直有 80/20 兩條虛線,
                # KDJ 沒有 —— 那是兩個面板之間唯一客觀的落差,補上。
                for _lvl_var in (self.kd_ob, self.kd_os):
                    try:
                        _lvl = float(_lvl_var.get())
                    except (TypeError, ValueError):
                        continue
                    apds.append(mpf.make_addplot([_lvl] * len(df), panel=current_panel,
                                                 color='#333333', linestyle='--'))
                active_panels['KDJ'] = current_panel; panel_ratios.append(1.2); current_panel += 1

            # 【ADR-134】JAE:A(RSI) / J(KDJ的J) / E(長期趨勢線) 三條 + 中性基準線
            if self.var_jae.get() and jae.COL_A in df.columns:
                apds.append(mpf.make_addplot(df[jae.COL_A], panel=current_panel,
                                             color='#FFCA28', ylabel='JAE'))
                apds.append(mpf.make_addplot(df[jae.COL_J], panel=current_panel, color='#E040FB'))
                apds.append(mpf.make_addplot(df[jae.COL_E], panel=current_panel,
                                             color='#00E5FF', width=1.6))
                _mid = self._jae_params_now()['midline']
                apds.append(mpf.make_addplot([_mid] * len(df), panel=current_panel,
                                             color='#333333', linestyle='--'))
                active_panels['JAE'] = current_panel; panel_ratios.append(1.2); current_panel += 1

            if self.var_dmi.get() and 'ADX' in df.columns:
                apds.append(mpf.make_addplot(df['+DI'], panel=current_panel, color='#FF1744', ylabel='DMI'))
                apds.append(mpf.make_addplot(df['-DI'], panel=current_panel, color='#00E676'))
                apds.append(mpf.make_addplot(df['ADX'], panel=current_panel, color='#29B6F6', width=1.5))
                active_panels['DMI'] = current_panel; panel_ratios.append(1.2); current_panel += 1

            if self.var_bbw.get() and 'BB_WIDTH' in df.columns:
                apds.append(mpf.make_addplot(df['BB_WIDTH'], panel=current_panel, color='#FF9100', ylabel='BB Width'))
                active_panels['BBW'] = current_panel; panel_ratios.append(1.0); current_panel += 1

            # 【ADR-011】法人/資券副圖已移除:資料來源 FinMind 已停用。

            txt_fmt_char = self.timeframe_var.get()
            dt_fmt = '%Y-%m-%d %H:%M' if "分" in txt_fmt_char else '%Y-%m-%d'

            # 【使用者調整#1】原本 figsize 是寫死的 (11, 8) 英吋,但 chart_frame 這個
            # tkinter 容器實際可用的像素空間通常比這個大很多 (尤其視窗變寬/最大化時)；
            # matplotlib 的 Figure 畫完之後不會自動跟著容器一起變大，只有 Tk 的 widget
            # 容器本身會撐開，圖表內容還是維持原本 figsize 換算出來的像素大小，
            # 於是圖表左側 (或周圍) 就會看到一塊沒有用到的空白。改成依 chart_frame
            # 目前實際的寬高 (winfo_width/height) 換算英吋，讓圖表確實填滿可用空間。
            self.chart_frame.update_idletasks()
            dpi = 100
            frame_w = self.chart_frame.winfo_width()
            frame_h = self.chart_frame.winfo_height()
            # 【ADR-036】記錄這次實際繪製時的容器尺寸;_debounced_resize_redraw
            # 會用它比對「尺寸有沒有真的變」,沒變就跳過重繪,避免資源浪費。
            self._last_drawn_chart_size = (frame_w, frame_h)
            # 視窗剛啟動、尚未完全繪製時 winfo_width()/height() 可能回傳 1 這種極小值，
            # 這時候退回一個合理的預設尺寸，避免算出畸形的 figsize。
            fig_w = (frame_w / dpi) if frame_w > 100 else 11
            fig_h = (frame_h / dpi) if frame_h > 100 else 8

            _phase_t0 = time.perf_counter()
            fig, axlist = mpf.plot(
                df, type='candle', volume=True, style=xq_style, returnfig=True, 
                figsize=(fig_w, fig_h), tight_layout=False, addplot=apds if apds else None, 
                panel_ratios=panel_ratios, datetime_format=dt_fmt,
                # 【第十六輪 第8項】365天1分K可達十萬點,超過 mplfinance 預設門檻
                # 會在主控台印出大段 WARNING (使用者關閉程式後才看到,誤以為錯誤)。
                # 提高門檻靜音;實際顯示效能由既有的視窗範圍縮放機制處理。
                warn_too_much_data=2000000,
                # 【使用者調整#2】xrotation 從 mplfinance 預設的 45 度改成 0 度 (水平顯示)，
                # 旋轉角度越大文字佔用的垂直高度越多；日期標籤本來就間隔得夠開，水平顯示
                # 不會互相重疊，卻能明顯縮小底部日期區塊佔用的版面。
                xrotation=0
            )
            _mpf_ms = (time.perf_counter() - _phase_t0) * 1000.0
            # 【第五輪修正:找到白邊真正的根因】前四輪都用 figsize / tight_layout /
            # subplots_adjust 想消除圖表四周留白,全都沒效——根本原因是 mplfinance
            # 的每個面板是用 fig.add_axes([固定矩形]) 建立的,而 fig.subplots_adjust()
            # 只會影響「用 subplot/GridSpec 建立」的軸域,對 add_axes 的固定位置軸域
            # 完全無效 (等於白呼叫)。這就是為什麼不管邊界比例怎麼調,面板位置都
            # 一動也不動。這次改成用 self._apply_chart_margins() 對每個面板軸域
            # 直接呼叫 set_position() 重新定位——這是唯一能真正搬動 mplfinance
            # 面板的方式。panel_ratios 存到 self,讓「版面微調」對話框的即時預覽
            # 也能用同一套邏輯重新定位、瞬間反映。
            self.axlist = axlist
            self.current_panel_ratios = list(panel_ratios)
            self._apply_chart_margins(fig, axlist, self.current_panel_ratios)
            self.active_panels = active_panels
            # 【使用者第三次反映#3】各副圖對應的欄位清單,供 auto_scale_indicator_panels()
            # 依「目前可見範圍」重新計算 Y 軸使用。
            self.panel_columns = {
                'Volume': ['Volume'],
                'MACD': ['MACD', 'Signal', 'Hist'],
                'RSI': ['RSI'],
                'KDJ': ['K', 'D', 'J'],
                'DMI': ['+DI', '-DI', 'ADX'],
                'BBW': ['BB_WIDTH'],
                'JAE': [jae.COL_A, jae.COL_J, jae.COL_E],
            }
            
            adj_str = "(還原)" if self.var_adjusted.get() and self.asset_type == "stock" else ""
            display_title = f"{self.current_symbol} {self.current_stock_name} {adj_str}".strip()
            axlist[0].set_title(f" {display_title} 旗艦操盤圖", color='#29B6F6', fontsize=10, loc='left')

            if self.saved_xlim is not None:
                try: 
                    axlist[0].set_xlim(self.saved_xlim)
                    self.auto_scale_y(axlist[0], self.saved_xlim[0], self.saved_xlim[1])
                    self.auto_scale_indicator_panels(self.saved_xlim[0], self.saved_xlim[1])
                except: pass
            else:
                n_c = {"1分K": 120, "5分K": 120, "15分K": 120, "30分K": 120, "60分K": 120, "日K": 120, "周K": 104, "月K": 60}.get(txt_fmt_char, 120)
                tot = len(df)
                if tot > n_c:
                    x_min, x_max = tot - n_c, tot
                else:
                    x_min, x_max = 0, max(1, tot)
                axlist[0].set_xlim(x_min, x_max)
                self.auto_scale_y(axlist[0], x_min, x_max)
                self.auto_scale_indicator_panels(x_min, x_max)

            # 【使用者調整#9】主圖 MA/BB 的 hover 文字，改成每個指標各自獨立的
            # text 物件、顏色跟隨該指標在圖上設定的線條顏色 (例如 SMA20 設藍色，
            # 文字也顯示藍色)，不再是統一寫死的黃色一大段字串。
            # 【ADR-025 blitting】animated=True:這些 hover 文字不參與一般重繪 (不烙進底圖),
            # 只由 _blit_hover() 在滑鼠移動時用 blit 快速疊加,徹底解決 hover 卡頓。
            main_text_props = dict(fontsize=9, weight='bold', verticalalignment='top', zorder=10000, clip_on=False, animated=True,
                                    bbox=dict(facecolor='#12161A', alpha=0.7, edgecolor='none', pad=2))
            self.txt_main_segments = []
            main_x = 0.01
            for i in range(6):
                col = f"MA_CUSTOM_{i}"
                if self.ma_shows[i].get() and col in df.columns:
                    c_hex = palette.resolve(self.ma_colors[i].get(), "#FFFFFF")
                    label_prefix = f"{self.ma_types[i].get()}{self.ma_periods[i].get()}"
                    obj = axlist[0].text(main_x, 0.97, '', transform=axlist[0].transAxes, color=c_hex, **main_text_props)

                    def _mk_ma_fmt(col=col, label_prefix=label_prefix):
                        trend_col = f"{col}_TREND"
                        def _fmt(row):
                            if col in row and not np.isnan(row[col]):
                                trend = row.get(trend_col, '')
                                return f"{label_prefix}: {row[col]:.2f} {trend}".strip()
                            return None
                        return _fmt
                    self.txt_main_segments.append({'obj': obj, 'fmt': _mk_ma_fmt()})
                    main_x += 0.095
            # 【ADR-131】兩組布林各一段十字線提示 (同一個工廠函式,不抄第二份)。
            def _add_bb_readout(prefix, show_var, color_var, default_hex, label):
                nonlocal main_x
                if not (show_var.get() and f'{prefix}_UPPER' in df.columns):
                    return
                _hex = palette.resolve(color_var.get(), default_hex)
                obj = axlist[0].text(main_x, 0.97, '', transform=axlist[0].transAxes,
                                     color=_hex, **main_text_props)

                def _fmt(row, _p=prefix, _l=label):
                    if f'{_p}_UPPER' in row and not np.isnan(row[f'{_p}_UPPER']):
                        return (f"{_l}上:{row[f'{_p}_UPPER']:.2f} 中:{row[f'{_p}_MID']:.2f}"
                                f" 下:{row[f'{_p}_LOWER']:.2f}")
                    return None
                self.txt_main_segments.append({'obj': obj, 'fmt': _fmt})
                main_x += 0.20

            _add_bb_readout('BB', self.bb_show, self.bb_color, "#00E5FF", 'BB')
            _add_bb_readout('BB2', self.bb2_show, self.bb2_color, "#E040FB", 'BB2')

            # 【使用者調整#8】副圖 (MACD/RSI/KDJ/DMI/布林寬度) 的 hover 文字，
            # 同樣改成每個數值各自獨立的 text 物件，顏色跟隨該數值在副圖裡的
            # 線條顏色。Hist 是長條圖、正負值顏色會變 (紅漲綠跌)，用
            # dynamic_color_key 標記，在 on_mouse_move 裡依當下數值正負動態改色，
            # 而不是固定一種顏色。
            sub_text_props = dict(fontsize=9, weight='bold', verticalalignment='top', zorder=10000, clip_on=False, animated=True,
                                   bbox=dict(facecolor='#12161A', alpha=0.7, edgecolor='none', pad=1))
            self.sub_texts = {}
            for name, p_idx in active_panels.items():
                ax_idx = p_idx * 2
                if ax_idx >= len(axlist): continue
                ax = axlist[ax_idx]
                segs = []
                if name == 'MACD':
                    o = ax.text(0.01, 0.90, '', transform=ax.transAxes, color='#FF1744', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"MACD: {row['MACD']:.2f}" if 'MACD' in row and not np.isnan(row['MACD']) else None)})
                    o = ax.text(0.15, 0.90, '', transform=ax.transAxes, color='#29B6F6', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"Signal: {row['Signal']:.2f}" if 'Signal' in row and not np.isnan(row['Signal']) else None)})
                    o = ax.text(0.30, 0.90, '', transform=ax.transAxes, color='#FF1744', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"Hist: {row['Hist']:.2f}" if 'Hist' in row and not np.isnan(row['Hist']) else None), 'dynamic_color_key': 'Hist'})
                elif name == 'RSI':
                    o = ax.text(0.01, 0.90, '', transform=ax.transAxes, color='#FFCA28', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"RSI: {row['RSI']:.2f}" if 'RSI' in row and not np.isnan(row['RSI']) else None)})
                elif name == 'KDJ':
                    o = ax.text(0.01, 0.90, '', transform=ax.transAxes, color='#FFFFFF', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"K: {row['K']:.2f}" if 'K' in row and not np.isnan(row['K']) else None)})
                    o = ax.text(0.13, 0.90, '', transform=ax.transAxes, color='#FFCA28', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"D: {row['D']:.2f}" if 'D' in row and not np.isnan(row['D']) else None)})
                    o = ax.text(0.25, 0.90, '', transform=ax.transAxes, color='#E040FB', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"J: {row['J']:.2f}" if 'J' in row and not np.isnan(row['J']) else None)})
                elif name == 'JAE':
                    # 【ADR-134】三條線各一段,顏色跟線一致 (A黃 / J紫 / E青)
                    for _x, _col, _c, _nm in ((0.01, jae.COL_A, '#FFCA28', 'A'),
                                              (0.11, jae.COL_J, '#E040FB', 'J'),
                                              (0.21, jae.COL_E, '#00E5FF', 'E')):
                        o = ax.text(_x, 0.90, '', transform=ax.transAxes, color=_c, **sub_text_props)
                        segs.append({'obj': o,
                                     'fmt': (lambda row, _cc=_col, _n=_nm:
                                             (f"{_n}: {row[_cc]:.2f}"
                                              if _cc in row and not np.isnan(row[_cc]) else None))})
                elif name == 'DMI':
                    o = ax.text(0.01, 0.90, '', transform=ax.transAxes, color='#FF1744', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"+DI: {row['+DI']:.2f}" if '+DI' in row and not np.isnan(row['+DI']) else None)})
                    o = ax.text(0.15, 0.90, '', transform=ax.transAxes, color='#00E676', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"-DI: {row['-DI']:.2f}" if '-DI' in row and not np.isnan(row['-DI']) else None)})
                    o = ax.text(0.29, 0.90, '', transform=ax.transAxes, color='#29B6F6', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"ADX: {row['ADX']:.2f}" if 'ADX' in row and not np.isnan(row['ADX']) else None)})
                elif name == 'BBW':
                    o = ax.text(0.01, 0.90, '', transform=ax.transAxes, color='#FF9100', **sub_text_props)
                    segs.append({'obj': o, 'fmt': lambda row: (f"BB Width: {row['BB_WIDTH']:.2f}%" if 'BB_WIDTH' in row and not np.isnan(row['BB_WIDTH']) else None)})
                self.sub_texts[name] = segs

            self.vlines = [ax.axvline(x=0, color='white', linestyle='--', linewidth=0.8, alpha=0.6, visible=False, zorder=50, animated=True) for ax in axlist[::2]]
            # 【第十六輪 第3項】水平虛線:只畫在主圖 (axlist[0]),y 對準游標所在
            # K棒的「收盤價」(不是滑鼠像素位置),與垂直線構成十字準星。
            self.hline_main = axlist[0].axhline(y=0, color='white', linestyle='--', linewidth=0.8, alpha=0.6, visible=False, zorder=50, animated=True)
            self._live_bar_reset_artists()  # 【ADR-041】活K棒 artists 跟著新 axes 重建
            # 【ADR-102】量價支撐壓力:在主圖畫水平線。放在這裡是因為 axlist 剛
            # 建好、還沒交給 canvas,與其他 overlay (vlines/hline) 同一階段。
            self._draw_sr_levels(axlist[0], raw_df)
            # 【ADR-133】黃金切割律:同一階段畫,同一份 raw_df,不用多打一次 API。
            self._draw_fib_levels(axlist[0], raw_df)
            # 【ADR-120】盤勢/型態偵測:不畫在圖上,只把「新出現的」寫進系統
            # 日誌。放在畫圖之後、用同一份 raw_df,不用多打一次 API。
            self._regime_check(raw_df)

            self.current_fig = fig
            self.current_canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
            canvas_widget = self.current_canvas.get_tk_widget()
            # 【第五輪修正】原本這裡有一段 canvas_widget.config(width=frame_w+delta,
            # height=frame_h+delta) 想強制畫布像素尺寸，但它完全無效:緊接著的
            # pack(fill=tk.BOTH, expand=True) 會強制 widget 撐滿容器、忽略 config
            # 指定的尺寸。既然 pack(fill=BOTH, expand=True) 本來就會讓畫布自動填滿
            # chart_frame (這正是我們要的「填滿可用空間」)，那段 config 不只沒用、
            # 還讓「寬度/高度微調」滑桿看起來像壞掉。直接移除,只留 pack 讓畫布
            # 自然填滿容器;圖表四周的留白改由 subplots_adjust 的邊界比例控制
            # (使用者可透過「📐 版面微調」對話框即時調整並儲存)。
            canvas_widget.pack(fill=tk.BOTH, expand=True)
            # 【ADR-025 blitting】每次「真正的重繪」(初次/縮放/平移/改版面) 完成後,
            # draw_event 會把「不含十字線與hover文字的底圖」快取起來;滑鼠移動時
            # 只做「還原底圖+畫十字線/文字+blit」(毫秒級),不再整張圖重畫。
            self._hover_bg = None
            self.current_canvas.mpl_connect('draw_event', self._on_canvas_draw)
            _draw_t0 = time.perf_counter()
            self.current_canvas.draw()
            _canvas_ms = (time.perf_counter() - _draw_t0) * 1000.0
            _total_ms = (time.perf_counter() - _draw_total_t0) * 1000.0

            self.current_canvas.mpl_connect('scroll_event', self.on_scroll_zoom)
            self.current_canvas.mpl_connect('button_press_event', self.on_mouse_press)
            self.current_canvas.mpl_connect('button_release_event', self.on_mouse_release)
            self.current_canvas.mpl_connect('motion_notify_event', self.on_mouse_move)
            # 【ADR-140】過去只量 canvas.draw，卻把日誌稱為 mpf.plot +
            # canvas.draw，會誤判 C++ 是否有幫助。現在分開指標、建圖、
            # raster draw 與總時間；只在總時間偏高時記錄，不洗版。
            if _total_ms > 350:
                self.log_message(
                    f"[{txt_fmt_char}] 載入成功 ({display_title}) — 繪圖 {_total_ms:.0f} ms "
                    f"(指標 {_indicator_ms:.0f} / 建圖 {_mpf_ms:.0f} / 畫布 {_canvas_ms:.0f} ms; "
                    f"{len(df)}/{len(raw_df)} 根K棒，上限 {max_bars})")
            else:
                self.log_message(f"[{txt_fmt_char}] 載入成功 ({display_title})")
        except Exception as e: self.log_message(f"畫布繪製失敗: {e}")

    def on_scroll_zoom(self, event):
        if event.inaxes is None or not self.axlist: return
        self._view_is_auto_default = False  # 使用者手動縮放,標記為非預設視角
        ax = self.axlist[0]; xmin, xmax = ax.get_xlim(); range_x = xmax - xmin
        factor = 0.85 if event.button == 'up' else 1.15; new_range = range_x * factor
        max_range = len(self.plot_df) + 20
        if new_range > max_range: new_range = max_range
        if new_range < 15: new_range = 15
        rel_x = (event.xdata - xmin) / range_x

        new_xmin = event.xdata - new_range * rel_x
        new_xmax = event.xdata + new_range * (1 - rel_x)
        ax.set_xlim(new_xmin, new_xmax)

        self.auto_scale_y(ax, new_xmin, new_xmax)
        self.auto_scale_indicator_panels(new_xmin, new_xmax)
        # 【效能】標記「縮放進行中」→ _on_canvas_draw 這段期間不做全圖 hover 底圖
        # 快取 (連續滾動時純浪費);停止滾動 200ms 後再補一次快取,hover 才能用 blit。
        self._zoom_active = True
        if getattr(self, '_zoom_end_id', None) is not None:
            try: self.after_cancel(self._zoom_end_id)
            except Exception: pass
        self._zoom_end_id = self.safe_after(200, self._on_zoom_settled)
        event.canvas.draw_idle()

    def _on_zoom_settled(self):
        """縮放停止後呼叫:清旗標並補一次完整重繪,讓 hover 底圖重新快取。"""
        self._zoom_end_id = None
        self._zoom_active = False
        try:
            if self.current_canvas is not None:
                self.current_canvas.draw_idle()
        except Exception:
            pass

    def _on_canvas_draw(self, event=None):
        """
        【ADR-025 blitting】任何一次「真正的重繪」完成後 (初次繪製、縮放、平移、
        版面微調...),把不含 hover 物件的底圖像素快取起來。hover 物件全部
        animated=True,一般重繪不會把它們烙進底圖。
        """
        try:
            cv = self.current_canvas
            fig = self.current_fig
            if cv is None or fig is None:
                return
            if event is not None and getattr(event, 'canvas', cv) is not cv:
                return  # 舊 canvas 的殘留事件,忽略
            # 【效能】縮放/平移進行中時,hover 底圖每一幀都被作廢 (視野一直變),
            # 這時候還 copy_from_bbox(整張圖) 純粹是白花錢 (一次全圖點陣複製)。
            # 互動中先跳過快取,等互動結束 (放開滑鼠/停止滾動) 再補一次即可,
            # 讓平移/縮放的每一幀省下一次全圖複製。
            if getattr(self, 'is_panning', False) or getattr(self, '_zoom_active', False):
                self._hover_bg = None
                self._hover_bg_view = None
                return
            self._hover_bg = cv.copy_from_bbox(fig.bbox)
            # 【ADR-044 第4項】記錄快取當下的視野:移動/縮放後視野改變,舊背景
            # 快取上疊畫新座標的活K棒/十字線會產生「殘影」;blit 前比對即可偵測。
            try:
                self._hover_bg_view = (self.axlist[0].get_xlim(), self.axlist[0].get_ylim()) if self.axlist else None
            except Exception:
                self._hover_bg_view = None
        except Exception:
            self._hover_bg = None

    def _blit_hover(self):
        """
        還原底圖 → 疊畫目前可見的十字線與 hover 文字 → blit 上屏。
        成功回傳 True (毫秒級);任何環節不支援/失敗回傳 False,呼叫端退回 draw_idle。
        """
        try:
            cv = self.current_canvas
            fig = self.current_fig
            bg = getattr(self, '_hover_bg', None)
            # 【ADR-044 第4項】視野已改變 (平移/縮放) → 舊背景作廢,請求完整重繪
            # 重新快取,本次不 blit —— 徹底消除殘影。
            try:
                if bg is not None and self.axlist:
                    cur_view = (self.axlist[0].get_xlim(), self.axlist[0].get_ylim())
                    saved_view = getattr(self, '_hover_bg_view', None)
                    if saved_view is None or cur_view != saved_view:
                        self._hover_bg = None
                        bg = None
                        self.current_canvas.draw_idle()  # 排程完整重繪,draw 事件會重新快取背景
            except Exception:
                pass
            if cv is None or fig is None or bg is None:
                return False
            cv.restore_region(bg)
            for line in self.vlines:
                if line.get_visible() and line.axes is not None:
                    line.axes.draw_artist(line)
            hl = getattr(self, 'hline_main', None)
            if hl is not None and hl.get_visible() and hl.axes is not None:
                hl.axes.draw_artist(hl)
            # 【ADR-041】活K棒 (形成中K棒 + 即時價線) 一併疊畫,與 hover 共用同一次 blit
            for art in (getattr(self, '_live_bar_artists', None) or ()):
                if art.get_visible() and art.axes is not None:
                    art.axes.draw_artist(art)
            for seg in self.txt_main_segments:
                o = seg['obj']
                if o.get_text() and o.axes is not None:
                    o.axes.draw_artist(o)
            for segs in self.sub_texts.values():
                for seg in segs:
                    o = seg['obj']
                    if o.get_text() and o.axes is not None:
                        o.axes.draw_artist(o)
            cv.blit(fig.bbox)
            return True
        except Exception:
            return False

    def on_mouse_press(self, event):
        if event.button == 1 and event.inaxes: 
            self.is_panning = True; self.press_x_pixel = event.x; self.pan_axes = event.inaxes; self.press_xlim = event.inaxes.get_xlim()

    def on_mouse_release(self, event):
        if event.button == 1:
            was_panning = self.is_panning
            self.is_panning = False; self.press_x_pixel = None
            # 【ADR-025】平移過程有節流,放開時補一次最終重繪,確保停在精確位置。
            if was_panning and self.current_canvas is not None:
                try:
                    self.current_canvas.draw_idle()
                except Exception:
                    pass

    def on_mouse_move(self, event):
        if self.is_panning and self.press_x_pixel is not None and event.inaxes == self.pan_axes:
            self._view_is_auto_default = False  # 使用者手動平移,標記為非預設視角
            ax = self.pan_axes; dx_pixel = event.x - self.press_x_pixel
            bbox = ax.get_window_extent(); xmin, xmax = self.press_xlim
            dx_data = (dx_pixel / bbox.width) * (xmax - xmin)
            
            new_xmin = xmin - dx_data
            new_xmax = xmax - dx_data
            ax.set_xlim(new_xmin, new_xmax)
            
            # 【ADR-025 節流】平移是「真重繪」(xlim 變了,blit 幫不上忙),但滑鼠事件
            # 每移動一像素就來一發,全處理會塞爆主執行緒。位移是用「按下當時」的
            # 基準絕對計算的,跳過中間幾幀完全不影響最終位置——每 30ms 處理一幀
            # 即可,放開滑鼠時 on_mouse_release 會補最終一繪。
            now = time.monotonic()
            if now - getattr(self, '_last_pan_draw', 0.0) < 0.03:
                return
            self._last_pan_draw = now
            # 【使用者第三次反映#3】各面板共用 x 軸,拖任一面板都要重算所有面板 Y 軸。
            self.auto_scale_y(self.axlist[0], new_xmin, new_xmax)
            self.auto_scale_indicator_panels(new_xmin, new_xmax)
            event.canvas.draw_idle()
            return 
            
        if event.inaxes is None or event.xdata is None:
            if self.last_hover_idx != -1:
                for line in self.vlines: line.set_visible(False)
                if getattr(self, 'hline_main', None) is not None: self.hline_main.set_visible(False)
                for seg in self.txt_main_segments: seg['obj'].set_text("")
                for segs in self.sub_texts.values():
                    for seg in segs: seg['obj'].set_text("")
                # 【ADR-025】離開圖表:還原底圖即可,不需整張重畫。
                if not self._blit_hover():
                    event.canvas.draw_idle()
                self.last_hover_idx = -1
            return
            
        try:
            idx = int(round(event.xdata))
            if 0 <= idx < len(self.plot_df):
                # 【ADR-025 核心修正:hover 卡頓根因】原本每換一根K棒就 draw_idle()
                # ——那是「整張圖」重繪 (幾百根K棒+量能+副圖),滑鼠掃過去等於連續
                # 幾十次完整重繪,主執行緒被塞爆,整個視窗連滑鼠都卡。改成 blit:
                # 還原快取底圖+只畫十字線與文字+上屏,一次毫秒級,絲滑跟手。
                # 另外資訊列字串原本在「同一根K棒內」每個像素都重組一次,一併
                # 收進「換K棒才更新」的判斷裡。
                if idx != self.last_hover_idx:
                    row = self.plot_df.iloc[idx]
                    for line in self.vlines: line.set_xdata([idx, idx]); line.set_visible(True)
                    if getattr(self, 'hline_main', None) is not None:
                        self.hline_main.set_ydata([row['Close'], row['Close']])
                        self.hline_main.set_visible(True)

                    # 【使用者調整#8/#9】各指標文字獨立更新;Hist 依正負動態紅綠。
                    for seg in self.txt_main_segments:
                        txt = seg['fmt'](row)
                        seg['obj'].set_text(txt if txt else "")
                    for segs in self.sub_texts.values():
                        for seg in segs:
                            txt = seg['fmt'](row)
                            seg['obj'].set_text(txt if txt else "")
                            if seg.get('dynamic_color_key') == 'Hist' and 'Hist' in row and not np.isnan(row['Hist']):
                                seg['obj'].set_color('#FF1744' if row['Hist'] > 0 else '#00E676')

                    if not self._blit_hover():
                        event.canvas.draw_idle()  # blit 不可用時的降級路徑
                    self.last_hover_idx = idx

                    tf = self.timeframe_var.get()
                    date_str = self.plot_df.index[idx].strftime('%Y-%m-%d %H:%M') if "分" in tf else self.plot_df.index[idx].strftime('%Y-%m-%d')
                    
                    prev_c = self.plot_df['Close'].iloc[idx-1] if idx > 0 else row['Open']
                    if idx == len(self.plot_df) - 1 and getattr(self, 'current_reference_price', 0) > 0:
                        prev_c = self.current_reference_price
                    chg_val = row['Close'] - prev_c  # 【第十一輪 第1項】漲跌點數
                    chg_pct = (row['Close'] - prev_c) / prev_c * 100
                    chg_sign = "▲" if chg_pct > 0 else ("▼" if chg_pct < 0 else "-")
                    
                    raw_vol = float(row['Volume'])
                    if self.asset_type == "future": vol_str = f"{raw_vol:,.0f} 口"
                    elif self.asset_type == "us_stock": vol_str = f"{raw_vol:,.0f} 股"
                    elif self.asset_type == "stock": vol_str = f"{raw_vol:,.0f} 張"
                    elif self.asset_type == "index_tw": vol_str = f"{_fmt_amt(raw_vol)} 億"
                    else: vol_str = f"{raw_vol:,.0f}"  
                    
                    display_name = f"{self.current_symbol} {self.current_stock_name}".strip()
                    info = f"{display_name}  |  時間: {date_str}  |  開: {row['Open']:.2f}  高: {row['High']:.2f}  低: {row['Low']:.2f}  收: {row['Close']:.2f}  |  漲跌: {chg_sign} {abs(chg_val):.2f} ({chg_sign} {abs(chg_pct):.2f}%)  |  量: {vol_str}"
                    self.lbl_hover_info.config(text=info, fg="#FF1744" if chg_pct > 0 else ("#00E676" if chg_pct < 0 else "white"))
        except Exception: pass

    def execute_order(self, action):
        """
        【ADR-008】下單面板全面重構後的委託組裝邏輯。
        依查證過的 shioaji 1.5.6 API 與交易所規則:
          order_lot: Common=整股 Fixing=盤後定價 Odd=盤後零股 IntradayOdd=盤中零股
          order_cond: Cash=現股 MarginTrading=融資 ShortSelling=融券
          order_type: ROD/IOC/FOK (僅整股可切換,其餘一律 ROD)
        零股類 (IntradayOdd/Odd) 交易所規定僅能現股、限價、ROD,不可融資融券、不可當沖;
        盤後定價 (Fixing) 成交價鎖定為當日 (上午) 收盤價,不可自訂價格。
        這些規則已在 set_trade_mode() 用 UI 鎖住,這裡再做一次防呆,避免任何管道繞過鎖定送出違規委託。

        【ADR-013】本函式只負責「驗證 + 組裝 shioaji Order 物件」，組好之後
        不會直接送出，而是呼叫 _show_order_confirmation() 開一個確認視窗，
        使用者按下「確認送出」才會真的呼叫 place_order()；按「取消」則整筆
        委託作廢，只記一筆「已取消下單」的日誌。
        """
        order_type_str = self.cb_order_type.get()
        price_str = self.entry_price.get().strip().replace("。", ".").replace("．", ".")
        qty = self.entry_qty.get()
        mode = self.trade_mode
        mode_labels = {"Common": "整股", "IntradayOdd": "盤中零股", "Fixing": "盤後定價", "Odd": "盤後零股"}
        cond_labels = {"Cash": "現股", "MarginTrading": "融資", "ShortSelling": "融券"}

        if not self.api_logged_in or not HAS_SJ:
            self.log_message(f"【拒絕交易】未連線至券商 API，無法 {action}。")
            return

        is_lot_restricted = mode in ("IntradayOdd", "Odd")  # 零股類

        # 【ADR-009】驗證規則本身移到 core/order_rules.py (純函式,可離線單元測試)，
        # 這裡只負責把結果轉成日誌訊息。零股類/盤後定價/數量上限的規則細節與
        # (拒絕交易) 前綴請見 core.order_rules.validate_stock_order 的 docstring
        # 與 tests/test_core.py。
        if self.asset_type == "stock":
            ok, reason = order_rules.validate_stock_order(mode, order_type_str, self.order_cond, self.order_type_tif, qty)
            if not ok:
                self.log_message(f"【拒絕交易】{reason}")
                return
        else:
            # 期貨沒有零股/盤後定價的概念,但同樣要擋數量必須是有效正整數
            # (上限依交易所保證金與流動性而定,這裡不設本系統自訂上限,只擋非法輸入)。
            try:
                if int(qty) <= 0:
                    self.log_message("【拒絕交易】數量必須是正整數。")
                    return
            except ValueError:
                self.log_message("【錯誤】數量請輸入有效整數。")
                return

        order_price = 0.0
        if order_type_str == "限價":
            try: order_price = float(price_str)
            except ValueError:
                self.log_message("【錯誤】選擇「限價」時，請輸入有效的價格數字！")
                return
            # 【使用者調整#4】送單前的最後一道保險:即使 FocusOut 事件因為某些
            # 操作時機沒觸發到,這裡強制再修正一次,確保絕對不會送出不符合
            # tick 規則的違規價格。
            rounded_price = tick_rules.round_to_tick(order_price, self.asset_type, self.current_symbol)
            if abs(rounded_price - order_price) > 1e-9:
                self.log_message(f"【價格自動修正】{order_price} 不符合跳動單位規則,已自動調整為 {self.fmt_price(rounded_price)}。")
                order_price = rounded_price

        try:
            raw_sym = self.current_symbol.replace('.TW', '').replace('.TWO', '')
            contract = None
            if self.asset_type == "future":
                # 【ADR-028】期貨通用化後不能再硬編 TXF/MXF 四個代號:目前圖表的
                # 合約 (current_contract) 就是查詢時解析好的 R1 連續合約,直接用。
                contract = getattr(self, 'current_contract', None)
                if contract is None:
                    contract = self._resolve_futures_contract(raw_sym)
            else:
                # 【ADR-114】1.7 列舉合約可能回傳只有 code 的輕量型別,
                # 只比 symbol 會變成「查無此代碼」;adapter 兩個欄位都比。
                contract = self.brokers['sinopac'].stock_contract(raw_sym)
                            
            if not contract:
                self.log_message(f"【錯誤】找不到 {raw_sym} 的合約資訊，請確認代碼是否正確！")
                return

            sj_action = sj.constant.Action.Buy if action == "買進" else sj.constant.Action.Sell
            use_daytrade = False  # 先初始化,避免期貨分支或未觸發當沖時在下方讀取到未定義變數
            effective_cond = "Cash"
            effective_tif = self.order_type_tif

            if self.asset_type == "future":
                # 期貨沒有零股/定價/現股融資融券的概念,只套用 條件(ROD/IOC/FOK) 與 限價/市價。
                p_type = sj.constant.FuturesPriceType.MKT if order_type_str == "市價" else sj.constant.FuturesPriceType.LMT
                fop_order_type = getattr(sj.constant.OrderType, self.order_type_tif, sj.constant.OrderType.ROD)
                effective_tif = self.order_type_tif
                order = self.sj_api.Order(price=order_price, quantity=int(qty), action=sj_action, price_type=p_type, order_type=fop_order_type)

            elif self.asset_type == "stock":
                lot_map = {"Common": sj.constant.StockOrderLot.Common, "IntradayOdd": sj.constant.StockOrderLot.IntradayOdd,
                           "Fixing": sj.constant.StockOrderLot.Fixing, "Odd": sj.constant.StockOrderLot.Odd}
                cond_map = {"Cash": sj.constant.StockOrderCond.Cash, "MarginTrading": sj.constant.StockOrderCond.MarginTrading,
                            "ShortSelling": sj.constant.StockOrderCond.ShortSelling}
                sj_order_lot = lot_map.get(mode, sj.constant.StockOrderLot.Common)
                # 零股類 (盤中零股/盤後零股) 強制現股,即使前面驗證通過這裡再保險一次;
                # 盤後定價/整股則照使用者選的 現股/融資/融券 (盤後定價依規則可搭配資券相抵)。
                effective_cond = "Cash" if is_lot_restricted else self.order_cond
                sj_order_cond = cond_map.get(effective_cond, sj.constant.StockOrderCond.Cash)
                effective_tif = "ROD" if mode != "Common" else self.order_type_tif
                sj_order_type = getattr(sj.constant.OrderType, effective_tif, sj.constant.OrderType.ROD)
                p_type = sj.constant.StockPriceType.MKT if order_type_str == "市價" else sj.constant.StockPriceType.LMT

                order_kwargs = dict(price=order_price, quantity=int(qty), action=sj_action, price_type=p_type,
                                     order_type=sj_order_type, order_lot=sj_order_lot, order_cond=sj_order_cond)

                # 【當沖】現股當沖(先賣後買) 只在:整股 + 現股 + 賣出 + 該股可現沖 + 使用者有勾選 時才附加。
                # 【ADR-009】資格判斷規則移到 core/order_rules.is_daytrade_eligible (純函式)。
                use_daytrade = (order_rules.is_daytrade_eligible(mode, effective_cond, action, self.current_day_trade)
                                 and self.daytrade_var.get())
                if use_daytrade:
                    # shioaji 不同版本此參數名稱可能是 daytrade_short 或舊版 first_sell,兩種都試,避免因版本差異丟例外。
                    try:
                        order = self.sj_api.Order(**order_kwargs, daytrade_short=True)
                    except TypeError:
                        try:
                            order = self.sj_api.Order(**order_kwargs, first_sell=sj.constant.StockFirstSell.Yes)
                        except Exception:
                            order = self.sj_api.Order(**order_kwargs)
                            self.log_message("【提示】此 shioaji 版本無法附加當沖旗標,已用一般委託送出,請自行確認是否需要手動標記當沖。")
                else:
                    order = self.sj_api.Order(**order_kwargs)
            else:
                self.log_message("【錯誤】目前僅支援台股與台指期。")
                return

            qty_unit = "股" if is_lot_restricted else ("口" if self.asset_type == "future" else "張")
            price_disp = self.fmt_price(order_price) if order_type_str == '限價' else '市價'

            confirm_ctx = dict(
                contract=contract, order=order, action=action, raw_sym=raw_sym,
                mode=mode, mode_labels=mode_labels, cond_labels=cond_labels,
                effective_cond=effective_cond, effective_tif=effective_tif,
                is_lot_restricted=is_lot_restricted, use_daytrade=use_daytrade,
                qty=qty, qty_unit=qty_unit, price_disp=price_disp, order_type_str=order_type_str,
            )
            self._show_order_confirmation(confirm_ctx)
        except Exception as e: self.log_message(f"【下單異常】: {e}")

    def _show_order_confirmation(self, ctx):
        """
        【ADR-013】下單前的確認視窗。彙整這筆委託的關鍵欄位給使用者最後確認一次，
        避免手滑按到買進/賣出、或設定沒注意到就送出真實委託。「確認送出」才會
        呼叫 _confirm_and_place_order() 真正打 shioaji API；「取消」只記日誌、
        不送出任何委託。
        """
        action = ctx["action"]; mode = ctx["mode"]
        is_future = (self.asset_type == "future")
        action_color = "#FF1744" if action == "買進" else "#00E676"  # 紅漲(買)綠跌(賣),與買賣按鈕配色一致

        dlg = tk.Toplevel(self)
        dlg.title("下單確認")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 380, 340)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="請確認以下委託內容", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 11, 'bold')).pack(pady=(15, 10))

        info_frame = tk.Frame(dlg, bg="#1A2026")
        info_frame.pack(fill=tk.X, padx=20)

        def _row(label, value, value_color="white"):
            row = tk.Frame(info_frame, bg="#1A2026")
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 10), width=8, anchor="w").pack(side=tk.LEFT)
            tk.Label(row, text=value, bg="#1A2026", fg=value_color, font=('微軟正黑體', 10, 'bold'), anchor="w").pack(side=tk.LEFT)

        stock_name = self.current_stock_name or ""
        _row("商品:", f"{ctx['raw_sym']} {stock_name}".strip())
        _row("買賣:", ctx["action"], value_color=action_color)
        if is_future:
            _row("交易別:", "期貨")
        else:
            _row("交易別:", ctx["mode_labels"].get(mode, mode))
            _row("種類:", ctx["cond_labels"].get(ctx["effective_cond"], "現股"))
        _row("條件:", ctx["effective_tif"])
        _row("類別:", ctx["order_type_str"])
        _row("數量:", f"{ctx['qty']} {ctx['qty_unit']}")
        _row("價格:", ctx["price_disp"])
        if ctx["use_daytrade"]:
            _row("當沖:", "是 (先賣後買)", value_color="#FFCA28")

        btn_frame = tk.Frame(dlg, bg="#1A2026")
        btn_frame.pack(fill=tk.X, padx=20, pady=(20, 15), side=tk.BOTTOM)

        def _on_cancel():
            dlg.destroy()
            self.log_message(f"【已取消下單】{ctx['raw_sym']} {action} {ctx['qty']}{ctx['qty_unit']} (使用者取消,未送出委託)")

        def _on_confirm():
            dlg.destroy()
            self._confirm_and_place_order(ctx)

        tk.Button(btn_frame, text="取消", bg="#2A323D", fg="white", font=('微軟正黑體', 10, 'bold'), relief="flat", command=_on_cancel).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))
        tk.Button(btn_frame, text="確認送出", bg=action_color, fg="white" if action == "買進" else "black", font=('微軟正黑體', 10, 'bold'), relief="flat", command=_on_confirm).pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=(5, 0))

    def _confirm_and_place_order(self, ctx):
        """【ADR-013/找到的bug 下單改背景執行緒】使用者在確認視窗按下「確認
        送出」後，才會執行到這裡真正呼叫 shioaji place_order()。

        【修正】place_order() 是同步的網路呼叫，原本直接在主執行緒 (按鈕
        點擊的 callback) 呼叫——shioaji 連線不穩、券商端回應慢、甚至網路
        卡住時，這一行會讓整個 Tk 事件迴圈停擺，所有視窗 (含關閉鈕) 在
        等待期間完全沒反應，使用者回報過「好像當機」、等了好幾分鐘都沒
        動靜，根因就是這裡。改成背景執行緒呼叫，拿到結果 (或例外) 後才
        用 safe_after() 排回主執行緒處理其餘邏輯 (見 _apply_order_result)
        ——等待券商回應期間 UI 仍可正常操作/關閉，不會被單一筆委託的
        網路延遲拖累整個程式。"""
        threading.Thread(target=self._place_order_worker, args=(ctx,), daemon=True).start()

    def _place_order_worker(self, ctx):
        """背景執行緒:只做 place_order() 這個可能較慢的網路呼叫，完成
        (含失敗) 都排回主執行緒 _apply_order_result 處理——這個函式本身
        絕對不能碰任何 tkinter widget (背景執行緒不可以操作 UI)。"""
        try:
            trade = self.sj_api.place_order(ctx["contract"], ctx["order"])
        except Exception as e:
            self.safe_after(0, self._apply_order_result, ctx, None, e)
            return
        self.safe_after(0, self._apply_order_result, ctx, trade, None)

    def _apply_order_result(self, ctx, trade, place_error):
        """【新ADR】place_order() 的結果 (成功的 trade 物件,或失敗的例外)
        一律排回主執行緒才處理——邏輯跟改動前完全相同 (逐段防護/重複
        委託防race/寫入「我的委託單」清單),唯一差別是 place_order()
        本身已經在背景執行緒做完,不會再卡住 UI。place_error 不為 None
        時直接視同原本 place_order() 那一行拋例外,走同一個「下單異常」
        分支,行為與改動前一致。"""
        contract = ctx["contract"]; order = ctx["order"]; action = ctx["action"]; raw_sym = ctx["raw_sym"]
        mode = ctx["mode"]; mode_labels = ctx["mode_labels"]; cond_labels = ctx["cond_labels"]
        is_lot_restricted = ctx["is_lot_restricted"]; use_daytrade = ctx["use_daytrade"]
        qty = ctx["qty"]; qty_unit = ctx["qty_unit"]; price_disp = ctx["price_disp"]
        effective_cond = ctx["effective_cond"]; effective_tif = ctx["effective_tif"]
        try:
            if place_error is not None:
                raise place_error
            status_name = "已委託"
            try:
                status_name = trade.status.status.name if hasattr(trade.status.status, 'name') else str(trade.status.status).split('.')[-1]
                broker_msg = trade.status.msg if trade.status.msg else "已收單"
                if self.asset_type == "stock":
                    tag = f"{mode_labels[mode]}/{cond_labels.get(effective_cond,'現股')}/{effective_tif}"
                    dt_tag = " [當沖]" if use_daytrade else ""
                    self.log_message(f"委託成功 {action} {raw_sym} {tag}{dt_tag} | 數量: {qty}{qty_unit} | 價格: {price_disp}")
                else:
                    self.log_message(f"委託成功 {action} {raw_sym} | 數量: {qty} 口 | 價格: {price_disp}")
                self.log_message(f"📌 狀態: {status_name} ({broker_msg})")
            except Exception: pass

            # 【使用者調整#5】送單成功立即塞進「我的委託單」清單,給即時回饋，
            # 不用等 set_order_callback 的推播才顯示 (推播可能有些微延遲)。
            # 之後 on_order_deal_callback 收到這筆單的後續回報時，會用同一個
            # order_id 覆蓋/更新這筆資料 (狀態、成交量等)，兩邊資料自然會對齊。
            #
            # 【第五輪修正:委託單仍然沒顯示】使用者第四輪回報:日誌印「委託成功
            # PendingSubmit」,但「我的委託單」分頁仍是空的。隔離測試證明這段
            # seed 邏輯本身在正常情境下會正確顯示,所以真實環境最可能是「某個
            # 真實 shioaji 物件的屬性存取丟出例外被吞掉」,而錯誤訊息印在「系統
            # 日誌」分頁、使用者切到「我的委託單」分頁時看不到。這次做兩件事:
            #   (1) 逐段獨立防護:取 order_id 這種可能因 shioaji 版本/物件差異
            #       而丟例外的動作,用自己的 try/except 包起來,取不到就退回本地
            #       暫時 key——絕不讓「取 id 失敗」連帶害整筆委託加不進清單。
            #   (2) 不論成功或失敗,都印出「明確、看得懂」的日誌:成功印
            #       【我的委託單】已加入清單,失敗印【我的委託單加入失敗】+原因。
            #       這樣下次測試可以直接判斷是「根本沒加進去(會看到失敗日誌)」
            #       還是「加進去了但畫面沒顯示(會看到成功日誌但清單空)」,
            #       不必再靠猜。
            try:
                # (1) 取委託 id:這一步最可能因真實 shioaji 物件差異丟例外,單獨防護
                order_id = ''
                try:
                    order_id = getattr(getattr(trade, 'order', None), 'id', '') or getattr(order, 'id', '') or ''
                except Exception as ie:
                    self.log_message(f"【提示】取委託書號失敗,改用暫時識別碼顯示 ({ie})")
                    order_id = ''

                if not order_id:
                    # 【ADR-027 防race】shioaji 委託回報可能比 place_order() 回傳「更早」
                    # 抵達 (callback 在網路等待期間先到)。此時正式項目已在清單裡,
                    # 若再建暫時項目會永久重複兩筆——先檢查是否已有吻合的正式項目
                    # (同商品/同買賣/同量,10 秒內),有就不再建暫時項目。
                    dup = None
                    try:
                        for _oid, _e in self.my_orders.items():
                            if (not str(_oid).startswith('_pending_')
                                    and _e.get('code') == raw_sym
                                    and self._normalize_action(_e.get('action')) == self._normalize_action(action)
                                    and self._safe_int(_e.get('quantity')) == self._safe_int(qty)
                                    and (time.time() - self._safe_ts(_e.get('ts'))) < 10):
                                dup = _oid; break
                    except Exception:
                        dup = None
                    if dup:
                        self._last_pending_order_key = None
                        self.log_message(f"【我的委託單】委託回報已先行抵達 (書號:{dup}),清單已是最新。")
                        self._refresh_my_orders_ui()
                        return
                    order_id = f"_pending_{raw_sym}_{action}_{qty}_{datetime.now().strftime('%H%M%S%f')}"
                    # 記錄這筆「用暫時 key 頂著」的委託資訊，之後 on_order_deal_callback
                    # 收到真正帶 id 的委託回報時，如果資訊吻合，會把這筆暫時的清掉，
                    # 換成正式的一筆，減少重複顯示的機率 (見 _handle_order_event)。
                    self._last_pending_order_key = order_id
                    self._last_pending_order_info = {'code': raw_sym, 'action': action, 'quantity': self._safe_int(qty)}
                else:
                    self._last_pending_order_key = None

                self.my_orders[order_id] = {
                    'id': order_id, 'code': raw_sym, 'action': action,
                    'price': price_disp if price_disp != '市價' else 0,
                    'quantity': self._safe_int(qty), 'order_cond': effective_cond, 'order_lot': mode,
                    'cancel_quantity': 0, 'modified_price': 0, 'filled_quantity': 0,
                    'status_display': status_name,
                    'ts': time.time(), 'time_str': datetime.now().strftime('%H:%M:%S'),
                    # 【ADR-023】保存 shioaji 回傳的 Trade 物件,刪改 (update_order/
                    # cancel_order) 需要對 Trade 操作,不是給 order id。callback 收到
                    # 正式回報時若能取得 trade 也會一併更新 (見 _handle_order_event)。
                    'trade': trade,
                }
                self._refresh_my_orders_ui()
                # (2) 明確的成功回饋:使用者下次若沒看到這行,代表根本沒走到這裡。
                self.log_message(f"【我的委託單】已加入清單: {raw_sym} {action} {qty}{qty_unit} (目前共 {len(self.my_orders)} 筆,可切到「我的委託單」分頁查看)")
            except Exception as e:
                # 任何例外都要被看見,不能再無聲無息地吞掉。
                self.log_message(f"【我的委託單加入失敗】{type(e).__name__}: {e}")
        except Exception as e:
            self.log_message(f"【下單異常】: {e}")
            if self._looks_like_session_dead(e):
                self._mark_session_dead()

    @staticmethod
    def _safe_int(v, default=0):
        """把可能是字串/浮點/None 的數量安全轉成 int,轉不動就回傳 default,不丟例外。"""
        try:
            return int(float(str(v).strip()))
        except (TypeError, ValueError):
            return default

    # ================= 自選股群組維護及輔助互動方法 =================
    def _wl_selected_sym(self):
        """取得自選股目前選取的代碼 (Treeview 的 iid 就是代碼);沒選取回 None。"""
        try:
            iid = self.tree_wl.focus() or ((self.tree_wl.selection() or [None])[0])
            return iid or None
        except Exception:
            return None

    def move_wl_up(self):
        sym = self._wl_selected_sym()
        group = self.current_wl_name.get()
        lst = self.watchlists.get(group, [])
        if not sym or sym not in lst: return
        idx = lst.index(sym)
        if idx == 0: return
        lst[idx-1], lst[idx] = lst[idx], lst[idx-1]
        self.on_wl_change()
        self._wl_select_programmatic(sym)
        self.save_watchlists()

    def move_wl_down(self):
        sym = self._wl_selected_sym()
        group = self.current_wl_name.get()
        lst = self.watchlists.get(group, [])
        if not sym or sym not in lst: return
        idx = lst.index(sym)
        if idx == len(lst) - 1: return
        lst[idx+1], lst[idx] = lst[idx], lst[idx+1]
        self.on_wl_change()
        self._wl_select_programmatic(sym)
        self.save_watchlists()

    def _wl_select_programmatic(self, sym):
        """程式化選取某列 (上移/下移後保持選取),抑制 select 事件避免誤觸查詢。"""
        self._wl_select_suppress = True
        try:
            self.tree_wl.selection_set(sym)
            self.tree_wl.focus(sym)
        except Exception:
            pass
        finally:
            # 事件在 idle 時才派發,延後解除抑制
            self.safe_after(50, lambda: setattr(self, '_wl_select_suppress', False))

    @staticmethod
    def _wl_fmt_quote(q):
        """把 (close, chg, pct) 格式化成三個顯示字串與漲跌 tag。q 為 None 回 '--'。"""
        if not q:
            return "--", "--", "--", 'wl_flat'
        close, chg, pct = q
        try:
            close = float(close); chg = float(chg); pct = float(pct)
        except (TypeError, ValueError):
            return "--", "--", "--", 'wl_flat'
        price_str = f"{close:,.0f}" if close >= 1000 else f"{close:.2f}"
        chg_str = f"{chg:+,.0f}" if abs(chg) >= 100 else f"{chg:+.2f}"
        pct_str = f"{pct:+.2f}%"
        tag = 'wl_up' if chg > 0 else ('wl_down' if chg < 0 else 'wl_flat')
        return price_str, chg_str, pct_str, tag

    def _wl_display_name(self, sym):
        """【第5項】自選股名稱:先試 shioaji 合約 name (台股/期貨代號都可能是純英文,
        P-42 同源,不能先用字元特徵猜美股);解析不到的純英文才取美股 shortName 快取。"""
        try:
            if sym == "^TWII": return "加權指數"
            if sym == "^TWOII": return "櫃買指數"
            if self.api_logged_in and HAS_SJ:
                c = self._resolve_wl_contract(sym)
                name = getattr(c, 'name', '') if c is not None else ''
                if name:
                    if fut_catalog.looks_like_futures_symbol(sym):
                        return fut_catalog.display_name(sym, name)
                    return str(name)
            if self._is_us_symbol(sym):
                return getattr(self, '_wl_us_names', {}).get(sym, '美股')
        except Exception:
            pass
        return "--"

    def on_wl_change(self, event=None):
        group = self.current_wl_name.get()
        syms = list(self.watchlists.get(group, []))
        # worker 讀這份快照決定要抓哪些報價 (UI 執行緒原子替換,不需鎖)
        self._wl_current_syms = syms
        self._wl_select_suppress = True
        try:
            for iid in self.tree_wl.get_children():
                self.tree_wl.delete(iid)
            for sym in syms:
                p, c, r, tag = self._wl_fmt_quote(self._wl_quotes.get(sym))
                name = self._wl_display_name(sym)
                try:
                    self.tree_wl.insert("", tk.END, iid=sym, values=(sym, name, p, c, r), tags=(tag,))
                except Exception:
                    self.tree_wl.insert("", tk.END, values=(sym, name, p, c, r), tags=(tag,))
        finally:
            self.safe_after(50, lambda: setattr(self, '_wl_select_suppress', False))

    def add_watchlist_group(self):
        new_group = sd.askstring("新增群組", "請輸入新群組名稱:")
        if new_group:
            self.watchlists[new_group] = []
            self.cb_wl['values'] = list(self.watchlists.keys())
            self.current_wl_name.set(new_group)
            self.on_wl_change()
            self.save_watchlists()

    def rename_watchlist_group(self):
        old_group = self.current_wl_name.get()
        new_group = sd.askstring("重新命名", "請輸入新群組名稱:", initialvalue=old_group)
        if new_group and new_group != old_group:
            self.watchlists[new_group] = self.watchlists.pop(old_group)
            self.cb_wl['values'] = list(self.watchlists.keys())
            self.current_wl_name.set(new_group)
            self.save_watchlists()

    def on_watchlist_select(self, event=None):
        if self._wl_select_suppress:
            return  # 程式化選取 (上移/下移/重建列表),不觸發查詢
        sym = self._wl_selected_sym()
        if not sym:
            return
        # 【ADR-028】依代碼自動切換市場模式:含數字→台股;^開頭→台股(指數);
        # 純英文且期貨合約存在(或為舊代號)→台期貨;其餘→美股。
        try:
            # 【第十一輪修正】期貨完整代號 (TXFR2/TXF202609) 含數字,必須先於
            # 「含數字=台股」判斷,否則被當股票查 → 合約查無 (使用者回報實例)。
            if sym.startswith('^'):
                self.market_mode.set("台股")
            elif self._looks_like_futures_symbol(sym):
                self.market_mode.set("台期貨")
            elif any(ch.isdigit() for ch in sym):
                self.market_mode.set("台股")
            elif sym.upper() in self.FUT_ALIASES or sym.upper() in ("TXF", "MXF") or (self.api_logged_in and HAS_SJ and self._resolve_futures_contract(sym)):
                self.market_mode.set("台期貨")
            else:
                self.market_mode.set("美股")
        except Exception:
            pass
        self.entry_symbol.delete(0, tk.END)
        self.entry_symbol.insert(0, sym)
        self.start_fetch_thread()
        # 【策略編輯器帶入 / ADR-077】若策略編輯器開著,點自選股把代碼帶進「目前
        # 作用中的欄位」——做B (執行商品) 或看A (訊號來源),由最後點過的欄位決定
        # (見 _qt_editor_symbol_target 的設定)。種類也一併自動判斷:做B→股票/期貨,
        # 看A→指數/期貨/股票 (看A可為指數)。
        self._feed_symbol_to_editor(sym)

    def _feed_symbol_to_editor(self, sym):
        """把代碼帶進策略編輯器目前作用中的欄位 (做B 或 看A)。

        【ADR-119】原本這段直接寫在「點自選股」裡,所以只有自選股能帶入;
        使用者回報「看A做B 選指數時,也希望能直接點上方的指數帶入」——
        上方指數走的是 load_index_chart(),完全沒有經過這段。抽成共用函式後
        兩個入口行為一致,日後多一個入口也只要呼叫這一個函式。
        """
        target = getattr(self, '_qt_editor_symbol_target', None)
        if target is None:
            return
        try:
            dlg_ref, e_sym_ref, cb_tt_ref, lookup_cb, kind = target
            if not dlg_ref.winfo_exists():
                return
            e_sym_ref.delete(0, tk.END)
            e_sym_ref.insert(0, sym)
            tt = (self._watch_trade_type_for_symbol(sym) if kind == 'A'
                  else self._trade_type_for_symbol(sym))
            cb_tt_ref.set(tt)
            if lookup_cb:
                lookup_cb()
        except Exception:
            pass

    def add_to_wl(self):
        group = self.current_wl_name.get()
        sym = self.entry_symbol.get().strip().upper()
        if sym and sym not in self.watchlists.get(group, []):
            self.watchlists[group].append(sym)
            self.on_wl_change()
            self.save_watchlists()

    def del_from_wl(self, event=None):
        group = self.current_wl_name.get()
        sym = self._wl_selected_sym()
        if sym and sym in self.watchlists.get(group, []):
            self.watchlists[group].remove(sym)
            self.on_wl_change()
            self.save_watchlists()

    def load_index_chart(self, symbol):
        self.entry_symbol.delete(0, tk.END)
        self.entry_symbol.insert(0, symbol)
        self.start_fetch_thread()
        # 【ADR-119】點上方的加權/櫃買指數時,也要能帶進策略編輯器 ——
        # 「看A做B」的 A 常常就是指數,原本只有自選股能帶入,指數得手動打。
        self._feed_symbol_to_editor(symbol)

    def set_timeframe(self, tf, fetch=True):
        self.timeframe_var.set(tf)
        for name, btn in self.tf_buttons.items():
            if name == tf: btn.config(bg="#29B6F6", fg="black")
            else: btn.config(bg="#1E242B", fg="white")
        if fetch: self.start_fetch_thread()

    def log_message(self, msg):
        self.log_txt.config(state=tk.NORMAL)
        now = datetime.now().strftime("%H:%M:%S")
        self.log_txt.insert(tk.END, f"[{now}] {msg}\n")
        self.log_txt.see(tk.END)
        self.log_txt.config(state=tk.DISABLED)
        # 【ADR-056/057】鏡射最新一行到「所有」量化面板 (底部分頁 + 獨立視窗)。
        # _qt_uis 在量化分頁建好後才存在,啟動流程早期呼叫 log_message 時
        # 這個屬性可能還沒建立,用 getattr 防呆。
        if getattr(self, '_qt_uis', None):
            try:
                text = str(msg).replace('\n', ' ')
                shown = f"最新: [{now}] " + (text[:90] + '…' if len(text) > 90 else text)
                for ui in self._qt_alive_uis():
                    try:
                        ui['lastlog'].config(text=shown)
                    except Exception:
                        pass
            except Exception:
                pass
        # 【新ADR】量化交易啟動中,任何成交/系統訊息 (統一以「【自動交易-xxx】」
        # 開頭,見 telegram_notify.is_quant_message) 都額外用 Telegram 推播一份。
        # 只在總開關開啟時推播 (使用者要求「啟動量化交易時」才要),且 bot_token/
        # chat_id 都設定好才會真的送;實際 HTTP 呼叫丟背景執行緒,不擋 UI。
        if (getattr(self, '_qt_running', False)
                and telegram_notify.is_quant_message(msg)
                and telegram_notify.config_ready(getattr(self, 'telegram_cfg', None))):
            self._send_telegram_async(str(msg))

    def _send_telegram_async(self, text):
        """背景執行緒送出 Telegram 訊息 (urllib,零新依賴)。成功/失敗都只記
        一行系統日誌 (不用「【自動交易」開頭,避免被 is_quant_message 抓到
        又反覆觸發推播);失敗不拋例外、不影響量化 runner 本身運作。"""
        cfg = self.telegram_cfg
        def _worker():
            import urllib.request
            try:
                req_spec = telegram_notify.build_send_request(
                    cfg.get('bot_token', ''), cfg.get('chat_id', ''), text)
                req = urllib.request.Request(req_spec['url'], data=req_spec['data'],
                                             headers=req_spec['headers'])
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read()
                ok, info = telegram_notify.parse_response(body)
                if not ok:
                    self.safe_after(0, self.log_message, f"【Telegram通知】送出失敗: {info}")
            except Exception as e:
                self.safe_after(0, self.log_message, f"【Telegram通知】送出失敗: {type(e).__name__}: {e}")
        threading.Thread(target=_worker, daemon=True).start()

    # ======================================================================
    # 【ADR-108】Telegram 遠端控制:手機下指令查詢狀態、開關策略
    #
    # 用 getUpdates 輪詢 (不是 webhook):webhook 需要公網 IP 與憑證,一般
    # 家用環境架不起來;輪詢只要能連外就行,零額外基礎設施。
    #
    # 安全機制全部在 core/telegram_control.py 且可離線測試——這是能遠端
    # 控制真實下單系統的功能,絕不允許沒測過就上。
    # ======================================================================
    TG_POLL_INTERVAL = 3.0          # 輪詢間隔 (秒)
    TG_POLL_TIMEOUT = 25            # long polling 等待秒數

    def _tg_control_enabled(self):
        cfg = getattr(self, 'telegram_cfg', None) or {}
        return bool(cfg.get('remote_control')) and telegram_notify.config_ready(cfg)

    def _tg_log_control_state(self):
        """【ADR-138】把「手機下指令會不會有回應」的現況寫進系統日誌。

        規則在 core/telegram_control.control_status(),這裡只負責印出來 ——
        沒開的時候也要印,那正是使用者最需要看到訊息的時候。
        """
        ok, why = telegram_control.control_status(getattr(self, 'telegram_cfg', None))
        self.log_message(("【遠端控制】✅ " if ok else "【遠端控制】ℹ️ ") + why)

    def start_telegram_control(self):
        """啟動遠端控制輪詢執行緒 (只啟動一次)。"""
        if getattr(self, '_tg_ctrl_thread_started', False):
            return
        self._tg_ctrl_thread_started = True
        self._tg_offset = 0
        self._tg_pending = None      # 待確認指令 {'code','expire','cmd','arg'}
        threading.Thread(target=self._tg_poll_worker, daemon=True).start()

    def _tg_poll_worker(self):
        """背景輪詢 Telegram 指令。整段包 try,絕不讓它影響主程式。"""
        import urllib.request
        import urllib.parse
        while not self._closing:
            try:
                if not self._tg_control_enabled():
                    time.sleep(self.TG_POLL_INTERVAL)
                    continue
                cfg = self.telegram_cfg
                url = (f"https://api.telegram.org/bot{cfg.get('bot_token','')}/getUpdates?"
                       + urllib.parse.urlencode({'offset': self._tg_offset,
                                                 'timeout': self.TG_POLL_TIMEOUT}))
                req = urllib.request.Request(url, headers={'User-Agent': 'StockBuild'})
                with urllib.request.urlopen(req, timeout=self.TG_POLL_TIMEOUT + 10) as resp:
                    payload = json.loads(resp.read().decode('utf-8'))
                for upd in (payload.get('result') or []):
                    self._tg_offset = max(self._tg_offset, int(upd.get('update_id', 0)) + 1)
                    msg = upd.get('message') or upd.get('edited_message') or {}
                    chat_id = str((msg.get('chat') or {}).get('id', ''))
                    text = str(msg.get('text') or '')
                    if not text:
                        continue
                    # 指令一律排回主執行緒執行:它會改策略狀態、讀 tkinter 變數,
                    # 在背景執行緒直接改會與 UI 競態 (鐵則 13 的精神)。
                    self.safe_after(0, self._tg_handle_command, chat_id, text)
            except Exception:
                # 網路不穩/Telegram 暫時故障都很常見,靜默重試即可;
                # 真的設定錯誤會在使用者按「測試」時就發現。
                time.sleep(self.TG_POLL_INTERVAL * 2)
                continue
            time.sleep(self.TG_POLL_INTERVAL)

    def _tg_reply(self, text):
        self._send_telegram_async(text)

    def _tg_handle_command(self, chat_id, text):
        """在主執行緒處理一則指令。所有分支都會回覆並記錄日誌。"""
        try:
            cfg = getattr(self, 'telegram_cfg', None) or {}
            cmd, arg = telegram_control.parse_command(text)
            if cmd is None:
                return          # 不是指令就不理會 (避免一般聊天觸發回覆)

            # 【安全第一關】只有設定檔那個 chat_id 能下指令
            if not telegram_control.is_authorized(chat_id, cfg.get('chat_id')):
                self.log_message(f"【遠端控制】⛔ 拒絕未授權的指令 (chat_id={chat_id}): {text}")
                return          # 刻意不回覆,不讓對方確認 Bot 存在

            self.log_message(f"【遠端控制】收到指令:{text}")

            if cmd == '/help':
                return self._tg_reply(telegram_control.help_text())
            if cmd == '/status':
                return self._tg_reply(telegram_control.status_text(
                    bool(self.api_logged_in), bool(self._qt_running), self.strategies))
            if cmd == '/list':
                return self._tg_reply(telegram_control.list_text(self.strategies))
            # 【ADR-137】查詢類支援「只看某個帳戶」——使用者的原話是
            # 「我要看目前 XX 帳號的損益」。不帶參數時維持原本的全部列出。
            if cmd == '/positions':
                return self._tg_reply(self._tg_positions_text(arg))
            if cmd == '/pnl':
                return self._tg_reply(self._tg_pnl_text(arg))
            if cmd == '/accounts':
                return self._tg_reply(self._tg_accounts_text())
            if cmd == '/strategy':
                st, why = telegram_control.match_strategy(self.strategies, arg)
                if st is None:
                    return self._tg_reply(f'❌ {why}')
                return self._tg_reply(telegram_control.strategy_detail_text(
                    st, self._qt_runtime(st['id'])))
            if cmd == '/yes':
                return self._tg_confirm(arg)

            # 停用類:不需確認 (往安全方向,緊急時不該還要多打一則訊息)
            if cmd == '/stop_all':
                return self._tg_apply('/stop_all', '')
            if cmd == '/off':
                return self._tg_apply('/off', arg)

            # 啟用類:先發確認碼。確認訊息要把「這一步會讓什麼開始動」講清楚
            # ——遠端看不到畫面,確認碼本身不構成知情同意。
            if cmd in ('/on', '/start_all'):
                target_name = None
                detail = None
                if cmd == '/on':
                    st, why = telegram_control.match_strategy(self.strategies, arg)
                    if st is None:
                        return self._tg_reply(f'❌ {why}')
                    # 前置檢查提前到「發確認碼之前」:不要讓使用者確認了一個
                    # 註定會失敗的操作。_tg_apply 在確認後會再檢查一次,因為
                    # 這 120 秒之間狀態可能被改動。
                    ok, blk, pend = self._qt_enable_blockers(st)
                    if not ok:
                        return self._tg_reply(f'❌ {blk}')
                    if pend:
                        return self._tg_reply(
                            f"❌ 策略「{st.get('name')}」的持倉狀態與模擬帳戶對不上,\n"
                            f"需要在主程式做「持倉核對」後才能啟用。")
                    target_name = str(st.get('name'))
                    detail = (f"{st.get('symbol')} {st.get('timeframe')} "
                              f"[{st.get('mode', '模擬')}] x{st.get('qty')}")
                else:
                    en = [s for s in (self.strategies or []) if s.get('enabled')]
                    if not en:
                        return self._tg_reply('❌ 沒有任何啟用中的策略,啟動總開關沒有意義。')
                    lv = [s for s in en if s.get('mode') == '實單']
                    detail = (f"將啟動 {len(en)} 個策略 (實單 {len(lv)} / 模擬 {len(en) - len(lv)})"
                              + (f"\n⚠️ 實單策略:{'、'.join(str(s.get('name')) for s in lv[:5])}"
                                 if lv else ''))
                code, expire = telegram_control.new_confirm_code()
                self._tg_pending = {'code': code, 'expire': expire, 'cmd': cmd, 'arg': arg}
                return self._tg_reply(
                    telegram_control.confirm_prompt(cmd, arg, code, target_name, detail))
        except Exception as e:
            self.log_message(f"【遠端控制】處理指令失敗:{type(e).__name__}: {e}")
            try:
                self._tg_reply(f'❌ 指令處理失敗:{type(e).__name__}')
            except Exception:
                pass

    def _tg_confirm(self, arg):
        ok, why = telegram_control.check_confirm(getattr(self, '_tg_pending', None), arg)
        if not ok:
            # 過期的待確認指令直接丟掉:留著只會讓下一次 /yes 收到「確認碼
            # 不正確」這種誤導性訊息,也避免一個舊指令長期掛在那裡。
            if '過期' in why:
                self._tg_pending = None
            return self._tg_reply(f'❌ {why}')
        pending = self._tg_pending
        self._tg_pending = None
        self._tg_apply(pending['cmd'], pending['arg'])

    def _tg_apply(self, cmd, arg):
        """真正改變狀態的地方。每個動作都寫進系統日誌 + 回覆手機。"""
        if cmd == '/stop_all':
            # 走跟畫面上「⛔ 全部停止」同一個方法,行為完全一致。
            self._qt_stop_all()
            self.log_message('【自動交易】⛔ 上述停止是由遠端指令觸發。')
            return self._tg_reply('🛑 已關閉自動交易總開關。\n(既有持倉不會自動平倉。)')

        if cmd == '/start_all':
            enabled = [s for s in (self.strategies or []) if s.get('enabled')]
            if not enabled:
                return self._tg_reply('❌ 沒有任何啟用中的策略,啟動總開關沒有意義。')
            live = [s for s in enabled if s.get('mode') == '實單']
            # 只有「有實單策略」才要求券商已登入:全模擬的策略不需要連線,
            # 不該因為沒登入就連模擬都不能跑。
            if live and not self.api_logged_in:
                return self._tg_reply('❌ 有實單策略但券商未登入,不予啟動。')
            self._qt_running = True
            self._qt_update_status_label()
            self._qt_refresh_tree()
            self.log_message(f'【自動交易】✅ 已由遠端指令開啟總開關:{len(enabled)} 個策略運轉中 '
                             f'(實單 {len(live)} 個 / 模擬 {len(enabled) - len(live)} 個)。')
            return self._tg_reply(f'🟢 已開啟自動交易總開關。\n'
                                  f'運轉中 {len(enabled)} 個策略 (實單 {len(live)} / '
                                  f'模擬 {len(enabled) - len(live)})。')

        st, why = telegram_control.match_strategy(self.strategies, arg)
        if st is None:
            return self._tg_reply(f'❌ {why}')
        name = str(st.get('name'))
        if cmd == '/off':
            self._qt_finish_set_enabled(st, False)
            self.log_message(f'【自動交易】⛔ 已由遠端指令停用策略「{name}」。')
            return self._tg_reply(f'🛑 已停用策略「{name}」。')
        if cmd == '/on':
            # 走跟畫面上「▶ 啟用」完全相同的前置檢查,不因為是遠端就放寬。
            ok, why, pending = self._qt_enable_blockers(st)
            if not ok:
                self.log_message(f'【自動交易】遠端啟用被擋下:{why}')
                return self._tg_reply(f'❌ {why}')
            if pending:
                # 持倉核對需要開視窗讓使用者選處理方式,手機上沒辦法做決定,
                # 也不該用預設值幫他決定 (起點錯了整條策略後續都是錯的)。
                self.log_message(f'【自動交易】遠端啟用被擋下:策略「{name}」持倉狀態與模擬帳戶不一致。')
                return self._tg_reply(
                    f'❌ 策略「{name}」的持倉狀態與模擬帳戶對不上,\n'
                    f'需要在主程式做「持倉核對」後才能啟用。')
            self._qt_finish_set_enabled(st, True)
            mode = str(st.get('mode', '模擬'))
            self.log_message(f'【自動交易】▶ 已由遠端指令啟用策略「{name}」({mode})。')
            tail = ('\n⚠️ 這是實單策略,總開關開啟後會用真實資金下單。'
                    if mode == '實單' else '')
            extra = ('' if self._qt_running else
                     '\n提醒:自動交易總開關目前是關閉的,策略不會實際運作。')
            return self._tg_reply(f'🟢 已啟用策略「{name}」({mode})。{tail}{extra}')

    def _tg_pick_account(self, arg):
        """【ADR-137】把指令參數解析成單一帳戶。回傳 (帳戶 或 None, 錯誤訊息 或 None)。

        沒帶參數 → (None, None) 代表「全部帳戶」,不是錯誤。
        """
        if not str(arg or '').strip():
            return None, None
        accts = getattr(self, 'paper_accts', None) or {}
        acct, why = telegram_control.match_account(accts, arg)
        if acct is None:
            return None, f'❌ {why}'
        return acct, None

    def _tg_positions_text(self, arg=''):
        try:
            acct, err = self._tg_pick_account(arg)
            if err:
                return err
            return telegram_control.positions_text(
                getattr(self, 'paper_accts', None) or {}, only=acct)
        except Exception as e:
            return f'❌ 查詢持倉失敗:{type(e).__name__}'

    def _tg_pnl_text(self, arg=''):
        try:
            acct, err = self._tg_pick_account(arg)
            if err:
                return err
            return telegram_control.pnl_text(getattr(self, 'paper_accts', None) or {},
                                             datetime.now().strftime('%Y-%m-%d'),
                                             only=acct)
        except Exception as e:
            return f'❌ 查詢損益失敗:{type(e).__name__}'

    def _tg_accounts_text(self):
        try:
            return telegram_control.accounts_text(getattr(self, 'paper_accts', None) or {})
        except Exception as e:
            return f'❌ 查詢帳戶失敗:{type(e).__name__}'

    def _telegram_test_send(self, bot_token, chat_id, text, status_cb):
        """設定視窗的「測試發送」用:直接用畫面上目前 (可能還沒存檔) 的
        bot_token/chat_id 送一則測試訊息,結果回報給 status_cb (更新對話框
        裡的狀態文字),不寫進主系統日誌、不受 is_quant_message/總開關限制。"""
        def _worker():
            import urllib.request
            try:
                req_spec = telegram_notify.build_send_request(bot_token, chat_id, text)
                req = urllib.request.Request(req_spec['url'], data=req_spec['data'],
                                             headers=req_spec['headers'])
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read()
                ok, info = telegram_notify.parse_response(body)
                self.safe_after(0, status_cb, ("✓ 測試訊息已送出,請檢查 Telegram。" if ok
                                               else f"✗ 送出失敗: {info}"), ok)
            except ValueError as e:
                self.safe_after(0, status_cb, f"✗ {e}", False)
            except Exception as e:
                self.safe_after(0, status_cb, f"✗ 送出失敗: {type(e).__name__}: {e}", False)
        threading.Thread(target=_worker, daemon=True).start()

    def _qt_open_telegram_settings(self):
        """【新ADR】Telegram 通知設定:量化交易總開關開啟期間,任何成交/系統
        訊息 (【自動交易-xxx】開頭) 都會額外推播一份到這裡設定的聊天。"""
        cfg = dict(self.telegram_cfg or {})
        dlg = tk.Toplevel(self)
        dlg.title("📱 Telegram 通知/遠端控制設定")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 560, 480)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass
        tk.Label(dlg, text=("啟動自動交易「總開關」期間,任何成交/風控/休市待命/例外等系統訊息 "
                            "(系統日誌裡「【自動交易-xxx】」開頭的那些) 都會額外用 Telegram 推播一份。\n"
                            "取得方式:跟 @BotFather 對話建立 Bot 拿到 Token;跟你的 Bot 說一句話後,"
                            "用 @userinfobot 或 https://api.telegram.org/bot<Token>/getUpdates 查出 Chat ID。"),
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 9), wraplength=520,
                 justify='left').pack(fill=tk.X, padx=10, pady=(10, 6))

        row = tk.Frame(dlg, bg="#1A2026"); row.pack(fill=tk.X, padx=12, pady=2)
        tk.Label(row, text="Bot Token", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=0, column=0, sticky='w')
        e_token = tk.Entry(row, width=42, bg="#2A323D", fg="white", show="*")
        e_token.insert(0, cfg.get('bot_token', '')); e_token.grid(row=0, column=1, padx=4, sticky='w', pady=(0, 4))
        tk.Label(row, text="Chat ID", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=1, column=0, sticky='w')
        e_chat = tk.Entry(row, width=20, bg="#2A323D", fg="white")
        e_chat.insert(0, cfg.get('chat_id', '')); e_chat.grid(row=1, column=1, padx=4, sticky='w')

        var_enabled = tk.BooleanVar(value=bool(cfg.get('enabled', False)))
        tk.Checkbutton(dlg, text="啟用 Telegram 通知", variable=var_enabled,
                       bg="#1A2026", fg="#00E676", selectcolor="#2A323D", activebackground="#1A2026",
                       font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(8, 2))

        # 【ADR-108】遠端控制跟通知分開兩個開關:通知只是唯讀推播,遠端控制
        # 則能讓系統開始下單,風險等級完全不同,不該被同一個勾選一起打開。
        var_remote = tk.BooleanVar(value=bool(cfg.get('remote_control', False)))
        tk.Checkbutton(dlg, text="⚠️ 啟用手機遠端控制 (可查詢狀態、開關策略)", variable=var_remote,
                       bg="#1A2026", fg="#FFA726", selectcolor="#2A323D", activebackground="#1A2026",
                       font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(2, 0))
        tk.Label(dlg, text=("開啟後,只有上面填的那一個 Chat ID 可以下指令 (其他人一律拒絕並記錄)。"
                            "手機可用 /status /list /positions /pnl 查詢、/off /stop_all 停用;"
                            "「啟用策略」與「開啟總開關」需回傳確認碼才生效。本介面不提供下單/改單。"),
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 8), wraplength=520,
                 justify='left').pack(fill=tk.X, padx=12, pady=(2, 2))

        lbl_status = tk.Label(dlg, text="", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9),
                              wraplength=520, justify='left', anchor='w')
        lbl_status.pack(fill=tk.X, padx=12, pady=(4, 0))

        def _set_status(msg, ok=True):
            try:
                if dlg.winfo_exists():
                    lbl_status.config(text=msg, fg="#00E676" if ok else "#FF5252")
            except Exception:
                pass

        def _test():
            token = e_token.get().strip()
            chat_id = e_chat.get().strip()
            if not token or not chat_id:
                _set_status("✗ 請先填 Bot Token 與 Chat ID", False)
                return
            _set_status("送出中…")
            self._telegram_test_send(token, chat_id, "【測試訊息】StockBuild Telegram 通知設定成功。", _set_status)

        def _save():
            token = e_token.get().strip()
            chat_id = e_chat.get().strip()
            enabled = bool(var_enabled.get())
            remote = bool(var_remote.get())
            if (enabled or remote) and not (token and chat_id):
                _set_status("✗ 啟用通知/遠端控制前,Bot Token 與 Chat ID 都要先填", False)
                return
            config_store.save_telegram_config(self.TELEGRAM_CONFIG_FILE, token, chat_id,
                                              enabled, remote)
            self.telegram_cfg = {'bot_token': token, 'chat_id': chat_id,
                                 'enabled': enabled, 'remote_control': remote}
            self.log_message(f"【Telegram通知】設定已儲存 ({'已啟用' if enabled else '已停用'})。")
            # 【ADR-138】改用同一份 control_status():存檔後印的話和啟動時
            # 印的話一致,使用者不會看到兩種說法。
            self._tg_log_control_state()
            if remote:
                self.start_telegram_control()
            dlg.destroy()

        foot = tk.Frame(dlg, bg="#1A2026"); foot.pack(pady=10)
        tk.Button(foot, text="🧪 測試發送", bg="#00ACC1", fg="black", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=14, pady=3, command=_test).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="儲存", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=18, pady=3, command=_save).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=18, pady=3, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    # ================= 使用者調整#5:我的委託單 / 我的已成交 =================
    # ================= 【第十一輪 第2項】我的庫存 =================
    def refresh_positions(self):
        """更新庫存 (按鈕/切分頁觸發)。網路查詢丟背景執行緒,不擋 UI;有防重入。"""
        if not (self.api_logged_in and HAS_SJ and self.sj_api):
            self.log_message("【庫存】請先登入券商 API 再查詢庫存。")
            return
        if self._positions_loading:
            return
        self._positions_loading = True
        try:
            self.lbl_positions_summary.config(text="查詢中...")
        except Exception:
            pass
        threading.Thread(target=self._positions_refresh_worker, daemon=True).start()

    @staticmethod
    def _position_to_dict(p, acct_label):
        """把 shioaji Position 物件的『全部欄位』防禦式轉成 dict (明細視窗要看全部數字)。"""
        d = {'帳戶': acct_label}
        try:
            raw = dict(getattr(p, '__dict__', {}) or {})
        except Exception:
            raw = {}
        if not raw:
            for k in dir(p):
                if k.startswith('_'):
                    continue
                try:
                    v = getattr(p, k)
                    if not callable(v):
                        raw[k] = v
                except Exception:
                    continue
        for k, v in raw.items():
            d[str(k)] = v
        return d

    def _positions_fetch_once(self):
        """實際呼叫 list_positions (證券帳戶 + 期貨帳戶各一次,按需查詢非輪詢)。
        回傳 (顯示列 list, 原始 dict list)。抽成獨立方法方便離線測試。"""
        rows, raws = [], []
        accounts = []
        try:
            if getattr(self.sj_api, 'stock_account', None) is not None:
                # 【第十三輪修正】shioaji list_positions 的證券 quantity 欄位本身
                # 就是「股數」,不是「張數」(1張=1000股);之前標「張」是單位誤植,
                # 數字沒有錯但單位標錯,導致跟券商 App 核對時顯示的數字對不起來。
                # 這裡只改單位標籤,不動數值本身 (數值本來就是對的)。
                accounts.append((self.sj_api.stock_account, '證券', '股'))
        except Exception:
            pass
        try:
            if getattr(self.sj_api, 'futopt_account', None) is not None:
                accounts.append((self.sj_api.futopt_account, '期貨', '口'))
        except Exception:
            pass
        for acct, label, unit in accounts:
            if label == '期貨' and getattr(self, '_fut_positions_unavailable', False):
                continue  # 【第十五輪修正】本次連線已確認期貨帳戶不可查,不再重試洗版
            try:
                # 【第十六輪修正 第1項】ADR-033 只改了標籤是不夠的:shioaji
                # list_positions 證券帳戶「預設」回傳單位其實是張 (21=21張),
                # 直接標成股會跟券商 App 對不起來 (使用者實測)。正確做法是
                # 用 unit=Unit.Share 要求券商直接回「股數」(含零股,例如
                # 21張+80股 會回 21080)。舊版 shioaji 沒有 Unit 參數時退回
                # 預設呼叫並把單位標回張,寧可標對單位也不猜換算。
                if label == '證券':
                    try:
                        positions = self.sj_api.list_positions(acct, unit=sj.constant.Unit.Share) or []
                    except (AttributeError, TypeError):
                        positions = self.sj_api.list_positions(acct) or []
                        unit = '張'
                else:
                    positions = self.sj_api.list_positions(acct) or []
            except Exception as e:
                msg = str(e)
                # 【第十五輪修正】406 Account Not Acceptable = 期貨帳戶不可用
                # (未開通期貨帳戶或未簽署 API 查詢同意書),不是程式錯誤——
                # 給友善說明並在本次連線內只提示一次,不要每次更新都重複報錯。
                if label == '期貨' and ('406' in msg or 'Account Not Acceptable' in msg):
                    self._fut_positions_unavailable = True
                    self.safe_after(0, self.log_message,
                        "【庫存】期貨帳戶無法查詢 (券商回覆 406 Account Not Acceptable):通常代表"
                        "尚未開通期貨帳戶、或期貨帳戶尚未簽署 API 查詢/交易同意書。已改為只查證券庫存;"
                        "若你確實有期貨帳戶,請洽永豐證券確認 API 權限後重新登入。")
                else:
                    self.safe_after(0, self.log_message, f"【庫存】{label}帳戶查詢失敗: {type(e).__name__}: {e}")
                if self._looks_like_session_dead(e):
                    self._mark_session_dead()
                continue
            for p in positions:
                try:
                    code = str(getattr(p, 'code', '') or '')
                    qty = self._safe_int(getattr(p, 'quantity', 0))
                    avg = float(getattr(p, 'price', 0) or 0)
                    last = float(getattr(p, 'last_price', 0) or 0)
                    pnl = float(getattr(p, 'pnl', 0) or 0)
                    direction = self._normalize_action(getattr(p, 'direction', ''))
                    # 報酬率用價差算 (與數量單位無關,證券/期貨通用;賣方向反號)
                    ret = 0.0
                    if avg > 0 and last > 0:
                        ret = (last - avg) / avg * 100.0
                        if '賣' in direction:
                            ret = -ret
                    name = ''
                    try:
                        if label == '證券':
                            c = self.sj_api.Contracts.Stocks.get(code)
                            name = str(getattr(c, 'name', '') or '') if c is not None else ''
                        else:
                            grp = getattr(self.sj_api.Contracts.Futures, code[:3], None)
                            first = next(iter(grp), None) if grp is not None else None
                            name = str(getattr(first, 'name', '') or '') if first is not None else ''
                    except Exception:
                        name = ''
                    rows.append({'acct': label, 'code': code, 'name': name or '--',
                                 'direction': direction, 'qty': f"{qty}{unit}",
                                 'avg': avg, 'last': last, 'pnl': pnl, 'ret': ret})
                    raws.append(self._position_to_dict(p, label))
                except Exception:
                    continue
        return rows, raws

    def _positions_refresh_worker(self):
        try:
            rows, raws = self._positions_fetch_once()
            self.safe_after(0, self._apply_positions, rows, raws)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【庫存】查詢異常: {type(e).__name__}: {e}")
            self.safe_after(0, lambda: setattr(self, '_positions_loading', False))

    def _apply_positions(self, rows, raws):
        """UI 執行緒套用庫存:先備妥→再刪→再插 (P-31),紅賺綠賠,更新摘要。"""
        try:
            self._positions_raw = raws
            prepared = []
            for r in rows:
                try:
                    try:
                        avg_str = self.fmt_price(r['avg']) if r['avg'] else '--'
                        last_str = self.fmt_price(r['last']) if r['last'] else '--'
                    except Exception:
                        avg_str, last_str = str(r['avg']), str(r['last'])
                    pnl = r['pnl']
                    pnl_str = f"{_fmt_amt_signed(pnl)}"
                    ret_str = f"{r['ret']:+.2f}%"
                    tag = 'pos_up' if pnl > 0 else ('pos_down' if pnl < 0 else 'pos_flat')
                    prepared.append(((r['acct'], r['code'], r['name'], r['direction'],
                                      r['qty'], avg_str, last_str, pnl_str, ret_str), tag))
                except Exception:
                    continue
            for iid in self.tree_positions.get_children():
                self.tree_positions.delete(iid)
            for vals, tag in prepared:
                self.tree_positions.insert("", tk.END, values=vals, tags=(tag,))
            total_pnl = sum(r['pnl'] for r in rows) if rows else 0.0
            ts = datetime.now().strftime('%H:%M:%S')
            if rows:
                self.lbl_positions_summary.config(
                    text=f"共 {len(rows)} 檔 | 總未實現損益: {_fmt_amt_signed(total_pnl)} | 更新: {ts}",
                    fg=('#FF1744' if total_pnl > 0 else ('#00E676' if total_pnl < 0 else '#8A99AD')))
            else:
                self.lbl_positions_summary.config(text=f"目前無庫存 | 更新: {ts}", fg="#8A99AD")
            self.log_message(f"【庫存】已更新: {len(rows)} 檔,總未實現損益 {_fmt_amt_signed(total_pnl)}。")
        except Exception as e:
            self.log_message(f"【庫存畫面更新異常】{type(e).__name__}: {e}")
        finally:
            self._positions_loading = False

    # 【第十四輪修正】明細視窗欄位中文對照。涵蓋 shioaji StockPosition/
    # FuturePosition 常見欄位;未列在表中的欄位(不同版本可能有差異)保留原始
    # key 當標題,至少不會漏資料,只是那一欄暫時沒有中文名稱。
    POSITION_FIELD_LABELS = {
        '帳戶': '帳戶', 'id': '序號', 'code': '代碼',
        'direction': '方向', 'quantity': '庫存量', 'price': '均價',
        'last_price': '現價', 'pnl': '損益', 'yd_quantity': '昨日庫存',
        'cond': '交易條件', 'margin_purchase_amount': '融資金額',
        'collateral': '擔保品', 'short_sale_margin': '融券保證金',
        'interest': '利息', 'channel': '交易通路', 'account_id': '帳號',
        'pnl_percent': '損益率%', 'cost_price': '成本價',
        'last_trade_price': '最後成交價', 'first_settle_datetime': '首次交割時間',
        'accounting_type': '會計方式', 'dividend_recevied': '已收股利',
        'dividend_recevigable': '應收股利',
    }

    @classmethod
    def _position_field_label(cls, key):
        return cls.POSITION_FIELD_LABELS.get(key, key)

    def _position_field_display(self, key, value):
        """欄位值的中文化顯示 (目前只有『方向』需要轉換;其餘欄位原樣顯示)。"""
        if key == 'direction':
            return self._normalize_action(str(value))
        return str(value)

    def _open_positions_detail_window(self):
        """【第2項】完整明細視窗:列出 list_positions 回傳的全部原始欄位數字 (全中文顯示)。"""
        if not self._positions_raw:
            self.log_message("【庫存】尚無資料,先按「更新庫存」查詢後再開明細視窗。")
            self.refresh_positions()
            return
        dlg = tk.Toplevel(self)
        dlg.title(f"庫存完整明細 — 共 {len(self._positions_raw)} 檔")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 960, 420)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass
        # 欄位 = 所有原始 key 的聯集,常用欄位排前面
        preferred = ['帳戶', 'code', 'direction', 'quantity', 'price', 'last_price', 'pnl', 'yd_quantity']
        all_keys = []
        for d in self._positions_raw:
            for k in d.keys():
                if k not in all_keys:
                    all_keys.append(k)
        cols = [k for k in preferred if k in all_keys] + [k for k in all_keys if k not in preferred]
        frame = tk.Frame(dlg, bg="#1A2026"); frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        tv = ttk.Treeview(frame, columns=cols, show="headings", style='Trades.Treeview')
        for c in cols:
            # 【第十四輪修正】表頭改用中文名稱 (POSITION_FIELD_LABELS);寬度仍依
            # 顯示文字長度估算,中文字較寬所以係數比原本 (給英文用的) 略增。
            label = self._position_field_label(c)
            tv.heading(c, text=label)
            tv.column(c, width=max(70, min(150, 16 * len(label) + 30)), anchor="center")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tv.tag_configure('visible_row', foreground='#FFFFFF', background='#12161A')
        for d in self._positions_raw:
            # 【第十四輪修正】值也中文化 (目前主要是「方向」:Action.Buy/Sell → 買進/賣出)
            tv.insert("", tk.END, values=tuple(self._position_field_display(c, d.get(c, '')) for c in cols), tags=('visible_row',))
        total_pnl = 0.0
        for d in self._positions_raw:
            try:
                total_pnl += float(d.get('pnl', 0) or 0)
            except Exception:
                pass
        bar = tk.Frame(dlg, bg="#1A2026"); bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        tk.Label(bar, text=f"總未實現損益: {_fmt_amt_signed(total_pnl)}", bg="#1A2026",
                 fg=('#FF1744' if total_pnl > 0 else ('#00E676' if total_pnl < 0 else '#FFFFFF')),
                 font=('微軟正黑體', 11, 'bold')).pack(side=tk.LEFT)
        tk.Button(bar, text="🔄 重新查詢", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=10, pady=2,
                  command=lambda: [dlg.destroy(), self.refresh_positions()]).pack(side=tk.RIGHT, padx=4)
        tk.Button(bar, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 9), padx=14, pady=2, command=dlg.destroy).pack(side=tk.RIGHT)

    # ================= 【ADR-035】量化自動交易 =================
    QT_STRATEGY_FILE = app_path("quant_strategies.json")   # 【ADR-060】絕對路徑
    QT_STATE_FILE = app_path("quant_state.json")      # 【ADR-060】絕對路徑
    QT_PAPER_FILE = app_path("paper_account.json")    # 【ADR-060】絕對路徑
    QT_TF_DAYS = {"1分K": 4, "5分K": 7, "15分K": 14, "30分K": 21, "60分K": 35, "日K": 300,
                  "周K": 700, "月K": 1500}  # 【新增】週期擴充:周K/月K 要夠長的原始資料才湊得出夠多根

    # 【ADR-120】原本這裡有 QT_PATTERN_PERSISTENT_IDS (盤勢型態通知去重用)。
    # 盤勢/型態偵測已從終極波段策略搬到主圖【盤勢判斷】,去重清單一併搬進
    # core/regime_panel.PERSISTENT_PATTERN_IDS。

    def _qt_load(self):
        """載入策略與持倉狀態。總開關 _qt_running 絕不持久化——每次啟動一律關閉。"""
        self.strategies = []
        self.strategy_runtimes = {}
        try:
            if os.path.exists(self.QT_STRATEGY_FILE):
                with open(self.QT_STRATEGY_FILE, 'r', encoding='utf-8') as f:
                    self.strategies = json.load(f) or []
        except Exception as e:
            self.log_message(f"【自動交易】策略檔載入失敗 ({e}),以空清單啟動。")
        try:
            if os.path.exists(self.QT_STATE_FILE):
                with open(self.QT_STATE_FILE, 'r', encoding='utf-8') as f:
                    self.strategy_runtimes = json.load(f) or {}
        except Exception:
            self.strategy_runtimes = {}
        for s in self.strategies:
            if s.get('id') not in self.strategy_runtimes:
                self.strategy_runtimes[s['id']] = strategy_engine.new_runtime()
        # 【ADR-041/新ADR多帳戶】虛擬模擬帳戶:檔案存的是 {account_id: 帳戶dict}。
        # 相容舊版單一帳戶檔 (頂層直接是帳戶dict,有 'cash' 鍵) ——讀到舊格式就
        # 原地搬進新格式當「預設帳戶」(id 固定用 'default',跟 strategy_engine
        # account_id_of() 的預設值對齊,舊策略不用手動改設定就能接上原本的部位)。
        self.paper_accts = {}
        try:
            if os.path.exists(self.QT_PAPER_FILE):
                with open(self.QT_PAPER_FILE, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and 'cash' in raw:
                    raw.setdefault('id', paper_account.DEFAULT_ACCOUNT_ID)
                    raw.setdefault('name', '預設帳戶')
                    self.paper_accts = {raw['id']: raw}
                elif isinstance(raw, dict):
                    self.paper_accts = raw
        except Exception:
            self.paper_accts = {}
        if not self.paper_accts:
            acct = paper_account.new_account(account_id=paper_account.DEFAULT_ACCOUNT_ID)
            self.paper_accts = {acct['id']: acct}

    def _qt_save(self):
        try:
            with open(self.QT_STRATEGY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.strategies, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log_message(f"【自動交易】策略檔儲存失敗: {e}")

    def _qt_save_state(self):
        """持倉狀態即時落地:程式重啟後不會忘記自己有持倉而重複進場。"""
        try:
            with open(self.QT_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.strategy_runtimes, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _qt_save_paper(self):
        try:
            with open(self.QT_PAPER_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.paper_accts, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _qt_paper_acct_for(self, strategy, warn=True):
        """【新ADR 多帳戶】取得策略設定的模擬帳戶 dict。帳戶被刪除或策略設定
        壞掉時,退回任一個現有帳戶——絕不能因為帳戶ID失效就讓成交記帳整個
        炸掉 (寧可記錯帳戶也不要漏記一筆真實成交)。warn=False 給
        _qt_refresh_tree 這類高頻率的純顯示呼叫用,避免同一個失效帳戶
        每次刷新畫面就洗一次系統日誌;實際成交路徑 (warn=True,預設) 才記警告。"""
        aid = strategy_engine.account_id_of(strategy)
        acct = self.paper_accts.get(aid)
        if acct is None:
            if self.paper_accts:
                acct = next(iter(self.paper_accts.values()))
                if warn:
                    self.log_message(f"【模擬帳戶】策略「{strategy.get('name')}」設定的帳戶"
                                    f"(id={aid}) 已不存在,暫時改記入「{acct.get('name', '?')}」,"
                                    f"請到策略編輯器重新選擇帳戶。")
            else:
                acct = paper_account.new_account(account_id=paper_account.DEFAULT_ACCOUNT_ID)
                self.paper_accts[acct['id']] = acct
        return acct

    def _qt_account_choices(self):
        """【新ADR 多帳戶】回傳 [(account_id, 帳戶名稱), ...],給策略編輯器與
        模擬帳戶視窗的帳戶下拉選單用。"""
        return [(aid, a.get('name', aid)) for aid, a in self.paper_accts.items()]

    def _qt_live_account_choices(self):
        """【ADR-110 階段2】實單可選的「券商 / 帳號」清單。

        回傳 [((broker_key, account_id), 顯示名稱), ...],第一項固定是
        「用券商預設帳號」= ('', '') —— 舊策略與沒特別指定的策略都落在這一項,
        行為與加這個功能之前完全相同。

        未登入時各家 adapter 的 list_accounts() 會回空 list,這時清單只剩
        預設那一項。這不是錯誤:使用者常在還沒登入時就打開策略編輯器。
        """
        out = [(('', ''), '(用券商預設帳號)')]
        for key, b in (getattr(self, 'brokers', None) or {}).items():
            blabel = getattr(b, 'display_name', None) or key
            try:
                accs = b.list_accounts()
            except Exception:
                accs = []
            for aid, aname in accs:
                out.append(((key, aid), f"{blabel} / {aname}"))
        return out

    def _qt_build_live_account_row(self, parent, s):
        """在策略編輯器裡加一列「實單帳戶」。回傳讀取目前選擇的函式。

        三個編輯器 (一般/自訂/終極波段) 共用這一個建構函式,不各寫一份——
        下單目標的選法若三處分歧,使用者會在不同編輯器看到不同行為,
        而這個欄位錯了就是真錢下錯戶頭。
        """
        fr = tk.Frame(parent, bg="#1A2026")
        fr.pack(fill=tk.X, padx=12, pady=(2, 0))
        tk.Label(fr, text="實單帳戶", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        choices = self._qt_live_account_choices()
        refs = [r for r, _lab in choices]
        cb = ttk.Combobox(fr, width=26, state='readonly', style="BlackText.TCombobox",
                          values=[lab for _r, lab in choices])
        cur = ((order_intent.broker_of(s), order_intent.account_of(s) or '')
               if order_intent.account_of(s) else ('', ''))
        cb.current(refs.index(cur) if cur in refs else 0)
        cb.pack(side=tk.LEFT, padx=6)
        # 設定過但目前找不到 (換帳號/未登入) 一定要講出來,不能靜靜地退回
        # 預設帳號讓使用者以為設定還在。
        warn = ''
        if order_intent.account_of(s) and cur not in refs:
            warn = f"⚠ 原設定 {order_intent.account_of(s)} 目前不在清單中 (未登入或帳號已變更)"
        tk.Label(fr, text=warn or "(只在「模式=實單」時有意義;未登入時只會有預設項)",
                 bg="#1A2026", fg=("#FFA726" if warn else "#8A99AD"),
                 font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=(6, 0))

        def _get():
            i = cb.current()
            return refs[i] if 0 <= i < len(refs) else ('', '')
        return _get

    def _qt_runtime(self, sid):
        if sid not in self.strategy_runtimes:
            self.strategy_runtimes[sid] = strategy_engine.new_runtime()
        return self.strategy_runtimes[sid]

    # ---------- 清單畫面 ----------
    def _qt_refresh_tree(self):
        try:
            prepared = []
            for s in self.strategies:
                rt = self._qt_runtime(s['id'])
                if s.get('kind') == 'custom':
                    conds = "🐍 自訂 Python (on_bar)"
                elif s.get('kind') == chukuangren_band.KIND:
                    conds = (f"🎯 終極波段策略 (看{strategy_engine.watch_symbol_of(s)},"
                            f"X={s.get('ck_x', 0):g})")
                else:
                    conds = "; ".join(strategy_engine.condition_label(c) for c in s.get('entry', [])) or '--'
                pos = '--'
                unreal_pnl_str = '--'
                is_mismatch = False
                if rt.get('state') in ('LONG', 'SHORT'):
                    pos = f"{'多' if rt['state']=='LONG' else '空'} {rt.get('qty',0)} @ {rt.get('entry_price',0):g}"
                    sym = s.get('symbol', '')
                    acct = self._qt_paper_acct_for(s, warn=False)
                    p = acct['positions'].get(sym)
                    # 【新ADR 持倉核對】清單顯示的「持倉」原本完全信任 rt (策略自己
                    # 記錄的狀態),帳戶被重置/刪除過而跟 rt 對不上時,清單會很有
                    # 自信地顯示一筆帳戶裡其實不存在的部位——這裡改成先核對,不一致
                    # 就用醒目的方式標示出來,而不是悄悄顯示錯誤資料 (實單模式的
                    # 部位在券商端,不適用這項核對)。
                    mm = strategy_engine.position_mismatch(s, rt, p) if s.get('mode') != '實單' else None
                    if mm is not None:
                        is_mismatch = True
                        acct_txt = '無' if not mm['acct_direction'] else f"{mm['acct_direction']}{mm['acct_qty']}"
                        pos = f"⚠{pos}(帳戶:{acct_txt})"
                    elif p:
                        import core.paper_account as paper_account
                        d = 1.0 if rt['state'] == 'LONG' else -1.0
                        diff = (float(p.get('mark_price', rt['entry_price'])) - rt['entry_price']) * d
                        if s.get('market') == '台股':
                            u = diff * 1000 * rt.get('qty', 0)
                        else:
                            mult, _ = paper_account._fut_multiplier(sym)
                            u = diff * mult * rt.get('qty', 0)
                        unreal_pnl_str = _fmt_amt_signed(u)

                status = '啟用' if s.get('enabled') else '停用'
                # 【新增】「狀態」只反映策略本身有沒有勾啟用,不代表現在真的有沒有
                # 在跑——啟用的策略若碰上總開關(自動交易)還沒開,一樣不會被評估。
                # 這裡把「策略啟用」與「總開關是否運轉中」合併成一眼看得出的欄位。
                running = bool(s.get('enabled')) and bool(self._qt_running)
                running_disp = '🟢 運轉中' if running else '⏸ 停止'
                if is_mismatch:
                    tag = 'qt_mismatch'
                else:
                    tag = 'qt_off' if not s.get('enabled') else ('qt_on' if s.get('mode') == '實單' else 'qt_sim')
                tt = strategy_engine.trade_type_of(s)
                sym_disp = f"{s.get('symbol','')} ({tt})"
                sym_name = self._wl_display_name(s.get('symbol','')) if s.get('symbol','') else ''
                # 【ADR-045】自訂策略的方向由 on_bar 程式碼決定,direction 只是佔位
                if s.get('kind') == 'custom':
                    dir_disp = '程式決定'
                else:
                    dir_disp = s.get('direction', '')
                # 【修正】終極波段策略的訊號一律看日K收盤 (core/chukuangren_band.py
                # 內部寫死,不受 s['timeframe'] 影響——那個欄位對這個策略種類其實
                # 沒有作用,只是沿用 new_strategy() 的預設值殘留)。清單週期欄位
                # 顯示殘留的「5分K」會讓人誤以為策略是照5分K在判斷,容易誤解;
                # 固定顯示「日K」才符合實際行為。
                if s.get('kind') == chukuangren_band.KIND:
                    tf_disp = '日K'
                else:
                    tf_disp = s.get('timeframe', '')
                # 【新ADR 多帳戶】只有模擬模式才有意義 (實單直接下到真實券商帳戶)
                # 【ADR-110 階段2】實單策略原本這一欄固定顯示 '--',等於清單上
                # 看不出「這張單會下到哪一家券商、哪一個帳號」。多券商之後這是
                # 必要資訊,改成顯示實際的下單目標。
                acct_disp = (self._qt_paper_acct_for(s, warn=False).get('name', '')
                             if s.get('mode') != '實單' else self._qt_live_target_text(s))
                prepared.append((s['id'], (s.get('name',''), sym_disp, sym_name, tf_disp,
                                            dir_disp, conds, s.get('mode','模擬'), acct_disp, status, running_disp,
                                            rt.get('trades_today', 0), pos, unreal_pnl_str), tag))
            # 【ADR-057】更新「所有」存活的量化面板 (分頁 + 獨立視窗),
            # 並保留各自原本的選取項,免得清單一刷新使用者選的策略就跑掉。
            for ui in self._qt_alive_uis():
                tree = ui['tree']
                try:
                    keep = tree.focus() or ((tree.selection() or [None])[0])
                    for iid in tree.get_children():
                        tree.delete(iid)
                    for iid, vals, tag in prepared:
                        tree.insert("", tk.END, iid=iid, values=vals, tags=(tag,))
                    if keep and tree.exists(keep):
                        tree.selection_set(keep); tree.focus(keep)
                except Exception:
                    pass
        except Exception:
            pass

    def _qt_selected(self):
        """取目前選取的策略。【ADR-057】以「使用者實際在操作的面板」為準
        (獨立視窗開著就看視窗那份);該面板沒選才退回其他面板找。"""
        try:
            uis = self._qt_alive_uis()
            prim = self._qt_primary_ui()
            ordered = ([prim] + [u for u in uis if u is not prim]) if prim else uis
            for ui in ordered:
                try:
                    tree = ui['tree']
                    iid = tree.focus() or ((tree.selection() or [None])[0])
                    if iid:
                        s = next((x for x in self.strategies if x['id'] == iid), None)
                        if s:
                            return s
                except Exception:
                    continue
            return None
        except Exception:
            return None

    def _qt_update_status_label(self):
        try:
            if not self._qt_running:
                text, color, arm_state = "🔴 自動交易未啟動 (安全)", "#FF5252", tk.NORMAL
            else:
                live = any(s.get('enabled') and s.get('mode') == '實單' for s in self.strategies)
                if live:
                    text, color = "🔥 自動交易運轉中 — 含實單策略!", "#FF1744"
                else:
                    text, color = "🟢 自動交易運轉中 (全部模擬)", "#00E676"
                arm_state = tk.DISABLED
            for ui in self._qt_alive_uis():
                try:
                    ui['status'].config(text=text, fg=color)
                    ui['arm'].config(state=arm_state)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------- 啟停 ----------
    def _qt_open_arm_dialog(self):
        """啟動總開關:列出將被啟動的策略,要求打字「啟動」確認,防止誤觸。"""
        enabled = [s for s in self.strategies if s.get('enabled')]
        if not enabled:
            # 【ADR-045】這個提示原本只寫進系統日誌 (在別的分頁),使用者按了
            # 按鈕看起來毫無反應。改用 messagebox 直接告知原因與下一步。
            self.log_message("【自動交易】沒有任何「啟用」中的策略,請先新增/啟用策略再啟動總開關。")
            messagebox.showinfo(
                "尚無啟用中的策略",
                "目前沒有任何「啟用」中的策略,總開關啟動了也不會做任何事。\n\n"
                "請先:\n"
                "1. 按「➕ 新增策略」建立策略 (或選取既有策略)\n"
                "2. 在清單選取策略 → 按「▶ 啟用」\n"
                "3. 再回來按「🟢 啟動自動交易」\n\n"
                "說明:清單裡的「啟用/停用」是單一策略的開關;\n"
                "「🟢 啟動自動交易」是全體總開關 (每次開程式預設關閉,防誤觸);\n"
                "「⛔ 全部停止」= 急停,立刻關閉總開關 (不平倉)。", parent=self)
            return
        dlg = tk.Toplevel(self)
        dlg.title("啟動自動交易 — 最後確認")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 560, 360)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force(); dlg.grab_set()
        except Exception:
            pass
        live = [s for s in enabled if s.get('mode') == '實單']
        tk.Label(dlg, text="即將啟動以下策略:", bg="#1A2026", fg="#FFCA28",
                 font=('微軟正黑體', 11, 'bold')).pack(anchor='w', padx=14, pady=(12, 4))
        box = tk.Listbox(dlg, bg="#12161A", fg="white", height=7, font=('微軟正黑體', 10))
        box.pack(fill=tk.X, padx=14)
        for s in enabled:
            if s.get('kind') == 'custom':
                dir_disp = '程式決定'
            else:
                dir_disp = s.get('direction')
            # 【修正】跟策略清單一致:終極波段策略固定顯示「日K」,不要顯示
            # 對它其實沒有作用的 s['timeframe'] 殘留值 (見 _qt_refresh_tree 註解)。
            tf_disp = '日K' if s.get('kind') == chukuangren_band.KIND else s.get('timeframe')
            # 【ADR-110 階段2】實單策略要在這裡就看到下單目標 (券商/帳號)。
            # 這是使用者按下「確認啟動」前最後一次能發現「下錯帳戶」的機會。
            tgt = f" → {self._qt_live_target_text(s)}" if s.get('mode') == '實單' else ''
            box.insert(tk.END, f"[{s.get('mode')}] {s.get('name')} — {s.get('symbol')} {tf_disp} {dir_disp} x{s.get('qty')}{tgt}")
        if live:
            tk.Label(dlg, text=f"⚠ 注意:有 {len(live)} 個「實單」策略,啟動後將自動送出真實委託,無人工確認!",
                     bg="#1A2026", fg="#FF1744", font=('微軟正黑體', 10, 'bold')).pack(anchor='w', padx=14, pady=(8, 0))
        else:
            tk.Label(dlg, text="全部為模擬模式:只記錄訊號,不會送出任何真實委託。",
                     bg="#1A2026", fg="#00E676", font=('微軟正黑體', 10)).pack(anchor='w', padx=14, pady=(8, 0))
        row = tk.Frame(dlg, bg="#1A2026"); row.pack(anchor='w', padx=14, pady=(10, 0))
        tk.Label(row, text="請輸入「啟動」兩字以確認:", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 10)).pack(side=tk.LEFT)
        e_confirm = tk.Entry(row, width=10, bg="#2A323D", fg="white", justify="center", font=('微軟正黑體', 11))
        e_confirm.pack(side=tk.LEFT, padx=6)

        def _go():
            if e_confirm.get().strip() != "啟動":
                self.log_message("【自動交易】確認文字不符 (需輸入「啟動」),未啟動。")
                return
            self._qt_running = True
            self._qt_update_status_label()
            self._qt_refresh_tree()  # 策略清單的「運轉狀態」欄要立刻反映總開關已開
            self.log_message(f"【自動交易】✅ 總開關已啟動:{len(enabled)} 個策略運轉中 (實單 {len(live)} 個 / 模擬 {len(enabled)-len(live)} 個)。")
            dlg.destroy()
        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(pady=14)
        tk.Button(btns, text="確認啟動", bg="#00C853", fg="black", relief="flat",
                  font=('微軟正黑體', 11, 'bold'), padx=18, pady=4, command=_go).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 11), padx=18, pady=4, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _qt_stop_all(self):
        """⛔ 急停:立刻停止所有自動評估與下單 (不平倉,持倉狀態保留)。"""
        self._qt_running = False
        self._qt_update_status_label()
        self._qt_refresh_tree()  # 策略清單的「運轉狀態」欄要立刻反映總開關已關
        self.log_message("【自動交易】⛔ 全部停止:總開關已關閉,不再評估任何策略、不再自動下單。"
                         "既有持倉不會自動平倉,請自行決定是否手動處理。")

    # ---------- 策略 CRUD ----------
    def _qt_new_strategy(self):
        # 【ADR-040】先問要建「內建條件策略」還是「自訂 Python 策略」
        dlg = tk.Toplevel(self)
        dlg.title("新增策略 — 選擇類型")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 460, 260)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force(); dlg.grab_set()
        except Exception:
            pass
        tk.Label(dlg, text="要建立哪一種策略?", bg="#1A2026", fg="#FFCA28",
                 font=('微軟正黑體', 11, 'bold')).pack(pady=(16, 8))
        def _builtin():
            dlg.destroy(); self._qt_open_editor(None)
        def _custom():
            dlg.destroy(); self._qt_open_custom_editor(None)
        def _chukuangren():
            dlg.destroy(); self._qt_open_chukuangren_editor(None)
        tk.Button(dlg, text="🧩 內建條件策略 (下拉選條件,免寫程式)", bg="#29B6F6", fg="black",
                  relief="flat", font=('微軟正黑體', 10, 'bold'), padx=10, pady=6,
                  command=_builtin).pack(fill=tk.X, padx=30, pady=4)
        tk.Button(dlg, text="🐍 自訂 Python 策略 (自己寫 on_bar)", bg="#AB47BC", fg="white",
                  relief="flat", font=('微軟正黑體', 10, 'bold'), padx=10, pady=6,
                  command=_custom).pack(fill=tk.X, padx=30, pady=4)
        tk.Button(dlg, text="🎯 終極波段策略 (看大盤,做自選商品)", bg="#FF7043", fg="black",
                  relief="flat", font=('微軟正黑體', 10, 'bold'), padx=10, pady=6,
                  command=_chukuangren).pack(fill=tk.X, padx=30, pady=4)

    def _qt_edit_strategy(self):
        s = self._qt_selected()
        if not s:
            self.log_message("【自動交易】請先在清單中選取要編輯的策略。")
            return
        # 【ADR-125】「啟用中不可編輯」的規則收斂到 core,與刪除共用同一個判斷。
        readonly = strategy_engine.is_locked(s)
        if readonly:
            self.log_message(f"【自動交易】「{s.get('name')}」為啟動中，進入唯讀檢視模式。若要修改請先停用。")
        if s.get('kind') == 'custom':
            self._qt_open_custom_editor(s, readonly=readonly)
        elif s.get('kind') == chukuangren_band.KIND:
            self._qt_open_chukuangren_editor(s, readonly=readonly)
        else:
            self._qt_open_editor(s, readonly=readonly)

    def _qt_delete_strategy(self):
        s = self._qt_selected()
        if not s:
            self.log_message("【自動交易】請先在清單中選取要刪除的策略。")
            return
        rt = self._qt_runtime(s['id'])
        # 【ADR-125】原本只擋「仍有持倉」,**沒有擋「啟用中」** —— 使用者實測
        # 把一檔運轉中、但持倉是 FLAT 的策略直接刪掉了。規則移到 core 讓它
        # 只有一份,而且離線測得到。
        ok, reason = strategy_engine.can_delete(s, rt)
        if not ok:
            self.log_message(f"【自動交易】{reason}")
            return
        self.strategies = [x for x in self.strategies if x['id'] != s['id']]
        self.strategy_runtimes.pop(s['id'], None)
        self._qt_save(); self._qt_save_state(); self._qt_refresh_tree()
        self.log_message(f"【自動交易】已刪除策略「{s.get('name')}」。")

    def _qt_move_strategy(self, delta):
        """【新增】策略清單順序可自行上下移動 (self.strategies 的 list 順序
        本身就是清單顯示順序,純粹交換相鄰兩個元素即可)。"""
        s = self._qt_selected()
        if not s:
            self.log_message("【自動交易】請先在清單中選取要移動順序的策略。")
            return
        idx = next((i for i, x in enumerate(self.strategies) if x['id'] == s['id']), None)
        if idx is None:
            return
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(self.strategies):
            return
        self.strategies[idx], self.strategies[new_idx] = self.strategies[new_idx], self.strategies[idx]
        self._qt_save(); self._qt_refresh_tree()
        for ui in self._qt_alive_uis():
            try:
                ui['tree'].selection_set(s['id']); ui['tree'].focus(s['id'])
            except Exception:
                pass

    def _qt_enable_blockers(self, s):
        """【ADR-108】啟用某策略前的所有前置檢查,抽成一個共用方法。

        回傳 (ok, 原因字串, 持倉不一致資訊 tuple 或 None)。ok=False 代表
        設定本身不合格;ok=True 但 mismatch 不是 None 代表要先做持倉核對。

        抽出來的理由:遠端控制 (/on) 也必須跑完全一樣的檢查,不能因為走
        另一條路就繞過驗證——那等於留了一個「從手機啟用一個設定不完整或
        持倉對不上的策略」的破口。
        """
        if s.get('kind') == 'custom':
            if not s.get('source_code') or s.get('qty', 0) <= 0:
                return False, f"自訂策略「{s.get('name')}」設定不完整,無法啟用。", None
        elif s.get('kind') == chukuangren_band.KIND:
            ok, msg = chukuangren_band.validate(s)
            if not ok:
                return False, f"策略「{s.get('name')}」無法啟用: {msg}", None
        else:
            ok, msg = strategy_engine.validate_strategy(s)
            if not ok:
                return False, f"策略「{s.get('name')}」無法啟用: {msg}", None
        # 【新ADR 持倉核對】重新啟用前,比對策略記錄的持倉狀態跟它指定的
        # 模擬帳戶內實際部位是否一致——帳戶檔可能因為手動重置/刪除帳戶等
        # 操作跟策略以為的狀態不同步,不一致就先讓使用者確認/處理,不要
        # 悄悄地用錯誤的起點繼續運轉 (實單模式的部位在券商端,不是模擬
        # 帳戶,不適用這項核對)。
        if s.get('mode') != '實單':
            rt = self._qt_runtime(s['id'])
            acct = self._qt_paper_acct_for(s, warn=False)
            sym = str(s.get('symbol', '')).upper()
            mismatch = strategy_engine.position_mismatch(s, rt, acct['positions'].get(sym))
            if mismatch:
                return True, '', (rt, acct, mismatch)
        return True, '', None

    def _qt_set_enabled(self, flag):
        s = self._qt_selected()
        if not s:
            self.log_message("【自動交易】請先在清單中選取策略。")
            return
        if flag:
            ok, why, pending = self._qt_enable_blockers(s)
            if not ok:
                self.log_message(f"【自動交易】{why}")
                return
            if pending:
                rt, acct, mismatch = pending
                self._qt_open_reconcile_dialog(s, rt, acct, mismatch)
                return
        self._qt_finish_set_enabled(s, flag)

    def _qt_finish_set_enabled(self, s, flag):
        s['enabled'] = bool(flag)
        # 【ADR-121】兩個計數都歸零:手動重新啟用等於「重新開始算」。
        rt = self._qt_runtime(s['id']); rt['error_count'] = 0; rt['data_error_count'] = 0
        self._qt_save(); self._qt_save_state(); self._qt_refresh_tree(); self._qt_update_status_label()
        self.log_message(f"【自動交易】策略「{s.get('name')}」已{'啟用' if flag else '停用'}。")

    def _qt_open_reconcile_dialog(self, s, rt, acct, mismatch):
        """【新ADR 持倉核對】策略記錄的持倉跟模擬帳戶內實際部位對不上時的
        確認視窗:讓使用者選擇「以帳戶為準同步」「手動平倉」「手動加倉」或
        「忽略差異強制啟用」,而不是悄悄放行一個起點就錯的策略。"""
        sym = str(s.get('symbol', '')).upper()
        acct_pos = acct['positions'].get(sym)
        dlg = tk.Toplevel(self)
        dlg.title(f"持倉核對 — {s.get('name')}")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 480, 400)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force(); dlg.grab_set()
        except Exception:
            pass
        rt_txt = ('無持倉' if mismatch['rt_state'] == 'FLAT'
                  else f"{'多' if mismatch['rt_state']=='LONG' else '空'} {mismatch['rt_qty']}")
        acct_txt = ('無持倉' if not mismatch['acct_direction']
                    else f"{mismatch['acct_direction']} {mismatch['acct_qty']}")
        tk.Label(dlg, text=f"策略「{s.get('name')}」({sym}) 的持倉記錄\n"
                            f"跟模擬帳戶「{acct.get('name')}」內的實際部位對不上,\n"
                            f"請確認後再啟用:",
                 bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 10, 'bold'),
                 justify='left').pack(anchor='w', padx=14, pady=(14, 6))
        grid = tk.Frame(dlg, bg="#12161A"); grid.pack(fill=tk.X, padx=14, pady=4)
        tk.Label(grid, text="策略記錄:", bg="#12161A", fg="#8A99AD").grid(row=0, column=0, sticky='w', padx=8, pady=4)
        tk.Label(grid, text=rt_txt, bg="#12161A", fg="white", font=('微軟正黑體', 10, 'bold')).grid(row=0, column=1, sticky='w', padx=8)
        tk.Label(grid, text="帳戶實際:", bg="#12161A", fg="#8A99AD").grid(row=1, column=0, sticky='w', padx=8, pady=4)
        tk.Label(grid, text=acct_txt, bg="#12161A", fg="white", font=('微軟正黑體', 10, 'bold')).grid(row=1, column=1, sticky='w', padx=8)

        price_row = tk.Frame(dlg, bg="#1A2026"); price_row.pack(fill=tk.X, padx=14, pady=(10, 0))
        tk.Label(price_row, text="手動平倉/加倉的成交價:", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        if acct_pos:
            default_price = float(acct_pos.get('mark_price', acct_pos.get('avg_price', 0)) or 0)
        elif rt.get('entry_price'):
            default_price = float(rt.get('entry_price') or 0)
        else:
            default_price = 0.0
        e_price = tk.Entry(price_row, width=10, bg="#2A323D", fg="white", justify='center')
        e_price.insert(0, f"{default_price:g}")
        e_price.pack(side=tk.LEFT, padx=6)

        def _sync_rt_from_acct():
            if acct_pos:
                rt['state'] = 'LONG' if acct_pos.get('direction') == '多' else 'SHORT'
                rt['qty'] = int(acct_pos.get('qty', 0))
                rt['entry_price'] = float(acct_pos.get('avg_price', 0) or 0)
                rt['exec_entry_price'] = rt['entry_price']
            else:
                rt['state'] = 'FLAT'; rt['qty'] = 0; rt['entry_price'] = 0.0; rt['exec_entry_price'] = 0.0
            self.log_message(f"【自動交易】策略「{s.get('name')}」持倉記錄已同步為帳戶實際部位。")
            dlg.destroy()
            self._qt_finish_set_enabled(s, True)

        def _manual_close():
            try:
                price = float(e_price.get())
            except ValueError:
                messagebox.showwarning("格式錯誤", "請輸入正確的價格。", parent=dlg); return
            if acct_pos:
                action = '賣出' if acct_pos.get('direction') == '多' else '買進'
                paper_account.apply_fill(acct, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                          s.get('market', '台股'), sym, action, 'CLOSE',
                                          int(acct_pos.get('qty', 0)), price,
                                          trade_type=strategy_engine.trade_type_of(s))
                self._qt_save_paper()
            rt['state'] = 'FLAT'; rt['qty'] = 0; rt['entry_price'] = 0.0; rt['exec_entry_price'] = 0.0
            self.log_message(f"【自動交易】策略「{s.get('name')}」已手動平倉,帳戶與策略記錄同步為無持倉。")
            dlg.destroy()
            self._qt_finish_set_enabled(s, True)

        def _manual_open():
            try:
                price = float(e_price.get())
            except ValueError:
                messagebox.showwarning("格式錯誤", "請輸入正確的價格。", parent=dlg); return
            if mismatch['rt_state'] == 'FLAT':
                messagebox.showwarning("無法加倉", "策略記錄目前無持倉,沒有方向/數量可以補進帳戶。", parent=dlg); return
            want_dir = '多' if mismatch['rt_state'] == 'LONG' else '空'
            if acct_pos and acct_pos.get('direction') != want_dir:
                messagebox.showwarning("方向衝突", "帳戶內已有反方向部位,請先手動平倉再加倉。", parent=dlg); return
            action = '買進' if mismatch['rt_state'] == 'LONG' else '賣出'
            paper_account.apply_fill(acct, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                      s.get('market', '台股'), sym, action, 'OPEN',
                                      mismatch['rt_qty'], price,
                                      trade_type=strategy_engine.trade_type_of(s))
            self._qt_save_paper()
            self.log_message(f"【自動交易】策略「{s.get('name')}」已在帳戶內補一筆開倉,與策略記錄同步。")
            dlg.destroy()
            self._qt_finish_set_enabled(s, True)

        def _force():
            self.log_message(f"【自動交易】策略「{s.get('name')}」忽略持倉差異,強制啟用 (請自行留意)。")
            dlg.destroy()
            self._qt_finish_set_enabled(s, True)

        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(fill=tk.X, padx=14, pady=(16, 4))
        tk.Button(btns, text="✅ 以帳戶為準同步後啟用", bg="#00C853", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), command=_sync_rt_from_acct).pack(fill=tk.X, pady=2)
        tk.Button(btns, text="🔻 手動平倉 (帳戶與記錄都清空持倉)", bg="#FF7043", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), command=_manual_close).pack(fill=tk.X, pady=2)
        tk.Button(btns, text="🔺 手動加倉 (依策略記錄在帳戶補一筆開倉)", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), command=_manual_open).pack(fill=tk.X, pady=2)
        tk.Button(btns, text="⚠ 忽略差異,強制啟用", bg="#5A6472", fg="white", relief="flat",
                  font=('微軟正黑體', 9), command=_force).pack(fill=tk.X, pady=2)
        tk.Button(dlg, text="取消", bg="#2A323D", fg="white", relief="flat",
                  command=dlg.destroy).pack(pady=(4, 12))

    # ---------- Runner:背景評估與下單 ----------
    def _qt_resolve(self, strategy):
        """解析策略「執行商品 (B)」合約。回傳 (contract, asset_type) 或 (None, None)。"""
        sym = str(strategy.get('symbol', '')).upper()
        try:
            if strategy.get('market') == '台期貨':
                c = self._resolve_futures_contract(sym)
                return (c, 'future') if c is not None else (None, None)
            c = self.sj_api.Contracts.Stocks.get(sym)
            return (c, 'stock') if c is not None else (None, None)
        except Exception:
            return (None, None)

    def _qt_resolve_watch(self, strategy):
        """【ADR-074 看A做B】解析「訊號來源 (A)」合約。A 可為股票/期貨/指數
        (加權/櫃買)。回傳 (contract, asset_type, sym, market_tag)。未啟用看A做B
        時就等於執行商品 B。"""
        if not strategy_engine.watch_enabled(strategy):
            c, at = self._qt_resolve(strategy)
            return c, at, str(strategy.get('symbol', '')).upper(), str(strategy.get('market', '台股'))
        sym = strategy_engine.watch_symbol_of(strategy)
        wtt = strategy_engine.watch_trade_type_of(strategy)
        try:
            if wtt == '指數' or strategy_engine.looks_like_index_symbol(sym):
                s = sym.upper()
                if s in ('^TWOII', 'TWOII', 'OTC101', 'OTC001', 'IX0002', 'OTC'):
                    c = self.brokers['sinopac'].index_contract('OTC')   # 【ADR-114】
                else:  # 預設加權
                    c = self.brokers['sinopac'].index_contract('TSE')   # 【ADR-114】
                return (c, 'index_tw', sym, '台股') if c is not None else (None, None, sym, '台股')
            if wtt == '期貨':
                c = self._resolve_futures_contract(sym)
                return (c, 'future', sym, '台期貨') if c is not None else (None, None, sym, '台期貨')
            c = self.sj_api.Contracts.Stocks.get(sym)
            return (c, 'stock', sym, '台股') if c is not None else (None, None, sym, '台股')
        except Exception:
            return (None, None, sym, '台股')

    def _qt_fetch_closed_bars(self, strategy, contract, asset_type, tf=None, cache_sym=None,
                              cache_market=None, allow_blocking=False, drop_last=True):
        """
        取策略用的「已收盤」K棒:下載(或快取)原始分K → 依週期重採樣 →
        剔除最後一根 (可能未完成)。回傳 df (可能為空)。

        【ADR-074】tf / cache_sym / cache_market 可覆寫,用來抓「訊號來源 A」的
        K棒 (A 的週期、A 的代碼);不帶時就抓策略本身 (執行商品 B) 的設定。

        【ADR-122】allow_blocking:呼叫端是不是「自己一條執行緒、慢一點沒關係」。
        量化 runner 一律 False (阻塞會拖住即時停損,見 P-90) —— 大範圍請求改丟
        給背景預抓、當場拋 KBarsPending。策略試跑/回測那種本來就在自己的 worker
        執行緒上跑的呼叫端傳 True,直接在原地把分段下載做完,不必等下一輪。
        """
        tf = tf or strategy.get('timeframe', '5分K')
        days = self.QT_TF_DAYS.get(tf)
        if days is None:
            # 【新增 自訂週期】不在固定表裡的自訂 N分K/N時K:抓的天數跟既有
            # 預設週期的級距對齊,分鐘數越大要越多天原始資料才湊得出足夠根數
            # 給 MA/BB 等指標算——不求精確,只求「跟同量級的預設週期一樣堪用」。
            mins = strategy_engine.timeframe_minutes(tf) or 5
            if mins <= 15:
                days = 10
            elif mins <= 60:
                days = 30
            elif mins <= 240:
                days = 60
            else:
                days = 120
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        _sym = (cache_sym or str(strategy.get('symbol'))).upper()
        _mkt = cache_market or strategy.get('market')
        key = f"QT|{_mkt}|{_sym}|{tf}"
        # 【ADR-122 → ADR-127】日K 類的新鮮度**不用牆上時鐘判斷**。
        #
        # ADR-122 已經論證過:_qt_fetch_closed_bars 會丟掉最後一根,所以日K 類
        # 「已收盤」的集合只在新的一盤開始時才會多一根。當時用 30 分鐘 TTL 當
        # 「90% 的近似」,結果就是整天每 30 分鐘白抓一次 300 天的資料 + 推播一次
        # (使用者實測 09:20 與 09:55 各收到一則,間隔 35 分鐘正好是 TTL 過期後
        #  的下一個 5 分鐘評估點)。ADR-127 改成問對的問題:「這段期間有沒有跨過
        # 任何一次開盤?」沒有就一定拿到一樣的資料,不必重抓。
        #
        # 分K 類維持原本的 30 秒 TTL —— 那個資料每根K棒真的會變,拉長會漏訊號。
        if kbars_plan.is_min_timeframe(tf):
            raw, fresh = self._kbars_cache_get(key, start_dt)
        else:
            raw, fresh = self._kbars_cache_get(key, start_dt,
                                               fresh_check=self._qt_day_cache_fresh)
        if raw is None or not fresh:
            plan = kbars_plan.chunk_plan(tf, days)
            if plan is not None and (allow_blocking or self.QT_PREFETCH_SYNC):
                # 自己的執行緒:原地分段下載,睡多久都只影響自己
                stats = {}
                raw = self._download_kbars_chunked(
                    contract, start_dt, end_dt, chunk_days=plan['chunk_days'],
                    subsplit_days=plan['subsplit_days'], stats=stats)
                if raw is not None and not raw.empty and not stats.get('fail_segs'):
                    self._kbars_cache_put(key, start_dt, raw, tf)
            elif plan is not None:
                # 【ADR-122】大範圍請求不在 runner 執行緒裡做:分段要跑好幾個
                # 請求 + 段間節流,月K 的 17 段會阻塞十幾秒,而同一條迴圈上還
                # 有每 3 秒的即時停損檢查 (P-90)。丟給背景執行緒,這一輪先回
                # KBarsPending,ADR-121 的 boundary 還原機制會讓下一個 tick
                # 再來看一次快取。
                # 上一次預抓失敗的話先把它回報出去 (消費掉),再讓下一輪重開一條。
                # 順序很重要:先報再開,否則失敗會被新的一輪「進行中」蓋掉,
                # 變成無聲的無限重試。
                failed = self._qt_prefetch_take_error(key)
                if failed:
                    raise strategy_engine.KBarsUnavailable(failed)
                self._qt_start_kbars_prefetch(key, contract, start_dt, end_dt, tf, plan)
                raise strategy_engine.KBarsPending(
                    f"{_sym} {tf} 需分 {plan['segments']} 段補資料,背景進行中")
            else:
                # 小範圍 (1分K/5分K):維持 ADR-122 之前的單次下載,行為零改變,
                # 也不會為了一個小請求多開一條執行緒。
                try:
                    raw = self._download_kbars_raw(contract, start_dt, end_dt)
                except Exception as e:
                    # 【ADR-121】把「抓資料失敗」包成專屬型別,讓 _quant_eval_pass
                    # 分得出它跟「策略邏輯壞掉」——前者要重試、不可自動停用策略。
                    # 斷線類例外維持原樣直接上拋:那不是暫時性失敗,重試沒有意義,
                    # 而且上層要靠它觸發 _mark_session_dead。
                    if self._looks_like_session_dead(e):
                        raise
                    raise strategy_engine.KBarsUnavailable(
                        f"{type(e).__name__}: {e}") from e
                if raw is not None and not raw.empty:
                    self._kbars_cache_put(key, start_dt, raw, tf)
        if raw is None or raw.empty:
            return pd.DataFrame()
        df = self._resample_sj_df(raw, tf, asset_type=asset_type)
        if df is None or len(df) < 2:
            return pd.DataFrame()
        # 最後一根視為未完成 (盤中一定是,收盤後犧牲一根換取絕不用未完成K棒的保證)
        #
        # 【ADR-132】drop_last=False 是給「收盤後的盤勢判斷推播」用的:那條路徑
        # 在 13:30 收盤之後才跑,當天的日K 已經定案,丟掉它等於**永遠看不到
        # 今天的型態** —— 對「每天收盤後告訴我今天出現什麼」這個需求是致命的。
        # 下單路徑一律維持 True (預設值),絕不可以拿未完成K棒去產生委託。
        if drop_last:
            df = df.iloc[:-1]
        # 【ADR-101】策略若用到籌碼條件,在這裡把籌碼併成 Chip* 欄位。
        # 放在資料準備階段的好處:條件函式簽名不變,實盤與回測共用同一條路。
        if strategy_engine.strategy_uses_chips(strategy):
            df = self._qt_attach_chips(df, strategy, cache_sym=_sym, cache_market=_mkt)
        return df

    def _qt_attach_chips(self, df, strategy, cache_sym=None, cache_market=None):
        """【ADR-101】把本地籌碼併進策略用的 df。純讀本地檔,不觸發下載。

        籌碼要先由使用者在「籌碼」分頁按更新下載;這裡刻意不自動下載——
        自動交易的評估迴圈每 2 秒跑一次,在裡面發網路請求會拖垮迴圈,
        也違反「下載由使用者明確發動」的設計 (見 ADR-100)。
        缺資料時條件會拋 ChipDataMissing,由 eval_conditions 收進 errors,
        GUI 提示使用者去更新籌碼,不會靜默當成條件成立。
        """
        try:
            sym = str(cache_sym or strategy.get('symbol') or '').upper()
            start_iso = (df.index.min()).strftime('%Y-%m-%d')
            end_iso = (df.index.max()).strftime('%Y-%m-%d')
            trade_type = strategy_engine.trade_type_of(strategy)

            stock_chips = None
            if trade_type != '期貨':
                allsym = chips_store.load_stock_inst_range(self.CHIPS_BASE_DIR, start_iso, end_iso)
                if allsym is not None and not allsym.empty:
                    stock_chips = allsym[allsym['Code'].astype(str).str.upper() == sym]
            margin_chips = chips_store.load(chips_store.margin_path(self.CHIPS_BASE_DIR))
            futures_chips = None
            fut_product = None
            if trade_type == '期貨':
                futures_chips = chips_store.load_futures_inst_range(
                    self.CHIPS_BASE_DIR, start_iso, end_iso)
                fut_product = self.CHIPS_FUT_PRODUCT_MAP.get(sym[:3], '臺股期貨')
            return chips_features.attach_chips(
                df, stock_chips=stock_chips, margin_chips=margin_chips,
                futures_chips=futures_chips, futures_product=fut_product,
                allow_same_day=bool(strategy.get('chips_allow_same_day')))
        except Exception as e:
            self.safe_after(0, self.log_message,
                            f"【籌碼】併入策略資料時發生問題 ({type(e).__name__}: {e});"
                            "該策略的籌碼條件本次將視為不成立。")
            return df

    # shioaji 期貨代號前綴 → 期交所商品名稱 (籌碼表的 Product 欄用中文)
    CHIPS_FUT_PRODUCT_MAP = {
        'TXF': '臺股期貨', 'MXF': '小型臺指期貨', 'TMF': '微型臺指期貨',
        'EXF': '電子期貨', 'FXF': '金融期貨',
    }

    def _resolve_strategy_symbol_name(self, trade_type, symbol):
        """【ADR-043 第2項】依交易種類解析商品中文名稱,確認代碼有效。
        回傳 (name 或 None, ok)。股票/零股查 Stocks;期貨查期貨合約。"""
        sym = str(symbol).strip().upper()
        if not sym or not (self.api_logged_in and HAS_SJ and self.sj_api):
            return None, False
        try:
            if trade_type == '期貨':
                c = self._resolve_futures_contract(sym)
                if c is not None:
                    # 【ADR-046】名稱正規化 (shioaji MXF 家族名稱污染)
                    nm = fut_catalog.display_name(getattr(c, 'symbol', sym),
                                                  getattr(c, 'name', '') or '')
                    return (nm or getattr(c, 'symbol', sym)), True
                return None, False
            c = self.sj_api.Contracts.Stocks.get(sym)
            if c is not None:
                return (getattr(c, 'name', '') or sym), True
            return None, False
        except Exception:
            return None, False

    def _place_strategy_order(self, strategy, intent, contract, asset_type, exec_price=None):
        """
        實單下單:鏡射 execute_order 的組單參數 (股票=現股整股,期貨=無條件單),
        委託方式依 strategy['price_type'] (限價/市價/範圍市價,新ADR)。
        限價才會套用 slippage_ticks 讓價;市價/範圍市價 價格送 0,依市場當下成交。

        【ADR-075 看A做B】exec_price:實際「要下單的商品 B」的成交基準價;有帶
        就用它 (看A做B),否則沿用 intent['price'] (一般模式 = 訊號商品自己的價)。
        contract/asset_type 一律是 B (執行商品),tick/讓價都以 B 計。

        【鐵則6】零股一律強制限價 (price_type_of 已做防呆退回限價),不因這裡
        新增的委託方式選項而放寬,交易所規則不可繞過。

        【ADR-110 階段1】讓價/檔位/限市價的判斷搬到 core/order_intent.py,
        委託物件的組裝搬到 brokers/<券商>.py;這裡只負責「選一家券商、送出、
        回報結果」。這樣同一個策略要改送別家券商時,改的是它綁定的 adapter,
        不是再抄一份下單邏輯。
        """
        try:
            oi = order_intent.build_live_order(strategy, intent, asset_type,
                                               exec_price=exec_price)
            broker = self._broker_for(strategy)
            if broker is None:
                return False, f"找不到策略指定的券商連線 ({self._broker_key_of(strategy)})"
            trade = broker.place_order(contract, oi)
            unit = strategy_engine.qty_unit_of(strategy)
            return True, (f"{oi['price_label']} x{oi['qty']}{unit} ({oi['trade_type']}) "
                          f"已送出 (狀態 {broker.order_status_text(trade)})")
        except broker_ipc.OrderOutcomeUnknown as e:
            # 【ADR-112】委託送進子行程但沒收到回應 —— **不知道**券商收到沒有。
            # 回 False 會讓策略維持 FLAT、下一根 K 棒再進場一次 (可能重複下單);
            # 回 True 會記一個可能不存在的部位。兩種猜法都會賠錢,所以第三條路:
            # 停掉這個策略,要求人工到券商端確認。
            self._qt_halt_strategy_unknown(strategy, str(e))
            return False, f"⚠️ 結果不確定,已自動停用此策略: {e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _qt_halt_strategy_unknown(self, strategy, detail):
        """下單結果不確定時,把策略停掉並大聲告知 (ADR-112)。

        自動停用是刻意的:在人確認券商端狀態之前,這個策略的持倉認知已經
        不可信,讓它繼續跑只會把錯誤放大。停用比「猜一個狀態繼續跑」安全。
        """
        try:
            self.safe_after(0, self._qt_finish_set_enabled, strategy, False)
        except Exception:
            strategy['enabled'] = False
        self.safe_after(0, self.log_message,
                        f"【自動交易-結果不明】🚨 策略「{strategy.get('name')}」的委託"
                        f"送出後失去回應,已自動停用。{detail}")

    # ------------------------------------------------------------------
    # 【ADR-110 階段2】self.sj_api 改成 self.brokers['sinopac'].api 的別名
    #
    # 【為什麼要改】ADR-097 階段0 把連線搬進 adapter 之後,系統裡同時存在
    # 兩個指向同一個 shioaji 實例的名字。它們**現在**是同步的,但那只是因為
    # 兩處賦值剛好都有一起更新——只要有人 (包含診斷腳本) 只改其中一個,
    # 就會出現「查詢用 A 連線、下單用 B 連線」這種極難查的錯誤。
    # 實際上這在 ADR-110 階段1 就發生過:診斷腳本設了 app.sj_api,下單卻走
    # adapter,委託送到另一個物件去,測試才抓到。
    #
    # 用 property 讓兩者共用同一份儲存,divergence 從「要小心避免」變成
    # 「不可能發生」。對外行為完全不變:讀寫 self.sj_api 的上百處都照舊。
    # ------------------------------------------------------------------
    @property
    def sj_api(self):
        b = (getattr(self, 'brokers', None) or {}).get('sinopac')
        if b is not None:
            return b.api
        return self.__dict__.get('_sj_api_nobroker')

    @sj_api.setter
    def sj_api(self, value):
        b = (getattr(self, 'brokers', None) or {}).get('sinopac')
        if b is not None:
            b.api = value
        else:
            # 沒安裝 shioaji (HAS_SJ=False) 時沒有 adapter 可放,退回自己存,
            # 讓既有「未安裝也不能當掉」的行為維持不變。
            self.__dict__['_sj_api_nobroker'] = value

    def _broker_key_of(self, strategy):
        """策略要用哪一家券商下單 (判斷邏輯在 core,這裡只是轉呼叫)。"""
        return order_intent.broker_of(strategy)

    def _broker_for(self, strategy):
        """取得策略綁定的券商 adapter;找不到回 None (呼叫端負責報錯)。"""
        return (getattr(self, 'brokers', None) or {}).get(self._broker_key_of(strategy))

    def _broker_label(self, broker_key):
        b = (getattr(self, 'brokers', None) or {}).get(broker_key)
        return getattr(b, 'display_name', None) or broker_key

    def _qt_live_target_text(self, strategy):
        """【ADR-110 階段2】「這個策略的實單會下到哪裡」——券商 + 帳號的人話。

        多券商之後,下錯帳戶是全新的風險:策略設定改一個字,單就送到別家去,
        而且是真錢。所以凡是會讓實單開始運作的畫面 (策略清單、啟動總開關的
        確認視窗) 都要顯示這一行,不能只有下單當下才看得到。
        """
        bkey = self._broker_key_of(strategy)
        acc_id = order_intent.account_of(strategy)
        acc_label = None
        if acc_id:
            broker = (getattr(self, 'brokers', None) or {}).get(bkey)
            if broker is not None:
                acc_label = dict(broker.list_accounts()).get(acc_id)
            # 找不到對應帳號時要看得出來是「設定了但現在找不到」,
            # 不可顯示成「預設帳號」讓使用者以為一切正常。
            acc_label = acc_label or f"{acc_id} ⚠找不到"
        return order_intent.describe_target(strategy, self._broker_label(bkey), acc_label)

    def _qt_log_session_closed(self, s, trade_type, include_night):
        """【ADR-070】策略因休市被跳過時記錄——但只在「開盤→休市」轉換那一刻記
        一次,不要每 2 秒洗版。用 _qt_session_state 記住每檔上次的開/收狀態。"""
        if not hasattr(self, '_qt_session_state'):
            self._qt_session_state = {}
        prev = self._qt_session_state.get(s['id'])
        if prev != 'closed':
            self._qt_session_state[s['id']] = 'closed'
            # prev 為 None 代表 runner 剛啟動就處於休市:也提示一次,讓使用者知道
            # 「現在非交易時間,策略待命中,開盤會自動接手」,不是程式沒反應。
            self.safe_after(0, self.log_message,
                            f"【自動交易-待命】策略「{s.get('name')}」目前非交易時間 "
                            f"({trade_type}{'含夜盤' if (trade_type == '期貨' and include_night) else ''}),"
                            f"暫不評估;進入交易時段會自動開始運作,無需人工介入。")

    def _qt_note_session_open(self, s, trade_type, include_night):
        """【ADR-070】策略從休市進入盤中時記一次,讓使用者看到「已自動接手」。"""
        if not hasattr(self, '_qt_session_state'):
            self._qt_session_state = {}
        prev = self._qt_session_state.get(s['id'])
        if prev == 'closed':
            label = market_session.session_label(trade_type, include_night=include_night)
            self.safe_after(0, self.log_message,
                            f"【自動交易-開盤】策略「{s.get('name')}」已進入交易時段 ({label}),"
                            f"開始自動評估與下單。")
        self._qt_session_state[s['id']] = 'open'

    # ------------------------------------------------------------------
    # 【ADR-121】開盤暖機 + 抓資料失敗的重試/記錄
    # ------------------------------------------------------------------
    QT_OPEN_WARMUP_SEC = market_session.QT_OPEN_WARMUP_SEC   # 開盤後幾秒內不抓K線
    QT_KBARS_MAX_ATTEMPTS = 3                                # 同一根K棒最多試幾次

    # 【ADR-122】策略路徑的日K 類快取新鮮度。
    #
    # 為什麼可以放這麼長 (相對於主圖的 CACHE_TTL_DAY_TF=300):
    # _qt_fetch_closed_bars 會**丟掉最後一根**視為未完成。對日K 策略而言,
    # 盤中整段時間「最後一根」都是今天,丟掉之後**評估用的資料從開盤到收盤
    # 完全不變** —— 要等到隔天第一根日K 出現,評估集合才會多一根。
    # 舊值 300 秒 < 日K 類 10 分鐘的評估間隔,等於「快取必定過期、完全沒作用」,
    # 每次評估都重打一次 300 天的大請求。
    # 分K 類不適用這個推論 (資料每根K棒真的會變),維持 CACHE_TTL_MIN_TF。
    QT_CACHE_TTL_DAY_TF = 1800   # 【ADR-127 起不再用於策略路徑】保留給其他呼叫端/相容
    QT_CACHE_MAX_AGE_SEC = 24 * 3600   # 【ADR-127】日K 類快取的絕對上限 (週末保險)

    # 【ADR-122 測試接縫】True = 大範圍分段下載改成「原地做完」而不是丟背景
    # 執行緒。production 永遠是 False;診斷腳本設 True,好讓既有的同步斷言
    # (呼叫一次 _quant_eval_pass 就檢查結果) continue to work。
    # 同 CLOSE_FORCE_EXIT 的做法 (P-74):會改變時序的機制一定要留一個明確的
    # 測試開關,不要讓測試去猜執行緒什麼時候跑完。
    QT_PREFETCH_SYNC = False
    # 預抓執行緒的分段下載參數。跑在自己的執行緒上,所以退避重試照用
    # (睡多久都只影響它自己);拉出來當常數是為了讓診斷能調快。
    QT_PREFETCH_RETRIES = 2
    QT_PREFETCH_PACE_SEC = 0.35

    def _qt_day_cache_fresh(self, cached_ts):
        """【ADR-127】日K 類快取還新鮮嗎:期間內沒有跨過任何開盤就算新鮮。

        另外加一道**絕對上限** (QT_CACHE_MAX_AGE_SEC) 當保險:週末沒有開盤,
        單靠 session 規則可以讓快取活過整個週末;雖然那期間確實不會有新的
        日K,但「記憶體裡的資料放了三天沒重新確認過」風險報酬不划算,寧可
        週一早上多抓一次。
        """
        try:
            age = time.time() - float(cached_ts)
        except (TypeError, ValueError):
            return False
        if age < 0 or age > self.QT_CACHE_MAX_AGE_SEC:
            return False
        t0 = datetime.fromtimestamp(float(cached_ts))
        return not market_session.any_session_opens_between(t0, datetime.now())

    def _qt_prefetch_init(self):
        """三個欄位各自獨立檢查,不可以用「第一個沒有就三個一起建」的寫法。

        那種寫法只要有人單獨重設其中一個 (診斷腳本重設 _qt_prefetch_inflight
        就踩到了),剩下兩個就永遠不會被建立,拿到的是 AttributeError。
        """
        if not hasattr(self, '_qt_prefetch_lock'):
            self._qt_prefetch_lock = threading.Lock()
        if not hasattr(self, '_qt_prefetch_inflight'):
            self._qt_prefetch_inflight = set()
        if not hasattr(self, '_qt_prefetch_errors'):
            self._qt_prefetch_errors = {}

    def _qt_prefetch_take_error(self, key):
        """【ADR-122】取出並清掉某個 key 上一次預抓的失敗訊息 (只回報一次)。

        清掉是刻意的:回報過之後就讓下一輪重新開一條預抓去試。若留著不清,
        同一則失敗會在每個 tick 重複回報;若不回報只重開,失敗就會完全無聲。
        """
        self._qt_prefetch_init()
        with self._qt_prefetch_lock:
            return self._qt_prefetch_errors.pop(key, None)

    def _qt_start_kbars_prefetch(self, key, contract, start_dt, end_dt, tf, plan):
        """【ADR-122】在背景執行緒分段補一份大範圍K線,補完寫進 _kbars_raw_cache。

        為什麼要另開執行緒:分段要跑好幾個請求 + 段間節流,月K 的 17 段光節流
        就 5.6 秒。量化 runner 是單執行緒,同一條迴圈上還有每 3 秒的
        _qt_update_realtime_pnl (期貨即時停損停利靠它) —— 在那裡阻塞十幾秒
        正是 P-90 說不可以做的事。搬到自己的執行緒上之後,分段下載原本的
        退避重試 (retries=2) 也可以照用,睡多久都只影響它自己。

        同一個 key 只會有一條在跑 (_qt_prefetch_inflight)。中止條件不用自己寫:
        _download_kbars_chunked 每段開始前都會問 _downloads_should_abort()
        (涵蓋關閉程式/登出/強制終止,ADR-060)。
        """
        self._qt_prefetch_init()
        with self._qt_prefetch_lock:
            if key in self._qt_prefetch_inflight:
                return False
            self._qt_prefetch_inflight.add(key)

        # 【ADR-127】同一個 key 每天只通知一次。這則訊息的前綴會被
        # telegram_notify.is_quant_message() 抓到 → 每次都推播,而 ADR-122 的
        # 30 分鐘 TTL 讓它整天每半小時響一次 (使用者實測 09:20 / 09:55)。
        # 資訊價值只有「第一次補資料要等一下」,重複的純粹是噪音。
        _today = f"{datetime.now():%Y-%m-%d}"
        if not hasattr(self, '_qt_prefetch_noted'):
            self._qt_prefetch_noted = {}
        if self._qt_prefetch_noted.get(key) != _today:
            self._qt_prefetch_noted[key] = _today
            self.safe_after(0, self.log_message,
                            f"【自動交易-資料】{key.split('|')[-2]} {tf} 歷史資料分 "
                            f"{plan['segments']} 段在背景補齊中,補完後策略會自動開始評估。")

        def _worker():
            try:
                stats = {}
                raw = self._download_kbars_chunked(
                    contract, start_dt, end_dt,
                    chunk_days=plan['chunk_days'], subsplit_days=plan['subsplit_days'],
                    retries=self.QT_PREFETCH_RETRIES, pace_sec=self.QT_PREFETCH_PACE_SEC,
                    stats=stats)
                # 【重要】部分成功不可以進快取:缺的若是**最近**那一段,策略會
                # 拿過期資料去評估並據此下單。寧可下一輪重來,也不要拿殘缺資料
                # 冒充完整 (同鐵則4「沒有真實資料就不要捏造」的精神)。
                if raw is None or raw.empty or stats.get('fail_segs') or stats.get('aborted'):
                    self._qt_prefetch_errors[key] = (
                        f"{tf} 分段補資料未完成 (成功 {stats.get('ok_segs', 0)} 段 / "
                        f"失敗 {stats.get('fail_segs', 0)} 段)")
                    return
                self._kbars_cache_put(key, start_dt, raw, tf)
                self._qt_prefetch_errors.pop(key, None)
            except Exception as e:
                self._qt_prefetch_errors[key] = f"{type(e).__name__}: {e}"
            finally:
                with self._qt_prefetch_lock:
                    self._qt_prefetch_inflight.discard(key)

        t = threading.Thread(target=_worker, daemon=True)
        self._qt_prefetch_thread = t      # 診斷用:可以 join 等它跑完
        t.start()
        return True

    def _qt_log_open_warmup(self, s, trade_type, include_night):
        """開盤暖機只記一次 (每檔策略每次開盤),不要每 2 秒洗版。"""
        if not hasattr(self, '_qt_warmup_noted'):
            self._qt_warmup_noted = {}
        label = market_session.session_label(trade_type, include_night=include_night)
        key = f"{datetime.now():%Y-%m-%d}|{label}"
        if self._qt_warmup_noted.get(s['id']) == key:
            return
        self._qt_warmup_noted[s['id']] = key
        self.safe_after(0, self.log_message,
                        f"【自動交易-開盤】策略「{s.get('name')}」({label}) 先等 "
                        f"{self.QT_OPEN_WARMUP_SEC} 秒避開開盤尖峰再開始抓K線 —— "
                        f"開盤後第一根「已收盤」K棒本來就還沒生出來,不影響訊號。")

    def _qt_on_kbars_fetch_error(self, s, rt, exc, boundary_key, prev_boundary):
        """【ADR-121】抓 K 線失敗:先重試,重試用完才記錄;永不自動停用策略。

        【為什麼重試不用 time.sleep()】量化 runner 是單執行緒,原地睡會一併
        拖住其他策略與 _qt_update_realtime_pnl (每 3 秒,期貨即時停損停利靠
        它)。主圖那條路可以睡 (它自己一條背景執行緒),這裡不行 —— 拿一個
        錯誤訊息換掉即時停損,是更糟的交易。

        改用 runner 自己的 2 秒節奏當退避:把 boundary 還原,下一個 tick 就會
        再評估同一根K棒。三次都失敗才記錄,並把 boundary 寫回去停止重試
        (不可以無限每 2 秒重打券商 API)。

        【為什麼不自動停用】抓不到資料時策略本來就不會產生任何 intent,停用
        它得不到任何保護;但停用會讓 _quant_eval_pass 與
        _qt_check_realtime_futures_stops 都跳過這檔策略 —— 部位還在,停損卻
        不跑了。網路瞬斷不該有這種後果。
        """
        sid = s['id']

        # 【ADR-122】「資料還在背景補」不是錯誤,是預期中的等待:不計任何
        # 錯誤、不記日誌 (啟動預抓時已經記過一行),單純把 boundary 還原,
        # 讓下一個 tick 再看一次快取。這裡刻意**不進重試計數** —— 分段補
        # 一份月K 要好幾十秒,用 3 次上限去卡它會讓它永遠補不完。
        if isinstance(exc, strategy_engine.KBarsPending):
            if boundary_key is not None:
                if prev_boundary is None:
                    self._qt_last_boundary.pop(sid, None)
                else:
                    self._qt_last_boundary[sid] = prev_boundary
            return

        if not hasattr(self, '_qt_fetch_attempts'):
            self._qt_fetch_attempts = {}
        prev_key, n = self._qt_fetch_attempts.get(sid, (None, 0))
        n = (n + 1) if prev_key == boundary_key else 1
        self._qt_fetch_attempts[sid] = (boundary_key, n)

        if boundary_key is not None and n < self.QT_KBARS_MAX_ATTEMPTS:
            # 還原 boundary → 下一個 runner tick (2 秒後) 重試同一根K棒
            if prev_boundary is None:
                self._qt_last_boundary.pop(sid, None)
            else:
                self._qt_last_boundary[sid] = prev_boundary
            return

        rt['data_error_count'] = int(rt.get('data_error_count', 0)) + 1
        self.safe_after(0, self.log_message,
                        f"【自動交易-資料異常】策略「{s.get('name')}」連 {n} 次抓不到K線: {exc}"
                        f" —— 已自動重試,策略維持啟用 (抓不到資料時本來就不會下單)。")
        self._qt_log_kbars_usage_once()
        if rt['data_error_count'] % 3 == 0:
            self.safe_after(0, self.log_message,
                            f"【自動交易-資料異常】⚠ 策略「{s.get('name')}」已累計 "
                            f"{rt['data_error_count']} 次抓不到K線。若持續發生,請確認網路與"
                            f"券商 API 流量 (上方日誌會附流量數字)。")

    def _qt_log_kbars_usage_once(self):
        """【ADR-121】抓資料失敗時把 API 流量寫進日誌,每天只印一次。

        沿用主圖 _download_kbars_chunked._log_usage_once 的做法。沒有這個數字,
        「券商端暫時性管制」與「當日流量耗盡」在日誌上長得一模一樣 —— 這正是
        使用者回報 kbars 逾時時無從判斷的原因。
        """
        today = f"{datetime.now():%Y-%m-%d}"
        if getattr(self, '_qt_usage_logged_date', None) == today:
            return
        self._qt_usage_logged_date = today
        try:
            u = self.sj_api.usage()
            used = getattr(u, 'bytes', None)
            limit = getattr(u, 'limit_bytes', None)
            remain = getattr(u, 'remaining_bytes', None)
            if used is not None:
                self.safe_after(0, self.log_message,
                                f"【自動交易-診斷】API 流量:已用 {used/1048576:.1f}MB / 上限 "
                                f"{(limit or 0)/1048576:.0f}MB / 剩餘 {(remain or 0)/1048576:.1f}MB "
                                f"(剩餘充足=暫時性管制,稍後自行恢復;趨近 0=今日流量耗盡)。")
        except Exception:
            pass

    def _quant_eval_pass(self, now_ts=None, today_str=None):
        """跑一輪全部策略的評估 (抽成獨立方法方便離線測試)。在背景執行緒執行。
        【ADR-041】明確傳入 now_ts/today_str (測試/手動觸發) 時跳過邊界閘門強制評估;
        runner 自然輪詢 (不帶參數) 才做 K棒邊界感知。"""
        import time as _time
        _forced = (now_ts is not None) or (today_str is not None)
        if now_ts is None:
            now_ts = _time.time()
        if today_str is None:
            today_str = datetime.now().strftime('%Y-%m-%d')
        if not self._qt_running:
            return
        if not (self.api_logged_in and HAS_SJ and self.sj_api):
            return
        changed = False
        if not hasattr(self, '_qt_last_boundary'):
            self._qt_last_boundary = {}
        for s in list(self.strategies):
            if not s.get('enabled'):
                continue
            rt = self._qt_runtime(s['id'])
            # 【ADR-070】交易時段閘門:非交易時間就完全不評估這檔策略,交易時間才
            # 自己運作。手動觸發 (_forced) 或這檔明確關閉閘門 (session_gate=False)
            # 才略過此檢查。期貨可依 futures_session ('day'/'day_night') 決定夜盤要
            # 不要做。收盤→開盤是自然銜接:runner 每 2 秒醒著,時鐘一進盤中,這裡
            # 就會放行,不需要任何人工重開;總開關 _qt_running 全程維持不動。
            if not _forced:
                tt = strategy_engine.trade_type_of(s)
                # 【ADR-124】改走共用判斷:終極波段這類「結構上只做日盤」的種類
                # 會覆寫存檔值,而且評估迴圈與即時停損從此用的是同一套規則。
                include_night = strategy_engine.include_night_of(s)
                if s.get('session_gate', True):
                    if not market_session.is_market_open(tt, include_night=include_night):
                        self._qt_log_session_closed(s, tt, include_night)
                        continue
                    else:
                        self._qt_note_session_open(s, tt, include_night)
                # 【ADR-121】開盤暖機:閘門剛打開的那幾秒不要去要 K 線。
                # 使用者實測 08:45/09:00/15:00 各噴一次 kbars 逾時,而那正是本檔
                # 策略當天第一次評估的時刻 —— 開盤瞬間全市場的用戶端同時打
                # 同一支 API。這裡刻意放在 _qt_last_boundary 被寫入**之前**,
                # 所以這根K棒沒有被吃掉,暖機結束後同一根照樣會評估到。
                #
                # 【ADR-123】暖機**不可以**巢狀在 session_gate 裡面 (原本是)。
                # 暖機解決的是「別在開盤鐘響那一秒去打券商 API」,跟使用者想不想
                # 要時段閘門是兩件事;而且關掉閘門的策略是 24 小時都在評估的,
                # 撞上開盤瞬間的機率反而更高,結果卻完全沒有保護。
                # 休市時 just_opened 本來就回 False,所以其餘時間行為不變。
                if market_session.just_opened(tt, include_night=include_night,
                                              warmup_sec=self.QT_OPEN_WARMUP_SEC):
                    self._qt_log_open_warmup(s, tt, include_night)
                    continue
            # 【ADR-041】邊界感知:只有該策略週期的K棒剛收盤才評估 (測試/手動觸發不受限)
            # 【ADR-074】看A做B 時,訊號來自 A,節奏依 A 的週期 (watch_timeframe)。
            # 【ADR-121】boundary_key / _prev_boundary 要讓下面的 except 看得到:
            # 抓資料失敗時要能把 boundary 還原,讓下一個 runner tick 重試同一根K棒。
            boundary_key = None
            _prev_boundary = None
            if not _forced:
                # 【新ADR 自訂週期】改用 strategy_engine.timeframe_minutes()
                # (涵蓋任意正整數的自訂 N分K/N時K,不再只認 1/5/15/30/60 這
                # 5個固定值)。邊界改算「距當日0點的總分鐘數」取模,而不是
                # 只對 now_dt.minute (0-59) 取模——後者只在週期<=60分鐘且
                # 整除60時才對,自訂的時K (例如3時K=180分鐘) 用分鐘取模永遠
                # 只會對齊到整點,對不齊真正的3小時邊界。這個算法對既有的
                # 1/5/15/30/60分K算出來的邊界時間跟原本完全一樣 (60分鐘以內
                # 的整除週期,「距0點總分鐘數取模」等於「該小時內取模」),
                # 只是同時把大於60分鐘、或不能整除60的自訂週期也算對。
                tf_mins = strategy_engine.timeframe_minutes(strategy_engine.watch_timeframe_of(s))
                now_dt = datetime.now()
                if tf_mins:
                    midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                    elapsed_min = int((now_dt - midnight).total_seconds() // 60)
                    boundary = midnight + timedelta(minutes=(elapsed_min // tf_mins) * tf_mins)
                    boundary_key = str(boundary)
                    # 給資料源 2 秒緩衝再抓,避免最後一根還沒生出來
                    if (now_dt - boundary).total_seconds() < 2:
                        continue
                else:
                    # 日K等長週期:每 10 分鐘檢查一次即可 (日K訊號不需要秒級延遲)
                    boundary = now_dt.replace(second=0, microsecond=0)
                    boundary -= timedelta(minutes=boundary.minute % 10)
                    boundary_key = str(boundary)
                if self._qt_last_boundary.get(s['id']) == boundary_key:
                    continue
                _prev_boundary = self._qt_last_boundary.get(s['id'])
                self._qt_last_boundary[s['id']] = boundary_key
            try:
                # 【ADR-074 看A做B】B (執行商品):下單、損益都用它;A (訊號來源):
                # 條件/指標看它 (可為指數;看A做B 關閉時 A=B)。
                contract, asset_type = self._qt_resolve(s)
                if contract is None:
                    raise RuntimeError(f"執行商品(做B)合約解析失敗: {s.get('symbol')}")

                if s.get('kind') == chukuangren_band.KIND:
                    # 【新ADR 終極波段策略】獨立分派:A固定用日K做突破/停損/停利
                    # 訊號判斷。
                    # 【新ADR 確認/下單分兩個時間點】確認成立後不會在這裡立刻下單,
                    # 而是記進 rt['armed_intent'],真正送單延後約60秒 (12:01) 由
                    # _qt_chukuangren_execute_pass() 獨立檢查執行。intents 這裡
                    # 永遠是空的,下面共用的intent處理迴圈對終極波段策略是no-op,
                    # 合乎預期。
                    #
                    # 【ADR-128】「12:00 二次確認」已經從這裡搬到
                    # _qt_chukuangren_confirm_pass() —— 原本它巢狀在這個 K 棒邊界
                    # 閘門底下,而閘門的節拍來自 watch_timeframe,所以策略被迫把
                    # watch_timeframe 寫死成 '5分K' 才踩得進 12:00~12:04 的窗口。
                    # 那個借用造成三個問題 (見 chukuangren_band.default_strategy()),
                    # 而且**一天只有一次機會**:5分K 邊界在 12:00 觸發一次就被
                    # _qt_last_boundary 記住,下一次是 12:05 —— 已經在窗口外。那一次
                    # 若剛好抓不到 5分K 資料 (背景補齊中/空 DataFrame),當天的二次
                    # 確認就整個丟掉,而且完全無聲。搬出去之後窗口內每個 runner tick
                    # 都能重試,反而比原本可靠。
                    w_contract, w_asset, w_sym, w_mkt = self._qt_resolve_watch(s)
                    if w_contract is None:
                        raise RuntimeError(f"看盤商品(看A)合約解析失敗: {strategy_engine.watch_symbol_of(s)}")
                    daily_df = self._qt_fetch_closed_bars(s, w_contract, w_asset, tf='日K',
                                                          cache_sym=w_sym, cache_market=w_mkt)
                    if daily_df is None or daily_df.empty:
                        continue  # 沒資料不算錯誤 (可能休市/剛登入還沒建立快取)
                    # 【ADR-120】原本這裡會順便跑盤勢/型態提醒。該功能已搬到
                    # 主圖【盤勢判斷】(看盤時就看得到,不用開著策略),這裡回到
                    # 只做進出場判斷。
                    params = chukuangren_band.params_of(s)
                    chukuangren_band.on_daily_close(params, rt, daily_df,
                                                    chukuangren_band.direction_of(s))
                    intents = []
                    b_exec_price = None
                    # 【ADR-128】這裡**只**做日K 訊號判斷。12:00 二次確認整條搬到
                    # _qt_chukuangren_confirm_pass(),這個分支不再保留任何捷徑 ——
                    # 中途版本曾在這裡留一個 `if _forced:` 的橋接當測試接縫,但查證
                    # 後沒有任何測試或 production 路徑需要它 (_quant_eval_pass 的唯一
                    # 呼叫端是 runner,不帶參數),留著只會變成「只有測試會走的
                    # production 分支」+ 第二個確認呼叫點 (P-67 的風險)。
                else:
                    w_contract, w_asset, w_sym, w_mkt = self._qt_resolve_watch(s)
                    if w_contract is None:
                        raise RuntimeError(f"訊號來源(看A)合約解析失敗: {strategy_engine.watch_symbol_of(s)}")
                    w_tf = strategy_engine.watch_timeframe_of(s)
                    df = self._qt_fetch_closed_bars(s, w_contract, w_asset, tf=w_tf,
                                                    cache_sym=w_sym, cache_market=w_mkt)
                    if df is None or df.empty:
                        continue  # 沒資料不算錯誤 (可能休市)
                    # 【ADR-075】看A做B:訊號/指標/停損停利全部看 A (df 就是 A);
                    # 「做B」只發生在下單/記帳那一層。b_exec_price = B 的最新已收盤價,
                    # 當實際成交價;非看A做B (A=B) 時 b_exec_price=None,用 A 自己的價。
                    b_exec_price = None
                    if strategy_engine.watch_enabled(s):
                        b_df = self._qt_fetch_closed_bars(s, contract, asset_type)
                        if b_df is None or b_df.empty:
                            continue  # B 沒有可用價格,先不動作 (可能 B 剛好無資料)
                        b_exec_price = float(b_df['Close'].iloc[-1])
                    if s.get('kind') == 'custom':
                        # 【ADR-040】自訂策略:在子行程執行 on_bar (逾時保護),
                        # 取決策後轉成與內建同格式的 intent → 下游 risk_check/下單完全同路。
                        # on_bar 看 A 的 df;intent 價用 A 收盤 (停損停利以 A 判定),下單另換 B。
                        decision = self._run_custom_in_subprocess(s, df, rt.get('state', 'FLAT'), runtime=rt)
                        # 【ADR-053】反手支援:改用 decision_to_intents,持多遇 SELL 會
                        # 產生 [平多, 反手開空] 兩個 intent,各自照常過 risk_check 與下單。
                        intents = custom_strategy.decision_to_intents(decision, s, rt, float(df['Close'].iloc[-1]), str(df.index[-1]))
                    else:
                        intents = strategy_engine.evaluate_strategy(s, rt, df, now_ts, today_str)
                # 【ADR-065】條件函式拋例外時 eval_conditions 只讓那一條算 False,
                # 不會中斷整組評估,但也不能完全無聲無息——否則「進場/出場條件
                # 到了卻沒有動作」時,使用者連錯誤訊息都看不到。
                cond_errs = rt.get('condition_errors') or []
                if cond_errs:
                    detail = "; ".join(f"{lab}: {err}" for lab, err in cond_errs)
                    self.safe_after(0, self.log_message,
                                    f"【自動交易-條件錯誤】策略「{s.get('name')}」有條件評估失敗 (視同不成立): {detail}")
                # 【ADR-066】進/出場條件明明成立,卻被使用者自己設定的時間窗
                # (entry_time_start/end、exit_time_start/end、specific_entry_time)
                # 排除——這不是錯誤,但一樣要讓使用者看得見,否則又是一種
                # 「條件到了卻沒有動作,查無原因」。
                time_skips = rt.get('time_window_skips') or []
                if time_skips:
                    detail = "; ".join(time_skips)
                    self.safe_after(0, self.log_message,
                                    f"【自動交易-時間窗跳過】策略「{s.get('name')}」: {detail}")
                for intent in intents:
                    # 【ADR-075】實際成交價 = B 的最新價 (看A做B);否則 = A 自己的 intent 價。
                    # intent['price'] (A 的價) 仍保留給 strategy_engine.apply_fill 當
                    # entry_price → 停損停利以 A 判定;下單/記帳一律用 exec_px (B)。
                    exec_px = b_exec_price if b_exec_price is not None else float(intent['price'])
                    _watch_tag = (f" [看{strategy_engine.watch_symbol_of(s)}訊號→做{s.get('symbol')}@{exec_px:g}]"
                                  if strategy_engine.watch_enabled(s) else "")
                    label = f"策略「{s.get('name')}」{intent['action']} {intent['qty']} {s.get('symbol')} @ {exec_px:g}{_watch_tag}"
                    ok, reason = strategy_engine.risk_check(s, rt, intent, now_ts)
                    if not ok:
                        self.safe_after(0, self.log_message, f"【自動交易-風控擋單】{label} — {reason}")
                        continue
                    if s.get('mode') == '實單' and self._qt_running:
                        sent, msg = self._place_strategy_order(s, intent, contract, asset_type, exec_price=exec_px)
                        if sent:
                            strategy_engine.apply_fill(s, rt, intent, now_ts, exec_price=exec_px)
                            changed = True
                            self.safe_after(0, self.log_message, f"【自動交易-實單】🔥 {label} | {intent['reason']} | {msg}")
                        else:
                            self.safe_after(0, self.log_message, f"【自動交易-實單失敗】{label} — {msg} (狀態未變更,下一根K棒會再評估)")
                    else:
                        strategy_engine.apply_fill(s, rt, intent, now_ts, exec_price=exec_px)
                        changed = True
                        # 【ADR-041/新ADR多帳戶】模擬成交記進策略指定的模擬帳戶
                        # (完整記帳:資金/持倉/損益;帳戶容器由 _qt_paper_acct_for 解析)
                        try:
                            acct = self._qt_paper_acct_for(s)
                            rec = paper_account.apply_fill(
                                acct, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                s.get('market', '台股'), s.get('symbol', ''),
                                intent['action'], intent['kind'], intent['qty'], exec_px,
                                trade_type=strategy_engine.trade_type_of(s))
                            paper_account.mark_price(acct, s.get('symbol', ''), exec_px)
                            self._qt_save_paper()
                            pnl_txt = f",此筆已實現 {_fmt_amt_signed(rec['pnl'])}" if intent['kind'] == 'CLOSE' else ""
                            eq = paper_account.equity(acct)
                            self.safe_after(0, self.log_message,
                                            f"【自動交易-模擬】🧪 {label} | {intent['reason']} → 已記入模擬帳戶"
                                            f"「{acct.get('name','?')}」(權益 {_fmt_amt(eq)}{pnl_txt})")
                        except Exception:
                            self.safe_after(0, self.log_message, f"【自動交易-模擬】🧪 {label} | {intent['reason']} (模擬)")
                rt['error_count'] = 0
                rt['data_error_count'] = 0
            except strategy_engine.KBarsFetchError as e:
                # 【ADR-121】抓不到K線是外部環境問題,先重試;重試用完才記錄,
                # 而且**永遠不會**因此自動停用策略 (見 _qt_on_kbars_fetch_error)。
                self._qt_on_kbars_fetch_error(s, rt, e, boundary_key, _prev_boundary)
            except Exception as e:
                # 【ADR-121】這裡只剩「策略邏輯/下單路徑」的錯誤。自動停用維持
                # 原本的行為 —— 真的壞掉的策略就該停手。
                rt['error_count'] = int(rt.get('error_count', 0)) + 1
                self.safe_after(0, self.log_message,
                                f"【自動交易-異常】策略「{s.get('name')}」第 {rt['error_count']} 次錯誤: {type(e).__name__}: {e}")
                if rt['error_count'] >= 3:
                    s['enabled'] = False
                    self._qt_save()
                    self.safe_after(0, self.log_message,
                                    f"【自動交易-保護】策略「{s.get('name')}」連續 3 次錯誤,已自動停用 (不影響其他策略與主系統)。")
                if self._looks_like_session_dead(e):
                    self._mark_session_dead()
        if changed:
            self._qt_save_state()
        self.safe_after(0, self._qt_refresh_tree)

    def _qt_update_realtime_pnl(self):
        try:
            needed = {}
            for s in self.strategies:
                if not s.get('enabled'):
                    continue
                rt = self._qt_runtime(s['id'])
                if rt.get('state') in ('LONG', 'SHORT'):
                    c, _ = self._qt_resolve(s)
                    if c:
                        needed[s.get('symbol')] = c
            
            # 【新ADR 多帳戶】所有帳戶的持倉都要納入標記價更新範圍,不是只看
            # 單一帳戶——不同策略可能把部位分散記在不同帳戶裡。
            for acct in self.paper_accts.values():
                for sym, p in acct['positions'].items():
                    if sym not in needed:
                        try:
                            c = self._resolve_futures_contract(sym) if p['market'] == '期貨' else self.sj_api.Contracts.Stocks.get(sym)
                            if c:
                                needed[sym] = c
                        except Exception:
                            pass

            if not needed:
                return False

            contracts = list(needed.values())
            snaps = self.sj_api.snapshots(contracts)
            if snaps:
                import core.paper_account as paper_account
                live_price_by_symbol = {}
                for snap in snaps:
                    code = getattr(snap, 'code', '')
                    close = getattr(snap, 'close', 0)
                    if code and close > 0:
                        for acct in self.paper_accts.values():
                            paper_account.mark_price(acct, code, float(close))
                        for sym, c in needed.items():
                            if getattr(c, 'code', '') == code or getattr(c, 'symbol', '') == code:
                                for acct in self.paper_accts.values():
                                    paper_account.mark_price(acct, sym, float(close))
                                live_price_by_symbol[sym] = float(close)
                # 【新ADR 期貨即時停損停利】沿用這次已經打過的 snapshots() 結果,
                # 不額外呼叫 API (鐵則5:snapshots() 節流)——期貨標的一旦價格觸及
                # 使用者設定的停損%/停利%/停損點數/停利點數,不等K棒收盤立刻出場。
                self._qt_check_realtime_futures_stops(live_price_by_symbol)
                return True
        except Exception:
            pass
        return False

    def _qt_check_realtime_futures_stops(self, live_price_by_symbol):
        """【新ADR】期貨標的用內建停損%/停利%/停損點數(元)/停利點數(元) 時,
        不等該策略訊號週期的K棒收盤,即時價 (live_price_by_symbol,來自
        _qt_update_realtime_pnl 剛打的 snapshots()) 一觸及條件就立刻出場。

        背景:strategy_engine.evaluate_strategy() 的停損/停利只在K棒收盤時
        評估一次,且「看A做B」時比對基準是A的價格 (ADR-075),兩者疊加會讓
        畫面上B的帳面已經虧超過停損點、系統卻遲遲沒有動作——這是使用者
        實測回報的重大落差,這裡補上以B (實際下單/實際損益) 的即時價為準的
        獨立監控通道,不影響原本以A為準、K棒收盤才判定的既有邏輯 (兩者互不
        排斥,誰先觸發就用誰的結果,觸發後 state 變 FLAT,另一邊自然是no-op)。

        【ADR-135】**股票/零股也適用了**(原本只做期貨)。使用者:「停損停利
        點數到了,就立即執行,不需要等待到收盤。」"""
        if not live_price_by_symbol:
            return
        for s in list(self.strategies):
            if not s.get('enabled'):
                continue
            # 【ADR-135】原本這裡擋掉非期貨。使用者要求「停損停利點數到了就立即
            # 執行,不需要等待到收盤」,沒有限定商品別 —— 拿掉限制,股票/零股也
            # 走同一條即時監控。上游 _qt_update_realtime_pnl 本來就同時解析
            # 股票與期貨合約,live_price_by_symbol 涵蓋得到。
            # 【ADR-124】這條路原本**完全沒有交易時段閘門** —— 它掛在
            # _qt_update_realtime_pnl 底下每 3 秒跑一次,不看時間。使用者實測:
            # 終極波段 12:01 開的空單,在 **15:00:01 夜盤開盤第一秒**被平掉
            # (日盤 13:45 收盤後價格不動,夜盤第一筆真實報價一進來就觸發停損)。
            # 三條會下單的路徑裡,只有這一條沒有閘門。
            #
            # 不會削弱 ADR-087 的原意:那是要解決「盤中帳面已經虧超過停損點卻
            # 沒動作」,而這裡只在「市場關閉 / 使用者關掉該盤別」時擋 ——
            # 那種時候本來就不可能成交。session_gate=False 代表使用者明確要求
            # 不管時間都跑,照舊尊重。
            if s.get('session_gate', True) and not market_session.is_market_open(
                    strategy_engine.trade_type_of(s),
                    include_night=strategy_engine.include_night_of(s)):
                continue
            sym = s.get('symbol', '')
            live_price = live_price_by_symbol.get(sym)
            if live_price is None:
                continue
            rt = self._qt_runtime(s['id'])
            if rt.get('state') not in ('LONG', 'SHORT'):
                continue
            intent = strategy_engine.check_intrabar_futures_stop(s, rt, live_price)
            if intent is None:
                continue
            try:
                contract, asset_type = self._qt_resolve(s)
                if contract is None:
                    continue
                label = f"策略「{s.get('name')}」{intent['action']} {intent['qty']} {sym} @ {live_price:g} [即時]"
                if s.get('mode') == '實單' and self._qt_running:
                    sent, msg = self._place_strategy_order(s, intent, contract, asset_type, exec_price=live_price)
                    if not sent:
                        self.safe_after(0, self.log_message, f"【自動交易-實單失敗】{label} — {msg} (狀態未變更,下次再試)")
                        continue
                    strategy_engine.apply_fill(s, rt, intent, time.time(), exec_price=live_price)
                    self.safe_after(0, self.log_message, f"【自動交易-實單】🔥 {label} | {intent['reason']} | {msg}")
                else:
                    strategy_engine.apply_fill(s, rt, intent, time.time(), exec_price=live_price)
                    acct = self._qt_paper_acct_for(s)
                    rec = paper_account.apply_fill(
                        acct, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        s.get('market', '台股'), sym, intent['action'], intent['kind'],
                        intent['qty'], live_price, trade_type=strategy_engine.trade_type_of(s))
                    paper_account.mark_price(acct, sym, live_price)
                    self._qt_save_paper()
                    pnl_txt = f",此筆已實現 {_fmt_amt_signed(rec['pnl'])}" if intent['kind'] == 'CLOSE' else ""
                    self.safe_after(0, self.log_message,
                                    f"【自動交易-模擬】🧪 {label} | {intent['reason']} → 已記入模擬帳戶{pnl_txt}")
                self._qt_save_state()
                self.safe_after(0, self._qt_refresh_tree)
                if getattr(self, '_paper_win', None) and self._paper_win.winfo_exists():
                    self.safe_after(0, self._qt_refresh_paper_account)
            except Exception as e:
                self.safe_after(0, self.log_message,
                                f"【自動交易-即時停損異常】策略「{s.get('name')}」: {type(e).__name__}: {e}")

    def _qt_chukuangren_confirm_one(self, s, rt, params, w_contract, w_asset,
                                    w_sym, w_mkt, today_key, now_ts):
        """【ADR-128】對單一終極波段策略做一次「12:00 二次確認」。

        回傳 True = 這次真的呼叫了 on_noon_confirm (不論確認成立或作廢,
        on_noon_confirm 都會寫 rt['last_confirm_date'],所以當天不會再進來);
        False = 還沒做成 (沒有待確認訊號 / 當天已確認 / **5分K 資料還沒到**),
        呼叫端應該讓下一個 tick 再試。

        「資料還沒到就回 False」是這次修正的核心:回 True 會讓當天的機會被吃掉,
        而那正是舊做法的隱患 (見 _qt_chukuangren_confirm_pass 的說明)。
        """
        if not (rt.get('pending_entry') or rt.get('pending_exit')):
            return False
        if rt.get('last_confirm_date') == today_key:
            return False
        intraday_df = self._qt_fetch_closed_bars(s, w_contract, w_asset, tf='5分K',
                                                 cache_sym=w_sym, cache_market=w_mkt)
        if intraday_df is None or intraday_df.empty:
            return False
        confirm_price = float(intraday_df['Close'].iloc[-1])
        chukuangren_band.on_noon_confirm(
            params, rt, confirm_price, today_key, now_ts, qty=int(s.get('qty', 1)))
        if rt.get('armed_intent'):
            self.safe_after(0, self.log_message,
                            f"【自動交易-待下單】策略「{s.get('name')}」12:00確認成立"
                            f" ({rt['armed_intent']['reason']}),約1分鐘後 (12:01) 依"
                            f"{s.get('symbol')}當時最新價自動送單。")
        return True

    def _qt_chukuangren_confirm_pass(self):
        """【ADR-128】終極波段的「隔天中午 12:00 二次確認」獨立通道。

        原本這件事巢狀在 `_quant_eval_pass` 的 K 棒邊界閘門底下,而閘門的節拍
        取自 `watch_timeframe` —— 所以策略被迫把 watch_timeframe 寫死成 '5分K'
        才踩得進 12:00~12:04 的窗口。使用者實機回報就是這個外洩:
        「我明明設定就是看日K,為什麼哪裡有看5分K?」

        搬出來之後有兩個好處:
        1. `watch_timeframe` 可以回到真話 (日K),節拍不再跟訊號週期綁在一起。
        2. **當天的確認不再只有一次機會**。舊做法在 12:00 觸發一次後,
           `_qt_last_boundary` 就記住了這個邊界,下一次是 12:05 —— 已經在窗口外。
           那一次若剛好拿不到 5分K (ADR-122 背景補齊中、ADR-121 逾時、空
           DataFrame),當天的二次確認就整個丟掉,而且完全無聲。現在窗口內每個
           runner tick (2 秒) 都會重試,直到 on_noon_confirm 真的被呼叫。

        掛在 quant_runner_worker 既有的 2 秒輪詢上。窗口外、沒有待確認訊號、
        當天已確認過的,都在打任何 API 之前就 continue,所以成本可忽略。
        """
        if not getattr(self, '_qt_running', False):
            return
        if not (self.api_logged_in and getattr(self, 'sj_api', None)):
            return
        now_dt = datetime.now()
        if not chukuangren_band.in_noon_confirm_window(now_dt):
            return
        today_key = now_dt.strftime('%Y-%m-%d')
        now_ts = time.time()
        for s in list(getattr(self, 'strategies', []) or []):
            if not s.get('enabled') or s.get('kind') != chukuangren_band.KIND:
                continue
            rt = self._qt_runtime(s['id'])
            # 【便宜的檢查放最前面】沒有待確認訊號 / 當天已確認過 → 完全不碰 API。
            if not (rt.get('pending_entry') or rt.get('pending_exit')):
                continue
            if rt.get('last_confirm_date') == today_key:
                continue
            # 【ADR-124】跟其他兩條會動到部位的路徑用同一個時段閘門。
            # 12:00 本來就在期貨日盤與台股盤中,所以正常情況下這裡一定放行;
            # 有閘門是為了「使用者把盤別關掉時,這條路徑也要聽話」。
            if s.get('session_gate', True) and not market_session.is_market_open(
                    strategy_engine.trade_type_of(s),
                    include_night=strategy_engine.include_night_of(s)):
                continue
            try:
                w_contract, w_asset, w_sym, w_mkt = self._qt_resolve_watch(s)
                if w_contract is None:
                    raise RuntimeError(
                        f"看盤商品(看A)合約解析失敗: {strategy_engine.watch_symbol_of(s)}")
                if self._qt_chukuangren_confirm_one(
                        s, rt, chukuangren_band.params_of(s), w_contract, w_asset,
                        w_sym, w_mkt, today_key, now_ts):
                    self._qt_save_state()
            except strategy_engine.KBarsPending:
                # 【ADR-122】資料正在背景補齊,不是錯誤。窗口還有好幾分鐘,
                # 下一個 tick 再試 —— 這正是搬出邊界閘門要換到的能力。
                continue
            except Exception as e:
                # 窗口內每 2 秒會走一次,錯誤訊息**每檔策略每天只記一次**,
                # 否則同一則會刷 150 次 (ADR-127 P-99 的同一類問題)。
                if not hasattr(self, '_qt_confirm_err_noted'):
                    self._qt_confirm_err_noted = {}
                if self._qt_confirm_err_noted.get(s['id']) != today_key:
                    self._qt_confirm_err_noted[s['id']] = today_key
                    self.safe_after(0, self.log_message,
                                    f"【自動交易-12:00確認異常】策略「{s.get('name')}」: "
                                    f"{type(e).__name__}: {e}(窗口內會持續重試)")

    def _qt_chukuangren_execute_pass(self):
        """【新ADR 確認/下單分兩個時間點】終極波段策略12:00確認成立後不會立刻
        下單,而是記進 rt['armed_intent']——真正送單要等
        chukuangren_band.on_execute_armed 判斷『已過60秒』(約12:01) 才發生。

        這個檢查獨立於 _quant_eval_pass 的5分K邊界閘門之外:終極波段策略的5分K
        邊界只在每5分鐘整點觸發一次 (12:00/12:05/...),不會剛好落在12:01,
        所以延遲下單無法沿用同一套邊界機制,得另外掛在 quant_runner_worker
        既有的2秒輪詢節奏上,每次都呼叫、沒有 armed_intent 的策略一律
        立刻continue (成本可忽略)。

        執行價用B (執行商品) 當下最新的1分K收盤價 (比照原本5分K的精神,但
        用更短週期讓「1分鐘後」這件事真的反映在價格上,不會跟12:00確認時
        用的是同一根還沒收盤的5分K)。"""
        if not self._qt_running:
            return
        if not (self.api_logged_in and HAS_SJ and self.sj_api):
            return
        now_ts = time.time()
        changed = False
        for s in list(self.strategies):
            if not s.get('enabled') or s.get('kind') != chukuangren_band.KIND:
                continue
            # 【ADR-124】原本靠 armed_intent + 10 分鐘時效**間接**安全 (只能在
            # 12:01 前後動作)。補上閘門,讓「終極波段不在夜盤動作」變成結構
            # 保證而不是巧合。12:01 在日盤內,閘門放行,既有行為不變。
            if s.get('session_gate', True) and not market_session.is_market_open(
                    strategy_engine.trade_type_of(s),
                    include_night=strategy_engine.include_night_of(s)):
                continue
            rt = self._qt_runtime(s['id'])
            if rt.get('armed_intent') is None:
                continue
            try:
                contract, asset_type = self._qt_resolve(s)
                if contract is None:
                    continue
                b_df = self._qt_fetch_closed_bars(s, contract, asset_type, tf='1分K')
                if b_df is None or b_df.empty:
                    continue
                exec_px = float(b_df['Close'].iloc[-1])
                intents = chukuangren_band.on_execute_armed(rt, exec_px, now_ts)
                # 【找到的bug/修正】12:00確認成立後若因為停用/總開關關閉/程式重啟
                # 等原因太久沒真正執行 (超過 on_execute_armed 的 max_age_sec,預設
                # 10分鐘),chukuangren_band 會把它作廢而不是用現在的價格補送——
                # 不然會變成拿一個好幾小時甚至隔天才確認的舊訊號,用完全不相干的
                # 當下價格下單,語意跟「12:00確認、12:01立刻執行」完全不符。這裡
                # 補上通知,讓使用者知道有一筆確認被作廢,不是系統沒反應。
                stale = rt.pop('last_discarded_stale_intent', None)
                if stale:
                    self.safe_after(0, self.log_message,
                                    f"【自動交易-警示】策略「{s.get('name')}」的12:00確認"
                                    f" ({stale.get('reason')}) 因為評估中斷太久 (超過10分鐘"
                                    f"未執行) 已作廢,不會用現在的價格補送;部位/訊號會在"
                                    f"下一次日K收盤重新評估,無需手動處理。")
                    self._qt_save_state()
                if not intents:
                    continue  # 還沒過60秒,下一輪(2秒後)再檢查
                for intent in intents:
                    label = (f"策略「{s.get('name')}」{intent['action']} {intent['qty']} "
                            f"{s.get('symbol')} @ {exec_px:g} [12:01延遲下單]")
                    ok, reason = strategy_engine.risk_check(s, rt, intent, now_ts)
                    if not ok:
                        self.safe_after(0, self.log_message, f"【自動交易-風控擋單】{label} — {reason}")
                        continue
                    if s.get('mode') == '實單' and self._qt_running:
                        sent, msg = self._place_strategy_order(s, intent, contract, asset_type, exec_price=exec_px)
                        if sent:
                            strategy_engine.apply_fill(s, rt, intent, now_ts, exec_price=exec_px)
                            changed = True
                            self.safe_after(0, self.log_message, f"【自動交易-實單】🔥 {label} | {intent['reason']} | {msg}")
                        else:
                            self.safe_after(0, self.log_message, f"【自動交易-實單失敗】{label} — {msg} (狀態未變更)")
                    else:
                        strategy_engine.apply_fill(s, rt, intent, now_ts, exec_price=exec_px)
                        changed = True
                        try:
                            acct = self._qt_paper_acct_for(s)
                            rec = paper_account.apply_fill(
                                acct, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                s.get('market', '台股'), s.get('symbol', ''),
                                intent['action'], intent['kind'], intent['qty'], exec_px,
                                trade_type=strategy_engine.trade_type_of(s))
                            paper_account.mark_price(acct, s.get('symbol', ''), exec_px)
                            self._qt_save_paper()
                            pnl_txt = f",此筆已實現 {_fmt_amt_signed(rec['pnl'])}" if intent['kind'] == 'CLOSE' else ""
                            eq = paper_account.equity(acct)
                            self.safe_after(0, self.log_message,
                                            f"【自動交易-模擬】🧪 {label} | {intent['reason']} → 已記入模擬帳戶"
                                            f" (權益 {_fmt_amt(eq)}{pnl_txt})")
                        except Exception:
                            self.safe_after(0, self.log_message, f"【自動交易-模擬】🧪 {label} | {intent['reason']} (模擬)")
                rt['error_count'] = 0
            except Exception as e:
                rt['error_count'] = int(rt.get('error_count', 0)) + 1
                self.safe_after(0, self.log_message,
                                f"【自動交易-異常】策略「{s.get('name')}」延遲下單第 {rt['error_count']} 次錯誤: "
                                f"{type(e).__name__}: {e}")
        if changed:
            self._qt_save_state()
            self.safe_after(0, self._qt_refresh_tree)

    def _qt_refresh_paper_account(self):
        """【新ADR 多帳戶】更新模擬帳戶視窗頭部數字 + 持倉表,對象是視窗目前
        選取的帳戶 (self._paper_win_account_id),不是固定的單一帳戶。
        同時順手更新「📊 全部帳戶總覽」視窗 (若開著),兩個視窗共用同一份
        帳戶資料,呼叫端不用每個地方都記得補一次全帳戶總覽的刷新。"""
        self._qt_refresh_all_accounts_window()
        try:
            if not getattr(self, '_paper_win', None) or not self._paper_win.winfo_exists():
                return
            a = self.paper_accts.get(getattr(self, '_paper_win_account_id', None))
            if a is None:
                return
            import core.paper_account as paper_account
            eq = paper_account.equity(a)
            unreal = paper_account.unrealized_pnl(a)
            ret_pct = (eq - a['initial_cash']) / a['initial_cash'] * 100.0 if a['initial_cash'] else 0.0
            
            if hasattr(self, '_paper_ui'):
                ui = self._paper_ui
                ui['init'].config(text=f"{_fmt_amt(a['initial_cash'])}")
                ui['cash'].config(text=f"{_fmt_amt(a['cash'])}")
                ui['eq'].config(text=f"{_fmt_amt(eq)}")
                ui['real'].config(text=f"{_fmt_amt_signed(a['realized_pnl'])}",
                                  fg='#FF1744' if a['realized_pnl'] > 0 else ('#00E676' if a['realized_pnl'] < 0 else 'white'))
                ui['unreal'].config(text=f"{_fmt_amt_signed(unreal)}", fg='#FF1744' if unreal > 0 else ('#00E676' if unreal < 0 else 'white'))
                ui['ret'].config(text=f"{ret_pct:+.2f}%", fg='#FF1744' if ret_pct > 0 else ('#00E676' if ret_pct < 0 else 'white'))

                tvp = ui['tvp']
                keep = tvp.focus() or ((tvp.selection() or [None])[0])
                for iid in tvp.get_children():
                    tvp.delete(iid)
                for sym, p in a['positions'].items():
                    d = 1.0 if p['direction'] == '多' else -1.0
                    diff = (float(p.get('mark_price', p['avg_price'])) - p['avg_price']) * d
                    if p['market'] == '台股':
                        u = diff * 1000 * p['qty']
                    else:
                        mult, _ = paper_account._fut_multiplier(sym)
                        u = diff * mult * p['qty']
                    tag = 'p_win' if u > 0 else ('p_loss' if u < 0 else 'p_flat')
                    name = self._wl_display_name(sym)
                    tvp.insert("", tk.END, iid=sym, values=(sym, name, p['direction'], p['qty'],
                                                    f"{p['avg_price']:g}", f"{p.get('mark_price', p['avg_price']):g}",
                                                    f"{_fmt_amt_signed(u)}"), tags=(tag,))
                if keep and tvp.exists(keep):
                    tvp.selection_set(keep); tvp.focus(keep)
        except Exception:
            pass

    def quant_runner_worker(self):
        """【ADR-041】量化 runner 改 2 秒節奏 + K棒邊界感知:
        舊版每 10 秒盲目輪詢,策略訊號最慢要等 10+3 秒才評估——對分K程式交易
        延遲太大 (使用者:報價延遲會造成重大虧損)。改為每 2 秒醒來,但只有
        「該策略的K棒剛收盤」(跨過邊界+2秒資料緩衝) 才真正抓資料評估:
        延遲從最慢 13 秒縮到約 2~4 秒,同時 API 呼叫次數反而更少。
        總開關關閉時完全閒置。"""
        import time as _time
        last_snap_time = 0
        while True:
            try:
                if getattr(self, '_closing', False):
                    return
                self._quant_eval_pass()
                # 【ADR-128】12:00 二次確認 → 12:01 送單,兩者都獨立於 K 棒邊界
                # 閘門之外,順序上確認必須在送單之前 (同一個 tick 內不會同時發生,
                # on_execute_armed 要等 60 秒,但順序寫對比較不容易誤讀)。
                self._qt_chukuangren_confirm_pass()
                self._qt_chukuangren_execute_pass()

                now_ts = _time.time()
                # 【修正】ADR-091 把 self.paper_acct (單一帳戶) 改成 self.paper_accts
                # (帳戶容器) 之後,這裡沒有同步改名,條件永遠是 None → 整段即時標記價
                # /未實現損益更新、以及 ADR-087 的期貨即時停損停利都沒有真正在跑。
                if self.api_logged_in and HAS_SJ and getattr(self, 'sj_api', None) and getattr(self, 'paper_accts', None):
                    if now_ts - last_snap_time >= 3.0:
                        last_snap_time = now_ts
                        if self._qt_update_realtime_pnl():
                            self.safe_after(0, self._qt_refresh_tree)
                            if getattr(self, '_paper_win', None) and self._paper_win.winfo_exists():
                                self.safe_after(0, self._qt_refresh_paper_account)
                            if getattr(self, '_all_accts_win', None) and self._all_accts_win.winfo_exists():
                                self.safe_after(0, self._qt_refresh_all_accounts_window)
            except Exception:
                pass
            _time.sleep(2)

    # ---------- 策略編輯器 ----------
    def _qt_build_price_type_row(self, parent, s, cb_tt):
        """【ADR-135】委託方式 (限價/市價/範圍市價) 這一列,內建與自訂策略共用。

        規則有兩條外部依據,**只能有一份實作**(兩份遲早分歧,P-67):
          · 範圍市價僅期貨支援 (TAIFEX 規則)
          · 零股依交易所規則只能限價 (鐵則6)
        會跟著「交易種類」下拉即時切換可選值。回傳 {'get': callable}。
        """
        row = tk.Frame(parent, bg="#1A2026")
        tk.Label(row, text="委託方式", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        cb_ptype = ttk.Combobox(row, width=8, state='readonly', style="BlackText.TCombobox")
        cb_ptype.pack(side=tk.LEFT, padx=6)
        hint = tk.Label(row, bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8))
        hint.pack(side=tk.LEFT, padx=(6, 0))

        def _sync(*_a):
            tt_now = cb_tt.get()
            if tt_now == '期貨':
                vals = list(strategy_engine.PRICE_TYPES)
                txt = "限價才會套用「讓價檔數」讓價;市價/範圍市價 依當下市場成交(價格送0)。"
            elif tt_now == '零股':
                vals = ['限價']
                txt = "零股依交易所規則只能限價 (鐵則6),市價/範圍市價 不開放。"
            else:
                vals = ['限價', '市價']
                txt = "限價才會套用「讓價檔數」讓價;市價 依當下市場成交(價格送0)。股票無範圍市價。"
            cb_ptype.config(values=vals)
            cur = s.get('price_type', '限價') if not cb_ptype.get() else cb_ptype.get()
            cb_ptype.set(cur if cur in vals else '限價')
            hint.config(text=txt)

        cb_tt.bind('<<ComboboxSelected>>', lambda e: _sync(), add='+')
        _sync()
        return {'frame': row, 'get': lambda: (cb_ptype.get() or '限價')}

    def _qt_open_custom_editor(self, strategy, readonly=False):
        """【ADR-040】自訂 Python 策略編輯器:基本參數 + 程式碼區 + 安全警語。"""
        is_new = strategy is None
        if is_new:
            s = strategy_engine.new_strategy()
            s['kind'] = 'custom'
            s['source_code'] = custom_strategy.EXAMPLE_SOURCE
            s['custom_params'] = {'fast': 5, 'slow': 20}
        else:
            s = json.loads(json.dumps(strategy))
        dlg = tk.Toplevel(self)
        title_suffix = " (唯讀 - 策略執行中)" if readonly else ""
        dlg.title("自訂 Python 策略" + ("" if is_new else f" — {s.get('name')}{title_suffix}"))
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 760, 700)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass
        # 安全警語 (最上方,紅字)
        warn = ("⚠ 安全須知:自訂策略是你寫的 Python 程式,會被系統執行。請「只執行你自己寫的、"
                "看得懂的」程式,絕不要貼上來路不明的程式碼。策略在獨立子行程執行 (崩潰/卡死不會"
                "拖垮主程式),且下單仍受總開關+每日次數+冷卻+熔斷三層防護把關。")
        tk.Label(dlg, text=warn, bg="#2A1215", fg="#FF8A80", font=('微軟正黑體', 9),
                 wraplength=720, justify='left').pack(fill=tk.X, padx=10, pady=(10, 4))

        top = tk.Frame(dlg, bg="#1A2026"); top.pack(fill=tk.X, padx=12, pady=2)
        _live_get = self._qt_build_live_account_row(dlg, s)   # 【ADR-110階段2】
        def _lbl(p, t): return tk.Label(p, text=t, bg="#1A2026", fg="white", font=('微軟正黑體', 9))
        def _ent(p, v, w=10):
            e = tk.Entry(p, width=w, bg="#2A323D", fg="white", justify="center"); e.insert(0, str(v)); return e
        _lbl(top, "策略名稱").grid(row=0, column=0, sticky='w')
        e_name = _ent(top, s.get('name', ''), 16); e_name.grid(row=0, column=1, padx=4)
        _lbl(top, "商品").grid(row=0, column=2, sticky='w', padx=(8, 0))
        e_sym = _ent(top, s.get('symbol', ''), 10); e_sym.grid(row=0, column=3, padx=4)
        _lbl(top, "交易種類").grid(row=0, column=4, sticky='w', padx=(8, 0))
        cb_tt = ttk.Combobox(top, values=list(strategy_engine.TRADE_TYPES), width=7, state='readonly', style="BlackText.TCombobox")
        cb_tt.set(strategy_engine.trade_type_of(s)); cb_tt.grid(row=0, column=5, padx=4)
        tk.Label(top, text="← 也可直接點左側自選股帶入(自動判斷股票/期貨)", bg="#1A2026",
                 fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=0, column=6, sticky='w', padx=(10, 0))
        lbl_cname = tk.Label(top, text="", bg="#1A2026", fg="#29B6F6", font=('微軟正黑體', 9, 'bold'))
        lbl_cname.grid(row=4, column=0, columnspan=6, sticky='w', pady=(4, 0))
        def _clook(*_a):
            name, ok = self._resolve_strategy_symbol_name(cb_tt.get(), e_sym.get())
            if ok:
                lbl_cname.config(text=f"✓ 已確認商品:{name}", fg="#00E676")
            elif e_sym.get().strip():
                lbl_cname.config(text="✗ 查無此代碼 (需先登入)", fg="#FF5252")
            else:
                lbl_cname.config(text="")
        e_sym.bind('<KeyRelease>', _clook); e_sym.bind('<FocusOut>', _clook)
        cb_tt.bind('<<ComboboxSelected>>', _clook)
        # 【策略編輯器帶入 / ADR-077】同 _qt_open_editor:記住做B 欄位,點進 B 也切回 B。
        _bt = (dlg, e_sym, cb_tt, _clook, 'B')
        self._qt_editor_symbol_target = _bt
        e_sym.bind('<FocusIn>', lambda *_a: setattr(self, '_qt_editor_symbol_target', _bt), add='+')
        _lbl(top, "週期(可自訂)").grid(row=1, column=0, sticky='w', pady=(6, 0))
        # 【新ADR 自訂週期】state='normal':可從下拉點選常用值,也可直接打字
        # 輸入自訂週期 (例如 45分K、3時K),存檔前 validate_strategy() 會擋格式錯誤。
        cb_tf = ttk.Combobox(top, values=list(strategy_engine.VALID_TIMEFRAMES), width=7, state='normal', style="BlackText.TCombobox")
        cb_tf.set(s.get('timeframe', '5分K')); cb_tf.grid(row=1, column=1, padx=4, pady=(6, 0))
        _lbl(top, "數量").grid(row=1, column=2, sticky='w', padx=(8, 0), pady=(6, 0))
        e_qty = _ent(top, s.get('qty', 1), 6); e_qty.grid(row=1, column=3, padx=4, pady=(6, 0))
        _lbl(top, "模式").grid(row=1, column=4, sticky='w', padx=(8, 0), pady=(6, 0))
        cb_mode = ttk.Combobox(top, values=['模擬', '實單'], width=7, state='readonly', style="BlackText.TCombobox")
        cb_mode.set(s.get('mode', '模擬')); cb_mode.grid(row=1, column=5, padx=4, pady=(6, 0))
        # 【ADR-045 → ADR-135】方向仍由 on_bar 的 BUY/SELL 決定 (不設欄位),
        # 但**停損/停利改成可以設**:使用者要求自訂策略「其他的都跟內建策略基本
        # 功能一樣」,而且要求「停損停利點數到了就立即執行」。
        # 全部預設 0 = 停用 = 維持 ADR-045 的原意 (完全由程式碼控制出場);
        # 有填才生效,所以既有自訂策略的行為不變。
        tk.Label(top, text="※ 方向由你的 on_bar 程式碼決定 (BUY/SELL);"
                          "下面的停損/停利留 0 就是完全交給程式碼控制。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).grid(
                 row=2, column=0, columnspan=6, sticky='w', pady=(6, 0))
        _risk = tk.Frame(top, bg="#1A2026")
        _risk.grid(row=8, column=0, columnspan=7, sticky='w', pady=(6, 0))
        tk.Label(_risk, text="停損%", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_sl = _ent(_risk, s.get('stop_loss_pct', 0.0), 6); e_sl.pack(side=tk.LEFT, padx=(2, 8))
        tk.Label(_risk, text="停利%", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_tp = _ent(_risk, s.get('take_profit_pct', 0.0), 6); e_tp.pack(side=tk.LEFT, padx=(2, 8))
        tk.Label(_risk, text="停損(元/點)", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_sla = _ent(_risk, s.get('stop_loss_abs', 0.0), 7); e_sla.pack(side=tk.LEFT, padx=(2, 8))
        tk.Label(_risk, text="停利(元/點)", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_tpa = _ent(_risk, s.get('take_profit_abs', 0.0), 7); e_tpa.pack(side=tk.LEFT, padx=(2, 8))
        tk.Label(_risk, text="讓價檔數", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_slip = _ent(_risk, s.get('slippage_ticks', 0), 5); e_slip.pack(side=tk.LEFT, padx=2)
        # 【ADR-136】即時觸發改成可勾選 (使用者要求),不分商品別。
        var_intrabar = tk.BooleanVar(value=strategy_engine.intrabar_stop_enabled(s))
        tk.Checkbutton(_risk, text="停損停利即時觸發 (盤中觸價就出場,不等收盤)",
                       variable=var_intrabar, bg="#1A2026", fg="#FFCA28",
                       selectcolor="#2A323D", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(top, text="※ 停損/停利 0=停用。勾「即時觸發」= 盤中觸價就出場;不勾 = 等該週期K棒收盤才判定。"
                          "回測會照同一個設定模擬 (勾了就用當根高低點判斷觸價)。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).grid(
                 row=9, column=0, columnspan=7, sticky='w')

        # 【ADR-135】風控/時段欄位:原本自訂策略的編輯器沒有這幾項,但
        # new_strategy() 的預設值 (每日3次、冷卻300秒、時段閘門開、含夜盤)
        # **一直都在生效** —— 使用者看不到也改不了,正是 ADR-123 那個
        # 「看不見的預設值」的同一類問題。這次一併攤開來可設定。
        _risk2 = tk.Frame(top, bg="#1A2026")
        _risk2.grid(row=10, column=0, columnspan=7, sticky='w', pady=(6, 0))
        tk.Label(_risk2, text="每日進場上限", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_maxd = _ent(_risk2, s.get('max_trades_per_day', 3), 5); e_maxd.pack(side=tk.LEFT, padx=(2, 8))
        tk.Label(_risk2, text="冷卻秒數", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_cool = _ent(_risk2, s.get('cooldown_sec', 300), 6); e_cool.pack(side=tk.LEFT, padx=(2, 8))
        tk.Label(_risk2, text="期貨時段", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        cb_fsess = ttk.Combobox(_risk2, values=['日盤+夜盤', '只做日盤'], width=9,
                                state='readonly', style="BlackText.TCombobox")
        cb_fsess.set('只做日盤' if s.get('futures_session') == 'day' else '日盤+夜盤')
        cb_fsess.pack(side=tk.LEFT, padx=(2, 8))
        var_sess_gate = tk.BooleanVar(value=bool(s.get('session_gate', True)))
        tk.Checkbutton(_risk2, text="交易時段閘門 (非交易時間待命)", variable=var_sess_gate,
                       bg="#1A2026", fg="white", selectcolor="#2A323D",
                       font=('微軟正黑體', 9)).pack(side=tk.LEFT)

        # 【ADR-135】委託方式 (限價/市價/範圍市價) —— 與內建策略共用同一份規則
        _pt = self._qt_build_price_type_row(top, s, cb_tt)
        _pt['frame'].grid(row=11, column=0, columnspan=7, sticky='w', pady=(6, 0))
        _lbl(top, "自訂參數 (key=value,逗號分隔)").grid(row=3, column=0, columnspan=2, sticky='w', pady=(6, 0))
        params_str = ", ".join(f"{k}={v}" for k, v in (s.get('custom_params', {}) or {}).items())
        e_params = _ent(top, params_str, 34); e_params.grid(row=3, column=2, columnspan=3, sticky='w', padx=4, pady=(6, 0))
        _lbl(top, '進場時段(起~迄)').grid(row=5, column=0, sticky='w', pady=(6, 0))
        e_en_st = _ent(top, s.get('entry_time_start', ''), 8); e_en_st.grid(row=5, column=1, padx=4, pady=(6, 0))
        e_en_ed = _ent(top, s.get('entry_time_end', ''), 8); e_en_ed.grid(row=5, column=2, padx=4, pady=(6, 0), sticky='w')
        _lbl(top, '出場時段(起~迄)').grid(row=5, column=3, sticky='w', pady=(6, 0), padx=(10, 0))
        e_ex_st = _ent(top, s.get('exit_time_start', ''), 8); e_ex_st.grid(row=5, column=4, padx=4, pady=(6, 0))
        e_ex_ed = _ent(top, s.get('exit_time_end', ''), 8); e_ex_ed.grid(row=5, column=5, padx=4, pady=(6, 0), sticky='w')
        _lbl(top, '特定進場時間').grid(row=6, column=0, sticky='w', pady=(6, 0))
        e_sp_en = _ent(top, s.get('specific_entry_time', ''), 8); e_sp_en.grid(row=6, column=1, padx=4, pady=(6, 0))
        tk.Label(top, text="(格式: HH:MM 或 HH:MM:SS)", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=6, column=2, columnspan=2, sticky='w', pady=(6, 0))
        # 【新ADR 多帳戶】模擬成交要記進哪個模擬帳戶
        _lbl(top, "模擬帳戶").grid(row=7, column=0, sticky='w', pady=(6, 0))
        _acct_choices = self._qt_account_choices()
        cb_acct2 = ttk.Combobox(top, width=16, state='readonly', style="BlackText.TCombobox",
                                values=[name for _aid, name in _acct_choices])
        _ids2 = [aid for aid, _name in _acct_choices]
        _cur_aid = strategy_engine.account_id_of(s)
        cb_acct2.current(_ids2.index(_cur_aid) if _cur_aid in _ids2 else 0)
        cb_acct2.grid(row=7, column=1, padx=4, pady=(6, 0), sticky='w')
        tk.Label(top, text="(只在「模式=模擬」時有意義)", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 8)).grid(row=7, column=2, columnspan=3, sticky='w', pady=(6, 0))
        # 【ADR-074】自訂 Python 策略也能看A做B:on_bar 看 A 的 K 棒,下單到 B。
        watch_ui = self._qt_build_watch_panel(dlg, s)

        def _open_param_editor():
            """【ADR-055】參數編輯視窗:用表格增刪改參數,不必手打逗號字串,
            更不必回去改程式碼 —— 程式碼裡用 ctx.param('名稱', 預設值) 讀取,
            這裡改的值會覆寫預設值。"""
            pd_dlg = tk.Toplevel(dlg); pd_dlg.title("⚙ 策略參數")
            pd_dlg.configure(bg="#1A2026"); self.center_window(pd_dlg, 460, 420)
            pd_dlg.transient(dlg)
            try: pd_dlg.lift(); pd_dlg.focus_force()
            except Exception: pass
            tk.Label(pd_dlg, text=("這些參數對應程式碼裡的 ctx.param('名稱', 預設值)。\n"
                                   "在這裡改數值,不用動程式碼;想批次找最佳值請用「🎯 參數最佳化」。"),
                     bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9), wraplength=430,
                     justify='left').pack(fill=tk.X, padx=10, pady=(10, 4))
            body = tk.Frame(pd_dlg, bg="#1A2026"); body.pack(fill=tk.BOTH, expand=True, padx=10)
            rows = []

            def _add_row(k='', v=''):
                fr = tk.Frame(body, bg="#1A2026"); fr.pack(fill=tk.X, pady=2)
                ek = tk.Entry(fr, width=18, bg="#2A323D", fg="white"); ek.insert(0, str(k))
                ek.pack(side=tk.LEFT, padx=2)
                tk.Label(fr, text="=", bg="#1A2026", fg="white").pack(side=tk.LEFT)
                ev = tk.Entry(fr, width=14, bg="#2A323D", fg="white"); ev.insert(0, str(v))
                ev.pack(side=tk.LEFT, padx=2)
                item = {'fr': fr, 'k': ek, 'v': ev}

                def _rm():
                    try: fr.destroy()
                    except Exception: pass
                    if item in rows: rows.remove(item)
                tk.Button(fr, text="✖", bg="#5A6472", fg="white", relief="flat",
                          font=('微軟正黑體', 8), padx=6, command=_rm).pack(side=tk.LEFT, padx=4)
                rows.append(item)

            for pair in e_params.get().split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    if k.strip():
                        _add_row(k.strip(), v.strip())
            if not rows:
                _add_row()

            def _apply():
                parts = []
                for it in rows:
                    k = it['k'].get().strip()
                    v = it['v'].get().strip()
                    if k:
                        parts.append(f"{k}={v}")
                e_params.delete(0, tk.END); e_params.insert(0, ", ".join(parts))
                pd_dlg.destroy()

            fbtn = tk.Frame(pd_dlg, bg="#1A2026"); fbtn.pack(side=tk.BOTTOM, pady=8)
            tk.Button(fbtn, text="➕ 新增參數", bg="#29B6F6", fg="black", relief="flat",
                      font=('微軟正黑體', 10), padx=12, pady=3, command=lambda: _add_row()).pack(side=tk.LEFT, padx=5)
            tk.Button(fbtn, text="✅ 套用", bg="#00C853", fg="black", relief="flat",
                      font=('微軟正黑體', 10, 'bold'), padx=14, pady=3, command=_apply).pack(side=tk.LEFT, padx=5)
            tk.Button(fbtn, text="取消", bg="#2A323D", fg="white", relief="flat",
                      font=('微軟正黑體', 10), padx=14, pady=3, command=pd_dlg.destroy).pack(side=tk.LEFT, padx=5)

        tk.Button(top, text="⚙ 參數視窗", bg="#546E7A", fg="white", relief="flat",
                  font=('微軟正黑體', 9), padx=8, pady=1,
                  command=_open_param_editor).grid(row=3, column=5, sticky='w', padx=4, pady=(6, 0))

        tk.Label(dlg, text="on_bar(ctx) 程式碼:", bg="#1A2026", fg="#FFCA28",
                 font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(6, 0))
        # 【ADR-051】code_frame 先建立、稍後才 pack:tkinter pack 依呼叫順序配置空間,
        # 底部的狀態列 + 按鈕列必須「先」以 side=BOTTOM 取得空間,否則狀態列文字
        # 換行變高時會把按鈕擠出視窗外 (使用者實例:試跑成功後按鈕被截掉)。
        code_frame = tk.Frame(dlg, bg="#1A2026")
        txt = tk.Text(code_frame, bg="#0D1117", fg="#E6EDF3", insertbackground="white",
                      font=('Consolas', 10), wrap='none', undo=True)
        ys = ttk.Scrollbar(code_frame, orient='vertical', command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        ys.pack(side=tk.RIGHT, fill=tk.Y); txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        txt.insert('1.0', s.get('source_code', custom_strategy.EXAMPLE_SOURCE))

        def _parse_params():
            # 【ADR-056】改呼叫共用的 _parse_kv_params,回測對話框的參數欄位
            # 用同一套解析規則,兩處行為保證一致。
            return self._parse_kv_params(e_params.get())

        def _collect():
            s['kind'] = 'custom'
            s['name'] = e_name.get().strip(); s['symbol'] = e_sym.get().strip().upper()
            s['trade_type'] = cb_tt.get()
            s['market'] = '台期貨' if cb_tt.get() == '期貨' else '台股'
            s['timeframe'] = cb_tf.get()
            # 【ADR-045】自訂策略無「方向」欄位:開多/開空由 on_bar 回傳的
            # BUY/SELL 決定 (decision_to_intent 從不讀 direction,開空僅限期貨)。
            # direction 填佔位值讓內建欄位檢查通過;停損/停利保留既有值
            # (舊策略不變,新策略為 0.0=停用),出場改由程式碼自行控制。
            s['direction'] = s.get('direction', '做多') or '做多'
            try: s['qty'] = int(e_qty.get().strip())
            except (TypeError, ValueError): s['qty'] = 0
            # 【ADR-135】停損/停利改成從欄位讀 (0=停用,維持 ADR-045 的原意)
            def _f(entry, default=0.0):
                try:
                    return float(entry.get().strip())
                except (TypeError, ValueError):
                    return default

            def _i(entry, default):
                try:
                    return int(float(entry.get().strip()))
                except (TypeError, ValueError):
                    return default
            s['stop_loss_pct'] = _f(e_sl)
            s['take_profit_pct'] = _f(e_tp)
            s['stop_loss_abs'] = _f(e_sla)
            s['take_profit_abs'] = _f(e_tpa)
            s['slippage_ticks'] = _i(e_slip, 0)
            s['max_trades_per_day'] = _i(e_maxd, 3)
            s['cooldown_sec'] = _i(e_cool, 300)
            s['futures_session'] = 'day' if cb_fsess.get() == '只做日盤' else 'day_night'
            s['session_gate'] = bool(var_sess_gate.get())
            s['intrabar_stop'] = bool(var_intrabar.get())
            s['price_type'] = _pt['get']()
            s['entry_time_start'] = e_en_st.get().strip()
            s['entry_time_end'] = e_en_ed.get().strip()
            s['exit_time_start'] = e_ex_st.get().strip()
            s['exit_time_end'] = e_ex_ed.get().strip()
            s['specific_entry_time'] = e_sp_en.get().strip()
            s['mode'] = cb_mode.get()
            s['account_id'] = _ids2[cb_acct2.current()] if cb_acct2.current() >= 0 else 'default'
            # 【ADR-110階段2】實單要下到哪一家券商的哪一個帳號
            s['broker'], s['broker_account'] = _live_get()
            s['custom_params'] = _parse_params()
            s['source_code'] = txt.get('1.0', 'end-1c')
            s.update(watch_ui['get']())  # 【ADR-074】看A做B 設定
            # 自訂策略的進出場由程式碼決定,填佔位讓內建 validate 的欄位檢查通過
            s['entry'] = s.get('entry') or [{'type': 'ma_cross_up', 'params': {}}]
            s['exit_signals'] = s.get('exit_signals') or []
            return s

        def _validate_custom(strat):
            if not strat['name']:
                return False, "策略名稱不可空白"
            if not strat['symbol']:
                return False, "商品代碼不可空白"
            if strat['qty'] <= 0:
                return False, "數量必須為正整數"
            # 【ADR-074】B (執行商品) 不可為指數;看A做B 時 A 的代碼/週期要合法。
            if strategy_engine.looks_like_index_symbol(strat.get('symbol', '')):
                return False, "執行商品 (做B) 不可為指數 (加權/櫃買等,不能下單)。指數只能當『看A』。"
            if strategy_engine.watch_enabled(strat):
                if not str(strategy_engine.watch_symbol_of(strat)).strip():
                    return False, "已啟用「看A做B」,但『看A』的商品代碼不可空白"
                if strategy_engine.watch_timeframe_of(strat) not in strategy_engine.VALID_TIMEFRAMES:
                    return False, "『看A』的週期不合法"
            # 【ADR-135】自訂策略也能選委託方式了 → 用**同一份**交易所規則檢查
            # (範圍市價僅期貨、零股只能限價)。
            _ok_pt, _why_pt = strategy_engine.validate_price_type(strat)
            if not _ok_pt:
                return False, _why_pt
            if not strategy_engine.is_valid_timeframe(strat.get('timeframe')):
                return False, "週期格式不正確 (可選 5分K/15分K/30分K/60分K/日K,或自訂 N分K/N時K)"
            # 【ADR-045】改用 AST 靜態檢查 (core/custom_strategy.validate_source),
            # 絕不在主行程 exec 使用者程式碼:舊版 exec 檢查 (1) 與「策略在獨立
            # 子行程執行」的安全承諾矛盾,頂層 while True 會凍死 GUI;(2) 使用者
            # 貼獨立腳本時因命名空間沒有 __name__ 而噴難懂的 NameError。
            return custom_strategy.validate_source(strat['source_code'])

        def _test_run():
            """用最近歷史資料試跑一次,讓使用者當場知道程式能不能跑、現在會回什麼決策。"""
            strat = _collect()
            ok, msg = _validate_custom(strat)
            if not ok:
                _set_status(f"✗ 檢查未通過:\n{msg}", "#FF5252")
                self.log_message(f"【自訂策略-檢查】{msg}")
                return
            if not (self.api_logged_in and HAS_SJ and self.sj_api):
                _set_status("✗ 需要先登入券商 API 才能抓歷史資料試跑。", "#FF5252")
                self.log_message("【自訂策略-試跑】需要先登入券商 API 才能抓歷史資料試跑。")
                return
            _set_status(f"⏳ 「{strat['name']}」下載資料並在子行程試跑中...", "#FFCA28")
            self.log_message(f"【自訂策略-試跑】「{strat['name']}」下載資料並在子行程試跑中...")
            threading.Thread(target=self._qt_custom_test_worker,
                             args=(copy.deepcopy(strat), _status_cb), daemon=True).start()

        def _save():
            strat = _collect()
            ok, msg = _validate_custom(strat)
            if not ok:
                _set_status(f"✗ 儲存失敗:\n{msg}", "#FF5252")
                self.log_message(f"【自訂策略】儲存失敗: {msg}")
                return
            if strat['mode'] == '實單' and is_new:
                strat['mode'] = '模擬'
                self.log_message("【自動交易-安全】新自訂策略一律先以「模擬」儲存;請先試跑+回測+觀察模擬訊號,再改實單。")
            if is_new:
                strat['enabled'] = False
                self.strategies.append(strat)
                self.strategy_runtimes[strat['id']] = strategy_engine.new_runtime()
            else:
                for i, x in enumerate(self.strategies):
                    if x['id'] == strat['id']:
                        self.strategies[i] = strat; break
            self._qt_save(); self._qt_save_state(); self._qt_refresh_tree()
            # 【ADR-045】明確告知存到哪 + 下一步;儲存成功後策略會出現在
            # 「量化交易」分頁清單 (這本身就是最直接的可見回饋)。
            self.log_message(f"【自訂策略】「{strat['name']}」已儲存至 {self.QT_STRATEGY_FILE} "
                             f"({strat['mode']}),已加入「量化交易」分頁清單。")
            messagebox.showinfo("儲存成功",
                                f"策略「{strat['name']}」已儲存 ({strat['mode']} 模式)。\n\n"
                                f"存放位置:{os.path.abspath(self.QT_STRATEGY_FILE)}\n\n"
                                "策略已加入「量化交易」分頁清單。下一步:\n"
                                "1. 在清單選取它 → 按「🔬 回測」看歷史績效\n"
                                "2. 按「▶ 啟用」→ 按「🟢 啟動自動交易」開始跑模擬\n"
                                "3. 模擬訊號觀察沒問題後,再考慮改「實單」。", parent=dlg)
            dlg.destroy()

        _clook()
        # 【ADR-045】對話框內狀態列:試跑/儲存/檢查的結果直接顯示在這裡。
        # 舊版所有回饋只送 log_message → 訊息印在主視窗「系統日誌與回報」分頁,
        # 但使用者此時人在「量化交易」分頁、對話框又蓋在最上層,完全看不到,
        # 造成「按了沒反應」的錯覺。狀態列 + 日誌雙軌並行。
        lbl_status = tk.Label(dlg, text="", bg="#1A2026", fg="#8A99AD",
                              font=('微軟正黑體', 9), wraplength=720, justify='left', anchor='w')

        def _set_status(msg, color="#8A99AD"):
            try:
                if dlg.winfo_exists():
                    lbl_status.config(text=msg, fg=color)
            except Exception:
                pass

        def _status_cb(msg, ok):
            # 試跑 worker 在背景執行緒完成後回呼;經 safe_after 回 UI 執行緒,
            # 對話框若已被關閉則安靜略過 (_set_status 內含 winfo_exists 防護)。
            self.safe_after(0, _set_status, msg, "#00E676" if ok else "#FF5252")
        # 【ADR-051】底部兩列先以 side=BOTTOM 卡位 (順序:按鈕列在最下、狀態列在其上),
        # 之後 code_frame 才 expand 吃剩餘空間 —— 狀態列再長也不會擠掉按鈕。
        foot = tk.Frame(dlg, bg="#1A2026"); foot.pack(side=tk.BOTTOM, pady=8)
        foot2 = tk.Frame(dlg, bg="#1A2026"); foot2.pack(side=tk.BOTTOM, pady=(0, 2))
        lbl_status.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 2))
        code_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(2, 4))

        # --- 【ADR-051】策略腳本檔案管理:一個策略 = 一個 .py 檔,可匯入/匯出 ---
        def _import_py():
            path = filedialog.askopenfilename(
                parent=dlg, title="匯入策略腳本 (.py)",
                initialdir=self._strategy_dir(),
                filetypes=[("Python 策略腳本", "*.py"), ("所有檔案", "*.*")])
            if not path:
                return
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    code = f.read()
            except UnicodeDecodeError:
                try:
                    with open(path, 'r', encoding='big5') as f:
                        code = f.read()
                except Exception as e:
                    _set_status(f"✗ 讀檔失敗 (編碼非 UTF-8/Big5): {e}", "#FF5252"); return
            except Exception as e:
                _set_status(f"✗ 讀檔失敗: {type(e).__name__}: {e}", "#FF5252"); return
            txt.delete('1.0', tk.END); txt.insert('1.0', code)
            ok, msg = custom_strategy.validate_source(code)
            if ok:
                _set_status(f"✅ 已匯入 {os.path.basename(path)} (通過靜態檢查),可按「🧪 試跑一次」。", "#00E676")
            else:
                _set_status(f"⚠ 已匯入 {os.path.basename(path)},但靜態檢查未過:\n{msg}", "#FFCA28")
            self.log_message(f"【自訂策略】已匯入腳本: {path}")

        def _export_py():
            name = (e_name.get().strip() or 'strategy')
            safe = "".join(ch for ch in name if ch.isalnum() or ch in ('_', '-', '.')) or 'strategy'
            path = filedialog.asksaveasfilename(
                parent=dlg, title="匯出策略腳本 (.py)",
                initialdir=self._strategy_dir(), initialfile=f"{safe}.py",
                defaultextension=".py",
                filetypes=[("Python 策略腳本", "*.py")])
            if not path:
                return
            try:
                header = (f"# 策略名稱: {name}\n"
                          f"# 商品: {e_sym.get().strip().upper()}  週期: {cb_tf.get()}  "
                          f"數量: {e_qty.get().strip()}\n"
                          f"# 自訂參數: {e_params.get().strip()}\n"
                          f"# 由 StockBuild 匯出於 {datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(header + txt.get('1.0', 'end-1c'))
            except Exception as e:
                _set_status(f"✗ 匯出失敗: {type(e).__name__}: {e}", "#FF5252"); return
            _set_status(f"✅ 已匯出至 {path}", "#00E676")
            self.log_message(f"【自訂策略】腳本已匯出: {path}")

        tk.Button(foot2, text="📂 匯入 .py", bg="#455A64", fg="white", relief="flat",
                  font=('微軟正黑體', 9), padx=10, pady=2, command=_import_py).pack(side=tk.LEFT, padx=4)
        tk.Button(foot2, text="💾 匯出 .py", bg="#455A64", fg="white", relief="flat",
                  font=('微軟正黑體', 9), padx=10, pady=2, command=_export_py).pack(side=tk.LEFT, padx=4)
        tk.Label(foot2, text=f"(腳本資料夾:{self._strategy_dir()})", bg="#1A2026",
                 fg="#8A99AD", font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=6)

        tk.Button(foot, text="🤖 AI 產生 (Claude)", bg="#FF8F00", fg="black", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=14, pady=4,
                  command=lambda: self._open_ai_helper_dialog(dlg, txt, _set_status)).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="🧪 試跑一次", bg="#AB47BC", fg="white", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=14, pady=4, command=_test_run).pack(side=tk.LEFT, padx=6)
        if readonly:
            tk.Button(foot, text="唯讀無法儲存", bg="#2A323D", fg="#8A99AD", relief="flat", state=tk.DISABLED,
                      font=('微軟正黑體', 11, 'bold'), padx=18, pady=4).pack(side=tk.LEFT, padx=6)
        else:
            tk.Button(foot, text="儲存策略", bg="#29B6F6", fg="black", relief="flat",
                      font=('微軟正黑體', 11, 'bold'), padx=18, pady=4, command=_save).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="關閉" if readonly else "取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 11), padx=18, pady=4, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    AI_CONFIG_FILE = "ai_config.json"
    TELEGRAM_CONFIG_FILE = "telegram_config.json"
    STRATEGY_SCRIPT_DIR = "strategies"
    BT_CACHE_TTL = 600          # 【ADR-052】回測下載快取有效期 (秒)
    BT_CACHE_MAX = 4            # 最多保留幾筆 (原始分K很佔記憶體)

    def _bt_download_cache_get(self, key):
        """【ADR-052】回測下載快取:同商品同範圍 10 分鐘內重跑免重抓。"""
        try:
            c = getattr(self, '_bt_dl_cache', None)
            if not c:
                return None
            item = c.get(key)
            if not item or (time.time() - item['t']) > self.BT_CACHE_TTL:
                return None
            return item['df']
        except Exception:
            return None

    def _bt_download_cache_put(self, key, df):
        try:
            if not hasattr(self, '_bt_dl_cache') or self._bt_dl_cache is None:
                self._bt_dl_cache = {}
            self._bt_dl_cache[key] = {'t': time.time(), 'df': df}
            while len(self._bt_dl_cache) > self.BT_CACHE_MAX:
                oldest = min(self._bt_dl_cache, key=lambda k: self._bt_dl_cache[k]['t'])
                self._bt_dl_cache.pop(oldest, None)
        except Exception:
            pass

    def _cost_params(self):
        """【ADR-050】交易成本參數 (可日後開放使用者在設定調整券商折扣)。
        目前回傳 None = 使用 core/cost_model 的台灣標準預設費率。"""
        return getattr(self, '_user_cost_params', None)

    def _strategy_dir(self):
        """【ADR-051】策略腳本資料夾 (一個策略 = 一個 .py 檔),不存在就建立。"""
        d = os.path.abspath(self.STRATEGY_SCRIPT_DIR)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return os.path.abspath('.')
        return d


    def _open_ai_helper_dialog(self, parent_dlg, txt_widget, editor_status_cb=None):
        """【ADR-049】AI 策略助手:中文描述 → Claude API 產生 on_bar 程式碼。
        產生的程式碼不被特殊對待:一樣過靜態檢查、子行程執行、三層下單防護;
        使用者仍須自己讀懂 + 試跑 + 回測 + 模擬後才考慮實單。"""
        cfg = config_store.load_ai_config(self.AI_CONFIG_FILE)
        dlg = tk.Toplevel(parent_dlg)
        dlg.title("🤖 AI 策略助手 (Claude)")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 640, 520)
        dlg.transient(parent_dlg)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass
        tk.Label(dlg, text=("用中文描述你的策略,AI 會產生 on_bar(ctx) 程式碼填入編輯器。\n"
                            "⚠ 產生的程式碼請務必自己讀懂,並走 試跑 → 回測 → 模擬 流程後再考慮實單。"),
                 bg="#2A1215", fg="#FF8A80", font=('微軟正黑體', 9), wraplength=600,
                 justify='left').pack(fill=tk.X, padx=10, pady=(10, 4))
        row = tk.Frame(dlg, bg="#1A2026"); row.pack(fill=tk.X, padx=12, pady=2)
        tk.Label(row, text="API Key", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=0, column=0, sticky='w')
        e_key = tk.Entry(row, width=42, bg="#2A323D", fg="white", show="*")
        e_key.insert(0, cfg.get('api_key', '')); e_key.grid(row=0, column=1, padx=4, sticky='w')
        tk.Label(row, text="(console.anthropic.com 取得,按 token 計費,只存本機)", bg="#1A2026",
                 fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=1, column=1, sticky='w')
        tk.Label(row, text="模型", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=2, column=0, sticky='w', pady=(4, 0))
        e_model = tk.Entry(row, width=28, bg="#2A323D", fg="white")
        e_model.insert(0, cfg.get('model') or ai_helper.DEFAULT_MODEL); e_model.grid(row=2, column=1, padx=4, sticky='w', pady=(4, 0))
        tk.Label(dlg, text="策略描述 (商品/週期在編輯器設定,這裡描述進出場邏輯與參數):",
                 bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(8, 0))
        t_desc = tk.Text(dlg, height=9, bg="#0D1117", fg="#E6EDF3", insertbackground="white",
                         font=('微軟正黑體', 10), wrap='word')
        t_desc.pack(fill=tk.BOTH, expand=True, padx=12, pady=(2, 4))
        t_desc.insert('1.0', "範例:收盤價跌破 60 日均線且 RSI(14) 低於 30 時做多;"
                             "站回 60 日均線或獲利 5% 平倉;虧損 2% 停損。均線與 RSI 週期要可調。")
        lbl_ai = tk.Label(dlg, text="", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9),
                          wraplength=600, justify='left', anchor='w')
        lbl_ai.pack(fill=tk.X, padx=12)

        def _ai_status(msg, color):
            try:
                if dlg.winfo_exists():
                    lbl_ai.config(text=msg, fg=color)
            except Exception:
                pass

        def _apply_code(code, warn_msg):
            try:
                if not txt_widget.winfo_exists():
                    return
                txt_widget.delete('1.0', tk.END)
                txt_widget.insert('1.0', code)
            except Exception:
                return
            if warn_msg:
                _ai_status(f"⚠ 已填入程式碼,但靜態檢查有問題,請修正後再試跑:\n{warn_msg}", "#FFCA28")
                if editor_status_cb:
                    editor_status_cb(f"⚠ AI 程式碼未過檢查:{warn_msg}", "#FFCA28")
            else:
                _ai_status("✅ 已產生並填入編輯器 (通過靜態檢查)。請讀懂程式碼後按「🧪 試跑一次」。", "#00E676")
                if editor_status_cb:
                    editor_status_cb("✅ AI 產生的程式碼已填入,請讀懂後試跑 → 回測 → 模擬。", "#00E676")

        def _generate():
            key = e_key.get().strip()
            model = e_model.get().strip() or ai_helper.DEFAULT_MODEL
            desc = t_desc.get('1.0', 'end-1c').strip()
            if not key:
                _ai_status("✗ 請先填入 Anthropic API Key。", "#FF5252")
                return
            if not desc:
                _ai_status("✗ 請先描述你的策略。", "#FF5252")
                return
            try:
                config_store.save_ai_config(self.AI_CONFIG_FILE, key, model)
            except Exception:
                pass
            _ai_status("⏳ 產生中 (呼叫 Claude API,約 5~30 秒)...", "#FFCA28")

            def _done(ok, payload):
                if ok:
                    code, warn = payload
                    self.safe_after(0, _apply_code, code, warn)
                    self.safe_after(0, self.log_message, "【AI 策略助手】已產生程式碼並填入編輯器。")
                else:
                    self.safe_after(0, _ai_status, f"❌ {payload}", "#FF5252")
                    self.safe_after(0, self.log_message, f"【AI 策略助手】產生失敗: {payload}")
            threading.Thread(target=self._ai_generate_worker, args=(desc, key, model, _done),
                             daemon=True).start()

        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(pady=8)
        tk.Button(btns, text="✨ 產生策略程式碼", bg="#FF8F00", fg="black", relief="flat",
                  font=('微軟正黑體', 11, 'bold'), padx=18, pady=4, command=_generate).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 11), padx=18, pady=4, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _ai_generate_worker(self, description, api_key, model, done_cb):
        """背景執行緒:呼叫 Anthropic Messages API (urllib,零新依賴)。
        成功 → done_cb(True, (code, warn_msg));失敗 → done_cb(False, 錯誤訊息)。"""
        import urllib.request
        import urllib.error
        try:
            payload = ai_helper.build_messages_payload(description, model=model)
            req = urllib.request.Request(
                ai_helper.API_URL,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json',
                         'x-api-key': api_key,
                         'anthropic-version': ai_helper.API_VERSION},
                method='POST')
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read()
            code = ai_helper.extract_code(body)
            ok, msg = custom_strategy.validate_source(code)
            done_cb(True, (code, "" if ok else msg))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode('utf-8', errors='replace')[:300]
            except Exception:
                detail = ''
            hint = ""
            if e.code == 401:
                hint = " (API Key 無效或未啟用)"
            elif e.code == 429:
                hint = " (額度/速率限制,稍後再試)"
            done_cb(False, f"HTTP {e.code}{hint}: {detail}")
        except Exception as e:
            done_cb(False, f"{type(e).__name__}: {str(e)[:200]}")

    def _qt_custom_test_worker(self, strat, status_cb=None):
        def _notify(msg, ok):
            if status_cb:
                try: status_cb(msg, ok)
                except Exception: pass
        try:
            contract, asset_type = self._qt_resolve(strat)
            if contract is None:
                self.safe_after(0, self.log_message, f"【自訂策略-試跑】合約解析失敗: {strat.get('symbol')}")
                _notify(f"✗ 合約解析失敗: {strat.get('symbol')} (代碼打錯?未登入?)", False)
                return
            # 【ADR-122】試跑在自己的 worker 執行緒上,可以原地把分段下載做完
            df = self._qt_fetch_closed_bars(strat, contract, asset_type, allow_blocking=True)
            if df is None or df.empty:
                self.safe_after(0, self.log_message, "【自訂策略-試跑】取不到K線資料。")
                _notify("✗ 取不到K線資料。", False)
                return
            decision = self._run_custom_in_subprocess(strat, df, 'FLAT')
            ok_msg = (f"✅ 執行成功!目前 (FLAT 狀態) 會回傳決策: {decision} "
                      f"(BUY=開多/SELL=開空或平多/CLOSE=平倉/HOLD=不動作)。可再按「🔬 回測」看完整績效。")
            self.safe_after(0, self.log_message, f"【自訂策略-試跑】{ok_msg}")
            _notify(ok_msg, True)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【自訂策略-試跑】❌ 執行失敗: {e}")
            _notify(f"❌ 執行失敗: {e}", False)

    def _qt_open_chukuangren_editor(self, strategy, readonly=False):
        """【新ADR】「終極波段策略」專屬編輯器:看加權指數(A)訊號,做自選商品(B),
        含隔天中午12:00二次確認的進出場/停損/點數移動停利→SMA20移動停利。
        獨立編輯器 (不沿用 _qt_open_editor 的條件組合UI),因為這個策略的邏輯是
        寫死的狀態機 (core/chukuangren_band.py),不是 AND/OR 條件拼出來的。"""
        is_new = strategy is None
        s = chukuangren_band.default_strategy() if is_new else json.loads(json.dumps(strategy))
        dlg = tk.Toplevel(self)
        title_suffix = " (唯讀 - 策略執行中)" if readonly else ""
        dlg.title("終極波段策略" + ("" if is_new else f" — {s.get('name')}{title_suffix}"))
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 660, 780)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass
        tk.Label(dlg, text=("看加權指數走勢決定進出場方向,實際下單另一個你自選的商品 (股票/期貨)。"
                            "進場/停損/停利訊號一律「隔天中午12:00 (5分K收盤價) 二次確認」，"
                            "當天觸發只是「待確認」，隔天不成立就作廢、繼續等下一次訊號。"
                            "確認成立後不會立刻下單，會等約1分鐘 (12:01) 依當時最新價才真正送單，"
                            "確認與下單是兩個不同時間點。"),
                 bg="#12181F", fg="#FFCA28", font=('微軟正黑體', 9), wraplength=600,
                 justify='left').pack(side=tk.TOP, fill=tk.X, padx=10, pady=(10, 4))

        # 【版面修正】不論之後再新增多少參數/區塊,「儲存/取消」按鈕跟狀態列
        # 一定要固定在視窗最下方看得到——這裡先用 side=BOTTOM pack 好佔住
        # 底部空間,下面才建可捲動區塊放其餘所有內容 (見 _make_scrollable);
        # 內容再怎麼加,超出視窗高度的部分用滑鼠滾輪/捲軸捲動,不會把按鈕
        # 擠出視窗外或跟內容疊在一起。_save 這裡還沒定義 (在後面),用
        # lambda 延後到真正點擊時才查找,不影響按鈕先建立。
        lbl_status = tk.Label(dlg, text="", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9),
                              wraplength=600, justify='left', anchor='w')
        lbl_status.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 4))

        def _set_status(msg, color="#8A99AD"):
            try:
                if dlg.winfo_exists():
                    lbl_status.config(text=msg, fg=color)
            except Exception:
                pass

        foot = tk.Frame(dlg, bg="#1A2026"); foot.pack(side=tk.BOTTOM, pady=8)
        if readonly:
            tk.Button(foot, text="唯讀無法儲存", bg="#2A323D", fg="#8A99AD", relief="flat", state=tk.DISABLED,
                      font=('微軟正黑體', 11, 'bold'), padx=18, pady=4).pack(side=tk.LEFT, padx=6)
        else:
            tk.Button(foot, text="儲存策略", bg="#29B6F6", fg="black", relief="flat",
                      font=('微軟正黑體', 11, 'bold'), padx=18, pady=4,
                      command=lambda: _save()).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="關閉" if readonly else "取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 11), padx=18, pady=4, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

        body = self._make_scrollable(dlg)

        def _lbl(p, t): return tk.Label(p, text=t, bg="#1A2026", fg="white", font=('微軟正黑體', 9))
        def _ent(p, v, w=10):
            e = tk.Entry(p, width=w, bg="#2A323D", fg="white", justify="center"); e.insert(0, str(v)); return e

        top = tk.Frame(body, bg="#1A2026"); top.pack(fill=tk.X, padx=12, pady=2)
        _live_get = self._qt_build_live_account_row(body, s)  # 【ADR-110階段2】
        _lbl(top, "策略名稱").grid(row=0, column=0, sticky='w')
        e_name = _ent(top, s.get('name', chukuangren_band.STRATEGY_NAME), 20); e_name.grid(row=0, column=1, padx=4, columnspan=2, sticky='w')

        _lbl(top, "執行商品(做B)").grid(row=1, column=0, sticky='w', pady=(8, 0))
        e_sym = _ent(top, s.get('symbol', ''), 10); e_sym.grid(row=1, column=1, padx=4, pady=(8, 0))
        _lbl(top, "交易種類").grid(row=1, column=2, sticky='w', padx=(8, 0), pady=(8, 0))
        cb_tt = ttk.Combobox(top, values=list(strategy_engine.TRADE_TYPES), width=7, state='readonly', style="BlackText.TCombobox")
        cb_tt.set(strategy_engine.trade_type_of(s)); cb_tt.grid(row=1, column=3, padx=4, pady=(8, 0))
        _lbl(top, "數量").grid(row=1, column=4, sticky='w', padx=(8, 0), pady=(8, 0))
        e_qty = _ent(top, s.get('qty', 1), 6); e_qty.grid(row=1, column=5, padx=4, pady=(8, 0))
        tk.Label(top, text="← 也可直接點左側自選股帶入(自動判斷股票/期貨)", bg="#1A2026",
                 fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=2, column=0, columnspan=6, sticky='w')
        lbl_cname = tk.Label(top, text="", bg="#1A2026", fg="#29B6F6", font=('微軟正黑體', 9, 'bold'))
        lbl_cname.grid(row=3, column=0, columnspan=6, sticky='w', pady=(2, 0))
        def _clook(*_a):
            name, ok = self._resolve_strategy_symbol_name(cb_tt.get(), e_sym.get())
            if ok:
                lbl_cname.config(text=f"✓ 已確認商品:{name}", fg="#00E676")
            elif e_sym.get().strip():
                lbl_cname.config(text="✗ 查無此代碼 (需先登入)", fg="#FF5252")
            else:
                lbl_cname.config(text="")
        e_sym.bind('<KeyRelease>', _clook); e_sym.bind('<FocusOut>', _clook)
        cb_tt.bind('<<ComboboxSelected>>', _clook)
        _bt = (dlg, e_sym, cb_tt, _clook, 'B')
        self._qt_editor_symbol_target = _bt
        e_sym.bind('<FocusIn>', lambda *_a: setattr(self, '_qt_editor_symbol_target', _bt), add='+')

        _lbl(top, "方向").grid(row=4, column=0, sticky='w', pady=(8, 0))
        cb_dir = ttk.Combobox(top, values=list(chukuangren_band.DIRECTIONS), width=7, state='readonly', style="BlackText.TCombobox")
        cb_dir.set(chukuangren_band.direction_of(s)); cb_dir.grid(row=4, column=1, padx=4, pady=(8, 0), sticky='w')
        _lbl(top, "模式").grid(row=4, column=2, sticky='w', padx=(8, 0), pady=(8, 0))
        cb_mode = ttk.Combobox(top, values=['模擬', '實單'], width=7, state='readonly', style="BlackText.TCombobox")
        cb_mode.set(s.get('mode', '模擬')); cb_mode.grid(row=4, column=3, padx=4, pady=(8, 0), sticky='w')
        tk.Label(top, text="(做空僅限期貨)", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 8)).grid(row=4, column=4, columnspan=2, sticky='w', padx=(8, 0), pady=(8, 0))

        # 【ADR-135】委託方式改用共用函式 (原本這裡是第三份複製)
        _pt = self._qt_build_price_type_row(top, s, cb_tt)
        _pt['frame'].grid(row=5, column=0, columnspan=6, sticky='w', pady=(8, 0))

        # 【新ADR 多帳戶】模擬成交要記進哪個模擬帳戶
        _lbl(top, "模擬帳戶").grid(row=6, column=0, sticky='w', pady=(8, 0))
        _acct_choices = self._qt_account_choices()
        cb_acct2 = ttk.Combobox(top, width=16, state='readonly', style="BlackText.TCombobox",
                                values=[name for _aid, name in _acct_choices])
        _ids2 = [aid for aid, _name in _acct_choices]
        _cur_aid = strategy_engine.account_id_of(s)
        cb_acct2.current(_ids2.index(_cur_aid) if _cur_aid in _ids2 else 0)
        cb_acct2.grid(row=6, column=1, padx=4, pady=(8, 0), sticky='w')
        tk.Label(top, text="(只在「模式=模擬」時有意義)", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 8)).grid(row=6, column=2, columnspan=3, sticky='w', padx=(8, 0), pady=(8, 0))

        watch_fr = tk.Frame(body, bg="#12181F"); watch_fr.pack(fill=tk.X, padx=12, pady=(8, 4))
        tk.Label(watch_fr, text="看盤(A) — 固定看指數,決定進出場方向 (不下單)", bg="#12181F",
                 fg="#29B6F6", font=('微軟正黑體', 9, 'bold')).grid(row=0, column=0, columnspan=4, sticky='w', pady=(4, 2))
        tk.Label(watch_fr, text="指數代碼", bg="#12181F", fg="white", font=('微軟正黑體', 9)).grid(row=1, column=0, sticky='w', padx=(4, 0))
        e_wsym = tk.Entry(watch_fr, width=10, bg="#2A323D", fg="white", justify="center")
        e_wsym.insert(0, s.get('watch_symbol', '') or '^TWII'); e_wsym.grid(row=1, column=1, padx=4)
        tk.Label(watch_fr, text="(預設加權^TWII,可改櫃買^TWOII等指數;點左側自選股點指數可帶入)",
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=1, column=2, columnspan=2, sticky='w', padx=(6, 0))
        # 【ADR-077 相容】on_watchlist_select 點自選股帶入時,固定會呼叫 cb_tt_ref.set(tt) ——
        # 本策略的看A固定是指數,不需要讓使用者改,但仍要給一個真的 Combobox 承接這個呼叫,
        # 不然點自選股會在 cb_tt_ref.set(tt) 那行拋例外 (被外層 except 悄悄吞掉,但label不會更新)。
        cb_wtt_a = ttk.Combobox(watch_fr, values=['指數'], width=6, state='readonly', style="BlackText.TCombobox")
        cb_wtt_a.set('指數')  # 不 grid 顯示,純粹承接 _qt_editor_symbol_target 的 set() 呼叫
        _at = (dlg, e_wsym, cb_wtt_a, lambda *_a: None, 'A')
        e_wsym.bind('<FocusIn>', lambda *_a: setattr(self, '_qt_editor_symbol_target', _at), add='+')

        # 參數區:X/C/F 兩方向共用;Y 只做多顯示、S1 只做空顯示 (ADR-085)。
        # 依「方向」下拉選單動態切換要顯示的那個停損參數。
        # 【ADR-129】原本做多顯示 Y+Z、做空顯示 S1+S2 (觸發線 + 確認線)。使用者
        # 實測回報「這兩個應該是要一樣,不需要分兩個,只需填一個 S1 就可以」——
        # 停損的觸發與隔天12:00 確認改用同一個點位,所以這裡各只留一個欄位。
        # z/s2 仍在資料格式裡 (讀得懂舊檔),值由 _collect() 鏡射、由
        # chukuangren_band.params_of() 覆寫,兩層都指向觸發值。
        pcontainer = tk.Frame(body, bg="#1A2026"); pcontainer.pack(fill=tk.X, padx=12, pady=(6, 4))
        tk.Label(pcontainer, text="參數 (加權指數點位/點數,依你的判斷自行設定)", bg="#1A2026",
                 fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor='w', pady=(0, 4))
        param_entries = {}

        def _mk_param_rows(parent, keys):
            fr = tk.Frame(parent, bg="#1A2026")
            for i, k in enumerate(keys):
                rr = i * 2  # 每個參數佔兩列:第一列標籤+輸入框,第二列灰色中文說明
                _lbl(fr, chukuangren_band.PARAM_LABELS[k]).grid(row=rr, column=0, sticky='w', pady=(4, 0))
                e = _ent(fr, s.get(f'ck_{k}', 0.0), 10)
                e.grid(row=rr, column=1, padx=(6, 16), pady=(4, 0), sticky='w')
                param_entries[k] = e
                tk.Label(fr, text=chukuangren_band.PARAM_HELP[k], bg="#1A2026", fg="#8A99AD",
                         font=('微軟正黑體', 8), wraplength=580, justify='left').grid(
                         row=rr + 1, column=0, columnspan=2, sticky='w', padx=(4, 0), pady=(0, 2))
            return fr

        common_fr = _mk_param_rows(pcontainer, ('x', 'c', 'f')); common_fr.pack(fill=tk.X, anchor='w')
        long_fr = _mk_param_rows(pcontainer, ('y',))
        short_fr = _mk_param_rows(pcontainer, ('s1',))

        def _refresh_dir(*_a):
            long_fr.pack_forget(); short_fr.pack_forget()
            if cb_dir.get() == '做空':
                short_fr.pack(fill=tk.X, anchor='w', after=common_fr)
            else:
                long_fr.pack(fill=tk.X, anchor='w', after=common_fr)
        cb_dir.bind('<<ComboboxSelected>>', _refresh_dir)
        _refresh_dir()

        # 【ADR-120】原本這裡有一整區「📈 盤勢/型態提醒」。使用者要求把盤勢
        # 判斷放到主圖 (看盤時就看得到,不必開著策略),因此整區移到主圖的
        # 【盤勢判斷】⚙ 設定 (見 open_regime_settings)。這個策略回到只做
        # 進出場邏輯。

        def _collect():
            s['kind'] = chukuangren_band.KIND
            s['name'] = e_name.get().strip() or chukuangren_band.STRATEGY_NAME
            s['symbol'] = e_sym.get().strip().upper()
            s['trade_type'] = cb_tt.get()
            s['market'] = '台期貨' if cb_tt.get() == '期貨' else '台股'
            s['direction'] = cb_dir.get() if cb_dir.get() in chukuangren_band.DIRECTIONS else '做多'
            try:
                s['qty'] = int(e_qty.get().strip())
            except (TypeError, ValueError):
                s['qty'] = 0
            s['mode'] = cb_mode.get()
            s['price_type'] = _pt['get']()
            s['account_id'] = _ids2[cb_acct2.current()] if cb_acct2.current() >= 0 else 'default'
            # 【ADR-110階段2】實單要下到哪一家券商的哪一個帳號
            s['broker'], s['broker_account'] = _live_get()
            s['watch_enabled'] = True
            s['watch_symbol'] = e_wsym.get().strip().upper() or '^TWII'
            s['watch_trade_type'] = '指數'
            # 【ADR-128】存真正的訊號週期 (日K)。舊版寫 '5分K' 是拿它當評估節拍器
            # 用,語意錯誤 —— 詳見 chukuangren_band.default_strategy() 的說明。
            # 真正的防線在 strategy_engine.DAY_SIGNAL_KINDS (覆寫存檔值),
            # 這裡是讓舊策略「下次儲存時」資料本身也洗乾淨 (同 ADR-123 的做法)。
            s['watch_timeframe'] = strategy_engine.DAY_SIGNAL_TIMEFRAME
            for k in chukuangren_band.PARAM_KEYS:
                # 【ADR-129】z / s2 已經沒有輸入框了 (併入 y / s1),所以這裡不能
                # 直接 param_entries[k] —— 會 KeyError。用 .get() 取,取不到就
                # 留給下面的鏡射處理。
                e = param_entries.get(k)
                if e is None:
                    continue
                try:
                    s[f'ck_{k}'] = float(e.get().strip())
                except (TypeError, ValueError):
                    s[f'ck_{k}'] = 0.0
            # 停損確認點位鏡射成觸發點位,讓存檔資料本身也一致 (真正的防線在
            # params_of(),這裡只是不留下自相矛盾的舊值,同 ADR-123 的做法)。
            for _dst, _src in chukuangren_band.MERGED_STOP_PAIRS:
                s[f'ck_{_dst}'] = s.get(f'ck_{_src}', 0.0)
            s['entry'] = s.get('entry') or []
            s['exit_signals'] = s.get('exit_signals') or []
            # 【ADR-120】pattern_* 幾個欄位已移除 (盤勢/型態搬到主圖【盤勢判斷】)。
            # 舊策略檔裡殘留的 pattern_* 不再被讀取,留著也無害,不主動清掉——
            # 萬一使用者想降版回舊程式,資料還在。
            # 【ADR-123】泛用停損/停利歸零:本策略的出場完全由 X/C/F/Y/Z 決定,
            # 編輯器裡也沒有這幾個欄位。舊策略存檔時順手洗乾淨 (真正的防線在
            # strategy_engine.OWN_EXIT_KINDS,這裡只是讓資料本身也一致)。
            for _k in ('stop_loss_pct', 'take_profit_pct', 'stop_loss_abs', 'take_profit_abs'):
                s[_k] = 0.0
            return s

        def _save():
            strat = _collect()
            ok, msg = chukuangren_band.validate(strat)
            if not ok:
                _set_status(f"✗ 儲存失敗:\n{msg}", "#FF5252")
                self.log_message(f"【終極波段策略】儲存失敗: {msg}")
                return
            if strat['mode'] == '實單' and is_new:
                strat['mode'] = '模擬'
                self.log_message("【自動交易-安全】新策略一律先以「模擬」模式儲存;請先觀察模擬訊號合理後,再編輯改為實單。")
            if is_new:
                strat['enabled'] = False
                self.strategies.append(strat)
                self.strategy_runtimes[strat['id']] = strategy_engine.new_runtime()
            else:
                for i, x in enumerate(self.strategies):
                    if x['id'] == strat['id']:
                        self.strategies[i] = strat; break
            self._qt_save(); self._qt_save_state(); self._qt_refresh_tree()
            self.log_message(f"【終極波段策略】「{strat['name']}」已儲存 ({strat['mode']}),已加入「量化交易」分頁清單。")
            messagebox.showinfo("儲存成功", f"策略「{strat['name']}」已儲存 ({strat['mode']} 模式)。", parent=dlg)
            dlg.destroy()

        _clook()

    def _qt_build_watch_panel(self, parent, s):
        """【ADR-074】建立「看A做B」設定面板 (內建/自訂策略編輯器共用)。
        回傳 {'get': callable} —— 呼叫 get() 拿到 {watch_enabled/watch_symbol/
        watch_trade_type/watch_timeframe} 併進策略。"""
        wf = tk.Frame(parent, bg="#12181F"); wf.pack(fill=tk.X, padx=12, pady=(8, 0))
        var_watch = tk.BooleanVar(value=bool(s.get('watch_enabled', False)))
        row0 = tk.Frame(wf, bg="#12181F"); row0.pack(fill=tk.X, pady=(4, 0))
        tk.Checkbutton(row0, text="👁 看A做B (用另一個商品的訊號來下單這一檔)", variable=var_watch,
                       bg="#12181F", fg="#29B6F6", selectcolor="#2A323D", activebackground="#12181F",
                       font=('微軟正黑體', 9, 'bold')).pack(side=tk.LEFT)
        tk.Label(row0, text="上方『商品代碼』= 做B (實際下單);指數(加權/櫃買)不能做B,只能當看A。",
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=(8, 0))
        rowA = tk.Frame(wf, bg="#12181F"); rowA.pack(fill=tk.X, pady=(2, 4))
        tk.Label(rowA, text="　看A 商品代碼", bg="#12181F", fg="#29B6F6", font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_wsym = tk.Entry(rowA, width=12, bg="#2A323D", fg="white", justify="center")
        e_wsym.insert(0, s.get('watch_symbol', '')); e_wsym.pack(side=tk.LEFT, padx=4)
        tk.Label(rowA, text="種類", bg="#12181F", fg="white", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(8, 0))
        cb_wtt = ttk.Combobox(rowA, values=list(strategy_engine.WATCH_TRADE_TYPES), width=6,
                              state='readonly', style="BlackText.TCombobox")
        cb_wtt.set(s.get('watch_trade_type', '指數')); cb_wtt.pack(side=tk.LEFT, padx=4)
        tk.Label(rowA, text="看A週期", bg="#12181F", fg="white", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(8, 0))
        # 【新ADR 自訂週期】state='normal' (可編輯) 而非 readonly:下拉仍列出常用
        # 預設值方便點選,但也可以直接打字輸入自訂週期 (例如 45分K、3時K),
        # 存檔前 validate_strategy() 的 is_valid_timeframe() 會擋下格式不正確的輸入。
        cb_wtf = ttk.Combobox(rowA, values=list(strategy_engine.VALID_TIMEFRAMES), width=6,
                              state='normal', style="BlackText.TCombobox")
        cb_wtf.set(s.get('watch_timeframe', '30分K')); cb_wtf.pack(side=tk.LEFT, padx=4)
        tk.Label(rowA, text="(可自訂輸入,如45分K/3時K;點一下此欄再點左側自選股即可帶入A並自動判斷指數/期貨/股票;訊號看A週期,下單看B最新價)",
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=(8, 0))
        lbl_wname = tk.Label(wf, text="", bg="#12181F", fg="#00E676", font=('微軟正黑體', 8))
        lbl_wname.pack(anchor='w', padx=4, pady=(0, 4))
        def _wlook(*_a):
            if not var_watch.get():
                lbl_wname.config(text=""); return
            wsym = e_wsym.get().strip()
            if cb_wtt.get() == '指數':
                lbl_wname.config(text=(f"✓ 看A(指數):{wsym}" if wsym
                                       else "請填看A的指數代碼 (加權^TWII / 櫃買^TWOII)"),
                                 fg="#00E676" if wsym else "#FF5252")
                return
            nm, ok = self._resolve_strategy_symbol_name(cb_wtt.get(), wsym)
            if ok:
                lbl_wname.config(text=f"✓ 看A:{nm}", fg="#00E676")
            elif wsym:
                lbl_wname.config(text="✗ 看A 查無此代碼 (需先登入;種類要選對)", fg="#FF5252")
            else:
                lbl_wname.config(text="")
        e_wsym.bind('<KeyRelease>', _wlook); e_wsym.bind('<FocusOut>', _wlook)
        cb_wtt.bind('<<ComboboxSelected>>', _wlook)
        var_watch.trace_add('write', lambda *_a: _wlook())
        # 【ADR-077】看A 商品代碼也支援「點自選股帶入」:點進這個欄位 (或勾了看A做B)
        # 就把它設為作用中目標,之後點左側自選股會帶入 A 並自動判斷 指數/期貨/股票。
        _at = (parent, e_wsym, cb_wtt, _wlook, 'A')
        def _activate_A(*_a):
            self._qt_editor_symbol_target = _at
        e_wsym.bind('<FocusIn>', _activate_A, add='+')
        # 勾選「看A做B」的當下,順手把作用中目標切到 A,方便馬上點自選股帶 A。
        var_watch.trace_add('write', lambda *_a: (_activate_A() if var_watch.get() else None))
        _wlook()
        return {'get': lambda: {
            'watch_enabled': bool(var_watch.get()),
            'watch_symbol': e_wsym.get().strip().upper(),
            'watch_trade_type': cb_wtt.get(),
            'watch_timeframe': cb_wtf.get(),
        }}

    def _qt_open_editor(self, strategy, readonly=False):
        """新增/編輯策略對話框:基本參數 + 條件建構器 (進場AND / 出場OR)。"""
        is_new = strategy is None
        s = strategy_engine.new_strategy() if is_new else json.loads(json.dumps(strategy))
        dlg = tk.Toplevel(self)
        title_suffix = " (唯讀 - 策略執行中)" if readonly else ""
        dlg.title("新增策略" if is_new else f"編輯策略 — {s.get('name')}{title_suffix}")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 800, 700)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass

        def _lbl(parent, txt):
            return tk.Label(parent, text=txt, bg="#1A2026", fg="white", font=('微軟正黑體', 9))
        def _ent(parent, val, w=10):
            e = tk.Entry(parent, width=w, bg="#2A323D", fg="white", justify="center")
            e.insert(0, str(val)); return e

        top = tk.Frame(dlg, bg="#1A2026"); top.pack(fill=tk.X, padx=12, pady=(10, 2))
        _live_get = self._qt_build_live_account_row(dlg, s)   # 【ADR-110階段2】
        top.columnconfigure(6, weight=1)
        _lbl(top, "策略名稱").grid(row=0, column=0, sticky='w')
        e_name = _ent(top, s.get('name', ''), 16); e_name.grid(row=0, column=1, padx=4)
        _lbl(top, "商品代碼").grid(row=0, column=2, sticky='w', padx=(10, 0))
        e_sym = _ent(top, s.get('symbol', ''), 10); e_sym.grid(row=0, column=3, padx=4)
        lbl_name = tk.Label(top, text="", bg="#1A2026", fg="#29B6F6", font=('微軟正黑體', 9, 'bold'))
        # 【ADR-138】columnspan 由 7 改 2:原本橫跨整列,會蓋到同一列 col2 起的
        # 「籌碼允許讀當日」勾選框 (目前因為文字短所以看起來沒事,是潛在撞格)。
        lbl_name.grid(row=5, column=0, columnspan=2, sticky='w', pady=(2, 0))
        _lbl(top, "交易種類").grid(row=0, column=4, sticky='w', padx=(10, 0))
        tk.Label(top, text="← 也可直接點左側自選股帶入(自動判斷股票/期貨)", bg="#1A2026",
                 fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=0, column=6, sticky='w', padx=(10, 0))
        cb_tt = ttk.Combobox(top, values=list(strategy_engine.TRADE_TYPES), width=7, state='readonly', style="BlackText.TCombobox")
        cb_tt.set(strategy_engine.trade_type_of(s)); cb_tt.grid(row=0, column=5, padx=4)
        def _lookup_name(*_a):
            name, ok = self._resolve_strategy_symbol_name(cb_tt.get(), e_sym.get())
            if ok:
                lbl_name.config(text=f"✓ 已確認商品:{name}", fg="#00E676")
            elif e_sym.get().strip():
                lbl_name.config(text="✗ 查無此代碼 (請確認交易種類與代碼是否正確;需先登入)", fg="#FF5252")
            else:
                lbl_name.config(text="")
            try:
                lbl_qty.config(text=f"數量({strategy_engine.qty_unit_of({'trade_type': cb_tt.get()})})")
            except Exception:
                pass
        e_sym.bind('<KeyRelease>', _lookup_name)
        e_sym.bind('<FocusOut>', _lookup_name)
        cb_tt.bind('<<ComboboxSelected>>', _lookup_name)
        # 【策略編輯器帶入 / ADR-077】記住做B (執行商品) 欄位。點自選股會帶進
        # 「目前作用中」的欄位;做B 是預設,點進 B 的代碼框也會切回 B。
        _bt = (dlg, e_sym, cb_tt, _lookup_name, 'B')
        self._qt_editor_symbol_target = _bt
        e_sym.bind('<FocusIn>', lambda *_a: setattr(self, '_qt_editor_symbol_target', _bt), add='+')
        _lbl(top, "週期(可自訂)").grid(row=1, column=0, sticky='w', pady=(6, 0))
        # 【新ADR 自訂週期】state='normal':可從下拉點選常用值,也可直接打字
        # 輸入自訂週期 (例如 45分K、3時K),存檔前 validate_strategy() 會擋格式錯誤。
        cb_tf = ttk.Combobox(top, values=list(strategy_engine.VALID_TIMEFRAMES), width=7, state='normal', style="BlackText.TCombobox")
        cb_tf.set(s.get('timeframe', '5分K')); cb_tf.grid(row=1, column=1, padx=4, pady=(6, 0))
        _lbl(top, "方向").grid(row=1, column=2, sticky='w', padx=(10, 0), pady=(6, 0))
        cb_dir = ttk.Combobox(top, values=['做多', '做空'], width=7, state='readonly', style="BlackText.TCombobox")
        cb_dir.set(s.get('direction', '做多')); cb_dir.grid(row=1, column=3, padx=4, pady=(6, 0))
        lbl_qty = tk.Label(top, text=f"數量({strategy_engine.qty_unit_of(s)})", bg="#1A2026", fg="white", font=('微軟正黑體', 9))
        lbl_qty.grid(row=1, column=4, sticky='w', padx=(10, 0), pady=(6, 0))
        e_qty = _ent(top, s.get('qty', 1), 6); e_qty.grid(row=1, column=5, padx=4, pady=(6, 0))
        _lbl(top, "停損%").grid(row=2, column=0, sticky='w', pady=(6, 0))
        e_sl = _ent(top, s.get('stop_loss_pct', 2.0), 6); e_sl.grid(row=2, column=1, padx=4, pady=(6, 0))
        _lbl(top, "停利% (0=停用)").grid(row=2, column=2, sticky='w', padx=(10, 0), pady=(6, 0))
        e_tp = _ent(top, s.get('take_profit_pct', 0.0), 6); e_tp.grid(row=2, column=3, padx=4, pady=(6, 0))
        _lbl(top, "讓價檔數").grid(row=2, column=4, sticky='w', padx=(10, 0), pady=(6, 0))
        e_slip = _ent(top, s.get('slippage_ticks', 2), 6); e_slip.grid(row=2, column=5, padx=4, pady=(6, 0))
        # 【ADR-070】交易時段閘門:期貨可選「只做日盤」或「日盤+夜盤」;
        # session_gate 打勾 = 非交易時間自動待命 (預設)。取消勾選才會 24 小時
        # 只要 K 棒邊界到就評估 (一般不建議,除非你很清楚在做什麼)。
        _lbl(top, "期貨時段").grid(row=4, column=0, sticky='w', pady=(6, 0))
        cb_fut_sess = ttk.Combobox(top, values=['日盤+夜盤', '只做日盤'], width=9, state='readonly', style="BlackText.TCombobox")
        cb_fut_sess.set('只做日盤' if s.get('futures_session') == 'day' else '日盤+夜盤')
        cb_fut_sess.grid(row=4, column=1, padx=4, pady=(6, 0), sticky='w')
        var_sess_gate = tk.BooleanVar(value=bool(s.get('session_gate', True)))
        tk.Checkbutton(top, text="非交易時間自動待命 (建議)", variable=var_sess_gate,
                       bg="#1A2026", fg="#8A99AD", selectcolor="#2A323D", activebackground="#1A2026",
                       font=('微軟正黑體', 8)).grid(row=4, column=2, columnspan=4, sticky='w', padx=(10, 0), pady=(6, 0))
        # 【ADR-101】籌碼未來函數開關 (進階,預設關閉)。
        # 籌碼是當日盤後才公布的:讓 T 日讀到 T 日籌碼 = 實盤不可能取得的資訊,
        # 回測績效會虛高。預設 T 日只讀得到 T-1 日(含)以前的籌碼;勾選這格
        # 僅供研究理論上限,回測報告會標示「含未來函數」。
        var_chip_sameday = tk.BooleanVar(value=bool(s.get('chips_allow_same_day', False)))
        tk.Checkbutton(top, text="⚠ 籌碼允許讀當日 (含未來函數,僅供研究)",
                       variable=var_chip_sameday,
                       bg="#1A2026", fg="#FF8A65", selectcolor="#2A323D", activebackground="#1A2026",
                       font=('微軟正黑體', 8)).grid(row=5, column=2, columnspan=4, sticky='w',
                                                    padx=(10, 0), pady=(2, 0))
        # 【ADR-043 第6項】絕對停損/停利 (股票=元,期貨=點;0=停用,與% 任一先到就出場)
        _lbl(top, "停損(元/點)").grid(row=6, column=0, sticky='w', pady=(6, 0))
        e_sl_abs = _ent(top, s.get('stop_loss_abs', 0.0), 6); e_sl_abs.grid(row=6, column=1, padx=4, pady=(6, 0))
        _lbl(top, "停利(元/點)").grid(row=6, column=2, sticky='w', padx=(10, 0), pady=(6, 0))
        e_tp_abs = _ent(top, s.get('take_profit_abs', 0.0), 6); e_tp_abs.grid(row=6, column=3, padx=4, pady=(6, 0))
        tk.Label(top, text="(0=停用)", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=6, column=4, sticky='w', padx=(10, 0), pady=(6, 0))
        # 【ADR-056】停損/停利/出場訊號三者至少要有一種,否則存檔會被 validate_strategy
        # 擋下。使用者常見的踩坑是三者都留 0 又沒加出場訊號 (畫面截圖實例)。
        # 存檔前用眼睛就看得到規則,比等到按下去才彈錯誤訊息更早攔截。
        _lbl(top, '進場時段(起~迄)').grid(row=7, column=0, sticky='w', pady=(6, 0))
        e_en_st = _ent(top, s.get('entry_time_start', ''), 8); e_en_st.grid(row=7, column=1, padx=4, pady=(6, 0))
        e_en_ed = _ent(top, s.get('entry_time_end', ''), 8); e_en_ed.grid(row=7, column=2, padx=4, pady=(6, 0), sticky='w')
        _lbl(top, '出場時段(起~迄)').grid(row=7, column=3, sticky='w', pady=(6, 0), padx=(10, 0))
        e_ex_st = _ent(top, s.get('exit_time_start', ''), 8); e_ex_st.grid(row=7, column=4, padx=4, pady=(6, 0))
        e_ex_ed = _ent(top, s.get('exit_time_end', ''), 8); e_ex_ed.grid(row=7, column=5, padx=4, pady=(6, 0), sticky='w')
        _lbl(top, '特定進場時間').grid(row=8, column=0, sticky='w', pady=(6, 0))
        e_sp_en = _ent(top, s.get('specific_entry_time', ''), 8); e_sp_en.grid(row=8, column=1, padx=4, pady=(6, 0))
        tk.Label(top, text="(格式: HH:MM 或 HH:MM:SS)", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=8, column=2, columnspan=2, sticky='w', pady=(6, 0))
        tk.Label(top, text="⚠ 停損%/停利%/停損(元)/停利(元)/出場訊號 至少要有一種不為 0,否則無法儲存 (持倉會永遠不出場)",
                 bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 8)).grid(row=9, column=0, columnspan=6, sticky='w', pady=(2, 0))
        # 【ADR-136】停損停利即時觸發:改成可勾選,不分商品別。
        # 舊策略沒有這個欄位時,intrabar_stop_enabled() 會沿用它原本的行為
        # (期貨=即時、股票=收盤才判定),所以勾選框打開時顯示的就是現況。
        #
        # 【ADR-138】放 row 14 而不是緊接在停損欄位後面:row 10~13 已被
        # Buy&Hold / 委託方式等佔用。ADR-136 原本放 row 10,**被後放的
        # Buy&Hold 整個蓋掉**,使用者實機截圖顯示內建策略根本看不到這個勾選框
        # (tkinter 的 grid 撞格不報錯,只是後放的蓋前放的 —— 同 P-104)。
        var_intrabar = tk.BooleanVar(value=strategy_engine.intrabar_stop_enabled(s))
        tk.Checkbutton(top, text="停損停利即時觸發 (盤中觸價就出場,不等K棒收盤;回測同步照此模擬)",
                       variable=var_intrabar, bg="#1A2026", fg="#FFCA28", selectcolor="#2A323D",
                       font=('微軟正黑體', 9)).grid(row=14, column=0, columnspan=6,
                                                    sticky='w', pady=(2, 0))
        # 【ADR-059】買進後持有不賣 (Buy & Hold):使用者要拿它當比較基準。
        # 勾了就放行「沒有出場方式」的驗證,但只限回測/模擬 (見 validate_strategy)。
        var_bnh = tk.BooleanVar(value=bool(s.get('buy_and_hold', False)))
        def _on_bnh_change(*args):
            if var_bnh.get():
                e_sl.delete(0, tk.END); e_sl.insert(0, "0.0")
                e_tp.delete(0, tk.END); e_tp.insert(0, "0.0")
                e_sl_abs.delete(0, tk.END); e_sl_abs.insert(0, "0.0")
                e_tp_abs.delete(0, tk.END); e_tp_abs.insert(0, "0.0")
        var_bnh.trace_add('write', _on_bnh_change)
        
        _bnh_row = tk.Frame(top, bg="#1A2026")
        _bnh_row.grid(row=10, column=0, columnspan=7, sticky='w', pady=(2, 0))
        tk.Checkbutton(_bnh_row, text="📌 買進後持有不賣 (Buy & Hold,當作比較基準)",
                       variable=var_bnh, bg="#1A2026", fg="#00E676", selectcolor="#2A323D",
                       font=('微軟正黑體', 9, 'bold'), activebackground="#1A2026").pack(side=tk.LEFT)
        tk.Label(_bnh_row, text="永不賣出;不可 (也不需要) 設停損停利/出場訊號。僅限回測/模擬。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=(8, 0))
        # 【ADR-062】三種模式並存,使用者要能互相比較
        _mode_row = tk.Frame(top, bg="#1A2026")
        _mode_row.grid(row=11, column=0, columnspan=7, sticky='w', pady=(2, 0))
        tk.Label(_mode_row, text="　模式", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        var_bnh_mode = tk.StringVar(value=str(s.get('bnh_mode', 'accumulate')))
        _mode_keys = list(strategy_engine.BNH_MODES)
        cb_bnh_mode = ttk.Combobox(_mode_row, width=34, state='readonly', style="BlackText.TCombobox",
                                   values=[strategy_engine.BNH_MODE_LABELS[k] for k in _mode_keys])
        cb_bnh_mode.current(_mode_keys.index(var_bnh_mode.get())
                            if var_bnh_mode.get() in _mode_keys else 1)
        cb_bnh_mode.pack(side=tk.LEFT, padx=6)
        tk.Label(_mode_row, text="每期金額", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(10, 2))
        e_dca_amt = tk.Entry(_mode_row, width=10, bg="#2A323D", fg="white")
        e_dca_amt.insert(0, str(s.get('dca_amount', 10000.0)))
        e_dca_amt.pack(side=tk.LEFT)
        _iv_keys = list(strategy_engine.DCA_INTERVALS)
        cb_dca_iv = ttk.Combobox(_mode_row, width=8, state='readonly', style="BlackText.TCombobox",
                                 values=[strategy_engine.DCA_INTERVAL_LABELS[k] for k in _iv_keys])
        cb_dca_iv.current(_iv_keys.index(str(s.get('dca_interval', 'month')))
                          if str(s.get('dca_interval', 'month')) in _iv_keys else 2)
        cb_dca_iv.pack(side=tk.LEFT, padx=4)
        _lbl_dca_hint = tk.Label(_mode_row, text="(定期定額才需要;數量隨價格變動,買不滿一單位的餘額累積到下期)",
                                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8))
        _lbl_dca_hint.pack(side=tk.LEFT, padx=(6, 0))

        def _on_bnh_mode(event=None):
            is_dca = _mode_keys[cb_bnh_mode.current()] == 'dca' if cb_bnh_mode.current() >= 0 else False
            st = tk.NORMAL if is_dca else tk.DISABLED
            try:
                e_dca_amt.config(state=st)
                cb_dca_iv.config(state='readonly' if is_dca else tk.DISABLED)
                _lbl_dca_hint.config(fg="#FFCA28" if is_dca else "#5A6472")
            except Exception:
                pass
        cb_bnh_mode.bind("<<ComboboxSelected>>", _on_bnh_mode)
        _on_bnh_mode()
        _lookup_name()
        _lbl(top, "每日進場上限").grid(row=3, column=0, sticky='w', pady=(6, 0))
        e_maxd = _ent(top, s.get('max_trades_per_day', 3), 6); e_maxd.grid(row=3, column=1, padx=4, pady=(6, 0))
        _lbl(top, "冷卻秒數").grid(row=3, column=2, sticky='w', padx=(10, 0), pady=(6, 0))
        e_cool = _ent(top, s.get('cooldown_sec', 300), 6); e_cool.grid(row=3, column=3, padx=4, pady=(6, 0))
        _lbl(top, "模式").grid(row=3, column=4, sticky='w', padx=(10, 0), pady=(6, 0))
        cb_mode = ttk.Combobox(top, values=['模擬', '實單'], width=7, state='readonly', style="BlackText.TCombobox")
        cb_mode.set(s.get('mode', '模擬')); cb_mode.grid(row=3, column=5, padx=4, pady=(6, 0))

        # 【ADR-135】委託方式改用共用函式 (規則的依據是交易所規定,只能有一份)
        _pt = self._qt_build_price_type_row(top, s, cb_tt)
        _pt['frame'].grid(row=12, column=0, columnspan=7, sticky='w', pady=(2, 0))

        # 【新ADR 多帳戶】模擬成交要記進哪個模擬帳戶——不同策略選不同帳戶,
        # 同一檔標的在不同策略下的部位才不會互相覆蓋 (見 core/paper_account.py
        # 頂部說明:positions 是用 symbol 當 key,同帳戶同symbol會互相覆蓋)。
        # 只在編輯器開的當下讀一次帳戶清單,存檔時只存 account_id (不是名稱),
        # 帳戶改名不影響已設定好的策略。
        _acct_row = tk.Frame(top, bg="#1A2026")
        _acct_row.grid(row=13, column=0, columnspan=7, sticky='w', pady=(2, 0))
        tk.Label(_acct_row, text="模擬帳戶", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        _acct_choices = self._qt_account_choices()
        cb_acct2 = ttk.Combobox(_acct_row, width=20, state='readonly', style="BlackText.TCombobox",
                                values=[name for _aid, name in _acct_choices])
        _cur_aid = strategy_engine.account_id_of(s)
        _ids2 = [aid for aid, _name in _acct_choices]
        cb_acct2.current(_ids2.index(_cur_aid) if _cur_aid in _ids2 else 0)
        cb_acct2.pack(side=tk.LEFT, padx=6)
        tk.Label(_acct_row, text="(只在「模式=模擬」時有意義;實單直接下到真實券商帳戶)",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=(6, 0))

        # --- 【ADR-074】看A做B ---
        watch_ui = self._qt_build_watch_panel(dlg, s)

        # --- 條件建構器 ---
        conds_frame = tk.Frame(dlg, bg="#1A2026"); conds_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 0))
        _lbl(conds_frame, "條件類型").grid(row=0, column=0, sticky='w')
        cond_keys = list(strategy_engine.CONDITIONS.keys())
        cond_names = [strategy_engine.CONDITIONS[k][0] for k in cond_keys]
        cb_cond = ttk.Combobox(conds_frame, values=cond_names, width=30, state='readonly', style="BlackText.TCombobox")
        cb_cond.grid(row=0, column=1, columnspan=2, padx=4, sticky='w')
        param_frame = tk.Frame(conds_frame, bg="#1A2026"); param_frame.grid(row=1, column=0, columnspan=6, sticky='w', pady=4)
        param_entries = {}

        def _rebuild_params(event=None):
            for w in param_frame.winfo_children():
                w.destroy()
            param_entries.clear()
            i = cb_cond.current()
            if i < 0:
                return
            _, spec, _fn = strategy_engine.CONDITIONS[cond_keys[i]]
            if not spec:
                _lbl(param_frame, "(此條件不需要參數)").grid(row=0, column=0, sticky='w')
                return
            # 【ADR-057】規格可能是 3 元素 (可自由輸入) 或 4 元素 (第4個是候選值,
            # 例如均線型態 SMA/EMA)。用 spec_parts() 正規化,不要在這裡自己解包
            # ——上一版就是寫死 `for k, lab, dv in spec` 才會在新條件上炸掉 (P-57)。
            for j, item in enumerate(spec):
                k, lab, dv, choices = strategy_engine.spec_parts(item)
                _lbl(param_frame, lab).grid(row=0, column=j*2, sticky='w', padx=(0, 2))
                if choices:
                    w = ttk.Combobox(param_frame, values=list(choices), width=7,
                                     state='readonly', style="BlackText.TCombobox")
                    w.set(str(dv))
                else:
                    w = _ent(param_frame, dv, 7)
                w.grid(row=0, column=j*2+1, padx=(0, 10))
                param_entries[k] = w
        cb_cond.bind("<<ComboboxSelected>>", _rebuild_params)
        if cond_names:
            cb_cond.current(0); _rebuild_params()

        entry_conds = list(s.get('entry', []))
        exit_conds = list(s.get('exit_signals', []))
        lists = tk.Frame(conds_frame, bg="#1A2026"); lists.grid(row=3, column=0, columnspan=6, sticky='we', pady=4)
        _lbl(lists, "進場條件 (全部成立才進場) — 單擊=帶回上方編輯器,雙擊=直接改參數").grid(row=0, column=0, sticky='w')
        lb_entry = tk.Listbox(lists, bg="#12161A", fg="#00E676", height=4, width=45, font=('微軟正黑體', 9))
        lb_entry.grid(row=1, column=0, padx=(0, 10), sticky='w')
        _lbl(lists, "出場訊號 (任一成立即出場) — 單擊/雙擊同左").grid(row=0, column=1, sticky='w')
        lb_exit = tk.Listbox(lists, bg="#12161A", fg="#FF8A65", height=4, width=45, font=('微軟正黑體', 9))
        lb_exit.grid(row=1, column=1, sticky='w')

        def _refresh_lists():
            lb_entry.delete(0, tk.END)
            for c in entry_conds:
                lb_entry.insert(tk.END, strategy_engine.condition_label(c))
            lb_exit.delete(0, tk.END)
            for c in exit_conds:
                lb_exit.insert(tk.END, strategy_engine.condition_label(c))
        _refresh_lists()

        def _collect_cond():
            i = cb_cond.current()
            if i < 0:
                return None
            key = cond_keys[i]
            p = {}
            for k, e in param_entries.items():
                try:
                    v = str(e.get()).strip()
                except Exception:
                    continue
                if v == '':
                    continue
                # 【ADR-057】數字就轉數字,轉不動就「保留字串」——舊版轉不動直接
                # pass 把整個參數丟掉,新增的均線型態 (SMA/EMA) 會被靜默吃掉,
                # 條件就永遠退回預設值,使用者完全不會知道自己選的沒生效。
                try:
                    p[k] = float(v) if '.' in v else int(v)
                except (TypeError, ValueError):
                    p[k] = v
            return {'type': key, 'params': p}

        def _add_entry():
            c = _collect_cond()
            if c: entry_conds.append(c); _refresh_lists()
        def _add_exit():
            c = _collect_cond()
            if c: exit_conds.append(c); _refresh_lists()
        def _del_entry():
            sel = lb_entry.curselection()
            if sel: entry_conds.pop(sel[0]); _refresh_lists()
        def _del_exit():
            sel = lb_exit.curselection()
            if sel: exit_conds.pop(sel[0]); _refresh_lists()

        # ============================================================
        # 【ADR-062】使用者需求 #1:點清單裡的條件就能改它
        # ============================================================
        # 舊版只能「移除再重加」,而且重加時要自己把參數重打一次 —— 條件一多
        # 就非常難改。現在:
        #   單擊 → 把上方的「條件類型 + 參數」同步成你點的那一條
        #          (接著按「加入進場/出場」就是複製一份,很順手)
        #   雙擊 → 直接跳出小視窗改參數,確定後「就地更新」那一條
        def _sync_builder_from(cond):
            """把建構器 (條件類型下拉 + 參數欄) 設成指定條件的內容。"""
            try:
                key = cond.get('type')
                if key not in cond_keys:
                    return
                cb_cond.current(cond_keys.index(key))
                _rebuild_params()
                for k, w in param_entries.items():
                    if k not in cond.get('params', {}):
                        continue
                    v = str(cond['params'][k])
                    try:
                        if isinstance(w, ttk.Combobox):
                            w.set(v)
                        else:
                            w.delete(0, tk.END); w.insert(0, v)
                    except Exception:
                        continue
            except Exception:
                pass

        def _on_pick(lb, conds):
            sel = lb.curselection()
            if sel and sel[0] < len(conds):
                _sync_builder_from(conds[sel[0]])

        def _edit_cond_dialog(conds, idx, title):
            """就地編輯一條既有條件的參數。"""
            if idx >= len(conds):
                return
            cond = conds[idx]
            meta = strategy_engine.CONDITIONS.get(cond.get('type'))
            if not meta:
                messagebox.showwarning("無法編輯", f"未知的條件類型:{cond.get('type')}", parent=dlg)
                return
            name, spec, _fn = meta
            ed = tk.Toplevel(dlg); ed.title(f"編輯{title}條件"); ed.configure(bg="#1A2026")
            self.center_window(ed, 460, 130 + 34 * max(1, len(spec)))
            try:
                ed.transient(dlg); ed.grab_set(); ed.lift(); ed.focus_force()
            except Exception:
                pass
            tk.Label(ed, text=name, bg="#1A2026", fg="#FFCA28",
                     font=('微軟正黑體', 10, 'bold')).pack(anchor='w', padx=12, pady=(12, 6))
            body = tk.Frame(ed, bg="#1A2026"); body.pack(fill=tk.X, padx=12)
            widgets = {}
            if not spec:
                tk.Label(body, text="(此條件不需要參數)", bg="#1A2026", fg="#8A99AD",
                         font=('微軟正黑體', 9)).grid(row=0, column=0, sticky='w')
            for j, item in enumerate(spec):
                k, lab, dv, choices = strategy_engine.spec_parts(item)
                tk.Label(body, text=lab, bg="#1A2026", fg="white",
                         font=('微軟正黑體', 9)).grid(row=j, column=0, sticky='w', pady=3)
                cur = cond.get('params', {}).get(k, dv)
                if choices:
                    w = ttk.Combobox(body, values=list(choices), width=10,
                                     state='readonly', style="BlackText.TCombobox")
                    w.set(str(cur))
                else:
                    w = tk.Entry(body, width=12, bg="#2A323D", fg="white")
                    w.insert(0, str(cur))
                w.grid(row=j, column=1, sticky='w', padx=8, pady=3)
                widgets[k] = w

            def _apply():
                p = {}
                for k, w in widgets.items():
                    v = str(w.get()).strip()
                    if v == '':
                        continue
                    try:
                        p[k] = float(v) if '.' in v else int(v)
                    except (TypeError, ValueError):
                        p[k] = v          # 保留字串型參數 (SMA/EMA)
                conds[idx] = {'type': cond['type'], 'params': p}
                _refresh_lists()
                ed.destroy()

            btns = tk.Frame(ed, bg="#1A2026"); btns.pack(pady=12)
            tk.Button(btns, text="確定修改", bg="#29B6F6", fg="black", relief="flat",
                      font=('微軟正黑體', 9, 'bold'), padx=16, command=_apply).pack(side=tk.LEFT, padx=4)
            tk.Button(btns, text="取消", bg="#2A323D", fg="white", relief="flat",
                      font=('微軟正黑體', 9), padx=16, command=ed.destroy).pack(side=tk.LEFT, padx=4)

        lb_entry.bind("<<ListboxSelect>>", lambda e: _on_pick(lb_entry, entry_conds))
        lb_exit.bind("<<ListboxSelect>>", lambda e: _on_pick(lb_exit, exit_conds))
        lb_entry.bind("<Double-Button-1>",
                      lambda e: (lb_entry.curselection() and
                                 _edit_cond_dialog(entry_conds, lb_entry.curselection()[0], "進場")))
        lb_exit.bind("<Double-Button-1>",
                     lambda e: (lb_exit.curselection() and
                                _edit_cond_dialog(exit_conds, lb_exit.curselection()[0], "出場")))
        addrow = tk.Frame(conds_frame, bg="#1A2026"); addrow.grid(row=2, column=0, columnspan=6, sticky='w', pady=2)
        tk.Button(addrow, text="➕ 加入進場", bg="#00C853", fg="black", relief="flat", padx=8, pady=1,
                  font=('微軟正黑體', 9, 'bold'), command=_add_entry).pack(side=tk.LEFT, padx=2)
        tk.Button(addrow, text="➕ 加入出場", bg="#FB8C00", fg="black", relief="flat", padx=8, pady=1,
                  font=('微軟正黑體', 9, 'bold'), command=_add_exit).pack(side=tk.LEFT, padx=2)
        tk.Button(addrow, text="移除選取進場", bg="#5A6472", fg="white", relief="flat", padx=8, pady=1,
                  font=('微軟正黑體', 9), command=_del_entry).pack(side=tk.LEFT, padx=8)
        tk.Button(addrow, text="移除選取出場", bg="#5A6472", fg="white", relief="flat", padx=8, pady=1,
                  font=('微軟正黑體', 9), command=_del_exit).pack(side=tk.LEFT, padx=2)

        def _save():
            s['name'] = e_name.get().strip()
            s['symbol'] = e_sym.get().strip().upper()
            s['trade_type'] = cb_tt.get()
            s['market'] = '台期貨' if cb_tt.get() == '期貨' else '台股'  # 相容既有 market 判斷
            s['timeframe'] = cb_tf.get()
            s['direction'] = cb_dir.get()
            try: s['qty'] = int(e_qty.get().strip())
            except (TypeError, ValueError): s['qty'] = 0
            try: s['stop_loss_pct'] = float(e_sl.get().strip())
            except (TypeError, ValueError): s['stop_loss_pct'] = 0.0
            try: s['take_profit_pct'] = float(e_tp.get().strip())
            except (TypeError, ValueError): s['take_profit_pct'] = 0.0
            try: s['stop_loss_abs'] = float(e_sl_abs.get().strip())
            except (TypeError, ValueError): s['stop_loss_abs'] = 0.0
            try: s['take_profit_abs'] = float(e_tp_abs.get().strip())
            except (TypeError, ValueError): s['take_profit_abs'] = 0.0
            try: s['slippage_ticks'] = int(e_slip.get().strip())
            except (TypeError, ValueError): s['slippage_ticks'] = 2
            s['price_type'] = _pt['get']()
            s['account_id'] = _ids2[cb_acct2.current()] if cb_acct2.current() >= 0 else 'default'
            # 【ADR-110階段2】實單要下到哪一家券商的哪一個帳號
            s['broker'], s['broker_account'] = _live_get()
            try: s['max_trades_per_day'] = int(e_maxd.get().strip())
            except (TypeError, ValueError): s['max_trades_per_day'] = 3
            s['entry_time_start'] = e_en_st.get().strip()
            s['entry_time_end'] = e_en_ed.get().strip()
            s['exit_time_start'] = e_ex_st.get().strip()
            s['exit_time_end'] = e_ex_ed.get().strip()
            s['specific_entry_time'] = e_sp_en.get().strip()
            # 【ADR-070】交易時段閘門設定
            s['futures_session'] = 'day' if cb_fut_sess.get() == '只做日盤' else 'day_night'
            s['session_gate'] = bool(var_sess_gate.get())
            s['intrabar_stop'] = bool(var_intrabar.get())
            # 【ADR-101】籌碼未來函數開關 (進階;預設 False = 只讀前一日籌碼)
            s['chips_allow_same_day'] = bool(var_chip_sameday.get())
            # 【ADR-074】看A做B 設定
            s.update(watch_ui['get']())
            try: s['cooldown_sec'] = float(e_cool.get().strip())
            except (TypeError, ValueError): s['cooldown_sec'] = 300
            s['mode'] = cb_mode.get()
            s['entry'] = entry_conds
            s['exit_signals'] = exit_conds
            s['buy_and_hold'] = bool(var_bnh.get())   # 【ADR-059】
            # 【ADR-062】買進持有模式與定期定額參數
            try:
                s['bnh_mode'] = _mode_keys[cb_bnh_mode.current()]
            except Exception:
                s['bnh_mode'] = 'accumulate'
            try:
                s['dca_amount'] = float(e_dca_amt.get().strip() or 0)
            except (TypeError, ValueError):
                s['dca_amount'] = 0.0
            try:
                s['dca_interval'] = _iv_keys[cb_dca_iv.current()]
            except Exception:
                s['dca_interval'] = 'month'
            ok, msg = strategy_engine.validate_strategy(s)
            if not ok:
                # 【ADR-056】舊版只寫 log_message:使用者這時人在「量化交易」分頁,
                # 系統日誌那個分頁被切換掉看不到 (bottom 分頁互斥,quant 顯示時
                # log 分頁是 pack_forget 狀態),於是「按了儲存沒反應」——其實是
                # 存了,但驗證沒過又完全看不到原因。改成強制彈窗,原因一定看得到。
                self.log_message(f"【自動交易】策略儲存失敗: {msg}")
                messagebox.showerror("策略儲存失敗",
                                     f"「{s.get('name') or '(未命名)'}」尚未儲存:\n\n{msg}\n\n"
                                     "請修正後再按一次「儲存策略」。", parent=dlg)
                return
            if s.get('mode') == '實單' and is_new:
                # 新策略直接選實單:強制先存成模擬,要求先觀察訊號 (安全預設)
                s['mode'] = '模擬'
                self.log_message("【自動交易-安全】新策略一律先以「模擬」模式儲存;請先觀察模擬訊號合理後,再編輯改為實單。")
            if is_new:
                s['enabled'] = False
                self.strategies.append(s)
                self.strategy_runtimes[s['id']] = strategy_engine.new_runtime()
            else:
                for i, x in enumerate(self.strategies):
                    if x['id'] == s['id']:
                        self.strategies[i] = s
                        break
            self._qt_save(); self._qt_save_state(); self._qt_refresh_tree()
            self.log_message(f"【自動交易】策略「{s['name']}」已儲存 ({s['mode']}/{'啟用' if s.get('enabled') else '停用'})。")
            messagebox.showinfo("儲存成功",
                                f"策略「{s['name']}」已儲存 ({s['mode']} 模式)。\n"
                                "已加入「量化交易」分頁清單,可選取後按「🔬 回測」。", parent=dlg)
            dlg.destroy()

        foot = tk.Frame(dlg, bg="#1A2026"); foot.pack(pady=10)
        if readonly:
            tk.Button(foot, text="唯讀無法儲存", bg="#2A323D", fg="#8A99AD", relief="flat", state=tk.DISABLED,
                      font=('微軟正黑體', 11, 'bold'), padx=18, pady=4).pack(side=tk.LEFT, padx=6)
        else:
            tk.Button(foot, text="儲存策略", bg="#29B6F6", fg="black", relief="flat",
                      font=('微軟正黑體', 11, 'bold'), padx=18, pady=4, command=_save).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="關閉" if readonly else "取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 11), padx=18, pady=4, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    # ================= 【第十六輪 第6項】主圖K棒自動更新 =================
    AUTO_REFRESH_TFS = {"1分K": 1, "5分K": 5, "15分K": 15, "30分K": 30, "60分K": 60}

    def _chart_auto_refresh_once(self):
        """
        一次自動刷新:抓目前商品最近幾天的分K → 與既有資料合併 → 保留視野重繪。
        視野規則:使用者停在最右側 (看最新K棒) 就跟隨新K棒平移;翻到歷史區
        就完全不動 (新K棒接在右邊,不影響既有K棒的位置索引)。
        設計取捨 (誠實):K棒本體在「每根收盤」時自動長出來;盤中當根的跳動
        請看即時串流報價/五檔——mplfinance 逐 tick 全圖重繪會造成明顯停頓,
        這正是使用者不要的,故不做。
        """
        # 【第十七輪修正】手動查詢進行中一律讓路:此時 current_contract 可能已
        # 指向新商品、current_df 卻還是舊商品 (發布在後),貿然合併會把兩個
        # 商品的K線黏在一起 (使用者實例:切到微型臺指瞬間K線圖異常)。
        if self._fetch_in_progress or self._login_in_progress:
            return
        contract = self.current_contract
        tf = getattr(self, 'current_timeframe', None) or self.timeframe_var.get()
        sym = self.current_symbol
        seq_at_start = self._fetch_seq
        df_ref = self.current_df  # 身分守衛:發布過新 df 就作廢本次結果
        if contract is None or not sym or tf not in self.AUTO_REFRESH_TFS:
            return
        if df_ref is None or len(df_ref) < 2:
            return
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=4)
        raw = self._download_kbars_raw(contract, start_dt, end_dt)
        if raw is None or raw.empty:
            return
        fresh = self._resample_sj_df(raw, tf)
        if fresh is None or fresh.empty:
            return
        def _apply():
            try:
                # 期間使用者若手動查了別的商品/週期,這次結果作廢 (序號守衛,ADR-024 同款);
                # 【第十七輪修正】外加 df 身分守衛:只要 current_df 物件換過 (任何新發布),
                # 本次合併一律作廢——確保絕不把 A 商品的資料黏進 B 商品的圖。
                if (self._fetch_seq != seq_at_start or self.current_symbol != sym
                        or self.current_df is not df_ref or self._fetch_in_progress):
                    return
                cur = self.current_df
                if cur is None or len(cur) < 2:
                    return
                prev_len = len(cur)
                prev_last_ts = cur.index[-1]
                prev_last_close = float(cur['Close'].iloc[-1])
                merged = pd.concat([cur[cur.index < fresh.index[0]], fresh])
                # 沒有任何變化 (連最後一根的收盤都沒動) 就不重繪,避免無謂閃動
                if (len(merged) == prev_len and merged.index[-1] == prev_last_ts
                        and abs(float(merged['Close'].iloc[-1]) - prev_last_close) < 1e-12):
                    return
                added = len(merged) - prev_len
                try:
                    x0, x1 = self.axlist[0].get_xlim()
                    if added > 0 and x1 >= prev_len - 1.5:
                        # 使用者在最右側看最新盤勢 → 視窗跟著新K棒平移
                        self.saved_xlim = (x0 + added, x1 + added)
                    else:
                        self.saved_xlim = (x0, x1)  # 翻到歷史區 → 畫面完全不動
                except Exception:
                    pass
                self.current_df = merged
                self.draw_chart(merged)
            except Exception:
                pass
        self.safe_after(0, _apply)

    def chart_auto_refresh_worker(self):
        """每 2 秒檢查一次:分K週期跨過「K棒收盤邊界」就自動刷新,免手動重新載入。"""
        import time as _time
        last_boundary = None
        while True:
            try:
                if getattr(self, '_closing', False):
                    return
                tf = getattr(self, 'current_timeframe', None)
                mins = self.AUTO_REFRESH_TFS.get(tf or '')
                if (mins and self.api_logged_in and HAS_SJ and self.current_contract is not None
                        and not self._login_in_progress):
                    now = datetime.now()
                    boundary = now.replace(second=0, microsecond=0)
                    boundary -= timedelta(minutes=boundary.minute % mins)
                    # 跨過新邊界且給資料源 3 秒緩衝,再抓 (太早抓最後一根還沒生出來)
                    if boundary != last_boundary and (now - boundary).total_seconds() >= 3:
                        last_boundary = boundary
                        self._chart_auto_refresh_once()
            except Exception:
                pass
            _time.sleep(2)

    # ---------- 【ADR-039】策略回測 ----------
    # 【ADR-056/057】日K預設天數 1500→3650→7300 (20年,使用者需求 #8):
    # 過去卡在 1500 是遷就 shioaji 分K原始資料的下載量,但日K本身現在有期交所
    # 歷史延伸 (期貨 R1) 撐更早的範圍,預設值理應反映「使用者匯入期交所歷史後
    # 真的能回測多久」,而不是 shioaji 的原始限制。
    # ⚠ 誠實提醒:20 年只是「預設帶入的起始日」,實際能回測多長仍取決於資料源
    # —— 期貨 R1 要先匯入期交所歷史才真的抓得到 2006 年;個股沒有這個延伸來源,
    # 範圍仍受 shioaji 深度限制 (下載完會如實回報實際抓到幾根,不會假裝有)。
    QT_BACKTEST_DAYS = {"1分K": 30, "5分K": 90, "15分K": 180, "30分K": 300,
                        "60分K": 400, "日K": 7300, "周K": 7300, "月K": 7300}

    def _run_custom_in_subprocess(self, strategy, df, position, timeout=8, runtime=None):
        """
        【ADR-040】在獨立子行程執行自訂策略的 on_bar,逾時/崩潰不影響主程式。
        回傳決策字串 (BUY/SELL/CLOSE/HOLD);任何失敗一律回 HOLD (安全:不動作)。
        """
        try:
            import subprocess, json as _json
            tail = df.tail(400)  # 只送最近 400 根,足夠算指標又不讓 IPC 過大
            records = [{'ts': str(ts), 'Open': float(r['Open']), 'High': float(r['High']),
                        'Low': float(r['Low']), 'Close': float(r['Close']),
                        'Volume': float(r.get('Volume', 0))}
                       for ts, r in tail.iterrows()]
            rt = runtime or {}
            payload = _json.dumps({'source_code': strategy.get('source_code', ''),
                                   'records': records, 'position': position,
                                   'params': strategy.get('custom_params', {}),
                                   'state': rt.get('custom_state') or {},
                                   'entry_price': rt.get('entry_price', 0.0),
                                   'bars_in_position': rt.get('bars_in_pos', 0)})
            proc = subprocess.run(
                [sys.executable, '-m', 'core.custom_runner'],
                input=payload, capture_output=True, text=True, timeout=timeout,
                cwd=os.path.dirname(os.path.abspath(__file__)))
            if proc.returncode != 0:
                raise RuntimeError(f"子行程結束碼 {proc.returncode}: {proc.stderr[:200]}")
            out = _json.loads(proc.stdout.strip() or '{}')
            if not out.get('ok'):
                raise RuntimeError(out.get('error', '未知錯誤'))
            if runtime is not None:
                runtime['custom_state'] = out.get('state') or {}
                runtime['bars_in_pos'] = int(rt.get('bars_in_pos', 0)) + (1 if position != 'FLAT' else 0)
            for lg in (out.get('logs') or [])[:5]:
                self.safe_after(0, self.log_message, f"【自訂策略-log】{lg}")
            return out.get('decision', 'HOLD')
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"自訂策略執行逾時 ({timeout}s),已中止 (可能有無窮迴圈或過重計算)")
        except Exception as e:
            raise RuntimeError(f"自訂策略子行程失敗: {type(e).__name__}: {e}")

    def _qt_optimize_selected(self):
        """【ADR-054】參數最佳化入口:對選取策略做網格搜尋,找出績效最佳參數。"""
        s = self._qt_selected()
        if not s:
            self.log_message("【最佳化】請先在清單中選取策略。")
            messagebox.showinfo("請先選取策略", "請先在「量化交易」清單中點選一個策略,再按「🎯 參數最佳化」。",
                                parent=self._qt_dialog_parent())
            return
        if s.get('kind') != 'custom':
            messagebox.showinfo("僅支援自訂策略",
                                "參數最佳化目前只支援「自訂 Python 策略」——\n"
                                "因為要掃描的參數是程式碼裡用 ctx.param('名稱', 預設值) 讀取的那些。",
                                parent=self._qt_dialog_parent())
            return
        if not (self.api_logged_in and HAS_SJ and self.sj_api):
            self.log_message("【最佳化】需要先登入券商 API 以取得歷史K線資料。")
            return
        if getattr(self, '_backtest_running', False):
            self._qt_offer_abort_backtest("最佳化")
            return
        self._qt_backtest_running_since = time.time()
        self._backtest_cancel = False
        self._qt_optimize_dialog(s)

    def _qt_optimize_dialog(self, s):
        from datetime import datetime as _dt
        dlg = tk.Toplevel(self); dlg.title(f"🎯 參數最佳化 — {s.get('name')}")
        dlg.configure(bg="#1A2026"); self.center_window(dlg, 1020, 700); dlg.transient(self)
        try: dlg.lift(); dlg.focus_force()
        except Exception: pass
        tk.Label(dlg, text=("在歷史資料上掃描參數組合,依你選的目標指標排名。\n"
                            "⚠ 這是「在過去找最好」,天生有過度最佳化 (overfitting) 風險:組合越多、"
                            "資料越短,選到的參數越可能只是運氣好。務必看「樣本外」那欄再決定。"),
                 bg="#2A1215", fg="#FF8A80", font=('微軟正黑體', 9), wraplength=860,
                 justify='left').pack(fill=tk.X, padx=10, pady=(10, 4))
        form = tk.Frame(dlg, bg="#1A2026"); form.pack(fill=tk.X, padx=12, pady=2)

        def _lbl(t, r, c, **kw):
            tk.Label(form, text=t, bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=r, column=c, sticky='w', **kw)

        _lbl("參數範圍", 0, 0)
        cur = ", ".join(f"{k}={v}" for k, v in (s.get('custom_params') or {}).items()) or "fast=5,7,10; slow=20,25,30"
        e_grid = tk.Entry(form, width=60, bg="#2A323D", fg="white")
        e_grid.insert(0, cur if '=' in cur and (',' in cur or ':' in cur) else "fast=5,7,10; slow=20,25,30")
        e_grid.grid(row=0, column=1, columnspan=4, padx=4, sticky='w')
        lbl_hint = tk.Label(form, text="格式:fast=5,7,10; slow=20:35:5  (逗號列舉 或 起:迄:間隔)",
                            bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8))
        lbl_hint.grid(row=1, column=1, columnspan=4, sticky='w', padx=4)
        # 【ADR-056】搜尋模式:網格 (使用者列好候選值) vs 隨機 (使用者只給範圍,
        # 系統自己抽樣嘗試)。使用者反映「網格要我自己先列好參數」,寬範圍
        # (如 fast=3:50) 網格會直接因組合數超過 500 上限被拒絕,隨機搜索
        # 就是為了這種「只想給範圍、不想自己窄化」的情境而加。
        _lbl("搜尋模式", 0, 5, padx=(14, 0))
        cb_mode_opt = ttk.Combobox(form, values=['網格 (列舉候選值)', '隨機 (只給範圍,自動嘗試)'],
                                   width=22, state='readonly', style="BlackText.TCombobox")
        cb_mode_opt.set('網格 (列舉候選值)'); cb_mode_opt.grid(row=0, column=6, padx=4, sticky='w')
        lbl_trials = tk.Label(form, text="嘗試次數", bg="#1A2026", fg="white", font=('微軟正黑體', 9))
        e_trials = tk.Entry(form, width=6, bg="#2A323D", fg="white"); e_trials.insert(0, "60")

        def _on_mode_change(event=None):
            random_mode = cb_mode_opt.get().startswith('隨機')
            if random_mode:
                lbl_hint.config(text="格式 (只要下限:上限):fast=3:50; slow=10:200 — 系統會在範圍內隨機抽樣")
                lbl_trials.grid(row=1, column=6, sticky='w', padx=4, pady=(2, 0))
                e_trials.grid(row=1, column=7, sticky='w')
            else:
                lbl_hint.config(text="格式:fast=5,7,10; slow=20:35:5  (逗號列舉 或 起:迄:間隔)")
                lbl_trials.grid_forget(); e_trials.grid_forget()
        cb_mode_opt.bind("<<ComboboxSelected>>", _on_mode_change)
        _lbl("目標指標", 2, 0, pady=(6, 0))
        cb_obj = ttk.Combobox(form, values=list(optimizer.OBJECTIVES.keys()), width=18,
                              state='readonly', style="BlackText.TCombobox")
        cb_obj.set('淨損益'); cb_obj.grid(row=2, column=1, padx=4, pady=(6, 0), sticky='w')
        _lbl("最少交易筆數", 2, 2, pady=(6, 0))
        e_min = tk.Entry(form, width=6, bg="#2A323D", fg="white"); e_min.insert(0, "5")
        e_min.grid(row=2, column=3, padx=4, pady=(6, 0), sticky='w')
        _lbl("起始日", 3, 0, pady=(6, 0))
        e_start = tk.Entry(form, width=12, bg="#2A323D", fg="white")
        e_start.insert(0, (datetime.now() - timedelta(days=self.QT_BACKTEST_DAYS.get(s.get('timeframe'), 365))).strftime('%Y-%m-%d'))
        e_start.grid(row=3, column=1, padx=4, pady=(6, 0), sticky='w')
        _lbl("結束日", 3, 2, pady=(6, 0))
        e_end = tk.Entry(form, width=12, bg="#2A323D", fg="white")
        e_end.insert(0, datetime.now().strftime('%Y-%m-%d')); e_end.grid(row=3, column=3, padx=4, pady=(6, 0), sticky='w')

        # 【ADR-058】期間快選鈕 (使用者需求 #4),與回測對話框同一組預設值
        _lbl("快選期間", 4, 0, pady=(6, 0))
        _qf = tk.Frame(form, bg="#1A2026"); _qf.grid(row=4, column=1, columnspan=7, sticky='w', pady=(6, 0))

        def _quick_opt(days):
            try:
                end = _dt.now(); start = end - timedelta(days=days)
                e_start.delete(0, tk.END); e_start.insert(0, start.strftime('%Y-%m-%d'))
                e_end.delete(0, tk.END); e_end.insert(0, end.strftime('%Y-%m-%d'))
            except Exception:
                pass
        for _i, (_lb, _d) in enumerate([("3M", 90), ("6M", 182), ("1Y", 365), ("2Y", 730),
                                        ("3Y", 1095), ("5Y", 1825), ("7Y", 2555),
                                        ("10Y", 3650), ("15Y", 5475), ("20Y", 7300)]):
            tk.Button(_qf, text=_lb, bg="#2A323D", fg="#29B6F6", relief="flat",
                      font=('微軟正黑體', 8, 'bold'), width=4, padx=2, pady=1,
                      command=(lambda dd=_d: _quick_opt(dd))).grid(row=0, column=_i, padx=1)

        # 【ADR-058】盤別口徑 (使用者需求 #3);非期貨日/周/月K不顯示,值固定 'all'
        var_session_opt = tk.StringVar(value='all')
        if str(s.get('market')) == '台期貨' and s.get('timeframe') in ('日K', '周K', '月K'):
            _lbl("盤別口徑", 5, 0, pady=(6, 0))
            _sf = tk.Frame(form, bg="#1A2026"); _sf.grid(row=5, column=1, columnspan=7, sticky='w', pady=(6, 0))
            tk.Radiobutton(_sf, text="近全(含夜盤)", variable=var_session_opt, value='all',
                           bg="#1A2026", fg="#E6EDF3", selectcolor="#2A323D",
                           font=('微軟正黑體', 9), activebackground="#1A2026").pack(side=tk.LEFT)
            tk.Radiobutton(_sf, text="只用日盤(08:45–13:45,長期回測口徑一致)",
                           variable=var_session_opt, value='day',
                           bg="#1A2026", fg="#E6EDF3", selectcolor="#2A323D",
                           font=('微軟正黑體', 9), activebackground="#1A2026").pack(side=tk.LEFT, padx=(10, 0))

        cols = ('rank', 'params', 'pnl', 'trades', 'win', 'pf', 'mdd', 'oos')
        heads = {'rank': '#▼', 'params': '參數組合', 'pnl': '淨損益', 'trades': '交易數',
                 'win': '勝率', 'pf': '獲利因子', 'mdd': '最大回撤', 'oos': '樣本外淨損益'}
        widths = {'rank': 40, 'params': 220, 'pnl': 120, 'trades': 70, 'win': 70,
                  'pf': 80, 'mdd': 120, 'oos': 130}
        # 【ADR-057】使用者需求 #10:結果表字太淡看不清楚。這張表沿用主畫面的
        # 深色 Treeview 樣式時,底色被系統畫成淺色、前景卻還是淺灰,對比極低。
        # 給它一個專屬的「白底黑字」樣式,並確保 tag 的顏色也都是深色系。
        _ost = ttk.Style()
        try:
            _ost.configure("Optim.Treeview", background="#FFFFFF", fieldbackground="#FFFFFF",
                           foreground="#000000", rowheight=22)
            _ost.configure("Optim.Treeview.Heading", background="#D7DEE6", foreground="#000000",
                           font=('微軟正黑體', 9, 'bold'))
            _ost.map("Optim.Treeview", background=[('selected', '#B3D4FC')],
                     foreground=[('selected', '#000000')])
        except Exception:
            pass
        tk.Label(dlg, text="結果依「你選的目標指標」由好到壞排序,第 1 名在最上面;灰底列 = 未達最少交易筆數門檻,不列入排名。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(anchor='w', padx=12, pady=(6, 0))
        tree = ttk.Treeview(dlg, columns=cols, show='headings', height=12, style="Optim.Treeview")
        for c in cols:
            tree.heading(c, text=heads[c]); tree.column(c, width=widths[c], anchor='center')
        lbl_st = tk.Label(dlg, text="", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9),
                          wraplength=860, justify='left', anchor='w')
        btns = tk.Frame(dlg, bg="#1A2026")
        btns.pack(side=tk.BOTTOM, pady=8)
        lbl_st.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 2))
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        state = {'stop': False, 'best': None, 'running': False}

        def _set(msg, color="#8A99AD"):
            try:
                if dlg.winfo_exists(): lbl_st.config(text=msg, fg=color)
            except Exception: pass

        def _fill(res):
            try:
                if not dlg.winfo_exists(): return
            except Exception:
                return
            for i in tree.get_children(): tree.delete(i)
            # 【ADR-057】res['results'] 在 core/optimizer.py 已依 (eligible, score)
            # 由大到小排序,所以「第 1 名 = 依你選的目標指標最好的那一組」,
            # 由上而下遞減。這裡不再重排,只忠實呈現。
            for n, r in enumerate(res['results'][:200], 1):
                m = r['metrics']
                pf = m.get('profit_factor', 0)
                pf_s = "∞" if pf == float('inf') else f"{pf:.2f}"
                oos = r.get('oos_pnl')
                tree.insert('', tk.END, values=(
                    n, ", ".join(f"{k}={v}" for k, v in r['params'].items()),
                    f"{_fmt_amt_signed(m.get('total_pnl', 0))}", m.get('trades', 0),
                    f"{m.get('win_rate', 0):.1f}%", pf_s,
                    f"{_fmt_amt(m.get('max_drawdown', 0))}",
                    "—" if oos is None else f"{_fmt_amt_signed(oos)}"),
                    tags=('ok' if r['eligible'] else 'bad',))
            # 黑字為主;未達門檻的那幾列用深灰 + 淺灰底,仍然看得清楚,
            # 但一眼區分得出「這組沒有列入排名」。
            tree.tag_configure('bad', foreground='#555555', background='#EFEFEF')
            tree.tag_configure('ok', foreground='#000000', background='#FFFFFF')
            state['best'] = res.get('best')
            b = res.get('best')
            if b:
                oos = b.get('oos_pnl')
                warn = ""
                if oos is not None and b['metrics'].get('total_pnl', 0) > 0 and oos <= 0:
                    warn = "  ⚠ 樣本外由盈轉虧 —— 高度疑似過度最佳化,不建議直接使用!"
                _set(f"✅ 完成:掃描 {res['evaluated']}/{res['total']} 組。最佳 "
                     f"{', '.join(f'{k}={v}' for k, v in b['params'].items())} → "
                     f"淨損益 {_fmt_amt_signed(b['metrics']['total_pnl'])}、{b['metrics']['trades']} 筆、"
                     f"勝率 {b['metrics']['win_rate']:.1f}%。{warn}",
                     "#FFCA28" if warn else "#00E676")
            else:
                _set(f"完成,但沒有組合達到最少交易筆數門檻 (掃描 {res['evaluated']} 組)。請放寬門檻或延長期間。", "#FFCA28")

        def _run():
            if state['running']:
                return
            random_mode = cb_mode_opt.get().startswith('隨機')
            try:
                if random_mode:
                    param_ranges = optimizer.parse_param_ranges(e_grid.get())
                    try:
                        n_trials = int(e_trials.get().strip() or 60)
                    except ValueError:
                        raise ValueError("嘗試次數需為整數")
                    if n_trials > 300:
                        _set("✗ 嘗試次數超過上限 300,請調低。", "#FF5252"); return
                    n = n_trials
                else:
                    grid = optimizer.parse_param_spec(e_grid.get())
                    n = optimizer.count_combos(grid)
                    if n > 500:
                        _set(f"✗ 參數組合共 {n} 組,超過上限 500 組,請縮小範圍或改用「隨機」模式。", "#FF5252"); return
                min_tr = int(e_min.get().strip() or 5)
                sd_ = _dt.strptime(e_start.get().strip(), '%Y-%m-%d')
                ed_ = _dt.strptime(e_end.get().strip(), '%Y-%m-%d')
                if sd_ >= ed_: raise ValueError
            except ValueError as e:
                _set(f"✗ 設定有誤: {e}" if str(e) else "✗ 日期格式須為 YYYY-MM-DD 且起始日早於結束日。", "#FF5252"); return
            except Exception as e:
                _set(f"✗ {e}", "#FF5252"); return
            state['stop'] = False; state['running'] = True
            self._backtest_running = True
            mode_txt = f"隨機搜索 {n} 次" if random_mode else f"{n} 組參數"
            _set(f"⏳ 下載資料並掃描 {mode_txt}中... (可按「⛔ 停止」中斷)", "#FFCA28")
            # 【ADR-056】should_stop 除了使用者按「⛔ 停止」,也要看 self._closing——
            # 使用者若在掃描中把整個程式關掉,背景執行緒必須盡快不再嘗試碰 Tk
            # (見 safe_after 的 P-59 說明),should_stop 提早讓 optimizer 的迴圈
            # 直接跳出,大幅縮短「還在呼叫 Tk 但視窗已經在銷毀」的競態窗口。
            search_spec = ('random', param_ranges, n) if random_mode else ('grid', grid, None)
            threading.Thread(target=self._qt_optimize_worker,
                             args=(copy.deepcopy(s), search_spec, cb_obj.get(), min_tr, sd_, ed_,
                                   lambda: state['stop'] or self._closing,
                                   lambda msg, col: self.safe_after(0, _set, msg, col),
                                   lambda res: self.safe_after(0, _fill, res),
                                   lambda: state.__setitem__('running', False),
                                   var_session_opt.get()),
                             daemon=True).start()

        def _apply_best():
            b = state.get('best')
            if not b:
                _set("尚未有最佳結果可套用。", "#FFCA28"); return
            if not messagebox.askyesno("套用最佳參數",
                                       f"要把策略「{s.get('name')}」的自訂參數改成:\n"
                                       f"{', '.join(f'{k}={v}' for k, v in b['params'].items())}\n\n"
                                       "提醒:歷史最佳不等於未來最佳,套用後請再走模擬觀察。", parent=dlg):
                return
            for i, x in enumerate(self.strategies):
                if x['id'] == s['id']:
                    merged = dict(x.get('custom_params') or {}); merged.update(b['params'])
                    self.strategies[i]['custom_params'] = merged
                    break
            self._qt_save(); self._qt_refresh_tree()
            self.log_message(f"【最佳化】「{s.get('name')}」已套用最佳參數: {b['params']}")
            _set("✅ 已套用最佳參數並存檔。", "#00E676")

        tk.Button(btns, text="🚀 開始掃描", bg="#00ACC1", fg="black", relief="flat",
                  font=('微軟正黑體', 11, 'bold'), padx=16, pady=4, command=_run).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="⛔ 停止", bg="#E53935", fg="white", relief="flat",
                  font=('微軟正黑體', 11), padx=14, pady=4,
                  command=lambda: (state.__setitem__('stop', True), _set("已要求停止,等待目前這組跑完...", "#FFCA28"))).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="✅ 套用最佳參數", bg="#00C853", fg="black", relief="flat",
                  font=('微軟正黑體', 11, 'bold'), padx=16, pady=4, command=_apply_best).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 11), padx=16, pady=4,
                  command=lambda: (state.__setitem__('stop', True), dlg.destroy())).pack(side=tk.LEFT, padx=6)

    def _qt_optimize_worker(self, s, search_spec, objective, min_trades, start_dt, end_dt,
                            should_stop, status_cb, done_cb, finally_cb, session_basis='all'):
        """【ADR-054/ADR-056】背景執行緒:下載資料 → 網格搜尋或隨機搜索 → 樣本外檢定。
        search_spec: ('grid', grid_dict, None) 或 ('random', ranges_dict, n_trials)。"""
        try:
            mode, spec, n_trials = search_spec
            contract, asset_type = self._qt_resolve(s)
            if contract is None:
                status_cb(f"✗ 合約解析失敗: {s.get('symbol')}", "#FF5252"); return
            tf = s.get('timeframe', '日K')
            tf_is_day = tf in ("日K", "周K", "月K")
            bt_key = (f"BT|{s.get('market')}|{str(s.get('symbol')).upper()}|"
                      f"{'day' if tf_is_day else 'min'}|{start_dt:%Y%m%d}|{end_dt:%Y%m%d}|{session_basis}")
            raw = self._bt_download_cache_get(bt_key)
            if raw is None:
                # 【ADR-058】期交所已涵蓋的部分不再向券商要 (使用者需求 #1)
                dl_from, dl_to, note = self._taifex_plan_download(
                    contract, asset_type, tf, start_dt, end_dt, session=session_basis, tag="最佳化")
                if note:
                    self.safe_after(0, self.log_message, f"【最佳化下載】{note}")
                if dl_from is None:
                    raw = pd.DataFrame()
                else:
                    raw = self._download_kbars_chunked(contract, dl_from, dl_to,
                                                       chunk_days=365 if tf_is_day else 90)
                    if raw is not None and not raw.empty:
                        self._bt_download_cache_put(bt_key, raw)
            df = self._resample_sj_df(raw, tf, asset_type, session_basis=session_basis) if (raw is not None and not raw.empty) else pd.DataFrame()
            # 【ADR-056】同回測 worker:最佳化也要能掃到期交所延伸出來的更早歷史。
            if asset_type == "future" and tf in ("日K", "周K", "月K"):
                df = self._extend_with_taifex(df, tf, contract=contract, session=session_basis)
            elif asset_type == "stock" and tf in ("日K", "周K", "月K"):
                sym = s.get('symbol')
                df = self._extend_with_yahoo(df, tf, sym=sym)
                try:
                    from data import dividend_store
                    if dividend_store.adjust_dataframe(df, str(sym)):
                        self.safe_after(0, self.log_message, f"【除權息】已對 {sym} 進行還原K線處理。")
                except Exception as e:
                    pass
            if df is None or df.empty:
                status_cb("✗ 取不到歷史資料 (券商下載失敗且無期交所本地歷史可用)。", "#FF5252"); return
            df = df[(df.index >= pd.Timestamp(start_dt)) & (df.index <= pd.Timestamp(end_dt) + pd.Timedelta(days=1))]
            if df is None or len(df) < 30:
                status_cb(f"✗ 此期間資料量不足 ({0 if df is None else len(df)} 根)。", "#FF5252"); return
            # 【ADR-075】看A做B:參數最佳化掃的是「條件參數」,而條件是看 A 的,
            # 所以要在 A (訊號來源) 的歷史上掃,才會找到對的訊號參數。掃描用的絕對
            # 損益是「看A做A」近似 (最佳化只需要相對排名),真實看A做B損益請用回測確認。
            opt_asset = asset_type
            if strategy_engine.watch_enabled(s):
                w_contract, w_asset, w_sym, w_mkt = self._qt_resolve_watch(s)
                if w_contract is None:
                    status_cb(f"✗ 看A商品解析失敗: {strategy_engine.watch_symbol_of(s)}", "#FF5252"); return
                w_tf = strategy_engine.watch_timeframe_of(s)
                a_df = self._qt_bt_load_df(w_contract, w_asset, w_sym, w_mkt, w_tf,
                                           start_dt, end_dt, session_basis=session_basis, tag="最佳化-看A")
                if a_df is None or len(a_df) < 30:
                    status_cb(f"✗ 看A ({w_sym}/{w_tf}) 歷史資料不足,無法最佳化。", "#FF5252"); return
                df = a_df; opt_asset = w_asset
                self.safe_after(0, self.log_message,
                                "【最佳化-看A做B】參數掃描在『看A』訊號商品上進行 (找對的訊號參數);"
                                "掃描顯示的絕對損益為看A做A近似,真實看A做B損益請用「🔬 回測」確認。")
            try:
                tick = tick_rules.get_tick(float(df['Close'].iloc[-1]), opt_asset, str(s.get('symbol')).upper())
            except Exception:
                tick = None
            slip = int(s.get('slippage_ticks', 2) or 0)
            total = n_trials if mode == 'random' else optimizer.count_combos(spec)

            def _prog(done, tot, combo, m):
                if done == 1 or done == tot or done % 5 == 0:
                    status_cb(f"⏳ 掃描中 {done}/{tot} 組 (目前 {', '.join(f'{k}={v}' for k, v in combo.items())} "
                              f"→ {m.get('trades', 0)} 筆,淨損益 {_fmt_amt_signed(m.get('total_pnl', 0))})", "#FFCA28")

            if mode == 'random':
                res = optimizer.random_search(s, df, spec, n_trials=n_trials, objective=objective,
                                              min_trades=min_trades, progress_cb=_prog,
                                              cost_params=self._cost_params(), slippage_ticks=slip,
                                              tick_size=tick, should_stop=should_stop)
            else:
                res = optimizer.optimize(s, df, spec, objective=objective, min_trades=min_trades,
                                         progress_cb=_prog, cost_params=self._cost_params(),
                                         slippage_ticks=slip, tick_size=tick, should_stop=should_stop)
            # 【ADR-054】對前 10 名做樣本內/外檢定 —— 揭露過度最佳化
            for r in res['results'][:10]:
                if not r['eligible']:
                    continue
                try:
                    wf = optimizer.walk_forward_check(s, df, r['params'], split_ratio=0.7,
                                                      slippage_ticks=slip, tick_size=tick,
                                                      cost_params=self._cost_params())
                    r['oos_pnl'] = wf['out_sample'].get('total_pnl', 0.0)
                except Exception:
                    r['oos_pnl'] = None
            done_cb(res)
            self.safe_after(0, self.log_message,
                            f"【最佳化】「{s.get('name')}」掃描 {res['evaluated']}/{total} 組完成。")
            # 【ADR-055】掃描「跑完但沒有數據」時,把真正的原因說出來,
            # 不要讓使用者面對一片空白的結果表自己猜。
            if res.get('error_summary'):
                self.safe_after(0, self.log_message, f"【最佳化-錯誤】{res['error_summary']}")
                status_cb(f"⚠ {res['error_summary']}", "#FF5252")
            elif res.get('best') is None:
                status_cb(f"⚠ 掃描完成,但沒有任何一組達到「最少交易筆數 {min_trades}」門檻,"
                          f"因此無最佳參數。請放寬門檻、拉長回測期間,或檢查策略是否幾乎不進場。", "#FFCA28")
        except Exception as e:
            status_cb(f"❌ 最佳化失敗: {type(e).__name__}: {e}", "#FF5252")
            self.safe_after(0, self.log_message, f"【最佳化】異常: {type(e).__name__}: {e}")
        finally:
            self.safe_after(0, lambda: setattr(self, '_backtest_running', False))
            try: finally_cb()
            except Exception: pass

    def _qt_backtest_selected(self):
        s = self._qt_selected()
        if not s:
            self.log_message("【回測】請先在清單中選取要回測的策略。")
            return
        if s.get('kind') == 'custom':
            if not s.get('source_code') or s.get('qty', 0) <= 0:
                self.log_message(f"【回測】自訂策略「{s.get('name')}」設定不完整,無法回測。")
                return
        elif s.get('kind') == chukuangren_band.KIND:
            # 【新ADR】v1 尚未支援回測/最佳化:隔天中午12點二次確認的雙時間週期
            # (日K訊號 + 5分K確認) 狀態機無法套進現有單一 df 的回測迴圈,需要
            # 另外設計才能支援;先誠實告知,只支援模擬/實單監控。
            self.log_message(f"【回測】「{s.get('name')}」(終極波段策略) 目前尚未支援回測/最佳化,"
                             "僅支援模擬/實單監控 (雙時間週期的狀態機需要另外設計回測邏輯)。")
            return
        else:
            ok, msg = strategy_engine.validate_strategy(s)
            if not ok:
                self.log_message(f"【回測】策略「{s.get('name')}」設定不完整,無法回測: {msg}")
                return
        if not (self.api_logged_in and HAS_SJ and self.sj_api):
            self.log_message("【回測】需要先登入券商 API 以取得歷史K線資料。")
            return
        if getattr(self, '_backtest_running', False):
            # 【ADR-057】使用者需求 #9:回測卡住/失敗時要能強制終止,不能永遠
            # 被「已有回測進行中」擋著、只能重開程式。
            self._qt_offer_abort_backtest("回測")
            return
        self._qt_backtest_running_since = time.time()
        self._backtest_cancel = False
        self._qt_backtest_ask_range(s)

    # ================= 【ADR-062】策略比較 (使用者需求 #2) =================
    def _qt_compare_dialog(self):
        """把多個策略在「同一段期間、同一組設定」下各跑一次回測,並排比較。

        存在理由:單筆長抱 / 累積加碼 / 定期定額 / 主動策略,四者要放在一起
        才知道誰真的比較好。分開跑再自己抄數字,期間或成本設定一不小心就
        不一樣,比較就沒有意義 —— 所以這裡強制所有策略共用同一組設定。
        """
        if not self.strategies:
            messagebox.showinfo("策略比較", "目前沒有任何策略可比較。", parent=self._qt_dialog_parent())
            return
        if getattr(self, '_backtest_running', False):
            self._qt_offer_abort_backtest("比較")
            return
        from datetime import datetime as _dt
        dlg = tk.Toplevel(self); dlg.title("📊 策略比較 — 同期間、同設定")
        dlg.configure(bg="#1A2026"); self.center_window(dlg, 1120, 700)
        try: dlg.lift(); dlg.focus_force()
        except Exception: pass
        tk.Label(dlg, text=("勾選要比較的策略 (2 個以上),它們會在同一段期間、同一組成本與盤別設定下各跑一次回測。\n"
                            "⚠ 只有「商品與週期相同」的策略才真的可比;不同商品放在一起比,比的是商品不是策略。"),
                 bg="#2A1215", fg="#FF8A80", font=('微軟正黑體', 9), justify=tk.LEFT,
                 wraplength=1080).pack(fill=tk.X, padx=10, pady=(10, 4))

        pick = tk.Frame(dlg, bg="#1A2026"); pick.pack(fill=tk.X, padx=12)
        tk.Label(pick, text="要比較的策略:", bg="#1A2026", fg="white",
                 font=('微軟正黑體', 9, 'bold')).pack(anchor='w')
        chk_frame = tk.Frame(pick, bg="#12161A"); chk_frame.pack(fill=tk.X, pady=2)
        vars_sel = {}
        for i, st in enumerate(self.strategies):
            v = tk.BooleanVar(value=False)
            vars_sel[st['id']] = v
            kind = ('長抱' if st.get('bnh_mode') == 'single' else
                    '加碼' if st.get('bnh_mode') == 'accumulate' else
                    '定期定額') if st.get('buy_and_hold') else (
                    '自訂' if st.get('kind') == 'custom' else
                    '終極波段(不支援回測)' if st.get('kind') == chukuangren_band.KIND else '條件')
            tk.Checkbutton(chk_frame, variable=v, bg="#12161A", fg="#E6EDF3", selectcolor="#2A323D",
                           font=('微軟正黑體', 9), activebackground="#12161A",
                           text=f"{st.get('name','')}  [{st.get('symbol','')} {st.get('timeframe','')} / {kind}]"
                           ).grid(row=i // 3, column=i % 3, sticky='w', padx=6, pady=1)

        form = tk.Frame(dlg, bg="#1A2026"); form.pack(fill=tk.X, padx=12, pady=(8, 2))
        tk.Label(form, text="起始日", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=0, column=0)
        e_s = tk.Entry(form, width=12, bg="#2A323D", fg="white")
        e_s.insert(0, (_dt.now() - timedelta(days=3650)).strftime('%Y-%m-%d'))
        e_s.grid(row=0, column=1, padx=4)
        tk.Label(form, text="結束日", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=0, column=2)
        e_e = tk.Entry(form, width=12, bg="#2A323D", fg="white")
        e_e.insert(0, _dt.now().strftime('%Y-%m-%d')); e_e.grid(row=0, column=3, padx=4)
        qf = tk.Frame(form, bg="#1A2026"); qf.grid(row=0, column=4, columnspan=8, sticky='w', padx=(10, 0))

        def _q(days):
            end = _dt.now(); start = end - timedelta(days=days)
            e_s.delete(0, tk.END); e_s.insert(0, start.strftime('%Y-%m-%d'))
            e_e.delete(0, tk.END); e_e.insert(0, end.strftime('%Y-%m-%d'))
        for i, (lab, d) in enumerate([("1Y", 365), ("3Y", 1095), ("5Y", 1825),
                                      ("10Y", 3650), ("15Y", 5475), ("20Y", 7300)]):
            tk.Button(qf, text=lab, bg="#2A323D", fg="#29B6F6", relief="flat",
                      font=('微軟正黑體', 8, 'bold'), width=4,
                      command=(lambda dd=d: _q(dd))).grid(row=0, column=i, padx=1)
        var_sess = tk.StringVar(value='all')
        tk.Label(form, text="盤別", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=1, column=0, pady=(6, 0))
        cb_sess = ttk.Combobox(form, width=16, state='readonly', style="BlackText.TCombobox",
                               values=['近全(含夜盤)', '只用日盤'])
        cb_sess.current(0); cb_sess.grid(row=1, column=1, columnspan=2, sticky='w', padx=4, pady=(6, 0))

        cols = ('name', 'mode', 'pnl', 'ret', 'trades', 'win', 'mdd', 'cost', 'invested', 'tpy')
        heads = {'name': '策略', 'mode': '類型', 'pnl': '淨損益', 'ret': '報酬率',
                 'trades': '交易數', 'win': '勝率', 'mdd': '最大回撤',
                 'cost': '交易成本(費+稅)', 'invested': '總持有成本', 'tpy': '每年交易次數'}
        widths = {'name': 150, 'mode': 90, 'pnl': 130, 'ret': 80, 'trades': 70,
                  'win': 70, 'mdd': 120, 'cost': 120, 'invested': 140, 'tpy': 100}
        tree = ttk.Treeview(dlg, columns=cols, show='headings', height=10, style="Optim.Treeview")
        for c in cols:
            tree.heading(c, text=heads[c]); tree.column(c, width=widths[c], anchor='center')
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        tree.tag_configure('best', foreground='#000000', background='#C8F7C5')
        tree.tag_configure('norm', foreground='#000000', background='#FFFFFF')
        tree.tag_configure('bad', foreground='#B00020', background='#FFE9E9')

        lbl_st = tk.Label(dlg, text="就緒:勾選 2 個以上策略後按「開始比較」。",
                          bg="#1A2026", fg="#29B6F6", font=('微軟正黑體', 9),
                          anchor='w', justify=tk.LEFT, wraplength=1080)
        lbl_st.pack(fill=tk.X, padx=12)
        state = {'running': False}

        def _set(msg, col="#29B6F6"):
            try:
                if lbl_st.winfo_exists():
                    lbl_st.config(text=msg, fg=col)
            except Exception:
                pass

        def _fill(rows, note):
            try:
                if not dlg.winfo_exists():
                    return
            except Exception:
                return
            for i in tree.get_children():
                tree.delete(i)
            ok_rows = [r for r in rows if r.get('ok')]
            best_pnl = max((r['m']['total_pnl'] for r in ok_rows), default=None)
            for r in rows:
                if not r.get('ok'):
                    tree.insert('', tk.END, values=(r['name'], r.get('mode', ''), r.get('err', '失敗'),
                                                    '—', '—', '—', '—', '—', '—', '—'), tags=('bad',))
                    continue
                m = r['m']
                tag = 'best' if (best_pnl is not None and m['total_pnl'] == best_pnl) else 'norm'
                inv = m.get('bnh_total_invested', 0) or m.get('cost_basis_max', 0)
                tree.insert('', tk.END, values=(
                    r['name'], r.get('mode', ''),
                    _fmt_amt_signed(m['total_pnl']), f"{m['total_return_pct']:+.2f}%",
                    m['trades'], f"{m['win_rate']:.1f}%",
                    _fmt_amt(m['max_drawdown']), _fmt_amt(m.get('total_cost', 0)),
                    _fmt_amt(inv), f"{m.get('trades_per_year', 0):.2f}"), tags=(tag,))
            _set(note, "#00E676")

        def _run():
            if state['running']:
                return
            picked = [st for st in self.strategies if vars_sel.get(st['id']) and vars_sel[st['id']].get()]
            if len(picked) < 2:
                _set("✗ 請至少勾選 2 個策略。", "#FF5252"); return
            try:
                sd = _dt.strptime(e_s.get().strip(), '%Y-%m-%d')
                ed = _dt.strptime(e_e.get().strip(), '%Y-%m-%d')
                if sd >= ed:
                    raise ValueError
            except ValueError:
                _set("✗ 日期格式須為 YYYY-MM-DD 且起始日早於結束日。", "#FF5252"); return
            syms = {(st.get('symbol'), st.get('timeframe')) for st in picked}
            warn = "" if len(syms) == 1 else "  ⚠ 你選的策略商品/週期不同,比較結果反映的是商品差異而非策略優劣!"
            state['running'] = True
            self._backtest_running = True
            self._backtest_cancel = False
            _set(f"⏳ 依序回測 {len(picked)} 個策略中...{warn}", "#FFCA28")
            sess = 'day' if cb_sess.current() == 1 else 'all'
            threading.Thread(target=self._qt_compare_worker,
                             args=([copy.deepcopy(x) for x in picked], sd, ed, sess,
                                   lambda msg, col: self.safe_after(0, _set, msg, col),
                                   lambda rows, note: self.safe_after(0, _fill, rows, note),
                                   lambda: state.__setitem__('running', False), warn),
                             daemon=True).start()

        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(pady=10)
        tk.Button(btns, text="▶ 開始比較", bg="#AB47BC", fg="white", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=20, pady=3, command=_run).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="⛔ 停止", bg="#FF1744", fg="white", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=16, pady=3,
                  command=lambda: setattr(self, '_backtest_cancel', True)).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=20, pady=3, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _qt_compare_worker(self, strats, start_dt, end_dt, session_basis,
                           status_cb, done_cb, finally_cb, warn=""):
        """背景:逐一回測並收集結果。任一策略失敗不影響其他 (該列標紅顯示原因)。"""
        rows = []
        try:
            for i, st in enumerate(strats, 1):
                if getattr(self, '_backtest_cancel', False) or self._closing:
                    status_cb(f"已中止 (完成 {i-1}/{len(strats)})。", "#FFCA28")
                    break
                mode = ('單筆長抱' if st.get('bnh_mode') == 'single' else
                        '累積加碼' if st.get('bnh_mode') == 'accumulate' else
                        '定期定額') if st.get('buy_and_hold') else (
                        '自訂Python' if st.get('kind') == 'custom' else
                        '終極波段策略' if st.get('kind') == chukuangren_band.KIND else '條件策略')
                if st.get('kind') == chukuangren_band.KIND:
                    rows.append({'name': st.get('name'), 'mode': mode, 'ok': False,
                                 'err': '尚未支援回測 (僅支援模擬/實單監控)'})
                    continue
                status_cb(f"⏳ ({i}/{len(strats)}) 回測「{st.get('name')}」...{warn}", "#FFCA28")
                try:
                    df = self._qt_prepare_df(st, start_dt, end_dt, session_basis)
                    if df is None or len(df) < 30:
                        rows.append({'name': st.get('name'), 'mode': mode, 'ok': False,
                                     'err': f"資料不足 ({0 if df is None else len(df)} 根)"})
                        continue
                    tick = None
                    try:
                        _, at = self._qt_resolve(st)
                        tick = tick_rules.get_tick(float(df['Close'].iloc[-1]), at,
                                                   str(st.get('symbol')).upper())
                    except Exception:
                        pass
                    r = backtest.run_backtest(
                        st, df, slippage_ticks=int(st.get('slippage_ticks', 2) or 0),
                        tick_size=tick, cost_params=self._cost_params(), apply_cost_model=True,
                        should_stop=lambda: getattr(self, '_backtest_cancel', False) or self._closing)
                    rows.append({'name': st.get('name'), 'mode': mode, 'ok': True, 'm': r['metrics']})
                except Exception as e:
                    rows.append({'name': st.get('name'), 'mode': mode, 'ok': False,
                                 'err': f"{type(e).__name__}: {str(e)[:60]}"})
            note = (f"✅ 完成:比較 {sum(1 for r in rows if r.get('ok'))}/{len(strats)} 個策略"
                    f" ({start_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d},"
                    f"盤別 {'只用日盤' if session_basis == 'day' else '近全'})。"
                    f"綠底 = 淨損益最高。{warn}")
            done_cb(rows, note)
            self.safe_after(0, self.log_message, f"【策略比較】{note}")
        except Exception as e:
            status_cb(f"❌ 比較失敗: {type(e).__name__}: {e}", "#FF5252")
        finally:
            self.safe_after(0, lambda: setattr(self, '_backtest_running', False))
            try:
                finally_cb()
            except Exception:
                pass

    def _qt_prepare_df(self, st, start_dt, end_dt, session_basis='all'):
        """【ADR-062】取得某策略回測用的 df —— 與 _qt_backtest_worker 同一套流程
        (下載/期交所涵蓋跳過/延伸/裁切),抽出來供策略比較共用,避免兩份分歧。"""
        contract, asset_type = self._qt_resolve(st)
        if contract is None:
            raise RuntimeError(f"合約解析失敗: {st.get('symbol')}")
        tf = st.get('timeframe', '日K')
        tf_is_day = tf in ("日K", "周K", "月K")
        bt_key = (f"BT|{st.get('market')}|{str(st.get('symbol')).upper()}|"
                  f"{'day' if tf_is_day else 'min'}|{start_dt:%Y%m%d}|{end_dt:%Y%m%d}|{session_basis}")
        raw = self._bt_download_cache_get(bt_key)
        if raw is None:
            dl_from, dl_to, note = self._taifex_plan_download(
                contract, asset_type, tf, start_dt, end_dt, session=session_basis, tag="比較")
            if note:
                self.safe_after(0, self.log_message, f"【策略比較】{note}")
            if dl_from is None:
                raw = pd.DataFrame()
            else:
                raw = self._download_kbars_chunked(contract, dl_from, dl_to,
                                                   chunk_days=365 if tf_is_day else 90)
                if raw is not None and not raw.empty:
                    self._bt_download_cache_put(bt_key, raw)
        df = (self._resample_sj_df(raw, tf, asset_type=asset_type, session_basis=session_basis)
              if (raw is not None and not raw.empty) else pd.DataFrame())
        if asset_type == "future" and tf_is_day:
            df = self._extend_with_taifex(df, tf, contract=contract, session=session_basis)
        elif asset_type == "stock" and tf_is_day:
            df = self._extend_with_yahoo(df, tf, sym=s.get('symbol'))
        if df is None or df.empty:
            return df
        try:
            df = df[(df.index >= pd.Timestamp(start_dt)) & (df.index <= pd.Timestamp(end_dt) + pd.Timedelta(days=1))]
        except Exception:
            pass
        # 【ADR-101】回測路徑也要併籌碼,且走與實盤完全相同的
        # _qt_attach_chips → chips_features.attach_chips,包含同一套未來函數
        # 防護。兩邊共用一條路,才不會出現「回測對、實盤錯」的雙軌問題。
        if df is not None and not df.empty and strategy_engine.strategy_uses_chips(st):
            df = self._qt_attach_chips(df, st, cache_sym=str(st.get('symbol')).upper(),
                                       cache_market=st.get('market'))
        return df

    def _qt_offer_abort_backtest(self, what="回測"):
        """【ADR-057】已有回測/最佳化在跑時,提供「強制終止」的出口。

        誠實說明能做到什麼:我們無法從外部安全地「殺掉」一條 Python 執行緒
        (沒有安全的 thread kill),所以強制終止的作法是:
          (1) 設 self._backtest_cancel = True —— 回測/最佳化迴圈每根K棒/每組
              參數都會檢查這個旗標,看到就主動跳出 (這是真正會停下來的路徑);
          (2) 立刻釋放 _backtest_running 旗標,讓使用者可以馬上再按一次回測,
              不必等舊的那條完全收工。
        若舊執行緒卡在「下載K線」這種不受我們控制的 API 呼叫上,它會在該次
        呼叫自然結束後才看到旗標而退出——這點必須讓使用者知道,不要假裝
        按下去就一定瞬間停止。"""
        elapsed = ""
        try:
            t0 = getattr(self, '_qt_backtest_running_since', None)
            if t0:
                elapsed = f"(已執行 {int(time.time() - t0)} 秒) "
        except Exception:
            pass
        ans = messagebox.askyesno(
            f"已有{what}進行中",
            f"目前已有一個回測/最佳化正在執行 {elapsed}。\n\n"
            "要強制終止它嗎?\n\n"
            "• 按「是」:送出取消訊號並解除鎖定,你可以立刻重新開始。\n"
            "  (若舊工作正卡在券商下載 API 上,它會在該次下載結束後才真正停止)\n"
            "• 按「否」:繼續等待目前這個完成。", parent=self._qt_dialog_parent())
        if not ans:
            return
        self._backtest_cancel = True
        self._backtest_running = False
        self.log_message(f"【{what}】已送出強制終止訊號,並解除鎖定 (可立即重新開始)。")

    @staticmethod
    def _parse_kv_params(text):
        """把 'fast=5, slow=20' 這種字串解析成 dict (int → float → 字串,依序嘗試)。
        【ADR-056】從自訂策略編輯器的區域函式抽出來,回測對話框的參數欄位
        也要用同一套解析規則,不能各寫一份互相不一致。"""
        out = {}
        for pair in (text or '').split(','):
            pair = pair.strip()
            if not pair or '=' not in pair:
                continue
            k, v = pair.split('=', 1)
            k = k.strip(); v = v.strip()
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
        return out

    def _qt_backtest_ask_range(self, s):
        """【ADR-043 第7項】回測前讓使用者自訂期間 (起訖日),預設用週期對應天數。
        【ADR-056】自訂策略再加一列「本次回測參數」:使用者常見的流程是
        「調參數 → 回測看結果 → 再調參數 → 再回測」,舊版每次都要離開回測、
        回策略編輯器改參數、存檔、再回來按回測——現在直接在同一個對話框改,
        按「開始回測」立即用新參數跑。預設不會覆寫已儲存的策略,除非勾選
        「同時更新已儲存的策略參數」。"""
        from datetime import datetime as _dt
        default_days = self.QT_BACKTEST_DAYS.get(s.get('timeframe', '5分K'), 90)
        end_default = _dt.now().strftime('%Y-%m-%d')
        start_default = (_dt.now() - timedelta(days=default_days)).strftime('%Y-%m-%d')
        dlg = tk.Toplevel(self)
        dlg.title("回測期間設定")
        dlg.configure(bg="#1A2026")
        is_custom = s.get('kind') == 'custom'
        self.center_window(dlg, 620, 560 if is_custom else 420)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force(); dlg.grab_set()
        except Exception:
            pass
        tk.Label(dlg, text=f"回測策略:{s.get('name')} ({s.get('symbol')} {s.get('timeframe')})",
                 bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 10, 'bold')).pack(pady=(14, 8))
        row = tk.Frame(dlg, bg="#1A2026"); row.pack(pady=4)
        tk.Label(row, text="起始日", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=0, column=0, padx=4)
        e_start = tk.Entry(row, width=14, bg="#2A323D", fg="white", justify="center"); e_start.insert(0, start_default); e_start.grid(row=0, column=1, padx=4)
        tk.Label(row, text="結束日", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).grid(row=1, column=0, padx=4, pady=(6, 0))
        e_end = tk.Entry(row, width=14, bg="#2A323D", fg="white", justify="center"); e_end.insert(0, end_default); e_end.grid(row=1, column=1, padx=4, pady=(6, 0))
        # 【ADR-058】使用者需求 #4:期間快選鈕。手動打日期容易打錯,而且
        # 「10 年前的今天」這種要自己算。按一下就把起訖日填好 (結束日固定為今天)。
        qrow = tk.Frame(dlg, bg="#1A2026"); qrow.pack(pady=(8, 2))
        tk.Label(qrow, text="快選期間:", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 9)).grid(row=0, column=0, padx=(0, 4))

        def _quick(days):
            try:
                end = _dt.now()
                start = end - timedelta(days=days)
                e_start.delete(0, tk.END); e_start.insert(0, start.strftime('%Y-%m-%d'))
                e_end.delete(0, tk.END); e_end.insert(0, end.strftime('%Y-%m-%d'))
            except Exception:
                pass

        _presets = [("3M", 90), ("6M", 182), ("1Y", 365), ("2Y", 730), ("3Y", 1095),
                    ("5Y", 1825), ("7Y", 2555), ("10Y", 3650), ("15Y", 5475), ("20Y", 7300)]
        for _i, (_lab, _d) in enumerate(_presets):
            tk.Button(qrow, text=_lab, bg="#2A323D", fg="#29B6F6", relief="flat",
                      font=('微軟正黑體', 8, 'bold'), width=4, padx=2, pady=1,
                      command=(lambda dd=_d: _quick(dd))).grid(row=0, column=_i + 1, padx=1)
        tk.Label(dlg, text="格式 YYYY-MM-DD;範圍越大下載越久。分K資料券商通常只保留近期。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(pady=(4, 0))

        # 【ADR-058】使用者需求 #3:期貨日/周/月K的「盤別口徑」選擇。
        var_session = tk.StringVar(value='all')
        _is_fut_daily = (str(s.get('market')) == '台期貨'
                         and s.get('timeframe') in ('日K', '周K', '月K'))
        if _is_fut_daily:
            ttk.Separator(dlg, orient='horizontal').pack(fill=tk.X, padx=12, pady=(8, 4))
            srow = tk.Frame(dlg, bg="#1A2026"); srow.pack(fill=tk.X, padx=12)
            tk.Label(srow, text="盤別口徑", bg="#1A2026", fg="white",
                     font=('微軟正黑體', 9, 'bold')).pack(anchor='w')
            tk.Radiobutton(srow, text="近全 (含夜盤) — 貼近現在的實際交易",
                           variable=var_session, value='all', bg="#1A2026", fg="#E6EDF3",
                           selectcolor="#2A323D", font=('微軟正黑體', 9),
                           activebackground="#1A2026").pack(anchor='w')
            tk.Radiobutton(srow, text="只用日盤 (08:45–13:45) — 長期回測口徑一致",
                           variable=var_session, value='day', bg="#1A2026", fg="#E6EDF3",
                           selectcolor="#2A323D", font=('微軟正黑體', 9),
                           activebackground="#1A2026").pack(anchor='w')
            tk.Label(srow, text=("⚠ 期交所夜盤 2017-05-15 才上線。回測若橫跨這天又選「近全」,\n"
                                 "　 前段其實只有日盤、後段含夜盤,等於策略前後面對不同口徑的商品\n"
                                 "　 (實測臺指期隔夜跳空中位數 0.38% → 0.06%,差約 6 倍)。\n"
                                 "　 要做一致口徑的長期回測,請選「只用日盤」。"),
                     bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 8),
                     justify=tk.LEFT).pack(anchor='w', pady=(2, 4))

        param_rows = []
        var_persist = None
        if is_custom:
            ttk.Separator(dlg, orient='horizontal').pack(fill=tk.X, padx=12, pady=(10, 6))
            prow = tk.Frame(dlg, bg="#1A2026"); prow.pack(fill=tk.X, padx=12)
            tk.Label(prow, text="本次回測參數", bg="#1A2026", fg="white", font=('微軟正黑體', 9)).pack(anchor='w')
            # 【ADR-057】使用者需求 #7:改成「一列一個參數、名稱 = 值」的表格式,
            # 不要擠成一行 "fast=7, slow=35" 的字串。這樣參數多的時候一眼就看得出
            # 有哪些參數、各是多少,也不會打錯逗號整串解析失敗。
            cur_params = dict(s.get('custom_params') or {})
            if not cur_params:
                # 策略還沒設過參數:從程式碼裡把 ctx.param('x', ...) 的名稱掃出來當空白列,
                # 使用者才知道這支策略「可以」調哪些參數 (掃不到就留一列空白讓他自己填)。
                try:
                    found = re.findall(r"""ctx\.param\(\s*['"]([A-Za-z_]\w*)['"]""", s.get('source_code', '') or '')
                    cur_params = {k: '' for k in dict.fromkeys(found)}
                except Exception:
                    cur_params = {}
            grid = tk.Frame(prow, bg="#1A2026"); grid.pack(fill=tk.X, pady=(2, 0))
            # 沿用外層宣告的 param_rows (不要重新指派,否則 _go() 讀到的是空 list)

            def _add_param_row(name='', value=''):
                r = len(param_rows)
                en = tk.Entry(grid, width=18, bg="#2A323D", fg="white", font=('微軟正黑體', 9))
                en.insert(0, str(name)); en.grid(row=r, column=0, padx=(0, 4), pady=1, sticky='w')
                tk.Label(grid, text="=", bg="#1A2026", fg="#8A99AD").grid(row=r, column=1, padx=2)
                ev = tk.Entry(grid, width=14, bg="#2A323D", fg="white", font=('微軟正黑體', 9))
                ev.insert(0, str(value)); ev.grid(row=r, column=2, padx=(4, 0), pady=1, sticky='w')
                param_rows.append((en, ev))

            for _k, _v in cur_params.items():
                _add_param_row(_k, _v)
            if not param_rows:
                _add_param_row()
            tk.Button(prow, text="＋ 新增一列參數", bg="#2A323D", fg="#29B6F6", relief="flat",
                      font=('微軟正黑體', 8), command=lambda: _add_param_row()).pack(anchor='w', pady=(3, 0))
            tk.Label(prow, text="改這裡不用先回策略編輯器,按「開始回測」就套用;名稱留白的列會被忽略。",
                     bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(anchor='w', pady=(2, 4))

            def _collect_param_rows():
                """把表格式輸入收集成 dict,型別依序嘗試 int → float → 字串
                (與策略編輯器的 _parse_kv_params 同一套規則)。"""
                out = {}
                for en, ev in param_rows:
                    try:
                        k = en.get().strip()
                        if not k:
                            continue
                        out.update(self._parse_kv_params(f"{k}={ev.get().strip()}"))
                    except Exception:
                        continue
                return out
            var_persist = tk.BooleanVar(value=False)
            tk.Checkbutton(prow, text="同時更新已儲存的策略參數 (下次開啟策略清單也會是這組)",
                          variable=var_persist, bg="#1A2026", fg="#8A99AD", selectcolor="#2A323D",
                          font=('微軟正黑體', 8)).pack(anchor='w')

        def _go():
            # 【ADR-047 崩潰修正】必須「先讀取輸入框的值、再 destroy 對話框」。
            # 舊版順序是 destroy → log_message 裡才呼叫 e_start.get() → 讀已
            # 銷毀的 widget → TclError: invalid command name → 例外中斷,
            # 背景執行緒沒起跑,但 _backtest_running 已被設 True 且永遠不會
            # 復位 → 之後每次按回測都被「已有回測進行中」擋下,看起來完全沒
            # 反應 (使用者實例:2022-01-01~2026-07-18)。
            start_s = e_start.get().strip()
            end_s = e_end.get().strip()
            try:
                sd_ = _dt.strptime(start_s, '%Y-%m-%d')
                ed_ = _dt.strptime(end_s, '%Y-%m-%d')
                if sd_ >= ed_:
                    raise ValueError
            except ValueError:
                self.log_message("【回測】日期格式錯誤或起始日不早於結束日,未執行。")
                messagebox.showwarning("日期格式錯誤",
                                       "請用 YYYY-MM-DD 格式,且起始日需早於結束日。", parent=dlg)
                return
            run_s = copy.deepcopy(s)
            if is_custom and param_rows:
                new_params = _collect_param_rows()
                run_s['custom_params'] = new_params
                if var_persist.get():
                    for i, x in enumerate(self.strategies):
                        if x['id'] == s['id']:
                            self.strategies[i]['custom_params'] = new_params
                            break
                    self._qt_save(); self._qt_refresh_tree()
                    self.log_message(f"【回測】已同時更新策略「{s.get('name')}」的參數: {new_params}")
            dlg.destroy()
            try:
                self._backtest_running = True
                self.log_message(f"【回測】開始回測「{s.get('name')}」{start_s} ~ {end_s},下載中...")
                threading.Thread(target=self._qt_backtest_worker,
                                 args=(run_s, sd_, ed_, var_session.get()), daemon=True).start()
            except Exception as e:
                # 起跑失敗一定要復位旗標,否則回測功能從此鎖死
                self._backtest_running = False
                self.log_message(f"【回測】啟動失敗: {type(e).__name__}: {e}")
        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(pady=12)
        tk.Button(btns, text="開始回測", bg="#AB47BC", fg="white", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=16, pady=4, command=_go).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=16, pady=4, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _qt_bt_load_df(self, contract, asset_type, symbol, market, tf, start_dt, end_dt,
                       session_basis='all', tag="回測", log=True):
        """【ADR-075】回測用:下載(快取)+重採樣+期交所/yahoo 延伸+範圍裁切,回傳 df。
        抽出來讓「看A做B」能分別載入 A (訊號) 與 B (成交) 兩個商品的歷史。"""
        tf_is_day = tf in ("日K", "周K", "月K")
        chunk = 365 if tf_is_day else 90
        bt_key = (f"BT|{market}|{str(symbol).upper()}|{tf}|"
                  f"{'day' if tf_is_day else 'min'}|{start_dt:%Y%m%d}|{end_dt:%Y%m%d}|{session_basis}")
        raw = self._bt_download_cache_get(bt_key)
        if raw is None:
            def _prog(done, total, s0, s1, n_rows):
                if log and (done == 1 or done == total or done % 2 == 0):
                    self.safe_after(0, self.log_message,
                                    f"【{tag}下載-{symbol}】進度 {done}/{total} 段 ({s0:%Y-%m-%d}~{s1:%Y-%m-%d},{n_rows} 根)...")
            dl_from, dl_to, note = self._taifex_plan_download(
                contract, asset_type, tf, start_dt, end_dt, session=session_basis, tag=tag)
            if note and log:
                self.safe_after(0, self.log_message, f"【{tag}下載-{symbol}】{note}")
            if dl_from is None:
                raw = pd.DataFrame()
            else:
                raw = self._download_kbars_chunked(contract, dl_from, dl_to, chunk_days=chunk, progress_cb=_prog)
                if raw is not None and not raw.empty:
                    self._bt_download_cache_put(bt_key, raw)
        df = (self._resample_sj_df(raw, tf, asset_type=asset_type, session_basis=session_basis)
              if (raw is not None and not raw.empty) else pd.DataFrame())
        if asset_type == "future" and tf_is_day:
            df = self._extend_with_taifex(df, tf, contract=contract, session=session_basis)
        elif asset_type == "stock" and tf_is_day:
            df = self._extend_with_yahoo(df, tf, sym=symbol)
            try:
                from data import dividend_store
                dividend_store.adjust_dataframe(df, str(symbol))
            except Exception:
                pass
        if df is None or df.empty:
            return pd.DataFrame()
        try:
            df = df[(df.index >= pd.Timestamp(start_dt)) & (df.index <= pd.Timestamp(end_dt) + pd.Timedelta(days=1))]
        except Exception:
            pass
        return df

    def _qt_backtest_worker(self, s, start_dt=None, end_dt=None, session_basis='all'):
        try:
            contract, asset_type = self._qt_resolve(s)
            if contract is None:
                self.safe_after(0, self.log_message, f"【回測】合約解析失敗: {s.get('symbol')}")
                return
            tf = s.get('timeframe', '5分K')
            # 【ADR-043 第7項】使用者指定期間優先;未指定則用週期預設天數
            if end_dt is None:
                end_dt = datetime.now()
            if start_dt is None:
                days = self.QT_BACKTEST_DAYS.get(tf, 90)
                start_dt = end_dt - timedelta(days=days)
            # 【ADR-047 → ADR-052 提速】回測慢的三個原因與對策:
            #   (a) shioaji 只提供「分K」原始資料,日K回測 4 年半仍要下載約
            #       百萬根分K —— 這是結構性成本,無法消除,只能少跑幾趟、
            #       並且不要重複跑。
            #   (b) 舊版固定 90 天/段 → 4.5 年切 19 段,每段都有 pace 間隔與
            #       往返延遲。日K以上改 365 天/段 (5 段),往返次數降到 1/4。
            #   (c) 同一商品同一範圍重複回測時整批重抓 → 加入回測下載快取
            #       (記憶體,10 分鐘內有效),第二次起近乎瞬間完成。
            _t0 = time.time()
            tf_is_day = tf in ("日K", "周K", "月K")
            chunk = 365 if tf_is_day else 90
            bt_key = (f"BT|{s.get('market')}|{str(s.get('symbol')).upper()}|"
                      f"{'day' if tf_is_day else 'min'}|{start_dt:%Y%m%d}|{end_dt:%Y%m%d}|{session_basis}")
            raw = self._bt_download_cache_get(bt_key)
            if raw is not None:
                self.safe_after(0, self.log_message,
                                f"【回測下載】命中快取 ({len(raw)} 根原始K棒),略過下載。")
            else:
                def _prog(done, total, s0, s1, n_rows):
                    if done == 1 or done == total or done % 2 == 0:
                        self.safe_after(0, self.log_message,
                                        f"【回測下載】進度 {done}/{total} 段 ({s0:%Y-%m-%d}~{s1:%Y-%m-%d},{n_rows} 根)...")
                # 【ADR-058】使用者需求 #1:期交所本地歷史已涵蓋的區間不再向券商要。
                # 這是「被流量管制擋住」最有效的解法 —— 不是想辦法閃過管制,
                # 而是根本不發那些請求 (同時也不吃每日流量配額)。
                dl_from, dl_to, note = self._taifex_plan_download(
                    contract, asset_type, tf, start_dt, end_dt, session=session_basis, tag="回測")
                if note:
                    self.safe_after(0, self.log_message, f"【回測下載】{note}")
                if dl_from is None:
                    raw = pd.DataFrame()
                else:
                    raw = self._download_kbars_chunked(contract, dl_from, dl_to,
                                                       chunk_days=chunk, progress_cb=_prog)
                    if raw is not None and not raw.empty:
                        self._bt_download_cache_put(bt_key, raw)
                        self.safe_after(0, self.log_message,
                                        f"【回測下載】完成 {len(raw)} 根原始K棒,耗時 {time.time()-_t0:.1f} 秒 "
                                        f"(已快取,10 分鐘內同商品同範圍重跑免下載)。")
            df = (self._resample_sj_df(raw, tf, asset_type=asset_type, session_basis=session_basis)
                  if (raw is not None and not raw.empty) else pd.DataFrame())
            # 【ADR-056】期貨 R1 連續合約:回測也要吃期交所歷史延伸,不能只有
            # 主圖顯示吃得到——這是使用者回報「已匯入期交所歷史(2000年),回測
            # 範圍卻還是卡在shioaji深度(~5年)」的根因。必須在「依範圍裁切」之前
            # 做,否則使用者指定的更早 start_dt 會把延伸出來的資料切光。
            if asset_type == "future" and tf in ("日K", "周K", "月K"):
                df = self._extend_with_taifex(df, tf, contract=contract, session=session_basis)
            elif asset_type == "stock" and tf in ("日K", "周K", "月K"):
                sym = s.get('symbol')
                df = self._extend_with_yahoo(df, tf, sym=sym)
                try:
                    from data import dividend_store
                    if dividend_store.adjust_dataframe(df, str(sym)):
                        self.safe_after(0, self.log_message, f"【除權息】已對 {sym} 進行還原K線處理。")
                except Exception as e:
                    pass
            if df is None or df.empty:
                self.safe_after(0, self.log_message,
                                "【回測】取不到歷史K線資料 (券商下載失敗且無期交所本地歷史可用)。")
                return
            # 依使用者指定範圍精確裁切 (下載可能多給)
            try:
                df = df[(df.index >= pd.Timestamp(start_dt)) & (df.index <= pd.Timestamp(end_dt) + pd.Timedelta(days=1))]
            except Exception:
                pass
            # 【ADR-058】盤別口徑實證檢查:橫跨 2017-05-15 夜盤上線日且口徑不一致時
            # 主動警告 (這是市場事實造成的,不是資料錯誤,但必須讓使用者知道)。
            if asset_type == "future" and tf in ("日K", "周K", "月K"):
                try:
                    diag = taifex_daily.detect_session_regime(df)
                    if diag.get('regime') == 'mixed':
                        self.safe_after(0, self.log_message, f"【回測-盤別】{diag['note']}")
                        self._last_session_warning = diag['note']
                    else:
                        self._last_session_warning = ""
                except Exception:
                    self._last_session_warning = ""
            if df is None or len(df) < 30:
                self.safe_after(0, self.log_message, f"【回測】此期間歷史資料量不足 ({0 if df is None else len(df)} 根,分K券商多只保留近期),請縮短範圍或改用較長週期。")
                return
            # 費用/滑價:用該商品的 tick 當滑價單位,讓價檔數沿用策略設定 (更貼近實際成交)
            try:
                tick = tick_rules.get_tick(float(df['Close'].iloc[-1]), asset_type, str(s.get('symbol')).upper())
            except Exception:
                tick = None
            slip = int(s.get('slippage_ticks', 2) or 0)
            # 【ADR-075 看A做B 回測】訊號看 A、成交看 B。上面載好的 df 是策略自身
            # (B,執行商品);啟用看A做B 時,另外載入 A (訊號來源) 的歷史當 signal_df,
            # 把 B 當 exec_df 傳進去。tick/滑價一律用 B (實際成交商品)。
            signal_df, exec_df = df, None
            if strategy_engine.watch_enabled(s):
                w_contract, w_asset, w_sym, w_mkt = self._qt_resolve_watch(s)
                if w_contract is None:
                    self.safe_after(0, self.log_message, f"【回測】看A商品解析失敗: {strategy_engine.watch_symbol_of(s)}")
                    return
                w_tf = strategy_engine.watch_timeframe_of(s)
                a_df = self._qt_bt_load_df(w_contract, w_asset, w_sym, w_mkt, w_tf,
                                           start_dt, end_dt, session_basis=session_basis, tag="回測-看A")
                if a_df is None or len(a_df) < 30:
                    self.safe_after(0, self.log_message,
                                    f"【回測】看A ({w_sym}/{w_tf}) 歷史資料不足 ({0 if a_df is None else len(a_df)} 根),無法回測看A做B。")
                    return
                signal_df, exec_df = a_df, df
                self.safe_after(0, self.log_message,
                                f"【回測-看A做B】訊號看 {w_sym}/{w_tf} ({len(a_df)} 根),成交做 {s.get('symbol')}/{tf} ({len(df)} 根)。")
            # 【ADR-050】套用真實成本模型 (手續費 + 交易稅);fee_rate 已停用。
            result = backtest.run_backtest(s, signal_df, slippage_ticks=slip, tick_size=tick,
                                           cost_params=self._cost_params(), apply_cost_model=True,
                                           should_stop=lambda: getattr(self, '_backtest_cancel', False) or self._closing,
                                           exec_df=exec_df)
            if getattr(self, '_backtest_cancel', False):
                self.safe_after(0, self.log_message, "【回測】已依使用者要求中止,不產生報告。")
                return
            # 報告的 K 線圖用「成交商品 B」(df) 顯示,標點才落在實際交易的商品上。
            self.safe_after(0, self._qt_show_backtest_report, s, df, result)
            m = result['metrics']
            self.safe_after(0, self.log_message,
                            f"【回測】「{s.get('name')}」完成:{m['trades']} 筆交易,淨損益 {_fmt_amt_signed(m['total_pnl'])} "
                            f"(毛利 {_fmt_amt_signed(m.get('gross_pnl', 0))} − 成本 {_fmt_amt(m.get('total_cost', 0))}),"
                            f"勝率 {m['win_rate']:.1f}%,最大回撤 {_fmt_amt(m['max_drawdown'])},"
                            f"全程耗時 {time.time()-_t0:.1f} 秒。")
            # 【ADR-055】把參數代入情形同步寫進系統日誌 (報告視窗關掉後仍查得到)
            try:
                pu = custom_strategy.describe_param_usage(result.get('param_given'), result.get('param_usage'))
                if pu:
                    self.safe_after(0, self.log_message, f"【回測-參數】{pu}")
            except Exception:
                pass
        except Exception as e:
            # 【ADR-055】舊版只寫系統日誌,使用者的實際感受是「按了回測完全沒反應」
            # (報告視窗沒出現、日誌那行被其他訊息刷過去)。異常一律另外彈窗,
            # 讓失敗看得見、且帶著可回報的錯誤型別與訊息。
            detail = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc(limit=6)
            self.safe_after(0, self.log_message, f"【回測】執行異常: {detail}")
            self.safe_after(0, self.log_message, f"【回測-追蹤】{tb.replace(chr(10), ' | ')}")
            self.safe_after(0, lambda: messagebox.showerror(
                "回測失敗", f"策略「{s.get('name')}」回測時發生錯誤,已中止:\n\n{detail}\n\n"
                            "完整堆疊已寫入系統日誌。"))
        finally:
            self.safe_after(0, lambda: setattr(self, '_backtest_running', False))

    def _qt_show_audit(self, s, result, parent=None):
        """【ADR-057】顯示回測報告的獨立驗算結果 (使用者需求 #5)。"""
        try:
            checks = backtest.audit_result(result, s)
        except Exception as e:
            messagebox.showerror("驗算失敗", f"驗算過程發生錯誤:{type(e).__name__}: {e}", parent=parent or self)
            return
        win = tk.Toplevel(parent or self)
        win.title(f"🧮 回測報告驗算 — {s.get('name')}")
        win.configure(bg="#1A2026")
        self.center_window(win, 860, 540)
        passed = sum(1 for c in checks if c['ok'])
        total = len(checks)
        all_ok = (passed == total)
        tk.Label(win, text=("✅ 全部通過" if all_ok else f"⚠ 有 {total - passed} 項不一致") +
                           f" ({passed}/{total})",
                 bg="#1A2026", fg="#00E676" if all_ok else "#FF5252",
                 font=('微軟正黑體', 13, 'bold')).pack(anchor='w', padx=14, pady=(12, 2))
        tk.Label(win, text=("驗算方式:完全不看報告是怎麼算出來的,直接從「每筆交易明細」用最直白的算式\n"
                            "重算一次,再跟報告上的數字對帳。兩條獨立路徑得到同一個答案,才有理由相信報告。"),
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9), justify=tk.LEFT).pack(anchor='w', padx=14)

        frame = tk.Frame(win, bg="#12161A"); frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        cols = ('ok', 'name', 'detail')
        tv = ttk.Treeview(frame, columns=cols, show='headings', height=12, style="Optim.Treeview")
        for c, h, w in (('ok', '結果', 60), ('name', '檢查項目', 230), ('detail', '重算 vs 報告', 520)):
            tv.heading(c, text=h); tv.column(c, width=w, anchor='w' if c == 'detail' else 'center')
        for c in checks:
            tv.insert('', tk.END, values=("✅ 一致" if c['ok'] else "❌ 不符", c['name'], c['detail']),
                      tags=('ok' if c['ok'] else 'bad',))
        tv.tag_configure('ok', foreground='#000000', background='#FFFFFF')
        tv.tag_configure('bad', foreground='#B00020', background='#FFE9E9')
        sb = tk.Scrollbar(frame, orient=tk.VERTICAL, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y); tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(win, text=("※ 這些檢查能保證什麼:報告的彙總數字忠實反映了這些交易明細。\n"
                            "※ 不能保證什麼:(1) 訊號判定是否符合你的策略意圖 —— 那要看策略邏輯;\n"
                            "　 (2) 成本模型的費率是否與你的券商實際收費一致 (可在成本設定核對)。\n"
                            "　 換句話說,全部通過 ≠ 這個策略在真實市場一定會這樣成交。"),
                 bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 8), justify=tk.LEFT).pack(anchor='w', padx=14, pady=(0, 8))
        tk.Button(win, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=20, command=win.destroy).pack(pady=(0, 10))
        self.log_message(f"【回測-驗算】「{s.get('name')}」{passed}/{total} 項一致。"
                         + ("" if all_ok else " 有不一致項目,請開驗算視窗檢視。"))

    def _qt_show_backtest_report(self, s, df, result):
        """回測報告視窗:績效數字 + 資金曲線 + K線標點 + 每筆交易明細。"""
        m = result['metrics']
        dlg = tk.Toplevel(self)
        # 【ADR-059】使用者需求 #2:報告要看得到「這份報告是哪一段期間跑出來的」。
        # 以「實際餵進引擎的 df」的頭尾為準,而不是使用者輸入的起訖日 —— 因為
        # 實際涵蓋範圍會被資料源深度限制 (券商只給到某天、期交所只到昨天),
        # 顯示使用者輸入的日期會讓人以為真的跑了那麼久。
        try:
            _rng_from, _rng_to = df.index[0], df.index[-1]
            _rng_days = max((_rng_to - _rng_from).days, 0)
            _rng_txt = (f"{_rng_from:%Y-%m-%d} ~ {_rng_to:%Y-%m-%d}"
                        f"  ({_rng_days / 365.0:.1f} 年 / {len(df):,} 根 {s.get('timeframe')})")
        except Exception:
            _rng_txt = "(無法判定期間)"
        dlg.title(f"回測報告 — {s.get('name')} ({s.get('symbol')} {s.get('timeframe')})  |  {_rng_txt}")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 1160, 800)
        # 【ADR-057】不要用 transient:transient 視窗在 Windows 上不會有自己的
        # 最大化/最小化按鈕 (只會跟著母視窗),使用者要求的「最大化/縮小」
        # 就做不出來。改成獨立 Toplevel,自己提供工具列按鈕 + 系統標題列按鈕。
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass

        # --- 【ADR-057】視窗大小控制列 (使用者需求 #6) ---
        winbar = tk.Frame(dlg, bg="#1A2026"); winbar.pack(fill=tk.X, padx=10, pady=(6, 0))
        _win_state = {'max': False, 'geom': None}

        def _toggle_max():
            try:
                if not _win_state['max']:
                    _win_state['geom'] = dlg.geometry()
                    try:
                        dlg.state('zoomed')          # Windows:真正的最大化
                    except tk.TclError:
                        dlg.attributes('-zoomed', True)   # Linux/其他 WM 的等效寫法
                    _win_state['max'] = True
                    btn_max.config(text="🗗 還原視窗")
                else:
                    try:
                        dlg.state('normal')
                    except tk.TclError:
                        dlg.attributes('-zoomed', False)
                    if _win_state['geom']:
                        dlg.geometry(_win_state['geom'])
                    _win_state['max'] = False
                    btn_max.config(text="🗖 最大化")
            except Exception as e:
                self.log_message(f"【回測報告】切換視窗大小失敗: {e}")

        btn_max = tk.Button(winbar, text="🗖 最大化", bg="#2A323D", fg="#29B6F6", relief="flat",
                            font=('微軟正黑體', 9, 'bold'), padx=10, command=_toggle_max)
        btn_max.pack(side=tk.LEFT)
        tk.Button(winbar, text="🗕 縮到最小", bg="#2A323D", fg="#8A99AD", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=10,
                  command=lambda: dlg.iconify()).pack(side=tk.LEFT, padx=4)
        tk.Button(winbar, text="✖ 關閉報告", bg="#2A323D", fg="#FF8A80", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=10,
                  command=dlg.destroy).pack(side=tk.RIGHT)
        # 【ADR-057】使用者需求 #5:「要怎麼確認是對的回測報告?」
        # 提供一顆驗算按鈕,用完全獨立的路徑從交易明細重算一次再對帳。
        tk.Button(winbar, text="🧮 驗算這份報告", bg="#00ACC1", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=10,
                  command=lambda: self._qt_show_audit(s, result, dlg)).pack(side=tk.LEFT, padx=(14, 0))
        # 雙擊標題區也能切換最大化 (符合一般視窗操作直覺)
        try:
            dlg.bind("<Double-Button-1>", lambda e: None)
        except Exception:
            pass

        # --- 上方:績效數字 ---
        pf = "∞" if m['profit_factor'] == float('inf') else f"{m['profit_factor']:.2f}"
        summary = tk.Frame(dlg, bg="#12161A"); summary.pack(fill=tk.X, padx=10, pady=(10, 4))
        cells = [
            ("淨損益(扣成本)", f"{_fmt_amt_signed(m['total_pnl'])}", '#FF1744' if m['total_pnl'] > 0 else ('#00E676' if m['total_pnl'] < 0 else 'white')),
            ("報酬率", f"{m['total_return_pct']:+.2f}%", '#FF1744' if m['total_return_pct'] > 0 else ('#00E676' if m['total_return_pct'] < 0 else 'white')),
            ("交易次數", f"{m['trades']}", 'white'),
            ("勝率", f"{m['win_rate']:.1f}%", '#FFCA28'),
            ("獲利因子", pf, '#FFCA28'),
            ("最大回撤", f"{_fmt_amt(m['max_drawdown'])}", '#00E676'),
            ("平均持有", f"{int(m['avg_bars_held'])} 根", 'white'),
            ("勝/負", f"{m['wins']}/{m['losses']}", 'white'),
        ]
        for i, (lab, val, col) in enumerate(cells):
            cell = tk.Frame(summary, bg="#12161A"); cell.grid(row=0, column=i, padx=10, pady=6)
            tk.Label(cell, text=lab, bg="#12161A", fg="#8A99AD", font=('微軟正黑體', 9)).pack()
            tk.Label(cell, text=val, bg="#12161A", fg=col, font=('微軟正黑體', 13, 'bold')).pack()
        # 【ADR-044】第二排進階指標
        wl = m.get('win_loss_ratio', 0)
        wl_txt = "∞" if wl == float('inf') else f"{wl:.2f}"
        bh = m.get('buy_hold_pnl', 0.0)
        cells2 = [
            ("年化報酬(簡化)", f"{m.get('ann_return_pct', 0):+.1f}%", 'white'),
            ("夏普比率", f"{m.get('sharpe', 0):.2f}", '#FFCA28'),
            ("最大回撤%", f"{m.get('max_drawdown_pct', 0):.1f}%", '#00E676'),
            ("期望值/筆", f"{_fmt_amt_signed(m.get('expectancy', 0))}", 'white'),
            ("賺賠比", wl_txt, '#FFCA28'),
            ("最大連勝/連敗", f"{m.get('max_consec_wins', 0)}/{m.get('max_consec_losses', 0)}", 'white'),
            # 【ADR-057】連續勝敗的「金額」:筆數看不出痛不痛,金額才看得出撐不撐得住
            ("最大連續獲利(金額)", f"{_fmt_amt_signed(m.get('max_consec_win_amount', 0))}", '#FF1744'),
            ("最大連續虧損(金額)", f"{_fmt_amt_signed(m.get('max_consec_loss_amount', 0))}", '#00E676'),
            ("總成本(費+稅)", f"{_fmt_amt(m.get('total_cost', 0))}", '#FF8A80'),
            ("買進持有對照", f"{_fmt_amt_signed(bh)} ({m.get('buy_hold_pct', 0):+.1f}%)",
             '#FF1744' if bh > 0 else ('#00E676' if bh < 0 else 'white')),
            # 【ADR-059】使用者需求:要能跟 Buy & Hold 比較「總持有成本」。
            # 兩種意思都給,標籤寫清楚,免得混淆:
            #   建倉成本 = 要準備多少資金 (進場價×數量×單位規模)
            #   交易總成本 = 摩擦成本 (費+稅),Buy & Hold 的主要優勢來源
            ("建倉成本(首筆)", f"{_fmt_amt(m.get('cost_basis_first', 0))}", '#FFCA28'),
            ("建倉成本(最大單筆)", f"{_fmt_amt(m.get('cost_basis_max', 0))}", '#FFCA28'),
            ("每年交易次數", f"{m.get('trades_per_year', 0):.2f} 次", '#29B6F6'),
            ("每年交易成本", f"{_fmt_amt(m.get('cost_per_year', 0))}", '#FF8A80'),
        ]
        # 【ADR-050】第三排:成本明細與交易結構 (回答「成本到底吃掉多少」)
        cells3 = [
            ("毛損益(未扣成本)", f"{_fmt_amt_signed(m.get('gross_pnl', 0))}",
             '#FF1744' if m.get('gross_pnl', 0) > 0 else ('#00E676' if m.get('gross_pnl', 0) < 0 else 'white')),
            ("手續費", f"{_fmt_amt(m.get('total_fee', 0))}", '#FF8A80'),
            ("交易稅", f"{_fmt_amt(m.get('total_tax', 0))}", '#FF8A80'),
            ("成本佔毛利", f"{m.get('cost_ratio_pct', 0):.2f}%", '#FFCA28'),
            ("每筆平均成本", f"{_fmt_amt(m.get('avg_cost_per_trade', 0))}", 'white'),
            ("最佳/最差單筆", f"{_fmt_amt_signed(m.get('best_trade', 0))} / {_fmt_amt_signed(m.get('worst_trade', 0))}", 'white'),
            ("總獲利/總虧損", f"{_fmt_amt(m.get('gross_profit', 0))} / {_fmt_amt(m.get('gross_loss', 0))}", 'white'),
            ("多/空筆數", f"{m.get('long_trades', 0)}/{m.get('short_trades', 0)}", 'white'),
            ("持倉曝險", f"{m.get('exposure_pct', 0):.1f}%", 'white'),
        ]
        # 【ADR-053】第四排:多空分項 + 單筆極值 (反手策略必看)
        lp, sp = m.get('long_pnl', 0.0), m.get('short_pnl', 0.0)
        cells4 = [
            ("做多損益", f"{_fmt_amt_signed(lp)}", '#FF1744' if lp > 0 else ('#00E676' if lp < 0 else 'white')),
            ("做空損益", f"{_fmt_amt_signed(sp)}", '#FF1744' if sp > 0 else ('#00E676' if sp < 0 else 'white')),
            ("做多勝率", f"{m.get('long_win_rate', 0):.1f}%", '#FFCA28'),
            ("做空勝率", f"{m.get('short_win_rate', 0):.1f}%", '#FFCA28'),
            ("最大單筆獲利", f"{_fmt_amt_signed(m.get('max_single_win', 0))}", '#FF1744'),
            ("最大單筆虧損", f"{_fmt_amt_signed(m.get('max_single_loss', 0))}", '#00E676'),
            ("平均持有(全部)", f"{int(m.get('avg_bars_held_all', 0))} 根", 'white'),
        ]
        for i, (lab, val, col) in enumerate(cells2):
            cell = tk.Frame(summary, bg="#12161A"); cell.grid(row=1, column=i, padx=10, pady=(0, 6))
            tk.Label(cell, text=lab, bg="#12161A", fg="#8A99AD", font=('微軟正黑體', 8)).pack()
            tk.Label(cell, text=val, bg="#12161A", fg=col, font=('微軟正黑體', 11, 'bold')).pack()
        for i, (lab, val, col) in enumerate(cells3):
            cell = tk.Frame(summary, bg="#12161A"); cell.grid(row=2, column=i, padx=10, pady=(0, 6))
            tk.Label(cell, text=lab, bg="#12161A", fg="#8A99AD", font=('微軟正黑體', 8)).pack()
            tk.Label(cell, text=val, bg="#12161A", fg=col, font=('微軟正黑體', 11, 'bold')).pack()
        for i, (lab, val, col) in enumerate(cells4):
            cell = tk.Frame(summary, bg="#12161A"); cell.grid(row=3, column=i, padx=10, pady=(0, 6))
            tk.Label(cell, text=lab, bg="#12161A", fg="#8A99AD", font=('微軟正黑體', 8)).pack()
            tk.Label(cell, text=val, bg="#12161A", fg=col, font=('微軟正黑體', 11, 'bold')).pack()
        tk.Label(dlg, text=("※「最大回撤」是資金曲線從歷史高點往下的最大跌幅 (可能由連續多筆虧損累積而成),"
                            "不等於單筆最大虧損 —— 單筆極值請看「最大單筆獲利/虧損」。"),
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(anchor='w', padx=12)
        # 【ADR-059】期間資訊也顯示在報告內容裡 (標題列可能被視窗寬度截掉)
        tk.Label(dlg, text=f"※ 回測期間:{_rng_txt}",
                 bg="#1A2026", fg="#29B6F6", font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(4, 0))
        # 【ADR-059】Buy & Hold / 期末結算的誠實揭露
        if m.get('buy_and_hold_mode'):
            # 【ADR-061】累積買進的核心數字獨立一列 —— 這就是使用者要拿來比較的
            # 「總持有成本」:總共買了幾次、投入多少、平均成本多少、現在值多少。
            _unit = '口' if str(s.get('market')) == '台期貨' else ('股' if strategy_engine.trade_type_of(s) == '零股' else '張')
            tk.Label(dlg, text=(
                f"📌 累積買進彙總:買進 {m.get('bnh_buys', 0):,} 次 → 累計部位 {m.get('bnh_total_qty', 0):,} {_unit}"
                f"　|　加權平均成本 {m.get('bnh_avg_cost', 0):,.2f}　|　期末價 {m.get('bnh_final_price', 0):,.2f}\n"
                f"　　總持有成本(投入本金) {_fmt_amt(m.get('bnh_total_invested', 0))}"
                f"　|　含手續費稅後 {_fmt_amt(m.get('bnh_total_invested_with_cost', 0))}"
                f"　|　期末市值 {_fmt_amt(m.get('bnh_final_value', 0))}"),
                bg="#12161A", fg="#00E676", font=('微軟正黑體', 10, 'bold'),
                justify=tk.LEFT).pack(anchor='w', padx=12, pady=(6, 2))
            _bnh_note = ("※ 本策略為「買進後持有不賣 (Buy & Hold)」:每次進場條件成立就再買一次 (累積加碼),"
                         "永不賣出;期末以最後一根收盤價逐筆結算。交易明細每一列 = 一次買進。")
            if str(s.get('market')) == '台期貨':
                _bnh_note += ("\n　 ⚠ 期貨的長期持有實際上必須每月換月 (R1 連續合約的價格已接續,"
                              "但換月的手續費與價差成本本回測「未」計入),真實成本會比這裡高。")
            else:
                _bnh_note += ("\n　 ⚠ 本回測使用未還原權息的價格,長期持有的股利收益「未」計入,"
                              "真實報酬會比這裡高。")
            tk.Label(dlg, text=_bnh_note, bg="#1A2026", fg="#FFCA28",
                     font=('微軟正黑體', 8), justify=tk.LEFT).pack(anchor='w', padx=12)
        elif m.get('settled_open_at_end'):
            tk.Label(dlg, text=("※ 回測結束時仍有未平倉部位,已用最後一根收盤價結算為一筆交易 "
                                "(出場原因標示「回測期末結算」);若不結算,這段未實現損益會完全不出現在報告上。"),
                     bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 8), justify=tk.LEFT).pack(anchor='w', padx=12)
        tk.Label(dlg, text=f"※ 成本計算:{m.get('cost_desc', '')};滑價 {s.get('slippage_ticks', 0)} 檔已計入成交價。",
                 bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 8)).pack(anchor='w', padx=12)
        # 【ADR-055】參數代入實證:直接顯示「策略程式碼實際讀到的參數與來源」,
        # 使用者不必靠猜判斷參數視窗有沒有生效 (拼錯 key 會被標成 ⚠)。
        try:
            pu = custom_strategy.describe_param_usage(result.get('param_given'), result.get('param_usage'))
            if pu:
                has_warn = '⚠' in pu
                tk.Label(dlg, text=f"※ 參數實際代入:{pu}", bg="#1A2026",
                         fg="#FF5252" if has_warn else "#00E676",
                         font=('微軟正黑體', 8), justify=tk.LEFT, wraplength=1120).pack(anchor='w', padx=12)
        except Exception:
            pass
        tk.Label(dlg, text="※ 回測採「委託視同成交、僅收盤評估」與實盤同一套邏輯;僅供參考,不代表未來績效。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(anchor='w', padx=12)

        # --- 中間:K線+標點 與 資金曲線 (雙圖) ---
        mid = tk.Frame(dlg, bg="#1A2026"); mid.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        try:
            fig = plt.Figure(figsize=(10.2, 3.4), facecolor="#12161A")
            axp = fig.add_subplot(121); axe = fig.add_subplot(122)
            xs = list(range(len(df)))
            axp.plot(xs, df['Close'].values, color="#B0BEC5", linewidth=0.8, zorder=1)
            kind_style = {'buy_open': ('^', '#FF1744'), 'sell_open': ('v', '#00E676'),
                          'buy_close': ('^', '#FF8A80'), 'sell_close': ('v', '#69F0AE')}
            ts_to_x = {ts: i for i, ts in enumerate(df.index)}
            for mk in result['markers']:
                x = ts_to_x.get(mk['ts'])
                if x is None:
                    continue
                mkr, col = kind_style.get(mk['kind'], ('o', 'white'))
                axp.scatter([x], [mk['price']], marker=mkr, color=col, s=42, zorder=3, edgecolors='black', linewidths=0.4)
            axp.set_title("K線與進出場點", color="white", fontsize=9)
            axp.set_facecolor("#12161A")
            # 資金曲線
            if result['equity']:
                ex = list(range(len(result['equity'])))
                ey = [e for _, e in result['equity']]
                axe.plot(ex, ey, color="#29B6F6", linewidth=1.0)
                axe.axhline(y=0, color="#5A6472", linewidth=0.6, linestyle='--')
                axe.fill_between(ex, ey, 0, where=[v >= 0 for v in ey], color="#FF1744", alpha=0.15)
                axe.fill_between(ex, ey, 0, where=[v < 0 for v in ey], color="#00E676", alpha=0.15)
            axe.set_title("資金曲線 (累積損益)", color="white", fontsize=9)
            axe.set_facecolor("#12161A")
            for ax in (axp, axe):
                ax.tick_params(colors="#8A99AD", labelsize=7)
                for spine in ax.spines.values():
                    spine.set_color("#2A323D")
            fig.tight_layout()
            canvas = FigureCanvasTkAgg(fig, master=mid)
            canvas.draw()
            canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        except Exception as e:
            tk.Label(mid, text=f"(圖表繪製失敗: {e})", bg="#1A2026", fg="#FF5252").pack()

        # --- 下方:每筆交易明細 ---
        tk.Label(dlg, text="每筆交易明細:", bg="#1A2026", fg="#FFCA28",
                 font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(4, 0))
        tv_frame = tk.Frame(dlg, bg="#1A2026"); tv_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(2, 8))
        cols = ("no", "dir", "entry_t", "entry_p", "exit_t", "exit_p", "pnl", "pnl_pct", "bars", "reason")
        heads = {"no": "#", "dir": "方向", "entry_t": "進場時間", "entry_p": "進場價",
                 "exit_t": "出場時間", "exit_p": "出場價", "pnl": "損益", "pnl_pct": "報酬%",
                 "bars": "持有K棒", "reason": "出場原因"}
        widths = {"no": 36, "dir": 46, "entry_t": 130, "entry_p": 70, "exit_t": 130,
                  "exit_p": 70, "pnl": 80, "pnl_pct": 64, "bars": 64, "reason": 220}
        tvt = ttk.Treeview(tv_frame, columns=cols, show="headings", style='Trades.Treeview', height=6)
        for c in cols:
            tvt.heading(c, text=heads[c]); tvt.column(c, width=widths[c], anchor="center")
        tvt.tag_configure('t_win', foreground='#FF1744', background='#12161A')
        tvt.tag_configure('t_loss', foreground='#00E676', background='#12161A')
        vsb = ttk.Scrollbar(tv_frame, orient="vertical", command=tvt.yview)
        tvt.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tvt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _fmt_ts(ts):
            try:
                s = ts.strftime('%Y-%m-%d %H:%M')
                return s.replace(' 00:00', '')
            except Exception:
                return str(ts)
        for i, t in enumerate(result['trades'], 1):
            tag = 't_win' if t['pnl'] > 0 else ('t_loss' if t['pnl'] < 0 else '')
            exit_ts_str = _fmt_ts(t['exit_ts'])
            if '買進持有' in t.get('exit_reason', '') or '未賣出' in t.get('exit_reason', ''):
                exit_ts_str = "--"
            tvt.insert("", tk.END, values=(
                i, t['direction'], _fmt_ts(t['entry_ts']), f"{t['entry_price']:g}",
                exit_ts_str, f"{t['exit_price']:g}", f"{_fmt_amt_signed(t['pnl'])}",
                f"{t['pnl_pct']:+.2f}%", t['bars_held'], t['exit_reason']), tags=(tag,))
        tk.Button(dlg, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=20, pady=3, command=dlg.destroy).pack(pady=(0, 8))

    def _qt_open_paper_window(self):
        """【ADR-041/新ADR多帳戶】模擬帳戶視窗:帳戶選單/資金/權益/持倉/交易史/重置。
        新增/刪除帳戶都在這個視窗做,策略編輯器只負責「選哪個帳戶」。"""
        try:
            if getattr(self, '_paper_win', None) and self._paper_win.winfo_exists():
                self._paper_win.deiconify(); self._paper_win.lift(); self._paper_win.focus_force()
                return
        except Exception:
            pass
        # 記住上次選的帳戶 (視窗重開時延續);若已不存在就退回第一個。
        if getattr(self, '_paper_win_account_id', None) not in self.paper_accts:
            self._paper_win_account_id = next(iter(self.paper_accts.keys()))
        dlg = tk.Toplevel(self)
        self._paper_win = dlg
        dlg.title("💰 模擬帳戶 (虛擬資金,僅供策略驗證)")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 940, 600)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass

        acct_row = tk.Frame(dlg, bg="#1A2026"); acct_row.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(acct_row, text="帳戶", bg="#1A2026", fg="white", font=('微軟正黑體', 9, 'bold')).pack(side=tk.LEFT)
        cb_acct = ttk.Combobox(acct_row, width=24, state='readonly', style="BlackText.TCombobox")
        cb_acct.pack(side=tk.LEFT, padx=6)

        def _acct_values():
            return self._qt_account_choices()

        def _refresh_acct_combo(select_id=None):
            choices = _acct_values()
            cb_acct['values'] = [name for _aid, name in choices]
            ids = [aid for aid, _name in choices]
            target = select_id or self._paper_win_account_id
            if target not in ids and ids:
                target = ids[0]
            self._paper_win_account_id = target
            if target in ids:
                cb_acct.current(ids.index(target))

        def _on_acct_selected(*_a):
            choices = _acct_values()
            idx = cb_acct.current()
            if 0 <= idx < len(choices):
                self._paper_win_account_id = choices[idx][0]
                self._qt_refresh_paper_account()
                _render_history()
        cb_acct.bind('<<ComboboxSelected>>', _on_acct_selected)

        def _add_account():
            name = sd.askstring("新增模擬帳戶", "帳戶名稱:", parent=dlg)
            if not name or not name.strip():
                return
            cash_str = sd.askstring("新增模擬帳戶", "初始資金:", initialvalue="1000000", parent=dlg)
            try:
                cash = float(cash_str) if cash_str else 0
                if cash <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self.log_message("【模擬帳戶】初始資金格式錯誤,未新增帳戶。")
                return
            acct = paper_account.new_account(cash, name=name.strip())
            self.paper_accts[acct['id']] = acct
            self._qt_save_paper()
            self.log_message(f"【模擬帳戶】已新增帳戶「{acct['name']}」(初始資金 {_fmt_amt(cash)})。")
            _refresh_acct_combo(select_id=acct['id'])
            self._qt_refresh_paper_account()
            _render_history()

        def _delete_account():
            if len(self.paper_accts) <= 1:
                messagebox.showwarning("無法刪除", "至少要保留一個模擬帳戶。", parent=dlg)
                return
            aid = self._paper_win_account_id
            acct = self.paper_accts.get(aid)
            if not acct:
                return
            using = [s.get('name', '?') for s in self.strategies
                    if strategy_engine.account_id_of(s) == aid]
            warn = (f"還有 {len(using)} 個策略 ({'、'.join(using)}) 設定用這個帳戶,"
                    f"刪除後它們會在下次成交時自動改記到其他帳戶,請自行到策略編輯器"
                    f"重新指定。\n\n" if using else "")
            if not messagebox.askyesno("確認刪除", f"{warn}確定要刪除帳戶「{acct.get('name')}」嗎?"
                                       f"(裡面的模擬交易紀錄會一併消失,無法復原)", parent=dlg):
                return
            del self.paper_accts[aid]
            self._qt_save_paper()
            self.log_message(f"【模擬帳戶】已刪除帳戶「{acct.get('name')}」。")
            _refresh_acct_combo()
            self._qt_refresh_paper_account()
            _render_history()

        def _rename_account():
            aid = self._paper_win_account_id
            acct = self.paper_accts.get(aid)
            if not acct:
                return
            name = sd.askstring("重新命名帳戶", "新名稱:", initialvalue=acct.get('name', ''), parent=dlg)
            if not name or not name.strip():
                return
            acct['name'] = name.strip()
            self._qt_save_paper()
            self.log_message(f"【模擬帳戶】帳戶已重新命名為「{acct['name']}」。")
            _refresh_acct_combo(select_id=aid)
            self._qt_refresh_paper_account(); _render_history()

        def _move_account(delta):
            """【新增】帳戶順序可自行排列;self.paper_accts 是 dict,順序即
            插入順序,重排就是照新順序重建一份字典 (Python 3.7+ dict 保序)。"""
            ids = list(self.paper_accts.keys())
            aid = self._paper_win_account_id
            if aid not in ids:
                return
            idx = ids.index(aid)
            new_idx = idx + delta
            if new_idx < 0 or new_idx >= len(ids):
                return
            ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
            self.paper_accts = {i: self.paper_accts[i] for i in ids}
            self._qt_save_paper()
            self.log_message("【模擬帳戶】已調整帳戶順序。")
            _refresh_acct_combo(select_id=aid)
            self._qt_refresh_tree()  # 策略清單的「模擬帳戶」欄選項順序也一併更新

        tk.Button(acct_row, text="➕ 新增帳戶", bg="#00C853", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=8, pady=1, command=_add_account).pack(side=tk.LEFT, padx=4)
        tk.Button(acct_row, text="🗑 刪除此帳戶", bg="#FF5252", fg="white", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=8, pady=1, command=_delete_account).pack(side=tk.LEFT, padx=4)
        tk.Button(acct_row, text="✏️ 重新命名", bg="#FB8C00", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=8, pady=1, command=_rename_account).pack(side=tk.LEFT, padx=4)
        tk.Button(acct_row, text="⬆ 上移", bg="#546E7A", fg="white", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=8, pady=1, command=lambda: _move_account(-1)).pack(side=tk.LEFT, padx=4)
        tk.Button(acct_row, text="⬇ 下移", bg="#546E7A", fg="white", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=8, pady=1, command=lambda: _move_account(1)).pack(side=tk.LEFT, padx=4)
        _refresh_acct_combo()

        head = tk.Frame(dlg, bg="#12161A"); head.pack(fill=tk.X, padx=10, pady=(10, 4))
        # 實際數值由下面的 self._qt_refresh_paper_account() 依目前選取的帳戶填入,
        # 這裡只建立骨架 (支援稍後切換帳戶時原地更新,不用整個視窗重建)。
        cells = [("初始資金", '', 'white', 'init'), ("現金", '', 'white', 'cash'),
                 ("權益數", '', '#FFCA28', 'eq'), ("已實現損益", '', 'white', 'real'),
                 ("未實現損益", '', 'white', 'unreal'), ("報酬率", '', 'white', 'ret')]
        self._paper_ui = {}
        for i, (lab, val, col, key) in enumerate(cells):
            cell = tk.Frame(head, bg="#12161A"); cell.grid(row=0, column=i, padx=12, pady=6)
            tk.Label(cell, text=lab, bg="#12161A", fg="#8A99AD", font=('微軟正黑體', 9)).pack()
            lbl = tk.Label(cell, text=val, bg="#12161A", fg=col, font=('微軟正黑體', 13, 'bold'))
            lbl.pack()
            self._paper_ui[key] = lbl
        tk.Label(dlg, text="※ 台股含手續費0.1425%與證交稅0.3%;期貨每口單邊估50元、以契約乘數計損益(TXF=200/MXF=50/TMF=10);未實現以最後成交標記價計。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(anchor='w', padx=12)
        tk.Label(dlg, text="目前持倉:", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(6, 0))
        pos_cols = ("sym", "name", "dir", "qty", "avg", "mark", "unreal")
        pos_heads = {"sym": "商品代號", "name": "商品名稱", "dir": "方向", "qty": "數量", "avg": "均價", "mark": "標記價", "unreal": "未實現"}
        widths_pos = {"sym": 80, "name": 90, "dir": 50, "qty": 50, "avg": 80, "mark": 80, "unreal": 90}
        tvp = ttk.Treeview(dlg, columns=pos_cols, show="headings", style='Trades.Treeview', height=4)
        for c in pos_cols:
            tvp.heading(c, text=pos_heads[c]); tvp.column(c, width=widths_pos[c], anchor="center")
        tvp.tag_configure('p_win', foreground='#FF1744', background='#12161A')
        tvp.tag_configure('p_loss', foreground='#00E676', background='#12161A')
        tvp.tag_configure('p_flat', foreground='white', background='#12161A')
        tvp.pack(fill=tk.X, padx=10, pady=2)
        self._paper_ui['tvp'] = tvp
        self._qt_refresh_paper_account()
        tk.Label(dlg, text="交易紀錄 (最新在上):", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(6, 0))
        h_cols = ("ts", "sym", "name", "dir", "act", "kind", "qty", "price", "fee", "pnl")
        h_heads = {"ts": "時間", "sym": "商品代號", "name": "商品名稱", "dir": "方向", "act": "買賣",
                   "kind": "開/平", "qty": "數量", "price": "價格", "fee": "費用", "pnl": "已實現"}
        tvh = ttk.Treeview(dlg, columns=h_cols, show="headings", style='Trades.Treeview', height=7)
        widths = {"ts": 150, "sym": 80, "name": 90, "dir": 50, "act": 50, "kind": 50,
                  "qty": 50, "price": 80, "fee": 70, "pnl": 90}
        for c in h_cols:
            tvh.heading(c, text=h_heads[c]); tvh.column(c, width=widths[c], anchor="center")
        tvh.tag_configure('p_win', foreground='#FF1744', background='#12161A')
        tvh.tag_configure('p_loss', foreground='#00E676', background='#12161A')
        tvh.tag_configure('p_flat', foreground='white', background='#12161A')
        vsb = ttk.Scrollbar(dlg, orient="vertical", command=tvh.yview)
        tvh.configure(yscrollcommand=vsb.set)
        tvh.pack(fill=tk.BOTH, expand=True, padx=10, pady=2)
        # 【使用者需求】商品代號旁補上商品名稱 + 方向 (做多/做空)。方向不是新的
        # 資料欄位,是「開倉買進/平倉賣出=做多」「開倉賣出/平倉買進=做空」直接從
        # 既有 action+kind 推導,history 紀錄本身不用改。名稱查詢結果就地快取,
        # 同一檔在 200 筆紀錄裡重複出現時不用重複解析。
        _name_cache = {}
        def _sym_name(sym):
            if sym not in _name_cache:
                _name_cache[sym] = self._wl_display_name(sym)
            return _name_cache[sym]

        def _render_history():
            """【新ADR 多帳戶】重繪交易紀錄表,對象是視窗目前選取的帳戶。"""
            for iid in tvh.get_children():
                tvh.delete(iid)
            acct = self.paper_accts.get(self._paper_win_account_id)
            if not acct:
                return
            for rec in reversed(acct['history'][-200:]):
                kind_txt = '開倉' if rec['kind'] == 'OPEN' else '平倉'
                is_long = (rec['kind'] == 'OPEN' and rec['action'] == '買進') or \
                          (rec['kind'] == 'CLOSE' and rec['action'] == '賣出')
                dir_txt = '做多' if is_long else '做空'
                tag = 'p_win' if rec['pnl'] > 0 else ('p_loss' if rec['pnl'] < 0 else 'p_flat')
                tvh.insert("", tk.END, values=(rec['ts'], rec['symbol'], _sym_name(rec['symbol']), dir_txt,
                                                rec['action'], kind_txt, rec['qty'], f"{rec['price']:g}",
                                                f"{_fmt_amt(rec['fee'])}",
                                                f"{_fmt_amt_signed(rec['pnl'])}" if rec['kind'] == 'CLOSE' else '--'), tags=(tag,))
        _render_history()
        foot = tk.Frame(dlg, bg="#1A2026"); foot.pack(pady=8)
        def _reset():
            aid = self._paper_win_account_id
            old = self.paper_accts.get(aid)
            if not old:
                return
            try:
                cash_str = sd.askstring("重置模擬帳戶", f"輸入「{old.get('name')}」的新初始資金 (清空持倉與歷史):", parent=dlg)
                if not cash_str:
                    return
                cash = float(cash_str)
                if cash <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self.log_message("【模擬帳戶】初始資金格式錯誤,未重置。")
                return
            # 保留原本的 id/name,只重置資金/持倉/歷史,不影響策略對這個帳戶的
            # account_id 設定 (策略不用因為重置就重新選帳戶)。
            self.paper_accts[aid] = paper_account.new_account(cash, name=old.get('name'), account_id=aid)
            self._qt_save_paper()
            self.log_message(f"【模擬帳戶】「{old.get('name')}」已重置,初始資金 {_fmt_amt(cash)}。")
            self._qt_refresh_paper_account(); _render_history()
        tk.Button(foot, text="🔄 重置帳戶", bg="#5A6472", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=14, pady=3, command=_reset).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=20, pady=3, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _qt_open_all_accounts_window(self):
        """【新增】全部帳戶總覽:「💰 模擬帳戶」視窗一次只能看一個帳戶,切換
        帳戶才看得到另一個的數字;這裡一次列出所有帳戶的資金/損益,以及
        跨帳戶合併的完整持倉清單,方便一眼盤點「現在所有模擬倉位長怎樣」。"""
        try:
            if getattr(self, '_all_accts_win', None) and self._all_accts_win.winfo_exists():
                self._all_accts_win.deiconify(); self._all_accts_win.lift(); self._all_accts_win.focus_force()
                self._qt_refresh_all_accounts_window()
                return
        except Exception:
            pass
        dlg = tk.Toplevel(self)
        self._all_accts_win = dlg
        dlg.title("📊 全部模擬帳戶總覽")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 1000, 620)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass

        tk.Label(dlg, text="帳戶總覽:", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(10, 0))
        acct_cols = ("name", "init", "cash", "eq", "real", "unreal", "ret", "npos")
        acct_heads = {"name": "帳戶名稱", "init": "初始資金", "cash": "現金", "eq": "權益數",
                      "real": "已實現損益", "unreal": "未實現損益", "ret": "報酬率", "npos": "持倉檔數"}
        widths_a = {"name": 120, "init": 100, "cash": 100, "eq": 100, "real": 100, "unreal": 100, "ret": 80, "npos": 70}
        tva = ttk.Treeview(dlg, columns=acct_cols, show="headings", style='Trades.Treeview', height=6)
        for c in acct_cols:
            tva.heading(c, text=acct_heads[c]); tva.column(c, width=widths_a[c], anchor="center")
        tva.tag_configure('p_win', foreground='#FF1744', background='#12161A')
        tva.tag_configure('p_loss', foreground='#00E676', background='#12161A')
        tva.tag_configure('p_flat', foreground='white', background='#12161A')
        tva.pack(fill=tk.X, padx=10, pady=4)
        self._all_accts_ui = {'tva': tva}

        tk.Label(dlg, text="全部持倉 (跨帳戶合併):", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(8, 0))
        pos_cols = ("acct", "sym", "name", "dir", "qty", "avg", "mark", "unreal")
        pos_heads = {"acct": "帳戶", "sym": "商品代號", "name": "商品名稱", "dir": "方向", "qty": "數量",
                     "avg": "均價", "mark": "標記價", "unreal": "未實現"}
        widths_p = {"acct": 100, "sym": 80, "name": 90, "dir": 50, "qty": 60, "avg": 80, "mark": 80, "unreal": 90}
        tvp2 = ttk.Treeview(dlg, columns=pos_cols, show="headings", style='Trades.Treeview', height=14)
        for c in pos_cols:
            tvp2.heading(c, text=pos_heads[c]); tvp2.column(c, width=widths_p[c], anchor="center")
        tvp2.tag_configure('p_win', foreground='#FF1744', background='#12161A')
        tvp2.tag_configure('p_loss', foreground='#00E676', background='#12161A')
        tvp2.tag_configure('p_flat', foreground='white', background='#12161A')
        vsb2 = ttk.Scrollbar(dlg, orient="vertical", command=tvp2.yview)
        tvp2.configure(yscrollcommand=vsb2.set)
        tvp2.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self._all_accts_ui['tvp'] = tvp2

        foot = tk.Frame(dlg, bg="#1A2026"); foot.pack(pady=8)
        tk.Button(foot, text="🔄 重新整理", bg="#5A6472", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=14, pady=3, command=self._qt_refresh_all_accounts_window).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=20, pady=3, command=dlg.destroy).pack(side=tk.LEFT, padx=6)
        self._qt_refresh_all_accounts_window()

    def _qt_refresh_all_accounts_window(self):
        """更新「全部帳戶總覽」視窗的兩個表格;沒開著這個視窗時直接跳過。"""
        try:
            if not getattr(self, '_all_accts_win', None) or not self._all_accts_win.winfo_exists():
                return
            ui = self._all_accts_ui
            tva = ui['tva']
            keep_a = tva.focus() or ((tva.selection() or [None])[0])
            for iid in tva.get_children():
                tva.delete(iid)
            for aid, a in self.paper_accts.items():
                eq = paper_account.equity(a)
                unreal = paper_account.unrealized_pnl(a)
                ret_pct = (eq - a['initial_cash']) / a['initial_cash'] * 100.0 if a['initial_cash'] else 0.0
                total_pnl = a['realized_pnl'] + unreal
                tag = 'p_win' if total_pnl > 0 else ('p_loss' if total_pnl < 0 else 'p_flat')
                tva.insert("", tk.END, iid=aid, values=(a.get('name', aid), _fmt_amt(a['initial_cash']),
                            _fmt_amt(a['cash']), _fmt_amt(eq), _fmt_amt_signed(a['realized_pnl']),
                            _fmt_amt_signed(unreal), f"{ret_pct:+.2f}%", len(a['positions'])), tags=(tag,))
            if keep_a and tva.exists(keep_a):
                tva.selection_set(keep_a); tva.focus(keep_a)

            tvp = ui['tvp']
            keep_p = tvp.focus() or ((tvp.selection() or [None])[0])
            for iid in tvp.get_children():
                tvp.delete(iid)
            for aid, a in self.paper_accts.items():
                for sym, p in a['positions'].items():
                    d = 1.0 if p['direction'] == '多' else -1.0
                    diff = (float(p.get('mark_price', p['avg_price'])) - p['avg_price']) * d
                    if p['market'] == '台股':
                        u = diff * 1000 * p['qty']
                    else:
                        mult, _ = paper_account._fut_multiplier(sym)
                        u = diff * mult * p['qty']
                    tag = 'p_win' if u > 0 else ('p_loss' if u < 0 else 'p_flat')
                    name = self._wl_display_name(sym)
                    iid = f"{aid}|{sym}"
                    tvp.insert("", tk.END, iid=iid, values=(a.get('name', aid), sym, name, p['direction'], p['qty'],
                                f"{p['avg_price']:g}", f"{p.get('mark_price', p['avg_price']):g}",
                                f"{_fmt_amt_signed(u)}"), tags=(tag,))
            if keep_p and tvp.exists(keep_p):
                tvp.selection_set(keep_p); tvp.focus(keep_p)
        except Exception:
            pass

    # ================= 【ADR-041】主圖活K棒 (tick 驅動,零全圖重繪) =================
    def _live_bar_on_tick(self, price):
        """
        tick callback 執行緒呼叫:把最新成交價累積進「形成中的K棒」。
        只累積狀態 (dict 操作,GIL 原子),不碰 UI;上屏由畫家 (_live_bar_painter)
        在主執行緒用 blitting 完成——這是 MultiCharts/XQ 等專業軟體「即時但
        不停頓」的做法:本地端用 tick 堆活K棒,而不是反覆重新下載整張圖。
        """
        tf = getattr(self, 'current_timeframe', None)
        mins = self.AUTO_REFRESH_TFS.get(tf or '')
        if not mins or price <= 0:
            return
        now = datetime.now()
        boundary = now.replace(second=0, microsecond=0)
        boundary -= timedelta(minutes=boundary.minute % mins)
        lb = self._live_bar
        if lb is None or lb.get('start') != boundary:
            self._live_bar = {'start': boundary, 'o': price, 'h': price, 'l': price, 'c': price, 'dirty': True}
        else:
            lb['h'] = max(lb['h'], price)
            lb['l'] = min(lb['l'], price)
            lb['c'] = price
            lb['dirty'] = True

    # ==================================================================
    # 【ADR-102 / ADR-120】主圖【盤勢判斷】
    #
    # 兩個子項共用主圖上同一顆勾選鈕:
    #   ① 量價支撐壓力 (Volume Profile,ADR-102) —— 畫水平線
    #   ② 盤勢/型態偵測 (ADR-120,原本在終極波段策略裡) —— 寫日誌通知
    # 計算分別在 core/volume_profile.py 與 core/market_pattern.py
    # (純邏輯、可離線測試);設定正規化與通知去重在 core/regime_panel.py。
    # 這裡只負責「決定用哪一段資料」「畫到主圖上」「把通知寫進日誌」。
    # ==================================================================
    SR_RANGE_VISIBLE = volume_profile.RANGE_VISIBLE
    SR_RANGE_FIXED = volume_profile.RANGE_FIXED
    SR_RANGE_MODES = volume_profile.RANGE_MODES

    def _sr_source_df(self, raw_df):
        """依目前模式決定要拿哪一段資料算支撐壓力。

        可見範圍:使用者放大/平移到哪就算哪一段,最符合看盤習慣。
        固定N根:不管畫面怎麼縮放都用最近 N 根,結果較穩定。
        """
        if raw_df is None or len(raw_df) == 0:
            return None
        mode = self.regime_settings.get('sr_range_mode', self.SR_RANGE_VISIBLE)
        n = int(self.regime_settings.get('sr_fixed_bars', 120) or 120)
        if mode == self.SR_RANGE_FIXED:
            return raw_df.tail(max(20, n))
        # 可見範圍:draw_chart 畫的是 self.plot_df (已依縮放裁切);取不到就退回全量
        vis = getattr(self, 'plot_df', None)
        if vis is not None and len(vis) >= 20:
            return vis
        return raw_df.tail(max(20, n))

    def _sr_active(self):
        """支撐壓力現在該不該畫:【盤勢判斷】總開關 + 支撐壓力子項都要開。"""
        if not getattr(self, 'regime_enabled_var', None) or not self.regime_enabled_var.get():
            return False
        if not getattr(self, 'sr_enabled_var', None) or not self.sr_enabled_var.get():
            return False
        return True

    def _draw_sr_levels(self, ax, raw_df):
        """在主圖畫支撐壓力水平線。任何例外都不可影響 K 線圖本身。"""
        self._sr_last_result = None
        if not self._sr_active():
            return
        try:
            src = self._sr_source_df(raw_df)
            if src is None or len(src) < 20:
                return
            result = volume_profile.find_levels(
                src, asset_type=self.asset_type,
                raw_symbol=str(self.current_symbol or '').upper(),
                max_levels=int(self.regime_settings.get('sr_max_levels', 6) or 6))
            if not result or not result['levels']:
                return
            self._sr_last_result = result
            for lv in result['levels']:
                # 【鐵則1】紅漲綠跌的延伸:壓力在上方用紅、支撐在下方用綠,
                # 與台股慣例一致 (紅=多方要突破的價、綠=空方要跌破的價)。
                if lv['role'] == volume_profile.ROLE_RESISTANCE:
                    color = '#FF1744'
                elif lv['role'] == volume_profile.ROLE_SUPPORT:
                    color = '#00E676'
                else:
                    color = '#FFCA28'
                # 強度決定粗細與透明度,一眼看得出哪幾條比較關鍵
                lw = 0.7 + 1.3 * float(lv['strength'])
                alpha = 0.35 + 0.45 * float(lv['strength'])
                ax.axhline(y=lv['price'], color=color, linestyle='--',
                           linewidth=lw, alpha=alpha, zorder=8)
                ax.text(0.995, lv['price'], f" {lv['price_str']} ",
                        transform=ax.get_yaxis_transform(), ha='right', va='center',
                        color=color, fontsize=7, alpha=min(1.0, alpha + 0.25),
                        bbox=dict(boxstyle='round,pad=0.15', fc='#12161A',
                                  ec=color, lw=0.5, alpha=0.75), zorder=9)
            for z in result.get('lvn_zones', []):
                # 真空帶語意不同 (價格容易快速穿越,不是支撐壓力),用淡灰點線區隔
                ax.axhline(y=z['price'], color='#8A99AD', linestyle=':',
                           linewidth=0.6, alpha=0.30, zorder=7)
        except Exception as e:
            # 支撐壓力只是輔助資訊,絕不能因為它出錯就讓整張 K 線圖畫不出來
            self.log_message(f"【支撐壓力】計算失敗 ({type(e).__name__}: {e}),本次略過繪製。")

    def _fib_toggle(self):
        """【ADR-133】黃金切割開關:存設定 + 重畫 + 勾起來時把點位寫進日誌。"""
        try:
            self._save_indicator_settings()
        except Exception:
            pass
        self.trigger_redraw()
        if self.fib_show.get():
            self.safe_after(300, self._fib_log_summary)

    def _draw_fib_levels(self, ax, raw_df):
        """在主圖畫黃金切割律的水平線。任何例外都不可影響 K 線圖本身
        (跟支撐壓力同樣的理由 —— 輔助資訊不可以害主功能掛掉)。"""
        self._fib_last_result = None
        if not self.fib_show.get():
            return
        try:
            src = self._sr_source_df(raw_df)
            if src is None or len(src) < 20:
                return
            result = fibonacci.compute(src, self.fib_lookback.get(), self.fib_ratios)
            if not result or not result['levels']:
                return
            self._fib_last_result = result
            base = palette.resolve(self.fib_color.get(), "#FFCA28")
            for lv in result['levels']:
                if lv['kind'] == 'endpoint':
                    lw, alpha, ls = 1.1, 0.75, '-'
                elif lv['key']:
                    # 0.382 / 0.618 是實務上最常看的兩條,畫粗一點一眼認得出來
                    lw, alpha, ls = 1.4, 0.85, '--'
                elif lv['kind'] == 'extend':
                    lw, alpha, ls = 0.7, 0.40, ':'
                else:
                    lw, alpha, ls = 0.8, 0.55, '--'
                ax.axhline(y=lv['price'], color=base, linestyle=ls,
                           linewidth=lw, alpha=alpha, zorder=6)
                ax.text(0.005, lv['price'], f" {lv['label']} {lv['price']:.2f} ",
                        transform=ax.get_yaxis_transform(), ha='left', va='center',
                        color=base, fontsize=7, alpha=min(1.0, alpha + 0.15),
                        bbox=dict(boxstyle='round,pad=0.15', fc='#12161A',
                                  ec=base, lw=0.5, alpha=0.70), zorder=7)
        except Exception as e:
            self.log_message(f"【黃金切割】計算失敗 ({type(e).__name__}: {e}),本次略過繪製。")

    def _fib_log_summary(self):
        """把這次算出的切割點位寫進系統日誌,方便拿數字去掛單或設停損。"""
        r = getattr(self, '_fib_last_result', None)
        if not r:
            return
        self.log_message(f"【黃金切割】{fibonacci.summarize(r)}")
        for lv in r['levels']:
            mark = '★' if lv['key'] else ' '
            self.log_message(f"　　{mark} {lv['label']}　{lv['price']:.2f}"
                             f"　({ {'endpoint': '端點', 'retrace': '回撤', 'extend': '延伸'}[lv['kind']] })")

    def _sr_log_summary(self):
        """把這次算出的點位摘要寫進系統日誌,方便拿數字去掛單或設停損。"""
        r = getattr(self, '_sr_last_result', None)
        if not r:
            return
        self.log_message(f"【支撐壓力】{volume_profile.summarize(r)}")
        for lv in r['levels']:
            self.log_message(f"　　{lv['role']} {lv['price_str']}"
                             f" (強度 {lv['strength']:.2f}) {lv['label']}")

    # ------------------------------------------------------------------
    # 【ADR-120】盤勢判斷:總開關 / 設定 / 型態通知
    # ------------------------------------------------------------------
    def _regime_toggle(self):
        """主圖那顆【盤勢判斷】勾選鈕:存檔 → 重畫 → 立刻評估一次。

        重畫是必要的:支撐壓力的水平線要靠重畫才會加上或移除。
        """
        self.regime_settings['enabled'] = bool(self.regime_enabled_var.get())
        self._save_regime_settings()
        # 關掉再打開時,使用者會期待「再通知一次現在的狀態」,所以清掉去重狀態。
        self._regime_notify_state = {}
        if self.current_df is not None:
            self.draw_chart(self.current_df)
        self._sr_log_summary()
        if not self.regime_settings['enabled']:
            self.log_message("【盤勢判斷】已關閉。")

    def _save_regime_settings(self):
        """把面板設定寫回 app_settings.json (整份存,讀回來再正規化)。"""
        try:
            self.app_settings['regime_panel'] = regime_panel.normalize(self.regime_settings)
            config_store.save_app_settings(self.app_settings_file, self.app_settings)
        except Exception as e:
            self.log_message(f"【盤勢判斷】設定儲存失敗 ({type(e).__name__}: {e}),本次僅套用於記憶體。")

    def _regime_check(self, raw_df):
        """主圖畫完後評估盤勢/型態,只把「新出現的」寫進系統日誌。

        【為什麼一定要去重】主圖每縮放/平移一次就重畫一次;若照
        evaluate_all() 的結果直接通知,同一個型態會在同一根 K 棒上被通知
        幾十次。去重規則是純函式 (core/regime_panel.plan_notifications),
        離線可測。

        通知前綴用「【盤勢判斷】」——會被 telegram_notify.is_quant_message()
        以外的一般路徑處理,只寫系統日誌,不影響任何委託。
        任何例外都不可影響 K 線圖本身 (跟支撐壓力同樣的理由)。
        """
        try:
            sym = str(self.current_symbol or '').upper()
            tf = self.timeframe_var.get()
            if not regime_panel.should_evaluate(self.regime_settings, sym, tf):
                return
            if raw_df is None or len(raw_df) < 10:
                return
            signals = market_pattern.evaluate_all(
                raw_df, regime_panel.pattern_params(self.regime_settings))
            if not signals:
                return
            bar_date = str(raw_df.index[-1]).split(' ')[0]
            prev = (self._regime_notify_state or {}).get(sym)
            msgs, new_state = regime_panel.plan_notifications(signals, prev, bar_date)
            self._regime_notify_state[sym] = new_state
            if msgs:
                name = self.current_stock_name or sym
                self.log_message(f"【盤勢判斷】{name} {bar_date} {tf}偵測到:" + "；".join(msgs))
        except Exception as e:
            self.log_message(f"【盤勢判斷】型態偵測失敗 ({type(e).__name__}: {e}),本次略過。")

    # 【ADR-132】每日收盤後的盤勢推播:多久檢查一次「時間到了沒」。
    # 60 秒足夠 (推播時刻的解析度是分鐘),而且這是自己的執行緒,
    # sleep 不會拖到任何東西 (跟量化 runner 不同,見 P-90)。
    REGIME_NOTIFY_POLL_SEC = 60

    def regime_daily_notify_worker(self):
        """加權指數盤勢判斷的背景工作者 (ADR-132 / ADR-133)。

        兩件事:
        1. **持續自動判斷** (ADR-133):每根 K 棒收盤後對加權指數的日K 與 60分K
           各評估一次,新出現的型態寫進系統日誌 —— **完全不需要使用者把主圖
           切到那個週期**。使用者的原話:「不需要我人工切換,你就可以自動判斷」。
        2. **每天收盤後推播一次** (ADR-132)。

        **刻意不掛在量化 runner 上**:那條迴圈由「自動交易總開關」控制,
        而盤勢判斷是看盤輔助,使用者沒開自動交易時一樣該運作。
        自己一條 daemon thread,總開關關著也照跑。
        """
        import time as _time
        while True:
            if getattr(self, '_closing', False):
                return
            try:
                self._regime_auto_scan_pass()
            except Exception as e:
                self.safe_after(0, self.log_message,
                                f"【盤勢判斷】自動判斷失敗 ({type(e).__name__}: {e})")
            try:
                self._regime_daily_notify_pass()
            except Exception as e:
                self.safe_after(0, self.log_message,
                                f"【盤勢判斷】每日推播檢查失敗 ({type(e).__name__}: {e})")
            _time.sleep(self.REGIME_NOTIFY_POLL_SEC)

    def _regime_scan_slot(self, tf, now_dt):
        """這個週期「現在屬於哪一根 K 棒」的識別字串。

        用它來決定「要不要重新評估」:同一根 K 棒內重複評估只會抓到一樣的
        資料、算出一樣的結果,白打 API (ADR-127 的同一個教訓)。

        分K 類:對齊 K 棒邊界 (60分K 就是每個整點)。
        日K 類:每 10 分鐘一格 —— 日K 盤中會持續變動 (這條路徑刻意用未完成的
        今日K棒,才看得到「今天正在形成的型態」),但不需要每分鐘重算。
        """
        mins = strategy_engine.timeframe_minutes(tf)
        midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = int((now_dt - midnight).total_seconds() // 60)
        step = mins if mins else 10
        return f"{tf}|{midnight:%Y-%m-%d}|{(elapsed // step) * step}"

    def _regime_auto_scan_pass(self, now_dt=None):
        """【ADR-133】自動對加權指數的日K/60分K 做盤勢判斷,不看主圖現在顯示什麼。

        結果只寫系統日誌 (沿用 ADR-120 的去重),Telegram 推播仍然維持
        「每天收盤後一次」(ADR-132) —— 使用者要的是「不用手動切換也看得到」,
        不是「一直被通知」。
        """
        s = regime_panel.normalize(self.regime_settings)
        if not (s['enabled'] and s['pattern_enabled']):
            return
        if not (self.api_logged_in and getattr(self, 'sj_api', None)):
            return
        now_dt = now_dt or datetime.now()
        # 只在台股有在跑的時候掃:收盤後資料不再變動,一直重掃沒有意義。
        # 收盤後那一輪由每日推播 (_regime_daily_notify_pass) 負責收尾。
        if not market_session.is_stock_open(now_dt):
            return
        if not hasattr(self, '_regime_scan_slots'):
            self._regime_scan_slots = {}
        for tf in regime_panel.PATTERN_TIMEFRAMES:
            try:
                slot = self._regime_scan_slot(tf, now_dt)
                if self._regime_scan_slots.get(tf) == slot:
                    continue
                df = self._regime_notify_fetch(tf)
                if df is None or len(df) < 20:
                    continue
                # 抓到資料才記 slot:抓不到就讓下一輪重試,不要把這一格用掉
                self._regime_scan_slots[tf] = slot
                signals = market_pattern.evaluate_all(df, regime_panel.pattern_params(s))
                if not signals:
                    continue
                bar_date = str(df.index[-1]).split(' ')[0]
                key = f"AUTO|{regime_panel.DEFAULT_INDEX_SYMBOL}|{tf}"
                prev = (self._regime_notify_state or {}).get(key)
                msgs, new_state = regime_panel.plan_notifications(signals, prev, bar_date)
                self._regime_notify_state[key] = new_state
                if msgs:
                    self.safe_after(0, self.log_message,
                                    f"【盤勢判斷】加權指數 {bar_date} {tf}偵測到:"
                                    + "；".join(msgs))
            except Exception as e:
                # 單一週期失敗不可以讓另一個週期也不掃 (鐵則8 的精神)
                self.safe_after(0, self.log_message,
                                f"【盤勢判斷】{tf} 自動判斷失敗 ({type(e).__name__}: {e})")

    def _regime_notify_fetch(self, tf):
        """抓加權指數某個週期的 K 棒給推播用。抓不到回 None。

        用 drop_last=False:這條路徑在收盤後才跑,當天的 K 棒已經定案,
        丟掉它就永遠看不到今天的型態 (見 _qt_fetch_closed_bars 的說明)。
        allow_blocking=True:這是自己的 worker 執行緒,慢一點沒關係 (ADR-122)。
        """
        probe = {'watch_enabled': True, 'watch_symbol': regime_panel.DEFAULT_INDEX_SYMBOL,
                 'watch_trade_type': '指數', 'symbol': regime_panel.DEFAULT_INDEX_SYMBOL,
                 'market': '台股', 'timeframe': tf}
        contract, asset, sym, mkt = self._qt_resolve_watch(probe)
        if contract is None:
            return None
        df = self._qt_fetch_closed_bars(probe, contract, asset, tf=tf,
                                        cache_sym=sym, cache_market=mkt,
                                        allow_blocking=True, drop_last=False)
        return df if (df is not None and not df.empty) else None

    def _regime_daily_notify_pass(self, now_dt=None, force=False):
        """收盤後的每日盤勢推播。時間沒到 / 今天發過了 → 直接返回。

        now_dt / force 是測試接縫:production 兩者都不傳。
        """
        s = regime_panel.normalize(self.regime_settings)
        now_dt = now_dt or datetime.now()
        if not hasattr(self, '_regime_notify_last_date'):
            self._regime_notify_last_date = None
        if not force and not regime_panel.should_notify_now(
                s, now_dt, self._regime_notify_last_date):
            return
        if not (self.api_logged_in and getattr(self, 'sj_api', None)):
            return   # 還沒登入就抓不到指數資料;下一分鐘再試 (不記日誌,免得洗版)

        today = now_dt.strftime('%Y-%m-%d')
        sections = []
        for tf in regime_panel.notify_timeframes(s):
            try:
                df = self._regime_notify_fetch(tf)
                if df is None or len(df) < 20:
                    continue
                signals = []
                if s['pattern_enabled']:
                    signals = market_pattern.evaluate_all(df, regime_panel.pattern_params(s))
                sr_summary = None
                if s['sr_enabled']:
                    r = volume_profile.find_levels(
                        df, asset_type='index_tw',
                        raw_symbol=regime_panel.DEFAULT_INDEX_SYMBOL,
                        max_levels=int(s.get('sr_max_levels', 6) or 6))
                    if r and r.get('levels'):
                        sr_summary = volume_profile.summarize(r)
                sections.append((tf, signals, sr_summary))
            except Exception as e:
                # 單一週期失敗不可以讓另一個週期也不發 —— 各自 try (鐵則8 的精神)
                self.safe_after(0, self.log_message,
                                f"【盤勢判斷】{tf} 每日推播計算失敗 ({type(e).__name__}: {e})")

        # **不論有沒有內容都記今天已處理**:否則沒有訊號的那幾天,這個 pass 會
        # 每 60 秒重跑一次 (含兩次資料抓取) 直到收盤,白白打 API。
        self._regime_notify_last_date = today
        text = regime_panel.format_daily_report(today, sections)
        if not text:
            return   # 沒東西講就不發 —— 一天一次的通知變成噪音就沒人看了 (P-99)
        self.log_message(text)
        if telegram_notify.config_ready(getattr(self, 'telegram_cfg', None)):
            # 直接送,**不經過 log_message 的推播閘門** —— 那道閘門要求
            # 「自動交易總開關開啟」,而盤勢推播是看盤輔助,不該綁在自動交易上。
            self._send_telegram_async(text)

    def open_regime_settings(self):
        """【盤勢判斷】設定:支撐壓力與盤勢/型態兩個子項各自可勾選。"""
        dlg = tk.Toplevel(self)
        dlg.title("盤勢判斷 設定")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 720, 620)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass

        s = regime_panel.normalize(self.regime_settings)

        tk.Label(dlg, text="【盤勢判斷】主圖上的兩項判斷,各自可勾選",
                 bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 11, 'bold')).pack(
                 anchor='w', padx=12, pady=(10, 4))

        # ---- 子項①:量價支撐壓力 ----
        sr_fr = tk.LabelFrame(dlg, text=" ① 量價支撐壓力 (畫在主圖) ", bg="#12181F",
                              fg="#00E676", font=('微軟正黑體', 10, 'bold'))
        sr_fr.pack(fill=tk.X, padx=12, pady=(4, 6))
        v_sr = tk.BooleanVar(value=s['sr_enabled'])
        tk.Checkbutton(sr_fr, text="啟用", variable=v_sr, bg="#12181F", fg="#00E676",
                       selectcolor="#2A323D", activebackground="#12181F",
                       font=('微軟正黑體', 9)).grid(row=0, column=0, sticky='w', padx=6, pady=4)
        tk.Label(sr_fr, text="計算範圍", bg="#12181F", fg="white",
                 font=('微軟正黑體', 9)).grid(row=0, column=1, sticky='e', padx=(10, 2))
        cb_range = ttk.Combobox(sr_fr, values=list(volume_profile.RANGE_MODES), width=9,
                                state="readonly", style='BlackText.TCombobox')
        cb_range.set(s['sr_range_mode'])
        cb_range.grid(row=0, column=2, padx=2)
        tk.Label(sr_fr, text="固定N根", bg="#12181F", fg="white",
                 font=('微軟正黑體', 9)).grid(row=0, column=3, sticky='e', padx=(10, 2))
        e_bars = tk.Entry(sr_fr, width=6, bg="#2A323D", fg="white", justify="center")
        e_bars.insert(0, str(s['sr_fixed_bars'])); e_bars.grid(row=0, column=4, padx=2)
        tk.Label(sr_fr, text="最多幾條", bg="#12181F", fg="white",
                 font=('微軟正黑體', 9)).grid(row=0, column=5, sticky='e', padx=(10, 2))
        e_lv = tk.Entry(sr_fr, width=6, bg="#2A323D", fg="white", justify="center")
        e_lv.insert(0, str(s['sr_max_levels'])); e_lv.grid(row=0, column=6, padx=(2, 6))

        # ---- 子項②:盤勢/型態偵測 ----
        pat_fr = tk.LabelFrame(dlg, text=" ② 盤勢/型態偵測 (只通知,不下單) ", bg="#12181F",
                               fg="#29B6F6", font=('微軟正黑體', 10, 'bold'))
        pat_fr.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))
        v_pat = tk.BooleanVar(value=s['pattern_enabled'])
        tk.Checkbutton(pat_fr, text="啟用", variable=v_pat, bg="#12181F", fg="#29B6F6",
                       selectcolor="#2A323D", activebackground="#12181F",
                       font=('微軟正黑體', 9)).grid(row=0, column=0, sticky='w', padx=6, pady=4)
        tk.Label(pat_fr, text="區間天數", bg="#12181F", fg="white",
                 font=('微軟正黑體', 9)).grid(row=0, column=1, sticky='e', padx=(10, 2))
        e_lb = tk.Entry(pat_fr, width=6, bg="#2A323D", fg="white", justify="center")
        e_lb.insert(0, str(s['pattern_lookback'])); e_lb.grid(row=0, column=2, padx=2)
        tk.Label(pat_fr, text="接近邊界%", bg="#12181F", fg="white",
                 font=('微軟正黑體', 9)).grid(row=0, column=3, sticky='e', padx=(10, 2))
        e_near = tk.Entry(pat_fr, width=6, bg="#2A323D", fg="white", justify="center")
        e_near.insert(0, str(s['pattern_near_pct'])); e_near.grid(row=0, column=4, padx=2)
        tk.Button(pat_fr, text="查看型態說明", bg="#5A6472", fg="white", relief="flat",
                  font=('微軟正黑體', 8),
                  command=lambda: self._show_pattern_catalog(dlg)).grid(row=0, column=5, padx=(10, 6))

        v_idx = tk.BooleanVar(value=s['index_only'])
        tk.Checkbutton(pat_fr, text="只在加權指數判斷 (取消勾選則主圖任何商品的日K都會判斷)",
                       variable=v_idx, bg="#12181F", fg="white", selectcolor="#2A323D",
                       activebackground="#12181F", font=('微軟正黑體', 9)).grid(
                       row=1, column=0, columnspan=6, sticky='w', padx=6)
        tk.Label(pat_fr, text="※ 型態的定義以「一天一根」為前提,因此只在 "
                             + " / ".join(regime_panel.PATTERN_TIMEFRAMES)
                             + " 評估 (60分K 一天只有 4~5 根,語意與日K 同量級);"
                               "偵測到新型態會寫進下方系統日誌。",
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 8)).grid(
                 row=2, column=0, columnspan=6, sticky='w', padx=6, pady=(2, 4))

        pat_vars = {}
        grid_fr = tk.Frame(pat_fr, bg="#12181F")
        grid_fr.grid(row=3, column=0, columnspan=6, sticky='w', padx=6, pady=(2, 8))
        on_now = set(s['pattern_list'])
        for i, item in enumerate(market_pattern.PATTERN_CATALOG):
            v = tk.BooleanVar(value=item['id'] in on_now)
            pat_vars[item['id']] = v
            tk.Checkbutton(grid_fr, text=item['label'], variable=v, bg="#12181F", fg="white",
                           selectcolor="#2A323D", activebackground="#12181F",
                           font=('微軟正黑體', 8)).grid(row=i // 2, column=i % 2,
                                                        sticky='w', padx=6, pady=1)

        # 【ADR-132】每日收盤後推播到手機
        ntf_fr = tk.LabelFrame(dlg, text=" 每日推播 (Telegram) ", bg="#12181F", fg="#00E5FF",
                               font=('微軟正黑體', 9, 'bold'))
        ntf_fr.pack(fill=tk.X, padx=12, pady=(6, 4))
        v_ntf = tk.BooleanVar(value=s['notify_enabled'])
        tk.Checkbutton(ntf_fr, text="每天收盤後把盤勢/型態與支撐壓力推播到手機",
                       variable=v_ntf, bg="#12181F", fg="white", selectcolor="#2A323D",
                       activebackground="#12181F", font=('微軟正黑體', 9)).grid(
                       row=0, column=0, columnspan=4, sticky='w', padx=6, pady=(4, 0))
        tk.Label(ntf_fr, text="推播時刻", bg="#12181F", fg="white",
                 font=('微軟正黑體', 9)).grid(row=1, column=0, sticky='e', padx=(6, 2))
        e_ntf_t = tk.Entry(ntf_fr, width=7, bg="#2A323D", fg="white", justify="center")
        e_ntf_t.insert(0, s['notify_hhmm']); e_ntf_t.grid(row=1, column=1, padx=2, sticky='w')
        ntf_tf_vars = {}
        _tf_fr = tk.Frame(ntf_fr, bg="#12181F"); _tf_fr.grid(row=1, column=2, sticky='w', padx=(12, 0))
        tk.Label(_tf_fr, text="週期", bg="#12181F", fg="white",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(0, 4))
        _on_tf = set(s['notify_timeframes'])
        for _tf in regime_panel.PATTERN_TIMEFRAMES:
            _v = tk.BooleanVar(value=_tf in _on_tf)
            ntf_tf_vars[_tf] = _v
            tk.Checkbutton(_tf_fr, text=_tf, variable=_v, bg="#12181F", fg="white",
                           selectcolor="#2A323D", activebackground="#12181F",
                           font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        tk.Label(ntf_fr, text="※ 一天只發一則 (含所有勾選的週期,每段都會標明週期);"
                             "沒有偵測到任何內容的那天不發。需要先在「Telegram」設定好 bot。",
                 bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 8)).grid(
                 row=2, column=0, columnspan=4, sticky='w', padx=6, pady=(2, 6))

        status = tk.Label(dlg, text="", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9))
        status.pack(anchor='w', padx=12)

        def _apply():
            self.regime_settings = regime_panel.normalize({
                'enabled': bool(self.regime_enabled_var.get()),
                'sr_enabled': bool(v_sr.get()),
                'sr_range_mode': cb_range.get(),
                'sr_fixed_bars': e_bars.get().strip(),
                'sr_max_levels': e_lv.get().strip(),
                'pattern_enabled': bool(v_pat.get()),
                'pattern_lookback': e_lb.get().strip(),
                'pattern_near_pct': e_near.get().strip(),
                'pattern_list': [pid for pid, v in pat_vars.items() if v.get()],
                'index_only': bool(v_idx.get()),
                'notify_enabled': bool(v_ntf.get()),
                'notify_hhmm': e_ntf_t.get().strip(),
                'notify_timeframes': [tf for tf, v in ntf_tf_vars.items() if v.get()],
            })
            self.sr_enabled_var.set(self.regime_settings['sr_enabled'])
            self._save_regime_settings()
            # 參數變了,舊的去重狀態不再代表「已經通知過現在這組設定的結果」。
            self._regime_notify_state = {}
            if self.current_df is not None:
                self.draw_chart(self.current_df)
            self._sr_log_summary()
            status.config(text="✓ 已套用並儲存", fg="#00E676")

        btns = tk.Frame(dlg, bg="#1A2026"); btns.pack(fill=tk.X, padx=12, pady=(4, 10))
        tk.Button(btns, text="套用並儲存", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=14, pady=3, command=_apply).pack(side=tk.LEFT)
        tk.Button(btns, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=16, pady=3, command=dlg.destroy).pack(side=tk.RIGHT)

    def _show_pattern_catalog(self, parent=None):
        """型態說明清單 (唯讀)。原本在終極波段策略編輯器裡,ADR-120 搬到主圖。"""
        info = tk.Toplevel(parent or self)
        info.title("盤勢/型態判斷清單 (共{}種)".format(len(market_pattern.PATTERN_CATALOG)))
        info.configure(bg="#1A2026")
        self.center_window(info, 640, 480)
        info.transient(parent or self)
        try:
            info.lift(); info.focus_force()
        except Exception:
            pass
        txt = tk.Text(info, bg="#12161A", fg="white", font=('微軟正黑體', 9),
                      wrap='word', relief='flat')
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for item in market_pattern.PATTERN_CATALOG:
            txt.insert(tk.END, f"【{item['category']}】{item['label']}\n{item['desc']}\n\n")
        txt.config(state='disabled')
        tk.Button(info, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  command=info.destroy).pack(pady=(0, 10))

    # ======================================================================
    # 【ADR-103】全方位選股
    #
    # 資料全部來自官方免費 API,**完全不動用 shioaji**:全市場 1900+ 檔若
    # 逐檔打 kbars 會直接撞上鐵則 5 的 API 配額上限。改用「一次拿全市場」
    # 的端點,每天只要 4~5 次請求。
    #
    # 技術面/籌碼面條件直接重用 strategy_engine.CONDITIONS——使用者在策略裡
    # 熟悉的條件,選股時是完全相同的語意 (見 core/market_screener 說明)。
    # ======================================================================
    SCREENER_BASE_DIR = APP_DIR
    SC_TWSE_MI_URL = 'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX'
    SC_TWSE_VAL_URL = 'https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d'
    SC_TWSE_REV_URL = 'https://openapi.twse.com.tw/v1/opendata/t187ap05_L'
    SC_TPEX_VAL_URL = 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis'
    SC_TPEX_REV_URL = 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O'
    SC_TPEX_QUOTE_URL = 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes'
    # 【ADR-104】可回溯歷史的上櫃日行情 (吃 date 參數,實測可回溯數月)。
    # 上面那支 openapi 只有當日,補不出上櫃的技術面歷史。
    SC_TPEX_HIST_URL = 'https://www.tpex.org.tw/www/zh-tw/afterTrading/otc'

    # 【ADR-103 修正】財報依「產業別」分成六個端點,必須全部抓才完整。
    # 實測踩到的坑:只抓 _ci 會**漏掉全部金控股** (2881 富邦金、2882 國泰金、
    # 2884 玉山金… 都在 _fh),而金控是台股權值股的重要部分。
    #   ci   一般業 (最大宗)      basi 基本 (少數)
    #   bd   建設業               fh   金融控股
    #   ins  保險業               mim  其他
    # 金控/保險沒有「營業收入/營業毛利」欄位 (損益結構本就不同),解析時
    # 毛利率自然為空——那是正確的,不是資料缺失。EPS 則六種都有。
    SC_FIN_VARIANTS = ('ci', 'basi', 'bd', 'fh', 'ins', 'mim')
    SC_TWSE_INC_TPL = 'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_{v}'
    SC_TWSE_BAL_TPL = 'https://openapi.twse.com.tw/v1/opendata/t187ap07_L_{v}'
    SC_TPEX_INC_TPL = 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_{v}'
    SC_TPEX_BAL_TPL = 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_{v}'
    SC_REQUEST_INTERVAL = 1.0      # 禮貌節流,同 ADR-100 的精神
    SC_HISTORY_DAYS = 180          # 預設歷史深度 (約 120 個交易日);可由「年數」下拉調大
    SC_YEAR_CHOICES = ('6 個月', '1 年', '2 年', '3 年', '5 年', '10 年')
    SC_YEAR_DAYS = {'6 個月': 180, '1 年': 365, '2 年': 730,
                    '3 年': 1095, '5 年': 1825, '10 年': 3650}

    def _sc_history_days(self):
        """使用者選的選股歷史深度 (天)。

        【ADR-118】原本寫死 180 天 —— 扣掉技術指標暖身 (25 天) 之後只剩約
        100 個交易日,以 20 天調倉一次來算才 4~5 期,回測結果的統計意義很薄弱。
        年數調大之後,「更新全市場資料」會去補以前沒抓過的日期 (增量下載),
        回測期數也跟著變多。
        """
        try:
            return self.SC_YEAR_DAYS.get(str(self.cb_sc_years.get()), self.SC_HISTORY_DAYS)
        except AttributeError:
            return self.SC_HISTORY_DAYS
    SC_INDUSTRY_ALL = '全部產業'     # 【ADR-105】產業下拉的「不限」選項

    def _sc_http_json_retry(self, url, params=None, tries=3, timeout=45):
        """帶重試的 JSON 下載。回傳 (資料, 錯誤訊息);成功時錯誤訊息為 ''。

        【為什麼一定要重試】實測發現官方的財報大檔端點 (t187ap06/07 的 _ci
        變體,單檔 1000+ 筆) 會**間歇性失敗**:同一個網址連續請求 4 次,
        第 1 次 JSONDecodeError、後 3 次都正常回 884 筆。沒有重試的話,
        使用者更新時只要剛好碰上那一次失敗,就會少掉整批資料 (例如全部上櫃
        損益表),而且畫面上看不出來——這正是本次驗證所抓到的實際問題。
        """
        last = ''
        for attempt in range(1, int(tries) + 1):
            if self._closing or self._sc_cancel:
                return None, '已中止'
            try:
                return self._chips_http_json(url, params or {}, timeout=timeout), ''
            except Exception as e:
                last = f"{type(e).__name__}: {str(e)[:60]}"
                if attempt < tries:
                    time.sleep(self.SC_REQUEST_INTERVAL * attempt * 2)
        return None, last

    # 【ADR-117】選股結果表有兩種欄位配置:選股結果 12 欄、回測結果 7 欄。
    #
    # 原本「選股」的欄位寫在建構函式裡、「回測」的欄位寫在 _sc_show_backtest
    # 裡,而且**只有回測那邊會重設欄位**。結果是:跑過一次回測之後,再按
    # 「開始選股」,資料是選股的、表頭卻還是回測的 (期別/訊號日/買進日…),
    # 12 個值被塞進 7 個欄位,完全對不起來 —— 這正是使用者回報的問題。
    #
    # 修法是把兩種配置都放這裡,兩個顯示函式各自呼叫 _sc_apply_columns()。
    # 「畫資料前先把欄位設成自己要的」變成兩邊都必須做的同一件事,不可能
    # 只有一邊記得。
    SC_COLUMNS = {
        'screen': (
            ('code', '代號', 60), ('name', '名稱', 90), ('industry', '產業', 90),
            ('close', '收盤', 70), ('pe', '本益比', 60), ('pb', '淨值比', 60),
            ('yield', '殖利率%', 65), ('eps', 'EPS', 60), ('gm', '毛利%', 60),
            ('yoy', '營收年增%', 80), ('roe', 'ROE%', 60), ('why', '符合條件', 260),
        ),
        'backtest': (
            ('period', '期別', 60), ('signal', '訊號日', 110), ('entry', '買進日', 110),
            ('exit', '賣出日', 110), ('picks', '選中檔數', 90),
            ('pnl', '本期損益', 110), ('ret', '本期報酬%', 110),
        ),
    }

    def _sc_apply_columns(self, mode):
        """把結果表切換成指定模式的欄位配置 (畫資料前一定要先呼叫)。"""
        spec = self.SC_COLUMNS[mode]
        try:
            self.tree_sc['columns'] = tuple(c for c, _h, _w in spec)
            for cid, head, width in spec:
                self.tree_sc.heading(cid, text=head)
                self.tree_sc.column(cid, width=width, anchor="center")
        except Exception as e:
            self.log_message(f"【選股】切換欄位配置失敗: {type(e).__name__}: {e}")

    def _build_screener_panel(self, parent):
        self._sc_running = False
        self._sc_cancel = False
        self._sc_rows = []

        bar = tk.Frame(parent, bg="#1A2026")
        bar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 0))
        self.btn_sc_update = tk.Button(
            bar, text="⬇ 更新全市場資料", bg="#00C853", fg="black", relief="flat",
            font=('微軟正黑體', 9, 'bold'), padx=8, pady=2, command=self._sc_start_update)
        self.btn_sc_update.pack(side=tk.LEFT)
        self.btn_sc_run = tk.Button(
            bar, text="🔍 開始選股", bg="#29B6F6", fg="black", relief="flat",
            font=('微軟正黑體', 9, 'bold'), padx=8, pady=2, command=self._sc_start_screen)
        self.btn_sc_run.pack(side=tk.LEFT, padx=4)
        self.btn_sc_bt = tk.Button(
            bar, text="📊 回測條件", bg="#FB8C00", fg="black", relief="flat",
            font=('微軟正黑體', 9, 'bold'), padx=8, pady=2, command=self._sc_open_backtest_dialog)
        self.btn_sc_bt.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_sc_stop = tk.Button(
            bar, text="■ 停止", bg="#2A323D", fg="white", relief="flat",
            font=('微軟正黑體', 9, 'bold'), padx=8, pady=2,
            state=tk.DISABLED, command=self._sc_stop)
        self.btn_sc_stop.pack(side=tk.LEFT)
        # 【ADR-116】選股結果常有數十列,底部分頁塞不下 —— 開大視窗看全貌。
        tk.Button(bar, text="🗖 開啟完整視窗", bg="#455A64", fg="white", relief="flat",
                  font=('微軟正黑體', 9), padx=8, pady=2,
                  command=lambda: self.open_panel_window('screener')).pack(side=tk.LEFT, padx=(8, 10))
        # 【ADR-118】歷史深度可選:回測期數直接取決於這個。
        tk.Label(bar, text="歷史", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        self.cb_sc_years = ttk.Combobox(bar, width=7, state='readonly',
                                        style="BlackText.TCombobox",
                                        values=list(self.SC_YEAR_CHOICES))
        self.cb_sc_years.current(1)          # 預設 1 年 (比原本的 180 天多一倍)
        self.cb_sc_years.pack(side=tk.LEFT, padx=(4, 10))

        tk.Label(bar, text="範本:", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(10, 2))
        self.cb_sc_preset = ttk.Combobox(bar, values=list(market_screener.PRESETS.keys()),
                                         width=24, state="readonly", style='BlackText.TCombobox')
        self.cb_sc_preset.set(list(market_screener.PRESETS.keys())[0])
        self.cb_sc_preset.pack(side=tk.LEFT)
        # 【重用】把「某個已存策略的進場條件」直接拿來掃全市場:策略編輯器
        # 已經有完整的條件 UI,選股不必再做一套一模一樣的編輯器。
        # 【ADR-105】產業別下拉:選項在載入基本面後才填 (不寫死清單,見
        # market_screener.industries_of 的說明)。
        tk.Label(bar, text="產業:", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(10, 2))
        self.cb_sc_industry = ttk.Combobox(bar, values=[self.SC_INDUSTRY_ALL], width=12,
                                           state="readonly", style='BlackText.TCombobox')
        self.cb_sc_industry.set(self.SC_INDUSTRY_ALL)
        self.cb_sc_industry.pack(side=tk.LEFT)

        tk.Label(bar, text="或用策略條件:", bg="#1A2026", fg="#8A99AD",
                 font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(10, 2))
        self.cb_sc_strategy = ttk.Combobox(bar, values=['(不使用)'], width=18,
                                           state="readonly", style='BlackText.TCombobox')
        self.cb_sc_strategy.set('(不使用)')
        self.cb_sc_strategy.pack(side=tk.LEFT)

        self.lbl_sc_status = tk.Label(bar, text="", bg="#1A2026", fg="#8A99AD",
                                      font=('微軟正黑體', 9), anchor="w")
        self.lbl_sc_status.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        # 基本面門檻:留空代表「不設這個條件」
        fbar = tk.Frame(parent, bg="#1A2026")
        fbar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(2, 0))
        tk.Label(fbar, text="基本面門檻 (留空=不限):", bg="#1A2026", fg="#FFCA28",
                 font=('微軟正黑體', 9, 'bold')).pack(side=tk.LEFT)
        self._sc_entries = {}
        for key, op, label in (('pe', market_screener.OP_LTE, '本益比≤'),
                               ('pb', market_screener.OP_LTE, '淨值比≤'),
                               ('yield', market_screener.OP_GTE, '殖利率≥'),
                               ('eps', market_screener.OP_GTE, 'EPS≥'),
                               ('gross_margin', market_screener.OP_GTE, '毛利率≥'),
                               ('revenue_yoy', market_screener.OP_GTE, '營收年增≥'),
                               ('roe', market_screener.OP_GTE, 'ROE≥')):
            tk.Label(fbar, text=label, bg="#1A2026", fg="#8A99AD",
                     font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(8, 1))
            e = tk.Entry(fbar, bg="#2A323D", fg="white", width=5, justify="center",
                         insertbackground="white")
            e.pack(side=tk.LEFT)
            self._sc_entries[key] = (e, op)

        table = tk.Frame(parent, bg="#1A2026")
        table.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 2))
        cols = tuple(c for c, _h, _w in self.SC_COLUMNS['screen'])
        self.tree_sc = ttk.Treeview(table, columns=cols, show="headings", height=5,
                                    style='Trades.Treeview')
        self._sc_apply_columns('screen')
        self.tree_sc.tag_configure('visible_row', foreground='#FFFFFF', background='#12161A')
        # 【鐵則1】回測結果的本期損益:賺紅、賠綠
        self.tree_sc.tag_configure('buy', foreground='#FF1744', background='#12161A')
        self.tree_sc.tag_configure('sell', foreground='#00E676', background='#12161A')
        self.tree_sc.bind("<Double-1>", self._sc_on_row_double_click)
        sb = tk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree_sc.yview)
        self.tree_sc.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_sc.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _sc_refresh_industries(self):
        """【ADR-105】從本地基本面讀出實際有的產業別填進下拉。"""
        try:
            fund = market_store.load_fundamental(self.SCREENER_BASE_DIR)
            names = [self.SC_INDUSTRY_ALL] + market_screener.industries_of(fund)
            self.cb_sc_industry['values'] = names
            if self.cb_sc_industry.get() not in names:
                self.cb_sc_industry.set(self.SC_INDUSTRY_ALL)
        except (tk.TclError, AttributeError):
            pass

    def _sc_refresh_strategy_list(self):
        """把目前已存的策略名稱填進下拉 (切到分頁時更新)。"""
        try:
            names = ['(不使用)'] + [str(s.get('name')) for s in (self.strategies or [])
                                    if s.get('entry')]
            self.cb_sc_strategy['values'] = names
            if self.cb_sc_strategy.get() not in names:
                self.cb_sc_strategy.set('(不使用)')
        except (tk.TclError, AttributeError):
            pass

    def _sc_set_status(self, msg):
        try:
            self.lbl_sc_status.config(text=msg)
        except (tk.TclError, AttributeError):
            pass

    def _sc_set_idle(self):
        try:
            self.btn_sc_update.config(state=tk.NORMAL)
            self.btn_sc_run.config(state=tk.NORMAL)
            self.btn_sc_bt.config(state=tk.NORMAL)
            self.btn_sc_stop.config(state=tk.DISABLED)
        except (tk.TclError, AttributeError):
            pass

    def _sc_stop(self):
        self._sc_cancel = True
        self._sc_set_status("停止中 ...")

    # ---------------- 下載 ----------------
    def _sc_start_update(self):
        if self._sc_running:
            return
        self._sc_running = True
        self._sc_cancel = False
        self.btn_sc_update.config(state=tk.DISABLED)
        self.btn_sc_run.config(state=tk.DISABLED)
        self.btn_sc_stop.config(state=tk.NORMAL)
        threading.Thread(target=self._sc_update_worker, daemon=True).start()

    def _sc_update_worker(self):
        """背景執行緒:補齊全市場日K歷史 + 更新基本面快照。

        日K 用 MI_INDEX 逐日抓 (它吃 date 參數,可回補歷史;STOCK_DAY_ALL
        只有當日,建不出技術面需要的歷史)。已抓過的日期會跳過 (同 ADR-100
        的不重抓機制)。
        """
        try:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=self._sc_history_days())
            have = market_store.available_daily_dates(
                self.SCREENER_BASE_DIR, start_dt.strftime('%Y-%m-%d'),
                end_dt.strftime('%Y-%m-%d'))
            days, cur = [], start_dt
            while cur <= end_dt:
                if cur.weekday() < 5 and cur.strftime('%Y-%m-%d') not in have:
                    days.append(cur)
                cur += timedelta(days=1)
            self.safe_after(0, self.log_message,
                            f"【選股】開始更新:{len(days)} 個交易日待抓 "
                            f"(上市+上櫃各一次,預估約 "
                            f"{len(days) * self.SC_REQUEST_INTERVAL * 2 / 60:.0f} 分鐘)。")
            ok_days = 0
            for i, day in enumerate(days, 1):
                if self._closing or self._sc_cancel:
                    self.safe_after(0, self.log_message, f"【選股】已中止 (完成 {i - 1}/{len(days)})。")
                    break
                self.safe_after(0, self._sc_set_status, f"下載日K {i}/{len(days)}:{day:%Y-%m-%d} ...")
                iso = day.strftime('%Y-%m-%d')
                got = []
                # 上市 (MI_INDEX)
                try:
                    payload = self._chips_http_json(
                        self.SC_TWSE_MI_URL,
                        {'date': day.strftime('%Y%m%d'), 'type': 'ALLBUT0999', 'response': 'json'})
                    df = fundamental_parser.parse_twse_mi_index(payload, date_iso=iso)
                    if not df.empty:
                        got.append(df)
                except Exception:
                    pass          # 單日失敗不中斷整批 (假日/官方暫時性錯誤很常見)
                time.sleep(self.SC_REQUEST_INTERVAL)
                # 【ADR-104】上櫃 (可回溯的 afterTrading/otc):同一天一起補,
                # 兩個市場的技術面歷史才會對齊,不會出現「上市有K棒、上櫃沒有」。
                try:
                    payload = self._chips_http_json(
                        self.SC_TPEX_HIST_URL,
                        {'date': day.strftime('%Y/%m/%d'), 'type': 'EW', 'response': 'json'})
                    df_o = fundamental_parser.parse_tpex_daily_history(payload, date_iso=iso)
                    if not df_o.empty:
                        got.append(df_o)
                except Exception:
                    pass
                if got:
                    market_store.upsert_daily(self.SCREENER_BASE_DIR,
                                              pd.concat(got, ignore_index=True))
                    ok_days += 1
                time.sleep(self.SC_REQUEST_INTERVAL)

            # 基本面快照 (每次更新都整批重抓:它是「現在的值」,不保留歷史)
            if not (self._closing or self._sc_cancel):
                self.safe_after(0, self._sc_set_status, "下載基本面 ...")
                self._sc_update_fundamental()
            self.safe_after(0, self.log_message, f"【選股】更新完成:{ok_days} 個交易日日K已入庫。")
            self.safe_after(0, self._sc_set_status, f"完成 ({ok_days} 日)")
        except Exception as e:
            self.safe_after(0, self.log_message, f"【選股】更新失敗:{type(e).__name__}: {e}")
        finally:
            self._sc_running = False
            self.safe_after(0, self._sc_set_idle)

    def _sc_update_fundamental(self):
        """抓齊所有基本面來源並合併存檔。

        每個來源都走 _sc_http_json_retry (帶重試),且**逐一回報實際筆數**:
        官方財報大檔會間歇性失敗,靜默少掉一整批資料是最危險的情況——
        使用者會以為篩選結果是完整的。重試後仍失敗一定要出現在日誌。
        """
        val = rev = inc = bal = None
        daily = market_store.load_daily_range(
            self.SCREENER_BASE_DIR,
            (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d'),
            datetime.now().strftime('%Y-%m-%d'))
        if daily is not None and not daily.empty:
            latest = daily['Date'].astype(str).max()
            daily = daily[daily['Date'].astype(str) == latest]
        parts_v, parts_r, parts_b = [], [], []
        for name, url, params, parser in (
            ('上市估值', self.SC_TWSE_VAL_URL,
             {'date': datetime.now().strftime('%Y%m%d'), 'selectType': 'ALL', 'response': 'json'},
             fundamental_parser.parse_twse_valuation),
            ('上櫃估值', self.SC_TPEX_VAL_URL, None, fundamental_parser.parse_tpex_valuation),
        ):
            self.safe_after(0, self._sc_set_status, f"下載{name} ...")
            payload, err = self._sc_http_json_retry(url, params)
            if payload is None:
                self.safe_after(0, self.log_message, f"【選股】⚠ {name}重試後仍失敗:{err}")
            else:
                d = parser(payload)
                if d is not None and not d.empty:
                    parts_v.append(d)
                    self.safe_after(0, self.log_message, f"【選股】{name}:{len(d)} 檔")
            time.sleep(self.SC_REQUEST_INTERVAL)
        for name, url in (('上市月營收', self.SC_TWSE_REV_URL), ('上櫃月營收', self.SC_TPEX_REV_URL)):
            self.safe_after(0, self._sc_set_status, f"下載{name} ...")
            payload, err = self._sc_http_json_retry(url)
            if payload is None:
                self.safe_after(0, self.log_message, f"【選股】⚠ {name}重試後仍失敗:{err}")
            else:
                d = fundamental_parser.parse_monthly_revenue(payload)
                if d is not None and not d.empty:
                    parts_r.append(d)
                    self.safe_after(0, self.log_message, f"【選股】{name}:{len(d)} 檔")
            time.sleep(self.SC_REQUEST_INTERVAL)
        # 【ADR-103 修正】財報六個產業別變體全部抓 (見 SC_FIN_VARIANTS 說明:
        # 只抓 _ci 會漏掉全部金控股)。單一變體失敗不影響其他 (鐵則8)。
        parts_i, parts_bal = [], []
        for tpl, bucket, kind, parser in (
            (self.SC_TWSE_INC_TPL, parts_i, '上市損益表', fundamental_parser.parse_income_statement),
            (self.SC_TPEX_INC_TPL, parts_i, '上櫃損益表', fundamental_parser.parse_income_statement),
            (self.SC_TWSE_BAL_TPL, parts_bal, '上市資產負債表', fundamental_parser.parse_balance_sheet),
            (self.SC_TPEX_BAL_TPL, parts_bal, '上櫃資產負債表', fundamental_parser.parse_balance_sheet),
        ):
            got, failed = 0, []
            for v in self.SC_FIN_VARIANTS:
                if self._closing or self._sc_cancel:
                    break
                self.safe_after(0, self._sc_set_status, f"下載 {kind} ({v}) ...")
                payload, err = self._sc_http_json_retry(tpl.format(v=v))
                if payload is None:
                    failed.append(v)      # 重試後仍失敗:一定要讓使用者看見
                else:
                    try:
                        d = parser(payload)
                        if d is not None and not d.empty:
                            bucket.append(d)
                            got += len(d)
                    except Exception:
                        failed.append(v)
                time.sleep(self.SC_REQUEST_INTERVAL)
            msg = f"【選股】{kind}:{got} 筆 (六種產業別合計)"
            if failed:
                # 靜默少掉一整批資料是最危險的:使用者會以為篩選結果是完整的
                msg += f";⚠ {'/'.join(failed)} 重試後仍失敗,這批股票的相關欄位會是空值"
            self.safe_after(0, self.log_message, msg)

        # 【ADR-103 修正】補上櫃日行情:櫃買的估值端點沒有收盤價,少了它
        # 上櫃股票就算不出 ROE (ROE 需要每股淨值 = 收盤價 ÷ 股價淨值比),
        # 實測 ROE 覆蓋率因此只有 55%,且上櫃全是空值。
        self.safe_after(0, self._sc_set_status, "下載上櫃日行情 ...")
        otc_daily, err = self._sc_http_json_retry(self.SC_TPEX_QUOTE_URL)
        if otc_daily is not None:
            d_otc = fundamental_parser.parse_tpex_daily_all(otc_daily)
            if d_otc is not None and not d_otc.empty:
                market_store.upsert_daily(self.SCREENER_BASE_DIR, d_otc)
                daily = pd.concat([daily, d_otc], ignore_index=True) if daily is not None else d_otc
                self.safe_after(0, self.log_message, f"【選股】上櫃日行情:{len(d_otc)} 檔")
        else:
            self.safe_after(0, self.log_message, f"【選股】⚠ 上櫃日行情重試後仍失敗:{err}")
        time.sleep(self.SC_REQUEST_INTERVAL)

        val = pd.concat(parts_v, ignore_index=True) if parts_v else None
        rev = pd.concat(parts_r, ignore_index=True) if parts_r else None
        inc = pd.concat(parts_i, ignore_index=True).drop_duplicates('Code') if parts_i else None
        bal = pd.concat(parts_bal, ignore_index=True).drop_duplicates('Code') if parts_bal else None
        merged = fundamental_parser.merge_fundamentals(
            valuation=val, revenue=rev, income=inc, balance=bal, daily=daily)
        if merged is not None and not merged.empty:
            market_store.save_fundamental(self.SCREENER_BASE_DIR, merged)
            self.safe_after(0, self.log_message,
                            f"【選股】基本面已更新:{len(merged)} 檔 "
                            f"(有EPS {int(merged['EPS'].notna().sum())} 檔)。")
            self.safe_after(0, self._sc_refresh_industries)

    # ---------------- 選股 ----------------
    def _sc_collect_fundamental_conds(self):
        """讀取基本面輸入框;留空=不設該條件。"""
        out = []
        for key, (entry, op) in self._sc_entries.items():
            txt = (entry.get() or '').strip()
            if not txt:
                continue
            try:
                out.append({'field': key, 'op': op, 'value': float(txt)})
            except ValueError:
                self.log_message(f"【選股】{key} 門檻「{txt}」不是有效數字,已略過此條件。")
        return out

    def _sc_start_screen(self):
        if self._sc_running:
            return
        fund = market_store.load_fundamental(self.SCREENER_BASE_DIR)
        if fund is None or fund.empty:
            messagebox.showwarning("選股", "本地尚無基本面資料。\n請先按「⬇ 更新全市場資料」下載。")
            return
        preset = market_screener.PRESETS.get(self.cb_sc_preset.get(), {})
        conds = list(preset.get('conditions') or [])
        f_conds = self._sc_collect_fundamental_conds()
        if not f_conds:
            f_conds = list(preset.get('fundamental') or [])
        # 若選了策略,改用該策略的進場條件 (取代範本的技術條件)
        sname = self.cb_sc_strategy.get()
        if sname and sname != '(不使用)':
            st = next((s for s in (self.strategies or []) if str(s.get('name')) == sname), None)
            if st and st.get('entry'):
                conds = list(st['entry'])
                self.log_message(f"【選股】使用策略「{sname}」的 {len(conds)} 個進場條件掃描全市場。")
        self._sc_running = True
        self._sc_cancel = False
        self.btn_sc_update.config(state=tk.DISABLED)
        self.btn_sc_run.config(state=tk.DISABLED)
        self.btn_sc_stop.config(state=tk.NORMAL)
        ind = self.cb_sc_industry.get()
        industries = None if (not ind or ind == self.SC_INDUSTRY_ALL) else [ind]
        threading.Thread(target=self._sc_screen_worker,
                         args=(fund, conds, f_conds, industries), daemon=True).start()

    def _sc_screen_worker(self, fund, conds, f_conds, industries=None):
        try:
            end_iso = datetime.now().strftime('%Y-%m-%d')
            start_iso = (datetime.now() - timedelta(days=self._sc_history_days())).strftime('%Y-%m-%d')
            daily = None
            if conds:
                self.safe_after(0, self._sc_set_status, "載入全市場日K ...")
                daily = market_store.load_daily_range(self.SCREENER_BASE_DIR, start_iso, end_iso)
                if daily is None:
                    self.safe_after(0, self.log_message,
                                    "【選股】本地沒有全市場日K,技術面條件無法評估;"
                                    "請先按「更新全市場資料」。")
            stock_chips = chips_store.load_stock_inst_range(self.CHIPS_BASE_DIR, start_iso, end_iso)
            margin_chips = chips_store.load(chips_store.margin_path(self.CHIPS_BASE_DIR))

            def _progress(i, total):
                self.safe_after(0, self._sc_set_status, f"篩選中 {i}/{total} ...")

            res = market_screener.screen(
                fund, daily, conditions=conds, fundamental_conds=f_conds,
                stock_chips=stock_chips, margin_chips=margin_chips,
                to_ohlcv=market_store.to_ohlcv_frame, industries=industries,
                progress_cb=_progress, should_stop=lambda: self._sc_cancel or self._closing)
            self._sc_rows = res['rows']
            self.safe_after(0, self._sc_fill_tree, res)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【選股】執行失敗:{type(e).__name__}: {e}")
        finally:
            self._sc_running = False
            self.safe_after(0, self._sc_set_idle)

    def _sc_fill_tree(self, res):
        """【P-31】先備妥再刪再插。"""
        # 【ADR-117】回測跑過之後表頭會停在回測那組,這裡一定要切回選股的
        # 欄位,否則 12 個值會被塞進 7 個欄位,資料與表頭完全對不起來。
        self._sc_apply_columns('screen')
        # 【ADR-116】記住「畫面上現在是什麼」就寫在畫它的地方,不寫在呼叫端——
        # 寫在呼叫端的話,任何一條沒記的路徑都會讓面板搬家後結果消失,
        # 而且不會有錯誤訊息 (實測踩過:直接呼叫這個函式的路徑就沒記到)。
        self._sc_last_render = ('screen', (res,))
        def _f(v, nd=2):
            if v is None:
                return '--'
            try:
                return f"{float(v):.{nd}f}"
            except (TypeError, ValueError):
                return str(v)
        prepared = []
        for r in res['rows']:
            why = ' / '.join((r.get('matched') or []) + (r.get('fundamental_matched') or []))
            prepared.append((r['code'], r['name'], r.get('industry', ''), _f(r['close']), _f(r['pe']),
                             _f(r['pb']), _f(r['yield']), _f(r['eps']),
                             _f(r['gross_margin'], 1), _f(r['revenue_yoy'], 1),
                             _f(r['roe'], 1), why[:120]))
        try:
            for i in self.tree_sc.get_children():
                self.tree_sc.delete(i)
            for vals in prepared:
                self.tree_sc.insert("", "end", values=vals, tags=('visible_row',))
        except (tk.TclError, AttributeError):
            pass
        msg = f"掃描 {res['scanned']} 檔 → 符合 {res['passed']} 檔"
        self._sc_set_status(msg)
        self.log_message(f"【選股】{msg}")
        for label, why in (res.get('errors') or [])[:3]:
            self.log_message(f"【選股-提醒】{label}: {why}")

    # ---------------- 選股回測 (ADR-106) ----------------
    def _sc_open_backtest_dialog(self):
        """【ADR-118】回測設定視窗。

        使用者回報「回測條件是什麼?不能自己設定嗎?期別是什麼?」——原本這個
        按鈕按下去就直接用寫死的參數跑,畫面上沒有任何地方說明它在做什麼,
        「期別」欄位也沒有解釋。與其加註解,不如把參數攤出來讓人設定,
        順便在同一個視窗裡把「這到底在算什麼」講清楚。
        """
        cfg = dict(getattr(self, '_sc_bt_cfg', None) or {})
        dlg = tk.Toplevel(self)
        dlg.title("📊 回測條件 — 設定")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 620, 430)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass

        tk.Label(dlg, text=(
            "這個功能在做什麼:\n"
            "把你目前選的那組條件,拿回過去的每一天重跑一次,看照著選出來的股票\n"
            "買進、持有一段時間再賣出,累積下來會賺還是賠。\n\n"
            "「期別」= 第幾次換股。每隔一個「調倉週期」就重選一次股票,\n"
            "每一次就是一期 —— 期別 1 是最早那一次,依序往後。\n"
            "訊號日 = 條件成立那天;買進日 = 下一個交易日的開盤 (不能當天就買到,\n"
            "那會變成偷看未來);賣出日 = 這一期結束、換股那天。"),
            bg="#12181F", fg="#8A99AD", font=('微軟正黑體', 9), justify='left',
            wraplength=580).pack(fill=tk.X, padx=10, pady=(10, 8))

        top = tk.Frame(dlg, bg="#1A2026"); top.pack(fill=tk.X, padx=14)

        def _row(r, label, key, default, hint):
            tk.Label(top, text=label, bg="#1A2026", fg="white",
                     font=('微軟正黑體', 9)).grid(row=r, column=0, sticky='w', pady=4)
            e = tk.Entry(top, width=10, bg="#2A323D", fg="white")
            e.insert(0, str(cfg.get(key, '') or ''))
            e.grid(row=r, column=1, padx=6, pady=4, sticky='w')
            tk.Label(top, text=hint, bg="#1A2026", fg="#8A99AD",
                     font=('微軟正黑體', 8)).grid(row=r, column=2, sticky='w')
            return e

        e_reb = _row(0, "調倉週期 (交易日)", 'rebalance_days', '',
                     "每隔幾天換一次股。留空=依資料長度自動 (約 5~20 天)")
        e_top = _row(1, "每期最多幾檔", 'top_n', '',
                     "符合條件的股票太多時只取前幾名。留空=10 檔")
        e_min = _row(2, "暖身根數", 'min_bars', '',
                     "技術指標要先有這麼多根K棒才算得準。留空=自動 (20~60)")

        tk.Label(dlg, text=(
            "※ 每期等權買進、含手續費與交易稅。\n"
            "※ 基本面條件在回測中會被略過 —— 基本面只有「當前快照」沒有歷史,\n"
            "   套用到過去等於偷看還沒公布的財報 (未來函數)。\n"
            "※ 想要更多期別,把工具列的「歷史」年數調大再更新全市場資料。"),
            bg="#1A2026", fg="#FFA726", font=('微軟正黑體', 8), justify='left',
            wraplength=580).pack(fill=tk.X, padx=14, pady=(10, 4))

        def _go():
            def _int_or_none(e):
                t = e.get().strip()
                if not t:
                    return None
                try:
                    v = int(t)
                    return v if v > 0 else None
                except ValueError:
                    return None
            self._sc_bt_cfg = {'rebalance_days': _int_or_none(e_reb),
                               'top_n': _int_or_none(e_top),
                               'min_bars': _int_or_none(e_min)}
            dlg.destroy()
            self._sc_start_backtest()

        foot = tk.Frame(dlg, bg="#1A2026"); foot.pack(pady=12)
        tk.Button(foot, text="▶ 開始回測", bg="#FB8C00", fg="black", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=18, pady=4,
                  command=_go).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=18, pady=4,
                  command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _sc_start_backtest(self):
        """回測目前這組選股條件在歷史上的表現。"""
        if self._sc_running:
            return
        daily = market_store.load_daily_range(
            self.SCREENER_BASE_DIR,
            (datetime.now() - timedelta(days=self._sc_history_days())).strftime('%Y-%m-%d'),
            datetime.now().strftime('%Y-%m-%d'))
        if daily is None or daily.empty:
            messagebox.showwarning("選股回測", "本地沒有全市場日K。\n請先按「⬇ 更新全市場資料」。")
            return
        n_days = daily['Date'].nunique()
        if n_days < 32:
            messagebox.showwarning(
                "選股回測",
                f"本地只有 {n_days} 個交易日的資料,不足以回測。\n"
                "請先讓「更新全市場資料」跑完 (預設抓 180 天)。")
            return

        preset = market_screener.PRESETS.get(self.cb_sc_preset.get(), {})
        conds = list(preset.get('conditions') or [])
        sname = self.cb_sc_strategy.get()
        if sname and sname != '(不使用)':
            st = next((x for x in (self.strategies or []) if str(x.get('name')) == sname), None)
            if st and st.get('entry'):
                conds = list(st['entry'])
        f_conds = self._sc_collect_fundamental_conds() or list(preset.get('fundamental') or [])
        if not conds:
            messagebox.showwarning(
                "選股回測",
                "這組條件沒有技術面/籌碼面條件,無法回測。\n\n"
                "基本面資料只有「當前快照」沒有歷史,套用到過去的日期會變成\n"
                "偷看尚未公布的財報 (未來函數),因此不能只靠基本面回測。\n\n"
                "請選一個含技術面條件的範本,或指定某個策略的進場條件。")
            return
        ind = self.cb_sc_industry.get()
        industries = None if (not ind or ind == self.SC_INDUSTRY_ALL) else [ind]
        fund = market_store.load_fundamental(self.SCREENER_BASE_DIR)

        self._sc_running = True
        self._sc_cancel = False
        self.btn_sc_update.config(state=tk.DISABLED)
        self.btn_sc_run.config(state=tk.DISABLED)
        self.btn_sc_bt.config(state=tk.DISABLED)
        self.btn_sc_stop.config(state=tk.NORMAL)
        threading.Thread(target=self._sc_backtest_worker,
                         args=(daily, fund, conds, f_conds, industries), daemon=True).start()

    def _sc_backtest_worker(self, daily, fund, conds, f_conds, industries):
        try:
            n_days = daily['Date'].nunique()
            # 資料只有 N 天時,暖身根數與調倉頻率要跟著縮,否則一期都跑不出來
            # 【ADR-118】使用者沒指定就沿用原本的自動推導 (資料短時自動縮),
            # 指定了就用指定的 —— 但仍夾在資料撐得住的範圍內,否則會一期都跑不出來。
            cfg = getattr(self, '_sc_bt_cfg', None) or {}
            auto_min = max(20, min(60, n_days // 3))
            auto_reb = max(5, min(20, n_days // 6))
            min_bars = int(cfg.get('min_bars') or auto_min)
            reb = int(cfg.get('rebalance_days') or auto_reb)
            top_n = int(cfg.get('top_n') or 10)
            min_bars = max(5, min(min_bars, max(5, n_days - 10)))
            reb = max(1, min(reb, max(1, n_days // 2)))
            start_iso = (datetime.now() - timedelta(days=self._sc_history_days())).strftime('%Y-%m-%d')
            end_iso = datetime.now().strftime('%Y-%m-%d')
            stock_chips = chips_store.load_stock_inst_range(self.CHIPS_BASE_DIR, start_iso, end_iso)
            margin_chips = chips_store.load(chips_store.margin_path(self.CHIPS_BASE_DIR))

            def _prog(i, total):
                self.safe_after(0, self._sc_set_status, f"回測中 第 {i}/{total} 期 ...")

            res = screener_backtest.run_screener_backtest(
                daily, conditions=conds, fundamental_df=fund,
                fundamental_conds=f_conds, industries=industries,
                rebalance_days=reb, top_n=top_n, min_bars=min_bars,
                stock_chips=stock_chips, margin_chips=margin_chips,
                to_ohlcv=market_store.to_ohlcv_frame,
                progress_cb=_prog, should_stop=lambda: self._sc_cancel or self._closing)
            self.safe_after(0, self._sc_show_backtest, res, reb, min_bars)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【選股回測】失敗:{type(e).__name__}: {e}")
        finally:
            self._sc_running = False
            self.safe_after(0, self._sc_set_idle)

    def _sc_show_backtest(self, res, reb, min_bars):
        """把回測結果填進結果表,並把警告寫進系統日誌。"""
        self._sc_last_render = ('backtest', (res, reb, min_bars))   # 【ADR-116】
        for w in (res.get('warnings') or []):
            self.log_message(f"【選股回測】{w}")
        m = res.get('metrics') or {}
        if not m or not m.get('periods'):
            self._sc_set_status("回測無結果 (詳見系統日誌)")
            return
        for line in screener_backtest.summarize(res).split('\n'):
            self.log_message(f"【選股回測】{line}")
        self.log_message(f"【選股回測】設定:每 {reb} 個交易日調倉一次、"
                         f"前 {min_bars} 天暖身、每期最多 10 檔等權、含手續費與交易稅。")
        # 用結果表顯示每一期
        rows = [(str(i), p['signal_date'], p['entry_date'], p['exit_date'],
                 str(p['picks']), f"{p['pnl']:,.0f}", f"{p['return_pct']:+.2f}")
                for i, p in enumerate(res['periods'], 1)]
        try:
            self._sc_apply_columns('backtest')   # 【ADR-117】
            for i in self.tree_sc.get_children():
                self.tree_sc.delete(i)
            for vals in rows:
                # 【鐵則1】本期賺紅、賠綠
                tag = 'buy' if float(vals[6]) > 0 else ('sell' if float(vals[6]) < 0 else 'visible_row')
                self.tree_sc.insert("", "end", values=vals, tags=(tag,))
        except (tk.TclError, AttributeError, ValueError):
            pass
        warn = " ⚠含未來函數" if res.get('has_lookahead') else ""
        self._sc_set_status(
            f"回測 {m['periods']} 期:總報酬 {m['total_return_pct']:+.2f}%、"
            f"超額 {m['excess_pct']:+.2f}%、最大回撤 {m['max_drawdown_pct']:.2f}%{warn}")

    def _sc_on_row_double_click(self, event=None):
        """雙擊結果列 → 直接把該股帶到主圖查詢 (選完股馬上看線圖)。"""
        try:
            sel = self.tree_sc.focus() or (self.tree_sc.selection() or [None])[0]
            if not sel:
                return
            code = str(self.tree_sc.item(sel, 'values')[0])
            self.entry_symbol.delete(0, tk.END)
            self.entry_symbol.insert(0, code)
            self.start_fetch_thread()
        except (tk.TclError, AttributeError, IndexError):
            pass

    def _live_bar_reset_artists(self):
        """draw_chart 後重建活K棒 artists (舊 axes 已被銷毀)。在主執行緒呼叫。"""
        self._live_bar_artists = None
        try:
            if not self.axlist or self.current_df is None:
                return
            tf = getattr(self, 'current_timeframe', None)
            if tf not in self.AUTO_REFRESH_TFS:
                return
            import matplotlib.patches as mpatches
            ax = self.axlist[0]
            wick, = ax.plot([0, 0], [0, 0], color='white', linewidth=1.0, animated=True, visible=False, zorder=60)
            body = mpatches.Rectangle((0, 0), 0.6, 0.0, animated=True, visible=False, zorder=61,
                                       edgecolor='white', linewidth=0.5)
            ax.add_patch(body)
            price_line = ax.axhline(y=0, color='#FFCA28', linestyle=':', linewidth=0.9,
                                     animated=True, visible=False, zorder=59)
            self._live_bar_artists = [wick, body, price_line]
        except Exception:
            self._live_bar_artists = None

    LIVE_BAR_PAINT_MS = 200          # 【ADR-126】活K棒上屏間隔 (原 400ms)
    LIVE_BAR_FALLBACK_SEC = 1.0      # blit 不可用時,最快每隔幾秒退回一次完整重繪

    def _live_bar_blocked_reason(self):
        """【ADR-126】活K棒**這一刻**畫不出來的原因;可以畫回 None。

        存在的理由是:原本這些條件寫成一個大 if,任何一條不成立就**安靜地
        什麼都不做**。使用者回報「K線完全不會即時更新」時,從畫面與日誌完全
        看不出是哪一條卡住 —— 這個函式讓它變成查得到的。
        """
        lb = self._live_bar
        if not lb or not lb.get('dirty'):
            return None if lb else 'no_tick'      # 沒有新 tick 不算「卡住」
        if not getattr(self, '_live_bar_artists', None):
            return 'no_artists'                  # draw_chart 沒建出 artists (週期不支援?)
        if self.current_df is None or len(self.current_df) == 0:
            return 'no_data'
        if self._fetch_in_progress:
            return 'fetching'
        if self._login_in_progress:
            return 'logging_in'
        if getattr(self, '_hover_bg', None) is None:
            return 'no_blit_bg'                  # 最常見的一條 (見 ADR-126)
        return None

    def _live_bar_painter(self):
        """活K棒有新 tick 就用 blitting 疊畫 (毫秒級,不重繪全圖)。

        【ADR-126】blit 畫不成時**不可以就這樣算了**:`_hover_bg` 會被縮放/
        平移/視野變動/尚未觸發 draw_event 等好幾條路作廢,而原本的寫法在那種
        情況下永遠靜悄悄地不畫 —— 使用者看到的就是「K線完全不會即時更新」,
        而且日誌一個字都沒有。所以補一條退路:blit 不可用時改用節流過的
        完整重繪 (draw_idle),慢一點但一定會動。
        """
        try:
            if getattr(self, '_closing', False):
                return
            lb = self._live_bar
            arts = getattr(self, '_live_bar_artists', None)
            blocked = self._live_bar_blocked_reason()
            if blocked in ('no_blit_bg', 'no_artists'):
                self._live_bar_fallback_repaint(blocked)
            if (lb and lb.get('dirty') and arts and self.current_df is not None
                    and len(self.current_df) > 0 and getattr(self, '_hover_bg', None) is not None
                    and not self._fetch_in_progress and not self._login_in_progress):
                lb['dirty'] = False
                wick, body, price_line = arts
                # 活K棒畫在「資料最後一根」的位置:盤中下載到的最後一根就是形成中
                # 的這一根,活K棒直接蓋在它上面,用最新 tick 值即時跳動。
                x = len(self.current_df) - 1
                o, h, l, c = lb['o'], lb['h'], lb['l'], lb['c']
                up = c >= o
                color = '#FF3B30' if up else '#00C853'   # 台灣慣例:漲紅跌綠
                wick.set_data([x, x], [l, h])
                wick.set_color(color)
                top, bot = max(o, c), min(o, c)
                if top == bot:
                    top = bot + 1e-9
                body.set_xy((x - 0.3, bot))
                body.set_width(0.6)
                body.set_height(top - bot)
                body.set_facecolor(color)
                price_line.set_ydata([c, c])
                price_line.set_color('#FF3B30' if up else '#00C853')
                for a in arts:
                    a.set_visible(True)
                if not self._blit_hover():
                    # blit 當場失敗 (底圖剛被視野變動作廢) —— 一樣要有東西動,
                    # 交給節流過的完整重繪,不要靜悄悄地跳過。
                    self._live_bar_fallback_repaint('blit_failed')
        except Exception:
            pass
        finally:
            self.safe_after(self.LIVE_BAR_PAINT_MS, self._live_bar_painter)

    def _live_bar_fallback_repaint(self, reason):
        """【ADR-126】blit 走不通時的退路:節流過的完整重繪。

        完整重繪比 blit 貴很多 (整張圖重畫),所以最快每 LIVE_BAR_FALLBACK_SEC
        秒一次 —— 目的是「一定會動」,不是「動得很順」。順帶在第一次退回時記
        一行日誌並附上原因代碼,下次再出問題就查得到是哪一條,不必再猜。
        """
        now = time.time()
        if now - getattr(self, '_live_bar_fallback_ts', 0.0) < self.LIVE_BAR_FALLBACK_SEC:
            return
        self._live_bar_fallback_ts = now
        if getattr(self, '_live_bar_fallback_noted', None) != reason:
            self._live_bar_fallback_noted = reason
            self.log_message(f"【主圖】活K棒改用完整重繪更新 (原因: {reason});"
                             f"畫面仍會即時跳動,只是比疊畫耗資源。")
        try:
            if self.current_canvas is not None:
                self.current_canvas.draw_idle()
        except Exception:
            pass

    def set_bottom_tab(self, key):
        """切換底部分頁:系統日誌/我的委託單/我的已成交/我的庫存/量化/籌碼。"""
        self.bottom_tab = key
        for frame in (self.log_tab_frame, self.orders_tab_frame, self.fills_tab_frame,
                      self.positions_tab_frame, self.quant_tab_frame, self.chips_tab_frame,
                      self.screener_tab_frame):
            frame.pack_forget()
        {"log": self.log_tab_frame, "orders": self.orders_tab_frame,
         "fills": self.fills_tab_frame, "positions": self.positions_tab_frame,
         "quant": self.quant_tab_frame, "chips": self.chips_tab_frame,
         "screener": self.screener_tab_frame}[key].pack(fill=tk.BOTH, expand=True)
        for k, btn in self.bottom_tab_buttons.items():
            if k == key: btn.config(bg="#29B6F6", fg="black")
            else: btn.config(bg="#2A323D", fg="white")
        # 【第十一輪 第2項】切到庫存分頁時自動查一次 (按需查詢,不做背景輪詢)
        if key == "positions":
            self.refresh_positions()
        # 【ADR-035】切到量化分頁時刷新清單與總開關狀態
        if key == "quant":
            self._qt_refresh_tree()
            self._qt_update_status_label()
        # 【ADR-100】切到籌碼分頁時從本地檔案載入 (純讀檔,不觸發任何下載;
        # 下載一律由使用者按「更新籌碼」明確發動,避免切分頁就打官方網站)
        if key == "chips":
            self._chips_refresh_view()
        # 【ADR-103】切到選股分頁時刷新可用的策略清單 (同樣不觸發下載)
        if key == "screener":
            self._sc_refresh_strategy_list()
            self._sc_refresh_industries()

    # shioaji 委託回報的 action 是英文列舉字串,顯示與比對前先正規化成中文。
    _ACTION_DISPLAY_MAP = {'Buy': '買進', 'Sell': '賣出',
                           'Action.Buy': '買進', 'Action.Sell': '賣出'}

    @classmethod
    def _normalize_action(cls, action):
        """把 shioaji 的 'Buy'/'Sell' (或已是中文的) 一律正規化成 '買進'/'賣出'。"""
        s = str(action or '')
        return cls._ACTION_DISPLAY_MAP.get(s, s)

    @staticmethod
    def _safe_ts(v):
        """把可能是 int/float/字串/None 的時間戳安全轉成 float 供排序,轉不動回 0.0。"""
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def _refresh_my_orders_ui(self):
        """
        【第六輪修正:委託單清單被清空的真正根因】原本的流程是
        「先把 Treeview 刪光 → 再排序 → 再逐列格式化插入」。已用 shioaji 官方
        回報格式端到端重現:只要排序或格式化在中途丟例外 (實測重現點:委託
        回報的 status.exchange_ts 與 seed 的 ts:0 型別不同,sorted() 直接
        TypeError),畫面就會停在「已刪光、還沒插入」的空白狀態——這正是
        使用者回報「日誌印已加入清單 (共1筆),分頁卻空白」的機制,而且之後
        每次 refresh 都死在同一處,清單永遠空白。

        修正原則:「先備妥 → 再刪 → 再插」。所有列先在記憶體裡安全組好
        (排序 key 一律經 _safe_ts 轉 float、逐列各自 try/except,壞一列跳過
        一列不連坐),全部組完才動 Treeview。這樣不管資料多髒,畫面最壞就是
        少顯示有問題的那一列,絕不會整個清空。
        """
        try:
            rows = sorted(self.my_orders.values(),
                          key=lambda o: self._safe_ts(o.get('ts')), reverse=True)
            prepared = []
            for o in rows:
                try:
                    price = o.get('price', 0)
                    if price in (None, '', 0, '0', '市價'):
                        price_str = '市價' if price == '市價' else ''
                    else:
                        try:
                            price_str = self.fmt_price(price)
                        except Exception:
                            price_str = str(price)  # 格式化失敗就原樣顯示,不犧牲整列
                    # 【ADR-023】每列帶 order_id 當 iid,雙擊時才能對應回是哪一筆委託。
                    lot_label = order_rules.MODE_LABELS.get(str(o.get('order_lot', '')), str(o.get('order_lot', '')) or '--')
                    prepared.append((o.get('id', ''), (
                        o.get('time_str', ''), o.get('code', ''),
                        lot_label,
                        self._normalize_action(o.get('action', '')),
                        price_str,
                        o.get('quantity', ''), o.get('filled_quantity', 0),
                        o.get('status_display', ''),
                    )))
                except Exception as row_err:
                    self.log_message(f"【我的委託單】有一筆資料無法顯示,已跳過: {type(row_err).__name__}: {row_err}")
            # 全部備妥後才動畫面
            for row_id in self.tree_orders.get_children():
                self.tree_orders.delete(row_id)
            for oid, vals in prepared:
                try:
                    self.tree_orders.insert("", tk.END, iid=oid, values=vals, tags=('visible_row',))
                except Exception:
                    # iid 若有重複或非法,退回不指定 iid (該列仍顯示,只是不能雙擊操作)
                    self.tree_orders.insert("", tk.END, values=vals, tags=('visible_row',))
            # 【第七輪】強制立即渲染,並印出「Treeview 實際列數」與「my_orders 筆數」。
            # 這是決定性診斷:下次若清單看起來還是空的,看這行就知道到底是
            #   (a) 兩個數字都>0 → 資料與插入都成功,純粹是顯示/主題渲染問題;或
            #   (b) my_orders>0 但 Treeview=0 → 插入端有問題 (可鎖定在插入這段);或
            #   (c) 兩個都=0 → 資料根本沒進 my_orders。
            try:
                self.tree_orders.update_idletasks()
            except Exception:
                pass
            n_tree = len(self.tree_orders.get_children())
            if self.my_orders:
                # 【ADR-027 診斷強化】附上「實際渲染的最新一列」內容。若日誌顯示
                # 已委託、畫面卻停在 PendingSubmit,即可斷定是渲染層問題;反之則
                # 是資料層,直接縮小追查範圍。
                head = f" | 最新列: {prepared[0][1][1]} {prepared[0][1][7]}" if prepared else ""
                self.log_message(f"【我的委託單】畫面已更新: Treeview {n_tree} 列 / my_orders {len(self.my_orders)} 筆{head}")
        except Exception as e:
            self.log_message(f"【我的委託單畫面更新異常】{type(e).__name__}: {e}")

    def _refresh_my_fills_ui(self):
        """與 _refresh_my_orders_ui 同原則:先備妥→再刪→再插,壞一列跳過一列。"""
        try:
            prepared = []
            for f in self.my_fills:
                try:
                    try:
                        price_str = self.fmt_price(f.get('price', 0))
                    except Exception:
                        price_str = str(f.get('price', ''))
                    prepared.append((
                        f.get('time_str', ''), f.get('code', ''),
                        self._normalize_action(f.get('action', '')),
                        price_str, f.get('quantity', ''),
                    ))
                except Exception as row_err:
                    self.log_message(f"【我的已成交】有一筆資料無法顯示,已跳過: {type(row_err).__name__}: {row_err}")
            for row_id in self.tree_fills.get_children():
                self.tree_fills.delete(row_id)
            for vals in prepared:
                self.tree_fills.insert("", tk.END, values=vals, tags=('visible_row',))
            try:
                self.tree_fills.update_idletasks()
            except Exception:
                pass
        except Exception as e:
            self.log_message(f"【我的已成交畫面更新異常】{type(e).__name__}: {e}")

    # ================= 委託刪改 (ADR-023):刪單 / 改量(減) / 改價 =================
    def _on_order_row_double_click(self, event=None):
        """雙擊「我的委託單」某列 → 開啟統一刪改對話框。列的 iid 就是 order_id。"""
        try:
            # 【ADR-027】優先用「滑鼠實際點到的那一列」(identify_row),比 focus/
            # selection 更不會抓錯;抓不到才退回 focus/selection。
            iid = ""
            if event is not None:
                try:
                    iid = self.tree_orders.identify_row(event.y)
                except Exception:
                    iid = ""
            if not iid:
                iid = self.tree_orders.focus() or ((self.tree_orders.selection() or [None])[0])
            self._open_modify_for_iid(iid, source="雙擊")
        except Exception as e:
            self.log_message(f"【刪改】開啟對話框失敗: {type(e).__name__}: {e}")

    def _on_modify_button_click(self):
        """【ADR-027】「🛠 刪改選取委託」按鈕:對目前選取的列開啟刪改對話框。"""
        try:
            iid = self.tree_orders.focus() or ((self.tree_orders.selection() or [None])[0])
            if not iid:
                self.log_message("【刪改】請先在下方清單點選一筆委託,再按「刪改選取委託」。")
                messagebox.showinfo("請先選取委託", "請先在「我的委託單」清單中點選一筆委託,再按此按鈕。")
                return
            self._open_modify_for_iid(iid, source="按鈕")
        except Exception as e:
            self.log_message(f"【刪改】開啟對話框失敗: {type(e).__name__}: {e}")

    def _open_modify_for_iid(self, iid, source=""):
        """依列 iid 反查委託並開啟刪改對話框 (雙擊與按鈕共用)。"""
        if not iid:
            return
        o = self.my_orders.get(iid)
        if not o:
            self.log_message(f"【刪改】找不到對應的委託資料 (iid={iid}),請重新整理後再試。")
            return
        self.log_message(f"【刪改】開啟刪改視窗 ({source}): {o.get('code','')} {self._normalize_action(o.get('action',''))} 狀態:{o.get('status_display','')}")
        self._open_order_modify_dialog(o)

    def _open_order_modify_dialog(self, o):
        """
        統一的委託刪改對話框。上方顯示這筆委託資訊,下方三種操作:
          - 刪單:一律可用 (只要還有未成交量)。
          - 改量:只能減少,輸入新的委託總量。
          - 改價:僅整股可用;零股/盤後定價停用並註明原因。
        每個操作按下後 → 本地規則驗證 → 確認視窗 → 真正呼叫 shioaji。
        """
        order_lot = o.get('order_lot', 'Common')
        is_odd = order_lot in ('IntradayOdd', 'Odd')
        unit = '股' if is_odd else '張'
        cur_qty = self._safe_int(o.get('quantity'))
        filled = self._safe_int(o.get('filled_quantity'))
        outstanding = cur_qty - filled
        can_price = order_rules.price_change_allowed(order_lot)

        # 先擋掉「已不能操作」的委託 (已成交/已取消),連對話框都不用開。
        ok, reason = order_rules.order_is_modifiable(o.get('status_display', ''), cur_qty, filled)
        if not ok:
            self.log_message(f"【刪改】{reason}")
            messagebox.showinfo("無法刪改", reason)
            return

        dlg = tk.Toplevel(self)
        dlg.title("委託刪改")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 420, 340)
        dlg.transient(self)
        try:
            dlg.grab_set()
        except Exception:
            pass
        # 【ADR-027】確保視窗浮在最上並取得焦點,不會默默開在主視窗後面
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass

        mode_label = order_rules.MODE_LABELS.get(order_lot, order_lot)
        act = self._normalize_action(o.get('action', ''))
        try:
            price_str = self.fmt_price(o.get('price', 0)) if o.get('price') else '市價'
        except Exception:
            price_str = str(o.get('price', ''))

        info = (f"商品: {o.get('code','')}  |  {act}  |  {mode_label}\n"
                f"委託價: {price_str}    委託量: {cur_qty}{unit}\n"
                f"已成交: {filled}{unit}    未成交: {outstanding}{unit}\n"
                f"狀態: {o.get('status_display','')}    書號: {o.get('id','')}")
        tk.Label(dlg, text=info, bg="#1A2026", fg="#E0E0E0", justify="left",
                 font=('微軟正黑體', 10)).pack(padx=16, pady=(14, 10), anchor="w")

        tk.Frame(dlg, bg="#2A323D", height=1).pack(fill=tk.X, padx=12, pady=(0, 8))

        # --- 改量列 ---
        qty_row = tk.Frame(dlg, bg="#1A2026"); qty_row.pack(fill=tk.X, padx=16, pady=4)
        tk.Label(qty_row, text=f"改量 (只能減少,新總量<{cur_qty}):", bg="#1A2026", fg="#FFFFFF",
                 font=('微軟正黑體', 10)).pack(side=tk.LEFT)
        var_newqty = tk.StringVar(value=str(max(1, cur_qty - 1)))
        e_qty = tk.Entry(qty_row, textvariable=var_newqty, width=8, justify="center",
                         bg="#2A323D", fg="#FFFFFF", insertbackground="#FFFFFF")
        e_qty.pack(side=tk.LEFT, padx=6)
        tk.Label(qty_row, text=unit, bg="#1A2026", fg="#FFFFFF", font=('微軟正黑體', 10)).pack(side=tk.LEFT)
        tk.Button(qty_row, text="改量", bg="#FB8C00", fg="black", relief="flat",
                  font=('微軟正黑體', 9, 'bold'), padx=10,
                  command=lambda: self._request_modification(dlg, o, 'qty', var_newqty.get())).pack(side=tk.RIGHT)

        # --- 改價列 (僅整股) ---
        price_row = tk.Frame(dlg, bg="#1A2026"); price_row.pack(fill=tk.X, padx=16, pady=4)
        if can_price:
            tk.Label(price_row, text="改價 (新價格):", bg="#1A2026", fg="#FFFFFF",
                     font=('微軟正黑體', 10)).pack(side=tk.LEFT)
            var_newprice = tk.StringVar(value=(self.fmt_price(o.get('price', 0)) if o.get('price') else ''))
            e_price = tk.Entry(price_row, textvariable=var_newprice, width=10, justify="center",
                               bg="#2A323D", fg="#FFFFFF", insertbackground="#FFFFFF")
            e_price.pack(side=tk.LEFT, padx=6)
            e_price.bind("<KeyRelease>", lambda ev: self._normalize_decimal_in(var_newprice))
            tk.Button(price_row, text="改價", bg="#FB8C00", fg="black", relief="flat",
                      font=('微軟正黑體', 9, 'bold'), padx=10,
                      command=lambda: self._request_modification(dlg, o, 'price', var_newprice.get())).pack(side=tk.RIGHT)
        else:
            tk.Label(price_row, text=f"改價: {mode_label}不可改價 (零股不可改價;盤後定價鎖定收盤價)",
                     bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).pack(side=tk.LEFT)

        # --- 刪單 + 關閉 ---
        btn_row = tk.Frame(dlg, bg="#1A2026"); btn_row.pack(fill=tk.X, padx=16, pady=(14, 12), side=tk.BOTTOM)
        tk.Button(btn_row, text="刪單 (取消整筆未成交)", bg="#E53935", fg="white", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=12, pady=4,
                  command=lambda: self._request_modification(dlg, o, 'cancel', None)).pack(side=tk.LEFT)
        tk.Button(btn_row, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=16, pady=4,
                  command=dlg.destroy).pack(side=tk.RIGHT)

    def _normalize_decimal_in(self, var):
        """對話框內的價格欄位:即時把全形句號轉半形 (與主下單欄位一致)。"""
        try:
            raw = var.get(); fixed = raw.translate(self._FULLWIDTH_DOT_MAP)
            if fixed != raw:
                var.set(fixed)
        except Exception:
            pass

    def _request_modification(self, parent_dlg, o, kind, raw_value):
        """本地規則驗證 → 通過才跳確認視窗。kind: 'cancel'/'qty'/'price'。"""
        order_lot = o.get('order_lot', 'Common')
        cur_qty = self._safe_int(o.get('quantity'))
        filled = self._safe_int(o.get('filled_quantity'))
        unit = '股' if order_lot in ('IntradayOdd', 'Odd') else '張'

        if kind == 'cancel':
            ok, reason = order_rules.validate_cancel(o.get('status_display', ''), cur_qty, filled)
            if not ok:
                messagebox.showwarning("無法刪單", reason); return
            summary = (f"【刪單】\n商品 {o.get('code','')} {self._normalize_action(o.get('action',''))}\n"
                       f"取消未成交 {cur_qty - filled}{unit} (整筆委託量 {cur_qty}{unit})")
            self._confirm_modification(parent_dlg, o, 'cancel', None, summary)

        elif kind == 'qty':
            ok, reason = order_rules.validate_qty_change(order_lot, o.get('status_display', ''),
                                                         cur_qty, filled, raw_value)
            if not ok:
                messagebox.showwarning("改量不符規則", reason); return
            new_total = int(str(raw_value).strip())
            summary = (f"【改量】(只能減少)\n商品 {o.get('code','')} {self._normalize_action(o.get('action',''))}\n"
                       f"數量 {cur_qty}{unit} → {new_total}{unit}  (減少 {cur_qty - new_total}{unit})")
            self._confirm_modification(parent_dlg, o, 'qty', new_total, summary)

        elif kind == 'price':
            # 先把輸入價格對齊 tick,再驗證。
            try:
                raw_p = float(str(raw_value).strip().translate(self._FULLWIDTH_DOT_MAP))
            except (TypeError, ValueError):
                messagebox.showwarning("改價不符規則", "新價格請輸入有效數字。"); return
            rounded = tick_rules.round_to_tick(raw_p, self.asset_type, self.current_symbol)
            ok, reason = order_rules.validate_price_change(order_lot, o.get('price', 0), rounded)
            if not ok:
                messagebox.showwarning("改價不符規則", reason); return
            new_price_str = self.fmt_price(rounded)
            note = "" if abs(rounded - raw_p) < 1e-9 else f"\n(已自動對齊跳動單位: {raw_p} → {new_price_str})"
            try:
                old_price_str = self.fmt_price(o.get('price', 0))
            except Exception:
                old_price_str = str(o.get('price', ''))
            summary = (f"【改價】\n商品 {o.get('code','')} {self._normalize_action(o.get('action',''))}\n"
                       f"價格 {old_price_str} → {new_price_str}{note}")
            self._confirm_modification(parent_dlg, o, 'price', rounded, summary)

    def _confirm_modification(self, parent_dlg, o, kind, new_value, summary):
        """刪改確認視窗 (實盤安全:一律先確認再送出)。按「確認送出」才真的打 shioaji。"""
        cdlg = tk.Toplevel(self)
        cdlg.title("確認刪改")
        cdlg.configure(bg="#1A2026")
        self.center_window(cdlg, 380, 220)
        cdlg.transient(self)
        try:
            cdlg.grab_set()
        except Exception:
            pass
        try:
            cdlg.lift(); cdlg.focus_force()
        except Exception:
            pass
        tk.Label(cdlg, text="請確認以下刪改內容:", bg="#1A2026", fg="#FFCA28",
                 font=('微軟正黑體', 11, 'bold')).pack(pady=(16, 8))
        tk.Label(cdlg, text=summary, bg="#1A2026", fg="#FFFFFF", justify="left",
                 font=('微軟正黑體', 11)).pack(padx=16, pady=4)
        btns = tk.Frame(cdlg, bg="#1A2026"); btns.pack(side=tk.BOTTOM, pady=14)
        def _do():
            cdlg.destroy()
            try:
                parent_dlg.destroy()
            except Exception:
                pass
            self._send_order_modification(o, kind, new_value)
        tk.Button(btns, text="確認送出", bg="#E53935", fg="white", relief="flat",
                  font=('微軟正黑體', 10, 'bold'), padx=16, pady=4, command=_do).pack(side=tk.LEFT, padx=8)
        tk.Button(btns, text="取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=20, pady=4, command=cdlg.destroy).pack(side=tk.LEFT, padx=8)

    def _find_trade_for_order(self, order_id):
        """
        取不到 seed 保存的 Trade 時的備援:用 list_trades() 依 order id 找回 Trade。
        這是「為了刪改單一委託而取一次委託列表」,不是輪詢狀態,不違反主動回報原則。
        不同 shioaji 版本 update_status 簽名可能不同,逐一 try,失敗就放棄並誠實回報。

        【找到的bug/修正】update_status()/list_trades() 都是同步網路呼叫,這個
        函式只會從背景執行緒 (_send_order_modification_worker) 呼叫,裡面的
        log_message() 不能直接呼叫 (會在背景執行緒操作 tkinter widget),
        一律要用 safe_after() 排回主執行緒。
        """
        try:
            for attempt in (
                lambda: self.sj_api.update_status(self.sj_api.stock_account),
                lambda: self.sj_api.update_status(),
            ):
                try:
                    attempt(); break
                except Exception:
                    continue
            for t in (self.sj_api.list_trades() or []):
                tid = getattr(getattr(t, 'order', None), 'id', '') or getattr(getattr(t, 'status', None), 'id', '')
                if tid and tid == order_id:
                    return t
        except Exception as e:
            self.safe_after(0, self.log_message, f"【刪改】嘗試取得委託物件失敗: {type(e).__name__}: {e}")
        return None

    def _send_order_modification(self, o, kind, new_value):
        """
        真正呼叫 shioaji 送出刪改。刪改是對 Trade 物件操作,不是給 order id。

        【重要 · 需實機首次驗證】shioaji update_order 的 qty 參數語意 (改量時傳
        「要減少的量」還是「新的總量」) 無法在此離線環境查證。依台灣交易所
        「改量即減量」的規則與 shioaji 文件慣例,本實作採「qty = 要減少的量」,
        並在送出前把「意圖 (10→7)」與「實際 API 參數 (qty=3)」都印進日誌,
        首次實機請用最小差距 (例如 10→9) 測試,並比對券商 App 的結果是否吻合;
        若發現方向相反,只需改這一處的 reduce/new_total 傳法。

        【找到的bug/修正 下單改背景執行緒同一批】find_trade_for_order (可能牽涉
        list_trades() 網路查詢) 跟 cancel_order()/update_order() 原本都直接在
        主執行緒 (確認視窗按鈕 callback) 呼叫——這幾個都是同步網路呼叫,券商
        端回應慢或網路卡住時會讓整個 Tk 事件迴圈停擺 (跟 ADR 記錄的下單當機
        是同一種根因)。改成背景執行緒做,完成後才用 safe_after() 排回主執行緒
        記錄結果。
        """
        if not (self.api_logged_in and HAS_SJ and self.sj_api):
            self.log_message("【刪改】券商 API 未登入,無法送出刪改。")
            return
        threading.Thread(target=self._send_order_modification_worker,
                         args=(o, kind, new_value), daemon=True).start()

    def _send_order_modification_worker(self, o, kind, new_value):
        """背景執行緒:找 Trade 物件 + 真正送出 cancel_order/update_order (皆為
        網路呼叫) 都在這裡做,完成 (含失敗) 都用 safe_after() 排回主執行緒
        記錄結果——這個函式不能碰任何 tkinter widget。"""
        trade = o.get('trade') or self._find_trade_for_order(o.get('id'))
        if trade is None:
            msg = "找不到可操作的委託物件 (此委託可能不是本次連線送出的),請改於券商 App 處理。"
            self.safe_after(0, self._report_order_mod_not_found, msg)
            return
        try:
            if kind == 'cancel':
                self.safe_after(0, self.log_message, f"【刪改送出】刪單 → cancel_order(trade)  書號:{o.get('id')}")
                self.sj_api.cancel_order(trade)
                self.safe_after(0, self._apply_order_mod_success, o.get('id'), kind, None)
            elif kind == 'qty':
                cur = self._safe_int(o.get('quantity'))
                new_total = int(new_value)
                reduce_qty = cur - new_total
                self.safe_after(0, self.log_message, f"【刪改送出】改量 意圖 {cur}→{new_total} (減 {reduce_qty})"
                                f" → update_order(trade, qty={reduce_qty})  書號:{o.get('id')}")
                self.sj_api.update_order(trade, qty=reduce_qty)
                self.safe_after(0, self._apply_order_mod_success, o.get('id'), kind, new_total)
            elif kind == 'price':
                new_price = float(new_value)
                self.safe_after(0, self.log_message, f"【刪改送出】改價 → update_order(trade, price={new_price})  書號:{o.get('id')}")
                self.sj_api.update_order(trade, price=new_price)
                self.safe_after(0, self._apply_order_mod_success, o.get('id'), kind, new_price)
            self.safe_after(0, self.log_message, "【刪改】券商已受理,請等待委託回報確認最終狀態。")
        except Exception as e:
            self.safe_after(0, self._report_order_mod_error, e)

    def _apply_order_mod_success(self, order_id, kind, new_value):
        """【找到的bug/修正】cancel_order()/update_order() 呼叫本身沒有丟例外,
        代表券商已經受理這個刪改請求,但清單上的正式狀態文字完全依賴後續的
        on_order_deal_callback 推播才會更新——使用者實測回報:在非交易時段
        送出的「預約單」,這個推播沒有確實送達,清單狀態停在原本的
        PendingSubmit 不動,讓人誤以為刪改沒有生效而重複送出,結果收到
        券商回的「已取消的預約單」(其實是在告知『上一次已經成功了』,
        不是真的失敗)。這裡比照 ADR-023 對「新委託」已經採用的做法
        (送出成功先樂觀更新清單,不用等推播才顯示):呼叫沒丟例外就先把
        本地狀態文字更新成對應的『已送出』字樣,之後如果真的等到委託回報,
        _handle_order_event 還是會再覆蓋成更精確的狀態,兩邊不衝突。"""
        entry = self.my_orders.get(order_id)
        if not entry:
            return
        if kind == 'cancel':
            entry['status_display'] = '已送出取消(等券商確認)'
        elif kind == 'qty':
            entry['status_display'] = f'已送出改量至{new_value}(等券商確認)'
        elif kind == 'price':
            entry['status_display'] = f'已送出改價至{new_value:g}(等券商確認)'
        self._refresh_my_orders_ui()

    def _report_order_mod_not_found(self, msg):
        self.log_message(f"【刪改】{msg}")
        messagebox.showwarning("無法刪改", msg)

    def _report_order_mod_error(self, e):
        # 【找到的bug/修正】原本只寫進系統日誌,使用者若沒切去那個分頁就看不到
        # 失敗原因,只會覺得「按了沒反應」。改成一定跳出訊息框,錯誤訊息才不會
        # 被漏看——這是實盤刪改委託失敗,使用者需要明確知道有沒有真的送成功。
        # 【找到的bug/修正2】使用者實測發現:如果上一次刪改其實已經成功,
        # 但清單狀態沒更新 (見 _apply_order_mod_success 的說明) 而重複送出,
        # 券商會回「已取消的預約單」這類「其實是成功、不是失敗」的錯誤——
        # 這裡偵測訊息裡有沒有「已取消」/「已收單」/「已完成」這類字樣,
        # 用比較不嚇人的措辭提示使用者,而不是統一都顯示成刺眼的「刪改失敗」。
        msg = f"{type(e).__name__}: {e}"
        self.log_message(f"【刪改失敗】{msg}")
        try:
            if any(kw in str(e) for kw in ("已取消", "已完成", "已收單")):
                messagebox.showwarning(
                    "委託可能已處理過",
                    f"券商回覆這筆委託「已經處理過」,很可能是上一次刪改其實已經成功,\n"
                    f"畫面狀態還沒更新才會看起來像沒生效。請重新整理/確認委託實際狀態,\n"
                    f"不需要再重複送出。\n\n券商原始訊息:\n{msg}")
            else:
                messagebox.showerror("刪改失敗", f"送出刪改時發生錯誤,請確認委託實際狀態:\n\n{msg}")
        except Exception:
            pass
        if self._looks_like_session_dead(e):
            self._mark_session_dead()

    def on_order_deal_callback(self, stat, msg):
        """
        【使用者調整#5】委託/成交主動回報 callback。依官方文件「使用限制」明確要求
        「委託狀態請使用主動回報，避免以 update_status() 輪詢」，這裡用
        set_order_callback() 註冊的 push callback 更新「我的委託單」「我的已成交」
        兩個清單，完全不做輪詢查詢。

        這個 callback 是 shioaji 內部執行緒呼叫的 (跟我們自己的 GUI 主執行緒不同)，
        所有畫面更新都要透過 self.safe_after() 排回主執行緒，不可以直接操作 widget。

        stat 是 shioaji 的 OrderState 列舉 (例如 OrderState.StockOrder /
        OrderState.StockDeal / OrderState.FuturesOrder / OrderState.FuturesDeal，
        不同版本可能是 FOrder/FDeal 或 TFTOrder/TFTDeal 這類別名)。這裡用字串比對
        "Deal"/"Order" 兩種類別而不是精確比對列舉值，避免因為 shioaji 版本不同
        造成列舉命名差異而漏接事件。
        """
        try:
            stat_name = str(stat)
            if "Deal" in stat_name:
                self._handle_deal_event(msg)
            elif "Order" in stat_name:
                self._handle_order_event(msg)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【委託回報處理異常】{e}")

    def _handle_order_event(self, msg):
        try:
            order = msg.get('order', {}) or {}
            status = msg.get('status', {}) or {}
            contract = msg.get('contract', {}) or {}
            operation = msg.get('operation', {}) or {}
            order_id = order.get('id', '')
            if not order_id:
                return
            # 【第六輪修正】shioaji 回報的 action 是英文 'Buy'/'Sell',但 seed 存的是
            # 中文 '買進'/'賣出',原本直接比對必定失敗,導致暫時項目永遠不會被
            # 正式回報替換,同一筆委託在清單裡出現兩筆 (已端到端重現)。比對前
            # 兩邊都先經 _normalize_action 正規化。
            act_norm = self._normalize_action(order.get('action'))
            carried_trade = None  # 【ADR-023】暫時項目被替換時,把它保存的 Trade 物件接續過去
            if (self._last_pending_order_key and self._last_pending_order_info
                    and self._last_pending_order_info.get('code') == contract.get('code')
                    and self._normalize_action(self._last_pending_order_info.get('action')) == act_norm
                    and self._safe_int(self._last_pending_order_info.get('quantity')) == self._safe_int(order.get('quantity'))):
                pending_entry = self.my_orders.get(self._last_pending_order_key, {})
                carried_trade = pending_entry.get('trade')
                self.my_orders.pop(self._last_pending_order_key, None)
                self._last_pending_order_key = None
                self._last_pending_order_info = None
            entry = self.my_orders.get(order_id, {})
            if carried_trade is not None and not entry.get('trade'):
                entry['trade'] = carried_trade
            op_type = operation.get('op_type', '')
            op_msg = operation.get('op_msg', '')
            status_display = {"New": "已委託", "Cancel": "已取消", "UpdateQty": "改量",
                               "UpdatePrice": "改價"}.get(op_type, op_type or entry.get('status_display', '委託中'))
            if op_msg and op_msg not in ("", " "):
                status_display = f"{status_display}({op_msg})"
            entry.update({
                'id': order_id,
                'code': contract.get('code', entry.get('code', '')),
                'action': act_norm or entry.get('action', ''),
                'price': order.get('price', entry.get('price', 0)),
                'quantity': order.get('quantity', entry.get('quantity', 0)),
                'order_cond': order.get('order_cond', entry.get('order_cond', '')),
                'order_lot': order.get('order_lot', entry.get('order_lot', '')),
                'cancel_quantity': status.get('cancel_quantity', entry.get('cancel_quantity', 0)),
                'modified_price': status.get('modified_price', entry.get('modified_price', 0)),
                'status_display': status_display,
                # 【第六輪修正】exchange_ts 型別依 shioaji 版本可能是 int/float/字串,
                # 一律經 _safe_ts 轉成 float 再存,避免與其他項目混排時 sorted() 炸掉。
                'ts': self._safe_ts(status.get('exchange_ts', entry.get('ts', 0))),
                'time_str': datetime.now().strftime('%H:%M:%S'),
            })
            entry.setdefault('filled_quantity', 0)
            self.my_orders[order_id] = entry
            # 【第六輪修正:可觀測性】每收到一筆委託回報都印一行,下次若清單再有
            # 異常,可直接從日誌判斷「回報有沒有進來、進來的內容長什麼樣」。
            self.safe_after(0, self.log_message,
                f"【委託回報】{status_display} {contract.get('code','')} {act_norm} "
                f"價:{order.get('price','')} 量:{order.get('quantity','')} 書號:{order_id}")
            self.safe_after(0, self._refresh_my_orders_ui)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【委託回報解析異常】{type(e).__name__}: {e}")

    def _handle_deal_event(self, msg):
        try:
            fill = {
                'trade_id': msg.get('trade_id', ''),
                'code': msg.get('code', ''),
                'action': self._normalize_action(msg.get('action', '')),
                'price': msg.get('price', 0),
                'quantity': msg.get('quantity', 0),
                'ts': self._safe_ts(msg.get('ts', 0)),  # 【第六輪修正】型別安全
                'time_str': datetime.now().strftime('%H:%M:%S'),
            }
            self.my_fills.insert(0, fill)
            self.my_fills = self.my_fills[:200]  # 上限200筆,避免無限增長佔用記憶體
            # 【第六輪修正:可觀測性】成交回報也印一行
            self.safe_after(0, self.log_message,
                f"【成交回報】{fill['code']} {fill['action']} 價:{fill['price']} 量:{fill['quantity']}")
            order_id = fill['trade_id']
            if order_id in self.my_orders:
                self.my_orders[order_id]['filled_quantity'] = self.my_orders[order_id].get('filled_quantity', 0) + fill['quantity']
                self.my_orders[order_id]['status_display'] = "全部成交" if self.my_orders[order_id]['filled_quantity'] >= self.my_orders[order_id].get('quantity', 0) else "部分成交"
                self.safe_after(0, self._refresh_my_orders_ui)
            self.safe_after(0, self._refresh_my_fills_ui)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【成交回報解析異常】{type(e).__name__}: {e}")

if __name__ == "__main__":
    app = StockTradingAppPro()
    app.mainloop()
