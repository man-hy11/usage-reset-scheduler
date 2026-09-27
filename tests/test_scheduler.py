import heapq
import json as _json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import accounts
import registry
import scheduler
import usage


@pytest.fixture(autouse=True)
def _default_paid_plan(monkeypatch):
    # 구독 게이트는 실제 `claude auth status`와 네트워크를 호출하므로,
    # 테스트가 따로 지정하지 않으면 항상 유료("pro")로 고정한다.
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    # main()이 실제 홈 디렉터리의 계정 폴더를 건드리지 않도록 막는다.
    monkeypatch.setattr(accounts, "share_claude_config_all", lambda: {})


def test_state_round_trip_through_dict():
    states = {
        1: scheduler.AccountState(next_run_at=1000, status="scheduled", fail_count=0),
        2: scheduler.AccountState(next_run_at=None, status="free_skip", fail_count=0),
    }
    raw = scheduler.state_to_dict(states)
    restored = scheduler.state_from_dict(raw)

    assert restored[1] == states[1]
    assert restored[2] == states[2]


def test_state_round_trip_preserves_usage_limit_fields():
    states = {
        1: scheduler.AccountState(
            next_run_at=1000,
            status="scheduled",
            five_hour_used_percent=42,
            five_hour_reset_at=1790496059,
            weekly_used_percent=17,
            weekly_reset_at=1791073800,
        ),
    }
    raw = scheduler.state_to_dict(states)
    restored = scheduler.state_from_dict(raw)

    assert restored[1] == states[1]


def test_state_from_dict_defaults_usage_limit_fields_to_none_when_absent():
    # 이 필드들이 추가되기 전에 저장된 기존 queue.json과의 하위 호환성.
    raw = {"1": {"next_run_at": 1000, "status": "scheduled", "fail_count": 0}}
    restored = scheduler.state_from_dict(raw)

    assert restored[1].five_hour_used_percent is None
    assert restored[1].weekly_reset_at is None


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


def _stub_usage_fetch(monkeypatch, email=None, plan="pro", **overrides):
    result = {
        "five_hour_used_percent": None,
        "five_hour_reset_at": None,
        "weekly_used_percent": None,
        "weekly_reset_at": None,
    }
    result.update(overrides)
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda config_dir: result)
    monkeypatch.setattr(accounts, "account_email", lambda account_id, tool="claude": email)
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": plan)


def test_initialize_states_marks_free_account_as_free_skip(monkeypatch):
    def fake_check(account_id, tool="claude"):
        if account_id == 2:
            raise accounts.AccountStatusError("유료 Claude 구독이 아님")
        return "pro"

    _stub_usage_fetch(monkeypatch)
    monkeypatch.setattr(accounts, "check_paid_subscription", fake_check)

    states = scheduler.initialize_states([1, 2], wait_until=None, delay_seconds=0, now=1000)

    assert states[2].status == "free_skip"
    assert states[2].next_run_at == 1000 + scheduler.PLAN_RECHECK_MIN * 60
    assert states[1].status == "scheduled"


