"""Canonical ``.omg/jobs/<id>/`` store with locked atomic writes (#68 PR1)."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
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


def _lock_path(project_root: Path, job_id: str) -> Path:
    return job_dir(project_root, job_id) / "job.lock"


def _require_flock() -> None:
    if fcntl is None or os.name != "posix":
        raise JobStoreError(
            "job store requires POSIX fcntl.flock",
            code="E_JOB_STORE",
        )


@contextmanager
def job_lock(project_root: Path, job_id: str) -> Iterator[None]:
    """Exclusive flock on ``job.lock`` (bounded wait)."""
    _require_flock()
    jdir = job_dir(project_root, job_id)
    ensure_managed_dir(jdir)
    path = _lock_path(project_root, job_id)
    path.touch(exist_ok=True)
    os.chmod(path, DATA_FILE_MODE)
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    with path.open("a+", encoding="utf-8") as lockf:
        while True:
            try:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JobStoreError(
                        f"timed out acquiring job lock for {job_id}",
                        code="E_JOB_STORE",
                    ) from None
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


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
) -> JobRecord:
    """Materialize job directory + initial ``queued`` job.json."""
    ensure_jobs_root(project_root)
    if job_id:
        jid = safe_job_id(job_id)
    else:
        jid = make_job_id()
        if not _JOB_ID_RE.fullmatch(jid):
            raise JobStoreError(f"invalid job_id {jid!r}", code="E_JOB_STORE")

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


def transition_job(
    project_root: Path,
    job_id: str,
    new_state: JobState,
    *,
    updates: dict[str, Any] | None = None,
) -> JobRecord:
    """Locked immutable transition + optional field updates."""
    try:
        with job_lock(project_root, job_id):
            record = read_job_record(project_root, job_id)
            assert_transition(record.state, new_state)
            if updates:
                for key, value in updates.items():
                    if key in IMMUTABLE_FIELDS:
                        raise JobStoreError(
                            f"cannot mutate immutable field {key!r}",
                            code="E_JOB_STORE",
                        )
                    if not hasattr(record, key):
                        raise JobStoreError(
                            f"unknown job field {key!r}",
                            code="E_JOB_STORE",
                        )
                    setattr(record, key, value)
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


def mark_provider_launching(
    project_root: Path,
    job_id: str,
    *,
    expected_runner_pid: int,
    expected_attempt: int = 1,
) -> JobRecord:
    """Set provider_process.state=launching under lock (pre-adapter)."""
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
) -> JobRecord:
    """Transactionally bind inner provider PID/PGID under lock.

    Fail-closed when job is not running, attempt/runner mismatch, cancel won,
    or a provider process is already bound.
    """
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


__all__ = [
    "append_jsonl",
    "artifacts_dir",
    "bind_provider_process",
    "create_job_dir",
    "ensure_jobs_root",
    "job_dir",
    "job_json_path",
    "job_lock",
    "jobs_root",
    "list_job_ids",
    "make_job_id",
    "mark_cancel_requested",
    "mark_provider_exited",
    "mark_provider_launching",
    "read_job_record",
    "safe_job_id",
    "transition_job",
    "update_job_fields",
    "utc_now",
    "write_job_record",
]
