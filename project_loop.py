"""Rotate accounts through a project's prompt loop, switching accounts on usage limits."""
from __future__ import annotations

import fcntl
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from step_runner import CONTINUE_STATUSES, StepOutcome

RESET_GRACE_SEC = 60
ACCOUNT_RECHECK_SEC = 60 * 60
AMBIGUOUS_LIMIT_PERCENT = 95
UNKNOWN_FAIL_LIMIT = 3
MAX_SLEEP_SLICE_SEC = 60


@dataclass
class LoopAccount:
    account_id: int
    tool: str
    available_at: int
    status: str  # "ready" | "exhausted" | "account_error"
    reason: str | None = None
    runs: int = 0


def _fmt(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _safe_usage(fetch_usage_fn, account: LoopAccount, print_fn) -> dict | None:
    try:
        return fetch_usage_fn(account.account_id, account.tool)
    except Exception as exc:
        print_fn(f"user{account.account_id} 사용량 조회 실패: {exc!r}")
        return None


def usage_block(status: dict, now: int, interval_min: int, min_percent: float = 100) -> tuple[int, str] | None:
    """사용량이 min_percent 이상 찬 창이 있으면 (다시 쓸 수 있는 시각, 사유), 아니면 None."""
    week_pct = status.get("weekly_used_percent")
    five_pct = status.get("five_hour_used_percent")
    if week_pct is not None and week_pct >= min_percent:
        reset_at, reason = status.get("weekly_reset_at"), "주간 한도"
    elif five_pct is not None and five_pct >= min_percent:
        reset_at, reason = status.get("five_hour_reset_at"), "5시간 한도"
    else:
        return None
    if reset_at is None or reset_at <= now:
        return now + interval_min * 60, reason
    return reset_at + RESET_GRACE_SEC, reason


def build_loop_accounts(
    account_ids: list[int],
    get_tool,
    plans: dict[int, str | None],
    fetch_usage_fn,
    *,
    start_at: int,
    now: int,
    interval_min: int,
    print_fn=print,
) -> list[LoopAccount]:
    result = []
    for account_id in account_ids:
        account = LoopAccount(account_id=account_id, tool=get_tool(account_id), available_at=start_at, status="ready")
        if plans.get(account_id) is None:
            account.status = "account_error"
            account.reason = "유료 구독 아님"
            account.available_at = max(start_at, now + ACCOUNT_RECHECK_SEC)
        else:
            status = _safe_usage(fetch_usage_fn, account, print_fn)
            block = usage_block(status, now, interval_min) if status is not None else None
            if block is not None:
                account.status = "exhausted"
                account.available_at = max(start_at, block[0])
                account.reason = block[1]
        result.append(account)
    return result


def format_status(loop_accounts: list[LoopAccount], current: LoopAccount, now: int) -> str:
    others = sorted(
        (account for account in loop_accounts if account is not current),
        key=lambda account: (account.available_at, account.account_id),
    )
    lines = ["==========작업루프 상태============"]
    for account in [current] + others:
        tag = f"user{account.account_id} [{account.tool:<6}]"
        if account is current:
            lines.append(f"▶ 실행: {tag}  (연속 {account.runs}회차, {_fmt(now)} 시작)")
        elif account.status == "exhausted":
            lines.append(f"  {tag}  소진({account.reason}) → {_fmt(account.available_at)} 재투입")
        elif account.status == "account_error":
            lines.append(f"  {tag}  계정 오류({account.reason}) → {_fmt(account.available_at)} 재확인")
        else:
            lines.append(f"  {tag}  대기")
    lines.append("===================================")
    return "\n".join(lines)


class ProjectLockError(Exception):
    """이미 다른 작업 루프가 같은 프로젝트를 잠그고 있다. str()은 그쪽이 기록한 정보."""


def lock_path_for(project_dir: Path, lock_root: Path) -> Path:
    digest = hashlib.sha1(str(Path(project_dir).expanduser().resolve()).encode("utf-8")).hexdigest()[:12]
    return lock_root / f"project-{digest}.lock"


def acquire_project_lock(lock_path: Path) -> int:
    # flock은 프로세스가 어떻게 끝나든(kill -9 포함) OS가 풀어 주므로 남은 잠금 파일을 지울 필요가 없다.
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.pread(fd, 4096, 0).decode("utf-8", errors="replace").strip()
        os.close(fd)
        raise ProjectLockError(holder or "(실행 정보 없음)") from None
    os.ftruncate(fd, 0)
    return fd


def write_lock_info(fd: int, info: str) -> None:
    os.ftruncate(fd, 0)
    os.pwrite(fd, info.encode("utf-8"), 0)


def release_project_lock(fd: int) -> None:
    os.close(fd)


CONTINUE_MESSAGES = {
    "COMPLETE": "현재 Step 완료. 같은 계정으로 다음 실행을 이어갑니다.",
    "FAILURE_ANALYSIS": "실패가 기록되었습니다. 다음 실행에서 실패 원인을 분석합니다.",
    "FAILURE_IMPROVEMENT": "실패 분석이 완료되었습니다. 다음 실행에서 개선 계획을 실행합니다.",
}


def _pick_next(loop_accounts: list[LoopAccount], now: int) -> LoopAccount | None:
    ready = [account for account in loop_accounts if account.available_at <= now]
    if not ready:
        return None
    return min(ready, key=lambda account: (account.available_at, account.account_id))


def _set_exhausted(account: LoopAccount, available_at: int, reason: str, print_fn) -> None:
    account.status = "exhausted"
    account.available_at = available_at
    account.reason = reason
    account.runs = 0
    print_fn(f"user{account.account_id} [{account.tool}] {reason} → {_fmt(available_at)} 재투입")


def _limit_available_at(outcome: StepOutcome, account: LoopAccount, fetch_usage_fn, now: int, interval_min: int, print_fn) -> tuple[int, str]:
    if outcome.limit_reset_at is not None and outcome.limit_reset_at > now:
        return outcome.limit_reset_at + RESET_GRACE_SEC, "한도 초과"
    status = _safe_usage(fetch_usage_fn, account, print_fn)
    block = usage_block(status, now, interval_min) if status is not None else None
    if block is not None:
        return block
    return now + interval_min * 60, "한도 초과(리셋 시각 미확인)"


def run_project_loop(
    loop_accounts: list[LoopAccount],
    *,
    run_step_fn,
    fetch_usage_fn,
    check_plan_fn,
    sleep_fn,
    now_fn,
    interval_min: int,
    print_fn=print,
) -> int:
    current: LoopAccount | None = None
    unknown_fail_streak = 0
    last_wait_target = None

    while True:
        now = now_fn()
        if current is None or current.available_at > now:
            current = _pick_next(loop_accounts, now)
            if current is None:
                soonest = min(loop_accounts, key=lambda account: (account.available_at, account.account_id))
                wait_target = (soonest.account_id, soonest.available_at)
                if wait_target != last_wait_target:
                    print_fn(f"[{_fmt(now)}] 사용 가능한 계정 없음 → user{soonest.account_id} 재개 {_fmt(soonest.available_at)}까지 대기")
                    last_wait_target = wait_target
                sleep_fn(min(soonest.available_at - now, MAX_SLEEP_SLICE_SEC))
                continue
            last_wait_target = None

        if current.status == "account_error":
            if check_plan_fn(current.account_id, current.tool) is None:
                current.available_at = now_fn() + ACCOUNT_RECHECK_SEC
                print_fn(f"user{current.account_id} 계정 오류 지속 → {_fmt(current.available_at)} 재확인")
                current = None
                continue
            print_fn(f"user{current.account_id} 계정 확인 완료 → 다시 사용합니다.")

        # 큐에서는 사용 가능해 보여도 그 사이 직접 사용해 한도가 찼을 수 있다.
        status = _safe_usage(fetch_usage_fn, current, print_fn)
        block = usage_block(status, now_fn(), interval_min) if status is not None else None
        if block is not None:
            _set_exhausted(current, *block, print_fn)
            current = None
            continue

        current.status = "ready"
        current.reason = None
        current.runs += 1
        print_fn(format_status(loop_accounts, current, now_fn()))
        outcome = run_step_fn(current)
        now = now_fn()

        if outcome.step_status in CONTINUE_STATUSES:
            unknown_fail_streak = 0
            print_fn(CONTINUE_MESSAGES[outcome.step_status])
            continue

        if outcome.step_status == "INCOMPLETE":
            print_fn(
                "현재 Step이 미완료 또는 차단되었습니다(STEP_STATUS: INCOMPLETE). "
                "계정을 바꿔도 해결되지 않으므로 중단합니다.\n"
                f"로그: {outcome.log_path}"
            )
            return 1

        if outcome.limit_hit:
            unknown_fail_streak = 0
            available_at, reason = _limit_available_at(outcome, current, fetch_usage_fn, now, interval_min, print_fn)
            _set_exhausted(current, available_at, reason, print_fn)
            current = None
            continue

        if outcome.account_error is not None:
            current.status = "account_error"
            current.reason = outcome.account_error
            current.available_at = now + ACCOUNT_RECHECK_SEC
            current.runs = 0
            print_fn(f"user{current.account_id} 계정 오류({outcome.account_error}) → {_fmt(current.available_at)} 재확인")
            current = None
            continue

        # STEP_STATUS도 신호도 없음: 사용률이 거의 다 찼으면 신호를 놓친 한도 초과로 본다.
        status = _safe_usage(fetch_usage_fn, current, print_fn)
        block = None
        if status is not None:
            block = usage_block(status, now, interval_min) or usage_block(
                status, now, interval_min, min_percent=AMBIGUOUS_LIMIT_PERCENT
            )
        if block is not None:
            unknown_fail_streak = 0
            _set_exhausted(current, *block, print_fn)
            current = None
            continue

        unknown_fail_streak += 1
        print_fn(
            f"완료 상태 문자열도 한도 신호도 찾지 못했습니다 "
            f"({unknown_fail_streak}/{UNKNOWN_FAIL_LIMIT}, 종료 코드 {outcome.exit_code}). 로그: {outcome.log_path}"
        )
        if unknown_fail_streak >= UNKNOWN_FAIL_LIMIT:
            print_fn("알 수 없는 실패가 연속되어 안전하게 중단합니다.")
            return 1

        print_fn(f"{interval_min}분 뒤 같은 계정으로 다시 시도합니다.")
        sleep_fn(interval_min * 60)
