# -*- coding: utf-8 -*-
"""
core/kbars_plan.py — 「這個 kbars 請求要不要分段、每段幾天」的單一出處 (ADR-122)

【為什麼要有這一層】shioaji 的 `kbars()` **一律回 1 分 K**,日/周/月K 都是
本地重採樣出來的 —— 所以「要幾天」直接等於「要多少筆分K」。範圍一大,
單次請求就容易在券商端逾時 (ADR-069 對主圖已經下過這個結論,ADR-121 的
使用者實例則是策略路徑踩到同一類問題)。

門檻與段長原本只寫在 `stock_app_pro.fetch_data_worker` 裡面的字面值,
策略路徑要用同一套規則時,只能把數字再抄一份 —— 而「兩份清單各自維護、
遲早分歧」正是 P-67 的坑。收斂到這裡當唯一出處。

【設計原則】
1. 純函式、零依賴,可離線測試 (鐵則 11)。
2. **保守**:認不得的週期一律當成「日K 類」處理 —— 寧可多切幾段 (慢一點
   但會成功),也不要拿一個未知週期去發一個可能逾時的大請求。
"""

# 分K 類週期:同樣的天數,分K 的資料量遠大於日K 以上 (日K 是重採樣出來的,
# 原始資料量一樣,但分K 週期本身要的天數少,兩者的門檻因此不同)。
MIN_TFS = frozenset({'1分K', '5分K', '15分K', '30分K', '60分K'})

# 門檻 = 超過這個天數就要分段;段長 = 每段幾天。
# 這四個數字與主圖 fetch_data_worker 內的字面值必須一致 (diag 有原始碼層級
# 斷言守著);改這裡就要一起改那裡,反之亦然。
MIN_TF_THRESHOLD_DAYS = 5
MIN_TF_CHUNK_DAYS = 10
MIN_TF_SUBSPLIT_DAYS = 5          # 段內再切小段搶救用 (見 _download_kbars_chunked)

DAY_TF_THRESHOLD_DAYS = 90
DAY_TF_CHUNK_DAYS = 90
DAY_TF_SUBSPLIT_DAYS = 30


def is_min_timeframe(tf):
    """是不是分K 類週期。認不得的一律回 False (當日K 類處理,見模組說明)。"""
    return str(tf or '').strip() in MIN_TFS


def chunk_plan(tf, days):
    """要不要分段?回 dict {'chunk_days','subsplit_days','segments'};不需要回 None。

    days 是「請求涵蓋的日曆天數」(end - start)。回傳的 segments 是**預估**
    段數,給呼叫端記日誌/寫測試用,不是下載器真正的切法依據 —— 真正的切法
    由 _download_kbars_chunked 依 chunk_days 逐段推進 (它每段之後會多跳一天,
    所以實際段數可能比這裡少一段,估算刻意取「不會低估」的那一邊)。
    """
    try:
        d = int(days)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    if is_min_timeframe(tf):
        chunk, sub = MIN_TF_CHUNK_DAYS, MIN_TF_SUBSPLIT_DAYS
    else:
        chunk, sub = DAY_TF_CHUNK_DAYS, DAY_TF_SUBSPLIT_DAYS
    # 【只看「切得出幾段」,不看門檻】過了門檻不等於真的會被切開:分K 的
    # 門檻是 5 天、段長卻是 10 天,所以 6~10 天全都是「過門檻但只有一段」,
    # 而 5分K 的實際取數天數正好是 7 天 —— 第一版照門檻判,就把使用者每天
    # 在跑的 5分K 也改成走背景預抓了 (P-91)。
    #
    # 因此這裡刻意**不**再寫一次 `if d <= threshold: return None`:在目前的
    # 參數下 (threshold <= chunk) 那句永遠不會改變結果,是一條測不到的死枝。
    # threshold 常數保留下來只有一個用途 —— 給診斷比對主圖的字面值,確保
    # 兩邊的規則沒有改岔。實際效果上兩邊仍一致:主圖對 6~10 天雖然會走
    # 分段函式,但 chunk_days=10 也只會切出一段,跟單次下載相同。
    segments = -(-d // chunk)
    if segments <= 1:
        return None
    return {'chunk_days': chunk, 'subsplit_days': sub, 'segments': segments}
