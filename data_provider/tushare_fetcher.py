# -*- coding: utf-8 -*-
"""
===================================
TushareFetcher - 备用数据源 1 (Priority 2)
===================================

数据来源：Tushare Pro API（挖地兔）
特点：需要 Token、有请求配额限制
优点：数据质量高、接口稳定

流控策略：
1. 实现"每分钟调用计数器"
2. 超过免费配额（80次/分）时，强制休眠到下一分钟
3. 使用 tenacity 实现指数退避重试
"""

import json as _json
import logging
import re
import time
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

import numpy as np
import pandas as pd
import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from .base import BaseFetcher, DataFetchError, RateLimitError, STANDARD_COLUMNS,is_bse_code, is_st_stock, is_kc_cy_stock, normalize_stock_code
from .realtime_types import UnifiedRealtimeQuote, ChipDistribution, safe_float, safe_int
from src.config import get_config
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache for ts.realtime_list() full-market snapshot.
# realtime_list returns ~5000 rows; cache 30s to avoid hammering the crawler.
# ---------------------------------------------------------------------------
_realtime_list_cache: Dict[str, Any] = {
    'data': None,
    'timestamp': 0.0,
    'ttl': 30,  # seconds
}


# ETF code prefixes by exchange
# Shanghai: 51xxxx, 52xxxx, 56xxxx, 58xxxx
# Shenzhen: 15xxxx, 16xxxx, 18xxxx
_ETF_SH_PREFIXES = ('51', '52', '56', '58')
_ETF_SZ_PREFIXES = ('15', '16', '18')
_ETF_ALL_PREFIXES = _ETF_SH_PREFIXES + _ETF_SZ_PREFIXES


def _is_etf_code(stock_code: str) -> bool:
    """
    Check if the code is an ETF fund code.

    ETF code ranges:
    - Shanghai ETF: 51xxxx, 52xxxx, 56xxxx, 58xxxx
    - Shenzhen ETF: 15xxxx, 16xxxx, 18xxxx
    """
    code = stock_code.strip().split('.')[0]
    return code.startswith(_ETF_ALL_PREFIXES) and len(code) == 6


