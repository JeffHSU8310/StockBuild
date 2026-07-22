"""
tests/test_core.py — core/ 與 data/ 模組的離線單元測試。

執行方式 (從專案根目錄，也就是 stock_app_pro.py 所在目錄):
    python tests/test_core.py
或
    python -m unittest tests.test_core -v

這份測試刻意不 import tkinter、不 import shioaji、不需要網路，
所以可以在任何裝了 Python + pandas + numpy 的機器上執行，
包括 CI 環境或沒有畫面的伺服器。修改 core/ 或 data/ 底下的任何檔案後，
都應該重新跑一次這份測試再交付。
"""
import os
import sys
import json
import tempfile
import unittest

# 讓這份測試不管從哪個目錄被呼叫，都找得到專案根目錄下的 core/ 與 data/ 套件
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from core import tick_rules
from core import futures_session
from core import market_session
from core import secure_store
from core import order_rules
from core import indicators
from core import strategy_engine
from core import backtest
from core import custom_strategy
from core import fut_catalog
from core import ai_helper
from core import cost_model
from core import optimizer
from core import paper_account
from core import taifex_daily
from data import config_store
from data import taifex_store


class TestTickRules(unittest.TestCase):
    def test_future_always_one_point(self):
        self.assertEqual(tick_rules.get_tick(46281, "future", "TXF"), 1.0)
        self.assertEqual(tick_rules.get_tick(1, "future", "TXF"), 1.0)

    def test_etf_price_band(self):
        # ETF (00 開頭): <50 -> 0.01, >=50 -> 0.05
        self.assertEqual(tick_rules.get_tick(49.99, "stock", "0050"), 0.01)
        self.assertEqual(tick_rules.get_tick(50.0, "stock", "0050"), 0.05)
        self.assertEqual(tick_rules.get_tick(105.8, "stock", "0050"), 0.05)

    def test_regular_stock_price_band_boundaries(self):
        cases = [
            (9.99, 0.01), (10.0, 0.05),
            (49.99, 0.05), (50.0, 0.1),
            (99.99, 0.1), (100.0, 0.5),
            (499.99, 0.5), (500.0, 1.0),
            (999.99, 1.0), (1000.0, 5.0),
        ]
        for price, expected in cases:
            with self.subTest(price=price):
                self.assertEqual(tick_rules.get_tick(price, "stock", "2330"), expected)

    def test_symbol_suffix_stripped(self):
        # .TW / .TWO 後綴不應該影響 ETF 判斷 (以 00 開頭與否為準)
        self.assertEqual(tick_rules.get_tick(40, "stock", "0050.TW"), 0.01)
        self.assertEqual(tick_rules.get_tick(40, "stock", "0050.TWO"), 0.01)

    def test_index_tw_uses_stock_rules(self):
        self.assertEqual(tick_rules.get_tick(9.99, "index_tw", "TAIEX"), 0.01)

    def test_fmt_price_decimal_places(self):
        self.assertEqual(tick_rules.fmt_price(1234, "stock", "2330"), "1234")   # tick=5.0 -> 整數
        self.assertEqual(tick_rules.fmt_price(600, "stock", "2330"), "600")      # tick=1.0 -> 整數
        self.assertEqual(tick_rules.fmt_price(105.8, "stock", "0050"), "105.80")  # tick=0.05 -> 2位
        self.assertEqual(tick_rules.fmt_price(60.5, "stock", "2330"), "60.5")     # tick=0.1 -> 1位

    def test_fmt_price_invalid_input(self):
        self.assertEqual(tick_rules.fmt_price(None, "stock", "2330"), "--")
        self.assertEqual(tick_rules.fmt_price("abc", "stock", "2330"), "--")

    def test_round_to_tick_snaps_to_valid_price(self):
        # 105.83 在 ETF (00開頭,>=50) tick=0.05 規則下,最接近的合法價位是 105.85
        result = tick_rules.round_to_tick(105.83, "stock", "0050")
        self.assertAlmostEqual(result, 105.85, places=2)

    def test_round_to_tick_already_valid_stays_unchanged(self):
        result = tick_rules.round_to_tick(105.80, "stock", "0050")
        self.assertAlmostEqual(result, 105.80, places=2)

    def test_round_to_tick_regular_stock_band(self):
        # 一般股票 60.37 在 tick=0.1 規則下 (50<=p<100),最接近的合法價位是 60.4
        result = tick_rules.round_to_tick(60.37, "stock", "2330")
        self.assertAlmostEqual(result, 60.4, places=2)

    def test_round_to_tick_crosses_price_band_boundary(self):
        # 99.97 用 tick=0.1 算最接近是 100.0,但 100.0 屬於下一個價格帶 (tick=0.5)，
        # 100.0 剛好也是 0.5 的整數倍,所以結果應該還是 100.0，驗證跨價格帶不會出錯。
        result = tick_rules.round_to_tick(99.97, "stock", "2330")
        self.assertAlmostEqual(result, 100.0, places=2)

    def test_round_to_tick_future_always_integer(self):
        result = tick_rules.round_to_tick(46281.37, "future", "TXF")
        self.assertEqual(result, 46281.0)


class TestFuturesSession(unittest.TestCase):
    def _build_sample_df(self):
        # 與 ADR-007 驗證用的模擬資料相同:
        # 7/9 夜盤(15:00起) -> 7/10 日盤(收46281) -> 7/10 傍晚夜盤(應歸屬7/11)
        idx, rows = [], []
        for h, m, c in [(15, 0, 45700), (23, 0, 45900), (4, 59, 45648)]:
            d = (pd.Timestamp('2026-07-09 %02d:%02d' % (h, m)) if h >= 15
                 else pd.Timestamp('2026-07-10 %02d:%02d' % (h, m)))
            idx.append(d); rows.append([c, c + 5, c - 5, c])
        for h, m, c in [(8, 45, 45800), (10, 0, 46100), (11, 0, 46495), (13, 44, 46281)]:
            idx.append(pd.Timestamp('2026-07-10 %02d:%02d' % (h, m))); rows.append([c, c + 10, c - 10, c])
        for h, m, c in [(15, 0, 46300), (20, 0, 46500)]:
            idx.append(pd.Timestamp('2026-07-10 %02d:%02d' % (h, m))); rows.append([c, c + 5, c - 5, c])
        df = pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'],
                           index=pd.DatetimeIndex(idx)).sort_index()
        df['Volume'] = 1
        return df

    def test_night_session_does_not_pollute_day_close(self):
        """ADR-007 的核心防退化測試:7/10 交易日收盤必須是日盤的 46281,不能被夜盤污染。"""
        df = self._build_sample_df()
        agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        result = futures_session.resample_future_session(df, "日K", agg)

        self.assertIn(pd.Timestamp('2026-07-10'), result.index)
        self.assertEqual(result.loc['2026-07-10', 'Close'], 46281)
        self.assertEqual(result.loc['2026-07-10', 'Open'], 45700)  # 7/9 夜盤開盤,近全視角

        # 7/10 傍晚 15:00 起的夜盤,必須歸到下一交易日 (7/11),不能留在 7/10
        self.assertIn(pd.Timestamp('2026-07-11'), result.index)
        self.assertEqual(result.loc['2026-07-11', 'Close'], 46500)

    def test_empty_dataframe_returns_empty(self):
        empty_df = pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
        empty_df.index = pd.DatetimeIndex([])
        agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        result = futures_session.resample_future_session(empty_df, "日K", agg)
        self.assertTrue(result.empty)

    def test_weekly_aggregation_runs_without_error(self):
        df = self._build_sample_df()
        agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        result = futures_session.resample_future_session(df, "周K", agg)
        self.assertFalse(result.empty)
        self.assertIn('Close', result.columns)


class TestOrderRules(unittest.TestCase):
    def test_common_mode_no_lot_restriction_but_has_qty_cap(self):
        # 整股模式沒有現股/融資融券/ROD-IOC-FOK 的限制,但數量上限 499 張仍然適用。
        ok, reason = order_rules.validate_stock_order("Common", "市價", "MarginTrading", "IOC", "3")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_common_mode_rejects_qty_over_499(self):
        ok, reason = order_rules.validate_stock_order("Common", "限價", "Cash", "ROD", "500")
        self.assertFalse(ok)
        self.assertIn("499", reason)

    def test_common_mode_accepts_qty_exactly_499(self):
        ok, reason = order_rules.validate_stock_order("Common", "限價", "Cash", "ROD", "499")
        self.assertTrue(ok)

    def test_common_mode_rejects_qty_zero_or_negative(self):
        ok, reason = order_rules.validate_stock_order("Common", "限價", "Cash", "ROD", "0")
        self.assertFalse(ok)

    def test_fixing_mode_rejects_qty_over_499(self):
        ok, reason = order_rules.validate_stock_order("Fixing", "限價", "Cash", "ROD", "500")
        self.assertFalse(ok)
        self.assertIn("499", reason)

    def test_intraday_odd_rejects_market_order(self):
        ok, reason = order_rules.validate_stock_order("IntradayOdd", "市價", "Cash", "ROD", "500")
        self.assertFalse(ok)
        self.assertIn("限價", reason)

    def test_intraday_odd_rejects_margin(self):
        ok, reason = order_rules.validate_stock_order("IntradayOdd", "限價", "MarginTrading", "ROD", "500")
        self.assertFalse(ok)
        self.assertIn("融資融券", reason)

    def test_intraday_odd_rejects_non_rod(self):
        ok, reason = order_rules.validate_stock_order("IntradayOdd", "限價", "Cash", "IOC", "500")
        self.assertFalse(ok)
        self.assertIn("ROD", reason)

    def test_intraday_odd_rejects_qty_out_of_range(self):
        ok, reason = order_rules.validate_stock_order("IntradayOdd", "限價", "Cash", "ROD", "1000")
        self.assertFalse(ok)
        self.assertIn("1~999", reason)

    def test_intraday_odd_rejects_non_numeric_qty(self):
        ok, reason = order_rules.validate_stock_order("IntradayOdd", "限價", "Cash", "ROD", "abc")
        self.assertFalse(ok)

    def test_intraday_odd_valid_order_passes(self):
        ok, reason = order_rules.validate_stock_order("IntradayOdd", "限價", "Cash", "ROD", "500")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_odd_after_hours_same_rules_as_intraday(self):
        ok, reason = order_rules.validate_stock_order("Odd", "市價", "Cash", "ROD", "500")
        self.assertFalse(ok)

    def test_fixing_rejects_market_order(self):
        ok, reason = order_rules.validate_stock_order("Fixing", "市價", "Cash", "ROD", "1")
        self.assertFalse(ok)
        self.assertIn("市價", reason)

    def test_fixing_rejects_non_rod(self):
        ok, reason = order_rules.validate_stock_order("Fixing", "限價", "Cash", "FOK", "1")
        self.assertFalse(ok)

    def test_fixing_valid_order_passes(self):
        ok, reason = order_rules.validate_stock_order("Fixing", "限價", "Cash", "ROD", "1")
        self.assertTrue(ok)

    def test_daytrade_eligible_only_common_cash_sell(self):
        self.assertTrue(order_rules.is_daytrade_eligible("Common", "Cash", "賣出", True))
        self.assertFalse(order_rules.is_daytrade_eligible("Common", "Cash", "買進", True))
        self.assertFalse(order_rules.is_daytrade_eligible("Common", "MarginTrading", "賣出", True))
        self.assertFalse(order_rules.is_daytrade_eligible("IntradayOdd", "Cash", "賣出", True))
        self.assertFalse(order_rules.is_daytrade_eligible("Common", "Cash", "賣出", False))


class TestIndicators(unittest.TestCase):
    def _sample_ohlc(self, n=60):
        idx = pd.date_range("2026-01-01", periods=n, freq="D")
        rng = np.random.default_rng(42)
        close = 100 + np.cumsum(rng.normal(0, 1, n))
        high = close + rng.uniform(0, 1, n)
        low = close - rng.uniform(0, 1, n)
        open_ = close + rng.uniform(-0.5, 0.5, n)
        return pd.DataFrame({'Open': open_, 'High': high, 'Low': low, 'Close': close}, index=idx)

    def test_sma_matches_pandas_rolling_mean(self):
        df = self._sample_ohlc()
        ma_flags = [True, False, False, False, False, False]
        ma_types = ["SMA"] * 6
        ma_periods = ["5", "10", "20", "60", "120", "240"]
        result = indicators.calculate_indicators(
            df, ma_flags, ma_types, ma_periods,
            bb_show=False, bbw_show=False,
            macd_show=False, macd_f="12", macd_s="26", macd_sig="9",
            rsi_show=False, rsi_p="14",
            kdj_show=False, kd_n="9", kd_m1="3", kd_m2="3",
            dmi_show=False, dmi_n="14",
        )
        self.assertIn("MA_CUSTOM_0", result.columns)
        expected = df['Close'].rolling(window=5).mean()
        pd.testing.assert_series_equal(result["MA_CUSTOM_0"], expected, check_names=False)
        # 沒開的 MA 不應該出現欄位
        self.assertNotIn("MA_CUSTOM_1", result.columns)

    def test_bollinger_bands_present_when_enabled(self):
        df = self._sample_ohlc()
        result = indicators.calculate_indicators(
            df, [False]*6, ["SMA"]*6, ["5"]*6,
            bb_show=True, bbw_show=False,
            macd_show=False, macd_f="12", macd_s="26", macd_sig="9",
            rsi_show=False, rsi_p="14",
            kdj_show=False, kd_n="9", kd_m1="3", kd_m2="3",
            dmi_show=False, dmi_n="14",
        )
        for col in ["BB_MID", "BB_UPPER", "BB_LOWER", "BB_STD"]:
            self.assertIn(col, result.columns)

    def test_macd_rsi_kdj_dmi_compute_without_error(self):
        df = self._sample_ohlc(n=100)
        result = indicators.calculate_indicators(
            df, [False]*6, ["SMA"]*6, ["5"]*6,
            bb_show=False, bbw_show=False,
            macd_show=True, macd_f="12", macd_s="26", macd_sig="9",
            rsi_show=True, rsi_p="14",
            kdj_show=True, kd_n="9", kd_m1="3", kd_m2="3",
            dmi_show=True, dmi_n="14",
        )
        for col in ["MACD", "Signal", "Hist", "RSI", "K", "D", "J", "+DI", "-DI", "ADX"]:
            self.assertIn(col, result.columns)
        # RSI 應該落在 0~100 範圍內 (排除 NaN)
        valid_rsi = result["RSI"].dropna()
        self.assertTrue((valid_rsi >= 0).all() and (valid_rsi <= 100).all())

    def test_invalid_period_is_silently_skipped(self):
        # 保留原本行為:週期欄位打錯字時只跳過該 MA,不拋例外
        df = self._sample_ohlc()
        ma_flags = [True, False, False, False, False, False]
        ma_periods = ["abc", "10", "20", "60", "120", "240"]  # 第一個故意打錯
        result = indicators.calculate_indicators(
            df, ma_flags, ["SMA"]*6, ma_periods,
            bb_show=False, bbw_show=False,
            macd_show=False, macd_f="12", macd_s="26", macd_sig="9",
            rsi_show=False, rsi_p="14",
            kdj_show=False, kd_n="9", kd_m1="3", kd_m2="3",
            dmi_show=False, dmi_n="14",
        )
        self.assertNotIn("MA_CUSTOM_0", result.columns)  # 轉換失敗,欄位不會被寫入


class TestConfigStore(unittest.TestCase):
    def test_broker_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "broker_config.json")
            config_store.save_broker_config(path, "ak", "sk", "pid123", "/path/ca.pfx")
            api_key, secret_key, pid, ca_path = config_store.load_broker_config(path)
            self.assertEqual((api_key, secret_key, pid, ca_path),
                              ("ak", "sk", "pid123", "/path/ca.pfx"))

    def test_broker_config_missing_file_returns_empty(self):
        result = config_store.load_broker_config("/tmp/this_file_should_not_exist_xyz.json")
        self.assertEqual(result, ('', '', '', ''))

    def test_broker_config_backward_compatible_with_old_fm_email_field(self):
        # 【ADR-011】舊版設定檔可能還留著 fm_email 欄位 (FinMind 登入已移除)，
        # 讀取時應該忽略這個多餘欄位，不能因為欄位對不上而炸掉。
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "broker_config.json")
            with open(path, 'w') as f:
                json.dump({'api_key': 'ak', 'secret_key': 'sk', 'pid': 'pid123',
                           'ca_path': '/path/ca.pfx', 'fm_email': 'old@leftover.com'}, f)
            api_key, secret_key, pid, ca_path = config_store.load_broker_config(path)
            self.assertEqual((api_key, secret_key, pid, ca_path),
                              ("ak", "sk", "pid123", "/path/ca.pfx"))

    def test_watchlists_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "watchlists.json")
            data = {"我的自選": ["2330", "0050"]}
            config_store.save_watchlists(path, data)
            loaded = config_store.load_watchlists(path)
            self.assertEqual(loaded, data)

    def test_watchlists_missing_file_returns_default(self):
        result = config_store.load_watchlists("/tmp/this_file_should_not_exist_xyz.json")
        self.assertEqual(result, config_store.DEFAULT_WATCHLISTS)


class TestOrderModification(unittest.TestCase):
    """ADR-023:委託刪改 (刪單/改量/改價) 規則驗證。"""

    def test_fully_filled_cannot_modify(self):
        ok, _ = order_rules.order_is_modifiable("全部成交", 10, 10)
        self.assertFalse(ok)

    def test_cancelled_cannot_modify(self):
        ok, _ = order_rules.order_is_modifiable("已取消(收單)", 10, 0)
        self.assertFalse(ok)

    def test_partially_filled_can_modify(self):
        ok, _ = order_rules.order_is_modifiable("部分成交", 10, 3)
        self.assertTrue(ok)

    def test_qty_change_must_decrease(self):
        ok, _ = order_rules.validate_qty_change("Common", "已委託", 10, 0, 10)  # 等量
        self.assertFalse(ok)
        ok, _ = order_rules.validate_qty_change("Common", "已委託", 10, 0, 12)  # 加量
        self.assertFalse(ok)
        ok, _ = order_rules.validate_qty_change("Common", "已委託", 10, 0, 7)   # 減量 OK
        self.assertTrue(ok)

    def test_qty_change_not_below_filled(self):
        ok, _ = order_rules.validate_qty_change("Common", "部分成交", 10, 6, 5)  # 5 < 已成交6
        self.assertFalse(ok)
        ok, _ = order_rules.validate_qty_change("Common", "部分成交", 10, 6, 6)  # 減到剛好已成交
        self.assertTrue(ok)

    def test_qty_change_min_one(self):
        ok, _ = order_rules.validate_qty_change("IntradayOdd", "已委託", 5, 0, 0)
        self.assertFalse(ok)

    def test_qty_change_bad_input(self):
        ok, _ = order_rules.validate_qty_change("Common", "已委託", 10, 0, "abc")
        self.assertFalse(ok)

    def test_odd_cannot_change_price(self):
        self.assertFalse(order_rules.price_change_allowed("IntradayOdd"))
        self.assertFalse(order_rules.price_change_allowed("Odd"))
        ok, _ = order_rules.validate_price_change("IntradayOdd", 10.0, 11.0)
        self.assertFalse(ok)

    def test_fixing_cannot_change_price(self):
        self.assertFalse(order_rules.price_change_allowed("Fixing"))

    def test_common_can_change_price(self):
        self.assertTrue(order_rules.price_change_allowed("Common"))
        ok, _ = order_rules.validate_price_change("Common", 104.90, 105.00)
        self.assertTrue(ok)

    def test_price_change_same_price_rejected(self):
        ok, _ = order_rules.validate_price_change("Common", 105.00, 105.00)
        self.assertFalse(ok)

    def test_price_change_non_positive_rejected(self):
        ok, _ = order_rules.validate_price_change("Common", 105.00, 0)
        self.assertFalse(ok)


