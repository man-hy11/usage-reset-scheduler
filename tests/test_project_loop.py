import pytest

import project_loop
from project_loop import LoopAccount
from step_runner import StepOutcome

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


def test_lock_path_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    locks = tmp_path / "locks"
    home_path = project_loop.lock_path_for(project_loop.Path("~") / "something", locks)
    absolute_path = project_loop.lock_path_for(project_loop.Path.home() / "something", locks)
    assert home_path == absolute_path


def test_acquire_clears_stale_lock_info(tmp_path):
    path = project_loop.lock_path_for(tmp_path / "proj", tmp_path / "locks")
    fd = project_loop.acquire_project_lock(path)
    project_loop.write_lock_info(fd, "old")
    project_loop.release_project_lock(fd)

    fd2 = project_loop.acquire_project_lock(path)
    try:
        with pytest.raises(project_loop.ProjectLockError) as exc_info:
            project_loop.acquire_project_lock(path)
        assert str(exc_info.value) == "(실행 정보 없음)"
    finally:
        project_loop.release_project_lock(fd2)


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
