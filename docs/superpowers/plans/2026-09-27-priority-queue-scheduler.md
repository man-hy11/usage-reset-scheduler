# Claude 우선순위 큐 스케줄러 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `run-step-loop-claude.sh` + `collect-usage.sh`의 계정별 독립 병렬 프로세스 구조를, 단일 Python 컨트롤러가 우선순위 큐로 여러 Claude 계정의 다음 실행 시각을 관리하고 `claude` 호출을 완전 순차로 실행하는 구조로 교체한다.

**Architecture:** `usage.py`(사용량 API 조회 + 다음 실행 시각 계산) → `accounts.py`(계정 디렉터리 규칙 + 유료 구독 게이트 + 계정 관리 CLI 동작) → `scheduler.py`(heapq 기반 메인 루프, CLI 진입점)의 3계층으로 상향식으로 쌓는다. 각 계층은 이전 계층의 순수 함수만 소비하며, `scheduler.py`만 `subprocess`/`time.sleep`/파일 I/O 같은 부수효과를 다룬다.

**Tech Stack:** Python 3.12 표준 라이브러리(`heapq`, `pathlib`, `subprocess`, `json`, `argparse`, `time`, `signal`) + `requests`(HTTP) + `pytest`(테스트, `monkeypatch`로 subprocess/requests 대체)

**Spec:** `docs/superpowers/specs/2026-09-27-priority-queue-scheduler-design.md`

## Global Constraints

- 계정 자격증명 위치는 변경하지 않는다: 계정 `1`은 `Path.home() / ".claude"`, 계정 `N≥2`는 `Path.home() / f".claude-account-{N}"`.
- 유료 플랜 허용 목록은 정확히 `{"pro", "max", "team", "enterprise"}` (소문자 비교).
- `claude auth status --json`에서 `loggedIn is True`이고 `authMethod == "claude.ai"`이고 `subscriptionType`이 허용 목록에 있어야 통과.
- 무료/미로그인/판정불가 계정은 최초 1회 확인 후 큐에서 영구 제외하며 이후 어떤 루프 반복에서도 재확인하지 않는다.
- `claude` 프로세스 실행은 항상 정확히 한 번에 하나만 진행한다 (동시 실행 금지).
- 사용량 조회 실패 계정은 `retry_pending` 상태로 유지하며, `fail_count`가 5 이상이어도 재시도를 멈추지 않는다 (영구 포기 없음). 5는 로그 심각도를 올리는 소프트 임계치일 뿐이다.
- 큐 상태는 매 반복마다 `.schedule/queue.json`에 영속화한다 (프로젝트 로컬, git 비대상).
- CLI 플래그 인터페이스(`--model`, `--effort`, `-w/--wait-until`, `-d/--delay`, `--interval`, `--threshold`, `--check-subscription`, `--add-account`, `--list-accounts`, `--remove-account`)는 기존 `run-step-loop-claude.sh`와 동일한 이름·형식을 유지한다.
- 기존 `run-step-loop.sh`(Codex)는 이번 작업에서 수정하지 않는다.

## Review Focus

- **큐 파일이 손상되었거나 존재하지 않을 때**: JSON 파싱 실패나 파일 부재 시 예외로 죽지 않고 빈 상태로 새로 시작해야 한다 (스펙의 "에러 처리" 절).
- **`--wait-until`에 과거 시각을 준 경우**: 기존 스크립트처럼 명확한 에러 메시지와 함께 즉시 종료해야 하며, 조용히 즉시 실행되거나 무한 대기해서는 안 된다.
- **사용량 API가 필드 일부만 결측으로 반환할 때(예: `five_hour_reset_at`만 없음)**: `compute_next_run`이 부분 결측을 완전 결측과 동일하게 취급해 폴백 시간을 쓰는지 — 결측 필드 하나 때문에 `None` 비교에서 예외가 나면 안 된다.
- **같은 `next_run_at`을 가진 두 계정이 동시에 큐에 있을 때**: `heapq`가 튜플 비교 중 두 번째 요소(계정 id, 정수)로 비교 가능해야 하며 타입 불일치로 `TypeError`가 나면 안 된다.
- **`--remove-account`로 존재하지 않는 계정을 지정했을 때**: 조용히 성공하지 않고 명확한 에러로 종료해야 한다 (기존 bash의 동작 유지).

---

## Task 1: `usage.py` — 사용량 조회 및 다음 실행 시각 계산

**Files:**
- Create: `usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Consumes: 없음 (최하위 계층, `requests`만 사용)
- Produces:
  - `read_claude_token(config_dir: pathlib.Path) -> str` — `config_dir / ".credentials.json"`에서 `claudeAiOauth.accessToken` 반환. 파일 없거나 키 없으면 `FileNotFoundError`/`KeyError` 그대로 전파.
  - `fetch_claude_usage(config_dir: pathlib.Path) -> dict` — `read_claude_token`으로 토큰을 얻어 `https://api.anthropic.com/api/oauth/usage`를 GET(`anthropic-beta: oauth-2025-04-20`, `Authorization: Bearer <token>`, `timeout=20`) 하고 정규화된 dict를 반환:
    `{"tool": "claude", "fetched_at": int, "five_hour_used_percent": float|None, "five_hour_reset_at": int|None, "weekly_used_percent": float|None, "weekly_reset_at": int|None}`
  - `compute_next_run(status: dict | None, threshold: float, fallback_min: int, now: int | None = None) -> int` — 다음 실행까지 대기할 초를 반환. `now`가 `None`이면 `int(time.time())` 사용.

- [ ] **Step 1: 디렉터리 및 의존성 확인**

Run: `python3 -c "import requests; print('ok')"`
Expected: `ok` 출력 (이미 설치되어 있음을 확인만 하는 단계, 설치 불필요)

- [ ] **Step 2: `compute_next_run`의 실패 폴백 테스트 작성**

`tests/test_usage.py` 새로 생성:

```python
import usage


def test_compute_next_run_returns_fallback_when_status_is_none():
    seconds = usage.compute_next_run(None, threshold=100, fallback_min=5, now=1_790_469_000)
    assert seconds == 5 * 60


def test_compute_next_run_returns_fallback_when_fields_missing():
    status = {
        "five_hour_used_percent": None,
        "five_hour_reset_at": None,
        "weekly_used_percent": None,
        "weekly_reset_at": None,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=7, now=1_790_469_000)
    assert seconds == 7 * 60


def test_compute_next_run_returns_fallback_when_reset_field_partially_missing():
    status = {
        "five_hour_used_percent": 10,
        "five_hour_reset_at": None,
        "weekly_used_percent": 5,
        "weekly_reset_at": 1_790_470_000,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=7, now=1_790_469_000)
    assert seconds == 7 * 60
```

- [ ] **Step 3: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_usage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usage'` (아직 `usage.py` 없음)

