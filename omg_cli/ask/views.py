"""Secret-safe offline advisor catalog facts.

Rows are projected from the static harness registry only.  This module never
calls shutil.which, subprocess, socket, urllib, or reads PATH.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from omg_cli.ask.registry import list_harness_specs, resolve_harness_id
from omg_cli.contracts.state_schemas import ContractValidationError


BINARY_PRESENCE = "not_probed"
NEXT_ACTION = (
    "no pinned identity/version/behavior fixture; do not treat as qualified"
)
CATALOG_ERROR_CODE = "E_ADVISOR_NOT_FOUND"

CATALOG_FACT_KEYS: tuple[str, ...] = (
    "harness_id",
    "aliases",
    "binary_names",
    "binary_presence",
    "observed_version",
    "tested_versions",
    "platforms",
    "advisor_read_only",
    "supports_advisor",
    "supports_executor",
    "supports_background",
    "supports_structured_output",
    "supports_resume",
    "identity_probe",
    "version_probe",
    "limitations",
    "next_action",
    "runtime_kind",
    "purpose",
    "worker_eligible",
    "authoritative",
    "auto_apply",
)

LIST_TABLE_KEYS: tuple[str, ...] = (
    "harness_id",
    "aliases",
    "advisor_read_only",
    "supports_advisor",
    "supports_executor",
    "supports_background",
    "supports_structured_output",
    "supports_resume",
    "binary_presence",
    "next_action",
)


class AdvisorCatalogError(ValueError):
    """Unknown or unresolvable catalog name.  Never a PATH/binary miss."""

    def __init__(self, message: str, *, code: str = CATALOG_ERROR_CODE) -> None:
        super().__init__(message)
        self.code = code


def _catalog_row(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "harness_id": spec["harness_id"],
        "aliases": sorted(spec["aliases"]),
        "binary_names": list(spec["binary_names"]),
        "binary_presence": BINARY_PRESENCE,
        "observed_version": None,
        "tested_versions": None,
        "platforms": [],
        "advisor_read_only": "unproven",
        "supports_advisor": False,
        "supports_executor": False,
        "supports_background": False,
        "supports_structured_output": False,
        "supports_resume": False,
        "identity_probe": "none",
        "version_probe": "none",
        "limitations": list(spec["limitations"]),
        "next_action": NEXT_ACTION,
        "runtime_kind": "external_cli",
        "purpose": "advisory",
        "worker_eligible": False,
        "authoritative": False,
        "auto_apply": False,
    }


def list_advisor_catalog() -> list[dict[str, Any]]:
    """Canonical-order catalog rows.  Shared by human and JSON surfaces."""

    return [_catalog_row(spec) for spec in list_harness_specs()]


def explain_advisor_catalog(name: str) -> dict[str, Any]:
    """Resolve *name* and return that catalog row plus ``resolved_from``."""

    try:
        harness_id = resolve_harness_id(name)
    except ContractValidationError as exc:
        raise AdvisorCatalogError(str(exc)) from exc
    stripped = name.strip()
    row = None
    for candidate in list_advisor_catalog():
        if candidate["harness_id"] == harness_id:
            row = dict(candidate)
            break
    if row is None:
        raise AdvisorCatalogError(f"unknown advisor {name!r}")
    row["resolved_from"] = stripped if stripped != harness_id else harness_id
    return row


def _format_cell(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def render_catalog_list_human(rows: list[dict[str, Any]]) -> str:
    """Tabular view derived only from catalog facts."""

    lines = ["\t".join(LIST_TABLE_KEYS)]
    for row in rows:
        lines.append("\t".join(_format_cell(row[key]) for key in LIST_TABLE_KEYS))
    return "\n".join(lines)


def render_catalog_row_human(row: Mapping[str, Any]) -> str:
    """Print the same keys/values as the JSON explain payload."""

    lines = [f"{key}: {json.dumps(row[key], ensure_ascii=False)}" for key in row]
    return "\n".join(lines)


__all__ = [
    "BINARY_PRESENCE",
    "CATALOG_ERROR_CODE",
    "CATALOG_FACT_KEYS",
    "LIST_TABLE_KEYS",
    "NEXT_ACTION",
    "AdvisorCatalogError",
    "explain_advisor_catalog",
    "list_advisor_catalog",
    "render_catalog_list_human",
    "render_catalog_row_human",
]
