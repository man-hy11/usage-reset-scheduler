# 다중 계정 작업 루프 (`--project-loop`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `scheduler.py --project-loop --project-dir DIR`로 여러 유료 계정을 번갈아 쓰며 DIR의 `prompt.md` step 루프를 계속 돌린다. 한 계정의 한도가 차면 그 계정은 리셋 시각까지 쉬게 하고, 쓸 수 있는 다음 계정이 같은 step을 이어받는다.

**Architecture:** 새 모듈 두 개를 추가한다. `step_runner.py`는 claude/codex 한 번 실행과 출력 해석(STEP_STATUS, 한도 신호, 계정 오류)을 맡는다. `project_loop.py`는 계정 상태, 다음 계정 선택, 결과 판정, 메인 루프, 프로젝트 잠금을 맡는다. `scheduler.py`에는 옵션 3개와 `main()`의 분기 1개만 추가한다. 기존 함수(`fetch_usage`, `_check_plan`, `select_paid_account_ids` 등)는 수정하지 않고 주입해서 쓴다. `project_loop`는 `scheduler`를 import하지 않는다(순환 import 방지).

**Tech Stack:** Python 3.10+, 표준 라이브러리(`subprocess`, `fcntl`, `hashlib`, `json`, `re`), pytest.

**Spec:** `docs/superpowers/specs/2026-09-28-project-loop-design.md`

## Global Constraints

- 새 코드는 표준 라이브러리만 사용한다(새 의존성 없음).
- **기존 함수는 수정하지 않는다.** `scheduler.py`에서 바뀌는 곳은 `parse_args`에 옵션 3개 추가, 상수와 헬퍼 추가, `main()`의 `--project-loop` 분기 한 줄뿐이다.
- **기존 테스트(`tests/test_*.py` 183개)는 수정 없이 모두 통과해야 한다.**
- 테스트는 저장소 루트에서 `python3 -m pytest`로 실행한다(루트 모듈 import가 cwd 기준).
- 사용자에게 보이는 메시지는 한국어로 쓴다.
- 판정 상수: 실행 전 소진 판단 100%, STEP_STATUS 없을 때 교차 확인 95%, 알 수 없는 실패 3회 연속이면 종료, 계정 오류 재확인 60분, 재투입 시각은 리셋 + 60초.
- 작업 루프 기본 모델/effort는 `claude-opus-5-5` / `high`이고 **claude 계정에만** 적용한다. codex는 CLI 기본값을 쓴다.
- 로그 위치: `<DIR>/.claude-runs/` 또는 `<DIR>/.codex-runs/`에 `step-<YYYYmmdd-HHMMSS>-user<N>.log`(텍스트)와 `.raw.jsonl`(원본).
- 잠금 파일: `<저장소>/.schedule/locks/project-<sha1(realpath)[:12]>.lock`. 대상 프로젝트 안에는 파일을 만들지 않는다.
- 작업 루프는 `.schedule/queue.json`을 읽지도 쓰지도 않는다.
- 커밋 메시지 끝에는 반드시 다음 두 줄을 붙인다:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01NMqboR51vArC6Bu4k4JUSs
  ```

## Review Focus

- **STEP_STATUS 줄이 여러 스트림 조각으로 나뉘어 도착하는 경우**(`"STEP_STA"` + `"TUS: COMPLETE\n"`): 합친 텍스트에서 감지해야 한다. → Task 1 `test_step_status_split_across_deltas_is_detected`
- **codex의 연속된 agent_message:** 앞 메시지 끝과 `STEP_STATUS` 줄이 붙으면 `^...$` 매치가 깨진다. 메시지마다 줄바꿈을 붙여야 한다. → Task 1 `test_codex_messages_are_newline_separated`
- **stdout에 JSON이 아닌 줄(경고 등)이 섞인 경우:** 무시하고 계속 처리해야 하며, 원본 로그에는 남아야 한다. → Task 1 `test_non_json_lines_are_ignored`, Task 2 `test_run_step_streams_text_writes_logs_and_parses_status`
- **루프가 도는 중에 `prompt.md`를 고친 경우:** 다음 실행부터 새 내용을 써야 한다. → Task 6 `test_run_step_reads_prompt_fresh_and_uses_claude_defaults`
- **실행 중 Ctrl+C:** 자식 프로세스를 종료하고, 잠금을 풀고, 종료 코드 130으로 끝나야 한다. → Task 2 `test_stop_process_terminates_running_child`, Task 6 `test_ctrl_c_returns_130_and_releases_lock`
- (추가) **한도 신호의 리셋 시각이 이미 지났거나 없는 경우:** 사용량 조회, 그다음 `--interval` 순서로 대신 정해야 한다. → Task 5 `test_limit_reset_falls_back_to_usage_then_interval`

---

### Task 1: `step_runner` 출력 해석 (STEP_STATUS, 한도/계정 오류 신호)

**Files:**
- Create: `step_runner.py`
- Create: `tests/fixtures/claude_limit.jsonl`
- Create: `tests/fixtures/codex_limit.jsonl`
- Test: `tests/test_step_runner.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `step_runner.CONTINUE_STATUSES: tuple[str, ...] = ("COMPLETE", "FAILURE_ANALYSIS", "FAILURE_IMPROVEMENT")`
  - `@dataclass StepOutcome(step_status: str | None, limit_hit: bool = False, limit_reset_at: int | None = None, account_error: str | None = None, exit_code: int = 0, log_path: Path | None = None)`
  - `find_step_status(text: str) -> str | None`
  - `ClaudeEventReader()` / `CodexEventReader()`: `.feed(line: str) -> str`(화면에 보여 줄 텍스트 반환), 속성 `.limit_hit: bool`, `.limit_reset_at: int | None`, `.account_error: str | None`

- [ ] **Step 1: fixture 파일 작성** (2026-09-28에 실제로 캡처한 출력에서 판정에 필요한 이벤트만 남긴 것)

`tests/fixtures/claude_limit.jsonl` (3줄):
```
{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1790787600, "rateLimitType": "seven_day", "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled", "isUsingOverage": false}}
{"type": "assistant", "error": "rate_limit", "message": {"role": "assistant", "content": [{"type": "text", "text": "You've hit your weekly limit · resets Oct 1, 2am (Asia/Seoul)"}]}}
{"type": "result", "subtype": "success", "is_error": true, "api_error_status": 429, "terminal_reason": "api_error", "result": "You've hit your weekly limit · resets Oct 1, 2am (Asia/Seoul)"}
```

`tests/fixtures/codex_limit.jsonl` (5줄, 아포스트로피는 U+2019 `’`):
```
{"type":"thread.started","thread_id":"01a0e5f2-8990-77e0-a782-f4ab32dda1bf"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `gpt-test` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."}}
{"type":"turn.started"}
{"type":"error","message":"You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 2:57 PM."}
{"type":"turn.failed","error":{"message":"You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 2:57 PM."}}
```

- [ ] **Step 2: 실패하는 테스트 작성**

`tests/test_step_runner.py`:
```python
import json
from pathlib import Path

import step_runner

FIXTURES = Path(__file__).parent / "fixtures"


def _delta(text):
    return json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})


def _feed_all(reader, lines):
    return "".join(reader.feed(line) for line in lines)


def test_find_step_status_returns_last_full_line_match():
    text = "STEP_STATUS: FAILURE_ANALYSIS\nmore work\nSTEP_STATUS: COMPLETE\n"
    assert step_runner.find_step_status(text) == "COMPLETE"


def test_find_step_status_ignores_partial_lines_and_unknown_values():
    assert step_runner.find_step_status("note: STEP_STATUS: COMPLETE\n") is None
    assert step_runner.find_step_status("STEP_STATUS: DONE\n") is None
    assert step_runner.find_step_status("") is None


def test_claude_reader_returns_text_deltas_only():
    reader = step_runner.ClaudeEventReader()
    text = _feed_all(reader, [_delta("hello "), json.dumps({"type": "system", "subtype": "init"}), _delta("world")])
    assert text == "hello world"
    assert reader.limit_hit is False
    assert reader.account_error is None


def test_step_status_split_across_deltas_is_detected():
    reader = step_runner.ClaudeEventReader()
    text = _feed_all(reader, [_delta("done.\nSTEP_STA"), _delta("TUS: COMPLETE"), _delta("\n")])
    assert step_runner.find_step_status(text) == "COMPLETE"


def test_claude_reader_detects_limit_and_reset_from_real_output():
    reader = step_runner.ClaudeEventReader()
    _feed_all(reader, (FIXTURES / "claude_limit.jsonl").read_text().splitlines())
    assert reader.limit_hit is True
    assert reader.limit_reset_at == 1790787600
    assert reader.account_error is None


def test_claude_reader_ignores_allowed_rate_limit_events():
    reader = step_runner.ClaudeEventReader()
    reader.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "resetsAt": 1790787600}}))
    assert reader.limit_hit is False
    assert reader.limit_reset_at is None


def test_claude_reader_detects_429_result_without_rate_limit_event():
    reader = step_runner.ClaudeEventReader()
    reader.feed(json.dumps({"type": "result", "is_error": True, "api_error_status": 429}))
    assert reader.limit_hit is True
    assert reader.limit_reset_at is None


def test_claude_reader_detects_account_errors():
    for error in ("authentication_failed", "oauth_org_not_allowed"):
        reader = step_runner.ClaudeEventReader()
        reader.feed(json.dumps({"type": "assistant", "error": error, "message": {"content": []}}))
        assert reader.account_error == error
        assert reader.limit_hit is False


def test_non_json_lines_are_ignored():
    for reader in (step_runner.ClaudeEventReader(), step_runner.CodexEventReader()):
        assert reader.feed("WARNING: something\n") == ""
        assert reader.feed("[1, 2]\n") == ""
        assert reader.feed("\n") == ""
        assert reader.limit_hit is False


def test_codex_reader_returns_agent_messages():
    reader = step_runner.CodexEventReader()
    line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})
    assert reader.feed(line) == "hi\n"


def test_codex_messages_are_newline_separated():
    reader = step_runner.CodexEventReader()
    lines = [
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "working"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "STEP_STATUS: COMPLETE"}}),
    ]
    assert step_runner.find_step_status(_feed_all(reader, lines)) == "COMPLETE"


def test_codex_reader_detects_limit_from_real_output():
    reader = step_runner.CodexEventReader()
    text = _feed_all(reader, (FIXTURES / "codex_limit.jsonl").read_text(encoding="utf-8").splitlines())
    assert reader.limit_hit is True
    assert reader.limit_reset_at is None
    assert reader.account_error is None
    assert text == ""


def test_codex_reader_detects_auth_errors():
    reader = step_runner.CodexEventReader()
    reader.feed(json.dumps({"type": "turn.failed", "error": {"message": "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again."}}))
    assert reader.account_error == "codex_auth"
    assert reader.limit_hit is False


def test_codex_reader_ignores_non_limit_item_errors():
    reader = step_runner.CodexEventReader()
    reader.feed(json.dumps({"type": "item.completed", "item": {"type": "error", "message": "Model metadata not found"}}))
    assert reader.limit_hit is False
    assert reader.account_error is None
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `python3 -m pytest tests/test_step_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'step_runner'`

- [ ] **Step 4: 구현**

`step_runner.py`:
```python
"""Run one project step with a claude/codex account and interpret its output."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

CONTINUE_STATUSES = ("COMPLETE", "FAILURE_ANALYSIS", "FAILURE_IMPROVEMENT")
_STEP_STATUS_RE = re.compile(
    r"^STEP_STATUS: (COMPLETE|FAILURE_ANALYSIS|FAILURE_IMPROVEMENT|INCOMPLETE)$",
    re.MULTILINE,
)
_CLAUDE_ACCOUNT_ERRORS = ("authentication_failed", "oauth_org_not_allowed")
_CODEX_AUTH_MARKERS = ("sign in again", "log out")


@dataclass
class StepOutcome:
    step_status: str | None
    limit_hit: bool = False
    limit_reset_at: int | None = None
    account_error: str | None = None
    exit_code: int = 0
    log_path: Path | None = None


def find_step_status(text: str) -> str | None:
    matches = _STEP_STATUS_RE.findall(text)
    return matches[-1] if matches else None


def _load_event(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


class ClaudeEventReader:
    """Consumes `claude -p --output-format stream-json` lines one at a time."""

    def __init__(self) -> None:
        self.limit_hit = False
        self.limit_reset_at: int | None = None
        self.account_error: str | None = None

    def feed(self, line: str) -> str:
        event = _load_event(line)
        if event is None:
            return ""
        kind = event.get("type")
        if kind == "stream_event":
            inner = event.get("event")
            delta = inner.get("delta") if isinstance(inner, dict) else None
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return str(delta.get("text", ""))
        elif kind == "rate_limit_event":
            # 정상 실행에도 status가 "allowed"/"allowed_warning"인 이벤트가 오므로 rejected만 본다.
            info = event.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                self.limit_hit = True
                resets_at = info.get("resetsAt")
                if isinstance(resets_at, (int, float)):
                    self.limit_reset_at = int(resets_at)
        elif kind == "assistant":
            error = event.get("error")
            if error == "rate_limit":
                self.limit_hit = True
            elif error in _CLAUDE_ACCOUNT_ERRORS:
                self.account_error = error
        elif kind == "result" and event.get("api_error_status") == 429:
            self.limit_hit = True
        return ""


class CodexEventReader:
    """Consumes `codex exec --json` lines one at a time."""

    def __init__(self) -> None:
        self.limit_hit = False
        self.limit_reset_at: int | None = None  # codex는 "try again at 2:57 PM"처럼 시각만 알려 준다
        self.account_error: str | None = None

    def feed(self, line: str) -> str:
        event = _load_event(line)
        if event is None:
            return ""
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                # 메시지마다 줄을 끊어야 마지막 메시지의 STEP_STATUS 줄이 ^...$에 걸린다.
                return item["text"] + "\n"
            return ""
        message = None
        if kind == "error":
            message = event.get("message")
        elif kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str):
            # 문구의 아포스트로피가 U+2019라서 문장 전체가 아니라 핵심 단어로 판정한다.
            lowered = message.lower()
            if "usage limit" in lowered:
                self.limit_hit = True
            elif any(marker in lowered for marker in _CODEX_AUTH_MARKERS):
                self.account_error = "codex_auth"
        return ""
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_step_runner.py -q`
Expected: PASS (14 passed)

- [ ] **Step 6: 커밋**

```bash
git add step_runner.py tests/test_step_runner.py tests/fixtures/claude_limit.jsonl tests/fixtures/codex_limit.jsonl
git commit -m "$(cat <<'EOF'
feat: parse STEP_STATUS, usage-limit and account-error signals from claude/codex output

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01NMqboR51vArC6Bu4k4JUSs
EOF
)"
```

---

### Task 2: `step_runner.run_step` (명령 구성, 실시간 스트리밍, 로그)

**Files:**
- Modify: `step_runner.py` (import와 함수 추가)
- Test: `tests/test_step_runner.py` (테스트 추가)

**Interfaces:**
- Consumes: Task 1의 `StepOutcome`, `ClaudeEventReader`, `CodexEventReader`, `find_step_status`; `accounts.account_dir(account_id: int, tool: str) -> Path`
- Produces:
  - `build_command(tool: str, project_dir: Path, prompt_text: str, model: str, effort: str) -> list[str]`
  - `build_env(account_id: int, tool: str) -> dict`
  - `run_log_dir(project_dir: Path, tool: str) -> Path`
  - `stop_process(proc: subprocess.Popen, grace_sec: float = 5) -> None`
  - `run_step(account_id: int, tool: str, project_dir: Path, prompt_text: str, model: str, effort: str, *, out=None) -> StepOutcome`

- [ ] **Step 1: 실패하는 테스트 추가** (`tests/test_step_runner.py` 맨 위 import에 `import re`, `import subprocess`, `import sys`를 추가하고, 파일 끝에 아래를 추가)

```python
def _fake_cli(monkeypatch, lines, exit_code=0):
    script = (
        "import sys\n"
        f"lines = {lines!r}\n"
        "for line in lines:\n"
        "    print(line, flush=True)\n"
        f"sys.exit({exit_code})\n"
    )
    monkeypatch.setattr(step_runner, "build_command", lambda *args, **kwargs: [sys.executable, "-c", script])


def _raw_path(log_path):
    return log_path.parent / (log_path.stem + ".raw.jsonl")


def test_build_command_claude_uses_model_effort_and_no_strict_mcp(tmp_path):
    cmd = step_runner.build_command("claude", tmp_path, "PROMPT", "claude-opus-5-5", "high")
    assert cmd[0] == "claude"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert "--strict-mcp-config" not in cmd
    assert cmd[-1] == "PROMPT"
    assert "stream-json" in cmd


def test_build_command_codex_ignores_model_and_sets_project_dir(tmp_path):
    cmd = step_runner.build_command("codex", tmp_path, "PROMPT", "claude-opus-5-5", "high")
    assert cmd[:3] == ["codex", "exec", "--json"]
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert "--model" not in cmd
    assert "claude-opus-5-5" not in cmd
    assert cmd[-1] == "PROMPT"


def test_build_env_points_tool_at_account_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(step_runner.accounts.Path, "home", lambda: tmp_path)
    claude_env = step_runner.build_env(3, "claude")
    codex_env = step_runner.build_env(4, "codex")
    assert claude_env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-3")
    assert codex_env["CODEX_HOME"] == str(tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-4")


def test_run_step_streams_text_writes_logs_and_parses_status(monkeypatch, tmp_path, capsys):
    _fake_cli(monkeypatch, [_delta("working...\n"), "not json", _delta("STEP_STATUS: COMPLETE\n")])
    outcome = step_runner.run_step(2, "claude", tmp_path, "PROMPT", "m", "e")

    assert outcome.step_status == "COMPLETE"
    assert outcome.exit_code == 0
    assert outcome.limit_hit is False
    assert outcome.log_path.parent == tmp_path / ".claude-runs"
    assert re.fullmatch(r"step-\d{8}-\d{6}-user2\.log", outcome.log_path.name)
    assert outcome.log_path.read_text() == "working...\nSTEP_STATUS: COMPLETE\n"
    assert "not json" in _raw_path(outcome.log_path).read_text()
    assert "working..." in capsys.readouterr().out


def test_run_step_detects_claude_limit(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, (FIXTURES / "claude_limit.jsonl").read_text().splitlines(), exit_code=1)
    outcome = step_runner.run_step(1, "claude", tmp_path, "PROMPT", "m", "e")
    assert outcome.step_status is None
    assert outcome.limit_hit is True
    assert outcome.limit_reset_at == 1790787600
    assert outcome.exit_code == 1


def test_run_step_codex_logs_to_codex_runs_and_detects_limit(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, (FIXTURES / "codex_limit.jsonl").read_text(encoding="utf-8").splitlines(), exit_code=1)
    outcome = step_runner.run_step(4, "codex", tmp_path, "PROMPT", "m", "e")
    assert outcome.log_path.parent == tmp_path / ".codex-runs"
    assert outcome.limit_hit is True
    assert outcome.exit_code == 1


def test_run_step_connects_stdin_to_devnull(monkeypatch, tmp_path):
    # stdin이 DEVNULL이면 select가 즉시 읽을 수 있다고 알리고 read()는 ''를 돌려준다.
    script = (
        "import json, select, sys\n"
        "ready = select.select([sys.stdin], [], [], 0.5)[0]\n"
        "data = sys.stdin.read() if ready else 'BLOCKED'\n"
        "print(json.dumps({'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': 'stdin=' + repr(data)}}}), flush=True)\n"
    )
    monkeypatch.setattr(step_runner, "build_command", lambda *args, **kwargs: [sys.executable, "-c", script])
    outcome = step_runner.run_step(1, "claude", tmp_path, "PROMPT", "m", "e")
    assert outcome.log_path.read_text() == "stdin=''"


def test_stop_process_terminates_running_child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    step_runner.stop_process(proc, grace_sec=5)
    assert proc.poll() is not None
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m pytest tests/test_step_runner.py -q`
Expected: FAIL — `AttributeError: module 'step_runner' has no attribute 'build_command'` 등

- [ ] **Step 3: 구현** (`step_runner.py` 상단 import를 아래로 교체하고, 파일 끝에 함수들을 추가)

import 교체:
```python
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import accounts
```

파일 끝에 추가:
```python
def build_command(tool: str, project_dir: Path, prompt_text: str, model: str, effort: str) -> list[str]:
    if tool == "codex":
        # codex는 CLI 기본 모델/effort를 쓴다(run-step-loop.sh와 동일).
        return ["codex", "exec", "--json", "--sandbox", "danger-full-access", "-C", str(project_dir), prompt_text]
    return [
        "claude", "--dangerously-skip-permissions",
        "--model", model,
        "--effort", effort,
        "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        prompt_text,
    ]


def build_env(account_id: int, tool: str) -> dict:
    env = os.environ.copy()
    if tool == "codex":
        env["CODEX_HOME"] = str(accounts.account_dir(account_id, "codex"))
    else:
        env["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(account_id, "claude"))
    return env


def run_log_dir(project_dir: Path, tool: str) -> Path:
    return project_dir / (".codex-runs" if tool == "codex" else ".claude-runs")


def stop_process(proc: subprocess.Popen, grace_sec: float = 5) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_step(
    account_id: int,
    tool: str,
    project_dir: Path,
    prompt_text: str,
    model: str,
    effort: str,
    *,
    out=None,
) -> StepOutcome:
    out = out if out is not None else sys.stdout
    log_dir = run_log_dir(project_dir, tool)
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = f"step-{datetime.now():%Y%m%d-%H%M%S}-user{account_id}"
    log_path = log_dir / f"{stem}.log"
    raw_path = log_dir / f"{stem}.raw.jsonl"
    reader = CodexEventReader() if tool == "codex" else ClaudeEventReader()
    text_parts: list[str] = []

    with open(raw_path, "w", encoding="utf-8") as raw_file, open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            build_command(tool, project_dir, prompt_text, model, effort),
            cwd=project_dir,
            env=build_env(account_id, tool),
            # codex는 stdin이 열려 있으면 추가 입력을 기다린다.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            for line in proc.stdout:
                raw_file.write(line)
                text = reader.feed(line)
                if text:
                    text_parts.append(text)
                    log_file.write(text)
                    log_file.flush()
                    out.write(text)
                    out.flush()
            exit_code = proc.wait()
        except BaseException:
            stop_process(proc)
            raise

    out.write("\n")
    out.flush()
    return StepOutcome(
        step_status=find_step_status("".join(text_parts)),
        limit_hit=reader.limit_hit,
        limit_reset_at=reader.limit_reset_at,
        account_error=reader.account_error,
        exit_code=exit_code,
        log_path=log_path,
    )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_step_runner.py -q`
Expected: PASS (22 passed)

- [ ] **Step 5: 커밋**

```bash
git add step_runner.py tests/test_step_runner.py
git commit -m "$(cat <<'EOF'
feat: run a project step with live streaming, text/raw logs and stdin closed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01NMqboR51vArC6Bu4k4JUSs
EOF
)"
```

---

### Task 3: `project_loop` 계정 상태, 소진 판단, 초기화, 상태 출력

**Files:**
- Create: `project_loop.py`
- Test: `tests/test_project_loop.py`

**Interfaces:**
- Consumes: Task 1의 `step_runner.CONTINUE_STATUSES`, `step_runner.StepOutcome`
- Produces:
  - 상수 `RESET_GRACE_SEC = 60`, `ACCOUNT_RECHECK_SEC = 3600`, `AMBIGUOUS_LIMIT_PERCENT = 95`, `UNKNOWN_FAIL_LIMIT = 3`
  - `@dataclass LoopAccount(account_id: int, tool: str, available_at: int, status: str, reason: str | None = None, runs: int = 0)`. `status`는 `"ready" | "exhausted" | "account_error"`
  - `usage_block(status: dict, now: int, interval_min: int, min_percent: float = 100) -> tuple[int, str] | None`
  - `build_loop_accounts(account_ids: list[int], get_tool, plans: dict[int, str | None], fetch_usage_fn, *, start_at: int, now: int, interval_min: int, print_fn=print) -> list[LoopAccount]`
  - `format_status(loop_accounts: list[LoopAccount], current: LoopAccount, now: int) -> str`
  - 내부 헬퍼 `_fmt(epoch: int) -> str`, `_safe_usage(fetch_usage_fn, account: LoopAccount, print_fn) -> dict | None`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_project_loop.py`:
```python
import project_loop
from project_loop import LoopAccount

NOW = 1_000_000


def usage(five=10, week=10, five_reset=None, week_reset=None):
    return {
        "five_hour_used_percent": five,
        "five_hour_reset_at": five_reset,
        "weekly_used_percent": week,
        "weekly_reset_at": week_reset,
    }


def test_usage_block_weekly_exhausted_uses_weekly_reset():
    assert project_loop.usage_block(usage(week=100, week_reset=NOW + 5000, five=100, five_reset=NOW + 100), NOW, 5) == (NOW + 5060, "주간 한도")


def test_usage_block_five_hour_exhausted_uses_five_hour_reset():
    assert project_loop.usage_block(usage(five=100, five_reset=NOW + 700), NOW, 5) == (NOW + 760, "5시간 한도")


def test_usage_block_below_threshold_is_none():
    assert project_loop.usage_block(usage(five=99.9, week=99.9), NOW, 5) is None
    assert project_loop.usage_block(usage(five=None, week=None), NOW, 5) is None


def test_usage_block_missing_or_past_reset_retries_after_interval():
    assert project_loop.usage_block(usage(five=100, five_reset=None), NOW, 5) == (NOW + 300, "5시간 한도")
    assert project_loop.usage_block(usage(week=100, week_reset=NOW - 10), NOW, 5) == (NOW + 300, "주간 한도")


def test_usage_block_custom_min_percent():
    assert project_loop.usage_block(usage(five=97, five_reset=NOW + 700), NOW, 5, min_percent=95) == (NOW + 760, "5시간 한도")


def test_build_loop_accounts_classifies_ready_exhausted_and_unpaid():
    usages = {1: usage(), 2: usage(week=100, week_reset=NOW + 9000)}
    result = project_loop.build_loop_accounts(
        [1, 2, 3],
        get_tool=lambda account_id: "codex" if account_id == 2 else "claude",
        plans={1: "pro", 2: "plus", 3: None},
        fetch_usage_fn=lambda account_id, tool: usages[account_id],
        start_at=NOW,
        now=NOW,
        interval_min=5,
        print_fn=lambda *args: None,
    )
    assert result[0] == LoopAccount(1, "claude", NOW, "ready")
    assert result[1] == LoopAccount(2, "codex", NOW + 9060, "exhausted", "주간 한도")
    assert result[2] == LoopAccount(3, "claude", NOW + project_loop.ACCOUNT_RECHECK_SEC, "account_error", "유료 구독 아님")


def test_build_loop_accounts_respects_later_start_and_survives_usage_errors():
    def failing_usage(account_id, tool):
        raise RuntimeError("429")

    result = project_loop.build_loop_accounts(
        [1], get_tool=lambda account_id: "claude", plans={1: "pro"}, fetch_usage_fn=failing_usage,
        start_at=NOW + 600, now=NOW, interval_min=5, print_fn=lambda *args: None,
    )
    assert result[0] == LoopAccount(1, "claude", NOW + 600, "ready")


def test_format_status_lists_current_first_then_others():
    current = LoopAccount(2, "claude", NOW, "ready", runs=3)
    others = [
        LoopAccount(1, "claude", NOW + 9000, "exhausted", "주간 한도"),
        LoopAccount(3, "claude", NOW, "ready"),
        LoopAccount(4, "codex", NOW + 3600, "account_error", "codex_auth"),
    ]
    text = project_loop.format_status(others + [current], current, NOW)
    lines = text.splitlines()
    assert lines[1].startswith("▶ 실행: user2 [claude")
    assert "연속 3회차" in lines[1]
    assert "user3 [claude" in lines[2] and "대기" in lines[2]
    assert "user4 [codex" in lines[3] and "계정 오류(codex_auth)" in lines[3]
    assert "user1 [claude" in lines[4] and "소진(주간 한도)" in lines[4]
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m pytest tests/test_project_loop.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'project_loop'`

- [ ] **Step 3: 구현**

`project_loop.py`:
```python
"""Rotate accounts through a project's prompt loop, switching accounts on usage limits."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_project_loop.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: 커밋**

