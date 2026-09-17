# -*- coding: utf-8 -*-
"""Limit-up pullback exit rules: staged take-profit + two-leg entries.

Sister module to ``trade_levels`` (kept separate for the 800-line rule).
Implements the 铁律 from the 涨停回踩 playbook that the boolean
``evaluate_trailing_exit`` cannot express:

  - +10% 分批止盈: sell HALF, raise the stop on the rest to cost.
  - 剩余仓位跟踪: close < MA10, or ATR×2.5 retrace from the peak → 清仓.
  - 回吐防护: after the trim, giving back below +5% → 清仓.
  - 时间止损: 5 sessions without >= +5%, hard ceiling at 7 sessions.
  - 趋势走坏: close < MA20 × 0.97 → 清仓.

``simulate_limit_up_pullback_trade`` replays those rules bar-by-bar for
the backtest, and is the first consumer of ``secondary_buy`` (deeper
pullback add-on leg) in a P&L simulation.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from src.services.trade_levels import (
    DEFAULT_SLIPPAGE_PCT,
    LIMIT_UP_KCCY,
    LIMIT_UP_MAIN,
)

_EXIT_DEFAULTS = {
    "TRIM_PCT": 10.0,       # +10% → sell half
    "GIVEBACK_PCT": 5.0,    # post-trim profit below +5% → clear
    "TIME_STOP_DAYS": 3,    # 3 天持仓风格: 无 +5% 即走
    "TIME_STOP_MIN_PCT": 5.0,
    "MAX_HOLD_DAYS": 3,
    "ATR_TRAIL_MUL": 2.5,
}


def lup_exit_enabled() -> bool:
    return os.environ.get("LUP_EXIT", "1") == "1"


def _ef(key: str) -> float:
    raw = os.environ.get(f"LUP_EXIT_{key}")
    if raw is None:
        return float(_EXIT_DEFAULTS[key])
    try:
        return float(raw)
    except ValueError:
        return float(_EXIT_DEFAULTS[key])


def evaluate_limit_up_pullback_exit(
    *,
    entry_price: float,
    current_price: float,
    ma10: float = 0.0,
    ma20: float = 0.0,
    atr: float = 0.0,
    holding_days: int = 0,
    peak_price: Optional[float] = None,
    trimmed: bool = False,
) -> Tuple[str, str]:
    """Return (action, reason); action ∈ {"hold", "trim", "exit"}.

    ``entry_price`` must already include slippage on both legs when the
    add-on filled; ``trimmed`` tells whether the +10% half-off happened.
    Returns ("hold", "invalid_input") on bad prices.
    """
    if entry_price <= 0 or current_price <= 0:
        return "hold", "invalid_input"
    profit_pct = (current_price - entry_price) / entry_price * 100.0
    peak = peak_price if (peak_price and peak_price > current_price) else current_price

    if ma20 > 0 and current_price < ma20 * 0.97:
        return "exit", "broke_ma20_3pct"
    if trimmed:
        if current_price < entry_price:
            return "exit", "post_trim_break_cost"
        if profit_pct < _ef("GIVEBACK_PCT"):
            return "exit", "post_trim_giveback"
        if ma10 > 0 and current_price < ma10:
            return "exit", "trail_below_ma10"
        if atr > 0 and (peak - current_price) >= atr * _ef("ATR_TRAIL_MUL"):
            return "exit", "trail_atr2.5_retrace"
    else:
        if profit_pct >= _ef("TRIM_PCT"):
            return "trim", "tp_half_+10pct"
    if (holding_days >= int(_ef("TIME_STOP_DAYS"))
            and profit_pct < _ef("TIME_STOP_MIN_PCT")):
        return "exit", f"time_stop_{int(_ef('TIME_STOP_DAYS'))}d_no_progress"
    if holding_days >= int(_ef("MAX_HOLD_DAYS")):
        return "exit", f"time_stop_{int(_ef('MAX_HOLD_DAYS'))}d_max_hold"
    return "hold", ""


def simulate_limit_up_pullback_trade(
    *,
    entry_price: float,
    stop_price: float,
    secondary_buy: float = 0.0,
    bars: List[Dict[str, float]],
    apply_slippage: bool = True,
    apply_limit_up_filter: bool = True,
    is_kc_cy: bool = False,
) -> Dict[str, Any]:
    """Forward simulation with two-leg entry and the half-off trim.

    Capital model: base leg = 4 units at ``entry_price``; optional add-on
    leg = 2 units at ``secondary_buy`` (fills only on a later bar whose low
    pierces it, before any trim). The trim sells 50% of live shares; the
    exit liquidates the rest at that bar's close.

    ``stop_price`` is the structural/amplitude stop produced by
    ``compute_limit_up_pullback_levels`` — an intraday pierce exits at the
    stop price (gap-downs fill at the open). Returns the same dict shape
    as ``trade_levels.simulate_forward_trade``.
    """
    slip = DEFAULT_SLIPPAGE_PCT / 100.0 if apply_slippage else 0.0
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
        return {"skipped": True, "skip_reason": "invalid_entry_or_stop"}
    if apply_limit_up_filter and bars:
        entry_pct = bars[0].get("pct_chg")
        limit_pct = LIMIT_UP_KCCY if is_kc_cy else LIMIT_UP_MAIN
        if entry_pct is not None and float(entry_pct) >= limit_pct:
            return {"skipped": True, "skip_reason": "limit_up_unfillable"}

    base_entry = entry_price * (1 + slip)
    units = 4.0
    cost = units * base_entry
    avg_cost = base_entry
    shares = units
    added = False
    trimmed = False
    proceeds = 0.0
    peak = base_entry

    for i, bar in enumerate(bars):
        close = float(bar.get("close") or 0.0)
        high = float(bar.get("high") or close)
        low = float(bar.get("low") or close)
        open_ = float(bar.get("open") or close)
        if close <= 0:
            continue
        peak = max(peak, high)

        # Gap-down through the stop fills at the open, else at the stop.
        if low <= stop_price:
            fill = min(open_, stop_price) * (1 - slip)
            proceeds += shares * fill
            ret = (proceeds - cost) / cost * 100.0
            return {"skipped": False, "exit_price": fill,
                    "exit_reason": "structural_stop", "return_pct": ret,
                    "hold_days": i + 1, "added_leg": added, "trimmed": trimmed}

        # Second leg: deeper pullback add-on (never on the entry bar).
        if (not added and secondary_buy and secondary_buy > stop_price
                and i >= 1 and low <= secondary_buy):
            fill2 = min(open_, secondary_buy) * (1 + slip)
            units2 = 2.0
            cost += units2 * fill2
            shares += units2
            avg_cost = cost / shares
            added = True

        action, reason = evaluate_limit_up_pullback_exit(
            entry_price=avg_cost, current_price=close,
            ma10=float(bar.get("ma10") or 0.0),
            ma20=float(bar.get("ma20") or 0.0),
            atr=float(bar.get("atr") or 0.0),
            holding_days=i + 1, peak_price=peak, trimmed=trimmed,
        )
        if action == "trim":
            sell = shares * 0.5
            proceeds += sell * close * (1 - slip)
            shares -= sell
            trimmed = True
        elif action == "exit":
            fill = close * (1 - slip)
            proceeds += shares * fill
            ret = (proceeds - cost) / cost * 100.0
            return {"skipped": False, "exit_price": fill,
                    "exit_reason": reason, "return_pct": ret,
                    "hold_days": i + 1, "added_leg": added, "trimmed": trimmed}

    if not bars:
        return {"skipped": True, "skip_reason": "no_bars"}
    last_close = float(bars[-1].get("close") or 0.0)
    if last_close <= 0:
        return {"skipped": True, "skip_reason": "invalid_exit_price"}
    proceeds += shares * last_close * (1 - slip)
    ret = (proceeds - cost) / cost * 100.0
    return {"skipped": False, "exit_price": last_close * (1 - slip),
            "exit_reason": "window_end", "return_pct": ret,
            "hold_days": len(bars), "added_leg": added, "trimmed": trimmed}
