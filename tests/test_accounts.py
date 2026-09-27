import json
from pathlib import Path

import pytest
import subprocess

import accounts


# 원본 함수는 아래 live-plan 테스트에서 직접 검증하기 위해 보관해 둔다.
_real_fetch_claude_live_plan = accounts.fetch_claude_live_plan
_real_fetch_codex_live_plan = accounts.fetch_codex_live_plan


@pytest.fixture(autouse=True)
def _no_live_plan_lookup(monkeypatch):
    # 실시간 구독 조회는 네트워크를 타므로 기본은 "정보 없음"(None)으로 막아
    # 로컬 subscriptionType만으로 판단하게 한다. 필요한 테스트만 덮어쓴다.
    monkeypatch.setattr(accounts, "fetch_claude_live_plan", lambda config_dir: None)
    monkeypatch.setattr(accounts, "fetch_codex_live_plan", lambda config_dir: None)


def test_account_dir_for_account_1_is_not_the_users_own_dot_claude(monkeypatch, tmp_path):
    # user1도 다른 계정과 같은 규칙을 쓴다 — ~/.claude는 사용자가 직접
    # `claude`를 실행할 때 쓰는 폴더이므로 스케줄러가 건드리지 않는다.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.account_dir(1) == tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-1"


def test_account_dir_for_account_n_uses_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.account_dir(3) == tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-3"


def test_account_dir_for_codex_account_1_is_not_the_users_own_dot_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.account_dir(1, tool="codex") == tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-1"


def test_account_dir_for_codex_account_n_uses_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert accounts.account_dir(4, tool="codex") == tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-4"


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


@pytest.mark.parametrize("plan", ["plus", "pro", "team", "business", "enterprise"])
def test_classify_subscription_accepts_codex_paid_plans(plan):
    status = {"loggedIn": True, "authMethod": "chatgpt", "subscriptionType": plan}
    assert accounts.classify_subscription(status) == plan


def test_classify_subscription_rejects_codex_free_plan():
    status = {"loggedIn": True, "authMethod": "chatgpt", "subscriptionType": "free"}
    with pytest.raises(accounts.AccountStatusError):
        accounts.classify_subscription(status)


class _FakeCompleted:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_run_claude_auth_status_sets_config_dir_for_account_1_too(monkeypatch, tmp_path):
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
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-1")
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

    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(
        tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-2"
    )


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


