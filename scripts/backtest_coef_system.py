#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replicate 孤舟's ETF "系数交易体系" with a reproducible oscillator.

His black-box "系数" is an overbought/oversold oscillator (~0..100+). We
substitute a transparent daily RSI(14) proxy and keep his MECHANICAL rules:

  建1: oscillator oversold      -> deploy 20% of planned capital
  建2: deeper oversold          -> deploy +20% (40% total)
  加仓: after 建2, every -3% drop -> deploy +10%, until 100% (加满躺平)
  减1: oscillator overbought    -> sell 50% of held units
  减2: stronger overbought      -> sell 50% of remainder, keep 利润底仓
  全程无止损 (no stop loss).

Signals are computed on close of day T and executed at OPEN of day T+1
(matches his "第二天介入"), so no lookahead.

The point is NOT to reproduce his exact gold-stock trades (we don't have his
系数), but to run the SAME mechanic across winners AND structural losers and
expose the martingale tail risk of "越跌越买 + 加满躺平 + 无止损".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import setup_env

setup_env()

import numpy as np
import pandas as pd

from src.services.local_db.store import LocalStockDB

# --- ETF basket: his cases (winners) + deliberate structural losers --------
ETF_UNIVERSE: List[tuple] = [
    ("517520.SH", "黄金股ETF",   "winner (his case)"),
    ("512880.SH", "证券ETF",     "mean-revert cyclical (his case)"),
    ("510500.SH", "中证500ETF",  "broad mean-revert"),
    ("512690.SH", "酒ETF",       "boom-bust"),
    ("515030.SH", "新能源车ETF", "boom-bust"),
    ("512760.SH", "芯片ETF",     "cyclical growth"),
    ("513050.SH", "中概互联ETF", "STRUCTURAL LOSER 21-24"),
    ("512010.SH", "医药ETF",     "STRUCTURAL LOSER 21-24"),
    ("512170.SH", "医疗ETF",     "STRUCTURAL LOSER 21-24"),
    ("513360.SH", "教育ETF",     "POLICY-CRUSHED (his own warned example)"),
    ("512200.SH", "房地产ETF",   "POLICY-CRUSHED"),
]

START = "20210101"
END = "20260922"


@dataclass
class CoefConfig:
    # RSI thresholds standing in for 系数 zones
    build1_rsi: float = 35.0      # 日线系数<10  -> oversold
    build2_rsi: float = 30.0      # 日线系数<0   -> deeper oversold
    reduce1_rsi: float = 70.0     # 120min系数≈100 -> overbought
    reduce2_rsi: float = 80.0     # 日线系数≈85-100 -> strong overbought
    add_step_pct: float = 0.03    # every -3% add
    max_adds: int = 6             # 6 adds * 10% = 60% -> 加满 (20+20+60)
    # planned-capital fractions (of original planned position P)
    f_build1: float = 0.20
    f_build2: float = 0.20        # incremental (build1 already spent 20)
    f_build2_direct: float = 0.40 # if 建2 hit with no 建1 first
    f_add: float = 0.10
    rsi_period: int = 14


@dataclass
class Result:
    code: str
    name: str
    tag: str
    total_return_pct: float       # honest economic return on planned capital
    annualized_pct: float
    max_drawdown_pct: float       # on marked portfolio value (cash+units*close)
    buyhold_pct: float            # buy&hold over same span
    max_deployed_pct: float       # peak cash deployed / P
    n_build1: int = 0
    n_build2: int = 0
    n_adds: int = 0
    n_reduce1: int = 0
    n_reduce2: int = 0
    years: float = 0.0
    ever_full_and_trapped: bool = False  # 加满 then portfolio < 70% of deployed
    worst_underwater_pct: float = 0.0    # worst (value-deployed)/deployed while invested


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(50.0)


