# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 核心分析流水线
===================================

职责：
1. 管理整个分析流程
2. 协调数据获取、存储、搜索、分析、通知等模块
3. 实现并发控制和异常处理
4. 提供股票分析的核心功能
"""

import logging
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock
from typing import List, Dict, Any, Optional, Tuple

import pandas as pd

from src.config import get_config, Config, get_effective_push_report_type
from src.storage import get_db
from data_provider import DataFetcherManager
from data_provider.realtime_types import ChipDistribution
from src.analyzer import GeminiAnalyzer, AnalysisResult, fill_chip_structure_if_needed
from src.data.stock_mapping import STOCK_NAME_MAP
from src.notification_service import NotificationService, NotificationChannel
from src.search_service import SearchService
from src.enums import ReportType
from src.stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult, TrendStatus, BuySignal
from src.core.trading_calendar import (
    get_calendar_today_for_market,
    get_market_for_stock,
    is_market_open,
    should_bypass_daily_fetch_cache_cn,
)
from bot.models import BotMessage

from ._analysis_mixin import _AnalysisMixin
from ._market_env_mixin import _MarketEnvMixin
from ._notify_mixin import _NotifyMixin
from ._run_mixin import _RunMixin

__all__ = ["StockAnalysisPipeline"]


logger = logging.getLogger(__name__)


class StockAnalysisPipeline(_AnalysisMixin, _MarketEnvMixin, _RunMixin, _NotifyMixin):
    """
    股票分析主流程调度器
    
    职责：
    1. 管理整个分析流程
    2. 协调数据获取、存储、搜索、分析、通知等模块
    3. 实现并发控制和异常处理
    """
    
    def __init__(
        self,
        config: Optional[Config] = None,
        max_workers: Optional[int] = None,
        source_message: Optional[BotMessage] = None,
        query_id: Optional[str] = None,
        query_source: Optional[str] = None,
        save_context_snapshot: Optional[bool] = None
    ):
        """
        初始化调度器
        
        Args:
            config: 配置对象（可选，默认使用全局配置）
            max_workers: 最大并发线程数（可选，默认从配置读取）
        """
        self.config = config or get_config()
        self.max_workers = max_workers or self.config.max_workers
        self.source_message = source_message
        self.query_id = query_id
        self.query_source = self._resolve_query_source(query_source)
        self.save_context_snapshot = (
            self.config.save_context_snapshot if save_context_snapshot is None else save_context_snapshot
        )
        
        # 初始化各模块
        self.db = get_db()
        self.fetcher_manager = DataFetcherManager()
        # 不再单独创建 akshare_fetcher，统一使用 fetcher_manager 获取增强数据
        self.trend_analyzer = StockTrendAnalyzer()  # 趋势分析器
        self.analyzer = GeminiAnalyzer()
        self.notifier = NotificationService(source_message=source_message)
        
        # 初始化搜索服务
        self.search_service = SearchService(
            bocha_keys=self.config.bocha_api_keys,
            tavily_keys=self.config.tavily_api_keys,
            brave_keys=self.config.brave_api_keys,
            serpapi_keys=self.config.serpapi_keys,
            minimax_keys=self.config.minimax_api_keys,
            searxng_base_urls=self.config.searxng_base_urls,
            zhipu_keys=self.config.zhipu_api_keys,
            news_max_age_days=self.config.news_max_age_days,
        )
        
        logger.info(f"调度器初始化完成，最大并发数: {self.max_workers}")
        logger.info("已启用趋势分析器 (MA5>MA10>MA20 多头判断)")
        # 打印实时行情/筹码配置状态
        logger.info(f"实时行情已启用 (优先级: {self.config.realtime_source_priority})")
        if self.config.enable_chip_distribution:
            logger.info("筹码分布分析已启用")
        else:
            logger.info("筹码分布分析已禁用")
        if self.search_service.is_available:
            logger.info("搜索服务已启用 (Tavily/SerpAPI)")
        else:
            logger.warning("搜索服务未启用（未配置 API Key）")
    
    def fetch_and_save_stock_data(
        self, 
        code: str,
        force_refresh: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """
        获取并保存单只股票数据
        
        断点续传逻辑：
        1. 检查数据库是否已有今日数据
        2. 如果有且不强制刷新，则跳过网络请求
        3. 否则从数据源获取并保存
        
        Args:
            code: 股票代码
            force_refresh: 是否强制刷新（忽略本地缓存）
            
        Returns:
            Tuple[是否成功, 错误信息]
        """
        try:
            # 首先获取股票名称
            stock_name = self.fetcher_manager.get_stock_name(code)

            from zoneinfo import ZoneInfo

            mkt = get_market_for_stock(code)
            today = get_calendar_today_for_market(mkt)
            # today: listing-market calendar date (not host date.today()) to avoid UTC skew.
            # CN: do not skip fetch intraday if today's row was saved earlier (e.g. 09:35 snapshot at 14:30).

            if not force_refresh and self.db.has_today_data(code, today):
                bypass = False
                if mkt == "cn":
                    row_ua = self.db.get_daily_bar_updated_at(code, today)
                    now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
                    bypass = should_bypass_daily_fetch_cache_cn(today, row_ua, now_cn)
                if bypass:
                    logger.info(
                        f"{stock_name}({code}) 已有 {today} 日线，但盘中或收盘未定型，重新拉取"
                    )
                else:
                    logger.info(f"{stock_name}({code}) 今日数据已存在，跳过获取（断点续传）")
                    return True, None

            # 从数据源获取数据
            logger.info(f"{stock_name}({code}) 开始从数据源获取数据...")
            df, source_name = self.fetcher_manager.get_daily_data(code, days=30)

            if df is None or df.empty:
                return False, "获取数据为空"

            # 保存到数据库
            saved_count = self.db.save_daily_data(df, code, source_name)
            logger.info(f"{stock_name}({code}) 数据保存成功（来源: {source_name}，新增 {saved_count} 条）")

            return True, None

        except Exception as e:
            error_msg = f"获取/保存数据失败: {str(e)}"
            logger.error(f"{stock_name}({code}) {error_msg}")
            return False, error_msg
    