class TestStrategyEngine(unittest.TestCase):
    """【ADR-035】量化策略引擎:條件庫/驗證/狀態機/風控。自動下單邏輯必須全數通過才可交付。"""

    @staticmethod
    def _mkdf(closes, highs=None, lows=None):
        n = len(closes)
        idx = pd.date_range("2026-01-01 09:00", periods=n, freq="5min")
        c = np.array(closes, dtype=float)
        return pd.DataFrame({'Open': c,
                             'High': np.array(highs, dtype=float) if highs else c + 0.5,
                             'Low': np.array(lows, dtype=float) if lows else c - 0.5,
                             'Close': c, 'Volume': [100] * n}, index=idx)

    def _cross_df(self, up=True):
        """產生一份「最後一根剛好發生均線交叉 (fast=3, slow=10)」的 df。"""
        if up:
            closes = [100 - i * 0.5 for i in range(30)] + [86 + i * 2.0 for i in range(6)]
        else:
            closes = [100 + i * 0.5 for i in range(30)] + [114 - i * 2.0 for i in range(6)]
        df = self._mkdf(closes)
        f = df['Close'].rolling(3).mean(); s = df['Close'].rolling(10).mean()
        for i in range(1, len(df)):
            if up and f.iloc[i-1] <= s.iloc[i-1] and f.iloc[i] > s.iloc[i]:
                return df.iloc[:i+1]
            if (not up) and f.iloc[i-1] >= s.iloc[i-1] and f.iloc[i] < s.iloc[i]:
                return df.iloc[:i+1]
        raise AssertionError("測試資料未產生交叉")

    def _base_strategy(self):
        s = strategy_engine.new_strategy()
        s['name'] = 'T'; s['symbol'] = '2330'; s['qty'] = 2
        s['entry'] = [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}]
        s['exit_signals'] = [{'type': 'ma_cross_down', 'params': {'fast': 3, 'slow': 10}}]
        s['stop_loss_pct'] = 2.0; s['take_profit_pct'] = 5.0
        return s

    def test_ma_cross_one_shot(self):
        df = self._cross_df(up=True)
        fn = strategy_engine.CONDITIONS['ma_cross_up'][2]
        self.assertTrue(fn(df, {'fast': 3, 'slow': 10}))
        # 交叉後的下一根不可重複觸發 (一次性)
        full = self._mkdf(list(df['Close']) + [df['Close'].iloc[-1] + 2])
        self.assertFalse(fn(full, {'fast': 3, 'slow': 10}))

    def test_break_high_and_price_levels(self):
        b = [100] * 25 + [106]
        self.assertTrue(strategy_engine.CONDITIONS['price_break_high'][2](
            self._mkdf(b, highs=[100.5] * 25 + [106.5]), {'n': 20}))
        self.assertFalse(strategy_engine.CONDITIONS['price_break_high'][2](self._mkdf([100] * 26), {'n': 20}))
        self.assertTrue(strategy_engine.CONDITIONS['price_above'][2](self._mkdf([100] * 5), {'value': 99}))
        self.assertFalse(strategy_engine.CONDITIONS['price_above'][2](self._mkdf([100] * 5), {'value': 101}))

    def test_validate_strategy_guards(self):
        s = self._base_strategy()
        self.assertTrue(strategy_engine.validate_strategy(s)[0])
        s2 = dict(s); s2['stop_loss_pct'] = 0; s2['take_profit_pct'] = 0; s2['exit_signals'] = []
        self.assertFalse(strategy_engine.validate_strategy(s2)[0])  # 沒有任何出場方式
        s3 = dict(s); s3['direction'] = '做空'; s3['market'] = '台股'
        self.assertFalse(strategy_engine.validate_strategy(s3)[0])  # 股票不可做空
        s4 = dict(s); s4['qty'] = 0
        self.assertFalse(strategy_engine.validate_strategy(s4)[0])
        s5 = dict(s); s5['entry'] = []
        self.assertFalse(strategy_engine.validate_strategy(s5)[0])

    def test_watch_ab_helpers_and_validation(self):
        # 【ADR-074】指數判斷
        self.assertTrue(strategy_engine.looks_like_index_symbol('^TWII'))
        self.assertTrue(strategy_engine.looks_like_index_symbol('TWOII'))
        self.assertFalse(strategy_engine.looks_like_index_symbol('2330'))
        self.assertFalse(strategy_engine.looks_like_index_symbol('TXF'))
        # 未啟用看A做B:A=B
        s = self._base_strategy()
        self.assertFalse(strategy_engine.watch_enabled(s))
        self.assertEqual(strategy_engine.watch_symbol_of(s), '2330')
        self.assertEqual(strategy_engine.watch_timeframe_of(s), s['timeframe'])
        # 啟用看A做B:看加權(^TWII)的30分K,做2330的5分K
        s.update({'watch_enabled': True, 'watch_symbol': '^TWII',
                  'watch_trade_type': '指數', 'watch_timeframe': '30分K', 'timeframe': '5分K'})
        self.assertEqual(strategy_engine.watch_symbol_of(s), '^TWII')
        self.assertEqual(strategy_engine.watch_trade_type_of(s), '指數')
        self.assertEqual(strategy_engine.watch_timeframe_of(s), '30分K')
        self.assertTrue(strategy_engine.validate_strategy(s)[0])
        # B 不可為指數
        s_bad = self._base_strategy(); s_bad['symbol'] = '^TWII'
        self.assertFalse(strategy_engine.validate_strategy(s_bad)[0])
        # 啟用看A做B 但 A 代碼空白 → 擋下
        s_bad2 = self._base_strategy()
        s_bad2.update({'watch_enabled': True, 'watch_symbol': '', 'watch_trade_type': '指數'})
        # watch_symbol 空會退回 symbol(2330),仍算合法;明確測「A 週期非法」
        s_bad2['watch_symbol'] = '^TWII'; s_bad2['watch_timeframe'] = '3分K'
        self.assertFalse(strategy_engine.validate_strategy(s_bad2)[0])

    def test_evaluate_all_on_A_sl_tp_on_A(self):
        # 【ADR-075】看A做B (修正語意):訊號/停損停利全部看 A;intent 價 = A 的收盤,
        # entry_price = A 的價 → 停損停利以 A 判定。B 的成交價由 app 層在下單時替換。
        import time as _t
        s = self._base_strategy()
        rt = strategy_engine.new_runtime()
        df = self._cross_df(up=True)  # A 的訊號 K 棒 (最後一根黃金交叉)
        a_close = float(df['Close'].iloc[-1])
        intents = strategy_engine.evaluate_strategy(s, rt, df, _t.time(), '2026-07-16')
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'OPEN')
        self.assertEqual(intents[0]['price'], a_close)  # 引擎只認 A 的價
        strategy_engine.apply_fill(s, rt, intents[0], _t.time())
        self.assertEqual(rt['entry_price'], a_close)
        # 停損以 A 的價格計:A 跌 2% 觸發停損
        sl = strategy_engine.evaluate_strategy(s, rt, self._mkdf(list(df['Close']) + [a_close * 0.97]),
                                               _t.time(), '2026-07-16')
        self.assertEqual(len(sl), 1)
        self.assertIn('停損', sl[0]['reason'])

    def test_state_machine_entry_sl_tp(self):
        import time as _t
        s = self._base_strategy(); rt = strategy_engine.new_runtime()
        df = self._cross_df(up=True)
        intents = strategy_engine.evaluate_strategy(s, rt, df, _t.time(), '2026-07-16')
        self.assertEqual(len(intents), 1)
        self.assertEqual((intents[0]['kind'], intents[0]['action'], intents[0]['qty']), ('OPEN', '買進', 2))
        # 同一根K棒不重複
        self.assertEqual(strategy_engine.evaluate_strategy(s, rt, df, _t.time(), '2026-07-16'), [])
        strategy_engine.apply_fill(s, rt, intents[0], _t.time())
        self.assertEqual(rt['state'], 'LONG'); self.assertEqual(rt['trades_today'], 1)
        # 停損
        df_sl = self._mkdf(list(df['Close']) + [rt['entry_price'] * 0.975])
        sl = strategy_engine.evaluate_strategy(s, rt, df_sl, _t.time(), '2026-07-16')
        self.assertEqual(len(sl), 1); self.assertIn('停損', sl[0]['reason']); self.assertEqual(sl[0]['action'], '賣出')
        strategy_engine.apply_fill(s, rt, sl[0], _t.time())
        self.assertEqual(rt['state'], 'FLAT'); self.assertLess(rt['realized_pnl_today'], 0)
        # 停利 (獨立 runtime)
        rt2 = strategy_engine.new_runtime()
        rt2.update({'state': 'LONG', 'entry_price': 100.0, 'qty': 2, 'day': '2026-07-16'})
        df_tp = self._mkdf(list(df['Close']) + [106.0])
        tp = strategy_engine.evaluate_strategy(s, rt2, df_tp, _t.time(), '2026-07-16')
        self.assertEqual(len(tp), 1); self.assertIn('停利', tp[0]['reason'])
        strategy_engine.apply_fill(s, rt2, tp[0], _t.time())
        self.assertAlmostEqual(rt2['realized_pnl_today'], 12.0)

    def test_short_direction_futures(self):
        import time as _t
        s = strategy_engine.new_strategy()
        s.update({'name': 'S', 'symbol': 'TXF', 'market': '台期貨', 'direction': '做空',
                  'qty': 1, 'stop_loss_pct': 1.0,
                  'entry': [{'type': 'ma_cross_down', 'params': {'fast': 3, 'slow': 10}}]})
        rt = strategy_engine.new_runtime()
        df = self._cross_df(up=False)
        i1 = strategy_engine.evaluate_strategy(s, rt, df, _t.time(), '2026-07-16')
        self.assertEqual(i1[0]['action'], '賣出')
        strategy_engine.apply_fill(s, rt, i1[0], _t.time())
        self.assertEqual(rt['state'], 'SHORT')
        df_up = self._mkdf(list(df['Close']) + [rt['entry_price'] * 1.02])
        i2 = strategy_engine.evaluate_strategy(s, rt, df_up, _t.time(), '2026-07-16')
        self.assertEqual(i2[0]['action'], '買進'); self.assertIn('停損', i2[0]['reason'])
        strategy_engine.apply_fill(s, rt, i2[0], _t.time())
        self.assertLess(rt['realized_pnl_today'], 0)

    def test_risk_guards(self):
        import time as _t
        s = self._base_strategy()
        rt = strategy_engine.new_runtime(); rt['day'] = '2026-07-16'; rt['trades_today'] = 3
        ok, reason = strategy_engine.risk_check(s, rt, {'kind': 'OPEN', 'qty': 1}, _t.time())
        self.assertFalse(ok); self.assertIn('每日進場上限', reason)
        ok, _ = strategy_engine.risk_check(s, rt, {'kind': 'CLOSE', 'qty': 1}, _t.time())
        self.assertTrue(ok)  # 出場不受每日次數限制
        rt2 = strategy_engine.new_runtime(); rt2['last_order_ts'] = _t.time()
        ok, reason = strategy_engine.risk_check(s, rt2, {'kind': 'OPEN', 'qty': 1}, _t.time())
        self.assertFalse(ok); self.assertIn('冷卻', reason)
        s_loss = dict(s); s_loss['daily_loss_limit'] = 100.0
        rt3 = strategy_engine.new_runtime(); rt3['realized_pnl_today'] = -150.0
        ok, reason = strategy_engine.risk_check(s_loss, rt3, {'kind': 'OPEN', 'qty': 1}, _t.time())
        self.assertFalse(ok); self.assertIn('熔斷', reason)

    def test_cooldown_never_blocks_close_ADR065(self):
        """【ADR-065】出場單不受冷卻限制——持倉一定要能出得去。
        舊版冷卻檢查寫在 OPEN/CLOSE 共用區塊,連 CLOSE 也一併被擋,
        跟每日次數/虧損熔斷兩項風控 (只管 OPEN) 不一致,也違反
        risk_check 自己的文件與 ADR-035 §7。"""
        import time as _t
        s = self._base_strategy(); s['cooldown_sec'] = 300.0
        now = _t.time()
        rt = strategy_engine.new_runtime()
        rt['last_order_ts'] = now  # 剛剛才下過單 (模擬進場後立刻遇到出場訊號)
        ok, _ = strategy_engine.risk_check(s, rt, {'kind': 'CLOSE', 'qty': 1}, now)
        self.assertTrue(ok, "出場單不該被冷卻擋下")
        # OPEN 仍然要受冷卻限制 (行為不變)
        ok2, reason = strategy_engine.risk_check(s, rt, {'kind': 'OPEN', 'qty': 1}, now)
        self.assertFalse(ok2); self.assertIn('冷卻', reason)

    def test_apply_fill_close_does_not_touch_cooldown_clock_ADR065(self):
        """【ADR-065】apply_fill 平倉時不可更新 last_order_ts,否則 ADR-053
        的反手 (decision_to_intents 同一輪回傳 [平倉, 開倉]) 會因為平倉剛把
        last_order_ts 撥成現在,緊接著的開倉在 risk_check 算出「距離上次下單
        0 秒」而被冷卻擋下——反手永遠只完成一半,等於重演 ADR-053 想解決的
        問題,只是換了一個機制觸發。"""
        import time as _t
        s = self._base_strategy(); s['cooldown_sec'] = 300.0
        long_ago = _t.time() - 10000.0
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'entry_price': 100.0, 'qty': 1, 'last_order_ts': long_ago})
        now = _t.time()
        close_intent = {'kind': 'CLOSE', 'action': '賣出', 'qty': 1, 'price': 105.0, 'reason': 'x'}
        strategy_engine.apply_fill(s, rt, close_intent, now)
        self.assertEqual(rt['state'], 'FLAT')
        # 平倉沒有更新 last_order_ts,緊接著的反手開倉不該被冷卻擋下
        open_intent = {'kind': 'OPEN', 'action': '賣出', 'qty': 1, 'price': 105.0, 'reason': 'y'}
        ok, reason = strategy_engine.risk_check(s, rt, open_intent, now)
        self.assertTrue(ok, f"反手開倉不該被冷卻擋下,實際原因: {reason}")

    def test_condition_error_is_captured_not_silent_ADR065(self):
        """【ADR-065】條件函式拋例外時,eval_conditions 只讓「那一條」算 False,
        不中斷整組評估——這個隔離設計本身是對的,但舊版完全不留痕跡,那條件
        會從此永遠評估為 False,使用者看到的是「條件到了卻沒有動作」,卻查
        不出任何原因。用一個故意拋例外的假條件注入 CONDITIONS,驗證
        evaluate_strategy 會把錯誤記進 runtime['condition_errors'] 且不崩潰;
        換回正常條件後錯誤要被清除,不會一直殘留誤導人。"""
        def _boom(df, params):
            raise ValueError("測試用例外")
        strategy_engine.CONDITIONS['__test_boom__'] = ("測試炸彈", [], _boom)
        try:
            s = self._base_strategy()
            s['entry'] = [{'type': '__test_boom__', 'params': {}}]
            rt = strategy_engine.new_runtime()
            df = self._cross_df(up=True)
            intents = strategy_engine.evaluate_strategy(s, rt, df, 0.0, '2026-07-21')
            self.assertEqual(intents, [])  # 條件失敗 → 不進場,但不應該拋例外炸掉整個評估
            self.assertEqual(len(rt['condition_errors']), 1)
            self.assertIn('測試用例外', rt['condition_errors'][0][1])
            # 換回正常條件,錯誤紀錄要被蓋掉清除
            rt2 = strategy_engine.new_runtime()
            s2 = self._base_strategy()
            strategy_engine.evaluate_strategy(s2, rt2, df, 0.0, '2026-07-21')
            self.assertEqual(rt2['condition_errors'], [])
        finally:
            del strategy_engine.CONDITIONS['__test_boom__']

    def test_day_rollover_resets_counters(self):
        import time as _t
        s = self._base_strategy()
        rt = strategy_engine.new_runtime()
        rt.update({'day': '2026-07-15', 'trades_today': 3, 'realized_pnl_today': -500.0})
        strategy_engine.evaluate_strategy(s, rt, self._cross_df(up=True), _t.time(), '2026-07-16')
        self.assertEqual(rt['trades_today'], 0)
        self.assertEqual(rt['realized_pnl_today'], 0.0)

    def test_entry_time_window_blocks_open_outside_window_ADR066(self):
        """【ADR-066】entry_time_start/end、specific_entry_time 這幾個進場時間窗
        欄位在兩個策略編輯器都能設定,但舊版 evaluate_strategy 的 FLAT 分支
        (一般進場) 與 buy_and_hold 分支從未呼叫 filter_intents_by_time——內建
        策略的進場時間限制形同虛設,不管使用者怎麼設定,條件一成立就會進場,
        設定的時間窗完全不生效。這裡驗證:設一個必定排除目前這根K棒的時間窗,
        即使進場條件成立也不該產生 OPEN intent,且 runtime 要留下可查的說明
        (不能又是一種「條件到了卻沒有動作,查無原因」)。"""
        import time as _t
        s = self._base_strategy()
        s['entry_time_start'] = '23:59:00'; s['entry_time_end'] = '23:59:59'
        rt = strategy_engine.new_runtime()
        df = self._cross_df(up=True)
        intents = strategy_engine.evaluate_strategy(s, rt, df, _t.time(), '2026-07-16')
        self.assertEqual(intents, [], "進場時間窗外不應該進場")
        self.assertEqual(rt['state'], 'FLAT')
        self.assertTrue(rt['time_window_skips'], "被時間窗排除時應留下可查的說明")

    def test_entry_time_window_allows_open_inside_window_ADR066(self):
        """對照組:時間窗涵蓋整天時,進場條件成立仍應正常產生 OPEN intent,
        不能因為加了時間窗過濾就連帶壞掉既有行為。"""
        import time as _t
        s = self._base_strategy()
        s['entry_time_start'] = '00:00:00'; s['entry_time_end'] = '23:59:59'
        rt = strategy_engine.new_runtime()
        df = self._cross_df(up=True)
        intents = strategy_engine.evaluate_strategy(s, rt, df, _t.time(), '2026-07-16')
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'OPEN')
        self.assertEqual(rt['time_window_skips'], [])

    def test_buy_and_hold_accumulate_respects_entry_time_window_ADR066(self):
        """買進持有 (累積模式) 一樣要遵守進場時間窗——舊版這個分支也漏了
        filter_intents_by_time。"""
        import time as _t
        s = strategy_engine.new_strategy()
        s.update({'name': 'BNH', 'symbol': '0050', 'qty': 1, 'buy_and_hold': True,
                  'bnh_mode': 'accumulate',
                  'entry': [{'type': 'always_true', 'params': {}}],
                  'entry_time_start': '23:59:00', 'entry_time_end': '23:59:59'})
        rt = strategy_engine.new_runtime()
        df = self._mkdf([100.0] * 5)
        intents = strategy_engine.evaluate_strategy(s, rt, df, _t.time(), '2026-07-16')
        self.assertEqual(intents, [])
        self.assertTrue(rt['time_window_skips'])


