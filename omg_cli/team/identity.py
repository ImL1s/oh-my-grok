"""Confined per-worker identity record (leader-owned)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_iso8601,
    require_safe_id,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from omg_cli.redaction import redact_value


CLI_WRITER = "omg-cli"
IDENTITY_STORE_KIND = "team_worker_identity"
IDENTITY_SCHEMA_VERSION = 1
MAX_IDENTITY_BYTES = 16_384

_IDENTITY_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "worker_id",
        "role",
        "generation",
        "attributes",
        "written_at",
    }
)


class IdentityError(RuntimeError):
    """Worker identity confinement or CAS failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def identity_path(
    root: Path | str, run_id: str, team_id: str, worker_id: str
) -> Path:
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    require_safe_id(worker_id, label="worker_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
        / "workers"
        / safe_path_key(worker_id, namespace="worker")
        / "identity.json"
    )


def _validate_identity(
    value: Mapping[str, Any], *, run_id: str, team_id: str, worker_id: str
) -> dict[str, Any]:
    row = dict(value)
    require_exact_keys(row, required=_IDENTITY_KEYS, label="team worker identity")
    if (
        row["store_kind"] != IDENTITY_STORE_KIND
        or row["schema_version"] != IDENTITY_SCHEMA_VERSION
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("team worker identity header mismatch")
    if (
        row["run_id"] != run_id
        or row["team_id"] != team_id
        or row["worker_id"] != worker_id
    ):
        raise ContractValidationError("team worker identity identity mismatch")
    require_safe_id(row["role"], label="role")
    require_integer(row["generation"], label="generation", minimum=0)
    require_iso8601(row["written_at"], label="written_at")
    if not isinstance(row["attributes"], (dict, list, str, int, bool, type(None))):
        raise ContractValidationError("team worker identity attributes are not JSON-safe")
    return row


def write_worker_identity(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    role: str = "worker",
    generation: int = 0,
    attributes: Any = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Write one confined identity record. Never overwrites a different worker."""

    require_safe_id(worker_id, label="worker_id")
    role_id = require_safe_id(role, label="role")
    require_integer(generation, label="generation", minimum=0)
    if expected_generation is not None:
        require_integer(expected_generation, label="expected_generation", minimum=0)
    redacted = redact_value({} if attributes is None else attributes)
    blob = canonical_json_bytes(redacted)
    if len(blob) > MAX_IDENTITY_BYTES:
        raise IdentityError("worker identity attributes exceed bounded byte limit")
    path = identity_path(root, run_id, team_id, worker_id)
    ensure_managed_dir(path.parent)
    candidate = {
        "store_kind": IDENTITY_STORE_KIND,
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "worker_id": worker_id,
        "role": role_id,
        "generation": generation,
        "attributes": redacted,
        "written_at": _utc_now(),
    }
    _validate_identity(
        candidate, run_id=run_id, team_id=team_id, worker_id=worker_id
    )
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            parsed = parse_canonical_json_bytes(path.read_bytes())
            if not isinstance(parsed, dict):
                raise ContractValidationError("team worker identity must be an object")
            existing = _validate_identity(
                parsed, run_id=run_id, team_id=team_id, worker_id=worker_id
            )
            if existing["worker_id"] != worker_id:
                raise IdentityError("worker identity path does not match worker_id")
            if expected_generation is not None and existing["generation"] != expected_generation:
                raise IdentityError("worker identity generation CAS mismatch")
            if (
                existing["generation"] == generation
                and existing["role"] == role_id
                and existing["attributes"] == redacted
            ):
                return {**existing, "duplicate": True, "path": str(path)}
            if existing["generation"] > generation:
                raise IdentityError("worker identity generation is stale")
        elif expected_generation is not None and expected_generation != 0:
            raise IdentityError("worker identity generation CAS mismatch")
        atomic_write_bytes(
            path, canonical_json_bytes(candidate), mode=DATA_FILE_MODE, replace=True
        )
    return {**candidate, "duplicate": False, "path": str(path)}


__all__ = [
    "IDENTITY_SCHEMA_VERSION",
    "IDENTITY_STORE_KIND",
    "IdentityError",
    "identity_path",
    "write_worker_identity",
]
