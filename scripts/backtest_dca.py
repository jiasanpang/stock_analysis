#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数定投 + 止盈策略回测（独立脚本，纯本地数据）。

回答两个问题：
1. 熊市里定投到底亏不亏？—— 按熊市区间拆解账户浮盈/浮亏。
2. 怎么止盈更挣钱、避免过山车？—— 对比 4 种止盈模式的收益、回撤与利润回吐。

口径
----
- 计值用**全收益指数** H00905.CSI（含分红再投），避免低估。
- 估值信号用中证500 PE_TTM 历史分位，**扩展窗口**计算（仅用当日之前的数据，无未来函数）。
- 月度定投（每月首个交易日），每日盯市。

止盈模式
--------
- none      : 不止盈，定投后长期持有（baseline）
- target    : 单轮累计收益率 >= target_ret 时清仓，之后重新开始定投
- valuation : PE 分位 >= tp_pe_pct 时清仓（贵了就跑）
- trailing  : 单轮利润从峰值回吐 >= trail_giveback 时清仓（保住胜利果实）

用法
----
    python scripts/backtest_dca.py                       # 4 种止盈横向对比 + 熊市拆解
    python scripts/backtest_dca.py --dca-mode valuation  # 估值加权定投
    python scripts/backtest_dca.py --start 20180101 --end 20260922
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.config import setup_env

setup_env()

from src.services.local_db.store import default_db


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class DcaConfig:
    price_code: str = "H00905.CSI"        # 全收益指数（计值）
    valuation_path: str = "data/index_valuation/000905.SH.parquet"
    start_date: str = "20150101"
    end_date: str = "20260922"
    monthly: float = 1000.0
    dca_mode: str = "fixed"               # fixed | valuation
    tp_mode: str = "none"                 # none | target | valuation | trailing
    target_ret: float = 0.30              # target 止盈阈值（单轮收益率）
    tp_pe_pct: float = 80.0               # valuation 止盈的 PE 分位阈值
    trail_giveback: float = 0.15          # trailing 止盈的利润回吐阈值
    restart: bool = True                  # 止盈后是否重新开始定投
    min_pe_obs: int = 120                 # 计算 PE 分位所需最少历史样本


# 估值加权定投的乘数表：(PE分位上限, 金额乘数)
VALUATION_MULTIPLIERS: Tuple[Tuple[float, float], ...] = (
    (30.0, 1.5),    # 便宜：多买
    (70.0, 1.0),    # 正常
    (85.0, 0.5),    # 偏贵：少买
    (101.0, 0.0),   # 很贵：停投
)


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


def _norm(d: str) -> str:
    return str(d).replace("-", "")[:8]


