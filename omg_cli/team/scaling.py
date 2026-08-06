"""Team plane lifecycle extensions (D4): dynamic scale + resume.

``omg team scale`` adds/removes panes on a RUNNING team under a file-based
scale lock and ``max_workers_cap()``. ``omg team resume`` reconciles
``team.json`` pane liveness after a leader restart/compaction.

HARD invariants (same as D1–D3):
- CLI single-writer (``writer=omg-cli``); never sets ``verified`` / ``passes``
- Gated by ``OMG_EXPERIMENTAL_TMUX_TEAM=1``; refuse nested worker context
- Bounded by ``max_workers_cap()``; dry-run touches no tmux/subprocess
- Scale-down kills **only** receipt-bound pane identities + recorded pgids —
  **no** self-matching ``pkill -f`` / ``pgrep -f``
- Scale-down preserves worktrees (post-mortem); never removes below 1 active
  pane unless the team is being stopped entirely
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import signal
import stat as stat_mod
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Sequence

from omg_cli.contracts.path_keys import ContractPathError
from omg_cli.evidence import CLI_WRITER
from omg_cli.fanout import max_workers_cap
from omg_cli.madmax import build_pane_command, tmux_available, tmux_env_args
from omg_cli.state import _run_dir, load_active_run, load_run, write_status
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    IDENTITY_RECEIPT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TEAM_WORKER_ENV,
    TeamError,
    TeamGateError,
    _grok_args_for_pane,
    _identity_rows,
    _list_pane_identities,
    _load_team_identity_chain,
    _pane_env_pairs,
    _pgid_for_pid,
    _pid_start_identity,
    _persist_team_identity_receipt,
    _read_tmux_launch_nonce_for_pane,
    _read_tmux_session_identity,
    _session_alive,
    _status_worker_alive,
    _tmux_launch_authority_matches,
    _task_role,
    _tmux_run,
    _utc_now,
    _window_alive,
    build_team_task_prompt,
    build_executor_pane_command,
    experimental_enabled,
    in_spawned_worker_context,
    load_team_meta,
    mutate_team_meta,
    team_dir,
    team_launch_receipt_path,
    team_meta_path,
    plugin_root,
    wrap_pane_with_worker_ready,
)
from omg_cli.team.providers import PROMPT_DELIVERY_PROMPT_FILE, build_executor_argv
from omg_cli.team.routing import ResolvedRouting, RoutingError, resolve_routing
from omg_cli.modes import build_grok_argv
from omg_cli.workers import (
    WorkerError,
    _norm_relpath,
    build_ownership_manifest,
    load_ownership_manifest,
    ownership_manifest_path,
    prepare_task,
    validate_task_worktree,
    validate_task_id,
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
_TMUX_WINDOW_ID = re.compile(r"^@[0-9]{1,16}$")
_TMUX_PANE_ID = re.compile(r"^%[0-9]{1,16}$")
_TMUX_CREATED_WINDOW_FORMAT = "#{window_index}\t#{window_id}\t#{pane_id}"
_TMUX_RECOVER_WINDOW_FORMAT = (
    "#{window_index}\t#{window_id}\t#{pane_id}\t#{window_name}\t"
    "#{session_name}\t#{session_id}\t#{@omg_launch_nonce}"
)
_TMUX_SCALED_PANE_FORMAT = "#{session_id}\t#{window_index}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{@omg_scale_nonce}\t#{pane_dead}"
_TMUX_RECORDED_PANE_FORMAT = (
    "#{session_name}\t#{session_id}\t#{window_index}\t#{pane_index}\t"
    "#{window_id}\t#{pane_id}\t#{pane_pid}\t#{@omg_scale_nonce}\t#{pane_dead}"
)
_TMUX_OWNED_WINDOW_FORMAT = (
    "#{window_id}\t#{pane_id}\t#{window_name}\t#{@omg_scale_nonce}"
)
_TMUX_SCALE_DISCOVERY_FORMAT = (
    "#{session_name}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{window_name}"
    "\t#{@omg_scale_nonce}\t#{@omg_launch_nonce}"
)
_TMUX_SCALE_NONCE_OPTION = "@omg_scale_nonce"
_TMUX_LAUNCH_NONCE_OPTION = "@omg_launch_nonce"
_TMUX_NONCE = re.compile(r"^[0-9a-f]{32}$")
_TMUX_WINDOW_ALLOCATION_RETRIES = 8
_SCALE_WAL_SCHEMA_VERSION = 1
_IDENTITY_WAL_MAX_BYTES = 1024 * 1024
_TMUX_RELAUNCH_TASK_OPTION = "@omg_relaunch_task_id"
_TMUX_RELAUNCH_NONCE_OPTION = "@omg_relaunch_nonce"
_TMUX_RELAUNCH_DISCOVERY_FORMAT = (
    "#{session_name}\t#{session_id}\t#{window_id}\t#{pane_id}\t"
    "#{@omg_launch_nonce}\t#{@omg_relaunch_task_id}\t"
    "#{@omg_relaunch_nonce}\t#{pane_dead}\t#{pane_start_command}"
)
_TMUX_RELAUNCH_TARGET_FORMAT = (
    "#{session_name}\t#{session_id}\t#{window_id}\t#{@omg_launch_nonce}"
)
_ScaleCommitOutcome = Literal["committed", "not_committed", "unknown"]
_SCALE_INTENT_RECORD_KEYS = frozenset(
    {
        "task_id",
        "window_index",
        "window_id",
        "window_nonce",
        "pane_id",
        "worktree",
        "argv_path",
        "pane_command",
        "argv",
        "role",
        "provider",
        "posture",
        "needs_pty",
        "prompt_delivery",
        "pid",
        "pgid",
        "pid_start",
        "status",
        "scaled_in_at",
    }
)
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
        raise TeamError("scale/stop lifecycle lock requires POSIX fcntl.flock") from exc

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
                f"scale lock open refused for run {run_id} (symlink or non-regular?): {exc}"
            ) from exc
        try:
            import stat as stat_mod

            st = os.fstat(fd)
            if not stat_mod.S_ISREG(st.st_mode) or st.st_nlink != 1:
                # Fail closed on hard links — never unlink/recreate under a
                # concurrent holder (that would split the lifecycle lock).
                raise TeamError(
                    f"scale lock must be a unique regular file for run {run_id} "
                    f"(nlink={st.st_nlink})"
                )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Re-check uniqueness under the lock before truncate.
            st = os.fstat(fd)
            if not stat_mod.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise TeamError(
                    f"scale lock inode must be a unique regular file "
                    f"for run {run_id} (nlink={st.st_nlink})"
                )
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
            f"omg team scale/resume disabled "
            f"(unset OMG_DISABLE_TMUX_TEAM; {EXPERIMENTAL_ENV}=0 disables). "
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


def _validate_scale_request_preflight(
    root: Path,
    run_id: str,
    *,
    active: Sequence[Mapping[str, Any]],
    task_specs: Sequence[Mapping[str, Any]],
) -> None:
    """Reject deterministic ownership/id conflicts before WAL publication."""
    active_ids = {str(row.get("task_id") or "") for row in active}
    new_specs = _canonical_scale_task_specs(task_specs)
    if any(str(spec["task_id"]) in active_ids for spec in new_specs):
        raise TeamError("scale-up task_id already active")
    try:
        manifest = load_ownership_manifest(root, run_id)
    except WorkerError:
        return
    rows = [row for row in manifest.get("tasks") or [] if isinstance(row, Mapping)]
    by_id = {str(row.get("task_id") or ""): row for row in rows}
    present = [str(spec["task_id"]) in by_id for spec in new_specs]
    if any(present) and not all(present):
        raise TeamError("partial scale-up ownership state conflicts with request")
    file_owners: dict[str, str] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        for relative in row.get("owned_files") or []:
            if isinstance(relative, str):
                file_owners[_norm_relpath(relative)] = task_id
    for spec in new_specs:
        task_id = str(spec["task_id"])
        for relative in spec["owned_files"]:
            owner = file_owners.get(relative)
            if owner is not None and owner != task_id and not spec.get("coordination"):
                raise TeamError(
                    f"shared-file collision on {relative!r} between {owner!r} and {task_id!r}"
                )
        if task_id in by_id:
            existing = _canonical_scale_task_specs([by_id[task_id]])[0]
            if existing != spec:
                raise TeamError(f"scale-up ownership differs task={task_id}")


def _ensure_scale_ownership_manifest(
    root: Path,
    run_id: str,
    *,
    existing_tasks: Sequence[Mapping[str, Any]],
    task_specs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Reuse an exact prepared manifest; create the expanded shape only once."""
    new_ids = {str(spec["task_id"]) for spec in task_specs}
    try:
        current = load_ownership_manifest(root, run_id)
    except WorkerError:
        current = None
    if current is not None:
        rows = [row for row in current.get("tasks") or [] if isinstance(row, Mapping)]
        current_ids = {str(row.get("task_id") or "") for row in rows}
        overlap = current_ids & new_ids
        if overlap:
            if overlap != new_ids:
                raise TeamError("partial scale-up ownership state conflicts with request")
            _validate_scale_request_preflight(
                root, run_id, active=(), task_specs=task_specs
            )
            return current
    try:
        return build_ownership_manifest(
            root, run_id, [*existing_tasks, *task_specs]
        )
    except WorkerError as exc:
        raise TeamError(str(exc)) from exc


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
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        atomic_write_bytes,
        read_managed_regular_bytes,
    )

    tid = str(task["task_id"])
    owned = list(task.get("owned_files") or [])
    wt = worktree_dir(root, run_id, tid)
    role = _task_role(task)
    tdir = team_dir(root, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    artifact_paths: list[Path] = []

    prompt = build_team_task_prompt(
        goal,
        run_id=run_id,
        task_id=tid,
        task_index=task_index,
        task_count=task_count,
        owned_files=owned,
        worktree=wt,
        **(
            {
                "provider": resolved.for_role(role).provider,
                "role": resolved.for_role(role).role,
                "posture": resolved.for_role(role).posture,
            }
            if multi_cli and resolved is not None
            else {}
        ),
    )
    prompt_dir = wt / ".omg" / "team-prompt"
    prompt_path = prompt_dir / f"{tid}.prompt.md"

    if multi_cli and resolved is not None:
        route = resolved.for_role(role)
        artifact_paths.append(prompt_path)
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
        from omg_cli.team.plane import wrap_pane_with_worker_ready

        pane_cmd = wrap_pane_with_worker_ready(
            build_executor_pane_command(
                argv,
                needs_pty=needs_pty,
                prompt_delivery=prompt_delivery,
                prompt_file=prompt_path,
            )
        )
    else:
        from omg_cli.team.plane import wrap_pane_with_worker_ready

        argv = build_grok_argv(
            mode="ulw",
            goal=goal,
            yolo=yolo,
            cwd=wt,
            safe=safe,
            extra=extra,
            run_id=run_id,
            skill_root=plugin_root(),
            prompt=prompt,
            disallow_shell=False,
        )
        last_prompt_path = prompt_dir / "last_prompt.md"
        try:
            prompt_index = argv.index("-p")
        except ValueError:
            prompt_index = -1
        if prompt_index < 0 or prompt_index + 1 >= len(argv):
            raise TeamError(f"scale-up grok prompt argv mismatch task={tid}")
        argv[prompt_index : prompt_index + 2] = [
            "--prompt-file",
            str(last_prompt_path),
        ]
        needs_pty = False
        provider = "grok"
        posture = "read-write"
        prompt_delivery = PROMPT_DELIVERY_PROMPT_FILE
        pane_cmd = wrap_pane_with_worker_ready(
            build_pane_command(_grok_args_for_pane(argv))
        )
        artifact_paths.extend([last_prompt_path, prompt_path])

    argv_path = tdir / f"{tid}.argv.json"
    expected_bodies = {
        prompt_path: prompt.encode("utf-8"),
        argv_path: (json.dumps(argv, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        ),
    }
    if not multi_cli:
        expected_bodies[last_prompt_path] = prompt.encode("utf-8")
    for path, expected in expected_bodies.items():
        try:
            atomic_write_bytes(path, expected, mode=DATA_FILE_MODE, replace=False)
        except FileExistsError:
            try:
                actual = read_managed_regular_bytes(path)
            except (OSError, ValueError, ContractPathError) as exc:
                raise TeamError(
                    f"scale-up preparation unreadable task={tid}"
                ) from exc
            if actual != expected:
                raise TeamError(f"scale-up preparation differs task={tid}")
        except (OSError, ValueError, ContractPathError) as exc:
            raise TeamError(
                f"scale-up preparation materialization failed task={tid}"
            ) from exc
    artifact_paths.append(argv_path)
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
        "_artifact_paths": [str(path.relative_to(root)) for path in artifact_paths],
    }


def _reuse_prepared_pane_record(
    *,
    root: Path,
    run_id: str,
    goal: str,
    task: Mapping[str, Any],
    task_index: int,
    task_count: int,
    window_index: int,
    multi_cli: bool,
    resolved: ResolvedRouting | None,
    yolo: bool,
    safe: bool,
    extra: Sequence[str] | None,
) -> dict[str, Any] | None:
    """Strictly reuse a complete deterministic prompt/argv preparation set."""
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        atomic_write_bytes,
        read_managed_regular_bytes,
    )

    task_id = str(task["task_id"])
    owned = list(task.get("owned_files") or [])
    role = _task_role(task)
    worktree = worktree_dir(root, run_id, task_id)
    argv_path = team_dir(root, run_id) / f"{task_id}.argv.json"
    prompt_dir = worktree / ".omg" / "team-prompt"
    task_prompt_path = prompt_dir / f"{task_id}.prompt.md"
    last_prompt_path = prompt_dir / "last_prompt.md"
    candidates = [argv_path, task_prompt_path]
    if not multi_cli:
        candidates.append(last_prompt_path)

    if multi_cli:
        if resolved is None:
            raise TeamError(f"scale-up routing missing task={task_id}")
        route = resolved.for_role(role)
        provider = route.provider
        posture = route.posture
        prompt = build_team_task_prompt(
            goal,
            run_id=run_id,
            task_id=task_id,
            task_index=task_index,
            task_count=task_count,
            owned_files=owned,
            worktree=worktree,
            provider=route.provider,
            role=route.role,
            posture=route.posture,
        )
        inv = build_executor_argv(
            route.provider,
            route.role,
            prompt_file=task_prompt_path,
            model=route.model,
            cwd=worktree,
            check_binary=False,
        )
        expected_argv = list(inv.argv)
        needs_pty = bool(inv.needs_pty)
        prompt_delivery = inv.prompt_delivery
        pane_cmd = build_executor_pane_command(
            expected_argv,
            needs_pty=needs_pty,
            prompt_delivery=prompt_delivery,
            prompt_file=task_prompt_path,
        )
    else:
        provider = "grok"
        posture = "read-write"
        needs_pty = False
        prompt_delivery = PROMPT_DELIVERY_PROMPT_FILE
        prompt = build_team_task_prompt(
            goal,
            run_id=run_id,
            task_id=task_id,
            task_index=task_index,
            task_count=task_count,
            owned_files=owned,
            worktree=worktree,
        )
        expected_argv = build_grok_argv(
            mode="ulw",
            goal=goal,
            yolo=yolo,
            cwd=worktree,
            safe=safe,
            extra=extra,
            run_id=run_id,
            skill_root=plugin_root(),
            prompt=prompt,
            disallow_shell=False,
        )
        try:
            prompt_index = expected_argv.index("-p")
        except ValueError:
            prompt_index = -1
        if prompt_index < 0 or prompt_index + 1 >= len(expected_argv):
            raise TeamError(f"scale-up grok prompt argv mismatch task={task_id}")
        expected_argv[prompt_index : prompt_index + 2] = [
            "--prompt-file",
            str(last_prompt_path),
        ]
        pane_cmd = wrap_pane_with_worker_ready(
            build_pane_command(_grok_args_for_pane(expected_argv))
        )

    expected_bodies = {
        argv_path: (
            json.dumps(expected_argv, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8"),
        task_prompt_path: prompt.encode("utf-8"),
    }
    if not multi_cli:
        expected_bodies[last_prompt_path] = prompt.encode("utf-8")

    observed: dict[Path, bytes] = {}
    missing: list[Path] = []
    for path in candidates:
        try:
            observed[path] = read_managed_regular_bytes(path)
        except FileNotFoundError:
            missing.append(path)
        except (OSError, ValueError, ContractPathError) as exc:
            raise TeamError(f"scale-up preparation unreadable task={task_id}") from exc
    if len(missing) == len(candidates):
        return None
    for path, body in observed.items():
        if body != expected_bodies[path]:
            raise TeamError(f"scale-up preparation differs task={task_id}")
    for path in missing:
        expected = expected_bodies[path]
        try:
            atomic_write_bytes(
                path,
                expected,
                mode=DATA_FILE_MODE,
                replace=False,
            )
        except FileExistsError:
            try:
                actual = read_managed_regular_bytes(path)
            except (OSError, ValueError, ContractPathError) as exc:
                raise TeamError(
                    f"scale-up preparation unreadable task={task_id}"
                ) from exc
            if actual != expected:
                raise TeamError(f"scale-up preparation differs task={task_id}")
        except (OSError, ValueError, ContractPathError) as exc:
            raise TeamError(
                f"scale-up preparation materialization failed task={task_id}"
            ) from exc
    artifact_paths = [argv_path, task_prompt_path]
    if not multi_cli:
        artifact_paths.append(last_prompt_path)
    return {
        "task_id": task_id,
        "window_index": window_index,
        "worktree": str(worktree),
        "argv_path": str(argv_path.relative_to(_run_dir(root, run_id))),
        "pane_command": wrap_pane_with_worker_ready(pane_cmd)
        if multi_cli
        else pane_cmd,
        "argv": expected_argv,
        "role": role,
        "provider": provider,
        "posture": posture,
        "needs_pty": needs_pty,
        "prompt_delivery": prompt_delivery,
        "pid": None,
        "pgid": None,
        "pid_start": None,
        "status": "pending",
        "scaled_in_at": _utc_now(),
        "_artifact_paths": [str(path.relative_to(root)) for path in artifact_paths],
    }


def _canonical_scale_task_specs(
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize every request field that can change a scaled worker."""
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in tasks:
        try:
            task_id = validate_task_id(str(raw.get("task_id") or raw.get("id") or ""))
        except WorkerError as exc:
            raise TeamError(str(exc)) from exc
        if task_id in seen:
            raise TeamError(f"duplicate task_id: {task_id}")
        seen.add(task_id)
        owned_raw = raw.get("owned_files") or raw.get("files") or []
        if not isinstance(owned_raw, list) or not all(
            isinstance(item, str) for item in owned_raw
        ):
            raise TeamError(f"task {task_id}: owned_files must be a string list")
        owned = [_norm_relpath(item) for item in owned_raw if item.strip()]
        if not owned:
            raise TeamError(f"task {task_id}: owned_files must be non-empty")
        capability_mode = str(raw.get("capability_mode") or "read-write").strip()
        if capability_mode not in {"read-only", "read-write"}:
            raise TeamError(f"task {task_id}: bad capability_mode {capability_mode!r}")
        coordination = str(raw.get("coordination") or "").strip() or None
        normalized.append(
            {
                "task_id": task_id,
                "owned_files": owned,
                "role": _task_role(raw),
                "capability_mode": capability_mode,
                "coordination": coordination,
            }
        )
    return normalized


def _scale_request_payload(
    *,
    meta: Mapping[str, Any],
    active: Sequence[Mapping[str, Any]],
    task_specs: Sequence[Mapping[str, Any]],
    start_index: int,
    yolo: bool,
    safe: bool,
    extra: Sequence[str] | None,
) -> dict[str, Any]:
    """Return the complete deterministic caller intent bound by the WAL."""
    from omg_cli.contracts.writer_chain import sha256_hex

    extra_args = list(extra or [])
    if not all(isinstance(item, str) for item in extra_args):
        raise TeamError("scale extra arguments must be strings")
    goal = str(meta.get("goal") or "(no goal)")
    return {
        "operation": "add",
        "base_identity_generation": int(meta.get("identity_generation", 0)),
        "base_receipt_sha256": str(
            meta.get("identity_receipt_sha256")
            or meta.get("launch_receipt_sha256")
            or ""
        ),
        "base_active_identities": _identity_rows(active),
        "start_worker_index": start_index,
        "task_count_after": len(active) + len(task_specs),
        "tasks": _canonical_scale_task_specs(task_specs),
        "yolo": bool(yolo),
        "safe": bool(safe),
        "extra": extra_args,
        "goal_sha256": sha256_hex(goal.encode("utf-8")),
        "multi_cli": bool(meta.get("multi_cli")),
        "routing": meta.get("routing"),
        "topology": meta.get("topology"),
        "team_id": meta.get("team_id"),
        "executor": meta.get("executor"),
    }


def _scale_request_sha256(
    *,
    meta: Mapping[str, Any],
    active: Sequence[Mapping[str, Any]],
    task_specs: Sequence[Mapping[str, Any]],
    start_index: int,
    yolo: bool,
    safe: bool,
    extra: Sequence[str] | None,
) -> str:
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    request = _scale_request_payload(
        meta=meta,
        active=active,
        task_specs=task_specs,
        start_index=start_index,
        yolo=yolo,
        safe=safe,
        extra=extra,
    )
    return sha256_hex(canonical_json_bytes(request))


def _scale_wal_path(root: Path, run_id: str, generation: int) -> Path:
    if generation <= 0:
        raise TeamError("scale WAL generation must be positive")
    return team_dir(root, run_id) / "scale-wal" / f"{generation}.json"


def _read_identity_wal_bytes(path: Path) -> bytes:
    """Descriptor-confined, bounded read for an immutable identity WAL."""
    from omg_cli.contracts.path_keys import open_existing_managed_dir_fd

    parent_fd = open_existing_managed_dir_fd(path.parent)
    fd = -1
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(fd)
        if (
            not stat_mod.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _IDENTITY_WAL_MAX_BYTES
        ):
            raise TeamError("identity WAL must be a bounded unique regular file")
        chunks: list[bytes] = []
        remaining = _IDENTITY_WAL_MAX_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        after = os.fstat(fd)
        if len(body) > _IDENTITY_WAL_MAX_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise TeamError("identity WAL changed during bounded read")
        return body
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def pending_identity_wal_operation(
    root: Path | str, run_id: str, meta: Mapping[str, Any]
) -> str | None:
    """Return the authenticated next-generation WAL operation, if present."""
    from omg_cli.contracts.writer_chain import parse_canonical_json_bytes

    root_path = Path(root).resolve()
    generation = int(meta.get("identity_generation", 0)) + 1
    path = _scale_wal_path(root_path, run_id, generation)
    try:
        body = _read_identity_wal_bytes(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, ContractPathError) as exc:
        raise TeamError("pending identity WAL is unreadable; lifecycle refused") from exc
    try:
        wal = parse_canonical_json_bytes(body)
    except ValueError as exc:
        raise TeamError("pending identity WAL is not strict canonical JSON") from exc
    if not isinstance(wal, Mapping):
        raise TeamError("pending identity WAL must be an object")
    kind = wal.get("store_kind")
    contract = wal.get("writer_contract")
    operation = (
        "add"
        if (kind, contract) == ("team_scale_wal", "scale-wal-v1")
        else "relaunch"
        if (kind, contract) == ("team_relaunch_wal", "relaunch-wal-v1")
        else None
    )
    common_keys = {
        "store_kind",
        "schema_version",
        "writer_contract",
        "writer",
        "run_id",
        "session_name",
        "session_id",
        "launch_nonce",
        "generation",
        "base_identity_generation",
        "base_receipt_sha256",
        "request",
        "request_sha256",
        "tasks",
    }
    expected_keys = common_keys | ({"target_window_id"} if operation == "relaunch" else set())
    base_receipt = str(
        meta.get("identity_receipt_sha256")
        or meta.get("launch_receipt_sha256")
        or ""
    )
    if (
        operation is None
        or set(wal) != expected_keys
        or wal.get("schema_version") != _SCALE_WAL_SCHEMA_VERSION
        or wal.get("writer") != CLI_WRITER
        or wal.get("run_id") != run_id
        or wal.get("generation") != generation
        or wal.get("base_identity_generation")
        != int(meta.get("identity_generation", 0))
        or wal.get("base_receipt_sha256") != base_receipt
        or not isinstance(wal.get("session_name"), str)
        or not wal.get("session_name")
        or not isinstance(wal.get("session_id"), str)
        or not wal.get("session_id")
        or not isinstance(wal.get("launch_nonce"), str)
        or _TMUX_NONCE.fullmatch(str(wal.get("launch_nonce"))) is None
        or not isinstance(wal.get("request"), Mapping)
        or not isinstance(wal.get("request_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(wal.get("request_sha256"))) is None
        or not isinstance(wal.get("tasks"), list)
        or (
            operation == "relaunch"
            and _TMUX_WINDOW_ID.fullmatch(str(wal.get("target_window_id") or ""))
            is None
        )
    ):
        raise TeamError("pending identity WAL authority/base is invalid")
    return operation


def _uncommitted_scale_wal_exists(
    root: Path,
    run_id: str,
    generation: int,
) -> bool:
    """Read-probe the pending WAL without interpreting mutable policy."""
    try:
        _read_identity_wal_bytes(_scale_wal_path(root, run_id, generation))
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise TeamError("pending scale WAL is unreadable; lifecycle refused") from exc
    return True


def _assert_no_uncommitted_scale_wal(
    root: Path, run_id: str, meta: Mapping[str, Any], *, operation: str
) -> None:
    """Prevent another lifecycle operation from taking an identity generation."""
    generation = int(meta.get("identity_generation", 0)) + 1
    pending = pending_identity_wal_operation(root, run_id, meta)
    if pending is not None:
        label = "scale-up" if pending == "add" else "relaunch"
        raise TeamError(
            f"{operation} refused while {label} WAL generation {generation} is pending; "
            f"retry the original {pending} request"
        )
    receipts = _load_future_identity_receipts(
        root,
        run_id,
        committed_generation=generation - 1,
    )
    if receipts:
        if (
            operation == "scale-down"
            and set(receipts) == {generation}
            and receipts[generation][0].get("operation") == "remove"
        ):
            # The scale-down body authenticates its exact receipt intent and
            # process identities before resuming the original transaction.
            return
        raise TeamError(
            f"{operation} refused while identity receipt generation(s) "
            f"{sorted(receipts)} are pending; retry the original identity operation"
        )


def _load_or_publish_scale_wal(
    root: Path,
    run_id: str,
    *,
    generation: int,
    meta: Mapping[str, Any],
    authority: Mapping[str, Any],
    request: Mapping[str, Any],
    request_sha256: str,
    task_specs: Sequence[Mapping[str, Any]],
    start_index: int,
    allow_create: bool = True,
) -> tuple[dict[str, Any], str]:
    """Create once, or strictly adopt, the immutable pre-side-effect scale WAL."""
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        atomic_write_bytes,
        fsync_existing_managed_dir,
        read_managed_regular_bytes,
    )
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    session = str(meta.get("session") or "")
    session_id = authority.get("session_id")
    launch_nonce = authority.get("launch_nonce")
    base_receipt = str(
        meta.get("identity_receipt_sha256")
        or meta.get("launch_receipt_sha256")
        or ""
    )
    if (
        not session
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(launch_nonce, str)
        or _TMUX_NONCE.fullmatch(launch_nonce) is None
        or re.fullmatch(r"[0-9a-f]{64}", base_receipt) is None
        or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None
    ):
        raise TeamError("scale WAL authority is invalid")
    deterministic = {
        "store_kind": "team_scale_wal",
        "schema_version": _SCALE_WAL_SCHEMA_VERSION,
        "writer_contract": "scale-wal-v1",
        "writer": CLI_WRITER,
        "run_id": run_id,
        "session_name": session,
        "session_id": session_id,
        "launch_nonce": launch_nonce,
        "generation": generation,
        "base_identity_generation": int(meta.get("identity_generation", 0)),
        "base_receipt_sha256": base_receipt,
        "request": dict(request),
        "request_sha256": request_sha256,
    }
    path = _scale_wal_path(root, run_id, generation)
    expected_keys = set(deterministic) | {"tasks"}

    def parse_existing(body: bytes) -> tuple[dict[str, Any], str]:
        try:
            parsed = parse_canonical_json_bytes(body)
        except ValueError as exc:
            raise TeamError("scale WAL is not strict canonical JSON") from exc
        if (
            not isinstance(parsed, dict)
            or set(parsed) != expected_keys
            or any(parsed.get(key) != value for key, value in deterministic.items())
        ):
            raise TeamError(
                "pending scale-up retry intent differs from WAL request/session/base"
            )
        rows = parsed.get("tasks")
        if not isinstance(rows, list) or len(rows) != len(task_specs):
            raise TeamError("scale WAL task plan mismatch")
        for offset, (spec, row) in enumerate(zip(task_specs, rows, strict=True)):
            if (
                not isinstance(row, Mapping)
                or set(row)
                != {
                    "task_id",
                    "planned_window_index",
                    "window_nonce",
                    "launch_name",
                    "scaled_in_at",
                }
                or row.get("task_id") != spec.get("task_id")
                or row.get("planned_window_index") != start_index + offset
                or not isinstance(row.get("window_nonce"), str)
                or _TMUX_NONCE.fullmatch(str(row.get("window_nonce"))) is None
                or row.get("launch_name")
                != f"{spec.get('task_id')}-{row.get('window_nonce')}"
                or not isinstance(row.get("scaled_in_at"), str)
                or not row.get("scaled_in_at")
            ):
                raise TeamError("scale WAL task plan mismatch")
        return parsed, sha256_hex(body)

    try:
        existing = read_managed_regular_bytes(path)
    except FileNotFoundError:
        if not allow_create:
            raise TeamError("receipt-bound scale WAL is missing")
        wal = {
            **deterministic,
            "tasks": [
                {
                    "task_id": str(spec["task_id"]),
                    "planned_window_index": start_index + offset,
                    "window_nonce": (nonce := secrets.token_hex(16)),
                    "launch_name": f"{spec['task_id']}-{nonce}",
                    "scaled_in_at": _utc_now(),
                }
                for offset, spec in enumerate(task_specs)
            ],
        }
        body = canonical_json_bytes(wal)
        try:
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
        except FileExistsError:
            try:
                return parse_existing(read_managed_regular_bytes(path))
            except (OSError, ValueError) as exc:
                raise TeamError("scale WAL publication conflict") from exc
        except OSError as exc:
            try:
                published = read_managed_regular_bytes(path)
            except (OSError, ValueError):
                raise exc
            if published != body:
                raise TeamError("scale WAL publication outcome is ambiguous") from exc
            fsync_existing_managed_dir(path.parent)
        return wal, sha256_hex(body)
    except (OSError, ValueError) as exc:
        raise TeamError("scale WAL is unreadable") from exc
    return parse_existing(existing)


def _apply_scale_wal_plan(
    records: Sequence[dict[str, Any]], wal: Mapping[str, Any]
) -> None:
    rows = wal.get("tasks")
    if not isinstance(rows, list) or len(rows) != len(records):
        raise TeamError("scale WAL record plan mismatch")
    for record, raw in zip(records, rows, strict=True):
        if not isinstance(raw, Mapping) or raw.get("task_id") != record.get("task_id"):
            raise TeamError("scale WAL record plan mismatch")
        record["window_index"] = int(raw["planned_window_index"])
        record["window_nonce"] = str(raw["window_nonce"])
        record["_launch_name"] = str(raw["launch_name"])
        record["_planned_window_index"] = int(raw["planned_window_index"])
        record["scaled_in_at"] = str(raw["scaled_in_at"])


def _public_scale_record(record: Mapping[str, Any]) -> dict[str, Any]:
    public = {key: value for key, value in record.items() if not key.startswith("_")}
    if set(public) != _SCALE_INTENT_RECORD_KEYS:
        raise TeamError("scale-up intent record keys mismatch")
    return public


def _scale_artifact_rows(
    root: Path,
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    from omg_cli.contracts.path_keys import read_managed_regular_bytes
    from omg_cli.contracts.writer_chain import sha256_hex

    relative_paths: set[str] = set()
    # Ownership is common to the transaction; pane builders contribute argv
    # and prompt artifacts through private, never-persisted bookkeeping.
    for record in records:
        for raw in record.get("_artifact_paths") or []:
            if not isinstance(raw, str):
                raise TeamError("scale-up artifact path must be a string")
            relative_paths.add(raw)
    rows: list[dict[str, str]] = []
    for relative in sorted(relative_paths):
        path = root / relative
        try:
            body = read_managed_regular_bytes(path)
        except (OSError, ValueError) as exc:
            raise TeamError(f"scale-up artifact unreadable: {relative}") from exc
        rows.append({"path": relative, "sha256": sha256_hex(body)})
    return rows


def _build_scale_intent(
    root: Path,
    run_id: str,
    *,
    request_sha256: str,
    scale_wal_sha256: str,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    from omg_cli.contracts.path_keys import read_managed_regular_bytes
    from omg_cli.contracts.writer_chain import sha256_hex

    artifacts = _scale_artifact_rows(root, records)
    ownership = ownership_manifest_path(root, run_id)
    try:
        ownership_body = read_managed_regular_bytes(ownership)
    except (OSError, ValueError) as exc:
        raise TeamError("scale-up ownership manifest unreadable") from exc
    artifacts.append(
        {
            "path": str(ownership.relative_to(root)),
            "sha256": sha256_hex(ownership_body),
        }
    )
    artifacts.sort(key=lambda row: row["path"])
    return {
        "request_sha256": request_sha256,
        "scale_wal_sha256": scale_wal_sha256,
        "records": [_public_scale_record(record) for record in records],
        "artifacts": artifacts,
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
    records: Sequence[dict[str, Any]],
    session_owned: bool = True,
    window_id: str | None = None,
) -> None:
    """Adopt an exact WAL-planned orphan, or launch it exactly once.

    ``session_owned=False`` (inside Teams) never trusts the shared session's
    ``@omg_launch_nonce`` — it does not exist there by design, since two
    inside Teams in one tmux session would otherwise clobber each other's
    stamp. Authority instead gates on the Team's own ``window_id``.
    """
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
    team_window_id = (
        window_id
        if isinstance(window_id, str) and _TMUX_WINDOW_ID.fullmatch(window_id)
        else None
    )
    if not session_owned and window_id is not None and team_window_id is None:
        raise TeamError("scale-up requires a valid team window id for inside Teams")
    created_records: list[dict[str, Any]] = []
    allocation_floor = 0
    session_id = (
        str(records[0].get("_session_id"))
        if records and records[0].get("_session_id")
        else None
    )
    launch_nonce = (
        str(records[0].get("_launch_nonce"))
        if records and records[0].get("_launch_nonce")
        else None
    )
    if session_id is not None and launch_nonce is not None:
        if (
            _read_tmux_session_identity(session) != (session, session_id)
            or not _tmux_launch_authority_matches(
                session,
                expected_nonce=launch_nonce,
                session_owned=session_owned,
                window_id=team_window_id,
            )
        ):
            raise TeamError("scale-up tmux session authority changed before launch")
        for rec in records:
            if rec.get("_session_name") not in {None, session}:
                raise TeamError("scale-up record session authority mismatch")
            rec["_session_name"] = session

    def discover(rec: Mapping[str, Any]) -> dict[str, str] | None:
        task_id = str(rec["task_id"])
        nonce = str(rec.get("window_nonce") or "")
        launch_name = str(rec.get("_launch_name") or "")
        if _TMUX_NONCE.fullmatch(nonce) is None or launch_name != f"{task_id}-{nonce}":
            raise TeamError(f"missing scale WAL tmux plan for {task_id!r}")
        listed = _tmux_run(
            ["list-windows", "-t", session, "-F", _TMUX_SCALE_DISCOVERY_FORMAT]
        )
        if listed.returncode != 0:
            err = (listed.stderr or listed.stdout or "").strip()[:400]
            raise TeamError(f"failed to enumerate scaled windows for {task_id!r}: {err}")
        conflicts: list[dict[str, str]] = []
        for line in (listed.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 7:
                raise TeamError(f"invalid tmux scaled-window enumeration for {task_id!r}")
            (
                row_session,
                row_session_id,
                window_id,
                pane_id,
                name,
                marker,
                row_launch_nonce,
            ) = parts
            if (
                row_session != session
                or
                _TMUX_WINDOW_ID.fullmatch(window_id) is None
                or _TMUX_PANE_ID.fullmatch(pane_id) is None
                or (marker and _TMUX_NONCE.fullmatch(marker) is None)
                or (session_id is not None and row_session_id != session_id)
                or (launch_nonce is not None and row_launch_nonce != launch_nonce)
            ):
                raise TeamError(f"invalid tmux scaled-window identity for {task_id!r}")
            if name in {launch_name, task_id} or marker == nonce:
                conflicts.append(
                    {
                        "window_id": window_id,
                        "pane_id": pane_id,
                        "window_name": name,
                        "window_nonce": marker,
                    }
                )
        exact = [
            row
            for row in conflicts
            if (
                row["window_name"] == launch_name
                and row["window_nonce"] in {"", nonce}
            )
            or (
                row["window_name"] == task_id
                and row["window_nonce"] == nonce
            )
        ]
        if len(conflicts) > 1 or len(exact) > 1:
            raise TeamError(f"ambiguous tmux orphan candidates for {task_id!r}")
        if conflicts and not exact:
            raise TeamError(f"conflicting tmux orphan candidate for {task_id!r}")
        return exact[0] if exact else None

    def bind_and_rename(
        rec: dict[str, Any], candidate: Mapping[str, str]
    ) -> None:
        task_id = str(rec["task_id"])
        nonce = str(rec["window_nonce"])
        launch_name = str(rec["_launch_name"])
        window_id = str(candidate["window_id"])
        pane_id = str(candidate["pane_id"])
        current_name = str(candidate["window_name"])
        current_nonce = str(candidate["window_nonce"])
        if current_name == task_id and current_nonce == nonce:
            rec["window_id"] = window_id
            rec["pane_id"] = pane_id
            return
        if current_name != launch_name or current_nonce not in {"", nonce}:
            raise TeamError(f"conflicting tmux orphan candidate for {task_id!r}")
        predicate = _and_tmux_formats(
            [
                f"#{{==:#{{window_id}},{window_id}}}",
                f"#{{==:#{{pane_id}},{pane_id}}}",
                f"#{{==:#{{window_name}},{launch_name}}}",
                f"#{{==:#{{{_TMUX_SCALE_NONCE_OPTION}}},{current_nonce}}}",
                *(
                    [f"#{{==:#{{{_TMUX_LAUNCH_NONCE_OPTION}}},{launch_nonce}}}"]
                    if launch_nonce is not None
                    else []
                ),
            ]
        )
        command_parts = [
            f"set-window-option -t {window_id} {_TMUX_SCALE_NONCE_OPTION} {nonce}",
            f"rename-window -t {window_id} {task_id}",
        ]
        if launch_nonce is not None:
            # ``team status`` reads a strict pane-scoped nonce with no session
            # fallback (see ``_status_worker_alive``); a scaled-in pane/window
            # that only carries ``@omg_scale_nonce`` is judged alive=False even
            # when the worker is running. Stamp both scopes atomically with
            # the rename/scale-nonce bind so there is no window where the pane
            # exists without its launch-nonce authority.
            command_parts.append(
                f"set-option -p -t {pane_id} {_TMUX_LAUNCH_NONCE_OPTION} {launch_nonce}"
            )
            command_parts.append(
                f"set-option -w -t {window_id} {_TMUX_LAUNCH_NONCE_OPTION} {launch_nonce}"
            )
        command = " ; ".join(command_parts)
        bound = _tmux_run(
            ["if-shell", "-F", "-t", window_id, predicate, command, ""]
        )
        if bound.returncode != 0:
            err = (bound.stderr or bound.stdout or "").strip()[:400]
            raise TeamError(f"failed to bind scaled window ownership for {task_id!r}: {err}")
        readback = _tmux_run(
            ["display-message", "-p", "-t", window_id, "-F", _TMUX_OWNED_WINDOW_FORMAT]
        )
        expected = f"{window_id}\t{pane_id}\t{task_id}\t{nonce}"
        if readback.returncode != 0 or (readback.stdout or "").splitlines() != [expected]:
            raise TeamError(f"scaled window ownership readback failed for {task_id!r}")
        if launch_nonce is not None:
            # Read back through the exact pane-scoped, no-fallback mechanism
            # ``team status`` itself uses, so a bind that silently missed the
            # pane option (window-only) is caught here, not in status later.
            live_pane_nonce = _read_tmux_launch_nonce_for_pane(
                pane_id, session, allow_session_fallback=False
            )
            if live_pane_nonce != launch_nonce:
                raise TeamError(
                    f"scaled pane launch-nonce readback failed for {task_id!r}"
                )
        rec["window_id"] = window_id
        rec["pane_id"] = pane_id

    try:
        for rec in records:
            tid = str(rec["task_id"])
            planned_index = int(rec.get("_planned_window_index", rec["window_index"]))
            target_index = max(planned_index, allocation_floor)
            wt = str(rec["worktree"])
            pane_cmd = str(rec["pane_command"])
            task_env_args = tmux_env_args(list(rec.get("_env_pairs") or []))
            window_nonce = str(rec.get("window_nonce") or "")
            launch_name = str(rec.get("_launch_name") or "")
            # Backward-compatible unit/helper surface; the production path
            # always supplies the authenticated WAL plan plus session id.
            if session_id is None and not window_nonce and not launch_name:
                window_nonce = secrets.token_hex(16)
                launch_name = f"{tid}-{window_nonce}"
                rec["window_nonce"] = window_nonce
                rec["_launch_name"] = launch_name
            if _TMUX_NONCE.fullmatch(window_nonce) is None or launch_name != f"{tid}-{window_nonce}":
                raise TeamError(f"missing scale WAL tmux plan for {tid!r}")
            phase = "primary"

            orphan = discover(rec) if session_id is not None else None
            if orphan is not None:
                bind_and_rename(rec, orphan)
                adopted_index = _tmux_run(
                    ["display-message", "-p", "-t", str(rec["window_id"]), "#{window_index}"]
                )
                lines = (adopted_index.stdout or "").splitlines()
                if adopted_index.returncode != 0 or len(lines) != 1:
                    raise TeamError(f"failed to read adopted window index for {tid!r}")
                try:
                    actual_index = int(lines[0])
                except ValueError as exc:
                    raise TeamError(f"invalid adopted window index for {tid!r}") from exc
                if actual_index < planned_index:
                    raise TeamError(f"adopted window index precedes WAL plan for {tid!r}")
                rec["window_index"] = actual_index
                created_records.append(rec)
                allocation_floor = actual_index + 1
                continue

            for attempt in range(_TMUX_WINDOW_ALLOCATION_RETRIES + 1):
                target = f"{session}:{target_index}"
                new_window_args = [
                    "new-window",
                    "-d",
                    "-P",
                    "-F",
                    _TMUX_CREATED_WINDOW_FORMAT,
                    "-t",
                    target,
                    "-n",
                    launch_name,
                    "-c",
                    wt,
                    *task_env_args,
                    pane_cmd,
                ]
                if session_id is not None and launch_nonce is not None:
                    if session_owned:
                        # Owned detached session: the session-level stamp is
                        # this Team's own authority.
                        authority_target = session
                        create_predicate = _and_tmux_formats(
                            [
                                f"#{{==:#{{session_name}},{session}}}",
                                f"#{{==:#{{session_id}},{session_id}}}",
                                f"#{{==:#{{{_TMUX_LAUNCH_NONCE_OPTION}}},{launch_nonce}}}",
                            ]
                        )
                    else:
                        # Inside mode: the shared session never carries this
                        # Team's ``@omg_launch_nonce`` (a second inside Team
                        # in the same session would otherwise stamp over it).
                        # Gate on the Team's own home window instead.
                        if team_window_id is None:
                            raise TeamError(
                                "scale-up requires a team window id for "
                                "inside-tmux authority"
                            )
                        authority_target = team_window_id
                        create_predicate = _and_tmux_formats(
                            [
                                f"#{{==:#{{session_name}},{session}}}",
                                f"#{{==:#{{session_id}},{session_id}}}",
                                f"#{{==:#{{window_id}},{team_window_id}}}",
                                f"#{{==:#{{{_TMUX_LAUNCH_NONCE_OPTION}}},{launch_nonce}}}",
                            ]
                        )
                    created = _tmux_run(
                        [
                            "if-shell",
                            "-F",
                            "-t",
                            authority_target,
                            create_predicate,
                            shlex.join(new_window_args),
                            "",
                        ]
                    )
                else:
                    created = _tmux_run(new_window_args)
                if created.returncode == 0:
                    lines = (created.stdout or "").splitlines()
                    parts = lines[0].split("\t") if len(lines) == 1 else []
                    try:
                        actual_index = int(parts[0])
                    except (IndexError, ValueError):
                        actual_index = -1
                    window_id = parts[1] if len(parts) == 3 else ""
                    pane_id = parts[2] if len(parts) == 3 else ""
                    handles_valid = (
                        _TMUX_WINDOW_ID.fullmatch(window_id) is not None
                        and _TMUX_PANE_ID.fullmatch(pane_id) is not None
                    )
                    if handles_valid:
                        rec["window_id"] = window_id
                        rec["pane_id"] = pane_id
                        rec["window_nonce"] = window_nonce
                        created_records.append(rec)
                    if (
                        len(parts) != 3
                        or actual_index != target_index
                        or not handles_valid
                    ):
                        if not handles_valid:
                            recovered = _tmux_run(
                                [
                                    "display-message",
                                    "-p",
                                    "-t",
                                    target,
                                    "-F",
                                    _TMUX_RECOVER_WINDOW_FORMAT,
                                ]
                            )
                            recovered_lines = (recovered.stdout or "").splitlines()
                            recovered_parts = (
                                recovered_lines[0].split("\t")
                                if len(recovered_lines) == 1
                                else []
                            )
                            if (
                                recovered.returncode == 0
                                and len(recovered_parts) == 7
                                and recovered_parts[0] == str(target_index)
                                and _TMUX_WINDOW_ID.fullmatch(recovered_parts[1])
                                is not None
                                and _TMUX_PANE_ID.fullmatch(recovered_parts[2])
                                is not None
                                and recovered_parts[3] == launch_name
                                and recovered_parts[4] == session
                                and recovered_parts[5] == session_id
                                and recovered_parts[6] == launch_nonce
                            ):
                                rec["window_id"] = recovered_parts[1]
                                rec["pane_id"] = recovered_parts[2]
                                rec["window_nonce"] = window_nonce
                                created_records.append(rec)
                                handles_valid = True
                        evidence = repr((created.stdout or "")[:400])
                        suffix = (
                            ""
                            if handles_valid
                            else "; immutable cleanup handle unavailable"
                        )
                        raise TeamError(
                            f"tmux new-window {phase} for {tid!r} did not return "
                            f"an exact window/pane identity: stdout={evidence}{suffix}"
                        )
                    bind_and_rename(
                        rec,
                        {
                            "window_id": window_id,
                            "pane_id": pane_id,
                            "window_name": launch_name,
                            "window_nonce": "",
                        },
                    )
                    rec["window_index"] = actual_index
                    allocation_floor = actual_index + 1
                    break

                lost_result_orphan = discover(rec) if session_id is not None else None
                if lost_result_orphan is not None:
                    bind_and_rename(rec, lost_result_orphan)
                    adopted_index = _tmux_run(
                        [
                            "display-message",
                            "-p",
                            "-t",
                            str(rec["window_id"]),
                            "#{window_index}",
                        ]
                    )
                    lines = (adopted_index.stdout or "").splitlines()
                    try:
                        actual_index = int(lines[0]) if len(lines) == 1 else -1
                    except ValueError:
                        actual_index = -1
                    if adopted_index.returncode != 0 or actual_index < planned_index:
                        raise TeamError(
                            f"failed to recover ambiguous tmux launch for {tid!r}"
                        )
                    rec["window_index"] = actual_index
                    created_records.append(rec)
                    allocation_floor = actual_index + 1
                    break

                collision = _tmux_run(
                    [
                        "display-message",
                        "-p",
                        "-t",
                        target,
                        "#{window_index}",
                    ]
                )
                collision_lines = (collision.stdout or "").splitlines()
                if collision.returncode != 0 or collision_lines != [str(target_index)]:
                    err = (created.stderr or created.stdout or "").strip()[:400]
                    raise TeamError(
                        f"failed to create scaled-in window for {tid!r}: {err}"
                    )
                if attempt >= _TMUX_WINDOW_ALLOCATION_RETRIES:
                    raise TeamError(
                        f"failed to allocate monotonic tmux window for {tid!r}: "
                        "collision retry budget exhausted"
                    )
                listed = _tmux_run(
                    [
                        "list-windows",
                        "-t",
                        session,
                        "-F",
                        "#{window_index}",
                    ]
                )
                index_lines = (listed.stdout or "").splitlines()
                if listed.returncode != 0 or not index_lines:
                    err = (listed.stderr or listed.stdout or "").strip()[:400]
                    raise TeamError(
                        f"failed to inspect tmux window indices for {tid!r}: {err}"
                    )
                try:
                    occupied = [int(line) for line in index_lines]
                except ValueError as exc:
                    raise TeamError(
                        f"tmux list-windows returned invalid indices for {tid!r}: "
                        f"{repr((listed.stdout or '')[:400])}"
                    ) from exc
                if any(index < 0 for index in occupied):
                    raise TeamError(
                        f"tmux list-windows returned invalid indices for {tid!r}: "
                        f"{repr((listed.stdout or '')[:400])}"
                    )
                target_index = max(target_index + 1, max(occupied) + 1)
                phase = "collision-retry"
            else:  # pragma: no cover - bounded loop always breaks or raises
                raise TeamError(f"failed to create scaled-in window for {tid!r}")
        if session_id is not None and launch_nonce is not None and (
            _read_tmux_session_identity(session) != (session, session_id)
            or not _tmux_launch_authority_matches(
                session,
                expected_nonce=launch_nonce,
                session_owned=session_owned,
                window_id=team_window_id,
            )
        ):
            raise TeamError("scale-up tmux session authority changed after launch")
    except (OSError, TeamError) as exc:
        rollback_errors = _rollback_created_tmux_windows(created_records)
        detail = str(exc)
        if rollback_errors:
            detail += "; rollback incomplete: " + "; ".join(rollback_errors)
        if isinstance(exc, TeamError):
            raise TeamError(detail) from exc
        raise TeamError(f"scale-up tmux launch failed: {detail}") from exc


def _rollback_created_tmux_windows(
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Best-effort rollback of only windows created by this scale attempt."""
    errors: list[str] = []
    for rec in reversed(records):
        window_id = rec.get("window_id")
        pane_id = rec.get("pane_id")
        task_id = rec.get("task_id")
        window_nonce = rec.get("window_nonce")
        session_name = rec.get("_session_name")
        session_id = rec.get("_session_id")
        launch_nonce = rec.get("_launch_nonce")
        launch_name = f"{task_id}-{window_nonce}"
        if (
            not isinstance(window_id, str)
            or _TMUX_WINDOW_ID.fullmatch(window_id) is None
            or not isinstance(pane_id, str)
            or _TMUX_PANE_ID.fullmatch(pane_id) is None
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(window_nonce, str)
            or _TMUX_NONCE.fullmatch(window_nonce) is None
        ):
            errors.append(f"missing immutable tmux ownership task={task_id}")
            continue
        try:
            nonce_match = (
                f"#{{==:#{{{_TMUX_SCALE_NONCE_OPTION}}},{window_nonce}}}"
            )
            pre_marker_match = _and_tmux_formats(
                [
                    f"#{{==:#{{{_TMUX_SCALE_NONCE_OPTION}}},}}",
                    f"#{{==:#{{window_name}},{launch_name}}}",
                ]
            )
            predicate = _and_tmux_formats(
                [
                    f"#{{==:#{{window_id}},{window_id}}}",
                    f"#{{==:#{{pane_id}},{pane_id}}}",
                    f"#{{||:{nonce_match},{pre_marker_match}}}",
                    *(
                        [
                            f"#{{==:#{{session_name}},{session_name}}}",
                            f"#{{==:#{{session_id}},{session_id}}}",
                            f"#{{==:#{{{_TMUX_LAUNCH_NONCE_OPTION}}},{launch_nonce}}}",
                        ]
                        if (
                            isinstance(session_name, str)
                            and session_name
                            and isinstance(session_id, str)
                            and session_id
                            and isinstance(launch_nonce, str)
                            and _TMUX_NONCE.fullmatch(launch_nonce) is not None
                        )
                        else []
                    ),
                ]
            )
            killed = _tmux_run(
                [
                    "if-shell",
                    "-F",
                    "-t",
                    window_id,
                    predicate,
                    f"kill-window -t {window_id}",
                    "",
                ]
            )
            presence = _tmux_window_presence(window_id)
            if presence is not False:
                err = (killed.stderr or killed.stdout or "").strip()[:400]
                state = "still present" if presence else "disappearance unverified"
                label = "identity mismatch" if killed.returncode == 0 else "failed"
                errors.append(
                    f"tmux rollback {label} task={task_id} window={window_id}; "
                    f"{state}: {err}"
                )
        except OSError as exc:
            errors.append(f"tmux rollback failed task={task_id}: {exc}")
    return errors


def _tmux_window_presence(window_id: str) -> bool | None:
    """Return exact global window-id presence, or None when not provable."""
    listed = _tmux_run(["list-windows", "-a", "-F", "#{window_id}"])
    if listed.returncode != 0:
        return None
    lines = (listed.stdout or "").splitlines()
    if any(_TMUX_WINDOW_ID.fullmatch(line) is None for line in lines):
        return None
    return window_id in lines


def _readback_committed_scale_up(
    root: Path,
    run_id: str,
    *,
    generation: int | None,
    receipt_sha256: str | None,
    previous_generation: int,
    previous_receipt_sha256: str,
    next_worker_index: int,
    records: Sequence[Mapping[str, Any]],
) -> tuple[_ScaleCommitOutcome, dict[str, Any] | None]:
    """Classify an ambiguous meta commit without collapsing unknown into absent."""
    if generation is None or receipt_sha256 is None:
        return "unknown", None
    try:
        current = load_team_meta(root, run_id)
    except (OSError, TeamError, TypeError, ValueError):
        return "unknown", None
    try:
        current_by_id = {
            str(record.get("task_id")): record
            for record in current.get("tasks") or []
            if isinstance(record, Mapping) and record.get("task_id")
        }
        identity_fields = (
            "window_index",
            "window_id",
            "window_nonce",
            "pane_id",
            "pid",
            "pgid",
            "pid_start",
        )
        expected_ids = {str(record.get("task_id") or "") for record in records}
        committed_shape = (
            current.get("identity_generation") == generation
            and current.get("identity_receipt_sha256") == receipt_sha256
            and int(current.get("next_worker_index") or 0) >= next_worker_index
            and all(
                (actual := current_by_id.get(str(expected.get("task_id") or "")))
                is not None
                and all(
                    actual.get(field) == expected.get(field)
                    for field in identity_fields
                )
                for expected in records
            )
        )
        prior_shape = (
            current.get("identity_generation", 0) == previous_generation
            and current.get("identity_receipt_sha256") == previous_receipt_sha256
            and expected_ids.isdisjoint(current_by_id)
        )
    except (TypeError, ValueError):
        return "unknown", None
    if not committed_shape and not prior_shape:
        return "unknown", None
    try:
        from omg_cli.contracts.path_keys import fsync_existing_managed_dir

        _load_team_identity_chain(root, run_id, current)
        fsync_existing_managed_dir(team_dir(root, run_id))
    except (OSError, TeamError, TypeError, ValueError):
        return "unknown", None
    if committed_shape:
        return "committed", dict(current)
    return "not_committed", None


def _readback_committed_scale_down(
    root: Path,
    run_id: str,
    *,
    generation: int | None,
    receipt_sha256: str | None,
    previous_generation: int,
    previous_receipt_sha256: str,
    victim_ids: set[str],
    last_scale: Mapping[str, Any],
) -> tuple[_ScaleCommitOutcome, dict[str, Any] | None]:
    """Classify an ambiguous scale-down meta commit (result-loss adoption).

    When ``mutate_team_meta`` committed victims to ``scaled_down`` but the
    caller observed ``OSError`` / lost the success response, identical retry
    must adopt the on-disk state instead of refusing for "only one active pane".
    """
    if generation is None or receipt_sha256 is None:
        return "unknown", None
    try:
        current = load_team_meta(root, run_id)
    except (OSError, TeamError, TypeError, ValueError):
        return "unknown", None
    try:
        current_by_id = {
            str(record.get("task_id")): record
            for record in current.get("tasks") or []
            if isinstance(record, Mapping) and record.get("task_id")
        }
        victims_committed = bool(victim_ids) and all(
            (row := current_by_id.get(tid)) is not None
            and str(row.get("status") or "") == STATUS_SCALED_DOWN
            for tid in victim_ids
        )
        # Adopt on identity fields only — last_scale.actions is volatile
        # telemetry and must not turn a true commit into "unknown".
        disk_last = current.get("last_scale")
        last_ok = isinstance(disk_last, Mapping) and (
            disk_last.get("op") == last_scale.get("op")
            and disk_last.get("n") == last_scale.get("n")
            and set(disk_last.get("task_ids") or [])
            == set(last_scale.get("task_ids") or [])
        )
        committed_shape = (
            current.get("identity_generation") == generation
            and current.get("identity_receipt_sha256") == receipt_sha256
            and victims_committed
            and last_ok
        )
        prior_shape = (
            current.get("identity_generation", 0) == previous_generation
            and current.get("identity_receipt_sha256") == previous_receipt_sha256
            and all(
                (row := current_by_id.get(tid)) is not None
                and str(row.get("status") or "") != STATUS_SCALED_DOWN
                for tid in victim_ids
            )
        )
    except (TypeError, ValueError):
        return "unknown", None
    if not committed_shape and not prior_shape:
        return "unknown", None
    try:
        from omg_cli.contracts.path_keys import fsync_existing_managed_dir

        _load_team_identity_chain(root, run_id, current)
        fsync_existing_managed_dir(team_dir(root, run_id))
    except (OSError, TeamError, TypeError, ValueError):
        return "unknown", None
    if committed_shape:
        return "committed", dict(current)
    return "not_committed", None


def _load_optional_pending_scale_receipt(
    root: Path,
    run_id: str,
    generation: int,
) -> tuple[dict[str, Any], str] | None:
    """Read an uncommitted next-generation receipt without mutating it."""
    pending = _load_future_identity_receipts(
        root,
        run_id,
        committed_generation=generation - 1,
    )
    if not pending:
        return None
    if set(pending) != {generation}:
        raise TeamError(
            "pending identity receipt generations are ambiguous; lifecycle refused"
        )
    return pending[generation]


def _load_future_identity_receipts(
    root: Path,
    run_id: str,
    *,
    committed_generation: int,
) -> dict[int, tuple[dict[str, Any], str]]:
    """Strictly read every receipt ahead of the committed identity generation."""
    from omg_cli.contracts.path_keys import (
        open_existing_managed_dir_fd,
        read_managed_regular_bytes,
    )
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    receipt_dir = team_dir(root, run_id) / "identity-receipts"
    directory_fd = -1
    try:
        directory_fd = open_existing_managed_dir_fd(receipt_dir)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, ContractPathError) as exc:
        raise TeamError("pending identity receipt directory unreadable") from exc
    try:
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise TeamError("pending identity receipt directory unreadable") from exc
    finally:
        os.close(directory_fd)

    candidates: dict[int, str] = {}
    for name in names:
        match = re.fullmatch(r"([0-9]{8})\.json", name)
        if match is None:
            continue
        generation = int(match.group(1))
        if generation <= committed_generation:
            continue
        if generation in candidates:
            raise TeamError("pending identity receipt generations are ambiguous")
        candidates[generation] = name

    out: dict[int, tuple[dict[str, Any], str]] = {}
    for generation, name in sorted(candidates.items()):
        path = receipt_dir / name
        try:
            body = read_managed_regular_bytes(
                path,
                max_bytes=_IDENTITY_WAL_MAX_BYTES,
            )
            parsed = parse_canonical_json_bytes(body)
        except (OSError, ValueError, ContractPathError) as exc:
            raise TeamError(
                f"pending identity receipt generation {generation} unreadable or invalid"
            ) from exc
        if not isinstance(parsed, dict):
            raise TeamError(
                f"pending identity receipt generation {generation} must be an object"
            )
        out[generation] = (
            parsed,
            sha256_hex(canonical_json_bytes(parsed)),
        )
    return out


def _validate_pending_scale_ownership(
    root: Path,
    run_id: str,
    task_specs: Sequence[Mapping[str, Any]],
    *,
    manifest_body: bytes,
) -> None:
    """Validate the exact descriptor-confined bytes hashed by the receipt."""
    try:
        manifest = json.loads(manifest_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TeamError("pending scale-up ownership manifest invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "writer",
            "schema_version",
            "run_id",
            "required_capability_mode",
            "tasks",
            "created_at",
            "status",
        }
        or manifest.get("writer") != CLI_WRITER
        or manifest.get("schema_version") != 1
        or manifest.get("run_id") != run_id
        or manifest.get("required_capability_mode") != "read-write"
        or manifest.get("status") != "open"
        or not isinstance(manifest.get("created_at"), str)
    ):
        raise TeamError("pending scale-up ownership manifest invalid")
    rows = manifest.get("tasks")
    if not isinstance(rows, list):
        raise TeamError("pending scale-up ownership manifest invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    file_owners: dict[str, str] = {}
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or set(row)
            != {
                "task_id",
                "role",
                "capability_mode",
                "owned_files",
                "worktree_path",
                "coordination",
                "status",
            }
        ):
            raise TeamError("pending scale-up ownership task row invalid")
        try:
            task_id = validate_task_id(str(row.get("task_id") or ""))
        except WorkerError as exc:
            raise TeamError("pending scale-up ownership task row invalid") from exc
        owned = row.get("owned_files")
        role = row.get("role")
        capability = row.get("capability_mode")
        coordination = row.get("coordination")
        if (
            task_id in by_id
            or not isinstance(owned, list)
            or not owned
            or not all(isinstance(item, str) and item for item in owned)
            or list(owned) != [_norm_relpath(item) for item in owned]
            or not isinstance(role, str)
            or not role
            or capability not in {"read-only", "read-write"}
            or (coordination is not None and not isinstance(coordination, str))
            or row.get("status") != "planned"
            or row.get("worktree_path")
            != str(worktree_dir(root, run_id, task_id))
        ):
            raise TeamError("pending scale-up ownership task row invalid")
        for relative in owned:
            previous = file_owners.get(relative)
            if previous is not None and previous != task_id and not coordination:
                raise TeamError("pending scale-up ownership collision invalid")
            file_owners[relative] = task_id
        by_id[task_id] = row
    for expected in _canonical_scale_task_specs(task_specs):
        row = by_id.get(expected["task_id"])
        if not isinstance(row, Mapping):
            raise TeamError(
                f"pending scale-up ownership differs task={expected['task_id']}"
            )
        role = row.get("role")
        if role in (None, "", "omg-executor"):
            role = "executor"
        actual = {
            "task_id": str(row.get("task_id") or ""),
            "owned_files": [
                _norm_relpath(item)
                for item in list(row.get("owned_files") or [])
                if isinstance(item, str) and item.strip()
            ],
            "role": role,
            "capability_mode": str(row.get("capability_mode") or "read-write"),
            "coordination": str(row.get("coordination") or "").strip() or None,
        }
        if actual != expected:
            raise TeamError(
                f"pending scale-up ownership differs task={expected['task_id']}"
            )
        if row.get("worktree_path") != str(
            worktree_dir(root, run_id, expected["task_id"])
        ):
            raise TeamError(
                f"pending scale-up worktree differs task={expected['task_id']}"
            )


def _validate_pending_worktree(
    path: Path,
    *,
    root: Path,
    run_id: str,
    task_id: str,
) -> None:
    """Delegate worktree authority to the canonical worker validator."""
    try:
        validate_task_worktree(root, run_id, task_id, path=path)
    except WorkerError as exc:
        raise TeamError(f"pending scale-up worktree is not OMG-managed: {path}") from exc


def _pending_scale_records(
    root: Path,
    run_id: str,
    *,
    receipt: Mapping[str, Any],
    request_sha256: str,
    task_specs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Recover the immutable launch plan without rebuilding any artifact."""
    from omg_cli.contracts.path_keys import (
        fsync_existing_managed_dir,
        read_managed_regular_bytes,
    )
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    intent = receipt.get("scale_intent")
    intent_hash = receipt.get("scale_intent_sha256")
    intent_keys = frozenset(intent) if isinstance(intent, Mapping) else frozenset()
    legacy_keys = {"request_sha256", "records", "artifacts"}
    current_keys = legacy_keys | {"scale_wal_sha256"}
    if (
        receipt.get("schema_version") != IDENTITY_RECEIPT_SCHEMA_VERSION
        or not isinstance(intent, Mapping)
        or intent_keys not in {frozenset(legacy_keys), frozenset(current_keys)}
        or not isinstance(intent_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", intent_hash) is None
        or sha256_hex(canonical_json_bytes(intent)) != intent_hash
    ):
        raise TeamError("pending scale-up receipt lacks authenticated retry intent")
    if intent.get("request_sha256") != request_sha256:
        raise TeamError("pending scale-up retry intent differs from original")
    wal_hash = intent.get("scale_wal_sha256")
    if wal_hash is not None:
        if (
            not isinstance(wal_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", wal_hash) is None
        ):
            raise TeamError("pending scale-up WAL hash mismatch")
        try:
            wal_body = read_managed_regular_bytes(
                _scale_wal_path(root, run_id, int(receipt["generation"]))
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise TeamError("pending scale-up WAL is unreadable") from exc
        if sha256_hex(wal_body) != wal_hash:
            raise TeamError("pending scale-up WAL hash mismatch")
    raw_records = intent.get("records")
    raw_artifacts = intent.get("artifacts")
    if (
        not isinstance(raw_records, list)
        or not isinstance(raw_artifacts, list)
        or len(raw_records) != len(task_specs)
    ):
        raise TeamError("pending scale-up retry intent shape mismatch")

    expected_specs = _canonical_scale_task_specs(task_specs)
    records: list[dict[str, Any]] = []
    expected_artifacts = {str(ownership_manifest_path(root, run_id).relative_to(root))}
    for expected, raw in zip(expected_specs, raw_records, strict=True):
        if not isinstance(raw, Mapping) or set(raw) != _SCALE_INTENT_RECORD_KEYS:
            raise TeamError("pending scale-up intent record keys mismatch")
        record = dict(raw)
        task_id = expected["task_id"]
        expected_worktree = worktree_dir(root, run_id, task_id)
        expected_argv = team_dir(root, run_id) / f"{task_id}.argv.json"
        if (
            record.get("task_id") != task_id
            or record.get("role") != expected["role"]
            or record.get("worktree") != str(expected_worktree)
            or record.get("argv_path")
            != str(expected_argv.relative_to(_run_dir(root, run_id)))
            or record.get("status") != STATUS_RUNNING
            or not isinstance(record.get("pane_command"), str)
            or not record.get("pane_command")
            or not isinstance(record.get("argv"), list)
            or not record.get("argv")
            or not all(isinstance(item, str) for item in record["argv"])
        ):
            raise TeamError("pending scale-up intent record mismatch")
        _validate_pending_worktree(
            expected_worktree,
            root=root,
            run_id=run_id,
            task_id=task_id,
        )
        prompt_dir = expected_worktree / ".omg" / "team-prompt"
        expected_artifacts.add(str(expected_argv.relative_to(root)))
        expected_artifacts.add(
            str((prompt_dir / f"{task_id}.prompt.md").relative_to(root))
        )
        if record.get("provider") == "grok":
            expected_artifacts.add(
                str((prompt_dir / "last_prompt.md").relative_to(root))
            )
        records.append(record)

    artifact_hashes: dict[str, str] = {}
    for row in raw_artifacts:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"path", "sha256"}
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256"))) is None
            or row["path"] in artifact_hashes
        ):
            raise TeamError("pending scale-up artifact receipt mismatch")
        artifact_hashes[str(row["path"])] = str(row["sha256"])
    if set(artifact_hashes) != expected_artifacts:
        raise TeamError("pending scale-up artifact set mismatch")
    artifact_bodies: dict[str, bytes] = {}
    for relative, expected_hash in artifact_hashes.items():
        try:
            actual = read_managed_regular_bytes(root / relative)
        except (OSError, ValueError) as exc:
            raise TeamError(
                f"pending scale-up artifact unreadable: {relative}"
            ) from exc
        if sha256_hex(actual) != expected_hash:
            raise TeamError(f"pending scale-up artifact changed: {relative}")
        artifact_bodies[relative] = actual

    ownership_relative = str(ownership_manifest_path(root, run_id).relative_to(root))
    _validate_pending_scale_ownership(
        root,
        run_id,
        task_specs,
        manifest_body=artifact_bodies[ownership_relative],
    )
    fsync_existing_managed_dir(team_dir(root, run_id))
    return records


def _recover_pending_scale_up(
    root: Path,
    run_id: str,
    *,
    meta: Mapping[str, Any],
    tasks_all: Sequence[Mapping[str, Any]],
    active: Sequence[Mapping[str, Any]],
    task_specs: Sequence[Mapping[str, Any]],
    request_sha256: str,
    authority: Mapping[str, Any],
    pending: tuple[dict[str, Any], str],
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Authenticate and rebind a receipt-published, meta-uncommitted scale-up."""
    receipt, receipt_hash = pending
    generation = int(meta.get("identity_generation", 0)) + 1
    before_raw = receipt.get("tasks_before")
    after_raw = receipt.get("tasks_after")
    if (
        receipt.get("operation") != "add"
        or receipt.get("generation") != generation
        or not isinstance(before_raw, list)
        or not isinstance(after_raw, list)
        or any(not isinstance(row, Mapping) for row in [*before_raw, *after_raw])
    ):
        raise TeamError("pending scale-up receipt shape mismatch")
    before_rows = _identity_rows(before_raw)
    after_rows = _identity_rows(after_raw)
    expected_before = _identity_rows(active)
    if before_rows != expected_before or after_rows[: len(before_rows)] != before_rows:
        raise TeamError("pending scale-up receipt continuity mismatch")
    pending_rows = after_rows[len(before_rows) :]
    records = _pending_scale_records(
        root,
        run_id,
        receipt=receipt,
        request_sha256=request_sha256,
        task_specs=task_specs,
    )
    if len(pending_rows) != len(records):
        raise TeamError("pending scale-up worker count mismatch")

    previous_index = -1
    for record, row in zip(records, pending_rows, strict=True):
        if row.get("task_id") != record.get("task_id"):
            raise TeamError("pending scale-up task identity mismatch")
        actual_index = row.get("window_index")
        if (
            not isinstance(actual_index, int)
            or isinstance(actual_index, bool)
            or actual_index < int(record["window_index"])
            or actual_index <= previous_index
        ):
            raise TeamError("pending scale-up window allocation mismatch")
        previous_index = actual_index
        for field in (
            "window_index",
            "window_id",
            "window_nonce",
            "pane_id",
            "pid",
            "pgid",
            "pid_start",
        ):
            if record.get(field) != row.get(field):
                raise TeamError("pending scale-up runtime identity mismatch")

    candidate = dict(meta)
    candidate["tasks"] = [*tasks_all, *records]
    candidate["identity_generation"] = generation
    candidate["identity_receipt_sha256"] = receipt_hash
    chain = _load_team_identity_chain(root, run_id, candidate)
    if not chain or chain[-1] != receipt:
        raise TeamError("pending scale-up receipt chain mismatch")

    session = str(meta.get("session") or "")
    session_id = authority.get("session_id")
    launch_nonce = authority.get("launch_nonce")
    meta_session_owned = bool(meta.get("session_owned", True))
    meta_window_id = meta.get("window_id")
    meta_window_id = (
        str(meta_window_id) if isinstance(meta_window_id, str) else None
    )
    if (
        not isinstance(session_id, str)
        or not isinstance(launch_nonce, str)
        or _read_tmux_session_identity(session) != (session, session_id)
        or not _tmux_launch_authority_matches(
            session,
            expected_nonce=launch_nonce,
            session_owned=meta_session_owned,
            window_id=meta_window_id,
        )
    ):
        raise TeamError("pending scale-up tmux session identity mismatch")
    for record in records:
        pid = record.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise TeamError("pending scale-up live worker identity mismatch")
        live_pid = _read_scaled_pane_pid(session_id=session_id, record=record)
        cleaned_dead = False
        if live_pid is None:
            # Remain-on-exit dead panes keep a pane_id but report pane_dead=1 and
            # no live PID rebind. Clean the exact receipt-bound pane, then treat
            # an absent process as needs_collect (never wedge on presence alone).
            cleaned_dead = _cleanup_exact_dead_recorded_pane(
                record,
                session=session,
                session_id=session_id,
                launch_nonce=launch_nonce,
            )
        current_pgid = _pgid_for_pid(pid)
        current_start = _pid_start_identity(pid)
        if (
            live_pid == pid
            and current_pgid == record.get("pgid")
            and current_start == record.get("pid_start")
        ):
            continue
        pane_id = record.get("pane_id")
        process_absent = current_pgid is None and current_start is None
        pane_absent = (
            isinstance(pane_id, str) and _tmux_pane_presence(pane_id) is False
        )
        if live_pid is None and process_absent and (cleaned_dead or pane_absent):
            record["status"] = STATUS_NEEDS_COLLECT
            continue
        raise TeamError("pending scale-up live worker identity mismatch")
    return records, receipt, receipt_hash


def _read_scaled_pane_pid(
    *,
    session_id: str,
    record: Mapping[str, Any],
) -> int | None:
    """Rebind a scaled pane only when every immutable tmux field still agrees."""
    window_index = record.get("window_index")
    window_id = record.get("window_id")
    pane_id = record.get("pane_id")
    window_nonce = record.get("window_nonce")
    if (
        not isinstance(window_index, int)
        or isinstance(window_index, bool)
        or window_index < 0
        or not isinstance(window_id, str)
        or _TMUX_WINDOW_ID.fullmatch(window_id) is None
        or not isinstance(pane_id, str)
        or _TMUX_PANE_ID.fullmatch(pane_id) is None
        or not isinstance(window_nonce, str)
        or _TMUX_NONCE.fullmatch(window_nonce) is None
    ):
        return None
    observed = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "-F",
            _TMUX_SCALED_PANE_FORMAT,
        ]
    )
    lines = (observed.stdout or "").splitlines()
    parts = lines[0].split("\t") if len(lines) == 1 else []
    if (
        observed.returncode != 0
        or len(parts) != 7
        or parts[:4] != [session_id, str(window_index), window_id, pane_id]
        or parts[5] != window_nonce
        or parts[6] != "0"
    ):
        return None
    try:
        pid = int(parts[4])
    except ValueError:
        return None
    return pid if pid > 0 else None


def _read_recorded_tmux_pane(
    rec: Mapping[str, Any],
    *,
    session: str,
    session_id: str,
) -> tuple[str, str] | None:
    """Read one receipt-bound pane through immutable tmux handles.

    ``window_index`` is a window slot in windows topology and a pane slot in
    split topology, so it is accepted only as corroborating evidence and is
    never used as a destructive target.
    """
    window_index = rec.get("window_index")
    recorded_window_id = rec.get("window_id")
    pane_id = rec.get("pane_id")
    pid = rec.get("pid")
    window_nonce = rec.get("window_nonce")
    if (
        not isinstance(window_index, int)
        or isinstance(window_index, bool)
        or window_index < 0
        or not isinstance(pane_id, str)
        or _TMUX_PANE_ID.fullmatch(pane_id) is None
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or (
            recorded_window_id is not None
            and (
                not isinstance(recorded_window_id, str)
                or _TMUX_WINDOW_ID.fullmatch(recorded_window_id) is None
            )
        )
        or (
            window_nonce is not None
            and (
                not isinstance(window_nonce, str)
                or _TMUX_NONCE.fullmatch(window_nonce) is None
                or recorded_window_id is None
            )
        )
    ):
        return None
    observed = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "-F",
            _TMUX_RECORDED_PANE_FORMAT,
        ]
    )
    lines = (observed.stdout or "").splitlines()
    parts = lines[0].split("\t") if len(lines) == 1 else []
    if (
        observed.returncode != 0
        or len(parts) != 9
        or parts[0] != session
        or parts[1] != session_id
        or str(window_index) not in parts[2:4]
        or _TMUX_WINDOW_ID.fullmatch(parts[4]) is None
        or parts[5] != pane_id
        or parts[6] != str(pid)
        or (recorded_window_id is not None and parts[4] != recorded_window_id)
        or (window_nonce is not None and parts[7] != window_nonce)
        or (window_nonce is None and parts[7] != "")
        or parts[8] != "0"
    ):
        return None
    return parts[4], pane_id


