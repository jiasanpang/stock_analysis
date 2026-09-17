# -*- coding: utf-8 -*-
"""Dataclasses for the V2 review system (4-step daily + weekly review).

Covers:
- LimitUpStock / LimitUpLadder  (Step 2: limit-up sentiment)
- NorthBoundFlow                (Step 1: north-bound capital)
- HoldingReview                 (Step 3: holdings deep review)
- WatchPoolCandidate            (Step 4: next-day watch pool)
- DailyReviewV2                 (aggregate of all 4 steps)
- WeeklyReviewData              (weekly review)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.market_analyzer import MarketOverview
from src._enhanced_market_types import (
    SentimentAnalysis,
    SectorHotspot,
    TechnicalAnalysis,
)


# ---------------------------------------------------------------------------
# Step 1 supplements: North-bound flow
# ---------------------------------------------------------------------------

@dataclass
class NorthBoundFlow:
    """北向资金流向数据"""
    date: str = ""
    sh_connect_net: float = 0.0   # 沪股通净买额(亿)
    sz_connect_net: float = 0.0   # 深股通净买额(亿)
    total_net: float = 0.0        # 合计净流入(亿)
    data_available: bool = False  # 数据是否可用

    @property
    def flow_description(self) -> str:
        if not self.data_available:
            return "北向资金数据暂不可用"
        if self.total_net > 50:
            return f"北向资金大幅净流入 {self.total_net:.1f} 亿，外资积极做多"
        if self.total_net > 0:
            return f"北向资金小幅净流入 {self.total_net:.1f} 亿"
        if self.total_net > -50:
            return f"北向资金小幅净流出 {abs(self.total_net):.1f} 亿"
        return f"北向资金大幅净流出 {abs(self.total_net):.1f} 亿，注意外资动向"


# ---------------------------------------------------------------------------
# Step 2: Limit-up ladder & sentiment
# ---------------------------------------------------------------------------

@dataclass
class LimitUpStock:
    """涨停板个股"""
    code: str = ""
    name: str = ""
    change_pct: float = 0.0
    price: float = 0.0
    amount: float = 0.0          # 成交额(亿)
    first_seal_time: str = ""    # 首次封板时间
    last_seal_time: str = ""     # 最后封板时间
    broken_count: int = 0        # 炸板次数
    streak_count: int = 1        # 连板数
    seal_funds: float = 0.0      # 封板资金(亿)
    sector: str = ""             # 所属行业
    turnover_rate: float = 0.0   # 换手率(%)


@dataclass
class LimitUpLadder:
    """连板梯队 & 涨停情绪"""
    date: str = ""
    max_streak: int = 0                                  # 最高连板数
    ladder: Dict[int, List[LimitUpStock]] = field(default_factory=dict)
    total_limit_up: int = 0       # 总涨停数
    total_broken: int = 0         # 总炸板数(曾涨停后打开)
    broken_rate: float = 0.0      # 炸板率 = broken / (limit_up + broken)
    dtgc_count: int = 0           # 跌停股数量
    loss_effect: str = ""         # 亏钱效应描述
    data_available: bool = False  # 数据是否可用

    def get_streak_summary(self) -> str:
        """生成连板梯队摘要文本"""
        if not self.data_available:
            return "涨停数据暂不可用"
        if not self.ladder:
            return "今日无涨停"
        lines = []
        for streak in sorted(self.ladder.keys(), reverse=True):
            stocks = self.ladder[streak]
            names = "、".join([s.name for s in stocks[:5]])
            suffix = f"等{len(stocks)}只" if len(stocks) > 5 else ""
            lines.append(f"{streak}连板({len(stocks)}只): {names}{suffix}")
        return " | ".join(lines)


# ---------------------------------------------------------------------------
# Step 3: Holdings deep review
# ---------------------------------------------------------------------------

@dataclass
class HoldingReview:
    """持仓个股复盘"""
    code: str = ""
    name: str = ""
    change_pct: float = 0.0
    price: float = 0.0
    # vs market / sector
    vs_market: str = ""          # "强于大盘" / "弱于大盘"
    vs_sector: str = ""          # "强于板块" / "弱于板块"
    sector_name: str = ""        # 所属板块名称
    # fundamentals
    pe_ratio: float = 0.0
    pb_ratio: float = 0.0
    # technicals
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma_alignment: str = ""       # "多头排列" / "空头排列" / "交叉"
    support_level: float = 0.0
    resistance_level: float = 0.0
    macd_status: str = ""        # "金叉" / "死叉" / "零轴上" / "零轴下"
    volume_status: str = ""      # "放量" / "缩量" / "平量"
    # advice
    operation_note: str = ""     # LLM 生成的操作建议
    data_available: bool = False


# ---------------------------------------------------------------------------
# Step 4: Next-day watch pool
# ---------------------------------------------------------------------------

@dataclass
class WatchPoolCandidate:
    """次日观察池候选"""
    code: str = ""
    name: str = ""
    sector: str = ""
    change_pct: float = 0.0
    reason: str = ""             # 入选理由
    buy_condition: str = ""      # 买入条件
    stop_loss: float = 0.0       # 止损位
    ideal_buy_price: float = 0.0 # 理想买入价
    risk_reward: float = 0.0     # 盈亏比
    data_available: bool = False


# ---------------------------------------------------------------------------
# Aggregate: Daily Review V2
# ---------------------------------------------------------------------------

@dataclass
class DailyReviewV2:
    """V2 每日复盘完整数据（4 步合一）"""
    date: str = ""
    # Step 1: 大盘整体
    market_overview: Optional[MarketOverview] = None
    north_flow: Optional[NorthBoundFlow] = None
    # Step 2: 涨停 & 情绪
    limit_up_ladder: Optional[LimitUpLadder] = None
    # Step 3: 持仓
    holdings: List[HoldingReview] = field(default_factory=list)
    # Step 4: 观察池
    watch_pool: List[WatchPoolCandidate] = field(default_factory=list)
    # Enhanced data (reuse existing)
    sentiment: Optional[SentimentAnalysis] = None
    sector_hotspots: List[SectorHotspot] = field(default_factory=list)
    technical: Optional[TechnicalAnalysis] = None
    news: List[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Weekly review
# ---------------------------------------------------------------------------

@dataclass
class WeeklyIndexPerformance:
    """单只指数周度表现"""
    name: str = ""
    weekly_change_pct: float = 0.0
    weekly_amount_change_pct: float = 0.0  # 周成交额变化


@dataclass
class WeeklySectorEntry:
    """板块周度表现"""
    name: str = ""
    weekly_change_pct: float = 0.0
    trend: str = ""  # "连续上涨" / "连续下跌" / "震荡"


@dataclass
class WeeklyReviewData:
    """周度复盘数据"""
    week_start: str = ""
    week_end: str = ""
    # indices
    weekly_indices: List[WeeklyIndexPerformance] = field(default_factory=list)
    # sector rotation
    sector_rotation: List[WeeklySectorEntry] = field(default_factory=list)
    strongest_mainline: str = ""   # 本周最强主线板块
    # holdings stats
    weekly_pnl_pct: float = 0.0    # 组合周收益率
    win_rate: float = 0.0          # 本周上涨持仓占比
    max_drawdown: float = 0.0      # 最大回撤
    holdings_detail: List[Dict[str, Any]] = field(default_factory=list)
    # adjustment suggestions
    weakened_holdings: List[str] = field(default_factory=list)
    core_positions: List[str] = field(default_factory=list)
    next_week_plan: str = ""       # 下周仓位计划
    data_available: bool = False


__all__ = [
    "DailyReviewV2",
    "HoldingReview",
    "LimitUpLadder",
    "LimitUpStock",
    "NorthBoundFlow",
    "WatchPoolCandidate",
    "WeeklyIndexPerformance",
    "WeeklyReviewData",
    "WeeklySectorEntry",
]
