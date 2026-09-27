"""Claude multi-account priority queue scheduler."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import accounts
import usage

QUEUE_PATH = Path(__file__).parent / ".schedule" / "queue.json"

FAIL_COUNT_WARN_THRESHOLD = 5


@dataclass
class AccountState:
    next_run_at: int | None
    status: str  # "scheduled" | "retry_pending" | "free_skip"
    fail_count: int = 0


def state_to_dict(states: dict[int, AccountState]) -> dict:
    return {str(account_id): asdict(state) for account_id, state in states.items()}


def state_from_dict(raw: dict) -> dict[int, AccountState]:
    return {
        int(account_id): AccountState(**fields)
        for account_id, fields in raw.items()
    }


def load_queue(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_queue(path: Path, raw: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
