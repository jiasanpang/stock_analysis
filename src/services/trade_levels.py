# -*- coding: utf-8 -*-
"""
Unified Trade Levels Engine
============================

Single source of truth for entry/stop/target levels across:
- Stock picker (quantitative screening output)
- AI analyzer (decision dashboard battle_plan)
- Backtest engines (forward return simulation)

Design principles:
1. Numerical layer is owned by code, not LLM. LLM only explains, never invents
   stop/take-profit numbers.
2. Strategy-aware: each strategy (buy_pullback / bottom_reversal / reversal_breakout)
   has its own level computation logic.
3. ATR-based trailing stop replaces fixed-percentage take-profit ceilings to
   let winners run while protecting profits.
4. Risk/Reward (R/R) is always computed; callers can hard-filter R/R < 1.8.

Public API:
- compute_trade_levels(...)        -> TradeLevels
- evaluate_trailing_exit(...)      -> (should_exit: bool, reason: str)
- compute_atr(...)                 -> float (14-day ATR helper)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Strategy IDs (mirrored from picker_strategies for cross-import safety)
BUY_PULLBACK = "buy_pullback"
BOTTOM_REVERSAL = "bottom_reversal"
REVERSAL_BREAKOUT = "reversal_breakout"

# Risk/Reward floor: candidates below this should be filtered by callers.
# Tuned to 2.0 (was 1.8): A-share round-trip cost (slippage + tax + commission)
# eats ~0.5% so net R/R floor is ~1.7; 2.0 net ≥ 1.7 keeps positive expectancy
# at win rates >= 45%.
RR_MIN = 2.0

# Slippage assumed by backtest (one-way, percent of price).
DEFAULT_SLIPPAGE_PCT = 0.3
# A-share frictions: commission + stamp duty.
# - 佣金 0.025% 双边（很多券商最低 5 元，对回测中性买卖建模 0.025% 即可）
# - 印花税 0.05% 单边卖出（2023-08 财政部下调后口径）
DEFAULT_COMMISSION_PCT = 0.025      # bps each side
DEFAULT_STAMP_DUTY_SELL_PCT = 0.05  # bps on sell only
# Round-trip cost (excl. slippage): commission*2 + stamp_duty = 0.10 %

# Limit-up percentage thresholds (used by backtest entry filter).
LIMIT_UP_MAIN = 9.8
LIMIT_UP_KCCY = 19.8


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class TradeLevels:
    """Computed trade levels for a single (stock, strategy, snapshot)."""

    ideal_buy: float = 0.0
    secondary_buy: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2_rule: str = ""
    expected_target: float = 0.0   # Expected average exit (used for R/R), accounts for trailing
    position_pct: float = 0.0      # 0.0 ~ 0.25 (0 = no position recommended)
    risk_reward: float = 0.0       # (expected_target - ideal_buy) / (ideal_buy - stop_loss)
    stage_rules: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def passes_rr_filter(self) -> bool:
        """True iff R/R >= RR_MIN (caller's hard filter)."""
        return self.risk_reward >= RR_MIN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ideal_buy": round(self.ideal_buy, 3),
            "secondary_buy": round(self.secondary_buy, 3),
            "stop_loss": round(self.stop_loss, 3),
            "take_profit_1": round(self.take_profit_1, 3),
            "take_profit_2_rule": self.take_profit_2_rule,
            "expected_target": round(self.expected_target, 3),
            "position_pct": round(self.position_pct, 3),
            "risk_reward": round(self.risk_reward, 2),
            "stage_rules": self.stage_rules,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# ATR helper
# ---------------------------------------------------------------------------


def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Compute simple-mean ATR over the last `period` bars.

    Returns 0.0 when input is too short or contains invalid data.
    Prefer reusing the ATR_20 column from data_provider when available; this
    helper is a fallback for callers that only have raw OHLC arrays.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return 0.0
    trs: List[float] = []
    for i in range(n - period, n):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        if not math.isnan(tr):
            trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs) / len(trs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_pos(*values: Optional[float]) -> bool:
    """All inputs are positive finite numbers."""
    for v in values:
        if v is None or not isinstance(v, (int, float)):
            return False
        if math.isnan(v) or v <= 0:
            return False
    return True


def _market_cap_band(market_cap_yi: float) -> str:
    """Return 'small' (<50亿), 'mid' (50-300亿), 'large' (>300亿)."""
    if market_cap_yi <= 0:
        return "mid"
    if market_cap_yi < 50:
        return "small"
    if market_cap_yi <= 300:
        return "mid"
    return "large"


def _base_position_pct(market_cap_yi: float) -> float:
    """Base position by market cap; refined later by R/R.

    Tightened from (6/12/18) to (5/12/15) to cap single-name blow-up risk —
    A-share idiosyncratic events (业绩雷/立案/质押爆仓) make 18% large-cap
    bets too concentrated even before R/R boost.
    """
    band = _market_cap_band(market_cap_yi)
    if band == "small":
        return 0.05   # 5% baseline (was 6%): small caps are illiquid + ATR-heavy
    if band == "mid":
        return 0.12   # 12% baseline (sweet spot)
    return 0.15       # 15% baseline (was 18%): cap concentration risk


def _stop_loss_pct_for_band(market_cap_yi: float) -> float:
    """Differentiated absolute stop-loss percent by market cap (positive value)."""
    band = _market_cap_band(market_cap_yi)
    if band == "small":
        return 0.06   # -6% (was -5%): small caps need wider stops vs intraday noise
    if band == "mid":
        return 0.06   # -6%
    return 0.07       # -7%


def _adjust_position_by_rr(base_pct: float, rr: float) -> float:
    """Refine position by R/R: high R/R earns boost, but with tighter cap.

    Tuned: boost factor 1.30 (was 1.20), cap 22% (was 25%) — separates "great"
    signals more from "decent" ones, while keeping absolute single-name cap safer.
    """
    if rr <= 0:
        return base_pct
    if rr >= 3.0:
        return min(base_pct * 1.30, 0.22)
    if rr < RR_MIN:
        return 0.0   # Caller should hard-filter; safety net here.
    return base_pct


# ---------------------------------------------------------------------------
# Strategy config table (data, not code) — eliminates ~150 lines of duplication
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StrategyConfig:
    """Per-strategy configuration. All multipliers are applied to ideal_buy."""

    tp1_mult: float           # take_profit_1 = ideal * tp1_mult
    expected_mult: float       # expected_target = ideal * expected_mult (used for R/R)
    pos_mult: float = 1.0      # position multiplier on top of market-cap baseline
    stop_pct_extra: float = 0.0  # additional stop-loss percent on top of band default
    abs_stop_cap_pct: Optional[float] = None  # if set, hard cap stop loss at this drop
    tp2_rule: str = "赚到 +15% 后启用动态保护——股价跌破 10 日均线，或从最高点回撤 2.5 倍日波幅时清仓"
    stage_rules: Dict[str, str] = field(default_factory=dict, hash=False, compare=False)


# Empirical expected-target multipliers account for staged trim + trailing tail
# distribution conditional on success. Tuned from historical picker_backtest.
_STRATEGY_CONFIGS: Dict[str, _StrategyConfig] = {
    BUY_PULLBACK: _StrategyConfig(
        tp1_mult=1.05, expected_mult=1.12,
        stage_rules={
            "stage_6pct": "减仓 1/3，止损上移至成本",
            "stage_12pct": "再减 1/3，止损上移至 +6%",
            "stage_15pct": "剩余 1/3 启用动态保护：跌破 10 日均线或从最高点回撤 2.5 倍日波幅时清仓",
        },
    ),
    BOTTOM_REVERSAL: _StrategyConfig(
        # Hard cap raised +15→+20: real reversals run +18-25% on main leg;
        # +15 single-shot exit caps fat-tail winners. New rule: +15 减半 / +20 全清.
        tp1_mult=1.08, expected_mult=1.14,
        pos_mult=0.6, stop_pct_extra=0.02,
        tp2_rule="浮盈 +15% 减半，+20% 全部止盈（让真反转走完主升段）",
        stage_rules={
            "stage_8pct": "减仓 1/3",
            "stage_15pct": "再减 1/2，止损上移至 +8%",
            "stage_20pct": "全部止盈（反转策略硬顶 +20%）",
            "no_progress_3d": "买入 3 日内未确认上涨 → 减半",
        },
    ),
}


def _pullback_secondary(ideal: float, ma10: float, ma20: float) -> float:
    """Deeper pullback entry. Anchor to MA10 (fallback MA20, then -4% of ideal).

    Always enforce at least 2% gap below ideal so the two entries don't visually
    overlap once the LLM expands each into a ±1.5% range in the report UI.
    """
    candidates: List[float] = []
    if _safe_pos(ma10) and ma10 < ideal:
        candidates.append(ma10)
    if _safe_pos(ma20) and ma20 < ideal:
        candidates.append(ma20)
    secondary = max(candidates) if candidates else ideal * 0.96
    return min(secondary, ideal * 0.98)


def _resolve_entry_anchor(
    sid: str, current_price: float, ma5: float, ma10: float, ma20: float,
    prior_high: Optional[float], recent_low: Optional[float], day_low: Optional[float],
) -> Tuple[float, float, Optional[float]]:
    """Return (ideal_buy, secondary_buy, tech_stop_anchor).

    - ideal_buy:        primary entry price for R/R math.
    - secondary_buy:    deeper pullback re-entry reference shown in UI.
    - tech_stop_anchor: technical stop reference (None = use abs_stop only).
    """
    if sid == BUY_PULLBACK:
        ideal = min(current_price, ma5 * 1.01) if _safe_pos(ma5) else current_price
        return ideal, _pullback_secondary(ideal, ma10, ma20), None
    if sid == BOTTOM_REVERSAL:
        anchor = recent_low * 0.98 if _safe_pos(recent_low) else current_price * 0.94
        secondary = recent_low if _safe_pos(recent_low) and recent_low < current_price else current_price * 0.95
        return current_price, min(secondary, current_price * 0.97), anchor
    ideal = min(current_price, ma5 * 1.01) if _safe_pos(ma5) else current_price
    return ideal, _pullback_secondary(ideal, ma10, ma20), None


# ---------------------------------------------------------------------------
# Public entry: compute_trade_levels
# ---------------------------------------------------------------------------


def compute_trade_levels(
    *,
    code: str = "",
    strategy_id: str,
    current_price: float,
    ma5: float = 0.0,
    ma10: float = 0.0,
    ma20: float = 0.0,
    atr: float = 0.0,
    market_cap_yi: float = 0.0,
    prior_high: Optional[float] = None,
    recent_low: Optional[float] = None,
    day_low: Optional[float] = None,
) -> TradeLevels:
    """Compute strategy-specific trade levels for a stock snapshot.

    Returns TradeLevels. Caller should check `passes_rr_filter` for hard filtering.
    Unknown strategy IDs fall back to buy_pullback semantics (safest default).
    """
    if not _safe_pos(current_price):
        logger.debug("[trade_levels] %s skipped: invalid current_price=%s", code, current_price)
        return TradeLevels(notes=["invalid_current_price"])

    sid = (strategy_id or "").strip().lower()
    cfg = _STRATEGY_CONFIGS.get(sid) or _STRATEGY_CONFIGS[BUY_PULLBACK]

    ideal, secondary, tech_stop_anchor = _resolve_entry_anchor(
        sid, current_price, ma5, ma10, ma20, prior_high, recent_low, day_low,
    )

    # Stop-loss: max(technical_anchor, absolute) so the higher floor wins.
    abs_stop_pct = _stop_loss_pct_for_band(market_cap_yi) + cfg.stop_pct_extra
    if cfg.abs_stop_cap_pct is not None:
        abs_stop_pct = min(abs_stop_pct, cfg.abs_stop_cap_pct)
    abs_stop = ideal * (1 - abs_stop_pct)

    if tech_stop_anchor is not None:
        stop = max(tech_stop_anchor, abs_stop)
    elif _safe_pos(ma20):
        # buy_pullback path: prefer MA20 - 0.5% as technical stop.
        stop = max(ma20 * 0.995, abs_stop)
    else:
        stop = abs_stop

    tp1 = ideal * cfg.tp1_mult
    expected = ideal * cfg.expected_mult

    risk = max(ideal - stop, 1e-6)
    rr = (expected - ideal) / risk

    base_pos = _base_position_pct(market_cap_yi) * cfg.pos_mult
    pos = _adjust_position_by_rr(base_pos, rr)

    return TradeLevels(
        ideal_buy=ideal, secondary_buy=secondary,
        stop_loss=stop, take_profit_1=tp1,
        take_profit_2_rule=cfg.tp2_rule, expected_target=expected,
        position_pct=pos, risk_reward=rr,
        stage_rules=cfg.stage_rules,
    )


LUP_STOP_PCT = 0.04        # -4% 幅度止损（文档铁律，取代旧 6%/7%）
LUP_CHASE_CAP_PCT = 1.08   # entry never > limit-up close * 1.08


def compute_limit_up_pullback_levels(
    *,
    code: str = "",
    current_price: float,
    ma5: float = 0.0,
    ma10: float = 0.0,
    ma20: float = 0.0,
    lu_open: float,
    lu_low: float,
    lu_close: float,
    market_cap_yi: float = 0.0,
) -> Optional[TradeLevels]:
    """Trade levels for the limit-up pullback (涨停回踩) setup.

    Anchored to the limit-up candle instead of fixed percentages:
      ideal     = stabilisation zone (max(MA5, 涨停实体1/2)), capped by the
                  chase boundary; 跌破涨停开盘价的事件由选股层直接淘汰.
      stop      = max(涨停K最低价, ideal-4%) — 结构位与幅度位取先触发者.
      tp1       = ideal * 1.10（卖出一半，锁定本金）.
      expected  = max(涨停日收盘 * 1.10, ideal * 1.15) — 剩余仓位博弈前高突破.
      secondary = 更深回踩加仓位（MA10/MA20 锚定，保持 <= ideal*0.98 UI 不变式）.

    Returns ``None`` when the anchor candle or current price is invalid, or
    when the geometry leaves no usable risk window.
    """
    if not _safe_pos(current_price, lu_open, lu_low, lu_close):
        logger.debug("[trade_levels] %s LUP skipped: bad anchor", code)
        return None
    mid = (lu_open + lu_close) / 2.0

    ideal = current_price
    zone = max(ma5 if _safe_pos(ma5) else 0.0, mid)
    if zone > 0 and ideal > zone:
        ideal = zone
    cap = lu_close * LUP_CHASE_CAP_PCT
    if ideal > cap:
        return None

    stop = max(lu_low, ideal * (1 - LUP_STOP_PCT))
    if stop >= ideal * 0.97:
        # Structural + amplitude stops leave < 3% risk window — geometric
        # dead zone (entry glued to the low); caller re-checks R/R anyway.
        stop = min(lu_low, ideal * (1 - LUP_STOP_PCT))
    if not stop < ideal:
        return None

    tp1 = ideal * 1.10
    expected = max(lu_close * 1.10, ideal * 1.15)
    risk = max(ideal - stop, 1e-6)
    rr = (expected - ideal) / risk

    base_pos = min(_base_position_pct(market_cap_yi), 0.15)
    pos = min(_adjust_position_by_rr(base_pos, rr), 0.15)  # 单票≤15% 铁律
    secondary = _pullback_secondary(ideal, ma10, ma20)
    if secondary <= stop:
        secondary = 0.0

    return TradeLevels(
        ideal_buy=ideal, secondary_buy=secondary,
        stop_loss=stop, take_profit_1=tp1,
        take_profit_2_rule=(
            "+10% 卖出一半；剩余跟踪 MA10 或自峰值回撤 2.5 倍日波幅清仓；"
            "无法突破涨停日收盘（前高）则清仓；放量长上影/天量滞涨直接全清"
        ),
        expected_target=expected, position_pct=pos, risk_reward=rr,
        stage_rules={
            "entry_split": "企稳确认日尾盘 4 成底仓；次日放量站上 5 日线加 2 成（总仓位 ≤6 成、单票 ≤15% 资金）",
            "tp_half": "浮盈 +10% 卖出一半",
            "trail": "剩余仓位跌破 MA10 或自峰值回撤 2.5×ATR 清仓",
            "prior_high": "未能突破涨停日收盘价（前高）→ 清仓",
            "top_signal": "放量长上影 / 天量滞涨 / 高位十字星 → 无视涨幅全清",
            "time_stop": "回调超 7 个交易日未拉升，或买入后 5 日无预期上涨 → 清仓",
            "trend_break": "收盘跌破 20 日均线且 3 日未收回 → 趋势走坏离场",
        },
        notes=["limit_up_pullback", f"anchor_low={lu_low:.3f}"],
    )


# ---------------------------------------------------------------------------
# Trailing exit evaluation
# ---------------------------------------------------------------------------


def evaluate_trailing_exit(
    *,
    strategy_id: str,
    entry_price: float,
    current_price: float,
    current_high: float,
    ma10: float,
    ma20: float = 0.0,
    atr: float = 0.0,
    holding_days: int = 0,
    peak_price: Optional[float] = None,
) -> Tuple[bool, str]:
    """Evaluate whether a held position should exit now.

    Sequence of checks (early-return on first hit):
      1. Below initial absolute stop: hard exit.
      2. Bottom-reversal special rule: +15% hard take-profit.
      3. Stage-based stop tightening (浮盈 5% → 成本，10% → +5%).
      4. Trailing (浮盈 >20%): below MA10 OR retraced ATR×2.5 from peak.
      5. Time stop: 20 trading days no meaningful progress.

    Args:
        strategy_id:   Strategy that opened the position.
        entry_price:   Original entry price.
        current_price: Latest close.
        current_high:  Latest bar high (used for hit detection).
        ma10:          Current MA10 value.
        ma20:          Current MA20 value (optional).
        atr:           Current ATR (period determined by caller; 14 or 20 fine).
        holding_days:  Trading days held.
        peak_price:    Highest close since entry (caller tracks; defaults to current_price).

    Returns:
        (should_exit, reason). reason is a short Chinese tag for logging.
    """
    if not _safe_pos(entry_price, current_price):
        return False, "invalid_input"

    profit_pct = (current_price - entry_price) / entry_price * 100
    peak = peak_price if (peak_price and peak_price > entry_price) else current_price

    sid = (strategy_id or "").strip().lower()

    # ---- Bottom reversal: swing-trade rules ----
    # This is a medium-term (20-60d) bet on "true bottom → consolidation
    # bottom_reversal (v2 left-side): manual-analysis watchlist for stocks
    # sitting in a real consolidation. Loose exit rules — the strategy
    # is observed, not auto-traded. Original v2 rule set:
    #   +35% hardcap, ATR/MA10 trailing only after +25%, 60d time stop,
    #   -8% hard floor.
    if sid == BOTTOM_REVERSAL:
        if profit_pct >= 35.0:
            return True, "bottom_reversal_hardcap_35pct"
        if profit_pct >= 25.0:
            if _safe_pos(atr) and atr > 0:
                retrace = peak - current_price
                if retrace >= atr * 3.0:
                    return True, "trailing_atr3.0_retrace"
            if _safe_pos(ma10) and current_price < ma10 * 0.97:
                return True, "trailing_below_ma10_3pct"
        if profit_pct <= -8.0:
            return True, "bottom_reversal_hard_floor_-8pct"
        if holding_days >= 60 and profit_pct < 3.0:
            return True, "time_stop_60d_no_progress"
        return False, ""

    # reversal_breakout (v3 right-side): actionable swing entry after
    # the breakout has already happened. Tight rules tuned for a
    # ~20d hold and a quick lock-in:
    #   +25% hardcap, ATR trailing from +12%, MA10 trail from +8%,
    #   -6% hard floor, 20d time stop.
    # All thresholds env-overridable for A/B tuning.
    if sid == REVERSAL_BREAKOUT:
        import os as _os
        def _ef(k, d):
            try: return float(_os.environ.get(k, d))
            except (ValueError, TypeError): return float(d)
        hardcap = _ef("RB_EXIT_HARDCAP_PCT", 25.0)
        trail_start = _ef("RB_EXIT_TRAIL_START_PCT", 12.0)
        trail_atr_mul = _ef("RB_EXIT_TRAIL_ATR_MUL", 2.0)
        ma10_start = _ef("RB_EXIT_MA10_START_PCT", 8.0)
        ma10_buf = _ef("RB_EXIT_MA10_BUF_PCT", 2.0)
        hard_floor = _ef("RB_EXIT_HARD_FLOOR_PCT", -6.0)
        grace_days = int(_ef("RB_EXIT_GRACE_DAYS", 0))
        time_stop_days = int(_ef("RB_EXIT_TIME_STOP_DAYS", 20))
        time_stop_min_pct = _ef("RB_EXIT_TIME_STOP_MIN_PCT", 2.0)
        if profit_pct >= hardcap:
            return True, f"reversal_breakout_hardcap_{int(hardcap)}pct"
        if profit_pct >= trail_start:
            if _safe_pos(atr) and atr > 0:
                retrace = peak - current_price
                if retrace >= atr * trail_atr_mul:
                    return True, f"trailing_atr{trail_atr_mul}_retrace"
        if profit_pct >= ma10_start:
            if _safe_pos(ma10) and current_price < ma10 * (1.0 - ma10_buf / 100.0):
                return True, f"trailing_below_ma10_{int(ma10_buf)}pct"
        if profit_pct <= hard_floor and holding_days > grace_days:
            return True, f"reversal_breakout_hard_floor_{int(hard_floor)}pct"
        if holding_days >= time_stop_days and profit_pct < time_stop_min_pct:
            return True, f"time_stop_{time_stop_days}d_no_progress"
        return False, ""

    # ---- buy_pullback (default): short-term rules below ----
    # Trailing zone (>=15% profit, was 20%): A-share rallies often start ABC
    # consolidation around +18-22%; entering trailing at 15% locks in 3-5%
    # additional profit on average versus the 20% threshold.
    if profit_pct >= 15.0:
        if _safe_pos(ma10) and current_price < ma10:
            return True, "trailing_below_ma10"
        if _safe_pos(atr) and atr > 0:
            retrace = peak - current_price
            if retrace >= atr * 2.5:
                return True, "trailing_atr2.5_retrace"

    # Stage stop: profit 12-15% — break +6% floor (was 10-20% / +5%).
    # Wider stages reduce premature stop-outs from intraday noise.
    if 12.0 <= profit_pct < 15.0:
        floor = entry_price * 1.06
        if current_price < floor:
            return True, "stage12_break_+6pct_floor"

    # Stage stop: profit 6-12% — break cost (was 5-10%).
    if 6.0 <= profit_pct < 12.0:
        if current_price < entry_price:
            return True, "stage6_break_cost"

    # Time stop: 20 trading days without breaking +5%.
    if holding_days >= 20 and profit_pct < 5.0:
        return True, "time_stop_20d_no_progress"

    # MA20 break for non-reversal strategies (defensive trend-failure exit).
    if _safe_pos(ma20) and current_price < ma20 * 0.97:
        return True, "broke_ma20"

    return False, ""


# ---------------------------------------------------------------------------
# Backtest helper: simulate forward trade with unified rules
# ---------------------------------------------------------------------------


def _net_return_pct(entry: float, exit_: float) -> float:
    """Net return after commission (both sides) and stamp duty (sell only)."""
    gross = (exit_ - entry) / entry * 100.0
    cost_pct = DEFAULT_COMMISSION_PCT * 2 + DEFAULT_STAMP_DUTY_SELL_PCT
    return gross - cost_pct


def simulate_forward_trade(
    *,
    strategy_id: str,
    entry_price: float,
    market_cap_yi: float,
    bars: List[Dict[str, float]],
    apply_slippage: bool = True,
    apply_limit_up_filter: bool = True,
    is_kc_cy: bool = False,
    hard_stop_pct: float = 0.0,
) -> Dict[str, Any]:
    """Simulate a forward trade using unified trade_levels exit rules.

    Replaces hardcoded -8% / +15% in picker_backtest_service. Bars must be
    forward-only (after entry day), each having keys: close, high, low,
    optional ma10, ma20, atr, pct_chg.

    Returns dict with: exit_price, exit_reason, return_pct, hold_days,
    skipped (True if entry skipped due to limit-up filter).
    """
    if not _safe_pos(entry_price):
        return {"skipped": True, "skip_reason": "invalid_entry_price"}

    # Slippage on entry (assume buy at slightly higher price).
    effective_entry = entry_price * (1 + DEFAULT_SLIPPAGE_PCT / 100.0) if apply_slippage else entry_price

    # Limit-up filter: if entry day's pct_chg >= limit-up threshold, skip
    # (cannot realistically buy in at limit-up board). Caller is responsible
    # for placing entry-day bar at index 0 OR passing pct_chg via kwargs.
    if apply_limit_up_filter and bars:
        entry_pct = bars[0].get("pct_chg")
        if entry_pct is not None:
            limit_pct = LIMIT_UP_KCCY if is_kc_cy else LIMIT_UP_MAIN
            if entry_pct >= limit_pct:
                return {"skipped": True, "skip_reason": "limit_up_unfillable"}

    peak_price = effective_entry
    hard_stop_price = (
        effective_entry * (1.0 - hard_stop_pct) if hard_stop_pct > 0 else 0.0
    )
    for i, bar in enumerate(bars):
        close = bar.get("close")
        high = bar.get("high", close)
        low = bar.get("low", close)
        if close is None or close <= 0:
            continue
        if high and high > peak_price:
            peak_price = high

        # Hard stop check FIRST — wins over trailing rules. Triggers when
        # the bar's intraday low pierces the hard stop level.
        if hard_stop_price > 0 and low is not None and float(low) <= hard_stop_price:
            exit_price_raw = hard_stop_price
            exit_price = (
                exit_price_raw * (1 - DEFAULT_SLIPPAGE_PCT / 100.0)
                if apply_slippage
                else exit_price_raw
            )
            ret = _net_return_pct(effective_entry, exit_price)
            return {
                "skipped": False,
                "exit_price": exit_price,
                "exit_reason": f"hard_stop_-{int(hard_stop_pct*100)}pct",
                "return_pct": ret,
                "hold_days": i + 1,
            }

        should_exit, reason = evaluate_trailing_exit(
            strategy_id=strategy_id,
            entry_price=effective_entry,
            current_price=float(close),
            current_high=float(high) if high else float(close),
            ma10=float(bar.get("ma10") or 0.0),
            ma20=float(bar.get("ma20") or 0.0),
            atr=float(bar.get("atr") or 0.0),
            holding_days=i + 1,
            peak_price=peak_price,
        )
        if should_exit:
            exit_price = float(close) * (1 - DEFAULT_SLIPPAGE_PCT / 100.0) if apply_slippage else float(close)
            ret = _net_return_pct(effective_entry, exit_price)
            return {
                "skipped": False,
                "exit_price": exit_price,
                "exit_reason": reason,
                "return_pct": ret,
                "hold_days": i + 1,
            }

    # No exit triggered: settle at last bar close.
    if not bars:
        return {"skipped": True, "skip_reason": "no_bars"}
    last_close = bars[-1].get("close")
    if last_close is None or last_close <= 0:
        return {"skipped": True, "skip_reason": "invalid_exit_price"}
    exit_price = float(last_close) * (1 - DEFAULT_SLIPPAGE_PCT / 100.0) if apply_slippage else float(last_close)
    ret = _net_return_pct(effective_entry, exit_price)
    return {
        "skipped": False,
        "exit_price": exit_price,
        "exit_reason": "window_end",
        "return_pct": ret,
        "hold_days": len(bars),
    }
