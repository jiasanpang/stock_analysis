# -*- coding: utf-8 -*-
"""Limit-up / broken-board / north-bound flow data fetchers (akshare).

All functions are standalone helpers with built-in timeout and error handling.
They return typed dataclass results; on failure they return empty/default objects
so the caller's review pipeline is never blocked.

APIs used:
- ``ak.stock_zt_pool_em(date)``          涨停池
- ``ak.stock_zt_pool_zbgc_em(date)``     炸板池
- ``ak.stock_zt_pool_dtgc_em(date)``     跌停池
- ``ak.stock_hsgt_fund_flow_summary_em`` 北向资金
- ``ak.stock_board_industry_cons_em``    板块成分股
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from src._concurrency import run_with_timeout, FuturesTimeout
from src._review_v2_types import (
    LimitUpLadder,
    LimitUpStock,
    NorthBoundFlow,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20  # seconds per API call


# ---------------------------------------------------------------------------
# Internal: safe akshare import + call wrapper
# ---------------------------------------------------------------------------

def _safe_akshare_call(fn, label: str, timeout: float = _DEFAULT_TIMEOUT):
    """Call *fn* with timeout; return DataFrame or None."""
    try:
        result = run_with_timeout(fn, timeout, label)
        if result is not None and isinstance(result, pd.DataFrame) and not result.empty:
            return result
    except FuturesTimeout:
        logger.warning(f"[LimitUp] {label} 超时 ({timeout}s)")
    except Exception as e:
        logger.warning(f"[LimitUp] {label} 失败: {e}")
    return None


# ---------------------------------------------------------------------------
# Public: Limit-up pool
# ---------------------------------------------------------------------------

def get_limit_up_pool(date: Optional[str] = None) -> List[LimitUpStock]:
    """获取涨停池个股列表.

    Args:
        date: 日期字符串 "YYYYMMDD", 默认今天.
    """
    import akshare as ak

    date_str = date or datetime.now().strftime("%Y%m%d")

    def _call():
        return ak.stock_zt_pool_em(date=date_str)

    df = _safe_akshare_call(_call, f"stock_zt_pool_em({date_str})")
    if df is None:
        return []

    stocks: List[LimitUpStock] = []
    try:
        for _, row in df.iterrows():
            stock = LimitUpStock(
                code=str(row.get("代码", "")),
                name=str(row.get("名称", "")),
                change_pct=_safe_float(row.get("涨跌幅", 0)),
                price=_safe_float(row.get("最新价", 0)),
                amount=_safe_float(row.get("成交额", 0)) / 1e8,  # 转为亿
                first_seal_time=str(row.get("首次封板时间", "")),
                last_seal_time=str(row.get("最后封板时间", "")),
                broken_count=int(_safe_float(row.get("炸板次数", 0))),
                streak_count=int(_safe_float(row.get("连板数", 1))),
                seal_funds=_safe_float(row.get("封板资金", 0)) / 1e8,
                sector=str(row.get("所属行业", "")),
                turnover_rate=_safe_float(row.get("换手率", 0)),
            )
            stocks.append(stock)
        logger.info(f"[LimitUp] 涨停池获取 {len(stocks)} 只 ({date_str})")
    except Exception as e:
        logger.warning(f"[LimitUp] 解析涨停池失败: {e}")
    return stocks


# ---------------------------------------------------------------------------
# Public: Broken-board pool
# ---------------------------------------------------------------------------

def get_broken_board_pool(date: Optional[str] = None) -> int:
    """获取炸板池数量（曾触涨停后回落）.

    Returns:
        炸板个股数量; 失败返回 0.
    """
    import akshare as ak

    date_str = date or datetime.now().strftime("%Y%m%d")

    def _call():
        return ak.stock_zt_pool_zbgc_em(date=date_str)

    df = _safe_akshare_call(_call, f"stock_zt_pool_zbgc_em({date_str})")
    if df is None:
        return 0
    count = len(df)
    logger.info(f"[LimitUp] 炸板池 {count} 只 ({date_str})")
    return count


# ---------------------------------------------------------------------------
# Public: Limit-down pool
# ---------------------------------------------------------------------------

def get_limit_down_pool(date: Optional[str] = None) -> int:
    """获取跌停池数量.

    Returns:
        跌停个股数量; 失败返回 0.
    """
    import akshare as ak

    date_str = date or datetime.now().strftime("%Y%m%d")

    def _call():
        return ak.stock_zt_pool_dtgc_em(date=date_str)

    df = _safe_akshare_call(_call, f"stock_zt_pool_dtgc_em({date_str})")
    if df is None:
        return 0
    count = len(df)
    logger.info(f"[LimitUp] 跌停池 {count} 只 ({date_str})")
    return count


# ---------------------------------------------------------------------------
# Public: Build limit-up ladder
# ---------------------------------------------------------------------------

def build_limit_up_ladder(
    date: Optional[str] = None,
) -> LimitUpLadder:
    """构建完整连板梯队.

    依次调用涨停池/炸板池/跌停池, 按连板数分组, 计算炸板率.
    """
    date_str = date or datetime.now().strftime("%Y%m%d")

    zt_stocks = get_limit_up_pool(date_str)
    broken_count = get_broken_board_pool(date_str)
    dtgc_count = get_limit_down_pool(date_str)

    ladder = LimitUpLadder(date=date_str)

    if not zt_stocks and broken_count == 0:
        ladder.data_available = False
        return ladder

    ladder.data_available = True
    ladder.total_limit_up = len(zt_stocks)
    ladder.total_broken = broken_count
    ladder.dtgc_count = dtgc_count

    # 炸板率
    total_attempts = len(zt_stocks) + broken_count
    ladder.broken_rate = (broken_count / total_attempts * 100) if total_attempts > 0 else 0.0

    # 按连板数分组
    groups: Dict[int, List[LimitUpStock]] = defaultdict(list)
    max_streak = 0
    for s in zt_stocks:
        streak = max(s.streak_count, 1)
        groups[streak].append(s)
        if streak > max_streak:
            max_streak = streak

    ladder.ladder = dict(groups)
    ladder.max_streak = max_streak

    # 亏钱效应检测
    ladder.loss_effect = _detect_loss_effect(
        limit_up_count=len(zt_stocks),
        broken_count=broken_count,
        dtgc_count=dtgc_count,
        max_streak=max_streak,
    )

    logger.info(
        f"[LimitUp] 连板梯队构建完成: 涨停{len(zt_stocks)} 炸板{broken_count} "
        f"跌停{dtgc_count} 最高{max_streak}连板 炸板率{ladder.broken_rate:.1f}%"
    )
    return ladder


# ---------------------------------------------------------------------------
# Public: North-bound flow
# ---------------------------------------------------------------------------

def get_north_flow() -> NorthBoundFlow:
    """获取北向资金当日净流入数据."""
    import akshare as ak

    flow = NorthBoundFlow(date=datetime.now().strftime("%Y-%m-%d"))

    def _call():
        return ak.stock_hsgt_fund_flow_summary_em()

    df = _safe_akshare_call(_call, "stock_hsgt_fund_flow_summary_em")
    if df is None:
        logger.warning("[LimitUp] 北向资金数据不可用")
        return flow

    try:
        # 东方财富接口返回格式可能变动, 做兼容处理
        # 常见列: "沪股通-净买额", "深股通-净买额" 或 "当日净流入"
        sh_net = 0.0
        sz_net = 0.0

        if len(df) > 0:
            row = df.iloc[-1]  # 最新一条
            for col in df.columns:
                col_lower = str(col).lower()
                if "沪" in col and "净" in col:
                    sh_net = _safe_float(row.get(col, 0)) / 1e4  # 万元→亿
                elif "深" in col and "净" in col:
                    sz_net = _safe_float(row.get(col, 0)) / 1e4
                elif "净流入" in col or "净买额" in col_lower:
                    total = _safe_float(row.get(col, 0)) / 1e4
                    # 如果只有一个合计列
                    if sh_net == 0.0 and sz_net == 0.0:
                        sh_net = total  # 近似

        flow.sh_connect_net = sh_net
        flow.sz_connect_net = sz_net
        flow.total_net = sh_net + sz_net
        # NaN 保护
        if pd.isna(flow.total_net) or flow.total_net == 0:
            flow.data_available = False
        else:
            flow.data_available = True

        logger.info(f"[LimitUp] 北向资金: 沪{sh_net:.1f}亿 深{sz_net:.1f}亿 合计{flow.total_net:.1f}亿")
    except Exception as e:
        logger.warning(f"[LimitUp] 解析北向资金失败: {e}")

    return flow


# ---------------------------------------------------------------------------
# Public: Board (sector) constituent stocks
# ---------------------------------------------------------------------------

def get_board_stocks(sector_name: str) -> List[Dict[str, Any]]:
    """获取行业板块成分股列表.

    Args:
        sector_name: 板块名称 (e.g. "半导体", "新能源")

    Returns:
        [{"code": ..., "name": ..., "change_pct": ..., "price": ...}, ...]
    """
    import akshare as ak

    def _call():
        return ak.stock_board_industry_cons_em(symbol=sector_name)

    df = _safe_akshare_call(_call, f"stock_board_industry_cons_em({sector_name})")
    if df is None:
        return []

    results: List[Dict[str, Any]] = []
    try:
        for _, row in df.iterrows():
            results.append({
                "code": str(row.get("代码", "")),
                "name": str(row.get("名称", "")),
                "change_pct": _safe_float(row.get("涨跌幅", 0)),
                "price": _safe_float(row.get("最新价", 0)),
                "amount": _safe_float(row.get("成交额", 0)),
                "turnover_rate": _safe_float(row.get("换手率", 0)),
            })
    except Exception as e:
        logger.warning(f"[LimitUp] 解析板块成分股失败({sector_name}): {e}")

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_float(val, default: float = 0.0) -> float:
    """Safely convert to float, returning *default* on failure."""
    try:
        v = float(val)
        if pd.isna(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _detect_loss_effect(
    limit_up_count: int,
    broken_count: int,
    dtgc_count: int,
    max_streak: int,
) -> str:
    """检测亏钱效应.

    判断标准:
    - 炸板率高(>40%) + 跌停多(>10) = 接力风险大
    - 最高连板断板(前一日更高) = 情绪退潮
    - 跌停 > 涨停 = 极端亏钱效应
    """
    total = limit_up_count + broken_count
    broken_rate = (broken_count / total * 100) if total > 0 else 0

    if dtgc_count > limit_up_count:
        return "极端亏钱效应，跌停多于涨停，管住手"
    if broken_rate > 40 and dtgc_count > 10:
        return "炸板率偏高且跌停较多，接力风险大，谨慎追高"
    if broken_rate > 40:
        return f"炸板率{broken_rate:.0f}%偏高，高位股分歧明显"
    if dtgc_count > 10:
        return f"跌停{dtgc_count}只偏多，注意恐慌情绪蔓延"
    if max_streak <= 2:
        return "连板高度受限，短线情绪偏弱"
    return "涨停结构相对健康，市场情绪尚可"
