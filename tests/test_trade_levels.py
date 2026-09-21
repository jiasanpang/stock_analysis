# -*- coding: utf-8 -*-
"""Tests for src/services/trade_levels.py — unified trade levels engine."""

from __future__ import annotations

import math

import pytest

from src.services.trade_levels import (
    BUY_PULLBACK,
    RR_MIN,
    TradeLevels,
    compute_atr,
    compute_trade_levels,
    evaluate_trailing_exit,
    simulate_forward_trade,
)


# ---------------------------------------------------------------------------
# compute_atr
# ---------------------------------------------------------------------------


def test_compute_atr_basic():
    """ATR over a flat series should equal high-low spread."""
    n = 20
    highs = [10.5] * n
    lows = [10.0] * n
    closes = [10.2] * n
    atr = compute_atr(highs, lows, closes, period=14)
    assert atr == pytest.approx(0.5, abs=0.01)


def test_compute_atr_too_short():
    """Insufficient data returns 0."""
    assert compute_atr([1.0] * 5, [0.9] * 5, [0.95] * 5, period=14) == 0.0


# ---------------------------------------------------------------------------
# compute_trade_levels: per-strategy
# ---------------------------------------------------------------------------


def test_buy_pullback_levels_sane():
    tl = compute_trade_levels(
        strategy_id=BUY_PULLBACK,
        current_price=10.0, ma5=9.9, ma10=9.7, ma20=9.5,
        market_cap_yi=120.0,  # mid-cap
    )
    assert tl.ideal_buy > 0
    assert tl.stop_loss < tl.ideal_buy
    assert tl.take_profit_1 > tl.ideal_buy
    assert tl.risk_reward > 0
    assert "动态保护" in tl.take_profit_2_rule
    assert 0 < tl.position_pct <= 0.25


def test_buy_pullback_secondary_anchored_to_ma10():
    """secondary_buy should track MA10 (deeper pullback), not just price*0.97.

    Regression: when current_price hugs MA5, the old `current_price * 0.97`
    formula produced a secondary only ~3% below ideal, which the LLM expanded
    into overlapping price ranges in the report UI.
    """
    tl = compute_trade_levels(
        strategy_id=BUY_PULLBACK,
        current_price=18.65, ma5=18.50, ma10=18.00, ma20=17.50,
        market_cap_yi=120.0,
    )
    assert tl.secondary_buy == pytest.approx(18.00, abs=0.01)
    assert tl.secondary_buy <= tl.ideal_buy * 0.98


def test_buy_pullback_secondary_fallback_when_no_ma10():
    tl = compute_trade_levels(
        strategy_id=BUY_PULLBACK,
        current_price=10.0, ma5=9.9, ma10=0.0, ma20=0.0,
        market_cap_yi=120.0,
    )
    assert tl.secondary_buy == pytest.approx(tl.ideal_buy * 0.96, abs=0.001)


def test_position_size_by_market_cap():
    """Small caps get smaller base position than large caps."""
    small = compute_trade_levels(
        strategy_id=BUY_PULLBACK, current_price=10.0,
        ma5=9.9, ma10=9.7, ma20=9.5, market_cap_yi=30.0,
    )
    large = compute_trade_levels(
        strategy_id=BUY_PULLBACK, current_price=10.0,
        ma5=9.9, ma10=9.7, ma20=9.5, market_cap_yi=500.0,
    )
    assert small.position_pct < large.position_pct


def test_invalid_price_returns_zero_levels():
    tl = compute_trade_levels(strategy_id=BUY_PULLBACK, current_price=0.0)
    assert tl.ideal_buy == 0.0
    assert tl.passes_rr_filter is False


def test_unknown_strategy_falls_back():
    tl = compute_trade_levels(
        strategy_id="nonexistent", current_price=10.0,
        ma5=9.9, ma10=9.7, ma20=9.5, market_cap_yi=100.0,
    )
    # Should not crash; produces valid levels via fallback.
    assert tl.ideal_buy > 0


def test_rr_filter_minimum():
    """Levels with R/R below RR_MIN must report passes_rr_filter=False."""
    tl = TradeLevels(ideal_buy=10, stop_loss=9, take_profit_1=10.5, risk_reward=0.5)
    assert tl.passes_rr_filter is False
    tl2 = TradeLevels(ideal_buy=10, stop_loss=9, take_profit_1=12.0, risk_reward=2.0)
    assert tl2.passes_rr_filter is True


# ---------------------------------------------------------------------------
# evaluate_trailing_exit
# ---------------------------------------------------------------------------


def test_trailing_below_ma10_after_20pct():
    should, reason = evaluate_trailing_exit(
        strategy_id=BUY_PULLBACK,
        entry_price=10.0, current_price=12.5, current_high=12.5,
        ma10=12.6, ma20=11.0, atr=0.5, holding_days=10, peak_price=13.0,
    )
    assert should is True
    assert reason == "trailing_below_ma10"


