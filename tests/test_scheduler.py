import heapq
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
