# -*- coding: utf-8 -*-
"""
Picker strategies: each strategy has its own screening logic and fixed params.

Strategies: buy_pullback, bottom_reversal, reversal_breakout, small_cap
No intensity modes (defensive/balanced/offensive) — each strategy has one set of params.

Removed strategies (kept here for context — see docs/CHANGELOG.md):
- eod_buyback (May 2026): 2yr OOS PF=0.58, negative alpha vs CSI300 B&H.
- breakout    (May 2026): two-window backtest PF 0.71/0.78, total return
  -64% vs benchmark +24% in 2025-05~2026-05; A/B tuning saturated at PF<1.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from src.services.picker import (
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
from src.services.trade_levels import (
    RR_MIN,
    compute_trade_levels,
)

# Strategy IDs (used in config)
BUY_PULLBACK = "buy_pullback"
BOTTOM_REVERSAL = "bottom_reversal"
REVERSAL_BREAKOUT = "reversal_breakout"  # right-side breakout of a deep base
SMALL_CAP = "small_cap"  # cross-sectional smallest-market-cap factor

# Default strategy when PICKER_STRATEGIES not set
DEFAULT_STRATEGIES = [BUY_PULLBACK]

# All available strategies
ALL_STRATEGIES = [BUY_PULLBACK, BOTTOM_REVERSAL, REVERSAL_BREAKOUT, SMALL_CAP]

def is_mainboard_stock(code: str) -> bool:
    """Check if a stock is listed on the main board (SSE/SZSE main).

    Excludes: ChiNext (300xxx), STAR Market (688xxx), BSE (8xxxxx/4xxxxx).
    """
    c = (code or "").strip().split(".")[0]
    if c.startswith("688") or c.startswith("30"):
        return False  # ChiNext + STAR
    if c.startswith("8") or c.startswith("4"):
        return False  # BSE
    if c.startswith("6") or c.startswith("00") or c.startswith("001") or c.startswith("002") or c.startswith("003"):
        return True  # SSE main + SZSE main
    return False


# Mid-cap bonus: 50-500 yi (亿) market cap
_MID_CAP_MIN = 50e8
_MID_CAP_MAX = 500e8

STRATEGY_DISPLAY_NAMES: Dict[str, str] = {
    BUY_PULLBACK: "买回踩",
    BOTTOM_REVERSAL: "底部反转",
    REVERSAL_BREAKOUT: "反转突破",
    SMALL_CAP: "小市值",
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
    # Momentum / entry range (None = skip filter)
    daily_change_min: Optional[float] = None
    daily_change_max: Optional[float] = None
    # Post-filters
    max_consecutive_up_days: int = 3
    require_volume_shrink: bool = False
    require_ma_bullish: bool = False
    max_retracement_pct: float = 0.5
    # Strategy-specific
    change_60d_min: Optional[float] = None  # min 60d change (None = skip)
    change_60d_max: Optional[float] = None  # max 60d change (None = no cap)
    volume_ratio_min: Optional[float] = VOLUME_RATIO_MIN  # None = skip filter
    # Turnover rate filter (None = skip filter)
    turnover_rate_min: Optional[float] = TURNOVER_MIN_PCT
    turnover_rate_max: Optional[float] = TURNOVER_MAX_PCT
    # Market cap filter (in 亿)
    market_cap_min: Optional[float] = None  # None = no minimum
    market_cap_max: Optional[float] = None  # None = no maximum
    # Leader bias exemption: 0=off; when >0, qualified leaders can pass with bias up to this %
    leader_bias_exempt_pct: float = 0.0
    # Min % below N-day high to qualify as pullback (0=disabled)
    min_pullback_from_high_pct: float = 0.0
    max_distance_above_ma10_pct: float = 0.0  # Max % price can be above MA10 (0=disabled); ensures near support
    require_price_above_ma20: bool = False     # Reject if price below MA20 (entering downtrend)


# Buy pullback: 60d > 5%, MA bullish, shrinking-volume pullback entry
BUY_PULLBACK_PARAMS = StrategyParams(
    max_bias_pct=5.0,                # No chasing: strict bias ceiling
    leader_bias_exempt_pct=0.0,      # No exemption: buy pullback = strict on bias
    pe_max=60,                       # Conservative valuation filter
    pe_ideal_low=10,                 # Lower bound of ideal PE range
    pe_ideal_high=30,                # Tighter ideal PE ceiling
    daily_change_min=-2.0,           # Precise pullback range: mild dip
    daily_change_max=2.0,            # Prevent chasing intraday rallies
    max_consecutive_up_days=3,       # Normal consolidation after 2-3 day rally
    require_volume_shrink=True,      # Core: healthy pullback = shrinking volume
    require_ma_bullish=True,         # MA alignment confirms uptrend
    max_retracement_pct=0.4,         # Stricter retracement limit
    change_60d_min=8.0,              # Require established uptrend (raised 5→8: weak
                                      # 5% trends often fail at pullback support)
    change_60d_max=65.0,             # Don't truncate momentum tail: 6-month
                                      # grid search (min∈{5,8,12} × max∈{35,50,65})
                                      # showed max=65 strictly dominates max=50
                                      # (+41.9% vs +39.8% total return, same MDD,
                                      # higher win rate). max=35 destroys alpha
                                      # entirely (~0% return). Pullbacks in
                                      # high-momentum names (60d 50-65%) have
                                      # the highest payoff in this dataset.
    volume_ratio_min=0.7,            # Allow shrinking volume to pass
    min_pullback_from_high_pct=2.0,  # 1-3% pullback is valid entry
    max_distance_above_ma10_pct=5.0,  # Wider support zone tolerance
    require_price_above_ma20=True,     # Below MA20 = downtrend, reject
)

# Bottom reversal: 60d -25% ~ -5%, true bottom with volume shrink stabilisation
BOTTOM_REVERSAL_PARAMS = StrategyParams(
    max_bias_pct=6.0,  # Stricter (near support)
    leader_bias_exempt_pct=0.0,  # No exemption: bottom stocks are not leaders
    pe_max=60,   # Tightened 100→60: reversal strategy is mean-reversion bet on
                 # oversold names; PE>60 with 15-20% drawdown is rarely a true
                 # "low base" — historical win rate <30% in this band.
    pe_ideal_low=8,
    pe_ideal_high=35,
    daily_change_min=1.0,  # Must be rising today — confirms reversal signal
    daily_change_max=5.0,
    max_consecutive_up_days=3,
    require_volume_shrink=True,  # Bottom must show volume contraction stabilisation
    require_ma_bullish=False,  # Bottom stocks often not MA bullish
    max_retracement_pct=0.618,  # Fibonacci 61.8% B-wave rebound filter
    change_60d_min=-25.0,
    change_60d_max=-10.0,  # Tightened -8→-10: drops shallower than -10% are usually
                           # noise/pullbacks rather than true reversals — filter them out
                           # to avoid pseudo-reversal candidates.
    volume_ratio_min=0.7,  # Allow low volume ratio (stabilisation = shrinking volume)
    # Cap market cap: oversold reversal works best on small/mid caps;
    # large caps (>300亿) rebound slower and dilute the bet.
    market_cap_max=300.0,
)

# Registry for get_strategy_params (single source of truth)
_STRATEGY_PARAMS: Dict[str, StrategyParams] = {
    BUY_PULLBACK: BUY_PULLBACK_PARAMS,
    BOTTOM_REVERSAL: BOTTOM_REVERSAL_PARAMS,
}


def get_strategy_params(strategy_id: str) -> StrategyParams:
    """Get params for strategy. Falls back to buy_pullback for unknown."""
    return _STRATEGY_PARAMS.get(strategy_id, BUY_PULLBACK_PARAMS)


# Scorer functions by strategy (used in score_and_rank)
_SCORERS: Dict[str, Any] = {}


def _score_pe(pe: float, params: StrategyParams) -> float:
    """PE score: ideal 10, partial range 5, else 0 (for pullback)."""
    if params.pe_ideal_low < pe < params.pe_ideal_high:
        return 10.0
    if 5 < pe <= params.pe_ideal_low or params.pe_ideal_high <= pe < PE_SCORE_PARTIAL_MAX:
        return 5.0
    return 0.0


def _score_pe_simple(pe: float, params: StrategyParams) -> float:
    """PE score: ideal 10, else 5 (for bottom_reversal)."""
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
    """Apply strategy-specific momentum filter.

    When a param is None the corresponding condition is skipped entirely.
    """
    # Per-strategy PE ceiling (shared basic filter only enforces pe<100;
    # tighter strategies like bottom_reversal need their own pe_max applied).
    if "市盈率-动态" in df.columns and params.pe_max is not None:
        pe = pd.to_numeric(df["市盈率-动态"], errors="coerce")
        df = df[(pe > 0) & (pe < params.pe_max)]

    if "涨跌幅" in df.columns:
        pct = pd.to_numeric(df["涨跌幅"], errors="coerce")
        if params.daily_change_min is not None:
            df = df[pct >= params.daily_change_min]
        if params.daily_change_max is not None:
            df = df[pct <= params.daily_change_max]

    if "60日涨跌幅" in df.columns:
        pct60 = pd.to_numeric(df["60日涨跌幅"], errors="coerce")
        if params.change_60d_min is not None:
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
    """Apply volume filter with strategy-specific settings.

    When a param is None the corresponding condition is skipped entirely.

    Priority:
    1. Use params.turnover_rate_min/max if explicitly set in strategy
    2. Fall back to passed-in turnover_min/max
    3. Use params.market_cap_min/max if set, otherwise apply default amount filter
    """
    if "量比" in df.columns and params.volume_ratio_min is not None:
        vr = pd.to_numeric(df["量比"], errors="coerce")
        df = df[vr > params.volume_ratio_min]

    # Turnover rate filter: use params if set, else use passed-in defaults
    if "换手率" in df.columns:
        tr_min = getattr(params, 'turnover_rate_min', None)
        tr_max = getattr(params, 'turnover_rate_max', None)
        # Only apply turnover filter when at least one bound is defined
        if tr_min is not None or tr_max is not None:
            tr = pd.to_numeric(df["换手率"], errors="coerce")
            # Fall back to passed-in defaults when strategy value is None
            effective_min = tr_min if tr_min is not None else turnover_min
            effective_max = tr_max if tr_max is not None else turnover_max
            df = df[(tr >= effective_min) & (tr <= effective_max)]

    # Market cap + amount filter
    if "总市值" in df.columns:
        cap_yi = pd.to_numeric(df["总市值"], errors="coerce") / 1e8
        
        # If strategy defines market_cap_min/max, use those
        market_cap_min = getattr(params, 'market_cap_min', None)
        market_cap_max = getattr(params, 'market_cap_max', None)
        
        if market_cap_min is not None or market_cap_max is not None:
            # Strategy-specific market cap range
            mask = pd.Series([True] * len(cap_yi), index=cap_yi.index)
            if market_cap_min is not None:
                mask = mask & (cap_yi >= market_cap_min)
            if market_cap_max is not None:
                mask = mask & (cap_yi <= market_cap_max)
            df = df[mask]
        else:
            # Default: use amount-based filter
            if "成交额" in df.columns:
                amt = pd.to_numeric(df["成交额"], errors="coerce")
                ok_small = (cap_yi < MARKET_CAP_TIER_YI) & (amt > AMOUNT_MIN_SMALL_CAP)
                ok_large = (cap_yi >= MARKET_CAP_TIER_YI) & (amt > AMOUNT_MIN_LARGE_CAP)
                df = df[ok_small | ok_large]
    elif "成交额" in df.columns:
        amt = pd.to_numeric(df["成交额"], errors="coerce")
        df = df[amt > AMOUNT_MIN_SMALL_CAP]

    return df


def score_buy_pullback(row: Dict[str, Any], params: StrategyParams) -> float:
    """Score for buy_pullback: trend + pullback + volume + PE.

    Scoring components:
        1. Trend (60d): established uptrend rewarded, decay above threshold  (0-30)
        2. Momentum: deeper pullback preferred for ideal entry              (0-20)
        3. Volume: shrinking volume rewarded (0.5-0.9 highest)              (0-25)
        4. Turnover                                                         (0-10)
        5. PE                                                               (0-10)
        6. Mid-cap bonus                                                    (0-5)
    """
    pct_60d = float(row.get("60日涨跌幅", 0) or 0)
    change_pct = float(row.get("涨跌幅", 0) or 0)
    vol_ratio = float(row.get("量比", 0) or 0)
    turnover = float(row.get("换手率", 0) or 0)
    pe = float(row.get("市盈率-动态", 0) or 0)
    total_mv = float(row.get("总市值", 0) or 0)

    # --- 1. Trend (60d) ---
    if pct_60d <= 0:
        trend = 0.0
    elif pct_60d <= TREND_DECAY_THRESHOLD_PCT:
        trend = min(pct_60d, 25.0)
    else:
        decay = 30 - (pct_60d - TREND_DECAY_THRESHOLD_PCT) * 0.5
        trend = max(0.0, decay)

    # --- 2. Momentum: deeper pullback preferred for buy_pullback ---
    if change_pct < -2:
        mom = 0.0           # Below filter threshold, won't appear
    elif -2 <= change_pct < -1:
        mom = 20.0          # Best entry within allowed pullback range
    elif -1 <= change_pct <= 1:
        mom = 18.0          # Shallow pullback/flat, good
    elif 1 < change_pct <= 2:
        mom = 10.0          # Small rise, acceptable
    else:
        mom = 0.0           # Beyond filter range

    # --- 3. Volume: reward shrinking volume (healthy pullback signal) ---
    if 0.5 <= vol_ratio <= 0.9:
        vol = 25.0  # Shrinking volume = best pullback signal
    elif 0.9 < vol_ratio <= 1.5:
        vol = 20.0  # Normal to slight expansion, acceptable
    elif 1.5 < vol_ratio <= 3.0:
        vol = 15.0  # Moderate expansion, some risk
    elif vol_ratio > 3.0:
        vol = 5.0   # Heavy volume, potential distribution
    elif vol_ratio >= 0.3:
        vol = 15.0  # Very low volume, thinly traded
    else:
        vol = 0.0   # No volume data

    # --- 4. Turnover ---
    to = 10.0 if 2 <= turnover <= 8 else (5.0 if 1 <= turnover < 2 else (3.0 if 8 < turnover <= 15 else 0.0))
    pe_s = _score_pe(pe, params)
    mid = _score_mid_cap(total_mv)
    return trend + mom + vol + to + pe_s + mid


def score_bottom_reversal(row: Dict[str, Any], params: StrategyParams) -> float:
    """Score for bottom reversal: deep 60d decline + volume transition + reversal candle.

    Scoring components:
        1. 60d decline depth: deeper decline = higher value at bottom  (0-25)
        2. Momentum: slight up preferred (daily_change_min=1.0 enforced) (10-20)
        3. Volume transition: shrink-then-expand breakout signal       (0-25)
        4. Turnover                                                    (5-10)
        5. PE simple                                                   (5-10)
        6. Mid-cap bonus                                               (0-5)
        7. Reversal candle pattern (bullish body / long lower shadow)  (0-10)
    """
    pct_60d = float(row.get("60日涨跌幅", 0) or 0)
    change_pct = float(row.get("涨跌幅", 0) or 0)
    vol_ratio = float(row.get("量比", 0) or 0)
    turnover = float(row.get("换手率", 0) or 0)
    pe = float(row.get("市盈率-动态", 0) or 0)
    total_mv = float(row.get("总市值", 0) or 0)

    # --- 1. 60d decline depth scoring ---
    # Deeper decline (-30% ~ -20%) = higher bottom value
    if pct_60d >= 0:
        trend = min(25.0, 20.0 + pct_60d * 0.5)
    elif pct_60d >= -10:
        # -10% ~ 0%: moderate value
        trend = max(0.0, 20.0 + pct_60d * 0.8)  # -10 -> 12, 0 -> 20
    elif pct_60d >= -30:
        # -30% ~ -10%: deep bottom bonus — deeper = more value
        trend = 15.0 + (-pct_60d - 10) * 0.5  # -10 -> 15, -20 -> 20, -30 -> 25
    else:
        # Below -30%: cap at 25 (diminishing returns at extreme decline)
        trend = 25.0

    # --- 2. Momentum: slight up preferred (daily_change_min=1.0 enforced) ---
    mom = 20.0 if 0 <= change_pct <= 3 else (15.0 if 3 < change_pct <= 5 else 10.0)

    # --- 3. Volume transition: shrink-to-expand breakout signal ---
    # Today's vol_ratio > 1.2 on a stabilising day = volume expansion after contraction
    # (The 5d avg vol_ratio check requires daily data; here we use row-level proxy)
    if vol_ratio > 1.5:
        vol = 25.0  # Strong volume expansion — breakout signal
    elif vol_ratio > 1.2:
        vol = 20.0  # Moderate expansion — potential breakout
    elif vol_ratio >= 0.7:
        vol = 10.0  # Low volume — still in contraction phase, acceptable
    else:
        vol = 0.0

    # --- 4. Turnover ---
    to = 10.0 if 1 <= turnover <= 10 else 5.0

    # --- 5. PE ---
    pe_s = _score_pe_simple(pe, params)

    # --- 6. Mid-cap bonus ---
    mid = _score_mid_cap(total_mv)

    # --- 7. Reversal candle pattern ---
    candle_bonus = 0.0
    open_price = float(row.get("今开", 0) or row.get("开盘", 0) or 0)
    close_price = float(row.get("最新价", 0) or row.get("收盘", 0) or 0)
    low_price = float(row.get("最低", 0) or 0)
    if open_price > 0 and close_price > 0:
        # Bullish candle: close > open
        if close_price > open_price:
            candle_bonus += 5.0
        # Long lower shadow: (open - low) > (close - open) indicates buying at bottom
        body = abs(close_price - open_price)
        lower_shadow = min(open_price, close_price) - low_price if low_price > 0 else 0
        if lower_shadow > body and lower_shadow > 0:
            candle_bonus += 5.0

    return trend + mom + vol + to + pe_s + mid + candle_bonus


# Populate _SCORERS after all scorers are defined
_SCORERS.update({
    BUY_PULLBACK: score_buy_pullback,
    BOTTOM_REVERSAL: score_bottom_reversal,
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

            # Compute unified trade levels (best-effort; missing MA → fallback inside)
            ma5 = float(pd.to_numeric(row.get("MA5", row.get("ma5", 0)), errors="coerce") or 0)
            ma10 = float(pd.to_numeric(row.get("MA10", row.get("ma10", 0)), errors="coerce") or 0)
            ma20 = float(pd.to_numeric(row.get("MA20", row.get("ma20", 0)), errors="coerce") or 0)
            atr = float(pd.to_numeric(row.get("ATR_20", row.get("atr_20", 0)), errors="coerce") or 0)
            tl = compute_trade_levels(
                code=code, strategy_id=strategy_id,
                current_price=price, ma5=ma5, ma10=ma10, ma20=ma20, atr=atr,
                market_cap_yi=total_mv / 1e8 if total_mv else 0.0,
            )

            # R/R hard filter: drop candidates with risk-reward below RR_MIN.
            # Skip filter when MA inputs unavailable (tl.ideal_buy=0 means the
            # engine couldn't compute meaningful levels — keep the row so the
            # AI/realtime layer can still evaluate).
            if tl.ideal_buy > 0 and tl.risk_reward > 0 and tl.risk_reward < RR_MIN:
                continue

            industry = str(row.get("industry") or row.get("行业") or "").strip()
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
                    industry=industry,
                    ideal_buy=tl.ideal_buy,
                    stop_loss=tl.stop_loss,
                    take_profit_1=tl.take_profit_1,
                    take_profit_2_rule=tl.take_profit_2_rule,
                    position_pct=tl.position_pct,
                    risk_reward=tl.risk_reward,
                )
            )
        except Exception:
            continue

    records.sort(key=lambda s: s.score, reverse=True)
    return records[:top_n]


def merge_candidates_by_code(candidates_per_strategy: Dict[str, List[ScreenedStock]]) -> List[ScreenedStock]:
    """Merge candidates from multiple strategies.

    - Dedupe by code, union strategies list.
    - Score = max single score across strategies + multi-strategy resonance bonus
      (2 strategies hit: +10; 3+ strategies hit: +20). Triple resonance also
      flags resonance="triple" so AI selection layer can mandatory-include them.
    - Trade levels: prefer the entry from the strategy that flagged it first
      (already populated on each candidate); on conflict pick the highest R/R
      so the merged record reflects the best risk-adjusted setup.
    """
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
                    industry=s.industry,
                    ideal_buy=s.ideal_buy,
                    secondary_buy=getattr(s, "secondary_buy", 0.0),
                    stop_loss=s.stop_loss,
                    take_profit_1=s.take_profit_1,
                    take_profit_2_rule=s.take_profit_2_rule,
                    position_pct=s.position_pct,
                    risk_reward=s.risk_reward,
                    limit_up_date=getattr(s, "limit_up_date", ""),
                    setup=getattr(s, "setup", ""),
                )
            else:
                strategies = getattr(existing, "strategies", []) or []
                if strategy_id not in strategies:
                    strategies = list(strategies) + [strategy_id]
                existing.score = max(existing.score, s.score)
                existing.strategies = strategies
                # Prefer the trade levels with the higher R/R (best setup).
                if s.risk_reward > existing.risk_reward and s.ideal_buy > 0:
                    existing.ideal_buy = s.ideal_buy
                    existing.stop_loss = s.stop_loss
                    existing.take_profit_1 = s.take_profit_1
                    existing.take_profit_2_rule = s.take_profit_2_rule
                    existing.position_pct = s.position_pct
                    existing.risk_reward = s.risk_reward

    # Multi-strategy resonance bonus.
    # Tuned: double +8 (was +10), triple +25 (was +20). Triple-resonance is
    # rare and historically much higher win rate, so widen the gap.
    for s in by_code.values():
        n_strategies = len(s.strategies or [])
        if n_strategies >= 3:
            s.score += 25.0
            s.resonance = "triple"
        elif n_strategies == 2:
            s.score += 8.0
            s.resonance = "double"

    merged = list(by_code.values())
    merged.sort(key=lambda s: s.score, reverse=True)
    return merged
