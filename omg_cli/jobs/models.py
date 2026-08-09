"""Job state machine + schema v1 (#68 PR1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


JOB_SCHEMA = 1

JobStateName = str  # queued|starting|running|succeeded|failed|cancelled|lost


class JobState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


TERMINAL_STATES: frozenset[JobState] = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.LOST,
    }
)

# Immutable transitions only (no reverse).
LEGAL_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.STARTING}),
    JobState.STARTING: frozenset(
        {
            JobState.RUNNING,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.LOST,  # #68 PR4 recovery after spawn-identity proof
        }
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.LOST,
        }
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.LOST: frozenset(),
}

IMMUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "job_id",
        "created_at",
        "provider",
        "role",
        "schema",
        "request",
        "attempt_budget",
    }
)

# Retry classification values (schema v1 additive; public retry remains explicit).
RETRY_CLASS_AUTOMATIC = "automatic"
RETRY_CLASS_MANUAL_ONLY = "manual_only"
RETRY_CLASS_NEVER = "never"
RETRY_CLASS_UNKNOWN = "unknown"
RETRY_CLASSES: frozenset[str] = frozenset(
    {
        RETRY_CLASS_AUTOMATIC,
        RETRY_CLASS_MANUAL_ONLY,
        RETRY_CLASS_NEVER,
        RETRY_CLASS_UNKNOWN,
    }
)

PROVIDER_PROCESS_STATES: frozenset[str] = frozenset(
    {"pending", "launching", "bound", "exited"}
)


def default_provider_process() -> dict[str, Any]:
    """Empty provider-process binding (inner agy group not yet launched)."""
    return {
        "state": "pending",
        "pid": None,
        "pgid": None,
        "pid_starttime": None,
        "handle": None,
        "bound_at": None,
        "exited_at": None,
    }


def default_request() -> dict[str, Any]:
    """Safe defaults for PR1 records that lack an immutable request snapshot."""
    return {
        "output_format": "text",
        "model": None,
        "effort": None,
        "mode": None,
        "timeout_s": 3600.0,
        "provider_binary": None,
        "provider_version": None,
        "provider_compat": None,
        "provider_pin_revision": None,
    }


class JobStoreError(ValueError):
    """Malformed / missing / fail-closed job store error."""

    code: str = "E_JOB_STORE"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class TransitionError(JobStoreError):
    """Illegal state transition."""

    code = "E_JOB_TRANSITION"


def assert_transition(current: JobState, nxt: JobState) -> None:
    allowed = LEGAL_TRANSITIONS.get(current, frozenset())
    if nxt not in allowed:
        raise TransitionError(
            f"illegal job transition {current.value} -> {nxt.value}",
            code="E_JOB_TRANSITION",
        )


@dataclass
class JobRecord:
    """Schema v1 job.json record (mutable fields may change under lock)."""

    job_id: str
    created_at: str
    provider: str
    role: str
    state: JobState
    attempt: int = 1
    schema: int = JOB_SCHEMA
    generation: int = 0
    pid: int | None = None
    pgid: int | None = None
    handle: str | None = None
    # Best-effort process start fingerprint for cancel ownership (PR1).
    # Null when the start-time probe failed — cancel then falls back to pid/pgid only.
    pid_starttime: str | None = None
    prompt: str = "prompt.md"
    result: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    exit: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    stdout: str = "stdout.jsonl"
    stderr: str = "stderr.jsonl"
    events: str = "events.jsonl"
    run_id: str | None = None
    updated_at: str | None = None
    cancel_reason: str | None = None
    cancel_requested_at: str | None = None
    worker: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    # Immutable provider request snapshot (optional on PR1 records).
    request: dict[str, Any] = field(default_factory=default_request)
    # Inner provider process group binding (agy); separate from outer runner.
    provider_process: dict[str, Any] = field(default_factory=default_provider_process)
    # Bounded session / resume metadata from provider result (not Team).
    session: dict[str, Any] | None = None
    # #68 PR3 — additive retry / retention metadata (schema v1; no bump).
    attempt_budget: int = 1
    retry_class: str | None = None
    retry_reason: str | None = None
    terminal_at: str | None = None
    attempt_started_at: str | None = None
    # #68 PR4 — additive owner lease / recovery metadata (schema v1; no bump).
    owner_lease: dict[str, Any] | None = None
    last_observed_at: str | None = None
    recovery: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": int(self.schema),
            "job_id": self.job_id,
            "created_at": self.created_at,
            "provider": self.provider,
            "role": self.role,
            "state": self.state.value,
            "attempt": int(self.attempt),
            "attempt_budget": int(self.attempt_budget),
            "generation": int(self.generation),
            "pid": self.pid,
            "pgid": self.pgid,
            "handle": self.handle,
            "pid_starttime": self.pid_starttime,
            "prompt": self.prompt,
            "result": self.result,
            "artifacts": list(self.artifacts),
            "exit": dict(self.exit) if isinstance(self.exit, dict) else self.exit,
            "usage": dict(self.usage) if isinstance(self.usage, dict) else self.usage,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "events": self.events,
            "run_id": self.run_id,
            "updated_at": self.updated_at,
            "cancel_reason": self.cancel_reason,
            "cancel_requested_at": self.cancel_requested_at,
            "worker": dict(self.worker) if self.worker else {},
            "error_message": self.error_message,
            "request": dict(self.request) if self.request else default_request(),
            "provider_process": (
                dict(self.provider_process)
                if self.provider_process
                else default_provider_process()
            ),
            "session": dict(self.session) if isinstance(self.session, dict) else self.session,
            "retry_class": self.retry_class,
            "retry_reason": self.retry_reason,
            "terminal_at": self.terminal_at,
            "attempt_started_at": self.attempt_started_at,
            "owner_lease": (
                dict(self.owner_lease) if isinstance(self.owner_lease, dict) else None
            ),
            "last_observed_at": self.last_observed_at,
            "recovery": (
                dict(self.recovery) if isinstance(self.recovery, dict) else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobRecord:
        if not isinstance(data, Mapping):
            raise JobStoreError("job.json must be an object", code="E_JOB_MALFORMED")
        try:
            schema = int(data.get("schema", 0))
        except (TypeError, ValueError) as exc:
            raise JobStoreError("job.json schema invalid", code="E_JOB_MALFORMED") from exc
        if schema != JOB_SCHEMA:
            raise JobStoreError(
                f"unsupported job schema {schema!r}",
                code="E_JOB_MALFORMED",
            )
        job_id = data.get("job_id")
        created_at = data.get("created_at")
        provider = data.get("provider")
        role = data.get("role")
        state_raw = data.get("state")
        if not all(
            isinstance(v, str) and v for v in (job_id, created_at, provider, role, state_raw)
        ):
            raise JobStoreError(
                "job.json missing required immutable fields",
                code="E_JOB_MALFORMED",
            )
        try:
            state = JobState(str(state_raw))
        except ValueError as exc:
            raise JobStoreError(
                f"unknown job state {state_raw!r}",
                code="E_JOB_MALFORMED",
            ) from exc
        try:
            attempt = int(data.get("attempt", 1))
            generation = int(data.get("generation", 0))
            attempt_budget = int(data.get("attempt_budget", 1))
        except (TypeError, ValueError) as exc:
            raise JobStoreError(
                "job.json attempt/generation/attempt_budget invalid",
                code="E_JOB_MALFORMED",
            ) from exc
        if attempt < 1 or attempt_budget < 1:
            raise JobStoreError(
                "job.json attempt/attempt_budget must be >= 1",
                code="E_JOB_MALFORMED",
            )

        pid = data.get("pid")
        pgid = data.get("pgid")
        if pid is not None:
            try:
                pid = int(pid)
            except (TypeError, ValueError) as exc:
                raise JobStoreError("job.json pid invalid", code="E_JOB_MALFORMED") from exc
        if pgid is not None:
            try:
                pgid = int(pgid)
            except (TypeError, ValueError) as exc:
                raise JobStoreError("job.json pgid invalid", code="E_JOB_MALFORMED") from exc

        artifacts = data.get("artifacts") or []
        if not isinstance(artifacts, list):
            raise JobStoreError(
                "job.json artifacts must be a list",
                code="E_JOB_MALFORMED",
            )

        worker = data.get("worker") or {}
        if worker is not None and not isinstance(worker, dict):
            raise JobStoreError("job.json worker must be an object", code="E_JOB_MALFORMED")

        exit_obj = data.get("exit")
        if exit_obj is not None and not isinstance(exit_obj, dict):
            raise JobStoreError("job.json exit must be an object", code="E_JOB_MALFORMED")
        usage = data.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise JobStoreError("job.json usage must be an object", code="E_JOB_MALFORMED")

        request_raw = data.get("request")
        if request_raw is None:
            request = default_request()
        elif not isinstance(request_raw, dict):
            raise JobStoreError("job.json request must be an object", code="E_JOB_MALFORMED")
        else:
            request = {**default_request(), **dict(request_raw)}

        pp_raw = data.get("provider_process")
        if pp_raw is None:
            provider_process = default_provider_process()
        elif not isinstance(pp_raw, dict):
            raise JobStoreError(
                "job.json provider_process must be an object",
                code="E_JOB_MALFORMED",
            )
        else:
            provider_process = {**default_provider_process(), **dict(pp_raw)}
            pp_state = provider_process.get("state")
            if pp_state not in PROVIDER_PROCESS_STATES:
                raise JobStoreError(
                    f"job.json provider_process.state invalid: {pp_state!r}",
                    code="E_JOB_MALFORMED",
                )

        session = data.get("session")
        if session is not None and not isinstance(session, dict):
            raise JobStoreError("job.json session must be an object", code="E_JOB_MALFORMED")

        retry_class = data.get("retry_class")
        if retry_class is not None:
            if not isinstance(retry_class, str) or retry_class not in RETRY_CLASSES:
                raise JobStoreError(
                    f"job.json retry_class invalid: {retry_class!r}",
                    code="E_JOB_MALFORMED",
                )

        owner_lease_raw = data.get("owner_lease")
        owner_lease: dict[str, Any] | None
        if owner_lease_raw is None:
            owner_lease = None
        elif not isinstance(owner_lease_raw, Mapping):
            raise JobStoreError(
                "job.json owner_lease must be an object",
                code="E_JOB_LEASE_MALFORMED",
            )
        else:
            from omg_cli.jobs.lease import validate_owner_lease

            owner_lease = validate_owner_lease(
                owner_lease_raw,
                expected_attempt=attempt,
            )

        last_observed_at = data.get("last_observed_at")
        if last_observed_at is not None and not isinstance(last_observed_at, str):
            raise JobStoreError(
                "job.json last_observed_at must be a string or null",
                code="E_JOB_LEASE_MALFORMED",
            )

        recovery_raw = data.get("recovery")
        if recovery_raw is None:
            recovery: dict[str, Any] | None = None
        else:
            from omg_cli.jobs.lease import validate_recovery_meta

            recovery = validate_recovery_meta(recovery_raw)

        # Terminal leases must be released when present.
        if (
            owner_lease is not None
            and state in TERMINAL_STATES
            and owner_lease.get("released_at") is None
        ):
            raise JobStoreError(
                "terminal job owner_lease must have released_at set",
                code="E_JOB_LEASE_MALFORMED",
            )
        # Active running lease must not be released.
        if (
            owner_lease is not None
            and state == JobState.RUNNING
            and owner_lease.get("released_at") is not None
        ):
            raise JobStoreError(
                "running job owner_lease must have released_at=null",
                code="E_JOB_LEASE_MALFORMED",
            )

        return cls(
            job_id=str(job_id),
            created_at=str(created_at),
            provider=str(provider),
            role=str(role),
            state=state,
            attempt=attempt,
            attempt_budget=attempt_budget,
            schema=schema,
            generation=generation,
            pid=pid,
            pgid=pgid,
            handle=str(data["handle"]) if data.get("handle") is not None else None,
            pid_starttime=(
                str(data["pid_starttime"])
                if data.get("pid_starttime") is not None
                else None
            ),
            prompt=str(data.get("prompt") or "prompt.md"),
            result=str(data["result"]) if data.get("result") is not None else None,
            artifacts=[a for a in artifacts if isinstance(a, dict)],
            exit=dict(exit_obj) if isinstance(exit_obj, dict) else None,
            usage=dict(usage) if isinstance(usage, dict) else None,
            stdout=str(data.get("stdout") or "stdout.jsonl"),
            stderr=str(data.get("stderr") or "stderr.jsonl"),
            events=str(data.get("events") or "events.jsonl"),
            run_id=str(data["run_id"]) if data.get("run_id") is not None else None,
            updated_at=str(data["updated_at"]) if data.get("updated_at") is not None else None,
            cancel_reason=(
                str(data["cancel_reason"])
                if data.get("cancel_reason") is not None
                else None
            ),
            cancel_requested_at=(
                str(data["cancel_requested_at"])
                if data.get("cancel_requested_at") is not None
                else None
            ),
            worker=dict(worker) if isinstance(worker, dict) else {},
            error_message=(
                str(data["error_message"])
                if data.get("error_message") is not None
                else None
            ),
            request=request,
            provider_process=provider_process,
            session=dict(session) if isinstance(session, dict) else None,
            retry_class=str(retry_class) if retry_class is not None else None,
            retry_reason=(
                str(data["retry_reason"])
                if data.get("retry_reason") is not None
                else None
            ),
            terminal_at=(
                str(data["terminal_at"]) if data.get("terminal_at") is not None else None
            ),
            attempt_started_at=(
                str(data["attempt_started_at"])
                if data.get("attempt_started_at") is not None
                else None
            ),
            owner_lease=owner_lease,
            last_observed_at=(
                str(last_observed_at) if last_observed_at is not None else None
            ),
            recovery=recovery,
        )

    def remaining_attempts(self) -> int:
        return max(0, int(self.attempt_budget) - int(self.attempt))

    def public_status(self) -> dict[str, Any]:
        """Status surface (no large payloads; no provider_binary; no owner_token)."""
        from omg_cli.jobs.lease import public_lease_summary
        from omg_cli.jobs.providers import public_request_summary

        lease_summary = None
        if self.owner_lease is not None:
            try:
                lease_summary = public_lease_summary(self.owner_lease)
            except JobStoreError:
                lease_summary = None

        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "provider": self.provider,
            "role": self.role,
            "attempt": self.attempt,
            "attempt_budget": self.attempt_budget,
            "remaining_attempts": self.remaining_attempts(),
            "retry_class": self.retry_class,
            "retry_reason": self.retry_reason,
            "terminal_at": self.terminal_at,
            "attempt_started_at": self.attempt_started_at,
            "pid": self.pid,
            "pgid": self.pgid,
            "handle": self.handle,
            "pid_starttime": self.pid_starttime,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_id": self.run_id,
            "exit": self.exit,
            "usage": self.usage,
            "error_message": self.error_message,
            "cancel_reason": self.cancel_reason,
            "cancel_requested_at": self.cancel_requested_at,
            "prompt": self.prompt,
            "result": self.result,
            "artifacts": list(self.artifacts),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "events": self.events,
            "request": public_request_summary(self.request),
            "provider_process": {
                "state": (self.provider_process or {}).get("state"),
                "pid": (self.provider_process or {}).get("pid"),
                "pgid": (self.provider_process or {}).get("pgid"),
                "bound_at": (self.provider_process or {}).get("bound_at"),
                "exited_at": (self.provider_process or {}).get("exited_at"),
                # intentionally omit pid_starttime / handle internals in public
            },
            "session": self.session,
            "owner_lease": lease_summary,
            "last_observed_at": self.last_observed_at,
            "recovery": (
                {
                    "last_action": (self.recovery or {}).get("last_action"),
                    "last_reason": (self.recovery or {}).get("last_reason"),
                    "last_at": (self.recovery or {}).get("last_at"),
                }
                if self.recovery
                else None
            ),
        }


__all__ = [
    "IMMUTABLE_FIELDS",
    "JOB_SCHEMA",
    "LEGAL_TRANSITIONS",
    "PROVIDER_PROCESS_STATES",
    "RETRY_CLASSES",
    "RETRY_CLASS_AUTOMATIC",
    "RETRY_CLASS_MANUAL_ONLY",
    "RETRY_CLASS_NEVER",
    "RETRY_CLASS_UNKNOWN",
    "TERMINAL_STATES",
    "JobRecord",
    "JobState",
    "JobStateName",
    "JobStoreError",
    "TransitionError",
    "assert_transition",
    "default_provider_process",
    "default_request",
]
