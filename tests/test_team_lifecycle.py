"""Hermetic lifecycle coverage for shorthand team status / resume / stop."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    STATUS_TOP_KEYS,
    WORKER_ENV_MARKERS,
    status_locked_view,
)
from omg_cli.team.runtime import launch_team, status_for_identity


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def test_status_includes_acks_and_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    _init_repo(tmp_path)

    meta = launch_team(
        "1. lane one\n2. lane two",
        workers=2,
        role="executor",
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        env={EXPERIMENTAL_ENV: "1"},
    )
    st = status_for_identity(tmp_path, meta["team_name"])
    assert st["topology"] == "split"
    assert st["launch_mode"] == "shorthand"
    assert "mailbox" in st or "api_summary" in st
    assert "mailbox" in st
    assert "api_summary" in st
    assert st["api_summary"] is not None
    assert st["api_summary"]["workerCount"] == 2
    assert "worktrees" in st
    assert len(st["worktrees"]) == 2
    for row in st["worktrees"]:
        assert row.get("task_id")
        assert row.get("worktree")

    # Locked --json view stays freeze-stable (no new keys leaked).
    locked = status_locked_view(st)
    assert set(locked.keys()) == set(STATUS_TOP_KEYS)
    assert "mailbox" not in locked
    assert "api_summary" not in locked
    assert "worktrees" not in locked
    assert "topology" not in locked
