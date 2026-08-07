"""Plan-only upstream parity refresh review engine (#78-C)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from omg_cli.contracts.parity_schema import (
    HOST_BASELINE_PIN_ID,
    SOURCE_STATUS_IDS,
    UPSTREAM_PIN_IDS,
    validate_host_baseline_snapshot,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_git_oid,
    require_iso8601,
    require_nonempty_string,
    require_object,
    require_string_list,
)

COMMITTED_REVIEWS_RELATIVE = "docs/parity/reviews"
_UPSTREAM_CATALOG_KEYS = frozenset({"source", "pin_revision", "capabilities"})
_UPSTREAM_CAPABILITY_KEYS = frozenset({"id", "source_paths", "promise"})


def _require_relative_posix(path_text: str, *, label: str) -> str:
    text = require_nonempty_string(path_text, label=label)
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or pure.parts[0] == "~":
        raise ContractValidationError(f"{label} must be a relative POSIX path")
    return text


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


def validate_upstream_catalog(upstream_catalog: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on malformed / duplicate upstream snapshot capability rows."""
    catalog = require_object(upstream_catalog, label="upstream_catalog")
    require_exact_keys(
        catalog,
        required=_UPSTREAM_CATALOG_KEYS,
        label="upstream_catalog",
    )
    source = require_nonempty_string(catalog["source"], label="upstream_catalog.source")
    if source not in SOURCE_STATUS_IDS:
        raise ContractValidationError(
            f"upstream_catalog.source {source!r} not in allowed snapshot sources"
        )
    require_git_oid(catalog["pin_revision"], label="upstream_catalog.pin_revision")
    caps = catalog["capabilities"]
    if not isinstance(caps, list):
        raise ContractValidationError("upstream_catalog.capabilities must be an array")
    rows: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(caps):
        cap = require_object(raw, label=f"upstream_catalog.capabilities[{index}]")
        require_exact_keys(
            cap,
            required=_UPSTREAM_CAPABILITY_KEYS,
            label=f"upstream_catalog.capabilities[{index}]",
        )
        cap_id = require_nonempty_string(
            cap["id"], label=f"upstream_catalog.capabilities[{index}].id"
        )
        if cap_id in rows:
            raise ContractValidationError(
                f"duplicate upstream capability id {cap_id!r}"
            )
        paths = require_string_list(
            cap["source_paths"],
            label=f"upstream_catalog.capabilities[{index}].source_paths",
            unique=True,
        )
        if not paths:
            raise ContractValidationError(
                f"upstream_catalog.capabilities[{index}].source_paths must be non-empty"
            )
        for relative in paths:
            _require_relative_posix(
                relative,
                label=f"upstream_catalog.capabilities[{index}].source_paths[]",
            )
        require_nonempty_string(
            cap["promise"],
            label=f"upstream_catalog.capabilities[{index}].promise",
        )
        rows[cap_id] = cap
    return catalog


def _catalog_rows(upstream_catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    catalog = validate_upstream_catalog(upstream_catalog)
    rows: dict[str, dict[str, Any]] = {}
    for cap in catalog["capabilities"]:
        rows[str(cap["id"])] = cap
    return rows


def canonical_changes_digest(changes: list[dict[str, Any]]) -> str:
    """Stable SHA-256 digest over change identity + detail (no dispositions)."""
    normalized: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        kind = change.get("change_kind")
        entry: dict[str, Any] = {
            "change_kind": kind,
            "detail": change.get("detail"),
        }
        if kind == "renamed":
            entry["from_id"] = change.get("from_id")
            entry["to_id"] = change.get("to_id")
        else:
            entry["capability_id"] = change.get("capability_id")
        normalized.append(entry)

    def _sort_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(entry.get("change_kind") or ""),
            str(entry.get("capability_id") or ""),
            str(entry.get("from_id") or ""),
            str(entry.get("to_id") or ""),
        )

    normalized.sort(key=_sort_key)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def committed_review_filename(
    *,
    source: str,
    from_revision: str,
    to_revision: str,
    change_digest: str,
) -> str:
    return f"{source}-{from_revision}-{to_revision}-{change_digest}.json"


