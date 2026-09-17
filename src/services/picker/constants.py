# -*- coding: utf-8 -*-
"""
Constants, data classes, and utility functions for the stock picker pipeline.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from src.config import get_config
from src.core.trading_calendar import get_last_trading_day
from data_provider.base import is_bse_code, is_kc_cy_stock, is_st_stock

logger = logging.getLogger(__name__)

# Bias filter threshold (strict entry): exclude stocks with MA5 bias > this %
# Mode overrides: defensive=6%, balanced=8%, offensive=10%
PICKER_MAX_BIAS_PCT = 8.0

# Volume filter: require volume ratio > this to exclude cold stocks
VOLUME_RATIO_MIN = 1.0
# Turnover: 1-15% (plan: 0.5->1 filter cold, 20->15 reduce speculation)
TURNOVER_MIN_PCT = 1.0
TURNOVER_MAX_PCT = 15.0
# Amount by market cap: <100e8 use 30M, >=100e8 use 100M
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

# B-wave risk (ABC): exclude stocks likely in B-wave bounce (fake recovery before C-wave down)
B_WAVE_LOOKBACK_DAYS = 20
B_WAVE_MIN_DROOP_PCT = 5.0  # A-wave drop must be at least 5%
B_WAVE_RETRACE_LO = 0.382   # Fibonacci B-wave zone: exact 38.2% retracement
B_WAVE_RETRACE_HI = 0.618   # exact 61.8% retracement
B_WAVE_LOW_DAYS_AGO_MIN = 2  # Low must be at least 2 days ago (we've bounced)
B_WAVE_LOW_DAYS_AGO_MAX = 14  # Low not more than 14 days ago (recent drop)


def _get_limit_up_pct(code: str, name: str = "") -> float:
    """Return limit-up percentage threshold based on board type and ST status."""
    if is_st_stock(name):
        return LIMIT_UP_PCT_ST
    if is_bse_code(code):
        return LIMIT_UP_PCT_BSE
    if is_kc_cy_stock(code):
        return LIMIT_UP_PCT_KC_CY
    return LIMIT_UP_PCT_MAIN


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
    # Trade levels (computed via src.services.trade_levels.compute_trade_levels)
    ideal_buy: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2_rule: str = ""
    position_pct: float = 0.0
    risk_reward: float = 0.0
    # Multi-strategy resonance flag: "" / "double" / "triple"
    resonance: str = ""
    # SW L1 industry name (best-effort; empty when data source missing it).
    industry: str = ""
    # Limit-up pullback extras (default 0/"" for all other strategies):
    # deeper-pullback add-on slot and the anchor limit-up event metadata.
    secondary_buy: float = 0.0
    limit_up_date: str = ""          # YYYYMMDD anchor day
    setup: str = ""                  # pattern name, e.g. "回踩10日线"

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
        if self.industry:
            d["industry"] = self.industry
        if self.strategies:
            d["strategies"] = self.strategies
        if self.ideal_buy > 0:
            d["ideal_buy"] = round(self.ideal_buy, 3)
            d["stop_loss"] = round(self.stop_loss, 3)
            d["take_profit_1"] = round(self.take_profit_1, 3)
            d["take_profit_2_rule"] = self.take_profit_2_rule
            d["position_pct"] = round(self.position_pct, 3)
            d["risk_reward"] = round(self.risk_reward, 2)
        if self.secondary_buy > 0:
            d["secondary_buy"] = round(self.secondary_buy, 3)
        if self.limit_up_date:
            d["limit_up_date"] = self.limit_up_date
        if self.setup:
            d["setup"] = self.setup
        if self.resonance:
            d["resonance"] = self.resonance
        return d


@dataclass
class ScreenStats:
    """Statistics from the screening process."""
    total_stocks: int = 0
    after_basic: int = 0
    after_veto: int = 0                  # After fundamental hard-veto filter
    after_momentum: int = 0
    after_volume: int = 0
    final_pool: int = 0
    veto_reasons: Dict[str, int] = field(default_factory=dict)  # {reason_key: count}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_stocks": self.total_stocks,
            "after_basic_filter": self.after_basic,
            "after_veto_filter": self.after_veto,
            "after_momentum_filter": self.after_momentum,
            "after_volume_filter": self.after_volume,
            "final_pool": self.final_pool,
            "veto_reasons": self.veto_reasons,
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
    # Trade levels copied from the underlying ScreenedStock
    ideal_buy: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2_rule: str = ""
    position_pct: float = 0.0
    risk_reward: float = 0.0
    strategies: List[str] = field(default_factory=list)
    resonance: str = ""
    secondary_buy: float = 0.0
    limit_up_date: str = ""
    setup: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "code": self.code, "name": self.name, "sector": self.sector,
            "reason": self.reason, "catalyst": self.catalyst,
            "attention": self.attention, "risk_note": self.risk_note,
        }
        if self.ideal_buy > 0:
            d["ideal_buy"] = round(self.ideal_buy, 3)
            d["stop_loss"] = round(self.stop_loss, 3)
            d["take_profit_1"] = round(self.take_profit_1, 3)
            d["take_profit_2_rule"] = self.take_profit_2_rule
            d["position_pct"] = round(self.position_pct, 3)
            d["risk_reward"] = round(self.risk_reward, 2)
        if self.secondary_buy > 0:
            d["secondary_buy"] = round(self.secondary_buy, 3)
        if self.limit_up_date:
            d["limit_up_date"] = self.limit_up_date
        if self.setup:
            d["setup"] = self.setup
        if self.strategies:
            d["strategies"] = self.strategies
        if self.resonance:
            d["resonance"] = self.resonance
        return d


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


def create_screener_from_config(data_manager=None) -> "StockScreener":  # noqa: F821
    """Create StockScreener with config from environment. Use for picker and backtest."""
    from src.services.picker.screener import StockScreener
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
