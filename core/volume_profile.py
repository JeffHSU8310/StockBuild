# -*- coding: utf-8 -*-
"""
core/volume_profile.py — 用量價關係推算關鍵支撐/壓力點位 (ADR-102)

【方法】以成交量分布 (Volume Profile / Volume-at-Price) 為核心,這是「量價
關係」最正統的做法:把價格切成一格一格的價格箱,統計每個價位實際成交了多少
量,量堆得越高的價位代表越多人在那裡換手,價格再次回到該處時越容易遇到
支撐或壓力。

產出的點位:
  POC (Point of Control) — 成交量最大的價位,最強的單一支撐/壓力。
  VAH / VAL              — 價值區 (Value Area) 上下緣,預設涵蓋 70% 成交量,
                            是最常被用來當作區間邊界的兩條線。
  HVN (High Volume Node) — 高量節點,籌碼密集、盤整過的價位。
  LVN (Low Volume Node)  — 低量節點,成交真空帶,價格容易快速穿越 (不是支撐
                            壓力,但知道它在哪很有用:突破 LVN 常常一路走完)。
  轉折高低點             — swing high/low **且當時要有明顯放量**才採計;
                            沒有量能確認的轉折點在事後看常常只是雜訊。

【三種商品都適用】股票 / 指數 / 期貨的差別只在「價格箱要多寬」:
價格箱寬度一律以 tick_rules.get_tick() 為基準 (台積電 0.5 元、台指期 1 點、
低價股 0.01 元各自不同),輸出的點位也一律 round_to_tick 對齊合法價位
(鐵則 7:價格不可無腦 .2f,交界價位會出現非法檔位)。

【誠實聲明】支撐壓力不是精確科學。本模組只做「把成交量在價格上的分布算
出來」這件可驗證的事,並用透明、可測試的規則挑出節點;它不預測價格會不會
真的在那裡反轉。規則刻意保持簡單清楚 (比照 core/market_pattern.py 的原則),
使用者知道我在算什麼,勝過看起來很厲害的黑箱。

純邏輯:零 tkinter、零 shioaji、零網路,可離線單元測試 (鐵則 11)。
"""
import pandas as pd

from core import tick_rules

# 產出點位的類型
KIND_POC = 'POC'
KIND_VAH = 'VAH'
KIND_VAL = 'VAL'
KIND_HVN = 'HVN'
KIND_LVN = 'LVN'
KIND_SWING_HIGH = 'SwingHigh'
KIND_SWING_LOW = 'SwingLow'

KIND_LABELS = {
    KIND_POC: '控制點 (量最大)',
    KIND_VAH: '價值區上緣',
    KIND_VAL: '價值區下緣',
    KIND_HVN: '高量節點',
    KIND_LVN: '低量真空帶',
    KIND_SWING_HIGH: '放量轉折高',
    KIND_SWING_LOW: '放量轉折低',
}

ROLE_RESISTANCE = '壓力'
ROLE_SUPPORT = '支撐'
ROLE_CURRENT = '現價附近'

DEFAULT_BINS = 40
DEFAULT_VALUE_AREA = 0.70


def _clean(df):
    """取出乾淨的 OHLCV;缺欄位或空資料回 None。"""
    if df is None or len(df) == 0:
        return None
    need = ('High', 'Low', 'Close')
    if not all(c in df.columns for c in need):
        return None
    out = df[[c for c in ('Open', 'High', 'Low', 'Close', 'Volume') if c in df.columns]].copy()
    for c in out.columns:
        out[c] = pd.to_numeric(out[c], errors='coerce')
    out = out.dropna(subset=['High', 'Low', 'Close'])
    if 'Volume' not in out.columns:
        # 沒有量欄位時退化成「每根權重相同」——結果等同純價格分布,
        # 仍然有參考價值 (價格待過越久的區間箱數越多),但不是真正的量價關係,
        # 呼叫端可用 result['volume_weighted'] 判斷要不要提示使用者。
        out['Volume'] = 1.0
    out['Volume'] = out['Volume'].fillna(0.0).clip(lower=0.0)
    return out if len(out) else None


