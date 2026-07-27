# -*- coding: utf-8 -*-
"""
brokers/kgi_worker.py — 凱基子行程 (ADR-112)

**這支程式要用 Python 3.13 以下的直譯器執行**,不是主程式那一個。
理由見 P-59:`kgisuperpy` 自帶的 C 擴充只編到 cp313,而且載入時會拉進
沒有 3.14 版的 numba,在主程式的 3.14 直譯器裡根本 import 不了。

用法 (由 brokers/kgi_proxy.py 自動啟動,一般不需手動執行):
    /path/to/python3.13 -m brokers.kgi_worker

協定:stdin 一行一個 JSON 請求,stdout 一行一個 JSON 回應
     (core/broker_ipc.py 定義)。

【設計要點】
1. **重用 brokers/kgi.py 的 KGIBroker,不重寫委託轉換。** 委託意圖 → 凱基
   參數的對照表只能有一份;若在 worker 裡另抄一份,tests/test_brokers.py
   的 19 個測試就測不到真正跑在 production 的那份程式碼了。
2. **stdout 只准出現協定 JSON。** kgisuperpy 內部會 print (例如登入訊息、
   `print('輸入賬號錯誤')`),這些若混進 stdout 會把協定弄壞。因此啟動時
   就把 sys.stdout 換掉,程式其餘部分的 print 一律導到 stderr。
3. **一次處理一個請求。** 不做並行:凱基的帳號綁定本來就必須序列化
   (見 ADR-111),而且單執行緒讓「請求 → 回應」的對應關係毫無歧義。
"""
import os
import sys
import traceback

# 讓子行程能 import 專案模組 (它的工作目錄可能不是專案根目錄)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import broker_ipc  # noqa: E402


def _install_stdout_guard():
    """把真正的 stdout 保留給協定,其餘 print 全部改走 stderr。

    見設計要點 2:kgisuperpy 會直接 print,不隔開的話第一次登入就會讓
    主程式收到一行不是 JSON 的東西,協定當場錯位。
    """
    real = sys.stdout
    sys.stdout = sys.stderr
    return real


def _handle(broker, op, args):
    if op == 'ping':
        return {'pong': True, 'python': sys.version.split()[0]}
    if op == 'login':
        broker.login(person_id=args.get('person_id'),
                     person_pwd=args.get('person_pwd'),
                     simulation=bool(args.get('simulation', False)))
        return {'logged_in': True}
    if op == 'logout':
        broker.logout()
        return {'logged_in': False}
    if op == 'list_accounts':
        return {'accounts': [list(x) for x in broker.list_accounts()]}
    if op == 'place_order':
        oi = args.get('order_intent') or {}
        trade = broker.place_order(None, oi)
        # trade 物件不能直接 JSON 化,只回主程式真正需要的欄位。
        return {'status_text': broker.order_status_text(trade)}
    raise ValueError(f"不支援的操作: {op}")


def main():
    out = _install_stdout_guard()
    # 這裡才 import:import 失敗的訊息要能透過協定回給主程式,
    # 而不是讓行程直接死掉留下一個看不懂的 returncode。
    try:
        from brokers.kgi import KGIBroker
        broker = KGIBroker()
        init_error = None
    except Exception as e:                       # pragma: no cover
        broker = None
        init_error = f"{type(e).__name__}: {e}"

    for line in sys.stdin:
        try:
            req = broker_ipc.decode_line(line)
        except Exception as e:
            sys.stderr.write(f"[kgi_worker] 壞掉的請求: {e}\n")
            continue
        if req is None:
            continue
        rid = req.get('id', 0)
        op = req.get('op', '')
        try:
            if broker is None:
                raise RuntimeError(f"凱基 SDK 無法載入: {init_error}")
            result = _handle(broker, op, req.get('args') or {})
            out.write(broker_ipc.encode_response(rid, True, result=result))
        except Exception as e:
            # 完整 traceback 留在 stderr 給排查用;回給主程式的是一行人話。
            sys.stderr.write(f"[kgi_worker] {op} 失敗:\n{traceback.format_exc()}\n")
            out.write(broker_ipc.encode_response(
                rid, False, error=f"{type(e).__name__}: {e}"))
        out.flush()


if __name__ == '__main__':
    main()
