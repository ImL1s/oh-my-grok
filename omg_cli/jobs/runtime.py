"""Durable job runtime: start/status/wait/collect/cancel/list (#68 PR1+PR2)."""

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
    default_provider_process,
)
# Remove unused direct imports that bypass monkeypatch wrappers
from omg_cli.jobs.ownership import (
    OwnershipOutcome,
    ProcessIdentity,
    probe_pid_starttime,
    reap_child,
)
from omg_cli.jobs.providers import (
    build_request_snapshot,
    preflight_antigravity,
    resolve_job_provider,
)
from omg_cli.jobs.store import (
    create_job_dir,
    job_dir,
    list_job_ids,
    mark_cancel_requested,
    read_job_record,
    safe_job_id,
    transition_job,
)

# Grace between SIGTERM and SIGKILL for cancel (seconds).
DEFAULT_CANCEL_GRACE_S = 2.0
DEFAULT_WAIT_POLL_S = 0.05


# Back-compat alias used by older tests/imports.
CancelOwnership = OwnershipOutcome


def _probe_pid_starttime(pid: int) -> str | None:
    return probe_pid_starttime(pid)


def _assert_cancel_ownership(
    record: JobRecord,
    target_pid: int,
    target_pgid: int,
    *,
    expected_starttime: str | None = None,
    label: str = "runner",
) -> OwnershipOutcome:
    from omg_cli.jobs.ownership import assert_ownership

    # When expected_starttime is provided (inner provider), prefer it over the
    # outer runner fingerprint on the record.
    starttime = (
        expected_starttime
        if expected_starttime is not None
        else record.pid_starttime
    )
    identity = ProcessIdentity(
        pid=int(target_pid),
        pgid=int(target_pgid),
        pid_starttime=starttime,
    )
    return assert_ownership(identity, job_id=record.job_id, label=label)


def _signal_identity(
    record: JobRecord,
    identity: ProcessIdentity,
    signum: int,
    *,
    label: str,
) -> OwnershipOutcome:
    # Call the 3-arg form so PR1 monkeypatches of _assert_cancel_ownership
    # (signature without kwargs) keep working. Overlay provider fingerprint
    # onto a lightweight record view when needed.
    if (
        identity.pid_starttime is not None
        and identity.pid_starttime != record.pid_starttime
    ):
        # Inner provider: temporarily present its fingerprint as record.pid_starttime
        # for the ownership helper without mutating durable state.
        view = JobRecord(
            job_id=record.job_id,
            created_at=record.created_at,
            provider=record.provider,
            role=record.role,
            state=record.state,
            attempt=record.attempt,
            schema=record.schema,
            generation=record.generation,
            pid=record.pid,
            pgid=record.pgid,
            handle=record.handle,
            pid_starttime=identity.pid_starttime,
        )
        ownership = _assert_cancel_ownership(view, identity.pid, identity.pgid)
    else:
        ownership = _assert_cancel_ownership(record, identity.pid, identity.pgid)
    if ownership is OwnershipOutcome.OK:
        _kill_pgid(identity.pgid, signum)
    return ownership


def _pid_alive(pid: int) -> bool:
    from omg_cli.jobs.ownership import pid_alive

    return pid_alive(pid)


def _reap_child(pid: int) -> None:
    reap_child(pid)


