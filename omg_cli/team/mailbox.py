"""Generation-fenced, redacted team mailbox owned by the OMG CLI.

Workers never edit mailbox files directly.  They ask the leader/CLI to send,
list, read, or acknowledge messages.  Each recipient has an independent
monotonic sequence so an acknowledgement cursor can be recovered after a
crash without relying on mtimes or in-memory state.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
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
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)
from omg_cli.redaction import redact_value


CLI_WRITER = "omg-cli"
MAX_MESSAGE_BYTES = 65_536
MAX_MESSAGES_PER_RECIPIENT = 4096


class MailboxError(RuntimeError):
    """A mailbox request violated identity, ordering, or replay rules."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mailbox_dir(root: Path | str, run_id: str, team_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
        / "mailbox"
    )


def _recipient_path(
    root: Path | str, run_id: str, team_id: str, recipient_id: str
) -> Path:
    require_safe_id(recipient_id, label="recipient_id")
    return _mailbox_dir(root, run_id, team_id) / (
        safe_path_key(recipient_id, namespace="recipient") + ".json"
    )


def _empty_mailbox(run_id: str, team_id: str, recipient_id: str) -> dict[str, Any]:
    return {
        "store_kind": "team_mailbox",
        "schema_version": 1,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "recipient_id": recipient_id,
        "next_sequence": 0,
        "ack_cursor": -1,
        "messages": [],
        "dedupe": {},
    }


