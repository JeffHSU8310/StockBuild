# -*- coding: utf-8 -*-
"""
core/telegram_control.py — Telegram 遠端控制的純邏輯層 (ADR-108)

把既有的 Telegram 通知 (ADR-090,單向發送) 擴充成**雙向**:手機可以下指令
查詢狀態、停用/啟用策略、緊急全停。

【設計原則,同 core/telegram_notify.py】零 tkinter / 零 shioaji / 零網路:
本模組只負責「解析指令」「判斷該不該執行」「組回覆文字」三件純邏輯,
真正的 HTTP 輪詢與狀態變更在 GUI 層。因此整套授權與確認機制都可以離線
完整測試——這是**能遠端控制真實下單系統**的功能,絕不允許沒測過就上。

================================================================
【安全設計:這是本模組存在的主要理由】
================================================================

手機能控制一個會真實下單的系統,代表手機遺失、Telegram 帳號被盜、或單純
誤觸,都可能造成金錢損失。因此:

1. **只接受設定檔裡那一個 chat_id**。別人就算知道 Bot 名稱、加了這個 Bot,
   指令也會被拒絕並記錄——授權是白名單,不是黑名單。

2. **啟用類指令 (`/on`、`/start_all`) 需要二次確認**:先回一個 4 碼確認碼,
   使用者要在時限內回傳 `/yes <碼>` 才真的執行。誤觸不會直接讓系統開始下單。
   停用類 (`/off`、`/stop_all`) **不需要確認**——那是往安全方向走,拖延反而
   有害 (使用者想緊急停止時,不該還要多打一則訊息)。

3. **完全不提供下單/改單指令**。要買賣一律回到有畫面的主程式,走鐵則 14 的
   確認視窗。遠端只能控制「策略要不要跑」,不能直接動錢。

4. 所有指令 (含被拒絕的) 都回傳結果並要求呼叫端記錄到系統日誌。
"""
import random
import string
import time

from core import paper_account

# 指令分類:決定要不要二次確認
CMD_READONLY = 'readonly'      # 查詢類,零風險
CMD_SAFE_WRITE = 'safe_write'  # 停用類,往安全方向,不需確認
CMD_RISKY = 'risky'            # 啟用類,會讓系統開始下單,需二次確認

CONFIRM_TTL_SEC = 120          # 確認碼有效期
CONFIRM_CODE_LEN = 4

COMMANDS = {
    '/status':    (CMD_READONLY, '查詢系統狀態 (登入/總開關/啟用中策略數)'),
    '/list':      (CMD_READONLY, '列出所有策略與其啟用狀態'),
    '/positions': (CMD_READONLY, '/positions [帳號] 查詢持倉 (不帶=全部帳戶)'),
    '/pnl':       (CMD_READONLY, '/pnl [帳號] 查詢今日損益 (不帶=全部帳戶)'),
    '/accounts':  (CMD_READONLY, '列出所有模擬帳戶 (查詢時可用編號或名稱)'),
    '/strategy':  (CMD_READONLY, '/strategy <策略名或編號> 查該策略的完整設定與狀態'),
    '/off':       (CMD_SAFE_WRITE, '/off <策略名或編號> 停用某策略'),
    '/stop_all':  (CMD_SAFE_WRITE, '緊急停止:關閉自動交易總開關'),
    '/on':        (CMD_RISKY, '/on <策略名或編號> 啟用某策略 (需確認)'),
    '/start_all': (CMD_RISKY, '開啟自動交易總開關 (需確認)'),
    '/yes':       (CMD_READONLY, '/yes <確認碼> 確認執行上一個待確認指令'),
    '/help':      (CMD_READONLY, '顯示可用指令'),
}


def control_status(cfg):
    """【ADR-138】回傳 (是否啟用, 給人看的原因)。

    起因:使用者回報「我在 Telegram 打 /help 完全沒有回應,但系統傳給我的
    訊息我收得到」。查下來不是 Telegram 那邊的問題,而是**通知與遠端控制是
    兩個獨立開關**(ADR-108 刻意這樣設計:通知是唯讀推播,遠端控制能讓系統
    開始下單,風險等級不同)。只勾「啟用 Telegram 通知」的話,輪詢執行緒
    根本不會建立,打什麼指令都不會有回應。

    而當時**連一行日誌都沒有** —— 沒開就靜悄悄什麼都不做,使用者無從得知
    自己少勾了一個框。這個函式就是要讓那個狀態說得出口 (P-02 的精神:
    沒有的東西要明講,不要靜默)。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    has_token = bool(str(cfg.get('bot_token', '')).strip())
    has_chat = bool(str(cfg.get('chat_id', '')).strip())
    if not cfg.get('remote_control'):
        return False, ("未啟用手機遠端控制 —— 這與「啟用 Telegram 通知」是兩個"
                       "獨立的勾選框,只勾通知的話手機下指令不會有任何回應。"
                       "請到「Telegram 設定」勾選「⚠️ 啟用手機遠端控制」。")
    if not has_token or not has_chat:
        missing = '、'.join([x for x, ok in (('Bot Token', has_token),
                                             ('Chat ID', has_chat)) if not ok])
        return False, f"已勾選遠端控制,但 {missing} 沒有填,無法啟動輪詢。"
    return True, f"已啟用手機遠端控制;只有 Chat ID {str(cfg.get('chat_id')).strip()} 下的指令會被接受。"


def is_authorized(chat_id, allowed_chat_id):
    """只有設定檔裡那一個 chat_id 可以下指令。

    刻意用字串精確比對:Telegram 的 chat_id 可能是負數 (群組) 或很長的數字,
    型別轉換容易出錯,而這裡出錯的後果是「別人能控制我的交易系統」。
    """
    a = str(chat_id or '').strip()
    b = str(allowed_chat_id or '').strip()
    return bool(a) and bool(b) and a == b


def parse_command(text):
    """把訊息文字解析成 (指令, 參數字串)。不是指令回 (None, '')。

    容忍 Telegram 群組裡的 `/cmd@BotName` 寫法與多餘空白。
    """
    s = str(text or '').strip()
    if not s.startswith('/'):
        return None, ''
    parts = s.split(None, 1)
    cmd = parts[0].lower()
    if '@' in cmd:                     # /status@MyBot → /status
        cmd = cmd.split('@', 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else ''
    return (cmd if cmd in COMMANDS else None), arg


def command_kind(cmd):
    meta = COMMANDS.get(cmd)
    return meta[0] if meta else None


def needs_confirm(cmd):
    """這個指令要不要二次確認 (只有啟用類要)。"""
    return command_kind(cmd) == CMD_RISKY


def new_confirm_code(now=None):
    """產生確認碼與到期時間。用數字+大寫字母,避免手機輸入時大小寫混淆。"""
    alphabet = string.ascii_uppercase + string.digits
    code = ''.join(random.choice(alphabet) for _ in range(CONFIRM_CODE_LEN))
    return code, float(now if now is not None else time.time()) + CONFIRM_TTL_SEC


def check_confirm(pending, code, now=None):
    """驗證確認碼。回傳 (ok, 原因)。

    pending 形如 {'code': 'AB12', 'expire': 1234567.0, 'cmd': '/on', 'arg': '策略A'}
    """
    if not pending:
        return False, '目前沒有待確認的指令'
    t = float(now if now is not None else time.time())
    if t > float(pending.get('expire', 0)):
        return False, '確認碼已過期,請重新下指令'
    if str(code or '').strip().upper() != str(pending.get('code', '')).upper():
        return False, '確認碼不正確'
    return True, ''


def match_strategy(strategies, arg):
    """依「編號」或「名稱」找策略。回傳 (策略 dict 或 None, 說明)。

    支援編號是因為手機打中文策略名很麻煩;`/list` 會把編號一起列出來。
    名稱比對允許部分符合,但**多個符合時要求使用者講清楚**——遠端操作沒有
    畫面可以確認,選錯策略的後果是啟用了不該啟用的東西。
    """
    items = list(strategies or [])
    if not items:
        return None, '目前沒有任何策略'
    key = str(arg or '').strip()
    if not key:
        return None, '請指定策略編號或名稱 (先用 /list 查看)'
    if key.isdigit():
        i = int(key)
        if 1 <= i <= len(items):
            return items[i - 1], ''
        return None, f'編號超出範圍 (目前有 {len(items)} 個策略)'
    exact = [s for s in items if str(s.get('name', '')).strip() == key]
    if len(exact) == 1:
        return exact[0], ''
    partial = [s for s in items if key in str(s.get('name', ''))]
    if len(partial) == 1:
        return partial[0], ''
    if len(partial) > 1:
        names = '、'.join(str(s.get('name')) for s in partial[:5])
        return None, f'有多個策略符合「{key}」:{names};請用編號指定'
    return None, f'找不到策略「{key}」(先用 /list 查看)'


# ---------------------------------------------------------------------------
# 回覆文字組裝 (純字串,不碰任何狀態)
# ---------------------------------------------------------------------------
def help_text():
    lines = ['📱 可用指令:']
    for cmd, (kind, desc) in COMMANDS.items():
        mark = {'readonly': '🔍', 'safe_write': '🛑', 'risky': '⚠️'}.get(kind, '')
        lines.append(f'{mark} {cmd} — {desc}')
    lines.append('')
    lines.append('⚠️ = 會讓系統開始下單,需要二次確認')
    lines.append('本介面不提供下單/改單,請回主程式操作。')
    return '\n'.join(lines)


def status_text(logged_in, qt_running, strategies, extra=None):
    items = list(strategies or [])
    enabled = [s for s in items if s.get('enabled')]
    real = [s for s in enabled if s.get('mode') == '實單']
    lines = [
        '📊 系統狀態',
        f"券商連線:{'🟢 已登入' if logged_in else '🔴 未登入'}",
        f"自動交易總開關:{'🟢 運轉中' if qt_running else '⏸ 已停止'}",
        f"策略:共 {len(items)} 個,啟用 {len(enabled)} 個 (其中實單 {len(real)} 個)",
    ]
    if enabled:
        lines.append('啟用中:' + '、'.join(str(s.get('name')) for s in enabled[:8]))
    if extra:
        lines.append(str(extra))
    return '\n'.join(lines)


def list_text(strategies):
    items = list(strategies or [])
    if not items:
        return '目前沒有任何策略'
    lines = ['📋 策略清單 (可用編號下指令):']
    for i, s in enumerate(items, 1):
        flag = '🟢' if s.get('enabled') else '⚪'
        mode = str(s.get('mode', '模擬'))
        mark = '💰實單' if mode == '實單' else '🧪模擬'
        lines.append(f"{i}. {flag} {s.get('name')} [{mark}] "
                     f"{s.get('symbol')} {s.get('timeframe')}")
    return '\n'.join(lines)


def positions_text(accounts, only=None):
    """模擬帳戶持倉清單。accounts 是 {account_id: 帳戶dict} 或帳戶 list。

    刻意**只報模擬帳戶**:實單庫存要打券商 API,那是 GUI 層的事,而且遠端
    查詢不該觸發券商流量 (鐵則 5 的精神)。文末明講這件事,免得使用者誤以為
    「Telegram 顯示無持倉」等於「實單也沒有持倉」。
    """
    accts = [only] if only is not None else _acct_list(accounts)
    lines = ['📦 模擬帳戶持倉']
    any_pos = False
    for acct in accts:
        pos = (acct or {}).get('positions') or {}
        if not pos:
            continue
        any_pos = True
        lines.append(f"[{acct.get('name', acct.get('id', '?'))}]")
        for sym, p in pos.items():
            lines.append(f"  {sym} {p.get('direction')} {p.get('qty')} "
                         f"@ {p.get('avg_price')}")
    if not any_pos:
        lines.append('  (無持倉)')
    lines.append('')
    lines.append('※ 實單庫存請於主程式「我的庫存」分頁查看 (需券商連線)。')
    return '\n'.join(lines)


def pnl_text(accounts, day, only=None):
    """模擬帳戶損益。day 形如 '2026-07-26',由呼叫端給 (純函式不看時鐘)。

    【ADR-137】only 帶入單一帳戶 dict 時只報那一個 —— 使用者的原話是
    「我要看目前**XX帳號**的損益」,帳戶多的時候全部列出來反而要自己找。
    """
    if only is not None:
        accts = [only]
    else:
        accts = _acct_list(accounts)
    if not accts:
        return '目前沒有任何模擬帳戶'
    lines = [f'💰 模擬帳戶損益 ({day})']
    for acct in accts:
        today = paper_account.realized_pnl_on(acct, day)
        unreal = paper_account.unrealized_pnl(acct)
        eq = paper_account.equity(acct)
        lines.append(
            f"[{acct.get('name', acct.get('id', '?'))}] 權益 {eq:,.0f}\n"
            f"  今日已實現 {today:+,.0f}｜未實現 {unreal:+,.0f}｜"
            f"累計已實現 {float(acct.get('realized_pnl', 0.0)):+,.0f}")
    lines.append('')
    lines.append('※ 未實現以最後一次標記價計算,非即時報價。')
    return '\n'.join(lines)


def _acct_list(accounts):
    """把 {id: 帳戶} 或 list 統一成 list,順序穩定 (編號才不會每次都變)。"""
    if isinstance(accounts, dict):
        return [accounts[k] for k in sorted(accounts.keys(), key=str)]
    return list(accounts or [])


def match_account(accounts, arg):
    """【ADR-137】依「編號」或「名稱」找模擬帳戶。回傳 (帳戶 dict 或 None, 說明)。

    比對規則刻意與 match_strategy 一致 (編號 / 完全符合 / 唯一部分符合),
    使用者只要學一套。多個符合時要求講清楚 —— 遠端沒有畫面可以確認,
    回錯帳戶的損益會直接誤導決策。
    """
    items = _acct_list(accounts)
    if not items:
        return None, '目前沒有任何模擬帳戶'
    key = str(arg or '').strip()
    if not key:
        return None, '請指定帳戶編號或名稱 (先用 /accounts 查看)'
    if key.isdigit():
        i = int(key)
        if 1 <= i <= len(items):
            return items[i - 1], ''
        return None, f'編號超出範圍 (目前有 {len(items)} 個帳戶)'
    def _nm(a):
        return str((a or {}).get('name', '') or '').strip()
    exact = [a for a in items if _nm(a) == key]
    if len(exact) == 1:
        return exact[0], ''
    partial = [a for a in items if key in _nm(a) or key == str((a or {}).get('id', ''))]
    if len(partial) == 1:
        return partial[0], ''
    if len(partial) > 1:
        names = '、'.join(_nm(a) for a in partial[:5])
        return None, f'有多個帳戶符合「{key}」:{names};請用編號指定'
    return None, f'找不到帳戶「{key}」(先用 /accounts 查看)'


def accounts_text(accounts):
    """【ADR-137】列出模擬帳戶與編號,讓使用者知道 /pnl 後面可以打什麼。"""
    items = _acct_list(accounts)
    if not items:
        return '目前沒有任何模擬帳戶'
    lines = ['🏦 模擬帳戶']
    for i, a in enumerate(items, 1):
        lines.append(f"{i}. {a.get('name', a.get('id', '?'))}"
                     f"  權益 {paper_account.equity(a):,.0f}")
    lines.append('')
    lines.append('用法:/pnl 1 或 /pnl 帳戶名稱;/positions 同理。')
    return '\n'.join(lines)


def strategy_detail_text(strategy, runtime=None):
    """【ADR-137】單一策略的完整設定與目前狀態。

    使用者在手機上問「這檔策略現在怎麼樣」時,需要的是**設定 + 狀態**兩塊:
    設定決定它會做什麼,狀態決定它現在在做什麼。只給其中一半都要再問一次。
    """
    if not strategy:
        return '❌ 找不到策略'
    s = strategy
    rt = runtime or {}
    tf = s.get('timeframe', '?')
    lines = [f"📋 {s.get('name', '?')}"]
    lines.append(f"狀態:{'✅ 啟用' if s.get('enabled') else '⬜ 停用'}"
                 f"｜模式:{s.get('mode', '模擬')}")
    lines.append(f"商品:{s.get('symbol', '?')} ({s.get('trade_type', '?')}) "
                 f"{tf} x{s.get('qty', 0)}")
    if s.get('watch_enabled'):
        lines.append(f"看A做B:訊號看 {s.get('watch_symbol', '?')} "
                     f"{s.get('watch_timeframe', tf)}")
    lines.append(f"委託方式:{s.get('price_type', '限價')}"
                 f"｜讓價 {s.get('slippage_ticks', 0)} 檔")
    _sl, _tp = s.get('stop_loss_pct', 0), s.get('take_profit_pct', 0)
    _sla, _tpa = s.get('stop_loss_abs', 0), s.get('take_profit_abs', 0)
    _risk = []
    if _sl:
        _risk.append(f'停損 {_sl}%')
    if _sla:
        _risk.append(f'停損 {_sla}')
    if _tp:
        _risk.append(f'停利 {_tp}%')
    if _tpa:
        _risk.append(f'停利 {_tpa}')
    lines.append('風控:' + ('、'.join(_risk) if _risk else '(未設,由出場訊號/程式碼決定)'))
    lines.append(f"即時觸發:{'是 (盤中觸價就出場)' if s.get('intrabar_stop') else '否 (等K棒收盤)'}")
    lines.append(f"每日上限 {s.get('max_trades_per_day', '?')} 次"
                 f"｜冷卻 {s.get('cooldown_sec', '?')} 秒")
    st = rt.get('state', 'FLAT')
    if st in ('LONG', 'SHORT'):
        lines.append(f"目前部位:{st} {rt.get('qty', 0)} @ {rt.get('exec_entry_price', 0)}")
    else:
        lines.append('目前部位:無')
    lines.append(f"今日進場 {int(rt.get('trades_today', 0) or 0)} 次")
    return '\n'.join(lines)


def confirm_prompt(cmd, arg, code, target_name=None, detail=None):
    """二次確認訊息。detail 由呼叫端帶入「這一步實際會讓什麼開始動」。

    刻意要求把標的/模式/數量寫進來:手機端看不到畫面,只回一個確認碼並不
    構成知情同意——使用者必須在按下 /yes 之前看到自己啟動的是哪一個東西。
    """
    what = f'{cmd} {arg}'.strip()
    tgt = f'「{target_name}」' if target_name else ''
    lines = ['⚠️ 這個操作會讓系統開始下單。', f'指令:{what}{tgt}']
    if detail:
        lines.append(str(detail))
    lines.append('')
    lines.append(f'確認請在 {CONFIRM_TTL_SEC} 秒內回傳:')
    lines.append(f'/yes {code}')
    return '\n'.join(lines)
