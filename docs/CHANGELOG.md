# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> For user-friendly release highlights, see the [GitHub Releases](https://github.com/jiasanpang/stock_analysis/releases) page.

## [Unreleased]

### Fixed
- Picker API: Fix `PickerResponse() got multiple values for keyword argument 'picker_mode'` — remove redundant kwargs since `result_dict` already includes them.

### Added
- `PICKER_SPOT_TIMEOUT`: Timeout (seconds) for full-market spot data fetch (AkShare/efinance). Default 30. Increase when Eastmoney API is slow.
- `PICKER_ALLOW_LOSS`: When `true`, allow loss-making stocks (PE≤0) in picker pool. Default `false`.
- Picker backtest: `PickerBacktestService` runs quantitative screener historically, evaluates forward returns. API: `POST /api/v1/picker-backtest/run`, `GET /picker-backtest/performance`, `GET /picker-backtest/results`. Frontend: "选股回测" tab on Backtest page.
- Stock screener `screen_as_of(trade_date)` for historical screening (Tushare only).
- `PUSH_REPORT_TYPE`: Separate report type for push notifications. When set (e.g. `brief`), push stays short while dashboard/file/Feishu doc remain detailed (`REPORT_TYPE`).
- `NOTIFY_ENABLED`: When `false`, disable all push notifications (for local runs). Default `true`.
- Stock picker bias filter: Layer 5 excludes candidates with MA5 bias > 8% (严进策略). Requires daily history from data provider.
- `--picker-only`: Run AI stock picker only (skip individual stock analysis and market review). Use `./test.sh picker` for quick verification.
- Stock picker 60-day change for Tushare: Compute 60日涨跌幅 via trade_cal + daily when using Tushare (AkShare spot already includes it).
- Stock picker constants: `VOLUME_RATIO_MIN`, `TREND_DECAY_THRESHOLD_PCT` for maintainability.
- `./test.sh picker-validation`: Offline validation of picker logic (60d decay, volume filter, prompt).

### Changed
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
