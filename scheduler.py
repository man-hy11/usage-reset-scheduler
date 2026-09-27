"""Claude multi-account priority queue scheduler."""
from __future__ import annotations

import argparse
import dataclasses
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
    five_hour_used_percent: float | None = None
    five_hour_reset_at: int | None = None
    weekly_used_percent: float | None = None
    weekly_reset_at: int | None = None
    email: str | None = None


def state_to_dict(states: dict[int, AccountState]) -> dict:
    return {str(account_id): asdict(state) for account_id, state in states.items()}


def state_from_dict(raw: dict) -> dict[int, AccountState]:
    valid_fields = {f.name for f in dataclasses.fields(AccountState)}
    result: dict[int, AccountState] = {}
    for account_id, fields in raw.items():
        try:
            filtered = {k: v for k, v in fields.items() if k in valid_fields}
            result[int(account_id)] = AccountState(**filtered)
        except (TypeError, ValueError, KeyError):
            continue
    return result


def _format_when(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def format_queue_summary(states: dict[int, AccountState]) -> str:
    header = "==========큐상태============"
    footer = "=" * len(header)
    lines = [header]

    for index, account_id in enumerate(sorted(states), start=1):
        state = states[account_id]
        lines.append(f"{index}. user{account_id}")

        if state.email:
            lines.append(f"  계정: {state.email}")

        if state.status == "free_skip":
            lines.append("  상태: free_skip")
            continue

        if state.status == "retry_pending":
            lines.append(f"  상태: retry_pending (실패 {state.fail_count}회)")
            lines.append(f"  다음 재시도: {_format_when(state.next_run_at)}")
        else:
            lines.append(f"  상태: {state.status}")
            lines.append(f"  다음 실행: {_format_when(state.next_run_at)}")

        if state.five_hour_used_percent is not None and state.five_hour_reset_at is not None:
            lines.append(
                f"  5시간 한도: {state.five_hour_used_percent:g}% (리셋 {_format_when(state.five_hour_reset_at)})"
            )
        if state.weekly_used_percent is not None and state.weekly_reset_at is not None:
            lines.append(
                f"  주간 한도: {state.weekly_used_percent:g}% (리셋 {_format_when(state.weekly_reset_at)})"
            )

    lines.append(footer)
    return "\n".join(lines)


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


def _fetch_usage_fields(account_id: int) -> dict:
    """Fetch current usage-limit fields and login email for account_id.

    Never raises — this is informational display data only and must not
    block startup or flip a gated account's schedule/status. The usage
    lookup and the email lookup fail independently of each other.
    """
    try:
        config_dir = accounts.account_dir(account_id)
        status = usage.fetch_claude_usage(config_dir)
        usage_fields = {
            "five_hour_used_percent": status.get("five_hour_used_percent"),
            "five_hour_reset_at": status.get("five_hour_reset_at"),
            "weekly_used_percent": status.get("weekly_used_percent"),
            "weekly_reset_at": status.get("weekly_reset_at"),
        }
    except Exception as exc:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] 초기 사용량 조회 실패: {exc!r}")
        usage_fields = {
            "five_hour_used_percent": None,
            "five_hour_reset_at": None,
            "weekly_used_percent": None,
            "weekly_reset_at": None,
        }

    usage_fields["email"] = accounts.account_email(account_id)
    return usage_fields


