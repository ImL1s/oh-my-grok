"""Shared parity check gate + gap filter (#78-A review fixes)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import load_json_object, validate_parity_inventory
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_check import (
    apply_strict_parity_gates,
    check_parity_inventory,
    filter_parity_gaps,
)


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "parity" / "omg-parity.json"


def test_strict_check_shared_by_cli_and_script() -> None:
    via_lib = check_parity_inventory(
        inventory_path=INVENTORY,
        repo_root=ROOT,
        strict=True,
    )
    assert via_lib["ok"] is True
    assert via_lib["strict"] is True

    script = subprocess.run(
        [sys.executable, "scripts/check_parity_inventory.py", "--strict"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert script.returncode == 0, script.stderr
    via_script = json.loads(script.stdout)
    assert via_script["ok"] is True
    assert via_script["strict"] is True
    assert via_script["schema_version"] == via_lib["schema_version"]
    assert via_script["open_gaps"] == via_lib["open_gaps"]
    assert via_script["capabilities"] == via_lib["capabilities"]


def test_strict_gate_rejects_complete_inventory_with_open_p0(tmp_path: Path) -> None:
    inventory = load_json_object(INVENTORY)
    broken = copy.deepcopy(inventory)
    broken["inventory_status"] = "complete"
    broken["category_status"] = {key: "complete" for key in broken["category_status"]}
    broken["source_status"] = {key: "complete" for key in broken["source_status"]}
    path = tmp_path / "omg-parity.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    # Schema + paths still pass without strict overclaim gate when not requested…
    check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=False)

    # …but strict refuses complete + open P0.
    with pytest.raises(ContractValidationError, match="open P0"):
        check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=True)

    validated = validate_parity_inventory(broken)
    with pytest.raises(ContractValidationError, match="open P0"):
        apply_strict_parity_gates(validated)


def test_strict_requires_existing_omg_paths(tmp_path: Path) -> None:
    inventory = load_json_object(INVENTORY)
    broken = copy.deepcopy(inventory)
    broken["capabilities"][0]["omg_paths"] = ["does/not/exist.py"]
    path = tmp_path / "omg-parity.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    # Non-strict: schema only (path existence not required).
    check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=False)

    with pytest.raises(ContractValidationError, match="omg implementation path"):
        check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=True)


def test_strict_rejects_empty_omg_paths_for_claimable_classifications(
    tmp_path: Path,
) -> None:
    inventory = load_json_object(INVENTORY)
    broken = copy.deepcopy(inventory)
    broken["capabilities"][0]["omg_paths"] = []
    path = tmp_path / "omg-parity.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=False)
    with pytest.raises(ContractValidationError, match="non-empty omg_paths"):
        check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=True)


def test_strict_rejects_unverifiable_healthy_evidence(tmp_path: Path) -> None:
    inventory = load_json_object(INVENTORY)
    broken = copy.deepcopy(inventory)
    row = broken["capabilities"][0]
    row["maturity"] = {"grok": "healthy"}
    row["evidence"] = {
        "tests": ["tests/test_parity_check.py"],
        "docs": ["docs/parity/README.md"],
        "live": [],
        "configured_paths": ["omg_cli/ask/providers.py"],
        "install_evidence": ["plugin.json"],
        "enabled_evidence": ["hooks/hooks.json"],
        "loadable_evidence": ["omg_cli/__init__.py"],
        "observed_evidence": ["docs/parity/omg-parity.json"],
        "healthy_evidence": ["x"],
    }
    path = tmp_path / "omg-parity.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    # Schema-only (no repo_root) still accepts opaque evidence strings.
    validate_parity_inventory(broken)

    with pytest.raises(ContractValidationError, match="healthy_evidence"):
        check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=True)


def test_filter_parity_gaps_defaults_to_open_only() -> None:
    inventory = load_json_object(INVENTORY)
    inventory = copy.deepcopy(inventory)
    inventory["gaps"].append(
        {
            "id": "gap.closed.example",
            "priority": "P0",
            "status": "closed",
            "issues": ["#78"],
            "capability_ids": ["parity.inventory.governance"],
            "summary": "closed example",
        }
    )
    open_only = filter_parity_gaps(inventory)
    assert all(gap["status"] == "open" for gap in open_only)
    assert "gap.closed.example" not in {gap["id"] for gap in open_only}

    p0_open = filter_parity_gaps(inventory, priority="P0")
    assert all(gap["status"] == "open" and gap["priority"] == "P0" for gap in p0_open)
    assert "gap.closed.example" not in {gap["id"] for gap in p0_open}

    all_gaps = filter_parity_gaps(inventory, include_all=True)
    assert "gap.closed.example" in {gap["id"] for gap in all_gaps}

    all_p0 = filter_parity_gaps(inventory, priority="P0", include_all=True)
    assert "gap.closed.example" in {gap["id"] for gap in all_p0}


def test_strict_check_invokes_completeness_promotion_gate(tmp_path: Path) -> None:
    """Closing P0s and flipping status strings is no longer enough for --strict.

    Promote a source that still lacks a committed completeness triple (Antigravity).
    OMC/OMX/OmO now have committed artifacts, so flipping those alone is insufficient
    to prove the gate still fails closed for unproven sources.
    """
    inventory = load_json_object(INVENTORY)
    broken = copy.deepcopy(inventory)
    for gap in broken["gaps"]:
        if gap.get("priority") == "P0":
            gap["status"] = "closed"
    broken["source_status"]["Antigravity"] = "complete"
    path = tmp_path / "omg-parity.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(ContractValidationError, match="completeness proof"):
        check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=True)

    validated = validate_parity_inventory(broken)
    with pytest.raises(ContractValidationError, match="completeness proof"):
        apply_strict_parity_gates(validated, repo_root=ROOT)


def test_strict_payload_reports_completeness_proof_state() -> None:
    payload = check_parity_inventory(
        inventory_path=INVENTORY,
        repo_root=ROOT,
        strict=True,
    )
    assert payload["ok"] is True
    assert payload["completeness_gate_checked"] is True
    assert payload["completeness_proofs_required"] is False
    assert payload["completeness_proofs_verified"] == 0
    assert payload["promoted_sources"] == []
    assert payload["promoted_categories"] == []

    script = subprocess.run(
        [sys.executable, "scripts/check_parity_inventory.py", "--strict"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert script.returncode == 0, script.stderr
    via_script = json.loads(script.stdout)
    assert via_script["completeness_gate_checked"] is True
    assert via_script["completeness_proofs_required"] is False
    assert via_script["completeness_proofs_verified"] == 0

