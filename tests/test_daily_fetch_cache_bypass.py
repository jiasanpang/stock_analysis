# -*- coding: utf-8 -*-
"""Tests for A-share intraday daily-bar fetch bypass (pipeline cache logic)."""

import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.core.trading_calendar import should_bypass_daily_fetch_cache_cn


class TestShouldBypassDailyFetchCacheCn(unittest.TestCase):
    """should_bypass_daily_fetch_cache_cn behavior."""

    def test_intraday_afternoon_bypasses(self):
        sh = ZoneInfo("Asia/Shanghai")
        d = date(2025, 3, 4)  # Tuesday (not validated; bypass uses is_market_open)
        now = datetime(2025, 3, 4, 14, 30, tzinfo=sh)
        with patch("src.core.trading_calendar.is_market_open", return_value=True):
            self.assertTrue(
                should_bypass_daily_fetch_cache_cn(
                    d, datetime(2025, 3, 4, 9, 35), now
                )
            )

    def test_after_close_recent_row_skips(self):
        sh = ZoneInfo("Asia/Shanghai")
        d = date(2025, 3, 4)
        now = datetime(2025, 3, 4, 18, 0, tzinfo=sh)
        row_ua = datetime(2025, 3, 4, 15, 30, tzinfo=sh)
        with patch("src.core.trading_calendar.is_market_open", return_value=True):
            self.assertFalse(
                should_bypass_daily_fetch_cache_cn(d, row_ua, now)
            )

    def test_after_close_stale_intraday_row_bypasses(self):
        sh = ZoneInfo("Asia/Shanghai")
        d = date(2025, 3, 4)
        now = datetime(2025, 3, 4, 18, 0, tzinfo=sh)
        row_ua = datetime(2025, 3, 4, 14, 30, tzinfo=sh)
        with patch("src.core.trading_calendar.is_market_open", return_value=True):
            self.assertTrue(
                should_bypass_daily_fetch_cache_cn(d, row_ua, now)
            )


if __name__ == "__main__":
    unittest.main()
