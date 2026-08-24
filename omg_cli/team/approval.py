"""Durable per-task approval records. Never writes OMG ``verified``."""

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
    require_iso8601,
    require_safe_id,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from omg_cli.redaction import redact_value


CLI_WRITER = "omg-cli"
APPROVAL_STORE_KIND = "team_task_approval"
APPROVAL_SCHEMA_VERSION = 1
APPROVAL_DECISIONS = frozenset({"approved", "rejected"})
MAX_NOTE_BYTES = 8_192

_APPROVAL_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "task_id",
        "decision",
        "approver",
        "note",
        "task_status",
        "allow_terminal",
        "never_sets_verified",
        "written_at",
    }
)


class ApprovalError(RuntimeError):
    """Task approval contract or terminal-override failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def approval_path(root: Path | str, run_id: str, team_id: str, task_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    require_safe_id(task_id, label="task_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
        / "approvals"
        / (safe_path_key(task_id, namespace="task") + ".json")
    )


def _validate_approval(
    value: Mapping[str, Any], *, run_id: str, team_id: str, task_id: str
) -> dict[str, Any]:
    row = dict(value)
    require_exact_keys(row, required=_APPROVAL_KEYS, label="team task approval")
    if (
        row["store_kind"] != APPROVAL_STORE_KIND
        or row["schema_version"] != APPROVAL_SCHEMA_VERSION
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("team task approval header mismatch")
    if row["run_id"] != run_id or row["team_id"] != team_id or row["task_id"] != task_id:
        raise ContractValidationError("team task approval identity mismatch")
    if row["decision"] not in APPROVAL_DECISIONS:
        raise ContractValidationError("team task approval decision is invalid")
    require_safe_id(row["approver"], label="approver")
    require_safe_id(row["task_status"], label="task_status")
    if not isinstance(row["allow_terminal"], bool):
        raise ContractValidationError("team task approval allow_terminal must be boolean")
    if row["never_sets_verified"] is not True:
        raise ContractValidationError("team task approval must never_sets_verified")
    require_iso8601(row["written_at"], label="written_at")
    if not isinstance(row["note"], (str, type(None))):
        raise ContractValidationError("team task approval note must be a string or null")
    return row


def read_task_approval(
    root: Path | str, *, run_id: str, team_id: str, task_id: str
) -> dict[str, Any] | None:
    path = approval_path(root, run_id, team_id, task_id)
    if not path.is_file():
        return None
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("team task approval must be an object")
    return _validate_approval(
        parsed, run_id=run_id, team_id=team_id, task_id=task_id
    )


def write_task_approval(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    decision: str,
    task_status: str,
    approver: str = "leader",
    note: str | None = None,
    allow_terminal: bool = False,
) -> dict[str, Any]:
    """Persist one approval. Refuses completed/failed unless allow_terminal."""

    require_safe_id(task_id, label="task_id")
    if decision not in APPROVAL_DECISIONS:
        raise ApprovalError("decision must be 'approved' or 'rejected'")
    require_safe_id(task_status, label="task_status")
    require_safe_id(approver, label="approver")
    if not isinstance(allow_terminal, bool):
        raise ApprovalError("allow_terminal must be a boolean")
    terminal = task_status in {"completed", "failed"}
    if terminal and not allow_terminal:
        raise ApprovalError(
            "cannot approve a completed/failed task without allow_terminal"
        )
    redacted_note: str | None
    if note is None:
        redacted_note = None
    elif not isinstance(note, str):
        raise ApprovalError("note must be a string when provided")
    else:
        redacted = redact_value(note)
        if not isinstance(redacted, str):
            redacted = str(redacted)
        if len(redacted.encode("utf-8")) > MAX_NOTE_BYTES:
            raise ApprovalError("approval note exceeds bounded byte limit")
        redacted_note = redacted
    path = approval_path(root, run_id, team_id, task_id)
    ensure_managed_dir(path.parent)
    record = {
        "store_kind": APPROVAL_STORE_KIND,
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "task_id": task_id,
        "decision": decision,
        "approver": approver,
        "note": redacted_note,
        "task_status": task_status,
        "allow_terminal": allow_terminal,
        "never_sets_verified": True,
        "written_at": _utc_now(),
    }
    _validate_approval(record, run_id=run_id, team_id=team_id, task_id=task_id)
    with exclusive_lock(path.with_suffix(".lock")):
        atomic_write_bytes(
            path, canonical_json_bytes(record), mode=DATA_FILE_MODE, replace=True
        )
    return {**record, "path": str(path)}


__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVAL_SCHEMA_VERSION",
    "APPROVAL_STORE_KIND",
    "ApprovalError",
    "approval_path",
    "read_task_approval",
    "write_task_approval",
]
