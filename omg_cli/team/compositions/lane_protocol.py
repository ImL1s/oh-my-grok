"""Composition Lane Worker Protocol V1 (#69 PR13).

Shared worker-scoped claim/submit adapters for Hyperplan and Security Research.
Routes every authoritative mutation through existing ``claim-task`` /
``transition-task-status``. Does **not** launch workers, panes, Jobs,
providers, PoC, or MCP surfaces. ``execution_supported`` remains ``false``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import exclusive_lock
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_object,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.team.api import (
    TeamApiError,
    _read_task,
    execute_team_api,
    resolve_team_api_cli_root,
    team_api_worker_context_present,
)
from omg_cli.team.compositions.task_driver import (
    MAX_INLINE_JSON_BYTES,
    CompositionTaskAdapter,
    CompositionTaskDriverError,
    assert_task_matches_lane_binding,
    parse_lane_task_result_v1,
    resolve_composition_batch_binding_v1,
    resolve_composition_lane_binding_v1,
)
from omg_cli.team.plane import (
    TEAM_ID_ENV,
    TEAM_RUN_ID_ENV,
    in_non_team_spawn_context,
    team_worker_identity,
)
from omg_cli.team.task_batch import _batch_binding, batch_record_path

COMPOSITION_LANE_CLAIM_KIND = "omg.team.composition_lane_claim"
COMPOSITION_LANE_CLAIM_SCHEMA_VERSION = 1
_CLAIM_REQUIRED = frozenset(
    {
        "kind",
        "schema_version",
        "source_kind",
        "run_id",
        "team_id",
        "composition_id",
        "composition_digest",
        "batch_id",
        "batch_digest",
        "lane_id",
        "task_id",
        "worker_id",
        "task_version",
        "claim_token",
        "leased_until",
        "lane",
        "input",
        "dependency_outputs",
        "result_contract",
        "execution_supported",
    }
)
_LANE_VIEW_REQUIRED = frozenset(
    {
        "role",
        "posture",
        "scope",
        "requires_code_change",
        "allow_implementation",
        "owned_files",
        "expected_artifact",
    }
)
_SCOPE_REQUIRED = frozenset({"kind", "value"})
_RESULT_CONTRACT_REQUIRED = frozenset({"schema_version", "statuses"})
_DEP_OUTPUT_REQUIRED = frozenset({"lane_id", "status", "payload"})
_DEP_OUTPUT_OPTIONAL = frozenset({"reason"})
_RESULT_STATUSES = ("blocked", "complete", "rejected")


class CompositionLaneProtocolError(ValueError):
    """Fail-closed composition lane worker protocol error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "E_TEAM_COMPOSITION_LANE",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _wrap_driver(exc: CompositionTaskDriverError) -> CompositionLaneProtocolError:
    return CompositionLaneProtocolError(
        str(exc),
        code=exc.code,
        details=getattr(exc, "details", None),
    )


