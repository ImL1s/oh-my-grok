from __future__ import annotations

import copy
import json
from itertools import combinations
from pathlib import Path

import pytest

from omg_cli.contracts.capability_schema import claimed_tiers, validate_capability_record
from omg_cli.contracts.event_contract import append_lifecycle_event, validate_lifecycle_event
from omg_cli.contracts.path_keys import IMMUTABLE_SOURCE_MODE, mode_bits
from omg_cli.contracts.resume_contract import (
    RECOVERY_CAPS,
    RESUME_SELECTORS,
    fit_context_turns,
    omit_oversized_physical_lines,
    ordered_warnings,
    retain_newest_complete_turns,
    retain_newest_parsed_records,
    retain_newest_physical_lines,
    retain_source_suffix,
    select_resume_selector,
    validate_golden_recovery_counts,
    validate_recovery_manifest,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_git_oid,
    require_integer,
    require_iso8601,
    require_sha256,
)
from omg_cli.contracts.team_envelope import validate_worker_envelope


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(relative: str) -> dict:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def test_scalar_schemas_reject_bool_partial_hash_and_naive_time() -> None:
    assert require_git_oid("a" * 40, label="git") == "a" * 40
    assert require_git_oid("b" * 64, label="git") == "b" * 64
    assert require_sha256("c" * 64, label="digest") == "c" * 64
    assert require_iso8601("2026-07-22T00:00:00Z", label="time").endswith("Z")
    with pytest.raises(ContractValidationError):
        require_integer(True, label="integer")
    with pytest.raises(ContractValidationError):
        require_git_oid("a" * 12, label="git")
    with pytest.raises(ContractValidationError):
        require_iso8601("2026-07-22T00:00:00", label="time")


def test_capability_tiers_remain_independent_evidence() -> None:
    record = validate_capability_record(_load("capabilities/independent-tier-evidence.json"))
    assert claimed_tiers(record) == ["configured", "enabled", "observed"]
    assert record["configured"] is True and record["installed"] is False
    bad = dict(record)
    bad["verified"] = 1
    with pytest.raises(ContractValidationError, match="independent boolean"):
        validate_capability_record(bad)
    for diagnostic in (
        "Authorization: Bearer raw-secret-token",
        "Cookie: session=raw-cookie",
        "TOKEN=raw-token",
        "https://example.test/?api_key=raw-key",
    ):
        leaked = dict(record)
        leaked["redacted_diagnostic"] = diagnostic
        with pytest.raises(ContractValidationError, match="unredacted credential"):
            validate_capability_record(leaked)
    redacted = dict(record)
    redacted["redacted_diagnostic"] = "Authorization: Bearer [REDACTED]"
    assert validate_capability_record(redacted) == redacted


def test_golden_partial_recovery_fixture_locks_caps_counts_and_warnings() -> None:
    path = FIXTURES / "recovery" / "bounded-900-lines-broken-chain-v1.jsonl"
    manifest = validate_recovery_manifest(
        _load("recovery/bounded-900-lines-broken-chain-v1.manifest.json")
    )
    assert RECOVERY_CAPS["source_bytes"] == 16_777_216
    assert len(path.read_bytes().splitlines()) == 900
    assert mode_bits(path) == IMMUTABLE_SOURCE_MODE
    validate_golden_recovery_counts(manifest["counters"], manifest["warnings"])
    assert manifest["partial"] is True

    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    unknown = [row for row in rows if row["type"].startswith("future_")]
    complete = [row for row in rows if row["type"] == "turn_end"]
    assert len(unknown) == 3
    assert len(complete) == 124
    assert rows[-1]["payload"]["truncated"] is True