```bash
git add project_loop.py tests/test_project_loop.py
git commit -m "$(cat <<'EOF'
feat: add project-loop account state, exhaustion check and status display

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01NMqboR51vArC6Bu4k4JUSs
EOF
)"
```

---

### Task 4: 프로젝트 잠금 (`flock`)

**Files:**
- Modify: `project_loop.py` (import와 함수 추가)
- Test: `tests/test_project_loop.py` (테스트 추가)

**Interfaces:**
- Consumes: 없음
- Produces:
  - `class ProjectLockError(Exception)`: `str(exc)`는 잠금을 쥐고 있는 쪽의 정보
  - `lock_path_for(project_dir: Path, lock_root: Path) -> Path`
  - `acquire_project_lock(lock_path: Path) -> int` (fd 반환, 이미 잠겨 있으면 `ProjectLockError`)
  - `write_lock_info(fd: int, info: str) -> None`
  - `release_project_lock(fd: int) -> None`

- [ ] **Step 1: 실패하는 테스트 추가** (`tests/test_project_loop.py` 맨 위에 `import pytest`를 추가하고, 파일 끝에 아래를 추가)

```python
def test_lock_blocks_second_holder_and_reports_first_holder_info(tmp_path):
    path = project_loop.lock_path_for(tmp_path / "proj", tmp_path / "locks")
    fd = project_loop.acquire_project_lock(path)
    project_loop.write_lock_info(fd, "PID 123, 시작 2026-09-28 12:00:00, 계정 user2")
    try:
        with pytest.raises(project_loop.ProjectLockError) as exc_info:
            project_loop.acquire_project_lock(path)
        assert "PID 123" in str(exc_info.value)
    finally:
        project_loop.release_project_lock(fd)

    fd_again = project_loop.acquire_project_lock(path)
    project_loop.release_project_lock(fd_again)


def test_lock_info_is_replaced_not_appended(tmp_path):
    path = project_loop.lock_path_for(tmp_path / "proj", tmp_path / "locks")
    fd = project_loop.acquire_project_lock(path)
    project_loop.write_lock_info(fd, "a much longer first line of lock info")
    project_loop.write_lock_info(fd, "short")
    project_loop.release_project_lock(fd)
    assert path.read_text() == "short"


def test_lock_path_is_same_for_symlinked_and_relative_paths(tmp_path, monkeypatch):
    real = tmp_path / "proj"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.chdir(tmp_path)
    locks = tmp_path / "locks"
    assert project_loop.lock_path_for(link, locks) == project_loop.lock_path_for(real, locks)
    assert project_loop.lock_path_for(project_loop.Path("proj"), locks) == project_loop.lock_path_for(real, locks)


def test_lock_on_different_projects_is_independent(tmp_path):
    locks = tmp_path / "locks"
    fd_a = project_loop.acquire_project_lock(project_loop.lock_path_for(tmp_path / "a", locks))
    fd_b = project_loop.acquire_project_lock(project_loop.lock_path_for(tmp_path / "b", locks))
    project_loop.release_project_lock(fd_a)
    project_loop.release_project_lock(fd_b)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m pytest tests/test_project_loop.py -q`
