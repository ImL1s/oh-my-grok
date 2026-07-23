"""Bounded, redacted notification event and lifecycle mapping contracts."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from collections.abc import Mapping
from typing import Any

from omg_cli.contracts.state_schemas import SAFE_ID_RE
from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.redaction import redact_text


_SEVERITIES = {"info", "success", "warning", "error"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# The strings are deliberately static.  Lifecycle payloads can contain prompts,
# commands, paths, and provider error bodies; none of those are notification
# content by default.
_LIFECYCLE_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "session_started": ("info", "Session started", "An OMG host session started."),
    "session_ended": ("success", "Session ended", "An OMG host session ended."),
    "run_started": ("info", "Run started", "An OMG workflow run started."),
    "run_completed": ("success", "Run completed", "An OMG workflow run completed."),
    "run_failed": ("error", "Run failed", "An OMG workflow run failed."),
    "run_cancelled": ("warning", "Run cancelled", "An OMG workflow run was cancelled."),
    "blocked": ("warning", "Needs input", "An OMG workflow is blocked and needs input."),
    "permission_denied": ("error", "Permission denied", "An OMG operation was denied."),
    "subagent_completed": ("success", "Subagent completed", "An OMG subagent completed."),
    "team_degraded": ("warning", "Team health degraded", "An OMG team reported degraded health."),
    "acceptance_passed": ("success", "Acceptance passed", "OMG acceptance verification passed."),
    "acceptance_failed": ("error", "Acceptance failed", "OMG acceptance verification failed."),
    "release_succeeded": ("success", "Release succeeded", "An OMG release completed."),
    "release_failed": ("error", "Release failed", "An OMG release failed."),
}


def _bounded(value: str, maximum: int) -> str:
    safe = "".join(
        " " if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F else char
        for char in redact_text(value)
    )
    body = safe.encode("utf-8")
    if len(body) <= maximum:
        return body.decode("utf-8")
    return body[:maximum].decode("utf-8", errors="ignore")


def _sha(value: str | bytes) -> str:
    body = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _valid_nonce(value: object) -> bool:
    return (
        isinstance(value, str)
        and 16 <= len(value) <= 4_096
        and not any(char in value for char in ("\0", "\r", "\n"))
    )


def create_notification_event(
    *,
    severity: str,
    title: str,
    message: str,
    owner_id: str,
    generation: int,
    owner_nonce: str,
    created_at: str | None = None,
    event_type: str = "message",
    stable_source_id: str | None = None,
) -> dict[str, Any]:
    if created_at is not None and not isinstance(created_at, str):
        raise TypeError("notification timestamp must be a string")
    timestamp = created_at or datetime.now().astimezone().isoformat()
    candidate = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("notification timestamp must include a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("notification timestamp must include a timezone")
    if severity not in _SEVERITIES:
        raise ValueError("notification severity is invalid")
    if not isinstance(title, str) or not isinstance(message, str):
        raise TypeError("notification title and message must be strings")
    if not isinstance(owner_id, str) or SAFE_ID_RE.fullmatch(owner_id) is None:
        raise ValueError("notification owner_id is invalid")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("notification generation is invalid")
    if not _valid_nonce(owner_nonce):
        raise ValueError("notification owner_nonce is invalid")
    if not isinstance(event_type, str) or _EVENT_TYPE.fullmatch(event_type) is None:
        raise ValueError("notification event_type is invalid")
    if stable_source_id is not None and (
        not isinstance(stable_source_id, str)
        or not stable_source_id
        or len(stable_source_id.encode("utf-8")) > 512
        or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in stable_source_id)
    ):
        raise ValueError("notification stable_source_id is invalid")
    bounded_title = _bounded(title, 256)
    bounded_message = _bounded(message, 2_048)
    source_identity = stable_source_id or _sha(
        canonical_json_bytes([event_type, timestamp, bounded_title, bounded_message])
    )
    dedupe_key = _sha(
        canonical_json_bytes([1, event_type, owner_id, generation, source_identity])
    )
    unsigned = {
        "store_kind": "omg_notification_event",
        "schema_version": 1,
        "repository_id": "OMG",
        "created_at": timestamp,
        "event_type": event_type,
        "dedupe_key": dedupe_key,
        "severity": severity,
        "title": bounded_title,
        "message": bounded_message,
        "owner": {
            "owner_id": owner_id,
            "generation": generation,
            "owner_nonce_sha256": _sha(owner_nonce),
        },
    }
    return {**unsigned, "event_id": _sha(canonical_json_bytes(unsigned))}


def notification_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return an integrity-checked allowlisted event projection for transport."""

    if not isinstance(event, Mapping):
        return None
    timestamp = event.get("created_at")
    severity = event.get("severity")
    title = event.get("title")
    message = event.get("message")
    current = event.get("owner")
    event_id = event.get("event_id")
    event_type = event.get("event_type")
    dedupe_key = event.get("dedupe_key")
    if (
        event.get("store_kind") != "omg_notification_event"
        or event.get("schema_version") != 1
        or event.get("repository_id") != "OMG"
        or not isinstance(timestamp, str)
        or len(timestamp.encode("utf-8")) > 128
        or not isinstance(event_type, str)
        or _EVENT_TYPE.fullmatch(event_type) is None
        or not isinstance(dedupe_key, str)
        or _SHA256.fullmatch(dedupe_key) is None
        or severity not in _SEVERITIES
        or not isinstance(title, str)
        or not isinstance(message, str)
        or title != _bounded(title, 256)
        or message != _bounded(message, 2_048)
        or not isinstance(current, Mapping)
        or not isinstance(event_id, str)
        or _SHA256.fullmatch(event_id) is None
    ):
        return None
    candidate = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    owner_id = current.get("owner_id")
    generation = current.get("generation")
    owner_nonce_sha256 = current.get("owner_nonce_sha256")
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or not isinstance(owner_id, str)
        or SAFE_ID_RE.fullmatch(owner_id) is None
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or not isinstance(owner_nonce_sha256, str)
        or _SHA256.fullmatch(owner_nonce_sha256) is None
    ):
        return None
    unsigned = {
        "store_kind": "omg_notification_event",
        "schema_version": 1,
        "repository_id": "OMG",
        "created_at": timestamp,
        "event_type": event_type,
        "dedupe_key": dedupe_key,
        "severity": severity,
        "title": title,
        "message": message,
        "owner": {
            "owner_id": owner_id,
            "generation": generation,
            "owner_nonce_sha256": owner_nonce_sha256,
        },
    }
    if event_id != _sha(canonical_json_bytes(unsigned)):
        return None
    return {**unsigned, "event_id": event_id}


