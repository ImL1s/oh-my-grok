"""Frozen ``repository-workflow/v1`` schema and deterministic planning helpers.

Runtime execution belongs to W4.  W0 freezes definition bytes, topology,
permissions, identities, replay IDs and terminal semantics without trusting
preview Grok Rhai syntax.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Mapping, Sequence
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
)
from .tracker_contract import CAPABILITY_MODES
from .writer_chain import canonical_json_bytes, sha256_hex


WORKFLOW_CONTRACT = "repository-workflow/v1"
WORKFLOW_TERMINALS = (
    "ship",
    "no_ship",
    "blocked",
    "cancelled",
    "failed",
    "interrupted",
    "effect_unknown",
)
WORKFLOW_CAPABILITY_TIERS = {
    "T0": "unavailable",
    "T1": "saved_prompt",
    "T2": "validated_runner",
    "T3": "durable_journal",
    "T4": "enforced_gate",
    "T5": "recoverable_effects",
}
STAGE_KINDS = ("author", "check", "verifier", "skeptic")
ALLOWED_PERMISSIONS = frozenset(
    {
        "read_repository",
        "write_declared_paths",
        "invoke_declared_mcp",
        "emit_declared_artifact",
        "run_declared_verification",
        "request_cli_transition",
        "reconcile_declared_effect",
    }
)
SIDE_EFFECT_PERMISSIONS = ALLOWED_PERMISSIONS - {"read_repository"}
FORBIDDEN_PERMISSIONS = frozenset(
    {
        "spawn_nested_agent",
        "run_nested_workflow",
        "supervise_workflow",
        "write_verified",
        "write_release_state",
        "publish_release",
        "unrestricted_shell",
    }
)
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_WORKFLOW_AGENTS = 16
MAX_PARALLELISM = 8
MAX_STAGES = 128
MAX_MATRIX_ROWS = 256


def task_requires_terminable_executor(task: Mapping[str, Any]) -> bool:
    """Return whether a planned task holds authority that may escape timeout."""
    permissions = set(task.get("permissions", ()))
    return bool(
        task.get("effect_type") is not None
        or task.get("write_paths")
        or task.get("mcp_allowlist")
        or permissions & SIDE_EFFECT_PERMISSIONS
    )


def workflow_definition_digest(value: Mapping[str, Any]) -> str:
    definition = dict(value)
    definition.pop("definition_digest", None)
    return sha256_hex(canonical_json_bytes(definition))


def _safe_repo_path(path: Any, *, label: str) -> str:
    text = require_nonempty_string(path, label=label)
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != text:
        raise ContractValidationError(f"{label} must be normalized repository-relative")
    if pure.name == "AGENTS.md":
        raise ContractValidationError("workflow may not write immutable AGENTS.md")
    if any(
        text == prefix.rstrip("/") or text.startswith(prefix)
        for prefix in (
            ".omg/state/",
            ".agy/state/",
            ".omg/artifacts/dual-parity/",
            ".agy/artifacts/dual-parity/",
        )
    ):
        raise ContractValidationError("workflow may not write canonical/release authority roots")
    return text


def _validate_argv(argv: Any, *, label: str) -> list[str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise ContractValidationError(f"{label} must be a non-empty argv array")
    shell_names = {"sh", "bash", "zsh", "fish", "dash", "cmd", "powershell", "pwsh"}
    if PurePosixPath(argv[0]).name in shell_names:
        raise ContractValidationError(f"{label} may not invoke a shell interpreter")
    for argument in argv:
        if any(marker in argument for marker in ("$(", "${", "`", "&&", "||", ";", "\n")):
            raise ContractValidationError(f"{label} contains shell interpolation syntax")
    return argv


STAGE_KEYS = {
    "id",
    "declaration_index",
    "depends_on",
    "kind",
    "role",
    "actor_identity",
    "capability_mode",
    "agent_count",
    "matrix",
    "mcp_allowlist",
    "write_paths",
    "permissions",
    "output_schema",
    "verification_argv",
    "timeout_seconds",
    "retry_budget",
    "artifact_contract",
    "effect_type",
}


def validate_stage(value: Mapping[str, Any]) -> dict[str, Any]:
    stage = require_object(value, label="workflow stage")
    require_exact_keys(stage, required=STAGE_KEYS, label="workflow stage")
    require_safe_id(stage["id"], label="stage.id")
    require_integer(stage["declaration_index"], label="declaration_index", minimum=0)
    if not isinstance(stage["depends_on"], list) or not all(
        isinstance(item, str) for item in stage["depends_on"]
    ):
        raise ContractValidationError("depends_on must be a string array")
    if len(stage["depends_on"]) != len(set(stage["depends_on"])):
        raise ContractValidationError("depends_on must not contain duplicates")
    if stage["kind"] not in STAGE_KINDS:
        raise ContractValidationError("workflow stage kind is unsupported")
    role = require_safe_id(stage["role"], label="stage.role")
    if "supervisor" in role or "workflow" in role:
        raise ContractValidationError("nested workflow/supervisor role is forbidden")
    require_safe_id(stage["actor_identity"], label="actor_identity")
    if stage["capability_mode"] not in CAPABILITY_MODES:
        raise ContractValidationError("stage capability_mode must be read-only or read-write")
    count = require_integer(stage["agent_count"], label="agent_count", minimum=1)
    if count > MAX_WORKFLOW_AGENTS:
        raise ContractValidationError("stage agent_count is unbounded")
    matrix = stage["matrix"]
    if not isinstance(matrix, list) or not matrix or len(matrix) > MAX_MATRIX_ROWS:
        raise ContractValidationError("stage matrix must be a bounded non-empty array")
    for row in matrix:
        require_object(row, label="matrix row")
    if count != len(matrix):
        raise ContractValidationError("agent_count must equal fixed matrix row count")
    if not isinstance(stage["mcp_allowlist"], list) or not all(
        isinstance(item, str) and item for item in stage["mcp_allowlist"]
    ):
        raise ContractValidationError("mcp_allowlist must be a string array")
    if len(stage["mcp_allowlist"]) != len(set(stage["mcp_allowlist"])):
        raise ContractValidationError("mcp_allowlist must be unique")
    if not isinstance(stage["write_paths"], list):
        raise ContractValidationError("write_paths must be an array")
    write_paths = [_safe_repo_path(item, label="write path") for item in stage["write_paths"]]
    if len(write_paths) != len(set(write_paths)):
        raise ContractValidationError("write_paths must be unique")
    permissions = stage["permissions"]
    if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
        raise ContractValidationError("permissions must be a string array")
    if len(permissions) != len(set(permissions)):
        raise ContractValidationError("permissions must be unique")
    unknown_permissions = set(permissions) - ALLOWED_PERMISSIONS
    if unknown_permissions or set(permissions) & FORBIDDEN_PERMISSIONS:
        raise ContractValidationError(
            f"workflow stage requests unsupported/privileged permission: {sorted(unknown_permissions)!r}"
        )
    if stage["capability_mode"] == "read-only":
        if write_paths or "write_declared_paths" in permissions:
            raise ContractValidationError("read-only stage may not declare writes")
    else:
        if not write_paths or "write_declared_paths" not in permissions:
            raise ContractValidationError("read-write stage requires declared paths and permission")
    if stage["mcp_allowlist"] and "invoke_declared_mcp" not in permissions:
        raise ContractValidationError("MCP allowlist requires invoke_declared_mcp permission")
    require_object(stage["output_schema"], label="output_schema")
    verification = stage["verification_argv"]
    if not isinstance(verification, list):
        raise ContractValidationError("verification_argv must be an array of argv arrays")
    for index, argv in enumerate(verification):
        _validate_argv(argv, label=f"verification_argv[{index}]")
    require_integer(stage["timeout_seconds"], label="timeout_seconds", minimum=1)
    retry = require_integer(stage["retry_budget"], label="retry_budget", minimum=0)
    if retry > 10:
        raise ContractValidationError("retry budget is unbounded")
    artifact = require_object(stage["artifact_contract"], label="artifact_contract")
    require_exact_keys(
        artifact,
        required={"paths", "schema", "required"},
        label="artifact_contract",
    )
    if not isinstance(artifact["paths"], list):
        raise ContractValidationError("artifact paths must be an array")
    for item in artifact["paths"]:
        _safe_repo_path(item, label="artifact path")
    require_object(artifact["schema"], label="artifact schema")
    if not isinstance(artifact["required"], bool):
        raise ContractValidationError("artifact required must be boolean")
    if stage["effect_type"] is not None:
        effect_type = require_safe_id(stage["effect_type"], label="effect_type")
        if "release" in effect_type or "publish" in effect_type:
            raise ContractValidationError("workflow may not declare release/publication effects")
        if "reconcile_declared_effect" not in permissions:
            raise ContractValidationError("effect_type requires reconciliation permission")
    return stage


DEFINITION_KEYS = {
    "contract",
    "name",
    "workflow_version",
    "definition_digest",
    "input_schema",
    "stages",
    "max_parallelism",
    "ship_rule",
    "migration",
    "native_projection",
}


def validate_workflow_definition(value: Mapping[str, Any]) -> dict[str, Any]:
    definition = require_object(value, label="workflow definition")
    require_exact_keys(definition, required=DEFINITION_KEYS, label="workflow definition")
    if definition["contract"] != WORKFLOW_CONTRACT:
        raise ContractValidationError("workflow contract must be repository-workflow/v1")
    name = require_nonempty_string(definition["name"], label="workflow name")
    if not WORKFLOW_NAME_RE.fullmatch(name):
        raise ContractValidationError("workflow name must be canonical lowercase kebab-case")
    version = require_nonempty_string(definition["workflow_version"], label="workflow_version")
    if not SEMVER_RE.fullmatch(version):
        raise ContractValidationError("workflow_version must be semantic version")
    expected_digest = workflow_definition_digest(definition)
    if definition["definition_digest"] != expected_digest:
        raise ContractValidationError("workflow definition digest mismatch")
    require_object(definition["input_schema"], label="input_schema")
    stages_raw = definition["stages"]
    if not isinstance(stages_raw, list) or not stages_raw or len(stages_raw) > MAX_STAGES:
        raise ContractValidationError("workflow stages must be bounded non-empty array")
    stages = [validate_stage(item) for item in stages_raw]
    stage_ids = [stage["id"] for stage in stages]
    if len(stage_ids) != len(set(stage_ids)):
        raise ContractValidationError("workflow stage IDs must be unique")
    indexes = [stage["declaration_index"] for stage in stages]
    if sorted(indexes) != list(range(len(stages))):
        raise ContractValidationError("declaration_index must be complete 0..N-1")
    stage_set = set(stage_ids)
    for stage in stages:
        if stage["id"] in stage["depends_on"]:
            raise ContractValidationError("workflow stage cannot depend on itself")
        stale = set(stage["depends_on"]) - stage_set
        if stale:
            raise ContractValidationError(f"workflow stage has stale dependencies: {sorted(stale)!r}")
    max_parallel = require_integer(
        definition["max_parallelism"], label="max_parallelism", minimum=1
    )
    if max_parallel > MAX_PARALLELISM:
        raise ContractValidationError("workflow max_parallelism is unbounded")
    if sum(stage["agent_count"] for stage in stages) > MAX_WORKFLOW_AGENTS:
        raise ContractValidationError("workflow total agent_count is unbounded")
    order = deterministic_topological_order(stages)
    if len(order) != len(stages):  # pragma: no cover - helper raises cycles
        raise ContractValidationError("workflow DAG is incomplete")
    actors_by_kind: dict[str, set[str]] = {kind: set() for kind in STAGE_KINDS}
    for stage in stages:
        actors_by_kind[stage["kind"]].add(stage["actor_identity"])
    if not actors_by_kind["author"] or not actors_by_kind["verifier"] or not actors_by_kind["skeptic"]:
        raise ContractValidationError("workflow requires author, verifier and skeptic stages")
    if actors_by_kind["author"] & (actors_by_kind["verifier"] | actors_by_kind["skeptic"]):
        raise ContractValidationError("author identity may not verify or skeptic-review itself")
    if actors_by_kind["verifier"] & actors_by_kind["skeptic"]:
        raise ContractValidationError("verifier and skeptic identities must be distinct")
    ship = require_object(definition["ship_rule"], label="ship_rule")
    require_exact_keys(
        ship,
        required={"required_stages", "verifier_stage", "skeptic_stage", "predicate"},
        label="ship_rule",
    )
    if not isinstance(ship["required_stages"], list) or set(ship["required_stages"]) != stage_set:
        raise ContractValidationError("ship_rule.required_stages must include every fixed stage")
    stage_by_id = {stage["id"]: stage for stage in stages}
    verifier_id = ship["verifier_stage"]
    skeptic_id = ship["skeptic_stage"]
    if verifier_id not in stage_by_id or stage_by_id[verifier_id]["kind"] != "verifier":
        raise ContractValidationError("ship_rule verifier_stage is invalid")
    if skeptic_id not in stage_by_id or stage_by_id[skeptic_id]["kind"] != "skeptic":
        raise ContractValidationError("ship_rule skeptic_stage is invalid")
    if ship["predicate"] != "all_required_pass_and_independent_verifier_skeptic_approve":
        raise ContractValidationError("workflow ship predicate is not the frozen hard gate")
    migration = require_object(definition["migration"], label="migration")
    require_exact_keys(
        migration,
        required={"supersedes_version", "supersedes_digest", "review_digest"},
        label="migration",
    )
    if migration["review_digest"] is not None:
        require_sha256(migration["review_digest"], label="migration.review_digest")
    if migration["supersedes_version"] is None:
        if migration["supersedes_digest"] is not None:
            raise ContractValidationError("supersedes digest requires supersedes version")
    else:
        if not SEMVER_RE.fullmatch(str(migration["supersedes_version"])):
            raise ContractValidationError("supersedes_version must be semantic")
        require_sha256(migration["supersedes_digest"], label="supersedes_digest")
        if migration["review_digest"] is None:
            raise ContractValidationError("workflow migration requires independent review digest")
    projection = require_object(definition["native_projection"], label="native_projection")
    require_exact_keys(
        projection,
        required={"provider", "status", "syntax", "probe_evidence", "fresh_invocation_evidence"},
        label="native_projection",
    )
    if projection["provider"] != "grok":
        raise ContractValidationError("OMG W0 workflow projection provider must be grok")
    if projection["status"] == "optional_unclaimed":
        if projection["syntax"] is not None:
            raise ContractValidationError("unclaimed native projection may not carry executable syntax")
    elif projection["status"] == "claimed":
        if not projection["probe_evidence"] or not projection["fresh_invocation_evidence"]:
            raise ContractValidationError("native workflow claim needs public probe and fresh invocation")
        raise ContractValidationError("E_WORKFLOW_NATIVE_UNSUPPORTED")
    else:
        raise ContractValidationError("native projection status is unsupported")
    return definition


def deterministic_topological_order(stages: Sequence[Mapping[str, Any]]) -> list[str]:
    stage_by_id = {str(stage["id"]): stage for stage in stages}
    indegree = {stage_id: 0 for stage_id in stage_by_id}
    children: dict[str, list[str]] = {stage_id: [] for stage_id in stage_by_id}
    for stage_id, stage in stage_by_id.items():
        dependencies = stage.get("depends_on")
        if not isinstance(dependencies, list):
            raise ContractValidationError("depends_on must be an array")
        for dependency in dependencies:
            if dependency not in stage_by_id:
                raise ContractValidationError(f"stale workflow dependency: {dependency!r}")
            indegree[stage_id] += 1
            children[dependency].append(stage_id)
    heap: list[tuple[int, str]] = []
    for stage_id, count in indegree.items():
        if count == 0:
            stage = stage_by_id[stage_id]
            heapq.heappush(heap, (int(stage["declaration_index"]), stage_id))
    order: list[str] = []
    while heap:
        _, stage_id = heapq.heappop(heap)
        order.append(stage_id)
        for child in children[stage_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                stage = stage_by_id[child]
                heapq.heappush(heap, (int(stage["declaration_index"]), child))
    if len(order) != len(stage_by_id):
        raise ContractValidationError("workflow dependency graph contains a cycle")
    return order


def ensure_immutable_same_version(
    installed: Mapping[str, Any], candidate: Mapping[str, Any]
) -> None:
    old = validate_workflow_definition(installed)
    new = validate_workflow_definition(candidate)
    if old["name"] != new["name"]:
        raise ContractValidationError("workflow registry name mismatch")
    if old["workflow_version"] == new["workflow_version"]:
        if old["definition_digest"] != new["definition_digest"]:
            raise ContractValidationError("same workflow version has mutable bytes")
    elif new["migration"]["supersedes_version"] != old["workflow_version"] or new[
        "migration"
    ]["supersedes_digest"] != old["definition_digest"]:
        raise ContractValidationError("new workflow version lacks exact supersedes binding")


def effective_permissions(
    declared: Sequence[str],
    repository_policy: Sequence[str],
    host_capabilities: Sequence[str],
    launch_receipt_permissions: Sequence[str],
) -> tuple[str, ...]:
    declared_set = set(declared)
    if declared_set - ALLOWED_PERMISSIONS:
        raise ContractValidationError("workflow declared unsupported permission")
    effective = declared_set & set(repository_policy) & set(host_capabilities) & set(
        launch_receipt_permissions
    )
    return tuple(permission for permission in declared if permission in effective)


def workflow_task_id(
    *,
    repository_id: str,
    workflow_name: str,
    workflow_version: str,
    definition_digest: str,
    input_digest: str,
    stage_id: str,
    matrix_index: int,
    run_generation: int,
) -> str:
    if repository_id not in {"OMG", "OMA"}:
        raise ContractValidationError("repository_id must be OMG or OMA")
    require_sha256(definition_digest, label="definition_digest")
    require_sha256(input_digest, label="input_digest")
    require_integer(matrix_index, label="matrix_index", minimum=0)
    require_integer(run_generation, label="run_generation", minimum=0)
    return sha256_hex(
        canonical_json_bytes(
            [
                repository_id,
                workflow_name,
                workflow_version,
                definition_digest,
                input_digest,
                stage_id,
                matrix_index,
                run_generation,
            ]
        )
    )


def workflow_replay_id(task_id: str, verified_receipt_hash: str | None) -> str:
    require_sha256(task_id, label="task_id")
    if verified_receipt_hash is not None:
        require_sha256(verified_receipt_hash, label="verified_receipt_hash")
    return sha256_hex(canonical_json_bytes([task_id, verified_receipt_hash]))


def decide_terminal(
    *,
    required_stage_results: Mapping[str, str],
    verifier_approved: bool,
    skeptic_approved: bool,
    permission_denied: bool = False,
    ambiguous_receipt: bool = False,
    external_effect_without_receipt: bool = False,
) -> str:
    if external_effect_without_receipt:
        return "effect_unknown"
    if permission_denied or ambiguous_receipt:
        return "blocked"
    if not required_stage_results or any(
        result not in {"passed", "approved"} for result in required_stage_results.values()
    ):
        return "no_ship"
    if not verifier_approved or not skeptic_approved:
        return "no_ship"
    return "ship"
