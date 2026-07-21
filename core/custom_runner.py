# -*- coding: utf-8 -*-
"""
core/custom_runner.py — 自訂策略的「子行程」執行入口。

【ADR-040】由 stock_app_pro.py 以 `python -m core.custom_runner` (帶逾時) 啟動。
用途:在獨立行程執行使用者策略,即使策略無窮迴圈/崩潰,主程式只要殺掉這個
子行程即可,不受影響。

協定 (單次執行):
  stdin  : JSON {source_code, records:[{ts,Open,High,Low,Close,Volume}...],
                 position, params}
  stdout : JSON {ok:true, decision:'BUY'/'SELL'/'CLOSE'/'HOLD'}  或
           JSON {ok:false, error:'...'}

刻意只依賴 pandas 與 core.custom_strategy,不 import 任何 GUI/broker 模組。
"""
import sys
import json


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({'ok': False, 'error': f'輸入解析失敗: {e}'}))
        return
    try:
        import pandas as pd
        from core import custom_strategy as cs
        recs = payload.get('records', [])
        if not recs:
            print(json.dumps({'ok': True, 'decision': 'HOLD'}))
            return
        idx = pd.to_datetime([r['ts'] for r in recs])
        df = pd.DataFrame({
            'Open': [r['Open'] for r in recs],
            'High': [r['High'] for r in recs],
            'Low': [r['Low'] for r in recs],
            'Close': [r['Close'] for r in recs],
            'Volume': [r.get('Volume', 0) for r in recs],
        }, index=idx)
        decision, ctx = cs.run_on_bar(payload['source_code'], df,
                                      payload.get('position', 'FLAT'),
                                      payload.get('params', {}),
                                      state=payload.get('state') or {},
                                      entry_price=payload.get('entry_price', 0.0),
                                      bars_in_position=payload.get('bars_in_position', 0),
                                      return_ctx=True)
        # 【ADR-044】state 回傳持久化;logs 給試跑/日誌顯示 (只收可 JSON 序列化的 state)
        try:
            state_out = json.loads(json.dumps(ctx.state))
        except (TypeError, ValueError):
            state_out = {}
        print(json.dumps({'ok': True, 'decision': decision, 'state': state_out, 'logs': ctx._logs}))
    except Exception as e:
        print(json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}))


if __name__ == '__main__':
    main()
