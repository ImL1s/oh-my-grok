"""Team Catalog V4 atomic task-batch DAG admission (#69 PR11).

Pure ``compile_task_batch_v1`` validates and normalizes bounded batch requests
with intra-batch dependency DAGs. ``admit_task_batch_v1`` persists tasks
crash-safely under the Team API config lock plus a per-batch lock. Batch-bound
tasks remain invisible to read/list/claim until the batch commit marker exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    ContractPathError,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    read_managed_regular_bytes,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_nonempty_string,
    require_object,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)
from omg_cli.evidence import CLI_WRITER
from omg_cli.team.api import (
    _api_config_path,
    _ensure_config_locked,
    _read_task,
    _task_path,
    _team_state_dir,
    _write_config,
    _write_task,
)

BATCH_SCHEMA_VERSION = 1
BATCH_STORE_KIND = "team_task_batch"
MIN_TASKS = 1
MAX_TASKS = 32
MAX_SUBJECT_CHARS = 4000
MAX_DESCRIPTION_CHARS = 8000
MAX_INLINE_JSON_BYTES = 64 * 1024
MAX_REQUIRED_FIELDS = 32

_INPUT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "team_id",
        "batch_id",
        "idempotency_key",
        "source",
        "tasks",
    }
)
_SOURCE_KEYS = frozenset({"kind", "source_id", "digest"})
_TASK_KEYS = frozenset(
    {
        "task_key",
        "subject",
        "description",
        "depends_on",
        "requires_code_change",
        "expected_artifact",
    }
)
_ARTIFACT_REQUIRED = frozenset({"kind", "schema_version", "required_fields"})
_ARTIFACT_OPTIONAL = frozenset({"dimension", "surface", "lane"})
_RECORD_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "batch_id",
        "idempotency_key",
        "digest",
        "state",
        "source",
        "tasks",
        "topo_order",
        "task_key_to_id",
        "updated_at",
    }
)

_crash_hook: Callable[[str], None] | None = None


class TaskBatchError(ValueError):
    """Fail-closed task-batch compiler or admission error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "E_TEAM_TASK_BATCH",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _raise_from_contract(exc: ContractValidationError) -> None:
    raise TaskBatchError(str(exc), code="E_TEAM_TASK_BATCH_INVALID") from exc


def _invoke_crash_hook(point: str) -> None:
    hook = _crash_hook
    if hook is not None:
        hook(point)


def batch_record_path(
    root: Path | str, run_id: str, team_id: str, idempotency_key: str
) -> Path:
    key = require_safe_id(idempotency_key, label="idempotency_key")
    rid = require_safe_id(run_id, label="run_id")
    tid = require_safe_id(team_id, label="team_id")
    return (
        _team_state_dir(Path(root).resolve(), rid, tid)
        / "batches"
        / f"{safe_path_key(key)}.json"
    )


def _parse_source(raw: Any) -> dict[str, Any]:
    try:
        obj = require_object(raw, label="source")
        require_exact_keys(obj, required=_SOURCE_KEYS, label="source")
        kind = require_safe_id(obj.get("kind"), label="source.kind")
        source_id = require_safe_id(obj.get("source_id"), label="source.source_id")
        digest = require_sha256(obj.get("digest"), label="source.digest")
    except ContractValidationError as exc:
        _raise_from_contract(exc)
    return {"kind": kind, "source_id": source_id, "digest": digest}


def _parse_expected_artifact(raw: Any) -> dict[str, Any]:
    try:
        obj = require_object(raw, label="expected_artifact")
        require_exact_keys(
            obj,
            required=_ARTIFACT_REQUIRED,
            optional=_ARTIFACT_OPTIONAL,
            label="expected_artifact",
        )
        kind = require_nonempty_string(obj.get("kind"), label="expected_artifact.kind")
        schema_version = require_integer(
            obj.get("schema_version"),
            label="expected_artifact.schema_version",
            minimum=1,
        )
        if schema_version != 1:
            raise ContractValidationError(
                "expected_artifact.schema_version must be 1"
            )
        fields_raw = obj.get("required_fields")
        if not isinstance(fields_raw, list) or not fields_raw:
            raise ContractValidationError(
                "expected_artifact.required_fields must be a non-empty array"
            )
        if len(fields_raw) > MAX_REQUIRED_FIELDS:
            raise ContractValidationError(
                f"expected_artifact.required_fields exceeds {MAX_REQUIRED_FIELDS} items"
            )
        required_fields: list[str] = []
        seen_fields: set[str] = set()
        for idx, item in enumerate(fields_raw):
            field = require_nonempty_string(
                item, label=f"expected_artifact.required_fields[{idx}]"
            )
            if field in seen_fields:
                raise ContractValidationError(
                    "expected_artifact.required_fields must be unique"
                )
            seen_fields.add(field)
            required_fields.append(field)
    except ContractValidationError as exc:
        _raise_from_contract(exc)

    out: dict[str, Any] = {
        "kind": kind,
        "schema_version": 1,
        "required_fields": required_fields,
    }
    for optional in ("dimension", "surface", "lane"):
        if optional in obj:
            try:
                out[optional] = require_nonempty_string(
                    obj.get(optional), label=f"expected_artifact.{optional}"
                )
            except ContractValidationError as exc:
                _raise_from_contract(exc)
    return out


