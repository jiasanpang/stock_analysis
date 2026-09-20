# -*- coding: utf-8 -*-
"""
Spot/daily data fetching, Tushare bulk paths and post-fetch normalisation.

Mixins inside this sub-package never import from each other; they all live
on the same ``StockScreener`` MRO, so ``self.<other_method>`` resolves at
runtime regardless of which mixin defined it.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import as_completed, TimeoutError as FuturesTimeout
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from src._concurrency import get_executor, run_with_timeout
from src.config import get_config
from src.services.picker.constants import (
    _resolve_fallback_trade_date,
    get_tushare_api,
)

logger = logging.getLogger(__name__)


class _DataFetchMixin:
    """Mixin: spot/Tushare batch fetch, realtime overlay, normalisation."""

    def _fetch_daily_batch(
        self,
        requests: List[Tuple[str, Optional[str], Optional[str], int]],
        max_workers: int = 5,
        total_timeout: float = 120.0,
    ) -> Dict[Tuple[str, str, str, int], Tuple[pd.DataFrame, str]]:
        """Fetch get_daily_data for multiple (code, start, end, days) in parallel."""
        if not self._data_manager or not requests:
            return {}

        def _key(c: str, s: Optional[str], e: Optional[str], d: int) -> Tuple[str, str, str, int]:
            return (c, s or "", e or "", d)

        def _fetch(args: Tuple[str, Optional[str], Optional[str], int]):
            code, start, end, days = args
            try:
                df, src = self._data_manager.get_daily_data(
                    code, start_date=start, end_date=end, days=days
                )
                if df is not None:
                    return (_key(code, start, end, days), (df, src))
            except Exception as e:
                logger.debug(f"[Screener] Batch fetch failed {code}: {e}")
            return None

        unique_requests = list(dict.fromkeys(requests))
        results: Dict[Tuple[str, str, str, int], Tuple[pd.DataFrame, str]] = {}
        pool = get_executor(max_workers, "screener_fetch")
        futures = {pool.submit(_fetch, req): req for req in unique_requests}
        try:
            for future in as_completed(futures, timeout=total_timeout):
                try:
                    res = future.result()
                except Exception as e:
                    code = futures[future][0]
                    logger.debug(f"[Screener] Batch fetch future error {code}: {e}")
                    continue
                if res:
                    results[res[0]] = res[1]
        except FuturesTimeout:
            pending = [futures[f][0] for f in futures if not f.done()]
            logger.warning(
                f"[Screener] _fetch_daily_batch global timeout ({total_timeout}s), "
                f"{len(pending)} pending: {pending[:10]}"
            )
        finally:
            for fut in futures:
                if not fut.done():
                    fut.cancel()
        return results

    # ── Data fetching ────────────────────────────────────────────

    _UA_LIST = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    ]

    def _fetch_spot_data(self, trade_date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """Fetch full A-share data for Phase-1 fast screening.
        Priority: Tushare(daily, fast bulk scan) -> AkShare(spot, fallback) -> efinance(quotes, last resort).
        """
        # Historical mode: only Tushare supports dated queries
        if trade_date:
            df = self._try_tushare(trade_date=trade_date)
            if df is not None and not df.empty:
                logger.info(f"[Screener] Historical mode - Using Tushare data: {len(df)} stocks")
                return df
            logger.warning("[Screener] Historical mode - Tushare returned empty, no data available")
            return None

        # --- 1. Tushare daily - fast bulk scan for Phase-1 ---
        logger.info(
            "[Screener] Trying Tushare daily (priority 1/3) - "
            "fast bulk scan for Phase-1; realtime precision deferred to Phase-2"
        )
        df = self._try_tushare(trade_date=None)
        if df is not None and not df.empty:
            logger.info(f"[Screener] Using Tushare daily data: {len(df)} stocks (Phase-1 fast path)")
            return df
        logger.warning("[Screener] Tushare daily unavailable, trying AkShare realtime fallback")

        # --- 2. AkShare realtime ---
        def _try_akshare() -> pd.DataFrame:
            import random
            import requests as _req
            ua = random.choice(self._UA_LIST)
            orig = _req.utils.default_headers
            _req.utils.default_headers = lambda: _req.structures.CaseInsensitiveDict({"User-Agent": ua})
            try:
                import akshare as ak
                return ak.stock_zh_a_spot_em()
            finally:
                _req.utils.default_headers = orig

        logger.info(
            f"[Screener] Trying AkShare realtime spot (wall timeout={self._spot_timeout}s) - "
            "priority 2/3 fallback"
        )
        t0 = time.time()
        try:
            df = run_with_timeout(_try_akshare, self._spot_timeout, "screener_akshare")
            logger.info(
                f"[Screener] Using AkShare realtime data: {len(df)} stocks in {time.time()-t0:.1f}s"
            )
            return df
        except FuturesTimeout:
            logger.warning(f"[Screener] AkShare hard-timeout after {self._spot_timeout}s, trying next source")
        except Exception as e:
            logger.warning(f"[Screener] AkShare failed: {e}, trying next source")

        # --- 3. efinance realtime ---
        def _try_efinance() -> pd.DataFrame:
            import efinance as ef
            return ef.stock.get_realtime_quotes()

        logger.info(
            f"[Screener] Trying efinance realtime quotes (wall timeout={self._spot_timeout}s) - "
            "priority 3/3 last resort"
        )
        t0 = time.time()
        try:
            df = run_with_timeout(_try_efinance, self._spot_timeout, "screener_efinance")
            logger.info(
                f"[Screener] Using efinance realtime data: {len(df)} stocks in {time.time()-t0:.1f}s"
            )
            return self._normalize_efinance_df(df)
        except FuturesTimeout:
            logger.warning(f"[Screener] efinance hard-timeout after {self._spot_timeout}s")
        except Exception as e:
            logger.warning(f"[Screener] efinance failed: {e}")

        logger.error("[Screener] All data sources exhausted - no spot data available")
        return None

    def _try_tushare(self, trade_date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """Fetch full-market daily data via Tushare Pro (daily + daily_basic + stock_basic)."""
        tushare_api = self._get_tushare_api()
        if tushare_api is None:
            logger.info("[Screener] Tushare API not available (TUSHARE_TOKEN unset or init failed)")
            return None

        try:
            china_now = datetime.now(ZoneInfo("Asia/Shanghai"))
            is_historical = trade_date is not None
            if trade_date is None:
                trade_date = china_now.strftime("%Y%m%d")

            logger.info(f"[Screener] Fetching via Tushare (trade_date={trade_date})...")
            t0 = time.time()

            df_daily = tushare_api.daily(trade_date=trade_date)
            if (df_daily is None or df_daily.empty) and not is_historical:
                fallback_date = _resolve_fallback_trade_date(china_now)
                logger.info(f"[Screener] No data for {trade_date}, trying last trading day {fallback_date}...")
                df_daily = tushare_api.daily(trade_date=fallback_date)
                trade_date = fallback_date

            if df_daily is None or df_daily.empty:
                logger.warning("[Screener] Tushare daily returned empty")
                return None

            df_daily.columns = [c.lower() for c in df_daily.columns]

            # Fetch valuation metrics
            df_basic = tushare_api.daily_basic(
                trade_date=trade_date,
                fields="ts_code,pe,pb,turnover_rate,volume_ratio,total_mv",
            )
            if df_basic is not None and not df_basic.empty:
                df_basic.columns = [c.lower() for c in df_basic.columns]
                df_daily = df_daily.merge(df_basic, on="ts_code", how="left")

            # Fetch stock names (cache for backtest: same for all days)
            if self._stock_basic_cache is not None:
                df_names = self._stock_basic_cache
            else:
                df_names = tushare_api.stock_basic(fields="ts_code,symbol,name,industry")
                if df_names is not None and not df_names.empty:
                    df_names.columns = [c.lower() for c in df_names.columns]
                    self._stock_basic_cache = df_names
            if df_names is not None and not df_names.empty:
                df_daily = df_daily.merge(df_names, on="ts_code", how="left")

            # Normalize columns to match AkShare convention used by filters
            df_daily["代码"] = df_daily.get("symbol", df_daily["ts_code"].str[:6])
            df_daily["名称"] = df_daily.get("name", "")
            df_daily["最新价"] = df_daily["close"]
            df_daily["涨跌幅"] = df_daily.get("pct_chg", 0)
            df_daily["市盈率-动态"] = df_daily.get("pe", pd.NA)
            vr_stale = pd.to_numeric(df_daily.get("volume_ratio", pd.NA), errors="coerce")
            df_daily["量比"] = vr_stale.fillna(1.0)
            df_daily["换手率"] = df_daily.get("turnover_rate", pd.NA)
            df_daily["成交额"] = df_daily.get("amount", 0).astype(float) * 1000  # 千元->元
            df_daily["市净率"] = df_daily.get("pb", pd.NA)
            df_daily["总市值"] = df_daily.get("total_mv", 0).astype(float) * 1e4  # 万元->元

            # Compute 60-day change
            df_daily = self._add_tushare_60d_change(df_daily, tushare_api, trade_date)

            # Join MA5/MA10/MA20/ATR_20 (required by trade_levels anchoring)
            df_daily = self._add_tushare_ma_atr(df_daily, tushare_api, trade_date)

            # Overlay realtime data
            if not is_historical:
                try:
                    from data_provider.tushare import realtime as _ts_rt
                    _ts_rt._realtime_list_cache.clear()
                    _ts_rt._rt_k_cache.clear()
                    logger.debug("[Screener] Forced realtime cache expiry before supplement")
                except (ImportError, AttributeError) as e:
                    logger.warning(f"[Screener] Could not force cache expiry: {e}")
                df_daily = self._supplement_realtime_data(df_daily)

            elapsed = time.time() - t0
            logger.info(f"[Screener] Tushare returned {len(df_daily)} stocks in {elapsed:.1f}s")
            return df_daily

        except Exception as e:
            logger.warning(f"[Screener] Tushare failed: {e}")
            return None

    def _get_tushare_api(self):
        """Get Tushare API instance from data_manager or create one.

        In historical/backtest mode (self._as_of_date set), wraps the api
        in a parquet-cached proxy so repeated iter runs are sub-second.
        """
        api = get_tushare_api(self._data_manager)
        if api is None:
            return None
        if getattr(self, "_as_of_date", None):
            from src.services.picker._tushare_cache import wrap_api
            return wrap_api(api)
        return api

    def _supplement_realtime_data(self, df_daily: pd.DataFrame) -> pd.DataFrame:
        """Overlay realtime fields onto the Tushare daily DataFrame.

        Primary source: Tushare rt_k (full-market realtime daily bars).
        Fallback source: realtime_list(src='dc') when rt_k is unavailable.
        """
        try:
            tushare_fetcher = None
            for fetcher in getattr(self._data_manager, '_fetchers', []):
                if type(fetcher).__name__ == 'TushareFetcher':
                    tushare_fetcher = fetcher
                    break

            if tushare_fetcher is None:
                logger.debug("[Screener] TushareFetcher not available, skipping realtime supplement")
                return df_daily

            # --- Primary source: rt_k ---
            df_rt = None
            try:
                rt_k_quotes = tushare_fetcher._fetch_realtime_rt_k()
                if rt_k_quotes:
                    rt_data = []
                    for ts_code, quote in rt_k_quotes.items():
                        rt_data.append({
                            'ts_code': ts_code,
                            'price': quote.price,
                            'pct_change': quote.change_pct,
                            'vol_ratio': quote.volume_ratio,
                            'turnover_rate': quote.turnover_rate,
                            'pe': quote.pe_ratio,
                            'pb': quote.pb_ratio,
                            'total_mv': quote.total_mv,
                            'amount': quote.amount,
                        })
                    if rt_data:
                        df_rt = pd.DataFrame(rt_data)
                        logger.info("[Screener] Realtime data source: rt_k (%d quotes)", len(df_rt))
            except Exception as e:
                logger.debug("[Screener] rt_k fetch error: %s", e)

            # --- Fallback: realtime_list ---
            if df_rt is None or df_rt.empty:
                logger.info("[Screener] rt_k unavailable, falling back to realtime_list")
                df_rt = tushare_fetcher._fetch_realtime_list()

            if df_rt is None or df_rt.empty:
                logger.warning(
                    "[Screener] Both rt_k and realtime_list returned empty/failed, "
                    "falling back to daily_basic (T-1) data"
                )
                return df_daily

            # Build a lookup by 6-digit code
            rt = df_rt.copy()
            if 'ts_code' in rt.columns:
                rt['_code6'] = rt['ts_code'].str.split('.').str[0]
            else:
                logger.debug("[Screener] realtime_list has no ts_code column, skipping supplement")
                return df_daily

            rt = rt.drop_duplicates(subset='_code6', keep='first').set_index('_code6')

            # Map daily df codes to 6-digit for joining
            if '代码' in df_daily.columns:
                code_series = df_daily['代码'].astype(str).str[:6]
            elif 'ts_code' in df_daily.columns:
                code_series = df_daily['ts_code'].str.split('.').str[0]
            else:
                logger.debug("[Screener] Cannot determine code column for realtime join")
                return df_daily

            updated_count = 0

            # -- vol_ratio --
            if 'vol_ratio' in rt.columns:
                rt_vr = code_series.map(rt['vol_ratio'])
                rt_vr = pd.to_numeric(rt_vr, errors='coerce')
                valid = rt_vr.notna() & (rt_vr > 0)
                if valid.any():
                    df_daily.loc[valid, '量比'] = rt_vr[valid]
                    updated_count += valid.sum()
                    logger.info(
                        "[Screener] Realtime vol_ratio supplemented for %d/%d stocks",
                        valid.sum(), len(df_daily),
                    )

            # -- turnover_rate --
            if 'turnover_rate' in rt.columns:
                rt_tr = code_series.map(rt['turnover_rate'])
                rt_tr = pd.to_numeric(rt_tr, errors='coerce')
                valid = rt_tr.notna() & (rt_tr > 0)
                if valid.any():
                    df_daily['换手率'] = df_daily.get('换手率', pd.NA)
                    df_daily.loc[valid, '换手率'] = rt_tr[valid]

            # -- pe --
            if 'pe' in rt.columns:
                rt_pe = code_series.map(rt['pe'])
                rt_pe = pd.to_numeric(rt_pe, errors='coerce')
                valid = rt_pe.notna()
                if valid.any():
                    df_daily.loc[valid, '市盈率-动态'] = rt_pe[valid]

            # -- pb --
            if 'pb' in rt.columns:
                rt_pb = code_series.map(rt['pb'])
                rt_pb = pd.to_numeric(rt_pb, errors='coerce')
                valid = rt_pb.notna()
                if valid.any():
                    df_daily.loc[valid, '市净率'] = rt_pb[valid]

            # -- total_mv --
            if 'total_mv' in rt.columns:
                rt_mv = code_series.map(rt['total_mv'])
                rt_mv = pd.to_numeric(rt_mv, errors='coerce')
                valid = rt_mv.notna() & (rt_mv > 0)
                if valid.any():
                    df_daily.loc[valid, '总市值'] = rt_mv[valid]

            # -- price --
            if 'price' in rt.columns:
                rt_price = code_series.map(rt['price'])
                rt_price = pd.to_numeric(rt_price, errors='coerce')
                valid = rt_price.notna() & (rt_price > 0)
                if valid.any():
                    df_daily.loc[valid, '最新价'] = rt_price[valid]

            # -- pct_change --
            if 'pct_change' in rt.columns:
                rt_chg = code_series.map(rt['pct_change'])
                rt_chg = pd.to_numeric(rt_chg, errors='coerce')
                valid = rt_chg.notna()
                if valid.any():
                    df_daily.loc[valid, '涨跌幅'] = rt_chg[valid]

            # -- amount --
            if 'amount' in rt.columns:
                rt_amt = code_series.map(rt['amount'])
                rt_amt = pd.to_numeric(rt_amt, errors='coerce')
                valid = rt_amt.notna() & (rt_amt > 0)
                if valid.any():
                    df_daily.loc[valid, '成交额'] = rt_amt[valid]

            # -- 60day --
            col_60d = None
            for candidate_col in ('60day', '60_day'):
                if candidate_col in rt.columns:
                    col_60d = candidate_col
                    break
            if col_60d:
                rt_60d = code_series.map(rt[col_60d])
                rt_60d = pd.to_numeric(rt_60d, errors='coerce')
                valid = rt_60d.notna()
                if valid.any():
                    df_daily.loc[valid, '60日涨跌幅'] = rt_60d[valid]
                    logger.info(
                        "[Screener] Realtime 60d change supplemented for %d stocks", valid.sum()
                    )

            logger.info("[Picker] Supplemented %d stocks with realtime data", updated_count)
            logger.info("[Screener] Realtime data supplement complete (%d rows)", len(rt))

        except Exception as e:
            logger.warning("[Screener] Failed to supplement realtime data: %s", e)

        # --- Fallback: compute volume ratio for stocks still missing it ---
        try:
            self._fill_missing_volume_ratio(df_daily)
        except Exception as e:
            logger.warning("[Screener] Failed to fill missing volume ratio: %s", e)

        return df_daily

    def _fill_missing_volume_ratio(self, df_daily: pd.DataFrame) -> None:
        """Compute volume ratio for stocks where it is missing or stale."""
        if '量比' not in df_daily.columns or not self._data_manager:
            return

        vr_col = pd.to_numeric(df_daily['量比'], errors='coerce')
        needs_calc = vr_col.isna() | (vr_col <= 0) | (vr_col == 1.0)
        if not needs_calc.any():
            return

        if '代码' in df_daily.columns:
            code_series = df_daily['代码'].astype(str).str[:6]
        elif 'ts_code' in df_daily.columns:
            code_series = df_daily['ts_code'].str.split('.').str[0]
        else:
            return

        vol_col = self._first_col(df_daily, 'vol', 'volume', '成交量')
        if vol_col is None:
            return

        filled = 0
        for idx in df_daily.index[needs_calc]:
            code = str(code_series.get(idx, ''))
            if not code:
                continue
            today_vol = pd.to_numeric(df_daily.at[idx, vol_col], errors='coerce')
            if pd.isna(today_vol) or today_vol <= 0:
                continue
            try:
                df_hist, _src = self._data_manager.get_daily_data(code, days=6)
                if df_hist is None or len(df_hist) < 2:
                    continue
                hist_vol_col = self._first_col(df_hist, 'vol', 'volume', '成交量')
                if hist_vol_col is None:
                    continue
                hist_vol = pd.to_numeric(df_hist[hist_vol_col], errors='coerce').iloc[:-1]
                avg_5d = hist_vol.mean()
                if pd.isna(avg_5d) or avg_5d <= 0:
                    continue
                vr = self._calc_volume_ratio(float(today_vol), float(avg_5d))
                if vr > 0:
                    df_daily.at[idx, '量比'] = vr
                    filled += 1
            except Exception:
                continue

        if filled > 0:
            logger.info(
                "[Screener] Computed volume ratio for %d/%d stocks (5-day avg fallback)",
                filled, int(needs_calc.sum()),
            )

    def _add_tushare_60d_change(
        self, df_daily: pd.DataFrame, tushare_api, trade_date: str
    ) -> pd.DataFrame:
        """Add 60d change for Tushare data by fetching close from 60 trading days ago."""
        try:
            start = (pd.Timestamp(trade_date) - pd.Timedelta(days=120)).strftime("%Y%m%d")
            df_cal = tushare_api.trade_cal(exchange="SSE", start_date=start, end_date=trade_date)
            if df_cal is None or df_cal.empty:
                logger.warning("[Screener] Tushare trade_cal returned empty, 60d change skipped")
                df_daily["60日涨跌幅"] = 0
                return df_daily

            df_cal.columns = [c.lower() for c in df_cal.columns]
            df_cal = df_cal[df_cal["is_open"] == 1].sort_values("cal_date")
            dates = df_cal["cal_date"].tolist()
            if trade_date not in dates:
                idx = 0
            else:
                idx = dates.index(trade_date)
            if idx < 60:
                logger.warning("[Screener] Not enough trading days for 60d change, skipped")
                df_daily["60日涨跌幅"] = 0
                return df_daily

            date_60d = dates[idx - 60]
            df_60d = tushare_api.daily(trade_date=date_60d)
            if df_60d is None or df_60d.empty:
                df_daily["60日涨跌幅"] = 0
                return df_daily

            df_60d.columns = [c.lower() for c in df_60d.columns]
            close_60d_map = df_60d.set_index("ts_code")["close"]
            close_today = pd.to_numeric(df_daily["close"], errors="coerce")
            close_60d = df_daily["ts_code"].map(close_60d_map)
            close_60d = pd.to_numeric(close_60d, errors="coerce")
            mask = (close_60d > 0) & close_today.notna() & close_60d.notna()
            pct_60d = pd.Series(0.0, index=df_daily.index)
            pct_60d.loc[mask] = (close_today.loc[mask] - close_60d.loc[mask]) / close_60d.loc[mask] * 100
            df_daily["60日涨跌幅"] = pct_60d.values
            logger.info(f"[Screener] Added 60d change for {mask.sum()} stocks (ref date {date_60d})")
        except Exception as e:
            logger.warning(f"[Screener] Failed to add 60d change: {e}")
            df_daily["60日涨跌幅"] = 0
        return df_daily

    def _add_tushare_ma_atr(
        self, df_daily: pd.DataFrame, tushare_api, trade_date: str
    ) -> pd.DataFrame:
        """Join MA5/MA10/MA20/ATR_20 computed from bulk daily bars.

        trade_levels anchors ideal_buy/secondary_buy/stop on these columns;
        without them every price level silently degrades to fixed constants.
        Disabled with env ``PICKER_MA_JOIN=0``.
        """
        if os.environ.get("PICKER_MA_JOIN", "1") != "1":
            return df_daily
        try:
            if "ts_code" not in df_daily.columns:
                return df_daily
            start = (pd.Timestamp(trade_date) - pd.Timedelta(days=60)).strftime("%Y%m%d")
            df_cal = tushare_api.trade_cal(exchange="SSE", start_date=start, end_date=trade_date)
            if df_cal is None or df_cal.empty:
                logger.warning("[Screener] trade_cal empty, MA/ATR join skipped")
                return df_daily
            df_cal.columns = [c.lower() for c in df_cal.columns]
            sessions = df_cal[df_cal["is_open"] == 1]["cal_date"].sort_values().tolist()
            sessions = [s for s in sessions if s <= trade_date][-21:]
            if len(sessions) < 5:
                return df_daily

            frames: List[pd.DataFrame] = []
            for td in sessions:
                try:
                    d = tushare_api.daily(
                        trade_date=td, fields="ts_code,close,high,low,pre_close"
                    )
                    if d is not None and not d.empty:
                        d.columns = [c.lower() for c in d.columns]
                        d["trade_date"] = td
                        frames.append(d)
                except Exception as e:
                    logger.debug("[Screener] MA/ATR history fetch %s failed: %s", td, e)
            if not frames:
                return df_daily

            hist = pd.concat(frames, ignore_index=True)
            for col in ("close", "high", "low", "pre_close"):
                hist[col] = pd.to_numeric(hist[col], errors="coerce")
            hist = hist.sort_values(["ts_code", "trade_date"], kind="stable")
            g = hist.groupby("ts_code", sort=False)
            hist["_ma5"] = g["close"].transform(lambda s: s.rolling(5).mean())
            hist["_ma10"] = g["close"].transform(lambda s: s.rolling(10).mean())
            hist["_ma20"] = g["close"].transform(lambda s: s.rolling(20).mean())
            tr = pd.concat([
                hist["high"] - hist["low"],
                (hist["high"] - hist["pre_close"]).abs(),
                (hist["low"] - hist["pre_close"]).abs(),
            ], axis=1).max(axis=1)
            hist["_tr"] = tr
            hist["_atr20"] = hist.groupby("ts_code", sort=False)["_tr"].transform(
                lambda s: s.rolling(20).mean()
            )

            latest = hist.groupby("ts_code", sort=False).tail(1).set_index("ts_code")
            for src_col, dst_col in (
                ("_ma5", "MA5"), ("_ma10", "MA10"),
                ("_ma20", "MA20"), ("_atr20", "ATR_20"),
            ):
                df_daily[dst_col] = df_daily["ts_code"].map(latest[src_col])
            filled = int(df_daily["MA20"].notna().sum())
            logger.info("[Screener] MA/ATR joined for %d/%d stocks", filled, len(df_daily))
        except Exception as e:
            logger.warning(f"[Screener] Failed to add MA/ATR columns: {e}")
        return df_daily

    @staticmethod
    def _normalize_efinance_df(df: pd.DataFrame) -> pd.DataFrame:
        """Normalize efinance column names to match AkShare's convention."""
        col_map = {
            "股票代码": "代码", "股票名称": "名称",
            "最新价": "最新价", "涨跌幅": "涨跌幅",
            "成交量": "成交量", "成交额": "成交额",
            "换手率": "换手率", "量比": "量比",
            "动态市盈率": "市盈率-动态", "市净率": "市净率",
            "总市值": "总市值", "流通市值": "流通市值",
        }
        renamed = {}
        for old, new in col_map.items():
            if old in df.columns:
                renamed[old] = new
        return df.rename(columns=renamed)

    # ── Basic filters ────────────────────────────────────────────