class TestBacktest(unittest.TestCase):
    """【ADR-039】回測引擎:與實盤共用同一套策略邏輯,逐根重放歷史。"""

    @staticmethod
    def _mkdf(closes):
        idx = pd.date_range("2026-01-01 09:00", periods=len(closes), freq="1D")
        c = np.array(closes, dtype=float)
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c,
                             'Volume': [100] * len(c)}, index=idx)

    def _long_strategy(self):
        s = strategy_engine.new_strategy()
        s.update({'name': 'BT', 'symbol': 'X', 'qty': 1, 'direction': '做多',
                  'entry': [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}],
                  'exit_signals': [{'type': 'ma_cross_down', 'params': {'fast': 3, 'slow': 10}}],
                  'stop_loss_pct': 0, 'take_profit_pct': 0})
        return s

    def test_long_backtest_produces_trades_and_report(self):
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(10)]
        df = self._mkdf(closes)
        r = backtest.run_backtest(self._long_strategy(), df)
        self.assertGreaterEqual(r['metrics']['trades'], 1)
        self.assertGreater(r['trades'][0]['pnl'], 0)  # 金叉後大漲第一筆獲利
        self.assertEqual(len(r['equity']), len(df) - 2)
        for k in ('entry_ts', 'exit_ts', 'entry_price', 'exit_price', 'pnl', 'bars_held', 'exit_reason'):
            self.assertIn(k, r['trades'][0])
        self.assertTrue(any(m['kind'] == 'buy_open' for m in r['markers']))
        self.assertTrue(any(m['kind'] == 'sell_close' for m in r['markers']))

    def test_backtest_watch_ab_exec_df(self):
        # 【ADR-075】看A做B 回測:A 決定進出場時機,成交價來自 B。
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(10)]
        a_df = self._mkdf(closes)              # A:訊號來源
        b_df = self._mkdf([c * 10 for c in closes])  # B:價格是 A 的 10 倍 (同時間戳)
        s = self._long_strategy()
        r_plain = backtest.run_backtest(s, a_df)                 # 看A做A
        r_watch = backtest.run_backtest(s, a_df, exec_df=b_df)   # 看A做B
        self.assertEqual(r_watch['metrics']['trades'], r_plain['metrics']['trades'])  # 交易「筆數/時機」相同
        # 成交價來自 B (約 A 的 10 倍),故第一筆進場價明顯不同、且損益放大約 10 倍
        self.assertAlmostEqual(r_watch['trades'][0]['entry_price'],
                               r_plain['trades'][0]['entry_price'] * 10, delta=1e-6)
        self.assertGreater(abs(r_watch['trades'][0]['pnl']), abs(r_plain['trades'][0]['pnl']))

    def test_backtest_exec_df_none_equals_plain(self):
        # exec_df=None 與不帶 exec_df 結果一致 (向下相容)。
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(10)]
        df = self._mkdf(closes)
        a = backtest.run_backtest(self._long_strategy(), df)['metrics']['total_pnl']
        b = backtest.run_backtest(self._long_strategy(), df, exec_df=None)['metrics']['total_pnl']
        self.assertEqual(a, b)

    def test_backtest_equals_live_logic(self):
        # 回測第一個進場點必須等於引擎判定的金叉點「之後下一根」(證明同一套邏輯)。
        # 【ADR-064】引擎用「金叉當根收盤前」的已收盤資料判定訊號 (eval_window
        # 不含當根),真正成交在訊號確認後的下一根開盤 (T+1),所以是 cross_i+1
        # 而不是 cross_i 本身——這不是誤差,是刻意的成交時機設計 (防止用到還沒
        # 走完的當根資料,見 P-49)。
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(10)]
        df = self._mkdf(closes)
        r = backtest.run_backtest(self._long_strategy(), df)
        f = df['Close'].rolling(3).mean(); sl = df['Close'].rolling(10).mean()
        cross_i = next(i for i in range(1, len(df)) if f.iloc[i-1] <= sl.iloc[i-1] and f.iloc[i] > sl.iloc[i])
        first_open = next(m['ts'] for m in r['markers'] if m['kind'] == 'buy_open')
        self.assertEqual(first_open, df.index[cross_i + 1])

    def test_fee_and_slippage_reduce_pnl(self):
        # 【ADR-050】預設已套用真實成本模型;fee_rate 是舊參數,只在
        # apply_cost_model=False 時生效 —— 兩條路徑分開驗證。
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(10)]
        df = self._mkdf(closes)
        raw = backtest.run_backtest(self._long_strategy(), df, apply_cost_model=False)['metrics']['total_pnl']
        legacy_fee = backtest.run_backtest(self._long_strategy(), df, fee_rate=0.01,
                                           apply_cost_model=False)['metrics']['total_pnl']
        with_cost = backtest.run_backtest(self._long_strategy(), df)['metrics']['total_pnl']
        slip = backtest.run_backtest(self._long_strategy(), df, slippage_ticks=1, tick_size=1,
                                     apply_cost_model=False)['metrics']['total_pnl']
        self.assertLess(legacy_fee, raw)
        self.assertLess(with_cost, raw, "預設成本模型必須實際扣減淨損益")
        self.assertLess(slip, raw)

    def test_short_backtest(self):
        # 做空友善資料:先漲→死叉做空進場→續跌→金叉出場,跌段做空獲利
        closes = [100 + i for i in range(20)] + [120 - i * 3 for i in range(10)] + [93 + i * 2 for i in range(10)]
        df = self._mkdf(closes)
        s = strategy_engine.new_strategy()
        s.update({'name': 'S', 'symbol': 'TXF', 'market': '台期貨', 'qty': 1, 'direction': '做空',
                  'entry': [{'type': 'ma_cross_down', 'params': {'fast': 3, 'slow': 10}}],
                  'exit_signals': [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}],
                  'stop_loss_pct': 0})
        r = backtest.run_backtest(s, df)
        self.assertGreaterEqual(r['metrics']['trades'], 1)
        self.assertEqual(r['trades'][0]['direction'], '做空')
        self.assertGreater(r['trades'][0]['pnl'], 0)

    def test_metrics_fields_and_ranges(self):
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(10)]
        df = self._mkdf(closes)
        m = backtest.run_backtest(self._long_strategy(), df)['metrics']
        for k in ('total_pnl', 'total_return_pct', 'win_rate', 'max_drawdown', 'trades',
                  'wins', 'losses', 'profit_factor', 'avg_bars_held', 'avg_win', 'avg_loss'):
            self.assertIn(k, m)
        self.assertGreaterEqual(m['win_rate'], 0); self.assertLessEqual(m['win_rate'], 100)
        self.assertGreaterEqual(m['max_drawdown'], 0)

    def test_insufficient_data(self):
        r = backtest.run_backtest(self._long_strategy(), self._mkdf([1, 2]))
        self.assertEqual(r['metrics']['trades'], 0)


class TestCustomStrategy(unittest.TestCase):
    """【ADR-040】自訂 Python 策略:on_bar 介面、決策正規化、轉 intent、回測同路。"""

    @staticmethod
    def _mkdf(closes):
        idx = pd.date_range("2026-01-01 09:00", periods=len(closes), freq="1D")
        c = np.array(closes, dtype=float)
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c,
                             'Volume': [100] * len(c)}, index=idx)

    def test_example_strategy_runs(self):
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)]
        df = self._mkdf(closes)
        f = df['Close'].rolling(3).mean(); sl = df['Close'].rolling(10).mean()
        cross_i = next(i for i in range(1, len(df)) if f.iloc[i-1] <= sl.iloc[i-1] and f.iloc[i] > sl.iloc[i])
        d = custom_strategy.run_on_bar(custom_strategy.EXAMPLE_SOURCE, df.iloc[:cross_i+1], 'FLAT', {'fast': 3, 'slow': 10})
        self.assertEqual(d, 'BUY')

    def test_normalize_decision(self):
        self.assertEqual(custom_strategy.normalize_decision(None), 'HOLD')
        self.assertEqual(custom_strategy.normalize_decision('買進'), 'BUY')
        self.assertEqual(custom_strategy.normalize_decision('buy'), 'BUY')
        self.assertEqual(custom_strategy.normalize_decision('亂寫'), 'HOLD')  # 無法辨識→安全 HOLD

    def test_errors_raise_strategyerror(self):
        df = self._mkdf([1, 2, 3, 4, 5])
        with self.assertRaises(custom_strategy.StrategyError):
            custom_strategy.run_on_bar("def wrong(): pass", df, 'FLAT')  # 缺 on_bar
        with self.assertRaises(custom_strategy.StrategyError):
            custom_strategy.run_on_bar("def on_bar(ctx): return 1/0", df, 'FLAT')  # 執行例外
        with self.assertRaises(custom_strategy.StrategyError):
            custom_strategy.run_on_bar("!!! not python", df, 'FLAT')  # 語法錯

    def test_decision_to_intent(self):
        rt = strategy_engine.new_runtime()
        s = {'qty': 2, 'market': '台股', 'direction': '做多'}
        i = custom_strategy.decision_to_intent('BUY', s, rt, 105.0)
        self.assertEqual((i['kind'], i['action'], i['qty']), ('OPEN', '買進', 2))
        rt['state'] = 'LONG'; rt['qty'] = 2
        i2 = custom_strategy.decision_to_intent('CLOSE', s, rt, 110.0)
        self.assertEqual((i2['kind'], i2['action']), ('CLOSE', '賣出'))
        # 股票不可放空
        self.assertIsNone(custom_strategy.decision_to_intent('SELL', {'qty': 1, 'market': '台股'}, strategy_engine.new_runtime(), 100))
        # 期貨可開空
        i3 = custom_strategy.decision_to_intent('SELL', {'qty': 1, 'market': '台期貨', 'direction': '做空'}, strategy_engine.new_runtime(), 100)
        self.assertEqual((i3['kind'], i3['action']), ('OPEN', '賣出'))
        self.assertIsNone(custom_strategy.decision_to_intent('HOLD', s, strategy_engine.new_runtime(), 100))

    def test_custom_backtest_matches_builtin(self):
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(10)]
        df = self._mkdf(closes)
        custom = {'kind': 'custom', 'name': 'C', 'symbol': '2330', 'market': '台股', 'qty': 1, 'direction': '做多',
                  'source_code': custom_strategy.EXAMPLE_SOURCE, 'custom_params': {'fast': 3, 'slow': 10}, 'stop_loss_pct': 0}
        rc = backtest.run_backtest(custom, df)
        builtin = strategy_engine.new_strategy()
        builtin.update({'name': 'B', 'symbol': '2330', 'qty': 1, 'direction': '做多',
                        'entry': [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}],
                        'exit_signals': [{'type': 'ma_cross_down', 'params': {'fast': 3, 'slow': 10}}], 'stop_loss_pct': 0})
        rb = backtest.run_backtest(builtin, df)
        c_open = next(m['ts'] for m in rc['markers'] if m['kind'] == 'buy_open')
        b_open = next(m['ts'] for m in rb['markers'] if m['kind'] == 'buy_open')
        self.assertEqual(c_open, b_open)  # 等價自訂策略進場點=內建

    def test_custom_backtest_error_safe(self):
        df = self._mkdf([100 - i for i in range(20)] + [80 + i * 3 for i in range(10)])
        bad = {'kind': 'custom', 'name': 'X', 'symbol': '2330', 'market': '台股', 'qty': 1, 'direction': '做多',
               'source_code': "def on_bar(ctx): return 1/0", 'custom_params': {}, 'stop_loss_pct': 0}
        r = backtest.run_backtest(bad, df)
        self.assertEqual(r['metrics']['trades'], 0)  # 出錯→回測不崩


class TestTradeTypeAndAbsStops(unittest.TestCase):
    """【ADR-043】交易種類 (股票/零股/期貨)、絕對停損停利、回測計價單位。"""

    @staticmethod
    def _mkdf(closes):
        idx = pd.date_range("2026-01-01", periods=len(closes), freq="1D")
        c = np.array(closes, dtype=float)
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c,
                             'Volume': [100] * len(c)}, index=idx)

    def test_trade_type_units(self):
        self.assertEqual(strategy_engine.qty_unit_of({'trade_type': '股票'}), '張')
        self.assertEqual(strategy_engine.qty_unit_of({'trade_type': '零股'}), '股')
        self.assertEqual(strategy_engine.qty_unit_of({'trade_type': '期貨'}), '口')
        self.assertEqual(strategy_engine.trade_type_of({'market': '台期貨'}), '期貨')  # 舊策略相容

    def test_backtest_price_units(self):
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(20)]
        df = self._mkdf(closes)
        base = {'name': 'T', 'symbol': 'X', 'qty': 1, 'direction': '做多',
                'entry': [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}],
                'exit_signals': [{'type': 'ma_cross_down', 'params': {'fast': 3, 'slow': 10}}], 'stop_loss_pct': 0}
        # 【ADR-050】此測試驗的是「單位換算」,關掉成本模型才能做等式比對
        s_stk = dict(base); s_stk['trade_type'] = '股票'
        r_stk = backtest.run_backtest(s_stk, df, apply_cost_model=False)
        diff = r_stk['trades'][0]['exit_price'] - r_stk['trades'][0]['entry_price']
        self.assertAlmostEqual(r_stk['trades'][0]['pnl'], diff * 1000)  # 股票×1000
        s_odd = dict(base); s_odd['trade_type'] = '零股'
        r_odd = backtest.run_backtest(s_odd, df, apply_cost_model=False)
        self.assertAlmostEqual(r_odd['trades'][0]['pnl'], diff * 1)      # 零股×1
        s_fut = dict(base); s_fut['trade_type'] = '期貨'; s_fut['symbol'] = 'TXF'; s_fut['market'] = '台期貨'
        r_fut = backtest.run_backtest(s_fut, df, apply_cost_model=False)
        d2 = r_fut['trades'][0]['exit_price'] - r_fut['trades'][0]['entry_price']
        self.assertAlmostEqual(r_fut['trades'][0]['pnl'], d2 * 200)      # TXF×200

    def test_absolute_stops(self):
        import time as _t
        s = strategy_engine.new_strategy()
        s.update({'name': 'A', 'symbol': '2330', 'trade_type': '股票', 'qty': 1, 'direction': '做多',
                  'entry': [{'type': 'ma_cross_up', 'params': {}}], 'stop_loss_pct': 0, 'stop_loss_abs': 5.0})
        rt = strategy_engine.new_runtime(); rt.update({'state': 'LONG', 'entry_price': 100.0, 'qty': 1, 'day': '2026-01-15'})
        df = self._mkdf([100] * 15 + [94.0])  # 跌6元 > 停損5元
        i = strategy_engine.evaluate_strategy(s, rt, df, _t.time(), '2026-01-16')
        self.assertEqual(len(i), 1); self.assertIn('停損', i[0]['reason']); self.assertIn('元', i[0]['reason'])
        # 期貨點數停利
        s2 = strategy_engine.new_strategy()
        s2.update({'name': 'F', 'symbol': 'TXF', 'trade_type': '期貨', 'market': '台期貨', 'qty': 1, 'direction': '做多',
                   'entry': [{'type': 'ma_cross_up', 'params': {}}], 'take_profit_abs': 50.0})
        rt2 = strategy_engine.new_runtime(); rt2.update({'state': 'LONG', 'entry_price': 18000.0, 'qty': 1, 'day': '2026-01-15'})
        df2 = self._mkdf([18000] * 15 + [18060.0])
        i2 = strategy_engine.evaluate_strategy(s2, rt2, df2, _t.time(), '2026-01-16')
        self.assertEqual(len(i2), 1); self.assertIn('停利', i2[0]['reason']); self.assertIn('點', i2[0]['reason'])

    def test_paper_odd_lot(self):
        a = paper_account.new_account(1000000)
        paper_account.apply_fill(a, 't', '台股', '2330', '買進', 'OPEN', 10, 600.0, trade_type='零股')
        self.assertGreater(a['cash'], 993000)   # 10股×600≈6000,不是6百萬
        b = paper_account.new_account(1000000)
        paper_account.apply_fill(b, 't', '台股', '2330', '買進', 'OPEN', 1, 600.0, trade_type='股票')
        self.assertLess(b['cash'], 941000)       # 1張×1000×600=60萬

    def test_validate_short_only_futures(self):
        s = strategy_engine.new_strategy()
        s.update({'name': 'V', 'symbol': '2330', 'trade_type': '股票', 'direction': '做空',
                  'entry': [{'type': 'ma_cross_up', 'params': {}}], 'stop_loss_pct': 2.0})
        self.assertFalse(strategy_engine.validate_strategy(s)[0])  # 股票做空擋
        s2 = dict(s); s2['trade_type'] = '期貨'; s2['market'] = '台期貨'
        self.assertTrue(strategy_engine.validate_strategy(s2)[0])   # 期貨做空放行


class TestCustomFreedomAndMetrics(unittest.TestCase):
    """【ADR-044】Ctx 自由度升級 (state/進場價/新指標/log) 與回測進階指標。"""

    @staticmethod
    def _mkdf(closes):
        idx = pd.date_range("2026-01-01", periods=len(closes), freq="1D")
        c = np.array(closes, dtype=float)
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c,
                             'Volume': [100] * len(c)}, index=idx)

    def test_ctx_state_and_fields(self):
        df = self._mkdf(list(np.linspace(100, 120, 30)))
        code = ("def on_bar(ctx):\n"
                "    ctx.state['n'] = ctx.state.get('n', 0) + 1\n"
                "    ctx.log('x')\n"
                "    _ = (ctx.open, ctx.high, ctx.low, ctx.volume, ctx.time)\n"
                "    _ = ctx.atr(5).iloc[-1]; _ = ctx.vwap().iloc[-1]\n"
                "    _ = ctx.roc(5).iloc[-1]; _ = ctx.stddev(5).iloc[-1]\n"
                "    if ctx.position == 'LONG' and ctx.bars_in_position >= 2:\n"
                "        return ctx.close_position()\n"
                "    return None\n")
        d, ctx = custom_strategy.run_on_bar(code, df, 'FLAT', {}, state={'n': 4}, return_ctx=True)
        self.assertEqual(ctx.state['n'], 5)
        self.assertEqual(len(ctx._logs), 1)
        d2 = custom_strategy.run_on_bar(code, df, 'LONG', {}, entry_price=100.0, bars_in_position=3)
        self.assertEqual(d2, 'CLOSE')

    def test_backtest_advanced_metrics(self):
        closes = [100 - i for i in range(20)] + [80 + i * 3 for i in range(10)] + [107 - i * 2 for i in range(20)]
        df = self._mkdf(closes)
        s = {'name': 'T', 'symbol': 'X', 'trade_type': '股票', 'qty': 1, 'direction': '做多',
             'entry': [{'type': 'ma_cross_up', 'params': {'fast': 3, 'slow': 10}}],
             'exit_signals': [{'type': 'ma_cross_down', 'params': {'fast': 3, 'slow': 10}}], 'stop_loss_pct': 0}
        m = backtest.run_backtest(s, df, fee_rate=0.001425)['metrics']
        for k in ('ann_return_pct', 'sharpe', 'max_drawdown_pct', 'expectancy', 'win_loss_ratio',
                  'max_consec_wins', 'max_consec_losses', 'total_fee', 'buy_hold_pnl',
                  'buy_hold_pct', 'max_bars_held'):
            self.assertIn(k, m)
        self.assertGreater(m['total_fee'], 0)
        self.assertGreaterEqual(m['max_drawdown_pct'], 0)


