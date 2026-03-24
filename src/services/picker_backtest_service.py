# -*- coding: utf-8 -*-
"""
Picker Backtest Service

Runs the quantitative screener historically and evaluates forward returns.
Uses top N by score (no LLM) for each trade date.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from data_provider.base import DataFetcherManager
from data_provider.caching_manager import CachingDataFetcherManager
from src.config import get_config
from src.services.stock_picker_service import (
    StockScreener,
    create_screener_from_config,
    get_tushare_api,
    ScreenedStock,
)

logger = logging.getLogger(__name__)

BENCHMARK_CODE = "000300.SH"  # CSI 300

# Stop-loss / take-profit (买卖点规则: 跌破 MA20 或一定跌幅；目标位 前高、整数关口)
STOP_LOSS_PCT = -8.0   # 止损：跌幅超过 8%（一定跌幅）
TAKE_PROFIT_PCT = 15.0  # 止盈：涨幅超过 15%（兜底，主用前高/整数关口）
GATEWAY_LEVELS = (5, 10, 20, 50, 100, 200, 500, 1000)  # 整数关口


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
        """Get Tushare API for trade_cal and benchmark (cached)."""
        if self._tushare_api is None:
            self._tushare_api = get_tushare_api(self._data_manager)
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
        stop_loss_pct: float = STOP_LOSS_PCT,
        take_profit_pct: float = TAKE_PROFIT_PCT,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Fetch daily data and compute return with stop-loss/take-profit per 买卖点规则.
        Stop-loss: 跌破 MA20 or 一定跌幅; Take-profit: 前高, 整数关口, or fallback 15%.
        Returns (exit_price, return_pct)."""
        try:
            # Need ~25 trading days before entry for MA20 and 前高
            start_dt = pd.Timestamp(trade_date) - pd.Timedelta(days=45)
            start_iso = start_dt.strftime("%Y-%m-%d")
            end_iso = f"{exit_date[:4]}-{exit_date[4:6]}-{exit_date[6:8]}"
            df, _ = self._data_manager.get_daily_data(
                code, start_date=start_iso, end_date=end_iso, days=80
            )
            if df is None or df.empty:
                return None, None
            date_col = next((c for c in ["date", "日期"] if c in df.columns), df.columns[0])
            close_col = next((c for c in ["close", "收盘"] if c in df.columns), None)
            high_col = next((c for c in ["high", "最高"] if c in df.columns), None)
            if close_col is None:
                return None, None
            df = df.sort_values(date_col).reset_index(drop=True)
            df[date_col] = pd.to_datetime(df[date_col])
            df["_date_str"] = df[date_col].dt.strftime("%Y-%m-%d")

            # MA20 (base fetcher may add it; compute if missing)
            if "ma20" not in df.columns:
                df["ma20"] = df[close_col].rolling(window=20, min_periods=1).mean()

            # Find entry row (trade_date) and subsequent rows
            entry_str = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
            entry_mask = df["_date_str"] == entry_str
            if not entry_mask.any():
                return None, None
            entry_idx = int(df.index[entry_mask].min())
            if entry_price <= 0:
                return None, None

            # 前高: max high in 20 trading days before entry (exclusive)
            prior_high: Optional[float] = None
            if high_col and entry_idx >= 20:
                prior_high = float(df.iloc[entry_idx - 20 : entry_idx][high_col].max())

            # 整数关口: next level above entry
            next_gateway: Optional[float] = None
            for g in GATEWAY_LEVELS:
                if g > entry_price:
                    next_gateway = float(g)
                    break

            # Iterate days after entry: stop-loss (跌破 MA20 or 一定跌幅) / take-profit (前高, 整数关口, fallback)
            for i in range(entry_idx + 1, len(df)):
                close = float(df.iloc[i][close_col])
                ret = (close - entry_price) / entry_price * 100

                # Stop-loss: 跌破 MA20 or 一定跌幅
                ma20_val = df.iloc[i].get("ma20")
                if pd.notna(ma20_val) and close < float(ma20_val):
                    return close, ret  # 跌破 MA20
                if ret <= stop_loss_pct:
                    return close, ret  # 一定跌幅

                # Take-profit: 前高, 整数关口, or fallback
                if prior_high is not None and close >= prior_high:
                    return close, ret  # 前高
                if next_gateway is not None and close >= next_gateway:
                    return close, ret  # 整数关口
                if ret >= take_profit_pct:
                    return close, ret  # Fallback 15%

            # No trigger: exit at planned exit_date
            exit_str = f"{exit_date[:4]}-{exit_date[4:6]}-{exit_date[6:8]}"
            exit_mask = df["_date_str"] == exit_str
            if not exit_mask.any():
                return None, None
            exit_price = float(df.loc[exit_mask, close_col].iloc[-1])
            ret = (exit_price - entry_price) / entry_price * 100
            return exit_price, ret
        except Exception as e:
            logger.debug(f"[PickerBacktest] Forward return failed {code}: {e}")
            return None, None

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
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="fwd") as pool:
            futures = {
                pool.submit(
                    self._get_forward_return, s.code, trade_date, exit_date, s.price
                ): s
                for s in picks
            }
            for fut in as_completed(futures):
                s = futures[fut]
                try:
                    exit_price, ret = fut.result()
                except Exception as e:
                    logger.debug(f"[PickerBacktest] Forward return failed {s.code}: {e}")
                    results.append(
                        PickResult(
                            trade_date=trade_date,
                            code=s.code,
                            name=s.name,
                            entry_price=s.price,
                            exit_price=None,
                            return_pct=None,
                            outcome="insufficient",
                            score=s.score,
                        )
                    )
                    continue
                if ret is None:
                    results.append(
                        PickResult(
                            trade_date=trade_date,
                            code=s.code,
                            name=s.name,
                            entry_price=s.price,
                            exit_price=exit_price,
                            return_pct=None,
                            outcome="insufficient",
                            score=s.score,
                        )
                    )
                else:
                    outcome = "win" if ret > 0 else "loss"
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
            picker_strategies: optional override (buy_pullback, breakout, etc.)

        Returns:
            Dict with results, summary, and performance metrics.
        """
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
        benchmark_returns: List[float] = []

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
            },
            "trade_dates_count": len(trade_dates),
        }
