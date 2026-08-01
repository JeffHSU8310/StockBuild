"""
core.api_test — 永豐金 API 測試(證券/期貨下單測試)的純規則。

## 這是什麼

永豐金要求:簽署 API 條款後,必須在**模擬環境**跑過「登入測試」與
「下單測試」,而且**證券、期貨兩個帳戶要分別測**,通過之後正式環境的
下單權限才會開通。規格出處是官方文件
<https://sinotrade.github.io/zh/tutor/prepare/terms/>。

官方文件明列的硬性條件(逐條抄下來,不是我推測的):

  - 測試一律在模擬模式:`sj.Shioaji(simulation=True)`
  - 「因應公司資訊安全規定,測試報告服務為星期一至五 08:00 ~ 20:00」
    · 08:00–18:00 無地域限制
    · 18:00–20:00 僅允許台灣 IP
  - 「證券/期貨下單測試,需間隔1秒以上,以利系統留存測試紀錄」
  - shioaji 版本 >= 1.2
  - API key 須開通「交易」權限
  - API 簽署時間須早於測試時間;審核約 5 分鐘
  - 結果查詢:正式模式登入後看 `accounts` 的 `signed` 欄位

## 為什麼規則放在 core/

「現在能不能測」「兩筆單要隔多久」「版本夠不夠」全部是純判斷,放這裡就能
離線測試,也不會在 GUI 與 broker adapter 兩邊各寫一份(P-67)。
本模組零 tkinter、零 shioaji 依賴(鐵則 11)—— 真正建立模擬連線與送出委託
在 `brokers/sinopac.py`,那一層才可以碰 SDK(ADR-097)。

## 一個刻意的保守決定

`ORDER_INTERVAL_SEC` 設 **1.5 秒**而不是文件寫的 1 秒。文件說的是
「間隔 1 秒**以上**」,取剛好 1.0 會落在邊界上;而這件事失敗的代價是
「測試紀錄沒留成、要重跑、還要再等 5 分鐘審核」,多等 0.5 秒毫無成本。
"""
import datetime

#: shioaji 版本下限(官方:版本 >= 1.2)
MIN_SDK_VERSION = (1, 2)

#: 兩筆測試單之間的最小間隔。官方要求「1 秒以上」,這裡取 1.5 留餘裕。
ORDER_INTERVAL_SEC = 1.5

#: 測試報告服務時間(星期一~五)
WINDOW_START = (8, 0)
WINDOW_END = (20, 0)
#: 這個時間之後只接受台灣 IP
TW_IP_ONLY_FROM = (18, 0)

#: 官方範例的預設委託。使用者可在對話框改,改壞了會被 validate_order() 擋下。
DEFAULT_STOCK = {'code': '2890', 'price': 28.0, 'qty': 1}
DEFAULT_FUTURES = {'code': 'TXF', 'price': 37000.0, 'qty': 1}

#: 審核時間(官方:約 5 分鐘)
REVIEW_MINUTES = 5


def parse_version(text):
    """把 '1.7.0' / 'v1.2' 這種字串解析成 (1, 7) / (1, 2)。解析不出來回 None。

    只取前兩段:版本下限是 1.2,第三段(patch)與這個判斷無關,而 shioaji
    的版本字串出現過 '1.2.0b1' 這種帶後綴的形式,硬要全解析反而容易誤判。
    """
    s = str(text or '').strip().lstrip('vV')
    if not s:
        return None
    parts = []
    for chunk in s.split('.')[:2]:
        digits = ''
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == '':
            return None
        parts.append(int(digits))
    if not parts:
        return None
    while len(parts) < 2:
        parts.append(0)
    return tuple(parts)


def version_ok(text):
    """回傳 (是否符合, 說明)。版本解析不出來時**當作不符合**並講清楚。

    刻意不放行未知版本:這條檢查的目的是在送出前就攔下「測了也不會過」的
    情況,放行未知等於把判斷丟給使用者自己從錯誤訊息猜。
    """
    ver = parse_version(text)
    want = '.'.join(str(x) for x in MIN_SDK_VERSION)
    if ver is None:
        return False, f"讀不出 shioaji 版本({text!r}),官方要求 >= {want}"
    if ver < MIN_SDK_VERSION:
        return False, f"shioaji 版本 {'.'.join(str(x) for x in ver)} 太舊,官方要求 >= {want}"
    return True, f"shioaji 版本 {'.'.join(str(x) for x in ver)}(>= {want})"