Expected: FAIL — `AttributeError: module 'project_loop' has no attribute 'lock_path_for'`

- [ ] **Step 3: 구현** (`project_loop.py`의 import 블록을 아래로 교체하고, 파일 끝에 함수들을 추가)

import 교체:
```python
from __future__ import annotations

import fcntl
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from step_runner import CONTINUE_STATUSES, StepOutcome
```

파일 끝에 추가:
```python
class ProjectLockError(Exception):
    """이미 다른 작업 루프가 같은 프로젝트를 잠그고 있다. str()은 그쪽이 기록한 정보."""


def lock_path_for(project_dir: Path, lock_root: Path) -> Path:
    digest = hashlib.sha1(str(Path(project_dir).resolve()).encode("utf-8")).hexdigest()[:12]
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
    return fd


def write_lock_info(fd: int, info: str) -> None:
    os.ftruncate(fd, 0)
    os.pwrite(fd, info.encode("utf-8"), 0)


def release_project_lock(fd: int) -> None:
    os.close(fd)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_project_loop.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: 커밋**

```bash
git add project_loop.py tests/test_project_loop.py
git commit -m "$(cat <<'EOF'
feat: add per-project flock so two project loops cannot edit one repo at once

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01NMqboR51vArC6Bu4k4JUSs
EOF
)"
```

---

### Task 5: `run_project_loop` 메인 루프 (계정 유지/교체, 대기, 결과 판정)

**Files:**
- Modify: `project_loop.py` (함수 추가)
- Test: `tests/test_project_loop.py` (테스트 추가)

**Interfaces:**
- Consumes: Task 3의 `LoopAccount`, `usage_block`, `format_status`, `_safe_usage`, `_fmt`, 상수들; Task 1의 `StepOutcome`, `CONTINUE_STATUSES`
- Produces:
  - `run_project_loop(loop_accounts: list[LoopAccount], *, run_step_fn, fetch_usage_fn, check_plan_fn, sleep_fn, now_fn, interval_min: int, print_fn=print) -> int`
    - `run_step_fn(account: LoopAccount) -> StepOutcome`
    - `fetch_usage_fn(account_id: int, tool: str) -> dict` (예외를 던질 수 있음)
    - `check_plan_fn(account_id: int, tool: str) -> str | None`
    - 반환값: 종료 코드. 정상 동작 중에는 반환하지 않고, 종료 조건에서만 `1`을 반환한다.

- [ ] **Step 1: 실패하는 테스트 추가** (`tests/test_project_loop.py` 맨 위에 `from step_runner import StepOutcome`을 추가하고, 파일 끝에 아래를 추가)

```python
RUN_SECONDS = 600


