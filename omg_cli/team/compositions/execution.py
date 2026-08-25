"""Composition Execution V1 — fixture + grok Jobs auto-worker path (#69).

Leader-only driver that runs the existing claim-lane / submit-lane-result
protocol, collects lane results, and persists
``omg.team.composition_execution_v1`` **only** after those workers ran.

``execution_supported=true`` is allowed **only** on that evidence document,
and only when worker evidence is complete (run ids, pane ids, lane
result digests; grok rows also carry ``job_id``). Forged
``{execution_supported: true}`` without evidence is refused. ``--input`` is a
composition ``ResultBundleV1`` and is normalized with the same exact-key /
foreign-writer / digest / artifact_kind contract as produce-decision /
produce-report **before** workers submit ``LaneTaskResultV1`` payloads.
Compile / produce / admit / collect / claim keep ``execution_supported=false``.

Executors: ``fixture`` (in-process pane workers) and ``grok`` (existing
``launch_worker`` Jobs plane; provider=grok). After wait, grok execute
proves process exit — a terminal ``job.json`` stamp is not enough.
agy / antigravity / claude / codex / cursor / kimi / omc remain refused.
Grok execute is **not** ``live_verified``. No PoC, tmux, MCP, or catalog
bump. Never writes ``passes`` / ``verified``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
    require_integer,
    require_object,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.evidence import CLI_WRITER
from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.ownership import ProcessIdentity
from omg_cli.jobs.runtime import (
    _read_spawn_identity_recovery,
    absorb_live_job_identities,
    cancel_job,
    job_status,
    prove_job_processes_gone,
    reap_captured_identities,
    wait_job,
)
from omg_cli.state import _safe_run_id
from omg_cli.team.api import _read_task
from omg_cli.team.compositions.lane_protocol import (
    CompositionLaneProtocolError,
    claim_composition_lane_v1,
    redact_claim_token,
    submit_composition_lane_result_v1,
)
from omg_cli.team.compositions.task_driver import (
    MAX_INLINE_JSON_BYTES,
    SOURCE_KIND_HYPERPLAN,
    SOURCE_KIND_SECURITY_RESEARCH,
    CompositionTaskAdapter,
    CompositionTaskDriverError,
    _require_leader_only_env,
    _require_live_run,
    _require_matching_team_id,
    _require_team_plane,
    _worker_names_from_control_plane,
    collect_composition_tasks_v1,
    parse_lane_task_result_v1,
    resolve_composition_batch_binding_v1,
)
from omg_cli.team.launch import (
    WORKER_TOPOLOGY_JOB,
    WorkerLaunchError,
    launch_worker,
)
from omg_cli.team.plane import (
    TEAM_ID_ENV,
    TEAM_LEADER_ROOT_ENV,
    TEAM_OWNER_TOKEN_ENV,
    TEAM_RUN_ID_ENV,
    TEAM_STATE_ROOT_ENV,
    TEAM_WORKER_ENV,
    TEAM_WORKER_ID_ENV,
    team_dir,
)

COMPOSITION_EXECUTION_KIND = "omg.team.composition_execution_v1"
COMPOSITION_EXECUTION_SCHEMA_VERSION = 1
FIXTURE_EXECUTOR = "fixture"
GROK_EXECUTOR = "grok"
HYPERPLAN_EXECUTION_FILENAME = "hyperplan-v1-execution.json"
SECURITY_RESEARCH_EXECUTION_FILENAME = "security-research-v1-execution.json"
GROK_JOB_WAIT_FALLBACK_S = 3600.0
_SUPPORTED_EXECUTORS = (FIXTURE_EXECUTOR, GROK_EXECUTOR)
_SUPPORTED_EXECUTORS_TEXT = "fixture, grok"

_REFUSED_LIVE_EXECUTORS = frozenset(
    {
        "agy",
        "antigravity",
        "claude",
        "codex",
        "cursor",
        "cursor-agent",
        "gemini",
        "kimi",
        "omc",
        "omx",
    }
)

_EXECUTION_REQUIRED = frozenset(
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
        "executor",
        "execution_supported",
        "worker_evidence",
        "lane_result_digests",
        "collected_digest",
        "limitations",
        "writer",
        "digest",
    }
)
_EVIDENCE_REQUIRED = frozenset(
    {
        "lane_id",
        "task_id",
        "worker_id",
        "run_id",
        "pane_id",
        "result_digest",
        "claim_digest",
    }
)
_LANE_DIGEST_REQUIRED = frozenset({"lane_id", "digest"})
_EVIDENCE_OPTIONAL = frozenset({"job_id"})
_FIXTURE_LIMITATIONS = (
    "executor=fixture",
    "no_live_providers",
    "no_poc_execution",
    "compile_execution_supported=false",
)
_GROK_LIMITATIONS = (
    "executor=grok",
    "jobs_plane_headless",
    "no_agy_claude_codex_cursor_kimi_omc",
    "no_poc_execution",
    "compile_execution_supported=false",
    "not_live_verified",
)
_SOURCE_FILENAMES = {
    SOURCE_KIND_HYPERPLAN: HYPERPLAN_EXECUTION_FILENAME,
    SOURCE_KIND_SECURITY_RESEARCH: SECURITY_RESEARCH_EXECUTION_FILENAME,
}


class CompositionExecutionError(ValueError):
    """Fail-closed composition execution error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "E_TEAM_COMPOSITION_EXEC",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _wrap_driver(exc: CompositionTaskDriverError) -> CompositionExecutionError:
    return CompositionExecutionError(
        str(exc),
        code=exc.code,
        details=getattr(exc, "details", None),
    )


def _wrap_lane(exc: CompositionLaneProtocolError) -> CompositionExecutionError:
    return CompositionExecutionError(
        str(exc),
        code=exc.code,
        details=getattr(exc, "details", None),
    )