def _hhmm(t):
    return (t.hour, t.minute)


def window_status(now=None):
    """現在能不能做 API 測試。回傳 (可否, 說明, 是否僅限台灣IP)。

    官方:星期一至五 08:00~20:00;18:00~20:00 僅允許台灣 IP。
    「僅限台灣 IP」這件事**這裡判斷不出來**(要知道對外 IP 的地理位置),
    所以誠實地把它當成第三個回傳值交給呼叫端去提醒使用者,而不是自己猜一個
    答案 —— 猜錯的後果是使用者以為程式壞了,其實是他人在國外(鐵則 4 的精神:
    沒有的資訊要標示,不要編)。
    """
    now = now or datetime.datetime.now()
    wd = now.weekday()              # 0=一 … 6=日
    hm = _hhmm(now)
    if wd >= 5:
        return False, "測試報告服務只在星期一至五開放,今天是週末", False
    if hm < WINDOW_START:
        return False, (f"還沒到測試時間(每日 {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d} 起,"
                       f"至 {WINDOW_END[0]:02d}:{WINDOW_END[1]:02d})"), False
    if hm >= WINDOW_END:
        return False, (f"已過測試時間(每日 {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}~"
                       f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d})"), False
    tw_only = hm >= TW_IP_ONLY_FROM
    if tw_only:
        return True, (f"在測試時間內,但 {TW_IP_ONLY_FROM[0]:02d}:{TW_IP_ONLY_FROM[1]:02d} 之後"
                      "**僅允許台灣 IP**(人在國外或走境外 VPN 會失敗)"), True
    return True, "在測試時間內(此時段無地域限制)", False


def validate_order(kind, code, price, qty):
    """送出前先在本地驗欄位(鐵則 9 的精神:不靠券商回錯誤才知道)。

    回傳 (ok, why)。這裡只擋「一定會被退」的明顯錯誤 —— 價格是否落在當日
    漲跌停之內**不在這裡判斷**,那需要當日參考價,測試流程刻意不去抓報價
    (會踩到鐵則 5 的快照節流)。對話框會提醒使用者這件事。
    """
    if kind not in ('證券', '期貨'):
        return False, f"不認得的測試種類:{kind}"
    c = str(code or '').strip()
    if not c:
        return False, "商品代碼不可空白"
    if kind == '證券' and not (c.isdigit() and len(c) == 4):
        return False, f"證券代碼要是 4 位數字(收到 {c!r})"
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False, f"價格不是數字:{price!r}"
    if p <= 0:
        return False, "價格必須大於 0(測試單一律用限價,不可用市價)"
    try:
        q = int(qty)
    except (TypeError, ValueError):
        return False, f"數量不是數字:{qty!r}"
    if q < 1:
        return False, "數量必須至少 1"
    # 這是「測試單」,不是真的要成交。數量大沒有意義,只會讓萬一環境接錯時
    # 的後果變嚴重,所以在本地就把上限壓死。
    if q > 1:
        return False, "測試單數量請維持 1(證券 1 張 / 期貨 1 口),這是測試不是交易"
    return True, ""


def pick_near_month(contracts, today=None):
    """從一堆期貨合約裡挑「最近月」。contracts 是 [(代碼, 交割日字串), ...]。

    回傳代碼;挑不出來回 None。

    為什麼不像官方範例那樣寫死 `TXFE6`:那是**會過期的**。官方文件寫於某個
    時點,照抄到程式裡,幾個月後就變成「查無此合約」而測試失敗,而失敗訊息
    完全不會告訴你原因是合約過期。

    刻意排除 R1/R2 這種連續合約:那是報價用的虛擬代碼,不是可下單的月份合約。
    """
    today = today or datetime.date.today()
    best = None
    for code, delivery in (contracts or []):
        c = str(code or '').strip().upper()
        if not c or c.endswith('R1') or c.endswith('R2'):
            continue
        d = _to_date(delivery)
        if d is None or d < today:
            continue
        if best is None or d < best[1]:
            best = (c, d)
    return best[0] if best else None


