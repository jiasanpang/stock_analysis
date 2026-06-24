# -*- coding: utf-8 -*-
"""
StockPickerService - Main coordinator for the two-stage stock picking pipeline.

Stage 1: Quantitative screening (StockScreener)
Stage 1.5: Real-time filtering
Stage 2: AI selection (AISelector)
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

from src.config import get_config
from src.search_service import SearchService
from data_provider.base import DataFetcherManager
from src.services.picker.constants import (
    PickerResult,
    ScreenedStock,
    StockPick,
)
from src.services.picker.screener import StockScreener
from src.services.picker.realtime_filter import filter_by_realtime
from src.services.picker.ai_selector import AISelector

logger = logging.getLogger(__name__)


class StockPickerService:
    """Two-stage stock picker: quantitative screening + AI selection."""

    def __init__(
        self,
        picker_strategies_override: Optional[List[str]] = None,
        picker_mode_override: Optional[str] = None,
    ):
        self.config = get_config()
        self._data_manager = DataFetcherManager()
        strategies = (
            picker_strategies_override
            if picker_strategies_override is not None
            else (getattr(self.config, "picker_strategies", None) or ["buy_pullback"])
        )
        mode = picker_mode_override or self.config.picker_mode
        self._screener = StockScreener(
            data_manager=self._data_manager,
            picker_strategies=strategies,
            picker_mode=mode,
            enable_b_wave_filter=getattr(self.config, "picker_enable_b_wave_filter", True),
        )
        self._search_service: Optional[SearchService] = None
        self._analyzer = None
        self._init_services()

    def _init_services(self):
        """Initialize search and LLM services."""
        self._search_service = SearchService(
            bocha_keys=self.config.bocha_api_keys,
            tavily_keys=self.config.tavily_api_keys,
            brave_keys=self.config.brave_api_keys,
            serpapi_keys=self.config.serpapi_keys,
            minimax_keys=self.config.minimax_api_keys,
            searxng_base_urls=self.config.searxng_base_urls,
            zhipu_keys=self.config.zhipu_api_keys,
            news_max_age_days=1,
        )
        from src.analyzer import GeminiAnalyzer
        self._analyzer = GeminiAnalyzer(self.config)

    def run(self) -> PickerResult:
        """Execute the full two-stage stock picking pipeline."""
        start = time.time()
        result = PickerResult(
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            picker_mode=self._screener._picker_mode,
            picker_strategies=getattr(self._screener, "_picker_strategies", []) or ["buy_pullback"],
        )

        try:
            # -- Stage 1: Quantitative screening --
            logger.info("[StockPicker] === Stage 1: Quantitative Screening ===")
            candidates, stats, candidates_per_strategy = self._screener.screen()
            result.screen_stats = stats
            result.screened_pool = candidates
            result.screened_pool_by_strategy = candidates_per_strategy

            if not candidates:
                logger.warning("[StockPicker] Screening returned 0 candidates")

            # -- Stage 1.5: Real-time filtering --
            if getattr(self.config, "picker_enable_realtime_filter", True):
                logger.info("[StockPicker] === Stage 1.5: Real-time Filtering ===")
                pre_count = len(candidates)
                candidates = filter_by_realtime(candidates, self._data_manager, self.config)
                logger.info(f"[StockPicker] Real-time filtering: {pre_count} -> {len(candidates)} candidates")
                result.screened_pool = candidates

            # -- Early exit: empty screened pool -> skip LLM --
            if not candidates:
                strategies = getattr(self._screener, "_picker_strategies", []) or []
                logger.warning(
                    "[StockPicker] Screened pool is empty after filtering, "
                    f"strategies={strategies}. Skipping LLM call and returning empty picks."
                )
                result.picks = []
                # Prefer a specific reason when MarketGuard removed strategies,
                # so the UI doesn't mislead users with the generic "no stocks
                # matched filters" message (e.g. when nothing was actually
                # screened because the strategy was gated off).
                gated = getattr(self._screener, "_gated_strategies", []) or []
                if gated:
                    reasons = "；".join(reason for _, reason in gated)
                    result.market_summary = f"{reasons}。今日不进行 AI 选股。"
                else:
                    result.market_summary = "今日无符合量化筛选严格条件的股票，不进行 AI 选股。"
                result.success = True
                result.elapsed_seconds = time.time() - start
                return result

            # -- Stage 2: Gather market intel + AI selection --
            logger.info("[StockPicker] === Stage 2: AI Selection ===")
            ai_selector = AISelector(
                config=self.config,
                data_manager=self._data_manager,
                search_service=self._search_service,
                analyzer=self._analyzer,
                screener=self._screener,
            )
            intel = ai_selector.gather_market_intel()
            chip_map = ai_selector.fetch_chip_for_candidates(candidates)
            prompt = ai_selector.build_prompt(intel, candidates, chip_map)

            try:
                llm_output = ai_selector.call_llm(prompt)
            except Exception as llm_exc:
                logger.warning(
                    "[StockPicker] LLM call raised exception, "
                    "degrading to quantitative results: %s", llm_exc,
                )
                llm_output = None

            if not llm_output:
                # Graceful degradation: return Stage-1 quantitative candidates
                logger.warning(
                    "[StockPicker] LLM unavailable, returning quantitative "
                    "screening results without AI analysis"
                )
                result.market_summary = (
                    "AI 分析暂不可用（LLM 服务异常），以下为量化筛选结果，仅供参考。"
                )
                result.picks = [
                    StockPick(
                        code=s.code,
                        name=s.name,
                        sector=getattr(s, "sector", ""),
                        reason=f"量化评分 {s.score:.1f}（换手率 {s.turnover_rate:.1f}%，"
                               f"涨跌 {s.change_pct:+.1f}%）",
                        catalyst="",
                        attention="medium",
                        risk_note="仅量化筛选，未经 AI 深度分析",
                        ideal_buy=s.ideal_buy,
                        stop_loss=s.stop_loss,
                        take_profit_1=s.take_profit_1,
                        take_profit_2_rule=s.take_profit_2_rule,
                        position_pct=s.position_pct,
                        risk_reward=s.risk_reward,
                        strategies=list(s.strategies or []),
                        resonance=s.resonance,
                    )
                    for s in candidates[:10]
                ]
                result.sectors_to_watch = []
                result.risk_warning = "LLM 服务异常，仅返回量化筛选结果，请谨慎参考。"
                result.generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                result.success = True
                result.elapsed_seconds = time.time() - start
                return result

            ai_selector.parse_result(llm_output, result)

            # Append stale-data annotation
            if intel.get("indices_stale"):
                result.indices_stale = True
            if intel.get("indices_stale") and result.market_summary:
                result.market_summary = (
                    result.market_summary.rstrip()
                    + "（注：指数实时数据暂不可用，以上为定性判断）"
                )

            # -- Post-validation: ensure LLM picks are within screened pool --
            if result.screened_pool:
                pool_codes = {s.code for s in result.screened_pool}
                validated_picks = []
                for pick in result.picks:
                    if pick.code in pool_codes:
                        validated_picks.append(pick)
                    else:
                        logger.warning(
                            f"[StockPicker] LLM pick {pick.code} not in screened pool, removed"
                        )
                if len(validated_picks) != len(result.picks):
                    logger.info(
                        f"[StockPicker] Post-validation: {len(result.picks)} -> {len(validated_picks)} picks"
                    )
                result.picks = validated_picks

            result.success = True

        except Exception as e:
            logger.error(f"[StockPicker] Error: {e}", exc_info=True)
            result.error = str(e)

        result.elapsed_seconds = time.time() - start
        return result
