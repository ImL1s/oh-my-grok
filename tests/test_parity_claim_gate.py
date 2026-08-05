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


REQUIRED_SNAPSHOT_SOURCES = ("OMC", "OMX", "OmO", "Antigravity")


def _write_required_snapshots(
    tmp_path: Path,
    inventory: dict,
    *,
    override: dict[str, dict] | None = None,
) -> None:
    """Seed all required upstream-snapshots/{Source}.json files from inventory."""
    snap_dir = tmp_path / "docs" / "parity" / "upstream-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    pins = inventory["upstream_pins"]
    override = override or {}
    for source in REQUIRED_SNAPSHOT_SOURCES:
        if source in override:
            catalog = copy.deepcopy(override[source])
        else:
            caps = []
            for row in inventory.get("capabilities", []):
                if not isinstance(row, dict):
                    continue
                upstream = row.get("upstream")
                if not isinstance(upstream, dict) or upstream.get("source") != source:
                    continue
                caps.append(
                    {
                        "id": row["id"],
                        "source_paths": list(upstream.get("source_paths", [])),
                        "promise": row.get("promise", ""),
                    }
                )
            catalog = {
                "source": source,
                "pin_revision": pins[source]["revision"],
                "capabilities": caps,
            }
        (snap_dir / f"{source}.json").write_text(
            json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
        )


def _ack_review(
    plan: dict,
    *,
    indices: list[int] | None = None,
    use_acknowledgments_key: bool = False,
    source: str | None = None,
    from_revision: str | None = None,
    to_revision: str | None = None,
    mutate_detail: bool = False,
) -> dict:
    """Build review artifact with acknowledged dispositions for plan changes."""
    changes = plan["changes"]
    if indices is None:
        indices = list(range(len(changes)))
    acked = []
    for i in indices:
        entry = {**changes[i], "disposition": "acknowledged"}
        if mutate_detail:
            entry["detail"] = {"fields": ["tampered"]}
        acked.append(entry)
    payload: dict = {
        "store_kind": "parity_refresh_review",
        "schema_version": 1,
        "source": source if source is not None else plan["source"],
        "from_revision": (
            from_revision if from_revision is not None else plan["from_revision"]
        ),
        "to_revision": to_revision if to_revision is not None else plan["to_revision"],
        "generated_at": plan.get("generated_at", "2026-08-05T12:00:00Z"),
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
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
    _write_required_snapshots(tmp_path, inventory)

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        now=FIXED_NOW,
    )

    assert payload["ok"] is True
    assert payload["inventory_status"] == "bootstrapping"
    assert payload["overclaims"] == 0
    assert payload["upstream_drift_checked"] is True
    assert payload["upstream_drift_resolved"] is True


