"""Security Research Composition Contract V1 — non-executing scaffold (#69 PR8).

Pure ``compile_security_research_v1`` builds a deterministic
hunt→validate×2→consolidate→verify DAG from a bounded spec.
``materialize_security_research_v1`` persists only under the canonical Team
run root:

  ``.omg/state/runs/<run>/team/compositions/security-research-v1.json``

``execution_supported`` is always ``false``. This module never launches panes,
Jobs, providers, Antigravity, MCP tools, or claimable API tasks, never runs
PoCs, and never sets ``verified`` / ``passes``.
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

SECURITY_RESEARCH_KIND = "omg.team.security_research"
SECURITY_RESEARCH_REPORT_KIND = "omg.team.security_research_report"
SECURITY_RESEARCH_SCHEMA_VERSION = 1
SECURITY_RESEARCH_FILENAME = "security-research-v1.json"
SECURITY_RESEARCH_REPORT_FILENAME = "security-research-v1-report.json"
SECURITY_RESEARCH_LOCK_NAME = "security-research-v1.lock"

MIN_ATTACK_SURFACES = 3
MAX_ATTACK_SURFACES = 8
MAX_TARGET_CHARS = 4000
MAX_SURFACE_CHARS = 64
MAX_LIMITS_KEYS = 8
MAX_EVIDENCE_ITEMS = 16
MAX_INLINE_JSON_BYTES = 64 * 1024
MAX_FINDING_ITEMS = 64
MAX_REJECTED_ITEMS = 64
MAX_NOTE_CHARS = 2000
MAX_STRING_CHARS = 2000
MAX_CWE_CHARS = 32
MAX_LIST_ITEMS = 32

_REL_PATH_RE = re.compile(r"^(?!\./)(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")
_ABS_HINT_RE = re.compile(r"(^|/)(Users|home|private|var/folders|tmp)/")
_SURFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_LANE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_CWE_RE = re.compile(r"^CWE-\d{1,5}$")
_SEVERITIES = frozenset({"info", "low", "medium", "high", "critical"})
_PROOF_KINDS = frozenset({"reproduced", "safe_static_proof"})
_VERDICTS = frozenset({"pass", "pass_with_findings", "block"})
_LANE_COVERAGE_STATUSES = frozenset({"complete", "rejected", "blocked"})

# CVSS 3.1 base metric keys (complete vector required when cvss present).
_CVSS_BASE_KEYS = (
    "attack_vector",
    "attack_complexity",
    "privileges_required",
    "user_interaction",
    "scope",
    "confidentiality",
    "integrity",
    "availability",
)

_SPEC_REQUIRED = frozenset({"schema_version", "attack_surfaces"})
_SPEC_OPTIONAL = frozenset({"target", "target_artifact", "limits", "evidence"})
_TARGET_ARTIFACT_KEYS = frozenset({"path", "digest"})
_EVIDENCE_KEYS = frozenset({"path", "digest", "label"})
_LIMITS_ALLOWED = frozenset(
    {
        "max_attack_surfaces",
        "max_inline_bytes",
        "notes",
    }
)

_LANE_KEYS = (
    "lane_id",
    "role",
    "posture",
    "surface",
    "depends_on",
    "requires_code_change",
    "allow_implementation",
    "owned_files",
    "expected_artifact",
)

_SAFE_POC_POLICY = {
    "schema_version": 1,
    "allowed_proof_kinds": ["static", "dry_run", "local_fixture"],
    "forbidden": [
        "network_access",
        "third_party_targets",
        "destructive_actions",
        "target_side_persistence",
        "ambient_or_real_credentials",
    ],
    "execution_supported": False,
    "immutable": True,
}

_REPORT_REQUIRED = frozenset(
    {
        "kind",
        "schema_version",
        "verdict",
        "composition_id",
        "composition_digest",
        "lane_coverage",
        "findings",
        "rejected_candidates",
        "incomplete_audit_blockers",
        "source_artifact_digests",
    }
)
_REPORT_OPTIONAL = frozenset({"notes", "writer", "limitations"})
_LANE_COVERAGE_KEYS = frozenset({"lane_id", "status", "artifact_digest", "reason"})
_FINDING_REQUIRED = frozenset(
    {
        "finding_id",
        "surface",
        "severity",
        "blocking",
        "attacker_capability",
        "attack_path",
        "reachability",
        "impact",
        "cwe_candidate",
        "evidence_locations",
        "remediation",
        "regression_check",
    }
)
_FINDING_OPTIONAL = frozenset(
    {
        "validator_artifact_refs",
        "proof_kind",
        "cvss",
        "notes",
    }
)


class SecurityResearchError(ValueError):
    """Fail-closed Security Research contract error."""

    def __init__(self, message: str, *, code: str = "E_TEAM_SECURITY_RESEARCH") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def security_research_compositions_dir(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / "compositions"


def security_research_manifest_path(root: Path | str, run_id: str) -> Path:
    return security_research_compositions_dir(root, run_id) / SECURITY_RESEARCH_FILENAME


def security_research_report_path(root: Path | str, run_id: str) -> Path:
    return (
        security_research_compositions_dir(root, run_id)
        / SECURITY_RESEARCH_REPORT_FILENAME
    )


def security_research_lock_path(root: Path | str, run_id: str) -> Path:
    return security_research_compositions_dir(root, run_id) / SECURITY_RESEARCH_LOCK_NAME


def parse_security_research_spec_v1(raw: Any) -> dict[str, Any]:
    """Validate and normalize a SecurityResearchSpecV1 (unknown fields refused)."""
    try:
        body = require_object(raw, label="security_research.spec")
        require_exact_keys(
            body,
            required=_SPEC_REQUIRED,
            optional=_SPEC_OPTIONAL,
            label="security_research.spec",
        )
    except ContractValidationError as exc:
        raise SecurityResearchError(str(exc), code="E_TEAM_SECURITY_RESEARCH_SPEC") from exc

    schema = body.get("schema_version")
    if schema != SECURITY_RESEARCH_SCHEMA_VERSION or isinstance(schema, bool):
        raise SecurityResearchError(
            "schema_version must be 1",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )

    has_target = "target" in body
    has_artifact = "target_artifact" in body
    if has_target == has_artifact:
        raise SecurityResearchError(
            "spec requires exactly one of target or target_artifact",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )

    out: dict[str, Any] = {"schema_version": SECURITY_RESEARCH_SCHEMA_VERSION}

    if has_target:
        target = body.get("target")
        if not isinstance(target, str) or not target.strip():
            raise SecurityResearchError(
                "target must be a non-empty string",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        if len(target) > MAX_TARGET_CHARS:
            raise SecurityResearchError(
                f"target exceeds {MAX_TARGET_CHARS} characters",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        if "\x00" in target:
            raise SecurityResearchError(
                "target contains NUL", code="E_TEAM_SECURITY_RESEARCH_SPEC"
            )
        out["target"] = target.strip()
    else:
        out["target_artifact"] = _parse_target_artifact(body.get("target_artifact"))

    surfaces = _parse_surfaces(body.get("attack_surfaces"))
    out["attack_surfaces"] = surfaces

    limits = body.get("limits")
    if limits is not None:
        out["limits"] = _parse_limits(limits, surface_count=len(surfaces))
    else:
        out["limits"] = {}

    evidence = body.get("evidence")
    if evidence is not None:
        out["evidence"] = _parse_evidence_list(evidence)
    else:
        out["evidence"] = []

    _assert_inline_budget(out)
    return out


def compile_security_research_v1(spec: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Pure compiler: SecurityResearchSpecV1 → SecurityResearchManifestV1.

    For N surfaces emits exactly N+4 read-only lanes:
    hunt.<surface>×N, validate.primary, validate.independent, consolidate, verify.
    Always stamps immutable safe-PoC policy and ``execution_supported=false``.
    """
    normalized = parse_security_research_spec_v1(spec)
    surfaces: list[str] = list(normalized["attack_surfaces"])
    hunters: list[dict[str, Any]] = []
    for surface in surfaces:
        lane_id = f"hunt.{surface}"
        hunters.append(
            _lane(
                lane_id=lane_id,
                role="security-reviewer",
                surface=surface,
                depends_on=[],
                expected_artifact=_hunt_artifact_schema(surface),
            )
        )
    hunter_ids = [row["lane_id"] for row in hunters]
    validate_primary = _lane(
        lane_id="validate.primary",
        role="verifier",
        surface=None,
        depends_on=list(hunter_ids),
        expected_artifact=_validate_artifact_schema("primary"),
    )
    validate_independent = _lane(
        lane_id="validate.independent",
        role="verifier",
        surface=None,
        depends_on=list(hunter_ids),
        expected_artifact=_validate_artifact_schema("independent"),
    )
    consolidate = _lane(
        lane_id="consolidate",
        role="security-reviewer",
        surface=None,
        depends_on=[*hunter_ids, "validate.primary", "validate.independent"],
        expected_artifact=_consolidate_artifact_schema(),
    )
    verify = _lane(
        lane_id="verify",
        role="verifier",
        surface=None,
        depends_on=[
            *hunter_ids,
            "validate.primary",
            "validate.independent",
            "consolidate",
        ],
        expected_artifact=_verify_artifact_schema(),
    )
    lanes = [
        *hunters,
        validate_primary,
        validate_independent,
        consolidate,
        verify,
    ]
    dependency_graph = {row["lane_id"]: list(row["depends_on"]) for row in lanes}
    composition_id = _composition_id_for_spec(normalized)
    # Immutable copy — never allow callers to mutate the module constant.
    safe_poc_policy = json.loads(json.dumps(_SAFE_POC_POLICY))
    result_contract = {
        "schema_version": 1,
        "requires_lane_coverage": True,
        "verdicts": sorted(_VERDICTS),
        "pass_requires_all_lanes_complete": True,
        "block_may_preserve_blocked_lanes": True,
        "high_critical_requires_dual_validator_and_proof": True,
        "cvss_requires_complete_metric_vector": True,
        "execution_supported": False,
        "never_writes_passes_or_verified": True,
    }
    manifest_core: dict[str, Any] = {
        "kind": SECURITY_RESEARCH_KIND,
        "schema_version": SECURITY_RESEARCH_SCHEMA_VERSION,
        "composition_id": composition_id,
        "writer": CLI_WRITER,
        "execution_supported": False,
        "safe_poc_policy": safe_poc_policy,
        "spec": normalized,
        "lanes": lanes,
        "dependency_graph": dependency_graph,
        "result_contract": result_contract,
        "lane_count": len(lanes),
        "hunter_count": len(hunters),
    }
    digest = sha256_hex(canonical_json_bytes(manifest_core))
    manifest = dict(manifest_core)
    manifest["digest"] = digest
    return manifest


