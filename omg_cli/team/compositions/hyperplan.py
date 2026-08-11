"""Hyperplan Composition Contract V1 — hermetic produce + task driver (#69 PR12).

Pure ``compile_hyperplan_v1`` builds a deterministic critic→synthesize→verify
DAG from a bounded spec. ``materialize_hyperplan_v1`` persists only under the
canonical Team run root:

  ``.omg/state/runs/<run>/team/compositions/hyperplan-v1.json``

``compile_hyperplan_decision_v1`` / ``produce_hyperplan_decision_v1`` derive a
decision from a bounded ``HyperplanResultBundleV1`` (offline; never executes
lanes). ``admit_hyperplan_tasks_v1`` / ``collect_hyperplan_tasks_v1`` bridge
the materialized DAG onto the PR11 Team task plane via the shared composition
task driver. ``execution_supported`` is always ``false``. This module never
launches panes, Jobs, providers, Antigravity, MCP tools, or automatic workers,
and never sets ``verified`` / ``passes``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    ContractPathError,
    atomic_write_bytes,
    exclusive_lock,
    read_managed_regular_bytes,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_nonempty_string,
    require_object,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.evidence import CLI_WRITER
from omg_cli.state import _safe_run_id, load_run
from omg_cli.team.plane import team_dir

HYPERPLAN_KIND = "omg.team.hyperplan"
HYPERPLAN_DECISION_KIND = "omg.team.hyperplan_decision"
HYPERPLAN_RESULT_BUNDLE_KIND = "omg.team.hyperplan_result_bundle"
HYPERPLAN_SCHEMA_VERSION = 1
HYPERPLAN_FILENAME = "hyperplan-v1.json"
HYPERPLAN_DECISION_FILENAME = "hyperplan-v1-decision.json"
HYPERPLAN_RESULT_BUNDLE_FILENAME = "hyperplan-v1-result-bundle.json"
HYPERPLAN_LOCK_NAME = "hyperplan-v1.lock"

MIN_CRITIQUE_DIMENSIONS = 3
MAX_CRITIQUE_DIMENSIONS = 8
MAX_GOAL_CHARS = 4000
MAX_DIMENSION_CHARS = 64
MAX_LIMITS_KEYS = 8
MAX_EVIDENCE_ITEMS = 16
MAX_INLINE_JSON_BYTES = 64 * 1024
MAX_REPAIR_ITEMS = 32
MAX_RISK_ITEMS = 32
MAX_LIMITATION_ITEMS = 32
MAX_CONFLICT_ITEMS = 32
MAX_NOTE_CHARS = 2000
MAX_FINDING_ITEMS = 64
MAX_LIST_ITEMS = 32

_REL_PATH_RE = re.compile(r"^(?!\./)(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")
_ABS_HINT_RE = re.compile(r"(^|/)(Users|home|private|var/folders|tmp)/")
_DIMENSION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_LANE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SEVERITIES = frozenset({"info", "low", "medium", "high", "critical"})
_RECEIPT_STATUSES = frozenset({"complete", "rejected", "blocked"})
_FINDING_DISPOSITIONS = frozenset({"accepted", "repair", "risk", "dismissed"})
_SYNTHESIS_VERDICTS = frozenset({"approved", "rejected"})
_VERIFY_VERDICTS = frozenset({"approved", "rejected"})
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "argv",
        "command",
        "commands",
        "credentials",
        "credential",
        "password",
        "token",
        "api_key",
        "url",
        "urls",
        "uri",
        "network",
        "socket",
        "poc",
        "exploit",
        "provider",
        "pane",
        "tmux",
        "job",
        "jobs",
        "mcp",
    }
)

_SPEC_REQUIRED = frozenset({"schema_version", "critique_dimensions"})
_SPEC_OPTIONAL = frozenset({"goal", "plan_artifact", "limits", "evidence"})
_PLAN_ARTIFACT_KEYS = frozenset({"path", "digest"})
_EVIDENCE_KEYS = frozenset({"path", "digest", "label"})
_LIMITS_ALLOWED = frozenset(
    {
        "max_critique_dimensions",
        "max_inline_bytes",
        "notes",
    }
)

_LANE_KEYS = (
    "lane_id",
    "role",
    "posture",
    "dimension",
    "depends_on",
    "requires_code_change",
    "allow_implementation",
    "owned_files",
    "expected_artifact",
)

_DECISION_REQUIRED = frozenset(
    {
        "kind",
        "schema_version",
        "verdict",
        "composition_id",
        "composition_digest",
        "lane_coverage",
        "conflicts",
        "required_repairs",
        "unresolved_risks",
        "limitations",
        "source_artifact_digests",
    }
)
_DECISION_OPTIONAL = frozenset({"notes", "writer"})
_LANE_COVERAGE_KEYS = frozenset({"lane_id", "status", "artifact_digest"})
_LANE_COVERAGE_STATUSES = frozenset({"complete", "rejected", "blocked"})


class HyperplanError(ValueError):
    """Fail-closed Hyperplan contract error."""

    def __init__(self, message: str, *, code: str = "E_TEAM_HYPERPLAN") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def hyperplan_compositions_dir(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / "compositions"


def hyperplan_manifest_path(root: Path | str, run_id: str) -> Path:
    return hyperplan_compositions_dir(root, run_id) / HYPERPLAN_FILENAME


def hyperplan_decision_path(root: Path | str, run_id: str) -> Path:
    return hyperplan_compositions_dir(root, run_id) / HYPERPLAN_DECISION_FILENAME


def hyperplan_result_bundle_path(root: Path | str, run_id: str) -> Path:
    return hyperplan_compositions_dir(root, run_id) / HYPERPLAN_RESULT_BUNDLE_FILENAME


def hyperplan_lock_path(root: Path | str, run_id: str) -> Path:
    return hyperplan_compositions_dir(root, run_id) / HYPERPLAN_LOCK_NAME


def parse_hyperplan_spec_v1(raw: Any) -> dict[str, Any]:
    """Validate and normalize a HyperplanSpecV1 object (unknown fields refused)."""
    try:
        body = require_object(raw, label="hyperplan.spec")
        require_exact_keys(
            body,
            required=_SPEC_REQUIRED,
            optional=_SPEC_OPTIONAL,
            label="hyperplan.spec",
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_SPEC") from exc

    schema = body.get("schema_version")
    if schema != HYPERPLAN_SCHEMA_VERSION or isinstance(schema, bool):
        raise HyperplanError(
            "schema_version must be 1",
            code="E_TEAM_HYPERPLAN_SPEC",
        )

    has_goal = "goal" in body
    has_plan = "plan_artifact" in body
    if has_goal == has_plan:
        raise HyperplanError(
            "spec requires exactly one of goal or plan_artifact",
            code="E_TEAM_HYPERPLAN_SPEC",
        )

    out: dict[str, Any] = {"schema_version": HYPERPLAN_SCHEMA_VERSION}

    if has_goal:
        goal = body.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise HyperplanError(
                "goal must be a non-empty string",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        if len(goal) > MAX_GOAL_CHARS:
            raise HyperplanError(
                f"goal exceeds {MAX_GOAL_CHARS} characters",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        if "\x00" in goal:
            raise HyperplanError("goal contains NUL", code="E_TEAM_HYPERPLAN_SPEC")
        out["goal"] = goal.strip()
    else:
        out["plan_artifact"] = _parse_plan_artifact(body.get("plan_artifact"))

    dims = _parse_dimensions(body.get("critique_dimensions"))
    out["critique_dimensions"] = dims

    limits = body.get("limits")
    if limits is not None:
        out["limits"] = _parse_limits(limits, dimension_count=len(dims))
    else:
        out["limits"] = {}

    evidence = body.get("evidence")
    if evidence is not None:
        out["evidence"] = _parse_evidence_list(evidence)
    else:
        out["evidence"] = []

    _assert_inline_budget(out)
    return out


def compile_hyperplan_v1(spec: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Pure compiler: HyperplanSpecV1 → HyperplanManifestV1.

    Builds N critic lanes (one per distinct dimension), one planner synthesis
    lane depending on every critic, and one verifier gate depending on the
    synthesis lane plus every critic. Always stamps ``execution_supported=false``.
    """
    normalized = parse_hyperplan_spec_v1(spec)
    dimensions: list[str] = list(normalized["critique_dimensions"])
    critics: list[dict[str, Any]] = []
    for dim in dimensions:
        lane_id = f"critic.{dim}"
        critics.append(
            _lane(
                lane_id=lane_id,
                role="critic",
                dimension=dim,
                depends_on=[],
                expected_artifact=_critic_artifact_schema(dim),
            )
        )
    critic_ids = [row["lane_id"] for row in critics]
    synthesize = _lane(
        lane_id="synthesize",
        role="planner",
        dimension=None,
        depends_on=list(critic_ids),
        expected_artifact=_synthesize_artifact_schema(),
    )
    verify = _lane(
        lane_id="verify",
        role="verifier",
        dimension=None,
        depends_on=["synthesize", *critic_ids],
        expected_artifact=_verify_artifact_schema(),
    )
    lanes = [*critics, synthesize, verify]
    dependency_graph = {row["lane_id"]: list(row["depends_on"]) for row in lanes}
    composition_id = _composition_id_for_spec(normalized)
    result_contract = {
        "schema_version": 1,
        "requires_lane_coverage": True,
        "approval_requires_empty_repairs": True,
        "approval_requires_empty_conflicts": True,
        "approval_requires_empty_unresolved_risks": True,
        "silent_approve_forbidden": True,
        "execution_supported": False,
    }
    manifest_core: dict[str, Any] = {
        "kind": HYPERPLAN_KIND,
        "schema_version": HYPERPLAN_SCHEMA_VERSION,
        "composition_id": composition_id,
        "writer": CLI_WRITER,
        "execution_supported": False,
        "spec": normalized,
        "lanes": lanes,
        "dependency_graph": dependency_graph,
        "result_contract": result_contract,
        "lane_count": len(lanes),
        "critic_count": len(critics),
    }
    digest = sha256_hex(canonical_json_bytes(manifest_core))
    manifest = dict(manifest_core)
    manifest["digest"] = digest
    return manifest


