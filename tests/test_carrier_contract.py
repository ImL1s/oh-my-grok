from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.tracker_contract import (
    bind_native_spawn,
    make_role_receipt,
    parse_imported_carriers,
    validate_spawn_receipt,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "carrier"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _carrier_expectations() -> dict:
    return {
        "expected_parent_thread_id": "019f877b-9ac4-77f0-92b5-cc7aa9b90948",
        "expected_cwd_hash": "1" * 64,
        "expected_run_id": "dual-parity-fixture",
        "expected_session_id": "omx-fixture-session",
    }


def test_exact_adapted_agent_path_is_imported_comparison_not_authority() -> None:
    parsed = parse_imported_carriers(
        _load("valid-imported-codex-carrier.json"),
        declared_imported_evidence=True,
        now=datetime(2026, 7, 22, tzinfo=timezone.utc),
        **_carrier_expectations(),
    )
    assert parsed == {
        "provenance_kind": "imported_comparison",
        "role": "executor",
        "correlation_token": "0123456789abcdef0123456789abcdef",
        "authority": "none",
        "native_child_authorized": False,
    }
    with pytest.raises(ContractValidationError, match="declared imported evidence"):
        parse_imported_carriers(
            _load("valid-imported-codex-carrier.json"), declared_imported_evidence=False
        )


@pytest.mark.parametrize(
    "fixture,match",
    [
        ("mismatched-token.json", "disagree"),
        ("malformed-agent-path.json", "agent_path"),
        ("already-used.json", "already used"),
    ],
)
def test_imported_carrier_negative_mutations_fail_closed(fixture: str, match: str) -> None:
    with pytest.raises(ContractValidationError, match=match):
        parse_imported_carriers(
            _load(fixture),
            declared_imported_evidence=True,
            now=datetime(2026, 7, 22, tzinfo=timezone.utc),
            **_carrier_expectations(),
        )


def test_imported_carrier_requires_all_binding_replay_fields_and_expected_values() -> None:
    carrier = _load("valid-imported-codex-carrier.json")
    for field in (
        "correlation_token",
        "parent_thread_id",
        "cwd_hash",
        "run_id",
        "session_id",
        "expires_at",
        "used",
    ):
        missing = copy.deepcopy(carrier)
        missing.pop(field)
        with pytest.raises(ContractValidationError, match="missing binding/replay"):
            parse_imported_carriers(
                missing,
                declared_imported_evidence=True,
                now=datetime(2026, 7, 22, tzinfo=timezone.utc),
                **_carrier_expectations(),
            )
    for field in (
        "expected_parent_thread_id",
        "expected_cwd_hash",
        "expected_run_id",
        "expected_session_id",
    ):
        expectations = _carrier_expectations()
        expectations[field] = "2" * 64 if field == "expected_cwd_hash" else "wrong-binding"
        with pytest.raises(ContractValidationError, match="mismatch"):
            parse_imported_carriers(
                carrier,
                declared_imported_evidence=True,
                now=datetime(2026, 7, 22, tzinfo=timezone.utc),
                **expectations,
            )
    with pytest.raises(ContractValidationError, match="expected parent_thread_id"):
        parse_imported_carriers(
            carrier,
            declared_imported_evidence=True,
            now=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )


def _spawn() -> dict:
    return {
        "store_kind": "spawn_receipt",
        "schema_version": 1,
        "receipt_id": "receipt-1",
        "run_id": "run-1",
        "team_id": "team-1",
        "task_id": "task-1",
        "parent_id": "parent-1",
        "parent_session_id": "session-1",
        "requested_role": "executor",
        "capability_mode": "read-write",
        "depth": 1,
        "attempt": 1,
        "receipt_generation": 3,
        "lease_generation": 3,
        "dispatch_nonce": "nonce-1",
        "expires_at": "2099-07-22T01:00:00Z",
        "expected_state": "claimed",
        "expected_sequence": 7,
    }


def test_native_spawn_binding_requires_exact_receipt_role_and_generation() -> None:
    spawn = validate_spawn_receipt(_spawn())
    role = make_role_receipt(spawn)
    assert {
        "parent_session_id",
        "depth",
        "attempt",
        "lease_generation",
        "expected_state",
        "expected_sequence",
    } <= set(role)
    binding = bind_native_spawn(
        spawn,
        role,
        host_spawn_id="grok-child-1",
        observed_session_id="grok-session-1",
        expected_generation=3,
    )
    assert binding["identity_truth"] == "grok_native_receipts"
    assert binding["transition_sequence"] == 8
    assert bind_native_spawn(
        spawn,
        role,
        host_spawn_id="grok-child-1",
        observed_session_id="grok-session-1",
        expected_generation=3,
    ) == binding
    with pytest.raises(ContractValidationError, match="conflicting native IDs"):
        bind_native_spawn(
            spawn,
            role,
            host_spawn_id="grok-child-2",
            observed_session_id="grok-session-2",
            expected_generation=3,
        )

    mutated_role = copy.deepcopy(role)
    mutated_role["expected_sequence"] += 1
    with pytest.raises(ContractValidationError, match="role_receipt disagrees"):
        bind_native_spawn(
            spawn,
            mutated_role,
            host_spawn_id="grok-child-1",
            observed_session_id="grok-session-1",
            expected_generation=3,
        )

    stale = copy.deepcopy(spawn)
    stale["receipt_generation"] = 2
    with pytest.raises(ContractValidationError, match="stale"):
        bind_native_spawn(
            stale,
            make_role_receipt(stale),
            host_spawn_id="grok-child-1",
            observed_session_id="grok-session-1",
            expected_generation=3,
        )
    elevated = copy.deepcopy(spawn)
    elevated["capability_mode"] = "execute"
    with pytest.raises(ContractValidationError, match="read-only or read-write"):
        validate_spawn_receipt(elevated)
    nested = copy.deepcopy(spawn)
    nested["depth"] = 2
    with pytest.raises(ContractValidationError, match="exactly one"):
        validate_spawn_receipt(nested)
    expired = copy.deepcopy(spawn)
    expired["receipt_id"] = "receipt-expired"
    expired["expires_at"] = "2026-07-22T01:00:00Z"
    with pytest.raises(ContractValidationError, match="expired"):
        bind_native_spawn(
            expired,
            make_role_receipt(expired),
            host_spawn_id="grok-child-expired",
            observed_session_id="grok-session-expired",
            expected_generation=3,
        )
