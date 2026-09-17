"""Pre-warm the local stock data warehouse from Tushare.

Usage::

    python scripts/preload_local_db.py --start 20250515 --end 20260515

Defaults to last ~250 trading days. Idempotent: rerun to fill gaps.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.local_db import default_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


INDEX_CODES = (
    "000001.SH",  # SSE composite
    "000300.SH",  # CSI 300
    "000905.SH",  # CSI 500
    "399001.SZ",  # SZSE composite
    "399006.SZ",  # ChiNext
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20250515", help="YYYYMMDD")
    parser.add_argument("--end", default="20260515", help="YYYYMMDD")
    parser.add_argument(
        "--tables",
        default="all",
        help="comma list: daily,daily_basic,moneyflow,index,top_list,limit_list,"
             "hsgt,basic,cal,all",
    )
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="seconds to sleep between Tushare calls (rate limit)")
    args = parser.parse_args()

    requested = (
        {"daily", "daily_basic", "moneyflow", "index", "top_list", "limit_list",
         "hsgt", "basic", "cal"}
        if args.tables == "all"
        else {t.strip() for t in args.tables.split(",") if t.strip()}
    )

    db = default_db()
    t_total = time.time()

    if "cal" in requested:
        logger.info("=== sync trade_cal (covering wide history) ===")
        r = db.sync_trade_cal(start_date="20100101", end_date=args.end)
        logger.info("  rows=%d errors=%d %.1fs", r.rows_added, r.errors, r.elapsed_s)

    if "basic" in requested:
        logger.info("=== sync stock_basic ===")
        r = db.sync_stock_basic()
        logger.info("  rows=%d errors=%d %.1fs", r.rows_added, r.errors, r.elapsed_s)

    market_tables = []
    if "daily" in requested:
        market_tables.append("daily")
    if "daily_basic" in requested:
        market_tables.append("daily_basic")
    if "moneyflow" in requested:
        market_tables.append("moneyflow")
    if market_tables:
        logger.info("=== sync market %s for %s..%s ===",
                    market_tables, args.start, args.end)
        reports = db.sync_market_daily_range(
            args.start, args.end, tables=market_tables, sleep_s=args.sleep,
        )
        for r in reports:
            logger.info(
                "  %s: dates=%d rows=%d errors=%d %.1fs",
                r.table, r.dates_synced, r.rows_added, r.errors, r.elapsed_s,
            )

    if "index" in requested:
        logger.info("=== sync index_daily for %s ===", list(INDEX_CODES))
        r = db.sync_index_daily(INDEX_CODES, args.start, args.end)
        logger.info("  rows=%d errors=%d %.1fs", r.rows_added, r.errors, r.elapsed_s)

    if "top_list" in requested:
        logger.info("=== sync top_list ===")
        r = db.sync_top_list(args.start, args.end)
        logger.info("  rows=%d errors=%d %.1fs", r.rows_added, r.errors, r.elapsed_s)

    if "limit_list" in requested:
        logger.info("=== sync limit_list_d ===")
        r = db.sync_limit_list(args.start, args.end)
        logger.info("  rows=%d errors=%d %.1fs", r.rows_added, r.errors, r.elapsed_s)

    if "hsgt" in requested:
        logger.info("=== sync moneyflow_hsgt ===")
        r = db.sync_moneyflow_hsgt(args.start, args.end)
        logger.info("  rows=%d errors=%d %.1fs", r.rows_added, r.errors, r.elapsed_s)

    logger.info("=== TOTAL %.1fs ===", time.time() - t_total)
    logger.info("=== meta snapshot ===")
    for k, v in db.meta().items():
        logger.info("  %s: %s", k, v)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
