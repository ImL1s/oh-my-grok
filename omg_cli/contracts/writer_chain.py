"""Canonical proposal, handoff, ownership and aggregate contracts.

This module owns byte semantics only.  It never grants repository or release
authority: callers must still hold the relevant run-manifest lease/capability.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


HANDOFF_DOMAIN = b"OMG-OMA-HANDOFF-V1\0"
INPUT_AGGREGATE_DOMAIN = b"OMG-OMA-REPO-AGGREGATE-INPUT-V1\0"
FINAL_AGGREGATE_DOMAIN = b"OMG-OMA-REPO-AGGREGATE-FINAL-V1\0"

PARENT_HASH_ORACLE: dict[str, tuple[str, ...]] = {
    "W0": (),
    "W1": ("W0",),
    "W2": ("W0",),
    "W3": ("W2",),
    "W4": ("W1", "W2"),
    "W5": ("W3", "W4"),
    "W6": ("W0", "W1", "W2", "W3", "W4", "W5"),
    "W7": ("W6",),
}


class VerifiedParentHashes(dict[str, str]):
    """Parent roots obtained from actual same-run authenticated envelopes.

    A plain caller-labelled mapping is not authority for a non-root wave.
    The repository run-manifest engine creates this value only after loading
    and verifying the parent artifacts against its pinned trust root.
    """

    __slots__ = ()


PROPOSAL_INDEX_PAYLOAD_KEYS = {
    "store_kind",
    "schema_version",
    "repository_id",
    "run_id",
    "wave",
    "owner",
    "run_manifest_path",
    "run_manifest_revision",
    "run_manifest_hash",
    "frozen_base_commit",
    "frozen_base_tree",
    "lease_generation",
    "parent_handoff_hashes",
    "entries",
    "w6_requests",
    "created_at",
}
W6_REQUEST_BINDING_KEYS = frozenset({"path", "byte_length", "sha256"})
OWNER_HANDOFF_PAYLOAD_KEYS = {
    "store_kind",
    "schema_version",
    "repository_id",
    "run_id",
    "wave",
    "owner",
    "run_manifest_path",
    "run_manifest_revision",
    "run_manifest_hash",
    "frozen_base_commit",
    "frozen_base_tree",
    "lease_generation",
    "proposal_index_path",
    "proposal_index_hash",
    "parent_handoff_hashes",
    "path_records",
    "created_at",
}

REPOSITORY_AGGREGATE_SIGNERS = {
    "OMG": "OMG-W6-aggregate-signer",
    "OMA": "OMA-W6-aggregate-signer",
}
REPOSITORY_AGGREGATE_VERIFIERS = {
    "OMG": "OMG-W6-aggregate-verifier",
    "OMA": "OMA-W6-aggregate-verifier",
}


class CanonicalJSONError(ContractValidationError):
    """A value is outside canonical JSON v1."""


def _validate_canonical_value(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, str):
        for character in value:
            codepoint = ord(character)
            if 0xD800 <= codepoint <= 0xDFFF:
                raise CanonicalJSONError(f"{path} contains an unpaired surrogate")
        return
    if isinstance(value, list) or isinstance(value, tuple):
        for index, item in enumerate(value):
            _validate_canonical_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJSONError(f"{path} has a non-string object key")
            _validate_canonical_value(key, path=f"{path}.<key>")
            _validate_canonical_value(item, path=f"{path}.{key}")
        return
    raise CanonicalJSONError(
        f"{path} uses unsupported type {type(value).__name__}; "
        "canonical JSON v1 permits null/bool/string/integer/array/object only"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Encode canonical JSON v1: compact UTF-8, code-point-sorted, no newline."""

    _validate_canonical_value(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CanonicalJSONError(f"canonical JSON encoding failed: {exc}") from exc


def parse_canonical_json_bytes(body: bytes) -> Any:
    if body.startswith(b"\xef\xbb\xbf") or body.endswith(b"\n"):
        raise CanonicalJSONError("canonical JSON must have no BOM or trailing newline")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CanonicalJSONError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalJSONError("invalid UTF-8 canonical JSON") from exc
    if canonical_json_bytes(value) != body:
        raise CanonicalJSONError("JSON bytes are not canonical JSON v1")
    return value


def sha256_hex(body: bytes | str) -> str:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def hmac_sha256_hex(key: bytes, domain: bytes, payload: Mapping[str, Any]) -> str:
    if len(key) != 32:
        raise ContractValidationError("HMAC key must contain exactly 32 bytes")
    return hmac.new(
        key, domain + canonical_json_bytes(payload), hashlib.sha256
    ).hexdigest()


def wave_suffix(wave: str) -> str:
    normalized = require_nonempty_string(wave, label="wave")
    if normalized.startswith(("OMG-", "OMA-")):
        normalized = normalized.split("-", 1)[1]
    if normalized not in PARENT_HASH_ORACLE:
        raise ContractValidationError(f"unsupported wave: {wave!r}")
    return normalized


def expected_parent_waves(wave: str, *, repository: str | None = None) -> list[str]:
    suffix = wave_suffix(wave)
    prefix = repository or (wave.split("-", 1)[0] if "-" in wave else "")
    if prefix:
        if prefix not in {"OMG", "OMA"}:
            raise ContractValidationError(f"unsupported repository prefix: {prefix!r}")
        return [f"{prefix}-{item}" for item in PARENT_HASH_ORACLE[suffix]]
    return list(PARENT_HASH_ORACLE[suffix])


def validate_parent_hashes(
    wave: str,
    parent_handoff_hashes: Sequence[str],
    trusted_parent_hashes: Mapping[str, str],
) -> list[str]:
    expected_waves = expected_parent_waves(wave)
    supplied = list(parent_handoff_hashes)
    if not expected_waves:
        if supplied or trusted_parent_hashes:
            raise ContractValidationError("W0 parent_handoff_hashes must be empty")
        return []
    if not isinstance(trusted_parent_hashes, VerifiedParentHashes):
        raise ContractValidationError(
            "parent hashes must come from verified same-run envelopes"
        )
    expected_hashes: list[str] = []
    for parent in expected_waves:
        if parent not in trusted_parent_hashes:
            raise ContractValidationError(f"missing trusted parent {parent}")
        expected_hashes.append(
            require_sha256(trusted_parent_hashes[parent], label=parent)
        )
    if supplied != expected_hashes:
        raise ContractValidationError(
            f"parent_handoff_hashes mismatch for {wave}: expected {expected_hashes!r}"
        )
    if len(supplied) != len(set(supplied)):
        raise ContractValidationError("parent_handoff_hashes contains a duplicate")
    return supplied


def sign_handoff(signed_payload: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    payload = dict(signed_payload)
    return {
        "signed_payload": payload,
        "signature": hmac_sha256_hex(key, HANDOFF_DOMAIN, payload),
    }


def handoff_hash(envelope: Mapping[str, Any]) -> str:
    payload = require_object(envelope.get("signed_payload"), label="signed_payload")
    return sha256_hex(canonical_json_bytes(payload))


def _validate_repository_artifact_path(
    value: Any,
    *,
    repository_id: str,
    run_id: str,
    wave: str | None,
    name: str,
) -> str:
    path = require_nonempty_string(value, label=name)
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != path:
        raise ContractValidationError(f"{name} must be normalized repository-relative")
    state_root = ".omg" if repository_id == "OMG" else ".agy"
    expected = (
        f"{state_root}/state/runs/{run_id}/run-manifest.json"
        if wave is None
        else f"{state_root}/artifacts/dual-parity/{run_id}/{wave}/{name}"
    )
    if path != expected:
        raise ContractValidationError(
            f"{name} is not the authoritative repository-local path"
        )
    return path


def _validate_owner_payload_common(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    repository = result.get("repository_id")
    if repository not in {"OMG", "OMA"}:
        raise ContractValidationError("owner payload repository_id must be OMG or OMA")
    run_id = require_safe_id(result.get("run_id"), label="run_id")
    wave = require_safe_id(result.get("wave"), label="wave")
    if not wave.startswith(repository + "-"):
        raise ContractValidationError("owner payload wave/repository mismatch")
    wave_suffix(wave)
    require_safe_id(result.get("owner"), label="owner")
    _validate_repository_artifact_path(
        result.get("run_manifest_path"),
        repository_id=repository,
        run_id=run_id,
        wave=None,
        name="run_manifest_path",
    )
    require_integer(
        result.get("run_manifest_revision"), label="run_manifest_revision", minimum=1
    )
    require_sha256(result.get("run_manifest_hash"), label="run_manifest_hash")
    require_git_oid(result.get("frozen_base_commit"), label="frozen_base_commit")
    require_git_oid(result.get("frozen_base_tree"), label="frozen_base_tree")
    require_integer(result.get("lease_generation"), label="lease_generation", minimum=1)
    parents = result.get("parent_handoff_hashes")
    if not isinstance(parents, list):
        raise ContractValidationError("parent_handoff_hashes must be an array")
    for digest in parents:
        require_sha256(digest, label="parent_handoff_hash")
    if len(parents) != len(set(parents)):
        raise ContractValidationError("parent_handoff_hashes contains a duplicate")
    require_iso8601(result.get("created_at"), label="created_at")
    return result


def validate_targeted_test(value: Mapping[str, Any]) -> dict[str, Any]:
    test = require_object(value, label="targeted_test")
    require_exact_keys(
        test,
        required={"argv", "rc", "stdout_sha256", "stderr_sha256"},
        label="targeted_test",
    )
    if (
        not isinstance(test["argv"], list)
        or not test["argv"]
        or not all(isinstance(item, str) and item for item in test["argv"])
    ):
        raise ContractValidationError(
            "targeted_test.argv must be a non-empty string array"
        )
    require_integer(test["rc"], label="targeted_test.rc")
    require_sha256(test["stdout_sha256"], label="targeted_test.stdout_sha256")
    require_sha256(test["stderr_sha256"], label="targeted_test.stderr_sha256")
    return test


def validate_proposal_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    required = {
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
    }
    require_exact_keys(entry, required=required, label="proposal entry")
    result = dict(entry)
    if result["repository_id"] not in {"OMG", "OMA"}:
        raise ContractValidationError("proposal repository_id must be OMG or OMA")
    for label in ("run_id", "wave", "owner", "proposal_id"):
        require_safe_id(result[label], label=label)
    path = require_nonempty_string(result["path"], label="path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != path:
        raise ContractValidationError(
            "proposal path must be normalized repository-relative"
        )
    for label in ("initial_sha256", "final_sha256"):
        value = result[label]
        if value != "ABSENT":
            require_sha256(value, label=label)
    if result["initial_sha256"] == result["final_sha256"] == "ABSENT":
        raise ContractValidationError("proposal path cannot be absent before and after")
    require_nonempty_string(result["reason"], label="reason")
    validate_targeted_test(result["targeted_test"])
    return result


def _validate_w6_request_path(
    value: Any,
    *,
    repository_id: str,
    run_id: str,
    wave: str,
) -> str:
    path = require_nonempty_string(value, label="w6 request path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in path or str(pure) != path:
        raise ContractValidationError(
            "w6 request path must be normalized repository-relative"
        )
    state_root = ".omg" if repository_id == "OMG" else ".agy"
    expected_prefix = (state_root, "artifacts", "dual-parity", run_id, wave)
    if pure.parts[: len(expected_prefix)] != expected_prefix or len(pure.parts) <= len(
        expected_prefix
    ):
        raise ContractValidationError(
            "w6 request path must be confined to the same repository/run/wave artifact root"
        )
    return path


def validate_w6_request_bindings(
    value: Any,
    *,
    repository_id: str,
    run_id: str,
    wave: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractValidationError("w6_requests must be an array")
    result: list[dict[str, Any]] = []
    paths: list[str] = []
    for raw in value:
        binding = require_object(raw, label="w6 request binding")
        require_exact_keys(
            binding,
            required=W6_REQUEST_BINDING_KEYS,
            label="w6 request binding",
        )
        path = _validate_w6_request_path(
            binding["path"],
            repository_id=repository_id,
            run_id=run_id,
            wave=wave,
        )
        require_integer(
            binding["byte_length"], label="w6 request byte_length", minimum=1
        )
        require_sha256(binding["sha256"], label="w6 request sha256")
        result.append(dict(binding))
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise ContractValidationError("w6_requests contains a duplicate path")
    if paths != sorted(paths, key=lambda item: item.encode("utf-8")):
        raise ContractValidationError("w6_requests must be UTF-8 byte sorted by path")
    return result


def _read_current_w6_request(
    repository_root: Path | str,
    path: str,
) -> bytes:
    root = Path(repository_root).resolve()
    pure = PurePosixPath(path)
    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ContractValidationError(f"w6 request path contains a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ContractValidationError(
            f"w6 request is missing or unsafe: {path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractValidationError(f"w6 request must be a regular file: {path}")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ContractValidationError(f"w6 request mode must be 0600: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    body = b"".join(chunks)
    parsed = parse_canonical_json_bytes(body)
    if not isinstance(parsed, dict):
        raise ContractValidationError(
            f"w6 request must be a canonical JSON object: {path}"
        )
    return body


def build_w6_request_binding(
    repository_root: Path | str,
    request_path: Path | str,
    *,
    repository_id: str,
    run_id: str,
    wave: str,
) -> dict[str, Any]:
    path = _validate_w6_request_path(
        str(request_path),
        repository_id=repository_id,
        run_id=run_id,
        wave=wave,
    )
    body = _read_current_w6_request(repository_root, path)
    return {"path": path, "byte_length": len(body), "sha256": sha256_hex(body)}


def verify_w6_request_bindings_current(
    value: Any,
    repository_root: Path | str,
    *,
    repository_id: str,
    run_id: str,
    wave: str,
) -> list[dict[str, Any]]:
    bindings = validate_w6_request_bindings(
        value,
        repository_id=repository_id,
        run_id=run_id,
        wave=wave,
    )
    for binding in bindings:
        current = build_w6_request_binding(
            repository_root,
            binding["path"],
            repository_id=repository_id,
            run_id=run_id,
            wave=wave,
        )
        if current != binding:
            raise ContractValidationError(
                f"w6 request current bytes differ from proposal binding: {binding['path']}"
            )
    return bindings


def validate_proposal_index_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = require_object(value, label="proposal index payload")
    require_exact_keys(
        payload,
        required=PROPOSAL_INDEX_PAYLOAD_KEYS,
        label="proposal index payload",
    )
    if (
        payload["store_kind"] != "owner_proposal_index"
        or payload["schema_version"] != 1
    ):
        raise ContractValidationError("proposal index header mismatch")
    result = _validate_owner_payload_common(payload)
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise ContractValidationError(
            "proposal index must contain at least one path entry"
        )
    paths: list[str] = []
    for raw in entries:
        entry = validate_proposal_entry(require_object(raw, label="proposal entry"))
        for field in ("repository_id", "run_id", "wave", "owner"):
            if entry[field] != result[field]:
                raise ContractValidationError(
                    f"proposal entry {field} differs from index"
                )
        paths.append(entry["path"])
    if len(paths) != len(set(paths)) or paths != sorted(
        paths, key=lambda item: item.encode("utf-8")
    ):
        raise ContractValidationError(
            "proposal paths must be unique and UTF-8 byte sorted"
        )
    validate_w6_request_bindings(
        payload["w6_requests"],
        repository_id=result["repository_id"],
        run_id=result["run_id"],
        wave=result["wave"],
    )
    return result


def validate_owner_handoff_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = require_object(value, label="owner handoff payload")
    require_exact_keys(
        payload,
        required=OWNER_HANDOFF_PAYLOAD_KEYS,
        label="owner handoff payload",
    )
    if payload["store_kind"] != "owner_handoff" or payload["schema_version"] != 1:
        raise ContractValidationError("owner handoff header mismatch")
    result = _validate_owner_payload_common(payload)
    _validate_repository_artifact_path(
        result["proposal_index_path"],
        repository_id=result["repository_id"],
        run_id=result["run_id"],
        wave=result["wave"],
        name="proposal-index.json",
    )
    require_sha256(result["proposal_index_hash"], label="proposal_index_hash")
    records = result["path_records"]
    if not isinstance(records, list) or not records:
        raise ContractValidationError(
            "owner handoff must contain at least one path record"
        )
    paths: list[str] = []
    for raw in records:
        record = validate_path_record(require_object(raw, label="path record"))
        for field in ("repository_id", "run_id", "wave", "owner"):
            if record[field] != result[field]:
                raise ContractValidationError(
                    f"path record {field} differs from handoff"
                )
        if record["proposal_hash"] != result["proposal_index_hash"]:
            raise ContractValidationError(
                "path record proposal_hash differs from proposal index"
            )
        paths.append(record["path"])
    if len(paths) != len(set(paths)) or paths != sorted(
        paths, key=lambda item: item.encode("utf-8")
    ):
        raise ContractValidationError(
            "handoff paths must be unique and UTF-8 byte sorted"
        )
    return result


def _verify_owner_envelope_signature(
    envelope: Mapping[str, Any], key: bytes
) -> dict[str, Any]:
    require_exact_keys(
        envelope,
        required={"signed_payload", "signature"},
        label="owner envelope",
    )
    payload = require_object(envelope["signed_payload"], label="signed_payload")
    expected_signature = hmac_sha256_hex(key, HANDOFF_DOMAIN, payload)
    signature = require_sha256(envelope["signature"], label="signature")
    if not hmac.compare_digest(signature, expected_signature):
        raise ContractValidationError("handoff signature mismatch")
    return payload


def verify_proposal_index(
    envelope: Mapping[str, Any],
    key: bytes,
    *,
    expected_repository: str,
    expected_run_id: str,
    expected_wave: str,
    expected_owner: str,
    trusted_parent_hashes: Mapping[str, str],
    repository_root: Path | str,
) -> str:
    payload = _verify_owner_envelope_signature(envelope, key)
    validate_proposal_index_payload(payload)
    for field, expected in (
        ("repository_id", expected_repository),
        ("run_id", expected_run_id),
        ("wave", expected_wave),
        ("owner", expected_owner),
    ):
        if payload[field] != expected:
            raise ContractValidationError(f"foreign {field} proposal index")
    validate_parent_hashes(
        expected_wave,
        payload["parent_handoff_hashes"],
        trusted_parent_hashes,
    )
    verify_w6_request_bindings_current(
        payload["w6_requests"],
        repository_root,
        repository_id=expected_repository,
        run_id=expected_run_id,
        wave=expected_wave,
    )
    return sha256_hex(canonical_json_bytes(payload))


def verify_handoff(
    envelope: Mapping[str, Any],
    key: bytes,
    *,
    expected_repository: str,
    expected_run_id: str,
    expected_wave: str,
    expected_owner: str,
    trusted_parent_hashes: Mapping[str, str],
) -> str:
    payload = _verify_owner_envelope_signature(envelope, key)
    validate_owner_handoff_payload(payload)
    if payload.get("repository_id") != expected_repository:
        raise ContractValidationError("foreign repository handoff")
    if payload.get("run_id") != expected_run_id:
        raise ContractValidationError("foreign run handoff")
    if payload.get("wave") != expected_wave:
        raise ContractValidationError("foreign wave handoff")
    if payload.get("owner") != expected_owner:
        raise ContractValidationError("foreign owner handoff")
    parents = payload.get("parent_handoff_hashes")
    if not isinstance(parents, list):
        raise ContractValidationError("parent_handoff_hashes must be an array")
    validate_parent_hashes(expected_wave, parents, trusted_parent_hashes)
    return sha256_hex(canonical_json_bytes(payload))


def validate_path_record(record: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "repository_id",
        "run_id",
        "wave",
        "owner",
        "path",
        "initial_sha256",
        "final_sha256",
        "reason",
        "proposal_id",
        "proposal_hash",
        "targeted_test",
    }
    require_exact_keys(record, required=required, label="path record")
    result = dict(record)
    if result["repository_id"] not in {"OMG", "OMA"}:
        raise ContractValidationError("path record repository_id must be OMG or OMA")
    for label in ("run_id", "wave", "owner", "proposal_id"):
        require_safe_id(result[label], label=label)
    path = require_nonempty_string(result["path"], label="path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != path:
        raise ContractValidationError(
            "path record path must be normalized repository-relative"
        )
    for label in ("initial_sha256", "final_sha256"):
        value = result[label]
        if value != "ABSENT":
            require_sha256(value, label=label)
    if result["initial_sha256"] == result["final_sha256"] == "ABSENT":
        raise ContractValidationError("path record cannot be absent before and after")
    require_nonempty_string(result["reason"], label="reason")
    require_sha256(result["proposal_hash"], label="proposal_hash")
    validate_targeted_test(
        require_object(result["targeted_test"], label="targeted_test")
    )
    return result


def _aggregate_envelope(
    payload: Mapping[str, Any],
    key: bytes,
    *,
    repository: str,
    domain: bytes,
) -> dict[str, Any]:
    if repository not in REPOSITORY_AGGREGATE_SIGNERS:
        raise ContractValidationError("repository must be OMG or OMA")
    canonical = canonical_json_bytes(payload)
    payload_hash = sha256_hex(canonical)
    key_id = sha256_hex(key)
    return {
        "algorithm": "HMAC-SHA256",
        "signer_id": REPOSITORY_AGGREGATE_SIGNERS[repository],
        "aggregate_key_id": key_id,
        "payload_hash": payload_hash,
        "payload": dict(payload),
        "signature": hmac.new(key, domain + canonical, hashlib.sha256).hexdigest(),
    }


def sign_input_aggregate(
    input_payload: Mapping[str, Any], key: bytes, *, repository: str
) -> dict[str, Any]:
    payload = dict(input_payload)
    if payload.get("repository_id") != repository:
        raise ContractValidationError("aggregate repository mismatch")
    roots = payload.get("ordered_owner_roots")
    if not isinstance(roots, list) or len(roots) != 6:
        raise ContractValidationError("input aggregate needs exactly six owner roots")
    expected_waves = [f"{repository}-W{index}" for index in range(6)]
    if [row.get("wave") for row in roots if isinstance(row, Mapping)] != expected_waves:
        raise ContractValidationError("owner roots must be ordered W0 through W5")
    if payload.get("final_commit") is not None:
        raise ContractValidationError("input aggregate final_commit must be null")
    return _aggregate_envelope(
        payload, key, repository=repository, domain=INPUT_AGGREGATE_DOMAIN
    )


def sign_final_aggregate(
    final_payload: Mapping[str, Any], key: bytes, *, repository: str
) -> dict[str, Any]:
    payload = dict(final_payload)
    if payload.get("repository_id") != repository:
        raise ContractValidationError("aggregate repository mismatch")
    input_envelope = payload.get("input_envelope")
    if not isinstance(input_envelope, Mapping):
        raise ContractValidationError("final aggregate must preserve input_envelope")
    for field in (
        "final_commit",
        "final_tree",
        "pushed_oid",
        "complete_delta_root",
        "semver",
        "release_nonce",
        "release_bundle_manifest_path",
        "release_bundle_manifest_sha256",
        "release_bundle_manifest_schema",
        "public_upload_order",
        "release_asset_root",
    ):
        if field not in payload or payload[field] in (None, ""):
            raise ContractValidationError(f"final aggregate missing {field}")
    return _aggregate_envelope(
        payload, key, repository=repository, domain=FINAL_AGGREGATE_DOMAIN
    )


def verify_aggregate_envelope(
    envelope: Mapping[str, Any],
    key: bytes,
    *,
    repository: str,
    kind: str,
) -> str:
    require_exact_keys(
        envelope,
        required={
            "algorithm",
            "signer_id",
            "aggregate_key_id",
            "payload_hash",
            "payload",
            "signature",
        },
        label="aggregate envelope",
    )
    if envelope["algorithm"] != "HMAC-SHA256":
        raise ContractValidationError("unsupported aggregate algorithm")
    payload = require_object(envelope["payload"], label="aggregate payload")
    if payload.get("repository_id") != repository:
        raise ContractValidationError("cross-repository aggregate replay")
    if envelope["signer_id"] != REPOSITORY_AGGREGATE_SIGNERS.get(repository):
        raise ContractValidationError("aggregate signer mismatch")
    if envelope["aggregate_key_id"] != sha256_hex(key):
        raise ContractValidationError("aggregate key ID mismatch")
    domain = INPUT_AGGREGATE_DOMAIN if kind == "input" else FINAL_AGGREGATE_DOMAIN
    if kind not in {"input", "final"}:
        raise ContractValidationError("aggregate kind must be input or final")
    canonical = canonical_json_bytes(payload)
    payload_hash = sha256_hex(canonical)
    if envelope["payload_hash"] != payload_hash:
        raise ContractValidationError("aggregate payload hash mismatch")
    expected = hmac.new(key, domain + canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(envelope["signature"]), expected):
        raise ContractValidationError("aggregate signature mismatch")
    return payload_hash


def _normalize_owned_path(path: str) -> str:
    path = path.replace("\\", "/")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != path:
        raise ContractValidationError(f"unsafe repository path: {path!r}")
    return path


def path_matches_pattern(path: str, pattern: str) -> bool:
    """Match a frozen owner pattern, treating ``/**`` as subtree ownership."""

    path = _normalize_owned_path(path)
    pattern = _normalize_owned_path(pattern)
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatchcase(path, pattern)


def owner_for_path(path: str, ownership: Mapping[str, Sequence[str]]) -> str:
    if PurePosixPath(path).name == "AGENTS.md":
        raise ContractValidationError("AGENTS.md is immutable")
    owners = [
        owner
        for owner, patterns in ownership.items()
        if any(path_matches_pattern(path, pattern) for pattern in patterns)
    ]
    if len(owners) != 1:
        raise ContractValidationError(
            f"path {path!r} must match exactly one owner, matched {owners!r}"
        )
    return owners[0]


@dataclass(frozen=True)
class GitChangeRecord:
    source: str
    kind: str
    path: str
    old_path: str | None = None
    old_mode: str | None = None
    new_mode: str | None = None
    old_oid: str | None = None
    new_oid: str | None = None
    status: str | None = None

    def owned_paths(self) -> tuple[str, ...]:
        return (self.old_path, self.path) if self.old_path else (self.path,)


def _git(root: Path, argv: Sequence[str], *, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", *argv],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise ContractValidationError(
            f"git {' '.join(argv)} failed rc={result.returncode}: "
            f"{result.stderr.decode('utf-8', errors='replace')[:400]}"
        )
    return result.stdout


def parse_raw_diff_z(body: bytes, *, source: str) -> list[GitChangeRecord]:
    """Parse ``git diff --raw -z`` while retaining modes/OIDs/rename pairs."""

    if not body:
        return []
    tokens = body.split(b"\0")
    if tokens[-1] != b"":
        raise ContractValidationError("raw git diff is not NUL terminated")
    tokens.pop()
    records: list[GitChangeRecord] = []
    index = 0
    while index < len(tokens):
        header = tokens[index]
        index += 1
        if not header.startswith(b":"):
            raise ContractValidationError("malformed raw git diff header")
        try:
            metadata = header[1:].decode("ascii").split()
        except UnicodeDecodeError as exc:
            raise ContractValidationError("non-ASCII raw diff metadata") from exc
        if len(metadata) != 5:
            raise ContractValidationError(
                "raw git diff header must contain five fields"
            )
        old_mode, new_mode, old_oid, new_oid, status = metadata
        if index >= len(tokens):
            raise ContractValidationError("raw git diff omitted path")
        try:
            first_path = tokens[index].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractValidationError("git path is not UTF-8") from exc
        index += 1
        old_path: str | None = None
        path = first_path
        if status.startswith(("R", "C")):
            if index >= len(tokens):
                raise ContractValidationError("rename/copy omitted destination")
            old_path = first_path
            try:
                path = tokens[index].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractValidationError(
                    "git destination path is not UTF-8"
                ) from exc
            index += 1
        records.append(
            GitChangeRecord(
                source=source,
                kind="raw",
                path=_normalize_owned_path(path),
                old_path=_normalize_owned_path(old_path) if old_path else None,
                old_mode=old_mode,
                new_mode=new_mode,
                old_oid=old_oid,
                new_oid=new_oid,
                status=status,
            )
        )
    return records


def collect_dirty_records(root: Path | str, base_commit: str) -> list[GitChangeRecord]:
    root_path = Path(root).resolve()
    records = parse_raw_diff_z(
        _git(
            root_path,
            [
                "diff",
                "--cached",
                "--raw",
                "-z",
                "--no-abbrev",
                "--find-renames=50%",
                base_commit,
                "--",
            ],
        ),
        source="base_to_index",
    )
    records += parse_raw_diff_z(
        _git(
            root_path,
            ["diff", "--raw", "-z", "--no-abbrev", "--find-renames=50%", "--"],
        ),
        source="index_to_worktree",
    )
    untracked = _git(
        root_path, ["ls-files", "--others", "--exclude-standard", "-z"]
    ).split(b"\0")
    for raw_path in untracked:
        if raw_path:
            records.append(
                GitChangeRecord(
                    source="untracked",
                    kind="untracked",
                    path=_normalize_owned_path(raw_path.decode("utf-8")),
                )
            )
    base_paths = set(
        item.decode("utf-8")
        for item in _git(
            root_path, ["ls-tree", "-r", "--name-only", "-z", base_commit]
        ).split(b"\0")
        if item
    )
    cached_ignored = _git(
        root_path,
        ["ls-files", "--cached", "--ignored", "--exclude-standard", "-z"],
    ).split(b"\0")
    for raw_path in cached_ignored:
        if raw_path:
            path = _normalize_owned_path(raw_path.decode("utf-8"))
            if path not in base_paths:
                records.append(
                    GitChangeRecord(
                        source="cached_ignored",
                        kind="force_added_ignored",
                        path=path,
                    )
                )
    submodules = _git(root_path, ["submodule", "status", "--recursive"]).decode(
        "utf-8", errors="strict"
    )
    for line in submodules.splitlines():
        if not line:
            continue
        marker = line[0]
        oid, separator, path_and_description = line[1:].partition(" ")
        if not separator or not oid or not path_and_description:
            raise ContractValidationError("malformed submodule status")
        path = path_and_description
        if path.endswith(")") and " (" in path:
            path = path.rsplit(" (", 1)[0]
        path = _normalize_owned_path(path)
        porcelain = _git(
            root_path,
            ["-C", path, "status", "--porcelain=v2", "-z", "--untracked-files=all"],
        )
        if marker != " " or porcelain:
            records.append(
                GitChangeRecord(
                    source="submodule",
                    kind="submodule",
                    path=path,
                    new_oid=oid,
                    status=marker,
                )
            )
    records.sort(key=lambda row: (row.path.encode("utf-8"), row.kind, row.source))
    seen: set[tuple[str, str, str, str | None]] = set()
    for row in records:
        identity = (row.source, row.kind, row.path, row.old_path)
        if identity in seen:
            raise ContractValidationError(f"duplicate dirty record: {identity!r}")
        seen.add(identity)
    return records


def verify_dirty_ownership(
    root: Path | str,
    base_commit: str,
    ownership: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for record in collect_dirty_records(root, base_commit):
        matched_owner: str | None = None
        for path in record.owned_paths():
            owner = owner_for_path(path, ownership)
            if matched_owner is not None and matched_owner != owner:
                raise ContractValidationError("rename/copy crosses writer ownership")
            matched_owner = owner
        verified.append({"owner": matched_owner, **record.__dict__})
    return verified


def verify_final_candidate(
    root: Path | str,
    *,
    base_commit: str,
    candidate_commit: str,
    ownership: Mapping[str, Sequence[str]],
    remote: str | None = None,
    approved_branch: str | None = None,
    approved_remote_old_oid: str | None = None,
) -> list[dict[str, Any]]:
    root_path = Path(root).resolve()
    parents = (
        _git(root_path, ["rev-list", "--parents", "-n", "1", candidate_commit])
        .decode()
        .split()
    )
    if len(parents) != 2 or parents[1] != base_commit:
        raise ContractValidationError(
            "candidate must have exactly frozen_base_commit as parent"
        )
    status = _git(
        root_path, ["status", "--porcelain=v2", "-z", "--untracked-files=all"]
    )
    if status:
        raise ContractValidationError(
            "candidate verification requires a clean residual tree"
        )
    delta = parse_raw_diff_z(
        _git(
            root_path,
            [
                "diff-tree",
                "-r",
                "--raw",
                "-z",
                "--no-abbrev",
                "--find-renames=50%",
                f"{base_commit}^{{tree}}",
                f"{candidate_commit}^{{tree}}",
                "--",
            ],
        ),
        source="final_tree",
    )
    verified: list[dict[str, Any]] = []
    for record in delta:
        owner: str | None = None
        for path in record.owned_paths():
            current = owner_for_path(path, ownership)
            if owner is not None and current != owner:
                raise ContractValidationError("final rename crosses writer ownership")
            owner = current
        verified.append({"owner": owner, **record.__dict__})
    if any(
        value is not None
        for value in (remote, approved_branch, approved_remote_old_oid)
    ):
        if not all((remote, approved_branch, approved_remote_old_oid)):
            raise ContractValidationError(
                "remote verification requires all remote fields"
            )
        output = _git(
            root_path,
            ["ls-remote", "--exit-code", str(remote), f"refs/heads/{approved_branch}"],
        ).decode("utf-8")
        rows = [line.split() for line in output.splitlines() if line.strip()]
        if len(rows) != 1 or rows[0][0] != approved_remote_old_oid:
            raise ContractValidationError("approved remote old OID drifted")
    return verified
