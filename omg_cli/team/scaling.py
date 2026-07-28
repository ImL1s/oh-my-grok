"""Team plane lifecycle extensions (D4): dynamic scale + resume.

``omg team scale`` adds/removes panes on a RUNNING team under a file-based
scale lock and ``max_workers_cap()``. ``omg team resume`` reconciles
``team.json`` pane liveness after a leader restart/compaction.

HARD invariants (same as D1–D3):
- CLI single-writer (``writer=omg-cli``); never sets ``verified`` / ``passes``
- Gated by ``OMG_EXPERIMENTAL_TMUX_TEAM=1``; refuse nested worker context
- Bounded by ``max_workers_cap()``; dry-run touches no tmux/subprocess
- Scale-down kills **only** recorded session windows + recorded pgids —
  **no** self-matching ``pkill -f`` / ``pgrep -f``
- Scale-down preserves worktrees (post-mortem); never removes below 1 active
  pane unless the team is being stopped entirely
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from omg_cli.evidence import CLI_WRITER
from omg_cli.fanout import max_workers_cap
from omg_cli.madmax import build_pane_command, tmux_available
from omg_cli.state import _run_dir, load_active_run, load_run, write_status
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    SCHEMA_VERSION,
    TEAM_WORKER_ENV,
    TeamError,
    TeamGateError,
    _build_task_grok_argv,
    _grok_args_for_pane,
    _list_pane_identities,
    _load_team_identity_chain,
    _materialize_task_prompt,
    _pane_env_pairs,
    _pgid_for_pid,
    _pid_start_identity,
    _persist_team_identity_receipt,
    _read_tmux_launch_nonce,
    _read_tmux_session_identity,
    _session_alive,
    _task_role,
    _tmux_run,
    _utc_now,
    _window_alive,
    build_executor_pane_command,
    experimental_enabled,
    in_spawned_worker_context,
    load_team_meta,
    mutate_team_meta,
    team_dir,
    team_meta_path,
)
from omg_cli.team.providers import PROMPT_DELIVERY_PROMPT_FILE, build_executor_argv
from omg_cli.team.routing import ResolvedRouting, RoutingError, resolve_routing
from omg_cli.workers import (
    WorkerError,
    build_ownership_manifest,
    load_ownership_manifest,
    prepare_task,
    worktree_dir,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCALE_LOCK_NAME = "scale.lock"
STATUS_SCALED_DOWN = "scaled_down"
STATUS_NEEDS_COLLECT = "needs_collect"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
ACTIVE_STATUSES = frozenset(
    {
        "running",
        "launched",
        "pending",
        "dry_run",
        STATUS_NEEDS_COLLECT,
        "idle",
        STATUS_BLOCKED,
    }
)
TERMINAL_PANE_STATUSES = frozenset(
    {STATUS_SCALED_DOWN, "stopped", STATUS_FAILED, "completed"}
)


# ---------------------------------------------------------------------------
# Paths / lock
# ---------------------------------------------------------------------------


def scale_lock_path(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / SCALE_LOCK_NAME


@contextmanager
def acquire_scale_lock(root: Path | str, run_id: str) -> Iterator[Path]:
    """Exclusive lifecycle lock under the run team dir (scale/stop/relaunch).

    Uses POSIX ``fcntl.flock`` so the kernel releases the lock if the holder
    process dies (stale files are not exclusive forever). The lock file is
    opened with ``O_NOFOLLOW`` under a managed parent descriptor so a symlink
    cannot redirect truncation outside ``.omg``. PID text is diagnostic only.
    """
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover
        raise TeamError(
            "scale/stop lifecycle lock requires POSIX fcntl.flock"
        ) from exc

    from omg_cli.contracts.path_keys import (
        ContractPathError,
        open_managed_dir_fd,
    )

    path = scale_lock_path(root, run_id)
    try:
        parent_fd = open_managed_dir_fd(path.parent)
    except ContractPathError as exc:
        raise TeamError(f"scale lock parent open refused: {exc}") from exc
    fd = -1
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(SCALE_LOCK_NAME, flags, 0o644, dir_fd=parent_fd)
        except OSError as exc:
            raise TeamError(
                f"scale lock open refused for run {run_id} "
                f"(symlink or non-regular?): {exc}"
            ) from exc
        try:
            import stat as stat_mod

            st = os.fstat(fd)
            if not stat_mod.S_ISREG(st.st_mode):
                raise TeamError(
                    f"scale lock must be a regular file for run {run_id}"
                )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = ""
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                holder = os.read(fd, 64).decode("utf-8", errors="replace").strip()
            except OSError:
                pass
            raise TeamError(
                f"scale lock held for run {run_id}"
                + (f" (pid={holder})" if holder else "")
                + f"; refuse concurrent scale/stop op ({path})"
            ) from exc
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            try:
                os.fsync(fd)
            except OSError:
                pass
            yield path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _assert_team_gates(*, env: Mapping[str, str] | None = None) -> None:
    if not experimental_enabled(env):
        raise TeamGateError(
            f"omg team scale/resume requires {EXPERIMENTAL_ENV}=1 "
            "(experimental tmux team plane; integration isolation only)"
        )
    if in_spawned_worker_context(env):
        raise TeamGateError(
            "omg team scale/resume refused: already inside a spawned-worker "
            f"context (depth-1; {TEAM_WORKER_ENV} or related markers set)"
        )


def _resolve_run_id(root: Path, run_id: str | None) -> str:
    if run_id:
        return str(run_id)
    active = load_active_run(root)
    if active is None:
        raise TeamError("no active run (pass --run ID)")
    return str(active["run_id"])


def _require_team_run(root: Path, run_id: str) -> dict[str, Any]:
    """Fail-closed: run must exist and be a team run with team.json."""
    run = load_run(root, run_id)
    if run is None:
        raise TeamError(f"no run found for --run {run_id!r}")
    path = team_meta_path(root, run_id)
    if not path.is_file():
        raise TeamError(f"team.json missing for run {run_id} (not a team run)")
    meta = load_team_meta(root, run_id)
    # Prefer explicit team flags; still accept CLI-stamped team.json alone.
    if run.get("team") is not True and meta.get("writer") != CLI_WRITER:
        raise TeamError(f"run {run_id} is not a team run")
    return meta


def _active_tasks(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in tasks:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("status") or "") == STATUS_SCALED_DOWN:
            continue
        out.append(dict(raw))
    return out


def _next_worker_index(meta: Mapping[str, Any]) -> int:
    """Monotonic window/worker index; never reuse an index."""
    stored = meta.get("next_worker_index")
    if isinstance(stored, int) and stored >= 0:
        base = stored
    else:
        base = 0
    max_idx = -1
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            max_idx = max(max_idx, int(raw.get("window_index") or 0))
        except (TypeError, ValueError):
            continue
    return max(base, max_idx + 1)


def _synthetic_scale_tasks(n: int, start_index: int) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for i in range(n):
        idx = start_index + i
        tid = f"scale-{idx}"
        tasks.append(
            {
                "task_id": tid,
                "owned_files": [f".omg/team-scale/{tid}.md"],
                "role": "executor",
            }
        )
    return tasks


def _ownership_tasks_from_manifest(root: Path, run_id: str) -> list[dict[str, Any]]:
    try:
        man = load_ownership_manifest(root, run_id)
    except WorkerError:
        return []
    out: list[dict[str, Any]] = []
    for t in man.get("tasks") or []:
        if not isinstance(t, Mapping):
            continue
        tid = str(t.get("task_id") or "")
        if not tid:
            continue
        owned = list(t.get("owned_files") or [])
        role = t.get("role")
        # Manifest stores default "omg-executor"; team plane uses short roles.
        if role in (None, "", "omg-executor"):
            role = "executor"
        entry: dict[str, Any] = {
            "task_id": tid,
            "owned_files": owned,
            "role": role,
        }
        if t.get("coordination"):
            entry["coordination"] = t["coordination"]
        out.append(entry)
    return out


def _build_pane_record(
    *,
    root: Path,
    run_id: str,
    goal: str,
    task: Mapping[str, Any],
    task_index: int,
    task_count: int,
    window_index: int,
    dry_run: bool,
    multi_cli: bool,
    resolved: ResolvedRouting | None,
    yolo: bool,
    safe: bool,
    extra: Sequence[str] | None,
) -> dict[str, Any]:
    tid = str(task["task_id"])
    owned = list(task.get("owned_files") or [])
    wt = worktree_dir(root, run_id, tid)
    role = _task_role(task)
    tdir = team_dir(root, run_id)
    tdir.mkdir(parents=True, exist_ok=True)

    if multi_cli and resolved is not None:
        route = resolved.for_role(role)
        prompt_path = _materialize_task_prompt(
            goal=goal,
            run_id=run_id,
            task_id=tid,
            task_index=task_index,
            task_count=task_count,
            owned_files=owned,
            worktree=wt,
            provider=route.provider,
            role=route.role,
            posture=route.posture,
        )
        inv = build_executor_argv(
            route.provider,
            route.role,
            prompt_file=prompt_path,
            model=route.model,
            cwd=wt,
            check_binary=False,
        )
        argv = list(inv.argv)
        needs_pty = bool(inv.needs_pty)
        provider = inv.provider
        posture = inv.posture
        prompt_delivery = inv.prompt_delivery
        pane_cmd = build_executor_pane_command(
            argv,
            needs_pty=needs_pty,
            prompt_delivery=prompt_delivery,
            prompt_file=prompt_path,
        )
    else:
        argv = _build_task_grok_argv(
            goal=goal,
            run_id=run_id,
            task_id=tid,
            task_index=task_index,
            task_count=task_count,
            owned_files=owned,
            worktree=wt,
            yolo=yolo,
            safe=safe,
            extra=extra,
        )
        needs_pty = False
        provider = "grok"
        posture = "read-write"
        prompt_delivery = PROMPT_DELIVERY_PROMPT_FILE
        pane_cmd = build_pane_command(_grok_args_for_pane(argv))

    argv_path = tdir / f"{tid}.argv.json"
    argv_path.write_text(
        json.dumps(argv, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "task_id": tid,
        "window_index": window_index,
        "worktree": str(wt),
        "argv_path": str(argv_path.relative_to(_run_dir(root, run_id))),
        "pane_command": pane_cmd,
        "argv": argv,
        "role": role,
        "provider": provider,
        "posture": posture,
        "needs_pty": needs_pty,
        "prompt_delivery": prompt_delivery,
        "pid": None,
        "pgid": None,
        "pid_start": None,
        "status": "dry_run" if dry_run else "pending",
        "scaled_in_at": _utc_now(),
    }


def _resolve_routing_from_meta(
    meta: Mapping[str, Any],
    roles_needed: Sequence[str],
) -> ResolvedRouting | None:
    if not meta.get("multi_cli"):
        return None
    routing = meta.get("routing")
    if not isinstance(routing, Mapping):
        return None
    # team.json stores resolved.to_dict() shape: {roles: {role: {...}}, ...}
    # or the original role map. Accept both.
    if "roles" in routing and isinstance(routing.get("roles"), Mapping):
        role_map: dict[str, Any] = {}
        for role, entry in routing["roles"].items():
            if isinstance(entry, Mapping):
                role_map[str(role)] = {
                    "provider": entry.get("provider") or "grok",
                    "model": entry.get("model"),
                }
        raw = role_map
    else:
        raw = dict(routing)
    try:
        return resolve_routing(
            raw,
            roles_needed=list(roles_needed) or ["executor"],
            check_binary=False,
        )
    except RoutingError as exc:
        raise TeamError(f"scale-up routing resolve failed: {exc}") from exc


def _add_tmux_windows(
    *,
    session: str,
    records: Sequence[Mapping[str, Any]],
) -> None:
    """Live path: append windows at explicit indices (never reuse)."""
    if not tmux_available():
        raise TeamError(
            "tmux is required for omg team scale --add (non-dry-run).\n"
            "  Use --dry-run to append team.json entries without launching."
        )
    if not _session_alive(session):
        raise TeamError(
            f"tmux session {session!r} is not alive; cannot scale up. "
            "Use omg team resume / restart the team first."
        )
    for rec in records:
        tid = str(rec["task_id"])
        widx = int(rec["window_index"])
        wt = str(rec["worktree"])
        pane_cmd = str(rec["pane_command"])
        # Target session:index so indices stay monotonic / explicit.
        target = f"{session}:{widx}"
        nw = _tmux_run(
            [
                "new-window",
                "-t",
                target,
                "-n",
                tid,
                "-c",
                wt,
                pane_cmd,
            ]
        )
        if nw.returncode != 0:
            # Fallback without forced index (tmux version quirks)
            nw2 = _tmux_run(
                [
                    "new-window",
                    "-t",
                    session,
                    "-n",
                    tid,
                    "-c",
                    wt,
                    pane_cmd,
                ]
            )
            if nw2.returncode != 0:
                err = (nw2.stderr or nw.stderr or nw2.stdout or "").strip()
                raise TeamError(f"failed to create scaled-in window for {tid!r}: {err}")


def _kill_pane_recorded(
    rec: Mapping[str, Any],
    *,
    session: str,
    dry: bool,
    actions: list[str],
    errors: list[str],
    signalled: list[dict[str, Any]],
    authority: Mapping[str, Any],
) -> None:
    """Kill only an immutable, immediately revalidated pane identity."""
    tid = rec.get("task_id")
    widx = rec.get("window_index")
    pid = rec.get("pid")
    pgid = rec.get("pgid")
    pane_id = rec.get("pane_id")
    pid_start = rec.get("pid_start")

    if not dry:
        if (
            not isinstance(widx, int)
            or not isinstance(pane_id, str)
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(pgid, int)
            or isinstance(pgid, bool)
            or pgid <= 0
            or not isinstance(pid_start, str)
            or not pid_start
            or _read_tmux_session_identity(session)
            != (session, authority.get("session_id"))
            or _read_tmux_launch_nonce(session) != authority.get("launch_nonce")
            or _list_pane_identities(session).get(widx) != (pane_id, pid)
            or _pid_start_identity(pid) != pid_start
            or _pgid_for_pid(pid) != pgid
        ):
            errors.append(f"immutable signal identity mismatch task={tid}")
            return
        # Re-read PID -> PGID at the last possible point before signal.
        observed_pgid = _pgid_for_pid(pid)
        if observed_pgid != pgid:
            errors.append(f"signal PGID drift refused task={tid}")
            return
        try:
            if os.name == "posix":
                os.killpg(observed_pgid, signal.SIGTERM)
                actions.append(f"killpg:SIGTERM pgid={observed_pgid} task={tid}")
            else:
                os.kill(pid, signal.SIGTERM)
                actions.append(f"kill:SIGTERM pid={pid} task={tid}")
            signalled.append({"task_id": tid, "pgid": observed_pgid, "pid": pid})
        except ProcessLookupError:
            actions.append(f"process already gone pgid={observed_pgid} task={tid}")
        except (PermissionError, OSError) as exc:
            errors.append(f"signal task={tid} target={observed_pgid}: {exc}")
            return
    elif dry:
        actions.append(f"dry_run: skipped kill for task={tid}")

    # 2) kill-window only (NOT kill-session)
    if session and not dry and widx is not None:
        try:
            if tmux_available():
                r = _tmux_run(["kill-window", "-t", f"{session}:{int(widx)}"])
                actions.append(
                    f"tmux kill-window -t {session}:{widx} (exit {r.returncode})"
                )
            else:
                actions.append("tmux unavailable; skipped kill-window")
        except OSError as exc:
            errors.append(f"tmux kill-window task={tid}: {exc}")


# ---------------------------------------------------------------------------
# scale up / down
# ---------------------------------------------------------------------------


def scale_team(
    root: Path | str | None = None,
    run_id: str | None = None,
    *,
    add: int | None = None,
    remove: int | None = None,
    dry_run: bool = False,
    yolo: bool = False,
    safe: bool = False,
    extra: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    tasks_json: str | Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Scale a RUNNING team up (``--add N``) or down (``--remove N``).

    Exactly one of *add* / *remove* must be a positive int. Never sets verified.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    _assert_team_gates(env=env)
    rid = _resolve_run_id(root_path, run_id)

    add_n = int(add) if add is not None else 0
    remove_n = int(remove) if remove is not None else 0
    if (add_n > 0) == (remove_n > 0):
        raise TeamError(
            "omg team scale requires exactly one of --add N or --remove N "
            f"(got add={add!r} remove={remove!r})"
        )
    if add_n < 0 or remove_n < 0:
        raise TeamError("--add / --remove must be positive integers")

    with acquire_scale_lock(root_path, rid):
        meta = _require_team_run(root_path, rid)
        stop_state = str(meta.get("stop_state") or "")
        if stop_state in {"stopping", "stopped", "stop_refused"} or meta.get(
            "stopped_at"
        ):
            raise TeamError(
                "scale refused: team is stopping/stopped "
                f"(stop_state={stop_state!r}); re-check status"
            )
        if add_n > 0:
            return _scale_up(
                root_path,
                rid,
                meta,
                n=add_n,
                dry_run=dry_run,
                yolo=yolo,
                safe=safe,
                extra=extra,
                tasks_json=tasks_json,
            )
        return _scale_down(
            root_path,
            rid,
            meta,
            n=remove_n,
            dry_run=dry_run,
        )


def _scale_up(
    root: Path,
    run_id: str,
    meta: dict[str, Any],
    *,
    n: int,
    dry_run: bool,
    yolo: bool,
    safe: bool,
    extra: Sequence[str] | None,
    tasks_json: str | Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    tasks_all = list(meta.get("tasks") or [])
    active = _active_tasks(tasks_all)
    cap = max_workers_cap()
    if len(active) + n > cap:
        raise TeamGateError(
            f"scale --add {n} refused: current_active={len(active)} + {n} "
            f"exceeds hard cap {cap} (OMG_MAX_WORKERS / max_workers_cap)"
        )

    start_idx = _next_worker_index(meta)
    if tasks_json is not None:
        from omg_cli.team.plane import _parse_tasks_json

        new_task_specs = _parse_tasks_json(tasks_json)
        if len(new_task_specs) != n:
            raise TeamError(
                f"--tasks-json length {len(new_task_specs)} must equal --add {n}"
            )
    else:
        new_task_specs = _synthetic_scale_tasks(n, start_idx)

    # Merge ownership: existing + new (CLI rewrite; single-writer)
    existing_own = _ownership_tasks_from_manifest(root, run_id)
    if not existing_own:
        # Fall back to team.json task records
        for rec in tasks_all:
            if not isinstance(rec, Mapping):
                continue
            tid = str(rec.get("task_id") or "")
            if not tid:
                continue
            existing_own.append(
                {
                    "task_id": tid,
                    "owned_files": [f".omg/team-scale/{tid}.md"],
                    "role": rec.get("role") or "executor",
                }
            )
    merged = existing_own + new_task_specs
    try:
        manifest = build_ownership_manifest(root, run_id, merged)
        for mtask in manifest.get("tasks") or []:
            tid = str(mtask["task_id"])
            # Only prepare NEW worktrees (existing already prepared)
            if any(str(t.get("task_id")) == tid for t in new_task_specs):
                prepare_task(root, run_id, tid)
    except WorkerError as exc:
        raise TeamError(str(exc)) from exc

    multi_cli = bool(meta.get("multi_cli"))
    roles = [_task_role(t) for t in new_task_specs]
    resolved = _resolve_routing_from_meta(meta, roles) if multi_cli else None
    goal = str(meta.get("goal") or "(no goal)")
    # Effective dry_run: explicit flag OR team already dry_run skeleton
    effective_dry = bool(dry_run or meta.get("dry_run"))

    new_records: list[dict[str, Any]] = []
    total_after = len(active) + n
    for i, spec in enumerate(new_task_specs):
        widx = start_idx + i
        rec = _build_pane_record(
            root=root,
            run_id=run_id,
            goal=goal,
            task=spec,
            task_index=len(active) + i + 1,
            task_count=total_after,
            window_index=widx,
            dry_run=effective_dry,
            multi_cli=multi_cli,
            resolved=resolved,
            yolo=yolo,
            safe=safe,
            extra=extra,
        )
        new_records.append(rec)

    if not effective_dry:
        session = str(meta.get("session") or "")
        try:
            chain = _load_team_identity_chain(root, run_id, meta)
            authority = chain[0]
            _add_tmux_windows(session=session, records=new_records)
            pane_identities = _list_pane_identities(session)
            for rec in new_records:
                widx = int(rec["window_index"])
                pane_identity = pane_identities.get(widx)
                if pane_identity is not None:
                    pane_id, pid = pane_identity
                    rec["pane_id"] = pane_id
                    rec["pid"] = pid
                    rec["pgid"] = _pgid_for_pid(pid)
                    rec["pid_start"] = _pid_start_identity(pid)
                    rec["status"] = (
                        STATUS_RUNNING
                        if rec["pgid"] is not None and rec["pid_start"] is not None
                        else "launched"
                    )
                else:
                    rec["status"] = "launched"
            if any(rec["status"] != STATUS_RUNNING for rec in new_records):
                raise TeamError("scale-up failed to bind complete worker identity")
            generation = int(meta.get("identity_generation", 0)) + 1
            _scale_receipt, scale_receipt_hash = _persist_team_identity_receipt(
                root,
                run_id,
                session=session,
                session_id=str(authority["session_id"]),
                launch_nonce=str(authority["launch_nonce"]),
                generation=generation,
                previous_receipt_sha256=str(
                    meta.get("identity_receipt_sha256")
                    or meta.get("launch_receipt_sha256")
                ),
                operation="add",
                tasks_before=active,
                tasks_after=[*active, *new_records],
            )
        except TeamError:
            raise
        except OSError as exc:
            raise TeamError(f"scale-up tmux launch failed: {exc}") from exc

    scale_at = _utc_now()
    last_scale = {
        "op": "add",
        "n": n,
        "window_indices": [r["window_index"] for r in new_records],
        "task_ids": [r["task_id"] for r in new_records],
        "dry_run": effective_dry,
    }
    new_task_list = list(tasks_all) + new_records
    new_task_count = len(_active_tasks(new_task_list))
    next_idx = start_idx + n
    identity_gen = generation if not effective_dry else None
    identity_hash = scale_receipt_hash if not effective_dry else None

    base_generation = int(meta.get("meta_generation") or 0)

    def _apply_scale_up(current: dict[str, Any]) -> dict[str, Any]:
        # Refuse to revive or extend a team that was stopped while we scaled.
        stop_state = str(current.get("stop_state") or "")
        if stop_state in {"stopping", "stopped", "stop_refused"} or current.get(
            "stopped_at"
        ):
            raise TeamError(
                "scale-up refused: team is stopping/stopped "
                f"(stop_state={stop_state!r}); re-check status"
            )
        updated = dict(current)
        updated["schema_version"] = int(
            current.get("schema_version") or SCHEMA_VERSION
        )
        updated["tasks"] = list(new_task_list)
        updated["task_count"] = new_task_count
        updated["next_worker_index"] = next_idx
        updated["last_scale_at"] = scale_at
        updated["last_scale"] = dict(last_scale)
        if identity_gen is not None:
            updated["identity_generation"] = identity_gen
            updated["identity_receipt_sha256"] = identity_hash
        return updated

    try:
        updated = mutate_team_meta(
            root,
            run_id,
            _apply_scale_up,
            expected_generation=base_generation,
        )
    except TeamError as exc:
        # Panes/receipt may already exist; merge new workers onto latest meta
        # (same pattern as scale-down CAS loss).
        if "stale team meta generation" not in str(exc):
            raise

        def _reconcile_scale_up(current: dict[str, Any]) -> dict[str, Any]:
            stop_state = str(current.get("stop_state") or "")
            if stop_state in {"stopping", "stopped", "stop_refused"} or current.get(
                "stopped_at"
            ):
                raise TeamError(
                    "scale-up refused after launch side effects: team is "
                    f"stopping/stopped (stop_state={stop_state!r}); re-check status"
                )
            updated = dict(current)
            existing_ids = {
                str(t.get("task_id"))
                for t in (current.get("tasks") or [])
                if isinstance(t, Mapping) and t.get("task_id")
            }
            merged = [
                dict(t)
                for t in (current.get("tasks") or [])
                if isinstance(t, Mapping)
            ]
            for rec in new_records:
                tid = str(rec.get("task_id") or "")
                if tid and tid not in existing_ids:
                    merged.append(dict(rec))
                    existing_ids.add(tid)
            updated["tasks"] = merged
            updated["task_count"] = len(_active_tasks(merged))
            updated["next_worker_index"] = max(
                int(current.get("next_worker_index") or 0), next_idx
            )
            updated["last_scale_at"] = scale_at
            updated["last_scale"] = dict(last_scale)
            if identity_gen is not None:
                updated["identity_generation"] = identity_gen
                updated["identity_receipt_sha256"] = identity_hash
            return updated

        updated = mutate_team_meta(root, run_id, _reconcile_scale_up)

    try:
        write_status(
            root,
            run_id,
            "running",
            extra={
                "team": True,
                "stage": "team_scaled_up",
                "scaled_add": n,
                "active_panes": updated["task_count"],
                "note": "scale-up never sets verified",
            },
        )
    except Exception:
        # Non-fatal for dry-run legacy status maps
        pass

    return {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "op": "add",
        "added": n,
        "task_ids": [r["task_id"] for r in new_records],
        "window_indices": [r["window_index"] for r in new_records],
        "active_panes": updated["task_count"],
        "next_worker_index": updated["next_worker_index"],
        "dry_run": effective_dry,
        "cap": cap,
        "verified": False,
        "note": (
            "scale-up appends panes; dry_run pid=None; "
            "never sets verified; bounded by max_workers_cap"
        ),
        "tasks_added": new_records,
    }


def _scale_down(
    root: Path,
    run_id: str,
    meta: dict[str, Any],
    *,
    n: int,
    dry_run: bool,
) -> dict[str, Any]:
    tasks_all: list[dict[str, Any]] = []
    for raw in meta.get("tasks") or []:
        if isinstance(raw, Mapping):
            tasks_all.append(dict(raw))

    active = [t for t in tasks_all if str(t.get("status") or "") != STATUS_SCALED_DOWN]
    if len(active) <= 1:
        raise TeamError(
            "scale --remove refused: never remove below 1 active pane "
            "(use omg team stop to tear down the whole team)"
        )
    if n >= len(active):
        raise TeamError(
            f"scale --remove {n} refused: would leave "
            f"{len(active) - n} active panes; minimum is 1 "
            f"(active={len(active)}; use omg team stop for full teardown)"
        )

    # Graceful drain: prefer idle/newest (highest window_index)
    def _drain_key(t: Mapping[str, Any]) -> tuple[int, int]:
        st = str(t.get("status") or "")
        idle_rank = 0 if st in ("idle", "dry_run", "pending", "launched") else 1
        try:
            widx = int(t.get("window_index") or 0)
        except (TypeError, ValueError):
            widx = 0
        # Sort: idle first, then newest (highest index) first
        return (idle_rank, -widx)

    ordered = sorted(active, key=_drain_key)
    victims = ordered[:n]
    victim_ids = {str(v.get("task_id")) for v in victims}

    session = str(meta.get("session") or "")
    effective_dry = bool(dry_run or meta.get("dry_run"))
    actions: list[str] = []
    errors: list[str] = []
    signalled: list[dict[str, Any]] = []
    preserved_worktrees: list[str] = []

    authority: Mapping[str, Any] = {}
    generation: int | None = None
    scale_receipt_hash: str | None = None
    immutable_victims: dict[str, Mapping[str, Any]] = {}
    survivors = [task for task in active if str(task.get("task_id")) not in victim_ids]
    if not effective_dry:
        chain = _load_team_identity_chain(root, run_id, meta)
        authority = chain[0]
        generation = int(meta.get("identity_generation", 0)) + 1
        _scale_receipt, scale_receipt_hash = _persist_team_identity_receipt(
            root,
            run_id,
            session=session,
            session_id=str(authority["session_id"]),
            launch_nonce=str(authority["launch_nonce"]),
            generation=generation,
            previous_receipt_sha256=str(
                meta.get("identity_receipt_sha256") or meta.get("launch_receipt_sha256")
            ),
            operation="remove",
            tasks_before=active,
            tasks_after=survivors,
        )
        immutable_victims = {
            str(row.get("task_id")): row
            for row in _scale_receipt["tasks_before"]
            if str(row.get("task_id")) in victim_ids
        }

    for v in victims:
        signal_identity = immutable_victims.get(str(v.get("task_id")), v)
        _kill_pane_recorded(
            signal_identity,
            session=session,
            dry=effective_dry,
            actions=actions,
            errors=errors,
            signalled=signalled,
            authority=authority,
        )
        wt = str(v.get("worktree") or "")
        if wt:
            preserved_worktrees.append(wt)
    if errors:
        raise TeamError(
            "scale-down refused incomplete cancellation: " + "; ".join(errors)
        )

    # Mark scaled_down; PRESERVE worktrees (do not delete)
    now = _utc_now()
    for rec in tasks_all:
        if str(rec.get("task_id")) in victim_ids:
            rec["status"] = STATUS_SCALED_DOWN
            rec["scaled_down_at"] = now
            # Clear live handles; keep historical pid/pgid for audit
            rec["pid"] = None
            rec["pgid"] = None

    down_task_list = list(tasks_all)
    down_task_count = len(_active_tasks(down_task_list))
    last_scale_down = {
        "op": "remove",
        "n": n,
        "task_ids": sorted(victim_ids),
        "preserved_worktrees": preserved_worktrees,
        "actions": actions,
        "dry_run": effective_dry,
    }
    down_identity_gen = generation if not effective_dry else None
    down_identity_hash = scale_receipt_hash if not effective_dry else None

    down_base_generation = int(meta.get("meta_generation") or 0)

    def _apply_scale_down(current: dict[str, Any]) -> dict[str, Any]:
        stop_state = str(current.get("stop_state") or "")
        if stop_state in {"stopping", "stopped", "stop_refused"} or current.get(
            "stopped_at"
        ):
            raise TeamError(
                "scale-down refused: team is stopping/stopped "
                f"(stop_state={stop_state!r}); re-check status"
            )
        updated = dict(current)
        updated["tasks"] = list(down_task_list)
        updated["task_count"] = down_task_count
        updated["last_scale_at"] = now
        updated["last_scale"] = dict(last_scale_down)
        if down_identity_gen is not None:
            updated["identity_generation"] = down_identity_gen
            updated["identity_receipt_sha256"] = down_identity_hash
        return updated

    try:
        updated = mutate_team_meta(
            root,
            run_id,
            _apply_scale_down,
            expected_generation=down_base_generation,
        )
    except TeamError as exc:
        # Process side effects may already be done; reconcile victim statuses
        # onto the latest document without requiring the pre-side-effect generation.
        if "stale team meta generation" not in str(exc):
            raise

        def _reconcile_scale_down(current: dict[str, Any]) -> dict[str, Any]:
            stop_state = str(current.get("stop_state") or "")
            if stop_state in {"stopping", "stopped", "stop_refused"} or current.get(
                "stopped_at"
            ):
                raise TeamError(
                    "scale-down refused after side effects: team is stopping/stopped "
                    f"(stop_state={stop_state!r}); re-check status"
                )
            updated = dict(current)
            by_id = {
                str(t.get("task_id")): t
                for t in down_task_list
                if isinstance(t, Mapping) and t.get("task_id")
            }
            merged: list[dict[str, Any]] = []
            for raw in list(current.get("tasks") or []):
                if not isinstance(raw, Mapping):
                    continue
                rec = dict(raw)
                tid = str(rec.get("task_id") or "")
                src = by_id.get(tid)
                if src is not None and tid in victim_ids:
                    for key in ("status", "scaled_down_at", "pid", "pgid"):
                        if key in src:
                            rec[key] = src[key]
                merged.append(rec)
            updated["tasks"] = merged
            updated["task_count"] = len(_active_tasks(merged))
            updated["last_scale_at"] = now
            updated["last_scale"] = dict(last_scale_down)
            if down_identity_gen is not None:
                updated["identity_generation"] = down_identity_gen
                updated["identity_receipt_sha256"] = down_identity_hash
            return updated

        updated = mutate_team_meta(root, run_id, _reconcile_scale_down)

    try:
        write_status(
            root,
            run_id,
            "running",
            extra={
                "team": True,
                "stage": "team_scaled_down",
                "scaled_remove": n,
                "active_panes": updated["task_count"],
                "note": "scale-down preserves worktrees; never sets verified",
            },
        )
    except Exception:
        pass

    return {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "op": "remove",
        "removed": n,
        "task_ids": sorted(victim_ids),
        "active_panes": updated["task_count"],
        "preserved_worktrees": preserved_worktrees,
        "actions": actions,
        "signalled": signalled,
        "errors": errors,
        "dry_run": effective_dry,
        "verified": False,
        "note": (
            "scale-down marks scaled_down; kills only recorded pgids + "
            "tmux windows (not session); preserves worktrees; no pkill -f"
        ),
    }


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def _reconcile_resume_tasks(
    meta: Mapping[str, Any],
    *,
    probe_tmux: bool,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], int]:
    """Probe liveness for *meta* tasks; return (status_by_tid, reconciliations, changed)."""

    session = str(meta.get("session") or "")
    dry = bool(meta.get("dry_run"))
    changed = 0
    tasks_out: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []

    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        rec = dict(raw)
        tid = str(rec.get("task_id") or "")
        prev = str(rec.get("status") or "unknown")
        widx = int(rec.get("window_index") or 0)

        if prev == STATUS_SCALED_DOWN:
            tasks_out.append(rec)
            continue

        if dry or prev == "dry_run":
            tasks_out.append(rec)
            continue

        if not probe_tmux:
            tasks_out.append(rec)
            continue

        pane_id = rec.get("pane_id")
        if isinstance(pane_id, str) and pane_id:
            from omg_cli.team.tmux import pane_alive

            win = pane_alive(pane_id)
        else:
            win = _window_alive(session, widx)
        if win is True:
            new_st = STATUS_RUNNING
            alive = True
        elif win is False:
            if prev in ("stopped", STATUS_FAILED, STATUS_BLOCKED):
                new_st = prev
            else:
                new_st = STATUS_NEEDS_COLLECT
            alive = False
        else:
            new_st = prev
            alive = None

        if new_st != prev:
            rec["status"] = new_st
            rec["resumed_at"] = _utc_now()
            rec["status_before_resume"] = prev
            changed += 1
            reconciliations.append(
                {
                    "task_id": tid,
                    "window_index": widx,
                    "from": prev,
                    "to": new_st,
                    "alive": alive,
                }
            )
        else:
            reconciliations.append(
                {
                    "task_id": tid,
                    "window_index": widx,
                    "from": prev,
                    "to": prev,
                    "alive": alive,
                    "unchanged": True,
                }
            )
        tasks_out.append(rec)

    status_by_tid: dict[str, dict[str, Any]] = {}
    for rec in tasks_out:
        tid_key = str(rec.get("task_id") or "")
        if tid_key:
            status_by_tid[tid_key] = rec
    return status_by_tid, reconciliations, changed


def resume_team(
    root: Path | str | None = None,
    run_id: str | None = None,
    *,
    probe_tmux: bool = True,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Reconcile team.json pane statuses after leader restart.

    Idempotent: only status reconciliation writes (CLI_WRITER-stamped).
    Never sets verified. Fail-closed if not a team run / team.json missing.

    Prefer pane-id liveness when recorded (split topology); fall back to
    window-index probes for legacy windows topology / hermetic mocks.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    _assert_team_gates(env=env)
    rid = _resolve_run_id(root_path, run_id)

    # Up to two attempts: on stale meta_generation, re-load + re-probe so a
    # concurrent stop/scale cannot be overwritten by a stale liveness snapshot.
    updated: dict[str, Any] | None = None
    session = ""
    dry = False
    changed = 0
    reconciliations: list[dict[str, Any]] = []
    for attempt in range(2):
        meta = _require_team_run(root_path, rid)
        session = str(meta.get("session") or "")
        dry = bool(meta.get("dry_run"))
        status_by_tid, reconciliations, changed = _reconcile_resume_tasks(
            meta, probe_tmux=probe_tmux
        )
        resumed_at = _utc_now()
        resume_changes = changed
        base_generation = int(meta.get("meta_generation") or 0)

        def _apply_resume(
            current: dict[str, Any],
            *,
            _status_by_tid: dict[str, dict[str, Any]] = status_by_tid,
            _resume_changes: int = resume_changes,
            _resumed_at: str = resumed_at,
        ) -> dict[str, Any]:
            # Merge probed status onto the locked task list by task_id so a
            # concurrent scale-add worker is preserved (Codex P1).
            next_doc = dict(current)
            merged: list[dict[str, Any]] = []
            for raw_task in list(current.get("tasks") or []):
                if not isinstance(raw_task, Mapping):
                    continue
                rec = dict(raw_task)
                tid_key = str(rec.get("task_id") or "")
                src = _status_by_tid.get(tid_key)
                if src is not None:
                    for key in ("status", "resumed_at", "status_before_resume"):
                        if key in src:
                            rec[key] = src[key]
                merged.append(rec)
            next_doc["tasks"] = merged
            next_doc["task_count"] = len(_active_tasks(merged))
            next_doc["resumed_at"] = _resumed_at
            next_doc["resume_changes"] = _resume_changes
            return next_doc

        try:
            updated = mutate_team_meta(
                root_path,
                rid,
                _apply_resume,
                expected_generation=base_generation,
            )
            break
        except TeamError as exc:
            if attempt == 0 and "stale team meta generation" in str(exc):
                continue
            raise

    assert updated is not None  # for type checkers; loop always sets or raises
    return {
        "writer": CLI_WRITER,
        "run_id": rid,
        "session": session,
        "dry_run": dry,
        "changes": changed,
        "reconciliations": reconciliations,
        "active_panes": updated["task_count"],
        "linked_ralph": updated.get("linked_ralph"),
        "verified": False,
        "note": (
            "resume reconciles pane liveness into team.json; "
            "never sets verified; enables status/collect/scale/stop after "
            "leader restart"
        ),
    }


def _worktree_dirty(worktree: Path) -> bool:
    """True when the worktree has meaningful uncommitted changes.

    Untracked ``.omg/`` under the worktree is ignored (control-plane noise from
    worker env / shared layout). Any other porcelain entry fails closed as dirty.
    """
    if not worktree.is_dir():
        return True
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if proc.returncode != 0:
        return True
    for raw in (proc.stdout or "").splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) >= 4 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[-1].strip()
        path = path.strip().strip('"')
        if path == ".omg" or path.startswith(".omg/"):
            continue
        return True
    return False


def _worker_api_tasks_terminal(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    env: Mapping[str, str] | None,
) -> bool | None:
    """Return True when every owned API task is terminal; None if unknown."""
    from omg_cli.team.api import TERMINAL_TASK_STATUSES, execute_team_api

    api_env = dict(env or {})
    api_env.setdefault(EXPERIMENTAL_ENV, "1")
    for key in (
        "OMG_TEAM_WORKER",
        "OMG_PROCESS_FANOUT_WORKER",
        "OMG_SPAWNED_WORKER",
    ):
        api_env.pop(key, None)
    code, envelope = execute_team_api(
        "list-tasks",
        {"run_id": run_id, "team_id": team_id},
        root=root,
        env=api_env,
    )
    if code != 0 or not envelope.get("ok"):
        return None
    tasks = (envelope.get("data") or {}).get("tasks") or []
    owned = [
        t
        for t in tasks
        if isinstance(t, Mapping)
        and (
            str(t.get("owner") or "") == worker_id
            or (
                isinstance(t.get("claim"), Mapping)
                and str((t.get("claim") or {}).get("owner") or "") == worker_id
            )
        )
    ]
    if not owned:
        # Seeded shorthand tasks often have no owner yet — treat as incomplete
        # unless every board task is already terminal.
        if not tasks:
            return False
        return all(
            isinstance(t, Mapping)
            and str(t.get("status") or "") in TERMINAL_TASK_STATUSES
            for t in tasks
        )
    return all(str(t.get("status") or "") in TERMINAL_TASK_STATUSES for t in owned)


def _bind_pane_process(rec: dict[str, Any], pane_id: str) -> None:
    from omg_cli.team.tmux import _bind_pane_pid

    pid = _bind_pane_pid(pane_id)
    rec["pane_id"] = pane_id
    rec["pid"] = pid
    rec["pgid"] = _pgid_for_pid(pid)
    rec["pid_start"] = _pid_start_identity(pid)
    rec["status"] = (
        STATUS_RUNNING
        if rec["pgid"] is not None and rec["pid_start"] is not None
        else "launched"
    )


def _resync_window_indices(
    session: str, tasks: Sequence[dict[str, Any]]
) -> None:
    """Align logical window_index slots with live pane_index after respawn."""
    observed = _list_pane_identities(session)
    by_pane = {pane_id: idx for idx, (pane_id, _pid) in observed.items()}
    for rec in tasks:
        pane_id = rec.get("pane_id")
        if isinstance(pane_id, str) and pane_id in by_pane:
            rec["window_index"] = by_pane[pane_id]


def relaunch_dead_incomplete_workers(
    root: Path | str | None = None,
    run_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Respawn dead panes for non-terminal tasks (generation +1) or mark blocked.

    Call after :func:`resume_team` reconciliation. Clean worktrees with matching
    launch identity are respawned; dirty / identity-drift trees are left alone
    and reported as ``blocked``.

    Live (non-dry) relaunch shares the run-dir :func:`acquire_scale_lock` with
    ``omg team scale`` so concurrent scale/resume cannot double-spawn panes or
    last-writer-wins ``team.json``.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    _assert_team_gates(env=env)
    rid = _resolve_run_id(root_path, run_id)
    meta = _require_team_run(root_path, rid)

    if bool(meta.get("dry_run")):
        return {
            "writer": CLI_WRITER,
            "run_id": rid,
            "relaunched": [],
            "blocked": [],
            "skipped": [],
            "identity_generation": int(meta.get("identity_generation") or 0),
            "verified": False,
            "note": "dry_run resume skips worker relaunch",
        }

    with acquire_scale_lock(root_path, rid):
        # Re-read under the lock so scale/resume cannot race a stale snapshot.
        return _relaunch_dead_incomplete_workers_locked(
            root_path, rid, env=env
        )


def _relaunch_dead_incomplete_workers_locked(
    root_path: Path,
    rid: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Body of relaunch; caller must hold :func:`acquire_scale_lock`."""
    from omg_cli.team.tmux import TmuxTeamError, pane_alive, respawn_worker_pane

    meta = _require_team_run(root_path, rid)
    if bool(meta.get("dry_run")):
        return {
            "writer": CLI_WRITER,
            "run_id": rid,
            "relaunched": [],
            "blocked": [],
            "skipped": [],
            "identity_generation": int(meta.get("identity_generation") or 0),
            "verified": False,
            "note": "dry_run resume skips worker relaunch",
        }

    session = str(meta.get("session") or "")
    team_id = str(meta.get("team_id") or "team")
    owner_token = meta.get("owner_token")
    relaunched: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    tasks_all: list[dict[str, Any]] = []
    for raw in meta.get("tasks") or []:
        if isinstance(raw, Mapping):
            tasks_all.append(dict(raw))

    candidates: list[dict[str, Any]] = []
    for rec in tasks_all:
        tid = str(rec.get("task_id") or "")
        status = str(rec.get("status") or "")
        if status in TERMINAL_PANE_STATUSES or status == STATUS_SCALED_DOWN:
            skipped.append({"task_id": tid, "reason": "terminal_or_scaled_down"})
            continue
        pane_id = rec.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            skipped.append({"task_id": tid, "reason": "no_pane_id"})
            continue
        alive = pane_alive(pane_id)
        if alive is True:
            skipped.append({"task_id": tid, "reason": "alive"})
            continue
        if alive is None:
            skipped.append({"task_id": tid, "reason": "tmux_unavailable"})
            continue
        # Dead pane.
        terminal = _worker_api_tasks_terminal(
            root_path,
            run_id=rid,
            team_id=team_id,
            worker_id=tid,
            env=env,
        )
        if terminal is True:
            skipped.append({"task_id": tid, "reason": "api_tasks_terminal"})
            continue
        candidates.append(rec)

    if not candidates:
        return {
            "writer": CLI_WRITER,
            "run_id": rid,
            "relaunched": [],
            "blocked": [],
            "skipped": skipped,
            "identity_generation": int(meta.get("identity_generation") or 0),
            "verified": False,
            "note": "no dead incomplete workers to relaunch",
        }

    if not session or not _session_alive(session):
        raise TeamError(
            f"tmux session {session!r} is not alive; cannot relaunch workers"
        )

    # Validate launch identity chain before mutating panes.
    chain = _load_team_identity_chain(root_path, rid, meta)
    authority = chain[0]
    if _read_tmux_session_identity(session) != (
        session,
        authority.get("session_id"),
    ):
        raise TeamError("live tmux session identity mismatch; refuse relaunch")
    if _read_tmux_launch_nonce(session) != authority.get("launch_nonce"):
        raise TeamError("live tmux launch nonce mismatch; refuse relaunch")

    target = str(meta.get("window_id") or session)
    tasks_before = _active_tasks(tasks_all)
    to_relaunch: list[dict[str, Any]] = []

    for rec in candidates:
        tid = str(rec.get("task_id") or "")
        wt = Path(str(rec.get("worktree") or ""))
        if _worktree_dirty(wt):
            rec["status"] = STATUS_BLOCKED
            rec["resume_blocked_at"] = _utc_now()
            rec["resume_block_reason"] = "dirty_worktree"
            blocked.append(
                {
                    "task_id": tid,
                    "reason": "dirty_worktree",
                    "worktree": str(wt),
                }
            )
            continue
        to_relaunch.append(rec)

    for rec in to_relaunch:
        tid = str(rec["task_id"])
        env_pairs = _pane_env_pairs(
            run_id=rid,
            team_id=team_id,
            worker_id=tid,
            leader_root=root_path,
            state_root=root_path / ".omg" / "state",
            owner_token=str(owner_token) if owner_token else None,
        )
        try:
            new_pane = respawn_worker_pane(
                target=target,
                worktree=str(rec["worktree"]),
                pane_command=str(rec["pane_command"]),
                env_pairs=env_pairs,
            )
        except TmuxTeamError as exc:
            raise TeamError(f"failed to relaunch worker {tid!r}: {exc}") from exc
        old_pane = rec.get("pane_id")
        _bind_pane_process(rec, new_pane)
        rec["resumed_at"] = _utc_now()
        rec["status_before_resume"] = STATUS_NEEDS_COLLECT
        rec.pop("resume_block_reason", None)
        rec.pop("resume_blocked_at", None)
        relaunched.append(
            {
                "task_id": tid,
                "from_pane_id": old_pane,
                "pane_id": new_pane,
                "pid": rec.get("pid"),
            }
        )

    if relaunched:
        _resync_window_indices(session, tasks_all)
        active_after = _active_tasks(tasks_all)
        generation = int(meta.get("identity_generation", 0)) + 1
        _receipt, receipt_hash = _persist_team_identity_receipt(
            root_path,
            rid,
            session=session,
            session_id=str(authority["session_id"]),
            launch_nonce=str(authority["launch_nonce"]),
            generation=generation,
            previous_receipt_sha256=str(
                meta.get("identity_receipt_sha256")
                or meta.get("launch_receipt_sha256")
            ),
            operation="relaunch",
            tasks_before=tasks_before,
            tasks_after=active_after,
        )
        identity_gen = generation
        identity_hash = receipt_hash
    else:
        generation = int(meta.get("identity_generation") or 0)
        identity_gen = None
        identity_hash = None

    relaunch_tasks = list(tasks_all)
    relaunch_count = len(_active_tasks(relaunch_tasks))
    relaunch_at = _utc_now()
    last_relaunch = {
        "relaunched": [r["task_id"] for r in relaunched],
        "blocked": [b["task_id"] for b in blocked],
        "at": relaunch_at,
    }

    def _apply_relaunch(current: dict[str, Any]) -> dict[str, Any]:
        out = dict(current)
        out["tasks"] = list(relaunch_tasks)
        out["task_count"] = relaunch_count
        out["resumed_at"] = relaunch_at
        out["last_relaunch"] = dict(last_relaunch)
        if identity_gen is not None:
            out["identity_generation"] = identity_gen
            out["identity_receipt_sha256"] = identity_hash
        return out

    mutate_team_meta(root_path, rid, _apply_relaunch)

    return {
        "writer": CLI_WRITER,
        "run_id": rid,
        "relaunched": relaunched,
        "blocked": blocked,
        "skipped": skipped,
        "identity_generation": generation,
        "verified": False,
        "note": (
            "relaunch respawns dead incomplete workers at generation+1 when "
            "worktree is clean; dirty/identity drift reported as blocked"
        ),
    }


