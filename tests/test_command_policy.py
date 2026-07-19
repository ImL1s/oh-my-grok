# tests/test_command_policy.py
"""Semantic acceptance command policy."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omg_cli.command_policy import (
    CommandPolicyError,
    check_command_policy,
    is_python_bin,
    resolve_allowlist,
    _basename_allowed,
)


def test_true_false_pytest_allowed():
    check_command_policy(["true"])
    check_command_policy(["false"])
    check_command_policy(["pytest", "tests/", "-q"])
    check_command_policy(["/usr/bin/pytest", "-q"])


def test_python_c_denied():
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy([sys.executable, "-c", "pass"])
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(["python3", "-c", "import os; os.system('claude')"])
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(["python", "-c", "print(1)"])


def test_python_m_pytest_allowed():
    check_command_policy(["python3", "-m", "pytest", "tests/", "-q"])
    check_command_policy(["python", "-m", "unittest", "discover"])
    check_command_policy(["python3.12", "-m", "pytest"])
    check_command_policy([sys.executable, "-m", "pytest", "-q"])


def test_python_m_other_denied():
    with pytest.raises(CommandPolicyError, match="-m"):
        check_command_policy(["python3", "-m", "http.server"])
    with pytest.raises(CommandPolicyError, match="-m"):
        check_command_policy(["python3", "-m", "pip", "install", "x"])


def test_python3evil_denied():
    with pytest.raises(CommandPolicyError, match="not in acceptance allowlist"):
        check_command_policy(["python3evil", "-m", "pytest"])
    allowed = resolve_allowlist()
    assert _basename_allowed("python3evil", allowed) is False
    assert _basename_allowed("python3-config", allowed) is False
    assert is_python_bin("python3.12") is True
    assert is_python_bin("python3evil") is False


def test_python_script_under_project(tmp_path):
    script = tmp_path / "tests" / "t.py"
    script.parent.mkdir(parents=True)
    script.write_text("print(1)\n", encoding="utf-8")
    check_command_policy(
        ["python3", "tests/t.py"],
        project_root=tmp_path,
    )
    # absolute outside project
    with pytest.raises(CommandPolicyError, match="not a .py path under the project"):
        check_command_policy(
            ["python3", "/etc/passwd.py"],
            project_root=tmp_path,
        )


def test_npx_denied():
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["npx", "eslint"])
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["npx", "claude"])


def test_npm_only_test_scripts():
    check_command_policy(["npm", "test"])
    check_command_policy(["npm", "run", "test"])
    check_command_policy(["npm", "run", "pytest", "--", "-q"])
    with pytest.raises(CommandPolicyError, match="npm"):
        check_command_policy(["npm", "install"])
    with pytest.raises(CommandPolicyError, match="npm"):
        check_command_policy(["npm", "run", "build"])


def test_shell_and_agent_always_denied():
    with pytest.raises(CommandPolicyError, match="shell interpreter"):
        check_command_policy(["bash", "-c", "true"])
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["claude", "--version"])
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["codex", "exec", "hi"])
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["rm", "-rf", "/"])


def test_no_allowlist_still_denies_floor():
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["claude"], no_allowlist=True)
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(["python3", "-c", "pass"], no_allowlist=True)
    with pytest.raises(CommandPolicyError, match="shell"):
        check_command_policy(["bash", "-c", "true"], no_allowlist=True)
    # break-glass can run non-default bins that are not on the floor
    check_command_policy(["curl", "https://example.com"], no_allowlist=True)


def test_allow_cmd_extends_but_not_floor():
    allowed = resolve_allowlist(["hello"])
    check_command_policy(["hello", "world"], allowlist=allowed)
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["claude"], allowlist=resolve_allowlist(["claude"]))


def test_node_eval_denied_even_if_allowed():
    allowed = resolve_allowlist(["node"])
    with pytest.raises(CommandPolicyError, match="-e"):
        check_command_policy(["node", "-e", "console.log(1)"], allowlist=allowed)

def test_glued_python_c_denied_even_with_no_allowlist():
    from omg_cli.command_policy import check_command_policy, CommandPolicyError
    from pathlib import Path
    import pytest
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(CommandPolicyError):
        check_command_policy(["python3", "-cimport os"], no_allowlist=True, project_root=root)
    with pytest.raises(CommandPolicyError):
        check_command_policy(["python3", "-c", "print(1)"], no_allowlist=True, project_root=root)
