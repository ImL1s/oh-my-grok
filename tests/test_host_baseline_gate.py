"""Host-baseline release gate: presence, staleness, symlink, freeze sync (#105 PR1)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import (
    FROZEN_PINS,
    HOST_BASELINE_GENERATED_RELATIVE,
    HOST_BASELINE_PIN_ID,
    HOST_BASELINE_SNAPSHOT_RELATIVE,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_claim_gate import (
    assert_host_baseline_gate,
    check_parity_release_claims,
    load_host_baseline_snapshot,
)
from tests.test_parity_claim_gate import (
    FIXED_NOW,
    _bootstrapping_inventory,
    _honest_docs,
    _scaffold_inventory_paths,
    _write_host_baseline_snapshot,
    _write_inventory,
    _write_required_snapshots,
)


def test_host_baseline_gate_passes_matching_snapshot(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    _scaffold_inventory_paths(tmp_path, inventory)
    _write_host_baseline_snapshot(tmp_path, inventory)
    payload = assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)
    assert payload["ok"] is True
    assert payload["public_commit"] == FROZEN_PINS[HOST_BASELINE_PIN_ID]


def test_host_snapshot_missing_fails(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    with pytest.raises(ContractValidationError, match="host baseline snapshot missing"):
        assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)


def test_stale_host_snapshot_rejected(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    stale_pin = "7cfcb20d2b50b0d18801a6c0af2e401c0e060894"
    snapshot = {
        "store_kind": "host_baseline_snapshot",
        "schema_version": 1,
        "host_id": HOST_BASELINE_PIN_ID,
        "repository": "https://github.com/example/grok-build",
        "public_commit": stale_pin,
        "source_revision": "4d6d11372ab8f73026a78c45a7b7e7b1310eb39f",
        "release": "0.2.121",
        "observed_version": "0.2.121",
        "platform": "test",
        "capabilities": [
            {
                "id": "grok.host.baseline.probe",
                "category": "reliability",
                "classification": "irrelevant",
                "owner": "host",
                "runtime": "grok",
                "status": "catalogued",
                "maturity": "catalogued",
                "promise": "stale probe",
                "evidence": {
                    "source_commit": stale_pin,
                    "source_paths": ["CHANGELOG.md"],
                    "notes": "stale",
                },
                "downstream_issues": [],
            }
        ],
        "review": {
            "status": "catalogued",
            "reviewed_pin": stale_pin,
            "notes": "stale",
        },
        "generated": {"docs": list(HOST_BASELINE_GENERATED_RELATIVE)},
        "issues": ["#105"],
        "maturity_floor": "catalogued",
    }
    _write_host_baseline_snapshot(tmp_path, inventory, snapshot_override=snapshot)
    with pytest.raises(ContractValidationError, match="stale host baseline"):
        assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)


def test_symlink_host_snapshot_rejected(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    real = tmp_path / "docs" / "parity" / "upstream-snapshots" / "real-host.json"
    real.parent.mkdir(parents=True, exist_ok=True)
    _write_host_baseline_snapshot(tmp_path, inventory)
    target = tmp_path / HOST_BASELINE_SNAPSHOT_RELATIVE
    data = target.read_text(encoding="utf-8")
    real.write_text(data, encoding="utf-8")
    target.unlink()
    os.symlink(real.name, target)
    with pytest.raises(ContractValidationError, match="symlink"):
        load_host_baseline_snapshot(tmp_path)


def test_malformed_host_snapshot_rejected(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    path = tmp_path / HOST_BASELINE_SNAPSHOT_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json\n", encoding="utf-8")
    for relative in HOST_BASELINE_GENERATED_RELATIVE:
        doc = tmp_path / relative
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("x\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="malformed|invalid"):
        assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)


def test_frozen_pins_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    _write_host_baseline_snapshot(tmp_path, inventory)
    monkeypatch.setitem(
        FROZEN_PINS,
        HOST_BASELINE_PIN_ID,
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    with pytest.raises(ContractValidationError, match="FROZEN_PINS"):
        assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)


def test_host_owned_cannot_claim_omg_implementation_in_release_path(
    tmp_path: Path,
) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    pin = inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"]
    snapshot = {
        "store_kind": "host_baseline_snapshot",
        "schema_version": 1,
        "host_id": HOST_BASELINE_PIN_ID,
        "repository": "https://github.com/example/grok-build",
        "public_commit": pin,
        "source_revision": "4d6d11372ab8f73026a78c45a7b7e7b1310eb39f",
        "release": "0.2.121",
        "observed_version": "0.2.121",
        "platform": "test",
        "capabilities": [
            {
                "id": "grok.dashboard.previous_turn_summary",
                "category": "dashboard",
                "classification": "host_owned",
                "owner": "host",
                "runtime": "grok",
                "status": "catalogued",
                "maturity": "catalogued",
                "promise": "dashboard summary",
                "evidence": {
                    "source_commit": pin,
                    "source_paths": ["CHANGELOG.md"],
                    "notes": "ui",
                },
                "downstream_issues": [],
                "omg_paths": ["omg_cli/team/__init__.py"],
            }
        ],
        "review": {"status": "catalogued", "reviewed_pin": pin, "notes": "bad"},
        "generated": {"docs": list(HOST_BASELINE_GENERATED_RELATIVE)},
        "issues": ["#105"],
        "maturity_floor": "catalogued",
    }
    _write_host_baseline_snapshot(tmp_path, inventory, snapshot_override=snapshot)
    with pytest.raises(ContractValidationError, match="omg_paths"):
        assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)


def test_release_gate_requires_host_snapshot(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    host = tmp_path / "docs" / "parity" / "upstream-snapshots" / "grok-build.json"
    assert host.is_file()
    host.unlink()
    assert not host.exists()
    with pytest.raises(ContractValidationError, match="host baseline snapshot"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            base_inventory=copy.deepcopy(inventory),
            now=FIXED_NOW,
        )


def test_release_gate_still_passes_with_host_snapshot(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        base_inventory=copy.deepcopy(inventory),
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["host_baseline_checked"] is True
