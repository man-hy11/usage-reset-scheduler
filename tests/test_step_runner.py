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
