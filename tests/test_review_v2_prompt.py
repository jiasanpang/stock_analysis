# -*- coding: utf-8 -*-
"""Tests for V2 prompt builder (src/_review_v2_prompt_builder.py)."""

import unittest
from unittest.mock import MagicMock

from src._review_v2_types import (
    DailyReviewV2,
    HoldingReview,
    LimitUpLadder,
    LimitUpStock,
    NorthBoundFlow,
    WatchPoolCandidate,
)
from src.market_analyzer import MarketOverview, MarketIndex


class TestComposeV2DailyPrompt(unittest.TestCase):
    """Test compose_v2_daily_prompt generates complete prompt."""

    def _make_full_data(self) -> DailyReviewV2:
        overview = MarketOverview(
            date="2025-01-10",
            indices=[
                MarketIndex(name="上证指数", code="000001", current=3200.0, change_pct=1.2),
                MarketIndex(name="创业板指", code="399006", current=2100.0, change_pct=-0.5),
            ],
            up_count=3500,
            down_count=1200,
            flat_count=300,
            limit_up_count=50,
            limit_down_count=5,
            total_amount=12000.0,
            top_sectors=[
                {"name": "半导体", "change_pct": 3.5},
                {"name": "AI", "change_pct": 2.8},
            ],
            bottom_sectors=[
                {"name": "房地产", "change_pct": -2.0},
            ],
        )
        north = NorthBoundFlow(
            date="2025-01-10",
            sh_connect_net=30.0,
            sz_connect_net=20.0,
            total_net=50.0,
            data_available=True,
        )
        ladder = LimitUpLadder(
            date="2025-01-10",
            data_available=True,
            total_limit_up=50,
            total_broken=10,
            broken_rate=16.7,
            dtgc_count=5,
            max_streak=5,
            loss_effect="涨停结构相对健康",
            ladder={
                5: [LimitUpStock(code="001", name="龙头A", change_pct=10.0, sector="半导体")],
                3: [LimitUpStock(code="002", name="龙头B", change_pct=10.0, sector="AI")],
            },
        )
        holdings = [
            HoldingReview(
                code="600519", name="贵州茅台", change_pct=2.0, price=1800.0,
                vs_market="强于大盘", ma5=1780, ma10=1750, ma20=1700,
                ma_alignment="多头排列", macd_status="金叉",
                support_level=1700, resistance_level=1850, volume_status="放量",
                data_available=True,
            ),
        ]
        watch_pool = [
            WatchPoolCandidate(
                code="000001", name="平安银行", sector="银行",
                change_pct=3.0, reason="主线板块", buy_condition="回踩14.5",
                stop_loss=14.0, ideal_buy_price=14.5, risk_reward=2.0,
                data_available=True,
            ),
        ]

        return DailyReviewV2(
            date="2025-01-10",
            market_overview=overview,
            north_flow=north,
            limit_up_ladder=ladder,
            holdings=holdings,
            watch_pool=watch_pool,
        )

    def test_prompt_contains_all_sections(self):
        from src._review_v2_prompt_builder import compose_v2_daily_prompt
        data = self._make_full_data()
        prompt = compose_v2_daily_prompt(data)

        self.assertIn("大盘全景", prompt)
        self.assertIn("涨停情绪", prompt)
        self.assertIn("持仓复盘", prompt)
        self.assertIn("次日观察池", prompt)
        self.assertIn("明日策略", prompt)

    def test_prompt_contains_data_values(self):
        from src._review_v2_prompt_builder import compose_v2_daily_prompt
        data = self._make_full_data()
        prompt = compose_v2_daily_prompt(data)

        self.assertIn("2025-01-10", prompt)
        self.assertIn("上证指数", prompt)
        self.assertIn("贵州茅台", prompt)
        self.assertIn("平安银行", prompt)
        self.assertIn("北向资金", prompt)

    def test_prompt_missing_data_graceful(self):
        from src._review_v2_prompt_builder import compose_v2_daily_prompt
        data = DailyReviewV2(date="2025-01-10")
        prompt = compose_v2_daily_prompt(data)

        # Should still generate without crashing
        self.assertIn("2025-01-10", prompt)
        self.assertIn("暂无", prompt)


class TestSectionBuilders(unittest.TestCase):
    """Test individual section builder functions."""

    def test_market_section_no_overview(self):
        from src._review_v2_prompt_builder import _build_market_section
        data = DailyReviewV2(date="2025-01-10")
        result = _build_market_section(data)
        self.assertIn("暂无", result)

    def test_limit_up_section_unavailable(self):
        from src._review_v2_prompt_builder import _build_limit_up_section
        data = DailyReviewV2(date="2025-01-10")
        result = _build_limit_up_section(data)
        self.assertIn("暂不可用", result)

    def test_holdings_section_empty(self):
        from src._review_v2_prompt_builder import _build_holdings_section
        data = DailyReviewV2(date="2025-01-10")
        result = _build_holdings_section(data)
        self.assertIn("STOCK_LIST", result)

    def test_watch_pool_section_empty(self):
        from src._review_v2_prompt_builder import _build_watch_pool_section
        data = DailyReviewV2(date="2025-01-10")
        result = _build_watch_pool_section(data)
        self.assertIn("暂无", result)

    def test_news_section_empty(self):
        from src._review_v2_prompt_builder import _build_news_section
        data = DailyReviewV2(date="2025-01-10")
        result = _build_news_section(data)
        self.assertIn("暂无", result)


class TestNorthFlowDescription(unittest.TestCase):
    """Test NorthBoundFlow.flow_description."""

    def test_large_inflow(self):
        flow = NorthBoundFlow(total_net=80.0, data_available=True)
        self.assertIn("大幅净流入", flow.flow_description)

    def test_small_inflow(self):
        flow = NorthBoundFlow(total_net=20.0, data_available=True)
        self.assertIn("小幅净流入", flow.flow_description)

    def test_large_outflow(self):
        flow = NorthBoundFlow(total_net=-80.0, data_available=True)
        self.assertIn("大幅净流出", flow.flow_description)

    def test_unavailable(self):
        flow = NorthBoundFlow(data_available=False)
        self.assertIn("暂不可用", flow.flow_description)


class TestMarketStrategyBlueprintV2(unittest.TestCase):
    """Test that CN blueprint contains V2 dimensions."""

    def test_cn_blueprint_has_limit_up_dimension(self):
        from src.core.market_strategy import CN_BLUEPRINT
        dim_names = [d.name for d in CN_BLUEPRINT.dimensions]
        self.assertIn("涨停情绪", dim_names)

    def test_cn_blueprint_has_holdings_dimension(self):
        from src.core.market_strategy import CN_BLUEPRINT
        dim_names = [d.name for d in CN_BLUEPRINT.dimensions]
        self.assertIn("持仓检视", dim_names)

    def test_us_blueprint_unchanged(self):
        from src.core.market_strategy import US_BLUEPRINT
        dim_names = [d.name for d in US_BLUEPRINT.dimensions]
        self.assertIn("Trend Regime", dim_names)
        # US should NOT have V2-specific CN dimensions
        self.assertNotIn("涨停情绪", dim_names)


if __name__ == "__main__":
    unittest.main()
