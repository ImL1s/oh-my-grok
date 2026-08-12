"""#17 all-or-nothing team start: failed live start clears active + debris."""

from __future__ import annotations

import json
import os
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


def test_dry_run_reuse_run_compensate_preserves_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#17: dry-run --run failure must not clear a pre-existing active pointer."""
    from omg_cli.modes import create_run
    from omg_cli.state import load_active_run
    from omg_cli.team import runtime

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    run = create_run(tmp_path, mode="ulw", goal="pre", force=True)
    rid = str(run["run_id"])
    assert load_active_run(tmp_path) is not None

    monkeypatch.setattr(
        runtime,
        "start_team",
        lambda *a, **k: {
            "run_id": rid,
            "schema_version": 1,
            "tasks": [{"task_id": "t1"}],
            "dry_run": True,
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
    monkeypatch.setattr(
        runtime,
        "_seed_api_board",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("seed boom")),
    )

    with pytest.raises(TeamError, match="api_board_seed|seed boom"):
        runtime.launch_team(
            "reuse dry",
            workers=1,
            role="executor",
            root=tmp_path,
            run_id=rid,
            dry_run=True,
        )
    active = load_active_run(tmp_path)
    assert active is not None
    assert str(active.get("run_id")) == rid


# ---------------------------------------------------------------------------
# PR #156: --run reuse preserves published owner_token
# ---------------------------------------------------------------------------


def test_run_reuse_preserves_published_owner_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Materialize then relaunch with --run must not mint a conflicting token."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.plane import team_meta_path

    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    first = start_team(
        "materialize first",
        TASKS,
        root=tmp_path,
        dry_run=True,
        owner_token="published-owner-token-aaa",
    )
    rid = str(first["run_id"])
    assert first["owner_token"] == "published-owner-token-aaa"
    meta_path = team_meta_path(tmp_path, rid)
    disk = json.loads(meta_path.read_text(encoding="utf-8"))
    assert disk["owner_token"] == "published-owner-token-aaa"
    assert disk.get("writer") == CLI_WRITER

    # Relaunch / materialize-to-live path: omit caller token → reuse published.
    second = start_team(
        "reuse without token",
        TASKS,
        root=tmp_path,
        run_id=rid,
        dry_run=True,
    )
    assert second["owner_token"] == "published-owner-token-aaa"
    disk2 = json.loads(meta_path.read_text(encoding="utf-8"))
    assert disk2["owner_token"] == "published-owner-token-aaa"

    # Explicit matching token also ok.
    third = start_team(
        "reuse matching token",
        TASKS,
        root=tmp_path,
        run_id=rid,
        dry_run=True,
        owner_token="published-owner-token-aaa",
    )
    assert third["owner_token"] == "published-owner-token-aaa"


def test_run_reuse_conflicting_caller_token_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit conflicting owner_token must fail before pane/state mutation."""
    from omg_cli.team.plane import team_meta_path

    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    first = start_team(
        "seed",
        TASKS,
        root=tmp_path,
        dry_run=True,
        owner_token="published-token-bbb",
    )
    rid = str(first["run_id"])
    meta_path = team_meta_path(tmp_path, rid)
    prior = meta_path.read_bytes()

    # Boom if we ever reach tmux / materialize past token resolve.
    monkeypatch.setattr(
        plane,
        "materialize_supervisor_pane_command",
        lambda **_k: (_ for _ in ()).throw(AssertionError("must not materialize")),
    )
    monkeypatch.setattr(plane, "tmux_available", lambda: True)

    with pytest.raises(TeamError, match="E_TEAM_OWNER_TOKEN_CONFLICT"):
        start_team(
            "conflict",
            TASKS,
            root=tmp_path,
            run_id=rid,
            dry_run=False,
            owner_token="attacker-fresh-token",
            topology="windows",
        )

    # Unrelated published authority must remain intact.
    assert meta_path.read_bytes() == prior
    assert (
        json.loads(prior.decode("utf-8"))["owner_token"] == "published-token-bbb"
    )


def test_resolve_owner_token_for_start_unit(tmp_path: Path) -> None:
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.plane import (
        resolve_owner_token_for_start,
        team_meta_path,
        _atomic_write_json,
    )

    (tmp_path / ".omg" / "state" / "runs" / "r1" / "team").mkdir(parents=True)
    # No team.json → fresh or caller.
    t1 = resolve_owner_token_for_start(tmp_path, run_id=None, owner_token=None)
    assert len(t1) == 32
    assert (
        resolve_owner_token_for_start(tmp_path, run_id=None, owner_token="abc")
        == "abc"
    )
    assert (
        resolve_owner_token_for_start(tmp_path, run_id="r1", owner_token="xyz")
        == "xyz"
    )

    _atomic_write_json(
        team_meta_path(tmp_path, "r1"),
        {
            "writer": CLI_WRITER,
            "run_id": "r1",
            "team_id": "team",
            "owner_token": "published-zzz",
            "schema_version": 1,
        },
    )
    assert (
        resolve_owner_token_for_start(tmp_path, run_id="r1", owner_token=None)
        == "published-zzz"
    )
    assert (
        resolve_owner_token_for_start(
            tmp_path, run_id="r1", owner_token="published-zzz"
        )
        == "published-zzz"
    )
    with pytest.raises(TeamError, match="E_TEAM_OWNER_TOKEN_CONFLICT"):
        resolve_owner_token_for_start(
            tmp_path, run_id="r1", owner_token="wrong"
        )


# ---------------------------------------------------------------------------
# PR #156 F2: --run reuse rolls back descriptor + prepublish authority
# ---------------------------------------------------------------------------

_PROVIDERS_ALL = frozenset({"grok", "codex", "agy", "cursor", "gemini"})


def _team_dir(root: Path, run_id: str) -> Path:
    from omg_cli.team.plane import team_dir

    return team_dir(root, run_id)


def _auth_path(root: Path, run_id: str, worker_id: str) -> Path:
    from omg_cli.team.supervisor import supervisor_prepublish_path

    return supervisor_prepublish_path(root, run_id, worker_id)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _assert_no_uncommitted_authority(root: Path, run_id: str) -> None:
    auth_dir = _team_dir(root, run_id) / "supervisor-authority"
    if not auth_dir.exists():
        return
    leftover = [p for p in auth_dir.iterdir() if p.is_file() or p.is_symlink()]
    assert leftover == [], f"uncommitted authority left: {leftover}"


def _force_tmux_boom(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    def boom(*_a, **_k):
        raise plane.TeamError(message)

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_create_tmux_session", boom)
    monkeypatch.setattr(plane, "_tmux_run", MagicMock())


def test_new_start_failure_removes_descriptor_and_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """New live start that fails after materialize must not leave artifacts."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    _force_tmux_boom(monkeypatch, "forced tmux failure after materialize")

    with pytest.raises(TeamError, match="transaction failed|forced tmux"):
        start_team(
            "new fail",
            TASKS,
            root=tmp_path,
            dry_run=False,
            topology="windows",
        )

    assert load_active_run(tmp_path) is None
    runs = list((tmp_path / ".omg" / "state" / "runs").glob("*"))
    assert runs, "failed new start should leave run dir for forensics"
    for run_dir in runs:
        tdir = run_dir / "team"
        assert not tdir.exists(), f"new-start team dir survived rollback: {tdir}"

    retry = start_team("new retry", TASKS, root=tmp_path, dry_run=True)
    assert retry["dry_run"] is True
    assert retry["run_id"] != runs[0].name


def test_reuse_rollback_restores_descriptor_and_removes_new_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reuse overwrite of descriptor + new authority must roll back exactly."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    first = start_team(
        "seed reuse",
        TASKS,
        root=tmp_path,
        dry_run=True,
        topology="windows",
    )
    rid = str(first["run_id"])
    tdir = _team_dir(tmp_path, rid)
    desc = tdir / "t1.provider.json"
    argv = tdir / "t1.argv.json"
    prior_desc = desc.read_bytes()
    prior_argv = argv.read_bytes()
    os.chmod(desc, 0o640)
    os.chmod(argv, 0o640)
    assert _mode(desc) == 0o640
    assert not _auth_path(tmp_path, rid, "t1").exists()

    _force_tmux_boom(monkeypatch, "forced tmux failure on reuse")
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

    assert desc.is_file()
    assert desc.read_bytes() == prior_desc
    assert _mode(desc) == 0o640
    assert argv.read_bytes() == prior_argv
    assert _mode(argv) == 0o640
    assert not _auth_path(tmp_path, rid, "t1").is_file()
    _assert_no_uncommitted_authority(tmp_path, rid)

    # Exact retry: same --run dry_run must succeed on restored artifacts.
    retry = start_team(
        "reuse retry",
        TASKS,
        root=tmp_path,
        run_id=rid,
        dry_run=True,
        force=True,
    )
    assert retry["run_id"] == rid
    assert retry["dry_run"] is True


