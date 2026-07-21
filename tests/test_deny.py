import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from omg_cli.deny import (
    decide_pre_tool_use,
    decide_spawn_subagent,
    should_deny_command,
)

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


def test_spawn_missing_capability_mode_denied():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "general-purpose",
                "prompt": "do work",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "capability_mode" in reason
    # Model must be told to retry, not abandon multi-agent
    assert "RETRY IMMEDIATELY" in reason
    assert "read-write" in reason
    assert "Do NOT abandon multi-agent" in reason


def test_spawn_missing_mode_explore_suggests_read_only():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "prompt": "map repo",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "RETRY IMMEDIATELY" in reason
    assert "capability_mode='read-only'" in reason or 'capability_mode="read-only"' in reason


def test_spawn_executor_read_write_allowed():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "omg-executor",
                "capability_mode": "read-write",
                "prompt": "implement",
            },
        }
    )
    assert d["decision"] == "allow"


def test_spawn_general_purpose_requires_read_write():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "general-purpose",
                "capability_mode": "read-only",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "read-write" in reason
    assert "RETRY IMMEDIATELY" in reason


def test_spawn_explore_requires_read_only():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "capability_mode": "read-write",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    assert "RETRY IMMEDIATELY" in d.get("reason", "")
    d2 = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "capability_mode": "read-only",
                "prompt": "x",
            },
        }
    )
    assert d2["decision"] == "allow"


def test_spawn_task_alias_and_camel_case_keys():
    d = decide_pre_tool_use(
        {
            "tool_name": "Task",
            "tool_input": {
                "subagentType": "omg-critic",
                "capabilityMode": "read-only",
                "prompt": "review",
            },
        }
    )
    assert d["decision"] == "allow"


def test_spawn_execute_mode_denied():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "general-purpose",
                "capability_mode": "execute",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "RETRY IMMEDIATELY" in reason
    assert "read-write" in reason


def test_spawn_all_mode_denied_with_retry():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "capability_mode": "all",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "RETRY IMMEDIATELY" in reason
    assert "read-only" in reason


def test_spawn_empty_type_missing_mode_denied():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {"prompt": "x"},
        }
    )
    assert d["decision"] == "deny"
    assert "RETRY IMMEDIATELY" in d.get("reason", "")


def test_spawn_invalid_mode_denied():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "capability_mode": "write-only",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    assert "RETRY IMMEDIATELY" in d.get("reason", "")
    assert "read-only" in d.get("reason", "")


def test_spawn_unsafe_env_allows_missing_mode(monkeypatch):
    monkeypatch.setenv("OMG_ALLOW_UNSAFE_SPAWN", "1")
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "allow"
    assert "OMG_ALLOW_UNSAFE_SPAWN" in d.get("reason", "")


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


def test_executor_nested_spawn_tools_denied():
    d = decide_spawn_subagent(
        {
            "subagent_type": "omg-executor",
            "capability_mode": "read-write",
            "tools": ["read_file", "spawn_subagent"],
        }
    )
    assert d["decision"] == "deny"
    assert "depth" in d["reason"].lower() or "spawn" in d["reason"].lower()


def test_depth_gt_1_denied():
    d = decide_spawn_subagent(
        {
            "subagent_type": "explore",
            "capability_mode": "read-only",
            "depth": 2,
        }
    )
    assert d["decision"] == "deny"


def test_multiline_command_deny_not_bypassed_by_newline():
    """A denied bin on its own line (newline, not ';') must still be denied.

    Multi-line shell scripts (heredocs, setup + run) are the common shape of
    real Bash/run_terminal_command payloads; the deny must not require a shell
    operator on the same line as the binary.
    """
    assert should_deny_command("echo start\nclaude -p 'hi'") is True
    assert should_deny_command("cd /tmp\ncodex exec foo") is True
    assert should_deny_command("set -e\n\n  agy -p go") is True
    assert should_deny_command("echo a\r\ncursor-agent --print x") is True
    # sanity: a plain multi-line script with no denied bin is still allowed
    assert should_deny_command("echo start\necho done") is False
    # the denied word as a mere argument mid-line stays allowed
    assert should_deny_command("echo run claude later\ntrue") is False
    # end-to-end through the PreToolUse decision
    ev = {
        "toolName": "run_terminal_command",
        "toolInput": {"command": "echo x\nagy -p go"},
    }
    assert decide_pre_tool_use(ev)["decision"] == "deny"
