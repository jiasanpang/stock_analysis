# -*- coding: utf-8 -*-
"""Weekly review module for V2 recap system.

Generates a comprehensive weekly review covering:
1. Weekly index performance & sector rotation
2. Holdings P&L, win rate, max drawdown
3. Weakened vs core holdings adjustment
4. Next week position planning

Triggered by ``python main.py --weekly-review`` or GitHub Actions (Sunday).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src._review_v2_types import (
    WeeklyIndexPerformance,
    WeeklyReviewData,
    WeeklySectorEntry,
)
from src.config import get_config

logger = logging.getLogger(__name__)


def run_weekly_review(
    analyzer=None,
    search_service=None,
    notifier=None,
    send_notification: bool = True,
) -> Optional[str]:
    """执行周度复盘.

    Args:
        analyzer: AI 分析器
        search_service: 搜索服务
        notifier: 通知服务
        send_notification: 是否发送通知

    Returns:
        周度复盘报告文本
    """
    from data_provider.base import DataFetcherManager
    from src.notification_service import NotificationService

    logger.info("========== 开始周度复盘 ==========")
    config = get_config()
    manager = DataFetcherManager()

    # 计算本周日期范围 (周一到周五)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # 本周一
    week_end = week_start + timedelta(days=4)  # 本周五
    if today < week_end:
        # 如果还没到周五，用上周
        week_end = week_start - timedelta(days=1)  # 上周五
        week_start = week_end - timedelta(days=4)  # 上周一

    week_start_str = week_start.strftime('%Y-%m-%d')
    week_end_str = week_end.strftime('%Y-%m-%d')
    logger.info(f"[WeeklyReview] 周期: {week_start_str} ~ {week_end_str}")

    # 1. 周度指数表现
    weekly_indices = _compute_weekly_indices(manager, week_start, week_end)

    # 2. 板块轮动
    sector_rotation, strongest_mainline = _compute_sector_rotation(manager, week_start, week_end)

    # 3. 持仓周度统计
    stock_codes = getattr(config, 'stock_list', []) or []
    pnl_pct, win_rate, max_dd, holdings_detail, weakened, core = _compute_holdings_stats(
        manager, stock_codes, week_start, week_end,
    )

    # 4. 组装数据
    weekly_data = WeeklyReviewData(
        week_start=week_start_str,
        week_end=week_end_str,
        weekly_indices=weekly_indices,
        sector_rotation=sector_rotation,
        strongest_mainline=strongest_mainline,
        weekly_pnl_pct=pnl_pct,
        win_rate=win_rate,
        max_drawdown=max_dd,
        holdings_detail=holdings_detail,
        weakened_holdings=weakened,
        core_positions=core,
        data_available=True,
    )

    # 5. 生成报告
    report = _generate_weekly_report(weekly_data, analyzer)

    # 6. 保存 & 推送
    if report and notifier:
        date_str = datetime.now().strftime('%Y%m%d')
        filename = f"weekly_review_{date_str}.md"
        filepath = notifier.save_report_to_file(
            f"# 📊 周度复盘\n\n{report}", filename,
        )
        logger.info(f"[WeeklyReview] 报告已保存: {filepath}")

        if send_notification and notifier.is_available():
            content = f"📊 周度复盘 ({week_start_str}~{week_end_str})\n\n{report}"
            notifier.send(content, email_send_to_all=True)
            logger.info("[WeeklyReview] 周度复盘已推送")

    logger.info("========== 周度复盘完成 ==========")
    return report


# ---------------------------------------------------------------------------
# Data computation
# ---------------------------------------------------------------------------

def _compute_weekly_indices(
    manager, week_start: date, week_end: date,
) -> List[WeeklyIndexPerformance]:
    """Compute weekly performance for major indices."""
    results = []
    # 上证指数、深成指、创业板、沪深300
    index_map = {
        "000001": "上证指数",
        "399001": "深证成指",
        "399006": "创业板指",
        "000300": "沪深300",
    }
    for code, name in index_map.items():
        try:
            df, _ = manager.get_index_daily_data(
                index_code=f"{code}.SH" if code.startswith("0") else f"{code}.SZ",
                days=10,
            )
            if df is None or df.empty or len(df) < 2:
                continue
            df = df.sort_values('date').tail(6)  # 最近 5-6 个交易日
            close_col = next((c for c in ('close', '收盘') if c in df.columns), None)
            if close_col is None:
                continue
            start_price = float(df.iloc[0][close_col])
            end_price = float(df.iloc[-1][close_col])
            if start_price > 0:
                change_pct = (end_price - start_price) / start_price * 100
                results.append(WeeklyIndexPerformance(
                    name=name,
                    weekly_change_pct=change_pct,
                ))
        except Exception as e:
            logger.warning(f"[WeeklyReview] 指数 {name} 计算失败: {e}")
    return results


def _compute_sector_rotation(
    manager, week_start: date, week_end: date,
) -> Tuple[List[WeeklySectorEntry], str]:
    """Compute weekly sector rotation.

    Returns:
        (sorted sector list, strongest mainline name)
    """
    try:
        top_sectors, bottom_sectors = manager.get_sector_rankings(10)
    except Exception as e:
        logger.warning(f"[WeeklyReview] 板块数据获取失败: {e}")
        return [], ""

    entries = []
    for s in (top_sectors or []):
        entries.append(WeeklySectorEntry(
            name=s.get('name', ''),
            weekly_change_pct=s.get('change_pct', 0.0),
            trend="领涨",
        ))
    for s in (bottom_sectors or []):
        entries.append(WeeklySectorEntry(
            name=s.get('name', ''),
            weekly_change_pct=s.get('change_pct', 0.0),
            trend="领跌",
        ))

    entries.sort(key=lambda e: e.weekly_change_pct, reverse=True)
    strongest = entries[0].name if entries else ""
    return entries, strongest


def _compute_holdings_stats(
    manager,
    stock_codes: List[str],
    week_start: date,
    week_end: date,
) -> Tuple[float, float, float, List[Dict], List[str], List[str]]:
    """Compute weekly holdings P&L.

    Returns:
        (pnl_pct, win_rate, max_drawdown, holdings_detail, weakened, core)
    """
    if not stock_codes:
        return 0.0, 0.0, 0.0, [], [], []

    changes = []
    details = []
    weakened = []
    core = []

    for code in stock_codes:
        try:
            df, _ = manager.get_daily_data(
                code,
                start_date=(week_start - timedelta(days=5)).strftime('%Y-%m-%d'),
                end_date=week_end.strftime('%Y-%m-%d'),
                days=10,
            )
            if df is None or df.empty or len(df) < 2:
                continue
            df = df.sort_values('date')
            start_price = float(df.iloc[0]['close'])
            end_price = float(df.iloc[-1]['close'])
            if start_price <= 0:
                continue

            change_pct = (end_price - start_price) / start_price * 100
            changes.append(change_pct)

            # 周最大回撤
            high = float(df['high'].max()) if 'high' in df.columns else end_price
            low = float(df['low'].min()) if 'low' in df.columns else start_price
            dd = (low - high) / high * 100 if high > 0 else 0.0

            name = str(df.iloc[-1].get('name', code)) if 'name' in df.columns else code
            details.append({
                'code': code, 'name': name,
                'weekly_change': change_pct, 'max_dd': dd,
            })

            if change_pct < -5.0:
                weakened.append(f"{name}({code})")
            elif change_pct >= 0:
                core.append(f"{name}({code})")

        except Exception as e:
            logger.warning(f"[WeeklyReview] {code} 周度数据获取失败: {e}")

    pnl_pct = sum(changes) / len(changes) if changes else 0.0
    win_rate = (sum(1 for c in changes if c > 0) / len(changes) * 100) if changes else 0.0
    max_dd = min((d['max_dd'] for d in details), default=0.0)

    return pnl_pct, win_rate, max_dd, details, weakened, core


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _generate_weekly_report(data: WeeklyReviewData, analyzer=None) -> str:
    """Generate weekly report via LLM or template."""
    if analyzer and analyzer.is_available():
        return _generate_llm_weekly_report(data, analyzer)
    return _generate_template_weekly_report(data)


def _generate_llm_weekly_report(data: WeeklyReviewData, analyzer) -> str:
    """Generate weekly report using LLM."""
    prompt = _build_weekly_prompt(data)
    logger.info("[WeeklyReview] 调用 AI 生成周度报告...")
    report = analyzer.generate_text(prompt, max_tokens=3000, temperature=0.7)
    if report:
        return report
    logger.warning("[WeeklyReview] AI 返回空，使用模板")
    return _generate_template_weekly_report(data)


def _build_weekly_prompt(data: WeeklyReviewData) -> str:
    """Build LLM prompt for weekly review."""
    # Indices
    idx_lines = []
    for idx in data.weekly_indices:
        idx_lines.append(f"- {idx.name}: 周涨跌幅 {idx.weekly_change_pct:+.2f}%")
    idx_text = "\n".join(idx_lines) if idx_lines else "暂无"

    # Sectors
    sector_lines = []
    for s in data.sector_rotation[:10]:
        sector_lines.append(f"- {s.name}: {s.weekly_change_pct:+.2f}% ({s.trend})")
    sector_text = "\n".join(sector_lines) if sector_lines else "暂无"

    # Holdings
    holding_lines = []
    for h in data.holdings_detail:
        holding_lines.append(f"- {h['name']}({h['code']}): 周涨跌 {h['weekly_change']:+.2f}%")
    holding_text = "\n".join(holding_lines) if holding_lines else "未配置持仓"

    return f"""你是一位资深市场分析师，请根据以下周度数据生成一份深度周度复盘报告。

