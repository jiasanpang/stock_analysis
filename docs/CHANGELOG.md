# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> For user-friendly release highlights, see the [GitHub Releases](https://github.com/jiasanpang/stock_analysis/releases) page.

## [Unreleased]

### Refactor — 选股策略精简：下线 reversal_breakout 与 small_cap

- 删除 `reversal_breakout`（反转突破）与 `small_cap`（小市值）两个策略：
  实际交易中不再使用。移除专属模块（`screener/reversal_breakout.py`、
  `screener/small_cap.py`）、MarketGuard RB 门控、trade_levels RB 出场规则、
  回测器专属持仓窗口/再平衡分支、API/前端选项与相关文档章节。
- `PICKER_STRATEGIES` 合法值收敛为 `buy_pullback, bottom_reversal`；
  历史数据库中的旧 strategy_id 记录保持可读，不做迁移。
- **Fix**：弱市 regime 白名单会在 -2% 门控之外再次移除 `buy_pullback`
  导致 LUP 弱市日 0 票；LUP 开启时自动将 `buy_pullback` 加入白名单
  （LUP 自身红线 + 3 天持仓已具限损能力）。

### Feat — 涨停回踩买入法引擎接管 buy_pullback（纯 tushare，LUP）

- **新引擎** `src/services/picker/screener/limit_up_pullback.py`：事件驱动
  涨停回踩选股。tushare `limit_list_d` 锚点池（缺权限时日线推导兜底）→
  涨停日质检（红线否决 + 教科书区间加分，阈值经 4142 个真实涨停样本校准）→
  5 日观察窗内五大形态确认（三阴不破阳/龙回头/回踩5日线/回踩10日线/假阴真阳）
  → 结构化买卖点（`compute_limit_up_pullback_levels`）。
- **卖出规则** `src/services/limit_up_rules.py`：+10% 减半仓 → MA10/ATR×2.5
  移动止损 → 3 日无 +5% 时间止损 / 最长 3 天持仓（超短风格）；position_tracker
  与回测器同步路由。
- **MarketGuard 放宽**：LUP 下 `BUY_PULLBACK_GATE_PCT` 默认 +2% → **-2%**
  （旧链路不变）。原 +2% 门控在回测中砍掉 47/65 个交易日且无 PF 收益。
- **`PICKER_DISABLE_AI=1`**：Stage 2 跳过新闻/LLM，直出量化选股+买卖价位；
  LLM 降级路径复用同一 `_quant_only_result`。
- **观察池**：复盘 V2 `screen_watch_pool` 优先输出 LUP 观察池
  （`limit_up_watch_candidates`），板块热度法降为兜底。
- **Fix**：`merge_candidates_by_code` 丢失 `limit_up_date/secondary_buy/setup`
  导致下游（回测出场路由、API 展示）静默回退旧链路；已随合并传递。
- **回测重基线**（2026-02-02~05-15，65 交易日）：11 笔，WR 63.6%，
  AvgRet +1.53%，**PF 2.31**，MDD 3.86%，+21.3% vs 基准 +5.5%。消融实验：
  缩量红线放宽（0.6→0.8）召回 6 倍但 PF 0.95，维持默认。
- 回滚：`LUP_ENABLED=0` 恢复旧横截面链路；`LUP_EXIT=0` 恢复旧卖出规则。

### Feat — 复盘 V2：四步式深度复盘 + 周度复盘 + 持仓复盘

- **V2 日报**（`--review-version v2`）：四步法结构（`src/core/market_review.py`
  分流，`src/_review_v2_*` 类型与提示词），涨停梯队/北向数据经
  `data_provider/akshare/limit_up.py` 采集。
- **周度复盘** `src/core/weekly_review.py` + `--weekly-review` CLI +
  `.github/workflows/weekly_review.yml`（每周六定时）。
- **持仓复盘** `src/core/holdings_review.py`、**次日观察池**
  `src/core/watch_pool.py`（LUP 观察池优先、板块热度兜底）。
- 附带：修复 `test_us_index_mapping` 的 sys.path 污染（改为 importlib 按文件
  加载，消除本地 `data_provider/akshare` 目录遮蔽 pip `akshare` 包）。

### Fixed — secondary_buy 与 ideal_buy 价格区间在报告 UI 重叠

- `trade_levels._resolve_entry_anchor` 在 `buy_pullback` 策略下用
  `current_price * 0.97` 作为 `secondary_buy`。当现价贴近 MA5 时，
  secondary 仅低于 ideal 约 3%，LLM 把两个单点各扩成 ±1.5% 的价格区间后
  视觉上完全贴边（如 ideal 18.5-18.8 vs secondary 18.2-18.5），用户
  看不出两档买点的区别。
- 修复：secondary_buy 改为锚定 MA10（其次 MA20、最后 ideal*0.96 兜底），
  并强制 `secondary ≤ ideal * 0.98`，保证两档买点至少 2% 间距。
  `bottom_reversal` 的 secondary 也顺手收紧为优先取 `recent_low`。
- 顺手：把过期的 `test_bottom_reversal_20pct_hardcap` 更新到现行的
  +35% hardcap，新增 2 条 secondary_buy 回归测试。

### Picker — drop `breakout` strategy (PF<1 across two backtest windows)

- **Removed `breakout`** from the picker strategy lineup. Two independent
  walk-forward backtests both confirmed sub-1.0 Profit Factor with negative
  alpha even after the May-2026 `hold_days 10→5` tuning:
  - 2024-07 ~ 2025-04 (10mo): WR 36.1%, AvgRet -0.90%, PF 0.71, MDD 73.2%
  - 2025-05 ~ 2026-05 (12mo): WR 40.2%, AvgRet -0.53%, PF 0.78,
    total -64.4% vs HS300 +24.4% vs equal-weight market +40.9%
- A/B saturation: tightening entry bands and the break-confirmation
  threshold (1.01) gave no incremental alpha — top-N scoring already
  selects the strongest candidates, so generic filter tightening has no
  effect on the picks that matter.
- Net delta surface removed: `BREAKOUT` constant, `BREAKOUT_PARAMS`, all
  `BO_*` env helpers, `score_breakout` scorer, `_STRATEGY_PARAMS`
  registry entry, breakout-only checks in `risk_filters.py` (long-upper-
  shadow + N-day high confirmation), `BREAKOUT` config and entry-anchor
  branch in `trade_levels.py`, the `picker_backtest_service` clamp,
  `DAILY_DATA_STRATEGIES` / `SECTOR_FILTER_STRATEGIES` set members,
  frontend `STRATEGY_OPTIONS` / `PickerStrategy` union / labels, API
  schema descriptions, and `docs/picker-strategies-guide.md` §3.2.
- `reversal_breakout` (right-side breakout of a deep base, validated
  PF 1.24 / +1.06% alpha) is unaffected — different mixin, different
  pipeline dispatch.

### Picker strategies — split bottom_reversal into watchlist + actionable

- **New strategy `reversal_breakout`** (反转突破): right-side swing entry
  triggered only after a deep-bottom stock breaks out of its 40d base
  with conviction. Requires the prior base (180d max DD ≥ 25%, 40d
  range ≤ 25%, MA60 not falling hard) AND a same-day trigger: close
  ≥ 40d-high × 1.01, pct_chg ∈ [3%, 7%], volume ≥ 60d-avg × 2, and
  main-force net inflow / today's amount ≥ 5%. Hold ~20 trading days,
  +25% hardcap, ATR2 trailing from +12%, -6% hard floor, 20d time
  stop. Backtest 2024-07~2025-04 (10mo incl. 9/24 rally):
  WR 33.7%, PF 1.24, alpha vs HS300 +1.06%. MarketGuard +1% gate
  prevents trading in falling tape. New mixin at
  `picker/screener/reversal_breakout.py`, all thresholds env-tunable
  via `REVERSAL_BREAKOUT_*`. Surface: `STRATEGY_DISPLAY_NAMES`,
  pipeline dispatch, sector-filter group, frontend strategy lists,
  API schemas/endpoints. Default hold_days forced to 20 in backtest.
