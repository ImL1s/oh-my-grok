"""Plan-only upstream parity refresh review engine (#78-C)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.parity_schema import UPSTREAM_PIN_IDS
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_git_oid,
    require_iso8601,
    require_nonempty_string,
    require_object,
)


def _capability_fingerprint(cap: dict[str, Any]) -> tuple[frozenset[str], str]:
    paths = cap.get("source_paths")
    if paths is None and "upstream" in cap:
        paths = cap["upstream"].get("source_paths", [])
    promise = cap.get("promise", "")
    if promise == "" and "upstream" in cap and isinstance(cap["upstream"], dict):
        promise = cap["upstream"].get("promise", "")
    if not isinstance(paths, list):
        paths = []
    return frozenset(str(p) for p in paths), str(promise)


def _sorted_paths(paths: frozenset[str]) -> list[str]:
    return sorted(paths)


def _fingerprint_payload(fp: tuple[frozenset[str], str]) -> dict[str, Any]:
    return {"source_paths": _sorted_paths(fp[0]), "promise": fp[1]}


def _inventory_rows_for_source(inventory: dict[str, Any], source: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in inventory.get("capabilities", []):
        if not isinstance(row, dict):
            continue
        upstream = row.get("upstream")
        if not isinstance(upstream, dict):
            continue
        if upstream.get("source") != source:
            continue
        cap_id = row.get("id")
        if isinstance(cap_id, str) and cap_id:
            rows[cap_id] = row
    return rows


def _catalog_rows(upstream_catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for cap in upstream_catalog.get("capabilities", []):
        if not isinstance(cap, dict):
            continue
        cap_id = cap.get("id")
        if isinstance(cap_id, str) and cap_id:
            rows[cap_id] = cap
    return rows


def _default_maturity_stub(existing_row: dict[str, Any] | None) -> dict[str, str]:
    if existing_row and isinstance(existing_row.get("maturity"), dict):
        return {runtime: "catalogued" for runtime in existing_row["maturity"]}
    return {"grok": "catalogued"}


def _capability_patch_stub(
    *,
    cap_id: str,
    source: str,
    revision: str,
    source_paths: list[str],
    promise: str,
    existing_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": cap_id,
        "upstream": {
            "source": source,
            "revision": revision,
            "source_paths": list(source_paths),
            "promise": promise,
        },
        "maturity": _default_maturity_stub(existing_row),
        "evidence": {"live": []},
    }


def build_refresh_plan(
    *,
    inventory: dict[str, Any],
    upstream_catalog: dict[str, Any],
    source: str,
    new_pin: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Diff upstream catalog against inventory for one source; never upgrades maturity."""
    require_nonempty_string(source, label="source")
    if source not in UPSTREAM_PIN_IDS:
        raise ContractValidationError(f"unsupported refresh source: {source!r}")
    require_git_oid(new_pin, label="new_pin")

    catalog_source = upstream_catalog.get("source")
    if catalog_source != source:
        raise ContractValidationError(
            f"upstream catalog source {catalog_source!r} != refresh source {source!r}"
        )

    catalog_pin = require_git_oid(
        upstream_catalog.get("pin_revision"),
        label="upstream_catalog.pin_revision",
    )
    if catalog_pin != new_pin:
        raise ContractValidationError(
            f"upstream catalog pin_revision {catalog_pin!r} != new_pin {new_pin!r}"
        )

    pins = require_object(inventory.get("upstream_pins", {}), label="upstream_pins")
    if source not in pins:
        raise ContractValidationError(f"inventory missing upstream pin for {source!r}")
    from_revision = require_git_oid(pins[source]["revision"], label="from_revision")

    inv_rows = _inventory_rows_for_source(inventory, source)
    cat_rows = _catalog_rows(upstream_catalog)

    inv_ids = set(inv_rows)
    cat_ids = set(cat_rows)

    changes: list[dict[str, Any]] = []
    patch_capabilities: list[dict[str, Any]] = []

    matched_inv: set[str] = set()
    matched_cat: set[str] = set()

    for cap_id in sorted(inv_ids & cat_ids):
        inv_row = inv_rows[cap_id]
        cat_row = cat_rows[cap_id]
        inv_fp = _capability_fingerprint(inv_row)
        cat_fp = _capability_fingerprint(cat_row)
        if inv_fp != cat_fp:
            fields: list[str] = []
            if inv_fp[0] != cat_fp[0]:
                fields.append("source_paths")
            if inv_fp[1] != cat_fp[1]:
                fields.append("promise")
            changes.append(
                {
                    "change_kind": "changed",
                    "capability_id": cap_id,
                    "detail": {
                        "fields": fields,
                        "before": _fingerprint_payload(inv_fp),
                        "after": _fingerprint_payload(cat_fp),
                    },
                }
            )
            patch_capabilities.append(
                _capability_patch_stub(
                    cap_id=cap_id,
                    source=source,
                    revision=new_pin,
                    source_paths=list(cat_row.get("source_paths", [])),
                    promise=str(cat_row.get("promise", "")),
                    existing_row=inv_row,
                )
            )
        matched_inv.add(cap_id)
        matched_cat.add(cap_id)

    remaining_inv = inv_ids - matched_inv
    remaining_cat = cat_ids - matched_cat

    rename_pairs: list[tuple[str, str]] = []
    used_inv: set[str] = set()
    used_cat: set[str] = set()
    for inv_id in sorted(remaining_inv):
        inv_fp = _capability_fingerprint(inv_rows[inv_id])
        for cat_id in sorted(remaining_cat - used_cat):
            cat_fp = _capability_fingerprint(cat_rows[cat_id])
            if inv_fp == cat_fp and inv_id != cat_id:
                rename_pairs.append((inv_id, cat_id))
                used_inv.add(inv_id)
                used_cat.add(cat_id)
                break

    for from_id, to_id in rename_pairs:
        inv_row = inv_rows[from_id]
        cat_row = cat_rows[to_id]
        cat_fp = _capability_fingerprint(cat_row)
        changes.append(
            {
                "change_kind": "renamed",
                "from_id": from_id,
                "to_id": to_id,
                "detail": {
                    "source_paths": list(cat_row.get("source_paths", [])),
                    "promise": cat_fp[1],
                },
            }
        )
        patch_capabilities.append(
            _capability_patch_stub(
                cap_id=to_id,
                source=source,
                revision=new_pin,
                source_paths=list(cat_row.get("source_paths", [])),
                promise=str(cat_row.get("promise", "")),
                existing_row=inv_row,
            )
        )

    for cap_id in sorted(remaining_inv - used_inv):
        changes.append(
            {
                "change_kind": "deleted",
                "capability_id": cap_id,
                "detail": {},
            }
        )

    for cap_id in sorted(remaining_cat - used_cat):
        cat_row = cat_rows[cap_id]
        cat_fp = _capability_fingerprint(cat_row)
        changes.append(
            {
                "change_kind": "added",
                "capability_id": cap_id,
                "detail": {
                    "source_paths": list(cat_row.get("source_paths", [])),
                    "promise": cat_fp[1],
                },
            }
        )
        patch_capabilities.append(
            _capability_patch_stub(
                cap_id=cap_id,
                source=source,
                revision=new_pin,
                source_paths=list(cat_row.get("source_paths", [])),
                promise=str(cat_row.get("promise", "")),
            )
        )

    when = generated_at or datetime.now(timezone.utc)
    generated_text = require_iso8601(
        when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        label="generated_at",
    )

    return {
        "store_kind": "parity_refresh_review",
        "schema_version": 1,
        "source": source,
        "from_revision": from_revision,
        "to_revision": new_pin,
        "generated_at": generated_text,
        "changes": changes,
        "proposed_inventory_patch": {
            "upstream_pins": {source: {"revision": new_pin}},
            "capabilities": patch_capabilities,
        },
        "guards": {
            "auto_maturity_upgrade": False,
            "requires_manual_mapping": True,
        },
    }


