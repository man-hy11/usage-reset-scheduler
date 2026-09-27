from pathlib import Path

import pytest

import registry


def test_get_tool_defaults_to_claude_when_unregistered(tmp_path):
    path = tmp_path / "accounts.json"
    assert registry.get_tool(path, 1) == "claude"


def test_get_tool_defaults_to_claude_when_file_missing(tmp_path):
    path = tmp_path / "does-not-exist.json"
    assert registry.get_tool(path, 4) == "claude"


def test_set_tool_then_get_tool_round_trips(tmp_path):
    path = tmp_path / "accounts.json"
    registry.set_tool(path, 4, "codex")
    assert registry.get_tool(path, 4) == "codex"
    # 등록 안 된 다른 계정은 여전히 기본값
    assert registry.get_tool(path, 1) == "claude"


def test_set_tool_rejects_unknown_tool(tmp_path):
    path = tmp_path / "accounts.json"
    with pytest.raises(ValueError):
        registry.set_tool(path, 4, "gemini")


def test_set_tool_preserves_other_entries(tmp_path):
    path = tmp_path / "accounts.json"
    registry.set_tool(path, 2, "claude")
    registry.set_tool(path, 4, "codex")

    assert registry.get_tool(path, 2) == "claude"
    assert registry.get_tool(path, 4) == "codex"


def test_load_registry_ignores_corrupted_file(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text("not valid json {{{")

    assert registry.load_registry(path) == {}
    assert registry.get_tool(path, 4) == "claude"


def test_load_registry_ignores_unknown_tool_value(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text('{"4": "gemini", "2": "codex"}')

    result = registry.load_registry(path)
    assert 4 not in result
    assert result[2] == "codex"


def test_set_tool_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "accounts.json"
    registry.set_tool(path, 1, "claude")
    assert path.exists()
