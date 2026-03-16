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
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401 - Dict used in screen()

import pandas as pd
from json_repair import repair_json

from src.config import get_config
from src.core.trading_calendar import get_last_trading_day
from src.search_service import SearchService
from data_provider.base import DataFetcherManager, is_kc_cy_stock

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
# MACD golden cross filter lookback (need ~30 days for MACD warmup)
MACD_LOOKBACK_DAYS = 35

# B-wave risk (波浪 ABC): exclude stocks likely in B-wave bounce (fake recovery before C-wave down)
B_WAVE_LOOKBACK_DAYS = 20
B_WAVE_MIN_DROOP_PCT = 5.0  # A-wave drop must be at least 5%
B_WAVE_RETRACE_LO = 0.35    # Fibonacci B-wave zone: 38.2% retracement
B_WAVE_RETRACE_HI = 0.65    # 61.8% retracement
B_WAVE_LOW_DAYS_AGO_MIN = 2  # Low must be at least 2 days ago (we've bounced)
B_WAVE_LOW_DAYS_AGO_MAX = 14  # Low not more than 14 days ago (recent drop)


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

    def screen(self, trade_date: Optional[str] = None) -> Tuple[List[ScreenedStock], ScreenStats, Dict[str, List[ScreenedStock]]]:
        """Run the full screening pipeline. Returns (candidates, stats, candidates_per_strategy).
        When trade_date is provided (YYYYMMDD), run historical screening (Tushare only).
        Uses multi-strategy when picker_strategies has multiple entries."""
        stats = ScreenStats()
        self._as_of_date = self._trade_date_to_iso(trade_date) if trade_date else None

        df = self._fetch_spot_data(trade_date)
        if df is None or df.empty:
            logger.warning("[Screener] No spot data available")
            return [], stats, {}

        stats.total_stocks = len(df)
        logger.info(f"[Screener] Starting with {stats.total_stocks} stocks, strategies={self._picker_strategies}")

        # Layer 1: Basic quality filter (shared, pe_max=100)
        df = self._filter_basic_for_strategies(df)
        stats.after_basic = len(df)
        logger.info(f"[Screener] After basic filter: {len(df)}")

        # Run each strategy and merge
        from src.services.picker_strategies import (
            get_strategy_params,
            filter_momentum,
            filter_volume,
            score_and_rank,
            merge_candidates_by_code,
            MACD_GOLDEN_CROSS,
        )

        candidates_per_strategy: Dict[str, List[ScreenedStock]] = {}
        for strategy_id in self._picker_strategies:
            params = get_strategy_params(strategy_id)
            df_s = filter_momentum(df.copy(), params)
            stats.after_momentum = len(df_s)
            df_s = filter_volume(df_s, params, self._turnover_min, self._turnover_max)
            stats.after_volume = len(df_s)

            cands = score_and_rank(df_s, strategy_id, params, top_n=PICKER_TOP_N_PER_STRATEGY)
            cands = self._filter_by_bias(
                cands,
                max_bias_pct=params.max_bias_pct,
                leader_bias_exempt_pct=getattr(params, "leader_bias_exempt_pct", 0.0),
            )
            cands = self._filter_limit_up_streak(cands)
            cands = self._filter_consecutive_up_days(cands, max_up_days=params.max_consecutive_up_days)
            cands = self._filter_healthy_pullback(cands, params=params)
            if strategy_id == MACD_GOLDEN_CROSS:
                cands = self._filter_macd_golden_cross(cands, lookback_days=MACD_LOOKBACK_DAYS)
            if self._enable_b_wave_filter:
                cands = self._filter_b_wave_risk(cands)

            if cands:
                candidates_per_strategy[strategy_id] = cands
                logger.info(f"[Screener] {strategy_id}: {len(cands)} candidates")

        if not candidates_per_strategy:
            stats.final_pool = 0
            logger.warning("[Screener] No candidates from any strategy")
            return [], stats, {}

        candidates = merge_candidates_by_code(candidates_per_strategy)
        stats.final_pool = len(candidates)
        logger.info(f"[Screener] Merged {stats.final_pool} candidates from {len(candidates_per_strategy)} strategies")
        return candidates, stats, candidates_per_strategy

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
    ) -> Dict[Tuple[str, str, str, int], Tuple[pd.DataFrame, str]]:
        """Fetch get_daily_data for multiple (code, start, end, days) in parallel.
        Deduplicates requests by key. Returns {(code, start, end, days): (df, source)}.
        Failed fetches are omitted."""
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
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="screener_fetch") as pool:
            for res in pool.map(_fetch, unique_requests):
                if res:
                    results[res[0]] = res[1]
        return results

    @staticmethod
    def _is_leader_candidate(s: ScreenedStock) -> bool:
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
            df_daily, _ = batch.get((s.code, "", end_date, 10), (None, ""))
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
            df_daily, _ = batch.get((s.code, "", end_date, days + 5), (None, ""))
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
            df_daily, _ = batch.get((s.code, "", end_date, days + 5), (None, ""))
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
    ) -> List[ScreenedStock]:
        """Filter for healthy pullback confirmation to distinguish from trend reversal.

        Checks (strategy-specific when params provided):
        1. Volume shrink: volume_ratio < 1.0 on pullback day (缩量回调)
        2. MA bullish alignment: MA5 > MA10 > MA20 (均线多头排列)
        3. Retracement limit: pullback < X% of prior 10d rally (回调幅度限制)
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
            df_daily, _ = batch.get((s.code, "", end_date, lookback_days + 5), (None, ""))
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

            # Check 1: Volume shrink (if required)
            if mode_params.require_volume_shrink and s.volume_ratio >= 1.0:
                # Pullback day should have volume_ratio < 1.0 (less selling pressure)
                # Only exclude if today is actually a down/flat day
                if s.change_pct <= 0:
                    logger.debug(f"[Screener] Exclude {s.code}: pullback but volume_ratio={s.volume_ratio:.2f} >= 1.0")
                    continue

            # Check 2: MA bullish alignment (MA5 > MA10 > MA20)
            if mode_params.require_ma_bullish and len(close_series) >= 20:
                ma5 = float(close_series.tail(5).mean())
                ma10 = float(close_series.tail(10).mean())
                ma20 = float(close_series.tail(20).mean())
                if not (ma5 > ma10 > ma20):
                    logger.debug(
                        f"[Screener] Exclude {s.code}: MA not bullish (MA5={ma5:.2f}, MA10={ma10:.2f}, MA20={ma20:.2f})"
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

            filtered.append(s)

        return filtered

    def _filter_macd_golden_cross(
        self,
        candidates: List[ScreenedStock],
        lookback_days: int = MACD_LOOKBACK_DAYS,
    ) -> List[ScreenedStock]:
        """Filter for MACD golden cross: DIF crosses above DEA in last 2 days.
        Uses pandas_ta_classic for MACD (fast=12, slow=26, signal=9)."""
        if not self._data_manager or not candidates:
            return candidates

        try:
            import pandas_ta_classic as ta
        except ImportError:
            logger.warning("[Screener] pandas_ta_classic not installed, skip MACD golden cross filter")
            return candidates

        end_date = self._as_of_date
        requests = [(s.code, None, end_date, lookback_days + 5) for s in candidates]
        batch = self._fetch_daily_batch(requests)

        filtered = []
        for s in candidates:
            df_daily, _ = batch.get((s.code, "", end_date, lookback_days + 5), (None, ""))
            if df_daily is None or len(df_daily) < 30:
                continue

            close_col = self._first_col(df_daily, "close", "收盘")
            if close_col is None:
                continue

            date_col = self._first_col(df_daily, "date", "日期") or df_daily.columns[0]
            df_daily = df_daily.sort_values(date_col).tail(lookback_days).reset_index(drop=True)
            close = pd.to_numeric(df_daily[close_col], errors="coerce").fillna(0)

            macd_df = ta.macd(close, fast=12, slow=26, signal=9)
            if macd_df is None or (isinstance(macd_df, pd.DataFrame) and macd_df.empty):
                continue

            # pandas_ta_classic returns DataFrame: col0=MACD line (DIF), col1=Signal (DEA), col2=Histogram
            if isinstance(macd_df, pd.DataFrame) and len(macd_df.columns) >= 2:
                dif = pd.to_numeric(macd_df.iloc[:, 0], errors="coerce").fillna(0)
                dea = pd.to_numeric(macd_df.iloc[:, 1], errors="coerce").fillna(0)
            else:
                continue
            if len(dif) < 2 or len(dea) < 2:
                continue

            prev_dif, curr_dif = float(dif.iloc[-2]), float(dif.iloc[-1])
            prev_dea, curr_dea = float(dea.iloc[-2]), float(dea.iloc[-1])
            # Golden cross: prev DIF < prev DEA and curr DIF > curr DEA
            if prev_dif < prev_dea and curr_dif > curr_dea:
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
            df_daily, _ = batch.get((s.code, "", end_date, lookback_days + 5), (None, ""))
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
        """Fetch full A-share data. Priority: Tushare → AkShare → efinance.
        When trade_date (YYYYMMDD) is provided, only Tushare is used (historical mode)."""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        # --- 1. Tushare (most stable, no eastmoney dependency) ---
        df = self._try_tushare(trade_date=trade_date)
        if df is not None and not df.empty:
            logger.info(f"[Screener] Using Tushare data: {len(df)} stocks")
            return df
        logger.info("[Screener] Tushare unavailable or empty, trying fallback sources")

        if trade_date:
            # Historical mode: only Tushare supported
            return None

        # --- 2. AkShare with hard wall-clock timeout ---
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
            logger.info(f"[Screener] Trying AkShare (wall timeout={self._spot_timeout}s)...")
            t0 = time.time()
            try:
                fut = pool.submit(_try_akshare)
                df = fut.result(timeout=self._spot_timeout)
                logger.info(f"[Screener] AkShare returned {len(df)} stocks in {time.time()-t0:.1f}s")
                return df
            except FuturesTimeout:
                logger.warning(f"[Screener] AkShare hard-timeout after {self._spot_timeout}s")
                fut.cancel()
            except Exception as e:
                logger.warning(f"[Screener] AkShare failed: {e}")

        # --- 3. efinance fallback with hard wall-clock timeout ---
        def _try_efinance() -> pd.DataFrame:
            import efinance as ef
            return ef.stock.get_realtime_quotes()

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="screener") as pool:
            logger.info(f"[Screener] Trying efinance (wall timeout={self._spot_timeout}s)...")
            t0 = time.time()
            try:
                fut = pool.submit(_try_efinance)
                df = fut.result(timeout=self._spot_timeout)
                logger.info(f"[Screener] efinance returned {len(df)} stocks in {time.time()-t0:.1f}s")
                return self._normalize_efinance_df(df)
            except FuturesTimeout:
                logger.warning(f"[Screener] efinance hard-timeout after {self._spot_timeout}s")
                fut.cancel()
            except Exception as e:
                logger.warning(f"[Screener] efinance failed: {e}")

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
                logger.warning("[StockPicker] Screening returned 0 candidates, proceeding with news only")

            # ── Stage 2: Gather market intel + AI selection ──
            logger.info("[StockPicker] === Stage 2: AI Selection ===")
            intel = self._gather_market_intel()
            chip_map = self._fetch_chip_for_candidates(candidates)
            prompt = self._build_prompt(intel, candidates, chip_map)
            llm_output = self._call_llm(prompt)

            if not llm_output:
                result.error = "LLM returned empty response"
                return result

            self._parse_result(llm_output, result)
            result.success = True

        except Exception as e:
            logger.error(f"[StockPicker] Error: {e}", exc_info=True)
            result.error = str(e)

        result.elapsed_seconds = time.time() - start
        return result

    _INTEL_ITEM_TIMEOUT = 15  # wall-clock timeout per market intel fetch (efinance may retry ~5s before fail, then akshare needs time)

    def _gather_market_intel(self) -> Dict[str, Any]:
        """Gather macro market data from multiple sources with per-call timeouts."""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        intel: Dict[str, Any] = {}

        def _timed_call(label, fn):
            """Run fn in a thread with wall-clock timeout. Return result or None."""
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="intel") as pool:
                try:
                    fut = pool.submit(fn)
                    return fut.result(timeout=self._INTEL_ITEM_TIMEOUT)
                except FuturesTimeout:
                    logger.warning(f"[StockPicker] {label} timed out after {self._INTEL_ITEM_TIMEOUT}s")
                    fut.cancel()
                except Exception as e:
                    logger.warning(f"[StockPicker] {label} failed: {e}")
            return None

        indices = _timed_call("indices", lambda: self._data_manager.get_main_indices("cn"))
        if indices:
            intel["indices"] = indices

        stats = _timed_call("market_stats", lambda: self._data_manager.get_market_stats())
        if stats:
            intel["stats"] = stats

        sector_result = _timed_call("sector_rankings", lambda: self._data_manager.get_sector_rankings(10))
        if sector_result:
            top_sectors, bottom_sectors = sector_result
            if top_sectors:
                intel["top_sectors"] = top_sectors
                intel["bottom_sectors"] = bottom_sectors

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
        today = datetime.now().strftime("%Y-%m-%d")
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
            parts.append("## 量化筛选池\n（今日筛选未产出候选，请仅基于市场情报推荐）\n")

        # ── Market intel ──
        if intel.get("indices"):
            parts.append("## 主要指数")
            for idx in intel["indices"]:
                name = idx.get("name", "")
                current = idx.get("current", 0)
                pct = idx.get("change_pct", 0)
                arrow = "↑" if pct > 0 else "↓" if pct < 0 else "→"
                parts.append(f"- {name}: {current:.2f} ({arrow}{pct:+.2f}%)")
            parts.append("")

        if intel.get("stats"):
            s = intel["stats"]
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
        return self._analyzer.generate_text(full_prompt, max_tokens=4096, temperature=0.7)

    def _parse_result(self, llm_output: str, result: PickerResult):
        """Parse LLM JSON output into PickerResult."""
        try:
            cleaned = llm_output.strip()
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