- **Repurposed `bottom_reversal` (v2) as watchlist**: the original
  left-side "still consolidating, about to launch" mixin is now
  tuned and scored for manual analysis rather than auto-trading.
  Previous "about-to-break" thresholds (drawdown ≥ 35%, MA60 ≥ 0%,
  price-pos ∈ [0.75, 0.97]) over-filtered and the resulting picks
  underperformed in live backtests (WR 13%, PF 0.42). New defaults
  surface stocks **still inside** the base for human review:
  drawdown ≥ 25%, MA60 ≥ -3%, range ≤ 30%, price-pos ∈ [0.30, 0.85],
  TOP_N 20. Scoring inverted: rewards lower price-pos (more upside
  room) and accepts flat/slow-falling MA60. Trade-levels exit rules
  restored to the v2 medium-swing config (+35% hardcap, -8% floor,
  60d time stop) since the strategy is observed, not traded.
  Backtest default hold_days raised to 40 to match the 60d time stop.

### Picker strategy lineup — drop eod_buyback, add small_cap

- **Removed `eod_buyback`** (尾盘买入): 2-year OOS validation showed PF 0.58
  with negative alpha vs CSI 300 buy-and-hold. Net delta: -1629 LOC.
  Surface deleted across backend (`screener/eod_buyback.py`, trade_levels
  branch, pipeline dispatch, realtime-filter bypass, scorer/params),
  frontend (`STRATEGY_OPTIONS`, `PickerStrategy` types, UI labels),
  configs (`PICKER_STRATEGIES` valid set), tests, and docs.
- **Added `small_cap`** (小市值, displayed as 小市值): dedicated
  LocalStockDB-backed screening path that returns the top-N smallest
  market-cap A-shares per trade date, with ST / `*ST` / 退市 / new-listing
  exclusion and an optional 5-day liquidity floor. Defaults: `top_n=50`,
  `min_amount_yuan=2,000,000` (per `docs/research/SMALL_CAP_FINAL_REPORT.md`).
  Tunable via `SMALL_CAP_TOP_N` / `SMALL_CAP_MIN_AMOUNT_YUAN` env. Surface:
  new `picker/screener/small_cap.py` mixin, registered in `ALL_STRATEGIES`,
  `STRATEGY_DISPLAY_NAMES`, `_parse_picker_strategies`, frontend strategy
  lists, and the picker-strategies guide.
- **Research scripts cleanup**: removed one-off small_cap exploratory
  scripts (`small_cap_optimize.py`, `small_cap_regime.py`,
  `small_cap_yearly.py`); conclusions live in
  `docs/research/SMALL_CAP_FINAL_REPORT.md`.

### Backtest v3 — strict AI-plan execution + agent-path data fixes

