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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def is_stale_run(root: Path, run_id: str) -> bool:
    """True when a pid file exists and the process is gone (ESRCH).

    No pid file, unreadable pid, or indeterminate liveness → not stale
    (mutex still applies unless force supersede).
    """
    pid_path = _run_pid_path(Path(root), run_id)
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid) is False


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

    Refuses when an active run exists with status in
    ``{initialized, running, verifying}`` unless:

    * ``force=True`` — **supersede**: cancel/kill the old active run first
      (best-effort process kill via pid file, then mark cancelled), or
    * the active run is **stale** (pid file present and process ESRCH) — the
      dead run is cancelled and create proceeds without force.

    Terminal statuses (cancelled/completed/failed/verified) do not block.
    """
    root = Path(root)
    ensure_omg_dirs(root)

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


def cancel_run(
    root: Path,
    run_id: str | None = None,
    *,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Mark run cancelled and clear active if it matches. Does not delete artifacts.

    Best-effort: if a pid file exists under the run dir, send SIGTERM to the
    process group (``killpg``) when possible, else the single pid. Ignore
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

    # Best-effort process-group kill via pid file (never self-matching pkill)
    pid_path = _runs_dir(root) / run_id / "pid"
    kill_actions: list[str] = []
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = -1
        if pid > 0:
            kill_actions = _kill_run_process_group(pid, grace_s=kill_grace_s)

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
    """True only for CLI-stamped acceptance.result.json with matching manifest sha.

    Agent-forged ``{passed: true}`` (any path, missing writer/sha) is rejected.
    """
    from omg_cli.acceptance import is_cli_acceptance_result, result_path

    path = result_path(Path(root), run_id)
    return is_cli_acceptance_result(path, root=Path(root), run_id=run_id)


def set_verified(root: Path, run_id: str, *, force: bool = False) -> dict[str, Any]:
    """Mark verified only when CLI acceptance result exists (unless force=True).

    Requires ``acceptance.result.json`` with ``writer=="omg-cli"``, ``passed``
    true, and ``manifest_sha256`` matching the frozen manifest. force is
    intentionally not exposed by the CLI router.
    """
    root = Path(root)
    current = load_run(root, run_id)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    if not force and not _has_acceptance_artifact(root, run_id):
        raise PermissionError(
            "refusing to set verified=true without CLI acceptance result "
            f"(writer=omg-cli, passed=true, matching manifest sha) "
            f"for run_id={run_id!r}"
        )
    current["verified"] = True
    current["status"] = "verified"
    current["updated_at"] = _utc_now()
    current["verified_at"] = current["updated_at"]
    _atomic_write_json(_status_path(root, run_id), current)
    return current
