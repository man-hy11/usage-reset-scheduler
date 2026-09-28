"""Claude multi-account priority queue scheduler."""
from __future__ import annotations

import argparse
import dataclasses
import heapq
import json
import os
import re
import subprocess
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import accounts
import registry
import usage

QUEUE_PATH = Path(__file__).parent / ".schedule" / "queue.json"
REGISTRY_PATH = Path(__file__).parent / ".schedule" / "accounts.json"

FAIL_COUNT_WARN_THRESHOLD = 5
# free_skip 계정의 유료 전환(free -> paid) 여부를 다시 확인하는 주기(분).
PLAN_RECHECK_MIN = 60


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
    plan: str | None = None  # 예: "pro", "max", "plus" — check_paid_subscription 결과
    tool: str = "claude"  # "claude" | "codex"
    last_run_at: int | None = None  # 마지막 실행을 시작(dispatch)한 시각


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


def _queue_order_key(item: tuple[int, AccountState]) -> tuple[int, int, int]:
    """다음 실행 예정 순서로 정렬하는 키. free_skip은 (구독 재확인 시각이
    있어도) 맨 뒤로, 같은 next_run_at끼리는 account_id 오름차순으로 묶는다."""
    account_id, state = item
    if state.status == "free_skip" or state.next_run_at is None:
        return (1, state.next_run_at or 0, account_id)
    return (0, state.next_run_at, account_id)


def _append_last_run_line(lines: list[str], state: AccountState) -> None:
    if state.last_run_at is not None:
        lines.append(f"  최근 실행: {_format_when(state.last_run_at)}")


def format_queue_summary(states: dict[int, AccountState]) -> str:
    header = "==========큐상태============"
    footer = "=" * len(header)
    lines = [header]

    ordered_ids = [account_id for account_id, _ in sorted(states.items(), key=_queue_order_key)]
    for index, account_id in enumerate(ordered_ids, start=1):
        state = states[account_id]
        lines.append(f"{index}. user{account_id} [{state.tool}]")

        if state.email:
            plan_suffix = f"({state.plan})" if state.plan else ""
            lines.append(f"  계정: {state.email}{plan_suffix}")

        if state.status == "free_skip":
            lines.append("  상태: free_skip")
            _append_last_run_line(lines, state)
            if state.next_run_at is not None:
                lines.append(f"  다음 구독 확인: {_format_when(state.next_run_at)}")
            continue

        if state.status == "retry_pending":
            lines.append(f"  상태: retry_pending (실패 {state.fail_count}회)")
            _append_last_run_line(lines, state)
            lines.append(f"  다음 재시도: {_format_when(state.next_run_at)}")
        else:
            lines.append(f"  상태: {state.status}")
            _append_last_run_line(lines, state)
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


def fetch_usage(account_id: int, tool: str) -> dict:
    """Dispatch to the usage-fetch function matching `tool`."""
    config_dir = accounts.account_dir(account_id, tool)
    if tool == "codex":
        return usage.fetch_codex_usage(config_dir)
    return usage.fetch_claude_usage(config_dir)


def _fetch_usage_fields(account_id: int, tool: str = "claude", plan: str | None = None) -> dict:
    """Fetch current usage-limit fields and login email for account_id.

    `plan` is the already-verified result of `check_paid_subscription`; it
    is passed through as-is so the (network-bound) plan check isn't repeated.

    Never raises — this is informational display data only and must not
    block startup or flip a gated account's schedule/status. The usage
    lookup and the email/plan lookup fail independently of each other.
    """
    try:
        status = fetch_usage(account_id, tool)
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

    usage_fields["email"] = accounts.account_email(account_id, tool)
    usage_fields["plan"] = plan
    return usage_fields


def _check_plan(account_id: int, tool: str) -> str | None:
    """Paid plan name, or None (with a log line) when the account isn't paid."""
    try:
        return accounts.check_paid_subscription(account_id, tool)
    except accounts.AccountStatusError as exc:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] 유료 구독 아님: {exc}")
        return None