class TestValidateSource(unittest.TestCase):
    """【ADR-045】自訂策略程式碼 AST 靜態檢查 (絕不 exec 使用者程式碼)。"""

    def test_valid_minimal(self):
        ok, msg = custom_strategy.validate_source("def on_bar(ctx):\n    return ctx.hold()")
        self.assertTrue(ok); self.assertEqual(msg, "")

    def test_example_source_passes(self):
        ok, msg = custom_strategy.validate_source(custom_strategy.EXAMPLE_SOURCE)
        self.assertTrue(ok, msg)

    def test_empty_source(self):
        ok, msg = custom_strategy.validate_source("")
        self.assertFalse(ok); self.assertIn("空白", msg)

    def test_syntax_error_reports_line(self):
        ok, msg = custom_strategy.validate_source("def on_bar(ctx)\n    pass")
        self.assertFalse(ok); self.assertIn("語法錯誤", msg); self.assertIn("第 1 行", msg)

    def test_missing_on_bar(self):
        ok, msg = custom_strategy.validate_source("def check(ctx):\n    return None")
        self.assertFalse(ok); self.assertIn("on_bar", msg)

    def test_on_bar_needs_arg(self):
        ok, msg = custom_strategy.validate_source("def on_bar():\n    return None")
        self.assertFalse(ok); self.assertIn("參數", msg)

    def test_standalone_script_gets_guidance(self):
        # 使用者把「獨立腳本」整份貼進來 (真實案例):要給教學提示,不是 NameError
        src = ("import schedule\n"
               "def check_and_trade():\n    pass\n"
               "if __name__ == \"__main__\":\n"
               "    login_api()\n"
               "    while True:\n"
               "        schedule.run_pending()\n")
        ok, msg = custom_strategy.validate_source(src)
        self.assertFalse(ok)
        self.assertIn("on_bar", msg)
        self.assertIn("獨立腳本", msg)

    def test_on_bar_plus_toplevel_loop_rejected(self):
        # 有 on_bar 但頂層還留著 while True → 子行程會卡死,必須擋下並說明
        src = ("def on_bar(ctx):\n    return ctx.hold()\n"
               "while True:\n    pass\n")
        ok, msg = custom_strategy.validate_source(src)
        self.assertFalse(ok); self.assertIn("頂層", msg)

    def test_never_executes_user_code(self):
        # 靜態檢查絕不執行使用者程式碼:頂層若被執行會寫檔,檢查後檔案不得存在
        import tempfile
        marker = os.path.join(tempfile.gettempdir(), "_adr045_exec_marker")
        if os.path.exists(marker):
            os.remove(marker)
        src = (f"open({marker!r}, 'w').write('executed')\n"
               "def on_bar(ctx):\n    return ctx.hold()\n")
        custom_strategy.validate_source(src)
        self.assertFalse(os.path.exists(marker), "validate_source 不應執行使用者程式碼")


class TestFutCatalog(unittest.TestCase):
    """【ADR-046】期貨代號樣式判斷 + 名稱正規化。"""

    def test_monthly_and_continuous_symbols(self):
        for s in ('TXFR1', 'TXFR2', 'MXF202608', 'CDF202607', 'TXF202609'):
            self.assertTrue(fut_catalog.looks_like_futures_symbol(s), s)

    def test_weekly_symbols_with_digit_code(self):
        # 週契約商品碼含數字 (舊版誤判成台股的根因)
        for s in ('MX1R1', 'MX2R2', 'MX4R1', 'MX5202607', 'MX1202607'):
            self.assertTrue(fut_catalog.looks_like_futures_symbol(s), s)

    def test_stock_codes_rejected(self):
        for s in ('2330', '0050', '00631L', 'SPYM', 'TXF', 'MXF', '', None, '2330R1'):
            self.assertFalse(fut_catalog.looks_like_futures_symbol(s), s)

    def test_display_name_fixes_polluted_mxf(self):
        # 使用者實例:shioaji 把 MXFR1 命名為「小型臺指W2近月」
        self.assertEqual(fut_catalog.display_name('MXFR1', '小型臺指W2近月'), '小型臺指期貨近月')
        self.assertEqual(fut_catalog.display_name('MXFR2', '小型臺指W2次月'), '小型臺指期貨次月')
        self.assertEqual(fut_catalog.display_name('MXF202608', '小型臺指W208'), '小型臺指期貨08')

    def test_display_name_keeps_correct_names(self):
        self.assertEqual(fut_catalog.display_name('TXFR1', '臺股期貨近月'), '臺股期貨近月')
        self.assertEqual(fut_catalog.display_name('TMFR1', '微型臺指期貨近月'), '微型臺指期貨近月')

    def test_display_name_weekly(self):
        self.assertEqual(fut_catalog.display_name('MX4R1', '小型臺指W207近月'), '小型臺指W4週契約近月')

    def test_unknown_products_untouched(self):
        # 表外商品 (股票期貨等) 寧缺勿錯:原樣沿用 shioaji 名稱
        self.assertEqual(fut_catalog.display_name('CDF202607', '台積電期貨07'), '台積電期貨07')
        self.assertEqual(fut_catalog.display_name('ZZZ202607', ''), 'ZZZ202607')


class TestAiHelper(unittest.TestCase):
    """【ADR-049】AI 策略助手純邏輯:payload 組裝 + 回應程式碼抽取。"""

    def test_payload_structure(self):
        p = ai_helper.build_messages_payload("RSI 低於 30 做多", model="m-test", max_tokens=500)
        self.assertEqual(p['model'], 'm-test')
        self.assertEqual(p['max_tokens'], 500)
        self.assertEqual(p['messages'], [{'role': 'user', 'content': 'RSI 低於 30 做多'}])
        self.assertIn('on_bar(ctx)', p['system'])

    def test_payload_empty_description(self):
        with self.assertRaises(ValueError):
            ai_helper.build_messages_payload("   ")

    def test_extract_plain_code(self):
        r = {'content': [{'type': 'text', 'text': 'def on_bar(ctx):\n    return ctx.hold()'}]}
        self.assertTrue(ai_helper.extract_code(r).startswith('def on_bar'))

    def test_extract_strips_markdown_fences(self):
        r = {'content': [{'type': 'text', 'text': '```python\ndef on_bar(ctx):\n    return ctx.buy()\n```'}]}
        code = ai_helper.extract_code(r)
        self.assertTrue(code.startswith('def on_bar'))
        self.assertNotIn('```', code)

    def test_extract_from_raw_json_bytes(self):
        import json as _json
        raw = _json.dumps({'content': [{'type': 'text', 'text': 'def on_bar(ctx):\n    pass'}]}).encode()
        self.assertIn('on_bar', ai_helper.extract_code(raw))

    def test_extract_api_error_raises(self):
        with self.assertRaises(ValueError):
            ai_helper.extract_code({'type': 'error', 'error': {'type': 'authentication_error', 'message': 'bad key'}})

    def test_extract_empty_content_raises(self):
        with self.assertRaises(ValueError):
            ai_helper.extract_code({'content': []})

    def test_generated_code_flows_into_validator(self):
        # AI 產出 → 靜態檢查 同一條管線 (不特殊對待)
        r = {'content': [{'type': 'text', 'text': 'def on_bar(ctx):\n    return ctx.hold()'}]}
        code = ai_helper.extract_code(r)
        ok, msg = custom_strategy.validate_source(code)
        self.assertTrue(ok, msg)


class TestAiConfigStore(unittest.TestCase):
    """【ADR-049】AI 設定檔存取。"""

    def test_roundtrip_and_missing(self):
        import tempfile
        p = os.path.join(tempfile.gettempdir(), '_adr049_ai.json')
        config_store.save_ai_config(p, 'sk-x', 'claude-sonnet-4-6')
        d = config_store.load_ai_config(p)
        self.assertEqual(d['api_key'], 'sk-x')
        self.assertEqual(d['model'], 'claude-sonnet-4-6')
        self.assertEqual(config_store.load_ai_config(p + '.none'), {})
        os.remove(p)


class TestCostModel(unittest.TestCase):
    """【ADR-050】台灣交易成本模型。"""

    def test_futures_side_cost(self):
        fee, tax = cost_model.side_cost('期貨', 'TXF', 20000, 1, 200.0, is_sell=False)
        self.assertAlmostEqual(fee, 50.0)
        self.assertAlmostEqual(tax, 20000 * 200 * 0.00002)

    def test_futures_tax_both_sides(self):
        c = cost_model.round_trip_cost('期貨', 'TXF', 20000, 21000, 1, 200.0)
        self.assertAlmostEqual(c['fee'], 100.0)          # 兩邊各 50
        self.assertGreater(c['tax'], 0)                   # 買賣皆收期交稅
        self.assertAlmostEqual(c['total'], c['fee'] + c['tax'])

    def test_stock_tax_only_on_sell(self):
        _, tax_buy = cost_model.side_cost('股票', '2330', 500, 1, 1000.0, is_sell=False)
        _, tax_sell = cost_model.side_cost('股票', '2330', 500, 1, 1000.0, is_sell=True)
        self.assertEqual(tax_buy, 0.0)
        self.assertAlmostEqual(tax_sell, 500 * 1000 * 0.003)

    def test_etf_lower_tax(self):
        _, t_etf = cost_model.side_cost('股票', '0050', 100, 1, 1000.0, is_sell=True)
        _, t_stk = cost_model.side_cost('股票', '2330', 100, 1, 1000.0, is_sell=True)
        self.assertLess(t_etf, t_stk)
        self.assertTrue(cost_model.is_etf('0050'))
        self.assertFalse(cost_model.is_etf('2330'))

    def test_minimum_fee_applied(self):
        fee, _ = cost_model.side_cost('零股', '2330', 10, 1, 1.0, is_sell=False)
        self.assertGreaterEqual(fee, cost_model.ODD_FEE_MIN)

    def test_discount_param(self):
        base, _ = cost_model.side_cost('股票', '2330', 500, 10, 1000.0, is_sell=False)
        disc, _ = cost_model.side_cost('股票', '2330', 500, 10, 1000.0, is_sell=False,
                                       params={'fee_discount': 0.6})
        self.assertAlmostEqual(disc, base * 0.6, places=6)

    def test_short_direction_taxes_entry_sell(self):
        c = cost_model.round_trip_cost('期貨', 'TXF', 20000, 19000, 1, 200.0, direction='做空')
        self.assertGreater(c['total'], 0)


class TestBacktestCosts(unittest.TestCase):
    """【ADR-050】回測必須實際扣掉成本 (舊版 fee_rate 寫死 0 → 總成本恆為 0)。"""

    def _fixture(self):
        import numpy as _np
        idx = pd.date_range('2022-01-01', periods=300, freq='D')
        c = 12000 + _np.cumsum(_np.random.RandomState(7).randn(300) * 80)
        df = pd.DataFrame({'Open': c, 'High': c + 50, 'Low': c - 50,
                           'Close': c, 'Volume': 1000}, index=idx)
        s = {'kind': 'custom', 'trade_type': '期貨', 'market': '台期貨', 'symbol': 'TXF',
             'qty': 1, 'direction': '做多', 'timeframe': '日K',
             'stop_loss_pct': 0, 'take_profit_pct': 0,
             'source_code': ("def on_bar(ctx):\n"
                             "    f = ctx.sma(5); s = ctx.sma(20)\n"
                             "    if ctx.position == 'FLAT' and ctx.cross_up(f, s): return ctx.buy()\n"
                             "    if ctx.position == 'LONG' and ctx.cross_down(f, s): return ctx.close_position()\n"
                             "    return ctx.hold()"),
             'entry': [{'type': 'ma_cross_up', 'params': {}}], 'exit_signals': []}
        return s, df

    def test_cost_is_nonzero_and_identity_holds(self):
        s, df = self._fixture()
        m = backtest.run_backtest(s, df)['metrics']
        self.assertGreater(m['trades'], 0)
        self.assertGreater(m['total_cost'], 0, "回測總成本不應為 0")
        self.assertAlmostEqual(m['gross_pnl'] - m['total_cost'], m['total_pnl'], places=6)

    def test_cost_reduces_net_pnl(self):
        s, df = self._fixture()
        with_cost = backtest.run_backtest(s, df)['metrics']
        no_cost = backtest.run_backtest(s, df, apply_cost_model=False)['metrics']
        self.assertLess(with_cost['total_pnl'], no_cost['total_pnl'])

    def test_extended_metrics_present(self):
        s, df = self._fixture()
        m = backtest.run_backtest(s, df)['metrics']
        for k in ('total_fee', 'total_tax', 'total_cost', 'gross_pnl', 'cost_desc',
                  'cost_ratio_pct', 'avg_cost_per_trade', 'best_trade', 'worst_trade',
                  'gross_profit', 'gross_loss', 'long_trades', 'short_trades',
                  'exposure_pct'):
            self.assertIn(k, m)
        self.assertGreaterEqual(m['best_trade'], m['worst_trade'])
        self.assertGreaterEqual(m['exposure_pct'], 0.0)

    def test_empty_result_has_all_keys(self):
        # 無交易時報告視窗也會取這些欄位,不可 KeyError
        m = backtest.run_backtest({'kind': 'custom', 'trade_type': '期貨', 'symbol': 'TXF',
                                   'qty': 1, 'direction': '做多',
                                   'source_code': 'def on_bar(ctx):\n    return ctx.hold()',
                                   'entry': [], 'exit_signals': []},
                                  pd.DataFrame())['metrics']
        for k in ('total_cost', 'gross_pnl', 'cost_ratio_pct', 'exposure_pct', 'best_trade'):
            self.assertIn(k, m)


SAR_SRC = """def on_bar(ctx):
    e = ctx.ema(ctx.param('fast', 7)); s = ctx.sma(ctx.param('slow', 25))
    if ctx.cross_up(e, s) and ctx.position in ('FLAT', 'SHORT'): return ctx.buy()
    if ctx.cross_down(e, s) and ctx.position in ('FLAT', 'LONG'): return ctx.sell()
    return ctx.hold()"""


def _sar_fixture(seed=3, n=400):
    import numpy as _np
    idx = pd.date_range('2022-01-01', periods=n, freq='D')
    c = 12000 + _np.cumsum(_np.random.RandomState(seed).randn(n) * 100)
    df = pd.DataFrame({'Open': c, 'High': c + 50, 'Low': c - 50,
                       'Close': c, 'Volume': 1000}, index=idx)
    s = {'kind': 'custom', 'trade_type': '期貨', 'market': '台期貨', 'symbol': 'TXF',
         'qty': 1, 'direction': '做多', 'timeframe': '日K',
         'source_code': SAR_SRC, 'entry': [], 'exit_signals': [], 'custom_params': {}}
    return s, df


class TestStopAndReverse(unittest.TestCase):
    """【ADR-053】反手 (Stop and Reverse) 策略必須真的多空雙向。"""

    def test_state_from_action_not_direction(self):
        # direction 是佔位值 '做多',賣出開倉仍須進 SHORT
        rt = strategy_engine.new_runtime()
        strategy_engine.apply_fill({'direction': '做多', 'qty': 1}, rt,
                                   {'kind': 'OPEN', 'action': '賣出', 'qty': 1, 'price': 100}, 0)
        self.assertEqual(rt['state'], 'SHORT')
        rt2 = strategy_engine.new_runtime()
        strategy_engine.apply_fill({'direction': '做空', 'qty': 1}, rt2,
                                   {'kind': 'OPEN', 'action': '買進', 'qty': 1, 'price': 100}, 0)
        self.assertEqual(rt2['state'], 'LONG')

    def test_short_close_pnl_sign(self):
        # 空單在價格下跌時應為獲利 (舊版用 direction 判斷 → 正負相反)
        rt = strategy_engine.new_runtime()
        s = {'direction': '做多', 'qty': 1}
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '賣出', 'qty': 1, 'price': 100}, 0)
        strategy_engine.apply_fill(s, rt, {'kind': 'CLOSE', 'action': '買進', 'qty': 1, 'price': 90}, 1)
        self.assertGreater(rt['realized_pnl_today'], 0)

    def test_reverse_intents_long_to_short(self):
        rt = strategy_engine.new_runtime(); rt['state'] = 'LONG'; rt['qty'] = 1
        s = {'market': '台期貨', 'qty': 1}
        out = custom_strategy.decision_to_intents('SELL', s, rt, 100.0)
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0]['kind'], out[0]['action']), ('CLOSE', '賣出'))
        self.assertEqual((out[1]['kind'], out[1]['action']), ('OPEN', '賣出'))

    def test_reverse_intents_short_to_long(self):
        rt = strategy_engine.new_runtime(); rt['state'] = 'SHORT'; rt['qty'] = 1
        out = custom_strategy.decision_to_intents('BUY', {'market': '台期貨', 'qty': 1}, rt, 100.0)
        self.assertEqual([(i['kind'], i['action']) for i in out],
                         [('CLOSE', '買進'), ('OPEN', '買進')])

    def test_close_decision_does_not_reverse(self):
        rt = strategy_engine.new_runtime(); rt['state'] = 'LONG'; rt['qty'] = 1
        out = custom_strategy.decision_to_intents('CLOSE', {'market': '台期貨', 'qty': 1}, rt, 100.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['kind'], 'CLOSE')

    def test_stock_cannot_reverse_short(self):
        # 非期貨:平多後不得開空
        rt = strategy_engine.new_runtime(); rt['state'] = 'LONG'; rt['qty'] = 1
        out = custom_strategy.decision_to_intents('SELL', {'market': '台股', 'qty': 1}, rt, 100.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['kind'], 'CLOSE')

    def test_backtest_records_both_directions(self):
        s, df = _sar_fixture()
        r = backtest.run_backtest(s, df)
        dirs = [t['direction'] for t in r['trades']]
        self.assertIn('做多', dirs)
        self.assertIn('做空', dirs)
        # 反手策略的已平倉部位必然多空交替
        for a, b in zip(dirs, dirs[1:]):
            self.assertNotEqual(a, b, "反手策略的交易方向應交替")

    def test_long_short_split_metrics(self):
        s, df = _sar_fixture()
        m = backtest.run_backtest(s, df)['metrics']
        self.assertGreater(m['long_trades'], 0)
        self.assertGreater(m['short_trades'], 0)
        self.assertAlmostEqual(m['long_pnl'] + m['short_pnl'], m['total_pnl'], places=6)
        self.assertLessEqual(m['max_single_loss'], 0.0)
        self.assertGreaterEqual(m['max_single_win'], 0.0)

    def test_backward_compat_single_intent(self):
        rt = strategy_engine.new_runtime()
        i = custom_strategy.decision_to_intent('BUY', {'market': '台期貨', 'qty': 1}, rt, 100.0)
        self.assertEqual(i['kind'], 'OPEN')


