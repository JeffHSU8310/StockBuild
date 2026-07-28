"""
diag_mock_tkinter.py — 無畫面診斷用的假 tkinter / shioaji / mplfinance / yfinance 環境。

僅供開發除錯使用，不隨 App 發布。用法:
    import diag_mock_tkinter
    diag_mock_tkinter.install_mock_tkinter()
    import stock_app_pro   # 之後 import 的 tkinter 都是假的

設計原則:
- mpf.plot() 用「真正的 matplotlib Figure + add_axes」建面板 (跟 mplfinance 內部
  一樣是 add_axes,不是 subplot 網格),讓 draw_chart() 對著真的 Axes 執行,
  才能抓到 set_position / subplots_adjust 這類版面問題。
- tkinter Variable (StringVar 等) 有真實的 get/set 行為。
- Treeview 有真實的 insert/delete/get_children 行為 (清單內容可驗證)。
- after() 不立即執行,把 callback 排進佇列,測試端用 flush_after() 手動沖掉,
  模擬「排回主執行緒」的時序。
"""
import sys
import types


# ---------------------------------------------------------------- Variables
class _Var:
    def __init__(self, master=None, value=None, name=None):
        self._v = value if value is not None else self._default
    def get(self): return self._v
    def set(self, v): self._v = v
    def trace_add(self, *a, **k): pass
    def trace(self, *a, **k): pass

class _StringVar(_Var): _default = ""
class _IntVar(_Var): _default = 0
class _DoubleVar(_Var): _default = 0.0
class _BooleanVar(_Var): _default = False


# ---------------------------------------------------------------- 泛用 Widget
class _MockWidget:
    def __init__(self, master=None, **kw):
        self.master = master
        self._kw = dict(kw)
        self._children = []
        self._bindings = {}
        self._destroyed = False   # 【ADR-057】忠實模擬 destroy/winfo_exists
        if master is not None and hasattr(master, "_children"):
            master._children.append(self)
    # 幾何管理
    def pack(self, *a, **k): return self
    def grid(self, *a, **k): return self
    def place(self, *a, **k): return self
    def pack_forget(self): pass
    def grid_forget(self): pass
    def pack_propagate(self, flag=None): pass
    def grid_propagate(self, flag=None): pass
    def destroy(self):
        # 【ADR-057】真實 tkinter 的 destroy() 會「連同所有子元件」一起銷毀,
        # 之後對它們呼叫 winfo_exists() 一律回 False,再操作則拋 TclError。
        # 這份 mock 原本 destroy() 是 no-op、winfo_exists() 永遠 True,
        # 等於完全測不到「元件被銷毀後不可再碰」這整類 bug (本專案最常見的
        # 崩潰來源之一)。改為忠實模擬,讓診斷案例能真的驗證清理邏輯。
        for ch in list(self._children):
            try:
                ch.destroy()
            except Exception:
                pass
        self._children = []
        self._destroyed = True
        if self.master is not None and hasattr(self.master, "_children"):
            try:
                self.master._children.remove(self)
            except ValueError:
                pass
    # 屬性
    def config(self, **kw): self._kw.update(kw)
    configure = config
    def cget(self, key): return self._kw.get(key)
    def __setitem__(self, k, v): self._kw[k] = v
    def __getitem__(self, k): return self._kw.get(k)
    # 事件
    def bind(self, seq, func=None, add=None): self._bindings[seq] = func
    def unbind(self, seq): self._bindings.pop(seq, None)
    def focus_set(self): pass
    def focus_get(self): return None
    # 尺寸
    def winfo_width(self): return int(self._kw.get("width", 1200))
    def winfo_height(self): return int(self._kw.get("height", 800))
    def winfo_x(self): return 0
    def winfo_y(self): return 0
    def winfo_children(self): return list(self._children)
    def winfo_exists(self): return not self._destroyed
    def winfo_screenwidth(self): return 1920
    def winfo_screenheight(self): return 1080
    def update_idletasks(self): pass
    def update(self): pass
    def lift(self): pass
    # 【ADR-116】真實 Toplevel 有這些視窗操作;mock 缺了它們,凡是「已開著就
    # 帶到最前面」的程式碼都會拋 AttributeError 掉進 except,診斷環境測不到
    # 正確行為 (同 P-57 的教訓:mock 缺方法 = 該路徑等於沒被測)。
    def deiconify(self): pass
    def iconify(self): pass
    def withdraw(self): pass
    def focus_force(self): pass
    def attributes(self, *a, **k): pass
    def resizable(self, *a, **k): pass
    def transient(self, *a): pass
    def grab_set(self): pass
    def title(self, *a): pass
    def geometry(self, *a): pass
    def protocol(self, *a, **k): pass
    def selection_present(self): return False
    def yview(self, *a, **k): pass
    def set(self, *a, **k): pass  # Scrollbar.set / 泛用
    def xview(self, *a, **k): pass
    def see(self, *a, **k): pass