def test_initialize_states_uses_wait_until_when_given(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    _stub_usage_fetch(monkeypatch)

    states = scheduler.initialize_states([1], wait_until=5000, delay_seconds=0, now=1000)

    assert states[1].next_run_at == 5000


def test_initialize_states_uses_now_plus_delay_when_no_wait_until(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    _stub_usage_fetch(monkeypatch)

    states = scheduler.initialize_states([1], wait_until=None, delay_seconds=120, now=1000)

    assert states[1].next_run_at == 1120


def test_initialize_states_fetches_usage_for_newly_scheduled_account(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    _stub_usage_fetch(
        monkeypatch,
        five_hour_used_percent=42,
        five_hour_reset_at=1790496059,
        weekly_used_percent=17,
        weekly_reset_at=1791073800,
    )

    states = scheduler.initialize_states([1], wait_until=None, delay_seconds=0, now=1000)

    assert states[1].five_hour_used_percent == 42
    assert states[1].five_hour_reset_at == 1790496059
    assert states[1].weekly_used_percent == 17
    assert states[1].weekly_reset_at == 1791073800


def test_initialize_states_does_not_fetch_usage_for_free_skip_account(monkeypatch):
    def fake_check(account_id, tool="claude"):
        raise accounts.AccountStatusError("유료 Claude 구독이 아님")

    monkeypatch.setattr(accounts, "check_paid_subscription", fake_check)

    def fail_if_called(config_dir):
        raise AssertionError("fetch_claude_usage should not be called for a free_skip account")

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", fail_if_called)

    states = scheduler.initialize_states([2], wait_until=None, delay_seconds=0, now=1000)

    assert states[2].status == "free_skip"


def test_initialize_states_leaves_usage_fields_none_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(accounts, "account_email", lambda account_id, tool="claude": None)

    def fake_fetch(config_dir):
        raise RuntimeError("network down")

    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)

    states = scheduler.initialize_states([1], wait_until=None, delay_seconds=0, now=1000)

    assert states[1].status == "scheduled"
    assert states[1].five_hour_used_percent is None
    assert states[1].weekly_used_percent is None


def test_retry_pending_accounts_promotes_on_success(monkeypatch):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="retry_pending", fail_count=2),
    }

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda config_dir: {"weekly_used_percent": 10, "weekly_reset_at": 9999, "five_hour_used_percent": 5, "five_hour_reset_at": 2000})
    monkeypatch.setattr(usage, "compute_next_run", lambda status, threshold, fallback_min, now: 500)

    result = scheduler.retry_pending_accounts(states, interval_min=5, threshold=100, fallback_min=305, now=1000)

    assert result[1].status == "scheduled"
    assert result[1].fail_count == 0
    assert result[1].next_run_at == 1500  # now + compute_next_run
    assert result[1].five_hour_used_percent == 5
    assert result[1].five_hour_reset_at == 2000
    assert result[1].weekly_used_percent == 10
    assert result[1].weekly_reset_at == 9999


def test_retry_pending_accounts_keeps_retrying_on_failure(monkeypatch):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="retry_pending", fail_count=2),
    }

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))

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

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))

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
        if len(call_order) == 2:
            raise _StopLoop()
        return True

    def fake_fetch(config_dir):
        return {"five_hour_used_percent": 1, "five_hour_reset_at": 99999, "weekly_used_percent": 1, "weekly_reset_at": 99999}

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
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


def test_run_scheduler_loop_stores_usage_limits_after_successful_run(monkeypatch, tmp_path):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="scheduled"),
    }

    class _StopLoop(Exception):
        pass

    def fake_run_claude(account_id):
        return True

    def fake_fetch(config_dir):
        return {
            "five_hour_used_percent": 42,
            "five_hour_reset_at": 1790496059,
            "weekly_used_percent": 17,
            "weekly_reset_at": 1791073800,
        }

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)
    monkeypatch.setattr(usage, "compute_next_run", lambda status, threshold, fallback_min, now: 999999)

    now_box = {"t": 100}

    def fake_now():
        return now_box["t"]

    call_count = {"n": 0}

    def fake_sleep(seconds):
        call_count["n"] += 1
        now_box["t"] += seconds
        if call_count["n"] >= 2:
            # 1번째 sleep은 claude 실행 전 대기, 2번째는 claude 실행+사용량
            # 갱신이 끝난 뒤 다음 루프 반복에서 발생 — 그 시점에 멈춘다.
            raise _StopLoop()

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

    assert states[1].five_hour_used_percent == 42
    assert states[1].five_hour_reset_at == 1790496059
    assert states[1].weekly_used_percent == 17
    assert states[1].weekly_reset_at == 1791073800


def test_parse_args_defaults_to_account_1_when_none_given():
    args = scheduler.parse_args([])
    assert args.account_ids == [1]
    # --model/--effort는 지정하지 않으면 None으로 남고, 계정별 도구(claude/codex)의
    # 기본값이 main()에서 실행 시점에 적용된다 (DEFAULT_MODEL/DEFAULT_EFFORT 참고).
    assert args.model is None
    assert args.effort is None
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


def test_parse_args_account_management_short_flags():
    args = scheduler.parse_args(["-a", "5"])
    assert args.add_account == 5

    args = scheduler.parse_args(["-l"])
    assert args.list_accounts is True

    args = scheduler.parse_args(["-r", "3"])
    assert args.remove_account == 3

    args = scheduler.parse_args(["1", "-c"])
    assert args.check_subscription is True


def test_build_claude_command_shape():
    cmd = scheduler.build_claude_command("claude-haiku-4-5", "low", "Reply with OK.")
    assert cmd == [
        "claude",
        "--dangerously-skip-permissions",
        "--strict-mcp-config",
        "--model", "claude-haiku-4-5",
        "--effort", "low",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "Reply with OK.",
    ]


