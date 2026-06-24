# -*- coding: utf-8 -*-
"""
``SearchService`` - top-level orchestrator that picks the best provider per
query, layers a small in-process cache and exposes the high-level helpers
used by the analyzer / market-review / picker pipelines.

Module-level ``get_search_service`` / ``reset_search_service`` provide the
shared singleton accessor used by the rest of the codebase.
"""
from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from data_provider.us_index_mapping import is_us_index_code

from .base_provider import BaseSearchProvider
from .bocha import BochaSearchProvider
from .brave import BraveSearchProvider
from .minimax import MiniMaxSearchProvider
from .models import SearchResponse, SearchResult
from .searxng import SearXNGSearchProvider
from .serpapi import SerpAPISearchProvider
from .tavily import TavilySearchProvider
from .zhipu import ZhipuSearchProvider

logger = logging.getLogger(__name__)


class SearchService:
    """
    搜索服务
    
    功能：
    1. 管理多个搜索引擎
    2. 自动故障转移
    3. 结果聚合和格式化
    4. 数据源失败时的增强搜索（股价、走势等）
    5. 港股/美股自动使用英文搜索关键词
    """
    
    # 增强搜索关键词模板（A股 中文）
    ENHANCED_SEARCH_KEYWORDS = [
        "{name} 股票 今日 股价",
        "{name} {code} 最新 行情 走势",
        "{name} 股票 分析 走势图",
        "{name} K线 技术分析",
        "{name} {code} 涨跌 成交量",
    ]

    # 增强搜索关键词模板（港股/美股 英文）
    ENHANCED_SEARCH_KEYWORDS_EN = [
        "{name} stock price today",
        "{name} {code} latest quote trend",
        "{name} stock analysis chart",
        "{name} technical analysis",
        "{name} {code} performance volume",
    ]
    
    def __init__(
        self,
        bocha_keys: Optional[List[str]] = None,
        tavily_keys: Optional[List[str]] = None,
        brave_keys: Optional[List[str]] = None,
        serpapi_keys: Optional[List[str]] = None,
        minimax_keys: Optional[List[str]] = None,
        searxng_base_urls: Optional[List[str]] = None,
        zhipu_keys: Optional[List[str]] = None,
        news_max_age_days: int = 3,
    ):
        """
        初始化搜索服务

        Args:
            bocha_keys: 博查搜索 API Key 列表
            tavily_keys: Tavily API Key 列表
            brave_keys: Brave Search API Key 列表
            serpapi_keys: SerpAPI Key 列表
            minimax_keys: MiniMax API Key 列表
            searxng_base_urls: SearXNG 实例地址列表（自建无配额兜底）
            zhipu_keys: 智谱 BigModel API Key 列表（Web Search API，与 GLM 共用 key）
            news_max_age_days: 新闻最大时效（天）
        """
        self._providers: List[BaseSearchProvider] = []
        self.news_max_age_days = max(1, news_max_age_days)

        # 初始化搜索引擎（按优先级排序）
        # 1. Bocha 优先（中文搜索优化，AI摘要）
        if bocha_keys:
            self._providers.append(BochaSearchProvider(bocha_keys))
            logger.info(f"已配置 Bocha 搜索，共 {len(bocha_keys)} 个 API Key")

        # 2. Tavily（免费额度更多，每月 1000 次）
        if tavily_keys:
            self._providers.append(TavilySearchProvider(tavily_keys))
            logger.info(f"已配置 Tavily 搜索，共 {len(tavily_keys)} 个 API Key")

        # 3. Brave Search（隐私优先，全球覆盖）
        if brave_keys:
            self._providers.append(BraveSearchProvider(brave_keys))
            logger.info(f"已配置 Brave 搜索，共 {len(brave_keys)} 个 API Key")

        # 4. SerpAPI 作为备选（每月 100 次）
        if serpapi_keys:
            self._providers.append(SerpAPISearchProvider(serpapi_keys))
            logger.info(f"已配置 SerpAPI 搜索，共 {len(serpapi_keys)} 个 API Key")

        # 5. Zhipu BigModel Web Search（结构化 REST API，与 GLM 共用 key）
        if zhipu_keys:
            self._providers.append(ZhipuSearchProvider(zhipu_keys))
            logger.info(f"已配置 Zhipu 搜索，共 {len(zhipu_keys)} 个 API Key")

        # 6. MiniMax（Coding Plan Web Search，结构化结果）
        if minimax_keys:
            self._providers.append(MiniMaxSearchProvider(minimax_keys))
            logger.info(f"已配置 MiniMax 搜索，共 {len(minimax_keys)} 个 API Key")

        # 7. SearXNG（自建实例，无配额兜底，最后兜底）
        if searxng_base_urls:
            self._providers.append(SearXNGSearchProvider(searxng_base_urls))
            logger.info(f"已配置 SearXNG 搜索，共 {len(searxng_base_urls)} 个实例")
        
        if not self._providers:
            logger.warning("未配置任何搜索引擎 API Key，新闻搜索功能将不可用")

        # In-memory search result cache: {cache_key: (timestamp, SearchResponse)}
        self._cache: Dict[str, Tuple[float, 'SearchResponse']] = {}
        # Default cache TTL in seconds (10 minutes)
        self._cache_ttl: int = 600
    
    @staticmethod
    def _is_foreign_stock(stock_code: str) -> bool:
        """判断是否为港股或美股"""
        import re
        code = stock_code.strip()
        # 美股：1-5个大写字母，可能包含点（如 BRK.B）
        if re.match(r'^[A-Za-z]{1,5}(\.[A-Za-z])?$', code):
            return True
        # 港股：带 hk 前缀或 5位纯数字
        lower = code.lower()
        if lower.startswith('hk'):
            return True
        if code.isdigit() and len(code) == 5:
            return True
        return False

    # A-share ETF code prefixes (Shanghai 51/52/56/58, Shenzhen 15/16/18)
    _A_ETF_PREFIXES = ('51', '52', '56', '58', '15', '16', '18')
    _ETF_NAME_KEYWORDS = ('ETF', 'FUND', 'TRUST', 'INDEX', 'TRACKER', 'UNIT')  # US/HK ETF name hints

    @staticmethod
    def is_index_or_etf(stock_code: str, stock_name: str) -> bool:
        """
        Judge if symbol is index-tracking ETF or market index.
        For such symbols, analysis focuses on index movement only, not issuer company risks.
        """
        code = (stock_code or '').strip().split('.')[0]
        if not code:
            return False
        # A-share ETF
        if code.isdigit() and len(code) == 6 and code.startswith(SearchService._A_ETF_PREFIXES):
            return True
        # US index (SPX, DJI, IXIC etc.)
        if is_us_index_code(code):
            return True
        # US/HK ETF: foreign symbol + name contains fund-like keywords
        if SearchService._is_foreign_stock(code):
            name_upper = (stock_name or '').upper()
            return any(kw in name_upper for kw in SearchService._ETF_NAME_KEYWORDS)
        return False

    @property
    def is_available(self) -> bool:
        """检查是否有可用的搜索引擎"""
        return any(p.is_available for p in self._providers)

    def _cache_key(self, query: str, max_results: int, days: int) -> str:
        """Build a cache key from query parameters."""
        return f"{query}|{max_results}|{days}"

    def _get_cached(self, key: str) -> Optional['SearchResponse']:
        """Return cached SearchResponse if still valid, else None."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, response = entry
        if time.time() - ts > self._cache_ttl:
            del self._cache[key]
            return None
        logger.debug(f"Search cache hit: {key[:60]}...")
        return response

    def _put_cache(self, key: str, response: 'SearchResponse') -> None:
        """Store a successful SearchResponse in cache."""
        # Hard cap: evict oldest entries when cache exceeds limit
        _MAX_CACHE_SIZE = 500
        if len(self._cache) >= _MAX_CACHE_SIZE:
            now = time.time()
            # First pass: remove expired entries
            expired = [k for k, (ts, _) in self._cache.items() if now - ts > self._cache_ttl]
            for k in expired:
                del self._cache[k]
            # Second pass: if still over limit, evict oldest entries (FIFO)
            if len(self._cache) >= _MAX_CACHE_SIZE:
                excess = len(self._cache) - _MAX_CACHE_SIZE + 1
                oldest = sorted(self._cache.keys(), key=lambda k: self._cache[k][0])[:excess]
                for k in oldest:
                    del self._cache[k]
        self._cache[key] = (time.time(), response)
    
    def search_stock_news(
        self,
        stock_code: str,
        stock_name: str,
        max_results: int = 5,
        focus_keywords: Optional[List[str]] = None
    ) -> SearchResponse:
        """
        搜索股票相关新闻
        
        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            max_results: 最大返回结果数
            focus_keywords: 重点关注的关键词列表
            
        Returns:
            SearchResponse 对象
        """
        # 智能确定搜索时间范围
        # 策略：
        # 1. 周二至周五：搜索近1天（24小时）
        # 2. 周六、周日：搜索近2-3天（覆盖周末）
        # 3. 周一：搜索近3天（覆盖周末）
        # 4. 用 NEWS_MAX_AGE_DAYS 限制上限
        today_weekday = datetime.now().weekday()
        if today_weekday == 0:  # 周一
            weekday_days = 3
        elif today_weekday >= 5:  # 周六(5)、周日(6)
            weekday_days = 2
        else:  # 周二(1) - 周五(4)
            weekday_days = 1
        search_days = min(weekday_days, self.news_max_age_days)

        # 构建搜索查询（优化搜索效果）
        is_foreign = self._is_foreign_stock(stock_code)
        if focus_keywords:
            # 如果提供了关键词，直接使用关键词作为查询
            query = " ".join(focus_keywords)
        elif is_foreign:
            # 港股/美股使用英文搜索关键词
            query = f"{stock_name} {stock_code} stock latest news"
        else:
            # 默认主查询：股票名称 + 核心关键词
            query = f"{stock_name} {stock_code} 股票 最新消息"

        logger.info(f"搜索股票新闻: {stock_name}({stock_code}), query='{query}', 时间范围: 近{search_days}天")

        # Check cache first
        cache_key = self._cache_key(query, max_results, search_days)
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.info(f"使用缓存搜索结果: {stock_name}({stock_code})")
            return cached

        # 依次尝试各个搜索引擎
        for provider in self._providers:
            if not provider.is_available:
                continue
            
            response = provider.search(query, max_results, days=search_days)
            
            if response.success and response.results:
                logger.info(f"使用 {provider.name} 搜索成功")
                self._put_cache(cache_key, response)
                return response
            else:
                logger.warning(f"{provider.name} 搜索失败: {response.error_message}，尝试下一个引擎")
        
        # 所有引擎都失败
        return SearchResponse(
            query=query,
            results=[],
            provider="None",
            success=False,
            error_message="所有搜索引擎都不可用或搜索失败"
        )
    
    def search_stock_events(
        self,
        stock_code: str,
        stock_name: str,
        event_types: Optional[List[str]] = None
    ) -> SearchResponse:
        """
        搜索股票特定事件（年报预告、减持等）
        
        专门针对交易决策相关的重要事件进行搜索
        
        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            event_types: 事件类型列表
            
        Returns:
            SearchResponse 对象
        """
        if event_types is None:
            if self._is_foreign_stock(stock_code):
                event_types = ["earnings report", "insider selling", "quarterly results"]
            else:
                event_types = ["年报预告", "减持公告", "业绩快报"]
        
        # 构建针对性查询
        event_query = " OR ".join(event_types)
        query = f"{stock_name} ({event_query})"
        
        logger.info(f"搜索股票事件: {stock_name}({stock_code}) - {event_types}")
        
        # 依次尝试各个搜索引擎
        for provider in self._providers:
            if not provider.is_available:
                continue
            
            response = provider.search(query, max_results=5)
            
            if response.success:
                return response
        
        return SearchResponse(
            query=query,
            results=[],
            provider="None",
            success=False,
            error_message="事件搜索失败"
        )
    
    def search_comprehensive_intel(
        self,
        stock_code: str,
        stock_name: str,
        max_searches: int = 3
    ) -> Dict[str, SearchResponse]:
        """
        多维度情报搜索（同时使用多个引擎、多个维度）
        
        搜索维度：
        1. 最新消息 - 近期新闻动态
        2. 风险排查 - 减持、处罚、利空
        3. 业绩预期 - 年报预告、业绩快报
        
        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            max_searches: 最大搜索次数
            
        Returns:
            {维度名称: SearchResponse} 字典
        """
        results = {}
        search_count = 0

        is_foreign = self._is_foreign_stock(stock_code)
        is_index_etf = self.is_index_or_etf(stock_code, stock_name)

        if is_foreign:
            search_dimensions = [
                {'name': 'latest_news', 'query': f"{stock_name} {stock_code} latest news events", 'desc': '最新消息'},
                {'name': 'market_analysis', 'query': f"{stock_name} analyst rating target price report", 'desc': '机构分析'},
                {'name': 'risk_check', 'query': (
                    f"{stock_name} {stock_code} index performance outlook tracking error"
                    if is_index_etf else f"{stock_name} risk insider selling lawsuit litigation"
                ), 'desc': '风险排查'},
                {'name': 'earnings', 'query': (
                    f"{stock_name} {stock_code} index performance composition outlook"
                    if is_index_etf else f"{stock_name} earnings revenue profit growth forecast"
                ), 'desc': '业绩预期'},
                {'name': 'industry', 'query': (
                    f"{stock_name} {stock_code} index sector allocation holdings"
                    if is_index_etf else f"{stock_name} industry competitors market share outlook"
                ), 'desc': '行业分析'},
            ]
        else:
            search_dimensions = [
                {'name': 'latest_news', 'query': f"{stock_name} {stock_code} 最新 新闻 重大 事件", 'desc': '最新消息'},
                {'name': 'market_analysis', 'query': f"{stock_name} 研报 目标价 评级 深度分析", 'desc': '机构分析'},
                {'name': 'risk_check', 'query': (
                    f"{stock_name} 指数走势 跟踪误差 净值 表现"
                    if is_index_etf else f"{stock_name} 减持 处罚 违规 诉讼 利空 风险"
                ), 'desc': '风险排查'},
                {'name': 'earnings', 'query': (
                    f"{stock_name} 指数成分 净值 跟踪表现"
                    if is_index_etf else f"{stock_name} 业绩预告 财报 营收 净利润 同比增长"
                ), 'desc': '业绩预期'},
                {'name': 'industry', 'query': (
                    f"{stock_name} 指数成分股 行业配置 权重"
                    if is_index_etf else f"{stock_name} 所在行业 竞争对手 市场份额 行业前景"
                ), 'desc': '行业分析'},
            ]
        
        logger.info(f"开始多维度情报搜索: {stock_name}({stock_code})")
        
        # 轮流使用不同的搜索引擎
        provider_index = 0
        
        for dim in search_dimensions:
            if search_count >= max_searches:
                break
            
            # 选择搜索引擎（轮流使用）
            available_providers = [p for p in self._providers if p.is_available]
            if not available_providers:
                break
            
            provider = available_providers[provider_index % len(available_providers)]
            provider_index += 1
            
            logger.info(f"[情报搜索] {dim['desc']}: 使用 {provider.name}")
            
            response = provider.search(dim['query'], max_results=3, days=self.news_max_age_days)
            results[dim['name']] = response
            search_count += 1
            
            if response.success:
                logger.info(f"[情报搜索] {dim['desc']}: 获取 {len(response.results)} 条结果")
            else:
                logger.warning(f"[情报搜索] {dim['desc']}: 搜索失败 - {response.error_message}")
            
            # 短暂延迟避免请求过快
            time.sleep(0.5)
        
        return results
    
    def format_intel_report(self, intel_results: Dict[str, SearchResponse], stock_name: str) -> str:
        """
        格式化情报搜索结果为报告
        
        Args:
            intel_results: 多维度搜索结果
            stock_name: 股票名称
            
        Returns:
            格式化的情报报告文本
        """
        lines = [f"【{stock_name} 情报搜索结果】"]
        
        # 维度展示顺序
        display_order = ['latest_news', 'market_analysis', 'risk_check', 'earnings', 'industry']
        
        for dim_name in display_order:
            if dim_name not in intel_results:
                continue
                
            resp = intel_results[dim_name]
            
            # 获取维度描述
            dim_desc = dim_name
            if dim_name == 'latest_news': dim_desc = '📰 最新消息'
            elif dim_name == 'market_analysis': dim_desc = '📈 机构分析'
            elif dim_name == 'risk_check': dim_desc = '⚠️ 风险排查'
            elif dim_name == 'earnings': dim_desc = '📊 业绩预期'
            elif dim_name == 'industry': dim_desc = '🏭 行业分析'
            
            lines.append(f"\n{dim_desc} (来源: {resp.provider}):")
            if resp.success and resp.results:
                # 增加显示条数
                for i, r in enumerate(resp.results[:4], 1):
                    date_str = f" [{r.published_date}]" if r.published_date else ""
                    lines.append(f"  {i}. {r.title}{date_str}")
                    # 如果摘要太短，可能信息量不足
                    snippet = r.snippet[:150] if len(r.snippet) > 20 else r.snippet
                    lines.append(f"     {snippet}...")
            else:
                lines.append("  未找到相关信息")
        
        return "\n".join(lines)
    
    def batch_search(
        self,
        stocks: List[Dict[str, str]],
        max_results_per_stock: int = 3,
        delay_between: float = 1.0
    ) -> Dict[str, SearchResponse]:
        """
        Batch search news for multiple stocks.
        
        Args:
            stocks: List of stocks
            max_results_per_stock: Max results per stock
            delay_between: Delay between searches (seconds)
            
        Returns:
            Dict of results
        """
        results = {}
        
        for i, stock in enumerate(stocks):
            if i > 0:
                time.sleep(delay_between)
            
            code = stock.get('code', '')
            name = stock.get('name', '')
            
            response = self.search_stock_news(code, name, max_results_per_stock)
            results[code] = response
        
        return results

    def search_stock_price_fallback(
        self,
        stock_code: str,
        stock_name: str,
        max_attempts: int = 3,
        max_results: int = 5
    ) -> SearchResponse:
        """
        Enhance search when data sources fail.
        
        When all data sources (efinance, akshare, tushare, baostock, etc.) fail to get
        stock data, use search engines to find stock trends and price info as supplemental data for AI analysis.
        
        Strategy:
        1. Search using multiple keyword templates
        2. Try all available search engines for each keyword
        3. Aggregate and deduplicate results
        
        Args:
            stock_code: Stock Code
            stock_name: Stock Name
            max_attempts: Max search attempts (using different keywords)
            max_results: Max results to return
            
        Returns:
            SearchResponse object with aggregated results
        """

        if not self.is_available:
            return SearchResponse(
                query=f"{stock_name} 股价走势",
                results=[],
                provider="None",
                success=False,
                error_message="未配置搜索引擎 API Key"
            )
        
        logger.info(f"[增强搜索] 数据源失败，启动增强搜索: {stock_name}({stock_code})")
        
        all_results = []
        seen_urls = set()
        successful_providers = []
        
        # 使用多个关键词模板搜索
        is_foreign = self._is_foreign_stock(stock_code)
        keywords = self.ENHANCED_SEARCH_KEYWORDS_EN if is_foreign else self.ENHANCED_SEARCH_KEYWORDS
        for i, keyword_template in enumerate(keywords[:max_attempts]):
            query = keyword_template.format(name=stock_name, code=stock_code)
            
            logger.info(f"[增强搜索] 第 {i+1}/{max_attempts} 次搜索: {query}")
            
            # 依次尝试各个搜索引擎
            for provider in self._providers:
                if not provider.is_available:
                    continue
                
                try:
                    response = provider.search(query, max_results=3)
                    
                    if response.success and response.results:
                        # 去重并添加结果
                        for result in response.results:
                            if result.url not in seen_urls:
                                seen_urls.add(result.url)
                                all_results.append(result)
                                
                        if provider.name not in successful_providers:
                            successful_providers.append(provider.name)
                        
                        logger.info(f"[增强搜索] {provider.name} 返回 {len(response.results)} 条结果")
                        break  # 成功后跳到下一个关键词
                    else:
                        logger.debug(f"[增强搜索] {provider.name} 无结果或失败")
                        
                except Exception as e:
                    logger.warning(f"[增强搜索] {provider.name} 搜索异常: {e}")
                    continue
            
            # 短暂延迟避免请求过快
            if i < max_attempts - 1:
                time.sleep(0.5)
        
        # 汇总结果
        if all_results:
            # 截取前 max_results 条
            final_results = all_results[:max_results]
            provider_str = ", ".join(successful_providers) if successful_providers else "None"
            
            logger.info(f"[增强搜索] 完成，共获取 {len(final_results)} 条结果（来源: {provider_str}）")
            
            return SearchResponse(
                query=f"{stock_name}({stock_code}) 股价走势",
                results=final_results,
                provider=provider_str,
                success=True,
            )
        else:
            logger.warning(f"[增强搜索] 所有搜索均未返回结果")
            return SearchResponse(
                query=f"{stock_name}({stock_code}) 股价走势",
                results=[],
                provider="None",
                success=False,
                error_message="增强搜索未找到相关信息"
            )

    def search_stock_with_enhanced_fallback(
        self,
        stock_code: str,
        stock_name: str,
        include_news: bool = True,
        include_price: bool = False,
        max_results: int = 5
    ) -> Dict[str, SearchResponse]:
        """
        综合搜索接口（支持新闻和股价信息）
        
        当 include_price=True 时，会同时搜索新闻和股价信息。
        主要用于数据源完全失败时的兜底方案。
        
        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            include_news: 是否搜索新闻
            include_price: 是否搜索股价/走势信息
            max_results: 每类搜索的最大结果数
            
        Returns:
            {'news': SearchResponse, 'price': SearchResponse} 字典
        """
        results = {}
        
        if include_news:
            results['news'] = self.search_stock_news(
                stock_code, 
                stock_name, 
                max_results=max_results
            )
        
        if include_price:
            results['price'] = self.search_stock_price_fallback(
                stock_code,
                stock_name,
                max_attempts=3,
                max_results=max_results
            )
        
        return results

    def format_price_search_context(self, response: SearchResponse) -> str:
        """
        将股价搜索结果格式化为 AI 分析上下文
        
        Args:
            response: 搜索响应对象
            
        Returns:
            格式化的文本，可直接用于 AI 分析
        """
        if not response.success or not response.results:
            return "【股价走势搜索】未找到相关信息，请以其他渠道数据为准。"
        
        lines = [
            f"【股价走势搜索结果】（来源: {response.provider}）",
            "⚠️ 注意：以下信息来自网络搜索，仅供参考，可能存在延迟或不准确。",
            ""
        ]
        
        for i, result in enumerate(response.results, 1):
            date_str = f" [{result.published_date}]" if result.published_date else ""
            lines.append(f"{i}. 【{result.source}】{result.title}{date_str}")
            lines.append(f"   {result.snippet[:200]}...")
            lines.append("")
        
        return "\n".join(lines)


# === 便捷函数 ===
_search_service: Optional[SearchService] = None


def get_search_service() -> SearchService:
    """获取搜索服务单例"""
    global _search_service
    
    if _search_service is None:
        from src.config import get_config
        config = get_config()
        
        _search_service = SearchService(
            bocha_keys=config.bocha_api_keys,
            tavily_keys=config.tavily_api_keys,
            brave_keys=config.brave_api_keys,
            serpapi_keys=config.serpapi_keys,
            minimax_keys=config.minimax_api_keys,
            searxng_base_urls=config.searxng_base_urls,
            zhipu_keys=config.zhipu_api_keys,
            news_max_age_days=config.news_max_age_days,
        )
    
    return _search_service


def reset_search_service() -> None:
    """重置搜索服务（用于测试）"""
    global _search_service
    _search_service = None


if __name__ == "__main__":
    # 测试搜索服务
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
    )
    
    # 手动测试（需要配置 API Key）
    service = get_search_service()
    
    if service.is_available:
        print("=== 测试股票新闻搜索 ===")
        response = service.search_stock_news("300389", "艾比森")
        print(f"搜索状态: {'成功' if response.success else '失败'}")
        print(f"搜索引擎: {response.provider}")
        print(f"结果数量: {len(response.results)}")
        print(f"耗时: {response.search_time:.2f}s")
        print("\n" + response.to_context())
    else:
        print("未配置搜索引擎 API Key，跳过测试")