def _cleanup_exact_dead_recorded_pane(
    rec: Mapping[str, Any],
    *,
    session: str,
    session_id: str,
    launch_nonce: str,
) -> bool:
    """Remove only a receipt-bound remain-on-exit pane; never signal its PID."""
    pane_id = rec.get("pane_id")
    window_id = rec.get("window_id")
    pid = rec.get("pid")
    window_nonce = rec.get("window_nonce")
    if (
        not isinstance(pane_id, str)
        or _TMUX_PANE_ID.fullmatch(pane_id) is None
        or not isinstance(window_id, str)
        or _TMUX_WINDOW_ID.fullmatch(window_id) is None
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(window_nonce, str)
        or _TMUX_NONCE.fullmatch(window_nonce) is None
    ):
        return False
    shown = _tmux_run(
        ["display-message", "-p", "-t", pane_id, "-F", _TMUX_RECORDED_PANE_FORMAT]
    )
    parts = (shown.stdout or "").strip().split("\t")
    if (
        shown.returncode != 0
        or len(parts) != 9
        or parts[0] != session
        or parts[1] != session_id
        or parts[4] != window_id
        or parts[5] != pane_id
        or parts[6] != str(pid)
        or parts[7] != window_nonce
        or parts[8] != "1"
    ):
        return False
    predicate = _and_tmux_formats(
        [
            f"#{{==:#{{session_name}},{session}}}",
            f"#{{==:#{{session_id}},{session_id}}}",
            f"#{{==:#{{{_TMUX_LAUNCH_NONCE_OPTION}}},{launch_nonce}}}",
            f"#{{==:#{{window_id}},{window_id}}}",
            f"#{{==:#{{pane_id}},{pane_id}}}",
            f"#{{==:#{{pane_pid}},{pid}}}",
            f"#{{==:#{{{_TMUX_SCALE_NONCE_OPTION}}},{window_nonce}}}",
            "#{==:#{pane_dead},1}",
        ]
    )
    killed = _tmux_run(
        ["if-shell", "-F", "-t", pane_id, predicate, f"kill-pane -t {pane_id}", ""]
    )
    if killed.returncode != 0 or _tmux_pane_presence(pane_id) is not False:
        raise TeamError(f"exact dead pane cleanup failed pane={pane_id}")
    return True