def select_paid_account_ids(count: int, get_tool, candidate_ids: list[int]) -> tuple[list[int], dict[int, str]]:
    """`candidate_ids`를 오름차순으로 훑으며, 유료 구독인 계정만 최대 `count`개 고른다.

    `--count`/`-n`용 선택 로직. 각 계정을 실시간으로 `check_paid_subscription`
    확인하므로(캐시된 queue.json 상태는 보지 않음 — `initialize_states`와 동일한
    이유, docstring 참고) 계정 수만큼 API 호출이 들어갈 수 있다.

    한 번에 하나씩 순서대로 확인하면(앞쪽에 free 계정이 몰려 있을 때) 지연이
    누적되므로, `count`개씩 배치로 묶어 배치 내에서는 동시에 확인한다. 한
    배치에서 paid 계정이 `count`개를 못 채우면 다음 배치(다음 `count`개
    후보)로 넘어간다 — "필요한 만큼만 확인하고 멈춘다"는 성격은 배치 단위로
    유지되므로, 앞쪽에서 다 채워지면 뒤쪽 후보는 아예 확인하지 않는다.

    선택된 계정의 plan도 함께 반환해서, 뒤이은 `initialize_states` 호출이
    같은 계정을 또 live-check하지 않도록 한다(`known_plans` 참고).
    """
    ordered_candidates = sorted(candidate_ids)
    selected: list[int] = []
    plans: dict[int, str] = {}

    for batch_start in range(0, len(ordered_candidates), count):
        if len(selected) >= count:
            break
        batch = ordered_candidates[batch_start:batch_start + count]

        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {
                account_id: executor.submit(_check_plan, account_id, get_tool(account_id))
                for account_id in batch
            }
            batch_plans = {account_id: future.result() for account_id, future in futures.items()}

        for account_id in batch:
            if len(selected) >= count:
                break
            plan = batch_plans[account_id]
            if plan is None:
                continue
            selected.append(account_id)
            plans[account_id] = plan

    return selected, plans


