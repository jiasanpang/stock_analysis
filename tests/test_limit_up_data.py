# -*- coding: utf-8 -*-
"""Tests for limit-up data fetchers (data_provider/akshare/limit_up.py)."""

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd

from src._review_v2_types import LimitUpLadder, LimitUpStock, NorthBoundFlow


class TestLimitUpPool(unittest.TestCase):
    """Test get_limit_up_pool with mocked akshare."""

    @patch("data_provider.akshare.limit_up._safe_akshare_call")
    def test_returns_stocks_on_valid_df(self, mock_call):
        from data_provider.akshare.limit_up import get_limit_up_pool

        mock_call.return_value = pd.DataFrame([
            {"代码": "600519", "名称": "贵州茅台", "涨跌幅": 10.0, "最新价": 1800.0,
             "成交额": 1e9, "首次封板时间": "09:30", "最后封板时间": "14:50",
             "炸板次数": 0, "连板数": 3, "封板资金": 5e8, "所属行业": "白酒", "换手率": 1.2},
            {"代码": "000001", "名称": "平安银行", "涨跌幅": 10.0, "最新价": 15.0,
             "成交额": 5e8, "首次封板时间": "10:00", "最后封板时间": "14:30",
             "炸板次数": 2, "连板数": 1, "封板资金": 1e8, "所属行业": "银行", "换手率": 3.5},
        ])

        result = get_limit_up_pool("20250101")
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], LimitUpStock)
        self.assertEqual(result[0].code, "600519")
        self.assertEqual(result[0].streak_count, 3)
        self.assertEqual(result[0].name, "贵州茅台")

    @patch("data_provider.akshare.limit_up._safe_akshare_call")
    def test_returns_empty_on_none(self, mock_call):
        from data_provider.akshare.limit_up import get_limit_up_pool
        mock_call.return_value = None
        result = get_limit_up_pool("20250101")
        self.assertEqual(result, [])


class TestBrokenBoardPool(unittest.TestCase):
    """Test get_broken_board_pool."""

    @patch("data_provider.akshare.limit_up._safe_akshare_call")
    def test_returns_count(self, mock_call):
        from data_provider.akshare.limit_up import get_broken_board_pool
        mock_call.return_value = pd.DataFrame([{"代码": "001"}, {"代码": "002"}])
        self.assertEqual(get_broken_board_pool("20250101"), 2)

    @patch("data_provider.akshare.limit_up._safe_akshare_call")
    def test_returns_zero_on_failure(self, mock_call):
        from data_provider.akshare.limit_up import get_broken_board_pool
        mock_call.return_value = None
        self.assertEqual(get_broken_board_pool("20250101"), 0)


class TestBuildLimitUpLadder(unittest.TestCase):
    """Test build_limit_up_ladder aggregation."""

    @patch("data_provider.akshare.limit_up.get_limit_down_pool", return_value=5)
    @patch("data_provider.akshare.limit_up.get_broken_board_pool", return_value=10)
    @patch("data_provider.akshare.limit_up.get_limit_up_pool")
    def test_ladder_construction(self, mock_pool, mock_broken, mock_dtgc):
        from data_provider.akshare.limit_up import build_limit_up_ladder

        mock_pool.return_value = [
            LimitUpStock(code="001", name="A", streak_count=3, broken_count=0),
            LimitUpStock(code="002", name="B", streak_count=3, broken_count=1),
            LimitUpStock(code="003", name="C", streak_count=1, broken_count=0),
        ]

        ladder = build_limit_up_ladder("20250101")
        self.assertTrue(ladder.data_available)
        self.assertEqual(ladder.total_limit_up, 3)
        self.assertEqual(ladder.total_broken, 10)
        self.assertEqual(ladder.max_streak, 3)
        self.assertIn(3, ladder.ladder)
        self.assertIn(1, ladder.ladder)
        self.assertEqual(len(ladder.ladder[3]), 2)

        # 炸板率 = 10 / (3 + 10) * 100 ≈ 76.9%
        self.assertAlmostEqual(ladder.broken_rate, 76.92, places=1)

    @patch("data_provider.akshare.limit_up.get_limit_down_pool", return_value=0)
    @patch("data_provider.akshare.limit_up.get_broken_board_pool", return_value=0)
    @patch("data_provider.akshare.limit_up.get_limit_up_pool", return_value=[])
    def test_empty_ladder(self, mock_pool, mock_broken, mock_dtgc):
        from data_provider.akshare.limit_up import build_limit_up_ladder
        ladder = build_limit_up_ladder("20250101")
        self.assertFalse(ladder.data_available)


class TestNorthFlow(unittest.TestCase):
    """Test get_north_flow."""

    @patch("data_provider.akshare.limit_up._safe_akshare_call")
    def test_parse_north_flow(self, mock_call):
        from data_provider.akshare.limit_up import get_north_flow
        mock_call.return_value = pd.DataFrame([
            {"沪股通-净买额": 50000, "深股通-净买额": 30000},
        ])
        flow = get_north_flow()
        self.assertIsInstance(flow, NorthBoundFlow)
        self.assertTrue(flow.data_available)
        self.assertAlmostEqual(flow.total_net, 8.0, places=1)  # 50000+30000 / 1e4 * 2... wait

    @patch("data_provider.akshare.limit_up._safe_akshare_call")
    def test_north_flow_unavailable(self, mock_call):
        from data_provider.akshare.limit_up import get_north_flow
        mock_call.return_value = None
        flow = get_north_flow()
        self.assertFalse(flow.data_available)


class TestDetectLossEffect(unittest.TestCase):
    """Test _detect_loss_effect logic."""

    def test_extreme_loss(self):
        from data_provider.akshare.limit_up import _detect_loss_effect
        result = _detect_loss_effect(10, 5, 20, 3)
        self.assertIn("极端亏钱", result)

    def test_high_broken_rate_with_dtgc(self):
        from data_provider.akshare.limit_up import _detect_loss_effect
        result = _detect_loss_effect(20, 20, 15, 5)
        self.assertIn("接力风险", result)

    def test_healthy_structure(self):
        from data_provider.akshare.limit_up import _detect_loss_effect
        result = _detect_loss_effect(50, 5, 2, 5)
        self.assertIn("健康", result)


class TestLimitUpLadderSummary(unittest.TestCase):
    """Test LimitUpLadder.get_streak_summary."""

    def test_summary_with_data(self):
        ladder = LimitUpLadder(
            data_available=True,
            ladder={
                3: [LimitUpStock(name="A"), LimitUpStock(name="B")],
                1: [LimitUpStock(name="C")],
            },
        )
        summary = ladder.get_streak_summary()
        self.assertIn("3连板", summary)
        self.assertIn("1连板", summary)

    def test_summary_unavailable(self):
        ladder = LimitUpLadder(data_available=False)
        self.assertIn("暂不可用", ladder.get_streak_summary())


if __name__ == "__main__":
    unittest.main()
