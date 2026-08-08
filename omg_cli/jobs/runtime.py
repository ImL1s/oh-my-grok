"""Durable job runtime: start/status/wait/collect/cancel/list (#68 PR1)."""

from __future__ import annotations

import enum
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


class CancelOwnership(enum.Enum):
    """Outcome of pre-signal ownership revalidation."""

    OK = "ok"  # Safe to signal this pid/pgid.
    GONE = "gone"  # Target already exited; do not signal.


def _probe_pid_starttime(pid: int) -> str | None:
    """Best-effort process start fingerprint (PR1 ownership aid).

    Linux: ``/proc/<pid>/stat`` starttime (field 22) as ``proc:<ticks>``.
    Elsewhere: ``ps -p PID -o lstart=`` as ``lstart:<text>``.
    Returns ``None`` when the probe fails — callers must treat that as
    \"fingerprint unavailable\" (cancel falls back to pid/pgid only).
    """
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            raw = proc_stat.read_text(encoding="utf-8", errors="replace")
            close = raw.rfind(")")
            if close < 0:
                return None
            rest = raw[close + 2 :].split()
            # After \"(comm)\": state=rest[0] … starttime is field 22 → rest[19].
            if len(rest) < 20:
                return None
            return f"proc:{rest[19]}"
        except OSError:
            return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        out = (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out:
        return None
    return f"lstart:{out}"


def _assert_cancel_ownership(
    record: JobRecord,
    target_pid: int,
    target_pgid: int,
) -> CancelOwnership:
    """Revalidate ownership before a cancel signal.

    Returns:
        ``CancelOwnership.OK`` — safe to call ``_kill_pgid``
        ``CancelOwnership.GONE`` — process already gone; do **not** signal

    Raises:
        ``JobStoreError`` (``E_JOB_PID_REUSED`` / ``E_JOB_PGID_MISMATCH``) on
        fingerprint/pgid mismatch or pid/pgid ``<= 1`` — fail-closed, no signal.
    """
    if target_pid <= 1 or target_pgid <= 1:
        raise JobStoreError(
            f"job {record.job_id} refuses to signal pid={target_pid} pgid={target_pgid} "
            "(both must be > 1)",
            code="E_JOB_PID_REUSED",
        )
    if not _pid_alive(target_pid):
        return CancelOwnership.GONE
    try:
        live_pgid = int(os.getpgid(target_pid))
    except ProcessLookupError:
        return CancelOwnership.GONE
    except OSError as exc:
        raise JobStoreError(
            f"job {record.job_id} cannot read live pgid for pid={target_pid}: {exc}",
            code="E_JOB_PGID_MISMATCH",
        ) from exc
    if live_pgid != int(target_pgid):
        raise JobStoreError(
            f"job {record.job_id} live pgid mismatch for pid={target_pid} "
            f"(recorded={target_pgid} live={live_pgid}); refusing to signal",
            code="E_JOB_PGID_MISMATCH",
        )
    expected = record.pid_starttime
    if expected is None or expected == "":
        return CancelOwnership.OK
    live = _probe_pid_starttime(target_pid)
    if live is None or live != expected:
        raise JobStoreError(
            f"job {record.job_id} pid {target_pid} ownership fingerprint mismatch "
            f"(recorded={expected!r} live={live!r}); refusing to signal "
            "(possible PID reuse)",
            code="E_JOB_PID_REUSED",
        )
    return CancelOwnership.OK


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
        # Hermetic default: long enough for SIGTERM→grace→SIGKILL, short for CI.
        worker.setdefault("sleep_s", 2.0)

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

    # Immediate child exit → never claim running.
    early_rc = proc.poll()
    if early_rc is None:
        # Bounded probe: catch runners that exit before the parent commits.
        for _ in range(10):
            time.sleep(0.01)
            early_rc = proc.poll()
            if early_rc is not None:
                break
    if early_rc is not None:
        _reap_child(pid)
        try:
            cur = read_job_record(project_root, record.job_id)
            if cur.state not in TERMINAL_STATES:
                if cur.state == JobState.QUEUED:
                    transition_job(project_root, record.job_id, JobState.STARTING)
                transition_job(
                    project_root,
                    record.job_id,
                    JobState.FAILED,
                    updates={
                        "exit": {
                            "class": "spawn_error",
                            "returncode": int(early_rc),
                        },
                        "error_message": (
                            f"job runner exited immediately with code {early_rc}"
                        ),
                        "pid": None,
                        "pgid": None,
                        "handle": None,
                    },
                )
        except JobStoreError:
            pass
        raise JobStoreError(
            f"job runner exited immediately with code {early_rc}",
            code="E_JOB_LAUNCH",
        )

    handle = f"{provider}:{record.job_id}:pid={pid}"
    # Best-effort ownership fingerprint; null when probe fails (PR1 honesty).
    pid_starttime = _probe_pid_starttime(pid)
    try:
        record = transition_job(
            project_root,
            record.job_id,
            JobState.RUNNING,
            updates={
                "pid": pid,
                "pgid": pgid,
                "handle": handle,
                "pid_starttime": pid_starttime,
            },
        )
    except Exception as commit_exc:
        # Belt-and-suspenders: any post-spawn commit failure must kill the
        # exact child and reconcile durable state (never leave starting or
        # dead-running). SystemExit/KeyboardInterrupt are not caught.
        _cleanup_after_spawn_commit_failure(
            project_root,
            record.job_id,
            pid=pid,
            pgid=pgid,
            handle=handle,
            expected_starttime=pid_starttime,
            exc=commit_exc,
        )
        raise JobStoreError(
            f"failed to commit running handle after spawn: {commit_exc}",
            code="E_JOB_LAUNCH",
        ) from commit_exc

    return StartResult(record=record, launched=True)


def _kill_child_exact(
    pid: int,
    pgid: int,
    *,
    expected_starttime: str | None = None,
) -> None:
    """Kill the exact spawn we own. Skip signals on fingerprint mismatch."""
    if pid <= 1 and pgid <= 1:
        return
    if expected_starttime:
        if _pid_alive(pid):
            live = _probe_pid_starttime(pid)
            if live is None or live != expected_starttime:
                # Possible PID reuse — do not signal the wrong process.
                return
    try:
        if pgid > 1:
            os.killpg(pgid, signal.SIGKILL)
        elif pid > 1:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            if pid > 1:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    _reap_child(pid)


def _cleanup_after_spawn_commit_failure(
    project_root: Path,
    job_id: str,
    *,
    pid: int,
    pgid: int,
    handle: str,
    exc: BaseException,
    expected_starttime: str | None = None,
) -> None:
    """Always kill the spawned child, then reconcile job.json fail-closed."""
    _kill_child_exact(pid, pgid, expected_starttime=expected_starttime)
    try:
        cur = read_job_record(project_root, job_id)
    except JobStoreError:
        # Unreadable — child already killed; best-effort failed if we can.
        _best_effort_stamp_failed(
            project_root,
            job_id,
            message=f"launch commit failed after spawn (unreadable): {exc}",
            spawn_pid=pid,
            spawn_pgid=pgid,
            spawn_handle=handle,
        )
        return

    if cur.state in TERMINAL_STATES:
        # Another winner (cancel/failed/succeeded) — keep durable terminal.
        return

    _best_effort_stamp_failed(
        project_root,
        job_id,
        message=f"launch commit failed after spawn: {exc}",
        spawn_pid=pid,
        spawn_pgid=pgid,
        spawn_handle=handle,
    )


def _best_effort_stamp_failed(
    project_root: Path,
    job_id: str,
    *,
    message: str,
    spawn_pid: int | None = None,
    spawn_pgid: int | None = None,
    spawn_handle: str | None = None,
) -> None:
    """Stamp failed from queued/starting/running(this spawn). Never leaves dead-running."""
    try:
        cur = read_job_record(project_root, job_id)
        if cur.state in TERMINAL_STATES:
            return
        if cur.state == JobState.QUEUED:
            transition_job(project_root, job_id, JobState.STARTING)
            cur = read_job_record(project_root, job_id)
        if cur.state == JobState.STARTING:
            transition_job(
                project_root,
                job_id,
                JobState.FAILED,
                updates={
                    "exit": {"class": "spawn_error", "returncode": 1},
                    "error_message": message,
                    "pid": None,
                    "pgid": None,
                    "handle": None,
                },
            )
            return
        if cur.state == JobState.RUNNING:
            # Commit may have landed before the exception — fail this spawn's handle.
            same_spawn = False
            if spawn_pid is not None and cur.pid is not None and int(cur.pid) == int(spawn_pid):
                same_spawn = True
            elif (
                spawn_handle is not None
                and cur.handle is not None
                and str(cur.handle) == str(spawn_handle)
            ):
                same_spawn = True
            elif (
                spawn_pgid is not None
                and cur.pgid is not None
                and int(cur.pgid) == int(spawn_pgid)
                and spawn_pid is not None
                and cur.pid is not None
                and int(cur.pid) == int(spawn_pid)
            ):
                same_spawn = True
            if same_spawn:
                transition_job(
                    project_root,
                    job_id,
                    JobState.FAILED,
                    updates={
                        "exit": {"class": "spawn_error", "returncode": 1},
                        "error_message": message,
                        # Keep pid/pgid for forensics; child is already killed.
                    },
                )
    except JobStoreError:
        pass


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
    """True when *pid* exists and is not a zombie.

    After ``os.kill(pid, 0)`` proves the pid exists, *ps* probe errors
    (including ``TimeoutExpired``) fail **open** as alive — never treat a
    live process as dead because the STAT probe hung. Only ProcessLookupError,
    empty STAT output, or an explicit zombie STAT returns False.
    """
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
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        out = (proc.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        # Probe failed after existence was proven — fail open as alive.
        return True
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
    if pgid <= 1:
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
    """Cancel by recorded PID/PGID with best-effort starttime fencing.

    Ownership is revalidated immediately before **every** signal (SIGTERM and
    SIGKILL). ``_kill_pgid`` runs only when revalidation returns
    ``CancelOwnership.OK``. ``GONE`` skips signalling and continues the cancel
    state transition. Mismatch raises ``E_JOB_PID_REUSED`` /
    ``E_JOB_PGID_MISMATCH`` with no further signals. Null fingerprint at start
    remains a PR1 limitation (pid/pgid + live PGID only — not full lease/nonce
    ownership).
    """
    job_id = safe_job_id(job_id)
    record = read_job_record(project_root, job_id)
    if record.state in TERMINAL_STATES:
        return record

    pgid = record.pgid
    pid = record.pid
    target_pgid = int(pgid) if pgid is not None else (int(pid) if pid is not None else 0)
    target_pid = int(pid) if pid is not None else target_pgid

    if target_pgid > 0 and target_pid > 0:
        if _pid_alive(target_pid):
            # Revalidate immediately before SIGTERM; signal only on OK.
            ownership = _assert_cancel_ownership(record, target_pid, target_pgid)
            if ownership is CancelOwnership.OK:
                _kill_pgid(target_pgid, signal.SIGTERM)
                deadline = time.monotonic() + max(0.0, float(grace_s))
                while time.monotonic() < deadline:
                    _reap_child(target_pid)
                    if not _pid_alive(target_pid):
                        break
                    time.sleep(0.05)
                if _pid_alive(target_pid):
                    # Revalidate again immediately before SIGKILL.
                    ownership = _assert_cancel_ownership(
                        record, target_pid, target_pgid
                    )
                    if ownership is CancelOwnership.OK:
                        _kill_pgid(target_pgid, signal.SIGKILL)
                        for _ in range(40):
                            _reap_child(target_pid)
                            if not _pid_alive(target_pid):
                                break
                            time.sleep(0.05)
            # GONE (or post-TERM exit): fall through to cancel stamp; never
            # signal a stale PGID.
            _reap_child(target_pid)
        else:
            _reap_child(target_pid)
    elif target_pid > 0:
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