def compute_profile(df, asset_type='stock', raw_symbol='', bins=DEFAULT_BINS,
                    value_area_pct=DEFAULT_VALUE_AREA):
    """計算成交量分布。回傳 dict,無法計算時回 None。

    回傳內容:
      bin_edges   : 價格箱邊界 (長度 = len(bin_volumes) + 1)
      bin_centers : 每箱的中心價
      bin_volumes : 每箱的成交量
      poc / vah / val : 控制點與價值區上下緣 (價格)
      total_volume, volume_weighted, price_low, price_high

    【量的分攤方式】每根 K 棒的成交量**平均分攤到它 High~Low 涵蓋的所有價格
    箱**,而不是全部記在收盤價那一箱。後者實作簡單但會嚴重失真:一根從 100
    漲到 110 的長紅棒,成交是發生在整段路徑上的,不是全部發生在 110。
    """
    data = _clean(df)
    if data is None:
        return None
    lo = float(data['Low'].min())
    hi = float(data['High'].max())
    if not (hi > lo):
        return None

    tick = float(tick_rules.get_tick((hi + lo) / 2.0, asset_type, str(raw_symbol) or '')) or 0.01
    n_bins = max(4, int(bins))
    # 箱寬至少一個 tick——比 tick 還細的箱沒有意義 (那些價位根本不能成交),
    # 只會讓分布圖出現一堆永遠為 0 的空箱。
    width = max((hi - lo) / n_bins, tick)
    n_bins = max(1, int(round((hi - lo) / width)))
    edges = [lo + width * i for i in range(n_bins + 1)]
    if edges[-1] < hi:
        edges[-1] = hi
    volumes = [0.0] * n_bins

    for _, row in data.iterrows():
        h, l, v = float(row['High']), float(row['Low']), float(row['Volume'])
        if v <= 0:
            continue
        if h <= l:                      # 一字板/無波動:整根量給該價位所在的箱
            idx = min(n_bins - 1, max(0, int((l - lo) / width)))
            volumes[idx] += v
            continue
        i0 = min(n_bins - 1, max(0, int((l - lo) / width)))
        i1 = min(n_bins - 1, max(0, int((h - lo) / width)))
        span = i1 - i0 + 1
        share = v / span
        for i in range(i0, i1 + 1):
            volumes[i] += share

    total = sum(volumes)
    if total <= 0:
        return None
    centers = [(edges[i] + edges[i + 1]) / 2.0 for i in range(n_bins)]
    poc_idx = max(range(n_bins), key=lambda i: volumes[i])

    # 價值區:從 POC 往兩側擴,每次挑「相鄰兩側較大的那一箱」,直到累積量
    # 達到 value_area_pct。這是 Market Profile 的標準做法。
    target = total * float(value_area_pct)
    lo_i = hi_i = poc_idx
    acc = volumes[poc_idx]
    while acc < target and (lo_i > 0 or hi_i < n_bins - 1):
        left = volumes[lo_i - 1] if lo_i > 0 else -1.0
        right = volumes[hi_i + 1] if hi_i < n_bins - 1 else -1.0
        if right >= left:
            hi_i += 1
            acc += volumes[hi_i]
        else:
            lo_i -= 1
            acc += volumes[lo_i]

    return {
        'bin_edges': edges,
        'bin_centers': centers,
        'bin_volumes': volumes,
        'bin_width': width,
        'poc': centers[poc_idx],
        'poc_index': poc_idx,
        'val': edges[lo_i],
        'vah': edges[hi_i + 1],
        'value_area_pct': float(value_area_pct),
        'total_volume': total,
        'volume_weighted': 'Volume' in (df.columns if df is not None else []),
        'price_low': lo,
        'price_high': hi,
    }


def _local_extrema(volumes, min_gap=2):
    """找局部極大 / 極小的箱索引。

    min_gap:兩個節點至少要隔幾箱,避免相鄰箱互相干擾產出一堆黏在一起的線。

    判定用「兩側皆不小於 (不大於),且至少一側嚴格大於 (小於)」——若兩側都
    只用 >=,平坦區 (三箱等量) 會同時被判成極大又極大,產出「高量節點」與
    「低量真空帶」落在同一價位這種自相矛盾的結果 (實測踩過)。
    """
    n = len(volumes)
    highs, lows = [], []
    for i in range(1, n - 1):
        a, b, c = volumes[i - 1], volumes[i], volumes[i + 1]
        if b >= a and b >= c and (b > a or b > c):
            highs.append(i)
        elif b <= a and b <= c and (b < a or b < c):
            lows.append(i)

    def _thin(idxs, key_desc=True):
        picked = []
        for i in sorted(idxs, key=lambda x: volumes[x], reverse=key_desc):
            if all(abs(i - j) >= min_gap for j in picked):
                picked.append(i)
        return picked
    return _thin(highs, True), _thin(lows, False)


