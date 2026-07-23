from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import (
    OMG_OWNER_PATTERNS,
    REQUIREMENT_ID_SET,
    load_json_object,
    validate_traceability,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import owner_for_path


ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "docs" / "parity" / "omg-traceability.json"


def test_traceability_has_one_entry_per_frozen_requirement() -> None:
    trace = validate_traceability(load_json_object(TRACE))
    assert trace["requirement_ids"] == list(REQUIREMENT_ID_SET)
    assert len(trace["entries"]) == 41

    for entry in trace["entries"]:
        for path in [*entry["code_paths"], *entry["test_paths"]]:
            owner = owner_for_path(path, OMG_OWNER_PATTERNS)
            assert owner in entry["waves"], (entry["requirement_id"], path, owner)


def test_traceability_rejects_unknown_wave_and_unowned_path() -> None:
    trace = load_json_object(TRACE)
    bad_wave = copy.deepcopy(trace)
    bad_wave["entries"][0]["waves"] = ["OMG-W9"]
    with pytest.raises(ContractValidationError, match="unknown OMG wave"):
        validate_traceability(bad_wave)

    with pytest.raises(ContractValidationError, match="exactly one owner"):
        owner_for_path("unowned/future.txt", OMG_OWNER_PATTERNS)


def test_traceability_checker_cli_verifies_owned_path_references() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_traceability.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"requirements": 41' in result.stdout
    assert '"owned_path_references": 82' in result.stdout
