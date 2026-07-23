"""Append-only normalized lifecycle event contract."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .path_keys import append_locked_jsonl_once
from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_iso8601,
    require_nonempty_string,
    require_object,
    require_safe_id,
)
from .writer_chain import canonical_json_bytes, parse_canonical_json_bytes, sha256_hex


LIFECYCLE_EVENTS = (
    "spawn_requested",
    "session_started",
    "turn_started",
    "turn_completed",
    "agent_closed",
    "agent_failed",
)


def validate_lifecycle_event(value: Mapping[str, Any]) -> dict[str, Any]:
    event = require_object(value, label="lifecycle event")
    require_exact_keys(
        event,
        required={
            "store_kind",
            "schema_version",
            "source",
            "source_cursor",
            "source_sequence",
            "event_id",
            "event_type",
            "run_id",
            "session_id",
            "observed_at",
            "payload",
        },
        label="lifecycle event",
    )
    if event["store_kind"] != "normalized_lifecycle_event":
        raise ContractValidationError("invalid lifecycle event store_kind")
    if require_integer(event["schema_version"], label="schema_version", minimum=1) != 1:
        raise ContractValidationError("unsupported lifecycle event schema")
    require_safe_id(event["source"], label="source")
    require_nonempty_string(event["source_cursor"], label="source_cursor")
    require_integer(event["source_sequence"], label="source_sequence", minimum=0)
    require_safe_id(event["event_id"], label="event_id")
    if event["event_type"] not in LIFECYCLE_EVENTS:
        raise ContractValidationError(f"unsupported event_type {event['event_type']!r}")
    require_safe_id(event["run_id"], label="run_id")
    require_safe_id(event["session_id"], label="session_id")
    require_iso8601(event["observed_at"], label="observed_at")
    require_object(event["payload"], label="payload")
    return event


def event_identity(value: Mapping[str, Any]) -> str:
    event = validate_lifecycle_event(value)
    return sha256_hex(
        canonical_json_bytes(
            [
                event["source"],
                event["source_cursor"],
                event["source_sequence"],
                event["event_id"],
            ]
        )
    )


def append_lifecycle_event(path: Path | str, value: Mapping[str, Any]) -> str:
    event = validate_lifecycle_event(value)
    body = canonical_json_bytes(event)
    identity = event_identity(event)

    def identity_from_record(existing: bytes) -> str:
        parsed = parse_canonical_json_bytes(existing)
        if not isinstance(parsed, Mapping):
            raise ContractValidationError("lifecycle journal row must be an object")
        return event_identity(parsed)

    try:
        append_locked_jsonl_once(
            path,
            body,
            identity=identity,
            identity_from_record=identity_from_record,
        )
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    return sha256_hex(body)
