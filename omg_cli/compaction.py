"""Generation-fenced, lossless runtime compaction checkpoints."""

from __future__ import annotations

import base64
import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    IMMUTABLE_SOURCE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.resume_contract import validate_recovery_manifest
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_integer,
    require_safe_id,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)
from omg_cli.redaction import redact_value


class CompactionError(RuntimeError):
    """A compaction generation or checkpoint encoding is invalid."""


def _checkpoint_path(root: Path | str, run_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    key = safe_path_key(run_id, namespace="compaction-run")
    return Path(root).resolve() / ".omg" / "state" / "compaction" / key / "checkpoint.json"


def _validate_checkpoint(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "run_id",
        "generation",
        "guidance_base64",
        "guidance_sha256",
        "receipts",
        "recovery_manifest",
        "recovery_receipt",
    }
    if set(row) != required:
        raise ContractValidationError("compaction checkpoint keys mismatch")
    if row["store_kind"] != "runtime_compaction_checkpoint" or row["schema_version"] != 1:
        raise ContractValidationError("compaction checkpoint header mismatch")
    require_safe_id(row["run_id"], label="run_id")
    require_integer(row["generation"], label="generation", minimum=0)
    if not isinstance(row["guidance_base64"], str) or not isinstance(
        row["guidance_sha256"], str
    ):
        raise ContractValidationError("compaction guidance encoding is malformed")
    try:
        guidance = base64.b64decode(row["guidance_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ContractValidationError("compaction guidance is not canonical base64") from exc
    if sha256_hex(guidance) != row["guidance_sha256"]:
        raise ContractValidationError("compaction guidance hash mismatch")
    if not isinstance(row["receipts"], list) or not all(
        isinstance(receipt, dict) for receipt in row["receipts"]
    ):
        raise ContractValidationError("compaction receipts must be objects")
    if not isinstance(row["recovery_manifest"], dict):
        raise ContractValidationError("compaction recovery manifest must be an object")
    if not isinstance(row["recovery_receipt"], dict):
        raise ContractValidationError("compaction recovery receipt must be an object")
    return row


_COPY_NAME_RE = re.compile(r"^source-[0-9a-f]{64}-([0-9a-f]{32})\.jsonl$")
_RECOVERY_RECEIPT_KEYS = {
    "store_kind",
    "schema_version",
    "artifact_id",
    "manifest_path",
    "manifest_sha256",
    "manifest_mode",
    "immutable_copy_path",
    "immutable_copy_sha256",
    "immutable_copy_mode",
}


def _read_immutable_regular(path: Path, *, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CompactionError(f"{label} is missing") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CompactionError(f"{label} must be a regular non-symlink file")
    if stat.S_IMODE(before.st_mode) != IMMUTABLE_SOURCE_MODE:
        raise CompactionError(f"{label} mode must be 0400")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CompactionError(f"{label} is unsafe") from exc
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

    def identity(row: os.stat_result) -> tuple[int, int, int, int]:
        return (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)

    try:
        path_after = path.lstat()
    except OSError as exc:
        raise CompactionError(f"{label} changed during verification") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != IMMUTABLE_SOURCE_MODE
        or stat.S_ISLNK(path_after.st_mode)
        or not stat.S_ISREG(path_after.st_mode)
        or identity(before) != identity(opened)
        or identity(opened) != identity(after)
        or identity(after) != identity(path_after)
    ):
        raise CompactionError(f"{label} changed during verification")
    return body


def _verify_recovery_artifacts(
    recovery_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    recovery = validate_recovery_manifest(recovery_manifest)
    copy_path = Path(str(recovery["immutable_copy_path"]))
    match = _COPY_NAME_RE.fullmatch(copy_path.name)
    if match is None:
        raise CompactionError("recovery copy name does not carry a unique artifact ID")
    artifact_id = match.group(1)
    if not copy_path.is_absolute():
        raise CompactionError("recovery copy path must be absolute")
    manifest_path = copy_path.parent / f"recovery-manifest-{artifact_id}.json"
    receipt_path = copy_path.parent / f"recovery-receipt-{artifact_id}.json"

    copy_body = _read_immutable_regular(copy_path, label="recovery copy")
    if sha256_hex(copy_body) != recovery["immutable_copy_sha256"]:
        raise CompactionError("recovery copy hash mismatch")
    manifest_body = _read_immutable_regular(
        manifest_path, label="recovery manifest"
    )
    if manifest_body != canonical_json_bytes(recovery):
        raise CompactionError("recovery manifest bytes differ from supplied manifest")
    receipt_body = _read_immutable_regular(receipt_path, label="recovery receipt")
    try:
        parsed_receipt = parse_canonical_json_bytes(receipt_body)
    except ValueError as exc:
        raise CompactionError("recovery receipt is not canonical JSON") from exc
    if not isinstance(parsed_receipt, dict) or set(parsed_receipt) != _RECOVERY_RECEIPT_KEYS:
        raise CompactionError("recovery receipt schema mismatch")
    expected_receipt = {
        "store_kind": "bounded_recovery_receipt",
        "schema_version": 1,
        "artifact_id": artifact_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_hex(manifest_body),
        "manifest_mode": "0400",
        "immutable_copy_path": str(copy_path),
        "immutable_copy_sha256": sha256_hex(copy_body),
        "immutable_copy_mode": "0400",
    }
    if parsed_receipt != expected_receipt:
        raise CompactionError("recovery receipt does not bind exact artifact bytes")
    return dict(recovery), {
        **parsed_receipt,
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_hex(receipt_body),
        "receipt_mode": "0400",
    }


def load_compaction_checkpoint(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    parsed = parse_canonical_json_bytes(source.read_bytes())
    if not isinstance(parsed, dict):
        raise ContractValidationError("compaction checkpoint must be an object")
    return _validate_checkpoint(parsed)


def create_compaction_checkpoint(
    root: Path | str,
    *,
    run_id: str,
    generation: int,
    guidance: bytes,
    receipts: Sequence[Mapping[str, Any]],
    recovery_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Create or idempotently adopt the checkpoint for one generation."""

    require_safe_id(run_id, label="run_id")
    require_integer(generation, label="generation", minimum=0)
    if not isinstance(guidance, bytes):
        raise ContractValidationError("compaction guidance must be bytes")
    redacted_receipts = redact_value([dict(receipt) for receipt in receipts])
    recovery, recovery_receipt = _verify_recovery_artifacts(recovery_manifest)
    if not isinstance(redacted_receipts, list):
        raise ContractValidationError("compaction payload is malformed")
    checkpoint = _validate_checkpoint(
        {
            "store_kind": "runtime_compaction_checkpoint",
            "schema_version": 1,
            "run_id": run_id,
            "generation": generation,
            "guidance_base64": base64.b64encode(guidance).decode("ascii"),
            "guidance_sha256": sha256_hex(guidance),
            "receipts": redacted_receipts,
            "recovery_manifest": recovery,
            "recovery_receipt": recovery_receipt,
        }
    )
    body = canonical_json_bytes(checkpoint)
    path = _checkpoint_path(root, run_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            current = load_compaction_checkpoint(path)
            if generation < current["generation"]:
                raise CompactionError("stale generation")
            if generation == current["generation"] and path.read_bytes() != body:
                raise CompactionError("generation already has different checkpoint bytes")
            if path.read_bytes() == body:
                return {"path": str(path), "sha256": sha256_hex(body), "checkpoint": current}
        atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=True)
    return {"path": str(path), "sha256": sha256_hex(body), "checkpoint": checkpoint}


def render_resume_context(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Decode lossless guidance while retaining the redacted metadata."""

    row = _validate_checkpoint(checkpoint)
    guidance = base64.b64decode(row["guidance_base64"], validate=True)
    return {
        "guidance": guidance,
        "receipts": list(row["receipts"]),
        "recovery_manifest": dict(row["recovery_manifest"]),
        "recovery_receipt": dict(row["recovery_receipt"]),
        "generation": row["generation"],
        "run_id": row["run_id"],
    }


__all__ = [
    "CompactionError",
    "create_compaction_checkpoint",
    "load_compaction_checkpoint",
    "render_resume_context",
]
