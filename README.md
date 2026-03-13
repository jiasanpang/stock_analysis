# Stock Analysis

AI-powered stock analysis for A-shares, HK, and US markets. Daily reports, strategy Q&A, and quantitative screening.

---

## What it does

- **Analysis** — Input stock codes, get AI-generated reports with buy/sell points, risk alerts, and sentiment scores
- **Agent Chat** — Multi-turn strategy Q&A (Chan theory, wave theory, MA crossover, etc.) via Web or API
- **AI Picker** — Two-stage screening: quantitative filters (fundamentals → momentum → volume) + LLM selection from 5000+ A-shares
- **Backtest** — Evaluate historical analysis accuracy (direction, stop-loss, take-profit)
- **Notifications** — WeChat Work, Feishu, Telegram, email, Pushover
- **Automation** — GitHub Actions, no server required

## Quick start

```bash
git clone https://github.com/jiasanpang/stock_analysis.git && cd stock_analysis
pip install -r requirements.txt
cp .env.example .env   # edit STOCK_LIST and at least one LLM key
python main.py --webui-only
```

Open http://127.0.0.1:8000

**GitHub Actions**: Fork → add Secrets (`STOCK_LIST`, `GEMINI_API_KEY` or `OPENAI_API_KEY`, optional notification URLs) → enable Actions → run workflow. See [full guide](docs/full-guide.md).

## Requirements

- Python 3.10+
- LLM: Gemini, OpenAI-compatible, Claude, DeepSeek, etc. (via [LiteLLM](https://github.com/BerriAI/litellm))
- Data: AkShare, Tushare, YFinance
- News: Tavily, SerpAPI, Bocha, Brave, MiniMax

## Docs

- [Full guide](docs/full-guide.md) — env vars, Docker, scheduling
- [LLM config](docs/LLM_CONFIG_GUIDE.md) — model setup, channels, fallbacks
- [FAQ](docs/FAQ.md)
- [Changelog](docs/CHANGELOG.md)

## License

MIT

---

*For learning and research only. Not investment advice.*
