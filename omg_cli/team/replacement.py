"""Identity-fenced Team worker replacement attempts (#69 PR5).

Leader-only transaction that replaces a lost / failed / restarted worker with
a new attempt on the same logical worker slot. Reuses ``launch_worker``
(pane|job) — no second scheduler. Prior handles are archived as immutable
history (never claim tokens). Crash-safe via durable replacement WAL +
idempotency key adoption.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, MutableMapping

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from omg_cli.team.launch import (
    WORKER_TOPOLOGY_JOB,
    WORKER_TOPOLOGY_PANE,
    WorkerExecutionHandle,
    WorkerLaunchError,
    cancel_job_backed_worker,
    launch_worker,
    stamp_execution_on_task,
    validate_execution_record,
)
from omg_cli.evidence import CLI_WRITER
from omg_cli.team.plane import (
    load_team_meta,
    mutate_team_meta,
    team_dir,
)
from omg_cli.team.scaling import acquire_scale_lock, pending_identity_wal_operation

ReplaceMode = Literal["lost", "failed", "restart"]
REPLACE_MODES: frozenset[str] = frozenset({"lost", "failed", "restart"})

WAL_KIND = "team_replace_worker_wal"
WAL_CONTRACT = "replace-worker-wal-v1"
BINDING_SCHEMA = 1
PRIOR_ATTEMPT_SCHEMA = 1


class ReplacementError(ValueError):
    """Fail-closed worker replacement error."""

    def __init__(self, message: str, *, code: str = "E_TEAM_REPLACE") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ReplacementResult:
    ok: bool
    run_id: str
    team_id: str
    worker_id: str
    attempt: int
    launch_generation: int
    mode: str
    idempotency_key: str
    adopted: bool
    dry_run: bool
    prior_attempt: dict[str, Any] | None
    execution: dict[str, Any] | None
    error: str | None = None
    code: str | None = None
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "team_id": self.team_id,
            "worker_id": self.worker_id,
            "attempt": self.attempt,
            "launch_generation": self.launch_generation,
            "mode": self.mode,
            "idempotency_key": self.idempotency_key,
            "adopted": self.adopted,
            "dry_run": self.dry_run,
            "prior_attempt": self.prior_attempt,
            "execution": self.execution,
            "error": self.error,
            "code": self.code,
            "verified": False,
            "writer": CLI_WRITER,
        }


PaneFenceFn = Callable[..., dict[str, Any]]
PaneLauncher = Callable[..., str]


def _token_fingerprint(token: str | None) -> str | None:
    if not isinstance(token, str) or not token.strip():
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def replacement_wal_dir(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / "replacement"


def replacement_wal_path(root: Path | str, run_id: str, idempotency_key: str) -> Path:
    safe = idempotency_key.strip()
    if not safe or "/" in safe or ".." in safe or len(safe) > 128:
        raise ReplacementError(
            "idempotency_key must be a non-empty path-safe token (<=128)",
            code="E_TEAM_REPLACE_IDEMPOTENCY",
        )
    return replacement_wal_dir(root, run_id) / f"{safe}.json"


def _load_wal(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = parse_canonical_json_bytes(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ReplacementError(
            f"corrupt replacement WAL: {exc}",
            code="E_TEAM_REPLACE_WAL",
        ) from exc
    if not isinstance(raw, dict):
        raise ReplacementError(
            "replacement WAL must be an object",
            code="E_TEAM_REPLACE_WAL",
        )
    if raw.get("store_kind") != WAL_KIND or raw.get("writer_contract") != WAL_CONTRACT:
        raise ReplacementError(
            "replacement WAL kind/contract mismatch",
            code="E_TEAM_REPLACE_WAL",
        )
    return raw


def _write_wal(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    ensure_managed_dir(path.parent)
    body = dict(payload)
    body.setdefault("store_kind", WAL_KIND)
    body.setdefault("writer_contract", WAL_CONTRACT)
    body.setdefault("writer", CLI_WRITER)
    atomic_write_bytes(
        path,
        canonical_json_bytes(body),
        mode=DATA_FILE_MODE,
        replace=True,
    )
    return body


def archive_prior_attempt(
    task: MutableMapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Move current execution into immutable prior_attempts (no claim tokens)."""
    execution = task.get("execution")
    if not isinstance(execution, Mapping):
        raise ReplacementError(
            "cannot archive missing execution",
            code="E_TEAM_REPLACE_NO_EXEC",
        )
    try:
        rec = validate_execution_record(execution)
    except WorkerLaunchError as exc:
        raise ReplacementError(
            f"cannot archive invalid execution: {exc}",
            code=getattr(exc, "code", None) or "E_TEAM_EXEC_SHAPE",
        ) from exc
    if rec.get("job_id") is not None and rec.get("pane_id") is not None:
        raise ReplacementError(
            "corrupt dual-handle execution refuses replacement",
            code="E_TEAM_EXEC_XOR",
        )
    prior = {
        "schema": PRIOR_ATTEMPT_SCHEMA,
        "attempt": int(task.get("attempt") or 1),
        "launch_generation": int(rec.get("launch_generation") or 0),
        "execution": dict(rec),
        "reason": reason,
        # Evidence descriptors only — never persist claim tokens.
        "worktree": task.get("worktree"),
        "provider": task.get("provider"),
        "role": task.get("role"),
        "status": task.get("status"),
    }
    # Preserve stamped route (additive #69 PR6); never invent from argv.
    route = task.get("route")
    if isinstance(route, Mapping):
        prior["route"] = dict(route)
    history = list(task.get("prior_attempts") or [])
    if not isinstance(history, list):
        raise ReplacementError(
            "prior_attempts must be a list",
            code="E_TEAM_REPLACE_HISTORY",
        )
    history.append(prior)
    task["prior_attempts"] = history
    return prior


