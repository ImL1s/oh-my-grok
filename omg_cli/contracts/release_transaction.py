"""Pure schema/state grammar for immutable, idempotent release transactions.

W0 owns validation only.  No function in this module invokes Git, GitHub, npm,
or any other external writer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_git_oid,
    require_integer,
    require_iso8601,
    require_nonempty_string,
    require_object,
    require_safe_id,
    require_sha256,
)
from .writer_chain import canonical_json_bytes, sha256_hex


OMA_REGISTRY_STATE_SUFFIX_SET = (
    "publish_pending",
    "publish_unknown",
    "publish_failed",
    "published",
    "version_readback_pending",
    "version_readback_unknown",
    "version_readback_failed",
    "version_readback_passed",
    "staging_tag_set_pending",
    "staging_tag_set_unknown",
    "staging_tag_set_failed",
    "staging_tag_set",
    "staging_tag_readback_pending",
    "staging_tag_readback_unknown",
    "staging_tag_readback_failed",
    "staging_tag_readback_passed",
    "final_tag_set_pending",
    "final_tag_set_unknown",
    "final_tag_set_failed",
    "final_tag_set",
    "final_tag_readback_pending",
    "final_tag_readback_unknown",
    "final_tag_readback_failed",
    "final_tag_readback_passed",
    "final_tag_restore_pending",
    "final_tag_restore_unknown",
    "final_tag_restore_failed",
    "final_tag_restored",
    "final_tag_restore_readback_pending",
    "final_tag_restore_readback_unknown",
    "final_tag_restore_readback_failed",
    "final_tag_restore_readback_passed",
    "deprecation_pending",
    "deprecation_unknown",
    "deprecation_failed",
    "deprecated",
    "deprecation_readback_pending",
    "deprecation_readback_unknown",
    "deprecation_readback_failed",
    "deprecation_readback_passed",
    "deprecation_not_applicable",
)
GITHUB_CHANNEL_STATE_SET = (
    "external_publisher_conflict",
    "github_promotion_pending",
    "github_promotion_unknown",
    "github_promotion_failed",
    "github_promoted",
    "github_promotion_readback_pending",
    "github_promotion_readback_unknown",
    "github_promotion_readback_failed",
    "github_promotion_readback_passed",
    "github_latest_set_pending",
    "github_latest_set_unknown",
    "github_latest_set_failed",
    "github_latest_set",
    "github_latest_readback_pending",
    "github_latest_readback_unknown",
    "github_latest_readback_failed",
    "github_latest_readback_passed",
    "all_channels_readback_passed",
)
GITHUB_LATEST_RESTORE_STATE_SET = (
    "github_latest_restore_pending",
    "github_latest_restore_unknown",
    "github_latest_restore_failed",
    "github_latest_restored",
    "github_latest_restore_readback_pending",
    "github_latest_restore_readback_unknown",
    "github_latest_restore_readback_failed",
    "github_latest_restore_readback_passed",
)
REGISTRY_BARRIER_STATES = (
    "registries_staged_passed",
    "registries_not_applicable",
    "registry_final_tags_readback_passed",
    "registry_final_tags_not_applicable",
)
RUN_MANIFEST_RELEASE_STATES = (
    "candidate_gates_passed",
    "branch_push_pending",
    "branch_push_unknown",
    "branch_push_failed",
    "branch_pushed",
    "branch_readback_pending",
    "branch_readback_unknown",
    "branch_readback_failed",
    "branch_readback_passed",
    "commit_proof_pending",
    "commit_proof_failed",
    "commit_proof_passed",
    "release_bundle_frozen",
    "frozen_pass",
)
PUBLIC_RELEASE_STATES = (
    "tag_push_pending",
    "tag_push_unknown",
    "tag_push_failed",
    "tag_pushed",
    "tag_readback_pending",
    "tag_readback_unknown",
    "tag_readback_failed",
    "tag_readback_passed",
    "prerelease_create_pending",
    "prerelease_create_unknown",
    "prerelease_create_failed",
    "prerelease_created",
    "prerelease_readback_pending",
    "prerelease_readback_unknown",
    "prerelease_readback_failed",
    "prerelease_readback_passed",
    "assets_readback_passed",
    "package_not_applicable",
    "attestation_pending",
    "attestation_unknown",
    "attestation_failed",
    "attestation_passed",
    "verified_write_pending",
    "verified_write_unknown",
    "verified_write_failed",
    "verified",
    "verified_readback_pending",
    "verified_readback_unknown",
    "verified_readback_failed",
    "verified_readback_passed",
    "final_readback_pending",
    "final_readback_unknown",
    "final_readback_failed",
    "final_readback_passed",
    "complete",
    "release_blocked",
    "branch_conflict_blocked",
    "identity_conflict_fix_forward_required",
    "withdrawal_required",
    "withdrawal_release_update_pending",
    "withdrawal_release_update_unknown",
    "withdrawal_release_update_failed",
    "withdrawal_release_updated",
    "withdrawal_release_readback_pending",
    "withdrawal_release_readback_unknown",
    "withdrawal_release_readback_failed",
    "withdrawal_release_readback_passed",
    "withdrawal_registry_cleanup_pending",
    "withdrawal_registries_not_applicable",
    "withdrawal_blocked",
    "withdrawn_fix_forward_required",
    "release_inconsistent_pending",
    "release_inconsistent_unknown",
    "release_inconsistent_reconciled",
    "release_inconsistent_fix_forward_required",
    "release_inconsistent_blocked",
)
REGISTRY_POLICY_KEYS = (
    "registry_id",
    "registry_url",
    "package",
    "final_dist_tag",
    "staging_tag_derivation",
    "credential_preflight_hash",
    "readback_preflight_hash",
)
FINAL_REGISTRY_ADDITIONS = (
    "tarball_sha256",
    "integrity",
    "provenance_hash",
    "staging_dist_tag",
    "prior_final_tag_identity",
)
PRODUCTION_REGISTRY_IDS = ("github-packages", "npmjs")
OMA_PACKAGE = "@iml1s/oh-my-agy"
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def release_transaction_identity_hash(
    repository: str, semver: str, frozen_commit: str, transaction_nonce: str
) -> str:
    if repository not in {"OMG", "OMA"}:
        raise ContractValidationError("repository must be OMG or OMA")
    if not SEMVER_RE.fullmatch(semver):
        raise ContractValidationError("semver is invalid")
    require_git_oid(frozen_commit, label="frozen_commit")
    require_safe_id(transaction_nonce, label="transaction_nonce")
    return sha256_hex(canonical_json_bytes([repository, semver, frozen_commit, transaction_nonce]))


def release_idempotency_key(
    repository: str,
    semver: str,
    frozen_commit: str,
    transaction_nonce: str,
    step: str,
    expected_identity_digest: str,
) -> str:
    release_transaction_identity_hash(repository, semver, frozen_commit, transaction_nonce)
    require_nonempty_string(step, label="step")
    require_sha256(expected_identity_digest, label="expected_identity_digest")
    return sha256_hex(
        canonical_json_bytes(
            [
                repository,
                semver,
                frozen_commit,
                transaction_nonce,
                step,
                expected_identity_digest,
            ]
        )
    )


def registry_cleanup_disposition_key(
    repository: str,
    semver: str,
    frozen_commit: str,
    transaction_nonce: str,
    registry_id: str,
) -> str:
    release_transaction_identity_hash(repository, semver, frozen_commit, transaction_nonce)
    require_safe_id(registry_id, label="registry_id")
    return sha256_hex(
        canonical_json_bytes(
            [
                repository,
                semver,
                frozen_commit,
                transaction_nonce,
                "registry_cleanup_disposition",
                registry_id,
            ]
        )
    )


def validate_registry_policy(
    value: Mapping[str, Any], *, production: bool = True
) -> dict[str, Any]:
    policy = require_object(value, label="registry policy")
    require_exact_keys(policy, required=set(REGISTRY_POLICY_KEYS), label="registry policy")
    registry_id = require_safe_id(policy["registry_id"], label="registry_id")
    if production and registry_id not in PRODUCTION_REGISTRY_IDS:
        raise ContractValidationError("registry_id is outside production allowlist")
    require_nonempty_string(policy["registry_url"], label="registry_url")
    if policy["package"] != OMA_PACKAGE:
        raise ContractValidationError(f"OMA package must be {OMA_PACKAGE}")
    require_safe_id(policy["final_dist_tag"], label="final_dist_tag")
    require_nonempty_string(policy["staging_tag_derivation"], label="staging_tag_derivation")
    require_sha256(policy["credential_preflight_hash"], label="credential_preflight_hash")
    require_sha256(policy["readback_preflight_hash"], label="readback_preflight_hash")
    return policy


def validate_claimed_registries(
    values: Sequence[Mapping[str, Any]],
    *,
    semver: str,
    transaction_identity_hash: str,
    production: bool = True,
) -> list[dict[str, Any]]:
    require_sha256(transaction_identity_hash, label="transaction_identity_hash")
    if not SEMVER_RE.fullmatch(semver):
        raise ContractValidationError("semver is invalid")
    rows: list[dict[str, Any]] = []
    ids: list[str] = []
    expected_tag = f"oma-prerelease-{transaction_identity_hash[:12]}"
    for value in values:
        row = require_object(value, label="claimed registry")
        require_exact_keys(
            row,
            required=set(REGISTRY_POLICY_KEYS + FINAL_REGISTRY_ADDITIONS),
            label="claimed registry",
        )
        validate_registry_policy(
            {name: row[name] for name in REGISTRY_POLICY_KEYS},
            production=production,
        )
        require_sha256(row["tarball_sha256"], label="tarball_sha256")
        require_nonempty_string(row["integrity"], label="integrity")
        require_sha256(row["provenance_hash"], label="provenance_hash")
        if row["staging_dist_tag"] != expected_tag or row["staging_dist_tag"] == "latest":
            raise ContractValidationError("staging_dist_tag does not match frozen derivation")
        require_nonempty_string(row["prior_final_tag_identity"], label="prior_final_tag_identity")
        ids.append(row["registry_id"])
        rows.append(row)
    if len(ids) != len(set(ids)):
        raise ContractValidationError("claimed registry IDs must be unique")
    if production and ids != [item for item in PRODUCTION_REGISTRY_IDS if item in ids]:
        raise ContractValidationError("claimed registries are not in frozen production order")
    return rows


def qualified_registry_states(registry_ids: Sequence[str]) -> tuple[str, ...]:
    if len(registry_ids) != len(set(registry_ids)):
        raise ContractValidationError("registry IDs must be unique")
    for registry_id in registry_ids:
        require_safe_id(registry_id, label="registry_id")
    return tuple(
        f"{registry_id}.{suffix}"
        for registry_id in registry_ids
        for suffix in OMA_REGISTRY_STATE_SUFFIX_SET
    )


def qualified_asset_states(asset_names: Sequence[str]) -> tuple[str, ...]:
    states: list[str] = []
    for name in asset_names:
        require_nonempty_string(name, label="asset name")
        digest = sha256_hex(name)[:16]
        for suffix in (
            "asset_upload_pending",
            "asset_upload_unknown",
            "asset_upload_failed",
            "asset_uploaded",
            "asset_readback_pending",
            "asset_readback_unknown",
            "asset_readback_failed",
            "asset_readback_passed",
        ):
            states.append(f"asset-{digest}.{suffix}")
    return tuple(states)


def allowed_release_states(
    registry_ids: Sequence[str] = (), asset_names: Sequence[str] = ()
) -> frozenset[str]:
    return frozenset(
        RUN_MANIFEST_RELEASE_STATES
        + PUBLIC_RELEASE_STATES
        + GITHUB_CHANNEL_STATE_SET
        + GITHUB_LATEST_RESTORE_STATE_SET
        + REGISTRY_BARRIER_STATES
        + qualified_registry_states(registry_ids)
        + qualified_asset_states(asset_names)
    )


def validate_release_state_name(
    state: str,
    *,
    registry_ids: Sequence[str] = (),
    asset_names: Sequence[str] = (),
) -> str:
    require_nonempty_string(state, label="release state")
    if (
        state != "package_not_applicable"
        and (
            state.startswith("package_")
            or state.startswith("promotion_")
            or state.startswith("latest_")
        )
    ):
        raise ContractValidationError("scalar package/promotion/latest aliases are forbidden")
    if state not in allowed_release_states(registry_ids, asset_names):
        raise ContractValidationError(f"release state is outside frozen grammar: {state!r}")
    return state


def release_state_is_success(
    state: str,
    *,
    registry_ids: Sequence[str] = (),
    asset_names: Sequence[str] = (),
) -> bool:
    """Only the exact terminal repository state is product success."""

    validate_release_state_name(
        state,
        registry_ids=registry_ids,
        asset_names=asset_names,
    )
    return state == "complete"


def classify_external_observation(
    result_count: int,
    *,
    timed_out: bool = False,
    call_failed: bool = False,
) -> str:
    """Classify a bounded external readback without collapsing ambiguity."""

    require_integer(result_count, label="result_count", minimum=0)
    if not isinstance(timed_out, bool) or not isinstance(call_failed, bool):
        raise ContractValidationError("external observation flags must be boolean")
    if timed_out and call_failed:
        raise ContractValidationError("external observation failure flags conflict")
    if (timed_out or call_failed) and result_count != 0:
        raise ContractValidationError("failed external observation cannot claim result rows")
    if timed_out:
        return "unknown"
    if call_failed:
        return "failed"
    if result_count == 0:
        return "absent"
    if result_count == 1:
        return "exact"
    return "ambiguous"


BUILD_RECEIPT_KEYS = {
    "argv",
    "cwd_realpath_hash",
    "toolchain",
    "environment_allowlist",
    "environment_value_hashes",
    "SOURCE_DATE_EPOCH",
    "locale",
    "timezone",
    "umask",
    "receipt_hash",
}


def validate_build_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    receipt = require_object(value, label="build receipt")
    require_exact_keys(receipt, required=BUILD_RECEIPT_KEYS, label="build receipt")
    argv = receipt["argv"]
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise ContractValidationError("build receipt argv must be a non-empty string array")
    require_sha256(receipt["cwd_realpath_hash"], label="cwd_realpath_hash")
    toolchain = receipt["toolchain"]
    if not isinstance(toolchain, list):
        raise ContractValidationError("build receipt toolchain must be an array")
    tool_names: list[str] = []
    for row in toolchain:
        item = require_object(row, label="toolchain row")
        require_exact_keys(item, required={"name", "version", "binary_sha256"}, label="toolchain row")
        require_nonempty_string(item["name"], label="tool name")
        require_nonempty_string(item["version"], label="tool version")
        require_sha256(item["binary_sha256"], label="binary_sha256")
        tool_names.append(item["name"])
    if len(tool_names) != len(set(tool_names)):
        raise ContractValidationError("build receipt tool names must be unique")
    if not isinstance(receipt["environment_allowlist"], list) or not all(
        isinstance(item, str) for item in receipt["environment_allowlist"]
    ):
        raise ContractValidationError("environment_allowlist must be a string array")
    env_hashes = require_object(receipt["environment_value_hashes"], label="environment_value_hashes")
    if len(receipt["environment_allowlist"]) != len(set(receipt["environment_allowlist"])):
        raise ContractValidationError("environment_allowlist must be unique")
    # Canonical JSON sorts object keys, so semantic ordering lives only in the
    # explicit allowlist; the hash map must cover exactly that set.
    if set(env_hashes) != set(receipt["environment_allowlist"]):
        raise ContractValidationError("environment hash keys must match the allowlist")
    for name, digest in env_hashes.items():
        require_nonempty_string(name, label="environment name")
        require_sha256(digest, label=f"environment hash {name}")
    require_integer(receipt["SOURCE_DATE_EPOCH"], label="SOURCE_DATE_EPOCH", minimum=0)
    require_nonempty_string(receipt["locale"], label="locale")
    require_nonempty_string(receipt["timezone"], label="timezone")
    if not re.fullmatch(r"0?[0-7]{3}", str(receipt["umask"])):
        raise ContractValidationError("umask must be a three/four-digit octal string")
    expected_hash = sha256_hex(
        canonical_json_bytes({key: receipt[key] for key in receipt if key != "receipt_hash"})
    )
    if receipt["receipt_hash"] != expected_hash:
        raise ContractValidationError("build receipt hash mismatch")
    return receipt


ASSET_KEYS = {"name", "relative_path", "byte_length", "sha256", "media_type"}
BUNDLE_KEYS = {
    "store_kind",
    "schema_version",
    "repository_id",
    "run_id",
    "owner",
    "candidate_commit",
    "candidate_tree",
    "semver",
    "bundle_directory",
    "public_upload_order",
    "assets",
    "checksum",
    "build_receipt",
    "registry_bindings",
    "release_asset_root",
}


def _safe_relative_path(value: Any, *, label: str) -> str:
    text = require_nonempty_string(value, label=label)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or str(path) != text:
        raise ContractValidationError(f"{label} must be normalized repository-relative")
    return text


def expected_bundle_manifest_relative_path(repository: str, run_id: str) -> str:
    require_safe_id(run_id, label="run_id")
    if repository == "OMG":
        return f".omg/artifacts/dual-parity/{run_id}/OMG-W6/release-bundle-manifest.json"
    if repository == "OMA":
        return f".agy/artifacts/dual-parity/{run_id}/OMA-W6/release-bundle-manifest.json"
    raise ContractValidationError("repository must be OMG or OMA")


def validate_release_bundle_manifest(
    value: Mapping[str, Any],
    *,
    manifest_relative_path: str,
    claimed_registries: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    manifest = require_object(value, label="release bundle manifest")
    require_exact_keys(manifest, required=BUNDLE_KEYS, label="release bundle manifest")
    if manifest["store_kind"] != "release_bundle_manifest" or manifest["schema_version"] != 1:
        raise ContractValidationError("release bundle manifest header mismatch")
    repository = manifest["repository_id"]
    if repository not in {"OMG", "OMA"}:
        raise ContractValidationError("bundle repository must be OMG or OMA")
    run_id = require_safe_id(manifest["run_id"], label="run_id")
    if manifest_relative_path != expected_bundle_manifest_relative_path(repository, run_id):
        raise ContractValidationError("release bundle manifest path mismatch")
    expected_owner = f"{repository}-W6" if repository == "OMA" else "OMG-W6"
    if manifest["owner"] != expected_owner:
        raise ContractValidationError("release bundle owner mismatch")
    require_git_oid(manifest["candidate_commit"], label="candidate_commit")
    require_git_oid(manifest["candidate_tree"], label="candidate_tree")
    semver = require_nonempty_string(manifest["semver"], label="semver")
    if not SEMVER_RE.fullmatch(semver):
        raise ContractValidationError("bundle semver is invalid")
    bundle_directory = _safe_relative_path(manifest["bundle_directory"], label="bundle_directory")
    expected_directory = str(PurePosixPath(manifest_relative_path).parent / "release-bundle")
    if bundle_directory != expected_directory:
        raise ContractValidationError("bundle directory must be manifest sibling release-bundle")
    payload_name = (
        f"oh-my-grok-{semver}.tar.gz" if repository == "OMG" else f"iml1s-oh-my-agy-{semver}.tgz"
    )
    expected_order = [payload_name, "SHA256SUMS"]
    if manifest["public_upload_order"] != expected_order:
        raise ContractValidationError("public upload order mismatch")
    assets = manifest["assets"]
    if not isinstance(assets, list) or len(assets) != 2:
        raise ContractValidationError("release bundle assets must contain exactly two entries")
    names: list[str] = []
    for asset in assets:
        row = require_object(asset, label="asset")
        require_exact_keys(row, required=ASSET_KEYS, label="asset")
        name = require_nonempty_string(row["name"], label="asset.name")
        names.append(name)
        relative = _safe_relative_path(row["relative_path"], label="asset.relative_path")
        if relative != f"{bundle_directory}/{name}":
            raise ContractValidationError("asset relative path does not match name/directory")
        require_integer(row["byte_length"], label="asset.byte_length", minimum=0)
        require_sha256(row["sha256"], label="asset.sha256")
        expected_media_type = "application/gzip" if name == payload_name else "text/plain"
        if row["media_type"] != expected_media_type:
            raise ContractValidationError("asset media type mismatch")
    if names != expected_order:
        raise ContractValidationError("asset rows must preserve public upload order")
    checksum = require_object(manifest["checksum"], label="checksum")
    require_exact_keys(
        checksum,
        required={
            "name",
            "payload_name",
            "payload_sha256",
            "bytes_utf8",
            "byte_length",
            "sha256",
        },
        label="checksum",
    )
    if checksum["name"] != "SHA256SUMS" or checksum["payload_name"] != payload_name:
        raise ContractValidationError("checksum identity mismatch")
    payload_sha = require_sha256(checksum["payload_sha256"], label="payload_sha256")
    if payload_sha != assets[0]["sha256"]:
        raise ContractValidationError("checksum payload hash differs from asset")
    expected_checksum_bytes = f"{payload_sha}  {payload_name}\n"
    if checksum["bytes_utf8"] != expected_checksum_bytes:
        raise ContractValidationError("SHA256SUMS bytes are not exact")
    encoded_checksum = expected_checksum_bytes.encode("utf-8")
    if checksum["byte_length"] != len(encoded_checksum):
        raise ContractValidationError("checksum byte length mismatch")
    if checksum["sha256"] != sha256_hex(encoded_checksum):
        raise ContractValidationError("checksum hash mismatch")
    if assets[1]["byte_length"] != len(encoded_checksum) or assets[1]["sha256"] != checksum["sha256"]:
        raise ContractValidationError("checksum asset row mismatch")
    validate_build_receipt(manifest["build_receipt"])
    bindings = manifest["registry_bindings"]
    if repository == "OMG":
        if bindings != [] or claimed_registries:
            raise ContractValidationError("OMG registry_bindings must be empty")
    else:
        if list(bindings) != [dict(row) for row in claimed_registries]:
            raise ContractValidationError("OMA registry_bindings must equal claimed_registries")
        for row in bindings:
            if row.get("tarball_sha256") != assets[0]["sha256"]:
                raise ContractValidationError("registry tarball hash differs from bundle")
    expected_root = sha256_hex(canonical_json_bytes(assets))
    if manifest["release_asset_root"] != expected_root:
        raise ContractValidationError("release asset root mismatch")
    return manifest


def verify_release_bundle_files(
    root: Path | str,
    manifest: Mapping[str, Any],
    *,
    manifest_relative_path: str,
    claimed_registries: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    validated = validate_release_bundle_manifest(
        manifest,
        manifest_relative_path=manifest_relative_path,
        claimed_registries=claimed_registries,
    )
    root_path = Path(root).resolve()
    manifest_path = (root_path / manifest_relative_path).resolve(strict=False)
    try:
        manifest_path.relative_to(root_path)
    except ValueError as exc:
        raise ContractValidationError("manifest path escapes repository") from exc
    bundle_dir = (root_path / validated["bundle_directory"]).resolve(strict=False)
    try:
        bundle_dir.relative_to(root_path)
    except ValueError as exc:
        raise ContractValidationError("bundle directory escapes repository") from exc
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise ContractValidationError("bundle directory missing or unsafe")
    actual_names = sorted(path.name for path in bundle_dir.iterdir())
    if actual_names != sorted(validated["public_upload_order"]):
        raise ContractValidationError("bundle file set contains missing/extra/renamed bytes")
    for asset in validated["assets"]:
        path = root_path / asset["relative_path"]
        if not path.is_file() or path.is_symlink():
            raise ContractValidationError("bundle asset missing or unsafe")
        body = path.read_bytes()
        if len(body) != asset["byte_length"] or sha256_hex(body) != asset["sha256"]:
            raise ContractValidationError("bundle asset byte drift")
    return validated


CALL_RECORD_KEYS = {
    "step",
    "state",
    "allowed_predecessor",
    "attempt",
    "redacted_external_locator",
    "expected_identity",
    "expected_identity_digest",
    "expected_byte_digest",
    "request_digest",
    "idempotency_key",
    "prior_mutable_identity",
    "external_id",
    "etag",
    "object_digest",
    "readback_at",
}


def make_call_record(
    *,
    repository: str,
    semver: str,
    frozen_commit: str,
    transaction_nonce: str,
    step: str,
    state: str,
    allowed_predecessor: str,
    attempt: int,
    redacted_external_locator: str,
    expected_identity: Mapping[str, Any],
    expected_byte_digest: str | None,
    request: Mapping[str, Any],
    prior_mutable_identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    identity = dict(expected_identity)
    identity_digest = sha256_hex(canonical_json_bytes(identity))
    if expected_byte_digest is not None:
        require_sha256(expected_byte_digest, label="expected_byte_digest")
    record = {
        "step": require_nonempty_string(step, label="step"),
        "state": require_nonempty_string(state, label="state"),
        "allowed_predecessor": require_nonempty_string(
            allowed_predecessor, label="allowed_predecessor"
        ),
        "attempt": require_integer(attempt, label="attempt", minimum=1),
        "redacted_external_locator": require_nonempty_string(
            redacted_external_locator, label="redacted_external_locator"
        ),
        "expected_identity": identity,
        "expected_identity_digest": identity_digest,
        "expected_byte_digest": expected_byte_digest,
        "request_digest": sha256_hex(canonical_json_bytes(dict(request))),
        "idempotency_key": release_idempotency_key(
            repository,
            semver,
            frozen_commit,
            transaction_nonce,
            step,
            identity_digest,
        ),
        "prior_mutable_identity": dict(prior_mutable_identity)
        if prior_mutable_identity is not None
        else None,
        "external_id": None,
        "etag": None,
        "object_digest": None,
        "readback_at": None,
    }
    return record


def validate_call_record(
    value: Mapping[str, Any],
    *,
    repository: str | None = None,
    semver: str | None = None,
    frozen_commit: str | None = None,
    transaction_nonce: str | None = None,
    request: Mapping[str, Any] | None = None,
    registry_ids: Sequence[str] = (),
    asset_names: Sequence[str] = (),
) -> dict[str, Any]:
    record = require_object(value, label="release call record")
    require_exact_keys(record, required=CALL_RECORD_KEYS, label="release call record")
    require_nonempty_string(record["step"], label="step")
    validate_release_state_name(
        record["state"], registry_ids=registry_ids, asset_names=asset_names
    )
    validate_release_state_name(
        record["allowed_predecessor"], registry_ids=registry_ids, asset_names=asset_names
    )
    require_integer(record["attempt"], label="attempt", minimum=1)
    require_nonempty_string(record["redacted_external_locator"], label="redacted_external_locator")
    identity = require_object(record["expected_identity"], label="expected_identity")
    if record["expected_identity_digest"] != sha256_hex(canonical_json_bytes(identity)):
        raise ContractValidationError("expected identity digest mismatch")
    if record["expected_byte_digest"] is not None:
        require_sha256(record["expected_byte_digest"], label="expected_byte_digest")
    require_sha256(record["request_digest"], label="request_digest")
    require_sha256(record["idempotency_key"], label="idempotency_key")
    context = (repository, semver, frozen_commit, transaction_nonce)
    if any(item is not None for item in context):
        if any(item is None for item in context):
            raise ContractValidationError("complete release identity is required for call verification")
        expected_key = release_idempotency_key(
            str(repository),
            str(semver),
            str(frozen_commit),
            str(transaction_nonce),
            record["step"],
            record["expected_identity_digest"],
        )
        if record["idempotency_key"] != expected_key:
            raise ContractValidationError("release call idempotency key mismatch")
    if request is not None:
        expected_request_digest = sha256_hex(canonical_json_bytes(dict(request)))
        if record["request_digest"] != expected_request_digest:
            raise ContractValidationError("release call request digest mismatch")
    if record["prior_mutable_identity"] is not None:
        require_object(record["prior_mutable_identity"], label="prior_mutable_identity")
    for field in ("external_id", "etag"):
        if record[field] is not None and not isinstance(record[field], str):
            raise ContractValidationError(f"{field} must be null or string")
    if record["object_digest"] is not None:
        require_sha256(record["object_digest"], label="object_digest")
    if record["readback_at"] is not None:
        require_iso8601(record["readback_at"], label="readback_at")
    return record


RELEASE_COMPLETION_EVIDENCE_KEYS = {
    "store_kind",
    "schema_version",
    "repository_id",
    "run_id",
    "semver",
    "frozen_commit",
    "transaction_nonce",
    "transaction_identity_hash",
    "release_active_manifest_sha256",
    "release_bundle_manifest_sha256",
    "final_state",
    "call_records",
    "verified_at",
}


def validate_release_completion_evidence(
    value: Mapping[str, Any],
    *,
    repository_id: str,
    run_id: str,
    semver: str,
    frozen_commit: str,
    transaction_nonce: str,
    release_active_manifest_sha256: str,
    release_bundle_manifest_sha256: str,
    claimed_release_channels: Sequence[str],
    registry_ids: Sequence[str] = (),
    asset_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate the immutable evidence that alone may close a release run.

    The record chain is deliberately redundant: every record carries its own
    transaction-derived idempotency key, and the evidence also binds the exact
    release-active manifest and frozen bundle.  A caller cannot substitute
    evidence from another run, candidate, nonce, or pre-release revision.
    """

    evidence = require_object(value, label="release completion evidence")
    require_exact_keys(
        evidence,
        required=RELEASE_COMPLETION_EVIDENCE_KEYS,
        label="release completion evidence",
    )
    if (
        evidence["store_kind"] != "release_completion_evidence"
        or evidence["schema_version"] != 1
        or isinstance(evidence["schema_version"], bool)
    ):
        raise ContractValidationError("release completion evidence header mismatch")
    if evidence["repository_id"] != repository_id or evidence["run_id"] != run_id:
        raise ContractValidationError("release completion evidence identity mismatch")
    if (
        evidence["semver"] != semver
        or evidence["frozen_commit"] != frozen_commit
        or evidence["transaction_nonce"] != transaction_nonce
    ):
        raise ContractValidationError("release completion transaction binding mismatch")
    expected_identity = release_transaction_identity_hash(
        repository_id, semver, frozen_commit, transaction_nonce
    )
    if evidence["transaction_identity_hash"] != expected_identity:
        raise ContractValidationError("release completion transaction identity hash mismatch")
    if evidence["release_active_manifest_sha256"] != require_sha256(
        release_active_manifest_sha256, label="release_active_manifest_sha256"
    ):
        raise ContractValidationError("release completion manifest hash mismatch")
    if evidence["release_bundle_manifest_sha256"] != require_sha256(
        release_bundle_manifest_sha256, label="release_bundle_manifest_sha256"
    ):
        raise ContractValidationError("release completion bundle hash mismatch")
    if evidence["final_state"] != "complete":
        raise ContractValidationError("release completion final_state must be complete")
    require_iso8601(evidence["verified_at"], label="verified_at")
    records = evidence["call_records"]
    if not isinstance(records, list) or not records:
        raise ContractValidationError("release completion call_records must be non-empty")

    validated: list[dict[str, Any]] = []
    states: list[str] = []
    for raw in records:
        record = validate_call_record(
            raw,
            repository=repository_id,
            semver=semver,
            frozen_commit=frozen_commit,
            transaction_nonce=transaction_nonce,
            registry_ids=registry_ids,
            asset_names=asset_names,
        )
        if validated and record["allowed_predecessor"] != validated[-1]["state"]:
            raise ContractValidationError("release completion call record chain is broken")
        if record["state"].endswith("_readback_passed") and (
            record["object_digest"] is None or record["readback_at"] is None
        ):
            raise ContractValidationError(
                "passed release readback requires object digest and timestamp"
            )
        validated.append(record)
        states.append(str(record["state"]))
    if states[-2:] != ["final_readback_passed", "complete"]:
        raise ContractValidationError(
            "release completion must end final_readback_passed->complete"
        )
    if validated[-1]["allowed_predecessor"] != "final_readback_passed":
        raise ContractValidationError("release completion terminal predecessor mismatch")

    required_states = {
        "branch_readback_passed",
        "commit_proof_passed",
        "tag_readback_passed",
        "prerelease_readback_passed",
        "assets_readback_passed",
        "verified_readback_passed",
        "final_readback_passed",
        "complete",
    }
    if "github" in claimed_release_channels:
        required_states.update(
            {"github_promotion_readback_passed", "github_latest_readback_passed"}
        )
    required_states.update(
        f"asset-{sha256_hex(name)[:16]}.asset_readback_passed"
        for name in asset_names
    )
    for registry_id in registry_ids:
        required_states.update(
            {
                f"{registry_id}.version_readback_passed",
                f"{registry_id}.staging_tag_readback_passed",
                f"{registry_id}.final_tag_readback_passed",
            }
        )
    missing = sorted(required_states - set(states))
    if missing:
        raise ContractValidationError(
            f"release completion is missing required verified states: {missing!r}"
        )
    if len(states) != len(set(states)):
        raise ContractValidationError("release completion states must not be replayed")
    steps = [str(record["step"]) for record in validated]
    keys = [str(record["idempotency_key"]) for record in validated]
    if len(steps) != len(set(steps)) or len(keys) != len(set(keys)):
        raise ContractValidationError(
            "release completion steps and idempotency keys must be unique"
        )
    required_order = [
        "branch_readback_passed",
        "commit_proof_passed",
        *(f"{registry_id}.version_readback_passed" for registry_id in registry_ids),
        *(f"{registry_id}.staging_tag_readback_passed" for registry_id in registry_ids),
        "tag_readback_passed",
        "prerelease_readback_passed",
        *(
            f"asset-{sha256_hex(name)[:16]}.asset_readback_passed"
            for name in asset_names
        ),
        "assets_readback_passed",
        *(
            ["github_promotion_readback_passed", "github_latest_readback_passed"]
            if "github" in claimed_release_channels
            else []
        ),
        *(f"{registry_id}.final_tag_readback_passed" for registry_id in registry_ids),
        "verified_readback_passed",
        "final_readback_passed",
        "complete",
    ]
    indexes = [states.index(state) for state in required_order]
    if indexes != sorted(indexes):
        raise ContractValidationError(
            "release completion verified states are out of required order"
        )
    return evidence


