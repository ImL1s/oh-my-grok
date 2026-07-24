"""OMX-shaped ``omg team api`` façade (P0 subset).

Durable mailbox/task mutations go through the CLI-owned stores under
``.omg/state/runs/<run_id>/team/<team_key>/``. Workers never write mailbox or
task files directly.

P0 ops match OMX names; remaining ``TEAM_API_OPERATIONS`` return
``E_TEAM_API_UNIMPLEMENTED``. Full 33-op parity is intentionally not claimed.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_integer,
    require_safe_id,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from omg_cli.team.mailbox import (
    MailboxError,
    ack_message,
    list_messages,
    read_message,
    send_message,
)
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    WORKER_ENV_MARKERS,
    TeamError,
    TeamGateError,
    experimental_enabled,
    in_spawned_worker_context,
    load_team_meta,
)


CLI_WRITER = "omg-cli"
CLAIM_LEASE_SECONDS = 15 * 60
TASK_ID_MAX_DIGITS = 20

TEAM_TASK_STATUSES = frozenset(
    {"pending", "blocked", "in_progress", "completed", "failed"}
)
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed"})
TASK_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset(),
    "blocked": frozenset(),
    "in_progress": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}

# Full OMX operation catalog (names only). P0 is the shipped subset.
TEAM_API_OPERATIONS: tuple[str, ...] = (
    "send-message",
    "broadcast",
    "mailbox-list",
    "mailbox-mark-delivered",
    "mailbox-mark-notified",
    "create-task",
    "read-task",
    "list-tasks",
    "update-task",
    "claim-task",
    "transition-task-status",
    "release-task-claim",
    "read-config",
    "read-manifest",
    "read-worker-status",
    "read-worker-heartbeat",
    "update-worker-heartbeat",
    "write-worker-inbox",
    "write-worker-identity",
    "append-event",
    "read-events",
    "await-event",
    "read-idle-state",
    "read-stall-state",
    "get-summary",
    "cleanup",
    "orphan-cleanup",
    "write-shutdown-request",
    "read-shutdown-ack",
    "read-monitor-snapshot",
    "write-monitor-snapshot",
    "read-task-approval",
    "write-task-approval",
)

P0_OPERATIONS: tuple[str, ...] = (
    "send-message",
    "mailbox-list",
    "mailbox-mark-delivered",
    "create-task",
    "list-tasks",
    "claim-task",
    "transition-task-status",
    "release-task-claim",
    "get-summary",
    "read-config",
    "write-worker-inbox",
)

TeamApiEnvelope = dict[str, Any]
Handler = Callable[[Path, dict[str, Any]], TeamApiEnvelope]


class TeamApiError(RuntimeError):
    """Structured team-api failure with envelope fields."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = 1,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = dict(details or {})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fail(
    operation: str,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> TeamApiEnvelope:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = dict(details)
    return {"ok": False, "operation": operation, "error": error}


def _ok(operation: str, data: Mapping[str, Any]) -> TeamApiEnvelope:
    return {"ok": True, "operation": operation, "data": dict(data)}


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            f"{key} is required",
            exit_code=2,
        )
    return value.strip()


def _optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if not isinstance(value, str):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            f"{key} must be a string when provided",
            exit_code=2,
        )
    stripped = value.strip()
    return stripped or None


def _resolve_team_id(payload: Mapping[str, Any]) -> str:
    team_id = _optional_str(payload, "team_id") or _optional_str(payload, "team_name")
    if not team_id:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "team_id or team_name is required",
            exit_code=2,
        )
    return require_safe_id(team_id, label="team_id")


def _resolve_run_id(payload: Mapping[str, Any], root: Path) -> str:
    run_id = _optional_str(payload, "run_id")
    if run_id:
        return require_safe_id(run_id, label="run_id")
    from omg_cli.state import load_active_run

    active = load_active_run(root)
    if active is None:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "run_id is required (no active run)",
            exit_code=2,
        )
    return require_safe_id(str(active["run_id"]), label="run_id")


def _require_control_plane(root: Path, run_id: str) -> dict[str, Any]:
    """Fail closed unless CLI-stamped ``team.json`` exists for this run.

    Prevents detached fake mailbox/task stores that look authoritative without
    an experimental team plane control-plane record.
    """
    try:
        return load_team_meta(root, run_id)
    except TeamError as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"team control plane missing for run {run_id}: {exc}",
            details={"error": "team_not_found", "run_id": run_id},
        ) from exc


