import json as _json

import pytest

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