def test_every_recovery_cap_has_below_exact_and_plus_one_oracle() -> None:
    source_cap = RECOVERY_CAPS["source_bytes"]
    for size in (source_cap - 1, source_cap):
        result = retain_source_suffix(b"x" * size)
        assert len(result["retained"]) == size and result["warnings"] == []
    source_over = retain_source_suffix(b"x\n" + b"a" * (source_cap - 1))
    assert source_over["source_bytes_total"] == source_cap + 1
    assert source_over["source_prefix_bytes_omitted"] == 1
    assert source_over["leading_fragment_bytes_omitted"] == 1
    assert source_over["retained"] == b"a" * (source_cap - 1)
    assert source_over["warnings"] == ["W_TRUNCATED_SOURCE"]

    line_cap = RECOVERY_CAPS["physical_line_bytes"]
    for size in (line_cap - 1, line_cap):
        result = omit_oversized_physical_lines([b"x" * size])
        assert result["oversized_lines_omitted"] == 0
    line_over = omit_oversized_physical_lines([b"x" * (line_cap + 1)])
    assert line_over["retained"] == []
    assert line_over["oversized_lines_omitted"] == 1
    assert len(line_over["omitted_hashes"]) == 1

    physical_cap = RECOVERY_CAPS["physical_lines"]
    for count in (physical_cap - 1, physical_cap, physical_cap + 1):
        result = retain_newest_physical_lines([b"{}"] * count)
        assert result["physical_lines_retained"] == min(count, physical_cap)
        assert result["physical_lines_omitted_oldest"] == max(0, count - physical_cap)

    parsed_cap = RECOVERY_CAPS["parsed_records"]
    for count in (parsed_cap - 1, parsed_cap, parsed_cap + 1):
        records = [
            {"record_class": "unknown" if index == 0 else "recognized", "index": index}
            for index in range(count)
        ]
        result = retain_newest_parsed_records(records)
        assert result["parsed_records_retained"] == min(count, parsed_cap)
        assert result["parsed_records_omitted_oldest"] == max(0, count - parsed_cap)
        if count == parsed_cap + 1:
            assert result["unknown_records_seen"] == 1
            assert result["unknown_records_retained"] == 0

    turn_cap = RECOVERY_CAPS["complete_turns"]
    for count in (turn_cap - 1, turn_cap, turn_cap + 1):
        result = retain_newest_complete_turns(list(range(count)))
        assert result["complete_turns_retained"] == min(count, turn_cap)
        assert result["complete_turns_omitted_oldest"] == max(0, count - turn_cap)
    with pytest.raises(ContractValidationError, match="E_RESUME_NO_COMPLETE_TURNS"):
        retain_newest_complete_turns([])

    context_cap = RECOVERY_CAPS["context_bytes"]
    for size in (context_cap - 1, context_cap):
        result = fit_context_turns([b"x" * size])
        assert result["context_bytes_after"] == size
    with pytest.raises(ContractValidationError, match="E_RESUME_CONTEXT_OVER_CAP"):
        fit_context_turns([b"x" * (context_cap + 1)])
    dropped = fit_context_turns([b"old", b"x" * context_cap])
    assert dropped["retained"] == [b"x" * context_cap]
    assert dropped["context_turns_omitted_oldest"] == 1
    assert ordered_warnings(
        [
            "W_UNKNOWN_RECORD_TYPE",
            "W_CONTEXT_TRUNCATED",
            "W_TRUNCATED_SOURCE",
            "W_TURNS_TRUNCATED",
            "W_PARSED_RECORDS_TRUNCATED",
            "W_CONTEXT_TRUNCATED",
        ]
    ) == [
        "W_TRUNCATED_SOURCE",
        "W_PARSED_RECORDS_TRUNCATED",
        "W_TURNS_TRUNCATED",
        "W_CONTEXT_TRUNCATED",
        "W_UNKNOWN_RECORD_TYPE",
    ]


def test_recovery_manifest_validator_rejects_retained_and_context_cap_plus_one() -> None:
    manifest = _load("recovery/bounded-900-lines-broken-chain-v1.manifest.json")
    too_many_lines = copy.deepcopy(manifest)
    too_many_lines["counters"]["physical_lines_retained"] = RECOVERY_CAPS["physical_lines"] + 1
    with pytest.raises(ContractValidationError, match="physical_lines_retained exceeds"):
        validate_recovery_manifest(too_many_lines)
    too_much_context = copy.deepcopy(manifest)
    too_much_context["counters"]["context_bytes_after"] = RECOVERY_CAPS["context_bytes"] + 1
    with pytest.raises(ContractValidationError, match="context_bytes_after exceeds"):
        validate_recovery_manifest(too_much_context)


def test_resume_selector_locks_all_six_ranks_and_run_native_compound() -> None:
    assert RESUME_SELECTORS == (
        "recovery_manifest",
        "run_id",
        "native_session_id",
        "current_process_run",
        "signed_handoff",
        "best_effort_cwd",
    )
    values = {
        "recovery_manifest": "recovery.json",
        "run_id": "run-1",
        "native_session_id": "session-1",
        "current_process_run": "run-current",
        "signed_handoff": "handoff-token",
        "best_effort_cwd": True,
    }
    for selector in RESUME_SELECTORS:
        assert select_resume_selector(
            {selector: values[selector]},
            best_effort=selector == "best_effort_cwd",
        ) == selector

    assert select_resume_selector(
        {"run_id": "run-1", "native_session_id": "session-1"}
    ) == "run_id"