【输出要求】纯 Markdown 格式，专业但易懂。

# 本周数据
周期: {data.week_start} ~ {data.week_end}

## 指数表现
{idx_text}

## 板块表现
{sector_text}

最强主线: {data.strongest_mainline}

## 持仓表现
{holding_text}

组合周收益: {data.weekly_pnl_pct:+.2f}%
胜率: {data.win_rate:.0f}%
最大回撤: {data.max_drawdown:.2f}%

弱化持仓: {', '.join(data.weakened_holdings) or '无'}
核心持仓: {', '.join(data.core_positions) or '无'}

---

# 输出格式

## {data.week_start}~{data.week_end} 周度复盘

### 一、本周指数与板块轮动
(分析本周指数走势特点、最强主线板块及其持续性)

### 二、持仓周度统计
(组合收益、胜率、回撤分析；逐一点评表现最强和最弱的持仓)

### 三、调仓建议
(建议剔除的弱化持仓及原因、保留的核心持仓及逻辑)

### 四、下周计划
(整体仓位建议: 行情差3成以内/行情好7成以内; 关注方向; 风控要点)

请直接输出报告内容。"""


def _generate_template_weekly_report(data: WeeklyReviewData) -> str:
    """Generate weekly report using template (no LLM)."""
    # Indices
    idx_lines = []
    for idx in data.weekly_indices:
        arrow = "↑" if idx.weekly_change_pct > 0 else "↓"
        idx_lines.append(f"- **{idx.name}**: {arrow}{abs(idx.weekly_change_pct):.2f}%")
    idx_text = "\n".join(idx_lines) if idx_lines else "暂无数据"

    # Sectors
    sector_lines = []
    for s in data.sector_rotation[:5]:
        sector_lines.append(f"- **{s.name}**: {s.weekly_change_pct:+.2f}%")
    sector_text = "\n".join(sector_lines) if sector_lines else "暂无数据"

    # Holdings
    holding_lines = []
    for h in data.holdings_detail:
        holding_lines.append(f"- **{h['name']}**: {h['weekly_change']:+.2f}%")
    holding_text = "\n".join(holding_lines) if holding_lines else "未配置持仓"

    weakened_text = ", ".join(data.weakened_holdings) if data.weakened_holdings else "无"
    core_text = ", ".join(data.core_positions) if data.core_positions else "无"

    return f"""## {data.week_start}~{data.week_end} 周度复盘

### 一、本周指数表现
{idx_text}

最强主线板块: {data.strongest_mainline}

### 二、板块轮动
{sector_text}

### 三、持仓统计
{holding_text}

组合周收益: **{data.weekly_pnl_pct:+.2f}%** | 胜率: **{data.win_rate:.0f}%** | 最大回撤: **{data.max_drawdown:.2f}%**

### 四、调仓建议
弱化持仓(建议关注): {weakened_text}
核心持仓(建议保留): {core_text}

### 五、下周计划
市场有风险，投资需谨慎。以上分析仅供参考，不构成投资建议。

---
*周度复盘时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}*
"""