@pytest.mark.parametrize("account_id", [1, 2])
def test_run_claude_always_sets_config_dir_including_account_1(monkeypatch, tmp_path, account_id):
    # user1도 전용 폴더를 쓴다 — 사용자의 ~/.claude를 물려받으면 인증 정보와
    # 세션 기록이 섞인다.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/should/be/overridden")
    captured = {}

    class _FakeCompletedProcess:
        stdout = ""
        returncode = 0

    def fake_run(cmd, env, capture_output, text):
        captured["env"] = env
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(scheduler, "subprocess", subprocess)
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id, tool="claude": tmp_path)

    scheduler.run_claude(account_id, "claude-haiku-4-5", "low")

    expected = tmp_path / ".usage-reset-scheduler" / "accounts" / f"claude-{account_id}"
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(expected)


@pytest.mark.parametrize("account_id", [1, 4])
def test_run_codex_always_sets_codex_home_including_account_1(monkeypatch, tmp_path, account_id):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CODEX_HOME", "/should/be/overridden")
    captured = {}

    class _FakeCompletedProcess:
        stdout = ""
        returncode = 0

    def fake_run(cmd, env, capture_output, text):
        captured["env"] = env
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(scheduler, "subprocess", subprocess)
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id, tool="codex": tmp_path)

    scheduler.run_codex(account_id, "gpt-6-luna", "low")

    expected = tmp_path / ".usage-reset-scheduler" / "accounts" / f"codex-{account_id}"
    assert captured["env"]["CODEX_HOME"] == str(expected)


def test_run_claude_extracts_text_deltas_and_returns_true_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": tmp_path / f"acct{account_id}")

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
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id, tool="claude": log_dir)

    result = scheduler.run_claude(1, "claude-haiku-4-5", "low")

    assert result is True
    logged_files = list(log_dir.glob("loop-*.log"))
    assert len(logged_files) == 1
    assert logged_files[0].read_text() == "OK"


def test_run_claude_returns_false_on_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": tmp_path / f"acct{account_id}")

    class _FakeCompletedProcess:
        def __init__(self):
            self.stdout = ""
            self.returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda cmd, env, capture_output, text: _FakeCompletedProcess())
    monkeypatch.setattr(scheduler, "subprocess", subprocess)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id, tool="claude": log_dir)

    result = scheduler.run_claude(1, "claude-haiku-4-5", "low")

    assert result is False


def test_run_claude_skips_non_dict_json_line_and_still_extracts_valid_text_deltas(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": tmp_path / f"acct{account_id}")

    stream_lines = [
        _json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "OK"}}}),
        _json.dumps([1, 2, 3]),
    ]

    class _FakeCompletedProcess:
        def __init__(self):
            self.stdout = "\n".join(stream_lines) + "\n"
            self.returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda cmd, env, capture_output, text: _FakeCompletedProcess())
    monkeypatch.setattr(scheduler, "subprocess", subprocess)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id, tool="claude": log_dir)

    result = scheduler.run_claude(1, "claude-haiku-4-5", "low")

    assert result is True
    logged_files = list(log_dir.glob("loop-*.log"))
    assert len(logged_files) == 1
    assert logged_files[0].read_text() == "OK"


def test_run_claude_skips_non_dict_event_field_and_still_extracts_valid_text_deltas(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": tmp_path / f"acct{account_id}")

    stream_lines = [
        _json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "OK"}}}),
        _json.dumps({"type": "stream_event", "event": "oops"}),
    ]

    class _FakeCompletedProcess:
        def __init__(self):
            self.stdout = "\n".join(stream_lines) + "\n"
            self.returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda cmd, env, capture_output, text: _FakeCompletedProcess())
    monkeypatch.setattr(scheduler, "subprocess", subprocess)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id, tool="claude": log_dir)

    result = scheduler.run_claude(1, "claude-haiku-4-5", "low")

    assert result is True
    logged_files = list(log_dir.glob("loop-*.log"))
    assert len(logged_files) == 1
    assert logged_files[0].read_text() == "OK"


