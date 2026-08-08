"""Scoped job-test cleanup — exact pid/pgid only (never pgrep-by-name)."""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest

# Exact (pid, pgid) identities spawned by the current test via start_job wrap.
_SPAWNED: set[tuple[int, int]] = set()
# Project roots whose durable job.json records may hold leftover handles.
_PROJECT_ROOTS: set[Path] = set()


def register_spawned_job(*, pid: int, pgid: int | None = None) -> None:
    """Record an exact runner identity for teardown kill only."""
    if pid <= 0:
        return
    target_pgid = int(pgid) if pgid is not None and int(pgid) > 0 else int(pid)
    _SPAWNED.add((int(pid), target_pgid))


def register_project_root(root: Path | str) -> None:
    """Remember a tmp project root so teardown can read job.json handles."""
    _PROJECT_ROOTS.add(Path(root).resolve())


def registered_pids() -> set[int]:
    return {pid for pid, _pgid in _SPAWNED}


def _handles_from_job_records() -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    try:
        from omg_cli.jobs.runtime import list_jobs
    except Exception:
        return found
    for root in list(_PROJECT_ROOTS):
        try:
            for row in list_jobs(root):
                pid = row.get("pid")
                if pid is None:
                    continue
                try:
                    ipid = int(pid)
                except (TypeError, ValueError):
                    continue
                if ipid <= 0:
                    continue
                pgid = row.get("pgid")
                try:
                    ipgid = int(pgid) if pgid is not None else ipid
                except (TypeError, ValueError):
                    ipgid = ipid
                found.add((ipid, ipgid if ipgid > 0 else ipid))
        except Exception:
            continue
    return found


def kill_registered_jobs() -> None:
    """SIGKILL only registered / job-record identities (never process-name match)."""
    handles = set(_SPAWNED) | _handles_from_job_records()
    _SPAWNED.clear()
    _PROJECT_ROOTS.clear()
    me = os.getpid()
    for pid, pgid in handles:
        if pid == me or pgid == me:
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
    """Track exact start_job pids; teardown kills only those (no pgrep)."""
    _SPAWNED.clear()
    _PROJECT_ROOTS.clear()

    import omg_cli.commands.job as job_cmd
    import omg_cli.jobs.runtime as runtime_mod

    real_start = runtime_mod.start_job

    def _tracking_start(*args: object, **kwargs: object):  # noqa: ANN001
        result = real_start(*args, **kwargs)
        try:
            root = Path(args[0] if args else kwargs["project_root"])  # type: ignore[index]
            register_project_root(root)
        except Exception:
            pass
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
    # Re-bind names imported into the calling test modules (if loaded).
    import sys

    for mod_name in ("tests.test_jobs_runtime", "test_jobs_runtime"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "start_job"):
            monkeypatch.setattr(mod, "start_job", _tracking_start)

    yield
    kill_registered_jobs()
    _scrub_job_env()
