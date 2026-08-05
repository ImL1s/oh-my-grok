"""Release claim gate: overclaim, live evidence freshness, upstream drift (#78-C Task 3)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import (
    PARITY_CATEGORY_TAXONOMY,
    PARITY_MATURITY_LEVELS,
    PARITY_V2_CLASSIFICATIONS,
    load_json_object,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_claim_gate import check_parity_release_claims
from omg_cli.parity_refresh import build_refresh_plan

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "parity" / "omg-parity.json"
CATALOG_V1 = ROOT / "tests" / "fixtures" / "parity" / "upstream_catalog_v1.json"
OVERCLAIM_README = ROOT / "tests" / "fixtures" / "parity" / "claims" / "readme_overclaim.md"
HONEST_README = ROOT / "tests" / "fixtures" / "parity" / "claims" / "readme_honest.md"
OMC_PIN = "67dddfc05ff29900d8251dcec0ed9dee3c947ffa"
NEW_PIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FIXED_NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


def _fresh_iso(*, days_ago: float = 0.0, now: datetime = FIXED_NOW) -> str:
    moment = now - timedelta(days=days_ago)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _minimal_inventory() -> dict:
    full = load_json_object(INVENTORY)
    omc_ids = {
        "team.plane_v3",
        "parity.inventory.governance",
        "omc.cli.session_surfaces",
    }
    inv = copy.deepcopy(full)
    inv["capabilities"] = [
        row
        for row in inv["capabilities"]
        if row.get("upstream", {}).get("source") == "OMC"
        and row["id"] in omc_ids
    ]
    cap_ids = {row["id"] for row in inv["capabilities"]}
    inv["gaps"] = [
        gap
        for gap in inv.get("gaps", [])
        if isinstance(gap, dict)
        and all(
            cid in cap_ids
            for cid in gap.get("capability_ids", [])
            if isinstance(cid, str)
        )
    ]
    return inv


def _bootstrapping_inventory(tmp_path: Path) -> dict:
    """Minimal honest bootstrapping inventory for pass-case tests."""
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    return {
        "store_kind": "parity_inventory",
        "schema_version": 2,
        "repository_id": "OMG",
        "ownership_manifest_id": "dual-parity-writers-v1",
        "inventory_status": "bootstrapping",
        "maturity_levels": list(PARITY_MATURITY_LEVELS),
        "classifications": list(PARITY_V2_CLASSIFICATIONS),
        "upstream_pins": {
            "OMC": {
                "repository": "https://github.com/example/omc",
                "revision": OMC_PIN,
                "kind": "commit",
            },
            "OMX": {
                "repository": "https://github.com/example/omx",
                "revision": "435d4a9cc982ffaf83fabbfbb8711ae6c178ffca",
                "kind": "commit",
            },
            "OmO": {
                "repository": "https://github.com/example/omo",
                "revision": "4ca872b57e45281a9a81190bb73637729288ffc3",
                "kind": "commit",
            },
            "Antigravity": {
                "repository": "https://github.com/example/ag",
                "revision": "bfab12dac5bd090015a89cf82e65093d13b567d9",
                "kind": "commit",
            },
            "GROK_BUILD": {
                "repository": "https://github.com/example/grok-build",
                "revision": "7cfcb20d2b50b0d18801a6c0af2e401c0e060894",
                "kind": "commit",
            },
        },
        "category_status": {cat: "bootstrapping" for cat in sorted(PARITY_CATEGORY_TAXONOMY)},
        "source_status": {
            "OMC": "bootstrapping",
            "OMX": "bootstrapping",
            "OmO": "bootstrapping",
            "Antigravity": "bootstrapping",
        },
        "live_evidence_max_age_days": 30,
        "capabilities": [
            {
                "id": "team.plane_v3",
                "category": "team",
                "promise": "Team plane v3",
                "classification": "omg_native",
                "upstream": {
                    "source": "OMC",
                    "revision": OMC_PIN,
                    "source_paths": ["README.md"],
                },
                "omg_paths": ["omg_cli/team/__init__.py"],
                "runtime_owner": "omg",
                "maturity": {"grok": "catalogued"},
                "evidence": {"tests": [], "docs": [], "live": []},
                "issues": ["#69"],
                "gap": "Not yet implemented.",
            },
            {
                "id": "parity.inventory.governance",
                "category": "parity_governance",
                "promise": "Parity inventory governance",
                "classification": "omg_native",
                "upstream": {
                    "source": "OMC",
                    "revision": OMC_PIN,
                    "source_paths": ["README.md"],
                },
                "omg_paths": ["omg_cli/parity_check.py"],
                "runtime_owner": "omg",
                "maturity": {"grok": "catalogued"},
                "evidence": {"tests": [], "docs": [], "live": []},
                "issues": ["#78"],
                "gap": "Release gate in progress.",
            },
        ],
        "gaps": [
            {
                "id": "gap.team.v3",
                "priority": "P0",
                "status": "open",
                "issues": ["#69"],
                "capability_ids": ["team.plane_v3"],
                "summary": "Team v3 missing",
            }
        ],
    }


def _write_inventory(tmp_path: Path, inventory: dict) -> Path:
    path = tmp_path / "docs" / "parity" / "omg-parity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    return path


def _write_catalog(tmp_path: Path, catalog: dict) -> Path:
    path = tmp_path / "upstream_catalog.json"
    path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    return path


def _load_catalog(*, pin_revision: str = NEW_PIN) -> dict:
    catalog = load_json_object(CATALOG_V1)
    catalog["pin_revision"] = pin_revision
    return catalog


def _scaffold_inventory_paths(tmp_path: Path, inventory: dict) -> None:
    """Create stub files for omg_paths and evidence paths under tmp_path."""
    paths: set[str] = set()
    for row in inventory.get("capabilities", []):
        if not isinstance(row, dict):
            continue
        for rel in row.get("omg_paths", []):
            if isinstance(rel, str):
                paths.add(rel)
        evidence = row.get("evidence", {})
        if isinstance(evidence, dict):
            for key, values in evidence.items():
                if key == "live":
                    continue
                if isinstance(values, list):
                    for rel in values:
                        if isinstance(rel, str):
                            paths.add(rel)
    for rel in paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("", encoding="utf-8")


def _live_verified_row(row: dict, *, days_ago: float = 1.0) -> None:
    row["maturity"] = {"grok": "live_verified"}
    row["evidence"] = {
        "tests": ["tests/test_parity_claim_gate.py"],
        "docs": ["docs/parity/README.md"],
        "configured_paths": ["omg_cli/parity_check.py"],
        "install_evidence": ["plugin.json"],
        "enabled_evidence": ["hooks/hooks.json"],
        "loadable_evidence": ["omg_cli/__init__.py"],
        "observed_evidence": ["docs/parity/omg-parity.json"],
        "healthy_evidence": ["tests/test_parity_claim_gate.py"],
        "live": [
            {
                "runtime": "grok",
                "platform": "darwin-arm64",
                "version": "0.2.107",
                "observed_at": _fresh_iso(days_ago=days_ago),
                "marker": "LIVE_OK",
            }
        ],
    }


def _honest_docs(tmp_path: Path) -> None:
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(HONEST_README.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "docs" / "parity" / "SUMMARY.md").write_text(
        "# Parity summary\n\nInventory status: **bootstrapping**.\n\n"
        "Capabilities catalogued only; no percentage claimed.\n",
        encoding="utf-8",
    )


def _ack_review(
    plan: dict,
    *,
    indices: list[int] | None = None,
    use_acknowledgments_key: bool = False,
) -> dict:
    """Build review artifact with acknowledged dispositions for plan changes."""
    changes = plan["changes"]
    if indices is None:
        indices = list(range(len(changes)))
    acked = [{**changes[i], "disposition": "acknowledged"} for i in indices]
    payload: dict = {
        "store_kind": "parity_refresh_review",
        "schema_version": 1,
        "source": plan["source"],
    }
    if use_acknowledgments_key:
        payload["acknowledgments"] = acked
    else:
        payload["changes"] = acked
    return payload


def test_release_gate_rejects_expired_live_evidence(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    _live_verified_row(inventory["capabilities"][0], days_ago=90)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)

    with pytest.raises(ContractValidationError, match="fresh"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_readme_overclaim(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(
        OVERCLAIM_README.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "docs" / "parity" / "SUMMARY.md").write_text(
        "Bootstrapping inventory — **parity 95%** complete.\n", encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_unresolved_upstream_add(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    cat_path = _write_catalog(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_unresolved_upstream_delete(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"] = [
        c for c in catalog["capabilities"] if c["id"] != "omc.cli.session_surfaces"
    ]
    cat_path = _write_catalog(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_unresolved_upstream_rename(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "omc.cli.session_surfaces":
            cap["id"] = "omc.cli.session_surfaces_v2"
            break
    cat_path = _write_catalog(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_unresolved_upstream_changed(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Updated promise text"
            cap["source_paths"] = ["README.md", "skills/team/SKILL.md"]
            break
    cat_path = _write_catalog(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            now=FIXED_NOW,
        )


def test_release_gate_passes_honest_bootstrapping_inventory(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        now=FIXED_NOW,
    )

    assert payload["ok"] is True
    assert payload["inventory_status"] == "bootstrapping"
    assert payload["overclaims"] == 0
    assert payload["upstream_drift_checked"] is False
    assert payload["upstream_drift_resolved"] is False


def test_release_gate_rejects_missing_healthy_evidence_path(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    _live_verified_row(inventory["capabilities"][0], days_ago=1.0)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    healthy = tmp_path / "tests/test_parity_claim_gate.py"
    if healthy.is_file():
        healthy.unlink()
    _honest_docs(tmp_path)

    with pytest.raises(ContractValidationError, match="healthy_evidence"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_case_insensitive_full_one_to_one(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(
        "# oh-my-grok\n\nWe target FULL 1:1 coverage.\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "parity" / "SUMMARY.md").write_text(
        "Bootstrapping inventory.\n", encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_passes_when_acknowledged(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(_ack_review(plan), indent=2), encoding="utf-8")

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        upstream_catalog_path=cat_path,
        review_artifact_path=review_path,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["upstream_drift_checked"] is True
    assert payload["upstream_drift_resolved"] is True


def test_upstream_drift_passes_when_rename_acknowledged(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "omc.cli.session_surfaces":
            cap["id"] = "omc.cli.session_surfaces_v2"
            break
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(_ack_review(plan), indent=2), encoding="utf-8")

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        upstream_catalog_path=cat_path,
        review_artifact_path=review_path,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["upstream_drift_checked"] is True
    assert payload["upstream_drift_resolved"] is True


def test_upstream_drift_passes_when_acknowledgments_key_used(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_ack_review(plan, use_acknowledgments_key=True), indent=2),
        encoding="utf-8",
    )

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        upstream_catalog_path=cat_path,
        review_artifact_path=review_path,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["upstream_drift_resolved"] is True
