"""Claude 사용량 조회 및 다음 실행 시각 계산."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

MAX_SLEEP_SECONDS = 691_200  # 8일, collect-usage.sh의 MAX와 동일


def read_claude_token(config_dir: Path) -> str:
    credentials_path = config_dir / ".credentials.json"
    with open(credentials_path) as f:
        credentials = json.load(f)
    return credentials["claudeAiOauth"]["accessToken"]


def read_codex_token(config_dir: Path) -> str:
    auth_path = config_dir / "auth.json"
    with open(auth_path) as f:
        auth = json.load(f)
    return auth["tokens"]["access_token"]


def fetch_claude_usage(config_dir: Path) -> dict:
    token = read_claude_token(config_dir)
    response = requests.get(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
        timeout=20,
    )
    response.raise_for_status()
    raw = response.json()

    five = raw.get("five_hour") or {}
    week = raw.get("seven_day") or {}

    return {
        "tool": "claude",
        "fetched_at": int(time.time()),
        "five_hour_used_percent": five.get("utilization"),
        "five_hour_reset_at": _to_epoch(five.get("resets_at")),
        "weekly_used_percent": week.get("utilization"),
        "weekly_reset_at": _to_epoch(week.get("resets_at")),
    }


def fetch_codex_usage(config_dir: Path) -> dict:
    token = read_codex_token(config_dir)
    response = requests.get(
        "https://chatgpt.com/backend-api/wham/usage",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    response.raise_for_status()
    raw = response.json()

    rate_limit = raw.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}

    return {
        "tool": "codex",
        "fetched_at": int(time.time()),
        "five_hour_used_percent": primary.get("used_percent"),
        "five_hour_reset_at": _to_epoch(primary.get("reset_at")),
        "weekly_used_percent": secondary.get("used_percent"),
        "weekly_reset_at": _to_epoch(secondary.get("reset_at")),
    }


def _to_epoch(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    from datetime import datetime

    return int(datetime.fromisoformat(value).timestamp())


def compute_next_run(
    status: dict | None,
    threshold: float,
    fallback_min: int,
    now: int | None = None,
) -> int:
    if now is None:
        now = int(time.time())
    fallback = fallback_min * 60

    if status is None:
        return fallback

    five_pct = status.get("five_hour_used_percent")
    five_reset = status.get("five_hour_reset_at")
    week_pct = status.get("weekly_used_percent")
    week_reset = status.get("weekly_reset_at")

    if None in (five_pct, five_reset, week_pct, week_reset):
        return fallback

    target = week_reset if week_pct >= threshold else five_reset

    if target <= now:
        return fallback
    if target - now + 60 >= MAX_SLEEP_SECONDS:
        return fallback
    return target - now + 60
