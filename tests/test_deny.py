import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from omg_cli.deny import should_deny_command, decide_pre_tool_use

ROOT = Path(__file__).resolve().parents[1]
PRE_TOOL = ROOT / "hooks" / "bin" / "pre_tool_use_deny.py"


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
    # -lc / login-command forms
    "bash -lc 'claude -p x'",
    "zsh -lc \"codex exec foo\"",
    "bash -cl 'claude -p x'",
    # unquoted sh -c
    "sh -c claude",
    "bash -c claude -p x",
    "zsh -c /usr/bin/codex exec",
    # common wrappers
    "nohup claude -p x",
    "nice claude -p x",
    "sudo claude -p x",
    "time claude -p x",
    "nohup nice claude -p x",
    "sudo /usr/local/bin/codex exec",
    "env nohup claude -p x",
    # path-prefixed env / shell / eval
    "/usr/bin/env claude -p x",
    "/bin/bash -c 'claude -p x'",
    "/bin/bash -lc \"codex exec foo\"",
    "eval claude -p x",
    "exec claude -p x",
    "/usr/bin/env bash -c 'claude -p x'",
])
def test_deny_external_cli(cmd):
    assert should_deny_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "grok -p 'hi'",
    "pytest tests/",
    "python3 scripts/foo.py",
    "git status",
    "echo claude is a word",  # word not as executable head
    "echo bash -c claude",  # narrative mention after echo
    "ls -lc /tmp",  # -lc is not shell login-command for ls
    "nice pytest tests/",
    "nohup sleep 1",
    "sudo apt update",
    "time make",
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


def _run_pre_tool(event: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    run_env["PYTHONPATH"] = str(ROOT) + os.pathsep + run_env.get("PYTHONPATH", "")
    # Ensure allow-env is controlled by caller
    run_env.pop("OMG_ALLOW_EXTERNAL_CLI", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(PRE_TOOL)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=run_env,
        cwd=str(ROOT),
        check=False,
    )


def test_pre_tool_use_deny_exit_codes_cmd_string_env_still_deny():
    """OMG_ALLOW_EXTERNAL_CLI=1 inside command string must NOT allow; process env only."""
    event = {
        "toolName": "run_terminal_command",
        "toolInput": {"command": "OMG_ALLOW_EXTERNAL_CLI=1 claude -p x"},
    }
    proc = _run_pre_tool(event)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["decision"] == "deny"


def test_pre_tool_use_allow_exit_zero():
    event = {
        "toolName": "run_terminal_command",
        "toolInput": {"command": "git status"},
    }
    proc = _run_pre_tool(event)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["decision"] == "allow"
