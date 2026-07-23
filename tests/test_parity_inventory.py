from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.contracts.capability_schema import CAPABILITY_TIERS, PARITY_CLASSIFICATIONS
from omg_cli.contracts.parity_schema import (
    FROZEN_PINS,
    OMG_MCP_OPERATIONS,
    OMG_OWNER_PATTERNS,
    REQUIREMENT_ID_SET,
    load_json_object,
    validate_parity_inventory,
)
from omg_cli.contracts.state_schemas import ContractValidationError


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "parity" / "omg-parity.json"


def test_checked_in_inventory_is_exact_and_machine_validated() -> None:
    inventory = validate_parity_inventory(load_json_object(INVENTORY))

    assert inventory["frozen_pins"] == FROZEN_PINS
    assert inventory["requirement_ids"] == list(REQUIREMENT_ID_SET)
    assert inventory["classifications"] == list(PARITY_CLASSIFICATIONS)
    assert inventory["capability_tiers"] == list(CAPABILITY_TIERS)
    assert inventory["mcp_operations"] == list(OMG_MCP_OPERATIONS)
    assert len(inventory["mcp_operations"]) == 9
    assert inventory["semantic_lsp_proxy_count"] == 0
    assert inventory["workflow"]["grok_native_projection"] == "optional_unclaimed"


def test_ownership_manifest_has_w0_through_w7_and_immutable_agents() -> None:
    assert list(OMG_OWNER_PATTERNS) == [f"OMG-W{index}" for index in range(8)]
    assert OMG_OWNER_PATTERNS["OMG-W7"] == ()
    flattened = [pattern for patterns in OMG_OWNER_PATTERNS.values() for pattern in patterns]
    assert not any(Path(pattern.rstrip("/**")).name == "AGENTS.md" for pattern in flattened)


def test_inventory_mutations_fail_closed() -> None:
    value = load_json_object(INVENTORY)
    missing = copy.deepcopy(value)
    missing["requirement_ids"].pop()
    with pytest.raises(ContractValidationError, match="requirement"):
        validate_parity_inventory(missing)

    claimed_native = copy.deepcopy(value)
    claimed_native["workflow"]["grok_native_projection"] = "claimed"
    with pytest.raises(ContractValidationError, match="workflow"):
        validate_parity_inventory(claimed_native)


def test_inventory_checker_cli_is_bounded_and_structured() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_parity_inventory.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"requirements": 41' in result.stdout
    assert '"mcp_operations": 9' in result.stdout