def test_main_check_subscription_prints_plan_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(accounts, "account_email", lambda account_id, tool="claude": None)
    monkeypatch.setattr(
        usage,
        "fetch_claude_usage",
        lambda config_dir: {
            "five_hour_used_percent": None,
            "five_hour_reset_at": None,
            "weekly_used_percent": None,
            "weekly_reset_at": None,
        },
    )

    rc = scheduler.main(["1", "--check-subscription"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "pro" in captured.out


def test_main_check_subscription_includes_email_when_known(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(accounts, "account_email", lambda account_id, tool="claude": "a@example.com")
    monkeypatch.setattr(
        usage,
        "fetch_claude_usage",
        lambda config_dir: {
            "five_hour_used_percent": None,
            "five_hour_reset_at": None,
            "weekly_used_percent": None,
            "weekly_reset_at": None,
        },
    )

    rc = scheduler.main(["1", "--check-subscription"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "a@example.com" in captured.out


def test_main_check_subscription_reports_skip_for_free_plan(monkeypatch, capsys):
    def fake_check(account_id, tool="claude"):
        raise accounts.AccountStatusError("유료 Claude 구독이 아님 (subscriptionType=free)")

    monkeypatch.setattr(accounts, "check_paid_subscription", fake_check)

    rc = scheduler.main(["2", "--check-subscription"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "SKIP" in captured.out


def test_main_check_subscription_includes_usage_limits(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(accounts, "account_email", lambda account_id, tool="claude": None)
    monkeypatch.setattr(
        usage,
        "fetch_claude_usage",
        lambda config_dir: {
            "five_hour_used_percent": 42,
            "five_hour_reset_at": 1790496059,
            "weekly_used_percent": 17,
            "weekly_reset_at": 1791073800,
        },
    )

    rc = scheduler.main(["1", "--check-subscription"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "42%" in captured.out
    assert "17%" in captured.out
    expected_five_date = datetime.fromtimestamp(1790496059).strftime("%Y-%m-%d")
    expected_week_date = datetime.fromtimestamp(1791073800).strftime("%Y-%m-%d")
    assert expected_five_date in captured.out
    assert expected_week_date in captured.out


def test_main_check_subscription_reports_usage_fetch_failure_without_crashing(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(accounts, "account_email", lambda account_id, tool="claude": None)

    def fake_fetch(config_dir):
        raise RuntimeError("network down")

    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)

    rc = scheduler.main(["1", "--check-subscription"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "사용량 조회 실패" in captured.out


def test_main_list_accounts_routes_to_accounts_module(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        accounts,
        "list_accounts",
        lambda get_tool=None: [(1, "pro", tmp_path / ".claude", "a@example.com", "claude")],
    )

    rc = scheduler.main(["--list-accounts"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "user1" in captured.out
    assert "pro" in captured.out
    assert "a@example.com" in captured.out
    assert "claude" in captured.out


def test_main_list_accounts_shows_dash_when_email_unknown(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        accounts,
        "list_accounts",
        lambda get_tool=None: [
            (2, "SKIP: free", tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-2", None, "claude")
        ],
    )

    rc = scheduler.main(["--list-accounts"])

    captured = capsys.readouterr()
    assert rc == 0
    lines = [l for l in captured.out.splitlines() if l.startswith("user2")]
    assert len(lines) == 1
    assert "-" in lines[0].split()


def test_main_add_account_routes_to_accounts_module(monkeypatch):
    called = {}

    def fake_add(account_id, tool="claude"):
        called["account_id"] = account_id
        called["tool"] = tool

    monkeypatch.setattr(accounts, "add_account", fake_add)
    monkeypatch.setattr(scheduler, "prompt_for_tool", lambda: "claude")
    monkeypatch.setattr(registry, "set_tool", lambda path, account_id, tool: None)

    rc = scheduler.main(["--add-account", "5"])

    assert rc == 0
    assert called["account_id"] == 5
    assert called["tool"] == "claude"


def test_main_add_account_prompts_for_tool_and_registers_codex(monkeypatch):
    called = {}

    monkeypatch.setattr(accounts, "add_account", lambda account_id, tool="claude": called.update(account_id=account_id, tool=tool))
    monkeypatch.setattr(scheduler, "prompt_for_tool", lambda: "codex")

    registered = {}
    monkeypatch.setattr(registry, "set_tool", lambda path, account_id, tool: registered.update(account_id=account_id, tool=tool))

    rc = scheduler.main(["--add-account", "4"])

    assert rc == 0
    assert called["tool"] == "codex"
    assert registered == {"account_id": 4, "tool": "codex"}


def test_main_remove_account_routes_to_accounts_module(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "remove_account", lambda account_id, tool="claude": Path("/fake/backup"))

    rc = scheduler.main(["--remove-account", "2"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "backup" in captured.out


def test_main_remove_account_1_returns_nonzero(monkeypatch, capsys):
    def fake_remove(account_id, tool="claude"):
        raise ValueError("user1은 제거할 수 없습니다")

    monkeypatch.setattr(accounts, "remove_account", fake_remove)

    rc = scheduler.main(["--remove-account", "1"])

    captured = capsys.readouterr()
    assert rc != 0
    assert "user1은 제거할 수 없습니다" in captured.err


def test_main_rejects_past_wait_until_without_entering_loop(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("과거 --wait-until인데 스케줄러 루프가 시작됨")

    monkeypatch.setattr(scheduler, "run_scheduler_loop", fail_if_called)

    rc = scheduler.main(["1", "--wait-until", "2000-01-01 00:00"])

    captured = capsys.readouterr()
    assert rc != 0
    assert "이미 과거" in captured.err


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


# --- Fix 1: busy-spin regression ---


def test_run_scheduler_loop_sleeps_instead_of_busy_spinning_on_retry_pending(monkeypatch, tmp_path):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="retry_pending", fail_count=1),
    }

    sleep_calls = []

    class _StopLoop(Exception):
        pass

    def fake_fetch(config_dir):
        raise RuntimeError("network error")

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)

    now_box = {"t": 100}

    def fake_now():
        return now_box["t"]

    iterations = {"n": 0}

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        now_box["t"] += max(seconds, 1)
        iterations["n"] += 1
        if iterations["n"] >= 5:
            raise _StopLoop()

    def fake_run_claude(account_id):
        raise AssertionError("account stuck in retry_pending should never reach run_claude_fn")

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

    # Loop made bounded progress (stopped after N iterations rather than running forever).
    assert iterations["n"] == 5
    # Critical: at least one sleep call must be a real, non-zero duration -
    # a sleep_fn(0) pattern would still "terminate" this test but leaves the
    # busy-spin/API-hammering bug unfixed.
    assert any(s > 0 for s in sleep_calls)


# --- Fix 2: silent exception swallowing ---


def test_retry_pending_accounts_logs_exception_message(monkeypatch, capsys):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="retry_pending", fail_count=0),
    }

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))

    def fake_fetch(config_dir):
        raise RuntimeError("some distinctive network failure")

    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)

    scheduler.retry_pending_accounts(states, interval_min=5, threshold=100, fallback_min=305, now=1000)

    captured = capsys.readouterr()
    assert "some distinctive network failure" in captured.out


def test_run_scheduler_loop_logs_exception_after_claude_run(monkeypatch, tmp_path, capsys):
    states = {
        1: scheduler.AccountState(next_run_at=100, status="scheduled"),
    }

    class _StopLoop(Exception):
        pass

    def fake_run_claude(account_id):
        return True

    def fake_fetch(config_dir):
        raise RuntimeError("post-run usage fetch boom")

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)

    call_count = {"n": 0}

    def fake_now():
        return 100

    def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise _StopLoop()

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

    captured = capsys.readouterr()
    assert "post-run usage fetch boom" in captured.out


# --- Fix 3: queue persistence across restarts ---


def test_initialize_states_preserves_existing_retry_pending_when_still_paid(monkeypatch):
    _stub_usage_fetch(monkeypatch)

    existing_states = {
        1: scheduler.AccountState(next_run_at=12345, status="retry_pending", fail_count=3),
    }

    states = scheduler.initialize_states(
        [1], wait_until=None, delay_seconds=0, now=1000, existing_states=existing_states
    )

    assert states[1].status == "retry_pending"
    assert states[1].next_run_at == 12345
    assert states[1].fail_count == 3


def test_initialize_states_preserves_existing_scheduled_when_still_paid(monkeypatch):
    _stub_usage_fetch(monkeypatch)

    existing_states = {
        1: scheduler.AccountState(next_run_at=5000, status="scheduled", fail_count=0),
    }

    states = scheduler.initialize_states(
        [1], wait_until=None, delay_seconds=0, now=1000, existing_states=existing_states
    )

    assert states[1].status == "scheduled"
    assert states[1].next_run_at == 5000


def test_initialize_states_demotes_saved_scheduled_account_that_turned_free(monkeypatch):
    _stub_usage_fetch(monkeypatch)

    def fake_check(account_id, tool="claude"):
        raise accounts.AccountStatusError("유료 구독이 아님 (subscriptionType=free)")

    monkeypatch.setattr(accounts, "check_paid_subscription", fake_check)

    existing_states = {
        1: scheduler.AccountState(next_run_at=5000, status="scheduled", fail_count=0, plan="pro"),
    }

    states = scheduler.initialize_states(
        [1], wait_until=None, delay_seconds=0, now=1000, existing_states=existing_states,
        plan_recheck_seconds=600,
    )

    assert states[1].status == "free_skip"
    assert states[1].next_run_at == 1600
    assert states[1].plan is None


def test_initialize_states_promotes_saved_free_skip_account_that_turned_paid(monkeypatch):
    _stub_usage_fetch(monkeypatch, plan="max")
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "max")

    existing_states = {
        1: scheduler.AccountState(next_run_at=None, status="free_skip"),
    }

    states = scheduler.initialize_states(
        [1], wait_until=None, delay_seconds=30, now=1000, existing_states=existing_states
    )

    assert states[1].status == "scheduled"
    assert states[1].next_run_at == 1030
    assert states[1].plan == "max"


class _StopLoop(Exception):
    pass


def _run_loop_with_fake_clock(states, tmp_path, run_claude_fn, start=100, plan_recheck_min=10):
    now_box = {"t": start}

    def fake_sleep(seconds):
        now_box["t"] += seconds

    with pytest.raises(_StopLoop):
        scheduler.run_scheduler_loop(
            states,
            interval_min=5,
            threshold=100,
            fallback_min=305,
            run_claude_fn=run_claude_fn,
            sleep_fn=fake_sleep,
            now_fn=lambda: now_box["t"],
            queue_path=tmp_path / "queue.json",
            plan_recheck_min=plan_recheck_min,
        )
    return now_box["t"]


def test_run_scheduler_loop_demotes_scheduled_account_that_turned_free(monkeypatch, tmp_path):
    _stub_usage_fetch(monkeypatch, weekly_used_percent=1, five_hour_reset_at=99999)
    monkeypatch.setattr(usage, "compute_next_run", lambda status, threshold, fallback_min, now: 999999)

    def fake_check(account_id, tool="claude"):
        if account_id == 1:
            raise accounts.AccountStatusError("유료 구독이 아님 (subscriptionType=free)")
        return "pro"

    monkeypatch.setattr(accounts, "check_paid_subscription", fake_check)

    states = {
        1: scheduler.AccountState(next_run_at=100, status="scheduled", plan="pro"),
        2: scheduler.AccountState(next_run_at=200, status="scheduled"),
    }
    ran = []

    def fake_run_claude(account_id):
        ran.append(account_id)
        raise _StopLoop()

    _run_loop_with_fake_clock(states, tmp_path, fake_run_claude)

    assert ran == [2]
    assert states[1].status == "free_skip"
    assert states[1].next_run_at == 100 + 10 * 60


def test_run_scheduler_loop_rechecks_free_skip_and_runs_once_it_turns_paid(monkeypatch, tmp_path):
    _stub_usage_fetch(monkeypatch)
    checks = []

    def fake_check(account_id, tool="claude"):
        checks.append(account_id)
        if len(checks) < 3:
            raise accounts.AccountStatusError("유료 구독이 아님 (subscriptionType=free)")
        return "pro"

    monkeypatch.setattr(accounts, "check_paid_subscription", fake_check)

    states = {1: scheduler.AccountState(next_run_at=100, status="free_skip")}

    def fake_run_claude(account_id):
        raise _StopLoop()

    end = _run_loop_with_fake_clock(states, tmp_path, fake_run_claude)

    # 100: free → 700: free → 1300: paid(승격) → 1300: 실행 직전 재확인 후 실행
    assert end == 100 + 2 * 10 * 60
    assert states[1].status == "scheduled"
    assert states[1].plan == "pro"


def test_initialize_states_refreshes_usage_for_account_restored_from_saved_queue(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    _stub_usage_fetch(
        monkeypatch,
        five_hour_used_percent=42,
        five_hour_reset_at=1790496059,
        weekly_used_percent=17,
        weekly_reset_at=1791073800,
    )

    existing_states = {
        1: scheduler.AccountState(
            next_run_at=12345,
            status="retry_pending",
            fail_count=3,
            five_hour_used_percent=1,
            five_hour_reset_at=1,
            weekly_used_percent=1,
            weekly_reset_at=1,
        ),
    }

    states = scheduler.initialize_states(
        [1], wait_until=None, delay_seconds=0, now=1000, existing_states=existing_states
    )

    # 스케줄/재시도 이력은 그대로 이어받되(스펙 요구), 한도 필드는 시작 시점에 항상 새로 조회한다.
    assert states[1].status == "retry_pending"
    assert states[1].next_run_at == 12345
    assert states[1].fail_count == 3
    assert states[1].five_hour_used_percent == 42
    assert states[1].five_hour_reset_at == 1790496059
    assert states[1].weekly_used_percent == 17
    assert states[1].weekly_reset_at == 1791073800


def test_initialize_states_gate_checks_account_not_in_existing_states(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    _stub_usage_fetch(monkeypatch)

    states = scheduler.initialize_states(
        [1], wait_until=None, delay_seconds=0, now=1000, existing_states={}
    )

    assert states[1].status == "scheduled"
    assert states[1].next_run_at == 1000


def test_main_loads_existing_queue_and_skips_paid_check_for_retry_pending_account(monkeypatch, tmp_path, capsys):
    queue_path = tmp_path / "queue.json"
    scheduler.save_queue(
        queue_path,
        scheduler.state_to_dict(
            {1: scheduler.AccountState(next_run_at=999999, status="retry_pending", fail_count=2)}
        ),
    )
    monkeypatch.setattr(scheduler, "QUEUE_PATH", queue_path)

    def fail_if_called(account_id):
        raise AssertionError("check_paid_subscription should not be called for an account loaded from the queue")

    monkeypatch.setattr(accounts, "check_paid_subscription", fail_if_called)
    _stub_usage_fetch(monkeypatch)

    captured_states = {}

    def fake_run_scheduler_loop(states, **kwargs):
        captured_states.update(states)

    monkeypatch.setattr(scheduler, "run_scheduler_loop", fake_run_scheduler_loop)

    rc = scheduler.main(["1"])

    assert rc == 0
    assert captured_states[1].status == "retry_pending"
    assert captured_states[1].next_run_at == 999999
    assert captured_states[1].fail_count == 2


def test_state_from_dict_ignores_unknown_extra_key():
    raw = {
        "1": {
            "next_run_at": 100,
            "status": "scheduled",
            "fail_count": 0,
            "totally_unknown_field": "should be dropped",
        }
    }

    result = scheduler.state_from_dict(raw)

    assert result[1] == scheduler.AccountState(next_run_at=100, status="scheduled", fail_count=0)


def test_state_from_dict_omits_entry_missing_required_key():
    raw = {"1": {"status": "scheduled"}}  # missing next_run_at

    result = scheduler.state_from_dict(raw)

    assert result == {}


# --- Fix 4: reject negative --delay/--interval ---


def test_main_rejects_negative_delay_without_entering_loop(monkeypatch, capsys):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("negative --delay이지만 스케줄러가 시작됨")

    monkeypatch.setattr(scheduler, "initialize_states", fail_if_called)
    monkeypatch.setattr(scheduler, "run_scheduler_loop", fail_if_called)

    rc = scheduler.main(["1", "--delay", "-5"])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.err


def test_main_rejects_negative_interval_without_entering_loop(monkeypatch, capsys):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("negative --interval이지만 스케줄러가 시작됨")

    monkeypatch.setattr(scheduler, "initialize_states", fail_if_called)
    monkeypatch.setattr(scheduler, "run_scheduler_loop", fail_if_called)

    rc = scheduler.main(["1", "--interval", "-5"])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.err


# --- Queue status printing ---


def test_format_queue_summary_shows_each_account_status():
    states = {
        1: scheduler.AccountState(next_run_at=1790496059, status="scheduled", fail_count=0),
        2: scheduler.AccountState(next_run_at=None, status="free_skip", fail_count=0),
        3: scheduler.AccountState(next_run_at=1790469300, status="retry_pending", fail_count=3),
    }

    summary = scheduler.format_queue_summary(states)

    assert "user1" in summary
    assert "상태: scheduled" in summary
    assert "user2" in summary
    assert "상태: free_skip" in summary
    assert "user3" in summary
    assert "상태: retry_pending" in summary
    assert "실패 3회" in summary


def test_format_queue_summary_includes_email_when_known():
    states = {
        1: scheduler.AccountState(next_run_at=1790496059, status="scheduled", email="a@example.com"),
        2: scheduler.AccountState(next_run_at=None, status="free_skip"),
    }

    summary = scheduler.format_queue_summary(states)

    assert "계정: a@example.com" in summary
    # user2에는 email이 없으므로 계정 줄 자체가 생략된다.
    assert summary.count("계정:") == 1


def test_format_queue_summary_wraps_entries_in_header_and_footer():
    states = {1: scheduler.AccountState(next_run_at=1790496059, status="scheduled")}

    summary = scheduler.format_queue_summary(states)
    lines = summary.splitlines()

    assert lines[0] == "==========큐상태============"
    assert set(lines[-1]) == {"="}
    assert len(lines[-1]) == len(lines[0])


def test_format_queue_summary_includes_date_not_just_time_of_day():
    # 다음 실행이 날짜가 바뀔 만큼 먼 미래(예: 주간 리셋)일 수 있으므로,
    # 시:분:초만 찍으면 "오늘인지 며칠 뒤인지" 구분이 안 되는 버그가 된다.
    from datetime import datetime

    next_run_at = 1790496059
    expected_date = datetime.fromtimestamp(next_run_at).strftime("%Y-%m-%d")

    states = {
        1: scheduler.AccountState(next_run_at=next_run_at, status="scheduled"),
    }

    summary = scheduler.format_queue_summary(states)

    assert expected_date in summary


def test_format_queue_summary_orders_by_account_id_when_next_run_at_ties():
    states = {
        3: scheduler.AccountState(next_run_at=100, status="scheduled"),
        1: scheduler.AccountState(next_run_at=100, status="scheduled"),
        2: scheduler.AccountState(next_run_at=100, status="scheduled"),
    }

    summary = scheduler.format_queue_summary(states)

    assert summary.index("user1") < summary.index("user2") < summary.index("user3")


def test_format_queue_summary_orders_by_next_run_at_not_account_id():
    # 실사례 버그 리포트: account_id 순서로 나오면 "다음 실행 순서"처럼 보여서
    # 헷갈린다는 지적 — 실제 다음 실행 시각 순서로 나와야 한다.
    states = {
        1: scheduler.AccountState(next_run_at=300, status="scheduled"),  # 가장 나중
        2: scheduler.AccountState(next_run_at=100, status="scheduled"),  # 가장 이름
        3: scheduler.AccountState(next_run_at=200, status="scheduled"),  # 중간
    }

    summary = scheduler.format_queue_summary(states)

    assert summary.index("user2") < summary.index("user3") < summary.index("user1")


def test_format_queue_summary_places_free_skip_accounts_last():
    states = {
        1: scheduler.AccountState(next_run_at=None, status="free_skip"),
        2: scheduler.AccountState(next_run_at=500, status="scheduled"),
    }

    summary = scheduler.format_queue_summary(states)

    assert summary.index("user2") < summary.index("user1")


def test_format_queue_summary_includes_usage_limits_when_known():
    states = {
        1: scheduler.AccountState(
            next_run_at=1790496059,
            status="scheduled",
            five_hour_used_percent=42,
            five_hour_reset_at=1790496059,
            weekly_used_percent=17,
            weekly_reset_at=1791073800,
        ),
    }

    summary = scheduler.format_queue_summary(states)

    assert "5시간 한도: 42%" in summary
    assert "주간 한도: 17%" in summary


def test_format_queue_summary_omits_usage_limits_when_unknown():
    states = {
        2: scheduler.AccountState(next_run_at=None, status="free_skip"),
    }

    summary = scheduler.format_queue_summary(states)

    assert "user2" in summary
    assert "상태: free_skip" in summary
    assert "5시간" not in summary
    assert "주간" not in summary


def test_run_scheduler_loop_prints_queue_summary_each_iteration(monkeypatch, tmp_path, capsys):
    states = {
        1: scheduler.AccountState(next_run_at=200, status="scheduled"),
        2: scheduler.AccountState(next_run_at=100, status="scheduled"),
    }

    call_order = []

    class _StopLoop(Exception):
        pass

    def fake_run_claude(account_id):
        call_order.append(account_id)
        if len(call_order) == 2:
            raise _StopLoop()
        return True

    def fake_fetch(config_dir):
        return {"five_hour_used_percent": 1, "five_hour_reset_at": 99999, "weekly_used_percent": 1, "weekly_reset_at": 99999}

    monkeypatch.setattr(accounts, "account_dir", lambda account_id, tool="claude": Path(f"/fake/{account_id}"))
    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)
    monkeypatch.setattr(usage, "compute_next_run", lambda status, threshold, fallback_min, now: 999999)

    now_box = {"t": 100}

    def fake_now():
        return now_box["t"]

    def fake_sleep(seconds):
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

    captured = capsys.readouterr()
    assert captured.out.count("==========큐상태============") >= 2
    assert "user1" in captured.out
    assert "user2" in captured.out
    assert captured.out.count("상태: scheduled") >= 2
