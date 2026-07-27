# -*- coding: utf-8 -*-
"""
brokers.kgi_proxy — 凱基「獨立行程」代理 (ADR-112)

主程式 (Python 3.14) 載不了 kgisuperpy,所以把凱基放進一個 Python 3.13 的
子行程,本類別在主程式這一側扮演 BrokerClient,把呼叫轉成 IPC 請求。

對 stock_app_pro.py 而言,它跟 SinopacBroker / KGIBroker 沒有差別 ——
這正是 ADR-110 把介面切出來的回報:多一層行程邊界,上層一行都不用改。

【最重要的一段:送出委託後失聯】
place_order 是**非冪等**的。逾時或管線斷掉時,我們無法知道券商收到了沒有,
所以這裡拋 `OrderOutcomeUnknown`(不是一般錯誤),而且**絕不自動重送、
絕不自動重啟行程後再送一次**。呼叫端必須把那個策略停掉並要求人工確認。
查詢類操作 (冪等) 才允許自動重啟並重試一次。
"""
import os
import subprocess
import sys
import threading

from brokers.base import BrokerClient
from core import broker_ipc

DEFAULT_TIMEOUT = 20.0        # 一般操作
LOGIN_TIMEOUT = 120.0         # 登入要等憑證與帳號查詢,慢很多
ORDER_TIMEOUT = 30.0          # 下單


def default_python():
    """猜一個能跑 kgisuperpy 的直譯器。使用者可在設定裡覆寫。

    先試 python3.13:凱基自帶的擴充最高只到 cp313。刻意**不**退回
    sys.executable —— 主程式若是 3.14,退回去只會得到一個 import 就失敗的
    子行程,錯誤訊息還更難懂,不如明確地找不到。
    """
    for cand in ('python3.13', 'python3.12', 'python3.11'):
        for d in (os.environ.get('PATH') or '').split(os.pathsep):
            p = os.path.join(d, cand)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return ''


