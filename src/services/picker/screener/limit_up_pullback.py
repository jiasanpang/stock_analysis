# -*- coding: utf-8 -*-
"""Limit-up pullback ("涨停回踩买入法") — the buy_pullback engine, reworked.

Pipeline (per EOD run, stateless re-derivation from history so it is
fully back-testable):

    1. CANDIDATE UNIVERSE  tushare ``limit_list_d`` over the last
       OBS_WINDOW trading days (fallback: derive limit-ups from daily
       bars when the pool has no permission/rows for a date).
    2. LIMIT-UP-DAY QUALITY (6 hard standards + forbidden boards):
       early solid body board (first_time<=10:30, non one-line,
       few reopens), turnover 10-20%, mild volume expansion 1.5-3x,
       mid-trend position (prior rise 30-70%, never doubled),
       MA5>=MA10>=MA20 with MA20 rising, ST/BSE excluded, boards set
       while the SSE index was not crashing (孤板 veto).
    3. OBSERVATION (D+1..T, max 5 sessions): any break below the
       limit-up open, any high-volume bearish day (放量回调), or a
       close below a falling MA20 kills the event.
    4. PATTERN CONFIRMATION at T (today must NOT be limit-up):
       回踩5日线 / 回踩10日线 / 三阴不破阳 / 假阴真阳.
    5. TRADE LEVELS via ``compute_limit_up_pullback_levels`` —
       structural stop at the limit-up candle low / -4% (tighter),
       +10% half-off then MA10/ATR trailing.

Not implemented (needs tushare ``share_float``, P2): 解禁前1个月 veto.

Runs as an EOD strategy: intraday the current-day bar is not in
LocalStockDB yet, so confirmation simply appears after the sync.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.services.picker.constants import ScreenedStock, _get_limit_up_pct

logger = logging.getLogger(__name__)

_INDEX_CODE = "000001.SH"

_DEFAULTS = {
    "OBS_WINDOW": 5,            # trading sessions after the limit-up day
    # 教科书最优板(10:30 前封板 + 换手 10-20% + 温和放量)在真实盘面占比
    # <4%（2026-02..05 实测），全部硬否决会零召回。故只设红线否决，
    # 教科书区间在 _lup_build_stock 里以加分体现排序优先级。
    "FIRST_TIME_MAX": 140000,   # HHMMSS hard cut（14点后偷袭板否决）
    "MAX_OPEN_NUM": 5,          # 反复炸板>5次否决；0次加分
    "MAX_LIMIT_TIMES": 5,      # >=5 = 高位连板末尾, forbidden
    "TURNOVER_MIN": 1.0,        # hard band (ideal 10-20 scored)
    "TURNOVER_MAX": 40.0,
    "VOL_EXPAND_MIN": 1.0,      # 缩量板否决；1.5-3 温和放量加分
    "VOL_EXPAND_MAX": 8.0,      # 极端爆量否决
    "PRE_GAIN_MIN": 5.0,        # rise from 60d low into the limit-up day
    "PRE_GAIN_MAX": 100.0,      # hard band (ideal 30-70 scored)
    "MAX_TOTAL_GAIN": 150.0,    # 趋势末端巨涨幅否决
    "PULLBACK_VOL_MAX": 0.6,    # confirmation-day vol / limit-up vol
    "BIG_YIN_VOL_RATIO": 0.9,   # bearish day >=0.9x limit-up vol = 出货
    "INDEX_CRASH_PCT": -1.5,    # limit-up day set while SSE <= this: 孤板
    "CHASE_CAP_PCT": 8.0,       # entry not more than this above limit-up close
    "ALLOW_CONSOLIDATION": 0.0, # 1 = 第五形态"缩量横盘"兜底（先回测再默认开）
    "CONSOL_VOL_MAX": 0.45,     # 兜底形态的缩量上限（比四形态 0.6 更严）
    "TOP_N": 20,
}

_PATTERN_STRENGTH = {
    "三阴不破阳": 92.0,
    "回踩5日线": 85.0,
    "回踩10日线": 82.0,
    "假阴真阳": 75.0,
    "缩量横盘": 66.0,
}

_PATTERN_ENV_ID = {
    "三阴不破阳": "THREE_YIN",
    "回踩5日线": "MA5",
    "回踩10日线": "MA10",
    "假阴真阳": "FAKE_YIN",
    "缩量横盘": "CONSOLIDATION",
}


def _envf(key: str) -> float:
    raw = os.environ.get(f"LUP_{key}")
    if raw is None:
        return float(_DEFAULTS[key])
    try:
        return float(raw)
    except ValueError:
        return float(_DEFAULTS[key])


def limit_up_pullback_enabled() -> bool:
    """Master switch: buy_pullback routes to this engine unless LUP_ENABLED=0."""
    return os.environ.get("LUP_ENABLED", "1") == "1"


def _pattern_enabled(name: str) -> bool:
    return os.environ.get(
        f"LUP_PATTERN_{_PATTERN_ENV_ID.get(name, name)}", "1") == "1"


def _first_time_seconds(v) -> Optional[int]:
    """'09:32:03' / '093203' -> 34383; None when unknown."""
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    if len(digits) < 6:
        return None
    if len(digits) > 6:
        digits = digits[-6:]
    try:
        h, m, s = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
    except ValueError:
        return None
    if h > 23 or m > 59 or s > 59:
        return None
    return h * 3600 + m * 60 + s


def _hhmmss_to_seconds(raw: float) -> int:
    """103000 (HHMMSS) -> 37800 (seconds since midnight)."""
    v = int(raw)
    return (v // 10000) * 3600 + (v % 10000 // 100) * 60 + v % 100


def _ev_num(ev: dict, *keys: str):
    """First numeric value among alias keys. tushare / localdb expose the
    reopen count as ``open_num`` or ``open_times`` depending on vintage."""
    for k in keys:
        v = pd.to_numeric(pd.Series([ev.get(k)]), errors="coerce").iloc[0]
        if pd.notna(v):
            return float(v)
    return None


def _col(df: pd.DataFrame, *names: str) -> Optional[str]:
    lower = {c.lower(): c for c in df.columns}
    for n in names:
        if n in df.columns:
            return n
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _ma(arr: np.ndarray, window: int, i: int) -> float:
    if i - window + 1 < 0:
        return float("nan")
    seg = arr[i - window + 1:i + 1]
    if np.isnan(seg).any():
        return float("nan")
    return float(seg.mean())


class _LimitUpPullbackMixin:
    """Mixin: ``_screen_limit_up_pullback`` (wired as the buy_pullback engine)."""

    def _screen_limit_up_pullback(
        self,
        spot_df: Optional[pd.DataFrame],
        trade_date_yyyymmdd: Optional[str] = None,
        sector_strong_codes: Optional[Set[str]] = None,
    ) -> List[ScreenedStock]:
        as_of = trade_date_yyyymmdd or getattr(self, "_as_of_date", None)
        as_of = as_of.replace("-", "") if as_of else datetime.now(
            ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        obs_window = int(_envf("OBS_WINDOW"))
        pool_dates = self._lup_recent_sessions(as_of, obs_window + 1)
        if not pool_dates:
            logger.info("[LUP] no trade calendar around %s, skip", as_of)
            return []

        pool = self._lup_load_pool(pool_dates)
        if pool.empty:
            logger.info("[LUP] empty limit-up pool for %s..%s",
                        pool_dates[0], pool_dates[-1])
            return []

        index_pct = self._lup_index_pct_map(pool_dates[0], as_of)
        events = self._lup_latest_events(pool, pool_dates, as_of)
        if not events:
            return []
        logger.info("[LUP] %d anchor events in observation window", len(events))

        out: List[ScreenedStock] = []
        dropped: Dict[str, int] = {}
        for ev in events:
            try:
                stock = self._lup_judge_one(ev, as_of, index_pct, sector_strong_codes)
            except Exception as e:  # one bad candidate must not kill the run
                logger.debug("[LUP] %s judge error: %s", ev.get("ts_code"), e)
                stock = None
            if stock is None:
                reason = str(ev.pop("_drop", "none"))
                dropped[reason] = dropped.get(reason, 0) + 1
            else:
                out.append(stock)
        logger.info("[LUP] judged %d -> %d picks; drops=%s",
                    len(events), len(out), dropped or "-")
        out.sort(key=lambda s: s.score, reverse=True)
        return out[:int(_envf("TOP_N"))]

    # ------------------------------------------------------------------
    # Public: next-day watch pool (alive events awaiting pattern confirm)
    # ------------------------------------------------------------------

    def limit_up_watch_candidates(
        self, trade_date_yyyymmdd: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Quality-passed limit-up events still alive in the observation
        window but without a confirmed buy pattern today.

        Returns dicts with: code, name, limit_up_date, days_waiting, close,
        break_level (跌破即出局), ma5, ma10. Empty list when LUP is off or
        data is unavailable.
        """
        if not limit_up_pullback_enabled():
            return []
        as_of = trade_date_yyyymmdd or getattr(self, "_as_of_date", None)
        as_of = as_of.replace("-", "") if as_of else datetime.now(
            ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        pool_dates = self._lup_recent_sessions(as_of, int(_envf("OBS_WINDOW")) + 1)
        if not pool_dates:
            return []
        pool = self._lup_load_pool(pool_dates)
        if pool.empty:
            return []
        index_pct = self._lup_index_pct_map(pool_dates[0], as_of)
        out: List[Dict[str, Any]] = []
        for ev in self._lup_latest_events(pool, pool_dates, as_of):
            try:
                entry = self._lup_watch_entry(ev, as_of, index_pct)
            except Exception as e:  # keep one bad event from killing the pool
                logger.debug("[LUP] %s watch error: %s", ev.get("ts_code"), e)
                entry = None
            if entry is not None:
                out.append(entry)
        out.sort(key=lambda d: d["days_waiting"], reverse=True)
        return out

    def _lup_watch_entry(
        self, ev: dict, as_of: str, index_pct: Dict[str, float],
    ) -> Optional[Dict[str, Any]]:
        prep = self._lup_prepare(ev, as_of, index_pct)
        if prep is None:
            return None
        bars, i_d, i_t = prep
        pattern, reason = self._lup_detect_pattern(bars, i_d, i_t, ev)
        # "no_pattern" = still cooking; today's board = re-locks, watch next day
        if pattern is not None or reason not in ("no_pattern",
                                                 "today_limit_up_unbuyable"):
            return None
        c = bars["close"].astype(float).values
        o = bars["open"].astype(float).values
        m5, m10 = _ma(c, 5, i_t), _ma(c, 10, i_t)
        return {
            "code": str(ev.get("ts_code", ""))[:6],
            "name": str(ev.get("name", "") or ""),
            "limit_up_date": str(ev["trade_date"]),
            "days_waiting": i_t - i_d,
            "close": float(c[i_t]),
            "break_level": float(o[i_d]),
            "ma5": 0.0 if np.isnan(m5) else float(m5),
            "ma10": 0.0 if np.isnan(m10) else float(m10),
        }

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _lup_recent_sessions(self, as_of: str, n: int) -> List[str]:
        try:
            from src.services.local_db import default_db
            cal = default_db().get_trade_cal("SSE")
            if cal is not None and not cal.empty:
                if "is_open" in cal.columns:
                    cal = cal[cal["is_open"].astype(str) == "1"]
                days = sorted(str(d) for d in cal["cal_date"].tolist())
                days = [d for d in days if d <= as_of]
                if days:
                    return days[-n:]
        except Exception as e:
            logger.debug("[LUP] localdb cal failed: %s", e)
        end = pd.Timestamp(as_of)
        days = pd.bdate_range(end=end, periods=n).strftime("%Y%m%d").tolist()
        return days

    def _lup_load_pool(self, dates: List[str]) -> pd.DataFrame:
        """limit_list_d rows for the window; per-date daily-bar fallback."""
        frames: List[pd.DataFrame] = []
        api = None
        try:
            api = self._get_tushare_api()
        except Exception:
            api = None
        for td in dates:
            df = None
            if api is not None:
                try:
                    df = api.limit_list_d(trade_date=td)
                except Exception as e:
                    logger.debug("[LUP] limit_list_d %s failed: %s", td, e)
                    df = None
            if df is not None and not df.empty:
                df = df.copy()
                df.columns = [c.lower() for c in df.columns]
                df["trade_date"] = td
                if "limit" in df.columns:
                    df = df[df["limit"].astype(str).str.upper() == "U"]
                frames.append(df)
            else:
                derived = self._lup_derive_pool(td)
                if derived is not None and not derived.empty:
                    frames.append(derived)
        if not frames:
            return pd.DataFrame()
        pool = pd.concat(frames, ignore_index=True)
        pool["trade_date"] = pool["trade_date"].astype(str)
        return pool

    def _lup_derive_pool(self, trade_date: str) -> Optional[pd.DataFrame]:
        """No limit_list_d rows: mark limit-ups off the bulk daily bar."""
        try:
            api = self._get_tushare_api()
            if api is None:
                return None
            df = api.daily(trade_date=trade_date,
                           fields="ts_code,name,close,pct_chg,open,high,low,vol")
        except Exception:
            return None
        if df is None or df.empty:
            return None
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        pct = pd.to_numeric(df["pct_chg"], errors="coerce").fillna(0)
        thresholds = np.array([
            _get_limit_up_pct(str(c)[:6], str(n or ""))
            for c, n in zip(df["ts_code"], df.get("name", [""] * len(df)))
        ])
        hit = df[pct >= thresholds - 0.2].copy()
        if hit.empty:
            return None
        hit["turnover_ratio"] = np.nan
        hit["limit_times"] = np.nan
        hit["open_num"] = np.nan
        hit["first_time"] = ""
        hit["trade_date"] = trade_date
        return hit

    def _lup_index_pct_map(self, start: str, end: str) -> Dict[str, float]:
        """SSE composite pct_chg per date (孤板 veto). Empty map on failure."""
        try:
            from src.services.local_db import default_db
            df = default_db().get_index_daily(_INDEX_CODE, start, end)
            if df is None or df.empty:
                return {}
            date_col = _col(df, "trade_date", "date")
            pct_col = _col(df, "pct_chg", "pct_change")
            if not date_col or not pct_col:
                return {}
            return {str(d)[:8]: float(p)
                    for d, p in zip(df[date_col], df[pct_col])}
        except Exception:
            return {}

    @staticmethod
    def _lup_latest_events(
        pool: pd.DataFrame, pool_dates: List[str], as_of: str,
    ) -> List[dict]:
        """One anchor event per code: the most recent limit-up before as_of."""
        usable = [d for d in pool_dates if d < as_of] or pool_dates[:-1]
        if not usable:
            return []
        events: List[dict] = []
        seen: Set[str] = set()
        for td in sorted(usable, reverse=True):
            day_rows = pool[pool["trade_date"] == td]
            for row in day_rows.to_dict(orient="records"):
                code = str(row.get("ts_code", ""))
                if not code or code in seen:
                    continue
                if code.split(".")[0].startswith(("8", "4", "92")):
                    continue
                seen.add(code)
                row["trade_date"] = td
                events.append(row)
        return events

    def _lup_bars(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        try:
            from src.services.local_db import default_db
            bars = default_db().get_daily(ts_code, start, end)
        except Exception:
            bars = None
        if bars is not None and not bars.empty:
            return bars
        dm = getattr(self, "_data_manager", None)
        if dm is not None:
            try:
                df, _src = dm.get_daily_data(ts_code, start_date=start,
                                             end_date=end)
                if df is not None and not df.empty:
                    out = df.reset_index()
                    date_col = _col(out, "trade_date", "date")
                    if date_col:
                        out[date_col] = pd.to_datetime(
                            out[date_col]).dt.strftime("%Y%m%d")
                    return out
            except Exception as e:
                logger.debug("[LUP] data_manager bars %s failed: %s", ts_code, e)
        return pd.DataFrame()

    # ------------------------------------------------------------------
    # Per-event judgement: quality -> observation veto -> pattern -> levels
    # ------------------------------------------------------------------

    def _lup_prepare(
        self, ev: dict, as_of: str, index_pct: Dict[str, float],
    ) -> Optional[Tuple[pd.DataFrame, int, int]]:
        """Shared per-event prep: ST/index gates, bars, quality, veto.

        Returns (bars, i_d, i_t) when the event is still alive, else None
        with the drop reason recorded in ``ev["_drop"]``.
        """
        ts_code = str(ev.get("ts_code", ""))
        name = str(ev.get("name", "") or "")
        if "ST" in name.upper() or "退" in name:
            ev["_drop"] = "st"
            return None
        d_day = str(ev["trade_date"])
        crash = index_pct.get(d_day)
        if crash is not None and crash <= _envf("INDEX_CRASH_PCT"):
            ev["_drop"] = "index_crash_isolated_board"
            return None

        start = (pd.Timestamp(d_day) - pd.Timedelta(days=240)).strftime("%Y%m%d")
        bars = self._lup_bars(ts_code, start, as_of)
        if bars is None or bars.empty or len(bars) < 25:
            ev["_drop"] = "no_bars"
            return None
        bars = self._lup_normalize(bars)
        dates = [str(d) for d in bars["_dates"]]
        i_d = dates.index(d_day) if d_day in dates else -1
        i_t = len(bars) - 1
        if i_d < 0:
            ev["_drop"] = "anchor_not_in_bars"
            return None
        if i_t <= i_d:
            ev["_drop"] = "no_pullback_days_yet"
            return None

        ok, reason = self._lup_quality_check(ev, bars, i_d)
        if not ok:
            ev["_drop"] = reason
            return None
        ok, reason = self._lup_observation_veto(bars, i_d, i_t)
        if not ok:
            ev["_drop"] = reason
            return None
        ev["_drop"] = None
        return bars, i_d, i_t

    def _lup_judge_one(
        self, ev: dict, as_of: str,
        index_pct: Dict[str, float],
        sector_strong_codes: Optional[Set[str]],
    ) -> Optional[ScreenedStock]:
        prep = self._lup_prepare(ev, as_of, index_pct)
        if prep is None:
            return None
        bars, i_d, i_t = prep
        ts_code = str(ev.get("ts_code", ""))
        name = str(ev.get("name", "") or "")
        pattern, p_reason = self._lup_detect_pattern(bars, i_d, i_t, ev)
        if pattern is None:
            ev["_drop"] = p_reason
            return None
        if sector_strong_codes:
            bare = ts_code.split(".")[0]
            if bare not in sector_strong_codes:
                ev["_drop"] = "sector_cold"
                return None
        stock = self._lup_build_stock(ev, bars, i_d, i_t, pattern, name)
        if stock is None:
            ev["_drop"] = "levels_or_rr"
        return stock

    @staticmethod
    def _lup_normalize(bars: pd.DataFrame) -> pd.DataFrame:
        out = bars.copy()
        out.columns = [c.lower() for c in out.columns]
        date_col = _col(out, "trade_date", "date") or out.columns[0]
        out["_dates"] = pd.to_datetime(out[date_col]).dt.strftime("%Y%m%d")
        out = out.sort_values("_dates", kind="stable").reset_index(drop=True)
        for c in ("open", "high", "low", "close", "vol"):
            col = _col(out, c, "volume") if c == "vol" else _col(out, c)
            if col and col != c:
                out = out.rename(columns={col: c})
        return out

    @staticmethod
    def _lup_quality_check(
        ev: dict, bars: pd.DataFrame, i_d: int,
    ) -> Tuple[bool, str]:
        """6 hard standards on the limit-up day itself."""
        if i_d < 25:
            return False, "short_history"
        o = bars["open"].astype(float).values
        h = bars["high"].astype(float).values
        lo = bars["low"].astype(float).values
        c = bars["close"].astype(float).values
        v = bars["vol"].astype(float).values
        if h[i_d] == lo[i_d] or o[i_d] >= c[i_d]:
            return False, "one_line_or_t_board"      # 一字/无量板
        ft = _first_time_seconds(ev.get("first_time"))
        if ft is not None and ft > _hhmmss_to_seconds(_envf("FIRST_TIME_MAX")):
            return False, "late_seal"
        on = _ev_num(ev, "open_num", "open_times")
        if on is not None and on > _envf("MAX_OPEN_NUM"):
            return False, "too_many_reopens"
        lt = _ev_num(ev, "limit_times")
        if lt is not None and lt >= _envf("MAX_LIMIT_TIMES"):
            return False, "high_streak_tail"           # 高位连板末尾
        tr = _ev_num(ev, "turnover_ratio")
        if tr is not None and not (_envf("TURNOVER_MIN") <= tr <= _envf("TURNOVER_MAX")):
            return False, "turnover_band"
        avg5 = v[max(0, i_d - 5):i_d].mean()
        if avg5 <= 0 or not (_envf("VOL_EXPAND_MIN") <= v[i_d] / avg5
                             <= _envf("VOL_EXPAND_MAX")):
            return False, "volume_expand"
        prior_low = c[max(0, i_d - 60):i_d].min()
        if prior_low <= 0:
            return False, "bad_history"
        pre_gain = (c[i_d - 1] / prior_low - 1) * 100
        total_gain = (c[i_d] / prior_low - 1) * 100
        if not (_envf("PRE_GAIN_MIN") <= pre_gain <= _envf("PRE_GAIN_MAX")):
            return False, "trend_position"           # 过低/过深
        if total_gain > _envf("MAX_TOTAL_GAIN"):
            return False, "doubled_stock"            # 规避翻倍股
        m5, m10, m20 = (_ma(c, w, i_d) for w in (5, 10, 20))
        m20_prev = _ma(c, 20, i_d - 3)
        if np.isnan(m5) or np.isnan(m10) or np.isnan(m20) or np.isnan(m20_prev):
            return False, "ma_unknown"
        if not (m5 >= m10 * 0.995 and m10 >= m20 * 0.995 and m20 > 0):
            return False, "ma_alignment"
        if m20 <= m20_prev:
            return False, "ma20_not_rising"
        return True, "ok"

    @staticmethod
    def _lup_observation_veto(
        bars: pd.DataFrame, i_d: int, i_t: int,
    ) -> Tuple[bool, str]:
        """Iron rules during D+1..T: no deep break, no high-volume exit."""
        o = bars["open"].astype(float).values
        h = bars["high"].astype(float).values
        lo = bars["low"].astype(float).values
        c = bars["close"].astype(float).values
        v = bars["vol"].astype(float).values
        lu_open, lu_vol = o[i_d], v[i_d]
        if lu_open <= 0 or lu_vol <= 0:
            return False, "bad_anchor"
        for j in range(i_d + 1, i_t + 1):
            if lo[j] < lu_open:
                return False, "broke_limit_up_open"   # 跌破涨停开盘价
            if c[j] < o[j] and v[j] >= _envf("BIG_YIN_VOL_RATIO") * lu_vol:
                return False, "high_volume_pullback"  # 放量阴线回调
        if i_t - i_d > int(_envf("OBS_WINDOW")):
            return False, "window_expired"
        m20 = _ma(c, 20, i_t)
        if np.isnan(m20) or c[i_t] < m20 * 0.99:
            return False, "below_ma20"
        return True, "ok"

    # ------------------------------------------------------------------
    # Four classic pullback patterns
    # ------------------------------------------------------------------

    @classmethod
    def _lup_detect_pattern(
        cls, bars: pd.DataFrame, i_d: int, i_t: int, ev: dict,
    ) -> Tuple[Optional[str], str]:
        o = bars["open"].astype(float).values
        h = bars["high"].astype(float).values
        lo = bars["low"].astype(float).values
        c = bars["close"].astype(float).values
        v = bars["vol"].astype(float).values
        lu_vol, lu_close, lu_open = v[i_d], c[i_d], o[i_d]
        mid = (lu_open + lu_close) / 2
        age = i_t - i_d
        shrink = v[i_d + 1:i_t + 1].mean() / lu_vol if lu_vol > 0 else 9
        limit_pct = _get_limit_up_pct(str(ev.get("ts_code", ""))[:6],
                                      str(ev.get("name", "")))
        today_pct = (c[i_t] / c[i_t - 1] - 1) * 100 if i_t >= 1 else 0
        if today_pct >= limit_pct - 0.3:
            return None, "today_limit_up_unbuyable"
        if c[i_t] > lu_close * (1 + _envf("CHASE_CAP_PCT") / 100):
            return None, "chase_too_far"

        checks = [
            ("三阴不破阳", lambda: cls._lup_p_three_yin(o, c, v, i_d, i_t, lu_vol, lu_open)),
            ("回踩5日线", lambda: cls._lup_p_ma(bars, c, v, o, lo, h, i_d, i_t, lu_vol, 5, mid)),
            ("回踩10日线", lambda: cls._lup_p_ma(bars, c, v, o, lo, h, i_d, i_t, lu_vol, 10, lu_open)),
            ("假阴真阳", lambda: cls._lup_p_fake_yin(o, c, v, i_d, i_t)),
        ]
        for name, fn in checks:
            if not _pattern_enabled(name):
                continue
            if name == "回踩5日线" and 1 <= age <= 3 and fn():
                return name, "ok"
            if name == "回踩10日线" and 2 <= age <= 4 and shrink <= _envf("PULLBACK_VOL_MAX") and fn():
                return name, "ok"
            if name == "三阴不破阳" and age <= 6 and fn():
                return name, "ok"
            if name == "假阴真阳" and age == 1 and fn():
                return name, "ok"

        # 第五形态兜底"缩量横盘"：过了全部红线与观察否决、但几何上不像
        # 四大经典形态的锚点（如横住不碰均线的小阳/十字）。兜底比四形态
        # 更严：缩量 ≤ CONSOL_VOL_MAX、当日不低开低走、站上涨停实体中轴，
        # 否则宁缺毋滥（0.6 同标准放宽实测 PF 2.73→1.16）。
        if (_envf("ALLOW_CONSOLIDATION") >= 1
                and _pattern_enabled("缩量横盘")
                and 2 <= age <= int(_envf("OBS_WINDOW"))
                and shrink <= _envf("CONSOL_VOL_MAX")
                and c[i_t] >= mid
                and c[i_t] >= o[i_t]):
            return "缩量横盘", "ok"
        return None, "no_pattern"

    @staticmethod
    def _lup_p_ma(
        bars: pd.DataFrame, c: np.ndarray, v: np.ndarray, o: np.ndarray,
        lo: np.ndarray, h: np.ndarray, i_d: int, i_t: int, lu_vol: float,
        window: int, floor: float,
    ) -> bool:
        """Pullback onto MA5/MA10 with shrinking volume then stabilising."""
        if lu_vol <= 0 or v[i_d + 1:i_t + 1].mean() / lu_vol > _envf("PULLBACK_VOL_MAX"):
            return False
        closes = c
        touched = any(
            lo[j] <= _ma(closes, window, j) * 1.01
            for j in range(i_d + 1, i_t + 1) if not np.isnan(_ma(closes, window, j))
        )
        ma_now = _ma(closes, window, i_t)
        if np.isnan(ma_now) or not touched or c[i_t] < ma_now * 0.99:
            return False
        rng = max(h[i_t] - lo[i_t], 1e-9)
        shadow = min(c[i_t], o[i_t]) - lo[i_t]
        stable = abs(c[i_t] - o[i_t]) <= 0.35 * rng or shadow >= 0.4 * rng
        return bool(stable and c[i_t] > floor)

    @staticmethod
    def _lup_p_three_yin(
        o: np.ndarray, c: np.ndarray, v: np.ndarray, i_d: int, i_t: int,
        lu_vol: float, lu_open: float,
    ) -> bool:
        """Three shrinking small bearish candles fully inside the limit-up body."""
        if i_t - 2 <= i_d:
            return False
        idx = (i_t - 2, i_t - 1, i_t)
        for j in idx:
            if c[j] >= o[j] or c[j] < lu_open:
                return False
            if abs(c[j] - o[j]) / o[j] * 100 > 4.0:
                return False
        vols = [v[j] for j in idx]
        if not (vols[0] > vols[1] > vols[2]):
            return False
        return vols[2] <= 0.5 * lu_vol

    @staticmethod
    def _lup_p_fake_yin(
        o: np.ndarray, c: np.ndarray, v: np.ndarray, i_d: int, i_t: int,
    ) -> bool:
        """Gap-up, close below open, but still above yesterday's close."""
        if i_t != i_d + 1:
            return False
        return bool(o[i_t] > c[i_d] and c[i_t] < o[i_t]
                    and c[i_t] > c[i_d] and v[i_t] <= v[i_d])

    # ------------------------------------------------------------------
    # Output assembly
    # ------------------------------------------------------------------

    def _lup_build_stock(
        self, ev: dict, bars: pd.DataFrame, i_d: int, i_t: int,
        pattern: str, name: str,
    ) -> Optional[ScreenedStock]:
        from src.services.trade_levels import (
            RR_MIN, compute_limit_up_pullback_levels,
        )
        o = bars["open"].astype(float).values
        lo = bars["low"].astype(float).values
        c = bars["close"].astype(float).values
        price = float(c[i_t])
        mv_yi = float(pd.to_numeric(
            pd.Series([ev.get("float_mv")]), errors="coerce").iloc[0] or 0) / 1e4  # 万元->亿
        tl = compute_limit_up_pullback_levels(
            code=str(ev.get("ts_code", "")), current_price=price,
            ma5=_ma(c, 5, i_t), ma10=_ma(c, 10, i_t), ma20=_ma(c, 20, i_t),
            lu_open=o[i_d], lu_low=lo[i_d], lu_close=c[i_d],
            market_cap_yi=mv_yi if mv_yi > 0 else 0.0,
        )
        if tl is None or tl.ideal_buy <= 0 or tl.risk_reward < RR_MIN:
            return None

        score = _PATTERN_STRENGTH.get(pattern, 70.0)
        # 教科书最优区间作加分（硬门槛见 _lup_quality_check）
        ft = _first_time_seconds(ev.get("first_time"))
        if ft is not None and ft <= _hhmmss_to_seconds(93500):
            score += 8                                     # 9:35 前锁板
        elif ft is not None and ft <= _hhmmss_to_seconds(103000):
            score += 4                                     # 10:30 前排板
        on = _ev_num(ev, "open_num", "open_times")
        if on is not None and on == 0:
            score += 6                                     # 封板坚决（零炸板）
        tr = _ev_num(ev, "turnover_ratio")
        if tr is not None and 10.0 <= tr <= 20.0:
            score += 6                                     # 教科书换手甜蜜区
        v = bars["vol"].astype(float).values
        avg5 = v[max(0, i_d - 5):i_d].mean()
        if avg5 > 0 and 1.5 <= v[i_d] / avg5 <= 3.0:
            score += 4                                     # 温和放量甜蜜区
        prior_low = c[max(0, i_d - 60):i_d].min()
        if prior_low > 0 and 30.0 <= (c[i_d - 1] / prior_low - 1) * 100 <= 70.0:
            score += 5                                     # 主升浪中段启动
        score += 5 if (c[i_t] + c[i_t - 1]) / 2 >= (o[i_d] + c[i_d]) / 2 else 0
        code6 = str(ev.get("ts_code", ""))[:6]
        return ScreenedStock(
            code=code6,
            name=name,
            price=price,
            change_pct=float(pd.to_numeric(
                pd.Series([ev.get("pct_chg")]), errors="coerce").iloc[0] or 0),
            turnover_rate=float(pd.to_numeric(
                pd.Series([ev.get("turnover_ratio")]), errors="coerce").iloc[0] or 0),
            market_cap=mv_yi,
            score=round(score, 1),
            strategies=["buy_pullback"],
            ideal_buy=tl.ideal_buy,
            secondary_buy=tl.secondary_buy,
            stop_loss=tl.stop_loss,
            take_profit_1=tl.take_profit_1,
            take_profit_2_rule=tl.take_profit_2_rule,
            position_pct=tl.position_pct,
            risk_reward=tl.risk_reward,
            limit_up_date=str(ev.get("trade_date", "")),
            setup=pattern,
        )
