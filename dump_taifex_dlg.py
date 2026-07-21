import sys
lines = open('g:/StockBuild-Antigravity/stock_app_pro.py', encoding='utf-8', errors='ignore').readlines()
with open('g:/StockBuild-Antigravity/taifex_dlg.txt', 'w', encoding='utf-8') as f:
    f.writelines(lines[3090:3150])
