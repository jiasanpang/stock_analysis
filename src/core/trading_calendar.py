# -*- coding: utf-8 -*-
"""
===================================
交易日历模块 (Issue #373)
===================================

职责：
1. 按市场（A股/港股/美股）判断当日是否为交易日
2. 按市场时区取“今日”日期，避免服务器 UTC 导致日期错误
3. 支持 per-stock 过滤：只分析当日开市市场的股票

依赖：exchange-calendars（可选，不可用时 fail-open）
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Optional, Set

import pandas as pd

logger = logging.getLogger(__name__)

# Exchange-calendars availability
_XCALS_AVAILABLE = False
try:
    import exchange_calendars as xcals
    _XCALS_AVAILABLE = True
except ImportError:
    logger.warning(
        "exchange-calendars not installed; trading day check disabled. "
        "Run: pip install exchange-calendars"
    )

# Market -> exchange code (exchange-calendars)
MARKET_EXCHANGE = {"cn": "XSHG", "hk": "XHKG", "us": "XNYS"}

# Market -> IANA timezone for "today"
MARKET_TIMEZONE = {
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
}


def get_calendar_today_for_market(market: Optional[str]) -> date:
    """
    Calendar 'today' in the market's timezone (Issue: intraday daily-bar refresh).

    Use this for StockDaily row keys instead of host date.today() when the server
    runs in UTC or another TZ than the listing market.
    """
    if not market:
        return date.today()
    tz_name = MARKET_TIMEZONE.get(market)
    if not tz_name:
        return date.today()
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz_name)).date()


def _cn_regular_auction_session_open(now_shanghai: datetime) -> bool:
    """True during A-share morning or afternoon auction (not lunch break)."""
    from zoneinfo import ZoneInfo

    sh = ZoneInfo("Asia/Shanghai")
    if now_shanghai.tzinfo is None:
        now_shanghai = now_shanghai.replace(tzinfo=sh)
    else:
        now_shanghai = now_shanghai.astimezone(sh)
    t = now_shanghai.time()
    morning = time(9, 30) <= t <= time(11, 30)
    afternoon = time(13, 0) <= t < time(15, 0)
    return morning or afternoon


def should_bypass_daily_fetch_cache_cn(
    row_date: date,
    row_updated_at: Optional[datetime],
    now_shanghai: datetime,
) -> bool:
    """
    When True, pipeline should refetch daily history even if has_today_data is True.

    Covers:
    - Intraday partial daily bars (scheduled run at e.g. 14:30 after an earlier save).
    - Rows written before ~15:05 Shanghai after a trading day (still missing final OHLCV).
    """
    from zoneinfo import ZoneInfo

    sh = ZoneInfo("Asia/Shanghai")
    if now_shanghai.tzinfo is None:
        now_shanghai = now_shanghai.replace(tzinfo=sh)
    else:
        now_shanghai = now_shanghai.astimezone(sh)

    if now_shanghai.date() != row_date:
        return True

    if not is_market_open("cn", row_date):
        return False

    if _cn_regular_auction_session_open(now_shanghai):
        return True

    if row_updated_at is None:
        return True

    ru = row_updated_at
    if ru.tzinfo is None:
        # Naive timestamps follow host TZ; deployments set TZ=Asia/Shanghai (see docker-compose).
        ru = ru.replace(tzinfo=sh)
    else:
        ru = ru.astimezone(sh)

    session_end = datetime.combine(row_date, time(15, 0), tzinfo=sh)
    return ru < session_end + timedelta(minutes=5)


def get_market_for_stock(code: str) -> Optional[str]:
    """
    Infer market region for a stock code.

    Returns:
        'cn' | 'hk' | 'us' | None (None = unrecognized, fail-open: treat as open)
    """
    if not code or not isinstance(code, str):
        return None
    code = (code or "").strip().upper()

    from data_provider import is_us_stock_code, is_us_index_code, is_hk_stock_code

    if is_us_stock_code(code) or is_us_index_code(code):
        return "us"
    if is_hk_stock_code(code):
        return "hk"
    # A-share: 6-digit numeric
    if code.isdigit() and len(code) == 6:
        return "cn"
    return None


def is_market_open(market: str, check_date: date) -> bool:
    """
    Check if the given market is open on the given date.

    Fail-open: returns True if exchange-calendars unavailable or date out of range.

    Args:
        market: 'cn' | 'hk' | 'us'
        check_date: Date to check

    Returns:
        True if trading day (or fail-open), False otherwise
    """
    if not _XCALS_AVAILABLE:
        return True
    ex = MARKET_EXCHANGE.get(market)
    if not ex:
        return True
    try:
        cal = xcals.get_calendar(ex)
        session = datetime(check_date.year, check_date.month, check_date.day)
        return cal.is_session(session)
    except Exception as e:
        logger.warning("trading_calendar.is_market_open fail-open: %s", e)
        return True


def get_last_trading_day(market: str, check_date: date) -> Optional[date]:
    """
    Get the most recent trading day before the given date.

    When check_date is a non-trading day (e.g. weekend), returns the previous
    trading day (e.g. Friday). Used by picker to fetch daily data on weekends.

    Args:
        market: 'cn' | 'hk' | 'us'
        check_date: Reference date

    Returns:
        The last trading day date, or None if exchange-calendars unavailable
    """
    if not _XCALS_AVAILABLE:
        return None
    ex = MARKET_EXCHANGE.get(market)
    if not ex:
        return None
    try:
        cal = xcals.get_calendar(ex)
        ts = datetime(check_date.year, check_date.month, check_date.day)
        session = cal.minute_to_session(pd.Timestamp(ts), direction="previous")
        return session.date() if session is not None else None
    except Exception as e:
        logger.warning("trading_calendar.get_last_trading_day: %s", e)
        return None


def get_open_markets_today() -> Set[str]:
    """
    Get markets that are open today (by each market's local timezone).

    Returns:
        Set of market keys ('cn', 'hk', 'us') that are trading today
    """
    if not _XCALS_AVAILABLE:
        return {"cn", "hk", "us"}
    result: Set[str] = set()
    from zoneinfo import ZoneInfo
    for mkt, tz_name in MARKET_TIMEZONE.items():
        try:
            tz = ZoneInfo(tz_name)
            today = datetime.now(tz).date()
            if is_market_open(mkt, today):
                result.add(mkt)
        except Exception as e:
            logger.warning("get_open_markets_today fail-open for %s: %s", mkt, e)
            result.add(mkt)
    return result


def compute_effective_region(
    config_region: str, open_markets: Set[str]
) -> Optional[str]:
    """
    Compute effective market review region given config and open markets.

    Args:
        config_region: From MARKET_REVIEW_REGION ('cn' | 'us' | 'both')
        open_markets: Markets open today

    Returns:
        None: caller uses config default (check disabled)
        '': all relevant markets closed, skip market review
        'cn' | 'us' | 'both': effective subset for today
    """
    if config_region not in ("cn", "us", "both"):
        config_region = "cn"
    if config_region == "cn":
        return "cn" if "cn" in open_markets else ""
    if config_region == "us":
        return "us" if "us" in open_markets else ""
    # both
    parts = []
    if "cn" in open_markets:
        parts.append("cn")
    if "us" in open_markets:
        parts.append("us")
    if not parts:
        return ""
    return "both" if len(parts) == 2 else parts[0]