class TestOptimizer(unittest.TestCase):
    """【ADR-054】參數最佳化 (網格搜尋)。"""

    def test_parse_list_and_range(self):
        g = optimizer.parse_param_spec('fast=5,7,10; slow=20:35:5')
        self.assertEqual(g['fast'], [5, 7, 10])
        self.assertEqual(g['slow'], [20, 25, 30])
        self.assertEqual(optimizer.count_combos(g), 9)

    def test_parse_errors(self):
        for bad in ('', 'fast', 'fast=', '=5', 'fast=1:5:0'):
            with self.assertRaises(ValueError):
                optimizer.parse_param_spec(bad)

    def test_combo_cap(self):
        s, df = _sar_fixture(n=120)
        big = {'a': list(range(30)), 'b': list(range(30))}
        with self.assertRaises(ValueError):
            optimizer.optimize(s, df, big, max_combos=100)

    def test_optimize_returns_ranked_best(self):
        s, df = _sar_fixture()
        res = optimizer.optimize(s, df, optimizer.parse_param_spec('fast=5,7; slow=20,25'),
                                 objective='淨損益', min_trades=1)
        self.assertEqual(res['total'], 4)
        self.assertEqual(res['evaluated'], 4)
        self.assertIsNotNone(res['best'])
        scores = [r['score'] for r in res['results'] if r['eligible']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(res['best']['metrics']['total_pnl'],
                         max(r['metrics']['total_pnl'] for r in res['results'] if r['eligible']))

    def test_min_trades_filter(self):
        s, df = _sar_fixture()
        res = optimizer.optimize(s, df, optimizer.parse_param_spec('fast=5; slow=20'),
                                 min_trades=10 ** 6)
        self.assertIsNone(res['best'])
        self.assertEqual(res['filtered_out'], 1)

    def test_should_stop_interrupts(self):
        s, df = _sar_fixture()
        calls = {'n': 0}

        def _stop():
            calls['n'] += 1
            return calls['n'] > 2
        res = optimizer.optimize(s, df, optimizer.parse_param_spec('fast=3,5,7,9; slow=20,25'),
                                 min_trades=1, should_stop=_stop)
        self.assertLess(res['evaluated'], res['total'])

    def test_objective_max_drawdown_smaller_is_better(self):
        s, df = _sar_fixture()
        res = optimizer.optimize(s, df, optimizer.parse_param_spec('fast=5,7; slow=20,25'),
                                 objective='最大回撤(越小越好)', min_trades=1)
        elig = [r for r in res['results'] if r['eligible']]
        self.assertEqual(res['best']['metrics']['max_drawdown'],
                         min(r['metrics']['max_drawdown'] for r in elig))

    def test_walk_forward_split(self):
        s, df = _sar_fixture()
        wf = optimizer.walk_forward_check(s, df, {'fast': 7, 'slow': 25}, split_ratio=0.7)
        self.assertEqual(wf['split_index'], int(len(df) * 0.7))
        self.assertIn('total_pnl', wf['in_sample'])
        self.assertIn('total_pnl', wf['out_sample'])

    def test_walk_forward_too_short(self):
        s, df = _sar_fixture(n=15)
        with self.assertRaises(ValueError):
            optimizer.walk_forward_check(s, df, {'fast': 7, 'slow': 25})


class TestParamTraceability(unittest.TestCase):
    """【ADR-055】參數代入的可驗證性:程式實際讀到什麼、來源是哪裡。"""

    SRC = ('''
def on_bar(ctx):
    fast = ctx.sma(ctx.param('fast', 5))
    slow = ctx.sma(ctx.param('slow', 20))
    if ctx.position == 'FLAT' and ctx.cross_up(fast, slow):
        return ctx.buy()
    if ctx.position == 'LONG' and ctx.cross_down(fast, slow):
        return ctx.close_position()
    return None
''')

    def _df(self, n=120):
        idx = pd.date_range('2024-01-01', periods=n, freq='D')
        c = 100 + np.sin(np.arange(n) / 6.0) * 8 + np.arange(n) * 0.05
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c, 'Volume': 1000}, index=idx)

    def _strategy(self, params):
        return {'kind': 'custom', 'trade_type': '期貨', 'market': '台期貨', 'symbol': 'TXF',
                'qty': 1, 'direction': '做多', 'timeframe': '日K', 'source_code': self.SRC,
                'entry': [], 'exit_signals': [], 'custom_params': params}

    def test_ctx_param_records_source(self):
        ctx = custom_strategy.Ctx(self._df(), 'FLAT', params={'fast': 7})
        self.assertEqual(ctx.param('fast', 5), 7)
        self.assertEqual(ctx.param('slow', 20), 20)
        self.assertTrue(ctx.param_reads['fast']['from_params'])
        self.assertFalse(ctx.param_reads['slow']['from_params'])

    def test_backtest_reports_param_usage(self):
        r = backtest.run_backtest(self._strategy({'fast': 7, 'slow': 25}), self._df())
        self.assertEqual(r['param_given'], {'fast': 7, 'slow': 25})
        self.assertTrue(r['param_usage']['fast']['from_params'])
        self.assertEqual(r['param_usage']['fast']['value'], 7)

    def test_typo_param_is_flagged(self):
        # 參數視窗打成 'fastt' → 程式讀不到,必須被標示出來而不是安靜吃預設值
        r = backtest.run_backtest(self._strategy({'fastt': 7}), self._df())
        desc = custom_strategy.describe_param_usage(r['param_given'], r['param_usage'])
        self.assertIn('⚠', desc)
        self.assertIn('fastt', desc)
        self.assertFalse(r['param_usage']['fast']['from_params'])

    def test_describe_empty_for_builtin(self):
        self.assertEqual(custom_strategy.describe_param_usage({}, {}), "")

    def test_different_params_change_result(self):
        # 參數真的有代入的最直接證明:換參數,交易結果就不同
        df = self._df()
        a = backtest.run_backtest(self._strategy({'fast': 3, 'slow': 10}), df)
        b = backtest.run_backtest(self._strategy({'fast': 20, 'slow': 60}), df)
        self.assertNotEqual(len(a['trades']), len(b['trades']))

    def test_optimizer_surfaces_errors(self):
        bad = self._strategy({})
        bad['source_code'] = "def on_bar(ctx):\n    return ctx.buy()\n"
        df = self._df()
        res = optimizer.optimize(bad, df, {'fast': [3, 5]}, objective='淨損益', min_trades=1)
        self.assertIn('error_summary', res)
        self.assertIn('errors', res)


class TestIndicatorCachePerf(unittest.TestCase):
    """【ADR-056】Ctx 指標快取:結果必須與未快取版本完全一致,且絕不洩露未來資料。"""

    def _df(self, n=300, seed=7):
        idx = pd.date_range('2023-01-01', periods=n, freq='D')
        c = 100 + np.cumsum(np.random.RandomState(seed).randn(n) * 2)
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c, 'Volume': 1000}, index=idx)

    def test_cached_matches_uncached_sma(self):
        df = self._df()
        window = df.iloc[:150]
        ctx_nocache = custom_strategy.Ctx(window, 'FLAT')          # full_df 預設 = df,無加速效益但邏輯相同
        ctx_full = custom_strategy.Ctx(window, 'FLAT', full_df=df)  # 有完整未來資料可用於快取
        a = ctx_nocache.sma(20)
        b = ctx_full.sma(20)
        pd.testing.assert_series_equal(a, b, check_names=False)

    def test_no_lookahead_even_with_full_df(self):
        # 兩份 df 只有「未來」(第150根之後) 不同,取到第149根為止的視野必須完全一樣。
        df_a = self._df(seed=1)
        df_b = df_a.copy()
        df_b.iloc[150:] = df_b.iloc[150:] * 5.0  # 竄改未來資料
        window = df_a.iloc[:150]
        ra = custom_strategy.Ctx(window, 'FLAT', full_df=df_a).sma(20)
        rb = custom_strategy.Ctx(window, 'FLAT', full_df=df_b).sma(20)
        pd.testing.assert_series_equal(ra, rb, check_names=False)

    def test_repeated_calls_share_cache_across_bars(self):
        # 用同一個 state dict 模擬「同一次回測、跨K棒」呼叫,快取應該被重複命中。
        df = self._df()
        state = {}
        for i in range(50, 60):
            ctx = custom_strategy.Ctx(df.iloc[:i], 'FLAT', full_df=df, state=state)
            ctx.sma(20)
        self.assertIn(('sma', 20), state['_ind_cache'])
        self.assertEqual(len(state['_ind_cache'][('sma', 20)]), len(df))  # 整段只算一次,長度=完整df

    def test_tuple_indicators_cached_correctly(self):
        df = self._df()
        window = df.iloc[:200]
        k1, d1 = custom_strategy.Ctx(window, 'FLAT').kd()
        k2, d2 = custom_strategy.Ctx(window, 'FLAT', full_df=df).kd()
        pd.testing.assert_series_equal(k1, k2, check_names=False)
        pd.testing.assert_series_equal(d1, d2, check_names=False)

    def test_backtest_result_unchanged_by_caching(self):
        # 回測整體結果 (交易/損益) 必須與逐根重算完全一致 —— 這是這個優化
        # 唯一被允許的行為:只換快慢,不換答案。
        SRC = ("def on_bar(ctx):\n"
               "    fast = ctx.sma(5)\n"
               "    slow = ctx.sma(20)\n"
               "    if ctx.position == 'FLAT' and ctx.cross_up(fast, slow):\n"
               "        return ctx.buy()\n"
               "    if ctx.position == 'LONG' and ctx.cross_down(fast, slow):\n"
               "        return ctx.close_position()\n"
               "    return None\n")
        df = self._df(n=400, seed=42)
        s = {'kind': 'custom', 'trade_type': '期貨', 'market': '台期貨', 'symbol': 'TXF', 'qty': 1,
             'direction': '做多', 'timeframe': '日K', 'source_code': SRC, 'entry': [], 'exit_signals': [],
             'custom_params': {}}
        orig = custom_strategy.Ctx._cached

        def no_cache(self, key, compute):
            return custom_strategy.Ctx._slice_to(compute(self.df), len(self.df))
        custom_strategy.Ctx._cached = no_cache
        try:
            r_old = backtest.run_backtest(s, df)
        finally:
            custom_strategy.Ctx._cached = orig
        r_new = backtest.run_backtest(s, df)
        self.assertEqual(r_old['metrics']['total_pnl'], r_new['metrics']['total_pnl'])
        self.assertEqual(len(r_old['trades']), len(r_new['trades']))
        for ta, tb in zip(r_old['trades'], r_new['trades']):
            self.assertEqual(ta['direction'], tb['direction'])
            self.assertEqual(ta['entry_price'], tb['entry_price'])


class TestCompileCache(unittest.TestCase):
    """【ADR-056】策略原始碼 compile 快取:同一段程式碼不必每根K棒重新編譯。"""

    def test_same_source_reuses_compiled_fn(self):
        src = "def on_bar(ctx):\n    return None\n"
        fn1, err1 = custom_strategy._compile_on_bar(src)
        fn2, err2 = custom_strategy._compile_on_bar(src)
        self.assertIsNone(err1); self.assertIsNone(err2)
        self.assertIs(fn1, fn2)  # 同一段原始碼 → 同一個函式物件 (真的重用了)

    def test_syntax_error_reported_not_cached_forever(self):
        fn, err = custom_strategy._compile_on_bar("def on_bar(ctx:\n    pass\n")
        self.assertIsNone(fn)
        self.assertIn("語法錯誤", err)

    def test_run_on_bar_still_works_with_cache(self):
        src = "def on_bar(ctx):\n    return ctx.buy() if ctx.position == 'FLAT' else None\n"
        idx = pd.date_range('2024-01-01', periods=5, freq='D')
        df = pd.DataFrame({'Open': [1] * 5, 'High': [1] * 5, 'Low': [1] * 5, 'Close': [1] * 5,
                           'Volume': [1] * 5}, index=idx)
        d1 = custom_strategy.run_on_bar(src, df, 'FLAT')
        d2 = custom_strategy.run_on_bar(src, df, 'FLAT')
        self.assertEqual(d1, custom_strategy.Ctx.BUY)
        self.assertEqual(d2, custom_strategy.Ctx.BUY)


class TestRandomSearch(unittest.TestCase):
    """【ADR-056】隨機搜索:只給範圍,系統自己抽樣,不必窮舉列出候選值。"""

    SRC = ("def on_bar(ctx):\n"
           "    fast = ctx.sma(ctx.param('fast', 5))\n"
           "    slow = ctx.sma(ctx.param('slow', 20))\n"
           "    if ctx.position == 'FLAT' and ctx.cross_up(fast, slow):\n"
           "        return ctx.buy()\n"
           "    if ctx.position == 'LONG' and ctx.cross_down(fast, slow):\n"
           "        return ctx.close_position()\n"
           "    return None\n")

    def _df(self, n=300):
        idx = pd.date_range('2023-01-01', periods=n, freq='D')
        c = 100 + np.cumsum(np.random.RandomState(9).randn(n) * 2)
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c, 'Volume': 1000}, index=idx)

    def _strategy(self):
        return {'kind': 'custom', 'trade_type': '期貨', 'market': '台期貨', 'symbol': 'TXF', 'qty': 1,
                'direction': '做多', 'timeframe': '日K', 'source_code': self.SRC, 'entry': [], 'exit_signals': [],
                'custom_params': {}}

    def test_parse_param_ranges_min_max_only(self):
        r = optimizer.parse_param_ranges("fast=3:50; slow=10:200")
        self.assertEqual(r['fast'], (3.0, 50.0, True))
        self.assertEqual(r['slow'], (10.0, 200.0, True))

    def test_parse_param_ranges_rejects_grid_syntax(self):
        with self.assertRaises(ValueError):
            optimizer.parse_param_ranges("fast=5,7,10")  # 逗號列舉不是隨機搜索語法
        with self.assertRaises(ValueError):
            optimizer.parse_param_ranges("fast=5:15:2")  # 三段 (含step) 也不是

    def test_random_search_runs_within_bounds(self):
        ranges = {'fast': (3, 20, True), 'slow': (21, 60, True)}
        res = optimizer.random_search(self._strategy(), self._df(), ranges, n_trials=15,
                                      objective='淨損益', min_trades=1, seed=42)
        self.assertEqual(res['evaluated'], 15)
        for r in res['results']:
            self.assertTrue(3 <= r['params']['fast'] <= 20)
            self.assertTrue(21 <= r['params']['slow'] <= 60)

    def test_random_search_respects_trial_cap(self):
        ranges = {'fast': (3, 20, True)}
        with self.assertRaises(ValueError):
            optimizer.random_search(self._strategy(), self._df(), ranges, n_trials=301)

    def test_random_search_same_return_shape_as_grid(self):
        # GUI 共用同一套顯示邏輯,兩個模式回傳的 key 集合必須一致。
        ranges = {'fast': (3, 10, True), 'slow': (15, 30, True)}
        res_r = optimizer.random_search(self._strategy(), self._df(), ranges, n_trials=5, min_trades=1, seed=1)
        res_g = optimizer.optimize(self._strategy(), self._df(), {'fast': [5], 'slow': [20]}, min_trades=1)
        self.assertEqual(set(res_r.keys()), set(res_g.keys()))


class TestIndicatorSettingsPersistence(unittest.TestCase):
    """【ADR-056】主/副圖指標參數持久化 (config_store)。"""

    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = config_store.load_indicator_settings(os.path.join(tmp, 'nope.json'))
            self.assertEqual(d['bb_period'], 20)
            self.assertEqual(d['var_macd'], True)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ind.json')
            d = config_store.load_indicator_settings(path)
            d['bb_period'] = 33
            d['rsi_p'] = '21'
            d['ma_colors'] = ['紅 (#FF1744)'] * 6
            config_store.save_indicator_settings(path, d)
            back = config_store.load_indicator_settings(path)
            self.assertEqual(back['bb_period'], 33)
            self.assertEqual(back['rsi_p'], '21')
            self.assertEqual(back['ma_colors'], ['紅 (#FF1744)'] * 6)

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bad.json')
            with open(path, 'w') as f:
                f.write("{not valid json")
            d = config_store.load_indicator_settings(path)
            self.assertEqual(d['bb_period'], 20)  # 沒有因為壞檔就整個爆掉


class TestTaifexExtendExplicitContract(unittest.TestCase):
    """【ADR-056】extend_shioaji_df 透過 core 層直接驗證合約判斷邏輯不依賴主圖狀態
    (stock_app_pro._extend_with_taifex 的 contract 參數修正,由 GUI 冒煙測試涵蓋,
    這裡驗證其依賴的純邏輯本身正確)。"""

    def test_only_r1_extends(self):
        hist = pd.DataFrame({'Open': [1.0], 'High': [1.0], 'Low': [1.0], 'Close': [1.0], 'Volume': [1.0]},
                            index=[pd.Timestamp('2020-01-02')])
        sj = pd.DataFrame({'Open': [2.0], 'High': [2.0], 'Low': [2.0], 'Close': [2.0], 'Volume': [2.0]},
                          index=[pd.Timestamp('2024-01-02')])
        out_r1 = taifex_daily.extend_shioaji_df(sj, hist, "日K")
        self.assertEqual(len(out_r1), 2)