def initialize_states(
    account_ids: list[int],
    wait_until: int | None,
    delay_seconds: int,
    now: int,
    threshold: float,
    fallback_min: int,
    get_tool=None,
    plan_recheck_seconds: int = PLAN_RECHECK_MIN * 60,
    known_plans: dict[int, str] | None = None,
) -> dict[int, AccountState]:
    """Build the starting state map for `account_ids` from scratch.

    Every script invocation (as opposed to a loop iteration within one
    already-running process) re-derives everything live: paid-plan gate,
    email, plan, usage-limit fields, and next_run_at. A previously saved
    queue.json is never consulted here — it exists only so the *running*
    loop can recover its own state after `save_queue` (see
    `run_scheduler_loop`), not to seed a new invocation. This avoids the
    account-N-changed-hands bug where a stale next_run_at from a different
    login (e.g. a 4-day weekly-reset wait) got attached to a fresh usage
    snapshot that no longer matched it.

    An account that fails the paid-plan gate becomes "free_skip" with
    next_run_at set to the next plan re-check (now + plan_recheck_seconds).

    For every other account, next_run_at is:
    - `wait_until`, if given (explicit override), else
    - `now + delay_seconds`, if `delay_seconds` > 0 (explicit override), else
    - derived from the freshly fetched usage-limit fields via
      `usage.compute_next_run(usage_fields, threshold, fallback_min, now)` —
      the same reset-time-based calculation the loop itself uses after each
      run, so the first schedule already reflects the account's real
      current usage instead of "start after a fixed delay".

    get_tool: optional callable(account_id) -> "claude"|"codex" (normally
    `functools.partial(registry.get_tool, REGISTRY_PATH)`). Defaults to
    always "claude" when omitted.

    known_plans: optional {account_id: plan} for accounts whose paid-plan
    status was already live-checked moments ago (e.g. by
    `select_paid_account_ids`), to skip a redundant `_check_plan` call.
    Accounts not present in this dict are still checked normally.
    """
    if get_tool is None:
        get_tool = lambda _account_id: "claude"
    if known_plans is None:
        known_plans = {}

    states: dict[int, AccountState] = {}

    for account_id in account_ids:
        tool = get_tool(account_id)
        last_run_at = last_logged_run_at(account_id, tool)
        plan = known_plans[account_id] if account_id in known_plans else _check_plan(account_id, tool)
        if plan is None:
            states[account_id] = AccountState(
                next_run_at=now + plan_recheck_seconds,
                status="free_skip",
                tool=tool,
                last_run_at=last_run_at,
            )
            continue

        usage_fields = _fetch_usage_fields(account_id, tool, plan)

        if wait_until is not None:
            next_run_at = wait_until
        elif delay_seconds > 0:
            next_run_at = now + delay_seconds
        else:
            next_run_at = now + usage.compute_next_run(usage_fields, threshold, fallback_min, now)

        states[account_id] = AccountState(
            next_run_at=next_run_at,
            status="scheduled",
            tool=tool,
            last_run_at=last_run_at,
            **usage_fields,
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
            status = fetch_usage(account_id, state.tool)
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
    plan_recheck_min: int = PLAN_RECHECK_MIN,
) -> None:
    """Run accounts in next_run_at order.

    free_skip accounts stay in the heap too: their next_run_at is the next
    plan re-check, and they are promoted to "scheduled" once they turn paid.
    Scheduled accounts are re-checked right before each run and demoted to
    free_skip if they are no longer paid.
    """

    def push_all():
        for aid, state in states.items():
            if state.next_run_at is not None:
                heapq.heappush(heap, (state.next_run_at, aid))

    heap = []
    push_all()

    while heap:
        next_run_at, account_id = heapq.heappop(heap)

        if states[account_id].next_run_at != next_run_at:
            # 재시도 처리 등으로 이미 갱신된 오래된 힙 항목이므로 버린다.
            continue

        now = now_fn()
        retry_pending_accounts(states, interval_min, threshold, fallback_min, now)
        save_queue(queue_path, state_to_dict(states))
        print(format_queue_summary(states))

        current = states[account_id]
        if current.status == "retry_pending" or current.next_run_at != next_run_at:
            push_all()
            if heap:
                sleep_fn(max(0, heap[0][0] - now_fn()))
            continue

        wait_seconds = max(0, current.next_run_at - now_fn())
        sleep_fn(wait_seconds)

        plan = _check_plan(account_id, current.tool)
        checked_at = now_fn()
        if plan is None:
            if current.status == "scheduled":
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] 무료 전환 감지 → free_skip")
            states[account_id] = AccountState(
                next_run_at=checked_at + plan_recheck_min * 60,
                status="free_skip",
                tool=current.tool,
                last_run_at=current.last_run_at,
            )
            save_queue(queue_path, state_to_dict(states))
            push_all()
            continue
        if current.status == "free_skip":
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] 유료 전환 감지({plan}) → 바로 실행")
            states[account_id] = AccountState(
                next_run_at=checked_at,
                status="scheduled",
                tool=current.tool,
                last_run_at=current.last_run_at,
                **_fetch_usage_fields(account_id, current.tool, plan),
            )
            save_queue(queue_path, state_to_dict(states))
            push_all()
            continue
        current.plan = plan

        # 몇 시에 실행을 *시작*했나가 목적이므로 완료 시각이 아닌 dispatch 시각을 남긴다.
        # 로컬 변수로 직접 들고 있는다 — states[account_id]는 run_claude_fn 실행 중
        # 다른 경로(다중 스레드/향후 병렬화 등)로 갱신될 수 있어, 굳이 객체에
        # 썼다가 되읽는 것보다 값을 그대로 들고 있는 편이 더 안전하다.
        last_run_at = checked_at
        success = run_claude_fn(account_id)
        if not success:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] claude 호출 실패")

        run_after = now_fn()
        tool = states[account_id].tool
        email = states[account_id].email
        plan = states[account_id].plan
        try:
            status = fetch_usage(account_id, tool)
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
                plan=plan,
                tool=tool,
                last_run_at=last_run_at,
            )
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [account {account_id}] 사용량 조회 실패: {exc!r}")
            states[account_id] = AccountState(
                next_run_at=run_after + interval_min * 60,
                status="retry_pending",
                fail_count=1,
                email=email,
                plan=plan,
                tool=tool,
                last_run_at=last_run_at,
            )

        save_queue(queue_path, state_to_dict(states))

        push_all()


