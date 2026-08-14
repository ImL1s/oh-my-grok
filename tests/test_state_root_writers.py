"""#74 PR2 — core run-state writers honor resolve_state_root().state_dir."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omg_cli.main import build_parser
from omg_cli.state import (
    _active_path,
    _create_lock_path,
    _physical_state_dir,
    _runs_dir,
    create_run,
    disable_force_verified_for_tests,
    enable_force_verified_for_tests,
    load_active_run,
    load_run,
    set_verified,
    write_status,
)
from omg_cli.state_root import (
    ENV_DISABLE_WORKSPACE_MARKER,
    ENV_STATE_DIR,
    ENV_WORKSPACE_MARKER,
    resolve_state_root,
)

_POSIX = os.name == "posix"


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _clear_state_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_STATE_DIR, raising=False)
    monkeypatch.delenv(ENV_WORKSPACE_MARKER, raising=False)
    monkeypatch.delenv(ENV_DISABLE_WORKSPACE_MARKER, raising=False)


def _option_strings(parser) -> set[str]:
    names: set[str] = set()
    for action in parser._actions:
        names.update(action.option_strings)
        dest = getattr(action, "choices", None)
        if isinstance(dest, dict):
            for sub in dest.values():
                names.update(_option_strings(sub))
    return names


def test_no_omg_state_dir_cli_flag() -> None:
    names = _option_strings(build_parser())
    assert "--state-dir" not in names
    assert "--state_dir" not in names
    assert "--omg-state-dir" not in names


def test_runs_dir_follows_centralized_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_state_root_env(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    central = tmp_path / "central"
    central.mkdir()
    monkeypatch.setenv(ENV_STATE_DIR, str(central))
    expected = resolve_state_root(cwd=project, explicit_project_root=project)
    assert expected.scope == "centralized"
    assert _physical_state_dir(project) == expected.state_dir
    runs = _runs_dir(project)
    assert runs == expected.state_dir / "state" / "runs"
    assert _active_path(project) == expected.state_dir / "state" / "active.json"
    assert _create_lock_path(project) == expected.state_dir / "state" / "create.lock"
    assert _under(runs, central)
    assert not _under(runs, project / ".omg")


def test_runs_dir_kill_switch_forces_per_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_state_root_env(monkeypatch)
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    (ws / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    monkeypatch.setenv(ENV_WORKSPACE_MARKER, "1")
    shared = _runs_dir(repo)
    assert shared == (ws / ".omg" / "state" / "runs").resolve()
    assert _under(shared, ws / ".omg")
    assert not _under(shared, repo / ".omg")

    monkeypatch.setenv(ENV_DISABLE_WORKSPACE_MARKER, "1")
    killed = _runs_dir(repo)
    assert killed == repo.resolve() / ".omg" / "state" / "runs"
    assert _under(killed, repo / ".omg")
    assert killed != ws.resolve() / ".omg" / "state" / "runs"


def test_runs_dir_default_per_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_state_root_env(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    assert _runs_dir(project) == (project / ".omg" / "state" / "runs").resolve()
    assert _physical_state_dir(project) == (project / ".omg").resolve()


@pytest.mark.skipif(not _POSIX, reason="ensure_managed_dir requires POSIX")
def test_create_run_writes_centralized_not_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_state_root_env(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    central = tmp_path / "central"
    central.mkdir()
    monkeypatch.setenv(ENV_STATE_DIR, str(central))
    expected = resolve_state_root(cwd=project, explicit_project_root=project)
    assert expected.scope == "centralized"

    run = create_run(project, mode="ralph", goal="central store")
    rid = run["run_id"]
    assert run["verified"] is False
    assert run["status"] == "initialized"

    status_path = expected.state_dir / "state" / "runs" / rid / "status.json"
    active_path = expected.state_dir / "state" / "active.json"
    lock_path = expected.state_dir / "state" / "create.lock"
    assert status_path.is_file()
    assert active_path.is_file()
    assert lock_path.exists()
    assert json.loads(status_path.read_text(encoding="utf-8"))["verified"] is False

    local_state = project / ".omg" / "state"
    assert not (local_state / "runs" / rid / "status.json").exists()
    assert not (local_state / "active.json").exists()
    assert not (local_state / "create.lock").exists()
    assert not _under(status_path, local_state)

    write_status(project, rid, "running")
    loaded = load_run(project, rid)
    assert loaded is not None
    assert loaded["status"] == "running"
    assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "running"
    assert load_active_run(project)["run_id"] == rid
    assert not (local_state / "runs" / rid / "status.json").exists()


@pytest.mark.skipif(not _POSIX, reason="ensure_managed_dir requires POSIX")
def test_kill_switch_create_run_stays_per_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_state_root_env(monkeypatch)
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    (ws / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    monkeypatch.setenv(ENV_WORKSPACE_MARKER, "1")
    monkeypatch.setenv(ENV_DISABLE_WORKSPACE_MARKER, "true")

    run = create_run(repo, mode="ulw", goal="killed marker")
    rid = run["run_id"]
    local_status = repo / ".omg" / "state" / "runs" / rid / "status.json"
    local_active = repo / ".omg" / "state" / "active.json"
    assert local_status.is_file()
    assert local_active.is_file()
    assert not (ws / ".omg" / "state" / "runs" / rid / "status.json").exists()
    assert not (ws / ".omg" / "state" / "active.json").exists()


@pytest.mark.skipif(not _POSIX, reason="ensure_managed_dir requires POSIX")
def test_workspace_marker_create_run_uses_workspace_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_state_root_env(monkeypatch)
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    (ws / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    monkeypatch.setenv(ENV_WORKSPACE_MARKER, "1")

    run = create_run(repo, mode="ulw", goal="shared workspace")
    rid = run["run_id"]
    ws_status = ws / ".omg" / "state" / "runs" / rid / "status.json"
    assert ws_status.is_file()
    assert not (repo / ".omg" / "state" / "runs" / rid / "status.json").exists()
    assert load_active_run(repo)["run_id"] == rid


@pytest.mark.skipif(not _POSIX, reason="ensure_managed_dir requires POSIX")
def test_centralized_verified_only_via_set_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_state_root_env(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    central = tmp_path / "central"
    central.mkdir()
    monkeypatch.setenv(ENV_STATE_DIR, str(central))
    run = create_run(project, mode="ulw", goal="no smuggle verified")
    rid = run["run_id"]
    status_path = _runs_dir(project) / rid / "status.json"
    body = json.loads(status_path.read_text(encoding="utf-8"))
    assert body["verified"] is False
    assert body["status"] != "verified"

    token = enable_force_verified_for_tests()
    try:
        assert token is not None
        verified = set_verified(project, rid, force=True)
    finally:
        disable_force_verified_for_tests()

    assert verified["verified"] is True
    assert verified["status"] == "verified"
    disk = json.loads(status_path.read_text(encoding="utf-8"))
    assert disk["verified"] is True
    assert not (project / ".omg" / "state" / "runs" / rid / "status.json").exists()
