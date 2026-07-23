"""Restartable native-team supervisor and generation+1 recovery."""

from __future__ import annotations

from collections.abc import Mapping
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
    require_iso8601,
    require_safe_id,
)
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from omg_cli.team.liveness import (
    LivenessError,
    classify_liveness,
    load_liveness,
    mark_terminal,
)
from omg_cli.team.plane import (
    NATIVE_TERMINAL_STATES,
    _cas_native_task,
    load_native_team,
)


CLI_WRITER = "omg-cli"
DEFAULT_SUPERVISOR_TIMEOUT_SECONDS = 120


class RecoveryError(RuntimeError):
    """Supervisor adoption or stale-worker recovery was not safe."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    require_iso8601(value, label="timestamp")
    return datetime.fromisoformat(
        value[:-1] + "+00:00" if value.endswith("Z") else value
    )


def supervisor_path(root: Path | str, run_id: str, team_id: str) -> Path:
    require_safe_id(run_id, label="run_id")
    require_safe_id(team_id, label="team_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / safe_path_key(team_id, namespace="team")
        / "supervisor.json"
    )


def _validate_supervisor(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "owner_id",
        "process_start_identity",
        "generation",
        "poll_sequence",
        "last_successful_poll",
        "released",
    }
    if set(row) != required:
        raise ContractValidationError("team supervisor keys mismatch")
    if (
        row["store_kind"] != "team_supervisor"
        or row["schema_version"] != 1
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("team supervisor header mismatch")
    for field in ("run_id", "team_id", "owner_id", "process_start_identity"):
        require_safe_id(row[field], label=field)
    require_integer(row["generation"], label="generation", minimum=0)
    require_integer(row["poll_sequence"], label="poll_sequence", minimum=0)
    require_iso8601(row["last_successful_poll"], label="last_successful_poll")
    if not isinstance(row["released"], bool):
        raise ContractValidationError("team supervisor released must be boolean")
    return row


def acquire_supervisor(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    owner_id: str,
    process_start_identity: str,
    now: datetime | None = None,
    timeout_seconds: int = DEFAULT_SUPERVISOR_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Create or adopt a stale/released supervisor at generation+1."""

    require_safe_id(owner_id, label="owner_id")
    require_safe_id(process_start_identity, label="process_start_identity")
    if not 1 <= timeout_seconds <= 86_400:
        raise RecoveryError("supervisor timeout seconds out of bounds")
    current_time = now or _utc_now()
    path = supervisor_path(root, run_id, team_id)
    ensure_managed_dir(path.parent)
    with exclusive_lock(path.with_suffix(".lock")):
        previous: dict[str, Any] | None = None
        if path.exists():
            parsed = parse_canonical_json_bytes(path.read_bytes())
            if not isinstance(parsed, dict):
                raise ContractValidationError("team supervisor must be an object")
            previous = _validate_supervisor(parsed)
            same = (
                previous["owner_id"] == owner_id
                and previous["process_start_identity"] == process_start_identity
                and not previous["released"]
            )
            if same:
                return previous
            stale = current_time > (
                _parse(previous["last_successful_poll"])
                + timedelta(seconds=timeout_seconds)
            )
            if not previous["released"] and not stale:
                raise RecoveryError("another healthy supervisor owns the team")
        generation = 0 if previous is None else previous["generation"] + 1
        candidate = _validate_supervisor(
            {
                "store_kind": "team_supervisor",
                "schema_version": 1,
                "writer": CLI_WRITER,
                "run_id": run_id,
                "team_id": team_id,
                "owner_id": owner_id,
                "process_start_identity": process_start_identity,
                "generation": generation,
                "poll_sequence": 0,
                "last_successful_poll": _iso(current_time),
                "released": False,
            }
        )
        atomic_write_bytes(
            path, canonical_json_bytes(candidate), mode=DATA_FILE_MODE, replace=True
        )
        return candidate