def require_composition_executor(executor: Any) -> str:
    """Fail closed unless *executor* is ``fixture`` or ``grok``."""
    if not isinstance(executor, str) or not executor.strip():
        raise CompositionExecutionError(
            f"executor is required (supported: {_SUPPORTED_EXECUTORS_TEXT})",
            code="E_TEAM_COMPOSITION_EXEC_EXECUTOR",
        )
    norm = executor.strip().lower()
    if norm == FIXTURE_EXECUTOR:
        return FIXTURE_EXECUTOR
    if norm == GROK_EXECUTOR:
        return GROK_EXECUTOR
    if norm in _REFUSED_LIVE_EXECUTORS:
        raise CompositionExecutionError(
            f"auto-execution executor {executor!r} refused "
            f"(supported: fixture|grok; {norm} live workers remain open under #69)",
            code="E_TEAM_COMPOSITION_EXEC_EXECUTOR",
            details={
                "executor": norm,
                "supported": list(_SUPPORTED_EXECUTORS),
            },
        )
    raise CompositionExecutionError(
        f"unsupported composition executor {executor!r} "
        f"(supported: {_SUPPORTED_EXECUTORS_TEXT})",
        code="E_TEAM_COMPOSITION_EXEC_EXECUTOR",
        details={"executor": norm, "supported": list(_SUPPORTED_EXECUTORS)},
    )


def require_fixture_executor(executor: Any) -> str:
    """Back-compat alias for :func:`require_composition_executor`."""
    return require_composition_executor(executor)


def _limitations_for(executor: str) -> tuple[str, ...]:
    if executor == GROK_EXECUTOR:
        return _GROK_LIMITATIONS
    return _FIXTURE_LIMITATIONS


def fixture_pane_id(worker_id: str) -> str:
    """Deterministic fixture pane identity (not a tmux ``%N`` pane)."""
    try:
        wid = require_safe_id(worker_id, label="worker_id")
        return require_safe_id(f"fx-{wid}", label="pane_id")
    except ContractValidationError as exc:
        raise CompositionExecutionError(
            str(exc),
            code="E_TEAM_COMPOSITION_EXEC_PANE",
        ) from exc


def grok_job_pane_id(job_id: str) -> str:
    """Deterministic grok Jobs pane identity (not a tmux ``%N`` pane)."""
    try:
        jid = require_safe_id(job_id, label="job_id")
        return require_safe_id(f"job-{jid}", label="pane_id")
    except ContractValidationError as exc:
        raise CompositionExecutionError(
            str(exc),
            code="E_TEAM_COMPOSITION_EXEC_PANE",
        ) from exc


def composition_execution_path(
    root: Path | str, run_id: str, source_kind: str
) -> Path:
    try:
        kind = require_safe_id(source_kind, label="source_kind")
    except ContractValidationError as exc:
        raise CompositionExecutionError(
            str(exc),
            code="E_TEAM_COMPOSITION_EXEC_INVALID",
        ) from exc
    filename = _SOURCE_FILENAMES.get(kind)
    if filename is None:
        raise CompositionExecutionError(
            f"unsupported composition source_kind {kind!r}",
            code="E_TEAM_COMPOSITION_EXEC_INVALID",
        )
    rid = _safe_run_id(run_id)
    return team_dir(root, rid) / "compositions" / filename


def _digest_core(body: Mapping[str, Any]) -> str:
    core = {k: v for k, v in body.items() if k != "digest"}
    return sha256_hex(canonical_json_bytes(core))


def _parse_worker_evidence_row(
    raw: Any,
    *,
    index: int,
    expected_run_id: str,
    executor: str = FIXTURE_EXECUTOR,
) -> dict[str, Any]:
    try:
        body = require_object(raw, label=f"worker_evidence[{index}]")
        require_exact_keys(
            body,
            required=_EVIDENCE_REQUIRED,
            optional=_EVIDENCE_OPTIONAL,
            label=f"worker_evidence[{index}]",
        )
        lane_id = require_safe_id(body.get("lane_id"), label="lane_id")
        task_id = require_safe_id(body.get("task_id"), label="task_id")
        worker_id = require_safe_id(body.get("worker_id"), label="worker_id")
        run_id = require_safe_id(body.get("run_id"), label="run_id")
        pane_id = require_safe_id(body.get("pane_id"), label="pane_id")
        result_digest = require_sha256(
            body.get("result_digest"), label="result_digest"
        )
        claim_digest = require_sha256(body.get("claim_digest"), label="claim_digest")
    except ContractValidationError as exc:
        raise CompositionExecutionError(
            str(exc),
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
        ) from exc
    if run_id != expected_run_id:
        raise CompositionExecutionError(
            "worker_evidence run_id must match document run_id",
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
        )
    job_id: str | None = None
    if executor == GROK_EXECUTOR:
        raw_job = body.get("job_id")
        if raw_job is None or (isinstance(raw_job, str) and not raw_job.strip()):
            raise CompositionExecutionError(
                "grok worker_evidence requires job_id",
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
            )
        try:
            job_id = require_safe_id(raw_job, label="job_id")
        except ContractValidationError as exc:
            raise CompositionExecutionError(
                str(exc),
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
            ) from exc
        expected_pane = grok_job_pane_id(job_id)
        if pane_id != expected_pane:
            raise CompositionExecutionError(
                "grok pane_id must equal job-{job_id}",
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
                details={"pane_id": pane_id, "expected": expected_pane},
            )
    else:
        if "job_id" in body:
            raise CompositionExecutionError(
                "fixture worker_evidence must not include job_id",
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
            )
        expected_pane = fixture_pane_id(worker_id)
        if pane_id != expected_pane:
            raise CompositionExecutionError(
                "fixture pane_id must equal fx-{worker_id}",
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
                details={"pane_id": pane_id, "expected": expected_pane},
            )
    row = {
        "lane_id": lane_id,
        "task_id": task_id,
        "worker_id": worker_id,
        "run_id": run_id,
        "pane_id": pane_id,
        "result_digest": result_digest,
        "claim_digest": claim_digest,
    }
    if job_id is not None:
        row["job_id"] = job_id
    return row


