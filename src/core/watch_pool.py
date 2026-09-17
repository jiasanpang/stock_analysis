# -*- coding: utf-8 -*-
"""Next-day watch pool screening for V2 daily recap (Step 4).

Primary source: the 涨停回踩 observation pool (pure tushare rules, via
StockScreener.limit_up_watch_candidates) — limit-up events that passed
quality + observation iron-rules and are awaiting pattern confirmation.
Fallback: screens 3-5 candidates from the strongest main-line sectors:
1. Identify leading sectors from ``overview.top_sectors``
2. Fetch constituent stocks of those sectors (akshare)
3. Apply technical filters: MA bullish alignment, volume-price coordination,
   not already limit-up (avoid chasing)
4. Return ``List[WatchPoolCandidate]`` with buy conditions and stop-loss.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from src._review_v2_types import WatchPoolCandidate

logger = logging.getLogger(__name__)

_MAX_WATCH_POOL = 5   # 最多 5 只
_MAX_SECTORS = 2      # 从 top 2 板块中筛选


def screen_watch_pool(market_overview=None) -> List[WatchPoolCandidate]:
    """生成次日观察池 (优先涨停回踩观察池，回退主线板块筛选).

    Args:
        market_overview: ``MarketOverview`` 实例.

    Returns:
        List[WatchPoolCandidate]
    """
    lup = _lup_watch_pool()
    if lup:
        logger.info(f"[WatchPool] 涨停回踩观察池命中 {len(lup)} 只")
        return lup[:_MAX_WATCH_POOL]

    if market_overview is None:
        logger.info("[WatchPool] 无市场概览数据，跳过观察池筛选")
        return []

    top_sectors = getattr(market_overview, 'top_sectors', []) or []
    if not top_sectors:
        logger.info("[WatchPool] 无板块数据，跳过观察池筛选")
        return []

    candidates: List[WatchPoolCandidate] = []

    for sector_data in top_sectors[:_MAX_SECTORS]:
        sector_name = sector_data.get('name', '')
        sector_change = sector_data.get('change_pct', 0.0)

        # 只从涨幅 > 1% 的板块中筛选
        if sector_change < 1.0:
            continue

        try:
            stocks = _get_sector_stocks(sector_name)
            sector_candidates = _filter_candidates(stocks, sector_name, sector_change)
            candidates.extend(sector_candidates)
        except Exception as e:
            logger.warning(f"[WatchPool] 板块 {sector_name} 筛选失败: {e}")

        if len(candidates) >= _MAX_WATCH_POOL:
            break

    # 按涨幅排序，取 top N
    candidates.sort(key=lambda c: c.change_pct, reverse=True)
    result = candidates[:_MAX_WATCH_POOL]

    logger.info(f"[WatchPool] 筛选出 {len(result)} 只观察池候选")
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _lup_watch_pool() -> List[WatchPoolCandidate]:
    """涨停回踩观察池 → WatchPoolCandidate（纯 tushare 量化规则）."""
    if os.environ.get("LUP_ENABLED", "1") != "1":
        return []
    try:
        from src.services.picker.constants import create_screener_from_config
        events = create_screener_from_config().limit_up_watch_candidates()
    except Exception as e:
        logger.warning(f"[WatchPool] 涨停回踩观察池获取失败: {e}")
        return []

    out: List[WatchPoolCandidate] = []
    for ev in events:
        ma5, ma10 = ev.get("ma5", 0.0), ev.get("ma10", 0.0)
        ideal = ma5 if ma5 > 0 else ev.get("close", 0.0)
        stop = ev.get("break_level", 0.0)
        cond = (
            f"等待回踩确认：MA5 {ma5:.2f}/MA10 {ma10:.2f} 缩量企稳可介入"
            if ma10 > 0 else "等待回踩 5/10 日线缩量企稳"
        )
        out.append(WatchPoolCandidate(
            code=ev.get("code", ""),
            name=ev.get("name", ""),
            reason=(
                f"涨停回踩观察池：{ev.get('limit_up_date', '')} 涨停，"
                f"观察第 {ev.get('days_waiting', 0)} 日等待确认形态"
            ),
            buy_condition=cond,
            stop_loss=stop,
            ideal_buy_price=ideal,
            data_available=True,
        ))
    return out


def _get_sector_stocks(sector_name: str) -> List[Dict[str, Any]]:
    """获取板块成分股."""
    try:
        from data_provider.akshare.limit_up import get_board_stocks
        return get_board_stocks(sector_name)
    except Exception as e:
        logger.warning(f"[WatchPool] 获取板块 {sector_name} 成分股失败: {e}")
        return []


def _filter_candidates(
    stocks: List[Dict[str, Any]],
    sector_name: str,
    sector_change: float,
) -> List[WatchPoolCandidate]:
    """对板块成分股做技术面过滤.

    筛选标准:
    - 涨幅 1%-8% (非涨停, 不追高)
    - 换手率 2%-15% (活跃但不过度投机)
    - 优先选择量价配合的个股
    """
    candidates: List[WatchPoolCandidate] = []

    for stock in stocks:
        code = stock.get('code', '')
        name = stock.get('name', '')
        change_pct = stock.get('change_pct', 0.0)
        price = stock.get('price', 0.0)
        turnover = stock.get('turnover_rate', 0.0)

        # 过滤: 涨幅 1%~8% (不追涨停)
        if change_pct < 1.0 or change_pct > 8.0:
            continue

        # 过滤: 换手率 2%~15%
        if turnover < 2.0 or turnover > 15.0:
            continue

        # 过滤: 价格 > 0
        if price <= 0:
            continue

        # 计算止损位 (ATR 近似: 用当日振幅 * 2 或直接 -5%)
        stop_loss_pct = 0.05  # 默认 5% 止损
        stop_loss = price * (1 - stop_loss_pct)

        # 理想买入价 (回踩 2% 附近)
        ideal_buy = price * 0.98

        # 盈亏比
        take_profit = price * 1.10  # 10% 目标
        risk = price - stop_loss
        reward = take_profit - price
        risk_reward = reward / risk if risk > 0 else 0.0

        reason = f"主线板块({sector_name} {sector_change:+.1f}%)，涨幅{change_pct:+.1f}%健康，换手{turnover:.1f}%"

        candidate = WatchPoolCandidate(
            code=code,
            name=name,
            sector=sector_name,
            change_pct=change_pct,
            reason=reason,
            buy_condition=f"回踩{ideal_buy:.2f}附近可关注",
            stop_loss=stop_loss,
            ideal_buy_price=ideal_buy,
            risk_reward=risk_reward,
            data_available=True,
        )
        candidates.append(candidate)

        if len(candidates) >= _MAX_WATCH_POOL:
            break

    return candidates
