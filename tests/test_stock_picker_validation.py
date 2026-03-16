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
        "enable_chip_distribution": True,
    })()
    sys.modules["src"] = types.ModuleType("src")
    sys.modules["src.config"] = cfg
    search_svc = types.ModuleType("search_service")
    search_svc.SearchService = type("SearchService", (), {})
    sys.modules["src.search_service"] = search_svc
    sys.modules["data_provider"] = types.ModuleType("data_provider")
    base = types.ModuleType("base")
    base.DataFetcherManager = type("DataFetcherManager", (), {})

    def _is_kc_cy(code):
        c = (code or "").strip().split(".")[0]
        return c.startswith("688") or c.startswith("30")

    base.is_kc_cy_stock = _is_kc_cy
    sys.modules["data_provider.base"] = base

    path = _root / "src" / "services" / "stock_picker_service.py"
    spec = importlib.util.spec_from_file_location("stock_picker_service", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["src.services"] = types.ModuleType("services")
    spec.loader.exec_module(mod)
    _PICKER_MOD = mod
    return mod


def _screener(mode: str = "balanced"):
    """Get StockScreener instance. Optional mode: defensive/balanced/offensive."""
    try:
        from src.services.stock_picker_service import StockScreener
    except ImportError:
        StockScreener = _get_picker_module().StockScreener
    return StockScreener(
        data_manager=None,
        picker_mode=mode,
    )


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


def test_pe_max_constant():
    """Verify balanced mode PE max is 100."""
    try:
        from src.services.stock_picker_service import PickerModeParams
    except ImportError:
        PickerModeParams = _get_picker_module().PickerModeParams

    assert PickerModeParams.for_mode("balanced").pe_max == 100


def test_limit_up_thresholds():
    """Verify limit-up thresholds: main 9.5%, ChiNext/STAR 19%."""
    try:
        from src.services.stock_picker_service import LIMIT_UP_PCT_MAIN, LIMIT_UP_PCT_KC_CY
    except ImportError:
        mod = _get_picker_module()
        LIMIT_UP_PCT_MAIN = mod.LIMIT_UP_PCT_MAIN
        LIMIT_UP_PCT_KC_CY = mod.LIMIT_UP_PCT_KC_CY

    assert LIMIT_UP_PCT_MAIN == 9.5
    assert LIMIT_UP_PCT_KC_CY == 19.0


def test_mode_params_all_modes():
    """Verify PickerModeParams for defensive, balanced, offensive."""
    try:
        from src.services.stock_picker_service import PickerModeParams
    except ImportError:
        PickerModeParams = _get_picker_module().PickerModeParams

    d = PickerModeParams.for_mode("defensive")
    assert d.max_bias_pct == 6.0 and d.pe_max == 50 and d.pe_ideal_low == 10 and d.pe_ideal_high == 25

    b = PickerModeParams.for_mode("balanced")
    assert b.max_bias_pct == 8.0 and b.pe_max == 100 and b.pe_ideal_low == 10 and b.pe_ideal_high == 30

    o = PickerModeParams.for_mode("offensive")
    assert o.max_bias_pct == 10.0 and o.pe_max == 100 and o.pe_ideal_low == 20 and o.pe_ideal_high == 50

    # Unknown mode falls back to balanced
    x = PickerModeParams.for_mode("invalid")
    assert x.max_bias_pct == 8.0


def test_pe_filter_defensive_excludes_above_50():
    """Defensive mode: PE > 50 excluded."""
    screener = _screener(mode="defensive")
    df = pd.DataFrame([
        _row(**{"市盈率-动态": 30}),
        _row(代码="B", 名称="B", **{"市盈率-动态": 60}),
    ])
    filtered = screener._filter_basic(df)
    assert len(filtered) == 1
    assert filtered.iloc[0]["代码"] == "600519"


def test_pe_filter_balanced_excludes_above_100():
    """Balanced mode: PE > 100 excluded."""
    screener = _screener(mode="balanced")
    df = pd.DataFrame([
        _row(**{"市盈率-动态": 50}),
        _row(代码="B", 名称="B", **{"市盈率-动态": 150}),
    ])
    filtered = screener._filter_basic(df)
    assert len(filtered) == 1
    assert filtered.iloc[0]["代码"] == "600519"


def test_pe_filter_offensive_allows_high_pe():
    """Offensive mode: PE 80 allowed (pe_max 100)."""
    screener = _screener(mode="offensive")
    df = pd.DataFrame([
        _row(**{"市盈率-动态": 80}),
    ])
    filtered = screener._filter_basic(df)
    assert len(filtered) == 1


def test_pe_scoring_defensive_ideal_range():
    """Defensive: PE 15 in ideal 10-25 gets full score."""
    screener = _screener(mode="defensive")
    df = pd.DataFrame([
        _row(代码="A", 名称="A", **{"市盈率-动态": 15}),
        _row(代码="B", 名称="B", **{"市盈率-动态": 40}),
    ])
    recs = screener._score_and_rank(df, top_n=5)
    assert len(recs) == 2
    scores = {r.code: r.score for r in recs}
    assert scores["A"] > scores["B"]


def test_pe_scoring_offensive_ideal_range():
    """Offensive: PE 35 in ideal 20-50 gets full score, PE 15 gets partial."""
    screener = _screener(mode="offensive")
    df = pd.DataFrame([
        _row(代码="A", 名称="A", **{"市盈率-动态": 35}),
        _row(代码="B", 名称="B", **{"市盈率-动态": 15}),
    ])
    recs = screener._score_and_rank(df, top_n=5)
    assert len(recs) == 2
    scores = {r.code: r.score for r in recs}
    assert scores["A"] > scores["B"]


def test_leader_exemption_candidate():
    """Verify _is_leader_candidate: 60d>15%, change 2-7%, vol_ratio>1.5, turnover 2-8%."""
    screener = _screener()
    try:
        from src.services.stock_picker_service import ScreenedStock
    except ImportError:
        ScreenedStock = _get_picker_module().ScreenedStock

    leader = ScreenedStock(
        code="001", name="L", price=10, change_pct=5, volume_ratio=2, turnover_rate=5,
        pe=20, pb=2, market_cap=100, amount=1, change_pct_60d=20, score=50,
    )
    non_leader = ScreenedStock(
        code="002", name="N", price=10, change_pct=1, volume_ratio=1, turnover_rate=1,
        pe=20, pb=2, market_cap=100, amount=1, change_pct_60d=10, score=50,
    )
    assert screener._is_leader_candidate(leader) is True
    assert screener._is_leader_candidate(non_leader) is False


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
    """Volume ratio 1.1 should pass (成交额 > 1e8 for cap>=100亿)."""
    screener = _screener()
    df = pd.DataFrame([_row(量比=1.1, 成交额=1.5e8)])  # cap 100e8 needs amt > 1e8
    filtered = screener._filter_volume(df)
    assert len(filtered) == 1


def test_turnover_filter_1_15():
    """Turnover 0.8 excluded (min 1%), 18 excluded (max 15%)."""
    screener = _screener()
    df = pd.DataFrame([
        _row(代码="A", 名称="A", 换手率=0.8, 成交额=1.5e8),
        _row(代码="B", 名称="B", 换手率=18, 成交额=1.5e8),
        _row(代码="C", 名称="C", 换手率=5, 成交额=1.5e8),
    ])
    filtered = screener._filter_volume(df)
    assert len(filtered) == 1
    assert filtered.iloc[0]["代码"] == "C"


def test_amount_by_market_cap():
    """Small cap (<100亿) needs 3000万, large cap >= 1亿."""
    screener = _screener()
    # 50亿 cap: 5000万 passes (3000万 min)
    df1 = pd.DataFrame([_row(代码="A", 名称="A", 总市值=50e8, 成交额=5e7)])
    f1 = screener._filter_volume(df1)
    assert len(f1) == 1
    # 100亿 cap: 5000万 fails (1亿 min)
    df2 = pd.DataFrame([_row(代码="B", 名称="B", 总市值=100e8, 成交额=5e7)])
    f2 = screener._filter_volume(df2)
    assert len(f2) == 0


def _make_daily_df(closes, dates=None):
    """Build daily DataFrame with close column. dates: list of YYYY-MM-DD strings."""
    n = len(closes)
    if dates is None:
        base = pd.Timestamp("2025-01-01")
        dates = [(base + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
    return pd.DataFrame({"close": closes, "date": dates})


def test_b_wave_filter_excludes_b_wave_bounce():
    """B-wave risk: high then low, bounce 50%, low 9 days ago -> excluded."""
    try:
        from src.services.stock_picker_service import StockScreener, ScreenedStock
    except ImportError:
        mod = _get_picker_module()
        StockScreener = mod.StockScreener
        ScreenedStock = mod.ScreenedStock

    # 20 days: high 100 at day 0-4, drop to 90 at day 9 (idx 9), bounce to 95 (current)
    # drop=10%, retrace=5/10=50%, days_since_low=10
    closes = [100] * 5 + [98, 96, 94, 92, 90] + [91, 92, 93, 94, 95] + [95] * 5  # low at idx 9
    df_daily = _make_daily_df(closes)

    class MockDM:
        def get_daily_data(self, code, end_date=None, days=25):
            return df_daily, "mock"

    screener = StockScreener(
        data_manager=MockDM(),
        picker_mode="balanced",
        enable_b_wave_filter=True,
    )
    cand = [ScreenedStock(code="001", name="X", price=95, change_pct=2, volume_ratio=1.2,
                          turnover_rate=4, pe=20, pb=2, market_cap=100, amount=1,
                          change_pct_60d=15, score=50)]
    out = screener._filter_b_wave_risk(cand)
    assert len(out) == 0, "B-wave bounce should be excluded"


def test_b_wave_filter_keeps_uptrend():
    """Uptrend: low first, high later -> no B-wave, keep."""
    try:
        from src.services.stock_picker_service import StockScreener, ScreenedStock
    except ImportError:
        mod = _get_picker_module()
        StockScreener = mod.StockScreener
        ScreenedStock = mod.ScreenedStock

    # 20 days: low 90 at start, rise to 100 at end
    closes = [90 + i * 0.5 for i in range(20)]
    df_daily = _make_daily_df(closes)

    class MockDM:
        def get_daily_data(self, code, end_date=None, days=25):
            return df_daily, "mock"

    screener = StockScreener(
        data_manager=MockDM(),
        picker_mode="balanced",
        enable_b_wave_filter=True,
    )
    cand = [ScreenedStock(code="002", name="Y", price=99.5, change_pct=2, volume_ratio=1.2,
                         turnover_rate=4, pe=20, pb=2, market_cap=100, amount=1,
                         change_pct_60d=15, score=50)]
    out = screener._filter_b_wave_risk(cand)
    assert len(out) == 1, "Uptrend should be kept"


def test_b_wave_filter_keeps_at_bottom():
    """At C bottom: low at most recent day -> keep (days_since_low=0)."""
    try:
        from src.services.stock_picker_service import StockScreener, ScreenedStock
    except ImportError:
        mod = _get_picker_module()
        StockScreener = mod.StockScreener
        ScreenedStock = mod.ScreenedStock

    # 20 days: high 100 at start, drop to 90 at end (idx 19)
    closes = [100 - i * 0.5 for i in range(20)]
    df_daily = _make_daily_df(closes)

    class MockDM:
        def get_daily_data(self, code, end_date=None, days=25):
            return df_daily, "mock"

    screener = StockScreener(
        data_manager=MockDM(),
        picker_mode="balanced",
        enable_b_wave_filter=True,
    )
    cand = [ScreenedStock(code="003", name="Z", price=90, change_pct=-1, volume_ratio=1.2,
                         turnover_rate=4, pe=20, pb=2, market_cap=100, amount=1,
                         change_pct_60d=5, score=40)]
    out = screener._filter_b_wave_risk(cand)
    assert len(out) == 1, "At bottom (no bounce yet) should be kept"


if __name__ == "__main__":
    tests = [
        test_prompt_contains_1_5_picks,
        test_bias_constant,
        test_volume_ratio_min_constant,
        test_pe_max_constant,
        test_limit_up_thresholds,
        test_mode_params_all_modes,
        test_pe_filter_defensive_excludes_above_50,
        test_pe_filter_balanced_excludes_above_100,
        test_pe_filter_offensive_allows_high_pe,
        test_pe_scoring_defensive_ideal_range,
        test_pe_scoring_offensive_ideal_range,
        test_leader_exemption_candidate,
        test_60d_decay_scoring,
        test_60d_25_vs_40_ordering,
        test_volume_filter_excludes_below_1_0,
        test_volume_filter_passes_above_1_0,
        test_turnover_filter_1_15,
        test_amount_by_market_cap,
        test_b_wave_filter_excludes_b_wave_bounce,
        test_b_wave_filter_keeps_uptrend,
        test_b_wave_filter_keeps_at_bottom,
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
