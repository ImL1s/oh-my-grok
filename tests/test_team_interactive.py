"""#147 PR2: direct-exec interactive pane contract (hermetic)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omg_cli.team.interactive import (
    E_TEAM_IO_MODE_UNSUPPORTED,
    InteractiveTeamError,
    grok_interactive_argv,
    pane_command_for_exec_script,
    resolve_effective_io_mode,
    write_interactive_exec_script,
    write_worker_inbox,
)
from omg_cli.team.io_capability import (
    IO_MODE_INTERACTIVE_TTY,
    TTY_OWNER_PROVIDER,
    interactive_pane_io_defaults,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV, TeamError, start_team

_POSIX = pytest.mark.skipif(
    os.name != "posix", reason="managed-store confinement requires POSIX dir_fd"
)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)


def _git_init(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True
    )
    (path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "i"],
        cwd=path,
        check=True,
        capture_output=True,
    )


TASKS = [{"task_id": "t1", "title": "one", "owned_files": ["README.md"]}]


def test_grok_interactive_argv_has_no_prompt_file() -> None:
    argv = grok_interactive_argv(cwd="/tmp/wt", posture="read-write")
    assert argv[0] == "grok"
    assert "--cwd" in argv
    assert "--prompt-file" not in argv
    assert "bypassPermissions" in argv
    ro = grok_interactive_argv(cwd="/tmp/wt", posture="read-only")
    assert "--permission-mode" in ro and "plan" in ro


def test_auto_stays_headless_until_live_qualification() -> None:
    assert (
        resolve_effective_io_mode(
            requested="auto", worker_topology="pane", provider="grok"
        )
        == "headless"
    )


def test_interactive_fails_closed_for_job_and_unqualified() -> None:
    with pytest.raises(InteractiveTeamError, match=E_TEAM_IO_MODE_UNSUPPORTED):
        resolve_effective_io_mode(
            requested="interactive", worker_topology="job", provider="grok"
        )
    with pytest.raises(InteractiveTeamError, match=E_TEAM_IO_MODE_UNSUPPORTED):
        resolve_effective_io_mode(
            requested="interactive", worker_topology="pane", provider="codex"
        )


def test_exec_script_is_0700_and_has_no_prompt(tmp_path: Path) -> None:
    dest = tmp_path / "t1.interactive.sh"
    argv = grok_interactive_argv(cwd=tmp_path, posture="read-write")
    write_interactive_exec_script(dest=dest, argv=argv, worktree=tmp_path)
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "exec" in text and "grok" in text
    assert "--prompt-file" not in text
    assert "XAI_API_KEY" not in text
    mode = dest.stat().st_mode & 0o777
    assert mode in {0o700, 0o666, 0o644} or (mode & 0o100)
    cmd = pane_command_for_exec_script(dest)
    assert cmd.startswith("exec /bin/sh ")
    assert "supervisor" not in cmd


def test_inbox_is_not_a_secret_dump(tmp_path: Path) -> None:
    p = write_worker_inbox(dest=tmp_path / "inbox.txt", body="task_id=t1\ndo the work\n")
    assert "do the work" in p.read_text(encoding="utf-8")


def test_interactive_pane_io_defaults() -> None:
    cap = interactive_pane_io_defaults()
    assert cap.io_mode == IO_MODE_INTERACTIVE_TTY
    assert cap.provider_tty_owner == TTY_OWNER_PROVIDER
    assert cap.operator_input_supported is True
    assert cap.input_ready is False


@_POSIX
def test_dry_run_interactive_fixture_skips_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive fixture",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )
    tasks = meta["tasks"]
    assert len(tasks) == 1
    rec = tasks[0]
    assert rec["io_mode"] == "interactive_tty"
    assert rec["provider_tty_owner"] == "provider"
    assert rec["operator_input_supported"] is True
    assert rec["input_ready"] is False
    pane = rec["pane_command"]
    assert "supervisor" not in pane
    assert "interactive.sh" in pane or "exec /bin/sh" in pane
    assert "--prompt-file" not in json.dumps(rec["argv"])
    assert rec["provider"] == "fixture"
    assert any("interactive_tty.py" in str(x) for x in rec["argv"])
    assert rec.get("inbox_path")


@_POSIX
def test_dry_run_interactive_grok_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "interactive grok",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        io_mode="interactive",
        env={EXPERIMENTAL_ENV: "1"},
    )
    rec = meta["tasks"][0]
    assert rec["provider"] == "grok"
    assert rec["io_mode"] == "interactive_tty"
    assert "--prompt-file" not in rec["argv"]
    assert rec["argv"][0] == "grok"
    assert "supervisor" not in rec["pane_command"]


def test_interactive_job_topology_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    with pytest.raises(TeamError, match=E_TEAM_IO_MODE_UNSUPPORTED):
        start_team(
            "nope",
            TASKS,
            root=tmp_path,
            dry_run=True,
            check_binary=False,
            worker_topology="job",
            io_mode="interactive",
            env={EXPERIMENTAL_ENV: "1"},
        )


def test_interactive_codex_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    with pytest.raises(TeamError, match=E_TEAM_IO_MODE_UNSUPPORTED):
        start_team(
            "nope",
            TASKS,
            root=tmp_path,
            dry_run=True,
            check_binary=False,
            routing={"executor": {"provider": "codex"}},
            io_mode="interactive",
            env={EXPERIMENTAL_ENV: "1"},
        )


@_POSIX
def test_default_launch_stays_headless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable(monkeypatch)
    _git_init(tmp_path)
    meta = start_team(
        "default",
        TASKS,
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        executor="fixture",
        env={EXPERIMENTAL_ENV: "1"},
    )
    rec = meta["tasks"][0]
    assert rec["io_mode"] == "headless_stream"
    assert "supervisor" in rec["pane_command"]


def test_parser_accepts_io_mode() -> None:
    from omg_cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["team", "launch", "--workers", "1", "--goal", "x", "--io-mode", "interactive"]
    )
    assert args.io_mode == "interactive"
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["team", "launch", "--workers", "1", "--goal", "x", "--io-mode", "proxy"]
        )


def test_fixture_refuses_without_controlling_tty() -> None:
    import subprocess
    import sys

    script = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "providers"
        / "interactive_tty.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "OMG_TEAM_PROVIDER_HOLD_S": "1"},
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0
    assert "E_FIXTURE_NO_TTY" in combined or "E_FIXTURE_TIMEOUT" in combined
    assert "PROVIDER_ECHO:" not in combined
