# -*- coding: utf-8 -*-
"""
core/screener_backtest.py — 選股條件的歷史回測 (ADR-106)

回答的問題:「這組選股條件，過去選出來的股票表現如何?」

【這不是策略回測】`core/backtest.py` 回測的是「單一標的、逐根 K 棒進出場」;
這裡回測的是**投資組合**:每隔一段時間 (調倉日) 重新篩選一次，把符合條件的
前 N 檔等權買入，持有到下一個調倉日再換一批。兩者的問題本質不同，因此另寫
一個模組，而不是硬塞進 backtest.py。

技術面/籌碼面條件仍然重用 `strategy_engine.CONDITIONS` 與
`market_screener.screen`，語意與實際選股完全一致。

================================================================
【⚠️ 未來函數:本模組最重要的正確性議題】
================================================================

**基本面條件無法誠實回測。** 本地的 `fundamental.csv` 只存「當前快照」
(沒有 Date 欄，見 data/market_store 的設計說明)。用「今天的 EPS/本益比」去
篩選三個月前的股票，等於在三個月前就知道了尚未公布的財報——這會讓回測績效
嚴重虛高，而且是最難察覺的一種錯誤 (數字看起來完全合理)。

因此本模組**預設不套用基本面條件**，並在結果裡明確回報這件事
(`fundamental_skipped`)。使用者可用 `allow_lookahead_fundamental=True`
強制套用，但結果會標記 `has_lookahead=True`，呼叫端必須把警告顯示出來。

技術面與籌碼面沒有這個問題:日K 有完整歷史，逐日重算不會偷看未來;籌碼
沿用 ADR-101 的防護 (T 日只讀得到 T-1 日以前)。

【進場價用「訊號日的下一個交易日開盤」】訊號是用收盤價算出來的，若用同一根
的收盤價成交，等於「知道收盤價之後還能用收盤價買到」——那是做不到的。
用下一個交易日的開盤價才是實際可執行的價格。

純邏輯:零網路、零 tkinter、零 shioaji，可離線單元測試 (鐵則 11)。
"""
import pandas as pd

from core import market_screener
from core import cost_model
from core import fundamental_parser as fp

DEFAULT_REBALANCE_DAYS = 20      # 約一個月一次
DEFAULT_TOP_N = 10
DEFAULT_MIN_BARS = 60            # 技術指標要算得出來的最少根數


def trading_days(daily_df):
    """從全市場日K取出排序後的交易日清單。"""
    if daily_df is None or len(daily_df) == 0 or 'Date' not in daily_df.columns:
        return []
    return sorted({str(d) for d in daily_df['Date'].dropna()})


def rebalance_dates(days, rebalance_days, min_bars=DEFAULT_MIN_BARS):
    """挑出調倉日:從第 min_bars 個交易日開始，每 rebalance_days 取一個。

    前 min_bars 天不選股,因為那時技術指標 (如 60MA) 還算不出來——硬選會得到
    一堆「因為資料不足而條件不成立」的空結果,不是真實的策略表現。
    """
    if not days or rebalance_days <= 0:
        return []
    start = max(0, int(min_bars))
    return [days[i] for i in range(start, len(days), int(rebalance_days))]


def _slice_until(daily_df, date_iso):
    """只保留 <= date_iso 的資料——回測當下不可看到未來的 K 棒。"""
    d = daily_df['Date'].astype(str)
    return daily_df[d <= str(date_iso)]


def _next_trading_day(days, date_iso):
    for d in days:
        if d > str(date_iso):
            return d
    return None


def _price_on(daily_df, code, date_iso, field='Open'):
    """取某檔某日的價格;沒有該日資料回 None (當天可能停牌)。"""
    sub = daily_df[(daily_df['Code'].astype(str) == str(code)) &
                   (daily_df['Date'].astype(str) == str(date_iso))]
    if sub.empty:
        return None
    v = pd.to_numeric(sub.iloc[0].get(field), errors='coerce')
    if pd.isna(v):
        v = pd.to_numeric(sub.iloc[0].get('Close'), errors='coerce')
    return None if pd.isna(v) else float(v)


