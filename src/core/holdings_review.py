# -*- coding: utf-8 -*-
"""Holdings deep review for V2 daily recap (Step 3).

Reads ``config.stock_list`` as the user's holdings / watchlist, fetches
each stock's daily bars, runs trend analysis (MA / MACD / volume), and
compares individual performance against the broad market and sector.

Returns ``List[HoldingReview]`` for the LLM prompt.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List, Optional

import pandas as pd

from src._review_v2_types import HoldingReview
from src.config import get_config

logger = logging.getLogger(__name__)


def review_holdings(market_overview=None) -> List[HoldingReview]:
    """对 STOCK_LIST 中的每只持仓进行深度复盘.

    Args:
        market_overview: ``MarketOverview`` 实例, 用于获取大盘涨跌幅做对比.

    Returns:
        List[HoldingReview]
    """
    from data_provider.base import DataFetcherManager
    from src.stock_analyzer import StockTrendAnalyzer

    config = get_config()
    stock_codes = getattr(config, 'stock_list', []) or []
    if not stock_codes:
        logger.info("[HoldingsReview] STOCK_LIST 为空，跳过持仓复盘")
        return []

    manager = DataFetcherManager()
    trend_analyzer = StockTrendAnalyzer()

    # 大盘涨跌幅 (上证指数) 用于对比
    market_change_pct = _get_market_change_pct(market_overview)

    results: List[HoldingReview] = []
    for code in stock_codes:
        try:
            hr = _review_single_holding(
                code=code,
                manager=manager,
                trend_analyzer=trend_analyzer,
                market_change_pct=market_change_pct,
            )
            if hr:
                results.append(hr)
        except Exception as e:
            logger.warning(f"[HoldingsReview] {code} 复盘失败: {e}")

    logger.info(f"[HoldingsReview] 完成 {len(results)}/{len(stock_codes)} 只持仓复盘")
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_market_change_pct(market_overview) -> float:
    """从 MarketOverview 中获取上证指数涨跌幅."""
    if market_overview is None:
        return 0.0
    for idx in getattr(market_overview, 'indices', []):
        if '000001' in getattr(idx, 'code', ''):
            return idx.change_pct
    return 0.0


def _review_single_holding(
    code: str,
    manager,
    trend_analyzer,
    market_change_pct: float,
) -> Optional[HoldingReview]:
    """对单只持仓做深度复盘."""
    hr = HoldingReview(code=code)

    # 1. 获取日线数据 (~90 天 for MA60)
    end_date = date.today()
    start_date = end_date - timedelta(days=89)
    try:
        df, source = manager.get_daily_data(
            code,
            start_date=start_date.strftime('%Y-%m-%d'),
            end_date=end_date.strftime('%Y-%m-%d'),
            days=90,
        )
    except Exception as e:
        logger.warning(f"[HoldingsReview] {code} 获取日线数据失败: {e}")
        return None

    if df is None or df.empty or len(df) < 20:
        logger.warning(f"[HoldingsReview] {code} 数据不足 ({len(df) if df is not None else 0} 行)")
        return None

    # 2. 趋势分析
    try:
        trend_result = trend_analyzer.analyze(df, code)
    except Exception as e:
        logger.warning(f"[HoldingsReview] {code} 趋势分析失败: {e}")
        trend_result = None

    if trend_result is None:
        return None

    hr.data_available = True
    hr.price = trend_result.current_price
    hr.ma5 = trend_result.ma5
    hr.ma10 = trend_result.ma10
    hr.ma20 = trend_result.ma20

    # 3. 今日涨跌幅
    try:
        latest_row = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) >= 2 else latest_row
        hr.change_pct = (
            (float(latest_row['close']) - float(prev_row['close']))
            / float(prev_row['close']) * 100
        ) if float(prev_row['close']) > 0 else 0.0
    except Exception:
        hr.change_pct = 0.0

    # 4. vs 大盘
    if hr.change_pct > market_change_pct + 1:
        hr.vs_market = "强于大盘"
    elif hr.change_pct < market_change_pct - 1:
        hr.vs_market = "弱于大盘"
    else:
        hr.vs_market = "与大盘持平"

    # 5. MA 排列
    hr.ma_alignment = _describe_ma_alignment(trend_result)

    # 6. MACD 状态
    hr.macd_status = _describe_macd(trend_result)

    # 7. 量能状态
    hr.volume_status = _describe_volume(trend_result)

    # 8. 支撑阻力
    if trend_result.support_levels:
        hr.support_level = trend_result.support_levels[0]
    elif trend_result.ma20 > 0:
        hr.support_level = trend_result.ma20

    if trend_result.resistance_levels:
        hr.resistance_level = trend_result.resistance_levels[0]

    # 9. 股票名称 (从数据中推断)
    if hasattr(df, 'columns') and 'name' in df.columns:
        hr.name = str(df.iloc[-1].get('name', code))
    else:
        hr.name = code

    return hr


def _describe_ma_alignment(trend) -> str:
    """描述均线排列."""
    ma5, ma10, ma20 = trend.ma5, trend.ma10, trend.ma20
    if ma5 > ma10 > ma20 > 0:
        return "多头排列"
    if ma5 < ma10 < ma20 and ma20 > 0:
        return "空头排列"
    if ma5 > ma10:
        return "短期偏多"
    if ma5 < ma10:
        return "短期偏弱"
    return "均线交叉"


def _describe_macd(trend) -> str:
    """描述 MACD 状态."""
    status = getattr(trend, 'macd_status', None)
    if status is not None:
        return status.value if hasattr(status, 'value') else str(status)
    return "未知"


def _describe_volume(trend) -> str:
    """描述量能状态."""
    vol_status = getattr(trend, 'volume_status', None)
    if vol_status is not None:
        return vol_status.value if hasattr(vol_status, 'value') else str(vol_status)
    ratio = getattr(trend, 'volume_ratio_5d', 1.0)
    if ratio > 1.5:
        return "放量"
    if ratio < 0.7:
        return "缩量"
    return "平量"
