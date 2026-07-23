"""Durable Grok host-session binding for resumable OMG workflows.

The CLI allocates the UUID before the first host launch, persists the binding
in authoritative run state, and then derives exactly one of ``--session-id``
or ``--resume`` from the persisted attempt count.  This module deliberately
does not own lifecycle state or acceptance authority.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any

from omg_cli.contracts.state_schemas import require_integer, require_safe_id, require_sha256


class HostSessionError(ValueError):
    """A persisted host-session binding is missing or malformed."""


@dataclass(frozen=True)
class HostSessionBinding:
    """Validated, process-independent Grok session binding."""

    session_id: str
    attempts: int = 0
    state: str = "allocated"

    @property
    def is_first_launch(self) -> bool:
        return self.attempts == 0

    def launch_argv(self) -> list[str]:
        """Return the one legal host continuity flag for the next attempt."""
        flag = "--session-id" if self.is_first_launch else "--resume"
        return [flag, self.session_id]

    def attempted(self) -> "HostSessionBinding":
        """Return the binding to persist immediately before host launch."""
        return HostSessionBinding(
            session_id=self.session_id,
            attempts=self.attempts + 1,
            state="launched",
        )

    def status_fields(self) -> dict[str, Any]:
        return {
            "grok_session_id": self.session_id,
            "grok_session_attempts": self.attempts,
            "grok_session_state": self.state,
        }


def _validated_uuid(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HostSessionError("grok session id must be a non-empty UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HostSessionError(f"invalid Grok session UUID: {value!r}") from exc
    # Grok accepts canonical UUID text.  Normalize persisted historical case.
    return str(parsed)


def allocate_host_session() -> HostSessionBinding:
    """Preallocate a new resumable Grok UUID before any host process starts."""
    return HostSessionBinding(session_id=str(uuid.uuid4()))


def load_host_session(run: dict[str, Any], *, required: bool = True) -> HostSessionBinding | None:
    """Load and validate a binding from authoritative run state.

    ``required=True`` is used for process-level resume and intentionally never
    allocates a replacement when the binding is absent or corrupt.
    """
    raw_id = run.get("grok_session_id")
    if raw_id is None:
        if required:
            raise HostSessionError(
                "run has no persisted Grok session binding; refusing silent new session"
            )
        return None

    session_id = _validated_uuid(raw_id)
    raw_attempts = run.get("grok_session_attempts", 0)
    if isinstance(raw_attempts, bool) or not isinstance(raw_attempts, int):
        raise HostSessionError("grok_session_attempts must be a non-negative integer")
    if raw_attempts < 0:
        raise HostSessionError("grok_session_attempts must not be negative")
    raw_state = run.get("grok_session_state", "allocated")
    if not isinstance(raw_state, str) or raw_state not in {
        "allocated",
        "launched",
        "resumable",
        "blocked",
        "closed",
    }:
        raise HostSessionError(f"invalid grok_session_state: {raw_state!r}")
    return HostSessionBinding(session_id=session_id, attempts=raw_attempts, state=raw_state)


def session_flag_argv(
    *,
    new_session_id: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """Validate and return mutually exclusive Grok session flags."""
    if new_session_id is not None and resume_session_id is not None:
        raise HostSessionError("cannot pass both --session-id and --resume")
    if new_session_id is not None:
        return ["--session-id", _validated_uuid(new_session_id)]
    if resume_session_id is not None:
        return ["--resume", _validated_uuid(resume_session_id)]
    return []


def session_route_argv(
    *,
    create_session_id: str | None = None,
    resume_session_id: str | None = None,
    continue_best_effort: bool = False,
    fork_session: bool = False,
    new_session_id: str | None = None,
    existing_session_ids: Iterable[str] = (),
) -> list[str]:
    """Return exactly one legal Grok create/resume/continue/fork route."""

    existing = {_validated_uuid(value) for value in existing_session_ids}
    if continue_best_effort:
        if fork_session:
            if create_session_id is not None or resume_session_id is not None or new_session_id is None:
                raise HostSessionError("best-effort fork requires one new --session-id")
            child = _validated_uuid(new_session_id)
            if child in existing:
                raise HostSessionError("fork session id already exists")
            return ["--continue", "--fork-session", "--session-id", child]
        if any((create_session_id, resume_session_id, new_session_id)):
            raise HostSessionError("--continue cannot be combined with an explicit route")
        return ["--continue"]
    if fork_session:
        if create_session_id is not None or resume_session_id is None or new_session_id is None:
            raise HostSessionError("fork requires --resume parent and a new --session-id")
        parent = _validated_uuid(resume_session_id)
        child = _validated_uuid(new_session_id)
        if child == parent or child in existing:
            raise HostSessionError("fork session id already exists")
        return ["--resume", parent, "--fork-session", "--session-id", child]
    if new_session_id is not None:
        raise HostSessionError("new_session_id is only valid for a named fork")
    if create_session_id is not None and resume_session_id is not None:
        raise HostSessionError("cannot combine create and resume routes")
    if create_session_id is not None:
        created = _validated_uuid(create_session_id)
        if created in existing:
            raise HostSessionError("session id already exists")
        return ["--session-id", created]
    if resume_session_id is not None:
        return ["--resume", _validated_uuid(resume_session_id)]
    raise HostSessionError("one create, resume, continue, or fork route is required")


def bind_session_lineage(
    *,
    session_id: str,
    parent_session_id: str | None,
    run_id: str,
    cwd_hash: str,
    generation: int,
    spawn_receipt_hash: str,
    role_receipt_hash: str,
    observed_session_id: str,
) -> dict[str, Any]:
    """Bind native session identity to run/cwd generation and signed receipts."""

    session = _validated_uuid(session_id)
    observed = _validated_uuid(observed_session_id)
    if observed != session:
        raise HostSessionError("observed native session does not match allocated session")
    parent = _validated_uuid(parent_session_id) if parent_session_id is not None else None
    try:
        require_safe_id(run_id, label="run_id")
        require_sha256(cwd_hash, label="cwd_hash")
        require_sha256(spawn_receipt_hash, label="spawn_receipt_hash")
        require_sha256(role_receipt_hash, label="role_receipt_hash")
        require_integer(generation, label="generation", minimum=0)
    except (TypeError, ValueError) as exc:
        raise HostSessionError(str(exc)) from exc
    return {
        "store_kind": "host_session_lineage",
        "schema_version": 1,
        "session_id": session,
        "parent_session_id": parent,
        "run_id": run_id,
        "cwd_hash": cwd_hash,
        "generation": generation,
        "spawn_receipt_hash": spawn_receipt_hash,
        "role_receipt_hash": role_receipt_hash,
    }


__all__ = [
    "HostSessionBinding",
    "HostSessionError",
    "allocate_host_session",
    "bind_session_lineage",
    "load_host_session",
    "session_flag_argv",
    "session_route_argv",
]
