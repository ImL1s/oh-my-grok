"""Fail-closed Team worker I/O capability contract (#147 PR1).

I/O classification is **independent** of pane/job topology. Readers normalize
missing/legacy fields to unproven/unsupported. Writers of new supervisor-owned
panes should stamp headless defaults; only the leader CLI may promote a worker
to interactive after proven ownership (PR2+).

Workers and descriptors must never self-promote ``operator_input_supported``
or ``input_ready`` to true via untrusted stdout scraping alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping

# ---------------------------------------------------------------------------
# Public enums (string constants — stable on the wire)
# ---------------------------------------------------------------------------

IO_MODE_INTERACTIVE_TTY: Final = "interactive_tty"
IO_MODE_HEADLESS_STREAM: Final = "headless_stream"
IO_MODE_BACKGROUND_JOB: Final = "background_job"
IO_MODE_UNPROVEN: Final = "unproven"

IO_MODES: Final[frozenset[str]] = frozenset(
    {
        IO_MODE_INTERACTIVE_TTY,
        IO_MODE_HEADLESS_STREAM,
        IO_MODE_BACKGROUND_JOB,
        IO_MODE_UNPROVEN,
    }
)

TTY_OWNER_PROVIDER: Final = "provider"
TTY_OWNER_SUPERVISOR: Final = "supervisor"
TTY_OWNER_NONE: Final = "none"
TTY_OWNER_UNKNOWN: Final = "unknown"

TTY_OWNERS: Final[frozenset[str]] = frozenset(
    {
        TTY_OWNER_PROVIDER,
        TTY_OWNER_SUPERVISOR,
        TTY_OWNER_NONE,
        TTY_OWNER_UNKNOWN,
    }
)

INTERACTION_EVIDENCE_SCHEMA: Final = "team_worker_interaction_evidence_v1"

# Stable public operator error codes (#147).
E_OPERATOR_INPUT_UNSUPPORTED: Final = "E_OPERATOR_INPUT_UNSUPPORTED"
E_OPERATOR_KEY_UNSUPPORTED: Final = "E_OPERATOR_KEY_UNSUPPORTED"
E_OPERATOR_INPUT_NOT_READY: Final = "E_OPERATOR_INPUT_NOT_READY"

OperatorAction = Literal["input", "key"]


@dataclass(frozen=True, slots=True)
class WorkerIoCapability:
    """Normalized I/O capability for one worker attempt/generation."""

    io_mode: str
    provider_tty_owner: str
    input_ready: bool
    operator_input_supported: bool
    interaction_evidence: dict[str, Any] | None

    def as_public_dict(self) -> dict[str, Any]:
        """Bounded public projection (no raw text, no secrets)."""
        return {
            "io_mode": self.io_mode,
            "provider_tty_owner": self.provider_tty_owner,
            "input_ready": self.input_ready,
            "operator_input_supported": self.operator_input_supported,
            "interaction_evidence": self.interaction_evidence,
        }


@dataclass(frozen=True, slots=True)
class IoCapabilityRefusal:
    """Typed refuse decision for operator input/key (map to OperatorError)."""

    code: str
    message: str
    details: dict[str, Any]


def supervisor_pane_io_defaults() -> WorkerIoCapability:
    """Defaults for new/current supervisor-owned headless panes (PR1 writers)."""
    return WorkerIoCapability(
        io_mode=IO_MODE_HEADLESS_STREAM,
        provider_tty_owner=TTY_OWNER_SUPERVISOR,
        input_ready=False,
        operator_input_supported=False,
        interaction_evidence=None,
    )


def background_job_io_defaults() -> WorkerIoCapability:
    """Defaults for job-topology workers without a pane TTY."""
    return WorkerIoCapability(
        io_mode=IO_MODE_BACKGROUND_JOB,
        provider_tty_owner=TTY_OWNER_NONE,
        input_ready=False,
        operator_input_supported=False,
        interaction_evidence=None,
    )


def unproven_io_defaults() -> WorkerIoCapability:
    """Fail-closed defaults for legacy/missing I/O fields."""
    return WorkerIoCapability(
        io_mode=IO_MODE_UNPROVEN,
        provider_tty_owner=TTY_OWNER_UNKNOWN,
        input_ready=False,
        operator_input_supported=False,
        interaction_evidence=None,
    )


def stamp_io_capability(
    target: dict[str, Any],
    cap: WorkerIoCapability | None = None,
) -> dict[str, Any]:
    """Write flat I/O fields onto a CLI-owned dict (descriptor / task row).

    Mutates *target* in place and returns it. Callers must only use this from
    leader/CLI write paths — never from worker self-report.
    """
    resolved = cap if cap is not None else supervisor_pane_io_defaults()
    target["io_mode"] = resolved.io_mode
    target["provider_tty_owner"] = resolved.provider_tty_owner
    target["input_ready"] = bool(resolved.input_ready)
    target["operator_input_supported"] = bool(resolved.operator_input_supported)
    target["interaction_evidence"] = resolved.interaction_evidence
    return target


def io_defaults_for_worker_topology(topology: str | None) -> WorkerIoCapability:
    """Map launch topology → PR1 writer defaults (independent of pane visibility).

    ``pane`` (supervisor-owned) → headless_stream / supervisor.
    ``job`` → background_job / none.
    Anything else / missing → unproven (readers still fail closed).
    """
    key = str(topology or "").strip().lower()
    if key in {"pane", "tmux", "supervisor"}:
        return supervisor_pane_io_defaults()
    if key in {"job", "background", "background_job"}:
        return background_job_io_defaults()
    return unproven_io_defaults()


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _normalize_io_mode(raw: Any) -> str:
    if not isinstance(raw, str):
        return IO_MODE_UNPROVEN
    key = raw.strip()
    if key in IO_MODES:
        return key
    return IO_MODE_UNPROVEN


def _normalize_tty_owner(raw: Any) -> str:
    if not isinstance(raw, str):
        return TTY_OWNER_UNKNOWN
    key = raw.strip()
    if key in TTY_OWNERS:
        return key
    return TTY_OWNER_UNKNOWN


def _normalize_interaction_evidence(
    raw: Any,
    *,
    attempt: int | None = None,
    generation: int | None = None,
) -> dict[str, Any] | None:
    """Return evidence only when schema + optional attempt/generation bind.

    Stale attempt/generation → treat as null (not ready).
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return None
    schema = raw.get("schema")
    if schema != INTERACTION_EVIDENCE_SCHEMA:
        return None
    ev_attempt = raw.get("attempt")
    ev_generation = raw.get("generation")
    if attempt is not None:
        if (
            isinstance(ev_attempt, bool)
            or not isinstance(ev_attempt, int)
            or ev_attempt != attempt
        ):
            return None
    if generation is not None:
        if (
            isinstance(ev_generation, bool)
            or not isinstance(ev_generation, int)
            or ev_generation != generation
        ):
            return None
    # Bound copy — never pass through unknown large blobs wholesale.
    out: dict[str, Any] = {"schema": INTERACTION_EVIDENCE_SCHEMA}
    if isinstance(ev_attempt, int) and not isinstance(ev_attempt, bool):
        out["attempt"] = ev_attempt
    if isinstance(ev_generation, int) and not isinstance(ev_generation, bool):
        out["generation"] = ev_generation
    ready_marker = raw.get("ready_marker")
    if ready_marker is None or isinstance(ready_marker, str):
        out["ready_marker"] = ready_marker
    proven_at = raw.get("proven_at")
    if proven_at is None or isinstance(proven_at, str):
        out["proven_at"] = proven_at
    pane_id = raw.get("pane_id")
    if pane_id is None or isinstance(pane_id, str):
        out["pane_id"] = pane_id
    provider_pid = raw.get("provider_pid")
    if provider_pid is None:
        out["provider_pid"] = None
    elif isinstance(provider_pid, int) and not isinstance(provider_pid, bool) and provider_pid > 0:
        out["provider_pid"] = provider_pid
    else:
        out["provider_pid"] = None
    return out


