# -*- coding: utf-8 -*-
"""Tests for the 涨停回踩买入法 engine: levels, exit rules, simulator,
quality checker, and the AI-disabled picker output."""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.services.trade_levels import compute_limit_up_pullback_levels
from src.services.limit_up_rules import (
    evaluate_limit_up_pullback_exit,
    simulate_limit_up_pullback_trade,
)


def _bar(close, *, o=None, h=None, lo=None, ma10=0.0, ma20=0.0, atr=0.0,
         pct=0.0):
    return {
        "close": close, "open": o if o is not None else close,
        "high": h if h is not None else close,
        "low": lo if lo is not None else close,
        "ma10": ma10, "ma20": ma20, "atr": atr, "pct_chg": pct,
    }


class TestComputeLupLevels(unittest.TestCase):

    def _levels(self, **kw):
        base = dict(current_price=10.9, ma5=10.6, ma10=10.2, ma20=9.8,
                    lu_open=10.2, lu_low=10.1, lu_close=11.0,
                    market_cap_yi=60.0)
        base.update(kw)
        return compute_limit_up_pullback_levels(**base)

    def test_core_invariants(self):
        tl = self._levels()
        self.assertIsNotNone(tl)
        self.assertGreater(tl.ideal_buy, tl.stop_loss)
        self.assertLessEqual(tl.secondary_buy, tl.ideal_buy * 0.98 + 1e-9)
        self.assertLessEqual(tl.position_pct, 0.15)
        self.assertGreater(tl.risk_reward, 0)
        self.assertAlmostEqual(tl.take_profit_1, tl.ideal_buy * 1.10)

    def test_ideal_anchored_to_zone(self):
        tl = self._levels(current_price=12.0)  # above zone -> pulled back
        self.assertAlmostEqual(tl.ideal_buy, max(10.6, (10.2 + 11.0) / 2))

    def test_chase_cap_returns_none(self):
        # MA5 glued far above the limit-up close: geometry beyond +8% chase
        tl = self._levels(current_price=13.0, ma5=12.8)
        self.assertIsNone(tl)

    def test_bad_anchor_returns_none(self):
        self.assertIsNone(self._levels(current_price=0.0))
        self.assertIsNone(self._levels(lu_low=0.0))


class TestLupExitEvaluator(unittest.TestCase):

    def _ev(self, **kw):
        base = dict(entry_price=10.0, current_price=10.0, ma10=9.5,
                    ma20=9.0, atr=0.3, holding_days=1, peak_price=None,
                    trimmed=False)
        base.update(kw)
        return evaluate_limit_up_pullback_exit(**base)

    def test_invalid_input_holds(self):
        action, reason = self._ev(entry_price=0.0)
        self.assertEqual((action, reason), ("hold", "invalid_input"))

    def test_trim_at_ten_pct(self):
        action, reason = self._ev(current_price=11.0)
        self.assertEqual(action, "trim")
        self.assertIn("tp_half", reason)

    def test_trail_below_ma10_after_trim(self):
        action, reason = self._ev(current_price=10.8, ma10=11.0,
                                  peak_price=11.5, trimmed=True)
        self.assertEqual((action, reason), ("exit", "trail_below_ma10"))

    def test_giveback_after_trim_exits(self):
        action, reason = self._ev(current_price=10.3, ma10=10.0,
                                  peak_price=11.5, trimmed=True)
        self.assertEqual(reason, "post_trim_giveback")

    def test_ma20_break_wins_over_hold(self):
        action, _ = self._ev(current_price=8.5, ma20=9.0)
        self.assertEqual(action, "exit")

    def test_time_stop_no_progress(self):
        action, reason = self._ev(current_price=10.2, ma10=9.5,
                                  holding_days=3)
        self.assertEqual((action, reason),
                         ("exit", "time_stop_3d_no_progress"))

    def test_max_hold_time_stop(self):
        action, reason = self._ev(current_price=10.8, holding_days=3)
        self.assertEqual((action, reason),
                         ("exit", "time_stop_3d_max_hold"))


