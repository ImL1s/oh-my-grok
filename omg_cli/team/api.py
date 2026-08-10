"""OMX-shaped ``omg team api`` façade (P0 / P0′ reliability subset).

Durable mailbox/task mutations go through the CLI-owned stores under
``.omg/state/runs/<run_id>/team/<team_key>/``. Workers never write mailbox or
task files directly.

P0 + P0′ ops (mailbox/task CRUD including claim renew/release, heartbeat/
shutdown/orphan, events, manifest) are implemented; remaining
``TEAM_API_OPERATIONS`` return ``E_TEAM_API_UNIMPLEMENTED``. Operation names
and metadata come from ``omg_cli.team.operation_catalog`` (default schema
v2; v1 golden remains frozen). Full OMX catalog parity is intentionally
not claimed — see ``omg team api catalog`` /
``docs/team-operation-catalog-v2.md``.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

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
from omg_cli.team.operation_catalog import (
    P0_OPERATIONS,
    TEAM_API_OPERATIONS,
    WORKER_ALLOWED_OPS,
    WORKER_DENIED_OPS,
)
from omg_cli.team.plane import (
    DISABLE_ENV,
    EXPERIMENTAL_ENV,
    TEAM_ID_ENV,
    TEAM_LEADER_ROOT_ENV,
    TEAM_OWNER_TOKEN_ENV,
    TEAM_RUN_ID_ENV,
    TEAM_STATE_ROOT_ENV,
    TEAM_WORKER_ENV,
    TEAM_WORKER_ID_ENV,
    TeamError,
    TeamGateError,
    experimental_enabled,
    in_non_team_spawn_context,
    in_spawned_worker_context,
    load_team_meta,
    team_worker_identity,
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


_TEAM_API_WORKER_CONTEXT_FIELDS = (
    TEAM_WORKER_ENV,
    TEAM_WORKER_ID_ENV,
    TEAM_RUN_ID_ENV,
    TEAM_ID_ENV,
    TEAM_LEADER_ROOT_ENV,
    TEAM_STATE_ROOT_ENV,
    TEAM_OWNER_TOKEN_ENV,
)


def team_api_worker_context_present(
    env: Mapping[str, str] | None = None,
) -> bool:
    """Whether any team-worker routing field is present.

    Partial fields must not silently fall through to leader semantics.  This is
    an environment-consistency check, not actor authentication.
    """
    source = env if env is not None else os.environ
    return any(
        (source.get(name) or "").strip()
        for name in _TEAM_API_WORKER_CONTEXT_FIELDS
    )


def _worker_routing_error(
    message: str, *, missing: list[str] | None = None
) -> TeamApiError:
    details: dict[str, Any] = {"error": "worker_leader_root_invalid"}
    if missing:
        details["missing"] = missing
    return TeamApiError(
        "E_TEAM_API_GATE",
        f"omg team api refused: {message}",
        exit_code=2,
        details=details,
    )


def _absolute_existing_dir(raw: str, *, label: str) -> tuple[Path, Path]:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise _worker_routing_error(f"{label} must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _worker_routing_error(f"{label} is not usable") from exc
    if not resolved.is_dir():
        raise _worker_routing_error(f"{label} is not a directory")
    return candidate, resolved


def resolve_team_api_cli_root(
    default_root: Path | str,
    *,
    explicit_root: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Route a worker CLI call from its task worktree to the leader store.

    Launcher-provided roots are consistency hints only.  Same-UID processes can
    alter their environment, so this does not authenticate team membership.
    """
    source = env if env is not None else os.environ
    fallback = Path(default_root).resolve()
    if not team_api_worker_context_present(source):
        return fallback

    marker = (source.get(TEAM_WORKER_ENV) or "").strip().lower()
    required = {
        TEAM_WORKER_ENV: marker in {"1", "true", "yes", "on"},
        **{
            name: bool((source.get(name) or "").strip())
            for name in _TEAM_API_WORKER_CONTEXT_FIELDS
            if name != TEAM_WORKER_ENV
        },
    }
    missing = [name for name, valid in required.items() if not valid]
    if missing:
        raise _worker_routing_error(
            "incomplete worker routing environment", missing=missing
        )

    _, leader_root = _absolute_existing_dir(
        (source.get(TEAM_LEADER_ROOT_ENV) or "").strip(),
        label=TEAM_LEADER_ROOT_ENV,
    )
    control_dir = leader_root / ".omg"
    if not control_dir.is_dir() or control_dir.is_symlink():
        raise _worker_routing_error("leader root has no real .omg control plane")

    state_input, state_root = _absolute_existing_dir(
        (source.get(TEAM_STATE_ROOT_ENV) or "").strip(),
        label=TEAM_STATE_ROOT_ENV,
    )
    try:
        expected_state = (control_dir / "state").resolve(strict=True)
    except OSError as exc:
        raise _worker_routing_error("leader state root is not usable") from exc
    if state_input.is_symlink() or state_root != expected_state:
        raise _worker_routing_error(
            f"{TEAM_STATE_ROOT_ENV} does not match leader state"
        )

    for label, raw_root in (
        ("--project-root", explicit_root),
        ("OMG_PROJECT_ROOT", source.get("OMG_PROJECT_ROOT")),
    ):
        if raw_root is None or not str(raw_root).strip():
            continue
        try:
            requested = Path(str(raw_root)).expanduser().resolve(strict=True)
        except OSError as exc:
            raise _worker_routing_error(
                f"{label} is not usable in worker context"
            ) from exc
        if requested != leader_root:
            raise _worker_routing_error(
                f"{label} differs from {TEAM_LEADER_ROOT_ENV}"
            )
    return leader_root


