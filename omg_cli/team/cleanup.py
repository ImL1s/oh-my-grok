"""Leader-only removal of terminal Team API artifacts after shutdown ack.

Distinct from ``orphan-cleanup`` (which only marks dead-pgid tasks stopped).
Fail-closed while the team is still running or claims are unexpired.
Never writes OMG ``verified``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import require_safe_id
from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.team.plane import team_dir, team_shutdown_request_path


CLI_WRITER = "omg-cli"
CLEANUP_STORE_KIND = "team_cleanup_receipt"
CLEANUP_SCHEMA_VERSION = 1
RUNNING_STATUSES = frozenset({"pending", "running", "launched", "in_progress"})

_TEAM_STORE_FILES = (
    "events.jsonl",
    "monitor_snapshot.json",
    "host_prompt_queue.json",
    "api-config.json",
)
_TEAM_STORE_DIRS = (
    "mailbox",
    "notify",
    "tasks",
    "workers",
    "liveness",
    "approvals",
)
_TEAM_STORE_LOCK_SUFFIXES = (".lock",)


class CleanupError(RuntimeError):
    """Cleanup refused because the team is not terminal."""

    def __init__(self, message: str, *, code: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc)


def team_api_store_dir(root: Path | str, run_id: str, team_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
    )


def shutdown_ack_path(root: Path | str, run_id: str, worker: str) -> Path:
    require_safe_id(worker, label="worker")
    return team_dir(root, run_id) / (
        f"shutdown-ack-{safe_path_key(worker, namespace='worker')}.json"
    )


def cleanup_receipt_path(root: Path | str, run_id: str, team_id: str) -> Path:
    require_safe_id(team_id, label="team_id")
    return team_dir(root, run_id) / (
        f"cleanup-{safe_path_key(team_id, namespace='team')}.json"
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _pgid_alive(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _claim_unexpired(claim: Mapping[str, Any] | None, *, now: datetime) -> bool:
    if not isinstance(claim, Mapping):
        return False
    stamp = _parse_iso(str(claim.get("leased_until") or ""))
    if stamp is None:
        return False
    return stamp > now


def _remove_tree(path: Path, *, removed: list[str], root: Path) -> None:
    if path.is_symlink():
        raise CleanupError(
            f"refusing to follow symlink during cleanup: {path}",
            code="E_TEAM_CLEANUP_PATH",
        )
    if not path.exists():
        return
    if path.is_dir():
        for child in sorted(path.iterdir(), key=lambda p: p.name):
            _remove_tree(child, removed=removed, root=root)
        path.rmdir()
        removed.append(str(path.relative_to(root)))
        return
    path.unlink()
    removed.append(str(path.relative_to(root)))


def cleanup_team_artifacts(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    meta: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    workers: Sequence[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Remove confined team-id store files after shutdown ack.

    Leaves ``team.json`` / shutdown request+acks / launch argv in the run
    ``team/`` directory. Does not call ``orphan-cleanup`` and does not set
    ``verified``.
    """

    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    root_path = Path(root).resolve()
    current = now or datetime.now(timezone.utc)
    request_path = team_shutdown_request_path(root_path, run_id)
    if not request_path.is_file():
        raise CleanupError(
            "cleanup requires a durable shutdown request",
            code="E_TEAM_CLEANUP_NO_SHUTDOWN",
        )

    missing_acks: list[str] = []
    for worker in workers:
        require_safe_id(worker, label="worker")
        if not shutdown_ack_path(root_path, run_id, worker).is_file():
            missing_acks.append(worker)
    if missing_acks:
        raise CleanupError(
            "cleanup requires shutdown ack for every worker",
            code="E_TEAM_CLEANUP_ACK_MISSING",
            details={"missing": missing_acks},
        )

    live: list[str] = []
    dry_run = bool(meta.get("dry_run"))
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        tid = str(raw.get("task_id") or "")
        status = str(raw.get("status") or "")
        pid = raw.get("pid")
        pgid = raw.get("pgid")
        try:
            pid_i = int(pid) if pid is not None else 0
        except (TypeError, ValueError):
            pid_i = 0
        try:
            pgid_i = int(pgid) if pgid is not None else 0
        except (TypeError, ValueError):
            pgid_i = 0
        if _pid_alive(pid_i) or _pgid_alive(pgid_i):
            live.append(tid or "unknown")
            continue
        if not dry_run and status in RUNNING_STATUSES:
            live.append(tid or "unknown")
    if live:
        raise CleanupError(
            "cleanup refused: team still running",
            code="E_TEAM_CLEANUP_RUNNING",
            details={"live_task_ids": live},
        )

    unexpired: list[str] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        claim = task.get("claim")
        if _claim_unexpired(claim if isinstance(claim, Mapping) else None, now=current):
            unexpired.append(str(task.get("id") or ""))
    if unexpired:
        raise CleanupError(
            "cleanup refused: unexpired task claims remain",
            code="E_TEAM_CLEANUP_CLAIMS",
            details={"task_ids": unexpired},
        )

    store = team_api_store_dir(root_path, run_id, team_id)
    removed: list[str] = []
    lock = store.with_suffix(".cleanup.lock")
    ensure_managed_dir(store if store.exists() else store.parent)
    with exclusive_lock(lock):
        if store.exists():
            if store.is_symlink() or not store.is_dir():
                raise CleanupError(
                    "team api store is not a real directory",
                    code="E_TEAM_CLEANUP_PATH",
                )
            for name in _TEAM_STORE_FILES:
                _remove_tree(store / name, removed=removed, root=root_path)
            for name in _TEAM_STORE_DIRS:
                _remove_tree(store / name, removed=removed, root=root_path)
            for child in list(store.iterdir()):
                if child.name.endswith(".lock") or child.suffix in _TEAM_STORE_LOCK_SUFFIXES:
                    _remove_tree(child, removed=removed, root=root_path)
        receipt = {
            "store_kind": CLEANUP_STORE_KIND,
            "schema_version": CLEANUP_SCHEMA_VERSION,
            "writer": CLI_WRITER,
            "run_id": run_id,
            "team_id": team_id,
            "cleaned_at": _utc_now(),
            "removed": sorted(removed),
            "never_sets_verified": True,
            "distinct_from": "orphan-cleanup",
        }
        receipt_path = cleanup_receipt_path(root_path, run_id, team_id)
        ensure_managed_dir(receipt_path.parent)
        atomic_write_bytes(
            receipt_path,
            canonical_json_bytes(receipt),
            mode=DATA_FILE_MODE,
            replace=True,
        )
    return {**receipt, "path": str(receipt_path)}


__all__ = [
    "CLEANUP_SCHEMA_VERSION",
    "CLEANUP_STORE_KIND",
    "CleanupError",
    "cleanup_receipt_path",
    "cleanup_team_artifacts",
    "shutdown_ack_path",
    "team_api_store_dir",
]
