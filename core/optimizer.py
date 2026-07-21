# -*- coding: utf-8 -*-
"""
core/optimizer.py — 策略參數最佳化 (網格搜尋 Grid Search + 隨機搜索 Random Search)

【ADR-054】使用者需求:不想每次都回去改程式碼裡的參數,而且希望系統能
自動找出「績效最好」的參數組合。

【ADR-056】網格搜尋要求使用者自己列出候選值或窄範圍 (受 500 組上限限制),
範圍一寬 (例如 fast=3:50 x slow=10:200 = 8930 組) 就直接被擋下,使用者還是
得自己手動窄化——這正是「不是我先預設好要的參數」這個抱怨的根源。新增
「隨機搜索」模式:使用者只給「下限:上限」,系統在範圍內隨機抽樣跑 N 次,
不保證找到全域最佳 (畢竟不是窮舉),但能在合理次數內探索遠大於 500 組的
空間。兩種模式回傳結構完全相同,GUI 共用同一套顯示邏輯。

作法 (誠實說明,不誇大):
  * 網格搜尋:把每個參數的候選值全排列組合,逐一跑回測,依目標指標排名。
  * 隨機搜索:給定範圍,隨機抽樣 N 組各跑一次回測,依目標指標排名。
  * 兩者都是「在歷史資料上找最好」,必然帶有過度最佳化 (overfitting) 風險:
    組合/次數越多、樣本越少,選出來的參數越可能只是運氣好。因此本模組:
      - 網格強制上限 max_combos (預設 500)、隨機搜索強制上限 max_trials (預設 300);
      - 回傳每組的完整指標 (不是只有一個分數),讓使用者自己看穩健性;
      - 內建 min_trades 過濾:交易筆數太少的組合不列入最佳 (統計上無意義);
      - 提供 split_ratio 樣本內/外檢定:前段最佳化、後段驗證,揭露落差。
  * 純邏輯、零 UI、零券商依賴,可離線測試。

參數規格 (spec) 格式 (網格搜尋 parse_param_spec):
    "fast=5,7,10; slow=20,25,30"      → {'fast': [5,7,10], 'slow': [20,25,30]}
    也支援 range 語法 "fast=5:15:2"    → 5,7,9,11,13 (start:stop:step,含頭不含尾)

參數範圍格式 (隨機搜索 parse_param_ranges):
    "fast=3:50; slow=10:200"          → {'fast': (3,50,整數), 'slow': (10,200,整數)}
"""
import copy
import itertools
import random

from . import backtest as _backtest


def parse_param_spec(spec):
    """把文字參數規格解析成 {名稱: [候選值,...]}。格式錯誤拋 ValueError。"""
    text = str(spec or '').strip()
    if not text:
        raise ValueError("參數範圍不可空白,例如:fast=5,7,10; slow=20,25,30")
    grid = {}
    for part in text.replace('\n', ';').split(';'):
        part = part.strip()
        if not part:
            continue
        if '=' not in part:
            raise ValueError(f"參數格式錯誤 (缺少 =):{part}")
        name, vals = part.split('=', 1)
        name = name.strip()
        vals = vals.strip()
        if not name:
            raise ValueError(f"參數名稱空白:{part}")
        if ':' in vals:
            try:
                nums = [float(x) for x in vals.split(':')]
            except ValueError:
                raise ValueError(f"range 語法需為數字 start:stop:step → {part}")
            if len(nums) not in (2, 3):
                raise ValueError(f"range 語法應為 start:stop 或 start:stop:step → {part}")
            start, stop = nums[0], nums[1]
            step = nums[2] if len(nums) == 3 else 1.0
            if step == 0:
                raise ValueError(f"step 不可為 0 → {part}")
            out, cur = [], start
            while (cur < stop) if step > 0 else (cur > stop):
                out.append(_num(cur))
                cur += step
            if not out:
                raise ValueError(f"range 產生 0 個值 → {part}")
            grid[name] = out
        else:
            items = [v.strip() for v in vals.split(',') if v.strip()]
            if not items:
                raise ValueError(f"參數沒有候選值:{part}")
            grid[name] = [_num(v) for v in items]
    if not grid:
        raise ValueError("沒有解析到任何參數")
    return grid