def _validate_mailbox(
    value: Mapping[str, Any], *, run_id: str, team_id: str, recipient_id: str
) -> dict[str, Any]:
    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "recipient_id",
        "next_sequence",
        "ack_cursor",
        "messages",
        "dedupe",
    }
    if set(row) != required:
        raise ContractValidationError("team mailbox keys mismatch")
    if (
        row["store_kind"] != "team_mailbox"
        or row["schema_version"] != 1
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("team mailbox header mismatch")
    for field, expected in (
        ("run_id", run_id),
        ("team_id", team_id),
        ("recipient_id", recipient_id),
    ):
        if row[field] != expected:
            raise ContractValidationError(f"team mailbox {field} mismatch")
    next_sequence = require_integer(
        row["next_sequence"], label="next_sequence", minimum=0
    )
    ack_cursor = require_integer(row["ack_cursor"], label="ack_cursor", minimum=-1)
    messages = row["messages"]
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES_PER_RECIPIENT:
        raise ContractValidationError("team mailbox messages are not bounded")
    if next_sequence != len(messages):
        raise ContractValidationError("team mailbox sequence/cardinality mismatch")
    if ack_cursor >= next_sequence:
        raise ContractValidationError("team mailbox ack cursor is in the future")
    dedupe = row["dedupe"]
    if not isinstance(dedupe, dict) or len(dedupe) > MAX_MESSAGES_PER_RECIPIENT:
        raise ContractValidationError("team mailbox dedupe map is not bounded")
    seen_ids: set[str] = set()
    seen_keys: dict[str, str] = {}
    for expected_sequence, raw in enumerate(messages):
        if not isinstance(raw, Mapping):
            raise ContractValidationError("team mailbox message must be an object")
        message = dict(raw)
        required_message = {
            "message_id",
            "sequence",
            "sender_id",
            "recipient_id",
            "generation",
            "kind",
            "body",
            "dedupe_key",
            "content_hash",
            "sent_at",
        }
        if set(message) != required_message:
            raise ContractValidationError("team mailbox message keys mismatch")
        require_safe_id(message["message_id"], label="message_id")
        if message["message_id"] in seen_ids:
            raise ContractValidationError("duplicate team mailbox message_id")
        seen_ids.add(message["message_id"])
        if message["sequence"] != expected_sequence:
            raise ContractValidationError("team mailbox message sequence gap")
        require_safe_id(message["sender_id"], label="sender_id")
        if message["recipient_id"] != recipient_id:
            raise ContractValidationError("team mailbox message recipient mismatch")
        require_integer(message["generation"], label="generation", minimum=0)
        require_safe_id(message["kind"], label="kind")
        require_safe_id(message["dedupe_key"], label="dedupe_key")
        require_iso8601(message["sent_at"], label="sent_at")
        if not isinstance(message["body"], (dict, list, str, int, bool, type(None))):
            raise ContractValidationError("team mailbox body is not JSON-safe")
        if message["content_hash"] != sha256_hex(
            canonical_json_bytes(
                {
                    "sender_id": message["sender_id"],
                    "recipient_id": message["recipient_id"],
                    "generation": message["generation"],
                    "kind": message["kind"],
                    "body": message["body"],
                    "dedupe_key": message["dedupe_key"],
                }
            )
        ):
            raise ContractValidationError("team mailbox content hash mismatch")
        seen_keys[message["dedupe_key"]] = message["content_hash"]
    if dedupe != seen_keys:
        raise ContractValidationError("team mailbox dedupe index mismatch")
    return row


def _load_locked(
    path: Path, *, run_id: str, team_id: str, recipient_id: str
) -> dict[str, Any]:
    if not path.exists():
        return _empty_mailbox(run_id, team_id, recipient_id)
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("team mailbox must be an object")
    return _validate_mailbox(
        parsed, run_id=run_id, team_id=team_id, recipient_id=recipient_id
    )


def cursor_token(sequence: int) -> str:
    require_integer(sequence, label="cursor", minimum=-1)
    return "start" if sequence == -1 else f"m{sequence:012d}"


def parse_cursor(value: str | int | None) -> int:
    if value is None or value == "start":
        return -1
    if isinstance(value, int) and not isinstance(value, bool):
        return require_integer(value, label="cursor", minimum=-1)
    if (
        not isinstance(value, str)
        or len(value) != 13
        or not value.startswith("m")
        or not value[1:].isdigit()
    ):
        raise MailboxError("mailbox cursor must be 'start' or m followed by 12 digits")
    try:
        return int(value[1:])
    except ValueError as exc:  # pragma: no cover - guarded by shape
        raise MailboxError("mailbox cursor is malformed") from exc


def send_message(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    sender_id: str,
    recipient_id: str,
    generation: int,
    kind: str,
    body: Any,
    dedupe_key: str,
    message_id: str | None = None,
    sent_at: str | None = None,
) -> dict[str, Any]:
    """Durably append a redacted message, adopting byte-identical retries."""

    require_safe_id(sender_id, label="sender_id")
    require_safe_id(recipient_id, label="recipient_id")
    require_integer(generation, label="generation", minimum=0)
    require_safe_id(kind, label="kind")
    require_safe_id(dedupe_key, label="dedupe_key")
    redacted = redact_value(body)
    content = {
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "generation": generation,
        "kind": kind,
        "body": redacted,
        "dedupe_key": dedupe_key,
    }
    content_hash = sha256_hex(canonical_json_bytes(content))
    if len(canonical_json_bytes(redacted)) > MAX_MESSAGE_BYTES:
        raise MailboxError("mailbox message exceeds bounded byte limit")
    mid = message_id or f"msg-{content_hash[:32]}"
    require_safe_id(mid, label="message_id")
    path = _recipient_path(root, run_id, team_id, recipient_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        state = _load_locked(
            path, run_id=run_id, team_id=team_id, recipient_id=recipient_id
        )
        previous = state["dedupe"].get(dedupe_key)
        if previous is not None:
            if previous != content_hash:
                raise MailboxError("mailbox dedupe key replayed with different content")
            existing = next(
                item for item in state["messages"] if item["dedupe_key"] == dedupe_key
            )
            return {
                **existing,
                "duplicate": True,
                "cursor": cursor_token(existing["sequence"]),
            }
        if len(state["messages"]) >= MAX_MESSAGES_PER_RECIPIENT:
            raise MailboxError("recipient mailbox reached hard message cap")
        if any(item["message_id"] == mid for item in state["messages"]):
            raise MailboxError("message_id replayed with a different dedupe key")
        sequence = state["next_sequence"]
        message = {
            "message_id": mid,
            "sequence": sequence,
            **content,
            "content_hash": content_hash,
            "sent_at": sent_at or _utc_now(),
        }
        updated = {
            **state,
            "next_sequence": sequence + 1,
            "messages": [*state["messages"], message],
            "dedupe": {**state["dedupe"], dedupe_key: content_hash},
        }
        _validate_mailbox(
            updated, run_id=run_id, team_id=team_id, recipient_id=recipient_id
        )
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
    return {**message, "duplicate": False, "cursor": cursor_token(sequence)}


def list_messages(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    recipient_id: str,
    after: str | int | None = None,
    generation: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List ordered metadata after a cursor without advancing acknowledgement."""

    cursor = parse_cursor(after)
    if generation is not None:
        require_integer(generation, label="generation", minimum=0)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 512:
        raise MailboxError("mailbox list limit must be between 1 and 512")
    path = _recipient_path(root, run_id, team_id, recipient_id)
    state = _load_locked(
        path, run_id=run_id, team_id=team_id, recipient_id=recipient_id
    )
    if cursor >= state["next_sequence"]:
        raise MailboxError("mailbox cursor is in the future")
    available = [
        message
        for message in state["messages"]
        if message["sequence"] > cursor
        and (generation is None or message["generation"] == generation)
    ]
    selected = available[:limit]
    rows = [
        {
            "message_id": item["message_id"],
            "sequence": item["sequence"],
            "sender_id": item["sender_id"],
            "generation": item["generation"],
            "kind": item["kind"],
            "content_hash": item["content_hash"],
            "sent_at": item["sent_at"],
            "cursor": cursor_token(item["sequence"]),
        }
        for item in selected
    ]
    return {
        "messages": rows,
        "ack_cursor": cursor_token(state["ack_cursor"]),
        "next_cursor": cursor_token(selected[-1]["sequence"] if selected else cursor),
        "has_more": len(selected) < len(available),
        "ack_path": [
            {
                "message_id": item["message_id"],
                "generation": item["generation"],
                "cursor": cursor_token(item["sequence"]),
            }
            for item in state["messages"]
            if state["ack_cursor"]
            < item["sequence"]
            <= (selected[-1]["sequence"] if selected else state["ack_cursor"])
        ],
    }


def read_message(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    recipient_id: str,
    message_id: str,
    generation: int | None = None,
) -> dict[str, Any]:
    """Read one immutable message body; reading never advances the cursor."""

    require_safe_id(message_id, label="message_id")
    if generation is not None:
        require_integer(generation, label="generation", minimum=0)
    path = _recipient_path(root, run_id, team_id, recipient_id)
    state = _load_locked(
        path, run_id=run_id, team_id=team_id, recipient_id=recipient_id
    )
    matches = [item for item in state["messages"] if item["message_id"] == message_id]
    if len(matches) != 1:
        raise MailboxError("mailbox message not found")
    message = matches[0]
    if generation is not None and message["generation"] != generation:
        raise MailboxError("mailbox message belongs to a stale generation")
    return {**message, "cursor": cursor_token(message["sequence"])}


def ack_message(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    recipient_id: str,
    message_id: str,
    expected_cursor: str | int | None,
    generation: int,
) -> dict[str, Any]:
    """CAS-ack exactly the next recipient message.

    Same-generation skipping is forbidden; messages from an older generation
    are superseded by the rollover path instead of acknowledged.
    """

    require_safe_id(message_id, label="message_id")
    require_integer(generation, label="generation", minimum=0)
    expected = parse_cursor(expected_cursor)
    path = _recipient_path(root, run_id, team_id, recipient_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        state = _load_locked(
            path, run_id=run_id, team_id=team_id, recipient_id=recipient_id
        )
        target_sequence = expected + 1
        if state["ack_cursor"] != expected:
            # A crash after the canonical write but before the response is a
            # normal retry. Adopt the exact acknowledged message, including a
            # generation rollover that superseded only older generations.
            matches = [
                item for item in state["messages"] if item["message_id"] == message_id
            ]
            if len(matches) == 1:
                prior = matches[0]
                skipped_retry = state["messages"][target_sequence : prior["sequence"]]
                if (
                    state["ack_cursor"] == prior["sequence"]
                    and prior["generation"] == generation
                    and not any(
                        item["generation"] >= generation for item in skipped_retry
                    )
                ):
                    return {
                        "message_id": message_id,
                        "ack_cursor": cursor_token(prior["sequence"]),
                        "duplicate": True,
                    }
            raise MailboxError("mailbox acknowledgement cursor CAS mismatch")
        if target_sequence >= state["next_sequence"]:
            raise MailboxError("no next mailbox message to acknowledge")
        matching = [
            item
            for item in state["messages"][target_sequence:]
            if item["message_id"] == message_id
        ]
        if len(matching) != 1:
            raise MailboxError("mailbox acknowledgement message not found")
        message = matching[0]
        if message["generation"] != generation:
            raise MailboxError("stale generation may not acknowledge mailbox message")
        skipped = state["messages"][target_sequence : message["sequence"]]
        if any(item["generation"] >= generation for item in skipped):
            raise MailboxError("mailbox acknowledgement may not skip messages")
        target_sequence = message["sequence"]
        updated = {**state, "ack_cursor": target_sequence}
        _validate_mailbox(
            updated, run_id=run_id, team_id=team_id, recipient_id=recipient_id
        )
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
    result = {
        "message_id": message_id,
        "ack_cursor": cursor_token(target_sequence),
        "duplicate": False,
    }
    if skipped:
        result["superseded"] = len(skipped)
    return result


__all__ = [
    "MailboxError",
    "ack_message",
    "cursor_token",
    "list_messages",
    "parse_cursor",
    "read_message",
    "send_message",
]
