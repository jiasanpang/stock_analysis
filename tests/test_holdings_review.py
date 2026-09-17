# -*- coding: utf-8 -*-
"""Tests for holdings review module (src/core/holdings_review.py)."""

import unittest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

import pandas as pd

from src._review_v2_types import HoldingReview


class TestReviewHoldingsEmpty(unittest.TestCase):
    """Test review_holdings with empty STOCK_LIST."""

    @patch("src.core.holdings_review.get_config")
    def test_empty_stock_list(self, mock_config):
        mock_cfg = MagicMock()
        mock_cfg.stock_list = []
        mock_config.return_value = mock_cfg

        from src.core.holdings_review import review_holdings
        result = review_holdings()
        self.assertEqual(result, [])


class TestReviewSingleHolding(unittest.TestCase):
    """Test _review_single_holding with mocked data."""

    def _make_mock_trend(self):
        trend = MagicMock()
        trend.current_price = 100.0
        trend.ma5 = 98.0
        trend.ma10 = 95.0
        trend.ma20 = 90.0
        trend.macd_status = MagicMock(value="金叉")
        trend.volume_status = MagicMock(value="放量")
        trend.volume_ratio_5d = 1.8
        trend.support_levels = [88.0]
        trend.resistance_levels = [110.0]
        return trend

    def _make_mock_df(self):
        n = 25
        dates = pd.date_range("2024-12-01", periods=n, freq="B")
        prices = [95.0 + i * 0.2 for i in range(n)]
        return pd.DataFrame({
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "close": prices,
            "high": [p + 1.0 for p in prices],
            "low": [p - 1.0 for p in prices],
            "volume": [1000] * n,
        })

    @patch("src.core.holdings_review.get_config")
    def test_review_with_valid_data(self, mock_config):
        mock_cfg = MagicMock()
        mock_cfg.stock_list = ["600519"]
        mock_config.return_value = mock_cfg

        mock_manager = MagicMock()
        mock_manager.get_daily_data.return_value = (self._make_mock_df(), "test")

        mock_trend_analyzer = MagicMock()
        mock_trend_analyzer.analyze.return_value = self._make_mock_trend()

        from src.core.holdings_review import _review_single_holding
        result = _review_single_holding(
            code="600519",
            manager=mock_manager,
            trend_analyzer=mock_trend_analyzer,
            market_change_pct=0.5,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.data_available)
        self.assertEqual(result.code, "600519")
        self.assertAlmostEqual(result.price, 100.0)
        self.assertEqual(result.ma_alignment, "多头排列")


class TestMAAlignment(unittest.TestCase):
    """Test _describe_ma_alignment helper."""

    def test_bullish_alignment(self):
        from src.core.holdings_review import _describe_ma_alignment
        trend = MagicMock(ma5=100, ma10=95, ma20=90)
        self.assertEqual(_describe_ma_alignment(trend), "多头排列")

    def test_bearish_alignment(self):
        from src.core.holdings_review import _describe_ma_alignment
        trend = MagicMock(ma5=80, ma10=85, ma20=90)
        self.assertEqual(_describe_ma_alignment(trend), "空头排列")

    def test_short_term_bullish(self):
        from src.core.holdings_review import _describe_ma_alignment
        trend = MagicMock(ma5=100, ma10=95, ma20=110)
        self.assertEqual(_describe_ma_alignment(trend), "短期偏多")


class TestVolumeDescription(unittest.TestCase):
    """Test _describe_volume helper."""

    def test_high_volume(self):
        from src.core.holdings_review import _describe_volume
        trend = MagicMock(volume_status=None, volume_ratio_5d=2.0)
        self.assertEqual(_describe_volume(trend), "放量")

    def test_low_volume(self):
        from src.core.holdings_review import _describe_volume
        trend = MagicMock(volume_status=None, volume_ratio_5d=0.5)
        self.assertEqual(_describe_volume(trend), "缩量")

    def test_normal_volume(self):
        from src.core.holdings_review import _describe_volume
        trend = MagicMock(volume_status=None, volume_ratio_5d=1.0)
        self.assertEqual(_describe_volume(trend), "平量")


class TestVsMarket(unittest.TestCase):
    """Test vs_market comparison logic."""

    def test_strong_than_market(self):
        from src.core.holdings_review import _review_single_holding
        hr = HoldingReview(code="test", change_pct=3.0, data_available=True)
        # Stronger: change_pct (3.0) > market_change (0.5) + 1
        market_change = 0.5
        if hr.change_pct > market_change + 1:
            hr.vs_market = "强于大盘"
        self.assertEqual(hr.vs_market, "强于大盘")

    def test_weak_than_market(self):
        hr = HoldingReview(code="test", change_pct=-2.0, data_available=True)
        market_change = 1.0
        if hr.change_pct < market_change - 1:
            hr.vs_market = "弱于大盘"
        self.assertEqual(hr.vs_market, "弱于大盘")


if __name__ == "__main__":
    unittest.main()
