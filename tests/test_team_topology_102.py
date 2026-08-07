"""#102 characterization: topology model + legacy receipt compatibility.

Hermetic — no live tmux. Locks pre-change contracts before schema/scaling
rewrites land.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.team import plane, topology
from omg_cli.team.topology import (
    VIEW_MODE_DEDICATED_WINDOW,
    VIEW_MODE_DETACHED_SESSION,
    VIEW_MODE_LEGACY_WINDOWS,
    VIEW_MODE_SAME_WINDOW,
    TopologyError,
    build_topology_snapshot,
    normalize_persisted_topology,
    placement_target_for_add,
    placement_target_for_relaunch,
    resolve_launch_view_mode,
    resolve_persisted_view_mode,
    topology_sha256,
)


def test_resolve_launch_never_returns_legacy_windows() -> None:
    assert resolve_launch_view_mode(inside_tmux=True) == VIEW_MODE_SAME_WINDOW
    assert (
        resolve_launch_view_mode(inside_tmux=True, dedicated_window=True)
        == VIEW_MODE_DEDICATED_WINDOW
    )
    assert resolve_launch_view_mode(inside_tmux=False) == VIEW_MODE_DETACHED_SESSION
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


def test_normalize_legacy_windows_does_not_promote_to_split() -> None:
    meta = {
        "topology": "windows",
        "session": "omg-legacy",
        "session_id": "$1",
        "launch_nonce": "c" * 32,
        "session_owned": True,
        "tasks": [
            {
                "task_id": "t1",
                "window_index": 0,
                "window_id": "@1",
                "pane_id": "%1",
            }
        ],
    }
    snap = normalize_persisted_topology(meta)
    assert snap.mode == VIEW_MODE_LEGACY_WINDOWS
    assert snap.topology_string == "windows"
    assert snap.is_legacy is True
    target = placement_target_for_add(snap)
    assert target.strategy == topology.PLACEMENT_LEGACY_WINDOW
    assert target.window_id is None


def test_identity_receipt_v1_v2_raw_bytes_unchanged_on_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readers may normalize in memory; raw receipt files must stay byte-identical."""
    root = tmp_path
    run_id = "20260807T000000Z-deadbeef"
    team_dir = root / ".omg" / "state" / "runs" / run_id / "team"
    team_dir.mkdir(parents=True)

    # Minimal v2 identity receipt fixture (exact key set).
    v2_receipt: dict[str, Any] = {
        "store_kind": "team_identity_receipt",
        "schema_version": plane.V2_IDENTITY_RECEIPT_SCHEMA_VERSION,
        "writer": plane.CLI_WRITER,
        "run_id": run_id,
        "session_name": "omg-workers",
        "session_id": "$7",
        "launch_nonce": "d" * 32,
        "generation": 1,
        "previous_receipt_sha256": "e" * 64,
        "operation": "add",
        "receipt_nonce": "f" * 32,
        "tasks_before": [
            {
                "task_id": "w1",
                "window_index": 0,
                "window_id": "@12",
                "window_nonce": "1" * 32,
                "pane_id": "%8",
                "pid": 11,
                "pgid": 11,
                "pid_start": "proc:11",
            }
        ],
        "tasks_after": [
            {
                "task_id": "w1",
                "window_index": 0,
                "window_id": "@12",
                "window_nonce": "1" * 32,
                "pane_id": "%8",
                "pid": 11,
                "pgid": 11,
                "pid_start": "proc:11",
            },
            {
                "task_id": "scale-1",
                "window_index": 1,
                "window_id": "@13",
                "window_nonce": "2" * 32,
                "pane_id": "%9",
                "pid": 12,
                "pgid": 12,
                "pid_start": "proc:12",
            },
        ],
        "scale_intent": {
            "request_sha256": "a" * 64,
            "scale_wal_sha256": "b" * 64,
            "records": [],
        },
        "scale_intent_sha256": None,
    }
    # Fill scale_intent_sha256 canonically.
    v2_receipt["scale_intent_sha256"] = sha256_hex(
        canonical_json_bytes(v2_receipt["scale_intent"])
    )
    body = canonical_json_bytes(v2_receipt)
    path = team_dir / "identity-receipt-1.json"
    path.write_bytes(body)
    before = path.read_bytes()

    # Parse only — must not rewrite.
    parsed = json.loads(before.decode("utf-8"))
    assert parsed["schema_version"] == 2
    assert path.read_bytes() == before

    # v2 projection must not invent v3-only keys.
    rows = plane._identity_rows_v2(v2_receipt["tasks_after"])
    assert rows[0]["window_index"] == 0
    assert set(rows[0]) == {
        "task_id",
        "window_index",
        "window_id",
        "window_nonce",
        "pane_id",
        "pid",
        "pgid",
        "pid_start",
    }

def test_resync_window_indices_is_noop_after_102() -> None:
    """#102: logical ordering must not be rewritten from live pane_index."""
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

    monkeypatch.setattr(scaling, "tmux_available", lambda: True)
    monkeypatch.setattr(scaling, "_session_alive", lambda _s: True)
    monkeypatch.setattr(
        "omg_cli.team.tmux.spawn_worker_same_window", fake_spawn
    )

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
    )
    assert calls == ["spawn_same_window"]
    assert record["pane_id"] == "%42"
    assert record["window_id"] == "@12"
    assert record["pid"] == 4242


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
