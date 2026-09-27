"""Claude 계정 디렉터리 규칙, 구독 게이트, 계정 관리."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

PAID_PLANS = {"pro", "max", "team", "enterprise"}


class AccountStatusError(Exception):
    pass


def account_dir(account_id: int) -> Path:
    if account_id == 1:
        return Path.home() / ".claude"
    return Path.home() / f".claude-account-{account_id}"


def classify_subscription(status: dict) -> str:
    if status.get("loggedIn") is not True:
        raise AccountStatusError("로그인되지 않은 계정")

    auth_method = str(status.get("authMethod") or "").lower()
    if auth_method != "claude.ai":
        raise AccountStatusError(f"Claude 구독 로그인이 아님 (authMethod={auth_method or 'unknown'})")

    plan = str(status.get("subscriptionType") or "").lower()
    if plan not in PAID_PLANS:
        raise AccountStatusError(f"유료 Claude 구독이 아님 (subscriptionType={plan or 'unknown'})")

    return plan


def run_claude_auth_status(account_id: int) -> dict:
    env = os.environ.copy()
    if account_id == 1:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(account_dir(account_id))

    try:
        completed = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise AccountStatusError("인증 상태 확인 실패") from exc

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AccountStatusError("인증 상태 JSON 파싱 실패") from exc


def check_paid_subscription(account_id: int) -> str:
    status = run_claude_auth_status(account_id)
    return classify_subscription(status)


def add_account(account_id: int) -> None:
    path = account_dir(account_id)
    path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(path)
    subprocess.run(["claude", "auth", "login"], env=env, check=True)


def remove_account(account_id: int) -> Path:
    if account_id == 1:
        raise ValueError("user1은 제거할 수 없습니다")

    path = account_dir(account_id)
    if not path.exists():
        raise FileNotFoundError(f"등록된 user{account_id} 계정을 찾을 수 없습니다: {path}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.parent / f"{path.name}.removed-{timestamp}-{os.getpid()}"
    shutil.move(str(path), str(backup))
    return backup


def list_accounts() -> list[tuple[int, str, Path]]:
    home = Path.home()
    account_ids = {1}
    for candidate in home.glob(".claude-account-*"):
        if not candidate.is_dir():
            continue
        suffix = candidate.name.removeprefix(".claude-account-")
        if suffix.isdigit() and int(suffix) >= 2:
            account_ids.add(int(suffix))

    results = []
    for account_id in sorted(account_ids):
        path = account_dir(account_id)
        try:
            plan = check_paid_subscription(account_id)
            results.append((account_id, plan, path))
        except AccountStatusError as exc:
            results.append((account_id, f"SKIP: {exc}", path))
    return results
