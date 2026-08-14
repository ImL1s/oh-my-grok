"""Composition Execution V1 — fixture-backed auto-worker path (#69 PR14).

Leader-only driver that runs the existing claim-lane / submit-lane-result
protocol with **fixture** workers, collects lane results, and persists
``omg.team.composition_execution_v1`` **only** after those workers ran.

``execution_supported=true`` is allowed **only** on that evidence document,
and only when worker evidence is complete (run ids, fixture pane ids, lane
result digests). Forged ``{execution_supported: true}`` without evidence is
refused. ``--input`` is a composition ``ResultBundleV1`` and is normalized
with the same exact-key / foreign-writer / digest / artifact_kind contract
as produce-decision / produce-report **before** fixture workers submit
``LaneTaskResultV1`` payloads. Compile / produce / admit / collect / claim
contracts keep ``execution_supported=false``.

This slice is fixture-only: grok / agy / antigravity / cursor (and other
live providers) auto-execution is fail-closed. No PoC, Jobs, tmux, MCP,
Antigravity, or ``live_*`` promotion. Never writes ``passes`` / ``verified``.
No catalog v5.
"""

from __future__ import annotations

import json
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
from omg_cli.team.launch import WORKER_TOPOLOGY_JOB
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
HYPERPLAN_EXECUTION_FILENAME = "hyperplan-v1-execution.json"
SECURITY_RESEARCH_EXECUTION_FILENAME = "security-research-v1-execution.json"