def _num(v):
    """字串轉數字 (能轉 int 就用 int,保持參數乾淨);轉不動就原樣保留字串。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if abs(f - int(f)) < 1e-12 else f


def count_combos(grid):
    n = 1
    for vals in grid.values():
        n *= max(1, len(vals))
    return n


def iter_combos(grid):
    """產生所有參數組合 (dict)。"""
    names = list(grid.keys())
    for combo in itertools.product(*[grid[n] for n in names]):
        yield dict(zip(names, combo))


# 可排序的目標指標:名稱 → (metrics 鍵, 越大越好?)
OBJECTIVES = {
    '淨損益': ('total_pnl', True),
    '獲利因子': ('profit_factor', True),
    '夏普比率': ('sharpe', True),
    '期望值/筆': ('expectancy', True),
    '勝率': ('win_rate', True),
    '最大回撤(越小越好)': ('max_drawdown', False),
}


def _eval_combo(strategy, df, combo, key, bigger_better, min_trades,
                slippage_ticks, tick_size, cost_params):
    """對「一組參數」跑一次回測並包成 result 項。網格/隨機搜索共用同一份邏輯,
    避免兩套實作對「怎麼算 score / 怎麼判斷 eligible」出現不一致。"""
    s = copy.deepcopy(strategy)
    merged = dict(s.get('custom_params') or {})
    merged.update(combo)
    s['custom_params'] = merged
    try:
        r = _backtest.run_backtest(s, df, slippage_ticks=slippage_ticks,
                                   tick_size=tick_size, cost_params=cost_params)
        m = r['metrics']
    except Exception as e:
        m = {'error': f"{type(e).__name__}: {e}", 'trades': 0, 'total_pnl': 0.0}
    raw = m.get(key, 0.0)
    if raw == float('inf'):
        raw = 1e12
    eligible = (m.get('trades', 0) >= min_trades) and ('error' not in m)
    return {'params': combo, 'metrics': m,
            'score': float(raw) if bigger_better else -float(raw),
            'eligible': eligible}


def _summarize_errors(results, evaluated):
    """【ADR-055】把每組例外彙總成一句話,不要讓「跑完但沒數據」變成沒頭沒尾。"""
    errors = [r['metrics']['error'] for r in results if 'error' in r['metrics']]
    error_summary = ''
    if errors:
        uniq = sorted(set(errors))
        error_summary = (f"{len(errors)}/{evaluated} 組回測發生錯誤;"
                         f"最常見: {uniq[0]}" + (f" (另有 {len(uniq) - 1} 種)" if len(uniq) > 1 else ""))
    return len(errors), error_summary


def optimize(strategy, df, param_grid, objective='淨損益', min_trades=5,
             max_combos=500, progress_cb=None, cost_params=None, slippage_ticks=0,
             tick_size=None, should_stop=None):
    """
    網格搜尋。回傳 dict:
      {'results': [...依目標排序...], 'objective':..., 'total': N, 'evaluated': M,
       'filtered_out': K, 'best': 最佳項或 None}
    每個 result 項: {'params': {...}, 'metrics': {...}, 'score': float, 'eligible': bool}

    should_stop(): 回呼,回傳 True 就中止 (GUI 取消鈕用)。
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"不支援的目標指標: {objective}")
    key, bigger_better = OBJECTIVES[objective]
    total = count_combos(param_grid)
    if total > max_combos:
        raise ValueError(f"參數組合共 {total} 組,超過上限 {max_combos} 組。"
                         f"請縮小候選值範圍,或改用「隨機搜索」模式 —— 組合越多越容易挑到只是運氣好的參數。")

    results = []
    evaluated = 0
    for combo in iter_combos(param_grid):
        if should_stop and should_stop():
            break
        r = _eval_combo(strategy, df, combo, key, bigger_better, min_trades,
                        slippage_ticks, tick_size, cost_params)
        evaluated += 1
        results.append(r)
        if progress_cb:
            try: progress_cb(evaluated, total, combo, r['metrics'])
            except Exception: pass

    results.sort(key=lambda x: (x['eligible'], x['score']), reverse=True)
    best = next((r for r in results if r['eligible']), None)
    n_err, error_summary = _summarize_errors(results, evaluated)
    return {'results': results, 'objective': objective, 'total': total,
            'evaluated': evaluated,
            'filtered_out': sum(1 for r in results if not r['eligible']),
            'errors': n_err, 'error_summary': error_summary,
            'best': best}


# ============================================================
# 【ADR-056】隨機搜索:使用者給「範圍」,系統自己抽樣嘗試,不必先列好候選值
# ============================================================