class TestSimulateLupTrade(unittest.TestCase):

    def test_invalid_stop_skips(self):
        sim = simulate_limit_up_pullback_trade(
            entry_price=10.0, stop_price=10.5, bars=[_bar(10.1)])
        self.assertTrue(sim["skipped"])

    def test_limit_up_entry_unfillable(self):
        sim = simulate_limit_up_pullback_trade(
            entry_price=10.0, stop_price=9.6,
            bars=[_bar(11.0, pct=10.0), _bar(11.2, pct=1.8)])
        self.assertEqual(sim.get("skip_reason"), "limit_up_unfillable")

    def test_gap_down_through_stop_fills_at_open(self):
        bars = [
            _bar(10.1, o=10.0, h=10.3, lo=9.9, ma10=9.9, ma20=9.8, atr=0.35),
            _bar(9.35, o=9.4, h=9.9, lo=9.3, ma10=9.9, ma20=9.8, atr=0.35),
        ]
        sim = simulate_limit_up_pullback_trade(
            entry_price=10.0, stop_price=9.6, bars=bars)
        self.assertEqual(sim["exit_reason"], "structural_stop")
        self.assertAlmostEqual(sim["exit_price"], 9.4 * 0.997, places=3)
        self.assertLess(sim["return_pct"], -5.5)

    def test_secondary_leg_and_trim_and_trail(self):
        bars = [
            _bar(10.1, o=10.0, h=10.3, lo=9.95, ma10=9.9, ma20=9.8,
                 atr=0.35),   # entry day: no add-on allowed on bar 0
            _bar(9.85, o=9.9, h=10.0, lo=9.6, ma10=9.9, ma20=9.8,
                 atr=0.35),   # deeper pullback -> secondary fills at 9.65
            _bar(11.1, o=10.2, h=11.2, lo=10.9, ma10=10.0, ma20=9.9,
                 atr=0.35),   # +12% over blended cost -> trim half
            _bar(9.8, o=10.5, h=10.6, lo=9.75, ma10=10.4, ma20=10.0,
                 atr=0.35),   # below cost -> clear
        ]
        sim = simulate_limit_up_pullback_trade(
            entry_price=10.0, stop_price=9.5, secondary_buy=9.65, bars=bars)
        self.assertTrue(sim["added_leg"])
        self.assertTrue(sim["trimmed"])
        self.assertEqual(sim["exit_reason"], "post_trim_break_cost")

    def test_window_end_when_no_rule_fires(self):
        bars = [
            _bar(10.2, o=10.0, h=10.35, lo=9.95, ma10=9.9, ma20=9.8,
                 atr=0.35, pct=2.0),
            _bar(10.4, o=10.2, h=10.5, lo=10.1, ma10=10.0, ma20=9.9,
                 atr=0.35, pct=2.0),
        ]
        sim = simulate_limit_up_pullback_trade(
            entry_price=10.0, stop_price=9.6, bars=bars)
        self.assertEqual(sim["exit_reason"], "window_end")
        self.assertGreater(sim["return_pct"], 0)


def _uptrend_bars(n=40, start=7.0, end=10.0, vol=1000.0):
    closes = np.linspace(start, end, n)
    return closes