- [ ] **Step 4: `usage.py` 최소 구현 (compute_next_run 폴백 경로만)**

```python
"""Claude 사용량 조회 및 다음 실행 시각 계산."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

MAX_SLEEP_SECONDS = 691_200  # 8일, collect-usage.sh의 MAX와 동일


def read_claude_token(config_dir: Path) -> str:
    credentials_path = config_dir / ".credentials.json"
    with open(credentials_path) as f:
        credentials = json.load(f)
    return credentials["claudeAiOauth"]["accessToken"]


def fetch_claude_usage(config_dir: Path) -> dict:
    token = read_claude_token(config_dir)
    response = requests.get(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
        timeout=20,
    )
    response.raise_for_status()
    raw = response.json()

    five = raw.get("five_hour") or {}
    week = raw.get("seven_day") or {}

    return {
        "tool": "claude",
        "fetched_at": int(time.time()),
        "five_hour_used_percent": five.get("utilization"),
        "five_hour_reset_at": _to_epoch(five.get("resets_at")),
        "weekly_used_percent": week.get("utilization"),
        "weekly_reset_at": _to_epoch(week.get("resets_at")),
    }


def _to_epoch(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    from datetime import datetime

    return int(datetime.fromisoformat(value).timestamp())


def compute_next_run(
    status: dict | None,
    threshold: float,
    fallback_min: int,
    now: int | None = None,
) -> int:
    if now is None:
        now = int(time.time())
    fallback = fallback_min * 60

    if status is None:
        return fallback

    five_pct = status.get("five_hour_used_percent")
    five_reset = status.get("five_hour_reset_at")
    week_pct = status.get("weekly_used_percent")
    week_reset = status.get("weekly_reset_at")

    if None in (five_pct, five_reset, week_pct, week_reset):
        return fallback

    return fallback
```

(마지막 `return fallback`은 Step 6에서 실제 리셋 계산으로 교체한다. 지금은 실패 폴백 테스트만 통과시킨다.)

- [ ] **Step 5: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_usage.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: 정상 계산 경로(주간 우선순위) 테스트 추가**

`tests/test_usage.py`에 추가:

```python
def test_compute_next_run_uses_five_hour_reset_when_weekly_under_threshold():
    status = {
        "five_hour_used_percent": 100,
        "five_hour_reset_at": 1_790_469_600,
        "weekly_used_percent": 42,
        "weekly_reset_at": 1_791_073_800,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=305, now=1_790_469_000)
    assert seconds == 660  # five_hour_reset_at - now + 60


def test_compute_next_run_uses_weekly_reset_when_weekly_at_or_over_threshold():
    status = {
        "five_hour_used_percent": 100,
        "five_hour_reset_at": 1_790_469_600,
        "weekly_used_percent": 100,
        "weekly_reset_at": 1_790_470_200,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=305, now=1_790_469_000)
    assert seconds == 1260  # weekly_reset_at - now + 60


def test_compute_next_run_falls_back_when_target_reset_in_past():
    status = {
        "five_hour_used_percent": 10,
        "five_hour_reset_at": 1_790_468_000,  # now보다 과거
        "weekly_used_percent": 5,
        "weekly_reset_at": 1_791_073_800,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=5, now=1_790_469_000)
    assert seconds == 5 * 60


def test_compute_next_run_falls_back_when_target_exceeds_max_window():
    status = {
        "five_hour_used_percent": 10,
        "five_hour_reset_at": 1_790_469_000 + usage.MAX_SLEEP_SECONDS + 10,
        "weekly_used_percent": 5,
        "weekly_reset_at": 1_790_469_000 + usage.MAX_SLEEP_SECONDS + 10,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=5, now=1_790_469_000)
    assert seconds == 5 * 60
```

- [ ] **Step 7: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_usage.py -v`
Expected: FAIL — 새 4개 테스트가 모두 `assert 305*60 == ...` 형태로 실패 (아직 폴백만 반환하므로)

- [ ] **Step 8: `compute_next_run`에 실제 리셋 계산 로직 구현**

`usage.py`의 `compute_next_run` 마지막 부분을 교체:

```python
def compute_next_run(
    status: dict | None,
    threshold: float,
    fallback_min: int,
    now: int | None = None,
) -> int:
    if now is None:
        now = int(time.time())
    fallback = fallback_min * 60

    if status is None:
        return fallback

    five_pct = status.get("five_hour_used_percent")
    five_reset = status.get("five_hour_reset_at")
    week_pct = status.get("weekly_used_percent")
    week_reset = status.get("weekly_reset_at")

    if None in (five_pct, five_reset, week_pct, week_reset):
        return fallback

    target = week_reset if week_pct >= threshold else five_reset

    if target <= now:
        return fallback
    if target - now + 60 >= MAX_SLEEP_SECONDS:
        return fallback
    return target - now + 60
```

- [ ] **Step 9: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_usage.py -v`
Expected: PASS (7 passed)

- [ ] **Step 10: `fetch_claude_usage` 테스트 작성 (requests mock)**

`tests/test_usage.py`에 추가:

```python
import json as _json

import pytest


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_claude_usage_normalizes_response(tmp_path, monkeypatch):
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(
        _json.dumps({"claudeAiOauth": {"accessToken": "tok-123"}})
    )

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(
            {
                "five_hour": {"utilization": 12, "resets_at": "2026-09-27T12:00:00+09:00"},
                "seven_day": {"utilization": 34, "resets_at": "2026-10-03T11:00:00+09:00"},
            }
        )

    monkeypatch.setattr(usage.requests, "get", fake_get)

    result = usage.fetch_claude_usage(config_dir)

    assert result["tool"] == "claude"
    assert result["five_hour_used_percent"] == 12
    assert result["weekly_used_percent"] == 34
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    assert captured["url"] == "https://api.anthropic.com/api/oauth/usage"


def test_read_claude_token_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        usage.read_claude_token(tmp_path / "does-not-exist")
```

- [ ] **Step 11: 테스트 실행하여 통과 확인 (회귀 확인 — `fetch_claude_usage`와 `read_claude_token`은 Step 4에서 이미 구현됨)**

Run: `cd ~/start_limit && python3 -m pytest tests/test_usage.py -v`
Expected: PASS (9 passed). FAIL이면 Step 4의 `read_claude_token`/`fetch_claude_usage` 구현이 이 테스트가 기대하는 시그니처·필드명과 어긋난 것이므로, 테스트가 아니라 Step 4 구현을 이 테스트에 맞게 고친다.

- [ ] **Step 12: Commit**

```bash
cd ~/start_limit
git init -q 2>/dev/null || true
git add usage.py tests/test_usage.py
git commit -m "feat: add usage.py for Claude usage lookup and next-run calculation"
```

(주: 이 리포지토리는 현재 git 초기화가 안 되어 있을 수 있다. `git init -q 2>/dev/null || true`로 안전하게 초기화하되, 이미 초기화되어 있으면 그대로 넘어간다. 최초 커밋이라면 `git commit`이 사용자/이메일 설정을 요구할 수 있으니 실패 시 사용자에게 안내한다.)

