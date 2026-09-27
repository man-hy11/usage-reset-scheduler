"""Claude multi-account priority queue scheduler."""
from __future__ import annotations

import argparse
import heapq
import json
import os
import subprocess
import sys
import time as _time
from dataclasses import dataclass, asdict
from datetime import datetime
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("account_ids", nargs="*", type=int, default=None)
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--effort", default="low")
    parser.add_argument("-w", "--wait-until", dest="wait_until", default=None)
    parser.add_argument("-d", "--delay", type=int, default=0)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--threshold", type=int, default=100)
    parser.add_argument("--check-subscription", action="store_true", default=False)
    parser.add_argument("--add-account", type=int, default=None)
    parser.add_argument("--list-accounts", action="store_true", default=False)
    parser.add_argument("--remove-account", type=int, default=None)

    args = parser.parse_args(argv)
    if not args.account_ids:
        args.account_ids = [1]
    else:
        seen = []
        for account_id in args.account_ids:
            if account_id not in seen:
                seen.append(account_id)
        args.account_ids = seen
    return args


PROMPT_TEXT = "Reply with OK."


def build_claude_command(model: str, effort: str, prompt: str) -> list[str]:
    return [
        "claude",
        "--dangerously-skip-permissions",
        "--model", model,
        "--effort", effort,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        prompt,
    ]


def log_dir_for_account(account_id: int) -> Path:
    config_dir = accounts.account_dir(account_id)
    log_dir = config_dir / "start-limit-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def run_claude(account_id: int, model: str, effort: str) -> bool:
    env = os.environ.copy()
    if account_id == 1:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(account_id))

    cmd = build_claude_command(model, effort, PROMPT_TEXT)
    completed = subprocess.run(cmd, env=env, capture_output=True, text=True)

    text_parts = []
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "stream_event":
            continue
        delta = (event.get("event") or {}).get("delta") or {}
        if delta.get("type") == "text_delta":
            text_parts.append(delta.get("text", ""))

    output_text = "".join(text_parts)

    log_dir = log_dir_for_account(account_id)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = log_dir / f"loop-{timestamp}.log"
    log_file.write_text(output_text)

    print(output_text)
    return completed.returncode == 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Once the scheduler loop starts, it runs until interrupted
    (KeyboardInterrupt/signal) — it does not exit naturally during normal operation."""
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    if args.add_account is not None:
        accounts.add_account(args.add_account)
        return 0

    if args.list_accounts:
        for account_id, status_text, path in accounts.list_accounts():
            print(f"user{account_id:<4} {status_text:<12} {path}")
        return 0

    if args.remove_account is not None:
        try:
            backup = accounts.remove_account(args.remove_account)
        except (ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"user{args.remove_account} 제거 완료 (복구 가능): {backup}")
        return 0

    if args.check_subscription:
        for account_id in args.account_ids:
            try:
                plan = accounts.check_paid_subscription(account_id)
                print(f"[user{account_id}] 유료 구독 확인: {plan}")
            except accounts.AccountStatusError as exc:
                print(f"[user{account_id}] SKIP: {exc}")
        return 0

    now = int(_time.time())
    wait_until_epoch = None
    if args.wait_until:
        try:
            wait_until_epoch = _parse_wait_until(args.wait_until, now)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    states = initialize_states(args.account_ids, wait_until_epoch, args.delay * 60, now)

    def run_claude_fn(account_id: int) -> bool:
        return run_claude(account_id, args.model, args.effort)

    run_scheduler_loop(
        states,
        interval_min=args.interval,
        threshold=args.threshold,
        fallback_min=args.interval,
        run_claude_fn=run_claude_fn,
        sleep_fn=_time.sleep,
        now_fn=lambda: int(_time.time()),
        queue_path=QUEUE_PATH,
    )
    return 0


def _parse_wait_until(text: str, now: int) -> int:
    now_dt = datetime.fromtimestamp(now)
    parsed = None
    for fmt in ("%H:%M", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            parsed = parsed.replace(year=now_dt.year, month=now_dt.month, day=now_dt.day)
        break

    if parsed is None:
        raise ValueError(f"잘못된 --wait-until 형식입니다: '{text}'")

    target_epoch = int(parsed.timestamp())
    if target_epoch <= now:
        raise ValueError(f"지정한 시각이 이미 과거입니다: '{text}' (현재: {now_dt:%Y-%m-%d %H:%M:%S})")

    return target_epoch


if __name__ == "__main__":
    sys.exit(main())
