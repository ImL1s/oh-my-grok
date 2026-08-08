"""Durable job runtime: start/status/wait/collect/cancel/list (#68 PR1)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omg_cli.jobs.models import (
    TERMINAL_STATES,
    JobRecord,
    JobState,
    JobStoreError,
    TransitionError,
)
from omg_cli.jobs.store import (
    create_job_dir,
    job_dir,
    list_job_ids,
    read_job_record,
    safe_job_id,
    transition_job,
)

# Grace between SIGTERM and SIGKILL for cancel (seconds).
DEFAULT_CANCEL_GRACE_S = 2.0
DEFAULT_WAIT_POLL_S = 0.05


@dataclass(frozen=True, slots=True)
class StartResult:
    record: JobRecord
    launched: bool


def _read_prompt_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JobStoreError(f"cannot read prompt file: {exc}", code="E_JOB_PROMPT") from exc
    return text


def start_job(
    project_root: Path,
    *,
    provider: str,
    role: str,
    prompt_file: Path | str,
    run_id: str | None = None,
    sleep_s: float | None = None,
    fail: bool = False,
    large_output: bool = False,
    ignore_sigterm: bool = False,
    launch: bool = True,
    runner_python: str | None = None,
) -> StartResult:
    """Atomic start: persist queued→starting before spawn; rollback to failed on launch error.

    PR1 admits ``provider=fake`` only for live spawn. ``antigravity`` raises
    ``E_JOB_PROVIDER`` (deferred to a later slice).
    """
    provider = (provider or "").strip().lower()
    role = (role or "").strip() or "researcher"
    if not role:
        raise JobStoreError("role is required", code="E_JOB_ROLE")

    if provider == "antigravity":
        raise JobStoreError(
            "PR1 admits hermetic --provider fake only; "
            "antigravity durable spawn is deferred (later #68 slice)",
            code="E_JOB_PROVIDER",
        )
    if provider != "fake":
        raise JobStoreError(
            f"unsupported job provider {provider!r}; PR1 supports: fake",
            code="E_JOB_PROVIDER",
        )

    prompt_path = Path(prompt_file)
    prompt_text = _read_prompt_file(prompt_path)

    worker: dict[str, Any] = {}
    if sleep_s is not None:
        worker["sleep_s"] = float(sleep_s)
    if fail:
        worker["fail"] = True
    if large_output:
        worker["large_output"] = True
    if ignore_sigterm:
        worker["ignore_sigterm"] = True
        worker.setdefault("sleep_s", 30.0)

    record = create_job_dir(
        project_root,
        provider=provider,
        role=role,
        prompt_text=prompt_text,
        run_id=run_id,
        worker=worker,
    )

    # queued → starting BEFORE launch
    record = transition_job(project_root, record.job_id, JobState.STARTING)

    if not launch:
        # Test hook: leave in starting without a live handle.
        return StartResult(record=record, launched=False)

    py = runner_python or sys.executable
    argv = [
        py,
        "-m",
        "omg_cli.jobs.runner",
        "--job-id",
        record.job_id,
        "--project-root",
        str(Path(project_root).resolve()),
    ]
    env = os.environ.copy()
    # Ensure the checkout's package is importable for the child.
    root_s = str(Path(project_root).resolve())
    # Prefer the oh-my-grok source tree on PYTHONPATH (parent of omg_cli).
    pkg_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH", "")
    parts = [pkg_root]
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["OMG_JOB_ID"] = record.job_id
    env["OMG_PROJECT_ROOT"] = root_s

    try:
        proc = subprocess.Popen(  # noqa: S603 — argv array, no shell
            argv,
            cwd=str(Path(project_root).resolve()),
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        transition_job(
            project_root,
            record.job_id,
            JobState.FAILED,
            updates={
                "exit": {"class": "spawn_error", "returncode": 1},
                "error_message": f"launch failed: {exc}",
                "pid": None,
                "pgid": None,
                "handle": None,
            },
        )
        raise JobStoreError(
            f"failed to launch job runner: {exc}",
            code="E_JOB_LAUNCH",
        ) from exc

    pid = int(proc.pid)
    try:
        pgid = int(os.getpgid(pid))
    except OSError:
        pgid = pid

    handle = f"{provider}:{record.job_id}:pid={pid}"
    try:
        record = transition_job(
            project_root,
            record.job_id,
            JobState.RUNNING,
            updates={
                "pid": pid,
                "pgid": pgid,
                "handle": handle,
            },
        )
    except JobStoreError as commit_exc:
        # Launch succeeded but state commit failed — kill the orphaned child
        # and stamp failed (never leave starting with a dead orphan).
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        _reap_child(pid)
        try:
            cur = read_job_record(project_root, record.job_id)
            if cur.state not in TERMINAL_STATES:
                # Prefer starting→failed; if somehow still queued, step through.
                if cur.state == JobState.QUEUED:
                    transition_job(project_root, record.job_id, JobState.STARTING)
                    cur = read_job_record(project_root, record.job_id)
                if cur.state == JobState.STARTING:
                    transition_job(
                        project_root,
                        record.job_id,
                        JobState.FAILED,
                        updates={
                            "exit": {"class": "spawn_error", "returncode": 1},
                            "error_message": (
                                f"launch commit failed after spawn: {commit_exc}"
                            ),
                            "pid": None,
                            "pgid": None,
                            "handle": None,
                        },
                    )
        except JobStoreError:
            pass
        raise JobStoreError(
            f"failed to commit running handle after spawn: {commit_exc}",
            code="E_JOB_LAUNCH",
        ) from commit_exc

    return StartResult(record=record, launched=True)


def job_status(project_root: Path, job_id: str) -> JobRecord:
    return read_job_record(project_root, safe_job_id(job_id))


def wait_job(
    project_root: Path,
    job_id: str,
    *,
    timeout_s: float,
    poll_s: float = DEFAULT_WAIT_POLL_S,
) -> tuple[JobRecord, bool]:
    """Poll until terminal or timeout. Timeout does **not** cancel.

    Returns ``(record, timed_out)``.
    """
    job_id = safe_job_id(job_id)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        record = read_job_record(project_root, job_id)
        if record.state in TERMINAL_STATES:
            return record, False
        if time.monotonic() >= deadline:
            return record, True
        time.sleep(max(0.01, float(poll_s)))


def _confined_job_path(jdir: Path, descriptor: str) -> Path:
    """Resolve *descriptor* under *jdir*; fail closed on absolute/`..` escape."""
    if not isinstance(descriptor, str) or not descriptor.strip():
        raise JobStoreError(
            "artifact descriptor must be a non-empty relative path",
            code="E_JOB_ARTIFACT",
        )
    raw = descriptor.strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        raise JobStoreError(
            f"artifact path escapes job dir: {raw!r}",
            code="E_JOB_ARTIFACT",
        )
    # Reject `..` components before resolve (defense in depth).
    if any(p == ".." for p in candidate.parts):
        raise JobStoreError(
            f"artifact path escapes job dir: {raw!r}",
            code="E_JOB_ARTIFACT",
        )
    jdir_res = jdir.resolve()
    target = (jdir_res / candidate).resolve()
    if not target.is_relative_to(jdir_res):
        raise JobStoreError(
            f"artifact path escapes job dir: {raw!r}",
            code="E_JOB_ARTIFACT",
        )
    return target


def collect_job(project_root: Path, job_id: str) -> dict[str, Any]:
    """Idempotent collect: summary + artifact descriptors only (no inline blobs)."""
    record = read_job_record(project_root, safe_job_id(job_id))
    if record.state not in TERMINAL_STATES:
        raise JobStoreError(
            f"job {job_id} is not terminal (state={record.state.value})",
            code="E_JOB_NOT_READY",
        )

    # Fail-closed: declared artifacts must stay under the job dir and exist.
    jdir = job_dir(project_root, record.job_id)
    missing: list[str] = []
    if record.result:
        target = _confined_job_path(jdir, record.result)
        if not target.is_file():
            missing.append(record.result)
    for art in record.artifacts:
        path = art.get("path") if isinstance(art, dict) else None
        if isinstance(path, str) and path:
            target = _confined_job_path(jdir, path)
            if not target.is_file():
                missing.append(path)
    if missing:
        raise JobStoreError(
            f"missing artifact(s): {', '.join(sorted(set(missing)))}",
            code="E_JOB_ARTIFACT",
        )

    summary = {
        "job_id": record.job_id,
        "state": record.state.value,
        "provider": record.provider,
        "role": record.role,
        "exit": record.exit,
        "usage": record.usage,
        "result": record.result,
        "artifacts": list(record.artifacts),
        "stdout": record.stdout,
        "events": record.events,
        "prompt": record.prompt,
        "error_message": record.error_message,
        "cancel_reason": record.cancel_reason,
    }
    return summary


def _pid_alive(pid: int) -> bool:
    """True when *pid* exists and is not a zombie (fail-open on probe errors)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # macOS/Linux: kill(0) succeeds for zombies; treat Z as dead.
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "stat="],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    if not out:
        return False
    # STAT may be like "Z", "Zs", "ZW", "Z+" …
    return not out.upper().startswith("Z")