def test_reuse_rollback_restores_preexisting_authority_bytes_and_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-existing prepublish authority is restored byte-and-mode exact."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    first = start_team(
        "seed auth",
        TASKS,
        root=tmp_path,
        dry_run=True,
        topology="windows",
    )
    rid = str(first["run_id"])
    desc = _team_dir(tmp_path, rid) / "t1.provider.json"
    prior_desc = desc.read_bytes()
    os.chmod(desc, 0o640)

    auth = _auth_path(tmp_path, rid, "t1")
    auth.parent.mkdir(parents=True, exist_ok=True)
    prior_auth = b'{"kind":"prior-prepublish-marker","worker":"t1"}\n'
    auth.write_bytes(prior_auth)
    os.chmod(auth, 0o400)
    assert _mode(auth) == 0o400

    _force_tmux_boom(monkeypatch, "forced tmux failure after publish")
    with pytest.raises(TeamError, match="transaction failed|forced tmux"):
        start_team(
            "reuse overwrite auth",
            TASKS,
            root=tmp_path,
            run_id=rid,
            dry_run=False,
            topology="windows",
            force=True,
        )

    assert desc.read_bytes() == prior_desc
    assert _mode(desc) == 0o640
    assert auth.is_file()
    assert auth.read_bytes() == prior_auth
    assert _mode(auth) == 0o400


