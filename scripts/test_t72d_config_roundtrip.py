"""T7.2-D: End-to-end audit that representative config keys actually persist
to .env AND propagate to the live Config instance after save.

Workflow per key:
  1. Read current Config value (cfg_before)
  2. Read raw .env value (env_before)
  3. PUT new value via SystemConfigService.update(reload_now=True)
  4. Read new Config value (cfg_after) — must equal expected
  5. Read raw .env (env_after) — must contain the new value
  6. Revert .env to env_before to keep the user's environment intact

Run: .venv/bin/python scripts/test_t72d_config_roundtrip.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import get_config  # noqa: E402
from src.services.system_config_service import SystemConfigService  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _read_env_raw(key: str) -> str:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""


def _restore_env_value(svc: SystemConfigService, key: str, original: str) -> None:
    """Best-effort restore — values that fail validation just stay at the test value."""
    try:
        version = svc._manager.get_config_version()
        svc.update(
            config_version=version,
            items=[{"key": key, "value": original}],
            reload_now=True,
        )
    except Exception as exc:
        print(f"  {YELLOW}WARN{RESET} restore {key} failed: {exc}")


def check(
    svc: SystemConfigService,
    key: str,
    new_value: str,
    cfg_attr: str,
    expected_check: Callable[[Any], bool],
    description: str,
) -> bool:
    print(f"  Testing {key} ({description})...")
    cfg = get_config()
    before_attr = getattr(cfg, cfg_attr, "<missing>")
    env_before = _read_env_raw(key)

    try:
        version = svc._manager.get_config_version()
        svc.update(
            config_version=version,
            items=[{"key": key, "value": new_value}],
            reload_now=True,
        )
    except Exception as exc:
        print(f"    {RED}FAIL{RESET} update raised: {exc}")
        return False

    cfg_after = get_config()
    after_attr = getattr(cfg_after, cfg_attr, "<missing>")
    env_after = _read_env_raw(key)

    ok_attr = expected_check(after_attr)
    ok_env = env_after == new_value or new_value in env_after

    status = f"{GREEN}PASS{RESET}" if (ok_attr and ok_env) else f"{RED}FAIL{RESET}"
    print(f"    {status}  cfg.{cfg_attr}: {before_attr!r} -> {after_attr!r}")
    print(f"          .env {key}: {env_before!r} -> {env_after!r}")
    if not ok_attr:
        print(f"          {RED}expected_check failed on cfg attr{RESET}")
    if not ok_env:
        print(f"          {RED}new value not in .env{RESET}")

    # Restore
    _restore_env_value(svc, key, env_before)

    return ok_attr and ok_env


def main() -> int:
    svc = SystemConfigService()

    cases: List[Tuple[str, str, str, Callable[[Any], bool], str]] = [
        # (key, new_value, cfg_attr, expected_check, description)
        (
            "REPORT_SUMMARY_ONLY",
            "true",
            "report_summary_only",
            lambda v: v is True,
            "switch true",
        ),
        (
            "LOG_LEVEL",
            "DEBUG",
            "log_level",
            lambda v: v == "DEBUG",
            "select",
        ),
        (
            "MARKET_REVIEW_REGION",
            "us",
            "market_review_region",
            lambda v: v == "us",
            "select",
        ),
        (
            "REPORT_TYPE",
            "brief",
            "report_type",
            lambda v: v == "brief",
            "select with validation",
        ),
        (
            "BACKTEST_ENABLED",
            "true",
            "backtest_enabled",
            lambda v: v is True,
            "switch true",
        ),
        (
            "TUSHARE_TOKEN",
            "test_token_t72d_audit_12345",
            "tushare_token",
            lambda v: v == "test_token_t72d_audit_12345",
            "string with token",
        ),
        (
            "HTTP_PROXY",
            "http://127.0.0.1:7890",
            "http_proxy",
            lambda v: v == "http://127.0.0.1:7890",
            "string",
        ),
        (
            "MINIMAX_API_KEYS",
            "key_a_t72d,key_b_t72d",
            "minimax_api_keys",
            lambda v: v == ["key_a_t72d", "key_b_t72d"],
            "csv -> list",
        ),
        (
            "ZHIPU_API_KEYS",
            "zhipu_a_t72d,zhipu_b_t72d",
            "zhipu_api_keys",
            lambda v: v == ["zhipu_a_t72d", "zhipu_b_t72d"],
            "csv -> list",
        ),
        (
            "GEMINI_API_KEY",
            "g1_t72d,g2_t72d",
            "gemini_api_keys",
            lambda v: v == ["g1_t72d", "g2_t72d"],
            "single-key field, csv merged into plural list",
        ),
        (
            "BOCHA_API_KEYS",
            "bocha_t72d_aaa,bocha_t72d_bbb",
            "bocha_api_keys",
            lambda v: v == ["bocha_t72d_aaa", "bocha_t72d_bbb"],
            "search source csv",
        ),
        (
            "REALTIME_SOURCE_PRIORITY",
            "tushare,akshare,efinance",
            "realtime_source_priority",
            lambda v: v == ["tushare", "akshare", "efinance"]
            or v == "tushare,akshare,efinance",
            "csv list",
        ),
        (
            "SCHEDULE_TIME",
            "09:30",
            "schedule_time",
            lambda v: v == "09:30",
            "time",
        ),
        (
            "WECHAT_WEBHOOK_URL",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-t72d",
            "wechat_webhook_url",
            lambda v: v
            == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-t72d",
            "webhook URL",
        ),
        (
            "EMAIL_RECEIVERS",
            "a@example.com,b@example.com",
            "email_receivers",
            lambda v: v == ["a@example.com", "b@example.com"]
            or v == "a@example.com,b@example.com",
            "csv emails",
        ),
    ]

    print(f"Running {len(cases)} config roundtrip checks...\n")
    print("Each test: read cfg.attr -> save .env -> reload Config -> verify -> restore.\n")

    results: List[Tuple[str, bool]] = []
    for key, new_value, cfg_attr, expected, desc in cases:
        ok = check(svc, key, new_value, cfg_attr, expected, desc)
        results.append((key, ok))
        print()

    passed = sum(1 for _, ok in results if ok)
    failed = [k for k, ok in results if not ok]
    print("=" * 60)
    print(f"Total: {passed}/{len(results)} passed")
    if failed:
        print(f"{RED}FAILED keys:{RESET} {failed}")
        return 1
    print(f"{GREEN}All config roundtrip checks passed.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
