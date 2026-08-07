"""#102 topology authority characterization and adversarial regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omg_cli.team import plane, topology
from omg_cli.team.topology import (
    TopologyError,
    VIEW_MODE_DEDICATED_WINDOW,
    VIEW_MODE_LEGACY_WINDOWS,
    VIEW_MODE_SAME_WINDOW,
    build_topology_snapshot,
    normalize_persisted_topology,
    placement_target_for_add,
    placement_target_for_relaunch,
    resolve_persisted_view_mode,
    topology_sha256,
)


def test_persisted_modes_include_legacy_windows() -> None:
    assert VIEW_MODE_LEGACY_WINDOWS not in topology.LAUNCH_VIEW_MODES
    assert VIEW_MODE_LEGACY_WINDOWS in topology.PERSISTED_VIEW_MODES


def test_topology_windows_string_classifies_as_legacy_not_same_window() -> None:
    mode = resolve_persisted_view_mode({"topology": "windows", "session": "omg-x"})
    assert mode == VIEW_MODE_LEGACY_WINDOWS
    with pytest.raises(TopologyError):
        # Ambiguous inside shape still refuses same_window invention.
        resolve_persisted_view_mode(
            {
                "attach_mode": "inside",
                "session_owned": False,
                "window_id": "@1",
            }
        )


def test_corrupt_windows_plus_same_window_view_mode_fail_closed() -> None:
    """#102 blocker: topology=windows + view_mode=same_window must not promote."""
    corrupt = {
        "topology": "windows",
        "view_mode": VIEW_MODE_SAME_WINDOW,
        "session": "omg-workers",
        "session_id": "$7",
        "launch_nonce": "a" * 32,
    }
    with pytest.raises(TopologyError, match="conflicts with view_mode"):
        resolve_persisted_view_mode(corrupt)
    with pytest.raises(TopologyError, match="conflicts|cannot combine"):
        normalize_persisted_topology(corrupt)

    from omg_cli.team import scaling
    from omg_cli.team.plane import TeamError

    with pytest.raises(TeamError, match="conflicts with view_mode"):
        scaling._resolve_scale_view_mode(corrupt)
    with pytest.raises(TeamError, match="conflicts with view_mode"):
        scaling._scale_request_payload(
            meta=corrupt,
            active=[],
            task_specs=[{"task_id": "x", "prompt": "p", "owned_files": []}],
            start_index=0,
            yolo=False,
            safe=False,
            extra=None,
        )


def test_normalize_same_window_snapshot_from_receipt_and_meta() -> None:
    receipt = {
        "view_mode": VIEW_MODE_SAME_WINDOW,
        "session_name": "omg-workers",
        "session_id": "$7",
        "launch_nonce": "a" * 32,
        "window_id": "@12",
        "leader_pane_id": "%3",
        "leader_pane_pid": 4242,
        "session_owned": False,
        "attach_mode": "inside",
        "layout": "main-vertical",
        "tasks": [
            {
                "task_id": "w1",
                "window_index": 0,
                "pane_id": "%8",
                "pid": 1,
                "pgid": 1,
                "pid_start": "proc:1",
            },
            {
                "task_id": "w2",
                "window_index": 1,
                "pane_id": "%9",
                "pid": 2,
                "pgid": 2,
                "pid_start": "proc:2",
            },
        ],
    }
    meta = {
        "topology": "split",
        "session": "omg-workers",
        "session_id": "$7",
        "launch_nonce": "a" * 32,
        "window_id": "@12",
        "leader_pane_id": "%3",
        "leader_pane_pid": 4242,
        "session_owned": False,
        "view_mode": VIEW_MODE_SAME_WINDOW,
        "identity_generation": 0,
        "identity_receipt_sha256": "b" * 64,
        "tasks": receipt["tasks"],
    }
    snap = normalize_persisted_topology(meta, receipt=receipt)
    assert snap.mode == VIEW_MODE_SAME_WINDOW
    assert snap.topology_string == "split"
    assert snap.anchor.team_window_id == "@12"
    assert snap.anchor.leader_pane_id == "%3"
    assert [w.task_id for w in snap.active_workers] == ["w1", "w2"]
    assert [w.logical_worker_index for w in snap.active_workers] == [0, 1]
    target = placement_target_for_add(snap)
    assert target.window_id == "@12"
    assert target.split_target_pane_id == "%9"
    assert target.horizontal_first is False
    assert placement_target_for_relaunch(snap).window_id == "@12"
    # Fingerprint is stable for identical authority.
    assert topology_sha256(snap) == topology_sha256(
        build_topology_snapshot(meta, receipt=receipt)
    )


