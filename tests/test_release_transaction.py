from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from omg_cli.contracts.release_transaction import (
    GITHUB_CHANNEL_STATE_SET,
    GITHUB_LATEST_RESTORE_STATE_SET,
    OMA_REGISTRY_STATE_SUFFIX_SET,
    PRODUCTION_REGISTRY_IDS,
    allowed_release_states,
    classify_external_observation,
    expected_bundle_manifest_relative_path,
    expected_three_registry_withdrawal_vectors,
    make_call_record,
    registry_cleanup_disposition_key,
    release_state_is_success,
    release_transaction_identity_hash,
    validate_call_record,
    validate_claimed_registries,
    validate_cleanup_dispositions,
    validate_registry_policy,
    validate_release_bundle_manifest,
    validate_release_completion_evidence,
    validate_release_state_name,
    verify_release_bundle_files,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "release"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_release_state_sets_and_registry_policy_are_exact() -> None:
    assert len(OMA_REGISTRY_STATE_SUFFIX_SET) == 41
    assert OMA_REGISTRY_STATE_SUFFIX_SET[-1] == "deprecation_not_applicable"
    assert "github_promotion_pending" in GITHUB_CHANNEL_STATE_SET
    assert "github_latest_restore_readback_passed" in GITHUB_LATEST_RESTORE_STATE_SET
    policies = _load("production-registry-policies.json")
    assert tuple(row["registry_id"] for row in policies) == PRODUCTION_REGISTRY_IDS
    assert all(validate_registry_policy(row) == row for row in policies)
    with pytest.raises(ContractValidationError, match="scalar"):
        validate_release_state_name("package_publish_pending")


def test_every_frozen_nonterminal_failure_or_ambiguity_state_withholds_success() -> None:
    assets = ["oh-my-grok-1.2.3.tar.gz", "SHA256SUMS"]
    registries = ["github-packages", "npmjs"]
    states = allowed_release_states(registries, assets)
    assert release_state_is_success(
        "complete", registry_ids=registries, asset_names=assets
    )
    for state in states - {"complete"}:
        assert not release_state_is_success(
            state,
            registry_ids=registries,
            asset_names=assets,
        ), state
    assert any(state.endswith("_pending") for state in states)
    assert any(state.endswith("_unknown") for state in states)
    assert any(state.endswith("_failed") for state in states)
    assert any("blocked" in state or "conflict" in state for state in states)


def test_external_observation_cardinality_and_failure_flags_are_fail_closed() -> None:
    assert classify_external_observation(0) == "absent"
    assert classify_external_observation(1) == "exact"
    assert classify_external_observation(2) == "ambiguous"
    assert classify_external_observation(0, timed_out=True) == "unknown"
    assert classify_external_observation(0, call_failed=True) == "failed"
    for kwargs in (
        {"result_count": -1},
        {"result_count": 0, "timed_out": 1},
        {"result_count": 0, "timed_out": True, "call_failed": True},
        {"result_count": 1, "timed_out": True},
        {"result_count": 1, "call_failed": True},
    ):
        with pytest.raises(ContractValidationError):
            classify_external_observation(**kwargs)


def test_claimed_registries_freeze_order_tag_bytes_and_provenance() -> None:
    semver = "1.2.3"
    identity = release_transaction_identity_hash("OMA", semver, "a" * 40, "nonce-1")
    claimed = []
    for policy in _load("production-registry-policies.json"):
        claimed.append(
            {
                **policy,
                "tarball_sha256": "b" * 64,
                "integrity": "sha512-fixture",
                "provenance_hash": "c" * 64,
                "staging_dist_tag": f"oma-prerelease-{identity[:12]}",
                "prior_final_tag_identity": "version:1.2.2",
            }
        )
    assert validate_claimed_registries(
        claimed, semver=semver, transaction_identity_hash=identity
    ) == claimed
    with pytest.raises(ContractValidationError, match="frozen production order"):
        validate_claimed_registries(
            claimed[::-1], semver=semver, transaction_identity_hash=identity
        )
    latest = copy.deepcopy(claimed)
    latest[0]["staging_dist_tag"] = "latest"
    with pytest.raises(ContractValidationError, match="staging_dist_tag"):
        validate_claimed_registries(latest, semver=semver, transaction_identity_hash=identity)


def test_w6_bundle_manifest_binds_exact_prebuilt_file_set(tmp_path: Path) -> None:
    manifest = _load("valid-omg-release-bundle-manifest.json")
    relative = expected_bundle_manifest_relative_path("OMG", "fixture-run")
    validate_release_bundle_manifest(manifest, manifest_relative_path=relative)
    bundle = tmp_path / manifest["bundle_directory"]
    bundle.mkdir(parents=True)
    (bundle / manifest["assets"][0]["name"]).write_bytes((FIXTURES / "payload.bytes").read_bytes())
    (bundle / "SHA256SUMS").write_bytes((FIXTURES / "SHA256SUMS").read_bytes())
    assert verify_release_bundle_files(
        tmp_path, manifest, manifest_relative_path=relative
    )["release_asset_root"] == manifest["release_asset_root"]

    (bundle / "SHA256SUMS").unlink()
    with pytest.raises(ContractValidationError, match="missing/extra/renamed"):
        verify_release_bundle_files(tmp_path, manifest, manifest_relative_path=relative)
    (bundle / "SHA256SUMS").write_bytes((FIXTURES / "SHA256SUMS").read_bytes())

    payload = bundle / manifest["assets"][0]["name"]
    renamed = bundle / "renamed.tar.gz"
    payload.rename(renamed)
    with pytest.raises(ContractValidationError, match="missing/extra/renamed"):
        verify_release_bundle_files(tmp_path, manifest, manifest_relative_path=relative)
    renamed.rename(payload)

    payload_bytes = payload.read_bytes()
    checksum_bytes = (bundle / "SHA256SUMS").read_bytes()
    payload.write_bytes(checksum_bytes)
    (bundle / "SHA256SUMS").write_bytes(payload_bytes)
    with pytest.raises(ContractValidationError, match="byte drift"):
        verify_release_bundle_files(tmp_path, manifest, manifest_relative_path=relative)
    payload.write_bytes(payload_bytes)
    (bundle / "SHA256SUMS").write_bytes(checksum_bytes)

    (bundle / manifest["assets"][0]["name"]).write_bytes(b"rebuilt bytes")
    with pytest.raises(ContractValidationError, match="byte drift"):
        verify_release_bundle_files(tmp_path, manifest, manifest_relative_path=relative)
    (bundle / manifest["assets"][0]["name"]).write_bytes((FIXTURES / "payload.bytes").read_bytes())
    (bundle / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="missing/extra/renamed"):
        verify_release_bundle_files(tmp_path, manifest, manifest_relative_path=relative)

    order_drift = copy.deepcopy(manifest)
    order_drift["public_upload_order"].reverse()
    with pytest.raises(ContractValidationError, match="upload order"):
        validate_release_bundle_manifest(order_drift, manifest_relative_path=relative)

    checksum_drift = copy.deepcopy(manifest)
    checksum_drift["checksum"]["payload_sha256"] = "0" * 64
    with pytest.raises(ContractValidationError, match="differs from asset"):
        validate_release_bundle_manifest(checksum_drift, manifest_relative_path=relative)
    receipt_drift = copy.deepcopy(manifest)
    receipt_drift["build_receipt"]["argv"].append("--rebuild")
    with pytest.raises(ContractValidationError, match="receipt hash mismatch"):
        validate_release_bundle_manifest(receipt_drift, manifest_relative_path=relative)
    media_drift = copy.deepcopy(manifest)
    media_drift["assets"][0]["media_type"] = "application/octet-stream"
    media_drift["release_asset_root"] = sha256_hex(canonical_json_bytes(media_drift["assets"]))
    with pytest.raises(ContractValidationError, match="media type mismatch"):
        validate_release_bundle_manifest(media_drift, manifest_relative_path=relative)


def test_release_completion_evidence_binds_manifest_transaction_and_readbacks() -> None:
    repository = "OMG"
    run_id = "release-run"
    semver = "1.2.3"
    frozen_commit = "a" * 40
    nonce = "release-nonce"
    assets = ["oh-my-grok-1.2.3.tar.gz", "SHA256SUMS"]
    states = [
        "branch_readback_passed",
        "commit_proof_passed",
        "tag_readback_passed",
        "prerelease_readback_passed",
        *(f"asset-{sha256_hex(name)[:16]}.asset_readback_passed" for name in assets),
        "assets_readback_passed",
        "github_promotion_readback_passed",
        "github_latest_readback_passed",
        "verified_readback_passed",
        "final_readback_passed",
        "complete",
    ]
    records = []
    predecessor = "candidate_gates_passed"
    for index, state in enumerate(states):
        record = make_call_record(
            repository=repository,
            semver=semver,
            frozen_commit=frozen_commit,
            transaction_nonce=nonce,
            step=f"step-{index}",
            state=state,
            allowed_predecessor=predecessor,
            attempt=1,
            redacted_external_locator="github:fixture",
            expected_identity={"state": state},
            expected_byte_digest=None,
            request={"state": state},
            prior_mutable_identity=None,
        )
        if state.endswith("_readback_passed"):
            record["object_digest"] = sha256_hex(state)
            record["readback_at"] = "2026-07-23T00:00:00Z"
        records.append(record)
        predecessor = state
    evidence = {
        "store_kind": "release_completion_evidence",
        "schema_version": 1,
        "repository_id": repository,
        "run_id": run_id,
        "semver": semver,
        "frozen_commit": frozen_commit,
        "transaction_nonce": nonce,
        "transaction_identity_hash": release_transaction_identity_hash(
            repository, semver, frozen_commit, nonce
        ),
        "release_active_manifest_sha256": "b" * 64,
        "release_bundle_manifest_sha256": "c" * 64,
        "final_state": "complete",
        "call_records": records,
        "verified_at": "2026-07-23T00:01:00Z",
    }
    kwargs = {
        "repository_id": repository,
        "run_id": run_id,
        "semver": semver,
        "frozen_commit": frozen_commit,
        "transaction_nonce": nonce,
        "release_active_manifest_sha256": "b" * 64,
        "release_bundle_manifest_sha256": "c" * 64,
        "claimed_release_channels": ["github"],
        "asset_names": assets,
    }
    assert validate_release_completion_evidence(evidence, **kwargs) == evidence

    for mutate in ("manifest", "idempotency", "readback", "order"):
        forged = copy.deepcopy(evidence)
        if mutate == "manifest":
            forged["release_active_manifest_sha256"] = "d" * 64
        elif mutate == "idempotency":
            forged["call_records"][0]["idempotency_key"] = "d" * 64
        else:
            if mutate == "readback":
                forged["call_records"][-2]["object_digest"] = None
            else:
                forged["call_records"][0], forged["call_records"][1] = (
                    forged["call_records"][1],
                    forged["call_records"][0],
                )
                forged["call_records"][0]["allowed_predecessor"] = (
                    "candidate_gates_passed"
                )
                forged["call_records"][1]["allowed_predecessor"] = forged[
                    "call_records"
                ][0]["state"]
                forged["call_records"][2]["allowed_predecessor"] = forged[
                    "call_records"
                ][1]["state"]
        with pytest.raises(ContractValidationError):
            validate_release_completion_evidence(forged, **kwargs)


def test_release_call_record_has_stable_identity_and_request_digests() -> None:
    record = make_call_record(
        repository="OMG",
        semver="1.2.3",
        frozen_commit="a" * 40,
        transaction_nonce="nonce-1",
        step="tag_push",
        state="tag_push_pending",
        allowed_predecessor="frozen_pass",
        attempt=1,
        redacted_external_locator="github:release",
        expected_identity={"tag": "v1.2.3", "target": "a" * 40},
        expected_byte_digest="b" * 64,
        request={"tag": "v1.2.3"},
        prior_mutable_identity=None,
    )
    context = {
        "repository": "OMG",
        "semver": "1.2.3",
        "frozen_commit": "a" * 40,
        "transaction_nonce": "nonce-1",
        "request": {"tag": "v1.2.3"},
    }
    assert validate_call_record(record, **context) == record
    replay = make_call_record(
        repository="OMG",
        semver="1.2.3",
        frozen_commit="a" * 40,
        transaction_nonce="nonce-1",
        step="tag_push",
        state="tag_push_pending",
        allowed_predecessor="frozen_pass",
        attempt=1,
        redacted_external_locator="github:release",
        expected_identity={"tag": "v1.2.3", "target": "a" * 40},
        expected_byte_digest="b" * 64,
        request={"tag": "v1.2.3"},
        prior_mutable_identity=None,
    )
    assert replay["idempotency_key"] == record["idempotency_key"]
    tampered = copy.deepcopy(record)
    tampered["expected_identity"]["tag"] = "v9.9.9"
    with pytest.raises(ContractValidationError, match="identity digest"):
        validate_call_record(tampered, **context)
    tampered_key = copy.deepcopy(record)
    tampered_key["idempotency_key"] = "0" * 64
    with pytest.raises(ContractValidationError, match="idempotency key mismatch"):
        validate_call_record(tampered_key, **context)
    tampered_request = copy.deepcopy(record)
    tampered_request["request_digest"] = "0" * 64
    with pytest.raises(ContractValidationError, match="request digest mismatch"):
        validate_call_record(tampered_request, **context)
    retry = make_call_record(
        repository="OMG",
        semver="1.2.3",
        frozen_commit="a" * 40,
        transaction_nonce="nonce-1",
        step="tag_push",
        state="tag_push_pending",
        allowed_predecessor="frozen_pass",
        attempt=2,
        redacted_external_locator="github:release",
        expected_identity={"tag": "v1.2.3", "target": "a" * 40},
        expected_byte_digest="b" * 64,
        request={"tag": "v1.2.3"},
        prior_mutable_identity=None,
    )
    assert retry["idempotency_key"] == record["idempotency_key"]


def test_three_registry_withdrawal_vectors_require_ordered_terminal_dispositions() -> None:
    fixture_vectors = tuple(tuple(row) for row in _load("three-registry-withdrawal-vectors.json"))
    assert expected_three_registry_withdrawal_vectors() == fixture_vectors
    ids = ["registry-a", "registry-b", "registry-c"]
    for vector in fixture_vectors:
        rows = []
        predecessor = "withdrawal_registry_cleanup_pending"
        for registry_id, disposition in zip(ids, vector, strict=True):
            state = (
                f"{registry_id}.deprecation_not_applicable"
                if disposition == "N/A"
                else f"{registry_id}.deprecation_readback_passed"
            )
            proof = (
                {"authoritative_no_write": True, "external_call": False}
                if disposition == "N/A"
                else {"deprecation_readback": True}
            )
            rows.append(
                {
                    "registry_id": registry_id,
                    "predecessor": predecessor,
                    "state": state,
                    "record_key": registry_cleanup_disposition_key(
                        "OMA", "1.2.3", "a" * 40, "nonce-1", registry_id
                    ),
                    "proof": proof,
                }
            )
            predecessor = state
        assert validate_cleanup_dispositions(
            ids,
            rows,
            repository="OMA",
            semver="1.2.3",
            frozen_commit="a" * 40,
            transaction_nonce="nonce-1",
        ) == rows
        broken = copy.deepcopy(rows)
        broken[-1]["predecessor"] = "withdrawal_registry_cleanup_pending"
        with pytest.raises(ContractValidationError, match="predecessor"):
            validate_cleanup_dispositions(
                ids,
                broken,
                repository="OMA",
                semver="1.2.3",
                frozen_commit="a" * 40,
                transaction_nonce="nonce-1",
            )

    common = {
        "repository": "OMA",
        "semver": "1.2.3",
        "frozen_commit": "a" * 40,
        "transaction_nonce": "nonce-1",
    }
    with pytest.raises(ContractValidationError, match="every claimed registry"):
        validate_cleanup_dispositions(ids, rows[:-1], **common)
    with pytest.raises(ContractValidationError, match="frozen registry order"):
        validate_cleanup_dispositions(ids, [rows[1], rows[0], rows[2]], **common)
    contradictory = copy.deepcopy(rows)
    contradictory[-1]["proof"] = {"authoritative_no_write": False, "external_call": True}
    with pytest.raises(ContractValidationError, match="exact no-write proof"):
        validate_cleanup_dispositions(ids, contradictory, **common)
    for suffix in ("deprecation_pending", "deprecation_unknown", "deprecation_failed"):
        nonterminal = copy.deepcopy(rows)
        nonterminal[-1]["state"] = f"registry-c.{suffix}"
        with pytest.raises(ContractValidationError, match="not terminal"):
            validate_cleanup_dispositions(ids, nonterminal, **common)
