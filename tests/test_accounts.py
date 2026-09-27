from pathlib import Path

import pytest
import subprocess

import accounts


def test_account_dir_for_account_1_is_dot_claude(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.account_dir(1) == tmp_path / ".claude"


def test_account_dir_for_account_n_uses_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.account_dir(3) == tmp_path / ".claude-account-3"


@pytest.mark.parametrize("plan", ["pro", "max", "team", "enterprise"])
def test_classify_subscription_accepts_paid_plans(plan):
    status = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": plan}
    assert accounts.classify_subscription(status) == plan


def test_classify_subscription_rejects_free_plan():
    status = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "free"}
    with pytest.raises(accounts.AccountStatusError):
        accounts.classify_subscription(status)


def test_classify_subscription_rejects_logged_out():
    status = {"loggedIn": False, "authMethod": "none", "subscriptionType": None}
    with pytest.raises(accounts.AccountStatusError):
        accounts.classify_subscription(status)


def test_classify_subscription_rejects_api_key_auth():
    status = {"loggedIn": True, "authMethod": "apiKey", "subscriptionType": "pro"}
    with pytest.raises(accounts.AccountStatusError):
        accounts.classify_subscription(status)


class _FakeCompleted:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_run_claude_auth_status_uses_account_1_without_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake_run(cmd, capture_output, text, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    result = accounts.run_claude_auth_status(1)

    assert result["subscriptionType"] == "pro"
    assert "CLAUDE_CONFIG_DIR" not in captured["env"]
    assert captured["cmd"] == ["claude", "auth", "status", "--json"]


def test_run_claude_auth_status_sets_config_dir_for_account_n(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake_run(cmd, capture_output, text, env, check):
        captured["env"] = env
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "free"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    accounts.run_claude_auth_status(2)

    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-account-2")


def test_run_claude_auth_status_raises_on_bad_json(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted("not-json")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    with pytest.raises(accounts.AccountStatusError):
        accounts.run_claude_auth_status(1)


def test_run_claude_auth_status_raises_on_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        raise subprocess.CalledProcessError(7, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    with pytest.raises(accounts.AccountStatusError):
        accounts.run_claude_auth_status(1)


def test_check_paid_subscription_returns_plan_for_paid_account(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    assert accounts.check_paid_subscription(3) == "max"


def test_check_paid_subscription_raises_for_free_account(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "free"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    with pytest.raises(accounts.AccountStatusError):
        accounts.check_paid_subscription(2)


def test_add_account_creates_dir_and_runs_login(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake_run(cmd, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeCompleted("", 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    accounts.add_account(2)

    assert (tmp_path / ".claude-account-2").is_dir()
    assert captured["cmd"] == ["claude", "auth", "login"]
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-account-2")


def test_remove_account_moves_to_backup_path(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".claude-account-2"
    target.mkdir()

    backup = accounts.remove_account(2)

    assert not target.exists()
    assert backup.exists()
    assert str(backup).startswith(str(tmp_path / ".claude-account-2.removed-"))


def test_remove_account_1_raises_value_error(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()

    with pytest.raises(ValueError):
        accounts.remove_account(1)


def test_remove_account_missing_raises_file_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(FileNotFoundError):
        accounts.remove_account(9)


def test_list_accounts_reports_status_for_each(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude-account-2").mkdir()

    responses = {
        str(tmp_path / ".claude"): '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}',
        str(tmp_path / ".claude-account-2"): '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "free"}',
    }

    def fake_run(cmd, capture_output, text, env, check):
        config_dir = env.get("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
        return _FakeCompleted(responses[config_dir])

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    result = accounts.list_accounts()
    result_by_id = {r[0]: r for r in result}

    assert result_by_id[1][1] == "pro"
    assert result_by_id[2][1].startswith("SKIP:")
