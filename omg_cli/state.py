# omg_cli/state.py
"""Authoritative run-state single-writer for oh-my-grok.

Only the omg CLI (this module) may mutate status / passes / verified under
``.omg/state/runs/<run_id>/``. Hooks and agents may only append events or write
proposals under ``.omg/artifacts/``.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX
    fcntl = None  # type: ignore[assignment]


OMG_SUBDIRS = (
    "state",
    "state/runs",
    "plans",
    "research",
    "handoffs",
    "artifacts",
    "ultragoal",
)


def ensure_omg_dirs(root: Path) -> Path:
    root = Path(root)
    for sub in OMG_SUBDIRS:
        (root / ".omg" / sub).mkdir(parents=True, exist_ok=True)
    return root


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_dir(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "runs"


def _active_path(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "active.json"


def _create_lock_path(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "create.lock"


def _status_path(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id / "status.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via temp file + os.replace (atomic on same filesystem)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _make_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


# Statuses that block a second concurrent create_run (active mutex).
ACTIVE_NON_TERMINAL_STATUSES = frozenset({"initialized", "running", "verifying"})
TERMINAL_STATUSES = frozenset(
    {"cancelled", "completed", "failed", "verified"}
)


def _pid_alive(pid: int) -> bool | None:
    """Check whether *pid* exists (signal 0).

    Returns True if alive (or PermissionError — process exists), False if
    ESRCH / ProcessLookupError (gone), None if other OSError.
    """
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _run_pid_path(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id / "pid"


def _run_pid_json_path(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id / "pid.json"


def process_starttime(pid: int) -> str | None:
    """Best-effort process start time string (macOS/Linux ``ps -o lstart=``)."""
    if pid <= 0:
        return None
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    text = (r.stdout or "").strip()
    return text or None


def write_pid_metadata(
    path: Path,
    *,
    pid: int,
    pgid: int | None = None,
    starttime: str | None = None,
) -> dict[str, Any]:
    """Write ``pid.json`` (and legacy plain ``pid`` file when path is run-dir pid.json).

    Shape: ``{pid, starttime, pgid}``. Used by leader launch and workers/*.pid.json.
    """
    path = Path(path)
    if starttime is None:
        starttime = process_starttime(pid)
    if pgid is None and os.name == "posix":
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = pid
    meta: dict[str, Any] = {
        "pid": int(pid),
        "starttime": starttime,
        "pgid": int(pgid) if pgid is not None else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Legacy plain pid sibling for older cancel readers
    if path.name == "pid.json":
        plain = path.with_name("pid")
        plain.write_text(f"{int(pid)}\n", encoding="utf-8")
    return meta


def read_pid_metadata(root: Path, run_id: str) -> dict[str, Any] | None:
    """Load pid.json if present; else fall back to plain pid file."""
    root = Path(root)
    jpath = _run_pid_json_path(root, run_id)
    if jpath.is_file():
        data = _read_json(jpath)
        if data and isinstance(data.get("pid"), int):
            return data
        # tolerate pid as string
        if data and data.get("pid") is not None:
            try:
                data = dict(data)
                data["pid"] = int(data["pid"])
                return data
            except (TypeError, ValueError):
                pass
    pid_path = _run_pid_path(root, run_id)
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return {"pid": pid, "starttime": None, "pgid": None}
    return None


def pid_matches_recorded(
    pid: int,
    recorded_starttime: str | None,
) -> bool:
    """True when it is safe to signal *pid* for this recorded starttime.

    * No recorded starttime (legacy plain ``pid`` file) → always True so
      cancel remains best-effort (matches pre-starttime behavior).
    * With starttime: require alive + matching ``ps -o lstart=`` (or proceed
      if ``ps`` unavailable). Mismatch ⇒ PID reuse ⇒ do not kill.
    """
    if pid <= 0:
        return False
    if not recorded_starttime:
        return True
    alive = _pid_alive(pid)
    if alive is False:
        return False
    current = process_starttime(pid)
    if current is None:
        # ps failed — proceed with kill (best-effort)
        return True
    return current == recorded_starttime


def is_stale_run(root: Path, run_id: str) -> bool:
    """True when a pid file exists and the process is gone (ESRCH).

    No pid file, unreadable pid, or indeterminate liveness → not stale
    (mutex still applies unless force supersede). When starttime is recorded
    and no longer matches (PID reuse), treat as stale so create can proceed.
    """
    meta = read_pid_metadata(Path(root), run_id)
    if meta is None:
        return False
    pid = int(meta["pid"])
    recorded = meta.get("starttime")
    recorded_s = recorded if isinstance(recorded, str) and recorded else None
    if recorded_s and not pid_matches_recorded(pid, recorded_s):
        # Dead or PID reused under a recorded starttime → reclaimable
        return True
    return _pid_alive(pid) is False


def _create_run_unlocked(
    root: Path,
    *,
    mode: str,
    goal: str,
    extra: dict[str, Any] | None = None,
    force: bool = False,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Inner create_run body (caller holds create.lock when available)."""
    active = load_active_run(root)
    if active is not None:
        st = str(active.get("status") or "")
        if st in ACTIVE_NON_TERMINAL_STATUSES:
            old_id = str(active.get("run_id") or "")
            stale = bool(old_id) and is_stale_run(root, old_id)
            if not force and not stale:
                raise RuntimeError(
                    "active run already exists: "
                    f"run_id={active.get('run_id')!r} status={st!r}; "
                    "cancel it first or pass force=True"
                )
            # Supersede (force) or reclaim stale: cancel/kill old active run.
            if old_id:
                try:
                    cancel_run(
                        root,
                        old_id,
                        kill_grace_s=kill_grace_s if force else 0.0,
                    )
                except FileNotFoundError:
                    # Race: status vanished; clear pointer and continue.
                    clear_active(root, old_id)

    run_id = _make_run_id()
    now = _utc_now()
    status: dict[str, Any] = {
        "run_id": run_id,
        "mode": mode,
        "goal": goal,
        "status": "initialized",
        "verified": False,
        "passes": 0,
        "created_at": now,
        "updated_at": now,
    }
    if extra:
        # Never allow callers to smuggle verified=true on create
        safe_extra = {k: v for k, v in extra.items() if k != "verified"}
        status.update(safe_extra)
        status["verified"] = False
        status["run_id"] = run_id
        status["status"] = "initialized"

    run_dir = _runs_dir(root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "workers").mkdir(exist_ok=True)
    _atomic_write_json(_status_path(root, run_id), status)
    _atomic_write_json(_active_path(root), {"run_id": run_id, "updated_at": now})
    return status


def create_run(
    root: Path,
    *,
    mode: str,
    goal: str,
    extra: dict[str, Any] | None = None,
    force: bool = False,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Create a new run directory + status.json and point active.json at it.

    Serializes concurrent creates via ``fcntl.flock`` on
    ``.omg/state/create.lock`` (POSIX). Refuses when an active run exists with
    status in ``{initialized, running, verifying}`` unless:

    * ``force=True`` — **supersede**: cancel/kill the old active run first
      (best-effort process kill via pid file, then mark cancelled), or
    * the active run is **stale** (pid file present and process ESRCH) — the
      dead run is cancelled and create proceeds without force.

    Terminal statuses (cancelled/completed/failed/verified) do not block.
    """
    root = Path(root)
    ensure_omg_dirs(root)

    lock_path = _create_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:
        return _create_run_unlocked(
            root,
            mode=mode,
            goal=goal,
            extra=extra,
            force=force,
            kill_grace_s=kill_grace_s,
        )

    with lock_path.open("a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            return _create_run_unlocked(
                root,
                mode=mode,
                goal=goal,
                extra=extra,
                force=force,
                kill_grace_s=kill_grace_s,
            )
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


# Keys that extra must never override; status param / identity fields win.
_WRITE_STATUS_RESERVED = frozenset({"status", "run_id", "verified", "created_at"})


def write_status(
    root: Path,
    run_id: str,
    status: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update status field (CLI-only path). Does not set verified=true."""
    root = Path(root)
    path = _status_path(root, run_id)
    current = _read_json(path)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    preserved_run_id = current.get("run_id", run_id)
    preserved_created_at = current.get("created_at")
    preserved_verified = current.get("verified", False)

    if extra:
        for k, v in extra.items():
            if k in _WRITE_STATUS_RESERVED:
                continue  # use set_verified for verified; identity/status are protected
            current[k] = v

    # Parameter and reserved fields always win over extra
    current["status"] = status
    current["run_id"] = preserved_run_id
    if preserved_created_at is not None:
        current["created_at"] = preserved_created_at
    # Never allow extra (or residual state) to set verified=true via this path
    current["verified"] = preserved_verified
    if current.get("verified") is True and not _has_acceptance_artifact(root, run_id):
        current["verified"] = False
    current["updated_at"] = _utc_now()
    _atomic_write_json(path, current)
    return current


def load_run(root: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(_status_path(root, run_id))


def load_active_run(root: Path) -> dict[str, Any] | None:
    """Load active pointer + corresponding status.json. None if missing."""
    root = Path(root)
    active = _read_json(_active_path(root))
    if not active:
        return None
    run_id = active.get("run_id")
    if not run_id or not isinstance(run_id, str):
        return None
    status = load_run(root, run_id)
    return status


def clear_active(root: Path, run_id: str | None = None) -> None:
    """Clear active pointer if it matches run_id (or always if run_id is None)."""
    root = Path(root)
    path = _active_path(root)
    if not path.exists():
        return
    if run_id is not None:
        active = _read_json(path)
        if not active or active.get("run_id") != run_id:
            return
    try:
        path.unlink()
    except OSError:
        # overwrite with empty marker if unlink fails
        _atomic_write_json(path, {"run_id": None, "updated_at": _utc_now()})


def _kill_run_process_group(pid: int, *, grace_s: float = 0.0) -> list[str]:
    """Best-effort kill of process group then process. Returns actions taken.

    Prefer ``os.killpg`` when the pid is a session leader (``start_new_session``).
    Ignores ESRCH / ProcessLookupError. Optional grace then SIGKILL.
    """
    actions: list[str] = []
    # Prefer process-group signal when possible (POSIX session leader)
    try:
        os.killpg(pid, signal.SIGTERM)
        actions.append("killpg:SIGTERM")
    except (ProcessLookupError, PermissionError, OSError):
        # Fall back to single-pid SIGTERM
        try:
            os.kill(pid, signal.SIGTERM)
            actions.append("kill:SIGTERM")
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if grace_s and grace_s > 0:
        import time

        time.sleep(grace_s)
        # Escalate to SIGKILL on group, then pid
        try:
            os.killpg(pid, signal.SIGKILL)
            actions.append("killpg:SIGKILL")
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
                actions.append("kill:SIGKILL")
            except (ProcessLookupError, PermissionError, OSError):
                pass
    return actions


def _kill_from_pid_meta(
    meta: dict[str, Any],
    *,
    grace_s: float = 0.0,
    label: str = "leader",
) -> list[str]:
    """Kill process for a pid.json meta dict if starttime still matches."""
    actions: list[str] = []
    try:
        pid = int(meta["pid"])
    except (KeyError, TypeError, ValueError):
        return actions
    if pid <= 0:
        return actions

    recorded = meta.get("starttime")
    recorded_s = recorded if isinstance(recorded, str) and recorded else None
    if not pid_matches_recorded(pid, recorded_s):
        actions.append(f"skip:{label}:pid_reuse_or_dead:{pid}")
        return actions

    # Prefer recorded pgid when present (session leader from start_new_session)
    kill_target = pid
    pgid = meta.get("pgid")
    if isinstance(pgid, int) and pgid > 0:
        kill_target = pgid

    sub = _kill_run_process_group(kill_target, grace_s=grace_s)
    if not sub and kill_target != pid:
        sub = _kill_run_process_group(pid, grace_s=grace_s)
    for a in sub:
        actions.append(f"{label}:{a}")
    if not sub:
        actions.append(f"{label}:no_signal_delivered:{pid}")
    return actions


def cancel_run(
    root: Path,
    run_id: str | None = None,
    *,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Mark run cancelled and clear active if it matches. Does not delete artifacts.

    Best-effort: if ``pid.json`` (or legacy ``pid``) exists under the run dir,
    verify starttime still matches (PID reuse guard), then send SIGTERM to the
    process group (``killpg``) when possible, else the single pid. Also scans
    ``workers/*.pid.json`` for multi-PID cancel skeleton. Ignore
    ProcessLookupError / ESRCH / permission errors. Optional grace then SIGKILL.
    """
    root = Path(root)
    if run_id is None:
        active = load_active_run(root)
        if active is None:
            raise FileNotFoundError("no active run to cancel")
        run_id = active["run_id"]
    current = load_run(root, run_id)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")

    kill_actions: list[str] = []

    # Leader process (pid.json preferred; plain pid fallback)
    meta = read_pid_metadata(root, run_id)
    if meta is not None:
        kill_actions.extend(
            _kill_from_pid_meta(meta, grace_s=kill_grace_s, label="leader")
        )

    # Multi-PID cancel skeleton: workers/*.pid.json
    workers_dir = _runs_dir(root) / run_id / "workers"
    if workers_dir.is_dir():
        for wpath in sorted(workers_dir.glob("*.pid.json")):
            wmeta = _read_json(wpath)
            if not wmeta:
                continue
            # Normalize pid type
            if "pid" in wmeta and not isinstance(wmeta["pid"], int):
                try:
                    wmeta = dict(wmeta)
                    wmeta["pid"] = int(wmeta["pid"])
                except (TypeError, ValueError):
                    continue
            kill_actions.extend(
                _kill_from_pid_meta(
                    wmeta,
                    grace_s=kill_grace_s,
                    label=f"worker:{wpath.stem}",
                )
            )

    current["status"] = "cancelled"
    current["verified"] = False
    current["updated_at"] = _utc_now()
    current["cancelled_at"] = current["updated_at"]
    if kill_actions:
        current["kill_actions"] = kill_actions
    _atomic_write_json(_status_path(root, run_id), current)
    clear_active(root, run_id)
    return current


def _has_acceptance_artifact(root: Path, run_id: str) -> bool:
    """True only for process-trusted CLI acceptance (disk stamp + in-process token).

    Agent-forged ``{passed: true}`` — even with ``writer=omg-cli`` and a
    matching manifest sha — is rejected unless ``run_acceptance`` registered a
    process-local token in this process.
    """
    from omg_cli.acceptance import is_trusted_acceptance

    return is_trusted_acceptance(Path(root), run_id)


def set_verified(root: Path, run_id: str, *, force: bool = False) -> dict[str, Any]:
    """Mark verified only when trusted CLI acceptance exists (unless force=True).

    Requires ``acceptance.result.json`` with ``writer=="omg-cli"``, ``passed``
    true, matching frozen manifest sha, **and** a process-local token from
    ``run_acceptance`` in this process. Disk-only forgeries are rejected.
    force is intentionally not exposed by the CLI router.
    """
    root = Path(root)
    current = load_run(root, run_id)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    if not force and not _has_acceptance_artifact(root, run_id):
        raise PermissionError(
            "refusing to set verified=true without trusted CLI acceptance "
            f"(writer=omg-cli, passed=true, matching manifest sha, "
            f"in-process run_acceptance token) for run_id={run_id!r}"
        )
    current["verified"] = True
    current["status"] = "verified"
    current["updated_at"] = _utc_now()
    current["verified_at"] = current["updated_at"]
    _atomic_write_json(_status_path(root, run_id), current)
    return current