class TestNewConditionsADR057(unittest.TestCase):
    """【ADR-057】新增的內建條件 (使用者需求 #3/#4)。"""

    def _df_from_closes(self, closes, volumes=None, opens=None):
        n = len(closes)
        idx = pd.date_range('2024-01-01', periods=n, freq='D')
        c = np.array(closes, dtype=float)
        o = np.array(opens, dtype=float) if opens is not None else c
        return pd.DataFrame({'Open': o, 'High': np.maximum(c, o) + 0.5,
                             'Low': np.minimum(c, o) - 0.5, 'Close': c,
                             'Volume': np.array(volumes, dtype=float) if volumes is not None else np.full(n, 1000.0)},
                            index=idx)

    def test_price_cross_up_ma(self):
        # 前 10 根壓在均線下,最後一根拉高越過 5 日均線
        closes = [10] * 10 + [9, 9, 9, 9, 9, 20]
        df = self._df_from_closes(closes)
        fn = strategy_engine.CONDITIONS['price_cross_up_ma'][2]
        self.assertTrue(fn(df, {'n': 5, 'kind': 'SMA'}))
        # 沒有穿越的情況
        df2 = self._df_from_closes([10] * 16)
        self.assertFalse(fn(df2, {'n': 5, 'kind': 'SMA'}))

    def test_price_cross_down_ma(self):
        closes = [10] * 10 + [11, 11, 11, 11, 11, 2]
        df = self._df_from_closes(closes)
        fn = strategy_engine.CONDITIONS['price_cross_down_ma'][2]
        self.assertTrue(fn(df, {'n': 5, 'kind': 'SMA'}))

    def test_ma_kind_ema_actually_differs(self):
        # EMA 與 SMA 必須真的走不同計算路徑 (否則「型態可選」等於假的)
        closes = list(np.linspace(10, 30, 40))
        df = self._df_from_closes(closes)
        sma = strategy_engine._ma_of(df['Close'], 10, 'SMA')
        ema = strategy_engine._ma_of(df['Close'], 10, 'EMA')
        self.assertNotAlmostEqual(float(sma.iloc[-1]), float(ema.iloc[-1]), places=6)
        # 認不得的型態要安全退回 SMA,不能拋例外
        fallback = strategy_engine._ma_of(df['Close'], 10, 'WMA?')
        self.assertAlmostEqual(float(fallback.iloc[-1]), float(sma.iloc[-1]), places=9)

    def test_price_above_below_ma_is_state_not_cross(self):
        # 狀態型條件:持續站上均線時每根都成立 (與交叉型不同)
        df = self._df_from_closes(list(range(10, 40)))
        above = strategy_engine.CONDITIONS['price_above_ma'][2]
        cross = strategy_engine.CONDITIONS['price_cross_up_ma'][2]
        self.assertTrue(above(df, {'n': 5, 'kind': 'SMA'}))
        self.assertFalse(cross(df, {'n': 5, 'kind': 'SMA'}))  # 一路向上,最後一根沒有「剛穿越」

    def test_ma_slope(self):
        up = self._df_from_closes(list(range(10, 50)))
        down = self._df_from_closes(list(range(50, 10, -1)))
        f_up = strategy_engine.CONDITIONS['ma_slope_up'][2]
        f_dn = strategy_engine.CONDITIONS['ma_slope_down'][2]
        self.assertTrue(f_up(up, {'n': 10, 'look': 3, 'kind': 'SMA'}))
        self.assertTrue(f_dn(down, {'n': 10, 'look': 3, 'kind': 'SMA'}))

    def test_ma_alignment(self):
        bull = self._df_from_closes(list(range(10, 120)))
        f_bull = strategy_engine.CONDITIONS['ma_align_bull'][2]
        f_bear = strategy_engine.CONDITIONS['ma_align_bear'][2]
        self.assertTrue(f_bull(bull, {'short': 5, 'mid': 20, 'long': 60, 'kind': 'SMA'}))
        self.assertFalse(f_bear(bull, {'short': 5, 'mid': 20, 'long': 60, 'kind': 'SMA'}))

    def test_volume_conditions(self):
        vols = [1000] * 25 + [5000]
        df = self._df_from_closes([10] * 26, volumes=vols)
        f_up = strategy_engine.CONDITIONS['volume_above_ma'][2]
        self.assertTrue(f_up(df, {'n': 20, 'mult': 1.5}))
        vols2 = [1000] * 25 + [100]
        df2 = self._df_from_closes([10] * 26, volumes=vols2)
        f_dn = strategy_engine.CONDITIONS['volume_below_ma'][2]
        self.assertTrue(f_dn(df2, {'n': 20, 'mult': 0.7}))

    def test_consecutive_up_down(self):
        opens = [10, 10, 10, 10, 10]
        closes = [11, 11, 11, 11, 11]      # 全部收紅
        df = self._df_from_closes(closes, opens=opens)
        f_up = strategy_engine.CONDITIONS['consecutive_up'][2]
        self.assertTrue(f_up(df, {'n': 3}))
        df2 = self._df_from_closes([9] * 5, opens=[10] * 5)
        f_dn = strategy_engine.CONDITIONS['consecutive_down'][2]
        self.assertTrue(f_dn(df2, {'n': 3}))

    def test_pct_change_conditions(self):
        df = self._df_from_closes([100, 110])
        f_up = strategy_engine.CONDITIONS['pct_change_above'][2]
        self.assertTrue(f_up(df, {'value': 5.0}))
        self.assertFalse(f_up(df, {'value': 15.0}))
        df2 = self._df_from_closes([100, 90])
        f_dn = strategy_engine.CONDITIONS['pct_change_below'][2]
        self.assertTrue(f_dn(df2, {'value': -5.0}))

    def test_inside_bar_and_gaps(self):
        idx = pd.date_range('2024-01-01', periods=2, freq='D')
        df = pd.DataFrame({'Open': [10, 10.5], 'High': [12, 11], 'Low': [8, 9],
                           'Close': [11, 10.5], 'Volume': [1, 1]}, index=idx)
        self.assertTrue(strategy_engine.CONDITIONS['inside_bar'][2](df, {}))
        gap = pd.DataFrame({'Open': [10, 13], 'High': [12, 14], 'Low': [8, 12.5],
                            'Close': [11, 13.5], 'Volume': [1, 1]}, index=idx)
        self.assertTrue(strategy_engine.CONDITIONS['gap_up'][2](gap, {'value': 0.0}))

    def test_every_condition_is_callable_and_safe_on_short_df(self):
        """所有條件遇到「資料太短」都必須回傳 False 而不是拋例外 —— 回測初期
        K棒不足時會大量走到這條路徑,任何一個拋例外都會讓整個回測中斷。"""
        tiny = self._df_from_closes([10, 11])
        for key, (name, spec, fn) in strategy_engine.CONDITIONS.items():
            params = {k: dv for k, _lab, dv, _ch in (strategy_engine.spec_parts(x) for x in spec)}
            try:
                r = fn(tiny, params)
            except Exception as e:
                self.fail(f"條件 {key} 在短資料上拋例外: {type(e).__name__}: {e}")
            self.assertIn(r, (True, False), f"條件 {key} 回傳非布林值: {r!r}")

    def test_spec_parts_backward_compatible(self):
        self.assertEqual(strategy_engine.spec_parts(('n', '期間', 20)), ('n', '期間', 20, None))
        self.assertEqual(strategy_engine.spec_parts(('k', '型態', 'SMA', ['SMA', 'EMA'])),
                         ('k', '型態', 'SMA', ['SMA', 'EMA']))

    def test_condition_label_handles_all_specs(self):
        for key in strategy_engine.CONDITIONS:
            label = strategy_engine.condition_label({'type': key, 'params': {}})
            self.assertIsInstance(label, str)
            self.assertTrue(label)
        # 無參數條件不要印出空括號
        self.assertNotIn("()", strategy_engine.condition_label({'type': 'inside_bar', 'params': {}}))


class TestBacktestAuditADR057(unittest.TestCase):
    """【ADR-057】回測報告自我驗算 (使用者需求 #5)。"""

    def _result(self, trades, metrics_override=None):
        m = {'total_pnl': sum(t['pnl'] for t in trades), 'trades': len(trades),
             'wins': sum(1 for t in trades if t['pnl'] > 0),
             'losses': sum(1 for t in trades if t['pnl'] < 0),
             'win_rate': (sum(1 for t in trades if t['pnl'] > 0) / len(trades) * 100.0) if trades else 0.0,
             'profit_factor': 0.0, 'max_drawdown': 0.0, 'max_consec_loss_amount': 0.0}
        gp = sum(t['pnl'] for t in trades if t['pnl'] > 0)
        gl = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
        m['profit_factor'] = (gp / gl) if gl > 0 else (float('inf') if gp > 0 else 0.0)
        run = 0.0; worst = 0.0
        for t in trades:
            run = run + t['pnl'] if t['pnl'] < 0 else 0.0
            worst = min(worst, run)
        m['max_consec_loss_amount'] = worst
        m['max_drawdown'] = abs(worst)
        if metrics_override:
            m.update(metrics_override)
        return {'trades': trades, 'metrics': m}

    def _t(self, pnl, direction='做多', ep=100.0, xp=None, bars=5):
        if xp is None:
            xp = ep + (1 if pnl > 0 else -1) * abs(pnl) / 10.0 * (1 if direction == '做多' else -1)
        return {'pnl': pnl, 'direction': direction, 'entry_price': ep, 'exit_price': xp,
                'qty': 1, 'bars_held': bars, 'entry_ts': pd.Timestamp('2024-01-01'),
                'exit_ts': pd.Timestamp('2024-01-10')}

    def test_consistent_report_passes_all(self):
        trades = [self._t(100), self._t(-50), self._t(200), self._t(-30)]
        checks = backtest.audit_result(self._result(trades))
        failed = [c for c in checks if not c['ok']]
        self.assertEqual(failed, [], f"一致的報告不該有失敗項: {failed}")

    def test_all_winning_report_passes(self):
        """全勝的報告不該被誤判 —— 第一版 audit 用 min(所有損益) 當「最大單筆
        虧損」,全勝時那是最小獲利 (正數),會讓正確的報告被判定不一致。"""
        trades = [self._t(100), self._t(50), self._t(200)]
        checks = backtest.audit_result(self._result(trades))
        failed = [c['name'] for c in checks if not c['ok']]
        self.assertEqual(failed, [], f"全勝報告不該有失敗項: {failed}")

    def test_all_losing_report_passes(self):
        trades = [self._t(-100), self._t(-50)]
        checks = backtest.audit_result(self._result(trades))
        failed = [c['name'] for c in checks if not c['ok']]
        self.assertEqual(failed, [], f"全敗報告不該有失敗項: {failed}")

    def test_favorable_move_with_loss_is_not_flagged(self):
        """有利價格方向卻小虧 (被成本吃掉) 是合理的,不可誤報。
        損益是金額、價差是點數,單位不同不能直接比大小 —— 第一版就是這樣
        寫才會對真實期貨回測誤報 (契約乘數 200)。"""
        t = self._t(-40.0, direction='做多', ep=100.0, xp=100.2)   # 價格漲,但淨虧
        checks = backtest.audit_result(self._result([t]))
        dir_check = [c for c in checks if '方向' in c['name']][0]
        self.assertTrue(dir_check['ok'], dir_check['detail'])

    def test_adverse_move_with_profit_is_flagged(self):
        """不利方向卻獲利 = 明細真的矛盾 (成本只會讓結果更差),必須抓到。"""
        t = self._t(50.0, direction='做多', ep=100.0, xp=99.0)
        checks = backtest.audit_result(self._result([t]))
        dir_check = [c for c in checks if '方向' in c['name']][0]
        self.assertFalse(dir_check['ok'])

    def test_tampered_total_pnl_is_caught(self):
        trades = [self._t(100), self._t(-50)]
        res = self._result(trades, {'total_pnl': 99999.0})
        checks = backtest.audit_result(res)
        bad = [c for c in checks if not c['ok']]
        self.assertTrue(any('淨損益' in c['name'] for c in bad))

    def test_tampered_win_rate_is_caught(self):
        trades = [self._t(100), self._t(-50)]
        res = self._result(trades, {'win_rate': 100.0})
        checks = backtest.audit_result(res)
        self.assertTrue(any(not c['ok'] and '勝率' in c['name'] for c in checks))

    def test_empty_trades_reports_cannot_audit(self):
        checks = backtest.audit_result({'trades': [], 'metrics': {}})
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]['ok'])

    def test_drawdown_smaller_than_worst_trade_is_caught(self):
        trades = [self._t(-500)]
        res = self._result(trades, {'max_drawdown': 1.0})
        checks = backtest.audit_result(res)
        self.assertTrue(any(not c['ok'] and '最大回撤' in c['name'] for c in checks))

    def test_audit_on_real_backtest_passes(self):
        """對真正跑出來的回測結果驗算,必須全數通過 —— 這同時也是
        _compute_metrics 與 audit_result 兩條獨立路徑的交叉驗證。"""
        SRC = ("def on_bar(ctx):\n"
               "    f = ctx.sma(5)\n"
               "    s = ctx.sma(20)\n"
               "    if ctx.position == 'FLAT' and ctx.cross_up(f, s):\n"
               "        return ctx.buy()\n"
               "    if ctx.position == 'LONG' and ctx.cross_down(f, s):\n"
               "        return ctx.close_position()\n"
               "    return None\n")
        n = 400
        idx = pd.date_range('2022-01-01', periods=n, freq='D')
        c = 100 + np.cumsum(np.random.RandomState(11).randn(n) * 1.5)
        df = pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c, 'Volume': 1000}, index=idx)
        strat = {'kind': 'custom', 'trade_type': '期貨', 'market': '台期貨', 'symbol': 'TXF',
                 'qty': 1, 'direction': '做多', 'timeframe': '日K', 'source_code': SRC,
                 'entry': [], 'exit_signals': [], 'custom_params': {}}
        r = backtest.run_backtest(strat, df)
        self.assertGreater(len(r['trades']), 0)
        checks = backtest.audit_result(r, strat)
        failed = [(c['name'], c['detail']) for c in checks if not c['ok']]
        self.assertEqual(failed, [], f"真實回測結果驗算失敗: {failed}")


class TestConsecAmountsADR057(unittest.TestCase):
    """【ADR-057】最大連續獲利/虧損「金額」(使用者需求 #5)。"""

    def _run(self, pnls):
        trades = [{'pnl': p, 'direction': '做多', 'entry_price': 100.0, 'exit_price': 101.0,
                   'qty': 1, 'bars_held': 1, 'entry_ts': pd.Timestamp('2024-01-01'),
                   'exit_ts': pd.Timestamp('2024-01-02')} for p in pnls]
        idx = pd.date_range('2024-01-01', periods=50, freq='D')
        df = pd.DataFrame({'Open': 100.0, 'High': 101.0, 'Low': 99.0, 'Close': 100.0,
                           'Volume': 1.0}, index=idx)
        eq = []
        run = 0.0
        for i, p in enumerate(pnls):
            run += p; eq.append((idx[i], run))
        return backtest._compute_metrics(trades, eq, df, 1, 1.0, 0.0, 0.0, sum(pnls), "test")

    def test_consecutive_amounts(self):
        # 連賺 100+200 = 300;連賠 -50-80-20 = -150
        m = self._run([100, 200, -50, -80, -20, 500])
        self.assertAlmostEqual(m['max_consec_win_amount'], 500.0)   # 單筆 500 大於 300
        self.assertAlmostEqual(m['max_consec_loss_amount'], -150.0)
        self.assertEqual(m['max_consec_wins'], 2)
        self.assertEqual(m['max_consec_losses'], 3)

    def test_all_wins_has_zero_loss_amount(self):
        m = self._run([10, 20, 30])
        self.assertAlmostEqual(m['max_consec_win_amount'], 60.0)
        self.assertAlmostEqual(m['max_consec_loss_amount'], 0.0)


class TestBacktestCancelADR057(unittest.TestCase):
    """【ADR-057】回測可強制中止 (使用者需求 #9)。"""

    def _setup(self, n=600):
        idx = pd.date_range('2022-01-01', periods=n, freq='D')
        c = 100 + np.cumsum(np.random.RandomState(5).randn(n))
        df = pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c, 'Volume': 1000}, index=idx)
        SRC = ("def on_bar(ctx):\n"
               "    if ctx.position == 'FLAT' and ctx.cross_up(ctx.sma(5), ctx.sma(20)):\n"
               "        return ctx.buy()\n"
               "    if ctx.position == 'LONG' and ctx.cross_down(ctx.sma(5), ctx.sma(20)):\n"
               "        return ctx.close_position()\n"
               "    return None\n")
        s = {'kind': 'custom', 'trade_type': '期貨', 'market': '台期貨', 'symbol': 'TXF', 'qty': 1,
             'direction': '做多', 'timeframe': '日K', 'source_code': SRC, 'entry': [],
             'exit_signals': [], 'custom_params': {}}
        return s, df

    def test_stops_early_and_returns_partial(self):
        s, df = self._setup()
        full = backtest.run_backtest(s, df)
        stopped = backtest.run_backtest(s, df, should_stop=lambda: True)
        # 中止不該拋例外,且結果應該比完整版少 (或至多相等)
        self.assertLessEqual(len(stopped['trades']), len(full['trades']))
        self.assertIn('metrics', stopped)

    def test_no_stop_callback_behaves_as_before(self):
        s, df = self._setup()
        a = backtest.run_backtest(s, df)
        b = backtest.run_backtest(s, df, should_stop=lambda: False)
        self.assertEqual(len(a['trades']), len(b['trades']))
        self.assertAlmostEqual(a['metrics']['total_pnl'], b['metrics']['total_pnl'])

    def test_stop_callback_exception_does_not_break_backtest(self):
        s, df = self._setup()
        def boom():
            raise RuntimeError("callback 壞了")
        r = backtest.run_backtest(s, df, should_stop=boom)
        self.assertIn('metrics', r)   # 回呼壞掉不該影響回測本身


class TestBnhModesADR062(unittest.TestCase):
    """【ADR-062】買進持有三模式:單筆長抱 / 累積加碼 / 定期定額。"""

    def _df(self, n=750, seed=11):
        idx = pd.date_range('2022-01-03', periods=n, freq='B')
        c = 100 + np.cumsum(np.random.RandomState(seed).randn(n) * 0.9)
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c,
                             'Volume': 1000.0}, index=idx)

    def _s(self, mode, **extra):
        d = {'kind': 'builtin', 'trade_type': '零股', 'market': '台股', 'symbol': '0050',
             'name': 'X', 'qty': 1, 'direction': '做多', 'timeframe': '日K',
             'buy_and_hold': True, 'bnh_mode': mode,
             'entry': [{'type': 'always_true', 'params': {}}], 'exit_signals': [],
             'stop_loss_pct': 0, 'take_profit_pct': 0, 'stop_loss_abs': 0, 'take_profit_abs': 0}
        d.update(extra)
        return d

    def test_unit_size_shared(self):
        self.assertEqual(strategy_engine.unit_size({'trade_type': '股票'}), 1000.0)
        self.assertEqual(strategy_engine.unit_size({'trade_type': '零股'}), 1.0)
        fut = strategy_engine.unit_size({'trade_type': '期貨', 'symbol': 'TXFR1'})
        self.assertGreater(fut, 1.0)

    def test_single_buys_exactly_once(self):
        r = backtest.run_backtest(self._s('single'), self._df())
        self.assertEqual(r['metrics']['bnh_buys'], 1)
        self.assertEqual(r['metrics']['trades'], 1)

    def test_accumulate_buys_many(self):
        r = backtest.run_backtest(self._s('accumulate'), self._df())
        self.assertGreater(r['metrics']['bnh_buys'], 500)

    def test_dca_buys_once_per_month(self):
        n = 750   # 約 3 年 → 約 36 個月
        r = backtest.run_backtest(self._s('dca', dca_amount=10000.0, dca_interval='month'),
                                  self._df(n))
        m = r['metrics']
        self.assertGreaterEqual(m['bnh_buys'], 30)
        self.assertLessEqual(m['bnh_buys'], 40, "每月一次,不該買到幾百次")

    def test_dca_weekly_more_than_monthly(self):
        df = self._df(500)
        wk = backtest.run_backtest(self._s('dca', dca_amount=10000.0, dca_interval='week'), df)
        mo = backtest.run_backtest(self._s('dca', dca_amount=10000.0, dca_interval='month'), df)
        self.assertGreater(wk['metrics']['bnh_buys'], mo['metrics']['bnh_buys'])

    def test_dca_invests_close_to_planned(self):
        """定期定額:實際投入應接近「期數 × 每期金額」(差額是買不滿一單位的餘額)。

        【ADR-064】股數是用「決策當根」收盤價 (close) 換算的,但實際成交價是
        「下一根」開盤價 (open, T+1) —— 兩者之間的隔夜跳空會讓單期成本跟預算
        有小幅落差 (可能略高於預算,不再是嚴格 <=)。這是成交時機設計本身的
        必然結果,不是餘額 (carry-over) 機制的 bug;carry-over 本身的精確保證
        由 test_dca_carry_over_when_too_expensive (固定價格、無跳空) 驗證。
        這裡放寬成相對容忍度,只用來抓「明顯超支」的真正錯誤。
        """
        m = backtest.run_backtest(
            self._s('dca', dca_amount=10000.0, dca_interval='month'), self._df(750))['metrics']
        planned = m['bnh_buys'] * 10000.0
        self.assertLessEqual(m['bnh_total_invested'], planned * 1.01)
        self.assertGreater(m['bnh_total_invested'], planned * 0.9)

    def test_dca_quantity_varies_with_price(self):
        """定期定額的重點:價格低時買得多。逐筆檢查 qty×price 大致等於每期預算。"""
        r = backtest.run_backtest(
            self._s('dca', dca_amount=10000.0, dca_interval='month'), self._df(750))
        qtys = {t['qty'] for t in r['trades']}
        self.assertGreater(len(qtys), 1, "數量應隨價格變動,不該每期都一樣")

    def test_dca_carry_over_when_too_expensive(self):
        """每期金額買不起一單位時,錢要留到下期,不可以憑空消失或硬買。"""
        n = 260
        idx = pd.date_range('2023-01-02', periods=n, freq='B')
        c = np.full(n, 500.0)          # 股票 1 張 = 1000 股 → 一張 500,000
        df = pd.DataFrame({'Open': c, 'High': c, 'Low': c, 'Close': c, 'Volume': 1000.0}, index=idx)
        s = self._s('dca', dca_amount=100000.0, dca_interval='month')
        s['trade_type'] = '股票'        # unit_size = 1000
        r = backtest.run_backtest(s, df)
        m = r['metrics']
        # 每月 10 萬、一張 50 萬 → 約每 5 個月才買得起一張
        self.assertGreaterEqual(m['bnh_buys'], 1)
        self.assertLessEqual(m['bnh_buys'], 3)
        for t in r['trades']:
            self.assertGreaterEqual(t['qty'], 1)

    def test_validate_dca_requires_amount(self):
        s = self._s('dca', dca_amount=0)
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn("金額", msg)

    def test_validate_rejects_unknown_mode(self):
        s = self._s('weird')
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)

    def test_all_modes_audit_clean(self):
        df = self._df(500)
        for s in (self._s('single'), self._s('accumulate'),
                  self._s('dca', dca_amount=10000.0, dca_interval='month')):
            r = backtest.run_backtest(s, df)
            failed = [c['name'] for c in backtest.audit_result(r) if not c['ok']]
            self.assertEqual(failed, [], f"{s['bnh_mode']} 驗算失敗: {failed}")

    def test_modes_are_comparable_same_period(self):
        """三種模式在同一段資料上都要能跑出結果 (策略比較的前提)。"""
        df = self._df(500)
        out = {}
        for mode, extra in (('single', {}), ('accumulate', {}),
                            ('dca', {'dca_amount': 10000.0, 'dca_interval': 'month'})):
            m = backtest.run_backtest(self._s(mode, **extra), df)['metrics']
            out[mode] = m
            self.assertGreater(m['bnh_buys'], 0)
            self.assertNotEqual(m['total_pnl'], 0.0)
        self.assertLess(out['single']['bnh_total_invested'], out['accumulate']['bnh_total_invested'])