def _is_us_code(stock_code: str) -> bool:
    """
    判断代码是否为美股
    
    美股代码规则：
    - 1-5个大写字母，如 'AAPL', 'TSLA'
    - 可能包含 '.'，如 'BRK.B'
    """
    code = stock_code.strip().upper()
    return bool(re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', code))


class TushareFetcher(BaseFetcher):
    """
    Tushare Pro 数据源实现
    
    优先级：2
    数据来源：Tushare Pro API
    
    关键策略：
    - 每分钟调用计数器，防止超出配额
    - 超过 80 次/分钟时强制等待
    - 失败后指数退避重试
    
    配额说明（Tushare 免费用户）：
    - 每分钟最多 80 次请求
    - 每天最多 500 次请求
    """
    
    name = "TushareFetcher"
    priority = int(os.getenv("TUSHARE_PRIORITY", "2"))  # 默认优先级，会在 __init__ 中根据配置动态调整

    def __init__(self, rate_limit_per_minute: int = 80):
        """
        初始化 TushareFetcher

        Args:
            rate_limit_per_minute: 每分钟最大请求数（默认80，Tushare免费配额）
        """
        self.rate_limit_per_minute = rate_limit_per_minute
        self._call_count = 0  # 当前分钟内的调用次数
        self._minute_start: Optional[float] = None  # 当前计数周期开始时间
        self._api: Optional[object] = None  # Tushare API 实例

        # 尝试初始化 API
        self._init_api()

        # 根据 API 初始化结果动态调整优先级
        self.priority = self._determine_priority()
    
    def _init_api(self) -> None:
        """
        初始化 Tushare API
        
        如果 Token 未配置，此数据源将不可用
        """
        config = get_config()
        
        if not config.tushare_token:
            logger.warning("Tushare Token 未配置，此数据源不可用")
            return
        
        try:
            import tushare as ts

            # Pass token directly to pro_api() to avoid writing ~/tk.csv (fixes
            # Operation not permitted in sandbox/restricted envs)
            self._api = ts.pro_api(token=config.tushare_token)
            
            # Fix: tushare SDK 1.4.x hardcodes api.waditu.com/dataapi which may
            # be unavailable (503). Monkey-patch the query method to use the
            # official api.tushare.pro endpoint which posts to root URL.
            self._patch_api_endpoint(config.tushare_token)

            logger.info("Tushare API 初始化成功")
            
        except Exception as e:
            logger.error(f"Tushare API 初始化失败: {e}")
            self._api = None

    def _patch_api_endpoint(self, token: str) -> None:
        """
        Patch tushare SDK to use the official api.tushare.pro endpoint.

        The SDK (v1.4.x) hardcodes http://api.waditu.com/dataapi and appends
        /{api_name} to the URL. That endpoint may return 503, causing silent
        empty-DataFrame failures. This method replaces the query method to
        POST directly to http://api.tushare.pro (root URL, no path suffix).
        """
        import types

        TUSHARE_API_URL = "http://api.tushare.pro"
        _token = token
        _timeout = getattr(self._api, '_DataApi__timeout', 30)

        def patched_query(self_api, api_name, fields='', **kwargs):
            req_params = {
                'api_name': api_name,
                'token': _token,
                'params': kwargs,
                'fields': fields,
            }
            res = requests.post(TUSHARE_API_URL, json=req_params, timeout=_timeout)
            if res.status_code != 200:
                raise Exception(f"Tushare API HTTP {res.status_code}")
            result = _json.loads(res.text)
            if result['code'] != 0:
                raise Exception(result['msg'])
            data = result['data']
            columns = data['fields']
            items = data['items']
            return pd.DataFrame(items, columns=columns)

        self._api.query = types.MethodType(patched_query, self._api)
        logger.debug(f"Tushare API endpoint patched to {TUSHARE_API_URL}")

    def _determine_priority(self) -> int:
        """
        根据 Token 配置和 API 初始化状态确定优先级

        策略：
        - Token 配置且 API 初始化成功：优先级 -1（绝对最高，优于 efinance）
        - 其他情况：优先级 2（默认）

        Returns:
            优先级数字（0=最高，数字越大优先级越低）
        """
        config = get_config()

        if config.tushare_token and self._api is not None:
            # Token 配置且 API 初始化成功，提升为最高优先级
            logger.info("✅ 检测到 TUSHARE_TOKEN 且 API 初始化成功，Tushare 数据源优先级提升为最高 (Priority -1)")
            return -1

        # Token 未配置或 API 初始化失败，保持默认优先级
        return 2

    def is_available(self) -> bool:
        """
        检查数据源是否可用

        Returns:
            True 表示可用，False 表示不可用
        """
        return self._api is not None

    def _check_rate_limit(self) -> None:
        """
        检查并执行速率限制
        
        流控策略：
        1. 检查是否进入新的一分钟
        2. 如果是，重置计数器
        3. 如果当前分钟调用次数超过限制，强制休眠
        """
        current_time = time.time()
        
        # 检查是否需要重置计数器（新的一分钟）
        if self._minute_start is None:
            self._minute_start = current_time
            self._call_count = 0
        elif current_time - self._minute_start >= 60:
            # 已经过了一分钟，重置计数器
            self._minute_start = current_time
            self._call_count = 0
            logger.debug("速率限制计数器已重置")
        
        # 检查是否超过配额
        if self._call_count >= self.rate_limit_per_minute:
            # 计算需要等待的时间（到下一分钟）
            elapsed = current_time - self._minute_start
            sleep_time = max(0, 60 - elapsed) + 1  # +1 秒缓冲
            
            logger.warning(
                f"Tushare 达到速率限制 ({self._call_count}/{self.rate_limit_per_minute} 次/分钟)，"
                f"等待 {sleep_time:.1f} 秒..."
            )
            
            time.sleep(sleep_time)
            
            # 重置计数器
            self._minute_start = time.time()
            self._call_count = 0
        
        # 增加调用计数
        self._call_count += 1
        logger.debug(f"Tushare 当前分钟调用次数: {self._call_count}/{self.rate_limit_per_minute}")
    
    def _convert_stock_code(self, stock_code: str) -> str:
        """
        转换股票代码为 Tushare 格式
        
        Tushare 要求的格式：
        - 沪市股票：600519.SH
        - 深市股票：000001.SZ
        - 沪市 ETF：510050.SH, 563230.SH
        - 深市 ETF：159919.SZ
        
        Args:
            stock_code: 原始代码，如 '600519', '000001', '563230'
            
        Returns:
            Tushare 格式代码，如 '600519.SH', '000001.SZ', '563230.SH'
        """
        code = stock_code.strip()
        
        # Already has suffix
        if '.' in code:
            return code.upper()
        
        # ETF: determine exchange by prefix
        if code.startswith(_ETF_SH_PREFIXES) and len(code) == 6:
            return f"{code}.SH"
        if code.startswith(_ETF_SZ_PREFIXES) and len(code) == 6:
            return f"{code}.SZ"
        
        # BSE (Beijing Stock Exchange): 8xxxxx, 4xxxxx, 920xxx
        if is_bse_code(code):
            return f"{code}.BJ"
        
        # Regular stocks
        # Shanghai: 60xxxx (main board), 688xxx (STAR Market)
        # Shenzhen: 000/001/002/003/004 (main+SME), 300/301xxx (ChiNext)
        if code.startswith("60") or code.startswith("688"):
            return f"{code}.SH"
        elif code.startswith(('000', '001', '002', '003', '004', '300', '301')):
            return f"{code}.SZ"
        else:
            logger.warning(f"无法确定股票 {code} 的市场，默认使用深市")
            return f"{code}.SZ"
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Tushare 获取原始数据
        
        根据代码类型选择不同接口：
        - 普通股票：daily()
        - ETF 基金：fund_daily()
        
        流程：
        1. 检查 API 是否可用
        2. 检查是否为美股（不支持）
        3. 执行速率限制检查
        4. 转换股票代码格式
        5. 根据代码类型选择接口并调用
        """
        if self._api is None:
            raise DataFetchError("Tushare API 未初始化，请检查 Token 配置")
        
        # US stocks not supported
        if _is_us_code(stock_code):
            raise DataFetchError(f"TushareFetcher 不支持美股 {stock_code}，请使用 AkshareFetcher 或 YfinanceFetcher")
        
        # Rate-limit check
        self._check_rate_limit()
        
        # Convert code format
        ts_code = self._convert_stock_code(stock_code)
        
        # Convert date format (Tushare requires YYYYMMDD)
        ts_start = start_date.replace('-', '')
        ts_end = end_date.replace('-', '')
        
        is_etf = _is_etf_code(stock_code)
        api_name = "fund_daily" if is_etf else "daily"
        logger.debug(f"调用 Tushare {api_name}({ts_code}, {ts_start}, {ts_end})")
        
        try:
            if is_etf:
                # ETF uses fund_daily interface
                df = self._api.fund_daily(
                    ts_code=ts_code,
                    start_date=ts_start,
                    end_date=ts_end,
                )
            else:
                # Regular stocks use daily interface
                df = self._api.daily(
                    ts_code=ts_code,
                    start_date=ts_start,
                    end_date=ts_end,
                )
            
            return df
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # 检测配额超限
            if any(keyword in error_msg for keyword in ['quota', '配额', 'limit', '权限']):
                logger.warning(f"Tushare 配额可能超限: {e}")
                raise RateLimitError(f"Tushare 配额超限: {e}") from e
            
            raise DataFetchError(f"Tushare 获取数据失败: {e}") from e
    
    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 Tushare 数据
        
        Tushare daily 返回的列名：
        ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount
        
        需要映射到标准列名：
        date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()
        
        # 列名映射
        column_mapping = {
            'trade_date': 'date',
            'vol': 'volume',
            # open, high, low, close, amount, pct_chg 列名相同
        }
        
        df = df.rename(columns=column_mapping)
        
        # 转换日期格式（YYYYMMDD -> YYYY-MM-DD）
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], format='%Y%m%d')
        
        # 成交量单位转换（Tushare 的 vol 单位是手，需要转换为股）
        if 'volume' in df.columns:
            df['volume'] = df['volume'] * 100
        
        # 成交额单位转换（Tushare 的 amount 单位是千元，转换为元）
        if 'amount' in df.columns:
            df['amount'] = df['amount'] * 1000
        
        # 添加股票代码列
        df['code'] = stock_code
        
        # 只保留需要的列
        keep_cols = ['code'] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        df = df[existing_cols]
        
        return df

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        """
        获取股票名称
        
        使用 Tushare 的 stock_basic 接口获取股票基本信息
        
        Args:
            stock_code: 股票代码
            
        Returns:
            股票名称，失败返回 None
        """
        if self._api is None:
            logger.warning("Tushare API 未初始化，无法获取股票名称")
            return None
        
        # 检查缓存
        if hasattr(self, '_stock_name_cache') and stock_code in self._stock_name_cache:
            return self._stock_name_cache[stock_code]
        
        # 初始化缓存
        if not hasattr(self, '_stock_name_cache'):
            self._stock_name_cache = {}
        
        try:
            # 速率限制检查
            self._check_rate_limit()
            
            # 转换代码格式
            ts_code = self._convert_stock_code(stock_code)
            
            # ETF uses fund_basic, regular stocks use stock_basic
            if _is_etf_code(stock_code):
                df = self._api.fund_basic(
                    ts_code=ts_code,
                    fields='ts_code,name'
                )
            else:
                df = self._api.stock_basic(
                    ts_code=ts_code,
                    fields='ts_code,name'
                )
            
            if df is not None and not df.empty:
                name = df.iloc[0]['name']
                self._stock_name_cache[stock_code] = name
                logger.debug(f"Tushare 获取股票名称成功: {stock_code} -> {name}")
                return name
            
        except Exception as e:
            logger.warning(f"Tushare 获取股票名称失败 {stock_code}: {e}")
        
        return None
    
    def get_stock_list(self) -> Optional[pd.DataFrame]:
        """
        获取股票列表
        
        使用 Tushare 的 stock_basic 接口获取全部股票列表
        
        Returns:
            包含 code, name 列的 DataFrame，失败返回 None
        """
        if self._api is None:
            logger.warning("Tushare API 未初始化，无法获取股票列表")
            return None
        
        try:
            # 速率限制检查
            self._check_rate_limit()
            
            # 调用 stock_basic 接口获取所有股票
            df = self._api.stock_basic(
                exchange='',
                list_status='L',
                fields='ts_code,name,industry,area,market'
            )
            
            if df is not None and not df.empty:
                # 转换 ts_code 为标准代码格式
                df['code'] = df['ts_code'].apply(lambda x: x.split('.')[0])
                
                # 更新缓存
                if not hasattr(self, '_stock_name_cache'):
                    self._stock_name_cache = {}
                for _, row in df.iterrows():
                    self._stock_name_cache[row['code']] = row['name']
                
                logger.info(f"Tushare 获取股票列表成功: {len(df)} 条")
                return df[['code', 'name', 'industry', 'area', 'market']]
            
        except Exception as e:
            logger.warning(f"Tushare 获取股票列表失败: {e}")
        
        return None

    def get_chip_distribution(self, stock_code: str) -> Optional[ChipDistribution]:
        """
        Get chip distribution via Tushare cyq_chips (requires 5000+ points).

        Data source: pro.cyq_chips() - daily cost distribution by price level.
        Updates 18-19:00 daily. Fallback when Akshare (Eastmoney) fails.
        """
        if self._api is None:
            return None
        if _is_us_code(stock_code):
            logger.debug(f"[API跳过] {stock_code} 是美股，无筹码分布数据")
            return None
        if _is_etf_code(stock_code):
            logger.debug(f"[API跳过] {stock_code} 是 ETF，无筹码分布数据")
            return None

        try:
            self._check_rate_limit()
            ts_code = self._convert_stock_code(stock_code)
            china_now = datetime.now(ZoneInfo("Asia/Shanghai"))
            end_date = china_now.strftime("%Y%m%d")
            start_date = (china_now - pd.Timedelta(days=10)).strftime("%Y%m%d")

            df_cal = self._api.trade_cal(exchange="SSE", start_date=start_date, end_date=end_date)
            if df_cal is None or df_cal.empty:
                return None
            df_cal.columns = [c.lower() for c in df_cal.columns]
            open_dates = df_cal[df_cal["is_open"] == 1]["cal_date"].tolist()
            if not open_dates:
                return None
            trade_date = open_dates[-1]

            self._check_rate_limit()
            df = self._api.cyq_chips(ts_code=ts_code, trade_date=trade_date)
            if df is None or df.empty:
                logger.debug(f"[筹码分布] Tushare cyq_chips 返回空 {stock_code} {trade_date}")
                return None

            df.columns = [c.lower() for c in df.columns]
            if df.empty:
                return None

            price = pd.to_numeric(df["price"], errors="coerce")
            pct = pd.to_numeric(df["percent"], errors="coerce").fillna(0)
            valid = price.notna() & (pct > 0)
            price = price[valid].values
            pct = pct[valid].values
            if len(price) == 0:
                return None

            avg_cost = float(np.sum(price * pct) / 100)
            if avg_cost <= 0:
                return None

            self._check_rate_limit()
            df_daily = self._api.daily(ts_code=ts_code, trade_date=trade_date)
            close_price = None
            if df_daily is not None and not df_daily.empty:
                df_daily.columns = [c.lower() for c in df_daily.columns]
                close_price = safe_float(df_daily.iloc[0].get("close"))

            idx = np.argsort(price)
            price_s = price[idx]
            pct_s = pct[idx]
            cum = np.cumsum(pct_s)

            def _percentile_price(pct_target: float) -> float:
                i = np.searchsorted(cum, pct_target)
                return float(price_s[min(i, len(price_s) - 1)])

            cost_90_low = _percentile_price(5)
            cost_90_high = _percentile_price(95)
            cost_70_low = _percentile_price(15)
            cost_70_high = _percentile_price(85)
            concentration_90 = (cost_90_high - cost_90_low) / avg_cost if avg_cost > 0 else 0
            concentration_70 = (cost_70_high - cost_70_low) / avg_cost if avg_cost > 0 else 0

            profit_ratio = 0.0
            if close_price and close_price > 0:
                below = pct_s[price_s < close_price]
                profit_ratio = float(np.sum(below) / 100) if len(below) > 0 else 0

            date_str = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
            chip = ChipDistribution(
                code=stock_code,
                date=date_str,
                source="tushare",
                profit_ratio=profit_ratio,
                avg_cost=avg_cost,
                cost_90_low=cost_90_low,
                cost_90_high=cost_90_high,
                concentration_90=concentration_90,
                cost_70_low=cost_70_low,
                cost_70_high=cost_70_high,
                concentration_70=concentration_70,
            )
            logger.info(
                f"[筹码分布] {stock_code} Tushare 日期={date_str}: 获利比例={chip.profit_ratio:.1%}, "
                f"平均成本={chip.avg_cost:.2f}, 90%集中度={chip.concentration_90:.2%}"
            )
            return chip
        except Exception as e:
            logger.warning(f"[筹码分布] Tushare 获取 {stock_code} 失败: {e}")
            return None

    # ------------------------------------------------------------------
    # realtime_list full-market snapshot (cached, with wall-clock timeout)
    # ------------------------------------------------------------------
    def _fetch_realtime_list(self, timeout: float = 10.0) -> Optional[pd.DataFrame]:
        """
        Fetch full-market realtime snapshot via ts.realtime_list(src='dc').

        Features:
        - Module-level cache (TTL 30s) to avoid hammering the crawler endpoint.
        - Wall-clock timeout (default 10s) via ThreadPoolExecutor.
        - Rate-limit safe: realtime_list is a 0-credit crawler API.

        Returns:
            DataFrame with ~5000 rows, or None on failure / timeout.
        """
        current_time = time.time()

        # --- cache check ---
        if (
            _realtime_list_cache['data'] is not None
            and current_time - _realtime_list_cache['timestamp'] < _realtime_list_cache['ttl']
        ):
            cache_age = int(current_time - _realtime_list_cache['timestamp'])
            logger.debug(
                f"[realtime_list] cache hit, age {cache_age}s / {_realtime_list_cache['ttl']}s"
            )
            return _realtime_list_cache['data']

        # --- fetch with timeout (explicit pool to avoid shutdown(wait=True) blocking) ---
        import tushare as ts

        def _call():
            return ts.realtime_list(src='dc')

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ts_realtime_list")
        future = pool.submit(_call)
        try:
            df = future.result(timeout=timeout)

            if df is not None and not df.empty:
                _realtime_list_cache['data'] = df
                _realtime_list_cache['timestamp'] = time.time()
                logger.info(
                    f"[realtime_list] fetched {len(df)} rows, cache refreshed (TTL={_realtime_list_cache['ttl']}s)"
                )
                return df
            else:
                logger.warning("[realtime_list] returned empty DataFrame")
                return None

        except FuturesTimeoutError:
            logger.warning(f"[realtime_list] wall-clock timeout ({timeout}s)")
            return None
        except Exception as e:
            logger.warning(f"[realtime_list] failed: {e}")
            return None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """
        Get realtime quote for a single stock.

        Strategy (priority order):
        1. ts.realtime_list(src='dc') – 0-credit crawler, full-market snapshot
           with 30s cache.  Rich fields: price, pct_change, volume, amount,
           open, high, low, pre_close, etc.
        2. ts.realtime_quote() – 0-credit crawler, per-stock (legacy)
        3. ts.get_realtime_quotes() – old Sina-based API (legacy)
        4. Pro quotation API (requires credits)

        Args:
            stock_code: Stock code (6-digit or with exchange suffix)

        Returns:
            UnifiedRealtimeQuote object, or None on failure
        """
        if self._api is None:
            return None

        from .realtime_types import RealtimeSource

        code_6 = stock_code.split('.')[0] if '.' in stock_code else stock_code

        # === Strategy 1: realtime_list (full-market, cached) ===
        df_all = self._fetch_realtime_list()
        if df_all is not None and not df_all.empty:
            try:
                # realtime_list returns ts_code like '000001.SZ'
                ts_code_full = self._convert_stock_code(stock_code)
                row = df_all[df_all['ts_code'] == ts_code_full]
                if row.empty:
                    # Also try matching by pure 6-digit code prefix
                    row = df_all[df_all['ts_code'].str.startswith(code_6)]
                if not row.empty:
                    row = row.iloc[0]
                    _price = safe_float(row.get('price'))
                    _pre = safe_float(row.get('pre_close'))
                    _change_amt = safe_float(row.get('change'))
                    if _change_amt is None and _price is not None and _pre is not None and _pre != 0:
                        _change_amt = round(_price - _pre, 4)

                    logger.debug(f"[RealTime] {stock_code} via realtime_list (cached full-market)")
                    return UnifiedRealtimeQuote(
                        code=stock_code,
                        name=str(row.get('name', '')),
                        source=RealtimeSource.TUSHARE,
                        price=_price,
                        change_pct=safe_float(row.get('pct_change')),
                        change_amount=_change_amt,
                        volume=safe_int(row.get('volume')),
                        amount=safe_float(row.get('amount')),
                        high=safe_float(row.get('high')),
                        low=safe_float(row.get('low')),
                        open_price=safe_float(row.get('open')),
                        pre_close=_pre,
                        turnover_rate=None,
                        pe_ratio=None,
                        pb_ratio=None,
                        total_mv=None,
                    )
            except Exception as e:
                logger.debug(f"[RealTime] realtime_list lookup failed for {stock_code}: {e}")

        # === Strategy 2: ts.realtime_quote() – 0-credit crawler (per stock) ===
        try:
            import tushare as ts
            df = ts.realtime_quote(code_6)

            if df is not None and not df.empty:
                row = df.iloc[0]
                logger.debug(f"[RealTime] {stock_code} via realtime_quote (0-credit crawler)")
                change_pct = safe_float(row.get('changepercent')) or safe_float(row.get('change_pct'))
                _price = safe_float(row.get('price'))
                _pre = safe_float(row.get('pre_close')) or safe_float(row.get('settlement'))
                _change_amount = (
                    (_price - _pre) if (_price is not None and _pre is not None and _pre != 0) else None
                )
                return UnifiedRealtimeQuote(
                    code=stock_code,
                    name=str(row.get('name', '')),
                    source=RealtimeSource.TUSHARE,
                    price=_price,
                    change_pct=change_pct,
                    change_amount=_change_amount,
                    volume=safe_int(row.get('volume')),
                    amount=None,
                    high=None,
                    low=None,
                    open_price=None,
                    pre_close=_pre,
                    turnover_rate=None,
                    pe_ratio=None,
                    pb_ratio=None,
                    total_mv=None,
                )
        except Exception as e:
            logger.debug(f"[RealTime] realtime_quote failed: {e}")

        # === Strategy 3: legacy ts.get_realtime_quotes (Sina-based) ===
        try:
            import tushare as ts
            if code_6 == '000001':
                symbol = 'sh000001'
            elif code_6 == '399001':
                symbol = 'sz399001'
            elif code_6 == '399006':
                symbol = 'sz399006'
            elif code_6 == '000300':
                symbol = 'sh000300'
            elif is_bse_code(code_6):
                symbol = f"bj{code_6}"
            else:
                symbol = code_6

            df = ts.get_realtime_quotes(symbol)
            if df is not None and not df.empty:
                row = df.iloc[0]
                logger.debug(f"[RealTime] {stock_code} via get_realtime_quotes (legacy)")
                _price = safe_float(row.get('price'))
                _pre = safe_float(row.get('pre_close')) or safe_float(row.get('settlement'))
                _change_amount = (
                    (_price - _pre) if (_price is not None and _pre is not None and _pre != 0) else None
                )
                return UnifiedRealtimeQuote(
                    code=stock_code,
                    name=str(row.get('name', '')),
                    source=RealtimeSource.TUSHARE,
                    price=_price,
                    change_pct=safe_float(row.get('changepercent')),
                    change_amount=_change_amount,
                    volume=safe_int(row.get('volume')),
                    amount=None,
                    high=None,
                    low=None,
                    open_price=None,
                    pre_close=_pre,
                    turnover_rate=None,
                    pe_ratio=None,
                    pb_ratio=None,
                    total_mv=None,
                )
        except Exception as e:
            logger.debug(f"[RealTime] get_realtime_quotes failed: {e}")

        # === Strategy 4: Pro quotation API (requires credits) ===
        try:
            self._check_rate_limit()
            ts_code = self._convert_stock_code(stock_code)
            df = self._api.quotation(ts_code=ts_code)
            if df is not None and not df.empty:
                row = df.iloc[0]
                logger.debug(f"[RealTime] {stock_code} via Pro quotation API")
                return UnifiedRealtimeQuote(
                    code=stock_code,
                    name=str(row.get('name', '')),
                    source=RealtimeSource.TUSHARE,
                    price=safe_float(row.get('price')),
                    change_pct=safe_float(row.get('pct_chg')),
                    change_amount=safe_float(row.get('change')),
                    volume=safe_int(row.get('vol')),
                    amount=safe_float(row.get('amount')),
                    high=safe_float(row.get('high')),
                    low=safe_float(row.get('low')),
                    open_price=safe_float(row.get('open')),
                    pre_close=safe_float(row.get('pre_close')),
                    turnover_rate=safe_float(row.get('turnover_ratio')),
                    pe_ratio=safe_float(row.get('pe')),
                    pb_ratio=safe_float(row.get('pb')),
                    total_mv=safe_float(row.get('total_mv')),
                )
        except Exception as e:
            logger.debug(f"[RealTime] Pro quotation API failed: {e}")

        logger.warning(f"[RealTime] Unable to fetch realtime quote for {stock_code}")
        return None

    def get_main_indices(self, region: str = "cn") -> Optional[List[dict]]:
        """Fetch main index daily data via Tushare index_daily API.

        This serves as a reliable fallback for overseas users where
        efinance/akshare (Eastmoney/Sina) time out.

        Strategy:
        - Query each index for the last 5 calendar days via index_daily.
        - Use the latest available row (handles weekends / holidays).
        - Return format aligned with efinance/akshare get_main_indices().
        """
        if region != "cn":
            return None
        if self._api is None:
            return None

        from .realtime_types import safe_float

        # Index mapping: ts_code -> (display_name, efinance-style code)
        indices_map = {
            '000001.SH': ('上证指数', 'sh000001'),
            '399001.SZ': ('深证成指', 'sz399001'),
            '399006.SZ': ('创业板指', 'sz399006'),
            '000016.SH': ('上证50', 'sh000016'),
            '000905.SH': ('中证500', 'sh000905'),
            '000688.SH': ('科创50', 'sh000688'),
            '000300.SH': ('沪深300', 'sh000300'),
        }

        china_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        end_date = china_now.strftime('%Y%m%d')
        start_date = (china_now - pd.Timedelta(days=7)).strftime('%Y%m%d')

        results = []

        for ts_code, (name, efinance_code) in indices_map.items():
            try:
                self._check_rate_limit()
                df = self._api.index_daily(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                )
                if df is None or df.empty:
                    logger.debug(f"[Tushare] index_daily empty for {ts_code}")
                    continue

                # index_daily returns newest first; take the first row
                row = df.iloc[0]
                trade_date = str(row.get('trade_date', ''))

                current = safe_float(row['close'])
                prev_close = safe_float(row['pre_close'])
                high = safe_float(row['high'])
                low = safe_float(row['low'])

                # Format data_date as YYYY-MM-DD for freshness tracking
                data_date = (
                    f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
                    if len(trade_date) == 8 else trade_date
                )

                # Compute amplitude (%) consistent with efinance/akshare
                amplitude = 0.0
                if prev_close and prev_close > 0 and high and low:
                    amplitude = (high - low) / prev_close * 100

                results.append({
                    'code': efinance_code,
                    'name': name,
                    'current': current,
                    'change': safe_float(row['change']),
                    'change_pct': safe_float(row['pct_chg']),
                    'open': safe_float(row['open']),
                    'high': high,
                    'low': low,
                    'prev_close': prev_close,
                    'volume': safe_float(row['vol']),
                    'amount': safe_float(row['amount']) * 1000 if safe_float(row['amount']) else 0,
                    'amplitude': amplitude,
                    'data_date': data_date,
                })
            except Exception as e:
                logger.debug(f"[Tushare] index_daily failed for {name}({ts_code}): {e}")
                continue

        if results:
            logger.info(f"[Tushare] Fetched {len(results)}/{len(indices_map)} index quotes")
            return results

        logger.warning("[Tushare] No index data retrieved from index_daily")
        return None

    def get_market_stats(self) -> Optional[dict]:
        """
        Get market up/down statistics.

        Strategy (priority order):
        1. During trading hours: use realtime_list (0-credit crawler) for live
           statistics. This is much more timely than daily() which only has
           post-market data.
        2. Fallback to daily() Pro API (works both intraday and post-market).
        """
        if self._api is None:
            return None

        try:
            self._check_rate_limit()
            logger.info("[API] Tushare get_market_stats...")

            china_now = datetime.now(ZoneInfo("Asia/Shanghai"))
            china_now_str = china_now.strftime("%H:%M")
            current_date = china_now.strftime("%Y%m%d")

            start_date = (datetime.now() - pd.Timedelta(days=20)).strftime('%Y%m%d')
            df_cal = self._api.trade_cal(exchange='SSE', start_date=start_date, end_date=current_date)
            date_list = df_cal[df_cal['is_open'] == 1]['cal_date'].tolist()

            is_trading_day = current_date in date_list
            use_realtime = is_trading_day and '09:25' <= china_now_str <= '16:30'

            # === Strategy 1: realtime_list during trading hours ===
            if use_realtime:
                rl_stats = self._market_stats_from_realtime_list()
                if rl_stats is not None:
                    rl_stats["data_date"] = china_now.strftime("%Y-%m-%d")
                    logger.info("[Tushare] market_stats from realtime_list (live)")
                    return rl_stats
                logger.debug("[Tushare] realtime_list unavailable, falling back to daily()")

            # === Strategy 2: daily() Pro API ===
            if use_realtime:
                target_date = current_date
                fallback_date = date_list[1] if len(date_list) > 1 else None
                logger.info("[Tushare] Trading hours, attempting daily() for market stats")
            else:
                fallback_date = None
                if current_date not in date_list:
                    target_date = date_list[0]
                elif china_now_str < '09:30':
                    target_date = date_list[1] if len(date_list) > 1 else date_list[0]
                else:
                    target_date = date_list[0]

            try:
                df = self._api.daily(
                    TS_CODE='3*.SZ,6*.SH,0*.SZ,92*.BJ',
                    start_date=target_date, end_date=target_date,
                )

                if (df is None or df.empty) and fallback_date:
                    logger.info(
                        f"[Tushare] daily() empty for {target_date}, "
                        f"falling back to {fallback_date}"
                    )
                    target_date = fallback_date
                    df = self._api.daily(
                        TS_CODE='3*.SZ,6*.SH,0*.SZ,92*.BJ',
                        start_date=fallback_date, end_date=fallback_date,
                    )

                if df is not None and not df.empty:
                    df.columns = [col.lower() for col in df.columns]
                    df_basic = self._api.stock_basic(fields='ts_code,name')
                    df = pd.merge(df, df_basic, on='ts_code', how='left')
                    if 'amount' in df.columns:
                        df['amount'] = df['amount'] * 1000

                    logger.info(
                        f"[Tushare] market_stats from daily() date={target_date}, rows={len(df)}"
                    )
                    stats = self._calc_market_stats(df)
                    if stats is not None:
                        stats["data_date"] = (
                            f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
                        )
                    return stats
                else:
                    logger.warning(
                        f"[Tushare] daily() returned empty for {target_date}, "
                        "no market stats available"
                    )
            except Exception as e:
                logger.error(f"[Tushare] ts.pro_api().daily failed: {e}")

        except Exception as e:
            logger.error(f"[Tushare] get_market_stats failed: {e}")

        return None

    def _market_stats_from_realtime_list(self) -> Optional[Dict[str, Any]]:
        """
        Compute market statistics directly from realtime_list snapshot.

        This avoids consuming Pro API credits and provides truly live data
        during trading hours.

        Returns:
            dict with up_count, down_count, flat_count, limit_up_count,
            limit_down_count, total_amount; or None on failure.
        """
        df = self._fetch_realtime_list()
        if df is None or df.empty:
            return None

        try:
            # realtime_list columns: ts_code, name, price, pct_change, change,
            # volume, amount, open, high, low, pre_close, ...
            required = {'ts_code', 'price', 'pre_close'}
            if not required.issubset(set(df.columns)):
                logger.warning(f"[realtime_list] missing required columns: {required - set(df.columns)}")
                return None

            # Filter to A-share stocks only (SH/SZ/BJ main boards)
            df = df[df['ts_code'].str.match(r'^(0|3|6|9)\d{5}\.(SZ|SH|BJ)$', na=False)].copy()
            if df.empty:
                return None

            df['price'] = pd.to_numeric(df['price'], errors='coerce')
            df['pre_close'] = pd.to_numeric(df['pre_close'], errors='coerce')

            # Drop rows with missing / zero prices or suspended stocks
            amount_col = 'amount' if 'amount' in df.columns else None
            if amount_col:
                df[amount_col] = pd.to_numeric(df[amount_col], errors='coerce')
                df = df[(df['price'] > 0) & (df['pre_close'] > 0) & (df[amount_col] > 0)]
            else:
                df = df[(df['price'] > 0) & (df['pre_close'] > 0)]

            up_count = 0
            down_count = 0
            flat_count = 0
            limit_up_count = 0
            limit_down_count = 0

            name_col = 'name' if 'name' in df.columns else None

            for _, row in df.iterrows():
                ts_code = str(row['ts_code'])
                pure_code = ts_code.split('.')[0]
                stock_name = str(row.get('name', '')) if name_col else ''
                cur = float(row['price'])
                pre = float(row['pre_close'])

                if is_bse_code(pure_code):
                    ratio = 0.30
                elif is_kc_cy_stock(pure_code):
                    ratio = 0.20
                elif is_st_stock(stock_name):
                    ratio = 0.05
                else:
                    ratio = 0.10

                limit_up_price = np.floor(pre * (1 + ratio) * 100 + 0.5) / 100.0
                limit_down_price = np.floor(pre * (1 - ratio) * 100 + 0.5) / 100.0
                lu_tol = round(abs(pre * (1 + ratio) - limit_up_price), 10)
                ld_tol = round(abs(pre * (1 - ratio) - limit_down_price), 10)

                if abs(cur - limit_up_price) <= lu_tol:
                    limit_up_count += 1
                if abs(cur - limit_down_price) <= ld_tol:
                    limit_down_count += 1

                if cur > pre:
                    up_count += 1
                elif cur < pre:
                    down_count += 1
                else:
                    flat_count += 1

            total_amount = 0.0
            if amount_col and amount_col in df.columns:
                # realtime_list amount is in yuan; convert to 亿
                total_amount = df[amount_col].sum() / 1e8

            return {
                'up_count': up_count,
                'down_count': down_count,
                'flat_count': flat_count,
                'limit_up_count': limit_up_count,
                'limit_down_count': limit_down_count,
                'total_amount': total_amount,
            }

        except Exception as e:
            logger.warning(f"[realtime_list] market stats computation failed: {e}")
            return None
    
    def _calc_market_stats(
            self,
            df: pd.DataFrame,
            ) -> Optional[Dict[str, Any]]:
            """从行情 DataFrame 计算涨跌统计。"""
            import numpy as np

            df = df.copy()
            
            # 1. 提取基础比对数据：最新价、昨收
            # 兼容不同接口返回的列名 sina/em efinance tushare xtdata
            code_col = next((c for c in ['代码', '股票代码', 'ts_code','stock_code'] if c in df.columns), None)
            name_col = next((c for c in ['名称', '股票名称','name','name'] if c in df.columns), None)
            close_col = next((c for c in ['最新价', '最新价', 'close','lastPrice'] if c in df.columns), None)
            pre_close_col = next((c for c in ['昨收', '昨日收盘', 'pre_close','lastClose'] if c in df.columns), None)
            amount_col = next((c for c in ['成交额', '成交额', 'amount','amount'] if c in df.columns), None) 
            
            limit_up_count = 0
            limit_down_count = 0
            up_count = 0
            down_count = 0
            flat_count = 0

            for code, name, current_price, pre_close, amount in zip(
                df[code_col], df[name_col], df[close_col], df[pre_close_col], df[amount_col]
            ):
                
                # 停牌过滤 efinance 的停牌数据有时候会缺失价格显示为 '-'，em 显示为none
                if pd.isna(current_price) or pd.isna(pre_close) or current_price in ['-'] or pre_close in ['-'] or amount == 0:
                    continue
                
                # em、efinance 为str 需要转换为float
                current_price = float(current_price)
                pre_close = float(pre_close)
                
                # 获取去除前缀的纯数字代码
                pure_code = normalize_stock_code(str(code)) 

                # A. 确定每只股票的涨跌幅比例 (使用纯数字代码判断)
                if is_bse_code(pure_code): 
                    ratio = 0.30
                elif is_kc_cy_stock(pure_code): #pure_code.startswith(('688', '30')):
                    ratio = 0.20
                elif is_st_stock(name): #'ST' in str_name:
                    ratio = 0.05
                else:
                    ratio = 0.10

                # B. 严格按照 A 股规则计算涨跌停价：昨收 * (1 ± 比例) -> 四舍五入保留2位小数
                limit_up_price = np.floor(pre_close * (1 + ratio) * 100 + 0.5) / 100.0
                limit_down_price = np.floor(pre_close * (1 - ratio) * 100 + 0.5) / 100.0

                limit_up_price_Tolerance = round(abs(pre_close * (1 + ratio) - limit_up_price), 10)
                limit_down_price_Tolerance = round(abs(pre_close * (1 - ratio) - limit_down_price), 10)

                # C. 精确比对
                if current_price > 0 :
                    is_limit_up = (current_price > 0) and (abs(current_price - limit_up_price) <= limit_up_price_Tolerance)
                    is_limit_down = (current_price > 0) and (abs(current_price - limit_down_price) <= limit_down_price_Tolerance)

                    if is_limit_up:
                        limit_up_count += 1
                    if is_limit_down:
                        limit_down_count += 1

                    if current_price > pre_close:
                        up_count += 1
                    elif current_price < pre_close:
                        down_count += 1
                    else:
                        flat_count += 1
                    
            # 统计数量
            stats = {
                'up_count': up_count,
                'down_count': down_count,
                'flat_count': flat_count,
                'limit_up_count': limit_up_count,
                'limit_down_count': limit_down_count,
                'total_amount': 0.0,
            }
            
            # 成交额统计
            if amount_col and amount_col in df.columns:
                df[amount_col] = pd.to_numeric(df[amount_col], errors='coerce')
                stats['total_amount'] = (df[amount_col].sum() / 1e8)
                
            return stats

    def get_sector_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        """
        获取板块涨跌榜 (Tushare Pro sw_daily, 5000+ 积分).
        申万一级行业日线，按涨跌幅排序。优先于东财接口（易限流）。
        """
        if self._api is None:
            return None
        try:
            self._check_rate_limit()
            china_now = datetime.now(ZoneInfo("Asia/Shanghai"))
            today_str = china_now.strftime("%Y%m%d")
            end_date = today_str
            start_date = (china_now - pd.Timedelta(days=10)).strftime("%Y%m%d")

            df_cal = self._api.trade_cal(exchange="SSE", start_date=start_date, end_date=end_date)
            if df_cal is None or df_cal.empty:
                return None
            df_cal.columns = [c.lower() for c in df_cal.columns]
            open_dates = df_cal[df_cal["is_open"] == 1]["cal_date"].tolist()
            # Tushare trade_cal usually returns descending order (latest first), but let's sort to be safe
            open_dates = sorted(open_dates, reverse=True)
            if not open_dates:
                return None
            
            # For market review, we really want TODAY's sector rankings.
            # If today is a trading day but data is not yet available (e.g. during trading hours),
            # sw_daily will return empty. We should NOT fall back to yesterday's data because
            # that would confuse the user (reporting yesterday's rising sectors as today's).
            # So we only check the latest trading day. If it's today and empty, we return None
            # to let the manager fall back to realtime fetchers (efinance/akshare).
            latest_trade_date = open_dates[0]
            
            self._check_rate_limit()
            df_sw = self._api.sw_daily(trade_date=latest_trade_date, fields="ts_code,name,pct_change")
            
            if df_sw is None or df_sw.empty:
                logger.debug(f"[板块排行] Tushare sw_daily 返回空 {latest_trade_date}，可能尚未更新")
                return None

            df_sw.columns = [c.lower() for c in df_sw.columns]
            if "pct_change" not in df_sw.columns:
                return None
            df_sw["pct_change"] = pd.to_numeric(df_sw["pct_change"], errors="coerce")
            df_sw = df_sw.dropna(subset=["pct_change"])
            name_col = "name" if "name" in df_sw.columns else df_sw.columns[1]

            top = df_sw.nlargest(n, "pct_change")
            bottom = df_sw.nsmallest(n, "pct_change")
            top_sectors = [{"name": str(row[name_col]), "change_pct": float(row["pct_change"])} for _, row in top.iterrows()]
            bottom_sectors = [{"name": str(row[name_col]), "change_pct": float(row["pct_change"])} for _, row in bottom.iterrows()]
            logger.info(f"[板块排行] Tushare sw_daily 成功: 领涨/领跌各{n}个板块")
            return top_sectors, bottom_sectors
        except Exception as e:
            logger.warning(f"[板块排行] Tushare 获取失败: {e}")
            return None


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    fetcher = TushareFetcher()
    
    try:
        # 测试历史数据
        df = fetcher.get_daily_data('600519')  # 茅台
        print(f"获取成功，共 {len(df)} 条数据")
        print(df.tail())
        
        # 测试股票名称
        name = fetcher.get_stock_name('600519')
        print(f"股票名称: {name}")
        
    except Exception as e:
        print(f"获取失败: {e}")

    # 测试市场统计
    print("\n" + "=" * 50)
    print("Testing get_market_stats (tushare)")
    print("=" * 50)
    try:
        stats = fetcher.get_market_stats()
        if stats:
            print(f"Market Stats successfully computed:")
            print(f"Up: {stats['up_count']} (Limit Up: {stats['limit_up_count']})")
            print(f"Down: {stats['down_count']} (Limit Down: {stats['limit_down_count']})")
            print(f"Flat: {stats['flat_count']}")
            print(f"Total Amount: {stats['total_amount']:.2f} 亿 (Yi)")
        else:
            print("Failed to compute market stats.")
    except Exception as e:
        print(f"Failed to compute market stats: {e}")
