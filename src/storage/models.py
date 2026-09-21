# -*- coding: utf-8 -*-
"""
ORM models for the stock analysis storage layer.

Every model uses the shared SQLAlchemy ``Base`` declared at module top so the
``DatabaseManager`` can ``Base.metadata.create_all(engine)`` and pick them up.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

logger = logging.getLogger(__name__)

# SQLAlchemy ORM 基类
Base = declarative_base()


class StockDaily(Base):
    """
    股票日线数据模型
    
    存储每日行情数据和计算的技术指标
    支持多股票、多日期的唯一约束
    """
    __tablename__ = 'stock_daily'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 股票代码（如 600519, 000001）
    code = Column(String(10), nullable=False, index=True)
    
    # 交易日期
    date = Column(Date, nullable=False, index=True)
    
    # OHLC 数据
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    
    # 成交数据
    volume = Column(Float)  # 成交量（股）
    amount = Column(Float)  # 成交额（元）
    pct_chg = Column(Float)  # 涨跌幅（%）
    
    # 技术指标
    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    volume_ratio = Column(Float)  # 量比
    
    # 数据来源
    data_source = Column(String(50))  # 记录数据来源（如 AkshareFetcher）
    
    # 更新时间
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 唯一约束：同一股票同一日期只能有一条数据
    __table_args__ = (
        UniqueConstraint('code', 'date', name='uix_code_date'),
        Index('ix_code_date', 'code', 'date'),
    )
    
    def __repr__(self):
        return f"<StockDaily(code={self.code}, date={self.date}, close={self.close})>"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'date': self.date,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'amount': self.amount,
            'pct_chg': self.pct_chg,
            'ma5': self.ma5,
            'ma10': self.ma10,
            'ma20': self.ma20,
            'volume_ratio': self.volume_ratio,
            'data_source': self.data_source,
        }


class NewsIntel(Base):
    """
    新闻情报数据模型

    存储搜索到的新闻情报条目，用于后续分析与查询
    """
    __tablename__ = 'news_intel'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 关联用户查询操作
    query_id = Column(String(64), index=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))

    # 搜索上下文
    dimension = Column(String(32), index=True)  # latest_news / risk_check / earnings / market_analysis / industry
    query = Column(String(255))
    provider = Column(String(32), index=True)

    # 新闻内容
    title = Column(String(300), nullable=False)
    snippet = Column(Text)
    url = Column(String(1000), nullable=False)
    source = Column(String(100))
    published_date = Column(DateTime, index=True)

    # 入库时间
    fetched_at = Column(DateTime, default=datetime.now, index=True)
    query_source = Column(String(32), index=True)  # bot/web/cli/system
    requester_platform = Column(String(20))
    requester_user_id = Column(String(64))
    requester_user_name = Column(String(64))
    requester_chat_id = Column(String(64))
    requester_message_id = Column(String(64))
    requester_query = Column(String(255))

    __table_args__ = (
        UniqueConstraint('url', name='uix_news_url'),
        Index('ix_news_code_pub', 'code', 'published_date'),
    )

    def __repr__(self) -> str:
        return f"<NewsIntel(code={self.code}, title={self.title[:20]}...)>"


class AnalysisHistory(Base):
    """
    分析结果历史记录模型

    保存每次分析结果，支持按 query_id/股票代码检索
    """
    __tablename__ = 'analysis_history'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 关联查询链路
    query_id = Column(String(64), index=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))
    report_type = Column(String(16), index=True)

    # 核心结论
    sentiment_score = Column(Integer)
    operation_advice = Column(String(20))
    trend_prediction = Column(String(50))
    analysis_summary = Column(Text)

    # Per-dimension scores for backtesting effectiveness analysis
    trend_score = Column(Integer, default=0)           # 0-30
    bias_score = Column(Integer, default=0)            # 0-15
    volume_score = Column(Integer, default=0)          # 0-18
    support_score = Column(Integer, default=0)         # 0-6
    macd_score = Column(Integer, default=0)            # 0-13
    rsi_score = Column(Integer, default=0)             # 0-5
    capital_flow_score = Column(Integer, default=0)    # 0-13 (weighted)

    # System-computed quantitative signals (not LLM output)
    signal_score = Column(Integer, default=0)          # 0-100 total technical score
    buy_signal = Column(String(24))                    # STRONG_BUY/BUY/HOLD/AVOID/STRONG_AVOID
    pe_ratio = Column(Float)
    market_environment = Column(String(24))            # bull/bear/sideways/strong_bear

    # 详细数据
    raw_result = Column(Text)
    news_content = Column(Text)
    context_snapshot = Column(Text)

    # 狙击点位（用于回测）
    ideal_buy = Column(Float)
    secondary_buy = Column(Float)
    stop_loss = Column(Float)
    take_profit = Column(Float)
    # Trade-levels engine extras (computed by trade_levels.py)
    position_pct = Column(Float)
    risk_reward = Column(Float)
    take_profit_2_rule = Column(Text)
    # Picker strategy that produced this analysis (used for backtest attribution).
    strategy_id = Column(String(32))

    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_analysis_code_time', 'code', 'created_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'query_id': self.query_id,
            'code': self.code,
            'name': self.name,
            'report_type': self.report_type,
            'sentiment_score': self.sentiment_score,
            'operation_advice': self.operation_advice,
            'trend_prediction': self.trend_prediction,
            'analysis_summary': self.analysis_summary,
            'trend_score': self.trend_score,
            'bias_score': self.bias_score,
            'volume_score': self.volume_score,
            'support_score': self.support_score,
            'macd_score': self.macd_score,
            'rsi_score': self.rsi_score,
            'capital_flow_score': self.capital_flow_score,
            'signal_score': self.signal_score,
            'buy_signal': self.buy_signal,
            'pe_ratio': self.pe_ratio,
            'market_environment': self.market_environment,
            'position_pct': self.position_pct,
            'risk_reward': self.risk_reward,
            'take_profit_2_rule': self.take_profit_2_rule,
            'strategy_id': self.strategy_id,
            'raw_result': self.raw_result,
            'news_content': self.news_content,
            'context_snapshot': self.context_snapshot,
            'ideal_buy': self.ideal_buy,
            'secondary_buy': self.secondary_buy,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class PickerHistory(Base):
    """AI stock picker run history — stores full result JSON per run."""

    __tablename__ = "picker_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_summary = Column(Text, default="")
    picks_json = Column(Text, default="[]")
    sectors_json = Column(Text, default="[]")
    risk_warning = Column(Text, default="")
    screen_stats_json = Column(Text)
    screened_pool_json = Column(Text)
    screened_pool_by_strategy_json = Column(Text, default=None)
    pick_count = Column(Integer, default=0)
    elapsed_seconds = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.now, index=True)
    picker_mode = Column(String(20), default=None)  # deprecated, use picker_strategies
    picker_strategies_json = Column(Text, default=None)  # JSON array: ["buy_pullback"]
    picker_leader_bias_exempt_pct = Column(Float, default=None)

    def to_summary_dict(self) -> Dict[str, Any]:
        picks = json.loads(self.picks_json) if self.picks_json else []
        sectors = json.loads(self.sectors_json) if self.sectors_json else []
        return {
            "id": self.id,
            "market_summary": self.market_summary or "",
            "pick_count": self.pick_count or len(picks),
            "picks_preview": [{"code": p.get("code", ""), "name": p.get("name", ""), "attention": p.get("attention", "")} for p in picks[:5]],
            "sectors_to_watch": sectors,
            "elapsed_seconds": self.elapsed_seconds or 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "picker_mode": self.picker_mode or "balanced",
            "picker_strategies": (
                json.loads(self.picker_strategies_json)
                if self.picker_strategies_json
                else ["buy_pullback"]
            ),
            "picker_leader_bias_exempt_pct": self.picker_leader_bias_exempt_pct,
        }

    def to_full_dict(self) -> Dict[str, Any]:
        strategies = json.loads(self.picker_strategies_json) if self.picker_strategies_json else None
        return {
            "id": self.id,
            "success": True,
            "market_summary": self.market_summary or "",
            "picks": json.loads(self.picks_json) if self.picks_json else [],
            "sectors_to_watch": json.loads(self.sectors_json) if self.sectors_json else [],
            "risk_warning": self.risk_warning or "",
            "screen_stats": json.loads(self.screen_stats_json) if self.screen_stats_json else None,
            "screened_pool": json.loads(self.screened_pool_json) if self.screened_pool_json else [],
            "screened_pool_by_strategy": (
                json.loads(self.screened_pool_by_strategy_json)
                if self.screened_pool_by_strategy_json else {}
            ),
            "generated_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            "elapsed_seconds": self.elapsed_seconds or 0,
            "error": "",
            "picker_mode": self.picker_mode or "balanced",
            "picker_strategies": strategies if strategies else ["buy_pullback"],
            "picker_leader_bias_exempt_pct": self.picker_leader_bias_exempt_pct,
        }


class PickerBacktestHistory(Base):
    """Picker backtest run history — stores full result JSON per run."""

    __tablename__ = "picker_backtest_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    start_date = Column(String(10), nullable=False)
    end_date = Column(String(10), nullable=False)
    hold_days = Column(Integer, nullable=False, default=10)
    top_n = Column(Integer, nullable=False, default=5)
    picker_mode = Column(String(20), default="balanced")  # deprecated, use picker_strategies_json
    picker_strategies_json = Column(Text, default=None)  # JSON array: ["buy_pullback"]
    picker_leader_bias_exempt_pct = Column(Float, default=None)
    trade_dates_count = Column(Integer, default=0)
    results_json = Column(Text, default="[]")
    summary_json = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)

    def to_summary_dict(self) -> Dict[str, Any]:
        summary = json.loads(self.summary_json) if self.summary_json else {}
        strategies = (
            json.loads(self.picker_strategies_json)
            if self.picker_strategies_json
            else None
        )
        return {
            "id": self.id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "hold_days": self.hold_days,
            "top_n": self.top_n,
            "picker_strategies": strategies,
            "trade_dates_count": self.trade_dates_count or 0,
            "win_rate_pct": summary.get("win_rate_pct"),
            "avg_return_pct": summary.get("avg_return_pct"),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_full_dict(self) -> Dict[str, Any]:
        strategies = (
            json.loads(self.picker_strategies_json)
            if self.picker_strategies_json
            else None
        )
        return {
            "id": self.id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "hold_days": self.hold_days,
            "top_n": self.top_n,
            "picker_strategies": strategies,
            "trade_dates_count": self.trade_dates_count or 0,
            "results": json.loads(self.results_json) if self.results_json else [],
            "summary": json.loads(self.summary_json) if self.summary_json else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BacktestResult(Base):
    """单条分析记录的回测结果。"""

    __tablename__ = 'backtest_results'

    id = Column(Integer, primary_key=True, autoincrement=True)

    analysis_history_id = Column(
        Integer,
        ForeignKey('analysis_history.id'),
        nullable=False,
        index=True,
    )

    # 冗余字段，便于按股票筛选
    code = Column(String(10), nullable=False, index=True)
    analysis_date = Column(Date, index=True)

    # 回测参数
    eval_window_days = Column(Integer, nullable=False, default=10)

    # 状态
    eval_status = Column(String(16), nullable=False, default='pending')
    evaluated_at = Column(DateTime, default=datetime.now, index=True)

    # 建议快照（避免未来分析字段变化导致回测不可解释）
    operation_advice = Column(String(20))
    position_recommendation = Column(String(8))  # long/cash

    # v2: System-computed signal snapshot (single source of truth, NOT LLM text)
    signal_score_at_eval = Column(Integer)
    buy_signal_at_eval = Column(String(24))           # STRONG_BUY/BUY/HOLD/AVOID/STRONG_AVOID
    market_environment_at_eval = Column(String(24))   # bull/bear/sideways/strong_bear
    strategy_id = Column(String(32))                  # buy_pullback (historical rows may hold retired IDs)
    risk_reward_at_eval = Column(Float)
    position_pct_at_eval = Column(Float)

    # v2: Per-dimension score snapshots (avoids JOIN on AnalysisHistory for analytics)
    trend_score_at_eval = Column(Integer)
    bias_score_at_eval = Column(Integer)
    volume_score_at_eval = Column(Integer)
    support_score_at_eval = Column(Integer)
    macd_score_at_eval = Column(Integer)
    rsi_score_at_eval = Column(Integer)
    capital_flow_score_at_eval = Column(Integer)

    # v2: Trade-levels engine simulation diagnostics
    exit_reason = Column(String(32))                  # stop_loss/trailing_ma10/stage_break_+12pct/...
    hold_days = Column(Integer)

    # 价格与收益
    start_price = Column(Float)
    end_close = Column(Float)
    max_high = Column(Float)
    min_low = Column(Float)
    stock_return_pct = Column(Float)

    # 方向与结果
    direction_expected = Column(String(16))  # up/down/flat/not_down
    direction_correct = Column(Boolean, nullable=True)
    outcome = Column(String(16))  # win/loss/neutral

    # 目标价命中（仅 long 且配置了止盈/止损时有意义）
    stop_loss = Column(Float)
    take_profit = Column(Float)
    hit_stop_loss = Column(Boolean)
    hit_take_profit = Column(Boolean)
    first_hit = Column(String(16))  # take_profit/stop_loss/ambiguous/neither/not_applicable
    first_hit_date = Column(Date)
    first_hit_trading_days = Column(Integer)

    # 模拟执行（long-only）
    simulated_entry_price = Column(Float)
    simulated_exit_price = Column(Float)
    simulated_exit_reason = Column(String(32))  # stop_loss/take_profit/time_exit/not_filled/...
    simulated_return_pct = Column(Float)

    # v3: AI-plan execution metrics
    entry_status = Column(String(24))   # filled/not_filled/not_filled_limit_up
    r_multiple = Column(Float)
    mae_pct = Column(Float)
    mfe_pct = Column(Float)

    __table_args__ = (
        UniqueConstraint(
            'analysis_history_id',
            'eval_window_days',
            name='uix_backtest_analysis_window',
        ),
        Index('ix_backtest_code_date', 'code', 'analysis_date'),
    )


class BacktestSummary(Base):
    """回测汇总指标（按股票或全局）。"""

    __tablename__ = 'backtest_summaries'

    id = Column(Integer, primary_key=True, autoincrement=True)

    scope = Column(String(16), nullable=False, index=True)  # overall/stock
    code = Column(String(16), index=True)

    eval_window_days = Column(Integer, nullable=False, default=10)
    computed_at = Column(DateTime, default=datetime.now, index=True)

    # 计数
    total_evaluations = Column(Integer, default=0)
    completed_count = Column(Integer, default=0)
    insufficient_count = Column(Integer, default=0)
    long_count = Column(Integer, default=0)
    cash_count = Column(Integer, default=0)

    win_count = Column(Integer, default=0)
    loss_count = Column(Integer, default=0)
    neutral_count = Column(Integer, default=0)

    # 准确率/胜率
    direction_accuracy_pct = Column(Float)
    win_rate_pct = Column(Float)
    neutral_rate_pct = Column(Float)

    # 收益
    avg_stock_return_pct = Column(Float)
    avg_simulated_return_pct = Column(Float)

    # 目标价触发统计（仅 long 且配置止盈/止损时统计）
    stop_loss_trigger_rate = Column(Float)
    take_profit_trigger_rate = Column(Float)
    ambiguous_rate = Column(Float)
    avg_days_to_first_hit = Column(Float)

    # v3: AI-plan execution aggregates
    fill_rate_pct = Column(Float)
    filled_count = Column(Integer, default=0)
    not_filled_count = Column(Integer, default=0)
    not_filled_limit_up_count = Column(Integer, default=0)
    trade_win_rate_pct = Column(Float)
    expectancy_pct = Column(Float)
    avg_r_multiple = Column(Float)
    profit_factor = Column(Float)
    max_drawdown_pct = Column(Float)
    avg_mae_pct = Column(Float)
    avg_mfe_pct = Column(Float)
    ambiguous_count = Column(Integer, default=0)

    # 诊断字段（JSON 字符串）
    diagnostics_json = Column(Text)
    # Per-bucket breakdowns (current engine).
    signal_breakdown_json = Column(Text)         # by buy_signal (STRONG_BUY/BUY/HOLD/AVOID/STRONG_AVOID)
    score_bucket_breakdown_json = Column(Text)   # by signal_score bucket (ge_80/70_80/60_70/lt_60)
    risk_reward_breakdown_json = Column(Text)    # by plan risk_reward bucket (ge_4 / 2_5_4 / 1_5_2_5 / lt_1_5)
    exit_reason_breakdown_json = Column(Text)    # by simulated exit_reason
    regime_breakdown_json = Column(Text)         # by market_environment
    strategy_breakdown_json = Column(Text)       # by strategy_id

    __table_args__ = (
        UniqueConstraint(
            'scope',
            'code',
            'eval_window_days',
            name='uix_backtest_summary_scope_code_window',
        ),
    )


class ConversationMessage(Base):
    """
    Agent 对话历史记录表
    """
    __tablename__ = 'conversation_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), index=True, nullable=False)
    role = Column(String(20), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now, index=True)


class LLMUsage(Base):
    """One row per litellm.completion() call — token-usage audit log."""

    __tablename__ = 'llm_usage'

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 'analysis' | 'agent' | 'market_review'
    call_type = Column(String(32), nullable=False, index=True)
    model = Column(String(128), nullable=False)
    stock_code = Column(String(16), nullable=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    called_at = Column(DateTime, default=datetime.now, index=True)