def swing_points(df, lookback=3, volume_mult=1.3):
    """找「有量能確認」的轉折高低點。回傳 (highs, lows),元素為價格。

    規則:第 i 根的 High 是前後 lookback 根裡的最大值,**且**該根成交量
    ≥ 區間平均量 × volume_mult 才採計。沒有量能確認的轉折點在事後看常常
    只是雜訊——這是「量價關係」的一部分,不是純價格型態。
    """
    data = _clean(df)
    if data is None or len(data) < lookback * 2 + 1:
        return [], []
    vol_mean = float(data['Volume'].mean()) or 0.0
    highs, lows = [], []
    n = len(data)
    hs, ls, vs = data['High'].values, data['Low'].values, data['Volume'].values
    for i in range(lookback, n - lookback):
        window = slice(i - lookback, i + lookback + 1)
        if vol_mean > 0 and vs[i] < vol_mean * float(volume_mult):
            continue
        if hs[i] == max(hs[window]):
            highs.append(float(hs[i]))
        if ls[i] == min(ls[window]):
            lows.append(float(ls[i]))
    return highs, lows


def _merge_close(levels, tol):
    """把彼此距離 < tol 的點位合併 (保留強度較高者,強度相加打折)。

    不合併的話,POC 與旁邊的 HVN、還有轉折點常常擠在一兩個 tick 內,
    畫到圖上就是一團看不清楚的線。
    """
    if not levels:
        return []

    def _compatible(a_kinds, b_kind):
        """LVN (成交真空帶) 與其他「籌碼密集」類型語意互斥,不可併成同一條。

        兩者併在一起會產出「低量真空帶 + 高量節點」這種自相矛盾的標籤,
        使用者看了只會困惑該價位到底有沒有量。
        """
        has_lvn = KIND_LVN in a_kinds
        if has_lvn != (b_kind == KIND_LVN):
            return False
        return True

    out = []
    for lv in sorted(levels, key=lambda x: -x['strength']):
        hit = next((o for o in out
                    if abs(o['price'] - lv['price']) < tol
                    and _compatible(o['kind_all'], lv['kind'])), None)
        if hit is None:
            out.append(dict(lv))
        else:
            # 兩個不同依據落在同一價位 → 該價位更可信,強度小幅加成
            hit['strength'] = min(1.0, hit['strength'] + lv['strength'] * 0.25)
            if lv['kind'] not in hit['kind_all']:
                hit['kind_all'] = hit['kind_all'] + [lv['kind']]
    return out


