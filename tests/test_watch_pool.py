# -*- coding: utf-8 -*-
"""Tests for watch pool module (src/core/watch_pool.py)."""

import unittest
from unittest.mock import patch, MagicMock

from src._review_v2_types import WatchPoolCandidate


class TestScreenWatchPoolEmpty(unittest.TestCase):
    """Test screen_watch_pool with no data."""

    @patch("src.core.watch_pool._lup_watch_pool", return_value=[])
    def test_none_overview(self, _lup):
        from src.core.watch_pool import screen_watch_pool
        result = screen_watch_pool(None)
        self.assertEqual(result, [])

    @patch("src.core.watch_pool._lup_watch_pool", return_value=[])
    def test_no_top_sectors(self, _lup):
        from src.core.watch_pool import screen_watch_pool
        overview = MagicMock()
        overview.top_sectors = []
        result = screen_watch_pool(overview)
        self.assertEqual(result, [])


class TestScreenWatchPoolFilter(unittest.TestCase):
    """Test filtering logic in watch pool screening."""

    @patch("src.core.watch_pool._lup_watch_pool", return_value=[])
    @patch("src.core.watch_pool._get_sector_stocks")
    def test_sectors_below_threshold_skipped(self, mock_get, _lup):
        from src.core.watch_pool import screen_watch_pool
        overview = MagicMock()
        overview.top_sectors = [
            {"name": "半导体", "change_pct": 0.5},  # < 1%, should be skipped
        ]
        result = screen_watch_pool(overview)
        self.assertEqual(result, [])
        mock_get.assert_not_called()

    @patch("src.core.watch_pool._lup_watch_pool", return_value=[])
    @patch("src.core.watch_pool._get_sector_stocks")
    def test_filters_by_change_and_turnover(self, mock_get, _lup):
        from src.core.watch_pool import screen_watch_pool

        mock_get.return_value = [
            {"code": "001", "name": "A", "change_pct": 3.0, "price": 20.0, "turnover_rate": 5.0},
            {"code": "002", "name": "B", "change_pct": 0.5, "price": 10.0, "turnover_rate": 5.0},  # too low change
            {"code": "003", "name": "C", "change_pct": 9.5, "price": 30.0, "turnover_rate": 5.0},  # too high change
            {"code": "004", "name": "D", "change_pct": 4.0, "price": 15.0, "turnover_rate": 1.0},  # too low turnover
        ]

        overview = MagicMock()
        overview.top_sectors = [
            {"name": "半导体", "change_pct": 2.5},
        ]
        result = screen_watch_pool(overview)
        # Only stock "001" should pass: change 3% (1-8%), turnover 5% (2-15%)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].code, "001")


class TestFilterCandidates(unittest.TestCase):
    """Test _filter_candidates directly."""

    def test_stop_loss_and_ideal_buy(self):
        from src.core.watch_pool import _filter_candidates

        stocks = [
            {"code": "600519", "name": "茅台", "change_pct": 4.0, "price": 100.0, "turnover_rate": 5.0},
        ]
        result = _filter_candidates(stocks, "白酒", 2.0)
        self.assertEqual(len(result), 1)
        c = result[0]
        self.assertAlmostEqual(c.stop_loss, 95.0)  # 5% stop loss
        self.assertAlmostEqual(c.ideal_buy_price, 98.0)  # 2% pullback
        self.assertTrue(c.risk_reward > 0)

    def test_max_pool_limit(self):
        from src.core.watch_pool import _filter_candidates

        stocks = [
            {"code": f"00{i}", "name": f"S{i}", "change_pct": 3.0 + i * 0.1,
             "price": 10.0 + i, "turnover_rate": 5.0}
            for i in range(10)
        ]
        result = _filter_candidates(stocks, "板块", 2.0)
        self.assertLessEqual(len(result), 5)  # _MAX_WATCH_POOL = 5


class TestWatchPoolCandidateDataclass(unittest.TestCase):
    """Test WatchPoolCandidate dataclass."""

    def test_default_values(self):
        c = WatchPoolCandidate()
        self.assertEqual(c.code, "")
        self.assertFalse(c.data_available)
        self.assertEqual(c.stop_loss, 0.0)

    def test_full_construction(self):
        c = WatchPoolCandidate(
            code="600519", name="贵州茅台", sector="白酒",
            change_pct=3.5, reason="主线板块", buy_condition="回踩98附近",
            stop_loss=95.0, ideal_buy_price=98.0, risk_reward=2.0,
            data_available=True,
        )
        self.assertEqual(c.name, "贵州茅台")
        self.assertTrue(c.data_available)


class TestLupWatchPool(unittest.TestCase):
    """涨停回踩 observation pool integration in screen_watch_pool."""

    def test_lup_watch_pool_disabled_by_env(self):
        with patch.dict("os.environ", {"LUP_ENABLED": "0"}):
            from src.core.watch_pool import _lup_watch_pool
            self.assertEqual(_lup_watch_pool(), [])

    def test_lup_watch_pool_swallows_errors(self):
        with patch("src.services.picker.constants.create_screener_from_config",
                   side_effect=RuntimeError("no tushare")):
            from src.core.watch_pool import _lup_watch_pool
            self.assertEqual(_lup_watch_pool(), [])

    def test_lup_watch_pool_maps_candidates(self):
        events = [{
            "code": "600123", "name": "测试股", "limit_up_date": "20260910",
            "days_waiting": 2, "close": 11.0, "break_level": 10.2,
            "ma5": 10.8, "ma10": 10.5,
        }]
        screener = MagicMock()
        screener.limit_up_watch_candidates.return_value = events
        with patch("src.services.picker.constants.create_screener_from_config",
                   return_value=screener):
            from src.core.watch_pool import _lup_watch_pool
            result = _lup_watch_pool()
        self.assertEqual(len(result), 1)
        c = result[0]
        self.assertEqual(c.code, "600123")
        self.assertAlmostEqual(c.stop_loss, 10.2)      # 涨停开盘价 = 观察失效线
        self.assertAlmostEqual(c.ideal_buy_price, 10.8)  # MA5 回踩区
        self.assertIn("涨停回踩观察池", c.reason)
        self.assertTrue(c.data_available)

    @patch("src.core.watch_pool._get_sector_stocks")
    def test_lup_pool_takes_priority(self, mock_get):
        lup = [WatchPoolCandidate(code="600123", name="测试股",
                                  data_available=True)]
        with patch("src.core.watch_pool._lup_watch_pool", return_value=lup):
            from src.core.watch_pool import screen_watch_pool
            overview = MagicMock()
            overview.top_sectors = [{"name": "半导体", "change_pct": 3.0}]
            result = screen_watch_pool(overview)
        self.assertEqual([c.code for c in result], ["600123"])
        mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