def validate_worker_binding(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ReplacementError(
            "worker binding required",
            code="E_TEAM_REPLACE_BINDING",
        )
    logical = raw.get("logical_worker_id") or raw.get("worker_id")
    api_task_id = raw.get("api_task_id")
    attempt = raw.get("attempt", 1)
    launch_generation = raw.get("launch_generation", 1)
    if not isinstance(logical, str) or not logical.strip():
        raise ReplacementError(
            "binding.logical_worker_id required",
            code="E_TEAM_REPLACE_BINDING",
        )
    if api_task_id is not None and (
        not isinstance(api_task_id, str) or not api_task_id.strip()
    ):
        raise ReplacementError(
            "binding.api_task_id must be a non-empty string when present",
            code="E_TEAM_REPLACE_BINDING",
        )
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or isinstance(launch_generation, bool)
        or not isinstance(launch_generation, int)
        or launch_generation < 1
    ):
        raise ReplacementError(
            "binding attempt/launch_generation must be positive ints",
            code="E_TEAM_REPLACE_BINDING",
        )
    out: dict[str, Any] = {
        "schema": int(raw.get("schema") or BINDING_SCHEMA),
        "logical_worker_id": logical.strip(),
        "attempt": int(attempt),
        "launch_generation": int(launch_generation),
    }
    if api_task_id is not None:
        out["api_task_id"] = str(api_task_id).strip()
    for key in ("run_id", "team_id", "topology", "provider", "role"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


def seed_worker_binding(
    task: MutableMapping[str, Any],
    *,
    run_id: str,
    team_id: str,
    api_task_id: str | None = None,
    attempt: int = 1,
    launch_generation: int = 1,
) -> dict[str, Any]:
    """Attach explicit API-task ↔ logical Team-worker binding on a team task."""
    tid = str(task.get("task_id") or task.get("worker_id") or "").strip()
    if not tid:
        raise ReplacementError("task_id required to seed binding", code="E_TEAM_REPLACE_BINDING")
    topo = task.get("worker_topology") or (
        (task.get("execution") or {}).get("topology")
        if isinstance(task.get("execution"), Mapping)
        else None
    )
    binding = validate_worker_binding(
        {
            "schema": BINDING_SCHEMA,
            "logical_worker_id": tid,
            "api_task_id": api_task_id,
            "attempt": attempt,
            "launch_generation": launch_generation,
            "run_id": run_id,
            "team_id": team_id,
            "topology": topo,
            "provider": task.get("provider"),
            "role": task.get("role"),
        }
    )
    task["binding"] = binding
    task["attempt"] = int(attempt)
    return binding


def _find_task(meta: Mapping[str, Any], worker_id: str) -> dict[str, Any]:
    for raw in meta.get("tasks") or []:
        if isinstance(raw, Mapping) and str(raw.get("task_id") or "") == worker_id:
            return dict(raw)
    raise ReplacementError(
        f"worker/task {worker_id!r} not found in team meta",
        code="E_TEAM_REPLACE_WORKER",
    )


def _fence_job_handle(
    root: Path,
    task: Mapping[str, Any],
    *,
    mode: ReplaceMode,
    team_id: str,
) -> dict[str, Any]:
    from omg_cli.jobs.models import JobState, JobStoreError
    from omg_cli.jobs.store import read_job_record

    execution = task.get("execution")
    if not isinstance(execution, Mapping):
        raise ReplacementError("missing execution", code="E_TEAM_REPLACE_NO_EXEC")
    rec = validate_execution_record(execution)
    if rec.get("topology") != WORKER_TOPOLOGY_JOB:
        raise ReplacementError("not job-backed", code="E_TEAM_REPLACE_TOPOLOGY")
    if not rec.get("job_id"):
        if mode == "restart":
            raise ReplacementError(
                "restart requires a live job identity",
                code="E_TEAM_REPLACE_CANCEL",
            )
        return {"ok": True, "reason": "no_job_id", "job_id": None}
    job_id = str(rec["job_id"])
    try:
        record = read_job_record(root, job_id)
    except JobStoreError:
        record = None
    if record is not None:
        req = getattr(record, "request", None) or {}
        owned = req.get("team_id") if isinstance(req, Mapping) else None
        if owned is None or str(owned) != str(team_id):
            raise ReplacementError(
                "foreign-team job refuses replacement",
                code="E_TEAM_REPLACE_FOREIGN",
            )
        state = record.state
        state_s = state.value if isinstance(state, JobState) else str(state)
        terminalish = {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.LOST,
        }
        if mode in ("lost", "failed") and isinstance(state, JobState) and state in terminalish:
            return {
                "ok": True,
                "reason": f"already_{state_s}",
                "job_id": job_id,
                "state": state_s,
            }
        # restart (or non-terminal lost/failed) requires exact Jobs cancel.
        cancelled = cancel_job_backed_worker(root, task, reason=f"team_replace_{mode}")
        if not cancelled.get("ok"):
            raise ReplacementError(
                f"job cancel failed: {cancelled.get('reason')}",
                code="E_TEAM_REPLACE_CANCEL",
            )
        return {
            "ok": True,
            "reason": "cancelled",
            "job_id": job_id,
            "state": cancelled.get("state"),
        }
    # Missing metadata: only admissible for lost/failed (not restart of live).
    if mode == "restart":
        raise ReplacementError(
            "restart requires identity-bound cancel; job metadata missing",
            code="E_TEAM_REPLACE_CANCEL",
        )
    return {"ok": True, "reason": "missing_job_metadata", "job_id": job_id}


def fence_pane_for_replacement(
    root: Path | str,
    task: Mapping[str, Any],
    *,
    meta: Mapping[str, Any],
    mode: ReplaceMode,
) -> dict[str, Any]:
    """Exact identity-bound pane fence used by replacement (and tests).

    Reuses scaling's recorded-pane kill path when the pane is still live.
    Proven absence is success for lost/failed; restart of a live pane must
    cancel via exact identity or refuse.
    """
    from omg_cli.madmax import tmux_available
    from omg_cli.team.plane import _tmux_launch_authority_matches
    from omg_cli.team.scaling import _kill_pane_recorded, _read_recorded_tmux_pane

    _ = root  # reserved for future path-bound probes
    execution = task.get("execution")
    if not isinstance(execution, Mapping):
        # Legacy pane tasks may only have pane_id on the task row.
        pane_id = task.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id.strip():
            raise ReplacementError(
                "missing pane execution", code="E_TEAM_REPLACE_NO_EXEC"
            )
        rec = {
            "topology": WORKER_TOPOLOGY_PANE,
            "pane_id": pane_id.strip(),
            "launch_generation": int(task.get("attempt") or 1),
        }
    else:
        rec = validate_execution_record(execution)
        if rec.get("topology") != WORKER_TOPOLOGY_PANE:
            raise ReplacementError(
                "not pane topology", code="E_TEAM_REPLACE_TOPOLOGY"
            )
    pane_id = rec.get("pane_id") or task.get("pane_id")
    if not isinstance(pane_id, str) or not pane_id.strip():
        if mode == "restart":
            raise ReplacementError(
                "restart requires a live pane identity",
                code="E_TEAM_REPLACE_CANCEL",
            )
        return {"ok": True, "reason": "no_pane_id", "pane_id": None}

    session = str(meta.get("session") or "")
    authority = {
        "session_id": meta.get("session_id"),
        "launch_nonce": meta.get("launch_nonce"),
    }
    # Without tmux / dry meta: treat as proven absent for lost/failed only.
    if bool(meta.get("dry_run")) or not tmux_available() or not session:
        if mode == "restart":
            raise ReplacementError(
                "restart cannot prove pane cancel without live tmux identity",
                code="E_TEAM_REPLACE_CANCEL",
            )
        return {"ok": True, "reason": "dry_or_no_tmux_absent", "pane_id": pane_id}

    task_rec = dict(task)
    task_rec.setdefault("pane_id", pane_id)
    try:
        live = _read_recorded_tmux_pane(
            task_rec,
            session=session,
            session_id=str(authority.get("session_id") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — probe failure is not proven absence
        raise ReplacementError(
            f"pane probe failed (not proven_absent): {exc}",
            code="E_TEAM_REPLACE_CANCEL",
        ) from exc

    if live is None:
        return {"ok": True, "reason": "proven_absent", "pane_id": pane_id}

    # Live pane — must identity-kill.
    errors: list[str] = []
    actions: list[str] = []
    signalled: list[dict[str, Any]] = []
    window_id = meta.get("window_id")
    window_id_s = str(window_id) if isinstance(window_id, str) else None
    try:
        owned = _tmux_launch_authority_matches(
            session,
            expected_nonce=str(authority.get("launch_nonce") or ""),
            session_owned=True,
            window_id=window_id_s,
            pane_ids=[pane_id],
        )
        _kill_pane_recorded(
            task_rec,
            session=session,
            dry=False,
            actions=actions,
            errors=errors,
            signalled=signalled,
            authority=authority,
            session_owned=owned,
            window_id=window_id_s,
        )
    except Exception as exc:  # noqa: BLE001 — fence must fail closed
        raise ReplacementError(
            f"pane fence failed: {exc}",
            code="E_TEAM_REPLACE_CANCEL",
        ) from exc
    if errors:
        raise ReplacementError(
            f"pane cancel failed: {'; '.join(errors)}",
            code="E_TEAM_REPLACE_CANCEL",
        )
    after = _read_recorded_tmux_pane(
        task_rec,
        session=session,
        session_id=str(authority.get("session_id") or ""),
    )
    if after is not None:
        raise ReplacementError(
            "pane still present after identity cancel",
            code="E_TEAM_REPLACE_CANCEL",
        )
    return {"ok": True, "reason": "cancelled", "pane_id": pane_id}


def _invalidate_api_claim(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    api_task_id: str | None,
    worker_id: str,
    new_attempt: int,
    new_generation: int,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Fence old claim token and stamp attempt on the API task binding."""
    if not api_task_id:
        return None
    from omg_cli.contracts.path_keys import exclusive_lock
    from omg_cli.team import api as team_api

    path = team_api._task_path(root, run_id, team_id, api_task_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        task = team_api._read_task(root, run_id, team_id, api_task_id)
        if task is None:
            raise ReplacementError(
                f"bound api task {api_task_id!r} missing",
                code="E_TEAM_REPLACE_BINDING",
            )
        binding = dict(task.get("binding") or {})
        logical = binding.get("logical_worker_id") or task.get("owner")
        if logical is not None and str(logical) != worker_id:
            raise ReplacementError(
                "api task binding worker mismatch",
                code="E_TEAM_REPLACE_BINDING",
            )
        old_claim = task.get("claim") if isinstance(task.get("claim"), Mapping) else None
        fenced_fp = _token_fingerprint(
            str(old_claim.get("token")) if old_claim else None
        )
        binding.update(
            {
                "schema": BINDING_SCHEMA,
                "logical_worker_id": worker_id,
                "attempt": new_attempt,
                "launch_generation": new_generation,
                "fenced_claim_token_sha256": fenced_fp,
            }
        )
        updated = team_api._write_task(
            root,
            run_id,
            team_id,
            {
                **task,
                "status": "pending" if task["status"] == "in_progress" else task["status"],
                "owner": None,
                "claim": None,
                "binding": binding,
                "version": int(task["version"]) + 1,
            },
        )
    return {"task": updated, "fenced_claim_token_sha256": fenced_fp}


def list_pending_replacement_wals(
    root: Path | str, run_id: str
) -> list[dict[str, Any]]:
    directory = replacement_wal_dir(root, run_id)
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        wal = _load_wal(path)
        if wal is None:
            continue
        state = str(wal.get("state") or "")
        if state in {"intent", "fenced", "launched"}:
            out.append(wal)
    return out


def recover_pending_replacement(
    root: Path | str,
    run_id: str,
    *,
    team_id: str | None = None,
    env: Mapping[str, str] | None = None,
    already_locked: bool = False,
) -> dict[str, Any]:
    """Adopt / finish pending replacement WALs (resume hook; no new launch policy)."""
    root_path = Path(root).resolve()
    pending = list_pending_replacement_wals(root_path, run_id)
    recovered: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for wal in pending:
        if team_id is not None and str(wal.get("team_id") or "") != str(team_id):
            failed.append(
                {
                    "idempotency_key": wal.get("idempotency_key"),
                    "reason": "foreign_team_wal",
                }
            )
            continue
        try:
            result = replace_worker(
                root_path,
                run_id=str(wal["run_id"]),
                team_id=str(wal["team_id"]),
                worker_id=str(wal["worker_id"]),
                mode=str(wal["mode"]),  # type: ignore[arg-type]
                expected_attempt=int(wal["expected_attempt"]),
                expected_launch_generation=int(wal["expected_launch_generation"]),
                idempotency_key=str(wal["idempotency_key"]),
                dry_run=bool(wal.get("dry_run")),
                env=env,
                recovering=True,
                already_locked=already_locked,
            )
            recovered.append(result.to_dict())
        except ReplacementError as exc:
            failed.append(
                {
                    "idempotency_key": wal.get("idempotency_key"),
                    "reason": exc.message,
                    "code": exc.code,
                }
            )
    return {"recovered": recovered, "failed": failed, "scanned": len(pending)}


def replace_worker(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    mode: ReplaceMode,
    expected_attempt: int,
    expected_launch_generation: int,
    idempotency_key: str,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    pane_launcher: PaneLauncher | None = None,
    pane_fence: PaneFenceFn | None = None,
    recovering: bool = False,
    already_locked: bool = False,
) -> ReplacementResult:
    """Replace a worker attempt under the lifecycle lock (fail closed)."""
    root_path = Path(root).resolve()
    if mode not in REPLACE_MODES:
        raise ReplacementError(
            f"mode must be one of {sorted(REPLACE_MODES)}",
            code="E_TEAM_REPLACE_MODE",
        )
    if (
        isinstance(expected_attempt, bool)
        or not isinstance(expected_attempt, int)
        or expected_attempt < 1
        or isinstance(expected_launch_generation, bool)
        or not isinstance(expected_launch_generation, int)
        or expected_launch_generation < 1
    ):
        raise ReplacementError(
            "expected_attempt/expected_launch_generation must be positive ints",
            code="E_TEAM_REPLACE_CAS",
        )

    wal_path = replacement_wal_path(root_path, run_id, idempotency_key)

    def _body() -> ReplacementResult:
        return _replace_worker_locked(
            root_path,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            mode=mode,
            expected_attempt=expected_attempt,
            expected_launch_generation=expected_launch_generation,
            idempotency_key=idempotency_key,
            wal_path=wal_path,
            dry_run=dry_run,
            env=env,
            pane_launcher=pane_launcher,
            pane_fence=pane_fence,
            recovering=recovering,
        )

    if already_locked:
        return _body()
    with acquire_scale_lock(root_path, run_id):
        return _body()


def _replace_worker_locked(
    root_path: Path,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    mode: ReplaceMode,
    expected_attempt: int,
    expected_launch_generation: int,
    idempotency_key: str,
    wal_path: Path,
    dry_run: bool,
    env: Mapping[str, str] | None,
    pane_launcher: PaneLauncher | None,
    pane_fence: PaneFenceFn | None,
    recovering: bool,
) -> ReplacementResult:
    meta = load_team_meta(root_path, run_id)
    if str(meta.get("team_id") or "team") != str(team_id):
        raise ReplacementError(
            "team_id mismatch vs team meta",
            code="E_TEAM_REPLACE_TEAM",
        )
    # Unrelated pending identity WAL refuses mutation (except our own recovery).
    pending_op = pending_identity_wal_operation(root_path, run_id, meta)
    if pending_op is not None and not recovering:
        raise ReplacementError(
            f"refusing replace while identity WAL pending ({pending_op})",
            code="E_TEAM_REPLACE_WAL_BUSY",
        )

    existing = _load_wal(wal_path)
    if existing is not None:
        return _adopt_or_resume_wal(
            root_path,
            meta=meta,
            wal=existing,
            wal_path=wal_path,
            expected_attempt=expected_attempt,
            expected_launch_generation=expected_launch_generation,
            mode=mode,
            worker_id=worker_id,
            team_id=team_id,
            dry_run=dry_run,
            env=env,
            pane_launcher=pane_launcher,
            pane_fence=pane_fence,
        )

    task = _find_task(meta, worker_id)
    binding_raw = task.get("binding")
    if binding_raw is None:
        # Auto-seed from task fields when start_team did not yet bind.
        binding = seed_worker_binding(
            task,
            run_id=run_id,
            team_id=team_id,
            attempt=int(task.get("attempt") or 1),
            launch_generation=int(
                (task.get("execution") or {}).get("launch_generation")
                if isinstance(task.get("execution"), Mapping)
                else 1
            )
            or 1,
        )
    else:
        binding = validate_worker_binding(binding_raw)

    if binding["logical_worker_id"] != worker_id:
        raise ReplacementError(
            "task-binding mismatch",
            code="E_TEAM_REPLACE_BINDING",
        )
    if int(binding["attempt"]) != expected_attempt:
        raise ReplacementError(
            "stale expected_attempt",
            code="E_TEAM_REPLACE_CAS",
        )
    execution = task.get("execution")
    if isinstance(execution, Mapping) and execution:
        if execution.get("job_id") is not None and execution.get("pane_id") is not None:
            raise ReplacementError(
                "corrupt dual-handle refuses replacement",
                code="E_TEAM_EXEC_XOR",
            )
        try:
            rec = validate_execution_record(execution)
        except WorkerLaunchError as exc:
            raise ReplacementError(
                f"invalid execution: {exc}",
                code=getattr(exc, "code", None) or "E_TEAM_EXEC_SHAPE",
            ) from exc
        if int(rec.get("launch_generation") or 0) != expected_launch_generation:
            raise ReplacementError(
                "stale expected_launch_generation",
                code="E_TEAM_REPLACE_CAS",
            )
        topology = str(rec.get("topology"))
    else:
        topology = str(
            task.get("worker_topology")
            or meta.get("worker_topology")
            or WORKER_TOPOLOGY_PANE
        )
        if expected_launch_generation != int(binding.get("launch_generation") or 1):
            raise ReplacementError(
                "stale expected_launch_generation",
                code="E_TEAM_REPLACE_CAS",
            )

    new_attempt = expected_attempt + 1
    new_generation = expected_launch_generation + 1
    provider = str(task.get("provider") or "grok")
    role = str(task.get("role") or "executor")

    wal = _write_wal(
        wal_path,
        {
            "store_kind": WAL_KIND,
            "writer_contract": WAL_CONTRACT,
            "writer": CLI_WRITER,
            "state": "intent",
            "idempotency_key": idempotency_key,
            "run_id": run_id,
            "team_id": team_id,
            "worker_id": worker_id,
            "mode": mode,
            "expected_attempt": expected_attempt,
            "expected_launch_generation": expected_launch_generation,
            "new_attempt": new_attempt,
            "new_launch_generation": new_generation,
            "topology": topology,
            "provider": provider,
            "role": role,
            "api_task_id": binding.get("api_task_id"),
            "old_execution": (
                dict(validate_execution_record(execution))
                if isinstance(execution, Mapping) and execution
                else None
            ),
            "new_execution": None,
            "dry_run": bool(dry_run),
            "nonce": secrets.token_hex(8),
        },
    )

    return _execute_replacement_from_wal(
        root_path,
        meta=meta,
        wal=wal,
        wal_path=wal_path,
        dry_run=dry_run,
        env=env,
        pane_launcher=pane_launcher,
        pane_fence=pane_fence,
        adopted=False,
    )


def _adopt_or_resume_wal(
    root: Path,
    *,
    meta: Mapping[str, Any],
    wal: Mapping[str, Any],
    wal_path: Path,
    expected_attempt: int,
    expected_launch_generation: int,
    mode: str,
    worker_id: str,
    team_id: str,
    dry_run: bool,
    env: Mapping[str, str] | None,
    pane_launcher: PaneLauncher | None,
    pane_fence: PaneFenceFn | None,
) -> ReplacementResult:
    if (
        str(wal.get("worker_id")) != worker_id
        or str(wal.get("team_id")) != team_id
        or str(wal.get("mode")) != mode
        or int(wal.get("expected_attempt") or -1) != expected_attempt
        or int(wal.get("expected_launch_generation") or -1)
        != expected_launch_generation
    ):
        raise ReplacementError(
            "idempotency key conflicts with a different replacement intent",
            code="E_TEAM_REPLACE_IDEMPOTENCY",
        )
    state = str(wal.get("state") or "")
    if state == "committed":
        return ReplacementResult(
            ok=True,
            run_id=str(wal["run_id"]),
            team_id=team_id,
            worker_id=worker_id,
            attempt=int(wal["new_attempt"]),
            launch_generation=int(wal["new_launch_generation"]),
            mode=mode,
            idempotency_key=str(wal["idempotency_key"]),
            adopted=True,
            dry_run=bool(wal.get("dry_run")),
            prior_attempt=wal.get("prior_attempt")
            if isinstance(wal.get("prior_attempt"), dict)
            else None,
            execution=wal.get("new_execution")
            if isinstance(wal.get("new_execution"), dict)
            else None,
        )
    if state == "failed":
        raise ReplacementError(
            f"prior replacement failed: {wal.get('error')}",
            code=str(wal.get("code") or "E_TEAM_REPLACE_FAILED"),
        )
    return _execute_replacement_from_wal(
        root,
        meta=meta,
        wal=dict(wal),
        wal_path=wal_path,
        dry_run=dry_run or bool(wal.get("dry_run")),
        env=env,
        pane_launcher=pane_launcher,
        pane_fence=pane_fence,
        adopted=True,
    )


def _replacement_job_stamps(
    *,
    idempotency_key: str,
    worker_id: str,
    attempt: int,
    launch_generation: int,
) -> dict[str, Any]:
    return {
        "team_replacement_key": str(idempotency_key),
        "team_worker_id": str(worker_id),
        "team_task_id": str(worker_id),
        "team_attempt": int(attempt),
        "team_launch_generation": int(launch_generation),
    }


def _find_orphaned_replacement_job(
    root: Path,
    *,
    team_id: str,
    worker_id: str,
    attempt: int,
    launch_generation: int,
    idempotency_key: str,
    run_id: str | None,
    provider: str,
) -> WorkerExecutionHandle | None:
    """Adopt a Jobs record created for this replacement before WAL launched stamp.

    Crash window: ``fenced`` → ``launch_worker`` succeeded → process died before
    WAL ``launched``. Identity is the replacement idempotency key stamped on the
    Jobs request (plus team/worker/attempt/generation CAS fields).
    """
    from omg_cli.jobs.store import list_job_ids, read_job_record

    matches: list[Any] = []
    for jid in list_job_ids(root):
        try:
            record = read_job_record(root, jid)
        except Exception:  # noqa: BLE001 — skip unreadable
            continue
        req = record.request if isinstance(record.request, Mapping) else {}
        if str(req.get("team_replacement_key") or "") != str(idempotency_key):
            continue
        if str(req.get("team_id") or "") != str(team_id):
            continue
        if str(req.get("team_worker_id") or "") != str(worker_id):
            continue
        if int(req.get("team_attempt") or -1) != int(attempt):
            continue
        if int(req.get("team_launch_generation") or -1) != int(launch_generation):
            continue
        if run_id is not None and record.run_id and str(record.run_id) != str(run_id):
            continue
        matches.append(record)
    if not matches:
        return None
    if len(matches) > 1:
        raise ReplacementError(
            "ambiguous orphaned replacement jobs for idempotency key",
            code="E_TEAM_REPLACE_IDEMPOTENCY",
        )
    record = matches[0]
    return WorkerExecutionHandle(
        topology=WORKER_TOPOLOGY_JOB,
        worker_id=worker_id,
        provider=provider,
        launch_generation=int(launch_generation),
        job_id=str(record.job_id),
        pane_id=None,
        attempt=int(attempt),
        run_id=run_id,
        team_id=team_id,
        task_id=worker_id,
        dry_run=False,
    )


def _execute_replacement_from_wal(
    root: Path,
    *,
    meta: Mapping[str, Any],
    wal: dict[str, Any],
    wal_path: Path,
    dry_run: bool,
    env: Mapping[str, str] | None,
    pane_launcher: PaneLauncher | None,
    pane_fence: PaneFenceFn | None,
    adopted: bool,
) -> ReplacementResult:
    run_id = str(wal["run_id"])
    team_id = str(wal["team_id"])
    worker_id = str(wal["worker_id"])
    mode: ReplaceMode = str(wal["mode"])  # type: ignore[assignment]
    new_attempt = int(wal["new_attempt"])
    new_generation = int(wal["new_launch_generation"])
    topology = str(wal["topology"])
    provider = str(wal.get("provider") or "grok")
    role = str(wal.get("role") or "executor")
    idempotency_key = str(wal["idempotency_key"])

    task = _find_task(meta, worker_id)
    state = str(wal.get("state") or "intent")
    # Track how far we got so cancel-before-fence can roll back without
    # advancing attempt/claim/generation (P0).
    progress = state

    try:
        if state == "intent":
            # Fence old execution FIRST. Cancel failure must not invalidate
            # claim or advance attempt/generation.
            if topology == WORKER_TOPOLOGY_JOB:
                fence = _fence_job_handle(root, task, mode=mode, team_id=team_id)
            else:
                fence_fn = pane_fence or fence_pane_for_replacement
                fence = fence_fn(root, task, meta=meta, mode=mode)
            if not fence.get("ok"):
                raise ReplacementError(
                    f"fence failed: {fence.get('reason')}",
                    code="E_TEAM_REPLACE_CANCEL",
                )
            # Only after proven fence: invalidate claim + archive prior.
            _invalidate_api_claim(
                root,
                run_id=run_id,
                team_id=team_id,
                api_task_id=wal.get("api_task_id")
                if isinstance(wal.get("api_task_id"), str)
                else None,
                worker_id=worker_id,
                new_attempt=new_attempt,
                new_generation=new_generation,
                env=env,
            )
            prior = archive_prior_attempt(task, reason=f"replace_{mode}")
            wal["prior_attempt"] = prior
            wal["fence"] = {
                k: fence[k]
                for k in ("ok", "reason", "job_id", "pane_id", "state")
                if k in fence
            }
            wal["state"] = "fenced"
            _write_wal(wal_path, wal)
            state = "fenced"
            progress = "fenced"

        if state == "fenced":
            if topology == WORKER_TOPOLOGY_PANE:
                from omg_cli.team.interactive import (
                    InteractiveTeamError,
                    clear_tui_ready_sidecar,
                )

                try:
                    clear_tui_ready_sidecar(task.get("tui_ready_path"))
                except InteractiveTeamError as exc:
                    raise ReplacementError(
                        str(exc), code="E_TEAM_REPLACE_SIDECAR"
                    ) from exc
            if dry_run:
                handle = launch_worker(
                    root,
                    worker_id=worker_id,
                    topology=topology,
                    provider=provider,
                    role=role,
                    run_id=run_id,
                    team_id=team_id,
                    task_id=worker_id,
                    attempt=new_attempt,
                    launch_generation=new_generation,
                    dry_run=True,
                    cwd=task.get("worktree"),
                )
                # Dry-run must not materialize pane/job or mutate live handles.
                wal["new_execution"] = handle.to_execution_record()
                wal["state"] = "committed"
                wal["dry_run"] = True
                _write_wal(wal_path, wal)
                # Still advance attempt markers on team meta for CAS consistency
                # without publishing live ids.
                _commit_team_task_replacement(
                    root,
                    run_id=run_id,
                    worker_id=worker_id,
                    new_attempt=new_attempt,
                    new_generation=new_generation,
                    handle=handle,
                    prior=wal.get("prior_attempt"),
                    api_task_id=wal.get("api_task_id")
                    if isinstance(wal.get("api_task_id"), str)
                    else None,
                    dry_run=True,
                )
                return ReplacementResult(
                    ok=True,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    attempt=new_attempt,
                    launch_generation=new_generation,
                    mode=mode,
                    idempotency_key=idempotency_key,
                    adopted=adopted,
                    dry_run=True,
                    prior_attempt=wal.get("prior_attempt")
                    if isinstance(wal.get("prior_attempt"), dict)
                    else None,
                    execution=handle.to_execution_record(),
                )

            # Crash-after-launch adopt: if a Jobs record already exists for this
            # idempotency key, commit THAT handle — never launch a second job.
            handle: WorkerExecutionHandle | None = None
            if topology == WORKER_TOPOLOGY_JOB:
                handle = _find_orphaned_replacement_job(
                    root,
                    team_id=team_id,
                    worker_id=worker_id,
                    attempt=new_attempt,
                    launch_generation=new_generation,
                    idempotency_key=idempotency_key,
                    run_id=run_id,
                    provider=provider,
                )
                if handle is not None:
                    adopted = True
            if handle is None:
                handle = launch_worker(
                    root,
                    worker_id=worker_id,
                    topology=topology,
                    provider=provider,
                    role=role,
                    run_id=run_id,
                    team_id=team_id,
                    task_id=worker_id,
                    attempt=new_attempt,
                    launch_generation=new_generation,
                    pane_launcher=(
                        pane_launcher if topology == WORKER_TOPOLOGY_PANE else None
                    ),
                    dry_run=False,
                    prompt_text=(
                        f"Team replacement worker={worker_id} attempt={new_attempt} "
                        f"run={run_id} team={team_id}"
                    ),
                    job_request_stamps=(
                        _replacement_job_stamps(
                            idempotency_key=idempotency_key,
                            worker_id=worker_id,
                            attempt=new_attempt,
                            launch_generation=new_generation,
                        )
                        if topology == WORKER_TOPOLOGY_JOB
                        else None
                    ),
                    cwd=task.get("worktree"),
                )
            # Readback already enforced inside launch_worker for jobs; panes
            # require a non-empty pane_id from launcher.
            if topology == WORKER_TOPOLOGY_PANE and not handle.pane_id:
                raise ReplacementError(
                    "pane replacement produced no pane_id readback",
                    code="E_TEAM_REPLACE_LAUNCH",
                )
            if topology == WORKER_TOPOLOGY_JOB and not handle.job_id:
                raise ReplacementError(
                    "job replacement produced no job_id readback",
                    code="E_TEAM_REPLACE_LAUNCH",
                )
            wal["new_execution"] = handle.to_execution_record()
            wal["state"] = "launched"
            _write_wal(wal_path, wal)
            state = "launched"
            progress = "launched"

        if state == "launched":
            new_exec = wal.get("new_execution")
            if not isinstance(new_exec, Mapping):
                raise ReplacementError(
                    "launched WAL missing new_execution",
                    code="E_TEAM_REPLACE_WAL",
                )
            handle = WorkerExecutionHandle(
                topology=str(new_exec["topology"]),
                worker_id=worker_id,
                provider=provider,
                launch_generation=int(new_exec["launch_generation"]),
                job_id=new_exec.get("job_id"),
                pane_id=new_exec.get("pane_id"),
                attempt=new_attempt,
                run_id=run_id,
                team_id=team_id,
                task_id=worker_id,
                dry_run=False,
            )
            _commit_team_task_replacement(
                root,
                run_id=run_id,
                worker_id=worker_id,
                new_attempt=new_attempt,
                new_generation=new_generation,
                handle=handle,
                prior=wal.get("prior_attempt"),
                api_task_id=wal.get("api_task_id")
                if isinstance(wal.get("api_task_id"), str)
                else None,
                dry_run=False,
            )
            wal["state"] = "committed"
            _write_wal(wal_path, wal)
            return ReplacementResult(
                ok=True,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                attempt=new_attempt,
                launch_generation=new_generation,
                mode=mode,
                idempotency_key=idempotency_key,
                adopted=adopted,
                dry_run=False,
                prior_attempt=wal.get("prior_attempt")
                if isinstance(wal.get("prior_attempt"), dict)
                else None,
                execution=handle.to_execution_record(),
            )

        raise ReplacementError(
            f"unknown replacement WAL state {state!r}",
            code="E_TEAM_REPLACE_WAL",
        )
    except (ReplacementError, WorkerLaunchError) as exc:
        code = getattr(exc, "code", None) or "E_TEAM_REPLACE"
        message = getattr(exc, "message", None) or str(exc)
        # Cancel/fence failure before fenced: roll back intent WAL and leave
        # attempt/claim/generation + old execution untouched.
        if (
            isinstance(exc, ReplacementError)
            and code == "E_TEAM_REPLACE_CANCEL"
            and progress == "intent"
        ):
            try:
                if wal_path.is_file():
                    wal_path.unlink()
            except OSError:
                pass
            raise
        # Launch / post-fence failure: leave old attempt fenced; never restore token.
        wal["state"] = "failed"
        wal["error"] = message
        wal["code"] = code
        _write_wal(wal_path, wal)
        # Mark team task unproven / replacement-failed without restoring claim.
        try:
            _mark_replacement_failed(
                root,
                run_id=run_id,
                worker_id=worker_id,
                new_attempt=new_attempt,
                new_generation=new_generation,
                error=message,
            )
        except Exception:  # noqa: BLE001 — best-effort status stamp
            pass
        if isinstance(exc, ReplacementError):
            raise
        raise ReplacementError(message, code=code) from exc


def _commit_team_task_replacement(
    root: Path,
    *,
    run_id: str,
    worker_id: str,
    new_attempt: int,
    new_generation: int,
    handle: WorkerExecutionHandle,
    prior: Any,
    api_task_id: str | None,
    dry_run: bool,
) -> None:
    def _mutator(current: dict[str, Any]) -> dict[str, Any]:
        tasks = []
        found = False
        for raw in current.get("tasks") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            if str(row.get("task_id") or "") != worker_id:
                tasks.append(row)
                continue
            found = True
            if prior and not row.get("prior_attempts"):
                row["prior_attempts"] = [prior]
            elif prior and isinstance(row.get("prior_attempts"), list):
                # Ensure prior is present (idempotent).
                histories = list(row["prior_attempts"])
                if not any(
                    isinstance(h, Mapping)
                    and h.get("attempt") == (prior or {}).get("attempt")
                    and h.get("launch_generation")
                    == (prior or {}).get("launch_generation")
                    for h in histories
                ):
                    histories.append(prior)
                row["prior_attempts"] = histories
            stamp_execution_on_task(row, handle)
            row["attempt"] = new_attempt
            binding = dict(row.get("binding") or {})
            binding.update(
                {
                    "schema": BINDING_SCHEMA,
                    "logical_worker_id": worker_id,
                    "attempt": new_attempt,
                    "launch_generation": new_generation,
                }
            )
            if api_task_id:
                binding["api_task_id"] = api_task_id
            row["binding"] = binding
            # #147: relaunch invalidates prior interaction_evidence; re-stamp
            # fail-closed I/O for the new attempt (topology-aware defaults).
            from omg_cli.team.io_capability import (
                io_defaults_for_worker_topology,
                stamp_io_capability,
            )

            topo = handle.topology or row.get("worker_topology") or WORKER_TOPOLOGY_PANE
            if topo not in (WORKER_TOPOLOGY_PANE, WORKER_TOPOLOGY_JOB):
                topo = WORKER_TOPOLOGY_PANE
            stamp_io_capability(row, io_defaults_for_worker_topology(str(topo)))
            from omg_cli.team.presentation import stamp_route_on_task

            stamp_route_on_task(
                row,
                provider=str(row.get("provider") or handle.provider),
                role=str(row.get("role") or "executor"),
                posture=(
                    str(row["posture"])
                    if isinstance(row.get("posture"), str)
                    else None
                ),
            )
            if not dry_run:
                row["status"] = "worker_launched"
            if handle.pane_id:
                row["pane_id"] = handle.pane_id
            if handle.job_id:
                row["job_id"] = handle.job_id
            # Clear stale process identity from prior pane attempt.
            if handle.topology == WORKER_TOPOLOGY_PANE and not dry_run:
                row.pop("pid", None)
                row.pop("pgid", None)
                row.pop("pid_start", None)
            tasks.append(row)
        if not found:
            raise ReplacementError(
                f"worker {worker_id!r} missing at commit",
                code="E_TEAM_REPLACE_WORKER",
            )
        current["tasks"] = tasks
        return current

    mutate_team_meta(root, run_id, _mutator)


def _mark_replacement_failed(
    root: Path,
    *,
    run_id: str,
    worker_id: str,
    new_attempt: int,
    new_generation: int,
    error: str,
) -> None:
    def _mutator(current: dict[str, Any]) -> dict[str, Any]:
        tasks = []
        for raw in current.get("tasks") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            if str(row.get("task_id") or "") == worker_id:
                row["status"] = "unproven"
                row["replacement_error"] = error
                row["attempt"] = new_attempt
                binding = dict(row.get("binding") or {})
                binding["attempt"] = new_attempt
                binding["launch_generation"] = new_generation
                row["binding"] = binding
                # Keep execution cleared / historical only — do not restore.
                if "execution" in row and isinstance(row.get("prior_attempts"), list):
                    # Active handle remains the fenced prior; clear live ids.
                    row["execution"] = {
                        "schema": 1,
                        "topology": (
                            row["execution"].get("topology")
                            if isinstance(row["execution"], Mapping)
                            else row.get("worker_topology") or "pane"
                        ),
                        "launch_generation": new_generation,
                    }
                    row.pop("job_id", None)
                    row.pop("pane_id", None)
            tasks.append(row)
        current["tasks"] = tasks
        return current

    mutate_team_meta(root, run_id, _mutator)


__all__ = [
    "BINDING_SCHEMA",
    "PRIOR_ATTEMPT_SCHEMA",
    "REPLACE_MODES",
    "ReplacementError",
    "ReplacementResult",
    "WAL_CONTRACT",
    "WAL_KIND",
    "archive_prior_attempt",
    "fence_pane_for_replacement",
    "list_pending_replacement_wals",
    "recover_pending_replacement",
    "replace_worker",
    "replacement_wal_dir",
    "replacement_wal_path",
    "seed_worker_binding",
    "validate_worker_binding",
]
