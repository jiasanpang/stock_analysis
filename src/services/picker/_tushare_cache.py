# -*- coding: utf-8 -*-
"""Tushare API local parquet cache for backtest hot-loops.

Use ``wrap_api(api)`` to get a transparent proxy that caches per
trade_date results to ``data/cache/tushare/{api}/{key}.parquet``.

Cached methods:
  - daily(trade_date=td)
  - daily_basic(trade_date=td, fields=...)
  - moneyflow(trade_date=td)
  - moneyflow_hsgt(start_date=td, end_date=td)
  - index_daily(ts_code=..., start_date=..., end_date=...)
  - sw_daily(trade_date=td, fields=...)
  - stock_basic(fields=...)         # cached forever, key=fields hash
  - top_list(trade_date=td)         # 龙虎榜
  - limit_list_d(trade_date=td)     # 涨停池明细
  - trade_cal(exchange=..., start_date=..., end_date=...)

Other methods pass through to the real api.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Any, Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_ROOT = os.path.join("data", "cache", "tushare")
_LOCK = threading.Lock()
_MEM: dict[str, pd.DataFrame] = {}
_STATS = {"mem": 0, "disk": 0, "localdb": 0, "miss": 0}

_USE_LOCAL_DB = os.environ.get("STOCK_USE_LOCAL_DB", "1") == "1"
_LOCAL_DB = None


def _get_local_db():
    global _LOCAL_DB
    if not _USE_LOCAL_DB:
        return None
    if _LOCAL_DB is not None:
        return _LOCAL_DB
    try:
        from src.services.local_db import default_db

        _LOCAL_DB = default_db()
    except Exception as e:
        logger.warning("[ts-cache] LocalStockDB unavailable: %s", e)
    return _LOCAL_DB


def _project_fields(df: Optional[pd.DataFrame], fields: Optional[str]) -> Optional[pd.DataFrame]:
    """Mimic Tushare's ``fields="a,b,c"`` projection on a DataFrame."""
    if df is None or df.empty or not fields:
        return df
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    keep = [c for c in wanted if c in df.columns]
    if not keep:
        return df
    return df[keep].copy()


def _localdb_lookup(api: str, **kw) -> Optional[pd.DataFrame]:
    """Try to satisfy a Tushare-style call from the local warehouse.

    Returns ``None`` when LocalDB doesn't have the row(s); the caller
    should fall through to disk cache / live fetch.
    """
    db = _get_local_db()
    if db is None:
        return None
    try:
        td = kw.get("trade_date")
        sd = kw.get("start_date")
        ed = kw.get("end_date")
        if api == "daily" and td:
            return _project_fields(db.get_market_daily(td), kw.get("fields"))
        if api == "daily_basic" and td:
            return _project_fields(db.get_market_daily_basic(td), kw.get("fields"))
        if api == "moneyflow" and td and not kw.get("ts_code"):
            return _project_fields(db.get_market_moneyflow(td), kw.get("fields"))
        if api == "moneyflow_hsgt" and sd:
            return _project_fields(db.get_moneyflow_hsgt(sd, ed or sd), kw.get("fields"))
        if api == "index_daily":
            ts_code = kw.get("ts_code")
            if ts_code:
                return _project_fields(db.get_index_daily(ts_code, sd, ed), kw.get("fields"))
        if api == "top_list" and td:
            return _project_fields(db.get_top_list(td, td), kw.get("fields"))
        if api == "limit_list_d" and td:
            return _project_fields(db.get_limit_list(td, td), kw.get("fields"))
        if api == "stock_basic":
            return _project_fields(db.get_stock_basic(), kw.get("fields"))
        if api == "trade_cal":
            ex = kw.get("exchange", "SSE")
            return _project_fields(db.get_trade_cal(ex, sd, ed), kw.get("fields"))
    except Exception as e:
        logger.debug("[ts-cache] LocalDB lookup %s failed: %s", api, e)
    return None


def _path(api: str, key: str) -> str:
    return os.path.join(CACHE_ROOT, api, f"{key}.parquet")


def _load_disk(api: str, key: str) -> Optional[pd.DataFrame]:
    p = _path(api, key)
    if not os.path.exists(p):
        return None
    try:
        return pd.read_parquet(p)
    except Exception as e:
        logger.warning("[ts-cache] read %s/%s failed: %s", api, key, e)
        return None


def _save_disk(api: str, key: str, df: pd.DataFrame) -> None:
    p = _path(api, key)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        df.to_parquet(p, index=False)
    except Exception as e:
        logger.warning("[ts-cache] write %s/%s failed: %s", api, key, e)