def parse_composition_execution_v1(raw: Any) -> dict[str, Any]:
    """Exact-key ``CompositionExecutionV1`` parser (fail closed).

    ``execution_supported=true`` is accepted only with complete worker
    evidence. A forged ``{execution_supported: true}`` object is refused.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CompositionExecutionError(
                "composition execution document is not valid UTF-8",
                code="E_TEAM_COMPOSITION_EXEC_PARSE",
            ) from exc
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_INLINE_JSON_BYTES:
            raise CompositionExecutionError(
                "composition execution JSON exceeds inline budget",
                code="E_TEAM_COMPOSITION_EXEC_PARSE",
            )
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CompositionExecutionError(
                f"composition execution is not valid JSON: {exc}",
                code="E_TEAM_COMPOSITION_EXEC_PARSE",
            ) from exc
    try:
        body = require_object(raw, label="CompositionExecutionV1")
        require_exact_keys(
            body,
            required=_EXECUTION_REQUIRED,
            optional=frozenset(),
            label="CompositionExecutionV1",
        )
        if body.get("kind") != COMPOSITION_EXECUTION_KIND:
            raise ContractValidationError("kind mismatch")
        schema_version = require_integer(
            body.get("schema_version"), label="schema_version", minimum=1
        )
        if schema_version != COMPOSITION_EXECUTION_SCHEMA_VERSION:
            raise ContractValidationError("schema_version must be 1")
        source_kind = require_safe_id(body.get("source_kind"), label="source_kind")
        if source_kind not in _SOURCE_FILENAMES:
            raise ContractValidationError("unsupported source_kind")
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
        collected_digest = require_sha256(
            body.get("collected_digest"), label="collected_digest"
        )
        digest = require_sha256(body.get("digest"), label="digest")
    except ContractValidationError as exc:
        raise CompositionExecutionError(
            str(exc),
            code="E_TEAM_COMPOSITION_EXEC_PARSE",
        ) from exc

    if body.get("writer") != CLI_WRITER:
        raise CompositionExecutionError(
            "foreign writer refused on composition execution document",
            code="E_TEAM_COMPOSITION_EXEC_WRITER",
        )
    try:
        executor = require_composition_executor(body.get("executor"))
    except CompositionExecutionError:
        raise CompositionExecutionError(
            "composition execution document executor must be fixture or grok",
            code="E_TEAM_COMPOSITION_EXEC_EXECUTOR",
        )

    if body.get("execution_supported") is not True:
        raise CompositionExecutionError(
            "execution_supported=true requires worker evidence "
            "(forged or missing evidence refused)",
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
        )

    evidence_raw = body.get("worker_evidence")
    if not isinstance(evidence_raw, list) or not evidence_raw:
        raise CompositionExecutionError(
            "execution_supported=true requires non-empty worker_evidence",
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
        )
    evidence: list[dict[str, Any]] = []
    seen_lanes: set[str] = set()
    for idx, row in enumerate(evidence_raw):
        parsed_row = _parse_worker_evidence_row(
            row, index=idx, expected_run_id=run_id, executor=executor
        )
        lid = parsed_row["lane_id"]
        if lid in seen_lanes:
            raise CompositionExecutionError(
                f"duplicate worker_evidence lane_id {lid!r}",
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
            )
        seen_lanes.add(lid)
        evidence.append(parsed_row)

    digest_raw = body.get("lane_result_digests")
    if not isinstance(digest_raw, list) or not digest_raw:
        raise CompositionExecutionError(
            "execution_supported=true requires lane_result_digests",
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
        )
    lane_digests: list[dict[str, str]] = []
    digest_lanes: set[str] = set()
    for idx, row in enumerate(digest_raw):
        try:
            item = require_object(row, label=f"lane_result_digests[{idx}]")
            require_exact_keys(
                item,
                required=_LANE_DIGEST_REQUIRED,
                optional=frozenset(),
                label=f"lane_result_digests[{idx}]",
            )
            lid = require_safe_id(item.get("lane_id"), label="lane_id")
            digest_val = require_sha256(item.get("digest"), label="digest")
        except ContractValidationError as exc:
            raise CompositionExecutionError(
                str(exc),
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
            ) from exc
        if lid in digest_lanes:
            raise CompositionExecutionError(
                f"duplicate lane_result_digests lane_id {lid!r}",
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
            )
        digest_lanes.add(lid)
        lane_digests.append({"lane_id": lid, "digest": digest_val})

    if seen_lanes != digest_lanes:
        raise CompositionExecutionError(
            "worker_evidence lanes must equal lane_result_digests lanes",
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
        )
    by_evidence = {row["lane_id"]: row["result_digest"] for row in evidence}
    for item in lane_digests:
        if by_evidence[item["lane_id"]] != item["digest"]:
            raise CompositionExecutionError(
                f"lane_result_digest mismatch for {item['lane_id']!r}",
                code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
            )

    expected_limitations = list(_limitations_for(executor))
    limitations = body.get("limitations")
    if not isinstance(limitations, list) or [
        str(x) for x in limitations
    ] != expected_limitations:
        raise CompositionExecutionError(
            "composition execution limitations mismatch",
            code="E_TEAM_COMPOSITION_EXEC_PARSE",
        )

    out: dict[str, Any] = {
        "kind": COMPOSITION_EXECUTION_KIND,
        "schema_version": COMPOSITION_EXECUTION_SCHEMA_VERSION,
        "source_kind": source_kind,
        "run_id": run_id,
        "team_id": team_id,
        "composition_id": composition_id,
        "composition_digest": composition_digest,
        "batch_id": batch_id,
        "batch_digest": batch_digest,
        "executor": executor,
        "execution_supported": True,
        "worker_evidence": sorted(evidence, key=lambda r: r["lane_id"]),
        "lane_result_digests": sorted(lane_digests, key=lambda r: r["lane_id"]),
        "collected_digest": collected_digest,
        "limitations": expected_limitations,
        "writer": CLI_WRITER,
    }
    expected = _digest_core(out)
    if digest != expected:
        raise CompositionExecutionError(
            "composition execution digest mismatch",
            code="E_TEAM_COMPOSITION_EXEC_DIGEST",
        )
    out["digest"] = digest
    size = len(canonical_json_bytes(out))
    if size > MAX_INLINE_JSON_BYTES:
        raise CompositionExecutionError(
            f"composition execution exceeds inline budget ({size})",
            code="E_TEAM_COMPOSITION_EXEC_PARSE",
        )
    return out


def compile_composition_execution_v1(
    *,
    source_kind: str,
    run_id: str,
    team_id: str,
    composition_id: str,
    composition_digest: str,
    batch_id: str,
    batch_digest: str,
    worker_evidence: Sequence[Mapping[str, Any]],
    collected_digest: str,
    executor: str = FIXTURE_EXECUTOR,
) -> dict[str, Any]:
    """Pure: worker evidence → ``CompositionExecutionV1``.

    Stamps ``execution_supported=true`` only when evidence is complete.
    Never accepts a caller-supplied ``execution_supported`` flag.
    """
    resolved_executor = require_composition_executor(executor)
    try:
        sk = require_safe_id(source_kind, label="source_kind")
        rid = require_safe_id(run_id, label="run_id")
        tid = require_safe_id(team_id, label="team_id")
        cid = require_safe_id(composition_id, label="composition_id")
        cd = require_sha256(composition_digest, label="composition_digest")
        bid = require_safe_id(batch_id, label="batch_id")
        bd = require_sha256(batch_digest, label="batch_digest")
        collected = require_sha256(collected_digest, label="collected_digest")
    except ContractValidationError as exc:
        raise CompositionExecutionError(
            str(exc),
            code="E_TEAM_COMPOSITION_EXEC_INVALID",
        ) from exc
    if sk not in _SOURCE_FILENAMES:
        raise CompositionExecutionError(
            f"unsupported composition source_kind {sk!r}",
            code="E_TEAM_COMPOSITION_EXEC_INVALID",
        )
    if not isinstance(worker_evidence, Sequence) or isinstance(
        worker_evidence, (str, bytes)
    ):
        raise CompositionExecutionError(
            "worker_evidence must be a non-empty array",
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
        )
    rows = [
        _parse_worker_evidence_row(
            row, index=idx, expected_run_id=rid, executor=resolved_executor
        )
        for idx, row in enumerate(worker_evidence)
    ]
    if not rows:
        raise CompositionExecutionError(
            "execution_supported=true requires non-empty worker_evidence",
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
        )
    lane_digests = [
        {"lane_id": row["lane_id"], "digest": row["result_digest"]} for row in rows
    ]
    draft = {
        "kind": COMPOSITION_EXECUTION_KIND,
        "schema_version": COMPOSITION_EXECUTION_SCHEMA_VERSION,
        "source_kind": sk,
        "run_id": rid,
        "team_id": tid,
        "composition_id": cid,
        "composition_digest": cd,
        "batch_id": bid,
        "batch_digest": bd,
        "executor": resolved_executor,
        "execution_supported": True,
        "worker_evidence": sorted(rows, key=lambda r: r["lane_id"]),
        "lane_result_digests": sorted(lane_digests, key=lambda r: r["lane_id"]),
        "collected_digest": collected,
        "limitations": list(_limitations_for(resolved_executor)),
        "writer": CLI_WRITER,
    }
    draft["digest"] = _digest_core(draft)
    return parse_composition_execution_v1(draft)


def _wrap_bundle(exc: BaseException) -> CompositionExecutionError:
    code = getattr(exc, "code", None) or "E_TEAM_COMPOSITION_EXEC_BUNDLE"
    details = getattr(exc, "details", None)
    return CompositionExecutionError(
        str(exc),
        code=str(code),
        details=details if isinstance(details, Mapping) else None,
    )


def _lane_results_from_bundle(
    bundle: Mapping[str, Any] | Any,
    *,
    adapter: CompositionTaskAdapter,
    manifest: Mapping[str, Any],
    expected_lanes: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Parse ``--input`` as the composition ResultBundleV1, then LaneTaskResultV1.

    Uses the same exact-key / foreign-writer / digest / artifact_kind contract
    as produce-decision / produce-report so a forged bundle cannot be
    stripped down and reauthored into CLI-trusted lane submissions.
    """
    try:
        normalized = adapter.normalize_result_bundle(bundle, manifest=manifest)
    except CompositionExecutionError:
        raise
    except ValueError as exc:
        raise _wrap_bundle(exc) from exc
    receipts = normalized.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        raise CompositionExecutionError(
            "result bundle receipts must be a non-empty array",
            code="E_TEAM_COMPOSITION_EXEC_BUNDLE",
        )
    by_lane: dict[str, dict[str, Any]] = {}
    for idx, receipt in enumerate(receipts):
        if not isinstance(receipt, Mapping):
            raise CompositionExecutionError(
                f"receipts[{idx}] must be an object",
                code="E_TEAM_COMPOSITION_EXEC_BUNDLE",
            )
        try:
            lane_id = require_safe_id(receipt.get("lane_id"), label="lane_id")
        except ContractValidationError as exc:
            raise CompositionExecutionError(
                str(exc),
                code="E_TEAM_COMPOSITION_EXEC_BUNDLE",
            ) from exc
        if lane_id in by_lane:
            raise CompositionExecutionError(
                f"duplicate result bundle lane_id {lane_id!r}",
                code="E_TEAM_COMPOSITION_EXEC_BUNDLE",
            )
        payload = {
            "schema_version": 1,
            "status": receipt.get("status"),
            "payload": receipt.get("payload"),
        }
        if "reason" in receipt:
            payload["reason"] = receipt["reason"]
        try:
            by_lane[lane_id] = parse_lane_task_result_v1(payload)
        except CompositionTaskDriverError as exc:
            raise _wrap_driver(exc) from exc
    expected = list(expected_lanes)
    if set(by_lane) != set(expected):
        raise CompositionExecutionError(
            "result bundle lane coverage mismatch",
            code="E_TEAM_COMPOSITION_EXEC_BUNDLE",
            details={"expected": sorted(expected), "actual": sorted(by_lane)},
        )
    return by_lane


