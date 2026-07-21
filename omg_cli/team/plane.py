"""Experimental tmux team plane (D1 grok-only + D3 multi-CLI routing).

Gate
----
``OMG_EXPERIMENTAL_TMUX_TEAM=1`` required. Isolation is **integration** isolation
(worktree ownership + seal + integrate), **not** an execution sandbox.

Zero-config (no ``routing``) preserves D1: all panes are grok via
``build_grok_argv`` / ``build_pane_command``. With ``routing``, D3 resolves
role→provider once (floors in :mod:`omg_cli.team.routing`) and builds
per-provider argv via :func:`omg_cli.team.providers.build_executor_argv`.

Lifecycle (mirrors process fanout's dry-run / PID contract with tmux):
  start  → create_run + ownership manifest + prepare worktrees + tmux session
  status → pure read (team.json + ownership + optional pane liveness)
  collect → seal_all_tasks + integrate_results (never sets verified)
  stop   → kill recorded session + killpg recorded pgids only (no pkill -f)

Dry-run never calls ``tmux_available()`` or ``subprocess`` — writes team.json
with ``pid=None`` / ``status=dry_run`` (parity with fanout). Multi-CLI dry-run
still records the would-be per-provider argv (and ``needs_pty``).
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence

from omg_cli.evidence import CLI_WRITER, safe_supervised_child_env
from omg_cli.fanout import max_workers_cap
from omg_cli.madmax import (
    build_pane_command,
    forwarded_env,
    session_name_for_cwd,
    tmux_available,
    tmux_env_args,
)
from omg_cli.modes import (
    HARD_RULES_REMINDER,
    _materialize_prompt_file,
    build_grok_argv,
    plugin_root,
)
from omg_cli.state import (
    _run_dir,
    create_run,
    load_active_run,
    load_run,
    write_status,
)
from omg_cli.team.providers import build_executor_argv
from omg_cli.team.roles import normalize_role
from omg_cli.team.routing import (
    ResolvedRouting,
    RoutingError,
    resolve_routing,
)
from omg_cli.workers import (
    WorkerError,
    build_ownership_manifest,
    load_ownership_manifest,
    ownership_manifest_path,
    prepare_owned_tasks,
    seal_all_tasks,
    worktree_dir,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPERIMENTAL_ENV = "OMG_EXPERIMENTAL_TMUX_TEAM"
# Markers injected into worker panes / process-fanout children so nested
# supervisors refuse (depth-1 — a worker must not launch a team).
WORKER_ENV_MARKERS: tuple[str, ...] = (
    "OMG_TEAM_WORKER",
    "OMG_PROCESS_FANOUT_WORKER",
    "OMG_SPAWNED_WORKER",
)
TEAM_WORKER_ENV = "OMG_TEAM_WORKER"
WORKSPACE_MODE = "worktree"
SCHEMA_VERSION = 1

# Locked status field set (freeze for --json consumers / tests).
STATUS_TOP_KEYS: tuple[str, ...] = (
    "run_id",
    "session",
    "dry_run",
    "workspace_mode",
    "tasks",
)
STATUS_TASK_KEYS: tuple[str, ...] = (
    "task_id",
    "window_index",
    "worktree",
    "status",
    "alive",
)


class TeamError(RuntimeError):
    """User-facing team plane error (maps to exit 1)."""


class TeamGateError(TeamError):
    """Policy / experimental gate failure (maps to exit 2)."""


# ---------------------------------------------------------------------------
# Paths / gates
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy_env(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def experimental_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return _truthy_env(source.get(EXPERIMENTAL_ENV))


def in_spawned_worker_context(env: Mapping[str, str] | None = None) -> bool:
    """True when this process is a depth-1 worker (must not re-launch team).

    Reuses the same marker family process fanout / team panes inject into
    child environments (``OMG_*_WORKER``). Prompt-only bans are insufficient.
    """
    source = env if env is not None else os.environ
    for key in WORKER_ENV_MARKERS:
        if _truthy_env(source.get(key)):
            return True
    return False


def team_dir(root: Path | str, run_id: str) -> Path:
    return _run_dir(Path(root), run_id) / "team"


def team_meta_path(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / "team.json"


def _require_cli_writer(data: Mapping[str, Any], *, label: str) -> None:
    if data.get("writer") != CLI_WRITER:
        raise TeamError(
            f"{label} lacks CLI writer authority "
            f"(writer={data.get('writer')!r}; expected {CLI_WRITER!r})"
        )


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(dict(data), indent=2, ensure_ascii=False, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_team_meta(root: Path | str, run_id: str) -> dict[str, Any]:
    path = team_meta_path(root, run_id)
    if not path.is_file():
        raise TeamError(f"team.json missing for run {run_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TeamError(f"team.json unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise TeamError("team.json must be a JSON object")
    _require_cli_writer(data, label="team.json")
    return data


def _parse_tasks_json(tasks_json: str | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(tasks_json, str):
        try:
            raw = json.loads(tasks_json)
        except json.JSONDecodeError as exc:
            raise TeamError(f"--tasks-json is not valid JSON: {exc}") from exc
    else:
        raw = list(tasks_json)
    if not isinstance(raw, list):
        raise TeamError("--tasks-json must be a JSON array")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise TeamError("each task must be a JSON object")
        out.append(dict(item))
    return out


def _assert_start_gates(
    tasks: Sequence[Mapping[str, Any]],
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Return task count after cap check; raise TeamGateError on refuse."""
    if not experimental_enabled(env):
        raise TeamGateError(
            f"omg team start is experimental and disabled by default.\n"
            f"  Set {EXPERIMENTAL_ENV}=1 to opt in.\n"
            f"  Isolation is worktree ownership + seal/integrate "
            f"(not an execution sandbox). Multi-CLI panes require explicit "
            f"role routing; zero-config remains grok-only."
        )
    if in_spawned_worker_context(env):
        raise TeamGateError(
            "omg team start refused: already inside a spawned-worker context "
            f"(depth-1; one of {', '.join(WORKER_ENV_MARKERS)} is set). "
            "Workers must not launch a team."
        )
    n = len(tasks)
    if n < 1:
        raise TeamError("at least one task is required")
    cap = max_workers_cap()
    if n > cap:
        raise TeamGateError(
            f"tasks={n} exceeds hard cap {cap} "
            f"(OMG_MAX_WORKERS / max_workers_cap)"
        )
    return n


