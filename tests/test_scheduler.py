import heapq
import json as _json
import subprocess
from pathlib import Path

import pytest

import accounts
import scheduler
import usage


def test_state_round_trip_through_dict():
    states = {
        1: scheduler.AccountState(next_run_at=1000, status="scheduled", fail_count=0),
        2: scheduler.AccountState(next_run_at=None, status="free_skip", fail_count=0),
    }
    raw = scheduler.state_to_dict(states)
    restored = scheduler.state_from_dict(raw)

    assert restored[1] == states[1]
    assert restored[2] == states[2]


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


def test_run_claude_extracts_text_deltas_and_returns_true_on_success(monkeypatch, tmp_path):
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


def test_run_claude_skips_non_dict_json_line_and_still_extracts_valid_text_deltas(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "account_dir", lambda account_id: tmp_path / f"acct{account_id}")

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
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id: log_dir)

    result = scheduler.run_claude(1, "claude-haiku-4-5", "low")

    assert result is True
    logged_files = list(log_dir.glob("loop-*.log"))
    assert len(logged_files) == 1
    assert logged_files[0].read_text() == "OK"


def test_run_claude_skips_non_dict_event_field_and_still_extracts_valid_text_deltas(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "account_dir", lambda account_id: tmp_path / f"acct{account_id}")

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
    monkeypatch.setattr(scheduler, "log_dir_for_account", lambda account_id: log_dir)

    result = scheduler.run_claude(1, "claude-haiku-4-5", "low")

    assert result is True
    logged_files = list(log_dir.glob("loop-*.log"))
    assert len(logged_files) == 1
    assert logged_files[0].read_text() == "OK"


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

    monkeypatch.setattr(accounts, "account_dir", lambda account_id: Path(f"/fake/{account_id}"))
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

    monkeypatch.setattr(accounts, "account_dir", lambda account_id: Path(f"/fake/{account_id}"))

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

    monkeypatch.setattr(accounts, "account_dir", lambda account_id: Path(f"/fake/{account_id}"))
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


def test_initialize_states_preserves_existing_retry_pending_without_paid_check(monkeypatch):
    def fail_if_called(account_id):
        raise AssertionError("check_paid_subscription should not be called for an account already in the saved queue")

    monkeypatch.setattr(accounts, "check_paid_subscription", fail_if_called)

    existing_states = {
        1: scheduler.AccountState(next_run_at=12345, status="retry_pending", fail_count=3),
    }

    states = scheduler.initialize_states(
        [1], wait_until=None, delay_seconds=0, now=1000, existing_states=existing_states
    )

    assert states[1].status == "retry_pending"
    assert states[1].next_run_at == 12345
    assert states[1].fail_count == 3


def test_initialize_states_preserves_existing_scheduled_without_paid_check(monkeypatch):
    def fail_if_called(account_id):
        raise AssertionError("check_paid_subscription should not be called for an account already scheduled")

    monkeypatch.setattr(accounts, "check_paid_subscription", fail_if_called)

    existing_states = {
        1: scheduler.AccountState(next_run_at=5000, status="scheduled", fail_count=0),
    }

    states = scheduler.initialize_states(
        [1], wait_until=None, delay_seconds=0, now=1000, existing_states=existing_states
    )

    assert states[1].status == "scheduled"
    assert states[1].next_run_at == 5000


def test_initialize_states_gate_checks_account_not_in_existing_states(monkeypatch):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id: "pro")

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
    raw = {"1": {"next_run_at": 100, "status": "scheduled", "fail_count": 0, "plan": "pro"}}

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