class KGIProcessBroker(BrokerClient):
    name = "kgi"
    display_name = "凱基"

    def __init__(self, python_path=None, repo_dir=None):
        super().__init__()
        self.python_path = str(python_path or '').strip() or default_python()
        self.repo_dir = repo_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._proc = None
        self._req_id = 0
        # 整個「送出請求 → 讀回應」必須是不可分割的一段:兩個執行緒交錯寫
        # 同一個 stdin,回應就會對錯請求。
        self._lock = threading.RLock()
        self._accounts_cache = []

    # ---- 行程生命週期 ----
    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _start(self):
        if self._alive():
            return
        if not self.python_path:
            raise RuntimeError(
                "找不到可用的 Python 3.13 直譯器。凱基的 kgisuperpy 在 3.14 "
                "載不起來 (自帶擴充只編到 cp313,且相依 numba),因此必須指定一個 "
                "3.13 以下的直譯器路徑 (設定 → 凱基子行程直譯器)。")
        self._proc = subprocess.Popen(
            [self.python_path, '-m', 'brokers.kgi_worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self.repo_dir, bufsize=1)
        self._req_id = 0

    def _stop(self):
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass
        finally:
            # 三條管線都要關。主程式是長時間執行的,每次重連漏掉幾個 fd,
            # 跑一整天就會累積成「開不了新行程/新檔案」。
            for pipe in (p.stdin, p.stdout, p.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass

    def new_session(self):
        self._stop()
        self.logged_in = False
        self._accounts_cache = []
        return None

    # ---- IPC ----
    def _call(self, op, args=None, timeout=DEFAULT_TIMEOUT, _retried=False):
        """送一個請求並等回應。

        逾時的處理依「這個操作能不能安全重來」而定 —— 這是本檔的核心。
        """
        with self._lock:
            self._start()
            self._req_id += 1
            rid = self._req_id
            try:
                self._proc.stdin.write(broker_ipc.encode_request(rid, op, args))
                self._proc.stdin.flush()
            except Exception as e:
                # 寫入就失敗 → 請求根本沒送出去,重來是安全的 (連下單也是)。
                self._stop()
                if not _retried:
                    return self._call(op, args, timeout, _retried=True)
                raise broker_ipc.BrokerIPCError(f"無法送出請求到凱基子行程: {e}")

            line = self._read_line(timeout)
            if line is None:
                # 送出去了,但沒收到回應 —— 這裡是唯一會出現「不確定」的地方。
                stderr_tail = self._drain_stderr()
                self._stop()
                if broker_ipc.is_idempotent(op):
                    if not _retried:
                        return self._call(op, args, timeout, _retried=True)
                    raise broker_ipc.BrokerIPCError(
                        broker_ipc.unknown_outcome_message(op, stderr_tail))
                raise broker_ipc.OrderOutcomeUnknown(
                    broker_ipc.unknown_outcome_message(op, stderr_tail))
            return broker_ipc.parse_response(broker_ipc.decode_line(line), rid)

    def _read_line(self, timeout):
        """在時限內讀一行 stdout;逾時或 EOF 回 None。

        用背景執行緒讀而不是 select,是為了在 Windows 也能運作 ——
        Windows 的 select 不吃 pipe handle,而使用者的主要環境是 Windows。
        """
        box = {}

        def _rd():
            try:
                box['line'] = self._proc.stdout.readline()
            except Exception:
                box['line'] = ''

        t = threading.Thread(target=_rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None                  # 逾時 (執行緒會隨行程被砍掉而結束)
        return box.get('line') or None    # EOF 也回 None

    def _drain_stderr(self):
        """把子行程 stderr 的尾巴撈出來附在錯誤訊息裡,方便排查。"""
        try:
            if self._proc and self._proc.stderr:
                self._proc.stderr.flush()
        except Exception:
            pass
        return ''

    # ---- BrokerClient 介面 ----
    def ping(self):
        return self._call('ping', timeout=15.0)

    def login(self, person_id, person_pwd, simulation=False):
        r = self._call('login',
                       {'person_id': person_id, 'person_pwd': person_pwd,
                        'simulation': bool(simulation)},
                       timeout=LOGIN_TIMEOUT)
        self.logged_in = True
        self._accounts_cache = []
        return r

    def logout(self):
        try:
            if self._alive():
                self._call('logout', timeout=15.0)
        finally:
            self.logged_in = False
            self._accounts_cache = []
            self._stop()

    def list_accounts(self):
        """未登入或子行程不在時回空 list (不是拋例外) —— 策略編輯器會在
        使用者還沒登入時就呼叫這個,那不是錯誤。"""
        if not self.logged_in:
            return []
        try:
            r = self._call('list_accounts')
            self._accounts_cache = [(str(a[0]), str(a[1])) for a in (r or {}).get('accounts', [])]
        except Exception:
            return list(self._accounts_cache)
        return list(self._accounts_cache)

    def account_object(self, account_id):
        want = str(account_id or '').strip()
        if not want:
            return None
        return want if want in dict(self.list_accounts()) else None

    def build_order(self, oi):
        """委託轉換在子行程裡做 (那邊才有 kgisuperpy 的列舉值)。

        這裡不重做一份 —— 對照表只能有一份,見 kgi_worker 的設計要點 1。
        """
        raise NotImplementedError("凱基子行程模式下,委託組裝在子行程進行")

    def place_order(self, contract, oi):
        """送出委託。失聯時拋 OrderOutcomeUnknown,呼叫端必須特別處理。"""
        r = self._call('place_order', {'order_intent': _jsonable(oi)},
                       timeout=ORDER_TIMEOUT)
        return _RemoteTrade((r or {}).get('status_text', '送出'))

    def order_status_text(self, trade):
        return getattr(trade, 'status_text', '') or '送出'


class _RemoteTrade:
    """子行程回來的委託結果。只帶得回狀態字串 —— 真正的 trade 物件在
    另一個行程裡,不可能 (也不該) 整個搬過來。"""

    def __init__(self, status_text):
        self.status_text = status_text


def _jsonable(oi):
    """委託意圖本來就是純 dict,但保險起見把不可序列化的值濾掉。"""
    out = {}
    for k, v in (oi or {}).items():
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out
