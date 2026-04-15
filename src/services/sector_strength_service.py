"""Sector strength service for identifying strong industry sectors."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

logger = logging.getLogger("stock_analysis")


class SectorStrengthService:
    """Evaluate industry sector strength and provide strong-sector stock codes.

    Uses AkShare (EastMoney) as the primary data source for:
    - Industry sector rankings (stock_board_industry_name_em)
    - Sector member stocks (stock_board_industry_cons_em)
    - Historical sector performance (stock_board_industry_hist_em)

    Backtest optimizations:
    - Sector history is preloaded once for the entire backtest range (_preload_sector_history)
    - Sector member stocks are cached without date key (_get_sector_members)
    """

    # TTL constants
    _DEFAULT_TTL = 3600          # 1 hour for general cache
    _MEMBERS_TTL = 86400         # 24 hours for sector members (quarterly changes)
    _PRELOAD_TTL = 86400         # 24 hours for preloaded history

    # Class-level shared cache so preloaded data is accessible across all instances
    _cache: Dict[str, Tuple[float, object]] = {}
    _cache_lock = threading.Lock()

    def __init__(self):
        self._cache_ttl = self._DEFAULT_TTL

    # ------------------------------------------------------------------ cache helpers

    def _get_cached(self, key: str, ttl: Optional[int] = None):
        """Get cached data if not expired.

        Args:
            key: Cache key.
            ttl: Custom TTL in seconds. Falls back to self._cache_ttl if None.
        """
        with self._cache_lock:
            entry = self._cache.get(key)
        if not entry:
            return None
        ts, data = entry
        effective_ttl = ttl if ttl is not None else self._cache_ttl
        if time.time() - ts < effective_ttl:
            return data
        return None

    def _set_cached(self, key: str, data):
        """Store data in cache (thread-safe write)."""
        with self._cache_lock:
            self._cache[key] = (time.time(), data)

    # ------------------------------------------------------------------ public API

    def get_strong_sectors(self, top_pct: float = 0.3, trade_date: Optional[str] = None) -> List[Dict]:
        """Get strong sectors ranked by recent performance.

        Args:
            top_pct: Top percentage of sectors to consider strong (0.3 = top 30%)
            trade_date: Optional trade date (YYYYMMDD) for historical mode. None = realtime.

        Returns:
            List of dicts with keys: name, change_pct
        """
        if trade_date is not None:
            return self._get_strong_sectors_backtest(top_pct, trade_date)
        return self._get_strong_sectors_realtime(top_pct)

    def get_strong_sector_codes(self, top_pct: float = 0.3, trade_date: Optional[str] = None) -> Set[str]:
        """Get stock codes belonging to strong sectors.

        Args:
            top_pct: Top percentage of sectors (0.3 = top 30%)
            trade_date: Optional trade date for historical mode

        Returns:
            Set of stock codes (6-digit, e.g. "600519") in strong sectors.
            Empty set if data unavailable.
        """
        cache_key = f"sector_codes_{trade_date or 'realtime'}_{int(top_pct * 100)}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        strong_sectors = self.get_strong_sectors(top_pct=top_pct, trade_date=trade_date)
        if not strong_sectors:
            return set()

        all_codes: Set[str] = set()
        failed = 0

        def _fetch_members(sector: Dict) -> Tuple[str, Set[str]]:
            """Fetch member codes for a single sector."""
            return sector["name"], self._get_sector_members(sector["name"])

        with ThreadPoolExecutor(max_workers=25) as executor:
            futures = {executor.submit(_fetch_members, s): s for s in strong_sectors}
            try:
                for future in as_completed(futures, timeout=180):
                    try:
                        name, codes = future.result(timeout=8)
                        all_codes.update(codes)
                    except Exception as e:
                        failed += 1
                        if failed <= 3:
                            logger.debug("[SectorStrength] Failed to get members: %s", e)
            except FuturesTimeout:
                logger.warning("[SectorStrength] Member fetch timed out after 180s, returning partial results")

        logger.info(
            "[SectorStrength] Strong sector codes: %d stocks from %d sectors (%d failed)",
            len(all_codes), len(strong_sectors), failed,
        )

        self._set_cached(cache_key, all_codes)
        return all_codes

    # ------------------------------------------------------------------ realtime mode

    def _get_strong_sectors_realtime(self, top_pct: float) -> List[Dict]:
        """Fetch strong sectors in realtime mode via AkShare."""
        import akshare as ak

        pct_key = int(top_pct * 100)
        cache_key = f"sectors_realtime_{datetime.now().strftime('%Y%m%d')}_{pct_key}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            logger.info("[SectorStrength] Fetching industry sector rankings from AkShare...")
            # Fetch sector rankings with retry (East Money API can be flaky)
            df = None
            for attempt in range(3):
                try:
                    df = ak.stock_board_industry_name_em()
                    if df is not None and not df.empty:
                        break
                except Exception as e:
                    wait = (attempt + 1) * 5  # 5s, 10s, 15s
                    logger.warning(
                        "[SectorStrength] Sector ranking fetch attempt %d/3 failed: %s, retrying in %ds",
                        attempt + 1, e, wait,
                    )
                    time.sleep(wait)

            if df is None or df.empty:
                logger.warning("[SectorStrength] All 3 attempts to fetch sector rankings failed")
                return []

            # Normalize column names
            change_col = '涨跌幅'
            name_col = '板块名称'
            if change_col not in df.columns or name_col not in df.columns:
                logger.warning("[SectorStrength] Unexpected columns: %s", list(df.columns))
                return []

            df[change_col] = pd.to_numeric(df[change_col], errors='coerce')
            df = df.dropna(subset=[change_col])
            df = df.sort_values(change_col, ascending=False)

            n_top = max(1, int(len(df) * top_pct))
            top_df = df.head(n_top)

            result = [
                {"name": row[name_col], "change_pct": float(row[change_col])}
                for _, row in top_df.iterrows()
            ]

            logger.info(
                "[SectorStrength] Found %d strong sectors (top %.0f%% of %d)",
                len(result), top_pct * 100, len(df),
            )

            self._set_cached(cache_key, result)
            return result
        except Exception as e:
            logger.warning("[SectorStrength] Failed to get sector rankings: %s", e)
            return []

    # ------------------------------------------------------------------ backtest mode

    def _preload_sector_history(self, start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
        """Preload historical data for all sectors at once.

        Called once at the start of backtest. Caches sector name -> DataFrame mapping.
        Subsequent calls within the TTL window return cached data directly.
        """
        cache_key = f"sector_hist_all_{start_date}_{end_date}"
        cached = self._get_cached(cache_key, ttl=self._PRELOAD_TTL)
        if cached is not None:
            return cached

        import akshare as ak

        # Get all sector names with retry (East Money API can be flaky)
        df_names = None
        for attempt in range(3):
            try:
                df_names = ak.stock_board_industry_name_em()
                if df_names is not None and not df_names.empty:
                    break
            except Exception as e:
                wait = (attempt + 1) * 5  # 5s, 10s, 15s
                logger.warning(
                    "[SectorStrength] Preload sector names attempt %d/3 failed: %s, retrying in %ds",
                    attempt + 1, e, wait,
                )
                time.sleep(wait)

        if df_names is None or df_names.empty:
            logger.warning("[SectorStrength] All 3 attempts to fetch sector names for preload failed")
            return {}

        sector_names = df_names['板块名称'].tolist()
        sector_hist: Dict[str, pd.DataFrame] = {}

        logger.info(
            "[SectorStrength] Preloading history for %d sectors (%s ~ %s) with concurrent requests...",
            len(sector_names), start_date, end_date,
        )

        def _fetch_one(name: str) -> Tuple[str, Optional[pd.DataFrame]]:
            """Fetch history for a single sector."""
            try:
                hist = ak.stock_board_industry_hist_em(
                    symbol=name, period="日k",
                    start_date=start_date, end_date=end_date,
                    adjust="",
                )
                if hist is not None and not hist.empty:
                    return name, hist
            except Exception:
                pass
            return name, None

        # Use 10 workers for concurrent fetching
        completed = 0
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_one, name): name for name in sector_names}
            try:
                for future in as_completed(futures, timeout=300):
                    name, hist = future.result()
                    if hist is not None:
                        sector_hist[name] = hist
                    completed += 1
                    if completed % 100 == 0:
                        logger.info("[SectorStrength] Preloaded %d/%d sectors...", completed, len(sector_names))
            except FuturesTimeout:
                logger.warning(
                    "[SectorStrength] Preload timed out after 300s, got %d/%d sectors",
                    completed, len(sector_names),
                )

        logger.info("[SectorStrength] Preloaded %d sectors with history data", len(sector_hist))
        self._set_cached(cache_key, sector_hist)
        return sector_hist

    def _get_strong_sectors_backtest(self, top_pct: float, trade_date: str) -> List[Dict]:
        """Fetch strong sectors in backtest mode using preloaded historical data.

        On the first call the full history is loaded once via _preload_sector_history.
        Each subsequent call for a different trade_date only performs an in-memory lookup.
        """
        # Per-date result cache (cheap dict lookup)
        pct_key = int(top_pct * 100)
        cache_key = f"sectors_hist_{trade_date}_{pct_key}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            td = datetime.strptime(trade_date, "%Y%m%d")

            # Use a wide range that covers the full backtest period + lookback buffer.
            # The preload result is cached, so only the first call is expensive.
            preload_start = (td - timedelta(days=400)).strftime("%Y%m%d")
            preload_end = (td + timedelta(days=30)).strftime("%Y%m%d")

            sector_hist = self._preload_sector_history(preload_start, preload_end)
            if not sector_hist:
                return []

            # Calculate 5-day change for *this* trade_date from preloaded data
            sector_changes: List[Dict] = []
            target = pd.Timestamp(td)

            for name, hist in sector_hist.items():
                try:
                    date_col = '日期' if '日期' in hist.columns else 'date'
                    close_col = '收盘' if '收盘' in hist.columns else 'close'

                    if date_col not in hist.columns or close_col not in hist.columns:
                        continue

                    hist_dates = pd.to_datetime(hist[date_col])
                    mask = hist_dates <= target
                    recent = hist.loc[mask].copy()
                    recent[date_col] = hist_dates[mask]
                    recent = recent.sort_values(date_col).tail(6)  # Need 6 rows for 5-day change

                    if len(recent) >= 2:
                        closes = pd.to_numeric(recent[close_col], errors='coerce').dropna()
                        if len(closes) >= 2:
                            n = min(5, len(closes))
                            change_5d = (float(closes.iloc[-1]) / float(closes.iloc[-n]) - 1) * 100
                            sector_changes.append({"name": name, "change_pct": change_5d})
                except Exception:
                    continue

            if not sector_changes:
                return []

            # Sort by 5-day change descending
            sector_changes.sort(key=lambda x: x["change_pct"], reverse=True)
            n_top = max(1, int(len(sector_changes) * top_pct))
            result = sector_changes[:n_top]

            logger.info(
                "[SectorStrength] Backtest %s: %d strong sectors (top %.0f%% of %d)",
                trade_date, len(result), top_pct * 100, len(sector_changes),
            )

            self._set_cached(cache_key, result)
            return result
        except Exception as e:
            logger.warning("[SectorStrength] Backtest sector ranking failed: %s", e)
            return []

    # ------------------------------------------------------------------ sector members

    def preload_realtime(self, top_pct: float = 0.3) -> None:
        """Preload sector ranking and member stocks in background.

        Call at server startup to warm the cache so user requests are instant.
        """
        try:
            logger.info("[SectorStrength] Background preload started (top_pct=%.0f%%)", top_pct * 100)
            t0 = time.time()
            codes = self.get_strong_sector_codes(top_pct=top_pct)
            elapsed = time.time() - t0
            logger.info("[SectorStrength] Background preload completed: %d codes in %.1fs", len(codes), elapsed)
        except Exception as e:
            logger.warning("[SectorStrength] Background preload failed: %s", e)

    def start_periodic_refresh(self, top_pct: float = 0.3, interval_seconds: int = 3600) -> None:
        """Start a background thread that periodically refreshes sector data.

        Args:
            top_pct: Top percentage of sectors to consider strong.
            interval_seconds: Refresh interval in seconds (default: 1 hour).
        """
        def _refresh_loop():
            while True:
                time.sleep(interval_seconds)
                try:
                    logger.info("[SectorStrength] Periodic refresh started (top_pct=%.0f%%)", top_pct * 100)
                    t0 = time.time()
                    codes = self.get_strong_sector_codes(top_pct=top_pct)
                    elapsed = time.time() - t0
                    logger.info("[SectorStrength] Periodic refresh completed: %d codes in %.1fs", len(codes), elapsed)
                except Exception as e:
                    logger.warning("[SectorStrength] Periodic refresh failed: %s", e)

        t = threading.Thread(target=_refresh_loop, daemon=True, name="sector-refresh")
        t.start()
        logger.info("[SectorStrength] Periodic refresh scheduled every %ds", interval_seconds)

    # ------------------------------------------------------------------ sector members

    def _get_sector_members(self, sector_name: str) -> Set[str]:
        """Get member stock codes for a sector, with persistent cache.

        Cache key does NOT include trade_date because sector composition
        changes only on a quarterly basis at most.
        """
        cache_key = f"members_{sector_name}"
        cached = self._get_cached(cache_key, ttl=self._MEMBERS_TTL)
        if cached is not None:
            return cached

        import akshare as ak

        for attempt in range(2):
            try:
                df = ak.stock_board_industry_cons_em(symbol=sector_name)
                if df is not None and not df.empty:
                    code_col = '代码' if '代码' in df.columns else 'code'
                    if code_col in df.columns:
                        codes = set(df[code_col].astype(str).str.zfill(6).tolist())
                        self._set_cached(cache_key, codes)
                        return codes
                    break
                time.sleep(0.02)  # Rate limiting
            except Exception as e:
                if attempt < 1:
                    logger.debug("[SectorStrength] Sector members fetch attempt %d/2 failed: %s, retrying", attempt + 1, e)
                    time.sleep(3)
                    continue
                pass
        return set()
