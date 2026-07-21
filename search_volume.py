import sys
lines = open('g:/StockBuild-Antigravity/stock_app_pro.py', encoding='utf-8').readlines()
for i, l in enumerate(lines):
    if "'Volume'" in l:
        print(f"{i+1}: {l.strip()}")
