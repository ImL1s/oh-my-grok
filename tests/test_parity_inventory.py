from __future__ import annotations

import copy
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
    assert {"#67", "#68", "#69", "#78"} <= open_issues
    for row in inventory["capabilities"]:
        assert all(level == "catalogued" for level in row["maturity"].values())


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
