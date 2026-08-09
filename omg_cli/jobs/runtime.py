"""Durable job runtime: start/status/wait/collect/cancel/list (#68 PR1+PR2)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

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
    IdentityProbeOutcome,
    OwnershipOutcome,
    ProcessIdentity,
    probe_identity_liveness,
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
    make_job_id,
    mark_cancel_requested,
    read_job_record,
    safe_job_id,
    transition_job,
    update_job_fields,
)

# Grace between SIGTERM and SIGKILL for cancel (seconds).
DEFAULT_CANCEL_GRACE_S = 2.0
DEFAULT_WAIT_POLL_S = 0.05


# Back-compat alias used by older tests/imports.
CancelOwnership = OwnershipOutcome


def _probe_pid_starttime(pid: int) -> str | None:
    return probe_pid_starttime(pid)


def _probe_gc_identity(identity: ProcessIdentity) -> IdentityProbeOutcome:
    """Monkeypatchable GC identity probe (tri-state + reused)."""
    return probe_identity_liveness(identity)


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
    prompt_file: Path | str | None = None,
    prompt_text: str | None = None,
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
    allow_internal: bool = False,
    request_overrides: dict[str, Any] | None = None,
    job_id: str | None = None,
    attempt_budget: int = 1,
) -> StartResult:
    """Atomic start: preflight → persist queued→starting before spawn.

    Antigravity admission is fail-closed (no job ID / no partial dir on probe
    failure). Fake-only flags with Antigravity raise ``E_JOB_PROVIDER_OPTIONS``.
    Internal providers (``grok-acp-session``) require ``allow_internal=True``.

    Pass either ``prompt_file`` or ``prompt_text`` (not both). Prefer
    ``prompt_text`` for concurrent callers (e.g. ask --background) so a shared
    temp path cannot cross-contaminate prompts.
    """
    provider = (provider or "").strip().lower()
    role = (role or "").strip() or "researcher"
    if not role:
        raise JobStoreError("role is required", code="E_JOB_ROLE")

    # Registry resolution (exact names only) — before any job dir creation.
    try:
        _adapter, meta = resolve_job_provider(
            provider, allow_internal=bool(allow_internal)
        )
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
    elif provider == "grok-acp-session":
        if not allow_internal:
            raise JobStoreError(
                "grok-acp-session is internal-only",
                code="E_JOB_PROVIDER_INTERNAL",
            )
        ov = dict(request_overrides or {})
        request_snapshot = build_request_snapshot(
            provider,
            timeout_s=provider_timeout_s or ov.get("timeout_s"),
            provider_binary=ov.get("provider_binary"),
            session_id=ov.get("session_id"),
            parent_run_id=ov.get("parent_run_id") or run_id,
            cwd=ov.get("cwd"),
            session_id_hash=ov.get("session_id_hash"),
            cwd_hash=ov.get("cwd_hash"),
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

    if prompt_text is not None and prompt_file is not None:
        raise JobStoreError(
            "pass prompt_file or prompt_text, not both",
            code="E_JOB_PROMPT",
        )
    if prompt_text is not None:
        resolved_prompt = str(prompt_text)
        if not resolved_prompt.strip():
            raise JobStoreError("prompt_text is empty", code="E_JOB_PROMPT")
    elif prompt_file is not None:
        resolved_prompt = _read_prompt_file(Path(prompt_file))
    else:
        raise JobStoreError(
            "prompt_file or prompt_text is required",
            code="E_JOB_PROMPT",
        )

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
        prompt_text=resolved_prompt,
        run_id=run_id,
        worker=worker,
        request=request_snapshot,
        job_id=job_id,
        attempt_budget=attempt_budget,
    )

    # queued → starting BEFORE launch
    record = transition_job(project_root, record.job_id, JobState.STARTING)

    if not launch:
        # Test hook: leave in starting without a live handle.
        return StartResult(record=record, launched=False)

    return launch_job_runner(
        project_root,
        record.job_id,
        runner_python=runner_python,
    )


def launch_job_runner(
    project_root: Path,
    job_id: str,
    *,
    runner_python: str | None = None,
) -> StartResult:
    """Spawn the existing ``omg_cli.jobs.runner`` child (single launcher path).

    Shared by ``start_job`` and ``retry_job`` — no second launcher.
    Expects the job to already be in ``starting`` with no live handle.
    """
    record = read_job_record(project_root, safe_job_id(job_id))
    if record.state != JobState.STARTING:
        raise JobStoreError(
            f"launch requires state=starting (got {record.state.value})",
            code="E_JOB_LAUNCH",
        )
    provider = record.provider

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
        from omg_cli.jobs.retry import classified_terminal_updates

        transition_job(
            project_root,
            record.job_id,
            JobState.FAILED,
            updates=classified_terminal_updates(
                state=JobState.FAILED,
                exit_obj={"class": "spawn_error", "returncode": 1},
                error_message=f"launch failed: {exc}",
                pid=None,
                pgid=None,
                handle=None,
            ),
        )
        err = JobStoreError(
            f"failed to launch job runner: {exc}",
            code="E_JOB_LAUNCH",
        )
        err.spawned = False
        err.disappearance_proven = True
        raise err from exc

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
            from omg_cli.jobs.retry import classified_terminal_updates

            cur = read_job_record(project_root, record.job_id)
            if cur.state not in TERMINAL_STATES:
                if cur.state == JobState.QUEUED:
                    transition_job(project_root, record.job_id, JobState.STARTING)
                transition_job(
                    project_root,
                    record.job_id,
                    JobState.FAILED,
                    updates=classified_terminal_updates(
                        state=JobState.FAILED,
                        exit_obj={
                            "class": "spawn_error",
                            "returncode": int(early_rc),
                        },
                        error_message=(
                            f"job runner exited immediately with code {early_rc}"
                        ),
                        pid=None,
                        pgid=None,
                        handle=None,
                    ),
                )
        except JobStoreError:
            pass
        err = JobStoreError(
            f"job runner exited immediately with code {early_rc}",
            code="E_JOB_LAUNCH",
        )
        err.spawned = True
        err.disappearance_proven = True
        raise err

    handle = f"{provider}:{record.job_id}:pid={pid}"
    # Best-effort ownership fingerprint; null when probe fails (PR1 honesty).
    pid_starttime = _probe_pid_starttime(pid)
    # Durable spawn identity before RUNNING commit — job.json may remain
    # STARTING/pid=null if the commit or a later update_job_fields fails.
    try:
        _write_spawn_identity_recovery(
            project_root,
            record.job_id,
            pid=pid,
            pgid=pgid,
            handle=handle,
            pid_starttime=pid_starttime,
            reason="post_spawn_pre_running",
        )
    except Exception as id_exc:
        proven = _kill_child_exact(pid, pgid, expected_starttime=pid_starttime)
        if not proven:
            try:
                _mark_spawn_uncertain(
                    project_root,
                    record.job_id,
                    detail=f"spawn identity write failed: {id_exc}",
                )
            except Exception:
                pass
            err = JobStoreError(
                f"failed to persist spawn identity after spawn: {id_exc}",
                code="E_JOB_CANCEL_UNPROVEN",
            )
            err.spawned = True
            err.disappearance_proven = False
            raise err from id_exc
        _best_effort_stamp_failed(
            project_root,
            record.job_id,
            message=f"launch commit aborted; spawn identity write failed: {id_exc}",
            spawn_pid=pid,
            spawn_pgid=pgid,
            spawn_handle=handle,
        )
        err = JobStoreError(
            f"failed to persist spawn identity after spawn: {id_exc}",
            code="E_JOB_LAUNCH",
        )
        err.spawned = True
        err.disappearance_proven = True
        raise err from id_exc
    try:
        from omg_cli.jobs.lease import acquire_owner_lease

        record = transition_job(
            project_root,
            record.job_id,
            JobState.RUNNING,
            updates={
                "pid": pid,
                "pgid": pgid,
                "handle": handle,
                "pid_starttime": pid_starttime,
                "owner_lease": acquire_owner_lease(attempt=int(record.attempt)),
            },
        )
    except Exception as commit_exc:
        # Belt-and-suspenders: any post-spawn commit failure must kill the
        # exact child and reconcile durable state (never leave starting or
        # dead-running). SystemExit/KeyboardInterrupt are not caught.
        try:
            proven = _cleanup_after_spawn_commit_failure(
                project_root,
                record.job_id,
                pid=pid,
                pgid=pgid,
                handle=handle,
                expected_starttime=pid_starttime,
                exc=commit_exc,
            )
        except JobStoreError as cleanup_exc:
            # Unproven cleanup that could not durable-persist identity.
            if getattr(cleanup_exc, "code", None) == "E_JOB_CANCEL_UNPROVEN":
                cleanup_exc.spawned = True
                cleanup_exc.disappearance_proven = False
            raise
        err = JobStoreError(
            f"failed to commit running handle after spawn: {commit_exc}",
            code="E_JOB_LAUNCH",
        )
        err.spawned = True
        err.disappearance_proven = bool(proven)
        raise err from commit_exc

    return StartResult(record=record, launched=True)


def _kill_child_exact(
    pid: int,
    pgid: int,
    *,
    expected_starttime: str | None = None,
) -> bool:
    """Kill the exact spawn we own.

    Returns True only when process-group disappearance is proven.
    Skip signals on fingerprint mismatch/uncertain probe — return False
    (caller must fail closed; do not claim the child is gone).
    """
    if pid <= 1 and pgid <= 1:
        return True
    if expected_starttime:
        if _pid_alive(pid):
            live = _probe_pid_starttime(pid)
            if live is None or live != expected_starttime:
                # Possible PID reuse or probe uncertain — do not signal.
                return False
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
    return _wait_until_gone(pid, timeout_s=2.0)


_SPAWN_IDENTITY_REL = "spawn_identity.json"
_SPAWN_UNCERTAIN_REL = "spawn_uncertain.json"


def _spawn_identity_path(project_root: Path, job_id: str) -> Path:
    return job_dir(project_root, job_id) / _SPAWN_IDENTITY_REL


def _spawn_uncertain_path(project_root: Path, job_id: str) -> Path:
    return job_dir(project_root, job_id) / _SPAWN_UNCERTAIN_REL


def _write_spawn_identity_recovery(
    project_root: Path,
    job_id: str,
    *,
    pid: int,
    pgid: int,
    handle: str | None = None,
    pid_starttime: str | None = None,
    reason: str | None = None,
) -> None:
    """Atomically retain runner identity outside job.json (cancel recovery)."""
    from omg_cli.contracts.path_keys import ensure_managed_dir

    jid = safe_job_id(job_id)
    jdir = job_dir(project_root, jid)
    ensure_managed_dir(jdir)
    payload: dict[str, Any] = {
        "job_id": jid,
        "pid": int(pid),
        "pgid": int(pgid),
        "handle": handle,
        "pid_starttime": pid_starttime,
        "reason": reason or "unproven_spawn",
    }
    _write_json_file(_spawn_identity_path(project_root, jid), payload)
    # Identity recovered — drop uncertain marker if present.
    _clear_spawn_uncertain(project_root, jid)


def _read_spawn_identity_recovery(
    project_root: Path, job_id: str
) -> ProcessIdentity | None:
    data = _read_json_file(_spawn_identity_path(project_root, job_id))
    if not isinstance(data, Mapping):
        return None
    pid = data.get("pid")
    pgid = data.get("pgid")
    if pid is None or pgid is None:
        return None
    try:
        return ProcessIdentity(
            pid=int(pid),
            pgid=int(pgid),
            pid_starttime=(
                str(data["pid_starttime"])
                if data.get("pid_starttime") is not None
                else None
            ),
        )
    except (TypeError, ValueError):
        return None


def _clear_spawn_identity_recovery(project_root: Path, job_id: str) -> None:
    try:
        _spawn_identity_path(project_root, job_id).unlink()
    except OSError:
        pass


def _mark_spawn_uncertain(
    project_root: Path, job_id: str, *, detail: str
) -> None:
    """Fail-closed marker when identity bytes cannot be persisted at all."""
    from omg_cli.contracts.path_keys import ensure_managed_dir

    jid = safe_job_id(job_id)
    ensure_managed_dir(job_dir(project_root, jid))
    _write_json_file(
        _spawn_uncertain_path(project_root, jid),
        {"uncertain": True, "detail": str(detail), "job_id": jid},
    )


def _spawn_uncertain(project_root: Path, job_id: str) -> bool:
    data = _read_json_file(_spawn_uncertain_path(project_root, job_id))
    return isinstance(data, Mapping) and bool(data.get("uncertain"))


def _clear_spawn_uncertain(project_root: Path, job_id: str) -> None:
    try:
        _spawn_uncertain_path(project_root, job_id).unlink()
    except OSError:
        pass


def _acp_binding_references_job(project_root: Path, job_id: str) -> bool:
    """True when any ACP singleton binding points at *job_id*, or is unreadable.

    Fail closed: a malformed / unreadable binding entry protects *all* jobs from
    GC (corruption is not treated as absence).
    """
    from omg_cli.jobs.store import ensure_jobs_root

    try:
        jid = safe_job_id(job_id)
    except JobStoreError:
        return False
    bind_dir = ensure_jobs_root(project_root) / "acp_bindings"
    if not bind_dir.is_dir():
        return False
    try:
        entries = list(bind_dir.iterdir())
    except OSError:
        # Unreadable binding dir — fail closed.
        return True
    for path in entries:
        if not path.is_file() or path.suffix != ".json":
            continue
        data = _read_json_file(path)
        if data is None:
            # Malformed / unreadable JSON — cannot prove it does not reference
            # this job; protect against GC deletion.
            return True
        if str(data.get("job_id") or "") == jid:
            return True
    return False


def _gc_identities_block_reason(
    project_root: Path, record: JobRecord
) -> str | None:
    """Return a skip reason when recorded identities are live/unproven; else None.

    Once ``_pid_alive`` is true, only explicit ``GONE`` (or a subsequent
    not-alive observation) or verified ``REUSED`` may permit quarantine.
    Probe-unavailable / getpgid errors are ``UNPROVEN`` and **block** GC —
    never treat them as gone.
    """
    from omg_cli.jobs.lease import lease_is_active

    if lease_is_active(record.owner_lease):
        return "active_owner_lease"

    if _spawn_uncertain(project_root, record.job_id):
        return "spawn_uncertain"

    from omg_cli.jobs.recovery import provider_launch_unbound, spawn_identity_unproven

    if spawn_identity_unproven(project_root, record.job_id):
        return "spawn_identity_malformed"
    if provider_launch_unbound(record):
        return "provider_identity_incomplete"

    runner = _runner_identity(record)
    if runner is None:
        runner = _read_spawn_identity_recovery(project_root, record.job_id)
    provider = _provider_identity(record)

    for identity, label in ((runner, "runner"), (provider, "provider")):
        if identity is None:
            continue
        if not _pid_alive(identity.pid):
            _reap_child(identity.pid)
            continue
        # PID observed alive — ownership uncertainty must block GC.
        try:
            outcome = _probe_gc_identity(identity)
        except JobStoreError:
            # Any raise from a patched/legacy ownership path: fail closed.
            return f"identity_unproven:{label}"
        if outcome is IdentityProbeOutcome.LIVE:
            return f"live_identity:{label}"
        if outcome is IdentityProbeOutcome.UNPROVEN:
            return f"identity_unproven:{label}"
        if outcome is IdentityProbeOutcome.REUSED:
            # Verified different occupant — recorded process is gone for GC.
            continue
        if outcome is IdentityProbeOutcome.GONE:
            # Died between the alive check and the probe.
            continue
        # Fresh liveness recheck: only not-alive permits quarantine.
        if not _pid_alive(identity.pid):
            _reap_child(identity.pid)
            continue
        return f"identity_unproven:{label}"
    return None


def _write_acp_binding_for_job(
    project_root: Path,
    job_id: str,
    bind_path: Path,
    payload: Mapping[str, Any],
) -> None:
    """Publish an ACP binding that references *job_id* under the external job lock.

    Coordinates with ``gc_jobs`` quarantine so a binding cannot appear in the
    unlocked window between GC eligibility and deletion.
    """
    from omg_cli.jobs.store import job_lock

    with job_lock(project_root, job_id):
        _write_json_file(bind_path, payload)


def _persist_unproven_spawn_identity(
    project_root: Path,
    job_id: str,
    *,
    pid: int,
    pgid: int,
    handle: str,
    expected_starttime: str | None,
    exc: BaseException,
) -> None:
    """Keep non-terminal job + spawn identity when kill cannot be proven.

    Never stamp FAILED while the child may still be alive — cancel_job treats
    FAILED as terminal and would skip signalling the orphan.

    Identity MUST be durable: prefer ``spawn_identity.json``, then job.json via
    ``update_job_fields``. Swallowing a total persist failure is forbidden —
    raises ``E_JOB_CANCEL_UNPROVEN`` so cancel/stop refuse until OS proof.
    """
    durable = False
    try:
        _write_spawn_identity_recovery(
            project_root,
            job_id,
            pid=int(pid),
            pgid=int(pgid),
            handle=str(handle),
            pid_starttime=expected_starttime,
            reason=f"unproven_spawn:{exc}",
        )
        durable = True
    except Exception:
        durable = False

    try:
        cur = read_job_record(project_root, job_id)
    except JobStoreError:
        cur = None
    if cur is not None and cur.state not in TERMINAL_STATES:
        try:
            update_job_fields(
                project_root,
                job_id,
                pid=int(pid),
                pgid=int(pgid),
                handle=str(handle),
                pid_starttime=expected_starttime,
                error_message=(
                    f"launch commit failed after spawn; cleanup disappearance "
                    f"unproven: {exc}"
                ),
            )
            durable = True
        except JobStoreError:
            pass

    if durable:
        return

    # Last resort: uncertain marker (no pid) so cancel_job cannot false-green.
    try:
        _mark_spawn_uncertain(
            project_root,
            job_id,
            detail=f"identity persist failed after unproven spawn: {exc}",
        )
        return
    except Exception as mark_exc:
        raise JobStoreError(
            f"job {job_id} unproven spawn identity could not be persisted "
            f"(recovery+job.json+uncertain marker failed): {mark_exc}",
            code="E_JOB_CANCEL_UNPROVEN",
        ) from mark_exc


def _cleanup_after_spawn_commit_failure(
    project_root: Path,
    job_id: str,
    *,
    pid: int,
    pgid: int,
    handle: str,
    exc: BaseException,
    expected_starttime: str | None = None,
) -> bool:
    """Kill the spawned child when ownership is certain; reconcile fail-closed.

    Returns True iff spawn disappearance is proven. When fingerprint/probe
    uncertain, retain spawn identity on a non-terminal record and return False.
    """
    proven = _kill_child_exact(pid, pgid, expected_starttime=expected_starttime)
    if not proven:
        _persist_unproven_spawn_identity(
            project_root,
            job_id,
            pid=pid,
            pgid=pgid,
            handle=handle,
            expected_starttime=expected_starttime,
            exc=exc,
        )
        return False

    try:
        cur = read_job_record(project_root, job_id)
    except JobStoreError:
        # Unreadable — child kill was proven; best-effort failed if we can.
        _best_effort_stamp_failed(
            project_root,
            job_id,
            message=f"launch commit failed after spawn (unreadable): {exc}",
            spawn_pid=pid,
            spawn_pgid=pgid,
            spawn_handle=handle,
        )
        return True

    if cur.state in TERMINAL_STATES:
        # Another winner (cancel/failed/succeeded) — keep durable terminal.
        return True

    _best_effort_stamp_failed(
        project_root,
        job_id,
        message=f"launch commit failed after spawn: {exc}",
        spawn_pid=pid,
        spawn_pgid=pgid,
        spawn_handle=handle,
    )
    return True


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
    from omg_cli.jobs.retry import classified_terminal_updates

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
                updates=classified_terminal_updates(
                    state=JobState.FAILED,
                    exit_obj={"class": "spawn_error", "returncode": 1},
                    error_message=message,
                    pid=None,
                    pgid=None,
                    handle=None,
                ),
            )
            _clear_spawn_identity_recovery(project_root, job_id)
            _clear_spawn_uncertain(project_root, job_id)
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
                    updates=classified_terminal_updates(
                        state=JobState.FAILED,
                        exit_obj={"class": "spawn_error", "returncode": 1},
                        error_message=message,
                        # Keep pid/pgid for forensics; child is already killed.
                    ),
                )
                _clear_spawn_identity_recovery(project_root, job_id)
                _clear_spawn_uncertain(project_root, job_id)
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
    stop_on_recovery_required: bool = False,
) -> tuple[JobRecord, bool]:
    """Poll until terminal or timeout. Timeout does **not** cancel.

    Returns ``(record, timed_out)``.

    When ``stop_on_recovery_required`` is True (CLI default), raises
    ``E_JOB_RECOVERY_REQUIRED`` if observation health requires operator action.
    """
    from omg_cli.jobs.recovery import RECOVERY_REQUIRED_HEALTH, observe_job

    job_id = safe_job_id(job_id)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        record = read_job_record(project_root, job_id)
        if record.state in TERMINAL_STATES:
            return record, False
        if stop_on_recovery_required:
            obs = observe_job(project_root, job_id)
            if obs.health in RECOVERY_REQUIRED_HEALTH:
                err = JobStoreError(
                    f"job {job_id} requires recovery "
                    f"(health={obs.health.value})",
                    code="E_JOB_RECOVERY_REQUIRED",
                )
                err.observation = obs  # type: ignore[attr-defined]
                raise err
        if time.monotonic() >= deadline:
            return record, True
        time.sleep(max(0.01, float(poll_s)))


def list_jobs(
    project_root: Path,
    *,
    state: str | None = None,
    provider: str | None = None,
    run_id: str | None = None,
    observe: bool = False,
) -> list[dict[str, Any]]:
    from omg_cli.jobs.recovery import observe_job

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
        body = rec.public_status()
        if observe:
            try:
                body["observation"] = observe_job(project_root, jid).to_public_dict()
            except JobStoreError:
                body["observation"] = None
        out.append(body)
    return out


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

    # Fail-closed: claimed launch/bind without complete PID/PGID — do not
    # speculative-kill outer (would orphan agy) and do not claim cancelled.
    # Covers launching-unbound and bound-but-incomplete (same as recover/retry).
    from omg_cli.jobs.recovery import (
        provider_incomplete_reason,
        provider_launch_unbound,
        spawn_identity_unproven,
    )

    incomplete = provider_incomplete_reason(record)
    if incomplete is not None or provider_launch_unbound(record):
        reason = incomplete or "provider_launch_unbound"
        raise JobStoreError(
            f"job {job_id} provider identity incomplete ({reason}); "
            "refusing speculative cancel (E_JOB_CANCEL_UNPROVEN)",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    if spawn_identity_unproven(project_root, job_id):
        raise JobStoreError(
            f"job {job_id} spawn_identity.json present but "
            "malformed/incomplete; refusing speculative cancel "
            "(E_JOB_CANCEL_UNPROVEN)",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    # Capture identities once. Re-reads / mark_provider_exited must not drop
    # them from the cancel gate without OS-level disappearance proof.
    # Fall back to spawn_identity.json when job.json still has pid=null after
    # an unproven post-spawn commit (update_job_fields may have failed).
    captured_runner = _runner_identity(record)
    if captured_runner is None:
        captured_runner = _read_spawn_identity_recovery(project_root, job_id)
    captured_provider = _provider_identity(record)

    # Fail-closed: nonterminal job with no durable runner identity cannot be
    # speculative-cancelled when spawn was uncertain or an ACP binding still
    # points at this job (STARTING+pid=null would otherwise stamp CANCELLED
    # and orphan the live child).
    if (
        captured_runner is None
        and captured_provider is None
        and record.state not in TERMINAL_STATES
        and record.state != JobState.QUEUED
        and record.state != JobState.CANCELLED
    ):
        if _spawn_uncertain(project_root, job_id) or _acp_binding_references_job(
            project_root, job_id
        ):
            raise JobStoreError(
                f"job {job_id} has no durable runner identity while spawn is "
                "uncertain or ACP-bound; refusing speculative cancel "
                "(E_JOB_CANCEL_UNPROVEN)",
                code="E_JOB_CANCEL_UNPROVEN",
            )

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
        _clear_spawn_identity_recovery(project_root, job_id)
        _clear_spawn_uncertain(project_root, job_id)
        return record
    if record.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.LOST}:
        _clear_spawn_identity_recovery(project_root, job_id)
        _clear_spawn_uncertain(project_root, job_id)
        return record

    from omg_cli.jobs.retry import classified_terminal_updates

    updates = classified_terminal_updates(
        state=JobState.CANCELLED,
        exit_obj={
            "class": "cancelled",
            "returncode": -signal.SIGKILL,
            "ok": False,
            "timed_out": False,
            "cancelled": True,
        },
        cancel_reason=reason or record.cancel_reason or "operator",
        error_message=None,
    )

    try:
        if record.state in {JobState.STARTING, JobState.RUNNING}:
            out = transition_job(
                project_root,
                job_id,
                JobState.CANCELLED,
                updates=updates,
            )
            _clear_spawn_identity_recovery(project_root, job_id)
            _clear_spawn_uncertain(project_root, job_id)
            return out
        if record.state == JobState.QUEUED:
            transition_job(project_root, job_id, JobState.STARTING)
            out = transition_job(
                project_root,
                job_id,
                JobState.CANCELLED,
                updates=updates,
            )
            _clear_spawn_identity_recovery(project_root, job_id)
            _clear_spawn_uncertain(project_root, job_id)
            return out
    except TransitionError:
        # Runner may have stamped CANCELLED/succeeded concurrently — only
        # accept CANCELLED after re-proving disappearance with captured ids.
        cur = read_job_record(project_root, job_id)
        if cur.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.LOST}:
            _clear_spawn_identity_recovery(project_root, job_id)
            _clear_spawn_uncertain(project_root, job_id)
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
            _clear_spawn_identity_recovery(project_root, job_id)
            _clear_spawn_uncertain(project_root, job_id)
            return cur
        raise
    return record


def _assert_prior_attempt_gone(project_root: Path, record: JobRecord) -> None:
    """Refuse retry while prior runner/provider identity may still be live.

    Tri-state probe:
    - GONE / verified REUSED → proceed
    - LIVE → ``E_JOB_RETRY_LIVE``
    - UNPROVEN → ``E_JOB_CANCEL_UNPROVEN``

    Also refuse claimed provider launch/bind without a complete durable
    PID/PGID (``launching`` unbound *or* ``bound`` incomplete), and
    present-but-malformed ``spawn_identity.json`` — same fail-closed
    semantics as cancel/recover (never treat incomplete as absent).
    Malformed or wrongly-marked ``lost`` records must not bypass this gate.
    """
    from omg_cli.jobs.ownership import probe_identity_for_recovery
    from omg_cli.jobs.recovery import (
        provider_incomplete_reason,
        provider_launch_unbound,
        spawn_identity_unproven,
    )

    if _spawn_uncertain(project_root, record.job_id):
        raise JobStoreError(
            f"job {record.job_id} has uncertain spawn identity; refusing retry",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    if spawn_identity_unproven(project_root, record.job_id):
        raise JobStoreError(
            f"job {record.job_id} spawn_identity.json present but "
            "malformed/incomplete; refusing retry",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    incomplete = provider_incomplete_reason(record)
    if incomplete is not None or provider_launch_unbound(record):
        reason = incomplete or "provider_launch_unbound"
        raise JobStoreError(
            f"job {record.job_id} provider identity incomplete "
            f"({reason}); refusing retry until identity is proven gone",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    runner = _runner_identity(record)
    if runner is None:
        runner = _read_spawn_identity_recovery(project_root, record.job_id)
    provider = _provider_identity(record)

    for identity, label in ((runner, "runner"), (provider, "provider")):
        if identity is None:
            continue
        outcome = probe_identity_for_recovery(identity)
        if outcome is IdentityProbeOutcome.GONE:
            _reap_child(identity.pid)
            continue
        if outcome is IdentityProbeOutcome.REUSED:
            # Verified different occupant — never signal; allow reclaim.
            continue
        if outcome is IdentityProbeOutcome.LIVE:
            # Terminal stamp can race the runner unwind — wait briefly.
            if _wait_until_gone(identity.pid, timeout_s=2.0):
                continue
            # Re-probe after wait.
            outcome2 = probe_identity_for_recovery(identity)
            if outcome2 is IdentityProbeOutcome.GONE:
                continue
            if outcome2 is IdentityProbeOutcome.REUSED:
                continue
            if outcome2 is IdentityProbeOutcome.LIVE:
                raise JobStoreError(
                    f"job {record.job_id} {label} process still live; refusing retry",
                    code="E_JOB_RETRY_LIVE",
                )
            raise JobStoreError(
                f"job {record.job_id} {label} identity unproven; refusing retry",
                code="E_JOB_CANCEL_UNPROVEN",
            )
        # UNPROVEN
        raise JobStoreError(
            f"job {record.job_id} {label} identity unproven; refusing retry",
            code="E_JOB_CANCEL_UNPROVEN",
        )


def preflight_retry_job(
    project_root: Path,
    job_id: str,
    *,
    attempt: int,
    intent: Any = None,
    now: datetime | None = None,
) -> JobRecord:
    """Pre-mutation retry admission (no archive / state change / launch).

    Sequence: validate id → read → assert_retry_admission → prior-gone →
    revalidate_stored_request. Returns the validated current record.
    """
    from omg_cli.jobs.providers import revalidate_stored_request
    from omg_cli.jobs.retry import RetryIntent, assert_retry_admission

    resolved = intent if intent is not None else RetryIntent.EXPLICIT
    if not isinstance(resolved, RetryIntent):
        resolved = RetryIntent(str(resolved))

    jid = safe_job_id(job_id)
    record = read_job_record(project_root, jid)
    assert_retry_admission(record, attempt=attempt, intent=resolved, now=now)
    _assert_prior_attempt_gone(project_root, record)
    revalidate_stored_request(record.provider, record.request)
    return record


def retry_job(
    project_root: Path,
    job_id: str,
    *,
    attempt: int,
    launch: bool = True,
    runner_python: str | None = None,
    intent: Any = None,
    now: datetime | None = None,
) -> StartResult:
    """Retry via shared path: preflight → prepare_retry → starting → launch.

    Default ``intent=explicit`` preserves public ``omg job retry`` semantics.
    Automatic intent is used by the bounded scheduler (#68 PR5) only.
    """
    from omg_cli.jobs.retry import RetryIntent
    from omg_cli.jobs.store import prepare_retry

    resolved = intent if intent is not None else RetryIntent.EXPLICIT
    if not isinstance(resolved, RetryIntent):
        resolved = RetryIntent(str(resolved))

    jid = safe_job_id(job_id)
    preflight_retry_job(
        project_root,
        jid,
        attempt=attempt,
        intent=resolved,
        now=now,
    )

    queued = prepare_retry(
        project_root,
        jid,
        next_attempt=int(attempt),
        intent=resolved,
        now=now,
    )
    # queued → starting before launch (same as start_job).
    starting = transition_job(project_root, queued.job_id, JobState.STARTING)
    if not launch:
        return StartResult(record=starting, launched=False)
    return launch_job_runner(
        project_root,
        starting.job_id,
        runner_python=runner_python,
    )


@dataclass(frozen=True, slots=True)
class GcResult:
    deleted: list[str]
    skipped: list[dict[str, str]]


def _team_binding_protects_job(project_root: Path, job_id: str) -> bool:
    """Extensible hook: future Team bindings may protect a job from GC.

    Currently always False (no Team job binding resolver). Kept as a single
    call site so PR3 GC can refuse when a future resolver reports protection.
    """
    del project_root, job_id
    return False


def gc_jobs(
    project_root: Path,
    *,
    retention_days: float,
) -> GcResult:
    """Delete terminal jobs older than retention; never touch nonterminal/ACP.

    Under the external job lock: re-read, recheck terminal/retention/bindings,
    prove recorded identities are gone, then atomically rename into
    ``.gc-quarantine/``. Delete the quarantined tree only after releasing the
    lock so retry cannot relaunch into a directory mid-rmtree.
    """
    from datetime import datetime, timedelta, timezone

    from omg_cli.jobs.store import (
        _validate_retention_days,
        delete_quarantined_tree,
        gc_candidates,
        job_lock,
        quarantine_job_dir,
    )

    root = Path(project_root).resolve()
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    days = _validate_retention_days(retention_days)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    for jid in gc_candidates(root, retention_days=days, now=now):
        quarantined: Path | None = None
        try:
            if _acp_binding_references_job(root, jid):
                skipped.append({"job_id": jid, "reason": "acp_binding"})
                continue
            if _team_binding_protects_job(root, jid):
                skipped.append({"job_id": jid, "reason": "team_binding"})
                continue
            with job_lock(root, jid):
                try:
                    rec = read_job_record(root, jid)
                except JobStoreError as exc:
                    skipped.append(
                        {
                            "job_id": jid,
                            "reason": f"malformed:{getattr(exc, 'code', 'E_JOB_MALFORMED')}",
                        }
                    )
                    continue
                if rec.state not in TERMINAL_STATES:
                    skipped.append({"job_id": jid, "reason": "nonterminal"})
                    continue
                if _acp_binding_references_job(root, jid):
                    skipped.append({"job_id": jid, "reason": "acp_binding"})
                    continue
                if _team_binding_protects_job(root, jid):
                    skipped.append({"job_id": jid, "reason": "team_binding"})
                    continue
                stamp_raw = rec.terminal_at or rec.updated_at or rec.created_at
                try:
                    stamp = datetime.fromisoformat(
                        str(stamp_raw).replace("Z", "+00:00")
                    )
                except (TypeError, ValueError):
                    skipped.append({"job_id": jid, "reason": "bad_timestamp"})
                    continue
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if stamp > cutoff:
                    skipped.append({"job_id": jid, "reason": "within_retention"})
                    continue
                block = _gc_identities_block_reason(root, rec)
                if block is not None:
                    skipped.append({"job_id": jid, "reason": block})
                    continue
                # Final binding recheck immediately before quarantine — covers a
                # binding that appeared after the earlier under-lock checks.
                if _acp_binding_references_job(root, jid):
                    skipped.append({"job_id": jid, "reason": "acp_binding"})
                    continue
                if _team_binding_protects_job(root, jid):
                    skipped.append({"job_id": jid, "reason": "team_binding"})
                    continue
                # Still holding lock — quarantine (rename) so retry cannot
                # requeue into this tree; delete only after unlock.
                quarantined = quarantine_job_dir(root, jid)
                if quarantined is None:
                    skipped.append({"job_id": jid, "reason": "already_absent"})
                    continue
            if quarantined is not None:
                delete_quarantined_tree(quarantined)
                deleted.append(jid)
        except JobStoreError as exc:
            skipped.append(
                {
                    "job_id": jid,
                    "reason": getattr(exc, "code", None) or "E_JOB_GC",
                }
            )
    return GcResult(deleted=deleted, skipped=skipped)


# ---------------------------------------------------------------------------
# #105 PR4 — durable ACP session sidecar ensure / reuse
# ---------------------------------------------------------------------------

DEFAULT_ACP_READY_TIMEOUT_S = 20.0
DEFAULT_ACP_HANDSHAKE_WAIT_S = 15.0


def _acp_may_clear_binding_after_start_failure(exc: BaseException) -> bool:
    """True only when spawn never happened or spawn disappearance is proven.

    Fail closed on missing attrs / uncertain cleanup: retain the pre-allocated
    job-bearing binding so a later ensure cannot double-spawn an orphan.
    """
    spawned = getattr(exc, "spawned", None)
    proven = getattr(exc, "disappearance_proven", None)
    if spawned is False:
        return True
    if proven is True:
        return True
    return False


def _acp_lock_path(
    project_root: Path, run_id: str, session_id_hash: str, cwd_hash: str
) -> Path:
    from omg_cli.jobs.store import ensure_jobs_root

    root = ensure_jobs_root(project_root)
    locks = root / ".locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    key = f"acp-{run_id}-{session_id_hash[:16]}-{cwd_hash[:16]}.lock"
    return locks / key


def _acp_binding_path(project_root: Path, run_id: str) -> Path:
    from omg_cli.jobs.store import ensure_jobs_root

    root = ensure_jobs_root(project_root)
    d = root / "acp_bindings"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / f"{run_id}.json"


def read_acp_sidecar_binding(
    project_root: Path, run_id: str
) -> dict[str, Any] | None:
    """Public read of the Team/jobs ACP singleton binding (may lack job_id)."""
    path = _acp_binding_path(Path(project_root).resolve(), str(run_id))
    return _read_json_file(path) if path.is_file() else None


_TEAM_STOP_STATES_BLOCKING_ACP = frozenset({"stopping", "stopped", "stop_refused"})


def _team_acp_stop_block_reason(project_root: Path, run_id: str) -> str | None:
    """Return a reason when Team stop state must abort ACP ensure; else None."""
    try:
        from omg_cli.team.plane import load_team_meta

        meta = load_team_meta(Path(project_root).resolve(), str(run_id))
    except Exception:
        # No team meta (unit tests / non-team) — ensure proceeds.
        return None
    st = str(meta.get("stop_state") or "")
    if st in _TEAM_STOP_STATES_BLOCKING_ACP or meta.get("stopped_at"):
        return f"team stop_state={st!r}"
    return None


def _acp_ensure_lock_busy(project_root: Path, run_id: str) -> bool:
    """True when an ACP ensure exclusive lock for this run appears held."""
    import fcntl

    from omg_cli.jobs.store import ensure_jobs_root

    root = ensure_jobs_root(Path(project_root).resolve())
    locks = root / ".locks"
    if not locks.is_dir():
        return False
    prefix = f"acp-{run_id}-"
    try:
        candidates = [
            p for p in locks.iterdir() if p.is_file() and p.name.startswith(prefix)
        ]
    except OSError:
        return False
    for path in candidates:
        try:
            with path.open("a+", encoding="utf-8") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                else:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        except OSError:
            continue
    return False


def _pending_acp_intent_idle(
    linked_acp: Mapping[str, Any], *, max_age_s: float = 60.0
) -> bool:
    """True when pending_at is parseable and older than *max_age_s* (abandoned)."""
    from datetime import datetime, timezone

    raw = linked_acp.get("pending_at")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age >= float(max_age_s)


def _acp_lock_paths_for_run(
    project_root: Path, run_id: str, *, binding: Mapping[str, Any] | None
) -> list[Path]:
    """Resolve ACP transaction lock path(s) for *run_id* (prefer binding hashes)."""
    root = Path(project_root).resolve()
    rid = str(run_id)
    paths: list[Path] = []
    if isinstance(binding, Mapping):
        sid = binding.get("session_id_hash")
        cwd = binding.get("cwd_hash")
        if isinstance(sid, str) and sid and isinstance(cwd, str) and cwd:
            paths.append(_acp_lock_path(root, rid, sid, cwd))
    if not paths:
        try:
            ident = resolve_acp_session_identity(root, rid)
            paths.append(
                _acp_lock_path(
                    root,
                    ident["run_id"],
                    ident["session_id_hash"],
                    ident["cwd_hash"],
                )
            )
        except JobStoreError:
            pass
    if not paths:
        from omg_cli.jobs.store import ensure_jobs_root

        locks = ensure_jobs_root(root) / ".locks"
        prefix = f"acp-{rid}-"
        if locks.is_dir():
            try:
                paths.extend(
                    sorted(
                        p
                        for p in locks.iterdir()
                        if p.is_file() and p.name.startswith(prefix)
                    )
                )
            except OSError:
                pass
    return paths


def resolve_acp_binding_for_team_stop(
    project_root: Path,
    run_id: str,
    *,
    reason: str = "team_stop",
    force: bool = False,
    linked_acp: Mapping[str, Any] | None = None,
    _before_lock_hook: Callable[[dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    """Resolve ACP binding for Team stop under the ACP transaction lock.

    Acquires the exact ensure lock, **re-reads** the binding, then:
    - ``ensuring`` + no ``job_id`` → CAS unlink (only if still that marker)
    - binding with ``job_id`` → cancel; unproven cancel refuses stop
    - no binding → pending idle/force clear decision for the caller

    ``_before_lock_hook`` is test-only (delay after peek, before lock).
    """
    from contextlib import ExitStack

    from omg_cli.contracts.path_keys import exclusive_lock

    root = Path(project_root).resolve()
    rid = str(run_id)
    bind_path = _acp_binding_path(root, rid)
    peek = _read_json_file(bind_path) if bind_path.is_file() else None
    if _before_lock_hook is not None:
        _before_lock_hook(peek)

    lock_paths = _acp_lock_paths_for_run(root, rid, binding=peek)
    out: dict[str, Any] = {
        "status": "no_binding",
        "binding_cleared": False,
        "cancelled": False,
        "attempted": False,
        "stop_ok": True,
        "session_close": False,
        "actions": [],
        "errors": [],
    }

    def _under_locks(stack: ExitStack) -> dict[str, Any]:
        for lp in sorted({str(p): p for p in lock_paths}.values(), key=str):
            stack.enter_context(exclusive_lock(lp))

        binding = _read_json_file(bind_path) if bind_path.is_file() else None
        if not isinstance(binding, Mapping):
            # No binding under lock — pending-only recovery for caller.
            has_pending = (
                isinstance(linked_acp, Mapping)
                and str(linked_acp.get("state") or "") == "pending"
            )
            if not has_pending:
                return {**out, "status": "no_binding", "stop_ok": True}
            # We hold the ACP transaction lock(s); do not NB-probe ourselves.
            idle = _pending_acp_intent_idle(linked_acp) if linked_acp else False
            if force or idle:
                return {
                    **out,
                    "status": "cleared_pending",
                    "stop_ok": True,
                    "actions": [
                        "cleared abandoned linked_acp_session pending "
                        "(proven idle / --force; no live sidecar)"
                    ],
                }
            return {
                **out,
                "status": "refused_fresh_pending",
                "stop_ok": False,
                "errors": [
                    "linked_acp_session pending without binding; "
                    "ensure may still be in flight; stop_refused"
                ],
                "actions": [
                    "fresh ACP pending; stop_refused until idle/force "
                    "or cancel target exists"
                ],
            }

        state = str(binding.get("state") or "")
        job_id = binding.get("job_id")
        out["binding"] = dict(binding)

        # CAS: unlink only if still ensuring with no job_id under the lock,
        # and no live matching ACP sidecar exists for this run/session/cwd.
        if state == "ensuring" and not job_id:
            again = _read_json_file(bind_path) if bind_path.is_file() else None
            if (
                isinstance(again, Mapping)
                and str(again.get("state") or "") == "ensuring"
                and not again.get("job_id")
            ):
                bind_sid = str(again.get("session_id_hash") or "")
                bind_cwd = str(again.get("cwd_hash") or "")
                live_ids: list[str] = []
                if bind_sid and bind_cwd:
                    live_ids = _matching_acp_sidecar_job_ids(
                        root,
                        run_id=rid,
                        session_id_hash=bind_sid,
                        cwd_hash=bind_cwd,
                    )
                if live_ids:
                    # Belt-and-suspenders: treat as job-bearing; do not unlink.
                    job_id = live_ids[0]
                    out["binding"] = {
                        **dict(again),
                        "job_id": job_id,
                        "recovery": "stop_repaired_null_ensuring",
                    }
                    try:
                        _write_acp_binding_for_job(
                            root,
                            job_id,
                            bind_path,
                            {
                                "run_id": rid,
                                "job_id": job_id,
                                "session_id_hash": bind_sid,
                                "cwd_hash": bind_cwd,
                                "state": "handshaking",
                                "recovery": "stop_repaired_null_ensuring",
                            },
                        )
                    except Exception:
                        pass
                    # Fall through to cancel path with job_id set.
                else:
                    try:
                        bind_path.unlink()
                    except OSError as exc:
                        return {
                            **out,
                            "status": "refused_unlink",
                            "stop_ok": False,
                            "errors": [f"ensuring binding unlink failed: {exc}"],
                        }
                    return {
                        **out,
                        "status": "cleared_ensuring",
                        "binding_cleared": True,
                        "stop_ok": True,
                        "actions": [
                            "cleared abandoned ACP ensuring binding "
                            "(CAS under ACP lock; no job_id; no live sidecar)"
                        ],
                    }
            # Promoted between reads while we held the lock — should not happen;
            # fall through to job_id handling with fresh read.
            if not (isinstance(job_id, str) and job_id):
                binding = again if isinstance(again, Mapping) else binding
                state = str(binding.get("state") or "")
                job_id = binding.get("job_id")
                out["binding"] = dict(binding)

        if isinstance(job_id, str) and job_id:
            try:
                cancel_job(root, job_id, reason=reason)
            except JobStoreError as cancel_exc:
                code = getattr(cancel_exc, "code", None) or "E_ACP_CANCEL"
                if code in {"E_JOB_UNKNOWN", "E_JOB_NOT_FOUND"}:
                    try:
                        if bind_path.is_file():
                            bind_path.unlink()
                    except OSError:
                        pass
                    return {
                        **out,
                        "status": "cancelled",
                        "attempted": True,
                        "cancelled": True,
                        "binding_cleared": True,
                        "job_id": job_id,
                        "stop_ok": True,
                        "actions": [
                            f"cancelled linked_acp_session sidecar job_id={job_id} "
                            f"(already absent {code})"
                        ],
                    }
                return {
                    **out,
                    "status": "cancel_unproven",
                    "attempted": True,
                    "cancelled": False,
                    "job_id": job_id,
                    "stop_ok": False,
                    "error": str(cancel_exc),
                    "error_code": code,
                    "errors": [
                        f"linked_acp_session cancel: {cancel_exc} ({code})"
                    ],
                    "actions": [
                        "linked_acp_session cancel unproven; "
                        "stop_refused (binding retained)"
                    ],
                }
            # Proven cancel — clear binding under the same lock.
            try:
                if bind_path.is_file():
                    bind_path.unlink()
            except OSError as exc:
                return {
                    **out,
                    "status": "cancelled",
                    "attempted": True,
                    "cancelled": True,
                    "job_id": job_id,
                    "stop_ok": True,
                    "error": f"binding unlink failed after proven cancel: {exc}",
                    "actions": [
                        f"cancelled linked_acp_session sidecar job_id={job_id} "
                        "(sidecar cancellation; not session/close)"
                    ],
                }
            return {
                **out,
                "status": "cancelled",
                "attempted": True,
                "cancelled": True,
                "binding_cleared": True,
                "job_id": job_id,
                "stop_ok": True,
                "actions": [
                    f"cancelled linked_acp_session sidecar job_id={job_id} "
                    "(sidecar cancellation; not session/close)"
                ],
            }

        # Binding without job_id but not ensuring (unexpected) — refuse.
        return {
            **out,
            "status": "refused_unexpected_binding",
            "stop_ok": False,
            "errors": [
                f"linked_acp_session binding state={state!r} without job_id; "
                "stop_refused"
            ],
            "actions": [
                "unexpected ACP binding shape; stop_refused (binding retained)"
            ],
        }

    if not lock_paths:
        # No lock file and no hashes — still handle pending-only / absent.
        if peek is None:
            has_pending = (
                isinstance(linked_acp, Mapping)
                and str(linked_acp.get("state") or "") == "pending"
            )
            if not has_pending:
                return out
            idle = _pending_acp_intent_idle(linked_acp) if linked_acp else False
            if force or idle:
                out["status"] = "cleared_pending"
                out["actions"] = [
                    "cleared abandoned linked_acp_session pending "
                    "(proven idle / --force; no live sidecar)"
                ]
                return out
            out["status"] = "refused_fresh_pending"
            out["stop_ok"] = False
            out["errors"] = [
                "linked_acp_session pending without binding; "
                "ensure may still be in flight; stop_refused"
            ]
            out["actions"] = [
                "fresh ACP pending; stop_refused until idle/force "
                "or cancel target exists"
            ]
            return out
        # Binding exists but no lock path — derive a lock from peek hashes hard fail
        sid = peek.get("session_id_hash") if isinstance(peek, Mapping) else None
        cwd = peek.get("cwd_hash") if isinstance(peek, Mapping) else None
        if isinstance(sid, str) and isinstance(cwd, str) and sid and cwd:
            lock_paths = [_acp_lock_path(root, rid, sid, cwd)]
        else:
            out["status"] = "refused_no_lock"
            out["stop_ok"] = False
            out["errors"] = [
                "ACP binding present but lock path unresolved; stop_refused"
            ]
            return out

    with ExitStack() as stack:
        return _under_locks(stack)


def resolve_acp_session_identity(
    project_root: Path, run_id: str
) -> dict[str, Any]:
    """Resolve canonical run_id + load_host_session(required=True) + cwd hashes.

    Fail closed on missing/malformed/ambiguous binding — no spawn.
    """
    from omg_cli.contracts.state_schemas import require_safe_id
    from omg_cli.host_acp import hash_cwd, hash_session_id
    from omg_cli.host_session import HostSessionError, load_host_session
    from omg_cli.state import load_run

    rid = require_safe_id(run_id, label="run_id")
    root = Path(project_root).resolve()
    run = load_run(root, rid)
    if run is None:
        raise JobStoreError(
            f"run {rid} missing; cannot bind ACP session",
            code="E_ACP_SESSION_BINDING",
        )
    try:
        binding = load_host_session(run, required=True)
    except HostSessionError as exc:
        raise JobStoreError(
            f"host session binding unavailable: {exc}",
            code="E_ACP_SESSION_BINDING",
        ) from exc
    if binding is None:
        raise JobStoreError(
            "host session binding missing",
            code="E_ACP_SESSION_BINDING",
        )
    cwd = str(root)
    return {
        "run_id": rid,
        "session_id": binding.session_id,
        "cwd": cwd,
        "session_id_hash": hash_session_id(binding.session_id),
        "cwd_hash": hash_cwd(cwd),
    }


def _read_json_file(path: Path) -> dict[str, Any] | None:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_file(path: Path, data: Mapping[str, Any]) -> None:
    import json

    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes

    body = json.dumps(dict(data), sort_keys=True, indent=2).encode("utf-8")
    atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=True)


def _load_receipt(project_root: Path, job_id: str) -> dict[str, Any] | None:
    from omg_cli.jobs.acp_provider import receipt_path

    path = receipt_path(job_dir(project_root, job_id))
    if not path.is_file():
        return None
    return _read_json_file(path)


def _outer_runner_alive(record: JobRecord) -> bool:
    """True when the durable outer runner handle is still the live process."""
    if record.state != JobState.RUNNING:
        return False
    if record.pid is None:
        return False
    pgid = record.pgid if record.pgid is not None else record.pid
    identity = ProcessIdentity(
        pid=int(record.pid),
        pgid=int(pgid),
        pid_starttime=record.pid_starttime,
    )
    try:
        from omg_cli.jobs.ownership import assert_ownership

        outcome = assert_ownership(
            identity, job_id=record.job_id, label="runner"
        )
    except JobStoreError:
        return False
    return outcome is OwnershipOutcome.OK


def _provider_process_bound_alive(record: JobRecord) -> bool:
    """True when provider_process is bound and the exact ACP peer is still live.

    Fingerprint-aware: zombie / PID-reuse / pgid mismatch → not live.
    ``pending`` / ``launching`` / ``exited`` are not live (no owned ACP peer).
    """
    pp = record.provider_process or default_provider_process()
    if str(pp.get("state") or "") != "bound":
        return False
    pid = pp.get("pid")
    pgid = pp.get("pgid")
    if pid is None or pgid is None:
        return False
    identity = ProcessIdentity(
        pid=int(pid),
        pgid=int(pgid),
        pid_starttime=(
            str(pp["pid_starttime"]) if pp.get("pid_starttime") is not None else None
        ),
    )
    try:
        from omg_cli.jobs.ownership import assert_ownership

        outcome = assert_ownership(
            identity, job_id=record.job_id, label="provider"
        )
    except JobStoreError:
        return False
    return outcome is OwnershipOutcome.OK


def _job_is_live_sidecar(project_root: Path, job_id: str) -> bool:
    """Live ACP sidecar requires outer runner AND bound inner ACP peer.

    Outer-only liveness is insufficient: a dead/zombie ACP child with a still-
    running outer runner must not count as reusable (P0 transient false success).
    """
    try:
        rec = read_job_record(project_root, job_id)
    except JobStoreError:
        return False
    if not _outer_runner_alive(rec):
        return False
    return _provider_process_bound_alive(rec)


def _job_handshake_still_viable(project_root: Path, job_id: str) -> bool:
    """Handshaking job: outer must be alive; bound peer must stay alive if present."""
    try:
        rec = read_job_record(project_root, job_id)
    except JobStoreError:
        return False
    if not _outer_runner_alive(rec):
        return False
    pp = rec.provider_process or default_provider_process()
    state = str(pp.get("state") or "pending")
    if state in {"pending", "launching"}:
        return True
    if state == "bound":
        return _provider_process_bound_alive(rec)
    # exited / unknown — not viable
    return False


def _matching_acp_sidecar_job_ids(
    project_root: Path,
    *,
    run_id: str,
    session_id_hash: str,
    cwd_hash: str,
) -> list[str]:
    """Return ACP sidecar job ids matching (run, session, cwd) that are still live."""
    root = Path(project_root).resolve()
    rid = str(run_id)
    sid = str(session_id_hash)
    cwd = str(cwd_hash)
    found: list[str] = []
    for jid in list_job_ids(root):
        try:
            rec = read_job_record(root, jid)
        except JobStoreError:
            continue
        if str(rec.provider or "") != "grok-acp-session":
            continue
        if rec.run_id is not None and str(rec.run_id) != rid:
            continue
        req = rec.request if isinstance(rec.request, Mapping) else {}
        if str(req.get("session_id_hash") or "") != sid:
            continue
        if str(req.get("cwd_hash") or "") != cwd:
            continue
        if _job_is_live_sidecar(root, jid) or _job_handshake_still_viable(root, jid):
            found.append(jid)
    return found


def _cancel_orphan_acp_sidecar(
    project_root: Path, job_id: str, *, reason: str
) -> None:
    """Best-effort cancel of a linked sidecar that is no longer fully live."""
    try:
        cancel_job(project_root, job_id, reason=reason)
    except JobStoreError:
        pass


def _wait_acp_ready(
    project_root: Path,
    job_id: str,
    *,
    session_id_hash: str,
    cwd_hash: str,
    parent_run_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    from omg_cli.host_acp import validate_receipt

    deadline = time.monotonic() + max(0.1, float(timeout_s))
    while time.monotonic() < deadline:
        if not _job_handshake_still_viable(project_root, job_id):
            # Job / ACP peer died before ready — fail closed (no transient success).
            try:
                rec = read_job_record(project_root, job_id)
                err = rec.error_message or f"state={rec.state.value}"
            except JobStoreError:
                err = "job unreadable"
            raise JobStoreError(
                f"ACP sidecar exited before ready: {err}",
                code="E_ACP_SIDECAR_DEAD",
            )
        raw = _load_receipt(project_root, job_id)
        if raw is not None:
            # Receipt published — refuse success unless inner ACP peer is still live.
            if not _job_is_live_sidecar(project_root, job_id):
                raise JobStoreError(
                    "ACP resume receipt present but inner provider process is gone",
                    code="E_ACP_SIDECAR_DEAD",
                )
            return validate_receipt(
                raw,
                session_id_hash=session_id_hash,
                cwd_hash=cwd_hash,
                parent_run_id=parent_run_id,
            ).to_dict()
        time.sleep(0.05)
    raise JobStoreError(
        "ACP sidecar readiness timed out",
        code="E_ACP_READY_TIMEOUT",
    )


def ensure_acp_session_sidecar(
    project_root: Path,
    *,
    run_id: str,
    provider_binary: str | None = None,
    ready_timeout_s: float = DEFAULT_ACP_READY_TIMEOUT_S,
    handshake_timeout_s: float = DEFAULT_ACP_HANDSHAKE_WAIT_S,
    _pre_spawn_hook: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Ensure one live ACP sidecar for (run_id, session, cwd); reuse if matching.

    Holds a dedicated transaction lock — never the Team scale lock.
    Writes a provisional ``ensuring`` binding **before** ``start_job`` so Team
    stop can observe in-flight ensure. Aborts if Team is stopping/stopped.
    ``_pre_spawn_hook`` is test-only (barrier between ensuring bind and spawn).
    """
    from omg_cli.contracts.path_keys import exclusive_lock
    from omg_cli.host_acp import AcpError

    root = Path(project_root).resolve()
    identity = resolve_acp_session_identity(root, run_id)
    rid = identity["run_id"]
    sid_hash = identity["session_id_hash"]
    cwd_hash = identity["cwd_hash"]
    lock_path = _acp_lock_path(root, rid, sid_hash, cwd_hash)

    with exclusive_lock(lock_path):
        bind_path = _acp_binding_path(root, rid)
        existing = _read_json_file(bind_path) if bind_path.is_file() else None

        # Reclaim abandoned pre-spawn marker (no job_id yet) — but NEVER unlink
        # when a live ACP sidecar still exists for this (run, session, cwd).
        if (
            existing
            and str(existing.get("state") or "") == "ensuring"
            and not existing.get("job_id")
        ):
            bind_sid = str(existing.get("session_id_hash") or sid_hash)
            bind_cwd = str(existing.get("cwd_hash") or cwd_hash)
            live_ids = _matching_acp_sidecar_job_ids(
                root,
                run_id=rid,
                session_id_hash=bind_sid,
                cwd_hash=bind_cwd,
            )
            if live_ids:
                # Repair binding to the live job — do not orphan via unlink.
                repaired_id = live_ids[0]
                _write_acp_binding_for_job(
                    root,
                    repaired_id,
                    bind_path,
                    {
                        "run_id": rid,
                        "job_id": repaired_id,
                        "session_id_hash": bind_sid,
                        "cwd_hash": bind_cwd,
                        "state": "handshaking",
                        "recovery": "repaired_null_ensuring_marker",
                    },
                )
                existing = _read_json_file(bind_path)
            else:
                try:
                    bind_path.unlink()
                except OSError:
                    pass
                existing = None

        if existing:
            ex_sid = existing.get("session_id_hash")
            ex_cwd = existing.get("cwd_hash")
            ex_job = existing.get("job_id")
            if ex_sid != sid_hash or ex_cwd != cwd_hash:
                raise JobStoreError(
                    "stale/conflicting ACP sidecar binding for run "
                    "(session/cwd hash mismatch); refusing silent retry",
                    code="E_ACP_SIDECAR_CONFLICT",
                )
            if isinstance(ex_job, str) and ex_job:
                if _job_is_live_sidecar(root, ex_job):
                    receipt = _load_receipt(root, ex_job)
                    if receipt is not None:
                        from omg_cli.host_acp import validate_receipt

                        try:
                            validated = validate_receipt(
                                receipt,
                                session_id_hash=sid_hash,
                                cwd_hash=cwd_hash,
                                parent_run_id=rid,
                            ).to_dict()
                        except AcpError as exc:
                            raise JobStoreError(
                                f"linked ACP receipt invalid: {exc}",
                                code="E_ACP_RECEIPT",
                            ) from exc
                        # Re-check after receipt read — inner may have died.
                        if not _job_is_live_sidecar(root, ex_job):
                            _cancel_orphan_acp_sidecar(
                                root, ex_job, reason="acp_inner_dead_after_receipt"
                            )
                            raise JobStoreError(
                                f"linked ACP sidecar job {ex_job} inner provider "
                                "exited; refusing reuse (not live)",
                                code="E_ACP_SIDECAR_STALE",
                            )
                        block = _team_acp_stop_block_reason(root, rid)
                        if block:
                            raise JobStoreError(
                                f"ACP ensure aborted: {block}",
                                code="E_ACP_ENSURE_ABORTED_STOP",
                            )
                        rec = read_job_record(root, ex_job)
                        return {
                            "ok": True,
                            "reused": True,
                            "job_id": ex_job,
                            "attempt": int(rec.attempt),
                            "receipt": validated,
                            "receipt_sha256": validated.get("receipt_sha256"),
                            "connection_owned": True,
                            "transport": "acp_stdio_job",
                            "status": "resumed",
                        }
                    # Still handshaking — wait boundedly (outer + launching/bound).
                    if not _job_handshake_still_viable(root, ex_job):
                        _cancel_orphan_acp_sidecar(
                            root, ex_job, reason="acp_handshake_not_viable"
                        )
                        raise JobStoreError(
                            f"linked ACP sidecar job {ex_job} is not live; "
                            "refusing untracked retry (cancel/clear binding first)",
                            code="E_ACP_SIDECAR_STALE",
                        )
                    try:
                        validated = _wait_acp_ready(
                            root,
                            ex_job,
                            session_id_hash=sid_hash,
                            cwd_hash=cwd_hash,
                            parent_run_id=rid,
                            timeout_s=ready_timeout_s,
                        )
                    except JobStoreError:
                        raise
                    if not _job_is_live_sidecar(root, ex_job):
                        _cancel_orphan_acp_sidecar(
                            root, ex_job, reason="acp_dead_after_handshake_wait"
                        )
                        raise JobStoreError(
                            f"linked ACP sidecar job {ex_job} is not live after "
                            "handshake; refusing reuse",
                            code="E_ACP_SIDECAR_STALE",
                        )
                    block = _team_acp_stop_block_reason(root, rid)
                    if block:
                        raise JobStoreError(
                            f"ACP ensure aborted: {block}",
                            code="E_ACP_ENSURE_ABORTED_STOP",
                        )
                    rec = read_job_record(root, ex_job)
                    return {
                        "ok": True,
                        "reused": True,
                        "job_id": ex_job,
                        "attempt": int(rec.attempt),
                        "receipt": validated,
                        "receipt_sha256": validated.get("receipt_sha256"),
                        "connection_owned": True,
                        "transport": "acp_stdio_job",
                        "status": "resumed",
                    }
                # Stale / inner-dead linked job — cancel orphan outer, refuse retry.
                _cancel_orphan_acp_sidecar(
                    root, ex_job, reason="acp_sidecar_stale_not_live"
                )
                raise JobStoreError(
                    f"linked ACP sidecar job {ex_job} is not live "
                    "(outer or inner provider process gone); "
                    "refusing untracked retry (cancel/clear binding first)",
                    code="E_ACP_SIDECAR_STALE",
                )

        # Abort before allocating a new sidecar if Team stop is in progress.
        block = _team_acp_stop_block_reason(root, rid)
        if block:
            raise JobStoreError(
                f"ACP ensure aborted before spawn: {block}",
                code="E_ACP_ENSURE_ABORTED_STOP",
            )

        # Pre-allocate concrete job_id and publish it in the binding BEFORE
        # start_job launches a runner — never leave reclaimable ensuring+null
        # beside a RUNNING sidecar if a later handshaking/recovery write fails.
        job_id = make_job_id()
        _write_acp_binding_for_job(
            root,
            job_id,
            bind_path,
            {
                "run_id": rid,
                "job_id": job_id,
                "session_id_hash": sid_hash,
                "cwd_hash": cwd_hash,
                "state": "ensuring",
            },
        )
        if _pre_spawn_hook is not None:
            _pre_spawn_hook()
        block = _team_acp_stop_block_reason(root, rid)
        if block:
            try:
                if bind_path.is_file():
                    bind_path.unlink()
            except OSError:
                pass
            raise JobStoreError(
                f"ACP ensure aborted before spawn: {block}",
                code="E_ACP_ENSURE_ABORTED_STOP",
            )

        # Start a new internal ACP job under the pre-allocated id.
        prompt = root / ".omg" / "jobs" / ".acp-prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        if not prompt.is_file():
            prompt.write_text(
                "# internal ACP session sidecar (no prompt/tools)\n",
                encoding="utf-8",
            )
        binary = provider_binary or os.environ.get("OMG_ACP_BIN")
        try:
            start = start_job(
                root,
                provider="grok-acp-session",
                role="acp-session",
                prompt_file=prompt,
                run_id=rid,
                allow_internal=True,
                provider_timeout_s=handshake_timeout_s,
                job_id=job_id,
                request_overrides={
                    "session_id": identity["session_id"],
                    "parent_run_id": rid,
                    "cwd": identity["cwd"],
                    "session_id_hash": sid_hash,
                    "cwd_hash": cwd_hash,
                    "provider_binary": binary,
                    "timeout_s": handshake_timeout_s,
                },
            )
        except Exception as exc:
            # Delete binding ONLY when spawn never happened OR cleanup proved
            # process-group disappearance. Unproven fingerprint/probe must
            # retain the pre-allocated job-bearing binding (fail closed).
            if _acp_may_clear_binding_after_start_failure(exc):
                try:
                    if bind_path.is_file():
                        bind_path.unlink()
                except OSError:
                    pass
                raise
            detail = str(exc)
            code = getattr(exc, "code", None) or "E_JOB_LAUNCH"
            if getattr(exc, "disappearance_proven", None) is False:
                code = "E_JOB_CANCEL_UNPROVEN"
            raise JobStoreError(
                f"{detail}; ACP binding retained with job_id={job_id} "
                "(spawn cleanup disappearance unproven)",
                code=str(code),
            ) from exc
        if start.record.job_id != job_id:
            # Should be impossible with job_id=; fail closed + cancel.
            try:
                cancel_job(root, start.record.job_id, reason="acp_job_id_mismatch")
            except Exception:
                pass
            raise JobStoreError(
                f"ACP start_job id mismatch: expected {job_id}, "
                f"got {start.record.job_id}",
                code="E_ACP_BINDING",
            )
        # Promote ensuring → handshaking (same pre-allocated job_id).
        handshake_binding = {
            "run_id": rid,
            "job_id": job_id,
            "session_id_hash": sid_hash,
            "cwd_hash": cwd_hash,
            "state": "handshaking",
        }
        try:
            _write_acp_binding_for_job(root, job_id, bind_path, handshake_binding)
        except Exception as write_exc:
            cancel_err: Exception | None = None
            try:
                cancel_job(root, job_id, reason="acp_handshaking_bind_failed")
            except Exception as cancel_exc:
                cancel_err = cancel_exc
            else:
                try:
                    if bind_path.is_file():
                        bind_path.unlink()
                except OSError:
                    pass
                raise JobStoreError(
                    f"ACP handshaking binding publish failed after start_job; "
                    f"sidecar cancelled: {write_exc}",
                    code="E_ACP_BINDING",
                ) from write_exc

            # Cancel unproven — pre-spawn ensuring already carries job_id, so
            # even if recovery writes fail the binding is not reclaimable-null.
            recovery_payload = {
                "run_id": rid,
                "job_id": job_id,
                "session_id_hash": sid_hash,
                "cwd_hash": cwd_hash,
                "state": "handshaking",
                "recovery": "handshaking_publish_failed",
            }
            try:
                _write_acp_binding_for_job(root, job_id, bind_path, recovery_payload)
            except Exception:
                try:
                    _write_acp_binding_for_job(
                        root,
                        job_id,
                        bind_path,
                        {**recovery_payload, "state": "ensuring"},
                    )
                except Exception:
                    pass  # ensuring+job_id from pre-alloc still on disk
            code = getattr(cancel_err, "code", None) or "E_JOB_CANCEL_UNPROVEN"
            raise JobStoreError(
                f"ACP handshaking binding publish failed after start_job ({write_exc}); "
                f"cancel not proven: {cancel_err}; "
                f"binding retained with job_id={job_id}",
                code=str(code),
            ) from cancel_err
        try:
            validated = _wait_acp_ready(
                root,
                job_id,
                session_id_hash=sid_hash,
                cwd_hash=cwd_hash,
                parent_run_id=rid,
                timeout_s=ready_timeout_s,
            )
        except Exception as exc:
            # Compensate: cancel exact job. Unlink the provisional binding ONLY
            # after cancel_job proves disappearance — E_JOB_CANCEL_UNPROVEN must
            # retain the singleton so a later ensure cannot spawn a second sidecar.
            cancel_err: Exception | None = None
            try:
                cancel_job(root, job_id, reason="acp_ready_failed")
            except Exception as cancel_exc:
                cancel_err = cancel_exc
            else:
                try:
                    if bind_path.is_file():
                        bind_path.unlink()
                except OSError:
                    pass

            if cancel_err is not None:
                code = getattr(cancel_err, "code", None) or "E_ACP_READY_CANCEL"
                raise JobStoreError(
                    f"ACP sidecar ready failed ({exc}); cancel not proven, "
                    f"binding retained: {cancel_err}",
                    code=str(code),
                ) from cancel_err

            if isinstance(exc, JobStoreError):
                raise
            raise JobStoreError(
                f"ACP sidecar ready failed: {exc}",
                code="E_ACP_READY",
            ) from exc

        block = _team_acp_stop_block_reason(root, rid)
        if block:
            cancel_err = None
            try:
                cancel_job(root, job_id, reason="acp_aborted_team_stop")
            except Exception as cancel_exc:
                cancel_err = cancel_exc
            else:
                try:
                    if bind_path.is_file():
                        bind_path.unlink()
                except OSError:
                    pass
            if cancel_err is not None:
                raise JobStoreError(
                    f"ACP ensure aborted after ready ({block}); cancel not proven, "
                    f"binding retained: {cancel_err}",
                    code=getattr(cancel_err, "code", None) or "E_JOB_CANCEL_UNPROVEN",
                ) from cancel_err
            raise JobStoreError(
                f"ACP ensure aborted after ready: {block}",
                code="E_ACP_ENSURE_ABORTED_STOP",
            )

        _write_acp_binding_for_job(
            root,
            job_id,
            bind_path,
            {
                "run_id": rid,
                "job_id": job_id,
                "session_id_hash": sid_hash,
                "cwd_hash": cwd_hash,
                "state": "ready",
                "receipt_sha256": validated.get("receipt_sha256"),
            },
        )
        rec = read_job_record(root, job_id)
        if not _job_is_live_sidecar(root, job_id):
            try:
                cancel_job(root, job_id, reason="acp_post_ready_dead")
            except Exception:
                pass
            raise JobStoreError(
                "ACP sidecar not live after ready receipt "
                "(outer runner or inner provider process gone)",
                code="E_ACP_SIDECAR_DEAD",
            )
        return {
            "ok": True,
            "reused": False,
            "job_id": job_id,
            "attempt": int(rec.attempt),
            "receipt": validated,
            "receipt_sha256": validated.get("receipt_sha256"),
            "connection_owned": True,
            "transport": "acp_stdio_job",
            "status": "resumed",
        }


def ensure_acp_session_for_team(
    gate: Any,
    *,
    root: Path | str,
    run_id: str,
    provider_binary: str | None = None,
    _pre_spawn_hook: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Team-injected provider_resume helper (AVAILABLE gate only).

    Returns helper fields merged into provider_session_result. On missing
    session binding, sets ``force_blocked=True``. Never claims session/close.
    """
    from omg_cli.host_models import FeatureGateResult

    if not isinstance(gate, FeatureGateResult) or gate.state != "AVAILABLE":
        return {
            "invoked": False,
            "transport_wired": False,
            "force_blocked": True,
            "reason": "ACP ensure requires AVAILABLE session_resume gate",
            "ok": False,
        }
    root_path = Path(root).resolve()
    try:
        result = ensure_acp_session_sidecar(
            root_path,
            run_id=run_id,
            provider_binary=provider_binary,
            _pre_spawn_hook=_pre_spawn_hook,
        )
    except JobStoreError as exc:
        if exc.code == "E_ACP_SESSION_BINDING":
            return {
                "invoked": False,
                "transport_wired": False,
                "force_blocked": True,
                "reason": str(exc),
                "ok": False,
                "next_action": (
                    "Persist a run-level grok_session_id via load_host_session "
                    "before --provider-session"
                ),
            }
        # AVAILABLE gate retained; execution failed.
        return {
            "invoked": True,
            "transport_wired": False,
            "ok": False,
            "execution": {
                "status": "failed",
                "transport": "acp_stdio_job",
                "error": str(exc)[:400],
                "error_code": exc.code,
                "connection_owned": False,
                "no_replay": True,
                "restore_code": False,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "invoked": True,
            "transport_wired": False,
            "ok": False,
            "execution": {
                "status": "failed",
                "transport": "acp_stdio_job",
                "error": str(exc)[:400],
                "connection_owned": False,
                "no_replay": True,
                "restore_code": False,
            },
        }

    return {
        "invoked": True,
        "transport_wired": True,
        "ok": True,
        "execution": {
            "status": "resumed",
            "transport": result["transport"],
            "job_id": result["job_id"],
            "attempt": result["attempt"],
            "reused": bool(result.get("reused")),
            "receipt_sha256": result.get("receipt_sha256"),
            "connection_owned": True,
            "no_replay": True,
            "restore_code": False,
        },
    }


def cancel_linked_acp_sidecar(
    project_root: Path, run_id: str, *, reason: str = "team_stop"
) -> dict[str, Any]:
    """Cancel only the Team-linked ACP job (not session/close).

    The singleton binding file is removed **only** after ``cancel_job`` returns
    successfully (disappearance proven). ``E_JOB_CANCEL_UNPROVEN`` and other
    cancel failures retain the binding so a later ensure cannot spawn a second
    sidecar for the same ``(run_id, session, cwd)`` tuple.
    """
    root = Path(project_root).resolve()
    bind_path = _acp_binding_path(root, run_id)
    out: dict[str, Any] = {
        "attempted": False,
        "cancelled": False,
        "binding_cleared": False,
        "job_id": None,
        "session_close": False,
        "note": "sidecar cancellation (not ACP session/close)",
    }
    existing = _read_json_file(bind_path) if bind_path.is_file() else None
    if not existing or not existing.get("job_id"):
        return out
    job_id = str(existing["job_id"])
    out["attempted"] = True
    out["job_id"] = job_id
    try:
        cancel_job(root, job_id, reason=reason)
    except JobStoreError as exc:
        out["error"] = str(exc)
        out["error_code"] = getattr(exc, "code", None) or "E_JOB_STORE"
        # Retain binding — live or unproven processes must keep the singleton ref.
        return out

    out["cancelled"] = True
    try:
        if bind_path.is_file():
            bind_path.unlink()
            out["binding_cleared"] = True
    except OSError as exc:
        # Cancel proved disappearance, but binding unlink failed — still report
        # cancelled; ensure may see a stale binding to a terminal job (STALE).
        out["error"] = f"binding unlink failed after proven cancel: {exc}"
    return out


__all__ = [
    "CancelOwnership",
    "GcResult",
    "StartResult",
    "cancel_job",
    "cancel_linked_acp_sidecar",
    "collect_job",
    "ensure_acp_session_for_team",
    "ensure_acp_session_sidecar",
    "gc_jobs",
    "job_status",
    "launch_job_runner",
    "list_jobs",
    "observe_job",
    "preflight_retry_job",
    "read_acp_sidecar_binding",
    "recover_job",
    "recover_jobs",
    "resolve_acp_binding_for_team_stop",
    "resolve_acp_session_identity",
    "retry_job",
    "start_job",
    "wait_job",
]


# Re-export observation / recovery APIs (public Python surface).
from omg_cli.jobs.recovery import (  # noqa: E402
    observe_job,
    recover_job,
    recover_jobs,
)