def _parse_task(raw: Any, *, label: str) -> dict[str, Any]:
    try:
        obj = require_object(raw, label=label)
    except ContractValidationError as exc:
        _raise_from_contract(exc)

    extra = set(obj) - _TASK_KEYS
    if extra:
        raise TaskBatchError(
            f"task batch rejects caller-supplied fields: {sorted(extra)!r}",
            code="E_TEAM_TASK_BATCH_INVALID",
        )

    try:
        require_exact_keys(obj, required=_TASK_KEYS, label=label)
        task_key = require_safe_id(obj.get("task_key"), label=f"{label}.task_key")
        subject = require_nonempty_string(obj.get("subject"), label=f"{label}.subject")
        description = require_nonempty_string(
            obj.get("description"), label=f"{label}.description"
        )
        depends_raw = obj.get("depends_on")
        if not isinstance(depends_raw, list):
            raise ContractValidationError(f"{label}.depends_on must be an array")
        depends_on: list[str] = []
        seen_deps: set[str] = set()
        for idx, dep in enumerate(depends_raw):
            dep_key = require_safe_id(dep, label=f"{label}.depends_on[{idx}]")
            if dep_key in seen_deps:
                raise TaskBatchError(
                    f"task batch duplicate depends_on entry {dep_key!r}",
                    code="E_TEAM_TASK_BATCH_INVALID",
                )
            seen_deps.add(dep_key)
            depends_on.append(dep_key)
        requires_code_change = obj.get("requires_code_change")
        if not isinstance(requires_code_change, bool):
            raise ContractValidationError(
                f"{label}.requires_code_change must be a boolean"
            )
    except ContractValidationError as exc:
        _raise_from_contract(exc)

    if len(subject) > MAX_SUBJECT_CHARS:
        raise TaskBatchError(
            f"subject exceeds {MAX_SUBJECT_CHARS} characters",
            code="E_TEAM_TASK_BATCH_INVALID",
        )
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise TaskBatchError(
            f"description exceeds {MAX_DESCRIPTION_CHARS} characters",
            code="E_TEAM_TASK_BATCH_INVALID",
        )

    expected_artifact = _parse_expected_artifact(obj.get("expected_artifact"))
    return {
        "task_key": task_key,
        "subject": subject,
        "description": description,
        "depends_on": depends_on,
        "requires_code_change": requires_code_change,
        "expected_artifact": expected_artifact,
    }


