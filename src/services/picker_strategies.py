# -*- coding: utf-8 -*-
"""
Picker strategies: each strategy has its own screening logic and fixed params.

Strategies: buy_pullback, breakout, bottom_reversal, macd_golden_cross
No intensity modes (defensive/balanced/offensive) — each strategy has one set of params.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from src.services.stock_picker_service import (
    ScreenedStock,
    TREND_DECAY_THRESHOLD_PCT,
    PE_SCORE_PARTIAL_MAX,
    VOLUME_RATIO_MIN,
    TURNOVER_MIN_PCT,
    TURNOVER_MAX_PCT,
    AMOUNT_MIN_SMALL_CAP,
    AMOUNT_MIN_LARGE_CAP,
    MARKET_CAP_TIER_YI,
)

# Strategy IDs (used in config)
BUY_PULLBACK = "buy_pullback"
BREAKOUT = "breakout"
BOTTOM_REVERSAL = "bottom_reversal"
MACD_GOLDEN_CROSS = "macd_golden_cross"

# Default strategy when PICKER_STRATEGIES not set
DEFAULT_STRATEGIES = [BUY_PULLBACK]

# All available strategies
ALL_STRATEGIES = [BUY_PULLBACK, BREAKOUT, BOTTOM_REVERSAL, MACD_GOLDEN_CROSS]

# Mid-cap bonus: 50-500 yi (亿) market cap
_MID_CAP_MIN = 50e8
_MID_CAP_MAX = 500e8

STRATEGY_DISPLAY_NAMES: Dict[str, str] = {
    BUY_PULLBACK: "买回踩",
    BREAKOUT: "突破",
    BOTTOM_REVERSAL: "底部反转",
    MACD_GOLDEN_CROSS: "MACD金叉",
}


@dataclass
class StrategyParams:
    """Fixed params for a strategy (no intensity modes)."""

    # Bias filter
    max_bias_pct: float
    # PE
    pe_max: float
    pe_ideal_low: float
    pe_ideal_high: float
    # Momentum / entry range
    daily_change_min: float
    daily_change_max: float
    # Post-filters
    max_consecutive_up_days: int
    require_volume_shrink: bool
    require_ma_bullish: bool
    max_retracement_pct: float
    # Strategy-specific
    change_60d_min: float  # min 60d change
    change_60d_max: Optional[float] = None  # max 60d change (None = no cap)
    volume_ratio_min: float = VOLUME_RATIO_MIN


# Buy pullback: 60d > 5%, MA bullish, pullback entry (expert-tuned params)
BUY_PULLBACK_PARAMS = StrategyParams(
    max_bias_pct=8.0,
    pe_max=100,
    pe_ideal_low=10,
    pe_ideal_high=35,
    daily_change_min=-2.5,
    daily_change_max=3.0,
    max_consecutive_up_days=3,
    require_volume_shrink=False,
    require_ma_bullish=True,
    max_retracement_pct=0.5,
    change_60d_min=5.0,
    change_60d_max=None,
    volume_ratio_min=1.0,
)

# Breakout: price breaks N-day high, volume confirmation
BREAKOUT_PARAMS = StrategyParams(
    max_bias_pct=12.0,  # Allow higher bias (breakout = chasing strength)
    pe_max=100,
    pe_ideal_low=15,
    pe_ideal_high=50,
    daily_change_min=2.0,  # Must be up
    daily_change_max=10.0,  # Not limit-up chase
    max_consecutive_up_days=4,
    require_volume_shrink=False,
    require_ma_bullish=False,  # Breakout may not have MA aligned yet
    max_retracement_pct=0.618,
    change_60d_min=-10.0,  # Allow some downtrend before breakout
    change_60d_max=80.0,  # Avoid parabolic
    volume_ratio_min=1.5,  # Stronger volume for breakout
)

# Bottom reversal: 60d -25% ~ 10%, stabilizing (expert-tuned: retracement relaxed)
BOTTOM_REVERSAL_PARAMS = StrategyParams(
    max_bias_pct=6.0,  # Stricter (near support)
    pe_max=100,
    pe_ideal_low=8,
    pe_ideal_high=35,
    daily_change_min=0.0,  # Stabilizing or slight up
    daily_change_max=5.0,
    max_consecutive_up_days=3,
    require_volume_shrink=False,
    require_ma_bullish=False,  # Bottom stocks often not MA bullish
    max_retracement_pct=1.0,  # Effectively skip: at bottom retracement often 100%
    change_60d_min=-25.0,
    change_60d_max=10.0,
    volume_ratio_min=1.2,  # Some volume for confirmation
)

# MACD golden cross: 60d -15% ~ 50%, daily 0% ~ 6%, volume confirmation
MACD_GOLDEN_CROSS_PARAMS = StrategyParams(
    max_bias_pct=8.0,
    pe_max=100,
    pe_ideal_low=10,
    pe_ideal_high=35,
    daily_change_min=0.0,
    daily_change_max=6.0,
    max_consecutive_up_days=3,
    require_volume_shrink=False,
    require_ma_bullish=False,  # MACD captures trend
    max_retracement_pct=0.8,  # Relaxed: golden cross often at inflection
    change_60d_min=-15.0,
    change_60d_max=50.0,
    volume_ratio_min=1.0,
)


# Registry for get_strategy_params (single source of truth)
_STRATEGY_PARAMS: Dict[str, StrategyParams] = {
    BUY_PULLBACK: BUY_PULLBACK_PARAMS,
    BREAKOUT: BREAKOUT_PARAMS,
    BOTTOM_REVERSAL: BOTTOM_REVERSAL_PARAMS,
    MACD_GOLDEN_CROSS: MACD_GOLDEN_CROSS_PARAMS,
}


def get_strategy_params(strategy_id: str) -> StrategyParams:
    """Get params for strategy. Falls back to buy_pullback for unknown."""
    return _STRATEGY_PARAMS.get(strategy_id, BUY_PULLBACK_PARAMS)


# Scorer functions by strategy (used in score_and_rank)
_SCORERS: Dict[str, Any] = {}


def _score_pe(pe: float, params: StrategyParams) -> float:
    """PE score: ideal 10, partial range 5, else 0 (for pullback/MACD)."""
    if params.pe_ideal_low < pe < params.pe_ideal_high:
        return 10.0
    if 5 < pe <= params.pe_ideal_low or params.pe_ideal_high <= pe < PE_SCORE_PARTIAL_MAX:
        return 5.0
    return 0.0


def _score_pe_simple(pe: float, params: StrategyParams) -> float:
    """PE score: ideal 10, else 5 (for breakout/bottom_reversal)."""
    return 10.0 if params.pe_ideal_low < pe < params.pe_ideal_high else 5.0


def _score_mid_cap(total_mv: float) -> float:
    """Mid-cap bonus: 50-500 yi."""
    return 5.0 if _MID_CAP_MIN < total_mv < _MID_CAP_MAX else 0.0


def parse_picker_strategies(value: Optional[str]) -> List[str]:
    """Parse PICKER_STRATEGIES env (comma-separated). Default: [buy_pullback]."""
    if not value or not value.strip():
        return DEFAULT_STRATEGIES.copy()
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    valid = [p for p in parts if p in ALL_STRATEGIES]
    return valid if valid else DEFAULT_STRATEGIES.copy()


def filter_momentum(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    """Apply strategy-specific momentum filter."""
    if "涨跌幅" in df.columns:
        pct = pd.to_numeric(df["涨跌幅"], errors="coerce")
        df = df[(pct >= params.daily_change_min) & (pct <= params.daily_change_max)]

    if "60日涨跌幅" in df.columns:
        pct60 = pd.to_numeric(df["60日涨跌幅"], errors="coerce")
        df = df[pct60 >= params.change_60d_min]
        if params.change_60d_max is not None:
            df = df[pct60 <= params.change_60d_max]

    return df


def filter_volume(
    df: pd.DataFrame,
    params: StrategyParams,
    turnover_min: float = TURNOVER_MIN_PCT,
    turnover_max: float = TURNOVER_MAX_PCT,
) -> pd.DataFrame:
    """Apply volume filter with strategy-specific volume_ratio_min."""
    if "量比" in df.columns:
        vr = pd.to_numeric(df["量比"], errors="coerce")
        df = df[vr > params.volume_ratio_min]

    if "换手率" in df.columns:
        tr = pd.to_numeric(df["换手率"], errors="coerce")
        df = df[(tr > turnover_min) & (tr < turnover_max)]

    if "成交额" in df.columns and "总市值" in df.columns:
        amt = pd.to_numeric(df["成交额"], errors="coerce")
        cap_yi = pd.to_numeric(df["总市值"], errors="coerce") / 1e8
        ok_small = (cap_yi < MARKET_CAP_TIER_YI) & (amt > AMOUNT_MIN_SMALL_CAP)
        ok_large = (cap_yi >= MARKET_CAP_TIER_YI) & (amt > AMOUNT_MIN_LARGE_CAP)
        df = df[ok_small | ok_large]
    elif "成交额" in df.columns:
        amt = pd.to_numeric(df["成交额"], errors="coerce")
        df = df[amt > AMOUNT_MIN_SMALL_CAP]

    return df


def score_buy_pullback(row: Dict[str, Any], params: StrategyParams) -> float:
    """Score for buy_pullback: trend + pullback + volume + PE."""
    pct_60d = float(row.get("60日涨跌幅", 0) or 0)
    change_pct = float(row.get("涨跌幅", 0) or 0)
    vol_ratio = float(row.get("量比", 0) or 0)
    turnover = float(row.get("换手率", 0) or 0)
    pe = float(row.get("市盈率-动态", 0) or 0)
    total_mv = float(row.get("总市值", 0) or 0)

    # Trend
    if pct_60d <= 0:
        trend = 0.0
    elif pct_60d <= TREND_DECAY_THRESHOLD_PCT:
        trend = min(pct_60d, 25.0)
    else:
        decay = 30 - (pct_60d - TREND_DECAY_THRESHOLD_PCT) * 0.5
        trend = max(0.0, decay)

    # Momentum (pullback preferred)
    if change_pct < -2:
        mom = -5.0
    elif -2 <= change_pct <= 1:
        mom = 20.0
    elif 1 < change_pct <= 3:
        mom = 15.0
    elif 3 < change_pct <= 5:
        mom = 8.0
    else:
        mom = max(0.0, 8.0 - (change_pct - 5) * 3)

    # Volume
    vol = 20.0 if 1.0 <= vol_ratio <= 3.0 else (15.0 if vol_ratio > 3.0 else (10.0 if vol_ratio > 0.8 else 0.0))
    # Turnover
    to = 10.0 if 2 <= turnover <= 8 else (5.0 if 1 <= turnover < 2 else (3.0 if 8 < turnover <= 15 else 0.0))
    pe_s = _score_pe(pe, params)
    mid = _score_mid_cap(total_mv)
    return trend + mom + vol + to + pe_s + mid


def score_breakout(row: Dict[str, Any], params: StrategyParams) -> float:
    """Score for breakout: volume + momentum + trend."""
    pct_60d = float(row.get("60日涨跌幅", 0) or 0)
    change_pct = float(row.get("涨跌幅", 0) or 0)
    vol_ratio = float(row.get("量比", 0) or 0)
    turnover = float(row.get("换手率", 0) or 0)
    pe = float(row.get("市盈率-动态", 0) or 0)
    total_mv = float(row.get("总市值", 0) or 0)

    # Higher change = better for breakout
    mom = min(25.0, change_pct * 3) if change_pct > 0 else 0.0
    # Volume critical
    vol = 25.0 if vol_ratio >= 2.0 else (15.0 if vol_ratio >= 1.5 else 5.0)
    # Trend: moderate positive preferred
    trend = min(15.0, max(0, pct_60d)) if 0 <= pct_60d <= 50 else 5.0
    to = 10.0 if 2 <= turnover <= 10 else 5.0
    pe_s = _score_pe_simple(pe, params)
    mid = _score_mid_cap(total_mv)
    return trend + mom + vol + to + pe_s + mid


def score_bottom_reversal(row: Dict[str, Any], params: StrategyParams) -> float:
    """Score for bottom reversal: less negative 60d + volume + stabilizing."""
    pct_60d = float(row.get("60日涨跌幅", 0) or 0)
    change_pct = float(row.get("涨跌幅", 0) or 0)
    vol_ratio = float(row.get("量比", 0) or 0)
    turnover = float(row.get("换手率", 0) or 0)
    pe = float(row.get("市盈率-动态", 0) or 0)
    total_mv = float(row.get("总市值", 0) or 0)

    # Prefer stocks that have stopped falling (60d not too negative, or improving)
    # -25% to 0: linear score 0-20; 0 to 10: 20-25
    if pct_60d >= 0:
        trend = min(25.0, 20.0 + pct_60d * 0.5)
    else:
        trend = max(0.0, 20.0 + pct_60d * 0.8)  # -25 -> 0, -10 -> 12

    # Slight up or flat preferred
    mom = 20.0 if 0 <= change_pct <= 3 else (15.0 if 3 < change_pct <= 5 else 10.0)
    vol = 20.0 if vol_ratio >= 1.5 else (10.0 if vol_ratio >= 1.2 else 0.0)
    to = 10.0 if 1 <= turnover <= 10 else 5.0
    pe_s = _score_pe_simple(pe, params)
    mid = _score_mid_cap(total_mv)
    return trend + mom + vol + to + pe_s + mid


def score_macd_golden_cross(row: Dict[str, Any], params: StrategyParams) -> float:
    """Score for MACD golden cross: volume + momentum + PE; trend neutral (MACD captures it)."""
    change_pct = float(row.get("涨跌幅", 0) or 0)
    vol_ratio = float(row.get("量比", 0) or 0)
    turnover = float(row.get("换手率", 0) or 0)
    pe = float(row.get("市盈率-动态", 0) or 0)
    total_mv = float(row.get("总市值", 0) or 0)

    # Moderate momentum preferred (0-6% daily)
    mom = 20.0 if 0 <= change_pct <= 3 else (15.0 if 3 < change_pct <= 5 else 10.0)
    vol = 20.0 if vol_ratio >= 1.5 else (10.0 if vol_ratio >= 1.0 else 0.0)
    to = 10.0 if 2 <= turnover <= 8 else (5.0 if 1 <= turnover < 2 else 0.0)
    pe_s = _score_pe(pe, params)
    mid = _score_mid_cap(total_mv)
    return mom + vol + to + pe_s + mid


# Populate _SCORERS after all scorers are defined
_SCORERS.update({
    BUY_PULLBACK: score_buy_pullback,
    BREAKOUT: score_breakout,
    BOTTOM_REVERSAL: score_bottom_reversal,
    MACD_GOLDEN_CROSS: score_macd_golden_cross,
})


def score_and_rank(
    df: pd.DataFrame,
    strategy_id: str,
    params: StrategyParams,
    top_n: int = 30,
) -> List[ScreenedStock]:
    """Score and rank by strategy, return top N with strategy tag."""
    scorer_fn = _SCORERS.get(strategy_id, score_buy_pullback)

    records: List[ScreenedStock] = []
    for _, row in df.iterrows():
        try:
            code = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            price = float(pd.to_numeric(row.get("最新价", 0), errors="coerce") or 0)
            change_pct = float(pd.to_numeric(row.get("涨跌幅", 0), errors="coerce") or 0)
            vol_ratio = float(pd.to_numeric(row.get("量比", 0), errors="coerce") or 0)
            turnover = float(pd.to_numeric(row.get("换手率", 0), errors="coerce") or 0)
            pe = float(pd.to_numeric(row.get("市盈率-动态", 0), errors="coerce") or 0)
            pb = float(pd.to_numeric(row.get("市净率", 0), errors="coerce") or 0)
            total_mv = float(pd.to_numeric(row.get("总市值", 0), errors="coerce") or 0)
            amount = float(pd.to_numeric(row.get("成交额", 0), errors="coerce") or 0)
            pct_60d = float(pd.to_numeric(row.get("60日涨跌幅", 0), errors="coerce") or 0)

            row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
            score = scorer_fn(row_dict, params)

            records.append(
                ScreenedStock(
                    code=code,
                    name=name,
                    price=price,
                    change_pct=change_pct,
                    volume_ratio=vol_ratio,
                    turnover_rate=turnover,
                    pe=pe,
                    pb=pb,
                    market_cap=total_mv / 1e8,
                    amount=amount / 1e8,
                    change_pct_60d=pct_60d,
                    score=score,
                    strategies=[strategy_id],
                )
            )
        except Exception:
            continue

    records.sort(key=lambda s: s.score, reverse=True)
    return records[:top_n]


def merge_candidates_by_code(candidates_per_strategy: Dict[str, List[ScreenedStock]]) -> List[ScreenedStock]:
    """Merge candidates from multiple strategies. Dedupe by code, union strategies, max score."""
    by_code: Dict[str, ScreenedStock] = {}
    for strategy_id, cands in candidates_per_strategy.items():
        for s in cands:
            existing = by_code.get(s.code)
            if existing is None:
                by_code[s.code] = ScreenedStock(
                    code=s.code,
                    name=s.name,
                    price=s.price,
                    change_pct=s.change_pct,
                    volume_ratio=s.volume_ratio,
                    turnover_rate=s.turnover_rate,
                    pe=s.pe,
                    pb=s.pb,
                    market_cap=s.market_cap,
                    amount=s.amount,
                    change_pct_60d=s.change_pct_60d,
                    score=max(s.score, 0),
                    strategies=[strategy_id],
                )
            else:
                strategies = getattr(existing, "strategies", []) or []
                if strategy_id not in strategies:
                    strategies = list(strategies) + [strategy_id]
                existing.score = max(existing.score, s.score)
                existing.strategies = strategies

    merged = list(by_code.values())
    merged.sort(key=lambda s: s.score, reverse=True)
    return merged