def _tmux_pane_presence(pane_id: str) -> bool | None:
    """Return exact global pane-id presence, or None when not provable."""
    listed = _tmux_run(["list-panes", "-a", "-F", "#{pane_id}"])
    if listed.returncode != 0:
        return None
    lines = (listed.stdout or "").splitlines()
    if any(_TMUX_PANE_ID.fullmatch(line) is None for line in lines):
        return None
    return pane_id in lines


def _and_tmux_formats(checks: Sequence[str]) -> str:
    """Combine already-safe tmux boolean formats without invoking a shell."""
    if not checks:
        return "0"
    combined = checks[0]
    for check in checks[1:]:
        combined = f"#{{&&:{combined},{check}}}"
    return combined


def _kill_recorded_tmux_pane_atomically(
    rec: Mapping[str, Any],
    *,
    session_id: str,
    launch_nonce: str,
    window_id: str,
    pane_id: str,
) -> subprocess.CompletedProcess[str]:
    """Conditionally kill a pane in one tmux server command queue entry.

    The format predicate and ``kill-pane`` execute inside tmux, closing the
    check/use gap where a restarted server could reuse numeric IDs between two
    client invocations.
    """
    pid = int(rec["pid"])
    window_nonce = rec.get("window_nonce")
    expected_window_nonce = window_nonce if isinstance(window_nonce, str) else ""
    checks = [
        f"#{{==:#{{session_id}},{session_id}}}",
        f"#{{==:#{{{_TMUX_LAUNCH_NONCE_OPTION}}},{launch_nonce}}}",
        f"#{{==:#{{window_id}},{window_id}}}",
        f"#{{==:#{{pane_id}},{pane_id}}}",
        f"#{{==:#{{pane_pid}},{pid}}}",
        f"#{{==:#{{{_TMUX_SCALE_NONCE_OPTION}}},{expected_window_nonce}}}",
    ]
    return _tmux_run(
        [
            "if-shell",
            "-F",
            "-t",
            pane_id,
            _and_tmux_formats(checks),
            f"kill-pane -t {pane_id}",
            "",
        ]
    )


