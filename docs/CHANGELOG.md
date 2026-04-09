# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> For user-friendly release highlights, see the [GitHub Releases](https://github.com/jiasanpang/stock_analysis/releases) page.

## [Unreleased]

### Added
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

### Changed
- **refactor(picker)**: buy_pullback 策略参数全面收紧 — 启用缩量检查、量比0.7、乖离率5%、PE上限60、日涨幅上限2%、60日上限40%、回调限制0.4
- **refactor(picker)**: buy_pullback 评分优化 — 缩量(0.5-0.9)最高分、深回踩(-3%~-1%)最高分、放量(>3.0)惩罚
- **refactor(picker)**: 底部反转策略参数优化 — 启用缩量检查、量比0.7、60日上限-5%、B浪过滤0.618
- **refactor(picker)**: 底部反转评分增强 — 60日跌幅深度加分、缩量→放量转折信号、反转K线形态加分
- **refactor(picker)**: bottom_reversal `daily_change_min` 0.0→1.0，macd_golden_cross 0.0→0.5，实时验证更严格
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
- Demo script: `test_realtime_filter_demo.py` shows filtering logic in action

### Changed
- Picker pipeline now has 3 stages:
  1. Quantitative screening (historical daily data)
  2. **Real-time filtering** (NEW: intraday market conditions)
  3. AI selection (LLM picks 1-5 from filtered pool)

### Picker page: Pill-style strategy chips (buy_pullback, breakout, bottom_reversal, macd_golden_cross), card container, result view shows strategy + timestamp.
- Picker backtest: Strategy selection (replaces mode dropdown). API accepts `picker_strategies`; history stores `picker_strategies_json`.
- Per-strategy leader bias exemption: `leader_bias_exempt_pct` in StrategyParams (breakout=14%, others=0). Removed global `PICKER_LEADER_BIAS_EXEMPT_PCT`.

### Changed
- GitHub Actions `daily_analysis.yml`: default schedule Beijing 18:00 (`cron`: UTC 10:00 Mon–Fri).
- Picker backtest: Remove `picker_mode` and `picker_leader_bias_exempt_pct` from API. Use `picker_strategies` only.
- Backtest page: holdDays/topN inputs allow empty (string state, parse on submit).
- Picker footer tagline: "买回踩 · 突破 · 底部反转 · MACD金叉 — 多策略并行，按需组合".

### Removed
- `PICKER_LEADER_BIAS_EXEMPT_PCT` env var (leader exemption now per-strategy).
- Picker mode (严进/平衡/进攻) from picker backtest API and UI.

### Added (previous)
- Multi-strategy picker: `PICKER_STRATEGIES` (comma-separated) runs multiple strategies in parallel. Strategies: `buy_pullback` (买回踩), `breakout` (突破), `bottom_reversal` (底部反转), `macd_golden_cross` (MACD金叉). Candidates merged and tagged by strategy. No intensity modes (defensive/balanced/offensive) — each strategy has fixed params.
- MACD golden cross strategy: uses `pandas-ta-classic` for MACD (fast=12, slow=26, signal=9). Filters candidates where DIF crosses above DEA in last 2 days. Params: 60d -15% ~ 50%, daily 0% ~ 6%, volume_ratio_min 1.0.
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
