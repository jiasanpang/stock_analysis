# -*- coding: utf-8 -*-
"""Tests for weekly review module (src/core/weekly_review.py)."""

import unittest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from src._review_v2_types import (
    WeeklyIndexPerformance,
    WeeklyReviewData,
    WeeklySectorEntry,
)


class TestWeeklyReviewDataTypes(unittest.TestCase):
    """Test WeeklyReviewData dataclass."""

    def test_default_values(self):
        data = WeeklyReviewData()
        self.assertEqual(data.week_start, "")
        self.assertFalse(data.data_available)
        self.assertEqual(data.weekly_pnl_pct, 0.0)
        self.assertEqual(data.win_rate, 0.0)

    def test_full_construction(self):
        data = WeeklyReviewData(
            week_start="2025-01-06",
            week_end="2025-01-10",
            weekly_indices=[
                WeeklyIndexPerformance(name="上证指数", weekly_change_pct=1.5),
                WeeklyIndexPerformance(name="创业板指", weekly_change_pct=-0.8),
            ],
            sector_rotation=[
                WeeklySectorEntry(name="半导体", weekly_change_pct=5.0, trend="领涨"),
            ],
            strongest_mainline="半导体",
            weekly_pnl_pct=2.3,
            win_rate=66.7,
            max_drawdown=-3.5,
            data_available=True,
        )
        self.assertEqual(data.strongest_mainline, "半导体")
        self.assertAlmostEqual(data.win_rate, 66.7)


class TestComputeHoldingsStats(unittest.TestCase):
    """Test _compute_holdings_stats with mocked manager."""

    def test_empty_stock_list(self):
        from src.core.weekly_review import _compute_holdings_stats
        mock_manager = MagicMock()
        pnl, wr, dd, details, weakened, core = _compute_holdings_stats(
            mock_manager, [], date(2025, 1, 6), date(2025, 1, 10),
        )
        self.assertEqual(pnl, 0.0)
        self.assertEqual(wr, 0.0)
        self.assertEqual(details, [])

    def test_with_stock_data(self):
        import pandas as pd
        from src.core.weekly_review import _compute_holdings_stats

        mock_manager = MagicMock()
        df = pd.DataFrame({
            "date": ["2025-01-06", "2025-01-10"],
            "close": [100.0, 110.0],
            "high": [105.0, 112.0],
            "low": [98.0, 108.0],
        })
        mock_manager.get_daily_data.return_value = (df, "test")

        pnl, wr, dd, details, weakened, core = _compute_holdings_stats(
            mock_manager, ["600519"], date(2025, 1, 6), date(2025, 1, 10),
        )
        self.assertAlmostEqual(pnl, 10.0, places=1)
        self.assertAlmostEqual(wr, 100.0)
        self.assertEqual(len(details), 1)
        self.assertEqual(len(core), 1)
        self.assertEqual(len(weakened), 0)


class TestGenerateTemplateReport(unittest.TestCase):
    """Test _generate_template_weekly_report."""

    def test_template_with_data(self):
        from src.core.weekly_review import _generate_template_weekly_report

        data = WeeklyReviewData(
            week_start="2025-01-06",
            week_end="2025-01-10",
            weekly_indices=[
                WeeklyIndexPerformance(name="上证指数", weekly_change_pct=1.5),
            ],
            sector_rotation=[
                WeeklySectorEntry(name="半导体", weekly_change_pct=5.0, trend="领涨"),
            ],
            strongest_mainline="半导体",
            holdings_detail=[
                {"name": "贵州茅台", "code": "600519", "weekly_change": 3.0},
            ],
            weekly_pnl_pct=2.5,
            win_rate=75.0,
            max_drawdown=-2.0,
            weakened_holdings=["弱势股(000001)"],
            core_positions=["贵州茅台(600519)"],
            data_available=True,
        )

        report = _generate_template_weekly_report(data)
        self.assertIn("周度复盘", report)
        self.assertIn("上证指数", report)
        self.assertIn("半导体", report)
        self.assertIn("贵州茅台", report)
        self.assertIn("2.50%", report)
        self.assertIn("75%", report)

    def test_template_no_holdings(self):
        from src.core.weekly_review import _generate_template_weekly_report

        data = WeeklyReviewData(
            week_start="2025-01-06",
            week_end="2025-01-10",
            data_available=True,
        )

        report = _generate_template_weekly_report(data)
        self.assertIn("未配置持仓", report)
        self.assertIn("暂无数据", report)


class TestBuildWeeklyPrompt(unittest.TestCase):
    """Test _build_weekly_prompt for LLM."""

    def test_prompt_contains_data(self):
        from src.core.weekly_review import _build_weekly_prompt

        data = WeeklyReviewData(
            week_start="2025-01-06",
            week_end="2025-01-10",
            weekly_indices=[
                WeeklyIndexPerformance(name="上证", weekly_change_pct=1.0),
            ],
            sector_rotation=[
                WeeklySectorEntry(name="半导体", weekly_change_pct=3.0, trend="领涨"),
            ],
            strongest_mainline="半导体",
            holdings_detail=[
                {"name": "A", "code": "001", "weekly_change": 2.0},
            ],
            weekly_pnl_pct=1.5,
            win_rate=80.0,
            max_drawdown=-1.0,
            data_available=True,
        )

        prompt = _build_weekly_prompt(data)
        self.assertIn("2025-01-06", prompt)
        self.assertIn("半导体", prompt)
        self.assertIn("周度复盘", prompt)


if __name__ == "__main__":
    unittest.main()
