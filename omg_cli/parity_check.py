"""Shared parity inventory check used by CLI and scripts/check_parity_inventory.py."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from omg_cli.contracts.parity_schema import (
    NORMATIVE_ARTIFACT_HASHES,
    inventory_completion_claims_allowed,
    load_json_object,
    validate_parity_inventory,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_claim_gate import check_parity_release_claims

__all__ = [
    "ARTIFACT_PATHS_RELATIVE",
    "apply_strict_parity_gates",
    "check_parity_inventory",
    "check_parity_release_claims",
    "filter_parity_gaps",
]

ARTIFACT_PATHS_RELATIVE = {
    "requirements": ".omx/plans/omg-oma-full-parity-requirements.md",
    "prd": ".omx/plans/prd-omg-oma-full-parity-20260722.md",
    "test_spec": ".omx/plans/test-spec-omg-oma-full-parity-20260722.md",
    "plan": ".omx/plans/plan-omg-oma-full-parity-20260722.md",
}


def _check_v1_normative_artifacts(repo_root: Path) -> bool:
    paths = {
        name: repo_root / relative for name, relative in ARTIFACT_PATHS_RELATIVE.items()
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        return False
    observed = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in paths.items()
    }
    if observed != NORMATIVE_ARTIFACT_HASHES:
        raise ContractValidationError(
            "normative artifact hash drift: "
            + str({"expected": NORMATIVE_ARTIFACT_HASHES, "observed": observed})
        )
    return True


def apply_strict_parity_gates(inventory: dict[str, Any]) -> None:
    """Fail closed on overclaim / status contradictions (strict mode only)."""
    if inventory.get("schema_version") != 2:
        return
    status = inventory.get("inventory_status")
    if status not in {"bootstrapping", "complete"}:
        raise ContractValidationError("inventory_status invalid")
    if not inventory_completion_claims_allowed(inventory):
        return
    open_p0 = [
        gap
        for gap in inventory.get("gaps", [])
        if isinstance(gap, dict)
        and gap.get("status") == "open"
        and gap.get("priority") == "P0"
    ]
    if open_p0:
        raise ContractValidationError(
            "complete inventory cannot leave open P0 gaps: "
            + ",".join(str(gap.get("id")) for gap in open_p0)
        )


def check_parity_inventory(
    *,
    inventory_path: Path | str,
    repo_root: Path | str,
    strict: bool = False,
    release: bool = False,
) -> dict[str, Any]:
    """Validate the canonical (or given) parity inventory.

    When ``strict`` is true, OMG implementation paths must exist under
    ``repo_root`` and v2 overclaim gates run (same as
    ``scripts/check_parity_inventory.py --strict``).

    When ``release`` is true, strict path checks are implied and the release
    claim gate runs (upstream drift, stale live evidence, docs overclaim).

    Raises ``ContractValidationError`` on failure. Returns a success payload
    suitable for CLI/script JSON output.
    """
    root = Path(repo_root)
    path = Path(inventory_path)
    raw = load_json_object(path)
    if release:
        strict = True
    if strict:
        inventory = validate_parity_inventory(raw, repo_root=root)
        apply_strict_parity_gates(inventory)
    else:
        inventory = validate_parity_inventory(raw)

    artifacts_checked = False
    if inventory.get("schema_version") == 1:
        artifacts_checked = _check_v1_normative_artifacts(root)

    open_gaps = [
        gap
        for gap in inventory.get("gaps", [])
        if isinstance(gap, dict) and gap.get("status") == "open"
    ]
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = str(path)

    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": inventory["schema_version"],
        "repository_id": inventory["repository_id"],
        "inventory_status": inventory.get("inventory_status"),
        "capabilities": len(inventory.get("capabilities", inventory.get("rows", []))),
        "open_gaps": len(open_gaps),
        "completion_claims_allowed": (
            inventory_completion_claims_allowed(inventory)
            if inventory.get("schema_version") == 2
            else False
        ),
        "strict": bool(strict),
        "release": bool(release),
        "normative_artifacts_verified": artifacts_checked,
        "inventory_path": relative,
    }
    if release:
        release_payload = check_parity_release_claims(
            inventory_path=path,
            repo_root=root,
        )
        payload.update(
            {
                "overclaims": release_payload.get("overclaims", 0),
                "upstream_drift_checked": release_payload.get(
                    "upstream_drift_checked", False
                ),
                "upstream_drift_resolved": release_payload.get(
                    "upstream_drift_resolved", False
                ),
            }
        )
    if inventory.get("schema_version") == 1:
        payload["requirements"] = len(inventory["requirement_ids"])
        payload["mcp_operations"] = len(inventory["mcp_operations"])
        payload["semantic_lsp_proxy_count"] = inventory["semantic_lsp_proxy_count"]
    return payload


def filter_parity_gaps(
    inventory: dict[str, Any],
    *,
    priority: str | None = None,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    """Default: open gaps only. ``include_all`` lists every status."""
    gaps = [
        gap for gap in inventory.get("gaps", []) if isinstance(gap, dict)
    ]
    if not include_all:
        gaps = [gap for gap in gaps if gap.get("status") == "open"]
    if priority:
        gaps = [gap for gap in gaps if gap.get("priority") == priority]
    return gaps