def write_refresh_review_artifact(repo_root: Path | str, plan: dict[str, Any]) -> Path:
    """Write review artifact under .omg/artifacts/parity/."""
    root = Path(repo_root)
    source = require_nonempty_string(plan.get("source"), label="plan.source")
    to_revision = require_git_oid(plan.get("to_revision"), label="plan.to_revision")
    short = to_revision[:12]
    out_dir = root / ".omg" / "artifacts" / "parity"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"refresh-{source}-{short}.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return path


def apply_refresh_plan(
    plan: dict[str, Any],
    *,
    inventory_path: Path | str,
) -> None:
    """Not implemented in #78-C — inventory mutation requires explicit break-glass."""
    raise NotImplementedError(
        "apply_refresh_plan is not implemented in #78-C; use --plan review only"
    )


def parity_refresh(
    *,
    inventory: dict[str, Any],
    upstream_catalog: dict[str, Any],
    source: str,
    new_pin: str,
    repo_root: Path | str,
    plan_only: bool = False,
    generated_at: datetime | None = None,
) -> Path:
    """Plan-only refresh entry point; fails closed without plan_only=True."""
    if not plan_only:
        raise ContractValidationError(
            "parity refresh requires --plan; inventory mutation is not implemented in #78-C"
        )
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=upstream_catalog,
        source=source,
        new_pin=new_pin,
        generated_at=generated_at,
    )
    return write_refresh_review_artifact(repo_root, plan)
