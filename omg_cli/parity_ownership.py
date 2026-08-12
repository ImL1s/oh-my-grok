"""Production residual-owner gates for the parity inventory (strict mode).

Schema-only ``validate_parity_inventory`` still accepts historical ``#78``
on unit fixtures. Closed governance milestone ``#78`` must not remain a
present-tense residual owner on capability/gap ``issues`` except the
allowlisted historical governance rows.
"""

from __future__ import annotations

from typing import Any

from omg_cli.contracts.state_schemas import ContractValidationError

CLOSED_GOVERNANCE_MILESTONE = "#78"
HISTORICAL_GOVERNANCE_CAPABILITY_IDS = frozenset({"parity.inventory.governance"})
HISTORICAL_GOVERNANCE_GAP_IDS = frozenset({"gap.parity.governance.remaining"})
AGGREGATE_TRACKER = "#79"
REQUIRED_CHILD_OWNERS = {
    "omo.edit.hash_anchored": "#76",
    "omo.quality.comment_hygiene": "#76",
    "gap.omo.edit_and_hygiene": "#76",
    "omc.tools.lsp_ast": "#73",
    "omo.tools.lsp_ast_codegraph_mcp": "#73",
}

__all__ = [
    "AGGREGATE_TRACKER",
    "CLOSED_GOVERNANCE_MILESTONE",
    "HISTORICAL_GOVERNANCE_CAPABILITY_IDS",
    "HISTORICAL_GOVERNANCE_GAP_IDS",
    "REQUIRED_CHILD_OWNERS",
    "check_parity_residual_owners",
]


def _issue_list(row: dict[str, Any]) -> list[str]:
    raw = row.get("issues") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _rows_by_id(rows: Any) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return found
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            found[str(row["id"])] = row
    return found


def check_parity_residual_owners(inventory: dict[str, Any]) -> None:
    """Fail closed when #78 remains a residual owner or locked children vanish."""
    if inventory.get("schema_version") != 2:
        return

    capabilities = _rows_by_id(inventory.get("capabilities"))
    gaps = _rows_by_id(inventory.get("gaps"))

    for cap_id, row in capabilities.items():
        issues = _issue_list(row)
        if (
            CLOSED_GOVERNANCE_MILESTONE in issues
            and cap_id not in HISTORICAL_GOVERNANCE_CAPABILITY_IDS
        ):
            raise ContractValidationError(
                f"capability {cap_id} issues contain residual "
                f"{CLOSED_GOVERNANCE_MILESTONE}"
            )

    for gap_id, gap in gaps.items():
        issues = _issue_list(gap)
        if gap.get("status") != "open":
            continue
        # Closed HISTORICAL_GOVERNANCE_GAP_IDS may keep #78; open rows may not.
        if CLOSED_GOVERNANCE_MILESTONE in issues:
            raise ContractValidationError(
                f"open gap {gap_id} issues contain residual "
                f"{CLOSED_GOVERNANCE_MILESTONE}"
            )

    for key, child in REQUIRED_CHILD_OWNERS.items():
        if key in capabilities:
            issues = _issue_list(capabilities[key])
        elif key in gaps:
            issues = _issue_list(gaps[key])
        else:
            continue
        if child not in issues:
            raise ContractValidationError(
                f"{key} missing required child owner {child} "
                f"({AGGREGATE_TRACKER} alone is insufficient)"
            )

    for gap_id, gap in gaps.items():
        if gap.get("status") != "open":
            continue
        owners = _issue_list(gap)
        for cap_id in gap.get("capability_ids") or []:
            cap_id = str(cap_id)
            cap = capabilities.get(cap_id)
            cap_issues = _issue_list(cap) if cap is not None else []
            missing = [owner for owner in owners if owner not in cap_issues]
            if missing:
                raise ContractValidationError(
                    f"{gap_id} owners {missing} missing from {cap_id}.issues"
                )