def load_price(cfg: DcaConfig) -> pd.Series:
    """全收益指数收盘价，index 为 datetime（升序）。"""
    db = default_db()
    df = db.get_index_daily(cfg.price_code, cfg.start_date, cfg.end_date)
    if df.empty:
        raise RuntimeError(f"本地无 {cfg.price_code}，请先同步 index_daily")
    df = df.copy()
    df["d"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
    s = df.sort_values("d").set_index("d")["close"].astype(float)
    return s[~s.index.duplicated(keep="last")]


def load_pe(cfg: DcaConfig) -> pd.Series:
    """指数 PE_TTM 历史，index 为 datetime。"""
    p = Path(cfg.valuation_path)
    if not p.exists():
        raise RuntimeError(f"估值文件缺失: {p}（需先用 index_dailybasic 落地）")
    df = pd.read_parquet(p)
    df["d"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
    s = df.sort_values("d").set_index("d")["pe_ttm"].astype(float)
    return s[~s.index.duplicated(keep="last")]


def pe_percentile_at(pe: pd.Series, date: pd.Timestamp, min_obs: int) -> float:
    """当日 PE 在"截至当日"历史中的分位（扩展窗口，无未来函数）。样本不足返回 nan。"""
    hist = pe.loc[:date].dropna()
    if len(hist) < min_obs:
        return float("nan")
    cur = hist.iloc[-1]
    return float((hist < cur).mean() * 100.0)


def valuation_multiplier(pct: float) -> float:
    if pd.isna(pct):
        return 1.0
    for upper, mult in VALUATION_MULTIPLIERS:
        if pct < upper:
            return mult
    return 0.0


def month_first_days(index: pd.DatetimeIndex) -> List[pd.Timestamp]:
    s = pd.Series(index, index=index)
    firsts = s.resample("MS").first().dropna()
    return [pd.Timestamp(x) for x in firsts.tolist()]


# ---------------------------------------------------------------------------
# 回测核心
# ---------------------------------------------------------------------------


@dataclass
class DcaResult:
    mode: str
    account_value: pd.Series                       # 每日账户总市值
    profit_ratio: pd.Series                        # 每日 (市值-累计投入)/累计投入
    events: List[Dict[str, object]] = field(default_factory=list)
    total_contributed: float = 0.0
    final_value: float = 0.0
    irr: float = 0.0
    max_drawdown: float = 0.0
    max_giveback: float = 0.0                      # 利润从峰值最大回吐比例
    contributions: List[Tuple[pd.Timestamp, float]] = field(default_factory=list)


def _irr(contribs: List[Tuple[pd.Timestamp, float]], end: pd.Timestamp, final_value: float) -> float:
    """资金加权年化：解 sum(amt*(1+r)^{持有年限}) = final_value。"""
    if not contribs or final_value <= 0:
        return 0.0
    h = np.array([max((end - d).days / 365.25, 1e-9) for d, _ in contribs])
    amt = np.array([a for _, a in contribs])

    def g(r: float) -> float:
        return float((amt * (1.0 + r) ** h).sum() - final_value)

    lo, hi = -0.99, 5.0
    if g(lo) > 0:
        return lo
    if g(hi) < 0:
        return hi
    for _ in range(200):
        mid = (lo + hi) / 2
        if g(mid) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def simulate(price: pd.Series, pe: pd.Series, cfg: DcaConfig) -> DcaResult:
    """按月定投 + 每日盯市 + 按 cfg.tp_mode 止盈。"""
    buy_days = set(month_first_days(price.index))
    realized_cash = 0.0
    total_contributed = 0.0
    cycle_invested = 0.0
    cycle_shares = 0.0
    cycle_peak_ratio = 0.0
    stopped = False
    contribs: List[Tuple[pd.Timestamp, float]] = []
    events: List[Dict[str, object]] = []

    av_list, pr_list = [], []
    peak_pr = 0.0
    max_giveback = 0.0

    for d, px in price.items():
        pe_pct = pe_percentile_at(pe, d, cfg.min_pe_obs)
        if d in buy_days and not stopped:
            if cfg.dca_mode == "valuation":
                mult = valuation_multiplier(pe_pct)
            elif cfg.tp_mode == "valuation" and not pd.isna(pe_pct) and pe_pct >= cfg.tp_pe_pct:
                mult = 0.0  # 估值止盈模式：高位不再加仓，避免买完即卖的无谓换手
            else:
                mult = 1.0
            amt = cfg.monthly * mult
            if amt > 0:
                cycle_shares += amt / px
                cycle_invested += amt
                total_contributed += amt
                contribs.append((d, amt))

        cycle_mv = cycle_shares * px
        account_value = realized_cash + cycle_mv
        cycle_ratio = (cycle_mv - cycle_invested) / cycle_invested if cycle_invested > 0 else 0.0
        cycle_peak_ratio = max(cycle_peak_ratio, cycle_ratio)

        cur_pr = (account_value - total_contributed) / total_contributed if total_contributed > 0 else 0.0
        peak_pr = max(peak_pr, cur_pr)
        max_giveback = max(max_giveback, peak_pr - cur_pr)

        triggered = _check_tp(cfg, pe_pct, cycle_ratio, cycle_peak_ratio, cycle_invested)
        if triggered:
            realized_cash += cycle_mv
            events.append({"date": d, "price": float(px), "cycle_return": cycle_ratio,
                           "cycle_invested": cycle_invested, "reason": triggered})
            cycle_shares = 0.0
            cycle_invested = 0.0
            cycle_peak_ratio = 0.0
            if not cfg.restart:
                stopped = True

        av_list.append(account_value)
        pr_list.append(cur_pr)

    account_value_s = pd.Series(av_list, index=price.index, dtype=float)
    profit_ratio_s = pd.Series(pr_list, index=price.index, dtype=float)
    final_value = float(account_value_s.iloc[-1]) if not account_value_s.empty else 0.0
    irr = _irr(contribs, price.index[-1], final_value)
    maxdd = _max_drawdown(account_value_s)
    return DcaResult(
        mode=cfg.tp_mode, account_value=account_value_s, profit_ratio=profit_ratio_s,
        events=events, total_contributed=total_contributed, final_value=final_value,
        irr=irr, max_drawdown=maxdd, max_giveback=max_giveback, contributions=contribs,
    )


def _check_tp(cfg: DcaConfig, pe_pct: float,
              cycle_ratio: float, cycle_peak_ratio: float, cycle_invested: float) -> Optional[str]:
    """判断当前是否触发止盈，返回原因字符串或 None。"""
    if cfg.tp_mode == "none" or cycle_invested <= 0:
        return None
    if cfg.tp_mode == "target" and cycle_ratio >= cfg.target_ret:
        return f"target>={cfg.target_ret:.0%}"
    if cfg.tp_mode == "valuation":
        if not pd.isna(pe_pct) and pe_pct >= cfg.tp_pe_pct:
            return f"pe_pct>={cfg.tp_pe_pct:.0f}%"
    if cfg.tp_mode == "trailing":
        if cycle_ratio > 0 and (cycle_peak_ratio - cycle_ratio) >= cfg.trail_giveback:
            return f"giveback>={cfg.trail_giveback:.0%}"
    return None


def _max_drawdown(nav: pd.Series) -> float:
    if nav.empty:
        return 0.0
    return float((nav / nav.cummax() - 1.0).min())


# ---------------------------------------------------------------------------
# 熊市拆解
# ---------------------------------------------------------------------------


def detect_bear_phases(price: pd.Series, threshold: float = -0.20) -> List[Dict[str, object]]:
    """用 20% zigzag 识别独立熊市区间：局部峰 → 谷 → 反弹确认（涨回20%）。

    避免"全历史最高点做锚"导致的多段熊市被合并问题。
    """
    t = abs(threshold)
    arr = price.values
    dates = price.index
    phases: List[Dict[str, object]] = []
    trend: Optional[str] = None
    ext_i = 0   # 当前摆动极值索引
    peak_i = 0
    for i in range(1, len(arr)):
        if trend in (None, "up"):
            if arr[i] > arr[ext_i]:
                ext_i = i
            elif arr[i] <= arr[ext_i] * (1 - t):
                trend, peak_i, ext_i = "down", ext_i, i
        else:  # down
            if arr[i] < arr[ext_i]:
                ext_i = i
            elif arr[i] >= arr[ext_i] * (1 + t):
                dd = arr[ext_i] / arr[peak_i] - 1.0
                if dd <= threshold:
                    phases.append({"peak": dates[peak_i], "trough": dates[ext_i],
                                   "recover": dates[i], "dd": dd})
                trend, ext_i = "up", i
    if trend == "down":
        dd = arr[ext_i] / arr[peak_i] - 1.0
        if dd <= threshold:
            phases.append({"peak": dates[peak_i], "trough": dates[ext_i],
                           "recover": None, "dd": dd})
    return phases


def bear_breakdown(price: pd.Series, results: Dict[str, DcaResult], threshold: float = -0.20) -> None:
    phases = detect_bear_phases(price, threshold)
    if not phases:
        print("未检测到 >=20% 回撤的熊市区间")
        return
    print(f"\n{'=' * 78}\n熊市区间拆解（指数回撤>=20%）：定投账户在谷底的浮盈亏\n{'=' * 78}")
    print(f"{'峰→谷':<26}{'指数跌幅':>9}{'反弹20%日':>13}" + "".join(f"{m:>13}" for m in results))
    for ph in phases:
        trough: pd.Timestamp = ph["trough"]  # type: ignore
        rec = ph["recover"]
        rec_s = rec.strftime("%Y-%m-%d") if rec is not None else "未收复"
        label = f"{ph['peak'].strftime('%Y-%m-%d')}→{trough.strftime('%Y-%m-%d')}"  # type: ignore
        cells = ""
        for m, r in results.items():
            pr = r.profit_ratio
            val = pr.loc[:trough]
            cells += f"{(val.iloc[-1] * 100 if not val.empty else 0.0):>12.1f}%"
        print(f"{label:<26}{ph['dd'] * 100:>8.1f}%{rec_s:>12}" + cells)  # type: ignore
    print("（单元格＝该模式定投账户在熊市谷底的累计浮盈/浮亏率；负值＝浮亏）")


# ---------------------------------------------------------------------------
# 汇总输出
# ---------------------------------------------------------------------------


def print_compare(results: Dict[str, DcaResult], cfg: DcaConfig) -> None:
    span = ""
    any_r = next(iter(results.values()))
    if not any_r.account_value.empty:
        span = f"{any_r.account_value.index[0].date()} ~ {any_r.account_value.index[-1].date()}"
    print(f"\n{'=' * 78}")
    print(f"指数定投 + 止盈回测 · {cfg.price_code} · {cfg.dca_mode} 定投 · 月投 {cfg.monthly:.0f} 元")
    print(f"区间 {span}\n{'=' * 78}")
    print(f"{'止盈模式':<12}{'累计投入':>11}{'期末市值':>11}{'总盈亏':>10}"
          f"{'年化IRR':>9}{'账户回撤':>9}{'利润回吐pp':>10}{'止盈次数':>8}")
    for m, r in results.items():
        profit = r.final_value - r.total_contributed
        print(f"{m:<12}{r.total_contributed / 1e4:>10.1f}万{r.final_value / 1e4:>10.1f}万"
              f"{profit / 1e4:>+9.1f}万{r.irr * 100:>8.2f}%{r.max_drawdown * 100:>8.1f}%"
              f"{r.max_giveback * 100:>9.1f}{len(r.events):>8}")
    print(f"\n注：账户回撤＝账户总市值最大回撤；利润回吐pp＝账面收益率从峰值最大回落的百分点（越小越能守住利润，少坐过山车）")


def print_events(results: Dict[str, DcaResult]) -> None:
    for m, r in results.items():
        if not r.events:
            continue
        ev = ", ".join(f"{e['date'].strftime('%Y-%m-%d')}({e['cycle_return'] * 100:+.0f}%)"  # type: ignore
                       for e in r.events)
        print(f"\n[{m}] 止盈点: {ev}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> DcaConfig:
    p = argparse.ArgumentParser(description="指数定投+止盈回测")
    p.add_argument("--start", default="20150101")
    p.add_argument("--end", default="20260922")
    p.add_argument("--monthly", type=float, default=1000.0)
    p.add_argument("--price-code", default="H00905.CSI")
    p.add_argument("--dca-mode", choices=["fixed", "valuation"], default="fixed")
    p.add_argument("--tp-mode", choices=["none", "target", "valuation", "trailing"], default=None,
                   help="指定单一模式；缺省则横向对比全部模式")
    p.add_argument("--target-ret", type=float, default=0.30)
    p.add_argument("--tp-pe-pct", type=float, default=80.0)
    p.add_argument("--trail-giveback", type=float, default=0.15)
    p.add_argument("--no-restart", action="store_true")
    a = p.parse_args(argv)
    return DcaConfig(
        price_code=a.price_code, start_date=_norm(a.start), end_date=_norm(a.end),
        monthly=a.monthly, dca_mode=a.dca_mode,
        tp_mode=a.tp_mode or "none", target_ret=a.target_ret, tp_pe_pct=a.tp_pe_pct,
        trail_giveback=a.trail_giveback, restart=not a.no_restart,
    ), a.tp_mode


def main() -> int:
    cfg, single = parse_args()
    price = load_price(cfg)
    pe = load_pe(cfg)
    print(f"价格 {len(price)} 天，估值 {len(pe)} 天", flush=True)

    modes = [single] if single else ["none", "target", "valuation", "trailing"]
    results: Dict[str, DcaResult] = {}
    for m in modes:
        c = DcaConfig(**{**cfg.__dict__, "tp_mode": m})
        results[m] = simulate(price, pe, c)

    print_compare(results, cfg)
    print_events(results)
    bear_breakdown(price, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
