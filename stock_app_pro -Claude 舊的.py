import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.simpledialog as sd
import threading
import time
import os
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib.pyplot as plt
import mplfinance as mpf
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from datetime import datetime, timedelta
from collections import deque
import gc  

# 【架構重構 ADR-009】純邏輯層抽出到 core/ 與 data/,詳見 DECISIONS.md。
# 這裡 import 進來的函式取代了原本寫在 StockTradingAppPro 類別內、
# 跟 tkinter/shioaji 完全無關的計算與檔案存取邏輯。
from core import tick_rules
from core import indicators as core_indicators
from core import futures_session
from core import order_rules
from data import config_store

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

class StockTradingAppPro(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("XQ 旗艦全週期互動版 - 全 API 實盤五檔報價系統")
        self.geometry("1700x950") 
        self.configure(bg="#12161A") 

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
        # 盤後備援快照節流 (避免每 0.5 秒狂打 snapshots 吃光每日 API 流量配額)
        self.last_fallback_snap_time = 0
        self.odd_no_stream_warned = False
        
        if HAS_SJ: self.sj_api = sj.Shioaji(simulation=False) 
        
        self.config_file = "broker_config.json"
        self.wl_file = "watchlists.json"
        self.chart_layout_file = "chart_layout.json"
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
        threading.Thread(target=self.fetch_market_indices_worker, daemon=True).start()
        threading.Thread(target=self.fetch_realtime_worker, daemon=True).start() 

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
        if HAS_SJ and self.api_logged_in:
            try:
                self.sj_api.logout()
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
             實際呼叫包在 try/except TclError 裡——因為單靠第 1 層仍有極窄
             的競態窗口 (排程當下 _closing 還是 False，但真正執行前視窗
             已經被關閉/銷毀)。
        兩層都做，才能確保背景執行緒不會在使用者關閉視窗後讓程式印出
        'invalid command name' 這類未捕捉例外。

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
            except tk.TclError:
                pass
        try:
            return tk.Tk.after(self, delay, _wrapped, *args)
        except tk.TclError:
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
        tk.Label(top_panel, text="(台股ETF皆可 / 期貨輸入 TXF / 美股輸入代碼)", bg="#1A2026", fg="#8A99AD", font=('微軟正黑體', 9)).pack(side=tk.LEFT, padx=5)

        self.main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.main_pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_frame = tk.Frame(self.main_pane, bg="#12161A", width=240)
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
        
        self.listbox_wl = tk.Listbox(wl_box, bg="#12161A", fg="white", height=5, selectbackground="#29B6F6")
        self.listbox_wl.pack(fill=tk.X, pady=2)
        self.listbox_wl.bind("<<ListboxSelect>>", self.on_watchlist_select)
        self.listbox_wl.bind("<Delete>", lambda e: self.del_from_wl())
        
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

        tk.Label(qty_price_frame, text="  量:", bg="#1A2026", fg="white").pack(side=tk.LEFT, padx=(8, 0))
        btn_qty_minus = tk.Button(qty_price_frame, text="－", bg="#2A323D", fg="white", relief="flat", padx=2, pady=0, command=lambda: self.step_qty(-1))
        btn_qty_minus.pack(side=tk.LEFT, padx=1)
        self.entry_qty = tk.Entry(qty_price_frame, width=5, bg="#2A323D", fg="white", justify="center")
        self.entry_qty.insert(0, "1")
        self.entry_qty.pack(side=tk.LEFT, padx=1)
        btn_qty_plus = tk.Button(qty_price_frame, text="＋", bg="#2A323D", fg="white", relief="flat", padx=2, pady=0, command=lambda: self.step_qty(1))
        btn_qty_plus.pack(side=tk.LEFT, padx=1)
        self.lbl_qty_unit = tk.Label(qty_price_frame, text="張", bg="#1A2026", fg="#8A99AD")
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

        # --- 成交明細跳動列表 (ADR-008 新增) ---
        tk.Label(info_box, text="成交明細:", bg="#1A2026", fg="#FFCA28", font=('微軟正黑體', 9, 'bold')).pack(anchor="w", pady=(4,0))
        self.listbox_trade_feed = tk.Listbox(info_box, bg="#12161A", fg="white", height=5, font=('Courier New', 9), selectbackground="#2A323D", activestyle="none")
        self.listbox_trade_feed.pack(fill=tk.X, pady=2)

        # --- Quote 串接五檔報價區 (價格可點擊直接帶入下單價,ADR-008 新增) ---
        # 【ADR-013】移除 fill=tk.X,改用 pack 預設的 anchor='center':
        # 框架縮回內容的自然寬度並在 info_box 裡水平置中,而不是被拉伸貼齊左邊。
        five_level_frame = tk.Frame(info_box, bg="#1A2026")
        five_level_frame.pack(pady=2)
        
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
        for key, label in [("log", "系統日誌與回報"), ("orders", "我的委託單"), ("fills", "我的已成交")]:
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
        orders_cols = ("time", "code", "action", "price", "quantity", "filled", "status")
        orders_headings = {"time": "時間", "code": "商品", "action": "買賣", "price": "價格",
                            "quantity": "數量", "filled": "已成交", "status": "狀態"}
        self.tree_orders = ttk.Treeview(self.orders_tab_frame, columns=orders_cols, show="headings", height=5, style='Trades.Treeview')
        for c in orders_cols:
            self.tree_orders.heading(c, text=orders_headings[c])
            self.tree_orders.column(c, width=80, anchor="center")
        # 【第七輪】明確設定資料列前景色 tag,插入時逐列套用,雙保險確保列文字可見。
        self.tree_orders.tag_configure('visible_row', foreground='#FFFFFF', background='#12161A')
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

        self.bottom_tab = "log"
        self.set_bottom_tab("log")

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
        【使用者回報#3】判斷一個例外訊息看起來像不像「shioaji 連線階段已經斷了」。
        目前查證到的已知情境：同一個永豐金帳號如果在別處 (例如官網/App) 也登入
        了會佔用交易階段的連線，可能導致 API 這邊的連線被中斷，之後的呼叫會出現
        類似 'SessionNotEstablished'、'Session error'、'session ... established'
        這類字樣的例外。這不是我們程式的 bug，是券商後端的連線/風控機制，我們
        沒辦法阻止它發生，但至少可以偵測到這個情況，把畫面正確反映成「已斷線」，
        不要讓使用者誤以為 API 還連著。
        """
        msg = str(exc)
        keywords = ["SessionNotEstablished", "Session error", "session", "NotReady", "not ready", "connection error"]
        return any(k.lower() in msg.lower() for k in keywords)

    def _mark_session_dead(self, reason=""):
        """
        【使用者回報#3】偵測到連線階段疑似已經斷線時呼叫：把 api_logged_in 撥回
        False、更新登入按鈕與狀態列文字，並提示使用者最常見的成因 (同一帳號在
        官網/App 也登入了)。使用者仍然需要自己去確認/處理另一端的登入狀態，
        我們這裡只負責讓畫面誠實反映現狀，不要留著「已連線」的假象。
        """
        if not self.api_logged_in:
            return  # 已經是斷線狀態，不用重複處理
        self.api_logged_in = False
        self.safe_after(0, lambda: self.lbl_api_status.config(text="🔴 連線中斷 (請重新登入)", fg="#FF5252"))
        self.safe_after(0, lambda: self.btn_login.config(text="🔒 登入券商實盤 API", bg="#FF9100", fg="black"))
        self.safe_after(0, self.log_message,
                         "【連線中斷】偵測到券商連線階段疑似已斷線。最常見原因：同一組帳號同時"
                         "在永豐金官網或 App 登入 (許多券商規定同一帳號的交易階段同時間只能有一個"
                         "生效)。請確認沒有在其他地方登入同一帳號，登出該處後，回來這裡重新點擊"
                         "「登入券商實盤 API」。")

    def toggle_login(self):
        if self.api_logged_in:
            self.api_logged_in = False
            self.lbl_api_status.config(text="🔴 券商未連線", fg="#FF5252")
            self.btn_login.config(text="🔒 登入券商實盤 API", bg="#FF9100", fg="black")
            self.log_message("【系統】已中斷券商實盤連線。")
            if HAS_SJ:
                try: self.sj_api.logout()
                except: pass
        else:
            self.open_login_dialog()

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
            threading.Thread(target=self.process_broker_login, args=(api, sec, pid, ca_p, ca_pw), daemon=True).start()

        tk.Button(dlg, text="驗證憑證並連線", bg="#FF9100", fg="black", font=('微軟正黑體', 10, 'bold'), command=do_login).pack(pady=15)

    # ================= ✨ v1 串流監聽 (單軌架構,以官方 intraday_odd 欄位精準分流) =================
    # 【修正說明】原先同時註冊 v0 set_quote_callback 與 v1 callbacks,
    # v0 以 topic 字串 "ODD" 判斷零股極不可靠,常把整股訊息寫進零股暫存 (反之亦然),
    # 造成零股與五檔資料互相污染。現改為只用 v1 typed callbacks,
    # 直接讀取官方 tick/bidask 物件上的 intraday_odd 布林欄位分流,並加鎖確保執行緒安全。

    def on_tick_stk_v1(self, exchange, tick):
        try:
            if self.current_contract and tick.code == self.current_contract.code:
                is_odd = bool(getattr(tick, 'intraday_odd', False))
                with self.quote_lock:
                    if is_odd:
                        self.current_tick_odd = tick
                    else:
                        self.current_tick_normal = tick
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

    def on_tick_fop_v1(self, exchange, tick):
        try:
            if self.current_contract and tick.code == self.current_contract.code:
                with self.quote_lock:
                    self.current_tick_normal = tick
                try:
                    self._record_trade_tick(tick.close, getattr(tick, 'volume', 0), False)
                except Exception: pass
        except Exception: pass

    def on_bidask_fop_v1(self, exchange, bidask):
        try:
            if self.current_contract and bidask.code == self.current_contract.code:
                with self.quote_lock:
                    self.current_bidask_normal = bidask
        except Exception: pass

    def process_broker_login(self, api_key, secret_key, pid, ca_path, ca_pw):
        if not HAS_SJ: 
            self.safe_after(0, self.log_message, "【錯誤】未安裝 shioaji 套件！")
            return
        self.safe_after(0, self.log_message, "連線至券商伺服器並下載最新合約檔中...")
        try:
            self.sj_api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
            self.sj_api.activate_ca(ca_path=ca_path, ca_passwd=ca_pw, person_id=pid)
            
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
            self.safe_after(0, lambda: self.btn_login.config(text="🔓 登出券商 API", bg="#FF1744", fg="white"))
            self.safe_after(0, lambda: self.lbl_api_status.config(text="🟢 券商 API 已連線 (實盤模式)", fg="#00E676"))
            self.safe_after(0, self.log_message, "【登入成功】憑證驗證通過，合約下載完畢，實盤下單與即時五檔已啟用！")
            self.safe_after(1000, self.start_fetch_thread)
        except Exception as e:
            if self._looks_like_session_dead(e):
                self.safe_after(0, self.log_message, f"【API 登入或憑證失敗】: {e}")
                self.safe_after(0, self.log_message,
                                 "【提示】這個錯誤常見原因是同一組帳號同時在永豐金官網/App 登入，"
                                 "佔用了交易階段的連線名額。請先確認並登出官網/App 的登入，"
                                 "再回來重新點擊「登入券商實盤 API」。")
            else:
                self.safe_after(0, self.log_message, f"【API 登入或憑證失敗】: {e}")

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

    def open_main_settings(self):
        dlg = tk.Toplevel(self); dlg.title("主圖指標參數設定"); dlg.configure(bg="#1A2026"); self.center_window(dlg, 400, 350); dlg.transient(self); dlg.grab_set()      
        tk.Label(dlg, text="開關", bg="#1A2026", fg="white").grid(row=0, column=0, pady=10); tk.Label(dlg, text="類型", bg="#1A2026", fg="white").grid(row=0, column=1); tk.Label(dlg, text="週期", bg="#1A2026", fg="white").grid(row=0, column=2); tk.Label(dlg, text="色彩", bg="#1A2026", fg="white").grid(row=0, column=3)
        for i in range(6):
            tk.Checkbutton(dlg, text=f"MA{i+1}", variable=self.ma_shows[i], bg="#1A2026", fg="white", selectcolor="#2A323D").grid(row=i+1, column=0, sticky="w", padx=15, pady=2)
            ttk.Combobox(dlg, textvariable=self.ma_types[i], values=["SMA", "EMA", "WMA"], width=6, state="readonly", style="BlackText.TCombobox").grid(row=i+1, column=1, padx=5)
            tk.Entry(dlg, textvariable=self.ma_periods[i], width=5, bg="#2A323D", fg="white", justify="center").grid(row=i+1, column=2, padx=5)
            ttk.Combobox(dlg, textvariable=self.ma_colors[i], values=list(self.color_map.keys()), width=10, state="readonly", style="BlackText.TCombobox").grid(row=i+1, column=3, padx=5)
        ttk.Separator(dlg, orient='horizontal').grid(row=7, column=0, columnspan=4, sticky='ew', pady=15)
        tk.Checkbutton(dlg, text="布林通道 (BBands 20,2)", variable=self.bb_show, bg="#1A2026", fg="#00E5FF", selectcolor="#2A323D").grid(row=8, column=0, columnspan=2, sticky="w", padx=15)
        ttk.Combobox(dlg, textvariable=self.bb_color, values=list(self.color_map.keys()), width=10, state="readonly", style="BlackText.TCombobox").grid(row=8, column=3, padx=5)
        tk.Button(dlg, text="確認並套用", bg="#29B6F6", fg="black", font=('微軟正黑體', 10, 'bold'), command=lambda: [self.trigger_redraw(), dlg.destroy()]).grid(row=9, column=0, columnspan=4, pady=20)

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
        tk.Button(dlg, text="確認並套用", bg="#29B6F6", fg="black", font=('微軟正黑體', 10, 'bold'), command=lambda: [self.trigger_redraw(), dlg.destroy()]).pack(pady=10)

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
        return core_indicators.calculate_indicators(
            df, ma_flags, ma_types, ma_periods,
            bb_show=self.bb_show.get(), bbw_show=self.var_bbw.get(),
            macd_show=self.var_macd.get(), macd_f=self.macd_f.get(), macd_s=self.macd_s.get(), macd_sig=self.macd_sig.get(),
            rsi_show=self.var_rsi.get(), rsi_p=self.rsi_p.get(),
            kdj_show=self.var_kdj.get(), kd_n=self.kd_n.get(), kd_m1=self.kd_m1.get(), kd_m2=self.kd_m2.get(),
            dmi_show=self.var_dmi.get(), dmi_n=self.dmi_n.get(),
        )

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
                                time.sleep(30)
                                continue 
                    except Exception: pass 
                # 【ADR-011】YF 備援已移除:大盤指數也一律使用 shioaji，
                # 未登入時維持顯示初始的「等待連線API...」文字，不再退化成 YF 資料。
            except Exception: pass
            time.sleep(30)

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
            time.sleep(0.5)

    def start_fetch_thread(self):
        # 【ADR-011】移除「未登入且未開YF備援就整個擋下」的舊檢查:
        # 美股本來就不需要登入 shioaji (自動用 yfinance)；台股是否需要登入
        # 交給 fetch_data_worker 依商品類型判斷並給出對應的錯誤訊息，
        # 這裡不用先猜測使用者輸入的是哪種商品。
        raw_sym = self.entry_symbol.get().strip().upper()
        if not raw_sym: return
        self.saved_xlim = None 
        tf = self.timeframe_var.get()
        
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
        threading.Thread(target=self.fetch_data_worker, args=(raw_sym, tf), daemon=True).start()

    def trigger_redraw(self):
        if self.current_df is not None and self.axlist is not None:
            try: self.saved_xlim = self.axlist[0].get_xlim()
            except: pass
            self.draw_chart(self.current_df)
        else: self.start_fetch_thread()

    def _on_chart_frame_resize(self, event=None):
        """
        【使用者調整#1】chart_frame 尺寸改變時 (通常是使用者拖曳視窗邊框)
        重新用目前的實際像素尺寸繪製圖表，讓圖表持續填滿可用空間，
        不會停留在視窗剛啟動時的舊尺寸。用 debounce 避免拖曳過程中
        <Configure> 連續觸發造成頻繁重繪、畫面頓卡：每次觸發就取消
        前一個排程中的重繪，只在停止拖曳 300ms 後才真的重繪一次。
        """
        if self._resize_after_id is not None:
            try: self.after_cancel(self._resize_after_id)
            except Exception: pass
        self._resize_after_id = self.safe_after(300, self._debounced_resize_redraw)

    def _debounced_resize_redraw(self):
        self._resize_after_id = None
        if self.current_df is not None:
            self.trigger_redraw()

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
    def _resample_future_session(self, sj_df, tf, agg_dict):
        # 【ADR-009】交易日聚合的核心邏輯移到 core/futures_session.py (純函式,不吞例外)。
        # 這裡保留原本的例外處理與日誌記錄 + 自然日退回機制,因為這兩件事本質上是
        # GUI 層的關注點 (要不要跟使用者說一聲、要不要用比較不準的資料頂著繼續跑)。
        try:
            return futures_session.resample_future_session(sj_df, tf, agg_dict)
        except Exception as e:
            self.safe_after(0, self.log_message, f"【期貨交易日聚合異常】{e},退回自然日聚合 (可能不準確)")
            return futures_session.resample_natural_day_fallback(sj_df, tf, agg_dict)

    def fetch_data_worker(self, raw_sym, tf):
        """
        【ADR-011】資料源政策改版:
          - 台股 (股票/ETF/指數/期貨) 一律使用 shioaji，不再有 yfinance/FinMind 備援。
            未登入券商 API 時直接報錯並退出，不會安靜地退化成其他資料源。
          - 美股自動使用 yfinance (shioaji 本來就不支援美股)，不需要手動切換。
        """
        try:
            yf_params = {"1分K": ("5d", "1m"), "5分K": ("30d", "5m"), "15分K": ("30d", "15m"), "30分K": ("30d", "30m"), "60分K": ("90d", "60m"), "日K": ("10y", "1d"), "周K": ("10y", "1wk"), "月K": ("10y", "1mo")}
            period, interval = yf_params.get(tf, ("1y", "1d"))

            contract = None; search_sym = raw_sym; stock_name = ""
            is_tw = any(c.isdigit() for c in raw_sym) and not raw_sym.startswith('^')
            is_taiwan_instrument = is_tw or raw_sym in ("^TWII", "^TWOII", "TXF", "MTX", "FITX", "MXF")

            if is_taiwan_instrument:
                self.asset_type = "stock"  # 預設值,下面依實際合約類型覆蓋 (index_tw/future)
                self.data_source = ""

                if not (self.api_logged_in and HAS_SJ):
                    self.safe_after(0, self.log_message, f"【錯誤】{raw_sym} 是台股/期貨/指數，資料僅使用券商 shioaji API，請先登入券商實盤 API 再查詢。")
                    return

                try:
                    if raw_sym == "^TWII":
                        contract = self.sj_api.Contracts.Indexs.TSE.TSE001
                        if contract: search_sym = raw_sym; stock_name = "加權指數"; self.asset_type = "index_tw"
                    elif raw_sym == "^TWOII":
                        contract = getattr(self.sj_api.Contracts.Indexs.OTC, 'OTC101', None)
                        if not contract: contract = getattr(self.sj_api.Contracts.Indexs.OTC, 'OTC001', None)
                        if contract: search_sym = raw_sym; stock_name = "櫃買指數"; self.asset_type = "index_tw"
                    elif raw_sym in ['TXF', 'MTX', 'FITX', 'MXF']:
                        code = 'TXF' if raw_sym in ['TXF', 'FITX'] else 'MXF'
                        contract = getattr(self.sj_api.Contracts.Futures, code).get(f"{code}R1")
                        if contract: search_sym = contract.symbol; stock_name = contract.name; self.asset_type = "future"
                    else:
                        contract = self.sj_api.Contracts.Stocks.get(raw_sym)
                        if not contract: contract = next((c for c in self.sj_api.Contracts.Stocks if c.symbol == raw_sym), None)
                        if contract: search_sym = raw_sym; stock_name = contract.name; self.asset_type = "stock"
                except Exception:
                    pass

                if not contract:
                    self.safe_after(0, self.log_message, f"【錯誤】券商合約查無 {raw_sym}，請確認代碼是否正確 (股票/ETF代碼、TXF/MTX 期貨、^TWII/^TWOII 指數)。")
                    return

                # 【修正】明確指定 QuoteVersion.v1 並逐路訂閱:
                # 原寫法四路訂閱包在同一 try,任一路失敗 (常見於零股 BidAsk) 整批中斷且無記錄,
                # 導致「五檔/零股沒資料卻查不出原因」。現在每一路獨立訂閱並記 log。
                try:
                    if getattr(self, 'current_contract', None):
                        for odd_flag in [True, False]:
                            try: self.sj_api.quote.unsubscribe(self.current_contract, quote_type=sj.constant.QuoteType.Tick, intraday_odd=odd_flag)
                            except: pass
                            try: self.sj_api.quote.unsubscribe(self.current_contract, quote_type=sj.constant.QuoteType.BidAsk, intraday_odd=odd_flag)
                            except: pass

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
                    try:
                        self.current_day_trade = (str(getattr(contract, 'day_trade', '')) in ('Yes', 'DayTrade.Yes', 'DayTrade.Yes: Yes') or getattr(getattr(contract, 'day_trade', None), 'value', '') == 'Yes')
                    except Exception:
                        self.current_day_trade = False
                    try:
                        self.current_reference_price = float(getattr(contract, 'reference', 0) or 0)
                    except Exception:
                        self.current_reference_price = 0.0
                    # 【使用者調整#5】換新標的時,由系統依這檔股票是否開放現沖，重新決定
                    # 「現股當沖(先賣後買)」checkbox 的預設勾選狀態——可以現沖就預設打勾
                    # (使用者明確要求:點買進/賣出就會直接送出現沖單，不需要每次手動勾)；
                    # 不能現沖則預設不勾。這裡只是設「這檔新標的的起始值」，之後在同一檔
                    # 股票內使用者手動取消勾選，不會被 update_daytrade_checkbox_state()
                    # (在切換交易別/種類等操作時也會呼叫到) 覆蓋回去，那個函式只負責
                    # 鎖定/解鎖與「不合格時強制清空」，合格時不會動勾選狀態。
                    self.safe_after(0, lambda: self.daytrade_var.set(bool(self.current_day_trade)))
                    self.safe_after(0, self.update_daytrade_badge)

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
                    if self.asset_type == "stock":
                        ok_ot = _sub(sj.constant.QuoteType.Tick, True, "零股Tick")
                        ok_ob = _sub(sj.constant.QuoteType.BidAsk, True, "零股五檔")
                        self.safe_after(0, self.log_message, f"【訂閱結果】整股Tick:{'✓' if ok_t else '✗'} 整股五檔:{'✓' if ok_b else '✗'} 零股Tick:{'✓' if ok_ot else '✗'} 零股五檔:{'✓' if ok_ob else '✗'}")
                    else:
                        self.safe_after(0, self.log_message, f"【訂閱結果】Tick:{'✓' if ok_t else '✗'} 五檔:{'✓' if ok_b else '✗'}")
                except Exception as e:
                    self.safe_after(0, self.log_message, f"訂閱報價串流異常: {e}")
            else:
                # 美股:自動使用 yfinance,shioaji 本來就不支援美股。
                self.asset_type = "us_stock"
                self.data_source = ""

            df = pd.DataFrame()
            adjust_flag = self.var_adjusted.get()

            if is_taiwan_instrument:
                if adjust_flag and self.asset_type == "stock":
                    # 【ADR-011】還原權息目前只有 yfinance 的 auto_adjust 能做到,
                    # shioaji kbars 沒有內建還原權息機制。台股既然一律用 shioaji,
                    # 這個勾選對台股暫時不會生效,明確告知使用者,不要讓他誤以為已套用。
                    self.safe_after(0, self.log_message, "【提示】「還原權息」目前僅 yfinance 資料源支援；台股改用 shioaji 後此設定暫不生效，顯示為原始價格。")

                self.data_source = "shioaji"
                self.safe_after(0, self.log_message, f"⚡ 極速引擎：透過永豐金 API 抓取 {stock_name} 歷史 K 線...")
                sj_days = {"1分K": 5, "5分K": 30, "15分K": 60, "30分K": 60, "60分K": 90, "日K": 730, "周K": 1825, "月K": 3650}
                days = sj_days.get(tf, 180)
                end_dt = datetime.now(); start_dt = end_dt - timedelta(days=days)

                try:
                    kbars = self.sj_api.kbars(contract, start=start_dt.strftime("%Y-%m-%d"), end=end_dt.strftime("%Y-%m-%d"))
                    if kbars is not None:
                        sj_df = pd.DataFrame({**kbars})
                        if not sj_df.empty:
                            sj_df['ts'] = pd.to_datetime(sj_df['ts'])
                            if sj_df['ts'].dt.tz is not None: sj_df['ts'] = sj_df['ts'].dt.tz_convert('Asia/Taipei').dt.tz_localize(None)
                            sj_df.set_index('ts', inplace=True)
                            sj_df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume', 'amount': 'Amount'}, inplace=True)
                            if self.asset_type == "index_tw" and 'Amount' in sj_df.columns: sj_df['Volume'] = sj_df['Amount'] / 100000000

                            resample_map = {"1分K": '1min', "5分K": '5min', "15分K": '15min', "30分K": '30min', "60分K": '60min', "日K": 'D', "周K": 'W-MON', "月K": 'MS'}
                            rule = resample_map.get(tf)
                            if rule:
                                agg_dict = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
                                if tf in ["日K", "周K", "月K"]:
                                    if self.asset_type == "future":
                                        # 期貨:用「交易日(session date)」聚合,不能用 resample('D')。
                                        # resample('D') 依自然日 00:00 切割,會把當晚 15:00 起的夜盤混進當日日盤,
                                        # 導致開/收/高/低全錯 (實測日K收盤被夜盤污染)。
                                        sj_df = self._resample_future_session(sj_df, tf, agg_dict)
                                    else:
                                        # 股票/指數沒有夜盤,自然日 resample 正確
                                        sj_df = sj_df.resample(rule, label='left', closed='left').agg(agg_dict).dropna()
                                else:
                                    sj_df = sj_df.resample(rule, label='left', closed='left').agg(agg_dict).dropna()
                                    mins = {"1分K": 1, "5分K": 5, "15分K": 15, "30分K": 30, "60分K": 60}.get(tf, 0)
                                    if mins > 0: sj_df.index = sj_df.index + pd.Timedelta(minutes=mins)
                            df = sj_df
                except Exception as e:
                    self.safe_after(0, self.log_message, "【提示】無法取得歷史 K 線報價 (shioaji)。")
                    if self._looks_like_session_dead(e):
                        self._mark_session_dead()

                if df.empty:
                    self.safe_after(0, self.log_message, f"券商 API 查無 {raw_sym} 資料，請確認代碼、連線狀態，或該檔是否已完成合約下載。")
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

            df.dropna(subset=['Open', 'High', 'Low', 'Close'], inplace=True)
            if df.empty: return

            if df.index.tz is not None: df.index = df.index.tz_localize(None)
            df.index = pd.to_datetime(df.index)
            self.current_symbol = search_sym; self.current_stock_name = stock_name; self.current_df = df 
            
            latest_p = df['Close'].iloc[-1]
            tick = self.get_tick(latest_p)
            if tick >= 1: format_str = f"{int(latest_p)}"
            elif tick == 0.5 or tick == 0.1: format_str = f"{latest_p:.1f}"
            else: format_str = f"{latest_p:.2f}"
            
            def update_ui():
                try:
                    # 【ADR-008】entry_price 在「盤後定價」模式下是 disabled (價格鎖定收盤價)。
                    # tkinter 的 Entry 在 disabled 狀態下無法直接 insert/delete,
                    # 換股或換週期重新載入報價時要先暫時解鎖寫入,再依目前交易別鎖回。
                    was_disabled = (str(self.entry_price['state']) == 'disabled')
                    if was_disabled: self.entry_price.config(state="normal")
                    self.entry_price.delete(0, tk.END)
                    self.entry_price.insert(0, format_str)
                    if was_disabled or self.trade_mode == "Fixing":
                        self.entry_price.config(state="disabled")
                    # 換股後 asset_type 可能從股票變期貨或反之,同步鎖定/解鎖下單面板的交易別與種類
                    self.update_order_panel_for_asset_type()
                    # 【修正】換股時清掉滑鼠游標資訊列與 hover 索引:這個標籤只在滑鼠移到
                    # K線圖上才會更新,換股後如果使用者還沒移動滑鼠,它會停留在「上一檔股票」
                    # 的資料,跟新載入的圖表標題 (顯示正確的新股票名稱) 不一致造成混淆。
                    self.lbl_hover_info.config(text="滑鼠游標移至 K 線圖上方以顯示詳細資訊...", fg="#29B6F6")
                    self.last_hover_idx = -1
                    self.draw_chart(df)
                except Exception as e: self.log_message(f"介面更新異常: {e}")
            self.safe_after(0, update_ui)
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
            active_panels = {}

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
            # 視窗剛啟動、尚未完全繪製時 winfo_width()/height() 可能回傳 1 這種極小值，
            # 這時候退回一個合理的預設尺寸，避免算出畸形的 figsize。
            fig_w = (frame_w / dpi) if frame_w > 100 else 11
            fig_h = (frame_h / dpi) if frame_h > 100 else 8

            fig, axlist = mpf.plot(
                df, type='candle', volume=True, style=xq_style, returnfig=True, 
                figsize=(fig_w, fig_h), tight_layout=False, addplot=apds if apds else None, 
                panel_ratios=panel_ratios, datetime_format=dt_fmt,
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
                    axlist[0].set_xlim(x_min, x_max)
                    self.auto_scale_y(axlist[0], x_min, x_max)
                    self.auto_scale_indicator_panels(x_min, x_max)

            # 【使用者調整#9】主圖 MA/BB 的 hover 文字，改成每個指標各自獨立的
            # text 物件、顏色跟隨該指標在圖上設定的線條顏色 (例如 SMA20 設藍色，
            # 文字也顯示藍色)，不再是統一寫死的黃色一大段字串。
            main_text_props = dict(fontsize=9, weight='bold', verticalalignment='top', zorder=10000, clip_on=False,
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
                        return f"BB上:{row['BB_UPPER']:.2f} 中:{row['BB_MID']:.2f} 下:{row['BB_LOWER']:.2f}"
                    return None
                self.txt_main_segments.append({'obj': obj, 'fmt': _fmt_bb})

            # 【使用者調整#8】副圖 (MACD/RSI/KDJ/DMI/布林寬度) 的 hover 文字，
            # 同樣改成每個數值各自獨立的 text 物件，顏色跟隨該數值在副圖裡的
            # 線條顏色。Hist 是長條圖、正負值顏色會變 (紅漲綠跌)，用
            # dynamic_color_key 標記，在 on_mouse_move 裡依當下數值正負動態改色，
            # 而不是固定一種顏色。
            sub_text_props = dict(fontsize=9, weight='bold', verticalalignment='top', zorder=10000, clip_on=False,
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

            self.vlines = [ax.axvline(x=0, color='white', linestyle='--', linewidth=0.8, alpha=0.6, visible=False, zorder=50) for ax in axlist[::2]]

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

    def on_mouse_press(self, event):
        if event.button == 1 and event.inaxes: 
            self.is_panning = True; self.press_x_pixel = event.x; self.pan_axes = event.inaxes; self.press_xlim = event.inaxes.get_xlim()

    def on_mouse_release(self, event):
        if event.button == 1: self.is_panning = False; self.press_x_pixel = None

    def on_mouse_move(self, event):
        if self.is_panning and self.press_x_pixel is not None and event.inaxes == self.pan_axes:
            ax = self.pan_axes; dx_pixel = event.x - self.press_x_pixel
            bbox = ax.get_window_extent(); xmin, xmax = self.press_xlim
            dx_data = (dx_pixel / bbox.width) * (xmax - xmin)
            
            new_xmin = xmin - dx_data
            new_xmax = xmax - dx_data
            ax.set_xlim(new_xmin, new_xmax)
            
            # 【使用者第三次反映#3】原本只有在價格面板上拖曳才會重新計算 Y 軸，
            # 但 mplfinance 各面板共用同一個 x 軸，不論在哪個面板上拖曳，
            # 所有面板 (含副圖) 顯示的資料範圍都會跟著改變，副圖的 Y 軸也要
            # 一併重新計算，不能只看是不是在價格面板上拖曳。
            self.auto_scale_y(self.axlist[0], new_xmin, new_xmax)
            self.auto_scale_indicator_panels(new_xmin, new_xmax)
            event.canvas.draw_idle()
            return 
            
        if event.inaxes is None or event.xdata is None:
            if self.last_hover_idx != -1:
                for line in self.vlines: line.set_visible(False)
                for seg in self.txt_main_segments: seg['obj'].set_text("")
                for segs in self.sub_texts.values():
                    for seg in segs: seg['obj'].set_text("")
                event.canvas.draw_idle()
                self.last_hover_idx = -1
            return
            
        try:
            idx = int(round(event.xdata))
            if 0 <= idx < len(self.plot_df):
                row = self.plot_df.iloc[idx]
                if idx != self.last_hover_idx:
                    for line in self.vlines: line.set_xdata([idx, idx]); line.set_visible(True)

                    # 【使用者調整#8/#9】每個指標的文字物件各自獨立更新內容，
                    # 顏色在 draw_chart() 建立時就已經設定成跟該指標線條相同的顏色，
                    # 這裡只需要更新文字內容；Hist 是特例，因為長條圖本身紅漲綠跌
                    # 隨數值正負變色，文字顏色也要跟著動態調整，不能是固定色。
                    for seg in self.txt_main_segments:
                        txt = seg['fmt'](row)
                        seg['obj'].set_text(txt if txt else "")
                    for segs in self.sub_texts.values():
                        for seg in segs:
                            txt = seg['fmt'](row)
                            seg['obj'].set_text(txt if txt else "")
                            if seg.get('dynamic_color_key') == 'Hist' and 'Hist' in row and not np.isnan(row['Hist']):
                                seg['obj'].set_color('#FF1744' if row['Hist'] > 0 else '#00E676')

                    event.canvas.draw_idle()
                    self.last_hover_idx = idx

                tf = self.timeframe_var.get()
                date_str = self.plot_df.index[idx].strftime('%Y-%m-%d %H:%M') if "分" in tf else self.plot_df.index[idx].strftime('%Y-%m-%d')
                
                prev_c = self.plot_df['Close'].iloc[idx-1] if idx > 0 else row['Open']
                chg_pct = (row['Close'] - prev_c) / prev_c * 100
                chg_sign = "▲" if chg_pct > 0 else ("▼" if chg_pct < 0 else "-")
                
                raw_vol = float(row['Volume'])
                if self.asset_type == "future": vol_str = f"{raw_vol:,.0f} 口"
                elif self.asset_type == "us_stock": vol_str = f"{raw_vol:,.0f} 股"
                elif self.asset_type == "stock": vol_str = f"{raw_vol:,.0f} 張"
                elif self.asset_type == "index_tw": vol_str = f"{raw_vol:,.2f} 億"
                else: vol_str = f"{raw_vol:,.0f}"  
                
                display_name = f"{self.current_symbol} {self.current_stock_name}".strip()
                info = f"{display_name}  |  時間: {date_str}  |  開: {row['Open']:.2f}  高: {row['High']:.2f}  低: {row['Low']:.2f}  收: {row['Close']:.2f}  |  漲跌: {chg_sign} {abs(chg_pct):.2f}%  |  量: {vol_str}"
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
            if raw_sym in ['TXF', 'MTX', 'FITX', 'MXF']:
                code = 'TXF' if raw_sym in ['TXF', 'FITX'] else 'MXF'
                contract = getattr(self.sj_api.Contracts.Futures, code).get(f"{code}R1")
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
    def move_wl_up(self):
        selection = self.listbox_wl.curselection()
        if not selection or selection[0] == 0: return
        idx = selection[0]
        group = self.current_wl_name.get()
        lst = self.watchlists[group]
        lst[idx-1], lst[idx] = lst[idx], lst[idx-1]
        self.on_wl_change()
        self.listbox_wl.selection_set(idx-1)
        self.save_watchlists()

    def move_wl_down(self):
        selection = self.listbox_wl.curselection()
        group = self.current_wl_name.get()
        lst = self.watchlists[group]
        if not selection or selection[0] == len(lst) - 1: return
        idx = selection[0]
        lst[idx+1], lst[idx] = lst[idx], lst[idx+1]
        self.on_wl_change()
        self.listbox_wl.selection_set(idx+1)
        self.save_watchlists()

    def on_wl_change(self, event=None):
        group = self.current_wl_name.get()
        self.listbox_wl.delete(0, tk.END)
        if group in self.watchlists:
            for sym in self.watchlists[group]: self.listbox_wl.insert(tk.END, sym)

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
        selection = self.listbox_wl.curselection()
        if selection:
            sym = self.listbox_wl.get(selection[0])
            self.entry_symbol.delete(0, tk.END)
            self.entry_symbol.insert(0, sym)
            self.start_fetch_thread()

    def add_to_wl(self):
        group = self.current_wl_name.get()
        sym = self.entry_symbol.get().strip().upper()
        if sym and sym not in self.watchlists.get(group, []):
            self.watchlists[group].append(sym)
            self.on_wl_change()
            self.save_watchlists()

    def del_from_wl(self, event=None):
        group = self.current_wl_name.get()
        selection = self.listbox_wl.curselection()
        if selection:
            sym = self.listbox_wl.get(selection[0])
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

    # ================= 使用者調整#5:我的委託單 / 我的已成交 =================
    def set_bottom_tab(self, key):
        """切換底部區塊顯示「系統日誌與回報」/「我的委託單」/「我的已成交」其中一個分頁。"""
        self.bottom_tab = key
        for frame in (self.log_tab_frame, self.orders_tab_frame, self.fills_tab_frame):
            frame.pack_forget()
        {"log": self.log_tab_frame, "orders": self.orders_tab_frame, "fills": self.fills_tab_frame}[key].pack(fill=tk.BOTH, expand=True)
        for k, btn in self.bottom_tab_buttons.items():
            if k == key: btn.config(bg="#29B6F6", fg="black")
            else: btn.config(bg="#2A323D", fg="white")

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
                    prepared.append((
                        o.get('time_str', ''), o.get('code', ''),
                        self._normalize_action(o.get('action', '')),
                        price_str,
                        o.get('quantity', ''), o.get('filled_quantity', 0),
                        o.get('status_display', ''),
                    ))
                except Exception as row_err:
                    self.log_message(f"【我的委託單】有一筆資料無法顯示,已跳過: {type(row_err).__name__}: {row_err}")
            # 全部備妥後才動畫面
            for row_id in self.tree_orders.get_children():
                self.tree_orders.delete(row_id)
            for vals in prepared:
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
                self.log_message(f"【我的委託單】畫面已更新: Treeview {n_tree} 列 / my_orders {len(self.my_orders)} 筆")
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
            if (self._last_pending_order_key and self._last_pending_order_info
                    and self._last_pending_order_info.get('code') == contract.get('code')
                    and self._normalize_action(self._last_pending_order_info.get('action')) == act_norm
                    and self._safe_int(self._last_pending_order_info.get('quantity')) == self._safe_int(order.get('quantity'))):
                self.my_orders.pop(self._last_pending_order_key, None)
                self._last_pending_order_key = None
                self._last_pending_order_info = None
            entry = self.my_orders.get(order_id, {})
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