DEFAULT_MODEL = {"claude": "claude-haiku-4-5", "codex": "gpt-6-luna"}
DEFAULT_EFFORT = {"claude": "low", "codex": "low"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("account_ids", nargs="*", type=int, default=None)
    parser.add_argument("--model", default=None, help="지정하지 않으면 계정별 도구(claude/codex)의 기본 모델을 사용합니다.")
    parser.add_argument("--effort", default=None, help="지정하지 않으면 계정별 도구(claude/codex)의 기본 effort를 사용합니다.")
    parser.add_argument("-w", "--wait-until", dest="wait_until", default=None)
    parser.add_argument("-d", "--delay", type=int, default=0)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--threshold", type=int, default=100)
    parser.add_argument("-c", "--check-subscription", action="store_true", default=False)
    parser.add_argument("-a", "--add-account", type=int, default=None)
    parser.add_argument("-l", "--list-accounts", action="store_true", default=False)
    parser.add_argument("-r", "--remove-account", type=int, default=None)
    parser.add_argument(
        "-n", "--count", type=int, default=None,
        help="계정 번호를 나열하는 대신, 등록된 계정 중 유료 구독인 계정을 1번부터 순서대로 최대 N개 자동 선택합니다. "
             "명시적 account_ids와 함께 쓸 수 없습니다.",
    )
    parser.add_argument("--project-loop", action="store_true", default=False,
                        help="여러 계정을 번갈아 쓰며 --project-dir의 프롬프트를 반복 실행합니다.")
    parser.add_argument("--project-dir", default=None, help="--project-loop 대상 프로젝트 (git 저장소)")
    parser.add_argument("--prompt-file", default=None, help="--project-dir 기준 프롬프트 파일. 기본값: prompt.md")

    args = parser.parse_args(argv)

    if args.count is not None:
        if args.account_ids:
            parser.error("account_ids와 --count/-n은 함께 쓸 수 없습니다.")
        if args.count <= 0:
            parser.error("--count/-n은 1 이상이어야 합니다.")
        args.account_ids = []
        return args

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
        # 추가 계정은 ~/.claude의 MCP 설정을 공유하므로, 한도 리셋용 짧은
        # 호출에서 MCP 서버들이 매번 기동되지 않도록 막는다.
        "--strict-mcp-config",
        "--model", model,
        "--effort", effort,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        prompt,
    ]


def build_codex_command(model: str, effort: str, prompt: str) -> list[str]:
    return [
        "codex", "exec",
        "--json",
        "--sandbox", "danger-full-access",
        "--model", model,
        "-c", f"model_reasoning_effort={effort}",
        prompt,
    ]


def log_dir_for_account(account_id: int, tool: str = "claude") -> Path:
    config_dir = accounts.account_dir(account_id, tool)
    log_dir = config_dir / "start-limit-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


_RUN_LOG_NAME_RE = re.compile(r"^loop-\d{8}-\d{6}\.log$")


def last_logged_run_at(account_id: int, tool: str = "claude") -> int | None:
    """가장 최근 실행 로그의 시각. 실행 기록이 없으면 None.

    로그 파일명에 실행 시각이 들어 있으므로 mtime 대신 파일명을 본다. 로그를
    백업했다가 되돌리면 mtime은 복사 시각으로 덮어써지기 때문이다.

    `loop-YYYYMMDD-HHMMSS.log` 형식은(정규식으로 그 형식만 남긴 뒤에는) 사전순
    정렬이 곧 시간순 정렬이므로, 로그가 아무리 쌓여도(로테이션 없음) 파일명마다
    datetime.strptime을 돌릴 필요 없이 가장 큰 이름 하나만 골라 그것만 파싱한다.
    """
    log_dir = accounts.account_dir(account_id, tool) / "start-limit-runs"
    try:
        names = os.listdir(log_dir)
    except OSError:
        return None

    candidates = [name for name in names if _RUN_LOG_NAME_RE.match(name)]
    if not candidates:
        return None

    latest_name = max(candidates)
    parsed = datetime.strptime(latest_name[5:-4], "%Y%m%d-%H%M%S")
    return int(parsed.timestamp())


def _extract_claude_text(stdout: str) -> str:
    text_parts = []
    for line in stdout.splitlines():
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
    return "".join(text_parts)


def _extract_codex_text(stdout: str) -> str:
    text_parts = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            text_parts.append(text)
    return "".join(text_parts)


def _run_and_log(account_id: int, tool: str, cmd: list[str], env: dict, extract_text) -> bool:
    # last_logged_run_at()이 이 파일명에서 dispatch 시각을 복원하므로, 완료 후가
    # 아니라 subprocess.run() 이전에 타임스탬프를 찍는다.
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    completed = subprocess.run(cmd, env=env, capture_output=True, text=True)
    output_text = extract_text(completed.stdout)

    log_dir = log_dir_for_account(account_id, tool)
    log_file = log_dir / f"loop-{timestamp}.log"
    log_file.write_text(output_text)

    print(output_text)
    return completed.returncode == 0


def run_claude(account_id: int, model: str, effort: str) -> bool:
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(account_id))

    cmd = build_claude_command(model, effort, PROMPT_TEXT)
    return _run_and_log(account_id, "claude", cmd, env, _extract_claude_text)


