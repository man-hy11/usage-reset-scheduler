"""Claude multi-account priority queue scheduler."""
from __future__ import annotations

import heapq
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


def initialize_states(
    account_ids: list[int],
    wait_until: int | None,
    delay_seconds: int,
    now: int,
) -> dict[int, AccountState]:
    states: dict[int, AccountState] = {}
    start_at = wait_until if wait_until is not None else now + delay_seconds

    for account_id in account_ids:
        try:
            accounts.check_paid_subscription(account_id)
        except accounts.AccountStatusError:
            states[account_id] = AccountState(next_run_at=None, status="free_skip")
            continue
        states[account_id] = AccountState(next_run_at=start_at, status="scheduled")

    return states


def retry_pending_accounts(
    states: dict[int, AccountState],
    interval_min: int,
    threshold: float,
    fallback_min: int,
    now: int,
) -> dict[int, AccountState]:
    for account_id, state in states.items():
        if state.status != "retry_pending":
            continue

        try:
            config_dir = accounts.account_dir(account_id)
            status = usage.fetch_claude_usage(config_dir)
        except Exception:
            state.fail_count += 1
            state.next_run_at = now + interval_min * 60
            if state.fail_count >= FAIL_COUNT_WARN_THRESHOLD:
                print(f"[account {account_id}] 사용량 조회 {state.fail_count}회 연속 실패, 계속 재시도합니다.")
            continue

        seconds = usage.compute_next_run(status, threshold, fallback_min, now)
        state.status = "scheduled"
        state.fail_count = 0
        state.next_run_at = now + seconds

    return states


def run_scheduler_loop(
    states: dict[int, AccountState],
    *,
    interval_min: int,
    threshold: float,
    fallback_min: int,
    run_claude_fn,
    sleep_fn,
    now_fn,
    queue_path: Path,
) -> None:
    def rebuild_heap():
        return [
            (state.next_run_at, account_id)
            for account_id, state in states.items()
            if state.status != "free_skip"
        ]

    heap = rebuild_heap()
    heapq.heapify(heap)

    while heap:
        next_run_at, account_id = heapq.heappop(heap)

        if states[account_id].status == "free_skip":
            continue
        if states[account_id].next_run_at != next_run_at:
            # 재시도 처리 등으로 이미 갱신된 오래된 힙 항목이므로 버린다.
            continue

        now = now_fn()
        retry_pending_accounts(states, interval_min, threshold, fallback_min, now)
        save_queue(queue_path, state_to_dict(states))

        current = states[account_id]
        if current.status != "scheduled" or current.next_run_at != next_run_at:
            for aid, state in states.items():
                if state.status != "free_skip":
                    heapq.heappush(heap, (state.next_run_at, aid))
            continue

        wait_seconds = max(0, current.next_run_at - now_fn())
        sleep_fn(wait_seconds)

        run_claude_fn(account_id)

        run_after = now_fn()
        try:
            config_dir = accounts.account_dir(account_id)
            status = usage.fetch_claude_usage(config_dir)
            seconds = usage.compute_next_run(status, threshold, fallback_min, run_after)
            states[account_id] = AccountState(
                next_run_at=run_after + seconds, status="scheduled", fail_count=0
            )
        except Exception:
            states[account_id] = AccountState(
                next_run_at=run_after + interval_min * 60,
                status="retry_pending",
                fail_count=1,
            )

        save_queue(queue_path, state_to_dict(states))

        for aid, state in states.items():
            if state.status != "free_skip":
                heapq.heappush(heap, (state.next_run_at, aid))
