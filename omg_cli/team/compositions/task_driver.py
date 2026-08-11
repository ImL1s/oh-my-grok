"""Shared Composition Task Driver V1 (#69 PR12).

Hermetic bridge from materialized Hyperplan / Security Research manifests to
PR11 ``TaskBatchV1`` admission and fail-closed collection into existing result
bundles. Does **not** launch workers, panes, Jobs, providers, or PoC surfaces.
``execution_supported`` remains ``false`` on both composition contracts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

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
from omg_cli.evidence import CLI_WRITER
from omg_cli.state import _safe_run_id, load_run
from omg_cli.team.api import (
    TeamApiError,
    _read_task,
    _require_control_plane,
    _task_path,
)
from omg_cli.team.task_batch import (
    TaskBatchError,
    _batch_binding,
    _load_batch_record,
    admit_task_batch_v1,
    batch_record_path,
    compile_task_batch_v1,
)

LANE_TASK_RESULT_SCHEMA_VERSION = 1
_LANE_RESULT_STATUSES = frozenset({"complete", "rejected", "blocked"})
_LANE_RESULT_REQUIRED = frozenset({"schema_version", "status", "payload"})
_LANE_RESULT_OPTIONAL = frozenset({"reason"})
_LANE_RESULT_FORBIDDEN = frozenset(
    {
        "lane_id",
        "artifact_kind",
        "digest",
        "composition_id",
        "composition_digest",
        "writer",
        "receipt",
        "receipt_digest",
        "bundle_digest",
        "source_artifact_digests",
    }
)
MAX_REASON_CHARS = 2000
MAX_INLINE_JSON_BYTES = 64 * 1024
MAX_SUBJECT_CHARS = 4000
MAX_DESCRIPTION_CHARS = 8000

SOURCE_KIND_HYPERPLAN = "hyperplan_v1"
SOURCE_KIND_SECURITY_RESEARCH = "security_research_v1"


class CompositionTaskDriverError(ValueError):
    """Fail-closed composition task driver error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "E_TEAM_COMPOSITION_TASK",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class CompositionTaskAdapter(Protocol):
    """Thin per-composition hooks for the shared driver."""

    source_kind: str
    result_bundle_kind: str
    error_ns: str

    def load_manifest(self, root: Path, run_id: str) -> dict[str, Any]: ...

    def composition_lock_path(self, root: Path, run_id: str) -> Path: ...

    def persist_from_bundle_locked(
        self,
        *,
        root: Path,
        run_id: str,
        manifest: Mapping[str, Any],
        bundle: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def raise_error(
        self,
        message: str,
        *,
        code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None: ...


def composition_batch_ids(source_kind: str, composition_id: str) -> tuple[str, str]:
    """Deterministic ``(batch_id, idempotency_key)`` from composition kind/id."""
    kind = require_safe_id(source_kind, label="source_kind")
    cid = require_safe_id(composition_id, label="composition_id")
    batch_id = f"batch-{kind}-{cid}"
    idempotency_key = f"admit-{kind}-{cid}"
    # Re-validate after concatenation (length / charset).
    return (
        require_safe_id(batch_id, label="batch_id"),
        require_safe_id(idempotency_key, label="idempotency_key"),
    )


def _require_live_run(root: Path, run_id: str) -> dict[str, Any]:
    status = load_run(root, run_id)
    if status is None:
        raise CompositionTaskDriverError(
            f"run {run_id!r} not found",
            code="E_TEAM_COMPOSITION_TASK_RUN",
        )
    if status.get("status") in {"cancelled", "complete"}:
        raise CompositionTaskDriverError(
            f"run {run_id!r} is not live (status={status.get('status')!r})",
            code="E_TEAM_COMPOSITION_TASK_RUN",
        )
    return status


def _require_team_plane(root: Path, run_id: str) -> dict[str, Any]:
    try:
        return _require_control_plane(root, run_id)
    except TeamApiError as exc:
        raise CompositionTaskDriverError(
            str(exc),
            code="E_TEAM_COMPOSITION_TASK_CONTROL_PLANE",
            details=dict(exc.details),
        ) from exc


def _lane_scope(lane: Mapping[str, Any]) -> str:
    for key in ("dimension", "surface", "lane"):
        value = lane.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}={value}"
    return "scope=none"


def _task_description(
    *,
    source_kind: str,
    composition_id: str,
    composition_digest: str,
    lane: Mapping[str, Any],
) -> str:
    lane_id = str(lane["lane_id"])
    depends = ",".join(str(x) for x in lane.get("depends_on") or []) or "(none)"
    digest_short = composition_digest[:16]
    text = (
        f"Composition task for {source_kind} composition_id={composition_id} "
        f"digest={digest_short}… lane={lane_id} role={lane.get('role')} "
        f"posture={lane.get('posture')} {_lane_scope(lane)} "
        f"depends_on=[{depends}]. "
        "On complete, submit LaneTaskResultV1 JSON as transition-task-status "
        "result: exact keys schema_version=1, status "
        "(complete|rejected|blocked), payload object; optional reason "
        "(required when status!=complete). Do NOT supply lane_id, "
        "artifact_kind, digests, composition id, or writer — collector derives "
        "those from the immutable manifest and committed batch."
    )
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[: MAX_DESCRIPTION_CHARS - 1] + "…"
    return text


def compile_composition_task_batch_v1(
    manifest: Mapping[str, Any],
    *,
    run_id: str,
    team_id: str,
    source_kind: str,
) -> dict[str, Any]:
    """Pure: composition manifest lanes → compiled ``TaskBatchV1``."""
    if not isinstance(manifest, Mapping):
        raise CompositionTaskDriverError(
            "manifest must be an object",
            code="E_TEAM_COMPOSITION_TASK_MANIFEST",
        )
    if manifest.get("execution_supported") is not False:
        raise CompositionTaskDriverError(
            "execution_supported must be false",
            code="E_TEAM_COMPOSITION_TASK_MANIFEST",
        )
    try:
        rid = require_safe_id(run_id, label="run_id")
        tid = require_safe_id(team_id, label="team_id")
        composition_id = require_safe_id(
            manifest.get("composition_id"), label="composition_id"
        )
        digest = require_sha256(manifest.get("digest"), label="digest")
    except ContractValidationError as exc:
        raise CompositionTaskDriverError(
            str(exc),
            code="E_TEAM_COMPOSITION_TASK_MANIFEST",
        ) from exc

    lanes = manifest.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise CompositionTaskDriverError(
            "manifest.lanes must be a non-empty array",
            code="E_TEAM_COMPOSITION_TASK_MANIFEST",
        )

    batch_id, idempotency_key = composition_batch_ids(source_kind, composition_id)
    tasks: list[dict[str, Any]] = []
    for idx, lane in enumerate(lanes):
        if not isinstance(lane, Mapping):
            raise CompositionTaskDriverError(
                f"manifest.lanes[{idx}] must be an object",
                code="E_TEAM_COMPOSITION_TASK_MANIFEST",
            )
        try:
            lane_id = require_safe_id(lane.get("lane_id"), label=f"lanes[{idx}].lane_id")
        except ContractValidationError as exc:
            raise CompositionTaskDriverError(
                str(exc),
                code="E_TEAM_COMPOSITION_TASK_MANIFEST",
            ) from exc
        depends_raw = lane.get("depends_on")
        if not isinstance(depends_raw, list):
            raise CompositionTaskDriverError(
                f"lanes[{idx}].depends_on must be an array",
                code="E_TEAM_COMPOSITION_TASK_MANIFEST",
            )
        depends_on = [str(d) for d in depends_raw]
        artifact = lane.get("expected_artifact")
        if not isinstance(artifact, Mapping):
            raise CompositionTaskDriverError(
                f"lanes[{idx}].expected_artifact must be an object",
                code="E_TEAM_COMPOSITION_TASK_MANIFEST",
            )
        subject = f"lane {lane_id}"
        if len(subject) > MAX_SUBJECT_CHARS:
            subject = subject[: MAX_SUBJECT_CHARS - 1] + "…"
        tasks.append(
            {
                "task_key": lane_id,
                "subject": subject,
                "description": _task_description(
                    source_kind=source_kind,
                    composition_id=composition_id,
                    composition_digest=digest,
                    lane=lane,
                ),
                "depends_on": depends_on,
                "requires_code_change": False,
                "expected_artifact": dict(artifact),
            }
        )

    payload = {
        "schema_version": 1,
        "run_id": rid,
        "team_id": tid,
        "batch_id": batch_id,
        "idempotency_key": idempotency_key,
        "source": {
            "kind": source_kind,
            "source_id": composition_id,
            "digest": digest,
        },
        "tasks": tasks,
    }
    try:
        return compile_task_batch_v1(payload)
    except TaskBatchError as exc:
        raise CompositionTaskDriverError(
            str(exc),
            code=getattr(exc, "code", "E_TEAM_TASK_BATCH") or "E_TEAM_TASK_BATCH",
            details=getattr(exc, "details", None),
        ) from exc


def parse_lane_task_result_v1(raw: Any) -> dict[str, Any]:
    """Exact-key ``LaneTaskResultV1`` parser (workers omit identity/digest fields)."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CompositionTaskDriverError(
                "lane task result is not valid UTF-8",
                code="E_TEAM_COMPOSITION_TASK_RESULT",
            ) from exc
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_INLINE_JSON_BYTES:
            raise CompositionTaskDriverError(
                "lane task result JSON exceeds inline budget",
                code="E_TEAM_COMPOSITION_TASK_RESULT",
            )
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CompositionTaskDriverError(
                f"lane task result is not valid JSON: {exc}",
                code="E_TEAM_COMPOSITION_TASK_RESULT",
            ) from exc
    try:
        body = require_object(raw, label="LaneTaskResultV1")
        forbidden = set(body) & _LANE_RESULT_FORBIDDEN
        if forbidden:
            raise ContractValidationError(
                f"LaneTaskResultV1 forbids worker-supplied keys: "
                f"{sorted(forbidden)!r}"
            )
        require_exact_keys(
            body,
            required=_LANE_RESULT_REQUIRED,
            optional=_LANE_RESULT_OPTIONAL,
            label="LaneTaskResultV1",
        )
        schema_version = require_integer(
            body.get("schema_version"),
            label="schema_version",
            minimum=1,
        )
        if schema_version != LANE_TASK_RESULT_SCHEMA_VERSION:
            raise ContractValidationError("schema_version must be 1")
        status = body.get("status")
        if status not in _LANE_RESULT_STATUSES:
            raise ContractValidationError(
                f"status must be one of {sorted(_LANE_RESULT_STATUSES)}"
            )
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise ContractValidationError("payload must be an object")
    except ContractValidationError as exc:
        raise CompositionTaskDriverError(
            str(exc),
            code="E_TEAM_COMPOSITION_TASK_RESULT",
        ) from exc

    out: dict[str, Any] = {
        "schema_version": LANE_TASK_RESULT_SCHEMA_VERSION,
        "status": status,
        "payload": dict(payload),
    }
    if "reason" in body:
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise CompositionTaskDriverError(
                "reason must be a non-empty string when present",
                code="E_TEAM_COMPOSITION_TASK_RESULT",
            )
        if len(reason) > MAX_REASON_CHARS:
            raise CompositionTaskDriverError(
                "reason too long",
                code="E_TEAM_COMPOSITION_TASK_RESULT",
            )
        out["reason"] = reason.strip()
    if status != "complete" and "reason" not in out:
        raise CompositionTaskDriverError(
            "non-complete LaneTaskResultV1 requires reason",
            code="E_TEAM_COMPOSITION_TASK_RESULT",
        )
    size = len(canonical_json_bytes(out))
    if size > MAX_INLINE_JSON_BYTES:
        raise CompositionTaskDriverError(
            f"lane task result exceeds inline budget ({size})",
            code="E_TEAM_COMPOSITION_TASK_RESULT",
        )
    return out


def admit_composition_tasks_v1(
    root: Path | str,
    run_id: str,
    team_id: str,
    adapter: CompositionTaskAdapter,
) -> dict[str, Any]:
    """Admit a materialized composition DAG as a committed Team task batch."""
    root_path = Path(root).resolve()
    rid = _safe_run_id(run_id)
    try:
        tid = require_safe_id(team_id, label="team_id")
    except ContractValidationError as exc:
        raise CompositionTaskDriverError(
            str(exc),
            code="E_TEAM_COMPOSITION_TASK_INVALID",
        ) from exc

    _require_live_run(root_path, rid)
    _require_team_plane(root_path, rid)
    lock = adapter.composition_lock_path(root_path, rid)

    with exclusive_lock(lock):
        manifest = adapter.load_manifest(root_path, rid)
        if manifest.get("execution_supported") is not False:
            raise CompositionTaskDriverError(
                "execution_supported must be false",
                code="E_TEAM_COMPOSITION_TASK_MANIFEST",
            )
        compiled = compile_composition_task_batch_v1(
            manifest,
            run_id=rid,
            team_id=tid,
            source_kind=adapter.source_kind,
        )
        # Re-derive raw admit payload from compiled fields (PR11 writer).
        payload = {
            "schema_version": compiled["schema_version"],
            "run_id": compiled["run_id"],
            "team_id": compiled["team_id"],
            "batch_id": compiled["batch_id"],
            "idempotency_key": compiled["idempotency_key"],
            "source": dict(compiled["source"]),
            "tasks": [
                {
                    "task_key": t["task_key"],
                    "subject": t["subject"],
                    "description": t["description"],
                    "depends_on": list(t["depends_on"]),
                    "requires_code_change": bool(t["requires_code_change"]),
                    "expected_artifact": dict(t["expected_artifact"]),
                }
                for t in compiled["tasks"]
            ],
        }
        try:
            admitted = admit_task_batch_v1(root_path, payload)
        except TaskBatchError as exc:
            raise CompositionTaskDriverError(
                str(exc),
                code=getattr(exc, "code", "E_TEAM_TASK_BATCH") or "E_TEAM_TASK_BATCH",
                details=getattr(exc, "details", None),
            ) from exc

    return {
        "ok": True,
        "idempotent": bool(admitted.get("idempotent")),
        "source_kind": adapter.source_kind,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "batch_id": admitted["batch_id"],
        "idempotency_key": admitted["idempotency_key"],
        "digest": admitted["digest"],
        "state": admitted["state"],
        "topo_order": list(admitted["topo_order"]),
        "task_key_to_id": dict(admitted["task_key_to_id"]),
        "execution_supported": False,
    }


def _load_committed_batch(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    idempotency_key: str,
    expected_digest: str,
    expected_batch_id: str,
    expected_source: Mapping[str, Any],
) -> dict[str, Any]:
    path = batch_record_path(root, run_id, team_id, idempotency_key)
    if path.is_symlink():
        raise CompositionTaskDriverError(
            "task batch record may not be a symlink",
            code="E_TEAM_COMPOSITION_TASK_BATCH",
        )
    try:
        record = _load_batch_record(path, run_id=run_id, team_id=team_id)
    except TaskBatchError as exc:
        raise CompositionTaskDriverError(
            str(exc),
            code=getattr(exc, "code", "E_TEAM_TASK_BATCH") or "E_TEAM_TASK_BATCH",
            details=getattr(exc, "details", None),
        ) from exc
    if record is None:
        raise CompositionTaskDriverError(
            "committed composition task batch missing",
            code="E_TEAM_COMPOSITION_TASK_BATCH",
        )
    if record.get("state") != "committed":
        raise CompositionTaskDriverError(
            f"composition task batch state must be committed "
            f"(got {record.get('state')!r})",
            code="E_TEAM_COMPOSITION_TASK_BATCH",
        )
    if record.get("digest") != expected_digest:
        raise CompositionTaskDriverError(
            "composition task batch digest mismatch",
            code="E_TEAM_COMPOSITION_TASK_BATCH",
        )
    if record.get("batch_id") != expected_batch_id:
        raise CompositionTaskDriverError(
            "composition task batch_id mismatch",
            code="E_TEAM_COMPOSITION_TASK_BATCH",
        )
    if dict(record.get("source") or {}) != dict(expected_source):
        raise CompositionTaskDriverError(
            "composition task batch source mismatch",
            code="E_TEAM_COMPOSITION_TASK_BATCH",
        )
    if record.get("writer") != CLI_WRITER:
        raise CompositionTaskDriverError(
            "foreign writer refused on composition task batch",
            code="E_TEAM_COMPOSITION_TASK_BATCH",
        )
    return record


def _sorted_task_ids(task_key_to_id: Mapping[str, str]) -> list[str]:
    ids = list(task_key_to_id.values())
    for tid in ids:
        if not str(tid).isdigit():
            raise CompositionTaskDriverError(
                f"task id must be numeric: {tid!r}",
                code="E_TEAM_COMPOSITION_TASK_BATCH",
            )
    return sorted(ids, key=lambda x: int(x))


@contextmanager
def _ordered_task_locks(
    root: Path, run_id: str, team_id: str, task_ids: Sequence[str]
) -> Iterator[None]:
    with ExitStack() as stack:
        for tid in task_ids:
            path = _task_path(root, run_id, team_id, tid)
            stack.enter_context(exclusive_lock(path.with_suffix(".lock")))
        yield


def collect_composition_tasks_v1(
    root: Path | str,
    run_id: str,
    team_id: str,
    adapter: CompositionTaskAdapter,
) -> dict[str, Any]:
    """Collect completed lane tasks into the existing result-bundle producer path."""
    root_path = Path(root).resolve()
    rid = _safe_run_id(run_id)
    try:
        tid = require_safe_id(team_id, label="team_id")
    except ContractValidationError as exc:
        raise CompositionTaskDriverError(
            str(exc),
            code="E_TEAM_COMPOSITION_TASK_INVALID",
        ) from exc

    _require_live_run(root_path, rid)
    _require_team_plane(root_path, rid)
    lock = adapter.composition_lock_path(root_path, rid)

    with exclusive_lock(lock):
        manifest = adapter.load_manifest(root_path, rid)
        if manifest.get("execution_supported") is not False:
            raise CompositionTaskDriverError(
                "execution_supported must be false",
                code="E_TEAM_COMPOSITION_TASK_MANIFEST",
            )
        compiled = compile_composition_task_batch_v1(
            manifest,
            run_id=rid,
            team_id=tid,
            source_kind=adapter.source_kind,
        )
        expected_source = dict(compiled["source"])
        batch_path = batch_record_path(
            root_path, rid, tid, str(compiled["idempotency_key"])
        )

        with exclusive_lock(batch_path.with_suffix(".lock")):
            record = _load_committed_batch(
                root_path,
                run_id=rid,
                team_id=tid,
                idempotency_key=str(compiled["idempotency_key"]),
                expected_digest=str(compiled["digest"]),
                expected_batch_id=str(compiled["batch_id"]),
                expected_source=expected_source,
            )
            mapping = dict(record["task_key_to_id"])
            lane_ids = [str(lane["lane_id"]) for lane in manifest["lanes"]]
            if set(mapping) != set(lane_ids):
                raise CompositionTaskDriverError(
                    "committed batch lane coverage mismatch",
                    code="E_TEAM_COMPOSITION_TASK_BATCH",
                    details={
                        "expected": sorted(lane_ids),
                        "actual": sorted(mapping),
                    },
                )
            if list(record.get("topo_order") or []) != list(compiled["topo_order"]):
                raise CompositionTaskDriverError(
                    "committed batch topo_order mismatch",
                    code="E_TEAM_COMPOSITION_TASK_BATCH",
                )

            tasks_by_key = {t["task_key"]: t for t in compiled["tasks"]}
            ordered_ids = _sorted_task_ids(mapping)
            with _ordered_task_locks(root_path, rid, tid, ordered_ids):
                receipts: list[dict[str, Any]] = []
                # Stable receipt order = manifest lane order.
                for lane in manifest["lanes"]:
                    lane_id = str(lane["lane_id"])
                    task_id = mapping[lane_id]
                    task = _read_task(root_path, rid, tid, task_id)
                    if task is None:
                        raise CompositionTaskDriverError(
                            f"mapped task {task_id!r} missing for lane {lane_id!r}",
                            code="E_TEAM_COMPOSITION_TASK_COLLECT",
                        )
                    _assert_task_ready_for_collect(
                        task,
                        compiled=compiled,
                        task_key=lane_id,
                        expected_artifact=tasks_by_key[lane_id]["expected_artifact"],
                    )
                    lane_result = parse_lane_task_result_v1(task.get("result"))
                    receipt: dict[str, Any] = {
                        "lane_id": lane_id,
                        "status": lane_result["status"],
                        "artifact_kind": tasks_by_key[lane_id]["expected_artifact"][
                            "kind"
                        ],
                        "payload": lane_result["payload"],
                    }
                    if "reason" in lane_result:
                        receipt["reason"] = lane_result["reason"]
                    receipts.append(receipt)

                bundle: dict[str, Any] = {
                    "kind": adapter.result_bundle_kind,
                    "schema_version": 1,
                    "composition_id": manifest["composition_id"],
                    "composition_digest": manifest["digest"],
                    "receipts": receipts,
                }
                produced = adapter.persist_from_bundle_locked(
                    root=root_path,
                    run_id=rid,
                    manifest=manifest,
                    bundle=bundle,
                )

    out = {
        "ok": True,
        "source_kind": adapter.source_kind,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "batch_digest": compiled["digest"],
        "task_key_to_id": mapping,
        "execution_supported": False,
    }
    out.update(produced)
    return out


def _assert_task_ready_for_collect(
    task: Mapping[str, Any],
    *,
    compiled: Mapping[str, Any],
    task_key: str,
    expected_artifact: Mapping[str, Any],
) -> None:
    if task.get("status") != "completed":
        raise CompositionTaskDriverError(
            f"task for lane {task_key!r} must be completed "
            f"(got {task.get('status')!r})",
            code="E_TEAM_COMPOSITION_TASK_COLLECT",
            details={"task_key": task_key, "status": task.get("status")},
        )
    # Honest transition-task-status → completed clears claim but retains owner.
    # Collect requires claim-free, not owner-free.
    if task.get("claim") is not None:
        raise CompositionTaskDriverError(
            f"task for lane {task_key!r} still claimed",
            code="E_TEAM_COMPOSITION_TASK_COLLECT",
            details={"task_key": task_key},
        )
    expected_batch = _batch_binding(compiled, task_key=task_key)
    if dict(task.get("batch") or {}) != expected_batch:
        raise CompositionTaskDriverError(
            f"task for lane {task_key!r} batch binding mismatch",
            code="E_TEAM_COMPOSITION_TASK_COLLECT",
            details={"task_key": task_key},
        )
    if dict(task.get("expected_artifact") or {}) != dict(expected_artifact):
        raise CompositionTaskDriverError(
            f"task for lane {task_key!r} expected_artifact mismatch",
            code="E_TEAM_COMPOSITION_TASK_COLLECT",
            details={"task_key": task_key},
        )
    if task.get("result") is None:
        raise CompositionTaskDriverError(
            f"task for lane {task_key!r} missing result",
            code="E_TEAM_COMPOSITION_TASK_COLLECT",
            details={"task_key": task_key},
        )


__all__ = [
    "CompositionTaskAdapter",
    "CompositionTaskDriverError",
    "LANE_TASK_RESULT_SCHEMA_VERSION",
    "SOURCE_KIND_HYPERPLAN",
    "SOURCE_KIND_SECURITY_RESEARCH",
    "admit_composition_tasks_v1",
    "collect_composition_tasks_v1",
    "compile_composition_task_batch_v1",
    "composition_batch_ids",
    "parse_lane_task_result_v1",
]
