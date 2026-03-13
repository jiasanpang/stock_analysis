# 配置指南

## 环境变量

### 必填

| 变量 | 说明 |
|------|------|
| `STOCK_LIST` | 自选股代码，如 `600519,hk00700,AAPL` |

### AI 模型（至少配置一个）

| 变量 | 说明 |
|------|------|
| `GEMINI_API_KEY` | Google AI Studio 免费 Key |
| `OPENAI_API_KEY` | OpenAI 兼容 API（DeepSeek、通义千问等） |
| `OPENAI_BASE_URL` | API 地址，如 `https://api.deepseek.com/v1` |
| `OPENAI_MODEL` | 模型名，如 `deepseek-chat` |
| `LITELLM_MODEL` | 主模型，格式 `provider/model` |
| `LITELLM_FALLBACK_MODELS` | 备选模型，逗号分隔 |

### 通知渠道（至少配置一个）

| 变量 | 说明 |
|------|------|
| `WECHAT_WEBHOOK_URL` | 企业微信 Webhook |
| `FEISHU_WEBHOOK_URL` | 飞书 Webhook |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram |
| `EMAIL_SENDER` + `EMAIL_PASSWORD` | 邮件 SMTP |
| `DISCORD_WEBHOOK_URL` | Discord |
| `PUSHPLUS_TOKEN` | PushPlus（国内） |
| `CUSTOM_WEBHOOK_URLS` | 自定义（钉钉等） |

### 搜索与数据

| 变量 | 说明 |
|------|------|
| `TAVILY_API_KEYS` | 新闻搜索（推荐） |
| `BOCHA_API_KEYS` | 博查搜索（中文优化） |
| `TUSHARE_TOKEN` | Tushare Pro（可选） |

### 其他

| 变量 | 说明 | 默认 |
|------|------|------|
| `REPORT_TYPE` | `simple` / `full` | - |
| `AGENT_MODE` | 启用 Agent 问股 | `false` |
| `TRADING_DAY_CHECK_ENABLED` | 交易日检查 | `true` |
| `SCHEDULE_TIME` | 定时执行时间 | `18:00` |

## GitHub Actions

1. Fork 仓库
2. `Settings` → `Secrets and variables` → `Actions` → 添加 Secrets
3. `Actions` → 启用工作流
4. `Actions` → `每日股票分析` → `Run workflow` 测试

定时：默认工作日 18:00（北京时间）。修改 `.github/workflows/daily_analysis.yml` 中的 `cron` 可调整。

## Docker

```bash
cp .env.example .env
vim .env   # 填入配置

# Web 服务模式
docker-compose -f docker/docker-compose.yml up -d server

# 定时任务模式
docker-compose -f docker/docker-compose.yml up -d analyzer
```

访问 http://localhost:8000

## 本地运行

```bash
pip install -r requirements.txt
cp .env.example .env
python main.py --serve-only   # 仅 Web 服务
python main.py                # 执行一次完整分析
python main.py --schedule     # 定时任务模式
```

## 通知渠道配置

- **企业微信/飞书**：群聊添加机器人，复制 Webhook URL
- **Telegram**：@BotFather 创建 Bot，@userinfobot 获取 Chat ID
- **邮件**：开启 SMTP，使用授权码（非登录密码）
- **钉钉**：群设置添加机器人，复制 Webhook。命令交互见 [钉钉配置](bot/dingding-bot-config.md)

## LLM 配置

**简单模式**：配置 `GEMINI_API_KEY` 或 `OPENAI_API_KEY` + `OPENAI_BASE_URL` + `OPENAI_MODEL` 即可。

**多模型备用**：使用 `LITELLM_MODEL` 和 `LITELLM_FALLBACK_MODELS`。

**渠道模式**：`LLM_CHANNELS=name1,name2`，每个渠道配置 `LLM_{NAME}_API_KEY`、`LLM_{NAME}_BASE_URL`、`LLM_{NAME}_MODELS`。

验证：`python test_env.py --llm`
