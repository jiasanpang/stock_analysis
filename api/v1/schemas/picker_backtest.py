# -*- coding: utf-8 -*-
"""Picker backtest API schemas."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class PickerBacktestRunRequest(BaseModel):
    start_date: str = Field(..., description="Start date YYYY-MM-DD or YYYYMMDD")
    end_date: str = Field(..., description="End date YYYY-MM-DD or YYYYMMDD")
    hold_days: int = Field(10, ge=1, le=60, description="Holding period in trading days")
    top_n: int = Field(5, ge=1, le=20, description="Number of picks per day by score")
    picker_strategies: Optional[List[str]] = Field(
        None,
        description="Strategies to use: buy_pullback",
    )


class PickerBacktestResultItem(BaseModel):
    trade_date: str
    code: str
    name: str = ""
    entry_price: float
    exit_price: Optional[float] = None
    return_pct: Optional[float] = None
    outcome: str  # win | loss | insufficient
    score: float = 0.0
    # Trade-levels engine diagnostics (why/when the trade exited)
    exit_reason: Optional[str] = None  # stop_loss / trailing_ma10 / stage_break_+12pct / hardcap_+20pct / window_end / ...
    hold_days: Optional[int] = None
    strategy_id: Optional[str] = None  # buy_pullback


class PickerBacktestSummary(BaseModel):
    start_date: str
    end_date: str
    hold_days: int
    top_n: int
    trade_dates_with_picks: int = 0  # days that had candidates (explains low total_picks)
    total_picks: int
    win_count: int
    loss_count: int
    insufficient_count: int
    win_rate_pct: Optional[float] = None
    avg_return_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    profit_factor: Optional[float] = None
    alpha_vs_benchmark_pct: Optional[float] = None
    benchmark_avg_return_pct: Optional[float] = None
    # Portfolio-level NAV metrics (single-trade stats can undersell the
    # compounding picture for portfolio-style strategies).
    cagr_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    calmar_ratio: Optional[float] = None


class PickerBacktestRunResponse(BaseModel):
    success: bool = True
    results: List[PickerBacktestResultItem] = Field(default_factory=list)
    summary: Optional[PickerBacktestSummary] = None
    trade_dates_count: int = 0