- **Engine rewrite** (`4db46ea`): `BacktestEngine` now strictly replays
  each historical analysis's own trade plan (`ideal_buy / stop_loss /
  take_profit`) against forward daily bars instead of overlaying
  hand-coded "trading rules". A-share frictions baked in (0.05% slippage,
  0.025% commission each side, 0.05% stamp duty on sell). Prior-day
  limit-up filter records `not_filled_limit_up` so retail can't fake a
  fill that would never queue. Same-bar stop+target straddles are
  closed at the stop and tagged `stop_loss_ambiguous` so they're
  auditable. Per-trade `r_multiple` / `mae_pct` / `mfe_pct` recorded.
- **Three-layer summary**: signal layer (direction accuracy / win rate),
  execution layer (fill rate / expectancy / avg R / profit factor / max
  drawdown), risk layer (MAE / MFE / ambiguous count). Engine module
  split into `_backtest_aggregation_mixin.py` to stay under the 800-line
  cap; `compute_summary` split into three ≤80-line helpers.
- **Risk-reward bucket breakdown** (`44a1556`): aggregates per-trade
  performance by the AI plan's R/R (`ge_4 / 2_5_4 / 1_5_2_5 / lt_1_5`),
  persisted to `backtest_summaries.risk_reward_breakdown_json` and
  rendered as a "按盈亏比" tile in the performance panel.
- **`analysis_history.strategy_id` persisted** (`29c4dc8`, `920db6c`):
  picker strategy now lands on disk so `strategy_breakdown` becomes
  meaningful instead of always-`buy_pullback`. New SQLite ALTER TABLE
  migration; agent path also wires it through `compute_trade_levels`.
- **Agent-path data fixes** (`920db6c`):
  1. `signal_score_at_eval` was always 0 — agent path wrote
     `total_score` but the extractor only read `signal_score`. Bridge
     both keys.
  2. `risk_reward = 161060400.0` poisoned the new bucket — legacy
     `compute_trade_levels` divides by ~0 when stop≈ideal. Clamp values
     outside `(0, 100]` to None at the storage boundary.
  3. Frontend `AnalysisResultsTable` shows `--` for `signal_score <= 0`
     so old v2 rows stop rendering as 0.
- **Frontend overhaul**: removed the "simulated trading rules"
  StrategyChips override; `PerformancePanel` redesigned around the
  three layers; new "入场" / "R" columns in the analysis-results table;
  `BacktestRunRequest.strategies` is deprecated and ignored server-side.
- **DB migrations**: `backtest_results` gains `entry_status`,
  `r_multiple`, `mae_pct`, `mfe_pct`; unique key changes from
  `(analysis_history_id, eval_window_days, strategy_id)` to
  `(analysis_history_id, eval_window_days)`. Robust SQLite migration
  rebuilds the legacy table to drop the inline UNIQUE on
  `engine_version` (which prior `DROP COLUMN` attempts silently failed
  on), then wipes legacy rows before recreating the new unique index.
- Tests: 688 passed, 92 subtests passed.

### Code-quality refactor (Phase 1-7)

Test suite: 685 passed, 0 failures. No ``.py`` file exceeds the 800-line cap.

### Frontend (Phase 5 + Phase 7)

- T5.1 — Frontend CI (`2e43e26`): `.github/workflows/frontend-ci.yml`,
  `test.sh frontend` subcommand, `typecheck`/`check` scripts.
- T5.2 — `api/error.ts` aligned with backend `APIResponse` envelope
  (`e2ce9a2`); semantic classification later refactored into a
  table-driven pipeline (`abbfed2`, parseApiError 168 → 79 lines).
- T5.3 — Split oversized pages (`e0f5055`: Backtest/Picker/Chat) +
  `ec6c4fd` (HomePage 443 → 116 via `pages/home/{hooks,components}/`).
  `agentChatStore` slice split intentionally not done — see plan.
- T5.4 — `useSystemConfig` helpers extracted to submodule (`9889496`).
  Three-hook split intentionally not done — see plan.
- T5.5 — Desktop launcher: Electron 31 → 42, added Linux build targets
  (`011ec05`).
- T7.1 — Picker list/detail routes split into `/picker` + `/picker/history/:id`
  (`d1ecac8`); `PickCard` / `ScreenedPoolTable` rows navigate to `/chat`
  for stock follow-up; null-safety fix for legacy history rows (`8d12c12`).
- T7.2 — Config registry trimmed 94 → 56 items (`21c9f28`); backend i18n
  (`title_zh` / `description_zh`) + frontend advanced folding (`b07cd35`);
  round-trip audit script (`b657f22`).

### Backend (Phase 1-4 + Phase 6)

- T1.x — `RateLimitMixin` adopted by all 6 A-share fetchers;
  `src/exceptions.py` unified taxonomy; `CachingManager` with TTL/stats;
  test files moved under `tests/`; `tests/test_backward_compat_imports.py`
  baseline added.
- T2.x — Split 4 P0 files: `tushare_fetcher` 2056 / `akshare_fetcher` 1869 /
  `storage` 2175 / `search_service` 2019 / `quantitative_filter` 1671 into
  sub-packages; each component ≤ 800 lines.
- T3.x — Split remaining P1 files: `analyzer` 1561 / `base.py` 1452 /
  `pipeline` 1616 / `config_registry` 1581 / `efinance_fetcher` 1189 /
  `stock_analyzer` 1125 / `aggregator` 919 / `main.py` 842;
  shared market-data utils extracted; T4.4-followup closed
  `enhanced_market_analyzer` (825 → 712) and `backtest_service`
  (815 → 635). Two real import bugs uncovered + fixed (T4.5).
- T4.1 — Unified `APIResponse` envelope (`code`/`message`/`data`/`timestamp`);
  global exception handler maps `src.exceptions.*` → HTTP codes.
- T4.2 — Shared DataFrame validators (`validate_daily_data` /
  `validate_realtime_data`) wired into every fetcher's `_normalize_data`.
- T4.3 — Thread-safety: double-check locking in `bot/handler.py`;
  `deque + Lock` in `bot/dispatcher.RateLimiter`; tightened
  `Dict[str, Any]` boundaries via TypedDicts.
- T6.1 — Replaced 7 per-call `ThreadPoolExecutor()` with class-level
  shared pools; 6 `max_workers=1` timeout-wrapper pseudo-uses removed via
  new `src/_concurrency.run_with_timeout()` helper.
- T6.3 — Entry-point shims verified: `analyzer_service.py` business logic
  migrated to `src/services/programmatic.py`; `webui.py` confirmed thin
  shim (59 lines).

### Refactor: trim the four remaining 800+ line modules (T3.4)

- ``main.py`` (842 → 671): the 172-line ``parse_arguments`` function
  moved to ``src/cli/args.py`` (re-exported via ``src.cli``).
- ``src/notification_service/aggregator.py`` (919 → 523): the long
  ``generate_dashboard_report`` and ``generate_wechat_dashboard``
  methods extracted into ``_dashboard_mixin.py``;
  ``ReportAggregatorMixin`` now inherits from ``_DashboardMixin``.
- ``src/stock_analyzer.py`` (1125 → 556): enums + ``TrendAnalysisResult``
  moved to ``src/_stock_analyzer_types.py``; the 343-line
  ``_generate_signal`` plus ``format_analysis`` moved to
  ``src/_stock_analyzer_signals.py``. Public surface unchanged
  (``__all__`` re-exports the seven canonical symbols).
- ``data_provider/efinance_fetcher.py`` (1169 lines, gone) replaced by
  the ``data_provider/efinance/`` sub-package, mirroring the existing
  ``akshare`` / ``tushare`` layout: ``utils.py``, ``base.py``
  (``_EfinanceCore``), ``historical.py``, ``realtime.py``,
  ``market.py``, ``fetcher.py``. All callers updated to
  ``from data_provider.efinance import EfinanceFetcher`` —
  ``tests/test_backward_compat_imports.py`` likewise.

### Refactor: split ``src/core/pipeline.py`` (1616 lines) into ``pipeline/`` package

- ``src/core/pipeline.py`` is gone; the class composition lives in five
  files under ``src/core/pipeline/``, each ≤ 785 lines (rule §1):
  - ``__init__.py`` — class signature + ``__init__`` +
    ``fetch_and_save_stock_data`` + re-exports the patch targets used
    by tests (``get_config`` / ``get_db`` / ``DataFetcherManager`` …).
  - ``_analysis_mixin.py`` — per-stock analysis (``analyze_stock``,
    context enhancement, agent invocation, result conversion).
  - ``_market_env_mixin.py`` — market-environment cache + context
    augmentation helpers.
  - ``_run_mixin.py`` — top-level ``run`` and ``process_single_stock``.
  - ``_notify_mixin.py`` — aggregate-report and notification dispatch.
- ``tests/test_pipeline_realtime_indicators.py`` patch paths updated
  from ``src.core.pipeline.{is_market_open,get_market_for_stock}`` to
  ``src.core.pipeline._market_env_mixin.{...}`` to match the new
  import location.

### Refactor: split ``src/core/config_registry.py`` (1581 lines) into ``config_registry/`` package

- The monolithic field-metadata file is gone. The contents are split
  into ``categories.py``, ``fields_a.py``, ``fields_b.py``,
  ``_inference.py`` and ``__init__.py`` — each ≤ 695 lines (rule §1).
- The 94 field definitions are split alphabetically into ``fields_a``
  (base / ai_model / data_source / first half of notification) and
  ``fields_b`` (rest of notification / system / agent / backtest), then
  merged in ``__init__.py`` via ``{**_FIELD_DEFS_A, **_FIELD_DEFS_B}``.
- Public API (``get_category_definitions`` / ``get_field_definition`` /
  ``get_registered_field_keys`` / ``build_schema_response`` /
  ``SCHEMA_VERSION``) is unchanged; the only call site
  (``src/services/system_config_service.py``) requires no edits.

### Refactor: split ``data_provider/base.py`` (1445 lines) into ``base/`` sub-package

- ``data_provider/base.py`` is gone; its contents live in seven focused
  files under ``data_provider/base/``, each ≤ 327 lines (rule §1):
  - ``__init__.py`` — public surface re-exports.
  - ``codes.py`` — stock-code helpers and exception summarisation.
  - ``fetcher.py`` — ``BaseFetcher`` ABC.
  - ``_realtime_mixin.py`` / ``_names_mixin.py`` / ``_market_mixin.py``
    — three internal mixins composed by ``DataFetcherManager``.
  - ``manager.py`` — ``DataFetcherManager`` (multi-mixin) singleton.
- All existing call sites (``api/`` / ``bot/`` / ``main.py`` / ``src/``
  / ``tests/``) keep working unchanged — public surface re-exported
  from ``data_provider.base``.
- Three latent stale imports surfaced and fixed during the split:
  ``from .akshare_fetcher`` / ``from .tushare_fetcher`` were left over
  from earlier shim removal; rewritten to the canonical
  ``from ..akshare`` / ``from ..tushare`` package paths.

### Refactor: split ``src/analyzer.py`` (1561 lines) into ``src/analyzer/`` package

- ``src/analyzer.py`` is gone; its contents live in eight focused modules
  under ``src/analyzer/`` and every file is now ≤ 513 lines (rule §1):
  - ``__init__.py`` — public surface re-exports.
  - ``integrity.py`` — content-integrity helpers + chip_structure fallback.
  - ``stock_name.py`` — multi-source stock-name resolver.
  - ``result.py`` — ``AnalysisResult`` dataclass.
  - ``_llm_client.py`` — ``_LLMClientMixin`` (LiteLLM Router init,
    ``generate_text``).
  - ``_prompt_builder.py`` — ``_PromptBuilderMixin`` (prompt assembly,
    market-snapshot helpers, formatters).
  - ``_response_parser.py`` — ``_ResponseParserMixin`` (JSON / text
    parsing into ``AnalysisResult``).
  - ``gemini.py`` — ``GeminiAnalyzer`` (composes the three mixins) +
    system-prompt constants + ``analyze`` / ``batch_analyze`` /
    integrity helper methods + ``get_analyzer`` factory.
- Public surface is unchanged: every existing
  ``from src.analyzer import …`` call site keeps working without
  modification.
- ``__main__`` diagnostic block (~33 lines) dropped — production code
  paths are exercised by ``tests/`` and ``scripts/diagnose_env.py``.

### Refactor: unified TTLCache for all data-provider module caches

- Replaced six raw module-level cache dictionaries with the unified
  ``TTLCache`` (`data_provider.caching_manager`):
  - ``data_provider/akshare/utils.py``: ``_realtime_cache`` /
    ``_etf_realtime_cache``.
  - ``data_provider/efinance_fetcher.py``: ``_realtime_cache`` /
    ``_etf_realtime_cache``.
  - ``data_provider/tushare/realtime.py``: ``_realtime_list_cache`` /
    ``_rt_k_cache`` / ``_daily_basic_cache`` / ``_daily_vol_avg_cache``
    (also drops the parallel ``_*_cache_time`` / ``_*_cache_date``
    sentinel variables).
- All caches now have explicit TTL, thread-safe get/set, and built-in
  hit/miss observability — satisfies ``code-quality.mdc`` rule §5
  ("Cache MUST be managed through a unified interface").
- ``src/services/picker/screener/data_fetch.py`` switched from poking
  cache-internal timestamps to the canonical
  ``TTLCache.clear()`` to force a refresh.
- ``tests/test_backward_compat_imports.py`` updated to track the renamed
  ``_rt_k_cache`` symbol.

### Refactor: drop legacy backward-compatibility shims

- Removed all 7 shim files left behind by phases 1-2 refactors:
  ``src/notification.py``, ``src/wechat_formatter.py``, ``src/formatters.py``,
  ``src/services/stock_picker_service.py``,
  ``src/services/picker/quantitative_filter.py``,
  ``data_provider/tushare_fetcher.py``,
  ``data_provider/akshare_fetcher.py``.
- All call sites (16 production modules + 4 test files + 1 GitHub workflow
  + ``test.sh``) updated to import directly from the canonical paths
  (``src.notification_service`` / ``src.notification_service.wechat_formatter``
  / ``src.notification_service.formatters`` / ``src.services.picker`` /
  ``src.services.picker.screener`` / ``data_provider.tushare`` /
  ``data_provider.akshare`` and their ``utils`` / ``realtime`` submodules).
- ``data_provider/__init__.py`` now imports ``AkshareFetcher`` /
  ``TushareFetcher`` from their respective canonical sub-packages.
- ``tests/test_backward_compat_imports.py`` rewritten to assert the
  canonical public surface only (legacy shim entries removed).
- Exception classes (``DataFetchError`` / ``RateLimitError`` /
  ``DataSourceUnavailableError``) consolidated: they now live only in
  ``src.exceptions``. ``data_provider/base.py`` imports them from there
  instead of defining its own copies, and every fetcher / test was
  updated to import from ``src.exceptions`` directly.
- New rule added in ``.cursor/rules/code-quality.mdc`` (section 1b) and
  ``AGENTS.md`` § 1: **single source of truth — no shims, no legacy
  paths.** Renames/moves must update all callers and delete the old
  file in the same change.

### Refactor: code quality phase 2.4 (split search service)

- ``src/search_service.py`` (2019 lines) replaced by the
  ``src.search_service`` sub-package. Each search provider gets its own file
  (``tavily.py`` / ``serpapi.py`` / ``bocha.py`` / ``minimax.py`` /
  ``brave.py`` / ``searxng.py``) plus shared modules:
  ``http_utils.py`` (``_post_with_retry`` / ``_get_with_retry`` /
  ``fetch_url_content``), ``models.py`` (``SearchResult`` / ``SearchResponse``
  dataclasses), ``base_provider.py`` (``BaseSearchProvider`` ABC),
  ``service.py`` (``SearchService`` orchestrator + ``get_search_service`` /
  ``reset_search_service`` singleton helpers).
- Largest split file is ``service.py`` at 732 lines; every provider stays
  well under the 800-line ceiling.
- Package ``__init__.py`` re-exports every public symbol so existing
  imports (``from src.search_service import SearchService`` / provider
  classes / dataclasses / module-level helpers) keep working.
- Tests that mocked ``src.search_service.requests.get`` /
  ``src.search_service.datetime`` were updated to point at the canonical
  module locations (``http_utils.requests`` / ``service.datetime``).

### Refactor: code quality phase 2.3 (split storage layer)

- ``src/storage.py`` (2175 lines) replaced by the ``src.storage`` sub-package:
  ``models.py`` (all 9 ORM models + ``Base``), ``manager_base.py``
  (singleton/engine/session lifecycle + schema migrations),
  ``daily_data.py`` (StockDaily queries + ``save_daily_data``),
  ``news.py`` (NewsIntel CRUD), ``analysis.py`` (AnalysisHistory CRUD +
  ``get_analysis_context`` + helpers), ``picker.py`` (picker history +
  picker backtest history), ``conversation.py`` (chat session/messages),
  ``llm_usage.py`` (LLMUsage audit log), ``manager.py`` (composes the
  mixins; module-level ``get_db`` / ``persist_llm_usage``).
- Largest split file is ``analysis.py`` at 613 lines; the package
  ``__init__.py`` re-exports every public symbol so existing imports
  (``from src.storage import DatabaseManager`` / ``get_db`` /
  ``persist_llm_usage`` / any ORM model) continue to work without changes.
- ``DatabaseManager.__init__`` keeps registering the atexit cleanup hook
  via ``type(self)._cleanup_engine`` so the inheritance chain stays
  decoupled from a hard-coded ``DatabaseManager`` reference.

### Refactor: code quality phase 2.2 (split StockScreener)

- ``src/services/picker/quantitative_filter.py`` (1671 lines) split into the
  ``src.services.picker.screener`` sub-package: ``base.py`` (shared state +
  small helpers + risk-filter delegations), ``data_fetch.py`` (spot/Tushare
  bulk + realtime overlay + normalisation), ``filters_scoring.py`` (basic
  filter / hard veto / momentum / volume / scoring), ``pipeline.py``
  (``screen()`` and ``screen_as_of()``), ``eod_buyback.py`` (the dedicated
  realtime EOD path), ``screener.py`` (composes the mixins).
- Each split file is ≤556 lines (largest: ``data_fetch.py``).

### Refactor: code quality phase 2.1 (split Tushare/Akshare fetchers)

- ``data_provider/tushare_fetcher.py`` (2056 lines) split into the
  ``data_provider.tushare`` sub-package: ``base.py`` (init/rate-limit/code conv),
  ``historical.py`` (daily/ETF/chip), ``realtime.py`` (full-market snapshots,
  ``rt_k`` and ``daily_basic`` caches), ``market.py`` (indices/market_stats/sector),
  ``utils.py`` (helpers/constants), ``fetcher.py`` (composes the mixins).
  Each file is now ≤800 lines.
- ``data_provider/akshare_fetcher.py`` (1869 lines) split into the
  ``data_provider.akshare`` sub-package along the same axes
  (``base``/``historical``/``realtime``/``market``/``utils``/``fetcher``).
- ``tests/test_akshare_realtime_logging.py``: ``monkeypatch.setattr`` targets
  retargeted to the new ``data_provider.akshare.realtime`` module so the patch
  hits the actual call site.

### Refactor: code quality phase 1 (anti-crawl / exception / cache convergence)

- `data_provider/rate_limit_mixin.py`: ``_last_request_time`` is now a per-instance
  attribute guarded by an instance-level ``threading.Lock`` (no more class-shared race).
- `tushare_fetcher`, `baostock_fetcher`, `yfinance_fetcher`, `pytdx_fetcher` now inherit
  ``RateLimitMixin`` so all six A-share fetchers share the same anti-crawl helpers.
- `src/exceptions.py`: unified taxonomy (``DataFetchError`` base +
  ``RateLimitError`` / ``NetworkError`` / ``DataSourceUnavailableError`` /
  ``ValidationError`` / ``UnknownError``) is now the single source of truth.
  ``data_provider.base`` and every fetcher imports the classes from here.
- `BaseFetcher._classify_exception(exc)` returns the taxonomy class (companion to the
  existing string-tuple ``_classify_error``).
- `data_provider/caching_manager.py`: added ``TTLCache`` (per-entry TTL, hit/miss
  stats, ``threading.RLock``) and ``trading_session_ttl`` helper.
- `data_provider/fundamentals_fetcher.py`: migrated the bespoke
  ``FundamentalsCache`` dataclass + four manual double-check-locked ``_ensure_*``
  helpers onto ``TTLCache`` (drops ~80 lines of boilerplate, no behaviour change).
- Cleanup: moved root-level diagnostic scripts ``test_env.py``,
  ``test_enhanced_review.py``, ``test_realtime_filter_demo.py``,
  ``test_verification.py`` and ``scripts/test_picker_backtest.py`` to
  ``scripts/diagnose_env.py`` / ``check_enhanced_review.py`` /
  ``demo_realtime_filter.py`` / ``verify_search_service.py`` /
  ``run_picker_backtest.py``. Updated docs and ``.gitignore``.
- Entry-point inventory: ``main.py`` (CLI), ``server.py`` (FastAPI),
  ``webui.py`` (env-var launcher) and ``analyzer_service.py`` (programmatic
  service facade documented in ``SKILL.md``) all kept — each has a distinct,
  user-visible role.

### Cleanup: remove backtest engine v1 entirely

- Dropped the `backtest_engine_version` config knob, the `BACKTEST_ENGINE_VERSION` env var,
  and the corresponding entry in `src/core/config_registry.py` / WebUI i18n.
- Dropped the `engine_version` column from `backtest_results` and `backtest_summaries`
  (SQLite `ALTER TABLE ... DROP COLUMN`), removed `EvaluationConfig.engine_version` and the
  `BacktestEngine.ENGINE_VERSION` constant, and rebuilt the unique indexes
  (`backtest_results` → `(analysis_history_id, eval_window_days, strategy_id)`,
  `backtest_summaries` → `(scope, code, eval_window_days)`).
- Also dropped the legacy `advice_breakdown_json` column / API field / TS type — the v2
  `signal_breakdown / score_bucket_breakdown / exit_reason_breakdown / regime_breakdown /
  strategy_breakdown` panels fully replace it.
- The one-shot wipe migration introduced earlier still runs once on first init so any
  stale rows from the old schema are erased before the column drop.

### Individual Stock Backtest: per-strategy multi-run

- API: `POST /api/v1/backtest/run` now accepts an optional `strategies: string[]` payload
  (subset of `buy_pullback / breakout / bottom_reversal / eod_buyback`). When supplied,
  each analysis record is evaluated once per strategy and persisted as a separate
  `BacktestResult` row, so users can compare per-strategy win rate / R/R / exit reasons
  side by side in the existing breakdown panel.
- DB: extended the `backtest_results` unique key to
  `(analysis_history_id, eval_window_days, engine_version, strategy_id)`. SQLite migration
  drops the old index and creates the new one in place.
- Service: shared data fetch (start price + forward bars) is reused across strategies,
  only the trade-levels evaluation runs per strategy. `force=True` rerun replaces only the
  rows whose `strategy_id` matches the new run, so other strategies for the same analysis
  are kept.
- WebUI: the individual-stock backtest "Controls" card grew a `回测策略` chip row mirroring
  the picker UI; defaults to `买回踩` and supports multi-select.

### BREAKING: Individual Stock Backtest Engine v2 (signal-driven, trade-levels-aware)
- **Engine rewritten** (`src/core/backtest_engine.py`): no longer parses LLM `operation_advice` text. Now driven by:
  - System-computed `buy_signal` (`STRONG_BUY` / `BUY` → long; `HOLD` / `AVOID` / `STRONG_AVOID` → cash). Eliminates Chinese-keyword fragility.
  - Unified `simulate_forward_trade` (same engine as picker backtest): staged exits, MA10 trailing, ATR×2.5 retracement, slippage, limit-up entry filter.
- **`engine_version` bumped to `v2`**. Default in `Config` and `BACKTEST_ENGINE_VERSION` env var both updated.
- **DB schema extended** (`backtest_results` adds 16 columns, `backtest_summaries` adds 5 JSON columns): `signal_score_at_eval` / `buy_signal_at_eval` / `market_environment_at_eval` / `strategy_id` / `risk_reward_at_eval` / `position_pct_at_eval` / 7 dim score snapshots / `exit_reason` / `hold_days`. Summaries now carry `signal_breakdown_json` / `score_bucket_breakdown_json` / `exit_reason_breakdown_json` / `regime_breakdown_json` / `strategy_breakdown_json`.
- **One-shot data wipe**: on first DB init under v3.x+, all legacy `backtest_results` / `backtest_summaries` rows are DELETEd (note: the `engine_version` column referenced here was later dropped entirely — see top of changelog). Re-run `/api/v1/backtest/run` to regenerate. Pre-v3.0 analyses (which lack `signal_score`) yield `eval_status='missing_signal'` and are skipped.
- **API + WebUI**:
  - `BacktestResultItem` exposes the 16 new fields; `PerformanceMetrics` exposes 5 breakdown dicts.
  - `BacktestPage.tsx` individual-backtest table replaces 「建议 / 止损 / 止盈」 columns with 「信号 / 量化分 / 策略 / 持仓 / 退出原因」.
  - New `BreakdownGrid` panel under `PerformancePanel` shows win-rate by signal / score-bucket / exit_reason / regime / strategy.
- **Tests**: `tests/test_backtest_engine.py` rewritten for v2 (15 cases covering signal mapping, missing_signal handling, simulate_forward_trade integration, breakdown aggregation). `tests/test_backtest_summary.py` updated to v2 row shape.

### Fixed (Picker Backtest WebUI: expose exit_reason / hold_days / strategy)
- **`PickResult` extended** with `exit_reason` (e.g., `stop_loss` / `trailing_ma10` / `stage_break_+12pct` / `hardcap_+20pct` / `window_end`), `hold_days`, and `strategy_id`. Previously `simulate_forward_trade` returned these but the picker backtest pipeline dropped them on the floor.
- **`_get_forward_return` signature changed** from `Tuple[exit_price, return_pct]` to `Dict[str, Any]` so the parallel runner can propagate the diagnostic fields.
- **API + TS schema + WebUI table** updated: results table now shows 持仓 / 退出原因 / 策略 columns. Users can finally diagnose *why* a backtest pick lost money (止损 vs 移动止盈 vs 窗口到期 vs 硬顶平仓).

### Fixed (Analysis WebUI: expose system signals & trade-level extras)
- **DB schema extended** (`AnalysisHistory`): added `signal_score` / `buy_signal` / `pe_ratio` / `market_environment` / `position_pct` / `risk_reward` / `take_profit_2_rule` columns. Auto-migration via `ALTER TABLE` (existing rows get NULL; new analyses populate full set).
- **save_analysis_history**: extracts the new fields from `context_snapshot.enhanced_context.{trend_analysis, trade_levels}` (computed by pipeline / trade_levels engine) and persists them.
- **API schemas** (`ReportSummary` / `ReportStrategy`): exposed `signal_score`, `buy_signal`, `pe_ratio`, `market_environment`, full per-dimension breakdown (`trend_score` / `bias_score` / `volume_score` / `support_score` / `macd_score` / `rsi_score` / `capital_flow_score`), and trade-level extras (`position_pct` / `risk_reward` / `take_profit_2_rule`). These were previously computed but silently dropped at the Pydantic boundary.
- **WebUI**: new `ReportScores` panel shows total quant score (0-100), `buy_signal` badge, market regime, PE, and a 7-dim score-bar breakdown. `ReportStrategy` now also renders 建议仓位 / 盈亏比 R/R / 后续止盈规则.
- **Stale comments fixed** in `storage.py` (`support_score` 0-12 → 0-6, `macd_score` 0-10 → 0-13, `capital_flow_score` 0-10 → 0-13 weighted) to match the post-rebalance scoring.

### Changed (Parameter Tuning Wave — Investment-Expert Review)
- **R/R floor raised 1.8 → 2.0** (`trade_levels.RR_MIN`): A-share round-trip cost (slippage + tax) ~0.5% leaves net 1.5 at 1.8 — insufficient at <50% win rate. New 2.0 keeps positive expectancy at win rates ≥45%.
- **Trailing trigger lowered +20% → +15%** (`evaluate_trailing_exit`): A-share rallies often start ABC consolidation around +18-22%; entering trailing at +15% locks 3-5% additional profit on average.
- **Stage stops widened**: 5% / 10% → 6% / 12% to reduce premature stop-outs from intraday noise. New floor at +6% break.
- **Position baselines tightened** (`_base_position_pct`): small-cap 6% → 5%, large-cap 18% → 15% to cap single-name idiosyncratic blow-ups; mid-cap unchanged at 12%.
- **Position boost re-shaped**: R/R≥3 boost 1.20×→1.30×, absolute cap 25%→22% — separates "great" signals from "decent" ones while keeping single-name exposure safer.
- **Small-cap stop loosened** -5% → -6%: small caps need wider stops vs intraday noise.
- **Breakout tp1 1.06 → 1.08**: first-leg average is +8-12%; trimming at +6% truncated fat-tail winners.
- **Bottom-reversal hard cap +15% → +20%** with new staged exit (+15% trim half, +20% full clear): real reversals run +18-25% on main leg; +15 single-shot capped winners.
- **EOD buyback `expected_mult` 1.025 → 1.018**: aligns with realistic next-day median return; previous value inflated R/R and over-sized position.
- **Picker filters tightened**:
  - `BUY_PULLBACK.change_60d_min` 5% → 8%
  - `BREAKOUT.volume_ratio_min` 2.0 → 1.7 (eased; 2.0 over-filtered moderate-volume genuine breakouts)
  - `BOTTOM_REVERSAL.change_60d_max` -5% → -8%
  - B-wave fib zone 0.35-0.65 → exact 0.382-0.618
- **Resonance bonus re-shaped**: double +10 → +8, triple +20 → +25 (widen gap; triple resonance is rare and historically much higher win rate).
- **Auto-reweight stricter**: lookback 28d → 42d (covers ≥1 style-rotation cycle), bad win-rate floor 40% → 42%, penalty factor 0.7 → 0.6 (more decisive).
- **Veto thresholds tightened**: pledge ratio 60% → 50% (most A-share pledge blow-ups occur >50%), reduction window 5d/2% → 10d/1% (insider-selling impact persists 2-3 weeks).
- **Regime-aware position scaling (E1)**: weak market ×0.6, neutral ×0.85, strong ×1.0 applied post-merge in `stock_picker_service.screen()` — complements existing market_guard which only restricts strategies.

### Changed (Picker Quality Tightening — Post-run Review)
- **`BOTTOM_REVERSAL.pe_max` 100 → 60**: reversal strategy is mean-reversion bet on oversold names; PE>60 with -15-20% drawdown rarely qualifies as a true "low base" — historical win rate <30% in this band. Tightening filters out high-PE "fake reversals" (e.g. mid-cap "白马" rebounds).
- **Industry concentration cap (E2)**: post-merge filter keeping at most N (default 2, env `PICKER_INDUSTRY_TOP_N`) highest-scoring candidates per SW L1 industry. Prevents sector-beta blow-ups where one industry can dominate the picker pool. New `ScreenedStock.industry` field (best-effort from Tushare `stock_basic`); cap silently skipped when industry data unavailable.

### Refactored
- `trade_levels.py`: collapsed 4 strategy-specific level-builder functions into a single config-table-driven implementation (`_StrategyConfig` + `_resolve_entry_anchor`). Removes ~120 lines of duplicate code; new strategies / parameter tuning now require config-only changes.

### Changed (Scoring Dimension Rebalance — Remove MA5-support Redundancy)
- **Support dimension 12 → 6**: removed MA5-support (structurally redundant with `bias_ma5` dimension — both measure "close to MA5"; old setup triple-counted "healthy pullback" theme as bias 14 + volume 18 + ma5_support 7 = 40% of total on one signal cluster). Kept MA10-support at 5; added MA20 trend-integrity at 1 (price above rising MA20).
- **MACD 10 → 13**: re-allocated from support; MACD is genuinely independent alpha but was under-weighted. Smoother ladder (GOLDEN_CROSS_ZERO 13 → BULLISH 8).
- **Capital flow 10 → 13**: re-allocated from support. External-facing `capital_flow_score` contract stays 0-10 (no upstream change needed); internal contribution is `raw × 1.3`.
- New dimension shape: Trend 30 / Bias 15 / Volume 18 / Support 6 / MACD 13 / RSI 5 / CapitalFlow 13 = 100.

### Changed (Per-stock Scoring System Hardening — 8 fixes)
- **Regime-aware classifier** (`classify_buy_signal`): bear / strong_bear bump BUY/STRONG_BUY thresholds by +10 (60→70, 75→85) so a "looks good" 60-score in bear no longer triggers BUY.
- **Hard score caps by market regime** (`_generate_signal`): strong_bear ×0.85→×0.75 with absolute cap 60; bear ×0.90→×0.85 with cap 75 — defense-in-depth against false-positive BUY signals in down markets.
- **PE valuation penalty**: PE>100 -8, PE>200 -15, PE<0 (loss) -5 — closes the bubble-stock loophole where 100x-PE names could still score 80+. New `TrendAnalysisResult.pe_ratio` field (optional, 0=skip).
- **Capital flow score clip**: `[0, 10]` clamp guards against external sources returning out-of-range values that silently inflated total score.
- **HEAVY_VOLUME_UP demotion in late-acceleration**: when 20d gain >30% or 5+ consecutive up days, score 14→6 (放量上涨 in 加速期 is typically distribution).
- **SHRINK_VOLUME_UP demotion in strong/bull trends**: 7→3 (滞涨缩量 = top divergence in uptrends).
- **MACD ladder smoothing**: CROSSING_UP 7→8, BULLISH 5→6 — closes the gap between strongest signal and persistent-bullish.
- **Bias dead-zone fix**: when MA20 history unavailable for `bias < -5` branch, replace flat 6 with linear interpolation of bias magnitude (avoids step discontinuity).

### Added
- **Unified trade levels engine** (`src/services/trade_levels.py`): single source of truth for entry / stop-loss / take-profit / position size / risk-reward across picker, analyzer, and backtest layers. Strategy-aware (buy_pullback / breakout / bottom_reversal / eod_buyback) with ATR-based trailing stop replacing fixed 15% take-profit ceiling for trend strategies — lets winners run on strong trends.
- **Trailing stop logic** (`evaluate_trailing_exit`): after +20% profit, exit on MA10 break OR ATR×2.5 retrace from peak. Bottom-reversal keeps hard +15% cap (no trailing). Stage-aware exits at +5% (trim 1/3, stop to cost) and +10% (trim again, stop to +5%).
- **Position tracker service** (`src/services/position_tracker.py`): stateless API for evaluating any held position against unified trailing rules, returning structured action recommendations. Ready for integration into watchlist scans / agent skills.
- **Picker outputs trade levels**: `ScreenedStock` and `StockPick` now carry `ideal_buy / stop_loss / take_profit_1 / take_profit_2_rule / position_pct / risk_reward`. Notifications and AI-selection prompts display these fields so users have immediate trade-action clarity.
- **Risk/Reward hard filter**: candidates with R/R < 1.8 are dropped during scoring. R/R is computed against an "expected average exit" (12-13% for trend strategies, 11.5% for reversal, 2.5% for EOD) that accounts for staged trimming + trailing tail.
- **Multi-strategy resonance bonus**: stocks flagged by 2 strategies get +10 score and `resonance="double"`; 3+ strategies get +20 and `resonance="triple"` (forced inclusion in AI selection prompt with ⭐⭐⭐ badge).
- **Fundamental hard-veto filter** (`data_provider/fundamentals_fetcher.py`): pre-screening removes candidates with controlling-shareholder pledge >60%, goodwill/total-assets >30%, recent ≥2% holder reductions, or negative earnings forecasts (预亏/预减/首亏/续亏/增亏). Fail-closed when Tushare unavailable. Filter stats recorded in `ScreenStats.veto_reasons`.
- **Strategy attribution service** (`src/services/strategy_attribution_service.py`): rolling 4-week per-strategy win-rate / profit-factor / max-drawdown computed from historical picker_history, with auto-reweighting (×0.7) for strategies that hit BOTH win-rate <40% AND profit-factor <1. Off by default (`STRATEGY_AUTO_REWEIGHT=true` to enable). Includes weekly report formatter.
- **Backtest unified with production**: `picker_backtest_service._get_forward_return` now uses the same trade_levels exit rules + 0.3% slippage + limit-up entry filter, eliminating the previous three-way rule mismatch (picker/analyzer/backtest).
- **Trade levels in analyzer prompt**: `_enhance_context` injects pre-computed `trade_levels` block; SYSTEM_PROMPT instructs LLM to use these numbers directly rather than inventing its own — addresses recurring stop/take confusion bugs.
- Seven-dimension scoring system (trend 30 + bias 15 + volume 18 + support 12 + MACD 10 + RSI 5 + capital flow 10 = 100)
- Market environment correction factor based on SSE MA20 direction (strong_bear ×0.85, bear ×0.90, neutral/bull ×1.0, strong_bull ×1.05)
- Phase-based bias threshold (startup 6%, main-rise 5%, acceleration 3.5%)
- ATR(20) dynamic bias threshold adjustment with local fallback calculation
- Capital flow scoring via Tushare moneyflow (main force + northbound, min 1M threshold)
- Stop-loss/take-profit consistency constraints in LLM prompts
- Dimension scores persistence for backtest evaluation in Agent mode
- Unified buy signal classification via `StockTrendAnalyzer.classify_buy_signal()`

### Fixed
- Agent mode missing dimension scores causing all backtest results to show "negative"
- DataFetcherManager thread-safety issue (double-checked locking in both __new__ and __init__)
- ATR_20 dynamic threshold never effective in production (DB didn't store the column)
- Prompt principle #7 contradicting code logic (updated to phase-based rule)
- Volume shrinkage scoring now market-condition-aware (bull 18 / sideways 10 / bear 0)

### Changed
- Bias threshold from fixed 4.5% to phase-based (3.5% / 5.0% / 6.0%)
- Strong bear market coefficient from 0.75 to 0.85
- Bear market coefficient from 0.85 to 0.90
- Volume exhaustion penalty: -5 points; extreme volume warning: -15 points
- Capital flow scoring tightened (6/3/1/0 for main force, 4/2/1/0 for northbound)

### Removed
- 17 deprecated Optional fields from AnalysisReportSchema (old format)
- `sector_relative_strength` unused field from TrendAnalysisResult
- All legacy analysis history data (database cleanup)

### Enhanced
- **个股分析评分体系重构** — 新增资金面维度(10分)，调整权重配比（降低MACD/RSI滞后指标权重，提升量能/支撑位权重）
- **动态乖离率阈值** — 基于20日ATR波动率自适应调整（高波动股放宽、低波动股收紧）
- **RSI超卖企稳条件** — RSI<30需连续2日回升确认企稳才给满分，避免下跌趋势误判
- **乖离率MA20方向区分** — 负乖离率时区分MA20上行(超跌反弹)与MA20下行(趋势破坏)
- **多日量能趋势分析** — 新增3/10/20日均量对比、天量预警(>5倍)、量能衰竭检测
- **资金面数据接入** — 接入Tushare北向资金(moneyflow_hsgt)和个股主力资金(moneyflow)，纳入评分体系
- **LLM Prompt增强** — 新增止盈止损规则、量能深度解读指令、资金面三角验证逻辑

### Added
- `data_provider/moneyflow_fetcher.py` — 资金流向数据获取模块(MoneyflowFetcher)
- Agent工具 `get_capital_flow` — 个股资金面分析工具

### Performance
- **perf(sector)**: Background preload of sector ranking & member data at startup via FastAPI lifespan
- **perf(sector)**: Periodic background refresh every 50 minutes (before 1-hour cache TTL)
- **perf(sector)**: Class-level shared cache across all service instances (preloaded data reusable by requests)
- **perf(sector)**: Concurrent member fetch with 10 workers and 300s timeout

### Fixed
- **fix(sector)**: Correct `TimeoutError` type for `concurrent.futures.as_completed` (`FuturesTimeout`)
- **fix(sector)**: Thread-safe cache reads with `_cache_lock`
- **fix(sector)**: Preload moved from `server.py` to `api/app.py` lifespan to work with all startup methods
- **fix(ci)**: Add `LLM_MINIMAX_*` env vars to `daily_analysis.yml` workflow

### Improved
- **improve(sector)**: AkShare retry logic (3 attempts with 5/10/15s backoff) for flaky East Money API
- **improve(picker)**: 180s timeout wrapper for sector code fetch with graceful degradation

### Added
- **feat(picker)**: 板块强度过滤 — 选股前自动过滤弱势板块，仅从强势行业板块成分股中选股
  - 按策略区分：buy_pullback/breakout 启用，bottom_reversal 跳过
  - 新增 `SectorStrengthService` 提供板块排名和成分股获取（AkShare数据源）
  - 新增环境变量：`PICKER_SECTOR_FILTER`, `PICKER_SECTOR_TOP_PCT`
- **feat(picker)**: MarketGuard 市场环境开关 — 沪指低于MA20超1%时自动限制选股策略
  - 三档判断：strong / neutral（缓冲区0~1%）/ weak
  - 新增 `get_index_daily_data()` 正确获取上证指数日线数据
  - 新增环境变量：`PICKER_MARKET_GUARD`, `PICKER_WEAK_MARKET_ACTION`, `PICKER_WEAK_MARKET_STRATEGIES`
- **feat(picker)**: buy_pullback 新增距20日高点最小距离过滤 (`min_pullback_from_high_pct=3.0`)，排除追高
- **feat(picker)**: buy_pullback 新增 MA20 下跌通道防护 (`require_price_above_ma20`)，跌破 MA20 排除
- **feat(picker)**: buy_pullback 新增 MA10 支撑位距离过滤 (`max_distance_above_ma10_pct=3.0`)，确认支撑位附近
- **feat(picker)**: eod_buyback 策略重写为龙虾方案 — 使用 `ts.get_realtime_quotes()` 分批200只查询全市场实时行情，替代 efinance 全市场接口
- **feat(picker)**: eod_buyback 补充 Tushare Pro `daily_basic` 数据（换手率、总市值）
- **feat(picker)**: eod_buyback 添加盘后量比过滤（15:00后生效）和 VWAP 实时验证

### Fixed
- **fix(picker)**: 修复 `_filter_by_realtime()` NoneType 比较崩溃，添加 `_safe_float()` 防护
- **fix(picker)**: eod_buyback 候选完整 bypass `_filter_by_realtime()`，避免双重过滤
- **fix(data)**: 修复 Tushare `change_amount` 计算错误（`bid-ask` → `price-pre_close`）
- **fix(data)**: 修复 Tushare 实时行情 9 个字段硬编码 0.0 改为 None
- **fix(picker)**: 空筛选池三重防护 — 跳过LLM、后验证、提示词加严
- **fix(picker)**: MarketGuard 指数代码 bug — `get_daily_data("000001")` 返回平安银行而非上证指数
- **fix(picker)**: MarketGuard limit 模式永久修改策略列表 — 改用 try/finally 恢复原始策略

### Changed
- **refactor(picker)**: buy_pullback 策略参数优化 — 均线多头加0.5%容差，缩量条件放宽至1.3
- **refactor(picker)**: buy_pullback 评分函数重写 — 量比评分奖励缩量，动量评分对齐入池范围
- **refactor(picker)**: buy_pullback 策略参数全面收紧 — 启用缩量检查、量比0.7、乖离率5%、PE上限60、日涨幅上限2%、60日上限40%、回调限制0.4
- **refactor(picker)**: buy_pullback 评分优化 — 缩量(0.5-0.9)最高分、深回踩(-3%~-1%)最高分、放量(>3.0)惩罚
- **refactor(picker)**: 底部反转策略参数优化 — 启用缩量检查、量比0.7、60日上限-5%、B浪过滤0.618
- **refactor(picker)**: 底部反转评分增强 — 60日跌幅深度加分、缩量→放量转折信号、反转K线形态加分
- **refactor(picker)**: bottom_reversal `daily_change_min` 0.0→1.0，实时验证更严格
- **refactor(picker)**: 清理未使用的全市场实时路径死代码（fetch_realtime_market_data、get_realtime_all_spots）
- **refactor(picker)**: 删除重复的 `_has_recent_limit_up` 函数
- **perf**: 并发超时 10s→60s，eastmoney patch 超时 15s→30s

### Added (previous)
- Real-time filtering stage (Stage 1.5) for stock picker: After quantitative screening, filter candidates by live market conditions (limit-up/down, daily price range, volume spike). **Enables intraday picking & same-day trading**.
  - `PICKER_ENABLE_REALTIME_FILTER` (bool, default true): Main toggle
  - `PICKER_REALTIME_EXCLUDE_LIMIT_UP` (bool, default true): Exclude up-limit-locked stocks
  - `PICKER_REALTIME_EXCLUDE_LIMIT_DOWN` (bool, default true): Exclude down-limit-locked stocks
  - `PICKER_REALTIME_DAILY_CHG_MIN` (float, optional): Min daily gain % (e.g. -2 for at least -2% pullback)
  - `PICKER_REALTIME_DAILY_CHG_MAX` (float, optional): Max daily gain % (e.g. 8 to avoid chasing too high)
  - `PICKER_REALTIME_MAX_VOLUME_RATIO` (float, default 0): Filter abnormal volume spikes (0 = no limit)
- New guide: `docs/realtime-picker-guide.md` with 3 ready-to-use templates (conservative/balanced/aggressive)
- Demo script: `scripts/demo_realtime_filter.py` shows filtering logic in action

### Changed
- Picker pipeline now has 3 stages:
  1. Quantitative screening (historical daily data)
  2. **Real-time filtering** (NEW: intraday market conditions)
  3. AI selection (LLM picks 1-5 from filtered pool)

### Picker page: Pill-style strategy chips (buy_pullback, breakout, bottom_reversal), card container, result view shows strategy + timestamp.
- Picker backtest: Strategy selection (replaces mode dropdown). API accepts `picker_strategies`; history stores `picker_strategies_json`.
- Per-strategy leader bias exemption: `leader_bias_exempt_pct` in StrategyParams (breakout=14%, others=0). Removed global `PICKER_LEADER_BIAS_EXEMPT_PCT`.

### Changed
- GitHub Actions `daily_analysis.yml`: default schedule Beijing 18:00 (`cron`: UTC 10:00 Mon–Fri).
- Picker backtest: Remove `picker_mode` and `picker_leader_bias_exempt_pct` from API. Use `picker_strategies` only.
- Backtest page: holdDays/topN inputs allow empty (string state, parse on submit).
- Picker footer tagline: "买回踩 · 突破 · 底部反转 — 多策略并行，按需组合".

### Removed
- `PICKER_LEADER_BIAS_EXEMPT_PCT` env var (leader exemption now per-strategy).
- Picker mode (严进/平衡/进攻) from picker backtest API and UI.

### Added (previous)
- Multi-strategy picker: `PICKER_STRATEGIES` (comma-separated) runs multiple strategies in parallel. Strategies: `buy_pullback` (买回踩), `breakout` (突破), `bottom_reversal` (底部反转). Candidates merged and tagged by strategy. No intensity modes (defensive/balanced/offensive) — each strategy has fixed params.
- Picker strategy comparison view: API returns `screened_pool_by_strategy` (per-strategy candidate lists). Frontend toggle "合并" / "按策略对比" to view merged pool or each strategy's candidates separately for comparison.

### Changed
- Picker strategy params (expert-tuned): 买回踩 daily_change -2.5%~3% (was -1%~4%), pe_ideal_high 35 (was 30); 底部反转 max_retracement_pct 1.0 (was 0.618), pe_max 100 (was 80); MACD max_retracement_pct 0.8 (was 0.618).
- Picker basic filter: Remove low-price filter (was exclude price < 3 yuan). Low-priced stocks are no longer excluded.
- Sector rankings (板块排行): Tushare `sw_daily` as priority source when TUSHARE_TOKEN configured. Avoids Eastmoney rate limiting (RemoteDisconnected). Requires 5000+ Tushare points.
- Picker backtest: stop-loss/take-profit per 买卖点规则 — stop-loss: 跌破 MA20 or -8% drawdown; take-profit: 前高 (20d high), 整数关口 (5/10/20/50/100...), or +15% fallback.
- Picker backtest speed: `CachingDataFetcherManager` caches get_daily_data per run; screener filters (`_filter_by_bias`, `_filter_limit_up_streak`, `_filter_consecutive_up_days`, `_filter_b_wave_risk`) fetch in parallel (5 workers) with request deduplication.

### Fixed
- Chip distribution (筹码分布): Tushare `cyq_chips` first (5000+ points), Akshare fallback. Serialize fetch (max_workers 1), delay 1.5–3s. Set `ENABLE_CHIP_DISTRIBUTION=false` if unstable.
- Picker API: Fix `PickerResponse() got multiple values for keyword argument 'picker_mode'` — remove redundant kwargs since `result_dict` already includes them.
- Stock code market detection: Add 001/003/004 prefixes for Shenzhen SME board (e.g. 003031). Tushare/Baostock/Yfinance now correctly map these to .SZ instead of falling back with warning.

### Added
- `PICKER_SPOT_TIMEOUT`: Timeout (seconds) for full-market spot data fetch (AkShare/efinance). Default 30. Increase when Eastmoney API is slow.
- `PICKER_ALLOW_LOSS`: When `true`, allow loss-making stocks (PE≤0) in picker pool. Default `false`.
- Picker backtest: `PickerBacktestService` runs quantitative screener historically, evaluates forward returns. API: `POST /api/v1/picker-backtest/run`, `GET /picker-backtest/performance`, `GET /picker-backtest/results`. Frontend: "选股回测" tab on Backtest page.
- Picker backtest persistence: Results saved to SQLite (`picker_backtest_history`). `GET /picker-backtest/history` (list), `GET /picker-backtest/history/{id}` (detail). Frontend loads last run from DB on tab switch (survives refresh), shows history list for quick switching.
- Stock screener `screen_as_of(trade_date)` for historical screening (Tushare only).
- `PUSH_REPORT_TYPE`: Separate report type for push notifications. When set (e.g. `brief`), push stays short while dashboard/file/Feishu doc remain detailed (`REPORT_TYPE`).
- `NOTIFY_ENABLED`: When `false`, disable all push notifications (for local runs). Default `true`.
- Stock picker bias filter: Layer 5 excludes candidates with MA5 bias > 8% (严进策略). Requires daily history from data provider.
- `--picker-only`: Run AI stock picker only (skip individual stock analysis and market review). Use `./test.sh picker` for quick verification.
- Stock picker 60-day change for Tushare: Compute 60日涨跌幅 via trade_cal + daily when using Tushare (AkShare spot already includes it).
- Stock picker constants: `VOLUME_RATIO_MIN`, `TREND_DECAY_THRESHOLD_PCT` for maintainability.
- `./test.sh picker-validation`: Offline validation of picker logic (60d decay, volume filter, prompt).

### Changed
- `daily_analysis.yml`: Split into 3 parallel jobs — `market-review` (45min), `stocks` (120min), `picker` (45min). Each job runs independently; schedule runs all 3, manual dispatch supports `full` / `market-only` / `stocks-only` / `picker-only`. Artifacts: `analysis-market-*`, `analysis-stocks-*`, `analysis-picker-*`.
- Backtest page: Improve layout and styling — gradient hero, segmented tabs, grid params for picker backtest.
- Picker backtest: Speed up — cache stock_basic (saves 1 Tushare call/day), batch benchmark fetch (N→1), parallelize forward returns (5 workers).
- Tushare: Pass token directly to `pro_api(token=...)` instead of `set_token()` to avoid writing `~/tk.csv`. Fixes "Operation not permitted" in sandbox/restricted environments (e.g. macOS, Docker).
- Stock picker: Use last A-share trading day (via exchange_calendars) when today has no Tushare data (e.g. weekends). Fixes empty quant pool on Saturday/Sunday.
- Stock picker: Spot data fetch timeout 10s → 30s (configurable via `PICKER_SPOT_TIMEOUT`).
- Workflow default: `REPORT_TYPE=simple` (detailed analysis/dashboard), `PUSH_REPORT_TYPE=brief` (short push).
- `docs/analysis-strategy-guide.md`: Added AI picker bias filter description, chase-risk exclusion (today > 9%), scope and limitations section.
- Stock picker: LLM picks 1-5 (was 3-8), explicit empty-position trigger (乖离率>5%占比>60%).
- Stock picker: 60-day gain >30% score decay to avoid end-of-trend buys.
- Stock picker: Volume ratio filter 0.8 → 1.0 to exclude cold stocks.
- Stock picker: PE filter 200 → 100 (PE_MAX constant).
- Stock picker: Limit-up streak filter — exclude 2+ limit-up days in last 5 days (连板/妖股).
- Stock picker: Board-specific limit-up threshold (main 10%, ChiNext/STAR 20%).
- Stock picker: Chip concentration in AI prompt (concentration_90, profit_ratio) when enabled.
- Stock picker: Industry dispersion constraint in prompt.
- `BIAS_THRESHOLD`: When not set, derive from `PICKER_MODE` (defensive 6%, balanced 8%, offensive 10%).
- Picker prompt: Align bias best-buy range with 买卖点规则 (2%/5%); replace "缩量回踩优先" with "量能配合的回踩" to match volume filter (量比>1).

### Removed
- Unused: `send_daily_report`, `get_notification_service` (notification.py); `analyze_stock` wrapper (stock_analyzer.py); `RealtimeQuote` alias (akshare); `EfinanceRealtimeQuote` (efinance); module-level test blocks in notification.py and stock_analyzer.py.
