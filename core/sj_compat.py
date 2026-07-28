# -*- coding: utf-8 -*-
"""
core/sj_compat.py — shioaji 1.5.6 / 1.7 相容層 (ADR-114)

【為什麼要相容而不是直接改成 1.7】升級券商 SDK 動到的是整個系統的資料入口
(鐵則 12:台股一律 shioaji)。如果一次改死成 1.7,一旦實機發現問題,退回的
成本是「再改一次全部呼叫點」。做成相容層則是改一行設定就能退回,而且**同一
份程式碼在兩個版本下都有測試覆蓋**。

【1.7 的實際變更】以下事實來自**直接讀 shioaji 1.7.0 wheel 裡的型別定義**
(`shioaji/_core.pyi`),不是照升級指南的敘述:

  1. `login()` 移除 `contracts_timeout` / `fetch_contract` / `contracts_cb`,
     新增 `subscribe_trade` / `receive_window` / `force_refresh`。
  2. 指數代碼改成交易所代碼 (加權 `IX0001`);舊的 `TSE001` / `OTC101` 不再適用。
  3. 新增輕量型別 `StockInfo` / `IndexInfo` / `FuturesInfo` / `OptionInfo` /
     `WarrantInfo` (只有 security_type/region/exchange/code/target_code/base)。
     完整的 `Contract` 類別**仍然有** symbol/category/unit/reference 等欄位,
     所以「symbol 被移除」要理解成「**列舉合約時拿到的可能是輕量型別**」,
     而不是「Contract 沒有 symbol 了」。

  ⚠️ 升級指南另外聲稱 `fetch_contracts()`、`Contracts.status` 已移除、
  `SecurityType.Future` 更名為 `Futures` —— 但 1.7.0 的型別定義裡**這三者
  都還在原樣**。本專案沒有用到這三個,所以不影響;但這說明指南與實作有出入,
  凡是靠指南敘述的判斷都要再查證。

【本模組的設計】全部是**鴨子型別的純函式**:傳進來的可以是真的 shioaji
物件,也可以是假物件。因此 1.5.6 與 1.7 兩種形狀都能離線測試,不需要連線、
不需要真的裝任何一版 (鐵則 11)。
"""
import inspect

# 指數代碼候選。順序 = 嘗試順序,新版在前 (升級後大多數情況第一個就中)。
#
# 為什麼是「候選清單」而不是「依版本選一個」:偵測 SDK 版本再分支,等於把
# 「版本號」當成「行為」的代理,而券商改版時這兩者不一定同步 (例如 1.7 的
# 指南就與實作有出入)。直接試哪一個拿得到合約,比猜版本可靠。
# 【1.7 代碼對照】升級指南明載舊寫法 `Indexs.TSE["001"]` → 新寫法
# `Indexs.TSE["IX0001"]`;櫃買指數指南沒有列出,由使用者提供的台灣指數代碼
# 對照表確認為 **IX0043**。
#
# ⚠️ 這裡記一個教訓:我原本從「IX + 舊代碼補零」的規律推論櫃買是 IX0101
# (舊代碼 101),結果是錯的 —— IX 後面那四位是**指數自己的編號**,跟舊代碼
# 沒有對應關係。規律看起來成立不代表它是規則 (見 P-66)。
#
# 舊代碼仍留在候選最後面,讓同一份程式碼在 1.5.6 底下也能跑;推論過的
# IX0101/IX0002 一併保留當保險,反正沒中只是多試一次。
INDEX_CANDIDATES = {
    'TSE': ('IX0001', 'TSE001', '001'),                                  # 加權指數
    'OTC': ('IX0043', 'IX0101', 'IX0002', 'OTC101', 'OTC001', '101'),    # 櫃買指數
}

# 其他常見指數代碼 (目前系統沒有對應的查詢入口,列出來供日後擴充參考):
#   IX0012 / TW50  富時臺灣證券交易所臺灣50指數

# 這些代碼都代表「指數」,策略層判斷用 (原本只認舊代碼)。
INDEX_SYMBOLS = frozenset({
    'TWII', 'TWOII', 'TSE', 'OTC',
    'TSE001', 'OTC101', 'OTC001',        # 1.5.6
    'IX0001', 'IX0043',                  # 1.7 (加權 / 櫃買)
    'IX0101', 'IX0002',                  # 1.7 (先前推論過的櫃買代碼,保留當保險)
})


def index_candidates(market):
    """某個市場的指數代碼候選 (由新到舊)。market: 'TSE' / 'OTC'。"""
    return INDEX_CANDIDATES.get(str(market or '').upper().strip(), ())


def _try_get(obj, key):
    """從一個「像容器的東西」用 key 取值,取不到回 None。

    shioaji 兩個版本的容器形狀不同 (1.7 的 ContractCategory 有 get/__getitem__,
    1.5.6 是巢狀的屬性群組),而且 __getattr__ 在查無時可能拋任何例外。
    這裡把三種取法都試過,全部失敗才回 None。
    """
    if obj is None:
        return None
    getter = getattr(obj, 'get', None)
    if callable(getter):
        try:
            v = getter(key)
            if v is not None:
                return v
        except Exception:
            pass
    try:
        v = obj[key]
        if v is not None:
            return v
    except Exception:
        pass
    try:
        v = getattr(obj, key, None)
        if v is not None:
            return v
    except Exception:
        pass
    return None