class _Entry(_MockWidget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._text = ""
        self._cursor = 0
        self._tv = kw.get("textvariable")
    def _sync_from_var(self):
        if self._tv is not None: self._text = str(self._tv.get())
    def _sync_to_var(self):
        if self._tv is not None: self._tv.set(self._text)
    def get(self):
        self._sync_from_var(); return self._text
    def insert(self, index, s):
        self._sync_from_var()
        i = self._cursor if index in ("insert", type("I", (), {})) else (len(self._text) if index == "end" else int(index) if str(index).isdigit() else self._cursor)
        if index == "insert": i = self._cursor
        self._text = self._text[:i] + str(s) + self._text[i:]
        self._cursor = i + len(str(s))
        self._sync_to_var()
    def delete(self, first, last=None):
        self._sync_from_var()
        if first == 0 and last in ("end", None):
            self._text = ""; self._cursor = 0
        self._sync_to_var()
    def index(self, what): return self._cursor
    def icursor(self, i): self._cursor = int(i)


class _Text(_MockWidget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._content = []
    def insert(self, index, s, *tags): self._content.append(str(s))
    def delete(self, *a): self._content = []
    def see(self, *a): pass
    def get(self, a="1.0", b="end"): return "".join(self._content)
    def tag_config(self, *a, **k): pass


class _Listbox(_MockWidget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._items = []
        self._sel = ()
    def insert(self, index, item): self._items.append(item)
    def delete(self, first, last=None): self._items = []
    def curselection(self): return self._sel
    def selection_set(self, i): self._sel = (i,)
    def get(self, i): return self._items[i]
    def size(self): return len(self._items)


class _Treeview(_MockWidget):
    """支援 heading/column/insert/delete/get_children/item,內容可驗證。"""
    _counter = 0
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._rows = {}      # iid -> values tuple
        self._tags = {}      # iid -> tags tuple (ADR-100:上色邏輯要可驗證)
        self._order = []
        self._focus = None
        self._selection = ()  # 【ADR-057】忠實模擬 selection API
    def heading(self, c, **kw): pass
    def column(self, c, **kw): pass
    def tag_configure(self, tag, **kw): pass
    def insert(self, parent, index, iid=None, values=(), **kw):
        if iid is None:
            _Treeview._counter += 1
            iid = f"I{_Treeview._counter:04d}"
        elif iid in self._rows:
            raise Exception(f"iid {iid} already exists")  # 忠實模擬真實 tkinter 重複 iid 會拋錯
        self._rows[iid] = tuple(values)
        # 【ADR-100】保存 tags:舊版直接丟棄,導致所有「依 tag 上色」的邏輯
        # (最典型的就是鐵則1 紅漲綠跌) 在診斷環境完全測不到——同 P-28 的教訓,
        # 空殼 mock 會讓被測程式碼從沒真正被驗證。
        self._tags[iid] = tuple(kw.get('tags') or ())
        self._order.append(iid)
        return iid
    def delete(self, *iids):
        for iid in iids:
            self._rows.pop(iid, None)
            self._tags.pop(iid, None)
            if iid in self._order: self._order.remove(iid)
    def get_children(self, item=None): return list(self._order)
    def focus(self, item=None):
        if item is not None: self._focus = item
        return self._focus or ""
    # 【ADR-057】以下三個是真實 ttk.Treeview 本來就有、但這份 mock 之前漏做的
    # API。量化面板改成多份 (分頁 + 獨立視窗) 後,刷新時要保留使用者原本
    # 選取的策略、選取時要判斷 iid 還在不在,都會用到它們;mock 缺這些方法
    # 會讓相關程式碼在診斷環境靜默走進 except 分支,測不到真正的行為。
    def selection(self, *args):
        # 【ADR-057】這個 mock 原本有「兩個」selection 定義 (一個在此、一個在
        # item() 之後),後定義的會靜默覆蓋前者 —— 導致 selection_set() 寫進去的
        # 值永遠讀不回來,相關程式碼在診斷環境走進 except 而測不到真實行為。
        # 已刪除重複定義,並在此合併兩種語意:
        #   * 有明確設定過 selection → 回傳它 (新的多面板選取邏輯需要)
        #   * 沒設定過 → 退回 focus (既有診斷案例都是用 focus() 設定的,不可破壞)
        if args:
            self._selection = tuple(args[0]) if isinstance(args[0], (list, tuple)) else (args[0],)
        if self._selection:
            return tuple(self._selection)
        return (self._focus,) if self._focus else ()
    def selection_set(self, *items):
        flat = []
        for it in items:
            flat.extend(it if isinstance(it, (list, tuple)) else [it])
        self._selection = tuple(flat)
    def exists(self, iid):
        return iid in self._rows
    def item(self, iid, option=None, **kw):
        # 忠實模擬真實 tkinter:item(iid, values=..., tags=...) 可更新既有列
        if kw:
            if iid not in self._rows:
                raise Exception(f"item {iid} not found")
            if 'values' in kw:
                self._rows[iid] = tuple(kw['values'])
        if kw and 'tags' in kw and iid in self._rows:
            self._tags[iid] = tuple(kw['tags'] or ())
        if option == "values": return self._rows.get(iid, ())
        if option == "tags": return self._tags.get(iid, ())
        return {"values": self._rows.get(iid, ()), "tags": self._tags.get(iid, ())}
    def identify_row(self, y):
        # 診斷用:回傳目前 focus 的列 (真實環境是依 y 座標,mock 無座標概念)
        return self._focus or ""


class _Scale(_MockWidget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._var = kw.get("variable")
        self._cmd = kw.get("command")
    def set(self, v):
        if self._var is not None: self._var.set(v)
        if self._cmd: self._cmd(v)


# ---------------------------------------------------------------- Tk 本體
class _Tk(_MockWidget):
    def __init__(self):
        super().__init__(None)
        self._after_queue = []      # (id, func, args)
        self._after_seq = 0
        self._cancelled = set()
    def after(self, delay, func=None, *args):
        self._after_seq += 1
        aid = f"after#{self._after_seq}"
        if func is not None:
            self._after_queue.append((aid, func, args))
        return aid
    def after_cancel(self, aid): self._cancelled.add(aid)
    def flush_after(self, max_rounds=5):
        """把 after 佇列裡尚未取消的 callback 全部執行掉 (模擬回到主執行緒)。"""
        for _ in range(max_rounds):
            q, self._after_queue = self._after_queue, []
            if not q: break
            for aid, func, args in q:
                if aid in self._cancelled: continue
                func(*args)
    def mainloop(self): pass
    def destroy(self): pass
    def quit(self): pass
    def option_add(self, *a): pass
    def resizable(self, *a): pass
    def state(self, *a): pass
    def iconbitmap(self, *a): pass
    def attributes(self, *a, **k): pass


# ---------------------------------------------------------------- ttk / 對話框
def _build_ttk():
    ttk = types.ModuleType("tkinter.ttk")
    ttk.Treeview = _Treeview
    # 【ADR-110】current() 原本永遠回 0 且忽略設定值,等於「下拉選單選了什麼」
    # 在診斷環境完全測不到 —— 而「策略指定哪一個實單帳號」正是靠這個讀出來的,
    # 選錯就是真錢下錯戶頭。改成真的記住索引,讓 current(i) → current() 能往返。
    def _cb_current(self, i=None):
        if i is None:
            return getattr(self, '_cur_index', 0)
        self._cur_index = int(i)
        # 【ADR-118】真實的 ttk.Combobox 選了索引之後,get() 會回傳那一項的值。
        # mock 若只記索引不連動 get(),所有「用 get() 讀使用者選擇」的程式碼
        # (籌碼年數、選股歷史深度…) 在診斷環境都讀到空字串而落回預設值,
        # 等於那條路徑沒被測到 (同 P-57)。
        vals = list(self._kw.get('values') or [])
        if 0 <= self._cur_index < len(vals):
            self._text = str(vals[self._cur_index])
            self._sync_to_var()
        return None

    ttk.Combobox = type("Combobox", (_Entry,), {
        "current": _cb_current,
        "set": lambda self, v: (self._tv.set(v) if self._tv else None),
    })
    ttk.Button = _MockWidget
    ttk.Label = _MockWidget
    ttk.Frame = _MockWidget
    ttk.Separator = _MockWidget
    ttk.Scrollbar = _MockWidget
    ttk.Notebook = _MockWidget
    ttk.PanedWindow = type("PanedWindow", (_MockWidget,), {
        "add": lambda self, child, **kw: None,
        "sashpos": lambda self, *a, **k: 0,
    })
    class _Style:
        def __init__(self, *a, **k): pass
        def theme_use(self, *a): pass
        def configure(self, *a, **k): pass
        def map(self, *a, **k): pass
    ttk.Style = _Style
    return ttk


def _build_messagebox():
    mb = types.ModuleType("tkinter.messagebox")
    mb.showinfo = lambda *a, **k: None
    mb.showwarning = lambda *a, **k: None
    mb.showerror = lambda *a, **k: None
    mb.askyesno = lambda *a, **k: True
    mb.askokcancel = lambda *a, **k: True
    return mb


def _build_simpledialog():
    sd = types.ModuleType("tkinter.simpledialog")
    sd.askstring = lambda *a, **k: None
    return sd


def _build_filedialog():
    """【ADR-051】策略腳本匯入/匯出用到 tkinter.filedialog;無顯示環境下打樁。
    預設回傳空字串 = 使用者按取消,診斷腳本不會真的碰檔案系統。"""
    fd = types.ModuleType("tkinter.filedialog")
    fd.askopenfilename = lambda *a, **k: ""
    fd.asksaveasfilename = lambda *a, **k: ""
    fd.askdirectory = lambda *a, **k: ""
    return fd


# ---------------------------------------------------------------- mplfinance (真 Axes)
def _build_mpf():
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    mpf = types.ModuleType("mplfinance")

    def make_addplot(data, **kw):
        return {"data": data, **kw}

    def make_marketcolors(**kw): return dict(kw)
    def make_mpf_style(**kw): return dict(kw)

    def plot(df, **kw):
        """
        模擬 mplfinance 的面板建構方式:每個面板用 fig.add_axes(rect) 建立
        (跟真的 mplfinance _panels.py 一樣,不是 subplot 網格),每個面板
        配一個 twinx。回傳 (fig, axlist),axlist = [ax0, ax0t, ax1, ax1t, ...]。
        """
        panel_ratios = kw.get("panel_ratios") or [5, 1.2]
        figsize = kw.get("figsize", (11, 8))
        fig = plt.figure(figsize=figsize)
        n = len(panel_ratios)
        total = sum(panel_ratios)
        # 模擬 mplfinance 預設留白 (這正是使用者抱怨的空白來源)
        left, right, top, bottom, gap = 0.125, 0.90, 0.90, 0.11, 0.02
        avail = (top - bottom) - gap * (n - 1)
        axlist = []
        y = top
        for r in panel_ratios:
            h = avail * r / total
            ax = fig.add_axes([left, y - h, right - left, h])
            axt = ax.twinx()
            axlist.extend([ax, axt])
            y = y - h - gap
        # 在主圖畫一條線,讓 get_xlim 之類的操作有真實資料範圍
        axlist[0].plot(range(len(df)), df["Close"].values)
        if kw.get("returnfig"):
            return fig, axlist
        return None

    mpf.plot = plot
    mpf.make_addplot = make_addplot
    mpf.make_marketcolors = make_marketcolors
    mpf.make_mpf_style = make_mpf_style
    return mpf


def _build_yfinance():
    yf = types.ModuleType("yfinance")
    class _T:
        def __init__(self, *a, **k): pass
        def history(self, *a, **k):
            import pandas as pd
            return pd.DataFrame()
    yf.Ticker = _T
    yf.download = lambda *a, **k: None
    return yf


def _build_backend_tkagg():
    mod = types.ModuleType("matplotlib.backends.backend_tkagg")
    class FigureCanvasTkAgg:
        def __init__(self, fig, master=None):
            self.figure = fig
            self._widget = _MockWidget(master)
        def get_tk_widget(self): return self._widget
        def draw(self): pass
        def draw_idle(self): pass
        def mpl_connect(self, *a, **k): return 1
        def flush_events(self): pass
    mod.FigureCanvasTkAgg = FigureCanvasTkAgg
    return mod


def _build_shioaji():
    sj = types.ModuleType("shioaji")
    class _Enum:
        def __getattr__(self, name): return name
    const = types.ModuleType("shioaji.constant")
    for name in ("Action", "StockPriceType", "OrderType", "StockOrderLot",
                 "StockOrderCond", "QuoteVersion", "QuoteType", "OrderState",
                 "FuturesPriceType", "FuturesOCType", "Unit", "Exchange"):
        setattr(const, name, _Enum())
    sj.constant = const
    class Order:
        def __init__(self, **kw):
            for k, v in kw.items(): setattr(self, k, v)
    sj.Order = Order
    sj.order = types.ModuleType("shioaji.order")
    sj.order.StockOrderLot = const.StockOrderLot
    class Shioaji:
        def __init__(self, *a, **k): pass
    sj.Shioaji = Shioaji
    sys.modules["shioaji.constant"] = const
    return sj


# ---------------------------------------------------------------- 安裝
def install_mock_tkinter():
    tk = types.ModuleType("tkinter")
    tk.Tk = _Tk
    tk.Toplevel = _MockWidget
    tk.Frame = _MockWidget
    tk.LabelFrame = _MockWidget
    tk.Label = _MockWidget
    tk.Button = _MockWidget
    tk.Checkbutton = _MockWidget
    tk.Radiobutton = _MockWidget
    tk.Entry = _Entry
    tk.Text = _Text
    tk.Listbox = _Listbox
    tk.Scrollbar = _MockWidget
    tk.Scale = _Scale
    tk.Canvas = _MockWidget
    tk.Menu = _MockWidget
    tk.PanedWindow = _MockWidget
    tk.StringVar = _StringVar
    tk.IntVar = _IntVar
    tk.DoubleVar = _DoubleVar
    tk.BooleanVar = _BooleanVar
    tk.TclError = type("TclError", (Exception,), {})
    # 常數
    for name, val in dict(X="x", Y="y", BOTH="both", LEFT="left", RIGHT="right",
                          TOP="top", BOTTOM="bottom", END="end", W="w", E="e",
                          N="n", S="s", NSEW="nsew", EW="ew", CENTER="center",
                          HORIZONTAL="horizontal", VERTICAL="vertical",
                          NORMAL="normal", DISABLED="disabled", INSERT="insert",
                          WORD="word", FLAT="flat", SUNKEN="sunken",
                          RAISED="raised", GROOVE="groove", RIDGE="ridge",
                          SINGLE="single", BROWSE="browse", EXTENDED="extended",
                          MULTIPLE="multiple", TRUE=True, FALSE=False).items():
        setattr(tk, name, val)
    ttk = _build_ttk()
    tk.ttk = ttk
    mb = _build_messagebox()
    tk.messagebox = mb
    sd = _build_simpledialog()
    tk.simpledialog = sd
    fd = _build_filedialog()
    tk.filedialog = fd

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.messagebox"] = mb
    sys.modules["tkinter.simpledialog"] = sd
    sys.modules["tkinter.filedialog"] = fd
    sys.modules["tkinter.font"] = types.ModuleType("tkinter.font")
    sys.modules["mplfinance"] = _build_mpf()
    sys.modules["yfinance"] = _build_yfinance()
    sys.modules["matplotlib.backends.backend_tkagg"] = _build_backend_tkagg()
    sys.modules["shioaji"] = _build_shioaji()
    return tk