def notification_from_lifecycle(
    lifecycle: Mapping[str, Any],
    *,
    owner_id: str,
    generation: int,
    owner_nonce: str,
) -> dict[str, Any] | None:
    """Map one bounded lifecycle record to a stable outbound event.

    Unknown lifecycle types are intentionally ignored.  The mapping never
    copies lifecycle payload values, so prompts, tool inputs, commands and
    provider error bodies cannot become outbound content by accident.
    """

    if not isinstance(lifecycle, Mapping):
        return None
    raw_type = lifecycle.get("notification_type", lifecycle.get("event_type"))
    event_type = str(raw_type or "")
    payload = lifecycle.get("payload")
    payload_map = payload if isinstance(payload, Mapping) else {}
    if event_type == "agent_closed":
        event_type = "subagent_completed" if payload_map.get("subagent_id") else "session_ended"
    elif event_type == "agent_failed":
        event_type = "run_failed"
    elif event_type == "completion":
        event_type = "run_completed"
    elif event_type in {"cancelled", "canceled"}:
        event_type = "run_cancelled"
    elif event_type in {"needs_input", "blocked_needs_input"}:
        event_type = "blocked"
    elif event_type == "team_health_degraded":
        event_type = "team_degraded"
    template = _LIFECYCLE_TEMPLATES.get(event_type)
    if template is None:
        return None
    stable_source = next(
        (
            value
            for value in (
                lifecycle.get("event_id"),
                lifecycle.get("source_cursor"),
                lifecycle.get("stable_source_id"),
            )
            if isinstance(value, str) and value
        ),
        None,
    )
    if stable_source is None:
        sequence = lifecycle.get("source_sequence")
        timestamp = lifecycle.get("observed_at")
        if isinstance(sequence, int) and not isinstance(sequence, bool) and isinstance(timestamp, str):
            stable_source = f"{timestamp}:{sequence}"
        else:
            return None
    observed_at = lifecycle.get("observed_at")
    severity, title, message = template
    try:
        return create_notification_event(
            severity=severity,
            title=title,
            message=message,
            owner_id=owner_id,
            generation=generation,
            owner_nonce=owner_nonce,
            created_at=observed_at if isinstance(observed_at, str) else None,
            event_type=event_type,
            stable_source_id=stable_source,
        )
    except (TypeError, ValueError):
        return None


def owner_matches(event: Mapping[str, Any], owner: Mapping[str, Any] | None) -> bool:
    if not isinstance(owner, Mapping) or not _valid_nonce(owner.get("owner_nonce")):
        return False
    owner_id = owner.get("owner_id")
    generation = owner.get("generation")
    if (
        not isinstance(owner_id, str)
        or SAFE_ID_RE.fullmatch(owner_id) is None
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        return False
    current = event.get("owner")
    return (
        isinstance(current, Mapping)
        and current.get("owner_id") == owner_id
        and current.get("generation") == generation
        and isinstance(current.get("owner_nonce_sha256"), str)
        and _SHA256.fullmatch(str(current.get("owner_nonce_sha256"))) is not None
        and current.get("owner_nonce_sha256") == _sha(str(owner.get("owner_nonce")))
    )


def notification_line(event: Mapping[str, Any], maximum: int = 2_048) -> str:
    safe_event = notification_payload(event)
    if safe_event is None:
        return "[OMG ERROR] Notification rejected: event integrity validation failed."
    return _bounded(
        f"[OMG {str(safe_event.get('severity') or 'info').upper()}] "
        f"{safe_event.get('title')}: {safe_event.get('message')}",
        maximum,
    )


def notification_outcome(
    adapter: str,
    status: str,
    code: str,
    event: dict[str, Any],
    destination: str | None,
    diagnostic: str | None = None,
) -> dict[str, Any]:
    return {
        "adapter": adapter,
        "status": status,
        "code": code,
        "event_id": (
            event.get("event_id")
            if isinstance(event.get("event_id"), str)
            and _SHA256.fullmatch(str(event.get("event_id"))) is not None
            else None
        ),
        "destination_sha256": _sha(destination) if destination is not None else None,
        "diagnostic": _bounded(diagnostic, 1_024) if diagnostic else None,
        "authoritative": False,
    }


__all__ = [
    "create_notification_event",
    "notification_line",
    "notification_from_lifecycle",
    "notification_outcome",
    "notification_payload",
    "owner_matches",
]