def test_refresh_active_workers_after_scale_add_clears_stale_panes() -> None:
    """#102 blocker: scale commit must refresh nested active_workers."""
    from omg_cli.team import scaling

    meta: dict[str, Any] = {
        "topology": "split",
        "view_mode": VIEW_MODE_SAME_WINDOW,
        "session": "omg-workers",
        "session_id": "$7",
        "launch_nonce": "a" * 32,
        "window_id": "@12",
        "leader_pane_id": "%3",
        "leader_pane_pid": 4242,
        "session_owned": False,
        "identity_generation": 1,
        "identity_receipt_sha256": "c" * 64,
        "next_worker_index": 2,
        "tasks": [
            {
                "task_id": "w0",
                "logical_worker_index": 0,
                "window_index": 0,
                "pane_id": "%8",
                "attempt": 1,
                "status": "running",
            },
            {
                "task_id": "w1",
                "logical_worker_index": 1,
                "window_index": 1,
                "pane_id": "%42",
                "attempt": 1,
                "status": "running",
            },
        ],
        "tmux_topology": {
            "schema_version": 1,
            "anchor": {
                "mode": VIEW_MODE_SAME_WINDOW,
                "session_name": "omg-workers",
                "session_id": "$7",
                "launch_nonce": "a" * 32,
                "session_owned": False,
                "team_window_id": "@12",
                "leader_pane_id": "%3",
                "leader_pane_pid": 4242,
            },
            "identity_generation": 0,
            "identity_receipt_sha256": "b" * 64,
            # Stale: still points at pre-add pane only.
            "active_workers": [
                {
                    "task_id": "w0",
                    "logical_worker_index": 0,
                    "attempt": 1,
                    "window_id": "@12",
                    "pane_id": "%8",
                }
            ],
            "placement": {
                "strategy": "right_stack",
                "right_stack_root_pane_id": "%8",
            },
            "layout": {
                "name": "main-vertical",
                "leader_width_policy": "clamped_half",
                "status": "clean",
                "last_error_code": None,
            },
        },
    }
    # Before refresh, placement would target stale %8 (poison).
    stale = normalize_persisted_topology(meta)
    assert placement_target_for_add(stale).split_target_pane_id == "%8"

    scaling._refresh_tmux_topology_projection(meta)
    assert meta["next_logical_worker_index"] == 2
    workers = meta["tmux_topology"]["active_workers"]
    assert [w["task_id"] for w in workers] == ["w0", "w1"]
    assert workers[1]["pane_id"] == "%42"
    assert meta["tmux_topology"]["placement"]["right_stack_root_pane_id"] == "%42"
    # Anchor immutable.
    assert meta["tmux_topology"]["anchor"]["leader_pane_id"] == "%3"

    fresh = normalize_persisted_topology(meta)
    assert placement_target_for_add(fresh).split_target_pane_id == "%42"


def test_resync_window_indices_is_noop() -> None:
    from omg_cli.team import scaling

    assert hasattr(scaling, "_resync_window_indices")
    tasks = [{"pane_id": "%1", "window_index": 7, "logical_worker_index": 7}]
    scaling._resync_window_indices("omg", tasks)
    assert tasks[0]["window_index"] == 7
    assert tasks[0]["logical_worker_index"] == 7


def test_add_tmux_windows_same_window_uses_split_not_new_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#96/#102 floor: same_window scale routes to split primitives."""
    from omg_cli.team import scaling
    from omg_cli.team.tmux import SpawnedWorkerPane

    calls: list[str] = []
    argv_trace: list[list[str]] = []

    def fake_spawn(**kwargs: Any) -> SpawnedWorkerPane:
        calls.append("spawn_same_window")
        assert kwargs["team_window_id"] == "@12"
        assert "new-window" not in str(kwargs)
        return SpawnedWorkerPane(
            session_id="$7",
            window_id="@12",
            pane_id="%42",
            pane_pid=4242,
            pane_owner_nonce=kwargs["pane_owner_nonce"],
        )

    def fake_tmux(args: list[str], *, socket_path: str | None = None):
        argv_trace.append(list(args))
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _s: True)
    monkeypatch.setattr(
        "omg_cli.team.tmux.spawn_worker_same_window", fake_spawn
    )
    monkeypatch.setattr("omg_cli.team.tmux._tmux_run", fake_tmux)

    record = {
        "task_id": "scale-2",
        "window_index": 2,
        "window_nonce": "a" * 32,
        "worktree": str(tmp_path / "wt"),
        "pane_command": "run-me",
        "_env_pairs": [],
    }
    scaling._add_tmux_windows(
        session="omg-workers",
        records=[record],
        session_owned=False,
        window_id="@12",
        view_mode=VIEW_MODE_SAME_WINDOW,
        leader_pane_id="%3",
        split_target_pane_id="%8",
        expected_server={
            "tmux_socket_path": "/tmp/tmux-sock",
            "tmux_server_pid": 99,
            "tmux_server_pid_start": "proc:99",
        },
        expected_session_id="$7",
        launch_nonce="a" * 32,
        meta={
            "topology": "split",
            "view_mode": VIEW_MODE_SAME_WINDOW,
            "window_id": "@12",
            "session_id": "$7",
            "launch_nonce": "a" * 32,
        },
    )
    assert calls == ["spawn_same_window"]
    assert record["pane_id"] == "%42"
    assert record["window_id"] == "@12"
    assert record["pid"] == 4242
    assert not any(a and a[0] == "new-window" for a in argv_trace)