def _assert_existing_matches_admitted(
    existing: Mapping[str, Any],
    *,
    topo_order: Sequence[str],
    mapping: Mapping[str, str],
) -> None:
    """Refuse truncated/fabricated execution artifacts on the idempotent path.

    ``parse_composition_execution_v1`` only requires a nonempty self-consistent
    evidence set. Idempotent execute must still bind that evidence to the
    admitted batch (``topo_order`` + lane→task mapping) before returning
    ``execution_supported=true``.
    """
    expected = [str(x) for x in topo_order]
    evidence = list(existing.get("worker_evidence") or [])
    evidence_lanes = [str(row.get("lane_id")) for row in evidence]
    if sorted(evidence_lanes) != sorted(expected):
        raise CompositionExecutionError(
            "existing composition execution lanes do not match admitted topo_order",
            code="E_TEAM_COMPOSITION_EXEC_CONFLICT",
            details={"stored": sorted(evidence_lanes), "admitted": sorted(expected)},
        )
    for row in evidence:
        lane_id = str(row.get("lane_id"))
        expected_task = mapping.get(lane_id)
        if expected_task is None or str(row.get("task_id")) != str(expected_task):
            raise CompositionExecutionError(
                f"existing composition execution task_id mismatch for {lane_id!r}",
                code="E_TEAM_COMPOSITION_EXEC_CONFLICT",
                details={
                    "lane_id": lane_id,
                    "stored": row.get("task_id"),
                    "admitted": expected_task,
                },
            )
    digest_lanes = [
        str(row.get("lane_id")) for row in existing.get("lane_result_digests") or []
    ]
    if sorted(digest_lanes) != sorted(expected):
        raise CompositionExecutionError(
            "existing composition execution lane_result_digests do not match "
            "admitted topo_order",
            code="E_TEAM_COMPOSITION_EXEC_CONFLICT",
            details={"stored": sorted(digest_lanes), "admitted": sorted(expected)},
        )


