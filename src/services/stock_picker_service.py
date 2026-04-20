# -*- coding: utf-8 -*-
"""
AI Stock Picker Service (with Quantitative Screening)

Two-stage pipeline:
  Stage 1 — Quantitative screener: pull full-market data via Tushare/AkShare/efinance,
            apply multi-layer filters (fundamentals, momentum, volume), compute 60d
            change (Tushare path uses trade_cal + daily), output ~30 candidates.
  Stage 2 — AI selector: combine the quant shortlist with market intel (sectors,
            news) and ask the LLM to pick 1-5 with reasoning (宁缺毋滥).
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple  # noqa: F401 - Dict used in screen()
from zoneinfo import ZoneInfo

import pandas as pd
from json_repair import repair_json

from src.config import get_config
from src.core.trading_calendar import get_last_trading_day
from src.search_service import SearchService
from data_provider.base import DataFetcherManager, is_bse_code, is_kc_cy_stock, is_st_stock

logger = logging.getLogger(__name__)

# Bias filter threshold (严进策略): exclude stocks with MA5 bias > this %
# Mode overrides: defensive=6%, balanced=8%, offensive=10%
PICKER_MAX_BIAS_PCT = 8.0

# Volume filter: require volume ratio > this to exclude cold stocks
VOLUME_RATIO_MIN = 1.0
# Turnover: 1-15% (plan: 0.5→1 filter cold, 20→15 reduce speculation)
TURNOVER_MIN_PCT = 1.0
TURNOVER_MAX_PCT = 15.0
# Amount by market cap: <100e8 use 30M, >=100e8 use 100M (plan: 5000W ineffective for large caps)
AMOUNT_MIN_SMALL_CAP = 3e7   # 3000万 for cap < 100亿
AMOUNT_MIN_LARGE_CAP = 1e8   # 1亿 for cap >= 100亿
MARKET_CAP_TIER_YI = 100.0   # 100亿 threshold

# 60-day trend decay: gains above this % get score decay (avoid end-of-trend buys)
TREND_DECAY_THRESHOLD_PCT = 30.0

# Limit-up streak filter: exclude if >= this many limit-up days in last 5 days
LIMIT_UP_DAYS_THRESHOLD = 2
LIMIT_UP_PCT_MAIN = 9.5   # main board (60/00/002) ~10%
LIMIT_UP_PCT_KC_CY = 19.0  # ChiNext/STAR (30/688) ~20%
LIMIT_UP_PCT_BSE = 29.0    # BSE (8/4/920) ~30%
LIMIT_UP_PCT_ST = 4.5      # ST stocks ~5%


def _get_limit_up_pct(code: str, name: str = "") -> float:
    """Return limit-up percentage threshold based on board type and ST status."""
    if is_st_stock(name):
        return LIMIT_UP_PCT_ST
    if is_bse_code(code):
        return LIMIT_UP_PCT_BSE
    if is_kc_cy_stock(code):
        return LIMIT_UP_PCT_KC_CY
    return LIMIT_UP_PCT_MAIN

# Leader bias exemption: 60d change > this % to qualify
LEADER_CHANGE_60D_MIN = 15.0
# Leader: today change 2-7%, volume_ratio > 1.5, turnover 2-8%
LEADER_CHANGE_PCT_LO, LEADER_CHANGE_PCT_HI = 2.0, 7.0
LEADER_VOLUME_RATIO_MIN = 1.5
LEADER_TURNOVER_LO, LEADER_TURNOVER_HI = 2.0, 8.0
# PE scoring: partial score upper bound (outside ideal but not bubble)
PE_SCORE_PARTIAL_MAX = 80

# Per-strategy top N before merge
PICKER_TOP_N_PER_STRATEGY = 30

# B-wave risk (波浪 ABC): exclude stocks likely in B-wave bounce (fake recovery before C-wave down)
B_WAVE_LOOKBACK_DAYS = 20
B_WAVE_MIN_DROOP_PCT = 5.0  # A-wave drop must be at least 5%
B_WAVE_RETRACE_LO = 0.35    # Fibonacci B-wave zone: 38.2% retracement
B_WAVE_RETRACE_HI = 0.65    # 61.8% retracement
B_WAVE_LOW_DAYS_AGO_MIN = 2  # Low must be at least 2 days ago (we've bounced)
B_WAVE_LOW_DAYS_AGO_MAX = 14  # Low not more than 14 days ago (recent drop)


@dataclass
class MarketEnvironment:
    """Market environment assessment based on SSE index vs MA20."""
    is_strong: bool          # True=bullish (strong/neutral), False=bearish (weak)
    index_price: float       # SSE index current close
    index_ma20: float        # SSE index 20-day MA
    diff_pct: float          # (price - ma20) / ma20 * 100
    regime: str = "strong"   # "strong" / "neutral" / "weak"


def _resolve_fallback_trade_date(china_now: datetime) -> str:
    """Resolve trade_date for live mode when today has no data (e.g. weekend)."""
    last_td = get_last_trading_day("cn", china_now.date())
    return last_td.strftime("%Y%m%d") if last_td else (china_now - pd.Timedelta(days=1)).strftime("%Y%m%d")


@dataclass
class PickerModeParams:
    """Mode-specific screening parameters (defensive/balanced/offensive).

    Entry strategy shifted from "chase momentum" to "buy pullback":
    - defensive: strict pullback, prefer stocks near MA5
    - balanced: moderate pullback + small chase allowed
    - offensive: allow stronger momentum but with limits

    Healthy pullback confirmation:
    - Volume shrink: pullback with low volume (less selling pressure)
    - MA alignment: MA5 > MA10 > MA20 (bullish structure)
    - Retracement limit: don't buy if retraced too much of prior rally
    """

    max_bias_pct: float
    pe_max: float
    pe_ideal_low: float
    pe_ideal_high: float
    # Entry range (pullback strategy)
    daily_change_min: float
    daily_change_max: float
    # Consecutive up days limit
    max_consecutive_up_days: int
    # Healthy pullback confirmation
    require_volume_shrink: bool      # Require volume_ratio < 1.0 on pullback
    require_ma_bullish: bool         # Require MA5 > MA10 > MA20
    max_retracement_pct: float       # Max retracement of prior 10d rally (0.5 = 50%)
    min_pullback_from_high_pct: float = 0.0  # Min % below N-day high to qualify as pullback (0=disabled)
    max_distance_above_ma10_pct: float = 0.0  # Max % price can be above MA10 (0=disabled)
    require_price_above_ma20: bool = False     # Reject if price below MA20

    @classmethod
    def for_mode(cls, mode: str) -> "PickerModeParams":
        """Get params for given mode. Falls back to balanced for unknown mode."""
        params = PICKER_MODE_PARAMS.get((mode or "balanced").lower())
        return params or PICKER_MODE_PARAMS["balanced"]


# Single source of truth for mode params
# Strategy: "buy pullback" instead of "chase momentum"
PICKER_MODE_PARAMS = {
    # defensive: strict pullback, must have volume shrink + MA bullish + limited retracement
    "defensive": PickerModeParams(
        max_bias_pct=6.0, pe_max=50, pe_ideal_low=10, pe_ideal_high=25,
        daily_change_min=-2.0, daily_change_max=2.0, max_consecutive_up_days=2,
        require_volume_shrink=True, require_ma_bullish=True, max_retracement_pct=0.382,
    ),
    # balanced: prefer volume shrink, require MA bullish
    "balanced": PickerModeParams(
        max_bias_pct=8.0, pe_max=100, pe_ideal_low=10, pe_ideal_high=30,
        daily_change_min=-1.0, daily_change_max=4.0, max_consecutive_up_days=3,
        require_volume_shrink=False, require_ma_bullish=True, max_retracement_pct=0.5,
    ),
    # offensive: only require MA bullish, allow larger retracement
    "offensive": PickerModeParams(
        max_bias_pct=10.0, pe_max=100, pe_ideal_low=20, pe_ideal_high=50,
        daily_change_min=0.0, daily_change_max=6.0, max_consecutive_up_days=4,
        require_volume_shrink=False, require_ma_bullish=True, max_retracement_pct=0.618,
    ),
}


# ── System prompt ────────────────────────────────────────────────

PICK_SYSTEM_PROMPT = """你是一位专业的 A 股市场分析师，负责从优质股票池中精选最具投资价值的标的。

## 你的任务
你将收到两类数据：
1. **量化筛选池**：系统已从全市场 5000+ 只股票中，通过严格的量化条件（正向趋势、合理估值、健康量能）筛选出的优质候选标的
2. **市场情报**：今日大盘指数、板块排行、热点新闻

请从量化筛选池中，结合市场情报，**精选 1-5 只**最具投资价值的股票。

## 核心选股原则（严格遵循）

### 1. 严进策略（不追高）
- **量化层**：筛选池已根据模式排除乖离率过高的标的（defensive 6%/balanced 8%/offensive 10%）；若启用龙头豁免，板块龙头可放宽至配置值（需满足 60日涨幅>15%、今日涨幅 2-7%、量比>1.5、换手 2-8%）
- **推荐优先级**：乖离率 < 2% 最佳买点；2-5% 可关注；接近阈值时降级为观望
- **公式**：乖离率 = (现价 - MA5) / MA5 × 100%

### 2. 趋势质量优先
- 60日涨幅 > 20%：强势趋势，加分
- 60日涨幅 10-20%：稳健趋势，正常评估
- 60日涨幅 5-10%：弱势趋势，需更强催化剂才考虑
- **今日涨幅**：2-6% 为健康上涨，>7% 需警惕追高风险

### 3. 估值安全边际（按模式）
- **defensive**：PE 10-25 倍理想，>50 排除
- **balanced**：PE 10-30 倍理想，30-50 需业绩支撑
- **offensive**：PE 20-50 倍可接受（动量股），>50 谨慎
- 具体区间见下方「当前配置」

### 4. 量能健康度
- 量比 1.0-2.5：健康放量，加分
- 量比 > 3.0：需警惕过度投机
- 换手率 2-8%：理想区间（筛选层已收紧为 1-15%）

### 4b. 买点与支撑规则
- **均线拟合**：均线缠绕（MA5、MA10、MA20 距离 <1%）时，不能把均线当支撑位，此时均线无参考价值
- **买点偏好**：量能配合（量比 1-2.5）的回踩 MA5/MA10 是较好买点；无 ABC 调整时警惕买到 B 浪反弹

### 5. 板块与市场共振
- 个股所在板块与今日领涨板块重合时，提升优先级
- 逆板块上涨（板块跌个股涨）需有独立催化剂才考虑
- **行业分散**：建议推荐标的分散于不同行业，避免单行业过度集中

### 5b. 筹码集中度（如有数据）
- 90%集中度 < 10%：筹码高度集中，主力控盘，加分
- 90%集中度 10-15%：筹码较集中，正常评估
- 获利比例 50-80%：健康区间；>90% 警惕派发

### 6. 风险控制
- **空仓触发**：若池中乖离率 > 5% 的标的占比 > 60%，说明市场整体偏高，应输出空仓观望、减少或零推荐
- 市场成交量萎缩或指数大跌时，优先建议空仓观望

## 输出格式
严格输出 JSON，不要输出 markdown 或解释文字：

```json
{
  "market_summary": "一句话概括今日市场特征及选股难度",
  "picks": [
    {
      "code": "600519",
      "name": "贵州茅台",
      "sector": "白酒",
      "reason": "推荐理由（引用具体数据：乖离率X%，60日涨幅X%，PE X倍）",
      "catalyst": "催化剂/驱动因素",
      "attention": "high/medium/low",
      "risk_note": "主要风险提示（必须包含乖离率风险提示）"
    }
  ],
  "sectors_to_watch": ["板块1", "板块2", "板块3"],
  "risk_warning": "整体市场风险提示（如：当前市场乖离率偏高，建议控制仓位）"
}
```

