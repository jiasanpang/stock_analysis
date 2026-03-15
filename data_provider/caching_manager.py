# -*- coding: utf-8 -*-
"""
Caching wrapper for DataFetcherManager.

Used by picker backtest to avoid duplicate get_daily_data fetches for the same
(stock_code, date_range) within a run. Thread-safe.
"""

import threading
from typing import Optional, Tuple

import pandas as pd

from .base import DataFetcherManager


class CachingDataFetcherManager:
    """Wraps DataFetcherManager with an in-memory cache for get_daily_data.
    Exposes _fetchers for get_tushare_api compatibility."""

    def __init__(self, underlying: DataFetcherManager):
        self._underlying = underlying
        self._cache: dict = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @property
    def _fetchers(self):
        return self._underlying._fetchers

    def get_daily_data(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 30,
    ) -> Tuple[pd.DataFrame, str]:
        """Fetch daily data with cache. Same (code, start, end, days) returns cached result."""
        key = (stock_code, start_date or "", end_date or "", days)
        with self._lock:
            if key in self._cache:
                self._hits += 1
                return self._cache[key]
            self._misses += 1
        result = self._underlying.get_daily_data(
            stock_code, start_date=start_date, end_date=end_date, days=days
        )
        with self._lock:
            self._cache[key] = result
        return result

    def clear_cache(self) -> None:
        """Clear cache (e.g. before a new backtest run)."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def cache_stats(self) -> Tuple[int, int]:
        """Return (hits, misses) for debugging."""
        with self._lock:
            return self._hits, self._misses