def _kill_pgid(pgid: int, signum: int) -> bool:
    from omg_cli.jobs.ownership import kill_pgid

    return kill_pgid(pgid, signum)


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
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    output_format: str | None = None,
    provider_timeout_s: float | None = None,
    launch: bool = True,
    runner_python: str | None = None,
) -> StartResult:
    """Atomic start: preflight → persist queued→starting before spawn.

    Antigravity admission is fail-closed (no job ID / no partial dir on probe
    failure). Fake-only flags with Antigravity raise ``E_JOB_PROVIDER_OPTIONS``.
    """
    provider = (provider or "").strip().lower()
    role = (role or "").strip() or "researcher"
    if not role:
        raise JobStoreError("role is required", code="E_JOB_ROLE")

    # Registry resolution (exact names only) — before any job dir creation.
    try:
        _adapter, meta = resolve_job_provider(provider)
    except JobStoreError:
        raise
    del _adapter

    request_snapshot: dict[str, Any]
    if provider == "antigravity":
        preflight = preflight_antigravity(
            output_format=output_format,
            model=model,
            effort=effort,
            mode=mode,
            timeout_s=provider_timeout_s,
            sleep_s=sleep_s,
            fail=fail,
            large_output=large_output,
            ignore_sigterm=ignore_sigterm,
        )
        request_snapshot = build_request_snapshot(
            provider, preflight=preflight
        )
    else:
        # fake
        if not meta.allow_fake_flags and (
            sleep_s is not None or fail or large_output or ignore_sigterm
        ):
            raise JobStoreError(
                "fake-only flags not allowed for this provider",
                code="E_JOB_PROVIDER_OPTIONS",
            )
        # Reject Antigravity-only options on fake? Plan allows model etc. narrowly
        # on start CLI — for fake they are stored but unused.
        request_snapshot = build_request_snapshot(
            provider,
            output_format=output_format,
            model=model,
            effort=effort,
            mode=mode,
            timeout_s=provider_timeout_s,
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
    if provider_timeout_s is not None:
        worker["timeout_s"] = float(provider_timeout_s)
    elif request_snapshot.get("timeout_s") is not None:
        worker["timeout_s"] = float(request_snapshot["timeout_s"])

    record = create_job_dir(
        project_root,
        provider=provider,
        role=role,
        prompt_text=prompt_text,
        run_id=run_id,
        worker=worker,
        request=request_snapshot,
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
        "stderr": record.stderr,
        "events": record.events,
        "prompt": record.prompt,
        "error_message": record.error_message,
        "cancel_reason": record.cancel_reason,
        "session": record.session,
    }
    return summary


def _provider_identity(record: JobRecord) -> ProcessIdentity | None:
    """Inner provider identity for the cancel gate.

    Any recorded pid/pgid remains in the gate (bound *or* exited). Clearing the
    durable ``state`` to ``exited`` must not drop the cancel target without
    OS-level disappearance proof.
    """
    pp = record.provider_process or {}
    pid = pp.get("pid")
    pgid = pp.get("pgid")
    if pid is None or pgid is None:
        return None
    return ProcessIdentity(
        pid=int(pid),
        pgid=int(pgid),
        pid_starttime=pp.get("pid_starttime"),
    )


def _runner_identity(record: JobRecord) -> ProcessIdentity | None:
    if record.pid is None:
        return None
    pgid = record.pgid if record.pgid is not None else record.pid
    return ProcessIdentity(
        pid=int(record.pid),
        pgid=int(pgid),
        pid_starttime=record.pid_starttime,
    )


def _wait_until_gone(pid: int, *, timeout_s: float = 2.0, poll_s: float = 0.05) -> bool:
    """Poll until *pid* is gone using monkeypatchable ``_pid_alive`` / ``_reap_child``."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        _reap_child(pid)
        if not _pid_alive(pid):
            return True
        time.sleep(max(0.01, float(poll_s)))
    _reap_child(pid)
    return not _pid_alive(pid)


def _prove_gone(
    record: JobRecord,
    identity: ProcessIdentity | None,
    *,
    label: str,
    already_gone: bool,
) -> bool:
    """Return True when *identity* is proven gone (alive check, wait, or ownership GONE)."""
    if identity is None or already_gone:
        return True
    if not _pid_alive(identity.pid):
        _reap_child(identity.pid)
        return True
    # Ownership GONE is OS-level proof (ProcessLookupError / gone between probes).
    try:
        if label == "provider":
            view = JobRecord(
                job_id=record.job_id,
                created_at=record.created_at,
                provider=record.provider,
                role=record.role,
                state=record.state,
                attempt=record.attempt,
                schema=record.schema,
                generation=record.generation,
                pid=record.pid,
                pgid=record.pgid,
                handle=record.handle,
                pid_starttime=identity.pid_starttime,
            )
            own = _assert_cancel_ownership(view, identity.pid, identity.pgid)
        else:
            own = _assert_cancel_ownership(record, identity.pid, identity.pgid)
    except JobStoreError:
        raise
    return own is OwnershipOutcome.GONE


def cancel_job(
    project_root: Path,
    job_id: str,
    *,
    reason: str | None = None,
    grace_s: float = DEFAULT_CANCEL_GRACE_S,
) -> JobRecord:
    """Cancel covering both outer runner and inner provider process groups.

    Graceful path: persist cancel request → SIGTERM outer → runner sets
    cancel_event → inner reaped by process runner → observe both gone.

    Forced path after grace: revalidate → kill inner first → kill outer →
    observe disappearance of both before claiming success.

    A durable ``CANCELLED`` stamp from the runner is **provisional** to this
    function: success requires OS-level disappearance of every captured
    identity (or ownership ``GONE``). ``wait_until_gone`` return values are
    checked; failure to observe disappearance raises ``E_JOB_CANCEL_UNPROVEN``.
    """
    job_id = safe_job_id(job_id)
    record = read_job_record(project_root, job_id)

    # Non-cancel terminals are final (idempotent). CANCELLED is provisional —
    # live pids may still need a force reap.
    if record.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.LOST}:
        return record

    if record.state != JobState.CANCELLED:
        record = mark_cancel_requested(
            project_root, job_id, reason=reason or "operator"
        )
        if record.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.LOST}:
            return record

    pp = record.provider_process or default_provider_process()
    pp_state = str(pp.get("state") or "pending")

    # Fail-closed: launching but unbound — do not speculative-kill outer
    # (would orphan agy) and do not claim cancelled.
    if pp_state == "launching" and pp.get("pid") is None:
        raise JobStoreError(
            f"job {job_id} provider process is launching but unbound; "
            "refusing speculative cancel (E_JOB_CANCEL_UNPROVEN)",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    # Capture identities once. Re-reads / mark_provider_exited must not drop
    # them from the cancel gate without OS-level disappearance proof.
    captured_runner = _runner_identity(record)
    captured_provider = _provider_identity(record)

    runner_gone = captured_runner is None
    provider_gone = captured_provider is None
    if captured_runner is not None and not _pid_alive(captured_runner.pid):
        runner_gone = True
        _reap_child(captured_runner.pid)
    if captured_provider is not None and not _pid_alive(captured_provider.pid):
        provider_gone = True
        _reap_child(captured_provider.pid)

    # ---- Graceful: SIGTERM outer runner (handler sets cancel_event) ----
    if captured_runner is not None and not runner_gone:
        ownership = _signal_identity(
            record, captured_runner, signal.SIGTERM, label="runner"
        )
        if ownership is OwnershipOutcome.GONE:
            runner_gone = True
        elif ownership is OwnershipOutcome.OK:
            deadline = time.monotonic() + max(0.0, float(grace_s))
            while time.monotonic() < deadline:
                if captured_runner is not None:
                    _reap_child(captured_runner.pid)
                    if not _pid_alive(captured_runner.pid):
                        runner_gone = True
                if captured_provider is not None:
                    _reap_child(captured_provider.pid)
                    if not _pid_alive(captured_provider.pid):
                        provider_gone = True
                if runner_gone and provider_gone:
                    break
                # CANCELLED stamp is provisional — never return success here.
                time.sleep(0.05)

    # Post-grace observation (must honor wait_until_gone return values).
    if captured_runner is not None and not runner_gone:
        if _wait_until_gone(captured_runner.pid, timeout_s=min(2.0, max(0.05, grace_s) or 0.05)):
            runner_gone = True
    if captured_provider is not None and not provider_gone:
        if _wait_until_gone(captured_provider.pid, timeout_s=min(2.0, max(0.05, grace_s) or 0.05)):
            provider_gone = True

    # ---- Forced: kill inner provider group first, then outer ----
    # Continue whenever either captured identity remains alive, even if the
    # runner already stamped CANCELLED.
    need_force = False
    if captured_provider is not None and not provider_gone and _pid_alive(captured_provider.pid):
        need_force = True
    if captured_runner is not None and not runner_gone and _pid_alive(captured_runner.pid):
        need_force = True

    if need_force:
        record = read_job_record(project_root, job_id)
        # Inner first — never abort the outer force attempt if inner wait fails.
        if captured_provider is not None and not provider_gone and _pid_alive(
            captured_provider.pid
        ):
            ownership = _signal_identity(
                record, captured_provider, signal.SIGKILL, label="provider"
            )
            if ownership is OwnershipOutcome.GONE:
                provider_gone = True
            elif ownership is OwnershipOutcome.OK:
                if _wait_until_gone(captured_provider.pid, timeout_s=2.0):
                    provider_gone = True
                # else: leave provider_gone False; still force-kill outer below.

        # Outer next — always attempt when still alive, even if inner wait failed.
        record = read_job_record(project_root, job_id)
        if captured_runner is not None and not runner_gone and _pid_alive(
            captured_runner.pid
        ):
            ownership = _signal_identity(
                record, captured_runner, signal.SIGKILL, label="runner"
            )
            if ownership is OwnershipOutcome.GONE:
                runner_gone = True
            elif ownership is OwnershipOutcome.OK:
                if _wait_until_gone(captured_runner.pid, timeout_s=2.0):
                    runner_gone = True
                # else: leave runner_gone False for the final UNPROVEN gate.

    # Final observation gate — CANCELLED stamp alone is never enough.
    # Raise UNPROVEN only after the full inner-then-outer force sequence.
    record = read_job_record(project_root, job_id)
    if record.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.LOST}:
        # Non-cancel race winner: still require captured targets gone if any.
        pass

    runner_gone = _prove_gone(
        record, captured_runner, label="runner", already_gone=runner_gone
    )
    provider_gone = _prove_gone(
        record, captured_provider, label="provider", already_gone=provider_gone
    )

    if captured_runner is not None and not runner_gone:
        raise JobStoreError(
            f"job {job_id} runner still alive; cannot prove cancellation",
            code="E_JOB_CANCEL_UNPROVEN",
        )
    if captured_provider is not None and not provider_gone:
        raise JobStoreError(
            f"job {job_id} provider still alive; cannot prove cancellation",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    # Proven gone — stamp CANCELLED if still non-terminal, else return idempotently.
    if record.state == JobState.CANCELLED:
        return record
    if record.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.LOST}:
        return record

    updates: dict[str, Any] = {
        "cancel_reason": reason or record.cancel_reason or "operator",
        "exit": {
            "class": "cancelled",
            "returncode": -signal.SIGKILL,
            "ok": False,
            "timed_out": False,
            "cancelled": True,
        },
        "error_message": None,
    }

    try:
        if record.state in {JobState.STARTING, JobState.RUNNING}:
            return transition_job(
                project_root,
                job_id,
                JobState.CANCELLED,
                updates=updates,
            )
        if record.state == JobState.QUEUED:
            transition_job(project_root, job_id, JobState.STARTING)
            return transition_job(
                project_root,
                job_id,
                JobState.CANCELLED,
                updates=updates,
            )
    except TransitionError:
        # Runner may have stamped CANCELLED/succeeded concurrently — only
        # accept CANCELLED after re-proving disappearance with captured ids.
        cur = read_job_record(project_root, job_id)
        if cur.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.LOST}:
            return cur
        if cur.state == JobState.CANCELLED:
            if not _prove_gone(
                cur, captured_runner, label="runner", already_gone=False
            ) or not _prove_gone(
                cur, captured_provider, label="provider", already_gone=False
            ):
                raise JobStoreError(
                    f"job {job_id} CANCELLED stamp without process disappearance",
                    code="E_JOB_CANCEL_UNPROVEN",
                )
            return cur
        raise
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
    "CancelOwnership",
    "StartResult",
    "cancel_job",
    "collect_job",
    "job_status",
    "list_jobs",
    "start_job",
    "wait_job",
]
