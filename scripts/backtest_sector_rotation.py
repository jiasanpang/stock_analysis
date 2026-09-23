#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业ETF"拥挤度/百分位"轮动回测（独立脚本，验证该思路是否靠谱）。

对应问题：有人用行业百分位（低=建、高=减）决定行业ETF买卖，这合理吗？
本脚本用申万一级31行业指数（data/sw_daily/）做三组检验：

1. 信号有效性：把"行业-月"按当期分位分桶，统计各桶的**前瞻1月/12月收益**。
   若低分位桶前瞻收益显著更高 → 反向信号成立；否则"低了就买"是接飞刀。
2. 策略对比（月度再平衡、等权）：
   - all31        : 等权持有全部31行业（baseline）
   - timing_sell  : 等权全部行业，但分位>=sell_pct 的行业当月清仓持现金（"高了就卖"）
   - contrarian_K : 只持有分位最低的 K 个行业（"低了就买"）
3. 卖出赢家的代价：timing_sell vs all31 的收益差，即"高分位清仓"少赚/多赚多少。

口径：信号=该行业**过去250日收益**在其自身历史中的扩展分位（仅用当日之前数据，无未来函数）。
数据：data/sw_daily/*.parquet（申万一级，2015-2026），需先用 Tushare sw_daily 同步。

用法
----
    python scripts/backtest_sector_rotation.py
    python scripts/backtest_sector_rotation.py --sell-pct 85 --top-k 8 --start 20160101
"""

from __future__ import annotations

import argparse
import glob
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.config import setup_env

setup_env()

TRADING_DAYS_PER_YEAR = 252
LOOKBACK = 250  # 信号回看窗口（约12个月）


@dataclass
class RotConfig:
    start_date: str = "20160101"   # 留足 LOOKBACK 历史
    end_date: str = "20260922"
    sell_pct: float = 85.0         # "高了就卖"阈值
    top_k: int = 8                 # 反向轮动持仓数
    sw_dir: str = "data/sw_daily"


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


def load_industries(cfg: RotConfig) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """返回 (close 宽表 index=日期 columns=行业代码, 代码->名称)。"""
    files = sorted(glob.glob(str(Path(cfg.sw_dir) / "*.parquet")))
    if not files:
        raise RuntimeError(f"{cfg.sw_dir} 为空，请先用 Tushare sw_daily 同步申万一级行业")
    closes: Dict[str, pd.Series] = {}
    names: Dict[str, str] = {}
    for f in files:
        df = pd.read_parquet(f)
        code = Path(f).stem
        df["d"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        s = df.sort_values("d").set_index("d")["close"].astype(float)
        s = s[~s.index.duplicated(keep="last")]
        closes[code] = s
        names[code] = str(df["name"].iloc[0]) if "name" in df.columns else code
    close = pd.DataFrame(closes).sort_index()
    close = close.loc[(close.index >= pd.Timestamp(cfg.start_date)) &
                      (close.index <= pd.Timestamp(cfg.end_date))]
    # 只保留区间内基本连续的行业
    close = close.dropna(axis=1, how="all")
    return close, names


def month_grid(index: pd.DatetimeIndex) -> List[pd.Timestamp]:
    s = pd.Series(index, index=index)
    return [pd.Timestamp(x) for x in s.resample("MS").first().dropna().tolist()]


# ---------------------------------------------------------------------------
# 信号：扩展分位（无未来函数）
# ---------------------------------------------------------------------------


def trailing_return(close: pd.DataFrame) -> pd.DataFrame:
    """过去 LOOKBACK 日收益。"""
    return close / close.shift(LOOKBACK) - 1.0


def expanding_percentile_at(tr: pd.Series, dates: List[pd.Timestamp], min_obs: int) -> Dict[pd.Timestamp, float]:
    """在给定日期上，计算 tr 当日值在其自身历史(<=当日)中的分位。"""
    out: Dict[pd.Timestamp, float] = {}
    vals = tr.dropna()
    arr_dates = vals.index
    arr = vals.values
    for d in dates:
        pos = arr_dates.searchsorted(d, side="right")  # 仅 <= d
        if pos < min_obs:
            out[d] = float("nan")
            continue
        cur = tr.get(d, np.nan)
        if pd.isna(cur):
            out[d] = float("nan")
            continue
        out[d] = float((arr[:pos] < cur).mean() * 100.0)
    return out


def build_signal(close: pd.DataFrame, months: List[pd.Timestamp], min_obs: int = 300
                 ) -> pd.DataFrame:
    """行业 x 月份 的分位信号表。"""
    tr = trailing_return(close)
    cols = {}
    for code in close.columns:
        cols[code] = pd.Series(expanding_percentile_at(tr[code], months, min_obs))
    sig = pd.DataFrame(cols)
    return sig.reindex(months)


# ---------------------------------------------------------------------------
# 信号有效性：分桶前瞻收益
# ---------------------------------------------------------------------------


def bucket_forward_returns(close: pd.DataFrame, sig: pd.DataFrame, months: List[pd.Timestamp]
                           ) -> pd.DataFrame:
    """各分位桶的前瞻1月/12月等权收益。"""
    fwd1 = close.pct_change().shift(-1).resample("MS").apply(lambda x: (1 + x).prod() - 1)
    fwd12 = close.shift(-12) / close - 1.0
    rows = []
    edges = [0, 10, 30, 50, 70, 90, 100.01]
    for lo, hi in zip(edges[:-1], edges[1:]):
        m1, m12 = [], []
        for d in months:
            if d not in sig.index:
                continue
            sel = sig.loc[d]
            mask = (sel >= lo) & (sel < hi)
            codes = sel[mask].dropna().index
            if len(codes) == 0:
                continue
            if d in fwd1.index:
                v1 = fwd1.loc[d, codes].mean()
                if pd.notna(v1):
                    m1.append(v1)
            for c in codes:
                if d in fwd12.index and c in fwd12.columns:
                    v12 = fwd12.loc[d, c]
                    if pd.notna(v12):
                        m12.append(v12)
        rows.append({
            "bucket": f"{lo:.0f}-{hi:.0f}%",
            "n": len(m1),
            "fwd1m": np.mean(m1) * 100 if m1 else float("nan"),
            "fwd12m": np.mean(m12) * 100 if m12 else float("nan"),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 策略回测
# ---------------------------------------------------------------------------


def _metrics(monthly: pd.Series) -> Dict[str, float]:
    if monthly.empty or len(monthly) < 2:
        return {"cagr": 0.0, "vol": 0.0, "sharpe": 0.0, "maxdd": 0.0, "total": 0.0}
    nav = (1 + monthly).cumprod()
    years = len(monthly) / 12.0
    total = float(nav.iloc[-1] - 1)
    cagr = float(nav.iloc[-1] ** (1 / years) - 1) if nav.iloc[-1] > 0 else -1.0
    vol = float(monthly.std() * math.sqrt(12))
    sharpe = float(monthly.mean() / monthly.std() * math.sqrt(12)) if monthly.std() else 0.0
    dd = float((nav / nav.cummax() - 1).min())
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "maxdd": dd, "total": total}


def run_strategies(close: pd.DataFrame, sig: pd.DataFrame, months: List[pd.Timestamp],
                   cfg: RotConfig) -> Dict[str, pd.Series]:
    """返回各策略的月度收益序列。"""
    ret_m = close.pct_change().resample("MS").apply(lambda x: (1 + x).prod() - 1)
    ret_m = ret_m.reindex(months).fillna(0.0)
    codes = list(close.columns)

    def portfolio_monthly(weights_fn) -> pd.Series:
        out = []
        prev_w: Optional[pd.Series] = None
        turnover = []
        for i, d in enumerate(months[:-1]):
            w = weights_fn(d)
            w = w.reindex(codes).fillna(0.0)
            if prev_w is not None:
                turnover.append(float((w - prev_w).abs().sum() / 2))
            prev_w = w
            nxt = months[i + 1]
            r = ret_m.loc[nxt, codes].fillna(0.0)
            out.append(float((w * r).sum()))
        s = pd.Series(out, index=months[1:], dtype=float)
        return s

    def w_all(d):
        return pd.Series(1.0 / len(codes), index=codes)

    def w_sell(d):
        row = sig.loc[d] if d in sig.index else pd.Series(dtype=float)
        keep = [c for c in codes if pd.isna(row.get(c, np.nan)) or row.get(c) < cfg.sell_pct]
        if not keep:
            return pd.Series(0.0, index=codes)
        return pd.Series({c: (1.0 / len(keep) if c in keep else 0.0) for c in codes})

    def w_contra(d):
        row = sig.loc[d] if d in sig.index else pd.Series(dtype=float)
        valid = row.dropna()
        if valid.empty:
            return pd.Series(0.0, index=codes)
        low = valid.nsmallest(cfg.top_k).index
        return pd.Series({c: (1.0 / len(low) if c in low else 0.0) for c in codes})

    return {
        "all31(等权持有)": portfolio_monthly(w_all),
        f"timing_sell(>={cfg.sell_pct:.0f}清仓)": portfolio_monthly(w_sell),
        f"contrarian_K(最低{cfg.top_k})": portfolio_monthly(w_contra),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="行业百分位轮动回测")
    p.add_argument("--start", default="20160101")
    p.add_argument("--end", default="20260922")
    p.add_argument("--sell-pct", type=float, default=85.0)
    p.add_argument("--top-k", type=int, default=8)
    a = p.parse_args()
    cfg = RotConfig(start_date=a.start, end_date=a.end, sell_pct=a.sell_pct, top_k=a.top_k)

    close, names = load_industries(cfg)
    months = month_grid(close.index)
    print(f"行业 {len(close.columns)} 个，月份 {len(months)}，区间 "
          f"{close.index[0].date()}~{close.index[-1].date()}", flush=True)

    sig = build_signal(close, months)

    print(f"\n{'=' * 70}\n① 信号有效性：分位桶 → 前瞻收益（等权，%）\n{'=' * 70}")
    bk = bucket_forward_returns(close, sig, months)
    print(bk.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    print("（若'低分位桶'前瞻收益不高于'高分位桶'，则反向信号不成立）")

    results = run_strategies(close, sig, months, cfg)
    print(f"\n{'=' * 70}\n② 策略对比（月度再平衡、等权）\n{'=' * 70}")
    print(f"{'策略':<26}{'年化':>8}{'波动':>8}{'Sharpe':>8}{'最大回撤':>9}{'累计':>9}")
    for name, mr in results.items():
        m = _metrics(mr)
        print(f"{name:<26}{m['cagr'] * 100:>7.2f}%{m['vol'] * 100:>7.1f}%"
              f"{m['sharpe']:>8.2f}{m['maxdd'] * 100:>8.1f}%{m['total'] * 100:>8.1f}%")

    base = _metrics(results["all31(等权持有)"])
    sell_key = [k for k in results if k.startswith("timing_sell")][0]
    contra_key = [k for k in results if k.startswith("contrarian_K")][0]
    sell = _metrics(results[sell_key])
    contra = _metrics(results[contra_key])
    print(f"\n③ 卖出赢家的代价：timing_sell 年化 {sell['cagr'] * 100:.2f}% - all31 {base['cagr'] * 100:.2f}% "
          f"= {(sell['cagr'] - base['cagr']) * 100:+.2f}%/年")
    print(f"   反向轮动 vs 等权：{(contra['cagr'] - base['cagr']) * 100:+.2f}%/年")
    return 0


if __name__ == "__main__":
    sys.exit(main())
