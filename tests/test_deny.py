import os
import pytest
from omg_cli.deny import should_deny_command, decide_pre_tool_use


@pytest.mark.parametrize("cmd", [
    "claude -p 'hi'",
    "codex exec foo",
    "/usr/local/bin/claude",
    "env claude -p x",
    "OMG_ALLOW_EXTERNAL_CLI=1 claude -p x",  # must STILL deny if process env unset
    "sh -c 'claude -p x'",
    "command codex exec",
    "xargs claude",
    "omc team 2:codex 'x'",
    "agy -p x",
    "cursor-agent -p x",
])
def test_deny_external_cli(cmd):
    assert should_deny_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "grok -p 'hi'",
    "pytest tests/",
    "python3 scripts/foo.py",
    "git status",
    "echo claude is a word",  # word not as executable head
])
def test_allow_benign(cmd):
    assert should_deny_command(cmd) is False


def test_process_env_allow_only_when_set(monkeypatch):
    monkeypatch.delenv("OMG_ALLOW_EXTERNAL_CLI", raising=False)
    d = decide_pre_tool_use({"toolName": "run_terminal_command", "toolInput": {"command": "claude -p x"}})
    assert d["decision"] == "deny"
    monkeypatch.setenv("OMG_ALLOW_EXTERNAL_CLI", "1")
    d = decide_pre_tool_use({"toolName": "run_terminal_command", "toolInput": {"command": "claude -p x"}})
    assert d["decision"] == "allow"
