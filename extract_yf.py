import sys

def run():
    lines = open('g:/StockBuild-Antigravity/stock_app_pro.py', encoding='utf-8').readlines()
    in_func = False
    out = []
    for l in lines:
        if 'def _fetch_data_worker_impl' in l:
            in_func = True
        if in_func:
            out.append(l)
            if 'self.plot_df =' in l:
                break
    with open('g:/StockBuild-Antigravity/yf_logic.txt', 'w', encoding='utf-8') as f:
        f.writelines(out[100:])

if __name__ == '__main__':
    run()
