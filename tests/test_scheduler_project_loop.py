import os
import signal
import sys
import time

import pytest

import accounts
import project_loop
import registry
import scheduler
import step_runner
from step_runner import StepOutcome

LOW_USAGE = {"five_hour_used_percent": 10, "five_hour_reset_at": None, "weekly_used_percent": 10, "weekly_reset_at": None}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "check_paid_subscription", lambda account_id, tool="claude": "pro")
    monkeypatch.setattr(accounts, "share_claude_config_all", lambda: {})
    monkeypatch.setattr(registry, "get_tool", lambda path, account_id: "claude")
    monkeypatch.setattr(scheduler, "fetch_usage", lambda account_id, tool: dict(LOW_USAGE))
    monkeypatch.setattr(scheduler, "PROJECT_LOCK_ROOT", tmp_path / "locks")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "prompt.md").write_text("first prompt")
    return root


@pytest.fixture
def captured_loop(monkeypatch):
    captured = {}

    def fake_run_project_loop(loop_accounts, **kwargs):
        captured["accounts"] = loop_accounts
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(project_loop, "run_project_loop", fake_run_project_loop)
    return captured


@pytest.fixture
def step_calls(monkeypatch):
    calls = []

    def fake_run_step(account_id, tool, project_dir, prompt_text, model, effort):
        calls.append((account_id, project_dir, prompt_text, model, effort))
        return StepOutcome(step_status="COMPLETE")

    monkeypatch.setattr(step_runner, "run_step", fake_run_step)
    return calls


def test_parse_args_defaults_leave_project_loop_off():
    args = scheduler.parse_args(["1"])
    assert args.project_loop is False
    assert args.project_dir is None
    assert args.prompt_file is None


def test_project_dir_without_project_loop_is_rejected(project, capsys):
    assert scheduler.main(["--project-dir", str(project)]) == 1
    assert "--project-loop와 함께만" in capsys.readouterr().err


def test_project_loop_rejects_management_flags(project, capsys):
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "-l"]) == 1
    assert "함께 쓸 수 없습니다" in capsys.readouterr().err


def test_project_loop_requires_project_dir(capsys):
    assert scheduler.main(["--project-loop"]) == 1
    assert "--project-dir이 필요합니다" in capsys.readouterr().err


def test_project_loop_rejects_interval_below_one(project, captured_loop, capsys):
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "--interval", "0"]) == 1
    assert "--interval은 1 이상이어야 합니다" in capsys.readouterr().err
    assert "accounts" not in captured_loop


def test_project_loop_rejects_missing_dir_non_git_and_missing_prompt(tmp_path, capsys):
    assert scheduler.main(["--project-loop", "--project-dir", str(tmp_path / "nope")]) == 1
    plain = tmp_path / "plain"
    plain.mkdir()
    assert scheduler.main(["--project-loop", "--project-dir", str(plain)]) == 1
    (plain / ".git").mkdir()
    assert scheduler.main(["--project-loop", "--project-dir", str(plain)]) == 1
    err = capsys.readouterr().err
    assert "프로젝트 디렉터리가 없습니다" in err
    assert "Git 저장소가 아닙니다" in err
    assert "프롬프트 파일을 찾을 수 없습니다" in err


def test_project_loop_builds_accounts_and_injects_existing_functions(project, captured_loop):
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "1", "2"]) == 0
    assert [account.account_id for account in captured_loop["accounts"]] == [1, 2]
    assert all(account.status == "ready" for account in captured_loop["accounts"])
    assert captured_loop["interval_min"] == 5
    assert captured_loop["check_plan_fn"] is scheduler._check_plan


def test_project_loop_count_selects_paid_accounts(project, captured_loop, monkeypatch):
    monkeypatch.setattr(accounts, "known_account_ids", lambda: {1, 2, 3})
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "-n", "2"]) == 0
    assert [account.account_id for account in captured_loop["accounts"]] == [1, 2]


def test_run_step_reads_prompt_fresh_and_uses_claude_defaults(project, captured_loop, step_calls):
    scheduler.main(["--project-loop", "--project-dir", str(project)])
    run_step_fn = captured_loop["run_step_fn"]
    account = captured_loop["accounts"][0]

    run_step_fn(account)
    (project / "prompt.md").write_text("edited prompt")
    run_step_fn(account)

    assert step_calls[0] == (1, project.resolve(), "first prompt", "claude-opus-5-5", "high")
    assert step_calls[1][2] == "edited prompt"


