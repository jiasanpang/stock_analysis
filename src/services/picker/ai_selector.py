# -*- coding: utf-8 -*-
"""AI selection logic (Stage 2) for the stock picker pipeline.

Handles market intelligence gathering, LLM prompt building, calling, and result parsing.
"""

import json
import logging
import time
from concurrent.futures import as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from json_repair import repair_json

from src._concurrency import FuturesTimeout, get_executor, run_with_timeout
from src.services.picker.constants import (
    PICK_SYSTEM_PROMPT,
    PickerModeParams,
    ScreenedStock,
    PickerResult,
    StockPick,
)

logger = logging.getLogger(__name__)


class AISelector:
    """Handles Stage 2: market intelligence + LLM-based stock selection."""

    SEARCH_QUERIES = [
        "今日A股市场热点 涨停分析",
        "A股主力资金流入 板块异动",
        "A股利好消息 政策催化",
    ]

    _INTEL_ITEM_TIMEOUT = 20
    _INTEL_TOTAL_TIMEOUT = 45

    def __init__(self, config, data_manager, search_service, analyzer, screener):
        self.config = config
        self._data_manager = data_manager
        self._search_service = search_service
        self._analyzer = analyzer
        self._screener = screener

    def gather_market_intel(self) -> Dict[str, Any]:
        """Gather macro market data from multiple sources with per-call timeouts."""
        intel: Dict[str, Any] = {}
        gather_start = time.time()

        def _safe_call(label, fn):
            """Wrapper that catches exceptions and returns (label, result/None)."""
            try:
                result = fn()
                return (label, result)
            except Exception as e:
                logger.warning(f"[StockPicker] {label} failed: {e}")
                return (label, None)

        pool = get_executor(3, "intel")
        fut_indices = fut_stats = fut_sectors = None
        try:
            fut_indices = pool.submit(
                _safe_call, "indices", lambda: self._data_manager.get_main_indices("cn")
            )
            fut_stats = pool.submit(
                _safe_call, "market_stats", lambda: self._data_manager.get_market_stats()
            )
            fut_sectors = pool.submit(
                _safe_call, "sector_rankings", lambda: self._data_manager.get_sector_rankings(10)
            )

            all_futures = {fut_indices: "indices", fut_stats: "market_stats", fut_sectors: "sector_rankings"}
            today_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

            try:
                for future in as_completed(all_futures, timeout=self._INTEL_TOTAL_TIMEOUT):
                    label_key = all_futures[future]
                    try:
                        label, result = future.result()
                    except Exception as e:
                        logger.warning(f"[StockPicker] {label_key} future error: {e}")
                        continue

                    if label == "indices" and result:
                        intel["indices"] = result
                        idx_dates = [idx.get("data_date") for idx in result if idx.get("data_date")]
                        has_stale_flag = any(idx.get("_stale") for idx in result)
                        if has_stale_flag or (idx_dates and all(d != today_str for d in idx_dates)):
                            intel["indices_stale"] = True
                            logger.warning(
                                "[StockPicker] Index data is stale "
                                f"(dates={set(idx_dates)}, today={today_str})"
                            )
                    elif label == "market_stats" and result:
                        intel["stats"] = result
                        has_stale_flag = result.get("_stale", False)
                        stats_date = result.get("data_date")
                        if has_stale_flag or (stats_date and stats_date != today_str):
                            intel["stats_stale"] = True
                            logger.warning(
                                f"[StockPicker] Market stats data is stale "
                                f"(data_date={stats_date}, today={today_str})"
                            )
                    elif label == "sector_rankings" and result:
                        top_sectors, bottom_sectors = result
                        if top_sectors:
                            intel["top_sectors"] = top_sectors
                            intel["bottom_sectors"] = bottom_sectors
                            all_secs = top_sectors + bottom_sectors
                            has_stale_flag = any(s.get("_stale") for s in all_secs)
                            sec_dates = [s.get("data_date") for s in all_secs if s.get("data_date")]
                            if has_stale_flag or (sec_dates and all(d != today_str for d in sec_dates)):
                                intel["sectors_stale"] = True
                                logger.warning(
                                    f"[StockPicker] Sector data is stale "
                                    f"(dates={set(sec_dates)}, today={today_str})"
                                )
            except FuturesTimeout:
                timed_out = [lbl for f, lbl in all_futures.items() if not f.done()]
                logger.warning(
                    f"[StockPicker] _gather_market_intel global timeout ({self._INTEL_TOTAL_TIMEOUT}s), "
                    f"unfinished: {timed_out}"
                )
        finally:
            for f in (fut_indices, fut_stats, fut_sectors):
                if f is not None and not f.done():
                    f.cancel()

        elapsed = time.time() - gather_start
        if elapsed >= self._INTEL_TOTAL_TIMEOUT:
            logger.warning(
                f"[StockPicker] _gather_market_intel hit overall timeout ({self._INTEL_TOTAL_TIMEOUT}s), "
                f"returning partial data: {list(intel.keys())}"
            )

        if self._search_service and self._search_service._providers:
            all_news: List[Dict] = []
            for query in self.SEARCH_QUERIES:
                try:
                    resp = self._search_service.search_stock_news(
                        "000001", "A股市场", max_results=5,
                        focus_keywords=[query],
                    )
                    if resp and resp.success and resp.results:
                        for r in resp.results:
                            all_news.append({
                                "title": r.title,
                                "snippet": r.snippet[:200] if r.snippet else "",
                            })
                except Exception as e:
                    logger.warning(f"[StockPicker] Search '{query}' failed: {e}")

            seen: set = set()
            unique: List[Dict] = []
            for n in all_news:
                if n["title"] not in seen:
                    seen.add(n["title"])
                    unique.append(n)
            intel["news"] = unique[:10]

        return intel

    def fetch_chip_for_candidates(
        self, candidates: List[ScreenedStock], max_stocks: int = 25, timeout_per_stock: float = 8.0
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch chip distribution for candidates."""
        chip_map: Dict[str, Dict[str, Any]] = {}
        if not getattr(self.config, "enable_chip_distribution", True):
            return chip_map
        if not self._data_manager or not candidates:
            return chip_map

        def _fetch_one(code: str) -> Optional[Dict[str, Any]]:
            try:
                chip = self._data_manager.get_chip_distribution(code)
                if chip:
                    return {
                        "concentration_90": chip.concentration_90,
                        "profit_ratio": chip.profit_ratio,
                    }
            except Exception as e:
                logger.debug(f"[StockPicker] Chip fetch failed for {code}: {e}")
            return None

        to_fetch = [s.code for s in candidates[:max_stocks]]
        for code in to_fetch:
            try:
                data = run_with_timeout(lambda c=code: _fetch_one(c), timeout_per_stock, "chip")
                if data:
                    chip_map[code] = data
            except FuturesTimeout:
                logger.debug(f"[StockPicker] Chip fetch timeout for {code}")
            except Exception as e:
                logger.debug(f"[StockPicker] Chip fetch error for {code}: {e}")

        if chip_map:
            logger.info(f"[StockPicker] Fetched chip data for {len(chip_map)}/{len(to_fetch)} candidates")
        return chip_map

    def build_prompt(
        self, intel: Dict[str, Any], candidates: List[ScreenedStock],
        chip_map: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> str:
        """Build the prompt with quant pool, chip data (if any), and market intel."""
        chip_map = chip_map or {}
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        strategies = getattr(self._screener, "_picker_strategies", []) or ["buy_pullback"]
        from src.services.picker_strategies import get_strategy_params, STRATEGY_DISPLAY_NAMES
        strategy_labels = ", ".join(STRATEGY_DISPLAY_NAMES.get(x, x) for x in strategies)
        p = get_strategy_params(strategies[0]) if strategies else PickerModeParams.for_mode("balanced")
        exempt_desc = "各策略自定" if len(strategies) > 1 else f"{getattr(p, 'leader_bias_exempt_pct', 0)}%"
        parts = [
            f"# 今日选股分析 ({today})\n",
            f"**当前配置**：策略={strategy_labels}，乖离率阈值={p.max_bias_pct}%，龙头豁免={exempt_desc}，"
            f"PE理想区间={p.pe_ideal_low}-{p.pe_ideal_high}倍\n",
        ]

        # -- Quant pool --
        if candidates:
            n_triple = sum(1 for s in candidates if getattr(s, "resonance", "") == "triple")
            n_double = sum(1 for s in candidates if getattr(s, "resonance", "") == "double")
            resonance_note = ""
            if n_triple or n_double:
                resonance_note = (
                    f"\n> 多策略共振统计：3 策略共振 {n_triple} 只 ⭐⭐⭐，"
                    f"2 策略共振 {n_double} 只 ⭐⭐（共振票应优先选入）"
                )
            parts.append(
                f"## 量化筛选池（从全市场筛选出的 {len(candidates)} 只候选）{resonance_note}"
            )
            has_chip = any(s.code in chip_map for s in candidates)
            has_strategies = len(strategies) > 1 and any(getattr(s, "strategies", []) for s in candidates)
            has_levels = any(getattr(s, "ideal_buy", 0) > 0 for s in candidates)
            strat_col = "| 策略 |" if has_strategies else ""
            strat_sep = "|------|" if has_strategies else ""
            level_col = "| 买入价 | 止损 | 首止盈 | R/R | 仓位% |" if has_levels else ""
            level_sep = "|--------|------|--------|-----|-------|" if has_levels else ""
            if has_chip:
                parts.append(
                    f"| 代码 | 名称 | 现价 | 涨跌% | 量比 | 换手% | PE | 市值(亿) | 60日% | "
                    f"筹码90% | 获利% |{strat_col}{level_col} 评分 |"
                )
                parts.append(
                    f"|------|------|------|-------|------|-------|-----|---------|-------|"
                    f"---------|-------|{strat_sep}{level_sep}------|"
                )
            else:
                parts.append(
                    f"| 代码 | 名称 | 现价 | 涨跌% | 量比 | 换手% | PE | 市值(亿) | 60日% |"
                    f"{strat_col}{level_col} 评分 |"
                )
                parts.append(
                    f"|------|------|------|-------|------|-------|-----|---------|-------|"
                    f"{strat_sep}{level_sep}------|"
                )
            for s in candidates:
                row = (
                    f"| {s.code} | {s.name} | {s.price:.2f} | "
                    f"{s.change_pct:+.2f} | {s.volume_ratio:.1f} | "
                    f"{s.turnover_rate:.1f} | {s.pe:.0f} | "
                    f"{s.market_cap:.0f} | {s.change_pct_60d:+.1f} |"
                )
                if has_chip:
                    chip = chip_map.get(s.code, {})
                    c90 = chip.get("concentration_90")
                    pr = chip.get("profit_ratio")
                    c90_str = f"{c90:.1%}" if c90 is not None else "-"
                    pr_str = f"{pr:.0%}" if pr is not None else "-"
                    row += f" {c90_str} | {pr_str} |"
                if has_strategies:
                    strat_tags = getattr(s, "strategies", []) or []
                    strat_labels_row = ",".join(STRATEGY_DISPLAY_NAMES.get(x, x) for x in strat_tags[:3])
                    res = getattr(s, "resonance", "")
                    badge = "⭐⭐⭐" if res == "triple" else ("⭐⭐" if res == "double" else "")
                    row += f" {strat_labels_row}{badge} |"
                if has_levels:
                    if getattr(s, "ideal_buy", 0) > 0:
                        row += (
                            f" {s.ideal_buy:.2f} | {s.stop_loss:.2f} | "
                            f"{s.take_profit_1:.2f} | {s.risk_reward:.2f} | "
                            f"{s.position_pct * 100:.0f}% |"
                        )
                    else:
                        row += " - | - | - | - | - |"
                row += f" {s.score:.0f} |"
                parts.append(row)
            parts.append("")
            if has_levels:
                parts.append(
                    "> 📐 上表中的【买入价 / 止损 / 首止盈 / R/R / 仓位】由系统统一计算，"
                    "**严禁 LLM 修改**。AI 仅负责"
                    "（1）从池中精选 1-5 只优质标的；"
                    "（2）判断现价是否仍在【买入价】附近（偏离 >2% 视为已错过买点）；"
                    "（3）补充行业 / 催化剂 / 风险描述。"
                )
                parts.append("")
        else:
            parts.append(
                "## 量化筛选池\n（今日筛选未产出候选，请返回空推荐列表，不要自行选股）\n"
            )

        # -- Market intel --
        indices_stale = intel.get("indices_stale", False)
        if intel.get("indices"):
            if indices_stale:
                parts.append(
                    "## 主要指数\n"
                    "今日指数实时数据获取失败，请勿在 market_summary 中引用具体指数涨跌幅数值。"
                    "请仅基于筛选池质量和板块数据给出市场判断。\n"
                )
            else:
                idx_dates = [idx.get("data_date") for idx in intel["indices"] if idx.get("data_date")]
                if idx_dates and all(d != today for d in idx_dates):
                    stale_date = idx_dates[0]
                    parts.append(
                        f"\u26a0\ufe0f 注意\uff1a以下指数数据来自 {stale_date}\uff08非今日实时数据\uff09\u3002\n"
                    )
                parts.append("## 主要指数")
                for idx in intel["indices"]:
                    name = idx.get("name", "")
                    current = idx.get("current", 0)
                    pct = idx.get("change_pct", 0)
                    arrow = "\u2191" if pct > 0 else "\u2193" if pct < 0 else "\u2192"
                    parts.append(f"- {name}: {current:.2f} ({arrow}{pct:+.2f}%)")
                parts.append("")

        if intel.get("stats"):
            stats_stale = intel.get("stats_stale", False)
            if stats_stale:
                parts.append(
                    "## 市场统计\n"
                    "今日市场涨跌统计实时数据获取失败，以下为往期数据，请勿在 market_summary 中引用具体涨跌家数。\n"
                )
            else:
                s = intel["stats"]
                data_date = s.get("data_date")
                if data_date and data_date != today:
                    parts.append(
                        f"\u26a0\ufe0f 注意\uff1a以下市场涨跌统计来自 {data_date}\uff08非今日实时数据\uff09\uff0c"
                        "\u8bf7以指数实时涨跌为准描述今日市场\u3002\n"
                    )
                parts.append("## 市场统计")
                parts.append(
                    f"- 上涨: {s.get('up_count', 0)} | 下跌: {s.get('down_count', 0)} | "
                    f"平盘: {s.get('flat_count', 0)}"
                )
                parts.append(
                    f"- 涨停: {s.get('limit_up_count', 0)} | 跌停: {s.get('limit_down_count', 0)}"
                )
                amt = s.get("total_amount", 0)
                if amt:
                    parts.append(f"- 两市成交额: {amt:.0f} 亿元")
                parts.append("")

        if intel.get("top_sectors"):
            sectors_stale = intel.get("sectors_stale", False)
            if sectors_stale:
                parts.append(
                    "## 板块排行\n"
                    "今日板块排行实时数据获取失败，以下为往期数据，请勿在报告中引用具体板块涨跌幅。\n"
                )
            else:
                parts.append("## 板块排行")
                parts.append("### 领涨板块")
                for sec in intel["top_sectors"][:10]:
                    parts.append(f"- {sec['name']}: {sec['change_pct']:+.2f}%")
                if intel.get("bottom_sectors"):
                    parts.append("### 领跌板块")
                    for sec in intel["bottom_sectors"][:5]:
                        parts.append(f"- {sec['name']}: {sec['change_pct']:+.2f}%")
                parts.append("")

        if intel.get("news"):
            parts.append("## 今日热点新闻")
            for i, n in enumerate(intel["news"][:10], 1):
                parts.append(f"{i}. **{n['title']}**")
                if n.get("snippet"):
                    parts.append(f"   {n['snippet']}")
            parts.append("")

        parts.append(
            "请从量化筛选池和市场情报中，精选 1-5 只最值得关注的 A 股股票。"
            "优先从筛选池中选择，建议行业分散、避免单行业过度集中。"
            "严格按照 JSON 格式输出。"
        )

        return "\n".join(parts)

    def call_llm(self, prompt: str) -> Optional[str]:
        """Call LLM with the combined prompt."""
        if not self._analyzer or not self._analyzer.is_available():
            logger.error("[StockPicker] LLM analyzer not available")
            return None

        full_prompt = f"{PICK_SYSTEM_PROMPT}\n\n---\n\n{prompt}"
        logger.info("[StockPicker] Calling LLM for final stock selection...")
        return self._analyzer.generate_text(full_prompt, max_tokens=16384, temperature=0.7)

    def parse_result(self, llm_output: str, result: PickerResult):
        """Parse LLM JSON output into PickerResult."""
        cleaned = (llm_output or "").strip()
        if not cleaned:
            logger.warning("[StockPicker] LLM returned empty content (possible token budget exhaustion)")
            result.error = "LLM returned empty content \u2013 token budget may be exhausted"
            result.success = False
            return

        try:
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines)

            repaired = repair_json(cleaned)
            data = json.loads(repaired)

            result.market_summary = data.get("market_summary", "")
            result.sectors_to_watch = data.get("sectors_to_watch", [])
            result.risk_warning = data.get("risk_warning", "")

            candidate_by_code: Dict[str, ScreenedStock] = {
                s.code: s for s in (result.screened_pool or [])
            }

            for p in data.get("picks", []):
                code = str(p.get("code", "")).strip()
                name = str(p.get("name", "")).strip()
                if code and name:
                    cand = candidate_by_code.get(code)
                    pick = StockPick(
                        code=code, name=name,
                        sector=p.get("sector", ""),
                        reason=p.get("reason", ""),
                        catalyst=p.get("catalyst", ""),
                        attention=p.get("attention", "medium"),
                        risk_note=p.get("risk_note", ""),
                    )
                    if cand:
                        pick.ideal_buy = cand.ideal_buy
                        pick.stop_loss = cand.stop_loss
                        pick.take_profit_1 = cand.take_profit_1
                        pick.take_profit_2_rule = cand.take_profit_2_rule
                        pick.position_pct = cand.position_pct
                        pick.risk_reward = cand.risk_reward
                        pick.strategies = list(cand.strategies or [])
                        pick.resonance = cand.resonance
                        pick.secondary_buy = getattr(cand, "secondary_buy", 0.0)
                        pick.limit_up_date = getattr(cand, "limit_up_date", "")
                        pick.setup = getattr(cand, "setup", "")
                    result.picks.append(pick)

            logger.info(f"[StockPicker] Parsed {len(result.picks)} stock picks")

        except Exception as e:
            logger.error(f"[StockPicker] Failed to parse LLM output: {e}")
            result.error = f"Failed to parse LLM response: {e}"
            result.success = False
