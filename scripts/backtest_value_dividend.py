#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长期价值/红利策略 —— 独立精简回测脚本（不接入 picker 主框架）。

目的：用本地已有的 daily_basic（dv_ttm 股息率 / pe_ttm / pb / total_mv）做
"股息率 + 估值"双因子初筛，验证长期价值红利在 A 股是否存在 alpha。

口径要点
--------
- 收益为**含分红总回报**：用 daily.pct_chg 累乘。已验证 pre_close 为除权价，
  pct_chg 已含除息效应（工行 2024 原始价 +44.5% vs 累乘 +52.4%）。
- 基准：沪深300（价格收益）+ 全市场等权（同为 pct_chg 含分红口径，公平对比）。
- 数据全部来自 data/local_db（daily_basic_by_date 覆盖 2020-01 ~ 2026-05）。

已知局限（精简版，验证用）
--------------------------
- 仅当前上市股票池 → 轻度幸存者偏差。
- 周期内等权按日再平衡近似（非买入持有权重漂移）。
- 股息率为 TTM 静态值，未校验派息率/现金流可持续性（价值陷阱需完整版剔除）。

用法示例
--------
    python scripts/backtest_value_dividend.py
    python scripts/backtest_value_dividend.py --top-n 30 --rebalance-days 60 \
        --min-yield 3 --max-pe 20 --max-pb 2.5 --out data/value_div_curve.csv
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.config import setup_env

setup_env()

from src.services.local_db.store import LocalStockDB, default_db

TRADING_DAYS_PER_YEAR = 252
TOTAL_MV_PER_YI = 1e4  # daily_basic.total_mv 单位为万元；1 亿 = 1e4 万元


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class BacktestConfig:
    """价值红利回测的全部可调参数。"""

    start_date: str = "20200102"
    end_date: str = "20260514"
    rebalance_days: int = 60          # 每隔多少个交易日再平衡（约一季度）
    top_n: int = 20                   # 每期持仓数量（等权）

    # 股票池过滤
    min_list_years: float = 3.0       # 上市年限下限，剔除次新
    exclude_st: bool = True           # 剔除 ST/*ST
    min_mv_yi: float = 50.0           # 总市值下限（亿元），剔除微盘

    # 因子阈值
    min_yield: float = 2.5            # dv_ttm 下限（%）
    max_yield: float = 12.0           # dv_ttm 上限（%），剔除异常高息陷阱
    max_pe: float = 25.0              # pe_ttm 上限（且要求 > 0，剔除亏损）
    max_pb: float = 3.0               # pb 上限（且要求 > 0）

    # 因子权重
    w_dividend: float = 0.5           # 股息率因子权重
    w_value: float = 0.5              # 估值因子权重（pb/pe 各半）

    benchmark_index: str = "H00922.CSI"   # 中证红利全收益（与含分红策略同口径）
    use_eqw_benchmark: bool = True    # 全市场等权基准（含分红口径）


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------


def _norm_date(d: str) -> str:
    return str(d).replace("-", "")[:8]


def _to_dt(d: str) -> datetime:
    return datetime.strptime(_norm_date(d), "%Y%m%d")


def load_trading_days(db: LocalStockDB, start: str, end: str) -> List[str]:
    """返回 [start, end] 内的交易日（升序，YYYYMMDD）。"""
    cal = db.get_trade_cal("SSE", start, end)
    if cal.empty:
        raise RuntimeError("trade_cal 为空，请先运行 scripts/preload_local_db.py 同步")
    days = cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist()
    return sorted(_norm_date(d) for d in days)


def build_eqw_daily_returns(db: LocalStockDB, trading_days: List[str]) -> Dict[str, float]:
    """全市场等权每日收益（pct_chg 均值，含分红口径）。"""
    out: Dict[str, float] = {}
    for i, td in enumerate(trading_days):
        df = db.get_market_daily(td)
        if df is not None and not df.empty and "pct_chg" in df.columns:
            mean = pd.to_numeric(df["pct_chg"], errors="coerce").mean()
            if pd.notna(mean):
                out[td] = float(mean)
        if (i + 1) % 200 == 0:
            print(f"  [eqw] {i + 1}/{len(trading_days)} days", flush=True)
    return out