def test_relaunch_spawn_uses_topology_detached_owner_nonce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#102: relaunch consumes placement + detached spawn + attempt++."""
    from omg_cli.team import scaling
    from omg_cli.team.tmux import SpawnedWorkerPane
    from omg_cli.team.topology import PlacementTarget

    seen: dict[str, Any] = {}

    def fake_spawn(**kwargs: Any) -> SpawnedWorkerPane:
        seen.update(kwargs)
        return SpawnedWorkerPane(
            session_id="$7",
            window_id="@12",
            pane_id="%99",
            pane_pid=9999,
            pane_owner_nonce=kwargs["pane_owner_nonce"],
        )

    monkeypatch.setattr(
        "omg_cli.team.tmux.spawn_worker_same_window", fake_spawn
    )
    placement = PlacementTarget(
        session_id="$7",
        window_id="@12",
        leader_pane_id="%3",
        split_target_pane_id="%8",
        strategy="right_stack",
        horizontal_first=False,
    )
    pane = scaling._spawn_relaunch_worker_via_topology(
        mode=VIEW_MODE_SAME_WINDOW,
        placement=placement,
        worktree=str(tmp_path),
        pane_command="sleep 1",
        env_pairs=[],
        expected_server={
            "tmux_socket_path": "/tmp/tmux-sock",
            "tmux_server_pid": 99,
            "tmux_server_pid_start": "proc:99",
        },
        expected_session_id="$7",
        pane_owner_nonce="d" * 32,
        launch_nonce="a" * 32,
    )
    assert pane == "%99"
    assert seen["pane_owner_nonce"] == "d" * 32
    assert seen["team_window_id"] == "@12"
    assert seen["horizontal"] is False
    assert seen.get("leader_pane_id") == "%3"


def test_add_tmux_windows_dedicated_never_new_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.team import scaling
    from omg_cli.team.tmux import SpawnedWorkerPane

    def fake_spawn(**kwargs: Any) -> SpawnedWorkerPane:
        assert kwargs["team_window_id"] == "@99"
        return SpawnedWorkerPane(
            session_id="$7",
            window_id="@99",
            pane_id="%50",
            pane_pid=5000,
            pane_owner_nonce=kwargs["pane_owner_nonce"],
        )

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _s: True)
    monkeypatch.setattr(
        "omg_cli.team.tmux.spawn_worker_dedicated_window", fake_spawn
    )
    record = {
        "task_id": "scale-3",
        "window_index": 3,
        "window_nonce": "b" * 32,
        "worktree": str(tmp_path / "wt"),
        "pane_command": "run",
        "_env_pairs": [],
    }
    scaling._add_tmux_windows(
        session="omg-workers",
        records=[record],
        session_owned=False,
        window_id="@99",
        view_mode=VIEW_MODE_DEDICATED_WINDOW,
        expected_server={
            "tmux_socket_path": "/tmp/tmux-sock",
            "tmux_server_pid": 99,
            "tmux_server_pid_start": "proc:99",
        },
        expected_session_id="$7",
        launch_nonce="c" * 32,
    )
    assert record["pane_id"] == "%50"
    assert record["window_id"] == "@99"


def test_scale_wal_request_binds_view_mode_and_window() -> None:
    from omg_cli.team import scaling

    meta = {
        "topology": "split",
        "view_mode": VIEW_MODE_SAME_WINDOW,
        "session": "omg-workers",
        "session_id": "$7",
        "launch_nonce": "a" * 32,
        "window_id": "@12",
        "leader_pane_id": "%3",
        "identity_generation": 0,
        "identity_receipt_sha256": "b" * 64,
        "goal": "g",
    }
    payload = scaling._scale_request_payload(
        meta=meta,
        active=[],
        task_specs=[{"task_id": "t1", "prompt": "p", "owned_files": ["a.py"]}],
        start_index=0,
        yolo=False,
        safe=False,
        extra=None,
    )
    assert payload["view_mode"] == VIEW_MODE_SAME_WINDOW
    assert payload["team_window_id"] == "@12"
    assert payload["leader_pane_id"] == "%3"