class TestTaifexOnlyPathADR060(unittest.TestCase):
    """【ADR-060】完全不靠券商、只用期交所資料的路徑必須產得出K線。"""

    def _hist(self, n=800):
        idx = pd.date_range('2015-01-01', periods=n, freq='B')
        c = 9000 + np.cumsum(np.random.RandomState(2).randn(n) * 30)
        return pd.DataFrame({'Open': c, 'High': c + 20, 'Low': c - 20, 'Close': c,
                             'Volume': 1000.0}, index=idx)

    def test_empty_shioaji_returns_taifex_data(self):
        """ADR-058 讓『期交所已完整涵蓋』時跳過券商下載,shioaji 端因此合法為空。
        舊版看到空就原樣回傳空表 → 圖表與回測都變成『取不到資料』。"""
        hist = self._hist()
        empty = pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
        for tf, expect_min in (("日K", 700), ("周K", 100), ("月K", 25)):
            out = taifex_daily.extend_shioaji_df(empty, hist, tf)
            self.assertGreaterEqual(len(out), expect_min, f"{tf} 應由期交所資料獨力產出")
            for col in ('Open', 'High', 'Low', 'Close', 'Volume'):
                self.assertIn(col, out.columns)

    def test_minute_tf_still_not_applicable(self):
        out = taifex_daily.extend_shioaji_df(
            pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume']), self._hist(), "5分K")
        self.assertTrue(out.empty)   # 官方日行情無法產生分K,不可假裝有

    def test_empty_taifex_returns_shioaji_unchanged(self):
        sj = self._hist(100)
        out = taifex_daily.extend_shioaji_df(sj, pd.DataFrame(), "日K")
        pd.testing.assert_frame_equal(out, sj)

    def test_normal_prepend_still_works(self):
        hist = self._hist()
        sj = hist.iloc[-50:].copy() * 1.0
        out = taifex_daily.extend_shioaji_df(sj, hist, "日K")
        self.assertEqual(len(out), len(hist))
        self.assertEqual(out.index[0], hist.index[0])

    def test_store_path_is_absolute_when_base_is_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = taifex_store.store_path(tmp, 'TX', session='all')
            self.assertTrue(os.path.isabs(p), p)
            self.assertTrue(p.endswith(os.path.join('taifex_daily', 'TX.csv')))


class TestBuyAndHoldADR059(unittest.TestCase):
    """【ADR-059】買進後持有不賣、期末結算、持有成本、報酬率分母修正。"""

    def _df(self, n=500, seed=4):
        idx = pd.date_range('2020-01-02', periods=n, freq='B')
        c = 100 + np.cumsum(np.random.RandomState(seed).randn(n) * 1.2)
        return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c,
                             'Volume': 1000.0}, index=idx)

    def _bnh_strategy(self, market='台股', tt='股票', symbol='2330'):
        return {'kind': 'builtin', 'trade_type': tt, 'market': market, 'symbol': symbol,
                'qty': 1, 'direction': '做多', 'timeframe': '日K', 'buy_and_hold': True,
                'entry': [{'type': 'always_true', 'params': {}}], 'exit_signals': [],
                'stop_loss_pct': 0, 'take_profit_pct': 0,
                'stop_loss_abs': 0, 'take_profit_abs': 0}

    # ---- 驗證規則 ----
    def test_validate_allows_no_exit_when_buy_and_hold(self):
        s = self._bnh_strategy()
        s['name'] = 'BH'
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertTrue(ok, msg)

    def test_validate_rejects_no_exit_without_flag(self):
        s = self._bnh_strategy(); s['name'] = 'X'; s['buy_and_hold'] = False
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn("買進後持有不賣", msg)   # 錯誤訊息要指路

    def test_buy_and_hold_cannot_have_stops(self):
        s = self._bnh_strategy(); s['name'] = 'X'; s['stop_loss_pct'] = 2.0
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn("矛盾", msg)

    def test_buy_and_hold_cannot_be_live(self):
        s = self._bnh_strategy(); s['name'] = 'X'; s['mode'] = '實單'
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn("實單", msg)

    def test_always_true_condition(self):
        fn = strategy_engine.CONDITIONS['always_true'][2]
        self.assertTrue(fn(self._df(10), {}))

    # ---- 回測行為 ----
    def test_always_true_accumulates_every_bar(self):
        """【ADR-061】語意更正:條件成立就「再買一次」,不是只買一次。
        always_true = 每根K棒都成立 → 每根都買 (定期定額)。"""
        n = 500
        r = backtest.run_backtest(self._bnh_strategy(), self._df(n))
        m = r['metrics']
        self.assertGreater(m['bnh_buys'], 400, "應該每根都買,不是只買一次")
        self.assertEqual(m['trades'], m['bnh_buys'], "每次買進 = 明細一列")
        self.assertEqual(m['settled_open_at_end'], m['bnh_buys'])
        self.assertTrue(m['buy_and_hold_mode'])
        self.assertIn('期末結算', r['trades'][0]['exit_reason'])

    def test_conditional_accumulation_buys_multiple_times(self):
        """逢低承接:收盤跌破均線才買 → 買進次數應多於 1 但少於總根數。"""
        s = self._bnh_strategy()
        s['entry'] = [{'type': 'price_below_ma', 'params': {'n': 20, 'kind': 'SMA'}}]
        n = 400
        r = backtest.run_backtest(s, self._df(n, seed=6))
        m = r['metrics']
        self.assertGreater(m['bnh_buys'], 1, "條件多次成立就該多次買進")
        self.assertLess(m['bnh_buys'], n, "不該每根都買 (條件不是永遠成立)")

    def test_never_sells(self):
        """永不賣出:所有明細的出場原因都必須是期末結算,不能有停損/訊號出場。"""
        s = self._bnh_strategy()
        s['entry'] = [{'type': 'price_below_ma', 'params': {'n': 20, 'kind': 'SMA'}}]
        r = backtest.run_backtest(s, self._df(400, seed=6))
        for t in r['trades']:
            self.assertIn('期末結算', t['exit_reason'])

    def test_accumulation_totals_reconcile(self):
        """總持有成本 / 平均成本 / 期末市值 必須與逐筆明細對得起來。"""
        s = self._bnh_strategy()
        s['entry'] = [{'type': 'price_below_ma', 'params': {'n': 20, 'kind': 'SMA'}}]
        r = backtest.run_backtest(s, self._df(400, seed=6))
        m = r['metrics']
        lots = r['trades']
        self.assertEqual(m['bnh_buys'], len(lots))
        self.assertEqual(m['bnh_total_qty'], sum(t['qty'] for t in lots))
        invested = sum(abs(t['entry_price']) * t['qty'] * 1000.0 for t in lots)
        self.assertAlmostEqual(m['bnh_total_invested'], invested, places=4)
        self.assertAlmostEqual(m['bnh_avg_cost'],
                               invested / (m['bnh_total_qty'] * 1000.0), places=6)
        self.assertAlmostEqual(m['bnh_final_value'],
                               m['bnh_final_price'] * m['bnh_total_qty'] * 1000.0, places=4)
        # 淨損益 = (期末價 - 平均成本) × 總量 × 單位規模 - 總成本
        manual = ((m['bnh_final_price'] - m['bnh_avg_cost']) * m['bnh_total_qty'] * 1000.0
                  - m['total_cost'])
        self.assertAlmostEqual(manual, m['total_pnl'], places=2)

    def test_weighted_average_cost_in_engine(self):
        """引擎的 apply_fill 在累積模式下要算加權平均成本,不是覆蓋。"""
        s = self._bnh_strategy()
        rt = strategy_engine.new_runtime()
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 1, 'price': 100.0}, 1.0)
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 3, 'price': 200.0}, 2.0)
        self.assertEqual(rt['qty'], 4)
        self.assertAlmostEqual(rt['entry_price'], (100.0 * 1 + 200.0 * 3) / 4)

    def test_normal_strategy_still_overwrites_not_accumulates(self):
        """非買進持有的一般策略,行為完全不變 (仍是覆蓋,不累積)。"""
        s = self._bnh_strategy(); s['buy_and_hold'] = False
        rt = strategy_engine.new_runtime()
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 1, 'price': 100.0}, 1.0)
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 3, 'price': 200.0}, 2.0)
        self.assertEqual(rt['qty'], 3)
        self.assertAlmostEqual(rt['entry_price'], 200.0)

    def test_settle_can_be_disabled(self):
        r = backtest.run_backtest(self._bnh_strategy(), self._df(), settle_open_at_end=False)
        self.assertEqual(r['metrics']['trades'], 0)   # 不結算就沒有已完成交易

    def test_settlement_does_not_affect_closed_strategies(self):
        """本來就有出場的策略,期末沒有未平倉時結果不該改變。"""
        SRC = ("def on_bar(ctx):\n"
               "    if ctx.position=='FLAT' and ctx.cross_up(ctx.sma(5), ctx.sma(20)):\n"
               "        return ctx.buy()\n"
               "    if ctx.position=='LONG' and ctx.cross_down(ctx.sma(5), ctx.sma(20)):\n"
               "        return ctx.close_position()\n"
               "    return None\n")
        st = {'kind': 'custom', 'trade_type': '期貨', 'market': '台期貨', 'symbol': 'TXF',
              'qty': 1, 'direction': '做多', 'timeframe': '日K', 'source_code': SRC,
              'entry': [], 'exit_signals': [], 'custom_params': {}}
        df = self._df(400, seed=9)
        a = backtest.run_backtest(st, df, settle_open_at_end=False)
        b = backtest.run_backtest(st, df, settle_open_at_end=True)
        # 若期末剛好無持倉,兩者應完全相同;若有持倉,b 應剛好多一筆
        self.assertIn(len(b['trades']) - len(a['trades']), (0, 1))
        if len(b['trades']) == len(a['trades']):
            self.assertAlmostEqual(a['metrics']['total_pnl'], b['metrics']['total_pnl'])

    # ---- 持有成本 ----
    def test_cost_basis_includes_contract_size(self):
        r = backtest.run_backtest(self._bnh_strategy(), self._df())
        t = r['trades'][0]
        expected = abs(t['entry_price']) * 1 * 1000.0   # 股票 1 張 = 1000 股
        self.assertAlmostEqual(r['metrics']['cost_basis_first'], expected, places=4)

    def test_turnover_metrics(self):
        r = backtest.run_backtest(self._bnh_strategy(), self._df())
        m = r['metrics']
        self.assertGreater(m['years'], 1.0)
        # 【ADR-061】always_true 累積模式每根都買,每年交易次數本來就很高;
        # 這個指標的意義是「成本結構」,不是「持有時間長短」。
        self.assertGreater(m['trades_per_year'], 0.0)
        self.assertGreaterEqual(m['cost_per_year'], 0.0)

    # ---- 報酬率分母修正 (既有 bug) ----
    def test_total_return_pct_matches_single_trade(self):
        """只有一筆交易時,總報酬率必須等於那筆的報酬% —— 舊版分母漏乘
        contract_size,股票會差 1000 倍 (使用者實例:0050 顯示 -53093.43%,
        正確是 -53.09%)。用一般 (會出場) 策略驗證,才保證只有一筆。"""
        SRC = ("def on_bar(ctx):\n"
               "    if ctx.position == 'FLAT' and len(ctx.df) == 10:\n"
               "        return ctx.buy()\n"
               "    if ctx.position == 'LONG' and len(ctx.df) == 60:\n"
               "        return ctx.close_position()\n"
               "    return None\n")
        st = {'kind': 'custom', 'trade_type': '股票', 'market': '台股', 'symbol': '0050',
              'qty': 1, 'direction': '做多', 'timeframe': '日K', 'source_code': SRC,
              'entry': [], 'exit_signals': [], 'custom_params': {}}
        r = backtest.run_backtest(st, self._df(200))
        self.assertEqual(len(r['trades']), 1)
        self.assertAlmostEqual(r['metrics']['total_return_pct'],
                               r['trades'][0]['pnl_pct'], places=6)

    def test_accumulation_return_pct_uses_total_invested(self):
        """【ADR-061】累積模式的報酬率分母必須是「總投入」,不是每筆平均。
        否則 119 筆的損益總和 ÷ 1 筆的規模 = 1473% 這種荒謬數字。"""
        s = self._bnh_strategy()
        s['entry'] = [{'type': 'price_below_ma', 'params': {'n': 20, 'kind': 'SMA'}}]
        r = backtest.run_backtest(s, self._df(400, seed=6))
        m = r['metrics']
        self.assertGreater(m['bnh_buys'], 10)
        expected = m['total_pnl'] / m['bnh_total_invested'] * 100.0
        self.assertAlmostEqual(m['total_return_pct'], expected, places=6)
        self.assertLess(abs(m['total_return_pct']), 500.0)

    def test_total_return_pct_reasonable_for_futures(self):
        s = self._bnh_strategy(market='台期貨', tt='期貨', symbol='TXF')
        r = backtest.run_backtest(s, self._df())
        self.assertLess(abs(r['metrics']['total_return_pct']), 1000.0)

    def test_audit_passes_on_buy_and_hold(self):
        r = backtest.run_backtest(self._bnh_strategy(), self._df())
        failed = [c['name'] for c in backtest.audit_result(r) if not c['ok']]
        self.assertEqual(failed, [])

    def test_audit_allows_zero_bars_held(self):
        """最後一根才買進的那筆持有 0 根,是合法的,驗算不可誤報。"""
        s = self._bnh_strategy()
        r = backtest.run_backtest(s, self._df(300))
        self.assertTrue(any(t['bars_held'] == 0 for t in r['trades']))
        failed = [c['name'] for c in backtest.audit_result(r) if not c['ok']]
        self.assertEqual(failed, [])


class TestSessionBasisADR058(unittest.TestCase):
    """【ADR-058】盤別口徑 (使用者需求 #3):日盤 vs 近全、口徑偵測、涵蓋範圍。"""

    def _minute_df(self, days=3):
        """造一段含日盤(08:45-13:45)與夜盤(15:00-次日05:00)的分K。"""
        rows = []
        base = pd.Timestamp('2024-01-02')
        for d in range(days):
            day = base + pd.Timedelta(days=d)
            # 日盤 08:45~13:45,價格 100+d
            for h, m in [(8, 45), (10, 0), (13, 45)]:
                rows.append((day + pd.Timedelta(hours=h, minutes=m), 100.0 + d))
            # 夜盤 15:00~23:00 (歸屬「下一個交易日」),價格 200+d 便於辨識
            for h in (15, 23):
                rows.append((day + pd.Timedelta(hours=h), 200.0 + d))
        idx = [r[0] for r in rows]; px = [r[1] for r in rows]
        return pd.DataFrame({'Open': px, 'High': px, 'Low': px, 'Close': px,
                             'Volume': [1.0] * len(px)}, index=pd.DatetimeIndex(idx)).sort_index()

    def test_day_basis_excludes_night(self):
        df = self._minute_df()
        agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        allsess = futures_session.resample_future_session(df, "日K", agg, session_basis='all')
        dayonly = futures_session.resample_future_session(df, "日K", agg, session_basis='day')
        # 只用日盤:所有價格都應該落在 100 區間 (夜盤的 200 不該出現)
        self.assertTrue((dayonly['High'] < 150).all(), dayonly)
        # 近全:一定會吃到夜盤的 200
        self.assertTrue((allsess['High'] >= 200).any(), allsess)

    def test_day_basis_volume_is_smaller(self):
        df = self._minute_df()
        agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        a = futures_session.resample_future_session(df, "日K", agg, session_basis='all')
        d = futures_session.resample_future_session(df, "日K", agg, session_basis='day')
        self.assertLess(d['Volume'].sum(), a['Volume'].sum())

    def test_default_is_all_unchanged(self):
        """不傳 session_basis 必須與 'all' 完全相同 (既有行為不可變)。"""
        df = self._minute_df()
        agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
        a = futures_session.resample_future_session(df, "日K", agg)
        b = futures_session.resample_future_session(df, "日K", agg, session_basis='all')
        pd.testing.assert_frame_equal(a, b)

    def test_taifex_day_session_ignores_after_hours(self):
        csv = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,"
               "結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,"
               "是否因訊息面暫停交易,交易時段,價差對單式委託成交量\n"
               "2026/07/10,TX,202607,45800,46000,45700,45950,-,-,30000,-,-,-,-,-,-,,盤後,0\n"
               "2026/07/10,TX,202607,45900,46495,45701,46281,633,1.39,90000,-,-,-,-,-,-,,一般,0\n")
        rows = taifex_daily.parse_csv_text(csv)
        allsess = taifex_daily.build_front_month_daily(rows, 'TX', session='all')
        dayonly = taifex_daily.build_front_month_daily(rows, 'TX', session='day')
        # 近全:Open 來自盤後(夜盤)45800、量含兩時段 120000
        self.assertEqual(allsess.iloc[0]['Open'], 45800)
        self.assertEqual(allsess.iloc[0]['Volume'], 120000)
        # 只用日盤:Open 是一般時段 45900、量只有 90000
        self.assertEqual(dayonly.iloc[0]['Open'], 45900)
        self.assertEqual(dayonly.iloc[0]['Volume'], 90000)
        self.assertEqual(dayonly.iloc[0]['Low'], 45701)   # 不含夜盤的 45700

    def test_detect_regime_day_only_before_night_start(self):
        idx = pd.date_range('2010-01-01', periods=200, freq='B')
        c = 8000 + np.cumsum(np.random.RandomState(1).randn(200) * 30)
        df = pd.DataFrame({'Open': c, 'High': c + 10, 'Low': c - 10, 'Close': c,
                           'Volume': 1000.0}, index=idx)
        r = taifex_daily.detect_session_regime(df)
        self.assertEqual(r['regime'], 'day_only')
        self.assertIn('2017-05-15', r['note'])

    def test_detect_regime_mixed_is_flagged(self):
        """夜盤上線前跳空大、上線後跳空小 → 必須判定為 mixed 並警告。"""
        pre_idx = pd.date_range('2015-01-01', periods=150, freq='B')
        post_idx = pd.date_range('2018-01-01', periods=150, freq='B')
        rs = np.random.RandomState(7)
        pre_close = 9000 + np.cumsum(rs.randn(150) * 20)
        # 前段:開盤與前收差距大 (約 0.4%)
        pre_open = pre_close * (1 + rs.choice([-1, 1], 150) * 0.004)
        post_close = 11000 + np.cumsum(rs.randn(150) * 20)
        # 後段:開盤幾乎貼著前收 (約 0.05%)
        post_open = post_close * (1 + rs.choice([-1, 1], 150) * 0.0005)
        idx = pre_idx.append(post_idx)
        o = np.concatenate([pre_open, post_open]); c = np.concatenate([pre_close, post_close])
        df = pd.DataFrame({'Open': o, 'High': np.maximum(o, c) + 5,
                           'Low': np.minimum(o, c) - 5, 'Close': c,
                           'Volume': 1000.0}, index=idx)
        r = taifex_daily.detect_session_regime(df)
        self.assertEqual(r['regime'], 'mixed', r)
        self.assertGreater(r['ratio'], 2.5)
        self.assertIn('只用日盤', r['note'])

    def test_detect_regime_short_data_is_safe(self):
        df = pd.DataFrame({'Open': [1.0], 'High': [1.0], 'Low': [1.0], 'Close': [1.0],
                           'Volume': [1.0]}, index=[pd.Timestamp('2024-01-01')])
        r = taifex_daily.detect_session_regime(df)
        self.assertEqual(r['regime'], 'unknown')

    def test_split_coverage(self):
        idx = pd.date_range('2010-01-01', periods=500, freq='B')
        df = pd.DataFrame({'Open': 1.0, 'High': 1.0, 'Low': 1.0, 'Close': 1.0,
                           'Volume': 1.0}, index=idx)
        import datetime as _d
        last = idx[-1].to_pydatetime()
        # 完全涵蓋 → 不必下載
        cu, nf = taifex_daily.split_coverage(df, _d.datetime(2010, 6, 1), last)
        self.assertIsNone(nf)
        # 需要補尾巴
        cu, nf = taifex_daily.split_coverage(df, _d.datetime(2010, 6, 1), last + _d.timedelta(days=30))
        self.assertIsNotNone(nf)
        self.assertGreater(pd.Timestamp(nf), idx[-1])
        # 起點早於期交所資料 → 整段仍需下載
        cu, nf = taifex_daily.split_coverage(df, _d.datetime(2005, 1, 1), last)
        self.assertEqual(pd.Timestamp(nf), pd.Timestamp(_d.datetime(2005, 1, 1)))
        # 沒有資料 → 整段下載
        cu, nf = taifex_daily.split_coverage(pd.DataFrame(), _d.datetime(2020, 1, 1), last)
        self.assertIsNone(cu)

    def test_store_two_sessions_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = pd.DataFrame({'Open': [1.0], 'High': [1.0], 'Low': [1.0], 'Close': [1.0],
                              'Volume': [10.0]}, index=[pd.Timestamp('2024-01-02')])
            d = pd.DataFrame({'Open': [2.0], 'High': [2.0], 'Low': [2.0], 'Close': [2.0],
                              'Volume': [5.0]}, index=[pd.Timestamp('2024-01-02')])
            taifex_store.save_daily(tmp, 'TX', a, session='all')
            taifex_store.save_daily(tmp, 'TX', d, session='day')
            self.assertEqual(taifex_store.load_daily(tmp, 'TX', session='all').iloc[0]['Open'], 1.0)
            self.assertEqual(taifex_store.load_daily(tmp, 'TX', session='day').iloc[0]['Open'], 2.0)
            self.assertTrue(taifex_store.has_daily(tmp, 'TX', session='day'))
            self.assertFalse(taifex_store.has_daily(tmp, 'MTX', session='day'))
            # 檔名必須不同,才不會互相覆蓋
            self.assertNotEqual(taifex_store.store_path(tmp, 'TX', 'all'),
                                taifex_store.store_path(tmp, 'TX', 'day'))