---

## Task 2: `accounts.py` — 계정 디렉터리 규칙 및 유료 구독 게이트

**Files:**
- Create: `accounts.py`
- Test: `tests/test_accounts.py`

**Interfaces:**
- Consumes: 없음 (표준 라이브러리 + `subprocess`만 사용, `usage.py`에 의존하지 않음)
- Produces:
  - `account_dir(account_id: int) -> pathlib.Path` — `1`이면 `Path.home() / ".claude"`, 그 외는 `Path.home() / f".claude-account-{account_id}"`
  - `run_claude_auth_status(account_id: int) -> dict` — 해당 계정 디렉터리를 `CLAUDE_CONFIG_DIR`(account_id==1이면 환경변수 제거)로 설정해 `claude auth status --json`을 서브프로세스로 실행하고 JSON 파싱해 반환. 실패(비정상 종료, JSON 파싱 실패)하면 `AccountStatusError` 예외 발생.
  - `class AccountStatusError(Exception)`
  - `classify_subscription(status: dict) -> str` — 통과 시 plan 이름(`"pro"` 등) 반환, 거부 사유가 있으면 `AccountStatusError(reason)` 발생.
  - `check_paid_subscription(account_id: int) -> str` — `run_claude_auth_status` + `classify_subscription`을 합쳐 최종 plan 이름 반환하거나 `AccountStatusError` 발생.
  - `add_account(account_id: int) -> None` — `account_dir` 생성 후 `CLAUDE_CONFIG_DIR`를 설정해 `claude auth login`을 서브프로세스로 실행(exit code 그대로 전파하려면 `subprocess.run(..., check=True)` 후 `CalledProcessError`는 호출자가 처리).
  - `list_accounts() -> list[tuple[int, str, Path]]` — `(account_id, status_text, path)` 목록. `status_text`는 plan 이름 또는 `"SKIP: <사유>"`.
  - `remove_account(account_id: int) -> Path` — 성공 시 백업 경로 반환. `account_id == 1`이면 `ValueError("user1은 제거할 수 없습니다")`. 대상 없으면 `FileNotFoundError`.

- [ ] **Step 1: `account_dir` 테스트 작성**

`tests/test_accounts.py` 새로 생성:

```python
from pathlib import Path

import accounts


def test_account_dir_for_account_1_is_dot_claude(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.account_dir(1) == tmp_path / ".claude"


def test_account_dir_for_account_n_uses_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.account_dir(3) == tmp_path / ".claude-account-3"
```

- [ ] **Step 2: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_accounts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'accounts'`

- [ ] **Step 3: `accounts.py` 뼈대 + `account_dir` 구현**

```python
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
```

- [ ] **Step 4: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_accounts.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: `classify_subscription` 테스트 작성**

`tests/test_accounts.py`에 추가:

```python
import pytest


@pytest.mark.parametrize("plan", ["pro", "max", "team", "enterprise"])
def test_classify_subscription_accepts_paid_plans(plan):
    status = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": plan}
    assert accounts.classify_subscription(status) == plan


def test_classify_subscription_rejects_free_plan():
    status = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "free"}
    with pytest.raises(accounts.AccountStatusError):
        accounts.classify_subscription(status)


def test_classify_subscription_rejects_logged_out():
    status = {"loggedIn": False, "authMethod": "none", "subscriptionType": None}
    with pytest.raises(accounts.AccountStatusError):
        accounts.classify_subscription(status)


def test_classify_subscription_rejects_api_key_auth():
    status = {"loggedIn": True, "authMethod": "apiKey", "subscriptionType": "pro"}
    with pytest.raises(accounts.AccountStatusError):
        accounts.classify_subscription(status)
```

- [ ] **Step 6: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_accounts.py -v`
Expected: FAIL — `AttributeError: module 'accounts' has no attribute 'classify_subscription'`

- [ ] **Step 7: `classify_subscription` 구현**

`accounts.py`에 추가:

```python
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
```

- [ ] **Step 8: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_accounts.py -v`
Expected: PASS (6 passed)

- [ ] **Step 9: `run_claude_auth_status` + `check_paid_subscription` 테스트 작성 (subprocess mock)**

`tests/test_accounts.py`에 추가:

```python
import subprocess