def load_index_daily_returns(
    db: LocalStockDB, code: str, trading_days: List[str]
) -> Dict[str, float]:
    """指数每日 pct_chg（价格收益）。"""
    if not trading_days:
        return {}
    df = db.get_index_daily(code, trading_days[0], trading_days[-1])
    if df.empty or "pct_chg" not in df.columns:
        return {}
    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str).map(_norm_date)
    return dict(zip(df["trade_date"], pd.to_numeric(df["pct_chg"], errors="coerce")))


def load_stock_basic(db: LocalStockDB) -> pd.DataFrame:
    sb = db.get_stock_basic()
    if sb.empty:
        raise RuntimeError("stock_basic 为空，请先同步基础数据")
    return sb[["ts_code", "name", "list_date", "industry"]].copy()


# ---------------------------------------------------------------------------
# 选股（单期）
# ---------------------------------------------------------------------------


def _apply_universe_filter(
    snap: pd.DataFrame, sb: pd.DataFrame, date: str, cfg: BacktestConfig
) -> pd.DataFrame:
    """在 daily_basic 快照上应用股票池过滤，返回合格候选。"""
    df = snap.merge(sb, on="ts_code", how="inner")
    if df.empty:
        return df

    for col in ("close", "pe_ttm", "pb", "dv_ttm", "total_mv"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[df["close"] > 0]
    df = df[df["pe_ttm"].notna() & (df["pe_ttm"] > 0) & (df["pe_ttm"] <= cfg.max_pe)]
    df = df[df["pb"].notna() & (df["pb"] > 0) & (df["pb"] <= cfg.max_pb)]
    df = df[df["dv_ttm"].notna() & (df["dv_ttm"] >= cfg.min_yield) & (df["dv_ttm"] <= cfg.max_yield)]
    df = df[df["total_mv"].notna() & (df["total_mv"] >= cfg.min_mv_yi * TOTAL_MV_PER_YI)]

    if cfg.exclude_st:
        df = df[~df["name"].astype(str).str.contains("ST", case=False, na=False)]

    cutoff = _to_dt(date).year - cfg.min_list_years
    df = df[pd.to_datetime(df["list_date"], format="%Y%m%d", errors="coerce").dt.year <= cutoff]
    return df.reset_index(drop=True)


def _score_and_select(df: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """计算股息率+估值复合因子分，返回 top_n。"""
    if df.empty:
        return df
    n = len(df)
    div_rank = df["dv_ttm"].rank(pct=True)                       # 越高越好
    pb_rank = (-df["pb"]).rank(pct=True)                         # 越低越好
    pe_rank = (-df["pe_ttm"]).rank(pct=True)                     # 越低越好
    value_rank = (pb_rank + pe_rank) / 2.0
    df = df.copy()
    df["score"] = cfg.w_dividend * div_rank + cfg.w_value * value_rank
    df = df.sort_values(["score", "dv_ttm"], ascending=[False, False])
    return df.head(cfg.top_n).reset_index(drop=True)


def select_picks(
    db: LocalStockDB, sb: pd.DataFrame, date: str, cfg: BacktestConfig
) -> pd.DataFrame:
    """给定再平衡日，返回选中的 top_n 股票（含 code/name/因子值/score）。"""
    snap = db.get_market_daily_basic(date)
    if snap.empty:
        return pd.DataFrame()
    eligible = _apply_universe_filter(snap, sb, date, cfg)
    return _score_and_select(eligible, cfg)


# ---------------------------------------------------------------------------
# 收益计算（含分红总回报）
# ---------------------------------------------------------------------------


def _compound(pct_series: pd.Series) -> float:
    """pct_chg 序列累乘 → 区间总回报（小数）。"""
    s = pd.to_numeric(pct_series, errors="coerce").dropna()
    if s.empty:
        return 0.0
    return float((1.0 + s / 100.0).prod() - 1.0)


def stock_daily_pct(
    db: LocalStockDB, code: str, start_excl: str, end_incl: str
) -> Dict[str, float]:
    """单只股票在 (start_excl, end_incl] 内每个交易日的 pct_chg。"""
    df = db.get_daily(code, start_excl, end_incl)
    if df.empty or "pct_chg" not in df.columns:
        return {}
    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str).map(_norm_date)
    df = df[(df["trade_date"] > _norm_date(start_excl)) & (df["trade_date"] <= _norm_date(end_incl))]
    return dict(zip(df["trade_date"], pd.to_numeric(df["pct_chg"], errors="coerce")))


def build_rebalance_periods(trading_days: List[str], rebalance_days: int) -> List[Tuple[str, str]]:
    """生成 (入场日, 出场日] 的再平衡区间列表。"""
    if len(trading_days) < 2:
        return []
    step = max(1, rebalance_days)
    points = trading_days[::step]
    last = trading_days[-1]
    if points[-1] != last:
        # 末尾剩余交易日过短时，并入最后一期，避免出现 1~2 天的退化区间
        tail = len(trading_days) - 1 - trading_days.index(points[-1])
        if tail < step / 2:
            points[-1] = last
        else:
            points.append(last)
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)]