class Harness:
    """가짜 시계와 미리 정한 실행 결과로 run_project_loop를 돌린다."""

    def __init__(self, account_ids, outcomes, usages=None, plans=None, tools=None):
        self.now = NOW
        self.outcomes = list(outcomes)
        self.usages = usages or {}
        self.plans = plans or {}
        self.ran = []
        self.sleeps = []
        self.output = []
        tools = tools or {}
        self.accounts = [LoopAccount(i, tools.get(i, "claude"), NOW, "ready") for i in account_ids]

    def run_step(self, account):
        self.ran.append(account.account_id)
        self.now += RUN_SECONDS
        return self.outcomes.pop(0) if self.outcomes else StepOutcome(step_status="INCOMPLETE")

    def fetch_usage(self, account_id, tool):
        value = self.usages.get(account_id, usage())
        if callable(value):
            return value()
        if isinstance(value, Exception):
            raise value
        return value

    def check_plan(self, account_id, tool):
        value = self.plans.get(account_id, "pro")
        return value() if callable(value) else value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += int(seconds)

    def run(self):
        return project_loop.run_project_loop(
            self.accounts,
            run_step_fn=self.run_step,
            fetch_usage_fn=self.fetch_usage,
            check_plan_fn=self.check_plan,
            sleep_fn=self.sleep,
            now_fn=lambda: self.now,
            interval_min=5,
            print_fn=self.output.append,
        )

    def account(self, account_id):
        return next(a for a in self.accounts if a.account_id == account_id)


def sequence(*values):
    """호출할 때마다 다음 값을 돌려주고, 마지막 값은 계속 반복한다."""
    remaining = list(values)

    def next_value():
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return next_value


def ok(status="COMPLETE"):
    return StepOutcome(step_status=status)


def limit(reset_at=None):
    return StepOutcome(step_status=None, limit_hit=True, limit_reset_at=reset_at, exit_code=1)


def unknown():
    return StepOutcome(step_status=None, exit_code=1)


def test_same_account_is_kept_until_its_limit_then_next_account_continues():
    h = Harness([1, 2], [ok(), ok("FAILURE_ANALYSIS"), limit(NOW + 50_000), ok()])
    assert h.run() == 1  # 마지막은 기본 INCOMPLETE로 종료
    assert h.ran == [1, 1, 1, 2, 2]
    assert h.account(1).status == "exhausted"
    assert h.account(1).available_at == NOW + 50_060


def test_incomplete_stops_immediately():
    h = Harness([1, 2], [ok("INCOMPLETE")])
    assert h.run() == 1
    assert h.ran == [1]
    assert any("INCOMPLETE" in line for line in h.output)


def test_next_account_already_exhausted_is_skipped_before_running():
    h = Harness(
        [1, 2, 3],
        [limit(NOW + 50_000)],
        usages={2: usage(week=100, week_reset=NOW + 90_000)},
    )
    h.run()
    assert h.ran == [1, 3]
    assert h.account(2).status == "exhausted"
    assert h.account(2).available_at == NOW + 90_060


def test_waits_for_soonest_reset_when_every_account_is_exhausted():
    h = Harness([1, 2], [limit(NOW + 3000), limit(NOW + 2000)])
    h.run()
    assert h.ran == [1, 2, 2]
    # 1번 실행 → +600, 2번 실행 → +1200. 가장 빠른 재투입은 2번 계정의 NOW+2060.
    assert h.sleeps == [NOW + 2060 - (NOW + 2 * RUN_SECONDS)]
    assert any("사용 가능한 계정 없음" in line for line in h.output)


def test_unknown_failures_retry_same_account_and_stop_after_three_in_a_row():
    h = Harness([1, 2], [unknown(), unknown(), ok(), unknown(), unknown(), unknown()])
    assert h.run() == 1
    assert h.ran == [1, 1, 1, 1, 1, 1]
    assert any("알 수 없는 실패" in line for line in h.output)