def committed_review_path(
    repo_root: Path | str,
    *,
    source: str,
    from_revision: str,
    to_revision: str,
    change_digest: str,
) -> Path:
    root = Path(repo_root)
    name = committed_review_filename(
        source=source,
        from_revision=from_revision,
        to_revision=to_revision,
        change_digest=change_digest,
    )
    return root / COMMITTED_REVIEWS_RELATIVE / name


def write_committed_refresh_review(
    repo_root: Path | str,
    plan: dict[str, Any],
    *,
    acknowledgments: list[dict[str, Any]] | None = None,
) -> Path:
    """Write immutable transition ledger under docs/parity/reviews/."""
    root = Path(repo_root)
    source = require_nonempty_string(plan.get("source"), label="plan.source")
    from_revision = require_git_oid(plan.get("from_revision"), label="plan.from_revision")
    to_revision = require_git_oid(plan.get("to_revision"), label="plan.to_revision")
    changes = plan.get("changes")
    if not isinstance(changes, list):
        raise ContractValidationError("plan.changes must be an array")
    digest = canonical_changes_digest([c for c in changes if isinstance(c, dict)])
    entries = acknowledgments
    if entries is None:
        entries = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            entries.append({**change, "disposition": "acknowledged"})
    payload = {
        "store_kind": "parity_refresh_review",
        "schema_version": 1,
        "source": source,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "generated_at": plan.get("generated_at"),
        "change_digest": digest,
        "changes": entries,
        "proposed_inventory_patch": plan.get("proposed_inventory_patch"),
        "guards": plan.get("guards"),
    }
    path = committed_review_path(
        root,
        source=source,
        from_revision=from_revision,
        to_revision=to_revision,
        change_digest=digest,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


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
        inv_fp = _capability_fingerprint(inv_rows[cap_id])
        changes.append(
            {
                "change_kind": "deleted",
                "capability_id": cap_id,
                "detail": {
                    "before": _fingerprint_payload(inv_fp),
                    "after": None,
                },
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


def _host_capability_fingerprint(cap: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": cap.get("id"),
        "classification": cap.get("classification"),
        "category": cap.get("category"),
        "promise": cap.get("promise"),
        "maturity": cap.get("maturity"),
        "source_commit": (cap.get("evidence") or {}).get("source_commit")
        if isinstance(cap.get("evidence"), Mapping)
        else None,
        "source_paths": sorted(
            list((cap.get("evidence") or {}).get("source_paths") or [])
            if isinstance(cap.get("evidence"), Mapping)
            else []
        ),
    }


def host_snapshot_content_hash(snapshot: Mapping[str, Any]) -> str:
    """Stable SHA-256 over validated host snapshot (canonical JSON)."""
    validated = validate_host_baseline_snapshot(dict(snapshot))
    payload = json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generated_docs_content_hash(repo_root: Path | str, relative_docs: list[str]) -> str:
    """SHA-256 over concatenated generated host doc bytes (sorted paths)."""
    root = Path(repo_root)
    hasher = hashlib.sha256()
    for relative in sorted(relative_docs):
        path = root / relative
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ContractValidationError(
                f"missing generated host baseline doc {relative}: {exc}"
            ) from exc
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\0")
    return hasher.hexdigest()


def build_host_baseline_refresh_plan(
    *,
    from_revision: str,
    to_revision: str,
    host_snapshot: Mapping[str, Any],
    previous_snapshot: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
    snapshot_hash: str | None = None,
    generated_docs_hash: str | None = None,
) -> dict[str, Any]:
    """Diff host-baseline capabilities across a GROK_BUILD pin transition."""
    require_git_oid(from_revision, label="from_revision")
    require_git_oid(to_revision, label="to_revision")
    snapshot = validate_host_baseline_snapshot(dict(host_snapshot))
    if snapshot["public_commit"] != to_revision:
        raise ContractValidationError(
            f"host snapshot public_commit {snapshot['public_commit']!r} != "
            f"to_revision {to_revision!r}"
        )
    prev_rows: dict[str, dict[str, Any]] = {}
    if previous_snapshot is not None:
        prev = validate_host_baseline_snapshot(dict(previous_snapshot))
        if prev["public_commit"] != from_revision:
            raise ContractValidationError(
                f"previous host snapshot public_commit {prev['public_commit']!r} != "
                f"from_revision {from_revision!r}"
            )
        for cap in prev["capabilities"]:
            prev_rows[str(cap["id"])] = cap
    new_rows = {str(cap["id"]): cap for cap in snapshot["capabilities"]}

    changes: list[dict[str, Any]] = []
    for cap_id in sorted(set(prev_rows) & set(new_rows)):
        before = _host_capability_fingerprint(prev_rows[cap_id])
        after = _host_capability_fingerprint(new_rows[cap_id])
        if before != after:
            changes.append(
                {
                    "change_kind": "changed",
                    "capability_id": cap_id,
                    "detail": {"before": before, "after": after},
                }
            )
    for cap_id in sorted(set(prev_rows) - set(new_rows)):
        changes.append(
            {
                "change_kind": "deleted",
                "capability_id": cap_id,
                "detail": {
                    "before": _host_capability_fingerprint(prev_rows[cap_id]),
                    "after": None,
                },
            }
        )
    for cap_id in sorted(set(new_rows) - set(prev_rows)):
        changes.append(
            {
                "change_kind": "added",
                "capability_id": cap_id,
                "detail": {
                    "after": _host_capability_fingerprint(new_rows[cap_id]),
                },
            }
        )

    when = generated_at or datetime.now(timezone.utc)
    generated_text = require_iso8601(
        when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        label="generated_at",
    )
    snap_hash = snapshot_hash or host_snapshot_content_hash(snapshot)
    return {
        "store_kind": "parity_refresh_review",
        "schema_version": 1,
        "source": HOST_BASELINE_PIN_ID,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "generated_at": generated_text,
        "changes": changes,
        "proposed_inventory_patch": {
            "upstream_pins": {HOST_BASELINE_PIN_ID: {"revision": to_revision}},
            "capabilities": [],
        },
        "guards": {
            "auto_maturity_upgrade": False,
            "requires_manual_mapping": True,
            "host_baseline": True,
        },
        "host_baseline": {
            "snapshot_path": "docs/parity/upstream-snapshots/grok-build.json",
            "snapshot_hash": snap_hash,
            "generated_docs_hash": generated_docs_hash or "",
            "reviewed_pin": to_revision,
            "previous_pin": from_revision,
            "release": snapshot["release"],
            "classification_complete": True,
        },
    }


def write_committed_host_baseline_review(
    repo_root: Path | str,
    plan: dict[str, Any],
    *,
    acknowledgments: list[dict[str, Any]] | None = None,
) -> Path:
    """Write GROK_BUILD pin-transition ledger under docs/parity/reviews/."""
    root = Path(repo_root)
    if plan.get("source") != HOST_BASELINE_PIN_ID:
        raise ContractValidationError(
            f"host baseline review source must be {HOST_BASELINE_PIN_ID!r}"
        )
    host_meta = require_object(plan.get("host_baseline"), label="plan.host_baseline")
    for key in ("snapshot_hash", "reviewed_pin", "previous_pin", "snapshot_path"):
        require_nonempty_string(host_meta.get(key), label=f"plan.host_baseline.{key}")
    path = write_committed_refresh_review(
        root, plan, acknowledgments=acknowledgments
    )
    # Re-read and ensure host_baseline block is persisted (write_committed strips unknown?).
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["host_baseline"] = dict(host_meta)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
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