def test_resume_selector_rejects_every_other_pair_and_compound_extension() -> None:
    values = {
        "recovery_manifest": "recovery.json",
        "run_id": "run-1",
        "native_session_id": "session-1",
        "current_process_run": "run-current",
        "signed_handoff": "handoff-token",
        "best_effort_cwd": True,
    }
    allowed = {"run_id", "native_session_id"}
    for first, second in combinations(RESUME_SELECTORS, 2):
        if {first, second} == allowed:
            continue
        with pytest.raises(ContractValidationError, match="E_RESUME_SELECTOR_CONFLICT"):
            select_resume_selector(
                {first: values[first], second: values[second]},
                best_effort=True,
            )

    for third in set(RESUME_SELECTORS) - allowed:
        with pytest.raises(ContractValidationError, match="E_RESUME_SELECTOR_CONFLICT"):
            select_resume_selector(
                {
                    "run_id": values["run_id"],
                    "native_session_id": values["native_session_id"],
                    third: values[third],
                },
                best_effort=True,
            )


def test_resume_selector_ignores_absent_values_and_gates_best_effort() -> None:
    assert select_resume_selector(
        {
            "run_id": "run-1",
            "native_session_id": None,
            "current_process_run": False,
            "signed_handoff": "",
            "best_effort_cwd": [],
        }
    ) == "run_id"
    with pytest.raises(ContractValidationError, match="E_RESUME_NOT_FOUND"):
        select_resume_selector({"unknown_selector": "value"})
    with pytest.raises(ContractValidationError, match="best-effort"):
        select_resume_selector({"best_effort_cwd": True})


def test_lifecycle_append_and_worker_envelope_are_bounded(tmp_path: Path) -> None:
    event = {
        "store_kind": "normalized_lifecycle_event",
        "schema_version": 1,
        "source": "grok",
        "source_cursor": "cursor-1",
        "source_sequence": 0,
        "event_id": "event-1",
        "event_type": "turn_completed",
        "run_id": "run-1",
        "session_id": "session-1",
        "observed_at": "2026-07-22T00:00:00Z",
        "payload": {"status": "complete"},
    }
    assert validate_lifecycle_event(event)["event_type"] == "turn_completed"
    digest = append_lifecycle_event(tmp_path / "events.jsonl", event)
    assert len(digest) == 64
    assert append_lifecycle_event(tmp_path / "events.jsonl", event) == digest
    assert len((tmp_path / "events.jsonl").read_bytes().splitlines()) == 1
    collision = copy.deepcopy(event)
    collision["payload"] = {"status": "different"}
    with pytest.raises(ContractValidationError, match="identity collision"):
        append_lifecycle_event(tmp_path / "events.jsonl", collision)
    assert len((tmp_path / "events.jsonl").read_bytes().splitlines()) == 1

    envelope = {
        "store_kind": "worker_envelope",
        "schema_version": 1,
        "run_id": "run-1",
        "team_id": "team-1",
        "task_id": "task-1",
        "parent_task_id": None,
        "dependencies": [],
        "dependency_results": {},
        "prompt": "inspect repository",
        "requested_role": "explore",
        "capability_mode": "read-only",
        "depth": 1,
        "write_scope": [],
        "verification_commands": [["python3", "-m", "pytest", "-q"]],
        "artifact_contract": {"kind": "report"},
        "guidance_hashes": {"AGENTS.md": "d" * 64},
        "mailbox_cursor": "cursor-0",
        "claim_generation": 1,
        "state_endpoint": ".omg/state/runs/run-1.json",
        "cancellation_token": "cancel-1",
        "expected_state": "claimed",
        "expected_sequence": 4,
    }
    assert validate_worker_envelope(envelope)["depth"] == 1
    invalid_depth = copy.deepcopy(envelope)
    invalid_depth["depth"] = 2
    with pytest.raises(ContractValidationError, match="exactly one"):
        validate_worker_envelope(invalid_depth)
    empty_artifact = copy.deepcopy(envelope)
    empty_artifact["artifact_contract"] = {}
    with pytest.raises(ContractValidationError, match="artifact_contract key mismatch"):
        validate_worker_envelope(empty_artifact)
    w6_authority = copy.deepcopy(envelope)
    w6_authority["capability_mode"] = "read-write"
    w6_authority["write_scope"] = [
        ".omg/artifacts/dual-parity/run-1/OMG-W6/aggregate-handoff.json"
    ]
    with pytest.raises(ContractValidationError, match="canonical authority"):
        validate_worker_envelope(w6_authority)