def _now_utc() -> datetime:
    """Timezone-aware clock seam for claim lease deadlines (tests monkeypatch)."""
    return datetime.now(timezone.utc)


def _utc_now() -> str:
    return _now_utc().isoformat().replace("+00:00", "Z")


def _format_lease_deadline(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_leased_until(claim: Mapping[str, Any] | None) -> datetime | None:
    """Return a timezone-aware UTC deadline, or None if missing/malformed/naive."""
    if not claim:
        return None
    raw = claim.get("leased_until")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc)


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
    an experimental team plane control-plane record. ``writer`` alone is not
    enough — require schema/run binding fields that dry-run/live start write.
    """
    try:
        meta = load_team_meta(root, run_id)
    except TeamError as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"team control plane missing for run {run_id}: {exc}",
            details={"error": "team_not_found", "run_id": run_id},
        ) from exc

    required = ("schema_version", "run_id", "session", "tasks", "task_count", "writer")
    missing = [key for key in required if key not in meta]
    if missing:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"team.json missing required control-plane fields: {', '.join(missing)}",
            details={"error": "team_not_found", "run_id": run_id, "missing": missing},
        )
    if meta.get("run_id") != run_id:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"team.json run_id mismatch (file={meta.get('run_id')!r} path={run_id!r})",
            details={"error": "team_not_found", "run_id": run_id},
        )
    if meta.get("schema_version") not in (1, 2):
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"team.json schema_version unsupported: {meta.get('schema_version')!r}",
            details={"error": "team_not_found", "run_id": run_id},
        )
    session = meta.get("session")
    if not isinstance(session, str) or not session.strip():
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "team.json session must be a non-empty string",
            details={"error": "team_not_found", "run_id": run_id},
        )
    tasks = meta.get("tasks")
    if not isinstance(tasks, list) or len(tasks) < 1:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "team.json tasks must be a non-empty list",
            details={"error": "team_not_found", "run_id": run_id},
        )
    task_count = meta.get("task_count")
    if (
        isinstance(task_count, bool)
        or not isinstance(task_count, int)
        or task_count != len(tasks)
    ):
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "team.json task_count must equal len(tasks)",
            details={"error": "team_not_found", "run_id": run_id},
        )
    return meta


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
    """True when a claim object has no usable unexpired lease.

    A missing claim object is not an active lease defender (returns False so
    callers that gate on ``claim and not _lease_expired(claim)`` stay correct).
    A present claim with missing/malformed/timezone-naive ``leased_until`` is
    fail-closed as expired — never immortal.
    """
    if not claim:
        return False
    stamp = _parse_leased_until(claim)
    if stamp is None:
        return True
    return stamp <= _now_utc()


def _classify_claim_for_reconcile(
    task: Mapping[str, Any],
    *,
    cutoff: datetime,
) -> Literal["unchanged", "preserve_unexpired", "release_expired"]:
    """Classify one normalized task for leader-resume claim reconciliation.

    Raises :class:`TeamApiError` on inconsistent claim/status shapes. Never
    infers worker liveness; lease authority alone decides preserve vs release.
    """
    task_id = str(task.get("id") or "")
    status = str(task.get("status") or "")
    claim = task.get("claim")
    owner = task.get("owner")

    def _refuse(invariant: str) -> None:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"task {task_id!r}: {invariant}",
            details={
                "error": "corrupt_claim",
                "task_id": task_id,
                "invariant": invariant,
            },
        )

    if status == "in_progress":
        if not isinstance(claim, Mapping) or not claim:
            _refuse("in_progress_missing_claim")
            raise AssertionError("unreachable")  # pragma: no cover
        claim_map = claim
        claim_owner = claim_map.get("owner")
        token = claim_map.get("token")
        # Fail closed: owner + claim.owner must be identical nonempty safe
        # string worker IDs (reject int/bool/path-like poison).
        if not isinstance(owner, str) or not owner:
            _refuse("non_string_owner")
        if not isinstance(claim_owner, str) or not claim_owner:
            _refuse("non_string_claim_owner")
        if owner != claim_owner:
            _refuse("owner_claim_mismatch")
        try:
            require_safe_id(owner, label="owner")
            require_safe_id(claim_owner, label="claim.owner")
        except ContractValidationError:
            _refuse("unsafe_owner")
        # Token must be a canonical nonempty string (reject false/non-string,
        # whitespace-only, or padded tokens). Public claim ops normalize via
        # _require_str → strip(), so a padded/ws-only stored token can be
        # "preserved" by resume but never authenticated publicly.
        if (
            not isinstance(token, str)
            or not token.strip()
            or token != token.strip()
        ):
            _refuse("non_string_token")
        stamp = _parse_leased_until(claim_map)
        if stamp is None or stamp <= cutoff:
            return "release_expired"
        return "preserve_unexpired"

    if status in {"pending", "blocked", "completed", "failed"}:
        if claim is not None:
            _refuse(f"{status}_has_claim")
        return "unchanged"

    _refuse(f"unsupported_status:{status}")
    raise AssertionError("unreachable")  # pragma: no cover


def reconcile_task_claims(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
) -> dict[str, Any]:
    """Reconcile Team API task claims after leader restart (resume lifecycle).

    Not a catalog/MCP operation — invoked only from ``resume_for_identity``.
    Returns IDs and counts only (never claim tokens). Does not create API
    config or task directories.
    """
    root_path = Path(root).resolve()
    rid = require_safe_id(run_id, label="run_id")
    tid = require_safe_id(team_id, label="team_id")
    cutoff = _now_utc()

    empty = {
        "status": "not_materialized",
        "scanned": 0,
        "preserved_unexpired": [],
        "released_expired": [],
        "unchanged": [],
    }
    config_path = _api_config_path(root_path, rid, tid)
    tasks_directory = _tasks_dir(root_path, rid, tid)
    config_exists = config_path.exists()
    tasks_dir_exists = tasks_directory.exists()

    if not config_exists and not tasks_dir_exists:
        return empty

    if not config_exists and tasks_dir_exists:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "detached task files without api-config",
            details={
                "error": "detached_tasks",
                "run_id": rid,
                "team_id": tid,
            },
        )

    config = _load_config(root_path, rid, tid)
    if config is None:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "api-config missing after existence check",
            details={"error": "corrupt_config", "run_id": rid, "team_id": tid},
        )

    # Snapshot + normalize every task before any mutation (all-stop preflight).
    snapshot = _list_tasks(root_path, rid, tid)
    preflight: list[tuple[dict[str, Any], str]] = []
    for task in snapshot:
        action = _classify_claim_for_reconcile(task, cutoff=cutoff)
        preflight.append((task, action))

    preserved: list[str] = []
    released: list[str] = []
    unchanged: list[str] = []

    for task, action in preflight:
        task_id = str(task["id"])
        if action == "unchanged":
            unchanged.append(task_id)
            continue
        if action == "preserve_unexpired":
            preserved.append(task_id)
            continue

        # Candidate mutation: lock, reread, reclassify (renew may win).
        path = _task_path(root_path, rid, tid, task_id)
        with exclusive_lock(path.with_suffix(".lock")):
            current = _read_task(root_path, rid, tid, task_id)
            if current is None:
                raise TeamApiError(
                    "E_TEAM_API_FAILED",
                    f"task {task_id!r}: disappeared under lock",
                    details={
                        "error": "corrupt_claim",
                        "task_id": task_id,
                        "invariant": "task_missing_under_lock",
                    },
                )
            locked_action = _classify_claim_for_reconcile(current, cutoff=cutoff)
            if locked_action == "release_expired":
                _write_task(
                    root_path,
                    rid,
                    tid,
                    {
                        **current,
                        "status": "pending",
                        "owner": None,
                        "claim": None,
                        "version": int(current["version"]) + 1,
                    },
                )
                released.append(task_id)
            elif locked_action == "preserve_unexpired":
                preserved.append(task_id)
            else:
                unchanged.append(task_id)

    return {
        "status": "ok",
        "scanned": len(snapshot),
        "preserved_unexpired": sorted(preserved, key=int),
        "released_expired": sorted(released, key=int),
        "unchanged": sorted(unchanged, key=int),
    }


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
    binding = row.get("binding")
    if binding is not None and not isinstance(binding, Mapping):
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "task binding must be an object or null",
            details={"error": "corrupt_task"},
        )
    out: dict[str, Any] = {
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
    if isinstance(binding, Mapping):
        out["binding"] = dict(binding)
    return out


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
    task = _normalize_task(parsed)
    if task["id"] != task_id:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"task file stem {task_id!r} mismatches embedded id {task['id']!r}",
            details={
                "error": "corrupt_task",
                "invariant": "filename_body_id_mismatch",
                "task_id": task_id,
                "embedded_id": task["id"],
            },
        )
    return task


def _list_tasks(root: Path, run_id: str, team_id: str) -> list[dict[str, Any]]:
    directory = _tasks_dir(root, run_id, team_id)
    if not directory.exists():
        return []
    loaded: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(directory.glob("task-*.json")):
        stem = path.name[len("task-") : -len(".json")]
        _validate_task_id(stem)
        parsed = parse_canonical_json_bytes(path.read_bytes())
        if not isinstance(parsed, dict):
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                "task file is corrupt",
                details={"error": "corrupt_task", "task_id": stem},
            )
        task = _normalize_task(parsed)
        loaded.append((stem, task))

    # Duplicate embedded IDs across files (checked before stem bind so a
    # pair of mismatched files sharing one id still fail closed distinctly).
    seen_ids: dict[str, str] = {}
    for stem, task in loaded:
        embedded = task["id"]
        prior = seen_ids.get(embedded)
        if prior is not None:
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                f"duplicate task id {embedded!r} in stems {prior!r} and {stem!r}",
                details={
                    "error": "corrupt_task",
                    "invariant": "duplicate_task_id",
                    "task_id": embedded,
                    "stems": [prior, stem],
                },
            )
        seen_ids[embedded] = stem

    tasks: list[dict[str, Any]] = []
    for stem, task in loaded:
        if task["id"] != stem:
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                f"task file stem {stem!r} mismatches embedded id {task['id']!r}",
                details={
                    "error": "corrupt_task",
                    "invariant": "filename_body_id_mismatch",
                    "task_id": stem,
                    "embedded_id": task["id"],
                },
            )
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


def _claim_attempt_matches(
    task: Mapping[str, Any], payload: Mapping[str, Any]
) -> str | None:
    """Return an error code when payload attempt disagrees with binding."""
    binding = task.get("binding")
    if not isinstance(binding, Mapping) or binding.get("attempt") is None:
        return None
    try:
        bound = int(binding["attempt"])
    except (TypeError, ValueError):
        return "corrupt_binding_attempt"
    if "attempt" not in payload and "expected_attempt" not in payload:
        # Soft: binding present but caller omitted attempt — still allow when
        # claim token matches; replacement fences tokens first. Strict when
        # callers supply an attempt fence.
        return None
    raw = payload.get("attempt", payload.get("expected_attempt"))
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return "invalid_attempt"
    if int(raw) != bound:
        return "stale_attempt"
    return None


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


def _op_read_task(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    task_id = _validate_task_id(_require_str(payload, "task_id"))
    _require_control_plane(root, run_id)
    task = _read_task(root, run_id, team_id, task_id)
    if task is None:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"task {task_id!r} not found",
            details={"error": "task_not_found", "task_id": task_id},
        )
    ready, deps = _task_readiness(root, run_id, team_id, task)
    return _ok(
        "read-task",
        {
            "task": task,
            "ready": ready,
            "blocked_by_incomplete": deps,
        },
    )


def _op_update_task(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    """Patch non-lifecycle task fields with optional expected_version CAS.

    Status / owner / claim mutations stay on claim/transition/release ops.
    """
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    task_id = _validate_task_id(_require_str(payload, "task_id"))
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

    patchable = {
        "subject",
        "description",
        "depends_on",
        "blocked_by",
        "requires_code_change",
        "result",
        "error",
    }
    provided = {k: payload[k] for k in patchable if k in payload}
    if not provided:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "update-task requires at least one of: subject, description, "
            "depends_on/blocked_by, requires_code_change, result, error",
            exit_code=2,
        )

    if "depends_on" in provided or "blocked_by" in provided:
        raw_deps = provided.get("depends_on", provided.get("blocked_by"))
        if not isinstance(raw_deps, list) or not all(
            isinstance(item, str) for item in raw_deps
        ):
            raise TeamApiError(
                "E_TEAM_API_INVALID_INPUT",
                "depends_on/blocked_by must be a string array",
                exit_code=2,
            )
        depends = [_validate_task_id(item.strip()) for item in raw_deps]
        provided["depends_on"] = depends
        provided["blocked_by"] = list(depends)

    if "requires_code_change" in provided and not isinstance(
        provided["requires_code_change"], bool
    ):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "requires_code_change must be a boolean when provided",
            exit_code=2,
        )
    for text_key in ("subject", "description"):
        if text_key in provided and not isinstance(provided[text_key], str):
            raise TeamApiError(
                "E_TEAM_API_INVALID_INPUT",
                f"{text_key} must be a string when provided",
                exit_code=2,
            )

    path = _task_path(root, run_id, team_id, task_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        task = _read_task(root, run_id, team_id, task_id)
        if task is None:
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                f"task {task_id!r} not found",
                details={"error": "task_not_found", "task_id": task_id},
            )
        if expected_version is not None and task["version"] != expected_version:
            return _ok(
                "update-task",
                {
                    "ok": False,
                    "error": "version_conflict",
                    "task": task,
                    "expected_version": expected_version,
                },
            )
        if _is_terminal(task["status"]) and any(
            k in provided for k in ("subject", "description", "depends_on", "blocked_by")
        ):
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                f"task {task_id!r} is terminal ({task['status']}); "
                "cannot patch subject/description/depends_on",
                details={"error": "already_terminal", "task_id": task_id},
            )
        merged = {**task, **provided, "version": task["version"] + 1}
        updated = _write_task(root, run_id, team_id, merged)
    return _ok("update-task", {"ok": True, "task": updated})


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

        attempt_err = _claim_attempt_matches(task, payload)
        if attempt_err:
            return _ok("claim-task", {"ok": False, "error": attempt_err})

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
        leased_until = _format_lease_deadline(
            _now_utc() + timedelta(seconds=CLAIM_LEASE_SECONDS)
        )
        binding = dict(task.get("binding") or {})
        if binding:
            binding["logical_worker_id"] = binding.get("logical_worker_id") or worker
        claim_body: dict[str, Any] = {
            "owner": worker,
            "token": token,
            "leased_until": leased_until,
        }
        if binding.get("attempt") is not None:
            claim_body["attempt"] = int(binding["attempt"])
        updated = _write_task(
            root,
            run_id,
            team_id,
            {
                **task,
                "status": "in_progress",
                "owner": worker,
                "claim": claim_body,
                "binding": binding or task.get("binding"),
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
    worker = require_safe_id(_require_str(payload, "worker"), label="worker")
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
            or claim.get("owner") != worker
            or task.get("owner") != worker
        ):
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                "claim_conflict",
                details={"error": "claim_conflict"},
            )
        attempt_err = _claim_attempt_matches(task, payload)
        if attempt_err:
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                attempt_err,
                details={"error": attempt_err},
            )
        # Fenced prior-attempt tokens: claim.attempt must match binding when both set.
        binding = task.get("binding") if isinstance(task.get("binding"), Mapping) else None
        if (
            binding
            and binding.get("attempt") is not None
            and claim.get("attempt") is not None
            and int(claim["attempt"]) != int(binding["attempt"])
        ):
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                "stale_attempt",
                details={"error": "stale_attempt"},
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
        attempt_err = _claim_attempt_matches(task, payload)
        if attempt_err:
            return _ok("release-task-claim", {"ok": False, "error": attempt_err})
        binding = task.get("binding") if isinstance(task.get("binding"), Mapping) else None
        if (
            binding
            and binding.get("attempt") is not None
            and isinstance(claim, Mapping)
            and claim.get("attempt") is not None
            and int(claim["attempt"]) != int(binding["attempt"])
        ):
            return _ok("release-task-claim", {"ok": False, "error": "stale_attempt"})
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


def _op_renew_task_claim(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    """Extend an active claim lease without rotating the claim token."""
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    task_id = _validate_task_id(_require_str(payload, "task_id"))
    claim_token = _require_str(payload, "claim_token")
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
        task = _read_task(root, run_id, team_id, task_id)
        if task is None:
            return _ok("renew-task-claim", {"ok": False, "error": "task_not_found"})
        if _is_terminal(task["status"]):
            return _ok(
                "renew-task-claim", {"ok": False, "error": "already_terminal"}
            )
        if task["status"] != "in_progress":
            return _ok("renew-task-claim", {"ok": False, "error": "claim_conflict"})
        claim = task.get("claim") or {}
        if (
            not task.get("owner")
            or not claim
            or claim.get("owner") != task.get("owner")
            or claim.get("token") != claim_token
            or claim.get("owner") != worker
            or task.get("owner") != worker
        ):
            return _ok("renew-task-claim", {"ok": False, "error": "claim_conflict"})
        attempt_err = _claim_attempt_matches(task, payload)
        if attempt_err:
            return _ok("renew-task-claim", {"ok": False, "error": attempt_err})
        binding = task.get("binding") if isinstance(task.get("binding"), Mapping) else None
        if (
            binding
            and binding.get("attempt") is not None
            and claim.get("attempt") is not None
            and int(claim["attempt"]) != int(binding["attempt"])
        ):
            return _ok("renew-task-claim", {"ok": False, "error": "stale_attempt"})
        current_deadline = _parse_leased_until(claim)
        if current_deadline is None or current_deadline <= _now_utc():
            return _ok("renew-task-claim", {"ok": False, "error": "lease_expired"})
        if expected_version is not None and task["version"] != expected_version:
            return _ok("renew-task-claim", {"ok": False, "error": "version_conflict"})

        new_deadline = _now_utc() + timedelta(seconds=CLAIM_LEASE_SECONDS)
        if new_deadline <= current_deadline:
            return _ok(
                "renew-task-claim", {"ok": False, "error": "lease_not_advanced"}
            )

        renewed_claim = {
            **dict(claim),
            "owner": worker,
            "token": claim_token,
            "leased_until": _format_lease_deadline(new_deadline),
        }
        updated = _write_task(
            root,
            run_id,
            team_id,
            {
                **task,
                "status": "in_progress",
                "owner": worker,
                "claim": renewed_claim,
                "version": task["version"] + 1,
            },
        )
    return _ok(
        "renew-task-claim",
        {"ok": True, "task": updated, "claimToken": claim_token},
    )


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


def _op_read_manifest(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    """Return CLI-stamped team.json control-plane (manifest) for the run."""
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    plane = _require_control_plane(root, run_id)
    # team_id is required for OMX shape; control plane is run-scoped today.
    return _ok(
        "read-manifest",
        {
            "run_id": run_id,
            "team_id": team_id,
            "manifest": plane,
        },
    )


def _events_path(root: Path, run_id: str, team_id: str) -> Path:
    return _team_state_dir(root, run_id, team_id) / "events.jsonl"


def _op_append_event(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    _require_control_plane(root, run_id)
    kind = require_safe_id(_require_str(payload, "kind"), label="kind")
    body = payload.get("body")
    if body is None:
        body = {}
    if not isinstance(body, (dict, list, str, int, float, bool)) and body is not None:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "body must be JSON-serializable when provided",
            exit_code=2,
        )
    worker = _optional_str(payload, "worker")
    if worker:
        worker = require_safe_id(worker, label="worker")
    event_id = _optional_str(payload, "event_id") or f"evt-{secrets.token_hex(8)}"
    require_safe_id(event_id, label="event_id")
    event = {
        "event_id": event_id,
        "ts": _utc_now(),
        "kind": kind,
        "worker": worker,
        "body": body,
        "run_id": run_id,
        "team_id": team_id,
    }
    path = _events_path(root, run_id, team_id)
    ensure_managed_dir(path.parent)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with exclusive_lock(path.with_suffix(".lock")):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return _ok("append-event", {"event": event, "path": str(path)})


def _op_read_events(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    _require_control_plane(root, run_id)
    after = _optional_str(payload, "after")  # event_id exclusive cursor
    kind_filter = _optional_str(payload, "kind")
    if kind_filter:
        kind_filter = require_safe_id(kind_filter, label="kind")
    limit = payload.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "limit must be a positive integer when provided",
            exit_code=2,
        )
    limit = min(limit, 1000)
    path = _events_path(root, run_id, team_id)
    events: list[dict[str, Any]] = []
    if path.exists():
        seen_after = after is None
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            eid = str(row.get("event_id") or "")
            if not seen_after:
                if eid == after:
                    seen_after = True
                continue
            if kind_filter and str(row.get("kind") or "") != kind_filter:
                continue
            events.append(row)
            if len(events) >= limit:
                break
    return _ok(
        "read-events",
        {
            "count": len(events),
            "events": events,
            "after": after,
            "limit": limit,
        },
    )


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


def _shutdown_ack_path(root: Path, run_id: str, worker: str) -> Path:
    from omg_cli.team.plane import team_dir

    require_safe_id(worker, label="worker")
    return team_dir(root, run_id) / f"shutdown-ack-{safe_path_key(worker, namespace='worker')}.json"


def _op_update_worker_heartbeat(
    root: Path, payload: dict[str, Any]
) -> TeamApiEnvelope:
    from omg_cli.team.liveness import (
        LivenessError,
        initialize_liveness,
        load_liveness,
        record_heartbeat,
    )

    run_id = _require_str(payload, "run_id")
    team_id = _resolve_team_id(payload)
    worker = _require_str(payload, "worker")
    task_id = _require_str(payload, "task_id")
    generation = int(payload.get("generation") or 0)
    expected = payload.get("expected_sequence")
    if expected is None:
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "expected_sequence is required",
            exit_code=2,
        )
    expected_sequence = int(expected)
    try:
        row = load_liveness(
            root, run_id=run_id, team_id=team_id, task_id=task_id
        )
        if row is None:
            initialize_liveness(
                root,
                run_id=run_id,
                team_id=team_id,
                task_id=task_id,
                worker_id=worker,
                generation=generation,
            )
        updated = record_heartbeat(
            root,
            run_id=run_id,
            team_id=team_id,
            task_id=task_id,
            worker_id=worker,
            generation=generation,
            expected_sequence=expected_sequence,
        )
    except (LivenessError, ValueError, TypeError, ContractValidationError) as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            str(exc),
            details={"error": "heartbeat_failed"},
        ) from exc
    return _ok(
        "update-worker-heartbeat",
        {
            "worker": worker,
            "task_id": task_id,
            "heartbeat_sequence": updated.get("heartbeat_sequence"),
            "heartbeat_at": updated.get("heartbeat_at"),
        },
    )


def _op_read_worker_heartbeat(
    root: Path, payload: dict[str, Any]
) -> TeamApiEnvelope:
    from omg_cli.team.liveness import classify_liveness, load_liveness

    run_id = _require_str(payload, "run_id")
    team_id = _resolve_team_id(payload)
    task_id = _require_str(payload, "task_id")
    row = load_liveness(root, run_id=run_id, team_id=team_id, task_id=task_id)
    if row is None:
        return _ok(
            "read-worker-heartbeat",
            {"task_id": task_id, "present": False, "liveness": None},
        )
    return _ok(
        "read-worker-heartbeat",
        {
            "task_id": task_id,
            "present": True,
            "liveness": classify_liveness(row),
            "row": row,
        },
    )


def _op_read_worker_status(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    from omg_cli.team.runtime import worker_ready_path

    run_id = _require_str(payload, "run_id")
    team_id = _resolve_team_id(payload)
    worker = _require_str(payload, "worker")
    ready_path = worker_ready_path(
        root, run_id=run_id, team_id=team_id, worker_id=worker
    )
    process_ready = False
    ready_payload: dict[str, Any] | None = None
    if ready_path.is_file():
        try:
            data = json.loads(ready_path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and data.get("writer") == CLI_WRITER
                and data.get("kind") == "worker_ready"
            ):
                process_ready = True
                ready_payload = data
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            process_ready = False
    return _ok(
        "read-worker-status",
        {
            "worker": worker,
            "process_ready": process_ready,
            "ready": ready_payload,
            "ready_path": str(ready_path),
        },
    )


def _op_write_shutdown_request(
    root: Path, payload: dict[str, Any]
) -> TeamApiEnvelope:
    from omg_cli.team.plane import _list_in_progress_api_tasks, _write_shutdown_request

    run_id = _require_str(payload, "run_id")
    team_id = _resolve_team_id(payload)
    force = bool(payload.get("force") or False)
    in_progress = _list_in_progress_api_tasks(root, run_id, team_id)
    path = _write_shutdown_request(
        root,
        run_id,
        team_id=team_id,
        force=force,
        in_progress=in_progress,
    )
    return _ok(
        "write-shutdown-request",
        {
            "path": str(path),
            "force": force,
            "in_progress_count": len(in_progress),
        },
    )


def _op_read_shutdown_request(
    root: Path, payload: dict[str, Any]
) -> TeamApiEnvelope:
    from omg_cli.team.plane import team_shutdown_request_path

    run_id = _require_str(payload, "run_id")
    path = team_shutdown_request_path(root, run_id)
    if not path.is_file():
        return _ok("read-shutdown-request", {"present": False, "request": None})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"shutdown request unreadable: {exc}",
        ) from exc
    return _ok(
        "read-shutdown-request",
        {"present": True, "request": data, "path": str(path)},
    )


def _op_write_shutdown_ack(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _require_str(payload, "run_id")
    worker = _require_str(payload, "worker")
    path = _shutdown_ack_path(root, run_id, worker)
    ensure_managed_dir(path.parent)
    body = {
        "store_kind": "team_shutdown_ack",
        "schema_version": 1,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "worker": worker,
        "acked_at": _utc_now(),
        "note": str(payload.get("note") or "worker acknowledges shutdown"),
    }
    atomic_write_bytes(
        path,
        (json.dumps(body, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
        mode=DATA_FILE_MODE,
        replace=True,
    )
    return _ok("write-shutdown-ack", {"worker": worker, "path": str(path)})


def _op_read_shutdown_ack(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    run_id = _require_str(payload, "run_id")
    worker = _require_str(payload, "worker")
    path = _shutdown_ack_path(root, run_id, worker)
    if not path.is_file():
        return _ok(
            "read-shutdown-ack",
            {"worker": worker, "present": False, "ack": None},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"shutdown ack unreadable: {exc}",
        ) from exc
    return _ok(
        "read-shutdown-ack",
        {"worker": worker, "present": True, "ack": data, "path": str(path)},
    )


def _op_orphan_cleanup(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    """Best-effort: mark tasks whose recorded pgid is gone as stopped in meta.

    Does not pkill; only reconciles team.json task status for dead pids.
    """
    from omg_cli.team.plane import load_team_meta, mutate_team_meta

    run_id = _require_str(payload, "run_id")
    try:
        meta = load_team_meta(root, run_id)
    except Exception as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            f"team meta missing: {exc}",
            details={"error": "team_not_found"},
        ) from exc

    cleaned: list[str] = []
    still_alive: list[str] = []

    def _pgid_gone(pgid: int) -> bool:
        if pgid <= 0:
            return True
        try:
            os.killpg(pgid, 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return True

    tasks = list(meta.get("tasks") or [])
    for task in tasks:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id") or "")
        pgid = task.get("pgid")
        if pgid is None:
            continue
        try:
            pgid_i = int(pgid)
        except (TypeError, ValueError):
            continue
        if _pgid_gone(pgid_i):
            cleaned.append(tid)
        else:
            still_alive.append(tid)

    def _apply(current: dict[str, Any]) -> dict[str, Any]:
        updated = dict(current)
        new_tasks = []
        for task in updated.get("tasks") or []:
            if not isinstance(task, dict):
                new_tasks.append(task)
                continue
            row = dict(task)
            if str(row.get("task_id") or "") in cleaned:
                row["status"] = "stopped"
                row["orphan_cleaned_at"] = _utc_now()
            new_tasks.append(row)
        updated["tasks"] = new_tasks
        updated["orphan_cleanup_at"] = _utc_now()
        return updated

    if cleaned:
        try:
            mutate_team_meta(root, run_id, _apply)
        except Exception as exc:
            raise TeamApiError(
                "E_TEAM_API_FAILED",
                f"orphan cleanup meta mutate failed: {exc}",
            ) from exc

    return _ok(
        "orphan-cleanup",
        {
            "run_id": run_id,
            "cleaned_task_ids": cleaned,
            "alive_task_ids": still_alive,
            "note": "pgid gone → task status=stopped; no process kill issued",
        },
    )


def _op_replace_worker(root: Path, payload: dict[str, Any]) -> TeamApiEnvelope:
    """Leader-only identity-fenced worker replacement (#69 PR5)."""
    from omg_cli.team.replacement import ReplacementError, replace_worker

    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    worker = require_safe_id(
        _require_str(payload, "worker")
        if "worker" in payload
        else _require_str(payload, "worker_id"),
        label="worker",
    )
    mode = _require_str(payload, "mode").strip().lower()
    idempotency_key = _require_str(payload, "idempotency_key")
    expected_attempt = payload.get("expected_attempt")
    expected_launch_generation = payload.get("expected_launch_generation")
    if (
        isinstance(expected_attempt, bool)
        or not isinstance(expected_attempt, int)
        or expected_attempt < 1
    ):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "expected_attempt must be a positive integer",
            exit_code=2,
        )
    if (
        isinstance(expected_launch_generation, bool)
        or not isinstance(expected_launch_generation, int)
        or expected_launch_generation < 1
    ):
        raise TeamApiError(
            "E_TEAM_API_INVALID_INPUT",
            "expected_launch_generation must be a positive integer",
            exit_code=2,
        )
    dry_run = bool(payload.get("dry_run", False))
    try:
        result = replace_worker(
            root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker,
            mode=mode,  # type: ignore[arg-type]
            expected_attempt=expected_attempt,
            expected_launch_generation=expected_launch_generation,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )
    except ReplacementError as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            exc.message,
            details={"error": exc.code, "code": exc.code},
        ) from exc
    return _ok("replace-worker", result.to_dict())


