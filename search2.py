import sys
with open('g:/StockBuild-Antigravity/stock_app_pro.py', encoding='utf-8') as f:
    lines = f.readlines()

with open('out.txt', 'w', encoding='utf-8') as f:
    f.write('--- 期交所歷史 ---\n')
    for i, l in enumerate(lines):
        if '📥 期交所歷史' in l:
            f.write(f'{i+1}: {l.strip()}\n')
    f.write('\n--- volume mpf ---\n')
    for i, l in enumerate(lines):
        if 'mpf.plot' in l:
            f.write(f'{i+1}: {l.strip()}\n')
        if 'volume' in l.lower() and 'mpf' in l.lower():
            f.write(f'{i+1}: {l.strip()}\n')
