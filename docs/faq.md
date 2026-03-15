# 常见问题

## 数据相关

**美股价格显示不对？** 已修复。可设置 `YFINANCE_PRIORITY=0` 优先使用 YFinance。

**量比显示为空？** 配置 `REALTIME_SOURCE_PRIORITY=tencent,akshare_sina,efinance,akshare_em`。

**Tushare Token 错误？** 可不配置，系统自动使用 AkShare 等免费源。有账号则在 Tushare 个人中心确认 Token。若遇 `Operation not permitted: tk.csv`，已修复：现直接传入 token，不再写入 `~/tk.csv`。

**数据限流/熔断？** 减少自选股数量、增加请求间隔。东财频繁失败可设 `ENABLE_EASTMONEY_PATCH=true`。

## 配置相关

**GitHub Actions 找不到环境变量？** Secrets 存敏感信息（API Key、Webhook），Variables 存非敏感（STOCK_LIST、REPORT_TYPE）。确认配置在正确位置。

**修改 .env 后未生效？** Docker 需重启容器。本地确保 `.env` 在项目根目录。

**非交易日想执行？** 手动触发时勾选 `force_run`，或设 `TRADING_DAY_CHECK_ENABLED=false`。

**本地分析不想推送？** 设 `NOTIFY_ENABLED=false`，或命令行加 `--no-notify`。

## 其他

**Web 服务启动失败？** 检查端口 8000 是否被占用。`WEBUI_AUTO_BUILD=false` 可关闭自动编译前端。

**更多问题**：搜索或提交 [GitHub Issue](https://github.com/jiasanpang/stock_analysis/issues)。
