# 股票智能分析系统

基于 AI 大模型的 A股/港股/美股智能分析系统。自动分析自选股 → 生成决策仪表盘 → 多渠道推送。

**零成本部署** · GitHub Actions 免费运行 · 无需服务器

## 功能特性

| 模块 | 说明 |
| ---- | ---- |
| AI 分析 | 决策仪表盘、精确买卖点位、操作检查清单；动态乖离率阈值（ATR自适应）、资金面评分（主力+北向资金）、多日量能趋势、Trailing 阶梯止盈止损 |
| AI 选股 | 量化筛选 + 实时过滤 + LLM 精选 1–5 只（涨停回踩/底部反转），输出**理想买入价 / 止损 / 首止盈 / 盈亏比 / 仓位建议**，盈亏比 R/R<2.0 自动剔除，多策略共振加分，同行业最多保留 2 只 |
| 多维度 | 技术面 + 资金面 + 筹码分布 + 舆情情报 + 实时行情 |
| 市场 | A股、港股、美股 |
| Agent 问股 | 多轮策略问答，12 种内建策略（Web/Bot/API） |
| 回测 | 严格按 AI 推荐的买入价/止损/止盈在历史 K 线上模拟成交（含 0.05% 滑点 + 0.075% 单边手续费 + 涨停过滤），输出三层指标：方向胜率 / 成交率·R 倍数·盈亏比·最大回撤 / MAE·MFE，并按信号、量化分、盈亏比、退出原因等多维度归因 |
| 推送 | 企业微信、飞书、钉钉、Telegram、Discord、邮件、Pushover 等 |
| 自动化 | GitHub Actions 定时执行（大盘复盘 + 个股分析 + AI选股） |

## 快速开始

### GitHub Actions（推荐）

1. **Fork** 本仓库
2. **配置 Secrets**：`Settings` → `Secrets and variables` → `Actions`
   - 必填：`STOCK_LIST`（如 `600519,hk00700,AAPL`）
   - AI：支持 Gemini、OpenAI、DeepSeek、Anthropic、DashScope（通义千问）等，配置任一即可
   - 通知：`WECHAT_WEBHOOK_URL` / `TELEGRAM_BOT_TOKEN` / `EMAIL_SENDER` 等（至少一个）
3. **启用 Actions**：`Actions` → 启用工作流
4. **手动测试**：`Actions` → `每日股票分析` → `Run workflow`

默认每个工作日 18:00（北京时间）自动执行。

### 本地 / Docker

```bash
git clone https://github.com/jiasanpang/stock_analysis.git && cd stock_analysis
pip install -r requirements.txt
cp .env.example .env   # 编辑配置
python main.py --serve-only   # 启动 Web 服务
```

访问 http://127.0.0.1:8000

Docker：`docker-compose -f docker/docker-compose.yml up -d server`

## 推送效果示例

```
📊 决策仪表盘 | 🟢买入:1 🟡观望:2 🔴卖出:0

🟢 贵州茅台(600519) | 缩量回踩MA5支撑，乖离率1.2%处于最佳买点
💰 买入1800 | 止损1750 | 目标1900

🟡 宁德时代(300750) | 乖离率7.8%超过警戒线，严禁追高
```

**个股评分体系**（7维度满分 100）：趋势(30) + 量能(18) + 乖离率(15) + MACD(13) + 资金面(13) + 支撑(6) + RSI(5)

- 大盘环境修正因子：基于上证 MA20 方向自动调整（强空头 ×0.75 cap 60、空头 ×0.85 cap 75、强多头 ×1.05），熊市 BUY 阈值 +10 防止假信号
- **估值惩罚**：PE>200 -15、PE>100 -8、亏损股 -5；资金面分 [0,10] 防御性 clip
- **加速期/滞涨陷阱识别**：20日涨幅 >30% 或连阳 ≥5 时放量上涨降权 14→6；强势趋势中缩量上涨降权 7→3
- **统一交易点位引擎**（`trade_levels.py`）：picker / analyzer / backtest 共用一份 `ideal_buy / stop_loss / take_profit_1 / position_pct / risk_reward` 计算
- **Trailing 阶梯止盈**：浮盈 6% 减 1/3 + 止损上移至成本 → 浮盈 12% 再减 1/3 + 止损至 +6% → 浮盈 ≥15% trailing（跌破 MA10 或回撤 ATR×2.5），让强势股利润奔跑
- **盈亏比硬过滤**：R/R < 2.0 直接剔除；双策略共振 +8、三策略共振 +25 加分
- **行业集中度上限**：同 SW 一级行业最多保留 2 只（环境变量 `PICKER_INDUSTRY_TOP_N` 可调）
- **基本面硬否决**：质押率 >50% / 高商誉 / 10 日内 ≥1% 减持 / 业绩预减股 picker 入池前剔除
- **市场环境感知仓位**：弱市 ×0.6 / 中性 ×0.85 / 强市 ×1.0 自动缩放
- 止损建议：小盘 -6% / 中盘 -6% / 大盘 -7%，配合 MA20 技术止损

## 技术栈

- **后端**：Python 3.10+ / FastAPI / Uvicorn
- **AI**：LiteLLM 统一接口，支持 Gemini、OpenAI、DeepSeek、Anthropic、DashScope 等 9+ 模型渠道
- **数据源**：Tushare、AkShare、efinance、通达信(pytdx)、BaoStock、yfinance
- **前端**：Vite + TypeScript + React（Web）/ Electron（桌面端）
- **部署**：GitHub Actions / Docker / 本地

## 文档

- [配置指南](docs/guide.md) — 环境变量、Docker、LLM、通知渠道
- [分析策略指南](docs/analysis-strategy-guide.md) — 核心理念、选股原则、策略说明
- [选股策略详解](docs/picker-strategies-guide.md) — 量化筛选策略（涨停回踩/底部反转）参数与流程
- **[实时行情筛选指南](docs/realtime-picker-guide.md)** — 🆕 支持当天选股+操作，实时筛选规则配置
- [远端部署](docs/remote-deploy.md) — Token 获取、自建服务器
- [常见问题](docs/faq.md)
- [更新日志](docs/CHANGELOG.md)

## 贡献

欢迎提交 Issue 和 Pull Request。详见 [贡献指南](docs/CONTRIBUTING.md)。

## License

[MIT License](LICENSE)

本项目参考自 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)。使用或二次开发请注明来源并附上链接。

## 免责声明

本项目仅供学习研究，不构成投资建议。股市有风险，投资需谨慎。