def _kill_pane_recorded(
    rec: Mapping[str, Any],
    *,
    session: str,
    dry: bool,
    actions: list[str],
    errors: list[str],
    signalled: list[dict[str, Any]],
    authority: Mapping[str, Any],
    session_owned: bool = True,
    window_id: str | None = None,
) -> None:
    """Kill only an immutable, immediately revalidated pane identity."""
    tid = rec.get("task_id")
    widx = rec.get("window_index")
    pid = rec.get("pid")
    pgid = rec.get("pgid")
    pane_id = rec.get("pane_id")
    pid_start = rec.get("pid_start")
    raw_session_id = authority.get("session_id")
    if not dry and not isinstance(raw_session_id, str):
        errors.append(f"missing tmux session identity task={tid}")
        return
    session_id = raw_session_id if isinstance(raw_session_id, str) else ""

    def _authority_ok() -> bool:
        expected_nonce = authority.get("launch_nonce")
        return isinstance(expected_nonce, str) and _tmux_launch_authority_matches(
            session,
            expected_nonce=expected_nonce,
            session_owned=session_owned,
            window_id=window_id,
            pane_ids=[pane_id] if isinstance(pane_id, str) else None,
        )

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
            or not tmux_available()
            or _read_tmux_session_identity(session) != (session, session_id)
            or not _authority_ok()
            or _read_recorded_tmux_pane(
                rec,
                session=session,
                session_id=session_id,
            )
            is None
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
        if (
            _read_tmux_session_identity(session) != (session, session_id)
            or not _authority_ok()
            or _read_recorded_tmux_pane(
                rec,
                session=session,
                session_id=session_id,
            )
            is None
        ):
            errors.append(f"immutable pre-signal tmux identity drift task={tid}")
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

    # 2) close only the exact pane. Never target mutable session:index: tmux
    # may renumber it as soon as SIGTERM makes another pane/window disappear.
    if session and not dry and pane_id is not None:
        try:
            if _read_tmux_session_identity(session) != (
                session,
                session_id,
            ) or not _authority_ok():
                errors.append(f"post-signal tmux session identity drift task={tid}")
                return
            live_handle = _read_recorded_tmux_pane(
                rec,
                session=session,
                session_id=session_id,
            )
            if live_handle is None:
                presence = _tmux_pane_presence(pane_id)
                if presence is False:
                    actions.append(f"tmux pane already gone pane={pane_id} task={tid}")
                    return
                state = "still present" if presence else "absence unverified"
                errors.append(
                    f"post-signal tmux pane identity mismatch task={tid}; {state}"
                )
                return
            launch_nonce = authority.get("launch_nonce")
            if not isinstance(launch_nonce, str):
                errors.append(f"missing tmux launch nonce task={tid}")
                return
            r = _kill_recorded_tmux_pane_atomically(
                rec,
                session_id=session_id,
                launch_nonce=launch_nonce,
                window_id=live_handle[0],
                pane_id=live_handle[1],
            )
            presence = _tmux_pane_presence(pane_id)
            if presence is not False:
                state = "still present" if presence else "disappearance unverified"
                err = (r.stderr or r.stdout or "").strip()[:400]
                errors.append(
                    f"tmux kill-pane task={tid} pane={pane_id}; {state}: {err}"
                )
                return
            actions.append(
                f"tmux conditional kill-pane -t {pane_id} (exit {r.returncode})"
            )
        except OSError as exc:
            errors.append(f"tmux kill-pane task={tid}: {exc}")


