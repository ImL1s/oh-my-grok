# omg_cli/state.py
"""Authoritative run-state single-writer for oh-my-grok.

Only the omg CLI (this module) may mutate status / passes / verified under
``.omg/state/runs/<run_id>/``. Hooks and agents may only append events or write
proposals under ``.omg/artifacts/``.
"""
from __future__ import annotations

import json
import os
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


def create_run(
    root: Path,
    *,
    mode: str,
    goal: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new run directory + status.json and point active.json at it."""
    root = Path(root)
    ensure_omg_dirs(root)
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
    current["status"] = status
    current["updated_at"] = _utc_now()
    if extra:
        for k, v in extra.items():
            if k == "verified":
                continue  # use set_verified only
            if k in ("run_id",):
                continue
            current[k] = v
    # Guard: never flip verified via write_status
    if current.get("verified") is True and not _has_acceptance_artifact(root, run_id):
        current["verified"] = False
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


def cancel_run(root: Path, run_id: str | None = None) -> dict[str, Any]:
    """Mark run cancelled and clear active if it matches. Does not delete artifacts."""
    root = Path(root)
    if run_id is None:
        active = load_active_run(root)
        if active is None:
            raise FileNotFoundError("no active run to cancel")
        run_id = active["run_id"]
    current = load_run(root, run_id)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    current["status"] = "cancelled"
    current["verified"] = False
    current["updated_at"] = _utc_now()
    current["cancelled_at"] = current["updated_at"]
    _atomic_write_json(_status_path(root, run_id), current)
    clear_active(root, run_id)
    return current


def _has_acceptance_artifact(root: Path, run_id: str) -> bool:
    """True if a verifier/acceptance artifact exists for this run."""
    candidates = [
        Path(root) / ".omg" / "state" / "runs" / run_id / "acceptance.json",
        Path(root) / ".omg" / "artifacts" / run_id / "acceptance.json",
        Path(root) / ".omg" / "artifacts" / f"{run_id}-acceptance.json",
    ]
    for p in candidates:
        data = _read_json(p)
        if not data:
            continue
        if data.get("passed") is True or data.get("accepted") is True:
            return True
    return False


def set_verified(root: Path, run_id: str, *, force: bool = False) -> dict[str, Any]:
    """Mark verified only when acceptance artifact exists (unless force=True for tests).

    force is intentionally not exposed by the CLI router in v1.
    """
    root = Path(root)
    current = load_run(root, run_id)
    if current is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
    if not force and not _has_acceptance_artifact(root, run_id):
        raise PermissionError(
            "refusing to set verified=true without acceptance artifact "
            f"for run_id={run_id!r}"
        )
    current["verified"] = True
    current["status"] = "verified"
    current["updated_at"] = _utc_now()
    current["verified_at"] = current["updated_at"]
    _atomic_write_json(_status_path(root, run_id), current)
    return current
