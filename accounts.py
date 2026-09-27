"""Claude/Codex 계정 디렉터리 규칙, 구독 게이트, 계정 관리.

account_id는 도구별로 별도 네임스페이스가 아니라 registry.py가 관리하는
전역 매핑(userN -> "claude"|"codex")을 통해 어느 도구인지 결정된다. 이
모듈의 함수들은 그 결과인 `tool` 문자열을 파라미터로 받는다 — 이 모듈
자체는 registry를 import하지 않는다(호출자가 조회해서 넘겨준다).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import usage

PAID_PLANS = {"pro", "max", "team", "enterprise"}

CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"

# Codex(ChatGPT) 쪽 subscriptionType 동급 값. free/unknown은 게이트 통과 못 함.
CODEX_PAID_PLANS = {"plus", "pro", "team", "business", "enterprise"}


class AccountStatusError(Exception):
    pass


def account_dir(account_id: int, tool: str = "claude") -> Path:
    if account_id == 1:
        return Path.home() / (".codex" if tool == "codex" else ".claude")
    return Path.home() / ".usage-reset-scheduler" / "accounts" / f"{tool}-{account_id}"


def classify_subscription(status: dict) -> str:
    if status.get("loggedIn") is not True:
        raise AccountStatusError("로그인되지 않은 계정")

    auth_method = str(status.get("authMethod") or "").lower()
    if auth_method not in ("claude.ai", "chatgpt"):
        raise AccountStatusError(f"지원하지 않는 로그인 방식 (authMethod={auth_method or 'unknown'})")

    plan = str(status.get("subscriptionType") or "").lower()
    paid_plans = CODEX_PAID_PLANS if auth_method == "chatgpt" else PAID_PLANS
    if plan not in paid_plans:
        raise AccountStatusError(f"유료 구독이 아님 (subscriptionType={plan or 'unknown'})")

    return plan


def _decode_jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _codex_auth_status(config_dir: Path) -> dict:
    """Build a claude-auth-status-shaped dict from Codex's auth.json.

    Codex has no `auth status --json` equivalent, so this decodes the
    (unverified — signature is not checked, only used for display data)
    id_token JWT stored locally by a successful `codex login`.
    """
    auth_path = config_dir / "auth.json"
    try:
        auth = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountStatusError("Codex 인증 상태 확인 실패") from exc

    tokens = auth.get("tokens") or {}
    id_token = tokens.get("id_token")
    if not id_token:
        return {"loggedIn": False}

    try:
        claims = _decode_jwt_claims(id_token)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AccountStatusError("Codex 인증 토큰 파싱 실패") from exc

    chatgpt_claims = claims.get("https://api.openai.com/auth") or {}
    return {
        "loggedIn": True,
        "authMethod": "chatgpt",
        "subscriptionType": chatgpt_claims.get("chatgpt_plan_type"),
        "email": claims.get("email"),
    }


def run_claude_auth_status(account_id: int, tool: str = "claude") -> dict:
    if tool == "codex":
        return _codex_auth_status(account_dir(account_id, tool))

    env = os.environ.copy()
    if account_id == 1:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(account_dir(account_id, tool))

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


def fetch_claude_live_plan(config_dir: Path) -> str | None:
    """Current plan straight from the OAuth profile endpoint.

    `claude auth status`/.credentials.json keep the subscriptionType from
    login time, so they miss upgrades and downgrades done afterwards. The
    profile endpoint's organization_type ("claude_pro", "claude_free", ...)
    reflects the live subscription. Returns e.g. "pro"/"free", or None when
    the response carries no organization_type. Raises AccountStatusError
    when the server rejects a not-yet-expired token (revoked — e.g. after a
    downgrade or logout elsewhere), and other exceptions on network/HTTP/
    token-file errors.
    """
    token = usage.read_claude_token(config_dir)
    response = requests.get(
        CLAUDE_PROFILE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
        timeout=20,
    )
    if response.status_code == 401 and not _claude_token_expired(config_dir):
        # 만료 전 토큰이 거부됐다면 폐기된 것 — `claude` 실행으로도 갱신되지 않는다.
        # (이미 만료된 토큰의 401은 다음 실행 때 refresh될 수 있으므로 일반 오류로 둔다.)
        raise AccountStatusError("OAuth 토큰이 폐기됨, 재로그인 필요 (401)")
    response.raise_for_status()
    organization = response.json().get("organization") or {}
    org_type = str(organization.get("organization_type") or "").lower()
    return org_type.removeprefix("claude_") or None


def _claude_token_expired(config_dir: Path) -> bool:
    credentials = json.loads((config_dir / ".credentials.json").read_text())
    expires_at_ms = credentials["claudeAiOauth"].get("expiresAt")
    return expires_at_ms is None or expires_at_ms / 1000 <= time.time()


def fetch_codex_live_plan(config_dir: Path) -> str | None:
    """Current ChatGPT plan from the usage endpoint's plan_type.

    The id_token's chatgpt_plan_type claim is only as fresh as the last
    token refresh, so it can lag behind an upgrade/downgrade. Returns e.g.
    "plus"/"free", or None when plan_type is missing. Raises on
    network/HTTP/token errors.
    """
    token = usage.read_codex_token(config_dir)
    response = requests.get(
        usage.CODEX_USAGE_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    response.raise_for_status()
    plan = str(response.json().get("plan_type") or "").lower()
    return plan or None


def check_paid_subscription(account_id: int, tool: str = "claude") -> str:
    """Return the paid plan name, or raise AccountStatusError.

    Login state comes from the local auth status; the plan itself is taken
    from the live API when reachable so upgrades (free -> paid) and
    downgrades (paid -> free) are both picked up without re-login. Falls
    back to the locally cached plan when the live lookup fails.
    """
    status = run_claude_auth_status(account_id, tool)
    if status.get("loggedIn") is True:
        fetch_live_plan = fetch_codex_live_plan if tool == "codex" else fetch_claude_live_plan
        try:
            live_plan = fetch_live_plan(account_dir(account_id, tool))
        except AccountStatusError:
            raise
        except Exception as exc:
            live_plan = None
            print(
                f"[account {account_id}] 실시간 구독 조회 실패, 로컬 정보(subscriptionType)로 판단: {exc!r}",
                file=sys.stderr,
            )
        if live_plan is not None:
            status = {**status, "subscriptionType": live_plan}
    return classify_subscription(status)


def account_email(account_id: int, tool: str = "claude") -> str | None:
    """Best-effort lookup of the logged-in email for account_id.

    Returns None (never raises) when the status call fails or carries no
    email field — email is informational display data, not a gate.
    """
    try:
        status = run_claude_auth_status(account_id, tool)
    except AccountStatusError:
        return None
    email = status.get("email")
    return email if isinstance(email, str) and email else None


def add_account(account_id: int, tool: str = "claude") -> None:
    path = account_dir(account_id, tool)
    path.mkdir(parents=True, exist_ok=True)

    if tool == "codex":
        env = os.environ.copy()
        env["CODEX_HOME"] = str(path)
        subprocess.run(["codex", "login"], env=env, check=True)
        return

    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(path)
    subprocess.run(["claude", "auth", "login"], env=env, check=True)


def remove_account(account_id: int, tool: str = "claude") -> Path:
    if account_id == 1:
        raise ValueError("user1은 제거할 수 없습니다")

    path = account_dir(account_id, tool)
    if not path.exists():
        raise FileNotFoundError(f"등록된 user{account_id} 계정을 찾을 수 없습니다: {path}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.parent / f"{path.name}.removed-{timestamp}-{os.getpid()}"
    shutil.move(str(path), str(backup))
    return backup


def list_accounts(get_tool=None) -> list[tuple[int, str, Path, str | None, str]]:
    """List every known account across both tools.

    get_tool: optional callable(account_id) -> "claude"|"codex", normally
    `functools.partial(registry.get_tool, registry_path)`. Defaults to
    always "claude" when omitted (pre-multi-tool behavior).
    """
    if get_tool is None:
        get_tool = lambda _account_id: "claude"

    accounts_root = Path.home() / ".usage-reset-scheduler" / "accounts"
    account_ids = {1}
    for candidate in accounts_root.glob("*-*"):
        if not candidate.is_dir():
            continue
        suffix = candidate.name.rsplit("-", 1)[-1]
        if suffix.isdigit() and int(suffix) >= 2:
            account_ids.add(int(suffix))

    results = []
    for account_id in sorted(account_ids):
        tool = get_tool(account_id)
        path = account_dir(account_id, tool)
        try:
            plan = check_paid_subscription(account_id, tool)
            results.append((account_id, plan, path, account_email(account_id, tool), tool))
        except AccountStatusError as exc:
            results.append((account_id, f"SKIP: {exc}", path, None, tool))
    return results