def _recover_or_kill_remove_victim(
    rec: Mapping[str, Any],
    *,
    session: str,
    authority: Mapping[str, Any],
    actions: list[str],
    errors: list[str],
    signalled: list[dict[str, Any]],
    session_owned: bool = True,
    window_id: str | None = None,
) -> None:
    """Resume an authenticated remove after partial signal/pane side effects."""
    task_id = str(rec.get("task_id") or "")
    pane_id = rec.get("pane_id")
    pid = rec.get("pid")
    pgid = rec.get("pgid")
    pid_start = rec.get("pid_start")
    session_id = authority.get("session_id")
    launch_nonce = authority.get("launch_nonce")
    if (
        not isinstance(pane_id, str)
        or _TMUX_PANE_ID.fullmatch(pane_id) is None
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(pgid, int)
        or isinstance(pgid, bool)
        or pgid <= 0
        or not isinstance(pid_start, str)
        or not pid_start
        or not isinstance(session_id, str)
        or not isinstance(launch_nonce, str)
        or _read_tmux_session_identity(session) != (session, session_id)
        or not _tmux_launch_authority_matches(
            session,
            expected_nonce=launch_nonce,
            session_owned=session_owned,
            window_id=window_id,
            pane_ids=[pane_id],
        )
    ):
        errors.append(f"remove retry authority mismatch task={task_id}")
        return
    current_start = _pid_start_identity(pid)
    current_pgid = _pgid_for_pid(pid)
    if _cleanup_exact_dead_recorded_pane(
        rec,
        session=session,
        session_id=session_id,
        launch_nonce=launch_nonce,
    ):
        actions.append(f"remove retry killed dead pane={pane_id} task={task_id}")
        return
    process_exact = current_start == pid_start and current_pgid == pgid
    process_absent = current_start is None and current_pgid is None
    live_handle = _read_recorded_tmux_pane(
        rec,
        session=session,
        session_id=session_id,
    )
    presence = _tmux_pane_presence(pane_id)
    if live_handle is not None and process_exact:
        _kill_pane_recorded(
            rec,
            session=session,
            dry=False,
            actions=actions,
            errors=errors,
            signalled=signalled,
            authority=authority,
            session_owned=session_owned,
            window_id=window_id,
        )
        return
    if live_handle is not None and process_absent:
        killed = _kill_recorded_tmux_pane_atomically(
            rec,
            session_id=session_id,
            launch_nonce=launch_nonce,
            window_id=live_handle[0],
            pane_id=live_handle[1],
        )
        if killed.returncode != 0 or _tmux_pane_presence(pane_id) is not False:
            errors.append(f"remove retry dead pane cleanup failed task={task_id}")
            return
        actions.append(f"remove retry killed dead pane={pane_id} task={task_id}")
        return
    if live_handle is None and presence is False and process_absent:
        actions.append(f"remove retry already complete task={task_id}")
        return
    errors.append(f"remove retry identity drift task={task_id}")


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
        _assert_no_uncommitted_scale_wal(
            root_path,
            rid,
            meta,
            operation=(
                "dry-run scale-down"
                if dry_run or bool(meta.get("dry_run"))
                else "scale-down"
            ),
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
    effective_dry = bool(dry_run or meta.get("dry_run"))
    pending_generation = int(meta.get("identity_generation", 0)) + 1
    pending_wal_exists = _uncommitted_scale_wal_exists(
        root,
        run_id,
        pending_generation,
    )
    pending_scale = _load_optional_pending_scale_receipt(
        root,
        run_id,
        pending_generation,
    )
    if effective_dry and pending_wal_exists:
        _assert_no_uncommitted_scale_wal(
            root,
            run_id,
            meta,
            operation="dry-run scale-up",
        )
    if effective_dry and pending_scale is not None:
        raise TeamError(
            "dry-run scale-up refused while an identity receipt is pending; "
            "retry the original live scale --add request"
        )
    retrying_transaction = pending_wal_exists or pending_scale is not None
    cap = max_workers_cap()
    if not retrying_transaction:
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
    new_task_specs = _canonical_scale_task_specs(new_task_specs)
    if not retrying_transaction:
        _validate_scale_request_preflight(
            root,
            run_id,
            active=active,
            task_specs=new_task_specs,
        )
    request_payload = _scale_request_payload(
        meta=meta,
        active=active,
        task_specs=new_task_specs,
        start_index=start_idx,
        yolo=yolo,
        safe=safe,
        extra=extra,
    )
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    request_sha256 = sha256_hex(canonical_json_bytes(request_payload))

    new_records: list[dict[str, Any]] = []
    generation: int | None = None
    scale_receipt_hash: str | None = None
    session = str(meta.get("session") or "")
    windows_created = False
    receipt_bound = pending_scale is not None
    receipt_attempted = False
    scale_wal: dict[str, Any] | None = None
    scale_wal_sha256: str | None = None
    try:
        authority: Mapping[str, Any] | None = None
        if not effective_dry:
            authority = _load_team_identity_chain(root, run_id, meta)[0]
            pending_intent = (
                pending_scale[0].get("scale_intent")
                if pending_scale is not None
                else None
            )
            pending_wal_hash = (
                pending_intent.get("scale_wal_sha256")
                if isinstance(pending_intent, Mapping)
                else None
            )
            if pending_scale is None or pending_wal_hash is not None:
                scale_wal, scale_wal_sha256 = _load_or_publish_scale_wal(
                    root,
                    run_id,
                    generation=pending_generation,
                    meta=meta,
                    authority=authority,
                    request=request_payload,
                    request_sha256=request_sha256,
                    task_specs=new_task_specs,
                    start_index=start_idx,
                    allow_create=pending_scale is None,
                )
                if pending_wal_hash is not None and scale_wal_sha256 != pending_wal_hash:
                    raise TeamError("pending scale-up WAL hash mismatch")
        if pending_scale is not None:
            assert authority is not None
            new_records, _scale_receipt, scale_receipt_hash = _recover_pending_scale_up(
                root,
                run_id,
                meta=meta,
                tasks_all=tasks_all,
                active=active,
                task_specs=new_task_specs,
                request_sha256=request_sha256,
                authority=authority,
                pending=pending_scale,
            )
            generation = pending_generation
        else:
            # Fresh path: publish ownership and launch artifacts exactly once.
            existing_own = _ownership_tasks_from_manifest(root, run_id)
            if not existing_own:
                for rec in tasks_all:
                    if not isinstance(rec, Mapping):
                        continue
                    tid = str(rec.get("task_id") or "")
                    if tid:
                        existing_own.append(
                            {
                                "task_id": tid,
                                "owned_files": [f".omg/team-scale/{tid}.md"],
                                "role": rec.get("role") or "executor",
                            }
                        )
            try:
                manifest = _ensure_scale_ownership_manifest(
                    root,
                    run_id,
                    existing_tasks=existing_own,
                    task_specs=new_task_specs,
                )
                new_ids = {str(task["task_id"]) for task in new_task_specs}
                for mtask in manifest.get("tasks") or []:
                    tid = str(mtask["task_id"])
                    if tid in new_ids:
                        prepare_task(root, run_id, tid)
            except WorkerError as exc:
                raise TeamError(str(exc)) from exc

            multi_cli = bool(meta.get("multi_cli"))
            roles = [_task_role(task) for task in new_task_specs]
            resolved = _resolve_routing_from_meta(meta, roles) if multi_cli else None
            goal = str(meta.get("goal") or "(no goal)")
            total_after = len(active) + n
            for i, spec in enumerate(new_task_specs):
                pane_kwargs = dict(
                    root=root,
                    run_id=run_id,
                    goal=goal,
                    task=spec,
                    task_index=len(active) + i + 1,
                    task_count=total_after,
                    window_index=start_idx + i,
                    dry_run=effective_dry,
                    multi_cli=multi_cli,
                    resolved=resolved,
                    yolo=yolo,
                    safe=safe,
                    extra=extra,
                )
                rec = None
                if not effective_dry:
                    rec = _reuse_prepared_pane_record(
                        **{key: value for key, value in pane_kwargs.items() if key != "dry_run"}
                    )
                if rec is None:
                    rec = _build_pane_record(**pane_kwargs)
                if not effective_dry:
                    rec["_env_pairs"] = _pane_env_pairs(
                        run_id=run_id,
                        team_id=str(meta.get("team_id") or "team"),
                        worker_id=str(rec["task_id"]),
                        leader_root=root,
                        state_root=root / ".omg" / "state",
                        owner_token=(
                            str(meta["owner_token"])
                            if meta.get("owner_token")
                            else None
                        ),
                    )
                new_records.append(rec)

            if not effective_dry:
                assert scale_wal is not None
                _apply_scale_wal_plan(new_records, scale_wal)

            if not effective_dry:
                assert authority is not None
                for rec in new_records:
                    rec["_session_id"] = str(authority["session_id"])
                    rec["_launch_nonce"] = str(authority["launch_nonce"])
                _add_tmux_windows(
                    session=session,
                    records=new_records,
                    session_owned=bool(meta.get("session_owned", True)),
                    window_id=(
                        str(meta["window_id"])
                        if isinstance(meta.get("window_id"), str)
                        else None
                    ),
                )
                windows_created = True
                session_id = str(authority["session_id"])
                for rec in new_records:
                    pid = _read_scaled_pane_pid(session_id=session_id, record=rec)
                    if pid is not None:
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
                generation = pending_generation
                receipt_attempted = True
                scale_intent = _build_scale_intent(
                    root,
                    run_id,
                    request_sha256=request_sha256,
                    scale_wal_sha256=str(scale_wal_sha256),
                    records=new_records,
                )
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
                    scale_intent=scale_intent,
                )
                receipt_bound = True
    except (OSError, TeamError, ContractPathError) as exc:
        if receipt_attempted and not receipt_bound:
            try:
                receipt_bound = (
                    _load_optional_pending_scale_receipt(
                        root,
                        run_id,
                        pending_generation,
                    )
                    is not None
                )
            except TeamError:
                # Publication is ambiguous: preserve the live window so a
                # later authenticated retry still has a recovery path.
                receipt_bound = True
        rollback_errors = (
            _rollback_created_tmux_windows(new_records)
            if windows_created and not receipt_bound
            else []
        )
        detail = str(exc)
        if receipt_bound:
            detail += "; receipt-bound windows preserved for retry"
        if rollback_errors:
            detail += "; rollback incomplete: " + "; ".join(rollback_errors)
        if isinstance(exc, TeamError):
            raise TeamError(detail) from exc
        raise TeamError(f"scale-up tmux launch failed: {detail}") from exc

    for rec in new_records:
        rec.pop("_env_pairs", None)
        rec.pop("_artifact_paths", None)
        rec.pop("_session_id", None)
        rec.pop("_session_name", None)
        rec.pop("_launch_nonce", None)
        rec.pop("_launch_name", None)
        rec.pop("_planned_window_index", None)

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
    next_idx = max(
        start_idx + n,
        max(
            (int(record["window_index"]) + 1 for record in new_records),
            default=start_idx + n,
        ),
    )
    identity_gen = generation if not effective_dry else None
    identity_hash = scale_receipt_hash if not effective_dry else None

    base_generation = int(meta.get("meta_generation") or 0)
    base_identity_generation = int(meta.get("identity_generation") or 0)
    base_identity_hash = str(
        meta.get("identity_receipt_sha256") or meta.get("launch_receipt_sha256") or ""
    )
    base_identity_rows = _identity_rows(active)
    new_ids = {str(record["task_id"]) for record in new_records}

    def _assert_scale_base(current: Mapping[str, Any]) -> None:
        current_active = _active_tasks(list(current.get("tasks") or []))
        current_ids = {
            str(task.get("task_id") or "")
            for task in current_active
            if task.get("task_id")
        }
        current_hash = str(
            current.get("identity_receipt_sha256")
            or current.get("launch_receipt_sha256")
            or ""
        )
        if (
            int(current.get("identity_generation") or 0) != base_identity_generation
            or current_hash != base_identity_hash
            or _identity_rows(current_active) != base_identity_rows
            or not new_ids.isdisjoint(current_ids)
        ):
            raise TeamError(
                "scale-up refused: identity chain or active tasks changed before meta commit"
            )

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
        _assert_scale_base(current)
        updated = dict(current)
        updated["schema_version"] = int(current.get("schema_version") or SCHEMA_VERSION)
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
                if stop_state in {
                    "stopping",
                    "stopped",
                    "stop_refused",
                } or current.get("stopped_at"):
                    raise TeamError(
                        "scale-up refused after launch side effects: team is "
                        f"stopping/stopped (stop_state={stop_state!r}); "
                        "re-check status"
                    )
                _assert_scale_base(current)
                updated = dict(current)
                merged = [
                    dict(t)
                    for t in (current.get("tasks") or [])
                    if isinstance(t, Mapping)
                ]
                for rec in new_records:
                    merged.append(dict(rec))
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
    except (OSError, TeamError) as exc:
        outcome, committed = _readback_committed_scale_up(
            root,
            run_id,
            generation=identity_gen,
            receipt_sha256=identity_hash,
            previous_generation=int(meta.get("identity_generation", 0)),
            previous_receipt_sha256=str(
                meta.get("identity_receipt_sha256")
                or meta.get("launch_receipt_sha256")
                or ""
            ),
            next_worker_index=next_idx,
            records=new_records,
        )
        if outcome == "committed" and committed is not None:
            updated = committed
        elif outcome == "not_committed":
            detail = str(exc)
            if not effective_dry:
                detail += (
                    "; identity receipt and live windows preserved; retry the "
                    "same scale --add request to reconcile"
                )
            if isinstance(exc, TeamError):
                raise TeamError(detail) from exc
            raise TeamError(f"scale-up meta commit failed: {detail}") from exc
        else:
            detail = (
                f"{exc}; scale-up meta commit outcome unknown; "
                "preserved tmux windows and identity receipt for reconciliation"
            )
            if isinstance(exc, TeamError):
                raise TeamError(detail) from exc
            raise TeamError(f"scale-up meta commit failed: {detail}") from exc

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
    session = str(meta.get("session") or "")
    effective_dry = bool(dry_run or meta.get("dry_run"))
    meta_session_owned = bool(meta.get("session_owned", True))
    meta_window_id = meta.get("window_id")
    meta_window_id = str(meta_window_id) if isinstance(meta_window_id, str) else None
    actions: list[str] = []
    errors: list[str] = []
    signalled: list[dict[str, Any]] = []
    preserved_worktrees: list[str] = []

    authority: Mapping[str, Any] = {}
    generation: int | None = None
    scale_receipt_hash: str | None = None
    immutable_victims: dict[str, Mapping[str, Any]] = {}
    recovering_remove = False
    _scale_receipt: Mapping[str, Any] | None = None

    # Load pending remove receipt *before* drain/min-1 gates so recovery binds
    # the exact receipt-encoded victim set and wrong --remove N surfaces the
    # receipt victims rather than a generic capacity error.
    if not effective_dry:
        chain = _load_team_identity_chain(root, run_id, meta)
        authority = chain[0]
        generation = int(meta.get("identity_generation", 0)) + 1
        pending_remove = _load_optional_pending_scale_receipt(root, run_id, generation)
        if pending_remove is not None:
            _scale_receipt, scale_receipt_hash = pending_remove
            recovering_remove = True

    if len(active) <= 1 and not recovering_remove:
        raise TeamError(
            "scale --remove refused: never remove below 1 active pane "
            "(use omg team stop to tear down the whole team)"
        )
    if n >= len(active) and not recovering_remove:
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

    active_by_id = {
        str(t.get("task_id") or ""): t
        for t in active
        if isinstance(t, Mapping) and t.get("task_id")
    }

    if recovering_remove and _scale_receipt is not None:
        before_rows = _scale_receipt.get("tasks_before")
        after_rows = _scale_receipt.get("tasks_after")
        if not isinstance(before_rows, list) or not isinstance(after_rows, list):
            raise TeamError(
                "pending scale-down receipt intent/authority mismatch: "
                f"generation={generation} operation=remove; "
                "tasks_before/tasks_after must be lists"
            )
        before_ids = {
            str(row.get("task_id") or "")
            for row in before_rows
            if isinstance(row, Mapping) and row.get("task_id")
        }
        after_ids = {
            str(row.get("task_id") or "")
            for row in after_rows
            if isinstance(row, Mapping) and row.get("task_id")
        }
        receipt_victim_ids = before_ids - after_ids
        if not receipt_victim_ids:
            raise TeamError(
                "pending scale-down receipt intent/authority mismatch: "
                f"generation={generation} operation=remove; "
                "receipt encodes no victims"
            )
        if n != len(receipt_victim_ids):
            raise TeamError(
                "pending scale-down receipt intent/authority mismatch: "
                f"generation={generation} operation=remove "
                f"receipt_victims={sorted(receipt_victim_ids)} "
                f"caller_remove={n}; retry exact "
                f"scale --remove {len(receipt_victim_ids)}"
            )
        missing = sorted(tid for tid in receipt_victim_ids if tid not in active_by_id)
        if missing:
            raise TeamError(
                "pending scale-down receipt intent/authority mismatch: "
                f"generation={generation} operation=remove "
                f"receipt_victims={sorted(receipt_victim_ids)} "
                f"not active: {missing}"
            )
        victim_ids = set(receipt_victim_ids)
        victims = [dict(active_by_id[tid]) for tid in sorted(victim_ids)]
    else:
        ordered = sorted(active, key=_drain_key)
        victims = ordered[:n]
        victim_ids = {str(v.get("task_id")) for v in victims}

    survivors = [task for task in active if str(task.get("task_id")) not in victim_ids]

    if not effective_dry:
        assert generation is not None
        assert authority
        if not recovering_remove:
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
                operation="remove",
                tasks_before=active,
                tasks_after=survivors,
            )
        assert _scale_receipt is not None
        assert scale_receipt_hash is not None
        receipt_keys = {
            "store_kind",
            "schema_version",
            "writer",
            "run_id",
            "session_name",
            "session_id",
            "launch_nonce",
            "generation",
            "previous_receipt_sha256",
            "operation",
            "receipt_nonce",
            "tasks_before",
            "tasks_after",
            "scale_intent",
            "scale_intent_sha256",
        }
        if (
            set(_scale_receipt) != receipt_keys
            or _scale_receipt.get("store_kind") != "team_identity_receipt"
            or _scale_receipt.get("schema_version")
            != IDENTITY_RECEIPT_SCHEMA_VERSION
            or _scale_receipt.get("writer") != CLI_WRITER
            or _scale_receipt.get("run_id") != run_id
            or _scale_receipt.get("operation") != "remove"
            or _scale_receipt.get("generation") != generation
            or _scale_receipt.get("session_name") != session
            or _scale_receipt.get("session_id") != authority.get("session_id")
            or _scale_receipt.get("launch_nonce") != authority.get("launch_nonce")
            or _scale_receipt.get("previous_receipt_sha256")
            != str(
                meta.get("identity_receipt_sha256")
                or meta.get("launch_receipt_sha256")
            )
            or not isinstance(_scale_receipt.get("receipt_nonce"), str)
            or re.fullmatch(
                r"[0-9a-f]{32}", str(_scale_receipt.get("receipt_nonce"))
            )
            is None
            or _scale_receipt.get("tasks_before") != _identity_rows(active)
            or _scale_receipt.get("tasks_after") != _identity_rows(survivors)
            or _scale_receipt.get("scale_intent") is not None
            or _scale_receipt.get("scale_intent_sha256") is not None
        ):
            raise TeamError(
                "pending scale-down receipt intent/authority mismatch: "
                f"generation={generation} operation=remove "
                f"receipt_victims={sorted(victim_ids)} caller_remove={n}"
            )
        candidate_tasks = []
        for task in tasks_all:
            candidate = dict(task)
            if str(candidate.get("task_id")) in victim_ids:
                candidate["status"] = STATUS_SCALED_DOWN
            candidate_tasks.append(candidate)
        chain_candidate = dict(meta)
        chain_candidate["tasks"] = candidate_tasks
        chain_candidate["identity_generation"] = generation
        chain_candidate["identity_receipt_sha256"] = scale_receipt_hash
        try:
            _load_team_identity_chain(root, run_id, chain_candidate)
        except TeamError as exc:
            raise TeamError(
                "pending scale-down receipt chain mismatch: "
                f"generation={generation} operation=remove "
                f"receipt_victims={sorted(victim_ids)}"
            ) from exc
        immutable_victims = {
            str(row.get("task_id")): row
            for row in _scale_receipt["tasks_before"]
            if str(row.get("task_id")) in victim_ids
        }

    for v in victims:
        signal_identity = immutable_victims.get(str(v.get("task_id")), v)
        if not effective_dry and recovering_remove:
            # Pending remove receipt: adopt partial signal/pane side effects.
            _recover_or_kill_remove_victim(
                signal_identity,
                session=session,
                authority=authority,
                actions=actions,
                errors=errors,
                signalled=signalled,
                session_owned=meta_session_owned,
                window_id=meta_window_id,
            )
        else:
            # First live attempt (or dry-run) uses the strict kill path so
            # PGID drift / identity mismatches surface before any meta write.
            _kill_pane_recorded(
                signal_identity,
                session=session,
                dry=effective_dry,
                actions=actions,
                errors=errors,
                signalled=signalled,
                authority=authority,
                session_owned=meta_session_owned,
                window_id=meta_window_id,
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
    previous_identity_generation = int(meta.get("identity_generation", 0))
    previous_identity_receipt = str(
        meta.get("identity_receipt_sha256")
        or meta.get("launch_receipt_sha256")
        or ""
    )

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
    except (OSError, TeamError) as exc:
        outcome, committed = _readback_committed_scale_down(
            root,
            run_id,
            generation=down_identity_gen,
            receipt_sha256=down_identity_hash,
            previous_generation=previous_identity_generation,
            previous_receipt_sha256=previous_identity_receipt,
            victim_ids=victim_ids,
            last_scale=last_scale_down,
        )
        if outcome == "committed" and committed is not None:
            updated = committed
        elif outcome == "not_committed":
            detail = str(exc)
            if not effective_dry:
                detail += (
                    "; identity receipt and cancelled panes preserved; retry the "
                    "same scale --remove request to reconcile"
                )
            if isinstance(exc, TeamError):
                raise TeamError(detail) from exc
            raise TeamError(f"scale-down meta commit failed: {detail}") from exc
        else:
            detail = (
                f"{exc}; scale-down meta commit outcome unknown; "
                "preserved cancelled panes and identity receipt for reconciliation"
            )
            if isinstance(exc, TeamError):
                raise TeamError(detail) from exc
            raise TeamError(f"scale-down meta commit failed: {detail}") from exc

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
            "atomically authenticated panes; preserves worktrees; no pkill -f"
        ),
    }


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def _reconcile_resume_tasks(
    meta: Mapping[str, Any],
    *,
    probe_tmux: bool,
    expected_session_id: str | None = None,
    launch_nonce: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], int]:
    """Probe liveness for *meta* tasks; return (status_by_tid, reconciliations, changed).

    Pane-id tasks use the same fail-closed identity chain as ``team_status``
    (session_id + pane nonce + pid/pid_start). Bare ``pane_alive`` is never
    enough to write ``STATUS_RUNNING`` — a respawned pane keeps ``%id`` while
    replacing the worker process.
    """

    session = str(meta.get("session") or "")
    dry = bool(meta.get("dry_run"))
    changed = 0
    tasks_out: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    nonce = launch_nonce if isinstance(launch_nonce, str) else None
    if nonce is None:
        raw_nonce = meta.get("launch_nonce")
        nonce = raw_nonce if isinstance(raw_nonce, str) else None

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
            expected_pid = rec.get("pid")
            expected_pid_i = (
                expected_pid if isinstance(expected_pid, int) and not isinstance(expected_pid, bool) else None
            )
            expected_start = rec.get("pid_start")
            expected_start_s = (
                expected_start if isinstance(expected_start, str) else None
            )
            exact = _status_worker_alive(
                pane_id=pane_id,
                session=session,
                expected_session_id=expected_session_id,
                launch_nonce=nonce,
                expected_pid_start=expected_start_s,
                expected_pid=expected_pid_i,
            )
            win = True if exact else False
        else:
            # Legacy windows topology / hermetic mocks without pane_id.
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
    """Run the complete resume reconciliation under the scale lifecycle lock."""
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    _assert_team_gates(env=env)
    rid = _resolve_run_id(root_path, run_id)
    with acquire_scale_lock(root_path, rid):
        return _resume_team_locked_impl(
            root_path,
            rid,
            probe_tmux=probe_tmux,
            env=env,
        )