def _get(
    api: str,
    key: str,
    fetch: Callable[[], Optional[pd.DataFrame]],
    localdb_kwargs: Optional[dict] = None,
) -> Optional[pd.DataFrame]:
    cache_key = f"{api}:{key}"
    with _LOCK:
        if cache_key in _MEM:
            _STATS["mem"] += 1
            return _MEM[cache_key]

    if localdb_kwargs is not None:
        local = _localdb_lookup(api, **localdb_kwargs)
        if local is not None and not local.empty:
            with _LOCK:
                _MEM[cache_key] = local
                _STATS["localdb"] += 1
            return local

    disk = _load_disk(api, key)
    if disk is not None:
        with _LOCK:
            _MEM[cache_key] = disk
            _STATS["disk"] += 1
        return disk
    try:
        df = fetch()
    except Exception as e:
        logger.warning("[ts-cache] %s/%s fetch error: %s; caching empty", api, key, e)
        df = pd.DataFrame()
    if df is None:
        df = pd.DataFrame()
    with _LOCK:
        _MEM[cache_key] = df
        _STATS["miss"] += 1
    _save_disk(api, key, df)
    return df


def _hash(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:10]


class CachedTushareAPI:
    """Transparent wrapper that caches per-trade_date Tushare results."""

    def __init__(self, real_api):
        self._api = real_api

    def daily(self, **kw):
        td = kw.get("trade_date")
        if td:
            return _get("daily", td, lambda: self._api.daily(**kw), localdb_kwargs=kw)
        return self._api.daily(**kw)

    def daily_basic(self, **kw):
        td = kw.get("trade_date")
        if td:
            fields = kw.get("fields", "")
            key = f"{td}_{_hash(fields)}" if fields else td
            return _get("daily_basic", key, lambda: self._api.daily_basic(**kw), localdb_kwargs=kw)
        return self._api.daily_basic(**kw)

    def moneyflow(self, **kw):
        td = kw.get("trade_date")
        if td and not kw.get("ts_code"):
            return _get("moneyflow", td, lambda: self._api.moneyflow(**kw), localdb_kwargs=kw)
        return self._api.moneyflow(**kw)

    def moneyflow_hsgt(self, **kw):
        sd = kw.get("start_date")
        ed = kw.get("end_date")
        if sd and ed and sd == ed:
            return _get("moneyflow_hsgt", sd, lambda: self._api.moneyflow_hsgt(**kw), localdb_kwargs=kw)
        if sd and ed:
            return _get("moneyflow_hsgt", f"{sd}_{ed}", lambda: self._api.moneyflow_hsgt(**kw), localdb_kwargs=kw)
        return self._api.moneyflow_hsgt(**kw)

    def index_daily(self, **kw):
        ts_code = kw.get("ts_code", "?")
        sd = kw.get("start_date", "")
        ed = kw.get("end_date", "")
        key = f"{ts_code}_{sd}_{ed}"
        return _get("index_daily", key, lambda: self._api.index_daily(**kw), localdb_kwargs=kw)

    def sw_daily(self, **kw):
        td = kw.get("trade_date")
        if td:
            fields = kw.get("fields", "")
            key = f"{td}_{_hash(fields)}" if fields else td
            return _get("sw_daily", key, lambda: self._api.sw_daily(**kw))
        return self._api.sw_daily(**kw)

    def stock_basic(self, **kw):
        fields = kw.get("fields", "all")
        return _get("stock_basic", _hash(fields), lambda: self._api.stock_basic(**kw), localdb_kwargs=kw)

    def top_list(self, **kw):
        td = kw.get("trade_date")
        if td:
            return _get("top_list", td, lambda: self._api.top_list(**kw), localdb_kwargs=kw)
        return self._api.top_list(**kw)

    def limit_list_d(self, **kw):
        td = kw.get("trade_date")
        if td:
            return _get("limit_list_d", td,
                        lambda: self._api.limit_list_d(**kw), localdb_kwargs=kw)
        return self._api.limit_list_d(**kw)

    def trade_cal(self, **kw):
        ex = kw.get("exchange", "SSE")
        sd = kw.get("start_date", "")
        ed = kw.get("end_date", "")
        key = f"{ex}_{sd}_{ed}"
        return _get("trade_cal", key, lambda: self._api.trade_cal(**kw), localdb_kwargs=kw)

    # Pass-through for anything else
    def __getattr__(self, name: str):
        return getattr(self._api, name)


def wrap_api(real_api):
    """Wrap a real Tushare pro api with disk caching."""
    if real_api is None:
        return None
    if isinstance(real_api, CachedTushareAPI):
        return real_api
    return CachedTushareAPI(real_api)


def stats() -> dict:
    return dict(_STATS)