def _topo_order(tasks_by_key: Mapping[str, Mapping[str, Any]]) -> list[str]:
    in_degree: dict[str, int] = {key: 0 for key in tasks_by_key}
    dependents: dict[str, list[str]] = {key: [] for key in tasks_by_key}
    for key, task in tasks_by_key.items():
        for dep in task["depends_on"]:
            if dep not in tasks_by_key:
                raise TaskBatchError(
                    f"task batch unknown dependency {dep!r} for {key!r}",
                    code="E_TEAM_TASK_BATCH_INVALID",
                )
            if dep == key:
                raise TaskBatchError(
                    f"task batch self-dependency on {key!r}",
                    code="E_TEAM_TASK_BATCH_INVALID",
                )
            in_degree[key] += 1
            dependents[dep].append(key)

    ready = sorted(key for key, degree in in_degree.items() if degree == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for child in sorted(dependents[node]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
        ready.sort()

    if len(order) != len(tasks_by_key):
        raise TaskBatchError(
            "task batch dependency cycle detected",
            code="E_TEAM_TASK_BATCH_INVALID",
        )
    return order


def _digest_core(compiled: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": compiled["schema_version"],
        "run_id": compiled["run_id"],
        "team_id": compiled["team_id"],
        "batch_id": compiled["batch_id"],
        "source": compiled["source"],
        "tasks": compiled["tasks"],
        "topo_order": compiled["topo_order"],
    }


def _assert_inline_budget(compiled: Mapping[str, Any]) -> None:
    size = len(canonical_json_bytes(dict(_digest_core(compiled))))
    if size > MAX_INLINE_JSON_BYTES:
        raise TaskBatchError(
            f"task batch exceeds inline budget ({size} > {MAX_INLINE_JSON_BYTES})",
            code="E_TEAM_TASK_BATCH_INVALID",
        )


def compile_task_batch_v1(raw: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Pure compiler: validate exact-key input and produce normalized batch."""
    try:
        body = require_object(raw, label="task_batch")
        require_exact_keys(body, required=_INPUT_KEYS, label="task_batch")
        schema_version = require_integer(
            body.get("schema_version"), label="schema_version", minimum=1
        )
        if schema_version != BATCH_SCHEMA_VERSION:
            raise ContractValidationError("schema_version must be 1")
        run_id = require_safe_id(body.get("run_id"), label="run_id")
        team_id = require_safe_id(body.get("team_id"), label="team_id")
        batch_id = require_safe_id(body.get("batch_id"), label="batch_id")
        idempotency_key = require_safe_id(
            body.get("idempotency_key"), label="idempotency_key"
        )
        source = _parse_source(body.get("source"))
        tasks_raw = body.get("tasks")
        if not isinstance(tasks_raw, list):
            raise ContractValidationError("tasks must be an array")
    except ContractValidationError as exc:
        _raise_from_contract(exc)

    if len(tasks_raw) < MIN_TASKS or len(tasks_raw) > MAX_TASKS:
        raise TaskBatchError(
            f"task batch must contain {MIN_TASKS}–{MAX_TASKS} tasks",
            code="E_TEAM_TASK_BATCH_INVALID",
        )

    parsed_tasks: list[dict[str, Any]] = []
    for idx, item in enumerate(tasks_raw):
        parsed_tasks.append(_parse_task(item, label=f"tasks[{idx}]"))

    tasks_by_key: dict[str, dict[str, Any]] = {}
    for task in parsed_tasks:
        key = task["task_key"]
        if key in tasks_by_key:
            raise TaskBatchError(
                f"task batch task_key must be unique; duplicate {key!r}",
                code="E_TEAM_TASK_BATCH_INVALID",
            )
        tasks_by_key[key] = task

    topo_order = _topo_order(tasks_by_key)
    ordered_tasks = [dict(tasks_by_key[key]) for key in topo_order]

    compiled: dict[str, Any] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "run_id": run_id,
        "team_id": team_id,
        "batch_id": batch_id,
        "idempotency_key": idempotency_key,
        "source": source,
        "tasks": ordered_tasks,
        "topo_order": topo_order,
    }
    compiled["digest"] = sha256_hex(canonical_json_bytes(_digest_core(compiled)))
    _assert_inline_budget(compiled)
    return compiled


def _validate_batch_record(
    record: Mapping[str, Any], *, run_id: str, team_id: str
) -> dict[str, Any]:
    row = dict(record)
    if set(row) != _RECORD_KEYS:
        raise TaskBatchError(
            "task batch record key mismatch",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )
    if (
        row.get("store_kind") != BATCH_STORE_KIND
        or row.get("schema_version") != BATCH_SCHEMA_VERSION
        or row.get("writer") != CLI_WRITER
    ):
        raise TaskBatchError(
            "task batch record header mismatch",
            code="E_TEAM_TASK_BATCH_FOREIGN",
        )
    if row.get("run_id") != run_id or row.get("team_id") != team_id:
        raise TaskBatchError(
            "task batch record identity mismatch",
            code="E_TEAM_TASK_BATCH_FOREIGN",
        )
    state = row.get("state")
    if state not in {"prepared", "committed"}:
        raise TaskBatchError(
            f"task batch record has invalid state {state!r}",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )
    require_sha256(row.get("digest"), label="digest")
    mapping = row.get("task_key_to_id")
    if not isinstance(mapping, dict) or not mapping:
        raise TaskBatchError(
            "task batch record task_key_to_id must be a non-empty object",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )
    normalized_mapping: dict[str, str] = {}
    for task_key, task_id in mapping.items():
        require_safe_id(task_key, label="task_key_to_id key")
        tid = require_nonempty_string(task_id, label="task_key_to_id value")
        if not tid.isdigit():
            raise TaskBatchError(
                "task batch record task_id must be numeric",
                code="E_TEAM_TASK_BATCH_CORRUPT",
            )
        normalized_mapping[task_key] = tid
    row["task_key_to_id"] = normalized_mapping
    topo = row.get("topo_order")
    if not isinstance(topo, list) or not all(isinstance(x, str) for x in topo):
        raise TaskBatchError(
            "task batch record topo_order must be a string array",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )
    tasks = row.get("tasks")
    if not isinstance(tasks, list):
        raise TaskBatchError(
            "task batch record tasks must be an array",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )
    return row


def _load_batch_record(
    path: Path, *, run_id: str, team_id: str
) -> dict[str, Any] | None:
    if path.is_symlink():
        raise TaskBatchError(
            "task batch record may not be a symlink",
            code="E_TEAM_TASK_BATCH_PATH",
        )
    if not path.exists():
        return None
    try:
        body = read_managed_regular_bytes(path, max_bytes=MAX_INLINE_JSON_BYTES)
    except ContractPathError as exc:
        message = str(exc)
        if "symlink" in message:
            raise TaskBatchError(message, code="E_TEAM_TASK_BATCH_PATH") from exc
        raise TaskBatchError(message, code="E_TEAM_TASK_BATCH_CORRUPT") from exc
    except OSError as exc:
        raise TaskBatchError(
            f"task batch record unreadable: {exc}",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        ) from exc
    try:
        parsed = parse_canonical_json_bytes(body)
    except ValueError as exc:
        raise TaskBatchError(
            f"task batch record is corrupt: {exc}",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        ) from exc
    if not isinstance(parsed, dict):
        raise TaskBatchError(
            "task batch record must be an object",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )
    return _validate_batch_record(parsed, run_id=run_id, team_id=team_id)


def _write_batch_record(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    ensure_managed_dir(path.parent)
    row = dict(record)
    row.setdefault("store_kind", BATCH_STORE_KIND)
    row.setdefault("schema_version", BATCH_SCHEMA_VERSION)
    row.setdefault("writer", CLI_WRITER)
    row["updated_at"] = _utc_now()
    validated = _validate_batch_record(
        row,
        run_id=str(row["run_id"]),
        team_id=str(row["team_id"]),
    )
    atomic_write_bytes(
        path,
        canonical_json_bytes(validated),
        mode=DATA_FILE_MODE,
        replace=True,
    )
    return validated


def batch_is_committed(
    root: Path | str,
    run_id: str,
    team_id: str,
    batch_meta: Mapping[str, Any],
) -> bool:
    """Return True when the batch commit marker exists for *batch_meta*."""
    if not isinstance(batch_meta, Mapping):
        return False
    idempotency_key = batch_meta.get("idempotency_key")
    digest = batch_meta.get("digest")
    if not isinstance(idempotency_key, str) or not isinstance(digest, str):
        return False
    try:
        require_safe_id(idempotency_key, label="batch.idempotency_key")
        require_sha256(digest, label="batch.digest")
    except (ContractValidationError, TaskBatchError):
        return False
    path = batch_record_path(root, run_id, team_id, idempotency_key)
    try:
        record = _load_batch_record(path, run_id=run_id, team_id=team_id)
    except TaskBatchError:
        return False
    if record is None:
        return False
    return (
        record.get("state") == "committed"
        and record.get("digest") == digest
        and record.get("idempotency_key") == idempotency_key
    )


def _reserve_contiguous_task_ids(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    count: int,
    start_id: int,
) -> tuple[list[str], int]:
    reserved: list[str] = []
    cursor = start_id
    while len(reserved) < count:
        task_id = str(cursor)
        if not _task_path(root, run_id, team_id, task_id).exists():
            reserved.append(task_id)
        cursor += 1
    return reserved, cursor


def _batch_binding(
    compiled: Mapping[str, Any], *, task_key: str
) -> dict[str, str]:
    return {
        "batch_id": str(compiled["batch_id"]),
        "idempotency_key": str(compiled["idempotency_key"]),
        "task_key": task_key,
        "digest": str(compiled["digest"]),
    }


def _expected_task_document(
    compiled: Mapping[str, Any],
    *,
    task: Mapping[str, Any],
    task_id: str,
    task_key_to_id: Mapping[str, str],
) -> dict[str, Any]:
    dep_ids: list[str] = []
    for dep in task["depends_on"]:
        try:
            dep_ids.append(task_key_to_id[dep])
        except KeyError as exc:
            raise TaskBatchError(
                f"task_key_to_id missing dependency {dep!r}",
                code="E_TEAM_TASK_BATCH_CORRUPT",
                details={"task_key": dep},
            ) from exc
    return {
        "id": task_id,
        "subject": task["subject"],
        "description": task["description"],
        "status": "pending",
        "depends_on": dep_ids,
        "blocked_by": dep_ids,
        "version": 1,
        "owner": None,
        "claim": None,
        "requires_code_change": bool(task["requires_code_change"]),
        "expected_artifact": dict(task["expected_artifact"]),
        "batch": _batch_binding(compiled, task_key=str(task["task_key"])),
    }


def _task_documents_match(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    for key in (
        "id",
        "subject",
        "description",
        "status",
        "depends_on",
        "blocked_by",
        "version",
        "owner",
        "claim",
        "requires_code_change",
        "expected_artifact",
        "batch",
    ):
        if actual.get(key) != expected.get(key):
            return False
    return True


def _admit_result(
    compiled: Mapping[str, Any],
    *,
    state: str,
    idempotent: bool,
    task_key_to_id: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "state": state,
        "idempotent": idempotent,
        "batch_id": compiled["batch_id"],
        "idempotency_key": compiled["idempotency_key"],
        "digest": compiled["digest"],
        "topo_order": list(compiled["topo_order"]),
        "task_key_to_id": dict(task_key_to_id),
        "writer": CLI_WRITER,
    }


def _validate_prepared_mapping(
    compiled: Mapping[str, Any], task_key_to_id: Mapping[str, str]
) -> None:
    """Refuse tampered/incomplete prepared mappings before any task write."""
    expected_keys = [str(task["task_key"]) for task in compiled["tasks"]]
    mapping_keys = list(task_key_to_id.keys())
    if set(mapping_keys) != set(expected_keys) or len(mapping_keys) != len(
        expected_keys
    ):
        raise TaskBatchError(
            "task_key_to_id keys mismatch compiled batch "
            f"(expected {sorted(expected_keys)!r}, got {sorted(mapping_keys)!r})",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )
    ids = list(task_key_to_id.values())
    if len(set(ids)) != len(ids):
        raise TaskBatchError(
            "task_key_to_id contains duplicate task ids",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )
    topo = [str(x) for x in compiled["topo_order"]]
    if set(topo) != set(expected_keys) or len(topo) != len(expected_keys):
        raise TaskBatchError(
            "compiled topo_order keys mismatch tasks",
            code="E_TEAM_TASK_BATCH_CORRUPT",
        )


def _ensure_task_slot_owned(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    expected: Mapping[str, Any],
) -> None:
    """Refuse overwriting a task id that is not owned by this batch binding."""
    task_id = str(expected["id"])
    existing = _read_task(root, run_id, team_id, task_id)
    if existing is None:
        return
    if _task_documents_match(existing, expected):
        return
    batch = existing.get("batch")
    expected_batch = expected.get("batch")
    if isinstance(batch, Mapping) and batch == expected_batch:
        # Same-batch binding: allow rewrite to repair a partial/crashed write.
        return
    raise TaskBatchError(
        f"task id {task_id!r} already exists without matching batch binding "
        "(refusing foreign overwrite)",
        code="E_TEAM_TASK_BATCH_FOREIGN",
        details={"task_id": task_id},
    )


def _write_and_verify_task(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    expected: Mapping[str, Any],
) -> None:
    task_id = str(expected["id"])
    _ensure_task_slot_owned(
        root, run_id=run_id, team_id=team_id, expected=expected
    )
    _write_task(root, run_id, team_id, expected)
    actual = _read_task(root, run_id, team_id, task_id)
    if actual is None or not _task_documents_match(actual, expected):
        raise TaskBatchError(
            f"task batch task {task_id!r} failed post-write verification",
            code="E_TEAM_TASK_BATCH_CORRUPT",
            details={"task_id": task_id},
        )


def admit_task_batch_v1(
    root: Path | str, payload: Mapping[str, Any] | Any
) -> dict[str, Any]:
    """Crash-safe atomic admission of a compiled task batch."""
    compiled = compile_task_batch_v1(payload)
    root_path = Path(root).resolve()
    run_id = str(compiled["run_id"])
    team_id = str(compiled["team_id"])
    idempotency_key = str(compiled["idempotency_key"])
    digest = str(compiled["digest"])

    batch_path = batch_record_path(root_path, run_id, team_id, idempotency_key)
    config_path = _api_config_path(root_path, run_id, team_id)

    if batch_path.is_symlink():
        raise TaskBatchError(
            "task batch record may not be a symlink",
            code="E_TEAM_TASK_BATCH_PATH",
        )

    ensure_managed_dir(config_path.parent)
    ensure_managed_dir(batch_path.parent)

    with exclusive_lock(config_path.with_suffix(".lock")):
        with exclusive_lock(batch_path.with_suffix(".lock")):
            existing = _load_batch_record(
                batch_path, run_id=run_id, team_id=team_id
            )

            if existing is not None:
                if existing.get("digest") != digest:
                    raise TaskBatchError(
                        "task batch idempotency key conflicts with a different digest",
                        code="E_TEAM_TASK_BATCH_CONFLICT",
                    )
                if existing.get("batch_id") != compiled["batch_id"]:
                    raise TaskBatchError(
                        "task batch record batch_id mismatch",
                        code="E_TEAM_TASK_BATCH_CONFLICT",
                    )
                task_key_to_id = dict(existing["task_key_to_id"])
                if existing.get("state") == "committed":
                    return _admit_result(
                        compiled,
                        state="committed",
                        idempotent=True,
                        task_key_to_id=task_key_to_id,
                    )
                _validate_prepared_mapping(compiled, task_key_to_id)
                record = existing
            else:
                config = _ensure_config_locked(root_path, run_id, team_id)
                start_id = int(config["next_task_id"])
                reserved, next_task_id = _reserve_contiguous_task_ids(
                    root_path,
                    run_id=run_id,
                    team_id=team_id,
                    count=len(compiled["tasks"]),
                    start_id=start_id,
                )
                task_key_to_id = {
                    str(task["task_key"]): reserved[idx]
                    for idx, task in enumerate(compiled["tasks"])
                }
                record = {
                    "store_kind": BATCH_STORE_KIND,
                    "schema_version": BATCH_SCHEMA_VERSION,
                    "writer": CLI_WRITER,
                    "run_id": run_id,
                    "team_id": team_id,
                    "batch_id": compiled["batch_id"],
                    "idempotency_key": idempotency_key,
                    "digest": digest,
                    "state": "prepared",
                    "source": dict(compiled["source"]),
                    "tasks": list(compiled["tasks"]),
                    "topo_order": list(compiled["topo_order"]),
                    "task_key_to_id": dict(task_key_to_id),
                    "updated_at": _utc_now(),
                }
                _write_batch_record(batch_path, record)
                _write_config(
                    root_path,
                    {
                        **config,
                        "next_task_id": next_task_id,
                        "updated_at": _utc_now(),
                    },
                )
                _invoke_crash_hook("after_reserve")

            for idx, task in enumerate(compiled["tasks"]):
                key = str(task["task_key"])
                try:
                    task_id = task_key_to_id[key]
                except KeyError as exc:
                    raise TaskBatchError(
                        f"task_key_to_id missing entry for {key!r}",
                        code="E_TEAM_TASK_BATCH_CORRUPT",
                        details={"task_key": key},
                    ) from exc
                expected = _expected_task_document(
                    compiled,
                    task=task,
                    task_id=task_id,
                    task_key_to_id=task_key_to_id,
                )
                _write_and_verify_task(
                    root_path,
                    run_id=run_id,
                    team_id=team_id,
                    expected=expected,
                )
                _invoke_crash_hook(f"after_task_write:{idx}")

            _invoke_crash_hook("before_commit")

            committed = {
                **record,
                "state": "committed",
                "task_key_to_id": dict(task_key_to_id),
                "updated_at": _utc_now(),
            }
            _write_batch_record(batch_path, committed)

            return _admit_result(
                compiled,
                state="committed",
                idempotent=False,
                task_key_to_id=task_key_to_id,
            )


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "BATCH_STORE_KIND",
    "TaskBatchError",
    "_crash_hook",
    "_load_batch_record",
    "admit_task_batch_v1",
    "batch_is_committed",
    "batch_record_path",
    "compile_task_batch_v1",
]
