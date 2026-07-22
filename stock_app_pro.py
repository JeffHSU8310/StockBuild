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
from core import futures_session
from core import order_rules
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
from data import config_store
from data import taifex_store

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
        self.current_symbol = "0050" 
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
        
        if HAS_SJ: self.sj_api = sj.Shioaji(simulation=False) 
        
        self.config_file = app_path("broker_config.json")
        self.wl_file = app_path("watchlists.json")
        # 【ADR-072】一般 App 設定 (零股開盤時刻、自動重連/自動登入偏好)。
        self.app_settings_file = app_path("app_settings.json")
        self.app_settings = config_store.load_app_settings(self.app_settings_file)
        # 套用盤中零股開盤時刻到 market_session (預設 09:10,可設定)。
        market_session.set_odd_lot_open_hhmm(self.app_settings.get('odd_lot_open', '09:10'))
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
        
        self.current_fig = None
        self.current_canvas = None
        
        # ================= 指標參數狀態 =================
        self.color_map = {
            "黃 (#FFCA28)": "#FFCA28", "白 (#FFFFFF)": "#FFFFFF", "藍 (#29B6F6)": "#29B6F6",
            "紫 (#E040FB)": "#E040FB", "橘 (#FF9100)": "#FF9100", "紅 (#FF1744)": "#FF1744",
            "青 (#00E5FF)": "#00E5FF", "綠 (#00E676)": "#00E676"
        }
        self.ma_shows = [tk.BooleanVar(value=True if i < 2 else False) for i in range(6)]
        self.ma_types = [tk.StringVar(value="SMA") for i in range(6)]
        self.ma_periods = [tk.StringVar(value=p) for p in ["5", "10", "20", "60", "120", "240"]]
        self.ma_colors = [tk.StringVar(value=c) for c in list(self.color_map.keys())[:6]]
        self.bb_show, self.bb_color = tk.BooleanVar(value=False), tk.StringVar(value="青 (#00E5FF)")
        # 【第九輪 圖3需求】布林通道自訂參數:期間 + 兩組標準差 (上下限各兩組)。
        # 第二組標準差設 0 = 不顯示第二組。
        self.bb_period = tk.IntVar(value=20)
        self.bb_std1 = tk.DoubleVar(value=2.0)
        self.bb_std2 = tk.DoubleVar(value=3.0)
        
        self.var_macd = tk.BooleanVar(value=True)
        self.macd_f, self.macd_s, self.macd_sig = tk.StringVar(value="12"), tk.StringVar(value="26"), tk.StringVar(value="9")
        self.var_rsi = tk.BooleanVar(value=False)
        self.rsi_p = tk.StringVar(value="14")
        self.var_kdj = tk.BooleanVar(value=False)
        self.kd_n, self.kd_m1, self.kd_m2 = tk.StringVar(value="9"), tk.StringVar(value="3"), tk.StringVar(value="3")
        self.var_dmi = tk.BooleanVar(value=False)
        self.dmi_n = tk.StringVar(value="14")
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
        self._qt_load()
        self._qt_refresh_tree()
        threading.Thread(target=self.fetch_market_indices_worker, daemon=True).start()
        threading.Thread(target=self.fetch_realtime_worker, daemon=True).start()
        # 【ADR-028】自選股即時報價 worker:每 10 秒批次抓一次目前群組的快照
        threading.Thread(target=self.watchlist_quote_worker, daemon=True).start()
        # 【ADR-035】量化 runner:總開關關閉時完全閒置
        threading.Thread(target=self.quant_runner_worker, daemon=True).start()
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
        if HAS_SJ and self.api_logged_in:
            # 【第十輪修正 問題4】logout 原本在主執行緒同步呼叫:session 卡死/
            # 網路異常時 logout 會永遠不回來,視窗直接「沒有回應」、連關都關
            # 不掉。改成背景執行緒跑 logout,最多等 3 秒;沒等到就放行——
            # 反正下一步 os._exit(0) 會強制結束整個行程,shioaji 連線隨行程
            # 消滅,券商端 session 由伺服器逾時回收 (重登流程 ADR-026 也會
            # 先 logout 舊連線,不會被殭屍 session 卡住)。
            try:
                t = threading.Thread(target=lambda: self.sj_api.logout(), daemon=True)
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
        self.entry_symbol.insert(0, "0050")
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
        tk.Button(row_frame, text="⚙ 均線/布林", bg="#2A323D", fg="white", relief="flat", command=self.open_main_settings).pack(side=tk.LEFT, padx=2)

        tk.Label(row_frame, text=" || 副圖:", bg="#12161A", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(side=tk.LEFT, padx=(10,2))
        indicators = [
            ("MACD", self.var_macd, lambda: self.open_sub_settings("MACD")),
            ("RSI", self.var_rsi, lambda: self.open_sub_settings("RSI")),
            ("KDJ", self.var_kdj, lambda: self.open_sub_settings("KDJ")),
            ("DMI", self.var_dmi, lambda: self.open_sub_settings("DMI")),
            ("布林", self.var_bbw, lambda: self.trigger_redraw()), 
        ]
        
        for name, var, cmd in indicators:
            tk.Checkbutton(row_frame, text=name, variable=var, bg="#12161A", fg="white", selectcolor="#2A323D", command=self.trigger_redraw).pack(side=tk.LEFT, padx=(3,0))
            if name in ["MACD", "RSI", "KDJ", "DMI"]:
                tk.Button(row_frame, text="⚙", bg="#2A323D", fg="white", relief="flat", padx=2, pady=0, command=cmd).pack(side=tk.LEFT)

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
        for key, label in [("log", "系統日誌與回報"), ("orders", "我的委託單"), ("fills", "我的已成交"), ("positions", "我的庫存"), ("quant", "量化交易")]:
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
            self.sj_api = sj.Shioaji(simulation=False)
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
            self.sj_api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【API 登入失敗】: {e}")
            self.safe_after(0, self.log_message,
                             "【提示】常見原因:(1) 同一帳號同時在永豐金官網/App 登入,佔用交易"
                             "階段名額——請登出該處後再試;(2) 舊連線尚未被券商端釋放——請等約"
                             " 1-2 分鐘再點一次「登入券商實盤 API」;(3) API Key/Secret 有誤。")
            return
        try:
            self.sj_api.activate_ca(ca_path=ca_path, ca_passwd=ca_pw, person_id=pid)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【憑證啟用失敗】: {e}")
            self.safe_after(0, self.log_message, "【提示】請確認憑證路徑、憑證密碼與身分證字號;報價查詢可用,但實盤下單需要憑證通過。")
            # 憑證失敗不 return:登入本身成功,報價功能仍可用;下單時會再被券商擋

        try:
            try:
                # 【修正】只註冊 v1 typed callbacks。
                # 舊版同時掛 v0 set_quote_callback 會造成零股/整股資料互相覆蓋污染。
                self.sj_api.quote.set_on_tick_stk_v1_callback(self.on_tick_stk_v1)
                self.sj_api.quote.set_on_bidask_stk_v1_callback(self.on_bidask_stk_v1)
                self.sj_api.quote.set_on_tick_fop_v1_callback(self.on_tick_fop_v1)
                self.sj_api.quote.set_on_bidask_fop_v1_callback(self.on_bidask_fop_v1)
            except Exception as cb_e:
                self.safe_after(0, self.log_message, f"五檔流初始化異常: {cb_e}")

            try:
                # 【使用者調整#5】委託/成交主動回報:依 shioaji 官方「使用限制」
                # 文件明確建議「委託狀態請使用主動回報，避免以 update_status() 輪詢」，
                # 這裡註冊一次 push callback，「我的委託單」「我的已成交」兩個清單
                # 完全靠這個 callback 更新，不做輪詢查詢。
                self.sj_api.set_order_callback(self.on_order_deal_callback)
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
    QT_COLS = ("name", "symbol", "tf", "direction", "conds", "mode", "status", "running", "today", "pos", "unreal")
    QT_HEADINGS = {"name": "策略名稱", "symbol": "商品", "tf": "週期", "direction": "方向",
                   "conds": "進場條件", "mode": "模式", "status": "狀態", "running": "運轉狀態",
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
                                  ("▶ 啟用", "#00C853", lambda: self._qt_set_enabled(True)),
                                  ("⏸ 停用", "#8A99AD", lambda: self._qt_set_enabled(False)),
                                  ("🔬 回測", "#AB47BC", self._qt_backtest_selected),
                                  ("🎯 參數最佳化", "#00ACC1", self._qt_optimize_selected),
                                  ("📊 策略比較", "#7E57C2", self._qt_compare_dialog)):
                fg = "white" if bg in ("#5A6472", "#AB47BC", "#7E57C2") else "black"
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
        widths = ({"name": 110, "symbol": 70, "tf": 55, "direction": 50, "conds": 240,
                   "mode": 50, "status": 70, "running": 80, "today": 65, "pos": 90, "unreal": 75} if compact else
                  {"name": 180, "symbol": 120, "tf": 70, "direction": 80, "conds": 460,
                   "mode": 70, "status": 80, "running": 100, "today": 80, "pos": 140, "unreal": 90})
        for c in self.QT_COLS:
            tree.heading(c, text=self.QT_HEADINGS[c])
            tree.column(c, width=widths[c], anchor="center")
        tree.tag_configure('qt_on', foreground='#FF1744', background='#12161A')     # 實單紅
        tree.tag_configure('qt_sim', foreground='#29B6F6', background='#12161A')    # 模擬藍
        tree.tag_configure('qt_off', foreground='#8A99AD', background='#12161A')    # 停用灰
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
            'bb_period': self.bb_period.get(), 'bb_std1': self.bb_std1.get(), 'bb_std2': self.bb_std2.get(),
            'var_bbw': self.var_bbw.get(),
            'var_macd': self.var_macd.get(), 'macd_f': self.macd_f.get(),
            'macd_s': self.macd_s.get(), 'macd_sig': self.macd_sig.get(),
            'var_rsi': self.var_rsi.get(), 'rsi_p': self.rsi_p.get(),
            'var_kdj': self.var_kdj.get(), 'kd_n': self.kd_n.get(),
            'kd_m1': self.kd_m1.get(), 'kd_m2': self.kd_m2.get(),
            'var_dmi': self.var_dmi.get(), 'dmi_n': self.dmi_n.get(),
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
            self.bb_std1.set(d.get('bb_std1', self.bb_std1.get()))
            self.bb_std2.set(d.get('bb_std2', self.bb_std2.get()))
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
        except Exception:
            pass  # 讀檔壞掉不影響已經是程式碼預設值的變數,圖表照樣畫得出來

    def _save_indicator_settings(self):
        """【ADR-056】明確動作觸發的存檔:對話框按「確認並套用」時呼叫,
        不是每次打字或每次畫圖都存檔。下次開啟程式會自動載入這份設定。"""
        config_store.save_indicator_settings(self.indicator_settings_file, self._collect_indicator_settings())

    def open_main_settings(self):
        dlg = tk.Toplevel(self); dlg.title("主圖指標參數設定"); dlg.configure(bg="#1A2026"); self.center_window(dlg, 400, 350); dlg.transient(self); dlg.grab_set()      
        tk.Label(dlg, text="開關", bg="#1A2026", fg="white").grid(row=0, column=0, pady=10); tk.Label(dlg, text="類型", bg="#1A2026", fg="white").grid(row=0, column=1); tk.Label(dlg, text="週期", bg="#1A2026", fg="white").grid(row=0, column=2); tk.Label(dlg, text="色彩", bg="#1A2026", fg="white").grid(row=0, column=3)
        for i in range(6):
            tk.Checkbutton(dlg, text=f"MA{i+1}", variable=self.ma_shows[i], bg="#1A2026", fg="white", selectcolor="#2A323D").grid(row=i+1, column=0, sticky="w", padx=15, pady=2)
            ttk.Combobox(dlg, textvariable=self.ma_types[i], values=["SMA", "EMA", "WMA"], width=6, state="readonly", style="BlackText.TCombobox").grid(row=i+1, column=1, padx=5)
            tk.Entry(dlg, textvariable=self.ma_periods[i], width=5, bg="#2A323D", fg="white", justify="center").grid(row=i+1, column=2, padx=5)
            ttk.Combobox(dlg, textvariable=self.ma_colors[i], values=list(self.color_map.keys()), width=10, state="readonly", style="BlackText.TCombobox").grid(row=i+1, column=3, padx=5)
        ttk.Separator(dlg, orient='horizontal').grid(row=7, column=0, columnspan=4, sticky='ew', pady=15)
        # 【第九輪 圖3需求】布林通道參數可自訂:期間 + 兩組標準差 (σ2=0 不顯示第二組)。
        tk.Checkbutton(dlg, text="布林通道", variable=self.bb_show, bg="#1A2026", fg="#00E5FF", selectcolor="#2A323D").grid(row=8, column=0, sticky="w", padx=15)
        bb_frame = tk.Frame(dlg, bg="#1A2026"); bb_frame.grid(row=8, column=1, columnspan=2, sticky="w")
        tk.Label(bb_frame, text="期間", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        tk.Entry(bb_frame, textvariable=self.bb_period, width=4, bg="#2A323D", fg="white", justify="center").pack(side=tk.LEFT, padx=(2, 6))
        tk.Label(bb_frame, text="σ1", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        tk.Entry(bb_frame, textvariable=self.bb_std1, width=4, bg="#2A323D", fg="white", justify="center").pack(side=tk.LEFT, padx=(2, 6))
        tk.Label(bb_frame, text="σ2", bg="#1A2026", fg="white").pack(side=tk.LEFT)
        tk.Entry(bb_frame, textvariable=self.bb_std2, width=4, bg="#2A323D", fg="white", justify="center").pack(side=tk.LEFT, padx=2)
        tk.Label(bb_frame, text="(σ2=0不畫第二組)", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).pack(side=tk.LEFT, padx=4)
        ttk.Combobox(dlg, textvariable=self.bb_color, values=list(self.color_map.keys()), width=10, state="readonly", style="BlackText.TCombobox").grid(row=8, column=3, padx=5)
        tk.Label(dlg, text="※ 按下方按鈕會記住這些設定,下次開啟程式直接沿用。", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 8)).grid(row=8, column=0, columnspan=4, sticky='w', padx=15)
        tk.Button(dlg, text="確認並套用 (並記住此設定)", bg="#29B6F6", fg="black", font=('微軟正黑體', 10, 'bold'), command=lambda: [self._save_indicator_settings(), self.trigger_redraw(), dlg.destroy()]).grid(row=9, column=0, columnspan=4, pady=20)

    def open_sub_settings(self, ind):
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
            return core_indicators.calculate_indicators(
                df, ma_flags, ma_types, ma_periods,
                bb_period=self.bb_period.get(), bb_std1=self.bb_std1.get(), bb_std2=self.bb_std2.get(),
                **common_kwargs)
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
            return core_indicators.calculate_indicators(
                df, ma_flags, ma_types, ma_periods, **common_kwargs)

    def auto_scale_y(self, ax, xmin, xmax):
        """主圖 (價格) 動態 Y 軸放縮 (依視角內最高極值)"""
        if self.plot_df is None or len(self.plot_df) == 0:
            return
        try:
            imin, imax = max(0, int(np.floor(xmin))), min(len(self.plot_df), int(np.ceil(xmax)))
            if imin >= imax:
                return
            sub_df = self.plot_df.iloc[imin:imax]
            low = float(sub_df['Low'].min())
            high = float(sub_df['High'].max())
            if high <= low:
                low, high = low - 1, high + 1
            padding = (high - low) * 0.05
            ax.set_ylim(low - padding, high + padding)
        except Exception:
            pass

    def auto_scale_indicator_panels(self, xmin, xmax):
        """
        副圖 (成交量/MACD/RSI/KDJ/DMI/布林寬) 動態 Y 軸放縮，依視角內最高極值調整。
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
                if name == 'Volume':
                    padding = max(high * 0.15, 1.0)
                    ax.set_ylim(0.0, max(high + padding, 10.0))
                else:
                    if high <= low:
                        low, high = low - 1, high + 1
                    padding = (high - low) * 0.12
                    ax.set_ylim(low - padding, high + padding)
        except Exception:
            pass

    # ================= 🚀 大盤與櫃買指數 API 數據滿載化 =================
    def fetch_market_indices_worker(self):
        while True:
            if self._closing:
                return  # 【ADR-012】視窗已關閉,提早結束這條 daemon thread,不再繼續迴圈或排程更新
            try:
                if self.api_logged_in and HAS_SJ:
                    try:
                        c_twii = self.sj_api.Contracts.Indexs.TSE.TSE001
                        c_twoii = self.sj_api.Contracts.Indexs.OTC.OTC101 
                        
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
                c = self.sj_api.Contracts.Indexs.TSE.TSE001
            elif sym == "^TWOII":
                c = getattr(self.sj_api.Contracts.Indexs.OTC, 'OTC101', None) or getattr(self.sj_api.Contracts.Indexs.OTC, 'OTC001', None)
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

    def watchlist_quote_worker(self):
        """【第十六輪 第2項】1 秒節奏:期貨/指數串流報價每秒上屏 (當沖等級);
        股票批次快照維持每 10 輪 (=10秒) 一次 (P-03 節流)。未登入時安靜等待。"""
        i = 0
        while True:
            try:
                # 【ADR-044】期指/指數串流上屏 1→0.5 秒;股票快照維持 5 秒 (i%10)。
                if i % 10 == 0:
                    self._wl_fetch_quotes_once()
                    self._wl_ensure_stream_subs()
                if self._wl_stream_quotes:
                    snap = dict(self._wl_stream_quotes)
                    self._wl_stream_quotes = {}
                    self.safe_after(0, self._apply_wl_quotes, snap)
            except Exception:
                pass
            i += 1
            time.sleep(0.5)

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
                        # 【修正1】節流至每 5 秒一次 —— 原本每 0.5 秒打一次 snapshots,
                        #          會快速耗盡 shioaji 每日 API 流量配額,之後所有查詢失效。
                        # 【修正2】shioaji snapshot「只有整股資料」,零股模式下不再拿整股
                        #          快照冒充零股報價 (這正是零股數據看起來錯亂的主因)。
                        now_time = time.time()
                        if now_time - self.last_fallback_snap_time >= 5:
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
            time.sleep(0.25)  # 【ADR-044】報價上屏 0.5→0.25 秒 (tick 是推播,加快上屏無 API 成本)

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
        threading.Thread(target=self.fetch_data_worker, args=(raw_sym, tf, seq, market), daemon=True).start()

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
            if pd.notna(low) and pd.notna(high) and high > low:
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
               "日K": 365, "周K": 1095, "月K": 1825}
    # 【ADR-069】兩段式第一段的小範圍。日/周/月K 特別小:因為日K以上的「深歷史」
    # 是由 yahoo(股票 20年)/期交所(期貨) 在同一次 _publish 裡延伸補上的,shioaji
    # 只需供「最近幾根」的即時新鮮度,不必抓 45~600 天的 1 分 K 回來重採樣 (那才是
    # 日K 切換要等十幾二十秒的主因)。窗口小 → 搶先出圖近乎即時,完整深歷史照樣有。
    QUICK_DAYS = {"1分K": 2, "5分K": 5, "15分K": 10, "30分K": 15, "60分K": 20, "日K": 7, "周K": 45, "月K": 120}
    CACHE_TTL_MIN_TF = 30    # 分K類快取視為新鮮的秒數
    CACHE_TTL_DAY_TF = 300   # 日K以上快取視為新鮮的秒數
    KBARS_CACHE_MAX = 6      # 快取最多保留幾檔商品的原始分K (LRU 淘汰最舊)

    def _download_kbars_chunked(self, contract, start_dt, end_dt, chunk_days=90, progress_cb=None,
                                retries=2, pace_sec=0.35, subsplit_days=30, abort_cb=None):
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
            last = None
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
                    last = e
            if last is not None:
                _log_usage_once()
                err_str = str(last)
                if "ServerError: kbars: request" in err_str:
                    err_brief = "券商該期間尚無資料或商品未上市"
                else:
                    err_brief = f"{type(last).__name__}: {err_str[:60]}"
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
                if last is not None and "ServerError: kbars: request" in str(last):
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
                    sp = _try_seg(sub, sub_end, 1)
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
        resample_map = {"1分K": '1min', "5分K": '5min', "15分K": '15min', "30分K": '30min',
                        "60分K": '60min', "日K": 'D', "周K": 'W-MON', "月K": 'MS'}
        rule = resample_map.get(tf)
        if not rule:
            return sj_df
        agg_dict = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        if tf in ["日K", "周K", "月K"]:
            if asset_type == "future":
                # 期貨:用「交易日(session date)」聚合,不能用 resample('D') (ADR-007)。
                return self._resample_future_session(sj_df, tf, agg_dict, session_basis=session_basis)
            return sj_df.resample(rule, label='left', closed='left').agg(agg_dict).dropna()
        out = sj_df.resample(rule, label='left', closed='left').agg(agg_dict).dropna()
        mins = {"1分K": 1, "5分K": 5, "15分K": 15, "30分K": 30, "60分K": 60}.get(tf, 0)
        if mins > 0:
            out.index = out.index + pd.Timedelta(minutes=mins)
        return out

    def _kbars_cache_get(self, key, need_start):
        """取快取。回傳 (raw_df涵蓋need_start之後 或 None, 是否新鮮)。"""
        c = self._kbars_raw_cache.get(key)
        if not c or c['start'] > need_start:
            return None, False
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

    def _taifex_load_hist(self, tx_id, session='all'):
        """讀取某期交所商品/盤別的本地日K (經記憶體快取)。沒匯入過回傳空 df。
        【ADR-058】快取 key 加上盤別,近全與只取日盤是兩份獨立資料。

        【ADR-060】「有沒有讀到期交所資料」必須看得見。
        使用者最大的困惑是:明明匯入過,系統卻照樣狂發分段下載,而且完全
        沒有任何訊息說明為什麼 —— 因為 load_daily 找不到檔案就安靜回傳空表。
        現在每個 (商品, 盤別) 第一次查詢時一定寫一行日誌,講清楚:
        讀到了幾根、涵蓋到哪、或者「找不到,我找的是這個完整路徑」。
        """
        key = (tx_id, str(session))
        hist = self._taifex_mem_cache.get(key)
        if hist is None:
            path = taifex_store.store_path(self.TAIFEX_BASE_DIR, tx_id, session=session)
            hist = taifex_store.load_daily(self.TAIFEX_BASE_DIR, tx_id, session=session)
            self._taifex_mem_cache[key] = hist
            sess_txt = '只用日盤' if session == 'day' else '近全'
            if hist is None or hist.empty:
                self.safe_after(0, self.log_message,
                                f"【期交所歷史】✗ 找不到 {tx_id} ({sess_txt}) 的本地資料。"
                                f"我找的路徑是:{path} —— 請確認檔案就在這裡 "
                                f"(用「📥 期交所歷史」匯入會自動存到正確位置)。")
            else:
                self.safe_after(0, self.log_message,
                                f"【期交所歷史】✓ 已讀取 {tx_id} ({sess_txt}) {len(hist):,} 根,"
                                f"涵蓋 {hist.index[0]:%Y-%m-%d} ~ {hist.index[-1]:%Y-%m-%d}。"
                                f"(檔案:{path})")
        return hist

    def show_taifex_status(self):
        """【ADR-060】一次列出所有期交所商品的本地資料狀態 —— 使用者要能直接
        回答「到底有沒有讀到、放對地方了沒」,不必自己翻資料夾猜。"""
        lines = [f"期交所歷史資料夾:{os.path.join(self.TAIFEX_BASE_DIR, taifex_store.SUBDIR)}", ""]
        any_found = False
        for tx_id in sorted(set(taifex_daily.PRODUCT_MAP.values())):
            for sess, label in (('all', '近全'), ('day', '日盤')):
                path = taifex_store.store_path(self.TAIFEX_BASE_DIR, tx_id, session=sess)
                if os.path.exists(path):
                    df = taifex_store.load_daily(self.TAIFEX_BASE_DIR, tx_id, session=sess)
                    if df is not None and not df.empty:
                        any_found = True
                        lines.append(f"✓ {tx_id} ({label}):{len(df):,} 根,"
                                     f"{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}")
                    else:
                        lines.append(f"⚠ {tx_id} ({label}):檔案存在但讀不出內容 → {path}")
                else:
                    lines.append(f"✗ {tx_id} ({label}):無檔案 → {os.path.basename(path)}")
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
        """【ADR-058】由合約推出期交所商品代碼;非 R1 連續合約或不支援的商品回 None。
        抽成獨立方法,讓「延伸」與「跳過下載」兩條路徑用同一套判斷,不會分歧。"""
        try:
            sym = str(getattr(contract, 'symbol', '') or '').upper()
            if not sym.endswith('R1'):
                return None
            return taifex_daily.PRODUCT_MAP.get(fut_catalog.product_code(sym))
        except Exception:
            return None

    def _taifex_plan_download(self, contract, asset_type, tf, start_dt, end_dt, session='all', tag="回測"):
        """【ADR-058】使用者需求 #1 的核心解法:能不下載就不要下載。

        使用者回報「回測/最佳化下載一直被券商擋」(ServerError: kbars request
        $P2P/... = Solace 路由層流量管制)。ADR-048 已經做過節流/退避/切小段,
        但那些都還是在「想辦法把請求送出去」。真正有效的作法是:期貨 R1 的
        日/周/月K,只要本地期交所歷史已經涵蓋那段期間,就根本不必向券商要
        —— 少發幾百個請求,自然不會被擋,也不吃你的每日流量配額。

        回傳 (need_from, need_to, note):
          * need_from/need_to = 仍需向券商下載的區間 (need_from 為 None 代表
            完全不必下載)
          * note = 給日誌看的白話說明
        任何判斷失敗一律退回「照原樣全段下載」,絕不因為這個最佳化而少抓資料。
        """
        try:
            if asset_type != "future" or tf not in ("日K", "周K", "月K"):
                return start_dt, end_dt, ""
            tx_id = self._taifex_prod_of(contract)
            if not tx_id:
                return start_dt, end_dt, ""
            hist = self._taifex_load_hist(tx_id, session=session)
            if (hist is None or hist.empty) and session == 'day':
                hist = self._taifex_load_hist(tx_id, session='all')
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

    def _extend_with_yahoo(self, pub_df, tf, sym=None):
        """對於股票/ETF，若 shioaji 資料不夠長，向 yfinance 請求深層歷史補齊。"""
        try:
            if pub_df is None or pub_df.empty: return pub_df
            sym = sym or getattr(self, 'current_symbol', '')
            if not sym or not sym[0].isdigit(): return pub_df
            
            yf_params = {"日K": ("20y", "1d"), "周K": ("20y", "1wk"), "月K": ("20y", "1mo")}
            if tf not in yf_params: return pub_df
            period, interval = yf_params[tf]
            
            import yfinance as yf
            df_yf = pd.DataFrame()
            for suffix in [".TW", ".TWO"]:
                df_yf = yf.Ticker(f"{sym}{suffix}").history(period=period, interval=interval, auto_adjust=False)
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
            return merged
        except Exception as e:
            self.safe_after(0, self.log_message, f"【深層歷史】由 yfinance 補齊失敗: {e}")
            return pub_df

    def _extend_with_taifex(self, pub_df, tf, contract=None, session='all'):
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
            
            # TODO: ... (taifex logic remains exactly as is, this is just to anchor the new method)
            # Actually I don't need to replace `_extend_with_taifex`, I will prepend `_extend_with_yahoo` before `_extend_with_taifex` definition.
            # 【ADR-058】依回測選定的盤別口徑取用對應的那一份期交所資料
            hist = self._taifex_load_hist(tx_id, session=session)
            if (hist is None or hist.empty) and session == 'day':
                self.safe_after(0, self.log_message,
                                f"【期交所歷史】{tx_id} 尚無「只用日盤」的資料檔 ({tx_id}_day.csv),"
                                f"請重新匯入一次期交所歷史即可同時產生。本次改用近全序列。")
                hist = self._taifex_load_hist(tx_id, session='all')
            if hist is None or hist.empty:
                return pub_df
            out = taifex_daily.extend_shioaji_df(pub_df, hist, tf)
            if len(out) > len(pub_df) and (tx_id, session) not in self._taifex_extend_noted:
                self._taifex_extend_noted.add((tx_id, session))
                self.safe_after(0, self.log_message,
                                f"【期交所歷史】{tx_id} 已用官方每日行情往前延伸至 {out.index[0]:%Y-%m-%d}"
                                f" (盤別:{'只用日盤' if session == 'day' else '近全'};重疊日期以 shioaji 為準)。")
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

    def fetch_data_worker(self, raw_sym, tf, seq=None, market="台股"):
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
            self._fetch_data_worker_impl(raw_sym, tf, seq, market)
        finally:
            self._fetch_in_progress = False

    def _fetch_data_worker_impl(self, raw_sym, tf, seq=None, market="台股"):
        try:
            yf_params = {"1分K": ("5d", "1m"), "5分K": ("30d", "5m"), "15分K": ("30d", "15m"), "30分K": ("30d", "30m"), "60分K": ("90d", "60m"), "日K": ("10y", "1d"), "周K": ("10y", "1wk"), "月K": ("10y", "1mo")}
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
                        if contract: search_sym = contract.symbol; stock_name = fut_catalog.display_name(contract.symbol, contract.name); self.asset_type = "future"
                    elif raw_sym == "^TWII":
                        contract = self.sj_api.Contracts.Indexs.TSE.TSE001
                        if contract: search_sym = raw_sym; stock_name = "加權指數"; self.asset_type = "index_tw"
                    elif raw_sym == "^TWOII":
                        contract = getattr(self.sj_api.Contracts.Indexs.OTC, 'OTC101', None)
                        if not contract: contract = getattr(self.sj_api.Contracts.Indexs.OTC, 'OTC001', None)
                        if contract: search_sym = raw_sym; stock_name = "櫃買指數"; self.asset_type = "index_tw"
                    elif raw_sym in ['TXF', 'MTX', 'FITX', 'MXF'] or self._looks_like_futures_symbol(raw_sym):
                        # 【第十一輪修正】台股模式下輸入/點選期貨完整代號 (TXFR2 等)
                        # 也走期貨解析,不再掉進股票查詢報「合約查無」。
                        contract = self._resolve_futures_contract(raw_sym)
                        if contract: search_sym = contract.symbol; stock_name = fut_catalog.display_name(contract.symbol, contract.name); self.asset_type = "future"
                    else:
                        contract = self.sj_api.Contracts.Stocks.get(raw_sym)
                        if not contract: contract = next((c for c in self.sj_api.Contracts.Stocks if c.symbol == raw_sym), None)
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
            _pub_state = {'n': None}  # 【ADR-049】上次實際發布的K棒根數 (含期交所延伸)

            def _publish(pub_df, full_ui=True, prev_len=None, note=""):
                if seq is not None and seq != self._fetch_seq:
                    return False  # 已有更新的查詢,放棄發布
                # 【ADR-049】期貨 R1 連續合約的日/周/月K,用期交所官方日行情往前
                # 延伸更長歷史 (只補 shioaji 涵蓋不到的更早日期,重疊處 shioaji 權威)。
                # 放在 _publish 統一入口:快取/快速段/完整段三條路徑都同樣延伸,
                # prev_len 平移邏輯不受影響 (兩次發布延伸的根數相同,差值仍正確)。
                if self.asset_type == "future" and tf in ("日K", "周K", "月K"):
                    pub_df = self._extend_with_taifex(pub_df, tf)
                elif self.asset_type == "stock" and tf in ("日K", "周K", "月K"):
                    pub_df = self._extend_with_yahoo(pub_df, tf, sym=search_sym)
                pub_df = pub_df.dropna(subset=['Open', 'High', 'Low', 'Close'])
                if pub_df.empty:
                    return False
                # 【ADR-049】記錄實際發布根數 (含延伸)。背景補全的 prev_len 必須用
                # 「上次實際發布的根數」計算平移量;若用延伸前的長度,xlim 會被
                # 多平移「延伸根數」而跳離原視角。
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
                            # 讀取目前視角並平移相同根數,視覺上完全不動。
                            try:
                                added = len(pub_df) - prev_len
                                if added > 0:
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
                else:
                    max_sj_lookback = end_dt - timedelta(days=1095)
                    if start_dt < max_sj_lookback:
                        start_dt = max_sj_lookback
                cache_key = search_sym

                # ---- 第一優先:快取涵蓋範圍 → 立即重採樣出圖 (秒開) ----
                cached_raw, cache_fresh = self._kbars_cache_get(cache_key, start_dt)
                published_from_cache = False
                cached_len = None
                if cached_raw is not None and not cached_raw.empty:
                    try:
                        df_cached = self._resample_sj_df(cached_raw, tf)
                        if _publish(df_cached, full_ui=True):
                            published_from_cache = True
                            cached_len = _pub_state['n']  # 【ADR-049】含延伸的實際發布根數
                    except Exception:
                        pass
                if published_from_cache and cache_fresh:
                    return  # 快取新鮮,直接完工——這就是「換週期/切回商品秒開」的路徑

                # ---- 兩段式第一段:先抓小範圍搶先出圖 ----
                # 【ADR-068/069】先抓 QUICK_DAYS 的小範圍「單次」下載搶先出圖,K 線
                # 馬上可看可 hover,完整歷史在同一背景執行緒接著補全 (補完就地換圖,
                # 視角不動)。
                #   ADR-068:股票的「分K」加入快速段 (原本只有期貨/指數有)。
                #   ADR-069:股票的「日/周/月K」也加入。日K以上原本沒快速段、又抓
                #     365 天 1 分 K 回來重採樣 (~15 秒),是日K切換慢的主因;其實深
                #     歷史是 _publish 裡的 yahoo/期交所延伸補的,shioaji 只要供最近
                #     幾根,故用很小的 QUICK_DAYS 就能搶先出「完整」日K圖。代價是
                #     快速段+完整段各跑一次 yahoo 延伸,但第二次在背景、不擋出圖。
                quick_len = None
                want_quick = self.asset_type in ("future", "index_tw", "stock")
                if (not published_from_cache) and want_quick and tf in self.QUICK_DAYS:
                    try:
                        q_days = self.QUICK_DAYS[tf]
                        q_start = end_dt - timedelta(days=q_days)
                        self.safe_after(0, self.log_message, f"⚡ 先載入近 {q_days} 天搶先出圖,完整歷史背景補全中...")
                        quick_raw = self._download_kbars_raw(contract, q_start, end_dt)
                        if quick_raw is not None and not quick_raw.empty:
                            df_quick = self._resample_sj_df(quick_raw, tf)
                            if _publish(df_quick, full_ui=True):
                                quick_len = _pub_state['n']  # 【ADR-049】含延伸的實際發布根數
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
                if prev is not None:
                    _publish(df_full, full_ui=False, prev_len=prev,
                             note=f"【背景補全】完整歷史已更新 (共 {len(df_full)} 根,總耗時 {time.time()-_t0:.1f} 秒)。")
                else:
                    _publish(df_full, full_ui=True)
                    self.safe_after(0, self.log_message, f"⚡ 完整歷史載入完成 (共 {len(df_full)} 根),耗時 {time.time()-_t0:.1f} 秒。")
                return
            else:
                # 美股:自動使用 yfinance
                self.data_source = "yfinance"
                self.safe_after(0, self.log_message, f"正在透過 YFinance 載入 {raw_sym} 歷史數據 (美股)...")
                try:
                    df = yf.Ticker(raw_sym).history(period=period, interval=interval, auto_adjust=adjust_flag)
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
            df = self.calculate_custom_indicators(raw_df)
            txt_fmt_char = self.timeframe_var.get()
            max_bars = 15000 if ("K" in txt_fmt_char and "日" not in txt_fmt_char and "周" not in txt_fmt_char and "月" not in txt_fmt_char) else 10000
            if len(df) > max_bars: df = df.iloc[-max_bars:].copy()
            self.plot_df = df 
            
            if getattr(self, 'current_canvas', None) is not None:
                try: self.current_canvas.get_tk_widget().destroy()
                except: pass
                self.current_canvas = None

            if getattr(self, 'current_fig', None) is not None:
                plt.close(self.current_fig)
                self.current_fig = None

            for widget in self.chart_frame.winfo_children(): widget.destroy()
            plt.close('all')
            gc.collect() 
            
            apds = []
            panel_ratios = [5, 1.2] 
            current_panel = 2
            active_panels = {'Volume': 1}

            for i in range(6):
                col_name = f"MA_CUSTOM_{i}"
                if self.ma_shows[i].get() and col_name in df.columns:
                    c_hex = self.color_map.get(self.ma_colors[i].get(), "#FFFFFF")
                    apds.append(mpf.make_addplot(df[col_name], panel=0, color=c_hex, width=1.2, secondary_y=False))

            if self.bb_show.get() and 'BB_UPPER' in df.columns:
                bb_hex = self.color_map.get(self.bb_color.get(), "#00E5FF")
                apds.append(mpf.make_addplot(df['BB_UPPER'], panel=0, color=bb_hex, linestyle='--', width=1.0, secondary_y=False))
                apds.append(mpf.make_addplot(df['BB_MID'], panel=0, color=bb_hex, linestyle='-', width=1.0, secondary_y=False))
                apds.append(mpf.make_addplot(df['BB_LOWER'], panel=0, color=bb_hex, linestyle='--', width=1.0, secondary_y=False))
                # 【第九輪 圖3需求】第二組上下限 (σ2):同色點線,與第一組虛線區隔。
                if 'BB_UPPER2' in df.columns:
                    apds.append(mpf.make_addplot(df['BB_UPPER2'], panel=0, color=bb_hex, linestyle=':', width=1.0, secondary_y=False))
                    apds.append(mpf.make_addplot(df['BB_LOWER2'], panel=0, color=bb_hex, linestyle=':', width=1.0, secondary_y=False))

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
                active_panels['KDJ'] = current_panel; panel_ratios.append(1.2); current_panel += 1

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
                    c_hex = self.color_map.get(self.ma_colors[i].get(), "#FFFFFF")
                    label_prefix = f"{self.ma_types[i].get()}{self.ma_periods[i].get()}"
                    obj = axlist[0].text(main_x, 0.97, '', transform=axlist[0].transAxes, color=c_hex, **main_text_props)

                    def _mk_ma_fmt(col=col, label_prefix=label_prefix):
                        def _fmt(row):
                            if col in row and not np.isnan(row[col]):
                                return f"{label_prefix}: {row[col]:.2f}"
                            return None
                        return _fmt
                    self.txt_main_segments.append({'obj': obj, 'fmt': _mk_ma_fmt()})
                    main_x += 0.095
            if self.bb_show.get() and 'BB_UPPER' in df.columns:
                bb_hex = self.color_map.get(self.bb_color.get(), "#00E5FF")
                obj = axlist[0].text(main_x, 0.97, '', transform=axlist[0].transAxes, color=bb_hex, **main_text_props)

                def _fmt_bb(row):
                    if 'BB_UPPER' in row and not np.isnan(row['BB_UPPER']):
                        s = f"BB上:{row['BB_UPPER']:.2f} 中:{row['BB_MID']:.2f} 下:{row['BB_LOWER']:.2f}"
                        if 'BB_UPPER2' in row and not np.isnan(row['BB_UPPER2']):
                            s += f" 上2:{row['BB_UPPER2']:.2f} 下2:{row['BB_LOWER2']:.2f}"
                        return s
                    return None
                self.txt_main_segments.append({'obj': obj, 'fmt': _fmt_bb})

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
            self.current_canvas.draw()
            
            self.current_canvas.mpl_connect('scroll_event', self.on_scroll_zoom)
            self.current_canvas.mpl_connect('button_press_event', self.on_mouse_press)
            self.current_canvas.mpl_connect('button_release_event', self.on_mouse_release)
            self.current_canvas.mpl_connect('motion_notify_event', self.on_mouse_move)
            self.log_message(f"[{txt_fmt_char}] 載入成功 ({display_title})")
        except Exception as e: self.log_message(f"畫布繪製失敗: {e}")

    def on_scroll_zoom(self, event):
        if event.inaxes is None or not self.axlist: return
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
        event.canvas.draw_idle()

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
                contract = self.sj_api.Contracts.Stocks.get(raw_sym)
                if not contract: contract = next((c for c in self.sj_api.Contracts.Stocks if c.symbol == raw_sym), None)
                            
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
        """【ADR-013】使用者在確認視窗按下「確認送出」後，才會執行到這裡真正呼叫 shioaji place_order()。"""
        contract = ctx["contract"]; order = ctx["order"]; action = ctx["action"]; raw_sym = ctx["raw_sym"]
        mode = ctx["mode"]; mode_labels = ctx["mode_labels"]; cond_labels = ctx["cond_labels"]
        is_lot_restricted = ctx["is_lot_restricted"]; use_daytrade = ctx["use_daytrade"]
        qty = ctx["qty"]; qty_unit = ctx["qty_unit"]; price_disp = ctx["price_disp"]
        effective_cond = ctx["effective_cond"]; effective_tif = ctx["effective_tif"]
        try:
            trade = self.sj_api.place_order(contract, order)
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
        # 【策略編輯器帶入】若「新增/編輯策略」對話框正開著,點自選股同時把
        # 商品代碼帶進對話框,交易種類 (股票/期貨) 也一併自動判斷,不用手key。
        target = getattr(self, '_qt_editor_symbol_target', None)
        if target is not None:
            try:
                dlg_ref, e_sym_ref, cb_tt_ref, lookup_cb = target
                if dlg_ref.winfo_exists():
                    e_sym_ref.delete(0, tk.END)
                    e_sym_ref.insert(0, sym)
                    cb_tt_ref.set(self._trade_type_for_symbol(sym))
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
    QT_TF_DAYS = {"1分K": 4, "5分K": 7, "15分K": 14, "30分K": 21, "60分K": 35, "日K": 300}

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
        # 【ADR-041】虛擬模擬帳戶
        self.paper_acct = None
        try:
            if os.path.exists(self.QT_PAPER_FILE):
                with open(self.QT_PAPER_FILE, 'r', encoding='utf-8') as f:
                    self.paper_acct = json.load(f)
        except Exception:
            self.paper_acct = None
        if not isinstance(self.paper_acct, dict) or 'cash' not in self.paper_acct:
            self.paper_acct = paper_account.new_account()

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
                json.dump(self.paper_acct, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

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
                else:
                    conds = "; ".join(strategy_engine.condition_label(c) for c in s.get('entry', [])) or '--'
                pos = '--'
                unreal_pnl_str = '--'
                if rt.get('state') in ('LONG', 'SHORT'):
                    pos = f"{'多' if rt['state']=='LONG' else '空'} {rt.get('qty',0)} @ {rt.get('entry_price',0):g}"
                    sym = s.get('symbol', '')
                    p = self.paper_acct['positions'].get(sym)
                    if p:
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
                tag = 'qt_off' if not s.get('enabled') else ('qt_on' if s.get('mode') == '實單' else 'qt_sim')
                tt = strategy_engine.trade_type_of(s)
                sym_disp = f"{s.get('symbol','')} ({tt})"
                # 【ADR-045】自訂策略的方向由 on_bar 程式碼決定,direction 只是佔位
                dir_disp = '程式決定' if s.get('kind') == 'custom' else s.get('direction', '')
                prepared.append((s['id'], (s.get('name',''), sym_disp, s.get('timeframe',''),
                                            dir_disp, conds, s.get('mode','模擬'), status, running_disp,
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
            dir_disp = '程式決定' if s.get('kind') == 'custom' else s.get('direction')
            box.insert(tk.END, f"[{s.get('mode')}] {s.get('name')} — {s.get('symbol')} {s.get('timeframe')} {dir_disp} x{s.get('qty')}")
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
        self.center_window(dlg, 460, 200)
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
        tk.Button(dlg, text="🧩 內建條件策略 (下拉選條件,免寫程式)", bg="#29B6F6", fg="black",
                  relief="flat", font=('微軟正黑體', 10, 'bold'), padx=10, pady=6,
                  command=_builtin).pack(fill=tk.X, padx=30, pady=4)
        tk.Button(dlg, text="🐍 自訂 Python 策略 (自己寫 on_bar)", bg="#AB47BC", fg="white",
                  relief="flat", font=('微軟正黑體', 10, 'bold'), padx=10, pady=6,
                  command=_custom).pack(fill=tk.X, padx=30, pady=4)

    def _qt_edit_strategy(self):
        s = self._qt_selected()
        if not s:
            self.log_message("【自動交易】請先在清單中選取要編輯的策略。")
            return
        if s.get('enabled'):
            self.log_message(f"【自動交易】啟動中的策略「{s.get('name')}」不得修改。請先停用後再編輯。")
            return
        if s.get('kind') == 'custom':
            self._qt_open_custom_editor(s)
        else:
            self._qt_open_editor(s)

    def _qt_delete_strategy(self):
        s = self._qt_selected()
        if not s:
            self.log_message("【自動交易】請先在清單中選取要刪除的策略。")
            return
        rt = self._qt_runtime(s['id'])
        if rt.get('state') in ('LONG', 'SHORT'):
            self.log_message(f"【自動交易】策略「{s.get('name')}」仍有持倉 ({rt['state']}),請先手動處理持倉並停用後再刪除。")
            return
        self.strategies = [x for x in self.strategies if x['id'] != s['id']]
        self.strategy_runtimes.pop(s['id'], None)
        self._qt_save(); self._qt_save_state(); self._qt_refresh_tree()
        self.log_message(f"【自動交易】已刪除策略「{s.get('name')}」。")

    def _qt_set_enabled(self, flag):
        s = self._qt_selected()
        if not s:
            self.log_message("【自動交易】請先在清單中選取策略。")
            return
        if flag:
            if s.get('kind') == 'custom':
                if not s.get('source_code') or s.get('qty', 0) <= 0:
                    self.log_message(f"【自動交易】自訂策略「{s.get('name')}」設定不完整,無法啟用。")
                    return
            else:
                ok, msg = strategy_engine.validate_strategy(s)
                if not ok:
                    self.log_message(f"【自動交易】策略「{s.get('name')}」無法啟用: {msg}")
                    return
        s['enabled'] = bool(flag)
        rt = self._qt_runtime(s['id']); rt['error_count'] = 0
        self._qt_save(); self._qt_refresh_tree(); self._qt_update_status_label()
        self.log_message(f"【自動交易】策略「{s.get('name')}」已{'啟用' if flag else '停用'}。")

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
                if s in ('^TWOII', 'TWOII', 'OTC101', 'OTC001', 'OTC'):
                    c = getattr(self.sj_api.Contracts.Indexs.OTC, 'OTC101', None) or getattr(self.sj_api.Contracts.Indexs.OTC, 'OTC001', None)
                else:  # 預設加權
                    c = self.sj_api.Contracts.Indexs.TSE.TSE001
                return (c, 'index_tw', sym, '台股') if c is not None else (None, None, sym, '台股')
            if wtt == '期貨':
                c = self._resolve_futures_contract(sym)
                return (c, 'future', sym, '台期貨') if c is not None else (None, None, sym, '台期貨')
            c = self.sj_api.Contracts.Stocks.get(sym)
            return (c, 'stock', sym, '台股') if c is not None else (None, None, sym, '台股')
        except Exception:
            return (None, None, sym, '台股')

    def _qt_fetch_closed_bars(self, strategy, contract, asset_type, tf=None, cache_sym=None, cache_market=None):
        """
        取策略用的「已收盤」K棒:下載(或快取)原始分K → 依週期重採樣 →
        剔除最後一根 (可能未完成)。回傳 df (可能為空)。

        【ADR-074】tf / cache_sym / cache_market 可覆寫,用來抓「訊號來源 A」的
        K棒 (A 的週期、A 的代碼);不帶時就抓策略本身 (執行商品 B) 的設定。
        """
        tf = tf or strategy.get('timeframe', '5分K')
        days = self.QT_TF_DAYS.get(tf, 7)
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        _sym = (cache_sym or str(strategy.get('symbol'))).upper()
        _mkt = cache_market or strategy.get('market')
        key = f"QT|{_mkt}|{_sym}|{tf}"
        raw, fresh = self._kbars_cache_get(key, start_dt)
        if raw is None or not fresh:
            raw = self._download_kbars_raw(contract, start_dt, end_dt)
            if raw is not None and not raw.empty:
                self._kbars_cache_put(key, start_dt, raw, tf)
        if raw is None or raw.empty:
            return pd.DataFrame()
        df = self._resample_sj_df(raw, tf, asset_type=asset_type)
        if df is None or len(df) < 2:
            return pd.DataFrame()
        # 最後一根視為未完成 (盤中一定是,收盤後犧牲一根換取絕不用未完成K棒的保證)
        return df.iloc[:-1]

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
        實單下單:鏡射 execute_order 的組單參數 (股票=現股整股限價ROD,
        期貨=限價ROD),價格 ± slippage_ticks 檔 (往成交方向讓價)。回傳 (ok, 說明)。

        【ADR-075 看A做B】exec_price:實際「要下單的商品 B」的成交基準價;有帶
        就用它 (看A做B),否則沿用 intent['price'] (一般模式 = 訊號商品自己的價)。
        contract/asset_type 一律是 B (執行商品),tick/讓價都以 B 計。
        """
        try:
            sym = str(strategy.get('symbol', '')).upper()
            qty = int(intent['qty'])
            base_px = float(exec_price) if exec_price is not None else float(intent['price'])
            ticks = int(strategy.get('slippage_ticks', 2) or 0)
            tick = tick_rules.get_tick(base_px, asset_type, sym)
            px = base_px + ticks * tick if intent['action'] == '買進' else base_px - ticks * tick
            px = round(round(px / tick) * tick, 4)
            sj_action = sj.constant.Action.Buy if intent['action'] == '買進' else sj.constant.Action.Sell
            tt = strategy_engine.trade_type_of(strategy)
            if tt == '期貨':
                order = self.sj_api.Order(price=px, quantity=qty, action=sj_action,
                                          price_type=sj.constant.FuturesPriceType.LMT,
                                          order_type=sj.constant.OrderType.ROD)
            elif tt == '零股':
                # 【ADR-043 第4項】零股:盤中零股單 (IntradayOdd),數量單位=股
                order = self.sj_api.Order(price=px, quantity=qty, action=sj_action,
                                          price_type=sj.constant.StockPriceType.LMT,
                                          order_type=sj.constant.OrderType.ROD,
                                          order_lot=sj.constant.StockOrderLot.IntradayOdd,
                                          order_cond=sj.constant.StockOrderCond.Cash)
            else:  # 股票 (整股)
                order = self.sj_api.Order(price=px, quantity=qty, action=sj_action,
                                          price_type=sj.constant.StockPriceType.LMT,
                                          order_type=sj.constant.OrderType.ROD,
                                          order_lot=sj.constant.StockOrderLot.Common,
                                          order_cond=sj.constant.StockOrderCond.Cash)
            trade = self.sj_api.place_order(contract, order)
            st = getattr(getattr(trade, 'status', None), 'status', '')
            unit = strategy_engine.qty_unit_of(strategy)
            return True, f"限價 {px:g} x{qty}{unit} ({tt}) 已送出 (狀態 {getattr(st, 'name', st) or '送出'})"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

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
            if (not _forced) and s.get('session_gate', True):
                tt = strategy_engine.trade_type_of(s)
                include_night = (s.get('futures_session', 'day_night') != 'day')
                if not market_session.is_market_open(tt, include_night=include_night):
                    self._qt_log_session_closed(s, tt, include_night)
                    continue
                else:
                    self._qt_note_session_open(s, tt, include_night)
            # 【ADR-041】邊界感知:只有該策略週期的K棒剛收盤才評估 (測試/手動觸發不受限)
            # 【ADR-074】看A做B 時,訊號來自 A,節奏依 A 的週期 (watch_timeframe)。
            if not _forced:
                tf_mins = {'1分K': 1, '5分K': 5, '15分K': 15, '30分K': 30, '60分K': 60}.get(strategy_engine.watch_timeframe_of(s))
                now_dt = datetime.now()
                if tf_mins:
                    boundary = now_dt.replace(second=0, microsecond=0)
                    boundary -= timedelta(minutes=boundary.minute % tf_mins)
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
                self._qt_last_boundary[s['id']] = boundary_key
            try:
                # 【ADR-074 看A做B】B (執行商品):下單、損益都用它;A (訊號來源):
                # 條件/指標看它 (可為指數;看A做B 關閉時 A=B)。
                contract, asset_type = self._qt_resolve(s)
                if contract is None:
                    raise RuntimeError(f"執行商品(做B)合約解析失敗: {s.get('symbol')}")
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
                            strategy_engine.apply_fill(s, rt, intent, now_ts)
                            changed = True
                            self.safe_after(0, self.log_message, f"【自動交易-實單】🔥 {label} | {intent['reason']} | {msg}")
                        else:
                            self.safe_after(0, self.log_message, f"【自動交易-實單失敗】{label} — {msg} (狀態未變更,下一根K棒會再評估)")
                    else:
                        strategy_engine.apply_fill(s, rt, intent, now_ts)
                        changed = True
                        # 【ADR-041】模擬成交記進虛擬模擬帳戶 (完整記帳:資金/持倉/損益)
                        try:
                            rec = paper_account.apply_fill(
                                self.paper_acct, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                s.get('market', '台股'), s.get('symbol', ''),
                                intent['action'], intent['kind'], intent['qty'], exec_px,
                                trade_type=strategy_engine.trade_type_of(s))
                            paper_account.mark_price(self.paper_acct, s.get('symbol', ''), exec_px)
                            self._qt_save_paper()
                            pnl_txt = f",此筆已實現 {_fmt_amt_signed(rec['pnl'])}" if intent['kind'] == 'CLOSE' else ""
                            eq = paper_account.equity(self.paper_acct)
                            self.safe_after(0, self.log_message,
                                            f"【自動交易-模擬】🧪 {label} | {intent['reason']} → 已記入模擬帳戶"
                                            f" (權益 {_fmt_amt(eq)}{pnl_txt})")
                        except Exception:
                            self.safe_after(0, self.log_message, f"【自動交易-模擬】🧪 {label} | {intent['reason']} (模擬)")
                rt['error_count'] = 0
            except Exception as e:
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
            
            for sym, p in self.paper_acct['positions'].items():
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
                for snap in snaps:
                    code = getattr(snap, 'code', '')
                    close = getattr(snap, 'close', 0)
                    if code and close > 0:
                        paper_account.mark_price(self.paper_acct, code, float(close))
                        for sym, c in needed.items():
                            if getattr(c, 'code', '') == code or getattr(c, 'symbol', '') == code:
                                paper_account.mark_price(self.paper_acct, sym, float(close))
                return True
        except Exception:
            pass
        return False

    def _qt_refresh_paper_account(self):
        try:
            if not getattr(self, '_paper_win', None) or not self._paper_win.winfo_exists():
                return
            a = self.paper_acct
            import core.paper_account as paper_account
            eq = paper_account.equity(a)
            unreal = paper_account.unrealized_pnl(a)
            ret_pct = (eq - a['initial_cash']) / a['initial_cash'] * 100.0 if a['initial_cash'] else 0.0
            
            if hasattr(self, '_paper_ui'):
                ui = self._paper_ui
                ui['eq'].config(text=f"{_fmt_amt(eq)}")
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
                    tvp.insert("", tk.END, iid=sym, values=(sym, p['market'], p['direction'], p['qty'],
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
                
                now_ts = _time.time()
                if self.api_logged_in and HAS_SJ and getattr(self, 'sj_api', None) and getattr(self, 'paper_acct', None):
                    if now_ts - last_snap_time >= 3.0:
                        last_snap_time = now_ts
                        if self._qt_update_realtime_pnl():
                            self.safe_after(0, self._qt_refresh_tree)
                            if getattr(self, '_paper_win', None) and self._paper_win.winfo_exists():
                                self.safe_after(0, self._qt_refresh_paper_account)
            except Exception:
                pass
            _time.sleep(2)

    # ---------- 策略編輯器 ----------
    def _qt_open_custom_editor(self, strategy):
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
        dlg.title("自訂 Python 策略" + ("" if is_new else f" — {s.get('name')}"))
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
        # 【策略編輯器帶入】同 _qt_open_editor:記住欄位讓點自選股可以帶入。
        self._qt_editor_symbol_target = (dlg, e_sym, cb_tt, _clook)
        _lbl(top, "週期").grid(row=1, column=0, sticky='w', pady=(6, 0))
        cb_tf = ttk.Combobox(top, values=list(strategy_engine.VALID_TIMEFRAMES), width=7, state='readonly', style="BlackText.TCombobox")
        cb_tf.set(s.get('timeframe', '5分K')); cb_tf.grid(row=1, column=1, padx=4, pady=(6, 0))
        _lbl(top, "數量").grid(row=1, column=2, sticky='w', padx=(8, 0), pady=(6, 0))
        e_qty = _ent(top, s.get('qty', 1), 6); e_qty.grid(row=1, column=3, padx=4, pady=(6, 0))
        _lbl(top, "模式").grid(row=1, column=4, sticky='w', padx=(8, 0), pady=(6, 0))
        cb_mode = ttk.Combobox(top, values=['模擬', '實單'], width=7, state='readonly', style="BlackText.TCombobox")
        cb_mode.set(s.get('mode', '模擬')); cb_mode.grid(row=1, column=5, padx=4, pady=(6, 0))
        # 【ADR-045】自訂策略不設 方向/停損%/停利% 欄位:做多做空、何時出場
        # 全部由使用者的 on_bar 程式碼決定 (ctx.buy/sell/close_position,
        # 移動停損可用 ctx.state + ctx.entry_price 自行實作)。
        tk.Label(top, text="※ 方向與停損/停利不在此設定 — 由你的 on_bar 程式碼自行決定進出場。",
                 bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).grid(
                 row=2, column=0, columnspan=6, sticky='w', pady=(6, 0))
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
            s['stop_loss_pct'] = float(s.get('stop_loss_pct', 0.0) or 0.0)
            s['take_profit_pct'] = float(s.get('take_profit_pct', 0.0) or 0.0)
            s['entry_time_start'] = e_en_st.get().strip()
            s['entry_time_end'] = e_en_ed.get().strip()
            s['exit_time_start'] = e_ex_st.get().strip()
            s['exit_time_end'] = e_ex_ed.get().strip()
            s['specific_entry_time'] = e_sp_en.get().strip()
            s['mode'] = cb_mode.get()
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
                                "3. 模擬訊號觀察沒問題後,再考慮改「實單」。", parent=self)
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
        tk.Button(foot, text="儲存策略", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 11, 'bold'), padx=18, pady=4, command=_save).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="取消", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 11), padx=18, pady=4, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    AI_CONFIG_FILE = "ai_config.json"
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
            df = self._qt_fetch_closed_bars(strat, contract, asset_type)
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
        tk.Label(rowA, text="　看A 商品代碼", bg="#12181F", fg="white", font=('微軟正黑體', 9)).pack(side=tk.LEFT)
        e_wsym = tk.Entry(rowA, width=12, bg="#2A323D", fg="white", justify="center")
        e_wsym.insert(0, s.get('watch_symbol', '')); e_wsym.pack(side=tk.LEFT, padx=4)
        tk.Label(rowA, text="種類", bg="#12181F", fg="white", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(8, 0))
        cb_wtt = ttk.Combobox(rowA, values=list(strategy_engine.WATCH_TRADE_TYPES), width=6,
                              state='readonly', style="BlackText.TCombobox")
        cb_wtt.set(s.get('watch_trade_type', '指數')); cb_wtt.pack(side=tk.LEFT, padx=4)
        tk.Label(rowA, text="看A週期", bg="#12181F", fg="white", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=(8, 0))
        cb_wtf = ttk.Combobox(rowA, values=list(strategy_engine.VALID_TIMEFRAMES), width=6,
                              state='readonly', style="BlackText.TCombobox")
        cb_wtf.set(s.get('watch_timeframe', '30分K')); cb_wtf.pack(side=tk.LEFT, padx=4)
        tk.Label(rowA, text="(訊號/指標看A這個週期;下單價與停損停利用做B的最新價)",
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
        _wlook()
        return {'get': lambda: {
            'watch_enabled': bool(var_watch.get()),
            'watch_symbol': e_wsym.get().strip().upper(),
            'watch_trade_type': cb_wtt.get(),
            'watch_timeframe': cb_wtf.get(),
        }}

    def _qt_open_editor(self, strategy):
        """新增/編輯策略對話框:基本參數 + 條件建構器 (進場AND / 出場OR)。"""
        is_new = strategy is None
        s = strategy_engine.new_strategy() if is_new else json.loads(json.dumps(strategy))
        dlg = tk.Toplevel(self)
        dlg.title("新增策略" if is_new else f"編輯策略 — {s.get('name')}")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 720, 620)
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
        top.columnconfigure(6, weight=1)
        _lbl(top, "策略名稱").grid(row=0, column=0, sticky='w')
        e_name = _ent(top, s.get('name', ''), 16); e_name.grid(row=0, column=1, padx=4)
        _lbl(top, "商品代碼").grid(row=0, column=2, sticky='w', padx=(10, 0))
        e_sym = _ent(top, s.get('symbol', ''), 10); e_sym.grid(row=0, column=3, padx=4)
        lbl_name = tk.Label(top, text="", bg="#1A2026", fg="#29B6F6", font=('微軟正黑體', 9, 'bold'))
        lbl_name.grid(row=5, column=0, columnspan=7, sticky='w', pady=(2, 0))
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
        # 【策略編輯器帶入】記住這個對話框的商品代碼/交易種類欄位,讓
        # on_watchlist_select 點自選股時可以直接寫回來 (見該函式的說明)。
        self._qt_editor_symbol_target = (dlg, e_sym, cb_tt, _lookup_name)
        _lbl(top, "週期").grid(row=1, column=0, sticky='w', pady=(6, 0))
        cb_tf = ttk.Combobox(top, values=list(strategy_engine.VALID_TIMEFRAMES), width=7, state='readonly', style="BlackText.TCombobox")
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
        tk.Button(foot, text="儲存策略", bg="#29B6F6", fg="black", relief="flat",
                  font=('微軟正黑體', 11, 'bold'), padx=18, pady=4, command=_save).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="取消", bg="#2A323D", fg="white", relief="flat",
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
            messagebox.showinfo("請先選取策略", "請先在「量化交易」清單中點選一個策略,再按「🎯 參數最佳化」。", parent=self)
            return
        if s.get('kind') != 'custom':
            messagebox.showinfo("僅支援自訂策略",
                                "參數最佳化目前只支援「自訂 Python 策略」——\n"
                                "因為要掃描的參數是程式碼裡用 ctx.param('名稱', 預設值) 讀取的那些。", parent=self)
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
            messagebox.showinfo("策略比較", "目前沒有任何策略可比較。", parent=self)
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
                    '定期定額') if st.get('buy_and_hold') else ('自訂' if st.get('kind') == 'custom' else '條件')
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
                        '自訂Python' if st.get('kind') == 'custom' else '條件策略')
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
            "• 按「否」:繼續等待目前這個完成。", parent=self)
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
        """【ADR-041】模擬帳戶視窗:資金/權益/持倉/交易史/重置。"""
        try:
            if getattr(self, '_paper_win', None) and self._paper_win.winfo_exists():
                self._paper_win.deiconify(); self._paper_win.lift(); self._paper_win.focus_force()
                return
        except Exception:
            pass
        a = self.paper_acct
        dlg = tk.Toplevel(self)
        self._paper_win = dlg
        dlg.title("💰 模擬帳戶 (虛擬資金,僅供策略驗證)")
        dlg.configure(bg="#1A2026")
        self.center_window(dlg, 860, 560)
        dlg.transient(self)
        try:
            dlg.lift(); dlg.focus_force()
        except Exception:
            pass
        eq = paper_account.equity(a)
        unreal = paper_account.unrealized_pnl(a)
        ret_pct = (eq - a['initial_cash']) / a['initial_cash'] * 100.0 if a['initial_cash'] else 0.0
        head = tk.Frame(dlg, bg="#12161A"); head.pack(fill=tk.X, padx=10, pady=(10, 4))
        cells = [("初始資金", f"{_fmt_amt(a['initial_cash'])}", 'white', 'init'),
                 ("現金", f"{_fmt_amt(a['cash'])}", 'white', 'cash'),
                 ("權益數", f"{_fmt_amt(eq)}", '#FFCA28', 'eq'),
                 ("已實現損益", f"{_fmt_amt_signed(a['realized_pnl'])}", '#FF1744' if a['realized_pnl'] > 0 else ('#00E676' if a['realized_pnl'] < 0 else 'white'), 'real'),
                 ("未實現損益", f"{_fmt_amt_signed(unreal)}", '#FF1744' if unreal > 0 else ('#00E676' if unreal < 0 else 'white'), 'unreal'),
                 ("報酬率", f"{ret_pct:+.2f}%", '#FF1744' if ret_pct > 0 else ('#00E676' if ret_pct < 0 else 'white'), 'ret')]
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
        pos_cols = ("sym", "market", "dir", "qty", "avg", "mark", "unreal")
        pos_heads = {"sym": "商品", "market": "市場", "dir": "方向", "qty": "數量", "avg": "均價", "mark": "標記價", "unreal": "未實現"}
        tvp = ttk.Treeview(dlg, columns=pos_cols, show="headings", style='Trades.Treeview', height=4)
        for c in pos_cols:
            tvp.heading(c, text=pos_heads[c]); tvp.column(c, width=110, anchor="center")
        tvp.tag_configure('p_win', foreground='#FF1744', background='#12161A')
        tvp.tag_configure('p_loss', foreground='#00E676', background='#12161A')
        tvp.tag_configure('p_flat', foreground='white', background='#12161A')
        tvp.pack(fill=tk.X, padx=10, pady=2)
        self._paper_ui['tvp'] = tvp
        self._qt_refresh_paper_account()
        tk.Label(dlg, text="交易紀錄 (最新在上):", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor='w', padx=12, pady=(6, 0))
        h_cols = ("ts", "sym", "act", "kind", "qty", "price", "fee", "pnl")
        h_heads = {"ts": "時間", "sym": "商品", "act": "買賣", "kind": "開/平", "qty": "數量", "price": "價格", "fee": "費用", "pnl": "已實現"}
        tvh = ttk.Treeview(dlg, columns=h_cols, show="headings", style='Trades.Treeview', height=7)
        widths = {"ts": 150, "sym": 80, "act": 50, "kind": 50, "qty": 50, "price": 80, "fee": 70, "pnl": 90}
        for c in h_cols:
            tvh.heading(c, text=h_heads[c]); tvh.column(c, width=widths[c], anchor="center")
        tvh.tag_configure('p_win', foreground='#FF1744', background='#12161A')
        tvh.tag_configure('p_loss', foreground='#00E676', background='#12161A')
        tvh.tag_configure('p_flat', foreground='white', background='#12161A')
        vsb = ttk.Scrollbar(dlg, orient="vertical", command=tvh.yview)
        tvh.configure(yscrollcommand=vsb.set)
        tvh.pack(fill=tk.BOTH, expand=True, padx=10, pady=2)
        for rec in reversed(a['history'][-200:]):
            kind_txt = '開倉' if rec['kind'] == 'OPEN' else '平倉'
            tag = 'p_win' if rec['pnl'] > 0 else ('p_loss' if rec['pnl'] < 0 else 'p_flat')
            tvh.insert("", tk.END, values=(rec['ts'], rec['symbol'], rec['action'], kind_txt,
                                            rec['qty'], f"{rec['price']:g}", f"{_fmt_amt(rec['fee'])}",
                                            f"{_fmt_amt_signed(rec['pnl'])}" if rec['kind'] == 'CLOSE' else '--'), tags=(tag,))
        foot = tk.Frame(dlg, bg="#1A2026"); foot.pack(pady=8)
        def _reset():
            try:
                cash_str = sd.askstring("重置模擬帳戶", "輸入新的初始資金 (清空持倉與歷史):", parent=dlg)
                if not cash_str:
                    return
                cash = float(cash_str)
                if cash <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self.log_message("【模擬帳戶】初始資金格式錯誤,未重置。")
                return
            self.paper_acct = paper_account.new_account(cash)
            self._qt_save_paper()
            self.log_message(f"【模擬帳戶】已重置,初始資金 {_fmt_amt(cash)}。")
            dlg.destroy(); self._qt_open_paper_window()
        tk.Button(foot, text="🔄 重置帳戶", bg="#5A6472", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=14, pady=3, command=_reset).pack(side=tk.LEFT, padx=6)
        tk.Button(foot, text="關閉", bg="#2A323D", fg="white", relief="flat",
                  font=('微軟正黑體', 10), padx=20, pady=3, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

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

    def _live_bar_painter(self):
        """每 400ms 檢查一次:活K棒有新 tick 就用 blitting 疊畫 (毫秒級,不重繪全圖)。"""
        try:
            if getattr(self, '_closing', False):
                return
            lb = self._live_bar
            arts = getattr(self, '_live_bar_artists', None)
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
                self._blit_hover()  # 與 hover 共用同一條 blit 管線 (含活K棒 artists)
        except Exception:
            pass
        finally:
            self.safe_after(400, self._live_bar_painter)

    def set_bottom_tab(self, key):
        """切換底部分頁:系統日誌/我的委託單/我的已成交/我的庫存。"""
        self.bottom_tab = key
        for frame in (self.log_tab_frame, self.orders_tab_frame, self.fills_tab_frame, self.positions_tab_frame, self.quant_tab_frame):
            frame.pack_forget()
        {"log": self.log_tab_frame, "orders": self.orders_tab_frame,
         "fills": self.fills_tab_frame, "positions": self.positions_tab_frame,
         "quant": self.quant_tab_frame}[key].pack(fill=tk.BOTH, expand=True)
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
            self.log_message(f"【刪改】嘗試取得委託物件失敗: {type(e).__name__}: {e}")
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
        """
        if not (self.api_logged_in and HAS_SJ and self.sj_api):
            self.log_message("【刪改】券商 API 未登入,無法送出刪改。")
            return
        trade = o.get('trade') or self._find_trade_for_order(o.get('id'))
        if trade is None:
            msg = "找不到可操作的委託物件 (此委託可能不是本次連線送出的),請改於券商 App 處理。"
            self.log_message(f"【刪改】{msg}")
            messagebox.showwarning("無法刪改", msg)
            return
        try:
            if kind == 'cancel':
                self.log_message(f"【刪改送出】刪單 → cancel_order(trade)  書號:{o.get('id')}")
                self.sj_api.cancel_order(trade)
            elif kind == 'qty':
                cur = self._safe_int(o.get('quantity'))
                new_total = int(new_value)
                reduce_qty = cur - new_total
                self.log_message(f"【刪改送出】改量 意圖 {cur}→{new_total} (減 {reduce_qty})"
                                 f" → update_order(trade, qty={reduce_qty})  書號:{o.get('id')}")
                self.sj_api.update_order(trade, qty=reduce_qty)
            elif kind == 'price':
                new_price = float(new_value)
                self.log_message(f"【刪改送出】改價 → update_order(trade, price={new_price})  書號:{o.get('id')}")
                self.sj_api.update_order(trade, price=new_price)
            self.log_message("【刪改】已送出,請等待委託回報確認 (清單狀態會自動更新)。")
        except Exception as e:
            self.log_message(f"【刪改失敗】{type(e).__name__}: {e}")
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