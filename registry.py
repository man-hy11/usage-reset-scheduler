"""계정 번호(userN) ↔ 도구(claude/codex) 매핑 레지스트리.

계정 번호는 원래 Claude 전용이었으므로, 레지스트리에 등록되지 않은
account_id는 하위 호환을 위해 기본값 "claude"로 취급한다.
"""
from __future__ import annotations

import json
from pathlib import Path

VALID_TOOLS = {"claude", "codex"}


def load_registry(path: Path) -> dict[int, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}

    result = {}
    for key, value in raw.items():
        try:
            account_id = int(key)
        except (TypeError, ValueError):
            continue
        if value not in VALID_TOOLS:
            continue
        result[account_id] = value
    return result


def save_registry(path: Path, registry: dict[int, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {str(account_id): tool for account_id, tool in registry.items()}
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2))


def get_tool(path: Path, account_id: int) -> str:
    return load_registry(path).get(account_id, "claude")


def set_tool(path: Path, account_id: int, tool: str) -> None:
    if tool not in VALID_TOOLS:
        raise ValueError(f"알 수 없는 도구: {tool!r} (claude 또는 codex만 가능)")
    registry = load_registry(path)
    registry[account_id] = tool
    save_registry(path, registry)
