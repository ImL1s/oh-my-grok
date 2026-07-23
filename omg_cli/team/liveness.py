"""Generation-fenced worker liveness with heartbeat/progress separation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_integer,
    require_iso8601,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


CLI_WRITER = "omg-cli"
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 90
DEFAULT_CLAIM_LEASE_SECONDS = 300


class LivenessError(RuntimeError):
    """Liveness input was stale, ambiguous, or not bound to the worker."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    require_iso8601(value, label="timestamp")
    return datetime.fromisoformat(
        value[:-1] + "+00:00" if value.endswith("Z") else value
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def liveness_path(root: Path | str, run_id: str, team_id: str, task_id: str) -> Path:
    for label, value in (
        ("run_id", run_id),
        ("team_id", team_id),
        ("task_id", task_id),
    ):
        require_safe_id(value, label=label)
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
        / "liveness"
        / (safe_path_key(task_id, namespace="task") + ".json")
    )


def _validate(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "task_id",
        "worker_id",
        "generation",
        "heartbeat_sequence",
        "progress_sequence",
        "heartbeat_at",
        "progress_at",
        "claim_started_at",
        "claim_expires_at",
        "last_progress_hash",
        "terminal",
    }
    if set(row) != required:
        raise ContractValidationError("worker liveness keys mismatch")
    if (
        row["store_kind"] != "worker_liveness"
        or row["schema_version"] != 1
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("worker liveness header mismatch")
    for field in ("run_id", "team_id", "task_id", "worker_id"):
        require_safe_id(row[field], label=field)
    for field in ("generation", "heartbeat_sequence", "progress_sequence"):
        require_integer(row[field], label=field, minimum=0)
    for field in ("heartbeat_at", "claim_started_at", "claim_expires_at"):
        require_iso8601(row[field], label=field)
    if row["progress_at"] is not None:
        require_iso8601(row["progress_at"], label="progress_at")
    if row["last_progress_hash"] is not None:
        require_sha256(row["last_progress_hash"], label="last_progress_hash")
    if not isinstance(row["terminal"], bool):
        raise ContractValidationError("worker liveness terminal must be boolean")
    if _parse(row["claim_expires_at"]) < _parse(row["claim_started_at"]):
        raise ContractValidationError("worker liveness claim expiry precedes start")
    progress_fields_present = (
        row["progress_at"] is not None,
        row["last_progress_hash"] is not None,
        row["progress_sequence"] > 0,
    )
    if len(set(progress_fields_present)) != 1:
        raise ContractValidationError("worker liveness progress identity is partial")
    if _parse(row["heartbeat_at"]) < _parse(row["claim_started_at"]):
        raise ContractValidationError("worker liveness heartbeat precedes claim start")
    if row["progress_at"] is not None:
        if _parse(row["progress_at"]) < _parse(row["claim_started_at"]):
            raise ContractValidationError(
                "worker liveness progress precedes claim start"
            )
        if _parse(row["claim_expires_at"]) < _parse(row["progress_at"]):
            raise ContractValidationError(
                "worker liveness claim expiry precedes progress"
            )
    return row


def initialize_liveness(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    worker_id: str,
    generation: int,
    now: datetime | None = None,
    claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
) -> dict[str, Any]:
    """Create a claim.  Same bytes adopt; a different active identity rejects."""

    require_integer(generation, label="generation", minimum=0)
    require_safe_id(worker_id, label="worker_id")
    if isinstance(claim_lease_seconds, bool) or not isinstance(
        claim_lease_seconds, int
    ):
        raise LivenessError("claim lease seconds must be an integer")
    if not 1 <= claim_lease_seconds <= 86_400:
        raise LivenessError("claim lease seconds out of bounds")
    current = now or _utc_now()
    candidate = _validate(
        {
            "store_kind": "worker_liveness",
            "schema_version": 1,
            "writer": CLI_WRITER,
            "run_id": run_id,
            "team_id": team_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "generation": generation,
            "heartbeat_sequence": 0,
            "progress_sequence": 0,
            "heartbeat_at": _iso(current),
            "progress_at": None,
            "claim_started_at": _iso(current),
            "claim_expires_at": _iso(current + timedelta(seconds=claim_lease_seconds)),
            "last_progress_hash": None,
            "terminal": False,
        }
    )
    path = liveness_path(root, run_id, team_id, task_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            parsed = parse_canonical_json_bytes(path.read_bytes())
            if not isinstance(parsed, dict):
                raise ContractValidationError("worker liveness must be an object")
            existing = _validate(parsed)
            if (
                existing["generation"] == generation
                and existing["worker_id"] == worker_id
                and not existing["terminal"]
            ):
                # Idempotent adoption after a crash between liveness creation
                # and the team-plane CAS.  Timestamps are not identity.
                return existing
            if existing["generation"] >= generation:
                raise LivenessError("same/newer worker liveness already exists")
            if not existing["terminal"]:
                raise LivenessError(
                    "older worker must be terminal before generation takeover"
                )
        atomic_write_bytes(
            path, canonical_json_bytes(candidate), mode=DATA_FILE_MODE, replace=True
        )
    return candidate


def load_liveness(
    root: Path | str, *, run_id: str, team_id: str, task_id: str
) -> dict[str, Any] | None:
    path = liveness_path(root, run_id, team_id, task_id)
    if not path.exists():
        return None
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("worker liveness must be an object")
    return _validate(parsed)


def _mutate(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    worker_id: str,
    generation: int,
    mutate: Any,
) -> dict[str, Any]:
    path = liveness_path(root, run_id, team_id, task_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        if not path.exists():
            raise LivenessError("worker liveness claim is missing")
        parsed = parse_canonical_json_bytes(path.read_bytes())
        if not isinstance(parsed, dict):
            raise ContractValidationError("worker liveness must be an object")
        current = _validate(parsed)
        if current["worker_id"] != worker_id or current["generation"] != generation:
            raise LivenessError("stale worker identity or generation")
        if current["terminal"]:
            raise LivenessError("terminal worker may not emit liveness")
        updated = _validate(mutate(dict(current)))
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
        return updated


def record_heartbeat(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    worker_id: str,
    generation: int,
    expected_sequence: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record presence only; the claim expiry is deliberately unchanged."""

    current_time = now or _utc_now()

    def apply(row: dict[str, Any]) -> dict[str, Any]:
        if row["heartbeat_sequence"] != expected_sequence:
            raise LivenessError("heartbeat sequence CAS mismatch")
        if _aware(current_time) < _parse(row["heartbeat_at"]):
            raise LivenessError("heartbeat timestamp moved backwards")
        return {
            **row,
            "heartbeat_sequence": expected_sequence + 1,
            "heartbeat_at": _iso(current_time),
        }

    return _mutate(
        root,
        run_id=run_id,
        team_id=team_id,
        task_id=task_id,
        worker_id=worker_id,
        generation=generation,
        mutate=apply,
    )


def record_progress(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    worker_id: str,
    generation: int,
    expected_sequence: int,
    evidence_sha256: str,
    now: datetime | None = None,
    claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
) -> dict[str, Any]:
    """Record substantive progress and renew the claim lease."""

    require_sha256(evidence_sha256, label="evidence_sha256")
    if isinstance(claim_lease_seconds, bool) or not isinstance(
        claim_lease_seconds, int
    ):
        raise LivenessError("claim lease seconds must be an integer")
    if not 1 <= claim_lease_seconds <= 86_400:
        raise LivenessError("claim lease seconds out of bounds")
    current_time = now or _utc_now()

    def apply(row: dict[str, Any]) -> dict[str, Any]:
        if row["progress_sequence"] != expected_sequence:
            raise LivenessError("progress sequence CAS mismatch")
        if row["last_progress_hash"] == evidence_sha256:
            raise LivenessError("progress evidence replay does not renew a claim")
        prior_time = (
            _parse(row["progress_at"])
            if row["progress_at"] is not None
            else _parse(row["claim_started_at"])
        )
        if _aware(current_time) < prior_time:
            raise LivenessError("progress timestamp moved backwards")
        return {
            **row,
            "progress_sequence": expected_sequence + 1,
            "progress_at": _iso(current_time),
            "claim_expires_at": _iso(
                current_time + timedelta(seconds=claim_lease_seconds)
            ),
            "last_progress_hash": evidence_sha256,
        }

    return _mutate(
        root,
        run_id=run_id,
        team_id=team_id,
        task_id=task_id,
        worker_id=worker_id,
        generation=generation,
        mutate=apply,
    )


def mark_terminal(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    worker_id: str,
    generation: int,
) -> dict[str, Any]:
    return _mutate(
        root,
        run_id=run_id,
        team_id=team_id,
        task_id=task_id,
        worker_id=worker_id,
        generation=generation,
        mutate=lambda row: {**row, "terminal": True},
    )


def classify_liveness(
    value: Mapping[str, Any],
    *,
    now: datetime | None = None,
    heartbeat_timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
) -> str:
    """Return ``terminal``, ``live``, ``stalled`` or ``dead``.

    ``stalled`` means recent heartbeats exist but the substantive claim lease
    expired.  ``dead`` means both heartbeat and claim evidence are stale.
    """

    row = _validate(value)
    if row["terminal"]:
        return "terminal"
    if not 1 <= heartbeat_timeout_seconds <= 86_400:
        raise LivenessError("heartbeat timeout seconds out of bounds")
    current = _aware(now or _utc_now())
    heartbeat_fresh = current <= (
        _parse(row["heartbeat_at"]) + timedelta(seconds=heartbeat_timeout_seconds)
    )
    claim_fresh = current <= _parse(row["claim_expires_at"])
    if heartbeat_fresh and claim_fresh:
        return "live"
    if heartbeat_fresh and not claim_fresh:
        return "stalled"
    return "dead"


__all__ = [
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS",
    "LivenessError",
    "classify_liveness",
    "initialize_liveness",
    "liveness_path",
    "load_liveness",
    "mark_terminal",
    "record_heartbeat",
    "record_progress",
]
