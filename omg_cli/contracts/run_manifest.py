"""Authoritative ``dual_parity_run_manifest/1`` bootstrap and CAS engine.

The module is deliberately executable with ``python3 -m``.  It creates the
repository-local manifest and trust layout, but it never prints or returns raw
HMAC keys.  Later public CLI routes must delegate to these functions rather
than implementing a second state machine.
"""

from __future__ import annotations

import argparse
import ast
import hmac
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .path_keys import (
    DATA_FILE_MODE,
    IMMUTABLE_SOURCE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    mode_bits,
)
from .parity_schema import OMG_OWNER_PATTERNS
from .release_transaction import (
    SEMVER_RE,
    expected_bundle_manifest_relative_path,
    validate_release_bundle_manifest,
    validate_release_completion_evidence,
    verify_release_bundle_files,
)
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
from .writer_chain import canonical_json_bytes, parse_canonical_json_bytes, sha256_hex
from .writer_chain import (
    FINAL_AGGREGATE_DOMAIN,
    HANDOFF_DOMAIN,
    INPUT_AGGREGATE_DOMAIN,
    VerifiedParentHashes,
    build_w6_request_binding,
    expected_parent_waves,
    handoff_hash,
    hmac_sha256_hex,
    owner_for_path,
    path_matches_pattern,
    sign_handoff,
    validate_parent_hashes,
    validate_proposal_entry,
    validate_w6_request_bindings,
    verify_dirty_ownership,
    verify_final_candidate,
    verify_handoff,
    verify_proposal_index,
)


RUN_MANIFEST_STATE_SET = (
    "initializing",
    "writers_active",
    "inputs_verified",
    "composition_active",
    "signing_revoked",
    "release_active",
    "closed",
    "blocked",
)
RUN_MANIFEST_TRANSITIONS: dict[str, frozenset[str]] = {
    "initializing": frozenset({"writers_active", "blocked"}),
    "writers_active": frozenset({"inputs_verified", "blocked"}),
    "inputs_verified": frozenset({"composition_active", "blocked"}),
    "composition_active": frozenset({"signing_revoked", "blocked"}),
    "signing_revoked": frozenset({"release_active", "blocked"}),
    "release_active": frozenset({"closed", "blocked"}),
    "closed": frozenset(),
    "blocked": frozenset(),
}
NORMATIVE_ARTIFACT_KEYS = ("requirements", "prd", "test_spec", "plan")
REPOSITORY_OWNER_ROWS = {
    "OMG": tuple(
        (f"OMG-W{index}", owner)
        for index, owner in enumerate(
            (
                "omg-contract-owner",
                "omg-install-owner",
                "omg-state-owner",
                "omg-team-owner",
                "omg-native-surface-owner",
                "omg-adapter-owner",
            )
        )
    ),
    "OMA": tuple(
        (f"OMA-W{index}", owner)
        for index, owner in enumerate(
            (
                "oma-contract-owner",
                "oma-install-owner",
                "oma-state-owner",
                "oma-team-owner",
                "oma-native-surface-owner",
                "oma-adapter-owner",
            )
        )
    ),
}
OMG_RELEASE_CHANNELS = ["github"]
OMG_OWNERSHIP_MANIFEST_HASH = sha256_hex(canonical_json_bytes(OMG_OWNER_PATTERNS))