def validate_cleanup_dispositions(
    registry_ids: Sequence[str],
    dispositions: Sequence[Mapping[str, Any]],
    *,
    repository: str,
    semver: str,
    frozen_commit: str,
    transaction_nonce: str,
) -> list[dict[str, Any]]:
    if len(registry_ids) != len(set(registry_ids)):
        raise ContractValidationError("cleanup registry IDs must be unique")
    for registry_id in registry_ids:
        require_safe_id(registry_id, label="registry_id")
    if len(dispositions) != len(registry_ids):
        raise ContractValidationError("every claimed registry needs one cleanup disposition")
    result: list[dict[str, Any]] = []
    previous_terminal = "withdrawal_registry_cleanup_pending"
    for expected_id, raw in zip(registry_ids, dispositions, strict=True):
        row = require_object(raw, label="cleanup disposition")
        require_exact_keys(
            row,
            required={"registry_id", "predecessor", "state", "record_key", "proof"},
            label="cleanup disposition",
        )
        if row["registry_id"] != expected_id:
            raise ContractValidationError("cleanup dispositions are not in frozen registry order")
        if row["predecessor"] != previous_terminal:
            raise ContractValidationError("cleanup predecessor mismatch")
        if row["state"] not in {
            f"{expected_id}.deprecation_readback_passed",
            f"{expected_id}.deprecation_not_applicable",
        }:
            raise ContractValidationError("cleanup disposition is not terminal")
        expected_key = registry_cleanup_disposition_key(
            repository, semver, frozen_commit, transaction_nonce, expected_id
        )
        if row["record_key"] != expected_key:
            raise ContractValidationError("cleanup disposition key mismatch")
        proof = require_object(row["proof"], label="cleanup proof")
        if row["state"].endswith("deprecation_not_applicable"):
            require_exact_keys(
                proof,
                required={"authoritative_no_write", "external_call"},
                label="N/A cleanup proof",
            )
            if proof != {"authoritative_no_write": True, "external_call": False}:
                raise ContractValidationError("N/A cleanup requires exact no-write proof")
        else:
            require_exact_keys(
                proof,
                required={"deprecation_readback"},
                label="deprecated cleanup proof",
            )
            if proof["deprecation_readback"] is not True:
                raise ContractValidationError("deprecated cleanup requires exact readback proof")
        previous_terminal = row["state"]
        result.append(row)
    return result


def expected_three_registry_withdrawal_vectors() -> tuple[tuple[str, ...], ...]:
    return (
        ("N/A", "N/A", "N/A"),
        ("DEPRECATED", "N/A", "N/A"),
        ("DEPRECATED", "DEPRECATED", "N/A"),
    )