# ---------------------------------------------------------------------------
# 回测主流程
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    strategy_nav: pd.Series            # index=trade_date, value=净值
    benchmark_navs: Dict[str, pd.Series]
    period_rows: List[Dict[str, object]]
    picks_per_period: List[Dict[str, object]]


def run_backtest(db: LocalStockDB, cfg: BacktestConfig) -> BacktestResult:
    trading_days = load_trading_days(db, cfg.start_date, cfg.end_date)
    if not trading_days:
        raise RuntimeError("回测区间内无交易日")
    print(f"交易日: {len(trading_days)} ({trading_days[0]} ~ {trading_days[-1]})", flush=True)

    sb = load_stock_basic(db)
    idx_ret = load_index_daily_returns(db, cfg.benchmark_index, trading_days)
    eqw_ret = build_eqw_daily_returns(db, trading_days) if cfg.use_eqw_benchmark else {}

    periods = build_rebalance_periods(trading_days, cfg.rebalance_days)
    print(f"再平衡区间: {len(periods)} 期，每期约 {cfg.rebalance_days} 交易日", flush=True)

    strat_daily: Dict[str, float] = {}
    period_rows: List[Dict[str, object]] = []
    picks_per_period: List[Dict[str, object]] = []

    for pi, (entry, exit_) in enumerate(periods):
        picks = select_picks(db, sb, entry, cfg)
        if picks.empty:
            print(f"  期 {pi + 1} {entry}->{exit_}: 0 选中，空仓", flush=True)
            period_rows.append(_empty_period_row(entry, exit_))
            picks_per_period.append({"entry": entry, "exit": exit_, "picks": []})
            continue

        # 每只票的每日 pct_chg；组合每日 = 等权均值
        per_stock: Dict[str, Dict[str, float]] = {}
        for code in picks["ts_code"].tolist():
            per_stock[code] = stock_daily_pct(db, code, entry, exit_)

        window_days = [d for d in trading_days if entry < d <= exit_]
        for d in window_days:
            vals = [per_stock[c][d] for c in per_stock if d in per_stock[c] and pd.notna(per_stock[c][d])]
            strat_daily[d] = float(pd.Series(vals).mean()) if vals else 0.0

        strat_period = _compound(pd.Series([strat_daily[d] for d in window_days]))
        idx_period = _compound(pd.Series([idx_ret.get(d, 0.0) for d in window_days]))
        eqw_period = _compound(pd.Series([eqw_ret.get(d, 0.0) for d in window_days])) if eqw_ret else float("nan")

        period_rows.append({
            "entry": entry, "exit": exit_, "n": int(len(picks)),
            "strategy": strat_period, "index": idx_period, "eqw": eqw_period,
            "avg_yield": float(picks["dv_ttm"].mean()),
            "avg_pb": float(picks["pb"].mean()),
            "avg_pe": float(picks["pe_ttm"].mean()),
        })
        picks_per_period.append({
            "entry": entry, "exit": exit_,
            "picks": picks[["ts_code", "name", "industry", "dv_ttm", "pe_ttm", "pb", "score"]].to_dict("records"),
        })
        print(
            f"  期 {pi + 1} {entry}->{exit_}: 持 {len(picks)} 只，"
            f"策略 {strat_period * 100:+.2f}% | {cfg.benchmark_index} {idx_period * 100:+.2f}%",
            flush=True,
        )

    ordered_days = [d for d in trading_days if d in strat_daily]
    strat_nav = _nav_curve([strat_daily[d] for d in ordered_days], ordered_days)
    bench_navs = {
        cfg.benchmark_index: _nav_curve([idx_ret.get(d, 0.0) for d in ordered_days], ordered_days),
    }
    if eqw_ret:
        bench_navs["EQW_market"] = _nav_curve([eqw_ret.get(d, 0.0) for d in ordered_days], ordered_days)

    return BacktestResult(strat_nav, bench_navs, period_rows, picks_per_period)