def resolve_index(indexs, market):
    """從 `api.Contracts.Indexs` 取出指數合約。找不到回 None。

    兩種存取形狀都試:
      1.7   : Indexs.get('IX0001')          ← ContractCategory 直接吃代碼
      1.5.6 : Indexs.TSE.TSE001             ← 先分交易所群組,再取代碼
    先試前者是因為升級後那是正解;失敗才退回舊形狀。全程不拋例外——
    指數抓不到要讓呼叫端自己決定怎麼處理 (通常是報「請先登入」)。
    """
    codes = index_candidates(market)
    if not codes or indexs is None:
        return None
    # 形狀一:直接用代碼查 (1.7)
    for code in codes:
        c = _try_get(indexs, code)
        if c is not None and not _looks_like_group(c):
            return c
    # 形狀二:先取交易所群組再查代碼 (1.5.6),群組名同時試市場名與各代碼前綴
    group = _try_get(indexs, str(market).upper())
    if group is not None:
        for code in codes:
            c = _try_get(group, code)
            if c is not None and not _looks_like_group(c):
                return c
    return None


def _looks_like_group(obj):
    """粗略判斷取回來的是「合約」還是「群組容器」。

    1.5.6 的 `Indexs.TSE` 是群組,若不擋掉,resolve_index 會把群組當成合約
    回傳,呼叫端拿去下單/訂閱就會出現非常難懂的錯誤。判斷依據是「有沒有
    code 這個字串屬性」——合約一定有,群組沒有。
    """
    code = getattr(obj, 'code', None)
    return not isinstance(code, str)


def contract_symbol(contract, default=''):
    """取合約的 symbol。1.7 列舉合約時可能拿到只有 `code` 的輕量型別,
    這時退回用 code —— 對本專案的用途 (比對使用者輸入的代號) 兩者等價。
    """
    if contract is None:
        return default
    v = getattr(contract, 'symbol', None)
    if isinstance(v, str) and v:
        return v
    v = getattr(contract, 'code', None)
    if isinstance(v, str) and v:
        return v
    return default


def match_contract_code(contract, wanted):
    """這個合約是不是使用者要的那一檔。symbol 與 code 都比對。

    1.5.6 用 symbol、1.7 的輕量型別只有 code,兩邊都比才不會因為版本不同
    就「查無此代碼」。
    """
    w = str(wanted or '').strip().upper()
    if not w:
        return False
    for attr in ('symbol', 'code'):
        v = getattr(contract, attr, None)
        if isinstance(v, str) and v.strip().upper() == w:
            return True
    return False


def supported_kwargs(func, desired):
    """濾掉目標函式不吃的具名參數,回傳 (可用的 kwargs, 被丟掉的名稱)。

    用途:`login(contracts_timeout=10000)` 在 1.7 會 TypeError。與其偵測版本
    號,不如直接問這個函式收不收這個參數 —— 這樣連「某個中間版本部分支援」
    這種情況也自然處理掉。

    取不到簽名時 (C 擴充常見) 一律原樣回傳:寧可讓呼叫失敗並看到真正的
    TypeError,也不要靜悄悄地把使用者指定的參數丟掉。
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return dict(desired), []
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(desired), []
    ok, dropped = {}, []
    for k, v in (desired or {}).items():
        if k in params:
            ok[k] = v
        else:
            dropped.append(k)
    return ok, dropped


# ---------------------------------------------------------------------------
# 帳戶顯示 (ADR-117)
# ---------------------------------------------------------------------------
# 【1.7 的變更】1.5.6 用不同的類別區分帳戶 (StockAccount / FutureAccount),
# 1.7 把它們統一成同一個 `Account` 類別,改用 `account_type` 欄位區分
# (Stock / Future / Intl)。原本靠 `type(a).__name__` 判斷的寫法在 1.7 會對
# 每一個帳戶都得到空字串 —— 於是三個不同的帳戶在下拉選單裡長得一模一樣,
# 使用者根本無從選起 (實機回報的問題)。
ACCOUNT_KIND_ZH = {'Stock': '證券', 'Future': '期貨', 'Intl': '複委託'}


def account_kind(acc):
    """帳戶種類的英文代號 ('Stock'/'Future'/'Intl');判斷不出來回 ''。

    先讀 1.7 的 `account_type`,再退回 1.5.6 的類別名稱。
    """
    at = getattr(acc, 'account_type', None)
    name = getattr(at, 'name', None) or (str(at) if at is not None else '')
    name = str(name or '').strip()
    if name and name.lower() not in ('none', ''):
        # 1.7 的 enum 字串可能是 'AccountType.Stock' 這種形式,取最後一段
        return name.split('.')[-1]
    cls = type(acc).__name__.replace('Account', '').strip()
    return cls


def account_label(acc, acc_id=''):
    """下拉選單顯示的文字。

    **一定要帶帳號**:種類與戶名都可能重複 (同一個人有兩個證券戶就長一樣),
    只有帳號是唯一的。使用者要能一眼看出「這三個是不同的東西」。
    未簽署的帳戶標出來 —— 那種帳戶送單會被券商退,先看到比較好。
    """
    kind = account_kind(acc)
    kind_zh = ACCOUNT_KIND_ZH.get(kind, kind or '未知類別')
    aid = str(acc_id or getattr(acc, 'account_id', '') or '').strip()
    user = str(getattr(acc, 'username', '') or '').strip()
    parts = [f'{kind_zh} {aid}'.strip()]
    if user:
        parts.append(user)
    if getattr(acc, 'signed', None) is False:
        parts.append('⚠未簽署')
    return '｜'.join(parts)
