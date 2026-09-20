# -*- coding: utf-8 -*-
"""Bottom-reversal v2: bottom → consolidation → about-to-launch.

Replaces the legacy "60-day decline + today rising" reversal screen.
The intent is the *second / third buy point* (not the first):

    1. The stock has fallen meaningfully from a prior peak (true bottom).
    2. It has already stopped falling and has been ranging for weeks
       (the consolidation that precedes a real launch).
    3. Price is sitting near the upper edge of that range, MA60 is no
       longer falling, and volume is "thinking about" expanding.

This is intentionally a medium-term swing (20-60 trading days), not a
day-trade. Win-rate target ~50-55% with larger per-trade payoff.

Pipeline:
  spot DataFrame (5495 rows from _fetch_spot_data)
    └── coarse pre-filter on spot columns (cheap, vectorised)
         └── parallel LocalDB lookup per candidate (180d daily bars)
              └── multi-window geometric tests
                   └── score & return top-N

LocalDB is the canonical source for the 180-day window. The pipeline
already pays the cost to assemble the full spot DataFrame, so we reuse
it for the cheap pre-filter and only pay per-symbol I/O on what passes.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import numpy as np
import pandas as pd

from src.services.picker.constants import ScreenedStock

logger = logging.getLogger(__name__)


# Window sizes (trading days).
_LOOKBACK_DAYS = 180          # full history pulled per candidate
_DEEP_BASE_WIN = 180          # drawdown reference window
_BOTTOM_WIN = 120             # "low base" window
_CONSOL_WIN = 40              # consolidation / range window
_MA_LONG = 60                 # long-term MA used for trend filter
_VOL_SHORT = 5                # short volume average
_VOL_LONG = 60                # long volume average

# Default thresholds. Tuned for a *watchlist* — i.e. surface stocks
# that are genuinely sitting in a real bottom right now, for manual
# review. We deliberately do NOT require "about-to-break" geometry
# (price-pos near range top, MA60 already turning up) — those are
# right-side breakout signals. Here we
# want the earlier picture: real fall happened, range has formed,
# price still inside the range. Each value is env-overridable for
# discretionary tuning.
_DEFAULTS = {
    "MIN_DRAWDOWN_PCT": 25.0,    # 25% drop already qualifies as a base
    "MIN_BOUNCE_PCT": 5.0,       # don't require a strong rebound — we want
                                  # stocks still IN the base, not leaving it
    "MAX_RANGE_PCT": 30.0,       # A-share consolidations 25-30% are common
    "MIN_RANGE_DAYS": 20,
    "VOL_RATIO_MIN": 0.6,        # don't gate on volume — bases run dry
    "VOL_RATIO_MAX": 2.5,
    "MA60_SLOPE_MIN_PCT": -3.0,  # real bottoms usually have a still-falling
                                  # MA60; only reject "free-fall" cases
    "PRICE_POS_LOW": 0.30,       # accept stocks anywhere in the lower-to-mid
    "PRICE_POS_HIGH": 0.85,      # range; exclude only the very top
    "MAX_PB": 4.0,
    "MARKET_CAP_MIN_YI": 30.0,
    "MARKET_CAP_MAX_YI": 300.0,
    "COARSE_60D_MIN_PCT": -50.0,
    "COARSE_60D_MAX_PCT": 10.0,
    "MIN_AMOUNT_YUAN": 80_000_000.0,
    "TOP_N": 20,                 # plenty for daily manual review
    "MAX_PARALLEL": 16,
}


def _envf(key: str) -> float:
    raw = os.environ.get(f"BOTTOM_REV2_{key}")
    if raw is None:
        return float(_DEFAULTS[key])
    try:
        return float(raw)
    except ValueError:
        return float(_DEFAULTS[key])


def _envi(key: str) -> int:
    return int(_envf(key))


class _BottomReversalV2Mixin:
    """Mixin: ``_screen_bottom_reversal_v2``."""

    def _screen_bottom_reversal_v2(
        self,
        spot_df: Optional[pd.DataFrame],
        trade_date_yyyymmdd: Optional[str] = None,
    ) -> List[ScreenedStock]:
        if spot_df is None or spot_df.empty:
            logger.info("[BottomRev2] no spot data")
            return []

        candidates = self._bottom_rev2_coarse_filter(spot_df)
        if not candidates:
            logger.info("[BottomRev2] coarse filter dropped everything")
            return []
        logger.info("[BottomRev2] coarse pool: %d candidates", len(candidates))

        as_of = trade_date_yyyymmdd or getattr(self, "_as_of_date", None)
        if as_of and "-" in as_of:
            as_of = as_of.replace("-", "")

        survivors = self._bottom_rev2_deep_filter(candidates, as_of_yyyymmdd=as_of)
        if not survivors:
            logger.info("[BottomRev2] geometric filter dropped everything")
            return []

        top_n = _envi("TOP_N")
        survivors.sort(key=lambda s: s.score, reverse=True)
        out = survivors[:top_n]
        logger.info("[BottomRev2] final: %d picks (top %d of %d survivors)",
                    len(out), top_n, len(survivors))
        return out

    # ------------------------------------------------------------------
    # Coarse: keep ~hundreds of candidates from the 5495-row spot frame.
    # ------------------------------------------------------------------

    def _bottom_rev2_coarse_filter(self, df: pd.DataFrame) -> List[dict]:
        if df is None or df.empty:
            return []
        panel = df.copy()

        # Drop ST / 退市 / *ST
        if "名称" in panel.columns:
            panel = panel[~panel["名称"].astype(str).str.contains(
                r"ST|退|\*", regex=True, na=False,
            )]
        # Drop BSE
        if "代码" in panel.columns:
            code_str = panel["代码"].astype(str)
            panel = panel[~code_str.str.startswith(("8", "4", "92"))]

        # PB cap (skip rows without PB by default)
        if "市净率" in panel.columns:
            pb = pd.to_numeric(panel["市净率"], errors="coerce")
            panel = panel[pb.notna() & (pb > 0) & (pb <= _envf("MAX_PB"))]

        # Market cap band (in yuan; convert to 亿 for thresholds)
        if "总市值" in panel.columns:
            mv_yi = pd.to_numeric(panel["总市值"], errors="coerce") / 1e8
            panel = panel[(mv_yi >= _envf("MARKET_CAP_MIN_YI"))
                          & (mv_yi <= _envf("MARKET_CAP_MAX_YI"))]

        # Liquidity floor (今日 成交额)
        if "成交额" in panel.columns:
            amt = pd.to_numeric(panel["成交额"], errors="coerce").fillna(0)
            panel = panel[amt >= _envf("MIN_AMOUNT_YUAN")]

        # 60d change band (already on the spot frame after _add_tushare_60d_change)
        if "60日涨跌幅" in panel.columns:
            ch60 = pd.to_numeric(panel["60日涨跌幅"], errors="coerce")
            panel = panel[ch60.notna()
                          & (ch60 >= _envf("COARSE_60D_MIN_PCT"))
                          & (ch60 <= _envf("COARSE_60D_MAX_PCT"))]

        # Exclude limit-up / limit-down on entry day (can't fill cleanly)
        if "涨跌幅" in panel.columns:
            day = pd.to_numeric(panel["涨跌幅"], errors="coerce").fillna(0)
            panel = panel[(day > -9.5) & (day < 9.5)]

        if panel.empty:
            return []

        # Materialise the rows we need (avoid passing DataFrame slices to
        # worker threads).
        keep = ["代码", "名称", "最新价", "涨跌幅", "量比", "换手率",
                "市盈率-动态", "市净率", "总市值", "成交额", "60日涨跌幅"]
        keep = [c for c in keep if c in panel.columns]
        rows = panel[keep].to_dict(orient="records")
        return rows

    # ------------------------------------------------------------------
    # Deep: multi-window geometric check per candidate, parallel.
    # ------------------------------------------------------------------

    def _bottom_rev2_deep_filter(
        self, candidates: List[dict], as_of_yyyymmdd: Optional[str],
    ) -> List[ScreenedStock]:
        try:
            from src.services.local_db import default_db
        except Exception as e:
            logger.warning("[BottomRev2] LocalDB unavailable: %s", e)
            return []
        db = default_db()

        # Resolve the as_of date — falls back to today if not given.
        if not as_of_yyyymmdd:
            from datetime import date as _date
            as_of_yyyymmdd = _date.today().strftime("%Y%m%d")
        try:
            as_of_ts = pd.Timestamp(as_of_yyyymmdd)
        except Exception:
            return []
        # Pull ~270 calendar days to be safe for 180 trading days.
        start_yyyymmdd = (as_of_ts - pd.Timedelta(days=270)).strftime("%Y%m%d")

        max_workers = max(1, _envi("MAX_PARALLEL"))
        results: List[ScreenedStock] = []

        def _judge(row: dict) -> Optional[ScreenedStock]:
            code = str(row.get("代码", "") or "").strip()
            if not code:
                return None
            ts_code = code if "." in code else (
                f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.SZ"
            )
            try:
                bars = db.get_daily(ts_code, start_yyyymmdd, as_of_yyyymmdd)
            except Exception:
                return None
            if bars is None or bars.empty or len(bars) < _CONSOL_WIN + 5:
                return None
            return self._bottom_rev2_judge_one(row, bars)

        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="bottom_rev2") as pool:
            futs = [pool.submit(_judge, r) for r in candidates]
            for fut in as_completed(futs):
                try:
                    s = fut.result()
                except Exception:
                    s = None
                if s is not None:
                    results.append(s)
        return results

    # ------------------------------------------------------------------
    # Per-symbol judgement — the actual geometry.
    # ------------------------------------------------------------------

    @staticmethod
    def _bottom_rev2_judge_one(
        row: dict, bars: pd.DataFrame,
    ) -> Optional[ScreenedStock]:
        # Normalise columns. LocalDB schema: trade_date, open, high, low,
        # close, pre_close, pct_chg, vol, amount.
        if bars is None or bars.empty:
            return None
        bars = bars.sort_values("trade_date").reset_index(drop=True)
        if len(bars) < _CONSOL_WIN + 5:
            return None

        close = bars["close"].astype(float).values
        high = bars["high"].astype(float).values
        low = bars["low"].astype(float).values
        vol = bars["vol"].astype(float).values

        current = float(close[-1])
        if current <= 0:
            return None

        # --- 1. drawdown over the long window ---
        win_180 = close[-_DEEP_BASE_WIN:] if len(close) >= _DEEP_BASE_WIN else close
        peak = float(np.max(win_180))
        trough = float(np.min(win_180))
        if peak <= 0:
            return None
        max_dd_pct = (peak - trough) / peak * 100.0
        if max_dd_pct < _envf("MIN_DRAWDOWN_PCT"):
            return None

        # --- 2. bounce from 120d low ---
        win_120 = close[-_BOTTOM_WIN:] if len(close) >= _BOTTOM_WIN else close
        low_120 = float(np.min(win_120))
        if low_120 <= 0:
            return None
        bounce_pct = (current - low_120) / low_120 * 100.0
        if bounce_pct < _envf("MIN_BOUNCE_PCT"):
            return None

        # --- 3. recent consolidation: 40d high/low range ---
        win_40_high = high[-_CONSOL_WIN:]
        win_40_low = low[-_CONSOL_WIN:]
        rng_high = float(np.max(win_40_high))
        rng_low = float(np.min(win_40_low))
        if rng_low <= 0 or rng_high <= 0:
            return None
        range_pct = (rng_high - rng_low) / rng_low * 100.0
        if range_pct > _envf("MAX_RANGE_PCT"):
            return None
        # Reject if essentially zero range (suspended / 1 bar)
        if len(win_40_high) < _envi("MIN_RANGE_DAYS"):
            return None

        # --- 4. price position inside the range ---
        if rng_high == rng_low:
            return None
        price_pos = (current - rng_low) / (rng_high - rng_low)
        if not (_envf("PRICE_POS_LOW") <= price_pos <= _envf("PRICE_POS_HIGH")):
            return None

        # --- 5. volume regime: short / long ---
        vol_short = float(np.mean(vol[-_VOL_SHORT:]))
        vol_long = float(np.mean(vol[-_VOL_LONG:])) if len(vol) >= _VOL_LONG else \
            float(np.mean(vol))
        if vol_long <= 0:
            return None
        vol_ratio = vol_short / vol_long
        if not (_envf("VOL_RATIO_MIN") <= vol_ratio <= _envf("VOL_RATIO_MAX")):
            return None

        # --- 6. MA60 slope (now vs 20 bars ago) ---
        if len(close) >= _MA_LONG + 20:
            ma60_series = pd.Series(close).rolling(_MA_LONG).mean().values
            ma60_now = float(ma60_series[-1])
            ma60_20ago = float(ma60_series[-21])
            if ma60_now <= 0 or ma60_20ago <= 0:
                return None
            ma60_slope_pct = (ma60_now - ma60_20ago) / ma60_20ago * 100.0
            if ma60_slope_pct < _envf("MA60_SLOPE_MIN_PCT"):
                return None
        else:
            ma60_slope_pct = 0.0  # short history: don't penalise

        # --- score (0-100): reward depth of base + tight range + ---
        # rising MA60 + upper-range price position
        # Watchlist scoring: reward "deeper base + tighter range + still
        # has upside room (price NOT yet near top) + MA60 stabilising".
        # Inverted price_pos vs right-side breakout: we want stocks
        # that haven't already run.
        score = 0.0
        score += min(30.0, max_dd_pct * 0.6)              # deep base
        score += 20.0 * (1.0 - min(1.0, range_pct / 30))  # tighter range
        score += 25.0 * (1.0 - price_pos)                 # MORE room above
        # MA60 slope: stop punishing falling — only reward flat/rising.
        # Clip to [-3, +3] then map to [0, 15].
        slope_clipped = max(-3.0, min(3.0, ma60_slope_pct))
        score += 15.0 * (slope_clipped + 3.0) / 6.0
        score += 10.0 * min(1.0, vol_ratio / 1.5)         # volume
        score = round(score, 2)

        code = str(row.get("代码", "") or "")
        price = float(pd.to_numeric(row.get("最新价", current), errors="coerce") or current)
        return ScreenedStock(
            code=code,
            name=str(row.get("名称", "") or ""),
            price=price,
            change_pct=float(pd.to_numeric(row.get("涨跌幅", 0), errors="coerce") or 0),
            volume_ratio=float(pd.to_numeric(row.get("量比", 0), errors="coerce") or 0),
            turnover_rate=float(pd.to_numeric(row.get("换手率", 0), errors="coerce") or 0),
            pe=float(pd.to_numeric(row.get("市盈率-动态", 0), errors="coerce") or 0),
            pb=float(pd.to_numeric(row.get("市净率", 0), errors="coerce") or 0),
            market_cap=float(pd.to_numeric(row.get("总市值", 0), errors="coerce") or 0) / 1e8,
            amount=float(pd.to_numeric(row.get("成交额", 0), errors="coerce") or 0) / 1e8,
            change_pct_60d=float(pd.to_numeric(row.get("60日涨跌幅", 0), errors="coerce") or 0),
            score=score,
            strategies=["bottom_reversal"],
        )