class TestTaifexDaily(unittest.TestCase):
    """【ADR-049】期交所官方每日行情解析 / 前月連續日K / shioaji 銜接。"""

    CSV = (
        "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,"
        "結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,"
        "是否因訊息面暫停交易,交易時段,價差對單式委託成交量\n"
        # 7/10 交易日:盤後(夜盤,前晚15:00起) + 一般(日盤,收46281) —— ADR-007 實例數字
        "2026/07/10,TX,202607,45800,46000,45700,45950,-,-,30000,-,-,-,-,-,-,,盤後,0\n"
        "2026/07/10,TX,202607,45900,46495,45701,46281,633,1.39,90000,-,-,-,-,-,-,,一般,0\n"
        # 次月合約 (不應被選為近月)
        "2026/07/10,TX,202609,45850,46400,45750,46200,-,-,5000,-,-,-,-,-,-,,一般,0\n"
        # 週契約與價差單 (應被排除)
        "2026/07/10,TX,202607W2,45900,46490,45710,46280,-,-,800,-,-,-,-,-,-,,一般,0\n"
        "2026/07/10,TX,202607/202609,-,-,-,-,-,-,120,-,-,-,-,-,-,,一般,0\n"
        # 別的商品 (應被排除)
        "2026/07/10,MTX,202607,45800,46495,45700,46281,-,-,40000,-,-,-,-,-,-,,一般,0\n"
        # 7/13 只有一般時段 (模擬舊資料無夜盤)
        "2026/07/13,TX,202607,46300,46500,46250,46450,-,-,80000,-,-,-,-,-,-,,一般,0\n"
    )

    def _rows(self):
        return taifex_daily.parse_csv_text(self.CSV)

    def test_parse_and_front_month_combine(self):
        df = taifex_daily.build_front_month_daily(self._rows(), 'TX')
        self.assertEqual(len(df), 2)
        bar = df.loc[pd.Timestamp('2026-07-10')]
        # 近全:Open=夜盤開 45800、Close=日盤收 46281、High/Low 兩時段極值、量加總
        self.assertEqual(bar['Open'], 45800)
        self.assertEqual(bar['Close'], 46281)
        self.assertEqual(bar['High'], 46495)
        self.assertEqual(bar['Low'], 45700)
        self.assertEqual(bar['Volume'], 120000)
        # 無夜盤日:直接用一般時段
        bar2 = df.loc[pd.Timestamp('2026-07-13')]
        self.assertEqual(bar2['Open'], 46300)
        self.assertEqual(bar2['Volume'], 80000)

    def test_month_rank_r1_and_r2_build(self):
        """【ADR-081】驗證近一 (month_rank=1) 與次月 (month_rank=2) 連續合約建立。"""
        csv = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,"
               "結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,"
               "是否因訊息面暫停交易,交易時段,價差對單式委託成交量\n"
               "2026/07/10,TX,202607,45900,46495,45701,46281,633,1.39,90000,-,-,-,-,-,-,,一般,0\n"
               "2026/07/10,TX,202608,45500,46000,45300,45800,500,1.10,15000,-,-,-,-,-,-,,一般,0\n")
        rows = taifex_daily.parse_csv_text(csv)
        r1 = taifex_daily.build_front_month_daily(rows, 'TX', session='all', month_rank=1)
        r2 = taifex_daily.build_front_month_daily(rows, 'TX', session='all', month_rank=2)
        self.assertEqual(r1.iloc[0]['Close'], 46281)
        self.assertEqual(r2.iloc[0]['Close'], 45800)

    def test_parse_big5_bytes_and_zip(self):
        raw = self.CSV.encode('cp950')
        rows = taifex_daily.extract_rows_from_bytes(raw, 'x.csv')
        self.assertEqual(len(taifex_daily.build_front_month_daily(rows, 'TX')), 2)
        import io as _io, zipfile as _zf
        buf = _io.BytesIO()
        with _zf.ZipFile(buf, 'w') as z:
            z.writestr('Daily_2026_07_10.csv', raw)
        rows_z = taifex_daily.extract_rows_from_bytes(buf.getvalue(), 'Daily_2026_07_10.zip')
        self.assertEqual(len(rows_z), len(rows))

    def test_html_error_page_yields_no_rows(self):
        rows = taifex_daily.extract_rows_from_bytes(b'<html><body>error</body></html>')
        self.assertEqual(rows, [])

    def test_merge_daily_new_wins_on_overlap(self):
        old = pd.DataFrame({'Open': [1.0], 'High': [2.0], 'Low': [0.5], 'Close': [1.5], 'Volume': [10.0]},
                           index=[pd.Timestamp('2026-07-10')])
        new = pd.DataFrame({'Open': [9.0], 'High': [9.0], 'Low': [9.0], 'Close': [9.0], 'Volume': [1.0]},
                           index=[pd.Timestamp('2026-07-10'), pd.Timestamp('2026-07-11')] [:1])
        merged = taifex_daily.merge_daily(old, new)
        self.assertEqual(merged.loc[pd.Timestamp('2026-07-10'), 'Close'], 9.0)

    def test_extend_shioaji_daily_prepends_only_older(self):
        hist = taifex_daily.build_front_month_daily(self._rows(), 'TX')
        # shioaji 端從 7/13 起 (與期交所 7/13 重疊,且收盤不同 → 必須以 shioaji 為準)
        sj = pd.DataFrame({'Open': [46310.0, 46500.0], 'High': [46520.0, 46700.0],
                           'Low': [46260.0, 46480.0], 'Close': [46460.0, 46650.0],
                           'Volume': [81000.0, 82000.0]},
                          index=[pd.Timestamp('2026-07-13'), pd.Timestamp('2026-07-14')])
        out = taifex_daily.extend_shioaji_df(sj, hist, "日K")
        self.assertEqual(len(out), 3)                      # 只前接 7/10 一根
        self.assertEqual(out.index[0], pd.Timestamp('2026-07-10'))
        self.assertEqual(out.loc[pd.Timestamp('2026-07-13'), 'Close'], 46460.0)  # shioaji 權威
        # 分K不適用,原樣回傳
        self.assertIs(taifex_daily.extend_shioaji_df(sj, hist, "5分K"), sj)

    def test_extend_weekly_uses_wmon_like_adr007(self):
        hist = taifex_daily.build_front_month_daily(self._rows(), 'TX')
        sj_w = pd.DataFrame({'Open': [46310.0], 'High': [46700.0], 'Low': [46260.0],
                             'Close': [46650.0], 'Volume': [163000.0]},
                            index=[pd.Timestamp('2026-07-13')])  # 7/13(一) 那週,shioaji 已有
        out = taifex_daily.extend_shioaji_df(sj_w, hist, "周K")
        # 期交所 7/10(五) 屬 7/6(一) 那週 → 前接一根;7/13 那週維持 shioaji
        self.assertEqual(len(out), 2)
        self.assertEqual(out.index[0], pd.Timestamp('2026-07-06'))
        self.assertEqual(out.loc[pd.Timestamp('2026-07-13'), 'Close'], 46650.0)

    def test_month_chunks_respects_limit(self):
        from datetime import date as _date
        chunks = taifex_daily.month_chunks(_date(2026, 1, 1), _date(2026, 3, 15), max_days=28)
        self.assertEqual(chunks[0][0], _date(2026, 1, 1))
        self.assertEqual(chunks[-1][1], _date(2026, 3, 15))
        for s, e in chunks:
            self.assertLessEqual((e - s).days + 1, 28)
        # 首尾相接不重疊
        for (s1, e1), (s2, e2) in zip(chunks, chunks[1:]):
            self.assertEqual((s2 - e1).days, 1)

    def test_store_roundtrip_and_missing(self):
        hist = taifex_daily.build_front_month_daily(self._rows(), 'TX')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(taifex_store.load_daily(tmp, 'TX').empty)  # 未匯入 → 空
            taifex_store.save_daily(tmp, 'TX', hist)
            back = taifex_store.load_daily(tmp, 'TX')
            self.assertEqual(len(back), len(hist))
            self.assertEqual(back.loc[pd.Timestamp('2026-07-10'), 'Close'], 46281)


class TestMarketSession(unittest.TestCase):
    """【ADR-070】交易時段判斷 (自動交易的開/收盤閘門)。用固定 datetime 驗邊界。"""
    # 2026-07-13 是週一,2026-07-18 週六,2026-07-19 週日。

    def _dt(self, y, mo, d, h, mi):
        from datetime import datetime as _datetime
        return _datetime(y, mo, d, h, mi)

    def test_stock_open_hours(self):
        # 週一 09:00 開、13:30 收 (含邊界),盤前/盤後關
        self.assertFalse(market_session.is_stock_open(self._dt(2026, 7, 13, 8, 59)))
        self.assertTrue(market_session.is_stock_open(self._dt(2026, 7, 13, 9, 0)))
        self.assertTrue(market_session.is_stock_open(self._dt(2026, 7, 13, 13, 30)))
        self.assertFalse(market_session.is_stock_open(self._dt(2026, 7, 13, 13, 31)))

    def test_stock_closed_on_weekend(self):
        self.assertFalse(market_session.is_stock_open(self._dt(2026, 7, 18, 10, 0)))  # 週六
        self.assertFalse(market_session.is_stock_open(self._dt(2026, 7, 19, 10, 0)))  # 週日

    def test_futures_day_session(self):
        self.assertFalse(market_session.is_futures_day_open(self._dt(2026, 7, 13, 8, 44)))
        self.assertTrue(market_session.is_futures_day_open(self._dt(2026, 7, 13, 8, 45)))
        self.assertTrue(market_session.is_futures_day_open(self._dt(2026, 7, 13, 13, 45)))
        self.assertFalse(market_session.is_futures_day_open(self._dt(2026, 7, 13, 13, 46)))

    def test_futures_night_evening_part(self):
        # 週一傍晚 15:00 起開盤
        self.assertFalse(market_session.is_futures_night_open(self._dt(2026, 7, 13, 14, 59)))
        self.assertTrue(market_session.is_futures_night_open(self._dt(2026, 7, 13, 15, 0)))
        self.assertTrue(market_session.is_futures_night_open(self._dt(2026, 7, 13, 23, 30)))

    def test_futures_night_morning_part(self):
        # 週二凌晨 04:59 仍在 (屬週一夜盤),05:00 收
        self.assertTrue(market_session.is_futures_night_open(self._dt(2026, 7, 14, 4, 59)))
        self.assertFalse(market_session.is_futures_night_open(self._dt(2026, 7, 14, 5, 0)))

    def test_friday_night_runs_into_saturday(self):
        # 週五 23:00 開;週六 04:00 仍屬週五夜盤;週六 15:00 不開盤
        self.assertTrue(market_session.is_futures_night_open(self._dt(2026, 7, 17, 23, 0)))
        self.assertTrue(market_session.is_futures_night_open(self._dt(2026, 7, 18, 4, 0)))
        self.assertFalse(market_session.is_futures_night_open(self._dt(2026, 7, 18, 15, 0)))

    def test_monday_predawn_has_no_night(self):
        # 週一凌晨不屬任何夜盤 (前一天週日沒開)
        self.assertFalse(market_session.is_futures_night_open(self._dt(2026, 7, 13, 3, 0)))

    def test_odd_lot_open_default_0910(self):
        # 預設盤中零股 09:10 才開:09:00 整股開、零股還沒開;09:10 零股才開
        self.assertTrue(market_session.is_stock_open(self._dt(2026, 7, 13, 9, 0)))
        self.assertFalse(market_session.is_odd_lot_open(self._dt(2026, 7, 13, 9, 0)))
        self.assertFalse(market_session.is_odd_lot_open(self._dt(2026, 7, 13, 9, 9)))
        self.assertTrue(market_session.is_odd_lot_open(self._dt(2026, 7, 13, 9, 10)))
        self.assertTrue(market_session.is_odd_lot_open(self._dt(2026, 7, 13, 13, 30)))
        self.assertFalse(market_session.is_odd_lot_open(self._dt(2026, 7, 13, 13, 31)))

    def test_odd_lot_open_configurable(self):
        # 顯式帶入 09:00 → 零股 09:00 就開 (未來交易所改制的情境)
        self.assertTrue(market_session.is_odd_lot_open(self._dt(2026, 7, 13, 9, 0), open_minute=9 * 60))
        # set_odd_lot_open_hhmm 改全域預設,再改回來不影響其他測試
        try:
            market_session.set_odd_lot_open_hhmm('09:00')
            self.assertTrue(market_session.is_odd_lot_open(self._dt(2026, 7, 13, 9, 0)))
        finally:
            market_session.set_odd_lot_open_minute(9 * 60 + 10)
        self.assertFalse(market_session.is_odd_lot_open(self._dt(2026, 7, 13, 9, 0)))

    def test_is_market_open_dispatch(self):
        day = self._dt(2026, 7, 13, 10, 0)   # 週一上午:兩市場都開
        self.assertTrue(market_session.is_market_open('股票', day))
        self.assertTrue(market_session.is_market_open('零股', day))
        self.assertTrue(market_session.is_market_open('期貨', day))
        night = self._dt(2026, 7, 13, 22, 0)  # 週一晚上:只有期貨夜盤
        self.assertFalse(market_session.is_market_open('股票', night))
        self.assertTrue(market_session.is_market_open('期貨', night))
        # include_night=False → 夜盤不算開
        self.assertFalse(market_session.is_market_open('期貨', night, include_night=False))
        # 未知種類保守回 False
        self.assertFalse(market_session.is_market_open('比特幣', day))

    def test_session_label(self):
        self.assertEqual(market_session.session_label('期貨', self._dt(2026, 7, 13, 10, 0)), '期貨日盤')
        self.assertEqual(market_session.session_label('期貨', self._dt(2026, 7, 13, 22, 0)), '期貨夜盤')
        self.assertEqual(market_session.session_label('期貨', self._dt(2026, 7, 13, 22, 0), include_night=False), '休市')
        self.assertEqual(market_session.session_label('股票', self._dt(2026, 7, 13, 22, 0)), '休市')


class TestSecureStore(unittest.TestCase):
    """【ADR-073】加密憑證存放:往返、金鑰不符、竄改偵測。"""
    def test_roundtrip(self):
        key = b'device-seed-abc-123'
        blob = secure_store.encrypt("憑證密碼P@ss零股", key)
        self.assertNotIn("憑證密碼", blob)  # 密文裡看不到明文
        self.assertEqual(secure_store.decrypt(blob, key), "憑證密碼P@ss零股")

    def test_dict_roundtrip(self):
        key = b'seed'
        d = {'pid': 'A123', 'ca_pw': 'secret', 'n': 5}
        back = secure_store.decrypt_dict(secure_store.encrypt_dict(d, key), key)
        self.assertEqual(back, d)

    def test_wrong_key_fails(self):
        blob = secure_store.encrypt("hello", b'key-A')
        with self.assertRaises(ValueError):
            secure_store.decrypt(blob, b'key-B')

    def test_tamper_detected(self):
        import base64
        blob = secure_store.encrypt("hello world", b'k')
        raw = bytearray(base64.b64decode(blob))
        raw[-1] ^= 0x01  # 動 tag 最後一個 byte
        tampered = base64.b64encode(bytes(raw)).decode('ascii')
        with self.assertRaises(ValueError):
            secure_store.decrypt(tampered, b'k')

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            secure_store.encrypt("x", b'')

    def test_ciphertext_differs_each_time(self):
        # 隨機 salt/nonce → 同明文同金鑰兩次密文不同
        key = b'k'
        self.assertNotEqual(secure_store.encrypt("same", key), secure_store.encrypt("same", key))


if __name__ == "__main__":
    unittest.main(verbosity=2)