def _op_read_presentation_state(
    root: Path, payload: dict[str, Any]
) -> TeamApiEnvelope:
    """Leader-only read-only presentation state projection (#69 PR6)."""
    from omg_cli.team.presentation import PresentationError, build_team_presentation_v1

    run_id = _resolve_run_id(payload, root)
    team_id = _resolve_team_id(payload)
    try:
        state = build_team_presentation_v1(root, run_id, team_id=team_id)
    except PresentationError as exc:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            exc.message,
            details={"error": exc.code, "code": exc.code},
        ) from exc
    return _ok("read-presentation-state", state)


_HANDLERS: dict[str, Handler] = {
    "send-message": _op_send_message,
    "mailbox-list": _op_mailbox_list,
    "mailbox-mark-delivered": _op_mailbox_mark_delivered,
    "create-task": _op_create_task,
    "read-task": _op_read_task,
    "list-tasks": _op_list_tasks,
    "update-task": _op_update_task,
    "claim-task": _op_claim_task,
    "transition-task-status": _op_transition_task_status,
    "release-task-claim": _op_release_task_claim,
    "renew-task-claim": _op_renew_task_claim,
    "get-summary": _op_get_summary,
    "read-config": _op_read_config,
    "read-manifest": _op_read_manifest,
    "write-worker-inbox": _op_write_worker_inbox,
    "update-worker-heartbeat": _op_update_worker_heartbeat,
    "read-worker-heartbeat": _op_read_worker_heartbeat,
    "read-worker-status": _op_read_worker_status,
    "write-shutdown-request": _op_write_shutdown_request,
    "read-shutdown-request": _op_read_shutdown_request,
    "write-shutdown-ack": _op_write_shutdown_ack,
    "read-shutdown-ack": _op_read_shutdown_ack,
    "orphan-cleanup": _op_orphan_cleanup,
    "append-event": _op_append_event,
    "read-events": _op_read_events,
    "replace-worker": _op_replace_worker,
    "read-presentation-state": _op_read_presentation_state,
}


def _bind_worker_env_field(
    out: dict[str, Any],
    *,
    field: str,
    env_value: str,
    label: str,
) -> None:
    """Fail closed on payload/env mismatch; otherwise inject env value."""
    claimed = out.get(field)
    if claimed is not None and str(claimed).strip() and str(claimed).strip() != env_value:
        raise TeamApiError(
            "E_TEAM_API_GATE",
            f"{label} must equal worker env identity {env_value!r}",
            exit_code=2,
            details={"error": "identity_mismatch", "field": field},
        )
    out[field] = env_value


def _apply_worker_identity_matrix(
    operation: str,
    payload: dict[str, Any],
    *,
    identity: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bind / restrict payload fields for a team-pane worker identity."""
    if operation in WORKER_DENIED_OPS or operation not in WORKER_ALLOWED_OPS:
        raise TeamApiError(
            "E_TEAM_API_GATE",
            f"worker {identity!r} is not allowed to run {operation!r}",
            exit_code=2,
            details={"error": "worker_op_denied", "worker": identity, "operation": operation},
        )
    source = env if env is not None else os.environ
    out = dict(payload)
    env_run = (source.get(TEAM_RUN_ID_ENV) or "").strip()
    env_team = (source.get(TEAM_ID_ENV) or "").strip()
    env_owner = (source.get(TEAM_OWNER_TOKEN_ENV) or "").strip()
    # Fail closed: worker identity matrix requires immutable run/team env.
    # Owner token stays optional (injected only when present).
    if not env_run or not env_team:
        missing = [
            name
            for name, value in (
                (TEAM_RUN_ID_ENV, env_run),
                (TEAM_ID_ENV, env_team),
            )
            if not value
        ]
        raise TeamApiError(
            "E_TEAM_API_GATE",
            "omg team api refused: worker identity requires "
            f"{TEAM_RUN_ID_ENV} and {TEAM_ID_ENV} "
            f"(missing: {', '.join(missing)})",
            exit_code=2,
            details={"error": "worker_env_incomplete", "missing": missing},
        )
    _bind_worker_env_field(out, field="run_id", env_value=env_run, label="run_id")
    _bind_worker_env_field(out, field="team_id", env_value=env_team, label="team_id")
    if env_owner:
        _bind_worker_env_field(
            out, field="owner_token", env_value=env_owner, label="owner_token"
        )
    else:
        # Do not trust client-supplied owner_token when pane env has none.
        out.pop("owner_token", None)
    if operation == "send-message":
        claimed = out.get("from_worker")
        if claimed is not None and str(claimed).strip() and str(claimed).strip() != identity:
            raise TeamApiError(
                "E_TEAM_API_GATE",
                f"from_worker must equal worker identity {identity!r}",
                exit_code=2,
                details={"error": "identity_mismatch"},
            )
        out["from_worker"] = identity
    if operation in ("mailbox-list", "mailbox-mark-delivered"):
        claimed = out.get("worker")
        if claimed is not None and str(claimed).strip() and str(claimed).strip() != identity:
            raise TeamApiError(
                "E_TEAM_API_GATE",
                f"mailbox worker must equal identity {identity!r}",
                exit_code=2,
                details={"error": "identity_mismatch"},
            )
        out["worker"] = identity
    if operation == "claim-task":
        claimed = out.get("worker")
        if claimed is not None and str(claimed).strip() and str(claimed).strip() != identity:
            raise TeamApiError(
                "E_TEAM_API_GATE",
                f"claim worker must equal identity {identity!r}",
                exit_code=2,
                details={"error": "identity_mismatch"},
            )
        out["worker"] = identity
    if operation in (
        "transition-task-status",
        "release-task-claim",
        "renew-task-claim",
    ):
        claimed = out.get("worker")
        if claimed is not None and str(claimed).strip() and str(claimed).strip() != identity:
            raise TeamApiError(
                "E_TEAM_API_GATE",
                f"worker must equal identity {identity!r}",
                exit_code=2,
                details={"error": "identity_mismatch"},
            )
        out["worker"] = identity
    if operation in (
        "update-worker-heartbeat",
        "write-shutdown-ack",
        "append-event",
    ):
        _bind_worker_env_field(
            out, field="worker", env_value=identity, label="worker"
        )
    if operation == "update-worker-heartbeat":
        # A team-pane worker may update only its own liveness key. Leader calls
        # bypass this matrix and may retain distinct task/host worker identities.
        _bind_worker_env_field(
            out, field="task_id", env_value=identity, label="task_id"
        )
    # Worker status and heartbeat reads remain unbound intentionally: their
    # target fields are team-visible selectors, not caller identities.
    # read-shutdown-ack is not worker-allowed and remains leader-only.
    return out


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
            f"omg team api disabled "
            f"({EXPERIMENTAL_ENV}=0 or {DISABLE_ENV}=1)",
        )

    # Process-fanout / spawned-subagent workers remain denied. Team panes may
    # use an identity-bound P0 subset (ACK/claim/transition/mailbox self).
    if in_non_team_spawn_context(env):
        return 2, _fail(
            op or "unknown",
            "E_TEAM_API_GATE",
            "omg team api refused: already inside a spawned-worker context "
            f"(depth-1; one of {', '.join(('OMG_PROCESS_FANOUT_WORKER', 'OMG_SPAWNED_WORKER'))} is set).",
        )

    identity = team_worker_identity(env)
    if identity is not None:
        try:
            payload = _apply_worker_identity_matrix(
                op, payload, identity=identity, env=env
            )
        except TeamApiError as exc:
            return exc.exit_code, _fail(
                op or "unknown",
                exc.code,
                exc.message,
                details=exc.details or None,
            )
    elif team_api_worker_context_present(env):
        # Identity/routing fields without a valid worker marker are partial
        # context, not a leader invocation.
        return 2, _fail(
            op or "unknown",
            "E_TEAM_API_GATE",
            "omg team api refused: partial or invalid worker environment",
            details={"error": "worker_env_incomplete"},
        )
    elif in_spawned_worker_context(env):
        # OMG_TEAM_WORKER without identity — fail closed.
        return 2, _fail(
            op or "unknown",
            "E_TEAM_API_GATE",
            "omg team api refused: OMG_TEAM_WORKER set without OMG_TEAM_WORKER_ID",
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
    "WORKER_ALLOWED_OPS",
    "WORKER_DENIED_OPS",
    "TeamApiError",
    "execute_team_api",
    "parse_input_json",
]
