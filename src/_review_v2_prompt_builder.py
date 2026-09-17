# -*- coding: utf-8 -*-
"""LLM prompt builder for V2 four-step daily recap.

Builds a structured prompt from ``DailyReviewV2`` data that guides the LLM
to produce a comprehensive review with five sections:

1. 大盘全景 (indices, volume, north flow, sectors, news)
2. 涨停情绪 (limit-up ladder, broken rate, loss effect)
3. 持仓复盘 (per-holding analysis)
4. 次日观察池 (watch pool candidates)
5. 明日策略 (position, rhythm, risk control)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, List

if TYPE_CHECKING:
    from src._review_v2_types import DailyReviewV2


def compose_v2_daily_prompt(data: DailyReviewV2) -> str:
    """Assemble the full V2 daily review LLM prompt.

    Args:
        data: ``DailyReviewV2`` aggregate.

    Returns:
        Complete prompt string.
    """
    market_section = _build_market_section(data)
    limit_up_section = _build_limit_up_section(data)
    holdings_section = _build_holdings_section(data)
    watch_pool_section = _build_watch_pool_section(data)
    news_section = _build_news_section(data)
    sentiment_section = _build_sentiment_section(data)
    technical_section = _build_technical_section(data)

    return f"""你是一位资深的A股市场分析师，请根据以下全面的市场数据生成一份深度复盘报告。

【重要】输出要求：
- 必须输出纯 Markdown 文本格式
- 语言风格要专业但易懂，适合普通投资者阅读
- 每个章节都要有数据支撑和明确结论
- 给出明确的仓位建议和风险提示

---

# 今日市场数据

{market_section}

{sentiment_section}

{limit_up_section}

{holdings_section}

{watch_pool_section}

{technical_section}

{news_section}

---

# 输出格式模板（请严格按此格式输出）

## {data.date} 深度复盘

### 一、大盘全景
（3-5 句话概括今日市场：指数涨跌、成交额变化、北向资金动向、板块主线、消息面影响。必须结合具体数据。）

### 二、涨停情绪
（分析连板梯队结构、炸板率含义、亏钱效应、情绪温度。给出短线接力风险判断。）

### 三、持仓复盘
（对每只持仓逐一点评：强于/弱于大盘、技术位状态、是否继续持有/减仓/止损。买入逻辑是否还成立。）

### 四、次日观察池
（列出 3-5 只候选，每只说明入选理由、买入条件和止损位。强调"不追高、等回踩"原则。）

### 五、明日策略
（给出明确的仓位结论：进攻/均衡/防守，对应仓位比例建议。列出一个触发失效条件。最后补充"以上分析仅供参考，不构成投资建议"。）

---

请直接输出复盘报告内容，不要输出其他说明文字。
"""


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_market_section(data: DailyReviewV2) -> str:
    """Step 1: 大盘指数 + 涨跌统计 + 北向资金 + 板块."""
    ov = data.market_overview
    if ov is None:
        return "## 大盘数据\n暂无数据"

    # 指数
    idx_lines = []
    for idx in ov.indices:
        direction = "↑" if idx.change_pct > 0 else "↓" if idx.change_pct < 0 else "-"
        idx_lines.append(f"- {idx.name}: {idx.current:.2f} ({direction}{abs(idx.change_pct):.2f}%)")
    idx_text = "\n".join(idx_lines) if idx_lines else "暂无指数数据"

    # 涨跌统计
    stats_text = (
        f"- 上涨: {ov.up_count} 家 | 下跌: {ov.down_count} 家 | 平盘: {ov.flat_count} 家\n"
        f"- 涨停: {ov.limit_up_count} 家 | 跌停: {ov.limit_down_count} 家\n"
        f"- 两市成交额: {ov.total_amount:.0f} 亿元"
    ) if ov.up_count else "涨跌统计暂不可用"

    # 北向资金
    north_text = data.north_flow.flow_description if data.north_flow else "北向资金数据暂不可用"

    # 板块
    top_sectors = ", ".join(
        [f"{s['name']}({s['change_pct']:+.2f}%)" for s in ov.top_sectors[:5]]
    ) if ov.top_sectors else "暂无"
    bot_sectors = ", ".join(
        [f"{s['name']}({s['change_pct']:+.2f}%)" for s in ov.bottom_sectors[:3]]
    ) if ov.bottom_sectors else "暂无"

    return f"""## 大盘数据
日期: {data.date}