class TestLupQualityCheck(unittest.TestCase):
    """Static quality gate on the limit-up day (6 hard standards)."""

    def _bars(self, *, one_line=False, lu_close=11.0):
        n = 40
        closes = _uptrend_bars(n=n, start=7.5, end=10.0)
        opens = closes - 0.05
        highs = closes + 0.1
        lows = closes - 0.15
        vols = np.full(n, 1000.0)
        # limit-up day at index n-1: +10% close, big volume
        o = closes[-2] * 1.02 if not one_line else lu_close
        h = lu_close + 0.05 if not one_line else lu_close
        lo = closes[-2] if not one_line else lu_close
        closes = np.append(closes, lu_close)
        opens = np.append(opens, o)
        highs = np.append(highs, h)
        lows = np.append(lows, lo)
        vols = np.append(vols, 2200.0)
        dates = pd.bdate_range(end="2026-09-10", periods=n + 1).strftime(
            "%Y%m%d").tolist()
        return pd.DataFrame({
            "trade_date": dates, "open": opens, "high": highs,
            "low": lows, "close": closes, "vol": vols, "_dates": dates,
        })

    _EV = {"first_time": "093500", "open_num": 0, "limit_times": 1,
           "turnover_ratio": 12.0, "ts_code": "600001.SH", "name": "测试股"}

    def _check(self, bars):
        from src.services.picker.screener.limit_up_pullback import (
            _LimitUpPullbackMixin,
        )
        return _LimitUpPullbackMixin._lup_quality_check(
            dict(self._EV), bars, len(bars) - 1)

    def test_good_board_passes(self):
        ok, reason = self._check(self._bars())
        self.assertTrue(ok, reason)

    def test_one_line_board_rejected(self):
        ok, reason = self._check(self._bars(one_line=True))
        self.assertFalse(ok)
        self.assertEqual(reason, "one_line_or_t_board")

    def test_late_seal_rejected(self):
        bars = self._bars()
        from src.services.picker.screener.limit_up_pullback import (
            _LimitUpPullbackMixin,
        )
        ev = dict(self._EV, first_time="143000")
        ok, reason = _LimitUpPullbackMixin._lup_quality_check(
            ev, bars, len(bars) - 1)
        self.assertFalse(ok)
        self.assertEqual(reason, "late_seal")

    def test_high_streak_rejected(self):
        bars = self._bars()
        from src.services.picker.screener.limit_up_pullback import (
            _LimitUpPullbackMixin,
        )
        ev = dict(self._EV, limit_times=6)
        ok, reason = _LimitUpPullbackMixin._lup_quality_check(
            ev, bars, len(bars) - 1)
        self.assertFalse(ok)
        self.assertEqual(reason, "high_streak_tail")


class TestPositionTrackerLupRouting(unittest.TestCase):

    def test_buy_pullback_routes_to_lup_trim(self):
        from src.services.position_tracker import evaluate_holding
        d = evaluate_holding(
            code="600001", name="测试", strategy_id="buy_pullback",
            entry_price=10.0, current_price=11.1, ma10=10.5, ma20=10.0,
            atr=0.3, holding_days=2)
        self.assertFalse(d.should_exit)
        self.assertIn("止盈 1/2", d.action)

    def test_other_strategy_keeps_legacy_rules(self):
        from src.services.position_tracker import evaluate_holding
        d = evaluate_holding(
            code="600001", name="测试", strategy_id="nonexistent",
            entry_price=10.0, current_price=10.5, ma10=10.2, ma20=10.1,
            atr=0.3, holding_days=2)
        self.assertEqual(d.action, "持有")


class TestPickerDisableAI(unittest.TestCase):

    def _svc(self):
        from src.services.picker.service import StockPickerService
        return StockPickerService.__new__(StockPickerService)

    def _screened(self, **kw):
        from src.services.picker.constants import ScreenedStock
        base = dict(code="600001", name="测试", price=10.5, change_pct=2.0,
                    turnover_rate=12.0, market_cap=80.0, score=88.0,
                    strategies=["buy_pullback"], ideal_buy=10.4,
                    stop_loss=9.9, take_profit_1=11.4,
                    secondary_buy=10.1, limit_up_date="20260901",
                    setup="回踩5日线")
        base.update(kw)
        return ScreenedStock(**base)

    def test_screened_to_pick_carries_lup_fields(self):
        from src.services.picker.service import StockPickerService
        s = self._screened()
        pick = StockPickerService._screened_to_pick(s)
        self.assertAlmostEqual(pick.secondary_buy, 10.1)
        self.assertEqual(pick.limit_up_date, "20260901")
        self.assertIn("回踩5日线", pick.reason)
        d = pick.to_dict()
        self.assertAlmostEqual(d["secondary_buy"], 10.1)
        self.assertEqual(d["setup"], "回踩5日线")

    def test_quant_only_result_success_without_llm(self):
        from src.services.picker.constants import PickerResult
        svc = self._svc()
        result = PickerResult()
        out = svc._quant_only_result(
            result, [self._screened()], start=0.0,
            summary="s", risk_warning="w")
        self.assertTrue(out.success)
        self.assertEqual(len(out.picks), 1)
        self.assertEqual(out.market_summary, "s")


if __name__ == "__main__":
    unittest.main()
