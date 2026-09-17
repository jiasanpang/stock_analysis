# -*- coding: utf-8 -*-
"""
Position Tracker Service
=========================

Lightweight position-tracking helpers that wrap the unified
`trade_levels.evaluate_trailing_exit` engine. Designed to be invoked from:
- Agent skills (when user provides entry price + code)
- Daily watchlist scan (when entry price is recorded externally)
- Notification weekly digest

This service is intentionally stateless — it does NOT own a position store.
Callers supply (code, entry_price, strategy_id) and current market context;
this service returns an actionable recommendation.

Public API:
- evaluate_holding(...)       -> HoldingDecision
- format_decision_message(...) -> str (Chinese summary for notification)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.services.limit_up_rules import (
    evaluate_limit_up_pullback_exit,
    lup_exit_enabled,
)
from src.services.trade_levels import evaluate_trailing_exit

logger = logging.getLogger(__name__)


@dataclass
class HoldingDecision:
    """Decision output for one held position."""

    code: str
    name: str
    entry_price: float
    current_price: float
    profit_pct: float
    should_exit: bool
    exit_reason: str = ""
    action: str = "持有"            # 持有 / 减仓 / 清仓
    stage_note: str = ""           # Stage-progress hint
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "entry_price": round(self.entry_price, 3),
            "current_price": round(self.current_price, 3),
            "profit_pct": round(self.profit_pct, 2),
            "should_exit": self.should_exit,
            "exit_reason": self.exit_reason,
            "action": self.action,
            "stage_note": self.stage_note,
            "notes": self.notes,
        }


def _stage_note(profit_pct: float, strategy_id: str) -> str:
    """Return human-readable stage progress hint.

    Thresholds aligned with current trade_levels.evaluate_trailing_exit:
    +6% / +12% staged stops, +15% trailing trigger.
    """
    if profit_pct >= 15.0:
        return "trailing 区：跌破 MA10 或回撤 ATR×2.5 即出"
    if profit_pct >= 12.0:
        return "已减 1/3，止损上移至 +6%；浮盈 +15% 启用 trailing"
    if profit_pct >= 6.0:
        return "已减 1/3，止损上移至成本"
    if profit_pct >= 0.0:
        return "持有观察，等待浮盈 +6%"
    return "成本下方，关注止损位"


def _lup_stage_note(profit_pct: float, trimmed: bool) -> str:
    """Stage hint aligned with the 涨停回踩 staged-exit rules."""
    if trimmed:
        return "已减半止盈，剩余仓位按 MA10 / ATR×2.5 跟踪，回吐 <+5% 清仓"
    if profit_pct >= 10.0:
        return "触及 +10%：分批止盈一半，止损上移至成本"
    if profit_pct >= 0.0:
        return "持有观察，+10% 分批止盈；破涨停最低价/-4% 止损"
    return "成本下方，严守结构止损位（涨停最低价）"


def _evaluate_lup_holding(
    *,
    code: str,
    name: str,
    entry_price: float,
    current_price: float,
    profit_pct: float,
    ma10: float,
    ma20: float,
    atr: float,
    holding_days: int,
    peak_price: Optional[float],
    trimmed: bool,
) -> HoldingDecision:
    """涨停回踩 path: staged trim + trailing rules from limit_up_rules."""
    verdict, reason = evaluate_limit_up_pullback_exit(
        entry_price=entry_price,
        current_price=current_price,
        ma10=ma10, ma20=ma20, atr=atr,
        holding_days=holding_days,
        peak_price=peak_price,
        trimmed=trimmed,
    )
    if verdict == "exit":
        should_exit, action = True, "清仓"
    elif verdict == "trim":
        should_exit, action = False, "止盈 1/2 + 止损上移至成本"
    else:
        should_exit, action = False, "持有"
    return HoldingDecision(
        code=code, name=name,
        entry_price=entry_price, current_price=current_price,
        profit_pct=profit_pct,
        should_exit=should_exit, exit_reason=reason,
        action=action,
        stage_note=_lup_stage_note(profit_pct, trimmed),
    )


def evaluate_holding(
    *,
    code: str,
    name: str = "",
    strategy_id: str = "buy_pullback",
    entry_price: float,
    current_price: float,
    current_high: Optional[float] = None,
    ma10: float = 0.0,
    ma20: float = 0.0,
    atr: float = 0.0,
    holding_days: int = 0,
    peak_price: Optional[float] = None,
    trimmed: bool = False,
) -> HoldingDecision:
    """Evaluate whether a single held position should be exited or trimmed.

    For ``buy_pullback`` (涨停回踩 engine, LUP_EXIT=1) this routes to
    ``limit_up_rules.evaluate_limit_up_pullback_exit``; all other strategies
    wrap ``trade_levels.evaluate_trailing_exit``. Adds stage-aware action
    classification (持有 / 减仓 / 清仓).
    """
    if entry_price <= 0 or current_price <= 0:
        return HoldingDecision(
            code=code, name=name, entry_price=entry_price, current_price=current_price,
            profit_pct=0.0, should_exit=False, action="持有",
            notes=["invalid_input"],
        )

    profit_pct = (current_price - entry_price) / entry_price * 100.0

    if strategy_id == "buy_pullback" and lup_exit_enabled():
        return _evaluate_lup_holding(
            code=code, name=name,
            entry_price=entry_price, current_price=current_price,
            profit_pct=profit_pct,
            ma10=ma10, ma20=ma20, atr=atr,
            holding_days=holding_days, peak_price=peak_price,
            trimmed=trimmed,
        )

    should_exit, reason = evaluate_trailing_exit(
        strategy_id=strategy_id,
        entry_price=entry_price,
        current_price=current_price,
        current_high=current_high or current_price,
        ma10=ma10, ma20=ma20, atr=atr,
        holding_days=holding_days,
        peak_price=peak_price,
    )

    if should_exit:
        # Any trade_levels exit reason is treated as full close; reason strings
        # are kept for transparency but action mapping is uniform.
        action = "清仓"
    else:
        # Even when not exiting, suggest staged trimming at +6% / +12%.
        if profit_pct >= 12.0:
            action = "减仓 1/3 + trailing 跟踪"
        elif profit_pct >= 6.0:
            action = "减仓 1/3 + 止损上移至成本"
        else:
            action = "持有"

    return HoldingDecision(
        code=code, name=name,
        entry_price=entry_price, current_price=current_price,
        profit_pct=profit_pct,
        should_exit=should_exit, exit_reason=reason,
        action=action,
        stage_note=_stage_note(profit_pct, strategy_id),
    )


def format_decision_message(decision: HoldingDecision) -> str:
    """Format a Chinese one-liner for notification."""
    emoji = "🔴" if decision.should_exit else ("🟡" if decision.profit_pct >= 6 else "⚪")
    parts = [
        f"{emoji} {decision.name}({decision.code})",
        f"成本 {decision.entry_price:.2f} → 现价 {decision.current_price:.2f}",
        f"浮盈 {decision.profit_pct:+.2f}%",
        f"建议：{decision.action}",
    ]
    if decision.exit_reason:
        parts.append(f"理由：{decision.exit_reason}")
    if decision.stage_note:
        parts.append(f"阶段：{decision.stage_note}")
    return " | ".join(parts)
