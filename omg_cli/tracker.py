"""Generation-fenced lifecycle projector and distinct tracker leases."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.event_contract import event_identity, validate_lifecycle_event
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
)
from omg_cli.contracts.tracker_contract import validate_projector_lease
from omg_cli.contracts.tracker_contract import (
    bind_native_spawn,
    make_role_receipt,
    validate_spawn_receipt,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)
from omg_cli.redaction import redact_value


TERMINAL_SESSION_STATES = frozenset({"closed", "failed"})
_EVENT_STATE = {
    "spawn_requested": "requested",
    "session_started": "active",
    "turn_started": "active",
    "turn_completed": "active",
    "agent_closed": "closed",
    "agent_failed": "failed",
}


class TrackerError(RuntimeError):
    pass


class TrackerLeaseBusy(TrackerError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    require_iso8601(value, label="timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _tracker_dir(root: Path | str, run_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    key = safe_path_key(run_id, namespace="tracker-run")
    return Path(root).resolve() / ".omg" / "state" / "tracker" / key


def tracker_projection_path(root: Path | str, run_id: str) -> Path:
    return _tracker_dir(root, run_id) / "projection.json"


def _projection_lock(root: Path | str, run_id: str) -> Path:
    return _tracker_dir(root, run_id) / "projection.lock"


def _empty_projection(run_id: str, generation: int) -> dict[str, Any]:
    return {
        "store_kind": "tracker_projection",
        "schema_version": 1,
        "run_id": run_id,
        "generation": generation,
        "revision": 0,
        "event_count": 0,
        "events": [],
        "event_hashes": {},
        "cursors": {},
        "sessions": {},
        "diagnostics": [],
        "updated_at": "1970-01-01T00:00:00Z",
    }


def _validate_projection(value: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    projection = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "run_id",
        "generation",
        "revision",
        "event_count",
        "events",
        "event_hashes",
        "cursors",
        "sessions",
        "diagnostics",
        "updated_at",
    }
    if set(projection) != required:
        raise ContractValidationError("tracker projection keys mismatch")
    if (
        projection["store_kind"] != "tracker_projection"
        or projection["schema_version"] != 1
        or projection["run_id"] != run_id
    ):
        raise ContractValidationError("tracker projection header mismatch")
    for name in ("generation", "revision", "event_count"):
        require_integer(projection[name], label=name, minimum=0)
    if not isinstance(projection["events"], list):
        raise ContractValidationError("tracker events must be an array")
    if not all(isinstance(projection[name], dict) for name in ("event_hashes", "cursors", "sessions")):
        raise ContractValidationError("tracker projection maps are malformed")
    if not isinstance(projection["diagnostics"], list):
        raise ContractValidationError("tracker diagnostics must be an array")
    require_iso8601(projection["updated_at"], label="updated_at")
    if projection["event_count"] != len(projection["events"]):
        raise ContractValidationError("tracker event count mismatch")
    for event in projection["events"]:
        validate_lifecycle_event(event)
    return projection


def load_tracker_projection(root: Path | str, run_id: str) -> dict[str, Any] | None:
    path = tracker_projection_path(root, run_id)
    if not path.exists():
        return None
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("tracker projection must be an object")
    return _validate_projection(parsed, run_id)


def _event_sort_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        event["observed_at"],
        str(event["source"]).encode("utf-8"),
        event["source_sequence"],
        str(event["event_id"]).encode("utf-8"),
    )


def _logical_event_identity(event: Mapping[str, Any]) -> str:
    payload = event["payload"]
    native_id = payload.get("native_event_id") or payload.get("host_event_id") or event["event_id"]
    return sha256_hex(
        canonical_json_bytes(
            [event["run_id"], event["session_id"], event["event_type"], native_id]
        )
    )


def _semantic_event_hash(event: Mapping[str, Any]) -> str:
    return sha256_hex(
        canonical_json_bytes(
            {
                "run_id": event["run_id"],
                "session_id": event["session_id"],
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "payload": event["payload"],
            }
        )
    )


def project_lifecycle_events(
    root: Path | str,
    *,
    run_id: str,
    generation: int,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    require_safe_id(run_id, label="run_id")
    require_integer(generation, label="generation", minimum=0)
    path = tracker_projection_path(root, run_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(_projection_lock(root, run_id)):
        current = load_tracker_projection(root, run_id) or _empty_projection(run_id, generation)
        if generation < current["generation"]:
            raise ContractValidationError("stale tracker generation")
        if generation > current["generation"] + 1:
            raise ContractValidationError("tracker generation skipped")
        candidates = list(current["events"])
        for raw in events:
            event = dict(validate_lifecycle_event(raw))
            if event["run_id"] != run_id:
                raise ContractValidationError("foreign run lifecycle event")
            candidates.append(event)

        physical: dict[str, str] = {}
        source_sequences: dict[tuple[str, int], str] = {}
        logical: dict[str, dict[str, Any]] = {}
        event_hashes: dict[str, str] = {}
        for event in sorted(candidates, key=_event_sort_key):
            identity = event_identity(event)
            digest = sha256_hex(canonical_json_bytes(event))
            prior_bytes = physical.get(identity)
            if prior_bytes is not None and prior_bytes != digest:
                raise ContractValidationError("tracker event identity conflicts with prior bytes")
            physical[identity] = digest
            sequence_key = (event["source"], event["source_sequence"])
            prior_sequence = source_sequences.get(sequence_key)
            if prior_sequence is not None and prior_sequence != identity:
                raise ContractValidationError("tracker source sequence conflicts with prior event")
            source_sequences[sequence_key] = identity

            logical_id = _logical_event_identity(event)
            semantic_hash = _semantic_event_hash(event)
            prior_semantic = event_hashes.get(logical_id)
            if prior_semantic is not None and prior_semantic != semantic_hash:
                raise ContractValidationError("tracker logical event conflicts across sources")
            event_hashes[logical_id] = semantic_hash
            logical.setdefault(logical_id, event)
        combined = sorted(logical.values(), key=_event_sort_key)

        cursors = dict(current["cursors"])
        for event in sorted(candidates, key=_event_sort_key):
            source = event["source"]
            prior_cursor = cursors.get(source)
            if prior_cursor is None or event["source_sequence"] > prior_cursor["source_sequence"]:
                cursors[source] = {
                    "source_sequence": event["source_sequence"],
                    "source_cursor": event["source_cursor"],
                    "event_id": event["event_id"],
                }
        sessions: dict[str, dict[str, Any]] = {}
        for event in combined:
            session_id = event["session_id"]
            session = sessions.setdefault(
                session_id,
                {
                    "state": "unknown",
                    "last_event_id": None,
                    "last_sequence": -1,
                    "host_spawn_ids": [],
                },
            )
            next_state = _EVENT_STATE[event["event_type"]]
            if session["state"] not in TERMINAL_SESSION_STATES:
                session["state"] = next_state
            if event["source_sequence"] >= session["last_sequence"]:
                session["last_sequence"] = event["source_sequence"]
                session["last_event_id"] = event["event_id"]
            host_id = event["payload"].get("host_spawn_id")
            if isinstance(host_id, str) and host_id and host_id not in session["host_spawn_ids"]:
                session["host_spawn_ids"] = sorted(
                    [*session["host_spawn_ids"], host_id], key=lambda item: item.encode("utf-8")
                )

        unresolved = []
        known_hosts = {
            host
            for session in sessions.values()
            for host in session.get("host_spawn_ids", [])
        }
        for diagnostic in current["diagnostics"]:
            if diagnostic.get("code") == "E_TRACKER_MISSING_CHILD" and diagnostic.get(
                "host_spawn_id"
            ) in known_hosts:
                continue
            unresolved.append(diagnostic)
        updated_at = max(
            [current["updated_at"], *(event["observed_at"] for event in combined)]
        )
        projection = {
            **current,
            "generation": generation,
            "revision": current["revision"] + 1,
            "event_count": len(combined),
            "events": combined,
            "event_hashes": event_hashes,
            "cursors": cursors,
            "sessions": sessions,
            "diagnostics": unresolved,
            "updated_at": updated_at,
        }
        _validate_projection(projection, run_id)
        atomic_write_bytes(
            path,
            canonical_json_bytes(projection),
            mode=DATA_FILE_MODE,
            replace=True,
        )
        return projection


def _lease_path(root: Path | str, run_id: str, kind: str) -> Path:
    if kind not in {"primary", "hud"}:
        raise ContractValidationError("tracker lease kind must be primary or hud")
    return _tracker_dir(root, run_id) / "leases" / f"{kind}.json"


def _default_process_identity_matches(pid: int, process_start_identity: str) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        from omg_cli.state import process_starttime

        observed = process_starttime(pid)
    except Exception:  # pragma: no cover - platform fallback
        observed = None
    return observed is None or observed == process_start_identity


def _validate_hud_lease(lease: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(lease)
    if value.get("store_kind") != "tracker_hud_lease" or value.get("schema_version") != 1:
        raise ContractValidationError("HUD lease header mismatch")
    projected = {**value, "store_kind": "tracker_projector_lease"}
    validate_projector_lease(projected)
    return value


def acquire_tracker_lease(
    root: Path | str,
    *,
    run_id: str,
    kind: str,
    pid: int,
    process_start_identity: str,
    owner_token: str,
    generation: int,
    cursor: str,
    now: datetime | None = None,
    stale_after_seconds: float = 60.0,
    process_identity_matches: Callable[[int, str], bool] | None = None,
) -> dict[str, Any]:
    require_integer(pid, label="pid", minimum=1)
    require_safe_id(owner_token, label="owner_token")
    require_integer(generation, label="generation", minimum=0)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    timestamp = current_time.isoformat().replace("+00:00", "Z")
    path = _lease_path(root, run_id, kind)
    ensure_managed_dir(path.parent)
    matcher = process_identity_matches or _default_process_identity_matches
    with exclusive_lock(path.with_suffix(".lock")):
        existing: dict[str, Any] | None = None
        if path.exists():
            parsed = parse_canonical_json_bytes(path.read_bytes())
            if not isinstance(parsed, dict):
                raise ContractValidationError("tracker lease must be an object")
            existing = (
                dict(validate_projector_lease(parsed))
                if kind == "primary"
                else _validate_hud_lease(parsed)
            )
        if existing is not None:
            age = (current_time - _parse_time(existing["last_successful_poll"])).total_seconds()
            healthy = age <= stale_after_seconds and matcher(
                existing["pid"], existing["process_start_identity"]
            )
            if existing["owner_token"] == owner_token:
                if generation != existing["generation"]:
                    raise TrackerLeaseBusy("owner attempted a different tracker generation")
            elif healthy:
                raise TrackerLeaseBusy("tracker lease is healthy")
            elif generation != existing["generation"] + 1:
                raise TrackerLeaseBusy("tracker takeover requires generation+1")
        lease = {
            "store_kind": "tracker_projector_lease" if kind == "primary" else "tracker_hud_lease",
            "schema_version": 1,
            "pid": pid,
            "process_start_identity": process_start_identity,
            "owner_token": owner_token,
            "generation": generation,
            "last_successful_poll": timestamp,
            "cursor": cursor,
            "error": None,
        }
        if kind == "primary":
            validate_projector_lease(lease)
        else:
            _validate_hud_lease(lease)
        atomic_write_bytes(path, canonical_json_bytes(lease), mode=DATA_FILE_MODE, replace=True)
        return lease


def reconcile_native_inventory(
    root: Path | str,
    *,
    run_id: str,
    inventory: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    path = tracker_projection_path(root, run_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(_projection_lock(root, run_id)):
        current = load_tracker_projection(root, run_id)
        if current is None:
            raise TrackerError("tracker projection does not exist")
        known = {
            host
            for session in current["sessions"].values()
            for host in session.get("host_spawn_ids", [])
        }
        diagnostics = [
            row
            for row in current["diagnostics"]
            if row.get("code") != "E_TRACKER_MISSING_CHILD"
        ]
        for item in inventory:
            host_id = str(item.get("host_spawn_id") or "")
            session_id = str(item.get("session_id") or "")
            if not host_id or host_id in known:
                continue
            diagnostics.append(
                redact_value(
                    {
                        "code": "E_TRACKER_MISSING_CHILD",
                        "host_spawn_id": host_id,
                        "session_id": session_id,
                    }
                )
            )
        diagnostics.sort(
            key=lambda row: (
                str(row.get("code", "")).encode(),
                str(row.get("host_spawn_id", "")).encode(),
            )
        )
        updated = {
            **current,
            "revision": current["revision"] + 1,
            "diagnostics": diagnostics,
            "updated_at": _utc_now(),
        }
        _validate_projection(updated, run_id)
        atomic_write_bytes(path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True)
        return {"strict_ok": not diagnostics, "diagnostics": diagnostics}


def _receipt_pair_path(root: Path | str, run_id: str, receipt_id: str) -> Path:
    require_safe_id(receipt_id, label="receipt_id")
    key = safe_path_key(receipt_id, namespace="spawn-receipt")
    return _tracker_dir(root, run_id) / "receipts" / f"{key}.json"


def _validate_stored_receipt_pair(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "run_id",
        "receipt_id",
        "spawn_receipt",
        "role_receipt",
        "spawn_receipt_hash",
        "role_receipt_hash",
        "status",
        "native_binding",
    }
    if set(row) != required:
        raise ContractValidationError("stored spawn receipt pair keys mismatch")
    if row["store_kind"] != "stored_spawn_receipt_pair" or row["schema_version"] != 1:
        raise ContractValidationError("stored spawn receipt pair header mismatch")
    require_safe_id(row["run_id"], label="run_id")
    spawn = validate_spawn_receipt(row["spawn_receipt"])
    if row["receipt_id"] != spawn["receipt_id"] or row["run_id"] != spawn["run_id"]:
        raise ContractValidationError("stored spawn receipt identity mismatch")
    role = make_role_receipt(spawn)
    if row["role_receipt"] != role:
        raise ContractValidationError("stored role receipt mismatch")
    spawn_hash = sha256_hex(canonical_json_bytes(spawn))
    role_hash = sha256_hex(canonical_json_bytes(role))
    if row["spawn_receipt_hash"] != spawn_hash or row["role_receipt_hash"] != role_hash:
        raise ContractValidationError("stored receipt hash mismatch")
    if row["status"] not in {"spawn_requested", "launch_unknown", "bound", "blocked"}:
        raise ContractValidationError("stored spawn receipt status mismatch")
    if row["native_binding"] is not None and not isinstance(row["native_binding"], dict):
        raise ContractValidationError("stored native binding must be an object or null")
    return row


def persist_spawn_receipt_pair(
    root: Path | str,
    *,
    spawn_receipt: Mapping[str, Any],
    role_receipt: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist exact W0 receipt bytes before any native spawn dispatch."""

    spawn = validate_spawn_receipt(spawn_receipt, now=now or datetime.now(timezone.utc))
    role = make_role_receipt(spawn)
    if dict(role_receipt) != role:
        raise ContractValidationError("role_receipt disagrees with spawn_receipt")
    pair = _validate_stored_receipt_pair(
        {
            "store_kind": "stored_spawn_receipt_pair",
            "schema_version": 1,
            "run_id": spawn["run_id"],
            "receipt_id": spawn["receipt_id"],
            "spawn_receipt": spawn,
            "role_receipt": role,
            "spawn_receipt_hash": sha256_hex(canonical_json_bytes(spawn)),
            "role_receipt_hash": sha256_hex(canonical_json_bytes(role)),
            "status": "spawn_requested",
            "native_binding": None,
        }
    )
    path = _receipt_pair_path(root, spawn["run_id"], spawn["receipt_id"])
    ensure_managed_dir(path.parent)
    body = canonical_json_bytes(pair)
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            parsed = parse_canonical_json_bytes(path.read_bytes())
            if not isinstance(parsed, dict):
                raise ContractValidationError("stored spawn receipt pair must be an object")
            current = _validate_stored_receipt_pair(parsed)
            if current["spawn_receipt_hash"] != pair["spawn_receipt_hash"]:
                raise ContractValidationError("spawn receipt ID replayed with different bytes")
            return current
        atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
    return pair


