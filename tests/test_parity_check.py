"""Shared parity check gate + gap filter (#78-A review fixes)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
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


def _row_by_id(rows: list[dict], row_id: str) -> dict:
    for row in rows:
        if row["id"] == row_id:
            return row
    raise AssertionError(row_id)


def test_apply_strict_rejects_residual_78_and_79_only_lock() -> None:
    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    apply_strict_parity_gates(inventory, repo_root=ROOT)

    residual = copy.deepcopy(inventory)
    issues = _row_by_id(residual["capabilities"], "omc.quality.visual_release")["issues"]
    if "#78" not in issues:
        issues.append("#78")
    validate_parity_inventory(residual)
    with pytest.raises(
        ContractValidationError, match=r"omc\.quality\.visual_release.*#78"
    ):
        apply_strict_parity_gates(residual)

    locked = copy.deepcopy(inventory)
    _row_by_id(locked["capabilities"], "omo.edit.hash_anchored")["issues"] = ["#79"]
    validate_parity_inventory(locked)
    with pytest.raises(
        ContractValidationError, match=r"omo\.edit\.hash_anchored.*#76"
    ):
        apply_strict_parity_gates(locked)


def test_strict_gate_requires_issue_state_evidence(tmp_path: Path) -> None:
    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    with pytest.raises(ContractValidationError, match="issue-state evidence missing"):
        apply_strict_parity_gates(inventory, repo_root=tmp_path)


def test_strict_gate_rejects_tampered_issue_state_digest(tmp_path: Path) -> None:
    from omg_cli.parity_issue_state import ISSUE_STATE_EVIDENCE_RELATIVE

    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    evidence_src = ROOT / ISSUE_STATE_EVIDENCE_RELATIVE
    evidence_dest = tmp_path / ISSUE_STATE_EVIDENCE_RELATIVE
    evidence_dest.parent.mkdir(parents=True)
    payload = json.loads(evidence_src.read_text(encoding="utf-8"))
    payload["content_digest"] = "0" * 64
    evidence_dest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="tampered"):
        apply_strict_parity_gates(inventory, repo_root=tmp_path)


def test_strict_gate_rejects_unknown_issue_state_store_kind(tmp_path: Path) -> None:
    from omg_cli.parity_issue_state import (
        ISSUE_STATE_EVIDENCE_RELATIVE,
        _issue_state_digest,
    )

    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    evidence_src = ROOT / ISSUE_STATE_EVIDENCE_RELATIVE
    evidence_dest = tmp_path / ISSUE_STATE_EVIDENCE_RELATIVE
    evidence_dest.parent.mkdir(parents=True)
    payload = json.loads(evidence_src.read_text(encoding="utf-8"))
    payload["store_kind"] = "not-a-real-store"
    payload["content_digest"] = _issue_state_digest(payload)
    evidence_dest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="unknown issue-state store_kind"):
        apply_strict_parity_gates(inventory, repo_root=tmp_path)


def test_strict_gate_rejects_reopening_67_in_evidence(tmp_path: Path) -> None:
    from omg_cli.parity_issue_state import (
        ISSUE_STATE_EVIDENCE_RELATIVE,
        _issue_state_digest,
    )

    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    evidence_src = ROOT / ISSUE_STATE_EVIDENCE_RELATIVE
    evidence_dest = tmp_path / ISSUE_STATE_EVIDENCE_RELATIVE
    evidence_dest.parent.mkdir(parents=True)
    payload = json.loads(evidence_src.read_text(encoding="utf-8"))
    payload["issues"]["#67"]["observed_state"] = "open"
    payload["content_digest"] = _issue_state_digest(payload)
    evidence_dest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="#67"):
        apply_strict_parity_gates(inventory, repo_root=tmp_path)


def test_issue_state_rejects_78_false_close(tmp_path: Path) -> None:
    from omg_cli.parity_issue_state import (
        ISSUE_STATE_EVIDENCE_RELATIVE,
        _issue_state_digest,
        load_and_validate_issue_state_evidence,
    )

    payload = json.loads(
        (ROOT / ISSUE_STATE_EVIDENCE_RELATIVE).read_text(encoding="utf-8")
    )
    payload["issues"]["#78"]["observed_state"] = "closed"
    payload["issues"]["#78"]["closed_at"] = "2026-08-08T08:26:30Z"
    payload["content_digest"] = _issue_state_digest(payload)
    path = tmp_path / "false-close.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="close_event after reopen"):
        load_and_validate_issue_state_evidence(path)


def test_issue_state_ttl_stale_fixture(tmp_path: Path) -> None:
    from omg_cli.parity_issue_state import (
        ISSUE_STATE_EVIDENCE_RELATIVE,
        _issue_state_digest,
        load_and_validate_issue_state_evidence,
    )

    payload = json.loads(
        (ROOT / ISSUE_STATE_EVIDENCE_RELATIVE).read_text(encoding="utf-8")
    )
    payload["freshness"] = {"semantics": "ttl", "max_age_days": 1}
    payload["source"]["observed_at"] = "2020-01-01T00:00:00Z"
    payload["content_digest"] = _issue_state_digest(payload)
    path = tmp_path / "ttl.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    load_and_validate_issue_state_evidence(
        path, now=datetime(2020, 1, 1, 12, tzinfo=timezone.utc)
    )
    with pytest.raises(ContractValidationError, match="stale"):
        load_and_validate_issue_state_evidence(
            path, now=datetime(2020, 1, 10, tzinfo=timezone.utc)
        )


def test_strict_gate_rejects_reopening_closure_sensitive_open_p0() -> None:
    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    broken = copy.deepcopy(inventory)
    for gap in broken["gaps"]:
        if gap["status"] == "open" and gap["priority"] == "P0":
            gap["issues"] = list(gap["issues"]) + ["#67"]
            for cap in broken["capabilities"]:
                if cap["id"] in gap["capability_ids"]:
                    if "#67" not in cap["issues"]:
                        cap["issues"] = list(cap["issues"]) + ["#67"]
            break
    with pytest.raises(ContractValidationError, match="reopening"):
        apply_strict_parity_gates(broken, repo_root=ROOT)


def test_strict_gate_rejects_closed_governance_as_residual_owner() -> None:
    inventory = load_json_object(INVENTORY)
    validated = validate_parity_inventory(inventory, repo_root=ROOT)
    apply_strict_parity_gates(validated, repo_root=ROOT)

    broken = copy.deepcopy(validated)
    for row in broken["capabilities"]:
        if row["id"] == "omo.quality.comment_hygiene":
            row["issues"] = ["#79"]
            break
    with pytest.raises(ContractValidationError, match="#76"):
        apply_strict_parity_gates(broken)

    residual = copy.deepcopy(validated)
    for row in residual["capabilities"]:
        if row["id"] == "omo.tools.lsp_ast_codegraph_mcp":
            row["issues"] = ["#78"]
            break
    with pytest.raises(ContractValidationError, match="residual #78"):
        apply_strict_parity_gates(residual)

