"""Per-recipient mailbox notify cursor, separate from mailbox schema v1.

``mailbox-mark-notified`` must not add keys to ``team_mailbox`` v1. Notify
state lives under ``notify/<recipient_key>/notify_cursor.json``.
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
    require_exact_keys,
    require_integer,
    require_iso8601,
    require_safe_id,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from omg_cli.team.mailbox import (
    cursor_token,
    parse_cursor,
    read_message,
)


CLI_WRITER = "omg-cli"
NOTIFY_STORE_KIND = "team_mailbox_notify"
NOTIFY_SCHEMA_VERSION = 1
MAX_NOTIFIED = 4096

_NOTIFY_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "recipient_id",
        "notify_cursor",
        "notified",
        "updated_at",
    }
)


class NotifyError(RuntimeError):
    """Notify cursor CAS, identity, or mailbox-binding failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _team_state_dir(root: Path | str, run_id: str, team_id: str) -> Path:
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
    )


def notify_cursor_path(
    root: Path | str, run_id: str, team_id: str, recipient_id: str
) -> Path:
    require_safe_id(recipient_id, label="recipient_id")
    return (
        _team_state_dir(root, run_id, team_id)
        / "notify"
        / safe_path_key(recipient_id, namespace="recipient")
        / "notify_cursor.json"
    )


def _empty_notify(run_id: str, team_id: str, recipient_id: str) -> dict[str, Any]:
    return {
        "store_kind": NOTIFY_STORE_KIND,
        "schema_version": NOTIFY_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "recipient_id": recipient_id,
        "notify_cursor": -1,
        "notified": {},
        "updated_at": _utc_now(),
    }


def _validate_notify(
    value: Mapping[str, Any], *, run_id: str, team_id: str, recipient_id: str
) -> dict[str, Any]:
    row = dict(value)
    require_exact_keys(row, required=_NOTIFY_KEYS, label="team mailbox notify")
    if (
        row["store_kind"] != NOTIFY_STORE_KIND
        or row["schema_version"] != NOTIFY_SCHEMA_VERSION
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("team mailbox notify header mismatch")
    if (
        row["run_id"] != run_id
        or row["team_id"] != team_id
        or row["recipient_id"] != recipient_id
    ):
        raise ContractValidationError("team mailbox notify identity mismatch")
    cursor = require_integer(row["notify_cursor"], label="notify_cursor", minimum=-1)
    require_iso8601(row["updated_at"], label="updated_at")
    notified = row["notified"]
    if not isinstance(notified, dict) or len(notified) > MAX_NOTIFIED:
        raise ContractValidationError("team mailbox notify map is not bounded")
    cleaned: dict[str, int] = {}
    for key, seq in notified.items():
        require_safe_id(key, label="notified.message_id")
        cleaned[key] = require_integer(seq, label="notified.sequence", minimum=0)
        if cleaned[key] > cursor:
            raise ContractValidationError("team mailbox notify sequence is in the future")
    row["notified"] = cleaned
    return row


def _load_locked(
    path: Path, *, run_id: str, team_id: str, recipient_id: str
) -> dict[str, Any]:
    if not path.exists():
        return _empty_notify(run_id, team_id, recipient_id)
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("team mailbox notify must be an object")
    return _validate_notify(
        parsed, run_id=run_id, team_id=team_id, recipient_id=recipient_id
    )


def mark_notified(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    recipient_id: str,
    message_id: str,
    expected_cursor: str | int | None = None,
    generation: int | None = None,
) -> dict[str, Any]:
    """CAS-advance the recipient notify cursor for one existing mailbox message.

    Does not mutate mailbox v1 files.
    """

    require_safe_id(message_id, label="message_id")
    message = read_message(
        root,
        run_id=run_id,
        team_id=team_id,
        recipient_id=recipient_id,
        message_id=message_id,
        generation=generation,
    )
    sequence = require_integer(message["sequence"], label="sequence", minimum=0)
    path = notify_cursor_path(root, run_id, team_id, recipient_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        state = _load_locked(
            path, run_id=run_id, team_id=team_id, recipient_id=recipient_id
        )
        current = int(state["notify_cursor"])
        expected = current if expected_cursor is None else parse_cursor(expected_cursor)
        prior = state["notified"].get(message_id)
        if prior is not None and prior == sequence and current >= sequence:
            if expected != current:
                raise NotifyError("mailbox notify cursor CAS mismatch")
            return {
                "message_id": message_id,
                "notify_cursor": cursor_token(current),
                "sequence": sequence,
                "duplicate": True,
            }
        if expected != current:
            raise NotifyError("mailbox notify cursor CAS mismatch")
        if sequence != current + 1:
            raise NotifyError("mailbox notify may not skip messages")
        updated = {
            **state,
            "notify_cursor": sequence,
            "notified": {**state["notified"], message_id: sequence},
            "updated_at": _utc_now(),
        }
        if len(updated["notified"]) > MAX_NOTIFIED:
            raise NotifyError("mailbox notify map reached hard cap")
        _validate_notify(
            updated, run_id=run_id, team_id=team_id, recipient_id=recipient_id
        )
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
    return {
        "message_id": message_id,
        "notify_cursor": cursor_token(sequence),
        "sequence": sequence,
        "duplicate": False,
    }


__all__ = [
    "NOTIFY_SCHEMA_VERSION",
    "NOTIFY_STORE_KIND",
    "NotifyError",
    "mark_notified",
    "notify_cursor_path",
]