def _lane_result_digests_from_results(
    by_lane: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    return sorted(
        (
            {"lane_id": lane_id, "digest": _result_digest(result)}
            for lane_id, result in by_lane.items()
        ),
        key=lambda row: row["lane_id"],
    )


def _fixture_worker_env(
    *,
    root: Path,
    run_id: str,
    team_id: str,
    worker_id: str,
    owner_token: str,
) -> dict[str, str]:
    leader = str(root.resolve())
    return {
        TEAM_WORKER_ENV: "1",
        TEAM_WORKER_ID_ENV: worker_id,
        TEAM_RUN_ID_ENV: run_id,
        TEAM_ID_ENV: team_id,
        TEAM_LEADER_ROOT_ENV: leader,
        TEAM_STATE_ROOT_ENV: str((root / ".omg" / "state").resolve()),
        TEAM_OWNER_TOKEN_ENV: owner_token,
        "OMG_PROJECT_ROOT": leader,
        "OMG_EXPERIMENTAL_TMUX_TEAM": "1",
    }


def _claim_digest(claim: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(redact_claim_token(claim)))


def _result_digest(result: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(parse_lane_task_result_v1(result)))


def _run_fixture_lane_worker(
    *,
    root: Path,
    run_id: str,
    team_id: str,
    lane_id: str,
    adapter: CompositionTaskAdapter,
    result: Mapping[str, Any],
    worker_env: Mapping[str, str],
    pane_id: str,
) -> dict[str, Any]:
    """Run one fixture worker through claim-lane / submit-lane-result."""
    try:
        claimed = claim_composition_lane_v1(
            root, run_id, team_id, lane_id, adapter, env=worker_env
        )
    except CompositionLaneProtocolError as exc:
        raise _wrap_lane(exc) from exc
    claim = claimed["claim"]
    try:
        submitted = submit_composition_lane_result_v1(
            root,
            run_id,
            team_id,
            adapter,
            claim=claim,
            result=result,
            env=worker_env,
        )
    except CompositionLaneProtocolError as exc:
        raise _wrap_lane(exc) from exc
    task = _read_task(root, run_id, team_id, str(submitted["task_id"]))
    if task is None or task.get("status") != "completed":
        raise CompositionExecutionError(
            f"composition worker did not complete lane {lane_id!r}",
            code="E_TEAM_COMPOSITION_EXEC_WORKER",
        )
    try:
        stored = parse_lane_task_result_v1(task.get("result"))
    except CompositionTaskDriverError as exc:
        raise _wrap_driver(exc) from exc
    digest = _result_digest(stored)
    return {
        "lane_id": str(submitted["lane_id"]),
        "task_id": str(submitted["task_id"]),
        "worker_id": str(claim["worker_id"]),
        "run_id": run_id,
        "pane_id": pane_id,
        "result_digest": digest,
        "claim_digest": _claim_digest(claim),
    }


def _try_load_existing_execution(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise CompositionExecutionError(
            "composition execution artifact may not be a symlink",
            code="E_TEAM_COMPOSITION_EXEC_PATH",
        )
    if not path.exists():
        return None
    try:
        body = read_managed_regular_bytes(path, max_bytes=MAX_INLINE_JSON_BYTES)
    except FileNotFoundError:
        return None
    except ContractPathError as exc:
        raise CompositionExecutionError(
            f"composition execution artifact unreadable: {exc}",
            code="E_TEAM_COMPOSITION_EXEC_PATH",
        ) from exc
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositionExecutionError(
            "composition execution artifact corrupt JSON",
            code="E_TEAM_COMPOSITION_EXEC_CORRUPT",
        ) from exc
    return parse_composition_execution_v1(parsed)


def _assert_tasks_pending_for_execute(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    mapping: Mapping[str, str],
    topo_order: Sequence[str],
) -> None:
    """Refuse unless every mapped lane is still pending and claim-free.

    Partial fixture execute (some lanes completed, no execution artifact) is
    fail-closed: retry must not silently resume mixed state. Operator repair
    of the task board is required. Live crash-resume remains #69 follow-up.
    """
    for lane_id in topo_order:
        task_id = mapping[str(lane_id)]
        task = _read_task(root, run_id, team_id, task_id)
        if task is None:
            raise CompositionExecutionError(
                f"mapped task {task_id!r} missing for lane {lane_id!r}",
                code="E_TEAM_COMPOSITION_EXEC_BATCH",
            )
        status = task.get("status")
        if status != "pending":
            raise CompositionExecutionError(
                f"composition execution requires pending lanes "
                f"(lane {lane_id!r} status={status!r})",
                code="E_TEAM_COMPOSITION_EXEC_STATE",
                details={"lane_id": lane_id, "status": status},
            )
        if task.get("claim") is not None or task.get("result") is not None:
            raise CompositionExecutionError(
                f"composition execution requires claim-free empty results "
                f"(lane {lane_id!r})",
                code="E_TEAM_COMPOSITION_EXEC_STATE",
                details={"lane_id": lane_id},
            )


def _wrap_job(exc: BaseException) -> CompositionExecutionError:
    code = getattr(exc, "code", None) or "E_TEAM_COMPOSITION_EXEC_JOB"
    details = getattr(exc, "details", None)
    return CompositionExecutionError(
        str(exc),
        code=str(code),
        details=details if isinstance(details, Mapping) else None,
    )


def _cancel_composition_job(
    root: Path,
    job_id: str,
    *,
    reason: str,
    identities: Sequence[Any] = (),
) -> None:
    """Cancel a grok composition job. Do not swallow ``JobStoreError``.

    ``cancel_job`` does not signal PIDs from a possibly forged terminal
    ``job.json`` stamp. Independently captured identities are reaped
    only when prove-after-cancel still sees LIVE processes.
    """
    try:
        cancel_job(root, job_id, reason=reason)
    except JobStoreError as exc:
        try:
            reap_captured_identities(identities)
        except JobStoreError:
            pass
        raise CompositionExecutionError(
            f"grok composition job {reason} and cancel failed: {exc}",
            code="E_TEAM_COMPOSITION_EXEC_JOB",
            details={"job_id": job_id},
        ) from exc
    if identities:
        try:
            prove_job_processes_gone(
                root, job_id, extra_identities=identities, timeout_s=0.05
            )
        except JobStoreError:
            reap_captured_identities(identities)


def _grok_job_wait_s(record: Any) -> float:
    """Wait at most the job's configured provider timeout (default 3600s)."""
    raw = None
    request = getattr(record, "request", None)
    if isinstance(request, Mapping):
        raw = request.get("timeout_s")
    if raw is None:
        worker = getattr(record, "worker", None)
        if isinstance(worker, Mapping):
            raw = worker.get("timeout_s")
    try:
        timeout = float(raw) if raw is not None else GROK_JOB_WAIT_FALLBACK_S
    except (TypeError, ValueError):
        timeout = GROK_JOB_WAIT_FALLBACK_S
    if timeout <= 0:
        return GROK_JOB_WAIT_FALLBACK_S
    return timeout


def _launch_and_wait_grok_job(
    *,
    root: Path,
    run_id: str,
    team_id: str,
    worker_id: str,
    task_id: str,
    source_kind: str,
    composition_id: str,
    lanes: Sequence[str],
) -> tuple[str, str]:
    """Launch one grok worker via existing ``launch_worker`` Jobs machinery.

    Returns ``(job_id, pane_id)``. Does not claim lanes. Never shells
    agy/claude/codex. Not ``live_verified``. A terminal ``job.json`` stamp
    is not process-exit proof; identities from ``job_status`` plus OS
    children of a still-live runner are proven gone after wait.
    """
    prompt = (
        "omg team composition execute executor=grok "
        f"source_kind={source_kind} run_id={run_id} team_id={team_id} "
        f"composition_id={composition_id} lanes={','.join(lanes)}"
    )
    try:
        handle = launch_worker(
            root,
            worker_id=worker_id,
            topology=WORKER_TOPOLOGY_JOB,
            provider=GROK_EXECUTOR,
            role="executor",
            run_id=run_id,
            team_id=team_id,
            task_id=task_id,
            prompt_text=prompt,
            dry_run=False,
            executor=GROK_EXECUTOR,
            cwd=root,
        )
    except (WorkerLaunchError, JobStoreError) as exc:
        raise _wrap_job(exc) from exc
    job_id = handle.job_id
    if not isinstance(job_id, str) or not job_id.strip():
        raise CompositionExecutionError(
            "grok composition launch produced no job_id",
            code="E_TEAM_COMPOSITION_EXEC_JOB",
        )
    if handle.provider != GROK_EXECUTOR:
        raise CompositionExecutionError(
            f"grok composition launch provider mismatch ({handle.provider!r})",
            code="E_TEAM_COMPOSITION_EXEC_JOB",
            details={"provider": handle.provider},
        )
    start_identities: tuple[Any, ...] = ()
    captured: dict[int, ProcessIdentity] = {}
    runner_pids: set[int] = set()
    timed_out = False
    record: Any = None
    try:
        spawn = _read_spawn_identity_recovery(root, job_id)
        if spawn is not None:
            captured[spawn.pid] = spawn
        runner = spawn
        start_identities = tuple(captured.values())
        runner_pids = {runner.pid} if runner is not None else set()
        started = job_status(root, job_id)

        def _sync_start_identities() -> None:
            nonlocal start_identities
            start_identities = tuple(captured.values())

        def _on_poll(_wait_record: object) -> None:
            del _wait_record
            absorb_live_job_identities(captured)
            _sync_start_identities()

        deadline = time.monotonic() + 0.5
        while True:
            absorb_live_job_identities(captured)
            _sync_start_identities()
            if (set(captured) - runner_pids) or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        wait_s = _grok_job_wait_s(started)
        try:
            record, timed_out = wait_job(
                root,
                job_id,
                timeout_s=wait_s,
                poll_s=0.02,
                stop_on_recovery_required=True,
                on_poll=_on_poll,
            )
        finally:
            absorb_live_job_identities(captured)
            _sync_start_identities()
    except JobStoreError as exc:
        _cancel_composition_job(
            root,
            job_id,
            reason="composition-grok-wait-error",
            identities=start_identities,
        )
        try:
            prove_job_processes_gone(
                root, job_id, extra_identities=start_identities
            )
        except JobStoreError as prove_exc:
            raise CompositionExecutionError(
                f"grok composition job {job_id} wait failed and cancel "
                f"unproven: {prove_exc}",
                code="E_TEAM_COMPOSITION_EXEC_JOB",
                details={"job_id": job_id},
            ) from prove_exc
        raise CompositionExecutionError(
            f"grok composition job {job_id} wait failed: {exc}",
            code="E_TEAM_COMPOSITION_EXEC_JOB",
            details={"job_id": job_id},
        ) from exc
    if timed_out:
        _cancel_composition_job(
            root,
            job_id,
            reason="wait-timeout",
            identities=start_identities,
        )
        try:
            prove_job_processes_gone(
                root, job_id, extra_identities=start_identities
            )
        except JobStoreError as exc:
            raise CompositionExecutionError(
                f"grok composition job {job_id} timed out and cancel "
                f"unproven: {exc}",
                code="E_TEAM_COMPOSITION_EXEC_JOB",
                details={"job_id": job_id},
            ) from exc
        raise CompositionExecutionError(
            f"grok composition job {job_id} timed out",
            code="E_TEAM_COMPOSITION_EXEC_JOB",
            details={"job_id": job_id},
        )
    # Terminal job.json is not process-exit proof. A forged SUCCEEDED stamp
    # used to skip cancel while grok was still live. Inner grok is a new
    # session (not in the runner pgid) and is missing from launch; capture
    # it from OS children of the still-live runner during wait. If the
    # runner died before that snapshot, identities can look gone while a
    # detached grok remains — fail closed like simplify.
    if not captured:
        try:
            cancel_job(root, job_id, reason="composition-grok-missing-runner")
        except JobStoreError:
            pass
        raise CompositionExecutionError(
            "grok composition runner identity was never captured "
            "before terminal state",
            code="E_TEAM_COMPOSITION_EXEC_JOB",
            details={"job_id": job_id},
        )
    try:
        prove_job_processes_gone(
            root, job_id, extra_identities=start_identities
        )
    except JobStoreError as exc:
        _cancel_composition_job(
            root,
            job_id,
            reason="composition-grok-terminal-live",
            identities=start_identities,
        )
        try:
            prove_job_processes_gone(
                root, job_id, extra_identities=start_identities
            )
        except JobStoreError as prove_exc:
            try:
                reap_captured_identities(start_identities)
                prove_job_processes_gone(
                    root, job_id, extra_identities=start_identities
                )
            except JobStoreError as reap_exc:
                raise CompositionExecutionError(
                    f"grok composition job {job_id} terminal but process "
                    f"still live: {reap_exc}",
                    code="E_TEAM_COMPOSITION_EXEC_JOB",
                    details={"job_id": job_id},
                ) from reap_exc
            raise CompositionExecutionError(
                f"grok composition job {job_id} claimed terminal while "
                f"process was live: {prove_exc}",
                code="E_TEAM_COMPOSITION_EXEC_JOB",
                details={"job_id": job_id},
            ) from prove_exc
        raise CompositionExecutionError(
            f"grok composition job {job_id} claimed terminal while "
            f"process was live: {exc}",
            code="E_TEAM_COMPOSITION_EXEC_JOB",
            details={"job_id": job_id},
        ) from exc
    if record is None or record.state != JobState.SUCCEEDED:
        state_value = getattr(getattr(record, "state", None), "value", None)
        raise CompositionExecutionError(
            f"grok composition job {job_id} did not succeed "
            f"(state={state_value})",
            code="E_TEAM_COMPOSITION_EXEC_JOB",
            details={"job_id": job_id, "state": state_value},
        )
    return job_id, grok_job_pane_id(job_id)


def _collected_bundle_digest(collected: Mapping[str, Any]) -> str:
    bundle = collected.get("bundle")
    if isinstance(bundle, Mapping) and bundle.get("digest"):
        try:
            return require_sha256(bundle.get("digest"), label="collected.bundle.digest")
        except ContractValidationError as exc:
            raise CompositionExecutionError(
                str(exc),
                code="E_TEAM_COMPOSITION_EXEC_COLLECT",
            ) from exc
    # Fall back to canonical digest of the produced bundle object.
    if isinstance(bundle, Mapping):
        return sha256_hex(canonical_json_bytes(dict(bundle)))
    raise CompositionExecutionError(
        "collect did not return a result bundle",
        code="E_TEAM_COMPOSITION_EXEC_COLLECT",
    )


def execute_composition_tasks_v1(
    root: Path | str,
    run_id: str,
    team_id: str,
    adapter: CompositionTaskAdapter,
    *,
    executor: str,
    bundle: Mapping[str, Any] | Any,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Leader-only: workers claim/submit → collect → execution evidence.

    ``executor=fixture`` uses in-process pane workers. ``executor=grok``
    launches grok through existing ``launch_worker`` Jobs machinery, waits,
    proves OS process exit (a terminal ``job.json`` stamp is not enough),
    then submits the normalized ``--input`` lane results. Does **not**
    flip compile/produce ``execution_supported``. Does not launch
    agy/claude/codex/cursor. Never writes ``passes`` / ``verified``.
    Not ``live_verified``.
    """
    resolved_executor = require_composition_executor(executor)
    try:
        _require_leader_only_env(env)
    except CompositionTaskDriverError as exc:
        raise _wrap_driver(exc) from exc
    root_path = Path(root).resolve()
    rid = _safe_run_id(run_id)
    try:
        tid = require_safe_id(team_id, label="team_id")
    except ContractValidationError as exc:
        raise CompositionExecutionError(
            str(exc),
            code="E_TEAM_COMPOSITION_EXEC_INVALID",
        ) from exc

    try:
        _require_live_run(root_path, rid)
        plane = _require_team_plane(root_path, rid)
        _require_matching_team_id(tid, plane)
        worker_names = _worker_names_from_control_plane(plane)
    except CompositionTaskDriverError as exc:
        raise _wrap_driver(exc) from exc

    worker_topo = str(plane.get("worker_topology") or "pane").strip().lower()
    if (
        resolved_executor == FIXTURE_EXECUTOR
        and worker_topo == WORKER_TOPOLOGY_JOB
    ):
        raise CompositionExecutionError(
            "job-backed composition execution remains open under #69 "
            "(fixture path is pane workers only; use --executor grok)",
            code="E_TEAM_COMPOSITION_EXEC_TOPOLOGY",
            details={"worker_topology": worker_topo},
        )

    owner_token = str(plane.get("owner_token") or "").strip()
    if not owner_token:
        raise CompositionExecutionError(
            "control plane owner_token required for composition workers",
            code="E_TEAM_COMPOSITION_EXEC_TOKEN",
        )
    worker_id = worker_names[0]
    pane_id = fixture_pane_id(worker_id)
    worker_env = _fixture_worker_env(
        root=root_path,
        run_id=rid,
        team_id=tid,
        worker_id=worker_id,
        owner_token=owner_token,
    )

    exec_path = composition_execution_path(root_path, rid, adapter.source_kind)
    existing = _try_load_existing_execution(exec_path)

    try:
        binding = resolve_composition_batch_binding_v1(
            root_path, rid, tid, adapter
        )
    except CompositionTaskDriverError as exc:
        raise _wrap_driver(exc) from exc

    manifest = binding["manifest"]
    if manifest.get("execution_supported") is not False:
        raise CompositionExecutionError(
            "manifest execution_supported must remain false",
            code="E_TEAM_COMPOSITION_EXEC_MANIFEST",
        )
    compiled = binding["compiled"]
    mapping = dict(binding["task_key_to_id"])
    topo_order = [str(x) for x in compiled["topo_order"]]
    by_lane = _lane_results_from_bundle(
        bundle,
        adapter=adapter,
        manifest=manifest,
        expected_lanes=topo_order,
    )

    if existing is not None:
        expected_ids = {
            "source_kind": adapter.source_kind,
            "run_id": rid,
            "team_id": tid,
            "composition_id": manifest["composition_id"],
            "composition_digest": manifest["digest"],
            "batch_id": compiled["batch_id"],
            "batch_digest": compiled["digest"],
            "executor": resolved_executor,
        }
        for key, value in expected_ids.items():
            if existing.get(key) != value:
                raise CompositionExecutionError(
                    f"existing composition execution {key} conflict",
                    code="E_TEAM_COMPOSITION_EXEC_CONFLICT",
                )
        _assert_existing_matches_admitted(
            existing, topo_order=topo_order, mapping=mapping
        )
        incoming_digests = _lane_result_digests_from_results(by_lane)
        stored_digests = sorted(
            list(existing["lane_result_digests"]),
            key=lambda row: str(row["lane_id"]),
        )
        if stored_digests != incoming_digests:
            raise CompositionExecutionError(
                "existing composition execution lane result digest conflict",
                code="E_TEAM_COMPOSITION_EXEC_CONFLICT",
                details={
                    "stored": stored_digests,
                    "incoming": incoming_digests,
                },
            )
        return {
            "ok": True,
            "idempotent": True,
            "source_kind": adapter.source_kind,
            "composition_id": manifest["composition_id"],
            "composition_digest": manifest["digest"],
            "batch_id": compiled["batch_id"],
            "batch_digest": compiled["digest"],
            "path": _rel_under_root(root_path, exec_path),
            "execution": existing,
            "execution_supported": True,
            "manifest_execution_supported": False,
        }

    _assert_tasks_pending_for_execute(
        root_path,
        run_id=rid,
        team_id=tid,
        mapping=mapping,
        topo_order=topo_order,
    )

    grok_job_id: str | None = None
    if resolved_executor == GROK_EXECUTOR:
        if not topo_order:
            raise CompositionExecutionError(
                "grok composition execute requires a non-empty topo_order",
                code="E_TEAM_COMPOSITION_EXEC_BATCH",
            )
        grok_job_id, pane_id = _launch_and_wait_grok_job(
            root=root_path,
            run_id=rid,
            team_id=tid,
            worker_id=worker_id,
            task_id=str(mapping[topo_order[0]]),
            source_kind=adapter.source_kind,
            composition_id=str(manifest["composition_id"]),
            lanes=topo_order,
        )

    evidence: list[dict[str, Any]] = []
    for lane_id in topo_order:
        row = _run_fixture_lane_worker(
            root=root_path,
            run_id=rid,
            team_id=tid,
            lane_id=lane_id,
            adapter=adapter,
            result=by_lane[lane_id],
            worker_env=worker_env,
            pane_id=pane_id,
        )
        if grok_job_id is not None:
            row["job_id"] = grok_job_id
        evidence.append(row)

    try:
        collected = collect_composition_tasks_v1(
            root_path, rid, tid, adapter, env=env
        )
    except CompositionTaskDriverError as exc:
        raise _wrap_driver(exc) from exc
    if collected.get("execution_supported") is not False:
        raise CompositionExecutionError(
            "collect must retain execution_supported=false",
            code="E_TEAM_COMPOSITION_EXEC_COLLECT",
        )
    collected_digest = _collected_bundle_digest(collected)

    document = compile_composition_execution_v1(
        source_kind=adapter.source_kind,
        run_id=rid,
        team_id=tid,
        composition_id=str(manifest["composition_id"]),
        composition_digest=str(manifest["digest"]),
        batch_id=str(compiled["batch_id"]),
        batch_digest=str(compiled["digest"]),
        worker_evidence=evidence,
        collected_digest=collected_digest,
        executor=resolved_executor,
    )

    compositions = exec_path.parent
    compositions.mkdir(parents=True, exist_ok=True)
    if compositions.is_symlink():
        raise CompositionExecutionError(
            "compositions directory may not be a symlink",
            code="E_TEAM_COMPOSITION_EXEC_PATH",
        )
    lock = adapter.composition_lock_path(root_path, rid)
    body = canonical_json_bytes(document)
    with exclusive_lock(lock):
        raced = _try_load_existing_execution(exec_path)
        if raced is not None:
            if raced.get("digest") == document["digest"]:
                return {
                    "ok": True,
                    "idempotent": True,
                    "source_kind": adapter.source_kind,
                    "composition_id": manifest["composition_id"],
                    "composition_digest": manifest["digest"],
                    "batch_id": compiled["batch_id"],
                    "batch_digest": compiled["digest"],
                    "path": _rel_under_root(root_path, exec_path),
                    "execution": raced,
                    "collected": collected,
                    "execution_supported": True,
                    "manifest_execution_supported": False,
                }
            raise CompositionExecutionError(
                "composition execution digest conflict: refusing overwrite",
                code="E_TEAM_COMPOSITION_EXEC_CONFLICT",
            )
        try:
            atomic_write_bytes(exec_path, body, mode=DATA_FILE_MODE, replace=False)
        except FileExistsError as exc:
            raise CompositionExecutionError(
                "concurrent composition execution race refused",
                code="E_TEAM_COMPOSITION_EXEC_RACE",
            ) from exc
        except ContractPathError as exc:
            raise CompositionExecutionError(
                f"composition execution path refused: {exc}",
                code="E_TEAM_COMPOSITION_EXEC_PATH",
            ) from exc
        loaded = _try_load_existing_execution(exec_path)
        if loaded is None or loaded.get("digest") != document["digest"]:
            raise CompositionExecutionError(
                "published composition execution digest mismatch",
                code="E_TEAM_COMPOSITION_EXEC_CORRUPT",
            )

    return {
        "ok": True,
        "idempotent": False,
        "source_kind": adapter.source_kind,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "batch_id": compiled["batch_id"],
        "batch_digest": compiled["digest"],
        "path": _rel_under_root(root_path, exec_path),
        "execution": loaded,
        "collected": collected,
        "execution_supported": True,
        "manifest_execution_supported": False,
    }


def _rel_under_root(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "COMPOSITION_EXECUTION_KIND",
    "COMPOSITION_EXECUTION_SCHEMA_VERSION",
    "FIXTURE_EXECUTOR",
    "GROK_EXECUTOR",
    "HYPERPLAN_EXECUTION_FILENAME",
    "SECURITY_RESEARCH_EXECUTION_FILENAME",
    "CompositionExecutionError",
    "compile_composition_execution_v1",
    "composition_execution_path",
    "execute_composition_tasks_v1",
    "fixture_pane_id",
    "grok_job_pane_id",
    "parse_composition_execution_v1",
    "require_composition_executor",
    "require_fixture_executor",
]
