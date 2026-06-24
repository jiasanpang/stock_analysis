# -*- coding: utf-8 -*-
"""Configuration validation logic.

Provides ``validate_config_structured`` which returns a list of
:class:`~src.config.base.ConfigIssue` instances covering all three LLM
configuration tiers (YAML / channels / legacy keys), notification channels,
data sources.
"""

from typing import TYPE_CHECKING, List

from .base import ConfigIssue

if TYPE_CHECKING:
    from .base import Config


def validate_config_structured(config: "Config") -> List[ConfigIssue]:
    """Return structured validation issues with severity levels.

    Covers all three LLM configuration tiers introduced by PR #494:
    - LITELLM_CONFIG (YAML)
    - LLM_CHANNELS (env)
    - Legacy per-provider keys

    Returns:
        List of ConfigIssue objects, each carrying a severity
        ("error" | "warning" | "info"), a human-readable message, and the
        primary environment variable / field name it relates to.
    """
    issues: List[ConfigIssue] = []

    # --- Stock list ---
    if not config.stock_list:
        issues.append(ConfigIssue(
            severity="error",
            message="未配置自选股列表 (STOCK_LIST)",
            field="STOCK_LIST",
        ))

    # --- Data sources (informational only) ---
    if not config.tushare_token:
        issues.append(ConfigIssue(
            severity="info",
            message="未配置 Tushare Token，将使用其他数据源",
            field="TUSHARE_TOKEN",
        ))

    # --- LLM availability ---
    if not config.llm_model_list:
        issues.append(ConfigIssue(
            severity="error",
            message=(
                "未配置任何 LLM（LITELLM_CONFIG / LLM_CHANNELS / *_API_KEY），"
                "AI 分析功能将不可用"
            ),
            field="LITELLM_CONFIG",
        ))
    elif not config.litellm_model:
        issues.append(ConfigIssue(
            severity="info",
            message=(
                "LITELLM_MODEL 未配置，将自动从可用 API Key 推断模型。"
                "建议尽早配置 LITELLM_MODEL（格式如 gemini/gemini-2.5-flash）"
            ),
            field="LITELLM_MODEL",
        ))

    # --- Search engine (informational only) ---
    if not (
        config.bocha_api_keys
        or config.minimax_api_keys
        or config.zhipu_api_keys
        or config.tavily_api_keys
        or config.brave_api_keys
        or config.serpapi_keys
        or config.searxng_base_urls
    ):
        issues.append(ConfigIssue(
            severity="info",
            message="未配置搜索引擎 API Key (Bocha/MiniMax/Zhipu/Tavily/Brave/SerpAPI/SearXNG)，新闻搜索功能将不可用",
            field="BOCHA_API_KEY",
        ))

    # --- Notification channels ---
    has_notification = bool(
        config.wechat_webhook_url
        or config.feishu_webhook_url
        or (config.telegram_bot_token and config.telegram_chat_id)
        or (config.email_sender and config.email_password)
        or (config.pushover_user_key and config.pushover_api_token)
        or config.pushplus_token
        or config.serverchan3_sendkey
        or config.custom_webhook_urls
        or (config.discord_bot_token and config.discord_main_channel_id)
        or config.discord_webhook_url
    )

    if not has_notification:
        issues.append(ConfigIssue(
            severity="warning",
            message="未配置通知渠道，将不发送推送通知",
            field="WECHAT_WEBHOOK_URL",
        ))

    # --- Vision key availability ---
    if config.vision_model:
        _VISION_KEY_MAP = {
            "gemini": config.gemini_api_keys,
            "vertex_ai": config.gemini_api_keys,
            "anthropic": config.anthropic_api_keys,
            "openai": config.openai_api_keys,
            "deepseek": config.deepseek_api_keys,
        }
        _primary_prefix = (
            config.vision_model.split("/")[0]
            if "/" in config.vision_model
            else "openai"
        )
        _priority_providers = [
            p.strip().lower()
            for p in config.vision_provider_priority.split(",")
            if p.strip()
        ]
        _all_providers = {_primary_prefix} | set(_priority_providers)

        _has_any_key = any(
            any(k and len(k) >= 8 for k in (_VISION_KEY_MAP.get(p) or []))
            for p in _all_providers
            if p in _VISION_KEY_MAP
        )
        if not _has_any_key:
            _checked = sorted(_all_providers & _VISION_KEY_MAP.keys())
            issues.append(ConfigIssue(
                severity="warning",
                message=(
                    "VISION_MODEL 已配置，但未找到可用的 Vision API Key "
                    f"（已检查：{', '.join(_checked)}）。"
                    "图片股票代码提取功能将不可用，请配置对应的 API Key。"
                ),
                field="VISION_MODEL",
            ))

    return issues