def test_missing_status_with_high_usage_is_treated_as_exhausted():
    # 실행 전 확인에서는 여유가 있고, 실행 후 교차 확인에서는 97%.
    h = Harness([1, 2], [unknown()], usages={1: sequence(usage(), usage(five=97, five_reset=NOW + 5000))})
    h.run()
    assert h.ran == [1, 2]
    assert h.account(1).status == "exhausted"
    assert h.account(1).available_at == NOW + 5060


def test_limit_reset_falls_back_to_usage_then_interval():
    # 신호에 리셋 시각이 없으면 사용량 조회 결과를 쓴다(실행 전 확인에서는 아직 여유가 있었음).
    h = Harness([1, 2], [limit(None)], usages={1: sequence(usage(), usage(week=100, week_reset=NOW + 70_000))})
    h.run()
    assert h.account(1).available_at == NOW + 70_060

    # 신호의 리셋 시각이 이미 지났고 사용량도 소진이 아니면 --interval 뒤에 다시 본다.
    h = Harness([1, 2], [limit(NOW - 100)])
    h.run()
    assert h.account(1).available_at == NOW + RUN_SECONDS + 300


def test_account_error_is_parked_and_rejoins_after_plan_recheck():
    h = Harness(
        [1, 2],
        [StepOutcome(step_status=None, account_error="authentication_failed", exit_code=1), limit(NOW + 100_000)],
    )
    h.run()
    assert h.ran == [1, 2, 1]
    assert h.account(1).status == "ready"
    assert any("계정 오류(authentication_failed)" in line for line in h.output)


def test_account_error_stays_parked_while_plan_check_fails():
    # 1번은 플랜 확인이 계속 실패해 매시간 재확인만 되고 실행되지 않는다.
    # 2번이 리셋 후 돌아와 기본 INCOMPLETE 결과로 루프가 끝난다.
    h = Harness(
        [1, 2],
        [StepOutcome(step_status=None, account_error="oauth_org_not_allowed", exit_code=1), limit(NOW + 100_000)],
        plans={1: None},
    )
    h.run()
    assert h.ran == [1, 2, 2]
    assert h.account(1).status == "account_error"
    assert sum("계정 오류 지속" in line for line in h.output) >= 2


def test_usage_errors_before_run_do_not_block_running():
    h = Harness([1], [ok("INCOMPLETE")], usages={1: RuntimeError("usage api down")})
    assert h.run() == 1
    assert h.ran == [1]
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m pytest tests/test_project_loop.py -q`
Expected: FAIL — `AttributeError: module 'project_loop' has no attribute 'run_project_loop'`

- [ ] **Step 3: 구현** (`project_loop.py` 끝에 추가)

```python
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

    while True:
        now = now_fn()
        if current is None or current.available_at > now:
            current = _pick_next(loop_accounts, now)
            if current is None:
                soonest = min(loop_accounts, key=lambda account: (account.available_at, account.account_id))
                print_fn(f"[{_fmt(now)}] 사용 가능한 계정 없음 → user{soonest.account_id} 재개 {_fmt(soonest.available_at)}까지 대기")
                sleep_fn(soonest.available_at - now)
                continue

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
        block = usage_block(status, now, interval_min, min_percent=AMBIGUOUS_LIMIT_PERCENT) if status is not None else None
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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_project_loop.py -q`
Expected: PASS (22 passed)

- [ ] **Step 5: 커밋**

```bash
git add project_loop.py tests/test_project_loop.py
git commit -m "$(cat <<'EOF'
feat: add project loop that keeps an account until its limit, then hands the step on

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01NMqboR51vArC6Bu4k4JUSs
EOF
)"
```

---

### Task 6: `scheduler.py` 연결 (옵션, 검증, 잠금, 계정 선택, Ctrl+C)

**Files:**
- Modify: `scheduler.py` — `parse_args`(옵션 3개 추가), 모듈 상수 추가, 헬퍼 함수 2개 추가, `main()`에 분기 한 줄 추가
- Test: `tests/test_scheduler_project_loop.py` (새 파일, 기존 `tests/test_scheduler.py`는 건드리지 않음)

**Interfaces:**
- Consumes: Task 2의 `step_runner.run_step`; Task 3~5의 `project_loop.build_loop_accounts`, `lock_path_for`, `acquire_project_lock`, `write_lock_info`, `release_project_lock`, `ProjectLockError`, `run_project_loop`; 기존 `fetch_usage`, `_check_plan`, `select_paid_account_ids`, `_share_claude_config_all`, `_parse_wait_until`, `accounts.known_account_ids`
- Produces:
  - `scheduler.PROJECT_LOCK_ROOT: Path`, `scheduler.PROJECT_LOOP_DEFAULT_MODEL = "claude-opus-5-5"`, `scheduler.PROJECT_LOOP_DEFAULT_EFFORT = "high"`
  - `scheduler._project_loop_arg_error(args, management_flags: bool) -> str | None`
  - `scheduler._run_project_loop_mode(args, get_tool, management_flags: bool) -> int`
  - `parse_args` 결과에 `project_loop: bool`, `project_dir: str | None`, `prompt_file: str | None`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_scheduler_project_loop.py`:
