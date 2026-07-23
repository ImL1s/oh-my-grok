"""Hermetic tests for team plane scaling + resume (D4).

Dry-run + FSM only — no live tmux/subprocess. Mirrors test_team_plane.py patterns.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omg_cli.evidence import CLI_WRITER
from omg_cli.fanout import max_workers_cap
from omg_cli.state import create_run, load_run
from omg_cli.team import plane, scaling
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    TeamError,
    TeamGateError,
    create_native_team,
    load_team_meta,
    prepare_native_spawn,
    start_team,
    team_meta_path,
)
from omg_cli.team.scaling import (
    STATUS_NEEDS_COLLECT,
    STATUS_RUNNING,
    STATUS_SCALED_DOWN,
    acquire_scale_lock,
    native_dispatch_plan,
    resume_team,
    scale_lock_path,
    scale_team,
)
from omg_cli.workers import worktree_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
PYTHON = sys.executable

TASKS_TWO = [
    {"task_id": "t-a", "owned_files": ["a.py"]},
    {"task_id": "t-b", "owned_files": ["b.py"]},
]
TASKS_THREE = [
    {"task_id": "t-a", "owned_files": ["a.py"]},
    {"task_id": "t-b", "owned_files": ["b.py"]},
    {"task_id": "t-c", "owned_files": ["c.py"]},
]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(".omg/\n", encoding="utf-8")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md", ".gitignore")
    _git(path, "commit", "-m", "initial")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _enable_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in plane.WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)


def _boom_subprocess(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("subprocess must not be called in dry_run scale")


def _boom_tmux(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("tmux_available must not be called in dry_run scale")


def _write_team_meta(root: Path, run_id: str, meta: dict[str, Any]) -> None:
    meta["writer"] = CLI_WRITER
    plane._atomic_write_json(team_meta_path(root, run_id), meta)


def _tasks_n(n: int) -> list[dict[str, Any]]:
    return [{"task_id": f"t{i}", "owned_files": [f"f{i}.py"]} for i in range(n)]


# ---------------------------------------------------------------------------
# scale up — cap + monotonic indices + dry-run
# ---------------------------------------------------------------------------


def test_scale_up_within_cap_appends_monotonic_indices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _boom_subprocess)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    meta = start_team("scale up", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    out = scale_team(tmp_path, rid, add=2, dry_run=True)
    assert out["op"] == "add"
    assert out["added"] == 2
    assert out["window_indices"] == [2, 3]
    assert out["dry_run"] is True
    assert out["verified"] is False

    disk = load_team_meta(tmp_path, rid)
    indices = [int(t["window_index"]) for t in disk["tasks"]]
    assert indices == [0, 1, 2, 3]
    assert len(set(indices)) == 4
    assert disk["next_worker_index"] == 4
    for rec in out["tasks_added"]:
        assert rec["pid"] is None
        assert rec["pgid"] is None


def test_scale_up_beyond_cap_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    cap = max_workers_cap()
    assert cap == 8
    tasks = _tasks_n(cap - 1)
    meta = start_team("near cap", tasks, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    # Fill to cap
    scale_team(tmp_path, rid, add=1, dry_run=True)
    disk = load_team_meta(tmp_path, rid)
    assert disk["task_count"] == cap

    with pytest.raises(TeamGateError, match="exceeds hard cap"):
        scale_team(tmp_path, rid, add=1, dry_run=True)


def test_scale_up_never_reuses_window_index_after_scale_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    meta = start_team("monotonic", TASKS_THREE, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    scale_team(tmp_path, rid, add=1, dry_run=True)
    # indices 0,1,2,3 — remove highest idle (3)
    scale_team(tmp_path, rid, remove=1, dry_run=True)

    out = scale_team(tmp_path, rid, add=1, dry_run=True)
    assert out["window_indices"] == [4]
    disk = load_team_meta(tmp_path, rid)
    all_indices = [int(t["window_index"]) for t in disk["tasks"]]
    assert 4 in all_indices
    assert 3 in all_indices  # scaled_down record preserved
    scaled = [t for t in disk["tasks"] if t.get("status") == STATUS_SCALED_DOWN]
    assert any(int(t["window_index"]) == 3 for t in scaled)


# ---------------------------------------------------------------------------
# scale down — recorded kills only, worktrees preserved, min 1 active
# ---------------------------------------------------------------------------


def test_scale_down_kills_only_recorded_targets_preserves_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    meta = start_team("scale down", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-scale-test"
    live["tasks"] = [
        {
            **live["tasks"][0],
            "pid": 11111,
            "pgid": 424242,
            "pane_id": "%11",
            "pid_start": "start-11111",
            "status": STATUS_RUNNING,
            "window_index": 0,
        },
        {
            **live["tasks"][1],
            "pid": 22222,
            "pgid": 424243,
            "pane_id": "%22",
            "pid_start": "start-22222",
            "status": STATUS_RUNNING,
            "window_index": 1,
        },
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$77",
        launch_nonce="b" * 32,
        tasks=live["tasks"],
    )
    live["launch_nonce"] = "b" * 32
    live["launch_receipt_sha256"] = receipt_hash
    live["identity_generation"] = 0
    live["identity_receipt_sha256"] = receipt_hash
    _write_team_meta(tmp_path, rid, live)
    worktrees_before = [Path(t["worktree"]) for t in live["tasks"] if t.get("worktree")]

    killpg_calls: list[tuple[int, int]] = []
    tmux_cmds: list[list[str]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))
        raise ProcessLookupError("gone")

    def fake_tmux_run(args: Any, **kw: Any) -> MagicMock:
        tmux_cmds.append(list(args))
        m = MagicMock()
        m.returncode = 0
        return m

    def guard_run(cmd: Any, *a: Any, **k: Any) -> Any:
        joined = " ".join(
            str(x) for x in (cmd if isinstance(cmd, (list, tuple)) else [cmd])
        )
        if "pkill" in joined or "pgrep" in joined:
            raise AssertionError(f"forbidden broad kill: {joined}")
        raise AssertionError(f"unexpected subprocess.run: {joined}")

    monkeypatch.setattr(scaling.os, "killpg", fake_killpg)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$77"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "b" * 32)
    monkeypatch.setattr(
        scaling,
        "_list_pane_identities",
        lambda _session: {0: ("%11", 11111), 1: ("%22", 22222)},
    )
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda pid: f"start-{pid}")
    monkeypatch.setattr(
        scaling,
        "_pgid_for_pid",
        lambda pid: {11111: 424242, 22222: 424243}[pid],
    )
    monkeypatch.setattr(subprocess, "run", guard_run)
    monkeypatch.setattr(plane.subprocess, "run", guard_run)

    out = scale_team(tmp_path, rid, remove=1, dry_run=False)
    assert out["op"] == "remove"
    assert out["removed"] == 1
    assert killpg_calls
    assert all(pg in (424242, 424243) for pg, _ in killpg_calls)
    assert any(
        c[:2] == ["kill-window", "-t"] and c[2].endswith(":1") for c in tmux_cmds
    )
    for wt in worktrees_before:
        assert wt.is_dir()
    disk = load_team_meta(tmp_path, rid)
    assert disk["task_count"] == 1
    assert any(t.get("status") == STATUS_SCALED_DOWN for t in disk["tasks"])


def test_scale_down_refuses_last_active_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    one = [{"task_id": "solo", "owned_files": ["solo.py"]}]
    meta = start_team("min one", one, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    with pytest.raises(TeamError, match="never remove below 1"):
        scale_team(tmp_path, rid, remove=1, dry_run=True)

    meta2 = start_team("two", TASKS_TWO, root=tmp_path, dry_run=True, force=True)
    rid2 = meta2["run_id"]
    with pytest.raises(TeamError, match="minimum is 1"):
        scale_team(tmp_path, rid2, remove=2, dry_run=True)


def test_scale_down_fails_closed_when_pid_pgid_drifts_before_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("pgid drift", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-scale-drift"
    live["tasks"] = [
        {
            **task,
            "pane_id": f"%{index + 31}",
            "pid": 31000 + index,
            "pgid": 41000 + index,
            "pid_start": f"start-{31000 + index}",
            "status": STATUS_RUNNING,
        }
        for index, task in enumerate(live["tasks"])
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$31",
        launch_nonce="c" * 32,
        tasks=live["tasks"],
    )
    live.update(
        {
            "launch_nonce": "c" * 32,
            "launch_receipt_sha256": receipt_hash,
            "identity_generation": 0,
            "identity_receipt_sha256": receipt_hash,
        }
    )
    _write_team_meta(tmp_path, rid, live)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$31"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "c" * 32)
    monkeypatch.setattr(
        scaling,
        "_list_pane_identities",
        lambda _session: {0: ("%31", 31000), 1: ("%32", 31001)},
    )
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda pid: f"start-{pid}")
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        scaling.os, "killpg", lambda pgid, sig: signals.append((pgid, int(sig)))
    )

    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    assert signals == []
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 0
    assert all(task["status"] == STATUS_RUNNING for task in disk["tasks"])


# ---------------------------------------------------------------------------
# scale lock
# ---------------------------------------------------------------------------


def test_scale_lock_refuses_concurrent_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)

    meta = start_team("lock", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    lock = scale_lock_path(tmp_path, rid)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("99999\n", encoding="utf-8")

    with pytest.raises(TeamError, match="scale lock held"):
        scale_team(tmp_path, rid, add=1, dry_run=True)


def test_acquire_scale_lock_exclusive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("lock ctx", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    with acquire_scale_lock(tmp_path, rid):
        assert scale_lock_path(tmp_path, rid).is_file()
        with pytest.raises(TeamError, match="scale lock held"):
            with acquire_scale_lock(tmp_path, rid):
                pass
    assert not scale_lock_path(tmp_path, rid).exists()


# ---------------------------------------------------------------------------
# resume — liveness reconciliation, idempotent, fail-closed
# ---------------------------------------------------------------------------


def test_resume_reconciles_liveness_from_tmux_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    meta = start_team("resume", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-resume-test"
    live["tasks"] = [
        {**live["tasks"][0], "status": STATUS_RUNNING, "window_index": 0},
        {**live["tasks"][1], "status": STATUS_RUNNING, "window_index": 1},
    ]
    _write_team_meta(tmp_path, rid, live)

    def fake_window_alive(_session: str, widx: int) -> bool | None:
        return widx == 0  # pane 0 alive, pane 1 dead

    monkeypatch.setattr(scaling, "_window_alive", fake_window_alive)

    out = resume_team(tmp_path, rid)
    assert out["changes"] == 1
    assert out["verified"] is False
    disk = load_team_meta(tmp_path, rid)
    by_id = {t["task_id"]: t for t in disk["tasks"]}
    assert by_id["t-a"]["status"] == STATUS_RUNNING
    assert by_id["t-b"]["status"] == STATUS_NEEDS_COLLECT


def test_resume_idempotent_second_run_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    meta = start_team("idem", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-idem"
    live["tasks"] = [
        {**live["tasks"][0], "status": STATUS_RUNNING, "window_index": 0},
        {**live["tasks"][1], "status": STATUS_RUNNING, "window_index": 1},
    ]
    _write_team_meta(tmp_path, rid, live)
    monkeypatch.setattr(
        scaling, "_window_alive", lambda _s, w: True if w == 0 else False
    )

    first = resume_team(tmp_path, rid)
    second = resume_team(tmp_path, rid)
    assert first["changes"] == 1
    assert second["changes"] == 0
    assert (
        load_team_meta(tmp_path, rid)["tasks"] == load_team_meta(tmp_path, rid)["tasks"]
    )
    # reconciliations stable (all unchanged on second pass)
    assert all(r.get("unchanged") for r in second["reconciliations"])


def test_resume_fail_closed_non_team_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    run = create_run(tmp_path, mode="ulw", goal="not team")
    rid = run["run_id"]
    with pytest.raises(TeamError, match="team.json missing"):
        resume_team(tmp_path, rid)


def test_resume_fail_closed_missing_team_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    run = create_run(tmp_path, mode="ulw", goal="ghost")
    rid = run["run_id"]
    # Mark as team in status but no team.json on disk
    from omg_cli.state import write_status

    write_status(tmp_path, rid, "running", extra={"team": True})
    with pytest.raises(TeamError, match="team.json missing"):
        resume_team(tmp_path, rid)


def test_resume_does_not_trust_stale_running_without_live_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """team.json says running but tmux probe says dead → needs_collect."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)

    meta = start_team("stale", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-stale"
    live["tasks"] = [
        {**live["tasks"][0], "status": STATUS_RUNNING, "window_index": 0},
    ]
    _write_team_meta(tmp_path, rid, live)
    monkeypatch.setattr(scaling, "_window_alive", lambda *_a, **_k: False)

    resume_team(tmp_path, rid)
    disk = load_team_meta(tmp_path, rid)
    assert disk["tasks"][0]["status"] == STATUS_NEEDS_COLLECT


# ---------------------------------------------------------------------------
# CLI smoke (dry-run scale/resume)
# ---------------------------------------------------------------------------


def test_cli_team_scale_dry_run(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    env = os.environ.copy()
    env[EXPERIMENTAL_ENV] = "1"
    for k in plane.WORKER_ENV_MARKERS:
        env.pop(k, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    start = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "start",
            "--dry-run",
            "--goal",
            "cli scale",
            "--tasks-json",
            json.dumps(TASKS_TWO),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert start.returncode == 0, start.stderr + start.stdout
    rid = json.loads(start.stdout)["run_id"]

    scale = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "scale",
            "--add",
            "1",
            "--dry-run",
            "--run",
            rid,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert scale.returncode == 0, scale.stderr + scale.stdout
    payload = json.loads(scale.stdout)
    assert payload["op"] == "add"
    assert payload.get("verified") is not True
    wt = worktree_dir(tmp_path, rid, payload["task_ids"][0])
    assert wt.is_dir()
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is not True


def test_native_dispatch_plan_respects_capacity_without_process_launch(
    tmp_path: Path,
) -> None:
    create_native_team(
        tmp_path,
        run_id="run-scale-native",
        team_id="team-scale-native",
        leader_id="leader",
        parent_session_id="parent-session",
        base_sha="a" * 40,
        created_at="2026-07-22T00:00:00Z",
        tasks=[
            {"task_id": task_id, "role": "verifier", "prompt": task_id}
            for task_id in ("a", "b", "c")
        ],
    )
    first = native_dispatch_plan(
        tmp_path,
        run_id="run-scale-native",
        team_id="team-scale-native",
        max_concurrency=2,
    )
    assert [item["task_id"] for item in first["ready"]] == ["a", "b"]
    assert first["slots"] == 2
    prepare_native_spawn(
        tmp_path,
        run_id="run-scale-native",
        team_id="team-scale-native",
        task_id="a",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="task a",
        expires_at="2099-01-01T00:00:00Z",
    )
    second = native_dispatch_plan(
        tmp_path,
        run_id="run-scale-native",
        team_id="team-scale-native",
        max_concurrency=2,
    )
    assert second["active"] == 1
    assert [item["task_id"] for item in second["ready"]] == ["b"]
    assert second["blocked_by_capacity"] == 1
    with pytest.raises(TeamError, match="max_concurrency"):
        native_dispatch_plan(
            tmp_path,
            run_id="run-scale-native",
            team_id="team-scale-native",
            max_concurrency=max_workers_cap() + 1,
        )


# ---------------------------------------------------------------------------
# aborted scale intent receipts (orphan adoption)
# ---------------------------------------------------------------------------


def _prepare_scale_signal_team(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[str, dict[str, Any]]:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("orphan intent", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    live = dict(load_team_meta(tmp_path, rid))
    live["dry_run"] = False
    live["session"] = "omg-scale-orphan"
    live["tasks"] = [
        {
            **task,
            "pane_id": f"%{index + 31}",
            "pid": 31000 + index,
            "pgid": 41000 + index,
            "pid_start": f"start-{31000 + index}",
            "status": STATUS_RUNNING,
        }
        for index, task in enumerate(live["tasks"])
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session=live["session"],
        session_id="$31",
        launch_nonce="c" * 32,
        tasks=live["tasks"],
    )
    live.update(
        {
            "launch_nonce": "c" * 32,
            "launch_receipt_sha256": receipt_hash,
            "identity_generation": 0,
            "identity_receipt_sha256": receipt_hash,
        }
    )
    _write_team_meta(tmp_path, rid, live)
    monkeypatch.setattr(
        scaling,
        "_read_tmux_session_identity",
        lambda _session: (live["session"], "$31"),
    )
    monkeypatch.setattr(scaling, "_read_tmux_launch_nonce", lambda _session: "c" * 32)
    monkeypatch.setattr(
        scaling,
        "_list_pane_identities",
        lambda _session: {0: ("%31", 31000), 1: ("%32", 31001)},
    )
    monkeypatch.setattr(scaling, "_pid_start_identity", lambda pid: f"start-{pid}")
    return rid, live


def test_scale_down_retry_after_aborted_signal_adopts_orphan_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    monkeypatch.setattr(
        scaling.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(AssertionError)
    )

    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    orphan_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    assert orphan_path.is_file()
    orphan_bytes = orphan_path.read_bytes()
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 0

    monkeypatch.setattr(
        scaling, "_pgid_for_pid", lambda pid: {31000: 41000, 31001: 41001}[pid]
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        scaling.os, "killpg", lambda pgid, sig: killed.append((pgid, int(sig)))
    )
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    tmux_cmds: list[list[str]] = []

    def fake_tmux_run(args: Any, **kw: Any) -> Any:
        tmux_cmds.append(list(args))
        from unittest.mock import MagicMock

        m = MagicMock()
        m.returncode = 0
        return m

    monkeypatch.setattr(scaling, "_tmux_run", fake_tmux_run)

    out = scale_team(tmp_path, rid, remove=1)
    assert out["op"] == "remove"
    assert out["removed"] == 1
    assert killed and all(pg == 41001 for pg, _ in killed)

    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 1
    assert orphan_path.read_bytes() == orphan_bytes
    parsed = parse_canonical_json_bytes(orphan_bytes)
    assert disk["identity_receipt_sha256"] == sha256_hex(canonical_json_bytes(parsed))


def test_scale_down_tampered_orphan_receipt_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
    )

    rid, _live = _prepare_scale_signal_team(monkeypatch, tmp_path)
    reads = iter([41001, 99999])
    monkeypatch.setattr(scaling, "_pgid_for_pid", lambda _pid: next(reads))
    monkeypatch.setattr(
        scaling.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(AssertionError)
    )
    with pytest.raises(TeamError, match="PGID drift"):
        scale_team(tmp_path, rid, remove=1)

    orphan_path = plane.team_identity_receipt_path(tmp_path, rid, 1)
    tampered = parse_canonical_json_bytes(orphan_path.read_bytes())
    tampered["tasks_after"] = []
    orphan_path.chmod(0o600)
    orphan_path.write_bytes(canonical_json_bytes(tampered))
    orphan_path.chmod(0o400)

    monkeypatch.setattr(
        scaling, "_pgid_for_pid", lambda pid: {31000: 41000, 31001: 41001}[pid]
    )
    monkeypatch.setattr(scaling.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(scaling, "tmux_available", lambda: True)

    with pytest.raises(
        TeamError, match="immutable team identity generation already exists"
    ):
        scale_team(tmp_path, rid, remove=1)
    disk = load_team_meta(tmp_path, rid)
    assert disk["identity_generation"] == 0
