"""Shared PID/PGID/start-time capture and revalidation for job processes (#68 PR2).

Used for both the outer job runner and the inner provider (agy) process group.
"""

from __future__ import annotations

import enum
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from omg_cli.jobs.models import JobStoreError


class OwnershipOutcome(enum.Enum):
    """Outcome of pre-signal ownership revalidation."""

    OK = "ok"  # Safe to signal this pid/pgid.
    GONE = "gone"  # Target already exited; do not signal.


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Durable process identity used for fail-closed cancel signalling."""

    pid: int
    pgid: int
    pid_starttime: str | None = None


def probe_pid_starttime(pid: int) -> str | None:
    """Best-effort process start fingerprint.

    Linux: ``/proc/<pid>/stat`` starttime (field 22) as ``proc:<ticks>``.
    Elsewhere: ``ps -p PID -o lstart=`` as ``lstart:<text>``.
    Returns ``None`` when the probe fails — callers treat that as
    \"fingerprint unavailable\".
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


def capture_identity(pid: int, *, pgid: int | None = None) -> ProcessIdentity:
    """Capture pid/pgid/starttime for a live process (best-effort fingerprint)."""
    if pid <= 0:
        raise JobStoreError(
            f"refuse to capture identity for pid={pid}",
            code="E_JOB_PID_REUSED",
        )
    if pgid is None:
        try:
            pgid = int(os.getpgid(pid))
        except ProcessLookupError as exc:
            raise JobStoreError(
                f"pid {pid} gone while capturing identity",
                code="E_JOB_PID_REUSED",
            ) from exc
        except OSError as exc:
            raise JobStoreError(
                f"cannot read pgid for pid={pid}: {exc}",
                code="E_JOB_PGID_MISMATCH",
            ) from exc
    return ProcessIdentity(
        pid=int(pid),
        pgid=int(pgid),
        pid_starttime=probe_pid_starttime(pid),
    )


def pid_alive(pid: int) -> bool:
    """True when *pid* exists and is not a zombie.

    After ``os.kill(pid, 0)`` proves the pid exists, *ps* probe errors
    (including ``TimeoutExpired``) fail **open** as alive — never treat a
    live process as dead because the STAT probe hung.
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
        return True
    if not out:
        return False
    return not out.upper().startswith("Z")


def assert_ownership(
    identity: ProcessIdentity,
    *,
    job_id: str,
    label: str = "process",
) -> OwnershipOutcome:
    """Revalidate ownership before a cancel signal.

    Returns:
        ``OwnershipOutcome.OK`` — safe to signal
        ``OwnershipOutcome.GONE`` — process already gone; do **not** signal

    Raises:
        ``JobStoreError`` (``E_JOB_PID_REUSED`` / ``E_JOB_PGID_MISMATCH`` /
        ``E_JOB_CANCEL_UNPROVEN``) on mismatch — fail-closed, no signal.
    """
    target_pid = int(identity.pid)
    target_pgid = int(identity.pgid)
    if target_pid <= 1 or target_pgid <= 1:
        raise JobStoreError(
            f"job {job_id} refuses to signal {label} "
            f"pid={target_pid} pgid={target_pgid} (both must be > 1)",
            code="E_JOB_PID_REUSED",
        )
    if not pid_alive(target_pid):
        return OwnershipOutcome.GONE
    try:
        live_pgid = int(os.getpgid(target_pid))
    except ProcessLookupError:
        return OwnershipOutcome.GONE
    except OSError as exc:
        raise JobStoreError(
            f"job {job_id} cannot read live pgid for {label} pid={target_pid}: {exc}",
            code="E_JOB_PGID_MISMATCH",
        ) from exc
    if live_pgid != target_pgid:
        raise JobStoreError(
            f"job {job_id} live pgid mismatch for {label} pid={target_pid} "
            f"(recorded={target_pgid} live={live_pgid}); refusing to signal",
            code="E_JOB_PGID_MISMATCH",
        )
    expected = identity.pid_starttime
    if expected is None or expected == "":
        return OwnershipOutcome.OK
    live = probe_pid_starttime(target_pid)
    if live is None or live != expected:
        raise JobStoreError(
            f"job {job_id} {label} pid {target_pid} ownership fingerprint mismatch "
            f"(recorded={expected!r} live={live!r}); refusing to signal "
            "(possible PID reuse)",
            code="E_JOB_PID_REUSED",
        )
    return OwnershipOutcome.OK


def kill_pgid(pgid: int, signum: int) -> bool:
    """Send signal to process group only (never by name). Returns True if sent."""
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, signum)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def reap_child(pid: int) -> None:
    """Best-effort waitpid when we are still the parent (avoids test zombies)."""
    if pid <= 0:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError:
        pass


def wait_until_gone(pid: int, *, timeout_s: float = 2.0, poll_s: float = 0.05) -> bool:
    """Poll until *pid* is gone (or zombie). Returns True if disappeared."""
    import time

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        reap_child(pid)
        if not pid_alive(pid):
            return True
        time.sleep(max(0.01, float(poll_s)))
    reap_child(pid)
    return not pid_alive(pid)


__all__ = [
    "OwnershipOutcome",
    "ProcessIdentity",
    "assert_ownership",
    "capture_identity",
    "kill_pgid",
    "pid_alive",
    "probe_pid_starttime",
    "reap_child",
    "wait_until_gone",
]