def _reap_child(pid: int) -> None:
    """Best-effort waitpid when we are still the parent (avoids test zombies)."""
    if pid <= 0:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError:
        pass


def _kill_pgid(pgid: int, signum: int) -> bool:
    """Send signal to process group only (never by name). Returns True if signal sent."""
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, signum)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False


def cancel_job(
    project_root: Path,
    job_id: str,
    *,
    reason: str | None = None,
    grace_s: float = DEFAULT_CANCEL_GRACE_S,
) -> JobRecord:
    """Cancel by recorded PID/PGID only. Idempotent for terminal jobs."""
    job_id = safe_job_id(job_id)
    record = read_job_record(project_root, job_id)
    if record.state in TERMINAL_STATES:
        return record

    pgid = record.pgid
    pid = record.pid
    target_pgid = int(pgid) if pgid is not None else (int(pid) if pid is not None else 0)
    target_pid = int(pid) if pid is not None else target_pgid

    if target_pgid > 0 and _pid_alive(target_pid):
        _kill_pgid(target_pgid, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, float(grace_s))
        while time.monotonic() < deadline:
            _reap_child(target_pid)
            if not _pid_alive(target_pid):
                break
            time.sleep(0.05)
        if _pid_alive(target_pid):
            _kill_pgid(target_pgid, signal.SIGKILL)
            for _ in range(40):
                _reap_child(target_pid)
                if not _pid_alive(target_pid):
                    break
                time.sleep(0.05)
    else:
        _reap_child(target_pid)

    updates: dict[str, Any] = {
        "cancel_reason": reason or "operator",
        "exit": {
            "class": "cancelled",
            "returncode": -signal.SIGKILL if target_pgid > 0 else -1,
        },
        "error_message": None,
    }

    # Re-read after kill: runner may have stamped succeeded/failed already.
    try:
        record = read_job_record(project_root, job_id)
    except JobStoreError:
        raise
    if record.state in TERMINAL_STATES:
        return record

    try:
        # starting → cancelled or running → cancelled
        if record.state in {JobState.STARTING, JobState.RUNNING}:
            return transition_job(
                project_root,
                job_id,
                JobState.CANCELLED,
                updates=updates,
            )
        # queued should not happen post-start path; treat as cancelled via starting first
        if record.state == JobState.QUEUED:
            transition_job(project_root, job_id, JobState.STARTING)
            return transition_job(
                project_root,
                job_id,
                JobState.CANCELLED,
                updates=updates,
            )
    except TransitionError:
        # Runner won the race and stamped a terminal state — return idempotently.
        return read_job_record(project_root, job_id)
    return record


def list_jobs(
    project_root: Path,
    *,
    state: str | None = None,
    provider: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for jid in list_job_ids(project_root):
        try:
            rec = read_job_record(project_root, jid)
        except JobStoreError:
            continue
        if state and rec.state.value != state:
            continue
        if provider and rec.provider != provider:
            continue
        if run_id and (rec.run_id or "") != run_id:
            continue
        out.append(rec.public_status())
    return out


__all__ = [
    "StartResult",
    "cancel_job",
    "collect_job",
    "job_status",
    "list_jobs",
    "start_job",
    "wait_job",
]
