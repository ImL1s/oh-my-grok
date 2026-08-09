"""Canonical ``.omg/jobs/<id>/`` store with locked atomic writes (#68 PR1)."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    MANAGED_DIR_MODE,
    ContractPathError,
    atomic_write_bytes,
    ensure_managed_dir,
)
from omg_cli.jobs.models import (
    IMMUTABLE_FIELDS,
    JobRecord,
    JobState,
    JobStoreError,
    assert_transition,
    default_provider_process,
    default_request,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_JOB_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_LOCK_TIMEOUT_S = 5.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_job_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


def safe_job_id(job_id: str) -> str:
    rid = (job_id or "").strip()
    if not rid or not _JOB_ID_RE.fullmatch(rid):
        raise JobStoreError(f"invalid job_id {job_id!r}", code="E_JOB_UNKNOWN")
    return rid


def jobs_root(project_root: Path) -> Path:
    return Path(project_root) / ".omg" / "jobs"


def ensure_jobs_root(project_root: Path) -> Path:
    root = jobs_root(project_root)
    ensure_managed_dir(root)
    try:
        os.chmod(root, MANAGED_DIR_MODE)
    except OSError:
        pass
    return root


def job_dir(project_root: Path, job_id: str) -> Path:
    return jobs_root(project_root) / safe_job_id(job_id)


def job_json_path(project_root: Path, job_id: str) -> Path:
    return job_dir(project_root, job_id) / "job.json"


def artifacts_dir(project_root: Path, job_id: str) -> Path:
    return job_dir(project_root, job_id) / "artifacts"


def job_locks_dir(project_root: Path) -> Path:
    """Stable lock directory *outside* deletable job trees (``.omg/jobs/.locks/``)."""
    return jobs_root(project_root) / ".locks"


def _lock_path(project_root: Path, job_id: str) -> Path:
    """External serialization lock — survives GC quarantine of the job dir."""
    return job_locks_dir(project_root) / f"{safe_job_id(job_id)}.lock"


def auto_retry_lock_path(project_root: Path) -> Path:
    """Project-wide scheduler lock (``.omg/jobs/.locks/auto-retry.lock``)."""
    return job_locks_dir(project_root) / "auto-retry.lock"


def _require_flock() -> None:
    if fcntl is None or os.name != "posix":
        raise JobStoreError(
            "job store requires POSIX fcntl.flock",
            code="E_JOB_STORE",
        )


def _acquire_named_lock(
    path: Path,
    *,
    timeout_s: float,
    busy_code: str,
    busy_message: str,
) -> Iterator[None]:
    """Exclusive flock helper shared by per-job and scheduler locks."""
    _require_flock()
    path.touch(exist_ok=True)
    os.chmod(path, DATA_FILE_MODE)
    deadline = time.monotonic() + float(timeout_s)
    with path.open("a+", encoding="utf-8") as lockf:
        while True:
            try:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JobStoreError(busy_message, code=busy_code) from None
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


@contextmanager
def job_lock(project_root: Path, job_id: str) -> Iterator[None]:
    """Exclusive flock on ``.omg/jobs/.locks/<job_id>.lock`` (bounded wait).

    Lock lives outside the job directory so GC can rename/delete the job tree
    without dropping serialization against retry / ACP binding writers.
    """
    jid = safe_job_id(job_id)
    ensure_jobs_root(project_root)
    locks = job_locks_dir(project_root)
    ensure_managed_dir(locks)
    try:
        os.chmod(locks, MANAGED_DIR_MODE)
    except OSError:
        pass
    path = _lock_path(project_root, jid)
    yield from _acquire_named_lock(
        path,
        timeout_s=_LOCK_TIMEOUT_S,
        busy_code="E_JOB_STORE",
        busy_message=f"timed out acquiring job lock for {job_id}",
    )


@contextmanager
def auto_retry_lock(
    project_root: Path,
    *,
    timeout_s: float = 5.0,
) -> Iterator[None]:
    """Exclusive project-wide auto-retry scheduler lock (bounded wait).

    Always acquired before any per-job lock. Contention → ``E_JOB_AUTO_RETRY_BUSY``.
    """
    ensure_jobs_root(project_root)
    locks = job_locks_dir(project_root)
    ensure_managed_dir(locks)
    try:
        os.chmod(locks, MANAGED_DIR_MODE)
    except OSError:
        pass
    path = auto_retry_lock_path(project_root)
    yield from _acquire_named_lock(
        path,
        timeout_s=timeout_s,
        busy_code="E_JOB_AUTO_RETRY_BUSY",
        busy_message="timed out acquiring auto-retry scheduler lock",
    )


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    body = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise JobStoreError(
            f"job durable write confinement failed: {exc}",
            code="E_JOB_STORE",
        ) from exc
    except OSError as exc:
        raise JobStoreError(
            f"job durable write failed: {exc}",
            code="E_JOB_STORE",
        ) from exc


def write_job_record(project_root: Path, record: JobRecord) -> JobRecord:
    """Persist ``job.json`` under lock (caller may already hold lock)."""
    path = job_json_path(project_root, record.job_id)
    record.updated_at = utc_now()
    record.generation = int(record.generation) + 1
    _atomic_write_json(path, record.to_dict())
    return record


def read_job_record(project_root: Path, job_id: str) -> JobRecord:
    path = job_json_path(project_root, job_id)
    if not path.is_file():
        raise JobStoreError(f"unknown job {job_id!r}", code="E_JOB_UNKNOWN")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JobStoreError(
            f"malformed job.json for {job_id}: {exc}",
            code="E_JOB_MALFORMED",
        ) from exc
    if not isinstance(data, dict):
        raise JobStoreError(
            f"malformed job.json for {job_id}: not an object",
            code="E_JOB_MALFORMED",
        )
    record = JobRecord.from_dict(data)
    if record.job_id != safe_job_id(job_id):
        raise JobStoreError(
            "job_id mismatch inside job.json",
            code="E_JOB_MALFORMED",
        )
    return record


def create_job_dir(
    project_root: Path,
    *,
    provider: str,
    role: str,
    prompt_text: str,
    run_id: str | None = None,
    worker: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
    job_id: str | None = None,
    attempt_budget: int = 1,
) -> JobRecord:
    """Materialize job directory + initial ``queued`` job.json."""
    ensure_jobs_root(project_root)
    if job_id:
        jid = safe_job_id(job_id)
    else:
        jid = make_job_id()
        if not _JOB_ID_RE.fullmatch(jid):
            raise JobStoreError(f"invalid job_id {jid!r}", code="E_JOB_STORE")

    try:
        budget = int(attempt_budget)
    except (TypeError, ValueError) as exc:
        raise JobStoreError(
            f"invalid attempt_budget {attempt_budget!r}",
            code="E_JOB_RETRY_BUDGET",
        ) from exc
    if budget < 1:
        raise JobStoreError(
            "attempt_budget must be >= 1",
            code="E_JOB_RETRY_BUDGET",
        )

    jdir = jobs_root(project_root) / jid
    if jdir.exists():
        raise JobStoreError(f"job dir already exists: {jid}", code="E_JOB_STORE")
    ensure_managed_dir(jdir)
    ensure_managed_dir(jdir / "artifacts")

    prompt_path = jdir / "prompt.md"
    atomic_write_bytes(
        prompt_path,
        prompt_text.encode("utf-8"),
        mode=DATA_FILE_MODE,
        replace=False,
    )
    # Touch empty event / stdout / stderr ledgers
    for name in ("events.jsonl", "stdout.jsonl", "stderr.jsonl"):
        atomic_write_bytes(
            jdir / name,
            b"",
            mode=DATA_FILE_MODE,
            replace=False,
        )

    now = utc_now()
    req = dict(request) if isinstance(request, dict) else default_request()
    record = JobRecord(
        job_id=jid,
        created_at=now,
        provider=provider,
        role=role,
        state=JobState.QUEUED,
        attempt=1,
        attempt_budget=budget,
        prompt="prompt.md",
        stdout="stdout.jsonl",
        stderr="stderr.jsonl",
        events="events.jsonl",
        run_id=run_id,
        updated_at=now,
        worker=dict(worker or {}),
        request=req,
        provider_process=default_provider_process(),
    )
    with job_lock(project_root, jid):
        write_job_record(project_root, record)
    return read_job_record(project_root, jid)


def _coerce_terminal_under_cancel_request(
    record: JobRecord,
    new_state: JobState,
    updates: dict[str, Any] | None,
) -> tuple[JobState, dict[str, Any]]:
    """Persist-before-signal: cancel_requested beats racing success/fail.

    Once ``cancel_requested_at`` is durable, a runner that finishes Adapter.run
    (including ignore_sigterm fakes that ignore cancel_event) must not stamp
    ``succeeded``/``failed``. Remap under the transition lock so cancel_job's
    request wins without weakening disappearance / UNPROVEN gates.
    """
    merged = dict(updates or {})
    if not record.cancel_requested_at:
        return new_state, merged
    if new_state not in {JobState.SUCCEEDED, JobState.FAILED}:
        return new_state, merged

    from omg_cli.jobs.retry import classified_terminal_updates

    prior_exit = merged.get("exit") if isinstance(merged.get("exit"), dict) else {}
    exit_obj = dict(prior_exit)
    exit_obj.update(
        {
            "class": "cancelled",
            "ok": False,
            "cancelled": True,
        }
    )
    if "returncode" not in exit_obj or exit_obj.get("returncode") is None:
        exit_obj["returncode"] = -15

    coerced = classified_terminal_updates(
        state=JobState.CANCELLED,
        exit_obj=exit_obj,
        cancel_reason=record.cancel_reason or merged.get("cancel_reason") or "operator",
    )
    out = dict(merged)
    out.update(coerced)
    # Never clobber list/optional fields with explicit None overlays.
    out = {k: v for k, v in out.items() if v is not None or k in (updates or {})}
    if not out.get("cancel_reason"):
        out["cancel_reason"] = record.cancel_reason or "operator"
    return JobState.CANCELLED, out


def transition_job(
    project_root: Path,
    job_id: str,
    new_state: JobState,
    *,
    updates: dict[str, Any] | None = None,
) -> JobRecord:
    """Locked immutable transition + optional field updates."""
    from omg_cli.jobs.lease import release_lease_dict
    from omg_cli.jobs.models import TERMINAL_STATES

    try:
        with job_lock(project_root, job_id):
            record = read_job_record(project_root, job_id)
            new_state, merged = _coerce_terminal_under_cancel_request(
                record, new_state, updates
            )
            assert_transition(record.state, new_state)
            if new_state in TERMINAL_STATES and "owner_lease" not in merged:
                if record.owner_lease is not None:
                    merged["owner_lease"] = release_lease_dict(record.owner_lease)
            _apply_field_updates(record, merged)
            record.state = new_state
            write_job_record(project_root, record)
            return read_job_record(project_root, job_id)
    except JobStoreError:
        raise
    except (OSError, ContractPathError) as exc:
        raise JobStoreError(
            f"job transition durable failure: {exc}",
            code="E_JOB_STORE",
        ) from exc


def update_job_fields(
    project_root: Path,
    job_id: str,
    **updates: Any,
) -> JobRecord:
    """Locked mutable field update without state change."""
    try:
        with job_lock(project_root, job_id):
            record = read_job_record(project_root, job_id)
            for key, value in updates.items():
                if key in IMMUTABLE_FIELDS or key == "state":
                    raise JobStoreError(
                        f"cannot mutate field {key!r} via update_job_fields",
                        code="E_JOB_STORE",
                    )
                if not hasattr(record, key):
                    raise JobStoreError(f"unknown job field {key!r}", code="E_JOB_STORE")
                setattr(record, key, value)
            write_job_record(project_root, record)
            return read_job_record(project_root, job_id)
    except JobStoreError:
        raise
    except (OSError, ContractPathError) as exc:
        raise JobStoreError(
            f"job update durable failure: {exc}",
            code="E_JOB_STORE",
        ) from exc


def _apply_field_updates(record: JobRecord, updates: dict[str, Any] | None) -> None:
    if not updates:
        return
    for key, value in updates.items():
        if key in IMMUTABLE_FIELDS:
            raise JobStoreError(
                f"cannot mutate immutable field {key!r}",
                code="E_JOB_STORE",
            )
        if key == "state":
            raise JobStoreError(
                "cannot mutate state via field updates",
                code="E_JOB_STORE",
            )
        if not hasattr(record, key):
            raise JobStoreError(f"unknown job field {key!r}", code="E_JOB_STORE")
        setattr(record, key, value)


def compare_and_transition_job(
    project_root: Path,
    job_id: str,
    new_state: JobState,
    *,
    expected_generation: int,
    expected_state: JobState | None = None,
    expected_attempt: int | None = None,
    expected_owner_token: str | None = None,
    expected_pid: int | None = None,
    expected_pgid: int | None = None,
    updates: dict[str, Any] | None = None,
) -> JobRecord:
    """CAS + transition under one lock (no nested ``transition_job``)."""
    try:
        with job_lock(project_root, job_id):
            record = read_job_record(project_root, job_id)
            if int(record.generation) != int(expected_generation):
                raise JobStoreError(
                    "job generation changed during recovery CAS",
                    code="E_JOB_RECOVERY_CONFLICT",
                )
            if expected_state is not None and record.state != expected_state:
                raise JobStoreError(
                    f"job state changed during recovery CAS "
                    f"(expected={expected_state.value} got={record.state.value})",
                    code="E_JOB_RECOVERY_CONFLICT",
                )
            if expected_attempt is not None and int(record.attempt) != int(expected_attempt):
                raise JobStoreError(
                    "job attempt changed during recovery CAS",
                    code="E_JOB_RECOVERY_CONFLICT",
                )
            if expected_pid is not None and record.pid != expected_pid:
                raise JobStoreError(
                    "job pid changed during recovery CAS",
                    code="E_JOB_RECOVERY_CONFLICT",
                )
            if expected_pgid is not None and record.pgid != expected_pgid:
                raise JobStoreError(
                    "job pgid changed during recovery CAS",
                    code="E_JOB_RECOVERY_CONFLICT",
                )
            if expected_owner_token is not None:
                lease = record.owner_lease if isinstance(record.owner_lease, dict) else {}
                if lease.get("owner_token") != expected_owner_token:
                    raise JobStoreError(
                        "job owner_token changed during recovery CAS",
                        code="E_JOB_RECOVERY_CONFLICT",
                    )
            new_state, merged = _coerce_terminal_under_cancel_request(
                record, new_state, updates
            )
            assert_transition(record.state, new_state)
            _apply_field_updates(record, merged)
            record.state = new_state
            write_job_record(project_root, record)
            return read_job_record(project_root, job_id)
    except JobStoreError:
        raise
    except (OSError, ContractPathError) as exc:
        raise JobStoreError(
            f"job CAS transition durable failure: {exc}",
            code="E_JOB_STORE",
        ) from exc


def renew_owner_lease(
    project_root: Path,
    job_id: str,
    *,
    expected_attempt: int,
    expected_owner_token: str,
    expected_runner_pid: int,
    now: datetime | None = None,
) -> JobRecord:
    """Heartbeat-only mutation; exact attempt/token/runner PID must match."""
    from omg_cli.jobs.lease import assert_owner_fence, renew_lease_dict

    try:
        with job_lock(project_root, job_id):
            record = read_job_record(project_root, job_id)
            assert_owner_fence(
                record,
                expected_attempt=expected_attempt,
                expected_owner_token=expected_owner_token,
                expected_runner_pid=expected_runner_pid,
            )
            record.owner_lease = renew_lease_dict(record.owner_lease or {}, now=now)
            write_job_record(project_root, record)
            return read_job_record(project_root, job_id)
    except JobStoreError:
        raise
    except (OSError, ContractPathError) as exc:
        raise JobStoreError(
            f"owner lease renew durable failure: {exc}",
            code="E_JOB_STORE",
        ) from exc


def update_owned_job_fields(
    project_root: Path,
    job_id: str,
    *,
    expected_attempt: int,
    expected_owner_token: str,
    expected_runner_pid: int,
    **updates: Any,
) -> JobRecord:
    """Owner-fenced field update without state change."""
    from omg_cli.jobs.lease import assert_owner_fence

    try:
        with job_lock(project_root, job_id):
            record = read_job_record(project_root, job_id)
            assert_owner_fence(
                record,
                expected_attempt=expected_attempt,
                expected_owner_token=expected_owner_token,
                expected_runner_pid=expected_runner_pid,
            )
            for key, value in updates.items():
                if key in IMMUTABLE_FIELDS or key == "state":
                    raise JobStoreError(
                        f"cannot mutate field {key!r} via update_owned_job_fields",
                        code="E_JOB_STORE",
                    )
                if not hasattr(record, key):
                    raise JobStoreError(f"unknown job field {key!r}", code="E_JOB_STORE")
                setattr(record, key, value)
            write_job_record(project_root, record)
            return read_job_record(project_root, job_id)
    except JobStoreError:
        raise
    except (OSError, ContractPathError) as exc:
        raise JobStoreError(
            f"owned job update durable failure: {exc}",
            code="E_JOB_STORE",
        ) from exc


def transition_owned_job(
    project_root: Path,
    job_id: str,
    new_state: JobState,
    *,
    expected_attempt: int,
    expected_owner_token: str,
    expected_runner_pid: int,
    updates: dict[str, Any] | None = None,
) -> JobRecord:
    """Owner-fenced terminal/state transition under one lock."""
    from omg_cli.jobs.lease import assert_owner_fence, release_lease_dict
    from omg_cli.jobs.models import TERMINAL_STATES

    try:
        with job_lock(project_root, job_id):
            record = read_job_record(project_root, job_id)
            assert_owner_fence(
                record,
                expected_attempt=expected_attempt,
                expected_owner_token=expected_owner_token,
                expected_runner_pid=expected_runner_pid,
            )
            new_state, merged = _coerce_terminal_under_cancel_request(
                record, new_state, updates
            )
            assert_transition(record.state, new_state)
            if new_state in TERMINAL_STATES and "owner_lease" not in merged:
                merged["owner_lease"] = release_lease_dict(record.owner_lease)
            _apply_field_updates(record, merged)
            record.state = new_state
            write_job_record(project_root, record)
            return read_job_record(project_root, job_id)
    except JobStoreError:
        raise
    except (OSError, ContractPathError) as exc:
        raise JobStoreError(
            f"owned job transition durable failure: {exc}",
            code="E_JOB_STORE",
        ) from exc


def mark_provider_launching(
    project_root: Path,
    job_id: str,
    *,
    expected_runner_pid: int,
    expected_attempt: int = 1,
    expected_owner_token: str | None = None,
) -> JobRecord:
    """Set provider_process.state=launching under lock (pre-adapter)."""
    from omg_cli.jobs.lease import assert_owner_fence

    with job_lock(project_root, job_id):
        record = read_job_record(project_root, job_id)
        if record.state != JobState.RUNNING:
            raise JobStoreError(
                f"cannot mark provider launching: job state={record.state.value}",
                code="E_JOB_STORE",
            )
        if int(record.attempt) != int(expected_attempt):
            raise JobStoreError(
                "cannot mark provider launching: attempt mismatch",
                code="E_JOB_STORE",
            )
        if record.pid is None or int(record.pid) != int(expected_runner_pid):
            raise JobStoreError(
                "cannot mark provider launching: runner pid mismatch",
                code="E_JOB_STORE",
            )
        if expected_owner_token is not None or record.owner_lease is not None:
            token = expected_owner_token
            if token is None and isinstance(record.owner_lease, dict):
                token = str(record.owner_lease.get("owner_token") or "")
            if not token:
                raise JobStoreError(
                    "cannot mark provider launching: missing owner token",
                    code="E_JOB_LEASE_FENCED",
                )
            assert_owner_fence(
                record,
                expected_attempt=expected_attempt,
                expected_owner_token=token,
                expected_runner_pid=expected_runner_pid,
            )
        if record.cancel_requested_at:
            raise JobStoreError(
                "cannot mark provider launching: cancel already requested",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        pp = dict(record.provider_process or default_provider_process())
        if pp.get("state") not in {"pending", "launching"}:
            raise JobStoreError(
                f"cannot mark provider launching from state={pp.get('state')!r}",
                code="E_JOB_STORE",
            )
        pp["state"] = "launching"
        record.provider_process = pp
        write_job_record(project_root, record)
        return read_job_record(project_root, job_id)


def bind_provider_process(
    project_root: Path,
    job_id: str,
    *,
    pid: int,
    pgid: int,
    pid_starttime: str | None,
    handle: str,
    expected_runner_pid: int,
    expected_attempt: int = 1,
    expected_owner_token: str | None = None,
) -> JobRecord:
    """Transactionally bind inner provider PID/PGID under lock.

    Fail-closed when job is not running, attempt/runner mismatch, cancel won,
    or a provider process is already bound.
    """
    from omg_cli.jobs.lease import assert_owner_fence

    with job_lock(project_root, job_id):
        record = read_job_record(project_root, job_id)
        if record.state != JobState.RUNNING:
            raise JobStoreError(
                f"cannot bind provider process: job state={record.state.value}",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        if int(record.attempt) != int(expected_attempt):
            raise JobStoreError(
                "cannot bind provider process: attempt mismatch",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        if record.pid is None or int(record.pid) != int(expected_runner_pid):
            raise JobStoreError(
                "cannot bind provider process: runner pid mismatch",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        if expected_owner_token is not None or record.owner_lease is not None:
            token = expected_owner_token
            if token is None and isinstance(record.owner_lease, dict):
                token = str(record.owner_lease.get("owner_token") or "")
            if not token:
                raise JobStoreError(
                    "cannot bind provider process: missing owner token",
                    code="E_JOB_LEASE_FENCED",
                )
            assert_owner_fence(
                record,
                expected_attempt=expected_attempt,
                expected_owner_token=token,
                expected_runner_pid=expected_runner_pid,
            )
        if record.cancel_requested_at:
            raise JobStoreError(
                "cannot bind provider process: cancel already requested",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        pp = dict(record.provider_process or default_provider_process())
        if pp.get("state") == "bound" and pp.get("pid") is not None:
            raise JobStoreError(
                "cannot bind provider process: already bound",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        if pp.get("state") not in {"pending", "launching"}:
            raise JobStoreError(
                f"cannot bind provider process from state={pp.get('state')!r}",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        if int(pid) <= 1 or int(pgid) <= 1:
            raise JobStoreError(
                f"cannot bind provider process: pid={pid} pgid={pgid} must be > 1",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        now = utc_now()
        record.provider_process = {
            "state": "bound",
            "pid": int(pid),
            "pgid": int(pgid),
            "pid_starttime": pid_starttime,
            "handle": handle,
            "bound_at": now,
            "exited_at": None,
        }
        write_job_record(project_root, record)
        return read_job_record(project_root, job_id)


def mark_provider_exited(project_root: Path, job_id: str) -> JobRecord:
    """Mark provider_process.state=exited after adapter.run returns."""
    with job_lock(project_root, job_id):
        record = read_job_record(project_root, job_id)
        pp = dict(record.provider_process or default_provider_process())
        if pp.get("state") in {"bound", "launching", "exited"}:
            pp["state"] = "exited"
            pp["exited_at"] = utc_now()
            record.provider_process = pp
            write_job_record(project_root, record)
        return read_job_record(project_root, job_id)


def mark_cancel_requested(
    project_root: Path,
    job_id: str,
    *,
    reason: str | None = None,
) -> JobRecord:
    """Persist cancel_requested_at under lock (before signalling)."""
    with job_lock(project_root, job_id):
        record = read_job_record(project_root, job_id)
        if record.state in (
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.LOST,
        ):
            return record
        if not record.cancel_requested_at:
            record.cancel_requested_at = utc_now()
        if reason:
            record.cancel_reason = reason
        write_job_record(project_root, record)
        return read_job_record(project_root, job_id)


def list_job_ids(project_root: Path) -> list[str]:
    root = jobs_root(project_root)
    if not root.is_dir():
        return []
    out: list[str] = []
    try:
        for entry in root.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            name = entry.name
            if _JOB_ID_RE.fullmatch(name) and (entry / "job.json").is_file():
                out.append(name)
    except OSError:
        return []
    return sorted(out)


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    """Append one JSON line (best-effort; not under job.lock)."""
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"
    ensure_managed_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def attempts_dir(project_root: Path, job_id: str) -> Path:
    return job_dir(project_root, job_id) / "attempts"


def attempt_dir(project_root: Path, job_id: str, attempt: int) -> Path:
    return attempts_dir(project_root, job_id) / f"{int(attempt):04d}"


def create_attempt_dir(project_root: Path, job_id: str, attempt: int) -> Path:
    """Create ``attempts/NNNN/`` (+ artifacts/) for an immutable archive slot.

    Prefer :func:`archive_attempt`, which stages into a sibling temp dir and
    atomically renames so incomplete archives never look final.
    """
    adir = attempt_dir(project_root, job_id, attempt)
    if adir.exists():
        raise JobStoreError(
            f"attempt archive already exists for attempt={attempt}",
            code="E_JOB_RETRY_ARCHIVE",
        )
    ensure_managed_dir(adir)
    ensure_managed_dir(adir / "artifacts")
    return adir


def _copy_file_if_present(src: Path, dst: Path) -> None:
    if not src.is_file():
        atomic_write_bytes(dst, b"", mode=DATA_FILE_MODE, replace=False)
        return
    try:
        data = src.read_bytes()
    except OSError as exc:
        raise JobStoreError(
            f"cannot archive {src.name}: {exc}",
            code="E_JOB_RETRY_ARCHIVE",
        ) from exc
    atomic_write_bytes(dst, data, mode=DATA_FILE_MODE, replace=False)


def _copy_tree_files(src: Path, dst: Path) -> None:
    """Copy regular files under *src* into *dst* (no symlink follow escape)."""
    if not src.is_dir():
        return
    ensure_managed_dir(dst)
    try:
        entries = list(src.iterdir())
    except OSError as exc:
        raise JobStoreError(
            f"cannot list artifacts for archive: {exc}",
            code="E_JOB_RETRY_ARCHIVE",
        ) from exc
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file():
            _copy_file_if_present(entry, dst / entry.name)
        elif entry.is_dir():
            _copy_tree_files(entry, dst / entry.name)


# Written last in staging before rename. Presence (with matching attempt) is the
# only signal that a published ``attempts/NNNN/`` is complete — ``attempt.json``
# alone is insufficient (legacy mid-copy could leave it without ledgers).
_ATTEMPT_ARCHIVE_COMPLETE_NAME = "archive.complete"
_ATTEMPT_ARCHIVE_COMPLETE_VERSION = 1


def _attempt_archive_complete(adir: Path, attempt: int) -> bool:
    """True when ``attempts/NNNN/`` is a finished published archive.

    Requires the staged-publication completion marker (written only after all
    archive contents land in staging). ``attempt.json`` with matching
    ``archived_attempt`` alone is never enough — that file can exist in a
    partial/legacy archive and must not trigger idempotent reuse (which would
    wipe intact active evidence).
    """
    complete = adir / _ATTEMPT_ARCHIVE_COMPLETE_NAME
    if not complete.is_file():
        return False
    try:
        marker = json.loads(complete.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(marker, dict):
        return False
    try:
        if int(marker.get("version", -1)) != _ATTEMPT_ARCHIVE_COMPLETE_VERSION:
            return False
        if int(marker.get("archived_attempt", -1)) != int(attempt):
            return False
    except (TypeError, ValueError):
        return False

    snap_path = adir / "attempt.json"
    if not snap_path.is_file():
        return False
    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    archived = data.get("archived_attempt")
    try:
        return int(archived) == int(attempt)
    except (TypeError, ValueError):
        return False


def _rmtree_best_effort(path: Path) -> None:
    import shutil

    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise JobStoreError(
            f"cannot remove incomplete attempt archive {path.name}: {exc}",
            code="E_JOB_RETRY_ARCHIVE",
        ) from exc


def _reset_active_attempt_ledgers(jdir: Path) -> None:
    """Clear live ledgers/artifacts so the next attempt starts empty."""
    for name in ("stdout.jsonl", "stderr.jsonl", "events.jsonl"):
        atomic_write_bytes(
            jdir / name,
            b"",
            mode=DATA_FILE_MODE,
            replace=True,
        )
    art = jdir / "artifacts"
    if art.is_dir():
        try:
            for entry in list(art.iterdir()):
                if entry.is_symlink():
                    continue
                if entry.is_file():
                    try:
                        entry.unlink()
                    except OSError:
                        pass
                elif entry.is_dir():
                    # Nested dirs from prior attempt — best-effort clear files.
                    try:
                        for nested in entry.rglob("*"):
                            if nested.is_file() and not nested.is_symlink():
                                nested.unlink(missing_ok=True)  # type: ignore[call-arg]
                        # remove empty dirs bottom-up
                        for nested in sorted(
                            (p for p in entry.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts),
                            reverse=True,
                        ):
                            try:
                                nested.rmdir()
                            except OSError:
                                pass
                        entry.rmdir()
                    except OSError:
                        pass
        except OSError:
            pass
    ensure_managed_dir(art)


def archive_attempt(
    project_root: Path,
    job_id: str,
    record: JobRecord,
    *,
    retry_dispatch: dict[str, Any] | None = None,
) -> Path:
    """Snapshot the completed attempt under ``attempts/NNNN/`` (immutable).

    Stages into a temporary sibling directory, writes ``archive.complete`` as
    the last staging step, then atomically renames into ``attempts/NNNN/``.
    Completeness requires that marker — ``attempt.json`` alone is never enough.
    A complete published archive for the same attempt is treated as idempotent
    (retry after crash between rename and job.json persist). Incomplete/legacy
    final dirs are removed and replaced.

    Optional ``retry_dispatch`` (fixed keys only) records why the prior attempt
    was replaced — never prompt/binary/owner-token content.

    Does **not** mutate ``prompt.md`` at the job root. Caller must hold the
    job lock (or accept a race). Evidence files are copied then truncated so
    the next attempt starts with empty ledgers without overwriting history.
    """
    jid = safe_job_id(job_id)
    jdir = job_dir(project_root, jid)
    attempt_n = int(record.attempt)
    adir = attempt_dir(project_root, jid, attempt_n)
    aroot = attempts_dir(project_root, jid)
    ensure_managed_dir(aroot)

    # Crash after publish + before job.json reset: reuse complete archive.
    if _attempt_archive_complete(adir, attempt_n):
        _reset_active_attempt_ledgers(jdir)
        return adir

    # Incomplete/legacy final slot (missing completion marker) — recoverable.
    if adir.exists():
        _rmtree_best_effort(adir)

    staging = aroot / f".staging-{attempt_n:04d}-{uuid.uuid4().hex[:8]}"
    try:
        ensure_managed_dir(staging)
        ensure_managed_dir(staging / "artifacts")

        snapshot = record.to_dict()
        snapshot["archived_at"] = utc_now()
        snapshot["archived_attempt"] = attempt_n
        if retry_dispatch is not None:
            # Fixed-key provenance only; never prompt / binary / owner token.
            snapshot["retry_dispatch"] = {
                "intent": str(retry_dispatch.get("intent") or ""),
                "requested_at": str(retry_dispatch.get("requested_at") or ""),
                "next_attempt": int(retry_dispatch["next_attempt"]),
                "retry_class": retry_dispatch.get("retry_class"),
                "retry_reason": retry_dispatch.get("retry_reason"),
            }
        _atomic_write_json(staging / "attempt.json", snapshot)

        _copy_file_if_present(jdir / "stdout.jsonl", staging / "stdout.jsonl")
        _copy_file_if_present(jdir / "stderr.jsonl", staging / "stderr.jsonl")
        _copy_file_if_present(jdir / "events.jsonl", staging / "events.jsonl")
        _copy_tree_files(jdir / "artifacts", staging / "artifacts")

        # Last staging write: publication is incomplete until this exists.
        _atomic_write_json(
            staging / _ATTEMPT_ARCHIVE_COMPLETE_NAME,
            {
                "archived_attempt": attempt_n,
                "version": _ATTEMPT_ARCHIVE_COMPLETE_VERSION,
            },
        )

        try:
            os.rename(staging, adir)
        except OSError as exc:
            # Racing publish already complete — treat as success.
            if _attempt_archive_complete(adir, attempt_n):
                try:
                    _rmtree_best_effort(staging)
                except JobStoreError:
                    pass
            else:
                raise JobStoreError(
                    f"failed to publish attempt archive for attempt={attempt_n}: {exc}",
                    code="E_JOB_RETRY_ARCHIVE",
                ) from exc
    except Exception:
        try:
            _rmtree_best_effort(staging)
        except JobStoreError:
            pass
        raise

    # Reset active ledgers / artifacts for the next attempt (history preserved).
    _reset_active_attempt_ledgers(jdir)
    return adir


def prepare_retry(
    project_root: Path,
    job_id: str,
    *,
    next_attempt: int,
    intent: Any = None,
    now: datetime | None = None,
) -> JobRecord:
    """Dedicated terminal→queued retry transaction (not a generic transition).

    Archives the prior attempt, then atomically resets mutable runtime fields
    and sets ``state=queued`` with ``attempt=next_attempt``. Does **not** launch.
    Re-runs admission under the job lock (closes scheduler evaluation races).
    """
    from omg_cli.jobs.retry import RetryIntent, assert_retry_admission

    resolved_intent = intent if intent is not None else RetryIntent.EXPLICIT
    if not isinstance(resolved_intent, RetryIntent):
        resolved_intent = RetryIntent(str(resolved_intent))

    jid = safe_job_id(job_id)
    with job_lock(project_root, jid):
        record = read_job_record(project_root, jid)
        assert_retry_admission(
            record,
            attempt=next_attempt,
            intent=resolved_intent,
            now=now,
        )
        dispatch = {
            "intent": resolved_intent.value,
            "requested_at": utc_now()
            if now is None
            else (
                now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
            )
            .astimezone(timezone.utc)
            .isoformat(),
            "next_attempt": int(next_attempt),
            "retry_class": record.retry_class,
            "retry_reason": record.retry_reason,
        }
        archive_attempt(project_root, jid, record, retry_dispatch=dispatch)

        # Reset runtime fields for the new attempt; keep immutable request/budget.
        record.state = JobState.QUEUED
        record.attempt = int(next_attempt)
        record.pid = None
        record.pgid = None
        record.handle = None
        record.pid_starttime = None
        record.result = None
        record.artifacts = []
        record.exit = None
        record.usage = None
        record.error_message = None
        record.cancel_reason = None
        record.cancel_requested_at = None
        record.session = None
        record.retry_class = None
        record.retry_reason = None
        record.terminal_at = None
        record.attempt_started_at = None
        record.owner_lease = None
        record.last_observed_at = None
        record.recovery = None
        record.provider_process = default_provider_process()
        # Keep worker knobs (fake flags / timeout) for the requeue.
        write_job_record(project_root, record)
        return read_job_record(project_root, jid)


def _parse_iso_ts(raw: str | None) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _validate_retention_days(retention_days: float) -> float:
    """Fail closed on negative / non-finite retention."""
    import math

    try:
        days = float(retention_days)
    except (TypeError, ValueError) as exc:
        raise JobStoreError(
            f"invalid retention_days {retention_days!r}",
            code="E_JOB_GC",
        ) from exc
    if not math.isfinite(days) or days < 0:
        raise JobStoreError(
            "retention_days must be a finite number >= 0",
            code="E_JOB_GC",
        )
    return days


def gc_candidates(
    project_root: Path,
    *,
    retention_days: float,
    now: datetime | None = None,
) -> list[str]:
    """Return job ids that *appear* eligible for GC (caller revalidates under lock).

    Never includes nonterminal or unreadable records. Retention clock uses
    ``terminal_at`` when present, else ``updated_at`` / ``created_at``.
    """
    days = _validate_retention_days(retention_days)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    out: list[str] = []
    for jid in list_job_ids(project_root):
        try:
            rec = read_job_record(project_root, jid)
        except JobStoreError:
            continue
        if rec.state not in (
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.LOST,
        ):
            continue
        stamp = (
            _parse_iso_ts(rec.terminal_at)
            or _parse_iso_ts(rec.updated_at)
            or _parse_iso_ts(rec.created_at)
        )
        if stamp is None:
            continue
        if stamp <= cutoff:
            out.append(jid)
    return out


def gc_quarantine_dir(project_root: Path) -> Path:
    return jobs_root(project_root) / ".gc-quarantine"


def quarantine_job_dir(project_root: Path, job_id: str) -> Path | None:
    """Atomically rename job dir into ``.gc-quarantine/`` (caller holds ``job_lock``).

    Returns the quarantine path, or ``None`` if the job dir is already absent.
    Does **not** delete; caller deletes the quarantined tree after releasing the lock.
    """
    jid = safe_job_id(job_id)
    jdir = job_dir(project_root, jid)
    if not jdir.exists():
        return None
    qroot = gc_quarantine_dir(project_root)
    ensure_managed_dir(qroot)
    try:
        os.chmod(qroot, MANAGED_DIR_MODE)
    except OSError:
        pass
    dest = qroot / f"{jid}.{uuid.uuid4().hex[:8]}"
    try:
        os.rename(jdir, dest)
    except OSError as exc:
        raise JobStoreError(
            f"failed to quarantine job dir {jid}: {exc}",
            code="E_JOB_GC",
        ) from exc
    return dest


def delete_quarantined_tree(path: Path) -> None:
    """``rmtree`` a previously quarantined job tree (lock already released)."""
    import shutil

    if path is None or not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise JobStoreError(
            f"failed to delete quarantined job tree {path}: {exc}",
            code="E_JOB_GC",
        ) from exc


def delete_job_dir(project_root: Path, job_id: str) -> None:
    """Remove a job directory tree (legacy helper; prefer quarantine+delete)."""
    import shutil

    jid = safe_job_id(job_id)
    jdir = job_dir(project_root, jid)
    if not jdir.exists():
        return
    try:
        shutil.rmtree(jdir)
    except OSError as exc:
        raise JobStoreError(
            f"failed to delete job dir {jid}: {exc}",
            code="E_JOB_GC",
        ) from exc


__all__ = [
    "append_jsonl",
    "archive_attempt",
    "artifacts_dir",
    "attempt_dir",
    "attempts_dir",
    "auto_retry_lock",
    "auto_retry_lock_path",
    "bind_provider_process",
    "compare_and_transition_job",
    "create_attempt_dir",
    "create_job_dir",
    "delete_job_dir",
    "delete_quarantined_tree",
    "ensure_jobs_root",
    "gc_candidates",
    "gc_quarantine_dir",
    "job_dir",
    "job_json_path",
    "job_lock",
    "job_locks_dir",
    "jobs_root",
    "list_job_ids",
    "make_job_id",
    "mark_cancel_requested",
    "mark_provider_exited",
    "mark_provider_launching",
    "prepare_retry",
    "quarantine_job_dir",
    "read_job_record",
    "renew_owner_lease",
    "safe_job_id",
    "transition_job",
    "transition_owned_job",
    "update_job_fields",
    "update_owned_job_fields",
    "utc_now",
    "write_job_record",
]