```python
import time

import pytest

import accounts
import project_loop
import registry
import scheduler
import step_runner
from step_runner import StepOutcome

LOW_USAGE = {"five_hour_used_percent": 10, "five_hour_reset_at": None, "weekly_used_percent": 10, "weekly_reset_at": None}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    monkeypatch.setattr(accounts, "share_claude_config_all", lambda: {})
    monkeypatch.setattr(registry, "get_tool", lambda path, account_id: "claude")
    monkeypatch.setattr(scheduler, "fetch_usage", lambda account_id, tool: dict(LOW_USAGE))
    monkeypatch.setattr(scheduler, "PROJECT_LOCK_ROOT", tmp_path / "locks")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "prompt.md").write_text("first prompt")
    return root


@pytest.fixture
def captured_loop(monkeypatch):
    captured = {}

    def fake_run_project_loop(loop_accounts, **kwargs):
        captured["accounts"] = loop_accounts
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(project_loop, "run_project_loop", fake_run_project_loop)
    return captured


@pytest.fixture
def step_calls(monkeypatch):
    calls = []

    def fake_run_step(account_id, tool, project_dir, prompt_text, model, effort):
        calls.append((account_id, project_dir, prompt_text, model, effort))
        return StepOutcome(step_status="COMPLETE")

    monkeypatch.setattr(step_runner, "run_step", fake_run_step)
    return calls


def test_parse_args_defaults_leave_project_loop_off():
    args = scheduler.parse_args(["1"])
    assert args.project_loop is False
    assert args.project_dir is None
    assert args.prompt_file is None


def test_project_dir_without_project_loop_is_rejected(project, capsys):
    assert scheduler.main(["--project-dir", str(project)]) == 1
    assert "--project-loop와 함께만" in capsys.readouterr().err


def test_project_loop_rejects_management_flags(project, capsys):
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "-l"]) == 1
    assert "함께 쓸 수 없습니다" in capsys.readouterr().err


def test_project_loop_requires_project_dir(capsys):
    assert scheduler.main(["--project-loop"]) == 1
    assert "--project-dir이 필요합니다" in capsys.readouterr().err


def test_project_loop_rejects_missing_dir_non_git_and_missing_prompt(tmp_path, capsys):
    assert scheduler.main(["--project-loop", "--project-dir", str(tmp_path / "nope")]) == 1
    plain = tmp_path / "plain"
    plain.mkdir()
    assert scheduler.main(["--project-loop", "--project-dir", str(plain)]) == 1
    (plain / ".git").mkdir()
    assert scheduler.main(["--project-loop", "--project-dir", str(plain)]) == 1
    err = capsys.readouterr().err
    assert "프로젝트 디렉터리가 없습니다" in err
    assert "Git 저장소가 아닙니다" in err
    assert "프롬프트 파일을 찾을 수 없습니다" in err


def test_project_loop_builds_accounts_and_injects_existing_functions(project, captured_loop):
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "1", "2"]) == 0
    assert [account.account_id for account in captured_loop["accounts"]] == [1, 2]
    assert all(account.status == "ready" for account in captured_loop["accounts"])
    assert captured_loop["interval_min"] == 5
    assert captured_loop["check_plan_fn"] is scheduler._check_plan


def test_project_loop_count_selects_paid_accounts(project, captured_loop, monkeypatch):
    monkeypatch.setattr(accounts, "known_account_ids", lambda: {1, 2, 3})
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "-n", "2"]) == 0
    assert [account.account_id for account in captured_loop["accounts"]] == [1, 2]


def test_run_step_reads_prompt_fresh_and_uses_claude_defaults(project, captured_loop, step_calls):
    scheduler.main(["--project-loop", "--project-dir", str(project)])
    run_step_fn = captured_loop["run_step_fn"]
    account = captured_loop["accounts"][0]

    run_step_fn(account)
    (project / "prompt.md").write_text("edited prompt")
    run_step_fn(account)

    assert step_calls[0] == (1, project.resolve(), "first prompt", "claude-opus-5-5", "high")
    assert step_calls[1][2] == "edited prompt"


def test_model_effort_override_and_custom_prompt_file(project, captured_loop, step_calls):
    (project / "other.md").write_text("other prompt")
    scheduler.main([
        "--project-loop", "--project-dir", str(project), "--prompt-file", "other.md",
        "--model", "claude-haiku-4-5", "--effort", "low",
    ])
    captured_loop["run_step_fn"](captured_loop["accounts"][0])
    assert step_calls[0] == (1, project.resolve(), "other prompt", "claude-haiku-4-5", "low")


def test_delay_pushes_first_run(project, captured_loop):
    before = int(time.time())
    scheduler.main(["--project-loop", "--project-dir", str(project), "-d", "5"])
    assert captured_loop["accounts"][0].available_at >= before + 300


def test_refuses_when_project_lock_is_held(project, captured_loop, tmp_path, capsys):
    fd = project_loop.acquire_project_lock(project_loop.lock_path_for(project, tmp_path / "locks"))
    project_loop.write_lock_info(fd, "PID 999, 시작 test")
    try:
        assert scheduler.main(["--project-loop", "--project-dir", str(project)]) == 1
    finally:
        project_loop.release_project_lock(fd)
    err = capsys.readouterr().err
    assert "이미 이 프로젝트에서 작업 루프가 실행 중입니다" in err
    assert "PID 999" in err
    assert "accounts" not in captured_loop


def test_ctrl_c_returns_130_and_releases_lock(project, monkeypatch, tmp_path):
    def interrupted(loop_accounts, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(project_loop, "run_project_loop", interrupted)
    assert scheduler.main(["--project-loop", "--project-dir", str(project)]) == 130
    fd = project_loop.acquire_project_lock(project_loop.lock_path_for(project, tmp_path / "locks"))
    project_loop.release_project_lock(fd)


def test_exits_when_no_account_is_paid(project, captured_loop, monkeypatch, capsys):
    def not_paid(account_id, tool="claude"):
        raise accounts.AccountStatusError("유료 구독이 아님")

    monkeypatch.setattr(accounts, "check_paid_subscription", not_paid)
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "1", "2"]) == 1
    assert "유료 구독인 계정이 없습니다" in capsys.readouterr().err
    assert "accounts" not in captured_loop


def test_existing_reset_mode_never_enters_project_loop(monkeypatch):
    entered = []
    monkeypatch.setattr(scheduler, "_run_project_loop_mode", lambda *args: entered.append(args) or 0)
    monkeypatch.setattr(scheduler, "initialize_states", lambda *args, **kwargs: {})
    monkeypatch.setattr(scheduler, "run_scheduler_loop", lambda *args, **kwargs: None)
    assert scheduler.main(["1"]) == 0
    assert entered == []
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m pytest tests/test_scheduler_project_loop.py -q`
Expected: FAIL — `SystemExit: 2`(`unrecognized arguments: --project-dir`)와 `AttributeError: ... 'PROJECT_LOCK_ROOT'`

- [ ] **Step 3: `parse_args`에 옵션 추가** (`scheduler.py`의 `-n/--count` `add_argument(...)` 호출 바로 뒤, `args = parser.parse_args(argv)` 앞)

```python
    parser.add_argument("--project-loop", action="store_true", default=False,
                        help="여러 계정을 번갈아 쓰며 --project-dir의 프롬프트를 반복 실행합니다.")
    parser.add_argument("--project-dir", default=None, help="--project-loop 대상 프로젝트 (git 저장소)")
    parser.add_argument("--prompt-file", default=None, help="--project-dir 기준 프롬프트 파일. 기본값: prompt.md")
```

- [ ] **Step 4: 상수와 헬퍼 추가** (`scheduler.py`의 `def _parse_wait_until(` 바로 앞)

```python
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
    if args.delay < 0 or args.interval < 0:
        return f"--delay와 --interval은 음수일 수 없습니다 (delay={args.delay}, interval={args.interval})"
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
```

- [ ] **Step 5: `main()`에 분기 추가** (`management_flags = (...)` 할당 바로 뒤, `if args.count is not None and management_flags:` 바로 앞)

```python
    if args.project_loop or args.project_dir is not None or args.prompt_file is not None:
        return _run_project_loop_mode(args, get_tool, management_flags)
```

- [ ] **Step 6: 새 테스트 통과 확인**

Run: `python3 -m pytest tests/test_scheduler_project_loop.py -q`
Expected: PASS (14 passed)

- [ ] **Step 7: 전체 테스트 확인 (기존 테스트는 수정 없이 통과해야 함)**

Run: `python3 -m pytest -q && git diff --stat f99e1f3 -- tests/test_scheduler.py tests/test_accounts.py tests/test_usage.py tests/test_registry.py`
Expected: `241 passed`(기존 183 + step_runner 22 + project_loop 22 + scheduler 연결 14), 기존 테스트 파일 diff 없음(두 번째 명령의 출력이 비어 있어야 함). `f99e1f3`은 이 작업 시작 전(설계서 커밋) 시점이다.

- [ ] **Step 8: 커밋**

```bash
git add scheduler.py tests/test_scheduler_project_loop.py
git commit -m "$(cat <<'EOF'
feat: add --project-loop mode wiring (options, validation, lock, account selection)

The existing reset-scheduler path is untouched; --project-loop branches off
in main() before any of it runs.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01NMqboR51vArC6Bu4k4JUSs
EOF
)"
```

---

### Task 7: README 문서화와 실제 CLI로 수동 확인

**Files:**
- Modify: `README.md` — 빠른 시작 예시, 새 섹션, 옵션 표 행, 로그 섹션

**Interfaces:**
- Consumes: Task 6까지의 CLI
- Produces: 문서, 수동 확인 결과

- [ ] **Step 1: 빠른 시작에 예시 추가** (`README.md`의 `python3 scheduler.py 1 -w "2026-10-01 09:00"` 줄 바로 뒤, 같은 코드 블록 안)

```bash

# 작업 루프: 유료 계정 4개를 번갈아 쓰며 ~/shortform-ai의 prompt.md를 계속 실행
# (한도가 찬 계정은 리셋 시각까지 쉬고, 쓸 수 있는 다음 계정이 같은 step을 이어받음)
python3 scheduler.py --project-loop --project-dir ~/shortform-ai -n 4
```

- [ ] **Step 2: 새 섹션 추가** (`## 전체 옵션` 바로 앞. 아래 `~~~` 사이의 내용을 그대로 넣는다)