def _make_jwt(claims: dict) -> str:
    import base64
    import json as _j

    def _b64(obj):
        return base64.urlsafe_b64encode(_j.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig"


def _write_codex_auth(config_dir: Path, claims: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    id_token = _make_jwt(claims)
    (config_dir / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": id_token, "access_token": "at-1"}})
    )


def test_run_claude_auth_status_codex_decodes_id_token(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_codex_auth(
        tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-4",
        {
            "email": "a@example.com",
            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
        },
    )

    status = accounts.run_claude_auth_status(4, tool="codex")

    assert status["loggedIn"] is True
    assert status["authMethod"] == "chatgpt"
    assert status["subscriptionType"] == "plus"
    assert status["email"] == "a@example.com"


def test_run_claude_auth_status_codex_raises_when_auth_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(accounts.AccountStatusError):
        accounts.run_claude_auth_status(4, tool="codex")


def test_run_claude_auth_status_codex_reports_logged_out_when_no_id_token(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_dir = tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-1"
    config_dir.mkdir(parents=True)
    (config_dir / "auth.json").write_text(json.dumps({"tokens": {}}))

    status = accounts.run_claude_auth_status(1, tool="codex")

    assert status["loggedIn"] is False


def test_check_paid_subscription_codex_returns_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_codex_auth(
        tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-1",
        {"email": "a@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}},
    )

    assert accounts.check_paid_subscription(1, tool="codex") == "pro"


def test_account_email_codex_returns_email(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_codex_auth(
        tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-1",
        {"email": "a@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}},
    )

    assert accounts.account_email(1, tool="codex") == "a@example.com"


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


def test_account_email_returns_email_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted(
            '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro", "email": "user1@example.com"}'
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    assert accounts.account_email(1) == "user1@example.com"


def test_account_email_returns_none_when_field_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted('{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    assert accounts.account_email(1) is None


def test_account_email_returns_none_instead_of_raising_on_status_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd, capture_output, text, env, check):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    assert accounts.account_email(1) is None


def test_add_account_creates_dir_and_runs_login(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake_run(cmd, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeCompleted("", 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")

    accounts.add_account(2)

    expected_dir = tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-2"
    assert expected_dir.is_dir()
    assert captured["cmd"] == ["claude", "auth", "login"]
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(expected_dir)
    assert json.loads((expected_dir / ".claude.json").read_text())["hasCompletedOnboarding"] is True
    assert (expected_dir / "settings.json").resolve() == (tmp_path / ".claude" / "settings.json").resolve()


def test_mark_onboarding_complete_preserves_existing_keys(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "a@example.com"}}))

    accounts.mark_onboarding_complete(tmp_path)

    config = json.loads((tmp_path / ".claude.json").read_text())
    assert config["hasCompletedOnboarding"] is True
    assert config["oauthAccount"] == {"emailAddress": "a@example.com"}
    assert (tmp_path / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_add_account_codex_creates_dir_and_runs_codex_login(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured = {}

    def fake_run(cmd, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeCompleted("", 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    accounts.add_account(4, tool="codex")

    expected_dir = tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-4"
    assert expected_dir.is_dir()
    assert captured["cmd"] == ["codex", "login"]
    assert captured["env"]["CODEX_HOME"] == str(expected_dir)


def test_remove_account_moves_to_backup_path(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-2"
    target.mkdir(parents=True)

    backup = accounts.remove_account(2)

    assert not target.exists()
    assert backup.exists()
    assert str(backup).startswith(str(target) + ".removed-")


def test_remove_account_1_raises_value_error(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-1").mkdir(parents=True)

    with pytest.raises(ValueError):
        accounts.remove_account(1)


def test_remove_account_missing_raises_file_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(FileNotFoundError):
        accounts.remove_account(9)


def test_list_accounts_reports_status_for_each(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    account1_dir = tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-1"
    account2_dir = tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-2"
    account1_dir.mkdir(parents=True)
    account2_dir.mkdir(parents=True)

    responses = {
        str(account1_dir): '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro", "email": "a@example.com"}',
        str(account2_dir): '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "free"}',
    }

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted(responses[env["CLAUDE_CONFIG_DIR"]])

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    result = accounts.list_accounts()
    result_by_id = {r[0]: r for r in result}

    assert result_by_id[1][1] == "pro"
    assert result_by_id[2][1].startswith("SKIP:")
    assert result_by_id[1][3] == "a@example.com"
    assert result_by_id[2][3] is None
    assert result_by_id[1][4] == "claude"


def test_list_accounts_discovers_codex_dirs_and_uses_get_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    account4_dir = tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-4"
    (tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-1").mkdir(parents=True)
    _write_codex_auth(
        account4_dir,
        {"email": "b@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}},
    )

    def fake_run(cmd, capture_output, text, env, check):
        return _FakeCompleted(
            '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro", "email": "a@example.com"}'
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "subprocess", subprocess)

    def get_tool(account_id):
        return "codex" if account_id == 4 else "claude"

    result = accounts.list_accounts(get_tool=get_tool)
    result_by_id = {r[0]: r for r in result}

    assert result_by_id[1][4] == "claude"
    assert result_by_id[4][4] == "codex"
    assert result_by_id[4][1] == "plus"
    assert result_by_id[4][3] == "b@example.com"
    assert result_by_id[4][2] == account4_dir


import time


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise accounts.requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._payload


def _fake_claude_auth_status(monkeypatch, subscription_type):
    payload = json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": subscription_type}
    )
    monkeypatch.setattr(subprocess, "run", lambda cmd, capture_output, text, env, check: _FakeCompleted(payload))


def test_check_paid_subscription_detects_downgrade_despite_stale_local_pro(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _fake_claude_auth_status(monkeypatch, "pro")
    monkeypatch.setattr(accounts, "fetch_claude_live_plan", lambda config_dir: "free")

    with pytest.raises(accounts.AccountStatusError, match="free"):
        accounts.check_paid_subscription(1)


def test_check_paid_subscription_detects_upgrade_despite_stale_local_free(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _fake_claude_auth_status(monkeypatch, "free")
    monkeypatch.setattr(accounts, "fetch_claude_live_plan", lambda config_dir: "max")

    assert accounts.check_paid_subscription(2) == "max"


def test_check_paid_subscription_falls_back_to_local_plan_when_live_lookup_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _fake_claude_auth_status(monkeypatch, "pro")

    def boom(config_dir):
        raise accounts.requests.HTTPError("429 Too Many Requests")

    monkeypatch.setattr(accounts, "fetch_claude_live_plan", boom)

    assert accounts.check_paid_subscription(1) == "pro"
    assert "실시간 구독 조회 실패" in capsys.readouterr().err


def test_check_paid_subscription_codex_detects_downgrade_via_live_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_codex_auth(
        tmp_path / ".usage-reset-scheduler" / "accounts" / "codex-1",
        {"email": "a@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}},
    )
    monkeypatch.setattr(accounts, "fetch_codex_live_plan", lambda config_dir: "free")

    with pytest.raises(accounts.AccountStatusError, match="free"):
        accounts.check_paid_subscription(1, tool="codex")


def test_check_paid_subscription_skips_live_lookup_when_logged_out(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, capture_output, text, env, check: _FakeCompleted('{"loggedIn": false}')
    )

    def fail_if_called(config_dir):
        raise AssertionError("live plan lookup should not run for a logged-out account")

    monkeypatch.setattr(accounts, "fetch_claude_live_plan", fail_if_called)

    with pytest.raises(accounts.AccountStatusError, match="로그인"):
        accounts.check_paid_subscription(1)


@pytest.mark.parametrize(
    "org_type, expected",
    [("claude_free", "free"), ("claude_pro", "pro"), ("claude_max", "max"), ("", None)],
)
def test_fetch_claude_live_plan_maps_organization_type(monkeypatch, tmp_path, org_type, expected):
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    captured = {}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["auth"] = headers["Authorization"]
        return _FakeResponse({"organization": {"organization_type": org_type}})

    monkeypatch.setattr(accounts.requests, "get", fake_get)

    assert _real_fetch_claude_live_plan(tmp_path) == expected
    assert captured["url"] == accounts.CLAUDE_PROFILE_URL
    assert captured["auth"] == "Bearer tok"


def test_fetch_claude_live_plan_raises_on_http_error(monkeypatch, tmp_path):
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    monkeypatch.setattr(accounts.requests, "get", lambda url, headers, timeout: _FakeResponse({}, status_code=429))

    with pytest.raises(accounts.requests.HTTPError):
        _real_fetch_claude_live_plan(tmp_path)


def test_fetch_codex_live_plan_reads_plan_type(monkeypatch, tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "at-1"}}))
    monkeypatch.setattr(
        accounts.requests, "get", lambda url, headers, timeout: _FakeResponse({"plan_type": "Plus"})
    )

    assert _real_fetch_codex_live_plan(tmp_path) == "plus"


def _write_claude_credentials(config_dir: Path, expires_at_ms: int) -> None:
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": expires_at_ms}})
    )


def test_fetch_claude_live_plan_treats_401_on_unexpired_token_as_revoked(monkeypatch, tmp_path):
    _write_claude_credentials(tmp_path, int((time.time() + 3600) * 1000))
    monkeypatch.setattr(accounts.requests, "get", lambda url, headers, timeout: _FakeResponse({}, status_code=401))

    with pytest.raises(accounts.AccountStatusError, match="폐기"):
        _real_fetch_claude_live_plan(tmp_path)


def test_fetch_claude_live_plan_treats_401_on_expired_token_as_transient(monkeypatch, tmp_path):
    _write_claude_credentials(tmp_path, int((time.time() - 60) * 1000))
    monkeypatch.setattr(accounts.requests, "get", lambda url, headers, timeout: _FakeResponse({}, status_code=401))

    with pytest.raises(accounts.requests.HTTPError):
        _real_fetch_claude_live_plan(tmp_path)


def test_check_paid_subscription_rejects_revoked_token_instead_of_falling_back(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _fake_claude_auth_status(monkeypatch, "pro")

    def revoked(config_dir):
        raise accounts.AccountStatusError("OAuth 토큰이 폐기됨, 재로그인 필요 (401)")

    monkeypatch.setattr(accounts, "fetch_claude_live_plan", revoked)

    with pytest.raises(accounts.AccountStatusError, match="폐기"):
        accounts.check_paid_subscription(1)


def _make_main_claude_home(home: Path) -> Path:
    source = home / ".claude"
    (source / "skills" / "my-skill").mkdir(parents=True)
    (source / "plugins").mkdir()
    (source / "settings.json").write_text('{"enabledPlugins": {"x@y": true}}')
    (source / "CLAUDE.md").write_text("# shared")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}))
    return source


def test_share_claude_config_links_entries_and_backs_up_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = _make_main_claude_home(tmp_path)
    account = tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-2"
    account.mkdir(parents=True)
    (account / "settings.json").write_text('{"theme": "dark"}')
    (account / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "b@example.com"}, "mcpServers": {"notion": {"url": "n"}}})
    )

    linked = accounts.share_claude_config(account)

    assert set(linked) == {"settings.json", "CLAUDE.md", "skills", "plugins"}
    for name in linked:
        assert (account / name).is_symlink()
        assert (account / name).resolve() == (source / name).resolve()
    assert (account / "skills" / "my-skill").is_dir()
    backups = list(account.glob(".pre-shared-*"))
    assert len(backups) == 1
    assert json.loads((backups[0] / "settings.json").read_text()) == {"theme": "dark"}

    config = json.loads((account / ".claude.json").read_text())
    assert config["oauthAccount"] == {"emailAddress": "b@example.com"}
    assert config["mcpServers"] == {"notion": {"url": "n"}, "github": {"command": "gh-mcp"}}


def test_share_claude_config_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _make_main_claude_home(tmp_path)
    account = tmp_path / ".usage-reset-scheduler" / "accounts" / "claude-2"
    account.mkdir(parents=True)

    accounts.share_claude_config(account)
    assert accounts.share_claude_config(account) == []
    assert list(account.glob(".pre-shared-*")) == []


def test_share_claude_config_never_touches_the_users_own_dot_claude(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = _make_main_claude_home(tmp_path)

    assert accounts.share_claude_config(source) == []
    assert not (source / "settings.json").is_symlink()


def test_share_claude_config_links_account_1_like_any_other(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = _make_main_claude_home(tmp_path)
    account1 = accounts.account_dir(1)
    account1.mkdir(parents=True)

    linked = accounts.share_claude_config(account1)

    assert "settings.json" in linked
    assert (account1 / "settings.json").resolve() == (source / "settings.json").resolve()


def test_share_claude_config_all_covers_every_claude_account_and_skips_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _make_main_claude_home(tmp_path)
    root = tmp_path / ".usage-reset-scheduler" / "accounts"
    for name in ("claude-1", "claude-2", "claude-3", "codex-4"):
        (root / name).mkdir(parents=True)

    results = accounts.share_claude_config_all()

    assert set(results) == {1, 2, 3}
    assert not (root / "codex-4" / "settings.json").exists()
