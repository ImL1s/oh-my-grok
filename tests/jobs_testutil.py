"""Scoped job-test cleanup — exact spawn identities only (never job.json / pgrep)."""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator

import pytest

# Exact (pid, pgid) identities captured from start_job's returned record only.
_SPAWNED: set[tuple[int, int]] = set()


def register_spawned_job(*, pid: int, pgid: int | None = None) -> None:
    """Record an exact runner identity for teardown kill only."""
    if pid <= 0:
        return
    target_pgid = int(pgid) if pgid is not None and int(pgid) > 0 else int(pid)
    _SPAWNED.add((int(pid), target_pgid))


def registered_pids() -> set[int]:
    return {pid for pid, _pgid in _SPAWNED}


def kill_registered_jobs() -> None:
    """SIGKILL only spawn-wrap identities — never job.json PIDs or name match."""
    handles = set(_SPAWNED)
    _SPAWNED.clear()
    me = os.getpid()
    for pid, pgid in handles:
        if pid <= 1 or pgid <= 1 or pid == me or pgid == me:
            continue
        try:
            if pgid > 0:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


def _scrub_job_env() -> None:
    for key in list(os.environ):
        if key.startswith("OMG_JOB_") or key == "OMG_PROJECT_ROOT":
            os.environ.pop(key, None)
    try:
        from omg_cli.project_root import clear_resolved_project_root

        clear_resolved_project_root()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _jobs_test_env_isolation(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Track exact start_job pids; teardown kills only those (no job.json / pgrep)."""
    _SPAWNED.clear()

    import omg_cli.commands.job as job_cmd
    import omg_cli.jobs.runtime as runtime_mod

    real_start = runtime_mod.start_job

    def _tracking_start(*args: object, **kwargs: object):  # noqa: ANN001
        result = real_start(*args, **kwargs)
        rec = getattr(result, "record", None)
        pid = getattr(rec, "pid", None) if rec is not None else None
        pgid = getattr(rec, "pgid", None) if rec is not None else None
        if pid is not None:
            register_spawned_job(
                pid=int(pid),
                pgid=int(pgid) if pgid is not None else None,
            )
        return result

    monkeypatch.setattr(runtime_mod, "start_job", _tracking_start)
    monkeypatch.setattr(job_cmd, "start_job", _tracking_start)

    real_launch = runtime_mod.launch_job_runner

    def _tracking_launch(*args: object, **kwargs: object):  # noqa: ANN001
        result = real_launch(*args, **kwargs)
        rec = getattr(result, "record", None)
        pid = getattr(rec, "pid", None) if rec is not None else None
        pgid = getattr(rec, "pgid", None) if rec is not None else None
        if pid is not None:
            register_spawned_job(
                pid=int(pid),
                pgid=int(pgid) if pgid is not None else None,
            )
        return result

    monkeypatch.setattr(runtime_mod, "launch_job_runner", _tracking_launch)

    # Also track retry_job which uses launch_job_runner after prepare.
    if hasattr(job_cmd, "retry_job"):
        real_retry = runtime_mod.retry_job

        def _tracking_retry(*args: object, **kwargs: object):  # noqa: ANN001
            return real_retry(*args, **kwargs)

        monkeypatch.setattr(runtime_mod, "retry_job", _tracking_retry)
        monkeypatch.setattr(job_cmd, "retry_job", _tracking_retry)

    import sys

    for mod_name in ("tests.test_jobs_runtime", "test_jobs_runtime"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "start_job"):
            monkeypatch.setattr(mod, "start_job", _tracking_start)
        if mod is not None and hasattr(mod, "launch_job_runner"):
            monkeypatch.setattr(mod, "launch_job_runner", _tracking_launch)

    yield
    kill_registered_jobs()
    _scrub_job_env()
