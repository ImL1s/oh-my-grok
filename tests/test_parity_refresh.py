"""Plan-only upstream parity refresh review engine (#78-C Task 1–2)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import load_json_object
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.main import main

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


def _load_catalog(path: Path | None = None, *, pin_revision: str = NEW_PIN) -> dict:
    catalog = load_json_object(path or CATALOG_V1)
    catalog["pin_revision"] = pin_revision
    return catalog


def _set_live_verified(row: dict) -> None:
    row["maturity"] = {"grok": "live_verified"}
    row["evidence"]["live"] = [
        {
            "runtime": "grok",
            "platform": "test",
            "version": "1.0",
            "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "marker": "TEST_OK",
        }
    ]


def _assert_maturity_stub_catalogued(stubs: list[dict]) -> None:
    for stub in stubs:
        for level in stub.get("maturity", {}).values():
            assert level == "catalogued"
        assert stub.get("evidence", {}).get("live", []) == []


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
    assert artifact.name == f"refresh-OMC-{NEW_PIN[:12]}.json"
    review = load_json_object(artifact)
    assert review["store_kind"] == "parity_refresh_review"
    assert review["schema_version"] == 1
    assert inv_path.read_bytes() == before
    for row in inventory["capabilities"]:
        peak = max(row["maturity"].values(), key=lambda m: m)
        assert peak == "catalogued"


def test_refresh_plan_classifies_upstream_added_capability() -> None:
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


def test_refresh_plan_classifies_upstream_deleted_capability() -> None:
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


def test_refresh_plan_classifies_upstream_renamed_capability() -> None:
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


def test_refresh_plan_classifies_upstream_changed_capability() -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Updated promise text"
            cap["source_paths"] = ["README.md", "skills/team/SKILL.md"]
            break

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
    )

    changed = [c for c in plan["changes"] if c["change_kind"] == "changed"]
    assert len(changed) == 1
    assert changed[0]["capability_id"] == "team.plane_v3"
    assert set(changed[0]["detail"]["fields"]) == {"promise", "source_paths"}
    detail = changed[0]["detail"]
    assert detail["before"]["promise"] != detail["after"]["promise"]
    assert detail["after"]["promise"] == "Updated promise text"
    assert detail["after"]["source_paths"] == ["README.md", "skills/team/SKILL.md"]
    assert "README.md" in detail["before"]["source_paths"]

    stubs = plan["proposed_inventory_patch"]["capabilities"]
    assert len(stubs) == 1
    stub = stubs[0]
    assert stub["id"] == "team.plane_v3"
    assert stub["upstream"]["revision"] == NEW_PIN
    assert stub["upstream"]["promise"] == "Updated promise text"
    assert stub["upstream"]["source_paths"] == ["README.md", "skills/team/SKILL.md"]
    assert stub["maturity"] == {"grok": "catalogued"}
    assert stub["evidence"]["live"] == []


def test_refresh_plan_changed_detail_binds_promise_old_to_new() -> None:
    """P2-2: changed detail must embed actual before/after promise values."""
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    old_promise = next(
        row.get("promise") or row.get("upstream", {}).get("promise", "")
        for row in inventory["capabilities"]
        if row["id"] == "team.plane_v3"
    )
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Promise revision B"
            break

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
    )
    changed = [c for c in plan["changes"] if c["change_kind"] == "changed"]
    assert len(changed) == 1
    detail = changed[0]["detail"]
    assert detail["before"]["promise"] == old_promise
    assert detail["after"]["promise"] == "Promise revision B"

    catalog["capabilities"] = copy.deepcopy(catalog["capabilities"])
    for cap in catalog["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Promise revision C"
            break
    plan_c = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
    )
    detail_c = next(
        c["detail"] for c in plan_c["changes"] if c["change_kind"] == "changed"
    )
    assert detail != detail_c
    assert detail_c["after"]["promise"] == "Promise revision C"


def test_refresh_plan_never_auto_upgrades_maturity() -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    _set_live_verified(inventory["capabilities"][0])
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
    _assert_maturity_stub_catalogued(plan["proposed_inventory_patch"]["capabilities"])


def test_refresh_plan_never_auto_upgrades_maturity_on_changed() -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    _set_live_verified(inventory["capabilities"][0])
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"][0]["promise"] = "Mutated promise"

    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=NEW_PIN,
    )

    changed = [c for c in plan["changes"] if c["change_kind"] == "changed"]
    assert len(changed) == 1
    _assert_maturity_stub_catalogued(plan["proposed_inventory_patch"]["capabilities"])


def test_refresh_plan_never_auto_upgrades_maturity_on_renamed() -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    for row in inventory["capabilities"]:
        if row["id"] == "omc.cli.session_surfaces":
            _set_live_verified(row)
            break
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
    assert len(renamed) == 1
    _assert_maturity_stub_catalogued(plan["proposed_inventory_patch"]["capabilities"])


def test_refresh_plan_rejects_catalog_pin_revision_mismatch() -> None:
    from omg_cli.parity_refresh import build_refresh_plan

    inventory = _minimal_inventory()
    catalog = _load_catalog(pin_revision=OMC_PIN)

    with pytest.raises(ContractValidationError, match="pin_revision"):
        build_refresh_plan(
            inventory=inventory,
            upstream_catalog=catalog,
            source="OMC",
            new_pin=NEW_PIN,
        )


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


def _write_catalog(tmp_path: Path, *, pin_revision: str = NEW_PIN) -> Path:
    catalog = _load_catalog(pin_revision=pin_revision)
    path = tmp_path / "upstream_catalog.json"
    path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    return path


def test_parity_refresh_plan_cli_writes_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    catalog_path = _write_catalog(tmp_path)
    before = INVENTORY.read_bytes()

    code = main(
        [
            "parity",
            "refresh",
            "--source",
            "OMC",
            "--pin",
            NEW_PIN,
            "--catalog",
            str(catalog_path),
            "--plan",
        ]
    )

    assert code == 0
    artifact = (
        tmp_path / ".omg" / "artifacts" / "parity" / f"refresh-OMC-{NEW_PIN[:12]}.json"
    )
    assert artifact.is_file()
    review = load_json_object(artifact)
    assert review["store_kind"] == "parity_refresh_review"
    assert INVENTORY.read_bytes() == before


def test_parity_refresh_without_plan_flag_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    catalog_path = _write_catalog(tmp_path)
    before = INVENTORY.read_bytes()

    code = main(
        [
            "parity",
            "refresh",
            "--source",
            "OMC",
            "--pin",
            NEW_PIN,
            "--catalog",
            str(catalog_path),
        ]
    )

    assert code == 1
    assert INVENTORY.read_bytes() == before
    assert not (tmp_path / ".omg" / "artifacts" / "parity").exists()


def test_parity_refresh_uses_global_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    catalog_path = _write_catalog(tmp_path)

    code = main(
        [
            "parity",
            "refresh",
            "--source",
            "OMC",
            "--pin",
            NEW_PIN,
            "--catalog",
            str(catalog_path),
            "--plan",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema_version"] == 1
    assert payload["command"] == "parity.refresh"
    assert payload["data"]["ok"] is True
    assert payload["data"]["source"] == "OMC"
    assert payload["data"]["to_revision"] == NEW_PIN
    assert "artifact_path" in payload["data"]