def supervisor_poll(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    owner_id: str,
    process_start_identity: str,
    generation: int,
    expected_sequence: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """CAS a successful reconciliation poll; failures do not update it."""

    path = supervisor_path(root, run_id, team_id)
    with exclusive_lock(path.with_suffix(".lock")):
        if not path.exists():
            raise RecoveryError("team supervisor lease is missing")
        parsed = parse_canonical_json_bytes(path.read_bytes())
        if not isinstance(parsed, dict):
            raise ContractValidationError("team supervisor must be an object")
        current = _validate_supervisor(parsed)
        expected = (
            owner_id,
            process_start_identity,
            generation,
            expected_sequence,
            False,
        )
        observed = (
            current["owner_id"],
            current["process_start_identity"],
            current["generation"],
            current["poll_sequence"],
            current["released"],
        )
        if observed != expected:
            raise RecoveryError("team supervisor poll CAS mismatch")
        updated = _validate_supervisor(
            {
                **current,
                "poll_sequence": expected_sequence + 1,
                "last_successful_poll": _iso(now or _utc_now()),
            }
        )
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
        return updated


def release_supervisor(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    owner_id: str,
    process_start_identity: str,
    generation: int,
) -> dict[str, Any]:
    path = supervisor_path(root, run_id, team_id)
    with exclusive_lock(path.with_suffix(".lock")):
        if not path.exists():
            raise RecoveryError("team supervisor lease is missing")
        parsed = parse_canonical_json_bytes(path.read_bytes())
        if not isinstance(parsed, dict):
            raise ContractValidationError("team supervisor must be an object")
        current = _validate_supervisor(parsed)
        if (
            current["owner_id"],
            current["process_start_identity"],
            current["generation"],
        ) != (owner_id, process_start_identity, generation):
            raise RecoveryError("team supervisor release identity mismatch")
        updated = _validate_supervisor({**current, "released": True})
        atomic_write_bytes(
            path, canonical_json_bytes(updated), mode=DATA_FILE_MODE, replace=True
        )
        return updated


def recover_native_task(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    expected_state: str,
    expected_sequence: int,
    expected_generation: int,
    now: datetime | None = None,
    force_launch_unknown: bool = False,
) -> dict[str, Any]:
    """Fence a stale task and return it to ready at generation+1."""

    state = load_native_team(root, run_id, team_id)
    task = dict(state["tasks"].get(task_id) or {})
    if not task:
        raise RecoveryError(f"unknown task {task_id!r}")
    if expected_state not in {"running", "launch_unknown"}:
        raise RecoveryError("only running/launch_unknown tasks can be recovered")
    if (task["state"], task["sequence"], task["generation"]) != (
        expected_state,
        expected_sequence,
        expected_generation,
    ):
        raise RecoveryError("task recovery state/sequence/generation fence mismatch")
    if expected_state == "running":
        live = load_liveness(root, run_id=run_id, team_id=team_id, task_id=task_id)
        if live is None:
            raise RecoveryError("running task has no liveness record")
        binding = task.get("binding") or {}
        worker_id = binding.get("host_spawn_id")
        if (
            not isinstance(worker_id, str)
            or live["worker_id"] != worker_id
            or live["generation"] != expected_generation
        ):
            raise RecoveryError("worker liveness identity/generation fence mismatch")
        disposition = classify_liveness(live, now=now)
        if disposition not in {"stalled", "dead", "terminal"}:
            raise RecoveryError(f"worker is not recoverable: liveness={disposition}")
        if disposition != "terminal":
            try:
                mark_terminal(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    generation=expected_generation,
                )
            except LivenessError:
                # A concurrent exact terminalization is safe.  The plane CAS
                # below remains the authority and still fences the generation.
                pass
        if task["envelope"]["write_scope"]:
            from omg_cli.team.worktree import (
                TeamWorktreeError,
                cancel_owned_worktree,
                load_worktree_receipt,
            )

            try:
                worktree = load_worktree_receipt(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    task_id=task_id,
                )
                if worktree["generation"] != expected_generation:
                    raise RecoveryError(
                        "stale write worker worktree generation differs from task"
                    )
                if worktree["state"] in {"created", "sealed", "conflict"}:
                    cancel_owned_worktree(
                        root,
                        run_id=run_id,
                        team_id=team_id,
                        task_id=task_id,
                        generation=expected_generation,
                    )
                elif worktree["state"] not in {"cancelled", "cleaned"}:
                    raise RecoveryError(
                        f"write worker recovery found unsafe worktree state "
                        f"{worktree['state']!r}"
                    )
            except (ContractValidationError, TeamWorktreeError) as exc:
                raise RecoveryError(
                    f"write worker worktree recovery failed: {exc}"
                ) from exc
    elif not force_launch_unknown:
        raise RecoveryError(
            "launch_unknown requires host absence/expiry reconciliation before retry"
        )
    envelope = {
        **task["envelope"],
        "claim_generation": expected_generation + 1,
        "expected_state": "ready",
        "expected_sequence": expected_sequence + 1,
    }
    _, updated_task = _cas_native_task(
        root,
        run_id=run_id,
        team_id=team_id,
        task_id=task_id,
        expected_state=expected_state,
        expected_sequence=expected_sequence,
        expected_generation=expected_generation,
        next_state="ready",
        updates={
            "generation": expected_generation + 1,
            "envelope": envelope,
            "receipt_id": None,
            "spawn_receipt_hash": None,
            "role_receipt_hash": None,
            "binding": None,
            "result": None,
            "result_hash": None,
            "replay_id": None,
            "error": f"recovered-from-{expected_state}-generation-{expected_generation}",
        },
    )
    return updated_task


def reconcile_team(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    now: datetime | None = None,
    recover_stale: bool = True,
) -> dict[str, Any]:
    """Schedule newly unblocked tasks and optionally recover stale workers."""

    actions: list[dict[str, Any]] = []
    # Reload after every CAS so dependency completion and sequences stay exact.
    state = load_native_team(root, run_id, team_id)
    for task_id in sorted(state["tasks"]):
        state = load_native_team(root, run_id, team_id)
        task = dict(state["tasks"][task_id])
        if task["state"] == "pending":
            dep_states = [state["tasks"][dep]["state"] for dep in task["dependencies"]]
            if any(dep in {"failed", "blocked", "cancelled"} for dep in dep_states):
                _, blocked = _cas_native_task(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    task_id=task_id,
                    expected_state="pending",
                    expected_sequence=task["sequence"],
                    expected_generation=task["generation"],
                    next_state="blocked",
                    updates={"error": "dependency-terminal-failure"},
                )
                actions.append(
                    {"task_id": task_id, "action": "blocked", "task": blocked}
                )
            elif all(dep == "complete" for dep in dep_states):
                envelope = {
                    **task["envelope"],
                    "dependency_results": {
                        dep: state["tasks"][dep]["result_hash"]
                        for dep in task["dependencies"]
                    },
                    "expected_state": "ready",
                    "expected_sequence": task["sequence"] + 1,
                }
                _, ready = _cas_native_task(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    task_id=task_id,
                    expected_state="pending",
                    expected_sequence=task["sequence"],
                    expected_generation=task["generation"],
                    next_state="ready",
                    updates={"envelope": envelope, "error": None},
                )
                actions.append({"task_id": task_id, "action": "ready", "task": ready})
        elif task["state"] == "running" and recover_stale:
            live = load_liveness(root, run_id=run_id, team_id=team_id, task_id=task_id)
            if live is not None and classify_liveness(live, now=now) in {
                "stalled",
                "dead",
            }:
                recovered = recover_native_task(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    task_id=task_id,
                    expected_state="running",
                    expected_sequence=task["sequence"],
                    expected_generation=task["generation"],
                    now=now,
                )
                actions.append(
                    {"task_id": task_id, "action": "recovered", "task": recovered}
                )
    final = load_native_team(root, run_id, team_id)
    return {
        "actions": actions,
        "terminal": all(
            task["state"] in NATIVE_TERMINAL_STATES for task in final["tasks"].values()
        ),
        "complete": all(
            task["state"] == "complete" for task in final["tasks"].values()
        ),
        "revision": final["revision"],
    }


__all__ = [
    "DEFAULT_SUPERVISOR_TIMEOUT_SECONDS",
    "RecoveryError",
    "acquire_supervisor",
    "reconcile_team",
    "recover_native_task",
    "release_supervisor",
    "supervisor_path",
    "supervisor_poll",
]
