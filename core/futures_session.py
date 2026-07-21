"""
core.futures_session — 期貨日/週/月K「交易日」聚合 (見 DECISIONS.md ADR-007)。

不依賴 tkinter/self，也不做任何日誌或例外吞掉的動作；例外直接往上拋，
由呼叫端 (GUI 層) 決定要怎麼記錄與是否要退回自然日聚合當備援。
這樣核心聚合邏輯本身可以被單元測試直接驗證「是否拋出」與「拋出什麼」，
不需要 mock self.after/self.log_message。
"""
import numpy as np
import pandas as pd


# 【ADR-058】臺指期貨「盤後交易時段 (夜盤)」上線日。
# 這一天之前臺灣期貨市場「沒有夜盤」，所以任何早於此日的日K，不論資料源怎麼
# 標示，本質上都只有日盤 08:45-13:45；這一天之後才有 15:00-次日05:00 的夜盤。
# 回測期間若橫跨這一天，「近全 (全時段)」序列的定義會在中途改變 —— 這是市場
# 事實，不是資料錯誤，但必須讓使用者知道。
NIGHT_SESSION_START = pd.Timestamp('2017-05-15')

DAY_SESSION_OPEN_MIN = 8 * 60 + 45    # 08:45
DAY_SESSION_CLOSE_MIN = 13 * 60 + 45  # 13:45


def resample_future_session(sj_df: pd.DataFrame, tf: str, agg_dict: dict,
                            session_basis: str = 'all') -> pd.DataFrame:
    """
    期貨日/週/月K的「交易日 (session date)」聚合，取代對期貨錯誤的 resample('D')。

    規則 (詳見 ADR-007)：
      - 每根分K的時間 > 13:45 (即 15:00 起的夜盤) → 歸屬到「下一個交易日」。
      - 時間 <= 13:45 (凌晨 00:00-05:00 夜盤延續、或日盤 08:45-13:45) → 維持當天日期不變。
      - 日盤在交易日分組內的時間順序排在夜盤兩段之後，取 'last' 當 Close 會自然落在
        日盤 13:45 那筆 —— 這是「全時段收盤 (近全)」的日K收盤定義。

    【ADR-058】session_basis：
      * 'all' (預設)：近全，維持上述 ADR-007 行為，Open 來自夜盤開盤。
      * 'day' ：只取日盤 08:45-13:45 的分K，再依自然日分組。用途是讓
        「橫跨 2017-05-15 夜盤上線日」的長期回測有一致的口徑 —— 因為夜盤
        上線前根本沒有夜盤資料，用 'all' 會讓序列定義在中途改變 (隔夜跳空
        幅度會突然縮小數倍，等於策略在前後兩段面對的是不同的商品)。
        代價是放棄夜盤資訊，對只做日盤的策略反而更貼近實際。

    tf 為 "日K" 時直接回傳交易日日K；"周K"/"月K" 則先算出交易日日K，
    再對這個日K序列做 W-MON / MS 的二次聚合。
    """
    if sj_df.empty:
        return sj_df
    df = sj_df.copy()
    cutoff_minutes = DAY_SESSION_CLOSE_MIN  # 13:45
    idx_minutes = df.index.hour * 60 + df.index.minute
    if str(session_basis) == 'day':
        # 只留日盤:08:45 <= t <= 13:45,交易日即自然日 (日盤不跨日)
        keep = (idx_minutes >= DAY_SESSION_OPEN_MIN) & (idx_minutes <= DAY_SESSION_CLOSE_MIN)
        df = df[keep]
        if df.empty:
            return df
        df['session_date'] = df.index.normalize()
    else:
        shift_days = np.where(idx_minutes > cutoff_minutes, 1, 0)
        session_date = df.index.normalize() + pd.to_timedelta(shift_days, unit='D')
        df['session_date'] = session_date

    daily = df.groupby('session_date').agg(agg_dict)
    daily.index.name = None
    daily = daily.sort_index()

    if tf == "日K":
        return daily
    elif tf == "周K":
        weekly_agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        return daily.resample('W-MON', label='left', closed='left').agg(weekly_agg).dropna()
    else:  # 月K
        monthly_agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        return daily.resample('MS', label='left', closed='left').agg(monthly_agg).dropna()


def resample_natural_day_fallback(sj_df: pd.DataFrame, tf: str, agg_dict: dict) -> pd.DataFrame:
    """
    交易日聚合失敗時的備援：退回自然日 resample('D')。
    【注意】這對期貨而言已知會被夜盤污染 (ADR-007 的問題本身)，只當最後手段，
    呼叫端使用這個備援時務必記錄日誌提醒使用者資料可能不準確。
    """
    rule_map = {"日K": 'D', "周K": 'W-MON', "月K": 'MS'}
    return sj_df.resample(rule_map.get(tf, 'D'), label='left', closed='left').agg(agg_dict).dropna()
