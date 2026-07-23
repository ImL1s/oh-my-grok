from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.team.liveness import (
    LivenessError,
    classify_liveness,
    initialize_liveness,
    load_liveness,
    mark_terminal,
    record_heartbeat,
    record_progress,
)


T0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
IDENTITY = {
    "run_id": "run-live",
    "team_id": "team-live",
    "task_id": "task-live",
    "worker_id": "host-child-1",
    "generation": 0,
}


def test_heartbeat_never_renews_claim_but_progress_does(tmp_path: Path) -> None:
    initial = initialize_liveness(tmp_path, **IDENTITY, now=T0, claim_lease_seconds=10)
    heartbeat = record_heartbeat(
        tmp_path,
        **IDENTITY,
        expected_sequence=0,
        now=T0 + timedelta(seconds=11),
    )
    assert heartbeat["claim_expires_at"] == initial["claim_expires_at"]
    assert (
        classify_liveness(
            heartbeat,
            now=T0 + timedelta(seconds=11),
            heartbeat_timeout_seconds=30,
        )
        == "stalled"
    )

    progress = record_progress(
        tmp_path,
        **IDENTITY,
        expected_sequence=0,
        evidence_sha256="a" * 64,
        now=T0 + timedelta(seconds=12),
        claim_lease_seconds=20,
    )
    assert progress["progress_sequence"] == 1
    assert progress["claim_expires_at"].startswith("2026-07-22T00:00:32")
    assert classify_liveness(progress, now=T0 + timedelta(seconds=20)) == "live"


def test_liveness_rejects_replay_stale_generation_and_terminal_emission(
    tmp_path: Path,
) -> None:
    initialize_liveness(tmp_path, **IDENTITY, now=T0)
    record_progress(
        tmp_path,
        **IDENTITY,
        expected_sequence=0,
        evidence_sha256="b" * 64,
        now=T0 + timedelta(seconds=1),
    )
    with pytest.raises(LivenessError, match="CAS mismatch"):
        record_progress(
            tmp_path,
            **IDENTITY,
            expected_sequence=0,
            evidence_sha256="c" * 64,
        )
    with pytest.raises(LivenessError, match="replay"):
        record_progress(
            tmp_path,
            **IDENTITY,
            expected_sequence=1,
            evidence_sha256="b" * 64,
        )
    with pytest.raises(LivenessError, match="stale"):
        record_heartbeat(
            tmp_path,
            **{**IDENTITY, "generation": 1},
            expected_sequence=0,
        )
    mark_terminal(tmp_path, **IDENTITY)
    assert (
        classify_liveness(
            load_liveness(
                tmp_path, **{k: IDENTITY[k] for k in ("run_id", "team_id", "task_id")}
            )
            or {}
        )
        == "terminal"
    )
    with pytest.raises(LivenessError, match="terminal"):
        record_heartbeat(tmp_path, **IDENTITY, expected_sequence=0)


def test_generation_takeover_requires_terminal_old_claim(tmp_path: Path) -> None:
    first = initialize_liveness(tmp_path, **IDENTITY, now=T0)
    adopted = initialize_liveness(tmp_path, **IDENTITY, now=T0 + timedelta(seconds=30))
    assert adopted == first
    with pytest.raises(LivenessError, match="older worker must be terminal"):
        initialize_liveness(
            tmp_path,
            **{**IDENTITY, "worker_id": "host-child-2", "generation": 1},
            now=T0 + timedelta(seconds=31),
        )
    mark_terminal(tmp_path, **IDENTITY)
    second = initialize_liveness(
        tmp_path,
        **{**IDENTITY, "worker_id": "host-child-2", "generation": 1},
        now=T0 + timedelta(seconds=32),
    )
    assert second["generation"] == 1
    assert (
        classify_liveness(
            second,
            now=T0 + timedelta(seconds=500),
            heartbeat_timeout_seconds=30,
        )
        == "dead"
    )


def test_liveness_rejects_backdated_events_and_boolean_lease(tmp_path: Path) -> None:
    initialize_liveness(tmp_path, **IDENTITY, now=T0)
    record_heartbeat(
        tmp_path,
        **IDENTITY,
        expected_sequence=0,
        now=T0 + timedelta(seconds=2),
    )
    with pytest.raises(LivenessError, match="backwards"):
        record_heartbeat(
            tmp_path,
            **IDENTITY,
            expected_sequence=1,
            now=T0 + timedelta(seconds=1),
        )
    with pytest.raises(LivenessError, match="integer"):
        record_progress(
            tmp_path,
            **IDENTITY,
            expected_sequence=0,
            evidence_sha256="d" * 64,
            claim_lease_seconds=True,
        )
