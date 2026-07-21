"""Hermetic tests for experimental grok-only tmux team plane (D1).

No live tmux. dry_run must never call tmux_available / subprocess.
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
from omg_cli.fanout import HARD_CAP_WORKERS, max_workers_cap
from omg_cli.state import create_run, load_run
from omg_cli.team import plane
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    STATUS_TASK_KEYS,
    STATUS_TOP_KEYS,
    TEAM_WORKER_ENV,
    TeamError,
    TeamGateError,
    collect_team,
    experimental_enabled,
    in_spawned_worker_context,
    load_team_meta,
    start_team,
    status_locked_view,
    stop_team,
    team_meta_path,
    team_status,
)
from omg_cli.workers import ownership_manifest_path, worktree_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
PYTHON = sys.executable

TASKS_TWO = [
    {"task_id": "t-a", "owned_files": ["a.py"]},
    {"task_id": "t-b", "owned_files": ["b.py"]},
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


def _boom_tmux(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("tmux_available must not be called in dry_run")


def _boom_subprocess(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("subprocess must not be called in dry_run")


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_experimental_gate_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    assert experimental_enabled() is False
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    assert experimental_enabled() is True
    monkeypatch.delenv(TEAM_WORKER_ENV, raising=False)
    assert in_spawned_worker_context() is False
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    assert in_spawned_worker_context() is True


def test_start_refuses_without_experimental_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    with pytest.raises(TeamGateError, match=EXPERIMENTAL_ENV):
        start_team("g", TASKS_TWO, root=tmp_path, dry_run=True)


def test_start_refuses_inside_spawned_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    with pytest.raises(TeamGateError, match="spawned-worker"):
        start_team("g", TASKS_TWO, root=tmp_path, dry_run=True)


def test_start_caps_at_hard_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    cap = max_workers_cap()
    assert cap <= HARD_CAP_WORKERS
    too_many = [
        {"task_id": f"t{i}", "owned_files": [f"f{i}.py"]}
        for i in range(cap + 1)
    ]
    with pytest.raises(TeamGateError, match="hard cap"):
        start_team("g", too_many, root=tmp_path, dry_run=True)


# ---------------------------------------------------------------------------
# dry-run start
# ---------------------------------------------------------------------------


def test_dry_run_writes_team_json_no_tmux_no_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(subprocess, "run", _boom_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _boom_subprocess)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    meta = start_team(
        "ship slices",
        TASKS_TWO,
        root=tmp_path,
        dry_run=True,
    )
    assert meta["writer"] == CLI_WRITER
    assert meta["dry_run"] is True
    assert meta["workspace_mode"] == "worktree"
    assert meta["session"]
    assert meta["session"].startswith("omg-")
    assert len(meta["tasks"]) == 2

    rid = meta["run_id"]
    path = team_meta_path(tmp_path, rid)
    assert path.is_file()
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["writer"] == CLI_WRITER
    assert disk["dry_run"] is True

    # ownership + real worktrees
    assert ownership_manifest_path(tmp_path, rid).is_file()
    for rec in meta["tasks"]:
        assert rec["pid"] is None
        assert rec["pgid"] is None
        assert rec["status"] == "dry_run"
        assert isinstance(rec["argv"], list)
        assert rec["argv"][0] == "grok"
        assert "--cwd" in rec["argv"]
        assert rec["pane_command"]
        assert "XAI_API_KEY" not in rec["pane_command"]
        assert "export " not in rec["pane_command"]
        wt = Path(rec["worktree"])
        assert wt.is_dir()
        assert wt == worktree_dir(tmp_path, rid, rec["task_id"])

    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is not True
    assert run.get("team") is True


def test_cli_team_start_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    env = os.environ.copy()
    env[EXPERIMENTAL_ENV] = "1"
    for k in plane.WORKER_ENV_MARKERS:
        env.pop(k, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    tasks = json.dumps(TASKS_TWO)
    r = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "start",
            "--dry-run",
            "--goal",
            "cli dry",
            "--tasks-json",
            tasks,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    payload = json.loads(r.stdout)
    assert payload["writer"] == CLI_WRITER
    assert payload["dry_run"] is True
    assert team_meta_path(tmp_path, payload["run_id"]).is_file()


def test_cli_team_start_refuses_without_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    env = os.environ.copy()
    env.pop(EXPERIMENTAL_ENV, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    r = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "start",
            "--dry-run",
            "--goal",
            "x",
            "--tasks-json",
            json.dumps(TASKS_TWO),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert EXPERIMENTAL_ENV in (r.stderr + r.stdout)


# ---------------------------------------------------------------------------
# status --json LOCKED keys
# ---------------------------------------------------------------------------


def test_status_json_locked_field_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    meta = start_team("st", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    # status may call tmux_available for liveness — allow False without boom
    monkeypatch.setattr(plane, "tmux_available", lambda: False)
    st = team_status(tmp_path, rid)
    locked = status_locked_view(st)
    assert set(locked.keys()) == set(STATUS_TOP_KEYS)
    assert locked["run_id"] == rid
    assert locked["session"] == meta["session"]
    assert locked["dry_run"] is True
    assert locked["workspace_mode"] == "worktree"
    assert len(locked["tasks"]) == 2
    for t in locked["tasks"]:
        assert set(t.keys()) == set(STATUS_TASK_KEYS)
        assert t["alive"] is False
        assert t["status"] == "dry_run"


# ---------------------------------------------------------------------------
# collect delegates; never verified
# ---------------------------------------------------------------------------


def test_collect_delegates_seal_and_integrate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: False)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    meta = start_team("collect me", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    seal_calls: list[tuple[Any, ...]] = []
    integrate_calls: list[tuple[Any, ...]] = []

    def fake_seal(root: Any, run_id: str, **kw: Any) -> list[dict[str, Any]]:
        seal_calls.append((root, run_id, kw))
        return [{"task_id": "t-a", "status": "skipped-no-worktree"}]

    def fake_integrate(root: Any, run_id: str, **kw: Any) -> dict[str, Any]:
        integrate_calls.append((root, run_id, kw))
        return {"status": "missing", "writer": CLI_WRITER, "run_id": run_id}

    monkeypatch.setattr(plane, "seal_all_tasks", fake_seal)
    # collect_team imports integrate_results inside the function
    import omg_cli.integrate as integrate_mod

    monkeypatch.setattr(integrate_mod, "integrate_results", fake_integrate)

    # Also patch the name used after import inside collect_team — rebind module
    monkeypatch.setattr(
        "omg_cli.integrate.integrate_results",
        fake_integrate,
    )

    result = collect_team(tmp_path, rid)
    assert seal_calls, "seal_all_tasks must be called"
    assert integrate_calls, "integrate_results must be called"
    assert seal_calls[0][1] == rid
    assert integrate_calls[0][1] == rid
    assert result["writer"] == CLI_WRITER
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is not True
    assert result.get("verified") is not True


def test_collect_rejects_forged_team_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="forge")
    rid = run["run_id"]
    path = team_meta_path(tmp_path, rid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "verified": True,
                "run_id": rid,
                "session": "evil",
                "dry_run": False,
                "tasks": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TeamError, match="CLI writer"):
        collect_team(tmp_path, rid)
    with pytest.raises(TeamError, match="CLI writer"):
        load_team_meta(tmp_path, rid)
    # forged verified is not honored anywhere in team plane
    run2 = load_run(tmp_path, rid)
    assert run2 is not None
    assert run2.get("verified") is not True


# ---------------------------------------------------------------------------
# stop: recorded session/pgids only; dry_run not signalled
# ---------------------------------------------------------------------------


def test_stop_uses_only_recorded_session_and_pgids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: False)

    meta = start_team("stop me", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    # Hand-edit to simulate a live record with pgids (not dry_run pids)
    live = dict(meta)
    live["dry_run"] = False
    live["session"] = "omg-test-session-xyz"
    live["tasks"] = [
        {
            **meta["tasks"][0],
            "pid": 424242,
            "pgid": 424242,
            "status": "running",
        },
        {
            **meta["tasks"][1],
            "pid": None,
            "pgid": None,
            "status": "dry_run",
        },
    ]
    live["writer"] = CLI_WRITER
    team_meta_path(tmp_path, rid).write_text(
        json.dumps(live, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    killpg_calls: list[tuple[int, int]] = []
    tmux_cmds: list[list[str]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))
        raise ProcessLookupError("gone")

    def fake_tmux_run(args: Any, **kw: Any) -> MagicMock:
        tmux_cmds.append(list(args))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(plane.os, "killpg", fake_killpg)
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)

    # Broad pkill must never be used — if anyone calls subprocess with pkill, fail
    def guard_run(cmd: Any, *a: Any, **k: Any) -> Any:
        joined = " ".join(str(x) for x in (cmd if isinstance(cmd, (list, tuple)) else [cmd]))
        if "pkill" in joined or "pgrep" in joined:
            raise AssertionError(f"forbidden broad kill: {joined}")
        raise AssertionError(f"unexpected subprocess.run: {joined}")

    monkeypatch.setattr(subprocess, "run", guard_run)
    monkeypatch.setattr(plane.subprocess, "run", guard_run)

    result = stop_team(tmp_path, rid)
    assert result["writer"] == CLI_WRITER
    # kill-session used recorded name only
    assert any(
        c[:2] == ["kill-session", "-t"] and c[2] == "omg-test-session-xyz"
        for c in tmux_cmds
    )
    # only the recorded pgid signalled; dry_run pid=None never signalled
    assert killpg_calls
    assert all(pg == 424242 for pg, _sig in killpg_calls)
    # actions must not invoke pkill; note text may mention the ban
    assert not any(
        a.strip().startswith("pkill") or " pkill " in f" {a} "
        for a in result.get("actions") or []
    )
    assert all(
        "killpg" in a or "tmux kill-session" in a or "tmux unavailable" in a
        or a.startswith("tmux")
        for a in result.get("actions") or []
        if "dry_run" not in a
    )


def test_stop_dry_run_entries_not_signalled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: False)

    meta = start_team("dry stop", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    killpg_calls: list[Any] = []
    monkeypatch.setattr(
        plane.os,
        "killpg",
        lambda *a, **k: killpg_calls.append(a) or (_ for _ in ()).throw(
            AssertionError("killpg on dry_run")
        ),
    )
    result = stop_team(tmp_path, rid)
    assert killpg_calls == []
    assert result["dry_run"] is True
    assert any("dry_run" in a for a in result["actions"])


# ---------------------------------------------------------------------------
# team.json CLI_WRITER stamp
# ---------------------------------------------------------------------------


def test_team_json_cli_writer_stamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: False)
    meta = start_team("stamp", TASKS_TWO, root=tmp_path, dry_run=True)
    disk = json.loads(team_meta_path(tmp_path, meta["run_id"]).read_text())
    assert disk["writer"] == CLI_WRITER
    assert disk.get("verified") is not True


def test_hand_written_verified_team_json_not_honored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hand-written team.json {verified:true} is not CLI-stamped → rejected."""
    _init_repo(tmp_path)
    run = create_run(tmp_path, mode="ulw", goal="v")
    rid = run["run_id"]
    path = team_meta_path(tmp_path, rid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"verified": True, "writer": "agent", "run_id": rid}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TeamError, match="CLI writer"):
        stop_team(tmp_path, rid)
    with pytest.raises(TeamError, match="CLI writer"):
        team_status(tmp_path, rid)
    # status.json verified untouched
    assert (load_run(tmp_path, rid) or {}).get("verified") is not True
