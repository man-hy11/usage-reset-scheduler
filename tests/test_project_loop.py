import pytest

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
