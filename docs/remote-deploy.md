# 远端部署与自动通知

README 已有 [快速开始](../README.md#快速开始) 4 步，本文档补充 **Token 获取方式** 和 **自建服务器** 部署。

---

## GitHub Actions：Secrets 清单

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

### 必填

| Name | 获取方式 |
|------|----------|
| `STOCK_LIST` | 自选股代码，逗号分隔 |
| AI 模型（至少 1 个） | 见下表 |
| 通知（至少 1 个） | 见下表 |

### AI 模型 Token

| Name | 获取地址 |
|------|----------|
| `GEMINI_API_KEY` | https://aistudio.google.com |
| `AIHUBMIX_KEY` | https://aihubmix.com |
| `OPENAI_API_KEY` + `OPENAI_BASE_URL` + `OPENAI_MODEL` | DeepSeek / 通义等兼容 API |
| `LLM_CHANNELS` + `LLM_DASHSCOPE_*` | 阿里云百炼，见 [配置指南](guide.md) LLM 渠道 |

### 通知 Token

| Name | 获取方式 |
|------|----------|
| `FEISHU_WEBHOOK_URL` | 飞书群 → 设置 → 群机器人 → 自定义 |
| `WECHAT_WEBHOOK_URL` | 企业微信群 → 设置 → 群机器人 |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | @BotFather / @userinfobot |
| `PUSHPLUS_TOKEN` | https://www.pushplus.plus |

### 运行配置

| Name | 说明 |
|------|------|
| `PICKER_ENABLED` | 每日分析后是否运行 AI 智能选股并推送（默认 true） |
| `REPORT_TYPE` | 报告类型（分析/仪表盘）：`simple`、`full`、`brief` |
| `PUSH_REPORT_TYPE` | 推送报告类型：不配置则同 REPORT_TYPE；可设为 `brief` 使推送精简、仪表盘保持详细 |

### 可选（搜索 / 数据）

| Name | 获取地址 |
|------|----------|
| `MINIMAX_API_KEYS` | https://platform.minimaxi.com |
| `BOCHA_API_KEYS` | https://open.bocha.cn |
| `TAVILY_API_KEYS` | https://tavily.com |
| `TUSHARE_TOKEN` | https://tushare.pro |

---

## 自建服务器

```bash
git clone https://github.com/jiasanpang/stock_analysis.git && cd stock_analysis
pip install -r requirements.txt && cp .env.example .env
# 编辑 .env：STOCK_LIST、AI、通知、SCHEDULE_ENABLED=true、SCHEDULE_TIME=18:00
```

**systemd 开机自启**：创建 `/etc/systemd/system/stock-analysis.service`：

```ini
[Unit]
Description=Stock Analysis Scheduler
After=network.target

[Service]
Type=simple
User=你的用户名
WorkingDirectory=/path/to/stock_analysis
ExecStart=/path/to/stock_analysis/.venv/bin/python main.py --schedule
Restart=always
RestartSec=10
EnvironmentFile=/path/to/stock_analysis/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable stock-analysis && sudo systemctl start stock-analysis
```

---

## 常见问题

- **没收到通知？** 检查 Secrets 是否正确，Run workflow 查看日志
- **改执行时间？** 编辑 `.github/workflows/daily_analysis.yml` 的 `cron`
- **非交易日？** 默认跳过；设 `TRADING_DAY_CHECK_ENABLED=false` 或 `--force-run` 强制执行
