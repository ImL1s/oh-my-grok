from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.team.plane import (
    TeamError,
    create_native_team,
    load_native_team,
    prepare_native_spawn,
    reconcile_native_spawn,
    record_native_result,
)
from omg_cli.team.recovery import (
    RecoveryError,
    acquire_supervisor,
    recover_native_task,
    release_supervisor,
    supervisor_poll,
)


T0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
STAMP = "2026-07-22T00:00:00Z"
EXPIRY = "2099-01-01T00:00:00Z"


def _create(root: Path) -> None:
    create_native_team(
        root,
        run_id="run-recovery",
        team_id="team-recovery",
        leader_id="leader",
        parent_session_id="session-parent",
        base_sha="a" * 40,
        created_at=STAMP,
        tasks=[
            {
                "task_id": "task-1",
                "role": "verifier",
                "prompt": "verify the bounded slice",
            }
        ],
    )


def _prepare(root: Path) -> dict:
    return prepare_native_spawn(
        root,
        run_id="run-recovery",
        team_id="team-recovery",
        task_id="task-1",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="verify slice",
        expires_at=EXPIRY,
    )


def _inventory(prepared: dict) -> list[dict]:
    pair = prepared["receipt_pair"]
    return [
        {
            "spawn_receipt_hash": pair["spawn_receipt_hash"],
            "role_receipt_hash": pair["role_receipt_hash"],
            "run_id": "run-recovery",
            "task_id": "task-1",
            "parent_id": "leader",
            "host_spawn_id": "host-child-1",
            "observed_session_id": "child-session-1",
        }
    ]


def test_supervisor_takeover_is_generation_fenced(tmp_path: Path) -> None:
    first = acquire_supervisor(
        tmp_path,
        run_id="run-recovery",
        team_id="team-recovery",
        owner_id="owner-a",
        process_start_identity="proc-a",
        now=T0,
        timeout_seconds=60,
    )
    assert (
        acquire_supervisor(
            tmp_path,
            run_id="run-recovery",
            team_id="team-recovery",
            owner_id="owner-a",
            process_start_identity="proc-a",
            now=T0 + timedelta(seconds=10),
            timeout_seconds=60,
        )
        == first
    )
    with pytest.raises(RecoveryError, match="healthy"):
        acquire_supervisor(
            tmp_path,
            run_id="run-recovery",
            team_id="team-recovery",
            owner_id="owner-b",
            process_start_identity="proc-b",
            now=T0 + timedelta(seconds=30),
            timeout_seconds=60,
        )
    adopted = acquire_supervisor(
        tmp_path,
        run_id="run-recovery",
        team_id="team-recovery",
        owner_id="owner-b",
        process_start_identity="proc-b",
        now=T0 + timedelta(seconds=61),
        timeout_seconds=60,
    )
    assert adopted["generation"] == 1
    polled = supervisor_poll(
        tmp_path,
        run_id="run-recovery",
        team_id="team-recovery",
        owner_id="owner-b",
        process_start_identity="proc-b",
        generation=1,
        expected_sequence=0,
        now=T0 + timedelta(seconds=62),
    )
    assert polled["poll_sequence"] == 1
    assert (
        release_supervisor(
            tmp_path,
            run_id="run-recovery",
            team_id="team-recovery",
            owner_id="owner-b",
            process_start_identity="proc-b",
            generation=1,
        )["released"]
        is True
    )


def test_dead_worker_recovers_at_generation_plus_one_and_stale_result_rejects(
    tmp_path: Path,
) -> None:
    _create(tmp_path)
    prepared = _prepare(tmp_path)
    reconcile_native_spawn(
        tmp_path,
        run_id="run-recovery",
        team_id="team-recovery",
        task_id="task-1",
        inventory=_inventory(prepared),
        expected_state="spawn_requested",
        expected_sequence=1,
        expected_generation=0,
        now=T0,
    )
    recovered = recover_native_task(
        tmp_path,
        run_id="run-recovery",
        team_id="team-recovery",
        task_id="task-1",
        expected_state="running",
        expected_sequence=2,
        expected_generation=0,
        now=T0 + timedelta(seconds=400),
    )
    assert (recovered["state"], recovered["sequence"], recovered["generation"]) == (
        "ready",
        3,
        1,
    )
    binding = _inventory(prepared)[0]
    with pytest.raises(TeamError, match="fence mismatch"):
        record_native_result(
            tmp_path,
            result={
                "store_kind": "native_worker_result",
                "schema_version": 1,
                "transport": "grok_native",
                "run_id": "run-recovery",
                "team_id": "team-recovery",
                "task_id": "task-1",
                "generation": 0,
                "host_spawn_id": binding["host_spawn_id"],
                "observed_session_id": binding["observed_session_id"],
                "spawn_receipt_hash": binding["spawn_receipt_hash"],
                "role_receipt_hash": binding["role_receipt_hash"],
                "expected_state": "running",
                "expected_sequence": 2,
                "replay_id": "result-old",
                "status": "ok",
                "artifact": {"kind": "team-result"},
                "verification_evidence": [],
                "completed_at": STAMP,
            },
        )


def test_launch_unknown_requires_explicit_reconciliation_before_retry(
    tmp_path: Path,
) -> None:
    _create(tmp_path)
    _prepare(tmp_path)
    unknown = reconcile_native_spawn(
        tmp_path,
        run_id="run-recovery",
        team_id="team-recovery",
        task_id="task-1",
        inventory=[],
        expected_state="spawn_requested",
        expected_sequence=1,
        expected_generation=0,
        now=T0,
    )
    assert unknown["task"]["state"] == "launch_unknown"
    with pytest.raises(RecoveryError, match="requires host absence"):
        recover_native_task(
            tmp_path,
            run_id="run-recovery",
            team_id="team-recovery",
            task_id="task-1",
            expected_state="launch_unknown",
            expected_sequence=2,
            expected_generation=0,
        )
    recovered = recover_native_task(
        tmp_path,
        run_id="run-recovery",
        team_id="team-recovery",
        task_id="task-1",
        expected_state="launch_unknown",
        expected_sequence=2,
        expected_generation=0,
        force_launch_unknown=True,
    )
    assert recovered["state"] == "ready"
    assert (
        load_native_team(tmp_path, "run-recovery", "team-recovery")["tasks"]["task-1"][
            "generation"
        ]
        == 1
    )
