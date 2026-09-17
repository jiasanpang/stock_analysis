# -*- coding: utf-8 -*-
"""
Picker Backtest Service

Runs the quantitative screener historically and evaluates forward returns.
Uses top N by score (no LLM) for each trade date.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from data_provider.base import DataFetcherManager
from data_provider.caching_manager import CachingDataFetcherManager
from src.config import get_config
from src.services.picker import (
    StockScreener,
    create_screener_from_config,
    get_tushare_api,
    ScreenedStock,
)
from src.services.limit_up_rules import (
    lup_exit_enabled,
    simulate_limit_up_pullback_trade,
)
from src.services.trade_levels import (
    DEFAULT_SLIPPAGE_PCT,
    LIMIT_UP_KCCY,
    LIMIT_UP_MAIN,
    simulate_forward_trade,
)

logger = logging.getLogger(__name__)

BENCHMARK_CODE = "000300.SH"  # CSI 300

_FORWARD_RETURNS_EXECUTOR = ThreadPoolExecutor(max_workers=5, thread_name_prefix="fwd")

# Legacy fallback constants (kept for backward compat where callers still
# reference them; new code should use simulate_forward_trade with unified rules).
STOP_LOSS_PCT = -8.0
TAKE_PROFIT_PCT = 15.0
GATEWAY_LEVELS = (5, 10, 20, 50, 100, 200, 500, 1000)


@dataclass
class PickResult:
    """Single pick outcome."""
    trade_date: str
    code: str
    name: str
    entry_price: float
    exit_price: Optional[float]
    return_pct: Optional[float]
    outcome: str  # "win" | "loss" | "insufficient"
    score: float = 0.0
    # Trade-levels engine extras (surfaced for diagnostic UI)
    exit_reason: Optional[str] = None  # stop_loss / trailing_ma10 / stage_break_+12pct / hardcap_+20pct / window_end / ...
    hold_days: Optional[int] = None
    strategy_id: Optional[str] = None


@dataclass
class PickerBacktestSummary:
    """Aggregated backtest metrics."""
    start_date: str
    end_date: str
    hold_days: int
    top_n: int
    total_picks: int
    win_count: int
    loss_count: int
    insufficient_count: int
    win_rate_pct: Optional[float]
    avg_return_pct: Optional[float]
    max_drawdown_pct: Optional[float]
    profit_factor: Optional[float]
    alpha_vs_benchmark_pct: Optional[float]
    benchmark_avg_return_pct: Optional[float]
    # ── New v6 benchmarks (truer alpha measurement) ──────────────────
    # Per-period equal-weight market average over same windows as picks
    market_eqw_avg_return_pct: Optional[float] = None
    alpha_vs_market_eqw_pct: Optional[float] = None
    # Whole-period totals for "buy & hold" comparison (cash-aware)
    strategy_total_return_pct: Optional[float] = None
    benchmark_total_return_pct: Optional[float] = None
    market_eqw_total_return_pct: Optional[float] = None
    bh_alpha_vs_benchmark_pct: Optional[float] = None
    bh_alpha_vs_market_eqw_pct: Optional[float] = None
    # NAV-derived portfolio metrics (especially relevant for small_cap)
    cagr_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    calmar_ratio: Optional[float] = None
    days_in_market_pct: Optional[float] = None



class PickerBacktestService:
    """Backtest the quantitative picker (no LLM) over historical dates."""

    def __init__(self, data_manager: Optional[DataFetcherManager] = None):
        base = data_manager or DataFetcherManager()
        self._data_manager = (
            base
            if isinstance(base, CachingDataFetcherManager)
            else CachingDataFetcherManager(base)
        )
        self._screener = create_screener_from_config(data_manager=self._data_manager)
        self._tushare_api = None

    def _get_tushare_api(self):
        """Get Tushare API for trade_cal and benchmark (cached, with disk parquet)."""
        if self._tushare_api is None:
            from src.services.picker._tushare_cache import wrap_api
            self._tushare_api = wrap_api(get_tushare_api(self._data_manager))
        return self._tushare_api

    def _get_trade_dates(self, start_date: str, end_date: str) -> List[str]:
        """Get list of trading dates in range (YYYYMMDD). Tushare first, fallback to exchange_calendars."""
        start = start_date.replace("-", "").replace("/", "")[:8]
        end = end_date.replace("-", "").replace("/", "")[:8]
        start_iso = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
        end_iso = f"{end[:4]}-{end[4:6]}-{end[6:8]}"

        # Try Tushare first
        api = self._get_tushare_api()
        if api is not None:
            try:
                df = api.trade_cal(exchange="SSE", start_date=start, end_date=end)
                if df is not None and not df.empty:
                    df.columns = [c.lower() for c in df.columns]
                    df = df[df["is_open"] == 1].sort_values("cal_date")
                    return df["cal_date"].astype(str).tolist()
            except Exception as e:
                logger.debug("Tushare trade_cal failed, trying exchange_calendars: %s", e)

        # Fallback: exchange_calendars (supports wider date range)
        try:
            import exchange_calendars as xcals
            cal = xcals.get_calendar("XSHG")
            sessions = cal.sessions_in_range(start_iso, end_iso)
            return [s.strftime("%Y%m%d") for s in sessions]
        except ImportError:
            logger.warning("exchange_calendars not installed; cannot fallback for trade dates")
            return []
        except Exception as e:
            logger.warning("exchange_calendars sessions_in_range failed: %s", e)
            return []

    def _fetch_daily_from_localdb(
        self, code: str, start_iso: str, end_iso: str,
    ) -> Optional["pd.DataFrame"]:
        """Read a single stock's daily bars from LocalStockDB and shape
        them to match what _get_forward_return expects (lowercase
        OHLCV columns with a `date` field).

        Returns ``None`` if LocalDB doesn't have the symbol or fails
        to open, so the caller can fall back to the network fetcher.
        """
        try:
            from src.services.local_db import default_db
            db = default_db()
            ts_code = code if "." in code else (
                f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.SZ"
            )
            start_yyyymmdd = start_iso.replace("-", "")
            end_yyyymmdd = end_iso.replace("-", "")
            df = db.get_daily(ts_code, start_yyyymmdd, end_yyyymmdd)
            if df is None or df.empty:
                return None
            out = df.rename(columns={"trade_date": "date"}).copy()
            if "date" in out.columns:
                out["date"] = pd.to_datetime(out["date"].astype(str), format="%Y%m%d")
            return out
        except Exception as e:  # pragma: no cover — keep backtest resilient
            logger.debug("[PickerBacktest] LocalDB read failed for %s: %s", code, e)
            return None

    def _get_exit_date(self, trade_date: str, hold_days: int) -> Optional[str]:
        """Get exit date (hold_days trading days after trade_date)."""
        dates = self._get_trade_dates(
            (pd.Timestamp(trade_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            (pd.Timestamp(trade_date) + pd.Timedelta(days=hold_days * 3)).strftime("%Y-%m-%d"),
        )
        if not dates:
            return None
        try:
            idx = dates.index(trade_date)
        except ValueError:
            return None
        if idx + hold_days >= len(dates):
            return None
        return dates[idx + hold_days]

    def _get_forward_return(
        self,
        code: str,
        trade_date: str,
        exit_date: str,
        entry_price: float,
        strategy_id: str = "buy_pullback",
        stop_loss_pct: float = STOP_LOSS_PCT,    # kept for backward compat (unused)
        take_profit_pct: float = TAKE_PROFIT_PCT,
        structural_stop: float = 0.0,
        secondary_buy: float = 0.0,
    ) -> Dict[str, Any]:
        """Fetch daily data and simulate the forward trade with the unified
        trade_levels engine (same rules as production picker / analyzer).

        Adds slippage (0.3% one-way) and limit-up entry filter (cannot fill at
        limit-up board). Returns (exit_price, return_pct).

        stop_loss_pct/take_profit_pct kwargs are kept only for backward-compat
        with older callers; the actual exits are governed by
        trade_levels.evaluate_trailing_exit.

        structural_stop/secondary_buy come from the pick's TradeLevels and
        enable the limit-up-pullback staged-exit simulator (Step 5 of the
        涨停回踩买入法 rollout) when LUP_EXIT is on.
        """
        try:
            start_dt = pd.Timestamp(trade_date) - pd.Timedelta(days=45)
            start_iso = start_dt.strftime("%Y-%m-%d")
            end_iso = f"{exit_date[:4]}-{exit_date[4:6]}-{exit_date[6:8]}"

            # LocalDB-first: backtest data is fully resident in the local
            # parquet warehouse. Going through DataFetcherManager hits the
            # network (Tushare) per symbol per session — that's the slow
            # path AND the one that returns empty in offline / sandboxed
            # environments, which manifests as "数据不足" for every pick.
            df = self._fetch_daily_from_localdb(code, start_iso, end_iso)
            if df is None or df.empty:
                df, _ = self._data_manager.get_daily_data(
                    code, start_date=start_iso, end_date=end_iso, days=80,
                )
            if df is None or df.empty:
                return {}
            date_col = next((c for c in ["date", "日期"] if c in df.columns), df.columns[0])
            close_col = next((c for c in ["close", "收盘"] if c in df.columns), None)
            high_col = next((c for c in ["high", "最高"] if c in df.columns), None)
            low_col = next((c for c in ["low", "最低"] if c in df.columns), None)
            open_col = next((c for c in ["open", "开盘"] if c in df.columns), None)
            pct_col = next((c for c in ["pct_chg", "涨跌幅"] if c in df.columns), None)
            if close_col is None:
                return {}

            df = df.sort_values(date_col).reset_index(drop=True)
            df[date_col] = pd.to_datetime(df[date_col])
            df["_date_str"] = df[date_col].dt.strftime("%Y-%m-%d")

            # MA10/MA20/ATR (compute if absent)
            for ma_col, win in (("ma10", 10), ("ma20", 20)):
                if ma_col not in df.columns:
                    df[ma_col] = df[close_col].rolling(window=win, min_periods=1).mean()
            if "atr" not in df.columns and high_col and low_col:
                tr = pd.concat([
                    (df[high_col] - df[low_col]).abs(),
                    (df[high_col] - df[close_col].shift()).abs(),
                    (df[low_col] - df[close_col].shift()).abs(),
                ], axis=1).max(axis=1)
                df["atr"] = tr.rolling(window=14, min_periods=1).mean()

            entry_str = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
            pick_mask = df["_date_str"] == entry_str
            if not pick_mask.any():
                return {}
            entry_idx = int(df.index[pick_mask].min())
            if entry_price <= 0:
                close_v = df.iloc[entry_idx][close_col]
                if pd.isna(close_v) or float(close_v) <= 0:
                    return {}
                entry_price = float(close_v)

            # small_cap is a cross-sectional rebalance factor: hold N trading
            # days, no stop/TP, only frictions. Different semantics from the
            # single-stock momentum strategies below.
            if strategy_id == "small_cap":
                # Find the exit-date row inside the local bar window. We
                # used to reference an undefined `hold_days` here, which
                # threw NameError → caught by the outer try → every
                # small_cap pick returned {} ("数据不足"). Derive the
                # actual exit index from exit_date instead.
                exit_str = f"{exit_date[:4]}-{exit_date[4:6]}-{exit_date[6:8]}"
                exit_mask = df["_date_str"] == exit_str
                forward_df = df.iloc[entry_idx:].copy()
                if exit_mask.any():
                    exit_idx = int(df.index[exit_mask].max())
                    hold = exit_idx - entry_idx
                else:
                    # Caller's exit_date is past the bars we have; clip
                    # to the last available bar instead of skipping.
                    hold = len(forward_df) - 1
                hold = max(1, hold)
                if len(forward_df) < 2:
                    return {}
                hold = min(hold, len(forward_df) - 1)
                exit_row = forward_df.iloc[hold]
                exit_price_raw = float(exit_row[close_col]) if pd.notna(exit_row[close_col]) else 0.0
                if exit_price_raw <= 0:
                    return {}
                ENTRY_SLIP, EXIT_SLIP, ROUND_TRIP = 0.001, 0.001, 0.10
                eff_entry = entry_price * (1 + ENTRY_SLIP)
                eff_exit = exit_price_raw * (1 - EXIT_SLIP)
                net_pct = (eff_exit - eff_entry) / eff_entry * 100.0 - ROUND_TRIP
                return {
                    "exit_price": eff_exit,
                    "return_pct": net_pct,
                    "exit_reason": "rebalance_window_end",
                    "hold_days": hold,
                }

            # Multi-day strategies (buy_pullback / bottom_reversal / reversal_breakout):
            # use unified trade_levels engine with ATR-trailing stop /
            # strategy-specific TP rules.
            forward_df = df.iloc[entry_idx:].copy()
            if len(forward_df) < 2:
                return {}
            bars: List[Dict[str, float]] = []
            for _, row in forward_df.iterrows():
                bar: Dict[str, float] = {
                    "close": float(row[close_col]) if pd.notna(row[close_col]) else 0.0,
                    "high": float(row[high_col]) if high_col and pd.notna(row[high_col]) else 0.0,
                    "low": float(row[low_col]) if low_col and pd.notna(row[low_col]) else 0.0,
                    "ma10": float(row["ma10"]) if pd.notna(row["ma10"]) else 0.0,
                    "ma20": float(row["ma20"]) if pd.notna(row["ma20"]) else 0.0,
                }
                if "atr" in df.columns and pd.notna(row.get("atr")):
                    bar["atr"] = float(row["atr"])
                if open_col and pd.notna(row.get(open_col)):
                    bar["open"] = float(row[open_col])
                if pct_col and pd.notna(row.get(pct_col)):
                    bar["pct_chg"] = float(row[pct_col])
                bars.append(bar)

            mcap_yi = 0.0  # unknown; trade_levels falls back to mid band
            is_kc_cy = code.startswith(("30", "68"))
            # buy_pullback hard stop: disabled by default. Earlier intuition
            # said a -3% intraday stop would cap MA20-break losses, but
            # measured A/B on 2026-02 → 2026-05 (hold=10, top_n=5):
            #   hard_stop=3% → 82 picks, WR=28%, avg=+0.68%, tot=+48.9%
            #   hard_stop=0  → 82 picks, WR=46%, avg=+1.73%, tot=+76.6%
            # The -3% stop kicks healthy intraday washes out for full
            # losses while the trade_levels MA20 / take-profit rules
            # would have let them recover. Leave hard stop off; users
            # who want it back set BUY_PULLBACK_HARD_STOP_PCT=0.03.
            hard_stop_pct = 0.0
            if strategy_id == "buy_pullback":
                try:
                    hard_stop_pct = float(os.environ.get("BUY_PULLBACK_HARD_STOP_PCT", "0"))
                except ValueError:
                    hard_stop_pct = 0.0
            # 涨停回踩 staged-exit simulator (structural stop + secondary
            # add-on + 10% trim/MA10 trail). Only for LUP-flavored picks that
            # carry real TradeLevels; falls through to the legacy engine
            # otherwise or when LUP_EXIT=0.
            if (
                strategy_id == "buy_pullback"
                and structural_stop > 0
                and lup_exit_enabled()
            ):
                sim = simulate_limit_up_pullback_trade(
                    entry_price=entry_price,
                    stop_price=structural_stop,
                    secondary_buy=secondary_buy,
                    bars=bars,
                    apply_slippage=True,
                    apply_limit_up_filter=True,
                    is_kc_cy=is_kc_cy,
                )
                if sim.get("skipped"):
                    return {}
                return sim
            sim = simulate_forward_trade(
                strategy_id=strategy_id,
                entry_price=entry_price,
                market_cap_yi=mcap_yi,
                bars=bars,
                apply_slippage=True,
                apply_limit_up_filter=True,
                is_kc_cy=is_kc_cy,
                hard_stop_pct=hard_stop_pct,
            )
            if sim.get("skipped"):
                return {}
            return sim
        except Exception as e:
            logger.debug(f"[PickerBacktest] Forward return failed {code}: {e}")
            return {}

    def _get_benchmark_return(self, trade_date: str, exit_date: str) -> Optional[float]:
        """Get benchmark (CSI 300) return over the same period."""
        api = self._get_tushare_api()
        if api is None:
            return None
        try:
            start = trade_date
            end = exit_date
            df = api.index_daily(ts_code=BENCHMARK_CODE, start_date=start, end_date=end)
            if df is None or len(df) < 2:
                return None
            df.columns = [c.lower() for c in df.columns]
            df = df.sort_values("trade_date")
            entry_row = df[df["trade_date"] == trade_date]
            exit_row = df[df["trade_date"] == exit_date]
            if entry_row.empty or exit_row.empty:
                return None
            p0 = float(entry_row["close"].iloc[0])
            p1 = float(exit_row["close"].iloc[0])
            if p0 <= 0:
                return None
            return (p1 - p0) / p0 * 100
        except Exception as e:
            logger.debug(f"[PickerBacktest] Benchmark return failed: {e}")
            return None

    def _get_market_eqw_returns_batch(
        self, date_pairs: List[Tuple[str, Optional[str]]]
    ) -> Dict[Tuple[str, str], float]:
        """Equal-weight cross-section average return over [trade_date, exit_date].

        For each (entry, exit) pair, computes:
            mean over all stocks of (close_exit / close_entry - 1)*100

        Excludes stocks missing either endpoint. Reads from LocalDB by-date
        shards — no network. Returns dict keyed by (entry, exit).
        """
        valid = [(td, ed) for td, ed in date_pairs if ed is not None]
        if not valid:
            return {}
        try:
            from src.services.local_db import default_db
            db = default_db()
        except Exception:
            return {}

        unique_dates = sorted({d for td, ed in valid for d in (td, ed)})
        close_by_date: Dict[str, Dict[str, float]] = {}
        for d in unique_dates:
            df = db.get_market_daily(d)
            if df is None or df.empty:
                continue
            if "ts_code" not in df.columns or "close" not in df.columns:
                continue
            close_by_date[d] = dict(zip(df["ts_code"], df["close"].astype(float)))

        result: Dict[Tuple[str, str], float] = {}
        for td, ed in valid:
            m_entry = close_by_date.get(td)
            m_exit = close_by_date.get(ed)
            if not m_entry or not m_exit:
                continue
            rets: List[float] = []
            for ts_code, p0 in m_entry.items():
                if p0 <= 0:
                    continue
                p1 = m_exit.get(ts_code)
                if p1 is None or p1 <= 0:
                    continue
                rets.append((p1 - p0) / p0 * 100)
            if rets:
                result[(td, ed)] = sum(rets) / len(rets)
        return result

    def _get_index_total_return(
        self, start_date: str, end_date: str
    ) -> Optional[float]:
        """Buy-and-hold benchmark return from start to end (close-to-close)."""
        try:
            from src.services.local_db import default_db
            db = default_db()
            df = db.get_index_daily(BENCHMARK_CODE, start_date, end_date)
            if df is None or df.empty or len(df) < 2:
                return None
            df = df.sort_values("trade_date")
            p0 = float(df["close"].iloc[0])
            p1 = float(df["close"].iloc[-1])
            if p0 <= 0:
                return None
            return (p1 - p0) / p0 * 100
        except Exception:
            return None

    def _get_market_eqw_total_return(
        self, start_date: str, end_date: str
    ) -> Optional[float]:
        """Equal-weight buy-and-hold of all stocks listed at start_date.

        Universe: all stocks present in market_daily on start_date AND end_date.
        Return: mean per-stock B&H return (geometric/arithmetic both equivalent
        for cross-section average; we use arithmetic for consistency).
        """
        try:
            from src.services.local_db import default_db
            db = default_db()
            df_start = db.get_market_daily(start_date)
            df_end = db.get_market_daily(end_date)
            if df_start is None or df_end is None or df_start.empty or df_end.empty:
                return None
            entry_map = dict(zip(df_start["ts_code"], df_start["close"].astype(float)))
            exit_map = dict(zip(df_end["ts_code"], df_end["close"].astype(float)))
            rets: List[float] = []
            for ts_code, p0 in entry_map.items():
                if p0 <= 0:
                    continue
                p1 = exit_map.get(ts_code)
                if p1 is None or p1 <= 0:
                    continue
                rets.append((p1 - p0) / p0 * 100)
            if not rets:
                return None
            return sum(rets) / len(rets)
        except Exception:
            return None

    def _get_benchmark_returns_batch(
        self, date_pairs: List[Tuple[str, Optional[str]]]
    ) -> Dict[Tuple[str, str], float]:
        """Fetch benchmark once for full range, compute per-period returns. Saves N-1 Tushare calls."""
        valid_pairs = [(td, ed) for td, ed in date_pairs if ed is not None]
        if not valid_pairs:
            return {}
        api = self._get_tushare_api()
        if api is None:
            return {}
        try:
            all_dates = set()
            for td, ed in valid_pairs:
                all_dates.add(td)
                all_dates.add(ed)
            start = min(all_dates)
            end = max(all_dates)
            df = api.index_daily(ts_code=BENCHMARK_CODE, start_date=start, end_date=end)
            if df is None or df.empty:
                return {}
            df.columns = [c.lower() for c in df.columns]
            df = df.sort_values("trade_date")
            close_map = df.set_index("trade_date")["close"].to_dict()
            result: Dict[Tuple[str, str], float] = {}
            for td, ed in valid_pairs:
                p0 = close_map.get(td)
                p1 = close_map.get(ed)
                if p0 is None or p1 is None or float(p0) <= 0:
                    continue
                result[(td, ed)] = (float(p1) - float(p0)) / float(p0) * 100
            return result
        except Exception as e:
            logger.debug(f"[PickerBacktest] Batch benchmark failed: {e}")
            return {}

    def _get_forward_returns_parallel(
        self, picks: List[ScreenedStock], trade_date: str, exit_date: str
    ) -> List[PickResult]:
        """Fetch forward returns for picks in parallel (max 5 workers to respect rate limits)."""
        results: List[PickResult] = []
        futures = {
            _FORWARD_RETURNS_EXECUTOR.submit(
                self._get_forward_return, s.code, trade_date, exit_date, s.price,
                (s.strategies[0] if s.strategies else "buy_pullback"),
                # limit_up_date marks picks from the 涨停回踩 engine; legacy
                # buy_pullback picks must keep the trade_levels simulator.
                structural_stop=s.stop_loss if s.limit_up_date else 0.0,
                secondary_buy=s.secondary_buy if s.limit_up_date else 0.0,
            ): s
            for s in picks
        }
        for fut in as_completed(futures):
            s = futures[fut]
            strategy_id = s.strategies[0] if s.strategies else "buy_pullback"
            try:
                sim = fut.result() or {}
            except Exception as e:
                logger.debug(f"[PickerBacktest] Forward return failed {s.code}: {e}")
                sim = {}

            ret = sim.get("return_pct")
            exit_price = sim.get("exit_price")
            exit_reason = sim.get("exit_reason")
            hold_days = sim.get("hold_days")
            outcome = "insufficient" if ret is None else ("win" if ret > 0 else "loss")
            results.append(
                PickResult(
                    trade_date=trade_date,
                    code=s.code,
                    name=s.name,
                    entry_price=s.price,
                    exit_price=exit_price,
                    return_pct=ret,
                    outcome=outcome,
                    score=s.score,
                    exit_reason=exit_reason,
                    hold_days=hold_days,
                    strategy_id=strategy_id,
                )
            )
        return results

    def run(
        self,
        start_date: str,
        end_date: str,
        hold_days: int = 10,
        top_n: int = 5,
        picker_strategies: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Run picker backtest.

        Args:
            start_date: YYYY-MM-DD or YYYYMMDD
            end_date: YYYY-MM-DD or YYYYMMDD
            hold_days: holding period in trading days
            top_n: number of picks per day (by score)
            picker_strategies: optional override (buy_pullback, bottom_reversal, etc.)

        Returns:
            Dict with results, summary, and performance metrics.
        """
        # small_cap is a monthly-rebalance factor; force hold_days >= 20 unless
        # the caller already requested a longer window. Top-N also defaults to
        # 50 to match the recommended portfolio size from the research report.
        # reversal_breakout (right-side) is a short swing; force ≥20d
        # window so trailing/time-stop rules have room to fire.
        if picker_strategies and set(picker_strategies) == {"reversal_breakout"}:
            if hold_days < 20:
                logger.info(
                    "[PickerBacktest] reversal_breakout swing window: forcing hold_days=20 (was %d)",
                    hold_days,
                )
                hold_days = 20

        # bottom_reversal (left-side watchlist) is a medium swing trade
        # observed over longer windows; force ≥40d to match the 60d
        # time-stop in trade_levels.
        if picker_strategies and set(picker_strategies) == {"bottom_reversal"}:
            if hold_days < 40:
                logger.info(
                    "[PickerBacktest] bottom_reversal medium-swing window: forcing hold_days=40 (was %d)",
                    hold_days,
                )
                hold_days = 40

        if picker_strategies and set(picker_strategies) == {"small_cap"}:
            if hold_days < 20:
                logger.info(
                    "[PickerBacktest] small_cap rebalance window: forcing hold_days=20 (was %d)",
                    hold_days,
                )
                hold_days = 20
            if top_n < 30:
                logger.info(
                    "[PickerBacktest] small_cap recommended top_n>=30; forcing top_n=50 (was %d)",
                    top_n,
                )
                top_n = 50

        if picker_strategies is not None:
            cfg = get_config()
            screener = StockScreener(
                data_manager=self._data_manager,
                picker_strategies=picker_strategies,
                picker_mode=cfg.picker_mode,
                turnover_min=cfg.picker_turnover_min,
                turnover_max=cfg.picker_turnover_max,
                enable_b_wave_filter=getattr(cfg, "picker_enable_b_wave_filter", True),
                allow_loss=getattr(cfg, "picker_allow_loss", False),
            )
        else:
            screener = self._screener

        self._data_manager.clear_cache()
        trade_dates = self._get_trade_dates(start_date, end_date)
        if not trade_dates:
            return {
                "error": "所选日期范围内无交易日，请检查日期格式或扩大范围",
                "results": [],
                "summary": None,
            }

        logger.info(
            "[PickerBacktest] 选股回测：纯量化筛选（无 LLM），每日取评分 top%d，持仓 %d 天，共 %d 个交易日",
            top_n, hold_days, len(trade_dates),
        )

        # Precompute exit dates and batch-fetch benchmark (saves N-1 Tushare calls)
        date_pairs: List[Tuple[str, Optional[str]]] = []
        for td in trade_dates:
            exit_d = self._get_exit_date(td, hold_days)
            date_pairs.append((td, exit_d))

        benchmark_map = self._get_benchmark_returns_batch(date_pairs)
        market_eqw_map = self._get_market_eqw_returns_batch(date_pairs)
        benchmark_returns: List[float] = []
        market_eqw_returns: List[float] = []

        results: List[PickResult] = []
        days_with_picks = 0
        for i, (td, exit_date) in enumerate(date_pairs):
            if (i + 1) % 20 == 0:
                logger.info(f"[PickerBacktest] Progress {i + 1}/{len(trade_dates)} dates")
            try:
                candidates, _, _ = screener.screen_as_of(td)
                picks = candidates[:top_n]
                if not picks:
                    logger.debug(f"[PickerBacktest] {td}: 筛选后无候选，跳过")
                    continue
                if exit_date is None:
                    logger.debug(f"[PickerBacktest] {td}: 持仓期不足，跳过")
                    continue
                days_with_picks += 1
                pick_info = ", ".join(f"{p.code}({p.score:.1f})" for p in picks)
                logger.info(f"[PickerBacktest] {td}: 筛选 top{top_n} → {pick_info}")

                bm_ret = benchmark_map.get((td, exit_date))
                if bm_ret is not None:
                    benchmark_returns.append(bm_ret)
                me_ret = market_eqw_map.get((td, exit_date))
                if me_ret is not None:
                    market_eqw_returns.append(me_ret)

                # Parallelize forward return fetches (5 picks per day)
                pick_results = self._get_forward_returns_parallel(picks, td, exit_date)
                # Log evaluation results for each pick
                for pr in pick_results:
                    if pr.return_pct is not None:
                        logger.info(
                            f"[PickerBacktest] {td} {pr.code} → "
                            f"入场={pr.entry_price:.2f}, 出场={pr.exit_price:.2f}, "
                            f"收益={pr.return_pct:+.2f}%, 结果={pr.outcome}"
                        )
                    else:
                        logger.warning(
                            f"[PickerBacktest] {td} {pr.code} → 数据不足，无法评估"
                        )
                    results.append(pr)
            except Exception as e:
                logger.warning(f"[PickerBacktest] Date {td} failed: {e}")
                continue

        # Aggregate
        valid = [r for r in results if r.return_pct is not None]
        wins = [r for r in valid if r.outcome == "win"]
        losses = [r for r in valid if r.outcome == "loss"]
        insufficient = [r for r in results if r.outcome == "insufficient"]

        win_rate = len(wins) / len(valid) * 100 if valid else None
        avg_ret = sum(r.return_pct for r in valid) / len(valid) if valid else None
        bm_avg = sum(benchmark_returns) / len(benchmark_returns) if benchmark_returns else None
        alpha = (avg_ret - bm_avg) if (avg_ret is not None and bm_avg is not None) else None
        me_avg = sum(market_eqw_returns) / len(market_eqw_returns) if market_eqw_returns else None
        alpha_me = (avg_ret - me_avg) if (avg_ret is not None and me_avg is not None) else None

        # Max drawdown: use daily batch returns
        batch_returns: Dict[str, List[float]] = {}
        for r in valid:
            batch_returns.setdefault(r.trade_date, []).append(r.return_pct or 0)
        daily_avg = [sum(v) / len(v) for v in batch_returns.values() if v]
        cum = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in daily_avg:
            cum *= 1 + r / 100
            peak = max(peak, cum)
            dd = (peak - cum) / peak * 100
            max_dd = max(max_dd, dd)
        max_drawdown = max_dd if daily_avg else None

        # Profit factor (gross profit / gross loss)
        gross_profit = sum(r.return_pct for r in wins if r.return_pct)
        gross_loss = abs(sum(r.return_pct for r in losses if r.return_pct))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

        # ── Whole-period B&H comparison (cash-aware) ──────────────────
        # Strategy total return: compound daily-avg returns over the full
        # trading-day axis. On no-pick days the position is in cash (0%).
        # This yields a portfolio-level NAV directly comparable to a buy-
        # and-hold of the benchmark over the same calendar window.
        no_pick_days = max(0, len(trade_dates) - len(daily_avg))
        days_in_market_pct = (
            len(daily_avg) / len(trade_dates) * 100 if trade_dates else None
        )
        nav = 1.0
        for r in daily_avg:
            nav *= 1 + r / 100
        # Cash days contribute nothing (factor 1.0); ignored explicitly
        _ = no_pick_days
        strategy_total = (nav - 1.0) * 100

        # NAV-derived risk metrics. Returns are *per rebalance batch*, not
        # daily — for short windows Sharpe is informational only.
        cagr_pct = None
        sharpe_ratio = None
        calmar_ratio = None
        if daily_avg and len(trade_dates) >= 2:
            try:
                import math
                years = max(len(trade_dates) / 252.0, 1e-6)
                if nav > 0:
                    cagr = nav ** (1.0 / years) - 1.0
                    cagr_pct = cagr * 100
                    if max_drawdown and max_drawdown > 0:
                        calmar_ratio = round(cagr_pct / max_drawdown, 3)
                # Sharpe: annualised mean / stdev of per-rebalance batch returns
                # (252 trading days / hold_days = annual batches per year).
                if len(daily_avg) >= 2:
                    mean_r = sum(daily_avg) / len(daily_avg) / 100
                    var = sum((x / 100 - mean_r) ** 2 for x in daily_avg) / (len(daily_avg) - 1)
                    sd = math.sqrt(var)
                    if sd > 0:
                        batches_per_year = 252.0 / max(hold_days, 1)
                        sharpe_ratio = round(
                            (mean_r * batches_per_year) /
                            (sd * math.sqrt(batches_per_year)), 3,
                        )
                if cagr_pct is not None:
                    cagr_pct = round(cagr_pct, 2)
            except Exception as e:
                logger.debug("[PickerBacktest] NAV metrics calc failed: %s", e)

        bh_bench = self._get_index_total_return(start_date, end_date)
        bh_market = self._get_market_eqw_total_return(start_date, end_date)
        bh_alpha_bench = (
            strategy_total - bh_bench if bh_bench is not None else None
        )
        bh_alpha_market = (
            strategy_total - bh_market if bh_market is not None else None
        )

        summary = PickerBacktestSummary(
            start_date=start_date,
            end_date=end_date,
            hold_days=hold_days,
            top_n=top_n,
            total_picks=len(results),
            win_count=len(wins),
            loss_count=len(losses),
            insufficient_count=len(insufficient),
            win_rate_pct=round(win_rate, 2) if win_rate is not None else None,
            avg_return_pct=round(avg_ret, 2) if avg_ret is not None else None,
            max_drawdown_pct=round(max_drawdown, 2) if max_drawdown is not None else None,
            profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
            alpha_vs_benchmark_pct=round(alpha, 2) if alpha is not None else None,
            benchmark_avg_return_pct=round(bm_avg, 2) if bm_avg is not None else None,
            market_eqw_avg_return_pct=round(me_avg, 2) if me_avg is not None else None,
            alpha_vs_market_eqw_pct=round(alpha_me, 2) if alpha_me is not None else None,
            strategy_total_return_pct=round(strategy_total, 2),
            benchmark_total_return_pct=round(bh_bench, 2) if bh_bench is not None else None,
            market_eqw_total_return_pct=round(bh_market, 2) if bh_market is not None else None,
            bh_alpha_vs_benchmark_pct=round(bh_alpha_bench, 2) if bh_alpha_bench is not None else None,
            bh_alpha_vs_market_eqw_pct=round(bh_alpha_market, 2) if bh_alpha_market is not None else None,
            days_in_market_pct=round(days_in_market_pct, 2) if days_in_market_pct is not None else None,
            cagr_pct=cagr_pct,
            sharpe_ratio=sharpe_ratio,
            calmar_ratio=calmar_ratio,
        )

        # Log summary (cache stats: hits / total lookups, not network requests)
        hits, misses = self._data_manager.cache_stats()
        total_lookups = hits + misses
        logger.info(
            "[PickerBacktest] 回测完成: 交易日=%d, 有候选=%d天, 总选股=%d, 胜=%d, 负=%d, 数据不足=%d, "
            "胜率=%.2f%%, 平均收益=%.2f%%, Alpha=%.2f%%, 缓存命中=%d/总查询=%d",
            len(trade_dates), days_with_picks, len(results), len(wins), len(losses), len(insufficient),
            win_rate or 0, avg_ret or 0, alpha or 0, hits, total_lookups,
        )

        return {
            "results": [
                {
                    "trade_date": r.trade_date,
                    "code": r.code,
                    "name": r.name,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "return_pct": r.return_pct,
                    "outcome": r.outcome,
                    "score": r.score,
                    "exit_reason": r.exit_reason,
                    "hold_days": r.hold_days,
                    "strategy_id": r.strategy_id,
                }
                for r in results
            ],
            "summary": {
                "start_date": summary.start_date,
                "end_date": summary.end_date,
                "hold_days": summary.hold_days,
                "top_n": summary.top_n,
                "trade_dates_with_picks": days_with_picks,
                "total_picks": summary.total_picks,
                "win_count": summary.win_count,
                "loss_count": summary.loss_count,
                "insufficient_count": summary.insufficient_count,
                "win_rate_pct": summary.win_rate_pct,
                "avg_return_pct": summary.avg_return_pct,
                "max_drawdown_pct": summary.max_drawdown_pct,
                "profit_factor": summary.profit_factor,
                "alpha_vs_benchmark_pct": summary.alpha_vs_benchmark_pct,
                "benchmark_avg_return_pct": summary.benchmark_avg_return_pct,
                "market_eqw_avg_return_pct": summary.market_eqw_avg_return_pct,
                "alpha_vs_market_eqw_pct": summary.alpha_vs_market_eqw_pct,
                "strategy_total_return_pct": summary.strategy_total_return_pct,
                "benchmark_total_return_pct": summary.benchmark_total_return_pct,
                "market_eqw_total_return_pct": summary.market_eqw_total_return_pct,
                "bh_alpha_vs_benchmark_pct": summary.bh_alpha_vs_benchmark_pct,
                "bh_alpha_vs_market_eqw_pct": summary.bh_alpha_vs_market_eqw_pct,
                "days_in_market_pct": summary.days_in_market_pct,
                "cagr_pct": summary.cagr_pct,
                "sharpe_ratio": summary.sharpe_ratio,
                "calmar_ratio": summary.calmar_ratio,
            },
            "trade_dates_count": len(trade_dates),
        }