def materialize_security_research_v1(
    root: Path | str,
    run_id: str,
    spec: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Atomically persist a compiled Security Research manifest under Team run root.

    Idempotent when the on-disk digest matches. Same ``composition_id`` with a
    different digest is refused. Corrupt / symlink / foreign-writer artifacts
    fail closed. Never launches execution surfaces.
    """
    root_path = Path(root)
    rid = _safe_run_id(run_id)
    _require_live_run(root_path, rid)
    manifest = compile_security_research_v1(spec)
    compositions = security_research_compositions_dir(root_path, rid)
    path = security_research_manifest_path(root_path, rid)
    lock = security_research_lock_path(root_path, rid)

    compositions.mkdir(parents=True, exist_ok=True)
    if compositions.is_symlink():
        raise SecurityResearchError(
            "compositions directory may not be a symlink",
            code="E_TEAM_SECURITY_RESEARCH_PATH",
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
                raise SecurityResearchError(
                    "composition_id digest conflict: refusing overwrite",
                    code="E_TEAM_SECURITY_RESEARCH_DIGEST_CONFLICT",
                )
            raise SecurityResearchError(
                "security-research-v1.json already materialized with a different composition",
                code="E_TEAM_SECURITY_RESEARCH_CONFLICT",
            )

        # run_id is persistence binding only — digest stays compile-stable.
        payload = dict(manifest)
        payload["run_id"] = rid
        body = canonical_json_bytes(payload)
        try:
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
        except FileExistsError as exc:
            raise SecurityResearchError(
                "concurrent materialize race refused",
                code="E_TEAM_SECURITY_RESEARCH_RACE",
            ) from exc
        except ContractPathError as exc:
            raise SecurityResearchError(
                f"materialize path refused: {exc}",
                code="E_TEAM_SECURITY_RESEARCH_PATH",
            ) from exc

        loaded = load_security_research_manifest(root_path, rid)
        if loaded.get("digest") != manifest["digest"]:
            raise SecurityResearchError(
                "published digest mismatch",
                code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
            )
        return {
            "ok": True,
            "idempotent": False,
            "path": _rel_under_root(root_path, path),
            "manifest": loaded,
        }


def load_security_research_manifest(root: Path | str, run_id: str) -> dict[str, Any]:
    """Load and validate a persisted SecurityResearchManifestV1 (fail-closed)."""
    root_path = Path(root)
    rid = _safe_run_id(run_id)
    path = security_research_manifest_path(root_path, rid)
    return _load_manifest_file(path, expected_run_id=rid)


def validate_security_research_report_v1(
    root: Path | str,
    run_id: str,
    report: Mapping[str, Any] | Any,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Validate a SecurityResearchReportV1 against the materialized manifest.

    Never invents findings or writes ``passes`` / ``verified``. Verdicts:
    ``pass`` / ``pass_with_findings`` / ``block`` with severity proof gates.
    """
    root_path = Path(root)
    rid = _safe_run_id(run_id)
    _require_live_run(root_path, rid)
    manifest = load_security_research_manifest(root_path, rid)
    normalized = _parse_report(report, manifest=manifest)

    if not persist:
        return {"ok": True, "persisted": False, "report": normalized}

    compositions = security_research_compositions_dir(root_path, rid)
    compositions.mkdir(parents=True, exist_ok=True)
    if compositions.is_symlink():
        raise SecurityResearchError(
            "compositions directory may not be a symlink",
            code="E_TEAM_SECURITY_RESEARCH_PATH",
        )
    path = security_research_report_path(root_path, rid)
    lock = security_research_lock_path(root_path, rid)
    with exclusive_lock(lock):
        body = canonical_json_bytes(normalized)
        try:
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=True)
        except ContractPathError as exc:
            raise SecurityResearchError(
                f"report path refused: {exc}",
                code="E_TEAM_SECURITY_RESEARCH_PATH",
            ) from exc
    return {
        "ok": True,
        "persisted": True,
        "path": _rel_under_root(root_path, path),
        "report": normalized,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_surfaces(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise SecurityResearchError(
            "attack_surfaces must be a non-empty list",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )
    if len(raw) < MIN_ATTACK_SURFACES or len(raw) > MAX_ATTACK_SURFACES:
        raise SecurityResearchError(
            f"attack_surfaces must contain "
            f"{MIN_ATTACK_SURFACES}–{MAX_ATTACK_SURFACES} items",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )
    surfaces: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        label = f"attack_surfaces[{idx}]"
        if not isinstance(item, str) or not item.strip():
            raise SecurityResearchError(
                f"{label} must be a non-empty string",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        text = item.strip()
        if len(text) > MAX_SURFACE_CHARS:
            raise SecurityResearchError(
                f"{label} exceeds {MAX_SURFACE_CHARS} characters",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        try:
            safe = require_safe_id(text, label=label)
        except ContractValidationError as exc:
            raise SecurityResearchError(
                str(exc), code="E_TEAM_SECURITY_RESEARCH_SPEC"
            ) from exc
        if not _SURFACE_RE.fullmatch(safe):
            raise SecurityResearchError(
                f"{label} must match {_SURFACE_RE.pattern}",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        if safe in seen:
            raise SecurityResearchError(
                f"duplicate attack surface {safe!r}",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        seen.add(safe)
        surfaces.append(safe)
    return sorted(surfaces)


def _parse_target_artifact(raw: Any) -> dict[str, Any]:
    try:
        obj = require_object(raw, label="target_artifact")
        require_exact_keys(
            obj,
            required=_TARGET_ARTIFACT_KEYS,
            optional=frozenset(),
            label="target_artifact",
        )
        path = require_nonempty_string(obj.get("path"), label="target_artifact.path")
        digest = require_sha256(obj.get("digest"), label="target_artifact.digest")
    except ContractValidationError as exc:
        raise SecurityResearchError(str(exc), code="E_TEAM_SECURITY_RESEARCH_SPEC") from exc
    _assert_relative_safe(path, label="target_artifact.path")
    return {"path": path.replace("\\", "/"), "digest": digest}


def _parse_limits(raw: Any, *, surface_count: int) -> dict[str, Any]:
    try:
        obj = require_object(raw, label="limits")
    except ContractValidationError as exc:
        raise SecurityResearchError(str(exc), code="E_TEAM_SECURITY_RESEARCH_SPEC") from exc
    unknown = set(obj) - _LIMITS_ALLOWED
    if unknown:
        raise SecurityResearchError(
            f"limits has unknown fields: {sorted(unknown)!r}",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )
    if len(obj) > MAX_LIMITS_KEYS:
        raise SecurityResearchError(
            "limits has too many fields",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )
    out: dict[str, Any] = {}
    if "max_attack_surfaces" in obj:
        value = obj["max_attack_surfaces"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise SecurityResearchError(
                "limits.max_attack_surfaces must be an int",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        if value < surface_count or value > MAX_ATTACK_SURFACES:
            raise SecurityResearchError(
                "limits.max_attack_surfaces out of range",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        out["max_attack_surfaces"] = value
    if "max_inline_bytes" in obj:
        value = obj["max_inline_bytes"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise SecurityResearchError(
                "limits.max_inline_bytes must be an int",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        if value < 256 or value > MAX_INLINE_JSON_BYTES:
            raise SecurityResearchError(
                "limits.max_inline_bytes out of range",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        out["max_inline_bytes"] = value
    if "notes" in obj:
        notes = obj["notes"]
        if not isinstance(notes, str):
            raise SecurityResearchError(
                "limits.notes must be a string",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        if len(notes) > MAX_NOTE_CHARS:
            raise SecurityResearchError(
                "limits.notes too long",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        out["notes"] = notes
    return out


def _parse_evidence_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise SecurityResearchError(
            "evidence must be a list",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )
    if len(raw) > MAX_EVIDENCE_ITEMS:
        raise SecurityResearchError(
            f"evidence exceeds {MAX_EVIDENCE_ITEMS} items",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
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
            raise SecurityResearchError(
                str(exc), code="E_TEAM_SECURITY_RESEARCH_SPEC"
            ) from exc
        _assert_relative_safe(path, label=f"{label}.path")
        norm = path.replace("\\", "/")
        if norm in seen_paths:
            raise SecurityResearchError(
                f"duplicate evidence path {norm!r}",
                code="E_TEAM_SECURITY_RESEARCH_SPEC",
            )
        seen_paths.add(norm)
        row: dict[str, Any] = {"path": norm, "digest": digest}
        if "label" in obj:
            try:
                row["label"] = require_safe_id(obj.get("label"), label=f"{label}.label")
            except ContractValidationError as exc:
                raise SecurityResearchError(
                    str(exc), code="E_TEAM_SECURITY_RESEARCH_SPEC"
                ) from exc
        out.append(row)
    out.sort(key=lambda row: row["path"])
    return out


def _assert_inline_budget(spec: Mapping[str, Any]) -> None:
    budget = int(spec.get("limits", {}).get("max_inline_bytes") or MAX_INLINE_JSON_BYTES)
    size = len(canonical_json_bytes(dict(spec)))
    if size > budget:
        raise SecurityResearchError(
            f"spec exceeds inline budget ({size} > {budget})",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )


def _assert_relative_safe(path: str, *, label: str) -> None:
    text = path.replace("\\", "/")
    if text.startswith("/") or text.startswith("~") or "://" in text:
        raise SecurityResearchError(
            f"{label} must be a relative safe path",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )
    if not _REL_PATH_RE.fullmatch(text):
        raise SecurityResearchError(
            f"{label} is not a safe relative path",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )
    if _ABS_HINT_RE.search(text):
        raise SecurityResearchError(
            f"{label} looks absolute/unsafe",
            code="E_TEAM_SECURITY_RESEARCH_SPEC",
        )


def _lane(
    *,
    lane_id: str,
    role: str,
    surface: str | None,
    depends_on: Sequence[str],
    expected_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "lane_id": lane_id,
        "role": role,
        "posture": "read-only",
        "surface": surface,
        "depends_on": list(depends_on),
        "requires_code_change": False,
        "allow_implementation": False,
        "owned_files": [],
        "expected_artifact": dict(expected_artifact),
    }
    return {key: row[key] for key in _LANE_KEYS}


def _hunt_artifact_schema(surface: str) -> dict[str, Any]:
    return {
        "kind": "omg.team.security_research.candidate_findings",
        "schema_version": 1,
        "surface": surface,
        "required_fields": [
            "surface",
            "candidates",
            "severity_hints",
            "evidence_pointers",
        ],
    }


def _validate_artifact_schema(lane: str) -> dict[str, Any]:
    return {
        "kind": "omg.team.security_research.validation",
        "schema_version": 1,
        "lane": lane,
        "required_fields": [
            "validated",
            "falsified",
            "proof_kind",
            "artifact_digest",
        ],
    }


def _consolidate_artifact_schema() -> dict[str, Any]:
    return {
        "kind": "omg.team.security_research.consolidated_report",
        "schema_version": 1,
        "required_fields": [
            "surviving_findings",
            "rejected_candidates",
            "severity_calibration",
            "recommended_verdict",
        ],
    }


def _verify_artifact_schema() -> dict[str, Any]:
    return {
        "kind": "omg.team.security_research.gate",
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
        "schema_version": SECURITY_RESEARCH_SCHEMA_VERSION,
        "attack_surfaces": list(spec["attack_surfaces"]),
        "target": spec.get("target"),
        "target_artifact": spec.get("target_artifact"),
        "limits": dict(spec.get("limits") or {}),
        "evidence": list(spec.get("evidence") or []),
    }
    digest = sha256_hex(canonical_json_bytes(seed))
    return f"sr1_{digest[:16]}"


def _require_live_run(root: Path, run_id: str) -> dict[str, Any]:
    status = load_run(root, run_id)
    if status is None:
        raise SecurityResearchError(
            f"run {run_id!r} missing or unreadable",
            code="E_TEAM_SECURITY_RESEARCH_STALE_RUN",
        )
    if status.get("run_id") != run_id:
        raise SecurityResearchError(
            "status.json run_id mismatch",
            code="E_TEAM_SECURITY_RESEARCH_STALE_RUN",
        )
    if status.get("status") == "cancelled":
        raise SecurityResearchError(
            f"run {run_id!r} is cancelled",
            code="E_TEAM_SECURITY_RESEARCH_STALE_RUN",
        )
    return status


def _try_load_existing_manifest(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise SecurityResearchError(
            "security-research-v1.json may not be a symlink",
            code="E_TEAM_SECURITY_RESEARCH_PATH",
        )
    if not path.exists():
        return None
    return _load_manifest_file(path, expected_run_id=None)


def _load_manifest_file(
    path: Path, *, expected_run_id: str | None
) -> dict[str, Any]:
    if path.is_symlink():
        raise SecurityResearchError(
            "security-research-v1.json may not be a symlink",
            code="E_TEAM_SECURITY_RESEARCH_PATH",
        )
    try:
        body = read_managed_regular_bytes(path, max_bytes=MAX_INLINE_JSON_BYTES)
    except FileNotFoundError as exc:
        raise SecurityResearchError(
            "security-research-v1.json missing",
            code="E_TEAM_SECURITY_RESEARCH_MISSING",
        ) from exc
    except ContractPathError as exc:
        raise SecurityResearchError(
            f"security-research-v1.json unreadable: {exc}",
            code="E_TEAM_SECURITY_RESEARCH_PATH",
        ) from exc
    if not body:
        raise SecurityResearchError(
            "security-research-v1.json empty/corrupt",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
        )
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityResearchError(
            "security-research-v1.json corrupt JSON",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
        ) from exc
    if not isinstance(parsed, dict):
        raise SecurityResearchError(
            "security-research-v1.json must be an object",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
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
        "safe_poc_policy",
        "spec",
        "lanes",
        "dependency_graph",
        "result_contract",
        "lane_count",
        "hunter_count",
    }
    optional = {"run_id"}
    try:
        require_exact_keys(
            raw,
            required=required,
            optional=optional,
            label="security_research.manifest",
        )
    except ContractValidationError as exc:
        raise SecurityResearchError(
            str(exc), code="E_TEAM_SECURITY_RESEARCH_CORRUPT"
        ) from exc

    if raw.get("kind") != SECURITY_RESEARCH_KIND:
        raise SecurityResearchError(
            "manifest kind mismatch", code="E_TEAM_SECURITY_RESEARCH_CORRUPT"
        )
    if raw.get("schema_version") != SECURITY_RESEARCH_SCHEMA_VERSION:
        raise SecurityResearchError(
            "manifest schema_version mismatch",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
        )
    if raw.get("writer") != CLI_WRITER:
        raise SecurityResearchError(
            "foreign writer refused",
            code="E_TEAM_SECURITY_RESEARCH_FOREIGN_WRITER",
        )
    if raw.get("execution_supported") is not False:
        raise SecurityResearchError(
            "execution_supported must be false",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
        )
    policy = raw.get("safe_poc_policy")
    if not isinstance(policy, dict) or policy.get("immutable") is not True:
        raise SecurityResearchError(
            "safe_poc_policy must be immutable",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
        )
    if policy.get("execution_supported") is not False:
        raise SecurityResearchError(
            "safe_poc_policy.execution_supported must be false",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
        )
    # Policy content must match the frozen constant (minus accidental mutation).
    if policy != _SAFE_POC_POLICY:
        raise SecurityResearchError(
            "safe_poc_policy drift from immutable contract",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
        )
    try:
        require_safe_id(raw.get("composition_id"), label="composition_id")
        require_sha256(raw.get("digest"), label="digest")
    except ContractValidationError as exc:
        raise SecurityResearchError(
            str(exc), code="E_TEAM_SECURITY_RESEARCH_CORRUPT"
        ) from exc

    run_id = raw.get("run_id")
    if expected_run_id is not None:
        if run_id != expected_run_id:
            raise SecurityResearchError(
                "manifest run_id mismatch",
                code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
            )
    elif run_id is not None:
        try:
            _safe_run_id(str(run_id))
        except ValueError as exc:
            raise SecurityResearchError(
                "manifest run_id invalid",
                code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
            ) from exc

    core = {k: v for k, v in raw.items() if k not in {"digest", "run_id"}}
    expected = sha256_hex(canonical_json_bytes(core))
    if expected != raw["digest"]:
        raise SecurityResearchError(
            "manifest digest mismatch (truncated or partially written)",
            code="E_TEAM_SECURITY_RESEARCH_CORRUPT",
        )

    spec = parse_security_research_spec_v1(raw.get("spec"))
    if not isinstance(raw.get("lanes"), list) or not raw["lanes"]:
        raise SecurityResearchError(
            "manifest lanes corrupt", code="E_TEAM_SECURITY_RESEARCH_CORRUPT"
        )
    expected_count = len(spec["attack_surfaces"]) + 4
    if int(raw.get("lane_count") or -1) != expected_count:
        raise SecurityResearchError(
            "lane_count mismatch", code="E_TEAM_SECURITY_RESEARCH_CORRUPT"
        )
    if int(raw.get("hunter_count") or -1) != len(spec["attack_surfaces"]):
        raise SecurityResearchError(
            "hunter_count mismatch", code="E_TEAM_SECURITY_RESEARCH_CORRUPT"
        )
    return dict(raw)


def _parse_report(raw: Any, *, manifest: Mapping[str, Any]) -> dict[str, Any]:
    try:
        body = require_object(raw, label="security_research.report")
        require_exact_keys(
            body,
            required=_REPORT_REQUIRED,
            optional=_REPORT_OPTIONAL,
            label="security_research.report",
        )
    except ContractValidationError as exc:
        raise SecurityResearchError(
            str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
        ) from exc

    if body.get("kind") != SECURITY_RESEARCH_REPORT_KIND:
        raise SecurityResearchError(
            "report kind mismatch",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    if body.get("schema_version") != SECURITY_RESEARCH_SCHEMA_VERSION:
        raise SecurityResearchError(
            "report schema_version must be 1",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    verdict = body.get("verdict")
    if verdict not in _VERDICTS:
        raise SecurityResearchError(
            "verdict must be pass|pass_with_findings|block",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    try:
        composition_id = require_safe_id(
            body.get("composition_id"), label="composition_id"
        )
        composition_digest = require_sha256(
            body.get("composition_digest"), label="composition_digest"
        )
    except ContractValidationError as exc:
        raise SecurityResearchError(
            str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
        ) from exc

    if composition_id != manifest.get("composition_id"):
        raise SecurityResearchError(
            "report composition_id does not match manifest",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    if composition_digest != manifest.get("digest"):
        raise SecurityResearchError(
            "report composition_digest does not match manifest",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )

    writer = body.get("writer", CLI_WRITER)
    if writer != CLI_WRITER:
        raise SecurityResearchError(
            "foreign writer refused on report",
            code="E_TEAM_SECURITY_RESEARCH_FOREIGN_WRITER",
        )

    required_lanes = [row["lane_id"] for row in manifest["lanes"]]
    coverage = _parse_lane_coverage(
        body.get("lane_coverage"),
        required_lanes=required_lanes,
        verdict=str(verdict),
    )
    findings = _parse_findings(
        body.get("findings"),
        surfaces=list(manifest["spec"]["attack_surfaces"]),
    )
    rejected = _parse_rejected_candidates(body.get("rejected_candidates"))
    blockers = _parse_string_list(
        body.get("incomplete_audit_blockers"),
        label="incomplete_audit_blockers",
        max_items=MAX_LIST_ITEMS,
    )
    sources = _parse_source_digests(
        body.get("source_artifact_digests"), manifest=manifest, coverage=coverage
    )

    _enforce_verdict_consistency(
        verdict=str(verdict),
        coverage=coverage,
        findings=findings,
        blockers=blockers,
    )

    out: dict[str, Any] = {
        "kind": SECURITY_RESEARCH_REPORT_KIND,
        "schema_version": SECURITY_RESEARCH_SCHEMA_VERSION,
        "verdict": verdict,
        "composition_id": composition_id,
        "composition_digest": composition_digest,
        "lane_coverage": coverage,
        "findings": findings,
        "rejected_candidates": rejected,
        "incomplete_audit_blockers": blockers,
        "source_artifact_digests": sources,
        "writer": CLI_WRITER,
    }
    if "limitations" in body:
        out["limitations"] = _parse_string_list(
            body.get("limitations"),
            label="limitations",
            max_items=MAX_LIST_ITEMS,
        )
    if "notes" in body:
        notes = body.get("notes")
        if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
            raise SecurityResearchError(
                "notes must be a short string",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        out["notes"] = notes
    return out


def _enforce_verdict_consistency(
    *,
    verdict: str,
    coverage: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    blockers: Sequence[str],
) -> None:
    incomplete = [row["lane_id"] for row in coverage if row["status"] != "complete"]
    blocking_findings = [f for f in findings if f.get("blocking") is True]

    if verdict in ("pass", "pass_with_findings"):
        if incomplete:
            raise SecurityResearchError(
                f"{verdict} requires all lanes complete; incomplete={incomplete!r}",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        if blockers:
            raise SecurityResearchError(
                f"{verdict} forbids incomplete_audit_blockers",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        if blocking_findings:
            raise SecurityResearchError(
                f"{verdict} forbids blocking findings",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        if verdict == "pass" and findings:
            raise SecurityResearchError(
                "pass requires no surviving findings",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        if verdict == "pass_with_findings" and not findings:
            raise SecurityResearchError(
                "pass_with_findings requires at least one non-blocking finding",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        return

    # block: either a blocking finding or an explicit incomplete-audit blocker.
    if not blocking_findings and not blockers:
        raise SecurityResearchError(
            "block requires a blocking finding or incomplete_audit_blockers",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    # Blocked lanes must carry reasons when status=blocked.
    for row in coverage:
        if row["status"] == "blocked" and not row.get("reason"):
            raise SecurityResearchError(
                f"blocked lane {row['lane_id']!r} requires reason",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )


def _parse_lane_coverage(
    raw: Any,
    *,
    required_lanes: Sequence[str],
    verdict: str,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise SecurityResearchError(
            "lane_coverage must be a list",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    by_id: dict[str, dict[str, Any]] = {}
    for idx, item in enumerate(raw):
        label = f"lane_coverage[{idx}]"
        try:
            obj = require_object(item, label=label)
            # reason is optional except enforced for blocked under block.
            require_exact_keys(
                obj,
                required=frozenset({"lane_id", "status", "artifact_digest"}),
                optional=frozenset({"reason"}),
                label=label,
            )
            lane_id = require_nonempty_string(obj.get("lane_id"), label=f"{label}.lane_id")
            if not _LANE_ID_RE.fullmatch(lane_id):
                raise SecurityResearchError(
                    f"{label}.lane_id is not a valid lane id",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
            status = require_nonempty_string(obj.get("status"), label=f"{label}.status")
            digest = require_sha256(
                obj.get("artifact_digest"), label=f"{label}.artifact_digest"
            )
        except ContractValidationError as exc:
            raise SecurityResearchError(
                str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
            ) from exc
        if status not in _LANE_COVERAGE_STATUSES:
            raise SecurityResearchError(
                f"{label}.status must be one of {sorted(_LANE_COVERAGE_STATUSES)}",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        if lane_id in by_id:
            raise SecurityResearchError(
                f"duplicate lane_coverage for {lane_id!r}",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        row: dict[str, Any] = {
            "lane_id": lane_id,
            "status": status,
            "artifact_digest": digest,
        }
        if "reason" in obj:
            reason = obj.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise SecurityResearchError(
                    f"{label}.reason must be a non-empty string when present",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
            if len(reason) > MAX_NOTE_CHARS:
                raise SecurityResearchError(
                    f"{label}.reason too long",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
            row["reason"] = reason.strip()
        # Non-block verdicts may not carry blocked lanes without flipping.
        if verdict in ("pass", "pass_with_findings") and status != "complete":
            raise SecurityResearchError(
                f"{verdict} forbids non-complete lane {lane_id!r}",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        by_id[lane_id] = row

    missing = [lane for lane in required_lanes if lane not in by_id]
    if missing:
        raise SecurityResearchError(
            f"report omits required lanes: {missing!r}",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    extra = sorted(set(by_id) - set(required_lanes))
    if extra:
        raise SecurityResearchError(
            f"report references unknown lanes: {extra!r}",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    return [by_id[lane] for lane in required_lanes]


def _parse_findings(
    raw: Any, *, surfaces: Sequence[str]
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise SecurityResearchError(
            "findings must be a list",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    if len(raw) > MAX_FINDING_ITEMS:
        raise SecurityResearchError(
            f"findings exceeds {MAX_FINDING_ITEMS} items",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    surface_set = set(surfaces)
    for idx, item in enumerate(raw):
        label = f"findings[{idx}]"
        try:
            obj = require_object(item, label=label)
            require_exact_keys(
                obj,
                required=_FINDING_REQUIRED,
                optional=_FINDING_OPTIONAL,
                label=label,
            )
            finding_id = require_safe_id(obj.get("finding_id"), label=f"{label}.finding_id")
            surface = require_safe_id(obj.get("surface"), label=f"{label}.surface")
        except ContractValidationError as exc:
            raise SecurityResearchError(
                str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
            ) from exc
        if finding_id in seen_ids:
            raise SecurityResearchError(
                f"duplicate finding_id {finding_id!r}",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        seen_ids.add(finding_id)
        if surface not in surface_set:
            raise SecurityResearchError(
                f"{label}.surface {surface!r} not in attack_surfaces",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        severity = obj.get("severity")
        if severity not in _SEVERITIES:
            raise SecurityResearchError(
                f"{label}.severity must be one of {sorted(_SEVERITIES)}",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        blocking = obj.get("blocking")
        if not isinstance(blocking, bool):
            raise SecurityResearchError(
                f"{label}.blocking must be a bool",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )

        row: dict[str, Any] = {
            "finding_id": finding_id,
            "surface": surface,
            "severity": severity,
            "blocking": blocking,
            "attacker_capability": _require_short_string(
                obj.get("attacker_capability"), label=f"{label}.attacker_capability"
            ),
            "attack_path": _require_short_string(
                obj.get("attack_path"), label=f"{label}.attack_path"
            ),
            "reachability": _require_short_string(
                obj.get("reachability"), label=f"{label}.reachability"
            ),
            "impact": _require_short_string(obj.get("impact"), label=f"{label}.impact"),
            "cwe_candidate": _require_cwe(obj.get("cwe_candidate"), label=f"{label}.cwe_candidate"),
            "evidence_locations": _parse_string_list(
                obj.get("evidence_locations"),
                label=f"{label}.evidence_locations",
                max_items=MAX_LIST_ITEMS,
            ),
            "remediation": _require_short_string(
                obj.get("remediation"), label=f"{label}.remediation"
            ),
            "regression_check": _require_short_string(
                obj.get("regression_check"), label=f"{label}.regression_check"
            ),
        }
        if not row["evidence_locations"]:
            raise SecurityResearchError(
                f"{label}.evidence_locations must be non-empty",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )

        # high/critical proof gate: dual validator refs + proof_kind.
        if severity in ("high", "critical"):
            refs = obj.get("validator_artifact_refs")
            if not isinstance(refs, list) or len(refs) < 2:
                raise SecurityResearchError(
                    f"{label} high/critical requires both validator artifact refs",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
            parsed_refs: list[str] = []
            for ridx, ref in enumerate(refs):
                try:
                    parsed_refs.append(
                        require_sha256(ref, label=f"{label}.validator_artifact_refs[{ridx}]")
                    )
                except ContractValidationError as exc:
                    raise SecurityResearchError(
                        str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
                    ) from exc
            # Must cite both primary + independent validators (distinct digests).
            if len(set(parsed_refs)) < 2:
                raise SecurityResearchError(
                    f"{label} high/critical requires two distinct validator digests",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
            row["validator_artifact_refs"] = parsed_refs
            proof_kind = obj.get("proof_kind")
            if proof_kind not in _PROOF_KINDS:
                raise SecurityResearchError(
                    f"{label} high/critical requires proof_kind "
                    f"reproduced|safe_static_proof",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
            row["proof_kind"] = proof_kind
        else:
            if "validator_artifact_refs" in obj:
                refs = obj.get("validator_artifact_refs")
                if refs is not None:
                    if not isinstance(refs, list):
                        raise SecurityResearchError(
                            f"{label}.validator_artifact_refs must be a list",
                            code="E_TEAM_SECURITY_RESEARCH_REPORT",
                        )
                    parsed_refs = []
                    for ridx, ref in enumerate(refs):
                        try:
                            parsed_refs.append(
                                require_sha256(
                                    ref, label=f"{label}.validator_artifact_refs[{ridx}]"
                                )
                            )
                        except ContractValidationError as exc:
                            raise SecurityResearchError(
                                str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
                            ) from exc
                    row["validator_artifact_refs"] = parsed_refs
            if "proof_kind" in obj:
                proof_kind = obj.get("proof_kind")
                if proof_kind not in _PROOF_KINDS:
                    raise SecurityResearchError(
                        f"{label}.proof_kind invalid",
                        code="E_TEAM_SECURITY_RESEARCH_REPORT",
                    )
                row["proof_kind"] = proof_kind

        if "cvss" in obj:
            row["cvss"] = _parse_cvss(obj.get("cvss"), label=f"{label}.cvss")
        if "notes" in obj:
            notes = obj.get("notes")
            if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
                raise SecurityResearchError(
                    f"{label}.notes must be a short string",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
            row["notes"] = notes
        out.append(row)
    return out


def _parse_rejected_candidates(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise SecurityResearchError(
            "rejected_candidates must be a list",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    if len(raw) > MAX_REJECTED_ITEMS:
        raise SecurityResearchError(
            f"rejected_candidates exceeds {MAX_REJECTED_ITEMS} items",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        label = f"rejected_candidates[{idx}]"
        try:
            obj = require_object(item, label=label)
            require_exact_keys(
                obj,
                required=frozenset({"candidate_id", "disposition", "reason"}),
                optional=frozenset({"surface", "notes"}),
                label=label,
            )
            candidate_id = require_safe_id(
                obj.get("candidate_id"), label=f"{label}.candidate_id"
            )
            disposition = require_nonempty_string(
                obj.get("disposition"), label=f"{label}.disposition"
            )
        except ContractValidationError as exc:
            raise SecurityResearchError(
                str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
            ) from exc
        if disposition not in ("falsified", "downgraded"):
            raise SecurityResearchError(
                f"{label}.disposition must be falsified|downgraded",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        reason = _require_short_string(obj.get("reason"), label=f"{label}.reason")
        row: dict[str, Any] = {
            "candidate_id": candidate_id,
            "disposition": disposition,
            "reason": reason,
        }
        if "surface" in obj:
            try:
                row["surface"] = require_safe_id(
                    obj.get("surface"), label=f"{label}.surface"
                )
            except ContractValidationError as exc:
                raise SecurityResearchError(
                    str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
                ) from exc
        if "notes" in obj:
            notes = obj.get("notes")
            if not isinstance(notes, str) or len(notes) > MAX_NOTE_CHARS:
                raise SecurityResearchError(
                    f"{label}.notes must be a short string",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
            row["notes"] = notes
        out.append(row)
    return out


def _parse_cvss(raw: Any, *, label: str) -> dict[str, Any]:
    try:
        obj = require_object(raw, label=label)
    except ContractValidationError as exc:
        raise SecurityResearchError(
            str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
        ) from exc
    missing = [k for k in _CVSS_BASE_KEYS if k not in obj]
    if missing:
        raise SecurityResearchError(
            f"{label} missing complete metric vector keys: {missing!r}",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    unknown = set(obj) - set(_CVSS_BASE_KEYS) - {"score", "vector_string"}
    if unknown:
        raise SecurityResearchError(
            f"{label} has unknown fields: {sorted(unknown)!r}",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    out: dict[str, Any] = {}
    for key in _CVSS_BASE_KEYS:
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SecurityResearchError(
                f"{label}.{key} must be a non-empty string",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        out[key] = value.strip()
    if "score" in obj:
        score = obj["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise SecurityResearchError(
                f"{label}.score must be a number",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        if score < 0 or score > 10:
            raise SecurityResearchError(
                f"{label}.score out of range",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        out["score"] = float(score)
    if "vector_string" in obj:
        vs = obj["vector_string"]
        if not isinstance(vs, str) or not vs.strip() or len(vs) > 128:
            raise SecurityResearchError(
                f"{label}.vector_string invalid",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        out["vector_string"] = vs.strip()
    return out


def _require_cwe(raw: Any, *, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SecurityResearchError(
            f"{label} must be a non-empty CWE id",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    text = raw.strip()
    if len(text) > MAX_CWE_CHARS or not _CWE_RE.fullmatch(text):
        raise SecurityResearchError(
            f"{label} must match CWE-N",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    return text


def _require_short_string(raw: Any, *, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SecurityResearchError(
            f"{label} must be a non-empty string",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    if len(raw) > MAX_STRING_CHARS:
        raise SecurityResearchError(
            f"{label} too long",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    return raw.strip()


def _parse_string_list(raw: Any, *, label: str, max_items: int) -> list[str]:
    if not isinstance(raw, list):
        raise SecurityResearchError(
            f"{label} must be a list",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    if len(raw) > max_items:
        raise SecurityResearchError(
            f"{label} exceeds {max_items} items",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    out: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise SecurityResearchError(
                f"{label}[{idx}] must be a non-empty string",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        if len(item) > MAX_NOTE_CHARS:
            raise SecurityResearchError(
                f"{label}[{idx}] too long",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        out.append(item.strip())
    return out


def _parse_source_digests(
    raw: Any,
    *,
    manifest: Mapping[str, Any],
    coverage: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    try:
        obj = require_object(raw, label="source_artifact_digests")
    except ContractValidationError as exc:
        raise SecurityResearchError(
            str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
        ) from exc
    required_keys = {"composition"}
    missing = required_keys - set(obj)
    if missing:
        raise SecurityResearchError(
            f"source_artifact_digests missing {sorted(missing)!r}",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    out: dict[str, str] = {}
    for key, value in obj.items():
        try:
            safe_key = require_safe_id(key, label="source_artifact_digests.key")
            digest = require_sha256(
                value, label=f"source_artifact_digests[{safe_key}]"
            )
        except ContractValidationError as exc:
            raise SecurityResearchError(
                str(exc), code="E_TEAM_SECURITY_RESEARCH_REPORT"
            ) from exc
        out[safe_key] = digest
    if out["composition"] != manifest.get("digest"):
        raise SecurityResearchError(
            "source_artifact_digests.composition must match manifest digest",
            code="E_TEAM_SECURITY_RESEARCH_REPORT",
        )
    # Bind every lane artifact digest under lane:<lane_id> when provided;
    # require all lanes to be bound for V1 completeness.
    for row in coverage:
        lane_key = f"lane_{row['lane_id'].replace('.', '_')}"
        # Accept either lane_<id> or the raw lane_id as safe_id may reject dots.
        bound = None
        if lane_key in out:
            bound = out[lane_key]
        # Also allow explicit per-lane keys already validated as safe ids.
        alt = row["lane_id"].replace(".", "-")
        if alt in out:
            bound = out[alt]
        if bound is None:
            raise SecurityResearchError(
                f"source_artifact_digests missing lane binding for {row['lane_id']!r} "
                f"(expected {lane_key!r} or {alt!r})",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
        if bound != row["artifact_digest"]:
            raise SecurityResearchError(
                f"source digest for lane {row['lane_id']!r} mismatch",
                code="E_TEAM_SECURITY_RESEARCH_REPORT",
            )
    for item in manifest.get("spec", {}).get("evidence") or []:
        label = item.get("label")
        if isinstance(label, str) and label in out:
            if out[label] != item.get("digest"):
                raise SecurityResearchError(
                    f"source digest for evidence label {label!r} mismatch",
                    code="E_TEAM_SECURITY_RESEARCH_REPORT",
                )
    return dict(sorted(out.items()))


def _rel_under_root(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "SECURITY_RESEARCH_FILENAME",
    "SECURITY_RESEARCH_KIND",
    "SECURITY_RESEARCH_LOCK_NAME",
    "SECURITY_RESEARCH_REPORT_FILENAME",
    "SECURITY_RESEARCH_REPORT_KIND",
    "SECURITY_RESEARCH_SCHEMA_VERSION",
    "SecurityResearchError",
    "compile_security_research_v1",
    "load_security_research_manifest",
    "materialize_security_research_v1",
    "parse_security_research_spec_v1",
    "security_research_manifest_path",
    "security_research_report_path",
    "validate_security_research_report_v1",
]