# ---------------------------------------------------------------------------
# Prompt / argv (grok-only)
# ---------------------------------------------------------------------------


def build_team_task_prompt(
    goal: str,
    *,
    run_id: str,
    task_id: str,
    task_index: int,
    task_count: int,
    owned_files: Sequence[str],
    worktree: Path | str,
    provider: str = "grok",
    role: str = "executor",
    posture: str | None = None,
) -> str:
    """Task-scoped prompt for a team pane (grok or multi-CLI)."""
    from omg_cli.modes import load_skill_body

    skill = load_skill_body("ulw", root=plugin_root())
    owned = "\n".join(f"- `{f}`" for f in owned_files) or "- (none listed)"
    mode_label = (
        "experimental grok-only tmux plane"
        if provider == "grok"
        else "experimental multi-CLI tmux team plane"
    )
    lines = [
        skill,
        "",
        HARD_RULES_REMINDER,
        "",
        f"## Active mode: team ({mode_label})",
        f"## Run id: {run_id}",
        f"## Task: {task_id} ({task_index}/{task_count})",
        f"## Role: {role}",
        f"## Provider: {provider}",
        f"## Worktree: {worktree}",
    ]
    if posture:
        lines.append(f"## Posture: {posture}")
    lines.extend(
        [
            "",
            "## Team-plane contract (CLI supervisor)",
            f"- You are **one** {provider} pane worker in an experimental tmux team session.",
            "- Own **only** the files listed below; do not edit outside ownership.",
            "- Work **inside this worktree**.",
            "- Do **not** invoke `omg team start` or other multi-worker supervisors.",
            "- Do **not** set verified / passes in `.omg/state/` — only omg CLI does.",
            "- After edits, leave the tree dirty; the leader runs "
            "`omg team collect` (seal + integrate).",
            "- Isolation is **integration** isolation (ownership + seal), "
            "not an execution sandbox.",
            "",
            "## Owned files",
            owned,
            "",
            "## Goal (shared)",
            goal.strip() or "(no goal provided)",
            "",
            f"Task index {task_index} of {task_count}. Coordinate via artifacts only.",
        ]
    )
    return "\n".join(lines)