def _to_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    s = str(value or '').strip()
    for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%Y%m%d'):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def preflight(sdk_version, has_stock_account, has_futopt_account, now=None):
    """送出任何東西之前的整批檢查。回傳 (可否開始, [說明行, ...])。

    帳戶缺一個**不是**致命錯誤:有人只簽了證券或只簽了期貨。缺的那一項會
    在說明裡標出來、對應的測試會被跳過,而不是整個流程拒跑。
    """
    lines = []
    ok = True

    v_ok, v_why = version_ok(sdk_version)
    lines.append(("✅ " if v_ok else "❌ ") + v_why)
    ok = ok and v_ok

    w_ok, w_why, tw_only = window_status(now)
    lines.append(("✅ " if w_ok else "❌ ") + w_why)
    ok = ok and w_ok
    if tw_only:
        lines.append("⚠️ 若你人在國外或連線走境外節點,這個時段會測不過。")

    if has_stock_account:
        lines.append("✅ 找得到證券帳戶")
    else:
        lines.append("⚠️ 找不到證券帳戶 —— 會跳過證券下單測試")
    if has_futopt_account:
        lines.append("✅ 找得到期貨帳戶")
    else:
        lines.append("⚠️ 找不到期貨帳戶 —— 會跳過期貨下單測試")

    if not (has_stock_account or has_futopt_account):
        lines.append("❌ 兩種帳戶都沒有,沒有東西可以測")
        ok = False

    return ok, lines


def format_report(results, now=None):
    """把每一步的結果排成一段給人看的報告。

    results 是 [{'step': 名稱, 'ok': bool, 'detail': 字串}, ...]。
    """
    now = now or datetime.datetime.now()
    lines = [f"【永豐 API 測試報告】{now.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for r in (results or []):
        mark = "✅" if r.get('ok') else "❌"
        detail = str(r.get('detail', '')).strip()
        lines.append(f"{mark} {r.get('step', '')}" + (f" —— {detail}" if detail else ""))
    lines.append("")
    passed = [r for r in (results or []) if r.get('ok')]
    lines.append(f"通過 {len(passed)} / {len(results or [])} 步。")
    if results and all(r.get('ok') for r in results):
        lines.append(f"送出完成。永豐端審核約 {REVIEW_MINUTES} 分鐘,"
                     "之後用「查詢測試狀態」或到簽署中心網站確認。")
        lines.append("提醒:若 API 條款的簽署時間**晚於**這次測試時間,這次不算數,"
                     "要重新測一次。")
    else:
        lines.append("有步驟沒過。請看上面各行的說明,修正後重跑 —— "
                     "重跑不會有副作用(模擬環境不會真的成交)。")
    return "\n".join(lines)


def signed_summary(accounts):
    """把正式環境登入後的 accounts 整理成「哪個帳戶通過測試了」。

    accounts 是 [(帳號字串, 種類字串, signed 布林), ...] —— 刻意用純資料而不是
    shioaji 物件,這樣這個函式能離線測試,取欄位的工作留在 broker adapter。
    """
    rows = list(accounts or [])
    if not rows:
        return "查不到任何帳戶(請先以**正式模式**登入)。"
    lines = ["【API 測試狀態】(來自正式環境登入後的 accounts.signed)", ""]
    for acc_id, kind, signed in rows:
        lines.append(f"{'✅ 已通過' if signed else '❌ 尚未通過'}  "
                     f"{kind or '?'}  {acc_id or '?'}")
    lines.append("")
    if all(s for _, _, s in rows):
        lines.append("全部帳戶都已通過 API 測試。")
    else:
        lines.append("尚未通過的帳戶請跑一次下單測試;剛測完的話請等審核"
                     f"(約 {REVIEW_MINUTES} 分鐘)後再查一次。")
    return "\n".join(lines)
