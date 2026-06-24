# -*- coding: utf-8 -*-
"""Environment variable loading and Config construction.

Contains:
- ``load_config_from_env()`` — builds a :class:`Config` from ``.env`` / os.environ
- ``get_config()`` — convenience singleton accessor
- ``get_effective_push_report_type()`` — resolves push report type
- Various parse/resolve helpers for individual config fields
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .base import Config, setup_env
from .llm_config import (
    channels_to_model_list,
    legacy_keys_to_model_list,
    parse_litellm_yaml,
    parse_llm_channels,
)

_logger = logging.getLogger(__name__)


def _merge_csv_env_keys(single_var: str, plural_var: str) -> List[str]:
    """Parse comma-separated API keys from singular env var and legacy plural alias."""
    merged: List[str] = []
    seen: set[str] = set()
    for chunk in (os.getenv(single_var, ""), os.getenv(plural_var, "")):
        for k in chunk.split(","):
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                merged.append(k)
    return merged


# ---------------------------------------------------------------------------
# Convenience singleton accessor
# ---------------------------------------------------------------------------

def get_config() -> Config:
    """Shortcut to get the global Config singleton."""
    return Config.get_instance()


def get_effective_push_report_type(cfg: Optional[Config] = None) -> str:
    """Return report type for push (same as REPORT_TYPE; dashboard and push unified)."""
    if cfg is None:
        cfg = get_config()
    return cfg.report_type


# ---------------------------------------------------------------------------
# Field-level parse / resolve helpers
# ---------------------------------------------------------------------------

def _parse_report_type(value: str) -> str:
    """Parse REPORT_TYPE, fallback to simple for invalid values (supports brief)."""
    v = (value or 'simple').strip().lower()
    if v in ('simple', 'full', 'brief'):
        return v
    _logger.warning(
        f"REPORT_TYPE '{value}' invalid, fallback to 'simple' (valid: simple/full/brief)"
    )
    return 'simple'


def _parse_market_review_region(value: str) -> str:
    """Parse market review region, fallback to cn for invalid values."""
    v = (value or 'cn').strip().lower()
    if v in ('cn', 'us', 'both'):
        return v
    _logger.warning(
        f"MARKET_REVIEW_REGION 配置值 '{value}' 无效，已回退为默认值 'cn'（合法值：cn / us / both）"
    )
    return 'cn'


def _parse_md2img_engine(value: str) -> str:
    """Parse MD2IMG_ENGINE, fallback to wkhtmltoimage for invalid values (Issue #455)."""
    v = (value or 'wkhtmltoimage').strip().lower()
    if v in ('wkhtmltoimage', 'markdown-to-file'):
        return v
    if v:
        _logger.warning(
            f"MD2IMG_ENGINE '{value}' invalid, fallback to 'wkhtmltoimage' "
            "(valid: wkhtmltoimage | markdown-to-file)"
        )
    return 'wkhtmltoimage'


def _parse_picker_mode(value: str) -> str:
    """Parse PICKER_MODE, fallback to balanced for invalid values."""
    v = (value or 'balanced').strip().lower()
    if v in ('defensive', 'offensive', 'balanced'):
        return v
    if v:
        _logger.warning(
            f"PICKER_MODE '{value}' invalid, fallback to 'balanced' "
            "(valid: defensive | offensive | balanced)"
        )
    return 'balanced'


def _resolve_bias_threshold() -> float:
    """Resolve BIAS_THRESHOLD: use env if set, else derive from PICKER_MODE (6/8/10)."""
    explicit = os.getenv('BIAS_THRESHOLD')
    if explicit:
        return max(1.0, float(explicit))
    picker = _parse_picker_mode(os.getenv('PICKER_MODE', 'balanced'))
    default = {'defensive': 6.0, 'balanced': 8.0, 'offensive': 10.0}.get(picker, 8.0)
    return max(1.0, default)


def _parse_picker_strategies(value: str) -> List[str]:
    """Parse PICKER_STRATEGIES (comma-separated). Default [buy_pullback] when empty."""
    valid = ('buy_pullback', 'bottom_reversal', 'reversal_breakout', 'small_cap')
    if not value or not value.strip():
        return ['buy_pullback']
    parts = [p.strip().lower() for p in value.split(',') if p.strip()]
    result = [p for p in parts if p in valid]
    return result if result else ['buy_pullback']


def _resolve_realtime_source_priority() -> str:
    """Resolve realtime source priority with automatic tushare injection.

    When TUSHARE_TOKEN is configured but REALTIME_SOURCE_PRIORITY is not
    explicitly set, automatically prepend 'tushare' to the default priority.
    """
    explicit = os.getenv('REALTIME_SOURCE_PRIORITY')
    default_priority = 'tencent,akshare_sina,efinance,akshare_em'

    if explicit:
        return explicit

    tushare_token = os.getenv('TUSHARE_TOKEN', '').strip()
    if tushare_token:
        resolved = f'tushare,{default_priority}'
        _logger.info(
            f"TUSHARE_TOKEN detected, auto-injecting tushare into realtime priority: {resolved}"
        )
        return resolved

    return default_priority


def _parse_stock_email_groups() -> List[Tuple[List[str], List[str]]]:
    """Parse STOCK_GROUP_N and EMAIL_GROUP_N from environment.

    Returns [(stocks, emails), ...] ordered by group index.
    """
    groups: dict = {}
    stock_re = re.compile(r'^STOCK_GROUP_(\d+)$', re.IGNORECASE)
    email_re = re.compile(r'^EMAIL_GROUP_(\d+)$', re.IGNORECASE)
    for key in os.environ:
        m = stock_re.match(key)
        if m:
            idx = int(m.group(1))
            val = os.environ[key].strip()
            groups.setdefault(idx, {})['stocks'] = [c.strip() for c in val.split(',') if c.strip()]
        m = email_re.match(key)
        if m:
            idx = int(m.group(1))
            val = os.environ[key].strip()
            groups.setdefault(idx, {})['emails'] = [e.strip() for e in val.split(',') if e.strip()]
    result = []
    for idx in sorted(groups.keys()):
        g = groups[idx]
        if 'stocks' in g and 'emails' in g and g['stocks'] and g['emails']:
            result.append((g['stocks'], g['emails']))
    return result


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load_config_from_env() -> Config:
    """Build a Config instance by reading environment variables.

    Loading priority:
    1. System environment variables
    2. .env file
    3. Default values in Config dataclass
    """
    # Ensure env vars are loaded
    setup_env()

    # === Proxy auto-config ===
    http_proxy = os.getenv('HTTP_PROXY') or os.getenv('http_proxy')
    if http_proxy:
        domestic_domains = [
            'eastmoney.com', 'sina.com.cn', '163.com', 'tushare.pro',
            'baostock.com', 'sse.com.cn', 'szse.cn', 'csindex.com.cn',
            'cninfo.com.cn', 'localhost', '127.0.0.1',
        ]
        current_no_proxy = os.getenv('NO_PROXY') or os.getenv('no_proxy') or ''
        existing_domains = current_no_proxy.split(',') if current_no_proxy else []
        final_domains = list(set(existing_domains + domestic_domains))
        final_no_proxy = ','.join(filter(None, final_domains))
        os.environ['NO_PROXY'] = final_no_proxy
        os.environ['no_proxy'] = final_no_proxy
        os.environ['HTTP_PROXY'] = http_proxy
        os.environ['http_proxy'] = http_proxy
        https_proxy = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy')
        if https_proxy:
            os.environ['HTTPS_PROXY'] = https_proxy
            os.environ['https_proxy'] = https_proxy

    # === Stock list (comma-separated, upper-cased Issue #355) ===
    stock_list_str = os.getenv('STOCK_LIST', '')
    stock_list = [
        (c or "").strip().upper()
        for c in stock_list_str.split(',')
        if (c or "").strip()
    ]
    if not stock_list:
        stock_list = ['600519', '000001', '300750']

    # === LiteLLM multi-key parsing (comma-separated in *_API_KEY; legacy *_API_KEYS merged) ===
    gemini_api_keys = _merge_csv_env_keys("GEMINI_API_KEY", "GEMINI_API_KEYS")

    anthropic_api_keys = _merge_csv_env_keys("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS")

    openai_api_keys = _merge_csv_env_keys("OPENAI_API_KEY", "OPENAI_API_KEYS")
    if not openai_api_keys:
        _aihubmix = os.getenv('AIHUBMIX_KEY', '').strip()
        if _aihubmix:
            openai_api_keys = [_aihubmix]

    deepseek_api_keys = _merge_csv_env_keys("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS")

    # LITELLM_MODEL: explicit config takes precedence; else infer from available keys
    litellm_model = os.getenv('LITELLM_MODEL', '').strip()
    if not litellm_model:
        _gemini_model_name = os.getenv('GEMINI_MODEL', 'gemini-3-flash-preview').strip()
        _anthropic_model_name = os.getenv('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022').strip()
        _openai_model_name = os.getenv('OPENAI_MODEL', 'gpt-4o-mini').strip()
        if gemini_api_keys:
            litellm_model = f'gemini/{_gemini_model_name}'
        elif anthropic_api_keys:
            litellm_model = f'anthropic/{_anthropic_model_name}'
        elif deepseek_api_keys:
            litellm_model = 'deepseek/deepseek-chat'
        elif openai_api_keys:
            if '/' not in _openai_model_name:
                litellm_model = f'openai/{_openai_model_name}'
            else:
                litellm_model = _openai_model_name

    # LITELLM_FALLBACK_MODELS
    _fallback_str = os.getenv('LITELLM_FALLBACK_MODELS', '')
    if _fallback_str.strip():
        litellm_fallback_models = [m.strip() for m in _fallback_str.split(',') if m.strip()]
    else:
        litellm_fallback_models = []

    # === LLM Channels + YAML config ===
    litellm_config_path = os.getenv('LITELLM_CONFIG', '').strip() or None
    llm_channels: List[Dict[str, Any]] = []
    llm_model_list: List[Dict[str, Any]] = []

    # Priority 1: LITELLM_CONFIG (standard LiteLLM YAML config file)
    if litellm_config_path:
        llm_model_list = parse_litellm_yaml(litellm_config_path)

    # Priority 2: LLM_CHANNELS (env var based channel config)
    if not llm_model_list:
        _channels_str = os.getenv('LLM_CHANNELS', '').strip()
        if _channels_str:
            llm_channels = parse_llm_channels(_channels_str)
            llm_model_list = channels_to_model_list(llm_channels)

    # Priority 3: Legacy env vars -> auto-build model_list (backward compatible)
    if not llm_model_list:
        llm_model_list = legacy_keys_to_model_list(
            gemini_api_keys, anthropic_api_keys, openai_api_keys,
            os.getenv('OPENAI_BASE_URL') or (
                'https://aihubmix.com/v1' if os.getenv('AIHUBMIX_KEY') else None
            ),
            deepseek_api_keys,
        )

    # Auto-infer LITELLM_MODEL from channels when not explicitly set
    if not litellm_model and llm_channels:
        for _ch in llm_channels:
            if _ch.get('models'):
                litellm_model = _ch['models'][0]
                break

    # Auto-infer LITELLM_FALLBACK_MODELS from channels when not explicitly set
    if not litellm_fallback_models and llm_channels and litellm_model:
        _all_ch_models: List[str] = []
        for _ch in llm_channels:
            _all_ch_models.extend(_ch.get('models', []))
        _seen = {litellm_model}
        litellm_fallback_models = [
            m for m in _all_ch_models
            if m not in _seen and not _seen.add(m)  # type: ignore[func-returns-value]
        ]

    # === Search engine API Keys ===
    bocha_keys_str = os.getenv('BOCHA_API_KEYS', '')
    bocha_api_keys = [k.strip() for k in bocha_keys_str.split(',') if k.strip()]

    minimax_keys_str = os.getenv('MINIMAX_API_KEYS', '')
    minimax_api_keys = [k.strip() for k in minimax_keys_str.split(',') if k.strip()]

    zhipu_keys_str = os.getenv('ZHIPU_API_KEYS', '')
    zhipu_api_keys = [k.strip() for k in zhipu_keys_str.split(',') if k.strip()]

    tavily_keys_str = os.getenv('TAVILY_API_KEYS', '')
    tavily_api_keys = [k.strip() for k in tavily_keys_str.split(',') if k.strip()]

    serpapi_keys_str = os.getenv('SERPAPI_API_KEYS', '')
    serpapi_keys = [k.strip() for k in serpapi_keys_str.split(',') if k.strip()]

    brave_keys_str = os.getenv('BRAVE_API_KEYS', '')
    brave_api_keys = [k.strip() for k in brave_keys_str.split(',') if k.strip()]

    _raw_urls = [u.strip() for u in os.getenv('SEARXNG_BASE_URLS', '').split(',') if u.strip()]
    searxng_base_urls = []
    invalid_searxng_urls = []
    for u in _raw_urls:
        p = urlparse(u)
        if p.scheme in ('http', 'https') and p.netloc:
            searxng_base_urls.append(u)
        else:
            invalid_searxng_urls.append(u)
    if invalid_searxng_urls:
        _logger.warning(
            "SEARXNG_BASE_URLS 中存在无效 URL，已忽略: %s",
            ", ".join(invalid_searxng_urls[:3]),
        )

    # WeChat message type and max bytes logic
    wechat_msg_type = os.getenv('WECHAT_MSG_TYPE', 'markdown')
    wechat_msg_type_lower = wechat_msg_type.lower()
    wechat_max_bytes_env = os.getenv('WECHAT_MAX_BYTES')
    if wechat_max_bytes_env not in (None, ''):
        wechat_max_bytes = int(wechat_max_bytes_env)
    else:
        wechat_max_bytes = 2048 if wechat_msg_type_lower == 'text' else 4000

    return Config(
        stock_list=stock_list,
        feishu_app_id=os.getenv('FEISHU_APP_ID'),
        feishu_app_secret=os.getenv('FEISHU_APP_SECRET'),
        feishu_folder_token=os.getenv('FEISHU_FOLDER_TOKEN'),
        tushare_token=os.getenv('TUSHARE_TOKEN'),
        litellm_model=litellm_model,
        litellm_fallback_models=litellm_fallback_models,
        litellm_config_path=litellm_config_path,
        llm_channels=llm_channels,
        llm_model_list=llm_model_list,
        gemini_api_keys=gemini_api_keys,
        anthropic_api_keys=anthropic_api_keys,
        openai_api_keys=openai_api_keys,
        deepseek_api_keys=deepseek_api_keys,
        gemini_model=os.getenv('GEMINI_MODEL', 'gemini-3-flash-preview'),
        gemini_temperature=float(os.getenv('GEMINI_TEMPERATURE', '0.7')),
        gemini_request_delay=float(os.getenv('GEMINI_REQUEST_DELAY', '2.0')),
        gemini_max_retries=int(os.getenv('GEMINI_MAX_RETRIES', '5')),
        gemini_retry_delay=float(os.getenv('GEMINI_RETRY_DELAY', '5.0')),
        anthropic_api_key=anthropic_api_keys[0] if anthropic_api_keys else None,
        anthropic_model=os.getenv('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022'),
        anthropic_temperature=float(os.getenv('ANTHROPIC_TEMPERATURE', '0.7')),
        anthropic_max_tokens=int(os.getenv('ANTHROPIC_MAX_TOKENS', '8192')),
        openai_api_key=openai_api_keys[0] if openai_api_keys else None,
        openai_base_url=os.getenv('OPENAI_BASE_URL') or (
            'https://aihubmix.com/v1' if os.getenv('AIHUBMIX_KEY') else None
        ),
        openai_model=os.getenv('OPENAI_MODEL', 'gpt-4o-mini'),
        openai_temperature=float(os.getenv('OPENAI_TEMPERATURE', '0.7')),
        vision_model=(os.getenv('VISION_MODEL') or "").strip(),
        vision_provider_priority=os.getenv('VISION_PROVIDER_PRIORITY', 'gemini,anthropic,openai'),
        bocha_api_keys=bocha_api_keys,
        minimax_api_keys=minimax_api_keys,
        zhipu_api_keys=zhipu_api_keys,
        tavily_api_keys=tavily_api_keys,
        brave_api_keys=brave_api_keys,
        serpapi_keys=serpapi_keys,
        searxng_base_urls=searxng_base_urls,
        news_max_age_days=max(1, int(os.getenv('NEWS_MAX_AGE_DAYS', '3'))),
        bias_threshold=_resolve_bias_threshold(),
        agent_mode=os.getenv('AGENT_MODE', 'false').lower() == 'true',
        agent_max_steps=int(os.getenv('AGENT_MAX_STEPS', '10')),
        agent_skills=[s.strip() for s in os.getenv('AGENT_SKILLS', '').split(',') if s.strip()],
        agent_strategy_dir=os.getenv('AGENT_STRATEGY_DIR'),
        wechat_webhook_url=os.getenv('WECHAT_WEBHOOK_URL'),
        feishu_webhook_url=os.getenv('FEISHU_WEBHOOK_URL'),
        telegram_bot_token=os.getenv('TELEGRAM_BOT_TOKEN'),
        telegram_chat_id=os.getenv('TELEGRAM_CHAT_ID'),
        telegram_message_thread_id=os.getenv('TELEGRAM_MESSAGE_THREAD_ID'),
        email_sender=os.getenv('EMAIL_SENDER'),
        email_sender_name=os.getenv('EMAIL_SENDER_NAME', 'stock_analysis股票分析助手'),
        email_password=os.getenv('EMAIL_PASSWORD'),
        email_receivers=[r.strip() for r in os.getenv('EMAIL_RECEIVERS', '').split(',') if r.strip()],
        stock_email_groups=_parse_stock_email_groups(),
        pushover_user_key=os.getenv('PUSHOVER_USER_KEY'),
        pushover_api_token=os.getenv('PUSHOVER_API_TOKEN'),
        pushplus_token=os.getenv('PUSHPLUS_TOKEN'),
        pushplus_topic=os.getenv('PUSHPLUS_TOPIC'),
        serverchan3_sendkey=os.getenv('SERVERCHAN3_SENDKEY'),
        custom_webhook_urls=[u.strip() for u in os.getenv('CUSTOM_WEBHOOK_URLS', '').split(',') if u.strip()],
        custom_webhook_bearer_token=os.getenv('CUSTOM_WEBHOOK_BEARER_TOKEN'),
        discord_bot_token=os.getenv('DISCORD_BOT_TOKEN'),
        discord_main_channel_id=os.getenv('DISCORD_MAIN_CHANNEL_ID'),
        discord_webhook_url=os.getenv('DISCORD_WEBHOOK_URL'),
        astrbot_url=os.getenv('ASTRBOT_URL'),
        astrbot_token=os.getenv('ASTRBOT_TOKEN'),
        report_type=_parse_report_type(os.getenv('REPORT_TYPE', 'simple')),
        report_summary_only=os.getenv('REPORT_SUMMARY_ONLY', 'false').lower() == 'true',
        analysis_delay=float(os.getenv('ANALYSIS_DELAY', '0')),
        feishu_max_bytes=int(os.getenv('FEISHU_MAX_BYTES', '20000')),
        wechat_max_bytes=wechat_max_bytes,
        wechat_msg_type=wechat_msg_type_lower,
        discord_max_words=int(os.getenv('DISCORD_MAX_WORDS', '2000')),
        markdown_to_image_channels=[
            c.strip().lower()
            for c in os.getenv('MARKDOWN_TO_IMAGE_CHANNELS', '').split(',')
            if c.strip()
        ],
        markdown_to_image_max_chars=int(os.getenv('MARKDOWN_TO_IMAGE_MAX_CHARS', '15000')),
        md2img_engine=_parse_md2img_engine(os.getenv('MD2IMG_ENGINE', 'wkhtmltoimage')),
        prefetch_realtime_quotes=os.getenv('PREFETCH_REALTIME_QUOTES', 'true').lower() == 'true',
        database_path=os.getenv('DATABASE_PATH', './data/stock_analysis.db'),
        save_context_snapshot=os.getenv('SAVE_CONTEXT_SNAPSHOT', 'true').lower() == 'true',
        backtest_enabled=os.getenv('BACKTEST_ENABLED', 'true').lower() == 'true',
        backtest_eval_window_days=int(os.getenv('BACKTEST_EVAL_WINDOW_DAYS', '10')),
        backtest_min_age_days=int(os.getenv('BACKTEST_MIN_AGE_DAYS', '14')),
        backtest_neutral_band_pct=float(os.getenv('BACKTEST_NEUTRAL_BAND_PCT', '2.0')),
        picker_strategies=_parse_picker_strategies(os.getenv('PICKER_STRATEGIES', '')),
        picker_mode=_parse_picker_mode(os.getenv('PICKER_MODE', 'balanced')),
        picker_turnover_min=float(os.getenv('PICKER_TURNOVER_MIN', '1.0')),
        picker_turnover_max=float(os.getenv('PICKER_TURNOVER_MAX', '15.0')),
        picker_enable_b_wave_filter=os.getenv('PICKER_ENABLE_B_WAVE_FILTER', 'true').lower() == 'true',
        strategy_auto_reweight=os.getenv('STRATEGY_AUTO_REWEIGHT', 'false').lower() == 'true',
        picker_allow_loss=os.getenv('PICKER_ALLOW_LOSS', 'false').lower() == 'true',
        picker_spot_timeout=int(os.getenv('PICKER_SPOT_TIMEOUT', '30')),
        picker_enable_realtime_filter=os.getenv('PICKER_ENABLE_REALTIME_FILTER', 'true').lower() == 'true',
        picker_realtime_exclude_limit_up=os.getenv(
            'PICKER_REALTIME_EXCLUDE_LIMIT_UP', 'true'
        ).lower() == 'true',
        picker_realtime_exclude_limit_down=os.getenv(
            'PICKER_REALTIME_EXCLUDE_LIMIT_DOWN', 'true'
        ).lower() == 'true',
        picker_realtime_daily_chg_min=(
            float(os.getenv('PICKER_REALTIME_DAILY_CHG_MIN', ''))
            if os.getenv('PICKER_REALTIME_DAILY_CHG_MIN') else None
        ),
        picker_realtime_daily_chg_max=(
            float(os.getenv('PICKER_REALTIME_DAILY_CHG_MAX', ''))
            if os.getenv('PICKER_REALTIME_DAILY_CHG_MAX') else None
        ),
        picker_realtime_max_volume_ratio=float(os.getenv('PICKER_REALTIME_MAX_VOLUME_RATIO', '0.0')),
        picker_market_guard=os.getenv('PICKER_MARKET_GUARD', 'true').lower() == 'true',
        picker_industry_top_n=int(os.getenv('PICKER_INDUSTRY_TOP_N', '2')),
        picker_weak_market_action=os.getenv('PICKER_WEAK_MARKET_ACTION', 'limit').strip().lower(),
        picker_weak_market_strategies=os.getenv('PICKER_WEAK_MARKET_STRATEGIES', 'bottom_reversal').strip(),
        picker_sector_filter=os.getenv('PICKER_SECTOR_FILTER', 'true').lower() == 'true',
        picker_sector_top_pct=int(os.getenv('PICKER_SECTOR_TOP_PCT', '15')),
        log_dir=os.getenv('LOG_DIR', './logs'),
        log_level=os.getenv('LOG_LEVEL', 'INFO'),
        max_workers=int(os.getenv('MAX_WORKERS', '3')),
        config_validate_mode=os.getenv('CONFIG_VALIDATE_MODE', 'warn').lower(),
        http_proxy=os.getenv('HTTP_PROXY'),
        https_proxy=os.getenv('HTTPS_PROXY'),
        schedule_time=os.getenv('SCHEDULE_TIME', '').strip(),
        market_review_region=_parse_market_review_region(
            os.getenv('MARKET_REVIEW_REGION', 'cn')
        ),
        webui_enabled=os.getenv('WEBUI_ENABLED', 'false').lower() == 'true',
        webui_host=os.getenv('WEBUI_HOST', '127.0.0.1'),
        webui_port=int(os.getenv('WEBUI_PORT', '8000')),
        bot_enabled=os.getenv('BOT_ENABLED', 'true').lower() == 'true',
        bot_command_prefix=os.getenv('BOT_COMMAND_PREFIX', '/'),
        bot_rate_limit_requests=int(os.getenv('BOT_RATE_LIMIT_REQUESTS', '10')),
        bot_rate_limit_window=int(os.getenv('BOT_RATE_LIMIT_WINDOW', '60')),
        bot_admin_users=[u.strip() for u in os.getenv('BOT_ADMIN_USERS', '').split(',') if u.strip()],
        feishu_verification_token=os.getenv('FEISHU_VERIFICATION_TOKEN'),
        feishu_encrypt_key=os.getenv('FEISHU_ENCRYPT_KEY'),
        feishu_stream_enabled=os.getenv('FEISHU_STREAM_ENABLED', 'false').lower() == 'true',
        dingtalk_app_key=os.getenv('DINGTALK_APP_KEY'),
        dingtalk_app_secret=os.getenv('DINGTALK_APP_SECRET'),
        dingtalk_stream_enabled=os.getenv('DINGTALK_STREAM_ENABLED', 'false').lower() == 'true',
        wecom_corpid=os.getenv('WECOM_CORPID'),
        wecom_token=os.getenv('WECOM_TOKEN'),
        wecom_encoding_aes_key=os.getenv('WECOM_ENCODING_AES_KEY'),
        wecom_agent_id=os.getenv('WECOM_AGENT_ID'),
        telegram_webhook_secret=os.getenv('TELEGRAM_WEBHOOK_SECRET'),
        discord_bot_status=os.getenv('DISCORD_BOT_STATUS', 'A股智能分析 | /help'),
        enable_chip_distribution=os.getenv('ENABLE_CHIP_DISTRIBUTION', 'true').lower() == 'true',
        enable_eastmoney_patch=os.getenv('ENABLE_EASTMONEY_PATCH', 'false').lower() == 'true',
        use_enhanced_market_review=os.getenv('USE_ENHANCED_MARKET_REVIEW', 'true').lower() == 'true',
        generate_wechat_format=os.getenv('GENERATE_WECHAT_FORMAT', 'true').lower() == 'true',
        wechat_account_name=os.getenv('WECHAT_ACCOUNT_NAME', 'A股智能分析'),
        wechat_slogan=os.getenv('WECHAT_SLOGAN', 'AI驱动的股市复盘，让投资更智能'),
        wechat_qr_code_text=os.getenv('WECHAT_QR_CODE_TEXT', '扫码关注获取每日复盘'),
        wechat_use_emoji=os.getenv('WECHAT_USE_EMOJI', 'true').lower() == 'true',
        wechat_use_dividers=os.getenv('WECHAT_USE_DIVIDERS', 'true').lower() == 'true',
        wechat_add_footer=os.getenv('WECHAT_ADD_FOOTER', 'true').lower() == 'true',
        wechat_max_length=int(os.getenv('WECHAT_MAX_LENGTH', '8000')),
        realtime_source_priority=_resolve_realtime_source_priority(),
        realtime_cache_ttl=int(os.getenv('REALTIME_CACHE_TTL', '600')),
        circuit_breaker_cooldown=int(os.getenv('CIRCUIT_BREAKER_COOLDOWN', '300')),
        tushare_rate_limit_per_minute=int(os.getenv('TUSHARE_RATE_LIMIT_PER_MINUTE', '80')),
    )


if __name__ == "__main__":
    # Quick config loading test
    config = get_config()
    print("=== Config load test ===")
    print(f"Stock list: {config.stock_list}")
    print(f"Database path: {config.database_path}")
    print(f"Max workers: {config.max_workers}")

    warnings = config.validate()
    if warnings:
        print("\nValidation results:")
        for w in warnings:
            print(f"  - {w}")
