"""Hermetic tests for experimental tmux team plane (D1 + D3 multi-CLI).

No live tmux. dry_run must never call tmux_available / subprocess.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
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
    build_executor_pane_command,
    collect_team,
    create_native_team,
    experimental_enabled,
    in_spawned_worker_context,
    load_team_meta,
    load_native_team,
    native_team_status,
    prepare_native_spawn,
    reconcile_native_spawn,
    record_native_result,
    start_team,
    status_locked_view,
    stop_team,
    team_meta_path,
    team_status,
    transition_native_delivery,
)
from omg_cli.team.providers import (
    PROMPT_DELIVERY_POSITIONAL_TEXT,
    PROMPT_DELIVERY_PROMPT_FILE,
    PROMPT_DELIVERY_STDIN,
    build_executor_argv,
)
from omg_cli.team.roles import UnknownRoleError
from omg_cli.team.routing import RoutingError
from omg_cli.workers import ownership_manifest_path, worktree_dir

_PROVIDERS_ALL = frozenset({"grok", "codex", "agy", "cursor", "gemini"})

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
PYTHON = sys.executable

FAKE_TMUX_SERVER = {
    "tmux_socket_path": "/tmp/omg-test-tmux.sock",
    "tmux_server_pid": 424242,
    "tmux_server_pid_start": "ps:omg-test-server",
}

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


def test_create_tmux_session_injects_task_scoped_worker_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_tmux_run(args: Any, **_kwargs: Any) -> MagicMock:
        command = list(args)
        calls.append(command)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "new-session":
            result.stdout = "omg-workers\t$7\n"
        return result

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    common_identity = {
        plane.TEAM_RUN_ID_ENV: "run-1",
        plane.TEAM_ID_ENV: "team-1",
        plane.TEAM_LEADER_ROOT_ENV: str(tmp_path.resolve()),
        plane.TEAM_STATE_ROOT_ENV: str((tmp_path / ".omg" / "state").resolve()),
        plane.TEAM_OWNER_TOKEN_ENV: "owner-1",
    }
    tasks = []
    for task_id in ("task-a", "task-b"):
        task_env = [*common_identity.items(), (plane.TEAM_WORKER_ID_ENV, task_id)]
        tasks.append(
            {
                "task_id": task_id,
                "worktree": str(tmp_path / task_id),
                "pane_command": f"run-{task_id}",
                "_env_pairs": task_env,
            }
        )

    assert plane._create_tmux_session(
        session="omg-workers",
        tasks=tasks,
        env_pairs=[(plane.TEAM_RUN_ID_ENV, "run-1")],
    ) == ("omg-workers", "$7")

    create = next(command for command in calls if command[0] == "new-session")
    later = next(command for command in calls if command[0] == "new-window")
    assert f"{plane.TEAM_WORKER_ID_ENV}=task-a" in create
    assert f"{plane.TEAM_WORKER_ID_ENV}=task-b" not in create
    assert f"{plane.TEAM_WORKER_ID_ENV}=task-b" in later
    assert f"{plane.TEAM_WORKER_ID_ENV}=task-a" not in later
    for key, value in common_identity.items():
        assert f"{key}={value}" in create
        assert f"{key}={value}" in later


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_experimental_gate_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.team.plane import DISABLE_ENV

    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    monkeypatch.delenv(DISABLE_ENV, raising=False)
    assert experimental_enabled() is True  # default on
    monkeypatch.setenv(DISABLE_ENV, "1")
    assert experimental_enabled() is False
    monkeypatch.delenv(DISABLE_ENV, raising=False)
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    assert experimental_enabled() is True
    monkeypatch.delenv(TEAM_WORKER_ENV, raising=False)
    assert in_spawned_worker_context() is False
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    assert in_spawned_worker_context() is True


def test_start_refuses_when_kill_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.team.plane import DISABLE_ENV

    _init_repo(tmp_path)
    monkeypatch.setenv(DISABLE_ENV, "1")
    with pytest.raises(TeamGateError, match=DISABLE_ENV):
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
        {"task_id": f"t{i}", "owned_files": [f"f{i}.py"]} for i in range(cap + 1)
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
        # Process-ready wrap may pin PYTHONPATH; forbid secret exports only.
        assert "XAI_API_KEY=" not in rec["pane_command"]
        assert "worker-ready" in rec["pane_command"]
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


def test_cli_team_start_refuses_with_kill_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.team.plane import DISABLE_ENV

    _init_repo(tmp_path)
    env = os.environ.copy()
    env.pop(EXPERIMENTAL_ENV, None)
    env[DISABLE_ENV] = "1"
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
    assert DISABLE_ENV in (r.stderr + r.stdout)


def test_live_start_persists_nonce_bound_immutable_launch_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    nonce_seen: list[str] = []

    def fake_tmux_run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "set-option" and command[-2] == plane.LAUNCH_NONCE_OPTION:
            nonce_seen.append(command[-1])
        elif command[0] == "display-message":
            if "-t" in command:
                result.stdout = f"{command[command.index('-t') + 1]}\t$3\n"
            else:
                result.stdout = "/tmp/omg-test-tmux.sock\t424242\n"
        elif command[0] == "list-panes":
            result.stdout = "0\t%7\t424242\n1\t%8\t424243\n"
        return result

    monkeypatch.setattr(
        plane,
        "_create_tmux_session",
        lambda **kwargs: (str(kwargs["session"]), "$3"),
    )
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(plane.os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")

    meta = start_team("live receipt", TASKS_TWO, root=tmp_path)
    receipt_path = plane.team_launch_receipt_path(tmp_path, meta["run_id"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert nonce_seen == [meta["launch_nonce"]]
    assert receipt["launch_nonce"] == meta["launch_nonce"]
    assert receipt["session_id"] == "$3"
    assert receipt["tasks"][0] == {
        "task_id": "t-a",
        "window_index": 0,
        "pane_id": "%7",
        "pid": 424242,
        "pgid": 425242,
        "pid_start": "start-424242",
    }
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600


def test_live_split_start_uses_atomic_pane_snapshot_before_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Split launch must not commit from PID-only probes after nonce bind."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    nonce_holder: dict[str, str] = {}
    list_panes_calls: list[list[str]] = []

    def fake_create(**kwargs: Any) -> tuple[str, str]:
        tasks = kwargs["tasks"]
        tasks[0]["pane_id"] = "%10"
        tasks[1]["pane_id"] = "%11"
        tasks[0]["_tmux_launch"] = {
            "attach_mode": "detached",
            "session_owned": True,
            "leader_pane_id": None,
            "window_id": "@7",
            "attach_hint": "tmux attach -t sess",
        }
        return (str(kwargs["session"]), "$3")

    def fake_tmux_run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "set-option" and command[-2] == plane.LAUNCH_NONCE_OPTION:
            nonce_holder["nonce"] = command[-1]
        elif command[0] == "display-message":
            if "-t" in command:
                result.stdout = f"{command[command.index('-t') + 1]}\t$3\n"
            else:
                result.stdout = "/tmp/omg-test-tmux.sock\t424242\n"
        elif command[0] == "list-panes":
            list_panes_calls.append(command)
            # Prefer window-scoped list when window_id known.
            assert "-t" in command and command[command.index("-t") + 1] == "@7"
            assert "-s" not in command
            nonce = nonce_holder["nonce"]
            result.stdout = (
                f"%10\t424242\t$3\t@7\t{nonce}\n"
                f"%11\t424243\t$3\t@7\t{nonce}\n"
            )
        return result

    monkeypatch.setattr(
        "omg_cli.team.tmux.create_split_team_session", fake_create
    )
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(plane.os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")

    meta = start_team(
        "live split receipt",
        TASKS_TWO,
        root=tmp_path,
        topology="split",
    )
    assert list_panes_calls, "expected atomic list-panes snapshot before receipt"
    receipt = json.loads(
        plane.team_launch_receipt_path(tmp_path, meta["run_id"]).read_text(
            encoding="utf-8"
        )
    )
    assert receipt["session_id"] == "$3"
    assert receipt["tasks"][0]["pane_id"] == "%10"
    assert receipt["tasks"][0]["pid"] == 424242
    assert receipt["tasks"][1]["pid"] == 424243


def test_live_split_start_clears_intent_only_after_verified_team_meta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Commit point is receipt + team.json; WAL clear must not precede meta."""
    import omg_cli.team.tmux as tmux_mod
    from omg_cli.team.tmux import write_team_launch_intent

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    nonce_holder: dict[str, str] = {}
    order: list[str] = []
    intent_path: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> tuple[str, str]:
        tasks = kwargs["tasks"]
        root = Path(kwargs["root"])
        rid = str(kwargs["run_id"])
        intent = write_team_launch_intent(
            root,
            run_id=rid,
            session_id="$3",
            window_name="omg-team-abad1dea",
            nonce="abad1deaabad1deaabad1deaabad1dea",
            tmux_server=FAKE_TMUX_SERVER,
        )
        intent_path["path"] = intent
        intent_path["run_id"] = rid
        tasks[0]["pane_id"] = "%10"
        tasks[1]["pane_id"] = "%11"
        tasks[0]["_tmux_launch"] = {
            "attach_mode": "detached",
            "session_owned": True,
            "leader_pane_id": None,
            "window_id": "@7",
            "attach_hint": "tmux attach -t sess",
        }
        tasks[0]["_tmux_launch_intent"] = str(intent)
        return (str(kwargs["session"]), "$3")

    def fake_tmux_run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "set-option" and command[-2] == plane.LAUNCH_NONCE_OPTION:
            nonce_holder["nonce"] = command[-1]
        elif command[0] == "display-message":
            if "-t" in command:
                result.stdout = f"{command[command.index('-t') + 1]}\t$3\n"
            else:
                result.stdout = "/tmp/omg-test-tmux.sock\t424242\n"
        elif command[0] == "list-panes":
            nonce = nonce_holder["nonce"]
            result.stdout = (
                f"%10\t424242\t$3\t@7\t{nonce}\n"
                f"%11\t424243\t$3\t@7\t{nonce}\n"
            )
        return result

    real_atomic = plane._atomic_write_json
    real_clear = tmux_mod.clear_team_launch_intent

    def track_atomic(path: Path, data: Any) -> None:
        if Path(path).name == "team.json":
            order.append("meta")
            assert intent_path["path"].is_file()
        real_atomic(path, data)

    def track_clear(path: Path | str | None) -> None:
        order.append("clear")
        assert "meta" in order, "WAL clear must follow durable team.json"
        assert team_meta_path(tmp_path, str(intent_path["run_id"])).is_file()
        real_clear(path)

    monkeypatch.setattr(
        "omg_cli.team.tmux.create_split_team_session", fake_create
    )
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(plane.os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")
    monkeypatch.setattr(plane, "_atomic_write_json", track_atomic)
    monkeypatch.setattr(tmux_mod, "clear_team_launch_intent", track_clear)

    meta = start_team(
        "intent after meta",
        TASKS_TWO,
        root=tmp_path,
        topology="split",
    )
    assert order == ["meta", "clear"]
    assert not intent_path["path"].is_file()
    loaded = load_team_meta(tmp_path, meta["run_id"])
    assert loaded["launch_receipt_sha256"] == meta["launch_receipt_sha256"]


def test_live_split_start_writes_status_before_intent_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """write_status must precede WAL clear so status failure leaves the intent."""
    import omg_cli.team.tmux as tmux_mod
    from omg_cli.team.tmux import write_team_launch_intent

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    nonce_holder: dict[str, str] = {}
    order: list[str] = []
    intent_path: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> tuple[str, str]:
        tasks = kwargs["tasks"]
        root = Path(kwargs["root"])
        rid = str(kwargs["run_id"])
        intent = write_team_launch_intent(
            root,
            run_id=rid,
            session_id="$3",
            window_name="omg-team-abad1dea",
            nonce="abad1deaabad1deaabad1deaabad1dea",
            tmux_server=FAKE_TMUX_SERVER,
        )
        intent_path["path"] = intent
        intent_path["run_id"] = rid
        tasks[0]["pane_id"] = "%10"
        tasks[1]["pane_id"] = "%11"
        tasks[0]["_tmux_launch"] = {
            "attach_mode": "detached",
            "session_owned": True,
            "leader_pane_id": None,
            "window_id": "@7",
            "attach_hint": "tmux attach -t sess",
        }
        tasks[0]["_tmux_launch_intent"] = str(intent)
        return (str(kwargs["session"]), "$3")

    def fake_tmux_run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "set-option" and command[-2] == plane.LAUNCH_NONCE_OPTION:
            nonce_holder["nonce"] = command[-1]
        elif command[0] == "display-message":
            if "-t" in command:
                result.stdout = f"{command[command.index('-t') + 1]}\t$3\n"
            else:
                result.stdout = "/tmp/omg-test-tmux.sock\t424242\n"
        elif command[0] == "list-panes":
            nonce = nonce_holder["nonce"]
            result.stdout = (
                f"%10\t424242\t$3\t@7\t{nonce}\n"
                f"%11\t424243\t$3\t@7\t{nonce}\n"
            )
        return result

    real_write_status = plane.write_status
    real_clear = tmux_mod.clear_team_launch_intent

    def track_status(*args: Any, **kwargs: Any) -> Any:
        order.append("status")
        assert intent_path["path"].is_file(), "WAL must still exist during status"
        return real_write_status(*args, **kwargs)

    def track_clear(path: Path | str | None) -> None:
        order.append("clear")
        assert "status" in order, "WAL clear must follow write_status"
        real_clear(path)

    monkeypatch.setattr(
        "omg_cli.team.tmux.create_split_team_session", fake_create
    )
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(plane.os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")
    monkeypatch.setattr(plane, "write_status", track_status)
    monkeypatch.setattr(tmux_mod, "clear_team_launch_intent", track_clear)

    start_team(
        "status before clear",
        TASKS_TWO,
        root=tmp_path,
        topology="split",
    )
    assert order == ["status", "clear"]
    assert not intent_path["path"].is_file()


def test_live_start_status_failure_keeps_intent_and_rolls_back_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Status failure before WAL clear must leave intent and remove new authority."""
    import omg_cli.team.tmux as tmux_mod
    from omg_cli.team.tmux import write_team_launch_intent

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    nonce_holder: dict[str, str] = {}
    intent_path: dict[str, Any] = {}
    alive = False
    commands: list[list[str]] = []

    def fake_create(**kwargs: Any) -> tuple[str, str]:
        nonlocal alive
        alive = True
        tasks = kwargs["tasks"]
        root = Path(kwargs["root"])
        rid = str(kwargs["run_id"])
        intent = write_team_launch_intent(
            root,
            run_id=rid,
            session_id="$3",
            window_name="omg-team-abad1dea",
            nonce="abad1deaabad1deaabad1deaabad1dea",
            tmux_server=FAKE_TMUX_SERVER,
        )
        intent_path["path"] = intent
        intent_path["run_id"] = rid
        tasks[0]["pane_id"] = "%10"
        tasks[1]["pane_id"] = "%11"
        tasks[0]["_tmux_launch"] = {
            "attach_mode": "detached",
            "session_owned": True,
            "leader_pane_id": None,
            "window_id": "@7",
            "attach_hint": "tmux attach -t sess",
        }
        tasks[0]["_tmux_launch_intent"] = str(intent)
        return (str(kwargs["session"]), "$3")

    def fake_tmux_run(args: Any, **_kw: Any) -> MagicMock:
        nonlocal alive
        command = list(args)
        commands.append(command)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "set-option" and command[-2] == plane.LAUNCH_NONCE_OPTION:
            nonce_holder["nonce"] = command[-1]
        elif command[0] == "display-message":
            if "-t" in command:
                result.stdout = f"{command[command.index('-t') + 1]}\t$3\n"
            else:
                result.stdout = "/tmp/omg-test-tmux.sock\t424242\n"
        elif command[0] == "list-panes":
            nonce = nonce_holder["nonce"]
            result.stdout = (
                f"%10\t424242\t$3\t@7\t{nonce}\n"
                f"%11\t424243\t$3\t@7\t{nonce}\n"
            )
        elif command[0] == "kill-session":
            alive = False
        elif command[0] == "has-session":
            result.returncode = 0 if alive else 1
        return result

    def status_fail(*_a: Any, **_k: Any) -> None:
        raise OSError("injected status failure before WAL clear")

    monkeypatch.setattr(
        "omg_cli.team.tmux.create_split_team_session", fake_create
    )
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(plane.os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")
    monkeypatch.setattr(plane, "write_status", status_fail)

    with pytest.raises(TeamError, match="transaction"):
        start_team(
            "status fail keeps intent",
            TASKS_TWO,
            root=tmp_path,
            topology="split",
        )

    assert alive is False
    assert intent_path["path"].is_file(), "WAL must survive status failure"
    rid = str(intent_path["run_id"])
    assert not plane.team_launch_receipt_path(tmp_path, rid).exists()
    assert not team_meta_path(tmp_path, rid).exists()


def test_live_start_keeps_authority_when_cleanup_unproven_after_meta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unproven tmux cleanup must not unlink newly published receipt/team.json."""
    import omg_cli.team.tmux as tmux_mod
    from omg_cli.team.tmux import write_team_launch_intent

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    nonce_holder: dict[str, str] = {}
    intent_path: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> tuple[str, str]:
        tasks = kwargs["tasks"]
        root = Path(kwargs["root"])
        rid = str(kwargs["run_id"])
        intent = write_team_launch_intent(
            root,
            run_id=rid,
            session_id="$3",
            window_name="omg-team-abad1dea",
            nonce="abad1deaabad1deaabad1deaabad1dea",
            tmux_server=FAKE_TMUX_SERVER,
        )
        intent_path["path"] = intent
        intent_path["run_id"] = rid
        tasks[0]["pane_id"] = "%10"
        tasks[1]["pane_id"] = "%11"
        tasks[0]["_tmux_launch"] = {
            "attach_mode": "detached",
            "session_owned": True,
            "leader_pane_id": None,
            "window_id": "@7",
            "attach_hint": "tmux attach -t sess",
        }
        tasks[0]["_tmux_launch_intent"] = str(intent)
        return (str(kwargs["session"]), "$3")

    def fake_tmux_run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "set-option" and command[-2] == plane.LAUNCH_NONCE_OPTION:
            nonce_holder["nonce"] = command[-1]
        elif command[0] == "display-message":
            if "-t" in command:
                result.stdout = f"{command[command.index('-t') + 1]}\t$3\n"
            else:
                result.stdout = "/tmp/omg-test-tmux.sock\t424242\n"
        elif command[0] == "list-panes":
            nonce = nonce_holder["nonce"]
            result.stdout = (
                f"%10\t424242\t$3\t@7\t{nonce}\n"
                f"%11\t424243\t$3\t@7\t{nonce}\n"
            )
        elif command[0] == "kill-session":
            result.returncode = 0
        elif command[0] == "has-session":
            # Absence unproven — session still reports present.
            result.returncode = 0
        return result

    def status_fail(*_a: Any, **_k: Any) -> None:
        raise OSError("injected status failure with unproven cleanup")

    monkeypatch.setattr(
        "omg_cli.team.tmux.create_split_team_session", fake_create
    )
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(plane.os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")
    monkeypatch.setattr(plane, "write_status", status_fail)

    with pytest.raises(TeamError, match="cleanup unproven|authority retained"):
        start_team(
            "keep authority on unproven cleanup",
            TASKS_TWO,
            root=tmp_path,
            topology="split",
        )

    rid = str(intent_path["run_id"])
    assert intent_path["path"].is_file()
    assert plane.team_launch_receipt_path(tmp_path, rid).is_file()
    assert team_meta_path(tmp_path, rid).is_file()


def test_live_start_retains_authority_after_wal_cleared_postcommit_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After WAL clear, post-commit failure must not delete receipt/team.json."""
    import omg_cli.team.tmux as tmux_mod
    from omg_cli.team.tmux import write_team_launch_intent

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    nonce_holder: dict[str, str] = {}
    intent_path: dict[str, Any] = {}
    kill_calls: list[list[str]] = []

    def fake_create(**kwargs: Any) -> tuple[str, str]:
        tasks = kwargs["tasks"]
        root = Path(kwargs["root"])
        rid = str(kwargs["run_id"])
        intent = write_team_launch_intent(
            root,
            run_id=rid,
            session_id="$3",
            window_name="omg-team-abad1dea",
            nonce="abad1deaabad1deaabad1deaabad1dea",
            tmux_server=FAKE_TMUX_SERVER,
        )
        intent_path["path"] = intent
        intent_path["run_id"] = rid
        tasks[0]["pane_id"] = "%10"
        tasks[1]["pane_id"] = "%11"
        tasks[0]["_tmux_launch"] = {
            "attach_mode": "detached",
            "session_owned": True,
            "leader_pane_id": None,
            "window_id": "@7",
            "attach_hint": "tmux attach -t sess",
        }
        tasks[0]["_tmux_launch_intent"] = str(intent)
        return (str(kwargs["session"]), "$3")

    def fake_tmux_run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        if command[0] == "kill-session":
            kill_calls.append(command)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "set-option" and command[-2] == plane.LAUNCH_NONCE_OPTION:
            nonce_holder["nonce"] = command[-1]
        elif command[0] == "display-message":
            if "-t" in command:
                result.stdout = f"{command[command.index('-t') + 1]}\t$3\n"
            else:
                result.stdout = "/tmp/omg-test-tmux.sock\t424242\n"
        elif command[0] == "list-panes":
            nonce = nonce_holder["nonce"]
            result.stdout = (
                f"%10\t424242\t$3\t@7\t{nonce}\n"
                f"%11\t424243\t$3\t@7\t{nonce}\n"
            )
        return result

    real_clear = tmux_mod.clear_team_launch_intent

    def clear_then_boom(path: Path | str | None) -> None:
        real_clear(path)
        raise RuntimeError("injected post-WAL-clear failure")

    monkeypatch.setattr(
        "omg_cli.team.tmux.create_split_team_session", fake_create
    )
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(plane.os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")
    monkeypatch.setattr(tmux_mod, "clear_team_launch_intent", clear_then_boom)

    with pytest.raises(TeamError, match="durable authority retained|WAL cleared"):
        start_team(
            "retain after wal clear",
            TASKS_TWO,
            root=tmp_path,
            topology="split",
        )

    rid = str(intent_path["run_id"])
    assert not intent_path["path"].is_file()
    assert plane.team_launch_receipt_path(tmp_path, rid).is_file()
    assert team_meta_path(tmp_path, rid).is_file()
    assert kill_calls == [], "must not kill session after durable WAL clear"


def test_snapshot_launch_pane_identities_refuses_session_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    def fake_tmux(args: Any, **_kw: Any) -> SimpleNamespace:
        command = list(args)
        if command[0] == "list-panes":
            # Pane still addressable but now in a foreign session.
            return SimpleNamespace(
                returncode=0,
                stdout="%10\t424242\t$99\t@7\taaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(plane, "_tmux_run", fake_tmux)
    with pytest.raises(TeamError, match="missing worker pane"):
        plane._snapshot_launch_pane_identities(
            expected_session_id="$3",
            expected_pane_ids=["%10"],
            expected_window_id="@7",
            expected_launch_nonce="a" * 32,
        )


@pytest.mark.parametrize(
    "failure_point",
    ["mouse", "nonce", "identity", "receipt", "meta", "status"],
)
def test_live_start_transaction_cleans_exact_session_and_partial_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    run = create_run(tmp_path, mode="ulw", goal=f"transaction {failure_point}")
    run_id = str(run["run_id"])
    status_path = tmp_path / ".omg" / "state" / "runs" / run_id / "status.json"
    status_before = status_path.read_bytes()
    commands: list[list[str]] = []
    alive = False

    def fake_tmux_run(args: Any, **_kw: Any) -> MagicMock:
        nonlocal alive
        command = list(args)
        commands.append(command)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "new-session":
            alive = True
            session_name = command[command.index("-s") + 1]
            result.stdout = f"{session_name}\t$3\n"
        elif command[0] == "set-option" and command[-2:] == ["mouse", "on"]:
            if failure_point == "mouse":
                result.returncode = 1
        elif command[0] == "set-option" and plane.LAUNCH_NONCE_OPTION in command:
            if failure_point == "nonce":
                result.returncode = 1
        elif command[0] == "display-message":
            if failure_point == "identity":
                result.returncode = 1
            else:
                session_name = plane.session_name_for_cwd(tmp_path.resolve())
                result.stdout = f"{session_name}\t$3\n"
        elif command[0] == "list-panes":
            result.stdout = "0\t%7\t424242\n1\t%8\t424243\n"
        elif command[0] == "kill-session":
            assert command == ["kill-session", "-t", "$3"]
            alive = False
        elif command[0] == "has-session":
            assert command == ["has-session", "-t", "$3"]
            result.returncode = 0 if alive else 1
        return result

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux_run)
    monkeypatch.setattr(plane.os, "getpgid", lambda pid: pid + 1000)

    if failure_point == "receipt":
        real_persist = plane._persist_team_launch_receipt

        def persist_then_fail(*args: Any, **kwargs: Any) -> Any:
            real_persist(*args, **kwargs)
            raise OSError("injected receipt persistence failure")

        monkeypatch.setattr(plane, "_persist_team_launch_receipt", persist_then_fail)
    elif failure_point == "meta":
        real_atomic = plane._atomic_write_json

        def meta_then_fail(path: Path, data: Any) -> None:
            real_atomic(path, data)
            if path == team_meta_path(tmp_path, run_id):
                raise OSError("injected team metadata failure")

        monkeypatch.setattr(plane, "_atomic_write_json", meta_then_fail)
    elif failure_point == "status":
        real_write_status = plane.write_status

        def status_then_fail(*args: Any, **kwargs: Any) -> Any:
            real_write_status(*args, **kwargs)
            raise OSError("injected status failure")

        monkeypatch.setattr(plane, "write_status", status_then_fail)

    with pytest.raises(TeamError, match="transaction"):
        start_team(
            f"transaction {failure_point}",
            TASKS_TWO,
            root=tmp_path,
            run_id=run_id,
        )

    assert alive is False
    assert ["kill-session", "-t", "$3"] in commands
    assert ["has-session", "-t", "$3"] in commands
    assert not plane.team_launch_receipt_path(tmp_path, run_id).exists()
    assert not team_meta_path(tmp_path, run_id).exists()
    assert status_path.read_bytes() == status_before


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


def test_collect_refuses_pending_scale_wal_before_seal_or_integrate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.path_keys import atomic_write_bytes

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("pending collect", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    atomic_write_bytes(
        plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json",
        b'{"pending":true}',
        replace=False,
    )
    seal_calls = 0
    integrate_calls = 0

    def forbidden_seal(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal seal_calls
        seal_calls += 1
        return []

    def forbidden_integrate(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal integrate_calls
        integrate_calls += 1
        return {}

    monkeypatch.setattr(plane, "seal_all_tasks", forbidden_seal)
    monkeypatch.setattr("omg_cli.integrate.integrate_results", forbidden_integrate)

    with pytest.raises(TeamError, match="scale WAL is pending"):
        collect_team(tmp_path, rid)

    assert seal_calls == 0
    assert integrate_calls == 0
    assert not (tmp_path / ".omg" / "artifacts" / "ulw-results" / rid).exists()


# ---------------------------------------------------------------------------
# stop: recorded session/pgids only; dry_run not signalled
# ---------------------------------------------------------------------------


def _write_live_stop_identity(
    root: Path,
    meta: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str = "$9",
    nonce: str = "a" * 32,
    pid: int | None = None,
    pgid: int | None = None,
) -> dict[str, Any]:
    live = dict(meta)
    live["dry_run"] = False
    live["session"] = "omg-test-session-xyz"
    live["tasks"] = [
        {
            **task,
            "pane_id": f"%{index + 7}",
            "pid": pid if pid is not None else 424242 + index,
            "pgid": pgid if pgid is not None else 525252 + index,
            "pid_start": f"test-start-{pid if pid is not None else 424242 + index}",
            "status": "running",
        }
        for index, task in enumerate(meta["tasks"])
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        root,
        str(meta["run_id"]),
        session=live["session"],
        session_id=session_id,
        launch_nonce=nonce,
        tasks=live["tasks"],
    )
    live["launch_nonce"] = nonce
    live["launch_receipt_sha256"] = receipt_hash
    live["identity_generation"] = 0
    live["identity_receipt_sha256"] = receipt_hash
    starts = {task["pid"]: task["pid_start"] for task in live["tasks"]}

    def _start_for(pid: int) -> str | None:
        if pid in starts:
            return starts[pid]
        # Allow pane_id-stable rebind tests (exec-replaced pane_pid).
        return f"test-start-{pid}"

    monkeypatch.setattr(plane, "_pid_start_identity", _start_for)
    plane._atomic_write_json(team_meta_path(root, str(meta["run_id"])), live)
    return live


def _tmux_identity_runner(
    live: dict[str, Any],
    commands: list[list[str]],
    *,
    session_id: str = "$9",
    nonce: str | None = None,
    pane_pid_delta: int = 0,
) -> Any:
    expected_nonce = nonce if nonce is not None else str(live["launch_nonce"])
    session_killed = False

    def run(args: Any, **_kw: Any) -> MagicMock:
        nonlocal session_killed
        command = list(args)
        commands.append(command)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command[0] == "display-message":
            # Pane-targeted probe: display-message -p -t %N …
            if "-t" in command:
                try:
                    t_idx = command.index("-t")
                    target = command[t_idx + 1]
                except (ValueError, IndexError):
                    target = None
                task = next(
                    (t for t in live["tasks"] if t.get("pane_id") == target),
                    None,
                )
                if task is not None:
                    fmt = command[-1] if command else ""
                    live_pid = int(task["pid"]) + pane_pid_delta
                    if "#{session_id}" in str(fmt) or "#{window_id}" in str(fmt):
                        result.stdout = (
                            f"{task['pane_id']}\t{live_pid}\t{session_id}\t@1\n"
                        )
                    else:
                        result.stdout = f"{task['pane_id']}\t{live_pid}\n"
                else:
                    result.stdout = f"{live['session']}\t{session_id}\n"
            else:
                result.stdout = f"{live['session']}\t{session_id}\n"
        elif command[0] == "show-options":
            result.stdout = expected_nonce + "\n"
        elif command[0] == "list-panes":
            # Global absence probe uses ``list-panes -a -F #{pane_id}``.
            if "-a" in command and "#{pane_id}" in str(command[-1]):
                result.stdout = "".join(
                    f"{task['pane_id']}\n" for task in live["tasks"]
                )
            else:
                result.stdout = "".join(
                    f"{task['window_index']}\t{task['pane_id']}\t{task['pid'] + pane_pid_delta}\n"
                    for task in live["tasks"]
                )
        elif command[0] == "kill-session":
            session_killed = True
        elif command[0] == "has-session":
            result.returncode = 1 if session_killed else 0
        return result

    return run


@pytest.mark.parametrize("force", [False, True])
def test_stop_refuses_pending_scale_wal_before_shutdown_or_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, force: bool
) -> None:
    from omg_cli.contracts.path_keys import atomic_write_bytes

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("pending stop", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]
    _write_live_stop_identity(tmp_path, meta, monkeypatch)
    atomic_write_bytes(
        plane.team_dir(tmp_path, rid) / "scale-wal" / "1.json",
        b'{"pending":true}',
        replace=False,
    )
    monkeypatch.setattr(
        plane,
        "_tmux_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pending WAL must not touch tmux")
        ),
    )
    monkeypatch.setattr(
        plane.os,
        "killpg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pending WAL must not signal")
        ),
    )

    with pytest.raises(TeamError, match="scale WAL is pending"):
        stop_team(tmp_path, rid, force=force)

    assert not plane.team_shutdown_request_path(tmp_path, rid).exists()


def test_stop_uses_only_recorded_session_and_pgids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: False)

    meta = start_team("stop me", TASKS_TWO, root=tmp_path, dry_run=True)
    rid = meta["run_id"]

    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)

    killpg_calls: list[tuple[int, int]] = []
    tmux_cmds: list[list[str]] = []
    gone_pids: set[int] = set()

    def fake_killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            if any(t["pid"] in gone_pids for t in live["tasks"] if t["pgid"] == pgid):
                raise ProcessLookupError("group gone")
            return
        killpg_calls.append((pgid, sig))
        gone_pids.add(next(t["pid"] for t in live["tasks"] if t["pgid"] == pgid))

    def fake_getpgid(pid: int) -> int:
        if pid in gone_pids:
            raise ProcessLookupError("gone")
        return next(t["pgid"] for t in live["tasks"] if t["pid"] == pid)

    monkeypatch.setattr(plane.os, "killpg", fake_killpg)
    monkeypatch.setattr(plane.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", _tmux_identity_runner(live, tmux_cmds))

    # Broad pkill must never be used — if anyone calls subprocess with pkill, fail
    def guard_run(cmd: Any, *a: Any, **k: Any) -> Any:
        joined = " ".join(
            str(x) for x in (cmd if isinstance(cmd, (list, tuple)) else [cmd])
        )
        if "pkill" in joined or "pgrep" in joined:
            raise AssertionError(f"forbidden broad kill: {joined}")
        raise AssertionError(f"unexpected subprocess.run: {joined}")

    monkeypatch.setattr(subprocess, "run", guard_run)
    monkeypatch.setattr(plane.subprocess, "run", guard_run)

    result = stop_team(tmp_path, rid)
    assert result["writer"] == CLI_WRITER
    # kill-session used the immutable tmux session ID from the receipt.
    assert any(c[:2] == ["kill-session", "-t"] and c[2] == "$9" for c in tmux_cmds)
    # only the recorded pgid signalled; dry_run pid=None never signalled
    assert killpg_calls
    assert {pg for pg, _sig in killpg_calls} == {525252, 525253}
    assert result["identity_verified"] is True
    # actions must not invoke pkill; note text may mention the ban
    assert not any(
        a.strip().startswith("pkill") or " pkill " in f" {a} "
        for a in result.get("actions") or []
    )


def test_stop_signals_revalidated_pgids_before_killing_exact_tmux_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("signal before tmux kill", TASKS_TWO, root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    commands: list[list[str]] = []
    events: list[str] = []
    killed_session = False
    gone_pids: set[int] = set()
    base_runner = _tmux_identity_runner(live, commands)

    def tmux_runner(args: Any, **kwargs: Any) -> MagicMock:
        nonlocal killed_session
        command = list(args)
        result = base_runner(command, **kwargs)
        if command[0] == "kill-session":
            killed_session = True
            events.append("kill-session")
        return result

    def getpgid(pid: int) -> int:
        assert killed_session is False, (
            "PGID authority must not be read after tmux kill"
        )
        if pid in gone_pids:
            raise ProcessLookupError("gone")
        return next(t["pgid"] for t in live["tasks"] if t["pid"] == pid)

    def killpg(pgid: int, sig: int) -> None:
        assert killed_session is False, "must signal before destroying pane authority"
        if sig == 0:
            if any(t["pid"] in gone_pids for t in live["tasks"] if t["pgid"] == pgid):
                raise ProcessLookupError("group gone")
            return
        events.append(f"killpg:{pgid}")
        gone_pids.add(next(t["pid"] for t in live["tasks"] if t["pgid"] == pgid))

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", tmux_runner)
    monkeypatch.setattr(plane.os, "getpgid", getpgid)
    monkeypatch.setattr(plane.os, "killpg", killpg)

    result = stop_team(tmp_path, meta["run_id"])

    assert result["identity_verified"] is True
    assert events == ["killpg:525252", "killpg:525253", "kill-session"]
    assert ["kill-session", "-t", "$9"] in commands


def test_stop_refuses_pgid_rebound_without_relaunch_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same %pane with drifted pgid/pid must not become signal authority."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("signal-time rebind", [TASKS_TWO[0]], root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    commands: list[list[str]] = []
    pgid_reads = iter([999999, 999999, 999999, 999999])
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", _tmux_identity_runner(live, commands))
    monkeypatch.setattr(plane.os, "getpgid", lambda _pid: next(pgid_reads, 999999))

    def killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            raise ProcessLookupError("gone")
        signals.append((pgid, int(sig)))

    monkeypatch.setattr(plane.os, "killpg", killpg)

    result = stop_team(tmp_path, meta["run_id"])

    assert signals == []
    assert result["identity_verified"] is False
    assert any(
        "identity" in e and "refused" in e for e in (result.get("errors") or [])
    )


def test_stop_sigkill_escalation_keeps_exact_receipt_pgid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SIGKILL uses the receipted pgid; live pgid drift refuses escalation."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team(
        "sigkill-time rebind", [TASKS_TWO[0]], root=tmp_path, dry_run=True
    )
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    commands: list[list[str]] = []
    # First resolve (preflight + signal): exact pgid. Later getpgid drifts →
    # resolve_kill returns None; escalation may still use last known pgid when
    # leader is gone — keep group alive until SIGKILL for this path.
    pgid_reads = iter([525252, 525252, 525252, 525252, 525252, 525252, 525252])
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", _tmux_identity_runner(live, commands))
    monkeypatch.setattr(plane.os, "getpgid", lambda _pid: next(pgid_reads, 525252))

    def killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            if any(s == signal.SIGKILL for _, s in signals):
                raise ProcessLookupError("gone")
            return
        signals.append((pgid, int(sig)))

    monkeypatch.setattr(plane.os, "killpg", killpg)

    result = stop_team(tmp_path, meta["run_id"], kill_grace_s=0.001)

    assert (525252, int(signal.SIGTERM)) in signals
    assert any(sig == int(signal.SIGKILL) for _pgid, sig in signals)
    assert result["identity_verified"] is True

def test_stop_refuses_when_process_group_disappearance_remains_unproved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team(
        "stubborn process group", [TASKS_TWO[0]], root=tmp_path, dry_run=True
    )
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    commands: list[list[str]] = []
    signals: list[tuple[int, int]] = []

    def getpgid(_pid: int) -> int:
        # Keep exact receipt identity live so SIGKILL uses the safe
        # resolved_kill path (not PGID-only escalation).
        return 525252

    def killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            return
        signals.append((pgid, int(sig)))

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", _tmux_identity_runner(live, commands))
    monkeypatch.setattr(plane.os, "getpgid", getpgid)
    monkeypatch.setattr(plane.os, "killpg", killpg)
    monkeypatch.setattr(
        plane,
        "_wait_process_group_disappearance",
        lambda _pgid, **_kw: (False, "process group disappearance timed out pgid=525252"),
    )
    monkeypatch.setattr(
        plane,
        "_process_group_disappeared",
        lambda _pgid: (False, None),
    )

    result = stop_team(tmp_path, meta["run_id"])

    assert signals == [
        (525252, int(signal.SIGTERM)),
        (525252, int(signal.SIGKILL)),
    ]
    assert result["stop_completed"] is False
    assert result["identity_verified"] is True
    assert not any(command[0] == "kill-session" for command in commands)
    assert any("disappearance timed out" in error for error in result["errors"])
    durable = load_team_meta(tmp_path, meta["run_id"])
    assert durable["stop_state"] == "stop_refused"
    assert durable["tasks"][0]["status"] == "launch_unknown"
    run = load_run(tmp_path, meta["run_id"])
    assert run is not None
    assert run["status"] == "blocked"
    assert run["stage"] == "team_stop_refused"


def test_process_group_disappearance_retries_transient_permission_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = iter(
        [
            PermissionError(1, "Operation not permitted"),
            PermissionError(1, "Operation not permitted"),
            ProcessLookupError(3, "No such process"),
        ]
    )

    def killpg(_pgid: int, sig: int) -> None:
        assert sig == 0
        raise next(probes)

    monkeypatch.setattr(plane.os, "killpg", killpg)

    assert plane._wait_process_group_disappearance(525252, timeout_s=0.1) == (
        True,
        None,
    )


def test_process_group_disappearance_persistent_permission_denial_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def killpg(_pgid: int, sig: int) -> None:
        assert sig == 0
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(plane.os, "killpg", killpg)

    gone, error = plane._wait_process_group_disappearance(525252, timeout_s=0.0)

    assert gone is False
    assert error == "process group disappearance timed out pgid=525252"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_stop_refuses_pgid_only_sigkill_after_leader_and_pane_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When exact pane + leader are gone, refuse SIGKILL on numeric PGID alone."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team(
        "real group survivor refuse", [TASKS_TWO[0]], root=tmp_path, dry_run=True
    )
    child_pid_path = tmp_path / "same-pgid-child.pid"
    script = """
import os
import signal
import sys

child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        signal.pause()
else:
    ready_path = sys.argv[1]
    pending_path = ready_path + ".pending"
    with open(pending_path, "w", encoding="utf-8") as handle:
        handle.write(str(child))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending_path, ready_path)
    while True:
        signal.pause()
"""
    leader = subprocess.Popen(
        [sys.executable, "-c", script, str(child_pid_path)],
        start_new_session=True,
    )
    child_pid: int | None = None
    pgid = leader.pid
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not child_pid_path.is_file():
            time.sleep(0.01)
        assert child_pid_path.is_file(), "child process did not report readiness"
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        assert os.getpgid(leader.pid) == pgid
        assert os.getpgid(child_pid) == pgid

        live = _write_live_stop_identity(
            tmp_path,
            meta,
            monkeypatch,
            pid=leader.pid,
            pgid=pgid,
        )
        commands: list[list[str]] = []
        base_runner = _tmux_identity_runner(live, commands)
        pane_token = str(live["tasks"][0]["pane_id"])

        def runner(args: Any, **kw: Any) -> Any:
            # After SIGTERM reaps the receipt leader, prove the pane is absent
            # via list-panes -a (not display-message non-zero → "gone").
            if (
                leader.poll() is not None
                and list(args)[:1] == ["list-panes"]
                and "-a" in list(args)
            ):
                from unittest.mock import MagicMock

                return MagicMock(returncode=0, stdout="%999\n", stderr="")
            return base_runner(args, **kw)

        monkeypatch.setattr(plane, "tmux_available", lambda: True)
        monkeypatch.setattr(plane, "_tmux_run", runner)
        reaper = threading.Thread(target=leader.wait, daemon=True)
        reaper.start()
        result = stop_team(tmp_path, meta["run_id"], kill_grace_s=0.1)
        reaper.join(timeout=2.0)

        assert leader.poll() is not None
        # Child may still be alive — we intentionally refuse PGID-only SIGKILL.
        assert child_pid is not None
        os.kill(child_pid, 0)
        assert not any(
            action.startswith(f"killpg:SIGKILL pgid={pgid}")
            for action in result["actions"]
        )
        assert result["stop_completed"] is False
        assert any("refusing PGID-only kill" in e for e in (result.get("errors") or []))
        durable = load_team_meta(tmp_path, meta["run_id"])
        assert durable["stop_state"] == "stop_refused"
        _ = pane_token
    finally:
        if leader.poll() is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            leader.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            leader.kill()
            leader.wait(timeout=2.0)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stop_refuses_sigkill_when_pane_absence_probe_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """list-panes -a non-zero/OSError must not authorize pane-gone SIGKILL."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team(
        "pane probe unknown", [TASKS_TWO[0]], root=tmp_path, dry_run=True
    )
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    commands: list[list[str]] = []
    leader_gone = False
    signals: list[tuple[int, int]] = []

    def getpgid(_pid: int) -> int:
        if leader_gone:
            raise ProcessLookupError("leader reaped")
        return 525252

    def killpg(pgid: int, sig: int) -> None:
        nonlocal leader_gone
        if sig == 0:
            return
        signals.append((pgid, int(sig)))
        if sig == signal.SIGTERM:
            leader_gone = True

    base = _tmux_identity_runner(live, commands)

    def runner(args: Any, **kw: Any) -> Any:
        if list(args)[:1] == ["list-panes"] and "-a" in list(args):
            from unittest.mock import MagicMock

            return MagicMock(returncode=2, stdout="", stderr="server busy")
        return base(args, **kw)

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", runner)
    monkeypatch.setattr(plane.os, "getpgid", getpgid)
    monkeypatch.setattr(plane.os, "killpg", killpg)
    monkeypatch.setattr(
        plane,
        "_wait_process_group_disappearance",
        lambda _pgid, **_kw: (False, None),
    )
    monkeypatch.setattr(
        plane,
        "_process_group_disappeared",
        lambda _pgid: (False, None),
    )

    result = stop_team(tmp_path, meta["run_id"], kill_grace_s=0)

    assert signals == [(525252, int(signal.SIGTERM))]
    assert not any(sig == int(signal.SIGKILL) for _pgid, sig in signals)
    assert result["stop_completed"] is False
    assert any("pane probe unknown" in e for e in (result.get("errors") or []))


def test_stop_refuses_sigkill_when_leader_pgid_none_after_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolve succeeds then final leader PGID is None → never SIGKILL on cache."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team(
        "final leader pgid none", [TASKS_TWO[0]], root=tmp_path, dry_run=True
    )
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    commands: list[list[str]] = []
    signals: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            return
        signals.append((pgid, int(sig)))

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", _tmux_identity_runner(live, commands))
    monkeypatch.setattr(plane.os, "getpgid", lambda _pid: 525252)
    monkeypatch.setattr(plane.os, "killpg", killpg)
    # Final revalidation: leader already gone even though resolve still sees
    # the receipt-bound identity via pane probe / _pgid_for_pid.
    monkeypatch.setattr(plane, "_receipt_leader_pgid", lambda _pid: (None, None))
    monkeypatch.setattr(
        plane,
        "_wait_process_group_disappearance",
        lambda _pgid, **_kw: (False, None),
    )
    monkeypatch.setattr(
        plane,
        "_process_group_disappeared",
        lambda _pgid: (False, None),
    )

    result = stop_team(tmp_path, meta["run_id"], kill_grace_s=0)

    assert signals == [(525252, int(signal.SIGTERM))]
    assert not any(sig == int(signal.SIGKILL) for _pgid, sig in signals)
    assert result["stop_completed"] is False
    assert any(
        "leader gone after resolve" in e or "refusing PGID-only kill" in e
        for e in (result.get("errors") or [])
    )
    durable = load_team_meta(tmp_path, meta["run_id"])
    assert durable["stop_state"] == "stop_refused"


