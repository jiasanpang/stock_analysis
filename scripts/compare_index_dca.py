#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主流指数定投收益横向对比（独立脚本，纯本地数据）。

对每个指数跑两套定投方案并对比：
- 朴素定投：固定金额买入 + 不止盈（baseline）
- 估值组合：估值加权买入（便宜多买/贵了停投）+ 估值止盈（PE分位>阈值清仓）

计值统一用全收益指数（含分红再投）；估值信号用各指数自身 PE_TTM 扩展窗口分位。
依赖 scripts/backtest_dca.py 的回测引擎与 data/index_valuation/ 下的 PE 数据。

用法
----
    python scripts/compare_index_dca.py
    python scripts/compare_index_dca.py --start 20180101 --tp-pe-pct 85
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import pandas as pd

from backtest_dca import (  # noqa: E402
    DcaConfig, DcaResult, load_pe, load_price, simulate,
)

# (名称, 全收益指数代码, PE估值代码 or None)
INDICES: List[Tuple[str, str, Optional[str]]] = [
    ("沪深300", "H00300.CSI", "000300.SH"),
    ("中证500", "H00905.CSI", "000905.SH"),
    ("中证1000", "H00852.CSI", "000852.SH"),
    ("上证50", "H00016.CSI", "000016.SH"),
    ("创业板指", "399664.SZ", "399006.SZ"),
    ("中证红利", "H00922.CSI", None),
    ("红利低波", "H20269.CSI", None),
    ("红利低波100", "H20955.CSI", None),
]


def _val_path(pe_code: str) -> str:
    return f"data/index_valuation/{pe_code}.parquet"


def run_one(name: str, tr_code: str, pe_code: Optional[str],
            start: str, end: str, monthly: float, tp_pe_pct: float
            ) -> Tuple[DcaResult, Optional[DcaResult]]:
    """返回 (朴素定投结果, 估值组合结果或None)。"""
    base = DcaConfig(
        price_code=tr_code,
        valuation_path=_val_path(pe_code) if pe_code else "",
        start_date=start, end_date=end, monthly=monthly,
    )
    price = load_price(base)

    plain_cfg = replace(base, dca_mode="fixed", tp_mode="none")
    plain = simulate(price, pd.Series(dtype=float), plain_cfg)

    val: Optional[DcaResult] = None
    if pe_code and Path(_val_path(pe_code)).exists():
        pe = load_pe(base)
        val_cfg = replace(base, dca_mode="valuation", tp_mode="valuation", tp_pe_pct=tp_pe_pct)
        val = simulate(price, pe, val_cfg)
    return plain, val


def main() -> int:
    p = argparse.ArgumentParser(description="主流指数定投收益对比")
    p.add_argument("--start", default="20150101")
    p.add_argument("--end", default="20260922")
    p.add_argument("--monthly", type=float, default=1000.0)
    p.add_argument("--tp-pe-pct", type=float, default=80.0)
    a = p.parse_args()

    years = (pd.Timestamp(a.end) - pd.Timestamp(a.start)).days / 365.25
    print(f"\n{'=' * 92}")
    print(f"主流指数定投对比 · {a.start}~{a.end}（{years:.1f}年）· 月投 {a.monthly:.0f} 元 · 全收益口径")
    print(f"{'=' * 92}")
    print(f"{'指数':<10}{'朴素IRR':>9}{'朴素回撤':>10}{'朴素盈亏':>11}"
          f"{'估值组合IRR':>12}{'估值回撤':>10}{'估值盈亏':>11}{'止盈':>6}")
    print("-" * 92)

    for name, tr, pe in INDICES:
        try:
            plain, val = run_one(name, tr, pe, a.start, a.end, a.monthly, a.tp_pe_pct)
        except Exception as e:  # noqa: BLE001
            print(f"{name:<10} 跳过：{e}")
            continue
        p_profit = plain.final_value - plain.total_contributed
        line = (f"{name:<10}{plain.irr * 100:>8.2f}%{plain.max_drawdown * 100:>9.1f}%"
                f"{p_profit / 1e4:>+10.1f}万")
        if val is not None:
            v_profit = val.final_value - val.total_contributed
            line += (f"{val.irr * 100:>11.2f}%{val.max_drawdown * 100:>9.1f}%"
                     f"{v_profit / 1e4:>+10.1f}万{len(val.events):>6}")
        else:
            line += f"{'—':>11}{'—':>10}{'—':>11}{'—':>6}"
        print(line)

    print("-" * 92)
    print("注：IRR 为资金加权年化（已剔除投入金额差异，可横向比）；估值组合＝估值加权买入+PE分位止盈。")
    print("    中证红利无指数 PE 估值数据，仅列朴素定投。未计交易费用与红利税。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