def load_etf(code: str) -> pd.DataFrame:
    api = LocalStockDB()._api()
    df = api.fund_daily(ts_code=code, start_date=START, end_date=END)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_values("trade_date").reset_index(drop=True)
    return df[["trade_date", "open", "high", "low", "close", "vol"]]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def simulate(df: pd.DataFrame, cfg: CoefConfig, capital: float = 1_000_000.0) -> Result:
    code = df.attrs.get("code", "?")
    name = df.attrs.get("name", "?")
    tag = df.attrs.get("tag", "")

    close = df["close"].astype(float)
    op = df["open"].astype(float)
    r = rsi(close, cfg.rsi_period).to_numpy()
    n = len(df)

    cash = capital
    units = 0.0
    deployed_peak = 0.0
    total_deployed = 0.0          # cumulative cash ever put in (for trapped test)
    phase = "flat"                # flat | build1 | armed | full
    anchor = 0.0                  # 建2 exec price
    add_level = 0
    reduce_state = 0              # 0 none, 1 did 减1, 2 did 减2
    # exec queue: signal on close T -> execute at open T+1
    pending: Optional[str] = None
    pending_add = False

    counts = dict(b1=0, b2=0, adds=0, r1=0, r2=0)
    values: List[float] = []
    dates: List[str] = []
    ever_full = False
    trapped = False
    worst_uw = 0.0
    realized_back = 0.0  # cumulative sale proceeds (deployed - realized = cash in market)

    def buy(price: float, frac_cash: float) -> float:
        nonlocal cash, units, deployed_peak, total_deployed
        amt = min(frac_cash, cash)
        if amt <= 1e-9 or price <= 0:
            return 0.0
        cash -= amt
        units += amt / price
        total_deployed += amt
        deployed_peak = max(deployed_peak, total_deployed - realized_back)
        return amt

    def sell(price: float, sell_units: float) -> None:
        nonlocal cash, units, realized_back
        sell_units = min(sell_units, units)
        if sell_units <= 0 or price <= 0:
            return
        proceeds = sell_units * price
        cash += proceeds
        units -= sell_units
        realized_back += proceeds

    prev_r = r[0]
    for i in range(1, n):
        price_open = op.iloc[i]
        price_close = close.iloc[i]
        cur_r = r[i]

        # --- execute pending order at today's open ---
        if pending == "build1":
            if buy(price_open, capital * cfg.f_build1) > 0:
                counts["b1"] += 1
                phase = "build1"
                reduce_state = 0
        elif pending == "build2":
            frac = cfg.f_build2 if phase == "build1" else cfg.f_build2_direct
            if buy(price_open, capital * frac) > 0:
                counts["b2"] += 1
                phase = "armed"
                anchor = price_open
                add_level = 0
                reduce_state = 0
        elif pending == "reduce1":
            if units > 0:
                sell(price_open, units * 0.5)
                counts["r1"] += 1
                reduce_state = 1
        elif pending == "reduce2":
            if units > 0:
                sell(price_open, units * 0.5)
                counts["r2"] += 1
                reduce_state = 2
                phase = "flat"     # keep 底仓, allow fresh cycle
        pending = None

        # --- price-triggered adds (armed/full accumulation), at open ---
        if phase == "armed" and add_level < cfg.max_adds and anchor > 0:
            target = anchor * (1.0 - cfg.add_step_pct * (add_level + 1))
            if price_open <= target:
                if buy(price_open, capital * cfg.f_add) > 0:
                    counts["adds"] += 1
                    add_level += 1
                    if add_level >= cfg.max_adds:
                        phase = "full"
                        ever_full = True

        # --- mark portfolio at close ---
        value = cash + units * price_close
        values.append(value)
        dates.append(df["trade_date"].iloc[i])

        # trapped test: near-fully-invested and deep underwater vs peak deployment
        if ever_full and deployed_peak > 0:
            if total_deployed - realized_back >= 0.95 * deployed_peak:
                worst_uw = min(worst_uw, (value - deployed_peak) / deployed_peak)
                if value < 0.70 * deployed_peak:
                    trapped = True

        # --- signals on today's close -> queue for next open ---
        crossed_down_b1 = prev_r >= cfg.build1_rsi > cur_r
        crossed_down_b2 = prev_r >= cfg.build2_rsi > cur_r
        crossed_up_r1 = prev_r <= cfg.reduce1_rsi < cur_r
        crossed_up_r2 = prev_r <= cfg.reduce2_rsi < cur_r

        if phase in ("flat",) and crossed_down_b1 and cash > capital * cfg.f_build1 * 0.5:
            pending = "build1"
        elif phase in ("flat", "build1") and crossed_down_b2 and cash > capital * cfg.f_build2 * 0.5:
            pending = "build2"
        elif reduce_state == 0 and units > 0 and crossed_up_r1:
            pending = "reduce1"
        elif reduce_state == 1 and units > 0 and crossed_up_r2:
            pending = "reduce2"

        prev_r = cur_r

    final_value = cash + units * close.iloc[-1]
    total_return = (final_value / capital - 1.0) * 100.0
    years = max(n / 244.0, 1e-9)
    annualized = ((final_value / capital) ** (1.0 / years) - 1.0) * 100.0 if final_value > 0 else -100.0

    vs = pd.Series(values, index=pd.to_datetime(dates))
    roll_max = vs.cummax()
    dd = ((vs - roll_max) / roll_max * 100.0).min()

    bh = (close.iloc[-1] / close.iloc[0] - 1.0) * 100.0

    return Result(
        code=code, name=name, tag=tag,
        total_return_pct=round(total_return, 1),
        annualized_pct=round(annualized, 2),
        max_drawdown_pct=round(float(dd), 1),
        buyhold_pct=round(bh, 1),
        max_deployed_pct=round(deployed_peak / capital * 100.0, 1),
        n_build1=counts["b1"], n_build2=counts["b2"], n_adds=counts["adds"],
        n_reduce1=counts["r1"], n_reduce2=counts["r2"],
        years=round(years, 1),
        ever_full_and_trapped=trapped,
        worst_underwater_pct=round(worst_uw * 100.0, 1),
    )


