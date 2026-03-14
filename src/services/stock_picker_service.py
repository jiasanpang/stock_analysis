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
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from json_repair import repair_json

from src.config import get_config
from src.search_service import SearchService
from data_provider.base import DataFetcherManager

logger = logging.getLogger(__name__)

# Bias filter threshold (严进策略): exclude stocks with MA5 bias > this %
PICKER_MAX_BIAS_PCT = 8.0

# Volume filter: require volume ratio > this to exclude cold stocks
VOLUME_RATIO_MIN = 1.0

# 60-day trend decay: gains above this % get score decay (avoid end-of-trend buys)
TREND_DECAY_THRESHOLD_PCT = 30.0

# Limit-up streak filter: exclude if >= this many limit-up days in last 5 days
LIMIT_UP_DAYS_THRESHOLD = 2
LIMIT_UP_PCT_THRESHOLD = 9.5  # pct_chg >= this treated as limit-up (main board ~10%)

# PE filter: exclude stocks with PE > this (obvious bubble)
PE_MAX = 100

# ── System prompt ────────────────────────────────────────────────

PICK_SYSTEM_PROMPT = """你是一位专业的 A 股市场分析师，负责从优质股票池中精选最具投资价值的标的。

## 你的任务
你将收到两类数据：
1. **量化筛选池**：系统已从全市场 5000+ 只股票中，通过严格的量化条件（正向趋势、合理估值、健康量能）筛选出的优质候选标的
2. **市场情报**：今日大盘指数、板块排行、热点新闻

请从量化筛选池中，结合市场情报，**精选 1-5 只**最具投资价值的股票。

## 核心选股原则（严格遵循）

### 1. 严进策略（不追高）
- **量化层**：筛选池已排除乖离率 > 8% 的标的（硬过滤）
- **推荐优先级**：乖离率 < 3% 最佳买点；3-5% 可关注；5-8% 降级为观望
- **公式**：乖离率 = (现价 - MA5) / MA5 × 100%

### 2. 趋势质量优先
- 60日涨幅 > 20%：强势趋势，加分
- 60日涨幅 10-20%：稳健趋势，正常评估
- 60日涨幅 5-10%：弱势趋势，需更强催化剂才考虑
- **今日涨幅**：2-6% 为健康上涨，>7% 需警惕追高风险

### 3. 估值安全边际
- PE 10-30 倍：理想区间，优先推荐
- PE 30-50 倍：需有业绩成长性支撑
- PE > 50 倍：谨慎，除非有强催化剂

### 4. 量能健康度
- 量比 1.0-2.5：健康放量，加分
- 量比 > 3.0：需警惕过度投机
- 换手率 2-8%：理想区间

### 5. 板块与市场共振
- 个股所在板块与今日领涨板块重合时，提升优先级
- 逆板块上涨（板块跌个股涨）需有独立催化剂才考虑

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
- attention: high（强烈关注，乖离率<3%且趋势强）、medium（适度关注）、low（跟踪观察，乖离率接近5%）
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

    def to_dict(self) -> Dict[str, Any]:
        return {
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
    generated_at: str = ""
    error: str = ""
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "market_summary": self.market_summary,
            "picks": [p.to_dict() for p in self.picks],
            "sectors_to_watch": self.sectors_to_watch,
            "risk_warning": self.risk_warning,
            "screen_stats": self.screen_stats.to_dict() if self.screen_stats else None,
            "screened_pool": [s.to_dict() for s in self.screened_pool],
            "generated_at": self.generated_at,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "error": self.error,
        }


# ── Quantitative Screener ───────────────────────────────────────

class StockScreener:
    """Multi-layer quantitative screener using full-market spot data."""

    _EXCLUDE_NAME_KEYWORDS = ("ST", "*ST", "退市", "N ", "C ")
    _ETF_PREFIXES = ("51", "52", "56", "58", "15", "16", "18")

    def __init__(self, data_manager=None):
        self._data_manager = data_manager

    def screen(self) -> Tuple[List[ScreenedStock], ScreenStats]:
        """Run the full screening pipeline. Returns (candidates, stats)."""
        stats = ScreenStats()

        df = self._fetch_spot_data()
        if df is None or df.empty:
            logger.warning("[Screener] No spot data available")
            return [], stats

        stats.total_stocks = len(df)
        logger.info(f"[Screener] Starting with {stats.total_stocks} stocks")

        # Layer 1: Basic quality filter
        df = self._filter_basic(df)
        stats.after_basic = len(df)
        logger.info(f"[Screener] After basic filter: {len(df)}")

        # Layer 2: Momentum filter
        df = self._filter_momentum(df)
        stats.after_momentum = len(df)
        logger.info(f"[Screener] After momentum filter: {len(df)}")

        # Layer 3: Volume / activity filter
        df = self._filter_volume(df)
        stats.after_volume = len(df)
        logger.info(f"[Screener] After volume filter: {len(df)}")

        # Layer 4: Score and rank
        candidates = self._score_and_rank(df)
        stats.final_pool = len(candidates)
        logger.info(f"[Screener] Final pool: {len(candidates)} candidates")

        # Layer 5: Bias filter (严进策略 — exclude MA5 bias > PICKER_MAX_BIAS_PCT)
        before_bias = len(candidates)
        candidates = self._filter_by_bias(candidates, max_bias_pct=PICKER_MAX_BIAS_PCT)
        stats.final_pool = len(candidates)
        if len(candidates) < before_bias:
            logger.info(f"[Screener] After bias filter: {stats.final_pool} candidates (excluded {before_bias - len(candidates)})")

        # Layer 6: Limit-up streak filter (exclude 连板/妖股)
        before_limit_up = len(candidates)
        candidates = self._filter_limit_up_streak(candidates)
        if len(candidates) < before_limit_up:
            stats.final_pool = len(candidates)
            logger.info(f"[Screener] After limit-up filter: {stats.final_pool} candidates (excluded {before_limit_up - len(candidates)})")

        return candidates, stats

    def _filter_by_bias(self, candidates: List[ScreenedStock], max_bias_pct: float = PICKER_MAX_BIAS_PCT) -> List[ScreenedStock]:
        """Filter out stocks with MA5 bias > max_bias_pct (严进策略)."""
        if not self._data_manager or not candidates:
            return candidates
        filtered = []
        for s in candidates:
            try:
                df_daily, _ = self._data_manager.get_daily_data(s.code, days=10)
                if df_daily is None or len(df_daily) < 5:
                    filtered.append(s)  # Keep if no data (don't exclude)
                    continue
                close_col = next((c for c in ["close", "收盘"] if c in df_daily.columns), None)
                if close_col is None:
                    filtered.append(s)
                    continue
                date_col = next((c for c in ["date", "日期"] if c in df_daily.columns), df_daily.columns[0])
                df_daily = df_daily.sort_values(date_col).tail(5)
                ma5 = float(df_daily[close_col].mean())
                if ma5 <= 0:
                    filtered.append(s)
                    continue
                bias_pct = (s.price - ma5) / ma5 * 100
                if bias_pct <= max_bias_pct:
                    filtered.append(s)
                else:
                    logger.debug(f"[Screener] Exclude {s.code} bias={bias_pct:.1f}% > {max_bias_pct}%")
            except Exception as e:
                logger.debug(f"[Screener] Bias check failed for {s.code}: {e}")
                filtered.append(s)  # Keep on error
        return filtered

    def _filter_limit_up_streak(
        self,
        candidates: List[ScreenedStock],
        days: int = 5,
        min_limit_up_days: int = LIMIT_UP_DAYS_THRESHOLD,
        pct_threshold: float = LIMIT_UP_PCT_THRESHOLD,
    ) -> List[ScreenedStock]:
        """Exclude stocks with 2+ limit-up days in last 5 days (连板/妖股 risk)."""
        if not self._data_manager or not candidates:
            return candidates
        filtered = []
        for s in candidates:
            try:
                df_daily, _ = self._data_manager.get_daily_data(s.code, days=days + 5)
                if df_daily is None or len(df_daily) < days:
                    filtered.append(s)
                    continue
                pct_col = next((c for c in ["pct_chg", "涨跌幅"] if c in df_daily.columns), None)
                if pct_col is None:
                    filtered.append(s)
                    continue
                date_col = next((c for c in ["date", "日期"] if c in df_daily.columns), df_daily.columns[0])
                df_daily = df_daily.sort_values(date_col).tail(days)
                pct = pd.to_numeric(df_daily[pct_col], errors="coerce").fillna(0)
                limit_up_count = int((pct >= pct_threshold).sum())
                if limit_up_count >= min_limit_up_days:
                    logger.debug(f"[Screener] Exclude {s.code} limit-up streak: {limit_up_count} days in last {days}")
                else:
                    filtered.append(s)
            except Exception as e:
                logger.debug(f"[Screener] Limit-up check failed for {s.code}: {e}")
                filtered.append(s)
        return filtered

    _UA_LIST = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    ]

    _SPOT_WALL_TIMEOUT = 10  # hard wall-clock timeout per provider

    def _fetch_spot_data(self) -> Optional[pd.DataFrame]:
        """Fetch full A-share data. Priority: Tushare → AkShare → efinance."""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        # --- 1. Tushare (most stable, no eastmoney dependency) ---
        df = self._try_tushare()
        if df is not None and not df.empty:
            return df

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
            logger.info(f"[Screener] Trying AkShare (wall timeout={self._SPOT_WALL_TIMEOUT}s)...")
            t0 = time.time()
            try:
                fut = pool.submit(_try_akshare)
                df = fut.result(timeout=self._SPOT_WALL_TIMEOUT)
                logger.info(f"[Screener] AkShare returned {len(df)} stocks in {time.time()-t0:.1f}s")
                return df
            except FuturesTimeout:
                logger.warning(f"[Screener] AkShare hard-timeout after {self._SPOT_WALL_TIMEOUT}s")
                fut.cancel()
            except Exception as e:
                logger.warning(f"[Screener] AkShare failed: {e}")

        # --- 3. efinance fallback with hard wall-clock timeout ---
        def _try_efinance() -> pd.DataFrame:
            import efinance as ef
            return ef.stock.get_realtime_quotes()

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="screener") as pool:
            logger.info(f"[Screener] Trying efinance (wall timeout={self._SPOT_WALL_TIMEOUT}s)...")
            t0 = time.time()
            try:
                fut = pool.submit(_try_efinance)
                df = fut.result(timeout=self._SPOT_WALL_TIMEOUT)
                logger.info(f"[Screener] efinance returned {len(df)} stocks in {time.time()-t0:.1f}s")
                return self._normalize_efinance_df(df)
            except FuturesTimeout:
                logger.warning(f"[Screener] efinance hard-timeout after {self._SPOT_WALL_TIMEOUT}s")
                fut.cancel()
            except Exception as e:
                logger.warning(f"[Screener] efinance failed: {e}")

        return None

    def _try_tushare(self) -> Optional[pd.DataFrame]:
        """Fetch full-market daily data via Tushare Pro (daily + daily_basic + stock_basic)."""
        tushare_api = self._get_tushare_api()
        if tushare_api is None:
            return None

        try:
            from zoneinfo import ZoneInfo
            china_now = datetime.now(ZoneInfo("Asia/Shanghai"))
            trade_date = china_now.strftime("%Y%m%d")

            logger.info(f"[Screener] Fetching via Tushare (trade_date={trade_date})...")
            t0 = time.time()

            df_daily = tushare_api.daily(trade_date=trade_date)
            if df_daily is None or df_daily.empty:
                # Maybe market hasn't opened yet or not a trading day; try yesterday
                yesterday = (china_now - pd.Timedelta(days=1)).strftime("%Y%m%d")
                logger.info(f"[Screener] No data for {trade_date}, trying {yesterday}...")
                df_daily = tushare_api.daily(trade_date=yesterday)
                trade_date = yesterday

            if df_daily is None or df_daily.empty:
                logger.warning("[Screener] Tushare daily returned empty")
                return None

            df_daily.columns = [c.lower() for c in df_daily.columns]

            # Fetch valuation metrics
            df_basic = tushare_api.daily_basic(
                trade_date=trade_date,
                fields="ts_code,pe,pe_ttm,pb,turnover_rate,volume_ratio,total_mv,circ_mv",
            )
            if df_basic is not None and not df_basic.empty:
                df_basic.columns = [c.lower() for c in df_basic.columns]
                df_daily = df_daily.merge(df_basic, on="ts_code", how="left")

            # Fetch stock names
            df_names = tushare_api.stock_basic(fields="ts_code,symbol,name")
            if df_names is not None and not df_names.empty:
                df_names.columns = [c.lower() for c in df_names.columns]
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
        # Try to reuse from data_manager's TushareFetcher
        if self._data_manager:
            for fetcher in self._data_manager._fetchers:
                if fetcher.__class__.__name__ == "TushareFetcher" and hasattr(fetcher, "_api") and fetcher._api:
                    return fetcher._api

        # Fallback: create fresh instance
        try:
            config = get_config()
            if not config.tushare_token:
                return None
            import tushare as ts
            ts.set_token(config.tushare_token)
            api = ts.pro_api()
            logger.info("[Screener] Created standalone Tushare API instance")
            return api
        except Exception as e:
            logger.warning(f"[Screener] Cannot init Tushare: {e}")
            return None

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

    def _filter_basic(self, df: pd.DataFrame) -> pd.DataFrame:
        """Layer 1: Remove ST, new listings, ETFs, penny stocks, and unprofitable."""
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

        # Price > 3 yuan (avoid penny stocks)
        if "最新价" in df.columns:
            df = df[pd.to_numeric(df["最新价"], errors="coerce") > 3]

        # PE > 0 (profitable) and PE < PE_MAX (exclude obvious bubble)
        if "市盈率-动态" in df.columns:
            pe = pd.to_numeric(df["市盈率-动态"], errors="coerce")
            df = df[(pe > 0) & (pe < PE_MAX)]

        return df

    def _filter_momentum(self, df: pd.DataFrame) -> pd.DataFrame:
        """Layer 2: Strong positive momentum — strict uptrend filter."""
        # Today's change > 0% (must be positive, showing strength)
        if "涨跌幅" in df.columns:
            pct = pd.to_numeric(df["涨跌幅"], errors="coerce")
            # Chase risk: exclude near limit-up (today > 9% = likely overextended)
            df = df[(pct > 0) & (pct < 9.0)]

        # 60-day change > 5% (clear medium-term uptrend)
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

        # Turnover rate 0.5% - 20% (active but not too speculative)
        if "换手率" in df.columns:
            tr = pd.to_numeric(df["换手率"], errors="coerce")
            df = df[(tr > 0.5) & (tr < 20)]

        # Amount > 5000万 (sufficient liquidity)
        if "成交额" in df.columns:
            amt = pd.to_numeric(df["成交额"], errors="coerce")
            df = df[amt > 5e7]

        return df

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

                # Composite score: Trend-focused, quality-first approach
                score = 0.0

                # 1. Trend strength (highest weight) - with decay for end-of-trend
                # 5-30%: linear score; >30%: decay to avoid buying at trend end
                if pct_60d <= 0:
                    pass
                elif pct_60d <= TREND_DECAY_THRESHOLD_PCT:
                    score += min(pct_60d, 25)
                else:
                    decay_score = 30 - (pct_60d - TREND_DECAY_THRESHOLD_PCT) * 0.5
                    score += max(0, decay_score)

                # 2. Today's momentum - max 20 points
                # Positive daily change, but cap to avoid over-weighting single day
                score += min(change_pct, 8) * 2.5
                # Chase risk penalty (align with 严进策略: 乖离率 > 5% 不追高)
                if change_pct > 7:
                    score -= 15

                # 3. Volume confirmation - max 20 points
                # Healthy volume (1.0-3.0 is ideal), not excessive
                if 1.0 <= vol_ratio <= 3.0:
                    score += 20
                elif vol_ratio > 3.0:
                    score += 15  # High volume but could be speculative
                elif vol_ratio > 0.8:
                    score += 10

                # 4. Turnover health - max 10 points
                # 2-8% is the sweet spot for liquidity without speculation
                if 2 <= turnover <= 8:
                    score += 10
                elif 1 <= turnover < 2:
                    score += 5
                elif 8 < turnover <= 12:
                    score += 3

                # 5. Valuation quality - max 10 points
                # Reasonable PE indicates fundamental support
                if 10 < pe < 30:
                    score += 10
                elif 5 < pe <= 10 or 30 <= pe < 50:
                    score += 5

                # 6. Market cap stability - bonus 5 points
                # Mid-cap stocks often have better growth potential
                if 50e8 < total_mv < 500e8:
                    score += 5

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

    def __init__(self):
        self.config = get_config()
        self._data_manager = DataFetcherManager()
        self._screener = StockScreener(data_manager=self._data_manager)
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
        result = PickerResult(generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"))

        try:
            # ── Stage 1: Quantitative screening ──
            logger.info("[StockPicker] === Stage 1: Quantitative Screening ===")
            candidates, stats = self._screener.screen()
            result.screen_stats = stats
            result.screened_pool = candidates

            if not candidates:
                logger.warning("[StockPicker] Screening returned 0 candidates, proceeding with news only")

            # ── Stage 2: Gather market intel + AI selection ──
            logger.info("[StockPicker] === Stage 2: AI Selection ===")
            intel = self._gather_market_intel()
            prompt = self._build_prompt(intel, candidates)
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

    _INTEL_ITEM_TIMEOUT = 8  # wall-clock timeout per market intel fetch

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

    def _build_prompt(self, intel: Dict[str, Any], candidates: List[ScreenedStock]) -> str:
        """Build the prompt with both quant pool and market intel."""
        today = datetime.now().strftime("%Y-%m-%d")
        parts = [f"# 今日选股分析 ({today})\n"]

        # ── Quant pool ──
        if candidates:
            parts.append(f"## 量化筛选池（从全市场筛选出的 {len(candidates)} 只候选）")
            parts.append(
                "| 代码 | 名称 | 现价 | 涨跌% | 量比 | 换手% | PE | 市值(亿) | 60日% | 评分 |"
            )
            parts.append(
                "|------|------|------|-------|------|-------|-----|---------|-------|------|"
            )
            for s in candidates:
                parts.append(
                    f"| {s.code} | {s.name} | {s.price:.2f} | "
                    f"{s.change_pct:+.2f} | {s.volume_ratio:.1f} | "
                    f"{s.turnover_rate:.1f} | {s.pe:.0f} | "
                    f"{s.market_cap:.0f} | {s.change_pct_60d:+.1f} | "
                    f"{s.score:.0f} |"
                )
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
            "请从量化筛选池和市场情报中，精选 5-10 只最值得关注的 A 股股票。"
            "优先从筛选池中选择，可结合新闻热点补充池外标的。"
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
