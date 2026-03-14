# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> For user-friendly release highlights, see the [GitHub Releases](https://github.com/jiasanpang/stock_analysis/releases) page.

## [Unreleased]

### Added
- `PUSH_REPORT_TYPE`: Separate report type for push notifications. When set (e.g. `brief`), push stays short while dashboard/file/Feishu doc remain detailed (`REPORT_TYPE`).
- `NOTIFY_ENABLED`: When `false`, disable all push notifications (for local runs). Default `true`.
- Stock picker bias filter: Layer 5 excludes candidates with MA5 bias > 8% (严进策略). Requires daily history from data provider.
- `--picker-only`: Run AI stock picker only (skip individual stock analysis and market review). Use `./test.sh picker` for quick verification.
- Stock picker 60-day change for Tushare: Compute 60日涨跌幅 via trade_cal + daily when using Tushare (AkShare spot already includes it).
- Stock picker constants: `VOLUME_RATIO_MIN`, `TREND_DECAY_THRESHOLD_PCT` for maintainability.
- `./test.sh picker-validation`: Offline validation of picker logic (60d decay, volume filter, prompt).

### Changed
- Workflow default: `REPORT_TYPE=simple` (detailed analysis/dashboard), `PUSH_REPORT_TYPE=brief` (short push).
- `docs/analysis-strategy-guide.md`: Added AI picker bias filter description, chase-risk exclusion (today > 9%), scope and limitations section.
- Stock picker: LLM picks 1-5 (was 3-8), explicit empty-position trigger (乖离率>5%占比>60%).
- Stock picker: 60-day gain >30% score decay to avoid end-of-trend buys.
- Stock picker: Volume ratio filter 0.8 → 1.0 to exclude cold stocks.
- Stock picker: PE filter 200 → 100 (PE_MAX constant).
- Stock picker: Limit-up streak filter — exclude 2+ limit-up days in last 5 days (连板/妖股).

### Removed
- Unused: `send_daily_report`, `get_notification_service` (notification.py); `analyze_stock` wrapper (stock_analyzer.py); `RealtimeQuote` alias (akshare); `EfinanceRealtimeQuote` (efinance); module-level test blocks in notification.py and stock_analyzer.py.