def test_release_gate_rejects_missing_required_upstream_snapshot(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json").unlink()

    with pytest.raises(ContractValidationError, match="missing required upstream snapshot"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_missing_upstream_snapshots_directory(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)

    with pytest.raises(ContractValidationError, match="upstream-snapshots"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_parity_readme_overclaim(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "docs" / "parity" / "README.md").write_text(
        "# Parity\n\nWe claim **complete parity** and **full 1:1** coverage.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_snapshot_pin_mismatch(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    bad = {
        "source": "OMC",
        "pin_revision": NEW_PIN,
        "capabilities": [
            {
                "id": "team.plane_v3",
                "source_paths": ["README.md"],
                "promise": "Team plane v3",
            },
            {
                "id": "parity.inventory.governance",
                "source_paths": ["README.md"],
                "promise": "Parity inventory governance",
            },
        ],
    }
    _write_required_snapshots(tmp_path, inventory, override={"OMC": bad})

    with pytest.raises(ContractValidationError, match="pin_revision"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


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
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
        new_pin=OMC_PIN,
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
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
        new_pin=OMC_PIN,
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
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
        new_pin=OMC_PIN,
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


def test_upstream_drift_rejects_ack_with_wrong_source(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_ack_review(plan, source="OMX"), indent=2), encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_rejects_ack_with_wrong_revision(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
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
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_ack_review(plan, to_revision=NEW_PIN), indent=2), encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_rejects_ack_with_tampered_detail(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Updated promise text"
            break
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_ack_review(plan, mutate_detail=True), indent=2), encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def test_doc_scan_covers_authoritative_parity_paths() -> None:
    from omg_cli.parity_claim_gate import _DOC_SCAN_RELATIVE

    required = {
        "README.md",
        "CHANGELOG.md",
        "docs/skills.md",
        "docs/parity/README.md",
        "docs/parity/schema-v2.md",
        "docs/parity/FEATURE-MATRIX.md",
        "docs/parity/GAPS.md",
        "docs/parity/MATRIX-OMC.md",
        "docs/parity/MATRIX-OMX.md",
        "docs/parity/MATRIX-OmO.md",
        "docs/parity/MATRIX-Antigravity.md",
        "docs/parity/SUMMARY.md",
        "docs/parity/SUMMARY.zh.md",
        "docs/parity/SUMMARY.zh-TW.md",
    }
    assert required.issubset(set(_DOC_SCAN_RELATIVE))


def test_release_gate_rejects_changelog_overclaim(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Achieved **complete parity** with upstream.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_skills_md_overclaim(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "skills.md").write_text(
        "# Skills\n\nOMG now offers **full 1:1** parity skill coverage.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def _mark_healthy(row: dict) -> None:
    row["maturity"] = {"grok": "healthy"}
    row["evidence"] = {
        "tests": ["tests/test_parity_claim_gate.py"],
        "docs": ["docs/parity/README.md"],
        "configured_paths": ["omg_cli/parity_check.py"],
        "install_evidence": ["plugin.json"],
        "enabled_evidence": ["hooks/hooks.json"],
        "loadable_evidence": ["omg_cli/__init__.py"],
        "observed_evidence": ["docs/parity/omg-parity.json"],
        "healthy_evidence": ["tests/test_parity_claim_gate.py"],
        "live": [],
    }


def test_release_gate_keeps_forbidden_scan_when_category_or_source_bootstrapping(
    tmp_path: Path,
) -> None:
    """P1-2: inventory_status=complete + healthy caps must not disable global scan
    while category_status / source_status remain bootstrapping."""
    inventory = _bootstrapping_inventory(tmp_path)
    inventory["inventory_status"] = "complete"
    for row in inventory["capabilities"]:
        _mark_healthy(row)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "README.md").write_text(
        "# oh-my-grok\n\nWe claim **complete parity** already.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_mislabeled_upstream_snapshot_source(tmp_path: Path) -> None:
    """P2-1: OMC.json must declare source==OMC; mislabeling must fail closed."""
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    pins = inventory["upstream_pins"]
    # Valid OMX catalogue written into OMC.json — would skip OMC coverage if
    # the gate only checked filename presence.
    mislabeled = {
        "source": "OMX",
        "pin_revision": pins["OMX"]["revision"],
        "capabilities": [],
    }
    _write_required_snapshots(tmp_path, inventory, override={"OMC": mislabeled})

    with pytest.raises(ContractValidationError, match=r"source.*OMC|expected.*OMC"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_rejects_stale_ack_after_promise_mutates(tmp_path: Path) -> None:
    """P2-2: ack for promise A→B must not clear drift when promise becomes C."""
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)

    catalog_b = _load_catalog(pin_revision=OMC_PIN)
    catalog_b = copy.deepcopy(catalog_b)
    for cap in catalog_b["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Promise revision B"
            break
    plan_b = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog_b,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    stale_ack = _ack_review(plan_b)

    catalog_c = copy.deepcopy(catalog_b)
    for cap in catalog_c["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Promise revision C"
            break
    cat_path = _write_catalog(tmp_path, catalog_c)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(stale_ack, indent=2), encoding="utf-8")

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def test_upstream_snapshots_match_inventory_pins() -> None:
    """Each required upstream snapshot pin equals upstream_pins[source].revision."""
    from omg_cli.parity_claim_gate import REQUIRED_UPSTREAM_SNAPSHOT_SOURCES

    inventory = load_json_object(INVENTORY)
    pins = inventory["upstream_pins"]
    snap_dir = ROOT / "docs" / "parity" / "upstream-snapshots"
    assert snap_dir.is_dir(), "upstream-snapshots directory missing"
    seen: set[str] = set()
    for source in REQUIRED_UPSTREAM_SNAPSHOT_SOURCES:
        path = snap_dir / f"{source}.json"
        assert path.is_file(), f"missing required snapshot {path.name}"
        snapshot = load_json_object(path)
        assert snapshot["source"] == source
        assert source in pins, f"{path.name}: unknown source {source!r}"
        assert snapshot["pin_revision"] == pins[source]["revision"], (
            f"{path.name}: pin_revision {snapshot['pin_revision']!r} "
            f"!= upstream_pins[{source!r}].revision {pins[source]['revision']!r}"
        )
        seen.add(source)
    assert seen == set(REQUIRED_UPSTREAM_SNAPSHOT_SOURCES)


def test_readme_documents_catalog_update_before_refresh_plan() -> None:
    """New-pin flow: update snapshot pin_revision first, then run --plan."""
    readme = (ROOT / "docs" / "parity" / "README.md").read_text(encoding="utf-8")
    assert "update the snapshot catalogue **first**" in readme
    assert "pin must match the catalogue pin_revision" in readme
    first = readme.index("update the snapshot catalogue **first**")
    plan = readme.index("omg parity refresh")
    assert first < plan


def test_release_gate_rejects_live_proven_phrase(tmp_path: Path) -> None:
    """P2: live-proven is a forbidden maturity synonym while bootstrapping."""
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(
        "Honest bootstrapping inventory.\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "Live-proven: grok inspect loads the contract.\n", encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match=r"overclaim|live.?proven"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_duplicate_upstream_capability_ids(tmp_path: Path) -> None:
    """P2: duplicate capability ids in a snapshot must fail closed (no LWW)."""
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    pins = inventory["upstream_pins"]
    dup = {
        "source": "OMC",
        "pin_revision": pins["OMC"]["revision"],
        "capabilities": [
            {
                "id": "team.plane_v3",
                "source_paths": ["README.md"],
                "promise": "new promise",
            },
            {
                "id": "team.plane_v3",
                "source_paths": ["README.md"],
                "promise": "Team plane v3",
            },
        ],
    }
    _write_required_snapshots(tmp_path, inventory, override={"OMC": dup})

    with pytest.raises(ContractValidationError, match="duplicate upstream capability"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_malformed_upstream_capability_row(tmp_path: Path) -> None:
    """P2: missing promise / non-object rows must fail closed."""
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    pins = inventory["upstream_pins"]
    bad = {
        "source": "OMC",
        "pin_revision": pins["OMC"]["revision"],
        "capabilities": [
            {"id": "team.plane_v3", "source_paths": ["README.md"]},
        ],
    }
    _write_required_snapshots(tmp_path, inventory, override={"OMC": bad})

    with pytest.raises(ContractValidationError, match=r"key mismatch|promise"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_pin_transition_requires_committed_review_even_when_zero_drift(
    tmp_path: Path,
) -> None:
    """P1: synced pin bump (inventory==snapshot) still needs docs/parity/reviews."""
    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "cccccccccccccccccccccccccccccccccccccccc"
    inventory["upstream_pins"]["OMC"]["revision"] = new_pin
    for row in inventory["capabilities"]:
        if row.get("upstream", {}).get("source") == "OMC":
            row["upstream"]["revision"] = new_pin
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)

    with pytest.raises(
        ContractValidationError, match="pin transition missing committed refresh review"
    ):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            base_inventory=base,
            now=FIXED_NOW,
        )


def test_pin_transition_passes_with_committed_review(tmp_path: Path) -> None:
    """P1: matching docs/parity/reviews ledger clears pin-transition gate."""
    from omg_cli.parity_refresh import write_committed_refresh_review

    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "dddddddddddddddddddddddddddddddddddddddd"
    inventory["upstream_pins"]["OMC"]["revision"] = new_pin
    for row in inventory["capabilities"]:
        if row.get("upstream", {}).get("source") == "OMC":
            row["upstream"]["revision"] = new_pin
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)

    # Build catalog from candidate snapshots for OMC.
    catalog = load_json_object(
        tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json"
    )
    plan = build_refresh_plan(
        inventory=base,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=new_pin,
        generated_at=FIXED_NOW,
    )
    write_committed_refresh_review(tmp_path, plan)

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        base_inventory=base,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["pin_transitions_reviewed"] is True


def test_upstream_drift_rejects_stale_delete_ack_after_fingerprint_mutates(
    tmp_path: Path,
) -> None:
    """P2: delete ack bound to fingerprint A must not clear delete of fingerprint B."""
    inventory = _minimal_inventory()
    target = next(
        row for row in inventory["capabilities"] if row["id"] == "omc.cli.session_surfaces"
    )
    # Ack deletion of original fingerprint.
    catalog_missing = _load_catalog(pin_revision=OMC_PIN)
    catalog_missing = copy.deepcopy(catalog_missing)
    catalog_missing["capabilities"] = [
        c for c in catalog_missing["capabilities"] if c["id"] != "omc.cli.session_surfaces"
    ]
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog_missing,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    stale_ack = _ack_review(plan)

    # Mutate inventory row fingerprint, then delete again — stale ack must fail.
    target["promise"] = "Mutated promise after prior delete ack"
    target["upstream"]["source_paths"] = ["README.md", "skills/mutated/SKILL.md"]
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    cat_path = _write_catalog(tmp_path, catalog_missing)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(stale_ack, indent=2), encoding="utf-8")

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )
