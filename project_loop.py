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