def run() -> List[Result]:
    cfg = CoefConfig()
    out: List[Result] = []
    for code, name, tag in ETF_UNIVERSE:
        df = load_etf(code)
        if df.empty:
            print(f"[skip] {code} {name}: no data")
            continue
        df.attrs.update(code=code, name=name, tag=tag)
        res = simulate(df, cfg)
        out.append(res)
        print(f"[ok] {code} {name}: {res.years}y  ret={res.total_return_pct}%  "
              f"ann={res.annualized_pct}%  MDD={res.max_drawdown_pct}%  "
              f"B&H={res.buyhold_pct}%")
    return out


def print_table(results: List[Result]) -> None:
    print("\n" + "=" * 118)
    print("孤舟『系数交易体系』复刻  (RSI(14) 替代黑箱系数 | 建1/建2/每跌3%加仓/减1减半/减2留底仓/全程无止损)")
    print("窗口 2021-01 ~ 2026-09 | 计划仓位 100w | 信号次日开盘成交 (无未来函数)")
    print("=" * 118)
    hdr = (f"{'ETF':<12}{'类型':<26}{'真实总收益':>10}{'年化':>8}{'最大回撤':>9}"
           f"{'买入持有':>9}{'峰值投入':>9}{'最差浮亏':>9}{'建/加/减':>11}")
    print(hdr)
    print("-" * 118)
    for r in results:
        ops = f"{r.n_build2}/{r.n_adds}/{r.n_reduce1+r.n_reduce2}"
        flag = "  ⚠️加满被套" if r.ever_full_and_trapped else ""
        print(f"{r.name:<12}{r.tag:<26}{r.total_return_pct:>9}%{r.annualized_pct:>7}%"
              f"{r.max_drawdown_pct:>8}%{r.buyhold_pct:>8}%{r.max_deployed_pct:>8}%"
              f"{r.worst_underwater_pct:>8}%{ops:>11}{flag}")
    print("=" * 118)

    winners = [r for r in results if "winner" in r.tag or "cyclical" in r.tag or "mean-revert" in r.tag]
    losers = [r for r in results if "LOSER" in r.tag or "CRUSHED" in r.tag]
    if winners:
        avg_w = np.mean([r.total_return_pct for r in winners])
        avg_wdd = np.mean([r.max_drawdown_pct for r in winners])
        print(f"赢家/均值回归组 ({len(winners)}只): 平均真实收益 {avg_w:.1f}%  平均最大回撤 {avg_wdd:.1f}%")
    if losers:
        avg_l = np.mean([r.total_return_pct for r in losers])
        avg_ldd = np.mean([r.max_drawdown_pct for r in losers])
        trapped_n = sum(1 for r in losers if r.ever_full_and_trapped)
        print(f"输家/政策打压组 ({len(losers)}只): 平均真实收益 {avg_l:.1f}%  平均最大回撤 {avg_ldd:.1f}%  "
              f"加满被套 {trapped_n}/{len(losers)} 只")


def main() -> int:
    results = run()
    if not results:
        print("no results")
        return 1
    print_table(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
