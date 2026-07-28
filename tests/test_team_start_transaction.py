"""#17 all-or-nothing team start: failed live start clears active + debris."""

from __future__ import annotations

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

def test_partial_prepare_failure_rolls_back_created_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If prepare_task fails mid-batch, earlier new worktrees are rolled back."""
    from omg_cli.workers import WorkerError, prepare_task as real_prepare, worktree_dir

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: True)

    calls: list[str] = []

    def flaky_prepare(root, run_id, task_id):  # type: ignore[no-untyped-def]
        calls.append(str(task_id))
        if str(task_id) == "t2":
            raise WorkerError("forced mid-prepare failure")
        return real_prepare(root, run_id, task_id)

    monkeypatch.setattr(plane, "prepare_task", flaky_prepare)

    with pytest.raises(TeamError, match="transaction failed|mid-prepare"):
        start_team(
            "partial prep",
            TASKS,
            root=tmp_path,
            dry_run=False,
            topology="windows",
        )

    assert load_active_run(tmp_path) is None
    assert "t1" in calls and "t2" in calls
    runs = list((tmp_path / ".omg" / "state" / "runs").glob("*"))
    for run_dir in runs:
        rid = run_dir.name
        for tid in ("t1", "t2"):
            wt = worktree_dir(tmp_path, rid, tid)
            assert not wt.exists() or not any(wt.iterdir()), f"orphan worktree {wt}"


def test_post_prep_setup_failure_rolls_back_new_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Failures after prepare (argv write) must still hit #17 rollback."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: True)

    real_write = Path.write_text

    def boom_write(self, *a, **k):  # type: ignore[no-untyped-def]
        if str(self).endswith(".argv.json"):
            raise OSError("forced argv write failure")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom_write)

    with pytest.raises(TeamError, match="transaction failed|argv write"):
        start_team(
            "post prep",
            TASKS,
            root=tmp_path,
            dry_run=True,
            topology="windows",
        )

    assert load_active_run(tmp_path) is None


def test_seed_failure_surfaces_incomplete_compensating_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """runtime launch must not ignore stop_completed=False after seed failure."""
    from omg_cli.team import runtime

    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    monkeypatch.setattr(
        runtime,
        "start_team",
        lambda *a, **k: {
            "run_id": "run-seed-1",
            "schema_version": 1,
            "tasks": [{"task_id": "t1"}],
        },
    )
    monkeypatch.setattr(runtime, "write_team_ref", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_ensure_lane_dirs", lambda *a, **k: None)
    monkeypatch.setattr(
        runtime,
        "decompose_goal",
        lambda goal, workers, role: [
            {"task_id": "t1", "title": "one", "owned_files": ["README.md"], "role": role}
        ],
    )

    def boom_seed(*_a, **_k):
        raise RuntimeError("board seed boom")

    monkeypatch.setattr(runtime, "_seed_api_board", boom_seed)

    def incomplete_stop(*_a, **_k):
        return {
            "stop_completed": False,
            "errors": ["identity mismatch: skipped tmux kill-session"],
        }

    monkeypatch.setattr(plane, "stop_team", incomplete_stop)

    with pytest.raises(TeamError, match="compensating stop incomplete"):
        runtime.launch_team(
            "seed fail",
            workers=1,
            role="executor",
            root=tmp_path,
            dry_run=False,
        )


def test_launch_seed_failure_removes_team_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#17: board seed failure compensates stop and deletes team ref."""
    from omg_cli.team import runtime

    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    monkeypatch.setattr(
        runtime,
        "start_team",
        lambda *a, **k: {
            "run_id": "run-seed-ref",
            "schema_version": 1,
            "tasks": [{"task_id": "t1"}],
        },
    )
    refs: list[str] = []

    def track_ref(*_a, **k):
        refs.append(str(k.get("team_name") or "x"))
        return tmp_path / "ref.json"

    monkeypatch.setattr(runtime, "write_team_ref", track_ref)
    monkeypatch.setattr(runtime, "_ensure_lane_dirs", lambda *a, **k: None)
    monkeypatch.setattr(
        runtime,
        "decompose_goal",
        lambda goal, workers, role: [
            {"task_id": "t1", "title": "one", "owned_files": ["README.md"], "role": role}
        ],
    )
    monkeypatch.setattr(
        runtime,
        "_seed_api_board",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("seed boom")),
    )
    removed: list[str] = []
    monkeypatch.setattr(
        runtime,
        "remove_team_ref",
        lambda root, name: removed.append(str(name)),
    )
    monkeypatch.setattr(
        plane,
        "stop_team",
        lambda *a, **k: {"stop_completed": True, "errors": []},
    )

    with pytest.raises(TeamError, match="api_board_seed|seed boom"):
        runtime.launch_team(
            "seed fail ref",
            workers=1,
            role="executor",
            root=tmp_path,
            dry_run=False,
        )
    assert refs, "ref should have been written before seed"
    assert removed, "compensating ref remove must run"


def test_launch_annotation_failure_compensates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#17: startup annotation failure after seed still compensates."""
    from omg_cli.team import runtime

    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    monkeypatch.setattr(
        runtime,
        "start_team",
        lambda *a, **k: {
            "run_id": "run-ann-1",
            "schema_version": 1,
            "tasks": [{"task_id": "t1"}],
            "dry_run": False,
        },
    )
    monkeypatch.setattr(runtime, "write_team_ref", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_ensure_lane_dirs", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_seed_api_board", lambda *a, **k: None)
    monkeypatch.setattr(
        runtime,
        "decompose_goal",
        lambda goal, workers, role: [
            {"task_id": "t1", "title": "one", "owned_files": ["README.md"], "role": role}
        ],
    )
    monkeypatch.setattr(
        runtime,
        "startup_readiness_payload",
        lambda *a, **k: {
            "startup_status": "running",
            "startup_acks": 1,
            "startup_expected": 1,
            "startup_ack_workers": ["t1"],
            "startup_missing_workers": [],
            "ready_timeout_ms": 1,
            "startup_note": "ok",
        },
    )
    monkeypatch.setattr(
        runtime,
        "persist_startup_annotations",
        lambda *a, **k: (_ for _ in ()).throw(TeamError("annotation boom")),
    )
    stopped: list[str] = []
    monkeypatch.setattr(
        plane,
        "stop_team",
        lambda root, rid, **k: stopped.append(str(rid))
        or {"stop_completed": True, "errors": []},
    )
    removed: list[str] = []
    monkeypatch.setattr(
        runtime, "remove_team_ref", lambda *a, **k: removed.append("yes")
    )

    with pytest.raises(TeamError, match="startup_annotations|annotation boom"):
        runtime.launch_team(
            "ann fail",
            workers=1,
            role="executor",
            root=tmp_path,
            dry_run=False,
        )
    assert stopped == ["run-ann-1"]
    assert removed == ["yes"]