def initialize_states(
    account_ids: list[int],
    wait_until: int | None,
    delay_seconds: int,
    now: int,
    existing_states: dict[int, AccountState] | None = None,
) -> dict[int, AccountState]:
    """Build the starting state map for `account_ids`.

    For any account_id already present in `existing_states` with status
    "scheduled" or "retry_pending", its saved next_run_at/status/fail_count
    are kept as-is (no fresh `check_paid_subscription` call, no schedule
    reset) so a scheduler restart doesn't lose mid-flight progress. Every
    other account_id goes through the normal paid-plan gate check.

    Regardless of which path an account took above, every non-free_skip
    account gets a fresh usage-limit lookup at startup (even if it already
    carried usage fields from a saved queue) so the very first queue-status
    print reflects current data instead of a possibly stale snapshot.
    """
    existing_states = existing_states or {}
    states: dict[int, AccountState] = {}
    start_at = wait_until if wait_until is not None else now + delay_seconds

    for account_id in account_ids:
        saved = existing_states.get(account_id)
        if saved is not None and saved.status in ("scheduled", "retry_pending"):
            state = dataclasses.replace(saved, **_fetch_usage_fields(account_id))
            states[account_id] = state
            continue

        try:
            accounts.check_paid_subscription(account_id)
        except accounts.AccountStatusError:
            states[account_id] = AccountState(next_run_at=None, status="free_skip")
            continue
        states[account_id] = AccountState(
            next_run_at=start_at, status="scheduled", **_fetch_usage_fields(account_id)
        )

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
        except Exception as exc:
            state.fail_count += 1
            state.next_run_at = now + interval_min * 60
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] 사용량 조회 실패: {exc!r}")
            if state.fail_count >= FAIL_COUNT_WARN_THRESHOLD:
                print(f"[account {account_id}] 사용량 조회 {state.fail_count}회 연속 실패, 계속 재시도합니다.")
            continue

        seconds = usage.compute_next_run(status, threshold, fallback_min, now)
        state.status = "scheduled"
        state.fail_count = 0
        state.next_run_at = now + seconds
        state.five_hour_used_percent = status.get("five_hour_used_percent")
        state.five_hour_reset_at = status.get("five_hour_reset_at")
        state.weekly_used_percent = status.get("weekly_used_percent")
        state.weekly_reset_at = status.get("weekly_reset_at")

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
        print(format_queue_summary(states))

        current = states[account_id]
        if current.status != "scheduled" or current.next_run_at != next_run_at:
            for aid, state in states.items():
                if state.status != "free_skip":
                    heapq.heappush(heap, (state.next_run_at, aid))
            if heap:
                sleep_fn(max(0, heap[0][0] - now_fn()))
            continue

        wait_seconds = max(0, current.next_run_at - now_fn())
        sleep_fn(wait_seconds)

        success = run_claude_fn(account_id)
        if not success:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] claude 호출 실패")

        run_after = now_fn()
        email = states[account_id].email
        try:
            config_dir = accounts.account_dir(account_id)
            status = usage.fetch_claude_usage(config_dir)
            seconds = usage.compute_next_run(status, threshold, fallback_min, run_after)
            states[account_id] = AccountState(
                next_run_at=run_after + seconds,
                status="scheduled",
                fail_count=0,
                five_hour_used_percent=status.get("five_hour_used_percent"),
                five_hour_reset_at=status.get("five_hour_reset_at"),
                weekly_used_percent=status.get("weekly_used_percent"),
                weekly_reset_at=status.get("weekly_reset_at"),
                email=email,
            )
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] 사용량 조회 실패: {exc!r}")
            states[account_id] = AccountState(
                next_run_at=run_after + interval_min * 60,
                status="retry_pending",
                fail_count=1,
                email=email,
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
    parser.add_argument("-c", "--check-subscription", action="store_true", default=False)
    parser.add_argument("-a", "--add-account", type=int, default=None)
    parser.add_argument("-l", "--list-accounts", action="store_true", default=False)
    parser.add_argument("-r", "--remove-account", type=int, default=None)

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
        if not isinstance(event, dict) or event.get("type") != "stream_event":
            continue
        inner_event = event.get("event")
        if not isinstance(inner_event, dict):
            continue
        delta = inner_event.get("delta")
        if not isinstance(delta, dict):
            continue
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
        for account_id, status_text, path, email in accounts.list_accounts():
            email_text = email or "-"
            print(f"user{account_id:<4} {status_text:<12} {email_text:<28} {path}")
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
                email = accounts.account_email(account_id)
                email_suffix = f" ({email})" if email else ""
                print(f"[user{account_id}] 유료 구독 확인: {plan}{email_suffix}")
            except accounts.AccountStatusError as exc:
                print(f"[user{account_id}] SKIP: {exc}")
                continue

            try:
                config_dir = accounts.account_dir(account_id)
                status = usage.fetch_claude_usage(config_dir)
            except Exception as exc:
                print(f"[user{account_id}]   사용량 조회 실패: {exc!r}")
                continue

            five_pct = status.get("five_hour_used_percent")
            five_reset = status.get("five_hour_reset_at")
            week_pct = status.get("weekly_used_percent")
            week_reset = status.get("weekly_reset_at")

            if five_pct is not None and five_reset is not None:
                print(f"[user{account_id}]   5시간 한도: {five_pct:g}% 사용, 리셋 {_format_when(five_reset)}")
            if week_pct is not None and week_reset is not None:
                print(f"[user{account_id}]   주간 한도: {week_pct:g}% 사용, 리셋 {_format_when(week_reset)}")
        return 0

    if args.delay < 0 or args.interval < 0:
        print(f"--delay와 --interval은 음수일 수 없습니다 (delay={args.delay}, interval={args.interval})", file=sys.stderr)
        return 1

    now = int(_time.time())
    wait_until_epoch = None
    if args.wait_until:
        try:
            wait_until_epoch = _parse_wait_until(args.wait_until, now)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    existing_raw = load_queue(QUEUE_PATH)
    try:
        existing_states = state_from_dict(existing_raw)
    except (TypeError, KeyError):
        existing_states = {}

    states = initialize_states(
        args.account_ids, wait_until_epoch, args.delay * 60, now, existing_states
    )

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
