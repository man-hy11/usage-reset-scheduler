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
