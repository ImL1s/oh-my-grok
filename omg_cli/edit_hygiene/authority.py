"""Team ownership + read-only capability_mode gates for mutating edit tools."""

from __future__ import annotations

import os
from pathlib import Path

from omg_cli.workers import (
    WorkerError,
    load_ownership_manifest,
    ownership_manifest_path,
)


class EditHygieneError(ValueError):
    """Base error for edit-hygiene / Team authority."""

    code = "E_EDIT"


class ReadOnlyEditError(EditHygieneError):
    """Mutating edit refused because capability_mode is read-only."""

    code = "E_READ_ONLY"


class OwnershipEditError(EditHygieneError):
    """Mutating edit refused because the path is not owned by the calling task."""

    code = "E_OWNERSHIP"


def _norm_relpath(raw: str) -> str:
    text = str(raw).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def resolve_run_task_ids(
    *,
    run_id: str | None = None,
    task_id: str | None = None,
) -> tuple[str | None, str | None]:
    rid = (run_id or os.environ.get("OMG_RUN_ID") or "").strip() or None
    tid = (task_id or os.environ.get("OMG_TASK_ID") or "").strip() or None
    return rid, tid


def capability_mode_env() -> str:
    return (os.environ.get("OMG_CAPABILITY_MODE") or "").strip().lower().replace("_", "-")


def assert_not_read_only() -> None:
    mode = capability_mode_env()
    if mode in {"read-only", "readonly"}:
        raise ReadOnlyEditError("mutating edit refused under OMG_CAPABILITY_MODE=read-only")


def _path_owned(owned: set[str], target: str) -> bool:
    needle = _norm_relpath(target)
    if not needle:
        return False
    if needle in owned:
        return True
    for item in owned:
        prefix = item.rstrip("/")
        if not prefix:
            continue
        if needle == prefix or needle.startswith(prefix + "/"):
            return True
    return False


def assert_path_owned_if_manifest(
    root: Path,
    target_path: str,
    *,
    run_id: str | None,
    task_id: str | None,
) -> None:
    """Refuse when an active ownership manifest exists and *target_path* is foreign.

    No manifest (or unreadable-missing) ⇒ host edits still allowed.
    """

    if not run_id:
        return
    try:
        mpath = ownership_manifest_path(root, run_id)
    except WorkerError as exc:
        raise OwnershipEditError(str(exc)) from exc
    if not mpath.is_file():
        return
    try:
        manifest = load_ownership_manifest(root, run_id)
    except WorkerError as exc:
        raise OwnershipEditError(str(exc)) from exc
    if not isinstance(manifest, dict):
        raise OwnershipEditError("ownership manifest is not an object")
    tasks = list(manifest.get("tasks") or [])
    if not task_id:
        raise OwnershipEditError(
            "active ownership manifest requires --task-id or OMG_TASK_ID"
        )
    wanted = str(task_id).strip()
    match: dict | None = None
    for entry in tasks:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("task_id") or "").strip() == wanted:
            match = entry
            break
    if match is None:
        raise OwnershipEditError("calling task is not in the ownership manifest")
    owned = {
        _norm_relpath(str(item))
        for item in (match.get("owned_files") or [])
        if str(item).strip()
    }
    if not _path_owned(owned, target_path):
        raise OwnershipEditError("target path is not owned by the calling task")


def assert_mutative_edit_allowed(
    root: Path,
    target_path: str,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Gate mutating ``omg edit`` tools (apply / comments --fix / simplify apply)."""

    assert_not_read_only()
    rid, tid = resolve_run_task_ids(run_id=run_id, task_id=task_id)
    assert_path_owned_if_manifest(root, target_path, run_id=rid, task_id=tid)