### 主要指数
{idx_text}

### 涨跌统计
{stats_text}

### 北向资金
{north_text}

### 领涨板块
{top_sectors}

### 领跌板块
{bot_sectors}"""


def _build_limit_up_section(data: DailyReviewV2) -> str:
    """Step 2: 涨停梯队 + 炸板率 + 亏钱效应."""
    lu = data.limit_up_ladder
    if lu is None or not lu.data_available:
        return "## 涨停数据\n涨停数据暂不可用"

    # 连板梯队详情
    ladder_lines = []
    for streak in sorted(lu.ladder.keys(), reverse=True):
        stocks = lu.ladder[streak]
        for s in stocks[:3]:
            ladder_lines.append(
                f"  {streak}连板: {s.name}({s.code}) "
                f"涨幅{s.change_pct:+.1f}% 行业:{s.sector}"
            )
    ladder_text = "\n".join(ladder_lines) if ladder_lines else "无涨停数据"

    return f"""## 涨停板 & 情绪
- 总涨停: {lu.total_limit_up} 家
- 总炸板: {lu.total_broken} 家 (炸板率: {lu.broken_rate:.1f}%)
- 跌停: {lu.dtgc_count} 家
- 最高连板: {lu.max_streak}连板
- 亏钱效应: {lu.loss_effect}

### 连板梯队
{ladder_text}"""


def _build_holdings_section(data: DailyReviewV2) -> str:
    """Step 3: 持仓复盘."""
    if not data.holdings:
        return "## 持仓数据\n未配置持仓 (STOCK_LIST 为空)"

    lines = []
    for h in data.holdings:
        lines.append(
            f"- **{h.name}**({h.code}): 涨跌 {h.change_pct:+.2f}% | "
            f"现价 {h.price:.2f} | {h.vs_market}\n"
            f"  MA5={h.ma5:.2f} MA10={h.ma10:.2f} MA20={h.ma20:.2f} | "
            f"排列: {h.ma_alignment} | MACD: {h.macd_status}\n"
            f"  支撑: {h.support_level:.2f} | 阻力: {h.resistance_level:.2f} | "
            f"量能: {h.volume_status}"
        )
    return f"## 持仓复盘\n" + "\n".join(lines)


def _build_watch_pool_section(data: DailyReviewV2) -> str:
    """Step 4: 次日观察池."""
    if not data.watch_pool:
        return "## 次日观察池\n暂无符合条件的候选"

    lines = []
    for w in data.watch_pool:
        lines.append(
            f"- **{w.name}**({w.code}) [{w.sector}] 涨幅 {w.change_pct:+.2f}%\n"
            f"  理由: {w.reason}\n"
            f"  买入条件: {w.buy_condition} | 止损: {w.stop_loss:.2f} | "
            f"盈亏比: {w.risk_reward:.2f}"
        )
    return f"## 次日观察池候选\n" + "\n".join(lines)


def _build_news_section(data: DailyReviewV2) -> str:
    """新闻摘要."""
    news = data.news
    if not news:
        return "## 市场新闻\n暂无相关新闻"

    parts = []
    for i, n in enumerate(news[:5], 1):
        if hasattr(n, "title"):
            title = (n.title or "")[:50]
            snippet = (n.snippet or "")[:100]
        else:
            title = n.get("title", "")[:50]
            snippet = n.get("snippet", "")[:100]
        parts.append(f"{i}. {title}\n   {snippet}")
    return "## 市场新闻\n" + "\n".join(parts)


def _build_sentiment_section(data: DailyReviewV2) -> str:
    """情绪分析摘要."""
    sa = data.sentiment
    if sa is None:
        return ""
    return f"""## 情绪指标
- 情绪等级: {sa.sentiment.value}
- 恐慌贪婪指数: {sa.fear_greed_index:.1f}/100
- 市场热度: {sa.market_heat:.1f}/100
- 资金流向: {sa.fund_flow_trend}
- 量比: {sa.volume_ratio:.1f}"""


def _build_technical_section(data: DailyReviewV2) -> str:
    """技术面分析."""
    ta = data.technical
    if ta is None:
        return ""
    return f"""## 技术面
- 趋势方向: {ta.trend_direction}
- 关键支撑: {ta.key_support:.0f}
- 关键阻力: {ta.key_resistance:.0f}
- 量价关系: {ta.volume_price_relation}
- 市场结构: {ta.market_structure}"""
