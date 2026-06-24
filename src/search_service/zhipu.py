# -*- coding: utf-8 -*-
"""Zhipu BigModel Web Search provider (structured REST API, not LLM tool-call)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from .base_provider import BaseSearchProvider
from .http_utils import _post_with_retry
from .models import SearchResponse, SearchResult

logger = logging.getLogger(__name__)


class ZhipuSearchProvider(BaseSearchProvider):
    """Zhipu BigModel Web Search API.

    Independent REST endpoint (POST /paas/v4/web_search) returning structured
    results (title/url/summary/media/date). Shares the same API key as GLM chat,
    so no extra credential is needed beyond LLM_ZHIPU_API_KEY / ZHIPU_API_KEYS.

    Native time-range filter via ``search_recency_filter``; falls back to
    client-side date filtering when the API omits dates.
    """

    API_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/web_search"

    _CB_FAILURE_THRESHOLD = 3
    _CB_COOLDOWN_SECONDS = 300

    # Map our `days` window to Zhipu recency enum
    _RECENCY_MAP = {
        1: "oneDay",
        7: "oneWeek",
        30: "oneMonth",
        365: "oneYear",
    }

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys, "Zhipu")
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0

    @property
    def is_available(self) -> bool:
        if not self._api_keys:
            return False
        if time.time() < self._circuit_open_until:
            logger.debug(
                f"[Zhipu] Circuit breaker OPEN, cooldown remaining: "
                f"{self._circuit_open_until - time.time():.0f}s"
            )
            return False
        return True

    def _record_success(self, key: str) -> None:
        super()._record_success(key)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_error(self, key: str) -> None:
        super()._record_error(key)
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._CB_FAILURE_THRESHOLD:
            self._circuit_open_until = time.time() + self._CB_COOLDOWN_SECONDS
            logger.warning(
                f"[Zhipu] Circuit breaker OPEN – "
                f"{self._consecutive_failures} consecutive failures, "
                f"cooldown {self._CB_COOLDOWN_SECONDS}s"
            )

    def _resolve_recency(self, days: int) -> str:
        for window, val in sorted(self._RECENCY_MAP.items()):
            if days <= window:
                return val
        return "noLimit"

    @staticmethod
    def _is_within_days(date_str: Optional[str], days: int) -> bool:
        if not date_str:
            return True
        try:
            from dateutil import parser as dateutil_parser
            dt = dateutil_parser.parse(date_str, fuzzy=True)
            now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
            return (now - dt) <= timedelta(days=days + 1)
        except Exception:
            return True

    @staticmethod
    def _extract_domain(url: str) -> str:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc.replace('www.', '')
            return domain or '未知来源'
        except Exception:
            return '未知来源'

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            }
            payload = {
                "search_query": query,
                "search_engine": "search_pro_sogou",
                "search_intent": False,
                "count": min(max(max_results, 1), 50),
                "search_recency_filter": self._resolve_recency(days),
                "content_size": "medium",
            }

            response = _post_with_retry(
                self.API_ENDPOINT, headers=headers, json=payload, timeout=15
            )

            if response.status_code != 200:
                error_msg = self._parse_http_error(response)
                logger.warning(f"[Zhipu] Search failed: {error_msg}")
                return SearchResponse(
                    query=query, results=[], provider=self.name,
                    success=False, error_message=error_msg,
                )

            data = response.json()
            items = data.get('search_result') or []

            results: List[SearchResult] = []
            for item in items:
                date_val = item.get('publish_date') or item.get('date')
                if not self._is_within_days(date_val, days):
                    continue
                link = item.get('link') or item.get('url') or ''
                results.append(SearchResult(
                    title=item.get('title', ''),
                    snippet=(item.get('content') or item.get('summary') or '')[:500],
                    url=link,
                    source=item.get('media') or self._extract_domain(link),
                    published_date=date_val,
                ))
                if len(results) >= max_results:
                    break

            logger.info(f"[Zhipu] Search done, query='{query}', {len(results)} results")

            return SearchResponse(
                query=query, results=results, provider=self.name, success=True,
            )

        except requests.exceptions.Timeout:
            error_msg = "Request timeout"
            logger.error(f"[Zhipu] {error_msg}")
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=error_msg,
            )
        except requests.exceptions.RequestException as e:
            error_msg = f"Network error: {e}"
            logger.error(f"[Zhipu] {error_msg}")
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=error_msg,
            )
        except Exception as e:
            error_msg = f"Unexpected error: {e}"
            logger.error(f"[Zhipu] {error_msg}")
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=error_msg,
            )

    @staticmethod
    def _parse_http_error(response) -> str:
        try:
            ct = response.headers.get('content-type', '')
            if 'json' in ct:
                err = response.json()
                return err.get('error', {}).get('message') or err.get('message') or str(err)
            return response.text[:200]
        except Exception:
            return f"HTTP {response.status_code}: {response.text[:200]}"
