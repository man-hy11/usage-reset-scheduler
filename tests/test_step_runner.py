import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_codex_auth_marker_ignores_unrelated_text():
    reader = step_runner.CodexEventReader()
    reader.feed(json.dumps({"type": "error", "message": "failed to write catalog output"}))
    assert reader.account_error is None


def test_codex_reader_ignores_non_limit_item_errors():
    reader = step_runner.CodexEventReader()
    reader.feed(json.dumps({"type": "item.completed", "item": {"type": "error", "message": "Model metadata not found"}}))
    assert reader.limit_hit is False
    assert reader.account_error is None


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


def _block_start():
    return json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}})


def test_claude_text_blocks_are_newline_separated():
    reader = step_runner.ClaudeEventReader()
    text = _feed_all(reader, [_block_start(), _delta("Working on it."), _block_start(), _delta("STEP_STATUS: COMPLETE")])
    assert text == "Working on it.\nSTEP_STATUS: COMPLETE"
    assert step_runner.find_step_status(text) == "COMPLETE"


def test_run_step_stops_child_when_interrupted(monkeypatch, tmp_path):
    script = (
        "import json, os, sys, time\n"
        "print(json.dumps({'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': str(os.getpid())}}}), flush=True)\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(step_runner, "build_command", lambda *args, **kwargs: [sys.executable, "-c", script])
    seen = {}

    class InterruptingOut:
        def write(self, text):
            seen["pid"] = int(text)
            raise KeyboardInterrupt

        def flush(self):
            pass

    with pytest.raises(KeyboardInterrupt):
        step_runner.run_step(1, "claude", tmp_path, "PROMPT", "m", "e", out=InterruptingOut())
    with pytest.raises(ProcessLookupError):
        os.kill(seen["pid"], 0)