def normalize_worker_io_capability(
    row: Mapping[str, Any] | None,
    *,
    attempt: int | None = None,
    generation: int | None = None,
) -> WorkerIoCapability:
    """Fail-closed normalize of a task/worker/descriptor I/O row.

    Missing or invalid fields → unproven / unknown / false / false / null.
    Never infers interactivity from provider name, needs_pty, pane visibility,
    or startup_status.
    """
    if row is None or not isinstance(row, Mapping):
        return unproven_io_defaults()

    # Prefer explicit I/O block when present; else flat keys on the row.
    source: Mapping[str, Any] = row
    nested = row.get("io_capability")
    if isinstance(nested, Mapping):
        source = nested

    has_any = any(
        key in source
        for key in (
            "io_mode",
            "provider_tty_owner",
            "input_ready",
            "operator_input_supported",
            "interaction_evidence",
        )
    )
    if not has_any:
        # Also check flat keys on outer row when nested missing.
        has_any = any(
            key in row
            for key in (
                "io_mode",
                "provider_tty_owner",
                "input_ready",
                "operator_input_supported",
                "interaction_evidence",
            )
        )
        if has_any:
            source = row
        else:
            return unproven_io_defaults()

    io_mode = _normalize_io_mode(source.get("io_mode"))
    owner = _normalize_tty_owner(source.get("provider_tty_owner"))
    input_ready = _as_bool(source.get("input_ready"), default=False)
    supported = _as_bool(source.get("operator_input_supported"), default=False)
    evidence = _normalize_interaction_evidence(
        source.get("interaction_evidence"),
        attempt=attempt,
        generation=generation,
    )

    # Product policy: supported requires interactive_tty; never auto-promote.
    if supported and io_mode != IO_MODE_INTERACTIVE_TTY:
        supported = False
    if io_mode != IO_MODE_INTERACTIVE_TTY:
        input_ready = False

    return WorkerIoCapability(
        io_mode=io_mode,
        provider_tty_owner=owner,
        input_ready=input_ready,
        operator_input_supported=supported,
        interaction_evidence=evidence,
    )


