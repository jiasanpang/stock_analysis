# -*- coding: utf-8 -*-
"""Config dataclass definition and related base types.

Contains the ``Config`` class (all field definitions), ``ConfigIssue``, and
``setup_env`` which bootstraps the ``.env`` file into ``os.environ``.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from dataclasses import dataclass, field
from dotenv import load_dotenv, dotenv_values


@dataclass
class ConfigIssue:
    """Structured configuration validation issue with a severity level.

    Attributes:
        severity: One of "error", "warning", or "info".
        message:  Human-readable description of the issue.
        field:    The environment variable / config field name most relevant to
                  this issue (empty string when not applicable).
    """

    severity: Literal["error", "warning", "info"]
    message: str
    field: str = ""

    def __str__(self) -> str:  # noqa: D105
        return self.message


def setup_env(override: bool = False):
    """Initialize environment variables from .env file.

    Args:
        override: If True, overwrite existing environment variables with values
                  from .env file. Set to True when reloading config after updates.
                  Default is False to preserve behavior on initial load where
                  system environment variables take precedence.
    """
    # src/config/base.py -> src/config/ -> src/ -> root
    env_file = os.getenv("ENV_FILE")
    if env_file:
        env_path = Path(env_file)
    else:
        env_path = Path(__file__).parent.parent.parent / '.env'
    load_dotenv(dotenv_path=env_path, override=override)


@dataclass
class Config:
    """System configuration class.

    Design notes:
    - Uses dataclass for concise field definitions
    - All fields are loaded from environment variables with sensible defaults
    - Singleton access via get_instance() / get_config()
    """

    # === Stock list ===
    stock_list: List[str] = field(default_factory=list)

    # === Feishu cloud document ===
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    feishu_folder_token: Optional[str] = None

    # === Data source API Token ===
    tushare_token: Optional[str] = None

    # === AI analysis config ===
    # LiteLLM unified model config (provider/model format, e.g. gemini/gemini-2.5-flash)
    litellm_model: str = ""
    litellm_fallback_models: List[str] = field(default_factory=list)

    # --- Multi-channel LLM config ---
    litellm_config_path: Optional[str] = None
    llm_channels: List[Dict[str, Any]] = field(default_factory=list)
    llm_model_list: List[Dict[str, Any]] = field(default_factory=list)

    # Multi-key support
    gemini_api_keys: List[str] = field(default_factory=list)
    anthropic_api_keys: List[str] = field(default_factory=list)
    openai_api_keys: List[str] = field(default_factory=list)
    deepseek_api_keys: List[str] = field(default_factory=list)

    gemini_model: str = "gemini-3-flash-preview"
    gemini_temperature: float = 0.7

    # Gemini API request config (rate-limit prevention)
    gemini_request_delay: float = 2.0
    gemini_max_retries: int = 5
    gemini_retry_delay: float = 5.0

    # Anthropic Claude API
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    anthropic_temperature: float = 0.7
    anthropic_max_tokens: int = 8192

    # OpenAI compatible API
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    openai_temperature: float = 0.7

    # === Vision config ===
    vision_model: str = ""
    vision_provider_priority: str = "gemini,anthropic,openai"

    # === Search engine config (multi-key load balancing) ===
    bocha_api_keys: List[str] = field(default_factory=list)
    minimax_api_keys: List[str] = field(default_factory=list)
    tavily_api_keys: List[str] = field(default_factory=list)
    brave_api_keys: List[str] = field(default_factory=list)
    serpapi_keys: List[str] = field(default_factory=list)
    searxng_base_urls: List[str] = field(default_factory=list)
    zhipu_api_keys: List[str] = field(default_factory=list)

    # === News & analysis filter ===
    news_max_age_days: int = 3
    bias_threshold: float = 5.0

    # === Agent mode ===
    agent_mode: bool = False
    agent_max_steps: int = 10
    agent_skills: List[str] = field(default_factory=list)
    agent_strategy_dir: Optional[str] = None

    # === Notification config ===
    wechat_webhook_url: Optional[str] = None
    feishu_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_message_thread_id: Optional[str] = None
    email_sender: Optional[str] = None
    email_sender_name: str = "stock_analysis股票分析助手"
    email_password: Optional[str] = None
    email_receivers: List[str] = field(default_factory=list)

    # Stock-to-email group routing (Issue #268)
    stock_email_groups: List[Tuple[List[str], List[str]]] = field(default_factory=list)

    # Pushover
    pushover_user_key: Optional[str] = None
    pushover_api_token: Optional[str] = None

    # Custom Webhook
    custom_webhook_urls: List[str] = field(default_factory=list)
    custom_webhook_bearer_token: Optional[str] = None

    # Discord
    discord_bot_token: Optional[str] = None
    discord_main_channel_id: Optional[str] = None
    discord_webhook_url: Optional[str] = None

    # AstrBot
    astrbot_token: Optional[str] = None
    astrbot_url: Optional[str] = None

    # Report type: simple / full / brief
    report_type: str = "simple"
    report_summary_only: bool = False

    # PushPlus
    pushplus_token: Optional[str] = None
    pushplus_topic: Optional[str] = None

    # Server-chan 3
    serverchan3_sendkey: Optional[str] = None

    # Analysis delay (seconds)
    analysis_delay: float = 0.0

    # Message length limits (bytes)
    feishu_max_bytes: int = 20000
    wechat_max_bytes: int = 4000
    discord_max_words: int = 2000
    wechat_msg_type: str = "markdown"

    # Markdown to image (Issue #289)
    markdown_to_image_channels: List[str] = field(default_factory=list)
    markdown_to_image_max_chars: int = 15000
    md2img_engine: str = "wkhtmltoimage"

    # Realtime quote prefetch (Issue #455)
    prefetch_realtime_quotes: bool = True

    # === Database ===
    database_path: str = "./data/stock_analysis.db"
    save_context_snapshot: bool = True

    # === Backtest ===
    backtest_enabled: bool = True
    backtest_eval_window_days: int = 10
    backtest_min_age_days: int = 14
    backtest_neutral_band_pct: float = 2.0

    # === Stock picker ===
    picker_strategies: List[str] = field(default_factory=list)
    picker_mode: str = "balanced"
    picker_turnover_min: float = 1.0
    picker_turnover_max: float = 15.0
    picker_enable_b_wave_filter: bool = True
    strategy_auto_reweight: bool = False
    picker_allow_loss: bool = False
    picker_spot_timeout: int = 30
    picker_enable_realtime_filter: bool = True
    picker_realtime_exclude_limit_up: bool = True
    picker_realtime_exclude_limit_down: bool = True
    picker_realtime_daily_chg_min: Optional[float] = None
    picker_realtime_daily_chg_max: Optional[float] = None
    picker_realtime_max_volume_ratio: float = 0.0

    # Market guard
    picker_market_guard: bool = True
    picker_weak_market_action: str = "limit"
    picker_weak_market_strategies: str = "bottom_reversal"

    # Industry concentration cap
    picker_industry_top_n: int = 2

    # Sector strength filter
    picker_sector_filter: bool = True
    picker_sector_top_pct: int = 15

    # === Logging ===
    log_dir: str = "./logs"
    log_level: str = "INFO"

    # === System ===
    max_workers: int = 3
    http_proxy: Optional[str] = None
    https_proxy: Optional[str] = None

    # === Scheduled tasks ===
    schedule_time: str = ""
    market_review_region: str = "cn"

    # === Realtime quote enhanced data ===
    enable_chip_distribution: bool = True
    enable_eastmoney_patch: bool = False

    # === Enhanced market review ===
    use_enhanced_market_review: bool = True
    generate_wechat_format: bool = True
    wechat_account_name: str = "A股智能分析"
    wechat_slogan: str = "AI驱动的股市复盘，让投资更智能"
    wechat_qr_code_text: str = "扫码关注获取每日复盘"
    wechat_use_emoji: bool = True
    wechat_use_dividers: bool = True
    wechat_add_footer: bool = True
    wechat_max_length: int = 8000

    # Realtime source priority
    realtime_source_priority: str = "tencent,akshare_sina,efinance,akshare_em"
    realtime_cache_ttl: int = 600
    circuit_breaker_cooldown: int = 300

    # Discord bot status
    discord_bot_status: str = "A股智能分析 | /help"

    # === Rate-limit config ===
    akshare_sleep_min: float = 2.0
    akshare_sleep_max: float = 5.0
    tushare_rate_limit_per_minute: int = 80
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0

    # === WebUI ===
    webui_enabled: bool = False
    webui_host: str = "127.0.0.1"
    webui_port: int = 8000

    # === Bot config ===
    bot_enabled: bool = True
    bot_command_prefix: str = "/"
    bot_rate_limit_requests: int = 10
    bot_rate_limit_window: int = 60
    bot_admin_users: List[str] = field(default_factory=list)

    # Feishu bot (event subscription)
    feishu_verification_token: Optional[str] = None
    feishu_encrypt_key: Optional[str] = None
    feishu_stream_enabled: bool = False

    # DingTalk bot
    dingtalk_app_key: Optional[str] = None
    dingtalk_app_secret: Optional[str] = None
    dingtalk_stream_enabled: bool = False

    # WeCom bot (callback mode)
    wecom_corpid: Optional[str] = None
    wecom_token: Optional[str] = None
    wecom_encoding_aes_key: Optional[str] = None
    wecom_agent_id: Optional[str] = None

    # Telegram bot
    telegram_webhook_secret: Optional[str] = None

    # === Config validation mode ===
    config_validate_mode: str = "warn"

    # Singleton instance storage
    _instance: Optional['Config'] = None

    @classmethod
    def get_instance(cls) -> 'Config':
        """Get the singleton Config instance.

        Ensures only one Config is created, loaded once from environment
        variables, and shared across all modules.
        """
        if cls._instance is None:
            from .loader import load_config_from_env
            cls._instance = load_config_from_env()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (primarily for testing)."""
        cls._instance = None

    def refresh_stock_list(self) -> None:
        """Hot-reload STOCK_LIST from the .env file / environment.

        Supports two config sources:
        1. .env file (local dev, scheduled tasks) - changes take effect on next execution
        2. System env vars (GitHub Actions, Docker) - fixed at startup
        """
        env_file = os.getenv("ENV_FILE")
        env_path = Path(env_file) if env_file else (Path(__file__).parent.parent.parent / '.env')
        stock_list_str = ''
        if env_path.exists():
            env_values = dotenv_values(env_path)
            stock_list_str = (env_values.get('STOCK_LIST') or '').strip()

        if not stock_list_str:
            stock_list_str = os.getenv('STOCK_LIST', '')

        stock_list = [
            (c or "").strip().upper()
            for c in stock_list_str.split(',')
            if (c or "").strip()
        ]

        if not stock_list:
            stock_list = ['000001']

        self.stock_list = stock_list

    def validate_structured(self) -> List['ConfigIssue']:
        """Return structured validation issues with severity levels.

        Delegates to ``validators.validate_config_structured()``.
        """
        from .validators import validate_config_structured
        return validate_config_structured(self)

    def validate(self) -> List[str]:
        """Return validation messages as plain strings (backward-compatible).

        Internally delegates to validate_structured().
        """
        return [issue.message for issue in self.validate_structured()]

    def get_db_url(self) -> str:
        """Get SQLAlchemy database connection URL.

        Automatically creates the database directory if it does not exist.
        """
        db_path = Path(self.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path.absolute()}"