def _team_state_dir(root: Path, run_id: str, team_id: str) -> Path:
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
    )


def _api_config_path(root: Path, run_id: str, team_id: str) -> Path:
    return _team_state_dir(root, run_id, team_id) / "api-config.json"


def _tasks_dir(root: Path, run_id: str, team_id: str) -> Path:
    return _team_state_dir(root, run_id, team_id) / "tasks"


def _validate_task_id(task_id: str) -> str:
    if (
        isinstance(task_id, str)
        and task_id.isdigit()
        and 1 <= len(task_id) <= TASK_ID_MAX_DIGITS
        and not (len(task_id) > 1 and task_id.startswith("0"))
    ):
        return task_id
    raise TeamApiError(
        "E_TEAM_API_INVALID_INPUT",
        "task_id must be a positive integer digit string",
        exit_code=2,
    )


def _task_path(root: Path, run_id: str, team_id: str, task_id: str) -> Path:
    _validate_task_id(task_id)
    return _tasks_dir(root, run_id, team_id) / f"task-{task_id}.json"


def _worker_dir(root: Path, run_id: str, team_id: str, worker: str) -> Path:
    require_safe_id(worker, label="worker")
    return (
        _team_state_dir(root, run_id, team_id)
        / "workers"
        / safe_path_key(worker, namespace="worker")
    )


def _empty_config(run_id: str, team_id: str) -> dict[str, Any]:
    return {
        "store_kind": "team_api_config",
        "schema_version": 1,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "next_task_id": 1,
        "workers": [],
        "updated_at": _utc_now(),
    }


def _validate_config(
    value: Mapping[str, Any], *, run_id: str, team_id: str
) -> dict[str, Any]:
    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "next_task_id",
        "workers",
        "updated_at",
    }
    if set(row) != required:
        raise ContractValidationError("team api-config keys mismatch")
    if (
        row["store_kind"] != "team_api_config"
        or row["schema_version"] != 1
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("team api-config header mismatch")
    if row["run_id"] != run_id or row["team_id"] != team_id:
        raise ContractValidationError("team api-config identity mismatch")
    require_integer(row["next_task_id"], label="next_task_id", minimum=1)
    workers = row["workers"]
    if not isinstance(workers, list):
        raise ContractValidationError("team api-config workers must be a list")
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for item in workers:
        if not isinstance(item, Mapping):
            raise ContractValidationError("team api-config worker must be an object")
        name = require_safe_id(item.get("name"), label="worker.name")
        if name in seen:
            raise ContractValidationError("team api-config duplicate worker")
        seen.add(name)
        normalized.append({"name": name})
    row["workers"] = normalized
    return row


def _load_config(root: Path, run_id: str, team_id: str) -> dict[str, Any] | None:
    path = _api_config_path(root, run_id, team_id)
    if not path.exists():
        return None
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "api-config is corrupt",
            details={"error": "corrupt_config"},
        )
    try:
        return _validate_config(parsed, run_id=run_id, team_id=team_id)
    except ContractValidationError as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            str(exc),
            details={"error": "corrupt_config"},
        ) from exc


def _write_config(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(config["run_id"])
    team_id = str(config["team_id"])
    row = _validate_config(config, run_id=run_id, team_id=team_id)
    path = _api_config_path(root, run_id, team_id)
    ensure_managed_dir(path.parent)
    atomic_write_bytes(
        path, canonical_json_bytes(row), mode=DATA_FILE_MODE, replace=True
    )
    return row


def _merge_config_workers(
    current: dict[str, Any], workers: list[str] | None
) -> dict[str, Any]:
    if not workers:
        return current
    known = {item["name"] for item in current["workers"]}
    merged = list(current["workers"])
    for name in workers:
        require_safe_id(name, label="worker")
        if name not in known:
            merged.append({"name": name})
            known.add(name)
    return {
        **current,
        "workers": merged,
        "updated_at": _utc_now(),
    }


def _ensure_config_locked(
    root: Path,
    run_id: str,
    team_id: str,
    *,
    workers: list[str] | None = None,
) -> dict[str, Any]:
    """Caller must already hold the api-config exclusive lock."""

    current = _load_config(root, run_id, team_id)
    if current is None:
        current = _empty_config(run_id, team_id)
    current = _merge_config_workers(current, workers)
    return _write_config(root, current)


def _ensure_config(
    root: Path,
    run_id: str,
    team_id: str,
    *,
    workers: list[str] | None = None,
) -> dict[str, Any]:
    path = _api_config_path(root, run_id, team_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        return _ensure_config_locked(
            root, run_id, team_id, workers=workers
        )


def _require_worker_in_config(config: Mapping[str, Any], worker: str) -> None:
    names = {item["name"] for item in config.get("workers") or []}
    if worker not in names:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"worker {worker!r} not found",
            details={"error": "worker_not_found"},
        )


def _is_terminal(status: str) -> bool:
    return status in TERMINAL_TASK_STATUSES


def _can_transition(src: str, dst: str) -> bool:
    return dst in TASK_STATUS_TRANSITIONS.get(src, frozenset())


def _lease_expired(claim: Mapping[str, Any] | None) -> bool:
    if not claim or not claim.get("leased_until"):
        return False
    raw = str(claim["leased_until"])
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return stamp <= datetime.now(timezone.utc)


def _normalize_task(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    status = str(row.get("status") or "pending")
    if status not in TEAM_TASK_STATUSES:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"invalid task status {status!r}",
            details={"error": "corrupt_task"},
        )
    task_id = _validate_task_id(str(row.get("id") or ""))
    depends = row.get("depends_on")
    if depends is None:
        depends = row.get("blocked_by") or []
    if not isinstance(depends, list) or not all(isinstance(x, str) for x in depends):
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "task depends_on must be a string array",
            details={"error": "corrupt_task"},
        )
    version = row.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "task version must be a positive integer",
            details={"error": "corrupt_task"},
        )
    claim = row.get("claim")
    if claim is not None and not isinstance(claim, Mapping):
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "task claim must be an object or null",
            details={"error": "corrupt_task"},
        )
    return {
        "id": task_id,
        "subject": str(row.get("subject") or ""),
        "description": str(row.get("description") or ""),
        "status": status,
        "created_at": str(row.get("created_at") or _utc_now()),
        "depends_on": list(depends),
        "blocked_by": list(depends),
        "version": version,
        "owner": row.get("owner"),
        "claim": dict(claim) if isinstance(claim, Mapping) else None,
        "result": row.get("result"),
        "error": row.get("error"),
        "completed_at": row.get("completed_at"),
        "requires_code_change": bool(row.get("requires_code_change", False)),
    }


def _write_task(
    root: Path, run_id: str, team_id: str, task: Mapping[str, Any]
) -> dict[str, Any]:
    normalized = _normalize_task(task)
    path = _task_path(root, run_id, team_id, normalized["id"])
    ensure_managed_dir(path.parent)
    atomic_write_bytes(
        path,
        canonical_json_bytes(normalized),
        mode=DATA_FILE_MODE,
        replace=True,
    )
    return normalized


def _read_task(
    root: Path, run_id: str, team_id: str, task_id: str
) -> dict[str, Any] | None:
    path = _task_path(root, run_id, team_id, task_id)
    if not path.exists():
        return None
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "task file is corrupt",
            details={"error": "corrupt_task"},
        )
    return _normalize_task(parsed)


def _list_tasks(root: Path, run_id: str, team_id: str) -> list[dict[str, Any]]:
    directory = _tasks_dir(root, run_id, team_id)
    if not directory.exists():
        return []
    tasks: list[dict[str, Any]] = []
    for path in sorted(directory.glob("task-*.json")):
        stem = path.name[len("task-") : -len(".json")]
        task = _read_task(root, run_id, team_id, stem)
        if task is not None:
            tasks.append(task)
    tasks.sort(key=lambda item: int(item["id"]))
    return tasks


def _task_readiness(
    root: Path, run_id: str, team_id: str, task: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    deps = list(task.get("depends_on") or task.get("blocked_by") or [])
    incomplete: list[str] = []
    for dep_id in deps:
        dep = _read_task(root, run_id, team_id, str(dep_id))
        if dep is None or dep["status"] != "completed":
            incomplete.append(str(dep_id))
    return (not incomplete, incomplete)


def _op_send_message(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    sender = require_safe_id(_require_str(payload, "from_worker"), label="from_worker")
    recipient = require_safe_id(_require_str(payload, "to_worker"), label="to_worker")
    body = payload.get("body")
    if body is None or (isinstance(body, str) and not body.strip()):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "body is required",
            exit_code=2,
        )
    generation = payload.get("generation", 0)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "generation must be a non-negative integer",
            exit_code=2,
        )
    kind = _optional_str(payload, "kind") or "message"
    dedupe_key = _optional_str(payload, "dedupe_key")
    if not dedupe_key:
        dedupe_key = f"auto-{secrets.token_hex(8)}"
    require_safe_id(kind, label="kind")
    require_safe_id(dedupe_key, label="dedupe_key")
    try:
        message = send_message(
            root,
            run_id=run_id,
            team_id=team_id,
            sender_id=sender,
            recipient_id=recipient,
            generation=generation,
            kind=kind,
            body=body.strip() if isinstance(body, str) else body,
            dedupe_key=dedupe_key,
            message_id=_optional_str(payload, "message_id"),
        )
    except (MailboxError, ContractValidationError) as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            str(exc),
            details={"error": "mailbox_error"},
        ) from exc
    return _ok("send-message", {"message": message})


def _op_mailbox_list(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    worker = require_safe_id(_require_str(payload, "worker"), label="worker")
    after = payload.get("after")
    generation = payload.get("generation")
    limit = payload.get("limit", 100)
    try:
        listing = list_messages(
            root,
            run_id=run_id,
            team_id=team_id,
            recipient_id=worker,
            after=after,
            generation=generation if isinstance(generation, int) else None,
            limit=limit if isinstance(limit, int) else 100,
        )
    except (MailboxError, ContractValidationError) as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            str(exc),
            details={"error": "mailbox_error"},
        ) from exc
    messages = listing["messages"]
    return _ok(
        "mailbox-list",
        {"worker": worker, "count": len(messages), "messages": messages, **listing},
    )


def _op_mailbox_mark_delivered(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    worker = require_safe_id(_require_str(payload, "worker"), label="worker")
    message_id = require_safe_id(
        _require_str(payload, "message_id"), label="message_id"
    )
    try:
        listing = list_messages(
            root, run_id=run_id, team_id=team_id, recipient_id=worker, limit=512
        )
        message = read_message(
            root,
            run_id=run_id,
            team_id=team_id,
            recipient_id=worker,
            message_id=message_id,
        )
        expected_cursor = listing["ack_cursor"]
        if "expected_cursor" in payload:
            expected_cursor = payload["expected_cursor"]
        generation = message["generation"]
        if "generation" in payload:
            generation = int(payload["generation"])
        ack = ack_message(
            root,
            run_id=run_id,
            team_id=team_id,
            recipient_id=worker,
            message_id=message_id,
            expected_cursor=expected_cursor,
            generation=generation,
        )
    except (MailboxError, ContractValidationError, ValueError, TypeError) as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            str(exc),
            details={"error": "mailbox_error"},
        ) from exc
    return _ok(
        "mailbox-mark-delivered",
        {
            "worker": worker,
            "message_id": message_id,
            "updated": True,
            "ack": ack,
        },
    )


def _op_create_task(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    subject = _require_str(payload, "subject")
    description = _require_str(payload, "description")
    workers_raw = payload.get("workers") or []
    worker_names: list[str] = []
    if isinstance(workers_raw, list):
        for item in workers_raw:
            if isinstance(item, str):
                worker_names.append(require_safe_id(item.strip(), label="worker"))
            elif isinstance(item, Mapping) and item.get("name"):
                worker_names.append(
                    require_safe_id(str(item["name"]).strip(), label="worker")
                )
    owner = _optional_str(payload, "owner")
    if owner:
        worker_names.append(require_safe_id(owner, label="owner"))
    blocked_by = payload.get("blocked_by") or payload.get("depends_on") or []
    if not isinstance(blocked_by, list) or not all(
        isinstance(item, str) for item in blocked_by
    ):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "blocked_by must be a string array when provided",
            exit_code=2,
        )
    depends_on = [_validate_task_id(item.strip()) for item in blocked_by]
    requires = payload.get("requires_code_change", False)
    if requires is not None and not isinstance(requires, bool):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "requires_code_change must be a boolean when provided",
            exit_code=2,
        )

    config_path = _api_config_path(root, run_id, team_id)
    ensure_managed_dir(config_path.parent)
    with exclusive_lock(config_path.with_suffix(".lock")):
        config = _ensure_config_locked(
            root, run_id, team_id, workers=worker_names
        )
        next_id = int(config["next_task_id"])
        while _task_path(root, run_id, team_id, str(next_id)).exists():
            next_id += 1
        task_id = str(next_id)
        task = _write_task(
            root,
            run_id,
            team_id,
            {
                "id": task_id,
                "subject": subject,
                "description": description,
                "status": "pending",
                "created_at": _utc_now(),
                "depends_on": list(depends_on),
                "blocked_by": list(depends_on),
                "version": 1,
                "owner": owner,
                "claim": None,
                "requires_code_change": bool(requires),
            },
        )
        _write_config(
            root,
            {
                **config,
                "next_task_id": next_id + 1,
                "updated_at": _utc_now(),
            },
        )
    return _ok("create-task", {"task": task})


def _op_list_tasks(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    tasks = _list_tasks(root, run_id, team_id)
    return _ok("list-tasks", {"count": len(tasks), "tasks": tasks})


def _op_claim_task(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    task_id = _validate_task_id(_require_str(payload, "task_id"))
    worker = require_safe_id(_require_str(payload, "worker"), label="worker")
    expected_version = payload.get("expected_version")
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "expected_version must be a positive integer when provided",
            exit_code=2,
        )

    path = _task_path(root, run_id, team_id, task_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        config = _load_config(root, run_id, team_id)
        if config is None:
            return _ok("claim-task", {"ok": False, "error": "team_not_found"})
        _require_worker_in_config(config, worker)
        task = _read_task(root, run_id, team_id, task_id)
        if task is None:
            return _ok("claim-task", {"ok": False, "error": "task_not_found"})
        ready, deps = _task_readiness(root, run_id, team_id, task)
        if not ready:
            return _ok(
                "claim-task",
                {"ok": False, "error": "blocked_dependency", "dependencies": deps},
            )
        if expected_version is not None and task["version"] != expected_version:
            return _ok("claim-task", {"ok": False, "error": "claim_conflict"})
        if _is_terminal(task["status"]):
            return _ok("claim-task", {"ok": False, "error": "already_terminal"})

        if task["status"] == "in_progress":
            if not _lease_expired(task.get("claim")):
                return _ok("claim-task", {"ok": False, "error": "claim_conflict"})
            task["owner"] = None
            task["claim"] = None
            task["status"] = "pending"

        if task["status"] in {"pending", "blocked"}:
            claim = task.get("claim")
            if claim and not _lease_expired(claim):
                return _ok("claim-task", {"ok": False, "error": "claim_conflict"})
            if task.get("owner") and task["owner"] != worker:
                return _ok("claim-task", {"ok": False, "error": "claim_conflict"})

        token = str(uuid.uuid4())
        leased_until = (
            datetime.now(timezone.utc) + timedelta(seconds=CLAIM_LEASE_SECONDS)
        ).isoformat().replace("+00:00", "Z")
        updated = _write_task(
            root,
            run_id,
            team_id,
            {
                **task,
                "status": "in_progress",
                "owner": worker,
                "claim": {
                    "owner": worker,
                    "token": token,
                    "leased_until": leased_until,
                },
                "version": task["version"] + 1,
            },
        )
    return _ok("claim-task", {"ok": True, "task": updated, "claimToken": token})


def _op_transition_task_status(
    root: Path, payload: dict[str, Any]
) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    task_id = _validate_task_id(_require_str(payload, "task_id"))
    src = _require_str(payload, "from")
    dst = _require_str(payload, "to")
    claim_token = _require_str(payload, "claim_token")
    if src not in TEAM_TASK_STATUSES or dst not in TEAM_TASK_STATUSES:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "from and to must be valid task statuses",
            exit_code=2,
        )
    result = payload.get("result")
    error = payload.get("error")
    if result is not None and not isinstance(result, str):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "result must be a string when provided",
            exit_code=2,
        )
    if error is not None and not isinstance(error, str):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "error must be a string when provided",
            exit_code=2,
        )
    if not _can_transition(src, dst):
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "invalid_transition",
            details={"error": "invalid_transition"},
        )

    path = _task_path(root, run_id, team_id, task_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        task = _read_task(root, run_id, team_id, task_id)
        if task is None:
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                "task_not_found",
                details={"error": "task_not_found"},
            )
        if _is_terminal(task["status"]):
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                "already_terminal",
                details={"error": "already_terminal"},
            )
        if task["status"] != src or not _can_transition(task["status"], dst):
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                "invalid_transition",
                details={"error": "invalid_transition"},
            )
        claim = task.get("claim") or {}
        if (
            not task.get("owner")
            or not claim
            or claim.get("owner") != task.get("owner")
            or claim.get("token") != claim_token
        ):
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                "claim_conflict",
                details={"error": "claim_conflict"},
            )
        if _lease_expired(claim):
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                "lease_expired",
                details={"error": "lease_expired"},
            )
        updated = _write_task(
            root,
            run_id,
            team_id,
            {
                **task,
                "status": dst,
                "completed_at": _utc_now(),
                "result": result if dst == "completed" else None,
                "error": error if dst == "failed" else None,
                "claim": None,
                "version": task["version"] + 1,
            },
        )
    return _ok("transition-task-status", {"ok": True, "task": updated})