def operator_input_refusal(
    cap: WorkerIoCapability,
    *,
    action: OperatorAction = "input",
) -> IoCapabilityRefusal | None:
    """Return a refusal if operator pane input/key is not allowed; else None.

    Gate is independent of CLI TTY / ``--operator-override`` (override must not
    bypass this check).
    """
    public = cap.as_public_dict()
    if (
        not cap.operator_input_supported
        or cap.io_mode != IO_MODE_INTERACTIVE_TTY
    ):
        if action == "key":
            return IoCapabilityRefusal(
                code=E_OPERATOR_KEY_UNSUPPORTED,
                message=(
                    "worker does not support operator pane keys "
                    f"(io_mode={cap.io_mode!r}, operator_input_supported="
                    f"{cap.operator_input_supported}); prefer omg team api "
                    "send-message for automation"
                ),
                details=public,
            )
        return IoCapabilityRefusal(
            code=E_OPERATOR_INPUT_UNSUPPORTED,
            message=(
                "worker does not support operator pane input "
                f"(io_mode={cap.io_mode!r}, operator_input_supported="
                f"{cap.operator_input_supported}); prefer omg team api "
                "send-message for automation"
            ),
            details=public,
        )
    if not cap.input_ready:
        return IoCapabilityRefusal(
            code=E_OPERATOR_INPUT_NOT_READY,
            message=(
                "worker operator input is supported but not ready "
                f"(input_ready={cap.input_ready}); wait for interactive "
                "readiness or prefer omg team api send-message"
            ),
            details=public,
        )
    return None


def assert_operator_input_allowed(
    cap: WorkerIoCapability,
    *,
    action: OperatorAction = "input",
) -> None:
    """Raise :class:`IoCapabilityRefuseError` when input/key must not proceed."""
    refusal = operator_input_refusal(cap, action=action)
    if refusal is not None:
        raise IoCapabilityRefuseError(
            refusal.message,
            code=refusal.code,
            details=refusal.details,
        )


class IoCapabilityRefuseError(RuntimeError):
    """Capability-layer refuse (map to OperatorError at the operator boundary)."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


__all__ = [
    "E_OPERATOR_INPUT_NOT_READY",
    "E_OPERATOR_INPUT_UNSUPPORTED",
    "E_OPERATOR_KEY_UNSUPPORTED",
    "INTERACTION_EVIDENCE_SCHEMA",
    "IO_MODES",
    "IO_MODE_BACKGROUND_JOB",
    "IO_MODE_HEADLESS_STREAM",
    "IO_MODE_INTERACTIVE_TTY",
    "IO_MODE_UNPROVEN",
    "IoCapabilityRefuseError",
    "IoCapabilityRefusal",
    "TTY_OWNERS",
    "TTY_OWNER_NONE",
    "TTY_OWNER_PROVIDER",
    "TTY_OWNER_SUPERVISOR",
    "TTY_OWNER_UNKNOWN",
    "WorkerIoCapability",
    "assert_operator_input_allowed",
    "background_job_io_defaults",
    "io_defaults_for_worker_topology",
    "normalize_worker_io_capability",
    "operator_input_refusal",
    "stamp_io_capability",
    "supervisor_pane_io_defaults",
    "unproven_io_defaults",
]
