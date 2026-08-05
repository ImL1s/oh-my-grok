# omg_cli/implementation.py
"""CLI-authoritative implementation-stage receipt (implement→review gate).

Distinct from caller-supplied ``evidence.implementation_receipt`` (unauthenticated
JSON, only accepted under the audited ``break_glass`` escape hatch — see
``autopilot._implementation_work_evidence``). This module writes/reads a real
on-disk stamp under ``.omg/state/runs/<run_id>/stages/implementation.json``
with ``writer == "omg-cli"``. The gate trusts this file without break_glass
only when it was produced under a live ``ExecutionLease`` (``assert_current``)
that also rebound autopilot ``implement_expected_*`` + ``implement_receipt_binder``
in the same lease-guarded operation — the same authority model as the
review/QA stage stamps.

Honesty limit: dual hand-edit of ``autopilot.json`` + ``implementation.json``
under a writable ``.omg/state`` tree remains residual (R14-5 / host deny);
receipt-alone forgery after implement entry (binder cleared to null) is
blocked.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omg_cli.evidence import CLI_WRITER, _atomic_write_json, validate_identifier

if TYPE_CHECKING:
    from omg_cli.state import ExecutionLease


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


def _assert_receipt_stamp_allowed(
    root: Path,
    run_id: str,
    lease: ExecutionLease,
    *,
    require_implement_phase: bool,
) -> None:
    """Fail-closed preconditions for ``stamp_implementation_receipt``.

    Binds the lease to the *target* root/run via ``_require_current_lease``,
    refuses terminal status / pending cancel, and (when an autopilot sidecar
    is being rebound) requires ``phase == "implement"``.
    """
    from omg_cli.state import (
        TERMINAL_STATUSES,
        _require_current_lease,
        load_cancellation_request,
        load_run,
    )

    _require_current_lease(root, run_id, lease)
    run = load_run(root, run_id) or {}
    status = str(run.get("status") or "")
    if status in TERMINAL_STATUSES:
        raise PermissionError(
            "run is terminal; refusing implementation receipt stamp: "
            f"status={status!r}"
        )
    if load_cancellation_request(root, run_id) is not None:
        raise PermissionError(
            "run has a pending cancellation request; "
            "refusing implementation receipt stamp"
        )
    if require_implement_phase:
        from omg_cli.autopilot import load_autopilot

        state = load_autopilot(root, run_id)
        phase = str(state.get("phase") or "")
        if phase != "implement":
            raise PermissionError(
                "autopilot phase must be 'implement' to stamp/rebind "
                f"implementation receipt; got phase={phase!r}"
            )


def stamp_implementation_receipt(
    root: Path | str,
    run_id: str,
    *,
    content_sha256: str,
    lease: ExecutionLease,
    note: str | None = None,
) -> dict[str, Any]:
    """Write a CLI-owned implementation receipt under a live execution lease.

    ``content_sha256`` must be a workspace/product fingerprint the CLI itself
    recomputed (e.g. ``autopilot._implement_workspace_fingerprint(root)``) —
    never a caller-supplied hash, or this would just be a laundered version
    of the unauthenticated inline ``evidence.implementation_receipt`` path.

    ``lease`` must be a held ``ExecutionLease`` bound to the *target*
    ``root``/``run_id`` (``_require_current_lease`` — not merely
    ``assert_current`` on some other run's lease). Refuses terminal status,
    pending cancellation, and (when an autopilot sidecar exists) any phase
    other than ``implement``. After writing the receipt, the sidecar path
    rebinds ``implement_expected_*`` and ``implement_receipt_binder`` under
    the same lease + ``transition_guard`` (re-checking the same invariants)
    so the implement→review gate can require both the on-disk receipt and
    the CLI-written binder (not receipt alone).
    """
    root = Path(root).resolve()
    run_id = validate_identifier(run_id, label="run_id")
    digest = (content_sha256 or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("content_sha256 must be 64 lowercase hex characters")

    from omg_cli.autopilot import (
        _save,
        autopilot_state_path,
        load_autopilot,
    )
    from omg_cli.state import transition_guard

    has_sidecar = autopilot_state_path(root, run_id).is_file()
    # Before any write: bind lease to *this* root/run and refuse unsafe state.
    # Must precede generation/invocation reads so an unheld lease raises
    # FencingError (not a misleading ValueError on generation=0).
    _assert_receipt_stamp_allowed(
        root, run_id, lease, require_implement_phase=has_sidecar
    )
    generation = lease.generation
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("lease.generation must be an int >= 1")
    invocation_id = validate_identifier(lease.invocation_id, label="invocation_id")
    record: dict[str, Any] = {
        "writer": CLI_WRITER,
        "schema_version": 1,
        "run_id": run_id,
        "invocation_id": invocation_id,
        "lease_generation": generation,
        "content_sha256": digest,
        "stamped_at": _utc_now(),
    }
    note = (note or "").strip()
    if note:
        record["note"] = note
    _atomic_write_json(implementation_receipt_path(root, run_id), record)

    # Rebind autopilot expected + binder under the same live lease so a
    # later process can gate on durable CLI-written fields (not a
    # process-private secret). Skip when no autopilot sidecar (e.g. bare
    # receipt unit tests on a non-autopilot run).
    if has_sidecar:
        with transition_guard(root, run_id):
            _assert_receipt_stamp_allowed(
                root, run_id, lease, require_implement_phase=True
            )
            state = load_autopilot(root, run_id)
            state["implement_expected_invocation_id"] = invocation_id
            state["implement_expected_lease_generation"] = generation
            state["implement_receipt_binder"] = {
                "invocation_id": invocation_id,
                "lease_generation": generation,
                "content_sha256": digest,
            }
            _save(root, run_id, state, lease)

    return record


def read_implementation_receipt(
    root: Path | str, run_id: str
) -> dict[str, Any] | None:
    """Return the on-disk receipt only if it is a validly CLI-stamped record.

    Fail-closed: malformed JSON, wrong writer, run_id mismatch, missing or
    empty ``invocation_id``, missing/non-int/``<1`` ``lease_generation``, or
    an ``invalidated`` record all read as "no receipt" rather than raising —
    callers treat this the same as an absent file.
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
    gen = data.get("lease_generation")
    if isinstance(gen, bool) or not isinstance(gen, int) or gen < 1:
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
