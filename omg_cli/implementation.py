# omg_cli/implementation.py
"""CLI-authoritative implementation-stage receipt (implement→review gate).

Distinct from caller-supplied ``evidence.implementation_receipt`` (unauthenticated
JSON, only accepted under the audited ``break_glass`` escape hatch — see
``autopilot._implementation_work_evidence``). This module writes/reads a real
on-disk stamp under ``.omg/state/runs/<run_id>/stages/implementation.json``
with ``writer == "omg-cli"``. The gate trusts this file without break_glass
because only ``stamp_implementation_receipt`` (a CLI-side helper, never driven
by raw model/host JSON) can create it — the same authority model as the
review/QA stage stamps.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.evidence import CLI_WRITER, _atomic_write_json, validate_identifier


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def implementation_receipt_path(root: Path | str, run_id: str) -> Path:
    run_id = validate_identifier(run_id, label="run_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
        / "implementation.json"
    )


def stamp_implementation_receipt(
    root: Path | str,
    run_id: str,
    *,
    content_sha256: str,
    invocation_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Write a CLI-owned implementation receipt (same-process trust only).

    ``content_sha256`` must be a workspace/product fingerprint the CLI itself
    recomputed (e.g. ``autopilot._implement_workspace_fingerprint(root)``) —
    never a caller-supplied hash, or this would just be a laundered version
    of the unauthenticated inline ``evidence.implementation_receipt`` path.

    ``invocation_id`` must be the active execution-lease id (binds the receipt
    to a real CLI invocation so a hand-written ``writer=omg-cli`` file cannot
    forge the gate).
    """
    root = Path(root).resolve()
    run_id = validate_identifier(run_id, label="run_id")
    invocation_id = validate_identifier(invocation_id, label="invocation_id")
    digest = (content_sha256 or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("content_sha256 must be 64 lowercase hex characters")
    record: dict[str, Any] = {
        "writer": CLI_WRITER,
        "schema_version": 1,
        "run_id": run_id,
        "invocation_id": invocation_id,
        "content_sha256": digest,
        "stamped_at": _utc_now(),
    }
    note = (note or "").strip()
    if note:
        record["note"] = note
    _atomic_write_json(implementation_receipt_path(root, run_id), record)
    return record


def read_implementation_receipt(
    root: Path | str, run_id: str
) -> dict[str, Any] | None:
    """Return the on-disk receipt only if it is a validly CLI-stamped record.

    Fail-closed: malformed JSON, wrong writer, run_id mismatch, missing or
    empty ``invocation_id``, or an ``invalidated`` record all read as "no
    receipt" rather than raising — callers treat this the same as an absent
    file.
    """
    run_id = validate_identifier(run_id, label="run_id")
    path = implementation_receipt_path(root, run_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("writer") != CLI_WRITER or data.get("run_id") != run_id:
        return None
    inv = data.get("invocation_id")
    if not isinstance(inv, str) or not inv.strip():
        return None
    if data.get("invalidated") is True:
        return None
    return data


def invalidate_implementation_receipt(
    root: Path | str, run_id: str, *, reason: str
) -> None:
    """Mark any existing on-disk receipt stale on (re)entering ``implement``.

    A receipt stamped during a prior implement cycle must never satisfy the
    implement→review work gate for a later cycle whose workspace fingerprint
    happens to still match (e.g. ``review → ralplan → implement`` with no new
    product changes) — that would let a stale receipt substitute for real
    work without ``break_glass``. Mirrors ``autopilot.invalidate_quality_stages``:
    mark in place (audit trail preserved) rather than delete. No-op when no
    valid CLI-stamped receipt exists yet.
    """
    root = Path(root).resolve()
    run_id = validate_identifier(run_id, label="run_id")
    path = implementation_receipt_path(root, run_id)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("writer") != CLI_WRITER:
        return
    if data.get("run_id") != run_id:
        return
    data["invalidated"] = True
    data["invalidated_reason"] = reason
    data["invalidated_at"] = _utc_now()
    _atomic_write_json(path, data)


__all__ = [
    "implementation_receipt_path",
    "invalidate_implementation_receipt",
    "read_implementation_receipt",
    "stamp_implementation_receipt",
]
