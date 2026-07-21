import sys

with open('stock_app_pro.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if '期望值' in line:
        print(f"stock_app_pro.py:{i+1}:{line.strip()}")
    if '買進持有' in line:
        print(f"stock_app_pro.py:{i+1}:{line.strip()}")
    if 'SJ_DAYS' in line:
        print(f"stock_app_pro.py:{i+1}:{line.strip()}")
    if 'show_backtest_report' in line:
        print(f"stock_app_pro.py:{i+1}:{line.strip()}")
    if 'TXF' in line:
         print(f"stock_app_pro.py:{i+1}:{line.strip()}")
