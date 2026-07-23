"""Fail-closed workflow permission admission."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omg_cli.contracts.workflow_contract import effective_permissions


def admit_stage(
    stage: Mapping[str, Any],
    *,
    repository_policy: Sequence[str],
    host_capabilities: Sequence[str],
    launch_receipt_permissions: Sequence[str],
    allowed_mcp: Sequence[str] | None = None,
    allowed_write_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    declared = list(stage["permissions"])
    effective = list(
        effective_permissions(
            declared,
            repository_policy,
            host_capabilities,
            launch_receipt_permissions,
        )
    )
    missing = [item for item in declared if item not in effective]
    mcp_denied = []
    if allowed_mcp is not None:
        mcp_denied = [item for item in stage["mcp_allowlist"] if item not in set(allowed_mcp)]
    paths_denied = []
    if allowed_write_paths is not None:
        paths_denied = [item for item in stage["write_paths"] if item not in set(allowed_write_paths)]
    allowed = not missing and not mcp_denied and not paths_denied
    return {
        "stage_id": stage["id"],
        "allowed": allowed,
        "declared": declared,
        "effective": effective,
        "missing": missing,
        "mcp_denied": mcp_denied,
        "write_paths_denied": paths_denied,
        "code": None if allowed else "E_WORKFLOW_PERMISSION_DENIED",
    }


def admit_definition(
    definition: Mapping[str, Any],
    *,
    repository_policy: Sequence[str],
    host_capabilities: Sequence[str],
    launch_receipt_permissions: Sequence[str],
    allowed_mcp: Sequence[str] | None = None,
    allowed_write_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    stages = [
        admit_stage(
            stage,
            repository_policy=repository_policy,
            host_capabilities=host_capabilities,
            launch_receipt_permissions=launch_receipt_permissions,
            allowed_mcp=allowed_mcp,
            allowed_write_paths=allowed_write_paths,
        )
        for stage in definition["stages"]
    ]
    return {"allowed": all(item["allowed"] for item in stages), "stages": stages}


__all__ = ["admit_definition", "admit_stage"]