def _empty_period_row(entry: str, exit_: str) -> Dict[str, object]:
    return {
        "entry": entry, "exit": exit_, "n": 0,
        "strategy": 0.0, "index": 0.0, "eqw": float("nan"),
        "avg_yield": float("nan"), "avg_pb": float("nan"), "avg_pe": float("nan"),
    }


def _nav_curve(daily_returns: List[float], dates: List[str]) -> pd.Series:
    nav, cur = [], 1.0
    for r in daily_returns:
        cur *= (1.0 + (0.0 if pd.isna(r) else r) / 100.0)
        nav.append(cur)
    return pd.Series(nav, index=dates, dtype=float)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def _max_drawdown(nav: pd.Series) -> float:
    if nav.empty:
        return 0.0
    roll_max = nav.cummax()
    dd = nav / roll_max - 1.0
    return float(dd.min())


def _metrics(nav: pd.Series) -> Dict[str, float]:
    if nav.empty or len(nav) < 2:
        return {"total": 0.0, "cagr": 0.0, "vol": 0.0, "sharpe": 0.0, "maxdd": 0.0}
    daily = nav.pct_change().dropna() * 100.0  # 回到 pct 口径
    total = float(nav.iloc[-1] - 1.0)
    first, last = _to_dt(nav.index[0]), _to_dt(nav.index[-1])
    years = max((last - first).days / 365.25, 1e-9)
    cagr = float(nav.iloc[-1] ** (1.0 / years) - 1.0) if nav.iloc[-1] > 0 else -1.0
    vol = float(daily.std() * math.sqrt(TRADING_DAYS_PER_YEAR)) if len(daily) > 1 else 0.0
    sharpe = float(daily.mean() / daily.std() * math.sqrt(TRADING_DAYS_PER_YEAR)) if daily.std() else 0.0
    return {"total": total, "cagr": cagr, "vol": vol, "sharpe": sharpe, "maxdd": _max_drawdown(nav)}


