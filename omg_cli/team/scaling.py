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
from contextlib import contextmanager
from datetime import datetime, timezone
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
    _atomic_write_json,
    _build_task_grok_argv,
    _grok_args_for_pane,
    _list_pane_pids,
    _materialize_task_prompt,
    _pgid_for_pid,
    _session_alive,
    _task_role,
    _tmux_run,
    _utc_now,
    _window_alive,
    build_executor_pane_command,
    experimental_enabled,
    in_spawned_worker_context,
    load_team_meta,
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
ACTIVE_STATUSES = frozenset(
    {
        "running",
        "launched",
        "pending",
        "dry_run",
        STATUS_NEEDS_COLLECT,
        "idle",
    }
)


# ---------------------------------------------------------------------------
# Paths / lock
# ---------------------------------------------------------------------------


def scale_lock_path(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / SCALE_LOCK_NAME


@contextmanager
def acquire_scale_lock(root: Path | str, run_id: str) -> Iterator[Path]:
    """Exclusive file lock under the run team dir (refuse concurrent scale).

    Uses ``O_CREAT|O_EXCL`` (no flock dependency). Holder writes PID for ops.
    """
    path = scale_lock_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        holder = ""
        try:
            holder = path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        raise TeamError(
            f"scale lock held for run {run_id}"
            + (f" (pid={holder})" if holder else "")
            + f"; refuse concurrent scale op ({path})"
        ) from exc
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        os.close(fd)
        fd = -1
        yield path
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            # py<3.8 missing_ok
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        except OSError:
            pass


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


def _ownership_tasks_from_manifest(
    root: Path, run_id: str
) -> list[dict[str, Any]]:
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
                raise TeamError(
                    f"failed to create scaled-in window for {tid!r}: {err}"
                )


def _kill_pane_recorded(
    rec: Mapping[str, Any],
    *,
    session: str,
    dry: bool,
    actions: list[str],
    errors: list[str],
    signalled: list[dict[str, Any]],
) -> None:
    """Kill only this pane's recorded pgid + its tmux window (not the session)."""
    tid = rec.get("task_id")
    widx = rec.get("window_index")
    pid = rec.get("pid")
    pgid = rec.get("pgid")

    # 1) killpg / kill only recorded targets — never pkill -f
    if pid is not None and not dry:
        target: int | None = None
        if isinstance(pgid, int) and pgid > 0:
            target = pgid
        elif isinstance(pid, int) and pid > 0:
            target = pid
        if target is not None:
            try:
                if os.name == "posix":
                    os.killpg(target, signal.SIGTERM)
                    actions.append(f"killpg:SIGTERM pgid={target} task={tid}")
                else:
                    os.kill(int(pid), signal.SIGTERM)
                    actions.append(f"kill:SIGTERM pid={pid} task={tid}")
                signalled.append({"task_id": tid, "pgid": target, "pid": pid})
            except (ProcessLookupError, PermissionError, OSError) as exc:
                errors.append(f"signal task={tid} target={target}: {exc}")
    elif dry:
        actions.append(f"dry_run: skipped kill for task={tid}")

    # 2) kill-window only (NOT kill-session)
    if session and not dry and widx is not None:
        try:
            if tmux_available():
                r = _tmux_run(
                    ["kill-window", "-t", f"{session}:{int(widx)}"]
                )
                actions.append(
                    f"tmux kill-window -t {session}:{widx} "
                    f"(exit {r.returncode})"
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
                    "owned_files": [
                        f".omg/team-scale/{tid}.md"
                    ],
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
            _add_tmux_windows(session=session, records=new_records)
            pane_pids = _list_pane_pids(session)
            for rec in new_records:
                widx = int(rec["window_index"])
                pid = pane_pids.get(widx)
                if pid is not None:
                    rec["pid"] = pid
                    rec["pgid"] = _pgid_for_pid(pid)
                    rec["status"] = STATUS_RUNNING
                else:
                    rec["status"] = "launched"
        except TeamError:
            raise
        except OSError as exc:
            raise TeamError(f"scale-up tmux launch failed: {exc}") from exc

    updated = dict(meta)
    updated["writer"] = CLI_WRITER
    updated["schema_version"] = int(meta.get("schema_version") or SCHEMA_VERSION)
    updated["tasks"] = list(tasks_all) + new_records
    updated["task_count"] = len(_active_tasks(updated["tasks"]))
    updated["next_worker_index"] = start_idx + n
    updated["last_scale_at"] = _utc_now()
    updated["last_scale"] = {
        "op": "add",
        "n": n,
        "window_indices": [r["window_index"] for r in new_records],
        "task_ids": [r["task_id"] for r in new_records],
        "dry_run": effective_dry,
    }
    # Never copy forged verified
    updated.pop("verified", None)
    updated.pop("passes", None)
    _atomic_write_json(team_meta_path(root, run_id), updated)

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

    active = [
        t
        for t in tasks_all
        if str(t.get("status") or "") != STATUS_SCALED_DOWN
    ]
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

    for v in victims:
        _kill_pane_recorded(
            v,
            session=session,
            dry=effective_dry,
            actions=actions,
            errors=errors,
            signalled=signalled,
        )
        wt = str(v.get("worktree") or "")
        if wt:
            preserved_worktrees.append(wt)

    # Mark scaled_down; PRESERVE worktrees (do not delete)
    now = _utc_now()
    for rec in tasks_all:
        if str(rec.get("task_id")) in victim_ids:
            rec["status"] = STATUS_SCALED_DOWN
            rec["scaled_down_at"] = now
            # Clear live handles; keep historical pid/pgid for audit
            rec["pid"] = None
            rec["pgid"] = None

    updated = dict(meta)
    updated["writer"] = CLI_WRITER
    updated["tasks"] = tasks_all
    updated["task_count"] = len(_active_tasks(tasks_all))
    updated["last_scale_at"] = now
    updated["last_scale"] = {
        "op": "remove",
        "n": n,
        "task_ids": sorted(victim_ids),
        "preserved_worktrees": preserved_worktrees,
        "actions": actions,
        "dry_run": effective_dry,
    }
    updated.pop("verified", None)
    updated.pop("passes", None)
    _atomic_write_json(team_meta_path(root, run_id), updated)

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
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    _assert_team_gates(env=env)
    rid = _resolve_run_id(root_path, run_id)
    meta = _require_team_run(root_path, rid)

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
            # dry-run skeleton: no live panes; leave as-is
            tasks_out.append(rec)
            continue

        if not probe_tmux:
            tasks_out.append(rec)
            continue

        win = _window_alive(session, widx)
        if win is True:
            new_st = STATUS_RUNNING
            alive = True
        elif win is False:
            # Dead window: unsealed work → needs-collect; stopped stays stopped
            if prev in ("stopped", STATUS_FAILED):
                new_st = prev
            else:
                new_st = STATUS_NEEDS_COLLECT
            alive = False
        else:
            # tmux unavailable — do not invent death
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

    updated = dict(meta)
    updated["writer"] = CLI_WRITER
    updated["tasks"] = tasks_out
    updated["task_count"] = len(_active_tasks(tasks_out))
    updated["resumed_at"] = _utc_now()
    updated["resume_changes"] = changed
    updated.pop("verified", None)
    updated.pop("passes", None)

    # Always rewrite CLI stamp (idempotent; even when changed==0 so resume
    # is recorded for operators). Pure-ish: only status reconciliation fields.
    _atomic_write_json(team_meta_path(root_path, rid), updated)

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


__all__ = [
    "SCALE_LOCK_NAME",
    "STATUS_NEEDS_COLLECT",
    "STATUS_SCALED_DOWN",
    "acquire_scale_lock",
    "resume_team",
    "scale_lock_path",
    "scale_team",
]