~~~markdown
## 작업 루프 (`--project-loop`)

리셋용 "OK" 호출 대신, 대상 프로젝트의 프롬프트(`prompt.md`)를 실제로 실행하는 모드입니다. `~/shortform-ai`의 `run-step-loop-claude.sh`/`run-step-loop.sh`를 여러 계정으로 이어서 돌리는 것과 같습니다.

```bash
python3 scheduler.py --project-loop --project-dir ~/shortform-ai -n 4
python3 scheduler.py --project-loop --project-dir ~/shortform-ai 2 3 4 --prompt-file prompt.md
```

동작:
- 한 계정으로 계속 실행합니다. 출력의 `STEP_STATUS: COMPLETE` / `FAILURE_ANALYSIS` / `FAILURE_IMPROVEMENT`를 보면 같은 계정으로 다음 실행을 이어갑니다.
- 한도에 걸리면(claude: `rate_limit_event` rejected / 429, codex: `turn.failed`의 "usage limit") 그 계정을 리셋 시각 + 1분까지 쉬게 하고, 쓸 수 있는 다음 계정이 **같은 step을 처음부터** 다시 실행합니다. 진행 상태는 `PHASE.md`, worklog, 작업 트리에 남아 있습니다.
- 계정을 쓰기 직전마다 사용량을 다시 조회해, 그 사이 한도가 찬 계정은 건너뜁니다.
- 쓸 수 있는 계정이 없으면 가장 빠른 리셋 시각까지 기다렸다가 재개합니다.
- 로그인 만료 같은 계정 오류는 그 계정만 빼 두고 60분마다 다시 확인합니다.
- 종료: `STEP_STATUS: INCOMPLETE`(권한/승인 문제라 계정을 바꿔도 해결되지 않음), 완료 상태도 한도 신호도 없는 실패가 3회 연속, 또는 `Ctrl+C`(종료 코드 130).

모델: `--model`/`--effort`는 **claude 계정에만** 적용되며, 이 모드의 기본값은 `claude-opus-5-5` / `high`입니다. codex 계정은 codex CLI 기본 설정으로 실행합니다.

로그: `<프로젝트>/.claude-runs/` 또는 `.codex-runs/`에 `step-<타임스탬프>-user<N>.log`(텍스트, 기존 스크립트 로그와 같은 형식)와 `step-<타임스탬프>-user<N>.raw.jsonl`(CLI 원본 출력)이 남습니다.

동시 실행:
- 같은 프로젝트에 작업 루프를 두 번 띄우면 두 번째는 시작하지 않습니다(프로젝트별 잠금, `.schedule/locks/`). 다른 프로젝트끼리는 동시에 돌릴 수 있습니다.
- 리셋 스케줄러(`python3 scheduler.py -n 4`)와 작업 루프는 동시에 돌려도 됩니다. claude는 토큰 갱신을 CLI가 잠금으로 순서를 맞추고, codex는 인증 실패 시 `auth.json`을 다시 읽습니다. codex에서 드물게 "refresh token was already used"가 나면 `-a N`으로 다시 로그인하세요.
- 작업 루프가 도는 동안 **같은 프로젝트에서 기존 `run-step-loop*.sh`를 돌리거나 직접 편집하지 마세요.** 이 잠금으로는 막지 못합니다.
- 작업 루프는 `.schedule/queue.json`을 읽거나 쓰지 않으므로, 리셋 스케줄러의 "최근 실행"에는 작업 루프의 실행이 표시되지 않습니다.
~~~

- [ ] **Step 3: 옵션 표에 행 추가** (`| \`--count N\` | \`-n\` | ...` 행 바로 뒤)

```markdown
| `--project-loop` | — | — | 작업 루프 모드. 여러 계정을 번갈아 쓰며 `--project-dir`의 프롬프트를 반복 실행 (위 "작업 루프" 참고). `-a`/`-l`/`-r`/`-c`와 함께 쓸 수 없음 |
| `--project-dir DIR` | — | — | `--project-loop` 대상 프로젝트. git 저장소여야 함 |
| `--prompt-file FILE` | — | `prompt.md` | `--project-dir` 기준 프롬프트 파일. 매 실행마다 새로 읽음 |
```

- [ ] **Step 4: 로그 섹션에 줄 추가** (`## 로그 및 상태 파일` 목록 끝)

```markdown
- 작업 루프 로그: `<대상 프로젝트>/.claude-runs/` 또는 `.codex-runs/`의 `step-<타임스탬프>-user<N>.log` / `.raw.jsonl`
- 작업 루프 잠금: `<프로젝트 루트>/.schedule/locks/project-<해시>.lock` (git 추적 대상 아님)
```

- [ ] **Step 5: 실제 CLI로 한도 감지 확인** (user1의 주간 한도가 찬 상태여야 함. 2026-10-01 02:00 리셋 전까지. 먼저 `python3 scheduler.py 1 -c`로 "주간 한도: 100%"인지 확인하고, 아니면 이 단계는 건너뛰고 그 사실을 기록한다)

```bash
TMP=$(mktemp -d) && git -C "$TMP" init -q && echo "Reply with OK." > "$TMP/prompt.md"
python3 -c "
import step_runner, pathlib
o = step_runner.run_step(1, 'claude', pathlib.Path('$TMP'), 'Reply with OK.', 'claude-haiku-4-5', 'low')
print(o)
"
```
Expected: `StepOutcome(step_status=None, limit_hit=True, limit_reset_at=1790787600, account_error=None, exit_code=1, log_path=...)`

- [ ] **Step 6: 실제 CLI로 계정 건너뛰기와 종료 확인** (user1은 소진, user3은 사용 가능한 상태. 비용을 줄이려고 haiku/low 사용)

```bash
TMP=$(mktemp -d) && git -C "$TMP" init -q
printf 'Reply with exactly this single line and nothing else:\nSTEP_STATUS: INCOMPLETE\n' > "$TMP/prompt.md"
python3 scheduler.py --project-loop --project-dir "$TMP" 1 3 --model claude-haiku-4-5 --effort low; echo "exit=$?"
ls "$TMP/.claude-runs"
```
Expected:
- 상태 출력에서 user1은 `소진(주간 한도) → 2026-10-01 02:00:59 재투입`, 실행은 `▶ 실행: user3`
- "STEP_STATUS: INCOMPLETE" 안내 후 `exit=1`
- `.claude-runs/`에 `step-…-user3.log`와 `step-…-user3.raw.jsonl`

- [ ] **Step 7: 실제 잠금 경로로 중복 실행 거부 확인** (다른 프로세스가 실제 `.schedule/locks/`의 잠금을 20초 동안 쥐고 있게 한 뒤 실행. 모델 호출은 없음)

```bash
TMP=$(mktemp -d) && git -C "$TMP" init -q && echo "Reply with OK." > "$TMP/prompt.md"
python3 -c "
import pathlib, time, project_loop, scheduler
fd = project_loop.acquire_project_lock(project_loop.lock_path_for(pathlib.Path('$TMP'), scheduler.PROJECT_LOCK_ROOT))
project_loop.write_lock_info(fd, 'PID manual-test')
time.sleep(20)
" &
sleep 2
python3 scheduler.py --project-loop --project-dir "$TMP" 3; echo "exit=$?"
wait
```
Expected: "이미 이 프로젝트에서 작업 루프가 실행 중입니다: … PID manual-test …"를 출력하고 `exit=1`

- [ ] **Step 8: 전체 테스트 재확인과 커밋**

Run: `python3 -m pytest -q`
Expected: `241 passed`

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs: document --project-loop mode, its options, logs and concurrency rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01NMqboR51vArC6Bu4k4JUSs
EOF
)"
```
