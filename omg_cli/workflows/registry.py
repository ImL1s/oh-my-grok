"""Immutable repository-local registry for compiled workflows."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
)
from omg_cli.contracts.workflow_contract import (
    SEMVER_RE,
    WORKFLOW_NAME_RE,
    ensure_immutable_same_version,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)

from .schema import WorkflowSchemaError, compile_workflow


class WorkflowRegistryError(RuntimeError):
    pass


def registry_root(root: Path | str) -> Path:
    return Path(root).resolve() / ".omg" / "workflows" / "registry"


def definition_path(root: Path | str, name: str, version: str) -> Path:
    if not isinstance(name, str) or not WORKFLOW_NAME_RE.fullmatch(name):
        raise WorkflowRegistryError("workflow registry name is invalid")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        raise WorkflowRegistryError("workflow registry version is invalid")
    base = registry_root(root)
    path = (base / name / f"{version}.json").resolve(strict=False)
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise WorkflowRegistryError("workflow registry path escapes root") from exc
    return path


def install_workflow(
    root: Path | str,
    source: Mapping[str, Any] | Path | str | bytes,
) -> dict[str, Any]:
    definition = compile_workflow(source)
    path = definition_path(root, definition["name"], definition["workflow_version"])
    ensure_managed_dir(path.parent)
    if path.parent.is_symlink() or path.is_symlink():
        raise WorkflowRegistryError("workflow registry symlink refused")
    body = canonical_json_bytes(definition)
    with exclusive_lock(path.parent / ".registry.lock"):
        previous_versions = list_workflows(root, name=definition["name"])
        if path.exists():
            installed = load_workflow(root, definition["name"], definition["workflow_version"])
            try:
                ensure_immutable_same_version(installed, definition)
            except Exception as exc:
                raise WorkflowRegistryError(str(exc)) from exc
            if path.read_bytes() != body:
                raise WorkflowRegistryError("same workflow version has non-canonical mutable bytes")
            duplicate = True
        else:
            if previous_versions:
                installed = load_workflow(
                    root,
                    definition["name"],
                    previous_versions[-1]["workflow_version"],
                )
                try:
                    ensure_immutable_same_version(installed, definition)
                except Exception as exc:
                    raise WorkflowRegistryError(str(exc)) from exc
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
            duplicate = False
        os.chmod(path, DATA_FILE_MODE)
    return {
        "definition": definition,
        "path": path.relative_to(Path(root).resolve()).as_posix(),
        "duplicate": duplicate,
    }


def load_workflow(root: Path | str, name: str, version: str) -> dict[str, Any]:
    path = definition_path(root, name, version)
    if not path.is_file() or path.is_symlink():
        raise WorkflowRegistryError(f"workflow not found: {name}@{version}")
    try:
        parsed = parse_canonical_json_bytes(path.read_bytes())
        if not isinstance(parsed, dict):
            raise WorkflowSchemaError("workflow registry entry must be object")
        return compile_workflow(parsed)
    except Exception as exc:
        raise WorkflowRegistryError(str(exc)) from exc


def _version_key(
    version: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    """Return SemVer precedence (build metadata intentionally ignored)."""
    precedence = version.split("+", 1)[0]
    core, separator, prerelease = precedence.partition("-")
    major, minor, patch = (int(item) for item in core.split("."))
    if not separator:
        return major, minor, patch, 1, ()
    identifiers: list[tuple[int, int | str]] = []
    for item in prerelease.split("."):
        identifiers.append((0, int(item)) if item.isdigit() else (1, item))
    return major, minor, patch, 0, tuple(identifiers)


def list_workflows(root: Path | str, *, name: str | None = None) -> list[dict[str, Any]]:
    base = registry_root(root)
    directories = (
        [base / name]
        if name is not None
        else sorted(base.iterdir())
        if base.is_dir()
        else []
    )
    rows: list[dict[str, Any]] = []
    for directory in directories:
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink():
                continue
            try:
                definition = compile_workflow(path)
            except (OSError, WorkflowSchemaError):
                continue
            rows.append(
                {
                    "name": definition["name"],
                    "workflow_version": definition["workflow_version"],
                    "definition_digest": definition["definition_digest"],
                    "path": path.relative_to(Path(root).resolve()).as_posix(),
                }
            )
    rows.sort(key=lambda row: (row["name"], _version_key(row["workflow_version"])))
    return rows


def resolve_workflow(
    root: Path | str,
    name: str,
    version: str | None = None,
) -> dict[str, Any]:
    if version is not None:
        return load_workflow(root, name, version)
    matches = list_workflows(root, name=name)
    if not matches:
        raise WorkflowRegistryError(f"workflow not found: {name}")
    return load_workflow(root, name, matches[-1]["workflow_version"])


__all__ = [
    "WorkflowRegistryError",
    "definition_path",
    "install_workflow",
    "list_workflows",
    "load_workflow",
    "registry_root",
    "resolve_workflow",
]
