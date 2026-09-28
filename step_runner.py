"""Run one project step with a claude/codex account and interpret its output."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

CONTINUE_STATUSES = ("COMPLETE", "FAILURE_ANALYSIS", "FAILURE_IMPROVEMENT")
_STEP_STATUS_RE = re.compile(
    r"^STEP_STATUS: (COMPLETE|FAILURE_ANALYSIS|FAILURE_IMPROVEMENT|INCOMPLETE)$",
    re.MULTILINE,
)
_CLAUDE_ACCOUNT_ERRORS = ("authentication_failed", "oauth_org_not_allowed")
_CODEX_AUTH_MARKERS = ("sign in again", "log out")


@dataclass
class StepOutcome:
    step_status: str | None
    limit_hit: bool = False
    limit_reset_at: int | None = None
    account_error: str | None = None
    exit_code: int = 0
    log_path: Path | None = None


def find_step_status(text: str) -> str | None:
    matches = _STEP_STATUS_RE.findall(text)
    return matches[-1] if matches else None


def _load_event(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


class ClaudeEventReader:
    """Consumes `claude -p --output-format stream-json` lines one at a time."""

    def __init__(self) -> None:
        self.limit_hit = False
        self.limit_reset_at: int | None = None
        self.account_error: str | None = None

    def feed(self, line: str) -> str:
        event = _load_event(line)
        if event is None:
            return ""
        kind = event.get("type")
        if kind == "stream_event":
            inner = event.get("event")
            delta = inner.get("delta") if isinstance(inner, dict) else None
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return str(delta.get("text", ""))
        elif kind == "rate_limit_event":
            # 정상 실행에도 status가 "allowed"/"allowed_warning"인 이벤트가 오므로 rejected만 본다.
            info = event.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                self.limit_hit = True
                resets_at = info.get("resetsAt")
                if isinstance(resets_at, (int, float)):
                    self.limit_reset_at = int(resets_at)
        elif kind == "assistant":
            error = event.get("error")
            if error == "rate_limit":
                self.limit_hit = True
            elif error in _CLAUDE_ACCOUNT_ERRORS:
                self.account_error = error
        elif kind == "result" and event.get("api_error_status") == 429:
            self.limit_hit = True
        return ""


class CodexEventReader:
    """Consumes `codex exec --json` lines one at a time."""

    def __init__(self) -> None:
        self.limit_hit = False
        self.limit_reset_at: int | None = None  # codex는 "try again at 2:57 PM"처럼 시각만 알려 준다
        self.account_error: str | None = None

    def feed(self, line: str) -> str:
        event = _load_event(line)
        if event is None:
            return ""
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                # 메시지마다 줄을 끊어야 마지막 메시지의 STEP_STATUS 줄이 ^...$에 걸린다.
                return item["text"] + "\n"
            return ""
        message = None
        if kind == "error":
            message = event.get("message")
        elif kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str):
            # 문구의 아포스트로피가 U+2019라서 문장 전체가 아니라 핵심 단어로 판정한다.
            lowered = message.lower()
            if "usage limit" in lowered:
                self.limit_hit = True
            elif any(marker in lowered for marker in _CODEX_AUTH_MARKERS):
                self.account_error = "codex_auth"
        return ""
