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
        {JobState.RUNNING, JobState.FAILED, JobState.CANCELLED}
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
    {"job_id", "created_at", "provider", "role", "schema"}
)


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
    events: str = "events.jsonl"
    run_id: str | None = None
    updated_at: str | None = None
    cancel_reason: str | None = None
    worker: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": int(self.schema),
            "job_id": self.job_id,
            "created_at": self.created_at,
            "provider": self.provider,
            "role": self.role,
            "state": self.state.value,
            "attempt": int(self.attempt),
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
            "events": self.events,
            "run_id": self.run_id,
            "updated_at": self.updated_at,
            "cancel_reason": self.cancel_reason,
            "worker": dict(self.worker) if self.worker else {},
            "error_message": self.error_message,
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
        except (TypeError, ValueError) as exc:
            raise JobStoreError(
                "job.json attempt/generation invalid",
                code="E_JOB_MALFORMED",
            ) from exc

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

        return cls(
            job_id=str(job_id),
            created_at=str(created_at),
            provider=str(provider),
            role=str(role),
            state=state,
            attempt=attempt,
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
            events=str(data.get("events") or "events.jsonl"),
            run_id=str(data["run_id"]) if data.get("run_id") is not None else None,
            updated_at=str(data["updated_at"]) if data.get("updated_at") is not None else None,
            cancel_reason=(
                str(data["cancel_reason"])
                if data.get("cancel_reason") is not None
                else None
            ),
            worker=dict(worker) if isinstance(worker, dict) else {},
            error_message=(
                str(data["error_message"])
                if data.get("error_message") is not None
                else None
            ),
        )

    def public_status(self) -> dict[str, Any]:
        """Status surface (no large payloads)."""
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "provider": self.provider,
            "role": self.role,
            "attempt": self.attempt,
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
            "prompt": self.prompt,
            "result": self.result,
            "artifacts": list(self.artifacts),
            "stdout": self.stdout,
            "events": self.events,
        }


__all__ = [
    "IMMUTABLE_FIELDS",
    "JOB_SCHEMA",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATES",
    "JobRecord",
    "JobState",
    "JobStateName",
    "JobStoreError",
    "TransitionError",
    "assert_transition",
]