## 注意事项
- code 和 name 必须使用筛选池中提供的真实数据
- attention: high（强烈关注，乖离率<2%且趋势强）、medium（适度关注）、low（跟踪观察，乖离率接近5%）
- **宁缺毋滥**：池子质量不佳时宁可推荐 0-2 只或空仓观望，绝不硬凑数量
- reason 中**必须引用乖离率**，这是与后续分析保持一致的关键
"""


# ── Data classes ─────────────────────────────────────────────────

@dataclass
class ScreenedStock:
    """A stock that passed quantitative screening."""
    code: str
    name: str
    price: float = 0.0
    change_pct: float = 0.0
    volume_ratio: float = 0.0
    turnover_rate: float = 0.0
    pe: float = 0.0
    pb: float = 0.0
    market_cap: float = 0.0          # in 亿
    amount: float = 0.0              # 成交额(亿)
    change_pct_60d: float = 0.0      # 60日涨跌幅
    score: float = 0.0               # composite score
    strategies: List[str] = field(default_factory=list)  # strategy IDs that selected this stock

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "code": self.code, "name": self.name, "price": self.price,
            "change_pct": round(self.change_pct, 2),
            "volume_ratio": round(self.volume_ratio, 2),
            "turnover_rate": round(self.turnover_rate, 2),
            "pe": round(self.pe, 1), "pb": round(self.pb, 2),
            "market_cap_yi": round(self.market_cap, 1),
            "amount_yi": round(self.amount, 1),
            "change_pct_60d": round(self.change_pct_60d, 2),
            "score": round(self.score, 1),
        }
        if self.strategies:
            d["strategies"] = self.strategies
        return d


@dataclass
class ScreenStats:
    """Statistics from the screening process."""
    total_stocks: int = 0
    after_basic: int = 0
    after_momentum: int = 0
    after_volume: int = 0
    final_pool: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_stocks": self.total_stocks,
            "after_basic_filter": self.after_basic,
            "after_momentum_filter": self.after_momentum,
            "after_volume_filter": self.after_volume,
            "final_pool": self.final_pool,
        }


@dataclass
class StockPick:
    """A single stock recommendation from the AI."""
    code: str
    name: str
    sector: str = ""
    reason: str = ""
    catalyst: str = ""
    attention: str = "medium"
    risk_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "sector": self.sector,
            "reason": self.reason, "catalyst": self.catalyst,
            "attention": self.attention, "risk_note": self.risk_note,
        }


@dataclass
class PickerResult:
    """Final result combining screening + AI selection."""
    success: bool = False
    market_summary: str = ""
    picks: List[StockPick] = field(default_factory=list)
    sectors_to_watch: List[str] = field(default_factory=list)
    risk_warning: str = ""
    screen_stats: Optional[ScreenStats] = None
    screened_pool: List[ScreenedStock] = field(default_factory=list)
    screened_pool_by_strategy: Dict[str, List[ScreenedStock]] = field(default_factory=dict)
    generated_at: str = ""
    error: str = ""
    elapsed_seconds: float = 0.0
    picker_mode: str = "balanced"
    picker_strategies: List[str] = field(default_factory=list)
    indices_stale: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "success": self.success,
            "market_summary": self.market_summary,
            "picks": [p.to_dict() for p in self.picks],
            "sectors_to_watch": self.sectors_to_watch,
            "risk_warning": self.risk_warning,
            "screen_stats": self.screen_stats.to_dict() if self.screen_stats else None,
            "screened_pool": [s.to_dict() for s in self.screened_pool],
            "screened_pool_by_strategy": {
                k: [s.to_dict() for s in v] for k, v in self.screened_pool_by_strategy.items()
            },
            "generated_at": self.generated_at,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "error": self.error,
        }
        d["picker_mode"] = self.picker_mode
        d["picker_strategies"] = self.picker_strategies
        d["indices_stale"] = self.indices_stale
        return d


def get_tushare_api(data_manager=None):
    """Get Tushare Pro API from data_manager's TushareFetcher or create standalone instance."""
    if data_manager:
        for fetcher in data_manager._fetchers:
            if fetcher.__class__.__name__ == "TushareFetcher" and hasattr(fetcher, "_api") and fetcher._api:
                return fetcher._api
    try:
        cfg = get_config()
        if not cfg.tushare_token:
            return None
        import tushare as ts
        # Pass token directly to avoid writing ~/tk.csv (fixes Operation not permitted)
        logger.info("[Picker] Created standalone Tushare API instance")
        return ts.pro_api(token=cfg.tushare_token)
    except Exception as e:
        logger.warning(f"[Picker] Cannot init Tushare: {e}")
        return None


def create_screener_from_config(data_manager=None) -> "StockScreener":
    """Create StockScreener with config from environment. Use for picker and backtest."""
    cfg = get_config()
    strategies = getattr(cfg, "picker_strategies", None) or ["buy_pullback"]
    return StockScreener(
        data_manager=data_manager,
        picker_strategies=strategies,
        picker_mode=cfg.picker_mode,
        turnover_min=cfg.picker_turnover_min,
        turnover_max=cfg.picker_turnover_max,
        enable_b_wave_filter=getattr(cfg, "picker_enable_b_wave_filter", True),
        allow_loss=getattr(cfg, "picker_allow_loss", False),
        spot_timeout=getattr(cfg, "picker_spot_timeout", 30),
    )


# ── Quantitative Screener ───────────────────────────────────────

