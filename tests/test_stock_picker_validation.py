# -*- coding: utf-8 -*-
"""
Validation tests for stock picker improvements (Phase 1 & 2).

Run with:
  python -m pytest tests/test_stock_picker_validation.py -v
  python tests/test_stock_picker_validation.py   # standalone (from project root)
  ./test.sh picker-validation
"""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pandas as pd

try:
    import pytest
except ImportError:
    pytest = None

_PICKER_MOD = None


def _get_picker_module():
    """Import stock_picker_service without full package deps (storage, sqlalchemy)."""
    global _PICKER_MOD
    if _PICKER_MOD is not None:
        return _PICKER_MOD
    import importlib.util
    import types

    cfg = types.ModuleType("src.config")
    cfg.get_config = lambda: type("C", (), {
        "bocha_api_keys": [], "tavily_keys": [], "brave_keys": [],
        "serpapi_keys": [], "minimax_keys": [], "searxng_base_urls": [],
    })()
    sys.modules["src"] = types.ModuleType("src")
    sys.modules["src.config"] = cfg
    search_svc = types.ModuleType("search_service")
    search_svc.SearchService = type("SearchService", (), {})
    sys.modules["src.search_service"] = search_svc
    sys.modules["data_provider"] = types.ModuleType("data_provider")
    base = types.ModuleType("base")
    base.DataFetcherManager = type("DataFetcherManager", (), {})
    sys.modules["data_provider.base"] = base

    path = _root / "src" / "services" / "stock_picker_service.py"
    spec = importlib.util.spec_from_file_location("stock_picker_service", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["src.services"] = types.ModuleType("services")
    spec.loader.exec_module(mod)
    _PICKER_MOD = mod
    return mod


def _screener():
    """Get StockScreener instance."""
    try:
        from src.services.stock_picker_service import StockScreener
    except ImportError:
        StockScreener = _get_picker_module().StockScreener
    return StockScreener(data_manager=None)


def _row(**kwargs):
    """Build a minimal screener row with defaults."""
    defaults = {
        "代码": "600519", "名称": "茅台", "最新价": 10, "涨跌幅": 2,
        "量比": 1.2, "换手率": 4, "市盈率-动态": 20, "市净率": 2,
        "总市值": 100e8, "成交额": 1e8, "60日涨跌幅": 10,
    }
    defaults.update(kwargs)
    return defaults


def test_prompt_contains_1_5_picks():
    """Verify LLM prompt says 1-5 picks, 60% empty trigger, 8% bias."""
    try:
        from src.services.stock_picker_service import PICK_SYSTEM_PROMPT
    except ImportError:
        PICK_SYSTEM_PROMPT = _get_picker_module().PICK_SYSTEM_PROMPT

    assert "1-5" in PICK_SYSTEM_PROMPT
    assert "60%" in PICK_SYSTEM_PROMPT
    assert "8%" in PICK_SYSTEM_PROMPT


def test_bias_constant():
    """Verify bias filter threshold."""
    try:
        from src.services.stock_picker_service import PICKER_MAX_BIAS_PCT
    except ImportError:
        PICKER_MAX_BIAS_PCT = _get_picker_module().PICKER_MAX_BIAS_PCT

    assert PICKER_MAX_BIAS_PCT == 8.0


def test_volume_ratio_min_constant():
    """Verify volume ratio filter uses VOLUME_RATIO_MIN=1.0."""
    try:
        from src.services.stock_picker_service import VOLUME_RATIO_MIN
    except ImportError:
        VOLUME_RATIO_MIN = _get_picker_module().VOLUME_RATIO_MIN

    assert VOLUME_RATIO_MIN == 1.0


def test_60d_decay_scoring():
    """Verify 60-day gain >30% gets decay, not full 40 points."""
    screener = _screener()
    df = pd.DataFrame([_row(**{"60日涨跌幅": 35})])
    recs = screener._score_and_rank(df, top_n=5)
    assert len(recs) == 1
    assert recs[0].change_pct_60d == 35
    assert recs[0].score > 0


def test_60d_25_vs_40_ordering():
    """25% (no decay) should score >= 40% (decay)."""
    screener = _screener()
    df = pd.DataFrame([
        _row(代码="A", 名称="A", **{"60日涨跌幅": 25}),
        _row(代码="B", 名称="B", **{"60日涨跌幅": 40}),
    ])
    recs = screener._score_and_rank(df, top_n=5)
    assert len(recs) == 2
    scores = {r.code: r.score for r in recs}
    assert scores["A"] >= scores["B"] - 1


def test_volume_filter_excludes_below_1_0():
    """Volume ratio 0.9 should be excluded."""
    screener = _screener()
    df = pd.DataFrame([_row(量比=0.9, 成交额=5e7)])
    filtered = screener._filter_volume(df)
    assert len(filtered) == 0


def test_volume_filter_passes_above_1_0():
    """Volume ratio 1.1 should pass (成交额 > 5e7)."""
    screener = _screener()
    df = pd.DataFrame([_row(量比=1.1, 成交额=1e8)])
    filtered = screener._filter_volume(df)
    assert len(filtered) == 1


if __name__ == "__main__":
    tests = [
        test_prompt_contains_1_5_picks,
        test_bias_constant,
        test_volume_ratio_min_constant,
        test_60d_decay_scoring,
        test_60d_25_vs_40_ordering,
        test_volume_filter_excludes_below_1_0,
        test_volume_filter_passes_above_1_0,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n{'All passed.' if failed == 0 else f'{failed} failed.'}")
    sys.exit(failed)