def find_levels(df, asset_type='stock', raw_symbol='', bins=DEFAULT_BINS,
                value_area_pct=DEFAULT_VALUE_AREA, max_levels=8,
                include_lvn=True, swing_lookback=3, swing_volume_mult=1.3):
    """算出關鍵支撐/壓力點位。回傳 dict:

      {'levels': [...], 'profile': {...}, 'current': 現價, 'tick': tick}

    每個 level:
      price     : 對齊 tick 的價位 (元 / 點)
      price_str : 依 fmt_price 格式化的字串 (鐵則 7)
      kind      : 主要依據 (POC/VAH/VAL/HVN/LVN/SwingHigh/SwingLow)
      kind_all  : 合併後包含的所有依據
      role      : 壓力 / 支撐 / 現價附近 (依現價比較)
      strength  : 0~1,相對強度 (量佔比為主)
      label     : 可直接顯示的中文說明
      distance_pct : 距離現價的百分比 (正=在上方)

    無法計算時回 None (資料太少 / 沒有量 / 價格全平)。
    """
    prof = compute_profile(df, asset_type=asset_type, raw_symbol=raw_symbol,
                           bins=bins, value_area_pct=value_area_pct)
    if prof is None:
        return None
    data = _clean(df)
    current = float(data['Close'].iloc[-1])
    sym = str(raw_symbol) or ''
    tick = float(tick_rules.get_tick(current, asset_type, sym)) or 0.01
    vols = prof['bin_volumes']
    centers = prof['bin_centers']
    vmax = max(vols) or 1.0

    # 支撐壓力候選 (籌碼密集類)。LVN 不放進來——它語意上是「成交真空帶」,
    # 不是支撐壓力,混在同一份清單裡排序會讓同一價位出現兩筆重複的線。
    raw = [
        {'price': prof['poc'], 'kind': KIND_POC, 'strength': 1.0},
        {'price': prof['vah'], 'kind': KIND_VAH, 'strength': 0.75},
        {'price': prof['val'], 'kind': KIND_VAL, 'strength': 0.75},
    ]
    hv_idx, lv_idx = _local_extrema(vols, min_gap=2)
    for i in hv_idx:
        if i == prof['poc_index']:
            continue
        raw.append({'price': centers[i], 'kind': KIND_HVN,
                    'strength': max(0.15, min(0.95, vols[i] / vmax))})
    sh, sl = swing_points(df, lookback=swing_lookback, volume_mult=swing_volume_mult)
    for p in sh:
        raw.append({'price': p, 'kind': KIND_SWING_HIGH, 'strength': 0.5})
    for p in sl:
        raw.append({'price': p, 'kind': KIND_SWING_LOW, 'strength': 0.5})

    for lv in raw:
        lv['kind_all'] = [lv['kind']]
    # 合併容差:兩個 tick 或半個價格箱,取大者
    tol = max(tick * 2, prof['bin_width'] * 0.5)
    merged = _merge_close(raw, tol=tol)

    def _pack(price_in, kind, kinds, strength):
        price = tick_rules.round_to_tick(float(price_in), asset_type, sym)
        if price <= 0:
            return None
        if abs(price - current) < tick:
            role = ROLE_CURRENT
        else:
            role = ROLE_RESISTANCE if price > current else ROLE_SUPPORT
        return {
            'price': price,
            'price_str': tick_rules.fmt_price(price, asset_type, sym),
            'kind': kind,
            'kind_all': list(kinds),
            'role': role,
            'strength': round(float(strength), 3),
            'label': ' + '.join(KIND_LABELS.get(k, k) for k in kinds),
            'distance_pct': round((price - current) / current * 100.0, 2) if current else 0.0,
        }

    out = []
    seen = set()
    for lv in sorted(merged, key=lambda x: -x['strength']):
        item = _pack(lv['price'], lv['kind'], lv['kind_all'], lv['strength'])
        if item is None or item['price'] in seen:
            continue          # round_to_tick 後可能撞在同一合法價位
        seen.add(item['price'])
        out.append(item)
        if len(out) >= max(1, int(max_levels)):
            break
    out = sorted(out, key=lambda x: -x['price'])   # 由上而下,對應圖上閱讀順序

    # 真空帶獨立輸出:畫圖時可用不同樣式 (淡色虛線),語意也不會跟支撐壓力混淆
    zones = []
    if include_lvn:
        for i in lv_idx:
            item = _pack(centers[i], KIND_LVN, [KIND_LVN], 0.25)
            if item and item['price'] not in seen and item['price'] not in {z['price'] for z in zones}:
                zones.append(item)
        zones = sorted(zones, key=lambda x: -x['price'])

    return {'levels': out, 'lvn_zones': zones, 'profile': prof,
            'current': current, 'tick': tick}


def summarize(result, top=3):
    """把結果整理成一行中文摘要,供日誌/狀態列顯示。"""
    if not result or not result.get('levels'):
        return "資料不足,無法計算支撐壓力"
    cur = result['current']
    res = [lv for lv in result['levels'] if lv['role'] == ROLE_RESISTANCE]
    sup = [lv for lv in result['levels'] if lv['role'] == ROLE_SUPPORT]
    res = sorted(res, key=lambda x: x['price'])[:top]
    sup = sorted(sup, key=lambda x: -x['price'])[:top]
    parts = []
    if res:
        parts.append("壓力 " + " / ".join(f"{lv['price_str']}" for lv in res))
    if sup:
        parts.append("支撐 " + " / ".join(f"{lv['price_str']}" for lv in sup))
    poc = result['profile']['poc']
    parts.append(f"POC {poc:.2f}")
    return f"現價 {cur:.2f}|" + "|".join(parts)