def native_dispatch_plan(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    max_concurrency: int,
) -> dict[str, Any]:
    """Select ready tasks without changing lanes or launching processes."""

    from omg_cli.fanout import max_workers_cap
    from omg_cli.team.plane import load_native_team

    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
        raise TeamError("native max_concurrency must be an integer")
    hard_cap = max_workers_cap()
    if not 1 <= max_concurrency <= hard_cap:
        raise TeamError(f"native max_concurrency must be between 1 and {hard_cap}")
    state = load_native_team(root, run_id, team_id)
    active_states = {
        "spawn_requested",
        "launch_unknown",
        "running",
    }
    active = sum(task["state"] in active_states for task in state["tasks"].values())
    slots = max(0, max_concurrency - active)
    ready = [
        {
            "task_id": task_id,
            "sequence": task["sequence"],
            "generation": task["generation"],
            "logical_role": task["logical_role"],
            "capability_mode": task["envelope"]["capability_mode"],
        }
        for task_id, task in sorted(state["tasks"].items())
        if task["state"] == "ready"
    ][:slots]
    return {
        "run_id": run_id,
        "team_id": team_id,
        "transport": state["transport"],
        "max_concurrency": max_concurrency,
        "active": active,
        "slots": slots,
        "ready": ready,
        "blocked_by_capacity": max(
            0,
            sum(task["state"] == "ready" for task in state["tasks"].values())
            - len(ready),
        ),
    }


__all__ = [
    "SCALE_LOCK_NAME",
    "STATUS_BLOCKED",
    "STATUS_NEEDS_COLLECT",
    "STATUS_SCALED_DOWN",
    "acquire_scale_lock",
    "relaunch_dead_incomplete_workers",
    "resume_team",
    "native_dispatch_plan",
    "scale_lock_path",
    "scale_team",
]
