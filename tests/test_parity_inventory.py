from __future__ import annotations

import copy
import re
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import (
    FROZEN_PINS,
    OMG_OWNER_PATTERNS,
    PARITY_MATURITY_LEVELS,
    PARITY_V2_CLASSIFICATIONS,
    UPSTREAM_PIN_IDS,
    load_json_object,
    validate_parity_inventory,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import owner_for_path
from omg_cli.parity_check import apply_strict_parity_gates
from omg_cli.parity_ownership import (
    HISTORICAL_GOVERNANCE_CAPABILITY_IDS,
    HISTORICAL_GOVERNANCE_GAP_IDS,
    REQUIRED_CHILD_OWNERS,
    check_parity_residual_owners,
)


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "parity" / "omg-parity.json"
V1_FIXTURE = ROOT / "tests" / "fixtures" / "parity" / "omg-parity-v1.json"


def test_checked_in_inventory_is_exact_and_machine_validated() -> None:
    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)

    assert inventory["schema_version"] == 2
    assert inventory["inventory_status"] == "bootstrapping"
    assert inventory["maturity_levels"] == list(PARITY_MATURITY_LEVELS)
    assert inventory["classifications"] == list(PARITY_V2_CLASSIFICATIONS)
    assert set(inventory["upstream_pins"]) == set(UPSTREAM_PIN_IDS)
    assert "OMG" not in inventory["upstream_pins"]
    ids = [row["id"] for row in inventory["capabilities"]]
    assert "antigravity.provider.adapter" in ids
    assert all("." in cap_id for cap_id in ids)
    open_issues = {
        issue
        for gap in inventory["gaps"]
        if gap["status"] == "open"
        for issue in gap["issues"]
    }
    open_p0_issues = {
        issue
        for gap in inventory["gaps"]
        if gap["status"] == "open" and gap["priority"] == "P0"
        for issue in gap["issues"]
    }
    assert {"#69", "#77", "#79"} <= open_issues
    assert "#78" not in open_issues
    assert open_p0_issues == {"#69"}
    assert not {"#67", "#68", "#78"} & open_p0_issues
    for row in inventory["capabilities"]:
        assert all(level == "catalogued" for level in row["maturity"].values())


def test_closed_issues_67_68_cannot_be_open_p0_owners() -> None:
    """Closure-sensitive issues stay legal historically but must not own open P0 gaps."""
    from omg_cli.parity_issue_state import (
        ISSUE_STATE_EVIDENCE_RELATIVE,
        load_and_validate_issue_state_evidence,
    )

    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    evidence = load_and_validate_issue_state_evidence(ROOT / ISSUE_STATE_EVIDENCE_RELATIVE)
    assert {"#67", "#68", "#78"} <= set(evidence["closure_sensitive"])
    assert evidence["issues"]["#67"]["observed_state"] == "closed"
    assert evidence["issues"]["#68"]["observed_state"] == "closed"
    assert evidence["issues"]["#78"]["observed_state"] == "open"
    closed = {
        issue_id
        for issue_id, row in evidence["issues"].items()
        if issue_id in set(evidence["closure_sensitive"])
        and row.get("blocks_open_p0") is True
    }
    all_gap_issues = {
        issue for gap in inventory["gaps"] for issue in gap["issues"]
    }
    assert closed <= all_gap_issues
    for gap in inventory["gaps"]:
        owners = set(gap["issues"])
        if gap["status"] == "open" and gap["priority"] == "P0":
            assert not closed & owners, gap["id"]
        if gap["id"] in {
            "gap.antigravity.provider",
            "gap.jobs.durable",
            "gap.parity.governance.remaining",
        }:
            assert gap["status"] == "closed"
            assert owners & closed


def test_open_gap_owners_cannot_disappear_from_capability_issues() -> None:
    """Active gap owners must appear on related capability issues (FEATURE-MATRIX)."""
    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    by_id = {row["id"]: row for row in inventory["capabilities"]}
    for gap in inventory["gaps"]:
        if gap["status"] != "open":
            continue
        owners = set(gap["issues"])
        for cap_id in gap["capability_ids"]:
            assert cap_id in by_id, cap_id
            missing = owners - set(by_id[cap_id].get("issues") or [])
            assert not missing, (
                f"{gap['id']} owners {sorted(missing)} missing from {cap_id}.issues"
            )
    adapter_issues = by_id["antigravity.provider.adapter"]["issues"]
    assert "#77" in adapter_issues
    assert "#67" in adapter_issues
    assert "#69" in adapter_issues
    open_p0 = {
        issue
        for gap in inventory["gaps"]
        if gap["status"] == "open" and gap["priority"] == "P0"
        for issue in gap["issues"]
    }
    assert open_p0 == {"#69"}
    assert inventory["inventory_status"] == "bootstrapping"