def parse_param_ranges(spec):
    """
    把 "fast=3:50; slow=10:200" 解析成 {名稱: (下限, 上限, 是否整數)}。
    只接受 min:max 兩段 (不像 parse_param_spec 支援逗號列舉或 start:stop:step)——
    隨機搜索本來就是要讓使用者「給範圍就好,不用自己列值」,語法刻意單純。
    兩個數字都是整數 (無小數點) 才視為整數參數,否則視為浮點數。
    """
    text = str(spec or '').strip()
    if not text:
        raise ValueError("參數範圍不可空白,例如:fast=3:50; slow=10:200")
    ranges = {}
    for part in text.replace('\n', ';').split(';'):
        part = part.strip()
        if not part:
            continue
        if '=' not in part:
            raise ValueError(f"參數格式錯誤 (缺少 =):{part}")
        name, vals = part.split('=', 1)
        name = name.strip()
        vals = vals.strip()
        if not name:
            raise ValueError(f"參數名稱空白:{part}")
        if ':' not in vals:
            raise ValueError(f"隨機搜索需要「下限:上限」格式,例如 fast=3:50 → {part}")
        pieces = vals.split(':')
        if len(pieces) != 2:
            raise ValueError(f"隨機搜索只接受 下限:上限 (不含 step) → {part}")
        try:
            lo, hi = float(pieces[0]), float(pieces[1])
        except ValueError:
            raise ValueError(f"下限/上限需為數字 → {part}")
        if lo >= hi:
            raise ValueError(f"下限必須小於上限 → {part}")
        is_int = pieces[0].strip().lstrip('-').isdigit() and pieces[1].strip().lstrip('-').isdigit()
        ranges[name] = (lo, hi, is_int)
    if not ranges:
        raise ValueError("沒有解析到任何參數範圍")
    return ranges


def random_search(strategy, df, param_ranges, n_trials=60, objective='淨損益', min_trades=5,
                  max_trials=300, progress_cb=None, cost_params=None, slippage_ticks=0,
                  tick_size=None, should_stop=None, seed=None):
    """
    在給定範圍內隨機抽樣 n_trials 組參數各跑一次回測 —— 使用者不必自己列出
    候選值,只要給「下限:上限」,系統自己找。這是應對「範圍寬,網格組合數
    爆炸 (fast=3:50 x slow=10:200 就有 8930 組,遠超網格搜尋 500 組上限)」的
    務實做法:犧牲窮舉的完整性,換取能在合理次數內探索寬範圍。

    ⚠ 與網格搜尋一樣有過度最佳化風險,而且因為是抽樣,連「這個範圍裡最好的
    組合」都不保證找到 (只是找到「抽到的裡面最好的」)。次數越多,越接近
    窮舉,但也越慢——這是準確度與時間的取捨,不是免費的午餐。

    回傳結構與 optimize() 完全相同,GUI 可以共用同一套顯示/套用邏輯。
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"不支援的目標指標: {objective}")
    if n_trials > max_trials:
        raise ValueError(f"嘗試次數 {n_trials} 超過上限 {max_trials},請調低。")
    key, bigger_better = OBJECTIVES[objective]
    rng = random.Random(seed)

    def _sample_one():
        combo = {}
        for name, (lo, hi, is_int) in param_ranges.items():
            if is_int:
                combo[name] = rng.randint(int(lo), int(hi))
            else:
                combo[name] = round(rng.uniform(lo, hi), 4)
        return combo

    results = []
    evaluated = 0
    for _ in range(n_trials):
        if should_stop and should_stop():
            break
        combo = _sample_one()
        r = _eval_combo(strategy, df, combo, key, bigger_better, min_trades,
                        slippage_ticks, tick_size, cost_params)
        evaluated += 1
        results.append(r)
        if progress_cb:
            try: progress_cb(evaluated, n_trials, combo, r['metrics'])
            except Exception: pass

    results.sort(key=lambda x: (x['eligible'], x['score']), reverse=True)
    best = next((r for r in results if r['eligible']), None)
    n_err, error_summary = _summarize_errors(results, evaluated)
    return {'results': results, 'objective': objective, 'total': n_trials,
            'evaluated': evaluated,
            'filtered_out': sum(1 for r in results if not r['eligible']),
            'errors': n_err, 'error_summary': error_summary,
            'best': best}


def walk_forward_check(strategy, df, params, split_ratio=0.7, **kw):
    """
    樣本內/外檢定:用前 split_ratio 的資料當「樣本內」,其餘當「樣本外」,
    對同一組參數各跑一次回測,揭露績效落差 (落差大 = 過度最佳化的警訊)。
    回傳 {'in_sample': metrics, 'out_sample': metrics, 'split_index': i}
    """
    n = len(df)
    i = int(n * float(split_ratio))
    if n < 20 or i < 10 or (n - i) < 10:
        raise ValueError("資料量太少,無法做樣本內/外檢定 (至少需 20 根,兩段各 10 根)")
    s = copy.deepcopy(strategy)
    merged = dict(s.get('custom_params') or {})
    merged.update(params or {})
    s['custom_params'] = merged
    kw.pop('objective', None)
    m_in = _backtest.run_backtest(s, df.iloc[:i], **kw)['metrics']
    m_out = _backtest.run_backtest(s, df.iloc[i:], **kw)['metrics']
    return {'in_sample': m_in, 'out_sample': m_out, 'split_index': i}
