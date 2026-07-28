"""#17 all-or-nothing team start: failed live start clears active + debris."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omg_cli.state import load_active_run
from omg_cli.team import plane
from omg_cli.team.plane import TeamError, start_team
from omg_cli.workers import ownership_manifest_path, worktree_dir


def _init_repo(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


def _enable_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")


TASKS: list[dict[str, Any]] = [
    {"task_id": "t1", "title": "one", "owned_files": ["README.md"]},
    {"task_id": "t2", "title": "two", "owned_files": ["docs/x.md"]},
]


def test_live_start_failure_clears_active_and_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    # Fail inside the live transaction after run/worktrees exist.
    def boom(*_a, **_k):
        raise plane.TeamError("forced tmux failure")

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_create_tmux_session", boom)
    monkeypatch.setattr(plane, "_tmux_run", MagicMock())

    with pytest.raises(TeamError, match="transaction failed|forced tmux"):
        start_team(
            "tx fail",
            TASKS,
            root=tmp_path,
            dry_run=False,
            topology="windows",
        )

    # Active pointer must not block a retry.
    assert load_active_run(tmp_path) is None

    # No ownership manifest left for a rolled-back run.
    # Discover any run dirs under .omg/state/runs
    runs = list((tmp_path / ".omg" / "state" / "runs").glob("*"))
    for run_dir in runs:
        rid = run_dir.name
        mpath = ownership_manifest_path(tmp_path, rid)
        assert not mpath.exists(), f"ownership left for {rid}"
        for tid in ("t1", "t2"):
            wt = worktree_dir(tmp_path, rid, tid)
            assert not wt.exists() or not any(wt.iterdir()), f"worktree debris {wt}"


def test_dry_run_still_succeeds_without_tmux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: False)
    meta = start_team("dry ok", TASKS, root=tmp_path, dry_run=True)
    assert meta["dry_run"] is True
    assert meta["run_id"]
    assert load_active_run(tmp_path) is not None
    assert (tmp_path / ".omg" / "state" / "runs" / meta["run_id"] / "team" / "team.json").is_file()


def test_existing_run_rollback_preserves_preexisting_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--run reuses a run: failed live start must not wipe prior worktrees."""
    from omg_cli.modes import create_run
    from omg_cli.workers import (
        build_ownership_manifest,
        prepare_owned_tasks,
        worktree_dir,
    )

    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    run = create_run(tmp_path, mode="ulw", goal="pre-existing", force=True)
    rid = str(run["run_id"])
    build_ownership_manifest(tmp_path, rid, TASKS)
    prepared = prepare_owned_tasks(tmp_path, rid)
    assert prepared
    marker = worktree_dir(tmp_path, rid, "t1") / "WORKER_MARKER.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("keep me\n", encoding="utf-8")
    prior_ownership = ownership_manifest_path(tmp_path, rid).read_bytes()

    def boom(*_a, **_k):
        raise plane.TeamError("forced tmux failure on reuse")

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_create_tmux_session", boom)
    monkeypatch.setattr(plane, "_tmux_run", MagicMock())

    with pytest.raises(TeamError, match="transaction failed|forced tmux"):
        start_team(
            "reuse fail",
            TASKS,
            root=tmp_path,
            run_id=rid,
            dry_run=False,
            topology="windows",
            force=True,
        )

    # Pre-existing worker content must survive.
    assert marker.is_file(), "pre-existing worktree wiped on --run rollback"
    assert marker.read_text(encoding="utf-8") == "keep me\n"
    # Ownership restored to pre-start bytes (or at least still present).
    assert ownership_manifest_path(tmp_path, rid).is_file()
    assert ownership_manifest_path(tmp_path, rid).read_bytes() == prior_ownership
    # Active pointer for a reused run is not cleared by created_run=False path
    # (run already existed); do not require load_active_run is None.