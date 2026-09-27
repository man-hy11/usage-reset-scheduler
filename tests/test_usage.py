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


def test_compute_next_run_uses_weekly_reset_even_when_five_hour_reset_is_none():
    # 실사례(user3): 주간 한도가 100% 소진돼 5시간 윈도우가 아직 한 번도 열리지
    # 않은 계정은 five_hour_reset_at을 None으로 반환한다. week_pct가 threshold
    # 이상이면 애초에 five_hour 값과 무관하게 주간 리셋만 보면 되므로, 이 경우
    # fallback으로 떨어지면 안 되고 정상적으로 주간 리셋 + 60초를 반환해야 한다.
    status = {
        "five_hour_used_percent": 0.0,
        "five_hour_reset_at": None,
        "weekly_used_percent": 100,
        "weekly_reset_at": 1_790_470_200,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=5, now=1_790_469_000)
    assert seconds == 1260  # weekly_reset_at - now + 60


def test_compute_next_run_uses_five_hour_reset_even_when_weekly_reset_is_none():
    # 대칭 사례: 주간이 threshold 미만이면 five_hour 값만 있으면 계산 가능해야
    # 하고, weekly_reset_at이 None이어도 fallback으로 떨어지면 안 된다.
    status = {
        "five_hour_used_percent": 100,
        "five_hour_reset_at": 1_790_469_600,
        "weekly_used_percent": 42,
        "weekly_reset_at": None,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=5, now=1_790_469_000)
    assert seconds == 660  # five_hour_reset_at - now + 60


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


def test_compute_next_run_falls_back_when_weekly_used_percent_missing():
    # week_pct 자체가 없으면 threshold와 비교할 수 없으니 fallback으로 떨어져야
    # 한다 (TypeError로 죽으면 안 됨).
    status = {
        "five_hour_used_percent": 10,
        "five_hour_reset_at": 1_790_469_600,
        "weekly_used_percent": None,
        "weekly_reset_at": 1_791_073_800,
    }
    seconds = usage.compute_next_run(status, threshold=100, fallback_min=5, now=1_790_469_000)
    assert seconds == 5 * 60


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


def test_fetch_codex_usage_normalizes_response(tmp_path, monkeypatch):
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "auth.json").write_text(
        _json.dumps({"tokens": {"access_token": "tok-456"}})
    )

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(
            {
                "rate_limit": {
                    "primary_window": {"used_percent": 12, "reset_at": "2026-09-27T12:00:00+09:00"},
                    "secondary_window": {"used_percent": 34, "reset_at": "2026-10-03T11:00:00+09:00"},
                }
            }
        )

    monkeypatch.setattr(usage.requests, "get", fake_get)

    result = usage.fetch_codex_usage(config_dir)

    assert result["tool"] == "codex"
    assert result["five_hour_used_percent"] == 12
    assert result["weekly_used_percent"] == 34
    assert captured["headers"]["Authorization"] == "Bearer tok-456"
    assert "chatgpt.com" in captured["url"]


def test_read_codex_token_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        usage.read_codex_token(tmp_path / "does-not-exist")
