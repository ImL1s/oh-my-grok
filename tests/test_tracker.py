from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from omg_cli.contracts.tracker_contract import make_role_receipt
from omg_cli.runtime_events import normalize_lifecycle_event
from omg_cli.tracker import (
    TrackerLeaseBusy,
    acquire_tracker_lease,
    load_tracker_projection,
    load_spawn_receipt_pair,
    persist_spawn_receipt_pair,
    project_lifecycle_events,
    reconcile_native_inventory,
    reconcile_spawn_observation,
)


def _event(sequence: int, event_type: str, *, event_id: str | None = None):
    return normalize_lifecycle_event(
        source="grok-native",
        source_cursor=f"cursor-{sequence}",
        source_sequence=sequence,
        event_id=event_id or f"event-{sequence}",
        event_type=event_type,
        run_id="run-1",
        session_id="session-1",
        observed_at=f"2026-07-22T00:00:{sequence:02d}Z",
        payload={"host_spawn_id": "spawn-1"},
    )


def test_projector_unions_out_of_order_sources_dedupes_and_never_reopens_closed(tmp_path) -> None:
    rows = [_event(2, "agent_closed"), _event(0, "spawn_requested"), _event(1, "session_started")]
    projected = project_lifecycle_events(tmp_path, run_id="run-1", generation=1, events=rows + [rows[1]])
    assert projected["event_count"] == 3
    assert projected["sessions"]["session-1"]["state"] == "closed"
    project_lifecycle_events(
        tmp_path,
        run_id="run-1",
        generation=1,
        events=[_event(3, "turn_started")],
    )
    assert load_tracker_projection(tmp_path, "run-1")["sessions"]["session-1"]["state"] == "closed"


def test_native_and_fallback_sources_form_one_logical_event_union(tmp_path) -> None:
    native = _event(0, "session_started", event_id="native-session-start")
    fallback = normalize_lifecycle_event(
        source="grok-fallback",
        source_cursor="fallback-0",
        source_sequence=0,
        event_id="native-session-start",
        event_type="session_started",
        run_id="run-1",
        session_id="session-1",
        observed_at="2026-07-22T00:00:01Z",
        payload={"host_spawn_id": "spawn-1"},
    )
    projected = project_lifecycle_events(
        tmp_path,
        run_id="run-1",
        generation=1,
        events=[fallback, native],
    )
    assert projected["event_count"] == 1
    assert set(projected["cursors"]) == {"grok-native", "grok-fallback"}


def test_stalled_primary_takeover_is_single_winner_and_hud_lease_is_separate(tmp_path) -> None:
    acquire_tracker_lease(
        tmp_path,
        run_id="run-1",
        kind="primary",
        pid=100,
        process_start_identity="old-start",
        owner_token="old-owner",
        generation=1,
        cursor="c1",
        now=datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
    )

    def contend(index: int):
        try:
            return acquire_tracker_lease(
                tmp_path,
                run_id="run-1",
                kind="primary",
                pid=200 + index,
                process_start_identity=f"new-{index}",
                owner_token=f"owner-{index}",
                generation=2,
                cursor="c1",
                now=datetime(2026, 7, 22, 0, 10, tzinfo=timezone.utc),
                stale_after_seconds=60,
                process_identity_matches=lambda _pid, _start: True,
            )
        except TrackerLeaseBusy:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [row for row in pool.map(contend, range(2)) if row]
    assert len(winners) == 1
    primary = winners[0]
    hud = acquire_tracker_lease(
        tmp_path,
        run_id="run-1",
        kind="hud",
        pid=300,
        process_start_identity="hud-start",
        owner_token="hud-owner",
        generation=2,
        cursor="hud-cursor",
        now=datetime(2026, 7, 22, 0, 10, tzinfo=timezone.utc),
    )
    assert hud["owner_token"] == "hud-owner"
    assert primary["owner_token"] != hud["owner_token"]


def test_reconciliation_reports_missing_child_until_projected(tmp_path) -> None:
    project_lifecycle_events(tmp_path, run_id="run-1", generation=1, events=[])
    result = reconcile_native_inventory(
        tmp_path,
        run_id="run-1",
        inventory=[{"host_spawn_id": "missing-child", "session_id": "session-x"}],
    )
    assert result["strict_ok"] is False
    assert result["diagnostics"][0]["code"] == "E_TRACKER_MISSING_CHILD"


def _spawn_receipt(receipt_id: str) -> dict:
    return {
        "store_kind": "spawn_receipt",
        "schema_version": 1,
        "receipt_id": receipt_id,
        "run_id": "run-1",
        "team_id": "team-1",
        "task_id": f"task-{receipt_id}",
        "parent_id": "parent-1",
        "parent_session_id": "session-1",
        "requested_role": "omg-executor",
        "capability_mode": "read-write",
        "depth": 1,
        "attempt": 1,
        "receipt_generation": 3,
        "lease_generation": 4,
        "dispatch_nonce": f"nonce-{receipt_id}",
        "expires_at": "2026-07-22T00:05:00Z",
        "expected_state": "spawn-requested",
        "expected_sequence": 7,
    }


def _inventory_row(pair: dict, spawn: dict, host_id: str) -> dict:
    return {
        "spawn_receipt_hash": pair["spawn_receipt_hash"],
        "role_receipt_hash": pair["role_receipt_hash"],
        "run_id": spawn["run_id"],
        "task_id": spawn["task_id"],
        "parent_id": spawn["parent_id"],
        "host_spawn_id": host_id,
        "observed_session_id": f"session-{host_id}",
    }


def test_spawn_receipts_persist_before_launch_and_reconcile_zero_one_many(tmp_path) -> None:
    now = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    spawn = _spawn_receipt("track-receipt-1")
    pair = persist_spawn_receipt_pair(
        tmp_path,
        spawn_receipt=spawn,
        role_receipt=make_role_receipt(spawn),
        now=now,
    )
    assert load_spawn_receipt_pair(
        tmp_path, run_id="run-1", receipt_id=spawn["receipt_id"]
    ) == pair
    unknown = reconcile_spawn_observation(
        tmp_path,
        run_id="run-1",
        receipt_id=spawn["receipt_id"],
        inventory=[],
        expected_generation=3,
        now=datetime(2026, 7, 22, 0, 1, tzinfo=timezone.utc),
    )
    assert unknown == {"outcome": "launch_unknown", "matches": 0, "retry_allowed": False}

    bound = reconcile_spawn_observation(
        tmp_path,
        run_id="run-1",
        receipt_id=spawn["receipt_id"],
        inventory=[_inventory_row(pair, spawn, "child-track-1")],
        expected_generation=3,
        now=datetime(2026, 7, 22, 0, 2, tzinfo=timezone.utc),
    )
    assert bound["outcome"] == "bound"
    assert bound["binding"]["identity_truth"] == "grok_native_receipts"

    other = _spawn_receipt("track-receipt-2")
    other_pair = persist_spawn_receipt_pair(
        tmp_path,
        spawn_receipt=other,
        role_receipt=make_role_receipt(other),
        now=now,
    )
    blocked = reconcile_spawn_observation(
        tmp_path,
        run_id="run-1",
        receipt_id=other["receipt_id"],
        inventory=[
            _inventory_row(other_pair, other, "child-track-2a"),
            _inventory_row(other_pair, other, "child-track-2b"),
        ],
        expected_generation=3,
        now=datetime(2026, 7, 22, 0, 2, tzinfo=timezone.utc),
    )
    assert blocked == {"outcome": "blocked", "matches": 2, "retry_allowed": False}