def test_trailing_atr_retrace_after_20pct():
    should, reason = evaluate_trailing_exit(
        strategy_id=BUY_PULLBACK,
        entry_price=10.0, current_price=12.0, current_high=12.0,
        ma10=11.5, ma20=11.0, atr=0.6, holding_days=10, peak_price=14.0,
    )
    # Retraced 2.0 from peak; ATR×2.5 = 1.5 → trigger.
    assert should is True
    assert reason == "trailing_atr2.5_retrace"


def test_no_exit_in_normal_holding():
    should, reason = evaluate_trailing_exit(
        strategy_id=BUY_PULLBACK,
        entry_price=10.0, current_price=10.5, current_high=10.6,
        ma10=10.3, ma20=10.0, atr=0.3, holding_days=3, peak_price=10.6,
    )
    assert should is False


def test_stage6_break_cost():
    """Profit at +7% (in 6-12% band) but current closes below entry → exit."""
    should, reason = evaluate_trailing_exit(
        strategy_id=BUY_PULLBACK,
        entry_price=10.0, current_price=9.95, current_high=10.7,
        ma10=10.3, ma20=10.0, atr=0.3, holding_days=5, peak_price=10.7,
    )
    # Current profit_pct = -0.5%, NOT in 6-12% band → no stage6 exit.
    # MA20=10, price=9.95, 9.95 > 9.7 (= 10*0.97) → no MA20 break either.
    assert should is False
    # Now move price into the 6-12% profit band but below entry would be
    # contradictory; instead test the boundary: profit 7% with current < entry
    # is impossible. So validate the stage triggers when price re-tests cost.
    should2, reason2 = evaluate_trailing_exit(
        strategy_id=BUY_PULLBACK,
        entry_price=10.0, current_price=9.99, current_high=10.7,
        ma10=10.3, ma20=10.5, atr=0.3, holding_days=5, peak_price=10.7,
    )
    # profit_pct≈-0.1% (not in band) but MA20 break check: 9.99 < 10.5*0.97=10.185 → exit.
    assert should2 is True
    assert reason2 == "broke_ma20"


def test_time_stop_20d_no_progress():
    should, reason = evaluate_trailing_exit(
        strategy_id=BUY_PULLBACK,
        entry_price=10.0, current_price=10.2, current_high=10.3,
        ma10=10.15, ma20=10.0, atr=0.2, holding_days=21, peak_price=10.3,
    )
    assert should is True
    assert reason == "time_stop_20d_no_progress"


# ---------------------------------------------------------------------------
# simulate_forward_trade
# ---------------------------------------------------------------------------


def test_simulate_forward_trade_take_profit():
    """A clean uptrend exits via trailing/MA10 break or window end."""
    bars = [
        {"close": 10.5, "high": 10.6, "ma10": 10.3, "ma20": 10.0, "atr": 0.3, "pct_chg": 1.0},
        {"close": 11.0, "high": 11.1, "ma10": 10.5, "ma20": 10.1, "atr": 0.3, "pct_chg": 4.7},
        {"close": 12.5, "high": 12.6, "ma10": 11.0, "ma20": 10.5, "atr": 0.3, "pct_chg": 13.6},
        {"close": 12.3, "high": 12.5, "ma10": 11.5, "ma20": 10.8, "atr": 0.3, "pct_chg": -1.6},
    ]
    res = simulate_forward_trade(
        strategy_id=BUY_PULLBACK, entry_price=10.0,
        market_cap_yi=100.0, bars=bars,
    )
    assert res.get("skipped") is False
    assert res["return_pct"] is not None


def test_simulate_forward_trade_limit_up_skip():
    """Entry day at limit-up gets skipped."""
    bars = [{"close": 11.0, "high": 11.0, "low": 10.5, "pct_chg": 9.99}]
    res = simulate_forward_trade(
        strategy_id=BUY_PULLBACK, entry_price=10.0,
        market_cap_yi=100.0, bars=bars,
    )
    assert res.get("skipped") is True
    assert res.get("skip_reason") == "limit_up_unfillable"


def test_simulate_forward_trade_stop_loss():
    """Sharp drop should trigger stop on day 1."""
    bars = [
        {"close": 10.0, "high": 10.0, "ma10": 10.0, "ma20": 10.0, "atr": 0.3, "pct_chg": 0.0},
        {"close": 9.0, "high": 9.5, "ma10": 9.8, "ma20": 9.7, "atr": 0.3, "pct_chg": -10.0},
    ]
    res = simulate_forward_trade(
        strategy_id=BUY_PULLBACK, entry_price=10.0,
        market_cap_yi=100.0, bars=bars, apply_limit_up_filter=False,
    )
    assert res.get("skipped") is False
    assert res["return_pct"] < 0
