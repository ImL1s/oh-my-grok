"""Production residual-owner gates for the parity inventory (strict mode).

Schema-only ``validate_parity_inventory`` still accepts historical ``#78``
on unit fixtures. Closed governance milestone ``#78`` must not remain a
present-tense residual owner on capability/gap ``issues`` except the
allowlisted historical governance rows. Host-baseline
``downstream_issues`` cannot list closed issues as current owners.
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
CLOSED_HOST_DOWNSTREAM_OWNERS = frozenset({"#67", "#68", "#78", "#95", "#103", "#104"})
AUTO_THEME_CAPABILITY_ID = "grok.tmux.auto_theme"
AUTO_THEME_FORBIDDEN_OWNERS = CLOSED_HOST_DOWNSTREAM_OWNERS | frozenset({"#147"})
REQUIRED_HOST_CURRENT_OWNERS = {
    "grok.session.acp_resume_no_replay": "#74",
    "grok.session.acp_close": "#74",
    "grok.session.child_restore_registration": "#74",
    "grok.session.restore_code_explicit": "#74",
    "grok.prompt_queue.lossless_ordered": "#69",
    "grok.prompt_queue.visible_while_waiting": "#69",
    "grok.prompt_queue.reorderable": "#69",
    "grok.subagent.parent_continue_reminder": "#69",
    "grok.subagent.cancel_no_restart": "#69",
    "grok.workflow.parallel_child_cap": "#69",
    "grok.dashboard.auto_recap_no_interleave": "#69",
}

__all__ = [
    "AGGREGATE_TRACKER",
    "AUTO_THEME_CAPABILITY_ID",
    "AUTO_THEME_FORBIDDEN_OWNERS",
    "CLOSED_GOVERNANCE_MILESTONE",
    "CLOSED_HOST_DOWNSTREAM_OWNERS",
    "HISTORICAL_GOVERNANCE_CAPABILITY_IDS",
    "HISTORICAL_GOVERNANCE_GAP_IDS",
    "REQUIRED_CHILD_OWNERS",
    "REQUIRED_HOST_CURRENT_OWNERS",
    "check_host_downstream_owners",
    "check_parity_residual_owners",
]


def _issue_list(row: dict[str, Any], key: str = "issues") -> list[str]:
    raw = row.get(key) or []
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
        if CLOSED_GOVERNANCE_MILESTONE not in issues:
            continue
        # Open gaps never keep #78; closed gaps only if historically allowlisted.
        if gap.get("status") == "open":
            raise ContractValidationError(
                f"open gap {gap_id} issues contain residual "
                f"{CLOSED_GOVERNANCE_MILESTONE}"
            )
        if gap_id not in HISTORICAL_GOVERNANCE_GAP_IDS:
            raise ContractValidationError(
                f"closed gap {gap_id} issues contain residual "
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


def check_host_downstream_owners(snapshot: dict[str, Any]) -> None:
    """Fail closed when host downstream_issues list a closed current owner."""
    capabilities = _rows_by_id(snapshot.get("capabilities"))
    for cap_id, row in capabilities.items():
        issues = set(_issue_list(row, "downstream_issues"))
        closed = sorted(issues & CLOSED_HOST_DOWNSTREAM_OWNERS)
        if closed:
            raise ContractValidationError(
                f"{cap_id} lists {closed[0]} as current downstream owner"
            )
        if cap_id == AUTO_THEME_CAPABILITY_ID:
            forbidden = sorted(issues & AUTO_THEME_FORBIDDEN_OWNERS)
            if forbidden:
                raise ContractValidationError(
                    f"{cap_id} lists {forbidden[0]} as current downstream owner"
                )
    for cap_id, owner in REQUIRED_HOST_CURRENT_OWNERS.items():
        required = capabilities.get(cap_id)
        if required is None:
            continue
        if owner not in _issue_list(required, "downstream_issues"):
            raise ContractValidationError(
                f"{cap_id} missing required current downstream owner {owner}"
            )
