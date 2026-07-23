from __future__ import annotations

import copy

import pytest

from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import (
    FINAL_AGGREGATE_DOMAIN,
    HANDOFF_DOMAIN,
    INPUT_AGGREGATE_DOMAIN,
    PARENT_HASH_ORACLE,
    CanonicalJSONError,
    VerifiedParentHashes,
    canonical_json_bytes,
    expected_parent_waves,
    handoff_hash,
    parse_canonical_json_bytes,
    sign_final_aggregate,
    sign_handoff,
    sign_input_aggregate,
    validate_parent_hashes,
    validate_path_record,
    validate_w6_request_bindings,
    verify_aggregate_envelope,
    verify_handoff,
)


def test_canonical_json_v1_is_integer_only_compact_utf8_and_roundtrips() -> None:
    body = canonical_json_bytes({"é": [True, None, 3], "a": "文字"})
    assert body == '{"a":"文字","é":[true,null,3]}'.encode()
    assert not body.endswith(b"\n") and not body.startswith(b"\xef\xbb\xbf")
    assert parse_canonical_json_bytes(body) == {"a": "文字", "é": [True, None, 3]}
    for value in ({"float": 1.0}, {1: "non-string"}, {"surrogate": "\ud800"}):
        with pytest.raises(CanonicalJSONError):
            canonical_json_bytes(value)
    with pytest.raises(CanonicalJSONError):
        parse_canonical_json_bytes(body + b"\n")
    with pytest.raises(CanonicalJSONError, match="duplicate"):
        parse_canonical_json_bytes(b'{"a":1,"a":2}')


def test_parent_hash_oracle_is_exact_and_completion_order_is_rejected() -> None:
    assert PARENT_HASH_ORACLE == {
        "W0": (),
        "W1": ("W0",),
        "W2": ("W0",),
        "W3": ("W2",),
        "W4": ("W1", "W2"),
        "W5": ("W3", "W4"),
        "W6": ("W0", "W1", "W2", "W3", "W4", "W5"),
        "W7": ("W6",),
    }
    assert expected_parent_waves("OMG-W4") == ["OMG-W1", "OMG-W2"]
    trusted = VerifiedParentHashes({"OMG-W1": "1" * 64, "OMG-W2": "2" * 64})
    assert validate_parent_hashes("OMG-W4", ["1" * 64, "2" * 64], trusted)
    with pytest.raises(ContractValidationError, match="mismatch"):
        validate_parent_hashes("OMG-W4", ["2" * 64, "1" * 64], trusted)
    with pytest.raises(ContractValidationError, match="verified same-run"):
        validate_parent_hashes(
            "OMG-W4",
            ["1" * 64, "2" * 64],
            {"OMG-W1": "1" * 64, "OMG-W2": "2" * 64},
        )
    with pytest.raises(ContractValidationError, match="W0"):
        validate_parent_hashes(
            "OMG-W0", ["1" * 64], VerifiedParentHashes({"OMG-W1": "1" * 64})
        )


def test_handoff_hmac_binds_repo_run_wave_owner_parents_and_domain() -> None:
    key = b"k" * 32
    path_record = {
        "repository_id": "OMG",
        "run_id": "run-1",
        "wave": "OMG-W0",
        "owner": "omg-contract-owner",
        "path": "omg_cli/contracts/writer_chain.py",
        "initial_sha256": "ABSENT",
        "final_sha256": "1" * 64,
        "reason": "introduce frozen writer contract",
        "proposal_id": "proposal-1",
        "proposal_hash": "a" * 64,
        "targeted_test": {
            "argv": ["python3", "-m", "pytest", "-q", "tests/test_writer_chain.py"],
            "rc": 0,
            "stdout_sha256": "3" * 64,
            "stderr_sha256": "4" * 64,
        },
    }
    payload = {
        "store_kind": "owner_handoff",
        "schema_version": 1,
        "repository_id": "OMG",
        "run_id": "run-1",
        "wave": "OMG-W0",
        "owner": "omg-contract-owner",
        "run_manifest_path": ".omg/state/runs/run-1/run-manifest.json",
        "run_manifest_revision": 2,
        "run_manifest_hash": "5" * 64,
        "frozen_base_commit": "6" * 40,
        "frozen_base_tree": "7" * 40,
        "lease_generation": 2,
        "proposal_index_path": ".omg/artifacts/dual-parity/run-1/OMG-W0/proposal-index.json",
        "parent_handoff_hashes": [],
        "proposal_index_hash": "a" * 64,
        "path_records": [path_record],
        "created_at": "2026-07-22T00:00:00Z",
    }
    envelope = sign_handoff(payload, key)
    digest = verify_handoff(
        envelope,
        key,
        expected_repository="OMG",
        expected_run_id="run-1",
        expected_wave="OMG-W0",
        expected_owner="omg-contract-owner",
        trusted_parent_hashes={},
    )
    assert digest == handoff_hash(envelope)
    assert HANDOFF_DOMAIN.endswith(b"\0")

    tampered = copy.deepcopy(envelope)
    tampered["signed_payload"]["run_id"] = "run-2"
    with pytest.raises(ContractValidationError, match="signature"):
        verify_handoff(
            tampered,
            key,
            expected_repository="OMG",
            expected_run_id="run-1",
            expected_wave="OMG-W0",
            expected_owner="omg-contract-owner",
            trusted_parent_hashes={},
        )
    foreign_payload = copy.deepcopy(payload)
    foreign_payload["run_id"] = "run-2"
    foreign_payload["run_manifest_path"] = ".omg/state/runs/run-2/run-manifest.json"
    foreign_payload["proposal_index_path"] = (
        ".omg/artifacts/dual-parity/run-2/OMG-W0/proposal-index.json"
    )
    foreign_payload["path_records"][0]["run_id"] = "run-2"
    foreign = sign_handoff(foreign_payload, key)
    with pytest.raises(ContractValidationError, match="foreign run"):
        verify_handoff(
            foreign,
            key,
            expected_repository="OMG",
            expected_run_id="run-1",
            expected_wave="OMG-W0",
            expected_owner="omg-contract-owner",
            trusted_parent_hashes={},
        )
    with pytest.raises(ContractValidationError, match="signature"):
        verify_handoff(
            envelope,
            b"x" * 32,
            expected_repository="OMG",
            expected_run_id="run-1",
            expected_wave="OMG-W0",
            expected_owner="omg-contract-owner",
            trusted_parent_hashes={},
        )


