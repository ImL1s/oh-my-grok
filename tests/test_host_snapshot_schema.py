"""Host-baseline snapshot schema validation (#105 PR1)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import (
    FROZEN_PINS,
    HOST_BASELINE_PIN_ID,
    load_json_object,
    validate_host_baseline_snapshot,
)
from omg_cli.contracts.state_schemas import ContractValidationError

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "parity" / "upstream-snapshots" / "grok-build.json"


def _fixture_snapshot() -> dict:
    return load_json_object(SNAPSHOT)


def test_canonical_host_snapshot_schema_valid() -> None:
    snapshot = validate_host_baseline_snapshot(_fixture_snapshot())
    assert snapshot["host_id"] == HOST_BASELINE_PIN_ID
    assert snapshot["public_commit"] == FROZEN_PINS[HOST_BASELINE_PIN_ID]
    assert snapshot["release"] == "0.2.121"
    assert snapshot["source_revision"] == "4d6d11372ab8f73026a78c45a7b7e7b1310eb39f"


def test_host_catalogue_classifications_complete() -> None:
    snapshot = validate_host_baseline_snapshot(_fixture_snapshot())
    classes = {cap["classification"] for cap in snapshot["capabilities"]}
    assert classes == {"host_owned", "consumed_downstream", "irrelevant"}
    for cap in snapshot["capabilities"]:
        assert cap["owner"] == "host"
        assert cap["runtime"] == "grok"
        assert cap["maturity"] == "catalogued"


def test_host_owned_rejects_omg_paths() -> None:
    snapshot = _fixture_snapshot()
    for cap in snapshot["capabilities"]:
        if cap["classification"] == "host_owned":
            cap["omg_paths"] = ["omg_cli/team/__init__.py"]
            break
    else:
        pytest.fail("expected at least one host_owned capability")
    with pytest.raises(ContractValidationError, match="omg_paths"):
        validate_host_baseline_snapshot(snapshot)


def test_host_snapshot_rejects_live_verified_maturity() -> None:
    snapshot = _fixture_snapshot()
    snapshot["capabilities"][0]["maturity"] = "live_verified"
    snapshot["capabilities"][0]["status"] = "live_verified"
    with pytest.raises(ContractValidationError, match="live_verified"):
        validate_host_baseline_snapshot(snapshot)


def test_host_snapshot_rejects_unknown_classification() -> None:
    snapshot = _fixture_snapshot()
    snapshot["capabilities"][0]["classification"] = "faithful"
    with pytest.raises(ContractValidationError, match="classification"):
        validate_host_baseline_snapshot(snapshot)


def test_host_snapshot_rejects_malformed_missing_keys(tmp_path: Path) -> None:
    bad = {"store_kind": "host_baseline_snapshot", "schema_version": 1}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="key mismatch|host_baseline"):
        validate_host_baseline_snapshot(load_json_object(path))


def test_host_snapshot_requires_nonempty_capabilities() -> None:
    snapshot = _fixture_snapshot()
    snapshot["capabilities"] = []
    with pytest.raises(ContractValidationError, match="capabilities"):
        validate_host_baseline_snapshot(snapshot)


def test_host_snapshot_duplicate_ids_rejected() -> None:
    snapshot = _fixture_snapshot()
    snapshot["capabilities"].append(copy.deepcopy(snapshot["capabilities"][0]))
    with pytest.raises(ContractValidationError, match="duplicate"):
        validate_host_baseline_snapshot(snapshot)
