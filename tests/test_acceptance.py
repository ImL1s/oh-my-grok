# tests/test_acceptance.py
"""Frozen acceptance runner + PRD schema + set_verified gate."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from omg_cli.acceptance import (
    CLI_WRITER,
    clear_cli_acceptance_tokens,
    freeze_acceptance,
    freeze_and_run,
    has_cli_acceptance_token,
    is_cli_acceptance_result,
    is_trusted_acceptance,
    load_prd,
    manifest_path,
    prd_has_acceptance_commands,
    result_path,
    run_acceptance,
    sanitized_env,
    sha_path,
    validate_prd,
)
from omg_cli.state import create_run, load_run, set_verified


def _valid_prd(**overrides):
    base = {
        "version": 1,
        "goal": "ship acceptance gate",
        "stories": [
            {
                "id": "s1",
                "title": "true command",
                "commands": [["true"]],
            }
        ],
        "global_commands": [],
    }
    base.update(overrides)
    return base


def test_validate_prd_ok():
    prd = validate_prd(_valid_prd())
    assert prd["version"] == 1
    assert prd["goal"] == "ship acceptance gate"
    assert prd["stories"][0]["id"] == "s1"
    assert prd["stories"][0]["commands"] == [["true"]]


def test_validate_prd_global_commands_only():
    prd = validate_prd(
        {
            "version": 1,
            "goal": "globals",
            "stories": [],
            "global_commands": [["true"]],
        }
    )
    assert prd["global_commands"] == [["true"]]


def test_bad_schema_fails():
    with pytest.raises(ValueError, match="version"):
        validate_prd({"goal": "x", "stories": []})

    with pytest.raises(ValueError, match="goal"):
        validate_prd({"version": 1, "goal": "", "stories": []})

    with pytest.raises(ValueError, match="no acceptance commands"):
        validate_prd(
            {
                "version": 1,
                "goal": "empty",
                "stories": [],
                "global_commands": [],
            }
        )

    with pytest.raises(ValueError, match="argv"):
        validate_prd(
            {
                "version": 1,
                "goal": "shell string not ok",
                "stories": [
                    {
                        "id": "s1",
                        "title": "bad",
                        "commands": ["pytest -q"],  # bare string, not argv array
                    }
                ],
            }
        )

    with pytest.raises(ValueError, match="id"):
        validate_prd(
            {
                "version": 1,
                "goal": "missing id",
                "stories": [{"title": "t", "commands": [["true"]]}],
            }
        )

    assert prd_has_acceptance_commands({"version": 1, "goal": "x", "stories": []}) is False
    assert prd_has_acceptance_commands(_valid_prd()) is True


def test_forge_passed_true_without_writer_rejected(tmp_path):
    clear_cli_acceptance_tokens()
    run = create_run(tmp_path, mode="ralph", goal="forge")
    rid = run["run_id"]
    # Agent-forged legacy paths
    for rel in (
        Path(".omg") / "state" / "runs" / rid / "acceptance.json",
        Path(".omg") / "artifacts" / f"{rid}-acceptance.json",
        Path(".omg") / "state" / "runs" / rid / "acceptance.result.json",
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"passed": True}), encoding="utf-8")

    with pytest.raises(PermissionError, match="CLI acceptance|acceptance|trusted"):
        set_verified(tmp_path, rid)

    # stamped but missing/wrong sha still fails
    result_path(tmp_path, rid).write_text(
        json.dumps(
            {
                "writer": CLI_WRITER,
                "passed": True,
                "manifest_sha256": "deadbeef" * 8,
                "results": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PermissionError):
        set_verified(tmp_path, rid)


def test_full_forge_with_correct_sha_without_run_acceptance_rejected(tmp_path):
    """CRITICAL: disk-forged full stamp (writer + sha + passed) cannot set_verified.

    Only an in-process token from run_acceptance makes the result trusted.
    """
    clear_cli_acceptance_tokens()
    run = create_run(tmp_path, mode="ralph", goal="full-forge")
    rid = run["run_id"]
    prd = _valid_prd(goal="full-forge")
    manifest = freeze_acceptance(tmp_path, rid, prd)
    digest = manifest["sha256"]

    # Agent forges a perfect-looking result without calling run_acceptance
    result_path(tmp_path, rid).write_text(
        json.dumps(
            {
                "writer": CLI_WRITER,
                "passed": True,
                "manifest_sha256": digest,
                "results": [
                    {"command": ["true"], "returncode": 0},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert has_cli_acceptance_token(tmp_path, rid) is False
    assert is_trusted_acceptance(tmp_path, rid) is False
    assert (
        is_cli_acceptance_result(
            result_path(tmp_path, rid), root=tmp_path, run_id=rid
        )
        is False
    )
    with pytest.raises(PermissionError, match="trusted|token|acceptance"):
        set_verified(tmp_path, rid)
    assert load_run(tmp_path, rid)["verified"] is False


def test_run_acceptance_true_then_set_verified(tmp_path):
    clear_cli_acceptance_tokens()
    run = create_run(tmp_path, mode="ralph", goal="ok")
    rid = run["run_id"]
    prd = _valid_prd(goal="ok", stories=[
        {"id": "s1", "title": "pass", "commands": [["true"]]}
    ])
    (tmp_path / ".omg" / "state" / "runs" / rid / "prd.json").write_text(
        json.dumps(prd) + "\n", encoding="utf-8"
    )

    manifest = freeze_acceptance(tmp_path, rid, prd)
    assert "sha256" in manifest
    assert manifest_path(tmp_path, rid).is_file()
    assert sha_path(tmp_path, rid).is_file()
    digest = sha_path(tmp_path, rid).read_text(encoding="utf-8").strip()
    assert len(digest) == 64

    ok = run_acceptance(tmp_path, rid)
    assert ok is True

    rpath = result_path(tmp_path, rid)
    assert rpath.is_file()
    result = json.loads(rpath.read_text(encoding="utf-8"))
    assert result["writer"] == CLI_WRITER
    assert result["passed"] is True
    assert result["manifest_sha256"] == digest
    assert result["results"][0]["returncode"] == 0
    assert result["results"][0]["command"] == ["true"]

    assert has_cli_acceptance_token(tmp_path, rid, digest) is True
    assert is_trusted_acceptance(tmp_path, rid) is True
    assert is_cli_acceptance_result(rpath, root=tmp_path, run_id=rid) is True
    verified = set_verified(tmp_path, rid)
    assert verified["verified"] is True
    assert verified["status"] == "verified"


def test_same_process_token_required_for_set_verified(tmp_path):
    """After real run_acceptance in this process, set_verified succeeds."""
    clear_cli_acceptance_tokens()
    run = create_run(tmp_path, mode="ralph", goal="token-ok")
    rid = run["run_id"]
    freeze_acceptance(tmp_path, rid, _valid_prd())
    assert run_acceptance(tmp_path, rid) is True
    assert is_trusted_acceptance(tmp_path, rid) is True
    set_verified(tmp_path, rid)
    assert load_run(tmp_path, rid)["verified"] is True


def test_run_acceptance_python_m_pytest_pass(tmp_path):
    """python -m pytest is the allowed python family form (not -c)."""
    run = create_run(tmp_path, mode="ulw", goal="py")
    rid = run["run_id"]
    # Write a tiny test file under project so pytest has something to collect
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_trivial.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    prd = {
        "version": 1,
        "goal": "py",
        "stories": [
            {
                "id": "s1",
                "title": "python m pytest",
                "commands": [[sys.executable, "-m", "pytest", "tests/test_trivial.py", "-q"]],
            }
        ],
        "global_commands": [],
    }
    freeze_acceptance(tmp_path, rid, prd)
    assert run_acceptance(tmp_path, rid) is True
    set_verified(tmp_path, rid)
    assert load_run(tmp_path, rid)["verified"] is True


def test_run_acceptance_python_c_denied(tmp_path):
    from omg_cli.acceptance import CommandPolicyError

    run = create_run(tmp_path, mode="ulw", goal="py-c")
    rid = run["run_id"]
    prd = {
        "version": 1,
        "goal": "py-c",
        "stories": [
            {
                "id": "s1",
                "title": "python -c denied",
                "commands": [[sys.executable, "-c", "pass"]],
            }
        ],
        "global_commands": [],
    }
    with pytest.raises(CommandPolicyError, match="-c"):
        freeze_acceptance(tmp_path, rid, prd)


def test_run_acceptance_false_not_verified(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="fail")
    rid = run["run_id"]
    prd = _valid_prd(
        goal="fail",
        stories=[{"id": "s1", "title": "fail", "commands": [["false"]]}],
    )
    freeze_acceptance(tmp_path, rid, prd)
    ok = run_acceptance(tmp_path, rid)
    assert ok is False

    result = json.loads(result_path(tmp_path, rid).read_text(encoding="utf-8"))
    assert result["writer"] == CLI_WRITER
    assert result["passed"] is False
    assert result["results"][0]["returncode"] != 0

    assert is_cli_acceptance_result(result_path(tmp_path, rid), root=tmp_path, run_id=rid) is False
    with pytest.raises(PermissionError):
        set_verified(tmp_path, rid)
    assert load_run(tmp_path, rid)["verified"] is False


def test_sanitized_env_strips_allow_external(monkeypatch):
    monkeypatch.setenv("OMG_ALLOW_EXTERNAL_CLI", "1")
    monkeypatch.setenv("OMG_ALLOW_FOO", "bar")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    env = sanitized_env()
    assert "OMG_ALLOW_EXTERNAL_CLI" not in env
    assert "OMG_ALLOW_FOO" not in env
    assert "PATH" in env


def test_freeze_and_run_helper(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="helper")
    rid = run["run_id"]
    prd = _valid_prd()
    ok = freeze_and_run(tmp_path, rid, prd)
    assert ok is True
    set_verified(tmp_path, rid)


def test_dry_run_acceptance_does_not_pass(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="dry")
    rid = run["run_id"]
    freeze_acceptance(tmp_path, rid, _valid_prd())
    ok = run_acceptance(tmp_path, rid, dry_run=True)
    assert ok is False
    result = json.loads(result_path(tmp_path, rid).read_text(encoding="utf-8"))
    assert result["passed"] is False
    assert result.get("dry_run") is True
    with pytest.raises(PermissionError):
        set_verified(tmp_path, rid)


def test_load_prd(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="load")
    rid = run["run_id"]
    assert load_prd(tmp_path, rid) is None
    prd = _valid_prd()
    p = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    p.write_text(json.dumps(prd), encoding="utf-8")
    loaded = load_prd(tmp_path, rid)
    assert loaded is not None
    assert loaded["goal"] == prd["goal"]


def test_allowlist_rejects_rm_and_claude():
    from omg_cli.acceptance import CommandAllowlistError, check_command_allowlist

    with pytest.raises(CommandAllowlistError, match="permanently denied|denied"):
        check_command_allowlist(["rm", "-rf", "/"])
    with pytest.raises(CommandAllowlistError, match="permanently denied|denied"):
        check_command_allowlist(["claude", "--version"])
    with pytest.raises(CommandAllowlistError, match="shell interpreter"):
        check_command_allowlist(["bash", "-c", "true"])
    with pytest.raises(CommandAllowlistError, match="not in acceptance allowlist"):
        check_command_allowlist(["curl", "https://example.com"])


def test_allowlist_allows_pytest_and_true():
    from omg_cli.acceptance import check_command_allowlist

    check_command_allowlist(["true"])
    check_command_allowlist(["false"])
    check_command_allowlist(["pytest", "tests/", "-q"])
    check_command_allowlist(["python3", "-m", "pytest", "-q"])
    check_command_allowlist(["python3.12", "-m", "unittest"])
    check_command_allowlist(["/usr/bin/pytest", "-q"])  # path basename
    with pytest.raises(Exception, match="-c"):
        check_command_allowlist(["python3", "-c", "pass"])


def test_allowlist_python_versioned_only_not_prefix():
    """python3.12 ok; python3evil / python3-config rejected (no startswith leak)."""
    from omg_cli.acceptance import (
        CommandAllowlistError,
        _basename_allowed,
        check_command_allowlist,
        resolve_allowlist,
    )

    allowed = resolve_allowlist()
    assert _basename_allowed("python3.12", allowed) is True
    assert _basename_allowed("python3", allowed) is True
    assert _basename_allowed("python", allowed) is True
    assert _basename_allowed("python2.7", allowed) is True
    assert _basename_allowed("python3evil", allowed) is False
    assert _basename_allowed("python3-config", allowed) is False
    assert _basename_allowed("python3foo", allowed) is False
    assert _basename_allowed("python3.", allowed) is False

    check_command_allowlist(["python3.12", "-m", "pytest"])
    with pytest.raises(CommandAllowlistError, match="not in acceptance allowlist"):
        check_command_allowlist(["python3evil", "-m", "pytest"])
    with pytest.raises(CommandAllowlistError, match="not in acceptance allowlist"):
        check_command_allowlist(["python3-config", "--help"])


def test_allowlist_extra_allow_and_no_allowlist():
    from omg_cli.acceptance import (
        CommandAllowlistError,
        check_command_allowlist,
        resolve_allowlist,
    )

    allowed = resolve_allowlist(["hello"])
    assert "hello" in allowed
    assert "pytest" in allowed
    check_command_allowlist(["hello", "world"], allowlist=allowed)
    # --no-allowlist still cannot run always-deny
    with pytest.raises(CommandAllowlistError, match="permanently denied"):
        check_command_allowlist(["claude"], no_allowlist=True)
    # but can run non-default bins like curl
    check_command_allowlist(["curl", "https://x"], no_allowlist=True)
    # floors still block python -c under break-glass
    with pytest.raises(CommandAllowlistError, match="-c"):
        check_command_allowlist(["python3", "-c", "pass"], no_allowlist=True)


def test_run_acceptance_rejects_denied_command(tmp_path):
    from omg_cli.acceptance import CommandAllowlistError

    run = create_run(tmp_path, mode="ralph", goal="deny")
    rid = run["run_id"]
    prd = {
        "version": 1,
        "goal": "deny",
        "stories": [
            {
                "id": "s1",
                "title": "bad",
                "commands": [["rm", "-rf", "x"]],
            }
        ],
        "global_commands": [],
    }
    # Policy runs at freeze time now
    with pytest.raises(CommandAllowlistError, match="rm"):
        freeze_acceptance(tmp_path, rid, prd)


def test_modes_ralph_require_acceptance_exit(monkeypatch, tmp_path):
    import subprocess

    from omg_cli.modes import run_mode
    from omg_cli.state import load_active_run

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen")),
    )
    rc = run_mode("ralph", "need accept", root=tmp_path, max_iter=1, dry_run=True)
    assert rc == 1
    active = load_active_run(tmp_path)
    assert active is not None
    assert active.get("verified") is False
    assert active["status"] == "completed"


def test_modes_ralph_with_passing_prd_verifies(monkeypatch, tmp_path):
    """After iter, freeze+run PRD with true → verified."""
    import subprocess
    from unittest.mock import MagicMock

    from omg_cli import modes as modes_mod
    from omg_cli.modes import run_mode
    from omg_cli.state import load_active_run

    mock_proc = MagicMock()
    mock_proc.pid = 55
    mock_proc.wait.return_value = 0
    real_popen = subprocess.Popen

    def selective_popen(argv, **kwargs):
        # Only mock grok launches; leave real Popen for acceptance subprocess.run
        if argv and argv[0] == "grok":
            return mock_proc
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", selective_popen)

    original_launch = modes_mod._launch_grok

    def launch_and_fill_prd(argv, *, cwd, run_dir, timeout, dry_run):
        rid = run_dir.name
        prd_path = Path(cwd) / ".omg" / "state" / "runs" / rid / "prd.json"
        prd_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "goal": "with prd",
                    "stories": [
                        {
                            "id": "s1",
                            "title": "ok",
                            "commands": [["true"]],
                        }
                    ],
                    "global_commands": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return original_launch(
            argv, cwd=cwd, run_dir=run_dir, timeout=timeout, dry_run=dry_run
        )

    monkeypatch.setattr(modes_mod, "_launch_grok", launch_and_fill_prd)

    rc = run_mode("ralph", "with prd", root=tmp_path, max_iter=2, dry_run=False)
    assert rc == 0
    active = load_active_run(tmp_path)
    assert active is not None
    assert active.get("verified") is True
    assert active.get("status") == "verified"
    assert result_path(tmp_path, active["run_id"]).is_file()