def test_closed_governance_78_is_not_a_residual_owner() -> None:
    """Closed #78 may stay on historical governance rows only."""
    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    apply_strict_parity_gates(inventory, repo_root=ROOT)
    residual = [
        row["id"]
        for row in inventory["capabilities"]
        if "#78" in row["issues"]
        and row["id"] not in HISTORICAL_GOVERNANCE_CAPABILITY_IDS
    ]
    assert residual == []
    assert "#78" in next(
        row["issues"]
        for row in inventory["capabilities"]
        if row["id"] == "parity.inventory.governance"
    )
    for gap in inventory["gaps"]:
        if gap["status"] == "open":
            assert "#78" not in gap["issues"], gap["id"]
        if gap["id"] == "gap.parity.governance.remaining":
            assert gap["status"] == "closed"
            assert gap["issues"] == ["#78"]
    by_id = {row["id"]: row for row in inventory["capabilities"]}
    by_id.update({gap["id"]: gap for gap in inventory["gaps"]})
    for key, child in REQUIRED_CHILD_OWNERS.items():
        assert child in by_id[key]["issues"], key
    assert "#73" in by_id["omc.tools.lsp_ast"]["issues"]
    assert "#73" in by_id["omo.tools.lsp_ast_codegraph_mcp"]["issues"]
    assert "#76" in by_id["omo.edit.hash_anchored"]["issues"]
    assert "#76" in by_id["omo.quality.comment_hygiene"]["issues"]
    assert "#76" in by_id["gap.omo.edit_and_hygiene"]["issues"]
    assert by_id["omo.edit.hash_anchored"]["issues"] != ["#79"]
    assert by_id["omc.tools.lsp_ast"]["issues"] != ["#79"]


def test_strict_gate_rejects_residual_78_and_tracker_only_locks() -> None:
    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)

    residual = copy.deepcopy(inventory)
    for row in residual["capabilities"]:
        if row["id"] == "omc.skills.catalog_aliases":
            row["issues"] = ["#78"]
            break
    validate_parity_inventory(residual)
    with pytest.raises(
        ContractValidationError, match=r"omc\.skills\.catalog_aliases.*#78"
    ):
        apply_strict_parity_gates(residual)

    tracker_only = copy.deepcopy(inventory)
    for row in tracker_only["capabilities"]:
        if row["id"] == "omc.tools.lsp_ast":
            row["issues"] = ["#79"]
            break
    validate_parity_inventory(tracker_only)
    with pytest.raises(ContractValidationError, match=r"omc\.tools\.lsp_ast.*#73"):
        apply_strict_parity_gates(tracker_only)

    open_gap = copy.deepcopy(inventory)
    mutated_id = None
    for gap in open_gap["gaps"]:
        if gap["status"] == "open":
            gap["issues"] = list(gap["issues"]) + ["#78"]
            mutated_id = gap["id"]
            break
    assert mutated_id is not None
    validate_parity_inventory(open_gap)
    with pytest.raises(
        ContractValidationError, match=re.escape(mutated_id) + r".*#78"
    ):
        apply_strict_parity_gates(open_gap)

    orphan = copy.deepcopy(inventory)
    for row in orphan["capabilities"]:
        if row["id"] == "team.plane_v3":
            row["issues"] = [item for item in row["issues"] if item != "#69"]
            break
    validate_parity_inventory(orphan)
    with pytest.raises(ContractValidationError, match=r"gap\.team\.v3.*#69"):
        apply_strict_parity_gates(orphan)


def test_strict_gate_rejects_closed_non_historical_gap_78() -> None:
    inventory = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    closed_gap = copy.deepcopy(inventory)
    mutated_closed = "gap.jobs.durable"
    assert mutated_closed not in HISTORICAL_GOVERNANCE_GAP_IDS
    for gap in closed_gap["gaps"]:
        if gap["id"] == mutated_closed:
            assert gap["status"] == "closed"
            gap["issues"] = list(gap["issues"]) + ["#78"]
            break
    else:
        raise AssertionError(mutated_closed)
    validate_parity_inventory(closed_gap)
    with pytest.raises(
        ContractValidationError, match=re.escape(mutated_closed) + r".*#78"
    ):
        apply_strict_parity_gates(closed_gap)
    with pytest.raises(
        ContractValidationError, match=re.escape(mutated_closed) + r".*#78"
    ):
        check_parity_residual_owners(closed_gap)