def _resume_team_locked_impl(
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
        _assert_no_uncommitted_scale_wal(
            root_path, rid, meta, operation="resume"
        )
        session = str(meta.get("session") or "")
        dry = bool(meta.get("dry_run"))
        expected_session_id: str | None = None
        launch_nonce = meta.get("launch_nonce")
        receipt_path = team_launch_receipt_path(root_path, rid)
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                receipt = None
            if isinstance(receipt, dict):
                if isinstance(receipt.get("session_id"), str):
                    expected_session_id = str(receipt["session_id"])
                if isinstance(receipt.get("launch_nonce"), str):
                    launch_nonce = receipt.get("launch_nonce") or launch_nonce
        status_by_tid, reconciliations, changed = _reconcile_resume_tasks(
            meta,
            probe_tmux=probe_tmux,
            expected_session_id=expected_session_id,
            launch_nonce=launch_nonce if isinstance(launch_nonce, str) else None,
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


def _resync_window_indices(session: str, tasks: Sequence[dict[str, Any]]) -> None:
    """Align logical window_index slots with live pane_index after respawn."""
    observed = _list_pane_identities(session)
    by_pane = {pane_id: idx for idx, (pane_id, _pid) in observed.items()}
    for rec in tasks:
        pane_id = rec.get("pane_id")
        if isinstance(pane_id, str) and pane_id in by_pane:
            rec["window_index"] = by_pane[pane_id]


def _relaunch_bootstrap_command(
    pane_command: str,
    *,
    session_id: str,
    launch_nonce: str,
    target_window_id: str,
    task_id: str,
    relaunch_nonce: str,
) -> str:
    """Build a stable pre-marker command that self-authenticates its pane."""
    pane = '"$TMUX_PANE"'
    task_q = shlex.quote(task_id)
    nonce_q = shlex.quote(relaunch_nonce)
    session_q = shlex.quote(session_id)
    launch_q = shlex.quote(launch_nonce)
    window_q = shlex.quote(target_window_id)
    commands = [
        f'test "$(tmux display-message -p -t {pane} "#{{session_id}}")" = {session_q}',
        f'test "$(tmux display-message -p -t {pane} "#{{@omg_launch_nonce}}")" = {launch_q}',
        f'test "$(tmux display-message -p -t {pane} "#{{window_id}}")" = {window_q}',
        f"tmux set-option -p -t {pane} {_TMUX_RELAUNCH_TASK_OPTION} {task_q}",
        f"tmux set-option -p -t {pane} {_TMUX_RELAUNCH_NONCE_OPTION} {nonce_q}",
        f'test "$(tmux show-options -pvt {pane} {_TMUX_RELAUNCH_TASK_OPTION})" = {task_q}',
        f'test "$(tmux show-options -pvt {pane} {_TMUX_RELAUNCH_NONCE_OPTION})" = {nonce_q}',
        f"exec sh -c {shlex.quote(pane_command)}",
    ]
    body = " && ".join(commands)
    from omg_cli.contracts.writer_chain import sha256_hex

    fingerprint = sha256_hex(body.encode("utf-8"))
    return f"OMG_RELAUNCH_START_SHA={fingerprint}; export OMG_RELAUNCH_START_SHA; {body}"


def _relaunch_start_fingerprint(command: str) -> str:
    match = re.match(
        r"^OMG_RELAUNCH_START_SHA=([0-9a-f]{64}); export OMG_RELAUNCH_START_SHA; (.*)$",
        command,
        flags=re.DOTALL,
    )
    if match is None:
        raise TeamError("relaunch bootstrap fingerprint missing")
    from omg_cli.contracts.writer_chain import sha256_hex

    if sha256_hex(match.group(2).encode("utf-8")) != match.group(1):
        raise TeamError("relaunch bootstrap fingerprint mismatch")
    return match.group(1)


def _relaunch_request_payload(
    meta: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    target_window_id: str,
) -> dict[str, Any]:
    from omg_cli.contracts.writer_chain import sha256_hex

    return {
        "operation": "relaunch",
        "base_identity_generation": int(meta.get("identity_generation", 0)),
        "base_receipt_sha256": str(
            meta.get("identity_receipt_sha256")
            or meta.get("launch_receipt_sha256")
            or ""
        ),
        "session_name": str(meta.get("session") or ""),
        "target_window_id": target_window_id,
        "topology": meta.get("topology"),
        "tasks": [
            {
                "task_id": str(rec.get("task_id") or ""),
                "old_pane_id": rec.get("pane_id"),
                "worktree": str(rec.get("worktree") or ""),
                "pane_command_sha256": sha256_hex(
                    str(rec.get("pane_command") or "").encode("utf-8")
                ),
            }
            for rec in candidates
        ],
    }


def _load_or_publish_relaunch_wal(
    root: Path,
    run_id: str,
    *,
    meta: Mapping[str, Any],
    authority: Mapping[str, Any],
    request: Mapping[str, Any],
    request_sha256: str,
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Create once, or strictly adopt, an immutable pre-respawn WAL."""
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        atomic_write_bytes,
        fsync_existing_managed_dir,
    )
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    generation = int(meta.get("identity_generation", 0)) + 1
    deterministic = {
        "store_kind": "team_relaunch_wal",
        "schema_version": _SCALE_WAL_SCHEMA_VERSION,
        "writer_contract": "relaunch-wal-v1",
        "writer": CLI_WRITER,
        "run_id": run_id,
        "session_name": str(meta.get("session") or ""),
        "session_id": str(authority.get("session_id") or ""),
        "launch_nonce": str(authority.get("launch_nonce") or ""),
        "target_window_id": str(request.get("target_window_id") or ""),
        "generation": generation,
        "base_identity_generation": int(meta.get("identity_generation", 0)),
        "base_receipt_sha256": str(
            meta.get("identity_receipt_sha256")
            or meta.get("launch_receipt_sha256")
            or ""
        ),
        "request": dict(request),
        "request_sha256": request_sha256,
    }
    expected_keys = set(deterministic) | {"tasks"}

    def parse(body: bytes) -> tuple[dict[str, Any], str]:
        try:
            parsed = parse_canonical_json_bytes(body)
        except ValueError as exc:
            raise TeamError("relaunch WAL is not strict canonical JSON") from exc
        if (
            not isinstance(parsed, dict)
            or set(parsed) != expected_keys
            or any(parsed.get(k) != v for k, v in deterministic.items())
        ):
            raise TeamError(
                "pending relaunch retry intent differs from WAL request/session/base"
            )
        rows = parsed.get("tasks")
        if not isinstance(rows, list) or len(rows) != len(candidates):
            raise TeamError("relaunch WAL task plan mismatch")
        for rec, row in zip(candidates, rows, strict=True):
            if (
                not isinstance(row, Mapping)
                or set(row)
                != {
                    "task_id",
                    "relaunch_nonce",
                    "started_at",
                    "start_command_sha256",
                }
                or row.get("task_id") != rec.get("task_id")
                or not isinstance(row.get("relaunch_nonce"), str)
                or _TMUX_NONCE.fullmatch(str(row.get("relaunch_nonce"))) is None
                or not isinstance(row.get("started_at"), str)
                or not row.get("started_at")
            ):
                raise TeamError("relaunch WAL task plan mismatch")
            command = _relaunch_bootstrap_command(
                str(rec.get("pane_command") or ""),
                session_id=str(deterministic["session_id"]),
                launch_nonce=str(deterministic["launch_nonce"]),
                target_window_id=str(deterministic["target_window_id"]),
                task_id=str(rec.get("task_id") or ""),
                relaunch_nonce=str(row["relaunch_nonce"]),
            )
            if row.get("start_command_sha256") != _relaunch_start_fingerprint(command):
                raise TeamError("relaunch WAL start-command fingerprint mismatch")
        return parsed, sha256_hex(body)

    path = _scale_wal_path(root, run_id, generation)
    try:
        return parse(_read_identity_wal_bytes(path))
    except FileNotFoundError:
        rows = []
        for rec in candidates:
            nonce = secrets.token_hex(16)
            command = _relaunch_bootstrap_command(
                str(rec.get("pane_command") or ""),
                session_id=str(deterministic["session_id"]),
                launch_nonce=str(deterministic["launch_nonce"]),
                target_window_id=str(deterministic["target_window_id"]),
                task_id=str(rec.get("task_id") or ""),
                relaunch_nonce=nonce,
            )
            rows.append(
                {
                    "task_id": str(rec.get("task_id") or ""),
                    "relaunch_nonce": nonce,
                    "started_at": _utc_now(),
                    "start_command_sha256": _relaunch_start_fingerprint(command),
                }
            )
        wal = {**deterministic, "tasks": rows}
        body = canonical_json_bytes(wal)
        try:
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
        except FileExistsError:
            return parse(_read_identity_wal_bytes(path))
        except OSError as exc:
            try:
                published = _read_identity_wal_bytes(path)
            except (OSError, TeamError, ValueError):
                raise exc
            if published != body:
                raise exc
            fsync_existing_managed_dir(path.parent)
        return wal, sha256_hex(body)


def _discover_relaunch_pane(
    *,
    session: str,
    session_id: str,
    launch_nonce: str,
    target_window_id: str,
    task_id: str,
    relaunch_nonce: str,
    start_command: str,
    require_marker: bool = False,
) -> str | None:
    """Find the single exact marker or pre-marker orphan; reject collisions."""
    listed = _tmux_run(["list-panes", "-a", "-F", _TMUX_RELAUNCH_DISCOVERY_FORMAT])
    if listed.returncode != 0:
        raise TeamError("failed to enumerate relaunch panes")
    expected_hash = _relaunch_start_fingerprint(start_command)
    exact: list[str] = []
    conflicts: list[str] = []
    for line in (listed.stdout or "").splitlines():
        parts = line.split("\t", 8)
        if len(parts) != 9:
            raise TeamError("tmux relaunch discovery returned malformed identity")
        (
            sname,
            sid,
            window_id,
            pane_id,
            lnonce,
            marker_task,
            marker_nonce,
            pane_dead,
            start,
        ) = parts
        if pane_dead not in {"0", "1"}:
            raise TeamError("tmux relaunch discovery returned malformed pane state")
        if sname != session or sid != session_id:
            continue
        if lnonce != launch_nonce:
            if marker_task == task_id or marker_nonce == relaunch_nonce:
                conflicts.append(f"{pane_id}:launch={lnonce!r}")
            continue
        if window_id != target_window_id:
            if marker_task == task_id or marker_nonce == relaunch_nonce:
                conflicts.append(f"{pane_id}:window={window_id!r}")
            continue
        start_matches = f"OMG_RELAUNCH_START_SHA={expected_hash}" in start
        marker_exact = marker_task == task_id and marker_nonce == relaunch_nonce
        partial_own = (
            start_matches
            and marker_task in {"", task_id}
            and marker_nonce in {"", relaunch_nonce}
        )
        marker_write_in_progress = (
            require_marker
            and marker_task in {"", task_id}
            and marker_nonce in {"", relaunch_nonce}
            and (marker_task == task_id or marker_nonce == relaunch_nonce)
        )
        if pane_dead == "1" and (marker_exact or partial_own):
            dead_predicate = _and_tmux_formats(
                [
                    f"#{{==:#{{session_name}},{session}}}",
                    f"#{{==:#{{session_id}},{session_id}}}",
                    f"#{{==:#{{window_id}},{target_window_id}}}",
                    f"#{{==:#{{pane_id}},{pane_id}}}",
                    f"#{{==:#{{{_TMUX_LAUNCH_NONCE_OPTION}}},{launch_nonce}}}",
                    "#{==:#{pane_dead},1}",
                    f"#{{m:*OMG_RELAUNCH_START_SHA={expected_hash}*,#{{pane_start_command}}}}",
                    f"#{{||:#{{==:#{{{_TMUX_RELAUNCH_TASK_OPTION}}},}},#{{==:#{{{_TMUX_RELAUNCH_TASK_OPTION}}},{task_id}}}}}",
                    f"#{{||:#{{==:#{{{_TMUX_RELAUNCH_NONCE_OPTION}}},}},#{{==:#{{{_TMUX_RELAUNCH_NONCE_OPTION}}},{relaunch_nonce}}}}}",
                ]
            )
            killed = _tmux_run(
                [
                    "if-shell",
                    "-F",
                    "-t",
                    pane_id,
                    dead_predicate,
                    f"kill-pane -t {pane_id}",
                    "",
                ]
            )
            if killed.returncode != 0 or _tmux_pane_presence(pane_id) is not False:
                raise TeamError(
                    f"failed to remove exact dead relaunch pane task={task_id}"
                )
            continue
        if marker_exact or (partial_own and not require_marker):
            if _TMUX_PANE_ID.fullmatch(pane_id) is None:
                raise TeamError("relaunch discovery returned invalid pane id")
            exact.append(pane_id)
        elif partial_own or marker_write_in_progress:
            continue
        elif marker_task == task_id or marker_nonce == relaunch_nonce or start_matches:
            conflicts.append(
                f"{pane_id}:task={marker_task!r}:nonce={marker_nonce!r}:"
                f"start_match={start_matches}"
            )
    if conflicts or len(exact) > 1:
        raise TeamError(
            f"ambiguous/foreign relaunch pane identity task={task_id}; "
            f"conflicts={conflicts!r} exact={exact!r}"
        )
    return exact[0] if exact else None


def _resolve_relaunch_target(
    meta: Mapping[str, Any], *, session_id: str, launch_nonce: str
) -> str:
    """Resolve one exact receipt-authorized split window."""
    session = str(meta.get("session") or "")
    recorded = str(meta.get("window_id") or "")
    listed = _tmux_run(
        ["list-windows", "-t", session, "-F", _TMUX_RELAUNCH_TARGET_FORMAT]
    )
    if listed.returncode != 0:
        raise TeamError("failed to verify relaunch target window authority")
    exact: list[str] = []
    for line in (listed.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            raise TeamError("tmux relaunch target discovery malformed")
        sname, sid, window_id, nonce = parts
        if (
            sname == session
            and sid == session_id
            and nonce == launch_nonce
            and _TMUX_WINDOW_ID.fullmatch(window_id) is not None
        ):
            exact.append(window_id)
    if _TMUX_WINDOW_ID.fullmatch(recorded) is not None:
        if exact.count(recorded) != 1:
            raise TeamError("recorded relaunch target window authority mismatch")
        return recorded
    if bool(meta.get("session_owned")) and len(exact) == 1:
        return exact[0]
    raise TeamError(
        "relaunch requires one exact receipt-authorized split target window"
    )


def _wait_for_relaunch_pane(**identity: Any) -> str | None:
    """Bounded wait for bootstrap marker scheduling; never launches a retry."""
    for attempt in range(20):
        pane_id = _discover_relaunch_pane(**identity, require_marker=True)
        if pane_id is not None:
            return pane_id
        if attempt != 19:
            time.sleep(0.05)
    return None


def _read_exact_relaunch_pane(
    pane_id: str,
    *,
    session: str,
    session_id: str,
    launch_nonce: str,
    target_window_id: str,
    task_id: str,
    relaunch_nonce: str,
) -> int:
    """Read one pane's complete authority and PID in one tmux query."""
    fmt = (
        _TMUX_RELAUNCH_DISCOVERY_FORMAT
        + "\t#{pane_pid}"
    )
    shown = _tmux_run(["display-message", "-p", "-t", pane_id, "-F", fmt])
    parts = (shown.stdout or "").strip().split("\t", 9)
    if shown.returncode != 0 or len(parts) != 10:
        raise TeamError(f"relaunch pane exact readback failed task={task_id}")
    try:
        pid = int(parts[9])
    except ValueError as exc:
        raise TeamError(f"relaunch pane PID invalid task={task_id}") from exc
    if (
        parts[0] != session
        or parts[1] != session_id
        or parts[2] != target_window_id
        or parts[3] != pane_id
        or parts[4] != launch_nonce
        or parts[5] != task_id
        or parts[6] != relaunch_nonce
        or parts[7] != "0"
        or pid <= 0
    ):
        raise TeamError(f"relaunch pane authority drift task={task_id}")
    return pid


def _recover_pending_relaunch_records(
    root: Path,
    run_id: str,
    *,
    meta: Mapping[str, Any],
    authority: Mapping[str, Any],
    wal: Mapping[str, Any],
    wal_sha256: str,
    pending: tuple[dict[str, Any], str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Validate and recover receipt-bound relaunched task records."""
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    receipt, receipt_hash = pending
    intent = receipt.get("scale_intent")
    if (
        receipt.get("schema_version") != IDENTITY_RECEIPT_SCHEMA_VERSION
        or receipt.get("writer") != CLI_WRITER
        or receipt.get("run_id") != run_id
        or receipt.get("operation") != "relaunch"
        or receipt.get("generation") != wal.get("generation")
        or receipt.get("previous_receipt_sha256") != wal.get("base_receipt_sha256")
        or receipt.get("session_name") != wal.get("session_name")
        or receipt.get("session_id") != authority.get("session_id")
        or receipt.get("launch_nonce") != authority.get("launch_nonce")
        or not isinstance(intent, Mapping)
        or set(intent) != {"request_sha256", "relaunch_wal_sha256", "records"}
        or receipt.get("scale_intent_sha256")
        != sha256_hex(canonical_json_bytes(intent))
        or intent.get("request_sha256") != wal.get("request_sha256")
        or intent.get("relaunch_wal_sha256") != wal_sha256
    ):
        raise TeamError("pending relaunch receipt intent/authority mismatch")
    raw_records = intent.get("records")
    wal_rows = wal.get("tasks")
    before_rows = receipt.get("tasks_before")
    after_rows = receipt.get("tasks_after")
    base_active = _active_tasks(
        [dict(row) for row in meta.get("tasks") or [] if isinstance(row, Mapping)]
    )
    expected_before = _identity_rows(base_active)
    if (
        not isinstance(raw_records, list)
        or not isinstance(wal_rows, list)
        or len(raw_records) != len(wal_rows)
        or before_rows != expected_before
        or not isinstance(after_rows, list)
        or len(after_rows) != len(expected_before)
        or any(not isinstance(row, Mapping) for row in after_rows)
    ):
        raise TeamError("pending relaunch receipt identity continuity mismatch")
    expected_identity_keys = set(_identity_rows([{}])[0])
    before_ids = [str(row.get("task_id") or "") for row in expected_before]
    after_ids = [str(row.get("task_id") or "") for row in after_rows]
    if (
        after_ids != before_ids
        or len(set(after_ids)) != len(after_ids)
        or any(set(row) != expected_identity_keys for row in after_rows)
    ):
        raise TeamError("pending relaunch receipt identity continuity mismatch")
    records: list[dict[str, Any]] = []
    current_by_id = {
        str(row.get("task_id") or ""): row
        for row in meta.get("tasks") or []
        if isinstance(row, Mapping)
    }
    for raw, plan in zip(raw_records, wal_rows, strict=True):
        if not isinstance(raw, Mapping) or not isinstance(plan, Mapping):
            raise TeamError("pending relaunch receipt record invalid")
        record = dict(raw)
        if (
            record.get("task_id") != plan.get("task_id")
            or record.get("relaunch_nonce") != plan.get("relaunch_nonce")
            or record.get("resumed_at") != plan.get("started_at")
            or not isinstance(record.get("pane_id"), str)
            or _TMUX_PANE_ID.fullmatch(str(record.get("pane_id"))) is None
            or record.get("status") not in {STATUS_RUNNING, "launched"}
        ):
            raise TeamError("pending relaunch receipt record invalid")
        base_record = current_by_id.get(str(plan["task_id"]))
        if not isinstance(base_record, Mapping):
            raise TeamError("pending relaunch receipt base task missing")
        pane_id = str(record["pane_id"])
        live_pane = _discover_relaunch_pane(
            session=str(wal["session_name"]),
            session_id=str(wal["session_id"]),
            launch_nonce=str(wal["launch_nonce"]),
            target_window_id=str(wal["target_window_id"]),
            task_id=str(plan["task_id"]),
            relaunch_nonce=str(plan["relaunch_nonce"]),
            start_command=_relaunch_bootstrap_command(
                str(base_record.get("pane_command") or ""),
                session_id=str(wal["session_id"]),
                launch_nonce=str(wal["launch_nonce"]),
                target_window_id=str(wal["target_window_id"]),
                task_id=str(plan["task_id"]),
                relaunch_nonce=str(plan["relaunch_nonce"]),
            ),
            require_marker=True,
        )
        recorded_pid = record.get("pid")
        if not isinstance(recorded_pid, int) or isinstance(recorded_pid, bool):
            raise TeamError("pending relaunch receipt process identity drift")
        if live_pane == pane_id:
            pid = _read_exact_relaunch_pane(
                pane_id,
                session=str(wal["session_name"]),
                session_id=str(wal["session_id"]),
                launch_nonce=str(wal["launch_nonce"]),
                target_window_id=str(wal["target_window_id"]),
                task_id=str(plan["task_id"]),
                relaunch_nonce=str(plan["relaunch_nonce"]),
            )
            if (
                recorded_pid != pid
                or record.get("pgid") != _pgid_for_pid(pid)
                or record.get("pid_start") != _pid_start_identity(pid)
            ):
                raise TeamError("pending relaunch receipt process identity drift")
        elif (
            live_pane is None
            and _tmux_pane_presence(pane_id) is False
            and _pgid_for_pid(recorded_pid) is None
            and _pid_start_identity(recorded_pid) is None
        ):
            record["status"] = STATUS_NEEDS_COLLECT
        else:
            raise TeamError("pending relaunch receipt pane is absent or ambiguous")
        records.append(record)
    relaunched_by_id = {str(row["task_id"]): row for row in records}
    after_by_id = {str(row["task_id"]): dict(row) for row in after_rows}
    full_active: list[dict[str, Any]] = []
    for base in base_active:
        task_id = str(base.get("task_id") or "")
        after_identity = after_by_id[task_id]
        recovered = relaunched_by_id.get(task_id)
        if recovered is not None:
            if _identity_rows([recovered])[0] != after_identity:
                raise TeamError("pending relaunch receipt relaunched identity mismatch")
            full_active.append(dict(recovered))
            continue
        base_identity = _identity_rows([base])[0]
        if any(
            after_identity.get(key) != value
            for key, value in base_identity.items()
            if key != "window_index"
        ):
            raise TeamError("pending relaunch receipt preserved identity mismatch")
        preserved = dict(base)
        preserved["window_index"] = after_identity["window_index"]
        full_active.append(preserved)
    return records, full_active, receipt_hash


def _commit_relaunch_meta(
    root: Path,
    run_id: str,
    *,
    base_meta_generation: int,
    base_identity_generation: int,
    tasks: Sequence[Mapping[str, Any]],
    receipt_hash: str | None,
    relaunched: Sequence[Mapping[str, Any]],
    blocked: Sequence[Mapping[str, Any]],
    at: str,
) -> dict[str, Any]:
    """CAS commit relaunch state and classify publication result loss."""
    identity_generation = base_identity_generation + 1 if receipt_hash else None
    expected_by_id = {
        str(row.get("task_id") or ""): row
        for row in tasks
        if isinstance(row, Mapping)
    }
    last_relaunch = {
        "relaunched": [str(row.get("task_id") or "") for row in relaunched],
        "blocked": [str(row.get("task_id") or "") for row in blocked],
        "at": at,
    }

    def apply(current: dict[str, Any]) -> dict[str, Any]:
        if int(current.get("identity_generation", 0)) != base_identity_generation:
            raise TeamError("stale relaunch identity generation")
        out = dict(current)
        out["tasks"] = [dict(row) for row in tasks]
        out["task_count"] = len(_active_tasks(out["tasks"]))
        out["resumed_at"] = at
        out["last_relaunch"] = dict(last_relaunch)
        if identity_generation is not None:
            out["identity_generation"] = identity_generation
            out["identity_receipt_sha256"] = receipt_hash
        return out

    try:
        return mutate_team_meta(
            root,
            run_id,
            apply,
            expected_generation=base_meta_generation,
        )
    except (OSError, TeamError):
        current = load_team_meta(root, run_id)
        current_by_id = {
            str(row.get("task_id") or ""): row
            for row in current.get("tasks") or []
            if isinstance(row, Mapping)
        }
        committed = (
            (identity_generation is None or (
                current.get("identity_generation") == identity_generation
                and current.get("identity_receipt_sha256") == receipt_hash
            ))
            and current.get("last_relaunch") == last_relaunch
            and all(current_by_id.get(tid) == row for tid, row in expected_by_id.items())
        )
        if not committed:
            raise
        _load_team_identity_chain(root, run_id, current)
        return current


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
        return _relaunch_dead_incomplete_workers_locked(root_path, rid, env=env)


def _relaunch_dead_incomplete_workers_locked(
    root_path: Path,
    rid: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Body of relaunch; caller must hold :func:`acquire_scale_lock`."""
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
    from omg_cli.team.tmux import TmuxTeamError, respawn_worker_pane

    meta = _require_team_run(root_path, rid)
    pending_operation = pending_identity_wal_operation(root_path, rid, meta)
    if pending_operation is None:
        pending_receipts = _load_future_identity_receipts(
            root_path,
            rid,
            committed_generation=int(meta.get("identity_generation", 0)),
        )
        if pending_receipts:
            raise TeamError(
                "relaunch refused while an identity receipt is pending; "
                "retry the original identity operation"
            )
    if pending_operation == "add":
        raise TeamError(
            "relaunch refused while add WAL is pending; retry original scale --add"
        )
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

    pending_task_ids: list[str] | None = None
    if pending_operation == "relaunch":
        from omg_cli.contracts.writer_chain import parse_canonical_json_bytes

        wal_path = _scale_wal_path(
            root_path, rid, int(meta.get("identity_generation", 0)) + 1
        )
        try:
            pending_wal = parse_canonical_json_bytes(_read_identity_wal_bytes(wal_path))
        except (OSError, TeamError, ValueError) as exc:
            raise TeamError("pending relaunch WAL cannot reconstruct request") from exc
        request = pending_wal.get("request") if isinstance(pending_wal, Mapping) else None
        request_tasks = request.get("tasks") if isinstance(request, Mapping) else None
        if not isinstance(request_tasks, list) or not request_tasks:
            raise TeamError("pending relaunch WAL request task plan invalid")
        pending_task_ids = []
        for row in request_tasks:
            task_id = row.get("task_id") if isinstance(row, Mapping) else None
            if not isinstance(task_id, str) or not task_id or task_id in pending_task_ids:
                raise TeamError("pending relaunch WAL request task plan invalid")
            pending_task_ids.append(task_id)

    candidates: list[dict[str, Any]] = []
    relaunch_expected_session_id: str | None = None
    relaunch_launch_nonce: str | None = None
    receipt_path = team_launch_receipt_path(root_path, rid)
    if receipt_path.is_file():
        try:
            launch_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            launch_receipt = None
        if isinstance(launch_receipt, dict):
            if isinstance(launch_receipt.get("session_id"), str):
                relaunch_expected_session_id = str(launch_receipt["session_id"])
            if isinstance(launch_receipt.get("launch_nonce"), str):
                relaunch_launch_nonce = str(launch_receipt["launch_nonce"])
    if relaunch_launch_nonce is None:
        raw_nonce = meta.get("launch_nonce")
        relaunch_launch_nonce = raw_nonce if isinstance(raw_nonce, str) else None

    for rec in tasks_all:
        tid = str(rec.get("task_id") or "")
        if pending_task_ids is not None:
            if tid in pending_task_ids:
                candidates.append(rec)
            else:
                skipped.append({"task_id": tid, "reason": "not_in_pending_relaunch"})
            continue
        status = str(rec.get("status") or "")
        if status in TERMINAL_PANE_STATUSES or status == STATUS_SCALED_DOWN:
            skipped.append({"task_id": tid, "reason": "terminal_or_scaled_down"})
            continue
        pane_id = rec.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            skipped.append({"task_id": tid, "reason": "no_pane_id"})
            continue
        if not tmux_available():
            skipped.append({"task_id": tid, "reason": "tmux_unavailable"})
            continue
        expected_pid = rec.get("pid")
        expected_pid_i = (
            expected_pid
            if isinstance(expected_pid, int) and not isinstance(expected_pid, bool)
            else None
        )
        expected_start = rec.get("pid_start")
        expected_start_s = expected_start if isinstance(expected_start, str) else None
        # Exact identity only — bare pane_alive would skip relaunch for a
        # respawned %id hosting a foreign process.
        if _status_worker_alive(
            pane_id=pane_id,
            session=session,
            expected_session_id=relaunch_expected_session_id,
            launch_nonce=relaunch_launch_nonce,
            expected_pid_start=expected_start_s,
            expected_pid=expected_pid_i,
        ):
            skipped.append({"task_id": tid, "reason": "alive"})
            continue
        # Dead pane or identity drift (respawn / reuse).
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

    if pending_task_ids is not None and [str(r.get("task_id")) for r in candidates] != pending_task_ids:
        raise TeamError("pending relaunch WAL tasks differ from base team meta")

    for rec in candidates:
        task_id = str(rec.get("task_id") or "")
        try:
            validate_task_worktree(
                root_path,
                rid,
                task_id,
                path=Path(str(rec.get("worktree") or "")),
            )
        except WorkerError as exc:
            raise TeamError(
                f"relaunch worktree authority invalid task={task_id}"
            ) from exc

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
    relaunch_expected_nonce = authority.get("launch_nonce")
    relaunch_window_id = meta.get("window_id")
    relaunch_window_id = (
        str(relaunch_window_id) if isinstance(relaunch_window_id, str) else None
    )
    if not isinstance(relaunch_expected_nonce, str) or not _tmux_launch_authority_matches(
        session,
        expected_nonce=relaunch_expected_nonce,
        session_owned=bool(meta.get("session_owned", True)),
        window_id=relaunch_window_id,
    ):
        raise TeamError("live tmux launch nonce mismatch; refuse relaunch")

    if meta.get("topology") != "split":
        raise TeamError(
            "relaunch requires receipt-bound split topology"
        )
    target = _resolve_relaunch_target(
        meta,
        session_id=str(authority["session_id"]),
        launch_nonce=str(authority["launch_nonce"]),
    )
    tasks_before = _active_tasks(tasks_all)
    to_relaunch: list[dict[str, Any]] = []

    for rec in candidates:
        tid = str(rec.get("task_id") or "")
        wt = Path(str(rec.get("worktree") or ""))
        if pending_operation != "relaunch" and _worktree_dirty(wt):
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

    if not to_relaunch:
        relaunch_at = _utc_now()
        _commit_relaunch_meta(
            root_path,
            rid,
            base_meta_generation=int(meta.get("meta_generation", 0)),
            base_identity_generation=int(meta.get("identity_generation", 0)),
            tasks=tasks_all,
            receipt_hash=None,
            relaunched=[],
            blocked=blocked,
            at=relaunch_at,
        )
        return {
            "writer": CLI_WRITER,
            "run_id": rid,
            "relaunched": [],
            "blocked": blocked,
            "skipped": skipped,
            "identity_generation": int(meta.get("identity_generation") or 0),
            "verified": False,
            "note": "no clean dead incomplete workers to relaunch",
        }

    request = _relaunch_request_payload(
        meta, to_relaunch, target_window_id=target
    )
    request_sha256 = sha256_hex(canonical_json_bytes(request))
    wal, wal_sha256 = _load_or_publish_relaunch_wal(
        root_path,
        rid,
        meta=meta,
        authority=authority,
        request=request,
        request_sha256=request_sha256,
        candidates=to_relaunch,
    )
    generation = int(wal["generation"])
    pending_receipt = _load_optional_pending_scale_receipt(root_path, rid, generation)
    receipt_hash: str
    if pending_receipt is not None:
        recovered, recovered_active, receipt_hash = _recover_pending_relaunch_records(
            root_path,
            rid,
            meta=meta,
            authority=authority,
            wal=wal,
            wal_sha256=wal_sha256,
            pending=pending_receipt,
        )
        recovered_by_id = {str(row["task_id"]): row for row in recovered}
        recovered_active_by_id = {
            str(row["task_id"]): row for row in recovered_active
        }
        tasks_all = [
            dict(recovered_active_by_id.get(str(row.get("task_id") or ""), row))
            for row in tasks_all
        ]
        for old in to_relaunch:
            rec = recovered_by_id[str(old["task_id"])]
            relaunched.append(
                {
                    "task_id": rec["task_id"],
                    "from_pane_id": old.get("pane_id"),
                    "pane_id": rec["pane_id"],
                    "pid": rec.get("pid"),
                }
            )
    else:
        plan_by_id = {
            str(row["task_id"]): row
            for row in wal.get("tasks") or []
            if isinstance(row, Mapping)
        }
        session_id = str(authority["session_id"])
        launch_nonce = str(authority["launch_nonce"])
        for rec in to_relaunch:
            tid = str(rec["task_id"])
            plan = plan_by_id[tid]
            relaunch_nonce = str(plan["relaunch_nonce"])
            start_command = _relaunch_bootstrap_command(
                str(rec["pane_command"]),
                session_id=session_id,
                launch_nonce=launch_nonce,
                target_window_id=target,
                task_id=tid,
                relaunch_nonce=relaunch_nonce,
            )
            adopted = _discover_relaunch_pane(
                session=session,
                session_id=session_id,
                launch_nonce=launch_nonce,
                target_window_id=target,
                task_id=tid,
                relaunch_nonce=relaunch_nonce,
                start_command=start_command,
            )
            if adopted is None:
                env_pairs = _pane_env_pairs(
                    run_id=rid,
                    team_id=team_id,
                    worker_id=tid,
                    leader_root=root_path,
                    state_root=root_path / ".omg" / "state",
                    owner_token=str(owner_token) if owner_token else None,
                )
                try:
                    adopted = respawn_worker_pane(
                        target=target,
                        worktree=str(rec["worktree"]),
                        pane_command=start_command,
                        env_pairs=env_pairs,
                    )
                except TmuxTeamError as exc:
                    adopted = _wait_for_relaunch_pane(
                        session=session,
                        session_id=session_id,
                        launch_nonce=launch_nonce,
                        target_window_id=target,
                        task_id=tid,
                        relaunch_nonce=relaunch_nonce,
                        start_command=start_command,
                    )
                    if adopted is None:
                        raise TeamError(
                            f"failed to relaunch worker {tid!r}: {exc}"
                        ) from exc
            # Require post-bootstrap marker readback before binding the pane.
            confirmed = _wait_for_relaunch_pane(
                session=session,
                session_id=session_id,
                launch_nonce=launch_nonce,
                target_window_id=target,
                task_id=tid,
                relaunch_nonce=relaunch_nonce,
                start_command=start_command,
            )
            if confirmed != adopted:
                raise TeamError(f"relaunch pane marker readback failed task={tid}")
            old_pane = rec.get("pane_id")
            pid = _read_exact_relaunch_pane(
                adopted,
                session=session,
                session_id=session_id,
                launch_nonce=launch_nonce,
                target_window_id=target,
                task_id=tid,
                relaunch_nonce=relaunch_nonce,
            )
            rec["pane_id"] = adopted
            rec["pid"] = pid
            rec["pgid"] = _pgid_for_pid(pid)
            rec["pid_start"] = _pid_start_identity(pid)
            rec["status"] = (
                STATUS_RUNNING
                if rec["pgid"] is not None and rec["pid_start"] is not None
                else "launched"
            )
            if rec["status"] != STATUS_RUNNING:
                raise TeamError(f"relaunch pane process binding incomplete task={tid}")
            rec["resumed_at"] = str(plan["started_at"])
            rec["relaunch_nonce"] = relaunch_nonce
            rec["status_before_resume"] = STATUS_NEEDS_COLLECT
            rec.pop("resume_block_reason", None)
            rec.pop("resume_blocked_at", None)
            relaunched.append(
                {
                    "task_id": tid,
                    "from_pane_id": old_pane,
                    "pane_id": adopted,
                    "pid": rec.get("pid"),
                }
            )

        _resync_window_indices(session, tasks_all)
        intent = {
            "request_sha256": request_sha256,
            "relaunch_wal_sha256": wal_sha256,
            "records": [
                dict(next(row for row in tasks_all if row.get("task_id") == item["task_id"]))
                for item in relaunched
            ],
        }
        _receipt, receipt_hash = _persist_team_identity_receipt(
            root_path,
            rid,
            session=session,
            session_id=session_id,
            launch_nonce=launch_nonce,
            generation=generation,
            previous_receipt_sha256=str(wal["base_receipt_sha256"]),
            operation="relaunch",
            tasks_before=tasks_before,
            tasks_after=_active_tasks(tasks_all),
            scale_intent=intent,
        )

    candidate_meta = dict(meta)
    candidate_meta["tasks"] = [dict(row) for row in tasks_all]
    candidate_meta["identity_generation"] = generation
    candidate_meta["identity_receipt_sha256"] = receipt_hash
    _load_team_identity_chain(root_path, rid, candidate_meta)

    relaunch_at = str((wal.get("tasks") or [{}])[0].get("started_at") or _utc_now())
    _commit_relaunch_meta(
        root_path,
        rid,
        base_meta_generation=int(meta.get("meta_generation", 0)),
        base_identity_generation=int(meta.get("identity_generation", 0)),
        tasks=tasks_all,
        receipt_hash=receipt_hash,
        relaunched=relaunched,
        blocked=blocked,
        at=relaunch_at,
    )

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
    "pending_identity_wal_operation",
    "scale_lock_path",
    "scale_team",
]
