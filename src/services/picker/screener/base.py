# -*- coding: utf-8 -*-
"""
Core ``StockScreener`` setup: constructor, instance attributes, market
environment check, small static utilities and the thin delegations to
``risk_filters``. All other concerns (data fetch, filters/scoring, pipeline)
live in dedicated mixins inside this sub-package.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from src.services.picker import risk_filters
from src.services.picker.constants import (
    MarketEnvironment,
    ScreenedStock,
    TURNOVER_MAX_PCT,
    TURNOVER_MIN_PCT,
)
from src.services.picker.market_guard import check_market_environment

logger = logging.getLogger(__name__)


class _ScreenerBase:
    """Shared state + small helpers used by every other mixin."""

    _EXCLUDE_NAME_KEYWORDS = ("ST", "*ST", "退市", "N ", "C ")
    _ETF_PREFIXES = ("51", "52", "56", "58", "15", "16", "18")

    # Strategies that require daily spot data (fetched via _fetch_spot_data).
    DAILY_DATA_STRATEGIES = {"buy_pullback"}

    # Strategies that benefit from sector strength filtering
    SECTOR_FILTER_STRATEGIES = {"buy_pullback"}

    def __init__(
        self,
        data_manager=None,
        picker_strategies: Optional[List[str]] = None,
        picker_mode: str = "balanced",
        turnover_min: Optional[float] = None,
        turnover_max: Optional[float] = None,
        enable_b_wave_filter: bool = True,
        allow_loss: bool = False,
        spot_timeout: Optional[int] = None,
    ):
        self._data_manager = data_manager
        self._spot_timeout = spot_timeout if spot_timeout is not None else int(
            os.getenv("PICKER_SPOT_TIMEOUT", "30")
        )
        self._as_of_date: Optional[str] = None  # YYYY-MM-DD for historical screening
        self._picker_strategies = picker_strategies if picker_strategies else ["buy_pullback"]
        self._picker_mode = (picker_mode or "balanced").lower()
        self._turnover_min = turnover_min if turnover_min is not None else TURNOVER_MIN_PCT
        self._turnover_max = turnover_max if turnover_max is not None else TURNOVER_MAX_PCT
        self._enable_b_wave_filter = enable_b_wave_filter
        self._allow_loss = allow_loss
        self._stock_basic_cache: Optional[pd.DataFrame] = None  # Reuse across days in backtest
        # Strategies removed by MarketGuard for the current screen() call.
        # Each entry: (strategy_id, human_readable_zh_reason). Consumed by
        # StockPickerService to render an accurate "今日市场" message instead
        # of the generic "no stocks matched filters" fallback.
        self._gated_strategies: List[Tuple[str, str]] = []

    def _check_market_environment(self) -> Optional[MarketEnvironment]:
        """Check SSE index vs MA20 to determine market regime."""
        return check_market_environment(self._data_manager, self._as_of_date)

    # ── Risk filter delegation ────────────────────────────────────────

    def _filter_by_bias(self, candidates, max_bias_pct=8.0, leader_bias_exempt_pct=0.0):
        return risk_filters.filter_by_bias(
            candidates, self._data_manager, self._as_of_date,
            self._fetch_daily_batch, max_bias_pct, leader_bias_exempt_pct,
        )

    def _filter_limit_up_streak(self, candidates, days=5, min_limit_up_days=2):
        return risk_filters.filter_limit_up_streak(
            candidates, self._data_manager, self._as_of_date,
            self._fetch_daily_batch, days, min_limit_up_days,
        )

    def _filter_consecutive_up_days(self, candidates, days=5, max_up_days=None):
        return risk_filters.filter_consecutive_up_days(
            candidates, self._data_manager, self._as_of_date,
            self._fetch_daily_batch, self._picker_mode, days, max_up_days,
        )

    def _filter_healthy_pullback(self, candidates, lookback_days=20, params=None, strategy_id=None):
        return risk_filters.filter_healthy_pullback(
            candidates, self._data_manager, self._as_of_date,
            self._fetch_daily_batch, self._picker_mode, lookback_days, params, strategy_id,
        )

    def _filter_b_wave_risk(self, candidates, lookback_days=20):
        return risk_filters.filter_b_wave_risk(
            candidates, self._data_manager, self._as_of_date,
            self._fetch_daily_batch, lookback_days,
        )

    @staticmethod
    def _is_leader_candidate(s: ScreenedStock) -> bool:
        """Check if stock qualifies for leader bias exemption."""
        return risk_filters.is_leader_candidate(s)

    # ── Utility methods ────────────────────────────────────────────

    @staticmethod
    def _first_col(df: pd.DataFrame, *names: str):
        """Return first column name that exists in ``df``, or ``None``."""
        for n in names:
            if n in df.columns:
                return n
        return None

    @staticmethod
    def _calc_volume_ratio(current_vol: float, avg_5d_vol: float) -> float:
        """Volume ratio normalised by elapsed trading minutes.

        After market close the value collapses to ``current / avg`` (full-day).
        Before market open the result is ``0.0`` (insufficient data).
        Intraday it is scaled by the elapsed fraction of the 240-min day so
        morning volumes do not look artificially low.
        """
        if not avg_5d_vol or avg_5d_vol <= 0 or not current_vol or current_vol <= 0:
            return 0.0

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        total_minutes = 240  # 9:30-11:30 (120 min) + 13:00-15:00 (120 min)

        if now.hour >= 15:
            return round(current_vol / avg_5d_vol, 2)

        if now.hour < 9 or (now.hour == 9 and now.minute < 30):
            return 0.0

        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        morning_close = now.replace(hour=11, minute=30, second=0, microsecond=0)
        afternoon_open = now.replace(hour=13, minute=0, second=0, microsecond=0)

        if now <= morning_close:
            elapsed = (now - market_open).total_seconds() / 60
        elif now < afternoon_open:
            elapsed = 120  # full morning session
        else:
            elapsed = 120 + (now - afternoon_open).total_seconds() / 60

        elapsed = max(1, min(elapsed, total_minutes))
        avg_per_min = avg_5d_vol / total_minutes
        current_per_min = current_vol / elapsed

        return round(current_per_min / avg_per_min, 2) if avg_per_min > 0 else 0.0

    @staticmethod
    def _trade_date_to_iso(trade_date: str) -> str:
        """Convert ``YYYYMMDD`` to ``YYYY-MM-DD``."""
        if not trade_date or len(trade_date) != 8:
            return trade_date
        return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
