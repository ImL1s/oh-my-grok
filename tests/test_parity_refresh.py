"""Plan-only upstream parity refresh review engine (#78-C Task 1)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import load_json_object
from omg_cli.contracts.state_schemas import ContractValidationError

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "parity" / "omg-parity.json"
CATALOG_V1 = ROOT / "tests" / "fixtures" / "parity" / "upstream_catalog_v1.json"
OMC_PIN = "67dddfc05ff29900d8251dcec0ed9dee3c947ffa"
NEW_PIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


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
    return inv


def _write_inventory(tmp_path: Path, inventory: dict) -> Path:
    path = tmp_path / "omg-parity.json"
    path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    return path


def _load_catalog(path: Path | None = None) -> dict:
    return load_json_object(path or CATALOG_V1)


def test_refresh_plan_emits_review_artifact_without_mutating_inventory(
    tmp_path: Path,
) -> None:
    from omg_cli.parity_refresh import parity_refresh

    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    before = inv_path.read_bytes()
    catalog = _load_catalog()

    artifact = parity_refresh(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
        repo_root=tmp_path,
        plan_only=True,
    )

    assert artifact.is_file()
    review = load_json_object(artifact)
    assert review["store_kind"] == "parity_refresh_review"
    assert review["schema_version"] == 1
    assert inv_path.read_bytes() == before
    for row in inventory["capabilities"]:
        peak = max(row["maturity"].values(), key=lambda m: m)
        assert peak == "catalogued"


def test_refresh_plan_classifies_upstream_added_capability(tmp_path: Path) -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
    )

    added = [c for c in plan["changes"] if c["change_kind"] == "added"]
    assert any(c["capability_id"] == "omc.new.capability" for c in added)


def test_refresh_plan_classifies_upstream_deleted_capability(tmp_path: Path) -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"] = [
        c for c in catalog["capabilities"] if c["id"] != "omc.cli.session_surfaces"
    ]

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
    )

    deleted = [c for c in plan["changes"] if c["change_kind"] == "deleted"]
    assert any(c["capability_id"] == "omc.cli.session_surfaces" for c in deleted)


def test_refresh_plan_classifies_upstream_renamed_capability(tmp_path: Path) -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "omc.cli.session_surfaces":
            cap["id"] = "omc.cli.session_surfaces_v2"
            break

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
    )

    renamed = [c for c in plan["changes"] if c["change_kind"] == "renamed"]
    assert any(
        c["from_id"] == "omc.cli.session_surfaces"
        and c["to_id"] == "omc.cli.session_surfaces_v2"
        for c in renamed
    )


def test_refresh_plan_never_auto_upgrades_maturity(tmp_path: Path) -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    inventory["capabilities"][0]["maturity"] = {"grok": "live_verified"}
    inventory["capabilities"][0]["evidence"]["live"] = [
        {
            "runtime": "grok",
            "platform": "test",
            "version": "1.0",
            "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "marker": "TEST_OK",
        }
    ]
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
    )

    assert plan["guards"]["auto_maturity_upgrade"] is False
    for stub in plan["proposed_inventory_patch"]["capabilities"]:
        for level in stub.get("maturity", {}).values():
            assert level == "catalogued"
        live = stub.get("evidence", {}).get("live", [])
        assert live == []


def test_refresh_rejects_apply_without_explicit_break_glass(tmp_path: Path) -> None:
    from omg_cli.parity_refresh import apply_refresh_plan, parity_refresh

    inventory = _minimal_inventory()
    catalog = _load_catalog()

    with pytest.raises(ContractValidationError, match="--plan"):
        parity_refresh(
            inventory=inventory,
            upstream_catalog=catalog,
            source="OMC",
            new_pin=NEW_PIN,
            repo_root=tmp_path,
            plan_only=False,
        )

    with pytest.raises(NotImplementedError):
        apply_refresh_plan({}, inventory_path=tmp_path / "omg-parity.json")
