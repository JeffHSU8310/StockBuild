#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_crossref.py — 跨模組名稱一致性掃描 (收尾檢查用,P-57)

【緣起】ADR-053 那輪把 backtest.py / stock_app_pro.py / test_core.py 都改成呼叫
core.custom_strategy.decision_to_intents,但 custom_strategy.py 本身沒有同步
補上這個函式——三個呼叫端各自「看起來合理」,py_compile 也過 (呼叫時才報
AttributeError,靜態編譯抓不到),結果是「回測按下去沒反應」「參數最佳化沒
數據」這種一路到使用者手上才炸開的問題 (ADR-055/056 修正)。

這支腳本做的事:掃描專案內所有 .py 檔,對每一個「module.attr」的跨模組屬性
存取 (import X / from Y import X 之後的 X.attr),檢查 attr 是否真的存在於
被參照的模組 (函式/類別/模組層級變數三種都算)。抓不到來源的模組 (第三方
套件、動態 import) 一律跳過,不誤報。

用法:
    python diag_crossref.py                  # 掃描目前資料夾
    python diag_crossref.py /path/to/project # 掃描指定資料夾

回傳碼:0 = 乾淨、1 = 抓到至少一處斷鏈 (可接進 CI / 收尾檢查腳本)。

限制 (誠實列出,別誤用):
  * 只抓「module.attr 這個 attr 不存在」這種一定是 bug 的情況;抓不到的
    (例如透過 getattr 動態組出來的名稱、跨檔案的 self.xxx 實例屬性、
    在函式內才 import 的模組) 一律跳過,寧可漏抓也不要洗版式誤報。
  * 只檢查「這次呼叫的名字在目標模組存不存在」,不檢查參數個數/型別是否
    相符 (那是另一類坑,建議用 diag_repro_issues.py 這種會真的執行到那行
    的案例來抓)。
  * 是收尾檢查的其中一步,不是唯一一步:CLAUDE.md 的驗證流程 (py_compile +
    tests/test_core.py + diag_repro_issues.py) 仍然全部要做。
"""
import ast
import os
import sys


def _module_public_names(tree):
    """收集一個模組 AST 頂層定義的所有名稱:函式、類別、模組層級變數賦值,
    以及這個模組自己 import 進來又轉手 re-export 的名字。"""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                # 【ADR-134】原本只認 `NAME = ...`,漏掉**元組拆解賦值**
                # (`A, B, C = 1, 2, 3`) —— core/jae.py 的 COL_A/COL_J/COL_E 就是
                # 這樣定義的,結果被誤報成「jae 沒有 COL_A 這個名稱」。
                # 誤報比漏報更糟:它會讓人以為真的斷鏈了而去亂改程式。
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _collect_local_aliases(tree, local_module_stems):
    """收集這個檔案裡,哪些本地變數名字綁定到「本專案的哪個模組」。
    處理兩層:
      1. `import foo` / `import foo as bar` / `from pkg import foo` /
         `from pkg import foo as bar`。
      2. 簡單的二手賦值鏈,例如:
             from . import custom_strategy as _cs
             custom_fn = _cs          # ← custom_fn 也要算進別名
         這個專案常見「函式內 lazy import 一個模組給內部變數、再賦值成另一個
         參數名字往下傳」的寫法 (core/backtest.py 的 custom_fn = _cs 就是
         原本 diag_crossref 抓不到 ADR-053 那次斷鏈的原因——只認 import 語句、
         不認後續的二手賦值),用固定點疊代把這種鏈路也標記出來。"""
    aliases = {}  # local_name -> module_stem
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                stem = alias.name.split('.')[-1]
                if stem in local_module_stems:
                    aliases[alias.asname or alias.name.split('.')[0]] = stem
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in local_module_stems:
                    aliases[alias.asname or alias.name] = alias.name

    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
              and isinstance(n.value, ast.Name)]
    changed = True
    while changed:
        changed = False
        for n in assigns:
            tgt, src = n.targets[0].id, n.value.id
            if src in aliases and tgt not in aliases:
                aliases[tgt] = aliases[src]
                changed = True
    return aliases


def _iter_py_files(root_dir):
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in
                       ('.git', '__pycache__', 'node_modules', '.venv', 'venv')]
        for fn in filenames:
            if fn.endswith('.py'):
                yield os.path.join(dirpath, fn)


def scan(root_dir):
    """回傳 (問題清單, stem->path 對照)。
    問題清單項目:(呼叫檔, 行號, 本地別名, 目標模組stem, 屬性名稱)"""
    py_files = list(_iter_py_files(root_dir))

    trees = {}
    for path in py_files:
        try:
            with open(path, encoding='utf-8') as f:
                trees[path] = ast.parse(f.read(), filename=path)
        except (SyntaxError, UnicodeDecodeError):
            continue  # 語法錯誤自有 py_compile 去抓,這裡不重複報

    # 模組 stem (檔名去副檔名) -> 該模組定義的名稱集合。同名 stem 出現在不同
    # 資料夾時取第一個找到的 (這個專案的模組命名本來就彼此不重複)。
    stem_names = {}
    stem_path = {}
    for path, tree in trees.items():
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem not in stem_names:
            stem_names[stem] = _module_public_names(tree)
            stem_path[stem] = path

    problems = []
    for path, tree in trees.items():
        aliases = _collect_local_aliases(tree, set(stem_names.keys()))
        if not aliases:
            continue
        # 這個檔案裡自己也定義了同名東西 (區域變數剛好同名) 就不查該名字——
        # 保守起見,只有「這個檔案沒有自己定義同名東西」才視為真的在用
        # import 進來的模組。
        own_names = _module_public_names(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                   and isinstance(node.ctx, ast.Load)):
                continue
            local_name = node.value.id
            if local_name in own_names or local_name not in aliases:
                continue
            target_stem = aliases[local_name]
            if target_stem == os.path.splitext(os.path.basename(path))[0]:
                continue  # 自己 import 自己這種怪狀況,略過
            target_names = stem_names.get(target_stem)
            if target_names is None:
                continue
            if node.attr not in target_names and not node.attr.startswith('_'):
                problems.append((path, node.lineno, local_name, target_stem, node.attr))
    return problems, stem_path


def scan_duplicate_defs(root_dir):
    """【ADR-109】掃描「同一個類別/模組裡定義了兩次的同名函式」。

    【緣起】`auto_scale_y` / `auto_scale_indicator_panels` 在
    stock_app_pro.py 裡各被定義了**兩次**:後面那份把前面那份整個蓋掉,
    Python 不會有任何警告。前一份裡「成交量副圖 Y 軸要從 0 起算」的特例
    因此靜默失效,量能長條被從底部裁掉一截,而程式看起來完全正常。

    這種錯誤 py_compile 抓不到、跨模組斷鏈掃描也抓不到 (名字是存在的),
    只能靠這種「同一個 scope 裡有沒有重名定義」的檢查。
    """
    dups = []
    for path in _iter_py_files(root_dir):
        try:
            tree = ast.parse(open(path, encoding='utf-8').read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        scopes = [('模組層級', tree.body)]
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                scopes.append((f'class {node.name}', node.body))
        for scope_name, body in scopes:
            seen = {}
            for n in body:
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # @overload / @property+setter 是合法的同名重複,不報。
                deco = {getattr(d, 'attr', getattr(d, 'id', '')) for d in n.decorator_list}
                if deco & {'overload', 'setter', 'getter', 'deleter', 'register'}:
                    continue
                if n.name in seen:
                    dups.append((path, seen[n.name], n.lineno, scope_name, n.name))
                seen[n.name] = n.lineno
    return dups


def main():
    root_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    problems, stem_path = scan(root_dir)

    dups = scan_duplicate_defs(root_dir)
    if dups:
        print(f"❌ diag_crossref: 發現 {len(dups)} 處重複定義 (後面那份會靜默蓋掉前面那份):\n")
        for path, first, second, scope_name, name in sorted(dups):
            rel = os.path.relpath(path, root_dir)
            print(f"  {rel}  {scope_name} 的 `{name}`:"
                  f"第 {first} 行定義後,第 {second} 行又定義一次")
        print("\n請確認:是不是新增功能時沒發現已經有同名的方法?"
              "兩份的內容常常不一樣,先比對差異再決定保留哪些行為,不要直接砍掉一份。")
        return 1

    if not problems:
        print("✅ diag_crossref: 沒有發現跨模組斷鏈或重複定義。")
        return 0
    print(f"❌ diag_crossref: 發現 {len(problems)} 處可能的跨模組斷鏈:\n")
    for path, lineno, local_name, target_stem, attr in sorted(problems):
        target_file = stem_path.get(target_stem, f"{target_stem}.py")
        rel = os.path.relpath(path, root_dir)
        print(f"  {rel}:{lineno}  呼叫 {local_name}.{attr}()"
              f"  → {target_stem} ({target_file}) 沒有 `{attr}` 這個名稱")
    print("\n請確認:是不是改名字/加函式時漏了同步某個檔案?"
         "(常見情境:core/ 裡改了函式名,只改了呼叫端沒改被呼叫的模組本身。)")
    return 1


if __name__ == '__main__':
    sys.exit(main())