def run_screener_backtest(daily_df, conditions=None, fundamental_df=None,
                          fundamental_conds=None, industries=None,
                          start_iso=None, end_iso=None,
                          rebalance_days=DEFAULT_REBALANCE_DAYS,
                          top_n=DEFAULT_TOP_N, min_bars=DEFAULT_MIN_BARS,
                          initial_cash=1_000_000.0,
                          allow_lookahead_fundamental=False,
                          apply_cost=True, cost_params=None,
                          stock_chips=None, margin_chips=None,
                          to_ohlcv=None, progress_cb=None, should_stop=None):
    """執行選股回測。回傳 dict。

    回傳內容:
      periods              每個調倉期的明細 (調倉日/選到幾檔/該期報酬)
      holdings             每期實際持有的個股與損益
      equity               權益曲線 [(日期, 權益)]
      metrics              彙總指標
      has_lookahead        是否含未來函數 (基本面被強制套用時為 True)
      fundamental_skipped  基本面條件是否被跳過 (預設 True)
      warnings             要顯示給使用者的警告
    """
    out = {'periods': [], 'holdings': [], 'equity': [], 'metrics': {},
           'has_lookahead': False, 'fundamental_skipped': False, 'warnings': []}
    if daily_df is None or len(daily_df) == 0:
        out['warnings'].append('本地沒有全市場日K,無法回測')
        return out

    days = trading_days(daily_df)
    if start_iso:
        days = [d for d in days if d >= str(start_iso)]
    if end_iso:
        days = [d for d in days if d <= str(end_iso)]
    if len(days) < min_bars + 2:
        out['warnings'].append(
            f'交易日不足 ({len(days)} 天),至少需要 {min_bars + 2} 天'
            f'(前 {min_bars} 天要拿來暖身算指標)')
        return out

    # 【未來函數把關】基本面預設不套用
    f_conds = list(fundamental_conds or [])
    if f_conds and not allow_lookahead_fundamental:
        out['fundamental_skipped'] = True
        out['warnings'].append(
            f'已略過 {len(f_conds)} 個基本面條件:本地基本面只有「當前快照」'
            '沒有歷史,套用到過去的日期等於偷看尚未公布的財報,會讓回測績效'
            '嚴重虛高。本次僅回測技術面/籌碼面條件。')
        f_conds = []
    elif f_conds:
        out['has_lookahead'] = True
        out['warnings'].append(
            '⚠ 已強制套用基本面條件,但基本面只有當前快照——本結果含未來函數,'
            '實盤無法複製,僅供研究參考。')

    if not conditions and not f_conds:
        out['warnings'].append('沒有可回測的條件')
        return out

    reb = rebalance_dates(days, rebalance_days, min_bars)
    if not reb:
        out['warnings'].append('區間內沒有可用的調倉日')
        return out

    cash = float(initial_cash)
    equity_curve = []
    total_periods = len(reb)

    for pi, sig_date in enumerate(reb, 1):
        if should_stop is not None and should_stop():
            out['warnings'].append(f'使用者中止 (完成 {pi - 1}/{total_periods} 期)')
            break
        if progress_cb is not None:
            progress_cb(pi, total_periods)

        # 進場日 = 訊號日的下一個交易日 (用開盤價成交,見模組 docstring)
        entry_date = _next_trading_day(days, sig_date)
        if entry_date is None:
            break
        exit_sig = reb[pi] if pi < len(reb) else days[-1]
        exit_date = _next_trading_day(days, exit_sig) or days[-1]
        if exit_date <= entry_date:
            continue

        visible = _slice_until(daily_df, sig_date)      # 當下看得到的資料
        res = market_screener.screen(
            fundamental_df if fundamental_df is not None else _codes_frame(visible),
            visible, conditions=conditions, fundamental_conds=f_conds,
            industries=industries, stock_chips=stock_chips, margin_chips=margin_chips,
            min_bars=min_bars, max_results=int(top_n), to_ohlcv=to_ohlcv)
        picks = [r['code'] for r in res['rows']][:int(top_n)]

        period = {'signal_date': sig_date, 'entry_date': entry_date,
                  'exit_date': exit_date, 'picks': len(picks),
                  'pnl': 0.0, 'return_pct': 0.0, 'names': []}
        if not picks:
            equity_curve.append((exit_date, cash))
            out['periods'].append(period)
            continue

        # 等權配置
        per_stock = cash / len(picks)
        period_pnl = 0.0
        for code in picks:
            buy = _price_on(daily_df, code, entry_date, 'Open')
            sell = _price_on(daily_df, code, exit_date, 'Open')
            if buy is None or sell is None or buy <= 0:
                continue
            qty_shares = int(per_stock // buy)
            if qty_shares <= 0:
                continue
            gross = (sell - buy) * qty_shares
            fee = 0.0
            if apply_cost:
                try:
                    # round_trip_cost 回傳的是明細 dict,要取 'total'
                    # (手續費 + 交易稅);直接拿 dict 去減會 TypeError。
                    cost = cost_model.round_trip_cost(
                        '股票', code, buy, sell, qty_shares / 1000.0, 1000,
                        params=cost_params)
                    fee = float(cost.get('total', 0.0)) if isinstance(cost, dict) else float(cost)
                except Exception:
                    fee = 0.0
            pnl = gross - fee
            period_pnl += pnl
            nm = _name_of(daily_df, code)
            period['names'].append(nm or code)
            out['holdings'].append({
                'period': pi, 'signal_date': sig_date, 'entry_date': entry_date,
                'exit_date': exit_date, 'code': code, 'name': nm,
                'buy': buy, 'sell': sell, 'shares': qty_shares,
                'pnl': round(pnl, 2),
                'return_pct': round((sell - buy) / buy * 100.0, 2),
            })
        base = cash
        cash += period_pnl
        period['pnl'] = round(period_pnl, 2)
        period['return_pct'] = round(period_pnl / base * 100.0, 2) if base else 0.0
        out['periods'].append(period)
        equity_curve.append((exit_date, round(cash, 2)))

    out['equity'] = equity_curve
    out['metrics'] = _metrics(out['periods'], out['holdings'], equity_curve,
                              float(initial_cash), daily_df, days)
    return out


def _codes_frame(daily_df):
    """沒有基本面資料時,用日K裡出現過的代號臨時組一張「只有代號」的表,
    讓純技術面回測也能跑 (基本面欄位全空,基本面條件本來就已被略過)。"""
    codes = sorted({str(c) for c in daily_df['Code'].dropna()})
    return pd.DataFrame({fp.COL_CODE: codes, fp.COL_NAME: codes})


def _name_of(daily_df, code):
    sub = daily_df[daily_df['Code'].astype(str) == str(code)]
    if sub.empty or 'Name' not in sub.columns:
        return None
    v = sub.iloc[-1].get('Name')
    return None if pd.isna(v) else str(v)


def _buy_hold_pct(daily_df, days):
    """全市場等權買進持有的對照報酬 (拿來判斷策略有沒有贏過「隨便買」)。"""
    if len(days) < 2:
        return 0.0
    first, last = days[0], days[-1]
    a = daily_df[daily_df['Date'].astype(str) == first][['Code', 'Close']]
    b = daily_df[daily_df['Date'].astype(str) == last][['Code', 'Close']]
    if a.empty or b.empty:
        return 0.0
    m = a.merge(b, on='Code', suffixes=('_a', '_b'))
    m = m[(pd.to_numeric(m['Close_a'], errors='coerce') > 0)]
    if m.empty:
        return 0.0
    r = (pd.to_numeric(m['Close_b'], errors='coerce') /
         pd.to_numeric(m['Close_a'], errors='coerce') - 1.0) * 100.0
    return round(float(r.mean()), 2)


def _metrics(periods, holdings, equity, initial_cash, daily_df, days):
    if not periods:
        return {'periods': 0, 'total_pnl': 0.0, 'total_return_pct': 0.0,
                'win_rate': 0.0, 'max_drawdown_pct': 0.0, 'trades': 0,
                'avg_picks': 0.0, 'buy_hold_pct': 0.0, 'excess_pct': 0.0}
    total_pnl = sum(p['pnl'] for p in periods)
    wins = [h for h in holdings if h['pnl'] > 0]
    peak, max_dd = initial_cash, 0.0
    for _d, v in equity:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100.0)
    total_pct = total_pnl / initial_cash * 100.0 if initial_cash else 0.0
    bh = _buy_hold_pct(daily_df, days)
    return {
        'periods': len(periods),
        'total_pnl': round(total_pnl, 2),
        'total_return_pct': round(total_pct, 2),
        'win_rate': round(len(wins) / len(holdings) * 100.0, 2) if holdings else 0.0,
        'max_drawdown_pct': round(max_dd, 2),
        'trades': len(holdings),
        'avg_picks': round(sum(p['picks'] for p in periods) / len(periods), 1),
        'period_win_rate': round(
            sum(1 for p in periods if p['pnl'] > 0) / len(periods) * 100.0, 2),
        'best_period_pct': round(max((p['return_pct'] for p in periods), default=0.0), 2),
        'worst_period_pct': round(min((p['return_pct'] for p in periods), default=0.0), 2),
        'buy_hold_pct': bh,
        'excess_pct': round(total_pct - bh, 2),
    }


def summarize(result):
    """把回測結果整理成幾行中文摘要。"""
    if not result or not result.get('metrics'):
        return '無回測結果'
    m = result['metrics']
    lines = [
        f"調倉 {m['periods']} 期,共 {m['trades']} 筆持股,平均每期選 {m['avg_picks']} 檔",
        f"總損益 {m['total_pnl']:,.0f} 元 ({m['total_return_pct']:+.2f}%)",
        f"個股勝率 {m['win_rate']:.1f}% / 期間勝率 {m['period_win_rate']:.1f}%",
        f"最大回撤 {m['max_drawdown_pct']:.2f}%",
        f"全市場買進持有 {m['buy_hold_pct']:+.2f}% → 超額 {m['excess_pct']:+.2f}%",
    ]
    if result.get('has_lookahead'):
        lines.append('⚠ 本結果含未來函數 (基本面用當前快照),實盤無法複製')
    return '\n'.join(lines)
