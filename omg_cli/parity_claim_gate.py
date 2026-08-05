"""Release claim gate: overclaim scanner, live evidence freshness, upstream drift (#78-C)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.parity_schema import (
    load_json_object,
    maturity_rank,
    max_runtime_maturity,
    validate_parity_inventory,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_refresh import build_refresh_plan

_DOC_SCAN_RELATIVE = (
    "README.md",
    "docs/parity/SUMMARY.md",
    "docs/parity/FEATURE-MATRIX.md",
    "docs/parity/SUMMARY.zh.md",
    "docs/parity/SUMMARY.zh-TW.md",
)
_FORBIDDEN_PHRASE_PATTERNS = (
    re.compile(r"(?i)live[ _-]?verified"),
    re.compile(r"full 1:1"),
    re.compile(r"(?i)complete parity"),
    re.compile(r"✅"),
    re.compile(r"(?i)parity \d+%"),
)
_CAPABILITY_CLAIM_RE = re.compile(
    r"(?i)\b(healthy|live[-_ ]?verified|implemented)\b"
)
_DRIFT_CHANGE_KINDS = frozenset({"added", "deleted", "renamed", "changed"})


def _now_or_utc(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _doc_restrictions_active(inventory: dict[str, Any]) -> bool:
    if inventory.get("inventory_status") != "complete":
        return True
    for row in inventory.get("capabilities", []):
        if not isinstance(row, dict):
            continue
        try:
            peak = max_runtime_maturity(row)
        except ContractValidationError:
            return True
        if maturity_rank(peak) < maturity_rank("healthy"):
            return True
    return False


def _capability_index(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in inventory.get("capabilities", []):
        if not isinstance(row, dict):
            continue
        cap_id = row.get("id")
        if isinstance(cap_id, str) and cap_id:
            rows[cap_id] = row
    return rows


def _claimed_maturity_rank(token: str) -> int:
    normalized = token.lower().replace("-", "_").replace(" ", "_")
    if normalized == "implemented":
        return maturity_rank("healthy")
    if normalized.startswith("live"):
        return maturity_rank("live_verified")
    return maturity_rank("healthy")


def _scan_doc_text(
    *,
    relative: str,
    text: str,
    restrictions_active: bool,
    capability_rows: dict[str, dict[str, Any]],
) -> list[str]:
    violations: list[str] = []
    if restrictions_active:
        for pattern in _FORBIDDEN_PHRASE_PATTERNS:
            if pattern.search(text):
                violations.append(
                    f"forbidden phrase {pattern.pattern!r} in {relative}"
                )
    for cap_id, row in capability_rows.items():
        if cap_id not in text:
            continue
        try:
            peak = max_runtime_maturity(row)
            peak_rank = maturity_rank(peak)
        except ContractValidationError:
            continue
        for line in text.splitlines():
            if cap_id not in line:
                continue
            for match in _CAPABILITY_CLAIM_RE.finditer(line):
                claimed_rank = _claimed_maturity_rank(match.group(1))
                if claimed_rank > peak_rank:
                    violations.append(
                        f"capability {cap_id!r} overclaimed in {relative} "
                        f"(claimed {match.group(1)!r}, peak {peak!r})"
                    )
    return violations


def scan_docs_for_overclaims(*, repo_root: Path, inventory: dict) -> list[str]:
    """Return human-readable overclaim violations (empty when docs are honest)."""
    root = Path(repo_root)
    restrictions_active = _doc_restrictions_active(inventory)
    capability_rows = _capability_index(inventory)
    violations: list[str] = []
    for relative in _DOC_SCAN_RELATIVE:
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        violations.extend(
            _scan_doc_text(
                relative=relative,
                text=text,
                restrictions_active=restrictions_active,
                capability_rows=capability_rows,
            )
        )
    return violations


def assert_live_evidence_fresh(inventory: dict, *, now: datetime | None = None) -> None:
    """Fail closed when live_verified rows carry stale evidence."""
    validate_parity_inventory(inventory, now=_now_or_utc(now))


def _change_identity(change: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = change.get("change_kind")
    if kind == "renamed":
        return (kind, change.get("from_id"), change.get("to_id"))
    return (kind, change.get("capability_id"))


def _review_change_entries(review_artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not review_artifact:
        return []
    for key in ("changes", "acknowledgments"):
        entries = review_artifact.get(key)
        if isinstance(entries, list):
            return [entry for entry in entries if isinstance(entry, dict)]
    return []


def _is_change_acknowledged(
    change: dict[str, Any], review_artifact: dict[str, Any] | None
) -> bool:
    identity = _change_identity(change)
    for entry in _review_change_entries(review_artifact):
        if entry.get("disposition") != "acknowledged":
            continue
        if _change_identity(entry) == identity:
            return True
    return False


def assert_upstream_drift_resolved(
    *,
    inventory: dict,
    upstream_catalog: dict,
    review_artifact: dict | None,
) -> None:
    """Fail when refresh plan diffs are not explicitly acknowledged."""
    source = upstream_catalog.get("source")
    new_pin = upstream_catalog.get("pin_revision")
    if not isinstance(source, str) or not isinstance(new_pin, str):
        raise ContractValidationError("upstream catalog missing source or pin_revision")

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=upstream_catalog,
        source=source,
        new_pin=new_pin,
    )
    unresolved: list[str] = []
    for change in plan.get("changes", []):
        if not isinstance(change, dict):
            continue
        kind = change.get("change_kind")
        if kind not in _DRIFT_CHANGE_KINDS:
            continue
        if _is_change_acknowledged(change, review_artifact):
            continue
        unresolved.append(str(change))
    if unresolved:
        raise ContractValidationError(
            "upstream drift unresolved: "
            + "; ".join(unresolved)
        )


def _load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return load_json_object(path)


def _iter_upstream_catalog_paths(
    repo_root: Path, upstream_catalog_path: Path | None
) -> list[Path]:
    if upstream_catalog_path is not None:
        return [Path(upstream_catalog_path)]
    snapshots = repo_root / "docs" / "parity" / "upstream-snapshots"
    if not snapshots.is_dir():
        return []
    return sorted(path for path in snapshots.glob("*.json") if path.is_file())


def check_parity_release_claims(
    *,
    inventory_path: Path,
    repo_root: Path,
    upstream_catalog_path: Path | None = None,
    review_artifact_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Return ok payload or raise ContractValidationError."""
    root = Path(repo_root)
    path = Path(inventory_path)
    when = _now_or_utc(now)
    raw = load_json_object(path)
    inventory = validate_parity_inventory(raw, now=when)

    overclaims = scan_docs_for_overclaims(repo_root=root, inventory=inventory)
    if overclaims:
        raise ContractValidationError(
            "release overclaim gate failed: " + "; ".join(overclaims)
        )

    assert_live_evidence_fresh(inventory, now=when)

    review_artifact = _load_optional_json(
        Path(review_artifact_path) if review_artifact_path is not None else None
    )
    catalog_paths = _iter_upstream_catalog_paths(root, upstream_catalog_path)
    upstream_checked = False
    for catalog_path in catalog_paths:
        upstream_checked = True
        catalog = load_json_object(catalog_path)
        assert_upstream_drift_resolved(
            inventory=inventory,
            upstream_catalog=catalog,
            review_artifact=review_artifact,
        )

    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = str(path)

    return {
        "ok": True,
        "inventory_status": inventory.get("inventory_status"),
        "schema_version": inventory.get("schema_version"),
        "overclaims": 0,
        "upstream_drift_resolved": upstream_checked,
        "inventory_path": relative,
    }
