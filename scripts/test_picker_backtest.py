#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quick test for picker backtest (买卖点规则, cache, parallel fetch)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import setup_env

setup_env()

from src.services.picker_backtest_service import PickerBacktestService


def main():
    # Short range: ~5 trading days to minimize API calls
    service = PickerBacktestService()
    result = service.run(
        start_date="2024-11-01",
        end_date="2024-11-15",
        hold_days=5,
        top_n=3,
    )
    if "error" in result:
        print(f"Error: {result['error']}")
        return 1
    summary = result.get("summary", {})
    print("\n=== Picker Backtest Result ===")
    print(f"Dates: {summary.get('start_date')} ~ {summary.get('end_date')}")
    print(f"Total picks: {summary.get('total_picks')}, Win: {summary.get('win_count')}, Loss: {summary.get('loss_count')}")
    print(f"Win rate: {summary.get('win_rate_pct')}%, Avg return: {summary.get('avg_return_pct')}%")
    print(f"Alpha: {summary.get('alpha_vs_benchmark_pct')}%")
    print("\nSample results (first 5):")
    for r in result.get("results", [])[:5]:
        print(f"  {r['trade_date']} {r['code']} {r['name']}: entry={r['entry_price']:.2f} exit={r.get('exit_price')} ret={r.get('return_pct')}% {r['outcome']}")
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