def materialize_hyperplan_v1(
    root: Path | str,
    run_id: str,
    spec: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Atomically persist a compiled Hyperplan manifest under the Team run root.

    Idempotent when the on-disk digest matches. Same ``composition_id`` with a
    different digest is refused. Corrupt / symlink / foreign-writer artifacts
    fail closed. Never launches execution surfaces.
    """
    root_path = Path(root)
    rid = _safe_run_id(run_id)
    _require_live_run(root_path, rid)
    manifest = compile_hyperplan_v1(spec)
    compositions = hyperplan_compositions_dir(root_path, rid)
    path = hyperplan_manifest_path(root_path, rid)
    lock = hyperplan_lock_path(root_path, rid)

    compositions.mkdir(parents=True, exist_ok=True)
    if compositions.is_symlink():
        raise HyperplanError(
            "compositions directory may not be a symlink",
            code="E_TEAM_HYPERPLAN_PATH",
        )

    with exclusive_lock(lock):
        existing = _try_load_existing_manifest(path)
        if existing is not None:
            if (
                existing.get("composition_id") == manifest["composition_id"]
                and existing.get("digest") == manifest["digest"]
            ):
                return {
                    "ok": True,
                    "idempotent": True,
                    "path": _rel_under_root(root_path, path),
                    "manifest": existing,
                }
            if existing.get("composition_id") == manifest["composition_id"]:
                raise HyperplanError(
                    "composition_id digest conflict: refusing overwrite",
                    code="E_TEAM_HYPERPLAN_DIGEST_CONFLICT",
                )
            raise HyperplanError(
                "hyperplan-v1.json already materialized with a different composition",
                code="E_TEAM_HYPERPLAN_CONFLICT",
            )

        # run_id is persistence binding only — digest stays compile-stable so
        # repeated materialize of the same spec remains idempotent.
        payload = dict(manifest)
        payload["run_id"] = rid
        body = canonical_json_bytes(payload)
        try:
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
        except FileExistsError as exc:
            # Lost the race after the existence check — re-enter fail-closed.
            raise HyperplanError(
                "concurrent materialize race refused",
                code="E_TEAM_HYPERPLAN_RACE",
            ) from exc
        except ContractPathError as exc:
            raise HyperplanError(
                f"materialize path refused: {exc}",
                code="E_TEAM_HYPERPLAN_PATH",
            ) from exc

        # Re-read through managed path to prove publication.
        loaded = load_hyperplan_manifest(root_path, rid)
        if loaded.get("digest") != manifest["digest"]:
            raise HyperplanError(
                "published digest mismatch",
                code="E_TEAM_HYPERPLAN_CORRUPT",
            )
        return {
            "ok": True,
            "idempotent": False,
            "path": _rel_under_root(root_path, path),
            "manifest": loaded,
        }


def load_hyperplan_manifest(root: Path | str, run_id: str) -> dict[str, Any]:
    """Load and validate a persisted HyperplanManifestV1 (fail-closed)."""
    root_path = Path(root)
    rid = _safe_run_id(run_id)
    path = hyperplan_manifest_path(root_path, rid)
    return _load_manifest_file(path, expected_run_id=rid)


def validate_hyperplan_decision_v1(
    root: Path | str,
    run_id: str,
    decision: Mapping[str, Any] | Any,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Validate a HyperplanDecisionV1 against the materialized manifest.

    Never invents or silently approves a decision. ``verdict=approved`` requires
    complete lane coverage plus empty conflicts/repairs/unresolved risks.
    """
    root_path = Path(root)
    rid = _safe_run_id(run_id)
    _require_live_run(root_path, rid)
    manifest = load_hyperplan_manifest(root_path, rid)
    normalized = _parse_decision(decision, manifest=manifest)

    if not persist:
        return {"ok": True, "persisted": False, "decision": normalized}

    compositions = hyperplan_compositions_dir(root_path, rid)
    compositions.mkdir(parents=True, exist_ok=True)
    if compositions.is_symlink():
        raise HyperplanError(
            "compositions directory may not be a symlink",
            code="E_TEAM_HYPERPLAN_PATH",
        )
    path = hyperplan_decision_path(root_path, rid)
    bundle_path = hyperplan_result_bundle_path(root_path, rid)
    lock = hyperplan_lock_path(root_path, rid)
    with exclusive_lock(lock):
        # Produce owns the decision commit marker whenever a result-bundle is
        # present. validate-decision must not overwrite (or mint) that marker.
        if bundle_path.is_symlink():
            raise HyperplanError(
                "hyperplan-v1-result-bundle.json may not be a symlink",
                code="E_TEAM_HYPERPLAN_PATH",
            )
        if bundle_path.exists():
            existing_decision = _try_load_existing_decision(path)
            if existing_decision is not None:
                existing_norm = _parse_decision(existing_decision, manifest=manifest)
                if existing_norm == normalized:
                    return {
                        "ok": True,
                        "persisted": True,
                        "idempotent": True,
                        "path": _rel_under_root(root_path, path),
                        "decision": existing_norm,
                    }
            raise HyperplanError(
                "result-bundle present: refuse validate-decision persist "
                "(decision commit marker is owned by produce-decision)",
                code="E_TEAM_HYPERPLAN_CONFLICT",
            )

        body = canonical_json_bytes(normalized)
        try:
            # Hand-authored validate path (no produce bundle): replace allowed.
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=True)
        except ContractPathError as exc:
            raise HyperplanError(
                f"decision path refused: {exc}",
                code="E_TEAM_HYPERPLAN_PATH",
            ) from exc
    return {
        "ok": True,
        "persisted": True,
        "idempotent": False,
        "path": _rel_under_root(root_path, path),
        "decision": normalized,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_dimensions(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise HyperplanError(
            "critique_dimensions must be a non-empty list",
            code="E_TEAM_HYPERPLAN_SPEC",
        )
    if len(raw) < MIN_CRITIQUE_DIMENSIONS or len(raw) > MAX_CRITIQUE_DIMENSIONS:
        raise HyperplanError(
            f"critique_dimensions must contain "
            f"{MIN_CRITIQUE_DIMENSIONS}–{MAX_CRITIQUE_DIMENSIONS} items",
            code="E_TEAM_HYPERPLAN_SPEC",
        )
    dims: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        label = f"critique_dimensions[{idx}]"
        if not isinstance(item, str) or not item.strip():
            raise HyperplanError(
                f"{label} must be a non-empty string",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        text = item.strip()
        if len(text) > MAX_DIMENSION_CHARS:
            raise HyperplanError(
                f"{label} exceeds {MAX_DIMENSION_CHARS} characters",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        try:
            safe = require_safe_id(text, label=label)
        except ContractValidationError as exc:
            raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_SPEC") from exc
        if not _DIMENSION_RE.fullmatch(safe):
            raise HyperplanError(
                f"{label} must match {_DIMENSION_RE.pattern}",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        if safe in seen:
            raise HyperplanError(
                f"duplicate critique dimension {safe!r}",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        seen.add(safe)
        dims.append(safe)
    # Stable order for deterministic digests / DAG.
    return sorted(dims)


def _parse_plan_artifact(raw: Any) -> dict[str, Any]:
    try:
        obj = require_object(raw, label="plan_artifact")
        require_exact_keys(
            obj,
            required=_PLAN_ARTIFACT_KEYS,
            optional=frozenset(),
            label="plan_artifact",
        )
        path = require_nonempty_string(obj.get("path"), label="plan_artifact.path")
        digest = require_sha256(obj.get("digest"), label="plan_artifact.digest")
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_SPEC") from exc
    _assert_relative_safe(path, label="plan_artifact.path")
    return {"path": path.replace("\\", "/"), "digest": digest}


def _parse_limits(raw: Any, *, dimension_count: int) -> dict[str, Any]:
    try:
        obj = require_object(raw, label="limits")
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_SPEC") from exc
    unknown = set(obj) - _LIMITS_ALLOWED
    if unknown:
        raise HyperplanError(
            f"limits has unknown fields: {sorted(unknown)!r}",
            code="E_TEAM_HYPERPLAN_SPEC",
        )
    if len(obj) > MAX_LIMITS_KEYS:
        raise HyperplanError(
            "limits has too many fields",
            code="E_TEAM_HYPERPLAN_SPEC",
        )
    out: dict[str, Any] = {}
    if "max_critique_dimensions" in obj:
        value = obj["max_critique_dimensions"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise HyperplanError(
                "limits.max_critique_dimensions must be an int",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        if value < dimension_count or value > MAX_CRITIQUE_DIMENSIONS:
            raise HyperplanError(
                "limits.max_critique_dimensions out of range",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        out["max_critique_dimensions"] = value
    if "max_inline_bytes" in obj:
        value = obj["max_inline_bytes"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise HyperplanError(
                "limits.max_inline_bytes must be an int",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        if value < 256 or value > MAX_INLINE_JSON_BYTES:
            raise HyperplanError(
                "limits.max_inline_bytes out of range",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        out["max_inline_bytes"] = value
    if "notes" in obj:
        notes = obj["notes"]
        if not isinstance(notes, str):
            raise HyperplanError(
                "limits.notes must be a string",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        if len(notes) > MAX_NOTE_CHARS:
            raise HyperplanError(
                "limits.notes too long",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        out["notes"] = notes
    return out


def _parse_evidence_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise HyperplanError(
            "evidence must be a list",
            code="E_TEAM_HYPERPLAN_SPEC",
        )
    if len(raw) > MAX_EVIDENCE_ITEMS:
        raise HyperplanError(
            f"evidence exceeds {MAX_EVIDENCE_ITEMS} items",
            code="E_TEAM_HYPERPLAN_SPEC",
        )
    out: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for idx, item in enumerate(raw):
        label = f"evidence[{idx}]"
        try:
            obj = require_object(item, label=label)
            require_exact_keys(
                obj,
                required=frozenset({"path", "digest"}),
                optional=frozenset({"label"}),
                label=label,
            )
            path = require_nonempty_string(obj.get("path"), label=f"{label}.path")
            digest = require_sha256(obj.get("digest"), label=f"{label}.digest")
        except ContractValidationError as exc:
            raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_SPEC") from exc
        _assert_relative_safe(path, label=f"{label}.path")
        norm = path.replace("\\", "/")
        if norm in seen_paths:
            raise HyperplanError(
                f"duplicate evidence path {norm!r}",
                code="E_TEAM_HYPERPLAN_SPEC",
            )
        seen_paths.add(norm)
        row: dict[str, Any] = {"path": norm, "digest": digest}
        if "label" in obj:
            try:
                row["label"] = require_safe_id(obj.get("label"), label=f"{label}.label")
            except ContractValidationError as exc:
                raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_SPEC") from exc
        out.append(row)
    out.sort(key=lambda row: row["path"])
    return out


def _assert_inline_budget(spec: Mapping[str, Any]) -> None:
    budget = int(spec.get("limits", {}).get("max_inline_bytes") or MAX_INLINE_JSON_BYTES)
    size = len(canonical_json_bytes(dict(spec)))
    if size > budget:
        raise HyperplanError(
            f"spec exceeds inline budget ({size} > {budget})",
            code="E_TEAM_HYPERPLAN_SPEC",
        )


def _assert_relative_safe(path: str, *, label: str) -> None:
    text = path.replace("\\", "/")
    if text.startswith("/") or text.startswith("~") or "://" in text:
        raise HyperplanError(f"{label} must be a relative safe path", code="E_TEAM_HYPERPLAN_SPEC")
    if not _REL_PATH_RE.fullmatch(text):
        raise HyperplanError(f"{label} is not a safe relative path", code="E_TEAM_HYPERPLAN_SPEC")
    if _ABS_HINT_RE.search(text):
        raise HyperplanError(f"{label} looks absolute/unsafe", code="E_TEAM_HYPERPLAN_SPEC")


def _lane(
    *,
    lane_id: str,
    role: str,
    dimension: str | None,
    depends_on: Sequence[str],
    expected_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "lane_id": lane_id,
        "role": role,
        "posture": "read-only",
        "dimension": dimension,
        "depends_on": list(depends_on),
        "requires_code_change": False,
        "allow_implementation": False,
        "owned_files": [],
        "expected_artifact": dict(expected_artifact),
    }
    # Keep key order stable for goldens / digests.
    return {key: row[key] for key in _LANE_KEYS}


def _critic_artifact_schema(dimension: str) -> dict[str, Any]:
    return {
        "kind": "omg.team.hyperplan.critique",
        "schema_version": 1,
        "dimension": dimension,
        "required_fields": [
            "dimension",
            "findings",
            "severity",
            "blocking",
        ],
    }


def _synthesize_artifact_schema() -> dict[str, Any]:
    return {
        "kind": "omg.team.hyperplan.synthesis",
        "schema_version": 1,
        "required_fields": [
            "summary",
            "merged_findings",
            "open_conflicts",
            "recommended_verdict",
        ],
    }


def _verify_artifact_schema() -> dict[str, Any]:
    return {
        "kind": "omg.team.hyperplan.verification",
        "schema_version": 1,
        "required_fields": [
            "gate",
            "covered_lanes",
            "blocking_issues",
            "verdict",
        ],
    }


def _composition_id_for_spec(spec: Mapping[str, Any]) -> str:
    seed = {
        "schema_version": HYPERPLAN_SCHEMA_VERSION,
        "critique_dimensions": list(spec["critique_dimensions"]),
        "goal": spec.get("goal"),
        "plan_artifact": spec.get("plan_artifact"),
        "limits": dict(spec.get("limits") or {}),
        "evidence": list(spec.get("evidence") or []),
    }
    digest = sha256_hex(canonical_json_bytes(seed))
    return f"hp1_{digest[:16]}"


def _require_live_run(root: Path, run_id: str) -> dict[str, Any]:
    status = load_run(root, run_id)
    if status is None:
        raise HyperplanError(
            f"run {run_id!r} missing or unreadable",
            code="E_TEAM_HYPERPLAN_STALE_RUN",
        )
    if status.get("run_id") != run_id:
        raise HyperplanError(
            "status.json run_id mismatch",
            code="E_TEAM_HYPERPLAN_STALE_RUN",
        )
    # Terminal cancelled runs are stale for new composition materialize.
    if status.get("status") == "cancelled":
        raise HyperplanError(
            f"run {run_id!r} is cancelled",
            code="E_TEAM_HYPERPLAN_STALE_RUN",
        )
    return status


def _try_load_existing_manifest(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise HyperplanError(
            "hyperplan-v1.json may not be a symlink",
            code="E_TEAM_HYPERPLAN_PATH",
        )
    if not path.exists():
        return None
    return _load_manifest_file(path, expected_run_id=None)


def _load_manifest_file(
    path: Path, *, expected_run_id: str | None
) -> dict[str, Any]:
    if path.is_symlink():
        raise HyperplanError(
            "hyperplan-v1.json may not be a symlink",
            code="E_TEAM_HYPERPLAN_PATH",
        )
    try:
        body = read_managed_regular_bytes(path, max_bytes=MAX_INLINE_JSON_BYTES)
    except FileNotFoundError as exc:
        raise HyperplanError(
            "hyperplan-v1.json missing",
            code="E_TEAM_HYPERPLAN_MISSING",
        ) from exc
    except ContractPathError as exc:
        raise HyperplanError(
            f"hyperplan-v1.json unreadable: {exc}",
            code="E_TEAM_HYPERPLAN_PATH",
        ) from exc
    if not body:
        raise HyperplanError(
            "hyperplan-v1.json empty/corrupt",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HyperplanError(
            "hyperplan-v1.json corrupt JSON",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        ) from exc
    if not isinstance(parsed, dict):
        raise HyperplanError(
            "hyperplan-v1.json must be an object",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    return _validate_persisted_manifest(parsed, expected_run_id=expected_run_id)


def _validate_persisted_manifest(
    raw: Mapping[str, Any], *, expected_run_id: str | None
) -> dict[str, Any]:
    required = {
        "kind",
        "schema_version",
        "composition_id",
        "digest",
        "writer",
        "execution_supported",
        "spec",
        "lanes",
        "dependency_graph",
        "result_contract",
        "lane_count",
        "critic_count",
    }
    optional = {"run_id"}
    try:
        require_exact_keys(
            raw,
            required=required,
            optional=optional,
            label="hyperplan.manifest",
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_CORRUPT") from exc

    if raw.get("kind") != HYPERPLAN_KIND:
        raise HyperplanError("manifest kind mismatch", code="E_TEAM_HYPERPLAN_CORRUPT")
    if raw.get("schema_version") != HYPERPLAN_SCHEMA_VERSION:
        raise HyperplanError(
            "manifest schema_version mismatch",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    if raw.get("writer") != CLI_WRITER:
        raise HyperplanError(
            "foreign writer refused",
            code="E_TEAM_HYPERPLAN_FOREIGN_WRITER",
        )
    if raw.get("execution_supported") is not False:
        raise HyperplanError(
            "execution_supported must be false",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    try:
        require_safe_id(raw.get("composition_id"), label="composition_id")
        require_sha256(raw.get("digest"), label="digest")
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_CORRUPT") from exc

    run_id = raw.get("run_id")
    if expected_run_id is not None:
        if run_id != expected_run_id:
            raise HyperplanError(
                "manifest run_id mismatch",
                code="E_TEAM_HYPERPLAN_CORRUPT",
            )
    elif run_id is not None:
        try:
            _safe_run_id(str(run_id))
        except ValueError as exc:
            raise HyperplanError(
                "manifest run_id invalid",
                code="E_TEAM_HYPERPLAN_CORRUPT",
            ) from exc

    # Digest is compile-stable: exclude persistence-only run_id + digest.
    core = {
        k: v for k, v in raw.items() if k not in {"digest", "run_id"}
    }
    expected = sha256_hex(canonical_json_bytes(core))
    if expected != raw["digest"]:
        raise HyperplanError(
            "manifest digest mismatch (truncated or partially written)",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )

    # Spec + lanes must still parse/compile-compatible.
    spec = parse_hyperplan_spec_v1(raw.get("spec"))
    if not isinstance(raw.get("lanes"), list) or not raw["lanes"]:
        raise HyperplanError("manifest lanes corrupt", code="E_TEAM_HYPERPLAN_CORRUPT")
    expected_count = len(spec["critique_dimensions"]) + 2
    if int(raw.get("lane_count") or -1) != expected_count:
        raise HyperplanError("lane_count mismatch", code="E_TEAM_HYPERPLAN_CORRUPT")
    if int(raw.get("critic_count") or -1) != len(spec["critique_dimensions"]):
        raise HyperplanError("critic_count mismatch", code="E_TEAM_HYPERPLAN_CORRUPT")
    # Recompile from normalized spec and compare the entire canonical derived
    # core — closes forged lane/dependency drift that re-stamps digest.
    recompiled = compile_hyperplan_v1(spec)
    expected_core = {k: v for k, v in recompiled.items() if k != "digest"}
    actual_core = {k: v for k, v in raw.items() if k not in {"digest", "run_id"}}
    if actual_core != expected_core:
        raise HyperplanError(
            "manifest derived core drift (forged lanes/dependencies refused)",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    if recompiled["digest"] != raw["digest"]:
        raise HyperplanError(
            "manifest digest mismatch against recompiled core",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    return dict(raw)


def _parse_decision(
    raw: Any, *, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        body = require_object(raw, label="hyperplan.decision")
        require_exact_keys(
            body,
            required=_DECISION_REQUIRED,
            optional=_DECISION_OPTIONAL,
            label="hyperplan.decision",
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_DECISION") from exc

    if body.get("kind") != HYPERPLAN_DECISION_KIND:
        raise HyperplanError(
            "decision kind mismatch",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    if body.get("schema_version") != HYPERPLAN_SCHEMA_VERSION:
        raise HyperplanError(
            "decision schema_version must be 1",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    verdict = body.get("verdict")
    if verdict not in ("approved", "rejected"):
        raise HyperplanError(
            "verdict must be approved|rejected",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    try:
        composition_id = require_safe_id(
            body.get("composition_id"), label="composition_id"
        )
        composition_digest = require_sha256(
            body.get("composition_digest"), label="composition_digest"
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_DECISION") from exc

    if composition_id != manifest.get("composition_id"):
        raise HyperplanError(
            "decision composition_id does not match manifest",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    if composition_digest != manifest.get("digest"):
        raise HyperplanError(
            "decision composition_digest does not match manifest",
            code="E_TEAM_HYPERPLAN_DECISION",
        )

    writer = body.get("writer", CLI_WRITER)
    if writer != CLI_WRITER:
        raise HyperplanError(
            "foreign writer refused on decision",
            code="E_TEAM_HYPERPLAN_FOREIGN_WRITER",
        )

    required_lanes = [row["lane_id"] for row in manifest["lanes"]]
    coverage = _parse_lane_coverage(
        body.get("lane_coverage"), required_lanes=required_lanes
    )
    conflicts = _parse_string_list(
        body.get("conflicts"), label="conflicts", max_items=MAX_CONFLICT_ITEMS
    )
    repairs = _parse_string_list(
        body.get("required_repairs"),
        label="required_repairs",
        max_items=MAX_REPAIR_ITEMS,
    )
    risks = _parse_string_list(
        body.get("unresolved_risks"),
        label="unresolved_risks",
        max_items=MAX_RISK_ITEMS,
    )
    limitations = _parse_string_list(
        body.get("limitations"),
        label="limitations",
        max_items=MAX_LIMITATION_ITEMS,
    )
    sources = _parse_source_digests(body.get("source_artifact_digests"), manifest=manifest)

    if verdict == "approved":
        # Never silent-approve incomplete / conflicted decisions.
        incomplete = [
            row["lane_id"] for row in coverage if row["status"] != "complete"
        ]
        if incomplete:
            raise HyperplanError(
                f"approved decision has incomplete lanes: {incomplete!r}",
                code="E_TEAM_HYPERPLAN_DECISION",
            )
        if conflicts or repairs or risks:
            raise HyperplanError(
                "approved decision requires empty conflicts, "
                "required_repairs, and unresolved_risks",
                code="E_TEAM_HYPERPLAN_DECISION",
            )

    out: dict[str, Any] = {
        "kind": HYPERPLAN_DECISION_KIND,
        "schema_version": HYPERPLAN_SCHEMA_VERSION,
        "verdict": verdict,
        "composition_id": composition_id,
        "composition_digest": composition_digest,
        "lane_coverage": coverage,
        "conflicts": conflicts,
        "required_repairs": repairs,
        "unresolved_risks": risks,
        "limitations": limitations,
        "source_artifact_digests": sources,
        "writer": CLI_WRITER,
    }
    if "notes" in body:
        notes = body.get("notes")
        if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
            raise HyperplanError(
                "notes must be a short string",
                code="E_TEAM_HYPERPLAN_DECISION",
            )
        out["notes"] = notes
    return out


def _parse_lane_coverage(
    raw: Any, *, required_lanes: Sequence[str]
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise HyperplanError(
            "lane_coverage must be a list",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    by_id: dict[str, dict[str, Any]] = {}
    for idx, item in enumerate(raw):
        label = f"lane_coverage[{idx}]"
        try:
            obj = require_object(item, label=label)
            require_exact_keys(
                obj,
                required=_LANE_COVERAGE_KEYS,
                optional=frozenset(),
                label=label,
            )
            lane_id = require_nonempty_string(obj.get("lane_id"), label=f"{label}.lane_id")
            if not _LANE_ID_RE.fullmatch(lane_id):
                raise HyperplanError(
                    f"{label}.lane_id is not a valid lane id",
                    code="E_TEAM_HYPERPLAN_DECISION",
                )
            status = require_nonempty_string(obj.get("status"), label=f"{label}.status")
            digest = require_sha256(
                obj.get("artifact_digest"), label=f"{label}.artifact_digest"
            )
        except ContractValidationError as exc:
            raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_DECISION") from exc
        # Reject unknown statuses.
        if status not in _LANE_COVERAGE_STATUSES:
            raise HyperplanError(
                f"{label}.status must be one of {sorted(_LANE_COVERAGE_STATUSES)}",
                code="E_TEAM_HYPERPLAN_DECISION",
            )
        if lane_id in by_id:
            raise HyperplanError(
                f"duplicate lane_coverage for {lane_id!r}",
                code="E_TEAM_HYPERPLAN_DECISION",
            )
        by_id[lane_id] = {
            "lane_id": lane_id,
            "status": status,
            "artifact_digest": digest,
        }

    missing = [lane for lane in required_lanes if lane not in by_id]
    if missing:
        raise HyperplanError(
            f"decision omits required lanes: {missing!r}",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    extra = sorted(set(by_id) - set(required_lanes))
    if extra:
        raise HyperplanError(
            f"decision references unknown lanes: {extra!r}",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    return [by_id[lane] for lane in required_lanes]


def _parse_string_list(raw: Any, *, label: str, max_items: int) -> list[str]:
    if not isinstance(raw, list):
        raise HyperplanError(
            f"{label} must be a list",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    if len(raw) > max_items:
        raise HyperplanError(
            f"{label} exceeds {max_items} items",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    out: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise HyperplanError(
                f"{label}[{idx}] must be a non-empty string",
                code="E_TEAM_HYPERPLAN_DECISION",
            )
        if len(item) > MAX_NOTE_CHARS:
            raise HyperplanError(
                f"{label}[{idx}] too long",
                code="E_TEAM_HYPERPLAN_DECISION",
            )
        out.append(item.strip())
    return out


def _parse_source_digests(
    raw: Any, *, manifest: Mapping[str, Any]
) -> dict[str, str]:
    try:
        obj = require_object(raw, label="source_artifact_digests")
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_DECISION") from exc
    # Must bind the composition digest at minimum; evidence paths optional.
    required_keys = {"composition"}
    missing = required_keys - set(obj)
    if missing:
        raise HyperplanError(
            f"source_artifact_digests missing {sorted(missing)!r}",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    out: dict[str, str] = {}
    for key, value in obj.items():
        try:
            safe_key = require_safe_id(key, label="source_artifact_digests.key")
            digest = require_sha256(
                value, label=f"source_artifact_digests[{safe_key}]"
            )
        except ContractValidationError as exc:
            raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_DECISION") from exc
        out[safe_key] = digest
    if out["composition"] != manifest.get("digest"):
        raise HyperplanError(
            "source_artifact_digests.composition must match manifest digest",
            code="E_TEAM_HYPERPLAN_DECISION",
        )
    # Bind optional evidence descriptors from the spec when present.
    for item in manifest.get("spec", {}).get("evidence") or []:
        path = str(item.get("path") or "")
        # evidence digests keyed by safe label when provided, else skipped
        label = item.get("label")
        if isinstance(label, str) and label in out:
            if out[label] != item.get("digest"):
                raise HyperplanError(
                    f"source digest for evidence label {label!r} mismatch",
                    code="E_TEAM_HYPERPLAN_DECISION",
                )
        _ = path  # path-keyed digests are optional in V1
    return dict(sorted(out.items()))


def _rel_under_root(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Result bundle → decision production (hermetic; execution_supported=false)
# ---------------------------------------------------------------------------

_BUNDLE_REQUIRED = frozenset(
    {
        "kind",
        "schema_version",
        "composition_id",
        "composition_digest",
        "receipts",
    }
)
_BUNDLE_OPTIONAL = frozenset({"notes", "writer"})
_RECEIPT_REQUIRED = frozenset({"lane_id", "status", "artifact_kind", "payload"})
_RECEIPT_OPTIONAL = frozenset({"reason"})
_CRITIC_PAYLOAD_REQUIRED = frozenset(
    {"dimension", "findings", "severity", "blocking"}
)
_FINDING_REQUIRED = frozenset({"finding_id", "summary"})
_FINDING_OPTIONAL = frozenset({"severity", "blocking", "notes"})
_SYNTHESIS_PAYLOAD_REQUIRED = frozenset(
    {
        "summary",
        "merged_findings",
        "open_conflicts",
        "recommended_verdict",
    }
)
_MERGED_FINDING_REQUIRED = frozenset({"finding_id", "disposition"})
_MERGED_FINDING_OPTIONAL = frozenset({"notes"})
_VERIFY_PAYLOAD_REQUIRED = frozenset(
    {"gate", "covered_lanes", "blocking_issues", "verdict"}
)


def compile_hyperplan_decision_v1(
    manifest: Mapping[str, Any] | Any,
    bundle: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Pure: HyperplanResultBundleV1 + ManifestV1 → DecisionV1.

    Derives lane coverage, conflicts, repairs, risks, verdict, and source
    digests. Never launches execution surfaces.
    """
    if not isinstance(manifest, Mapping):
        raise HyperplanError(
            "manifest must be an object",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    normalized_bundle = _normalize_result_bundle(bundle, manifest=manifest)
    decision = _derive_decision_from_bundle(manifest, normalized_bundle)
    return _parse_decision(decision, manifest=manifest)


def produce_hyperplan_decision_v1(
    root: Path | str,
    run_id: str,
    bundle: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Persist result bundle then decision under the composition lock.

    Writes ``hyperplan-v1-result-bundle.json`` first, then atomically writes
    ``hyperplan-v1-decision.json`` last as the commit marker. Identical
    production is idempotent; conflicts / path attacks leave no authoritative
    decision. Never mutates ``status.json`` / ``passes`` / ``verified``.
    """
    root_path = Path(root)
    rid = _safe_run_id(run_id)
    _require_live_run(root_path, rid)
    manifest = load_hyperplan_manifest(root_path, rid)
    compositions = hyperplan_compositions_dir(root_path, rid)
    compositions.mkdir(parents=True, exist_ok=True)
    if compositions.is_symlink():
        raise HyperplanError(
            "compositions directory may not be a symlink",
            code="E_TEAM_HYPERPLAN_PATH",
        )
    lock = hyperplan_lock_path(root_path, rid)
    with exclusive_lock(lock):
        return _persist_hyperplan_decision_locked(
            root_path=root_path,
            rid=rid,
            manifest=manifest,
            bundle=bundle,
        )


def _persist_hyperplan_decision_locked(
    *,
    root_path: Path,
    rid: str,
    manifest: Mapping[str, Any],
    bundle: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Bundle-first / decision-last persistence; caller holds composition lock."""
    normalized_bundle = _normalize_result_bundle(bundle, manifest=manifest)
    decision = compile_hyperplan_decision_v1(manifest, normalized_bundle)
    bundle_path = hyperplan_result_bundle_path(root_path, rid)
    decision_path = hyperplan_decision_path(root_path, rid)
    bundle_body = canonical_json_bytes(normalized_bundle)
    decision_body = canonical_json_bytes(decision)

    existing_bundle = _try_load_existing_result_bundle(bundle_path)
    existing_decision = _try_load_existing_decision(decision_path)
    if existing_bundle is not None or existing_decision is not None:
        same_bundle = (
            existing_bundle is not None
            and existing_bundle.get("digest") == normalized_bundle["digest"]
            and _bundle_core_equal(existing_bundle, normalized_bundle)
        )
        same_decision = (
            existing_decision is not None and existing_decision == decision
        )
        if same_bundle and same_decision:
            return {
                "ok": True,
                "idempotent": True,
                "bundle_path": _rel_under_root(root_path, bundle_path),
                "path": _rel_under_root(root_path, decision_path),
                "bundle": existing_bundle,
                "decision": existing_decision,
            }
        if existing_decision is not None and not same_decision:
            raise HyperplanError(
                "hyperplan-v1-decision.json conflict: refusing overwrite",
                code="E_TEAM_HYPERPLAN_CONFLICT",
            )
        if existing_bundle is not None and not same_bundle:
            raise HyperplanError(
                "hyperplan-v1-result-bundle.json conflict: refusing overwrite",
                code="E_TEAM_HYPERPLAN_CONFLICT",
            )
        raise HyperplanError(
            "incomplete prior result production refused "
            "(bundle/decision mismatch)",
            code="E_TEAM_HYPERPLAN_CONFLICT",
        )

    try:
        atomic_write_bytes(
            bundle_path, bundle_body, mode=DATA_FILE_MODE, replace=False
        )
    except FileExistsError as exc:
        raise HyperplanError(
            "concurrent result-bundle write race refused",
            code="E_TEAM_HYPERPLAN_RACE",
        ) from exc
    except ContractPathError as exc:
        raise HyperplanError(
            f"result-bundle path refused: {exc}",
            code="E_TEAM_HYPERPLAN_PATH",
        ) from exc

    try:
        atomic_write_bytes(
            decision_path, decision_body, mode=DATA_FILE_MODE, replace=False
        )
    except (FileExistsError, ContractPathError) as exc:
        raise HyperplanError(
            f"decision commit marker refused after bundle write: {exc}",
            code="E_TEAM_HYPERPLAN_PATH",
        ) from exc

    loaded_bundle = _load_result_bundle_file(bundle_path)
    if loaded_bundle.get("digest") != normalized_bundle["digest"]:
        raise HyperplanError(
            "published result-bundle digest mismatch",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    loaded_decision = _parse_decision(
        json.loads(decision_path.read_bytes().decode("utf-8")),
        manifest=manifest,
    )
    if loaded_decision != decision:
        raise HyperplanError(
            "published decision mismatch",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    return {
        "ok": True,
        "idempotent": False,
        "bundle_path": _rel_under_root(root_path, bundle_path),
        "path": _rel_under_root(root_path, decision_path),
        "bundle": loaded_bundle,
        "decision": loaded_decision,
    }


def admit_hyperplan_tasks_v1(
    root: Path | str,
    run_id: str,
    team_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Admit a materialized Hyperplan DAG onto the Team task plane (PR12)."""
    from omg_cli.team.compositions.task_driver import (
        CompositionTaskDriverError,
        admit_composition_tasks_v1,
    )

    try:
        return admit_composition_tasks_v1(
            root, run_id, team_id, _HyperplanTaskAdapter(), env=env
        )
    except CompositionTaskDriverError as exc:
        raise HyperplanError(str(exc), code=exc.code) from exc


def collect_hyperplan_tasks_v1(
    root: Path | str,
    run_id: str,
    team_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Collect completed Hyperplan lane tasks into produce-decision (PR12)."""
    from omg_cli.team.compositions.task_driver import (
        CompositionTaskDriverError,
        collect_composition_tasks_v1,
    )

    try:
        return collect_composition_tasks_v1(
            root, run_id, team_id, _HyperplanTaskAdapter(), env=env
        )
    except CompositionTaskDriverError as exc:
        raise HyperplanError(str(exc), code=exc.code) from exc


class _HyperplanTaskAdapter:
    source_kind = "hyperplan_v1"
    result_bundle_kind = HYPERPLAN_RESULT_BUNDLE_KIND
    error_ns = "E_TEAM_HYPERPLAN"

    def load_manifest(self, root: Path, run_id: str) -> dict[str, Any]:
        return load_hyperplan_manifest(root, run_id)

    def composition_lock_path(self, root: Path, run_id: str) -> Path:
        return hyperplan_lock_path(root, run_id)

    def persist_from_bundle_locked(
        self,
        *,
        root: Path,
        run_id: str,
        manifest: Mapping[str, Any],
        bundle: Mapping[str, Any],
    ) -> dict[str, Any]:
        compositions = hyperplan_compositions_dir(root, run_id)
        compositions.mkdir(parents=True, exist_ok=True)
        if compositions.is_symlink():
            raise HyperplanError(
                "compositions directory may not be a symlink",
                code="E_TEAM_HYPERPLAN_PATH",
            )
        return _persist_hyperplan_decision_locked(
            root_path=root,
            rid=run_id,
            manifest=manifest,
            bundle=bundle,
        )

    def raise_error(
        self,
        message: str,
        *,
        code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        raise HyperplanError(message, code=code)


def _bundle_core_equal(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    left = {k: v for k, v in a.items() if k != "digest"}
    right = {k: v for k, v in b.items() if k != "digest"}
    return left == right


def _normalize_result_bundle(
    raw: Any, *, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        body = require_object(raw, label="hyperplan.result_bundle")
        require_exact_keys(
            body,
            required=_BUNDLE_REQUIRED,
            optional=_BUNDLE_OPTIONAL | frozenset({"digest"}),
            label="hyperplan.result_bundle",
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc

    if body.get("kind") != HYPERPLAN_RESULT_BUNDLE_KIND:
        raise HyperplanError(
            "result bundle kind mismatch",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    if body.get("schema_version") != HYPERPLAN_SCHEMA_VERSION:
        raise HyperplanError(
            "result bundle schema_version must be 1",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    try:
        composition_id = require_safe_id(
            body.get("composition_id"), label="composition_id"
        )
        composition_digest = require_sha256(
            body.get("composition_digest"), label="composition_digest"
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
    if composition_id != manifest.get("composition_id"):
        raise HyperplanError(
            "result bundle composition_id does not match manifest",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    if composition_digest != manifest.get("digest"):
        raise HyperplanError(
            "result bundle composition_digest does not match manifest",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    writer = body.get("writer", CLI_WRITER)
    if writer != CLI_WRITER:
        raise HyperplanError(
            "foreign writer refused on result bundle",
            code="E_TEAM_HYPERPLAN_FOREIGN_WRITER",
        )

    required_lanes = [row["lane_id"] for row in manifest["lanes"]]
    lane_meta = {row["lane_id"]: row for row in manifest["lanes"]}
    receipts = _parse_receipts(
        body.get("receipts"),
        required_lanes=required_lanes,
        lane_meta=lane_meta,
    )
    out: dict[str, Any] = {
        "kind": HYPERPLAN_RESULT_BUNDLE_KIND,
        "schema_version": HYPERPLAN_SCHEMA_VERSION,
        "composition_id": composition_id,
        "composition_digest": composition_digest,
        "receipts": receipts,
        "writer": CLI_WRITER,
    }
    if "notes" in body:
        notes = body.get("notes")
        if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
            raise HyperplanError(
                "result bundle notes must be a short string",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        out["notes"] = notes
    core = {k: v for k, v in out.items()}
    digest = sha256_hex(canonical_json_bytes(core))
    if "digest" in body:
        try:
            claimed = require_sha256(body.get("digest"), label="digest")
        except ContractValidationError as exc:
            raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
        if claimed != digest:
            raise HyperplanError(
                "result bundle digest mismatch (CLI recomputed)",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
    out["digest"] = digest
    _assert_bundle_inline_budget(out)
    return out


def _assert_bundle_inline_budget(bundle: Mapping[str, Any]) -> None:
    size = len(canonical_json_bytes(dict(bundle)))
    if size > MAX_INLINE_JSON_BYTES:
        raise HyperplanError(
            f"result bundle exceeds inline budget ({size} > {MAX_INLINE_JSON_BYTES})",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )


def _expected_artifact_kind(lane_id: str, lane_meta: Mapping[str, Any]) -> str:
    expected = lane_meta.get("expected_artifact") or {}
    kind = expected.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    if lane_id.startswith("critic."):
        return "omg.team.hyperplan.critique"
    if lane_id == "synthesize":
        return "omg.team.hyperplan.synthesis"
    if lane_id == "verify":
        return "omg.team.hyperplan.verification"
    raise HyperplanError(
        f"unknown lane artifact kind for {lane_id!r}",
        code="E_TEAM_HYPERPLAN_BUNDLE",
    )


def _parse_receipts(
    raw: Any,
    *,
    required_lanes: Sequence[str],
    lane_meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise HyperplanError(
            "receipts must be a list",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    by_id: dict[str, dict[str, Any]] = {}
    for idx, item in enumerate(raw):
        label = f"receipts[{idx}]"
        try:
            obj = require_object(item, label=label)
            require_exact_keys(
                obj,
                required=_RECEIPT_REQUIRED,
                optional=_RECEIPT_OPTIONAL | frozenset({"digest"}),
                label=label,
            )
            lane_id = require_nonempty_string(
                obj.get("lane_id"), label=f"{label}.lane_id"
            )
        except ContractValidationError as exc:
            raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
        if not _LANE_ID_RE.fullmatch(lane_id):
            raise HyperplanError(
                f"{label}.lane_id is not a valid lane id",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        if lane_id in by_id:
            raise HyperplanError(
                f"duplicate receipt for lane {lane_id!r}",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        status = obj.get("status")
        if status not in _RECEIPT_STATUSES:
            raise HyperplanError(
                f"{label}.status must be one of {sorted(_RECEIPT_STATUSES)}",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        artifact_kind = obj.get("artifact_kind")
        expected_kind = _expected_artifact_kind(
            lane_id, lane_meta[lane_id] if lane_id in lane_meta else {}
        )
        if artifact_kind != expected_kind:
            raise HyperplanError(
                f"{label}.artifact_kind must be {expected_kind!r}",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        payload = _parse_receipt_payload(
            obj.get("payload"),
            lane_id=lane_id,
            artifact_kind=str(artifact_kind),
            lane_meta=lane_meta.get(lane_id) or {},
            label=f"{label}.payload",
        )
        row: dict[str, Any] = {
            "lane_id": lane_id,
            "status": status,
            "artifact_kind": artifact_kind,
            "payload": payload,
        }
        if "reason" in obj:
            reason = obj.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise HyperplanError(
                    f"{label}.reason must be a non-empty string when present",
                    code="E_TEAM_HYPERPLAN_BUNDLE",
                )
            if len(reason) > MAX_NOTE_CHARS:
                raise HyperplanError(
                    f"{label}.reason too long",
                    code="E_TEAM_HYPERPLAN_BUNDLE",
                )
            row["reason"] = reason.strip()
        if status != "complete" and not row.get("reason"):
            raise HyperplanError(
                f"{label} non-complete status requires reason",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        digest = sha256_hex(canonical_json_bytes(row))
        if "digest" in obj:
            try:
                claimed = require_sha256(obj.get("digest"), label=f"{label}.digest")
            except ContractValidationError as exc:
                raise HyperplanError(
                    str(exc), code="E_TEAM_HYPERPLAN_BUNDLE"
                ) from exc
            if claimed != digest:
                raise HyperplanError(
                    f"{label}.digest mismatch (CLI recomputed)",
                    code="E_TEAM_HYPERPLAN_BUNDLE",
                )
        row["digest"] = digest
        by_id[lane_id] = row

    missing = [lane for lane in required_lanes if lane not in by_id]
    if missing:
        raise HyperplanError(
            f"result bundle omits required lanes: {missing!r}",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    extra = sorted(set(by_id) - set(required_lanes))
    if extra:
        raise HyperplanError(
            f"result bundle references unknown lanes: {extra!r}",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    return [by_id[lane] for lane in required_lanes]


def _refuse_forbidden_keys(obj: Mapping[str, Any], *, label: str) -> None:
    bad = sorted(set(obj) & _FORBIDDEN_PAYLOAD_KEYS)
    if bad:
        raise HyperplanError(
            f"{label} contains forbidden fields: {bad!r}",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )


def _parse_receipt_payload(
    raw: Any,
    *,
    lane_id: str,
    artifact_kind: str,
    lane_meta: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    try:
        obj = require_object(raw, label=label)
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
    _refuse_forbidden_keys(obj, label=label)
    if lane_id.startswith("critic."):
        return _parse_critic_payload(obj, lane_meta=lane_meta, label=label)
    if lane_id == "synthesize":
        return _parse_synthesis_payload(obj, label=label)
    if lane_id == "verify":
        return _parse_verify_payload(obj, label=label)
    raise HyperplanError(
        f"{label} unsupported lane {lane_id!r} kind {artifact_kind!r}",
        code="E_TEAM_HYPERPLAN_BUNDLE",
    )


def _require_short_string(raw: Any, *, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise HyperplanError(
            f"{label} must be a non-empty string",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    if len(raw) > MAX_NOTE_CHARS:
        raise HyperplanError(f"{label} too long", code="E_TEAM_HYPERPLAN_BUNDLE")
    return raw.strip()


def _parse_critic_payload(
    obj: Mapping[str, Any], *, lane_meta: Mapping[str, Any], label: str
) -> dict[str, Any]:
    try:
        require_exact_keys(
            obj,
            required=_CRITIC_PAYLOAD_REQUIRED,
            optional=frozenset({"notes"}),
            label=label,
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
    try:
        dimension = require_safe_id(obj.get("dimension"), label=f"{label}.dimension")
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
    expected_dim = lane_meta.get("dimension")
    if expected_dim is not None and dimension != expected_dim:
        raise HyperplanError(
            f"{label}.dimension must be {expected_dim!r}",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    severity = obj.get("severity")
    if severity not in _SEVERITIES:
        raise HyperplanError(
            f"{label}.severity must be one of {sorted(_SEVERITIES)}",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    blocking = obj.get("blocking")
    if not isinstance(blocking, bool):
        raise HyperplanError(
            f"{label}.blocking must be a bool",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    findings = _parse_critic_findings(
        obj.get("findings"), label=f"{label}.findings"
    )
    out: dict[str, Any] = {
        "dimension": dimension,
        "findings": findings,
        "severity": severity,
        "blocking": blocking,
    }
    if "notes" in obj:
        notes = obj.get("notes")
        if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
            raise HyperplanError(
                f"{label}.notes must be a short string",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        out["notes"] = notes
    return out


def _parse_critic_findings(raw: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise HyperplanError(
            f"{label} must be a list",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    if len(raw) > MAX_FINDING_ITEMS:
        raise HyperplanError(
            f"{label} exceeds {MAX_FINDING_ITEMS} items",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        row_label = f"{label}[{idx}]"
        try:
            obj = require_object(item, label=row_label)
            require_exact_keys(
                obj,
                required=_FINDING_REQUIRED,
                optional=_FINDING_OPTIONAL,
                label=row_label,
            )
            finding_id = require_safe_id(
                obj.get("finding_id"), label=f"{row_label}.finding_id"
            )
        except ContractValidationError as exc:
            raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
        if finding_id in seen:
            raise HyperplanError(
                f"duplicate finding_id {finding_id!r}",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        seen.add(finding_id)
        row: dict[str, Any] = {
            "finding_id": finding_id,
            "summary": _require_short_string(
                obj.get("summary"), label=f"{row_label}.summary"
            ),
        }
        if "severity" in obj:
            severity = obj.get("severity")
            if severity not in _SEVERITIES:
                raise HyperplanError(
                    f"{row_label}.severity invalid",
                    code="E_TEAM_HYPERPLAN_BUNDLE",
                )
            row["severity"] = severity
        if "blocking" in obj:
            blocking = obj.get("blocking")
            if not isinstance(blocking, bool):
                raise HyperplanError(
                    f"{row_label}.blocking must be a bool",
                    code="E_TEAM_HYPERPLAN_BUNDLE",
                )
            row["blocking"] = blocking
        if "notes" in obj:
            notes = obj.get("notes")
            if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
                raise HyperplanError(
                    f"{row_label}.notes must be a short string",
                    code="E_TEAM_HYPERPLAN_BUNDLE",
                )
            row["notes"] = notes
        out.append(row)
    out.sort(key=lambda row: row["finding_id"])
    return out


def _parse_synthesis_payload(obj: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    try:
        require_exact_keys(
            obj,
            required=_SYNTHESIS_PAYLOAD_REQUIRED,
            optional=frozenset({"notes"}),
            label=label,
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
    summary = _require_short_string(obj.get("summary"), label=f"{label}.summary")
    recommended = obj.get("recommended_verdict")
    if recommended not in _SYNTHESIS_VERDICTS:
        raise HyperplanError(
            f"{label}.recommended_verdict must be approved|rejected",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    conflicts = _parse_bounded_string_list(
        obj.get("open_conflicts"),
        label=f"{label}.open_conflicts",
        max_items=MAX_CONFLICT_ITEMS,
    )
    merged = _parse_merged_findings(
        obj.get("merged_findings"), label=f"{label}.merged_findings"
    )
    out: dict[str, Any] = {
        "summary": summary,
        "merged_findings": merged,
        "open_conflicts": conflicts,
        "recommended_verdict": recommended,
    }
    if "notes" in obj:
        notes = obj.get("notes")
        if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
            raise HyperplanError(
                f"{label}.notes must be a short string",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        out["notes"] = notes
    return out


def _parse_merged_findings(raw: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise HyperplanError(
            f"{label} must be a list",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    if len(raw) > MAX_FINDING_ITEMS:
        raise HyperplanError(
            f"{label} exceeds {MAX_FINDING_ITEMS} items",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        row_label = f"{label}[{idx}]"
        try:
            obj = require_object(item, label=row_label)
            require_exact_keys(
                obj,
                required=_MERGED_FINDING_REQUIRED,
                optional=_MERGED_FINDING_OPTIONAL,
                label=row_label,
            )
            finding_id = require_safe_id(
                obj.get("finding_id"), label=f"{row_label}.finding_id"
            )
        except ContractValidationError as exc:
            raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
        if finding_id in seen:
            raise HyperplanError(
                f"duplicate merged finding_id {finding_id!r}",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        seen.add(finding_id)
        disposition = obj.get("disposition")
        if disposition not in _FINDING_DISPOSITIONS:
            raise HyperplanError(
                f"{row_label}.disposition must be one of "
                f"{sorted(_FINDING_DISPOSITIONS)}",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        row: dict[str, Any] = {
            "finding_id": finding_id,
            "disposition": disposition,
        }
        if "notes" in obj:
            notes = obj.get("notes")
            if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
                raise HyperplanError(
                    f"{row_label}.notes must be a short string",
                    code="E_TEAM_HYPERPLAN_BUNDLE",
                )
            row["notes"] = notes
        out.append(row)
    out.sort(key=lambda row: row["finding_id"])
    return out


def _parse_verify_payload(obj: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    try:
        require_exact_keys(
            obj,
            required=_VERIFY_PAYLOAD_REQUIRED,
            optional=frozenset({"notes"}),
            label=label,
        )
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_BUNDLE") from exc
    gate = _require_short_string(obj.get("gate"), label=f"{label}.gate")
    if gate != "hyperplan-v1":
        raise HyperplanError(
            f"{label}.gate must be 'hyperplan-v1'",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    verdict = obj.get("verdict")
    if verdict not in _VERIFY_VERDICTS:
        raise HyperplanError(
            f"{label}.verdict must be approved|rejected",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    covered = _parse_bounded_string_list(
        obj.get("covered_lanes"),
        label=f"{label}.covered_lanes",
        max_items=MAX_LIST_ITEMS,
    )
    blockers = _parse_bounded_string_list(
        obj.get("blocking_issues"),
        label=f"{label}.blocking_issues",
        max_items=MAX_LIST_ITEMS,
    )
    # Contradictory verifier approval + blockers: fail closed.
    if verdict == "approved" and blockers:
        raise HyperplanError(
            "verify approval with blocking_issues is contradictory",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    out: dict[str, Any] = {
        "gate": gate,
        "covered_lanes": covered,
        "blocking_issues": blockers,
        "verdict": verdict,
    }
    if "notes" in obj:
        notes = obj.get("notes")
        if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
            raise HyperplanError(
                f"{label}.notes must be a short string",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        out["notes"] = notes
    return out


def _parse_bounded_string_list(
    raw: Any, *, label: str, max_items: int
) -> list[str]:
    if not isinstance(raw, list):
        raise HyperplanError(
            f"{label} must be a list",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    if len(raw) > max_items:
        raise HyperplanError(
            f"{label} exceeds {max_items} items",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )
    out: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise HyperplanError(
                f"{label}[{idx}] must be a non-empty string",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        if len(item) > MAX_NOTE_CHARS:
            raise HyperplanError(
                f"{label}[{idx}] too long",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        out.append(item.strip())
    return out


def _derive_decision_from_bundle(
    manifest: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    receipts = {row["lane_id"]: row for row in bundle["receipts"]}

    # Collect critic findings; IDs must be globally unique.
    findings_by_id: dict[str, dict[str, Any]] = {}
    finding_blocking: dict[str, bool] = {}
    for lane in manifest["lanes"]:
        lane_id = lane["lane_id"]
        if not lane_id.startswith("critic."):
            continue
        receipt = receipts[lane_id]
        if receipt["status"] != "complete":
            continue
        payload = receipt["payload"]
        # Lane-level blocking applies when a finding omits its own flag.
        lane_blocking = bool(payload.get("blocking"))
        for finding in payload["findings"]:
            fid = finding["finding_id"]
            if fid in findings_by_id:
                raise HyperplanError(
                    f"duplicate finding_id {fid!r} across critic lanes",
                    code="E_TEAM_HYPERPLAN_BUNDLE",
                )
            findings_by_id[fid] = finding
            if "blocking" in finding:
                finding_blocking[fid] = bool(finding["blocking"])
            else:
                finding_blocking[fid] = lane_blocking

    synth = receipts["synthesize"]
    synth_payload = synth["payload"]
    merged = list(synth_payload.get("merged_findings") or [])
    open_conflicts = list(synth_payload.get("open_conflicts") or [])
    synth_verdict = synth_payload.get("recommended_verdict")

    merged_ids = {row["finding_id"] for row in merged}
    if set(findings_by_id) != merged_ids:
        missing = sorted(set(findings_by_id) - merged_ids)
        invented = sorted(merged_ids - set(findings_by_id))
        if missing:
            raise HyperplanError(
                f"synthesis omits critic findings: {missing!r}",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )
        raise HyperplanError(
            f"synthesis invents unknown finding ids: {invented!r}",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )

    # Contradictory synthesis approval with open conflicts → fail closed.
    if synth_verdict == "approved" and open_conflicts:
        raise HyperplanError(
            "synthesis approval with open_conflicts is contradictory",
            code="E_TEAM_HYPERPLAN_BUNDLE",
        )

    repairs: list[str] = []
    risks: list[str] = []
    accepted_blocking: list[str] = []
    for row in merged:
        fid = row["finding_id"]
        disposition = row["disposition"]
        note = row.get("notes")
        label = f"{fid}: {note}" if note else fid
        if disposition == "repair":
            repairs.append(label)
        elif disposition == "risk":
            risks.append(label)
        elif disposition == "accepted" and finding_blocking.get(fid):
            accepted_blocking.append(fid)

    coverage: list[dict[str, Any]] = []
    incomplete: list[str] = []
    for lane in manifest["lanes"]:
        lane_id = lane["lane_id"]
        receipt = receipts[lane_id]
        coverage.append(
            {
                "lane_id": lane_id,
                "status": receipt["status"],
                "artifact_digest": receipt["digest"],
            }
        )
        if receipt["status"] != "complete":
            incomplete.append(lane_id)

    verify = receipts["verify"]
    verify_payload = verify["payload"]
    if verify["status"] == "complete":
        expected_covered = [lane["lane_id"] for lane in manifest["lanes"]]
        covered = list(verify_payload.get("covered_lanes") or [])
        if set(covered) != set(expected_covered) or len(covered) != len(
            expected_covered
        ):
            raise HyperplanError(
                "verify.covered_lanes must list every manifest lane exactly once "
                f"(expected {sorted(expected_covered)!r}, got {sorted(set(covered))!r})",
                code="E_TEAM_HYPERPLAN_BUNDLE",
            )

    verify_verdict = verify_payload.get("verdict")
    verify_blockers = list(verify_payload.get("blocking_issues") or [])

    # Derive verdict — incomplete always rejects; approved only under strict gates.
    can_approve = (
        not incomplete
        and not open_conflicts
        and not repairs
        and not risks
        and not accepted_blocking
        and not verify_blockers
        and synth_verdict == "approved"
        and verify_verdict == "approved"
        and synth["status"] == "complete"
        and verify["status"] == "complete"
    )
    verdict = "approved" if can_approve else "rejected"

    sources: dict[str, str] = {
        "composition": str(manifest["digest"]),
        "result_bundle": str(bundle["digest"]),
    }
    for row in coverage:
        key = f"lane_{row['lane_id'].replace('.', '_')}"
        sources[key] = row["artifact_digest"]

    return {
        "kind": HYPERPLAN_DECISION_KIND,
        "schema_version": HYPERPLAN_SCHEMA_VERSION,
        "verdict": verdict,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "lane_coverage": coverage,
        "conflicts": list(open_conflicts),
        "required_repairs": repairs,
        "unresolved_risks": risks,
        "limitations": [
            "execution_supported=false",
            "hermetic_result_production_v1",
        ],
        "source_artifact_digests": dict(sorted(sources.items())),
        "writer": CLI_WRITER,
    }


def _try_load_existing_result_bundle(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise HyperplanError(
            "hyperplan-v1-result-bundle.json may not be a symlink",
            code="E_TEAM_HYPERPLAN_PATH",
        )
    if not path.exists():
        return None
    return _load_result_bundle_file(path)


def _load_result_bundle_file(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise HyperplanError(
            "hyperplan-v1-result-bundle.json may not be a symlink",
            code="E_TEAM_HYPERPLAN_PATH",
        )
    try:
        body = read_managed_regular_bytes(path, max_bytes=MAX_INLINE_JSON_BYTES)
    except FileNotFoundError as exc:
        raise HyperplanError(
            "hyperplan-v1-result-bundle.json missing",
            code="E_TEAM_HYPERPLAN_MISSING",
        ) from exc
    except ContractPathError as exc:
        raise HyperplanError(
            f"hyperplan-v1-result-bundle.json unreadable: {exc}",
            code="E_TEAM_HYPERPLAN_PATH",
        ) from exc
    if not body:
        raise HyperplanError(
            "hyperplan-v1-result-bundle.json empty/corrupt",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HyperplanError(
            "hyperplan-v1-result-bundle.json corrupt JSON",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        ) from exc
    if not isinstance(parsed, dict):
        raise HyperplanError(
            "hyperplan-v1-result-bundle.json must be an object",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    if parsed.get("writer") != CLI_WRITER:
        raise HyperplanError(
            "foreign writer refused on result bundle",
            code="E_TEAM_HYPERPLAN_FOREIGN_WRITER",
        )
    if parsed.get("kind") != HYPERPLAN_RESULT_BUNDLE_KIND:
        raise HyperplanError(
            "result bundle kind mismatch",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    try:
        require_sha256(parsed.get("digest"), label="digest")
    except ContractValidationError as exc:
        raise HyperplanError(str(exc), code="E_TEAM_HYPERPLAN_CORRUPT") from exc
    core = {k: v for k, v in parsed.items() if k != "digest"}
    expected = sha256_hex(canonical_json_bytes(core))
    if expected != parsed["digest"]:
        raise HyperplanError(
            "result bundle digest mismatch (truncated or partially written)",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    return dict(parsed)


def _try_load_existing_decision(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise HyperplanError(
            "hyperplan-v1-decision.json may not be a symlink",
            code="E_TEAM_HYPERPLAN_PATH",
        )
    if not path.exists():
        return None
    try:
        body = read_managed_regular_bytes(path, max_bytes=MAX_INLINE_JSON_BYTES)
    except ContractPathError as exc:
        raise HyperplanError(
            f"hyperplan-v1-decision.json unreadable: {exc}",
            code="E_TEAM_HYPERPLAN_PATH",
        ) from exc
    if not body:
        raise HyperplanError(
            "hyperplan-v1-decision.json empty/corrupt",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HyperplanError(
            "hyperplan-v1-decision.json corrupt JSON",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        ) from exc
    if not isinstance(parsed, dict):
        raise HyperplanError(
            "hyperplan-v1-decision.json must be an object",
            code="E_TEAM_HYPERPLAN_CORRUPT",
        )
    if parsed.get("writer") != CLI_WRITER:
        raise HyperplanError(
            "foreign writer refused on decision",
            code="E_TEAM_HYPERPLAN_FOREIGN_WRITER",
        )
    return dict(parsed)


__all__ = [
    "HYPERPLAN_DECISION_FILENAME",
    "HYPERPLAN_DECISION_KIND",
    "HYPERPLAN_FILENAME",
    "HYPERPLAN_KIND",
    "HYPERPLAN_RESULT_BUNDLE_FILENAME",
    "HYPERPLAN_RESULT_BUNDLE_KIND",
    "HYPERPLAN_SCHEMA_VERSION",
    "HyperplanError",
    "admit_hyperplan_tasks_v1",
    "collect_hyperplan_tasks_v1",
    "compile_hyperplan_decision_v1",
    "compile_hyperplan_v1",
    "hyperplan_decision_path",
    "hyperplan_manifest_path",
    "hyperplan_result_bundle_path",
    "load_hyperplan_manifest",
    "materialize_hyperplan_v1",
    "parse_hyperplan_spec_v1",
    "produce_hyperplan_decision_v1",
    "validate_hyperplan_decision_v1",
]