def load_spawn_receipt_pair(
    root: Path | str, *, run_id: str, receipt_id: str
) -> dict[str, Any] | None:
    path = _receipt_pair_path(root, run_id, receipt_id)
    if not path.exists():
        return None
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("stored spawn receipt pair must be an object")
    return _validate_stored_receipt_pair(parsed)


def reconcile_spawn_observation(
    root: Path | str,
    *,
    run_id: str,
    receipt_id: str,
    inventory: Sequence[Mapping[str, Any]],
    expected_generation: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve zero/one/many host matches without blind double spawning."""

    path = _receipt_pair_path(root, run_id, receipt_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        if not path.exists():
            raise TrackerError("spawn receipt pair does not exist")
        parsed = parse_canonical_json_bytes(path.read_bytes())
        if not isinstance(parsed, dict):
            raise ContractValidationError("stored spawn receipt pair must be an object")
        pair = _validate_stored_receipt_pair(parsed)
        spawn = pair["spawn_receipt"]
        if spawn["receipt_generation"] != expected_generation:
            raise ContractValidationError("stale spawn receipt generation")
        exact: list[dict[str, Any]] = []
        related_invalid = False
        for raw in inventory:
            row = dict(raw)
            related = row.get("spawn_receipt_hash") == pair["spawn_receipt_hash"] or row.get(
                "role_receipt_hash"
            ) == pair["role_receipt_hash"]
            if not related:
                continue
            expected = {
                "spawn_receipt_hash": pair["spawn_receipt_hash"],
                "role_receipt_hash": pair["role_receipt_hash"],
                "run_id": spawn["run_id"],
                "task_id": spawn["task_id"],
                "parent_id": spawn["parent_id"],
            }
            if any(row.get(field) != value for field, value in expected.items()):
                related_invalid = True
                continue
            if not isinstance(row.get("host_spawn_id"), str) or not isinstance(
                row.get("observed_session_id"), str
            ):
                related_invalid = True
                continue
            exact.append(row)
        if related_invalid or len(exact) > 1:
            updated = {**pair, "status": "blocked", "native_binding": None}
            atomic_write_bytes(
                path,
                canonical_json_bytes(updated),
                mode=DATA_FILE_MODE,
                replace=True,
            )
            return {"outcome": "blocked", "matches": len(exact), "retry_allowed": False}
        if not exact:
            expired = False
            try:
                validate_spawn_receipt(spawn, now=now or datetime.now(timezone.utc))
            except ContractValidationError as exc:
                if "expired" not in str(exc):
                    raise
                expired = True
            updated = {**pair, "status": "launch_unknown", "native_binding": None}
            atomic_write_bytes(
                path,
                canonical_json_bytes(updated),
                mode=DATA_FILE_MODE,
                replace=True,
            )
            return {"outcome": "launch_unknown", "matches": 0, "retry_allowed": expired}
        candidate = exact[0]
        binding = bind_native_spawn(
            spawn,
            pair["role_receipt"],
            host_spawn_id=candidate["host_spawn_id"],
            observed_session_id=candidate["observed_session_id"],
            expected_generation=expected_generation,
            expected_run_id=run_id,
            expected_task_id=spawn["task_id"],
            expected_parent_id=spawn["parent_id"],
            now=now or datetime.now(timezone.utc),
        )
        updated = {**pair, "status": "bound", "native_binding": binding}
        _validate_stored_receipt_pair(updated)
        atomic_write_bytes(
            path,
            canonical_json_bytes(updated),
            mode=DATA_FILE_MODE,
            replace=True,
        )
        return {"outcome": "bound", "matches": 1, "retry_allowed": False, "binding": binding}


__all__ = [
    "TrackerError",
    "TrackerLeaseBusy",
    "acquire_tracker_lease",
    "load_tracker_projection",
    "project_lifecycle_events",
    "persist_spawn_receipt_pair",
    "load_spawn_receipt_pair",
    "reconcile_native_inventory",
    "reconcile_spawn_observation",
    "tracker_projection_path",
]
