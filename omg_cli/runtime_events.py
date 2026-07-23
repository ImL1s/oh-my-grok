"""Bounded, redacted, source-specific lifecycle journals."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.event_contract import (
    append_lifecycle_event,
    event_identity,
    validate_lifecycle_event,
)
from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import ContractValidationError, require_safe_id
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)
from omg_cli.redaction import redact_value


MAX_EVENT_BYTES = 65_536
MAX_HOOK_IDENTITIES = 4096

_HOOK_ALIASES = {
    "SessionStart": ("SessionStart", "session_started"),
    "PreToolUse": ("PreToolUse", "turn_started"),
    "PostToolUse": ("PostToolUse", "turn_completed"),
    "Stop": ("Stop", "agent_closed"),
    "SessionEnd": ("SessionEnd", "agent_closed"),
    "SubagentStart": ("SubagentStart", "spawn_requested"),
    "SubagentEnd": ("SubagentStop", "agent_closed"),
    "SubagentStop": ("SubagentStop", "agent_closed"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_event_id(value: object, *, prefix: str) -> str:
    text = str(value or "").strip()
    try:
        return require_safe_id(text, label="event_id")
    except ContractValidationError:
        return f"{prefix}-{sha256_hex(text.encode('utf-8'))[:32]}"


def source_journal_path(root: Path | str, source: str) -> Path:
    require_safe_id(source, label="source")
    key = safe_path_key(source, namespace="lifecycle-source")
    directory = Path(root).resolve() / ".omg" / "state" / "events"
    return directory / f"{key}.jsonl"


def _cursor_path(root: Path | str, source: str) -> Path:
    key = safe_path_key(source, namespace="lifecycle-source")
    return Path(root).resolve() / ".omg" / "state" / "event-cursors" / f"{key}.json"


def normalize_lifecycle_event(
    *,
    source: str,
    source_cursor: str,
    source_sequence: int,
    event_id: str,
    event_type: str,
    run_id: str,
    session_id: str,
    payload: Mapping[str, Any],
    observed_at: str | None = None,
) -> dict[str, Any]:
    redacted_payload = redact_value(dict(payload))
    if not isinstance(redacted_payload, dict):  # pragma: no cover - Mapping above
        raise ContractValidationError("event payload must redact to an object")
    event = {
        "store_kind": "normalized_lifecycle_event",
        "schema_version": 1,
        "source": source,
        "source_cursor": source_cursor,
        "source_sequence": source_sequence,
        "event_id": event_id,
        "event_type": event_type,
        "run_id": run_id,
        "session_id": session_id,
        "observed_at": observed_at or _utc_now(),
        "payload": redacted_payload,
    }
    validate_lifecycle_event(event)
    return event


def append_runtime_event(root: Path | str, event: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_lifecycle_event(dict(event))
    if len(canonical_json_bytes(normalized)) > MAX_EVENT_BYTES:
        raise ContractValidationError("normalized lifecycle event exceeds bounded limit")
    path = source_journal_path(root, normalized["source"])
    ensure_managed_dir(path.parent)
    digest = append_lifecycle_event(path, normalized)
    return {
        "journal_path": path,
        "event_hash": digest,
        "event_identity": event_identity(normalized),
    }


def _read_cursor(path: Path, source: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "store_kind": "lifecycle_source_cursor",
            "schema_version": 1,
            "source": source,
            "last_sequence": -1,
            "identities": {},
        }
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("lifecycle cursor must be an object")
    if (
        parsed.get("store_kind") != "lifecycle_source_cursor"
        or parsed.get("schema_version") != 1
        or parsed.get("source") != source
        or not isinstance(parsed.get("last_sequence"), int)
        or isinstance(parsed.get("last_sequence"), bool)
        or not isinstance(parsed.get("identities"), dict)
    ):
        raise ContractValidationError("invalid lifecycle source cursor")
    return parsed


def append_hook_event(
    root: Path | str,
    *,
    hook_event: str,
    payload: Mapping[str, Any],
    run_id: str | None = None,
    session_id: str | None = None,
    event_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Append one route-independent hook observation.

    Duplicate plugin/global observations reuse their original sequence and
    timestamp so W0's byte-exact idempotency rule remains authoritative.
    """

    canonical_hook, event_type = _HOOK_ALIASES.get(
        hook_event, ("UnknownHook", "agent_failed")
    )
    source = "grok-hook"
    root_path = Path(root).resolve()
    cursor_path = _cursor_path(root_path, source)
    ensure_managed_dir(cursor_path.parent)
    lock_path = cursor_path.with_suffix(".lock")
    raw_payload = {key: item for key, item in dict(payload).items() if key not in {"route", "hook_route"}}
    raw_payload["hook_event"] = canonical_hook
    if canonical_hook == "SubagentStop":
        hashes_valid = all(
            isinstance(raw_payload.get(field), str)
            and len(raw_payload[field]) == 64
            and all(char in "0123456789abcdef" for char in raw_payload[field])
            for field in ("spawn_receipt_hash", "role_receipt_hash")
        )
        generation = raw_payload.get("generation")
        receipt_generation = raw_payload.get("receipt_generation")
        generation_valid = (
            generation is None
            or receipt_generation is None
            or (
                isinstance(generation, int)
                and not isinstance(generation, bool)
                and receipt_generation == generation
            )
        )
        bound = (
            raw_payload.get("bound") is True
            and isinstance(raw_payload.get("host_spawn_id"), str)
            and bool(raw_payload["host_spawn_id"])
            and hashes_valid
            and generation_valid
        )
        if not bound:
            event_type = "agent_failed"
            raw_payload["diagnostic"] = "E_UNBOUND_SUBAGENT_COMPLETION"
    identity_id = _safe_event_id(
        event_id or raw_payload.get("event_id") or raw_payload.get("hook_event_id") or os.urandom(16).hex(),
        prefix="hook",
    )
    run = _safe_event_id(run_id or os.environ.get("OMG_RUN_ID") or "unbound-run", prefix="run")
    session = _safe_event_id(
        session_id
        or os.environ.get("GROK_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "unbound-session",
        prefix="session",
    )

    with exclusive_lock(lock_path):
        state = _read_cursor(cursor_path, source)
        previous = state["identities"].get(identity_id)
        if previous is None:
            sequence = state["last_sequence"] + 1
            timestamp = observed_at or _utc_now()
            source_cursor = f"hook-{sequence}"
        else:
            sequence = previous["source_sequence"]
            timestamp = previous["observed_at"]
            source_cursor = previous["source_cursor"]
        event = normalize_lifecycle_event(
            source=source,
            source_cursor=source_cursor,
            source_sequence=sequence,
            event_id=identity_id,
            event_type=event_type,
            run_id=run,
            session_id=session,
            observed_at=timestamp,
            payload=raw_payload,
        )
        event_hash = sha256_hex(canonical_json_bytes(event))
        if previous is not None and previous["event_hash"] != event_hash:
            raise ContractValidationError("hook event identity conflicts with prior bytes")
        result = append_runtime_event(root_path, event)
        result["duplicate"] = previous is not None
        if previous is None:
            identities = dict(state["identities"])
            identities[identity_id] = {
                "source_sequence": sequence,
                "source_cursor": source_cursor,
                "observed_at": timestamp,
                "event_hash": event_hash,
            }
            if len(identities) > MAX_HOOK_IDENTITIES:
                ordered = sorted(
                    identities.items(),
                    key=lambda pair: (pair[1]["source_sequence"], pair[0]),
                )[-MAX_HOOK_IDENTITIES:]
                identities = dict(ordered)
            state = {**state, "last_sequence": sequence, "identities": identities}
            atomic_write_bytes(
                cursor_path,
                canonical_json_bytes(state),
                mode=DATA_FILE_MODE,
                replace=True,
            )
        return result


def read_runtime_events(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in source.read_bytes().splitlines():
        parsed = parse_canonical_json_bytes(line)
        if not isinstance(parsed, dict):
            raise ContractValidationError("lifecycle journal row must be an object")
        rows.append(validate_lifecycle_event(parsed))
    return rows


def read_all_runtime_events(root: Path | str) -> list[dict[str, Any]]:
    directory = Path(root).resolve() / ".omg" / "state" / "events"
    rows: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.jsonl")):
            rows.extend(read_runtime_events(path))
    return sorted(
        rows,
        key=lambda row: (
            row["observed_at"],
            row["source"].encode("utf-8"),
            row["source_sequence"],
            row["event_id"].encode("utf-8"),
        ),
    )


__all__ = [
    "MAX_EVENT_BYTES",
    "append_hook_event",
    "append_runtime_event",
    "normalize_lifecycle_event",
    "read_all_runtime_events",
    "read_runtime_events",
    "source_journal_path",
]
