"""Shared PID/PGID/start-time capture and revalidation for job processes (#68 PR2).

Used for both the outer job runner and the inner provider (agy) process group.
"""

from __future__ import annotations

import enum
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omg_cli.jobs.models import JobStoreError


class OwnershipOutcome(enum.Enum):
    """Outcome of pre-signal ownership revalidation."""

    OK = "ok"  # Safe to signal this pid/pgid.
    GONE = "gone"  # Target already exited; do not signal.


class IdentityProbeOutcome(enum.Enum):
    """Tri-state (+ reused) identity probe for GC — never conflate probe failure with reuse.

    ``UNPROVEN`` — fingerprint/pgid probe unavailable; do **not** treat as gone.
    ``REUSED`` — non-null fingerprint mismatch or live pgid mismatch (verified).
    ``LIVE`` — recorded identity still matches the live process.
    ``GONE`` — process lookup failure / not alive.
    """

    LIVE = "live"
    GONE = "gone"
    REUSED = "reused"
    UNPROVEN = "unproven"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Durable process identity used for fail-closed cancel signalling."""

    pid: int
    pgid: int
    pid_starttime: str | None = None


def is_json_int(value: Any) -> bool:
    """True when *value* is a JSON integer (Python ``int``, not ``bool``/float)."""
    return isinstance(value, int) and not isinstance(value, bool)


def process_identity_id_ok(value: Any) -> bool:
    """PID/PGID is a strict JSON integer (bool/float rejected; range checked later)."""
    return is_json_int(value)


def process_identity_id_in_range(value: Any) -> bool:
    """Strict JSON integer in the process-identity range (> 1)."""
    return is_json_int(value) and int(value) > 1


def process_fingerprint_ok(value: Any) -> bool:
    """``pid_starttime`` / fingerprints: null or string only (never coerce)."""
    return value is None or isinstance(value, str)


def parse_process_identity(
    *,
    pid: Any,
    pgid: Any,
    pid_starttime: Any = None,
) -> ProcessIdentity | None:
    """Build a :class:`ProcessIdentity` only from type-strict fields.

    Rejects bool/float/string PIDs and non-string fingerprints. Does **not**
    coerce via ``int()`` / ``str()`` (which would false-green GONE/REUSED).
    Out-of-range IDs (<= 1) still construct so ``assert_ownership`` /
    probes can fail closed with their existing codes.
    """
    if not process_fingerprint_ok(pid_starttime):
        return None
    if pid is None or pgid is None:
        return None
    if not process_identity_id_ok(pid) or not process_identity_id_ok(pgid):
        return None
    return ProcessIdentity(
        pid=int(pid),
        pgid=int(pgid),
        pid_starttime=pid_starttime if isinstance(pid_starttime, str) else None,
    )


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


def probe_identity_liveness(identity: ProcessIdentity) -> IdentityProbeOutcome:
    """Classify live identity for GC without treating probe failure as reuse.

    Unlike ``assert_ownership`` (cancel fail-closed: raise on mismatch *or*
    unavailable fingerprint), this distinguishes:

    - fingerprint unavailable / getpgid OSError → ``UNPROVEN``
    - non-null fingerprint mismatch / pgid mismatch → ``REUSED``
    - exact match (or no expected fingerprint) → ``LIVE``
    - process lookup failure / not alive → ``GONE``
    """
    target_pid = int(identity.pid)
    target_pgid = int(identity.pgid)
    if target_pid <= 1 or target_pgid <= 1:
        # Refuse to treat init/invalid targets as reclaimable for GC.
        return IdentityProbeOutcome.UNPROVEN
    if not pid_alive(target_pid):
        return IdentityProbeOutcome.GONE
    try:
        live_pgid = int(os.getpgid(target_pid))
    except ProcessLookupError:
        return IdentityProbeOutcome.GONE
    except OSError:
        # Probe unavailable — not proof of reuse.
        return IdentityProbeOutcome.UNPROVEN
    if live_pgid != target_pgid:
        return IdentityProbeOutcome.REUSED
    expected = identity.pid_starttime
    if expected is None or expected == "":
        return IdentityProbeOutcome.LIVE
    live = probe_pid_starttime(target_pid)
    if live is None:
        return IdentityProbeOutcome.UNPROVEN
    if live != expected:
        return IdentityProbeOutcome.REUSED
    return IdentityProbeOutcome.LIVE


def probe_identity_for_recovery(identity: ProcessIdentity) -> IdentityProbeOutcome:
    """Recovery-strict identity probe.

    Same as :func:`probe_identity_liveness`, except a live process with a
    missing expected start fingerprint is ``UNPROVEN`` — PID alone never
    proves ownership after lease expiry.
    """
    expected = identity.pid_starttime
    if expected is None or expected == "":
        target_pid = int(identity.pid)
        target_pgid = int(identity.pgid)
        if target_pid <= 1 or target_pgid <= 1:
            return IdentityProbeOutcome.UNPROVEN
        if not pid_alive(target_pid):
            return IdentityProbeOutcome.GONE
        # Live without fingerprint — cannot prove this is the historical owner.
        return IdentityProbeOutcome.UNPROVEN
    return probe_identity_liveness(identity)


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


def _direct_child_pids(parent_pid: int) -> list[int]:
    """Best-effort direct children of *parent_pid* (Linux ``/proc`` then ``ps``)."""
    if parent_pid <= 1:
        return []
    out: list[int] = []
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            for entry in proc_root.iterdir():
                if not entry.name.isdigit():
                    continue
                child = int(entry.name)
                if child <= 1 or child == parent_pid:
                    continue
                try:
                    status = (entry / "status").read_text(encoding="utf-8")
                except OSError:
                    continue
                for line in status.splitlines():
                    if line.startswith("PPid:"):
                        try:
                            ppid = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            break
                        if ppid == parent_pid:
                            out.append(child)
                        break
        except OSError:
            out = []
        if out:
            return out
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return out
    if result.returncode != 0:
        return out
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid_i = int(parts[0])
            ppid_i = int(parts[1])
        except ValueError:
            continue
        if ppid_i == parent_pid and pid_i > 1 and pid_i != parent_pid:
            out.append(pid_i)
    return out


def child_identities(parent_pid: int) -> tuple[ProcessIdentity, ...]:
    """Capture identities of *parent_pid*'s direct children via OS ppid.

    This is independent of ``job.json``: a hostile provider can forge the
    durable record, but it cannot fake its parent pid. Used to observe the
    inner grok/agy process while the job runner is still live.
    """
    found: list[ProcessIdentity] = []
    seen: set[int] = set()
    for pid in _direct_child_pids(parent_pid):
        if pid in seen:
            continue
        seen.add(pid)
        try:
            found.append(capture_identity(pid))
        except JobStoreError:
            continue
    return tuple(found)


def _pids_in_pgid(pgid: int) -> list[int]:
    """Best-effort live PIDs that currently share *pgid*."""
    if pgid <= 1:
        return []
    out: list[int] = []
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid_i = int(parts[0])
            pgid_i = int(parts[1])
        except ValueError:
            continue
        if pgid_i == pgid and pid_i > 1:
            out.append(pid_i)
    return out


def refresh_identity(ident: ProcessIdentity) -> ProcessIdentity | None:
    """Re-read pgid when *ident*'s start-time fingerprint still matches.

    Used after ``setsid()``: the process is the same occupant, so disappearance
    proof must track the new session rather than classify PGID mismatch as
    reuse.
    """
    if ident.pid <= 1:
        return None
    if not pid_alive(ident.pid):
        return None
    live_start = probe_pid_starttime(ident.pid)
    expected = ident.pid_starttime
    if not isinstance(expected, str) or expected == "":
        return None
    if live_start is None or live_start != expected:
        return None
    try:
        live_pgid = int(os.getpgid(ident.pid))
    except (ProcessLookupError, OSError):
        return None
    if live_pgid <= 1:
        return None
    return ProcessIdentity(
        pid=ident.pid,
        pgid=live_pgid,
        pid_starttime=live_start if isinstance(live_start, str) else expected,
    )


def same_occupant(expected: ProcessIdentity, observed: ProcessIdentity) -> bool:
    """True when *observed* is still the process recorded in *expected*.

    A later occupant reusing the PID has a different start-time fingerprint.
    Missing expected fingerprint is treated as the same occupant so we do not
    signal an unproven replacement.
    """
    if expected.pid != observed.pid:
        return False
    stamp = expected.pid_starttime
    if not isinstance(stamp, str) or stamp == "":
        return True
    return observed.pid_starttime == stamp


def merge_identity(
    found: dict[int, ProcessIdentity], ident: ProcessIdentity
) -> bool:
    """Insert *ident*, or refresh PGID when the start-time fingerprint matches.

    A descendant that later calls ``setsid()`` keeps pid+starttime and gets a
    new pgid. Treating that as ``REUSED`` would drop a still-live process.
    A start-time mismatch is a different occupant and is not overwritten.
    """
    existing = found.get(ident.pid)
    if existing is None:
        found[ident.pid] = ident
        return True
    if (
        existing.pgid == ident.pgid
        and existing.pid_starttime == ident.pid_starttime
    ):
        return False
    expected = existing.pid_starttime
    incoming = ident.pid_starttime
    if (
        isinstance(expected, str)
        and expected != ""
        and incoming == expected
        and existing.pgid != ident.pgid
    ):
        found[ident.pid] = ident
        return True
    return False


def become_child_subreaper() -> bool:
    """Linux: inherit orphaned grandchildren after an inner provider ``setsid``.

    Returns True when the kernel accepted ``PR_SET_CHILD_SUBREAPER``. macOS has
    no equivalent; callers still snapshot live children/pgid members and fail
    closed when an inner identity is never captured.
    """
    if sys.platform != "linux":
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        # linux/prctl.h PR_SET_CHILD_SUBREAPER = 36
        rc = int(libc.prctl(36, 1, 0, 0, 0))
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return rc == 0


def pgid_member_identities(pgid: int) -> tuple[ProcessIdentity, ...]:
    """Capture identities of every live process in *pgid*.

    Inner grok may fork a background child in the same session, close stdio,
    and exit. Proving only the leader PID is gone is not process-group exit.
    Snapshot members while the leader is still live; do not rescan a pgid
    after the leader dies (the id can be reused).
    """
    if pgid <= 1:
        return ()
    found: list[ProcessIdentity] = []
    seen: set[int] = set()
    for pid in _pids_in_pgid(pgid):
        if pid in seen:
            continue
        seen.add(pid)
        try:
            ident = capture_identity(pid)
        except JobStoreError:
            continue
        if ident.pgid != pgid:
            continue
        found.append(ident)
    return tuple(found)


__all__ = [
    "IdentityProbeOutcome",
    "OwnershipOutcome",
    "ProcessIdentity",
    "assert_ownership",
    "become_child_subreaper",
    "capture_identity",
    "child_identities",
    "is_json_int",
    "pgid_member_identities",
    "kill_pgid",
    "merge_identity",
    "parse_process_identity",
    "pid_alive",
    "probe_identity_for_recovery",
    "probe_identity_liveness",
    "probe_pid_starttime",
    "refresh_identity",
    "same_occupant",
    "process_fingerprint_ok",
    "process_identity_id_in_range",
    "process_identity_id_ok",
    "reap_child",
    "wait_until_gone",
]
