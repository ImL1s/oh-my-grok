"""Versioned, bounded team task envelope shared by native and tmux carriers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from .state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_nonempty_string,
    require_object,
    require_safe_id,
    require_sha256,
    require_string_list,
)
from .tracker_contract import CAPABILITY_MODES


MAX_PROMPT_BYTES = 131_072
MAX_VERIFICATION_COMMANDS = 32
MAX_WRITE_PATHS = 256


def _safe_write_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ContractValidationError("worker write scope must be normalized repository-relative")
    parts = path.parts
    authoritative_wave_path = (
        len(parts) >= 5
        and parts[0] in {".omg", ".agy"}
        and parts[1:3] == ("artifacts", "dual-parity")
        and parts[4] in {"OMG-W6", "OMG-W7", "OMA-W6", "OMA-W7"}
    )
    if (
        path.name == "AGENTS.md"
        or value in {".omg/state", ".agy/state"}
        or value.startswith((".omg/state/", ".agy/state/"))
        or authoritative_wave_path
        or path.name in {"aggregate-handoff.json", "release-bundle-manifest.json"}
    ):
        raise ContractValidationError("worker may not write immutable/canonical authority paths")
    return value


def _validate_argv(argv: list[str]) -> None:
    shell_names = {"sh", "bash", "zsh", "fish", "dash", "cmd", "powershell", "pwsh"}
    if PurePosixPath(argv[0]).name in shell_names:
        raise ContractValidationError("worker verification command may not invoke a shell")
    for argument in argv:
        if any(marker in argument for marker in ("$(", "${", "`", "&&", "||", ";", "\n")):
            raise ContractValidationError("worker verification argv contains shell interpolation")


def validate_worker_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    envelope = require_object(value, label="worker envelope")
    required = {
        "store_kind",
        "schema_version",
        "run_id",
        "team_id",
        "task_id",
        "parent_task_id",
        "dependencies",
        "dependency_results",
        "prompt",
        "requested_role",
        "capability_mode",
        "depth",
        "write_scope",
        "verification_commands",
        "artifact_contract",
        "guidance_hashes",
        "mailbox_cursor",
        "claim_generation",
        "state_endpoint",
        "cancellation_token",
        "expected_state",
        "expected_sequence",
    }
    require_exact_keys(envelope, required=required, label="worker envelope")
    if envelope["store_kind"] != "worker_envelope" or envelope["schema_version"] != 1:
        raise ContractValidationError("worker envelope header mismatch")
    for field in (
        "run_id",
        "team_id",
        "task_id",
        "requested_role",
        "cancellation_token",
        "expected_state",
    ):
        require_safe_id(envelope[field], label=field)
    if envelope["parent_task_id"] is not None:
        require_safe_id(envelope["parent_task_id"], label="parent_task_id")
    require_string_list(envelope["dependencies"], label="dependencies", unique=True)
    require_object(envelope["dependency_results"], label="dependency_results")
    prompt = require_nonempty_string(envelope["prompt"], label="prompt")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ContractValidationError("worker prompt exceeds bounded byte cap")
    if envelope["capability_mode"] not in CAPABILITY_MODES:
        raise ContractValidationError("worker capability_mode must be restricted")
    if require_integer(envelope["depth"], label="depth", minimum=1) != 1:
        raise ContractValidationError("worker depth must be exactly one")
    write_scope = require_string_list(envelope["write_scope"], label="write_scope", unique=True)
    for path in write_scope:
        _safe_write_path(path)
    if len(write_scope) > MAX_WRITE_PATHS:
        raise ContractValidationError("worker write scope is unbounded")
    if envelope["capability_mode"] == "read-only" and write_scope:
        raise ContractValidationError("read-only worker may not declare write paths")
    commands = envelope["verification_commands"]
    if not isinstance(commands, list) or len(commands) > MAX_VERIFICATION_COMMANDS:
        raise ContractValidationError("verification_commands is not a bounded array")
    for argv in commands:
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ContractValidationError("each verification command must be a non-empty argv array")
        _validate_argv(argv)
    artifact = require_object(envelope["artifact_contract"], label="artifact_contract")
    require_exact_keys(
        artifact,
        required={"kind"},
        optional={"schema", "path", "sha256"},
        label="artifact_contract",
    )
    artifact_kind = require_safe_id(artifact["kind"], label="artifact_contract.kind")
    if artifact_kind in {
        "aggregate-handoff",
        "canonical-state",
        "release-bundle",
        "release-authority",
        "verified",
    }:
        raise ContractValidationError("worker artifact contract may not claim authority")
    if "schema" in artifact:
        require_safe_id(artifact["schema"], label="artifact_contract.schema")
    if "path" in artifact:
        _safe_write_path(require_nonempty_string(artifact["path"], label="artifact_contract.path"))
    if "sha256" in artifact:
        require_sha256(artifact["sha256"], label="artifact_contract.sha256")
    hashes = require_object(envelope["guidance_hashes"], label="guidance_hashes")
    for name, digest in hashes.items():
        require_nonempty_string(name, label="guidance name")
        require_sha256(digest, label=f"guidance hash {name}")
    require_nonempty_string(envelope["mailbox_cursor"], label="mailbox_cursor")
    require_integer(envelope["claim_generation"], label="claim_generation", minimum=0)
    require_nonempty_string(envelope["state_endpoint"], label="state_endpoint")
    require_integer(envelope["expected_sequence"], label="expected_sequence", minimum=0)
    return envelope