def _op_release_task_claim(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    task_id = _validate_task_id(_require_str(payload, "task_id"))
    claim_token = _require_str(payload, "claim_token")
    worker = require_safe_id(_require_str(payload, "worker"), label="worker")

    path = _task_path(root, run_id, team_id, task_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        task = _read_task(root, run_id, team_id, task_id)
        if task is None:
            return _ok("release-task-claim", {"ok": False, "error": "task_not_found"})
        if (
            task["status"] == "pending"
            and not task.get("claim")
            and not task.get("owner")
        ):
            return _ok("release-task-claim", {"ok": True, "task": task})
        if _is_terminal(task["status"]):
            return _ok(
                "release-task-claim", {"ok": False, "error": "already_terminal"}
            )
        claim = task.get("claim") or {}
        if (
            not task.get("owner")
            or not claim
            or claim.get("owner") != task.get("owner")
            or claim.get("token") != claim_token
            or claim.get("owner") != worker
        ):
            return _ok("release-task-claim", {"ok": False, "error": "claim_conflict"})
        if _lease_expired(claim):
            return _ok("release-task-claim", {"ok": False, "error": "lease_expired"})
        updated = _write_task(
            root,
            run_id,
            team_id,
            {
                **task,
                "status": "pending",
                "owner": None,
                "claim": None,
                "version": task["version"] + 1,
            },
        )
    return _ok("release-task-claim", {"ok": True, "task": updated})


def _op_read_config(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    plane = _require_control_plane(root, run_id)
    config = _load_config(root, run_id, team_id)
    if config is None:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "team_not_found",
            details={"error": "team_not_found"},
        )
    return _ok("read-config", {"config": config, "plane": plane})


def _op_get_summary(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    config = _load_config(root, run_id, team_id)
    if config is None:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "team_not_found",
            details={"error": "team_not_found"},
        )
    tasks = _list_tasks(root, run_id, team_id)
    counts = {
        "total": len(tasks),
        "pending": 0,
        "blocked": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
    }
    for task in tasks:
        status = task["status"]
        if status in counts:
            counts[status] += 1
    workers = [
        {
            "name": item["name"],
            "alive": False,
            "lastTurnAt": None,
            "turnsWithoutProgress": 0,
        }
        for item in config["workers"]
    ]
    summary = {
        "teamName": team_id,
        "run_id": run_id,
        "workerCount": len(config["workers"]),
        "tasks": counts,
        "workers": workers,
        "nonReportingWorkers": [item["name"] for item in config["workers"]],
    }
    return _ok("get-summary", {"summary": summary})


def _op_write_worker_inbox(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    worker = require_safe_id(_require_str(payload, "worker"), label="worker")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "content is required",
            exit_code=2,
        )
    config = _load_config(root, run_id, team_id)
    if config is None:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "team_not_found",
            details={"error": "team_not_found"},
        )
    _require_worker_in_config(config, worker)
    path = _worker_dir(root, run_id, team_id, worker) / "inbox.md"
    ensure_managed_dir(path.parent)
    # Leader/CLI-owned write only — workers must not self-write this path.
    atomic_write_bytes(
        path, content.encode("utf-8"), mode=DATA_FILE_MODE, replace=True
    )
    return _ok("write-worker-inbox", {"worker": worker, "path": str(path)})


_HANDLERS: dict[str, Handler] = {
    "send-message": _op_send_message,
    "mailbox-list": _op_mailbox_list,
    "mailbox-mark-delivered": _op_mailbox_mark_delivered,
    "create-task": _op_create_task,
    "list-tasks": _op_list_tasks,
    "claim-task": _op_claim_task,
    "transition-task-status": _op_transition_task_status,
    "release-task-claim": _op_release_task_claim,
    "get-summary": _op_get_summary,
    "read-config": _op_read_config,
    "write-worker-inbox": _op_write_worker_inbox,
}


def execute_team_api(
    operation: str,
    input_payload: Mapping[str, Any] | None,
    *,
    root: Path | str,
    env: Mapping[str, str] | None = None,
) -> tuple[int, TeamApiEnvelope]:
    """Dispatch one team-api operation. Returns ``(exit_code, envelope)``."""

    op = (operation or "").strip()
    payload = dict(input_payload or {})
    root_path = Path(root).resolve()

    if not experimental_enabled(env):
        return 2, _fail(
            op or "unknown",
            "E_TEAM_API_GATE",
            f"omg team api requires {EXPERIMENTAL_ENV}=1",
        )

    if in_spawned_worker_context(env):
        return 2, _fail(
            op or "unknown",
            "E_TEAM_API_GATE",
            "omg team api refused: already inside a spawned-worker context "
            f"(depth-1; one of {', '.join(WORKER_ENV_MARKERS)} is set). "
            "Workers must not mutate team mailbox/task stores via team api.",
        )

    if not op:
        return 2, _fail(
            "unknown",
            "E_TEAM_API_UNKNOWN",
            "operation is required",
        )

    if op not in TEAM_API_OPERATIONS:
        return 2, _fail(
            op,
            "E_TEAM_API_UNKNOWN",
            f"unknown team api operation: {op}",
        )

    if op not in P0_OPERATIONS:
        return 2, _fail(
            op,
            "E_TEAM_API_UNIMPLEMENTED",
            f"operation {op!r} is not in the P0 subset "
            f"({len(P0_OPERATIONS)}/{len(TEAM_API_OPERATIONS)} OMX ops)",
        )

    handler = _HANDLERS.get(op)
    if handler is None:  # pragma: no cover
        return 2, _fail(
            op,
            "E_TEAM_API_UNIMPLEMENTED",
            f"operation {op!r} handler missing",
        )

    try:
        # Control-plane gate before any mailbox/task mutation or read that
        # could materialize detached authoritative-looking state.
        run_id = _resolve_run_id(payload, root_path)
        _require_control_plane(root_path, run_id)
        envelope = handler(root_path, payload)
        data = envelope.get("data")
        if (
            envelope.get("ok") is True
            and isinstance(data, Mapping)
            and data.get("ok") is False
        ):
            return 1, _fail(
                op,
                "E_TEAM_API_FAILED",
                str(data.get("error") or "operation failed"),
                details=dict(data),
            )
        return 0, envelope
    except TeamGateError as exc:
        return 2, _fail(op, "E_TEAM_API_GATE", str(exc))
    except TeamApiError as exc:
        return exc.exit_code, _fail(
            op, exc.code, exc.message, details=exc.details or None
        )
    except (MailboxError, ContractValidationError, ValueError) as exc:
        return 1, _fail(
            op,
            "E_TEAM_API_FAILED",
            str(exc),
            details={"error": "contract_error"},
        )


def parse_input_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            f"--input is not valid JSON: {exc}",
            exit_code=2,
        ) from exc
    if not isinstance(parsed, dict):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "--input must be a JSON object",
            exit_code=2,
        )
    return parsed


__all__ = [
    "P0_OPERATIONS",
    "TEAM_API_OPERATIONS",
    "TeamApiError",
    "execute_team_api",
    "parse_input_json",
]
