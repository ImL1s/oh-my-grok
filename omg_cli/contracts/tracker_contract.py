"""Tracker leases, imported carrier comparison and Grok receipt contracts."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_iso8601,
    require_nonempty_string,
    require_object,
    require_safe_id,
    require_sha256,
)
from .writer_chain import canonical_json_bytes, sha256_hex


ROLE_INTENT_RE = re.compile(r"^omx_role_intent_([0-9a-f]{32})$")
AGENT_PATH_RE = re.compile(r"^/root/(omx_role_intent_([0-9a-f]{32}))$")
CAPABILITY_MODES = ("read-only", "read-write")
MAX_PROCESS_NATIVE_BINDINGS = 4096
_NATIVE_BINDING_LOCK = threading.Lock()
_NATIVE_BINDINGS: dict[tuple[str, str], tuple[str, str]] = {}


def _parse_role_task(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractValidationError(f"{label} must be a string or null")
    match = ROLE_INTENT_RE.fullmatch(value)
    if not match:
        raise ContractValidationError(f"{label} is not an exact adapted role-intent task")
    return match.group(1)


def _parse_agent_path(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractValidationError("agent_path must be a string or null")
    match = AGENT_PATH_RE.fullmatch(value)
    if not match:
        raise ContractValidationError("agent_path must match /root/omx_role_intent_<32hex>")
    return match.group(2)


def parse_imported_carriers(
    value: Mapping[str, Any],
    *,
    declared_imported_evidence: bool,
    now: datetime | None = None,
    expected_parent_thread_id: str | None = None,
    expected_cwd_hash: str | None = None,
    expected_run_id: str | None = None,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    """Parse Codex carriers for comparison evidence, never native authority."""

    if not declared_imported_evidence:
        raise ContractValidationError("Codex carriers require declared imported evidence")
    carrier = require_object(value, label="imported carrier")
    allowed = {
        "agent_role",
        "agent_type",
        "correlation_token",
        "task_name",
        "agent_path",
        "parent_thread_id",
        "cwd_hash",
        "run_id",
        "session_id",
        "expires_at",
        "used",
    }
    extra = set(carrier) - allowed
    if extra:
        raise ContractValidationError(f"unexpected imported carrier fields: {sorted(extra)!r}")
    required_bindings = {
        "correlation_token",
        "parent_thread_id",
        "cwd_hash",
        "run_id",
        "session_id",
        "expires_at",
        "used",
    }
    missing = required_bindings - set(carrier)
    if missing:
        raise ContractValidationError(
            f"imported carrier missing binding/replay fields: {sorted(missing)!r}"
        )
    typed_role = carrier.get("agent_role")
    typed_type = carrier.get("agent_type")
    for label, item in (("agent_role", typed_role), ("agent_type", typed_type)):
        if item is not None:
            require_safe_id(item, label=label)
    if typed_role is not None and typed_type is not None and typed_role != typed_type:
        raise ContractValidationError("typed agent_role/agent_type disagree")
    typed_token = carrier.get("correlation_token")
    if not isinstance(typed_token, str) or not re.fullmatch(r"[0-9a-f]{32}", typed_token):
        raise ContractValidationError("correlation_token must be 32 lowercase hex")
    task_token = _parse_role_task(carrier.get("task_name"), label="task_name")
    path_token = _parse_agent_path(carrier.get("agent_path"))
    present_tokens = [item for item in (typed_token, task_token, path_token) if item]
    if present_tokens and len(set(present_tokens)) != 1:
        raise ContractValidationError("typed/task/path provenance carriers disagree")
    if not (typed_role or task_token or path_token):
        raise ContractValidationError("no supported imported provenance carrier")
    for label in ("parent_thread_id", "cwd_hash", "run_id", "session_id"):
        require_nonempty_string(carrier[label], label=label)
    require_sha256(carrier["cwd_hash"], label="cwd_hash")
    if carrier.get("used") is not False:
        raise ContractValidationError("imported carrier token was already used")
    timestamp = require_iso8601(carrier["expires_at"], label="expires_at")
    parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if parsed <= current:
        raise ContractValidationError("imported carrier expired")
    expected_fields = {
        "parent_thread_id": expected_parent_thread_id,
        "cwd_hash": expected_cwd_hash,
        "run_id": expected_run_id,
        "session_id": expected_session_id,
    }
    for field, expected in expected_fields.items():
        if expected is None:
            raise ContractValidationError(
                f"expected {field} is required to bind imported evidence"
            )
        if carrier.get(field) != expected:
            raise ContractValidationError(f"imported carrier {field} mismatch")
    return {
        "provenance_kind": "imported_comparison",
        "role": typed_role or typed_type,
        "correlation_token": present_tokens[0] if present_tokens else None,
        "authority": "none",
        "native_child_authorized": False,
    }


SPAWN_RECEIPT_REQUIRED = {
    "store_kind",
    "schema_version",
    "receipt_id",
    "run_id",
    "team_id",
    "task_id",
    "parent_id",
    "parent_session_id",
    "requested_role",
    "capability_mode",
    "depth",
    "attempt",
    "receipt_generation",
    "lease_generation",
    "dispatch_nonce",
    "expires_at",
    "expected_state",
    "expected_sequence",
}


def validate_spawn_receipt(
    value: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    receipt = require_object(value, label="spawn_receipt")
    require_exact_keys(receipt, required=SPAWN_RECEIPT_REQUIRED, label="spawn_receipt")
    if receipt["store_kind"] != "spawn_receipt" or receipt["schema_version"] != 1:
        raise ContractValidationError("spawn_receipt header mismatch")
    for field in (
        "receipt_id",
        "run_id",
        "team_id",
        "task_id",
        "parent_id",
        "parent_session_id",
        "requested_role",
        "dispatch_nonce",
        "expected_state",
    ):
        require_safe_id(receipt[field], label=field)
    if receipt["capability_mode"] not in CAPABILITY_MODES:
        raise ContractValidationError("capability_mode must be read-only or read-write")
    if require_integer(receipt["depth"], label="depth", minimum=1) != 1:
        raise ContractValidationError("Grok subagent depth must be exactly one")
    for field in ("attempt", "receipt_generation", "lease_generation", "expected_sequence"):
        require_integer(receipt[field], label=field, minimum=0)
    timestamp = require_iso8601(receipt["expires_at"], label="expires_at")
    if now is not None:
        parsed = datetime.fromisoformat(
            timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        )
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if parsed <= current:
            raise ContractValidationError("spawn receipt expired")
    return receipt


def make_role_receipt(spawn_receipt: Mapping[str, Any]) -> dict[str, Any]:
    spawn = validate_spawn_receipt(spawn_receipt)
    spawn_hash = sha256_hex(canonical_json_bytes(spawn))
    role = {
        "store_kind": "role_receipt",
        "schema_version": 1,
        "receipt_id": f"role-{spawn['receipt_id']}",
        "spawn_receipt_hash": spawn_hash,
        "run_id": spawn["run_id"],
        "team_id": spawn["team_id"],
        "task_id": spawn["task_id"],
        "parent_id": spawn["parent_id"],
        "parent_session_id": spawn["parent_session_id"],
        "requested_role": spawn["requested_role"],
        "capability_mode": spawn["capability_mode"],
        "depth": spawn["depth"],
        "attempt": spawn["attempt"],
        "receipt_generation": spawn["receipt_generation"],
        "lease_generation": spawn["lease_generation"],
        "dispatch_nonce": spawn["dispatch_nonce"],
        "expires_at": spawn["expires_at"],
        "expected_state": spawn["expected_state"],
        "expected_sequence": spawn["expected_sequence"],
    }
    return role


def bind_native_spawn(
    spawn_receipt: Mapping[str, Any],
    role_receipt: Mapping[str, Any],
    *,
    host_spawn_id: str,
    observed_session_id: str,
    expected_generation: int,
    expected_run_id: str | None = None,
    expected_task_id: str | None = None,
    expected_parent_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    spawn = validate_spawn_receipt(
        spawn_receipt,
        now=now or datetime.now(timezone.utc),
    )
    role = require_object(role_receipt, label="role_receipt")
    expected_role = make_role_receipt(spawn)
    if role != expected_role:
        raise ContractValidationError("role_receipt disagrees with spawn_receipt")
    if spawn["receipt_generation"] != expected_generation:
        raise ContractValidationError("stale spawn receipt generation")
    for field, expected in (
        ("run_id", expected_run_id),
        ("task_id", expected_task_id),
        ("parent_id", expected_parent_id),
    ):
        if expected is not None and spawn[field] != expected:
            raise ContractValidationError(f"spawn receipt {field} mismatch")
    require_safe_id(host_spawn_id, label="host_spawn_id")
    require_safe_id(observed_session_id, label="observed_session_id")
    spawn_hash = sha256_hex(canonical_json_bytes(spawn))
    role_hash = sha256_hex(canonical_json_bytes(role))
    binding_key = (spawn_hash, role_hash)
    identity = (host_spawn_id, observed_session_id)
    with _NATIVE_BINDING_LOCK:
        previous = _NATIVE_BINDINGS.get(binding_key)
        if previous is not None and previous != identity:
            raise ContractValidationError(
                "spawn/role receipts are already bound to conflicting native IDs"
            )
        if previous is None:
            if len(_NATIVE_BINDINGS) >= MAX_PROCESS_NATIVE_BINDINGS:
                raise ContractValidationError("native receipt binding table is full")
            _NATIVE_BINDINGS[binding_key] = identity
    return {
        "store_kind": "native_spawn_binding",
        "schema_version": 1,
        "run_id": spawn["run_id"],
        "task_id": spawn["task_id"],
        "parent_id": spawn["parent_id"],
        "host_spawn_id": host_spawn_id,
        "observed_session_id": observed_session_id,
        "spawn_receipt_hash": spawn_hash,
        "role_receipt_hash": role_hash,
        "receipt_generation": expected_generation,
        "expected_state": spawn["expected_state"],
        "transition_sequence": spawn["expected_sequence"] + 1,
        "identity_truth": "grok_native_receipts",
    }


def validate_projector_lease(value: Mapping[str, Any]) -> dict[str, Any]:
    lease = require_object(value, label="projector lease")
    require_exact_keys(
        lease,
        required={
            "store_kind",
            "schema_version",
            "pid",
            "process_start_identity",
            "owner_token",
            "generation",
            "last_successful_poll",
            "cursor",
            "error",
        },
        label="projector lease",
    )
    if lease["store_kind"] != "tracker_projector_lease" or lease["schema_version"] != 1:
        raise ContractValidationError("projector lease header mismatch")
    require_integer(lease["pid"], label="pid", minimum=1)
    require_nonempty_string(lease["process_start_identity"], label="process_start_identity")
    require_safe_id(lease["owner_token"], label="owner_token")
    require_integer(lease["generation"], label="generation", minimum=0)
    require_iso8601(lease["last_successful_poll"], label="last_successful_poll")
    require_nonempty_string(lease["cursor"], label="cursor")
    if lease["error"] is not None and not isinstance(lease["error"], str):
        raise ContractValidationError("error must be null or a redacted string")
    return lease
