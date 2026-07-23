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
  stop   → signal only nonce-bound immutable launch identities (no pkill -f)

Dry-run never calls ``tmux_available()`` or ``subprocess`` — writes team.json
with ``pid=None`` / ``status=dry_run`` (parity with fanout). Multi-CLI dry-run
still records the would-be per-provider argv, ``needs_pty``, and
``prompt_delivery``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import stat
import subprocess
import uuid
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
from omg_cli.team.providers import (
    PROMPT_DELIVERY_POSITIONAL_TEXT,
    PROMPT_DELIVERY_PROMPT_FILE,
    PROMPT_DELIVERY_STDIN,
    PromptDelivery,
    build_executor_argv,
)
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
LAUNCH_RECEIPT_SCHEMA_VERSION = 1
LAUNCH_NONCE_OPTION = "@omg_launch_nonce"
_TMUX_SESSION_ID = re.compile(r"^\$[0-9]{1,16}$")
_TMUX_PANE_ID = re.compile(r"^%[0-9]{1,16}$")

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


def team_launch_receipt_path(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / "launch-receipt.json"


def team_identity_receipt_path(root: Path | str, run_id: str, generation: int) -> Path:
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise TeamError("team identity receipt generation must be non-negative")
    return team_dir(root, run_id) / "identity-receipts" / f"{generation:08d}.json"


def _require_cli_writer(data: Mapping[str, Any], *, label: str) -> None:
    if data.get("writer") != CLI_WRITER:
        raise TeamError(
            f"{label} lacks CLI writer authority "
            f"(writer={data.get('writer')!r}; expected {CLI_WRITER!r})"
        )


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes,
    )

    body = (
        json.dumps(dict(data), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TeamError(f"secure team.json publication refused: {exc}") from exc


def load_team_meta(root: Path | str, run_id: str) -> dict[str, Any]:
    path = team_meta_path(root, run_id)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise TeamError(f"team.json missing for run {run_id}")
    except OSError as exc:
        raise TeamError(f"team.json secure open refused: {exc}") from exc
    from omg_cli.contracts.path_keys import DATA_FILE_MODE

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TeamError("team.json must be a regular non-symlink file")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
            descriptor = -1
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TeamError(f"team.json unreadable: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(data, dict):
        raise TeamError("team.json must be a JSON object")
    _require_cli_writer(data, label="team.json")
    if stat.S_IMODE(info.st_mode) != DATA_FILE_MODE:
        raise TeamError(
            f"team.json mode must be {DATA_FILE_MODE:04o}, "
            f"got {stat.S_IMODE(info.st_mode):04o}"
        )
    return data


def _parse_tasks_json(
    tasks_json: str | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
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
            f"tasks={n} exceeds hard cap {cap} (OMG_MAX_WORKERS / max_workers_cap)"
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


def _resolve_prompt_body(
    argv: Sequence[str],
    *,
    prompt_file: Path | str,
) -> list[str]:
    """Replace *prompt_file* path placeholders in *argv* with the file body."""
    path = Path(prompt_file)
    pf = str(path)
    # Also match resolved path forms (build may store absolute or relative).
    candidates = {pf}
    try:
        candidates.add(str(path.resolve()))
    except OSError:
        pass
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TeamError(f"cannot read prompt file for pane delivery: {exc}") from exc
    return [body if tok in candidates else tok for tok in argv]


def build_executor_pane_command(
    argv: Sequence[str],
    *,
    needs_pty: bool = False,
    shell: str | None = None,
    da1_drain: bool = True,
    prompt_delivery: PromptDelivery | str = PROMPT_DELIVERY_PROMPT_FILE,
    prompt_file: Path | str | None = None,
) -> str:
    """Login-shell wrapped pane command for any executor argv.

    Unlike :func:`omg_cli.madmax.build_pane_command` (grok-only), this keeps the
    full argv (binary included). When *needs_pty* is True (agy), the binary is
    launched under ``pty.spawn`` so headless/non-TTY output is not dropped
    (ref agy-pty.py).

    Prompt delivery (provider-aware; see :class:`ExecutorInvocation.prompt_delivery`):
    - ``prompt-file``: argv already contains ``--prompt-file <path>``; exec as-is.
    - ``stdin``: redirect materialized prompt into the process stdin
      (``exec … - < path``) so codex's trailing ``-`` sentinel is fed.
    - ``positional-text``: read prompt file body and substitute path placeholders
      in argv (cursor trailing positional; agy/gemini ``-p`` value).
    """
    shell = shell or os.environ.get("SHELL") or "/bin/zsh"
    drain = (
        "perl -e 'use POSIX; tcflush(0, TCIFLUSH)' 2>/dev/null; " if da1_drain else ""
    )
    delivery = str(prompt_delivery or PROMPT_DELIVERY_PROMPT_FILE)
    argv_list = [str(x) for x in argv]

    if delivery == PROMPT_DELIVERY_POSITIONAL_TEXT:
        if prompt_file is None:
            raise TeamError(
                "positional-text prompt delivery requires prompt_file "
                "(path of materialized task prompt)"
            )
        argv_list = _resolve_prompt_body(argv_list, prompt_file=prompt_file)
    elif delivery == PROMPT_DELIVERY_STDIN:
        if prompt_file is None:
            raise TeamError(
                "stdin prompt delivery requires prompt_file "
                "(redirect source for codex trailing '-')"
            )
    elif delivery != PROMPT_DELIVERY_PROMPT_FILE:
        raise TeamError(f"unknown prompt_delivery mode: {delivery!r}")

    stdin_redirect = ""
    if delivery == PROMPT_DELIVERY_STDIN:
        # Inner shell redirect only — body stays out of ps-visible argv.
        stdin_redirect = f" < {shlex.quote(str(prompt_file))}"

    if needs_pty:
        # pty.spawn child gets a real pty (agy issue #76); argv via JSON
        # avoids shell-quoting the full command body twice.
        # stdin redirect does not apply under pty.spawn (agy uses positional-text).
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
            f"{stdin_redirect}"
        )
    else:
        inner_body = f"sleep 0.2; {drain}exec {shlex.join(argv_list)}{stdin_redirect}"
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


def _tmux_run(
    args: Sequence[str], *, check: bool = False
) -> subprocess.CompletedProcess[str]:
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
) -> tuple[str, str]:
    """Create one session and return its exact tmux name/ID handle."""
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
            "-P",
            "-F",
            "#{session_name}\t#{session_id}",
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
    parts = (create.stdout or "").strip().split("\t")
    if (
        len(parts) != 2
        or parts[0] != session
        or _TMUX_SESSION_ID.fullmatch(parts[1]) is None
    ):
        # A successful non-attached ``new-session`` created the requested name.
        # A pre-existing name would have made ``new-session`` fail, so this
        # requested name is still confined to the just-created transaction.
        cleanup_error = _cleanup_created_tmux_session((session, session))
        message = "tmux create did not return an exact session handle"
        if cleanup_error:
            message += f"; {cleanup_error}"
        raise TeamError(message)
    handle = (parts[0], parts[1])

    try:
        for task in tasks[1:]:
            nw = _tmux_run(
                [
                    "new-window",
                    "-t",
                    handle[1],
                    "-n",
                    str(task["task_id"]),
                    "-c",
                    str(task["worktree"]),
                    str(task["pane_command"]),
                ]
            )
            if nw.returncode != 0:
                err = (nw.stderr or nw.stdout or "").strip()
                raise TeamError(
                    f"failed to create window for task {task['task_id']!r}: {err}"
                )

        option = _tmux_run(["set-option", "-t", handle[1], "mouse", "on"])
        if option.returncode != 0:
            raise TeamError("failed to configure created tmux session")
    except (TeamError, OSError) as exc:
        cleanup_error = _cleanup_created_tmux_session(handle)
        if cleanup_error:
            raise TeamError(f"{exc}; {cleanup_error}") from exc
        raise
    return handle


def _cleanup_created_tmux_session(handle: tuple[str, str]) -> str | None:
    """Kill only the immutable ID returned by ``tmux new-session`` and verify."""
    _session_name, session_id = handle
    try:
        _tmux_run(["kill-session", "-t", session_id])
        probe = _tmux_run(["has-session", "-t", session_id])
    except OSError as exc:
        return f"created tmux session cleanup failed: {exc}"
    if probe.returncode != 1:
        return "created tmux session cleanup could not verify disappearance"
    return None


def _list_pane_identities(session: str) -> dict[int, tuple[str, int]]:
    """Map window index to exact tmux pane identity and pane PID."""
    r = _tmux_run(
        [
            "list-panes",
            "-s",
            "-t",
            session,
            "-F",
            "#{window_index}\t#{pane_id}\t#{pane_pid}",
        ]
    )
    if r.returncode != 0:
        return {}
    out: dict[int, tuple[str, int]] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3 or _TMUX_PANE_ID.fullmatch(parts[1]) is None:
            continue
        try:
            window_index = int(parts[0])
            pane_pid = int(parts[2])
        except ValueError:
            continue
        if window_index in out or pane_pid <= 0:
            return {}
        out[window_index] = (parts[1], pane_pid)
    return out


def _list_pane_pids(session: str) -> dict[int, int]:
    """Compatibility view used by dynamic scaling; not process authority."""
    return {
        window_index: pane_pid
        for window_index, (_pane_id, pane_pid) in _list_pane_identities(session).items()
    }


def _read_tmux_session_identity(session: str) -> tuple[str, str] | None:
    r = _tmux_run(
        ["display-message", "-p", "-t", session, "#{session_name}\t#{session_id}"]
    )
    if r.returncode != 0:
        return None
    parts = (r.stdout or "").strip().split("\t")
    if (
        len(parts) != 2
        or parts[0] != session
        or _TMUX_SESSION_ID.fullmatch(parts[1]) is None
    ):
        return None
    return parts[0], parts[1]


def _read_tmux_launch_nonce(session: str) -> str | None:
    r = _tmux_run(["show-options", "-v", "-t", session, LAUNCH_NONCE_OPTION])
    if r.returncode != 0:
        return None
    value = (r.stdout or "").strip()
    if len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value):
        return None
    return value


def _persist_team_launch_receipt(
    root: Path,
    run_id: str,
    *,
    session: str,
    session_id: str,
    launch_nonce: str,
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Persist the immutable process identity used by ``team stop``."""
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    rows: list[dict[str, Any]] = []
    for raw in tasks:
        rows.append(
            {
                "task_id": raw.get("task_id"),
                "window_index": raw.get("window_index"),
                "pane_id": raw.get("pane_id"),
                "pid": raw.get("pid"),
                "pgid": raw.get("pgid"),
                "pid_start": raw.get("pid_start"),
            }
        )
    receipt = {
        "store_kind": "team_launch_receipt",
        "schema_version": LAUNCH_RECEIPT_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "session_name": session,
        "session_id": session_id,
        "launch_nonce": launch_nonce,
        "generation": 0,
        "previous_receipt_sha256": None,
        "tasks": rows,
    }
    body = canonical_json_bytes(receipt)
    path = team_launch_receipt_path(root, run_id)
    try:
        atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
    except FileExistsError as exc:
        raise TeamError("immutable team launch receipt already exists") from exc
    return receipt, sha256_hex(body)


def _snapshot_live_start_files(
    paths: Sequence[Path],
) -> dict[Path, tuple[bytes | None, int | None]]:
    snapshots: dict[Path, tuple[bytes | None, int | None]] = {}
    for path in paths:
        if path.is_symlink():
            raise TeamError(f"live start transaction path may not be a symlink: {path}")
        if not path.exists():
            snapshots[path] = (None, None)
            continue
        if not path.is_file():
            raise TeamError(f"live start transaction path must be a file: {path}")
        snapshots[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return snapshots


def _restore_live_start_files(
    snapshots: Mapping[Path, tuple[bytes | None, int | None]],
) -> list[str]:
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes

    errors: list[str] = []
    for path, (body, mode) in snapshots.items():
        try:
            if body is None:
                if path.is_dir() and not path.is_symlink():
                    raise OSError(
                        f"partial transaction path became a directory: {path}"
                    )
                path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(path, body, mode=mode or DATA_FILE_MODE)
        except (OSError, ValueError) as exc:
            errors.append(f"restore {path}: {exc}")
    return errors


def _load_team_launch_receipt(
    root: Path, run_id: str, meta: Mapping[str, Any]
) -> dict[str, Any]:
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    path = team_launch_receipt_path(root, run_id)
    if not path.is_file() or path.is_symlink():
        raise TeamError("immutable team launch receipt missing")
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise TeamError("team launch receipt must be an object")
    required = {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "session_name",
        "session_id",
        "launch_nonce",
        "generation",
        "previous_receipt_sha256",
        "tasks",
    }
    if set(parsed) != required:
        raise TeamError("team launch receipt keys mismatch")
    if (
        parsed["store_kind"] != "team_launch_receipt"
        or parsed["schema_version"] != LAUNCH_RECEIPT_SCHEMA_VERSION
        or parsed["writer"] != CLI_WRITER
        or parsed["run_id"] != run_id
        or parsed["session_name"] != meta.get("session")
        or parsed["launch_nonce"] != meta.get("launch_nonce")
        or parsed["generation"] != 0
        or parsed["previous_receipt_sha256"] is not None
        or _TMUX_SESSION_ID.fullmatch(str(parsed["session_id"])) is None
        or not isinstance(parsed["tasks"], list)
    ):
        raise TeamError("team launch receipt identity mismatch")
    body_hash = sha256_hex(canonical_json_bytes(parsed))
    if body_hash != meta.get("launch_receipt_sha256"):
        raise TeamError("team launch receipt hash mismatch")
    expected_tasks = meta.get("tasks")
    generation = meta.get("identity_generation", 0)
    if not isinstance(expected_tasks, list):
        raise TeamError("team launch receipt task count mismatch")
    if generation == 0 and len(expected_tasks) != len(parsed["tasks"]):
        raise TeamError("team launch receipt task count mismatch")
    for expected, actual in zip(expected_tasks, parsed["tasks"]):
        if not isinstance(actual, Mapping):
            raise TeamError("team launch receipt task row mismatch")
        if set(actual) != {
            "task_id",
            "window_index",
            "pane_id",
            "pid",
            "pgid",
            "pid_start",
        }:
            raise TeamError("team launch receipt task keys mismatch")
        if generation == 0 and (
            not isinstance(expected, Mapping)
            or any(expected.get(field) != actual.get(field) for field in actual)
        ):
            raise TeamError("team.json differs from immutable launch receipt")
    return parsed


def _identity_rows(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "task_id": raw.get("task_id"),
            "window_index": raw.get("window_index"),
            "pane_id": raw.get("pane_id"),
            "pid": raw.get("pid"),
            "pgid": raw.get("pgid"),
            "pid_start": raw.get("pid_start"),
        }
        for raw in tasks
    ]


def _persist_team_identity_receipt(
    root: Path,
    run_id: str,
    *,
    session: str,
    session_id: str,
    launch_nonce: str,
    generation: int,
    previous_receipt_sha256: str,
    operation: str,
    tasks_before: Sequence[Mapping[str, Any]],
    tasks_after: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Append one immutable scale generation to the launch identity chain."""
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.state_schemas import require_sha256
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    if generation <= 0:
        raise TeamError("scaled identity generation must be positive")
    require_sha256(previous_receipt_sha256, label="previous_receipt_sha256")
    if operation not in {"add", "remove"}:
        raise TeamError("scaled identity receipt operation mismatch")
    receipt = {
        "store_kind": "team_identity_receipt",
        "schema_version": LAUNCH_RECEIPT_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "session_name": session,
        "session_id": session_id,
        "launch_nonce": launch_nonce,
        "generation": generation,
        "previous_receipt_sha256": previous_receipt_sha256,
        "operation": operation,
        "receipt_nonce": uuid.uuid4().hex,
        "tasks_before": _identity_rows(tasks_before),
        "tasks_after": _identity_rows(tasks_after),
    }
    body = canonical_json_bytes(receipt)
    receipt_path = team_identity_receipt_path(root, run_id, generation)
    try:
        atomic_write_bytes(
            receipt_path,
            body,
            mode=DATA_FILE_MODE,
            replace=False,
        )
    except FileExistsError as exc:
        adopted = _adopt_aborted_identity_receipt(receipt_path, receipt)
        if adopted is None:
            raise TeamError(
                "immutable team identity generation already exists"
            ) from exc
        return adopted
    return receipt, sha256_hex(body)


def _adopt_aborted_identity_receipt(
    path: Path, intended: Mapping[str, Any]
) -> tuple[dict[str, Any], str] | None:
    """Adopt the orphaned intent receipt of an identical aborted scale attempt.

    A scale attempt persists its intent receipt before signalling; a signalling
    failure aborts before the meta commit, leaving the immutable receipt behind
    while ``identity_generation`` stays unchanged, so the retry recomputes the
    same generation and would otherwise wedge on the existing file forever.
    Every field except the per-attempt ``receipt_nonce`` is deterministic from
    the unchanged team state, so exact equality on all other fields proves the
    orphan is this writer's own aborted intent; the retry resumes it verbatim.
    Any other content stays a hard conflict.
    """
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    try:
        if path.is_symlink() or not path.is_file():
            return None
        parsed = parse_canonical_json_bytes(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != set(intended):
        return None
    for key, value in intended.items():
        if key == "receipt_nonce":
            continue
        if parsed.get(key) != value:
            return None
    nonce = parsed.get("receipt_nonce")
    if not isinstance(nonce, str) or len(nonce) != 32:
        return None
    return parsed, sha256_hex(canonical_json_bytes(parsed))


def _load_team_identity_chain(
    root: Path, run_id: str, meta: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Validate every generation and return the complete append-only chain."""
    from omg_cli.contracts.state_schemas import require_sha256
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    launch = _load_team_launch_receipt(root, run_id, meta)
    chain = [launch]
    previous_rows = launch["tasks"]
    previous_hash = str(meta.get("launch_receipt_sha256") or "")
    require_sha256(previous_hash, label="launch_receipt_sha256")
    generation = meta.get("identity_generation", 0)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise TeamError("team identity generation is invalid")
    for expected_generation in range(1, generation + 1):
        path = team_identity_receipt_path(root, run_id, expected_generation)
        if path.is_symlink() or not path.is_file():
            raise TeamError(
                f"team identity receipt generation {expected_generation} missing"
            )
        parsed = parse_canonical_json_bytes(path.read_bytes())
        if not isinstance(parsed, dict):
            raise TeamError("team identity receipt must be an object")
        required = {
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
        }
        if set(parsed) != required:
            raise TeamError("team identity receipt keys mismatch")
        if (
            parsed["store_kind"] != "team_identity_receipt"
            or parsed["schema_version"] != LAUNCH_RECEIPT_SCHEMA_VERSION
            or parsed["writer"] != CLI_WRITER
            or parsed["run_id"] != run_id
            or parsed["session_name"] != launch["session_name"]
            or parsed["session_id"] != launch["session_id"]
            or parsed["launch_nonce"] != launch["launch_nonce"]
            or parsed["generation"] != expected_generation
            or parsed["previous_receipt_sha256"] != previous_hash
            or parsed["operation"] not in {"add", "remove"}
            or not isinstance(parsed["receipt_nonce"], str)
            or len(parsed["receipt_nonce"]) != 32
            or not isinstance(parsed["tasks_before"], list)
            or not isinstance(parsed["tasks_after"], list)
        ):
            raise TeamError("team identity receipt chain mismatch")
        for field in ("tasks_before", "tasks_after"):
            for row in parsed[field]:
                if not isinstance(row, Mapping) or set(row) != {
                    "task_id",
                    "window_index",
                    "pane_id",
                    "pid",
                    "pgid",
                    "pid_start",
                }:
                    raise TeamError("team identity receipt task row mismatch")
        if parsed["tasks_before"] != previous_rows:
            raise TeamError("team identity receipt task continuity mismatch")
        previous_hash = sha256_hex(canonical_json_bytes(parsed))
        previous_rows = parsed["tasks_after"]
        chain.append(parsed)
    expected_hash = meta.get(
        "identity_receipt_sha256", meta.get("launch_receipt_sha256")
    )
    if previous_hash != expected_hash:
        raise TeamError("team identity receipt chain head mismatch")
    expected_active = [
        task
        for task in meta.get("tasks") or []
        if isinstance(task, Mapping) and task.get("status") != "scaled_down"
    ]
    latest_rows = launch["tasks"] if generation == 0 else chain[-1]["tasks_after"]
    if _identity_rows(expected_active) != latest_rows:
        raise TeamError("team.json active identities differ from receipt chain")
    return chain


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
        return None


def _pid_start_identity(pid: int) -> str | None:
    """Return an OS start identity that changes when a PID is reused."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    if raw:
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        if close >= 0 and len(fields) > 19:
            return f"proc:{fields[19]}"
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join((result.stdout or "").split())
    return f"ps:{value}" if result.returncode == 0 and value else None


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
            prompt_delivery = inv.prompt_delivery
            pane_cmd = build_executor_pane_command(
                argv,
                needs_pty=needs_pty,
                prompt_delivery=prompt_delivery,
                prompt_file=prompt_path,
            )
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
            prompt_delivery = PROMPT_DELIVERY_PROMPT_FILE
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
            "prompt_delivery": prompt_delivery,
            "pid": None,
            "pgid": None,
            "pid_start": None,
            "status": "dry_run" if dry_run else "pending",
        }
        task_records.append(rec)

    routing_payload = resolved.to_dict() if resolved is not None else None

    if dry_run:
        # HERMETIC: never call tmux_available() / subprocess
        note = "dry_run skeleton; pid=None; no tmux/subprocess; " + (
            "multi-CLI per-provider argv recorded"
            if multi_cli
            else "grok-only pane argv recorded"
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
            "next_worker_index": n,
            "created_at": _utc_now(),
            "tasks": task_records,
            "multi_cli": multi_cli,
            "routing": routing_payload,
            "linked_ralph": None,
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
    launch_nonce = uuid.uuid4().hex
    transaction_paths = (
        team_launch_receipt_path(root_path, rid),
        team_meta_path(root_path, rid),
        _run_dir(root_path, rid) / "status.json",
    )
    snapshots = _snapshot_live_start_files(transaction_paths)
    created_handle: tuple[str, str] | None = None
    try:
        created_handle = _create_tmux_session(
            session=session,
            tasks=task_records,
            env_pairs=env_pairs,
        )
        option = _tmux_run(
            [
                "set-option",
                "-t",
                created_handle[1],
                LAUNCH_NONCE_OPTION,
                launch_nonce,
            ]
        )
        if option.returncode != 0:
            raise TeamError("failed to bind tmux launch nonce")

        session_identity = _read_tmux_session_identity(session)
        pane_identities = _list_pane_identities(created_handle[1])
        if session_identity != created_handle or len(pane_identities) != len(
            task_records
        ):
            raise TeamError("tmux launch identity readback failed")
        for rec in task_records:
            widx = int(rec["window_index"])
            pane_identity = pane_identities.get(widx)
            if pane_identity is not None:
                pane_id, pid = pane_identity
                rec["pane_id"] = pane_id
                rec["pid"] = pid
                rec["pgid"] = _pgid_for_pid(pid)
                rec["pid_start"] = _pid_start_identity(pid)
                rec["status"] = (
                    "running"
                    if rec["pgid"] is not None and rec["pid_start"] is not None
                    else "launched"
                )
            else:
                rec["status"] = "launched"  # session created; pid unknown

        _receipt, launch_receipt_sha256 = _persist_team_launch_receipt(
            root_path,
            rid,
            session=session,
            session_id=created_handle[1],
            launch_nonce=launch_nonce,
            tasks=task_records,
        )

        meta = {
            "writer": CLI_WRITER,
            "schema_version": SCHEMA_VERSION,
            "run_id": rid,
            "session": session,
            "launch_nonce": launch_nonce,
            "launch_receipt_sha256": launch_receipt_sha256,
            "identity_generation": 0,
            "identity_receipt_sha256": launch_receipt_sha256,
            "dry_run": False,
            "workspace_mode": WORKSPACE_MODE,
            "goal": goal,
            "task_count": n,
            "next_worker_index": n,
            "created_at": _utc_now(),
            "tasks": task_records,
            "multi_cli": multi_cli,
            "routing": routing_payload,
            "linked_ralph": None,
            "note": (
                "experimental multi-CLI tmux team; stop via immutable launch identity"
                if multi_cli
                else "experimental grok-only tmux team; stop via immutable launch identity"
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
    except Exception as exc:
        cleanup_error = (
            _cleanup_created_tmux_session(created_handle)
            if created_handle is not None
            else None
        )
        restore_errors = _restore_live_start_files(snapshots)
        details = [str(exc)]
        if cleanup_error:
            details.append(cleanup_error)
        details.extend(restore_errors)
        raise TeamError(
            "tmux live start transaction failed: " + "; ".join(details)
        ) from exc


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


def _confined_team_ralph_state_path(root: Path, run_id: str) -> Path:
    """Return the canonical Ralph state path after rejecting symlink components."""
    from omg_cli.contracts.path_keys import ContractPathError, confined_path
    from omg_cli.team.pipeline import team_ralph_state_path

    expected = team_ralph_state_path(root, run_id)
    try:
        confined = confined_path(
            root,
            ".omg",
            "state",
            "runs",
            run_id,
            "stages",
            "team-ralph.json",
        )
    except ContractPathError as exc:
        raise TeamError(f"linked Ralph path is not confined: {exc}") from exc
    if confined != expected:
        raise TeamError("linked Ralph canonical path mismatch")
    return expected


def _load_linked_ralph_state(
    root: Path,
    run_id: str,
    *,
    linked_ralph: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Read only the exact, confined, non-symlink Ralph state for this run."""
    from omg_cli.team.pipeline import team_pipeline_state_path

    expected = _confined_team_ralph_state_path(root, run_id)
    stored_path = linked_ralph.get("path")
    if not isinstance(stored_path, str) or stored_path != str(expected):
        raise TeamError("linked Ralph stored path does not match canonical run path")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(expected, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > 1024 * 1024:
            raise TeamError("linked Ralph state must be a bounded regular file")
        body = os.read(descriptor, opened.st_size + 1)
        if len(body) != opened.st_size:
            raise TeamError("linked Ralph state changed while reading")
    finally:
        os.close(descriptor)

    current = os.lstat(expected)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise TeamError("linked Ralph path identity changed while reading")

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TeamError(f"linked Ralph state is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TeamError("linked Ralph state must be a JSON object")
    linked_team = parsed.get("linked_team")
    if (
        parsed.get("writer") != CLI_WRITER
        or parsed.get("schema_version") != SCHEMA_VERSION
        or parsed.get("run_id") != run_id
        or parsed.get("mode") != "team-ralph"
        or not isinstance(parsed.get("status"), str)
        or not isinstance(linked_team, Mapping)
        or linked_team.get("run_id") != run_id
        or linked_team.get("team_meta") != str(team_meta_path(root, run_id))
        or linked_team.get("pipeline") != str(team_pipeline_state_path(root, run_id))
    ):
        raise TeamError("linked Ralph state schema or writer identity mismatch")
    return expected, parsed


def _write_confined_linked_ralph_state(
    root: Path,
    run_id: str,
    expected: Path,
    data: Mapping[str, Any],
) -> None:
    """Atomically update the canonical Ralph state without following symlinks."""
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes

    if _confined_team_ralph_state_path(root, run_id) != expected:
        raise TeamError("linked Ralph path changed before write")
    body = (
        json.dumps(dict(data), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(expected, body, mode=DATA_FILE_MODE)
    except ValueError as exc:
        raise TeamError(f"linked Ralph write refused: {exc}") from exc


def _live_signal_target_matches(
    session: str,
    receipt: Mapping[str, Any],
    row: Mapping[str, Any],
) -> bool:
    """Revalidate exact tmux and OS process identity immediately before signal."""
    window_index = row.get("window_index")
    pane_id = row.get("pane_id")
    pid = row.get("pid")
    pgid = row.get("pgid")
    pid_start = row.get("pid_start")
    if (
        isinstance(window_index, bool)
        or not isinstance(window_index, int)
        or not isinstance(pane_id, str)
        or _TMUX_PANE_ID.fullmatch(pane_id) is None
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(pgid, bool)
        or not isinstance(pgid, int)
        or pgid <= 0
        or not isinstance(pid_start, str)
        or not pid_start
    ):
        return False
    if _read_tmux_session_identity(session) != (session, receipt.get("session_id")):
        return False
    if _read_tmux_launch_nonce(session) != receipt.get("launch_nonce"):
        return False
    if _list_pane_identities(session).get(window_index) != (pane_id, pid):
        return False
    return _pgid_for_pid(pid) == pgid and _pid_start_identity(pid) == pid_start


def _process_group_disappeared(pgid: int) -> tuple[bool, str | None]:
    """Probe the entire exact process group without delivering a signal."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True, None
    except PermissionError:
        # EPERM proves neither absence nor a fatal probe failure.  macOS can
        # report it briefly while a just-killed process group is being reaped;
        # keep the bounded disappearance poll running and require a later
        # ESRCH before claiming the whole group is gone.
        return False, None
    except OSError as exc:
        return False, f"process group disappearance probe failed pgid={pgid}: {exc}"
    return False, None


def _receipt_leader_pgid(
    pid: int,
) -> tuple[int | None, str | None]:
    """Read the receipted leader PGID without conflating errors with absence."""
    if os.name != "posix":
        return pid, None
    try:
        return os.getpgid(pid), None
    except ProcessLookupError:
        return None, None
    except (PermissionError, OSError) as exc:
        return None, f"leader identity probe failed pid={pid}: {exc}"


def _wait_process_group_disappearance(
    pgid: int,
    *,
    timeout_s: float = 1.0,
) -> tuple[bool, str | None]:
    """Bounded poll proving no member remains in the receipted process group."""
    import time

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        gone, error = _process_group_disappeared(pgid)
        if gone or error is not None:
            return gone, error
        if time.monotonic() >= deadline:
            return False, f"process group disappearance timed out pgid={pgid}"
        time.sleep(0.01)


def stop_team(
    root: Path | str | None = None,
    run_id: str | None = None,
    *,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Stop only an exact nonce-bound immutable launch identity.

    ``team.json`` alone is never process authority.  The immutable receipt,
    live tmux session/pane identity, pane PID and OS PGID must all agree.
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

    verified_targets: list[dict[str, Any]] = []
    receipt: dict[str, Any] | None = None
    identity_verified = False
    if session and not dry:
        try:
            chain = _load_team_identity_chain(root_path, run_id, meta)
            receipt = chain[0]
            current_rows = (
                receipt["tasks"] if len(chain) == 1 else chain[-1]["tasks_after"]
            )
            if not tmux_available():
                raise TeamError("tmux unavailable for launch identity readback")
            observed_session = _read_tmux_session_identity(session)
            observed_nonce = _read_tmux_launch_nonce(session)
            observed_panes = _list_pane_identities(session)
            if observed_session != (session, receipt["session_id"]):
                raise TeamError("live tmux session identity mismatch")
            if observed_nonce != receipt["launch_nonce"]:
                raise TeamError("live tmux launch nonce mismatch")
            for row in current_rows:
                window_index = row["window_index"]
                pane_id = row["pane_id"]
                pid = row["pid"]
                pgid = row["pgid"]
                pid_start = row["pid_start"]
                if (
                    isinstance(window_index, bool)
                    or not isinstance(window_index, int)
                    or not isinstance(pane_id, str)
                    or _TMUX_PANE_ID.fullmatch(pane_id) is None
                    or isinstance(pid, bool)
                    or not isinstance(pid, int)
                    or pid <= 0
                    or isinstance(pgid, bool)
                    or not isinstance(pgid, int)
                    or pgid <= 0
                    or not isinstance(pid_start, str)
                    or not pid_start
                    or observed_panes.get(window_index) != (pane_id, pid)
                    or _pgid_for_pid(pid) != pgid
                    or _pid_start_identity(pid) != pid_start
                ):
                    raise TeamError("live tmux pane/process identity mismatch")
                verified_targets.append(dict(row))
            identity_verified = True
        except (TeamError, ProcessLookupError, PermissionError, OSError) as exc:
            errors.append(f"identity verification refused signalling: {exc}")

    # 1) Signal each target only while its exact session/pane/PID/PGID identity
    # is still live.  Do this before killing tmux: after kill-session the pane
    # authority is gone and a recorded PGID could already have been reused.
    signalled: list[dict[str, Any]] = []
    attempted_task_ids: set[str] = set()
    process_disappearance_verified = bool(identity_verified and verified_targets)
    for raw in verified_targets:
        pid = raw["pid"]
        pgid = raw["pgid"]
        tid = raw.get("task_id")
        if not isinstance(pid, int) or not isinstance(pgid, int):
            errors.append(f"verified signal identity became invalid for task={tid}")
            continue
        target = pgid
        try:
            if receipt is None or not _live_signal_target_matches(
                session, receipt, raw
            ):
                identity_verified = False
                errors.append(
                    f"signal identity drift refused signalling for task={tid}"
                )
                process_disappearance_verified = False
                continue
            attempted_task_ids.add(str(tid))
            if os.name == "posix":
                try:
                    os.killpg(target, signal.SIGTERM)
                    actions.append(f"killpg:SIGTERM pgid={target} task={tid}")
                    signalled.append({"task_id": tid, "pgid": target, "pid": pid})
                except ProcessLookupError:
                    actions.append(f"process already gone pgid={target} task={tid}")
                if kill_grace_s and kill_grace_s > 0:
                    import time

                    time.sleep(float(kill_grace_s))
            else:
                os.kill(pid, signal.SIGTERM)
                actions.append(f"kill:SIGTERM pid={pid} task={tid}")
                signalled.append({"task_id": tid, "pgid": target, "pid": pid})

            group_gone, group_error = _process_group_disappeared(pgid)
            if group_error is not None:
                process_disappearance_verified = False
                errors.append(group_error)
                continue
            if not group_gone:
                leader_pgid, leader_error = _receipt_leader_pgid(pid)
                if leader_error is not None:
                    identity_verified = False
                    process_disappearance_verified = False
                    errors.append(leader_error)
                    continue
                escalation_authorized = bool(
                    receipt is not None
                    and leader_pgid in (None, pgid)
                    and _read_tmux_session_identity(session)
                    == (session, receipt.get("session_id"))
                    and _read_tmux_launch_nonce(session) == receipt.get("launch_nonce")
                )
                if not escalation_authorized:
                    identity_verified = False
                    process_disappearance_verified = False
                    errors.append(
                        f"SIGKILL group authority drift refused signalling task={tid}"
                    )
                    continue
                try:
                    os.killpg(target, signal.SIGKILL)
                    actions.append(f"killpg:SIGKILL pgid={target} task={tid}")
                except ProcessLookupError:
                    actions.append(
                        f"process group gone before SIGKILL pgid={target} task={tid}"
                    )
                except (PermissionError, OSError) as exc:
                    identity_verified = False
                    process_disappearance_verified = False
                    errors.append(f"SIGKILL task={tid} target={target}: {exc}")
                    continue
                group_gone, group_error = _wait_process_group_disappearance(pgid)
                if group_error is not None:
                    process_disappearance_verified = False
                    errors.append(group_error)
                    continue

            remaining_pgid, leader_error = _receipt_leader_pgid(pid)
            if leader_error is not None:
                process_disappearance_verified = False
                errors.append(leader_error)
                continue
            if group_gone and remaining_pgid != pgid:
                actions.append(f"process disappearance verified task={tid}")
            else:
                process_disappearance_verified = False
                errors.append(
                    "leader/group disappearance unproved "
                    f"for task={tid} pid={pid} pgid={pgid}"
                )
        except (PermissionError, OSError) as exc:
            identity_verified = False
            process_disappearance_verified = False
            errors.append(f"signal task={tid} target={target}: {exc}")

    # 2) Only after process-group signalling, kill the exact immutable tmux
    # session ID.  Session/nonce must still match; pane liveness may disappear
    # because TERM already succeeded.
    session_disappearance_verified = bool(dry)
    if session and not dry:
        try:
            session_still_exact = bool(
                identity_verified
                and process_disappearance_verified
                and receipt is not None
                and _read_tmux_session_identity(session)
                == (session, receipt.get("session_id"))
                and _read_tmux_launch_nonce(session) == receipt.get("launch_nonce")
            )
        except OSError as exc:
            session_still_exact = False
            errors.append(f"tmux pre-kill identity readback: {exc}")
        if session_still_exact and receipt is not None:
            session_id = str(receipt["session_id"])
            try:
                r = _tmux_run(["kill-session", "-t", session_id])
                probe = _tmux_run(["has-session", "-t", session_id])
                if r.returncode == 0 and probe.returncode == 1:
                    session_disappearance_verified = True
                    actions.append(f"tmux kill-session -t {session_id}")
                    actions.append(f"tmux disappearance verified {session_id}")
                elif r.returncode != 0:
                    errors.append(
                        f"tmux kill-session failed for {session_id}: exit {r.returncode}"
                    )
                else:
                    errors.append(
                        "tmux session disappearance unproved "
                        f"for {session_id}: has-session exit {probe.returncode}"
                    )
            except OSError as exc:
                errors.append(f"tmux kill-session: {exc}")
        else:
            actions.append("identity mismatch: skipped tmux kill-session")
    elif dry:
        actions.append("dry_run: skipped tmux kill-session")

    stop_completed = bool(
        dry
        or (
            identity_verified
            and process_disappearance_verified
            and session_disappearance_verified
        )
    )

    # Update team.json without hiding live or uncertain process truth.
    updated = dict(meta)
    updated["stop_actions"] = actions
    if stop_completed:
        updated["stopped_at"] = _utc_now()
        updated["stop_state"] = "stopped"
        for rec in updated.get("tasks") or []:
            if isinstance(rec, dict) and rec.get("status") not in ("dry_run",):
                rec["status"] = "stopped"
    else:
        updated["stop_refused_at"] = _utc_now()
        updated["stop_state"] = "stop_refused"
        updated["stop_refused_reasons"] = list(errors) or [
            "exact process/session disappearance was not proved"
        ]
        for rec in updated.get("tasks") or []:
            if (
                isinstance(rec, dict)
                and str(rec.get("task_id")) in attempted_task_ids
                and rec.get("status") not in ("dry_run",)
            ):
                rec["status"] = "launch_unknown"
    # Cancel linked ralph composition state when present (D4 team+ralph).
    linked_ralph = updated.get("linked_ralph")
    if (
        stop_completed
        and isinstance(linked_ralph, Mapping)
        and linked_ralph.get("path")
    ):
        try:
            rp, rdata = _load_linked_ralph_state(
                root_path, run_id, linked_ralph=linked_ralph
            )
            rdata["status"] = "cancelled"
            rdata["cancelled_via"] = "team_stop"
            rdata["cancelled_at"] = _utc_now()
            _write_confined_linked_ralph_state(root_path, run_id, rp, rdata)
            actions.append(f"cancelled linked_ralph at {rp}")
        except (TeamError, OSError, json.JSONDecodeError, TypeError) as exc:
            errors.append(f"linked_ralph cancel: {exc}")
    updated.pop("verified", None)
    updated.pop("passes", None)
    _atomic_write_json(team_meta_path(root_path, run_id), updated)

    try:
        write_status(
            root_path,
            run_id,
            "cancelled" if stop_completed else "blocked",
            extra={
                "team": True,
                "stage": "team_stopped" if stop_completed else "team_stop_refused",
                "session": session,
                "note": (
                    "team stop completed with exact disappearance proof"
                    if stop_completed
                    else "team stop refused: live or uncertain launch identity retained"
                ),
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
        "linked_ralph": linked_ralph,
        "identity_verified": identity_verified,
        "process_disappearance_verified": process_disappearance_verified,
        "session_disappearance_verified": session_disappearance_verified,
        "stop_completed": stop_completed,
        "note": "stop signals only immutable launch receipt identities; no pkill -f",
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


# ---------------------------------------------------------------------------
# W3 authoritative Grok-native team control plane
# ---------------------------------------------------------------------------


NATIVE_TEAM_STATES = frozenset(
    {
        "pending",
        "ready",
        "spawn_requested",
        "launch_unknown",
        "running",
        "delivered",
        "integrating",
        "complete",
        "failed",
        "blocked",
        "cancelled",
    }
)
NATIVE_TERMINAL_STATES = frozenset({"complete", "failed", "blocked", "cancelled"})
NATIVE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"ready", "blocked", "cancelled"}),
    "ready": frozenset({"spawn_requested", "blocked", "cancelled"}),
    "spawn_requested": frozenset({"running", "launch_unknown", "blocked", "cancelled"}),
    "launch_unknown": frozenset({"running", "ready", "blocked", "cancelled"}),
    "running": frozenset({"delivered", "ready", "failed", "blocked", "cancelled"}),
    "delivered": frozenset({"integrating", "failed", "blocked", "cancelled"}),
    "integrating": frozenset({"complete", "failed", "blocked"}),
    "complete": frozenset(),
    "failed": frozenset(),
    "blocked": frozenset(),
    "cancelled": frozenset(),
}


def native_team_path(root: Path | str, run_id: str, team_id: str) -> Path:
    """Canonical CLI-owned native-team state path."""

    from omg_cli.contracts.path_keys import safe_path_key
    from omg_cli.contracts.state_schemas import require_safe_id

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
        / "native-team.json"
    )


def _native_lock(path: Path):
    from omg_cli.contracts.path_keys import exclusive_lock

    return exclusive_lock(path.with_suffix(".lock"))


def _native_write(path: Path, state: Mapping[str, Any]) -> None:
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        atomic_write_bytes,
        ensure_managed_dir,
    )
    from omg_cli.contracts.writer_chain import canonical_json_bytes

    ensure_managed_dir(path.parent)
    atomic_write_bytes(
        path, canonical_json_bytes(dict(state)), mode=DATA_FILE_MODE, replace=True
    )


def _validate_native_team(value: Mapping[str, Any]) -> dict[str, Any]:
    from omg_cli.contracts.state_schemas import (
        ContractValidationError,
        require_iso8601,
        require_integer,
        require_safe_id,
        require_git_oid,
        require_sha256,
    )
    from omg_cli.contracts.team_envelope import validate_worker_envelope
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
    from omg_cli.team.roles import native_subagent_type, required_capability_mode

    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "writer",
        "run_id",
        "team_id",
        "leader_id",
        "parent_session_id",
        "transport",
        "base_sha",
        "revision",
        "created_at",
        "tasks",
    }
    if set(row) != required:
        raise ContractValidationError("native team state keys mismatch")
    if (
        row["store_kind"] != "native_team_plane"
        or row["schema_version"] != 1
        or row["writer"] != CLI_WRITER
    ):
        raise ContractValidationError("native team state header mismatch")
    for field in ("run_id", "team_id", "leader_id", "parent_session_id"):
        require_safe_id(row[field], label=field)
    if row["transport"] not in {"grok_native", "tmux_grok"}:
        raise ContractValidationError("native team transport is unsupported")
    require_git_oid(row["base_sha"], label="base_sha")
    require_integer(row["revision"], label="revision", minimum=0)
    require_iso8601(row["created_at"], label="created_at")
    if not isinstance(row["tasks"], dict) or not row["tasks"]:
        raise ContractValidationError("native team tasks must be a non-empty object")
    for task_id, raw in row["tasks"].items():
        require_safe_id(task_id, label="task_id")
        if not isinstance(raw, Mapping):
            raise ContractValidationError("native team task must be an object")
        task = dict(raw)
        expected_task_keys = {
            "task_id",
            "logical_role",
            "dependencies",
            "state",
            "sequence",
            "generation",
            "attempt",
            "envelope",
            "receipt_id",
            "spawn_receipt_hash",
            "role_receipt_hash",
            "binding",
            "result",
            "result_hash",
            "replay_id",
            "updated_at",
            "error",
        }
        if set(task) != expected_task_keys:
            raise ContractValidationError("native team task keys mismatch")
        if task["task_id"] != task_id:
            raise ContractValidationError("native team task key/id mismatch")
        require_safe_id(task["logical_role"], label="logical_role")
        if not isinstance(task["dependencies"], list) or not all(
            isinstance(item, str) and item in row["tasks"]
            for item in task["dependencies"]
        ):
            raise ContractValidationError("native team task dependencies are invalid")
        if len(task["dependencies"]) != len(set(task["dependencies"])):
            raise ContractValidationError(
                "native team task dependencies are duplicated"
            )
        if task["state"] not in NATIVE_TEAM_STATES:
            raise ContractValidationError("native team task state is invalid")
        require_integer(task["sequence"], label="sequence", minimum=0)
        require_integer(task["generation"], label="generation", minimum=0)
        require_integer(task["attempt"], label="attempt", minimum=0)
        envelope = validate_worker_envelope(task["envelope"])
        envelope_identity = {
            "run_id": row["run_id"],
            "team_id": row["team_id"],
            "task_id": task_id,
            "parent_task_id": row["leader_id"],
            "dependencies": task["dependencies"],
            "claim_generation": task["generation"],
            "requested_role": native_subagent_type(task["logical_role"]),
            "capability_mode": required_capability_mode(task["logical_role"]),
        }
        for field, expected in envelope_identity.items():
            if envelope[field] != expected:
                raise ContractValidationError(
                    f"native team task envelope {field} mismatch"
                )
        dependency_results = envelope["dependency_results"]
        if set(dependency_results) != set(task["dependencies"]):
            raise ContractValidationError(
                "native team dependency result keys differ from dependencies"
            )
        for dependency, digest in dependency_results.items():
            if digest is not None:
                require_sha256(digest, label=f"dependency result {dependency}")
        if task["state"] in {"pending", "ready"} and (
            envelope["expected_state"] != task["state"]
            or envelope["expected_sequence"] != task["sequence"]
        ):
            raise ContractValidationError(
                "dispatchable native task envelope fence differs from task"
            )
        for field in ("spawn_receipt_hash", "role_receipt_hash", "result_hash"):
            if task[field] is not None:
                require_sha256(task[field], label=field)
        for field in ("receipt_id", "replay_id"):
            if task[field] is not None:
                require_safe_id(task[field], label=field)
        receipt_fields = (
            task["receipt_id"],
            task["spawn_receipt_hash"],
            task["role_receipt_hash"],
        )
        if any(value is not None for value in receipt_fields) and not all(
            value is not None for value in receipt_fields
        ):
            raise ContractValidationError("native team receipt identity is partial")
        has_receipt = all(value is not None for value in receipt_fields)
        if task["binding"] is not None:
            if not isinstance(task["binding"], dict):
                raise ContractValidationError(
                    "native team binding must be object or null"
                )
            if row["transport"] == "grok_native":
                binding = task["binding"]
                required_binding = {
                    "store_kind",
                    "schema_version",
                    "run_id",
                    "task_id",
                    "parent_id",
                    "host_spawn_id",
                    "observed_session_id",
                    "spawn_receipt_hash",
                    "role_receipt_hash",
                    "receipt_generation",
                    "expected_state",
                    "transition_sequence",
                    "identity_truth",
                }
                if set(binding) != required_binding:
                    raise ContractValidationError("native Grok binding keys mismatch")
                if (
                    binding["store_kind"] != "native_spawn_binding"
                    or binding["schema_version"] != 1
                    or binding["identity_truth"] != "grok_native_receipts"
                ):
                    raise ContractValidationError("native Grok binding header mismatch")
                for field in (
                    "run_id",
                    "task_id",
                    "parent_id",
                    "host_spawn_id",
                    "observed_session_id",
                    "expected_state",
                ):
                    require_safe_id(binding[field], label=f"binding.{field}")
                for field in ("spawn_receipt_hash", "role_receipt_hash"):
                    require_sha256(binding[field], label=f"binding.{field}")
                require_integer(
                    binding["receipt_generation"],
                    label="binding.receipt_generation",
                    minimum=0,
                )
                require_integer(
                    binding["transition_sequence"],
                    label="binding.transition_sequence",
                    minimum=1,
                )
                expected_binding = {
                    "run_id": row["run_id"],
                    "task_id": task_id,
                    "parent_id": row["leader_id"],
                    "spawn_receipt_hash": task["spawn_receipt_hash"],
                    "role_receipt_hash": task["role_receipt_hash"],
                    "receipt_generation": task["generation"],
                }
                if any(
                    binding[field] != expected
                    for field, expected in expected_binding.items()
                ):
                    raise ContractValidationError(
                        "native Grok binding identity mismatch"
                    )
        if task["result"] is not None:
            if not isinstance(task["result"], dict):
                raise ContractValidationError(
                    "native team result must be object or null"
                )
            result = _validate_native_result(task["result"])
            if result["transport"] != row["transport"]:
                raise ContractValidationError("native team result transport mismatch")
            if task["result_hash"] != sha256_hex(canonical_json_bytes(result)):
                raise ContractValidationError("native team result hash mismatch")
            if task["replay_id"] != result["replay_id"]:
                raise ContractValidationError(
                    "native team result replay identity mismatch"
                )
            if len(result["verification_evidence"]) != len(
                envelope["verification_commands"]
            ):
                raise ContractValidationError(
                    "native team result evidence count differs from commands"
                )
        elif task["result_hash"] is not None or task["replay_id"] is not None:
            raise ContractValidationError(
                "native team result identity exists without result"
            )
        if task["binding"] is not None and not has_receipt:
            raise ContractValidationError("native team binding exists without receipts")
        if task["result"] is not None and task["binding"] is None:
            raise ContractValidationError(
                "native team result exists without worker binding"
            )
        if task["state"] in {"pending", "ready"} and (
            has_receipt or task["binding"] is not None or task["result"] is not None
        ):
            raise ContractValidationError(
                "unlaunched native task claims launch/result identity"
            )
        if task["state"] in {"spawn_requested", "launch_unknown"} and (
            not has_receipt or task["binding"] is not None or task["result"] is not None
        ):
            raise ContractValidationError(
                "unreconciled native spawn identity is incomplete"
            )
        if task["state"] == "running" and (
            not has_receipt or task["binding"] is None or task["result"] is not None
        ):
            raise ContractValidationError("running native task identity is incomplete")
        if task["state"] in {"delivered", "integrating", "complete"} and (
            not has_receipt
            or task["binding"] is None
            or task["result"] is None
            or task["result"]["status"] != "ok"
        ):
            raise ContractValidationError(
                "successful native delivery identity is incomplete"
            )
        require_iso8601(task["updated_at"], label="updated_at")
        if task["error"] is not None and not isinstance(task["error"], str):
            raise ContractValidationError(
                "native team task error must be string or null"
            )
        if task["error"] is not None and len(task["error"].encode("utf-8")) > 4096:
            raise ContractValidationError("native team task error exceeds byte cap")
    try:
        _validate_native_dag(list(row["tasks"].values()))
    except TeamError as exc:
        raise ContractValidationError(str(exc)) from exc
    return row


def load_native_team(root: Path | str, run_id: str, team_id: str) -> dict[str, Any]:
    from omg_cli.contracts.writer_chain import parse_canonical_json_bytes

    path = native_team_path(root, run_id, team_id)
    if not path.exists():
        raise TeamError(f"native team state missing: run={run_id} team={team_id}")
    parsed = parse_canonical_json_bytes(path.read_bytes())
    if not isinstance(parsed, dict):
        raise TeamError("native team state must be an object")
    return _validate_native_team(parsed)


def _validate_native_dag(tasks: Sequence[Mapping[str, Any]]) -> None:
    ids = [str(item.get("task_id") or "") for item in tasks]
    if len(ids) != len(set(ids)):
        raise TeamError("native team has duplicate task_id")
    known = set(ids)
    dependencies: dict[str, list[str]] = {}
    for raw, task_id in zip(tasks, ids, strict=True):
        deps = raw.get("dependencies") or []
        if not isinstance(deps, list) or not all(
            isinstance(item, str) for item in deps
        ):
            raise TeamError(f"task {task_id}: dependencies must be a string array")
        if task_id in deps or any(dep not in known for dep in deps):
            raise TeamError(f"task {task_id}: dependency is self/unknown")
        dependencies[task_id] = list(deps)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise TeamError("native team dependency graph contains a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)


def create_native_team(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    leader_id: str,
    parent_session_id: str,
    base_sha: str,
    tasks: Sequence[Mapping[str, Any]],
    transport: str = "grok_native",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create the immutable task DAG and generation-zero envelopes.

    ``transport`` is selected once.  A later native/tmux switch is rejected;
    both transports consume the same fenced envelopes and state transitions.
    """

    from omg_cli.contracts.state_schemas import (
        ContractValidationError,
        require_safe_id,
        require_git_oid,
    )
    from omg_cli.contracts.path_keys import safe_path_key
    from omg_cli.contracts.team_envelope import validate_worker_envelope
    from omg_cli.team.roles import native_subagent_type, required_capability_mode

    for label, value in (
        ("run_id", run_id),
        ("team_id", team_id),
        ("leader_id", leader_id),
        ("parent_session_id", parent_session_id),
    ):
        require_safe_id(value, label=label)
    require_git_oid(base_sha, label="base_sha")
    if transport not in {"grok_native", "tmux_grok"}:
        raise TeamError("transport must be explicitly grok_native or tmux_grok")
    if not tasks or len(tasks) > max_workers_cap():
        raise TeamError(f"native team task count must be 1..{max_workers_cap()}")
    _validate_native_dag(tasks)
    timestamp = created_at or _utc_now()
    state_endpoint = str(native_team_path(root, run_id, team_id))
    task_rows: dict[str, Any] = {}
    for raw in tasks:
        task_id = require_safe_id(raw.get("task_id"), label="task_id")
        logical_role = str(raw.get("role") or "executor")
        requested_role = native_subagent_type(logical_role)
        capability_mode = required_capability_mode(logical_role)
        supplied_mode = raw.get("capability_mode")
        if supplied_mode is not None and supplied_mode != capability_mode:
            raise TeamError(
                f"task {task_id}: capability_mode must be {capability_mode!r} for role"
            )
        write_scope = list(raw.get("write_scope") or raw.get("owned_files") or [])
        if capability_mode == "read-only" and write_scope:
            raise TeamError(f"task {task_id}: read-only role cannot own write paths")
        if capability_mode == "read-write" and not write_scope:
            raise TeamError(
                f"task {task_id}: read-write role requires an explicit write scope"
            )
        dependencies = list(raw.get("dependencies") or [])
        envelope = validate_worker_envelope(
            {
                "store_kind": "worker_envelope",
                "schema_version": 1,
                "run_id": run_id,
                "team_id": team_id,
                "task_id": task_id,
                "parent_task_id": leader_id,
                "dependencies": dependencies,
                "dependency_results": {item: None for item in dependencies},
                "prompt": str(raw.get("prompt") or task_id),
                "requested_role": requested_role,
                "capability_mode": capability_mode,
                "depth": 1,
                "write_scope": write_scope,
                "verification_commands": list(raw.get("verification_commands") or []),
                "artifact_contract": dict(
                    raw.get("artifact_contract") or {"kind": "team-result"}
                ),
                "guidance_hashes": dict(raw.get("guidance_hashes") or {}),
                "mailbox_cursor": "start",
                "claim_generation": 0,
                "state_endpoint": state_endpoint,
                "cancellation_token": "cancel-"
                + safe_path_key(
                    json.dumps(
                        [run_id, team_id, task_id],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    namespace="cancel",
                ),
                "expected_state": "ready" if not dependencies else "pending",
                "expected_sequence": 0,
            }
        )
        task_rows[task_id] = {
            "task_id": task_id,
            "logical_role": logical_role,
            "dependencies": dependencies,
            "state": "ready" if not dependencies else "pending",
            "sequence": 0,
            "generation": 0,
            "attempt": 0,
            "envelope": envelope,
            "receipt_id": None,
            "spawn_receipt_hash": None,
            "role_receipt_hash": None,
            "binding": None,
            "result": None,
            "result_hash": None,
            "replay_id": None,
            "updated_at": timestamp,
            "error": None,
        }
    candidate = _validate_native_team(
        {
            "store_kind": "native_team_plane",
            "schema_version": 1,
            "writer": CLI_WRITER,
            "run_id": run_id,
            "team_id": team_id,
            "leader_id": leader_id,
            "parent_session_id": parent_session_id,
            "transport": transport,
            "base_sha": base_sha,
            "revision": 0,
            "created_at": timestamp,
            "tasks": task_rows,
        }
    )
    path = native_team_path(root, run_id, team_id)
    with _native_lock(path):
        if path.exists():
            current = load_native_team(root, run_id, team_id)
            adopted_candidate = {
                **candidate,
                "created_at": current["created_at"],
                "tasks": {
                    task_id: {
                        **task,
                        "updated_at": current["tasks"]
                        .get(task_id, {})
                        .get("updated_at", task["updated_at"]),
                    }
                    for task_id, task in candidate["tasks"].items()
                },
            }
            if current != adopted_candidate:
                raise ContractValidationError(
                    "native team identity replayed with different bytes"
                )
            return current
        _native_write(path, candidate)
    return candidate


def _cas_native_task(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    expected_state: str,
    expected_sequence: int,
    expected_generation: int,
    next_state: str,
    updates: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from omg_cli.contracts.state_schemas import require_integer, require_safe_id

    require_safe_id(task_id, label="task_id")
    require_integer(expected_sequence, label="expected_sequence", minimum=0)
    require_integer(expected_generation, label="expected_generation", minimum=0)
    if next_state not in NATIVE_TRANSITIONS.get(expected_state, frozenset()):
        raise TeamError(
            f"illegal native task transition {expected_state}->{next_state}"
        )
    path = native_team_path(root, run_id, team_id)
    with _native_lock(path):
        current = load_native_team(root, run_id, team_id)
        task = dict(current["tasks"].get(task_id) or {})
        if not task:
            raise TeamError(f"unknown native team task {task_id!r}")
        observed = (task["state"], task["sequence"], task["generation"])
        expected = (expected_state, expected_sequence, expected_generation)
        if observed != expected:
            raise TeamError(
                f"native task CAS mismatch: expected={expected!r} observed={observed!r}"
            )
        changed = dict(updates or {})
        forbidden = {"task_id", "dependencies", "logical_role", "sequence", "state"}
        if forbidden & set(changed):
            raise TeamError("native task CAS update contains immutable/control fields")
        if isinstance(changed.get("error"), str):
            from omg_cli.redaction import redact_text

            redacted_error = redact_text(changed["error"])
            changed["error"] = redacted_error.encode("utf-8")[:4096].decode(
                "utf-8", errors="ignore"
            )
        task = {
            **task,
            **changed,
            "state": next_state,
            "sequence": expected_sequence + 1,
            "updated_at": _utc_now(),
        }
        tasks = dict(current["tasks"])
        tasks[task_id] = task
        updated = _validate_native_team(
            {**current, "revision": current["revision"] + 1, "tasks": tasks}
        )
        _native_write(path, updated)
        return updated, task


def prepare_native_spawn(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    expected_sequence: int,
    expected_generation: int,
    lease_generation: int,
    description: str,
    worktree: Path | str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Persist receipts, CAS ``ready->spawn_requested``, return tool payload."""

    from datetime import timedelta
    import uuid

    from omg_cli.contracts.state_schemas import ContractValidationError, require_integer
    from omg_cli.contracts.tracker_contract import (
        make_role_receipt,
        validate_spawn_receipt,
    )
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
    from omg_cli.team.providers import build_grok_native_spawn
    from omg_cli.tracker import load_spawn_receipt_pair, persist_spawn_receipt_pair

    require_integer(lease_generation, label="lease_generation", minimum=0)
    current = load_native_team(root, run_id, team_id)
    if current["transport"] != "grok_native":
        raise TeamError("native spawn preparation cannot switch a tmux_grok team lane")
    task = dict(current["tasks"].get(task_id) or {})
    if not task:
        raise TeamError(f"unknown native team task {task_id!r}")
    if (task["state"], task["sequence"], task["generation"]) != (
        "ready",
        expected_sequence,
        expected_generation,
    ):
        raise TeamError("native spawn preparation CAS mismatch")
    envelope = {
        **task["envelope"],
        "claim_generation": expected_generation,
        "expected_state": "ready",
        "expected_sequence": expected_sequence,
        "dependency_results": {
            dep: current["tasks"][dep]["result_hash"] for dep in task["dependencies"]
        },
    }
    if envelope["capability_mode"] == "read-write":
        if worktree is None:
            raise TeamError("read-write native task requires its exact owned worktree")
        from omg_cli.team.worktree import TeamWorktreeError, load_worktree_receipt

        try:
            worktree_receipt = load_worktree_receipt(
                root,
                run_id=run_id,
                team_id=team_id,
                task_id=task_id,
            )
        except (ContractValidationError, TeamWorktreeError) as exc:
            raise TeamError(f"read-write native worktree is not valid: {exc}") from exc
        expected_worktree = {
            "generation": expected_generation,
            "base_sha": current["base_sha"],
            "owned_paths": sorted(
                envelope["write_scope"], key=lambda item: item.encode("utf-8")
            ),
            "state": "created",
            "worktree_path": str(Path(worktree).resolve()),
        }
        if any(
            worktree_receipt[field] != expected
            for field, expected in expected_worktree.items()
        ):
            raise TeamError(
                "read-write native worktree identity/scope/generation mismatch"
            )
    from omg_cli.contracts.path_keys import safe_path_key

    receipt_id = (
        "spawn-"
        + safe_path_key(
            json.dumps(
                [team_id, task_id, expected_generation, task["attempt"] + 1],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            namespace="spawn-receipt",
        )[:48]
    )
    expiry = expires_at or (
        datetime.now(timezone.utc) + timedelta(minutes=10)
    ).isoformat().replace("+00:00", "Z")
    previous = load_spawn_receipt_pair(root, run_id=run_id, receipt_id=receipt_id)
    if previous is None:
        spawn_receipt = {
            "store_kind": "spawn_receipt",
            "schema_version": 1,
            "receipt_id": receipt_id,
            "run_id": run_id,
            "team_id": team_id,
            "task_id": task_id,
            "parent_id": current["leader_id"],
            "parent_session_id": current["parent_session_id"],
            "requested_role": envelope["requested_role"],
            "capability_mode": envelope["capability_mode"],
            "depth": 1,
            "attempt": task["attempt"] + 1,
            "receipt_generation": expected_generation,
            "lease_generation": lease_generation,
            "dispatch_nonce": uuid.uuid4().hex,
            "expires_at": expiry,
            "expected_state": "ready",
            "expected_sequence": expected_sequence,
        }
        role_receipt = make_role_receipt(spawn_receipt)
        stored = persist_spawn_receipt_pair(
            root, spawn_receipt=spawn_receipt, role_receipt=role_receipt
        )
    else:
        if previous["status"] != "spawn_requested":
            raise TeamError(
                "persisted native receipt was already reconciled; blind redispatch refused"
            )
        spawn_receipt = validate_spawn_receipt(
            previous["spawn_receipt"], now=datetime.now(timezone.utc)
        )
        role_receipt = make_role_receipt(spawn_receipt)
        expected_identity = {
            "run_id": run_id,
            "team_id": team_id,
            "task_id": task_id,
            "parent_id": current["leader_id"],
            "parent_session_id": current["parent_session_id"],
            "requested_role": envelope["requested_role"],
            "capability_mode": envelope["capability_mode"],
            "receipt_generation": expected_generation,
            "lease_generation": lease_generation,
            "expected_state": "ready",
            "expected_sequence": expected_sequence,
        }
        if any(
            spawn_receipt[field] != value for field, value in expected_identity.items()
        ):
            raise TeamError("persisted native receipt identity differs from ready task")
        stored = previous
    invocation = build_grok_native_spawn(
        envelope,
        spawn_receipt,
        role_receipt,
        description=description,
        worktree=worktree,
    )
    _, updated_task = _cas_native_task(
        root,
        run_id=run_id,
        team_id=team_id,
        task_id=task_id,
        expected_state="ready",
        expected_sequence=expected_sequence,
        expected_generation=expected_generation,
        next_state="spawn_requested",
        updates={
            "attempt": task["attempt"] + 1,
            "envelope": envelope,
            "receipt_id": receipt_id,
            "spawn_receipt_hash": sha256_hex(canonical_json_bytes(spawn_receipt)),
            "role_receipt_hash": sha256_hex(canonical_json_bytes(role_receipt)),
            "binding": None,
            "result": None,
            "result_hash": None,
            "replay_id": None,
            "error": None,
        },
    )
    return {
        "task": updated_task,
        "receipt_pair": stored,
        "invocation": invocation.to_dict(),
    }


def reconcile_native_spawn(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    inventory: Sequence[Mapping[str, Any]],
    expected_state: str,
    expected_sequence: int,
    expected_generation: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Adopt exactly one observed Grok host identity or enter launch_unknown."""

    from omg_cli.team.liveness import initialize_liveness
    from omg_cli.tracker import reconcile_spawn_observation

    current = load_native_team(root, run_id, team_id)
    if current["transport"] != "grok_native":
        raise TeamError(
            "native spawn reconciliation cannot switch a tmux_grok team lane"
        )
    task = dict(current["tasks"].get(task_id) or {})
    if (task.get("state"), task.get("sequence"), task.get("generation")) != (
        expected_state,
        expected_sequence,
        expected_generation,
    ) or expected_state not in {"spawn_requested", "launch_unknown"}:
        raise TeamError("native spawn reconciliation CAS mismatch")
    receipt_id = task.get("receipt_id")
    if not isinstance(receipt_id, str):
        raise TeamError("native task has no persisted spawn receipt")
    outcome = reconcile_spawn_observation(
        root,
        run_id=run_id,
        receipt_id=receipt_id,
        inventory=inventory,
        expected_generation=expected_generation,
        now=now,
    )
    if outcome["outcome"] == "bound":
        # Create/adopt liveness before exposing ``running``.  A crash here is
        # safe: reconciliation reuses the same persisted receipt and identity.
        initialize_liveness(
            root,
            run_id=run_id,
            team_id=team_id,
            task_id=task_id,
            worker_id=outcome["binding"]["host_spawn_id"],
            generation=expected_generation,
            now=now,
        )
        _, updated_task = _cas_native_task(
            root,
            run_id=run_id,
            team_id=team_id,
            task_id=task_id,
            expected_state=expected_state,
            expected_sequence=expected_sequence,
            expected_generation=expected_generation,
            next_state="running",
            updates={"binding": outcome["binding"], "error": None},
        )
        return {**outcome, "task": updated_task}
    next_state = (
        "launch_unknown" if outcome["outcome"] == "launch_unknown" else "blocked"
    )
    if expected_state == next_state:
        return {**outcome, "task": task}
    _, updated_task = _cas_native_task(
        root,
        run_id=run_id,
        team_id=team_id,
        task_id=task_id,
        expected_state=expected_state,
        expected_sequence=expected_sequence,
        expected_generation=expected_generation,
        next_state=next_state,
        updates={"error": outcome["outcome"]},
    )
    return {**outcome, "task": updated_task}


def _validate_native_result(value: Mapping[str, Any]) -> dict[str, Any]:
    from omg_cli.contracts.state_schemas import (
        ContractValidationError,
        require_integer,
        require_iso8601,
        require_safe_id,
        require_sha256,
    )

    row = dict(value)
    required = {
        "store_kind",
        "schema_version",
        "transport",
        "run_id",
        "team_id",
        "task_id",
        "generation",
        "host_spawn_id",
        "observed_session_id",
        "spawn_receipt_hash",
        "role_receipt_hash",
        "expected_state",
        "expected_sequence",
        "replay_id",
        "status",
        "artifact",
        "verification_evidence",
        "completed_at",
    }
    if set(row) != required:
        raise ContractValidationError("native worker result keys mismatch")
    if row["store_kind"] != "native_worker_result" or row["schema_version"] != 1:
        raise ContractValidationError("native worker result header mismatch")
    if row["transport"] not in {"grok_native", "tmux_grok"}:
        raise ContractValidationError("native worker result transport mismatch")
    for field in (
        "run_id",
        "team_id",
        "task_id",
        "host_spawn_id",
        "observed_session_id",
        "replay_id",
    ):
        require_safe_id(row[field], label=field)
    require_integer(row["generation"], label="generation", minimum=0)
    require_integer(row["expected_sequence"], label="expected_sequence", minimum=0)
    for field in ("spawn_receipt_hash", "role_receipt_hash"):
        require_sha256(row[field], label=field)
    if row["expected_state"] != "running":
        raise ContractValidationError(
            "native worker result expected_state must be running"
        )
    if row["status"] not in {"ok", "failed", "blocked", "cancelled"}:
        raise ContractValidationError("native worker result status mismatch")
    if not isinstance(row["artifact"], dict):
        raise ContractValidationError("native worker result artifact must be an object")
    from omg_cli.contracts.writer_chain import canonical_json_bytes

    if len(canonical_json_bytes(row["artifact"])) > 65_536:
        raise ContractValidationError("native worker result artifact is unbounded")
    if (
        not isinstance(row["verification_evidence"], list)
        or len(row["verification_evidence"]) > 32
    ):
        raise ContractValidationError(
            "native result verification evidence is unbounded"
        )
    for digest in row["verification_evidence"]:
        require_sha256(digest, label="verification_evidence")
    require_iso8601(row["completed_at"], label="completed_at")
    return row


def record_native_result(
    root: Path | str,
    *,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """CAS-consume one immutable result; stale/cross-lane/replay fails closed."""

    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    value = _validate_native_result(result)
    current = load_native_team(root, value["run_id"], value["team_id"])
    task = dict(current["tasks"].get(value["task_id"]) or {})
    if value["transport"] != current["transport"]:
        raise TeamError("native result crossed the immutable team transport lane")
    result_hash = sha256_hex(canonical_json_bytes(value))
    if task.get("result_hash") is not None:
        if (
            task["result_hash"] == result_hash
            and task.get("replay_id") == value["replay_id"]
        ):
            from omg_cli.team.liveness import LivenessError, mark_terminal

            try:
                mark_terminal(
                    root,
                    run_id=value["run_id"],
                    team_id=value["team_id"],
                    task_id=value["task_id"],
                    worker_id=value["host_spawn_id"],
                    generation=value["generation"],
                )
            except LivenessError:
                pass
            return {"duplicate": True, "result_hash": result_hash, "task": task}
        raise TeamError("native result replay conflicts with prior immutable result")
    if (task.get("state"), task.get("sequence"), task.get("generation")) != (
        "running",
        value["expected_sequence"],
        value["generation"],
    ):
        raise TeamError("native result state/sequence/generation fence mismatch")
    binding = task.get("binding") or {}
    expected_bindings = {
        "host_spawn_id": value["host_spawn_id"],
        "observed_session_id": value["observed_session_id"],
        "spawn_receipt_hash": value["spawn_receipt_hash"],
        "role_receipt_hash": value["role_receipt_hash"],
    }
    if any(
        binding.get(field) != expected for field, expected in expected_bindings.items()
    ):
        raise TeamError("native result does not match bound Grok identity/receipts")
    artifact_contract = task["envelope"]["artifact_contract"]
    if any(
        value["artifact"].get(field) != expected
        for field, expected in artifact_contract.items()
    ):
        raise TeamError("native result artifact violates the task artifact contract")
    expected_evidence = len(task["envelope"]["verification_commands"])
    if len(value["verification_evidence"]) != expected_evidence:
        raise TeamError(
            "native result verification evidence count differs from declared commands"
        )
    next_state = "delivered" if value["status"] == "ok" else value["status"]
    _, updated_task = _cas_native_task(
        root,
        run_id=value["run_id"],
        team_id=value["team_id"],
        task_id=value["task_id"],
        expected_state="running",
        expected_sequence=value["expected_sequence"],
        expected_generation=value["generation"],
        next_state=next_state,
        updates={
            "result": value,
            "result_hash": result_hash,
            "replay_id": value["replay_id"],
            "error": None if value["status"] == "ok" else value["status"],
        },
    )
    from omg_cli.team.liveness import LivenessError, mark_terminal

    try:
        mark_terminal(
            root,
            run_id=value["run_id"],
            team_id=value["team_id"],
            task_id=value["task_id"],
            worker_id=value["host_spawn_id"],
            generation=value["generation"],
        )
    except LivenessError:
        # The accepted result remains canonical.  A retry takes the duplicate
        # branch above and retries terminalization without consuming twice.
        pass
    return {"duplicate": False, "result_hash": result_hash, "task": updated_task}


def transition_native_delivery(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    task_id: str,
    expected_state: str,
    expected_sequence: int,
    expected_generation: int,
    next_state: str,
    result_hash: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Leader-only delivery integration/terminal transition."""

    from omg_cli.contracts.state_schemas import require_sha256

    require_sha256(result_hash, label="result_hash")
    current = load_native_team(root, run_id, team_id)
    task = dict(current["tasks"].get(task_id) or {})
    if task.get("result_hash") != result_hash:
        raise TeamError("delivery result hash differs from accepted immutable result")
    _, updated_task = _cas_native_task(
        root,
        run_id=run_id,
        team_id=team_id,
        task_id=task_id,
        expected_state=expected_state,
        expected_sequence=expected_sequence,
        expected_generation=expected_generation,
        next_state=next_state,
        updates={"error": error},
    )
    return updated_task


def native_team_status(
    root: Path | str, *, run_id: str, team_id: str
) -> dict[str, Any]:
    """Read-only bounded projection; never grants completion authority."""

    state = load_native_team(root, run_id, team_id)
    tasks = [
        {
            "task_id": task_id,
            "state": task["state"],
            "sequence": task["sequence"],
            "generation": task["generation"],
            "attempt": task["attempt"],
            "host_spawn_id": (task.get("binding") or {}).get("host_spawn_id"),
            "result_hash": task.get("result_hash"),
            "error": task.get("error"),
        }
        for task_id, task in sorted(state["tasks"].items())
    ]
    return {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "transport": state["transport"],
        "revision": state["revision"],
        "tasks": tasks,
        "terminal": all(task["state"] in NATIVE_TERMINAL_STATES for task in tasks),
        "complete": all(task["state"] == "complete" for task in tasks),
        "verified": False,
    }


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
    "NATIVE_TEAM_STATES",
    "NATIVE_TERMINAL_STATES",
    "NATIVE_TRANSITIONS",
    "create_native_team",
    "load_native_team",
    "native_team_path",
    "native_team_status",
    "prepare_native_spawn",
    "record_native_result",
    "reconcile_native_spawn",
    "transition_native_delivery",
]
