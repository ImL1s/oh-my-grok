"""Hermetic lifecycle coverage for shorthand team status / resume / stop."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from omg_cli.madmax import tmux_available
from omg_cli.team.api import execute_team_api
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    STATUS_TOP_KEYS,
    WORKER_ENV_MARKERS,
    load_team_meta,
    status_locked_view,
    stop_team,
)
from omg_cli.team.runtime import launch_team, resume_for_identity, status_for_identity

TEAM_ID = "team"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=False,
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


def _cleanup_session(session: str | None) -> None:
    if not session:
        return
    _tmux("kill-session", "-t", session)


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

    from omg_cli.team.plane import format_status_table

    table = format_status_table(st)
    assert "topology:" in table
    assert "mailbox:" in table
    assert "api_summary:" in table
    assert "worktrees:" in table


@pytest.mark.tmux
def test_resume_restarts_dead_fixture_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill one fixture pane; resume relaunches it at identity generation+1."""
    if not tmux_available() and shutil.which("tmux") is None:
        pytest.skip("tmux not available")

    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.setenv("OMG_TEAM_FIXTURE_HOLD_S", "60")
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "20000")
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    _init_repo(tmp_path)

    session: str | None = None
    run_id: str | None = None
    try:
        meta = launch_team(
            "1. lane one\n2. lane two",
            workers=2,
            role="executor",
            root=tmp_path,
            dry_run=False,
            check_binary=False,
            env={EXPERIMENTAL_ENV: "1"},
            team_id=TEAM_ID,
            executor="fixture",
            detach=True,
        )
        assert meta.get("startup_status") == "running"
        assert int(meta.get("identity_generation") or 0) == 0
        session = str(meta.get("session") or "")
        run_id = str(meta["run_id"])
        team_name = str(meta["team_name"])

        by_id = {t["task_id"]: t for t in meta["tasks"]}
        victim = by_id["w2"]
        pane_id = str(victim["pane_id"])
        old_pid = victim.get("pid")
        kill = _tmux("kill-pane", "-t", pane_id)
        assert kill.returncode in (0, 1), kill.stderr

        out = resume_for_identity(tmp_path, team_name, env={EXPERIMENTAL_ENV: "1"})
        assert out.get("verified") is False
        assert out.get("identity_generation") == 1
        relaunched = out.get("relaunched") or []
        assert any(r.get("task_id") == "w2" for r in relaunched)
        assert not any(r.get("task_id") == "w2" for r in (out.get("blocked") or []))

        disk = load_team_meta(tmp_path, run_id)
        assert disk["identity_generation"] == 1
        w2 = next(t for t in disk["tasks"] if t["task_id"] == "w2")
        assert w2["status"] == "running"
        assert w2["pane_id"] != pane_id
        assert w2.get("pid") not in (None, old_pid)
        # Surviving worker untouched.
        w1 = next(t for t in disk["tasks"] if t["task_id"] == "w1")
        assert w1["pane_id"] == by_id["w1"]["pane_id"]
        assert w1["status"] == "running"

        # API board tasks remain non-terminal (pending).
        code, envelope = execute_team_api(
            "list-tasks",
            {"run_id": run_id, "team_id": TEAM_ID},
            root=tmp_path,
            env={EXPERIMENTAL_ENV: "1"},
        )
        assert code == 0 and envelope.get("ok")
        tasks = (envelope.get("data") or {}).get("tasks") or []
        assert tasks
        assert all(t.get("status") not in {"completed", "failed"} for t in tasks)

        # New pane is live in the session.
        probe = _tmux(
            "display-message", "-p", "-t", str(w2["pane_id"]), "#{pane_id}"
        )
        assert probe.returncode == 0
        assert (probe.stdout or "").strip() == w2["pane_id"]
    finally:
        if run_id:
            try:
                stop_team(tmp_path, run_id)
            except Exception:
                pass
        _cleanup_session(session)