def run_codex(account_id: int, model: str, effort: str) -> bool:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(accounts.account_dir(account_id, "codex"))

    cmd = build_codex_command(model, effort, PROMPT_TEXT)
    return _run_and_log(account_id, "codex", cmd, env, _extract_codex_text)


def run_tool(account_id: int, tool: str, model: str, effort: str) -> bool:
    if tool == "codex":
        return run_codex(account_id, model, effort)
    return run_claude(account_id, model, effort)


def prompt_for_tool(input_fn=input) -> str:
    """Interactively ask which tool a newly-registered account uses.

    Enter (empty input) or "1" selects claude; "2" selects codex. Any other
    input re-prompts.
    """
    print("사용할 도구를 선택하세요:")
    print("1. claude (기본값)")
    print("2. codex")
    while True:
        choice = input_fn("입력 (Enter=1): ").strip()
        if choice in ("", "1"):
            return "claude"
        if choice == "2":
            return "codex"
        print(f"알 수 없는 입력입니다: {choice!r} (1 또는 2를 입력하세요)")


def _share_claude_config_all() -> None:
    """Keep every added Claude account in sync with ~/.claude (best effort)."""
    try:
        for account_id, linked in accounts.share_claude_config_all().items():
            if linked:
                print(f"[account {account_id}] ~/.claude 설정 공유 연결: {', '.join(linked)}")
    except OSError as exc:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ~/.claude 설정 공유 실패: {exc!r}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Once the scheduler loop starts, it runs until interrupted
    (KeyboardInterrupt/signal) — it does not exit naturally during normal operation."""
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    def get_tool(account_id: int) -> str:
        return registry.get_tool(REGISTRY_PATH, account_id)

    management_flags = (
        args.add_account is not None
        or args.list_accounts
        or args.remove_account is not None
        or args.check_subscription
    )
    if args.project_loop or args.project_dir is not None or args.prompt_file is not None:
        return _run_project_loop_mode(args, get_tool, management_flags)
    if args.count is not None and management_flags:
        print(
            "--count/-n은 --add-account/--list-accounts/--remove-account/"
            "--check-subscription과 함께 쓸 수 없습니다.",
            file=sys.stderr,
        )
        return 1

    known_plans: dict[int, str] = {}
    if args.count is not None:
        candidate_ids = list(accounts.known_account_ids())
        args.account_ids, known_plans = select_paid_account_ids(args.count, get_tool, candidate_ids)
        if not args.account_ids:
            print("--count/-n: 유료 구독인 계정이 없습니다.", file=sys.stderr)
            return 1
        if len(args.account_ids) < args.count:
            print(
                f"--count/-n: 유료 구독 계정 {len(args.account_ids)}개만 찾았습니다 "
                f"(요청 {args.count}개, 전체 계정 {len(candidate_ids)}개): "
                + ", ".join(f"user{i}" for i in args.account_ids)
            )

    if args.add_account is not None:
        tool = prompt_for_tool()
        registry.set_tool(REGISTRY_PATH, args.add_account, tool)
        accounts.add_account(args.add_account, tool)
        return 0

    if args.list_accounts:
        for account_id, status_text, path, email, tool in accounts.list_accounts(get_tool):
            email_text = email or "-"
            print(f"user{account_id:<4} [{tool:<6}] {status_text:<12} {email_text:<28} {path}")
        return 0

    if args.remove_account is not None:
        try:
            backup = accounts.remove_account(args.remove_account, get_tool(args.remove_account))
        except (ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"user{args.remove_account} 제거 완료 (복구 가능): {backup}")
        return 0

    if args.check_subscription:
        for account_id in args.account_ids:
            tool = get_tool(account_id)
            try:
                plan = accounts.check_paid_subscription(account_id, tool)
                email = accounts.account_email(account_id, tool)
                account_text = f"{email}({plan})" if email else plan
                print(f"[user{account_id}] [{tool}] 유료 구독 확인: {account_text}")
            except accounts.AccountStatusError as exc:
                print(f"[user{account_id}] [{tool}] SKIP: {exc}")
                continue

            try:
                status = fetch_usage(account_id, tool)
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

    _share_claude_config_all()

    states = initialize_states(
        args.account_ids,
        wait_until_epoch,
        args.delay * 60,
        now,
        threshold=args.threshold,
        fallback_min=args.interval,
        get_tool=get_tool,
        known_plans=known_plans,
    )

    def run_claude_fn(account_id: int) -> bool:
        tool = states[account_id].tool
        model = args.model or DEFAULT_MODEL[tool]
        effort = args.effort or DEFAULT_EFFORT[tool]
        return run_tool(account_id, tool, model, effort)

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


PROJECT_LOCK_ROOT = Path(__file__).parent / ".schedule" / "locks"
PROJECT_LOOP_DEFAULT_MODEL = "claude-opus-5-5"
PROJECT_LOOP_DEFAULT_EFFORT = "high"


def _project_loop_arg_error(args, management_flags: bool) -> str | None:
    if not args.project_loop:
        return "--project-dir/--prompt-file은 --project-loop와 함께만 쓸 수 있습니다."
    if management_flags:
        return "--project-loop는 --add-account/--list-accounts/--remove-account/--check-subscription과 함께 쓸 수 없습니다."
    if args.project_dir is None:
        return "--project-loop에는 --project-dir이 필요합니다."
    if args.delay < 0:
        return f"--delay는 음수일 수 없습니다 (delay={args.delay})"
    if args.interval < 1:
        return f"--project-loop의 --interval은 1 이상이어야 합니다 (interval={args.interval})"
    project_dir = Path(args.project_dir).expanduser()
    if not project_dir.is_dir():
        return f"프로젝트 디렉터리가 없습니다: {project_dir}"
    if not (project_dir / ".git").exists():
        return f"Git 저장소가 아닙니다: {project_dir}"
    prompt_path = project_dir / (args.prompt_file or "prompt.md")
    if not prompt_path.is_file():
        return f"프롬프트 파일을 찾을 수 없습니다: {prompt_path}"
    return None


def _run_project_loop_mode(args, get_tool, management_flags: bool) -> int:
    import project_loop
    import step_runner

    error = _project_loop_arg_error(args, management_flags)
    if error:
        print(error, file=sys.stderr)
        return 1
    project_dir = Path(args.project_dir).expanduser().resolve()
    prompt_path = project_dir / (args.prompt_file or "prompt.md")

    now = int(_time.time())
    start_at = now
    if args.wait_until:
        try:
            start_at = _parse_wait_until(args.wait_until, now)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    start_at += args.delay * 60

    try:
        lock_fd = project_loop.acquire_project_lock(project_loop.lock_path_for(project_dir, PROJECT_LOCK_ROOT))
    except project_loop.ProjectLockError as exc:
        print(
            f"이미 이 프로젝트에서 작업 루프가 실행 중입니다: {project_dir}\n  {exc}\n"
            "중복 실행하면 두 에이전트가 PHASE.md와 git을 동시에 수정하므로 시작하지 않습니다.",
            file=sys.stderr,
        )
        return 1

    try:
        if args.count is not None:
            account_ids, plans = select_paid_account_ids(args.count, get_tool, list(accounts.known_account_ids()))
        else:
            account_ids = args.account_ids
            plans = {account_id: _check_plan(account_id, get_tool(account_id)) for account_id in account_ids}
        if not any(plans.get(account_id) for account_id in account_ids):
            print("--project-loop: 유료 구독인 계정이 없습니다.", file=sys.stderr)
            return 1

        project_loop.write_lock_info(
            lock_fd,
            f"PID {os.getpid()}, 시작 {datetime.now():%Y-%m-%d %H:%M:%S}, 계정 "
            + ", ".join(f"user{account_id}" for account_id in account_ids),
        )
        _share_claude_config_all()

        loop_accounts = project_loop.build_loop_accounts(
            account_ids, get_tool, plans, fetch_usage,
            start_at=start_at, now=int(_time.time()), interval_min=args.interval,
        )
        model = args.model or PROJECT_LOOP_DEFAULT_MODEL
        effort = args.effort or PROJECT_LOOP_DEFAULT_EFFORT

        def run_step_fn(account):
            # 루프 도중 프롬프트를 고치면 다음 실행부터 반영되도록 매번 읽는다.
            prompt_text = prompt_path.read_text(encoding="utf-8")
            return step_runner.run_step(account.account_id, account.tool, project_dir, prompt_text, model, effort)

        try:
            return project_loop.run_project_loop(
                loop_accounts,
                run_step_fn=run_step_fn,
                fetch_usage_fn=fetch_usage,
                check_plan_fn=_check_plan,
                sleep_fn=_time.sleep,
                now_fn=lambda: int(_time.time()),
                interval_min=args.interval,
            )
        except KeyboardInterrupt:
            print("\n작업 루프를 중단했습니다 (Ctrl+C).")
            return 130
    finally:
        project_loop.release_project_lock(lock_fd)


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