def test_reuse_rollback_unlinks_newly_created_descriptor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A descriptor created only by the failed reuse is removed."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    first = start_team(
        "seed missing desc",
        TASKS,
        root=tmp_path,
        dry_run=True,
        topology="windows",
    )
    rid = str(first["run_id"])
    tdir = _team_dir(tmp_path, rid)
    created = tdir / "t2.provider.json"
    keep = tdir / "t1.provider.json"
    prior_keep = keep.read_bytes()
    created.unlink()
    assert not created.exists()

    _force_tmux_boom(monkeypatch, "forced tmux failure new descriptor")
    with pytest.raises(TeamError, match="transaction failed|forced tmux"):
        start_team(
            "reuse creates desc",
            TASKS,
            root=tmp_path,
            run_id=rid,
            dry_run=False,
            topology="windows",
            force=True,
        )

    assert keep.read_bytes() == prior_keep
    assert not created.exists()
    assert not _auth_path(tmp_path, rid, "t2").is_file()


def test_reuse_rollback_multi_cli_and_fixture_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D3 multi-CLI and fixture reuse must apply the same rollback contract."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    first = start_team(
        "seed multi",
        TASKS,
        root=tmp_path,
        dry_run=True,
        routing={"executor": {"provider": "codex"}},
        available_providers=_PROVIDERS_ALL,
        check_binary=False,
    )
    rid = str(first["run_id"])
    desc = _team_dir(tmp_path, rid) / "t1.provider.json"
    prior_desc = desc.read_bytes()
    os.chmod(desc, 0o640)

    _force_tmux_boom(monkeypatch, "forced multi-cli reuse failure")
    with pytest.raises(TeamError, match="transaction failed|forced multi-cli"):
        start_team(
            "reuse multi",
            TASKS,
            root=tmp_path,
            run_id=rid,
            dry_run=False,
            routing={"executor": {"provider": "codex"}},
            available_providers=_PROVIDERS_ALL,
            check_binary=False,
            force=True,
        )
    assert desc.read_bytes() == prior_desc
    assert _mode(desc) == 0o640
    _assert_no_uncommitted_authority(tmp_path, rid)

    fixture_first = start_team(
        "seed fixture",
        TASKS,
        root=tmp_path,
        dry_run=True,
        executor="fixture",
        force=True,
    )
    fid = str(fixture_first["run_id"])
    fdesc = _team_dir(tmp_path, fid) / "t1.provider.json"
    prior_fdesc = fdesc.read_bytes()
    os.chmod(fdesc, 0o640)

    _force_tmux_boom(monkeypatch, "forced fixture reuse failure")
    with pytest.raises(TeamError, match="transaction failed|forced fixture"):
        start_team(
            "reuse fixture",
            TASKS,
            root=tmp_path,
            run_id=fid,
            dry_run=False,
            executor="fixture",
            force=True,
        )
    assert fdesc.read_bytes() == prior_fdesc
    assert _mode(fdesc) == 0o640
    _assert_no_uncommitted_authority(tmp_path, fid)


def test_reuse_rollback_exact_retry_after_second_forced_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two failed live reuses in a row still restore the original artifacts."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    first = start_team("seed twice", TASKS, root=tmp_path, dry_run=True)
    rid = str(first["run_id"])
    desc = _team_dir(tmp_path, rid) / "t1.provider.json"
    prior_desc = desc.read_bytes()
    os.chmod(desc, 0o640)
    auth = _auth_path(tmp_path, rid, "t1")
    auth.parent.mkdir(parents=True, exist_ok=True)
    prior_auth = b'{"kind":"stable-prepublish","n":2}\n'
    auth.write_bytes(prior_auth)
    os.chmod(auth, 0o400)

    _force_tmux_boom(monkeypatch, "forced first reuse fail")
    with pytest.raises(TeamError, match="transaction failed|forced first"):
        start_team(
            "fail 1",
            TASKS,
            root=tmp_path,
            run_id=rid,
            dry_run=False,
            force=True,
        )
    _force_tmux_boom(monkeypatch, "forced second reuse fail")
    with pytest.raises(TeamError, match="transaction failed|forced second"):
        start_team(
            "fail 2",
            TASKS,
            root=tmp_path,
            run_id=rid,
            dry_run=False,
            force=True,
        )

    assert desc.read_bytes() == prior_desc
    assert _mode(desc) == 0o640
    assert auth.read_bytes() == prior_auth
    assert _mode(auth) == 0o400

    retry = start_team(
        "after two fails",
        TASKS,
        root=tmp_path,
        run_id=rid,
        dry_run=True,
        force=True,
    )
    assert retry["run_id"] == rid
