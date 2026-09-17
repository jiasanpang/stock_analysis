# -*- coding: utf-8 -*-
"""
The orchestrator: ``screen()`` runs the full multi-layer pipeline, while
``screen_as_of()`` is a thin date-clamped wrapper used by the backtest harness.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from src.config import get_config
from src.services.picker.constants import (
    PICKER_MAX_BIAS_PCT,
    PICKER_TOP_N_PER_STRATEGY,
    PICKER_MODE_PARAMS,
    ScreenedStock,
    ScreenStats,
)

logger = logging.getLogger(__name__)


class _PipelineMixin:
    """Mixin: ``screen()`` and ``screen_as_of()``."""

    def screen(
        self, trade_date: Optional[str] = None
    ) -> Tuple[List[ScreenedStock], ScreenStats, Dict[str, List[ScreenedStock]]]:
        """Run the full screening pipeline. Returns (candidates, stats, candidates_per_strategy).
        When trade_date is provided (YYYYMMDD), run historical screening (Tushare only).
        Uses multi-strategy when picker_strategies has multiple entries."""
        stats = ScreenStats()
        self._gated_strategies = []
        from .limit_up_pullback import limit_up_pullback_enabled
        _lup_on = limit_up_pullback_enabled()
        self._as_of_date = self._trade_date_to_iso(trade_date) if trade_date else None
        # Push as_of_date to the caching manager so its LocalStockDB-backed
        # window resolution doesn't peek into the future during backtests.
        if self._data_manager is not None:
            try:
                setattr(self._data_manager, "_as_of_date", self._as_of_date)
            except Exception:
                pass

        # Preserve original strategies -- restored in finally block
        original_strategies = list(self._picker_strategies)

        try:
            # -- Market environment guard --
            cfg = get_config()
            # Always evaluate market_env (used downstream for regime-aware position
            # scaling even when picker_market_guard is disabled).
            market_env = self._check_market_environment()
            # EOD_VARIANT bypasses market guard (variants have own regime logic)
            import os as _os
            _bypass_guard = bool(_os.environ.get("EOD_VARIANT", "").strip())
            # buy_pullback regime gate: requires SSE > MA20 by an explicit
            # threshold to avoid the 2025-11 -389% drawdown.
            # +2% was calibrated for the legacy cross-sectional chain; the LUP
            # engine self-limits (structural stop + RR>=2 + 缩量确认), and the
            # +2% gate cut 47/65 backtest days for no PF benefit. Under LUP the
            # default drops to -2% (stop only when SSE is deeply below MA20).
            # Toggle via BUY_PULLBACK_REQUIRE_STRONG (default on).
            # Threshold via BUY_PULLBACK_GATE_PCT (default: -2.0 LUP / 2.0 legacy).
            _default_gate = "-2.0" if _lup_on else "2.0"
            try:
                _gate_pct = float(_os.environ.get("BUY_PULLBACK_GATE_PCT", _default_gate))
            except ValueError:
                _gate_pct = float(_default_gate)
            if (
                _os.environ.get("BUY_PULLBACK_REQUIRE_STRONG", "1") == "1"
                and "buy_pullback" in self._picker_strategies
                and market_env is not None
                and market_env.diff_pct < _gate_pct
                and not _bypass_guard
            ):
                logger.warning(
                    "[MarketGuard/buy_pullback] SSE diff %+.2f%% < +%.1f%% required, "
                    "removing buy_pullback for this day",
                    market_env.diff_pct, _gate_pct,
                )
                self._gated_strategies.append((
                    "buy_pullback",
                    f"大盘强度不足（上证仅高于MA20 {market_env.diff_pct:+.2f}% < 要求 +{_gate_pct:.1f}%），"
                    f"买回踩策略已自动暂停以避免弱势反弹失败",
                ))
                self._picker_strategies = [s for s in self._picker_strategies if s != "buy_pullback"]
                if not self._picker_strategies:
                    return [], stats, {}

            # bottom_reversal regime gate: same lesson as buy_pullback —
            # "second buy point" technical patterns only convert into
            # MarketGuard for reversal_breakout: right-side breakouts in a
            # falling tape become "fake breakout then crash". Gate only
            # opens when SSE is at least +1% above its MA20. Default +1%
            # (looser than buy_pullback's +2% because reversal candidates
            # are catch-up rebounds, not momentum trades). Toggle via
            # REVERSAL_BREAKOUT_REQUIRE_STRONG / REVERSAL_BREAKOUT_GATE_PCT.
            # bottom_reversal (v2) is a manual-analysis watchlist and is
            # not gated by this guard.
            try:
                _rb_gate = float(_os.environ.get("REVERSAL_BREAKOUT_GATE_PCT", "1.0"))
            except ValueError:
                _rb_gate = 1.0
            if (
                _os.environ.get("REVERSAL_BREAKOUT_REQUIRE_STRONG", "1") == "1"
                and "reversal_breakout" in self._picker_strategies
                and market_env is not None
                and market_env.diff_pct < _rb_gate
                and not _bypass_guard
            ):
                logger.warning(
                    "[MarketGuard/reversal_breakout] SSE diff %+.2f%% < %+.2f%% required, "
                    "removing reversal_breakout for this day",
                    market_env.diff_pct, _rb_gate,
                )
                self._gated_strategies.append((
                    "reversal_breakout",
                    f"大盘强度不足（上证仅高于MA20 {market_env.diff_pct:+.2f}% < 要求 +{_rb_gate:.2f}%），"
                    f"反转突破策略已自动暂停以规避假突破风险",
                ))
                self._picker_strategies = [s for s in self._picker_strategies if s != "reversal_breakout"]
                if not self._picker_strategies:
                    return [], stats, {}

            if getattr(cfg, "picker_market_guard", True) and not _bypass_guard:
                if market_env and not market_env.is_strong:
                    raw_action = getattr(cfg, "picker_weak_market_action", "limit")
                    action = (raw_action or "limit").strip().lower()
                    if action not in ("skip", "limit"):
                        logger.warning(
                            "[MarketGuard] Invalid picker_weak_market_action=%r, fallback to 'limit'",
                            raw_action,
                        )
                        action = "limit"
                    logger.warning(
                        "[MarketGuard] Weak market (regime=%s), action=%s",
                        market_env.regime, action,
                    )
                    if action == "skip":
                        logger.warning(
                            "[MarketGuard] Weak market detected, skipping all strategies"
                        )
                        return [], stats, {}
                    elif action == "limit":
                        allowed_str = getattr(cfg, "picker_weak_market_strategies", "bottom_reversal")
                        allowed = [s.strip() for s in allowed_str.split(",") if s.strip()]
                        original = list(self._picker_strategies)
                        self._picker_strategies = [s for s in self._picker_strategies if s in allowed]
                        if not self._picker_strategies:
                            logger.warning(
                                "[MarketGuard] Weak market, no allowed strategies remain "
                                "(original: %s, allowed: %s)", original, allowed,
                            )
                            return [], stats, {}
                        logger.warning(
                            "[MarketGuard] Weak market, limiting to strategies: %s",
                            self._picker_strategies,
                        )

            # Determine which strategies need daily data vs realtime-only path
            daily_strategies = [s for s in self._picker_strategies if s in self.DAILY_DATA_STRATEGIES]
            realtime_only_strategies = [s for s in self._picker_strategies if s not in self.DAILY_DATA_STRATEGIES]
            needs_daily = len(daily_strategies) > 0

            if daily_strategies:
                logger.info(f"[Screener] Daily-data strategies: {daily_strategies}")
            if realtime_only_strategies:
                logger.info(f"[Screener] Realtime-only strategies (skip daily fetch): {realtime_only_strategies}")

            from src.services.picker_strategies import (
                get_strategy_params,
                filter_momentum,
                filter_volume,
                score_and_rank,
                merge_candidates_by_code,
            )

            candidates_per_strategy: Dict[str, List[ScreenedStock]] = {}

            # df and _sector_strong_codes are referenced by post-pipeline
            # strategy dispatch blocks (bottom_reversal) outside the
            # if-needs-daily branch — keep them bound to safe defaults
            # when no daily fetch happens.
            df = None
            _sector_strong_codes: Set[str] = set()
            # --- Daily-data pipeline: only fetch spot data when at least one strategy needs it ---
            if needs_daily:
                df = self._fetch_spot_data(trade_date)
                if df is None or df.empty:
                    logger.warning("[Screener] No spot data available for daily strategies")
                    # Daily strategies cannot proceed, but realtime strategies may still run below
                    df = None
                else:
                    stats.total_stocks = len(df)
                    logger.info(
                        f"[Screener] Starting daily pipeline with {stats.total_stocks} stocks, "
                        f"strategies={daily_strategies}"
                    )

                    # Layer 1: Basic quality filter (shared, pe_max=100)
                    df = self._filter_basic_for_strategies(df)
                    stats.after_basic = len(df)
                    logger.info(f"[Screener] After basic filter: {len(df)}")

                    # Layer 1.2: Fundamental hard-veto (P1-A)
                    df = self._filter_hard_veto(df, stats)
                    if stats.after_veto == 0:
                        stats.after_veto = stats.after_basic

                    # Layer 1.5: Prepare sector strength data
                    _sector_strong_codes: Set[str] = set()
                    _need_sector = any(s in self.SECTOR_FILTER_STRATEGIES for s in daily_strategies)
                    if getattr(cfg, "picker_sector_filter", True) and _need_sector:
                        try:
                            from src.services.sector_strength_service import SectorStrengthService
                            sector_svc = SectorStrengthService()
                            sector_top_pct = getattr(cfg, "picker_sector_top_pct", 30) / 100.0
                            from concurrent.futures import ThreadPoolExecutor as _TPE
                            with _TPE(max_workers=1) as _executor:
                                _future = _executor.submit(
                                    sector_svc.get_strong_sector_codes,
                                    top_pct=sector_top_pct,
                                    trade_date=trade_date,
                                )
                                try:
                                    _sector_strong_codes = _future.result(timeout=180)
                                except Exception as _te:
                                    logger.warning(
                                        "[Screener] Sector codes fetch timed out or failed (%s), "
                                        "skipping sector filter", _te,
                                    )
                                    _sector_strong_codes = set()
                            if _sector_strong_codes:
                                logger.info(
                                    "[Screener] Sector data ready: %d codes from top %.0f%% sectors",
                                    len(_sector_strong_codes), sector_top_pct * 100,
                                )
                            else:
                                logger.warning("[Screener] Sector filter: no sector data available")
                        except Exception as e:
                            logger.warning("[Screener] Sector filter error: %s", e)
                    elif not _need_sector:
                        logger.info(
                            "[Screener] No strategy requires sector filter, skipping sector data fetch"
                        )

                    # Run each daily-data strategy
                    for strategy_id in daily_strategies:
                        # small_cap and bottom_reversal both live in
                        # DAILY_DATA_STRATEGIES so we fetch spot once
                        # across strategies, but they do NOT go through
                        # the params-driven momentum / volume / score
                        # pipeline — handled by dedicated dispatch
                        # blocks below.
                        if strategy_id in ("small_cap", "bottom_reversal", "reversal_breakout"):
                            continue
                        if strategy_id == "buy_pullback" and _lup_on:
                            # 涨停回踩引擎（专用 dispatch，见下方）接管 buy_pullback
                            continue
                        params = get_strategy_params(strategy_id)

                        # Apply sector filter for applicable strategies
                        df_s = df.copy()
                        if _sector_strong_codes and strategy_id in self.SECTOR_FILTER_STRATEGIES:
                            code_col = None
                            for col in ['code', '代码', 'ts_code']:
                                if col in df_s.columns:
                                    code_col = col
                                    break
                            if code_col:
                                before_sector = len(df_s)
                                df_s_codes = df_s[code_col].astype(str).str[:6]
                                df_s = df_s[df_s_codes.isin(_sector_strong_codes)]
                                logger.info(
                                    "[Screener] %s: sector filter %d -> %d",
                                    strategy_id, before_sector, len(df_s),
                                )

                        df_s = filter_momentum(df_s, params)
                        stats.after_momentum = len(df_s)
                        df_s = filter_volume(df_s, params)
                        stats.after_volume = len(df_s)

                        logger.debug(
                            f"[Screener] {strategy_id}: after filter_momentum={stats.after_momentum}, "
                            f"after filter_volume={stats.after_volume}"
                        )

                        cands = score_and_rank(df_s, strategy_id, params, top_n=PICKER_TOP_N_PER_STRATEGY)
                        cands = self._filter_by_bias(
                            cands,
                            max_bias_pct=params.max_bias_pct,
                            leader_bias_exempt_pct=getattr(params, "leader_bias_exempt_pct", 0.0),
                        )
                        cands = self._filter_limit_up_streak(cands)
                        cands = self._filter_consecutive_up_days(cands, max_up_days=params.max_consecutive_up_days)
                        cands = self._filter_healthy_pullback(cands, params=params, strategy_id=strategy_id)
                        if self._enable_b_wave_filter:
                            cands = self._filter_b_wave_risk(cands)

                        # buy_pullback moneyflow filter (smart-money/dumb-money divergence)
                        # 主散背离: 大单+超大单 净流入 > 0 AND 中小单 净流入 < 0
                        # Toggle via BUY_PULLBACK_MONEYFLOW_FILTER (default on).
                        if (
                            strategy_id == "buy_pullback"
                            and cands
                            and _os.environ.get("BUY_PULLBACK_MONEYFLOW_FILTER", "1") == "1"
                        ):
                            cands = self._filter_buy_pullback_moneyflow(cands, trade_date=trade_date)

                        if cands:
                            candidates_per_strategy[strategy_id] = cands
                            logger.info(f"[Screener] {strategy_id}: {len(cands)} candidates")
                            if logger.isEnabledFor(10):  # DEBUG level
                                top5 = cands[:5]
                                top5_str = ", ".join(f"{c.code}({c.score:.1f})" for c in top5)
                                logger.debug(f"[Screener] {strategy_id} top-5: {top5_str}")
            else:
                logger.info("[Screener] No daily-data strategies selected; skipping spot fetch")

            # --- small_cap: cross-sectional market-cap rank ---
            # Live mode uses the realtime spot DataFrame fetched above
            # (today's full-market quote). Historical / backtest mode reads
            # from LocalStockDB by-date shards.
            if "small_cap" in self._picker_strategies:
                from .small_cap import small_cap_min_amount_yuan, small_cap_top_n
                td_yyyymmdd = trade_date if trade_date else (
                    self._as_of_date.replace("-", "") if self._as_of_date else None
                )
                sc_cands = self._screen_small_cap(
                    trade_date_yyyymmdd=td_yyyymmdd,
                    top_n=small_cap_top_n(),
                    min_amount_yuan=small_cap_min_amount_yuan(),
                )
                if sc_cands:
                    candidates_per_strategy["small_cap"] = sc_cands
                    logger.info(f"[Screener] small_cap: {len(sc_cands)} candidates")

            # --- bottom_reversal (v2): left-side "still consolidating,
            # about to launch" screener. Watchlist-grade output for
            # manual analysis — looser geometric filters, no sector
            # gate, no smart-money confirmation. See bottom_reversal_v2.py.
            if "bottom_reversal" in self._picker_strategies:
                td_yyyymmdd = trade_date if trade_date else (
                    self._as_of_date.replace("-", "") if self._as_of_date else None
                )
                br_cands = self._screen_bottom_reversal_v2(
                    spot_df=df,
                    trade_date_yyyymmdd=td_yyyymmdd,
                )
                if br_cands:
                    candidates_per_strategy["bottom_reversal"] = br_cands
                    logger.info(f"[Screener] bottom_reversal: {len(br_cands)} candidates")

            # --- reversal_breakout (v3): right-side confirmation. Buys
            # only AFTER a deep-bottom stock breaks out of its base with
            # volume + same-day main-force net inflow. Actionable swing
            # entry. See reversal_breakout.py.
            if "reversal_breakout" in self._picker_strategies:
                td_yyyymmdd = trade_date if trade_date else (
                    self._as_of_date.replace("-", "") if self._as_of_date else None
                )
                _rb_sector_codes = _sector_strong_codes if needs_daily else set()
                rb_cands = self._screen_reversal_breakout(
                    spot_df=df,
                    trade_date_yyyymmdd=td_yyyymmdd,
                    sector_strong_codes=_rb_sector_codes,
                )
                if rb_cands:
                    candidates_per_strategy["reversal_breakout"] = rb_cands
                    logger.info(f"[Screener] reversal_breakout: {len(rb_cands)} candidates")

            # --- buy_pullback (涨停回踩引擎): event-driven. Anchor universe
            # is the recent limit-up pool (tushare limit_list_d, daily-bar
            # derived fallback); five pullback patterns confirm the entry.
            # Toggle back to the legacy trend-pullback chain via LUP_ENABLED=0.
            if "buy_pullback" in self._picker_strategies and _lup_on:
                td_yyyymmdd = trade_date if trade_date else (
                    self._as_of_date.replace("-", "") if self._as_of_date else None
                )
                lup_cands = self._screen_limit_up_pullback(
                    spot_df=df,
                    trade_date_yyyymmdd=td_yyyymmdd,
                    sector_strong_codes=_sector_strong_codes,
                )
                if lup_cands:
                    candidates_per_strategy["buy_pullback"] = lup_cands
                    logger.info(f"[Screener] buy_pullback(涨停回踩): {len(lup_cands)} candidates")

            if not candidates_per_strategy:
                stats.final_pool = 0
                logger.warning("[Screener] No candidates from any strategy")
                return [], stats, {}

            candidates = merge_candidates_by_code(candidates_per_strategy)

            # Auto-reweight underperforming strategies (P1-B). Off by default.
            if getattr(cfg, "strategy_auto_reweight", False):
                try:
                    from src.services.strategy_attribution_service import (
                        get_strategy_weights,
                    )
                    from src.storage import get_db
                    weights = get_strategy_weights(
                        db=get_db(),
                        data_manager=self._data_manager,
                    )
                    if weights:
                        for s in candidates:
                            sids = s.strategies or []
                            if sids:
                                w = min((weights.get(sid, 1.0) for sid in sids), default=1.0)
                                if w < 1.0:
                                    s.score *= w
                        candidates.sort(key=lambda s: s.score, reverse=True)
                        bad = [sid for sid, w in weights.items() if w < 1.0]
                        if bad:
                            logger.info(
                                "[Screener] Auto-reweight applied. Penalised strategies: %s",
                                bad,
                            )
                except Exception as exc:
                    logger.warning("[Screener] Auto-reweight failed (skipped): %s", exc)

            # -- Industry concentration cap (E2) --
            INDUSTRY_TOP_N = int(getattr(cfg, "picker_industry_top_n", 2) or 2)
            if candidates and INDUSTRY_TOP_N > 0:
                tagged = [s for s in candidates if getattr(s, "industry", "")]
                if tagged:
                    candidates.sort(key=lambda s: s.score, reverse=True)
                    seen: Dict[str, int] = {}
                    kept: List[ScreenedStock] = []
                    dropped = 0
                    for s in candidates:
                        ind = getattr(s, "industry", "") or ""
                        if not ind:
                            kept.append(s)
                            continue
                        if seen.get(ind, 0) >= INDUSTRY_TOP_N:
                            dropped += 1
                            continue
                        seen[ind] = seen.get(ind, 0) + 1
                        kept.append(s)
                    if dropped > 0:
                        logger.info(
                            "[Screener] Industry concentration cap (top %d/industry): "
                            "dropped %d, kept %d",
                            INDUSTRY_TOP_N, dropped, len(kept),
                        )
                    candidates = kept

            # -- Regime-aware position scaling (E1) --
            if market_env is not None and candidates:
                regime_scale = {"weak": 0.6, "neutral": 0.85, "strong": 1.0}.get(
                    market_env.regime, 1.0
                )
                if regime_scale < 1.0:
                    for s in candidates:
                        if getattr(s, "position_pct", 0) > 0:
                            s.position_pct = round(s.position_pct * regime_scale, 3)
                    logger.info(
                        "[Screener] Regime=%s, position scaled x%.2f",
                        market_env.regime, regime_scale,
                    )

            stats.final_pool = len(candidates)
            logger.info(
                f"[Screener] Merged {stats.final_pool} candidates from "
                f"{len(candidates_per_strategy)} strategies"
            )
            return candidates, stats, candidates_per_strategy
        finally:
            # Always restore original strategies after this call
            self._picker_strategies = original_strategies

    def screen_as_of(
        self, trade_date: str
    ) -> Tuple[List[ScreenedStock], ScreenStats, Dict[str, List[ScreenedStock]]]:
        """Run screening as of a specific trade date (YYYYMMDD). For backtest use."""
        return self.screen(trade_date=trade_date)

    def _filter_buy_pullback_moneyflow(
        self,
        cands: List[ScreenedStock],
        trade_date: Optional[str],
    ) -> List[ScreenedStock]:
        """Smart-money / dumb-money divergence filter.

        Keep only candidates where on the candidate-selection day:
          - main force (大单+超大单) net inflow > 0
          - retail (中小单) net inflow < 0

        Hypothesis: institutions accumulate on dips while retail panics out.
        Same lever that materially improved eod_buyback (Iter-1 → +0.13 PF).

        Drops candidates with no flow data (conservative — avoid false positives).
        """
        td = trade_date if trade_date else (
            self._as_of_date.replace("-", "") if self._as_of_date else None
        )
        if not td:
            return cands

        try:
            from data_provider.moneyflow_fetcher import MoneyflowFetcher

            api = self._get_tushare_api() if hasattr(self, "_get_tushare_api") else None
            if api is None:
                from src.services.picker import get_tushare_api as _get_api

                api = _get_api(self._data_manager) if self._data_manager else None
            if api is None:
                logger.warning("[buy_pullback/moneyflow] no Tushare api; skipping filter")
                return cands
            mf = MoneyflowFetcher(api)
            df_mf = mf.get_market_moneyflow(td)
        except Exception as e:
            logger.warning(
                "[buy_pullback/moneyflow] fetch failed (%s); skipping flow filter", e
            )
            return cands

        if df_mf is None or df_mf.empty:
            logger.warning(
                "[buy_pullback/moneyflow] no flow data for %s; skipping filter", td
            )
            return cands

        df_mf["code"] = df_mf["ts_code"].str.split(".").str[0]
        for col in (
            "buy_lg_amount", "buy_elg_amount", "sell_lg_amount", "sell_elg_amount",
            "buy_sm_amount", "buy_md_amount", "sell_sm_amount", "sell_md_amount",
        ):
            if col in df_mf.columns:
                df_mf[col] = pd.to_numeric(df_mf[col], errors="coerce").fillna(0)

        df_mf["main_net"] = (
            df_mf.get("buy_lg_amount", 0) + df_mf.get("buy_elg_amount", 0)
            - df_mf.get("sell_lg_amount", 0) - df_mf.get("sell_elg_amount", 0)
        )
        df_mf["retail_net"] = (
            df_mf.get("buy_sm_amount", 0) + df_mf.get("buy_md_amount", 0)
            - df_mf.get("sell_sm_amount", 0) - df_mf.get("sell_md_amount", 0)
        )
        flow_lookup = df_mf.set_index("code")[["main_net", "retail_net"]].to_dict("index")

        before = len(cands)
        kept: List[ScreenedStock] = []
        for c in cands:
            f = flow_lookup.get(str(c.code))
            if f is None:
                continue
            if f["main_net"] > 0 and f["retail_net"] < 0:
                kept.append(c)
        logger.info(
            "[buy_pullback/moneyflow] %s: filter (main>0 & retail<0): %d → %d",
            td, before, len(kept),
        )
        return kept