def build_executor_pane_command(
    argv: Sequence[str],
    *,
    needs_pty: bool = False,
    shell: str | None = None,
    da1_drain: bool = True,
) -> str:
    """Login-shell wrapped pane command for any executor argv.

    Unlike :func:`omg_cli.madmax.build_pane_command` (grok-only), this keeps the
    full argv (binary included). When *needs_pty* is True (agy), the binary is
    launched under ``pty.spawn`` so headless/non-TTY output is not dropped
    (ref agy-pty.py).
    """
    shell = shell or os.environ.get("SHELL") or "/bin/zsh"
    drain = (
        "perl -e 'use POSIX; tcflush(0, TCIFLUSH)' 2>/dev/null; "
        if da1_drain
        else ""
    )
    argv_list = [str(x) for x in argv]
    if needs_pty:
        # pty.spawn child gets a real pty (agy issue #76); argv via JSON
        # avoids shell-quoting the full command body twice.
        payload = json.dumps(argv_list, ensure_ascii=False)
        py = (
            "import json,pty,sys;"
            " argv=json.loads(sys.argv[1]);"
            " rc=pty.spawn(argv);"
            " sys.exit(0 if rc in (0, None) else int(rc or 1))"
        )
        inner_body = (
            f"sleep 0.2; {drain}"
            f"exec python3 -c {shlex.quote(py)} {shlex.quote(payload)}"
        )
    else:
        inner_body = f"sleep 0.2; {drain}exec {shlex.join(argv_list)}"
    return f"exec {shlex.quote(shell)} -lc {shlex.quote(inner_body)}"


def _task_role(task: Mapping[str, Any]) -> str:
    """Role for a task dict; default ``executor`` (D1 zero-config posture)."""
    raw = task.get("role")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "executor"
    return normalize_role(str(raw))


