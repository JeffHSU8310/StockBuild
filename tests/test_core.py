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
import datetime
import unittest
import urllib.parse

# 讓這份測試不管從哪個目錄被呼叫，都找得到專案根目錄下的 core/ 與 data/ 套件
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from core import tick_rules
from core import futures_session
from core import market_session
from core import secure_store
from core import order_rules
from core import order_intent
from core import sj_compat
from core import unit_format
from core import indicators
from core import chart_viewport
from core import history_policy
from core import strategy_engine
from core import backtest
from core import custom_strategy
from core import fut_catalog
from core import ai_helper
from core import cost_model
from core import optimizer
from core import paper_account
from core import taifex_daily
from core import chukuangren_band
from core import telegram_notify
from core import telegram_control
from core import market_pattern
from core import kbars_plan
from core import fibonacci
from core import jae
from core import palette
from core import api_test
from core import regime_panel
from core import chips_parser
from core import chips_features
from core import volume_profile
from core import fundamental_parser
from core import market_screener
from core import screener_backtest
from data import config_store
from data import taifex_store
from data import chips_store
from data import market_store
from data import kbars_store


class TestChartViewport(unittest.TestCase):
    def test_unlaid_out_canvas_keeps_adr082_caps(self):
        self.assertEqual(chart_viewport.render_limit("5分K", 1), 1200)
        self.assertEqual(chart_viewport.render_limit("日K", None), 3000)

    def test_pixel_width_reduces_invisible_artist_count(self):
        self.assertEqual(chart_viewport.render_limit("5分K", 640), 800)
        self.assertEqual(chart_viewport.render_limit("日K", 640), 2600)
        self.assertEqual(chart_viewport.render_limit("5分K", 300), 375)

    def test_limits_never_exceed_existing_safety_caps(self):
        self.assertEqual(chart_viewport.render_limit("1分K", 4000), 1200)
        self.assertEqual(chart_viewport.render_limit("周K", 4000), 650)

    def test_long_timeframes_keep_at_least_ten_years(self):
        self.assertGreaterEqual(chart_viewport.render_limit("日K", 640), 2600)
        self.assertGreaterEqual(chart_viewport.render_limit("周K", 640), 530)
        self.assertGreaterEqual(chart_viewport.render_limit("月K", 640), 125)

    def test_small_canvas_still_has_usable_history(self):
        self.assertEqual(chart_viewport.render_limit("15分K", 200), 300)

    def test_tail_window_preserves_full_input_and_returns_copy(self):
        original = pd.DataFrame({'Close': np.arange(2000)})
        plotted, limit = chart_viewport.tail_window(original, "5分K", 640)
        self.assertEqual(limit, 800)
        self.assertEqual(len(plotted), 800)
        self.assertEqual(len(original), 2000)
        self.assertEqual(plotted.iloc[0]['Close'], 1200)
        plotted.iloc[-1, 0] = -1
        self.assertEqual(original.iloc[-1]['Close'], 1999)


class TestHistoryPolicy(unittest.TestCase):
    def test_deep_history_is_at_least_ten_years(self):
        self.assertEqual(history_policy.MIN_HISTORY_YEARS, 10)
        self.assertGreaterEqual(history_policy.DEEP_HISTORY_DAYS, 3653)

    def test_only_long_timeframes_request_deep_history(self):
        for tf in ("日K", "周K", "月K"):
            self.assertTrue(history_policy.needs_deep_history(tf), tf)
        for tf in ("1分K", "5分K", "60分K"):
            self.assertFalse(history_policy.needs_deep_history(tf), tf)

    def test_us_request_has_more_than_ten_year_boundary(self):
        self.assertEqual(history_policy.US_HISTORY_PERIOD, "20y")

    def test_first_shioaji_backfill_requests_maximum_practical_range(self):
        self.assertEqual(history_policy.SHIOAJI_MAX_REQUEST_DAYS, 7300)


class TestKbarsSqliteStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'nested', 'kbars.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _df(self, closes=(100.0, 101.0)):
        idx = pd.to_datetime(['2026-07-31', '2026-08-01'])
        return pd.DataFrame({'Open': closes, 'High': closes, 'Low': closes,
                             'Close': closes, 'Volume': [10, 20]}, index=idx)

    def test_initialize_creates_database_and_schema(self):
        self.assertEqual(kbars_store.initialize(self.db), self.db)
        self.assertTrue(os.path.exists(self.db))

    def test_upsert_and_load_round_trip(self):
        self.assertEqual(kbars_store.upsert(self.db, '2330', 'stock', '日K', self._df()), 2)
        got = kbars_store.load(self.db, '2330', 'stock', '日K')
        self.assertEqual(len(got), 2)
        self.assertEqual(float(got.iloc[-1]['Close']), 101.0)

    def test_same_bar_is_updated_not_duplicated(self):
        kbars_store.upsert(self.db, '2330', 'stock', '日K', self._df())
        kbars_store.upsert(self.db, '2330', 'stock', '日K', self._df((100.0, 999.0)))
        got = kbars_store.load(self.db, '2330', 'stock', '日K')
        self.assertEqual(len(got), 2)
        self.assertEqual(float(got.iloc[-1]['Close']), 999.0)

    def test_symbols_and_timeframes_are_isolated(self):
        kbars_store.upsert(self.db, '2330', 'stock', '日K', self._df())
        self.assertIsNone(kbars_store.load(self.db, '0050', 'stock', '日K'))
        self.assertIsNone(kbars_store.load(self.db, '2330', 'stock', '周K'))
        first, last, count = kbars_store.coverage(self.db, '2330', 'stock', '日K')
        self.assertEqual(count, 2)
        self.assertTrue(first.startswith('2026-07-31'))
        self.assertTrue(last.startswith('2026-08-01'))


class TestChartViewport(unittest.TestCase):
    """ADR-140 的視窗規則,**上限在 ADR-141 被改過**。

    這三條原本寫的是 ADR-082/140 的舊上限(日K 1000、寬度 640 時 800、
    周K 1000),而 ADR-141 已經明確改成「長週期畫布本身就要容得下 10 年」
    (日 2,600 / 週 530 / 月 125,見 core/history_policy.py 與
    core/chart_viewport.LONG_TERM_LIMITS)。實作照 ADR-141 走,測試沒跟著改,
    於是 main 上這三條一直是紅的。這裡把期望值更新成 ADR-141 的決定,
    並改用常數而不是再抄一次數字 —— 下次調整上限就不會再分歧一次(P-67)。
    """

    def test_unlaid_out_canvas_falls_back_to_the_cap(self):
        """Tk 還沒排版時寬度不可信,退回該週期的上限。"""
        self.assertEqual(chart_viewport.render_limit("5分K", 1),
                         chart_viewport.INTRADAY_CAP)
        self.assertEqual(chart_viewport.render_limit("日K", None),
                         chart_viewport.LONG_TERM_LIMITS["日K"][1])

    def test_pixel_width_reduces_invisible_artist_count(self):
        """分K 會依畫布寬度縮減 artist 數量(ADR-140 的本意)。"""
        self.assertEqual(chart_viewport.render_limit("5分K", 640), 800)
        self.assertEqual(chart_viewport.render_limit("5分K", 300), 375)

    def test_long_period_never_drops_below_ten_years(self):
        """【ADR-141】長週期**不論畫布多窄**都不可以被砍到只剩幾年 ——
        那正是 P-119 記錄的坑(使用者載了十年卻只看得到四年)。

        這裡斷言的是**不變式**而不是某個寬度的確切數字:窄畫布時
        `max(下限, 寬度×1.25)` 保住下限、寬畫布時 `min(上限, ...)` 壓住上限,
        兩者之間隨寬度變動 —— 挑一個寬度硬比等於把實作細節抄進測試。"""
        for tf in ("日K", "周K", "月K"):
            floor_, cap = chart_viewport.LONG_TERM_LIMITS[tf]
            for w in (None, 1, 200, 640, 1920, 40000):
                got = chart_viewport.render_limit(tf, w)
                self.assertGreaterEqual(got, floor_,
                                        f"{tf} 寬度={w} 時掉到 10 年下限以下 ({got})")
                self.assertLessEqual(got, cap, f"{tf} 寬度={w} 時超過上限 ({got})")
            self.assertEqual(chart_viewport.render_limit(tf, 40000), cap,
                             f"{tf} 夠寬時要吃滿上限")
        # 分K 沒有 10 年的問題,維持 ADR-082 的 1200 上限
        self.assertEqual(chart_viewport.render_limit("1分K", 4000),
                         chart_viewport.INTRADAY_CAP)

    def test_ten_year_floor_really_covers_ten_years(self):
        """反向對照:下限的數字要真的夠 10 年,不是隨手填的
        (台股一年約 250 個交易日 / 52 週 / 12 個月)。"""
        self.assertGreaterEqual(chart_viewport.LONG_TERM_LIMITS["日K"][0],
                                250 * history_policy.MIN_HISTORY_YEARS)
        self.assertGreaterEqual(chart_viewport.LONG_TERM_LIMITS["周K"][0],
                                52 * history_policy.MIN_HISTORY_YEARS)
        self.assertGreaterEqual(chart_viewport.LONG_TERM_LIMITS["月K"][0],
                                12 * history_policy.MIN_HISTORY_YEARS)

    def test_small_canvas_still_has_usable_history(self):
        self.assertEqual(chart_viewport.render_limit("15分K", 200), 300)

    def test_tail_window_preserves_full_input_and_returns_copy(self):
        original = pd.DataFrame({'Close': np.arange(2000)})
        plotted, limit = chart_viewport.tail_window(original, "5分K", 640)
        self.assertEqual(limit, 800)
        self.assertEqual(len(plotted), 800)
        self.assertEqual(len(original), 2000)
        self.assertEqual(plotted.iloc[0]['Close'], 1200)
        plotted.iloc[-1, 0] = -1
        self.assertEqual(original.iloc[-1]['Close'], 1999)


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
        # (【新ADR 自訂週期】任意正整數的 N分K/N時K 現在都合法,這裡改用真正
        # 不合法的格式測試,不能再用 '3分K' 這種其實合法的自訂週期當反例)
        s_bad2['watch_symbol'] = '^TWII'; s_bad2['watch_timeframe'] = '亂七八糟'
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

    def test_apply_fill_exec_entry_price_tracks_b_separately_from_a(self):
        """【新ADR】看A做B時,apply_fill(exec_price=) 要把B的成交價存進
        exec_entry_price,跟A的entry_price是兩個不同尺度的數字。"""
        s = strategy_engine.new_strategy()
        s.update({'name': 'AB', 'symbol': 'MXFR1', 'trade_type': '期貨', 'market': '台期貨'})
        rt = strategy_engine.new_runtime()
        intent = {'kind': 'OPEN', 'action': '買進', 'qty': 1, 'price': 22500.0}  # A(TXF)的價
        strategy_engine.apply_fill(s, rt, intent, 0, exec_price=44402.0)  # B(MXFR1)的成交價
        self.assertEqual(rt['entry_price'], 22500.0)
        self.assertEqual(rt['exec_entry_price'], 44402.0)
        close_intent = {'kind': 'CLOSE', 'action': '賣出', 'qty': 1, 'price': 22400.0}
        strategy_engine.apply_fill(s, rt, close_intent, 1)
        self.assertEqual(rt['exec_entry_price'], 0.0)

    def test_apply_fill_without_exec_price_falls_back_to_intent_price(self):
        """未帶 exec_price (一般不看A做B、測試/回測既有呼叫點) 時,
        exec_entry_price 退回等於 entry_price,行為不變。"""
        s = strategy_engine.new_strategy()
        rt = strategy_engine.new_runtime()
        intent = {'kind': 'OPEN', 'action': '買進', 'qty': 1, 'price': 100.0}
        strategy_engine.apply_fill(s, rt, intent, 0)
        self.assertEqual(rt['exec_entry_price'], 100.0)

    # ---------- 【新ADR】期貨即時停損/停利 (不等K棒收盤) ----------

    def test_intrabar_futures_stop_triggers_immediately_on_live_price(self):
        s = strategy_engine.new_strategy()
        s.update({'name': 'F', 'symbol': 'MXFR1', 'trade_type': '期貨', 'market': '台期貨',
                  'stop_loss_abs': 70.0})
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 1, 'exec_entry_price': 44402.0})
        intent = strategy_engine.check_intrabar_futures_stop(s, rt, 44277.0)  # 跌125點>70點
        self.assertIsNotNone(intent)
        self.assertEqual(intent['kind'], 'CLOSE')
        self.assertEqual(intent['action'], '賣出')
        self.assertIn('即時停損', intent['reason'])

    def test_intrabar_futures_stop_not_triggered_within_threshold(self):
        s = strategy_engine.new_strategy()
        s.update({'name': 'F', 'symbol': 'MXFR1', 'trade_type': '期貨', 'market': '台期貨',
                  'stop_loss_abs': 70.0})
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 1, 'exec_entry_price': 44402.0})
        intent = strategy_engine.check_intrabar_futures_stop(s, rt, 44350.0)  # 跌52點<70點
        self.assertIsNone(intent)

    def test_intrabar_stop_now_applies_to_stocks(self):
        """【ADR-135】原本這條測「股票不適用即時停損」。使用者要求
        「停損停利點數到了,就立即執行,不需要等待到收盤」且沒有限定商品別,
        所以規則反過來了:**股票也要即時觸發**。同一組數字、相反的期待。"""
        s = strategy_engine.new_strategy()
        s.update({'name': 'ST', 'symbol': '2330', 'trade_type': '股票', 'stop_loss_abs': 5.0})
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 1, 'exec_entry_price': 600.0})
        intent = strategy_engine.check_intrabar_stop(s, rt, 500.0)   # 跌100元遠超5元
        self.assertIsNotNone(intent, "股票也要即時停損 (ADR-135)")
        self.assertEqual(intent['kind'], 'CLOSE')
        # 反向對照:沒到停損點就不可以出場 (否則等於一有部位就砍)
        self.assertIsNone(strategy_engine.check_intrabar_stop(s, rt, 597.0))

    def test_intrabar_stop_applies_to_odd_lot_too(self):
        s = strategy_engine.new_strategy()
        s.update({'name': 'ODD', 'symbol': '2330', 'trade_type': '零股',
                  'take_profit_abs': 10.0})
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 100, 'exec_entry_price': 600.0})
        self.assertIsNotNone(strategy_engine.check_intrabar_stop(s, rt, 615.0))
        self.assertIsNone(strategy_engine.check_intrabar_stop(s, rt, 605.0))

    def test_legacy_alias_still_points_at_the_same_function(self):
        """舊名 check_intrabar_futures_stop 留成別名,既有呼叫端不必一次全改。"""
        self.assertIs(strategy_engine.check_intrabar_futures_stop,
                      strategy_engine.check_intrabar_stop)

    def test_intrabar_futures_stop_ignored_when_flat(self):
        s = strategy_engine.new_strategy()
        s.update({'name': 'F', 'symbol': 'MXFR1', 'trade_type': '期貨', 'market': '台期貨',
                  'stop_loss_abs': 70.0})
        rt = strategy_engine.new_runtime()  # state 預設 FLAT
        intent = strategy_engine.check_intrabar_futures_stop(s, rt, 40000.0)
        self.assertIsNone(intent)

    def test_intrabar_futures_stop_take_profit_pct_short(self):
        s = strategy_engine.new_strategy()
        s.update({'name': 'F', 'symbol': 'TMF', 'trade_type': '期貨', 'market': '台期貨',
                  'take_profit_pct': 1.0})
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'SHORT', 'qty': 1, 'exec_entry_price': 20000.0})
        intent = strategy_engine.check_intrabar_futures_stop(s, rt, 19700.0)  # 跌1.5%>1%停利
        self.assertIsNotNone(intent)
        self.assertEqual(intent['action'], '買進')
        self.assertIn('即時停利', intent['reason'])

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

    # ---------- 【新ADR 多帳戶】模擬帳戶 id/name、策略的 account_id ----------

    def test_new_account_default_id_and_name(self):
        a = paper_account.new_account(1000000)
        self.assertTrue(a['id'])  # 非空 (自動產生的 uuid 短碼)
        self.assertEqual(a['name'], '預設帳戶')

    def test_new_account_ids_are_unique(self):
        a = paper_account.new_account()
        b = paper_account.new_account()
        self.assertNotEqual(a['id'], b['id'])

    def test_new_account_explicit_name_and_id(self):
        a = paper_account.new_account(500000, name='策略A專用', account_id='acct-a')
        self.assertEqual(a['id'], 'acct-a')
        self.assertEqual(a['name'], '策略A專用')

    def test_new_account_blank_name_falls_back(self):
        a = paper_account.new_account(name='   ')
        self.assertEqual(a['name'], '未命名帳戶')

    def test_two_accounts_are_independent(self):
        """【核心防衝突機制】兩個帳戶各自的 positions/history 完全獨立,
        同一檔標的在不同帳戶開倉不會互相影響——這是多帳戶存在的意義。"""
        a = paper_account.new_account(1000000, account_id='a')
        b = paper_account.new_account(1000000, account_id='b')
        paper_account.apply_fill(a, 't', '台期貨', 'MXFR1', '買進', 'OPEN', 1, 44000, trade_type='期貨')
        paper_account.apply_fill(b, 't', '台期貨', 'MXFR1', '賣出', 'OPEN', 1, 44000, trade_type='期貨')
        self.assertEqual(a['positions']['MXFR1']['direction'], '多')
        self.assertEqual(b['positions']['MXFR1']['direction'], '空')
        self.assertEqual(len(a['history']), 1)
        self.assertEqual(len(b['history']), 1)

    def test_account_id_of_default_when_unset(self):
        s = strategy_engine.new_strategy()
        self.assertEqual(s['account_id'], 'default')
        self.assertEqual(strategy_engine.account_id_of(s), 'default')

    def test_account_id_of_explicit(self):
        s = strategy_engine.new_strategy()
        s['account_id'] = 'acct-xyz'
        self.assertEqual(strategy_engine.account_id_of(s), 'acct-xyz')

    def test_account_id_of_blank_falls_back_to_default(self):
        s = strategy_engine.new_strategy()
        s['account_id'] = '   '
        self.assertEqual(strategy_engine.account_id_of(s), 'default')
        del s['account_id']
        self.assertEqual(strategy_engine.account_id_of(s), 'default')  # 舊策略沒這個欄位

    # ---------- 【新ADR 持倉核對】position_mismatch ----------

    def test_position_mismatch_none_when_both_flat(self):
        s = strategy_engine.new_strategy()
        rt = strategy_engine.new_runtime()  # state='FLAT'
        self.assertIsNone(strategy_engine.position_mismatch(s, rt, None))

    def test_position_mismatch_none_when_match(self):
        s = strategy_engine.new_strategy()
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 5})
        acct_pos = {'direction': '多', 'qty': 5, 'avg_price': 100.0}
        self.assertIsNone(strategy_engine.position_mismatch(s, rt, acct_pos))

    def test_position_mismatch_detects_orphan_account_position(self):
        """策略記錄無持倉,但帳戶內卻有一筆該商品的部位。"""
        s = strategy_engine.new_strategy()
        rt = strategy_engine.new_runtime()  # FLAT
        acct_pos = {'direction': '空', 'qty': 3, 'avg_price': 44000.0}
        m = strategy_engine.position_mismatch(s, rt, acct_pos)
        self.assertIsNotNone(m)
        self.assertEqual(m['rt_state'], 'FLAT')
        self.assertEqual(m['rt_qty'], 0)
        self.assertEqual(m['acct_direction'], '空')
        self.assertEqual(m['acct_qty'], 3)

    def test_position_mismatch_detects_missing_account_position(self):
        """策略記錄有持倉,但帳戶內完全沒有這筆部位 (例如帳戶被重置過)。"""
        s = strategy_engine.new_strategy()
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 2})
        m = strategy_engine.position_mismatch(s, rt, None)
        self.assertIsNotNone(m)
        self.assertEqual(m['rt_state'], 'LONG')
        self.assertEqual(m['rt_qty'], 2)
        self.assertIsNone(m['acct_direction'])
        self.assertEqual(m['acct_qty'], 0)

    def test_position_mismatch_detects_qty_difference(self):
        s = strategy_engine.new_strategy()
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'SHORT', 'qty': 5})
        acct_pos = {'direction': '空', 'qty': 3, 'avg_price': 100.0}
        m = strategy_engine.position_mismatch(s, rt, acct_pos)
        self.assertIsNotNone(m)
        self.assertEqual(m['rt_qty'], 5)
        self.assertEqual(m['acct_qty'], 3)

    def test_position_mismatch_detects_direction_difference(self):
        s = strategy_engine.new_strategy()
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 5})
        acct_pos = {'direction': '空', 'qty': 5, 'avg_price': 100.0}
        m = strategy_engine.position_mismatch(s, rt, acct_pos)
        self.assertIsNotNone(m)
        self.assertEqual(m['acct_direction'], '空')

    # ---------- 【新ADR 自訂週期】timeframe_minutes / is_valid_timeframe ----------

    def test_timeframe_minutes_presets(self):
        self.assertEqual(strategy_engine.timeframe_minutes('1分K'), 1)
        self.assertEqual(strategy_engine.timeframe_minutes('5分K'), 5)
        self.assertEqual(strategy_engine.timeframe_minutes('60分K'), 60)

    def test_timeframe_minutes_custom_minute(self):
        self.assertEqual(strategy_engine.timeframe_minutes('45分K'), 45)

    def test_timeframe_minutes_custom_hour(self):
        self.assertEqual(strategy_engine.timeframe_minutes('3時K'), 180)

    def test_timeframe_minutes_day_class_returns_none(self):
        for tf in ('日K', '周K', '月K'):
            self.assertIsNone(strategy_engine.timeframe_minutes(tf))

    def test_timeframe_minutes_invalid_returns_none(self):
        for tf in ('', '0分K', '-5分K', '亂七八糟', '5分', None):
            self.assertIsNone(strategy_engine.timeframe_minutes(tf))

    def test_is_valid_timeframe_day_class(self):
        for tf in ('日K', '周K', '月K'):
            self.assertTrue(strategy_engine.is_valid_timeframe(tf))

    def test_is_valid_timeframe_custom(self):
        self.assertTrue(strategy_engine.is_valid_timeframe('45分K'))
        self.assertTrue(strategy_engine.is_valid_timeframe('3時K'))

    def test_is_valid_timeframe_rejects_garbage(self):
        for tf in ('', '亂七八糟', '0分K', None):
            self.assertFalse(strategy_engine.is_valid_timeframe(tf))

    def test_validate_strategy_accepts_custom_timeframe(self):
        s = self._base_strategy(timeframe='45分K')
        self.assertTrue(strategy_engine.validate_strategy(s)[0])

    def test_validate_strategy_rejects_bad_timeframe(self):
        s = self._base_strategy(timeframe='亂七八糟')
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn('週期', msg)

    # ---------- 【新ADR】委託方式:限價/市價/範圍市價 ----------

    def _base_strategy(self, **overrides):
        s = strategy_engine.new_strategy()
        s.update({'name': 'P', 'symbol': 'X', 'direction': '做多',
                  'entry': [{'type': 'ma_cross_up', 'params': {}}], 'stop_loss_pct': 2.0})
        s.update(overrides)
        return s

    def test_price_type_default_is_limit(self):
        s = strategy_engine.new_strategy()
        self.assertEqual(s['price_type'], '限價')
        self.assertEqual(strategy_engine.price_type_of(s), '限價')

    def test_price_type_range_market_valid_for_futures(self):
        s = self._base_strategy(trade_type='期貨', market='台期貨', symbol='TXF', price_type='範圍市價')
        self.assertTrue(strategy_engine.validate_strategy(s)[0])
        self.assertEqual(strategy_engine.price_type_of(s), '範圍市價')

    def test_price_type_range_market_rejected_for_stock(self):
        s = self._base_strategy(trade_type='股票', price_type='範圍市價')
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn('範圍市價', msg)

    def test_price_type_market_valid_for_stock(self):
        s = self._base_strategy(trade_type='股票', price_type='市價')
        self.assertTrue(strategy_engine.validate_strategy(s)[0])

    def test_price_type_odd_lot_forced_limit(self):
        s = self._base_strategy(trade_type='零股', price_type='市價')
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn('零股', msg)
        # price_type_of 防呆:即使存了不合法值,讀出來也強制退回限價
        self.assertEqual(strategy_engine.price_type_of(s), '限價')

    def test_price_type_invalid_value_falls_back_to_limit(self):
        s = self._base_strategy(trade_type='期貨', market='台期貨', price_type='亂填')
        self.assertEqual(strategy_engine.price_type_of(s), '限價')
        ok, msg = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)  # 存檔時仍然要擋下不合法值,不能默默改掉使用者的設定


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


class TestTelegramConfigStore(unittest.TestCase):
    """【新ADR】Telegram 通知設定檔存取。"""

    def test_roundtrip_and_missing(self):
        import tempfile
        p = os.path.join(tempfile.gettempdir(), '_telegram_notify_config.json')
        config_store.save_telegram_config(p, '123:ABC', '999888', True)
        d = config_store.load_telegram_config(p)
        self.assertEqual(d['bot_token'], '123:ABC')
        self.assertEqual(d['chat_id'], '999888')
        self.assertTrue(d['enabled'])
        self.assertEqual(config_store.load_telegram_config(p + '.none'), {})
        os.remove(p)

    def test_disabled_roundtrip(self):
        import tempfile
        p = os.path.join(tempfile.gettempdir(), '_telegram_notify_config2.json')
        config_store.save_telegram_config(p, '', '', False)
        d = config_store.load_telegram_config(p)
        self.assertEqual(d['bot_token'], '')
        self.assertFalse(d['enabled'])
        os.remove(p)

    def test_remote_control_defaults_off(self):
        """【ADR-108】遠端控制預設關閉:沒有明講要開,就不能開。
        舊設定檔沒有這個欄位時,讀回來也必須是 False。"""
        import tempfile
        p = os.path.join(tempfile.gettempdir(), '_telegram_cfg_remote.json')
        config_store.save_telegram_config(p, '123:ABC', '999', True)   # 沒帶 remote_control
        self.assertFalse(config_store.load_telegram_config(p)['remote_control'])
        config_store.save_telegram_config(p, '123:ABC', '999', True, True)
        self.assertTrue(config_store.load_telegram_config(p)['remote_control'])
        # 通知關、遠端開:兩個開關互不牽動
        config_store.save_telegram_config(p, '123:ABC', '999', False, True)
        d = config_store.load_telegram_config(p)
        self.assertFalse(d['enabled'])
        self.assertTrue(d['remote_control'])
        os.remove(p)


class TestTelegramControlAuth(unittest.TestCase):
    """【ADR-108】遠端控制的授權與指令解析。

    這一組測的是「別人能不能控制我的交易系統」,任何一條紅燈都等於安全破口。
    """

    def test_only_configured_chat_id_allowed(self):
        self.assertTrue(telegram_control.is_authorized('999888', '999888'))
        self.assertTrue(telegram_control.is_authorized(999888, '999888'))     # 型別不同也要通過
        self.assertTrue(telegram_control.is_authorized('-100123', '-100123'))  # 群組是負數
        self.assertFalse(telegram_control.is_authorized('999889', '999888'))
        self.assertFalse(telegram_control.is_authorized('9998880', '999888'))  # 不可前綴符合就放行

    def test_empty_config_never_authorizes(self):
        """沒設定 chat_id 時,不能變成「誰都可以」。"""
        for allowed in ('', None, '   '):
            self.assertFalse(telegram_control.is_authorized('999888', allowed))
        for who in ('', None, '   '):
            self.assertFalse(telegram_control.is_authorized(who, '999888'))

    def test_parse_command(self):
        self.assertEqual(telegram_control.parse_command('/status'), ('/status', ''))
        self.assertEqual(telegram_control.parse_command('  /ON  策略A '), ('/on', '策略A'))
        self.assertEqual(telegram_control.parse_command('/status@MyBot'), ('/status', ''))
        self.assertEqual(telegram_control.parse_command('/off@MyBot 3'), ('/off', '3'))

    def test_non_command_ignored(self):
        """一般聊天不能被當成指令 (也不該回覆,避免洩漏 Bot 在運作)。"""
        for text in ('早安', '', None, 'status', '/不存在的指令'):
            self.assertIsNone(telegram_control.parse_command(text)[0])

    def test_enable_needs_confirm_disable_does_not(self):
        """停用往安全方向走,不可以拖延;啟用會讓系統開始下單,必須確認。"""
        for cmd in ('/on', '/start_all'):
            self.assertTrue(telegram_control.needs_confirm(cmd), cmd)
        for cmd in ('/off', '/stop_all', '/status', '/list', '/positions', '/pnl'):
            self.assertFalse(telegram_control.needs_confirm(cmd), cmd)

    def test_no_order_commands_exist(self):
        """鐵則 14:下單只能走主程式的確認視窗,遠端介面不得有任何下單指令。"""
        for bad in ('/buy', '/sell', '/order', '/close', '/cover'):
            self.assertNotIn(bad, telegram_control.COMMANDS)

    def test_confirm_code_roundtrip(self):
        code, expire = telegram_control.new_confirm_code(now=1000.0)
        pending = {'code': code, 'expire': expire, 'cmd': '/on', 'arg': '1'}
        self.assertEqual(len(code), telegram_control.CONFIRM_CODE_LEN)
        ok, _ = telegram_control.check_confirm(pending, code, now=1001.0)
        self.assertTrue(ok)
        ok, _ = telegram_control.check_confirm(pending, code.lower(), now=1001.0)
        self.assertTrue(ok, "手機輸入常是小寫,大小寫要一致視之")

    def test_confirm_rejects_wrong_expired_and_missing(self):
        code, expire = telegram_control.new_confirm_code(now=1000.0)
        pending = {'code': code, 'expire': expire, 'cmd': '/on', 'arg': '1'}
        self.assertFalse(telegram_control.check_confirm(pending, 'ZZZZ', now=1001.0)[0])
        self.assertFalse(telegram_control.check_confirm(pending, '', now=1001.0)[0])
        # 過期
        later = 1000.0 + telegram_control.CONFIRM_TTL_SEC + 1
        self.assertFalse(telegram_control.check_confirm(pending, code, now=later)[0])
        # 沒有待確認指令時,任何碼都不該通過
        self.assertFalse(telegram_control.check_confirm(None, code, now=1001.0)[0])
        self.assertFalse(telegram_control.check_confirm({}, code, now=1001.0)[0])


class TestTelegramControlStrategyMatch(unittest.TestCase):
    """【ADR-108】遠端選策略。選錯策略 = 啟用了不該啟用的東西。"""

    def setUp(self):
        self.items = [
            {'name': '台積電順勢', 'enabled': False, 'mode': '模擬',
             'symbol': '2330', 'timeframe': '日K'},
            {'name': '台積電逆勢', 'enabled': True, 'mode': '實單',
             'symbol': '2330', 'timeframe': '60分'},
            {'name': '大盤期指', 'enabled': False, 'mode': '模擬',
             'symbol': 'TXF', 'timeframe': '日K'},
        ]

    def test_match_by_number(self):
        self.assertIs(telegram_control.match_strategy(self.items, '2')[0], self.items[1])
        self.assertIsNone(telegram_control.match_strategy(self.items, '0')[0])
        self.assertIsNone(telegram_control.match_strategy(self.items, '4')[0])

    def test_exact_name_wins_over_partial(self):
        got, _ = telegram_control.match_strategy(self.items, '台積電順勢')
        self.assertIs(got, self.items[0])

    def test_ambiguous_partial_requires_number(self):
        """兩個都叫「台積電…」時必須拒絕,不可以自作主張挑第一個。"""
        got, why = telegram_control.match_strategy(self.items, '台積電')
        self.assertIsNone(got)
        self.assertIn('編號', why)

    def test_unique_partial_ok(self):
        self.assertIs(telegram_control.match_strategy(self.items, '期指')[0], self.items[2])

    def test_missing_and_empty(self):
        self.assertIsNone(telegram_control.match_strategy(self.items, '不存在')[0])
        self.assertIsNone(telegram_control.match_strategy(self.items, '')[0])
        self.assertIsNone(telegram_control.match_strategy([], '1')[0])


class TestTelegramControlTexts(unittest.TestCase):
    """【ADR-108】回覆文字:遠端看不到畫面,文字說錯等於判斷錯。"""

    def setUp(self):
        self.items = [
            {'name': 'A', 'enabled': True, 'mode': '實單', 'symbol': '2330', 'timeframe': '日K'},
            {'name': 'B', 'enabled': False, 'mode': '模擬', 'symbol': '2317', 'timeframe': '日K'},
        ]

    def test_status_counts_real_money_strategies(self):
        t = telegram_control.status_text(True, True, self.items)
        self.assertIn('實單 1', t)
        self.assertIn('啟用 1', t)

    def test_status_shows_switch_off(self):
        self.assertIn('已停止', telegram_control.status_text(True, False, self.items))
        self.assertIn('未登入', telegram_control.status_text(False, False, self.items))

    def test_list_marks_mode_and_state(self):
        t = telegram_control.list_text(self.items)
        self.assertIn('1.', t)
        self.assertIn('實單', t)
        self.assertIn('模擬', t)
        self.assertEqual(telegram_control.list_text([]), '目前沒有任何策略')

    def test_help_covers_every_command(self):
        t = telegram_control.help_text()
        for cmd in telegram_control.COMMANDS:
            self.assertIn(cmd, t)
        self.assertIn('不提供下單', t)

    def test_confirm_prompt_states_what_will_happen(self):
        t = telegram_control.confirm_prompt('/on', '1', 'AB12', 'A', '2330 日K [實單] x1')
        self.assertIn('/yes AB12', t)
        self.assertIn('「A」', t)
        self.assertIn('實單', t)

    def test_positions_text_says_simulation_only(self):
        acct = paper_account.new_account(name='模擬一')
        paper_account.apply_fill(acct, '2026-07-26 09:05:00', '台股', '2330',
                                 '買進', 'OPEN', 1, 600.0)
        t = telegram_control.positions_text({acct['id']: acct})
        self.assertIn('2330', t)
        self.assertIn('實單庫存', t)      # 必須講明這裡看不到實單
        self.assertIn('無持倉', telegram_control.positions_text({}))

    def test_pnl_text_only_counts_the_given_day(self):
        """今日已實現只能算今天的:算進昨天的會讓使用者誤判當日風險。"""
        acct = paper_account.new_account(initial_cash=1000000.0, name='模擬一')
        paper_account.apply_fill(acct, '2026-07-25 09:05:00', '台股', '2330',
                                 '買進', 'OPEN', 1, 600.0)
        paper_account.apply_fill(acct, '2026-07-25 13:05:00', '台股', '2330',
                                 '賣出', 'CLOSE', 1, 610.0)      # 昨天賺
        paper_account.apply_fill(acct, '2026-07-26 09:05:00', '台股', '2317',
                                 '買進', 'OPEN', 1, 200.0)
        paper_account.apply_fill(acct, '2026-07-26 13:05:00', '台股', '2317',
                                 '賣出', 'CLOSE', 1, 190.0)      # 今天賠
        y = paper_account.realized_pnl_on(acct, '2026-07-25')
        t = paper_account.realized_pnl_on(acct, '2026-07-26')
        self.assertGreater(y, 0)
        self.assertLess(t, 0)
        self.assertAlmostEqual(y + t, acct['realized_pnl'], places=2)
        self.assertIn('2026-07-26', telegram_control.pnl_text({acct['id']: acct}, '2026-07-26'))

    def test_realized_pnl_on_survives_bad_records(self):
        """查詢損益不該有機會讓程式當掉。"""
        acct = {'history': [{'ts': None, 'pnl': 1}, {'ts': '2026-07-26', 'pnl': 'x'},
                            {'ts': '2026-07-26 10:00:00', 'pnl': 5.0}]}
        self.assertAlmostEqual(paper_account.realized_pnl_on(acct, '2026-07-26'), 5.0)
        self.assertEqual(paper_account.realized_pnl_on(acct, ''), 0.0)
        self.assertEqual(paper_account.realized_pnl_on(None, '2026-07-26'), 0.0)


class TestTelegramNotify(unittest.TestCase):
    """【新ADR】Telegram 通知純邏輯層:訊息判斷/組請求/解析回應,零網路呼叫。"""

    def test_is_quant_message_matches_prefix(self):
        self.assertTrue(telegram_notify.is_quant_message("【自動交易-模擬】策略「A」買進 1 2330 @ 600"))
        self.assertTrue(telegram_notify.is_quant_message("【自動交易-風控擋單】xxx"))
        self.assertFalse(telegram_notify.is_quant_message("【量化交易】已開啟獨立視窗"))
        self.assertFalse(telegram_notify.is_quant_message("隨便一段不相干的文字"))
        self.assertFalse(telegram_notify.is_quant_message(""))
        self.assertFalse(telegram_notify.is_quant_message(None))

    def test_config_ready(self):
        self.assertTrue(telegram_notify.config_ready({'bot_token': '123:ABC', 'chat_id': '999'}))
        self.assertFalse(telegram_notify.config_ready({'bot_token': '', 'chat_id': '999'}))
        self.assertFalse(telegram_notify.config_ready({'bot_token': '123:ABC', 'chat_id': ''}))
        self.assertFalse(telegram_notify.config_ready({}))
        self.assertFalse(telegram_notify.config_ready(None))

    def test_build_send_request_shape(self):
        req = telegram_notify.build_send_request('123:ABC', '999888', '策略成交測試')
        self.assertEqual(req['url'], 'https://api.telegram.org/bot123:ABC/sendMessage')
        self.assertIn(b'chat_id=999888', req['data'])
        self.assertIn('Content-Type', req['headers'])

    def test_build_send_request_rejects_blank_credentials(self):
        with self.assertRaises(ValueError):
            telegram_notify.build_send_request('', '999888', 'x')
        with self.assertRaises(ValueError):
            telegram_notify.build_send_request('123:ABC', '', 'x')

    def test_build_send_request_truncates_long_text(self):
        long_text = 'A' * 5000
        req = telegram_notify.build_send_request('123:ABC', '999888', long_text)
        # 解碼回來確認實際送出的文字沒有超過上限太多 (含截斷提示字樣)
        decoded = urllib.parse.parse_qs(req['data'].decode('utf-8'))['text'][0]
        self.assertLessEqual(len(decoded), telegram_notify.MAX_MESSAGE_LEN + 20)
        self.assertIn('截斷', decoded)

    def test_parse_response_ok(self):
        ok, msg = telegram_notify.parse_response(b'{"ok":true,"result":{"message_id":1}}')
        self.assertTrue(ok)

    def test_parse_response_failure_with_description(self):
        ok, msg = telegram_notify.parse_response('{"ok":false,"description":"Bad Request: chat not found"}')
        self.assertFalse(ok)
        self.assertIn('chat not found', msg)

    def test_parse_response_invalid_json(self):
        ok, msg = telegram_notify.parse_response('not json at all')
        self.assertFalse(ok)


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

    # ---------- 【ADR-138】新增的 key 也要真的存得起來 ----------

    def test_keys_added_after_adr056_survive_a_roundtrip(self):
        """原本 load/save 都只認 DEFAULT_INDICATOR_SETTINGS 列到的 key,於是
        ADR-131 (布林中線類型/第2組)、ADR-133 (黃金切割)、ADR-134 (KDJ 超買賣線
        /JAE) 的設定「存檔時被濾掉、讀檔時也被濾掉」—— 使用者每次重開都回到
        程式碼預設值。這條測試就是釘死這件事不可以再發生。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ind.json')
            d = config_store.load_indicator_settings(path)
            d.update({'bb_type': 'EMA', 'bb2_show': True, 'bb2_period': 60,
                      'bb2_std_up': 1.5, 'bb2_std_dn': 2.5,
                      'fib_show': True, 'fib_lookback': 77,
                      'fib_ratios': [0.382, 0.618],
                      'kd_ob': '85', 'var_jae': True,
                      'jae_params': {'rsi_p': 14, 'trend_p': 60}})
            config_store.save_indicator_settings(path, d)
            back = config_store.load_indicator_settings(path)
            for k in ('bb_type', 'bb2_show', 'bb2_period', 'bb2_std_up', 'bb2_std_dn',
                      'fib_show', 'fib_lookback', 'fib_ratios', 'kd_ob', 'var_jae',
                      'jae_params'):
                self.assertIn(k, back, f"{k} 存了卻讀不回來")
            self.assertEqual(back['bb_type'], 'EMA')
            self.assertEqual(back['fib_lookback'], 77)
            self.assertEqual(back['jae_params'], {'rsi_p': 14, 'trend_p': 60})

    def test_missing_keys_still_get_defaults(self):
        """反向對照:不能因為「什麼都保留」就把預設值機制弄丟。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ind.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'rsi_p': '21'}, f)
            back = config_store.load_indicator_settings(path)
            self.assertEqual(back['rsi_p'], '21')
            self.assertEqual(back['bb_period'], 20)      # 預設值仍在
            self.assertEqual(back['var_macd'], True)


class TestBollingerSigmaMigration(unittest.TestCase):
    """【ADR-138】布林 σ 由「內圈那對 / 外圈那對」改成「上線 σ / 下線 σ」,
    舊設定檔要能平順遷移 (使用者手上就有一份 bb_std1=2.0 / bb_std2=3.0)。"""

    def test_legacy_std1_becomes_both_sides(self):
        out = config_store.migrate_indicator_settings(
            {'bb_std1': 2.0, 'bb_std2': 3.0, 'bb2_std1': 1.5, 'bb2_std2': 0.0})
        self.assertEqual(out['bb_std_up'], 2.0)
        self.assertEqual(out['bb_std_dn'], 2.0)
        self.assertEqual(out['bb2_std_up'], 1.5)
        self.assertEqual(out['bb2_std_dn'], 1.5)

    def test_outer_sigma_is_discarded_not_used_as_lower(self):
        """關鍵斷言:舊的 σ2 (外圈) **不可以**被拿去當下線的 σ ——
        那會讓使用者看到一條他從沒設定過的歪斜通道 (上 2σ、下 3σ)。"""
        out = config_store.migrate_indicator_settings({'bb_std1': 2.0, 'bb_std2': 3.0})
        self.assertNotEqual(out['bb_std_dn'], 3.0)
        self.assertEqual(out['bb_std_dn'], out['bb_std_up'])

    def test_new_keys_win_and_migration_is_idempotent(self):
        src = {'bb_std1': 2.0, 'bb_std2': 3.0, 'bb_std_up': 1.0, 'bb_std_dn': 4.0}
        out = config_store.migrate_indicator_settings(src)
        self.assertEqual((out['bb_std_up'], out['bb_std_dn']), (1.0, 4.0))
        again = config_store.migrate_indicator_settings(out)
        self.assertEqual((again['bb_std_up'], again['bb_std_dn']), (1.0, 4.0))

    def test_bad_or_absent_legacy_leaves_it_to_the_defaults(self):
        for src in ({}, {'bb_std1': 'abc'}, {'bb_std1': 0}, {'bb_std1': -1}):
            out = config_store.migrate_indicator_settings(src)
            self.assertNotIn('bb_std_up', out, f"{src} 不該硬塞一個值進去")

    def test_does_not_mutate_the_input(self):
        src = {'bb_std1': 2.0}
        config_store.migrate_indicator_settings(src)
        self.assertEqual(src, {'bb_std1': 2.0})

    def test_load_applies_the_migration(self):
        """走完整讀檔路徑 —— 純函式對了但沒被呼叫是常見的空殼 (P-64)。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'old.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'bb_show': True, 'bb_period': 10,
                           'bb_std1': 1.5, 'bb_std2': 2.5}, f)
            d = config_store.load_indicator_settings(path)
            self.assertEqual(d['bb_std_up'], 1.5)
            self.assertEqual(d['bb_std_dn'], 1.5)
            self.assertEqual(d['bb_period'], 10)   # 其他欄位沒被弄壞


class TestOddLotADR146(unittest.TestCase):
    """【ADR-146】零股機制稽核:可下單時間 09:10 之後、只能限價、數量 1~999 股。

    使用者要求檢查這個機制有沒有問題。查出來的**真 bug**:策略的數量上限
    一律用 100,但零股的單位是「股」(1~999 是交易所規則)—— 零股策略最多
    只能買 100 股,而 500 股完全合法。
    """

    def _s(self, tt='零股', qty=1, **over):
        s = strategy_engine.new_strategy()
        s.update({'name': '零股測試', 'symbol': '2330', 'trade_type': tt, 'qty': qty,
                  'direction': '做多', 'stop_loss_pct': 2.0,
                  'entry': [{'type': 'always_true', 'params': {}}]})
        s.update(over)
        return s

    # ---------- 一、可下單時間:09:10 ~ 13:30 ----------

    def test_odd_lot_opens_at_0910_not_0900(self):
        """盤中零股 09:10 才開盤 —— 整股 09:00 就開了,兩者不同。"""
        wed = lambda hh, mm: datetime.datetime(2026, 8, 5, hh, mm)
        self.assertFalse(market_session.is_market_open('零股', dt=wed(9, 0)))
        self.assertFalse(market_session.is_market_open('零股', dt=wed(9, 9)),
                         "09:09 還沒開盤")
        self.assertTrue(market_session.is_market_open('零股', dt=wed(9, 10)),
                        "09:10 整要開盤 (邊界)")
        # 反向對照:整股 09:00 就開了,不可以被零股的規則影響
        self.assertTrue(market_session.is_market_open('股票', dt=wed(9, 0)))

    def test_odd_lot_closes_at_1330(self):
        wed = lambda hh, mm: datetime.datetime(2026, 8, 5, hh, mm)
        self.assertTrue(market_session.is_market_open('零股', dt=wed(13, 30)))
        self.assertFalse(market_session.is_market_open('零股', dt=wed(13, 31)))

    def test_odd_lot_closed_at_weekend(self):
        self.assertFalse(market_session.is_market_open(
            '零股', dt=datetime.datetime(2026, 8, 8, 10, 0)))

    def test_odd_lot_open_time_is_configurable(self):
        """【ADR-072】未來交易所改成 09:00 時要能自己切,不必改程式。"""
        old = market_session.ODD_LOT_OPEN_MIN
        try:
            market_session.set_odd_lot_open_hhmm('09:00')
            self.assertTrue(market_session.is_market_open(
                '零股', dt=datetime.datetime(2026, 8, 5, 9, 0)))
        finally:
            market_session.set_odd_lot_open_minute(old)
        self.assertFalse(market_session.is_market_open(
            '零股', dt=datetime.datetime(2026, 8, 5, 9, 0)), "還原後要回到 09:10")

    # ---------- 二、只能限價(鐵則 6) ----------

    def test_odd_lot_is_always_limit_even_if_the_strategy_says_market(self):
        for pt in ('市價', '範圍市價', '限價', '亂寫'):
            self.assertEqual(strategy_engine.price_type_of(self._s(price_type=pt)), '限價',
                             f"零股存了「{pt}」也必須退回限價")

    def test_saving_an_odd_lot_market_strategy_is_rejected(self):
        ok, why = strategy_engine.validate_strategy(self._s(price_type='市價'))
        self.assertFalse(ok)
        self.assertIn('限價', why)

    def test_built_order_is_limit_rod_odd(self):
        oi = order_intent.build_live_order(
            self._s(qty=500, slippage_ticks=0),
            {'kind': 'OPEN', 'action': '買進', 'qty': 500, 'price': 600.0}, 'stock')
        self.assertEqual(oi['price_type'], '限價')
        self.assertEqual(oi['order_lot'], '零股')
        self.assertEqual(oi['time_in_force'], order_intent.TIF_ROD)
        self.assertGreater(oi['price'], 0, "限價單的價格不可以送 0")

    # ---------- 三、數量 1~999 股(這次抓到的真 bug) ----------

    def test_odd_lot_quantity_uses_the_exchange_rule_not_the_lot_cap(self):
        """500 股是完全合法的零股委託,不可以被「100 張」那個防呆上限擋下。"""
        for q in (1, 100, 101, 500, 999):
            ok, why = strategy_engine.validate_strategy(self._s(qty=q))
            self.assertTrue(ok, f"零股 {q} 股應該存得起來,卻被擋:{why}")

    def test_odd_lot_over_999_is_rejected_with_the_right_reason(self):
        ok, why = strategy_engine.validate_strategy(self._s(qty=1000))
        self.assertFalse(ok)
        self.assertIn('999', why)
        self.assertIn('交易所', why, "這是交易所規則,訊息不可以說『請調整程式碼上限』")
        self.assertNotIn('程式碼', why)

    def test_lot_products_keep_the_conservative_cap(self):
        """反向對照:整股/期貨的 100 張/口 防呆上限不可以被放寬成 999。"""
        for tt in ('股票', '期貨'):
            s = self._s(tt=tt, qty=101)
            if tt == '期貨':
                s['market'] = '台期貨'
            ok, why = strategy_engine.validate_strategy(s)
            self.assertFalse(ok, f"{tt} 101 應該被擋")
            self.assertIn('100', why)
            self.assertIn('防呆', why, "這是本系統的上限,要跟交易所規則講清楚差別")
            self.assertTrue(strategy_engine.validate_strategy(self._s(tt=tt, qty=100))[0]
                            if tt == '股票' else True)

    def test_send_time_guard_blocks_illegal_quantity(self):
        """【鐵則 9】送出前在本地擋掉,不依賴券商回退單。

        存檔時已經驗過一次,這是第二道 —— 數量還可能經由舊策略檔的殘留值、
        手動改 JSON 等途徑變動,而這裡是委託離開系統的最後一個關口。
        """
        s = self._s(qty=1, slippage_ticks=0)
        for bad in (0, -1, 1000, 5000):
            with self.assertRaises(ValueError, msg=f"零股 {bad} 股竟然送得出去"):
                order_intent.build_live_order(
                    s, {'kind': 'OPEN', 'action': '買進', 'qty': bad, 'price': 600.0},
                    'stock')
        # 反向對照:合法數量必須送得出去 (否則等於把功能鎖死)
        for good in (1, 500, 999):
            oi = order_intent.build_live_order(
                s, {'kind': 'OPEN', 'action': '買進', 'qty': good, 'price': 600.0}, 'stock')
            self.assertEqual(oi['qty'], good)

    def test_quantity_limit_is_a_single_source(self):
        """上限只有一份 —— 策略路徑與手動下單對零股用的是同一個 999
        (原本就是因為兩份各自維護才出錯的,P-67)。"""
        cap, unit, is_rule = order_rules.strategy_qty_limit('零股')
        self.assertEqual(cap, order_rules.MAX_QTY_ODD)
        self.assertEqual(unit, '股')
        self.assertTrue(is_rule, "零股上限是交易所規則")
        cap2, unit2, is_rule2 = order_rules.strategy_qty_limit('股票')
        self.assertEqual(unit2, '張')
        self.assertFalse(is_rule2, "整股上限是本系統防呆,不是交易所規則")
        self.assertEqual(order_rules.strategy_qty_limit('期貨')[1], '口')

    # ---------- 四、零股不可做空 ----------

    def test_odd_lot_cannot_short(self):
        ok, why = strategy_engine.validate_strategy(self._s(direction='做空'))
        self.assertFalse(ok)
        self.assertIn('期貨', why)


class TestRealtimeEntryADR145(unittest.TestCase):
    """【ADR-145】即時進場:用即時報價判斷「當日漲跌幅」類進場條件,不等 K 棒收盤。

    使用者:「當A(比如:加權指數)盤中**即時**跌幅超過X%,就買進B。」
    """

    def _df(self):
        """昨收 100;今天已收盤的 K 棒最低 98.0(只跌 2%)。"""
        idx = pd.to_datetime(['2026-08-04 13:00', '2026-08-04 13:30',
                              '2026-08-05 09:00', '2026-08-05 09:05'])
        return pd.DataFrame({'Open': [100, 100, 99, 98.5],
                             'High': [100, 100, 99.5, 99.0],
                             'Low': [100, 100, 98.5, 98.0],
                             'Close': [100.0, 100.0, 98.5, 98.2],
                             'Volume': [1] * 4}, index=idx)

    def _s(self, **over):
        s = strategy_engine.new_strategy()
        s.update({'symbol': '2330', 'trade_type': '股票', 'qty': 1, 'direction': '做多',
                  'realtime_entry': True,
                  'entry': [{'type': 'day_drop_over', 'params': {'value': 3.0}}],
                  'exit_signals': []})
        s.update(over)
        return s

    # ---------- 開關 ----------

    def test_off_by_default(self):
        """會下單的功能不可以預設打開。"""
        self.assertFalse(strategy_engine.realtime_entry_enabled(
            strategy_engine.new_strategy()))
        self.assertFalse(strategy_engine.realtime_entry_enabled({}))
        self.assertFalse(strategy_engine.realtime_entry_enabled(None))
        self.assertTrue(strategy_engine.realtime_entry_enabled({'realtime_entry': True}))

    # ---------- 哪些條件算得出來 ----------

    def test_only_day_pct_conditions_are_live_evaluable(self):
        ok, _ = strategy_engine.live_entry_supported(self._s())
        self.assertTrue(ok)
        ok2, why2 = strategy_engine.live_entry_supported(
            self._s(entry=[{'type': 'day_drop_over', 'params': {'value': 3.0}},
                           {'type': 'ma_cross_up', 'params': {}}]))
        self.assertFalse(ok2, "只要有一條需要 K 棒序列就不能只憑即時價判斷")
        self.assertIn('均線', why2, "原因要點名是哪一條擋的")

    def test_no_entry_conditions_is_not_supported(self):
        ok, why = strategy_engine.live_entry_supported(self._s(entry=[]))
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_unsupported_conditions_never_fire(self):
        """反向對照:條件不支援時,即時路徑必須什麼都不做 ——
        不可以「忽略那條算不出來的條件」就進場。

        測試資料刻意讓**可即時的那一條成立**(即時價 96.5 → 跌 3.5% > 3%),
        所以只要不認得的條件被跳過,就會真的下單。第一版用的是 90.0,
        那個價位下另一條剛好也算不成立,於是拿掉守門照樣綠 —— 那是巧合,
        不是保護(突變測試抓到的)。
        """
        rt = strategy_engine.new_runtime()
        self.assertIsNotNone(strategy_engine.check_realtime_entry(
            self._s(), rt, self._df(), 96.5), "前提:單獨那一條在 96.5 是會成立的")
        for bad in ('ma_cross_up', 'kd_cross_up', 'always_true', '不存在的條件'):
            s = self._s(entry=[{'type': 'day_drop_over', 'params': {'value': 3.0}},
                               {'type': bad, 'params': {}}])
            self.assertIsNone(
                strategy_engine.check_realtime_entry(s, rt, self._df(), 96.5),
                f"混進 {bad} 之後就不可以即時進場")

        # 上面那組其實**證明力不夠**:不認得的條件被當成「漲幅條件」硬算時,
        # 在跌的那一天剛好也算出 False,所以拿掉守門照樣是 None(突變測試抓到)。
        # 這裡改用**漲的一天**:不認得的條件被硬算成漲幅時會算出 True,
        # 少了守門就會真的進場。
        up_df = self._df().copy()
        for col, vals in (('Open', [100, 101, 103]), ('High', [100, 102, 104.5]),
                          ('Low', [100, 100.5, 102.5]), ('Close', [100.0, 101.5, 104.0])):
            up_df.iloc[1:, up_df.columns.get_loc(col)] = vals
        s_up = self._s(entry=[{'type': 'day_rise_over', 'params': {'value': 3.0}}])
        self.assertIsNotNone(strategy_engine.check_realtime_entry(s_up, rt, up_df, 104.0),
                             "前提:漲幅條件單獨在這天是會成立的")
        for bad in ('ma_cross_up', 'always_true', '不存在的條件'):
            s_bad = self._s(entry=[{'type': 'day_rise_over', 'params': {'value': 3.0}},
                                   {'type': bad, 'params': {}}])
            self.assertIsNone(
                strategy_engine.check_realtime_entry(s_bad, rt, up_df, 104.0),
                f"混進 {bad} 之後就不可以即時進場(它需要 K 棒序列,即時價算不出來)")

    # ---------- 即時價的語意:K 棒模式的超集 ----------

    def test_live_price_fires_earlier_than_the_bar_would(self):
        """核心行為:已收盤 K 棒只跌 2%(不成立),但即時價已經跌 3.5% → 成立。"""
        df = self._df()
        self.assertFalse(strategy_engine._c_day_drop_over(df, {'value': 3.0}),
                         "前提:純 K 棒模式此刻不該成立")
        intent = strategy_engine.check_realtime_entry(
            self._s(), strategy_engine.new_runtime(), df, 96.5)
        self.assertIsNotNone(intent)
        self.assertEqual(intent['kind'], 'OPEN')
        self.assertEqual(intent['action'], '買進')
        self.assertEqual(intent['price'], 96.5)
        self.assertIn('即時', intent['reason'])

    def test_live_price_not_low_enough_does_not_fire(self):
        """反向對照:即時價沒跌夠就不可以進場。"""
        self.assertIsNone(strategy_engine.check_realtime_entry(
            self._s(), strategy_engine.new_runtime(), self._df(), 99.0))

    def test_live_is_a_superset_of_the_bar_model(self):
        """即時判斷必須是 K 棒判斷的**超集**:K 棒成立的,即時一定也成立。
        少了這個性質,回測與實盤就會出現互相矛盾的訊號。"""
        df = self._df()
        # 讓 K 棒本身就成立 (最低 96 → -4%)
        df2 = df.copy()
        df2.iloc[-1, df2.columns.get_loc('Low')] = 96.0
        self.assertTrue(strategy_engine._c_day_drop_over(df2, {'value': 3.0}))
        # 即時價比較高(還沒跌那麼多)也照樣成立 —— 因為當日極值已經跌破了
        self.assertIsNotNone(strategy_engine.check_realtime_entry(
            self._s(), strategy_engine.new_runtime(), df2, 99.0))

    def test_day_pct_change_folds_live_price_into_the_extreme(self):
        df = self._df()
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(df, '昨收', '盤中觸價', 'down'), -2.0)
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(df, '昨收', '盤中觸價', 'down',
                                           live_price=96.5), -3.5)
        # 即時價比當日最低高 → 極值不變 (只會更早,不會更寬鬆)
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(df, '昨收', '盤中觸價', 'down',
                                           live_price=99.9), -2.0)

    # ---------- 方向與加碼 ----------

    def test_short_direction_enters_with_sell(self):
        intent = strategy_engine.check_realtime_entry(
            self._s(direction='做空'), strategy_engine.new_runtime(), self._df(), 96.5)
        self.assertEqual(intent['action'], '賣出')

    def test_respects_max_entries(self):
        """已持倉且分批上限是 1 → 即時路徑也不可以再進場 (與 K 棒路徑同一份規則)。"""
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 1, 'entries': 1})
        self.assertIsNone(strategy_engine.check_realtime_entry(
            self._s(), rt, self._df(), 96.5))
        # 調大就可以加碼,而且方向跟現有部位一致
        intent = strategy_engine.check_realtime_entry(
            self._s(max_entries=3), rt, self._df(), 96.5)
        self.assertIsNotNone(intent)
        self.assertEqual(intent['action'], '買進')
        self.assertIn('加碼', intent['reason'])

    def test_short_position_scale_in_is_still_a_sell(self):
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'SHORT', 'qty': 1, 'entries': 1})
        intent = strategy_engine.check_realtime_entry(
            self._s(max_entries=3, direction='做空'), rt, self._df(), 96.5)
        self.assertEqual(intent['action'], '賣出', "加碼不可以變成反手")

    # ---------- 壞輸入 ----------

    def test_bad_inputs_return_none_not_raise(self):
        s, rt = self._s(), strategy_engine.new_runtime()
        self.assertIsNone(strategy_engine.check_realtime_entry(s, rt, self._df(), None))
        self.assertIsNone(strategy_engine.check_realtime_entry(s, rt, None, 96.5))
        self.assertIsNone(strategy_engine.check_realtime_entry(
            s, rt, self._df().iloc[:1], 96.5))
        # 只有當天的資料 → 算不出昨收 → 不進場 (不可以亂猜一個基準)
        only_today = self._df().iloc[2:]
        self.assertIsNone(strategy_engine.check_realtime_entry(s, rt, only_today, 50.0))

    def test_bad_threshold_does_not_fire(self):
        s = self._s(entry=[{'type': 'day_drop_over', 'params': {'value': 'abc'}}])
        self.assertIsNone(strategy_engine.check_realtime_entry(
            s, strategy_engine.new_runtime(), self._df(), 50.0))


class TestScaleInADR144(unittest.TestCase):
    """【ADR-144】分批進場 / 加碼 —— 拆掉「買了就不准再買」的限制。

    使用者的原話:
      「回測引擎非常『死腦筋』…只要你買了(狀態變成 LONG),在你把股票賣掉
        之前,我就絕對不准你買第二筆…造成回測時一年只買一次的元兇。」
    """

    def _df(self, n=30, flat=True):
        idx = pd.date_range('2026-01-01', periods=n, freq='D')
        c = [100.0] * n if flat else [100.0 + i for i in range(n)]
        return pd.DataFrame({'Open': c, 'High': [x + 1 for x in c],
                             'Low': [x - 1 for x in c], 'Close': c,
                             'Volume': [1000] * n}, index=idx)

    def _strategy(self, **over):
        s = strategy_engine.new_strategy()
        s.update({'symbol': '2330', 'trade_type': '股票', 'qty': 1, 'timeframe': '日K',
                  'direction': '做多', 'stop_loss_pct': 0.0, 'take_profit_pct': 0.0,
                  'entry': [{'type': 'always_true', 'params': {}}],
                  'exit_signals': []})
        s.update(over)
        return s

    # ---------- max_entries 的語意 ----------

    def test_default_is_one_so_existing_strategies_behave_exactly_as_before(self):
        """預設 1 = 舊行為。這是整個 ADR 最重要的相容性保證。"""
        self.assertEqual(strategy_engine.max_entries_of(strategy_engine.new_strategy()), 1)
        self.assertEqual(strategy_engine.max_entries_of({}), 1)
        self.assertEqual(strategy_engine.max_entries_of(None), 1)

    def test_zero_or_negative_means_unlimited(self):
        for v in (0, -1, -99):
            self.assertEqual(strategy_engine.max_entries_of({'max_entries': v}),
                             strategy_engine.UNLIMITED_ENTRIES, f"{v} 應視為不限")

    def test_garbage_falls_back_to_one_not_unlimited(self):
        """壞值一律退回 1(最保守),不可以退回「不限」——
        那會讓一個打錯字的欄位變成無限加碼。"""
        for v in ('abc', None, '', [], {}):
            self.assertEqual(strategy_engine.max_entries_of({'max_entries': v}), 1,
                             f"{v!r} 應退回 1")

    def test_buy_and_hold_accumulate_is_unlimited_by_definition(self):
        s = {'buy_and_hold': True, 'bnh_mode': 'accumulate', 'max_entries': 1}
        self.assertEqual(strategy_engine.max_entries_of(s),
                         strategy_engine.UNLIMITED_ENTRIES)
        # 但「單筆長抱」不是累積,仍受 max_entries 管
        self.assertEqual(
            strategy_engine.max_entries_of({'buy_and_hold': True, 'bnh_mode': 'single'}), 1)

    def test_entries_of_handles_old_runtimes_without_migration(self):
        """舊 runtime 沒有 'entries':有部位算 1、沒部位算 0。
        少了這條,既有策略重啟後會突然可以多買一筆。"""
        self.assertEqual(strategy_engine.entries_of({'qty': 5}), 1)
        self.assertEqual(strategy_engine.entries_of({'qty': 0}), 0)
        self.assertEqual(strategy_engine.entries_of({}), 0)
        self.assertEqual(strategy_engine.entries_of({'entries': 3, 'qty': 3}), 3)

    # ---------- can_scale_in 的守門 + 原因 ----------

    def test_flat_is_always_allowed(self):
        ok, why = strategy_engine.can_scale_in({'max_entries': 1}, {'state': 'FLAT'})
        self.assertTrue(ok)
        self.assertEqual(why, '')

    def test_blocked_reason_names_the_setting_the_user_has_to_change(self):
        """使用者看到「一年只買一次」時最需要的就是這句話 ——
        訊息裡必須點名是哪個設定擋的,不能只說「不行」。"""
        ok, why = strategy_engine.can_scale_in(
            {'max_entries': 1}, {'state': 'LONG', 'qty': 1, 'entries': 1})
        self.assertFalse(ok)
        self.assertIn('可分批進場次數', why)
        ok2, why2 = strategy_engine.can_scale_in(
            {'max_entries': 3}, {'state': 'LONG', 'qty': 3, 'entries': 3})
        self.assertFalse(ok2)
        self.assertIn('3', why2)
        # 還沒到上限就要放行 (反向對照:否則等於功能沒開)
        ok3, _ = strategy_engine.can_scale_in(
            {'max_entries': 3}, {'state': 'LONG', 'qty': 2, 'entries': 2})
        self.assertTrue(ok3)

    # ---------- evaluate_strategy:持倉中要不要再進場 ----------

    def _run(self, s, bars=6):
        rt = strategy_engine.new_runtime()
        df = self._df()
        opens = 0
        for i in range(bars):
            rt['last_bar_ts'] = ''
            for it in strategy_engine.evaluate_strategy(s, rt, df.iloc[:5 + i],
                                                        float(i), '2026-01-01'):
                if it['kind'] == 'OPEN':
                    opens += 1
                strategy_engine.apply_fill(s, rt, it, float(i))
        return opens, rt

    def test_default_still_buys_only_once(self):
        """反向對照:預設值下行為必須跟以前一模一樣(只買一次)。"""
        opens, rt = self._run(self._strategy())
        self.assertEqual(opens, 1)
        self.assertEqual(rt['qty'], 1)

    def test_max_entries_three_buys_three_times_then_stops(self):
        opens, rt = self._run(self._strategy(max_entries=3))
        self.assertEqual(opens, 3, "設 3 就該買到 3 筆")
        self.assertEqual(rt['qty'], 3)
        self.assertEqual(rt['entries'], 3)

    def test_blocked_reason_is_recorded_for_the_log_tab(self):
        """引擎要把「為什麼沒買」留下來,回測的訊息紀錄分頁才印得出來。"""
        _opens, rt = self._run(self._strategy())
        self.assertTrue(rt.get('blocked_entry'), "條件成立卻沒買,必須留下原因")
        self.assertIn('平倉', rt['blocked_entry'])

    def test_scale_in_never_reverses_the_position(self):
        """加碼的方向必須跟現有部位一致 —— 這裡是加碼不是反手。
        接錯的話,使用者在完全沒設定反手的情況下會被反向開倉。"""
        s = self._strategy(max_entries=3, direction='做空')
        rt = strategy_engine.new_runtime()
        df = self._df()
        actions = []
        for i in range(4):
            rt['last_bar_ts'] = ''
            for it in strategy_engine.evaluate_strategy(s, rt, df.iloc[:5 + i],
                                                        float(i), '2026-01-01'):
                actions.append(it['action'])
                strategy_engine.apply_fill(s, rt, it, float(i))
        self.assertEqual(actions, ['賣出'] * 3, f"做空的加碼要一律是賣出,實際 {actions}")
        self.assertEqual(rt['state'], 'SHORT')

    def test_exit_wins_over_scale_in_on_the_same_bar(self):
        """同一根 K 棒上不可以一邊喊出場一邊加碼。"""
        s = self._strategy(max_entries=5,
                           exit_signals=[{'type': 'always_true', 'params': {}}])
        rt = strategy_engine.new_runtime()
        df = self._df()
        rt['last_bar_ts'] = ''
        strategy_engine.apply_fill(s, rt, strategy_engine.evaluate_strategy(
            s, rt, df.iloc[:5], 0.0, '2026-01-01')[0], 0.0)
        self.assertEqual(rt['state'], 'LONG')
        rt['last_bar_ts'] = ''
        ints = strategy_engine.evaluate_strategy(s, rt, df.iloc[:6], 1.0, '2026-01-01')
        self.assertEqual([i['kind'] for i in ints], ['CLOSE'],
                         f"出場那一根不可以同時加碼,實際 {ints}")

    def test_stop_loss_uses_average_cost_after_scaling_in(self):
        """加碼後停損要用**加權平均成本**判斷,不是最後一筆的價格。"""
        s = self._strategy(max_entries=2, stop_loss_pct=5.0)
        rt = strategy_engine.new_runtime()
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 1, 'price': 100.0}, 1.0)
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 1, 'price': 200.0}, 2.0)
        self.assertAlmostEqual(rt['entry_price'], 150.0)
        # 均價 150 的 5% 停損 = 142.5
        self.assertIsNone(strategy_engine.check_bar_stop(s, rt, 143.0))
        self.assertIsNotNone(strategy_engine.check_bar_stop(s, rt, 142.0))

    # ---------- 回測 ----------

    def _rise_then_drop(self, n=30, drop_at=12):
        """先漲一段(進場+加碼),再一根大跌(觸發出場訊號)。"""
        idx = pd.date_range('2026-01-01', periods=n, freq='D')
        c = []
        for i in range(n):
            c.append(100.0 + i if i < drop_at else 60.0)
        return pd.DataFrame({'Open': c, 'High': [x + 1 for x in c],
                             'Low': [x - 1 for x in c], 'Close': c,
                             'Volume': [1000] * n}, index=idx)

    def test_backtest_produces_one_trade_row_per_lot_on_settlement(self):
        """期末結算路徑:每一筆 lot 各一列。"""
        s = self._strategy(max_entries=3,
                           exit_signals=[{'type': 'pct_change_below',
                                          'params': {'value': -99}}])
        r = backtest.run_backtest(s, self._df(n=40, flat=False))
        self.assertEqual(len(r['trades']), 3,
                         "分 3 筆進場就該有 3 列交易明細 (使用者要看到買在哪幾個價位)")
        self.assertEqual(len({t['entry_price'] for t in r['trades']}), 3,
                         "三筆的進場價應各不相同")

    def test_backtest_closes_every_lot_on_an_exit_signal(self):
        """**出場訊號**路徑也要平掉每一筆 lot,不是只平第一筆。

        這條與上面那條走的是完全不同的程式碼(CLOSE 分支 vs 期末結算)。
        第一版只有期末結算那條,於是「平倉只結算第一筆」的突變照樣全綠 ——
        因為那個測試的部位根本不是被出場訊號平掉的(P-124)。
        """
        s = self._strategy(max_entries=3,
                           exit_signals=[{'type': 'pct_change_below',
                                          'params': {'value': -20.0}}])
        r = backtest.run_backtest(s, self._rise_then_drop())
        closed = [t for t in r['trades'] if '出場訊號' in str(t.get('exit_reason', ''))]
        self.assertEqual(len(closed), 3,
                         f"3 筆 lot 都要被出場訊號平掉,實際 {len(closed)} 筆 "
                         f"({[t.get('exit_reason') for t in r['trades']]})")
        self.assertEqual(len({t['entry_price'] for t in closed}), 3,
                         "三筆的進場價應各不相同 (證明不是同一筆被記三次)")
        # 平倉後才可以再進場 —— 部位真的空了
        self.assertTrue(any(x['kind'] == '出場' for x in r['log']))

    def test_backtest_default_unchanged(self):
        """反向對照:預設值下回測結果必須跟加這個功能之前一樣。"""
        r = backtest.run_backtest(self._strategy(), self._df(n=40, flat=False))
        self.assertEqual(len(r['trades']), 1)

    def test_backtest_log_explains_why_nothing_happened(self):
        """使用者要的「訊息 / 紀錄」分頁,核心價值就是這一條。"""
        r = backtest.run_backtest(self._strategy(), self._df(n=40, flat=False))
        kinds = [x['kind'] for x in r['log']]
        self.assertIn('進場', kinds)
        self.assertIn('未進場', kinds)
        blocked = [x for x in r['log'] if x['kind'] == '未進場']
        self.assertTrue(blocked)
        self.assertIn('可分批進場次數', blocked[0]['text'])

    def test_log_collapses_consecutive_identical_reasons(self):
        """同一個原因連續幾百根,要合併成一列並標明次數 ——
        逐根印出來會有幾萬列,使用者反而看不到重點。"""
        r = backtest.run_backtest(self._strategy(), self._df(n=60, flat=False))
        blocked = [x for x in r['log'] if x['kind'] == '未進場']
        self.assertEqual(len(blocked), 1, "連續相同的原因要合併成一列")
        self.assertGreater(blocked[0]['n'], 5)
        self.assertIn('連續', blocked[0]['text'])

    def test_log_is_bounded(self):
        self.assertLessEqual(backtest.LOG_MAX_ROWS, 20000)
        r = backtest.run_backtest(self._strategy(max_entries=0),
                                  self._df(n=200, flat=False))
        self.assertLessEqual(len(r['log']), backtest.LOG_MAX_ROWS + 1)

    def test_empty_result_has_log_key(self):
        """報告視窗會讀 result['log'],沒有這個 key 會 KeyError。"""
        self.assertIn('log', backtest._empty_result())
        r = backtest.run_backtest(self._strategy(), self._df(n=2))
        self.assertIn('log', r)

    def test_backtest_with_no_trades_says_why(self):
        """一筆交易都沒有時,紀錄要回答「為什麼」,不是空白分頁。"""
        s = self._strategy(entry=[{'type': 'pct_change_above',
                                   'params': {'value': 9999}}])
        r = backtest.run_backtest(s, self._df(n=40, flat=False))
        self.assertEqual(len(r['trades']), 0)
        self.assertTrue(r['log'], "沒有交易時更需要訊息紀錄")
        self.assertTrue(any('沒有' in x['text'] for x in r['log']))


class TestDayPctChangeConditions(unittest.TestCase):
    """【ADR-143】當日(盤中)漲跌幅條件:「A 跌超過 X% 就買 B、漲超過 Y% 就賣 B」。

    使用者的原話:
      「一樣看A做B,當A盤中跌幅超過X%,就買進B。(支援股票整張、零股、期貨)
        X為參數,可自行輸入。當A盤中漲幅超過Y%,就賣出B。Y為參數,可自行輸入。」
    """

    def _df(self, rows):
        idx = pd.to_datetime([r[0] for r in rows])
        return pd.DataFrame(
            {'Open': [r[1] for r in rows], 'High': [r[2] for r in rows],
             'Low': [r[3] for r in rows], 'Close': [r[4] for r in rows],
             'Volume': [1000] * len(rows)}, index=idx)

    def _intraday(self):
        """昨收 100;今天最低 96.5(-3.5%)、最高 101.5(+1.5%)、收盤 98(-2%)。"""
        return self._df([
            ('2026-08-04 13:00', 100, 100, 100, 100.0),
            ('2026-08-04 13:30', 100, 100, 100, 100.0),   # 昨收 = 這一根
            ('2026-08-05 09:00', 99, 101.5, 97.0, 98.5),
            ('2026-08-05 09:05', 98.5, 99.0, 96.5, 98.0),
        ])

    # ---------- 基準價 ----------

    def test_reference_is_previous_trading_days_last_close(self):
        """昨收 = **前一交易日最後一根**的收盤,不是「前一根」——
        在 5分K 上「前一根」是 5 分鐘前的價格,那不是漲跌幅。"""
        self.assertEqual(strategy_engine._day_ref_price(self._intraday(), '昨收'), 100.0)

    def test_reference_today_open(self):
        self.assertEqual(strategy_engine._day_ref_price(self._intraday(), '今開'), 99.0)

    def test_no_previous_day_returns_none_not_a_made_up_number(self):
        """只有當天的資料 → 算不出昨收就回 None,條件不成立。
        絕不可以退而求其次拿前一根收盤充數(那會算出一個看似合理的錯數字)。"""
        only_today = self._df([('2026-08-05 09:00', 99, 100, 97, 98.5),
                               ('2026-08-05 09:05', 98.5, 99, 96.5, 98.0)])
        self.assertIsNone(strategy_engine._day_ref_price(only_today, '昨收'))
        self.assertFalse(strategy_engine._c_day_drop_over(only_today, {'value': 1.0}))
        self.assertFalse(strategy_engine._c_day_rise_over(only_today, {'value': 1.0}))

    # ---------- 觸價 vs 收盤價 ----------

    def test_touch_uses_the_days_extreme_close_uses_the_bar(self):
        df = self._intraday()
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(df, '昨收', '盤中觸價', 'down'), -3.5)
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(df, '昨收', '收盤價', 'down'), -2.0)
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(df, '昨收', '盤中觸價', 'up'), 1.5)

    def test_touch_scans_the_whole_day_not_just_the_last_bar(self):
        """最低點出現在**今天稍早**那一根時也要算得到 ——
        只看最後一根的話,盤中曾經跌 3.5% 這件事會整個漏掉。"""
        df = self._df([
            ('2026-08-04 13:30', 100, 100, 100, 100.0),
            ('2026-08-05 09:00', 99, 99.5, 96.0, 97.0),   # 最低在這一根 (-4%)
            ('2026-08-05 09:05', 97, 99.0, 98.5, 99.0),   # 已經拉回
        ])
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(df, '昨收', '盤中觸價', 'down'), -4.0)
        self.assertTrue(strategy_engine._c_day_drop_over(df, {'value': 3.0}))
        # 反向對照:改用收盤價就不該成立 (收在 -1%)
        self.assertFalse(
            strategy_engine._c_day_drop_over(df, {'value': 3.0, 'use': '收盤價'}))

    def test_yesterdays_extreme_is_not_counted(self):
        """昨天跌很多不算今天的 —— 少了這條,「當日」兩個字就沒有意義。"""
        df = self._df([
            ('2026-08-04 09:00', 100, 100, 80.0, 100.0),   # 昨天最低 80
            ('2026-08-04 13:30', 100, 100, 100, 100.0),
            ('2026-08-05 09:00', 100, 100.5, 99.5, 100.0),  # 今天幾乎沒動
        ])
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(df, '昨收', '盤中觸價', 'down'), -0.5)
        self.assertFalse(strategy_engine._c_day_drop_over(df, {'value': 3.0}))

    # ---------- X / Y 門檻 ----------

    def test_threshold_boundary_and_sign(self):
        df = self._intraday()      # 當日觸價 -3.5%
        self.assertTrue(strategy_engine._c_day_drop_over(df, {'value': 3.5}), "剛好等於門檻要成立")
        self.assertTrue(strategy_engine._c_day_drop_over(df, {'value': 3.0}))
        self.assertFalse(strategy_engine._c_day_drop_over(df, {'value': 3.6}))

    def test_threshold_takes_absolute_value(self):
        """使用者說「跌幅超過 X%」,X 在他腦中就是正數。既有的
        pct_change_below 要求填負數是個已知會踩的坑,這裡兩種寫法一律當成
        同一件事。

        關鍵是**門檻沒到的時候也要一致**——只比對「都成立」的情況是空殼:
        少了 abs(),填 -3 會變成 `chg <= 3`,那等於「只要不是漲超過 3% 就成立」,
        任何一天都會觸發,而使用者完全不會發現(突變測試抓到的)。"""
        big = self._intraday()          # 當日觸價 -3.5%
        self.assertTrue(strategy_engine._c_day_drop_over(big, {'value': 3.0}))
        self.assertTrue(strategy_engine._c_day_drop_over(big, {'value': -3.0}))

        mild = self._df([               # 當日只跌 1%
            ('2026-08-04 13:30', 100, 100, 100, 100.0),
            ('2026-08-05 09:00', 100, 100.2, 99.0, 99.5),
        ])
        self.assertFalse(strategy_engine._c_day_drop_over(mild, {'value': 3.0}))
        self.assertFalse(strategy_engine._c_day_drop_over(mild, {'value': -3.0}),
                         "填負數不可以讓門檻失效變成『幾乎永遠成立』")

        mild_up = self._df([            # 當日只漲 1%
            ('2026-08-04 13:30', 100, 100, 100, 100.0),
            ('2026-08-05 09:00', 100, 101.0, 99.8, 100.5),
        ])
        self.assertFalse(strategy_engine._c_day_rise_over(mild_up, {'value': 3.0}))
        self.assertFalse(strategy_engine._c_day_rise_over(mild_up, {'value': -3.0}),
                         "漲幅條件同理")

    def test_rise_condition_mirrors_the_drop_one(self):
        up = self._df([
            ('2026-08-04 13:30', 100, 100, 100, 100.0),
            ('2026-08-05 09:00', 101, 104.0, 100.5, 103.0),
        ])
        self.assertTrue(strategy_engine._c_day_rise_over(up, {'value': 4.0}))
        self.assertFalse(strategy_engine._c_day_rise_over(up, {'value': 4.1}))
        # 漲的一天不該觸發「跌幅」條件 (反向對照:兩個條件不可以接反)
        self.assertFalse(strategy_engine._c_day_drop_over(up, {'value': 1.0}))

    # ---------- 日K 上要自然退化 ----------

    def test_daily_bars_degrade_to_previous_close(self):
        """日K 的「前一根」就是前一個交易日,不必特別處理就該正確。"""
        daily = self._df([('2026-08-03', 100, 100, 100, 100.0),
                          ('2026-08-04', 100, 101, 95.0, 96.0)])
        self.assertEqual(strategy_engine._day_ref_price(daily, '昨收'), 100.0)
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(daily, '昨收', '盤中觸價', 'down'), -5.0)
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(daily, '昨收', '收盤價', 'down'), -4.0)

    # ---------- 期貨夜盤:交易日不是自然日 ----------

    def test_futures_night_session_belongs_to_the_next_trading_day(self):
        """【ADR-007 的規則】15:00 之後的夜盤算下一個交易日。
        用自然日分組的話「當日」會從半夜被切成兩半,跌幅整個算錯。"""
        fut = self._df([
            ('2026-08-04 13:45', 20000, 20000, 20000, 20000.0),   # 交易日 8/4 收盤
            ('2026-08-04 15:00', 20000, 20010, 19900, 19950.0),   # 夜盤 → 交易日 8/5
            ('2026-08-05 09:00', 19950, 19960, 19300, 19400.0),   # 日盤 → 交易日 8/5
        ])
        self.assertEqual(strategy_engine._day_ref_price(fut, '昨收'), 20000.0)
        self.assertAlmostEqual(
            strategy_engine.day_pct_change(fut, '昨收', '盤中觸價', 'down'),
            (19300 / 20000 - 1) * 100)

    def test_stock_intraday_is_not_treated_as_having_a_night_session(self):
        """反向對照:股票分K(09:00~13:30)不可以被誤判成含夜盤,
        否則交易日規則會把資料整個錯位。"""
        idx = pd.to_datetime(['2026-08-05 09:00', '2026-08-05 13:30'])
        self.assertFalse(futures_session.has_night_session_bars(idx))
        # 日K 的索引都是午夜,午夜落在「早於 08:45」裡 —— 少了那道判斷會誤判
        self.assertFalse(futures_session.has_night_session_bars(
            pd.to_datetime(['2026-08-04', '2026-08-05'])))
        # 真的有夜盤才回 True
        self.assertTrue(futures_session.has_night_session_bars(
            pd.to_datetime(['2026-08-04 15:00', '2026-08-05 09:00'])))

    def test_session_date_of_matches_the_vectorised_rule(self):
        """單根版與 resample_future_session 的向量版必須是同一條規則(P-67)。"""
        self.assertEqual(futures_session.session_date_of('2026-08-04 13:45'),
                         datetime.date(2026, 8, 4))
        self.assertEqual(futures_session.session_date_of('2026-08-04 13:46'),
                         datetime.date(2026, 8, 5))
        self.assertEqual(futures_session.session_date_of('2026-08-05 03:00'),
                         datetime.date(2026, 8, 5))

    # ---------- 註冊到條件庫 (GUI 才選得到) ----------

    def test_registered_in_the_condition_library(self):
        for key in ('day_drop_over', 'day_rise_over'):
            self.assertIn(key, strategy_engine.CONDITIONS, f"{key} 沒有註冊,GUI 選不到")
            name, spec, fn = strategy_engine.CONDITIONS[key]
            self.assertTrue(name)
            keys = [strategy_engine.spec_parts(x)[0] for x in spec]
            self.assertEqual(keys, ['value', 'base', 'use'])
            # 基準與用價都要是下拉選單 (自由輸入會打錯字,打錯就靜默退回預設)
            for item in spec[1:]:
                self.assertIsNotNone(strategy_engine.spec_parts(item)[3],
                                     "參考價/用哪個價要是下拉選單")

    def test_condition_label_is_readable(self):
        lab = strategy_engine.condition_label(
            {'type': 'day_drop_over', 'params': {'value': 3.0}})
        self.assertIn('跌幅', lab)
        self.assertIn('3.0', lab)

    def test_bad_params_do_not_blow_up_the_whole_evaluation(self):
        """一條條件寫壞只能讓那一條變 False (ADR-065),不可以連累其他條件。"""
        df = self._intraday()
        ok, results = strategy_engine.eval_conditions(
            df, [{'type': 'day_drop_over', 'params': {'value': 'abc'}},
                 {'type': 'day_drop_over', 'params': {'value': 3.0}}], 'OR')
        self.assertTrue(ok, "第二條是好的,整組 OR 要成立")
        self.assertFalse(results[0][1])
        self.assertTrue(results[1][1])

    def test_unknown_base_or_use_falls_back_instead_of_crashing(self):
        df = self._intraday()
        # 認不得的基準 → 走「昨收」那條路 (base 只有 '今開' 是特例)
        self.assertTrue(strategy_engine._c_day_drop_over(
            df, {'value': 3.0, 'base': '亂寫', 'use': '亂寫'}))


class TestApiTestRules(unittest.TestCase):
    """【ADR-139】永豐 API 測試(模擬環境的登入/下單測試)的純規則。

    規格出處:https://sinotrade.github.io/zh/tutor/prepare/terms/
    這裡測的每一條都對應官方文件上的一句話,不是我自己發明的限制。
    """

    # ---------- 版本下限:官方「版本 >= 1.2」 ----------

    def test_parse_version(self):
        self.assertEqual(api_test.parse_version('1.7.0'), (1, 7))
        self.assertEqual(api_test.parse_version('v1.2'), (1, 2))
        self.assertEqual(api_test.parse_version('1'), (1, 0))
        self.assertEqual(api_test.parse_version('1.2.0b1'), (1, 2))
        for bad in ('', None, 'abc', 'x.y'):
            self.assertIsNone(api_test.parse_version(bad), f"{bad!r} 不該解析成功")

    def test_version_ok_boundary(self):
        self.assertTrue(api_test.version_ok('1.2')[0], "1.2 是下限,要放行")
        self.assertTrue(api_test.version_ok('1.7.0')[0])
        self.assertFalse(api_test.version_ok('1.1.9')[0])
        self.assertFalse(api_test.version_ok('0.9')[0])

    def test_unknown_version_is_rejected_not_waved_through(self):
        """讀不出版本時當作不符合。放行未知等於把判斷丟回給使用者從錯誤訊息猜。"""
        ok, why = api_test.version_ok('???')
        self.assertFalse(ok)
        self.assertIn('1.2', why, "要講清楚官方要求的版本")

    # ---------- 測試時段:週一~五 08:00~20:00,18:00 後僅限台灣 IP ----------

    def _at(self, y, m, d, hh, mm):
        return datetime.datetime(y, m, d, hh, mm)

    def test_window_weekday_inside(self):
        # 2026-08-03 是星期一
        ok, why, tw = api_test.window_status(self._at(2026, 8, 3, 10, 0))
        self.assertTrue(ok)
        self.assertFalse(tw, "10:00 沒有地域限制")

    def test_window_boundaries(self):
        mon = lambda hh, mm: api_test.window_status(self._at(2026, 8, 3, hh, mm))
        self.assertFalse(mon(7, 59)[0], "07:59 還沒開放")
        self.assertTrue(mon(8, 0)[0], "08:00 整要放行(邊界)")
        self.assertTrue(mon(19, 59)[0], "19:59 還在時段內")
        self.assertFalse(mon(20, 0)[0], "20:00 整已結束(邊界)")

    def test_window_tw_ip_only_flag(self):
        """18:00 之後僅限台灣 IP。程式判斷不出使用者的 IP 在哪,
        所以要誠實把這件事回報成一個旗標讓呼叫端提醒,不可以自己猜。"""
        self.assertFalse(api_test.window_status(self._at(2026, 8, 3, 17, 59))[2])
        self.assertTrue(api_test.window_status(self._at(2026, 8, 3, 18, 0))[2])
        # 但仍然是「可以測」——僅限台灣 IP 不等於不能測
        self.assertTrue(api_test.window_status(self._at(2026, 8, 3, 18, 30))[0])

    def test_window_closed_at_weekend(self):
        # 2026-08-01 星期六 / 2026-08-02 星期日
        for d in (1, 2):
            ok, why, _ = api_test.window_status(self._at(2026, 8, d, 10, 0))
            self.assertFalse(ok, f"8/{d} 是週末,不該放行")
            self.assertIn('星期', why)

    # ---------- 間隔:官方「需間隔1秒以上」 ----------

    def test_order_interval_is_above_one_second(self):
        """官方說的是「1 秒**以上**」,取剛好 1.0 會落在邊界上。
        這條測試釘住「不可以為了跑快一點把它調到 1 秒或更短」。"""
        self.assertGreater(api_test.ORDER_INTERVAL_SEC, 1.0)

    # ---------- 欄位驗證(鐵則9:不靠券商回錯誤才知道) ----------

    def test_validate_order_accepts_the_official_examples(self):
        """反向對照:官方文件自己的範例必須驗得過,否則等於把功能鎖死。"""
        ok, why = api_test.validate_order('證券', '2890', 28, 1)
        self.assertTrue(ok, why)
        ok, why = api_test.validate_order('期貨', 'TXF', 37000, 1)
        self.assertTrue(ok, why)

    def test_validate_order_rejects_bad_fields(self):
        cases = [
            ('證券', '', 28, 1),          # 空代碼
            ('證券', '289', 28, 1),       # 證券代碼不是 4 位
            ('證券', '2890', 0, 1),       # 價格 0
            ('證券', '2890', -1, 1),      # 負價
            ('證券', '2890', 'abc', 1),   # 價格不是數字
            ('證券', '2890', 28, 0),      # 數量 0
            ('證券', '2890', 28, 'x'),    # 數量不是數字
            ('股票', '2890', 28, 1),      # 不認得的種類
        ]
        for kind, code, px, qty in cases:
            ok, why = api_test.validate_order(kind, code, px, qty)
            self.assertFalse(ok, f"{(kind, code, px, qty)} 應該被擋下")
            self.assertTrue(why, "被擋下就要講原因")

    def test_quantity_capped_at_one(self):
        """測試單刻意把數量上限壓死在 1。這是測試不是交易,量大只會讓
        萬一環境接錯時的後果變嚴重。"""
        ok, why = api_test.validate_order('證券', '2890', 28, 2)
        self.assertFalse(ok)
        self.assertIn('1', why)

    # ---------- 最近月合約:不可以照抄官方範例的固定代碼 ----------

    def test_pick_near_month(self):
        rows = [('TXFH6', '2026/03/18'), ('TXFI6', '2026/09/16'),
                ('TXFJ6', '2026/10/21'), ('TXFR1', '2026/08/19')]
        got = api_test.pick_near_month(rows, today=datetime.date(2026, 8, 1))
        self.assertEqual(got, 'TXFI6', "應該挑到今天之後最近的那個月份")

    def test_pick_near_month_excludes_continuous_contracts(self):
        """R1/R2 是報價用的連續合約,不是可下單的月份合約。
        少了這條排除,它常常是日期最近的那一個而被選中。"""
        rows = [('TXFR1', '2026/08/19'), ('TXFR2', '2026/09/16'),
                ('TXFI6', '2026/09/16')]
        self.assertEqual(api_test.pick_near_month(rows, today=datetime.date(2026, 8, 1)),
                         'TXFI6')

    def test_pick_near_month_skips_expired(self):
        rows = [('TXFG6', '2026/07/15'), ('TXFI6', '2026/09/16')]
        self.assertEqual(api_test.pick_near_month(rows, today=datetime.date(2026, 8, 1)),
                         'TXFI6')

    def test_pick_near_month_handles_junk(self):
        self.assertIsNone(api_test.pick_near_month([]))
        self.assertIsNone(api_test.pick_near_month(None))
        self.assertIsNone(api_test.pick_near_month([('TXFG6', 'not-a-date')]))
        self.assertIsNone(api_test.pick_near_month([('TXFG6', '2026/07/15')],
                                                   today=datetime.date(2026, 8, 1)))

    def test_pick_near_month_accepts_several_date_formats(self):
        for fmt in ('2026/09/16', '2026-09-16', '20260916'):
            self.assertEqual(
                api_test.pick_near_month([('TXFI6', fmt)], today=datetime.date(2026, 8, 1)),
                'TXFI6', f"{fmt} 應該解析得出來")

    # ---------- preflight / 報告 ----------

    def test_preflight_blocks_on_bad_version_or_time(self):
        ok, lines = api_test.preflight('1.1', True, True,
                                       now=self._at(2026, 8, 3, 10, 0))
        self.assertFalse(ok)
        ok, lines = api_test.preflight('1.7', True, True,
                                       now=self._at(2026, 8, 1, 10, 0))   # 週六
        self.assertFalse(ok)

    def test_preflight_passes_when_everything_is_fine(self):
        """反向對照:條件都滿足時一定要放行,不然這個檢查等於把功能鎖死。"""
        ok, lines = api_test.preflight('1.7.0', True, True,
                                       now=self._at(2026, 8, 3, 10, 0))
        self.assertTrue(ok, "\n".join(lines))

    def test_preflight_missing_one_account_is_not_fatal(self):
        """有人只簽證券或只簽期貨。缺一個要標出來並跳過,不是整個流程拒跑。"""
        ok, lines = api_test.preflight('1.7', True, False,
                                       now=self._at(2026, 8, 3, 10, 0))
        self.assertTrue(ok)
        self.assertTrue(any('期貨' in x for x in lines))
        # 但兩個都沒有就真的沒東西可測
        ok2, _ = api_test.preflight('1.7', False, False,
                                    now=self._at(2026, 8, 3, 10, 0))
        self.assertFalse(ok2)

    def test_format_report_distinguishes_pass_and_fail(self):
        good = api_test.format_report([{'step': '登入測試', 'ok': True, 'detail': ''}])
        self.assertIn('✅', good)
        self.assertIn('審核', good, "全過時要提醒還要等審核")
        bad = api_test.format_report([{'step': '登入測試', 'ok': False, 'detail': '金鑰錯'}])
        self.assertIn('❌', bad)
        self.assertIn('金鑰錯', bad)
        self.assertNotIn('審核', bad, "沒過就不該說『等審核』")

    def test_accounts_text_lists_what_the_broker_returned(self):
        """【ADR-139 追記】使用者問「帳號&API KEY 還是要我手動先輸入嗎」——
        金鑰要手動輸入,**帳號不用**:帳號是登入成功後永豐回傳的。
        這個函式就是把回傳的帳戶印出來讓使用者確認券商端認得哪些帳號。"""
        txt = api_test.accounts_text([('1234567', 'S', True), ('7654321', 'F', False)])
        self.assertIn('1234567', txt)
        self.assertIn('7654321', txt)
        self.assertIn('2', txt, "要講出回傳幾個帳戶")
        self.assertIn('已通過', txt)
        self.assertIn('尚未通過', txt)

    def test_accounts_text_says_so_when_empty(self):
        """登入成功卻沒有帳戶是真實會發生的狀況(金鑰沒綁帳號),
        要講清楚而不是印一片空白。"""
        for empty in ([], None):
            txt = api_test.accounts_text(empty)
            self.assertIn('沒有回傳', txt)

    def test_signed_summary(self):
        txt = api_test.signed_summary([('1234567', 'S', True), ('7654321', 'F', False)])
        self.assertIn('1234567', txt)
        self.assertIn('❌', txt)
        allpass = api_test.signed_summary([('1234567', 'S', True)])
        self.assertIn('全部帳戶都已通過', allpass)
        self.assertEqual(api_test.signed_summary([]).count('查不到'), 1)


class TestPalette255(unittest.TestCase):
    """【ADR-138】指標線色盤:使用者要求「所有指標線的顏色可以更多選擇,
    最好有 255 種」。"""

    def test_generated_palette_has_exactly_255_colors(self):
        self.assertEqual(len(palette.GENERATED_COLORS), 255)

    def test_the_255_are_actually_in_the_palette_the_ui_uses(self):
        """光是「產生了 255 色」沒有用,要真的接到 PALETTE / color_map ——
        少了這條,把 PALETTE 改回只剩舊 8 色也是綠的。"""
        cmap = palette.color_map()
        self.assertGreaterEqual(len(palette.PALETTE), 255 + 8)
        for label, hx in palette.GENERATED_COLORS:
            self.assertEqual(cmap.get(label), hx, f"{label} 沒有進到下拉選單")

    def test_legacy_eight_come_first_and_unchanged(self):
        """設定檔存的是**標籤字串**,標籤一改使用者存過的顏色就全部對不上而
        靜默退回預設色。這條測試釘死舊標籤不可以動,而且要排在最前面
        (`list(color_map)[:6]` 是 MA1~MA6 的預設色)。"""
        want = ["黃 (#FFCA28)", "白 (#FFFFFF)", "藍 (#29B6F6)", "紫 (#E040FB)",
                "橘 (#FF9100)", "紅 (#FF1744)", "青 (#00E5FF)", "綠 (#00E676)"]
        self.assertEqual(palette.labels()[:8], want)
        self.assertEqual(list(palette.color_map())[:8], want)

    def test_labels_and_hexes_are_unique(self):
        labels = palette.labels()
        self.assertEqual(len(labels), len(set(labels)))
        hexes = [h for _, h in palette.PALETTE]
        self.assertEqual(len(hexes), len(set(hexes)), "同色重複出現等於選單裡有廢選項")

    def test_every_hex_is_well_formed_and_matches_its_label(self):
        for label, hx in palette.PALETTE:
            self.assertRegex(hx, r'^#[0-9A-F]{6}$')
            self.assertIn(hx, label, f"{label} 的標籤與色碼對不上")

    def test_no_color_is_too_dark_to_see_on_the_dark_chart(self):
        """主圖背景是深色,亮度太低的線等於隱形 —— 那種顏色放進選單是騙人的。"""
        for label, hx in palette.PALETTE:
            r, g, b = (int(hx[i:i + 2], 16) for i in (1, 3, 5))
            self.assertGreaterEqual(max(r, g, b), 0x20, f"{label} 在深色背景幾乎看不見")

    def test_resolve_falls_back_to_the_embedded_hex(self):
        """色盤日後再調整時,舊設定檔的標籤仍要還原成當初那個顏色,
        不可以靜默變色。"""
        self.assertEqual(palette.resolve("黃 (#FFCA28)"), "#FFCA28")
        self.assertEqual(palette.resolve("我亂取的名字 (#123456)"), "#123456")
        self.assertEqual(palette.resolve("完全沒有色碼", "#ABCDEF"), "#ABCDEF")
        self.assertEqual(palette.resolve(None, "#ABCDEF"), "#ABCDEF")
        self.assertEqual(palette.resolve("", "#ABCDEF"), "#ABCDEF")


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
        K棒不足時會大量走到這條路徑,任何一個拋例外都會讓整個回測中斷。

        【ADR-101】籌碼條件要餵「有 Chip 欄位但只有兩根」的 df:欄位不存在
        代表的是「根本沒載入籌碼」(使用者還沒下載),那是另一種狀況,刻意設計成
        拋 ChipDataMissing 讓 GUI 能提示;這裡要測的是本來的意圖——**資料太短**。
        實際的籌碼欄位一定由 attach_chips 建立,所以「有欄位、前幾根是 NaN」
        才是回測初期真正會遇到的情形。
        """
        tiny = self._df_from_closes([10, 11])
        tiny_with_chips = tiny.copy()
        for col in chips_features.ALL_CHIP_COLS:
            tiny_with_chips[col] = [1000.0, 2000.0]
        for key, (name, spec, fn) in strategy_engine.CONDITIONS.items():
            params = {k: dv for k, _lab, dv, _ch in (strategy_engine.spec_parts(x) for x in spec)}
            df_in = tiny_with_chips if key in strategy_engine.CHIP_CONDITION_IDS else tiny
            try:
                r = fn(df_in, params)
            except Exception as e:
                self.fail(f"條件 {key} 在短資料上拋例外: {type(e).__name__}: {e}")
            self.assertIn(r, (True, False), f"條件 {key} 回傳非布林值: {r!r}")

    def test_chip_conditions_never_break_backtest_via_eval_conditions(self):
        """【ADR-101】即使完全沒有籌碼欄位,走 eval_conditions 也只會讓那一條
        變 False 並記錄原因,不會中斷回測 (回測是透過 evaluate_strategy →
        eval_conditions,例外一定被接住)。這是上面那條安全網的真正保證。"""
        tiny = self._df_from_closes([10, 11])
        for key in strategy_engine.CHIP_CONDITION_IDS:
            errors = []
            ok, _ = strategy_engine.eval_conditions(
                tiny, [{'type': key, 'params': {}}], errors=errors)
            self.assertFalse(ok, f"{key} 沒有籌碼時不該成立")
            self.assertEqual(len(errors), 1, f"{key} 應留下可讀的錯誤原因")

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

    def test_same_direction_refill_accumulates_adr144(self):
        """【ADR-144 行為變更】同方向再次成交 = **加碼**,不再覆蓋。

        原本這條測試斷言「一般策略仍是覆蓋」(qty=3、成本=200)。那個行為是
        **錯的**,只是被「持倉中不評估進場條件」的閘門遮住了 —— 一旦真的發生
        第二筆成交,覆蓋會讓第一筆的成本基礎憑空消失,停損停利跟著算錯。
        ADR-144 開放分批進場之後,這件事會真的發生,所以改成加權平均成本。

        `apply_fill` 是「已經成交了,更新狀態」——「該不該買第二筆」是
        `evaluate_strategy` / `can_scale_in()` 的職責,不是這裡的。
        """
        s = self._bnh_strategy(); s['buy_and_hold'] = False
        rt = strategy_engine.new_runtime()
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 1, 'price': 100.0}, 1.0)
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 3, 'price': 200.0}, 2.0)
        self.assertEqual(rt['qty'], 4, "同方向加碼要累加數量")
        self.assertAlmostEqual(rt['entry_price'], (100.0 * 1 + 200.0 * 3) / 4,
                               msg="成本要變成加權平均,不是被最後一筆蓋掉")
        self.assertEqual(rt['entries'], 2)

    def test_first_entry_after_flat_does_not_accumulate(self):
        """反向對照:平倉之後的第一筆是全新部位,不可以跟上一段混在一起。"""
        s = self._bnh_strategy(); s['buy_and_hold'] = False
        rt = strategy_engine.new_runtime()
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 2, 'price': 100.0}, 1.0)
        strategy_engine.apply_fill(s, rt, {'kind': 'CLOSE', 'action': '賣出',
                                           'qty': 2, 'price': 110.0}, 2.0)
        self.assertEqual(rt['entries'], 0, "平倉要把分批計數歸零")
        strategy_engine.apply_fill(s, rt, {'kind': 'OPEN', 'action': '買進',
                                           'qty': 1, 'price': 300.0}, 3.0)
        self.assertEqual(rt['qty'], 1)
        self.assertAlmostEqual(rt['entry_price'], 300.0)
        self.assertEqual(rt['entries'], 1)

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

    def _dts(self, y, mo, d, h, mi, sec=0):
        from datetime import datetime as _datetime
        return _datetime(y, mo, d, h, mi, sec)

    # ---------- 【ADR-121】開盤暖機 ----------
    def test_just_opened_stock_window(self):
        """09:00:00 起算 30 秒內算「剛開盤」,滿 30 秒放行 (左閉右開)。

        使用者實測 08:45 與 09:00 各噴一次 kbars 逾時 —— 正是閘門打開那一秒
        去要 K 線的結果。這個窗口寫錯 (早一秒晚一秒) 不會有任何錯誤訊息,
        只會在開盤那一瞬間繼續踩同一個坑,所以邊界要逐秒驗。
        """
        f = lambda sec, mi=0, h=9: market_session.just_opened(
            '股票', self._dts(2026, 7, 13, h, mi, sec), warmup_sec=30)
        self.assertFalse(f(59, mi=59, h=8), "08:59:59 還沒開盤")
        self.assertTrue(f(0), "09:00:00 就算剛開盤")
        self.assertTrue(f(29), "09:00:29 仍在暖機內")
        self.assertFalse(f(30), "09:00:30 暖機結束,要放行")
        self.assertFalse(f(0, mi=1), "09:01:00 早就過了")

    def test_just_opened_futures_day_window(self):
        """期貨日盤 08:45 —— 使用者兩則逾時的第一則就在這裡。"""
        f = lambda h, mi, sec: market_session.just_opened(
            '期貨', self._dts(2026, 7, 13, h, mi, sec), warmup_sec=30)
        self.assertFalse(f(8, 44, 59))
        self.assertTrue(f(8, 45, 0))
        self.assertTrue(f(8, 45, 29))
        self.assertFalse(f(8, 45, 30))

    def test_just_opened_futures_night_only_at_evening_open(self):
        """夜盤 15:00 是開盤;凌晨段 (00:00~05:00) 是**同一盤的延續**,不是剛開盤。

        05:00 是收盤不是開盤 —— 把它也當成開盤會在收盤那一刻莫名其妙停一段
        時間不評估。
        """
        self.assertTrue(market_session.just_opened(
            '期貨', self._dts(2026, 7, 13, 15, 0, 10), warmup_sec=30))
        self.assertFalse(market_session.just_opened(
            '期貨', self._dts(2026, 7, 14, 0, 0, 10), warmup_sec=30), "凌晨段不是剛開盤")
        self.assertFalse(market_session.just_opened(
            '期貨', self._dts(2026, 7, 14, 5, 0, 10), warmup_sec=30), "05:00 是收盤")

    def test_session_open_minute_contract(self):
        """`_session_open_minute` 只回「**今天**這一盤的開盤時刻」,不是「盤別的
        開盤時刻」。

        這條要直接測這個 helper,不能只透過 just_opened 驗 —— 夜盤凌晨段
        (00:00~05:00) 走 just_opened 時會被 `0 <= elapsed` 這個算式擋掉,
        所以就算這裡回錯值,just_opened 的結果**看起來仍然是對的**
        (突變測試實測:改壞它,只測 just_opened 的斷言全部照樣綠)。
        契約錯了要當場看得見,不能靠下游的算式湊巧擋住。
        """
        f = market_session._session_open_minute
        # 傍晚段:今天 15:00 開的,回 15:00
        self.assertEqual(f('期貨', self._dts(2026, 7, 13, 15, 0, 10)),
                         market_session.FUT_NIGHT_OPEN_MIN)
        # 凌晨段:那一盤是**昨天** 15:00 開的,今天沒有開盤時刻可回
        self.assertIsNone(f('期貨', self._dts(2026, 7, 14, 0, 30, 0)),
                          "凌晨段屬前一天的夜盤,不該回今天的開盤時刻")
        self.assertIsNone(f('期貨', self._dts(2026, 7, 14, 4, 59, 0)))
        # 日盤與台股
        self.assertEqual(f('期貨', self._dts(2026, 7, 13, 9, 0, 0)),
                         market_session.FUT_DAY_OPEN_MIN)
        self.assertEqual(f('股票', self._dts(2026, 7, 13, 10, 0, 0)),
                         market_session.STOCK_OPEN_MIN)
        # 休市 / 未知種類
        self.assertIsNone(f('股票', self._dts(2026, 7, 13, 14, 0, 0)))
        self.assertIsNone(f('不存在', self._dts(2026, 7, 13, 10, 0, 0)))

    def test_just_opened_respects_include_night(self):
        """關閉夜盤的策略,15:00 對它而言是休市,不該回 True。"""
        self.assertFalse(market_session.just_opened(
            '期貨', self._dts(2026, 7, 13, 15, 0, 5), include_night=False, warmup_sec=30))

    def test_just_opened_false_when_closed_or_unknown(self):
        """休市、週末、未知種類一律 False —— 這個函式只負責「剛開盤」這件事,
        「有沒有開盤」是 is_market_open 的職責,不可以在這裡二度把關。"""
        self.assertFalse(market_session.just_opened('股票', self._dts(2026, 7, 18, 9, 0, 5)))
        self.assertFalse(market_session.just_opened('股票', self._dts(2026, 7, 13, 11, 0, 0)))
        self.assertFalse(market_session.just_opened('不存在', self._dts(2026, 7, 13, 9, 0, 5)))

    def test_just_opened_warmup_zero_disables(self):
        """暖機秒數設 0/負數 = 關閉這個機制 (使用者想要完全回到舊行為時的出口)。"""
        for w in (0, -1, None if False else 0.0):
            self.assertFalse(market_session.just_opened(
                '股票', self._dts(2026, 7, 13, 9, 0, 0), warmup_sec=w), repr(w))

    def test_just_opened_bad_warmup_is_safe(self):
        """暖機秒數壞掉時寧可不擋 —— 擋住等於策略整段不評估,比不擋危險。"""
        self.assertFalse(market_session.just_opened(
            '股票', self._dts(2026, 7, 13, 9, 0, 0), warmup_sec='abc'))

    def test_just_opened_odd_lot_uses_its_own_open_time(self):
        """盤中零股開盤時刻是可設定的 (ADR-072),暖機要跟著它走,不是寫死 09:00。"""
        old = market_session.ODD_LOT_OPEN_MIN
        try:
            market_session.set_odd_lot_open_hhmm('09:10')
            self.assertFalse(market_session.just_opened(
                '零股', self._dts(2026, 7, 13, 9, 0, 5), warmup_sec=30), "09:00 零股還沒開盤")
            self.assertTrue(market_session.just_opened(
                '零股', self._dts(2026, 7, 13, 9, 10, 5), warmup_sec=30))
        finally:
            market_session.ODD_LOT_OPEN_MIN = old

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


class TestChukuangrenBand(unittest.TestCase):
    """【ADR-084/085】「楚狂人之終極波段」:看加權指數(A)訊號,做自選商品(B),
    單一方向(做多/做空)、隔天中午12點二次確認、點數移動停利(從進場點D起算)→SMA20移動停利。"""

    def _mk_daily_df(self, closes, start='2026-01-01'):
        idx = pd.date_range(start=start, periods=len(closes), freq='D')
        return pd.DataFrame({'Close': closes}, index=idx)

    def _params(self, **overrides):
        # h 已在 ADR-085 移除;停利線一律從進場點 D 起算
        p = {'x': 100.0, 'y': 90.0, 'z': 92.0, 's1': 110.0, 's2': 108.0, 'c': 5.0, 'f': 2.0}
        p.update(overrides)
        return p

    def _confirm_and_execute(self, params, rt, confirm_price, today_key, qty=1,
                              exec_price=None, now_ts=1000.0, delay_sec=60.0):
        """【新ADR 確認/下單分兩個時間點】測試輔助:模擬 12:00 confirm →
        (過了 delay_sec 秒) 12:01 執行 這兩步,回傳跟舊版 on_noon_check 一樣
        的 intents list,方便既有測試沿用同一種斷言寫法。exec_price 不帶時
        預設等於 confirm_price (=A/B同價的簡化情境,不影響邏輯本身測試)。"""
        chukuangren_band.on_noon_confirm(params, rt, confirm_price, today_key, now_ts, qty=qty)
        ep = confirm_price if exec_price is None else exec_price
        return chukuangren_band.on_execute_armed(rt, ep, now_ts + delay_sec, delay_sec=delay_sec)

    # ---------- default_strategy / params_of / validate ----------

    def test_default_strategy_shape(self):
        s = chukuangren_band.default_strategy()
        self.assertEqual(s['kind'], 'chukuangren_band')
        self.assertEqual(s['direction'], '做多')
        self.assertTrue(s['watch_enabled'])
        self.assertEqual(s['watch_symbol'], '^TWII')
        # 【ADR-128】舊值是 '5分K',但那不是訊號週期 —— 是被拿去驅動評估節拍的
        # 借用值。訊號一直看日K,這裡要存真話。
        self.assertEqual(s['watch_timeframe'], '日K')
        p = chukuangren_band.params_of(s)
        self.assertEqual(set(p.keys()), set(chukuangren_band.PARAM_KEYS))
        self.assertNotIn('h', p)  # H 已移除

    def test_param_keys_for_direction(self):
        # 【ADR-129】z / s2 已併入 y / s1,不再出現在 UI 參數清單
        self.assertEqual(chukuangren_band.param_keys_for('做多'), ('x', 'y', 'c', 'f'))
        self.assertEqual(chukuangren_band.param_keys_for('做空'), ('x', 's1', 'c', 'f'))

    def _valid_long_strategy(self, **overrides):
        s = chukuangren_band.default_strategy()
        s.update({'symbol': 'MXFR1', 'trade_type': '期貨', 'market': '台期貨', 'qty': 1,
                  'ck_x': 100.0, 'ck_y': 90.0, 'ck_z': 92.0, 'ck_c': 5.0, 'ck_f': 2.0})
        s.update(overrides)
        return s

    def test_default_strategy_price_type_is_limit(self):
        s = chukuangren_band.default_strategy()
        self.assertEqual(s['price_type'], '限價')

    def test_validate_range_market_valid_for_futures(self):
        s = self._valid_long_strategy(price_type='範圍市價')
        self.assertTrue(chukuangren_band.validate(s)[0])

    def test_validate_range_market_rejected_for_stock(self):
        s = self._valid_long_strategy(trade_type='股票', market='台股', price_type='範圍市價')
        ok, msg = chukuangren_band.validate(s)
        self.assertFalse(ok)
        self.assertIn('範圍市價', msg)

    def test_validate_odd_lot_forced_limit(self):
        s = self._valid_long_strategy(trade_type='零股', market='台股', price_type='市價')
        ok, msg = chukuangren_band.validate(s)
        self.assertFalse(ok)
        self.assertIn('零股', msg)

    def test_every_param_has_label_and_help(self):
        # 每一個要填的參數都要有標籤與完整中文說明,不可漏 (UI 直接讀這兩份)
        for k in chukuangren_band.PARAM_KEYS:
            self.assertIn(k, chukuangren_band.PARAM_LABELS)
            self.assertIn(k, chukuangren_band.PARAM_HELP)
            self.assertTrue(chukuangren_band.PARAM_HELP[k].strip())

    def test_validate_allows_long_stop_above_x(self):
        """【ADR-130】Y 與 X 的高低不再限制 —— 實際進場價不是 X,停損要貼著
        實際進場價才控得住虧損 (使用者要求自行決定)。"""
        s = chukuangren_band.default_strategy()
        s['symbol'] = '2330'; s['qty'] = 1; s['direction'] = '做多'
        s['ck_x'] = 100; s['ck_y'] = 105
        ok, msg = chukuangren_band.validate(s)
        self.assertTrue(ok, f"Y 高於 X 不該再被擋: {msg}")

    def test_validate_allows_short_stop_below_x(self):
        """【ADR-130】同上,做空方向。"""
        s = chukuangren_band.default_strategy()
        s['symbol'] = 'TXF'; s['qty'] = 1; s['direction'] = '做空'
        s['trade_type'] = '期貨'; s['market'] = '台期貨'
        s['ck_x'] = 100; s['ck_s1'] = 95
        ok, msg = chukuangren_band.validate(s)
        self.assertTrue(ok, f"S1 低於 X 不該再被擋: {msg}")

    def test_validate_short_rejects_stock(self):
        s = chukuangren_band.default_strategy()
        s['symbol'] = '2330'; s['qty'] = 1; s['direction'] = '做空'
        s['trade_type'] = '股票'; s['ck_x'] = 100; s['ck_s1'] = 110
        ok, msg = chukuangren_band.validate(s)
        self.assertFalse(ok)

    def test_validate_rejects_index_execution_symbol(self):
        s = chukuangren_band.default_strategy()
        s['symbol'] = '^TWII'; s['qty'] = 1
        s['ck_x'] = 100; s['ck_y'] = 90
        ok, msg = chukuangren_band.validate(s)
        self.assertFalse(ok)

    def test_validate_passes_reasonable_long(self):
        s = chukuangren_band.default_strategy()
        s['symbol'] = '2330'; s['qty'] = 1; s['direction'] = '做多'
        s['ck_x'] = 100; s['ck_y'] = 90; s['ck_z'] = 92; s['ck_c'] = 5; s['ck_f'] = 2
        ok, msg = chukuangren_band.validate(s)
        self.assertTrue(ok, msg)

    # ---------- on_daily_close:方向閘門的進場訊號偵測 ----------

    def test_long_direction_enters_only_long(self):
        rt = strategy_engine.new_runtime()
        params = self._params()
        # 突破 X → 待確認多單
        df_up = self._mk_daily_df([95] * 19 + [105])
        chukuangren_band.on_daily_close(params, rt, df_up, direction='做多')
        self.assertEqual(rt['pending_entry']['dir'], 'LONG')
        # 做多方向遇到跌破 X 不進場 (不會反向做空)
        rt2 = strategy_engine.new_runtime()
        df_dn = self._mk_daily_df([105] * 19 + [95])
        chukuangren_band.on_daily_close(params, rt2, df_dn, direction='做多')
        self.assertIsNone(rt2['pending_entry'])

    def test_short_direction_enters_only_short(self):
        rt = strategy_engine.new_runtime()
        params = self._params()
        df_dn = self._mk_daily_df([105] * 19 + [95])
        chukuangren_band.on_daily_close(params, rt, df_dn, direction='做空')
        self.assertEqual(rt['pending_entry']['dir'], 'SHORT')
        rt2 = strategy_engine.new_runtime()
        df_up = self._mk_daily_df([95] * 19 + [105])
        chukuangren_band.on_daily_close(params, rt2, df_up, direction='做空')
        self.assertIsNone(rt2['pending_entry'])

    def test_daily_close_no_trigger_at_exact_x(self):
        rt = strategy_engine.new_runtime()
        params = self._params()
        df = self._mk_daily_df([95] * 19 + [100])
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertIsNone(rt['pending_entry'])

    def test_daily_close_idempotent_same_trading_day(self):
        rt = strategy_engine.new_runtime()
        params = self._params()
        df = self._mk_daily_df([95] * 19 + [105])
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        rt['pending_entry'] = None
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertIsNone(rt['pending_entry'])

    def test_daily_close_not_enough_history_is_noop(self):
        rt = strategy_engine.new_runtime()
        params = self._params()
        df = self._mk_daily_df([105] * 10)
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertIsNone(rt['pending_entry'])
        self.assertEqual(rt['last_daily_bar_date'], '')

    # ---------- on_noon_confirm+on_execute_armed:隔天中午確認進場 + 記錄進場指數點位 ----------

    def test_noon_check_confirms_long_entry_records_index_point(self):
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-01'}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 105.0, '2026-01-02', qty=3)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'OPEN')
        self.assertEqual(intents[0]['action'], '買進')
        self.assertEqual(intents[0]['qty'], 3)
        self.assertIsNone(rt['pending_entry'])
        # ADR-085:一定記錄進場當時的加權指數點位
        self.assertEqual(rt['entry_index_price'], 105.0)
        self.assertIn('記錄進場加權指數點位', intents[0]['reason'])

    def test_noon_check_confirms_short_entry_records_index_point(self):
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'SHORT', 'date': '2026-01-01'}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 95.0, '2026-01-02', qty=2)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['action'], '賣出')
        self.assertEqual(rt['entry_index_price'], 95.0)

    def test_noon_check_invalidates_long_entry_when_back_below_x(self):
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-01'}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 98.0, '2026-01-02')
        self.assertEqual(intents, [])
        self.assertIsNone(rt['pending_entry'])
        self.assertEqual(rt['entry_index_price'], 0.0)

    def test_noon_check_dedupes_same_trading_day(self):
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-01'}
        params = self._params()
        self._confirm_and_execute(params, rt, 105.0, '2026-01-02')
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-02'}
        intents2 = self._confirm_and_execute(params, rt, 106.0, '2026-01-02')
        self.assertEqual(intents2, [])

    # ---------- 【新ADR】確認(12:00)與下單(12:01)分兩個時間點 ----------

    def test_noon_confirm_arms_but_does_not_execute_immediately(self):
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-01'}
        params = self._params()
        chukuangren_band.on_noon_confirm(params, rt, 105.0, '2026-01-02', now_ts=1000.0, qty=3)
        self.assertIsNotNone(rt['armed_intent'])
        self.assertEqual(rt['armed_intent']['kind'], 'OPEN')
        self.assertEqual(rt['armed_at_ts'], 1000.0)
        # ADR-085 記錄進場點位在確認當下就發生,不用等到執行
        self.assertEqual(rt['entry_index_price'], 105.0)
        # 還沒過60秒 (12:01) → 不應該有 intent
        intents = chukuangren_band.on_execute_armed(rt, 106.0, now_ts=1030.0)
        self.assertEqual(intents, [])
        self.assertIsNotNone(rt['armed_intent'])  # 仍在等待,還沒清掉

    def test_execute_armed_fires_after_delay_using_exec_price_not_confirm_price(self):
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-01'}
        params = self._params()
        chukuangren_band.on_noon_confirm(params, rt, 105.0, '2026-01-02', now_ts=1000.0, qty=3)
        # 過了60秒 (約12:01),用執行當下 (B) 的最新價 108.0,不是12:00確認價105.0
        intents = chukuangren_band.on_execute_armed(rt, 108.0, now_ts=1060.0)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'OPEN')
        self.assertEqual(intents[0]['action'], '買進')
        self.assertEqual(intents[0]['qty'], 3)
        self.assertEqual(intents[0]['price'], 108.0)  # 執行價,不是確認價
        self.assertIsNone(rt['armed_intent'])          # 已清空,不會重複下單
        self.assertEqual(rt['armed_at_ts'], 0.0)

    def test_execute_armed_noop_when_nothing_armed(self):
        rt = strategy_engine.new_runtime()
        intents = chukuangren_band.on_execute_armed(rt, 100.0, now_ts=9999.0)
        self.assertEqual(intents, [])

    def test_execute_armed_close_uses_current_qty_from_runtime(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['qty'] = 5
        rt['pending_exit'] = {'reason': 'SL', 'date': '2026-01-01'}
        params = self._params()
        chukuangren_band.on_noon_confirm(params, rt, 91.0, '2026-01-02', now_ts=2000.0)  # 91<Z=92
        intents = chukuangren_band.on_execute_armed(rt, 90.5, now_ts=2060.0)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'CLOSE')
        self.assertEqual(intents[0]['qty'], 5)
        self.assertEqual(intents[0]['price'], 90.5)

    # ---------- 【找到的bug/修正】armed_intent 過期作廢 (max_age_sec) ----------

    def test_execute_armed_discards_stale_intent_past_max_age(self):
        """12:00確認成立後,若因為停用/總開關關閉/程式重啟等原因太久沒真正
        執行 (超過 max_age_sec),不應該用『現在』的價格補送一筆委託——那已經
        跟這個策略『12:00確認、12:01立刻執行』的設計語意不符。"""
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-01'}
        params = self._params()
        chukuangren_band.on_noon_confirm(params, rt, 105.0, '2026-01-02', now_ts=1000.0, qty=3)
        self.assertIsNotNone(rt['armed_intent'])
        # 超過 max_age_sec (預設600秒) 才第一次被評估到 (例如總開關關了很久)
        intents = chukuangren_band.on_execute_armed(rt, 999.0, now_ts=1000.0 + 601.0)
        self.assertEqual(intents, [])
        self.assertIsNone(rt['armed_intent'])   # 已作廢清空,不會殘留等下次補送
        self.assertEqual(rt['armed_at_ts'], 0.0)
        self.assertIsNotNone(rt.get('last_discarded_stale_intent'))
        self.assertEqual(rt['last_discarded_stale_intent']['kind'], 'OPEN')

    def test_execute_armed_still_fires_within_max_age(self):
        """確認一下修正沒有誤傷正常情況:延遲60秒內、也還沒超過 max_age_sec
        的正常執行,行為要跟修正前完全一樣。"""
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-01'}
        params = self._params()
        chukuangren_band.on_noon_confirm(params, rt, 105.0, '2026-01-02', now_ts=1000.0, qty=3)
        intents = chukuangren_band.on_execute_armed(rt, 108.0, now_ts=1060.0)
        self.assertEqual(len(intents), 1)
        self.assertIsNone(rt.get('last_discarded_stale_intent'))

    def test_execute_armed_custom_max_age(self):
        rt = strategy_engine.new_runtime()
        rt['pending_entry'] = {'dir': 'LONG', 'date': '2026-01-01'}
        params = self._params()
        chukuangren_band.on_noon_confirm(params, rt, 105.0, '2026-01-02', now_ts=1000.0, qty=3)
        intents = chukuangren_band.on_execute_armed(rt, 999.0, now_ts=1000.0 + 70.0, max_age_sec=65.0)
        self.assertEqual(intents, [])
        self.assertIsNotNone(rt.get('last_discarded_stale_intent'))

    # ---------- 固定停損:觸發→隔天確認/作廢 ----------

    def test_daily_close_long_fixed_stop_loss_pending(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['entry_index_price'] = 100.0
        params = self._params()
        df = self._mk_daily_df([100] * 19 + [85])  # 85 < Y=90
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertEqual(rt['pending_exit'], {'reason': 'SL', 'date': df.index[-1].strftime('%Y-%m-%d')})

    def test_noon_check_confirms_long_stop_loss(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['qty'] = 2
        rt['pending_exit'] = {'reason': 'SL', 'date': '2026-01-01'}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 91.0, '2026-01-02')  # 91 < Z=92
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'CLOSE')
        self.assertEqual(intents[0]['action'], '賣出')
        self.assertIsNone(rt['pending_exit'])

    def test_noon_check_invalidates_long_stop_loss_when_back_above_z(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'
        rt['pending_exit'] = {'reason': 'SL', 'date': '2026-01-01'}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 93.0, '2026-01-02')  # 93 > Z=92
        self.assertEqual(intents, [])
        self.assertIsNone(rt['pending_exit'])

    def test_daily_close_short_fixed_stop_loss_pending(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'SHORT'; rt['entry_index_price'] = 100.0
        params = self._params()
        df = self._mk_daily_df([100] * 19 + [115])  # 115 > S1=110
        chukuangren_band.on_daily_close(params, rt, df, direction='做空')
        self.assertEqual(rt['pending_exit']['reason'], 'SL')

    def test_noon_check_confirms_short_stop_loss(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'SHORT'
        rt['pending_exit'] = {'reason': 'SL', 'date': '2026-01-01'}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 109.0, '2026-01-02')  # 109 > S2=108
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['action'], '買進')

    # ---------- 點數移動停利:從進場點D起算、棘輪、觸發 ----------

    def test_long_point_trailing_arms_from_entry_and_ratchets(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['entry_index_price'] = 100.0
        params = self._params()  # c=5, f=2
        # close=110, profit=10>c=5 → armed, base 從進場點 100 起算;
        # n=(10-5)//2=2 → base=100+2*2=104。close(110) 仍 > 104,不觸發。
        df = self._mk_daily_df([100] * 19 + [110])
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertTrue(rt['trail_armed'])
        self.assertEqual(rt['trail_base'], 104.0)
        self.assertIsNone(rt['pending_exit'])

    def test_long_point_trailing_never_ratchets_down_and_triggers(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['entry_index_price'] = 100.0
        rt['trail_armed'] = True; rt['trail_base'] = 108.0
        params = self._params()
        # profit=close(107)-100=7,n=(7-5)//2=1 → candidate=100+2=102 < 108,不下調。
        # close(107) < base(108) → 觸發點數移動停利待確認。
        df = self._mk_daily_df([100] * 20 + [107])
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertEqual(rt['trail_base'], 108.0)
        self.assertEqual(rt['pending_exit']['reason'], 'TP_POINT')

    def test_noon_check_confirms_long_point_take_profit(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['trail_base'] = 107.0
        rt['pending_exit'] = {'reason': 'TP_POINT', 'date': '2026-01-01'}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 106.0, '2026-01-02')  # 106 <= 107
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'CLOSE')
        self.assertEqual(rt['trail_base'], 0.0)

    def test_noon_check_invalidates_long_point_take_profit(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['trail_base'] = 107.0
        rt['pending_exit'] = {'reason': 'TP_POINT', 'date': '2026-01-01'}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 108.0, '2026-01-02')  # 108 > 107
        self.assertEqual(intents, [])
        self.assertEqual(rt['trail_base'], 107.0)

    def test_short_point_trailing_arms_from_entry_and_ratchets_down(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'SHORT'; rt['entry_index_price'] = 100.0
        params = self._params()  # c=5, f=2
        df = self._mk_daily_df([100] * 19 + [90])  # profit=100-90=10>5
        chukuangren_band.on_daily_close(params, rt, df, direction='做空')
        self.assertTrue(rt['trail_armed'])
        self.assertEqual(rt['trail_base'], 96.0)  # 100 - (10-5)//2 *2 = 100-4 = 96

    # ---------- SMA20 移動停利模式切換與確認 ----------

    def test_long_switches_to_sma20_mode_and_fixed_stop_still_monitored(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['entry_index_price'] = 100.0
        params = self._params(y=50.0)
        df = self._mk_daily_df([100] * 19 + [150])
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertTrue(rt['sma20_mode'])
        self.assertIsNone(rt['pending_exit'])
        df2 = self._mk_daily_df([100] * 19 + [150, 95])
        chukuangren_band.on_daily_close(params, rt, df2, direction='做多')
        self.assertEqual(rt['pending_exit']['reason'], 'TP_SMA20')

    def test_long_does_not_switch_to_sma20_mode_when_profit_not_over_c(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['entry_index_price'] = 100.0
        params = self._params(y=50.0)  # c=5.0
        # profit=104-100=4 < C=5,即使收盤(104)已站上 sma20(=100.2) 也不該切換
        df = self._mk_daily_df([100] * 19 + [104])
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertFalse(rt['sma20_mode'])

    def test_short_switches_to_sma20_mode_only_after_profit_over_c(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'SHORT'; rt['entry_index_price'] = 100.0
        params = self._params(s1=150.0)  # c=5.0
        # profit=100-97=3 < C=5,即使收盤(97)已跌破 sma20(=99.85) 也不該切換
        df = self._mk_daily_df([100] * 19 + [97])
        chukuangren_band.on_daily_close(params, rt, df, direction='做空')
        self.assertFalse(rt['sma20_mode'])
        # 隔天獲利拉大到超過 C 且仍跌破 sma20,才真正切換
        df2 = self._mk_daily_df([100] * 19 + [97, 80])
        chukuangren_band.on_daily_close(params, rt, df2, direction='做空')
        self.assertTrue(rt['sma20_mode'])

    def test_long_fixed_stop_still_fires_even_in_sma20_mode(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['entry_index_price'] = 100.0
        rt['sma20_mode'] = True
        params = self._params()  # y=90
        df = self._mk_daily_df([100] * 19 + [85])
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertEqual(rt['pending_exit']['reason'], 'SL')

    def test_noon_check_confirms_sma20_take_profit(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'
        rt['pending_exit'] = {'reason': 'TP_SMA20', 'date': '2026-01-01', 'sma20_ref': 102.0}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 99.0, '2026-01-02')
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'CLOSE')

    def test_noon_check_invalidates_sma20_take_profit(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'
        rt['pending_exit'] = {'reason': 'TP_SMA20', 'date': '2026-01-01', 'sma20_ref': 102.0}
        params = self._params()
        intents = self._confirm_and_execute(params, rt, 103.0, '2026-01-02')
        self.assertEqual(intents, [])

    # ---------- pending_exit 待確認期間暫停繼續評估 ----------

    def test_daily_close_paused_while_pending_exit_unresolved(self):
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['entry_index_price'] = 100.0
        rt['pending_exit'] = {'reason': 'SL', 'date': '2025-12-31'}
        rt['last_daily_bar_date'] = '2025-12-31'
        params = self._params()
        df = self._mk_daily_df([100] * 19 + [200])
        chukuangren_band.on_daily_close(params, rt, df, direction='做多')
        self.assertEqual(rt['pending_exit']['reason'], 'SL')

    # ---------- 完整流程整合測試 ----------

    def test_full_round_trip_long_entry_then_sma20_take_profit(self):
        rt = strategy_engine.new_runtime()
        params = self._params(y=50.0)
        df1 = self._mk_daily_df([95] * 19 + [105])
        chukuangren_band.on_daily_close(params, rt, df1, direction='做多')
        self.assertEqual(rt['pending_entry']['dir'], 'LONG')

        intents = self._confirm_and_execute(params, rt, 106.0, '2026-01-20', qty=1)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['kind'], 'OPEN')
        strategy_engine.apply_fill({'buy_and_hold': False}, rt, intents[0], now_ts=0)
        self.assertEqual(rt['state'], 'LONG')
        self.assertEqual(rt['entry_index_price'], 106.0)

        df2 = self._mk_daily_df([95] * 19 + [105, 130])
        chukuangren_band.on_daily_close(params, rt, df2, direction='做多')
        self.assertTrue(rt['sma20_mode'])
        self.assertIsNone(rt['pending_exit'])

        df3 = self._mk_daily_df([95] * 19 + [105, 130, 90])
        chukuangren_band.on_daily_close(params, rt, df3, direction='做多')
        self.assertEqual(rt['pending_exit']['reason'], 'TP_SMA20')

        sma20_ref = rt['pending_exit']['sma20_ref']
        intents2 = self._confirm_and_execute(params, rt, sma20_ref - 1, '2026-01-23')
        self.assertEqual(len(intents2), 1)
        self.assertEqual(intents2[0]['kind'], 'CLOSE')
        strategy_engine.apply_fill({'buy_and_hold': False}, rt, intents2[0], now_ts=0)
        self.assertEqual(rt['state'], 'FLAT')


class TestMarketPattern(unittest.TestCase):
    """【新ADR 盤勢型態提醒】core/market_pattern.py 的離線測試:每個型態至少
    一個「應該觸發」+ 一個「不應該觸發」的案例,確保規則跟文件描述一致。"""

    def _mk_df(self, closes, highs=None, lows=None, opens=None, start='2026-01-01'):
        n = len(closes)
        idx = pd.date_range(start, periods=n, freq='D')
        return pd.DataFrame({
            'Open': opens or closes, 'High': highs or closes,
            'Low': lows or closes, 'Close': closes,
        }, index=idx)

    def _zigzag(self, anchors, seg_len=4):
        prices = [float(anchors[0])]
        for a, b in zip(anchors, anchors[1:]):
            for k in range(1, seg_len + 1):
                prices.append(a + (b - a) * k / seg_len)
        return prices

    # ---------- classify_regime ----------

    def test_classify_regime_range_bound(self):
        closes = ([100, 110] * 30)[:60]
        df = self._mk_df(closes)
        r = market_pattern.classify_regime(df, lookback=60, near_pct=3.0)
        self.assertIsNotNone(r)
        self.assertEqual(r['regime'], '區間整理')

    def test_classify_regime_uptrend(self):
        closes = [100 + i for i in range(60)]
        df = self._mk_df(closes)
        r = market_pattern.classify_regime(df, lookback=60, near_pct=3.0)
        self.assertEqual(r['regime'], '上升趨勢')

    def test_classify_regime_downtrend(self):
        closes = [160 - i for i in range(60)]
        df = self._mk_df(closes)
        r = market_pattern.classify_regime(df, lookback=60, near_pct=3.0)
        self.assertEqual(r['regime'], '下降趨勢')

    def test_classify_regime_near_high_and_low(self):
        closes = [100] * 30 + [200] * 29 + [199]
        df = self._mk_df(closes)
        r = market_pattern.classify_regime(df, lookback=60, near_pct=3.0)
        self.assertTrue(r['near_high'])
        closes2 = [200] * 30 + [100] * 29 + [101]
        df2 = self._mk_df(closes2)
        r2 = market_pattern.classify_regime(df2, lookback=60, near_pct=3.0)
        self.assertTrue(r2['near_low'])

    # ---------- double_top / double_bottom ----------

    def test_double_top_confirmed_on_neckline_break(self):
        closes = [100, 105, 110, 105, 100, 95, 100, 105, 110, 105, 100, 95, 90]
        df = self._mk_df(closes)
        r = market_pattern.detect_double_top(df, swing_window=2, tolerance_pct=3.0, lookback=60)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'double_top')
        self.assertEqual(r['neckline'], 95.0)

    def test_double_top_not_yet_broken(self):
        closes = [100, 105, 110, 105, 100, 95, 100, 105, 110, 105, 100, 95]
        df = self._mk_df(closes)
        r = market_pattern.detect_double_top(df, swing_window=2, tolerance_pct=3.0, lookback=60)
        self.assertIsNone(r)

    def test_double_bottom_confirmed_on_neckline_break(self):
        closes = [100, 95, 90, 95, 100, 105, 100, 95, 90, 95, 100, 105, 110]
        df = self._mk_df(closes)
        r = market_pattern.detect_double_bottom(df, swing_window=2, tolerance_pct=3.0, lookback=60)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'double_bottom')
        self.assertEqual(r['neckline'], 105.0)

    # ---------- head_shoulders_top / bottom ----------

    def test_head_shoulders_top_confirmed(self):
        closes = [100, 105, 110, 105, 100, 95, 100, 105, 130, 105, 100, 95, 100, 105, 110, 105, 100, 95, 90]
        df = self._mk_df(closes)
        r = market_pattern.detect_head_shoulders_top(df, swing_window=2, tolerance_pct=5.0,
                                                       head_margin_pct=1.0, lookback=90)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'head_shoulders_top')
        self.assertEqual(r['head'], 130.0)

    def test_head_shoulders_top_no_signal_on_plain_uptrend(self):
        closes = [100 + i for i in range(30)]
        df = self._mk_df(closes)
        r = market_pattern.detect_head_shoulders_top(df, swing_window=2, tolerance_pct=5.0,
                                                       head_margin_pct=1.0, lookback=90)
        self.assertIsNone(r)

    def test_head_shoulders_bottom_confirmed(self):
        closes = [200 - c + 100 for c in
                  [100, 105, 110, 105, 100, 95, 100, 105, 130, 105, 100, 95, 100, 105, 110, 105, 100, 95, 90]]
        df = self._mk_df(closes)
        r = market_pattern.detect_head_shoulders_bottom(df, swing_window=2, tolerance_pct=5.0,
                                                          head_margin_pct=1.0, lookback=90)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'head_shoulders_bottom')

    # ---------- undercut_reverse / false_breakout ----------

    def test_undercut_reverse_confirmed(self):
        closes = [100] * 25 + [98, 96, 94, 96, 101]
        df = self._mk_df(closes)
        r = market_pattern.detect_undercut_reverse(df, lookback=60, recover_bars=5)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'undercut_reverse')
        self.assertEqual(r['support'], 100.0)

    def test_undercut_reverse_no_breach_no_signal(self):
        closes = [100] * 25 + [101, 102, 101, 102, 103]
        df = self._mk_df(closes)
        r = market_pattern.detect_undercut_reverse(df, lookback=60, recover_bars=5)
        self.assertIsNone(r)

    def test_false_breakout_confirmed(self):
        closes = [100] * 25 + [102, 103, 104, 102, 99]
        df = self._mk_df(closes)
        r = market_pattern.detect_false_breakout(df, lookback=60, recover_bars=5)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'false_breakout')
        self.assertEqual(r['resistance'], 100.0)

    # ---------- island_reversal ----------

    def test_island_reversal_down_confirmed(self):
        highs = [98, 100, 102, 115, 116, 117, 118, 105]
        lows = [93, 95, 97, 110, 111, 112, 113, 100]
        closes = [(h + l) / 2 for h, l in zip(highs, lows)]
        df = self._mk_df(closes, highs=highs, lows=lows)
        r = market_pattern.detect_island_reversal(df, lookback=30, gap_pct=0.5, max_island_bars=5)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'island_reversal_down')

    def test_island_reversal_none_on_continuous_move(self):
        highs = [100 + i for i in range(10)]
        lows = [95 + i for i in range(10)]
        closes = [(h + l) / 2 for h, l in zip(highs, lows)]
        df = self._mk_df(closes, highs=highs, lows=lows)
        r = market_pattern.detect_island_reversal(df, lookback=30, gap_pct=0.5, max_island_bars=5)
        self.assertIsNone(r)

    # ---------- range_breakout / range_breakdown ----------

    def test_range_breakout_confirmed(self):
        closes = [100] * 11 + [105]
        df = self._mk_df(closes)
        r = market_pattern.detect_range_breakout(df, lookback=10)
        self.assertIsNotNone(r)
        self.assertEqual(r['range_high'], 100.0)

    def test_range_breakout_not_first_day_no_repeat(self):
        closes = [100] * 10 + [105, 106]
        df = self._mk_df(closes)
        r = market_pattern.detect_range_breakout(df, lookback=10)
        self.assertIsNone(r)  # 105已經是第一天突破,106這天不是「第一次」

    def test_range_breakdown_confirmed(self):
        closes = [100] * 11 + [95]
        df = self._mk_df(closes)
        r = market_pattern.detect_range_breakdown(df, lookback=10)
        self.assertIsNotNone(r)
        self.assertEqual(r['range_low'], 100.0)

    # ---------- N字形 ----------

    def test_n_pattern_up_confirmed(self):
        closes = [100, 95, 90, 95, 100, 105, 110, 105, 100, 96, 92, 97, 102, 107, 112]
        df = self._mk_df(closes)
        r = market_pattern.detect_n_pattern_up(df, swing_window=2, lookback=90)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'n_up')
        self.assertEqual(r['high_a'], 110.0)

    def test_n_pattern_down_confirmed(self):
        base = [100, 95, 90, 95, 100, 105, 110, 105, 100, 96, 92, 97, 102, 107, 112]
        closes = [200 - c for c in base]
        df = self._mk_df(closes)
        r = market_pattern.detect_n_pattern_down(df, swing_window=2, lookback=90)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'n_down')

    def test_n_pattern_up_none_when_lower_low(self):
        closes = [100, 95, 90, 95, 100, 105, 110, 105, 100, 92, 88, 93, 98, 103, 108]
        df = self._mk_df(closes)
        r = market_pattern.detect_n_pattern_up(df, swing_window=2, lookback=90)
        self.assertIsNone(r)  # low2(88) < low1(90),沒有墊高,不是N字

    # ---------- triangle / wedge ----------

    def test_triangle_symmetrical(self):
        anchors = [115, 130, 100, 122, 108, 115, 112, 113]
        closes = self._zigzag(anchors, seg_len=4)
        df = self._mk_df(closes)
        r = market_pattern.detect_triangle_wedge(df, swing_window=2, lookback=len(closes), flat_pct=0.05)
        self.assertIsNotNone(r)
        self.assertEqual(r['pattern'], 'triangle_symmetrical')

    def test_triangle_none_on_plain_uptrend(self):
        closes = [100 + i for i in range(30)]
        df = self._mk_df(closes)
        r = market_pattern.detect_triangle_wedge(df, swing_window=2, lookback=30, flat_pct=0.05)
        self.assertIsNone(r)  # 單純上升,沒有波段低點可以組出三角形

    # ---------- evaluate_all ----------

    def test_evaluate_all_returns_list_and_respects_enabled_patterns(self):
        closes = [100] * 25 + [98, 96, 94, 96, 101]
        df = self._mk_df(closes)
        signals = market_pattern.evaluate_all(df, {'enabled_patterns': ['undercut_reverse']})
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['pattern'], 'undercut_reverse')
        signals_none = market_pattern.evaluate_all(df, {'enabled_patterns': ['double_top']})
        self.assertEqual(signals_none, [])

    def test_evaluate_all_handles_insufficient_data(self):
        df = self._mk_df([100, 101, 102])
        self.assertEqual(market_pattern.evaluate_all(df), [])

    def test_pattern_catalog_structure(self):
        self.assertGreaterEqual(len(market_pattern.PATTERN_CATALOG), 10)
        for entry in market_pattern.PATTERN_CATALOG:
            self.assertIn('id', entry)
            self.assertIn('label', entry)
            self.assertIn('category', entry)
            self.assertIn('desc', entry)


class TestChipsParser(unittest.TestCase):
    """【ADR-100】籌碼資料解析。樣本結構取自四個官方端點的實際回應。"""

    def test_to_num_handles_official_placeholders(self):
        # 官方「無資料」有多種寫法,全部要吃成 0 而不是拋例外
        for raw, want in [('1,234', 1234), ('-1,234', -1234), ('--', 0), ('---', 0),
                          ('', 0), (None, 0), ('0', 0), ('  5,000  ', 5000)]:
            self.assertEqual(chips_parser.to_num(raw), want, f"to_num({raw!r})")

    def test_roc_to_iso_covers_all_four_source_formats(self):
        # 四個來源的日期寫法各不相同,同一支函式都要吃得下
        self.assertEqual(chips_parser.roc_to_iso('20260724'), '2026-07-24')      # 證交所 date
        self.assertEqual(chips_parser.roc_to_iso('115/07/24'), '2026-07-24')     # 櫃買 date
        self.assertEqual(chips_parser.roc_to_iso('115年07月24日 三大法人'), '2026-07-24')
        self.assertEqual(chips_parser.roc_to_iso('2026/07/23'), '2026-07-23')    # 期交所 CSV
        self.assertIsNone(chips_parser.roc_to_iso(''))
        self.assertIsNone(chips_parser.roc_to_iso('無效字串'))

    def test_is_warrant_keeps_stocks_etfs_and_preferred_shares(self):
        # 權證要濾掉 (佔 T86 九成筆數),但特別股/債券ETF 不可誤殺
        for code in ('030001', '073456', '081234'):
            self.assertTrue(chips_parser.is_warrant(code), code)
        for code in ('2330', '0050', '00878', '006208', '2883B', '00945B', '00632R'):
            self.assertFalse(chips_parser.is_warrant(code), code)

    def test_parse_twse_market_inst_sums_three_investors(self):
        payload = {'stat': 'OK', 'date': '20260724',
                   'fields': ['單位名稱', '買進金額', '賣出金額', '買賣差額'],
                   'data': [['自營商(自行買賣)', '10', '5', '5'],
                            ['自營商(避險)', '20', '30', '-10'],
                            ['投信', '100', '40', '60'],
                            ['外資及陸資(不含外資自營商)', '900', '1,000', '-100'],
                            ['外資自營商', '0', '0', '0'],
                            ['合計', '1,030', '1,075', '-45']]}
        df = chips_parser.parse_twse_market_inst(payload)
        self.assertEqual(len(df), 1)
        r = df.iloc[0]
        self.assertEqual(r['Date'], '2026-07-24')
        self.assertEqual(r['Foreign'], -100)   # 外資及陸資 + 外資自營商
        self.assertEqual(r['Trust'], 60)
        self.assertEqual(r['Dealer'], -5)      # 自行 + 避險
        # 合計刻意用三者相加,不直接採官方「合計」列
        self.assertEqual(r['InstTotal'], -45)

    def test_parse_twse_market_inst_rejects_failed_payload(self):
        self.assertTrue(chips_parser.parse_twse_market_inst({'stat': 'ERROR'}).empty)
        self.assertTrue(chips_parser.parse_twse_market_inst(None).empty)

    def _t86_payload(self):
        row = ['2330', '台積電'] + ['0'] * 17
        row[4], row[7], row[10], row[11], row[18] = '1,000', '200', '300', '400', '1,900'
        warrant = ['030001', '某權證'] + ['9,999'] * 17
        return {'stat': 'OK', 'date': '20260724',
                'fields': ['f%d' % i for i in range(19)], 'data': [row, warrant]}

    def test_parse_twse_stock_inst_merges_foreign_and_drops_warrants(self):
        df = chips_parser.parse_twse_stock_inst(self._t86_payload())
        self.assertEqual(len(df), 1, "權證應被濾掉")
        r = df.iloc[0]
        self.assertEqual(r['Code'], '2330')
        self.assertEqual(r['Foreign'], 1200)   # 外陸資 1000 + 外資自營商 200
        self.assertEqual(r['Trust'], 300)
        self.assertEqual(r['Dealer'], 400)
        self.assertEqual(r['InstTotal'], 1900)

    def test_parse_twse_stock_inst_can_keep_warrants(self):
        df = chips_parser.parse_twse_stock_inst(self._t86_payload(), drop_warrants=False)
        self.assertEqual(len(df), 2)

    def _tpex_payload(self):
        # 7 組 × 3 欄 + 代號/名稱 + 合計,且滿足三條恆等式
        def row(code, name, f_ex, f_dlr, trust, d_self, d_hedge):
            f_tot, d_tot = f_ex + f_dlr, d_self + d_hedge
            vals = [code, name]
            for net in (f_ex, f_dlr, f_tot, trust, d_self, d_hedge, d_tot):
                vals += ['0', '0', str(net)]      # 買進/賣出留 0,只驗買賣超
            vals.append(str(f_tot + trust + d_tot))
            return vals
        return {'tables': [{'title': '三大法人買賣明細資訊', 'date': '115/07/24',
                            'fields': ['代號', '名稱'] + ['x'] * 22,
                            'data': [row('6488', '環球晶', 100, 5, 20, 3, 7),
                                     row('030001', '某權證', 1, 1, 1, 1, 1)]}]}

    def test_verify_tpex_layout_passes_on_consistent_data(self):
        ok, msg = chips_parser.verify_tpex_layout(self._tpex_payload())
        ok = (ok == chips_parser.LAYOUT_OK)
        self.assertTrue(ok, msg)

    def test_verify_tpex_layout_detects_column_reshuffle(self):
        """櫃買改版換欄位順序時要能被抓到,不能無聲產出錯誤籌碼。"""
        bad = self._tpex_payload()
        bad['tables'][0]['data'][0][10] = '99999'    # 破壞「外資合計=外資+外資自營商」
        ok, msg = chips_parser.verify_tpex_layout(bad)
        ok = (ok == chips_parser.LAYOUT_OK)
        self.assertFalse(ok)
        self.assertIn('不符恆等式', msg)

    def test_parse_tpex_stock_inst_uses_group_totals(self):
        df = chips_parser.parse_tpex_stock_inst(self._tpex_payload())
        self.assertEqual(len(df), 1, "權證應被濾掉")
        r = df.iloc[0]
        self.assertEqual(r['Date'], '2026-07-24')
        self.assertEqual(r['Code'], '6488')
        self.assertEqual(r['Foreign'], 105)    # 外資合計組
        self.assertEqual(r['Trust'], 20)
        self.assertEqual(r['Dealer'], 10)      # 自營商合計組
        self.assertEqual(r['InstTotal'], 135)

    def test_parse_twse_margin_reads_today_and_prev_balance(self):
        payload = {'stat': 'OK', 'date': '20260724', 'tables': [{
            'fields': ['項目', '買進', '賣出', '現金(券)償還', '前日餘額', '今日餘額'],
            'data': [['融資(交易單位)', '1', '2', '3', '9,349,915', '9,354,810'],
                     ['融券(交易單位)', '1', '2', '3', '208,646', '204,754'],
                     ['融資金額(仟元)', '1', '2', '3', '582,626,349', '577,059,537']]}]}
        r = chips_parser.parse_twse_margin(payload).iloc[0]
        self.assertEqual(r['Date'], '2026-07-24')
        self.assertEqual(r['MarginBalance'], 9354810)
        self.assertEqual(r['MarginPrevBalance'], 9349915)
        self.assertEqual(r['ShortBalance'], 204754)
        self.assertEqual(r['MarginAmountBalance'], 577059537)

    def _taifex_csv(self):
        head = ('日期,商品名稱,身份別,多方交易口數,多方交易契約金額,空方交易口數,'
                '空方交易契約金額,多空交易口數淨額,多空交易契約金額淨額,多方未平倉口數,'
                '多方未平倉契約金額,空方未平倉口數,空方未平倉契約金額,'
                '多空未平倉口數淨額,多空未平倉契約金額淨額')
        return (head + '\n'
                '2026/07/23,臺股期貨,自營商,1,2,3,4,-405,-3635089,5,6,7,8,739,6677060\n'
                '2026/07/23,電子期貨,投信,1,2,3,4,10,20,5,6,7,8,-30,-40\n')

    def test_parse_taifex_inst_reads_net_oi(self):
        df = chips_parser.parse_taifex_inst(self._taifex_csv())
        self.assertEqual(len(df), 2)
        r = df.iloc[0]
        self.assertEqual(r['Date'], '2026-07-23')
        self.assertEqual(r['Product'], '臺股期貨')
        self.assertEqual(r['Investor'], '自營商')
        self.assertEqual(r['NetOI'], 739)          # 多空未平倉口數淨額
        self.assertEqual(r['NetOIAmount'], 6677060)
        self.assertEqual(r['NetTrade'], -405)      # 多空交易口數淨額

    def test_parse_taifex_inst_filters_products(self):
        df = chips_parser.parse_taifex_inst(self._taifex_csv(), products=('臺股期貨',))
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]['Product'], '臺股期貨')

    def test_decode_taifex_handles_big5_and_utf8(self):
        # 期交所實測是 cp950;另有來源可能給 UTF-8 BOM,兩者都要能解
        self.assertIn('臺股期貨', chips_parser.decode_taifex('臺股期貨'.encode('cp950')))
        self.assertIn('臺股期貨', chips_parser.decode_taifex('臺股期貨'.encode('utf-8-sig')))

    def test_parse_taifex_inst_handles_empty_input(self):
        self.assertTrue(chips_parser.parse_taifex_inst(b'').empty)


class TestChipsStore(unittest.TestCase):
    """【ADR-100】籌碼本地儲存:冪等 upsert 與「已抓過就不重抓」的日期查詢。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _df(self, date, code='2330', total=100):
        return pd.DataFrame([{'Date': date, 'Code': code, 'Name': 'X',
                              'Foreign': 1, 'Trust': 2, 'Dealer': 3, 'InstTotal': total}])

    def test_upsert_creates_then_appends_new_date(self):
        p = chips_store.stock_inst_path(self.tmp, '2026-07-24')
        chips_store.upsert(p, self._df('2026-07-24'))
        chips_store.upsert(p, self._df('2026-07-23'))
        self.assertEqual(len(chips_store.load(p)), 2)
        self.assertEqual(chips_store.available_dates(p), {'2026-07-23', '2026-07-24'})

    def test_upsert_same_date_overwrites_not_duplicates(self):
        """重複匯入同一天不可產生重複列——否則「不重複抓取」的前提就破功。"""
        p = chips_store.stock_inst_path(self.tmp, '2026-07-24')
        chips_store.upsert(p, self._df('2026-07-24', total=100))
        chips_store.upsert(p, self._df('2026-07-24', total=999))
        df = chips_store.load(p)
        self.assertEqual(len(df), 1)
        self.assertEqual(int(df.iloc[0]['InstTotal']), 999, "同日期應被新資料覆蓋")

    def test_upsert_empty_df_leaves_file_untouched(self):
        p = chips_store.stock_inst_path(self.tmp, '2026-07-24')
        chips_store.upsert(p, self._df('2026-07-24'))
        chips_store.upsert(p, pd.DataFrame())
        self.assertEqual(len(chips_store.load(p)), 1)

    def test_code_leading_zeros_survive_roundtrip(self):
        """0050 / 00632R 這類代號不可被 CSV 讀回時變成數字 50。

        注意同一天的資料必須一次寫入:upsert 的語意是「同日期整批覆蓋」,
        分兩次寫同一天會讓後者取代前者 (這是刻意的冪等行為,見
        test_upsert_same_date_overwrites_not_duplicates)。
        """
        p = chips_store.stock_inst_path(self.tmp, '2026-07-24')
        day = pd.concat([self._df('2026-07-24', code='0050'),
                         self._df('2026-07-24', code='00632R'),
                         self._df('2026-07-24', code='2330')], ignore_index=True)
        chips_store.upsert(p, day)
        codes = set(chips_store.load(p)['Code'].astype(str))
        self.assertEqual(codes, {'0050', '00632R', '2330'})

    def test_stock_inst_path_splits_by_year_month(self):
        a = chips_store.stock_inst_path(self.tmp, '2026-07-24')
        b = chips_store.stock_inst_path(self.tmp, '2026-08-01')
        self.assertNotEqual(a, b)
        self.assertTrue(a.endswith('stock_inst_2026-07.csv'))
        self.assertTrue(b.endswith('stock_inst_2026-08.csv'))

    def test_months_between_spans_year_boundary(self):
        got = chips_store._months_between('2025-11-01', '2026-02-15')
        self.assertEqual(got, ['2025-11', '2025-12', '2026-01', '2026-02'])

    def test_available_dates_on_missing_file_is_empty(self):
        p = chips_store.stock_inst_path(self.tmp, '2099-01-01')
        self.assertEqual(chips_store.available_dates(p), set())

    def test_load_stock_inst_range_merges_across_month_files(self):
        chips_store.upsert(chips_store.stock_inst_path(self.tmp, '2026-06-30'),
                           self._df('2026-06-30'))
        chips_store.upsert(chips_store.stock_inst_path(self.tmp, '2026-07-01'),
                           self._df('2026-07-01'))
        df = chips_store.load_stock_inst_range(self.tmp, '2026-06-01', '2026-07-31')
        self.assertEqual(len(df), 2)
        # 區間外的資料不可被帶進來
        narrow = chips_store.load_stock_inst_range(self.tmp, '2026-07-01', '2026-07-31')
        self.assertEqual(len(narrow), 1)

    def test_stock_inst_available_dates_scans_all_month_files(self):
        for d in ('2026-06-30', '2026-07-01', '2026-07-02'):
            chips_store.upsert(chips_store.stock_inst_path(self.tmp, d), self._df(d))
        got = chips_store.stock_inst_available_dates(self.tmp, '2026-06-01', '2026-07-31')
        self.assertEqual(got, {'2026-06-30', '2026-07-01', '2026-07-02'})

    def test_futures_inst_range_merges_across_years(self):
        for d in ('2025-12-31', '2026-01-02'):
            df = pd.DataFrame([{'Date': d, 'Product': '臺股期貨', 'Investor': '外資及陸資',
                                'NetOI': 1, 'NetOIAmount': 2, 'NetTrade': 3, 'NetTradeAmount': 4}])
            chips_store.upsert(chips_store.futures_inst_path(self.tmp, d[:4]), df)
        out = chips_store.load_futures_inst_range(self.tmp, '2025-01-01', '2026-12-31')
        self.assertEqual(len(out), 2)


class TestChipsFeatures(unittest.TestCase):
    """【ADR-101】籌碼併入 df 與未來函數防護——本專案最需要被守住的正確性。"""

    def _bars(self, n=10, start='2026-07-13', volume=1_000_000.0):
        idx = pd.date_range(start, periods=n, freq='D')
        return pd.DataFrame({'Open': 100.0, 'High': 101.0, 'Low': 99.0,
                             'Close': 100.0, 'Volume': volume}, index=idx)

    def _chips(self, n=10, start='2026-07-13', step=1000):
        idx = pd.date_range(start, periods=n, freq='D')
        vals = [step * (i + 1) for i in range(n)]
        return pd.DataFrame({'Date': [d.strftime('%Y-%m-%d') for d in idx],
                             'Foreign': vals, 'Trust': [0] * n, 'Dealer': [0] * n,
                             'InstTotal': vals})

    def test_default_never_reads_same_day_chips(self):
        """**最重要的一條**:T 日只能讀到 T-1 日(含)以前的籌碼。

        籌碼是當日盤後才公布的。若 T 日 K 棒讀得到 T 日籌碼,回測績效會虛高
        到實盤無法複製 (look-ahead bias)。
        """
        out = chips_features.attach_chips(self._bars(), stock_chips=self._chips())
        col = out[chips_features.COL_FOREIGN]
        self.assertTrue(pd.isna(col.iloc[0]), "第一根沒有前一日籌碼,應為 NaN")
        self.assertEqual(col.iloc[1], 1000, "第二根應讀到第一天的籌碼")
        self.assertEqual(col.iloc[2], 2000, "第三根應讀到第二天的籌碼")
        # 逐根確認:讀到的值一律是「前一根日期」的籌碼,不是自己當日
        for i in range(1, 10):
            self.assertEqual(col.iloc[i], 1000 * i,
                             f"第{i}根不可讀到當日籌碼 {1000 * (i + 1)}")

    def test_allow_same_day_is_opt_in_only(self):
        """進階選項開啟後才讀當日 (僅供研究,呼叫端須標示含未來函數)。"""
        out = chips_features.attach_chips(self._bars(), stock_chips=self._chips(),
                                          allow_same_day=True)
        col = out[chips_features.COL_FOREIGN]
        self.assertEqual(col.iloc[0], 1000, "允許當日時第一根就該讀到當日籌碼")
        self.assertEqual(col.iloc[5], 6000)

    def test_gap_in_chips_carries_last_known_value(self):
        """籌碼中間缺日 (假日/停牌) 時沿用最後一筆已知值,不可跳成 NaN。"""
        chips = self._chips(n=3)                      # 只有 7/13~7/15
        out = chips_features.attach_chips(self._bars(n=8), stock_chips=chips)
        col = out[chips_features.COL_FOREIGN]
        self.assertEqual(col.iloc[3], 3000, "7/16 應沿用 7/15 的籌碼")
        self.assertEqual(col.iloc[7], 3000, "之後仍沿用最後一筆已知值")

    def test_no_chips_gives_nan_columns_not_crash(self):
        out = chips_features.attach_chips(self._bars(), stock_chips=None)
        self.assertIn(chips_features.COL_FOREIGN, out.columns)
        self.assertFalse(chips_features.has_chip_data(out, [chips_features.COL_FOREIGN]))

    def test_attach_does_not_mutate_input(self):
        df = self._bars()
        chips_features.attach_chips(df, stock_chips=self._chips())
        self.assertNotIn(chips_features.COL_FOREIGN, df.columns, "不可就地修改傳入的 df")

    def test_futures_chips_split_by_investor(self):
        idx = pd.date_range('2026-07-13', periods=5, freq='D')
        rows = []
        for i, d in enumerate(idx):
            for inv, oi in (('外資及陸資', -1000 * (i + 1)), ('投信', 500), ('自營商', 20)):
                rows.append({'Date': d.strftime('%Y-%m-%d'), 'Product': '臺股期貨',
                             'Investor': inv, 'NetOI': oi})
        out = chips_features.attach_chips(self._bars(n=5), futures_chips=pd.DataFrame(rows),
                                          futures_product='臺股期貨')
        self.assertEqual(out[chips_features.COL_FUT_FOREIGN_OI].iloc[1], -1000)
        self.assertEqual(out[chips_features.COL_FUT_TRUST_OI].iloc[1], 500)
        self.assertEqual(out[chips_features.COL_FUT_DEALER_OI].iloc[1], 20)

    def test_series_of_raises_when_missing(self):
        """缺資料要拋 ChipDataMissing,不可靜默回 False——「沒資料」與
        「條件沒到」對使用者的意義完全不同 (ADR-065 的教訓)。"""
        with self.assertRaises(chips_features.ChipDataMissing):
            chips_features.series_of(self._bars(), chips_features.COL_FOREIGN)
        empty = chips_features.attach_chips(self._bars(), stock_chips=None)
        with self.assertRaises(chips_features.ChipDataMissing):
            chips_features.series_of(empty, chips_features.COL_FOREIGN)

    def test_consecutive_helpers(self):
        s = pd.Series([1, 2, 3, 4.0])
        self.assertTrue(chips_features.consecutive_net(s, 3, positive=True))
        self.assertFalse(chips_features.consecutive_net(s, 3, positive=False))
        self.assertFalse(chips_features.consecutive_net(pd.Series([1, -1, 2.0]), 3, positive=True))
        # NaN 一律視為不成立 (沒資料 ≠ 條件達成)
        self.assertFalse(chips_features.consecutive_net(pd.Series([1, float('nan'), 3.0]), 3))
        self.assertTrue(chips_features.consecutive_decreasing(pd.Series([5, 4, 3, 2.0]), 3))
        self.assertFalse(chips_features.consecutive_decreasing(pd.Series([5, 4, 5, 2.0]), 3))
        self.assertTrue(chips_features.turned_positive(pd.Series([-1, 5.0])))
        self.assertFalse(chips_features.turned_positive(pd.Series([1, 5.0])))
        self.assertTrue(chips_features.turned_negative(pd.Series([1, -5.0])))

    def test_timeframe_gate(self):
        self.assertTrue(chips_features.timeframe_supports_chips('日K'))
        self.assertTrue(chips_features.timeframe_supports_chips('週K'))
        self.assertFalse(chips_features.timeframe_supports_chips('5分K'))
        self.assertFalse(chips_features.timeframe_supports_chips('60分K'))


class TestChipConditions(unittest.TestCase):
    """【ADR-101】籌碼條件在策略引擎中的行為。"""

    def _df_with_chips(self, foreign, trust=None, inst=None, margin=None,
                       fut_oi=None, volume=1_000_000.0):
        n = len(foreign)
        idx = pd.date_range('2026-07-01', periods=n, freq='D')
        df = pd.DataFrame({'Open': 100.0, 'High': 101.0, 'Low': 99.0,
                           'Close': 100.0, 'Volume': volume}, index=idx)
        df[chips_features.COL_FOREIGN] = foreign
        df[chips_features.COL_TRUST] = trust if trust is not None else [0] * n
        df[chips_features.COL_DEALER] = [0] * n
        df[chips_features.COL_INST_TOTAL] = inst if inst is not None else foreign
        df[chips_features.COL_MARGIN] = margin if margin is not None else [0] * n
        df[chips_features.COL_FUT_FOREIGN_OI] = fut_oi if fut_oi is not None else [0] * n
        return df

    def _run(self, cid, df, params=None):
        return strategy_engine.CONDITIONS[cid][2](df, params or {})

    def test_foreign_buy_and_sell_streak(self):
        up = self._df_with_chips([100, 200, 300, 400])
        self.assertTrue(self._run('chip_foreign_buy_streak', up, {'n': 3}))
        self.assertFalse(self._run('chip_foreign_sell_streak', up, {'n': 3}))
        mixed = self._df_with_chips([100, -200, 300, 400])
        self.assertFalse(self._run('chip_foreign_buy_streak', mixed, {'n': 3}))
        down = self._df_with_chips([-1, -2, -3, -4])
        self.assertTrue(self._run('chip_foreign_sell_streak', down, {'n': 3}))

    def test_inst_threshold_uses_lots_not_shares(self):
        """參數是「張」,資料是「股」,比較時要換算 (500 張 = 500,000 股)。"""
        df = self._df_with_chips([0, 0, 600_000])
        self.assertTrue(self._run('chip_inst_buy_over', df, {'value': 500}))
        self.assertFalse(self._run('chip_inst_buy_over', df, {'value': 700}))
        df2 = self._df_with_chips([0, 0, -600_000])
        self.assertTrue(self._run('chip_inst_sell_over', df2, {'value': -500}))

    def test_foreign_turn_buy_only_on_flip_bar(self):
        flip = self._df_with_chips([-100, -50, 200])
        self.assertTrue(self._run('chip_foreign_turn_buy', flip))
        cont = self._df_with_chips([-100, 50, 200])
        self.assertFalse(self._run('chip_foreign_turn_buy', cont), "已翻多的隔天不該再成立")

    def test_foreign_volume_ratio(self):
        df = self._df_with_chips([0, 0, 150_000], volume=1_000_000.0)
        self.assertTrue(self._run('chip_foreign_vol_ratio', df, {'value': 10}))
        self.assertFalse(self._run('chip_foreign_vol_ratio', df, {'value': 20}))

    def test_margin_decrease_streak(self):
        df = self._df_with_chips([0] * 5, margin=[100, 90, 80, 70, 60])
        self.assertTrue(self._run('chip_margin_decrease_streak', df, {'n': 3}))
        self.assertFalse(self._run('chip_margin_increase_streak', df, {'n': 3}))

    def test_futures_foreign_oi_conditions(self):
        df = self._df_with_chips([0] * 3, fut_oi=[-500, -200, 800])
        self.assertTrue(self._run('chip_fut_foreign_oi_above', df, {'value': 0}))
        self.assertFalse(self._run('chip_fut_foreign_oi_below', df, {'value': 0}))
        self.assertTrue(self._run('chip_fut_foreign_turn_long', df))
        self.assertFalse(self._run('chip_fut_foreign_turn_short', df))

    def test_missing_chips_reported_via_errors_not_silent(self):
        """沒有籌碼欄位時,eval_conditions 要把原因收進 errors 讓 GUI 能提示。"""
        plain = pd.DataFrame({'Open': 1.0, 'High': 1.0, 'Low': 1.0, 'Close': 1.0,
                              'Volume': 1.0}, index=pd.date_range('2026-07-01', periods=5))
        errors = []
        ok, _ = strategy_engine.eval_conditions(
            plain, [{'type': 'chip_foreign_buy_streak', 'params': {'n': 3}}], errors=errors)
        self.assertFalse(ok)
        self.assertEqual(len(errors), 1)
        self.assertIn('ChipDataMissing', errors[0][1])

    def test_strategy_uses_chips_detection(self):
        s = strategy_engine.new_strategy()
        s['entry'] = [{'type': 'ma_cross_up', 'params': {}}]
        self.assertFalse(strategy_engine.strategy_uses_chips(s))
        s['entry'].append({'type': 'chip_foreign_buy_streak', 'params': {'n': 3}})
        self.assertTrue(strategy_engine.strategy_uses_chips(s))
        s2 = strategy_engine.new_strategy()
        s2['exit_signals'] = [{'type': 'chip_foreign_turn_sell', 'params': {}}]
        self.assertTrue(strategy_engine.strategy_uses_chips(s2), "出場條件也要算")

    def test_validate_blocks_chip_conditions_on_intraday(self):
        """分K 策略用籌碼條件應在存檔時被擋下並說明原因。"""
        s = strategy_engine.new_strategy()
        s.update({'name': 'X', 'symbol': '2330', 'timeframe': '5分K', 'qty': 1,
                  'direction': '做多', 'stop_loss_pct': 2.0,
                  'entry': [{'type': 'chip_foreign_buy_streak', 'params': {'n': 3}}]})
        ok, why = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn('籌碼條件只能用於', why)
        s['timeframe'] = '日K'
        ok2, why2 = strategy_engine.validate_strategy(s)
        self.assertTrue(ok2, why2)

    def test_new_strategy_defaults_to_no_lookahead(self):
        self.assertFalse(strategy_engine.new_strategy()['chips_allow_same_day'],
                         "預設必須是「不讀當日籌碼」")


class TestVolumeProfile(unittest.TestCase):
    """【ADR-102】量價關係推算支撐壓力。"""

    def _df(self, rows, start='2026-01-01'):
        return pd.DataFrame(rows, index=pd.date_range(start, periods=len(rows), freq='B'))

    def _concentrated(self, n=150, hot=104.5, hot_vol=5_000_000, cold_vol=500_000):
        """構造一段「104.5 附近成交量特別大」的資料,POC 應落在該處。"""
        rng = np.random.RandomState(3)
        rows = []
        for i in range(n):
            if i % 3 == 0:
                base, vol = hot, hot_vol
            else:
                base, vol = 100 + rng.rand() * 10, cold_vol
            rows.append({'Open': base, 'High': base + 0.6, 'Low': base - 0.6,
                         'Close': base, 'Volume': vol})
        return self._df(rows)

    def test_poc_lands_on_high_volume_price(self):
        r = volume_profile.find_levels(self._concentrated(), asset_type='stock',
                                       raw_symbol='2330')
        self.assertIsNotNone(r)
        self.assertLess(abs(r['profile']['poc'] - 104.5), 1.5,
                        f"POC 應落在成交密集區附近,實際 {r['profile']['poc']}")

    def test_value_area_contains_poc_and_covers_target_pct(self):
        prof = volume_profile.compute_profile(self._concentrated(), asset_type='stock',
                                              raw_symbol='2330')
        self.assertLessEqual(prof['val'], prof['poc'])
        self.assertGreaterEqual(prof['vah'], prof['poc'])
        # 價值區內的量應達到設定比例
        lo, hi = prof['val'], prof['vah']
        inside = sum(v for c, v in zip(prof['bin_centers'], prof['bin_volumes'])
                     if lo <= c <= hi)
        self.assertGreaterEqual(inside / prof['total_volume'], 0.65)

    def test_volume_spread_across_bar_range_not_only_close(self):
        """一根長紅棒的量要分攤到 High~Low 全程,不可全部記在收盤價那一箱。

        全記在收盤價是常見的簡化實作,但會嚴重失真:成交是發生在整段路徑上的。
        """
        df = self._df([{'Open': 100, 'High': 110, 'Low': 100, 'Close': 110,
                        'Volume': 1000}])
        prof = volume_profile.compute_profile(df, asset_type='stock', raw_symbol='2330',
                                              bins=10)
        nonzero = [v for v in prof['bin_volumes'] if v > 0]
        self.assertGreater(len(nonzero), 1, "量應分攤到多個價格箱")
        self.assertAlmostEqual(sum(prof['bin_volumes']), 1000, places=4,
                               msg="分攤後總量必須守恆")

    def test_no_contradictory_hvn_and_lvn_on_same_level(self):
        """同一條線不可同時標成「高量節點」與「低量真空帶」(實測踩過)。"""
        r = volume_profile.find_levels(self._concentrated(), asset_type='stock',
                                       raw_symbol='2330')
        for lv in r['levels']:
            self.assertNotIn(volume_profile.KIND_LVN, lv['kind_all'],
                             "支撐壓力清單不該混入真空帶")
        lvn_prices = {z['price'] for z in r['lvn_zones']}
        level_prices = {lv['price'] for lv in r['levels']}
        self.assertFalse(lvn_prices & level_prices, "真空帶不可與支撐壓力同價位")

    def test_levels_have_no_duplicate_prices(self):
        r = volume_profile.find_levels(self._concentrated(), asset_type='stock',
                                       raw_symbol='2330')
        prices = [lv['price'] for lv in r['levels']]
        self.assertEqual(len(prices), len(set(prices)), "同一價位不可出現兩條線")

    def test_role_split_by_current_price(self):
        r = volume_profile.find_levels(self._concentrated(), asset_type='stock',
                                       raw_symbol='2330')
        cur, tick = r['current'], r['tick']
        for lv in r['levels']:
            if lv['role'] == volume_profile.ROLE_RESISTANCE:
                self.assertGreater(lv['price'], cur)
            elif lv['role'] == volume_profile.ROLE_SUPPORT:
                self.assertLess(lv['price'], cur)
            else:
                self.assertLess(abs(lv['price'] - cur), tick)

    def test_prices_aligned_to_tick_for_stock_and_future(self):
        """【鐵則7】輸出點位必須是合法檔位,不可出現 605.3 這種非法價。"""
        r = volume_profile.find_levels(self._concentrated(), asset_type='stock',
                                       raw_symbol='2330')
        for lv in r['levels']:
            t = tick_rules.get_tick(lv['price'], 'stock', '2330')
            aligned = round(lv['price'] / t) * t
            self.assertAlmostEqual(aligned, lv['price'], places=6,
                                   msg=f"{lv['price']} 未對齊 tick {t}")
        rows = [{'Open': 20000 + i * 3, 'High': 20030 + i * 3, 'Low': 19970 + i * 3,
                 'Close': 20000 + i * 3, 'Volume': 100000} for i in range(60)]
        rf = volume_profile.find_levels(self._df(rows), asset_type='future',
                                        raw_symbol='TXF')
        self.assertEqual(rf['tick'], 1.0, "期貨 tick 應為 1 點")
        for lv in rf['levels']:
            self.assertEqual(lv['price'], round(lv['price']), "期貨點位應為整數")

    def test_swing_points_require_volume_confirmation(self):
        """沒有放量的轉折點不採計——這是「量價關係」而非純價格型態。"""
        rows = []
        for i in range(21):
            price = 100 + (5 if i == 10 else 0)      # 第 10 根是價格高點
            rows.append({'Open': price, 'High': price, 'Low': price - 1,
                         'Close': price, 'Volume': 1000})
        highs, _ = volume_profile.swing_points(self._df(rows), lookback=3, volume_mult=1.3)
        self.assertEqual(highs, [], "沒有放量的高點不該被採計")
        rows[10]['Volume'] = 10_000                   # 同一根給大量
        highs2, _ = volume_profile.swing_points(self._df(rows), lookback=3, volume_mult=1.3)
        self.assertIn(105.0, highs2, "放量的高點應被採計")

    def test_handles_missing_volume_column(self):
        """沒有 Volume 欄位時要退化成等權重而不是崩潰 (指數常見)。"""
        rows = [{'Open': 100 + i, 'High': 101 + i, 'Low': 99 + i, 'Close': 100 + i}
                for i in range(30)]
        r = volume_profile.find_levels(self._df(rows), asset_type='index_tw',
                                       raw_symbol='^TWII')
        self.assertIsNotNone(r)
        self.assertTrue(r['levels'])

    def test_returns_none_on_unusable_data(self):
        self.assertIsNone(volume_profile.compute_profile(None))
        self.assertIsNone(volume_profile.compute_profile(pd.DataFrame()))
        flat = self._df([{'Open': 5, 'High': 5, 'Low': 5, 'Close': 5, 'Volume': 10}] * 5)
        self.assertIsNone(volume_profile.compute_profile(flat),
                          "價格全平無法形成價格箱,應回 None 而不是除以零")

    def test_zero_volume_returns_none(self):
        rows = [{'Open': 100 + i, 'High': 101 + i, 'Low': 99 + i, 'Close': 100 + i,
                 'Volume': 0} for i in range(20)]
        self.assertIsNone(volume_profile.compute_profile(self._df(rows)))

    def test_summarize_is_readable_and_safe(self):
        r = volume_profile.find_levels(self._concentrated(), asset_type='stock',
                                       raw_symbol='2330')
        text = volume_profile.summarize(r)
        self.assertIn('POC', text)
        self.assertIn('現價', text)
        self.assertIsInstance(volume_profile.summarize(None), str)

    def test_max_levels_respected(self):
        r = volume_profile.find_levels(self._concentrated(), asset_type='stock',
                                       raw_symbol='2330', max_levels=3)
        self.assertLessEqual(len(r['levels']), 3)


class TestFundamentalParser(unittest.TestCase):
    """【ADR-103】基本面解析。"""

    def test_to_float_distinguishes_missing_from_zero(self):
        """**最關鍵的一條**:本益比「-」代表虧損(沒有本益比),不可當成 0。

        若當成 0,使用者設「本益比 <= 15」會把虧損公司全選進來——嚴重選股錯誤。
        """
        self.assertIsNone(fundamental_parser.to_float('-'))
        self.assertIsNone(fundamental_parser.to_float('--'))
        self.assertIsNone(fundamental_parser.to_float(''))
        self.assertIsNone(fundamental_parser.to_float(None))
        self.assertIsNone(fundamental_parser.to_float('N/A'))
        self.assertEqual(fundamental_parser.to_float('0'), 0.0)
        self.assertEqual(fundamental_parser.to_float('1,234.5'), 1234.5)
        self.assertEqual(fundamental_parser.to_float('12.5%'), 12.5)

    def test_is_listed_equity_filters_warrants(self):
        for c in ('2330', '0050', '00878', '006208'):
            self.assertTrue(fundamental_parser.is_listed_equity(c), c)
        for c in ('030001', '073456', '2330A'):
            self.assertFalse(fundamental_parser.is_listed_equity(c), c)

    def test_parse_twse_valuation(self):
        payload = {'stat': 'OK',
                   'fields': ['證券代號', '證券名稱', '收盤價', '殖利率(%)', '股利年度',
                              '本益比', '股價淨值比', '財報年/季'],
                   'data': [['2330', '台積電', '2350.00', '1.20', 114, '31.59', '10.34', '115/1'],
                            ['1101', '台泥', '23.80', '3.36', 114, '-', '0.76', '115/1'],
                            ['030001', '某權證', '1.0', '0', 114, '5', '1', '115/1']]}
        df = fundamental_parser.parse_twse_valuation(payload)
        self.assertEqual(len(df), 2, "權證應被濾掉")
        tsmc = df[df['Code'] == '2330'].iloc[0]
        self.assertEqual(tsmc['PE'], 31.59)
        self.assertEqual(tsmc['Close'], 2350.0)
        cement = df[df['Code'] == '1101'].iloc[0]
        self.assertTrue(pd.isna(cement['PE']), "本益比「-」應為空值而不是 0")

    def test_parse_mi_index_picks_largest_table(self):
        """MI_INDEX 有多張表,要挑「筆數最大且含收盤價」的那張,不可寫死索引。"""
        payload = {'stat': 'OK', 'date': '20260724', 'tables': [
            {'fields': ['指數', '收盤指數'], 'data': [['發行量加權股價指數', '20000']]},
            {'fields': ['證券代號', '證券名稱', '成交股數', '成交筆數', '成交金額',
                        '開盤價', '最高價', '最低價', '收盤價'],
             'data': [['2330', '台積電', '1,000', '5', '100', '2375', '2395', '2290', '2290'],
                      ['030001', '權證', '1', '1', '1', '1', '1', '1', '1']]},
        ]}
        df = fundamental_parser.parse_twse_mi_index(payload)
        self.assertEqual(len(df), 1, "權證應被濾掉")
        r = df.iloc[0]
        self.assertEqual(r['Date'], '2026-07-24')
        self.assertEqual(r['Close'], 2290.0)
        self.assertEqual(r['Open'], 2375.0)

    def test_parse_monthly_revenue_uses_official_yoy(self):
        rows = [{'公司代號': '2330', '公司名稱': '台積電',
                 '營業收入-當月營收': '100000',
                 '營業收入-去年同月增減(%)': '67.87',
                 '營業收入-上月比較增減(%)': '5.2'}]
        df = fundamental_parser.parse_monthly_revenue(rows)
        self.assertEqual(df.iloc[0]['RevenueYoYPct'], 67.87)
        self.assertEqual(df.iloc[0]['RevenueMoMPct'], 5.2)

    def test_parse_income_statement_computes_gross_margin(self):
        rows = [{'公司代號': '2330', '營業收入': '1000', '營業毛利（毛損）淨額': '662.5',
                 '基本每股盈餘（元）': '22.08'},
                {'公司代號': '9999', '營業收入': '0', '營業毛利（毛損）淨額': '10',
                 '基本每股盈餘（元）': '1'}]
        df = fundamental_parser.parse_income_statement(rows)
        a = df[df['Code'] == '2330'].iloc[0]
        self.assertEqual(a['EPS'], 22.08)
        self.assertAlmostEqual(a['GrossMarginPct'], 66.25, places=2)
        b = df[df['Code'] == '9999'].iloc[0]
        # pandas 會把數值欄的 None 存成 NaN,兩者都代表「無資料」;
        # 關鍵是**不可以是 0** (0 會讓「毛利率 >= 0」這種條件誤判成通過)。
        self.assertTrue(pd.isna(b['GrossMarginPct']),
                        f"營收為 0 時毛利率應為空,實際 {b['GrossMarginPct']!r}")

    def test_derive_eps_from_pe_is_exact_definition(self):
        """EPS = 收盤價 ÷ 本益比,這是本益比的定義,不是估計。"""
        self.assertAlmostEqual(fundamental_parser.derive_eps_from_pe(100.0, 10.0), 10.0)
        self.assertIsNone(fundamental_parser.derive_eps_from_pe(100.0, 0),
                          "本益比 0/負 (虧損) 不可推出 EPS")
        self.assertIsNone(fundamental_parser.derive_eps_from_pe(None, 10.0))

    def test_derive_roe(self):
        # 每股淨值 = 100/2 = 50;ROE = 5/50 = 10%
        self.assertAlmostEqual(fundamental_parser.derive_roe(5.0, 2.0, 100.0), 10.0)
        self.assertIsNone(fundamental_parser.derive_roe(5.0, 0, 100.0))
        self.assertIsNone(fundamental_parser.derive_roe(None, 2.0, 100.0))

    def test_income_statement_reads_both_chinese_and_english_code_field(self):
        """上市用中文欄名「公司代號」、上櫃用英文「SecuritiesCompanyCode」。

        只讀其中一種會**整批漏掉另一個市場**且不報錯 (筆數少了但看不出來)
        ——這是實測抓到的真實 bug:上櫃 884 筆損益表原本全被濾掉,只剩 7 筆。
        """
        twse = [{'公司代號': '2330', '營業收入': '1000',
                 '營業毛利（毛損）淨額': '660', '基本每股盈餘（元）': '22.08'}]
        tpex = [{'SecuritiesCompanyCode': '1240', 'CompanyName': '茂生農經',
                 '營業收入': '689952', '營業毛利（毛損）淨額': '88232',
                 '基本每股盈餘（元）': '0.89'}]
        a = fundamental_parser.parse_income_statement(twse)
        b = fundamental_parser.parse_income_statement(tpex)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1, "英文欄名的上櫃資料不可被漏掉")
        self.assertEqual(b.iloc[0]['Code'], '1240')
        self.assertEqual(b.iloc[0]['EPS'], 0.89)

    def test_financial_holding_has_eps_but_no_gross_margin(self):
        """金控/保險沒有「營業收入/營業毛利」欄位,毛利率為空是正確的。

        金控股在 _fh 變體而不是 _ci——只抓 _ci 會漏掉富邦金/國泰金等全部
        金控股 (實測抓到的缺口)。
        """
        fh = [{'公司代號': '2881', '公司名稱': '富邦金', '淨收益': '100',
               '基本每股盈餘（元）': '2.40'}]
        df = fundamental_parser.parse_income_statement(fh)
        self.assertEqual(df.iloc[0]['EPS'], 2.40)
        self.assertTrue(pd.isna(df.iloc[0]['GrossMarginPct']),
                        "金控沒有毛利率概念,應為空而不是 0")

    def test_balance_sheet_derives_equity_from_assets_minus_debts(self):
        """上櫃資產負債表沒有「權益總額」,用會計恆等式推導。"""
        twse = [{'公司代號': '2330', '權益總額': '5000'}]
        tpex = [{'SecuritiesCompanyCode': '1240', '資產總計': '3000', '負債總計': '1200'}]
        self.assertEqual(fundamental_parser.parse_balance_sheet(twse).iloc[0]['Equity'], 5000)
        self.assertEqual(fundamental_parser.parse_balance_sheet(tpex).iloc[0]['Equity'], 1800)

    def test_roc_date_to_iso(self):
        """櫃買日行情的 Date 是民國 7 碼;沒解析它會讓整批資料日期為空。"""
        self.assertEqual(fundamental_parser.roc_date_to_iso('1150724'), '2026-07-24')
        self.assertEqual(fundamental_parser.roc_date_to_iso('20260724'), '2026-07-24')
        self.assertIsNone(fundamental_parser.roc_date_to_iso(''))

    def test_tpex_daily_history_uses_field_names_not_positions(self):
        """【ADR-104】櫃買歷史行情有兩個陷阱,都必須靠「欄位名」而非位置解決:

        1. 欄位名帶前後空白:'收盤 '、' 成交金額(元)'、'成交股數  '。
        2. 欄位順序是「收盤、漲跌、開盤、最高、最低」——**收盤在開盤前面**,
           與一般 OHLC 慣例相反。寫死索引會讓開高低收全部錯位,而且數字看起來
           仍然「像價格」,不會報錯。
        """
        payload = {'date': '20260724', 'tables': [{
            'fields': ['代號', '名稱', '收盤 ', '漲跌', '開盤 ', '最高 ', '最低',
                       '成交股數  ', ' 成交金額(元)'],
            'data': [['6488', '環球晶', '1030.00', '-5.00', '1020.00', '1035.00',
                      '1010.00', '1,234,000', '999'],
                     ['030001', '某權證', '1', '0', '1', '1', '1', '1', '1']]}]}
        df = fundamental_parser.parse_tpex_daily_history(payload)
        self.assertEqual(len(df), 1, "權證應被濾掉")
        r = df.iloc[0]
        self.assertEqual(r['Date'], '2026-07-24')
        self.assertEqual(r['Code'], '6488')
        # 關鍵:開/收不可互換 (欄位順序陷阱)
        self.assertEqual(r['Open'], 1020.0)
        self.assertEqual(r['Close'], 1030.0)
        self.assertEqual(r['High'], 1035.0)
        self.assertEqual(r['Low'], 1010.0)
        self.assertEqual(r['Volume'], 1234000.0)
        # OHLC 邏輯自洽 (錯位時這裡會失敗)
        self.assertGreaterEqual(r['High'], max(r['Open'], r['Close']))
        self.assertLessEqual(r['Low'], min(r['Open'], r['Close']))

    def test_tpex_daily_history_handles_missing_fields(self):
        self.assertTrue(fundamental_parser.parse_tpex_daily_history(None).empty)
        self.assertTrue(fundamental_parser.parse_tpex_daily_history({}).empty)
        bad = {'tables': [{'fields': ['代號', '名稱'], 'data': [['6488', 'X']]}]}
        self.assertTrue(fundamental_parser.parse_tpex_daily_history(bad).empty,
                        "缺必要欄位時應回空表而不是抓錯欄")

    def test_tpex_daily_all_parses_own_date_field(self):
        rows = [{'Date': '1150724', 'SecuritiesCompanyCode': '6488', 'CompanyName': '環球晶',
                 'Open': '1020', 'High': '1035', 'Low': '1010', 'Close': '1030',
                 'TradingShares': '5000'}]
        df = fundamental_parser.parse_tpex_daily_all(rows)
        self.assertEqual(df.iloc[0]['Date'], '2026-07-24',
                         "呼叫端沒傳日期時要能自己從 Date 欄解析")
        self.assertEqual(df.iloc[0]['Close'], 1030.0)

    def test_merge_fills_eps_from_pe_when_income_missing(self):
        """上櫃沒有損益表端點,EPS 要能從 收盤價/本益比 補上。"""
        val = pd.DataFrame([{'Code': '6488', 'Name': 'A', 'Close': 100.0,
                             'PE': 10.0, 'PB': 2.0, 'YieldPct': 3.0}])
        m = fundamental_parser.merge_fundamentals(valuation=val)
        r = m.iloc[0]
        self.assertAlmostEqual(float(r['EPS']), 10.0)
        self.assertAlmostEqual(float(r['ROEPct']), 20.0)

    def test_merge_handles_mixed_missing_eps_without_dtype_error(self):
        """【實測踩到】一批股票缺 EPS 時,新版 pandas 對 float64 欄位指派含
        None 的 list 會直接拋 TypeError 而不是轉成 NaN。這個崩潰只在「部分
        缺、部分有」的資料組合才會出現,小樣本很容易漏掉。"""
        val = pd.DataFrame([
            {'Code': '1101', 'Name': 'A', 'Close': 100.0, 'PE': None, 'PB': 1.0, 'YieldPct': 1.0},
            {'Code': '2330', 'Name': 'B', 'Close': 200.0, 'PE': 10.0, 'PB': 2.0, 'YieldPct': 2.0},
            {'Code': '6488', 'Name': 'C', 'Close': 50.0, 'PE': None, 'PB': 1.5, 'YieldPct': 3.0},
        ])
        inc = pd.DataFrame([{'Code': '2330', 'EPS': 22.08, 'GrossMarginPct': 60.0}])
        m = fundamental_parser.merge_fundamentals(valuation=val, income=inc)
        self.assertEqual(len(m), 3)
        self.assertAlmostEqual(float(m[m.Code == '2330'].iloc[0]['EPS']), 22.08)
        self.assertTrue(pd.isna(m[m.Code == '1101'].iloc[0]['EPS']),
                        "本益比為空 (虧損) 時不可推出 EPS")

    def test_monthly_revenue_carries_industry(self):
        rows = [{'公司代號': '2330', '公司名稱': '台積電', '產業別': '半導體業',
                 '營業收入-當月營收': '100', '營業收入-去年同月增減(%)': '67.87'},
                {'SecuritiesCompanyCode': '1240', 'CompanyName': '茂生農經',
                 '產業別': '農業科技', '營業收入-當月營收': '50',
                 '營業收入-去年同月增減(%)': '24.2'}]
        df = fundamental_parser.parse_monthly_revenue(rows)
        self.assertEqual(len(df), 2, "上市中文欄名與上櫃英文欄名都要讀得到")
        self.assertEqual(df[df.Code == '2330'].iloc[0]['Industry'], '半導體業')
        self.assertEqual(df[df.Code == '1240'].iloc[0]['Industry'], '農業科技')


class TestMarketScreener(unittest.TestCase):
    """【ADR-103】選股引擎。"""

    def _fund(self):
        return pd.DataFrame([
            {'Code': '2330', 'Name': '台積電', 'Close': 2350.0, 'PE': 31.59, 'PB': 10.34,
             'YieldPct': 1.2, 'EPS': 22.08, 'GrossMarginPct': 66.25,
             'RevenueYoYPct': 67.9, 'RevenueMoMPct': 5.0, 'MonthRevenue': 1e6,
             'Equity': 1e9, 'ROEPct': 9.72},
            {'Code': '1102', 'Name': '亞泥', 'Close': 32.75, 'PE': 10.99, 'PB': 0.66,
             'YieldPct': 7.02, 'EPS': 2.98, 'GrossMarginPct': 18.0,
             'RevenueYoYPct': 3.0, 'RevenueMoMPct': 1.0, 'MonthRevenue': 1e5,
             'Equity': 1e8, 'ROEPct': 6.0},
            {'Code': '9999', 'Name': '虧損股', 'Close': 10.0, 'PE': None, 'PB': 0.5,
             'YieldPct': 0.0, 'EPS': None, 'GrossMarginPct': None,
             'RevenueYoYPct': -50.0, 'RevenueMoMPct': -10.0, 'MonthRevenue': 1e3,
             'Equity': 1e6, 'ROEPct': None},
        ])

    def test_value_screen_picks_low_pe_high_yield(self):
        r = market_screener.screen(
            self._fund(), None,
            fundamental_conds=[{'field': 'pe', 'op': market_screener.OP_LTE, 'value': 15},
                               {'field': 'yield', 'op': market_screener.OP_GTE, 'value': 5}])
        codes = [x['code'] for x in r['rows']]
        self.assertEqual(codes, ['1102'])

    def test_missing_value_never_passes(self):
        """**最重要**:本益比為空(虧損)絕不可通過「本益比<=15」。

        若把空值當成跳過,虧損股會被選進價值股清單——這是選股工具最嚴重的
        錯誤類型,因為使用者會照著它下單。
        """
        r = market_screener.screen(
            self._fund(), None,
            fundamental_conds=[{'field': 'pe', 'op': market_screener.OP_LTE, 'value': 999}])
        codes = [x['code'] for x in r['rows']]
        self.assertNotIn('9999', codes, "本益比無資料的虧損股不可通過本益比條件")
        self.assertIn('2330', codes)

    def test_eval_fundamental_reports_reason(self):
        row = self._fund().iloc[0]
        ok, why = market_screener.eval_fundamental(
            row, [{'field': 'pe', 'op': market_screener.OP_LTE, 'value': 10}])
        self.assertFalse(ok)
        self.assertIn('本益比', why)
        ok2, _ = market_screener.eval_fundamental(
            row, [{'field': 'pe', 'op': market_screener.OP_LTE, 'value': 40}])
        self.assertTrue(ok2)

    def test_technical_conditions_reuse_strategy_engine(self):
        """技術面直接重用 strategy_engine.CONDITIONS,語意與策略完全一致。"""
        idx = pd.date_range('2026-01-01', periods=60, freq='B')
        rising = pd.DataFrame({'Open': 1.0, 'High': 1.0, 'Low': 1.0,
                               'Close': np.arange(60, dtype=float) + 100,
                               'Volume': 1000.0}, index=idx)
        daily = pd.DataFrame([{'Date': d.strftime('%Y-%m-%d'), 'Code': '2330',
                               'Name': 'A', 'Open': c, 'High': c, 'Low': c,
                               'Close': c, 'Volume': 1000.0}
                              for d, c in zip(idx, rising['Close'])])
        r = market_screener.screen(
            self._fund(), daily,
            conditions=[{'type': 'price_above_ma', 'params': {'n': 20, 'kind': 'SMA'}}],
            fundamental_conds=[],
            to_ohlcv=market_store.to_ohlcv_frame)
        self.assertIn('2330', [x['code'] for x in r['rows']],
                      "持續上漲的股票應通過「站上20MA」")

    def test_should_stop_aborts_scan(self):
        r = market_screener.screen(
            self._fund(), pd.DataFrame([{'Date': '2026-01-01', 'Code': '2330', 'Name': 'A',
                                         'Open': 1, 'High': 1, 'Low': 1, 'Close': 1,
                                         'Volume': 1}]),
            conditions=[{'type': 'ma_cross_up', 'params': {}}],
            to_ohlcv=market_store.to_ohlcv_frame,
            should_stop=lambda: True)
        self.assertTrue(any('中止' in e[0] for e in r['errors']))

    def test_industry_filter(self):
        """【ADR-105】產業篩選。"""
        f = self._fund()
        f['Industry'] = ['半導體業', '水泥工業', None]
        r = market_screener.screen(f, None, industries=['半導體業'])
        self.assertEqual([x['code'] for x in r['rows']], ['2330'])
        r2 = market_screener.screen(f, None, industries=['半導體業', '水泥工業'])
        self.assertEqual(sorted(x['code'] for x in r2['rows']), ['1102', '2330'])
        # 不指定 = 不限產業
        r3 = market_screener.screen(f, None, industries=None)
        self.assertEqual(len(r3['rows']), 3)

    def test_industry_missing_excluded_when_specified(self):
        """產業別為空的個股,在「有指定產業」時不可混進來 (同空值不通過原則)。"""
        f = self._fund()
        f['Industry'] = ['半導體業', '水泥工業', None]
        r = market_screener.screen(f, None, industries=['半導體業', '水泥工業'])
        self.assertNotIn('9999', [x['code'] for x in r['rows']])

    def test_industries_of_reads_actual_values(self):
        f = self._fund()
        f['Industry'] = ['半導體業', '水泥工業', '半導體業']
        self.assertEqual(market_screener.industries_of(f), ['半導體業', '水泥工業'])
        self.assertEqual(market_screener.industries_of(None), [])
        self.assertEqual(market_screener.industries_of(pd.DataFrame()), [])

    def test_result_row_carries_industry(self):
        f = self._fund()
        f['Industry'] = ['半導體業', '水泥工業', None]
        r = market_screener.screen(f, None, industries=['半導體業'])
        self.assertEqual(r['rows'][0]['industry'], '半導體業')

    def test_presets_are_all_valid(self):
        for name, preset in market_screener.PRESETS.items():
            for c in preset.get('conditions', []):
                self.assertIn(c['type'], strategy_engine.CONDITIONS,
                              f"範本「{name}」用了不存在的條件 {c['type']}")
            for f in preset.get('fundamental', []):
                self.assertIn(f['field'], market_screener.FUNDAMENTAL_FIELDS,
                              f"範本「{name}」用了不存在的基本面欄位 {f['field']}")
                self.assertIn(f['op'], market_screener.OPS)


class TestMarketStore(unittest.TestCase):
    """【ADR-103】全市場資料儲存。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _daily(self, date, code='2330', close=100.0):
        return pd.DataFrame([{'Date': date, 'Code': code, 'Name': 'A', 'Open': close,
                              'High': close, 'Low': close, 'Close': close, 'Volume': 1000.0}])

    def test_upsert_daily_is_idempotent_per_date_code(self):
        market_store.upsert_daily(self.tmp, self._daily('2026-07-24', close=100))
        market_store.upsert_daily(self.tmp, self._daily('2026-07-24', close=999))
        df = market_store.load(market_store.daily_path(self.tmp, '2026-07-24'))
        self.assertEqual(len(df), 1)
        self.assertEqual(float(df.iloc[0]['Close']), 999.0, "同日同股應被覆蓋")

    def test_upsert_daily_splits_by_month(self):
        market_store.upsert_daily(self.tmp, self._daily('2026-06-30'))
        market_store.upsert_daily(self.tmp, self._daily('2026-07-01'))
        self.assertIsNotNone(market_store.load(market_store.daily_path(self.tmp, '2026-06-01')))
        self.assertIsNotNone(market_store.load(market_store.daily_path(self.tmp, '2026-07-01')))

    def test_available_daily_dates_supports_incremental(self):
        for d in ('2026-07-23', '2026-07-24'):
            market_store.upsert_daily(self.tmp, self._daily(d))
        got = market_store.available_daily_dates(self.tmp, '2026-07-01', '2026-07-31')
        self.assertEqual(got, {'2026-07-23', '2026-07-24'})

    def test_to_ohlcv_frame_builds_indexed_series(self):
        rows = pd.concat([self._daily(f'2026-07-{d:02d}', close=100 + d) for d in (20, 21, 22)],
                         ignore_index=True)
        market_store.upsert_daily(self.tmp, rows)
        daily = market_store.load_daily_range(self.tmp, '2026-07-01', '2026-07-31')
        ohlcv = market_store.to_ohlcv_frame(daily, '2330')
        self.assertEqual(len(ohlcv), 3)
        self.assertIsInstance(ohlcv.index, pd.DatetimeIndex)
        self.assertEqual(float(ohlcv['Close'].iloc[-1]), 122.0)
        self.assertIsNone(market_store.to_ohlcv_frame(daily, '9999'))

    def test_code_leading_zeros_preserved(self):
        market_store.upsert_daily(self.tmp, self._daily('2026-07-24', code='0050'))
        df = market_store.load(market_store.daily_path(self.tmp, '2026-07-24'))
        self.assertIn('0050', set(df['Code'].astype(str)))

    def test_fundamental_snapshot_roundtrip(self):
        f = pd.DataFrame([{'Code': '2330', 'Name': '台積電', 'PE': 31.59}])
        market_store.save_fundamental(self.tmp, f)
        got = market_store.load_fundamental(self.tmp)
        self.assertEqual(len(got), 1)
        self.assertEqual(got.iloc[0]['Code'], '2330')


class TestScreenerBacktest(unittest.TestCase):
    """【ADR-106】選股回測。"""

    def _market(self, n_days=80, n_stocks=12, n_winners=4):
        """前 n_winners 檔持續上漲,其餘持續下跌。

        趨勢差距刻意拉大且雜訊調小:若下跌股的隨機波動足以讓它短期站上 20MA,
        它被選中其實是條件的正常行為 (不是 bug),但那樣就測不出「條件有沒有
        選對趨勢」。這裡讓兩組明顯分離,測試才有鑑別力。
        """
        rng = np.random.RandomState(5)
        idx = pd.date_range('2026-01-01', periods=n_days, freq='B')
        rows = []
        for k in range(n_stocks):
            code = f'{1000 + k}'
            trend = 1.0 if k < n_winners else -0.8
            px = 100.0
            for d in idx:
                px = max(5.0, px + trend + rng.randn() * 0.15)
                rows.append({'Date': d.strftime('%Y-%m-%d'), 'Code': code,
                             'Name': f'股{k}', 'Open': px, 'High': px * 1.01,
                             'Low': px * 0.99, 'Close': px, 'Volume': 1e6})
        return pd.DataFrame(rows)

    def _fund(self, n_stocks=12):
        return pd.DataFrame([{
            'Code': f'{1000 + k}', 'Name': f'股{k}', 'Industry': '測試業',
            'Close': 100.0, 'PE': 10.0, 'PB': 1.0, 'YieldPct': 5.0, 'EPS': 2.0,
            'GrossMarginPct': 30.0, 'RevenueYoYPct': 20.0, 'RevenueMoMPct': 1.0,
            'MonthRevenue': 1.0, 'Equity': 1.0, 'ROEPct': 10.0} for k in range(n_stocks)])

    def _run(self, **kw):
        base = dict(conditions=[{'type': 'price_above_ma',
                                 'params': {'n': 20, 'kind': 'SMA'}}],
                    rebalance_days=10, top_n=5, min_bars=25,
                    to_ohlcv=market_store.to_ohlcv_frame)
        base.update(kw)
        return screener_backtest.run_screener_backtest(self._market(), **base)

    def test_picks_the_trending_stocks(self):
        r = self._run()
        picked = {h['code'] for h in r['holdings']}
        winners = {'1000', '1001', '1002', '1003'}
        self.assertTrue(picked, "應該有選到股票")
        self.assertEqual(picked, winners,
                         f"「站上20MA」應選中持續上漲的4檔,實際 {sorted(picked)}")
        self.assertGreater(r['metrics']['total_return_pct'], 0)
        self.assertGreater(r['metrics']['excess_pct'], 0,
                           "選中上漲股應該勝過全市場買進持有")

    def test_fundamental_conditions_skipped_by_default(self):
        """**最重要**:基本面只有當前快照,預設不可套用到過去的日期。

        套用等於在當時就知道尚未公布的財報,會讓回測績效嚴重虛高——而且
        數字看起來完全合理,是最難察覺的錯誤。
        """
        r = self._run(fundamental_df=self._fund(),
                      fundamental_conds=[{'field': 'pe', 'op': '<=', 'value': 15}])
        self.assertTrue(r['fundamental_skipped'])
        self.assertFalse(r['has_lookahead'])
        self.assertTrue(any('已略過' in w for w in r['warnings']))

    def test_fundamental_forced_marks_lookahead(self):
        r = self._run(fundamental_df=self._fund(),
                      fundamental_conds=[{'field': 'pe', 'op': '<=', 'value': 15}],
                      allow_lookahead_fundamental=True)
        self.assertFalse(r['fundamental_skipped'])
        self.assertTrue(r['has_lookahead'], "強制套用時必須標記未來函數")
        self.assertIn('未來函數', screener_backtest.summarize(r))

    def test_entry_is_next_trading_day_after_signal(self):
        """訊號用收盤價算出來,不可能用同一根的收盤價成交——進場必須是隔一日。"""
        r = self._run()
        for p in r['periods']:
            self.assertGreater(p['entry_date'], p['signal_date'],
                               "進場日必須晚於訊號日")
            self.assertGreater(p['exit_date'], p['entry_date'])

    def test_no_future_data_leaks_into_screening(self):
        """篩選當下只能看到 <= 訊號日的 K 棒。"""
        daily = self._market()
        seen = {}

        def spy_to_ohlcv(df, code):
            seen['max_date'] = max(seen.get('max_date', ''), str(df['Date'].max()))
            return market_store.to_ohlcv_frame(df, code)

        r = screener_backtest.run_screener_backtest(
            daily, conditions=[{'type': 'price_above_ma', 'params': {'n': 20}}],
            rebalance_days=10, top_n=3, min_bars=25, to_ohlcv=spy_to_ohlcv)
        last_signal = max(p['signal_date'] for p in r['periods'])
        self.assertLessEqual(seen['max_date'], last_signal,
                             "篩選時看到了訊號日之後的資料 (未來函數)")

    def test_rebalance_dates_skip_warmup(self):
        days = [f'2026-01-{d:02d}' for d in range(1, 29)]
        reb = screener_backtest.rebalance_dates(days, rebalance_days=5, min_bars=10)
        self.assertEqual(reb[0], days[10], "前 min_bars 天要留給指標暖身")
        self.assertEqual(reb[1], days[15])

    def test_insufficient_data_warns_instead_of_crashing(self):
        tiny = self._market(n_days=10)
        r = screener_backtest.run_screener_backtest(
            tiny, conditions=[{'type': 'price_above_ma', 'params': {'n': 20}}],
            min_bars=60, to_ohlcv=market_store.to_ohlcv_frame)
        self.assertEqual(r['periods'], [])
        self.assertTrue(any('交易日不足' in w for w in r['warnings']))

    def test_no_conditions_warns(self):
        r = screener_backtest.run_screener_backtest(
            self._market(), conditions=[], min_bars=25,
            to_ohlcv=market_store.to_ohlcv_frame)
        self.assertTrue(any('沒有可回測的條件' in w for w in r['warnings']))

    def test_empty_market_returns_warning(self):
        r = screener_backtest.run_screener_backtest(None)
        self.assertTrue(r['warnings'])
        self.assertEqual(r['periods'], [])

    def test_metrics_include_buy_hold_comparison(self):
        """要能回答「這組條件有沒有贏過隨便買」。"""
        m = self._run()['metrics']
        for k in ('total_return_pct', 'buy_hold_pct', 'excess_pct',
                  'max_drawdown_pct', 'win_rate', 'period_win_rate'):
            self.assertIn(k, m)
        self.assertAlmostEqual(m['excess_pct'],
                               m['total_return_pct'] - m['buy_hold_pct'], places=1)

    def test_costs_reduce_return(self):
        with_cost = self._run(apply_cost=True)['metrics']['total_pnl']
        no_cost = self._run(apply_cost=False)['metrics']['total_pnl']
        self.assertLess(with_cost, no_cost, "含手續費與交易稅的損益必須較低")

    def test_should_stop_aborts(self):
        r = self._run(should_stop=lambda: True)
        self.assertTrue(any('中止' in w for w in r['warnings']))


class TestOrderIntent(unittest.TestCase):
    """【ADR-110 階段1】券商中立的委託意圖。

    這一層算出來的東西會被四家券商的 adapter 各自翻譯後送出去成為真實委託,
    所以「讓價方向」「檔位對齊」「零股限價」這三件事錯一個就是真的賠錢。
    """

    def _s(self, **kw):
        s = {'symbol': '2330', 'trade_type': '股票', 'price_type': '限價',
             'slippage_ticks': 2, 'qty': 1}
        s.update(kw)
        return s

    def _build(self, strategy, action='買進', price=600.0, qty=1,
               asset='stock', exec_price=None):
        return order_intent.build_live_order(
            strategy, {'action': action, 'qty': qty, 'price': price},
            asset, exec_price=exec_price)

    # --- 讓價方向 ---
    def test_buy_slips_up_sell_slips_down(self):
        """讓價的用意是「寧可貴一點也要成交」;方向寫反會變成掛一個更不可能
        成交的價,策略看起來就像從來不進場——這種 bug 不會報錯,只會沒交易。"""
        self.assertEqual(self._build(self._s(), '買進')['price'], 602.0)
        self.assertEqual(self._build(self._s(), '賣出')['price'], 598.0)

    def test_zero_slippage(self):
        oi = self._build(self._s(slippage_ticks=0), '買進')
        self.assertEqual(oi['price'], 600.0)

    # --- 檔位 (鐵則7) ---
    def test_price_aligned_to_tick_across_bands(self):
        """跨價格帶時要用對的 tick,送非法價位會被券商直接退單。"""
        # 99.9 (<100 → tick 0.1) 讓 2 檔 = 100.1,不是 101.9
        self.assertAlmostEqual(self._build(self._s(), '買進', price=99.9)['price'], 100.1)
        # 49.95 (<50 → tick 0.05) 讓 2 檔 = 50.05
        self.assertAlmostEqual(self._build(self._s(), '買進', price=49.95)['price'], 50.05)
        # ETF (00 開頭) 有自己的一套檔位
        oi = self._build(self._s(symbol='0050'), '買進', price=150.0)
        self.assertAlmostEqual(oi['tick'], 0.05)
        self.assertAlmostEqual(oi['price'], 150.1)

    def test_futures_tick_is_one_point(self):
        oi = self._build(self._s(symbol='TXFR1', trade_type='期貨'),
                         '買進', price=18000.0, asset='future')
        self.assertEqual(oi['tick'], 1.0)
        self.assertEqual(oi['price'], 18002.0)

    # --- 限價 / 市價 ---
    def test_market_order_sends_zero_price_but_keeps_limit_for_display(self):
        """市價單價格必須送 0;但讓價後的參考價仍要留著給日誌/確認視窗看,
        否則使用者事後完全查不到「當時打算用什麼價成交」。"""
        oi = self._build(self._s(trade_type='期貨', symbol='TXFR1', price_type='市價'),
                         '買進', price=18000.0, asset='future')
        self.assertEqual(oi['price'], 0.0)
        self.assertEqual(oi['limit_price'], 18002.0)
        self.assertEqual(oi['price_label'], '市價')

    def test_limit_label_shows_actual_price(self):
        self.assertEqual(self._build(self._s(), '買進')['price_label'], '限價602')

    # --- 零股 (鐵則6) ---
    def test_odd_lot_forced_to_limit(self):
        """零股送市價 = 交易所規則違規,券商會退單。策略就算存了市價也要擋。"""
        oi = self._build(self._s(trade_type='零股', price_type='市價'), '買進', qty=100)
        self.assertEqual(oi['price_type'], '限價')
        self.assertEqual(oi['price'], 602.0)
        self.assertEqual(oi['order_lot'], order_intent.LOT_ODD)

    def test_round_lot_marked_common(self):
        self.assertEqual(self._build(self._s())['order_lot'], order_intent.LOT_COMMON)

    # --- 看A做B (ADR-075) ---
    def test_exec_price_overrides_signal_price(self):
        """看A做B:訊號價是 A 的,但下單一定要用 B 的價,否則會用完全不相干
        的價格去掛單 (例如用大盤指數的點數去掛個股)。"""
        oi = self._build(self._s(), '買進', price=999.0, exec_price=600.0)
        self.assertEqual(oi['price'], 602.0)

    # --- 其餘欄位 ---
    def test_neutral_fields_have_no_broker_specific_values(self):
        """意圖 dict 裡不可出現任何一家券商的 SDK 常數——出現了就代表抽象
        層漏了,下一家 adapter 會被迫去看懂永豐的列舉值。"""
        oi = self._build(self._s())
        for v in oi.values():
            self.assertNotIn('shioaji', str(type(v)).lower())
        self.assertEqual(oi['time_in_force'], 'ROD')
        self.assertEqual(oi['order_cond'], order_intent.COND_CASH)
        self.assertIn(oi['action'], order_intent.ACTIONS)
        self.assertIn(oi['trade_type'], order_intent.TRADE_TYPES)
        self.assertIn(oi['price_type'], order_intent.PRICE_TYPES)

    def test_describe_is_readable(self):
        d = order_intent.describe(self._build(self._s(), '買進', qty=3))
        self.assertIn('買進', d)
        self.assertIn('2330', d)
        self.assertIn('限價602', d)


class TestOrderIntentBrokerRouting(unittest.TestCase):
    """【ADR-110 階段2】策略指定券商/帳號。

    這一組守的是「單會下到哪裡」。下錯帳戶不會有任何錯誤訊息,只會在對帳時
    才發現真錢跑到別的戶頭去了,所以預設值與 None 的語意都要精確。
    """

    def test_missing_broker_falls_back_to_sinopac(self):
        """舊策略檔沒有 broker 欄位,不能因此變成「沒有券商可用」而全部停擺。"""
        for s in ({}, {'broker': ''}, {'broker': '   '}, None):
            self.assertEqual(order_intent.broker_of(s), 'sinopac')

    def test_explicit_broker_respected(self):
        self.assertEqual(order_intent.broker_of({'broker': 'kgi'}), 'kgi')
        self.assertEqual(order_intent.broker_of({'broker': ' mega '}), 'mega')

    def test_account_none_means_sdk_default(self):
        """None 與空字串必須等價地代表「使用者沒選」,才能沿用舊行為;
        若回空字串,adapter 會以為「有指定」而去查一個不存在的帳號。"""
        for s in ({}, {'broker_account': ''}, {'broker_account': '  '}, None):
            self.assertIsNone(order_intent.account_of(s))
        self.assertEqual(order_intent.account_of({'broker_account': 'F-123456'}), 'F-123456')

    def test_order_intent_carries_routing(self):
        oi = order_intent.build_live_order(
            {'symbol': '2330', 'trade_type': '股票', 'price_type': '限價',
             'slippage_ticks': 0, 'broker': 'kgi', 'broker_account': 'A-1'},
            {'action': '買進', 'qty': 1, 'price': 600.0}, 'stock')
        self.assertEqual(oi['broker'], 'kgi')
        self.assertEqual(oi['account'], 'A-1')

    def test_order_intent_account_none_for_legacy_strategy(self):
        """沒指定帳號時 account 必須是 None —— adapter 靠這個判斷「完全不要帶
        account 參數」,也就是跟加這個功能之前送出完全相同的委託。"""
        oi = order_intent.build_live_order(
            {'symbol': '2330', 'trade_type': '股票', 'price_type': '限價',
             'slippage_ticks': 0},
            {'action': '買進', 'qty': 1, 'price': 600.0}, 'stock')
        self.assertIsNone(oi['account'])
        self.assertEqual(oi['broker'], 'sinopac')

    def test_describe_target_readable(self):
        self.assertIn('預設帳號', order_intent.describe_target({}))
        t = order_intent.describe_target({'broker': 'kgi', 'broker_account': 'A-1'})
        self.assertIn('kgi', t)
        self.assertIn('A-1', t)
        # 呼叫端可以覆寫成人看得懂的名稱 (券商中文名/帳號暱稱)
        t2 = order_intent.describe_target({'broker': 'sinopac'}, '永豐金', '證券戶')
        self.assertEqual(t2, '永豐金 / 證券戶')


class _C:
    """假合約:有 code 就算合約 (真 shioaji 的合約一定有 code 字串)。"""
    def __init__(self, code, symbol=None, name=''):
        self.code = code
        if symbol is not None:
            self.symbol = symbol
        self.name = name


class _Group156:
    """1.5.6 形狀:Indexs.TSE 是群組,用屬性取代碼 (Indexs.TSE.TSE001)。"""
    def __init__(self, items):
        for k, v in items.items():
            setattr(self, k, v)


class _Indexs156:
    def __init__(self):
        self.TSE = _Group156({'TSE001': _C('TSE001', 'TSE001', '加權指數')})
        self.OTC = _Group156({'OTC101': _C('OTC101', 'OTC101', '櫃買指數')})


class _Cat17:
    """1.7 形狀:ContractCategory 直接用代碼查 (Indexs.get('IX0001'))。"""
    def __init__(self, items):
        self._m = dict(items)
    def get(self, key, default=None):
        return self._m.get(key, default)
    def __getitem__(self, key):
        return self._m[key]
    def __iter__(self):
        return iter(self._m.values())


class TestSjCompatIndex(unittest.TestCase):
    """【ADR-114】shioaji 1.5.6 / 1.7 的指數合約解析。

    加權與櫃買指數是主圖與「看A做B」策略的核心;解析錯了不是報錯,而是
    整個指數功能安靜地不動作,所以兩種版本形狀都要有測試。
    """

    def test_resolves_on_1_7_shape(self):
        idx = _Cat17({'IX0001': _C('IX0001', name='加權指數'),
                      'IX0002': _C('IX0002', name='櫃買指數')})
        self.assertEqual(sj_compat.resolve_index(idx, 'TSE').code, 'IX0001')
        self.assertEqual(sj_compat.resolve_index(idx, 'OTC').code, 'IX0002')

    def test_resolves_on_1_5_6_shape(self):
        idx = _Indexs156()
        self.assertEqual(sj_compat.resolve_index(idx, 'TSE').code, 'TSE001')
        self.assertEqual(sj_compat.resolve_index(idx, 'OTC').code, 'OTC101')

    def test_prefers_new_code_when_both_exist(self):
        """過渡期若兩種代碼都在,要用新的 —— 舊代碼可能是即將淘汰的別名。"""
        idx = _Cat17({'IX0001': _C('IX0001'), 'TSE001': _C('TSE001')})
        self.assertEqual(sj_compat.resolve_index(idx, 'TSE').code, 'IX0001')

    def test_otc_falls_back_through_all_candidates(self):
        """櫃買在不同版本有 OTC101 / OTC001 兩種代碼,都要試。"""
        idx = _Cat17({'OTC001': _C('OTC001')})
        self.assertEqual(sj_compat.resolve_index(idx, 'OTC').code, 'OTC001')

    def test_returns_none_when_absent(self):
        """找不到要回 None 讓呼叫端處理,不可拋例外 (未登入時很常見)。"""
        self.assertIsNone(sj_compat.resolve_index(_Cat17({}), 'TSE'))
        self.assertIsNone(sj_compat.resolve_index(None, 'TSE'))
        self.assertIsNone(sj_compat.resolve_index(_Indexs156(), 'BAD'))

    def test_never_returns_a_group_as_contract(self):
        """1.5.6 的 Indexs.TSE 是群組;誤當成合約回傳,拿去訂閱/下單會出現
        非常難懂的錯誤。用「有沒有 code 字串」區分。"""
        class _Weird:
            def get(self, k, default=None):
                return _Group156({})      # 任何 key 都回一個群組
        self.assertIsNone(sj_compat.resolve_index(_Weird(), 'TSE'))

    def test_survives_container_that_raises(self):
        """真實 SDK 的 __getattr__ 查無時可能拋任何例外。"""
        class _Angry:
            def get(self, k, default=None):
                raise RuntimeError('boom')
            def __getitem__(self, k):
                raise KeyError(k)
            def __getattr__(self, k):
                raise AttributeError(k)
        self.assertIsNone(sj_compat.resolve_index(_Angry(), 'TSE'))


class TestSjCompatContractFields(unittest.TestCase):
    """【ADR-114】1.7 列舉合約可能拿到只有 code 的輕量型別 (StockInfo)。"""

    def test_symbol_falls_back_to_code(self):
        self.assertEqual(sj_compat.contract_symbol(_C('2330', '2330')), '2330')
        self.assertEqual(sj_compat.contract_symbol(_C('2330')), '2330')  # 無 symbol
        self.assertEqual(sj_compat.contract_symbol(None, 'x'), 'x')

    def test_match_compares_both_symbol_and_code(self):
        """只比 symbol 的話,1.7 的輕量型別會全部比不中 → 「查無此代碼」。"""
        self.assertTrue(sj_compat.match_contract_code(_C('2330', '2330'), '2330'))
        self.assertTrue(sj_compat.match_contract_code(_C('2330'), '2330'))
        self.assertTrue(sj_compat.match_contract_code(_C('2330'), ' 2330 '))
        self.assertFalse(sj_compat.match_contract_code(_C('2330'), '2317'))
        self.assertFalse(sj_compat.match_contract_code(_C('2330'), ''))


class TestSjCompatLoginKwargs(unittest.TestCase):
    """【ADR-114】1.7 的 login() 不再吃 contracts_timeout。"""

    def test_drops_unsupported_kwarg(self):
        def login_17(api_key, secret_key, subscribe_trade=True,
                     receive_window=30000, force_refresh=False): pass
        kw, dropped = sj_compat.supported_kwargs(login_17, {'contracts_timeout': 10000})
        self.assertEqual(kw, {})
        self.assertEqual(dropped, ['contracts_timeout'])

    def test_keeps_supported_kwarg(self):
        def login_156(api_key, secret_key, contracts_timeout=10000): pass
        kw, dropped = sj_compat.supported_kwargs(login_156, {'contracts_timeout': 10000})
        self.assertEqual(kw, {'contracts_timeout': 10000})
        self.assertEqual(dropped, [])

    def test_kwargs_signature_keeps_everything(self):
        def anything(api_key, **kw): pass
        kw, dropped = sj_compat.supported_kwargs(anything, {'contracts_timeout': 1})
        self.assertEqual(kw, {'contracts_timeout': 1})

    def test_unknown_signature_passes_through(self):
        """取不到簽名時 (C 擴充常見,shioaji 的 login 就是 Rust 編出來的)
        寧可原樣送出讓它報 TypeError,也不要靜悄悄丟掉使用者指定的參數。

        用 `max` 當樣本:它是 inspect.signature 會拋 ValueError 的內建函式。
        """
        import inspect
        with self.assertRaises(ValueError):
            inspect.signature(max)          # 確認這個樣本真的取不到簽名
        kw, dropped = sj_compat.supported_kwargs(max, {'contracts_timeout': 1})
        self.assertEqual(kw, {'contracts_timeout': 1})
        self.assertEqual(dropped, [])


class TestScreenerNaNText(unittest.TestCase):
    """【ADR-117】全市場資料合併後,缺漏欄位會是 NaN。"""

    def test_nan_never_reaches_display(self):
        """`str(v or '')` 在 pandas 資料上是錯的:NaN 是 truthy,所以
        `nan or ''` 得到 nan,顯示出來就是字面上的 'nan' (實機回報過)。"""
        import numpy as np
        for v in (float('nan'), np.nan, None, '', '  ', 'nan', 'NaN', 'NAN'):
            self.assertEqual(market_screener._text(v), '', repr(v))

    def test_keeps_real_values(self):
        self.assertEqual(market_screener._text('  台積電 '), '台積電')
        self.assertEqual(market_screener._text(2330), '2330')

    def test_custom_default(self):
        self.assertEqual(market_screener._text(float('nan'), '—'), '—')

    def test_row_with_missing_name_shows_blank_not_nan(self):
        """端到端:某檔股票在基本面來源缺名稱時,結果列不可出現 'nan'。"""
        fund = pd.DataFrame([
            {'Code': '1591', 'Name': float('nan'), 'Industry': float('nan'),
             'Close': 41.6, 'PE': None, 'PB': None, 'YieldPct': None,
             'EPS': None, 'GrossMarginPct': None, 'RevenueYoYPct': None,
             'MonthRevenue': None, 'Equity': None, 'ROEPct': None},
            {'Code': '2072', 'Name': '世紀風電', 'Industry': '綠能環保',
             'Close': 153.0, 'PE': 11.71, 'PB': 1.26, 'YieldPct': 4.63,
             'EPS': 13.0, 'GrossMarginPct': 20.0, 'RevenueYoYPct': 5.0,
             'MonthRevenue': 1.0, 'Equity': 1.0, 'ROEPct': 10.0},
        ])
        res = market_screener.screen(fund, None, conditions=[], fundamental_conds=[])
        by_code = {r['code']: r for r in res['rows']}
        self.assertEqual(by_code['1591']['name'], '')
        self.assertEqual(by_code['1591']['industry'], '')
        self.assertEqual(by_code['2072']['name'], '世紀風電')
        for r in res['rows']:
            for k, v in r.items():
                self.assertNotEqual(str(v).lower(), 'nan', f"{k} 出現 nan")


class TestUnitFormat(unittest.TestCase):
    """【ADR-118】籌碼的單位換算。

    這些數字錯了不會有任何錯誤訊息 —— 只會讓使用者看到錯的量能/金額,
    然後據此判斷買賣。所以每一種換算都要有明確的期望值。
    """

    def test_shares_to_lots(self):
        """買賣超原始單位是股,台股講的是張 (1張=1000股)。"""
        self.assertEqual(unit_format.fmt_lots(102513033), '102,513')
        self.assertEqual(unit_format.fmt_lots(-565000), '-565')
        self.assertEqual(unit_format.fmt_lots(0), '0')
        self.assertEqual(unit_format.fmt_lots(1500, nd=1), '1.5')

    def test_yuan_to_yi(self):
        """大盤法人金額原始單位是元。-87,484,625,299 元 ≈ -874.85 億。"""
        self.assertEqual(unit_format.fmt_yi(-87484625299, '元'), '-874.85')
        self.assertEqual(unit_format.fmt_yi(1611819893, '元'), '16.12')
        self.assertEqual(unit_format.fmt_yi(0, '元'), '0.00')

    def test_qian_yuan_to_yi(self):
        """融資金額原始單位是仟元。545,534,811 仟元 = 5455.35 億。"""
        self.assertEqual(unit_format.fmt_yi(545534811, '仟元'), '5,455.35')
        self.assertEqual(unit_format.fmt_yi(100000, '仟元'), '1.00')

    def test_unknown_unit_raises(self):
        """不支援的單位要當場報錯,不可默默算出一個沒意義的數字。"""
        with self.assertRaises(ValueError):
            unit_format.fmt_yi(100, '美元')

    def test_missing_shows_dashes_not_zero(self):
        """「沒有資料」與「數值是 0」在籌碼上意義完全不同。"""
        for v in (None, float('nan'), '', '--', 'abc'):
            self.assertEqual(unit_format.fmt_lots(v), unit_format.MISSING, repr(v))
            self.assertEqual(unit_format.fmt_yi(v, '元'), unit_format.MISSING, repr(v))
            self.assertEqual(unit_format.fmt_int(v), unit_format.MISSING, repr(v))

    def test_accepts_comma_strings(self):
        """從 CSV 讀進來可能已經是帶千分位的字串。"""
        self.assertEqual(unit_format.fmt_lots('1,000,000'), '1,000')

    def test_signed_keeps_direction(self):
        """增減欄位靠正負號分辨方向,也靠它決定紅漲綠跌 (鐵則1)。"""
        self.assertEqual(unit_format.fmt_signed_yi(1000000, '仟元'), '+10.00')
        self.assertEqual(unit_format.fmt_signed_yi(-1000000, '仟元'), '-10.00')
        self.assertEqual(unit_format.fmt_signed_yi(None, '仟元'), unit_format.MISSING)

    def test_diff_series_needs_oldest_first(self):
        """相鄰兩期的差。第一期沒有前值 → None。"""
        self.assertEqual(unit_format.diff_series([100, 130, 120]), [None, 30, -10])
        self.assertEqual(unit_format.diff_series([]), [])
        self.assertEqual(unit_format.diff_series([5]), [None])

    def test_diff_series_handles_gaps(self):
        """中間缺一天時,不可拿更早的值硬算出一個假的增減。"""
        self.assertEqual(unit_format.diff_series([100, None, 120]), [None, None, None])

    def test_diff_direction_is_today_minus_yesterday(self):
        """方向寫反的話,融資增加會顯示成減少 —— 結論剛好相反。"""
        d = unit_format.diff_series([500, 600])
        self.assertEqual(d[1], 100, "應為 今日 − 前日")

    def test_is_positive_for_coloring(self):
        self.assertIs(unit_format.is_positive('+10.00'), True)
        self.assertIs(unit_format.is_positive('-10.00'), False)
        self.assertIs(unit_format.is_positive('1,234'), True)
        self.assertIsNone(unit_format.is_positive(unit_format.MISSING))
        self.assertIsNone(unit_format.is_positive(''))



class TestRegimePanel(unittest.TestCase):
    """【ADR-120】主圖【盤勢判斷】面板的純邏輯:設定正規化 + 通知去重。

    去重規則是這次改動裡最容易出錯、又最難在 GUI 上發現的地方 —— 主圖每
    縮放/平移一次就重畫一次,少了去重就會被同一個型態通知洗版,而畫面上
    只會看到日誌一直捲,不會有任何錯誤訊息。
    """

    # ---------- normalize ----------
    def test_normalize_empty_gives_working_defaults(self):
        """第一次啟動 (設定檔沒有這一段) 也要拿得到一份能用的設定。"""
        for raw in (None, {}, [], 'x', 0):
            s = regime_panel.normalize(raw)
            self.assertEqual(set(s), set(regime_panel.PANEL_DEFAULTS), repr(raw))
            self.assertIn(s['sr_range_mode'], volume_profile.RANGE_MODES)
            self.assertTrue(s['pattern_list'])

    def test_normalize_clamps_absurd_values(self):
        """設定檔被手動改壞時,不可以讓離譜的數值傳進計算層。"""
        s = regime_panel.normalize({'sr_fixed_bars': 0, 'sr_max_levels': 999,
                                    'pattern_lookback': 1, 'pattern_near_pct': -5})
        self.assertEqual(s['sr_fixed_bars'], 20)
        self.assertEqual(s['sr_max_levels'], 20)
        self.assertEqual(s['pattern_lookback'], 10)
        self.assertEqual(s['pattern_near_pct'], 0.1)

    def test_normalize_rejects_unknown_range_mode(self):
        s = regime_panel.normalize({'sr_range_mode': '亂打的'})
        self.assertEqual(s['sr_range_mode'], regime_panel.PANEL_DEFAULTS['sr_range_mode'])

    def test_normalize_bad_types_fall_back(self):
        s = regime_panel.normalize({'sr_fixed_bars': 'abc', 'pattern_near_pct': None,
                                    'pattern_lookback': float('nan')})
        self.assertEqual(s['sr_fixed_bars'], regime_panel.PANEL_DEFAULTS['sr_fixed_bars'])
        self.assertEqual(s['pattern_near_pct'], regime_panel.PANEL_DEFAULTS['pattern_near_pct'])
        self.assertEqual(s['pattern_lookback'], regime_panel.PANEL_DEFAULTS['pattern_lookback'])

    def test_normalize_empty_pattern_list_is_respected(self):
        """「全部取消勾選」跟「舊設定檔沒存這個欄位」是不同的意思:
        前者要尊重使用者的選擇,後者才給預設全開。"""
        self.assertEqual(regime_panel.normalize({'pattern_list': []})['pattern_list'], [])
        self.assertTrue(regime_panel.normalize({})['pattern_list'])

    def test_normalize_drops_unknown_pattern_ids(self):
        s = regime_panel.normalize({'pattern_list': ['regime', '不存在的型態']})
        self.assertEqual(s['pattern_list'], ['regime'])

    def test_normalize_does_not_mutate_input_or_defaults(self):
        raw = {'pattern_list': ['regime']}
        regime_panel.normalize(raw)
        self.assertEqual(raw, {'pattern_list': ['regime']})
        regime_panel.normalize({})['pattern_list'].append('汙染')
        self.assertNotIn('汙染', regime_panel.PANEL_DEFAULTS['pattern_list'])

    def test_pattern_params_feeds_evaluate_all(self):
        """正規化後的設定要能直接餵給 market_pattern.evaluate_all。"""
        p = regime_panel.pattern_params({'pattern_lookback': 30, 'pattern_near_pct': 1.5,
                                         'pattern_list': ['regime']})
        self.assertEqual(p['lookback'], 30)
        self.assertEqual(p['near_pct'], 1.5)
        self.assertEqual(p['enabled_patterns'], ('regime',))
        df = pd.DataFrame({'Open': range(100, 160), 'High': range(101, 161),
                           'Low': range(99, 159), 'Close': range(100, 160)},
                          index=pd.date_range('2026-01-01', periods=60, freq='B'))
        sigs = market_pattern.evaluate_all(df, p)
        self.assertTrue(all(s['pattern'].startswith(('regime', 'near_range')) for s in sigs), sigs)

    # ---------- should_evaluate ----------
    def test_should_evaluate_requires_both_switches(self):
        base = {'enabled': True, 'pattern_enabled': True}
        self.assertTrue(regime_panel.should_evaluate(base, '^TWII', '日K'))
        self.assertFalse(regime_panel.should_evaluate({**base, 'enabled': False}, '^TWII', '日K'))
        self.assertFalse(regime_panel.should_evaluate({**base, 'pattern_enabled': False},
                                                      '^TWII', '日K'))

    def test_should_evaluate_allows_only_the_listed_timeframes(self):
        """【ADR-132】使用者要求加入 60分K,所以改成允許清單。

        **不可以**變成「任何週期都能跑」:5分K/15分K 會產生大量沒有意義的型態
        訊號,而 ADR-132 之後這些訊號會推播到手機上。"""
        base = {'enabled': True, 'pattern_enabled': True}
        for tf in ('日K', '60分K'):
            self.assertTrue(regime_panel.should_evaluate(base, '^TWII', tf), repr(tf))
        for tf in ('5分K', '15分K', '30分K', '周K', '月K', '', None):
            self.assertFalse(regime_panel.should_evaluate(base, '^TWII', tf), repr(tf))

    def test_should_evaluate_index_only_switch(self):
        base = {'enabled': True, 'pattern_enabled': True, 'index_only': True}
        self.assertTrue(regime_panel.should_evaluate(base, '^twii', '日K'))
        self.assertFalse(regime_panel.should_evaluate(base, '2330', '日K'))
        self.assertTrue(regime_panel.should_evaluate({**base, 'index_only': False},
                                                     '2330', '日K'))

    def test_is_index_symbol_covers_both_naming(self):
        """^TWII 是本程式內部代碼,IX0001/TSE 是 shioaji 側的寫法 (ADR-114)。"""
        for sym in ('^TWII', '^twii', 'TWII', 'TSE', 'TSE001', 'IX0001'):
            self.assertTrue(regime_panel.is_index_symbol(sym), sym)
        for sym in ('^TWOII', 'IX0043', '2330', '', None):
            self.assertFalse(regime_panel.is_index_symbol(sym), sym)

    # ---------- plan_notifications ----------
    ONESHOT = {'pattern': 'double_top', 'label': 'M頭', 'bias': '偏空'}
    REGIME_RANGE = {'pattern': 'regime', 'label': '目前盤勢:區間整理', 'bias': ''}
    REGIME_UP = {'pattern': 'regime', 'label': '目前盤勢:上升趨勢', 'bias': ''}

    def test_first_evaluation_notifies_everything(self):
        msgs, st = regime_panel.plan_notifications([self.ONESHOT, self.REGIME_RANGE],
                                                   None, '2026-07-28')
        self.assertEqual(msgs, ['M頭[偏空]', '目前盤勢:區間整理'])
        self.assertEqual(st['date'], '2026-07-28')

    def test_redraw_same_bar_does_not_repeat(self):
        """主圖縮放/平移會重畫,重畫不可以再通知一次同一個型態。"""
        sigs = [self.ONESHOT, self.REGIME_RANGE]
        msgs, st = regime_panel.plan_notifications(sigs, None, '2026-07-28')
        self.assertTrue(msgs)
        for _ in range(5):
            msgs, st = regime_panel.plan_notifications(sigs, st, '2026-07-28')
            self.assertEqual(msgs, [], "同一根K棒重畫不可重複通知")

    def test_oneshot_notifies_again_on_a_new_bar(self):
        """換到新的一根K棒,型態重新確認就是新的事件,要再通知。"""
        _, st = regime_panel.plan_notifications([self.ONESHOT], None, '2026-07-28')
        msgs, _ = regime_panel.plan_notifications([self.ONESHOT], st, '2026-07-29')
        self.assertEqual(msgs, ['M頭[偏空]'])

    def test_persistent_state_silent_until_it_changes(self):
        """盤勢連續 20 天都是「區間整理」不該連發 20 則通知;變了才通知。"""
        _, st = regime_panel.plan_notifications([self.REGIME_RANGE], None, '2026-07-28')
        msgs, st = regime_panel.plan_notifications([self.REGIME_RANGE], st, '2026-07-29')
        self.assertEqual(msgs, [], "持續狀態沒變不可重複通知")
        msgs, st = regime_panel.plan_notifications([self.REGIME_UP], st, '2026-07-30')
        self.assertEqual(msgs, ['目前盤勢:上升趨勢'])

    def test_persistent_disappearing_then_returning_notifies_again(self):
        _, st = regime_panel.plan_notifications([self.REGIME_RANGE], None, '2026-07-28')
        msgs, st = regime_panel.plan_notifications([], st, '2026-07-29')
        self.assertEqual(msgs, [])
        msgs, st = regime_panel.plan_notifications([self.REGIME_RANGE], st, '2026-07-30')
        self.assertEqual(msgs, ['目前盤勢:區間整理'])

    def test_plan_does_not_mutate_previous_state(self):
        """呼叫端要靠「舊 state」判斷,函式偷改它會讓下一次判斷失準。"""
        _, st = regime_panel.plan_notifications([self.ONESHOT], None, '2026-07-28')
        snapshot = json.loads(json.dumps(st))
        regime_panel.plan_notifications([self.ONESHOT, self.REGIME_RANGE], st, '2026-07-28')
        self.assertEqual(st, snapshot)

    def test_plan_survives_garbage_signals(self):
        msgs, st = regime_panel.plan_notifications(
            [None, 'x', {}, {'pattern': ''}, self.ONESHOT], None, '2026-07-28')
        self.assertEqual(msgs, ['M頭[偏空]'])
        self.assertIsInstance(st, dict)

    def test_persistent_ids_cover_the_stateful_signals(self):
        """evaluate_all 會吐出的「持續狀態」訊號都必須列進去 —— 漏一個就會
        變成每天/每次重畫都通知一次。"""
        for pid in ('regime', 'near_range_high', 'near_range_low',
                    'triangle_symmetrical', 'triangle_ascending',
                    'triangle_descending', 'wedge_rising', 'wedge_falling'):
            self.assertIn(pid, regime_panel.PERSISTENT_PATTERN_IDS, pid)
        self.assertNotIn('double_top', regime_panel.PERSISTENT_PATTERN_IDS)




class TestKBarsPlan(unittest.TestCase):
    """【ADR-122】kbars 請求要不要分段、每段幾天。

    shioaji 的 kbars 一律回 1 分 K (日/周/月K 是本地重採樣的),所以「天數」
    直接等於資料量 —— 範圍一大單次請求就容易在券商端逾時。門檻寫錯不會有
    錯誤訊息,只會在某個週期上偶爾逾時,而且很難重現。
    """

    def test_decision_is_driven_by_chunk_size_not_threshold(self):
        """判斷依據是「切得出第二段嗎」,不是「過門檻了嗎」。

        threshold 常數只留給診斷去比對主圖的字面值 (確保兩邊規則沒改岔),
        不參與判斷 —— 在 threshold <= chunk 的參數下那句永遠不會改變結果,
        寫了就是一條測不到的死枝。
        """
        self.assertIsNone(kbars_plan.chunk_plan('日K', kbars_plan.DAY_TF_CHUNK_DAYS))
        self.assertIsNotNone(kbars_plan.chunk_plan('日K', kbars_plan.DAY_TF_CHUNK_DAYS + 1))
        self.assertIsNone(kbars_plan.chunk_plan('5分K', kbars_plan.MIN_TF_CHUNK_DAYS))
        self.assertIsNotNone(kbars_plan.chunk_plan('5分K', kbars_plan.MIN_TF_CHUNK_DAYS + 1))

    def test_single_segment_counts_as_no_chunking(self):
        """過了門檻但只切得出一段 = 不需分段。

        分K 的門檻是 5 天、段長卻是 10 天,所以 6~10 天都是「過門檻但只有
        一段」。那跟單次下載完全一樣,卻要多繞一條背景預抓的路 —— 等於平白
        改變一條使用者每天在跑的路徑 (5分K 取 7 天) 的行為。
        這條實作時真的寫錯過,診斷案例當場抓到。
        """
        for d in range(kbars_plan.MIN_TF_THRESHOLD_DAYS + 1,
                       kbars_plan.MIN_TF_CHUNK_DAYS + 1):
            self.assertIsNone(kbars_plan.chunk_plan('5分K', d), f"{d} 天只有一段")
        self.assertIsNotNone(kbars_plan.chunk_plan('5分K', kbars_plan.MIN_TF_CHUNK_DAYS + 1))

    def test_min_and_day_timeframes_use_different_rules(self):
        """分K 類與日K 類的門檻/段長不同,不可以共用一組數字。"""
        self.assertTrue(kbars_plan.is_min_timeframe('60分K'))
        self.assertFalse(kbars_plan.is_min_timeframe('日K'))
        self.assertEqual(kbars_plan.chunk_plan('60分K', 35)['chunk_days'],
                         kbars_plan.MIN_TF_CHUNK_DAYS)
        self.assertEqual(kbars_plan.chunk_plan('60分K', 35)['segments'], 4)
        self.assertEqual(kbars_plan.chunk_plan('日K', 300)['chunk_days'],
                         kbars_plan.DAY_TF_CHUNK_DAYS)

    def test_actual_strategy_lookbacks(self):
        """對上策略路徑真正會用的天數 (QT_TF_DAYS):哪些要分段、哪些不用。

        1分K/5分K **不可以**被判成要分段 —— 那兩個是使用者現在真的在跑的
        週期,改成走背景預抓等於改變它們的行為 (ADR-122 明講行為零改變)。
        """
        for tf, days, need in [('1分K', 4, False), ('5分K', 7, False),
                               ('15分K', 14, True), ('30分K', 21, True),
                               ('60分K', 35, True), ('日K', 300, True),
                               ('周K', 700, True), ('月K', 1500, True)]:
            got = kbars_plan.chunk_plan(tf, days)
            self.assertEqual(got is not None, need, f"{tf} {days}天")

    def test_segments_estimate(self):
        self.assertEqual(kbars_plan.chunk_plan('日K', 300)['segments'], 4)
        self.assertEqual(kbars_plan.chunk_plan('月K', 1500)['segments'], 17)

    def test_unknown_timeframe_is_conservative(self):
        """認不得的週期當日K 類處理:寧可多切幾段 (慢但會成功),
        也不要拿未知週期去發一個可能逾時的大請求。"""
        self.assertFalse(kbars_plan.is_min_timeframe('45分K'))
        self.assertIsNone(kbars_plan.chunk_plan('45分K', 30))
        self.assertIsNotNone(kbars_plan.chunk_plan('45分K', 300))

    def test_garbage_input_returns_none(self):
        for days in (None, 'abc', 0, -5, float('nan')):
            self.assertIsNone(kbars_plan.chunk_plan('日K', days), repr(days))
        self.assertFalse(kbars_plan.is_min_timeframe(None))




class TestOwnExitKinds(unittest.TestCase):
    """【ADR-123】自己管出場的策略種類,泛用停損/停利不可以插手。

    使用者實測:終極波段策略的 5 口 TXF 被「即時停損出場 (損益 -2.44% ≤
    -2.0%)」砍掉,而那個 2.0% 是 new_strategy() 的預設值 —— 終極波段的
    專屬編輯器根本沒有這個欄位,使用者從頭到尾沒看過也沒設定過。
    它的出場應該完全由 X/C/F/Y/Z (加權指數點位 + 隔天12:00二次確認) 決定。
    """

    def _rt(self, state='LONG', entry=41700.0, qty=5):
        rt = strategy_engine.new_runtime()
        rt.update({'state': state, 'exec_entry_price': entry, 'entry_price': entry,
                   'qty': qty})
        return rt

    def test_kind_constant_is_pinned_to_chukuangren(self):
        """OWN_EXIT_KINDS 用字串寫死 (避免循環 import),所以要有一條斷言
        把它跟真正的 KIND 釘在一起 —— 日後改名才不會無聲脫鉤。"""
        self.assertIn(chukuangren_band.KIND, strategy_engine.OWN_EXIT_KINDS)

    def test_chukuangren_never_hit_by_generic_intrabar_stop(self):
        """重現使用者的情境:既有存檔的終極波段策略仍帶著 stop_loss_pct=2.0,
        現價比進場價低 2.44% —— 不可以產生任何出場 intent。"""
        s = chukuangren_band.default_strategy()
        s.update({'symbol': 'TXF', 'trade_type': '期貨', 'qty': 5,
                  'stop_loss_pct': 2.0})          # 舊存檔殘留值
        self.assertIsNone(
            strategy_engine.check_intrabar_futures_stop(s, self._rt(), 40688.0))

    def test_chukuangren_immune_to_every_generic_stop_field(self):
        """四個泛用欄位都要擋,不是只擋 stop_loss_pct。"""
        rt = self._rt()
        for field, value, price in (('stop_loss_pct', 1.0, 40000.0),
                                    ('take_profit_pct', 1.0, 43000.0),
                                    ('stop_loss_abs', 100.0, 40000.0),
                                    ('take_profit_abs', 100.0, 43000.0)):
            s = chukuangren_band.default_strategy()
            s.update({'symbol': 'TXF', 'trade_type': '期貨', 'qty': 5, field: value})
            self.assertIsNone(
                strategy_engine.check_intrabar_futures_stop(s, rt, price), field)

    def test_normal_futures_strategy_still_stops_out(self):
        """反向對照:這個守門不可以把整個即時停損功能關掉 —— 一般期貨策略
        在同樣條件下**仍然要**出場。少了這條,把守門寫成『全部都擋』也會
        測起來一片綠。"""
        s = strategy_engine.new_strategy()
        s.update({'symbol': 'TXF', 'trade_type': '期貨', 'qty': 5,
                  'stop_loss_pct': 2.0})
        intent = strategy_engine.check_intrabar_futures_stop(s, self._rt(), 40688.0)
        self.assertIsNotNone(intent)
        self.assertEqual(intent['kind'], 'CLOSE')

    def test_has_own_exit_logic_is_safe_on_garbage(self):
        for bad in (None, {}, {'kind': None}, {'kind': ''}, {'kind': 'custom'}):
            self.assertFalse(strategy_engine.has_own_exit_logic(bad), repr(bad))

    def test_default_strategy_zeroes_generic_stops(self):
        """治本的那一半:新建的終極波段策略資料本身就不該帶泛用停損停利。"""
        s = chukuangren_band.default_strategy()
        for k in ('stop_loss_pct', 'take_profit_pct', 'stop_loss_abs', 'take_profit_abs'):
            self.assertEqual(s[k], 0.0, k)
        # 對照:一般策略維持原本的預設 (這次沒有動到它)
        self.assertEqual(strategy_engine.new_strategy()['stop_loss_pct'], 2.0)




class TestIncludeNightOf(unittest.TestCase):
    """【ADR-124】「這檔策略要不要做夜盤」的單一判斷。

    使用者實測:終極波段 12:01 開的空單,在 **15:00:01 夜盤開盤第一秒**被
    平掉。使用者的原話是「終極波段不會在夜盤做任何動作的」—— 那不是偏好,
    是這個策略的設計事實(看A 是加權指數日K、12:00 確認、12:01 執行)。
    """

    def test_kind_constant_is_pinned(self):
        """用字串寫死 (避免循環 import),所以要有斷言把兩邊釘在一起。"""
        self.assertIn(chukuangren_band.KIND, strategy_engine.DAY_SESSION_ONLY_KINDS)

    def test_chukuangren_never_includes_night(self):
        """既有存檔是 day_night 也要被覆寫 —— 不做資料遷移就正確。"""
        for stored in ('day_night', 'day', None):
            s = chukuangren_band.default_strategy()
            if stored is not None:
                s['futures_session'] = stored
            else:
                s.pop('futures_session', None)
            self.assertFalse(strategy_engine.include_night_of(s), repr(stored))

    def test_normal_strategy_follows_user_setting(self):
        """一般策略照使用者設定走 —— 覆寫不可以擴大到所有策略,
        否則「日盤+夜盤」的策略夜盤就不受保護了。"""
        base = strategy_engine.new_strategy()
        self.assertFalse(strategy_engine.include_night_of(
            dict(base, futures_session='day')))
        self.assertTrue(strategy_engine.include_night_of(
            dict(base, futures_session='day_night')))
        # 缺欄位維持既有預設 (含夜盤)
        no_field = dict(base)
        no_field.pop('futures_session', None)
        self.assertTrue(strategy_engine.include_night_of(no_field))

    def test_safe_on_garbage(self):
        self.assertTrue(strategy_engine.include_night_of(None))
        self.assertTrue(strategy_engine.include_night_of({}))
        self.assertFalse(strategy_engine.include_night_of(
            {'kind': 'chukuangren_band'}))

    def test_day_only_and_own_exit_are_separate_concepts(self):
        """兩份清單目前內容相同,但語意不同 (一個講出場邏輯、一個講交易時段),
        不可以合併成同一個判斷 —— 日後多一個「自己管出場但要做夜盤」的策略
        種類時,合併過的寫法會一起把夜盤關掉。"""
        self.assertIsNot(strategy_engine.OWN_EXIT_KINDS,
                         strategy_engine.DAY_SESSION_ONLY_KINDS)




class TestStrategyLock(unittest.TestCase):
    """【ADR-125】啟用中的策略不可編輯、不可刪除。

    使用者實測:一檔「狀態=啟用、運轉狀態=運轉中」的策略被直接刪掉了
    (持倉是 FLAT,所以原本唯一那道「仍有持倉」的檢查放行了)。
    """

    def test_locked_when_enabled(self):
        self.assertTrue(strategy_engine.is_locked({'enabled': True}))
        self.assertFalse(strategy_engine.is_locked({'enabled': False}))

    def test_is_locked_safe_on_garbage(self):
        """缺欄位要當成「沒啟用」—— 這個方向是安全的 (不會鎖死刪除功能),
        而真正在跑的策略一定有 enabled=True。"""
        for bad in (None, {}, {'enabled': None}, {'enabled': 0}):
            self.assertFalse(strategy_engine.is_locked(bad), repr(bad))

    def test_enabled_cannot_be_deleted(self):
        """重現截圖:啟用中 + 持倉 FLAT —— 舊程式會直接刪掉。"""
        ok, reason = strategy_engine.can_delete(
            {'name': '空', 'enabled': True}, {'state': 'FLAT'})
        self.assertFalse(ok)
        self.assertIn('停用', reason)

    def test_position_still_blocks_when_disabled(self):
        """既有的「仍有持倉」檢查不可以被弄丟。"""
        ok, reason = strategy_engine.can_delete(
            {'name': '空', 'enabled': False}, {'state': 'LONG'})
        self.assertFalse(ok)
        self.assertIn('持倉', reason)

    def test_disabled_and_flat_can_be_deleted(self):
        """反向對照:正常情況**要刪得掉** —— 少了這條,把 can_delete 寫成
        永遠 False 也會一片綠,那等於把刪除功能整個鎖死。"""
        ok, reason = strategy_engine.can_delete(
            {'name': '空', 'enabled': False}, {'state': 'FLAT'})
        self.assertTrue(ok)
        self.assertEqual(reason, '')

    def test_enabled_reason_wins_over_position(self):
        """兩個條件都不成立時,訊息要指向「先停用」—— 那才是使用者要做的
        第一步,先叫他去處理持倉會讓他繞路。"""
        ok, reason = strategy_engine.can_delete(
            {'name': '空', 'enabled': True}, {'state': 'LONG'})
        self.assertFalse(ok)
        self.assertIn('停用', reason)
        self.assertNotIn('仍有持倉', reason)

    def test_missing_runtime_is_treated_as_flat(self):
        ok, _ = strategy_engine.can_delete({'name': 'x', 'enabled': False}, None)
        self.assertTrue(ok)




class TestSessionOpensBetween(unittest.TestCase):
    """【ADR-127】日K 類快取的新鮮度:改問「有沒有跨過開盤」而不是牆上時鐘。

    使用者實測:同一則「^TWII 日K 歷史資料分 4 段在背景補齊中」在 09:20 與
    09:55 各推播一次(相隔 35 分鐘 = ADR-122 的 30 分鐘 TTL 過期後的下一個
    5 分鐘評估點)。而那段期間 ^TWII 的日K「已收盤」集合根本沒變 ——
    整天每半小時白抓一次 300 天資料、白推播一次。
    """

    def _d(self, *a):
        from datetime import datetime as _dt
        return _dt(*a)

    def test_within_one_session_is_fresh(self):
        """重現使用者的情境:09:20 → 09:55 沒有跨過任何開盤 → 快取仍新鮮。"""
        self.assertFalse(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 9, 20), self._d(2026, 7, 30, 9, 55)))
        self.assertFalse(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 10, 0), self._d(2026, 7, 30, 14, 0)))

    def test_crossing_an_open_is_stale(self):
        """跨過開盤就一定要過期 —— 這個方向錯了會拿舊資料去評估、下錯單。"""
        # 09:00 整股開盤
        self.assertTrue(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 8, 50), self._d(2026, 7, 30, 9, 5)))
        # 09:10 盤中零股開盤
        self.assertTrue(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 9, 5), self._d(2026, 7, 30, 9, 20)))
        # 08:45 期貨日盤
        self.assertTrue(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 8, 0), self._d(2026, 7, 30, 8, 50)))
        # 15:00 期貨夜盤
        self.assertTrue(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 14, 0), self._d(2026, 7, 30, 15, 30)))

    def test_boundary_is_left_open_right_closed(self):
        """區間是 (t0, t1]:剛好等於 t0 的開盤不算「期間內發生」(那一刻的資料
        已經反映在快取裡了);剛好等於 t1 的算。

        用 09:10 (盤中零股開盤) 當 t0 而不是 09:00 —— 09:00 往後 30 分鐘內還
        有 09:10 這個開盤,拿它當例子會測到別的東西 (第一版就是這樣寫錯的,
        單元測試當場紅)。09:10 之後下一個開盤要等到 15:00。"""
        op = self._d(2026, 7, 30, 9, 10)
        self.assertFalse(market_session.any_session_opens_between(
            op, self._d(2026, 7, 30, 9, 40)), "t0 就是開盤那一刻 → 不算期間內發生")
        self.assertTrue(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 9, 9), op), "t1 剛好等於開盤 → 算")

    def test_weekend_has_no_open(self):
        """週末沒有開盤 → 期間內沒有新的日K 產生。"""
        self.assertFalse(market_session.any_session_opens_between(
            self._d(2026, 8, 1, 10, 0), self._d(2026, 8, 2, 20, 0)))   # 週六→週日

    def test_crossing_into_next_weekday_is_stale(self):
        self.assertTrue(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 20, 0), self._d(2026, 7, 31, 9, 30)))

    def test_defensive_cases_err_toward_stale(self):
        """判斷不出來一律當「過期」—— 多抓一次無害,誤判新鮮會下錯單。"""
        self.assertTrue(market_session.any_session_opens_between(None, self._d(2026, 7, 30)))
        self.assertTrue(market_session.any_session_opens_between(self._d(2026, 7, 30), None))
        # 時鐘倒退 (校時/夏令時)
        self.assertTrue(market_session.any_session_opens_between(
            self._d(2026, 7, 30, 10, 0), self._d(2026, 7, 30, 9, 0)))
        # 跨太久不逐日枚舉,直接當過期
        self.assertTrue(market_session.any_session_opens_between(
            self._d(2026, 1, 1), self._d(2026, 7, 30)))

    def test_open_minutes_are_the_union(self):
        """開盤時刻取聯集是刻意的:多列一個只會讓快取早一點過期(安全方向),
        少列一個才危險。"""
        mins = market_session.session_open_minutes()
        for m in (market_session.STOCK_OPEN_MIN, market_session.ODD_LOT_OPEN_MIN,
                  market_session.FUT_DAY_OPEN_MIN, market_session.FUT_NIGHT_OPEN_MIN):
            self.assertIn(m, mins)



class TestDaySignalTimeframe(unittest.TestCase):
    """【ADR-128】終極波段的「看A 訊號週期」是結構上固定的日K,不是存檔值。

    使用者實機回報:「我明明設定就是看日K,為什麼哪裡有看5分K?」——
    因為 watch_timeframe 被寫死成 '5分K',而那個值的用途根本不是訊號週期,
    是拿去驅動 _quant_eval_pass 的 K 棒邊界節拍 (為了踩進 12:00 確認窗口)。
    訊號本身一直是日K (`tf='日K'` 寫死在呼叫端),所以介面回答錯了。
    """

    def test_kind_is_pinned_to_the_module(self):
        """字串清單與 chukuangren_band.KIND 釘在一起,改名不會無聲脫鉤。"""
        self.assertIn(chukuangren_band.KIND, strategy_engine.DAY_SIGNAL_KINDS)

    def test_override_ignores_saved_value(self):
        """既有策略檔裡殘留的 '5分K' 必須被覆寫 —— 不做資料遷移也正確。"""
        s = chukuangren_band.default_strategy()
        s['watch_timeframe'] = '5分K'          # 模擬舊存檔
        self.assertEqual(strategy_engine.watch_timeframe_of(s), '日K')
        s['watch_timeframe'] = '30分K'
        self.assertEqual(strategy_engine.watch_timeframe_of(s), '日K')

    def test_default_strategy_stores_the_truth(self):
        self.assertEqual(chukuangren_band.default_strategy()['watch_timeframe'], '日K')

    def test_other_kinds_are_untouched(self):
        """反向對照:一般策略的 watch_timeframe 不可以被改掉 —— 少了這條,
        把 watch_timeframe_of() 寫成「永遠回日K」也會一片綠,那會把所有
        看A做B 策略的訊號週期都毀掉。"""
        s = strategy_engine.new_strategy()
        s.update({'watch_enabled': True, 'watch_symbol': '^TWII', 'watch_timeframe': '30分K'})
        self.assertEqual(strategy_engine.watch_timeframe_of(s), '30分K')
        s['watch_timeframe'] = '5分K'
        self.assertEqual(strategy_engine.watch_timeframe_of(s), '5分K')
        # 看A做B 關閉時仍沿用執行週期 (既有行為)
        s2 = strategy_engine.new_strategy()
        s2.update({'watch_enabled': False, 'timeframe': '15分K'})
        self.assertEqual(strategy_engine.watch_timeframe_of(s2), '15分K')

    def test_day_timeframe_drives_the_day_class_cadence(self):
        """覆寫成日K 之後,評估節拍會走「日K 類」那一支 (timeframe_minutes 回 None),
        也就是 _quant_eval_pass 的 10 分鐘檢查,而不是 5 分鐘的分K 邊界。"""
        self.assertIsNone(strategy_engine.timeframe_minutes(
            strategy_engine.DAY_SIGNAL_TIMEFRAME))
        self.assertTrue(strategy_engine.is_valid_timeframe(
            strategy_engine.DAY_SIGNAL_TIMEFRAME))

    def test_three_kind_lists_stay_separate(self):
        """三份清單目前內容相同但語意不同 (出場邏輯 / 交易時段 / 訊號週期),
        刻意不合併 —— 合併過的寫法在日後多一個『自己管出場但訊號看分K』的
        種類時會一起把週期改錯。"""
        self.assertIsNot(strategy_engine.OWN_EXIT_KINDS, strategy_engine.DAY_SIGNAL_KINDS)
        self.assertIsNot(strategy_engine.DAY_SESSION_ONLY_KINDS, strategy_engine.DAY_SIGNAL_KINDS)


class TestNoonConfirmWindow(unittest.TestCase):
    """【ADR-128】12:00 二次確認的時間窗口收斂成 core 的純函式。"""

    def _d(self, h, m, s=0):
        from datetime import datetime as _dt
        return _dt(2026, 7, 30, h, m, s)

    def test_inside_window(self):
        for h, m in ((12, 0), (12, 1), (12, 4)):
            self.assertTrue(chukuangren_band.in_noon_confirm_window(self._d(h, m)),
                            f"{h}:{m:02d} 應在窗口內")
        self.assertTrue(chukuangren_band.in_noon_confirm_window(self._d(12, 4, 59)))

    def test_outside_window(self):
        for h, m in ((11, 59), (12, 5), (12, 30), (13, 0), (9, 0)):
            self.assertFalse(chukuangren_band.in_noon_confirm_window(self._d(h, m)),
                             f"{h}:{m:02d} 不該在窗口內")

    def test_window_is_five_minutes_not_one_instant(self):
        """窗口刻意不是「12:00 那一秒」:確認要抓 5分K,資料可能還在背景補齊
        (ADR-122) 或逾時重試 (ADR-121),需要可以重試的餘裕。這也是舊做法
        「一天只有一次機會」的修正重點。"""
        self.assertEqual(chukuangren_band.NOON_CONFIRM_HOUR, 12)
        self.assertGreaterEqual(chukuangren_band.NOON_CONFIRM_END_MINUTE, 2)

    def test_defensive_none_and_garbage(self):
        self.assertFalse(chukuangren_band.in_noon_confirm_window(None))
        self.assertFalse(chukuangren_band.in_noon_confirm_window('12:00'))


class TestMergedStopParams(unittest.TestCase):
    """【ADR-129】停損的「觸發」與「隔天12:00 確認」合併成同一個點位。

    使用者實機回報:「這兩個應該是要一樣,不需要分兩個,只需填一個 S1 就可以。
    (多單也是一樣)」。截圖裡他填 S1=41701 / S2=41700 —— 差 1 點,等於在為一個
    沒有實質作用的欄位傷腦筋。
    """

    def _strat(self, **over):
        s = chukuangren_band.default_strategy()
        s.update({'symbol': 'MXFR1', 'trade_type': '期貨', 'market': '台期貨', 'qty': 1,
                  'ck_x': 100.0, 'ck_y': 90.0, 'ck_z': 92.0,
                  'ck_s1': 110.0, 'ck_s2': 108.0, 'ck_c': 5.0, 'ck_f': 2.0})
        s.update(over)
        return s

    # ---------- params_of 是唯一的收斂點 ----------

    def test_confirm_level_is_overridden_by_trigger_level(self):
        p = chukuangren_band.params_of(self._strat())
        self.assertEqual(p['z'], p['y'], "多單:確認點位要等於觸發點位")
        self.assertEqual(p['s2'], p['s1'], "空單:確認點位要等於觸發點位")
        self.assertEqual(p['z'], 90.0)
        self.assertEqual(p['s2'], 110.0)

    def test_old_strategy_file_needs_no_migration(self):
        """舊策略檔裡的 z/s2 就算差很多,也一律不生效 —— 不做資料遷移。

        這是 ADR-123 的教訓:一個使用者看不到的欄位若還能左右出場行為,
        遲早會出事。"""
        p = chukuangren_band.params_of(self._strat(ck_z=99999.0, ck_s2=-1.0))
        self.assertEqual(p['z'], 90.0)
        self.assertEqual(p['s2'], 110.0)

    def test_other_params_are_untouched(self):
        """反向對照:只有 z/s2 被覆寫。少了這條,把 params_of 寫成「全部歸零」
        或「全部等於 y」也會讓上面兩條綠。"""
        p = chukuangren_band.params_of(self._strat())
        self.assertEqual(p['x'], 100.0)
        self.assertEqual(p['y'], 90.0)
        self.assertEqual(p['s1'], 110.0)
        self.assertEqual(p['c'], 5.0)
        self.assertEqual(p['f'], 2.0)

    def test_merged_pairs_point_at_the_trigger_not_the_other_way(self):
        """方向不可以寫反 —— 反了就變成「確認值覆寫觸發值」,而使用者填的是觸發值。"""
        self.assertEqual(chukuangren_band.MERGED_STOP_PAIRS, (('z', 'y'), ('s2', 's1')))

    # ---------- UI 參數清單 ----------

    def test_ui_no_longer_asks_for_the_confirm_level(self):
        for keys in (chukuangren_band.LONG_PARAM_KEYS, chukuangren_band.SHORT_PARAM_KEYS):
            self.assertNotIn('z', keys)
            self.assertNotIn('s2', keys)
        self.assertIn('y', chukuangren_band.LONG_PARAM_KEYS)
        self.assertIn('s1', chukuangren_band.SHORT_PARAM_KEYS)

    def test_data_format_still_carries_the_keys(self):
        """z/s2 留在 PARAM_KEYS:舊策略檔讀得進來,使用者降版回舊程式也不會壞。"""
        self.assertIn('z', chukuangren_band.PARAM_KEYS)
        self.assertIn('s2', chukuangren_band.PARAM_KEYS)
        # 每個 key 都要有標籤與說明,否則編輯器 _mk_param_rows 會 KeyError
        for k in chukuangren_band.PARAM_KEYS:
            self.assertIn(k, chukuangren_band.PARAM_LABELS)
            self.assertIn(k, chukuangren_band.PARAM_HELP)

    # ---------- 行為差異:合併之後出場判斷真的改變 ----------

    def test_long_confirm_now_follows_y_not_the_old_z(self):
        """做多、Y=90、舊檔 Z=92,隔天12:00 指數 91:
             舊行為 91 < Z=92 → 平倉
             新行為 91 > Y=90 → 作廢、繼續持有
        這條就是合併前後**唯一會看到差別**的地方,所以一定要測到。"""
        p = chukuangren_band.params_of(self._strat())     # 經過 params_of
        rt = strategy_engine.new_runtime()
        rt['state'] = 'LONG'; rt['qty'] = 2
        rt['pending_exit'] = {'reason': 'SL', 'date': '2026-01-01'}
        chukuangren_band.on_noon_confirm(p, rt, 91.0, '2026-01-02', now_ts=1000.0, qty=2)
        self.assertIsNone(rt.get('armed_intent'),
                          "91 已站回 Y=90 之上,不該平倉 (舊行為會因為 91<Z=92 而平倉)")
        self.assertEqual(rt['state'], 'LONG')
        # 正控:真的跌破 Y 就要平倉 —— 少了這條,把停損整個關掉也會綠
        rt2 = strategy_engine.new_runtime()
        rt2['state'] = 'LONG'; rt2['qty'] = 2
        rt2['pending_exit'] = {'reason': 'SL', 'date': '2026-01-01'}
        chukuangren_band.on_noon_confirm(p, rt2, 89.0, '2026-01-02', now_ts=1000.0, qty=2)
        self.assertIsNotNone(rt2.get('armed_intent'), "89 < Y=90,停損確認要成立")
        self.assertEqual(rt2['armed_intent']['action'], '賣出')

    def test_short_confirm_now_follows_s1_not_the_old_s2(self):
        p = chukuangren_band.params_of(self._strat())
        rt = strategy_engine.new_runtime()
        rt['state'] = 'SHORT'; rt['qty'] = 2
        rt['pending_exit'] = {'reason': 'SL', 'date': '2026-01-01'}
        chukuangren_band.on_noon_confirm(p, rt, 109.0, '2026-01-02', now_ts=1000.0, qty=2)
        self.assertIsNone(rt.get('armed_intent'),
                          "109 已回到 S1=110 之下,不該平倉 (舊行為會因為 109>S2=108 而平倉)")
        # 正控
        rt2 = strategy_engine.new_runtime()
        rt2['state'] = 'SHORT'; rt2['qty'] = 2
        rt2['pending_exit'] = {'reason': 'SL', 'date': '2026-01-01'}
        chukuangren_band.on_noon_confirm(p, rt2, 111.0, '2026-01-02', now_ts=1000.0, qty=2)
        self.assertIsNotNone(rt2.get('armed_intent'), "111 > S1=110,停損確認要成立")
        self.assertEqual(rt2['armed_intent']['action'], '買進')

    # ---------- validate 的訊息 ----------

    def test_validate_accepts_the_users_own_screenshot_params(self):
        """【ADR-130】使用者截圖那組 (做空、X=42500、停損 41701) 現在必須存得起來。

        原本被擋,理由是「做空要指數漲上去才停損」—— 但那是拿設定的分界 X 當
        進場價在推論。實際進場要等 X 被跌破、再等隔天12:00確認,那時指數可能
        已經在 41800,停損設 42000 才貼著實際進場價。"""
        s = self._strat(direction='做空', ck_x=42500.0, ck_s1=41701.0)
        ok, msg = chukuangren_band.validate(s)
        self.assertTrue(ok, f"不該再擋這組參數: {msg}")

    def test_validate_still_rejects_an_unset_stop(self):
        """反向對照:相對關係不擋,但「有沒有填」一定要擋。

        0 不是合法的加權指數點位,而 0 的後果是災難性的:
          做多 Y=0 → `close < 0` 永遠不成立 → **完全沒有停損**
          做空 S1=0 → `close > 0` 永遠成立 → 每天都誤記待確認停損
        舊規則只「順便」擋到做空那半邊,做多的 Y=0 一直是沒人擋的洞 (ADR-130 補上)。"""
        s = self._strat(direction='做多', ck_x=42500.0, ck_y=0.0)
        ok, msg = chukuangren_band.validate(s)
        self.assertFalse(ok, "Y=0 等於沒有停損,必須擋下來")
        self.assertIn('Y', msg)
        s2 = self._strat(direction='做空', ck_x=42500.0, ck_s1=0.0)
        ok2, msg2 = chukuangren_band.validate(s2)
        self.assertFalse(ok2, "S1=0 會讓系統每天誤判待確認停損,必須擋下來")
        self.assertIn('S1', msg2)

    def test_validate_still_rejects_unset_x(self):
        """X=0 同樣是壞資料:做多會變成「收盤 > 0」→ 每天都進場。"""
        s = self._strat(direction='做多', ck_x=0.0, ck_y=90.0)
        ok, msg = chukuangren_band.validate(s)
        self.assertFalse(ok)
        self.assertIn('X', msg)

    def test_validate_passes_with_only_the_single_stop_filled(self):
        """反向對照:只填一個停損點位就必須存得起來 —— 這才是使用者要的。
        (舊程式沒有 z/s2 的檢查,所以這裡真正守的是「別為了合併而多加驗證」)"""
        s = self._strat(direction='做多', ck_x=42500.0, ck_y=42000.0)
        s.pop('ck_z', None)
        ok, msg = chukuangren_band.validate(s)
        self.assertTrue(ok, f"只填 Y 應該可以存檔,實際被擋: {msg}")
        s2 = self._strat(direction='做空', ck_x=42500.0, ck_s1=43000.0, trade_type='期貨')
        s2.pop('ck_s2', None)
        ok2, msg2 = chukuangren_band.validate(s2)
        self.assertTrue(ok2, f"只填 S1 應該可以存檔,實際被擋: {msg2}")



class TestStopLevelIndependentOfX(unittest.TestCase):
    """【ADR-130】停損只看使用者填的那個點位,跟進出場分界 X 的高低無關。

    使用者的要求:
      「有時候停損點的位置不一定會高於進場點,因為實際進場的位置,已經離一開始
        設定的進場位置太遠,導致虧損會很大。所以你說停損點必須大於進出場分界,
        這一點不要被限制,是允許我自行設定,你只要依照我設定的停損位置去判斷
        是否符合條件即可。」

    重點:**實際進場價不是 X**。進場要等 X 被突破/跌破,再等隔天 12:00 二次確認,
    那時指數可能已經離 X 很遠 —— 停損要貼著實際進場價才控得住虧損。
    """

    def _mk_daily_df(self, closes):
        idx = pd.date_range('2026-01-01', periods=len(closes), freq='D')
        return pd.DataFrame({'Open': closes, 'High': [c + 1 for c in closes],
                             'Low': [c - 1 for c in closes], 'Close': closes,
                             'Volume': [1000] * len(closes)}, index=idx)

    def _short_params(self):
        # 做空:X=42500,但停損設在 X **之下** 的 42000 (舊規則會擋掉)
        return {'x': 42500.0, 'y': 0.0, 'z': 0.0,
                's1': 42000.0, 's2': 42000.0, 'c': 1000.0, 'f': 200.0}

    def _short_rt(self, entry_index_price=41800.0):
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'SHORT', 'qty': 5, 'entry_price': 41800.0,
                   'exec_entry_price': 41800.0, 'entry_index_price': entry_index_price})
        chukuangren_band.ensure_runtime(rt)
        return rt

    def test_short_stop_below_x_does_not_trigger_while_index_stays_under_it(self):
        """指數 41900 < S1=42000 → 不停損,即使 41900 也遠低於 X=42500。"""
        p = self._short_params()
        rt = self._short_rt()
        chukuangren_band.on_daily_close(p, rt, self._mk_daily_df([41900.0] * 25),
                                        direction='做空')
        self.assertIsNone(rt['pending_exit'],
                          "41900 還在 S1=42000 之下,不該停損")

    def test_short_stop_below_x_triggers_when_index_crosses_it(self):
        """指數 42100 > S1=42000 → 停損,而且**不必**漲過 X=42500。

        這正是使用者要的:停損貼著實際進場價 (41800),而不是被迫等到 42500。"""
        p = self._short_params()
        rt = self._short_rt()
        chukuangren_band.on_daily_close(p, rt, self._mk_daily_df([42100.0] * 25),
                                        direction='做空')
        self.assertIsNotNone(rt['pending_exit'], "42100 已突破 S1=42000,要記待確認停損")
        self.assertEqual(rt['pending_exit']['reason'], 'SL')
        # 隔天 12:00 仍高於 S1 → 真的平倉
        chukuangren_band.on_noon_confirm(p, rt, 42100.0, '2026-02-01', now_ts=1000.0, qty=5)
        self.assertIsNotNone(rt.get('armed_intent'))
        self.assertEqual(rt['armed_intent']['action'], '買進')

    def test_short_stop_below_x_invalidates_when_index_falls_back(self):
        """反向對照:隔天 12:00 已回到 S1 之下 → 作廢、繼續持有。"""
        p = self._short_params()
        rt = self._short_rt()
        chukuangren_band.on_daily_close(p, rt, self._mk_daily_df([42100.0] * 25),
                                        direction='做空')
        chukuangren_band.on_noon_confirm(p, rt, 41950.0, '2026-02-01', now_ts=1000.0, qty=5)
        self.assertIsNone(rt.get('armed_intent'), "41950 已回到 S1=42000 之下,應作廢")
        self.assertEqual(rt['state'], 'SHORT')

    def test_long_stop_above_x_is_judged_by_the_stop_level_only(self):
        """做多對稱:X=42500、停損 Y=43000 (在 X **之上**,舊規則會擋)。
        實際進場價 43200,指數 42900 < Y=43000 → 停損。"""
        p = {'x': 42500.0, 'y': 43000.0, 'z': 43000.0,
             's1': 0.0, 's2': 0.0, 'c': 1000.0, 'f': 200.0}
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 3, 'entry_price': 43200.0,
                   'exec_entry_price': 43200.0, 'entry_index_price': 43200.0})
        chukuangren_band.ensure_runtime(rt)
        chukuangren_band.on_daily_close(p, rt, self._mk_daily_df([42900.0] * 25),
                                        direction='做多')
        self.assertIsNotNone(rt['pending_exit'], "42900 已跌破 Y=43000,要記待確認停損")
        self.assertEqual(rt['pending_exit']['reason'], 'SL')
        # 正控:還在 Y 之上就不該停損
        rt2 = strategy_engine.new_runtime()
        rt2.update({'state': 'LONG', 'qty': 3, 'entry_price': 43200.0,
                    'exec_entry_price': 43200.0, 'entry_index_price': 43200.0})
        chukuangren_band.ensure_runtime(rt2)
        chukuangren_band.on_daily_close(p, rt2, self._mk_daily_df([43100.0] * 25),
                                        direction='做多')
        self.assertIsNone(rt2['pending_exit'], "43100 還在 Y=43000 之上,不該停損")

    def test_entry_still_uses_x_only(self):
        """反向對照:**進場**仍然只看 X —— 這次只放寬停損,不可以連進場一起弄鬆。"""
        p = self._short_params()          # X=42500
        rt = strategy_engine.new_runtime()
        chukuangren_band.ensure_runtime(rt)
        # 42400 < X=42500 → 記待確認空單進場
        chukuangren_band.on_daily_close(p, rt, self._mk_daily_df([42400.0] * 25),
                                        direction='做空')
        self.assertIsNotNone(rt['pending_entry'])
        self.assertEqual(rt['pending_entry']['dir'], 'SHORT')
        # 42600 > X → 不進場
        rt2 = strategy_engine.new_runtime()
        chukuangren_band.ensure_runtime(rt2)
        chukuangren_band.on_daily_close(p, rt2, self._mk_daily_df([42600.0] * 25),
                                        direction='做空')
        self.assertIsNone(rt2['pending_entry'], "42600 在 X 之上,做空不該進場")



class TestBollingerTwoSets(unittest.TestCase):
    """【ADR-131】布林改成「兩組完整通道」:每組有自己的中線 (類型+期間) 與
    自己的上下線 (ADR-138 起,上線/下線各一個 σ)。

    背景:使用者實機截圖顯示主圖指標設定裡**布林那一整列不見了** (被說明文字
    蓋掉,見 diag 的 grid 版面檢查),同時要求「可以自己設定中線&上下線各2組
    的參數」。
    """

    def _ohlc(self, n=80):
        idx = pd.date_range("2026-01-01", periods=n, freq="D")
        rng = np.random.default_rng(7)
        close = 100 + np.cumsum(rng.normal(0, 1, n))
        return pd.DataFrame({'Open': close, 'High': close + 1,
                             'Low': close - 1, 'Close': close}, index=idx)

    def _calc(self, df, **over):
        kw = dict(bb_show=True, bbw_show=False,
                  macd_show=False, macd_f="12", macd_s="26", macd_sig="9",
                  rsi_show=False, rsi_p="14",
                  kdj_show=False, kd_n="9", kd_m1="3", kd_m2="3",
                  dmi_show=False, dmi_n="14")
        kw.update(over)
        return indicators.calculate_indicators(
            df, [False] * 6, ["SMA"] * 6, ["5"] * 6, **kw)

    # ---------- moving_average:均線類型的單一出處 ----------

    def test_moving_average_kinds(self):
        s = pd.Series([float(i) for i in range(1, 21)])
        pd.testing.assert_series_equal(indicators.moving_average(s, 5, 'SMA'),
                                       s.rolling(window=5).mean())
        pd.testing.assert_series_equal(indicators.moving_average(s, 5, 'EMA'),
                                       s.ewm(span=5, adjust=False).mean())
        self.assertFalse(indicators.moving_average(s, 5, 'WMA').isna().all())
        # 大小寫/空白不該影響
        pd.testing.assert_series_equal(indicators.moving_average(s, 5, ' ema '),
                                       s.ewm(span=5, adjust=False).mean())

    def test_moving_average_bad_input_returns_none(self):
        s = pd.Series([1.0, 2.0, 3.0])
        self.assertIsNone(indicators.moving_average(s, 'abc', 'SMA'))
        self.assertIsNone(indicators.moving_average(s, 0, 'SMA'))
        self.assertIsNone(indicators.moving_average(s, 5, 'NOPE'))

    def test_ma_types_constant_matches_what_the_ui_offers(self):
        self.assertEqual(tuple(indicators.MA_TYPES), ('SMA', 'EMA', 'WMA'))

    # ---------- bollinger_set 的數學 ----------

    def test_bollinger_math_upper_and_lower_use_their_own_sigma(self):
        """【ADR-138】上線用 std_up、下線用 std_dn —— 兩個完全分開。
        刻意用**不對稱**的 2.0/3.0：兩邊一樣的話，把 std_dn 接錯邊也不會被拓到。"""
        df = self._ohlc()
        indicators.bollinger_set(df, 20, 2.0, 3.0, ma_type='SMA', prefix='BB',
                                 with_width=True)
        mid = df['Close'].rolling(window=20).mean()
        std = df['Close'].rolling(window=20).std()
        pd.testing.assert_series_equal(df['BB_MID'], mid, check_names=False)
        pd.testing.assert_series_equal(df['BB_UPPER'], mid + 2.0 * std, check_names=False)
        pd.testing.assert_series_equal(df['BB_LOWER'], mid - 3.0 * std, check_names=False)
        # 寬度跟著兩邊各自的 σ 走
        pd.testing.assert_series_equal(df['BB_WIDTH'], (5.0 * std) / mid * 100,
                                       check_names=False)

    def test_no_second_pair_of_bands_anymore(self):
        """【ADR-138】不再有「第二對上下線」—— 要第二條通道請開「第2組」。
        舊欄位名殘留在 df 裡的話，繪圖那邊會默默多畫兩條點線。"""
        df = self._ohlc()
        indicators.bollinger_set(df, 20, 2.0, 3.0, prefix='BB')
        self.assertNotIn('BB_UPPER2', df.columns)
        self.assertNotIn('BB_LOWER2', df.columns)

    def test_sigma_zero_or_bad_falls_back_to_two_not_to_a_flat_line(self):
        """σ 填 0 / 負數 / 亂打一通 → 退回 2.0。
        不可以照單全部用 0：那會讓上下線跟中線重疊，看起來像「布林壞了」。"""
        df = self._ohlc()
        indicators.bollinger_set(df, 20, 0.0, 'abc', prefix='BB')
        mid = df['Close'].rolling(window=20).mean()
        std = df['Close'].rolling(window=20).std()
        pd.testing.assert_series_equal(df['BB_UPPER'], mid + 2.0 * std, check_names=False)
        pd.testing.assert_series_equal(df['BB_LOWER'], mid - 2.0 * std, check_names=False)

    def test_std_always_from_close_regardless_of_middle_type(self):
        """中線可以是 EMA/WMA,但標準差一律取收盤價的 rolling std ——
        那是布林通道的定義,不隨中線類型改變。"""
        df_s, df_e = self._ohlc(), self._ohlc()
        indicators.bollinger_set(df_s, 20, 2.0, 0.0, ma_type='SMA', prefix='BB')
        indicators.bollinger_set(df_e, 20, 2.0, 0.0, ma_type='EMA', prefix='BB')
        pd.testing.assert_series_equal(df_s['BB_STD'], df_e['BB_STD'])
        # 但中線本身要真的不一樣 (否則等於 ma_type 沒有生效)
        self.assertFalse(df_s['BB_MID'].equals(df_e['BB_MID']))

    def test_bad_params_fall_back_instead_of_crashing(self):
        """壞參數不可以讓整張圖畫不出來 (ADR-029 的降級慣例)。"""
        df = self._ohlc()
        indicators.bollinger_set(df, 'abc', 'x', 'y', prefix='BB', with_width=True)
        self.assertIn('BB_MID', df.columns)
        self.assertFalse(df['BB_MID'].isna().all())

    def test_prefix_isolates_the_two_sets(self):
        df = self._ohlc()
        indicators.bollinger_set(df, 20, 2.0, 0.0, prefix='BB')
        indicators.bollinger_set(df, 60, 1.5, 0.0, prefix='BB2')
        for c in ('BB_MID', 'BB_UPPER', 'BB_LOWER', 'BB2_MID', 'BB2_UPPER', 'BB2_LOWER'):
            self.assertIn(c, df.columns)
        self.assertFalse(df['BB_MID'].equals(df['BB2_MID']), "兩組中線期間不同,值不該一樣")

    # ---------- calculate_indicators 的整合 ----------

    def test_set1_column_names_unchanged(self):
        """第1組**必須**沿用原本的欄位名 —— 繪圖/十字線/BBW 副圖都靠它們,
        改名等於一次弄壞三個地方。這條是本次改動的迴歸防線。"""
        df = self._calc(self._ohlc(), bb_show=True, bbw_show=True,
                        bb_period=20, bb_std_up=2.0, bb_std_dn=3.0)
        for c in ('BB_MID', 'BB_STD', 'BB_UPPER', 'BB_LOWER', 'BB_WIDTH'):
            self.assertIn(c, df.columns)

    def test_second_set_off_by_default(self):
        """反向對照:沒開第2組就不可以產生 BB2 欄位 (否則圖上會多出線)。"""
        df = self._calc(self._ohlc(), bb_show=True)
        self.assertNotIn('BB2_MID', df.columns)

    def test_second_set_on_produces_its_own_columns(self):
        df = self._calc(self._ohlc(), bb_show=True, bb_period=20,
                        bb2_show=True, bb2_period=60, bb2_std_up=1.5, bb2_std_dn=2.5,
                        bb2_type='EMA')
        for c in ('BB2_MID', 'BB2_UPPER', 'BB2_LOWER'):
            self.assertIn(c, df.columns)
        # 第2組的中線是 EMA(60),跟第1組 SMA(20) 必須不同
        self.assertFalse(df['BB_MID'].equals(df['BB2_MID']))
        # BB_WIDTH 只由第1組產生 (副圖只有一條,不改副圖語意)
        self.assertNotIn('BB2_WIDTH', df.columns)

    def test_second_set_independent_of_first(self):
        """第2組開著、第1組關著也要能畫 —— 兩組互相獨立。"""
        df = self._calc(self._ohlc(), bb_show=False, bbw_show=False,
                        bb2_show=True, bb2_period=30)
        self.assertIn('BB2_MID', df.columns)
        self.assertNotIn('BB_MID', df.columns)

    def test_bb_type_applies_to_first_set(self):
        df_s = self._calc(self._ohlc(), bb_show=True, bb_period=20, bb_type='SMA')
        df_e = self._calc(self._ohlc(), bb_show=True, bb_period=20, bb_type='EMA')
        self.assertFalse(df_s['BB_MID'].equals(df_e['BB_MID']),
                         "bb_type 沒有生效 (兩種中線算出來一樣)")



class TestRegimeDailyNotify(unittest.TestCase):
    """【ADR-132】每天收盤後把盤勢判斷推播到手機。

    使用者要求:「如果有出現相關盤勢,要用傳訊息到我的手機通知我,跟我說是
    什麼型態或是支撐壓力。每天一次,收盤後通知即可。」以及「額外增加對 60分K
    的盤勢判斷 ... 要註明是 60分K 出現」。
    """

    def _dt(self, h, m, day=10):
        from datetime import datetime as _d
        return _d(2026, 3, day, h, m)

    def _on(self, **over):
        s = {'enabled': True, 'notify_enabled': True}
        s.update(over)
        return s

    # ---------- 推播時刻 ----------

    def test_default_time_is_after_market_close(self):
        """台股 13:30 收盤 —— 預設時刻一定要晚於收盤,否則抓到的是盤中資料。"""
        self.assertGreater(regime_panel.notify_minute({}), 13 * 60 + 30)

    def test_not_before_the_configured_time(self):
        s = self._on(notify_hhmm='14:00')
        self.assertFalse(regime_panel.should_notify_now(s, self._dt(13, 59), None))
        self.assertTrue(regime_panel.should_notify_now(s, self._dt(14, 0), None))
        self.assertTrue(regime_panel.should_notify_now(s, self._dt(14, 1), None))

    def test_late_start_still_sends(self):
        """**過了時刻就送**,不是「剛好那一分鐘才送」。

        程式可能 14:00 沒開機、或正忙著別的事。錯過那一分鐘就整天不發,
        對一天只發一次的通知是最糟的失敗模式 —— 使用者不會知道自己漏掉了。"""
        s = self._on(notify_hhmm='14:00')
        self.assertTrue(regime_panel.should_notify_now(s, self._dt(21, 30), None))

    def test_only_once_per_day(self):
        s = self._on(notify_hhmm='14:00')
        self.assertFalse(regime_panel.should_notify_now(s, self._dt(15, 0), '2026-03-10'))
        # 隔天要恢復
        self.assertTrue(regime_panel.should_notify_now(s, self._dt(15, 0, day=11), '2026-03-10'))

    def test_switches_off(self):
        self.assertFalse(regime_panel.should_notify_now(
            {'enabled': True, 'notify_enabled': False}, self._dt(15, 0), None))
        self.assertFalse(regime_panel.should_notify_now(
            {'enabled': False, 'notify_enabled': True}, self._dt(15, 0), None))

    def test_bad_time_falls_back_to_default(self):
        """壞設定不可以變成「整天不停推播」或「永遠不推播」。"""
        for bad in ('99:99', 'abc', '', None, '14', 12345):
            self.assertEqual(regime_panel.normalize({'notify_hhmm': bad})['notify_hhmm'],
                             regime_panel.DEFAULT_NOTIFY_HHMM, repr(bad))
        self.assertEqual(regime_panel.normalize({'notify_hhmm': '9:5'})['notify_hhmm'], '09:05')

    def test_none_datetime_is_safe(self):
        self.assertFalse(regime_panel.should_notify_now(self._on(), None, None))

    # ---------- 週期清單 ----------

    def test_notify_timeframes_default_and_filtering(self):
        self.assertEqual(tuple(regime_panel.notify_timeframes({})),
                         regime_panel.PATTERN_TIMEFRAMES)
        self.assertEqual(regime_panel.notify_timeframes({'notify_timeframes': ['60分K']}),
                         ['60分K'])
        # 認不得的週期要被濾掉 (不可以讓 5分K 混進推播)
        self.assertEqual(regime_panel.notify_timeframes(
            {'notify_timeframes': ['5分K', '日K']}), ['日K'])
        # 有欄位但空的 → 尊重使用者真的全取消 (同 pattern_list 的處理)
        self.assertEqual(regime_panel.notify_timeframes({'notify_timeframes': []}), [])

    # ---------- 訊息組裝 ----------

    def _sig(self, label, bias='偏多'):
        return {'pattern': 'regime', 'label': label, 'bias': bias}

    def test_report_labels_every_timeframe(self):
        """使用者明確要求「要註明是 60分K 出現」—— 每一段都要標週期。"""
        txt = regime_panel.format_daily_report(
            '2026-03-10',
            [('日K', [self._sig('多頭趨勢')], '現價 100|壓力 110|支撐 90'),
             ('60分K', [self._sig('區間整理', '中性')], None)])
        self.assertIn('[日K]', txt)
        self.assertIn('[60分K]', txt)
        self.assertIn('多頭趨勢', txt)
        self.assertIn('區間整理', txt)
        self.assertIn('支撐', txt)
        self.assertIn('2026-03-10', txt)

    def test_report_is_none_when_nothing_to_say(self):
        """沒東西講就不發 —— 一天一次的通知一旦變成噪音就沒人看了 (P-99)。"""
        self.assertIsNone(regime_panel.format_daily_report('2026-03-10', []))
        self.assertIsNone(regime_panel.format_daily_report(
            '2026-03-10', [('日K', [], None), ('60分K', [], None)]))

    def test_report_sends_when_only_sr_is_available(self):
        """反向對照:只有支撐壓力、沒有型態時**仍然要發** ——
        使用者要的是「型態**或**支撐壓力」。"""
        txt = regime_panel.format_daily_report('2026-03-10', [('日K', [], '現價 100|支撐 90')])
        self.assertIsNotNone(txt)
        self.assertIn('支撐壓力', txt)

    def test_report_prefix_is_the_pushable_one(self):
        """前綴要能被 telegram_notify.is_quant_message() 認出來 —— 這則訊息
        同時會寫進系統日誌,前綴不對就只有日誌看得到。"""
        txt = regime_panel.format_daily_report('2026-03-10', [('日K', [self._sig('多頭趨勢')], None)])
        self.assertTrue(telegram_notify.is_quant_message(txt))



class TestFibonacci(unittest.TestCase):
    """【ADR-133】黃金切割律 (費波南希回撤)。

    比例的定義:百分比是「**已經回撤掉多少**」——
      上升段 (低→高):0% 在高點、100% 在低點,價位 = high - (high-low)*r
      下降段 (高→低):0% 在低點、100% 在高點,價位 = low + (high-low)*r
    兩個方向同一條算式,只差起算端,所以 r>1 自然落在起點的另一側 (延伸目標)。
    """

    def _df(self, closes):
        n = len(closes)
        return pd.DataFrame({'Open': closes,
                             'High': [c + 1 for c in closes],
                             'Low': [c - 1 for c in closes],
                             'Close': closes,
                             'Volume': [100] * n},
                            index=pd.date_range('2026-01-01', periods=n, freq='D'))

    # ---------- 比率清單 ----------

    def test_key_ratios_are_the_two_most_watched(self):
        """0.618 是黃金比例、0.382 是另一條最常看的線。"""
        self.assertIn(0.618, fibonacci.KEY_RATIOS)
        self.assertIn(0.382, fibonacci.KEY_RATIOS)
        self.assertTrue(fibonacci.is_key_ratio(0.618))
        self.assertFalse(fibonacci.is_key_ratio(0.5))

    def test_default_levels_cover_the_standard_set(self):
        for r in (0.236, 0.382, 0.5, 0.618, 0.786):
            self.assertIn(r, fibonacci.DEFAULT_LEVELS, f"缺少標準比率 {r}")

    def test_secondary_levels_are_the_taiwan_convention(self):
        """次級分割律:0 與 0.382 的中間值 0.191、0.618 與 1 的中間值 0.809。"""
        self.assertEqual(tuple(fibonacci.SECONDARY_LEVELS), (0.191, 0.809))
        self.assertAlmostEqual(0.191, (0.0 + 0.382) / 2, places=3)
        self.assertAlmostEqual(0.809, (0.618 + 1.0) / 2, places=3)

    def test_normalize_ratios_is_defensive(self):
        """壞設定不可以讓主圖畫不出來。"""
        self.assertEqual(fibonacci.normalize_ratios(None), tuple(fibonacci.DEFAULT_LEVELS))
        self.assertEqual(fibonacci.normalize_ratios([]), tuple(fibonacci.DEFAULT_LEVELS))
        self.assertEqual(fibonacci.normalize_ratios(['abc', None]), tuple(fibonacci.DEFAULT_LEVELS))
        # 去重 + 排序 + 濾掉離譜值
        self.assertEqual(fibonacci.normalize_ratios([0.618, 0.382, 0.618, -1, 99]),
                         (0.382, 0.618))

    # ---------- 擺盪點與方向 ----------

    def test_uptrend_when_high_comes_last(self):
        sw = fibonacci.find_swing(self._df([100.0] + [100 + i for i in range(1, 40)]), 60)
        self.assertEqual(sw['trend'], fibonacci.TREND_UP)

    def test_downtrend_when_low_comes_last(self):
        sw = fibonacci.find_swing(self._df([140 - i for i in range(40)]), 60)
        self.assertEqual(sw['trend'], fibonacci.TREND_DOWN)

    def test_lookback_limits_the_window(self):
        """只看最近 N 根 —— 很久以前的極值不該綁架現在的切割區間。"""
        closes = [500.0] * 10 + [100.0 + i for i in range(30)]
        sw = fibonacci.find_swing(self._df(closes), 30)
        self.assertLess(sw['high'], 200.0, "取樣窗外的 500 不該被算進來")

    def test_flat_data_returns_none(self):
        """完全沒有波動 (High == Low) 就沒有可切割的區間。

        注意 _df() 會把 High/Low 撐開 ±1,所以「收盤價都一樣」**不等於**
        沒有波動 —— 這裡要自己造一份真正持平的資料 (第一版寫錯,測試當場紅)。"""
        flat = pd.DataFrame({'Open': [100.0] * 30, 'High': [100.0] * 30,
                             'Low': [100.0] * 30, 'Close': [100.0] * 30,
                             'Volume': [100] * 30},
                            index=pd.date_range('2026-01-01', periods=30, freq='D'))
        self.assertIsNone(fibonacci.find_swing(flat, 30))
        # 反向對照:有波動就要算得出來
        self.assertIsNotNone(fibonacci.find_swing(self._df([100.0] * 30), 30))

    def test_bad_input_is_safe(self):
        self.assertIsNone(fibonacci.find_swing(None, 60))
        self.assertIsNone(fibonacci.find_swing(pd.DataFrame(), 60))
        # lookback 壞掉要退回預設而不是爆炸
        self.assertIsNotNone(fibonacci.find_swing(
            self._df([100 + i for i in range(40)]), 'abc'))

    # ---------- 價位計算 ----------

    def test_uptrend_level_prices(self):
        """上升段 100→200:0% 在高點 200、100% 在低點 100、61.8% 在 138.2。"""
        hi, lo = 200.0, 100.0
        self.assertAlmostEqual(fibonacci.level_price(hi, lo, fibonacci.TREND_UP, 0.0), 200.0)
        self.assertAlmostEqual(fibonacci.level_price(hi, lo, fibonacci.TREND_UP, 1.0), 100.0)
        self.assertAlmostEqual(fibonacci.level_price(hi, lo, fibonacci.TREND_UP, 0.5), 150.0)
        self.assertAlmostEqual(fibonacci.level_price(hi, lo, fibonacci.TREND_UP, 0.618), 138.2)
        self.assertAlmostEqual(fibonacci.level_price(hi, lo, fibonacci.TREND_UP, 0.382), 161.8)

    def test_downtrend_level_prices_are_mirrored(self):
        """下降段 200→100:0% 在低點 100、100% 在高點 200、61.8% 在 161.8。"""
        hi, lo = 200.0, 100.0
        self.assertAlmostEqual(fibonacci.level_price(hi, lo, fibonacci.TREND_DOWN, 0.0), 100.0)
        self.assertAlmostEqual(fibonacci.level_price(hi, lo, fibonacci.TREND_DOWN, 1.0), 200.0)
        self.assertAlmostEqual(fibonacci.level_price(hi, lo, fibonacci.TREND_DOWN, 0.618), 161.8)

    def test_extension_falls_beyond_the_start(self):
        """延伸 (r>1) 要落在起點的另一側 —— 那才是「跌破/突破之後的目標」。"""
        hi, lo = 200.0, 100.0
        up = fibonacci.level_price(hi, lo, fibonacci.TREND_UP, 1.618)
        self.assertLess(up, lo, "上升段的延伸應在低點之下 (跌破後的目標)")
        dn = fibonacci.level_price(hi, lo, fibonacci.TREND_DOWN, 1.618)
        self.assertGreater(dn, hi, "下降段的延伸應在高點之上")

    def test_levels_are_ordered_and_tagged(self):
        sw = {'high': 200.0, 'low': 100.0, 'trend': fibonacci.TREND_UP}
        lv = fibonacci.levels(sw, [0.0, 0.382, 0.618, 1.0, 1.618])
        self.assertEqual([x['ratio'] for x in lv], [0.0, 0.382, 0.618, 1.0, 1.618])
        kinds = {x['ratio']: x['kind'] for x in lv}
        self.assertEqual(kinds[0.0], 'endpoint')
        self.assertEqual(kinds[1.0], 'endpoint')
        self.assertEqual(kinds[0.618], 'retrace')
        self.assertEqual(kinds[1.618], 'extend')
        self.assertTrue([x for x in lv if x['ratio'] == 0.618][0]['key'])

    def test_all_prices_inside_the_band_for_retracements(self):
        """反向對照:0~1 之間的比例算出來一定落在高低點之間。
        算式寫反 (例如把 high-低 寫成 low-high) 這條就會紅。"""
        sw = {'high': 200.0, 'low': 100.0, 'trend': fibonacci.TREND_UP}
        for x in fibonacci.levels(sw, fibonacci.DEFAULT_LEVELS):
            self.assertGreaterEqual(x['price'], 100.0 - 1e-9, x['label'])
            self.assertLessEqual(x['price'], 200.0 + 1e-9, x['label'])

    def test_compute_and_summarize(self):
        r = fibonacci.compute(self._df([100 + i for i in range(60)]), 60,
                              fibonacci.DEFAULT_LEVELS)
        self.assertIsNotNone(r)
        self.assertEqual(r['swing']['trend'], fibonacci.TREND_UP)
        txt = fibonacci.summarize(r)
        self.assertIn('0.618', txt)
        self.assertIn(fibonacci.TREND_UP, txt)
        self.assertIn('資料不足', fibonacci.summarize(None))

    def test_ratio_label_is_readable(self):
        self.assertEqual(fibonacci.ratio_label(0.5), '0.5')
        self.assertEqual(fibonacci.ratio_label(0.618), '0.618')
        self.assertEqual(fibonacci.ratio_label(1.0), '1')
        self.assertEqual(fibonacci.ratio_label(0.0), '0')



class TestIndicatorHelpersExtracted(unittest.TestCase):
    """【ADR-134】RSI/KDJ 抽成可重用函式後,結果必須與抽出前**逐位元相同**。

    這是純重構的迴歸防線:JAE 要共用同一份算式,但不可以順手改動既有指標。
    """

    def _ohlc(self, n=120):
        idx = pd.date_range('2026-01-01', periods=n, freq='D')
        rng = np.random.default_rng(11)
        close = 100 + np.cumsum(rng.normal(0, 1, n))
        return pd.DataFrame({'Open': close, 'High': close + 1.5,
                             'Low': close - 1.5, 'Close': close}, index=idx)

    def test_rsi_matches_the_inline_formula(self):
        df = self._ohlc()
        p = 14
        delta = df['Close'].diff()
        gain = delta.clip(lower=0).ewm(com=p - 1, adjust=False).mean()
        loss = (-1 * delta.clip(upper=0)).ewm(com=p - 1, adjust=False).mean()
        expect = 100 - (100 / (1 + (gain / loss)))
        pd.testing.assert_series_equal(indicators.rsi(df['Close'], p), expect,
                                       check_names=False)

    def test_kdj_matches_the_standard_formula(self):
        """標準 KDJ:K = (1/3)RSV + (2/3)K_prev (m1=3 → ewm(com=2));J = 3K - 2D。"""
        df = self._ohlc()
        n, m1, m2 = 9, 3, 3
        low_min = df['Low'].rolling(window=n).min()
        high_max = df['High'].rolling(window=n).max()
        rsv = 100 * (df['Close'] - low_min) / (high_max - low_min)
        k = rsv.ewm(com=m1 - 1, adjust=False).mean()
        d = k.ewm(com=m2 - 1, adjust=False).mean()
        got_rsv, got_k, got_d, got_j = indicators.kdj(df, n, m1, m2)
        pd.testing.assert_series_equal(got_rsv, rsv, check_names=False)
        pd.testing.assert_series_equal(got_k, k, check_names=False)
        pd.testing.assert_series_equal(got_d, d, check_names=False)
        pd.testing.assert_series_equal(got_j, 3 * k - 2 * d, check_names=False)

    def test_calculate_indicators_still_produces_the_same_columns(self):
        df = self._ohlc()
        out = indicators.calculate_indicators(
            df, [False] * 6, ['SMA'] * 6, ['5'] * 6,
            bb_show=False, bbw_show=False,
            macd_show=False, macd_f='12', macd_s='26', macd_sig='9',
            rsi_show=True, rsi_p='14',
            kdj_show=True, kd_n='9', kd_m1='3', kd_m2='3',
            dmi_show=False, dmi_n='14')
        for c in ('RSI', 'RSV', 'K', 'D', 'J'):
            self.assertIn(c, out.columns)
        pd.testing.assert_series_equal(out['RSI'], indicators.rsi(df['Close'], 14),
                                       check_names=False)


class TestJAE(unittest.TestCase):
    """【ADR-134】JAE 指標 (使用者自創:A=RSI、J=KDJ的J、E=長期趨勢線)。"""

    def _df(self, closes):
        return pd.DataFrame({'Open': closes, 'High': [c + 1 for c in closes],
                             'Low': [c - 1 for c in closes], 'Close': closes},
                            index=pd.date_range('2026-01-01', periods=len(closes), freq='D'))

    def _rising(self, n=200):
        """上升趨勢,但**要有回檔**。

        一條完全筆直的上升線會讓 RSI 恆等於 100 (一天都沒有下跌),
        E 跟著貼在 100、斜率變 0 → 判成「盤整」。那是測試資料退化,
        不是程式錯 —— 第一版就是這樣紅的。真實行情一定有回檔,
        所以固定用「上升 + 小幅震盪」當素材。"""
        return self._df([100.0 + i * 0.8 + (2.0 if i % 3 == 0 else -1.5)
                         for i in range(n)])

    def _falling(self, n=200):
        return self._df([300.0 - i * 0.8 + (2.0 if i % 3 == 0 else -1.5)
                         for i in range(n)])

    # ---------- 三條線的來源 ----------

    def test_a_is_rsi_and_j_is_kdj_j(self):
        """A 與 J **必須**就是既有的 RSI 與 KDJ 的 J —— 使用者的定義。
        自己另算一份 (哪怕只差一點) 就違反定義。"""
        df = self._rising()
        p = dict(jae.DEFAULT_PARAMS)
        jae.compute(df, p)
        pd.testing.assert_series_equal(
            df[jae.COL_A], indicators.rsi(df['Close'], p['a_period']), check_names=False)
        _, _, _, j = indicators.kdj(df, p['j_n'], p['j_m1'], p['j_m2'])
        pd.testing.assert_series_equal(df[jae.COL_J], j, check_names=False)

    def test_e_is_a_slow_version_of_a_on_the_same_scale(self):
        """E 要跟 A/J 同尺度才交叉得起來,而且要比 A **平滑/慢**。

        「慢」用「每根的變動幅度」來測,不是用「最後一根誰比較高」——
        後者只在單調趨勢的最後一根成立,遇到回檔就會翻面 (第一版就是這樣紅的)。"""
        df = self._rising()
        jae.compute(df)
        a, e = df[jae.COL_A].dropna(), df[jae.COL_E].dropna()
        self.assertTrue((e.between(0, 100)).all(), "E 應與 RSI 同尺度 (0~100)")
        self.assertLess(float(e.diff().abs().mean()), float(a.diff().abs().mean()),
                        "E 每根的變動應該明顯小於 A (E 才叫長期線)")

    # ---------- 趨勢判斷 ----------

    def test_trend_up_needs_both_position_and_slope(self):
        df = self._rising()
        jae.compute(df)
        trend, e_now, slope = jae.trend_of(df[jae.COL_E])
        self.assertEqual(trend, jae.TREND_UP)
        self.assertGreater(e_now, 50.0)
        self.assertGreater(slope, 0)

    def test_trend_down(self):
        df = self._falling()
        jae.compute(df)
        trend, e_now, slope = jae.trend_of(df[jae.COL_E])
        self.assertEqual(trend, jae.TREND_DOWN)

    def test_steady_trend_is_not_mistaken_for_flat(self):
        """**這條是設計被改掉的原因。**

        RSI 量的是動能不是價格方向,所以一段「等速下跌」會讓 RSI 穩定在低點、
        E 收斂成一條水平線 —— 斜率趨近 0。第一版要求「位置與斜率兩個都成立」,
        於是最典型的持續下跌反而被判成「盤整」。改成位置為主之後才正確。"""
        e = pd.Series([34.5, 34.6, 34.5, 34.5, 34.6, 34.5])   # 遠低於中線、幾乎不動
        trend, _, slope = jae.trend_of(e, {'e_slope_bars': 3})
        self.assertLess(abs(slope), 0.5, "前提:斜率確實幾乎是 0")
        self.assertEqual(trend, jae.TREND_DOWN, "遠離中線本身就是趨勢的證據")
        e_up = pd.Series([70.0, 70.1, 70.0, 70.0, 70.1, 70.0])
        self.assertEqual(jae.trend_of(e_up, {'e_slope_bars': 3})[0], jae.TREND_UP)

    def test_near_midline_falls_back_to_slope(self):
        """中線附近位置說不準,才由斜率決定偏哪一邊。"""
        near_up = pd.Series([48.0, 48.5, 49.0, 49.5, 50.0, 50.5])
        self.assertEqual(jae.trend_of(near_up, {'e_slope_bars': 3})[0], jae.TREND_UP)
        near_dn = pd.Series([52.0, 51.5, 51.0, 50.5, 50.0, 49.5])
        self.assertEqual(jae.trend_of(near_dn, {'e_slope_bars': 3})[0], jae.TREND_DOWN)

    def test_flat_near_midline_is_the_only_way_to_get_flat(self):
        """反向對照:「盤整」不可以消失 —— 中線附近又幾乎不動才是真的盤整。"""
        e = pd.Series([50.0, 50.01, 50.0, 50.0, 50.01, 50.0])
        self.assertEqual(jae.trend_of(e, {'e_slope_bars': 3})[0], jae.TREND_FLAT)

    def test_band_and_eps_are_configurable(self):
        e = pd.Series([53.0] * 6)          # 中線 +3
        self.assertEqual(jae.trend_of(e, {'neutral_band': 5.0})[0], jae.TREND_FLAT)
        self.assertEqual(jae.trend_of(e, {'neutral_band': 2.0})[0], jae.TREND_UP)

    def test_trend_is_safe_on_bad_input(self):
        self.assertEqual(jae.trend_of(None)[0], jae.TREND_FLAT)
        self.assertEqual(jae.trend_of(pd.Series([], dtype=float))[0], jae.TREND_FLAT)

    # ---------- 交叉 ----------

    def test_golden_cross_is_fast_crossing_up(self):
        """黃金交叉的定義:**比較快的那條由下往上穿過比較慢的那條**。
        三組配對都照這個定義,不會有的組別反過來。"""
        fast = pd.Series([10.0, 20.0])
        slow = pd.Series([15.0, 15.0])
        self.assertEqual(jae._cross_at(fast, slow, 1), jae.CROSS_GOLDEN)
        self.assertEqual(jae._cross_at(slow, fast, 1), jae.CROSS_DEATH)

    def test_no_cross_when_lines_do_not_meet(self):
        fast = pd.Series([10.0, 12.0])
        slow = pd.Series([20.0, 21.0])
        self.assertIsNone(jae._cross_at(fast, slow, 1))

    def test_nan_never_counts_as_a_cross(self):
        """暖機期一堆 NaN,不可以被當成交叉 (那會在圖表最左邊噴一串假訊號)。"""
        fast = pd.Series([float('nan'), 20.0])
        slow = pd.Series([15.0, 15.0])
        self.assertIsNone(jae._cross_at(fast, slow, 1))

    def test_cross_pairs_are_ordered_fast_then_slow(self):
        """配對一律 (快, 慢);寫反的話「黃金交叉」的意義就整個顛倒。"""
        self.assertEqual(jae.CROSS_PAIRS[0][:2], (jae.COL_J, jae.COL_A))
        self.assertEqual(jae.CROSS_PAIRS[1][:2], (jae.COL_A, jae.COL_E))
        self.assertEqual(jae.CROSS_PAIRS[2][:2], (jae.COL_J, jae.COL_E))

    def test_crosses_found_over_a_lookback(self):
        df = self._df([100.0 + 10 * ((-1) ** (i // 7)) + i * 0.05 for i in range(200)])
        jae.compute(df)
        found = jae.crosses(df, lookback=80)
        self.assertTrue(found, "震盪的資料應該找得到交叉")
        for c in found:
            self.assertIn(c['kind'], (jae.CROSS_GOLDEN, jae.CROSS_DEATH))
            self.assertIn('×', c['pair'])

    # ---------- 參數 ----------

    def test_params_are_clamped_not_crashed(self):
        p = jae.normalize_params({'a_period': -5, 'e_period': 99999,
                                  'midline': 'abc', 'j_n': None})
        self.assertGreaterEqual(p['a_period'], jae.PARAM_RANGES['a_period'][0])
        self.assertLessEqual(p['e_period'], jae.PARAM_RANGES['e_period'][1])
        self.assertEqual(p['midline'], jae.DEFAULT_PARAMS['midline'])
        self.assertEqual(p['j_n'], jae.DEFAULT_PARAMS['j_n'])

    def test_params_actually_change_the_lines(self):
        """反向對照:參數要真的有作用,不然「可自行設定」是假的。"""
        d1, d2 = self._rising(), self._rising()
        jae.compute(d1, {'a_period': 14, 'e_period': 60})
        jae.compute(d2, {'a_period': 5, 'e_period': 200})
        self.assertFalse(d1[jae.COL_A].equals(d2[jae.COL_A]))
        self.assertFalse(d1[jae.COL_E].equals(d2[jae.COL_E]))

    def test_every_param_has_a_label_and_a_range(self):
        for k in jae.DEFAULT_PARAMS:
            self.assertIn(k, jae.PARAM_LABELS)
            self.assertIn(k, jae.PARAM_RANGES)

    # ---------- 摘要 ----------

    def test_evaluate_and_summarize(self):
        df = self._rising()
        jae.compute(df)
        r = jae.evaluate(df, lookback=5)
        self.assertIsNotNone(r)
        self.assertEqual(r['trend'], jae.TREND_UP)
        txt = jae.summarize(r)
        self.assertIn('趨勢', txt)
        self.assertIn('A ', txt)
        self.assertIn('資料不足', jae.summarize(None))

    def test_evaluate_returns_none_without_columns(self):
        self.assertIsNone(jae.evaluate(self._rising()))
        self.assertIsNone(jae.evaluate(None))



class TestPriceTypeSharedRule(unittest.TestCase):
    """【ADR-135】委託方式的規則抽成 validate_price_type(),內建與自訂策略共用。

    這一份的依據是**交易所規則**(範圍市價僅期貨、零股只能限價/鐵則6),
    分歧的後果是送出去會被券商退的委託 —— 所以只能有一份。
    """

    def _s(self, tt, pt):
        s = strategy_engine.new_strategy()
        s.update({'trade_type': tt, 'price_type': pt})
        return s

    def test_futures_can_use_all_three(self):
        for pt in strategy_engine.PRICE_TYPES:
            ok, why = strategy_engine.validate_price_type(self._s('期貨', pt))
            self.assertTrue(ok, f"期貨 {pt} 應該可以: {why}")

    def test_range_market_is_futures_only(self):
        for tt in ('股票', '零股'):
            ok, why = strategy_engine.validate_price_type(self._s(tt, '範圍市價'))
            self.assertFalse(ok, f"{tt} 不該允許範圍市價")
            self.assertIn('範圍市價', why)

    def test_odd_lot_is_limit_only(self):
        """鐵則6:零股依交易所規則只能限價。"""
        for pt in ('市價', '範圍市價'):
            ok, why = strategy_engine.validate_price_type(self._s('零股', pt))
            self.assertFalse(ok, f"零股不該允許 {pt}")
        ok, _ = strategy_engine.validate_price_type(self._s('零股', '限價'))
        self.assertTrue(ok)

    def test_stock_allows_limit_and_market(self):
        for pt in ('限價', '市價'):
            ok, why = strategy_engine.validate_price_type(self._s('股票', pt))
            self.assertTrue(ok, f"股票 {pt} 應該可以: {why}")

    def test_unknown_price_type_rejected(self):
        ok, why = strategy_engine.validate_price_type(self._s('股票', '亂填'))
        self.assertFalse(ok)

    def test_validate_strategy_still_uses_the_same_rule(self):
        """反向對照:抽出去之後,內建策略的驗證不可以就此失效。"""
        s = self._s('零股', '市價')
        s.update({'name': 'X', 'symbol': '2330', 'qty': 1, 'timeframe': '5分K',
                  'direction': '做多', 'stop_loss_pct': 2.0})
        ok, why = strategy_engine.validate_strategy(s)
        self.assertFalse(ok)
        self.assertIn('零股', why)



class TestIntrabarStopToggle(unittest.TestCase):
    """【ADR-136】停損停利即時觸發改成可勾選的選項,不分商品別。"""

    def _s(self, tt='股票', **over):
        s = strategy_engine.new_strategy()
        s.update({'symbol': '2330', 'trade_type': tt, 'stop_loss_abs': 5.0})
        s.update(over)
        return s

    def _rt(self, entry=600.0, state='LONG'):
        rt = strategy_engine.new_runtime()
        rt.update({'state': state, 'qty': 1, 'entry_price': entry, 'exec_entry_price': entry})
        return rt

    def test_new_strategy_defaults_to_on(self):
        self.assertTrue(strategy_engine.new_strategy()['intrabar_stop'])

    def test_explicit_setting_wins(self):
        self.assertTrue(strategy_engine.intrabar_stop_enabled(self._s(intrabar_stop=True)))
        self.assertFalse(strategy_engine.intrabar_stop_enabled(self._s(intrabar_stop=False)))
        # 不分商品:期貨勾掉也要關得掉
        self.assertFalse(strategy_engine.intrabar_stop_enabled(
            self._s('期貨', intrabar_stop=False)))

    def test_legacy_strategies_keep_their_old_behaviour(self):
        """**沒有這個欄位的舊策略不可以被悄悄改掉行為。**

        期貨從 ADR-087 起本來就是即時;股票/零股本來是K棒收盤才判定。"""
        for tt, want in (('期貨', True), ('股票', False), ('零股', False)):
            s = self._s(tt)
            s.pop('intrabar_stop', None)
            self.assertEqual(strategy_engine.intrabar_stop_enabled(s), want, tt)

    def test_toggle_actually_gates_the_check(self):
        s = self._s('股票', intrabar_stop=True)
        self.assertIsNotNone(strategy_engine.check_intrabar_stop(s, self._rt(), 590.0))
        s['intrabar_stop'] = False
        self.assertIsNone(strategy_engine.check_intrabar_stop(s, self._rt(), 590.0),
                          "沒勾就不可以即時觸發")

    def test_chukuangren_still_excluded(self):
        """反向對照:自己管出場的種類仍然不適用,勾了也一樣 (ADR-123)。"""
        s = self._s('期貨', intrabar_stop=True)
        s['kind'] = chukuangren_band.KIND
        self.assertIsNone(strategy_engine.check_intrabar_stop(s, self._rt(40000.0), 30000.0))


class TestBarStopExtracted(unittest.TestCase):
    """【ADR-136】K棒收盤的停損判斷抽成 check_bar_stop(),讓自訂策略也能共用。"""

    def _s(self, **over):
        s = strategy_engine.new_strategy()
        s.update({'symbol': '2330', 'trade_type': '股票', 'direction': '做多'})
        # 【重要】new_strategy() 預設 stop_loss_pct=2.0 —— 不歸零的話每個案例
        # 都同時有兩個停損在跑,測到的不是自己設的那一個 (第一版就是這樣紅的)。
        for _k in ('stop_loss_pct', 'take_profit_pct', 'stop_loss_abs', 'take_profit_abs'):
            s[_k] = 0.0
        s.update(over)
        return s

    def _rt(self, entry=100.0, state='LONG'):
        rt = strategy_engine.new_runtime()
        rt.update({'state': state, 'qty': 1, 'entry_price': entry, 'exec_entry_price': entry})
        return rt

    def test_pct_and_abs_stops(self):
        s = self._s(stop_loss_pct=2.0)
        self.assertIsNotNone(strategy_engine.check_bar_stop(s, self._rt(), 97.0))
        self.assertIsNone(strategy_engine.check_bar_stop(s, self._rt(), 99.0))
        s2 = self._s(stop_loss_abs=3.0)
        self.assertIsNotNone(strategy_engine.check_bar_stop(s2, self._rt(), 96.0))
        self.assertIsNone(strategy_engine.check_bar_stop(s2, self._rt(), 98.0))

    def test_take_profit(self):
        s = self._s(take_profit_pct=5.0)
        self.assertIsNotNone(strategy_engine.check_bar_stop(s, self._rt(), 106.0))
        self.assertIsNone(strategy_engine.check_bar_stop(s, self._rt(), 104.0))

    def test_short_side_is_mirrored(self):
        s = self._s(stop_loss_abs=3.0, trade_type='期貨', market='台期貨')
        self.assertIsNotNone(strategy_engine.check_bar_stop(s, self._rt(state='SHORT'), 104.0))
        self.assertIsNone(strategy_engine.check_bar_stop(s, self._rt(state='SHORT'), 102.0))

    def test_no_position_no_stop(self):
        s = self._s(stop_loss_abs=1.0)
        self.assertIsNone(strategy_engine.check_bar_stop(s, strategy_engine.new_runtime(), 1.0))

    def test_evaluate_strategy_still_uses_it(self):
        """反向對照:抽出去之後內建策略的K棒停損不可以失效。"""
        s = self._s(stop_loss_pct=2.0, timeframe='日K', qty=1)
        rt = self._rt()
        df = pd.DataFrame({'Open': [100.0] * 30, 'High': [101.0] * 30,
                           'Low': [96.0] * 30, 'Close': [100.0] * 29 + [97.0],
                           'Volume': [1000] * 30},
                          index=pd.date_range('2026-01-01', periods=30, freq='D'))
        intents = strategy_engine.evaluate_strategy(s, rt, df, 1.0, '2026-01-30')
        self.assertTrue(any(it['kind'] == 'CLOSE' for it in intents),
                        "內建策略的K棒收盤停損不可以因為重構而失效")


class TestStopLevelsAndTouch(unittest.TestCase):
    """【ADR-136】把停損停利換算成價位,給回測判斷「盤中有沒有觸價」。"""

    def _s(self, **over):
        s = strategy_engine.new_strategy()
        s.update({'symbol': '2330', 'trade_type': '股票'})
        for _k in ('stop_loss_pct', 'take_profit_pct', 'stop_loss_abs', 'take_profit_abs'):
            s[_k] = 0.0          # 見 TestBarStopExtracted._s 的說明
        s.update(over)
        return s

    def _rt(self, entry=100.0, state='LONG'):
        rt = strategy_engine.new_runtime()
        rt.update({'state': state, 'qty': 1, 'entry_price': entry})
        return rt

    def test_levels_for_long(self):
        stop, take = strategy_engine.stop_levels(
            self._s(stop_loss_abs=5.0, take_profit_abs=8.0), self._rt())
        self.assertAlmostEqual(stop, 95.0)
        self.assertAlmostEqual(take, 108.0)

    def test_levels_for_short_are_mirrored(self):
        stop, take = strategy_engine.stop_levels(
            self._s(stop_loss_abs=5.0, take_profit_abs=8.0), self._rt(state='SHORT'))
        self.assertAlmostEqual(stop, 105.0)
        self.assertAlmostEqual(take, 92.0)

    def test_nearest_level_wins_when_both_pct_and_abs_set(self):
        """同時設了% 與點數 → 取**先會被碰到**的那一個 (離進場價最近)。"""
        stop, _ = strategy_engine.stop_levels(
            self._s(stop_loss_pct=10.0, stop_loss_abs=3.0), self._rt())
        self.assertAlmostEqual(stop, 97.0, msg="3元比10%(=10元)先到")

    def test_touch_uses_high_low(self):
        s = self._s(stop_loss_abs=5.0)
        # 收盤沒破,但盤中最低點破了 → 即時模型要觸發
        hit = strategy_engine.bar_touch_exit(s, self._rt(), 99.0, 100.0, 94.0)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit[0], 95.0)
        self.assertIn('停損', hit[1])
        self.assertIsNone(strategy_engine.bar_touch_exit(s, self._rt(), 99.0, 100.0, 96.0))

    def test_gap_fills_at_open_not_at_the_stop(self):
        """跳空時成交在開盤價 (比停損價更差) —— 不做樂觀假設。"""
        s = self._s(stop_loss_abs=5.0)
        hit = strategy_engine.bar_touch_exit(s, self._rt(), 90.0, 91.0, 89.0)
        self.assertAlmostEqual(hit[0], 90.0, msg="跳空開低,只能成交在開盤價")

    def test_stop_wins_when_both_touched_in_one_bar(self):
        """同一根同時碰到停損與停利 → 算停損。分不出誰先到,取對使用者不利的
        那一邊才不會高估績效。"""
        s = self._s(stop_loss_abs=5.0, take_profit_abs=5.0)
        hit = strategy_engine.bar_touch_exit(s, self._rt(), 100.0, 106.0, 94.0)
        self.assertIn('停損', hit[1])

    def test_no_levels_no_touch(self):
        self.assertIsNone(strategy_engine.bar_touch_exit(self._s(), self._rt(), 1, 2, 0))


class TestCustomStrategyStopsInBacktest(unittest.TestCase):
    """【ADR-136】自訂策略的停損停利原本在回測裡**完全沒有被套用**。

    使用者:「自訂策略,程式不要寫的太複雜,只要專注在買賣的條件行為就好。
    其他的就交給系統…並且可以有效執行回測(這一點非常重要)。」
    """

    # on_bar 要回傳 ctx.buy()/ctx.sell()/ctx.close_position()/None,
    # 而且部位狀態讀的是 **ctx.position**(ctx.state 是使用者自己的暫存 dict)。
    # 第一版寫成回傳字串 'BUY' 且讀 ctx.state,結果一筆單都沒開 —— 是測試
    # 的假策略寫錯,不是回測錯。
    SRC = ("def on_bar(ctx):\n"
           "    if ctx.position == 'FLAT':\n"
           "        return ctx.buy()\n"
           "    return None\n")

    def _df(self, closes, lows=None, highs=None):
        n = len(closes)
        lows = lows or [c - 0.5 for c in closes]
        highs = highs or [c + 0.5 for c in closes]
        return pd.DataFrame({'Open': closes, 'High': highs, 'Low': lows,
                             'Close': closes, 'Volume': [1000] * n},
                            index=pd.date_range('2026-01-01', periods=n, freq='D'))

    def _strat(self, **over):
        s = strategy_engine.new_strategy()
        s.update({'kind': 'custom', 'source_code': self.SRC, 'name': 'C',
                  'symbol': '2330', 'trade_type': '股票', 'market': '台股',
                  'qty': 1, 'timeframe': '日K', 'custom_params': {},
                  'intrabar_stop': False})
        for _k in ('stop_loss_pct', 'take_profit_pct', 'stop_loss_abs', 'take_profit_abs'):
            s[_k] = 0.0          # 自訂策略預設「完全交給程式碼」(ADR-045)
        s.update(over)
        return s

    def test_custom_strategy_stop_now_fires_in_backtest(self):
        """進場後一路下跌:設了停損就必須出場。修正前這裡是 0 筆平倉。"""
        df = self._df([100.0] * 3 + [100.0 - i * 2 for i in range(1, 12)])
        r = backtest.run_backtest(self._strat(stop_loss_abs=5.0), df,
                                  apply_cost_model=False, settle_open_at_end=False)
        self.assertTrue(r['trades'], "自訂策略設了停損,回測必須有平倉紀錄")
        self.assertIn('停損', r['trades'][0]['exit_reason'])

    def test_without_stop_it_still_holds(self):
        """反向對照:沒設停損就不該平倉 —— 否則等於系統亂砍使用者的部位。"""
        df = self._df([100.0] * 3 + [100.0 - i * 2 for i in range(1, 12)])
        r = backtest.run_backtest(self._strat(), df, apply_cost_model=False,
                                  settle_open_at_end=False)
        self.assertFalse(r['trades'], "沒設停損就不該有平倉")

    def test_intrabar_toggle_changes_the_backtest(self):
        """勾了即時觸發,回測要用當根**高低點**判斷觸價 —— 只摸到影子也算。

        這一組資料收盤永遠不破停損,只有盤中最低點破 → 不勾:不出場;
        勾了:出場。兩者結果不同,才代表回測真的照設定在模擬。"""
        closes = [100.0] * 3 + [99.0] * 10
        lows = [99.5] * 3 + [90.0] * 10          # 盤中破,收盤沒破
        df = self._df(closes, lows=lows)
        off = backtest.run_backtest(self._strat(stop_loss_abs=5.0, intrabar_stop=False),
                                    df, apply_cost_model=False, settle_open_at_end=False)
        on = backtest.run_backtest(self._strat(stop_loss_abs=5.0, intrabar_stop=True),
                                   df, apply_cost_model=False, settle_open_at_end=False)
        self.assertFalse(off['trades'], "沒勾即時:收盤沒破就不出場")
        self.assertTrue(on['trades'], "勾了即時:盤中觸價就要出場")
        self.assertIn('盤中觸及', on['trades'][0]['exit_reason'])



class TestTelegramQueryCommands(unittest.TestCase):
    """【ADR-137】手機問「我要看目前 XX 帳號的損益」——雙向查詢。

    ADR-108 已經有雙向的骨架 (getUpdates 輪詢 + 白名單 + /status /pnl …),
    這一輪補的是「**指定對象**」:帳戶要能挑、策略要能看細節。
    """

    def _acct(self, aid, name, realized=0.0, with_pos=False):
        """帳戶一律用 paper_account 的正式建構子與 apply_fill 產生。

        第一版自己拼 dict:先是參數順序傳錯,改好之後又因為手拼的持倉少了
        `market`/`share_per_unit` 等欄位,`equity()` 直接 KeyError。
        **測試資料的形狀要由產生它的正式程式碼決定**,不要自己捏。
        """
        a = paper_account.new_account(name=name, account_id=aid)
        a['realized_pnl'] = realized
        if with_pos:
            paper_account.apply_fill(a, '2026-07-31', '台股', '2330',
                                     '買進', 'OPEN', 1, 600.0, trade_type='股票')
        return a

    def _accts(self):
        return {'a1': self._acct('a1', '主帳戶', 1000.0),
                'a2': self._acct('a2', '測試帳戶', -500.0, with_pos=True)}

    # ---------- 帳戶比對 ----------

    def test_match_by_index_and_name(self):
        accts = self._accts()
        a, why = telegram_control.match_account(accts, '2')
        self.assertIsNotNone(a, why)
        self.assertEqual(a['name'], '測試帳戶')
        a2, _ = telegram_control.match_account(accts, '主帳戶')
        self.assertEqual(a2['id'], 'a1')

    def test_partial_name_works(self):
        a, why = telegram_control.match_account(self._accts(), '測試')
        self.assertIsNotNone(a, why)
        self.assertEqual(a['id'], 'a2')

    def test_ambiguous_name_is_rejected(self):
        """多個符合時要求講清楚 —— 回錯帳戶的損益會直接誤導決策。"""
        accts = {'a1': self._acct('a1', '測試A'), 'a2': self._acct('a2', '測試B')}
        a, why = telegram_control.match_account(accts, '測試')
        self.assertIsNone(a)
        self.assertIn('請用編號', why)

    def test_unknown_and_empty(self):
        self.assertIsNone(telegram_control.match_account(self._accts(), '不存在')[0])
        self.assertIsNone(telegram_control.match_account(self._accts(), '')[0])
        self.assertIsNone(telegram_control.match_account({}, '1')[0])
        self.assertIsNone(telegram_control.match_account(self._accts(), '99')[0])

    def test_index_order_is_stable(self):
        """編號要穩定 —— 每次問同一個編號必須是同一個帳戶,否則遠端指令會指錯。"""
        accts = self._accts()
        first = [telegram_control.match_account(accts, '1')[0]['id'] for _ in range(5)]
        self.assertEqual(set(first), {first[0]})

    # ---------- 只看一個帳戶 ----------

    def test_pnl_filters_to_one_account(self):
        accts = self._accts()
        acct, _ = telegram_control.match_account(accts, '測試帳戶')
        one = telegram_control.pnl_text(accts, '2026-07-31', only=acct)
        self.assertIn('測試帳戶', one)
        self.assertNotIn('主帳戶', one)
        # 反向對照:不帶 only 要列出全部 (原本的行為不可以被弄壞)
        allt = telegram_control.pnl_text(accts, '2026-07-31')
        self.assertIn('主帳戶', allt)
        self.assertIn('測試帳戶', allt)

    def test_positions_filters_to_one_account(self):
        accts = self._accts()
        acct, _ = telegram_control.match_account(accts, '1')      # 主帳戶,無持倉
        self.assertIn('無持倉', telegram_control.positions_text(accts, only=acct))
        acct2, _ = telegram_control.match_account(accts, '2')
        self.assertIn('2330', telegram_control.positions_text(accts, only=acct2))

    def test_accounts_text_lists_numbers(self):
        txt = telegram_control.accounts_text(self._accts())
        self.assertIn('1.', txt)
        self.assertIn('主帳戶', txt)
        self.assertIn('/pnl', txt, '要告訴使用者查詢時可以怎麼打')
        self.assertIn('沒有任何模擬帳戶', telegram_control.accounts_text({}))

    # ---------- 策略詳情 ----------

    def test_strategy_detail_has_settings_and_state(self):
        """設定決定它會做什麼、狀態決定它現在在做什麼 —— 少一半都要再問一次。"""
        s = strategy_engine.new_strategy()
        s.update({'name': '測試策略', 'symbol': '2330', 'trade_type': '股票',
                  'timeframe': '15分K', 'qty': 2, 'enabled': True, 'mode': '模擬',
                  'price_type': '市價', 'stop_loss_pct': 2.0, 'intrabar_stop': True})
        rt = strategy_engine.new_runtime()
        rt.update({'state': 'LONG', 'qty': 2, 'exec_entry_price': 600.0})
        txt = telegram_control.strategy_detail_text(s, rt)
        for need in ('測試策略', '2330', '15分K', '市價', '停損 2.0%', 'LONG', '即時觸發'):
            self.assertIn(need, txt, f'策略詳情缺少「{need}」')

    def test_strategy_detail_is_safe_without_runtime(self):
        s = strategy_engine.new_strategy()
        s['name'] = 'X'
        self.assertIn('無', telegram_control.strategy_detail_text(s, None))
        self.assertIn('找不到', telegram_control.strategy_detail_text(None))

    # ---------- 指令表 ----------

    def test_new_commands_are_registered_and_readonly(self):
        """新增的查詢指令一律 readonly —— 查詢絕不可以變成會動錢的指令。"""
        for c in ('/accounts', '/strategy'):
            self.assertIn(c, telegram_control.COMMANDS)
            self.assertEqual(telegram_control.command_kind(c),
                             telegram_control.CMD_READONLY, c)
            self.assertFalse(telegram_control.needs_confirm(c))
        self.assertIn('/accounts', telegram_control.help_text())

    def test_parse_command_accepts_args_for_queries(self):
        self.assertEqual(telegram_control.parse_command('/pnl 主帳戶'), ('/pnl', '主帳戶'))
        self.assertEqual(telegram_control.parse_command('/strategy 2'), ('/strategy', '2'))
        # 未授權/非指令仍然不理會
        self.assertEqual(telegram_control.parse_command('我要看損益')[0], None)

    def test_still_no_order_command(self):
        """反向對照:雙向查詢擴充之後,**仍然不可以有下單指令**
        (ADR-108 的安全設計:遠端只能控制策略要不要跑,不能直接動錢)。"""
        for bad in ('/buy', '/sell', '/order', '/close'):
            self.assertNotIn(bad, telegram_control.COMMANDS)



if __name__ == "__main__":
    unittest.main(verbosity=2)