def test_v1_migration_fixture_still_validates() -> None:
    inventory = validate_parity_inventory(load_json_object(V1_FIXTURE))
    assert inventory["schema_version"] == 1
    assert inventory["frozen_pins"] == FROZEN_PINS
    assert "OMG" in inventory["frozen_pins"]


def test_ownership_manifest_has_w0_through_w7_and_immutable_agents() -> None:
    assert list(OMG_OWNER_PATTERNS) == [f"OMG-W{index}" for index in range(8)]
    assert OMG_OWNER_PATTERNS["OMG-W7"] == ()
    flattened = [pattern for patterns in OMG_OWNER_PATTERNS.values() for pattern in patterns]
    assert not any(Path(pattern.rstrip("/**")).name == "AGENTS.md" for pattern in flattened)


@pytest.mark.parametrize(
    ("path", "owner"),
    [
        ("CLAUDE.md", "OMG-W6"),
        ("pytest.ini", "OMG-W6"),
        ("omg_cli/acceptance.py", "OMG-W6"),
        ("omg_cli/command_policy.py", "OMG-W6"),
        ("omg_cli/goals.py", "OMG-W2"),
        ("omg_cli/stop_gate.py", "OMG-W2"),
        ("omg_cli/team/cli.py", "OMG-W3"),
        ("omg_cli/team/decomposition.py", "OMG-W3"),
        ("omg_cli/team/runtime.py", "OMG-W3"),
        ("omg_cli/team/tmux.py", "OMG-W3"),
        ("scripts/live_autopilot_smoke.sh", "OMG-W6"),
        ("scripts/live_team_smoke.py", "OMG-W3"),
        ("tests/__init__.py", "OMG-W6"),
        ("tests/fixtures/__init__.py", "OMG-W6"),
        ("tests/fixtures/team_worker_fixture.py", "OMG-W3"),
        ("tests/test_acceptance.py", "OMG-W6"),
        ("tests/test_autopilot_honesty_docs.py", "OMG-W6"),
        ("tests/test_command_policy.py", "OMG-W6"),
        ("tests/test_deny.py", "OMG-W2"),
        ("tests/test_goals.py", "OMG-W2"),
        ("tests/test_stop_gate.py", "OMG-W2"),
        ("tests/test_team_cli.py", "OMG-W3"),
        ("tests/test_team_decomposition.py", "OMG-W3"),
        ("tests/test_team_lifecycle.py", "OMG-W3"),
        ("tests/test_team_runtime.py", "OMG-W3"),
        ("tests/test_team_tmux_transport.py", "OMG-W3"),
        ("omg_cli/parity_issue_state.py", "OMG-W0"),
        ("docs/parity/issue-state/v1.json", "OMG-W0"),
        ("docs/parity/issue-state/README.md", "OMG-W0"),
        ("docs/parity/reviews/README.md", "OMG-W0"),
        (
            "docs/parity/reviews/OMC-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-"
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-deadbeef.json",
            "OMG-W0",
        ),
    ],
)
def test_current_release_surfaces_have_exact_owner(path: str, owner: str) -> None:
    assert owner_for_path(path, OMG_OWNER_PATTERNS) == owner


def test_inventory_mutations_fail_closed() -> None:
    value = load_json_object(INVENTORY)
    missing = copy.deepcopy(value)
    missing["capabilities"].pop()
    # Drop corresponding gap refs by clearing gaps — still must fail on empty? 
    # Actually removing one capability while gaps still reference it fails first.
    with pytest.raises(ContractValidationError):
        validate_parity_inventory(missing, repo_root=ROOT)

    claimed = copy.deepcopy(value)
    claimed["upstream_pins"]["OMG"] = {
        "repository": "https://example.invalid/omg",
        "revision": "ffffffffffffffffffffffffffffffffffffffff",
        "kind": "commit",
    }
    with pytest.raises(ContractValidationError, match="OMG"):
        validate_parity_inventory(claimed, repo_root=ROOT)


def test_inventory_checker_cli_is_bounded_and_structured() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_parity_inventory.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"schema_version": 2' in result.stdout
    assert '"ok": true' in result.stdout
