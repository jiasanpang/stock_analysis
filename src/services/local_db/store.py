"""LocalStockDB — local parquet warehouse for A-share data.

Purpose
-------
Replace ad-hoc on-the-fly Tushare caching with a persistent, queryable
local warehouse. Backtests read from local parquet (ms-level), only
hitting Tushare via :meth:`sync` when explicitly asked.

Storage layout
--------------
``data/local_db/``::

    daily/<ts_code>.parquet           # OHLCV per stock (append-only)
    daily_basic/<ts_code>.parquet     # turnover_rate / pe / pb / total_mv
    moneyflow/<ts_code>.parquet       # main / retail net flows
    index_daily/<ts_code>.parquet     # index OHLCV (CSI300 etc)
    moneyflow_hsgt/_all.parquet       # north-bound, single table (date-keyed)
    top_list/_all.parquet             # dragon list, single table
    stock_basic/_all.parquet          # static metadata
    trade_cal/_all.parquet            # SSE trading calendar
    _meta.json                         # per-table latest sync date

Concurrency
-----------
Writers serialise via a per-table :class:`threading.Lock`. Readers are
lock-free (parquet append + atomic rename pattern).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path(os.environ.get("STOCK_LOCAL_DB", "data/local_db"))


# ---------------------------------------------------------------------------
# Per-stock tables (sharded by ts_code)
# ---------------------------------------------------------------------------

PER_STOCK_TABLES = ("daily", "daily_basic", "moneyflow", "index_daily")

# Single-file tables (unsharded; ranges queried via filters)
SINGLE_TABLES = ("moneyflow_hsgt", "top_list", "stock_basic", "trade_cal",
                 "limit_list_d")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm_date(d: str) -> str:
    """YYYY-MM-DD or YYYYMMDD → YYYYMMDD (Tushare native)."""
    if not d:
        return ""
    s = str(d).replace("-", "")
    return s[:8]


def _to_ts_code(code: str) -> str:
    """000001 → 000001.SZ ; 600000 → 600000.SH ; preserves already-suffixed codes."""
    if not code:
        return code
    if "." in code:
        return code
    if code.startswith(("60", "68", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _from_ts_code(ts_code: str) -> str:
    return ts_code.split(".")[0] if ts_code and "." in ts_code else ts_code


# ---------------------------------------------------------------------------
# LocalStockDB
# ---------------------------------------------------------------------------


@dataclass
class SyncReport:
    table: str
    rows_added: int
    dates_synced: int
    elapsed_s: float
    errors: int = 0


class LocalStockDB:
    """Local parquet warehouse. Single instance per process is fine."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or DEFAULT_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self._meta_path = self.root / "_meta.json"
        self._lock = threading.Lock()
        # Per-table fine-grained locks for concurrent sync calls.
        self._table_locks: Dict[str, threading.Lock] = {
            t: threading.Lock() for t in (*PER_STOCK_TABLES, *SINGLE_TABLES)
        }
        self._tushare_api = None

    # ------------------------------------------------------------------
    # Tushare client (lazy)
    # ------------------------------------------------------------------

    def _api(self):
        if self._tushare_api is None:
            from dotenv import load_dotenv

            load_dotenv()
            import tushare as ts

            token = os.environ.get("TUSHARE_TOKEN")
            if not token:
                raise RuntimeError("TUSHARE_TOKEN not set")
            ts.set_token(token)
            self._tushare_api = ts.pro_api()
        return self._tushare_api

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _stock_path(self, table: str, ts_code: str) -> Path:
        return self.root / table / f"{ts_code}.parquet"

    def _single_path(self, table: str) -> Path:
        return self.root / table / "_all.parquet"

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def _read_meta(self) -> Dict[str, Any]:
        if not self._meta_path.exists():
            return {}
        try:
            return json.loads(self._meta_path.read_text())
        except Exception:
            return {}

    def _write_meta(self, meta: Dict[str, Any]) -> None:
        with self._lock:
            self._meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    def meta(self) -> Dict[str, Any]:
        return self._read_meta()

    # ------------------------------------------------------------------
    # Read APIs (used by backtest hot-loop)
    # ------------------------------------------------------------------

    def get_daily(
        self,
        code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        return self._read_per_stock("daily", code, start_date, end_date)

    def get_daily_basic(
        self, code: str, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> pd.DataFrame:
        return self._read_per_stock("daily_basic", code, start_date, end_date)

    def get_moneyflow(
        self, code: str, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> pd.DataFrame:
        return self._read_per_stock("moneyflow", code, start_date, end_date)

    def get_index_daily(
        self, code: str, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> pd.DataFrame:
        return self._read_per_stock("index_daily", code, start_date, end_date)

    def get_market_daily(self, trade_date: str) -> pd.DataFrame:
        """Full-market snapshot for a single trade_date (read from per-stock files).

        Slow if the universe is large; prefer pre-aggregating market-level
        queries via :meth:`api_daily` shim instead.
        """
        return self._read_market("daily", trade_date)

    def get_market_daily_basic(self, trade_date: str) -> pd.DataFrame:
        return self._read_market("daily_basic", trade_date)

    def get_market_moneyflow(self, trade_date: str) -> pd.DataFrame:
        return self._read_market("moneyflow", trade_date)

    def get_top_list(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> pd.DataFrame:
        return self._read_single("top_list", start_date, end_date)

    def get_limit_list(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> pd.DataFrame:
        return self._read_single("limit_list_d", start_date, end_date)

    def get_moneyflow_hsgt(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> pd.DataFrame:
        return self._read_single("moneyflow_hsgt", start_date, end_date)

    def get_stock_basic(self) -> pd.DataFrame:
        p = self._single_path("stock_basic")
        if not p.exists():
            return pd.DataFrame()
        return pd.read_parquet(p)

    def get_trade_cal(
        self,
        exchange: str = "SSE",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        p = self._single_path("trade_cal")
        if not p.exists():
            return pd.DataFrame()
        df = pd.read_parquet(p)
        if "exchange" in df.columns:
            df = df[df["exchange"] == exchange]
        if start_date:
            df = df[df["cal_date"] >= _norm_date(start_date)]
        if end_date:
            df = df[df["cal_date"] <= _norm_date(end_date)]
        return df.reset_index(drop=True)

    def universe(self, table: str = "daily") -> List[str]:
        d = self.root / table
        if not d.exists():
            return []
        return sorted(
            f.stem for f in d.glob("*.parquet") if not f.stem.startswith("_")
        )

    # ------------------------------------------------------------------
    # Internal readers
    # ------------------------------------------------------------------

    def _read_per_stock(
        self,
        table: str,
        code: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> pd.DataFrame:
        ts_code = _to_ts_code(code)
        p = self._stock_path(table, ts_code)
        if not p.exists():
            return pd.DataFrame()
        df = pd.read_parquet(p)
        if df.empty:
            return df
        date_col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        if start_date:
            df = df[df[date_col] >= _norm_date(start_date)]
        if end_date:
            df = df[df[date_col] <= _norm_date(end_date)]
        return df.reset_index(drop=True)

    def _read_market(self, table: str, trade_date: str) -> pd.DataFrame:
        td = _norm_date(trade_date)
        # Fast path: by-date sharded mirror (single file, ~10-50ms).
        by_date = self.root / f"{table}_by_date" / f"{td}.parquet"
        if by_date.exists():
            try:
                return pd.read_parquet(by_date)
            except Exception:
                pass
        # Fallback: scan all per-stock parquet files (~5-10s for 5400 stocks).
        chunks: List[pd.DataFrame] = []
        d = self.root / table
        if not d.exists():
            return pd.DataFrame()
        for f in d.glob("*.parquet"):
            try:
                df = pd.read_parquet(f)
                if "trade_date" in df.columns:
                    sub = df[df["trade_date"] == td]
                    if not sub.empty:
                        chunks.append(sub)
            except Exception:
                continue
        return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()

    def _read_single(
        self, table: str, start_date: Optional[str], end_date: Optional[str]
    ) -> pd.DataFrame:
        p = self._single_path(table)
        if not p.exists():
            return pd.DataFrame()
        df = pd.read_parquet(p)
        if df.empty:
            return df
        date_col = "trade_date" if "trade_date" in df.columns else None
        if not date_col:
            return df
        if start_date:
            df = df[df[date_col] >= _norm_date(start_date)]
        if end_date:
            df = df[df[date_col] <= _norm_date(end_date)]
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Sync APIs (write path)
    # ------------------------------------------------------------------

    def sync_market_daily_range(
        self,
        start_date: str,
        end_date: str,
        tables: Iterable[str] = ("daily", "daily_basic", "moneyflow"),
        sleep_s: float = 0.0,
    ) -> List[SyncReport]:
        """Pull full-market data per trade_date, shard to per-stock parquet.

        For each trading day in [start, end], call ``api.<table>(trade_date=)``
        and append rows to the corresponding per-stock parquet file.

        Idempotent: re-syncing an already-cached date is a no-op (rows are
        deduped on (trade_date)).
        """
        api = self._api()
        cal = self._fetch_trade_cal(start_date, end_date)
        if cal.empty:
            logger.warning("[LocalDB] empty trade_cal; refusing to sync")
            return []

        sessions = cal[cal["is_open"] == 1]["cal_date"].sort_values().tolist()
        logger.info(
            "[LocalDB] sync_market_daily_range %s..%s tables=%s sessions=%d",
            start_date, end_date, list(tables), len(sessions),
        )

        reports: List[SyncReport] = []
        for table in tables:
            t0 = time.time()
            rows_added = 0
            errors = 0
            (self.root / table).mkdir(parents=True, exist_ok=True)
            # Per-table buffer: ts_code -> list[DataFrame]
            buffer: Dict[str, List[pd.DataFrame]] = {}
            for td in sessions:
                try:
                    df = self._call_api(api, table, trade_date=td)
                    if df is None or df.empty:
                        continue
                    df.columns = [c.lower() for c in df.columns]
                    if "ts_code" not in df.columns:
                        continue
                    if "trade_date" not in df.columns:
                        df["trade_date"] = td
                    rows_added += len(df)
                    for ts_code, sub in df.groupby("ts_code"):
                        buffer.setdefault(ts_code, []).append(sub)
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                except Exception as e:
                    errors += 1
                    logger.warning("[LocalDB] %s %s failed: %s", table, td, e)

            self._flush_per_stock(table, buffer)

            elapsed = time.time() - t0
            reports.append(SyncReport(
                table=table, rows_added=rows_added,
                dates_synced=len(sessions), elapsed_s=elapsed, errors=errors,
            ))
            logger.info(
                "[LocalDB] %s synced %d sessions, +%d rows in %.1fs (%d errors)",
                table, len(sessions), rows_added, elapsed, errors,
            )
            self._update_meta(table, end_date)

        return reports

    def sync_index_daily(
        self,
        index_codes: Iterable[str],
        start_date: str,
        end_date: str,
    ) -> SyncReport:
        api = self._api()
        t0 = time.time()
        rows_added = 0
        errors = 0
        (self.root / "index_daily").mkdir(parents=True, exist_ok=True)
        buffer: Dict[str, List[pd.DataFrame]] = {}
        for code in index_codes:
            try:
                df = api.index_daily(
                    ts_code=code,
                    start_date=_norm_date(start_date),
                    end_date=_norm_date(end_date),
                )
                if df is None or df.empty:
                    continue
                df.columns = [c.lower() for c in df.columns]
                rows_added += len(df)
                buffer[code] = [df]
            except Exception as e:
                errors += 1
                logger.warning("[LocalDB] index_daily %s failed: %s", code, e)

        self._flush_per_stock("index_daily", buffer)
        elapsed = time.time() - t0
        self._update_meta("index_daily", end_date)
        return SyncReport(
            table="index_daily", rows_added=rows_added,
            dates_synced=0, elapsed_s=elapsed, errors=errors,
        )

    def sync_top_list(self, start_date: str, end_date: str) -> SyncReport:
        return self._sync_single_by_date("top_list", start_date, end_date)

    def sync_limit_list(self, start_date: str, end_date: str) -> SyncReport:
        return self._sync_single_by_date("limit_list_d", start_date, end_date)

    def sync_moneyflow_hsgt(self, start_date: str, end_date: str) -> SyncReport:
        return self._sync_single_by_date("moneyflow_hsgt", start_date, end_date)

    def sync_stock_basic(self) -> SyncReport:
        api = self._api()
        t0 = time.time()
        rows = 0
        errors = 0
        try:
            df = api.stock_basic(
                exchange="", list_status="L",
                fields="ts_code,symbol,name,area,industry,market,list_date",
            )
            if df is not None and not df.empty:
                df.columns = [c.lower() for c in df.columns]
                rows = len(df)
                p = self._single_path("stock_basic")
                p.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(p, index=False)
        except Exception as e:
            errors = 1
            logger.warning("[LocalDB] stock_basic failed: %s", e)
        elapsed = time.time() - t0
        self._update_meta("stock_basic", time.strftime("%Y%m%d"))
        return SyncReport("stock_basic", rows, 0, elapsed, errors)

    def sync_trade_cal(
        self,
        exchanges: Iterable[str] = ("SSE",),
        start_date: str = "20100101",
        end_date: Optional[str] = None,
    ) -> SyncReport:
        api = self._api()
        t0 = time.time()
        end_date = end_date or time.strftime("%Y%m%d")
        rows = 0
        errors = 0
        chunks: List[pd.DataFrame] = []
        for ex in exchanges:
            try:
                df = api.trade_cal(
                    exchange=ex,
                    start_date=_norm_date(start_date),
                    end_date=_norm_date(end_date),
                )
                if df is not None and not df.empty:
                    df.columns = [c.lower() for c in df.columns]
                    if "exchange" not in df.columns:
                        df["exchange"] = ex
                    chunks.append(df)
                    rows += len(df)
            except Exception as e:
                errors += 1
                logger.warning("[LocalDB] trade_cal %s failed: %s", ex, e)

        if chunks:
            combined = pd.concat(chunks, ignore_index=True)
            p = self._single_path("trade_cal")
            p.parent.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(p, index=False)
        elapsed = time.time() - t0
        self._update_meta("trade_cal", end_date)
        return SyncReport("trade_cal", rows, 0, elapsed, errors)

    # ------------------------------------------------------------------
    # Internal write helpers
    # ------------------------------------------------------------------

    def _call_api(self, api, table: str, **kwargs) -> Optional[pd.DataFrame]:
        fn = getattr(api, table, None)
        if fn is None:
            raise AttributeError(f"tushare api has no method {table}")
        return fn(**kwargs)

    def _flush_per_stock(
        self, table: str, buffer: Dict[str, List[pd.DataFrame]]
    ) -> None:
        """Append-and-dedupe rows into per-stock parquet files."""
        if not buffer:
            return
        lock = self._table_locks[table]
        with lock:
            (self.root / table).mkdir(parents=True, exist_ok=True)
            for ts_code, frames in buffer.items():
                p = self._stock_path(table, ts_code)
                new_df = pd.concat(frames, ignore_index=True)
                if p.exists():
                    try:
                        old_df = pd.read_parquet(p)
                        merged = pd.concat([old_df, new_df], ignore_index=True)
                    except Exception:
                        merged = new_df
                else:
                    merged = new_df
                if "trade_date" in merged.columns:
                    merged = merged.drop_duplicates(subset=["trade_date"], keep="last")
                    merged = merged.sort_values("trade_date").reset_index(drop=True)
                tmp = p.with_suffix(".parquet.tmp")
                merged.to_parquet(tmp, index=False)
                tmp.replace(p)

    def _sync_single_by_date(
        self, table: str, start_date: str, end_date: str
    ) -> SyncReport:
        api = self._api()
        cal = self._fetch_trade_cal(start_date, end_date)
        sessions = cal[cal["is_open"] == 1]["cal_date"].sort_values().tolist()
        t0 = time.time()
        rows_added = 0
        errors = 0
        chunks: List[pd.DataFrame] = []
        for td in sessions:
            try:
                df = self._call_api(api, table, trade_date=td)
                if df is not None and not df.empty:
                    df.columns = [c.lower() for c in df.columns]
                    if "trade_date" not in df.columns:
                        df["trade_date"] = td
                    chunks.append(df)
                    rows_added += len(df)
            except Exception as e:
                errors += 1
                logger.warning("[LocalDB] %s %s failed: %s", table, td, e)

        if chunks:
            new_df = pd.concat(chunks, ignore_index=True)
            p = self._single_path(table)
            p.parent.mkdir(parents=True, exist_ok=True)
            with self._table_locks[table]:
                if p.exists():
                    try:
                        old_df = pd.read_parquet(p)
                        merged = pd.concat([old_df, new_df], ignore_index=True)
                    except Exception:
                        merged = new_df
                else:
                    merged = new_df
                merged = merged.drop_duplicates(
                    subset=[c for c in merged.columns if c in ("trade_date", "ts_code")],
                    keep="last",
                ).reset_index(drop=True)
                tmp = p.with_suffix(".parquet.tmp")
                merged.to_parquet(tmp, index=False)
                tmp.replace(p)
        elapsed = time.time() - t0
        self._update_meta(table, end_date)
        return SyncReport(table, rows_added, len(sessions), elapsed, errors)

    def _fetch_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Use cached trade_cal if available, else fetch fresh."""
        cached = self.get_trade_cal("SSE", start_date, end_date)
        if not cached.empty:
            return cached
        api = self._api()
        df = api.trade_cal(
            exchange="SSE",
            start_date=_norm_date(start_date),
            end_date=_norm_date(end_date),
        )
        if df is None or df.empty:
            return pd.DataFrame()
        df.columns = [c.lower() for c in df.columns]
        return df

    def _update_meta(self, table: str, latest_date: str) -> None:
        meta = self._read_meta()
        meta[table] = {
            "latest_date": _norm_date(latest_date),
            "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._write_meta(meta)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_default: Optional[LocalStockDB] = None
_default_lock = threading.Lock()


def default_db() -> LocalStockDB:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = LocalStockDB()
    return _default


__all__ = ["LocalStockDB", "default_db", "SyncReport"]
