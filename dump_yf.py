import sys
def run():
    lines = open('g:/StockBuild-Antigravity/stock_app_pro.py', encoding='utf-8').readlines()
    in_func = False
    out = []
    for l in lines:
        if 'def _extend_with_yahoo' in l:
            in_func = True
        if in_func:
            out.append(l)
            if l.strip().startswith('def ') and not l.strip().startswith('def _extend_with_yahoo'):
                break
    with open('g:/StockBuild-Antigravity/dump_yf.txt', 'w', encoding='utf-8') as f:
        f.writelines(out)
if __name__ == '__main__':
    run()