def test_pane_proven_absent_distinguishes_unknown_from_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    calls: list[list[str]] = []

    def fake_tmux(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        calls.append(command)
        if command == ["list-panes", "-a", "-F", "#{pane_id}"]:
            return MagicMock(returncode=0, stdout="%1\n%2\n", stderr="")
        return MagicMock(returncode=1, stdout="", stderr="no")

    monkeypatch.setattr(plane, "_tmux_run", fake_tmux)
    assert plane._pane_proven_absent("%3") == (True, None)
    assert plane._pane_proven_absent("%1") == (False, None)

    def boom(_args: Any, **_kw: Any) -> MagicMock:
        raise OSError("tmux gone")

    monkeypatch.setattr(plane, "_tmux_run", boom)
    absent, err = plane._pane_proven_absent("%3")
    assert absent is None
    assert err is not None and "OSError" in err

    monkeypatch.setattr(
        plane,
        "_tmux_run",
        lambda *_a, **_k: MagicMock(returncode=3, stdout="", stderr="err"),
    )
    absent, err = plane._pane_proven_absent("%3")
    assert absent is None
    assert err is not None and "exit=3" in err

    monkeypatch.setattr(
        plane,
        "_tmux_run",
        lambda *_a, **_k: MagicMock(returncode=0, stdout="not-a-pane\n", stderr=""),
    )
    absent, err = plane._pane_proven_absent("%3")
    assert absent is None
    assert err is not None and "malformed" in err


def test_stop_forged_writer_and_pgid_without_launch_receipt_never_signals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("forged", TASKS_TWO, root=tmp_path, dry_run=True)
    meta["dry_run"] = False
    meta["session"] = "omg-forged-session"
    meta["tasks"][0].update({"pid": 424242, "pgid": 525252})
    team_meta_path(tmp_path, meta["run_id"]).write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    tmux_commands: list[Any] = []
    signals: list[Any] = []
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", lambda args: tmux_commands.append(args))
    monkeypatch.setattr(plane.os, "killpg", lambda *args: signals.append(args))

    result = stop_team(tmp_path, meta["run_id"])

    assert result["identity_verified"] is False
    assert signals == []
    # has-session probe is allowed; kill-session / kill-pane must never run.
    assert all(cmd and cmd[0] == "has-session" for cmd in tmux_commands)
    assert any("launch receipt missing" in error for error in result["errors"])


def test_stop_refuses_pgid_drift_on_same_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Receipted pgid drift under the same pane_id refuses process signalling."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("pgid rebind", TASKS_TWO, root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    commands: list[list[str]] = []
    signals: list[Any] = []
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", _tmux_identity_runner(live, commands))
    monkeypatch.setattr(plane.os, "getpgid", lambda _pid: 999999)
    monkeypatch.setattr(
        plane.os,
        "killpg",
        lambda pgid, sig: (
            (_ for _ in ()).throw(ProcessLookupError("gone"))
            if sig == 0
            else signals.append((pgid, sig))
        ),
    )

    result = stop_team(tmp_path, meta["run_id"])

    assert result["identity_verified"] is False
    assert signals == []


@pytest.mark.parametrize(
    ("session_id", "pane_pid_delta", "nonce", "expect_ok"),
    [
        ("$77", 0, None, False),  # wrong session id
        ("$9", 1, None, False),  # pane pid replaced (respawn) → refuse
        ("$9", 0, "b" * 32, False),  # wrong launch nonce
        ("$9", 0, None, True),  # exact identity → OK
    ],
)
def test_stop_tmux_session_nonce_or_pane_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_id: str,
    pane_pid_delta: int,
    nonce: str | None,
    expect_ok: bool,
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("stale tmux", TASKS_TWO, root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    commands: list[list[str]] = []
    signals: list[Any] = []
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(
        plane,
        "_tmux_run",
        _tmux_identity_runner(
            live,
            commands,
            session_id=session_id,
            nonce=nonce,
            pane_pid_delta=pane_pid_delta,
        ),
    )

    def _getpgid(pid: int) -> int:
        for t in live["tasks"]:
            if t["pid"] == pid:
                return int(t["pgid"])
        # Rebound pane_pid (receipt_pid + delta) keeps original pgid for hermetic test.
        return int(live["tasks"][0]["pgid"])

    monkeypatch.setattr(plane.os, "getpgid", _getpgid)

    def killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            raise ProcessLookupError("gone")
        signals.append((pgid, sig))

    monkeypatch.setattr(plane.os, "killpg", killpg)

    result = stop_team(tmp_path, meta["run_id"])

    if expect_ok:
        assert result["identity_verified"] is True
        assert signals
    else:
        assert result["identity_verified"] is False
        assert signals == []
        assert not any(command[0] == "kill-session" for command in commands)


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
        lambda *a, **k: (
            killpg_calls.append(a)
            or (_ for _ in ()).throw(AssertionError("killpg on dry_run"))
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


def test_team_json_publication_ignores_predictable_symlink_temp_and_sets_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "team.json"
    target = tmp_path / "victim"
    target.write_text("unchanged", encoding="utf-8")
    predictable = tmp_path / f".team.json.{os.getpid()}.tmp"
    predictable.symlink_to(target)

    plane._atomic_write_json(path, {"writer": CLI_WRITER})

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert predictable.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["writer"] == CLI_WRITER


def test_stop_refuses_incomplete_scaled_identity_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("chain gap", [TASKS_TWO[0]], root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    _receipt, bad_head = plane._persist_team_identity_receipt(
        tmp_path,
        live["run_id"],
        session=live["session"],
        session_id="$9",
        launch_nonce=live["launch_nonce"],
        generation=2,
        previous_receipt_sha256=live["launch_receipt_sha256"],
        operation="add",
        tasks_before=live["tasks"],
        tasks_after=live["tasks"],
    )
    live["identity_generation"] = 2
    live["identity_receipt_sha256"] = bad_head
    plane._atomic_write_json(team_meta_path(tmp_path, live["run_id"]), live)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", _tmux_identity_runner(live, []))
    monkeypatch.setattr(
        plane.os, "killpg", lambda pgid, sig: signals.append((pgid, int(sig)))
    )

    stopped = stop_team(tmp_path, live["run_id"])

    assert stopped["identity_verified"] is False
    assert signals == []
    assert any("generation 1 missing" in error for error in stopped["errors"])


def test_identity_receipt_publish_then_raise_adopts_exact_published_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts import path_keys

    real_atomic_write = path_keys.atomic_write_bytes
    real_fsync_dir = path_keys.fsync_existing_managed_dir
    synced: list[Path] = []

    def publish_then_raise(*args: Any, **kwargs: Any) -> Path:
        real_atomic_write(*args, **kwargs)
        raise OSError("simulated parent fsync result loss")

    def track_fsync(path: Path) -> None:
        synced.append(path)
        real_fsync_dir(path)

    monkeypatch.setattr(path_keys, "atomic_write_bytes", publish_then_raise)
    monkeypatch.setattr(path_keys, "fsync_existing_managed_dir", track_fsync)

    receipt, receipt_hash = plane._persist_team_identity_receipt(
        tmp_path,
        "run-1",
        session="omg-workers",
        session_id="$7",
        launch_nonce="a" * 32,
        generation=1,
        previous_receipt_sha256="b" * 64,
        operation="add",
        tasks_before=[],
        tasks_after=[],
    )

    path = plane.team_identity_receipt_path(tmp_path, "run-1", 1)
    body = path.read_bytes()
    assert receipt["generation"] == 1
    assert receipt_hash == hashlib.sha256(body).hexdigest()
    assert synced == [path.parent]


def test_identity_receipt_v2_scale_intent_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.writer_chain import canonical_json_bytes

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("v2 intent", [TASKS_TWO[0]], root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    intent = {
        "operation": "add",
        "tasks": [{"task_id": "t-c", "owned_files": ["c.py"]}],
    }
    receipt, head = plane._persist_team_identity_receipt(
        tmp_path,
        live["run_id"],
        session=live["session"],
        session_id="$9",
        launch_nonce=live["launch_nonce"],
        generation=1,
        previous_receipt_sha256=live["launch_receipt_sha256"],
        operation="add",
        tasks_before=live["tasks"],
        tasks_after=live["tasks"],
        scale_intent=intent,
    )
    live["identity_generation"] = 1
    live["identity_receipt_sha256"] = head
    plane._atomic_write_json(team_meta_path(tmp_path, live["run_id"]), live)

    chain = plane._load_team_identity_chain(tmp_path, live["run_id"], live)

    assert receipt["schema_version"] == plane.IDENTITY_RECEIPT_SCHEMA_VERSION
    assert chain[-1]["scale_intent"] == intent
    assert (
        chain[-1]["scale_intent_sha256"]
        == hashlib.sha256(canonical_json_bytes(intent)).hexdigest()
    )


def test_identity_receipt_v2_rejects_scale_intent_hash_tamper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("v2 tamper", [TASKS_TWO[0]], root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    receipt, _head = plane._persist_team_identity_receipt(
        tmp_path,
        live["run_id"],
        session=live["session"],
        session_id="$9",
        launch_nonce=live["launch_nonce"],
        generation=1,
        previous_receipt_sha256=live["launch_receipt_sha256"],
        operation="add",
        tasks_before=live["tasks"],
        tasks_after=live["tasks"],
        scale_intent={"operation": "add", "task_ids": ["t-c"]},
    )
    receipt["scale_intent"] = {"operation": "add", "task_ids": ["t-evil"]}
    body = canonical_json_bytes(receipt)
    plane.team_identity_receipt_path(tmp_path, live["run_id"], 1).write_bytes(body)
    live["identity_generation"] = 1
    live["identity_receipt_sha256"] = sha256_hex(body)
    plane._atomic_write_json(team_meta_path(tmp_path, live["run_id"]), live)

    with pytest.raises(TeamError, match="scale intent mismatch"):
        plane._load_team_identity_chain(tmp_path, live["run_id"], live)


def test_identity_receipt_loader_accepts_committed_legacy_v1_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("legacy identity", [TASKS_TWO[0]], root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    legacy_rows = [
        {
            "task_id": task.get("task_id"),
            "window_index": task.get("window_index"),
            "pane_id": task.get("pane_id"),
            "pid": task.get("pid"),
            "pgid": task.get("pgid"),
            "pid_start": task.get("pid_start"),
        }
        for task in live["tasks"]
    ]
    legacy = {
        "store_kind": "team_identity_receipt",
        "schema_version": 1,
        "writer": CLI_WRITER,
        "run_id": live["run_id"],
        "session_name": live["session"],
        "session_id": "$9",
        "launch_nonce": live["launch_nonce"],
        "generation": 1,
        "previous_receipt_sha256": live["launch_receipt_sha256"],
        "operation": "remove",
        "receipt_nonce": "c" * 32,
        "tasks_before": legacy_rows,
        "tasks_after": legacy_rows,
    }
    body = canonical_json_bytes(legacy)
    path = plane.team_identity_receipt_path(tmp_path, live["run_id"], 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    live["identity_generation"] = 1
    live["identity_receipt_sha256"] = sha256_hex(body)
    plane._atomic_write_json(team_meta_path(tmp_path, live["run_id"]), live)

    chain = plane._load_team_identity_chain(tmp_path, live["run_id"], live)

    assert chain[-1] == legacy


def _write_legacy_v1_launch_receipt(
    root: Path,
    live: dict[str, Any],
    *,
    session_id: str = "$9",
) -> dict[str, Any]:
    """Persist a #106-era schema-v1 launch receipt and sync meta hashes."""
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    rows = [
        {
            "task_id": task.get("task_id"),
            "window_index": task.get("window_index"),
            "pane_id": task.get("pane_id"),
            "pid": task.get("pid"),
            "pgid": task.get("pgid"),
            "pid_start": task.get("pid_start"),
        }
        for task in live["tasks"]
    ]
    receipt = {
        "store_kind": "team_launch_receipt",
        "schema_version": plane.LEGACY_LAUNCH_RECEIPT_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": live["run_id"],
        "session_name": live["session"],
        "session_id": session_id,
        "launch_nonce": live["launch_nonce"],
        "generation": 0,
        "previous_receipt_sha256": None,
        "tasks": rows,
    }
    body = canonical_json_bytes(receipt)
    path = plane.team_launch_receipt_path(root, str(live["run_id"]))
    if path.exists():
        path.unlink()
    atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
    digest = sha256_hex(body)
    live["launch_receipt_sha256"] = digest
    live["identity_receipt_sha256"] = digest
    live["identity_generation"] = 0
    plane._atomic_write_json(team_meta_path(root, str(live["run_id"])), live)
    return receipt


def test_launch_receipt_loader_accepts_schema_v1_for_identity_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#106 v1 launch receipts remain readable for stop/scale/relaunch chains."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("legacy launch", [TASKS_TWO[0]], root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    receipt = _write_legacy_v1_launch_receipt(tmp_path, live)

    loaded = plane._load_team_launch_receipt(tmp_path, live["run_id"], live)
    chain = plane._load_team_identity_chain(tmp_path, live["run_id"], live)

    assert loaded == receipt
    assert loaded["schema_version"] == plane.LEGACY_LAUNCH_RECEIPT_SCHEMA_VERSION
    assert "intent_nonce" not in loaded
    assert "window_name" not in loaded
    assert chain[0] == receipt


def test_launch_receipt_loader_still_accepts_schema_v2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Current schema-v2 receipts (with intent binding fields) still load."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("v2 launch", [TASKS_TWO[0]], root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)

    loaded = plane._load_team_launch_receipt(tmp_path, live["run_id"], live)
    chain = plane._load_team_identity_chain(tmp_path, live["run_id"], live)

    assert loaded["schema_version"] == plane.LAUNCH_RECEIPT_SCHEMA_VERSION
    assert "intent_nonce" in loaded
    assert "window_name" in loaded
    assert chain[0] == loaded


def test_identity_receipt_publish_then_raise_rejects_unexpected_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.contracts import path_keys

    real_atomic_write = path_keys.atomic_write_bytes

    def publish_other_then_raise(path: Path, _body: bytes, **kwargs: Any) -> Path:
        real_atomic_write(path, b'{"unexpected":true}', **kwargs)
        raise OSError("simulated ambiguous publication")

    monkeypatch.setattr(path_keys, "atomic_write_bytes", publish_other_then_raise)

    with pytest.raises(OSError, match="ambiguous publication"):
        plane._persist_team_identity_receipt(
            tmp_path,
            "run-1",
            session="omg-workers",
            session_id="$7",
            launch_nonce="a" * 32,
            generation=1,
            previous_receipt_sha256="b" * 64,
            operation="add",
            tasks_before=[],
            tasks_after=[],
        )

    path = plane.team_identity_receipt_path(tmp_path, "run-1", 1)
    assert path.read_bytes() == b'{"unexpected":true}'


@pytest.mark.parametrize(
    "durability_error",
    [
        OSError("retry fsync failed"),
        pytest.param(
            None,
            id="contract-path-error",
        ),
    ],
    ids=["os-error", None],
)
def test_identity_receipt_publish_then_raise_propagates_durability_retry_failure(
    durability_error: BaseException | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omg_cli.contracts import path_keys
    from omg_cli.contracts.path_keys import ContractPathError

    real_atomic_write = path_keys.atomic_write_bytes
    failure = durability_error or ContractPathError("retry fsync refused")

    def publish_then_raise(*args: Any, **kwargs: Any) -> Path:
        real_atomic_write(*args, **kwargs)
        raise OSError("simulated parent fsync result loss")

    def fail_fsync(_path: Path) -> None:
        raise failure

    monkeypatch.setattr(path_keys, "atomic_write_bytes", publish_then_raise)
    monkeypatch.setattr(path_keys, "fsync_existing_managed_dir", fail_fsync)

    with pytest.raises(type(failure), match="retry fsync"):
        plane._persist_team_identity_receipt(
            tmp_path,
            "run-1",
            session="omg-workers",
            session_id="$7",
            launch_nonce="a" * 32,
            generation=1,
            previous_receipt_sha256="b" * 64,
            operation="add",
            tasks_before=[],
            tasks_after=[],
        )

    path = plane.team_identity_receipt_path(tmp_path, "run-1", 1)
    assert json.loads(path.read_text(encoding="utf-8"))["generation"] == 1


def test_stop_after_scale_validates_chain_and_signals_only_current_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("scaled stop", TASKS_TWO, root=tmp_path, dry_run=True)
    live = _write_live_stop_identity(tmp_path, meta, monkeypatch)
    before = list(live["tasks"])
    after = [before[0]]
    _receipt, head = plane._persist_team_identity_receipt(
        tmp_path,
        live["run_id"],
        session=live["session"],
        session_id="$9",
        launch_nonce=live["launch_nonce"],
        generation=1,
        previous_receipt_sha256=live["launch_receipt_sha256"],
        operation="remove",
        tasks_before=before,
        tasks_after=after,
    )
    live["tasks"][1]["status"] = "scaled_down"
    live["tasks"][1]["pid"] = None
    live["tasks"][1]["pgid"] = None
    live["tasks"][1]["pid_start"] = None
    live["identity_generation"] = 1
    live["identity_receipt_sha256"] = head
    plane._atomic_write_json(team_meta_path(tmp_path, live["run_id"]), live)
    commands: list[list[str]] = []
    runner = _tmux_identity_runner({**live, "tasks": after}, commands)
    gone = False
    signals: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        nonlocal gone
        if sig == 0:
            if gone:
                raise ProcessLookupError("gone")
            return
        signals.append((pgid, int(sig)))
        gone = True

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", runner)
    monkeypatch.setattr(
        plane.os,
        "getpgid",
        lambda _pid: (
            (_ for _ in ()).throw(ProcessLookupError("gone"))
            if gone
            else after[0]["pgid"]
        ),
    )
    monkeypatch.setattr(plane.os, "killpg", killpg)

    stopped = stop_team(tmp_path, live["run_id"])

    assert stopped["identity_verified"] is True, stopped
    assert signals == [(after[0]["pgid"], int(signal.SIGTERM))]
    assert all(row["task_id"] == after[0]["task_id"] for row in stopped["signalled"])


# ---------------------------------------------------------------------------
# D3 multi-CLI routing (dry-run only)
# ---------------------------------------------------------------------------


def test_zero_config_still_all_grok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No routing → D1 parity: all grok panes."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    meta = start_team("z", TASKS_TWO, root=tmp_path, dry_run=True)
    assert meta.get("multi_cli") is False
    assert meta.get("routing") is None
    for rec in meta["tasks"]:
        assert rec["argv"][0] == "grok"
        assert rec["provider"] == "grok"
        assert rec["needs_pty"] is False


def test_dry_run_multi_cli_codex_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    tasks = [
        {
            "task_id": "t1",
            "role": "executor",
            "owned_files": ["a.py"],
        }
    ]
    meta = start_team(
        "multi",
        tasks,
        root=tmp_path,
        dry_run=True,
        routing={"executor": {"provider": "codex"}},
        available_providers=_PROVIDERS_ALL,
    )
    assert meta["multi_cli"] is True
    assert meta["dry_run"] is True
    assert meta["routing"] is not None
    assert meta["routing"]["by_role"]["executor"]["provider"] == "codex"
    assert len(meta["tasks"]) == 1
    rec = meta["tasks"][0]
    assert rec["task_id"] == "t1"
    assert rec["provider"] == "codex"
    assert rec["role"] == "executor"
    assert rec["posture"] == "read-write"
    assert rec["argv"][0] == "codex"
    assert "exec" in rec["argv"]
    assert "-s" in rec["argv"]
    assert rec["argv"][rec["argv"].index("-s") + 1] == "workspace-write"
    assert rec["needs_pty"] is False
    assert rec["prompt_delivery"] == PROMPT_DELIVERY_STDIN
    assert rec["pid"] is None
    # pane command embeds codex, not only grok
    assert "codex" in rec["pane_command"]
    # stdin delivery: redirect prompt file into codex's trailing `-`
    assert rec["argv"][-1] == "-"
    # shell fragment ends with: ... - < '<promptfile>'
    assert " < " in rec["pane_command"]
    prompt_path = Path(rec["worktree"]) / ".omg" / "team-prompt" / "t1.prompt.md"
    assert prompt_path.is_file()
    assert (
        str(prompt_path) in rec["pane_command"]
        or prompt_path.name in rec["pane_command"]
    )
    # Body must NOT appear in recorded argv (stays out of ps for stdin mode).
    body = prompt_path.read_text(encoding="utf-8")
    assert body not in rec["argv"]
    run = load_run(tmp_path, meta["run_id"])
    assert run is not None
    assert run.get("verified") is not True


def test_dry_run_agy_records_needs_pty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    tasks = [{"task_id": "t-agy", "role": "executor", "owned_files": ["x.py"]}]
    meta = start_team(
        "agy pane",
        tasks,
        root=tmp_path,
        dry_run=True,
        routing={"executor": {"provider": "agy"}},
        available_providers=_PROVIDERS_ALL,
    )
    rec = meta["tasks"][0]
    assert rec["provider"] == "agy"
    assert rec["needs_pty"] is True
    assert rec["argv"][0] == "agy"
    assert rec["prompt_delivery"] == PROMPT_DELIVERY_POSITIONAL_TEXT
    assert "pty" in rec["pane_command"] or "python3" in rec["pane_command"]
    assert meta["routing"]["by_role"]["executor"]["needs_pty"] is True
    # positional-text: prompt BODY (not path alone) must reach the pty payload
    # (JSON-escaped inside python3 -c payload, so match a distinctive line).
    prompt_path = Path(rec["worktree"]) / ".omg" / "team-prompt" / "t-agy.prompt.md"
    body = prompt_path.read_text(encoding="utf-8")
    assert "agy pane" in body
    assert "agy pane" in rec["pane_command"]
    # path placeholder must not remain as the sole -p value in the pane payload
    # once body is substituted (argv record still has the path).
    assert str(prompt_path) in rec["argv"]
    assert str(prompt_path) not in rec["pane_command"]


def test_build_executor_pane_command_codex_stdin_redirect(tmp_path: Path) -> None:
    """Unit: codex pane ends with `... - < 'promptfile'`."""
    pf = tmp_path / "task.prompt.md"
    pf.write_text("DO THE TASK\n", encoding="utf-8")
    inv = build_executor_argv(
        "codex",
        "executor",
        prompt_file=pf,
        cwd=tmp_path,
        model=None,
    )
    assert inv.prompt_delivery == PROMPT_DELIVERY_STDIN
    cmd = build_executor_pane_command(
        inv.argv,
        needs_pty=inv.needs_pty,
        prompt_delivery=inv.prompt_delivery,
        prompt_file=pf,
    )
    assert "codex" in cmd
    assert inv.argv[-1] == "-"
    # Redirection on the inner exec, not in the argv list itself.
    assert (
        f"< {pf!s}" in cmd
        or f"< '{pf}'" in cmd
        or f'< "{pf}"' in cmd
        or (" < " in cmd and str(pf) in cmd)
    )
    assert "DO THE TASK" not in cmd  # body not inlined for stdin mode


def test_build_executor_pane_command_cursor_positional_body(tmp_path: Path) -> None:
    pf = tmp_path / "task.prompt.md"
    body = "CURSOR_TASK_BODY_UNIQUE_xyz"
    pf.write_text(body, encoding="utf-8")
    inv = build_executor_argv(
        "cursor",
        "executor",
        prompt_file=pf,
        cwd=tmp_path,
    )
    assert inv.prompt_delivery == PROMPT_DELIVERY_POSITIONAL_TEXT
    assert inv.argv[-1] == str(pf)  # path placeholder at build time
    cmd = build_executor_pane_command(
        inv.argv,
        needs_pty=inv.needs_pty,
        prompt_delivery=inv.prompt_delivery,
        prompt_file=pf,
    )
    assert body in cmd
    assert "cursor-agent" in cmd


def test_build_executor_pane_command_grok_prompt_file_unchanged(tmp_path: Path) -> None:
    pf = tmp_path / "task.prompt.md"
    pf.write_text("GROK_BODY\n", encoding="utf-8")
    inv = build_executor_argv(
        "grok",
        "executor",
        prompt_file=pf,
        cwd=tmp_path,
    )
    assert inv.prompt_delivery == PROMPT_DELIVERY_PROMPT_FILE
    cmd = build_executor_pane_command(
        inv.argv,
        needs_pty=inv.needs_pty,
        prompt_delivery=inv.prompt_delivery,
        prompt_file=pf,
    )
    assert "--prompt-file" in cmd
    assert str(pf) in cmd
    assert "GROK_BODY" not in cmd  # body stays in file
    assert " < " not in cmd


def test_start_rejects_cursor_on_reviewer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)

    tasks = [
        {
            "task_id": "rev",
            "role": "code-reviewer",
            "owned_files": ["r.py"],
        }
    ]
    with pytest.raises((TeamError, RoutingError), match="FLOOR 1|cursor|structured"):
        start_team(
            "bad",
            tasks,
            root=tmp_path,
            dry_run=True,
            routing={"code-reviewer": {"provider": "cursor"}},
            available_providers=_PROVIDERS_ALL,
        )


def test_start_rejects_unknown_role_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)

    tasks = [{"task_id": "t1", "owned_files": ["a.py"]}]
    with pytest.raises(UnknownRoleError):
        start_team(
            "bad role",
            tasks,
            root=tmp_path,
            dry_run=True,
            routing={"not-a-role": {"provider": "codex"}},
            available_providers=_PROVIDERS_ALL,
        )


def test_loud_fallback_at_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", _boom_tmux)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    tasks = [{"task_id": "t1", "role": "executor", "owned_files": ["a.py"]}]
    meta = start_team(
        "fallback",
        tasks,
        root=tmp_path,
        dry_run=True,
        routing={"executor": {"provider": "codex"}},
        available_providers=frozenset({"grok"}),  # codex missing → loud fallback
    )
    rec = meta["tasks"][0]
    assert rec["provider"] == "grok"
    assert rec["argv"][0] == "grok"
    route = meta["routing"]["by_role"]["executor"]
    assert route["fallback_from"] == "codex"
    assert route["warning"]
    assert meta["routing"]["warnings"]
    err = capsys.readouterr().err
    assert "codex" in err or route["warning"]


def test_resolved_routing_snapshot_stable_in_team_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setattr(plane, "tmux_available", lambda: False)
    monkeypatch.setattr(plane.subprocess, "run", _boom_subprocess)

    tasks = [
        {"task_id": "t1", "role": "executor", "owned_files": ["a.py"]},
        {"task_id": "t2", "role": "code-reviewer", "owned_files": ["b.py"]},
    ]
    meta = start_team(
        "snap",
        tasks,
        root=tmp_path,
        dry_run=True,
        routing={
            "executor": {"provider": "codex"},
            "code-reviewer": {"provider": "gemini"},
        },
        available_providers=_PROVIDERS_ALL,
    )
    rid = meta["run_id"]
    disk = load_team_meta(tmp_path, rid)
    assert disk["routing"] == meta["routing"]
    assert disk["routing"]["by_role"]["executor"]["provider"] == "codex"
    assert disk["routing"]["by_role"]["code-reviewer"]["provider"] == "gemini"
    # status is pure read — does not rewrite routing
    st = team_status(tmp_path, rid)
    locked = status_locked_view(st)
    assert set(locked.keys()) == set(STATUS_TOP_KEYS)
    disk2 = load_team_meta(tmp_path, rid)
    assert disk2["routing"] == disk["routing"]


def test_cli_team_start_dry_run_with_routing(
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
    # Hermetic: force binary-check skip by only using providers we can fake via
    # PATH? resolve_routing probes PATH. Inject a fake codex on PATH.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("codex", "grok"):
        p = fake_bin / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")

    tasks = json.dumps([{"task_id": "t1", "role": "executor", "owned_files": ["a.py"]}])
    routing = json.dumps({"executor": {"provider": "codex"}})
    r = subprocess.run(
        [
            PYTHON,
            str(BIN_OMG),
            "team",
            "start",
            "--dry-run",
            "--goal",
            "cli multi",
            "--tasks-json",
            tasks,
            "--routing",
            routing,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    payload = json.loads(r.stdout)
    assert payload["multi_cli"] is True
    assert payload["tasks"][0]["provider"] == "codex"
    assert payload["tasks"][0]["argv"][0] == "codex"
    assert team_meta_path(tmp_path, payload["run_id"]).is_file()


def _create_native_plane(
    root: Path, *, transport: str = "grok_native"
) -> dict[str, Any]:
    return create_native_team(
        root,
        run_id="run-native",
        team_id="team-native",
        leader_id="leader",
        parent_session_id="parent-session",
        base_sha="a" * 40,
        transport=transport,
        created_at="2026-07-22T00:00:00Z",
        tasks=[
            {
                "task_id": "verify-1",
                "role": "verifier",
                "prompt": "verify one bounded slice",
                "verification_commands": [["python3", "-V"]],
                "artifact_contract": {"kind": "team-result"},
            }
        ],
    )


def _native_inventory(prepared: dict[str, Any]) -> list[dict[str, Any]]:
    pair = prepared["receipt_pair"]
    return [
        {
            "spawn_receipt_hash": pair["spawn_receipt_hash"],
            "role_receipt_hash": pair["role_receipt_hash"],
            "run_id": "run-native",
            "task_id": "verify-1",
            "parent_id": "leader",
            "host_spawn_id": "grok-child-1",
            "observed_session_id": "grok-session-1",
        }
    ]


def test_native_plane_exact_spawn_receipts_result_cas_and_terminal_flow(
    tmp_path: Path,
) -> None:
    created = _create_native_plane(tmp_path)
    assert _create_native_plane(tmp_path) == created
    task = created["tasks"]["verify-1"]
    assert task["envelope"]["depth"] == 1
    assert task["envelope"]["capability_mode"] == "read-only"
    assert task["envelope"]["requested_role"] == "omg-verifier"

    prepared = prepare_native_spawn(
        tmp_path,
        run_id="run-native",
        team_id="team-native",
        task_id="verify-1",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="verify bounded slice",
        expires_at="2099-01-01T00:00:00Z",
    )
    invocation = prepared["invocation"]
    assert invocation["tool_name"] == "spawn_subagent"
    assert invocation["transport"] == "grok_native"
    assert set(invocation["tool_input"]) == {
        "prompt",
        "description",
        "subagent_type",
        "background",
        "capability_mode",
    }
    assert invocation["tool_input"]["capability_mode"] == "read-only"
    assert invocation["tool_input"]["background"] is True
    assert "argv" not in invocation

    bound = reconcile_native_spawn(
        tmp_path,
        run_id="run-native",
        team_id="team-native",
        task_id="verify-1",
        inventory=_native_inventory(prepared),
        expected_state="spawn_requested",
        expected_sequence=1,
        expected_generation=0,
        now=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    assert bound["outcome"] == "bound"
    running = bound["task"]
    binding = running["binding"]
    result = {
        "store_kind": "native_worker_result",
        "schema_version": 1,
        "transport": "grok_native",
        "run_id": "run-native",
        "team_id": "team-native",
        "task_id": "verify-1",
        "generation": 0,
        "host_spawn_id": binding["host_spawn_id"],
        "observed_session_id": binding["observed_session_id"],
        "spawn_receipt_hash": binding["spawn_receipt_hash"],
        "role_receipt_hash": binding["role_receipt_hash"],
        "expected_state": "running",
        "expected_sequence": 2,
        "replay_id": "result-1",
        "status": "ok",
        "artifact": {"kind": "team-result"},
        "verification_evidence": ["b" * 64],
        "completed_at": "2026-07-22T00:01:00Z",
    }
    with pytest.raises(TeamError, match="evidence count"):
        record_native_result(
            tmp_path,
            result={**result, "verification_evidence": []},
        )
    accepted = record_native_result(tmp_path, result=result)
    assert accepted["task"]["state"] == "delivered"
    assert record_native_result(tmp_path, result=result)["duplicate"] is True
    with pytest.raises(TeamError, match="crossed"):
        record_native_result(tmp_path, result={**result, "transport": "tmux_grok"})

    integrating = transition_native_delivery(
        tmp_path,
        run_id="run-native",
        team_id="team-native",
        task_id="verify-1",
        expected_state="delivered",
        expected_sequence=3,
        expected_generation=0,
        next_state="integrating",
        result_hash=accepted["result_hash"],
    )
    transition_native_delivery(
        tmp_path,
        run_id="run-native",
        team_id="team-native",
        task_id="verify-1",
        expected_state="integrating",
        expected_sequence=integrating["sequence"],
        expected_generation=0,
        next_state="complete",
        result_hash=accepted["result_hash"],
    )
    assert (
        native_team_status(tmp_path, run_id="run-native", team_id="team-native")[
            "complete"
        ]
        is True
    )


def test_native_plane_reuses_receipt_after_crash_before_task_cas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _create_native_plane(tmp_path)
    real_cas = plane._cas_native_task
    captured: dict[str, Any] = {}

    def crash_after_receipt(*args: Any, **kwargs: Any):
        captured.update(kwargs["updates"])
        raise TeamError("injected crash after receipt persistence")

    monkeypatch.setattr(plane, "_cas_native_task", crash_after_receipt)
    with pytest.raises(TeamError, match="injected crash"):
        prepare_native_spawn(
            tmp_path,
            run_id="run-native",
            team_id="team-native",
            task_id="verify-1",
            expected_sequence=0,
            expected_generation=0,
            lease_generation=0,
            description="verify bounded slice",
            expires_at="2099-01-01T00:00:00Z",
        )
    monkeypatch.setattr(plane, "_cas_native_task", real_cas)
    prepared = prepare_native_spawn(
        tmp_path,
        run_id="run-native",
        team_id="team-native",
        task_id="verify-1",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="verify bounded slice",
        expires_at="2099-01-01T00:00:00Z",
    )
    assert prepared["task"]["receipt_id"] == captured["receipt_id"]
    assert prepared["task"]["spawn_receipt_hash"] == captured["spawn_receipt_hash"]


def test_native_plane_rejects_capability_mismatch_and_lane_switch(
    tmp_path: Path,
) -> None:
    with pytest.raises(TeamError, match="capability_mode"):
        create_native_team(
            tmp_path,
            run_id="run-native",
            team_id="team-native",
            leader_id="leader",
            parent_session_id="parent-session",
            base_sha="a" * 40,
            tasks=[
                {
                    "task_id": "bad",
                    "role": "verifier",
                    "capability_mode": "read-write",
                }
            ],
        )
    _create_native_plane(tmp_path, transport="tmux_grok")
    with pytest.raises(TeamError, match="cannot switch"):
        prepare_native_spawn(
            tmp_path,
            run_id="run-native",
            team_id="team-native",
            task_id="verify-1",
            expected_sequence=0,
            expected_generation=0,
            lease_generation=0,
            description="wrong lane",
        )
    assert (
        load_native_team(tmp_path, "run-native", "team-native")["transport"]
        == "tmux_grok"
    )


def test_native_plane_receipt_identity_includes_team_and_write_scope_is_required(
    tmp_path: Path,
) -> None:
    _create_native_plane(tmp_path)
    create_native_team(
        tmp_path,
        run_id="run-native",
        team_id="team-native-2",
        leader_id="leader",
        parent_session_id="parent-session",
        base_sha="a" * 40,
        created_at="2026-07-22T00:00:00Z",
        tasks=[{"task_id": "verify-1", "role": "verifier", "prompt": "second"}],
    )
    one = prepare_native_spawn(
        tmp_path,
        run_id="run-native",
        team_id="team-native",
        task_id="verify-1",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="first team",
        expires_at="2099-01-01T00:00:00Z",
    )
    two = prepare_native_spawn(
        tmp_path,
        run_id="run-native",
        team_id="team-native-2",
        task_id="verify-1",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="second team",
        expires_at="2099-01-01T00:00:00Z",
    )
    assert one["task"]["receipt_id"] != two["task"]["receipt_id"]

    with pytest.raises(TeamError, match="explicit write scope"):
        create_native_team(
            tmp_path,
            run_id="run-write",
            team_id="team-write",
            leader_id="leader",
            parent_session_id="parent-session",
            base_sha="a" * 40,
            tasks=[{"task_id": "write-1", "role": "executor"}],
        )