def test_model_effort_override_and_custom_prompt_file(project, captured_loop, step_calls):
    (project / "other.md").write_text("other prompt")
    scheduler.main([
        "--project-loop", "--project-dir", str(project), "--prompt-file", "other.md",
        "--model", "claude-haiku-4-5", "--effort", "low",
    ])
    captured_loop["run_step_fn"](captured_loop["accounts"][0])
    assert step_calls[0] == (1, project.resolve(), "other prompt", "claude-haiku-4-5", "low")


def test_delay_pushes_first_run(project, captured_loop):
    before = int(time.time())
    scheduler.main(["--project-loop", "--project-dir", str(project), "-d", "5"])
    assert captured_loop["accounts"][0].available_at >= before + 300


def test_refuses_when_project_lock_is_held(project, captured_loop, tmp_path, capsys):
    fd = project_loop.acquire_project_lock(project_loop.lock_path_for(project, tmp_path / "locks"))
    project_loop.write_lock_info(fd, "PID 999, 시작 test")
    try:
        assert scheduler.main(["--project-loop", "--project-dir", str(project)]) == 1
    finally:
        project_loop.release_project_lock(fd)
    err = capsys.readouterr().err
    assert "이미 이 프로젝트에서 작업 루프가 실행 중입니다" in err
    assert "PID 999" in err
    assert "accounts" not in captured_loop


def test_ctrl_c_returns_130_and_releases_lock(project, monkeypatch, tmp_path):
    def interrupted(loop_accounts, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(project_loop, "run_project_loop", interrupted)
    assert scheduler.main(["--project-loop", "--project-dir", str(project)]) == 130
    fd = project_loop.acquire_project_lock(project_loop.lock_path_for(project, tmp_path / "locks"))
    project_loop.release_project_lock(fd)


def test_exits_when_no_account_is_paid(project, captured_loop, monkeypatch, capsys):
    def not_paid(account_id, tool="claude"):
        raise accounts.AccountStatusError("유료 구독이 아님")

    monkeypatch.setattr(accounts, "check_paid_subscription", not_paid)
    assert scheduler.main(["--project-loop", "--project-dir", str(project), "1", "2"]) == 1
    assert "유료 구독인 계정이 없습니다" in capsys.readouterr().err
    assert "accounts" not in captured_loop


def test_existing_reset_mode_never_enters_project_loop(monkeypatch):
    entered = []
    monkeypatch.setattr(scheduler, "_run_project_loop_mode", lambda *args: entered.append(args) or 0)
    monkeypatch.setattr(scheduler, "initialize_states", lambda *args, **kwargs: {})
    monkeypatch.setattr(scheduler, "run_scheduler_loop", lambda *args, **kwargs: None)
    assert scheduler.main(["1"]) == 0
    assert entered == []


def test_project_loop_makes_stdout_line_buffered(project, captured_loop, monkeypatch):
    import io
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", stream)
    assert stream.line_buffering is False
    scheduler.main(["--project-loop", "--project-dir", str(project)])
    assert stream.line_buffering is True


def test_sigterm_stops_loop_returns_130_restores_handler_and_releases_lock(project, monkeypatch, tmp_path):
    def terminated(loop_accounts, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        raise AssertionError("SIGTERM did not interrupt the loop")

    monkeypatch.setattr(project_loop, "run_project_loop", terminated)
    before = signal.getsignal(signal.SIGTERM)
    assert scheduler.main(["--project-loop", "--project-dir", str(project)]) == 130
    assert signal.getsignal(signal.SIGTERM) is before
    fd = project_loop.acquire_project_lock(project_loop.lock_path_for(project, tmp_path / "locks"))
    project_loop.release_project_lock(fd)


def test_ctrl_c_during_account_selection_returns_130_and_releases_lock(project, monkeypatch, tmp_path):
    def interrupted(account_id, tool):
        raise KeyboardInterrupt

    monkeypatch.setattr(scheduler, "_check_plan", interrupted)
    assert scheduler.main(["--project-loop", "--project-dir", str(project)]) == 130
    fd = project_loop.acquire_project_lock(project_loop.lock_path_for(project, tmp_path / "locks"))
    project_loop.release_project_lock(fd)