def print_summary(result: BacktestResult, cfg: BacktestConfig) -> None:
    strat = _metrics(result.strategy_nav)
    print("\n" + "=" * 64)
    print("价值红利策略 · 精简回测结果（含分红总回报）")
    print("=" * 64)
    span = ""
    if not result.strategy_nav.empty:
        span = f"{result.strategy_nav.index[0]} ~ {result.strategy_nav.index[-1]}"
    print(f"区间: {span}   持仓: top {cfg.top_n}   再平衡: 每 {cfg.rebalance_days} 交易日")
    print(f"因子: 股息率 {cfg.w_dividend:.0%} + 估值 {cfg.w_value:.0%} | "
          f"dv_ttm∈[{cfg.min_yield},{cfg.max_yield}]% pe≤{cfg.max_pe} pb≤{cfg.max_pb} "
          f"市值≥{cfg.min_mv_yi:.0f}亿")

    print(f"\n{'组合':<14}{'累计':>10}{'年化':>9}{'波动':>9}{'Sharpe':>9}{'最大回撤':>10}")
    rows = [("策略", strat)]
    for name, nav in result.benchmark_navs.items():
        rows.append((name, _metrics(nav)))
    for name, m in rows:
        print(f"{name:<14}{m['total'] * 100:>9.1f}%{m['cagr'] * 100:>8.1f}%"
              f"{m['vol']:>8.1f}%{m['sharpe']:>9.2f}{m['maxdd'] * 100:>9.1f}%")

    # 超额收益（年化）
    for name, nav in result.benchmark_navs.items():
        bm = _metrics(nav)
        print(f"\n年化超额 vs {name}: {(strat['cagr'] - bm['cagr']) * 100:+.2f}%")

    valid = [r for r in result.period_rows if r["n"]]
    if valid:
        wins = sum(1 for r in valid if r["strategy"] > 0)
        beat_idx = sum(1 for r in valid if r["strategy"] > r["index"])
        avg_y = pd.Series([r["avg_yield"] for r in valid]).mean()
        print(f"\n期数: {len(valid)}   正收益期: {wins} ({wins / len(valid):.0%})   "
              f"跑赢基准({cfg.benchmark_index})期数: {beat_idx} ({beat_idx / len(valid):.0%})")
        print(f"选中组合平均股息率: {avg_y:.2f}%")
    print("=" * 64)


def export_csv(result: BacktestResult, path: str) -> None:
    nav = result.strategy_nav.rename("strategy_nav").to_frame()
    for name, s in result.benchmark_navs.items():
        nav[name] = s
    nav.index.name = "trade_date"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    nav.to_csv(path)
    print(f"\n净值曲线已导出: {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> Tuple[BacktestConfig, Optional[str]]:
    p = argparse.ArgumentParser(description="长期价值/红利策略精简回测")
    p.add_argument("--start", default="20200102")
    p.add_argument("--end", default="20260514")
    p.add_argument("--rebalance-days", type=int, default=60)
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--min-list-years", type=float, default=3.0)
    p.add_argument("--min-mv", type=float, default=50.0, help="总市值下限（亿元）")
    p.add_argument("--min-yield", type=float, default=2.5, help="dv_ttm 下限（%%）")
    p.add_argument("--max-yield", type=float, default=12.0, help="dv_ttm 上限（%%）")
    p.add_argument("--max-pe", type=float, default=25.0)
    p.add_argument("--max-pb", type=float, default=3.0)
    p.add_argument("--w-dividend", type=float, default=0.5)
    p.add_argument("--w-value", type=float, default=0.5)
    p.add_argument("--keep-st", action="store_true", help="保留 ST 股票")
    p.add_argument("--no-eqw", action="store_true", help="关闭全市场等权基准")
    p.add_argument("--benchmark", default="H00922.CSI", help="基准指数（默认中证红利全收益）")
    p.add_argument("--out", default=None, help="导出净值曲线 CSV 路径")
    a = p.parse_args(argv)
    return BacktestConfig(
        start_date=_norm_date(a.start), end_date=_norm_date(a.end),
        rebalance_days=a.rebalance_days, top_n=a.top_n,
        min_list_years=a.min_list_years, exclude_st=not a.keep_st, min_mv_yi=a.min_mv,
        min_yield=a.min_yield, max_yield=a.max_yield, max_pe=a.max_pe, max_pb=a.max_pb,
        w_dividend=a.w_dividend, w_value=a.w_value,
        benchmark_index=a.benchmark, use_eqw_benchmark=not a.no_eqw,
    ), a.out


def main() -> int:
    cfg, out_path = parse_args()
    db = default_db()
    print("加载本地数据并构建基准...", flush=True)
    result = run_backtest(db, cfg)
    print_summary(result, cfg)
    if out_path:
        export_csv(result, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