def _validate_repository_policy(
    repository_id: str,
    *,
    claimed_release_channels: Sequence[str],
    claimed_registries: Sequence[Mapping[str, Any]],
    ownership_manifest_hash: str,
) -> None:
    """Fail closed on the frozen repository-specific ownership/release policy."""

    if repository_id != "OMG":
        return
    if list(claimed_release_channels) != OMG_RELEASE_CHANNELS:
        raise ContractValidationError(
            "OMG claimed_release_channels must be exactly ['github']"
        )
    if list(claimed_registries) != []:
        raise ContractValidationError("OMG claimed_registries must be empty")
    if ownership_manifest_hash != OMG_OWNERSHIP_MANIFEST_HASH:
        raise ContractValidationError("OMG ownership manifest hash mismatch")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def expected_manifest_path(root: Path | str, run_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    return (
        Path(root).resolve() / ".omg" / "state" / "runs" / run_id / "run-manifest.json"
    )


def expected_trust_root(root: Path | str, run_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    return (
        Path(root).resolve() / ".omg" / "artifacts" / "dual-parity" / run_id / "trust"
    )


def _manifest_lock(root: Path | str, run_id: str) -> Path:
    return expected_manifest_path(root, run_id).with_name("run-manifest.lock")


def _validate_repo(repository_id: str) -> str:
    if repository_id not in REPOSITORY_OWNER_ROWS:
        raise ContractValidationError("repository_id must be OMG or OMA")
    return repository_id


def _validate_normative_hashes(value: Mapping[str, Any]) -> dict[str, str]:
    hashes = require_object(value, label="normative_artifact_hashes")
    if tuple(sorted(hashes)) != tuple(sorted(NORMATIVE_ARTIFACT_KEYS)):
        raise ContractValidationError(
            f"normative_artifact_hashes keys must be {NORMATIVE_ARTIFACT_KEYS!r}"
        )
    return {
        name: require_sha256(hashes[name], label=name)
        for name in NORMATIVE_ARTIFACT_KEYS
    }


def _create_trust_layout(
    root: Path,
    *,
    run_id: str,
    repository_id: str,
    created_at: str,
) -> tuple[dict[str, Any], Path]:
    trust_dir = expected_trust_root(root, run_id)
    if trust_dir.exists():
        raise FileExistsError(f"trust root already exists: {trust_dir}")
    keys_dir = ensure_managed_dir(trust_dir / "keys")
    owner_rows: list[dict[str, Any]] = []
    for wave, owner in REPOSITORY_OWNER_ROWS[repository_id]:
        key = secrets.token_bytes(32)
        key_path = keys_dir / f"{wave}.hmac"
        atomic_write_bytes(key_path, key, mode=DATA_FILE_MODE, replace=False)
        owner_rows.append(
            {
                "wave": wave,
                "owner": owner,
                "key_id": f"{wave}-hmac-{sha256_hex(key)[:16]}",
                "key_sha256": sha256_hex(key),
                "key_path": str(key_path.relative_to(root)),
            }
        )
    aggregate_key = secrets.token_bytes(32)
    aggregate_path = keys_dir / f"{repository_id}-W6-aggregate.hmac"
    atomic_write_bytes(
        aggregate_path, aggregate_key, mode=DATA_FILE_MODE, replace=False
    )
    aggregate_key_id = f"{repository_id}-W6-aggregate-{sha256_hex(aggregate_key)[:16]}"
    trust = {
        "store_kind": "writer_trust_root",
        "schema_version": 1,
        "repository_id": repository_id,
        "run_id": run_id,
        "created_at": created_at,
        "owners": owner_rows,
        "aggregate": {
            "signer_id": f"{repository_id}-W6-aggregate-signer",
            "verifier_id": f"{repository_id}-W6-aggregate-verifier",
            "key_id": aggregate_key_id,
            "key_sha256": sha256_hex(aggregate_key),
            "key_path": str(aggregate_path.relative_to(root)),
        },
        "coordinator_capabilities": [],
    }
    trust_path = trust_dir / "writer-trust.json"
    atomic_write_bytes(
        trust_path, canonical_json_bytes(trust), mode=DATA_FILE_MODE, replace=False
    )
    return trust, trust_path


def initialize_run_manifest(
    root: Path | str,
    *,
    repository_id: str,
    run_id: str,
    frozen_base_commit: str,
    frozen_base_tree: str,
    approved_branch: str,
    approved_remote: str,
    approved_remote_old_oid: str,
    normative_artifact_hashes: Mapping[str, str],
    ownership_manifest_hash: str,
    claimed_release_channels: Sequence[str],
    claimed_registries: Sequence[Mapping[str, Any]] = (),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create manifest+trust from the only legal creation edge."""

    repository_id = _validate_repo(repository_id)
    require_safe_id(run_id, label="run_id")
    root_path = Path(root).resolve()
    timestamp = created_at or _utc_now()
    require_iso8601(timestamp, label="created_at")
    hashes = _validate_normative_hashes(normative_artifact_hashes)
    for label, digest in (
        ("frozen_base_commit", frozen_base_commit),
        ("frozen_base_tree", frozen_base_tree),
        ("approved_remote_old_oid", approved_remote_old_oid),
    ):
        require_git_oid(digest, label=label)
    require_sha256(ownership_manifest_hash, label="ownership_manifest_hash")
    require_nonempty_string(approved_branch, label="approved_branch")
    require_nonempty_string(approved_remote, label="approved_remote")
    channels = list(claimed_release_channels)
    if not channels or any(not isinstance(item, str) or not item for item in channels):
        raise ContractValidationError(
            "claimed_release_channels must be non-empty strings"
        )
    if len(channels) != len(set(channels)):
        raise ContractValidationError("claimed_release_channels must be unique")
    registries = [dict(item) for item in claimed_registries]
    _validate_repository_policy(
        repository_id,
        claimed_release_channels=channels,
        claimed_registries=registries,
        ownership_manifest_hash=ownership_manifest_hash,
    )
    destination = expected_manifest_path(root_path, run_id)
    ensure_managed_dir(destination.parent)
    with exclusive_lock(_manifest_lock(root_path, run_id)):
        if destination.exists():
            raise FileExistsError(destination)
        trust_dir = expected_trust_root(root_path, run_id)
        try:
            trust, trust_path = _create_trust_layout(
                root_path,
                run_id=run_id,
                repository_id=repository_id,
                created_at=timestamp,
            )
            trust_hash = sha256_hex(canonical_json_bytes(trust))
            owner_rows = [
                {
                    "wave": row["wave"],
                    "owner": row["owner"],
                    "key_id": row["key_id"],
                    "key_sha256": row["key_sha256"],
                }
                for row in trust["owners"]
            ]
            aggregate = trust["aggregate"]
            manifest = {
                "store_kind": "dual_parity_run_manifest",
                "schema_version": 1,
                "repository_id": repository_id,
                "repository_realpath_hash": sha256_hex(str(root_path).encode("utf-8")),
                "run_id": run_id,
                "revision": 1,
                "previous_manifest_hash": None,
                "state": "initializing",
                "frozen_base_commit": frozen_base_commit,
                "frozen_base_tree": frozen_base_tree,
                "approved_branch": approved_branch,
                "approved_remote": approved_remote,
                "approved_remote_old_oid": approved_remote_old_oid,
                "normative_artifact_hashes": hashes,
                "ownership_manifest_id": "dual-parity-writers-v1",
                "ownership_manifest_hash": ownership_manifest_hash,
                "trust_root_path": str(trust_path.relative_to(root_path)),
                "trust_root_hash": trust_hash,
                "ordered_owners": owner_rows,
                "aggregate_signer_id": aggregate["signer_id"],
                "aggregate_verifier_id": aggregate["verifier_id"],
                "aggregate_key_id": aggregate["key_id"],
                "aggregate_key_sha256": aggregate["key_sha256"],
                "claimed_release_channels": channels,
                "claimed_registries": registries,
                "lease_generation": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            validate_run_manifest(
                manifest, root=root_path, path=destination, verify_trust=False
            )
            atomic_write_bytes(
                destination,
                canonical_json_bytes(manifest),
                mode=DATA_FILE_MODE,
                replace=False,
            )
        except Exception:
            if not destination.exists() and trust_dir.exists():
                # The trust directory did not become authoritative; remove only
                # files created by this invocation.
                for child in sorted(trust_dir.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        child.rmdir()
                trust_dir.rmdir()
            raise
    return dict(manifest)


RUN_MANIFEST_REQUIRED_KEYS = {
    "store_kind",
    "schema_version",
    "repository_id",
    "repository_realpath_hash",
    "run_id",
    "revision",
    "previous_manifest_hash",
    "state",
    "frozen_base_commit",
    "frozen_base_tree",
    "approved_branch",
    "approved_remote",
    "approved_remote_old_oid",
    "normative_artifact_hashes",
    "ownership_manifest_id",
    "ownership_manifest_hash",
    "trust_root_path",
    "trust_root_hash",
    "ordered_owners",
    "aggregate_signer_id",
    "aggregate_verifier_id",
    "aggregate_key_id",
    "aggregate_key_sha256",
    "claimed_release_channels",
    "claimed_registries",
    "lease_generation",
    "created_at",
    "updated_at",
}


def validate_run_manifest(
    value: Mapping[str, Any],
    *,
    root: Path | str,
    path: Path | str,
    verify_trust: bool = True,
) -> dict[str, Any]:
    manifest = require_object(value, label="run manifest")
    require_exact_keys(
        manifest, required=RUN_MANIFEST_REQUIRED_KEYS, label="run manifest"
    )
    if manifest["store_kind"] != "dual_parity_run_manifest":
        raise ContractValidationError("run manifest store_kind mismatch")
    if manifest["schema_version"] != 1 or isinstance(manifest["schema_version"], bool):
        raise ContractValidationError("run manifest schema_version must be integer 1")
    repository = _validate_repo(manifest["repository_id"])
    run_id = require_safe_id(manifest["run_id"], label="run_id")
    root_path = Path(root).resolve()
    expected = expected_manifest_path(root_path, run_id)
    if Path(path).resolve(strict=False) != expected.resolve(strict=False):
        raise ContractValidationError(
            "run manifest path is not repository authoritative path"
        )
    if manifest["repository_realpath_hash"] != sha256_hex(
        str(root_path).encode("utf-8")
    ):
        raise ContractValidationError("run manifest repository realpath mismatch")
    require_integer(manifest["revision"], label="revision", minimum=1)
    if manifest["revision"] == 1:
        if manifest["previous_manifest_hash"] is not None:
            raise ContractValidationError(
                "creation manifest previous hash must be null"
            )
    else:
        require_sha256(
            manifest["previous_manifest_hash"], label="previous_manifest_hash"
        )
    if manifest["state"] not in RUN_MANIFEST_STATE_SET:
        raise ContractValidationError("run manifest state is outside frozen set")
    for label in (
        "frozen_base_commit",
        "frozen_base_tree",
        "approved_remote_old_oid",
    ):
        require_git_oid(manifest[label], label=label)
    for label in (
        "ownership_manifest_hash",
        "trust_root_hash",
        "aggregate_key_sha256",
    ):
        require_sha256(manifest[label], label=label)
    require_nonempty_string(manifest["approved_branch"], label="approved_branch")
    require_nonempty_string(manifest["approved_remote"], label="approved_remote")
    _validate_normative_hashes(manifest["normative_artifact_hashes"])
    if manifest["ownership_manifest_id"] != "dual-parity-writers-v1":
        raise ContractValidationError("ownership manifest ID mismatch")
    expected_trust_path = (
        expected_trust_root(root_path, run_id) / "writer-trust.json"
    ).relative_to(root_path)
    if manifest["trust_root_path"] != str(expected_trust_path):
        raise ContractValidationError("trust root path mismatch")
    owners = manifest["ordered_owners"]
    if not isinstance(owners, list) or len(owners) != 6:
        raise ContractValidationError("ordered_owners must contain exactly six rows")
    expected_rows = REPOSITORY_OWNER_ROWS[repository]
    for row, (wave, owner) in zip(owners, expected_rows, strict=True):
        data = require_object(row, label="owner row")
        require_exact_keys(
            data,
            required={"wave", "owner", "key_id", "key_sha256"},
            label="owner row",
        )
        if data["wave"] != wave or data["owner"] != owner:
            raise ContractValidationError("owner rows are not in frozen W0-W5 order")
        require_nonempty_string(data["key_id"], label="key_id")
        require_sha256(data["key_sha256"], label="key_sha256")
    if manifest["aggregate_signer_id"] != f"{repository}-W6-aggregate-signer":
        raise ContractValidationError("aggregate signer ID mismatch")
    if manifest["aggregate_verifier_id"] != f"{repository}-W6-aggregate-verifier":
        raise ContractValidationError("aggregate verifier ID mismatch")
    require_nonempty_string(manifest["aggregate_key_id"], label="aggregate_key_id")
    if not isinstance(manifest["claimed_release_channels"], list):
        raise ContractValidationError("claimed_release_channels must be an array")
    if not isinstance(manifest["claimed_registries"], list):
        raise ContractValidationError("claimed_registries must be an array")
    _validate_repository_policy(
        repository,
        claimed_release_channels=manifest["claimed_release_channels"],
        claimed_registries=manifest["claimed_registries"],
        ownership_manifest_hash=str(manifest["ownership_manifest_hash"]),
    )
    require_integer(manifest["lease_generation"], label="lease_generation", minimum=1)
    require_iso8601(manifest["created_at"], label="created_at")
    require_iso8601(manifest["updated_at"], label="updated_at")
    if verify_trust:
        _verify_trust(root_path, manifest)
    return manifest


def _verify_trust(root: Path, manifest: Mapping[str, Any]) -> None:
    trust_path = root / str(manifest["trust_root_path"])
    if not trust_path.is_file() or trust_path.is_symlink():
        raise ContractValidationError("trust root is missing or unsafe")
    if mode_bits(trust_path) != DATA_FILE_MODE:
        raise ContractValidationError("trust root mode must be 0600")
    body = trust_path.read_bytes()
    trust = parse_canonical_json_bytes(body)
    if not isinstance(trust, dict):
        raise ContractValidationError("trust root must be an object")
    if sha256_hex(body) != manifest["trust_root_hash"]:
        raise ContractValidationError("trust root hash mismatch")
    if (
        trust.get("repository_id") != manifest["repository_id"]
        or trust.get("run_id") != manifest["run_id"]
    ):
        raise ContractValidationError("trust root repository/run mismatch")
    if trust.get("coordinator_capabilities") != []:
        raise ContractValidationError("coordinator must receive no trust capability")
    trust_owners = trust.get("owners")
    if not isinstance(trust_owners, list) or len(trust_owners) != 6:
        raise ContractValidationError("trust root must contain six owners")
    for pinned, row in zip(manifest["ordered_owners"], trust_owners, strict=True):
        if any(
            pinned[field] != row.get(field)
            for field in ("wave", "owner", "key_id", "key_sha256")
        ):
            raise ContractValidationError("trust owner row differs from manifest")
        key_path = root / str(row.get("key_path", ""))
        if (
            not key_path.is_file()
            or key_path.is_symlink()
            or mode_bits(key_path) != DATA_FILE_MODE
        ):
            raise ContractValidationError("owner key path/mode mismatch")
        key = key_path.read_bytes()
        if len(key) != 32 or sha256_hex(key) != row["key_sha256"]:
            raise ContractValidationError("owner key digest mismatch")
    aggregate = require_object(trust.get("aggregate"), label="trust aggregate")
    if aggregate.get("signer_id") != manifest["aggregate_signer_id"]:
        raise ContractValidationError("trust aggregate signer mismatch")
    if aggregate.get("verifier_id") != manifest["aggregate_verifier_id"]:
        raise ContractValidationError("trust aggregate verifier mismatch")
    if aggregate.get("key_id") != manifest["aggregate_key_id"]:
        raise ContractValidationError("trust aggregate key ID mismatch")
    aggregate_path = root / str(aggregate.get("key_path", ""))
    if (
        not aggregate_path.is_file()
        or aggregate_path.is_symlink()
        or mode_bits(aggregate_path) != DATA_FILE_MODE
    ):
        raise ContractValidationError("aggregate key path/mode mismatch")
    key = aggregate_path.read_bytes()
    if len(key) != 32 or sha256_hex(key) != manifest["aggregate_key_sha256"]:
        raise ContractValidationError("aggregate key digest mismatch")


def read_run_manifest(
    path: Path | str, *, root: Path | str | None = None
) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ContractValidationError("run manifest is missing or unsafe")
    if mode_bits(manifest_path) != DATA_FILE_MODE:
        raise ContractValidationError("run manifest mode must be 0600")
    body = manifest_path.read_bytes()
    parsed = parse_canonical_json_bytes(body)
    if not isinstance(parsed, dict):
        raise ContractValidationError("run manifest must be an object")
    if root is None:
        try:
            root_path = manifest_path.resolve().parents[4]
        except IndexError as exc:
            raise ContractValidationError(
                "cannot derive repository root from manifest"
            ) from exc
    else:
        root_path = Path(root).resolve()
    manifest = validate_run_manifest(parsed, root=root_path, path=manifest_path)
    journal = _final_sign_transaction_path(
        root_path,
        str(manifest["repository_id"]),
        str(manifest["run_id"]),
    )
    if journal.exists():
        raise ContractValidationError(
            "final signing transaction recovery is pending"
        )
    if manifest["state"] == "closed":
        _read_and_verify_release_completion_evidence(
            root_path, manifest_path, manifest
        )
    return manifest


IMMUTABLE_MANIFEST_FIELDS = frozenset(
    RUN_MANIFEST_REQUIRED_KEYS
    - {
        "revision",
        "previous_manifest_hash",
        "state",
        "lease_generation",
        "updated_at",
    }
)


def transition_run_manifest(
    path: Path | str,
    *,
    expected_revision: int,
    expected_previous_manifest_hash: str | None,
    expected_state: str,
    next_state: str,
    expected_lease_generation: int,
    updated_at: str | None = None,
) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    root = manifest_path.parents[4]
    run_id = manifest_path.parent.name
    with exclusive_lock(_manifest_lock(root, run_id)):
        _recover_final_sign_transaction(root, manifest_path)
        before_body = manifest_path.read_bytes()
        before = read_run_manifest(manifest_path, root=root)
        if expected_state == "composition_active" and next_state == "signing_revoked":
            raise ContractValidationError(
                "composition_active->signing_revoked is reserved for atomic final signing"
            )
        if expected_state == "release_active" and next_state == "closed":
            raise ContractValidationError(
                "release_active->closed is reserved for verified release finalization"
            )
        if (
            expected_state == "inputs_verified" and next_state == "composition_active"
        ) or (expected_state == "signing_revoked" and next_state == "release_active"):
            trust = _load_trust(root, before)
            aggregate, _aggregate_path, _aggregate_body = (
                _read_repository_aggregate_store(root, before)
            )
            phase = "input" if expected_state == "inputs_verified" else "final"
            expected_store_revision = 1 if phase == "input" else 2
            envelope = aggregate[
                "input_envelope" if phase == "input" else "final_envelope"
            ]
            if aggregate["revision"] != expected_store_revision or envelope is None:
                raise ContractValidationError(
                    f"{expected_state}->{next_state} requires canonical {phase} aggregate"
                )
            _verify_repository_aggregate_unlocked(
                manifest_path,
                phase=phase,
                envelope=require_object(envelope, label=f"{phase} aggregate envelope"),
                root=root,
                manifest=before,
                trust=trust,
            )
        after = _build_manifest_transition(
            before,
            before_body=before_body,
            root=root,
            manifest_path=manifest_path,
            expected_revision=expected_revision,
            expected_previous_manifest_hash=expected_previous_manifest_hash,
            expected_state=expected_state,
            next_state=next_state,
            expected_lease_generation=expected_lease_generation,
            updated_at=updated_at,
        )
        atomic_write_bytes(
            manifest_path, canonical_json_bytes(after), mode=DATA_FILE_MODE
        )
    return after


def expected_release_completion_evidence_path(
    root: Path | str, repository_id: str, run_id: str
) -> Path:
    return expected_repository_aggregate_path(root, repository_id, run_id).with_name(
        "release-completion-evidence.json"
    )


def _release_completion_inputs(
    root: Path,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    aggregate, _path, _body = _read_repository_aggregate_store(root, manifest)
    final_envelope = require_object(
        aggregate.get("final_envelope"), label="final aggregate envelope"
    )
    payload = require_object(
        final_envelope.get("payload"), label="final aggregate payload"
    )
    assets = payload.get("public_upload_order")
    if not isinstance(assets, list) or not all(
        isinstance(item, str) and item for item in assets
    ):
        raise ContractValidationError(
            "final aggregate public_upload_order is malformed"
        )
    registries = [
        require_safe_id(row.get("registry_id"), label="registry_id")
        for row in manifest["claimed_registries"]
        if isinstance(row, Mapping)
    ]
    if len(registries) != len(manifest["claimed_registries"]):
        raise ContractValidationError("claimed registry rows are malformed")
    return payload, registries, list(assets)


def _validate_release_completion_for_manifest(
    root: Path,
    manifest: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    release_active_manifest_sha256: str,
) -> dict[str, Any]:
    payload, registries, assets = _release_completion_inputs(root, manifest)
    return validate_release_completion_evidence(
        evidence,
        repository_id=str(manifest["repository_id"]),
        run_id=str(manifest["run_id"]),
        semver=str(payload["semver"]),
        frozen_commit=str(payload["final_commit"]),
        transaction_nonce=str(payload["release_nonce"]),
        release_active_manifest_sha256=release_active_manifest_sha256,
        release_bundle_manifest_sha256=str(
            payload["release_bundle_manifest_sha256"]
        ),
        claimed_release_channels=list(manifest["claimed_release_channels"]),
        registry_ids=registries,
        asset_names=assets,
    )


def _read_and_verify_release_completion_evidence(
    root: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_path = expected_release_completion_evidence_path(
        root, str(manifest["repository_id"]), str(manifest["run_id"])
    )
    try:
        before = evidence_path.lstat()
    except OSError as exc:
        raise ContractValidationError(
            "closed run is missing immutable release completion evidence"
        ) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != IMMUTABLE_SOURCE_MODE
    ):
        raise ContractValidationError(
            "release completion evidence must be a regular 0400 file"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(evidence_path, flags)
    except OSError as exc:
        raise ContractValidationError(
            "release completion evidence is unsafe"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        body = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            body += chunk
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_after = evidence_path.lstat()
    except OSError as exc:
        raise ContractValidationError(
            "release completion evidence changed during verification"
        ) from exc
    def identity(row: os.stat_result) -> tuple[int, int, int, int]:
        return (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != IMMUTABLE_SOURCE_MODE
        or stat.S_ISLNK(path_after.st_mode)
        or identity(before) != identity(opened)
        or identity(opened) != identity(after)
        or identity(after) != identity(path_after)
    ):
        raise ContractValidationError(
            "release completion evidence changed during verification"
        )
    parsed = parse_canonical_json_bytes(body)
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != body:
        raise ContractValidationError(
            "release completion evidence must be canonical JSON"
        )
    return _validate_release_completion_for_manifest(
        root,
        manifest,
        parsed,
        release_active_manifest_sha256=str(manifest["previous_manifest_hash"]),
    )


def finalize_release_run_manifest(
    path: Path | str,
    *,
    expected_revision: int,
    expected_previous_manifest_hash: str | None,
    expected_lease_generation: int,
    evidence: Mapping[str, Any],
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Close a release-active run only through immutable verified evidence."""

    manifest_path = Path(path).resolve()
    root = manifest_path.parents[4]
    run_id = manifest_path.parent.name
    with exclusive_lock(_manifest_lock(root, run_id)):
        _recover_final_sign_transaction(root, manifest_path)
        before_body = manifest_path.read_bytes()
        before = read_run_manifest(manifest_path, root=root)
        if before["state"] != "release_active":
            raise ContractValidationError(
                "release finalization requires release_active state"
            )
        validated = _validate_release_completion_for_manifest(
            root,
            before,
            evidence,
            release_active_manifest_sha256=sha256_hex(before_body),
        )
        completion_path = expected_release_completion_evidence_path(
            root, str(before["repository_id"]), str(before["run_id"])
        )
        completion_body = canonical_json_bytes(validated)
        if completion_path.exists():
            if (
                completion_path.is_symlink()
                or mode_bits(completion_path) != IMMUTABLE_SOURCE_MODE
                or completion_path.read_bytes() != completion_body
            ):
                raise ContractValidationError(
                    "existing release completion evidence conflicts"
                )
        else:
            atomic_write_bytes(
                completion_path,
                completion_body,
                mode=IMMUTABLE_SOURCE_MODE,
                replace=False,
            )
        after = _build_manifest_transition(
            before,
            before_body=before_body,
            root=root,
            manifest_path=manifest_path,
            expected_revision=expected_revision,
            expected_previous_manifest_hash=expected_previous_manifest_hash,
            expected_state="release_active",
            next_state="closed",
            expected_lease_generation=expected_lease_generation,
            updated_at=updated_at,
        )
        atomic_write_bytes(
            manifest_path, canonical_json_bytes(after), mode=DATA_FILE_MODE
        )
        _read_and_verify_release_completion_evidence(root, manifest_path, after)
    return after


def _build_manifest_transition(
    before: Mapping[str, Any],
    *,
    before_body: bytes,
    root: Path,
    manifest_path: Path,
    expected_revision: int,
    expected_previous_manifest_hash: str | None,
    expected_state: str,
    next_state: str,
    expected_lease_generation: int,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Validate and construct one CAS transition while the caller holds the lock."""

    if before["revision"] != expected_revision:
        raise ContractValidationError("run manifest revision CAS mismatch")
    if before["previous_manifest_hash"] != expected_previous_manifest_hash:
        raise ContractValidationError("run manifest previous hash CAS mismatch")
    if before["state"] != expected_state:
        raise ContractValidationError("run manifest state predecessor mismatch")
    if before["lease_generation"] != expected_lease_generation:
        raise ContractValidationError("run manifest lease generation mismatch")
    if next_state not in RUN_MANIFEST_TRANSITIONS[expected_state]:
        raise ContractValidationError(
            f"illegal run manifest transition {expected_state}->{next_state}"
        )
    timestamp = updated_at or _utc_now()
    require_iso8601(timestamp, label="updated_at")
    after = dict(before)
    after.update(
        {
            "revision": expected_revision + 1,
            "previous_manifest_hash": sha256_hex(before_body),
            "state": next_state,
            "lease_generation": expected_lease_generation + 1,
            "updated_at": timestamp,
        }
    )
    for field in IMMUTABLE_MANIFEST_FIELDS:
        if after[field] != before[field]:  # pragma: no cover - construction invariant
            raise ContractValidationError(f"immutable manifest field drifted: {field}")
    validate_run_manifest(after, root=root, path=manifest_path)
    return after


def _write_exact_or_adopt(path: Path, body: bytes) -> None:
    if path.exists():
        if (
            path.is_symlink()
            or path.read_bytes() != body
            or mode_bits(path) != DATA_FILE_MODE
        ):
            raise ContractValidationError(
                f"existing authenticated artifact differs: {path}"
            )
        return
    atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)


def _read_authenticated_artifact(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ContractValidationError(f"{label} is missing or unsafe")
    if mode_bits(path) != DATA_FILE_MODE:
        raise ContractValidationError(f"{label} mode must be 0600")
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError(f"{label} must be an object")
    return parsed


def _load_trust(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    # ``read_run_manifest`` already validates this file and every pinned key;
    # read it again only to obtain the exact owner key paths without returning
    # key material across the public API.
    return _read_authenticated_artifact(
        root / str(manifest["trust_root_path"]), label="trust root"
    )


def _owner_key(
    root: Path,
    manifest: Mapping[str, Any],
    trust: Mapping[str, Any],
    wave: str,
) -> tuple[str, bytes]:
    manifest_rows = [row for row in manifest["ordered_owners"] if row["wave"] == wave]
    trust_rows = [row for row in trust["owners"] if row.get("wave") == wave]
    if len(manifest_rows) != 1 or len(trust_rows) != 1:
        raise ContractValidationError(f"missing pinned owner identity for {wave}")
    manifest_row = manifest_rows[0]
    trust_row = trust_rows[0]
    for field in ("wave", "owner", "key_id", "key_sha256"):
        if trust_row.get(field) != manifest_row[field]:
            raise ContractValidationError(f"trust owner identity drift for {wave}")
    key_path = root / str(trust_row.get("key_path", ""))
    if (
        not key_path.is_file()
        or key_path.is_symlink()
        or mode_bits(key_path) != DATA_FILE_MODE
    ):
        raise ContractValidationError(f"owner key path/mode mismatch for {wave}")
    key = key_path.read_bytes()
    if len(key) != 32 or sha256_hex(key) != manifest_row["key_sha256"]:
        raise ContractValidationError(f"owner key digest mismatch for {wave}")
    return str(manifest_row["owner"]), key


def _assert_manifest_binding(
    payload: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_relative_path: str,
    manifest_hash: str,
    manifest_revision: int | None = None,
    lease_generation: int | None = None,
) -> None:
    expected = {
        "repository_id": manifest["repository_id"],
        "run_id": manifest["run_id"],
        "run_manifest_path": manifest_relative_path,
        "run_manifest_revision": (
            manifest["revision"] if manifest_revision is None else manifest_revision
        ),
        "run_manifest_hash": manifest_hash,
        "frozen_base_commit": manifest["frozen_base_commit"],
        "frozen_base_tree": manifest["frozen_base_tree"],
        "lease_generation": (
            manifest["lease_generation"]
            if lease_generation is None
            else lease_generation
        ),
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ContractValidationError(f"stale or foreign parent {field}")


def _verify_parent_wave(
    *,
    root: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    trust: Mapping[str, Any],
    wave: str,
    memo: dict[str, str],
    visiting: set[str],
    binding_manifest_hash: str | None = None,
    binding_manifest_revision: int | None = None,
    binding_lease_generation: int | None = None,
    verify_current_paths: bool = False,
    evidence: dict[str, dict[str, Any]] | None = None,
    entries_by_wave: dict[str, list[Mapping[str, Any]]] | None = None,
) -> str:
    if wave in memo:
        return memo[wave]
    if wave in visiting:  # pragma: no cover - frozen oracle is acyclic
        raise ContractValidationError("parent handoff graph contains a cycle")
    visiting.add(wave)
    direct_parent_hashes = VerifiedParentHashes()
    for parent_wave in expected_parent_waves(wave):
        direct_parent_hashes[parent_wave] = _verify_parent_wave(
            root=root,
            manifest_path=manifest_path,
            manifest=manifest,
            trust=trust,
            wave=parent_wave,
            memo=memo,
            visiting=visiting,
            binding_manifest_hash=binding_manifest_hash,
            binding_manifest_revision=binding_manifest_revision,
            binding_lease_generation=binding_lease_generation,
            verify_current_paths=verify_current_paths,
            evidence=evidence,
            entries_by_wave=entries_by_wave,
        )

    owner, key = _owner_key(root, manifest, trust, wave)
    artifact_dir = (
        root / ".omg" / "artifacts" / "dual-parity" / str(manifest["run_id"]) / wave
    )
    proposal_path = artifact_dir / "proposal-index.json"
    handoff_path = artifact_dir / "handoff.json"
    proposal_envelope = _read_authenticated_artifact(
        proposal_path, label=f"{wave} proposal index"
    )
    handoff_envelope = _read_authenticated_artifact(
        handoff_path, label=f"{wave} handoff"
    )
    proposal_hash = verify_proposal_index(
        proposal_envelope,
        key,
        expected_repository=str(manifest["repository_id"]),
        expected_run_id=str(manifest["run_id"]),
        expected_wave=wave,
        expected_owner=owner,
        trusted_parent_hashes=direct_parent_hashes,
        repository_root=root,
    )
    digest = verify_handoff(
        handoff_envelope,
        key,
        expected_repository=str(manifest["repository_id"]),
        expected_run_id=str(manifest["run_id"]),
        expected_wave=wave,
        expected_owner=owner,
        trusted_parent_hashes=direct_parent_hashes,
    )
    proposal_payload = require_object(
        proposal_envelope.get("signed_payload"), label="proposal signed_payload"
    )
    handoff_payload = require_object(
        handoff_envelope.get("signed_payload"), label="handoff signed_payload"
    )
    manifest_relative_path = str(manifest_path.relative_to(root))
    manifest_hash = binding_manifest_hash or sha256_hex(manifest_path.read_bytes())
    for payload in (proposal_payload, handoff_payload):
        _assert_manifest_binding(
            payload,
            manifest=manifest,
            manifest_relative_path=manifest_relative_path,
            manifest_hash=manifest_hash,
            manifest_revision=binding_manifest_revision,
            lease_generation=binding_lease_generation,
        )
    if handoff_payload.get("proposal_index_path") != str(
        proposal_path.relative_to(root)
    ):
        raise ContractValidationError("parent handoff proposal path mismatch")
    if handoff_payload.get("proposal_index_hash") != proposal_hash:
        raise ContractValidationError("parent handoff proposal hash mismatch")
    entries = proposal_payload.get("entries")
    records = handoff_payload.get("path_records")
    if (
        not isinstance(entries, list)
        or not isinstance(records, list)
        or len(entries) != len(records)
    ):
        raise ContractValidationError(
            "parent proposal/handoff path cardinality mismatch"
        )
    for entry, record in zip(entries, records, strict=True):
        expected_record = {**entry, "proposal_hash": proposal_hash}
        if record != expected_record:
            raise ContractValidationError(
                "parent proposal/handoff path record mismatch"
            )
    if verify_current_paths:
        _verify_proposal_paths_current(
            root,
            entries,
            base_commit=str(manifest["frozen_base_commit"]),
        )
    if entries_by_wave is not None:
        entries_by_wave[wave] = [dict(entry) for entry in entries]
    if evidence is not None:
        requests = [dict(item) for item in proposal_payload["w6_requests"]]
        evidence[wave] = {
            "wave": wave,
            "owner": owner,
            "proposal_index_path": str(proposal_path.relative_to(root)),
            "proposal_index_hash": proposal_hash,
            "handoff_path": str(handoff_path.relative_to(root)),
            "handoff_hash": digest,
            "dependency_parent_handoff_hashes": list(
                handoff_payload["parent_handoff_hashes"]
            ),
            "path_test_root": sha256_hex(canonical_json_bytes(entries)),
            "w6_requests": requests,
        }
    visiting.remove(wave)
    memo[wave] = digest
    return digest


def _verified_parent_hashes(
    *,
    root: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    wave: str,
) -> VerifiedParentHashes:
    trust = _load_trust(root, manifest)
    memo: dict[str, str] = {}
    result = VerifiedParentHashes()
    for parent_wave in expected_parent_waves(wave):
        result[parent_wave] = _verify_parent_wave(
            root=root,
            manifest_path=manifest_path,
            manifest=manifest,
            trust=trust,
            wave=parent_wave,
            memo=memo,
            visiting=set(),
        )
    return result


def _read_current_proposal_path(root: Path, relative_path: str) -> bytes | None:
    candidate = root / relative_path
    current = root
    for part in Path(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise ContractValidationError(
                f"proposal path contains a symlink: {relative_path}"
            )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContractValidationError(
            f"proposal path is missing or unsafe: {relative_path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractValidationError(
                f"proposal path must be a regular file: {relative_path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _git_bytes(root: Path, argv: Sequence[str], *, label: str) -> bytes:
    result = subprocess.run(
        ["git", *argv],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ContractValidationError(
            f"{label} failed rc={result.returncode}: "
            f"{result.stderr.decode('utf-8', errors='replace')[:300]}"
        )
    return result.stdout


def _base_path_bytes(root: Path, base_commit: str, relative_path: str) -> bytes | None:
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{base_commit}:{relative_path}"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode != 0:
        return None
    return _git_bytes(
        root,
        ["show", f"{base_commit}:{relative_path}"],
        label=f"read frozen base path {relative_path}",
    )


def _expected_omg_owner_paths(root: Path, *, base_commit: str) -> dict[str, set[str]]:
    _git_bytes(
        root,
        ["cat-file", "-e", f"{base_commit}^{{commit}}"],
        label="validate frozen base commit",
    )
    base_paths = {
        item.decode("utf-8")
        for item in _git_bytes(
            root,
            ["ls-tree", "-r", "--name-only", "-z", base_commit],
            label="enumerate frozen base paths",
        ).split(b"\0")
        if item
    }
    current_paths = {
        item.decode("utf-8")
        for item in _git_bytes(
            root,
            ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            label="enumerate current repository paths",
        ).split(b"\0")
        if item
    }
    universe = base_paths | current_paths
    expected: dict[str, set[str]] = {}
    for wave in (f"OMG-W{index}" for index in range(6)):
        paths: set[str] = set()
        for pattern in OMG_OWNER_PATTERNS[wave]:
            if any(character in pattern for character in "*?["):
                paths.update(
                    candidate
                    for candidate in universe
                    if path_matches_pattern(candidate, pattern)
                )
            else:
                paths.add(pattern)
        if not paths:
            raise ContractValidationError(
                f"ownership oracle resolved no paths for {wave}"
            )
        for candidate in paths:
            if owner_for_path(candidate, OMG_OWNER_PATTERNS) != wave:
                raise ContractValidationError(
                    f"ownership oracle path does not resolve uniquely to {wave}: {candidate}"
                )
        expected[wave] = paths
    return expected


def _verify_proposal_paths_current(
    root: Path,
    entries: Sequence[Mapping[str, Any]],
    *,
    base_commit: str,
) -> None:
    for entry in entries:
        relative_path = str(entry["path"])
        test = require_object(entry.get("targeted_test"), label="targeted_test")
        if require_integer(test.get("rc"), label="targeted_test.rc") != 0:
            raise ContractValidationError(
                f"targeted_test must pass for proposal path: {relative_path}"
            )
        initial = _base_path_bytes(root, base_commit, relative_path)
        expected_initial = "ABSENT" if initial is None else sha256_hex(initial)
        if entry["initial_sha256"] != expected_initial:
            raise ContractValidationError(
                f"proposal path frozen-base bytes differ from signed initial hash: {relative_path}"
            )
        current = _read_current_proposal_path(root, relative_path)
        expected = str(entry["final_sha256"])
        if expected == "ABSENT":
            if current is not None:
                raise ContractValidationError(
                    f"proposal path expected absent but exists: {relative_path}"
                )
            continue
        if current is None:
            raise ContractValidationError(f"proposal path is missing: {relative_path}")
        if sha256_hex(current) != expected:
            raise ContractValidationError(
                f"proposal path current bytes differ from signed final hash: {relative_path}"
            )


def _verify_complete_owner_path_union(
    root: Path,
    *,
    manifest: Mapping[str, Any],
    entries_by_wave: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    seen: dict[str, str] = {}
    changed: list[dict[str, Any]] = []
    expected = (
        _expected_omg_owner_paths(root, base_commit=str(manifest["frozen_base_commit"]))
        if manifest["repository_id"] == "OMG"
        else None
    )
    if manifest["repository_id"] == "OMG":
        changed = verify_dirty_ownership(
            root,
            str(manifest["frozen_base_commit"]),
            OMG_OWNER_PATTERNS,
        )
    for wave, _owner in REPOSITORY_OWNER_ROWS[str(manifest["repository_id"])]:
        entries = entries_by_wave.get(wave)
        if entries is None:
            raise ContractValidationError(
                f"missing authenticated proposal entries for {wave}"
            )
        supplied = {str(entry["path"]) for entry in entries}
        if len(supplied) != len(entries):
            raise ContractValidationError(
                "signed proposal path union contains a duplicate"
            )
        for relative_path in supplied:
            previous = seen.setdefault(relative_path, wave)
            if previous != wave:
                raise ContractValidationError(
                    f"signed proposal path is owned by multiple waves: {relative_path}"
                )
            if manifest["repository_id"] == "OMG":
                actual_owner = owner_for_path(relative_path, OMG_OWNER_PATTERNS)
                if actual_owner != wave:
                    raise ContractValidationError(
                        f"signed proposal path owner differs from oracle: {relative_path}"
                    )
        if expected is not None and supplied != expected[wave]:
            missing = sorted(expected[wave] - supplied)
            extra = sorted(supplied - expected[wave])
            raise ContractValidationError(
                f"signed path union differs from ownership oracle for {wave}: "
                f"missing={missing[:5]!r} extra={extra[:5]!r}"
            )
    for record in changed:
        owner = str(record["owner"])
        if owner == f"{manifest['repository_id']}-W6":
            continue
        changed_paths = [record["path"]]
        if record.get("old_path") is not None:
            changed_paths.append(record["old_path"])
        for relative_path in changed_paths:
            if seen.get(str(relative_path)) != owner:
                raise ContractValidationError(
                    "inclusive changed-path universe is not signed by its exact owner: "
                    f"{relative_path}"
                )


def _verified_input_owner_roots(
    *,
    root: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    input_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if input_payload is None:
        if manifest["state"] != "inputs_verified":
            raise ContractValidationError(
                "input owner-root verification requires authenticated input history"
            )
        previous_hash = require_sha256(
            manifest["previous_manifest_hash"], label="previous_manifest_hash"
        )
        previous_revision = int(manifest["revision"]) - 1
        previous_lease = int(manifest["lease_generation"]) - 1
    else:
        previous_revision = (
            require_integer(
                input_payload.get("run_manifest_revision"),
                label="input run_manifest_revision",
                minimum=2,
            )
            - 1
        )
        previous_lease = (
            require_integer(
                input_payload.get("lease_generation"),
                label="input lease_generation",
                minimum=2,
            )
            - 1
        )
        if (
            manifest["state"] == "inputs_verified"
            and manifest["revision"] == input_payload["run_manifest_revision"]
        ):
            previous_hash = require_sha256(
                manifest["previous_manifest_hash"], label="previous_manifest_hash"
            )
        else:
            first = _read_authenticated_artifact(
                root
                / ".omg"
                / "artifacts"
                / "dual-parity"
                / str(manifest["run_id"])
                / f"{manifest['repository_id']}-W0"
                / "proposal-index.json",
                label=f"{manifest['repository_id']}-W0 proposal index",
            )
            first_payload = require_object(
                first.get("signed_payload"), label="proposal signed_payload"
            )
            previous_hash = require_sha256(
                first_payload.get("run_manifest_hash"),
                label="owner run_manifest_hash",
            )
    if previous_revision < 1 or previous_lease < 1:
        raise ContractValidationError("input owner-root history is invalid")
    trust = _load_trust(root, manifest)
    memo: dict[str, str] = {}
    evidence: dict[str, dict[str, Any]] = {}
    entries_by_wave: dict[str, list[Mapping[str, Any]]] = {}
    for wave, _owner in REPOSITORY_OWNER_ROWS[str(manifest["repository_id"])]:
        _verify_parent_wave(
            root=root,
            manifest_path=manifest_path,
            manifest=manifest,
            trust=trust,
            wave=wave,
            memo=memo,
            visiting=set(),
            binding_manifest_hash=previous_hash,
            binding_manifest_revision=previous_revision,
            binding_lease_generation=previous_lease,
            verify_current_paths=True,
            evidence=evidence,
            entries_by_wave=entries_by_wave,
        )
    _verify_complete_owner_path_union(
        root,
        manifest=manifest,
        entries_by_wave=entries_by_wave,
    )
    ordered = [
        evidence[wave]
        for wave, _owner in REPOSITORY_OWNER_ROWS[str(manifest["repository_id"])]
    ]
    accepted = [
        {"wave": row["wave"], **request}
        for row in ordered
        for request in row["w6_requests"]
    ]
    path_roots = [
        {"wave": row["wave"], "path_test_root": row["path_test_root"]}
        for row in ordered
    ]
    return {
        "ordered_owner_roots": ordered,
        "parent_handoff_hashes": [row["handoff_hash"] for row in ordered],
        "path_test_merkle_root": sha256_hex(canonical_json_bytes(path_roots)),
        "accepted_w6_proposals": accepted,
    }


def emit_owner_handoff(
    manifest_path: Path | str,
    *,
    wave: str,
    owner: str,
    proposal_entries: Sequence[Mapping[str, Any]],
    w6_request_paths: Sequence[Path | str] | None = None,
    parent_handoff_hashes: Sequence[str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Authenticate one owner's proposal index and handoff without exposing its key.

    The operation is idempotent only for byte-identical artifacts.  A crash
    after the proposal write can therefore be resumed safely, while any drift
    fails rather than replacing signed evidence.
    """

    path = Path(manifest_path).resolve()
    root = path.parents[4]
    manifest = read_run_manifest(path, root=root)
    if manifest["state"] != "writers_active":
        raise ContractValidationError(
            "owner handoff requires writers_active manifest state"
        )
    rows = [row for row in manifest["ordered_owners"] if row["wave"] == wave]
    if len(rows) != 1 or rows[0]["owner"] != owner:
        raise ContractValidationError("wave/owner is not an ordered manifest owner")
    trusted_parent_hashes = _verified_parent_hashes(
        root=root,
        manifest_path=path,
        manifest=manifest,
        wave=wave,
    )
    derived_parent_hashes = [
        trusted_parent_hashes[parent_wave]
        for parent_wave in expected_parent_waves(wave)
    ]
    if (
        parent_handoff_hashes is not None
        and list(parent_handoff_hashes) != derived_parent_hashes
    ):
        raise ContractValidationError(
            "asserted parent_handoff_hashes differ from verified artifacts"
        )
    parent_handoff_hashes = derived_parent_hashes
    validate_parent_hashes(wave, parent_handoff_hashes, trusted_parent_hashes)
    entries = [validate_proposal_entry(dict(entry)) for entry in proposal_entries]
    if not entries:
        raise ContractValidationError("proposal index must contain owned paths")
    entries.sort(key=lambda entry: entry["path"].encode("utf-8"))
    for entry in entries:
        for field, expected in (
            ("repository_id", manifest["repository_id"]),
            ("run_id", manifest["run_id"]),
            ("wave", wave),
            ("owner", owner),
        ):
            if entry[field] != expected:
                raise ContractValidationError(
                    f"proposal entry {field} differs from manifest"
                )
    if len({entry["path"] for entry in entries}) != len(entries):
        raise ContractValidationError("proposal index contains duplicate paths")

    w6_requests = [
        build_w6_request_binding(
            root,
            request_path,
            repository_id=str(manifest["repository_id"]),
            run_id=str(manifest["run_id"]),
            wave=wave,
        )
        for request_path in (w6_request_paths or ())
    ]
    w6_requests.sort(key=lambda binding: binding["path"].encode("utf-8"))
    if len({binding["path"] for binding in w6_requests}) != len(w6_requests):
        raise ContractValidationError("w6_requests contains a duplicate path")

    manifest_body = path.read_bytes()
    manifest_hash = sha256_hex(manifest_body)
    run_manifest_relative = str(path.relative_to(root))
    artifact_dir = ensure_managed_dir(
        root / ".omg" / "artifacts" / "dual-parity" / manifest["run_id"] / wave
    )
    proposal_path = artifact_dir / "proposal-index.json"
    handoff_path = artifact_dir / "handoff.json"

    timestamp = created_at
    if timestamp is None and proposal_path.exists():
        existing = parse_canonical_json_bytes(proposal_path.read_bytes())
        if not isinstance(existing, dict):
            raise ContractValidationError("existing proposal index is not an object")
        payload = require_object(existing.get("signed_payload"), label="signed_payload")
        timestamp = payload.get("created_at")
    timestamp = timestamp or _utc_now()
    require_iso8601(timestamp, label="created_at")

    proposal_payload = {
        "store_kind": "owner_proposal_index",
        "schema_version": 1,
        "repository_id": manifest["repository_id"],
        "run_id": manifest["run_id"],
        "wave": wave,
        "owner": owner,
        "run_manifest_path": run_manifest_relative,
        "run_manifest_revision": manifest["revision"],
        "run_manifest_hash": manifest_hash,
        "frozen_base_commit": manifest["frozen_base_commit"],
        "frozen_base_tree": manifest["frozen_base_tree"],
        "lease_generation": manifest["lease_generation"],
        "parent_handoff_hashes": list(parent_handoff_hashes),
        "entries": entries,
        "w6_requests": w6_requests,
        "created_at": timestamp,
    }

    trust = parse_canonical_json_bytes(
        (root / manifest["trust_root_path"]).read_bytes()
    )
    if not isinstance(
        trust, dict
    ):  # pragma: no cover - read_run_manifest verified this
        raise ContractValidationError("trust root is not an object")
    trust_rows = [row for row in trust["owners"] if row.get("wave") == wave]
    if len(trust_rows) != 1 or trust_rows[0].get("owner") != owner:
        raise ContractValidationError("trust root wave/owner mismatch")
    key_path = root / str(trust_rows[0]["key_path"])
    key = key_path.read_bytes()
    proposal_envelope = sign_handoff(proposal_payload, key)
    proposal_hash = handoff_hash(proposal_envelope)
    path_records = [
        {
            **{
                name: entry[name]
                for name in (
                    "repository_id",
                    "run_id",
                    "wave",
                    "owner",
                    "path",
                    "initial_sha256",
                    "final_sha256",
                    "reason",
                    "proposal_id",
                    "targeted_test",
                )
            },
            "proposal_hash": proposal_hash,
        }
        for entry in entries
    ]
    handoff_payload = {
        "store_kind": "owner_handoff",
        "schema_version": 1,
        "repository_id": manifest["repository_id"],
        "run_id": manifest["run_id"],
        "wave": wave,
        "owner": owner,
        "run_manifest_path": run_manifest_relative,
        "run_manifest_revision": manifest["revision"],
        "run_manifest_hash": manifest_hash,
        "frozen_base_commit": manifest["frozen_base_commit"],
        "frozen_base_tree": manifest["frozen_base_tree"],
        "lease_generation": manifest["lease_generation"],
        "proposal_index_path": str(proposal_path.relative_to(root)),
        "proposal_index_hash": proposal_hash,
        "parent_handoff_hashes": list(parent_handoff_hashes),
        "path_records": path_records,
        "created_at": timestamp,
    }
    handoff_envelope = sign_handoff(handoff_payload, key)
    verify_proposal_index(
        proposal_envelope,
        key,
        expected_repository=manifest["repository_id"],
        expected_run_id=manifest["run_id"],
        expected_wave=wave,
        expected_owner=owner,
        trusted_parent_hashes=trusted_parent_hashes,
        repository_root=root,
    )
    verified_handoff_hash = verify_handoff(
        handoff_envelope,
        key,
        expected_repository=manifest["repository_id"],
        expected_run_id=manifest["run_id"],
        expected_wave=wave,
        expected_owner=owner,
        trusted_parent_hashes=trusted_parent_hashes,
    )
    _write_exact_or_adopt(proposal_path, canonical_json_bytes(proposal_envelope))
    _write_exact_or_adopt(handoff_path, canonical_json_bytes(handoff_envelope))
    return {
        "proposal_index_path": str(proposal_path),
        "proposal_index_hash": proposal_hash,
        "handoff_path": str(handoff_path),
        "handoff_hash": verified_handoff_hash,
        "path_count": len(path_records),
    }


AGGREGATE_ENVELOPE_KEYS = {
    "algorithm",
    "signer_id",
    "aggregate_key_id",
    "payload_hash",
    "payload",
    "signature",
}
AGGREGATE_MANIFEST_BINDING_KEYS = {
    "repository_id",
    "run_id",
    "run_manifest_path",
    "run_manifest_revision",
    "run_manifest_hash",
    "lease_generation",
    "frozen_base_commit",
    "frozen_base_tree",
    "approved_branch",
    "approved_remote",
    "approved_remote_old_oid",
    "trust_root_path",
    "trust_root_hash",
    "ownership_manifest_id",
    "ownership_manifest_hash",
    "normative_artifact_hashes",
    "claimed_release_channels",
    "claimed_registries",
}
INPUT_AGGREGATE_PAYLOAD_KEYS = AGGREGATE_MANIFEST_BINDING_KEYS | {
    "store_kind",
    "schema_version",
    "ordered_owner_roots",
    "parent_handoff_hashes",
    "path_test_merkle_root",
    "accepted_w6_proposals",
    "final_commit",
}
FINAL_AGGREGATE_PAYLOAD_KEYS = AGGREGATE_MANIFEST_BINDING_KEYS | {
    "store_kind",
    "schema_version",
    "input_envelope",
    "input_aggregate_hash",
    "final_commit",
    "final_tree",
    "pushed_oid",
    "complete_delta_root",
    "semver",
    "deterministic_proof_hash",
    "live_proof_hash",
    "code_review_proof_hash",
    "ultraqa_proof_hash",
    "release_nonce",
    "release_bundle_manifest_path",
    "release_bundle_manifest_sha256",
    "release_bundle_manifest_schema",
    "public_upload_order",
    "release_asset_root",
}
OWNER_ROOT_KEYS = {
    "wave",
    "owner",
    "proposal_index_path",
    "proposal_index_hash",
    "handoff_path",
    "handoff_hash",
    "dependency_parent_handoff_hashes",
    "path_test_root",
    "w6_requests",
}
ACCEPTED_W6_PROPOSAL_KEYS = {"wave", "path", "byte_length", "sha256"}
GENERATED_OUTPUT_ATTESTATION_KEYS = {
    "store_kind",
    "schema_version",
    "standalone_hook_inputs_hash",
    "generated_output_request_hash",
    "generated_input_request_hash",
    "first_output_hash",
    "second_output_hash",
    "check_receipt",
}
GENERATED_OUTPUT_CHECK_RECEIPT_KEYS = {
    "argv",
    "rc",
    "stdout_sha256",
    "stderr_sha256",
}
AGGREGATE_PHASE_STATES: dict[str, tuple[str, ...]] = {
    "input": (
        "inputs_verified",
        "composition_active",
        "signing_revoked",
        "release_active",
        "closed",
    ),
    "final": (
        "composition_active",
        "signing_revoked",
        "release_active",
        "closed",
    ),
}
REPOSITORY_AGGREGATE_HANDOFF_KEYS = {
    "store_kind",
    "schema_version",
    "repository_id",
    "run_id",
    "revision",
    "previous_aggregate_hash",
    "input_envelope",
    "final_envelope",
}


def expected_repository_aggregate_path(
    root: Path | str, repository_id: str, run_id: str
) -> Path:
    repository = _validate_repo(repository_id)
    require_safe_id(run_id, label="run_id")
    state_root = ".omg" if repository == "OMG" else ".agy"
    return (
        Path(root).resolve()
        / state_root
        / "artifacts"
        / "dual-parity"
        / run_id
        / f"{repository}-W6"
        / "aggregate-handoff.json"
    )


def _validate_repository_aggregate_store(
    value: Mapping[str, Any], *, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    store = require_object(value, label="repository aggregate handoff")
    require_exact_keys(
        store,
        required=REPOSITORY_AGGREGATE_HANDOFF_KEYS,
        label="repository aggregate handoff",
    )
    if (
        store["store_kind"] != "repo_aggregate_handoff"
        or store["schema_version"] != 1
        or isinstance(store["schema_version"], bool)
    ):
        raise ContractValidationError("repository aggregate handoff header mismatch")
    if (
        store["repository_id"] != manifest["repository_id"]
        or store["run_id"] != manifest["run_id"]
    ):
        raise ContractValidationError("repository aggregate handoff identity mismatch")
    revision = require_integer(store["revision"], label="aggregate revision", minimum=1)
    if revision not in {1, 2}:
        raise ContractValidationError("aggregate revision must be 1 or 2")
    require_object(store["input_envelope"], label="input_envelope")
    if revision == 1:
        if (
            store["previous_aggregate_hash"] is not None
            or store["final_envelope"] is not None
        ):
            raise ContractValidationError(
                "input aggregate CAS must have null predecessor/final"
            )
    else:
        previous_hash = require_sha256(
            store["previous_aggregate_hash"], label="previous_aggregate_hash"
        )
        require_object(store["final_envelope"], label="final_envelope")
        predecessor = {
            **store,
            "revision": 1,
            "previous_aggregate_hash": None,
            "final_envelope": None,
        }
        if previous_hash != sha256_hex(canonical_json_bytes(predecessor)):
            raise ContractValidationError(
                "repository aggregate predecessor hash mismatch"
            )
    return store


def _read_repository_aggregate_store(
    root: Path, manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], Path, bytes]:
    path = expected_repository_aggregate_path(
        root, str(manifest["repository_id"]), str(manifest["run_id"])
    )
    store = _read_authenticated_artifact(path, label="repository aggregate handoff")
    body = path.read_bytes()
    return _validate_repository_aggregate_store(store, manifest=manifest), path, body


def _aggregate_domain(phase: str) -> bytes:
    if phase == "input":
        return INPUT_AGGREGATE_DOMAIN
    if phase == "final":
        return FINAL_AGGREGATE_DOMAIN
    raise ContractValidationError("aggregate phase must be input or final")


def _aggregate_key(
    root: Path, manifest: Mapping[str, Any], trust: Mapping[str, Any]
) -> bytes:
    """Load only the separately pinned W6 aggregate key.

    The key never crosses this module's public API.  In addition to the trust
    root hash validation performed by ``read_run_manifest``, pin the exact key
    path and reject accidental/substituted reuse of any W0-W5 owner key.
    """

    aggregate = require_object(trust.get("aggregate"), label="trust aggregate")
    for field, expected in (
        ("signer_id", manifest["aggregate_signer_id"]),
        ("verifier_id", manifest["aggregate_verifier_id"]),
        ("key_id", manifest["aggregate_key_id"]),
        ("key_sha256", manifest["aggregate_key_sha256"]),
    ):
        if aggregate.get(field) != expected:
            raise ContractValidationError(
                f"trust aggregate {field} differs from run manifest"
            )
    expected_path = (
        expected_trust_root(root, str(manifest["run_id"]))
        / "keys"
        / f"{manifest['repository_id']}-W6-aggregate.hmac"
    )
    expected_relative = str(expected_path.relative_to(root))
    if aggregate.get("key_path") != expected_relative:
        raise ContractValidationError("aggregate key is not at its authoritative path")
    if (
        not expected_path.is_file()
        or expected_path.is_symlink()
        or mode_bits(expected_path) != DATA_FILE_MODE
    ):
        raise ContractValidationError("aggregate key path/mode mismatch")
    key = expected_path.read_bytes()
    if len(key) != 32 or sha256_hex(key) != manifest["aggregate_key_sha256"]:
        raise ContractValidationError("aggregate key digest mismatch")
    owner_digests = {str(row["key_sha256"]) for row in manifest["ordered_owners"]}
    if sha256_hex(key) in owner_digests:
        raise ContractValidationError("aggregate key must be distinct from owner keys")
    return key


def _expected_aggregate_manifest_revision(
    manifest: Mapping[str, Any], *, phase: str
) -> tuple[int, int]:
    states = AGGREGATE_PHASE_STATES.get(phase)
    if states is None:
        raise ContractValidationError("aggregate phase must be input or final")
    state = str(manifest["state"])
    if state not in states:
        raise ContractValidationError(
            "aggregate verification is inactive for the current manifest state"
        )
    offset = states.index(state)
    return int(manifest["revision"]) - offset, int(
        manifest["lease_generation"]
    ) - offset


def _validate_aggregate_manifest_binding(
    payload: Mapping[str, Any],
    *,
    root: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    phase: str,
    signing: bool,
) -> None:
    expected_static = {
        "repository_id": manifest["repository_id"],
        "run_id": manifest["run_id"],
        "run_manifest_path": str(manifest_path.relative_to(root)),
        "frozen_base_commit": manifest["frozen_base_commit"],
        "frozen_base_tree": manifest["frozen_base_tree"],
        "approved_branch": manifest["approved_branch"],
        "approved_remote": manifest["approved_remote"],
        "approved_remote_old_oid": manifest["approved_remote_old_oid"],
        "trust_root_path": manifest["trust_root_path"],
        "trust_root_hash": manifest["trust_root_hash"],
        "ownership_manifest_id": manifest["ownership_manifest_id"],
        "ownership_manifest_hash": manifest["ownership_manifest_hash"],
        "normative_artifact_hashes": manifest["normative_artifact_hashes"],
        "claimed_release_channels": manifest["claimed_release_channels"],
        "claimed_registries": manifest["claimed_registries"],
    }
    for field, expected in expected_static.items():
        if payload.get(field) != expected:
            raise ContractValidationError(
                f"aggregate payload {field} differs from run manifest"
            )
    revision = require_integer(
        payload.get("run_manifest_revision"),
        label="run_manifest_revision",
        minimum=1,
    )
    lease = require_integer(
        payload.get("lease_generation"), label="lease_generation", minimum=1
    )
    manifest_hash = require_sha256(
        payload.get("run_manifest_hash"), label="run_manifest_hash"
    )
    expected_hash: str | None
    if signing:
        expected_revision = int(manifest["revision"])
        expected_lease = int(manifest["lease_generation"])
        expected_hash = sha256_hex(manifest_path.read_bytes())
    else:
        expected_revision, expected_lease = _expected_aggregate_manifest_revision(
            manifest, phase=phase
        )
        offset = int(manifest["revision"]) - expected_revision
        expected_hash = (
            sha256_hex(manifest_path.read_bytes())
            if offset == 0
            else manifest["previous_manifest_hash"]
            if offset == 1
            else None
        )
    if revision != expected_revision or lease != expected_lease:
        raise ContractValidationError(
            "aggregate payload manifest revision or lease is stale"
        )
    if expected_hash is not None and manifest_hash != expected_hash:
        raise ContractValidationError("aggregate payload manifest hash is stale")


def _validate_aggregate_header(payload: Mapping[str, Any], *, phase: str) -> str:
    expected_kind = f"repo_aggregate_{phase}"
    if payload["store_kind"] != expected_kind:
        raise ContractValidationError(
            f"{phase} aggregate store_kind must be {expected_kind}"
        )
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise ContractValidationError(
            f"{phase} aggregate schema_version must be integer 1"
        )
    repository = _validate_repo(payload["repository_id"])
    require_safe_id(payload["run_id"], label="run_id")
    require_nonempty_string(payload["run_manifest_path"], label="run_manifest_path")
    _validate_normative_hashes(payload["normative_artifact_hashes"])
    channels = payload["claimed_release_channels"]
    if (
        not isinstance(channels, list)
        or not channels
        or any(not isinstance(item, str) or not item for item in channels)
        or len(channels) != len(set(channels))
    ):
        raise ContractValidationError(
            "claimed_release_channels must be a non-empty unique string array"
        )
    registries = payload["claimed_registries"]
    if not isinstance(registries, list) or any(
        not isinstance(item, Mapping) for item in registries
    ):
        raise ContractValidationError("claimed_registries must be an object array")
    return repository


def _validate_aggregate_payload_shape(payload: Mapping[str, Any], *, phase: str) -> str:
    if phase == "input":
        required = INPUT_AGGREGATE_PAYLOAD_KEYS
    elif phase == "final":
        required = FINAL_AGGREGATE_PAYLOAD_KEYS | (
            {"generated_output_attestation"}
            if payload.get("repository_id") == "OMG"
            else set()
        )
    else:  # pragma: no cover - guarded by _aggregate_domain
        raise ContractValidationError("aggregate phase must be input or final")
    require_exact_keys(payload, required=required, label=f"{phase} aggregate payload")
    return _validate_aggregate_header(payload, phase=phase)


def _validate_input_aggregate_payload(
    payload: Mapping[str, Any],
    *,
    authenticated_evidence: Mapping[str, Any] | None = None,
) -> None:
    repository = _validate_aggregate_payload_shape(payload, phase="input")
    roots = payload.get("ordered_owner_roots")
    if not isinstance(roots, list) or len(roots) != 6:
        raise ContractValidationError("input aggregate needs exactly six owner roots")
    expected_rows = REPOSITORY_OWNER_ROWS[repository]
    expected_waves = [wave for wave, _owner in expected_rows]
    actual_waves: list[str] = []
    hashes: list[str] = []
    known_hashes: dict[str, str] = {}
    accepted: list[dict[str, Any]] = []
    path_roots: list[dict[str, str]] = []
    state_root = ".omg" if repository == "OMG" else ".agy"
    run_id = str(payload["run_id"])
    for index, (value, (expected_wave, expected_owner)) in enumerate(
        zip(roots, expected_rows, strict=True)
    ):
        row = require_object(value, label=f"ordered_owner_roots[{index}]")
        require_exact_keys(
            row, required=OWNER_ROOT_KEYS, label=f"ordered_owner_roots[{index}]"
        )
        wave = require_safe_id(row["wave"], label=f"ordered_owner_roots[{index}].wave")
        owner = require_safe_id(
            row["owner"], label=f"ordered_owner_roots[{index}].owner"
        )
        if wave != expected_wave or owner != expected_owner:
            raise ContractValidationError(
                "owner roots must contain the frozen W0-W5 wave/owner order"
            )
        actual_waves.append(wave)
        artifact_root = f"{state_root}/artifacts/dual-parity/{run_id}/{wave}"
        if row["proposal_index_path"] != f"{artifact_root}/proposal-index.json":
            raise ContractValidationError("owner root proposal path mismatch")
        if row["handoff_path"] != f"{artifact_root}/handoff.json":
            raise ContractValidationError("owner root handoff path mismatch")
        require_sha256(
            row["proposal_index_hash"],
            label=f"ordered_owner_roots[{index}].proposal_index_hash",
        )
        digest = require_sha256(
            row["handoff_hash"],
            label=f"ordered_owner_roots[{index}].handoff_hash",
        )
        dependency_hashes = row["dependency_parent_handoff_hashes"]
        if not isinstance(dependency_hashes, list):
            raise ContractValidationError(
                "dependency_parent_handoff_hashes must be an array"
            )
        expected_dependencies = [
            known_hashes[parent] for parent in expected_parent_waves(wave)
        ]
        if dependency_hashes != expected_dependencies:
            raise ContractValidationError(
                "owner root dependency parent hashes mismatch"
            )
        path_test_root = require_sha256(
            row["path_test_root"],
            label=f"ordered_owner_roots[{index}].path_test_root",
        )
        requests = validate_w6_request_bindings(
            row["w6_requests"],
            repository_id=repository,
            run_id=run_id,
            wave=wave,
        )
        accepted.extend({"wave": wave, **request} for request in requests)
        path_roots.append({"wave": wave, "path_test_root": path_test_root})
        hashes.append(digest)
        known_hashes[wave] = digest
    if actual_waves != expected_waves:
        raise ContractValidationError("owner roots must be ordered W0 through W5")
    if len(set(hashes)) != len(hashes):
        raise ContractValidationError("ordered owner roots contain a duplicate hash")
    parents = payload.get("parent_handoff_hashes")
    if parents != hashes:
        raise ContractValidationError(
            "input aggregate parent_handoff_hashes must equal the six ordered roots"
        )
    merkle_root = require_sha256(
        payload.get("path_test_merkle_root"), label="path_test_merkle_root"
    )
    if merkle_root != sha256_hex(canonical_json_bytes(path_roots)):
        raise ContractValidationError(
            "input aggregate path_test_merkle_root differs from owner roots"
        )
    supplied_accepted = payload.get("accepted_w6_proposals")
    if not isinstance(supplied_accepted, list):
        raise ContractValidationError("accepted_w6_proposals must be an array")
    for index, raw in enumerate(supplied_accepted):
        row = require_object(raw, label=f"accepted_w6_proposals[{index}]")
        require_exact_keys(
            row,
            required=ACCEPTED_W6_PROPOSAL_KEYS,
            label=f"accepted_w6_proposals[{index}]",
        )
        require_safe_id(row["wave"], label="accepted_w6_proposals.wave")
        require_nonempty_string(row["path"], label="accepted_w6_proposals.path")
        require_integer(
            row["byte_length"], label="accepted_w6_proposals.byte_length", minimum=1
        )
        require_sha256(row["sha256"], label="accepted_w6_proposals.sha256")
    if supplied_accepted != accepted:
        raise ContractValidationError(
            "accepted_w6_proposals must exactly equal signed current W6 requests"
        )
    if authenticated_evidence is not None:
        expected_evidence = {
            "ordered_owner_roots": list(roots),
            "parent_handoff_hashes": hashes,
            "path_test_merkle_root": merkle_root,
            "accepted_w6_proposals": list(supplied_accepted),
        }
        if dict(authenticated_evidence) != expected_evidence:
            raise ContractValidationError(
                "input aggregate evidence differs from authenticated W0-W5 handoffs"
            )
    if payload.get("final_commit") is not None:
        raise ContractValidationError("input aggregate final_commit must be null")


STANDALONE_INPUT_SLOTS = [
    {
        "binding": "generated_output_request",
        "kind": "full_file",
        "owner": "OMG-W1",
        "path": "scripts/generate_standalone_hook.py",
        "position": 1,
    },
    {
        "binding": "generated_input_request",
        "kind": "full_file_plus_extracted_body",
        "owner": "OMG-W2",
        "path": "omg_cli/deny.py",
        "position": 2,
    },
    {
        "binding": "generated_input_request",
        "kind": "extracted_utf8_function",
        "owner": "OMG-W2",
        "path": "hooks/bin/_common.py",
        "position": 3,
        "selector": "hook_disabled",
    },
    {
        "binding": "final_version_selection",
        "json_pointer": "/version",
        "kind": "json_string_selector_value",
        "owner": "OMG-W6",
        "path": "plugin.json",
        "position": 4,
    },
]


def _extract_function_bytes(source: bytes, name: str) -> bytes:
    text = source.decode("utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            segment = ast.get_source_segment(text, node)
            if segment is None:
                break
            return segment.strip("\n").encode("utf-8")
    raise ContractValidationError(f"cannot extract generated input function {name!r}")


def _deny_body_bytes(source: bytes) -> bytes:
    text = source.decode("utf-8")
    tree = ast.parse(text)
    preamble_end = 0
    seen_code = False
    for node in tree.body:
        is_import = isinstance(node, (ast.Import, ast.ImportFrom))
        expression = node.value if isinstance(node, ast.Expr) else None
        is_docstring = isinstance(expression, ast.Constant) and isinstance(
            expression.value, str
        )
        if is_import:
            if seen_code:
                raise ContractValidationError(
                    "deny.py contains a top-level import after generated input code"
                )
            preamble_end = max(preamble_end, node.end_lineno or 0)
        elif is_docstring and not seen_code:
            preamble_end = max(preamble_end, node.end_lineno or 0)
        else:
            seen_code = True
    body = "".join(text.splitlines(keepends=True)[preamble_end:])
    return (body.strip("\n") + "\n").encode("utf-8")


def _generated_request_binding(
    input_payload: Mapping[str, Any], *, wave: str, filename: str
) -> Mapping[str, Any]:
    expected_suffix = f"/{wave}/{filename}"
    rows = [
        row
        for row in input_payload["accepted_w6_proposals"]
        if row.get("wave") == wave
        and str(row.get("path", "")).endswith(expected_suffix)
    ]
    if len(rows) != 1:
        raise ContractValidationError(
            f"authenticated input must contain exactly one {wave}/{filename} request"
        )
    return rows[0]


def _authenticate_generated_request(
    root: Path,
    *,
    manifest: Mapping[str, Any],
    trust: Mapping[str, Any],
    input_payload: Mapping[str, Any],
    wave: str,
    filename: str,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    binding = _generated_request_binding(input_payload, wave=wave, filename=filename)
    expected_path = f".omg/artifacts/dual-parity/{manifest['run_id']}/{wave}/{filename}"
    if binding["path"] != expected_path:
        raise ContractValidationError("generated request path is not authoritative")
    path = root / expected_path
    envelope = _read_authenticated_artifact(path, label=f"{wave} {filename}")
    body = path.read_bytes()
    if binding["byte_length"] != len(body) or binding["sha256"] != sha256_hex(body):
        raise ContractValidationError(
            "generated request differs from authenticated input"
        )
    owner, key = _owner_key(root, manifest, trust, wave)
    require_exact_keys(
        envelope,
        required={"signed_payload", "signature"},
        label="generated request envelope",
    )
    payload = require_object(
        envelope["signed_payload"], label="generated request payload"
    )
    signature = require_sha256(
        envelope["signature"], label="generated request signature"
    )
    expected_signature = hmac_sha256_hex(key, HANDOFF_DOMAIN, payload)
    if not hmac.compare_digest(signature, expected_signature):
        raise ContractValidationError("generated request signature mismatch")
    expected_kind = (
        "generated_output_request" if wave == "OMG-W1" else "generated_input_request"
    )
    for field, expected in (
        ("store_kind", expected_kind),
        ("schema_version", 1),
        ("repository_id", "OMG"),
        ("run_id", manifest["run_id"]),
        ("wave", wave),
        ("owner", owner),
        ("frozen_base_commit", manifest["frozen_base_commit"]),
        ("frozen_base_tree", manifest["frozen_base_tree"]),
        ("ordered_input_slots", STANDALONE_INPUT_SLOTS),
    ):
        if payload.get(field) != expected:
            raise ContractValidationError(f"generated request {field} mismatch")
    root_row = next(
        row for row in input_payload["ordered_owner_roots"] if row["wave"] == wave
    )
    if (
        payload.get("parent_handoff_hashes")
        != root_row["dependency_parent_handoff_hashes"]
    ):
        raise ContractValidationError("generated request parent hash mismatch")
    return payload, binding


def _require_snapshot_row(
    value: Any, *, expected: Mapping[str, Any], label: str
) -> None:
    row = require_object(value, label=label)
    for field, expected_value in expected.items():
        if row.get(field) != expected_value:
            raise ContractValidationError(f"{label}.{field} differs from current input")


def _run_generator(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [sys.executable, "scripts/generate_standalone_hook.py", *arguments],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    return result


def _derive_generated_output_attestation(
    root: Path,
    *,
    manifest: Mapping[str, Any],
    trust: Mapping[str, Any],
    input_payload: Mapping[str, Any],
) -> dict[str, Any]:
    output_request, output_binding = _authenticate_generated_request(
        root,
        manifest=manifest,
        trust=trust,
        input_payload=input_payload,
        wave="OMG-W1",
        filename="generated-output-request.json",
    )
    input_request, input_binding = _authenticate_generated_request(
        root,
        manifest=manifest,
        trust=trust,
        input_payload=input_payload,
        wave="OMG-W2",
        filename="generated-input-request.json",
    )
    generator = _read_current_proposal_path(root, "scripts/generate_standalone_hook.py")
    deny = _read_current_proposal_path(root, "omg_cli/deny.py")
    common = _read_current_proposal_path(root, "hooks/bin/_common.py")
    generated = _read_current_proposal_path(
        root, "hooks/bin/omg_pretool_deny_standalone.py"
    )
    if None in (generator, deny, common, generated):
        raise ContractValidationError("generated output input/output file is missing")
    assert generator is not None and deny is not None and common is not None
    assert generated is not None
    deny_body = _deny_body_bytes(deny)
    hook_disabled = _extract_function_bytes(common, "hook_disabled")
    plugin_body = _read_current_proposal_path(root, "plugin.json")
    if plugin_body is None:
        raise ContractValidationError("plugin.json is missing")
    try:
        plugin_version = json.loads(plugin_body.decode("utf-8"))["version"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ContractValidationError("plugin.json /version is invalid") from exc
    if not isinstance(plugin_version, str):
        raise ContractValidationError("plugin.json /version must be a string")
    version_bytes = plugin_version.encode("utf-8")
    generator_row = {
        "path": "scripts/generate_standalone_hook.py",
        "full_bytes_sha256": sha256_hex(generator),
        "full_bytes_size": len(generator),
    }
    deny_row = {
        "path": "omg_cli/deny.py",
        "full_bytes_sha256": sha256_hex(deny),
        "full_bytes_size": len(deny),
        "post_import_body_sha256": sha256_hex(deny_body),
        "post_import_body_size": len(deny_body),
    }
    common_row = {
        "path": "hooks/bin/_common.py",
        "selector": "hook_disabled",
        "extracted_utf8_sha256": sha256_hex(hook_disabled),
        "extracted_utf8_size": len(hook_disabled),
    }
    output_snapshot = require_object(
        output_request.get("input_snapshot"), label="generated output input_snapshot"
    )
    _require_snapshot_row(
        output_snapshot.get("generator"), expected=generator_row, label="generator"
    )
    _require_snapshot_row(output_snapshot.get("deny"), expected=deny_row, label="deny")
    _require_snapshot_row(
        output_snapshot.get("common_hook_disabled"),
        expected=common_row,
        label="common_hook_disabled",
    )
    output_generator = require_object(
        output_request.get("generator"), label="generated output generator"
    )
    _require_snapshot_row(output_generator, expected=generator_row, label="generator")
    if output_generator.get("interface") != "standalone_hook_generator/1":
        raise ContractValidationError("generated output request interface mismatch")
    owned_inputs = input_request.get("owned_inputs")
    if not isinstance(owned_inputs, list) or len(owned_inputs) != 2:
        raise ContractValidationError(
            "generated input request must contain two owned inputs"
        )
    _require_snapshot_row(
        owned_inputs[0], expected={"position": 2, **deny_row}, label="owned_inputs[0]"
    )
    _require_snapshot_row(
        owned_inputs[1],
        expected={"position": 3, **common_row},
        label="owned_inputs[1]",
    )
    selector = require_object(
        input_request.get("version_selector_request"), label="version_selector_request"
    )
    _require_snapshot_row(
        selector,
        expected={
            "path": "plugin.json",
            "json_pointer": "/version",
            "position": 4,
            "required_json_type": "string",
            "value_owner": "OMG-W6",
        },
        label="version_selector_request",
    )
    interface = _run_generator(root, "--interface")
    if (
        interface.returncode != 0
        or interface.stdout != b"standalone_hook_generator/1\n"
    ):
        raise ContractValidationError("generator interface readback failed")
    first = _run_generator(root, "--print")
    second = _run_generator(root, "--print")
    if first.returncode != 0 or second.returncode != 0 or first.stdout != second.stdout:
        raise ContractValidationError("generated output is not deterministic")
    if first.stdout != generated:
        raise ContractValidationError(
            "generated output bytes differ from canonical render"
        )
    check = _run_generator(root, "--check")
    if check.returncode != 0:
        raise ContractValidationError("generated output check failed")
    ordered_inputs = [
        {"position": 1, "kind": "full_file", **generator_row},
        {"position": 2, "kind": "full_file_plus_extracted_body", **deny_row},
        {"position": 3, "kind": "extracted_utf8_function", **common_row},
        {
            "position": 4,
            "kind": "json_string_selector_value",
            "path": "plugin.json",
            "json_pointer": "/version",
            "value": plugin_version,
            "value_utf8_sha256": sha256_hex(version_bytes),
        },
    ]
    return {
        "store_kind": "omg_generated_output_attestation",
        "schema_version": 1,
        "standalone_hook_inputs_hash": sha256_hex(canonical_json_bytes(ordered_inputs)),
        "generated_output_request_hash": str(output_binding["sha256"]),
        "generated_input_request_hash": str(input_binding["sha256"]),
        "first_output_hash": sha256_hex(first.stdout),
        "second_output_hash": sha256_hex(second.stdout),
        "check_receipt": {
            "argv": ["python3", "scripts/generate_standalone_hook.py", "--check"],
            "rc": check.returncode,
            "stdout_sha256": sha256_hex(check.stdout),
            "stderr_sha256": sha256_hex(check.stderr),
        },
    }


def build_generated_output_attestation(
    manifest_path: Path | str, *, input_envelope: Mapping[str, Any]
) -> dict[str, Any]:
    """Derive the OMG generated-output proof from authenticated current bytes."""

    path = Path(manifest_path).resolve()
    root = path.parents[4]
    run_id = path.parent.name
    with exclusive_lock(_manifest_lock(root, run_id)):
        manifest = read_run_manifest(path, root=root)
        if manifest["repository_id"] != "OMG":
            raise ContractValidationError("generated output attestation is OMG-only")
        trust = _load_trust(root, manifest)
        _verify_repository_aggregate_unlocked(
            path,
            phase="input",
            envelope=input_envelope,
            root=root,
            manifest=manifest,
            trust=trust,
        )
        input_payload = require_object(
            input_envelope.get("payload"), label="input aggregate payload"
        )
        return _derive_generated_output_attestation(
            root,
            manifest=manifest,
            trust=trust,
            input_payload=input_payload,
        )


def _validate_generated_output_attestation(
    value: Any, *, expected: Mapping[str, Any] | None = None
) -> None:
    attestation = require_object(value, label="generated_output_attestation")
    require_exact_keys(
        attestation,
        required=GENERATED_OUTPUT_ATTESTATION_KEYS,
        label="generated_output_attestation",
    )
    if (
        attestation["store_kind"] != "omg_generated_output_attestation"
        or attestation["schema_version"] != 1
        or isinstance(attestation["schema_version"], bool)
    ):
        raise ContractValidationError("generated_output_attestation header mismatch")
    for field in (
        "standalone_hook_inputs_hash",
        "generated_output_request_hash",
        "generated_input_request_hash",
        "first_output_hash",
        "second_output_hash",
    ):
        require_sha256(
            attestation[field], label=f"generated_output_attestation.{field}"
        )
    if attestation["first_output_hash"] != attestation["second_output_hash"]:
        raise ContractValidationError("generated output hashes are not deterministic")
    receipt = require_object(
        attestation["check_receipt"], label="generated_output_attestation.check_receipt"
    )
    require_exact_keys(
        receipt,
        required=GENERATED_OUTPUT_CHECK_RECEIPT_KEYS,
        label="generated_output_attestation.check_receipt",
    )
    if receipt["argv"] != [
        "python3",
        "scripts/generate_standalone_hook.py",
        "--check",
    ]:
        raise ContractValidationError("generated output check argv mismatch")
    if require_integer(receipt["rc"], label="generated output check rc") != 0:
        raise ContractValidationError("generated output check must pass")
    require_sha256(receipt["stdout_sha256"], label="generated output check stdout")
    require_sha256(receipt["stderr_sha256"], label="generated output check stderr")
    if expected is not None and attestation != dict(expected):
        raise ContractValidationError(
            "generated_output_attestation differs from authenticated current bytes"
        )


def _validate_final_aggregate_payload(payload: Mapping[str, Any]) -> None:
    repository = _validate_aggregate_payload_shape(payload, phase="final")
    for field in ("final_commit", "final_tree", "pushed_oid"):
        require_git_oid(payload.get(field), label=field)
    if payload["final_commit"] != payload["pushed_oid"]:
        raise ContractValidationError("pushed_oid must equal final_commit")
    for field in (
        "complete_delta_root",
        "deterministic_proof_hash",
        "live_proof_hash",
        "code_review_proof_hash",
        "ultraqa_proof_hash",
        "release_bundle_manifest_sha256",
        "release_asset_root",
    ):
        require_sha256(payload.get(field), label=field)
    semver = require_nonempty_string(payload.get("semver"), label="semver")
    if not SEMVER_RE.fullmatch(semver):
        raise ContractValidationError("semver is invalid")
    require_safe_id(payload.get("release_nonce"), label="release_nonce")
    expected_bundle_path = expected_bundle_manifest_relative_path(
        repository, str(payload["run_id"])
    )
    if payload.get("release_bundle_manifest_path") != expected_bundle_path:
        raise ContractValidationError("release bundle manifest path mismatch")
    if payload.get("release_bundle_manifest_schema") != "release_bundle_manifest/1":
        raise ContractValidationError("release bundle manifest schema mismatch")
    expected_archive = (
        f"oh-my-grok-{semver}.tar.gz"
        if repository == "OMG"
        else f"iml1s-oh-my-agy-{semver}.tgz"
    )
    upload_order = payload.get("public_upload_order")
    if upload_order != [expected_archive, "SHA256SUMS"]:
        raise ContractValidationError("public_upload_order is not repository exact")
    require_sha256(payload.get("input_aggregate_hash"), label="input_aggregate_hash")
    require_object(payload.get("input_envelope"), label="input_envelope")
    if repository == "OMG":
        _validate_generated_output_attestation(payload["generated_output_attestation"])


def _current_toolchain_row(command: str) -> dict[str, str]:
    executable = shutil.which(command)
    if executable is None:
        raise ContractValidationError(
            f"build receipt tool is unavailable: {command}"
        )
    binary = Path(executable).resolve()
    if not binary.is_file() or binary.is_symlink():
        raise ContractValidationError(
            f"build receipt tool path is unsafe: {command}"
        )
    try:
        body = binary.read_bytes()
        result = subprocess.run(
            [str(binary), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractValidationError(
            f"build receipt tool cannot be inspected: {command}"
        ) from exc
    version_bytes = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0 or not version_bytes:
        raise ContractValidationError(
            f"build receipt tool version probe failed: {command}"
        )
    try:
        version = version_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            f"build receipt tool version is not UTF-8: {command}"
        ) from exc
    return {
        "name": command,
        "version": version,
        "binary_sha256": sha256_hex(body),
    }


def _expected_current_build_receipt(
    root: Path,
    *,
    repository_id: str,
    candidate_commit: str,
    semver: str,
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct the only accepted receipt from live tools and output rows.

    The output digest is intentionally carried in the canonical argv as well as
    the separately verified asset table.  A caller therefore cannot authenticate
    an arbitrary, self-rehashed description of a different build invocation.
    """

    assets = bundle.get("assets")
    if not isinstance(assets, list) or len(assets) != 2:
        raise ContractValidationError(
            "build receipt requires the exact current two-asset bundle"
        )
    payload_name = (
        f"oh-my-grok-{semver}.tar.gz"
        if repository_id == "OMG"
        else f"iml1s-oh-my-agy-{semver}.tgz"
    )
    rows = [row for row in assets if isinstance(row, Mapping)]
    payload_rows = [row for row in rows if row.get("name") == payload_name]
    checksum_rows = [row for row in rows if row.get("name") == "SHA256SUMS"]
    if len(payload_rows) != 1 or len(checksum_rows) != 1:
        raise ContractValidationError(
            "build receipt output rows do not match the canonical bundle"
        )
    payload_row = payload_rows[0]
    checksum_row = checksum_rows[0]
    payload_sha = require_sha256(
        payload_row.get("sha256"), label="build receipt payload sha256"
    )
    require_sha256(
        checksum_row.get("sha256"), label="build receipt checksum sha256"
    )
    if repository_id == "OMG":
        command = "python3"
        argv = [
            "python3",
            "scripts/release_attest.py",
            "--asset",
            str(payload_row.get("relative_path")),
            "--checksums",
            str(checksum_row.get("relative_path")),
            "--asset-sha256",
            payload_sha,
        ]
    else:
        command = "npm"
        argv = [
            "npm",
            "pack",
            "--json",
            "--pack-destination",
            str(bundle.get("bundle_directory")),
        ]
    epoch_bytes = _git_bytes(
        root,
        ["show", "-s", "--format=%ct", candidate_commit],
        label="read candidate SOURCE_DATE_EPOCH",
    ).strip()
    try:
        source_date_epoch = int(epoch_bytes.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractValidationError(
            "candidate SOURCE_DATE_EPOCH is invalid"
        ) from exc
    environment = {
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    receipt: dict[str, Any] = {
        "argv": argv,
        "cwd_realpath_hash": sha256_hex(os.fsencode(str(root.resolve()))),
        "toolchain": [_current_toolchain_row(command)],
        "environment_allowlist": ["SOURCE_DATE_EPOCH", "LC_ALL", "TZ"],
        "environment_value_hashes": {
            name: sha256_hex(value) for name, value in environment.items()
        },
        "SOURCE_DATE_EPOCH": source_date_epoch,
        "locale": "C.UTF-8",
        "timezone": "UTC",
        "umask": "022",
    }
    receipt["receipt_hash"] = sha256_hex(canonical_json_bytes(receipt))
    return receipt


def _verify_current_build_receipt(
    root: Path,
    *,
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> None:
    expected = _expected_current_build_receipt(
        root,
        repository_id=str(manifest["repository_id"]),
        candidate_commit=str(payload["final_commit"]),
        semver=str(payload["semver"]),
        bundle=bundle,
    )
    if bundle.get("build_receipt") != expected:
        raise ContractValidationError(
            "build receipt differs from canonical live tools, environment, or outputs"
        )


def _verify_current_final_git_identity(
    root: Path,
    *,
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    head = _git_bytes(
        root,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        label="read current HEAD commit",
    ).decode("ascii").strip()
    if head != payload["final_commit"]:
        raise ContractValidationError("final_commit differs from current git HEAD")
    current_tree = _git_bytes(
        root,
        ["rev-parse", "--verify", "HEAD^{tree}"],
        label="read current HEAD tree",
    ).decode("ascii").strip()
    if current_tree != payload["final_tree"]:
        raise ContractValidationError("final_tree differs from current git HEAD tree")
    base_tree = _git_bytes(
        root,
        ["rev-parse", "--verify", f"{manifest['frozen_base_commit']}^{{tree}}"],
        label="read frozen base tree",
    ).decode("ascii").strip()
    if base_tree != manifest["frozen_base_tree"]:
        raise ContractValidationError("frozen base tree differs from current git object")
    ownership = OMG_OWNER_PATTERNS if manifest["repository_id"] == "OMG" else {}
    if not ownership:
        raise ContractValidationError(
            "final git ownership oracle is unavailable for this repository"
        )
    delta = verify_final_candidate(
        root,
        base_commit=str(manifest["frozen_base_commit"]),
        candidate_commit=str(payload["final_commit"]),
        ownership=ownership,
        remote=str(manifest["approved_remote"]),
        approved_branch=str(manifest["approved_branch"]),
        approved_remote_old_oid=str(manifest["approved_remote_old_oid"]),
    )
    expected_delta_root = sha256_hex(canonical_json_bytes(delta))
    if payload["complete_delta_root"] != expected_delta_root:
        raise ContractValidationError("complete_delta_root differs from final git delta")
    if manifest["repository_id"] == "OMG":
        plugin_path = root / "plugin.json"
        if not plugin_path.is_file() or plugin_path.is_symlink():
            raise ContractValidationError("plugin.json is missing or unsafe")
        try:
            plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractValidationError("plugin.json is invalid") from exc
        if not isinstance(plugin, dict) or plugin.get("version") != payload["semver"]:
            raise ContractValidationError(
                "current plugin.json version differs from final semver"
            )


def _validate_final_aggregate_evidence(
    root: Path,
    *,
    manifest: Mapping[str, Any],
    trust: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    """Rebuild every mutable final-signing claim from canonical current bytes."""

    input_envelope = require_object(payload["input_envelope"], label="input_envelope")
    input_payload = require_object(
        input_envelope.get("payload"), label="input aggregate payload"
    )

    _verify_current_final_git_identity(
        root,
        manifest=manifest,
        payload=payload,
    )

    relative = str(payload["release_bundle_manifest_path"])
    bundle_path = root / relative
    bundle = _read_authenticated_artifact(bundle_path, label="release bundle manifest")
    bundle_body = bundle_path.read_bytes()
    if sha256_hex(bundle_body) != payload["release_bundle_manifest_sha256"]:
        raise ContractValidationError("release bundle manifest hash mismatch")
    validated = validate_release_bundle_manifest(
        bundle,
        manifest_relative_path=relative,
        claimed_registries=manifest["claimed_registries"],
    )
    verify_release_bundle_files(
        root,
        validated,
        manifest_relative_path=relative,
        claimed_registries=manifest["claimed_registries"],
    )
    expected_fields = {
        "repository_id": manifest["repository_id"],
        "run_id": manifest["run_id"],
        "candidate_commit": payload["final_commit"],
        "candidate_tree": payload["final_tree"],
        "semver": payload["semver"],
        "public_upload_order": payload["public_upload_order"],
        "release_asset_root": payload["release_asset_root"],
        "registry_bindings": manifest["claimed_registries"],
    }
    for field, expected in expected_fields.items():
        if validated.get(field) != expected:
            raise ContractValidationError(
                f"release bundle manifest {field} differs from final aggregate"
            )
    _verify_current_build_receipt(
        root,
        manifest=manifest,
        payload=payload,
        bundle=validated,
    )
    _validate_repository_policy(
        str(manifest["repository_id"]),
        claimed_release_channels=manifest["claimed_release_channels"],
        claimed_registries=manifest["claimed_registries"],
        ownership_manifest_hash=str(manifest["ownership_manifest_hash"]),
    )
    if manifest["repository_id"] == "OMG":
        expected_attestation = _derive_generated_output_attestation(
            root,
            manifest=manifest,
            trust=trust,
            input_payload=input_payload,
        )
        _validate_generated_output_attestation(
            payload["generated_output_attestation"],
            expected=expected_attestation,
        )


def _verify_repository_aggregate_unlocked(
    manifest_path: Path,
    *,
    phase: str,
    envelope: Mapping[str, Any],
    root: Path,
    manifest: Mapping[str, Any],
    trust: Mapping[str, Any],
) -> str:
    domain = _aggregate_domain(phase)
    require_exact_keys(
        envelope, required=AGGREGATE_ENVELOPE_KEYS, label="aggregate envelope"
    )
    if envelope["algorithm"] != "HMAC-SHA256":
        raise ContractValidationError("unsupported aggregate algorithm")
    if envelope["signer_id"] != manifest["aggregate_signer_id"]:
        raise ContractValidationError("aggregate signer mismatch")
    payload = require_object(envelope["payload"], label="aggregate payload")
    _validate_aggregate_payload_shape(payload, phase=phase)
    _validate_aggregate_manifest_binding(
        payload,
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        phase=phase,
        signing=False,
    )
    if envelope["aggregate_key_id"] != manifest["aggregate_key_id"]:
        raise ContractValidationError("aggregate key ID mismatch")
    canonical = canonical_json_bytes(payload)
    payload_hash = sha256_hex(canonical)
    if require_sha256(envelope["payload_hash"], label="payload_hash") != payload_hash:
        raise ContractValidationError("aggregate payload hash mismatch")
    signature = require_sha256(envelope["signature"], label="aggregate signature")
    key = _aggregate_key(root, manifest, trust)
    expected_signature = hmac_sha256_hex(key, domain, payload)
    if not hmac.compare_digest(signature, expected_signature):
        raise ContractValidationError("aggregate signature mismatch")
    if phase == "input":
        authenticated_evidence = _verified_input_owner_roots(
            root=root,
            manifest_path=manifest_path,
            manifest=manifest,
            input_payload=payload,
        )
        _validate_input_aggregate_payload(
            payload, authenticated_evidence=authenticated_evidence
        )
        return payload_hash

    _validate_final_aggregate_payload(payload)
    input_envelope = require_object(payload["input_envelope"], label="input_envelope")
    input_hash = _verify_repository_aggregate_unlocked(
        manifest_path,
        phase="input",
        envelope=input_envelope,
        root=root,
        manifest=manifest,
        trust=trust,
    )
    if payload["input_aggregate_hash"] != input_hash:
        raise ContractValidationError(
            "final aggregate input_aggregate_hash does not preserve input envelope"
        )
    _validate_final_aggregate_evidence(
        root,
        manifest=manifest,
        trust=trust,
        payload=payload,
    )
    return payload_hash


FINAL_SIGN_TRANSACTION_KEYS = {
    "store_kind",
    "schema_version",
    "repository_id",
    "run_id",
    "manifest_path",
    "aggregate_path",
    "old_manifest",
    "new_manifest",
    "old_aggregate",
    "new_aggregate",
}


def _final_sign_transaction_path(
    root: Path, repository_id: str, run_id: str
) -> Path:
    return expected_repository_aggregate_path(
        root, repository_id, run_id
    ).with_name("aggregate-handoff.final-sign-transaction.json")


def _validate_final_sign_transaction(
    root: Path,
    manifest_path: Path,
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    journal = require_object(value, label="final signing transaction")
    require_exact_keys(
        journal,
        required=FINAL_SIGN_TRANSACTION_KEYS,
        label="final signing transaction",
    )
    if (
        journal["store_kind"] != "final_sign_transaction"
        or journal["schema_version"] != 1
        or isinstance(journal["schema_version"], bool)
    ):
        raise ContractValidationError("final signing transaction header mismatch")
    repository_id = _validate_repo(str(journal["repository_id"]))
    run_id = require_safe_id(journal["run_id"], label="run_id")
    expected_manifest = expected_manifest_path(root, run_id)
    expected_aggregate = expected_repository_aggregate_path(
        root, repository_id, run_id
    )
    if manifest_path != expected_manifest:
        raise ContractValidationError(
            "final signing transaction manifest path is not authoritative"
        )
    if journal["manifest_path"] != str(expected_manifest.relative_to(root)):
        raise ContractValidationError("final signing transaction manifest path mismatch")
    if journal["aggregate_path"] != str(expected_aggregate.relative_to(root)):
        raise ContractValidationError("final signing transaction aggregate path mismatch")
    old_manifest = validate_run_manifest(
        require_object(journal["old_manifest"], label="old_manifest"),
        root=root,
        path=manifest_path,
    )
    new_manifest = validate_run_manifest(
        require_object(journal["new_manifest"], label="new_manifest"),
        root=root,
        path=manifest_path,
    )
    if (
        old_manifest["repository_id"] != repository_id
        or old_manifest["run_id"] != run_id
        or new_manifest["repository_id"] != repository_id
        or new_manifest["run_id"] != run_id
    ):
        raise ContractValidationError("final signing transaction identity mismatch")
    old_manifest_body = canonical_json_bytes(old_manifest)
    expected_new_manifest = _build_manifest_transition(
        old_manifest,
        before_body=old_manifest_body,
        root=root,
        manifest_path=manifest_path,
        expected_revision=int(old_manifest["revision"]),
        expected_previous_manifest_hash=old_manifest["previous_manifest_hash"],
        expected_state="composition_active",
        next_state="signing_revoked",
        expected_lease_generation=int(old_manifest["lease_generation"]),
        updated_at=str(new_manifest["updated_at"]),
    )
    if new_manifest != expected_new_manifest:
        raise ContractValidationError(
            "final signing transaction manifest transition mismatch"
        )
    old_aggregate = _validate_repository_aggregate_store(
        require_object(journal["old_aggregate"], label="old_aggregate"),
        manifest=old_manifest,
    )
    new_aggregate = _validate_repository_aggregate_store(
        require_object(journal["new_aggregate"], label="new_aggregate"),
        manifest=new_manifest,
    )
    old_aggregate_body = canonical_json_bytes(old_aggregate)
    if (
        old_aggregate["revision"] != 1
        or new_aggregate["revision"] != 2
        or new_aggregate["previous_aggregate_hash"]
        != sha256_hex(old_aggregate_body)
        or new_aggregate["input_envelope"] != old_aggregate["input_envelope"]
    ):
        raise ContractValidationError(
            "final signing transaction aggregate transition mismatch"
        )
    final_envelope = require_object(
        new_aggregate["final_envelope"], label="final_envelope"
    )
    trust = _load_trust(root, new_manifest)
    _verify_repository_aggregate_unlocked(
        manifest_path,
        phase="final",
        envelope=final_envelope,
        root=root,
        manifest=new_manifest,
        trust=trust,
    )
    return old_manifest, new_manifest, old_aggregate, new_aggregate


def _read_transaction_side(path: Path, *, label: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ContractValidationError(f"{label} is missing or unsafe")
    if mode_bits(path) != DATA_FILE_MODE:
        raise ContractValidationError(f"{label} mode must be 0600")
    return path.read_bytes()


def _remove_final_sign_transaction_journal(path: Path) -> None:
    path.unlink()
    if os.name != "posix":  # pragma: no cover
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover_final_sign_transaction(
    root: Path, manifest_path: Path
) -> dict[str, Any] | None:
    """Recover an interrupted final aggregate/manifest two-file commit.

    The caller holds the manifest lock.  Only the exact old/new byte pairs from
    the canonical intent journal are accepted; any third state fails closed.
    """

    run_id = manifest_path.parent.name
    candidates = [
        _final_sign_transaction_path(root, repository, run_id)
        for repository in REPOSITORY_OWNER_ROWS
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    if len(existing) != 1:
        raise ContractValidationError(
            "multiple final signing transaction journals are present"
        )
    journal_path = existing[0]
    journal = _read_authenticated_artifact(
        journal_path, label="final signing transaction"
    )
    old_manifest, new_manifest, old_aggregate, new_aggregate = (
        _validate_final_sign_transaction(root, manifest_path, journal)
    )
    aggregate_path = expected_repository_aggregate_path(
        root, str(old_manifest["repository_id"]), str(old_manifest["run_id"])
    )
    old_manifest_body = canonical_json_bytes(old_manifest)
    new_manifest_body = canonical_json_bytes(new_manifest)
    old_aggregate_body = canonical_json_bytes(old_aggregate)
    new_aggregate_body = canonical_json_bytes(new_aggregate)
    current_manifest_body = _read_transaction_side(
        manifest_path, label="run manifest"
    )
    current_aggregate_body = _read_transaction_side(
        aggregate_path, label="repository aggregate handoff"
    )
    manifest_old = current_manifest_body == old_manifest_body
    manifest_new = current_manifest_body == new_manifest_body
    aggregate_old = current_aggregate_body == old_aggregate_body
    aggregate_new = current_aggregate_body == new_aggregate_body
    if not (manifest_old or manifest_new) or not (aggregate_old or aggregate_new):
        raise ContractValidationError(
            "final signing transaction found an unjournaled file state"
        )
    if manifest_old and aggregate_old:
        _remove_final_sign_transaction_journal(journal_path)
        return None
    if aggregate_old:
        atomic_write_bytes(
            aggregate_path, new_aggregate_body, mode=DATA_FILE_MODE
        )
    if manifest_old:
        atomic_write_bytes(
            manifest_path, new_manifest_body, mode=DATA_FILE_MODE
        )
    if (
        _read_transaction_side(manifest_path, label="run manifest")
        != new_manifest_body
        or _read_transaction_side(
            aggregate_path, label="repository aggregate handoff"
        )
        != new_aggregate_body
    ):
        raise ContractValidationError("final signing transaction recovery drifted")
    _remove_final_sign_transaction_journal(journal_path)
    return require_object(new_aggregate["final_envelope"], label="final_envelope")


def sign_repository_aggregate(
    manifest_path: Path | str,
    *,
    expected_revision: int,
    expected_lease_generation: int,
    phase: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Sign one repository aggregate through the manifest-fenced W6 capability.

    No caller supplies or receives key bytes.  The exact manifest state,
    revision, and lease are checked while holding the authoritative manifest
    lock so a concurrent CAS cannot race the signature.
    """

    path = Path(manifest_path).resolve()
    root = path.parents[4]
    domain = _aggregate_domain(phase)
    run_id = path.parent.name
    with exclusive_lock(_manifest_lock(root, run_id)):
        recovered = _recover_final_sign_transaction(root, path)
        if recovered is not None:
            requested = require_object(payload, label="aggregate payload")
            recovered_payload = require_object(
                recovered.get("payload"), label="recovered aggregate payload"
            )
            if (
                phase == "final"
                and recovered_payload == requested
                and recovered_payload.get("run_manifest_revision")
                == expected_revision
                and recovered_payload.get("lease_generation")
                == expected_lease_generation
            ):
                return recovered
            raise ContractValidationError(
                "aggregate sign capability is stale, revoked, or inactive"
            )
        manifest = read_run_manifest(path, root=root)
        allowed_state = "inputs_verified" if phase == "input" else "composition_active"
        if (
            manifest["state"] != allowed_state
            or manifest["revision"] != expected_revision
            or manifest["lease_generation"] != expected_lease_generation
        ):
            raise ContractValidationError(
                "aggregate sign capability is stale, revoked, or inactive"
            )
        data = require_object(payload, label="aggregate payload")
        _validate_aggregate_payload_shape(data, phase=phase)
        _validate_aggregate_manifest_binding(
            data,
            root=root,
            manifest_path=path,
            manifest=manifest,
            phase=phase,
            signing=True,
        )
        trust = _load_trust(root, manifest)
        key = _aggregate_key(root, manifest, trust)
        payload_hash = sha256_hex(canonical_json_bytes(data))
        envelope = {
            "algorithm": "HMAC-SHA256",
            "signer_id": manifest["aggregate_signer_id"],
            "aggregate_key_id": manifest["aggregate_key_id"],
            "payload_hash": payload_hash,
            "payload": dict(data),
            "signature": hmac_sha256_hex(key, domain, data),
        }
        _verify_repository_aggregate_unlocked(
            path,
            phase=phase,
            envelope=envelope,
            root=root,
            manifest=manifest,
            trust=trust,
        )

        aggregate_path = expected_repository_aggregate_path(
            root, str(manifest["repository_id"]), str(manifest["run_id"])
        )
        if phase == "input":
            desired = {
                "store_kind": "repo_aggregate_handoff",
                "schema_version": 1,
                "repository_id": manifest["repository_id"],
                "run_id": manifest["run_id"],
                "revision": 1,
                "previous_aggregate_hash": None,
                "input_envelope": envelope,
                "final_envelope": None,
            }
            desired_body = canonical_json_bytes(desired)
            if aggregate_path.exists():
                stored, stored_path, stored_body = _read_repository_aggregate_store(
                    root, manifest
                )
                if (
                    stored_path != aggregate_path
                    or stored["revision"] != 1
                    or stored_body != desired_body
                ):
                    raise ContractValidationError(
                        "conflicting repository input aggregate signature"
                    )
            else:
                atomic_write_bytes(
                    aggregate_path,
                    desired_body,
                    mode=DATA_FILE_MODE,
                    replace=False,
                )
            return envelope

        stored, stored_path, stored_body = _read_repository_aggregate_store(
            root, manifest
        )
        if stored_path != aggregate_path or stored["revision"] != 1:
            raise ContractValidationError(
                "final aggregate requires the canonical input aggregate CAS"
            )
        if stored["input_envelope"] != data["input_envelope"]:
            raise ContractValidationError(
                "final aggregate input envelope differs from canonical CAS"
            )
        desired = {
            **stored,
            "revision": 2,
            "previous_aggregate_hash": sha256_hex(stored_body),
            "final_envelope": envelope,
        }
        desired_body = canonical_json_bytes(desired)
        manifest_body = path.read_bytes()
        after = _build_manifest_transition(
            manifest,
            before_body=manifest_body,
            root=root,
            manifest_path=path,
            expected_revision=expected_revision,
            expected_previous_manifest_hash=manifest["previous_manifest_hash"],
            expected_state="composition_active",
            next_state="signing_revoked",
            expected_lease_generation=expected_lease_generation,
        )
        after_body = canonical_json_bytes(after)
        journal_path = _final_sign_transaction_path(
            root, str(manifest["repository_id"]), str(manifest["run_id"])
        )
        journal = {
            "store_kind": "final_sign_transaction",
            "schema_version": 1,
            "repository_id": manifest["repository_id"],
            "run_id": manifest["run_id"],
            "manifest_path": str(path.relative_to(root)),
            "aggregate_path": str(aggregate_path.relative_to(root)),
            "old_manifest": manifest,
            "new_manifest": after,
            "old_aggregate": stored,
            "new_aggregate": desired,
        }
        atomic_write_bytes(
            journal_path,
            canonical_json_bytes(journal),
            mode=DATA_FILE_MODE,
            replace=False,
        )
        atomic_write_bytes(aggregate_path, desired_body, mode=DATA_FILE_MODE)
        atomic_write_bytes(path, after_body, mode=DATA_FILE_MODE)
        if (
            _read_transaction_side(path, label="run manifest") != after_body
            or _read_transaction_side(
                aggregate_path, label="repository aggregate handoff"
            )
            != desired_body
        ):
            raise ContractValidationError("final signing transaction commit drifted")
        _remove_final_sign_transaction_journal(journal_path)
        return envelope


def verify_repository_aggregate(
    manifest_path: Path | str,
    *,
    phase: str,
    envelope: Mapping[str, Any],
) -> str:
    """Verify a repository aggregate using only the pinned verify operation."""

    path = Path(manifest_path).resolve()
    root = path.parents[4]
    _aggregate_domain(phase)
    run_id = path.parent.name
    with exclusive_lock(_manifest_lock(root, run_id)):
        _recover_final_sign_transaction(root, path)
        manifest = read_run_manifest(path, root=root)
        trust = _load_trust(root, manifest)
        store, _store_path, _store_body = _read_repository_aggregate_store(
            root, manifest
        )
        stored_envelope = (
            store["input_envelope"] if phase == "input" else store["final_envelope"]
        )
        if stored_envelope is None or envelope != stored_envelope:
            raise ContractValidationError(
                "aggregate envelope differs from canonical repository handoff"
            )
        return _verify_repository_aggregate_unlocked(
            path,
            phase=phase,
            envelope=envelope,
            root=root,
            manifest=manifest,
            trust=trust,
        )


def _aggregate_cli_path(
    manifest_path: Path | str, candidate: Path | str, *, label: str
) -> Path:
    path = Path(manifest_path).resolve()
    root = path.parents[4]
    manifest = read_run_manifest(path, root=root)
    aggregate_root = (
        root
        / ".omg"
        / "artifacts"
        / "dual-parity"
        / str(manifest["run_id"])
        / f"{manifest['repository_id']}-W6"
    ).resolve(strict=False)
    resolved = Path(candidate).resolve(strict=False)
    if resolved.parent != aggregate_root or not resolved.name:
        raise ContractValidationError(
            f"{label} must be a direct child of the authoritative W6 artifact root"
        )
    return resolved


def _canonical_aggregate_cli_path(
    manifest_path: Path | str, candidate: Path | str, *, label: str
) -> Path:
    path = Path(manifest_path).resolve()
    root = path.parents[4]
    manifest = read_run_manifest(path, root=root)
    expected = expected_repository_aggregate_path(
        root, str(manifest["repository_id"]), str(manifest["run_id"])
    )
    resolved = Path(candidate).resolve(strict=False)
    if resolved != expected:
        raise ContractValidationError(
            f"{label} must be the canonical repository aggregate handoff path"
        )
    return resolved


def _parse_hash_pairs(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError("artifact hash must be NAME=SHA256")
        name, digest = value.split("=", 1)
        result[name] = digest
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m omg_cli.contracts.run_manifest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--root", default=".")
    init.add_argument("--repository-id", choices=("OMG", "OMA"), required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument("--frozen-base-commit", required=True)
    init.add_argument("--frozen-base-tree", required=True)
    init.add_argument("--approved-branch", required=True)
    init.add_argument("--approved-remote", required=True)
    init.add_argument("--approved-remote-old-oid", required=True)
    init.add_argument(
        "--artifact-hash", action="append", default=[], metavar="NAME=SHA256"
    )
    init.add_argument("--ownership-manifest-hash", required=True)
    init.add_argument("--release-channel", action="append", default=[])
    init.add_argument("--claimed-registries-json", default="[]")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--path", required=True)
    verify.add_argument("--root")

    transition = subparsers.add_parser("transition")
    transition.add_argument("--path", required=True)
    transition.add_argument("--expected-revision", required=True, type=int)
    transition.add_argument("--expected-previous-manifest-hash")
    transition.add_argument(
        "--expected-state", required=True, choices=RUN_MANIFEST_STATE_SET
    )
    transition.add_argument(
        "--next-state", required=True, choices=RUN_MANIFEST_STATE_SET
    )
    transition.add_argument("--expected-lease-generation", required=True, type=int)

    finalize = subparsers.add_parser("finalize-release")
    finalize.add_argument("--path", required=True)
    finalize.add_argument("--expected-revision", required=True, type=int)
    finalize.add_argument("--expected-previous-manifest-hash")
    finalize.add_argument("--expected-lease-generation", required=True, type=int)
    finalize.add_argument("--evidence", required=True)

    emit = subparsers.add_parser("emit-handoff")
    emit.add_argument("--path", required=True)
    emit.add_argument("--wave", required=True)
    emit.add_argument("--owner", required=True)
    emit.add_argument("--proposal-entries-json", required=True)
    emit.add_argument("--parent-handoff-hash", action="append")
    emit.add_argument("--w6-request", action="append", default=[])

    sign_aggregate = subparsers.add_parser("sign-aggregate")
    sign_aggregate.add_argument("--path", required=True)
    sign_aggregate.add_argument("--phase", choices=("input", "final"), required=True)
    sign_aggregate.add_argument("--expected-revision", required=True, type=int)
    sign_aggregate.add_argument("--expected-lease-generation", required=True, type=int)
    sign_aggregate.add_argument("--input", required=True)
    sign_aggregate.add_argument("--output", required=True)

    verify_aggregate = subparsers.add_parser("verify-aggregate")
    verify_aggregate.add_argument("--path", required=True)
    verify_aggregate.add_argument("--phase", choices=("input", "final"), required=True)
    verify_aggregate.add_argument("--input", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            registries = json.loads(args.claimed_registries_json)
            manifest = initialize_run_manifest(
                args.root,
                repository_id=args.repository_id,
                run_id=args.run_id,
                frozen_base_commit=args.frozen_base_commit,
                frozen_base_tree=args.frozen_base_tree,
                approved_branch=args.approved_branch,
                approved_remote=args.approved_remote,
                approved_remote_old_oid=args.approved_remote_old_oid,
                normative_artifact_hashes=_parse_hash_pairs(args.artifact_hash),
                ownership_manifest_hash=args.ownership_manifest_hash,
                claimed_release_channels=args.release_channel or ["github"],
                claimed_registries=registries,
            )
            result = {
                "ok": True,
                "path": str(expected_manifest_path(args.root, args.run_id)),
                "manifest_hash": sha256_hex(canonical_json_bytes(manifest)),
                "revision": manifest["revision"],
                "state": manifest["state"],
            }
        elif args.command == "verify":
            manifest = read_run_manifest(args.path, root=args.root)
            result = {
                "ok": True,
                "path": str(Path(args.path).resolve()),
                "manifest_hash": sha256_hex(canonical_json_bytes(manifest)),
                "revision": manifest["revision"],
                "state": manifest["state"],
            }
        elif args.command == "transition":
            manifest = transition_run_manifest(
                args.path,
                expected_revision=args.expected_revision,
                expected_previous_manifest_hash=args.expected_previous_manifest_hash,
                expected_state=args.expected_state,
                next_state=args.next_state,
                expected_lease_generation=args.expected_lease_generation,
            )
            result = {
                "ok": True,
                "path": str(Path(args.path).resolve()),
                "manifest_hash": sha256_hex(canonical_json_bytes(manifest)),
                "revision": manifest["revision"],
                "state": manifest["state"],
            }
        elif args.command == "finalize-release":
            evidence_path = _aggregate_cli_path(
                args.path, args.evidence, label="release completion evidence input"
            )
            evidence = _read_authenticated_artifact(
                evidence_path, label="release completion evidence input"
            )
            manifest = finalize_release_run_manifest(
                args.path,
                expected_revision=args.expected_revision,
                expected_previous_manifest_hash=args.expected_previous_manifest_hash,
                expected_lease_generation=args.expected_lease_generation,
                evidence=evidence,
            )
            result = {
                "ok": True,
                "path": str(Path(args.path).resolve()),
                "evidence_path": str(
                    expected_release_completion_evidence_path(
                        Path(args.path).resolve().parents[4],
                        str(manifest["repository_id"]),
                        str(manifest["run_id"]),
                    )
                ),
                "manifest_hash": sha256_hex(canonical_json_bytes(manifest)),
                "revision": manifest["revision"],
                "state": manifest["state"],
            }
        elif args.command == "emit-handoff":
            entries = json.loads(
                Path(args.proposal_entries_json).read_text(encoding="utf-8")
            )
            if not isinstance(entries, list):
                raise ContractValidationError("proposal entries JSON must be an array")
            result = {
                "ok": True,
                **emit_owner_handoff(
                    args.path,
                    wave=args.wave,
                    owner=args.owner,
                    proposal_entries=entries,
                    w6_request_paths=args.w6_request,
                    parent_handoff_hashes=args.parent_handoff_hash,
                ),
            }
        elif args.command == "sign-aggregate":
            input_path = _aggregate_cli_path(
                args.path, args.input, label="aggregate payload input"
            )
            output = _canonical_aggregate_cli_path(
                args.path, args.output, label="aggregate envelope output"
            )
            payload = _read_authenticated_artifact(
                input_path, label="aggregate payload input"
            )
            envelope = sign_repository_aggregate(
                args.path,
                expected_revision=args.expected_revision,
                expected_lease_generation=args.expected_lease_generation,
                phase=args.phase,
                payload=payload,
            )
            result = {
                "ok": True,
                "path": str(output),
                "payload_hash": envelope["payload_hash"],
                "phase": args.phase,
            }
        else:
            input_path = _canonical_aggregate_cli_path(
                args.path, args.input, label="aggregate envelope input"
            )
            store = _read_authenticated_artifact(
                input_path, label="repository aggregate handoff"
            )
            envelope = require_object(
                store.get(
                    "input_envelope" if args.phase == "input" else "final_envelope"
                ),
                label=f"{args.phase} aggregate envelope",
            )
            payload_hash = verify_repository_aggregate(
                args.path, phase=args.phase, envelope=envelope
            )
            result = {
                "ok": True,
                "path": str(input_path),
                "payload_hash": payload_hash,
                "phase": args.phase,
            }
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(main())