_REFUSED_LIVE_EXECUTORS = frozenset(
    {
        "agy",
        "antigravity",
        "claude",
        "codex",
        "cursor",
        "cursor-agent",
        "gemini",
        "grok",
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
_LIMITATIONS = (
    "executor=fixture",
    "no_live_providers",
    "no_poc_execution",
    "compile_execution_supported=false",
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


def require_fixture_executor(executor: Any) -> str:
    """Fail closed unless *executor* is exactly ``fixture``."""
    if not isinstance(executor, str) or not executor.strip():
        raise CompositionExecutionError(
            "executor is required (supported: fixture)",
            code="E_TEAM_COMPOSITION_EXEC_EXECUTOR",
        )
    norm = executor.strip().lower()
    if norm == FIXTURE_EXECUTOR:
        return FIXTURE_EXECUTOR
    if norm in _REFUSED_LIVE_EXECUTORS:
        raise CompositionExecutionError(
            f"auto-execution executor {executor!r} refused "
            f"(fixture only; {norm} live workers remain open under #69)",
            code="E_TEAM_COMPOSITION_EXEC_EXECUTOR",
            details={"executor": norm, "supported": FIXTURE_EXECUTOR},
        )
    raise CompositionExecutionError(
        f"unsupported composition executor {executor!r} (supported: fixture)",
        code="E_TEAM_COMPOSITION_EXEC_EXECUTOR",
        details={"executor": norm, "supported": FIXTURE_EXECUTOR},
    )


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
    raw: Any, *, index: int, expected_run_id: str
) -> dict[str, Any]:
    try:
        body = require_object(raw, label=f"worker_evidence[{index}]")
        require_exact_keys(
            body,
            required=_EVIDENCE_REQUIRED,
            optional=frozenset(),
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
    expected_pane = fixture_pane_id(worker_id)
    if pane_id != expected_pane:
        raise CompositionExecutionError(
            "fixture pane_id must equal fx-{worker_id}",
            code="E_TEAM_COMPOSITION_EXEC_EVIDENCE",
            details={"pane_id": pane_id, "expected": expected_pane},
        )
    return {
        "lane_id": lane_id,
        "task_id": task_id,
        "worker_id": worker_id,
        "run_id": run_id,
        "pane_id": pane_id,
        "result_digest": result_digest,
        "claim_digest": claim_digest,
    }


def parse_composition_execution_v1(raw: Any) -> dict[str, Any]:
    """Exact-key ``CompositionExecutionV1`` parser (fail closed).

    ``execution_supported=true`` is accepted only with complete fixture worker
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
        executor = require_fixture_executor(body.get("executor"))
    except CompositionExecutionError:
        raise CompositionExecutionError(
            "composition execution document executor must be fixture",
            code="E_TEAM_COMPOSITION_EXEC_EXECUTOR",
        )

    if body.get("execution_supported") is not True:
        raise CompositionExecutionError(
            "execution_supported=true requires fixture worker evidence "
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
            row, index=idx, expected_run_id=run_id
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

    limitations = body.get("limitations")
    if not isinstance(limitations, list) or [str(x) for x in limitations] != list(
        _LIMITATIONS
    ):
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
        "limitations": list(_LIMITATIONS),
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
    """Pure: fixture worker evidence → ``CompositionExecutionV1``.

    Stamps ``execution_supported=true`` only when evidence is complete.
    Never accepts a caller-supplied ``execution_supported`` flag.
    """
    require_fixture_executor(executor)
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
        _parse_worker_evidence_row(row, index=idx, expected_run_id=rid)
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
        "executor": FIXTURE_EXECUTOR,
        "execution_supported": True,
        "worker_evidence": sorted(rows, key=lambda r: r["lane_id"]),
        "lane_result_digests": sorted(lane_digests, key=lambda r: r["lane_id"]),
        "collected_digest": collected,
        "limitations": list(_LIMITATIONS),
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
            f"fixture worker did not complete lane {lane_id!r}",
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
                f"fixture execution requires pending lanes "
                f"(lane {lane_id!r} status={status!r})",
                code="E_TEAM_COMPOSITION_EXEC_STATE",
                details={"lane_id": lane_id, "status": status},
            )
        if task.get("claim") is not None or task.get("result") is not None:
            raise CompositionExecutionError(
                f"fixture execution requires claim-free empty results "
                f"(lane {lane_id!r})",
                code="E_TEAM_COMPOSITION_EXEC_STATE",
                details={"lane_id": lane_id},
            )


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
    """Leader-only: fixture-worker claim/submit → collect → execution evidence.

    Does **not** flip compile/produce ``execution_supported``. Does not launch
    grok/agy/antigravity/cursor, tmux, Jobs, MCP, or PoC surfaces. Never
    writes ``passes`` / ``verified``.
    """
    require_fixture_executor(executor)
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
    if worker_topo == WORKER_TOPOLOGY_JOB:
        raise CompositionExecutionError(
            "job-backed composition execution remains open under #69 "
            "(this slice is fixture pane workers only)",
            code="E_TEAM_COMPOSITION_EXEC_TOPOLOGY",
            details={"worker_topology": worker_topo},
        )

    owner_token = str(plane.get("owner_token") or "").strip()
    if not owner_token:
        raise CompositionExecutionError(
            "control plane owner_token required for fixture workers",
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
        }
        for key, value in expected_ids.items():
            if existing.get(key) != value:
                raise CompositionExecutionError(
                    f"existing composition execution {key} conflict",
                    code="E_TEAM_COMPOSITION_EXEC_CONFLICT",
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

    evidence: list[dict[str, Any]] = []
    for lane_id in topo_order:
        evidence.append(
            _run_fixture_lane_worker(
                root=root_path,
                run_id=rid,
                team_id=tid,
                lane_id=lane_id,
                adapter=adapter,
                result=by_lane[lane_id],
                worker_env=worker_env,
                pane_id=pane_id,
            )
        )

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
        executor=FIXTURE_EXECUTOR,
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
    "HYPERPLAN_EXECUTION_FILENAME",
    "SECURITY_RESEARCH_EXECUTION_FILENAME",
    "CompositionExecutionError",
    "compile_composition_execution_v1",
    "composition_execution_path",
    "execute_composition_tasks_v1",
    "fixture_pane_id",
    "parse_composition_execution_v1",
    "require_fixture_executor",
]