class StockScreener:
    """Multi-layer quantitative screener using full-market spot data."""

    _EXCLUDE_NAME_KEYWORDS = ("ST", "*ST", "退市", "N ", "C ")
    _ETF_PREFIXES = ("51", "52", "56", "58", "15", "16", "18")

    def __init__(
        self,
        data_manager=None,
        picker_strategies: Optional[List[str]] = None,
        picker_mode: str = "balanced",
        turnover_min: Optional[float] = None,
        turnover_max: Optional[float] = None,
        enable_b_wave_filter: bool = True,
        allow_loss: bool = False,
        spot_timeout: Optional[int] = None,
    ):
        self._data_manager = data_manager
        self._spot_timeout = spot_timeout if spot_timeout is not None else int(
            os.getenv("PICKER_SPOT_TIMEOUT", "30")
        )
        self._as_of_date: Optional[str] = None  # YYYY-MM-DD for historical screening
        self._picker_strategies = picker_strategies if picker_strategies else ["buy_pullback"]
        self._picker_mode = (picker_mode or "balanced").lower()
        self._turnover_min = turnover_min if turnover_min is not None else TURNOVER_MIN_PCT
        self._turnover_max = turnover_max if turnover_max is not None else TURNOVER_MAX_PCT
        self._enable_b_wave_filter = enable_b_wave_filter
        self._allow_loss = allow_loss
        self._stock_basic_cache: Optional[pd.DataFrame] = None  # Reuse across days in backtest

    # Strategies that require daily spot data (fetched via _fetch_spot_data).
    # eod_buyback uses a dedicated realtime full-market path and does NOT need daily data.
    DAILY_DATA_STRATEGIES = {"buy_pullback", "breakout", "bottom_reversal"}

    # Strategies that benefit from sector strength filtering
    SECTOR_FILTER_STRATEGIES = {"buy_pullback", "breakout"}

    def _check_market_environment(self) -> Optional[MarketEnvironment]:
        """Check SSE index vs MA20 to determine market regime.

        Returns MarketEnvironment or None if data unavailable.
        """
        if not self._data_manager:
            return None
        try:
            # Use SSE composite index (000001.SH) via dedicated index API
            df, source = self._data_manager.get_index_daily_data(
                index_code="000001.SH", days=25, end_date=self._as_of_date,
            )
            if df is None or len(df) < 20:
                logger.warning("[MarketGuard] SSE index data insufficient (<20 bars)")
                return None

            close_col = self._first_col(df, "close", "收盘")
            if close_col is None:
                return None

            close_series = pd.to_numeric(df[close_col], errors="coerce").dropna()
            if len(close_series) < 20:
                return None

            close_series = close_series.tail(20)
            ma20 = float(close_series.mean())
            current = float(close_series.iloc[-1])
            diff_pct = (current - ma20) / ma20 * 100 if ma20 > 0 else 0.0
            logger.debug("[MarketGuard] SSE index data: %d bars, latest close=%.1f", len(df), current)

            # Buffer zone: within 1% below MA20 is considered neutral (not weak)
            MARKET_GUARD_BUFFER_PCT = 1.0

            if current > ma20:
                regime = "strong"
            elif (ma20 - current) / ma20 * 100 <= MARKET_GUARD_BUFFER_PCT:
                regime = "neutral"  # Within buffer zone
            else:
                regime = "weak"

            env = MarketEnvironment(
                is_strong=regime != "weak",  # strong and neutral both pass
                index_price=current,
                index_ma20=ma20,
                diff_pct=diff_pct,
                regime=regime,
            )
            logger.info(
                "[MarketGuard] SSE %.1f %s MA20 %.1f (diff %+.2f%%) -> %s",
                current, ">" if current > ma20 else "<", ma20, diff_pct,
                regime.upper(),
            )
            return env
        except Exception as e:
            logger.warning("[MarketGuard] Market check failed: %s", e)
            return None

    def screen(self, trade_date: Optional[str] = None) -> Tuple[List[ScreenedStock], ScreenStats, Dict[str, List[ScreenedStock]]]:
        """Run the full screening pipeline. Returns (candidates, stats, candidates_per_strategy).
        When trade_date is provided (YYYYMMDD), run historical screening (Tushare only).
        Uses multi-strategy when picker_strategies has multiple entries."""
        stats = ScreenStats()
        self._as_of_date = self._trade_date_to_iso(trade_date) if trade_date else None

        # Preserve original strategies — restored in finally block
        original_strategies = list(self._picker_strategies)

        try:
            # -- Market environment guard --
            cfg = get_config()
            if getattr(cfg, "picker_market_guard", True):
                market_env = self._check_market_environment()
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

                    # Layer 1.5: Prepare sector strength data
                    _sector_strong_codes: Set[str] = set()
                    # Only fetch sector data when at least one selected strategy needs it
                    _need_sector = any(s in self.SECTOR_FILTER_STRATEGIES for s in daily_strategies)
                    if getattr(cfg, "picker_sector_filter", True) and _need_sector:
                        try:
                            from src.services.sector_strength_service import SectorStrengthService
                            sector_svc = SectorStrengthService()
                            sector_top_pct = getattr(cfg, "picker_sector_top_pct", 30) / 100.0
                            # Wrap sector fetch with timeout to prevent blocking
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
                                        "[Screener] Sector codes fetch timed out or failed (%s), skipping sector filter",
                                        _te,
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

                        if cands:
                            candidates_per_strategy[strategy_id] = cands
                            logger.info(f"[Screener] {strategy_id}: {len(cands)} candidates")
                            if logger.isEnabledFor(10):  # DEBUG level
                                top5 = cands[:5]
                                top5_str = ", ".join(f"{c.code}({c.score:.1f})" for c in top5)
                                logger.debug(f"[Screener] {strategy_id} top-5: {top5_str}")
            else:
                logger.info("[Screener] Skipping daily data fetch (only realtime strategies selected)")

            # --- eod_buyback: dedicated realtime full-market screening path ---
            if "eod_buyback" in self._picker_strategies and self._data_manager:
                logger.info("[Screener] Running eod_buyback via realtime full-market screening...")
                eod_rt_cands = self._screen_eod_buyback_realtime()
                if eod_rt_cands:
                    candidates_per_strategy["eod_buyback"] = eod_rt_cands
                    logger.info(f"[Screener] eod_buyback (realtime path): {len(eod_rt_cands)} candidates")
                else:
                    candidates_per_strategy.pop("eod_buyback", None)
                    logger.info("[Screener] eod_buyback (realtime path): 0 candidates")

            if not candidates_per_strategy:
                stats.final_pool = 0
                logger.warning("[Screener] No candidates from any strategy")
                return [], stats, {}

            candidates = merge_candidates_by_code(candidates_per_strategy)
            stats.final_pool = len(candidates)
            logger.info(f"[Screener] Merged {stats.final_pool} candidates from {len(candidates_per_strategy)} strategies")
            return candidates, stats, candidates_per_strategy
        finally:
            # Always restore original strategies after this call
            self._picker_strategies = original_strategies

    def screen_as_of(self, trade_date: str) -> Tuple[List[ScreenedStock], ScreenStats, Dict[str, List[ScreenedStock]]]:
        """Run screening as of a specific trade date (YYYYMMDD). For backtest use."""
        return self.screen(trade_date=trade_date)

    @staticmethod
    def _first_col(df: pd.DataFrame, *names: str):
        """Return first column name that exists in df, or None."""
        for n in names:
            if n in df.columns:
                return n
        return None

    @staticmethod
    def _trade_date_to_iso(trade_date: str) -> str:
        """Convert YYYYMMDD to YYYY-MM-DD."""
        if not trade_date or len(trade_date) != 8:
            return trade_date
        return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"

    def _fetch_daily_batch(
        self,
        requests: List[Tuple[str, Optional[str], Optional[str], int]],
        max_workers: int = 5,
        total_timeout: float = 120.0,
    ) -> Dict[Tuple[str, str, str, int], Tuple[pd.DataFrame, str]]:
        """Fetch get_daily_data for multiple (code, start, end, days) in parallel.
        Deduplicates requests by key. Returns {(code, start, end, days): (df, source)}.
        Failed fetches are omitted.  A total_timeout (default 120s) prevents
        the batch from blocking the pipeline indefinitely."""
        from concurrent.futures import as_completed, TimeoutError as FuturesTimeout
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
        # Explicit pool management: avoid `with` which calls shutdown(wait=True) and blocks on timeout
        pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="screener_fetch")
        futures = {pool.submit(_fetch, req): req for req in unique_requests}
        try:
            for future in as_completed(futures, timeout=total_timeout):
                try:
                    res = future.result()  # already done, no extra timeout needed
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
            pool.shutdown(wait=False, cancel_futures=True)
        return results

    def _screen_eod_buyback_realtime(self) -> List[ScreenedStock]:
        """Screen eod_buyback via Tushare batch realtime quotes (lobster approach).

        Simple approach: fetch all A-share codes, batch query 200 at a time
        via ts.get_realtime_quotes(), filter in one pass.

        Steps:
          1. Get all stock codes from available fetcher
          2. Batch query realtime quotes (200 per batch) via ts.get_realtime_quotes()
          3. Compute change_pct from price / pre_close
          4. One-pass filter: mainboard + change 3-6% + turnover 5-12% + market_cap 60-300yi
          5. Check recent limit-up (Rule 4) via daily data
          6. Return ScreenedStock list
        """
        import tushare as ts
        from src.services.picker_strategies import is_mainboard_stock

        if not self._data_manager:
            logger.warning("[EOD-RT] No data_manager")
            return []

        # Step 1: Get all stock codes
        t0 = time.time()

        all_codes: list = []
        for fetcher in self._data_manager._fetchers:
            if hasattr(fetcher, "get_stock_list"):
                try:
                    df_list = fetcher.get_stock_list()
                    if df_list is not None and not df_list.empty:
                        all_codes = df_list["code"].tolist()
                        logger.info(
                            f"[EOD-RT] Got {len(all_codes)} stock codes from {type(fetcher).__name__}"
                        )
                        break
                except Exception as e:
                    logger.debug(f"[EOD-RT] get_stock_list failed from {type(fetcher).__name__}: {e}")

        if not all_codes:
            logger.warning("[EOD-RT] Failed to get stock code list")
            return []

        # Step 2: Batch query realtime quotes (200 per batch)
        BATCH_SIZE = 200
        all_dfs: list = []
        for i in range(0, len(all_codes), BATCH_SIZE):
            batch = all_codes[i : i + BATCH_SIZE]
            try:
                df_batch = ts.get_realtime_quotes(batch)
                if df_batch is not None and not df_batch.empty:
                    all_dfs.append(df_batch)
            except Exception as e:
                logger.debug(f"[EOD-RT] Batch {i // BATCH_SIZE} failed: {e}")

        if not all_dfs:
            logger.warning("[EOD-RT] No realtime data from any batch")
            return []

        df = pd.concat(all_dfs, ignore_index=True)
        logger.info(f"[EOD-RT] Fetched realtime quotes for {len(df)} stocks in {time.time() - t0:.1f}s")

        # Step 2.5: Supplement turnover_rate and total_mv from Tushare Pro daily_basic
        # ts.get_realtime_quotes() doesn't provide these fields; fetch the latest
        # trading day's daily_basic data as a supplement.
        try:
            tushare_fetcher = None
            for fetcher in self._data_manager._fetchers:
                if type(fetcher).__name__ == "TushareFetcher":
                    tushare_fetcher = fetcher
                    break

            if tushare_fetcher and tushare_fetcher._api:
                from src.core.trading_calendar import get_last_trading_day as _get_ltd

                now_sh_sup = datetime.now(ZoneInfo("Asia/Shanghai"))
                trade_day = _get_ltd("cn", now_sh_sup.date())
                if trade_day is None:
                    # Fallback: try today, then yesterday, then day-before-yesterday
                    from datetime import timedelta as _td
                    for offset in (0, 1, 2):
                        candidate = (now_sh_sup.date() - _td(days=offset)).strftime("%Y%m%d")
                        tushare_fetcher._check_rate_limit()
                        _df_try = tushare_fetcher._api.daily_basic(
                            trade_date=candidate, fields="ts_code,turnover_rate,total_mv"
                        )
                        if _df_try is not None and not _df_try.empty:
                            trade_day = (now_sh_sup.date() - _td(days=offset))
                            break

                if trade_day is not None:
                    trade_date_str = trade_day.strftime("%Y%m%d")
                    tushare_fetcher._check_rate_limit()
                    df_basic = tushare_fetcher._api.daily_basic(
                        trade_date=trade_date_str,
                        fields="ts_code,turnover_rate,total_mv,pe_ttm,pb",
                    )
                    if df_basic is not None and not df_basic.empty:
                        # Convert ts_code (e.g. "000001.SZ") to plain code ("000001")
                        df_basic["code"] = df_basic["ts_code"].str.split(".").str[0]
                        # total_mv from Tushare Pro is in 万元, convert to 亿
                        df_basic["total_mv_yi"] = df_basic["total_mv"] / 1e4
                        df = df.merge(
                            df_basic[["code", "turnover_rate", "total_mv_yi", "pe_ttm", "pb"]],
                            on="code", how="left",
                        )
                        logger.info(
                            f"[EOD-RT] Supplemented turnover/mv from daily_basic({trade_date_str}): "
                            f"{df['turnover_rate'].notna().sum()} turnover, "
                            f"{df['total_mv_yi'].notna().sum()} market_cap"
                        )
                    else:
                        logger.warning(f"[EOD-RT] daily_basic returned empty for {trade_date_str}")
                else:
                    logger.warning("[EOD-RT] Could not determine latest trading day for daily_basic")
            else:
                logger.debug("[EOD-RT] TushareFetcher not available, skipping daily_basic supplement")
        except Exception as e:
            logger.warning(f"[EOD-RT] Failed to supplement from daily_basic: {e}")

        logger.debug(f"[EOD-RT] DataFrame columns after supplement: {list(df.columns)}")

        # Step 3: Compute change_pct from price and pre_close
        df["price"] = pd.to_numeric(df.get("price", pd.Series(dtype=float)), errors="coerce")
        # pre_close may be named 'pre_close' or 'settlement' depending on tushare version
        pre_close_col = "pre_close" if "pre_close" in df.columns else "settlement"
        df["pre_close"] = pd.to_numeric(df.get(pre_close_col, pd.Series(dtype=float)), errors="coerce")
        df["calc_change_pct"] = (
            (df["price"] - df["pre_close"]) / df["pre_close"].replace(0, float("nan"))
        ) * 100

        # Step 4: One-pass filter
        mask = pd.Series(True, index=df.index)

        # Rule 1: mainboard only
        code_col = "code"
        if code_col in df.columns:
            mask &= df[code_col].apply(lambda c: is_mainboard_stock(str(c)))

        # Rule: exclude ST
        if "name" in df.columns:
            mask &= ~df["name"].str.contains("ST", na=False, case=False)

        # Rule 2: change 3%-6% (filter weak gains & near-limit extremes)
        mask &= (df["calc_change_pct"] >= 3.0) & (df["calc_change_pct"] <= 6.0)

        # Rule 3: turnover 5%-12% (wider band to capture more candidates)
        turnover_col = None
        for col_name in ["turnover", "turnover_rate"]:
            if col_name in df.columns:
                turnover_col = col_name
                break
        if turnover_col:
            turnover = pd.to_numeric(df[turnover_col], errors="coerce")
            has_turnover = turnover.notna() & (turnover > 0)
            if has_turnover.any():
                mask &= ~has_turnover | ((turnover >= 5.0) & (turnover <= 12.0))
                logger.info(
                    f"[EOD-RT] Turnover filter applied via '{turnover_col}' "
                    f"({has_turnover.sum()} stocks had data)"
                )
            else:
                logger.info("[EOD-RT] Turnover data unavailable, skipping filter")
        else:
            logger.info("[EOD-RT] No turnover column, skipping filter")

        # Rule 6: market cap 60-300yi (from realtime or daily_basic 'total_mv_yi')
        # ts.get_realtime_quotes may provide mktcap in 万元 units; daily_basic total_mv_yi is already in 亿
        mktcap_col = None
        mktcap_already_yi = False
        for col_name in ["mktcap", "nmc", "market_cap"]:
            if col_name in df.columns:
                mktcap_col = col_name
                break
        if mktcap_col is None and "total_mv_yi" in df.columns:
            mktcap_col = "total_mv_yi"
            mktcap_already_yi = True
        if mktcap_col:
            mktcap = pd.to_numeric(df[mktcap_col], errors="coerce")
            # Convert to 亿: realtime mktcap from tushare is in 万元; total_mv_yi is already in 亿
            mktcap_yi = mktcap if mktcap_already_yi else mktcap / 1e4
            has_mktcap = mktcap.notna() & (mktcap > 0)
            if has_mktcap.any():
                mask &= ~has_mktcap | ((mktcap_yi >= 60.0) & (mktcap_yi <= 300.0))
                logger.info(
                    f"[EOD-RT] Market cap filter applied via '{mktcap_col}' "
                    f"({has_mktcap.sum()} stocks had data)"
                )
            else:
                logger.info("[EOD-RT] Market cap data unavailable, skipping filter")
        else:
            logger.info("[EOD-RT] No market cap column, skipping filter")

        # Rule 7: VWAP — price must be >= VWAP (applies both intraday and post-close)
        # ts.get_realtime_quotes: amount is in 元, volume is in 股 => VWAP = amount / volume (元/股), same unit as price
        if "volume" in df.columns and "amount" in df.columns:
            rt_volume_vwap = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            rt_amount_vwap = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
            vwap_valid = (rt_volume_vwap > 0) & (rt_amount_vwap > 0)
            vwap = rt_amount_vwap / rt_volume_vwap.replace(0, float("nan"))
            # Only filter rows where VWAP can be computed; keep rows without data
            vwap_mask = ~vwap_valid | (df["price"] >= vwap)
            mask &= vwap_mask
            logger.info(f"[EOD-RT] VWAP filter applied ({vwap_valid.sum()} stocks had data)")
        else:
            logger.info("[EOD-RT] VWAP skipped (no volume/amount columns)")

        df_filtered = df[mask].copy()
        logger.info(f"[EOD-RT] After all filters: {len(df_filtered)} stocks")

        if df_filtered.empty:
            return []

        # Rule 5: volume ratio 2-5x — only after market close (15:00+)
        # During trading hours, intraday cumulative volume vs historical full-day volume
        # creates a mismatch (off by tens of times), so skip.
        now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
        is_after_close = now_sh.hour >= 15

        # Store computed volume ratios for later use in ScreenedStock
        vol_ratio_map: Dict[str, float] = {}

        if is_after_close and "volume" in df_filtered.columns:
            logger.info("[EOD-RT] Post-close mode: computing volume ratio from 5-day avg")
            rt_vol = pd.to_numeric(df_filtered["volume"], errors="coerce")
            keep_idx: list = []
            for idx, row in df_filtered.iterrows():
                code = str(row.get("code", ""))
                today_vol = float(rt_vol.get(idx, 0) or 0)
                if today_vol <= 0:
                    keep_idx.append(idx)  # no data, keep conservatively
                    continue
                try:
                    df_daily, _src = self._data_manager.get_daily_data(code, days=6)
                    if df_daily is None or len(df_daily) < 2:
                        keep_idx.append(idx)
                        continue
                    vol_col = self._first_col(df_daily, "vol", "volume", "成交量")
                    if vol_col is None:
                        keep_idx.append(idx)
                        continue
                    # Exclude the most recent row (today) to get previous 5 days
                    hist_vol = pd.to_numeric(df_daily[vol_col], errors="coerce").iloc[:-1]
                    avg_5d = hist_vol.mean()
                    if avg_5d <= 0 or pd.isna(avg_5d):
                        keep_idx.append(idx)
                        continue
                    vol_ratio = today_vol / avg_5d
                    vol_ratio_map[code] = vol_ratio
                    if 2.5 <= vol_ratio <= 4.0:
                        keep_idx.append(idx)
                    else:
                        logger.debug(f"[EOD-RT] {code} vol_ratio={vol_ratio:.2f} out of [2.5,4], dropped")
                except Exception as e:
                    logger.debug(f"[EOD-RT] vol_ratio calc error for {code}: {e}")
                    keep_idx.append(idx)  # on error, keep conservatively
            before_cnt = len(df_filtered)
            df_filtered = df_filtered.loc[keep_idx]
            logger.info(
                f"[EOD-RT] Volume ratio filter: {before_cnt} -> {len(df_filtered)} stocks"
            )
        else:
            logger.info(
                f"[EOD-RT] Volume ratio skipped ({'intraday' if not is_after_close else 'no volume data'})"
            )

        if df_filtered.empty:
            return []

        # Step 5: Compute 60d change for candidate stocks
        change_60d_map: Dict[str, float] = {}
        tushare_api = self._get_tushare_api()
        if tushare_api:
            try:
                candidate_codes = [str(row.get("code", "")) for _, row in df_filtered.iterrows()]
                # Fetch close prices from 60 trading days ago for all candidates
                from src.core.trading_calendar import get_last_trading_day as _get_ltd_60

                now_sh_60 = datetime.now(ZoneInfo("Asia/Shanghai"))
                td_60 = _get_ltd_60("cn", now_sh_60.date())
                if td_60 is not None:
                    td_str = td_60.strftime("%Y%m%d")
                    start_60 = (pd.Timestamp(td_str) - pd.Timedelta(days=120)).strftime("%Y%m%d")
                    df_cal = tushare_api.trade_cal(exchange="SSE", start_date=start_60, end_date=td_str)
                    if df_cal is not None and not df_cal.empty:
                        df_cal.columns = [c.lower() for c in df_cal.columns]
                        df_cal = df_cal[df_cal["is_open"] == 1].sort_values("cal_date")
                        dates_60 = df_cal["cal_date"].tolist()
                        if td_str in dates_60:
                            idx_60 = dates_60.index(td_str)
                        else:
                            idx_60 = len(dates_60) - 1
                        if idx_60 >= 60:
                            date_60d_ago = dates_60[idx_60 - 60]
                            df_60d = tushare_api.daily(trade_date=date_60d_ago)
                            if df_60d is not None and not df_60d.empty:
                                df_60d.columns = [c.lower() for c in df_60d.columns]
                                close_60d_lookup = dict(
                                    zip(
                                        df_60d["ts_code"].str.split(".").str[0],
                                        pd.to_numeric(df_60d["close"], errors="coerce"),
                                    )
                                )
                                for _, row in df_filtered.iterrows():
                                    code = str(row.get("code", ""))
                                    cur_price = float(row.get("price", 0) or 0)
                                    old_close = close_60d_lookup.get(code)
                                    if old_close and old_close > 0 and cur_price > 0:
                                        change_60d_map[code] = (cur_price - old_close) / old_close * 100
                                logger.info(
                                    f"[EOD-RT] 60d change computed for {len(change_60d_map)} candidates"
                                )
                        else:
                            logger.debug("[EOD-RT] Not enough trading days for 60d change")
            except Exception as e:
                logger.warning(f"[EOD-RT] Failed to compute 60d change: {e}")

        # Step 5b: Build ScreenedStock list
        candidates_pre: list = []
        for _, row in df_filtered.iterrows():
            code = str(row.get("code", ""))
            name = str(row.get("name", ""))
            price = float(row.get("price", 0) or 0)
            chg = float(row.get("calc_change_pct", 0) or 0)
            # Prefer daily_basic turnover_rate; fallback to realtime turnover
            raw_tr = row.get("turnover_rate") if "turnover_rate" in df_filtered.columns else None
            if raw_tr is None or pd.isna(raw_tr):
                raw_tr = row.get("turnover", 0)
            turnover_val = float(pd.to_numeric(raw_tr, errors="coerce") or 0)
            amount_val = float(pd.to_numeric(row.get("amount", 0), errors="coerce") or 0)
            # Market cap in 亿: prefer total_mv_yi (already 亿) from daily_basic
            raw_mc = row.get("total_mv_yi") if "total_mv_yi" in df_filtered.columns else None
            mktcap_val = float(pd.to_numeric(raw_mc, errors="coerce") or 0) if raw_mc is not None else 0.0

            candidates_pre.append(ScreenedStock(
                code=code, name=name, price=price,
                change_pct=chg,
                volume_ratio=vol_ratio_map.get(code, 0.0),
                turnover_rate=turnover_val,
                pe=float(pd.to_numeric(row.get("pe_ttm"), errors="coerce") or 0)
                if "pe_ttm" in df_filtered.columns else 0.0,
                pb=float(pd.to_numeric(row.get("pb"), errors="coerce") or 0)
                if "pb" in df_filtered.columns else 0.0,
                market_cap=mktcap_val,
                amount=amount_val / 1e8 if amount_val else 0.0,
                change_pct_60d=change_60d_map.get(code, 0.0),
                score=0.0,
                strategies=["eod_buyback"],
            ))

        # Step 5c: Prepare sector strength data for scoring bonus
        _sector_top20_codes: Set[str] = set()
        _sector_top50_codes: Set[str] = set()
        try:
            from src.services.sector_strength_service import SectorStrengthService
            _eod_sector_svc = SectorStrengthService()
            # Reuse cached sector data; no new network requests if cache is warm
            _sector_top20_codes = _eod_sector_svc.get_strong_sector_codes(top_pct=0.20)
            _sector_top50_codes = _eod_sector_svc.get_strong_sector_codes(top_pct=0.50)
            logger.info(
                "[EOD-RT] Sector strength loaded: top20%%=%d codes, top50%%=%d codes",
                len(_sector_top20_codes), len(_sector_top50_codes),
            )
        except Exception as e:
            logger.warning("[EOD-RT] Sector strength data unavailable, skipping bonus: %s", e)

        # Step 5d: Filter consecutive up days (chasing risk guard)
        from src.services.picker_strategies import EOD_BUYBACK_PARAMS as _eod_params
        max_consec = _eod_params.max_consecutive_up_days
        candidates_pre = self._filter_consecutive_up_days(candidates_pre, max_up_days=max_consec)
        logger.info(f"[EOD-RT] After consecutive-up-days filter (max={max_consec}): {len(candidates_pre)} candidates")

        # Step 6: Score candidates with today limit-up signal detection
        logger.info(f"[EOD-RT] Scoring {len(candidates_pre)} candidates with today limit-up signal...")
        final: list = []
        for s in candidates_pre:
            try:
                base_score = (
                    10.0
                    + min(s.change_pct, 6.0) * 2
                    + (5.0 if 100 <= s.market_cap <= 200 else 0.0)
                )
                # -- Intra-band momentum strength (replaces limit-up signal) --
                # Within the 3-6% band, higher change indicates stronger EOD buying pressure
                today_change = s.change_pct
                if today_change >= 5.5:
                    base_score += 15.0  # Upper band: strong EOD momentum
                    logger.debug("[EOD] %s strong band momentum (%.1f%%), +15 pts", s.code, today_change)
                elif today_change >= 4.5:
                    base_score += 8.0   # Mid band: moderate EOD momentum
                    logger.debug("[EOD] %s moderate band momentum (%.1f%%), +8 pts", s.code, today_change)
                # Below 4.5%: no bonus (weak end-of-day buying)

                # -- Sector strength bonus --
                # Strong sector stocks have higher next-day continuation probability
                if _sector_top20_codes or _sector_top50_codes:
                    if s.code in _sector_top20_codes:
                        base_score += 10.0
                        logger.debug("[EOD] %s sector top 20%%, +10 pts", s.code)
                    elif s.code in _sector_top50_codes:
                        base_score += 5.0
                        logger.debug("[EOD] %s sector top 50%%, +5 pts", s.code)
                    else:
                        base_score -= 10.0
                        logger.debug("[EOD] %s sector bottom 50%%, -10 pts", s.code)

                s.score = base_score
                final.append(s)
            except Exception as e:
                logger.debug(f"[EOD-RT] scoring error for {s.code}: {e}")

        logger.info(f"[EOD-RT] Final eod_buyback candidates: {len(final)}")

        # Deduplicate by code, keeping the first occurrence
        seen_codes: set = set()
        deduped: list = []
        for s in final:
            if s.code not in seen_codes:
                seen_codes.add(s.code)
                deduped.append(s)
        if len(deduped) < len(final):
            logger.info(f"[EOD-RT] Deduplicated: {len(final)} -> {len(deduped)} candidates")
        return deduped

    def _has_recent_limit_up_check(self, code: str, days: int = 20) -> bool:
        """Check if stock had limit-up within recent N trading days.

        NOTE: Currently unused. Retained for potential future strategies.
        The eod_buyback strategy now uses intra-band momentum scoring instead.
        """
        try:
            if not self._data_manager:
                return False
            df, _src = self._data_manager.get_daily_data(code, days=days)
            if df is None or df.empty:
                return False
            limit_pct = LIMIT_UP_PCT_KC_CY if is_kc_cy_stock(code) else LIMIT_UP_PCT_MAIN
            chg_col = self._first_col(df, "pct_chg", "涨跌幅", "change_pct")
            if chg_col is None:
                return False
            pct = pd.to_numeric(df[chg_col], errors="coerce")
            return bool((pct >= limit_pct).any())
        except Exception as e:
            logger.debug(f"[EOD-RT] _has_recent_limit_up_check error for {code}: {e}")
            return False

    @staticmethod
    def _is_leader_candidate(s: "ScreenedStock") -> bool:
        """Check if stock qualifies for leader bias exemption (板块龙头+量能确认)."""
        return (
            s.change_pct_60d > LEADER_CHANGE_60D_MIN
            and LEADER_CHANGE_PCT_LO <= s.change_pct <= LEADER_CHANGE_PCT_HI
            and s.volume_ratio > LEADER_VOLUME_RATIO_MIN
            and LEADER_TURNOVER_LO <= s.turnover_rate <= LEADER_TURNOVER_HI
        )

    def _filter_by_bias(
        self,
        candidates: List[ScreenedStock],
        max_bias_pct: float = PICKER_MAX_BIAS_PCT,
        leader_bias_exempt_pct: float = 0.0,
    ) -> List[ScreenedStock]:
        """Filter out stocks with MA5 bias > max_bias_pct (严进策略).
        When leader_bias_exempt_pct > 0, allow bias up to that value for leader candidates."""
        if not self._data_manager or not candidates:
            return candidates
        end_date = self._as_of_date
        requests = [(s.code, None, end_date, 10) for s in candidates]
        batch = self._fetch_daily_batch(requests)
        filtered = []
        for s in candidates:
            df_daily, _ = batch.get((s.code, "", end_date or "", 10), (None, ""))
            if df_daily is None or len(df_daily) < 5:
                filtered.append(s)
                continue
            close_col = self._first_col(df_daily, "close", "收盘")
            if close_col is None:
                filtered.append(s)
                continue
            date_col = self._first_col(df_daily, "date", "日期") or df_daily.columns[0]
            df_daily = df_daily.sort_values(date_col).tail(5)
            ma5 = float(df_daily[close_col].mean())
            if ma5 <= 0:
                filtered.append(s)
                continue
            bias_pct = (s.price - ma5) / ma5 * 100
            if bias_pct <= max_bias_pct:
                filtered.append(s)
            elif (
                leader_bias_exempt_pct > 0
                and bias_pct <= leader_bias_exempt_pct
                and self._is_leader_candidate(s)
            ):
                filtered.append(s)
                logger.debug(f"[Screener] Leader exempt {s.code} bias={bias_pct:.1f}%")
            else:
                logger.debug(f"[Screener] Exclude {s.code} bias={bias_pct:.1f}% > {max_bias_pct}%")
        return filtered

    def _filter_limit_up_streak(
        self,
        candidates: List[ScreenedStock],
        days: int = 5,
        min_limit_up_days: int = LIMIT_UP_DAYS_THRESHOLD,
    ) -> List[ScreenedStock]:
        """Exclude stocks with 2+ limit-up days in last 5 days (连板/妖股 risk).
        Uses board-specific threshold: main 10%, ChiNext/STAR 20%.
        """
        if not self._data_manager or not candidates:
            return candidates
        end_date = self._as_of_date
        requests = [(s.code, None, end_date, days + 5) for s in candidates]
        batch = self._fetch_daily_batch(requests)
        filtered = []
        for s in candidates:
            df_daily, _ = batch.get((s.code, "", end_date or "", days + 5), (None, ""))
            if df_daily is None or len(df_daily) < days:
                filtered.append(s)
                continue
            pct_col = self._first_col(df_daily, "pct_chg", "涨跌幅")
            if pct_col is None:
                filtered.append(s)
                continue
            pct_threshold = LIMIT_UP_PCT_KC_CY if is_kc_cy_stock(s.code) else LIMIT_UP_PCT_MAIN
            date_col = self._first_col(df_daily, "date", "日期") or df_daily.columns[0]
            df_daily = df_daily.sort_values(date_col).tail(days)
            pct = pd.to_numeric(df_daily[pct_col], errors="coerce").fillna(0)
            limit_up_count = int((pct >= pct_threshold).sum())
            if limit_up_count >= min_limit_up_days:
                logger.debug(
                    f"[Screener] Exclude {s.code} limit-up streak: {limit_up_count} days in last {days}"
                )
            else:
                filtered.append(s)
        return filtered

    def _filter_consecutive_up_days(
        self,
        candidates: List[ScreenedStock],
        days: int = 5,
        max_up_days: Optional[int] = None,
    ) -> List[ScreenedStock]:
        """Exclude stocks with too many consecutive up days (avoid buying at streak end)."""
        if not self._data_manager or not candidates:
            return candidates

        if max_up_days is None:
            max_up_days = PickerModeParams.for_mode(self._picker_mode).max_consecutive_up_days
        end_date = self._as_of_date
        requests = [(s.code, None, end_date, days + 5) for s in candidates]
        batch = self._fetch_daily_batch(requests)
        filtered = []
        for s in candidates:
            df_daily, _ = batch.get((s.code, "", end_date or "", days + 5), (None, ""))
            if df_daily is None or len(df_daily) < days:
                filtered.append(s)
                continue
            pct_col = self._first_col(df_daily, "pct_chg", "涨跌幅")
            if pct_col is None:
                filtered.append(s)
                continue
            date_col = self._first_col(df_daily, "date", "日期") or df_daily.columns[0]
            df_daily = df_daily.sort_values(date_col).tail(days)
            pct_series = pd.to_numeric(df_daily[pct_col], errors="coerce").fillna(0).values

            consecutive_up = 0
            for pct in reversed(pct_series):
                if pct > 0:
                    consecutive_up += 1
                else:
                    break

            if consecutive_up > max_up_days:
                logger.debug(
                    f"[Screener] Exclude {s.code}: {consecutive_up} consecutive up days > max {max_up_days}"
                )
            else:
                filtered.append(s)
        return filtered

    def _filter_healthy_pullback(
        self,
        candidates: List[ScreenedStock],
        lookback_days: int = 20,
        params: Optional[Any] = None,
        strategy_id: Optional[str] = None,
    ) -> List[ScreenedStock]:
        """Filter for healthy pullback confirmation to distinguish from trend reversal.

        Checks (strategy-specific when params provided):
        1. Volume shrink: volume_ratio < 1.0 on pullback day (缩量回调)
        2. MA bullish alignment: MA5 > MA10 > MA20 (均线多头排列)
        3. Retracement limit: pullback < X% of prior 10d rally (回调幅度限制)
        4. Min distance from 20d high: must be >=X% below high (距高点距离)
        5. Price above MA20: reject if below MA20 (下跌通道排除)
        6. Near MA10 support: price within X% above MA10 (支撑位确认)
        7. [breakout] Long upper shadow filter: fake breakout signal (假突破过滤)
        8. [breakout] Resistance breakout confirmation: close >= 20d high (阻力位突破确认)
        """
        if not self._data_manager or not candidates:
            return candidates

        mode_params = params if params is not None else PickerModeParams.for_mode(self._picker_mode)
        end_date = self._as_of_date

        # Batch fetch daily data for all candidates
        requests = [(s.code, None, end_date, lookback_days + 5) for s in candidates]
        batch = self._fetch_daily_batch(requests)

        filtered = []
        for s in candidates:
            df_daily, _ = batch.get((s.code, "", end_date or "", lookback_days + 5), (None, ""))
            if df_daily is None or len(df_daily) < 10:
                filtered.append(s)  # Keep if no data
                continue

            close_col = self._first_col(df_daily, "close", "收盘", "最新价")
            high_col = self._first_col(df_daily, "high", "最高")
            date_col = self._first_col(df_daily, "date", "日期") or df_daily.columns[0]
            if close_col is None:
                filtered.append(s)
                continue

            df_daily = df_daily.sort_values(date_col).tail(lookback_days).reset_index(drop=True)
            close_series = pd.to_numeric(df_daily[close_col], errors="coerce").fillna(0)

            # --- Breakout-specific checks (7 & 8): fake breakout filter ---
            if strategy_id == "breakout":
                open_col = self._first_col(df_daily, "open", "开盘")
                # Check 7: Long upper shadow filter (fake breakout signal)
                max_shadow_ratio = getattr(mode_params, 'max_upper_shadow_ratio', 2.0)
                if high_col and open_col and close_col:
                    latest = df_daily.iloc[-1]
                    h = float(pd.to_numeric(latest.get(high_col, 0), errors="coerce") or 0)
                    c = float(pd.to_numeric(latest.get(close_col, 0), errors="coerce") or 0)
                    o = float(pd.to_numeric(latest.get(open_col, 0), errors="coerce") or 0)
                    body = abs(c - o)
                    upper_shadow = h - max(c, o)
                    if body > 0 and upper_shadow / body > max_shadow_ratio:
                        logger.debug(
                            "[Screener] %s excluded: long upper shadow (ratio=%.1f > %.1f), fake breakout",
                            s.code, upper_shadow / body, max_shadow_ratio,
                        )
                        continue

                # Check 8: Resistance breakout confirmation (close must >= N-day high excluding today)
                bk_lookback = getattr(mode_params, 'breakout_lookback_days', 20)
                if high_col and len(df_daily) >= 2:
                    high_series_bk = pd.to_numeric(df_daily[high_col], errors="coerce").fillna(0)
                    # Use up to bk_lookback days, but always exclude today (last row)
                    window = min(bk_lookback, len(df_daily) - 1)
                    # Lookback high excluding today
                    high_nd = float(high_series_bk.iloc[-(window + 1):-1].max())
                    current_close = s.price
                    if high_nd > 0 and current_close < high_nd * 0.995:
                        logger.debug(
                            "[Screener] %s excluded: close %.2f below %dd high %.2f, not a true breakout",
                            s.code, current_close, bk_lookback, high_nd,
                        )
                        continue

            # Check 1: Volume shrink or mild expansion (1.0-1.3 acceptable for healthy pullback)
            VOLUME_SHRINK_LIMIT = 1.3  # Allow mild expansion as valid pullback signal
            if mode_params.require_volume_shrink and s.change_pct <= 0 and s.volume_ratio >= VOLUME_SHRINK_LIMIT:
                logger.debug(
                    "[Screener] Exclude %s %s: volume_ratio %.2f >= %.1f on pullback day",
                    s.code, s.name, s.volume_ratio, VOLUME_SHRINK_LIMIT,
                )
                continue

            # Check 2: MA bullish alignment with tolerance for convergence
            MA_TOLERANCE = 0.005  # 0.5% tolerance for MA convergence (accumulation signal)
            if mode_params.require_ma_bullish and len(close_series) >= 20:
                ma5 = float(close_series.tail(5).mean())
                ma10 = float(close_series.tail(10).mean())
                ma20 = float(close_series.tail(20).mean())
                if not (ma5 >= ma10 * (1 - MA_TOLERANCE) and ma10 >= ma20 * (1 - MA_TOLERANCE)):
                    logger.debug(
                        "[Screener] Exclude %s %s: MA not bullish "
                        "(MA5=%.2f, MA10=%.2f, MA20=%.2f, gap=%.2f%%)",
                        s.code, s.name, ma5, ma10, ma20,
                        (ma5 - ma10) / ma10 * 100 if ma10 > 0 else 0,
                    )
                    continue

            # Check 3: Retracement limit
            if len(close_series) >= 10 and high_col:
                high_series = pd.to_numeric(df_daily[high_col], errors="coerce").fillna(0)
                low_col = self._first_col(df_daily, "low", "最低")
                if low_col:
                    low_series = pd.to_numeric(df_daily[low_col], errors="coerce").fillna(0)
                else:
                    low_series = close_series  # Fallback to close if no low column
                # Prior 10d high and low
                recent_high = float(high_series.tail(10).max())
                recent_low = float(low_series.tail(10).min())
                rally = recent_high - recent_low
                if rally > 0.01 and recent_high > 0:  # Avoid near-zero division
                    current_pullback = recent_high - s.price
                    # Only check if actually pulled back (current_pullback > 0)
                    if current_pullback > 0:
                        retracement = current_pullback / rally
                        if retracement > mode_params.max_retracement_pct:
                            logger.debug(
                                f"[Screener] Exclude {s.code}: retracement {retracement:.1%} > max {mode_params.max_retracement_pct:.1%}"
                            )
                            continue

            # Check 4: minimum distance from 20-day high
            min_pb = getattr(mode_params, 'min_pullback_from_high_pct', 0.0)
            if min_pb > 0 and high_col and len(close_series) >= 20:
                high_series_4 = pd.to_numeric(df_daily[high_col], errors="coerce").fillna(0)
                if len(high_series_4) >= 20:
                    high_20d = float(high_series_4.tail(20).max())
                    if high_20d > 0:
                        distance_from_high_pct = (high_20d - s.price) / high_20d * 100
                        if distance_from_high_pct < min_pb:
                            logger.debug(
                                "[Screener] Exclude %s %s: only %.1f%% below 20d high (%.2f), "
                                "need >= %.1f%% pullback",
                                s.code, s.name, distance_from_high_pct, high_20d, min_pb,
                            )
                            continue

            # Check 5: price must be above MA20 (below MA20 = entering downtrend)
            req_above_ma20 = getattr(mode_params, 'require_price_above_ma20', False)
            if req_above_ma20 and len(close_series) >= 20:
                ma20 = float(close_series.tail(20).mean())
                if ma20 > 0 and s.price < ma20:
                    logger.debug(
                        "[Screener] Exclude %s %s: price %.2f below MA20 %.2f, "
                        "potential downtrend",
                        s.code, s.name, s.price, ma20,
                    )
                    continue

            # Check 6: price must be near MA10 support (not floating above)
            max_above_ma10 = getattr(mode_params, 'max_distance_above_ma10_pct', 0.0)
            if max_above_ma10 > 0 and len(close_series) >= 10:
                ma10 = float(close_series.tail(10).mean())
                if ma10 > 0:
                    dist_above_ma10 = (s.price - ma10) / ma10 * 100
                    if dist_above_ma10 > max_above_ma10:
                        logger.debug(
                            "[Screener] Exclude %s %s: %.1f%% above MA10 (%.2f), "
                            "not near support yet (max %.1f%%)",
                            s.code, s.name, dist_above_ma10, ma10, max_above_ma10,
                        )
                        continue

            filtered.append(s)

        return filtered

    def _filter_b_wave_risk(
        self,
        candidates: List[ScreenedStock],
        lookback_days: int = B_WAVE_LOOKBACK_DAYS,
    ) -> List[ScreenedStock]:
        """Exclude stocks likely in B-wave bounce (fake recovery before C-wave down).
        Pattern: A-wave drop >= 5%, then bounce 35-65% of the drop, low 2-14 days ago.
        """
        if not self._data_manager or not candidates:
            return candidates
        end_date = self._as_of_date
        requests = [(s.code, None, end_date, lookback_days + 5) for s in candidates]
        batch = self._fetch_daily_batch(requests)
        filtered = []
        for s in candidates:
            df_daily, _ = batch.get((s.code, "", end_date or "", lookback_days + 5), (None, ""))
            if df_daily is None or len(df_daily) < lookback_days:
                filtered.append(s)
                continue
            close_col = self._first_col(df_daily, "close", "收盘", "最新价")
            if close_col is None:
                filtered.append(s)
                continue
            date_col = self._first_col(df_daily, "date", "日期") or df_daily.columns[0]
            df_daily = df_daily.sort_values(date_col).tail(lookback_days).reset_index(drop=True)
            ser = pd.to_numeric(df_daily[close_col], errors="coerce").fillna(0)
            if len(ser) < lookback_days:
                filtered.append(s)
                continue
            idx_max = int(ser.idxmax())
            idx_min = int(ser.idxmin())
            high_val = float(ser.iloc[idx_max])
            low_val = float(ser.iloc[idx_min])
            if high_val <= 0 or low_val <= 0:
                filtered.append(s)
                continue

            if idx_min <= idx_max:
                filtered.append(s)
                continue
            drop_pct = (high_val - low_val) / high_val * 100
            if drop_pct < B_WAVE_MIN_DROOP_PCT:
                filtered.append(s)
                continue

            current = s.price
            rebound_pct = (current - low_val) / low_val * 100 if low_val > 0 else 0
            retracement = rebound_pct / drop_pct if drop_pct > 0 else 0
            days_since_low = (len(ser) - 1) - idx_min

            if (
                B_WAVE_RETRACE_LO <= retracement <= B_WAVE_RETRACE_HI
                and B_WAVE_LOW_DAYS_AGO_MIN <= days_since_low <= B_WAVE_LOW_DAYS_AGO_MAX
            ):
                logger.debug(
                    f"[Screener] Exclude {s.code} B-wave risk: drop={drop_pct:.1f}%, "
                    f"retrace={retracement:.0%}, low {days_since_low}d ago"
                )
            else:
                filtered.append(s)
        return filtered

    _UA_LIST = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    ]

    def _fetch_spot_data(self, trade_date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """Fetch full A-share data for Phase-1 fast screening.
        Priority: Tushare(daily, fast bulk scan) → AkShare(spot, fallback) → efinance(quotes, last resort).
        Realtime precision is guaranteed by Phase-2 _filter_by_realtime(force_refresh=True).
        When trade_date (YYYYMMDD) is provided, only Tushare is used (historical mode)."""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        # Historical mode: only Tushare supports dated queries
        if trade_date:
            df = self._try_tushare(trade_date=trade_date)
            if df is not None and not df.empty:
                logger.info(f"[Screener] Historical mode — Using Tushare data: {len(df)} stocks")
                return df
            logger.warning("[Screener] Historical mode — Tushare returned empty, no data available")
            return None

        # --- 1. Tushare daily — fast bulk scan for Phase-1 initial screening ---
        # Tushare daily API returns closing data quickly without full-market realtime overhead.
        # Realtime accuracy (turnover_rate, market_cap) is ensured in Phase-2 _filter_by_realtime.
        logger.info(
            "[Screener] Trying Tushare daily (priority 1/3) — "
            "fast bulk scan for Phase-1; realtime precision deferred to Phase-2"
        )
        df = self._try_tushare(trade_date=None)
        if df is not None and not df.empty:
            logger.info(f"[Screener] Using Tushare daily data: {len(df)} stocks (Phase-1 fast path)")
            return df
        logger.warning("[Screener] Tushare daily unavailable, trying AkShare realtime fallback")

        # --- 2. AkShare realtime (stock_zh_a_spot_em) with hard wall-clock timeout ---
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

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="screener") as pool:
            logger.info(
                f"[Screener] Trying AkShare realtime spot (wall timeout={self._spot_timeout}s) — "
                "priority 2/3 fallback"
            )
            t0 = time.time()
            try:
                fut = pool.submit(_try_akshare)
                df = fut.result(timeout=self._spot_timeout)
                logger.info(
                    f"[Screener] Using AkShare realtime data: {len(df)} stocks in {time.time()-t0:.1f}s"
                )
                return df
            except FuturesTimeout:
                logger.warning(f"[Screener] AkShare hard-timeout after {self._spot_timeout}s, trying next source")
                fut.cancel()
            except Exception as e:
                logger.warning(f"[Screener] AkShare failed: {e}, trying next source")

        # --- 3. efinance realtime (get_realtime_quotes) — last resort ---
        def _try_efinance() -> pd.DataFrame:
            import efinance as ef
            return ef.stock.get_realtime_quotes()

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="screener") as pool:
            logger.info(
                f"[Screener] Trying efinance realtime quotes (wall timeout={self._spot_timeout}s) — "
                "priority 3/3 last resort"
            )
            t0 = time.time()
            try:
                fut = pool.submit(_try_efinance)
                df = fut.result(timeout=self._spot_timeout)
                logger.info(
                    f"[Screener] Using efinance realtime data: {len(df)} stocks in {time.time()-t0:.1f}s"
                )
                return self._normalize_efinance_df(df)
            except FuturesTimeout:
                logger.warning(f"[Screener] efinance hard-timeout after {self._spot_timeout}s")
                fut.cancel()
            except Exception as e:
                logger.warning(f"[Screener] efinance failed: {e}")

        logger.error("[Screener] All data sources exhausted — no spot data available")
        return None

    def _try_tushare(self, trade_date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """Fetch full-market daily data via Tushare Pro (daily + daily_basic + stock_basic).
        When trade_date (YYYYMMDD) is provided, use it for historical screening."""
        tushare_api = self._get_tushare_api()
        if tushare_api is None:
            logger.info("[Screener] Tushare API not available (TUSHARE_TOKEN unset or init failed)")
            return None

        try:
            from zoneinfo import ZoneInfo
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
                df_names = tushare_api.stock_basic(fields="ts_code,symbol,name")
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
            # volume_ratio may be None for some Tushare tiers; default to 1.0 (neutral)
            vr = pd.to_numeric(df_daily.get("volume_ratio", pd.NA), errors="coerce")
            df_daily["量比"] = vr.fillna(1.0)
            df_daily["换手率"] = df_daily.get("turnover_rate", pd.NA)
            df_daily["成交额"] = df_daily.get("amount", 0).astype(float) * 1000  # 千元→元
            df_daily["市净率"] = df_daily.get("pb", pd.NA)
            df_daily["总市值"] = df_daily.get("total_mv", 0).astype(float) * 1e4  # 万元→元

            # Compute 60-day change (Tushare daily does not include it; AkShare spot does)
            df_daily = self._add_tushare_60d_change(df_daily, tushare_api, trade_date)

            elapsed = time.time() - t0
            logger.info(f"[Screener] Tushare returned {len(df_daily)} stocks in {elapsed:.1f}s")
            return df_daily

        except Exception as e:
            logger.warning(f"[Screener] Tushare failed: {e}")
            return None

    def _add_tushare_60d_change(
        self, df_daily: pd.DataFrame, tushare_api, trade_date: str
    ) -> pd.DataFrame:
        """Add 60日涨跌幅 for Tushare data by fetching close from 60 trading days ago."""
        try:
            # Get trading calendar to find date 60 trading days before trade_date
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

    def _get_tushare_api(self):
        """Get Tushare API instance from data_manager or create one."""
        return get_tushare_api(self._data_manager)

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

    def _filter_basic(self, df: pd.DataFrame, pe_max: Optional[float] = None) -> pd.DataFrame:
        """Layer 1: Remove ST, new listings, ETFs, and unprofitable (PE filter)."""
        pe_max = pe_max if pe_max is not None else PickerModeParams.for_mode(self._picker_mode).pe_max
        return self._filter_basic_impl(df, pe_max)

    def _filter_basic_for_strategies(self, df: pd.DataFrame) -> pd.DataFrame:
        """Basic filter for multi-strategy (shared pe_max=100)."""
        return self._filter_basic_impl(df, pe_max=100.0)

    def _filter_basic_impl(self, df: pd.DataFrame, pe_max: float) -> pd.DataFrame:
        """Shared implementation for basic filter."""
        # Exclude by name keywords
        name_col = "名称"
        if name_col in df.columns:
            mask = pd.Series(True, index=df.index)
            for kw in self._EXCLUDE_NAME_KEYWORDS:
                mask &= ~df[name_col].str.contains(kw, na=False, regex=False)
            df = df[mask]

        # Exclude ETF codes
        code_col = "代码"
        if code_col in df.columns:
            df = df[~df[code_col].str[:2].isin(self._ETF_PREFIXES)]

        # PE filter: exclude PE >= pe_max; when allow_loss=False, also exclude PE<=0 (unprofitable)
        if "市盈率-动态" in df.columns:
            pe = pd.to_numeric(df["市盈率-动态"], errors="coerce")
            if self._allow_loss:
                df = df[pe < pe_max]
            else:
                df = df[(pe > 0) & (pe < pe_max)]

        return df

    def _filter_momentum(self, df: pd.DataFrame) -> pd.DataFrame:
        """Layer 2: Pullback entry filter — buy near support, not chase highs.

        Strategy shift: Instead of requiring positive change (追涨), we now
        prefer stocks that are consolidating or pulling back to support.
        Mode-specific daily change range controls entry aggressiveness.
        """
        mode_params = PickerModeParams.for_mode(self._picker_mode)

        # Daily change within mode-specific range (pullback strategy)
        if "涨跌幅" in df.columns:
            pct = pd.to_numeric(df["涨跌幅"], errors="coerce")
            # Mode-specific range: defensive [-2,2], balanced [-1,4], offensive [0,6]
            df = df[(pct >= mode_params.daily_change_min) & (pct <= mode_params.daily_change_max)]
            logger.debug(
                f"[Screener] Momentum filter: daily change in [{mode_params.daily_change_min}, {mode_params.daily_change_max}]%"
            )

        # 60-day change > 5% (clear medium-term uptrend — keep this requirement)
        if "60日涨跌幅" in df.columns:
            pct60 = pd.to_numeric(df["60日涨跌幅"], errors="coerce")
            df = df[pct60 > 5]

        return df

    def _filter_volume(self, df: pd.DataFrame) -> pd.DataFrame:
        """Layer 3: Volume activity — above-average volume, healthy turnover."""
        # Volume ratio > VOLUME_RATIO_MIN (ensure active interest, exclude cold stocks)
        if "量比" in df.columns:
            vr = pd.to_numeric(df["量比"], errors="coerce")
            df = df[vr > VOLUME_RATIO_MIN]

        # Turnover rate 1-15% (filter cold, reduce speculation)
        if "换手率" in df.columns:
            tr = pd.to_numeric(df["换手率"], errors="coerce")
            df = df[(tr > self._turnover_min) & (tr < self._turnover_max)]

        # Amount by market cap: <100亿 use 3000万, >=100亿 use 1亿
        if "成交额" in df.columns and "总市值" in df.columns:
            amt = pd.to_numeric(df["成交额"], errors="coerce")
            cap_yi = pd.to_numeric(df["总市值"], errors="coerce") / 1e8
            ok_small = (cap_yi < MARKET_CAP_TIER_YI) & (amt > AMOUNT_MIN_SMALL_CAP)
            ok_large = (cap_yi >= MARKET_CAP_TIER_YI) & (amt > AMOUNT_MIN_LARGE_CAP)
            df = df[ok_small | ok_large]
        elif "成交额" in df.columns:
            amt = pd.to_numeric(df["成交额"], errors="coerce")
            df = df[amt > AMOUNT_MIN_SMALL_CAP]

        return df

    def _score_trend(self, pct_60d: float) -> float:
        """Score trend strength. 5-30% linear; >30% decay to avoid end-of-trend buys."""
        if pct_60d <= 0:
            return 0.0
        if pct_60d <= TREND_DECAY_THRESHOLD_PCT:
            return min(pct_60d, 25.0)
        decay = 30 - (pct_60d - TREND_DECAY_THRESHOLD_PCT) * 0.5
        return max(0.0, decay)

    def _score_momentum(self, change_pct: float) -> float:
        """Score today's momentum — pullback strategy: lower change = better entry.

        New logic (回踩优先):
        - Change near 0% (pullback/consolidation): highest score
        - Change 0-3%: good entry, moderate score
        - Change 3-5%: acceptable, lower score
        - Change >5%: chase risk, penalty
        - Change <-2%: possible breakdown, penalty
        """
        if change_pct < -2:
            return -5.0  # Too weak, possible breakdown
        if -2 <= change_pct <= 1:
            return 20.0  # Best: pullback or slight dip, ideal entry
        if 1 < change_pct <= 3:
            return 15.0  # Good: small up, still reasonable entry
        if 3 < change_pct <= 5:
            return 8.0   # Acceptable: moderate chase
        # change_pct > 5: chase risk
        return max(0.0, 8.0 - (change_pct - 5) * 3)  # Penalty for chasing

    def _score_volume(self, vol_ratio: float) -> float:
        """Score volume confirmation. 1.0-3.0 ideal, >3.0 partial, >0.8 minimal."""
        if 1.0 <= vol_ratio <= 3.0:
            return 20.0
        if vol_ratio > 3.0:
            return 15.0
        return 10.0 if vol_ratio > 0.8 else 0.0

    def _score_turnover(self, turnover: float) -> float:
        """Score turnover health. 2-8% ideal, 1-2% or 8-15% partial."""
        if 2 <= turnover <= 8:
            return 10.0
        if 1 <= turnover < 2:
            return 5.0
        return 3.0 if 8 < turnover <= self._turnover_max else 0.0

    def _score_pe(self, pe: float) -> float:
        """Score valuation. Mode-specific PE ideal range."""
        p = PickerModeParams.for_mode(self._picker_mode)
        if p.pe_ideal_low < pe < p.pe_ideal_high:
            return 10.0
        if 5 < pe <= p.pe_ideal_low or p.pe_ideal_high <= pe < PE_SCORE_PARTIAL_MAX:
            return 5.0
        return 0.0

    def _score_and_rank(self, df: pd.DataFrame, top_n: int = 30) -> List[ScreenedStock]:
        """Score remaining stocks and return top N.

        Scoring philosophy: Prioritize trend strength and reasonable valuation
        over short-term volume spikes. This aligns with the analyzer's strict
        criteria (bias < 5%, bullish alignment).
        """
        records = []
        for _, row in df.iterrows():
            try:
                code = str(row.get("代码", ""))
                name = str(row.get("名称", ""))
                price = float(pd.to_numeric(row.get("最新价", 0), errors="coerce") or 0)
                change_pct = float(pd.to_numeric(row.get("涨跌幅", 0), errors="coerce") or 0)
                vol_ratio = float(pd.to_numeric(row.get("量比", 0), errors="coerce") or 0)
                turnover = float(pd.to_numeric(row.get("换手率", 0), errors="coerce") or 0)
                pe = float(pd.to_numeric(row.get("市盈率-动态", 0), errors="coerce") or 0)
                pb = float(pd.to_numeric(row.get("市净率", 0), errors="coerce") or 0)
                total_mv = float(pd.to_numeric(row.get("总市值", 0), errors="coerce") or 0)
                amount = float(pd.to_numeric(row.get("成交额", 0), errors="coerce") or 0)
                pct_60d = float(pd.to_numeric(row.get("60日涨跌幅", 0), errors="coerce") or 0)

                score = (
                    self._score_trend(pct_60d)
                    + self._score_momentum(change_pct)
                    + self._score_volume(vol_ratio)
                    + self._score_turnover(turnover)
                    + self._score_pe(pe)
                    + (5.0 if 50e8 < total_mv < 500e8 else 0.0)  # Mid-cap bonus
                )

                records.append(ScreenedStock(
                    code=code, name=name, price=price,
                    change_pct=change_pct, volume_ratio=vol_ratio,
                    turnover_rate=turnover, pe=pe, pb=pb,
                    market_cap=total_mv / 1e8,
                    amount=amount / 1e8,
                    change_pct_60d=pct_60d, score=score,
                ))
            except Exception:
                continue

        records.sort(key=lambda s: s.score, reverse=True)
        return records[:top_n]


# ── Main Service ─────────────────────────────────────────────────

class StockPickerService:
    """Two-stage stock picker: quantitative screening + AI selection."""

    SEARCH_QUERIES = [
        "今日A股市场热点 涨停分析",
        "A股主力资金流入 板块异动",
        "A股利好消息 政策催化",
    ]

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
            # ── Stage 1: Quantitative screening ──
            logger.info("[StockPicker] === Stage 1: Quantitative Screening ===")
            candidates, stats, candidates_per_strategy = self._screener.screen()
            result.screen_stats = stats
            result.screened_pool = candidates
            result.screened_pool_by_strategy = candidates_per_strategy

            if not candidates:
                logger.warning("[StockPicker] Screening returned 0 candidates")

            # ── Stage 1.5: Real-time filtering (new) ──
            if getattr(self.config, "picker_enable_realtime_filter", True):
                logger.info("[StockPicker] === Stage 1.5: Real-time Filtering ===")
                pre_count = len(candidates)
                candidates = self._filter_by_realtime(candidates)
                logger.info(f"[StockPicker] Real-time filtering: {pre_count} → {len(candidates)} candidates")
                result.screened_pool = candidates

            # ── Early exit: empty screened pool → skip LLM ──
            if not candidates:
                strategies = getattr(self._screener, "_picker_strategies", []) or []
                logger.warning(
                    "[StockPicker] Screened pool is empty after filtering, "
                    f"strategies={strategies}. Skipping LLM call and returning empty picks."
                )
                result.picks = []
                result.market_summary = "今日无符合量化筛选严格条件的股票，不进行 AI 选股。"
                result.success = True
                result.elapsed_seconds = time.time() - start
                return result

            # ── Stage 2: Gather market intel + AI selection ──
            logger.info("[StockPicker] === Stage 2: AI Selection ===")
            intel = self._gather_market_intel()
            chip_map = self._fetch_chip_for_candidates(candidates)
            prompt = self._build_prompt(intel, candidates, chip_map)

            try:
                llm_output = self._call_llm(prompt)
            except Exception as llm_exc:
                logger.warning(
                    "[StockPicker] LLM call raised exception, "
                    "degrading to quantitative results: %s", llm_exc,
                )
                llm_output = None

            if not llm_output:
                # Graceful degradation: return Stage-1 quantitative candidates
                # instead of failing the entire pipeline.
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
                        sector=s.sector,
                        reason=f"量化评分 {s.score:.1f}（换手率 {s.turnover_rate:.1f}%，"
                               f"涨跌 {s.change_pct:+.1f}%）",
                        catalyst="",
                        attention="medium",
                        risk_note="仅量化筛选，未经 AI 深度分析",
                    )
                    for s in candidates[:10]
                ]
                # Fill fields normally populated by LLM to prevent frontend errors
                result.sectors_to_watch = []
                result.risk_warning = "LLM 服务异常，仅返回量化筛选结果，请谨慎参考。"
                result.generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                result.success = True
                result.elapsed_seconds = time.time() - start
                return result

            self._parse_result(llm_output, result)

            # Append stale-data annotation to market_summary if indices were not fresh
            # Propagate stale flag to result for frontend warning UI
            if intel.get("indices_stale"):
                result.indices_stale = True
            if intel.get("indices_stale") and result.market_summary:
                result.market_summary = (
                    result.market_summary.rstrip()
                    + "（注：指数实时数据暂不可用，以上为定性判断）"
                )

            # ── Post-validation: ensure LLM picks are within screened pool ──
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
                        f"[StockPicker] Post-validation: {len(result.picks)} → {len(validated_picks)} picks"
                    )
                result.picks = validated_picks

            result.success = True

        except Exception as e:
            logger.error(f"[StockPicker] Error: {e}", exc_info=True)
            result.error = str(e)

        result.elapsed_seconds = time.time() - start
        return result

    def _filter_by_realtime(self, candidates: List[ScreenedStock]) -> List[ScreenedStock]:
        """Filter candidates by real-time market conditions (limit-up, volume spike, price range, etc).
        
        Uses strategy-specific params to enforce daily_change_max and volume_ratio_min limits.
        Queries realtime data in PARALLEL to avoid timeouts on large candidate pools.
        
        Args:
            candidates: List of ScreenedStock from quantitative screening
            
        Returns:
            Filtered list of candidates that pass real-time checks
        """
        if not candidates:
            return candidates

        from src.services.picker_strategies import get_strategy_params
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
        
        is_kccy = is_kc_cy_stock

        # Pre-warm realtime_list cache so parallel individual queries all hit cache
        # instead of 22 threads simultaneously triggering network requests.
        try:
            from data_provider.tushare_fetcher import TushareFetcher
            if self._data_manager:
                for fetcher in getattr(self._data_manager, '_fetchers', []):
                    if isinstance(fetcher, TushareFetcher) and fetcher.is_available():
                        fetcher._fetch_realtime_list()
                        logger.info(
                            "[Screener] realtime_list cache warmed up before parallel fetch "
                            "(%d candidates)", len(candidates),
                        )
                        break
        except Exception:
            pass  # Non-critical; individual queries will handle fallback

        # Parallel fetch of realtime quotes (max 10 workers, 2s timeout per stock)
        def _fetch_one_quote(code: str) -> tuple[str, Optional]:
            """Fetch realtime quote for one stock. Returns (code, quote)."""
            try:
                if not self._data_manager:
                    return (code, None)
                # Always force fresh realtime data for picker realtime validation phase
                try:
                    quote = self._data_manager.get_realtime_quote(code, force_refresh=True)
                except TypeError:
                    # Fallback if force_refresh param not yet supported in data_provider
                    quote = self._data_manager.get_realtime_quote(code)
                return (code, quote)
            except Exception as e:
                logger.debug(f"[RealTime] Failed to fetch {code}: {e}")
                return (code, None)
        
        # Fetch all realtime quotes in parallel
        realtime_quotes: Dict[str, Optional] = {}
        with ThreadPoolExecutor(max_workers=10, thread_name_prefix="realtime") as executor:
            futures = {executor.submit(_fetch_one_quote, stock.code): stock.code for stock in candidates}
            try:
                for future in as_completed(futures, timeout=60):  # 60s total for all
                    try:
                        code, quote = future.result(timeout=10)  # Each future must finish in 10s
                        realtime_quotes[code] = quote
                    except FuturesTimeout:
                        code = futures[future]
                        logger.debug(f"[RealTime] Timeout fetching {code}")
                        realtime_quotes[code] = None
                    except Exception as e:
                        code = futures[future]
                        logger.debug(f"[RealTime] Error fetching {code}: {e}")
                        realtime_quotes[code] = None
            except FuturesTimeout:
                # Global timeout — mark remaining stocks as None and log
                timed_out = [c for f, c in futures.items() if not f.done()]
                for c in timed_out:
                    realtime_quotes.setdefault(c, None)
                logger.warning(
                    f"[RealTime] Global timeout (60s): {len(timed_out)} futures unfinished: {timed_out}"
                )
        
        # Helper: safe float conversion (None/NaN -> None)
        def _safe_float(val) -> Optional[float]:
            """Convert to float safely. Returns None for None/NaN/non-numeric."""
            if val is None:
                return None
            try:
                f = float(val)
                if f != f:  # NaN check
                    return None
                return f
            except (TypeError, ValueError):
                return None

        # Now filter using fetched data
        filtered = []
        excluded_reasons: Dict[str, List[str]] = {}
        
        for stock in candidates:
            code = stock.code
            try:
                # eod_buyback candidates already filtered by _screen_eod_buyback_realtime();
                # skip all realtime re-validation to avoid false negatives.
                if "eod_buyback" in (stock.strategies or []):
                    filtered.append(stock)
                    continue

                reasons = []
                
                # Use realtime data if available, otherwise fall back to daily data
                realtime_quote = realtime_quotes.get(code)
                change_pct = _safe_float(
                    realtime_quote.change_pct
                    if (realtime_quote and realtime_quote.change_pct is not None)
                    else stock.change_pct
                )
                vol_ratio = _safe_float(stock.volume_ratio)  # default to daily data
                if realtime_quote and realtime_quote.volume_ratio is not None and realtime_quote.volume_ratio > 0:
                    vol_ratio = _safe_float(realtime_quote.volume_ratio)
                elif realtime_quote and (realtime_quote.volume_ratio is None or realtime_quote.volume_ratio == 0):
                    logger.debug(
                        f"[Picker] {code}: realtime volume_ratio missing/zero, "
                        f"using daily value {stock.volume_ratio}"
                    )

                # Log when critical fields are None for debugging
                if change_pct is None or vol_ratio is None:
                    logger.debug(
                        f"[RealTime] {code}: change_pct={change_pct}, vol_ratio={vol_ratio} "
                        f"(realtime={'yes' if realtime_quote else 'no'})"
                    )

                # Rule 1: Exclude limit-up stocks
                if getattr(self.config, "picker_realtime_exclude_limit_up", True):
                    limit_up_pct = LIMIT_UP_PCT_KC_CY if is_kccy(code) else LIMIT_UP_PCT_MAIN
                    if change_pct is not None and change_pct >= limit_up_pct - 0.1:
                        reasons.append(f"涨停({change_pct:.1f}%)")
                
                # Rule 2: Exclude limit-down stocks
                if getattr(self.config, "picker_realtime_exclude_limit_down", True):
                    limit_down_pct = -LIMIT_UP_PCT_KC_CY if is_kccy(code) else -LIMIT_UP_PCT_MAIN
                    if change_pct is not None and change_pct <= limit_down_pct + 0.1:
                        reasons.append(f"跌停({change_pct:.1f}%)")
                
                # Rule 3: Apply STRATEGY-SPECIFIC limits (prioritize realtime over quantitative)
                # Update turnover_rate / market_cap with realtime data for all strategies
                # Fall back to daily data when realtime value is missing or zero
                turnover = _safe_float(stock.turnover_rate)
                market_cap = _safe_float(stock.market_cap)
                if realtime_quote:
                    rt_turnover = _safe_float(getattr(realtime_quote, "turnover_rate", None))
                    if rt_turnover is not None and rt_turnover > 0:
                        turnover = rt_turnover
                    elif rt_turnover is not None and rt_turnover == 0:
                        logger.debug(
                            f"[Picker] {code}: realtime turnover_rate is 0, "
                            f"using daily value {stock.turnover_rate}"
                        )
                    rt_mv = _safe_float(getattr(realtime_quote, "total_mv", None))
                    if rt_mv is not None and rt_mv > 0:
                        # total_mv from UnifiedRealtimeQuote is in yuan; convert to 亿
                        market_cap = rt_mv / 1e8
                    elif rt_mv is not None and rt_mv == 0:
                        logger.debug(
                            f"[Picker] {code}: realtime total_mv is 0, "
                            f"using daily value {stock.market_cap}"
                        )

                # Apply strategy-specific params
                if stock.strategies:
                    strategy_failures = []
                    for strategy_id in stock.strategies:
                        params = get_strategy_params(strategy_id)
                        
                        # eod_buyback handled by top-level bypass above; defensive skip
                        if strategy_id == "eod_buyback":
                            continue
                        # For other strategies, use relaxed params from filter_momentum/filter_volume
                        if (
                            params.daily_change_min is not None
                            and change_pct is not None
                            and change_pct < params.daily_change_min
                        ):
                            strategy_failures.append(
                                f"涨幅不足({change_pct:.1f}%<{params.daily_change_min}%)"
                            )
                        
                        if (
                            params.daily_change_max is not None
                            and change_pct is not None
                            and change_pct > params.daily_change_max
                        ):
                            strategy_failures.append(
                                f"涨幅超({change_pct:.1f}%>{params.daily_change_max}%)"
                            )
                        
                        if (
                            params.volume_ratio_min is not None
                            and vol_ratio is not None
                            and vol_ratio < params.volume_ratio_min
                        ):
                            strategy_failures.append(
                                f"量比不足({vol_ratio:.1f}x<{params.volume_ratio_min}x)"
                            )
                    
                    if strategy_failures:
                        reasons.extend(strategy_failures)
                
                # Rule 4: Filter by today's change % range (environment override)
                daily_chg_min = getattr(self.config, "picker_realtime_daily_chg_min", None)
                daily_chg_max = getattr(self.config, "picker_realtime_daily_chg_max", None)
                if daily_chg_min is not None and change_pct is not None and change_pct < daily_chg_min:
                    reasons.append(f"涨幅不足(要求>{daily_chg_min}%,当前{change_pct:.1f}%)")
                if daily_chg_max is not None and change_pct is not None and change_pct > daily_chg_max:
                    reasons.append(f"涨幅过大(要求<{daily_chg_max}%,当前{change_pct:.1f}%)")
                
                # Rule 5: Exclude abnormal volume spike
                max_vol_ratio = getattr(self.config, "picker_realtime_max_volume_ratio", 0.0)
                if max_vol_ratio > 0 and vol_ratio is not None and vol_ratio > max_vol_ratio:
                    reasons.append(f"异常放量(量比{vol_ratio:.1f}>{max_vol_ratio})")
                
                if reasons:
                    excluded_reasons[code] = reasons
                else:
                    filtered.append(stock)

            except Exception as e:
                # Graceful degradation: keep the candidate on unexpected errors
                logger.warning(f"[RealTime] Realtime check failed for {code}: {e}")
                filtered.append(stock)
        
        if excluded_reasons:
            logger.info(f"[StockPicker] Real-time filtering excluded {len(excluded_reasons)} stocks:")
            for code, reasons in sorted(excluded_reasons.items())[:10]:
                logger.info(f"  {code}: {', '.join(reasons)}")
            if len(excluded_reasons) > 10:
                logger.info(f"  ... and {len(excluded_reasons) - 10} more")
        
        return filtered

    # ------------------------------------------------------------------
    # EOD buyback helpers
    # ------------------------------------------------------------------

    _INTEL_ITEM_TIMEOUT = 20  # wall-clock timeout per market intel fetch (reduced: Tushare is fast, efinance 5s fail-fast)
    _INTEL_TOTAL_TIMEOUT = 45  # overall wall-clock cap for the entire _gather_market_intel (was 60)

    def _gather_market_intel(self) -> Dict[str, Any]:
        """Gather macro market data from multiple sources with per-call timeouts.

        All three data fetches (indices, market_stats, sector_rankings) run in
        **parallel** via a shared ThreadPoolExecutor.  An overall wall-clock cap
        (_INTEL_TOTAL_TIMEOUT) prevents cascading fallback retries from exceeding
        a reasonable budget.  If the cap is reached, the method returns whatever
        data has been collected so far.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
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

        # Explicit pool management: avoid `with` which calls shutdown(wait=True) and blocks on timeout
        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="intel")
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

            try:
                for future in as_completed(all_futures, timeout=self._INTEL_TOTAL_TIMEOUT):
                    label_key = all_futures[future]
                    try:
                        label, result = future.result()  # already done, no extra timeout needed
                    except Exception as e:
                        logger.warning(f"[StockPicker] {label_key} future error: {e}")
                        continue

                    if label == "indices" and result:
                        intel["indices"] = result
                        # Check if any index is flagged stale by DataFetcherManager
                        today_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
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
                        # Check if market stats are stale
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
                            # Check if sector data is stale
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
                # Overall timeout reached — collect whatever finished so far
                timed_out = [lbl for f, lbl in all_futures.items() if not f.done()]
                logger.warning(
                    f"[StockPicker] _gather_market_intel global timeout ({self._INTEL_TOTAL_TIMEOUT}s), "
                    f"unfinished: {timed_out}"
                )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

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

    def _fetch_chip_for_candidates(
        self, candidates: List[ScreenedStock], max_stocks: int = 25, timeout_per_stock: float = 8.0
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch chip distribution for candidates. Returns {code: {concentration_90, profit_ratio}}."""
        chip_map: Dict[str, Dict[str, Any]] = {}
        if not getattr(self.config, "enable_chip_distribution", True):
            return chip_map
        if not self._data_manager or not candidates:
            return chip_map
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

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
        # max_workers=1: Eastmoney chip API closes connections on parallel requests
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="chip") as pool:
            futures = {pool.submit(_fetch_one, code): code for code in to_fetch}
            for fut in futures:
                code = futures[fut]
                try:
                    data = fut.result(timeout=timeout_per_stock)
                    if data:
                        chip_map[code] = data
                except FuturesTimeout:
                    logger.debug(f"[StockPicker] Chip fetch timeout for {code}")
                except Exception as e:
                    logger.debug(f"[StockPicker] Chip fetch error for {code}: {e}")

        if chip_map:
            logger.info(f"[StockPicker] Fetched chip data for {len(chip_map)}/{len(to_fetch)} candidates")
        return chip_map

    def _build_prompt(
        self, intel: Dict[str, Any], candidates: List[ScreenedStock], chip_map: Optional[Dict[str, Dict[str, Any]]] = None
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

        # ── Quant pool ──
        if candidates:
            parts.append(f"## 量化筛选池（从全市场筛选出的 {len(candidates)} 只候选）")
            has_chip = any(s.code in chip_map for s in candidates)
            has_strategies = len(strategies) > 1 and any(getattr(s, "strategies", []) for s in candidates)
            strat_col = "| 策略 |" if has_strategies else ""
            strat_sep = "|------|" if has_strategies else ""
            if has_chip:
                parts.append(
                    f"| 代码 | 名称 | 现价 | 涨跌% | 量比 | 换手% | PE | 市值(亿) | 60日% | 筹码90% | 获利% |{strat_col} 评分 |"
                )
                parts.append(
                    f"|------|------|------|-------|------|-------|-----|---------|-------|---------|-------|{strat_sep}------|"
                )
            else:
                parts.append(
                    f"| 代码 | 名称 | 现价 | 涨跌% | 量比 | 换手% | PE | 市值(亿) | 60日% |{strat_col} 评分 |"
                )
                parts.append(
                    f"|------|------|------|-------|------|-------|-----|---------|-------|{strat_sep}------|"
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
                    strat_labels = ",".join(STRATEGY_DISPLAY_NAMES.get(x, x) for x in strat_tags[:3])
                    row += f" {strat_labels} |"
                row += f" {s.score:.0f} |"
                parts.append(row)
            parts.append("")
        else:
            parts.append(
                "## 量化筛选池\n（今日筛选未产出候选，请返回空推荐列表，不要自行选股）\n"
            )

        # ── Market intel ──
        indices_stale = intel.get("indices_stale", False)
        if intel.get("indices"):
            if indices_stale:
                # Stale index data: do NOT show specific numbers to prevent LLM from citing them
                parts.append(
                    "## 主要指数\n"
                    "今日指数实时数据获取失败，请勿在 market_summary 中引用具体指数涨跌幅数值。"
                    "请仅基于筛选池质量和板块数据给出市场判断。\n"
                )
            else:
                # Check if index data is stale (e.g. from Tushare index_daily fallback)
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
                # Stale stats: warn LLM not to cite specific numbers
                parts.append(
                    "## 市场统计\n"
                    "今日市场涨跌统计实时数据获取失败，以下为往期数据，请勿在 market_summary 中引用具体涨跌家数。\n"
                )
            else:
                s = intel["stats"]
                # Warn LLM if market stats are not from today (e.g. Tushare daily fallback)
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
                # Stale sector data: warn LLM not to cite specific sector rankings
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

    def _call_llm(self, prompt: str) -> Optional[str]:
        """Call LLM with the combined prompt."""
        if not self._analyzer or not self._analyzer.is_available():
            logger.error("[StockPicker] LLM analyzer not available")
            return None

        full_prompt = f"{PICK_SYSTEM_PROMPT}\n\n---\n\n{prompt}"
        logger.info("[StockPicker] Calling LLM for final stock selection...")
        return self._analyzer.generate_text(full_prompt, max_tokens=16384, temperature=0.7)

    def _parse_result(self, llm_output: str, result: PickerResult):
        """Parse LLM JSON output into PickerResult."""
        # Guard against empty LLM response (e.g. finish_reason="length" with reasoning models)
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

            for p in data.get("picks", []):
                code = str(p.get("code", "")).strip()
                name = str(p.get("name", "")).strip()
                if code and name:
                    result.picks.append(StockPick(
                        code=code, name=name,
                        sector=p.get("sector", ""),
                        reason=p.get("reason", ""),
                        catalyst=p.get("catalyst", ""),
                        attention=p.get("attention", "medium"),
                        risk_note=p.get("risk_note", ""),
                    ))

            logger.info(f"[StockPicker] Parsed {len(result.picks)} stock picks")

        except Exception as e:
            logger.error(f"[StockPicker] Failed to parse LLM output: {e}")
            result.error = f"Failed to parse LLM response: {e}"
            result.success = False