def _build_task_grok_argv(
    *,
    goal: str,
    run_id: str,
    task_id: str,
    task_index: int,
    task_count: int,
    owned_files: Sequence[str],
    worktree: Path,
    yolo: bool = False,
    safe: bool = False,
    extra: Sequence[str] | None = None,
) -> list[str]:
    prompt = build_team_task_prompt(
        goal,
        run_id=run_id,
        task_id=task_id,
        task_index=task_index,
        task_count=task_count,
        owned_files=owned_files,
        worktree=worktree,
    )
    argv = build_grok_argv(
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
    task_prompt_dir = worktree / ".omg" / "team-prompt"
    # Prefer materializing under run-scoped team dir when worktree may be mkdir-only;
    # still use worktree-local dir so --cwd isolation stays clear.
    task_prompt_dir.mkdir(parents=True, exist_ok=True)
    argv = _materialize_prompt_file(argv, task_prompt_dir)
    # Also stash prompt for operators
    (task_prompt_dir / f"{task_id}.prompt.md").write_text(prompt, encoding="utf-8")
    return argv


def _grok_args_for_pane(argv: Sequence[str]) -> list[str]:
    """Strip leading ``grok`` token — ``build_pane_command`` re-adds it."""
    if argv and argv[0] == "grok":
        return list(argv[1:])
    return list(argv)


def _materialize_task_prompt(
    *,
    goal: str,
    run_id: str,
    task_id: str,
    task_index: int,
    task_count: int,
    owned_files: Sequence[str],
    worktree: Path,
    provider: str,
    role: str,
    posture: str | None,
) -> Path:
    """Write prompt under worktree and return its path."""
    prompt = build_team_task_prompt(
        goal,
        run_id=run_id,
        task_id=task_id,
        task_index=task_index,
        task_count=task_count,
        owned_files=owned_files,
        worktree=worktree,
        provider=provider,
        role=role,
        posture=posture,
    )
    task_prompt_dir = worktree / ".omg" / "team-prompt"
    task_prompt_dir.mkdir(parents=True, exist_ok=True)
    path = task_prompt_dir / f"{task_id}.prompt.md"
    path.write_text(prompt, encoding="utf-8")
    return path


def _pane_env_pairs() -> list[tuple[str, str]]:
    """Allowlisted env + worker depth marker (secrets via -e, never pane argv)."""
    pairs = list(forwarded_env())
    # Strip lifecycle escape hatches, then force team-worker marker.
    scrubbed = safe_supervised_child_env({k: v for k, v in pairs})
    out = [(k, v) for k, v in scrubbed.items()]
    # Ensure marker wins even if parent had a falsey value.
    out = [(k, v) for k, v in out if k not in WORKER_ENV_MARKERS]
    out.append((TEAM_WORKER_ENV, "1"))
    out.sort(key=lambda kv: kv[0])
    return out


# ---------------------------------------------------------------------------
# tmux helpers (live path only — never called from dry_run)
# ---------------------------------------------------------------------------


def _tmux_run(args: Sequence[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _create_tmux_session(
    *,
    session: str,
    tasks: list[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
) -> None:
    """Create one session with one window per task (live path)."""
    if not tmux_available():
        raise TeamError(
            "tmux is required for omg team start (non-dry-run).\n"
            "  Install: brew install tmux\n"
            "  Or use --dry-run to write team.json without launching."
        )
    env_args = tmux_env_args(env_pairs)
    if not tasks:
        raise TeamError("no tasks for tmux session")

    first = tasks[0]
    create = _tmux_run(
        [
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            str(first["task_id"]),
            "-c",
            str(first["worktree"]),
            *env_args,
            str(first["pane_command"]),
        ]
    )
    if create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip()
        raise TeamError(
            f"failed to create tmux session {session!r} "
            f"(exit {create.returncode}): {err}"
        )

    for task in tasks[1:]:
        nw = _tmux_run(
            [
                "new-window",
                "-t",
                session,
                "-n",
                str(task["task_id"]),
                "-c",
                str(task["worktree"]),
                str(task["pane_command"]),
            ]
        )
        if nw.returncode != 0:
            err = (nw.stderr or nw.stdout or "").strip()
            # Best-effort cleanup of half-created session
            _tmux_run(["kill-session", "-t", session])
            raise TeamError(
                f"failed to create window for task {task['task_id']!r}: {err}"
            )

    _tmux_run(["set-option", "-t", session, "mouse", "on"])


def _list_pane_pids(session: str) -> dict[int, int]:
    """Map window_index → pane_pid for *session* (best-effort)."""
    r = _tmux_run(
        [
            "list-panes",
            "-s",
            "-t",
            session,
            "-F",
            "#{window_index} #{pane_pid}",
        ]
    )
    out: dict[int, int] = {}
    if r.returncode != 0:
        return out
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        try:
            widx = int(parts[0])
            pid = int(parts[1])
        except ValueError:
            continue
        out[widx] = pid
    return out


def _session_alive(session: str) -> bool:
    if not tmux_available():
        return False
    r = _tmux_run(["has-session", "-t", session])
    return r.returncode == 0


def _window_alive(session: str, window_index: int) -> bool | None:
    """True/False when tmux available; None when tmux unavailable."""
    if not tmux_available():
        return None
    if not _session_alive(session):
        return False
    r = _tmux_run(
        [
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_index}",
        ]
    )
    if r.returncode != 0:
        return False
    indices = set()
    for line in (r.stdout or "").splitlines():
        try:
            indices.add(int(line.strip()))
        except ValueError:
            continue
    return window_index in indices


def _pgid_for_pid(pid: int) -> int | None:
    if os.name != "posix":
        return pid
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return pid


# ---------------------------------------------------------------------------
# start / status / collect / stop
# ---------------------------------------------------------------------------


def start_team(
    goal: str,
    tasks_json: str | Sequence[Mapping[str, Any]],
    *,
    root: Path | str | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    yolo: bool = False,
    safe: bool = False,
    force: bool = False,
    extra: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    routing: Mapping[str, Any] | None = None,
    available_providers: Collection[str] | None = None,
    check_binary: bool = True,
) -> dict[str, Any]:
    """Create ownership + worktrees + team.json (+ live tmux unless dry_run).

    Parameters
    ----------
    routing:
        Optional role→``{provider, model?}`` map. When **omitted / None**,
        behavior matches D1 exactly (all grok panes via ``build_grok_argv``).
        When provided, D3 floors apply and per-provider argv is recorded.
    available_providers:
        Optional hermetic provider set for routing binary checks (tests).
    check_binary:
        When False, skip PATH probes (still apply FLOOR 1/2/3).

    Returns the written team.json payload.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    goal = (goal or "").strip() or "(no goal)"
    tasks = _parse_tasks_json(tasks_json)
    n = _assert_start_gates(tasks, env=env)

    multi_cli = routing is not None
    resolved: ResolvedRouting | None = None
    if multi_cli:
        # Roles from task dicts (default executor) + explicit routing keys.
        roles_needed = [_task_role(t) for t in tasks]
        try:
            resolved = resolve_routing(
                routing,
                roles_needed=roles_needed,
                available_providers=available_providers,
                check_binary=check_binary,
            )
        except RoutingError as exc:
            raise TeamError(str(exc)) from exc
        # UnknownRoleError propagates (FLOOR 2) — do not swallow.

    # Resolve / create run
    if run_id:
        if load_run(root_path, run_id) is None:
            raise TeamError(f"no run found for --run {run_id!r}")
        rid = run_id
    else:
        note = (
            "experimental multi-CLI tmux team plane "
            f"(gate {EXPERIMENTAL_ENV}=1); integration isolation only"
            if multi_cli
            else (
                "experimental grok-only tmux team plane "
                f"(gate {EXPERIMENTAL_ENV}=1); multi-CLI via --routing"
            )
        )
        create_extra: dict[str, Any] = {
            "team": True,
            "workspace_mode": WORKSPACE_MODE,
            "task_count": n,
            "note": note,
            "multi_cli": multi_cli,
        }
        try:
            from omg_cli.integrate import git_rev_parse_head

            base_sha = git_rev_parse_head(root_path)
            if base_sha:
                create_extra["base_sha"] = base_sha
        except Exception:
            pass
        try:
            run = create_run(
                root_path,
                mode="ulw",
                goal=goal,
                extra=create_extra,
                force=force,
            )
        except RuntimeError as exc:
            raise TeamError(str(exc)) from exc
        rid = str(run["run_id"])

    # Ownership + real worktrees (filesystem; dry_run still prepares)
    try:
        manifest = build_ownership_manifest(root_path, rid, tasks)
        prepare_owned_tasks(root_path, rid)
    except WorkerError as exc:
        raise TeamError(str(exc)) from exc

    tdir = team_dir(root_path, rid)
    tdir.mkdir(parents=True, exist_ok=True)

    session = session_name_for_cwd(root_path)
    env_pairs = _pane_env_pairs()

    # Original task dicts by task_id (for role lookup; manifest may drop fields).
    tasks_by_id: dict[str, dict[str, Any]] = {}
    for t in tasks:
        tid0 = str(t.get("task_id") or t.get("id") or "")
        if tid0:
            tasks_by_id[tid0] = t

    task_records: list[dict[str, Any]] = []
    manifest_tasks = list(manifest.get("tasks") or [])
    # Preserve manifest order for window indices
    for i, mtask in enumerate(manifest_tasks):
        tid = str(mtask["task_id"])
        wt = Path(str(mtask.get("worktree_path") or worktree_dir(root_path, rid, tid)))
        owned = list(mtask.get("owned_files") or [])
        src_task = tasks_by_id.get(tid) or mtask
        role = _task_role(src_task)

        if multi_cli and resolved is not None:
            route = resolved.for_role(role)
            prompt_path = _materialize_task_prompt(
                goal=goal,
                run_id=rid,
                task_id=tid,
                task_index=i + 1,
                task_count=n,
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
                check_binary=False,  # already checked at resolve
            )
            argv = list(inv.argv)
            needs_pty = bool(inv.needs_pty)
            provider = inv.provider
            posture = inv.posture
            pane_cmd = build_executor_pane_command(argv, needs_pty=needs_pty)
        else:
            # D1 zero-config path — identical to pre-D3 behavior.
            argv = _build_task_grok_argv(
                goal=goal,
                run_id=rid,
                task_id=tid,
                task_index=i + 1,
                task_count=n,
                owned_files=owned,
                worktree=wt,
                yolo=yolo,
                safe=safe,
                extra=extra,
            )
            needs_pty = False
            provider = "grok"
            posture = "read-write"  # executor default; D1 does not route roles
            pane_cmd = build_pane_command(_grok_args_for_pane(argv))

        # Persist per-task argv under team/ (mirrors fanout workers/*.argv.json)
        argv_path = tdir / f"{tid}.argv.json"
        argv_path.write_text(
            json.dumps(argv, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        rec: dict[str, Any] = {
            "task_id": tid,
            "window_index": i,
            "worktree": str(wt),
            "argv_path": str(argv_path.relative_to(_run_dir(root_path, rid))),
            "pane_command": pane_cmd,
            "argv": argv,
            "role": role,
            "provider": provider,
            "posture": posture,
            "needs_pty": needs_pty,
            "pid": None,
            "pgid": None,
            "status": "dry_run" if dry_run else "pending",
        }
        task_records.append(rec)

    routing_payload = resolved.to_dict() if resolved is not None else None

    if dry_run:
        # HERMETIC: never call tmux_available() / subprocess
        note = (
            "dry_run skeleton; pid=None; no tmux/subprocess; "
            + (
                "multi-CLI per-provider argv recorded"
                if multi_cli
                else "grok-only pane argv recorded"
            )
        )
        meta = {
            "writer": CLI_WRITER,
            "schema_version": SCHEMA_VERSION,
            "run_id": rid,
            "session": session,
            "dry_run": True,
            "workspace_mode": WORKSPACE_MODE,
            "goal": goal,
            "task_count": n,
            "created_at": _utc_now(),
            "tasks": task_records,
            "multi_cli": multi_cli,
            "routing": routing_payload,
            "note": note,
        }
        _atomic_write_json(team_meta_path(root_path, rid), meta)
        write_status(
            root_path,
            rid,
            "completed",
            extra={
                "team": True,
                "stage": "team_dry_run",
                "task_count": n,
                "multi_cli": multi_cli,
                "note": "team dry_run completed; verified remains false",
            },
        )
        return meta

    # Live path: create tmux session + fill pids
    try:
        _create_tmux_session(
            session=session,
            tasks=task_records,
            env_pairs=env_pairs,
        )
    except TeamError:
        raise
    except OSError as exc:
        raise TeamError(f"tmux launch failed: {exc}") from exc

    pane_pids = _list_pane_pids(session)
    for rec in task_records:
        widx = int(rec["window_index"])
        pid = pane_pids.get(widx)
        if pid is not None:
            rec["pid"] = pid
            rec["pgid"] = _pgid_for_pid(pid)
            rec["status"] = "running"
        else:
            rec["status"] = "launched"  # session created; pid unknown

    meta = {
        "writer": CLI_WRITER,
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "session": session,
        "dry_run": False,
        "workspace_mode": WORKSPACE_MODE,
        "goal": goal,
        "task_count": n,
        "created_at": _utc_now(),
        "tasks": task_records,
        "multi_cli": multi_cli,
        "routing": routing_payload,
        "note": (
            "experimental multi-CLI tmux team; stop via recorded session/pgids only"
            if multi_cli
            else "experimental grok-only tmux team; stop via recorded session/pgids only"
        ),
    }
    _atomic_write_json(team_meta_path(root_path, rid), meta)
    write_status(
        root_path,
        rid,
        "running",
        extra={
            "team": True,
            "stage": "team_running",
            "session": session,
            "task_count": n,
            "multi_cli": multi_cli,
        },
    )
    return meta


def team_status(
    root: Path | str | None = None,
    run_id: str | None = None,
    *,
    probe_tmux: bool = True,
) -> dict[str, Any]:
    """Pure READ status with LOCKED field set. Never writes state."""
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    if not run_id:
        active = load_active_run(root_path)
        if active is None:
            raise TeamError("no active run (pass --run ID)")
        run_id = str(active["run_id"])

    meta = load_team_meta(root_path, run_id)
    session = str(meta.get("session") or "")
    dry = bool(meta.get("dry_run"))
    workspace_mode = str(meta.get("workspace_mode") or WORKSPACE_MODE)

    # Optional ownership presence (read-only; ignore missing)
    ownership_present = ownership_manifest_path(root_path, run_id).is_file()
    if ownership_present:
        try:
            load_ownership_manifest(root_path, run_id)
        except WorkerError:
            ownership_present = False

    tasks_out: list[dict[str, Any]] = []
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        tid = str(raw.get("task_id") or "")
        widx = int(raw.get("window_index") or 0)
        wt = str(raw.get("worktree") or "")
        st = str(raw.get("status") or "unknown")
        # dry_run / never-launched panes are not live
        if dry or st == "dry_run" or raw.get("pid") is None and dry:
            alive = False
        elif not probe_tmux:
            alive = False
        else:
            win = _window_alive(session, widx)
            alive = bool(win) if win is not None else False
        tasks_out.append(
            {
                "task_id": tid,
                "window_index": widx,
                "worktree": wt,
                "status": st,
                "alive": alive,
            }
        )

    # LOCKED top-level keys only (plus no extras for --json freeze)
    locked = {
        "run_id": run_id,
        "session": session,
        "dry_run": dry,
        "workspace_mode": workspace_mode,
        "tasks": tasks_out,
    }
    # Sanity: exact key set
    assert set(locked.keys()) == set(STATUS_TOP_KEYS)
    for t in tasks_out:
        assert set(t.keys()) == set(STATUS_TASK_KEYS)
    # Attach ownership_present as non-locked diagnostic only when human path
    # needs it — keep locked payload pure; callers may ignore extras via keys.
    locked_with_diag = dict(locked)
    locked_with_diag["_ownership_present"] = ownership_present
    return locked_with_diag


def status_locked_view(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the LOCKED field set from a status payload."""
    tasks = []
    for t in status.get("tasks") or []:
        if not isinstance(t, Mapping):
            continue
        tasks.append({k: t.get(k) for k in STATUS_TASK_KEYS})
    return {
        "run_id": status.get("run_id"),
        "session": status.get("session"),
        "dry_run": status.get("dry_run"),
        "workspace_mode": status.get("workspace_mode"),
        "tasks": tasks,
    }


def collect_team(
    root: Path | str | None = None,
    run_id: str | None = None,
    *,
    force_seal: bool = False,
    skip_preflight: bool = False,
    require_squash: bool = False,
) -> dict[str, Any]:
    """Thin wrapper: seal_all_tasks then integrate_results. Never sets verified."""
    from omg_cli.integrate import integrate_results

    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    if not run_id:
        active = load_active_run(root_path)
        if active is None:
            raise TeamError("no active run (pass --run ID)")
        run_id = str(active["run_id"])

    # Require CLI-stamped team.json so forged {verified:true} team files
    # cannot be used as a collect authority signal.
    load_team_meta(root_path, run_id)

    try:
        seal_results = seal_all_tasks(root_path, run_id, force=force_seal)
    except WorkerError as exc:
        raise TeamError(f"seal failed: {exc}") from exc

    try:
        integrate = integrate_results(
            root_path,
            run_id,
            skip_preflight=skip_preflight,
            require_squash=require_squash,
        )
    except Exception as exc:
        raise TeamError(f"integrate failed: {exc}") from exc

    # Explicit: never touch verified / passes
    run = load_run(root_path, run_id) or {}
    out = {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "seal": seal_results,
        "integrate": integrate,
        "verified": bool(run.get("verified")),
        "note": "collect never sets verified; use omg accept after green evidence",
    }
    return out


def stop_team(
    root: Path | str | None = None,
    run_id: str | None = None,
    *,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Kill recorded tmux session + killpg recorded pgids only.

    Never uses self-matching ``pkill -f`` / ``pgrep -f``. Dry-run entries
    (``pid is None``) are never signalled.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    if not run_id:
        active = load_active_run(root_path)
        if active is None:
            raise TeamError("no active run (pass --run ID)")
        run_id = str(active["run_id"])

    meta = load_team_meta(root_path, run_id)
    session = str(meta.get("session") or "")
    dry = bool(meta.get("dry_run"))
    actions: list[str] = []
    errors: list[str] = []

    # 1) tmux kill-session (best-effort; skip when dry_run skeleton)
    if session and not dry:
        try:
            if tmux_available():
                r = _tmux_run(["kill-session", "-t", session])
                if r.returncode == 0:
                    actions.append(f"tmux kill-session -t {session}")
                else:
                    # already gone is fine
                    actions.append(
                        f"tmux kill-session -t {session} "
                        f"(exit {r.returncode}; best-effort)"
                    )
            else:
                actions.append("tmux unavailable; skipped kill-session")
        except OSError as exc:
            errors.append(f"tmux kill-session: {exc}")
    elif dry:
        actions.append("dry_run: skipped tmux kill-session")

    # 2) killpg only recorded pgids with real pids
    signalled: list[dict[str, Any]] = []
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        pid = raw.get("pid")
        pgid = raw.get("pgid")
        tid = raw.get("task_id")
        if pid is None:
            # dry_run / never launched — never signal
            continue
        target: int | None = None
        if isinstance(pgid, int) and pgid > 0:
            target = pgid
        elif isinstance(pid, int) and pid > 0:
            target = pid
        if target is None:
            continue
        try:
            if os.name == "posix":
                os.killpg(target, signal.SIGTERM)
                actions.append(f"killpg:SIGTERM pgid={target} task={tid}")
                if kill_grace_s and kill_grace_s > 0:
                    import time

                    time.sleep(float(kill_grace_s))
                    try:
                        os.killpg(target, signal.SIGKILL)
                        actions.append(f"killpg:SIGKILL pgid={target} task={tid}")
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            else:
                os.kill(int(pid), signal.SIGTERM)
                actions.append(f"kill:SIGTERM pid={pid} task={tid}")
            signalled.append({"task_id": tid, "pgid": target, "pid": pid})
        except (ProcessLookupError, PermissionError, OSError) as exc:
            errors.append(f"signal task={tid} target={target}: {exc}")

    # Update team.json status (CLI write only)
    updated = dict(meta)
    updated["stopped_at"] = _utc_now()
    updated["stop_actions"] = actions
    for rec in updated.get("tasks") or []:
        if isinstance(rec, dict) and rec.get("status") not in ("dry_run",):
            rec["status"] = "stopped"
    _atomic_write_json(team_meta_path(root_path, run_id), updated)

    try:
        write_status(
            root_path,
            run_id,
            "cancelled",
            extra={
                "team": True,
                "stage": "team_stopped",
                "session": session,
                "note": "team stop: session + recorded pgids only; no pkill -f",
            },
        )
    except Exception as exc:
        errors.append(f"write_status: {exc}")

    return {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "session": session,
        "dry_run": dry,
        "actions": actions,
        "signalled": signalled,
        "errors": errors,
        "note": "stop uses only recorded session name + pgids; no pkill -f",
    }


def format_status_table(status: Mapping[str, Any]) -> str:
    lines = [
        f"run_id:         {status.get('run_id')}",
        f"session:        {status.get('session')}",
        f"dry_run:        {status.get('dry_run')}",
        f"workspace_mode: {status.get('workspace_mode')}",
        "",
        f"{'task_id':<20} {'win':>4} {'alive':<6} {'status':<12} worktree",
        "-" * 72,
    ]
    for t in status.get("tasks") or []:
        lines.append(
            f"{str(t.get('task_id') or ''):<20} "
            f"{int(t.get('window_index') or 0):>4} "
            f"{str(bool(t.get('alive'))):<6} "
            f"{str(t.get('status') or ''):<12} "
            f"{t.get('worktree') or ''}"
        )
    return "\n".join(lines)


__all__ = [
    "CLI_WRITER",
    "EXPERIMENTAL_ENV",
    "STATUS_TASK_KEYS",
    "STATUS_TOP_KEYS",
    "TEAM_WORKER_ENV",
    "TeamError",
    "TeamGateError",
    "WORKER_ENV_MARKERS",
    "WORKSPACE_MODE",
    "build_executor_pane_command",
    "build_team_task_prompt",
    "collect_team",
    "experimental_enabled",
    "format_status_table",
    "in_spawned_worker_context",
    "load_team_meta",
    "start_team",
    "status_locked_view",
    "stop_team",
    "team_dir",
    "team_meta_path",
    "team_status",
]