def _require_utc_z(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompositionLaneProtocolError(
            f"{label} must be a non-empty UTC timestamp",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        )
    text = value.strip()
    if not text.endswith("Z"):
        raise CompositionLaneProtocolError(
            f"{label} must be UTC (Z-suffixed)",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        )
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise CompositionLaneProtocolError(
            f"{label} must be ISO-8601 UTC",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CompositionLaneProtocolError(
            f"{label} must be UTC",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        )
    if parsed.utcoffset().total_seconds() != 0:
        raise CompositionLaneProtocolError(
            f"{label} must be UTC",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        )
    return text


def parse_composition_lane_claim_v1(raw: Any) -> dict[str, Any]:
    """Exact-key ``CompositionLaneClaimV1`` parser (fail closed)."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CompositionLaneProtocolError(
                "composition lane claim is not valid UTF-8",
                code="E_TEAM_COMPOSITION_LANE_CLAIM",
            ) from exc
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_INLINE_JSON_BYTES:
            raise CompositionLaneProtocolError(
                "composition lane claim JSON exceeds inline budget",
                code="E_TEAM_COMPOSITION_LANE_CLAIM",
            )
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CompositionLaneProtocolError(
                f"composition lane claim is not valid JSON: {exc}",
                code="E_TEAM_COMPOSITION_LANE_CLAIM",
            ) from exc
    try:
        body = require_object(raw, label="CompositionLaneClaimV1")
        require_exact_keys(
            body,
            required=_CLAIM_REQUIRED,
            optional=frozenset(),
            label="CompositionLaneClaimV1",
        )
        if body.get("kind") != COMPOSITION_LANE_CLAIM_KIND:
            raise ContractValidationError("kind mismatch")
        schema_version = require_integer(
            body.get("schema_version"), label="schema_version", minimum=1
        )
        if schema_version != COMPOSITION_LANE_CLAIM_SCHEMA_VERSION:
            raise ContractValidationError("schema_version must be 1")
        source_kind = require_safe_id(body.get("source_kind"), label="source_kind")
        run_id = require_safe_id(body.get("run_id"), label="run_id")
        team_id = require_safe_id(body.get("team_id"), label="team_id")
        composition_id = require_safe_id(
            body.get("composition_id"), label="composition_id"
        )
        composition_digest = require_sha256(
            body.get("composition_digest"), label="composition_digest"
        )
        batch_id = require_safe_id(body.get("batch_id"), label="batch_id")
        batch_digest = require_sha256(body.get("batch_digest"), label="batch_digest")
        lane_id = require_safe_id(body.get("lane_id"), label="lane_id")
        task_id = require_safe_id(body.get("task_id"), label="task_id")
        if not str(task_id).isdigit():
            raise ContractValidationError("task_id must be numeric")
        worker_id = require_safe_id(body.get("worker_id"), label="worker_id")
        task_version = require_integer(
            body.get("task_version"), label="task_version", minimum=1
        )
        claim_token = body.get("claim_token")
        if not isinstance(claim_token, str) or not claim_token.strip():
            raise ContractValidationError("claim_token must be a non-empty string")
        if len(claim_token) > 256:
            raise ContractValidationError("claim_token too long")
        leased_until = _require_utc_z(body.get("leased_until"), label="leased_until")
        if body.get("execution_supported") is not False:
            raise ContractValidationError("execution_supported must be false")
        lane = require_object(body.get("lane"), label="lane")
        require_exact_keys(
            lane,
            required=_LANE_VIEW_REQUIRED,
            optional=frozenset(),
            label="lane",
        )
        scope = require_object(lane.get("scope"), label="lane.scope")
        require_exact_keys(
            scope,
            required=_SCOPE_REQUIRED,
            optional=frozenset(),
            label="lane.scope",
        )
        scope_kind = require_safe_id(scope.get("kind"), label="lane.scope.kind")
        scope_value = require_safe_id(scope.get("value"), label="lane.scope.value")
        if not isinstance(lane.get("role"), str) or not str(lane["role"]).strip():
            raise ContractValidationError("lane.role must be a non-empty string")
        if not isinstance(lane.get("posture"), str) or not str(lane["posture"]).strip():
            raise ContractValidationError("lane.posture must be a non-empty string")
        if lane.get("requires_code_change") is not False:
            raise ContractValidationError("requires_code_change must be false")
        if lane.get("allow_implementation") is not False:
            raise ContractValidationError("allow_implementation must be false")
        owned = lane.get("owned_files")
        if not isinstance(owned, list) or any(not isinstance(x, str) for x in owned):
            raise ContractValidationError("owned_files must be a string array")
        expected_artifact = require_object(
            lane.get("expected_artifact"), label="expected_artifact"
        )
        input_obj = require_object(body.get("input"), label="input")
        deps = body.get("dependency_outputs")
        if not isinstance(deps, list):
            raise ContractValidationError("dependency_outputs must be an array")
        parsed_deps: list[dict[str, Any]] = []
        for idx, row in enumerate(deps):
            dep = require_object(row, label=f"dependency_outputs[{idx}]")
            require_exact_keys(
                dep,
                required=_DEP_OUTPUT_REQUIRED,
                optional=_DEP_OUTPUT_OPTIONAL,
                label=f"dependency_outputs[{idx}]",
            )
            dep_lane = require_safe_id(dep.get("lane_id"), label="dependency lane_id")
            dep_status = dep.get("status")
            if dep_status not in _RESULT_STATUSES:
                raise ContractValidationError(
                    f"dependency_outputs[{idx}].status invalid"
                )
            dep_payload = require_object(
                dep.get("payload"), label=f"dependency_outputs[{idx}].payload"
            )
            out_dep: dict[str, Any] = {
                "lane_id": dep_lane,
                "status": dep_status,
                "payload": dict(dep_payload),
            }
            if "reason" in dep:
                reason = dep.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ContractValidationError(
                        f"dependency_outputs[{idx}].reason invalid"
                    )
                out_dep["reason"] = reason.strip()
            parsed_deps.append(out_dep)
        result_contract = require_object(
            body.get("result_contract"), label="result_contract"
        )
        require_exact_keys(
            result_contract,
            required=_RESULT_CONTRACT_REQUIRED,
            optional=frozenset(),
            label="result_contract",
        )
        rc_ver = require_integer(
            result_contract.get("schema_version"),
            label="result_contract.schema_version",
            minimum=1,
        )
        if rc_ver != 1:
            raise ContractValidationError("result_contract.schema_version must be 1")
        statuses = result_contract.get("statuses")
        if list(statuses) != list(_RESULT_STATUSES):
            raise ContractValidationError("result_contract.statuses mismatch")
    except (ContractValidationError, CompositionLaneProtocolError) as exc:
        if isinstance(exc, CompositionLaneProtocolError):
            raise
        raise CompositionLaneProtocolError(
            str(exc),
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        ) from exc

    out = {
        "kind": COMPOSITION_LANE_CLAIM_KIND,
        "schema_version": COMPOSITION_LANE_CLAIM_SCHEMA_VERSION,
        "source_kind": source_kind,
        "run_id": run_id,
        "team_id": team_id,
        "composition_id": composition_id,
        "composition_digest": composition_digest,
        "batch_id": batch_id,
        "batch_digest": batch_digest,
        "lane_id": lane_id,
        "task_id": task_id,
        "worker_id": worker_id,
        "task_version": task_version,
        "claim_token": claim_token.strip(),
        "leased_until": leased_until,
        "lane": {
            "role": str(lane["role"]).strip(),
            "posture": str(lane["posture"]).strip(),
            "scope": {"kind": scope_kind, "value": scope_value},
            "requires_code_change": False,
            "allow_implementation": False,
            "owned_files": list(owned),
            "expected_artifact": dict(expected_artifact),
        },
        "input": dict(input_obj),
        "dependency_outputs": parsed_deps,
        "result_contract": {
            "schema_version": 1,
            "statuses": list(_RESULT_STATUSES),
        },
        "execution_supported": False,
    }
    size = len(canonical_json_bytes(out))
    if size > MAX_INLINE_JSON_BYTES:
        raise CompositionLaneProtocolError(
            f"composition lane claim exceeds inline budget ({size})",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        )
    return out


def redact_claim_token(claim: Mapping[str, Any]) -> dict[str, Any]:
    """Human-readable view: claim token redacted (machine JSON keeps it)."""
    out = dict(claim)
    if "claim_token" in out:
        out["claim_token"] = "<redacted>"
    return out


def _lane_scope_view(lane: Mapping[str, Any]) -> dict[str, str]:
    for kind in ("dimension", "surface"):
        value = lane.get(kind)
        if isinstance(value, str) and value.strip():
            return {"kind": kind, "value": require_safe_id(value.strip(), label=kind)}
    return {
        "kind": "lane",
        "value": require_safe_id(lane.get("lane_id"), label="lane_id"),
    }


def _bounded_lane_input(
    *,
    source_kind: str,
    manifest: Mapping[str, Any],
    lane: Mapping[str, Any],
) -> dict[str, Any]:
    """Bounded goal/target + evidence descriptors (never leader conversation)."""
    spec = manifest.get("spec")
    if not isinstance(spec, Mapping):
        raise CompositionLaneProtocolError(
            "manifest.spec must be an object",
            code="E_TEAM_COMPOSITION_LANE_INPUT",
        )
    out: dict[str, Any] = {}
    if source_kind == "hyperplan_v1":
        if "goal" in spec:
            out["goal"] = spec["goal"]
        if "plan_artifact" in spec:
            out["plan_artifact"] = dict(spec["plan_artifact"])
        if "evidence" in spec:
            out["evidence"] = list(spec["evidence"])
        dim = lane.get("dimension")
        if isinstance(dim, str) and dim.strip():
            out["critique_dimension"] = dim.strip()
    elif source_kind == "security_research_v1":
        if "target" in spec:
            out["target"] = spec["target"]
        if "target_artifact" in spec:
            out["target_artifact"] = dict(spec["target_artifact"])
        if "evidence" in spec:
            out["evidence"] = list(spec["evidence"])
        surface = lane.get("surface")
        if isinstance(surface, str) and surface.strip():
            out["attack_surface"] = surface.strip()
        policy = manifest.get("safe_poc_policy")
        if isinstance(policy, Mapping):
            # Immutable policy snapshot; always execution_supported=false.
            out["safe_poc_policy"] = {
                "schema_version": policy.get("schema_version"),
                "allowed_proof_kinds": list(policy.get("allowed_proof_kinds") or []),
                "execution_supported": False,
            }
    else:
        raise CompositionLaneProtocolError(
            f"unsupported source_kind {source_kind!r}",
            code="E_TEAM_COMPOSITION_LANE_INPUT",
        )
    size = len(canonical_json_bytes(out))
    if size > MAX_INLINE_JSON_BYTES:
        raise CompositionLaneProtocolError(
            f"lane input exceeds inline budget ({size})",
            code="E_TEAM_COMPOSITION_LANE_INPUT",
        )
    return out


def _require_worker_gate(
    *,
    root: Path | str,
    run_id: str,
    team_id: str,
    env: Mapping[str, str] | None,
) -> tuple[Path, str, Mapping[str, str]]:
    """Worker-only gate + authoritative leader-root resolution."""
    source: Mapping[str, str]
    if env is None:
        source = os.environ
    else:
        source = env

    if in_non_team_spawn_context(source):
        raise CompositionLaneProtocolError(
            "composition lane protocol refused: already inside a spawned-worker "
            "context (depth-1; process-fanout / spawned-subagent marker set)",
            code="E_TEAM_COMPOSITION_LANE_GATE",
            details={"error": "non_team_spawn_denied"},
        )

    identity = team_worker_identity(source)
    if identity is None:
        if team_api_worker_context_present(source):
            raise CompositionLaneProtocolError(
                "composition lane protocol refused: partial or invalid worker "
                "environment",
                code="E_TEAM_COMPOSITION_LANE_GATE",
                details={"error": "worker_env_incomplete"},
            )
        raise CompositionLaneProtocolError(
            "composition lane protocol refused: leader invocation "
            "(worker-only claim-lane / submit-lane-result)",
            code="E_TEAM_COMPOSITION_LANE_GATE",
            details={"error": "leader_op_denied"},
        )

    try:
        rid = require_safe_id(run_id, label="run_id")
        tid = require_safe_id(team_id, label="team_id")
        worker_id = require_safe_id(identity, label="worker_id")
    except ContractValidationError as exc:
        raise CompositionLaneProtocolError(
            str(exc),
            code="E_TEAM_COMPOSITION_LANE_GATE",
        ) from exc

    env_run = (source.get(TEAM_RUN_ID_ENV) or "").strip()
    env_team = (source.get(TEAM_ID_ENV) or "").strip()
    if env_run != rid or env_team != tid:
        raise CompositionLaneProtocolError(
            "run_id/team_id must match worker environment bindings",
            code="E_TEAM_COMPOSITION_LANE_GATE",
            details={
                "error": "identity_mismatch",
                "caller_run_id": rid,
                "env_run_id": env_run,
                "caller_team_id": tid,
                "env_team_id": env_team,
            },
        )

    try:
        leader_root = resolve_team_api_cli_root(root, env=source)
    except TeamApiError as exc:
        raise CompositionLaneProtocolError(
            str(exc),
            code="E_TEAM_COMPOSITION_LANE_GATE",
            details=dict(exc.details),
        ) from exc

    return leader_root, worker_id, source


def _load_dependency_outputs(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    lane: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    """Load validated dependency results in manifest depends_on order.

    Returns ``(outputs, state)`` where ``state`` is one of:

    - ``ready`` — every dependency is completed, claim-free, and has a
      parseable ``LaneTaskResultV1`` result;
    - ``api_blocked`` — at least one dependency is missing or not
      ``completed`` (Team API ``claim-task`` must return
      ``blocked_dependency`` without mutating the target claim);
    - ``protocol_gap`` — dependencies appear completed to the Team API but
      lack a claim-free result (protocol refuses without claiming, or
      releases if a claim somehow succeeded).
    """
    depends = lane.get("depends_on") or []
    if not isinstance(depends, list):
        raise CompositionLaneProtocolError(
            "lane.depends_on must be an array",
            code="E_TEAM_COMPOSITION_LANE_DEPS",
        )
    mapping = binding["task_key_to_id"]
    compiled = binding["compiled"]
    tasks_by_key = binding["tasks_by_key"]
    outputs: list[dict[str, Any]] = []
    for dep_lane_id in depends:
        dep_key = str(dep_lane_id)
        if dep_key not in mapping:
            raise CompositionLaneProtocolError(
                f"dependency lane {dep_key!r} missing from batch mapping",
                code="E_TEAM_COMPOSITION_LANE_DEPS",
            )
        task_id = str(mapping[dep_key])
        task = _read_task(root, run_id, team_id, task_id)
        if task is None or task.get("status") != "completed":
            return [], "api_blocked"
        if task.get("claim") is not None or task.get("result") is None:
            return [], "protocol_gap"
        try:
            assert_task_matches_lane_binding(
                task,
                lane_id=dep_key,
                expected_batch=_batch_binding(compiled, task_key=dep_key),
                expected_artifact=tasks_by_key[dep_key]["expected_artifact"],
            )
            lane_result = parse_lane_task_result_v1(task.get("result"))
        except CompositionTaskDriverError as exc:
            raise _wrap_driver(exc) from exc
        row: dict[str, Any] = {
            "lane_id": dep_key,
            "status": lane_result["status"],
            "payload": lane_result["payload"],
        }
        if "reason" in lane_result:
            row["reason"] = lane_result["reason"]
        outputs.append(row)
    return outputs, "ready"


def _release_orphaned_claim(
    *,
    root: Path,
    run_id: str,
    team_id: str,
    task_id: str,
    worker_id: str,
    claim_token: str,
    env: Mapping[str, str],
) -> None:
    """Best-effort release; fail closed if the claim remains active."""
    code, envelope = execute_team_api(
        "release-task-claim",
        {
            "run_id": run_id,
            "team_id": team_id,
            "task_id": task_id,
            "claim_token": claim_token,
            "worker": worker_id,
        },
        root=root,
        env=env,
    )
    task = _read_task(root, run_id, team_id, task_id)
    claim_cleared = (
        task is not None
        and task.get("claim") is None
        and task.get("status") != "in_progress"
    )
    if code != 0 or not claim_cleared:
        details = {
            "error": "orphaned_claim_release_failed",
            "task_id": task_id,
            "release_exit_code": code,
        }
        if isinstance(envelope, Mapping) and isinstance(envelope.get("error"), Mapping):
            details["release_error"] = dict(envelope["error"])
        raise CompositionLaneProtocolError(
            "failed to release orphaned claim after protocol dependency refusal",
            code="E_TEAM_COMPOSITION_LANE_DEPS",
            details=details,
        )


def _api_fail(
    operation: str,
    code: int,
    envelope: Mapping[str, Any],
) -> CompositionLaneProtocolError:
    err = envelope.get("error") if isinstance(envelope, Mapping) else None
    details = {}
    message = f"{operation} failed"
    err_code = "E_TEAM_COMPOSITION_LANE_API"
    if isinstance(err, Mapping):
        message = str(err.get("message") or message)
        err_code = str(err.get("code") or err_code)
        if isinstance(err.get("details"), Mapping):
            details = dict(err["details"])
    elif isinstance(envelope.get("data"), Mapping):
        data = envelope["data"]
        if data.get("ok") is False:
            message = str(data.get("error") or message)
            details = dict(data)
    details.setdefault("operation", operation)
    details.setdefault("exit_code", code)
    return CompositionLaneProtocolError(
        message,
        code=err_code if err_code.startswith("E_") else "E_TEAM_COMPOSITION_LANE_API",
        details=details,
    )


def _validate_adapter_lane_payload(
    adapter: CompositionTaskAdapter,
    *,
    lane: Mapping[str, Any],
    lane_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Run composition-specific receipt payload validation before terminal transition."""
    try:
        return adapter.validate_lane_task_result_payload(
            lane=lane, lane_result=lane_result
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.startswith("E_"):
            raise CompositionLaneProtocolError(
                str(exc),
                code=code,
                details={"error": "lane_payload_invalid"},
            ) from exc
        raise CompositionLaneProtocolError(
            f"lane payload validation failed: {exc}",
            code="E_TEAM_COMPOSITION_LANE_RESULT",
            details={"error": "lane_payload_invalid"},
        ) from exc


def claim_composition_lane_v1(
    root: Path | str,
    run_id: str,
    team_id: str,
    lane_id: str,
    adapter: CompositionTaskAdapter,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Worker-only: claim one composition lane via ``claim-task``."""
    leader_root, worker_id, worker_env = _require_worker_gate(
        root=root, run_id=run_id, team_id=team_id, env=env
    )
    try:
        lid = require_safe_id(lane_id, label="lane_id")
    except ContractValidationError as exc:
        raise CompositionLaneProtocolError(
            str(exc),
            code="E_TEAM_COMPOSITION_LANE_INVALID",
        ) from exc

    lock = adapter.composition_lock_path(leader_root, run_id)
    # Resolve batch path after a lightweight preview under composition lock.
    with exclusive_lock(lock):
        try:
            preview = resolve_composition_batch_binding_v1(
                leader_root, run_id, team_id, adapter
            )
        except CompositionTaskDriverError as exc:
            raise _wrap_driver(exc) from exc
        batch_path = batch_record_path(
            leader_root,
            preview["run_id"],
            preview["team_id"],
            str(preview["compiled"]["idempotency_key"]),
        )
        with exclusive_lock(batch_path.with_suffix(".lock")):
            try:
                binding = resolve_composition_batch_binding_v1(
                    leader_root, run_id, team_id, adapter
                )
                lane_binding = resolve_composition_lane_binding_v1(binding, lid)
            except CompositionTaskDriverError as exc:
                raise _wrap_driver(exc) from exc

            task = _read_task(
                leader_root,
                lane_binding["run_id"],
                lane_binding["team_id"],
                lane_binding["task_id"],
            )
            try:
                task = assert_task_matches_lane_binding(
                    task,
                    lane_id=lane_binding["lane_id"],
                    expected_batch=lane_binding["expected_batch"],
                    expected_artifact=lane_binding["expected_artifact"],
                )
            except CompositionTaskDriverError as exc:
                raise _wrap_driver(exc) from exc

            dep_outputs, dep_state = _load_dependency_outputs(
                leader_root,
                run_id=lane_binding["run_id"],
                team_id=lane_binding["team_id"],
                lane=lane_binding["lane"],
                binding=lane_binding,
            )
            # Protocol-stricter gap: refuse without claiming so no orphan sticks.
            if dep_state == "protocol_gap":
                raise CompositionLaneProtocolError(
                    "dependency outputs not ready for protocol claim "
                    "(completed dependencies require a claim-free result)",
                    code="E_TEAM_COMPOSITION_LANE_DEPS",
                    details={"error": "protocol_dependency_gap"},
                )

            lane_input = _bounded_lane_input(
                source_kind=adapter.source_kind,
                manifest=lane_binding["manifest"],
                lane=lane_binding["lane"],
            )
            # Budget the would-be envelope before mutating (deps ready only).
            if dep_state == "ready":
                probe = {
                    "kind": COMPOSITION_LANE_CLAIM_KIND,
                    "schema_version": COMPOSITION_LANE_CLAIM_SCHEMA_VERSION,
                    "source_kind": adapter.source_kind,
                    "run_id": lane_binding["run_id"],
                    "team_id": lane_binding["team_id"],
                    "composition_id": lane_binding["manifest"]["composition_id"],
                    "composition_digest": lane_binding["manifest"]["digest"],
                    "batch_id": lane_binding["compiled"]["batch_id"],
                    "batch_digest": lane_binding["compiled"]["digest"],
                    "lane_id": lane_binding["lane_id"],
                    "task_id": lane_binding["task_id"],
                    "worker_id": worker_id,
                    "task_version": int(task["version"]) + 1,
                    "claim_token": "0" * 36,
                    "leased_until": "2099-01-01T00:00:00Z",
                    "lane": {
                        "role": lane_binding["lane"]["role"],
                        "posture": lane_binding["lane"]["posture"],
                        "scope": _lane_scope_view(lane_binding["lane"]),
                        "requires_code_change": False,
                        "allow_implementation": False,
                        "owned_files": list(
                            lane_binding["lane"].get("owned_files") or []
                        ),
                        "expected_artifact": dict(
                            lane_binding["expected_artifact"]
                        ),
                    },
                    "input": lane_input,
                    "dependency_outputs": dep_outputs,
                    "result_contract": {
                        "schema_version": 1,
                        "statuses": list(_RESULT_STATUSES),
                    },
                    "execution_supported": False,
                }
                size = len(canonical_json_bytes(probe))
                if size > MAX_INLINE_JSON_BYTES:
                    raise CompositionLaneProtocolError(
                        f"composition lane claim exceeds inline budget ({size})",
                        code="E_TEAM_COMPOSITION_LANE_CLAIM",
                        details={"error": "inline_budget_exceeded", "size": size},
                    )

            expected_version = int(task["version"])
            code, envelope = execute_team_api(
                "claim-task",
                {
                    "run_id": lane_binding["run_id"],
                    "team_id": lane_binding["team_id"],
                    "task_id": lane_binding["task_id"],
                    "worker": worker_id,
                    "expected_version": expected_version,
                },
                root=leader_root,
                env=worker_env,
            )

            # Intentional API blocked_dependency path: must not leave a claim.
            if dep_state == "api_blocked":
                if code == 0:
                    data = envelope.get("data") or {}
                    token = data.get("claimToken") if isinstance(data, Mapping) else None
                    if isinstance(token, str) and token.strip():
                        _release_orphaned_claim(
                            root=leader_root,
                            run_id=lane_binding["run_id"],
                            team_id=lane_binding["team_id"],
                            task_id=lane_binding["task_id"],
                            worker_id=worker_id,
                            claim_token=token,
                            env=worker_env,
                        )
                    raise CompositionLaneProtocolError(
                        "dependencies not ready after claim",
                        code="E_TEAM_COMPOSITION_LANE_DEPS",
                        details={"error": "protocol_dependency_gap"},
                    )
                raise _api_fail("claim-task", code, envelope)

            if code != 0:
                raise _api_fail("claim-task", code, envelope)
            data = envelope.get("data") or {}
            if not isinstance(data, Mapping) or data.get("ok") is not True:
                raise _api_fail("claim-task", code, envelope)
            claimed_task = data.get("task")
            token = data.get("claimToken")
            if not isinstance(claimed_task, Mapping) or not isinstance(token, str):
                raise CompositionLaneProtocolError(
                    "claim-task returned malformed claim",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            claim_body = claimed_task.get("claim") or {}
            if not isinstance(claim_body, Mapping):
                raise CompositionLaneProtocolError(
                    "claim-task returned task without claim",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            leased_until = _require_utc_z(
                claim_body.get("leased_until"), label="leased_until"
            )
            if claim_body.get("token") != token:
                raise CompositionLaneProtocolError(
                    "claim token mismatch between task and claimToken",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            if claimed_task.get("owner") != worker_id:
                raise CompositionLaneProtocolError(
                    "claim owner mismatch",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            try:
                assert_task_matches_lane_binding(
                    claimed_task,
                    lane_id=lane_binding["lane_id"],
                    expected_batch=lane_binding["expected_batch"],
                    expected_artifact=lane_binding["expected_artifact"],
                )
            except CompositionTaskDriverError as exc:
                _release_orphaned_claim(
                    root=leader_root,
                    run_id=lane_binding["run_id"],
                    team_id=lane_binding["team_id"],
                    task_id=lane_binding["task_id"],
                    worker_id=worker_id,
                    claim_token=token,
                    env=worker_env,
                )
                raise _wrap_driver(exc) from exc

            # Defense in depth: re-check protocol deps after claim; never stick.
            dep_outputs, dep_state_after = _load_dependency_outputs(
                leader_root,
                run_id=lane_binding["run_id"],
                team_id=lane_binding["team_id"],
                lane=lane_binding["lane"],
                binding=lane_binding,
            )
            if dep_state_after != "ready":
                _release_orphaned_claim(
                    root=leader_root,
                    run_id=lane_binding["run_id"],
                    team_id=lane_binding["team_id"],
                    task_id=lane_binding["task_id"],
                    worker_id=worker_id,
                    claim_token=token,
                    env=worker_env,
                )
                raise CompositionLaneProtocolError(
                    "dependencies not ready after claim",
                    code="E_TEAM_COMPOSITION_LANE_DEPS",
                    details={"error": "protocol_dependency_gap", "state": dep_state_after},
                )

            claim = {
                "kind": COMPOSITION_LANE_CLAIM_KIND,
                "schema_version": COMPOSITION_LANE_CLAIM_SCHEMA_VERSION,
                "source_kind": adapter.source_kind,
                "run_id": lane_binding["run_id"],
                "team_id": lane_binding["team_id"],
                "composition_id": lane_binding["manifest"]["composition_id"],
                "composition_digest": lane_binding["manifest"]["digest"],
                "batch_id": lane_binding["compiled"]["batch_id"],
                "batch_digest": lane_binding["compiled"]["digest"],
                "lane_id": lane_binding["lane_id"],
                "task_id": lane_binding["task_id"],
                "worker_id": worker_id,
                "task_version": int(claimed_task["version"]),
                "claim_token": token,
                "leased_until": leased_until,
                "lane": {
                    "role": lane_binding["lane"]["role"],
                    "posture": lane_binding["lane"]["posture"],
                    "scope": _lane_scope_view(lane_binding["lane"]),
                    "requires_code_change": False,
                    "allow_implementation": False,
                    "owned_files": list(lane_binding["lane"].get("owned_files") or []),
                    "expected_artifact": dict(lane_binding["expected_artifact"]),
                },
                "input": lane_input,
                "dependency_outputs": dep_outputs,
                "result_contract": {
                    "schema_version": 1,
                    "statuses": list(_RESULT_STATUSES),
                },
                "execution_supported": False,
            }
            # Validate + budget the authoritative envelope.
            claim = parse_composition_lane_claim_v1(claim)

    return {
        "ok": True,
        "claim": claim,
        "execution_supported": False,
    }


def submit_composition_lane_result_v1(
    root: Path | str,
    run_id: str,
    team_id: str,
    adapter: CompositionTaskAdapter,
    *,
    claim: Mapping[str, Any] | Any,
    result: Mapping[str, Any] | Any,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Worker-only: submit ``LaneTaskResultV1`` via ``transition-task-status``."""
    leader_root, worker_id, worker_env = _require_worker_gate(
        root=root, run_id=run_id, team_id=team_id, env=env
    )
    # Parse completely before any mutation.
    parsed_claim = parse_composition_lane_claim_v1(claim)
    try:
        parsed_result = parse_lane_task_result_v1(result)
    except CompositionTaskDriverError as exc:
        raise _wrap_driver(exc) from exc
    canonical_result = canonical_json_bytes(parsed_result).decode("utf-8")

    if parsed_claim["source_kind"] != adapter.source_kind:
        raise CompositionLaneProtocolError(
            "claim source_kind mismatch for composition command",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        )
    if parsed_claim["run_id"] != require_safe_id(run_id, label="run_id"):
        raise CompositionLaneProtocolError(
            "claim run_id mismatch",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        )
    if parsed_claim["team_id"] != require_safe_id(team_id, label="team_id"):
        raise CompositionLaneProtocolError(
            "claim team_id mismatch",
            code="E_TEAM_COMPOSITION_LANE_CLAIM",
        )
    if parsed_claim["worker_id"] != worker_id:
        raise CompositionLaneProtocolError(
            "claim worker_id must equal worker environment identity",
            code="E_TEAM_COMPOSITION_LANE_GATE",
            details={"error": "identity_mismatch"},
        )

    lock = adapter.composition_lock_path(leader_root, run_id)
    with exclusive_lock(lock):
        try:
            preview = resolve_composition_batch_binding_v1(
                leader_root, run_id, team_id, adapter
            )
        except CompositionTaskDriverError as exc:
            raise _wrap_driver(exc) from exc
        batch_path = batch_record_path(
            leader_root,
            preview["run_id"],
            preview["team_id"],
            str(preview["compiled"]["idempotency_key"]),
        )
        with exclusive_lock(batch_path.with_suffix(".lock")):
            try:
                binding = resolve_composition_batch_binding_v1(
                    leader_root, run_id, team_id, adapter
                )
                lane_binding = resolve_composition_lane_binding_v1(
                    binding, parsed_claim["lane_id"]
                )
            except CompositionTaskDriverError as exc:
                raise _wrap_driver(exc) from exc

            if (
                lane_binding["manifest"]["composition_id"]
                != parsed_claim["composition_id"]
                or lane_binding["manifest"]["digest"]
                != parsed_claim["composition_digest"]
                or lane_binding["compiled"]["batch_id"] != parsed_claim["batch_id"]
                or lane_binding["compiled"]["digest"] != parsed_claim["batch_digest"]
                or lane_binding["task_id"] != parsed_claim["task_id"]
            ):
                raise CompositionLaneProtocolError(
                    "claim composition/batch/task binding mismatch",
                    code="E_TEAM_COMPOSITION_LANE_CLAIM",
                )
            if dict(lane_binding["expected_artifact"]) != dict(
                parsed_claim["lane"]["expected_artifact"]
            ):
                raise CompositionLaneProtocolError(
                    "claim expected_artifact mismatch",
                    code="E_TEAM_COMPOSITION_LANE_CLAIM",
                )

            task = _read_task(
                leader_root,
                lane_binding["run_id"],
                lane_binding["team_id"],
                lane_binding["task_id"],
            )
            try:
                task = assert_task_matches_lane_binding(
                    task,
                    lane_id=lane_binding["lane_id"],
                    expected_batch=lane_binding["expected_batch"],
                    expected_artifact=lane_binding["expected_artifact"],
                )
            except CompositionTaskDriverError as exc:
                raise _wrap_driver(exc) from exc

            # Idempotent same-result resubmit.
            if task.get("status") == "completed" and task.get("claim") is None:
                stored = task.get("result")
                stored_bytes = (
                    stored.encode("utf-8")
                    if isinstance(stored, str)
                    else stored
                    if isinstance(stored, (bytes, bytearray))
                    else None
                )
                same_result = (
                    stored_bytes is not None
                    and stored_bytes == canonical_result.encode("utf-8")
                )
                if (
                    same_result
                    and task.get("owner") == worker_id
                    and dict(task.get("batch") or {})
                    == dict(lane_binding["expected_batch"])
                ):
                    return {
                        "ok": True,
                        "idempotent": True,
                        "task_id": lane_binding["task_id"],
                        "lane_id": lane_binding["lane_id"],
                        "status": "completed",
                        "execution_supported": False,
                    }
                raise CompositionLaneProtocolError(
                    "completed task result/owner/binding conflict",
                    code="E_TEAM_COMPOSITION_LANE_CONFLICT",
                    details={"error": "result_conflict"},
                )

            # Active claim must match the claim document.
            claim_body = task.get("claim") or {}
            if (
                task.get("status") != "in_progress"
                or not isinstance(claim_body, Mapping)
                or claim_body.get("owner") != worker_id
                or task.get("owner") != worker_id
                or claim_body.get("token") != parsed_claim["claim_token"]
                or int(task.get("version") or 0) != int(parsed_claim["task_version"])
            ):
                raise CompositionLaneProtocolError(
                    "active claim does not match CompositionLaneClaimV1",
                    code="E_TEAM_COMPOSITION_LANE_CLAIM",
                    details={"error": "claim_conflict"},
                )

            # Re-validate live dependency completion/outputs — never trust a
            # forged claim envelope's dependency_outputs alone.
            live_deps, live_state = _load_dependency_outputs(
                leader_root,
                run_id=lane_binding["run_id"],
                team_id=lane_binding["team_id"],
                lane=lane_binding["lane"],
                binding=lane_binding,
            )
            if live_state != "ready":
                raise CompositionLaneProtocolError(
                    "live dependency outputs not ready for submit",
                    code="E_TEAM_COMPOSITION_LANE_DEPS",
                    details={"error": "protocol_dependency_gap", "state": live_state},
                )
            # Expected depends_on order must match live outputs (forged empty
            # deps fail even when the claim document omitted them).
            expected_dep_lanes = [
                str(x) for x in (lane_binding["lane"].get("depends_on") or [])
            ]
            live_dep_lanes = [row["lane_id"] for row in live_deps]
            if live_dep_lanes != expected_dep_lanes:
                raise CompositionLaneProtocolError(
                    "live dependency outputs order/coverage mismatch",
                    code="E_TEAM_COMPOSITION_LANE_DEPS",
                    details={
                        "error": "dependency_outputs_mismatch",
                        "expected": expected_dep_lanes,
                        "actual": live_dep_lanes,
                    },
                )

            # Lane-specific payload validation before terminal transition so
            # invalid submissions leave the claim usable for retry.
            _validate_adapter_lane_payload(
                adapter,
                lane=lane_binding["lane"],
                lane_result=parsed_result,
            )

            code, envelope = execute_team_api(
                "transition-task-status",
                {
                    "run_id": lane_binding["run_id"],
                    "team_id": lane_binding["team_id"],
                    "task_id": lane_binding["task_id"],
                    "from": "in_progress",
                    "to": "completed",
                    "claim_token": parsed_claim["claim_token"],
                    "worker": worker_id,
                    "result": canonical_result,
                },
                root=leader_root,
                env=worker_env,
            )
            if code != 0:
                raise _api_fail("transition-task-status", code, envelope)
            data = envelope.get("data") or {}
            if not isinstance(data, Mapping) or data.get("ok") is not True:
                raise _api_fail("transition-task-status", code, envelope)

            # Re-read and verify authoritative post-conditions.
            updated = _read_task(
                leader_root,
                lane_binding["run_id"],
                lane_binding["team_id"],
                lane_binding["task_id"],
            )
            if updated is None:
                raise CompositionLaneProtocolError(
                    "task missing after transition",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            if updated.get("status") != "completed":
                raise CompositionLaneProtocolError(
                    "task status not completed after transition",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            if updated.get("claim") is not None:
                raise CompositionLaneProtocolError(
                    "claim not cleared after transition",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            if updated.get("owner") != worker_id:
                raise CompositionLaneProtocolError(
                    "owner not retained after transition",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            stored = updated.get("result")
            if not isinstance(stored, str) or stored.encode("utf-8") != canonical_result.encode(
                "utf-8"
            ):
                raise CompositionLaneProtocolError(
                    "stored result bytes mismatch",
                    code="E_TEAM_COMPOSITION_LANE_API",
                )
            try:
                assert_task_matches_lane_binding(
                    updated,
                    lane_id=lane_binding["lane_id"],
                    expected_batch=lane_binding["expected_batch"],
                    expected_artifact=lane_binding["expected_artifact"],
                )
            except CompositionTaskDriverError as exc:
                raise _wrap_driver(exc) from exc

    return {
        "ok": True,
        "idempotent": False,
        "task_id": lane_binding["task_id"],
        "lane_id": lane_binding["lane_id"],
        "status": "completed",
        "lane_result_status": parsed_result["status"],
        "execution_supported": False,
    }


__all__ = [
    "COMPOSITION_LANE_CLAIM_KIND",
    "COMPOSITION_LANE_CLAIM_SCHEMA_VERSION",
    "CompositionLaneProtocolError",
    "claim_composition_lane_v1",
    "parse_composition_lane_claim_v1",
    "redact_claim_token",
    "submit_composition_lane_result_v1",
]