class _FakeCompleted:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_run_claude_auth_status_uses_account_1_without_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake_run(cmd, capture_output, text, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    result = accounts.run_claude_auth_status(1)

    assert result["subscriptionType"] == "pro"
    assert "CLAUDE_CONFIG_DIR" not in captured["env"]
    assert captured["cmd"] == ["claude", "auth", "status", "--json"]


def test_run_claude_auth_status_sets_config_dir_for_account_n(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake_run(cmd, capture_output, text, env, check):
        captured["env"] = env
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "free"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    accounts.run_claude_auth_status(2)

    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-account-2")


def test_run_claude_auth_status_raises_on_bad_json(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted("not-json")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    with pytest.raises(accounts.AccountStatusError):
        accounts.run_claude_auth_status(1)


def test_run_claude_auth_status_raises_on_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        raise subprocess.CalledProcessError(7, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    with pytest.raises(accounts.AccountStatusError):
        accounts.run_claude_auth_status(1)


def test_check_paid_subscription_returns_plan_for_paid_account(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    assert accounts.check_paid_subscription(3) == "max"


def test_check_paid_subscription_raises_for_free_account(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "free"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    with pytest.raises(accounts.AccountStatusError):
        accounts.check_paid_subscription(2)
```

- [ ] **Step 10: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_accounts.py -v`
Expected: FAIL — `AttributeError: module 'accounts' has no attribute 'run_claude_auth_status'` (및 `check_paid_subscription`)

- [ ] **Step 11: `run_claude_auth_status`, `check_paid_subscription` 구현**

`accounts.py`에 추가:

```python
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
```

- [ ] **Step 12: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_accounts.py -v`
Expected: PASS (12 passed)

- [ ] **Step 13: `add_account`, `list_accounts`, `remove_account` 테스트 작성**

`tests/test_accounts.py`에 추가:

```python
def test_add_account_creates_dir_and_runs_login(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake_run(cmd, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeCompleted("", 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    accounts.add_account(2)

    assert (tmp_path / ".claude-account-2").is_dir()
    assert captured["cmd"] == ["claude", "auth", "login"]
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-account-2")


def test_remove_account_moves_to_backup_path(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".claude-account-2"
    target.mkdir()

    backup = accounts.remove_account(2)

    assert not target.exists()
    assert backup.exists()
    assert str(backup).startswith(str(tmp_path / ".claude-account-2.removed-"))


def test_remove_account_1_raises_value_error(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()

    with pytest.raises(ValueError):
        accounts.remove_account(1)


def test_remove_account_missing_raises_file_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(FileNotFoundError):
        accounts.remove_account(9)


def test_list_accounts_reports_status_for_each(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude-account-2").mkdir()

    responses = {
        str(tmp_path / ".claude"): '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}',
        str(tmp_path / ".claude-account-2"): '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "free"}',
    }

    def fake_run(cmd, capture_output, text, env, check):
        config_dir = env.get("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
        return _FakeCompleted(responses[config_dir])

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    result = accounts.list_accounts()
    result_by_id = {r[0]: r for r in result}

    assert result_by_id[1][1] == "pro"
    assert result_by_id[2][1].startswith("SKIP:")
```

- [ ] **Step 14: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_accounts.py -v`
Expected: FAIL — `add_account`, `remove_account`, `list_accounts` 미정의로 `AttributeError`

- [ ] **Step 15: `add_account`, `remove_account`, `list_accounts` 구현**

`accounts.py`에 추가:

```python
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
```

- [ ] **Step 16: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_accounts.py -v`
Expected: PASS (17 passed)

- [ ] **Step 17: Commit**

```bash
cd ~/start_limit
git add accounts.py tests/test_accounts.py
git commit -m "feat: add accounts.py for account directory rules and paid-plan gate"
```

---

## Task 3: `scheduler.py` — 우선순위 큐 상태 저장/로드 및 재시도 로직

**Files:**
- Create: `scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes:
  - `accounts.account_dir(account_id: int) -> Path`
  - `accounts.check_paid_subscription(account_id: int) -> str` (raises `accounts.AccountStatusError`)
  - `usage.fetch_claude_usage(config_dir: Path) -> dict`
  - `usage.compute_next_run(status, threshold, fallback_min, now=None) -> int`
- Produces (이 태스크 범위):
  - `QUEUE_PATH: Path` — 기본값 `Path(__file__).parent / ".schedule" / "queue.json"`
  - `load_queue(path: Path) -> dict` — 손상/부재 시 빈 dict 반환
  - `save_queue(path: Path, state: dict) -> None`
  - `class AccountState` (dataclass): `next_run_at: int | None`, `status: str`(`"scheduled"|"retry_pending"|"free_skip"`), `fail_count: int = 0`
  - `state_to_dict(states: dict[int, AccountState]) -> dict` / `state_from_dict(raw: dict) -> dict[int, AccountState]`
  - (다음 태스크에서 이 함수들을 소비해 메인 루프를 완성한다)

- [ ] **Step 1: `AccountState` 직렬화 테스트 작성**

`tests/test_scheduler.py` 새로 생성:

```python
from pathlib import Path

import scheduler


def test_state_round_trip_through_dict():
    states = {
        1: scheduler.AccountState(next_run_at=1000, status="scheduled", fail_count=0),
        2: scheduler.AccountState(next_run_at=None, status="free_skip", fail_count=0),
    }
    raw = scheduler.state_to_dict(states)
    restored = scheduler.state_from_dict(raw)

    assert restored[1] == states[1]
    assert restored[2] == states[2]
```

- [ ] **Step 2: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scheduler'`

- [ ] **Step 3: `scheduler.py` 뼈대 + `AccountState`, 직렬화 함수 구현**

```python
"""Claude 다중 계정 우선순위 큐 스케줄러."""
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
```

- [ ] **Step 4: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: `load_queue`/`save_queue` 테스트 작성**

`tests/test_scheduler.py`에 추가:

```python
def test_save_and_load_queue_round_trip(tmp_path):
    path = tmp_path / "queue.json"
    states = {1: scheduler.AccountState(next_run_at=500, status="scheduled")}

    scheduler.save_queue(path, scheduler.state_to_dict(states))
    loaded_raw = scheduler.load_queue(path)
    loaded = scheduler.state_from_dict(loaded_raw)

    assert loaded[1] == states[1]


def test_load_queue_returns_empty_dict_when_file_missing(tmp_path):
    path = tmp_path / "does-not-exist.json"
    assert scheduler.load_queue(path) == {}


def test_load_queue_returns_empty_dict_when_file_corrupted(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text("not valid json {{{")
    assert scheduler.load_queue(path) == {}
```

- [ ] **Step 6: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'save_queue'`

- [ ] **Step 7: `load_queue`/`save_queue` 구현**

`scheduler.py`에 추가:

```python
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
```

- [ ] **Step 8: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS (4 passed)

- [ ] **Step 9: Commit**

```bash
cd ~/start_limit
git add scheduler.py tests/test_scheduler.py
git commit -m "feat: add scheduler state model and queue persistence"
```

---

## Task 4: `scheduler.py` — 초기화(구독 게이트) 및 우선순위 큐 메인 루프

**Files:**
- Modify: `scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: Task 3의 `AccountState`, `state_to_dict`, `state_from_dict`, `load_queue`, `save_queue`; Task 2의 `accounts.check_paid_subscription`, `accounts.account_dir`, `accounts.AccountStatusError`; Task 1의 `usage.fetch_claude_usage`, `usage.compute_next_run`
- Produces:
  - `initialize_states(account_ids: list[int], wait_until: int | None, delay_seconds: int, now: int) -> dict[int, AccountState]` — 각 계정 구독 확인 후 초기 상태 생성. 무료/실패는 `free_skip`(`next_run_at=None`), 유료는 `scheduled`(`next_run_at = wait_until if wait_until else now + delay_seconds`).
  - `retry_pending_accounts(states: dict[int, AccountState], interval_min: int, threshold: float, fallback_min: int, now: int) -> dict[int, AccountState]` — 순수 함수가 아니라 `usage.fetch_claude_usage`를 호출하는 부수효과 함수. `retry_pending` 상태인 계정들을 순회하며 재조회 시도. in-place로 states를 갱신하고 반환.
  - `run_scheduler_loop(states: dict[int, AccountState], *, interval_min: int, threshold: float, fallback_min: int, run_claude_fn, sleep_fn, now_fn, queue_path: Path) -> None` — 메인 루프. `run_claude_fn(account_id: int) -> bool`(성공 여부), `sleep_fn(seconds: float) -> None`, `now_fn() -> int`는 테스트에서 주입 가능하도록 파라미터화. 힙이 빌 때까지(모든 계정 `free_skip`) 반복.

- [ ] **Step 1: `initialize_states` 테스트 작성**

`tests/test_scheduler.py`에 추가:

```python
def test_initialize_states_marks_free_account_as_free_skip(monkeypatch):
    def fake_check(account_id):
        if account_id == 2:
            raise accounts.AccountStatusError("유료 Claude 구독이 아님")
        return "pro"

    monkeypatch.setattr(accounts, "check_paid_subscription", fake_check)

    states = scheduler.initialize_states([1, 2], wait_until=None, delay_seconds=0, now=1000)

    assert states[2].status == "free_skip"
    assert states[2].next_run_at is None
    assert states[1].status == "scheduled"


def test_initialize_states_uses_wait_until_when_given(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id: "pro")

    states = scheduler.initialize_states([1], wait_until=5000, delay_seconds=0, now=1000)

    assert states[1].next_run_at == 5000


def test_initialize_states_uses_now_plus_delay_when_no_wait_until(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id: "pro")

    states = scheduler.initialize_states([1], wait_until=None, delay_seconds=120, now=1000)

    assert states[1].next_run_at == 1120
```

- [ ] **Step 2: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'initialize_states'`

- [ ] **Step 3: `initialize_states` 구현**

`scheduler.py`에 추가:

```python
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
```

- [ ] **Step 4: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: `retry_pending_accounts` 테스트 작성**

`tests/test_scheduler.py`에 추가:

```python
def test_retry_pending_accounts_promotes_on_success(monkeypatch):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="retry_pending", fail_count=2),
    }

    monkeypatch.setattr(accounts, "account_dir", lambda account_id: Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda config_dir: {"weekly_used_percent": 10, "weekly_reset_at": 9999, "five_hour_used_percent": 5, "five_hour_reset_at": 2000})
    monkeypatch.setattr(usage, "compute_next_run", lambda status, threshold, fallback_min, now: 500)

    result = scheduler.retry_pending_accounts(states, interval_min=5, threshold=100, fallback_min=305, now=1000)

    assert result[1].status == "scheduled"
    assert result[1].fail_count == 0
    assert result[1].next_run_at == 1500  # now + compute_next_run


def test_retry_pending_accounts_keeps_retrying_on_failure(monkeypatch):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="retry_pending", fail_count=2),
    }

    monkeypatch.setattr(accounts, "account_dir", lambda account_id: Path(f"/fake/{account_id}"))

    def fake_fetch(config_dir):
        raise RuntimeError("network error")

    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)

    result = scheduler.retry_pending_accounts(states, interval_min=5, threshold=100, fallback_min=305, now=1000)

    assert result[1].status == "retry_pending"
    assert result[1].fail_count == 3
    assert result[1].next_run_at == 1000 + 5 * 60


def test_retry_pending_accounts_keeps_retrying_past_five_failures(monkeypatch):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="retry_pending", fail_count=5),
    }

    monkeypatch.setattr(accounts, "account_dir", lambda account_id: Path(f"/fake/{account_id}"))

    def fake_fetch(config_dir):
        raise RuntimeError("network error")

    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)

    result = scheduler.retry_pending_accounts(states, interval_min=5, threshold=100, fallback_min=305, now=1000)

    assert result[1].status == "retry_pending"
    assert result[1].fail_count == 6


def test_retry_pending_accounts_ignores_scheduled_and_free_skip():
    states = {
        1: scheduler.AccountState(next_run_at=100, status="scheduled", fail_count=0),
        2: scheduler.AccountState(next_run_at=None, status="free_skip", fail_count=0),
    }

    result = scheduler.retry_pending_accounts(states, interval_min=5, threshold=100, fallback_min=305, now=1000)

    assert result[1].status == "scheduled"
    assert result[2].status == "free_skip"
```

- [ ] **Step 6: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'retry_pending_accounts'`

- [ ] **Step 7: `retry_pending_accounts` 구현**

`scheduler.py`에 추가:

```python
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
```

- [ ] **Step 8: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS (11 passed)

- [ ] **Step 9: 우선순위 큐 순서(정합성) 테스트 작성 — heapq 튜플 비교**

`tests/test_scheduler.py`에 추가:

```python
import heapq


def test_heap_orders_by_next_run_at_then_account_id():
    states = {
        3: scheduler.AccountState(next_run_at=200, status="scheduled"),
        1: scheduler.AccountState(next_run_at=100, status="scheduled"),
        2: scheduler.AccountState(next_run_at=100, status="scheduled"),
    }
    heap = [(state.next_run_at, account_id) for account_id, state in states.items()]
    heapq.heapify(heap)

    first = heapq.heappop(heap)
    second = heapq.heappop(heap)

    assert first == (100, 1)
    assert second == (100, 2)
```

- [ ] **Step 10: 테스트 실행하여 통과 확인 (이미 표준 heapq 동작이므로 바로 통과해야 함)**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS (12 passed) — 이 테스트는 `run_scheduler_loop`가 사용할 정렬 키가 `(next_run_at, account_id)`처럼 완전히 정렬 가능한 튜플이어야 함을 문서화한다.

- [ ] **Step 11: `run_scheduler_loop` 순차 실행 테스트 작성**

`tests/test_scheduler.py`에 추가. 루프를 끝내려면 `run_claude_fn`이 예외를 던지는 방식을 쓴다 — `run_claude_fn`은 실제로는 `bool`만 반환하고 `states`를 직접 건드리지 않으므로(아래 Step 14 구현이 `run_claude_fn` 반환 직후 무조건 `states[account_id]`를 새로 계산해 덮어쓴다), 테스트 안에서 `states`를 직접 조작해 종료시키는 방식은 그 덮어쓰기에 의해 무효화되어 무한 루프가 된다:

```python
def test_run_scheduler_loop_runs_claude_sequentially_in_next_run_order(monkeypatch, tmp_path):
    states = {
        1: scheduler.AccountState(next_run_at=200, status="scheduled"),
        2: scheduler.AccountState(next_run_at=100, status="scheduled"),
    }

    call_order = []
    sleep_calls = []

    class _StopLoop(Exception):
        pass

    def fake_run_claude(account_id):
        call_order.append(account_id)
        # 두 번째 호출 후 예외로 루프를 끝낸다 (states 직접 조작은 Step 14의
        # 무조건 덮어쓰기 로직에 의해 무효화되므로 쓰지 않는다).
        if len(call_order) == 2:
            raise _StopLoop()
        return True

    def fake_fetch(config_dir):
        return {"five_hour_used_percent": 1, "five_hour_reset_at": 99999, "weekly_used_percent": 1, "weekly_reset_at": 99999}

    monkeypatch.setattr(accounts, "account_dir", lambda account_id: Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)
    monkeypatch.setattr(usage, "compute_next_run", lambda status, threshold, fallback_min, now: 999999)

    now_box = {"t": 100}

    def fake_now():
        return now_box["t"]

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        now_box["t"] += seconds

    with pytest.raises(_StopLoop):
        scheduler.run_scheduler_loop(
            states,
            interval_min=5,
            threshold=100,
            fallback_min=305,
            run_claude_fn=fake_run_claude,
            sleep_fn=fake_sleep,
            now_fn=fake_now,
            queue_path=tmp_path / "queue.json",
        )

    assert call_order == [2, 1]
    assert sleep_calls == [0, 100]  # account2: 100-100=0 대기, account1: 200-100=100 대기
```

- [ ] **Step 12: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'run_scheduler_loop'`

- [ ] **Step 13: (통합됨 — Step 11에서 이미 `tmp_path`를 사용하므로 별도 조치 불필요)**

- [ ] **Step 14: `run_scheduler_loop` 구현**

`scheduler.py`에 추가:

```python
import heapq


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
```

주: 힙에 오래된 항목이 섞일 수 있어(재시도 처리로 시각이 바뀐 경우) pop 시점에 `next_run_at` 일치 여부로 오래된 항목을 걸러낸다(lazy deletion 패턴). 매 반복 끝에서 살아있는 계정 전체를 다시 push하므로 힙 크기는 계정 수에 비례해 유지된다.

- [ ] **Step 15: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS (13 passed)

- [ ] **Step 16: 전체 테스트 스위트 실행**

Run: `cd ~/start_limit && python3 -m pytest tests/ -v`
Expected: 모든 테스트 PASS (`test_usage.py`, `test_accounts.py`, `test_scheduler.py` 합산)

- [ ] **Step 17: Commit**

```bash
cd ~/start_limit
git add scheduler.py tests/test_scheduler.py
git commit -m "feat: implement priority-queue scheduler main loop with retry-first pop handling"
```

---

## Task 5: CLI 진입점 — argparse, `claude` 서브프로세스 실행, `--check-subscription`/계정 관리 라우팅

**Files:**
- Modify: `scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: Task 4의 `initialize_states`, `run_scheduler_loop`, `QUEUE_PATH`; Task 2의 `accounts.add_account`, `accounts.list_accounts`, `accounts.remove_account`, `accounts.check_paid_subscription`, `accounts.AccountStatusError`
- Produces:
  - `parse_args(argv: list[str]) -> argparse.Namespace` — 기존 bash 플래그와 동일한 이름의 옵션 파싱. 계정번호는 위치 인자로 `nargs="*"`, 지정 안 하면 `[1]`.
  - `build_claude_command(model: str, effort: str, prompt: str) -> list[str]` — `["claude", "--dangerously-skip-permissions", "--model", model, "--effort", effort, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", prompt]`
  - `run_claude(account_id: int, model: str, effort: str) -> bool` — `build_claude_command`로 명령을 만들어 `CLAUDE_CONFIG_DIR`를 설정한 환경에서 `subprocess.run`으로 실행하고 stdout을 stream-json 파싱해 텍스트만 출력(로그 파일에도 기록), 성공 여부(exit code 0) 반환.
  - `main(argv: list[str] | None = None) -> int` — 전체 진입점. `--add-account`/`--list-accounts`/`--remove-account` 라우팅, `--check-subscription` 처리, 그 외에는 `initialize_states` + `run_scheduler_loop` 호출.

- [ ] **Step 1: `parse_args` 테스트 작성**

`tests/test_scheduler.py`에 추가:

```python
def test_parse_args_defaults_to_account_1_when_none_given():
    args = scheduler.parse_args([])
    assert args.account_ids == [1]
    assert args.model == "claude-haiku-4-5"
    assert args.effort == "low"
    assert args.interval == 5
    assert args.threshold == 100
    assert args.delay == 0
    assert args.check_subscription is False


def test_parse_args_accepts_multiple_account_ids():
    args = scheduler.parse_args(["1", "2", "3"])
    assert args.account_ids == [1, 2, 3]


def test_parse_args_parses_wait_until_and_options():
    args = scheduler.parse_args(["2", "-w", "14:00", "--model", "opus", "--effort", "high"])
    assert args.account_ids == [2]
    assert args.wait_until == "14:00"
    assert args.model == "opus"
    assert args.effort == "high"


def test_parse_args_account_management_flags():
    args = scheduler.parse_args(["--add-account", "5"])
    assert args.add_account == 5

    args = scheduler.parse_args(["--list-accounts"])
    assert args.list_accounts is True

    args = scheduler.parse_args(["--remove-account", "3"])
    assert args.remove_account == 3
```

- [ ] **Step 2: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k parse_args`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'parse_args'`

- [ ] **Step 3: `parse_args` 구현**

`scheduler.py` 상단에 `import argparse` 추가 후 다음 함수 추가:

```python
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
```

- [ ] **Step 4: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k parse_args`
Expected: PASS (4 passed)

- [ ] **Step 5: `build_claude_command` 테스트 작성**

`tests/test_scheduler.py`에 추가:

```python
def test_build_claude_command_shape():
    cmd = scheduler.build_claude_command("claude-haiku-4-5", "low", "Reply with OK.")
    assert cmd == [
        "claude",
        "--dangerously-skip-permissions",
        "--model", "claude-haiku-4-5",
        "--effort", "low",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "Reply with OK.",
    ]
```

- [ ] **Step 6: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k build_claude_command`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'build_claude_command'`

- [ ] **Step 7: `build_claude_command` 구현**

`scheduler.py`에 추가:

```python
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
```

- [ ] **Step 8: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k build_claude_command`
Expected: PASS (1 passed)

- [ ] **Step 9: `run_claude` 테스트 작성 (subprocess mock, stream-json 파싱)**

`tests/test_scheduler.py`에 추가:

```python
def test_run_claude_extracts_text_deltas_and_returns_true_on_success(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(accounts, "account_dir", lambda account_id: tmp_path / f"acct{account_id}")

    stream_lines = [
        _json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "OK"}}}),
        _json.dumps({"type": "other"}),
    ]

    class _FakeCompletedProcess:
        def __init__(self):
            self.stdout = "\n".join(stream_lines) + "\n"
            self.returncode = 0

    def fake_run(cmd, env, capture_output, text):
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(scheduler, "subprocess", subprocess)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id: log_dir)

    result = scheduler.run_claude(1, "claude-haiku-4-5", "low")

    assert result is True
    logged_files = list(log_dir.glob("loop-*.log"))
    assert len(logged_files) == 1
    assert logged_files[0].read_text() == "OK"


def test_run_claude_returns_false_on_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "account_dir", lambda account_id: tmp_path / f"acct{account_id}")

    class _FakeCompletedProcess:
        def __init__(self):
            self.stdout = ""
            self.returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda cmd, env, capture_output, text: _FakeCompletedProcess())
    monkeypatch.setattr(scheduler, "subprocess", subprocess)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id: log_dir)

    result = scheduler.run_claude(1, "claude-haiku-4-5", "low")

    assert result is False
```

이 테스트 파일 상단에 `import json as _json`과 `import subprocess`를 추가한다 (아직 없다면).

- [ ] **Step 10: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k run_claude`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'run_claude'` (및 `log_dir_for_account`)

- [ ] **Step 11: `log_dir_for_account`, `run_claude` 구현**

`scheduler.py` 상단에 `import subprocess`, `import os`, `import time as _time`, `from datetime import datetime` 추가 (없는 것만) 후 다음 함수 추가:

```python
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
```

- [ ] **Step 12: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k run_claude`
Expected: PASS (2 passed)

- [ ] **Step 13: `main` 테스트 작성 — `--check-subscription`, `--list-accounts`, `--add-account`, `--remove-account` 라우팅**

`tests/test_scheduler.py`에 추가:

```python
def test_main_check_subscription_prints_plan_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id: "pro")

    rc = scheduler.main(["1", "--check-subscription"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "pro" in captured.out


def test_main_check_subscription_reports_skip_for_free_plan(monkeypatch, capsys):
    def fake_check(account_id):
        raise accounts.AccountStatusError("유료 Claude 구독이 아님 (subscriptionType=free)")

    monkeypatch.setattr(accounts, "check_paid_subscription", fake_check)

    rc = scheduler.main(["2", "--check-subscription"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "SKIP" in captured.out


def test_main_list_accounts_routes_to_accounts_module(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        accounts,
        "list_accounts",
        lambda: [(1, "pro", tmp_path / ".claude")],
    )

    rc = scheduler.main(["--list-accounts"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "user1" in captured.out
    assert "pro" in captured.out


def test_main_add_account_routes_to_accounts_module(monkeypatch):
    called = {}

    def fake_add(account_id):
        called["account_id"] = account_id

    monkeypatch.setattr(accounts, "add_account", fake_add)

    rc = scheduler.main(["--add-account", "5"])

    assert rc == 0
    assert called["account_id"] == 5


def test_main_remove_account_routes_to_accounts_module(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "remove_account", lambda account_id: Path("/fake/backup"))

    rc = scheduler.main(["--remove-account", "2"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "backup" in captured.out


def test_main_remove_account_1_returns_nonzero(monkeypatch, capsys):
    def fake_remove(account_id):
        raise ValueError("user1은 제거할 수 없습니다")

    monkeypatch.setattr(accounts, "remove_account", fake_remove)

    rc = scheduler.main(["--remove-account", "1"])

    captured = capsys.readouterr()
    assert rc != 0
    assert "user1은 제거할 수 없습니다" in captured.err


def test_main_rejects_past_wait_until_without_entering_loop(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id: "pro")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("과거 --wait-until인데 스케줄러 루프가 시작됨")

    monkeypatch.setattr(scheduler, "run_scheduler_loop", fail_if_called)

    rc = scheduler.main(["1", "--wait-until", "2000-01-01 00:00"])

    captured = capsys.readouterr()
    assert rc != 0
    assert "이미 과거" in captured.err
```

- [ ] **Step 14: 테스트 실행하여 실패 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k main`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'main'`

- [ ] **Step 15: `main` 구현**

`scheduler.py`에 추가 (파일 맨 아래):

```python
import sys


def main(argv: list[str] | None = None) -> int:
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
    from datetime import datetime

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
```

- [ ] **Step 16: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k main`
Expected: PASS (7 passed)

- [ ] **Step 17: `_parse_wait_until` 형식·과거 시각 검증 테스트 작성**

`tests/test_scheduler.py`에 추가. `now`를 고정된 epoch로 명시적으로 넘겨 "미래/과거" 판정을 결정론적으로 만든다:

```python
def test_parse_wait_until_hh_mm_format_resolves_to_today():
    from datetime import datetime

    fixed_now_dt = datetime(2026, 9, 27, 9, 0, 0)
    now_epoch = int(fixed_now_dt.timestamp())

    epoch = scheduler._parse_wait_until("23:59", now_epoch)
    parsed = datetime.fromtimestamp(epoch)
    assert (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute) == (2026, 9, 27, 23, 59)


def test_parse_wait_until_full_datetime_format():
    from datetime import datetime

    now_epoch = int(datetime(2026, 1, 1, 0, 0, 0).timestamp())
    epoch = scheduler._parse_wait_until("2026-12-31 08:00", now_epoch)
    parsed = datetime.fromtimestamp(epoch)
    assert (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute) == (2026, 12, 31, 8, 0)


def test_parse_wait_until_invalid_format_raises():
    with pytest.raises(ValueError):
        scheduler._parse_wait_until("not-a-time", now=1_000_000)


def test_parse_wait_until_past_time_raises():
    from datetime import datetime

    now_epoch = int(datetime(2026, 9, 27, 23, 0, 0).timestamp())
    with pytest.raises(ValueError, match="이미 과거"):
        scheduler._parse_wait_until("09:00", now_epoch)
```

- [ ] **Step 18: 테스트 실행하여 통과 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/test_scheduler.py -v -k parse_wait_until`
Expected: PASS (4 passed) — 형식 파싱은 이미 Step 15에서 구현했으므로 회귀 확인이고, 과거 시각 거부는 Step 15에서 새로 추가한 로직을 검증한다.

- [ ] **Step 19: 전체 테스트 스위트 실행**

Run: `cd ~/start_limit && python3 -m pytest tests/ -v`
Expected: 전체 PASS

- [ ] **Step 20: 구문/타입 스모크 체크**

Run: `cd ~/start_limit && python3 -c "import scheduler; import accounts; import usage; print('imports ok')"`
Expected: `imports ok` 출력, 예외 없음

- [ ] **Step 21: 실제 `--check-subscription` 수동 검증 (기존 계정 활용)**

Run: `cd ~/start_limit && python3 scheduler.py 1 2 3 --check-subscription`
Expected: `[user1] 유료 구독 확인: pro`, `[user2] SKIP: ...`(무료), `[user3] 유료 구독 확인: max` 형태의 실제 출력 (실제 환경의 계정 상태에 따라 plan 이름은 다를 수 있음 — 핵심은 예외 없이 세 계정 모두에 대해 판정 결과가 출력되는지)

- [ ] **Step 22: Commit**

```bash
cd ~/start_limit
git add scheduler.py tests/test_scheduler.py
git commit -m "feat: add CLI entry point with account management routing and claude execution"
```

---

## Task 6: 기존 bash 자산 제거 및 문서/gitignore 정리

**Files:**
- Delete: `run-step-loop-claude.sh`
- Delete: `tests/test-loop-independence.sh`, `tests/test-claude-multi-account.sh`, `tests/test-claude-paid-gate.sh`, `tests/test-claude-account-manager.sh`, `tests/test-collect-usage.sh`
- Delete: `tests/bin/claude`, `tests/bin/curl`
- Delete: `tests/fixtures/usage.json`, `tests/fixtures/usage-not-exhausted.json`, `tests/fixtures/usage-both-exhausted.json` (및 존재한다면 `tests/fixtures/not-found.json`, `tests/fixtures/claude-profile/*`, `tests/fixtures/fake-home/*`)
- Keep (delete 대상 아님): `collect-usage.sh` — `run-step-loop.sh:112`(`COLLECT_USAGE="$SCRIPT_DIR/collect-usage.sh"`)가 여전히 참조하므로 삭제하면 Codex 경로가 깨진다. 이미 검증 완료(design.md 작성 시 `grep`으로 확인).
- Create: `.gitignore` (없다면), `.schedule/`를 무시 항목에 추가
- Modify: `docs/worklog/remove-step-dependency.md` — 이번 전환을 새 절로 추가

**Interfaces:**
- Consumes: 없음 (정리 작업)
- Produces: 없음

- [ ] **Step 1: 삭제 대상 파일 제거 (`collect-usage.sh`는 Codex가 참조하므로 제외)**

```bash
cd ~/start_limit
rm -f run-step-loop-claude.sh
rm -f tests/test-loop-independence.sh tests/test-claude-multi-account.sh \
      tests/test-claude-paid-gate.sh tests/test-claude-account-manager.sh \
      tests/test-collect-usage.sh
rm -f tests/bin/claude tests/bin/curl
rm -f tests/fixtures/usage.json tests/fixtures/usage-not-exhausted.json \
      tests/fixtures/usage-both-exhausted.json
rm -rf tests/fixtures/claude-profile tests/fixtures/fake-home
```

- [ ] **Step 2: 남은 파일 목록으로 정리 결과 확인**

Run: `cd ~/start_limit && find . -maxdepth 2 -name "*.sh" -o -maxdepth 2 -name "*.py" | sort`
Expected: `run-step-loop.sh`, `collect-usage.sh`(Codex 유지), `scheduler.py`, `accounts.py`, `usage.py`가 남아있고 `run-step-loop-claude.sh`는 없음.

- [ ] **Step 3: `.gitignore`에 `.schedule/` 추가**

`.gitignore` 파일이 없으면 새로 생성, 있으면 Edit으로 추가:

```
.schedule/
__pycache__/
*.pyc
```

- [ ] **Step 4: 전체 pytest 재실행하여 삭제로 인한 회귀가 없는지 확인**

Run: `cd ~/start_limit && python3 -m pytest tests/ -v`
Expected: 전체 PASS (bash 테스트 삭제로 인해 실행되던 스위트가 pytest 파일들로만 구성됨)

- [ ] **Step 5: `docs/worklog/remove-step-dependency.md`에 전환 기록 추가**

파일 끝(`## 최종 결과` 섹션 다음)에 새 섹션 추가:

```markdown

## 2026-09-27 우선순위 큐 스케줄러로 전환 (Python)

- 상태: 완료
- 배경: 계정을 여러 개 지정하면 `run-step-loop-claude.sh`가 계정별로 완전히 독립된
  백그라운드 프로세스를 띄우는 구조였다. 이를 단일 컨트롤러가 각 계정의 다음 실행
  시각을 `heapq` 기반 우선순위 큐로 관리하고, `claude` 호출을 완전 순차로 실행하는
  구조로 바꿨다.
- 설계 문서: `docs/superpowers/specs/2026-09-27-priority-queue-scheduler-design.md`
- 구현: `usage.py`(사용량 조회 및 다음 실행 시각 계산), `accounts.py`(계정 디렉터리
  규칙, 유료 구독 게이트, 계정 관리), `scheduler.py`(우선순위 큐 메인 루프 및 CLI
  진입점)를 Python으로 신규 작성했다.
- 무료 플랜 계정은 최초 1회 확인 후 큐에서 영구 제외하며 재확인하지 않는다.
- 사용량 조회 실패 계정은 `retry_pending` 상태로 유지되며, 큐에서 어떤 계정이든
  pop될 때마다 실패 이력 있는 계정들을 먼저 재조회한다. 5회 이상 연속 실패해도
  포기하지 않고 계속 재시도하되 로그 심각도만 올린다.
- 계정 자격증명 위치(`~/.claude`, `~/.claude-account-N`)는 변경하지 않았다.
- 삭제: `run-step-loop-claude.sh`와 관련 bash 테스트·모의 CLI 일체.
- 유지: `run-step-loop.sh`(Codex)는 이번 전환 범위 밖이다.
- Windows 네이티브 지원은 표준 라이브러리(`pathlib`, `subprocess`, `time`,
  `heapq`) 위주로 작성해 이식 가능성을 열어뒀으나, 실제 Windows 환경 검증은
  하지 않았다. `claude`/`codex` CLI 자체의 Windows `CLAUDE_CONFIG_DIR` 지원 여부는
  이 전환의 통제 범위 밖이다.
- 테스트: `tests/test_usage.py`, `tests/test_accounts.py`, `tests/test_scheduler.py`
  (pytest)로 전체 재작성. 전체 스위트 통과.
- 롤백: git 이력에서 `run-step-loop-claude.sh`와 관련 bash 테스트를 복원하고
  `scheduler.py`/`accounts.py`/`usage.py`, `.schedule/`를 제거한다.
```

- [ ] **Step 6: Commit**

```bash
cd ~/start_limit
git add -A
git commit -m "chore: remove legacy bash multi-account loop scripts and tests, replaced by Python scheduler"
```

---

## Task 7: 실제 계정으로 엔드투엔드 스모크 검증 (짧은 시간 창)

**Files:**
- 없음 (코드 변경 없이 검증만 수행)

**Interfaces:**
- Consumes: `scheduler.main`
- Produces: 없음

- [ ] **Step 1: `--wait-until`을 아주 가까운 미래(1~2분 뒤)로 주고 단일 계정으로 1회 사이클 관찰**

Run: `cd ~/start_limit && timeout 240 python3 scheduler.py 1 --wait-until "$(date -d '+90 seconds' '+%H:%M')" --interval 1`

Expected: 지정 시각까지 대기 후 `claude` 호출 1회 발생, 이후 사용량 조회 결과에 따라 다음 실행 시각이 계산되어 출력됨. `timeout 240`으로 무한 대기를 방지했으므로 240초 후 강제 종료되는 것은 정상이다 (스케줄러 자체가 무한루프이므로).

- [ ] **Step 2: 실행 중 `.schedule/queue.json`이 갱신되는지 확인**

Run (Step 1과 별도 터미널 또는 Step 1 종료 후): `cat ~/start_limit/.schedule/queue.json`
Expected: 계정 `1`의 `next_run_at`이 미래 시각으로, `status`가 `"scheduled"`로 기록되어 있음

- [ ] **Step 3: 여러 계정(무료 포함) 지정 시 무료 계정이 즉시 SKIP 처리되고 루프에서 제외되는지 실행 로그로 확인**

Run: `cd ~/start_limit && timeout 30 python3 scheduler.py 1 2 3 --check-subscription`
Expected: 세 계정 각각의 판정 결과가 즉시 출력되고 정상 종료(exit 0). (이 명령은 `--check-subscription`이므로 루프에 들어가지 않고 바로 종료된다.)

- [ ] **Step 4: 최종 확인 — 이 태스크는 커밋할 코드 변경이 없으므로 커밋 생략**

검증 결과를 사용자에게 보고한다. 문제가 발견되면 해당 태스크로 돌아가 수정 후 재검증한다.