def test_path_record_binds_initial_final_proposal_and_targeted_test() -> None:
    record = {
        "repository_id": "OMG",
        "run_id": "run-1",
        "wave": "OMG-W0",
        "owner": "omg-contract-owner",
        "path": "omg_cli/contracts/writer_chain.py",
        "initial_sha256": "ABSENT",
        "final_sha256": "1" * 64,
        "reason": "introduce frozen writer contract",
        "proposal_id": "proposal-1",
        "proposal_hash": "2" * 64,
        "targeted_test": {
            "argv": ["python3", "-m", "pytest", "-q", "tests/test_writer_chain.py"],
            "rc": 0,
            "stdout_sha256": "3" * 64,
            "stderr_sha256": "4" * 64,
        },
    }
    assert validate_path_record(record)["initial_sha256"] == "ABSENT"
    bad = copy.deepcopy(record)
    bad["path"] = "../escape.py"
    with pytest.raises(ContractValidationError, match="repository-relative"):
        validate_path_record(bad)


def test_w6_request_bindings_are_exact_confined_unique_and_byte_sorted() -> None:
    first = {
        "path": ".omg/artifacts/dual-parity/run-1/OMG-W4/a-request.json",
        "byte_length": 17,
        "sha256": "1" * 64,
    }
    second = {
        "path": ".omg/artifacts/dual-parity/run-1/OMG-W4/b-request.json",
        "byte_length": 23,
        "sha256": "2" * 64,
    }
    assert validate_w6_request_bindings(
        [first, second],
        repository_id="OMG",
        run_id="run-1",
        wave="OMG-W4",
    ) == [first, second]
    assert (
        validate_w6_request_bindings(
            [], repository_id="OMG", run_id="run-1", wave="OMG-W0"
        )
        == []
    )

    invalid_cases = [
        ([{**first, "extra": True}], "key mismatch"),
        ([{**first, "byte_length": True}], "must be an integer"),
        ([second, first], "sorted"),
        ([first, first], "duplicate"),
        ([{**first, "path": "../escape.json"}], "repository-relative"),
        (
            [
                {
                    **first,
                    "path": ".omg/artifacts/dual-parity/other-run/OMG-W4/a-request.json",
                }
            ],
            "confined",
        ),
        (
            [
                {
                    **first,
                    "path": ".omg/artifacts/dual-parity/run-1/OMG-W3/a-request.json",
                }
            ],
            "confined",
        ),
    ]
    for bindings, message in invalid_cases:
        with pytest.raises(ContractValidationError, match=message):
            validate_w6_request_bindings(
                bindings,
                repository_id="OMG",
                run_id="run-1",
                wave="OMG-W4",
            )


def test_input_and_final_aggregates_use_distinct_domains_and_exact_six_roots() -> None:
    key = bytes(range(32))
    roots = [{"wave": f"OMG-W{i}", "handoff_hash": str(i) * 64} for i in range(6)]
    input_payload = {
        "repository_id": "OMG",
        "run_id": "run-1",
        "ordered_owner_roots": roots,
        "final_commit": None,
    }
    input_envelope = sign_input_aggregate(input_payload, key, repository="OMG")
    input_hash = verify_aggregate_envelope(
        input_envelope, key, repository="OMG", kind="input"
    )
    assert input_hash == input_envelope["payload_hash"]
    assert INPUT_AGGREGATE_DOMAIN != FINAL_AGGREGATE_DOMAIN
    with pytest.raises(ContractValidationError, match="signature"):
        verify_aggregate_envelope(input_envelope, key, repository="OMG", kind="final")

    final_payload = {
        "repository_id": "OMG",
        "run_id": "run-1",
        "input_envelope": input_envelope,
        "final_commit": "a" * 40,
        "final_tree": "b" * 40,
        "pushed_oid": "a" * 40,
        "complete_delta_root": "c" * 64,
        "semver": "1.2.3",
        "release_nonce": "nonce-1",
        "release_bundle_manifest_path": ".omg/artifacts/dual-parity/run-1/OMG-W6/release-bundle-manifest.json",
        "release_bundle_manifest_sha256": "d" * 64,
        "release_bundle_manifest_schema": "release_bundle_manifest/1",
        "public_upload_order": ["oh-my-grok-1.2.3.tar.gz", "SHA256SUMS"],
        "release_asset_root": "e" * 64,
    }
    final_envelope = sign_final_aggregate(final_payload, key, repository="OMG")
    assert (
        verify_aggregate_envelope(final_envelope, key, repository="OMG", kind="final")
        == final_envelope["payload_hash"]
    )
    with pytest.raises(ContractValidationError, match="cross-repository"):
        verify_aggregate_envelope(final_envelope, key, repository="OMA", kind="final")

    bad = copy.deepcopy(input_payload)
    bad["ordered_owner_roots"] = roots[::-1]
    with pytest.raises(ContractValidationError, match="ordered W0 through W5"):
        sign_input_aggregate(bad, key, repository="OMG")
