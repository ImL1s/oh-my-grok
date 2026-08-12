"""Tmux team plane (D1 grok-only + D3 multi-CLI routing).

Gate
----
**Default on.** Kill switch ``OMG_DISABLE_TMUX_TEAM=1``; legacy
``OMG_EXPERIMENTAL_TMUX_TEAM=0`` also disables. Isolation is **integration**
isolation (worktree ownership + seal + integrate), **not** an execution sandbox.

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

import copy
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Literal, Mapping, Sequence

from omg_cli.evidence import CLI_WRITER, safe_supervised_child_env
from omg_cli.fanout import max_workers_cap
from omg_cli.madmax import (
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
    clear_active,
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
    prepare_task,
    seal_all_tasks,
    worktree_dir,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Historical opt-in name (still accepted). Default is **on**; kill switch below.
EXPERIMENTAL_ENV = "OMG_EXPERIMENTAL_TMUX_TEAM"
# Production kill switch — when truthy, all team launch/api/scale paths refuse.
DISABLE_ENV = "OMG_DISABLE_TMUX_TEAM"
# Markers injected into worker panes / process-fanout children so nested
# supervisors refuse (depth-1 — a worker must not launch a team).
WORKER_ENV_MARKERS: tuple[str, ...] = (
    "OMG_TEAM_WORKER",
    "OMG_PROCESS_FANOUT_WORKER",
    "OMG_SPAWNED_WORKER",
)
TEAM_WORKER_ENV = "OMG_TEAM_WORKER"
TEAM_RUN_ID_ENV = "OMG_TEAM_RUN_ID"
TEAM_ID_ENV = "OMG_TEAM_ID"
TEAM_WORKER_ID_ENV = "OMG_TEAM_WORKER_ID"
TEAM_STATE_ROOT_ENV = "OMG_TEAM_STATE_ROOT"
TEAM_LEADER_ROOT_ENV = "OMG_TEAM_LEADER_ROOT"
TEAM_OWNER_TOKEN_ENV = "OMG_TEAM_OWNER_TOKEN"
# Markers that still mean "non-team depth-1 spawn" for team-api denial.
SPAWN_DENY_API_MARKERS: tuple[str, ...] = (
    "OMG_PROCESS_FANOUT_WORKER",
    "OMG_SPAWNED_WORKER",
)
WORKSPACE_MODE = "worktree"
# Team.json schema: v1 = pre-#102; v2 adds tmux_topology / logical worker slots.
LEGACY_TEAM_META_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
# Linked Ralph state remains schema 1 (independent of team.json bumps).
RALPH_SCHEMA_VERSION = 1
LAUNCH_RECEIPT_SCHEMA_VERSION = 3
# #108-era receipts: intent binding without topology (#96 view_mode).
V2_LAUNCH_RECEIPT_SCHEMA_VERSION = 2
# #106-era immutable launch receipts (no intent_nonce / window_name).
# Readable only for existing-Team identity-chain ops (stop/scale/relaunch).
LEGACY_LAUNCH_RECEIPT_SCHEMA_VERSION = 1
# Legacy identity receipts reused schema_version=1 before scale_intent fields.
LEGACY_IDENTITY_RECEIPT_SCHEMA_VERSION = 1
# #98-era identity receipts with window_id / window_nonce / scale_intent.
V2_IDENTITY_RECEIPT_SCHEMA_VERSION = 2
# #102 identity receipts: logical_worker_index / attempt / pane owner / topology.
IDENTITY_RECEIPT_SCHEMA_VERSION = 3
LAUNCH_NONCE_OPTION = "@omg_launch_nonce"
WORKER_PANE_NONCE_OPTION = "@omg_worker_nonce"
_TMUX_SESSION_ID = re.compile(r"^\$[0-9]{1,16}$")
_TMUX_PANE_ID = re.compile(r"^%[0-9]{1,16}$")
_TMUX_WINDOW_ID = re.compile(r"^@[0-9]{1,16}$")

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
    """Policy / experimental gate failure (maps to exit 2).

    Optional ``code`` is a stable typed token (e.g. ``E_TEAM_NESTED_LAUNCH``)
    for CLI/API consumers; message remains human-readable.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Paths / gates
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy_env(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _falsey_env(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("0", "false", "no", "off")


def experimental_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the tmux team plane may run.

    **Default on** after production promotion (process-ready + fixture smoke).
    Kill switch: ``OMG_DISABLE_TMUX_TEAM=1``.
    Legacy: ``OMG_EXPERIMENTAL_TMUX_TEAM=0`` still disables; ``=1`` enables.
    """
    source = env if env is not None else os.environ
    if _truthy_env(source.get(DISABLE_ENV)):
        return False
    raw = source.get(EXPERIMENTAL_ENV)
    if raw is not None and str(raw).strip() != "":
        if _falsey_env(raw):
            return False
        if _truthy_env(raw):
            return True
        # Unknown non-empty value: fail closed (disabled).
        return False
    return True


def in_spawned_worker_context(env: Mapping[str, str] | None = None) -> bool:
    """True when this process is a depth-1 worker (must not re-launch team).

    Reuses the same marker family process fanout / team panes inject into
    child environments (``OMG_*_WORKER``). Prompt-only bans are insufficient.
    Command-text env assignments are never consulted — only the provided
    mapping or the real process environment.
    """
    source = env if env is not None else os.environ
    for key in WORKER_ENV_MARKERS:
        if _truthy_env(source.get(key)):
            return True
    return False


def refuse_nested_team_launch(
    env: Mapping[str, str] | None = None,
    *,
    action: str = "launch",
) -> None:
    """Fail closed before Team side effects when already a depth-1 worker.

    Stable code: ``E_TEAM_NESTED_LAUNCH``. Must run before run creation,
    worktree prep, tmux mutation, descriptor publication, or state writes.
    Does not authorize from command-text env assignments.

    Does **not** gate legal pane ``omg team supervisor``: those processes are
    intentionally worker-marked and admitted via identity-bound descriptor
    validation (see ``admit_pane_supervisor``). Use this only for lifecycle
    verbs that create or control a nested team (launch/start/run/scale/…).
    """
    if in_spawned_worker_context(env):
        raise TeamGateError(
            f"omg team {action} refused: already inside a spawned-worker context "
            f"(depth-1; E_TEAM_NESTED_LAUNCH; one of "
            f"{', '.join(WORKER_ENV_MARKERS)} is set). "
            "Workers must not launch or control a nested team.",
            code="E_TEAM_NESTED_LAUNCH",
        )


# Leader-only operator mutations (#101 / PR #156): workers must not drive
# peer/leader panes. Identity-bound ``api`` and read-only ``status`` /
# ``panes`` / ``capture`` remain usable from worker context.
LEADER_ONLY_OPERATOR_ACTIONS: frozenset[str] = frozenset(
    {
        "input",
        "key",
        "focus",
        "view",
    }
)


def refuse_worker_operator_mutation(
    env: Mapping[str, str] | None = None,
    *,
    action: str = "input",
) -> None:
    """Fail closed before operator/tmux mutations when already a worker.

    Stable code: ``E_TEAM_WORKER_OPERATION_REFUSED``. Must run before
    ``project_root`` discovery, operator helpers, tmux client focus/send, or
    any worktree/state write. Does not authorize from command-text env
    assignments.

    Does **not** gate legal pane supervisor admission, identity-bound team
    API, or genuinely read-only status/panes/capture paths.
    """
    if in_spawned_worker_context(env):
        raise TeamGateError(
            f"omg team {action} refused: worker processes cannot invoke "
            f"leader/operator controls (E_TEAM_WORKER_OPERATION_REFUSED; "
            f"one of {', '.join(WORKER_ENV_MARKERS)} is set). "
            "Use identity-bound team API or read-only status/panes/capture.",
            code="E_TEAM_WORKER_OPERATION_REFUSED",
        )


def in_non_team_spawn_context(env: Mapping[str, str] | None = None) -> bool:
    """True for process-fanout / spawned-subagent workers (not team panes)."""
    source = env if env is not None else os.environ
    for key in SPAWN_DENY_API_MARKERS:
        if _truthy_env(source.get(key)):
            return True
    return False


def team_worker_identity(env: Mapping[str, str] | None = None) -> str | None:
    """Return team worker id when ``OMG_TEAM_WORKER`` + identity env are set."""
    source = env if env is not None else os.environ
    if not _truthy_env(source.get(TEAM_WORKER_ENV)):
        return None
    wid = (source.get(TEAM_WORKER_ID_ENV) or "").strip()
    return wid or None


def team_dir(root: Path | str, run_id: str) -> Path:
    return _run_dir(Path(root), run_id) / "team"


def team_meta_path(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / "team.json"


def team_meta_lock_path(root: Path | str, run_id: str) -> Path:
    """Run-scoped exclusive lock for authoritative ``team.json`` mutations."""

    return team_dir(root, run_id) / "team-meta.lock"


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


# Identity fields that may never change across team.json mutations.
_TEAM_META_IMMUTABLE_FIELDS: tuple[str, ...] = (
    "run_id",
    "created_at",
    "launch_nonce",
    "launch_receipt_sha256",
    "workspace_mode",
    "session",
    "view_mode",
    "session_owned",
    "session_id",
    "window_id",
    "leader_pane_id",
    "leader_pane_pid",
    "tmux_socket_path",
    "tmux_server_pid",
    "tmux_server_pid_start",
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


def _atomic_write_json_at(parent_fd: int, name: str, data: Mapping[str, Any]) -> None:
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        ContractPathError,
        atomic_write_bytes_at,
    )

    body = (
        json.dumps(dict(data), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes_at(parent_fd, name, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise TeamError(f"secure team.json publication refused: {exc}") from exc


def _load_team_meta_from_fd(parent_fd: int, *, run_id: str) -> dict[str, Any]:
    """Load ``team.json`` relative to a pinned team-directory descriptor."""

    from omg_cli.contracts.path_keys import DATA_FILE_MODE

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open("team.json", flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise TeamError(f"team.json missing for run {run_id}")
    except OSError as exc:
        raise TeamError(f"team.json secure open refused: {exc}") from exc
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
            f"team.json mode must be {DATA_FILE_MODE:04o}, got {stat.S_IMODE(info.st_mode):04o}"
        )
    return data


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
            f"team.json mode must be {DATA_FILE_MODE:04o}, got {stat.S_IMODE(info.st_mode):04o}"
        )
    return data


def _read_meta_generation(meta: Mapping[str, Any]) -> int:
    """Return current meta_generation (0 when absent on pre-#21 documents)."""

    raw = meta.get("meta_generation")
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise TeamError(
            f"team.json meta_generation must be a non-negative int, got {raw!r}"
        )
    return raw


def _assert_immutable_team_meta(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    run_id: str,
) -> None:
    if after.get("run_id") != run_id:
        raise TeamError(
            f"team.json run_id mismatch after mutate (path={run_id!r} body={after.get('run_id')!r})"
        )
    for key in _TEAM_META_IMMUTABLE_FIELDS:
        if key not in before or before.get(key) is None:
            continue
        if after.get(key) != before.get(key):
            raise TeamError(
                f"team.json immutable field {key!r} changed under mutate "
                f"({before.get(key)!r} -> {after.get(key)!r})"
            )
    # #102: nested tmux_topology.anchor is launch-immutable when present.
    before_topo = before.get("tmux_topology")
    after_topo = after.get("tmux_topology")
    if isinstance(before_topo, Mapping):
        before_anchor = before_topo.get("anchor")
        if isinstance(before_anchor, Mapping) and before_anchor:
            if not isinstance(after_topo, Mapping):
                raise TeamError("team.json tmux_topology.anchor removed under mutate")
            after_anchor = after_topo.get("anchor")
            if after_anchor != before_anchor:
                raise TeamError(
                    "team.json tmux_topology.anchor changed under mutate"
                )


def _build_launch_tmux_topology(
    meta: Mapping[str, Any],
    *,
    launch_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the nested tmux_topology object for a fresh launch meta write."""
    from omg_cli.team.topology import (
        LAYOUT_STATUS_CLEAN,
        build_topology_snapshot,
        TopologyError,
    )

    try:
        snap = build_topology_snapshot(meta, receipt=launch_receipt)
    except TopologyError:
        # Dry-run / incomplete authority: omit nested object rather than fail launch.
        # Live path always has session_id + launch_nonce before this helper runs.
        return {
            "schema_version": 1,
            "anchor": None,
            "identity_generation": int(meta.get("identity_generation") or 0),
            "identity_receipt_sha256": meta.get("identity_receipt_sha256")
            or meta.get("launch_receipt_sha256"),
            "active_workers": [],
            "placement": {"strategy": "right_stack", "right_stack_root_pane_id": None},
            "layout": {
                "name": meta.get("layout") or "tiled",
                "leader_width_policy": "clamped_half",
                "status": LAYOUT_STATUS_CLEAN,
                "last_error_code": None,
            },
        }
    body = snap.to_tmux_topology_dict()
    body["layout"]["status"] = LAYOUT_STATUS_CLEAN
    return body


def mutate_team_meta(
    root: Path | str,
    run_id: str,
    mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    *,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Atomically mutate authoritative ``team.json`` under a run-scoped lock.

    Contract (#21):
      1. Validate CLI writer authority + schema via :func:`load_team_meta`
      2. Serialize concurrent writers with ``team-meta.lock``
      3. Optional CAS on ``meta_generation`` (stale → :class:`TeamError`)
      4. Apply *mutator* on a deep copy; reject immutable identity changes
      5. Bump / introduce ``meta_generation``; strip ``verified`` / ``passes``
      6. Publish via :func:`_atomic_write_json` (exact ``0600``, confined)

    *mutator* receives a deep copy of the current document and may either
    mutate it in place and return ``None``/the same dict, or return a new
    mapping. On publication failure the previous valid document remains.
    """
    from omg_cli.contracts.path_keys import (
        ContractPathError,
        exclusive_lock_at,
        open_managed_dir_fd,
    )
    from omg_cli.contracts.state_schemas import require_safe_id

    rid = require_safe_id(run_id, label="run_id")
    root_path = Path(root).resolve()
    team_path = team_dir(root_path, rid)
    try:
        parent_fd = open_managed_dir_fd(team_path)
    except ContractPathError as exc:
        raise TeamError(f"secure team directory open refused: {exc}") from exc
    try:
        # Lock, read, and publish all share this pinned team-dir inode.
        with exclusive_lock_at(parent_fd, "team-meta.lock"):
            current = _load_team_meta_from_fd(parent_fd, run_id=rid)
            if current.get("run_id") is not None and current.get("run_id") != rid:
                raise TeamError(
                    f"team.json run_id mismatch (file={current.get('run_id')!r} path={rid!r})"
                )
            # Prefer path-bound identity when historical docs omit run_id.
            if current.get("run_id") is None:
                current = dict(current)
                current["run_id"] = rid

            current_gen = _read_meta_generation(current)
            if expected_generation is not None:
                if (
                    isinstance(expected_generation, bool)
                    or not isinstance(expected_generation, int)
                    or expected_generation < 0
                ):
                    raise TeamError(
                        "expected_generation must be a non-negative int, "
                        f"got {expected_generation!r}"
                    )
                if expected_generation != current_gen:
                    raise TeamError(
                        f"stale team meta generation: expected {expected_generation}, "
                        f"have {current_gen}"
                    )

            draft = copy.deepcopy(dict(current))
            result = mutator(draft)
            if result is None:
                updated = draft
            else:
                if not isinstance(result, Mapping):
                    raise TeamError("team.json mutator must return a mapping or None")
                updated = dict(result)

            if not isinstance(updated, dict):
                raise TeamError("team.json mutator result must be a JSON object")

            _assert_immutable_team_meta(current, updated, run_id=rid)
            # Path identity wins; re-stamp CLI writer authority always.
            updated["run_id"] = rid
            updated["writer"] = CLI_WRITER
            if "schema_version" not in updated:
                updated["schema_version"] = current.get(
                    "schema_version", SCHEMA_VERSION
                )
            updated["meta_generation"] = current_gen + 1
            updated.pop("verified", None)
            updated.pop("passes", None)

            _require_cli_writer(updated, label="team.json")
            _atomic_write_json_at(parent_fd, "team.json", updated)
            return updated
    finally:
        os.close(parent_fd)


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
            f"omg team is disabled "
            f"(set {DISABLE_ENV}=1 kill-switch, or {EXPERIMENTAL_ENV}=0).\n"
            f"  Unset {DISABLE_ENV} (and do not set {EXPERIMENTAL_ENV}=0) to enable.\n"
            f"  Isolation is worktree ownership + seal/integrate "
            f"(not an execution sandbox). Multi-CLI panes require explicit "
            f"role routing; zero-config remains grok-only."
        )
    refuse_nested_team_launch(env, action="start")
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
            "## Coordination (CLI-first)",
            f"- Your worker id is `{task_id}` (also in `OMG_TEAM_WORKER_ID`).",
            "- Provider readiness is owned by the pane supervisor (#99): "
            "phases through `provider_ready` / `task_dispatched` with live "
            "provider identity — not a pre-provider helper receipt.",
            "- Read your inbox under the team api worker dir when present.",
            "- If your tools allow shell, optionally enrich with a mailbox "
            "ACK (never required for launch success; cannot override failed "
            "provider evidence):",
            "  `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api send-message --input "
            f'\'{{"run_id":"{run_id}","team_id":"team","from_worker":"{task_id}",'
            '"to_worker":"leader-fixed","body":"ACK"}\'`',
            "- Then `claim-task` for your board task, work, commit, and "
            "`transition-task-status` (include `worker` matching your id + "
            "claim_token) to completed.",
            "- Do **not** forge another worker's identity; the CLI binds "
            "`from_worker` / claim owner / transition worker to your env identity.",
            "",
            "## Owned files",
            owned,
            "",
            "## Goal (shared)",
            goal.strip() or "(no goal provided)",
            "",
            f"Task index {task_index} of {task_count}.",
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


def _pane_env_pairs(
    *,
    run_id: str | None = None,
    team_id: str | None = None,
    worker_id: str | None = None,
    leader_root: Path | str | None = None,
    state_root: Path | str | None = None,
    owner_token: str | None = None,
) -> list[tuple[str, str]]:
    """Allowlisted env + worker depth marker (secrets via -e, never pane argv)."""
    pairs = list(forwarded_env())
    # Strip lifecycle escape hatches, then force team-worker marker.
    scrubbed = safe_supervised_child_env({k: v for k, v in pairs})
    out = [(k, v) for k, v in scrubbed.items()]
    # Ensure marker wins even if parent had a falsey value.
    out = [(k, v) for k, v in out if k not in WORKER_ENV_MARKERS]
    out.append((TEAM_WORKER_ENV, "1"))
    # Workers call ``omg team api`` from panes; gate must be present via -e
    # (not in madmax forwarded_env allowlist).
    out.append((EXPERIMENTAL_ENV, "1"))
    if run_id:
        out.append((TEAM_RUN_ID_ENV, str(run_id)))
    if team_id:
        out.append((TEAM_ID_ENV, str(team_id)))
    if worker_id:
        out.append((TEAM_WORKER_ID_ENV, str(worker_id)))
    if leader_root is not None:
        leader_resolved = str(Path(leader_root).resolve())
        out.append((TEAM_LEADER_ROOT_ENV, leader_resolved))
        # #100: pin OMG_PROJECT_ROOT to the validated leader so any incidental
        # CLI import inside the pane skips nested worktree .omg discovery.
        # Provider cwd remains the worktree (descriptor); this is control-plane
        # root only.
        out.append(("OMG_PROJECT_ROOT", leader_resolved))
    if state_root is not None:
        out.append((TEAM_STATE_ROOT_ENV, str(Path(state_root).resolve())))
    if owner_token:
        out.append((TEAM_OWNER_TOKEN_ENV, str(owner_token)))
    # Deduplicate by key (last wins).
    merged: dict[str, str] = {}
    for key, value in out:
        merged[key] = value
    return sorted(merged.items(), key=lambda kv: kv[0])


def build_supervisor_prefix(descriptor_path: Path | str) -> str:
    """Shell fragment: run ``team supervisor`` with a vetted argv descriptor.

    Pins ``PYTHONPATH`` to the repo that contains ``omg_cli`` so detached tmux
    panes (no parent env) can still import the package. The supervisor spawns
    the provider child — never ``worker-ready && provider`` (#99).
    """
    import shlex
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    py_path = shlex.quote(str(repo_root))
    desc = shlex.quote(str(Path(descriptor_path).resolve()))
    supervisor = shlex.join(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            "supervisor",
            "--descriptor",
            str(Path(descriptor_path).resolve()),
        ]
    )
    # Portable env prefix (dash/sh/bash/zsh) — avoid bash-only ${var:+…}.
    # ``desc`` is already embedded via shlex.join; keep py_path explicit.
    _ = desc  # path validated via resolve above
    return f"PYTHONPATH={py_path}:$PYTHONPATH {supervisor}"


def build_worker_ready_prefix() -> str:
    """Deprecated legacy helper (#99).

    Kept for import compatibility; new launches must use
    :func:`build_supervisor_prefix`. A v1 receipt alone cannot prove ready.
    """
    import shlex
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    py_path = shlex.quote(str(repo_root))
    ready = shlex.join([sys.executable, "-m", "omg_cli.main", "team", "worker-ready"])
    return f"PYTHONPATH={py_path}:$PYTHONPATH {ready}"


def wrap_pane_with_supervisor(descriptor_path: Path | str) -> str:
    """Pane command that runs the provider-aware supervisor (#99).

    The supervisor owns spawn + readiness receipts. Fail-closed on missing
    descriptor path.
    """
    path = Path(descriptor_path)
    if not str(path):
        raise TeamError("wrap_pane_with_supervisor requires a descriptor path")
    return build_supervisor_prefix(path)


def wrap_pane_with_worker_ready(pane_command: str) -> str:
    """Deprecated: pre-provider helper wrap (#99 false-green path).

    Retained only so old call sites/tests can detect the legacy pattern.
    New Team launches must use :func:`wrap_pane_with_supervisor`.
    """
    prefix = build_worker_ready_prefix()
    cmd = str(pane_command or "").strip()
    if not cmd:
        raise TeamError("wrap_pane_with_worker_ready requires a non-empty command")
    return f"{prefix} && {cmd}"


def materialize_supervisor_pane_command(
    *,
    descriptor_path: Path | str,
    provider: str,
    argv: Sequence[str],
    prompt_delivery: str = "prompt-file",
    prompt_file: Path | str | None = None,
    needs_pty: bool = False,
    cwd: Path | str | None = None,
    identity_basenames: Sequence[str] | None = None,
    provider_strategy: str | None = None,
    startup_strategy: str | None = None,
    # Prepublish authority (CLI launch intent) — required before live panes
    # when team.json is not yet authoritative.
    leader_root: Path | str | None = None,
    run_id: str | None = None,
    team_id: str | None = None,
    worker_id: str | None = None,
    owner_token: str | None = None,
    authority_generation: int = 0,
    authority_attempt: int = 1,
    publish_authority: bool = False,
) -> str:
    """Write provider descriptor (+ optional prepublish authority) and pane cmd.

    When ``publish_authority`` is true, binds root/run/team/worker, owner
    token, and descriptor path+digest under the team tree **before** the
    caller spawns panes. Supervisor admission fails closed without this
    record when ``team.json`` is absent.
    """
    from omg_cli.team.supervisor import (
        publish_supervisor_authority,
        write_provider_descriptor,
    )

    written = write_provider_descriptor(
        descriptor_path,
        provider=provider,
        argv=argv,
        prompt_delivery=prompt_delivery,
        prompt_file=prompt_file,
        needs_pty=needs_pty,
        cwd=cwd,
        identity_basenames=identity_basenames,
        provider_strategy=provider_strategy,
        startup_strategy=startup_strategy,
    )
    if publish_authority:
        if (
            leader_root is None
            or not run_id
            or not team_id
            or not worker_id
            or not owner_token
        ):
            raise TeamError(
                "materialize_supervisor_pane_command publish_authority "
                "requires leader_root, run_id, team_id, worker_id, owner_token"
            )
        try:
            publish_supervisor_authority(
                leader_root=leader_root,
                run_id=str(run_id),
                team_id=str(team_id),
                worker_id=str(worker_id),
                owner_token=str(owner_token),
                descriptor_path=written,
                generation=int(authority_generation),
                attempt=int(authority_attempt),
            )
        except Exception as exc:
            # Surface supervisor typed failures as TeamError for start/scale.
            from omg_cli.team.supervisor import SupervisorError

            if isinstance(exc, SupervisorError):
                raise TeamError(str(exc)) from exc
            raise
    return wrap_pane_with_supervisor(descriptor_path)


def build_fixture_pane_command(
    *,
    descriptor_path: Path | str | None = None,
    leader_root: Path | str | None = None,
    run_id: str | None = None,
    team_id: str | None = None,
    worker_id: str | None = None,
    owner_token: str | None = None,
    authority_generation: int = 0,
    authority_attempt: int = 1,
    publish_authority: bool = False,
) -> str:
    """Pane command for hermetic transport smoke (ACK fixture; no grok).

    Resolves ``tests/fixtures/team_worker_fixture.py`` relative to the repo
    checkout that contains ``omg_cli/``. Uses the #99 supervisor with provider
    ``fixture`` so readiness is process-proven, not a pre-provider helper.
    """
    import sys

    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "team_worker_fixture.py"
    )
    if not fixture.is_file():
        raise TeamError(
            f"fixture executor requested but missing fixture script: {fixture}"
        )
    if descriptor_path is None:
        # Shared fallback descriptor under /tmp is avoided — callers in
        # start_team pass a per-task path. Hermetic unit tests may omit it.
        raise TeamError(
            "build_fixture_pane_command requires descriptor_path (#99)"
        )
    return materialize_supervisor_pane_command(
        descriptor_path=descriptor_path,
        provider="fixture",
        argv=[sys.executable, str(fixture)],
        prompt_delivery="prompt-file",
        needs_pty=False,
        leader_root=leader_root,
        run_id=run_id,
        team_id=team_id,
        worker_id=worker_id,
        owner_token=owner_token,
        authority_generation=authority_generation,
        authority_attempt=authority_attempt,
        publish_authority=publish_authority,
    )

# ---------------------------------------------------------------------------
# tmux helpers (live path only — never called from dry_run)
# ---------------------------------------------------------------------------


def _tmux_run(
    args: Sequence[str],
    *,
    check: bool = False,
    socket_path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a tmux client command, optionally pinned to ``-S socket_path``."""
    argv: list[str] = ["tmux"]
    if socket_path is not None:
        if (
            not isinstance(socket_path, str)
            or not socket_path
            or "\x00" in socket_path
        ):
            raise TeamError(
                f"refused tmux -S with invalid socket_path {socket_path!r}"
            )
        argv.extend(["-S", socket_path])
    argv.extend(args)
    return subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=True,
    )


def _tmux_scope_from_launch(
    tmux_launch: Mapping[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Extract ``(socket_path, server)`` from ``_tmux_launch`` / team meta."""
    if not isinstance(tmux_launch, Mapping):
        return None, None
    from omg_cli.team.tmux import _intent_tmux_server

    server = _intent_tmux_server(tmux_launch)
    if server is None:
        return None, None
    return str(server["tmux_socket_path"]), server


def _require_plane_tmux_server(
    expected: Mapping[str, Any] | None,
    *,
    socket_path: str | None = None,
    action: str,
) -> dict[str, Any]:
    """Fail closed unless live tmux server matches WAL/launch identity."""
    from omg_cli.team.tmux import _require_tmux_server

    try:
        return _require_tmux_server(
            expected, socket_path=socket_path, action=action
        )
    except Exception as exc:
        # TmuxTeamError → TeamError for plane callers.
        raise TeamError(str(exc)) from exc


def _create_tmux_session(
    *,
    session: str,
    tasks: list[dict[str, Any]],
    env_pairs: list[tuple[str, str]],
) -> tuple[str, str]:
    """Create one session and return its exact tmux name/ID handle.

    Stamps ``tasks[0]["_tmux_launch"]`` with the creating server identity so
    nonce bind / receipt / rollback cannot act on a restarted server's ``$N``.
    """
    if not tmux_available():
        raise TeamError(
            "tmux is required for omg team start (non-dry-run).\n"
            "  Install: brew install tmux\n"
            "  Or use --dry-run to write team.json without launching."
        )
    if not tasks:
        raise TeamError("no tasks for tmux session")

    first = tasks[0]
    first_env_args = tmux_env_args(list(first.get("_env_pairs") or env_pairs))
    create = _tmux_run(
        [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{session_name}\t#{session_id}\t#{pid}\t#{socket_path}",
            "-s",
            session,
            "-n",
            str(first["task_id"]),
            "-c",
            str(first["worktree"]),
            *first_env_args,
            str(first["pane_command"]),
        ]
    )
    if create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip()
        raise TeamError(
            f"failed to create tmux session {session!r} (exit {create.returncode}): {err}"
        )
    parts = (create.stdout or "").strip().split("\t")
    if (
        len(parts) != 4
        or parts[0] != session
        or _TMUX_SESSION_ID.fullmatch(parts[1]) is None
        or not parts[2].isdigit()
        or not parts[3]
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
    from omg_cli.team.tmux import _server_identity_from_create

    try:
        server = _server_identity_from_create(
            pid=int(parts[2]), socket_path=parts[3].strip()
        )
    except Exception as exc:
        cleanup_error = _cleanup_created_tmux_session(handle)
        message = f"tmux create refused: {exc}"
        if cleanup_error:
            message += f"; {cleanup_error}"
        raise TeamError(message) from exc
    sock = str(server["tmux_socket_path"])
    tasks[0]["_tmux_launch"] = {
        "attach_mode": "detached",
        "session_owned": True,
        "leader_pane_id": None,
        "window_id": None,
        "attach_hint": f"tmux attach -t {handle[0]}",
        "session_id": handle[1],
        "tmux_socket_path": server["tmux_socket_path"],
        "tmux_server_pid": server["tmux_server_pid"],
        "tmux_server_pid_start": server["tmux_server_pid_start"],
    }

    try:
        from omg_cli.team.tmux import _tmux_run_if_identity

        for task in tasks[1:]:
            task_env_args = tmux_env_args(list(task.get("_env_pairs") or env_pairs))
            nw_argv = [
                "new-window",
                "-t",
                handle[1],
                "-n",
                str(task["task_id"]),
                "-c",
                str(task["worktree"]),
                *task_env_args,
                str(task["pane_command"]),
            ]
            # PID+start if-shell so a replacement server on the same socket
            # cannot receive subsequent worker windows before postcheck.
            nw = _tmux_run_if_identity(
                nw_argv,
                target=handle[1],
                expected_server=server,
                socket_path=sock,
                expected_session_id=handle[1],
            )
            if nw.returncode != 0:
                err = (nw.stderr or nw.stdout or "").strip()
                raise TeamError(
                    f"failed to create window for task {task['task_id']!r}: {err}"
                )

        option = _tmux_run(
            ["set-option", "-t", handle[1], "mouse", "on"], socket_path=sock
        )
        if option.returncode != 0:
            raise TeamError("failed to configure created tmux session")
        _require_plane_tmux_server(
            server, socket_path=sock, action="windows-topology create commit"
        )
    except (TeamError, OSError) as exc:
        cleanup_error = _cleanup_created_tmux_session(
            handle, socket_path=sock, expected_server=server
        )
        if cleanup_error:
            raise TeamError(f"{exc}; {cleanup_error}") from exc
        raise
    return handle


def _cleanup_created_tmux_session(
    handle: tuple[str, str],
    *,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> str | None:
    """Kill only the immutable ID returned by ``tmux new-session`` and verify.

    When *expected_server* is set, refuse to kill a same-numbered ``$N`` on a
    restarted/replaced tmux server (pid + start-id gated).
    """
    _session_name, session_id = handle
    if expected_server is not None:
        from omg_cli.team.tmux import _cleanup_session

        return _cleanup_session(
            handle, socket_path=socket_path, expected_server=expected_server
        )
    try:
        _tmux_run(["kill-session", "-t", session_id], socket_path=socket_path)
        probe = _tmux_run(
            ["has-session", "-t", session_id], socket_path=socket_path
        )
    except OSError as exc:
        return f"created tmux session cleanup failed: {exc}"
    if probe.returncode != 1:
        return "created tmux session cleanup could not verify disappearance"
    return None


_LAUNCH_PANE_SNAPSHOT_FMT = (
    "#{pane_id}\t#{pane_pid}\t#{session_id}\t#{window_id}\t"
    "#{@omg_launch_nonce}"
)


def _snapshot_launch_pane_identities(
    *,
    expected_session_id: str,
    expected_pane_ids: Sequence[str],
    expected_window_id: str | None = None,
    expected_launch_nonce: str | None = None,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """One ``list-panes`` snapshot for receipt commit (anti split-brain).

    Prefer ``list-panes -t <window_id>`` when the Team window is known (inside
    mode). Refuse commit when any expected pane left ``session_id`` /
    ``window_id``, or when a bound launch nonce is present but mismatched.
    When *expected_server* is set, refuse unless the live server matches before
    accepting pane PIDs into the immutable receipt.
    """
    if expected_server is not None:
        _require_plane_tmux_server(
            expected_server,
            socket_path=socket_path,
            action="launch pane identity snapshot",
        )
    if (
        not isinstance(expected_session_id, str)
        or _TMUX_SESSION_ID.fullmatch(expected_session_id) is None
    ):
        raise TeamError("tmux launch identity snapshot requires session id")
    expected = [
        pane_id
        for pane_id in expected_pane_ids
        if isinstance(pane_id, str) and _TMUX_PANE_ID.fullmatch(pane_id) is not None
    ]
    if not expected or len(expected) != len(list(expected_pane_ids)):
        raise TeamError("tmux launch identity snapshot requires exact pane ids")
    target: str
    argv: list[str]
    if (
        isinstance(expected_window_id, str)
        and _TMUX_WINDOW_ID.fullmatch(expected_window_id) is not None
    ):
        target = expected_window_id
        argv = [
            "list-panes",
            "-t",
            target,
            "-F",
            _LAUNCH_PANE_SNAPSHOT_FMT,
        ]
    else:
        target = expected_session_id
        argv = [
            "list-panes",
            "-s",
            "-t",
            target,
            "-F",
            _LAUNCH_PANE_SNAPSHOT_FMT,
        ]
    try:
        listed = _tmux_run(argv, socket_path=socket_path)
    except OSError as exc:
        raise TeamError(f"tmux launch identity snapshot failed: {exc}") from exc
    if listed.returncode != 0:
        raise TeamError("tmux launch identity snapshot failed")
    by_pane: dict[str, dict[str, Any]] = {}
    for line in (listed.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 5:
            continue
        pane_id, pid_s, session_id, window_id, nonce_s = parts
        if _TMUX_PANE_ID.fullmatch(pane_id) is None:
            continue
        if session_id != expected_session_id:
            continue
        if (
            isinstance(expected_window_id, str)
            and _TMUX_WINDOW_ID.fullmatch(expected_window_id) is not None
            and window_id != expected_window_id
        ):
            continue
        try:
            pane_pid = int(pid_s)
        except ValueError:
            continue
        if pane_pid <= 0:
            continue
        if _TMUX_WINDOW_ID.fullmatch(window_id) is None:
            continue
        live_nonce = _parse_tmux_launch_nonce(nonce_s)
        by_pane[pane_id] = {
            "pane_id": pane_id,
            "pane_pid": pane_pid,
            "session_id": session_id,
            "window_id": window_id,
            "launch_nonce": live_nonce,
        }
    for pane_id in expected:
        row = by_pane.get(pane_id)
        if row is None:
            raise TeamError(
                "tmux launch identity snapshot missing worker pane "
                f"{pane_id} in expected session/window"
            )
        if expected_launch_nonce is not None:
            live_nonce = row.get("launch_nonce")
            if live_nonce != expected_launch_nonce:
                raise TeamError(
                    f"tmux launch nonce mismatch on pane {pane_id} before receipt"
                )
    if expected_server is not None:
        _require_plane_tmux_server(
            expected_server,
            socket_path=socket_path,
            action="launch pane identity snapshot commit",
        )
    return {pane_id: by_pane[pane_id] for pane_id in expected}


def _list_pane_identities(
    session: str,
    *,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> dict[int, tuple[str, int]]:
    """Map slot index to exact tmux pane identity and pane PID.

    *Windows* topology: one pane per window — slot equals ``window_index``.
    *Split* topology: multiple panes share one window — slot equals
    ``pane_index`` (creation order), matching task ``window_index`` slots.
    Ambiguous multi-window multi-pane layouts fail closed (empty map).

    When *expected_server* is set, authorize against that server before and
    after the ``-S``-scoped list so a replacement cannot publish foreign
    ``%N``/pane PIDs into the immutable launch receipt.

    Accepts list-panes rows as either::

        window_index\\tpane_index\\tpane_id\\tpane_pid   (preferred)
        window_index\\tpane_id\\tpane_pid                 (legacy windows mocks)
    """
    if expected_server is not None:
        _require_plane_tmux_server(
            expected_server,
            socket_path=socket_path,
            action="list pane identities",
        )
    r = _tmux_run(
        [
            "list-panes",
            "-s",
            "-t",
            session,
            "-F",
            "#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}",
        ],
        socket_path=socket_path,
    )
    if r.returncode != 0:
        return {}
    rows: list[tuple[int, int | None, str, int]] = []
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 4 and _TMUX_PANE_ID.fullmatch(parts[2]) is not None:
            try:
                window_index = int(parts[0])
                pane_index = int(parts[1])
                pane_pid = int(parts[3])
            except ValueError:
                continue
            pane_id = parts[2]
        elif len(parts) == 3 and _TMUX_PANE_ID.fullmatch(parts[1]) is not None:
            # Legacy 3-field rows (windows topology / hermetic mocks).
            try:
                window_index = int(parts[0])
                pane_pid = int(parts[2])
            except ValueError:
                continue
            pane_index = None
            pane_id = parts[1]
        else:
            continue
        if pane_pid <= 0:
            return {}
        rows.append((window_index, pane_index, pane_id, pane_pid))
    if not rows:
        return {}
    windows = {row[0] for row in rows}
    out: dict[int, tuple[str, int]] = {}
    if len(rows) == len(windows):
        # One pane per window (legacy windows topology).
        for window_index, _pane_index, pane_id, pane_pid in rows:
            if window_index in out:
                return {}
            out[window_index] = (pane_id, pane_pid)
    elif len(windows) == 1 and all(row[1] is not None for row in rows):
        # Split topology: key by pane_index within the single window.
        for _window_index, pane_index, pane_id, pane_pid in sorted(
            rows, key=lambda row: int(row[1] or 0)
        ):
            assert pane_index is not None
            if pane_index in out:
                return {}
            out[pane_index] = (pane_id, pane_pid)
    else:
        # Mixed multi-window multi-pane — refuse ambiguous identity.
        return {}
    if expected_server is not None:
        _require_plane_tmux_server(
            expected_server,
            socket_path=socket_path,
            action="list pane identities commit",
        )
    return out


def _list_pane_pids(
    session: str,
    *,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> dict[int, int]:
    """Compatibility view used by dynamic scaling; not process authority."""
    return {
        window_index: pane_pid
        for window_index, (_pane_id, pane_pid) in _list_pane_identities(
            session,
            socket_path=socket_path,
            expected_server=expected_server,
        ).items()
    }


def _read_tmux_session_identity(
    session: str,
    *,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> tuple[str, str] | None:
    if expected_server is not None:
        try:
            _require_plane_tmux_server(
                expected_server,
                socket_path=socket_path,
                action="session identity readback",
            )
        except TeamError:
            return None
    r = _tmux_run(
        ["display-message", "-p", "-t", session, "#{session_name}\t#{session_id}"],
        socket_path=socket_path,
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
    if expected_server is not None:
        try:
            _require_plane_tmux_server(
                expected_server,
                socket_path=socket_path,
                action="session identity readback commit",
            )
        except TeamError:
            return None
    return parts[0], parts[1]


def _parse_tmux_launch_nonce(value: str | None) -> str | None:
    text = (value or "").strip()
    if len(text) != 32 or any(ch not in "0123456789abcdef" for ch in text):
        return None
    return text


def _read_tmux_launch_nonce(session: str) -> str | None:
    """Read session-scoped launch nonce (owned detached sessions only)."""
    try:
        r = _tmux_run(["show-options", "-v", "-t", session, LAUNCH_NONCE_OPTION])
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return _parse_tmux_launch_nonce(r.stdout)


def _probe_tmux_launch_nonce_for_pane(
    pane_id: str,
    session: str,
    *,
    allow_session_fallback: bool = False,
) -> tuple[str | None, bool]:
    """Probe pane-scoped launch nonce with an explicit ok/unknown bit.

    Returns ``(nonce, True)`` only when a valid 32-hex nonce was read.
    Returns ``(None, False)`` on OSError, non-zero tmux, malformed, or missing
    — callers must treat that as UNKNOWN, never as a confirmed foreign nonce.
    """
    if isinstance(pane_id, str) and _TMUX_PANE_ID.fullmatch(pane_id) is not None:
        try:
            r = _tmux_run(
                ["show-options", "-p", "-v", "-t", pane_id, LAUNCH_NONCE_OPTION]
            )
        except OSError:
            return None, False
        if r.returncode != 0:
            return None, False
        parsed = _parse_tmux_launch_nonce(r.stdout)
        if parsed is not None:
            return parsed, True
        # Malformed/missing pane nonce: do not fall through unless legacy.
        if not allow_session_fallback:
            return None, False
    if allow_session_fallback:
        nonce = _read_tmux_launch_nonce(session)
        return nonce, nonce is not None
    return None, False


def _read_tmux_launch_nonce_for_pane(
    pane_id: str,
    session: str,
    *,
    allow_session_fallback: bool = False,
) -> str | None:
    """Read pane-scoped launch nonce; session fallback is legacy-only.

    New receipts with a valid ``pane_id`` must fail closed on OSError, non-zero
    tmux status, or malformed output — never silently adopt a shared session
    nonce (concurrent Teams in one session overwrite that option).

    Note: ``None`` means probe unknown *or* absent — use
    :func:`_probe_tmux_launch_nonce_for_pane` when UNKNOWN must not collapse
    into DEAD_OR_FOREIGN (relaunch / side-effect paths).
    """
    nonce, ok = _probe_tmux_launch_nonce_for_pane(
        pane_id, session, allow_session_fallback=allow_session_fallback
    )
    return nonce if ok else None


def _read_tmux_launch_nonce_for_window(window_id: str) -> str | None:
    if not isinstance(window_id, str) or _TMUX_WINDOW_ID.fullmatch(window_id) is None:
        return None
    try:
        r = _tmux_run(
            ["show-options", "-w", "-v", "-t", window_id, LAUNCH_NONCE_OPTION]
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return _parse_tmux_launch_nonce(r.stdout)


def _bind_tmux_launch_nonce(
    *,
    session_id: str,
    launch_nonce: str,
    window_id: str | None,
    pane_ids: Sequence[str],
    session_owned: bool = True,
    socket_path: str | None = None,
    expected_server: Mapping[str, Any] | None = None,
) -> None:
    """Bind nonce so concurrent Teams in one session cannot overwrite each other.

    Pane/window options isolate workers. Session-scoped stamp is only for
    Team-owned detached sessions — inside mode must not clobber a shared
    ``@omg_launch_nonce`` on the leader session.

    When *expected_server* is set, each ``set-option`` runs under a PID+start
    ``if-shell`` gate (same pattern as create/kill) so a replacement server on
    the socket cannot retain a foreign nonce mutation between Python precheck
    and postcheck. Pre/post probes remain defense-in-depth only.
    """
    from omg_cli.team.tmux import _tmux_run_if_identity

    if expected_server is not None:
        _require_plane_tmux_server(
            expected_server,
            socket_path=socket_path,
            action="launch nonce bind",
        )
    bound = False
    win_for_gate = (
        window_id
        if isinstance(window_id, str) and _TMUX_WINDOW_ID.fullmatch(window_id)
        else None
    )

    def _set_option(argv: list[str], *, target: str) -> subprocess.CompletedProcess[str]:
        if expected_server is not None:
            return _tmux_run_if_identity(
                argv,
                target=target,
                expected_server=expected_server,
                socket_path=socket_path,
                window_id=win_for_gate if target != session_id else None,
                expected_session_id=session_id,
            )
        return _tmux_run(argv, socket_path=socket_path)

    for pane_id in pane_ids:
        if not isinstance(pane_id, str) or _TMUX_PANE_ID.fullmatch(pane_id) is None:
            continue
        try:
            option = _set_option(
                [
                    "set-option",
                    "-p",
                    "-t",
                    pane_id,
                    LAUNCH_NONCE_OPTION,
                    launch_nonce,
                ],
                target=pane_id,
            )
        except OSError as exc:
            raise TeamError(f"failed to bind tmux launch nonce on pane: {exc}") from exc
        if option.returncode != 0:
            raise TeamError(f"failed to bind tmux launch nonce on pane {pane_id}")
        bound = True
    if win_for_gate is not None:
        try:
            option = _set_option(
                [
                    "set-option",
                    "-w",
                    "-t",
                    win_for_gate,
                    LAUNCH_NONCE_OPTION,
                    launch_nonce,
                ],
                target=win_for_gate,
            )
        except OSError as exc:
            raise TeamError(f"failed to bind tmux launch nonce on window: {exc}") from exc
        if option.returncode != 0:
            raise TeamError("failed to bind tmux launch nonce on window")
        bound = True
    if session_owned:
        try:
            option = _set_option(
                [
                    "set-option",
                    "-t",
                    session_id,
                    LAUNCH_NONCE_OPTION,
                    launch_nonce,
                ],
                target=session_id,
            )
        except OSError as exc:
            raise TeamError(
                f"failed to bind tmux launch nonce on session: {exc}"
            ) from exc
        if option.returncode != 0:
            raise TeamError("failed to bind tmux launch nonce")
        bound = True
    if not bound:
        raise TeamError("failed to bind tmux launch nonce")
    if expected_server is not None:
        _require_plane_tmux_server(
            expected_server,
            socket_path=socket_path,
            action="launch nonce bind commit",
        )

def _persist_team_launch_receipt(
    root: Path,
    run_id: str,
    *,
    session: str,
    session_id: str,
    launch_nonce: str,
    tasks: Sequence[Mapping[str, Any]],
    intent_nonce: str | None = None,
    window_name: str | None = None,
    view_mode: str | None = None,
    layout: str | None = None,
    leader_pane_id: str | None = None,
    leader_pane_pid: int | None = None,
    window_id: str | None = None,
    session_owned: bool | None = None,
    attach_mode: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Persist the immutable process identity used by ``team stop``."""
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
    from omg_cli.team.topology import VIEW_MODES

    if intent_nonce is not None and (
        not isinstance(intent_nonce, str) or not intent_nonce
    ):
        raise TeamError("launch receipt intent_nonce must be a non-empty string")
    if window_name is not None and (
        not isinstance(window_name, str) or not window_name
    ):
        raise TeamError("launch receipt window_name must be a non-empty string")
    if (intent_nonce is None) != (window_name is None):
        raise TeamError(
            "launch receipt intent_nonce and window_name must both be set or both null"
        )
    if view_mode is not None and (
        not isinstance(view_mode, str) or view_mode not in VIEW_MODES
    ):
        raise TeamError(f"launch receipt view_mode unsupported: {view_mode!r}")
    if layout is not None and (not isinstance(layout, str) or not layout):
        raise TeamError("launch receipt layout must be a non-empty string")
    if leader_pane_id is not None and (
        not isinstance(leader_pane_id, str)
        or _TMUX_PANE_ID.fullmatch(leader_pane_id) is None
    ):
        raise TeamError("launch receipt leader_pane_id must be an exact pane id")
    if leader_pane_pid is not None and (
        isinstance(leader_pane_pid, bool)
        or not isinstance(leader_pane_pid, int)
        or leader_pane_pid <= 0
    ):
        raise TeamError("launch receipt leader_pane_pid must be a positive int")
    if window_id is not None and (
        not isinstance(window_id, str) or _TMUX_WINDOW_ID.fullmatch(window_id) is None
    ):
        raise TeamError("launch receipt window_id must be an exact window id")
    if session_owned is not None and not isinstance(session_owned, bool):
        raise TeamError("launch receipt session_owned must be a bool")
    if attach_mode is not None and (
        not isinstance(attach_mode, str) or attach_mode not in {"inside", "detached"}
    ):
        raise TeamError(f"launch receipt attach_mode unsupported: {attach_mode!r}")
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
        "intent_nonce": intent_nonce,
        "window_name": window_name,
        "view_mode": view_mode,
        "layout": layout,
        "leader_pane_id": leader_pane_id,
        "leader_pane_pid": leader_pane_pid,
        "window_id": window_id,
        "session_owned": session_owned,
        "attach_mode": attach_mode,
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
    *,
    unlink_new: bool = True,
) -> list[str]:
    """Restore snapshotted live-start files.

    When ``unlink_new`` is False, paths that did not exist at snapshot time are
    left in place (keep newly published receipt / team.json). Use that when
    tmux cleanup is unproven or the launch-intent WAL was already cleared —
    deleting authority would hide a live worker.
    """
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes

    errors: list[str] = []
    for path, (body, mode) in snapshots.items():
        try:
            if body is None:
                if not unlink_new:
                    continue
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


def _remove_team_worktree(root: Path, worktree: Path) -> str | None:
    """Best-effort remove a prepared team worktree. Returns error text or None."""

    root = Path(root).resolve()
    wt = Path(worktree)
    if not wt.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0 and not wt.exists():
            return None
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        import shutil

        if wt.is_dir() and not wt.is_symlink():
            shutil.rmtree(wt, ignore_errors=True)
        elif wt.exists():
            wt.unlink(missing_ok=True)
    except OSError as exc:
        return f"worktree remove {wt}: {exc}"
    return None if not wt.exists() else f"worktree still present: {wt}"


def _rollback_partial_team_start(
    root: Path,
    run_id: str,
    *,
    created_run: bool,
    worktrees: Sequence[Path],
    team_dir_path: Path | None,
    remove_ownership: bool = True,
    ownership_backup: bytes | None = None,
    file_backups: Mapping[Path, bytes | None] | None = None,
) -> list[str]:
    """Undo pre-commit start debris (issue #17 all-or-nothing).

    Only destroys resources **this invocation created**:
    - ``worktrees`` must be newly prepared paths (not pre-existing --run trees)
    - ``team_dir_path`` only when this start created the directory
    - ownership: restore ``ownership_backup`` when we overwrote a prior file;
      otherwise unlink only if ``remove_ownership`` (new write / new run)
    - ``file_backups``: path → prior bytes (or None if this start created the file)

    Reverse order: worktrees → team dir (or file restore) → ownership → active.
    Never raises; returns actionable error strings for diagnostics.
    """
    from omg_cli.workers import ownership_manifest_path

    errors: list[str] = []
    root = Path(root).resolve()
    rid = str(run_id)

    for wt in reversed(list(worktrees)):
        err = _remove_team_worktree(root, Path(wt))
        if err:
            errors.append(err)

    if team_dir_path is not None:
        tdir = Path(team_dir_path)
        if tdir.is_dir() and not tdir.is_symlink():
            try:
                import shutil

                shutil.rmtree(tdir, ignore_errors=True)
            except OSError as exc:
                errors.append(f"team dir remove {tdir}: {exc}")
    elif file_backups:
        # Reused team dir: restore or remove files this start overwrote/created.
        for fpath, prior in file_backups.items():
            path = Path(fpath)
            try:
                if prior is None:
                    if path.is_file() or path.is_symlink():
                        path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(f".{path.name}.{os.getpid()}.rb.tmp")
                    try:
                        tmp.write_bytes(prior)
                        os.replace(tmp, path)
                    finally:
                        if tmp.exists():
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
            except OSError as exc:
                errors.append(f"file restore {path}: {exc}")

    try:
        mpath = ownership_manifest_path(root, rid)
        if ownership_backup is not None:
            # Restore pre-start ownership when --run reuses an existing run.
            try:
                mpath.parent.mkdir(parents=True, exist_ok=True)
                tmp = mpath.with_name(f".{mpath.name}.{os.getpid()}.rb.tmp")
                try:
                    tmp.write_bytes(ownership_backup)
                    os.replace(tmp, mpath)
                finally:
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
            except OSError as exc:
                errors.append(f"ownership manifest restore: {exc}")
        elif remove_ownership and (mpath.is_file() or mpath.is_symlink()):
            mpath.unlink(missing_ok=True)
    except OSError as exc:
        errors.append(f"ownership manifest remove: {exc}")

    if created_run:
        try:
            clear_active(root, rid)
        except OSError as exc:
            errors.append(f"clear active: {exc}")
        # Leave run directory for forensics; clearing active unblocks relaunch.
    return errors


def _load_team_launch_receipt(
    root: Path, run_id: str, meta: Mapping[str, Any]
) -> dict[str, Any]:
    from omg_cli.contracts.path_keys import read_managed_regular_bytes
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    path = team_launch_receipt_path(root, run_id)
    try:
        body = read_managed_regular_bytes(path)
    except (OSError, ValueError) as exc:
        raise TeamError("immutable team launch receipt missing") from exc
    parsed = parse_canonical_json_bytes(body)
    if not isinstance(parsed, dict):
        raise TeamError("team launch receipt must be an object")
    # Exact key sets: v1 (#106) lacked intent binding; v2 adds intent fields;
    # v3 (#96) binds view_mode / leader-window topology for stop.
    legacy_required = {
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
    v2_required = legacy_required | {
        "intent_nonce",
        "window_name",
    }
    v3_required = v2_required | {
        "view_mode",
        "layout",
        "leader_pane_id",
        "leader_pane_pid",
        "window_id",
        "session_owned",
        "attach_mode",
    }
    schema_version = parsed.get("schema_version")
    if (
        (
            schema_version == LEGACY_LAUNCH_RECEIPT_SCHEMA_VERSION
            and set(parsed) != legacy_required
        )
        or (
            schema_version == V2_LAUNCH_RECEIPT_SCHEMA_VERSION
            and set(parsed) != v2_required
        )
        or (
            schema_version == LAUNCH_RECEIPT_SCHEMA_VERSION
            and set(parsed) != v3_required
        )
        or schema_version
        not in {
            LEGACY_LAUNCH_RECEIPT_SCHEMA_VERSION,
            V2_LAUNCH_RECEIPT_SCHEMA_VERSION,
            LAUNCH_RECEIPT_SCHEMA_VERSION,
        }
    ):
        raise TeamError("team launch receipt keys mismatch")
    if schema_version in {
        V2_LAUNCH_RECEIPT_SCHEMA_VERSION,
        LAUNCH_RECEIPT_SCHEMA_VERSION,
    }:
        intent_nonce = parsed.get("intent_nonce")
        window_name = parsed.get("window_name")
        if (intent_nonce is None) != (window_name is None):
            raise TeamError("team launch receipt identity mismatch")
        if intent_nonce is not None and (
            not isinstance(intent_nonce, str)
            or not intent_nonce
            or not isinstance(window_name, str)
            or not window_name
        ):
            raise TeamError("team launch receipt identity mismatch")
    if schema_version == LAUNCH_RECEIPT_SCHEMA_VERSION:
        from omg_cli.team.topology import VIEW_MODES

        view_mode = parsed.get("view_mode")
        if view_mode is not None and (
            not isinstance(view_mode, str) or view_mode not in VIEW_MODES
        ):
            raise TeamError("team launch receipt identity mismatch")
        layout = parsed.get("layout")
        if layout is not None and (not isinstance(layout, str) or not layout):
            raise TeamError("team launch receipt identity mismatch")
        leader_pane_id = parsed.get("leader_pane_id")
        if leader_pane_id is not None and (
            not isinstance(leader_pane_id, str)
            or _TMUX_PANE_ID.fullmatch(leader_pane_id) is None
        ):
            raise TeamError("team launch receipt identity mismatch")
        leader_pane_pid = parsed.get("leader_pane_pid")
        if leader_pane_pid is not None and (
            isinstance(leader_pane_pid, bool)
            or not isinstance(leader_pane_pid, int)
            or leader_pane_pid <= 0
        ):
            raise TeamError("team launch receipt identity mismatch")
        window_id = parsed.get("window_id")
        if window_id is not None and (
            not isinstance(window_id, str)
            or _TMUX_WINDOW_ID.fullmatch(window_id) is None
        ):
            raise TeamError("team launch receipt identity mismatch")
        session_owned = parsed.get("session_owned")
        if session_owned is not None and not isinstance(session_owned, bool):
            raise TeamError("team launch receipt identity mismatch")
        attach_mode = parsed.get("attach_mode")
        if attach_mode is not None and attach_mode not in {"inside", "detached"}:
            raise TeamError("team launch receipt identity mismatch")
    if (
        parsed["store_kind"] != "team_launch_receipt"
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
    # Fail-closed: bool / str / negative must not skip gen-0 continuity.
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise TeamError("team identity generation is invalid")
    if not isinstance(expected_tasks, list):
        raise TeamError("team launch receipt task count mismatch")
    # Always validate receipt task row shape (independent of meta.tasks length).
    for actual in parsed["tasks"]:
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
    if generation == 0:
        if len(expected_tasks) != len(parsed["tasks"]):
            raise TeamError("team launch receipt task count mismatch")
        for expected, actual in zip(expected_tasks, parsed["tasks"]):
            if not isinstance(expected, Mapping) or any(
                expected.get(field) != actual.get(field) for field in actual
            ):
                raise TeamError("team.json differs from immutable launch receipt")
    return parsed


_LEGACY_IDENTITY_ROW_KEYS = frozenset(
    {
        "task_id",
        "window_index",
        "pane_id",
        "pid",
        "pgid",
        "pid_start",
    }
)
_V2_IDENTITY_ROW_KEYS = _LEGACY_IDENTITY_ROW_KEYS | {
    "window_id",
    "window_nonce",
}
# Back-compat alias used by older call sites / tests.
_IDENTITY_ROW_KEYS = _V2_IDENTITY_ROW_KEYS
_V3_IDENTITY_ROW_KEYS = frozenset(
    {
        "task_id",
        "logical_worker_index",
        "attempt",
        "window_index",  # dual-write alias of logical index for locked consumers
        "window_id",
        "window_nonce",  # dual-write alias of window_owner_nonce
        "window_owner_nonce",
        "pane_id",
        "pane_owner_kind",
        "pane_owner_nonce",
        "pid",
        "pgid",
        "pid_start",
    }
)


def _identity_rows_v2(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project the #98 v2 identity row shape (window_id / window_nonce)."""
    return [
        {
            "task_id": raw.get("task_id"),
            "window_index": raw.get(
                "window_index", raw.get("logical_worker_index")
            ),
            "window_id": raw.get("window_id"),
            "window_nonce": raw.get(
                "window_nonce", raw.get("window_owner_nonce")
            ),
            "pane_id": raw.get("pane_id"),
            "pid": raw.get("pid"),
            "pgid": raw.get("pgid"),
            "pid_start": raw.get("pid_start"),
        }
        for raw in tasks
    ]


def _identity_rows(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Default identity projection for new writers (#102 → v3)."""
    return _identity_rows_v3(tasks)


def _identity_rows_v3(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project the #102 v3 identity row (logical slot + pane owner evidence)."""
    return [_normalize_identity_row(raw) for raw in tasks]


def _normalize_identity_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize any receipt/task row into the in-memory v3 shape.

    Never writes back to disk. v1/v2 rows map:
    - logical_worker_index ← window_index
    - attempt ← explicit attempt or 1
    - window_owner_nonce ← window_nonce
    - pane_owner_nonce ← pane_owner_nonce / window_nonce / relaunch nonce
    Dual-writes window_nonce and window_index for locked scale consumers.
    """
    tid = raw.get("task_id")
    logical = raw.get("logical_worker_index")
    if logical is None:
        logical = raw.get("window_index")
    # Preserve explicit window_index when present (legacy live index dual-write);
    # logical_worker_index is the #102 authority for ordering.
    window_index = raw.get("window_index")
    if window_index is None:
        window_index = logical
    attempt = raw.get("attempt", 1)
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
    ):
        attempt = 1
    window_nonce = raw.get("window_owner_nonce", raw.get("window_nonce"))
    pane_owner_nonce = raw.get("pane_owner_nonce")
    if pane_owner_nonce is None:
        pane_owner_nonce = raw.get("relaunch_nonce") or window_nonce
    pane_owner_kind = raw.get("pane_owner_kind")
    if not isinstance(pane_owner_kind, str) or not pane_owner_kind:
        if raw.get("relaunch_nonce"):
            pane_owner_kind = "relaunch_nonce"
        elif window_nonce:
            pane_owner_kind = "window_nonce"
        else:
            pane_owner_kind = "launch_nonce"
    return {
        "task_id": tid,
        "logical_worker_index": logical,
        "attempt": attempt,
        "window_index": window_index,
        "window_id": raw.get("window_id"),
        "window_nonce": window_nonce,
        "window_owner_nonce": window_nonce,
        "pane_id": raw.get("pane_id"),
        "pane_owner_kind": pane_owner_kind,
        "pane_owner_nonce": pane_owner_nonce,
        "pid": raw.get("pid"),
        "pgid": raw.get("pgid"),
        "pid_start": raw.get("pid_start"),
    }


def _normalize_identity_receipt(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """In-memory normalize of a persisted identity receipt (any schema)."""
    out = dict(parsed)
    out["tasks_before"] = [
        _normalize_identity_row(row)
        for row in (parsed.get("tasks_before") or [])
        if isinstance(row, Mapping)
    ]
    out["tasks_after"] = [
        _normalize_identity_row(row)
        for row in (parsed.get("tasks_after") or [])
        if isinstance(row, Mapping)
    ]
    return out


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
    scale_intent: Mapping[str, Any] | None = None,
    topology_mode: str | None = None,
    topology_before_sha256: str | None = None,
    topology_after_sha256: str | None = None,
    operation_intent: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Append one immutable scale generation to the launch identity chain."""
    from omg_cli.contracts.path_keys import (
        DATA_FILE_MODE,
        atomic_write_bytes,
        fsync_existing_managed_dir,
        read_managed_regular_bytes,
    )
    from omg_cli.contracts.state_schemas import require_sha256
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    if generation <= 0:
        raise TeamError("scaled identity generation must be positive")
    require_sha256(previous_receipt_sha256, label="previous_receipt_sha256")
    if operation not in {"add", "remove", "relaunch"}:
        raise TeamError("scaled identity receipt operation mismatch")
    if not isinstance(topology_mode, str) or not topology_mode:
        raise TeamError("v3 identity receipt requires topology_mode")
    if operation_intent is None:
        raise TeamError("v3 identity receipt requires operation_intent")
    scale_intent_body = (
        canonical_json_bytes(scale_intent) if scale_intent is not None else None
    )
    operation_intent_body = canonical_json_bytes(operation_intent)
    receipt = {
        "store_kind": "team_identity_receipt",
        "schema_version": IDENTITY_RECEIPT_SCHEMA_VERSION,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "session_name": session,
        "session_id": session_id,
        "launch_nonce": launch_nonce,
        "generation": generation,
        "previous_receipt_sha256": previous_receipt_sha256,
        "operation": operation,
        "receipt_nonce": uuid.uuid4().hex,
        "tasks_before": _identity_rows_v3(tasks_before),
        "tasks_after": _identity_rows_v3(tasks_after),
        "scale_intent": dict(scale_intent) if scale_intent is not None else None,
        "scale_intent_sha256": (
            sha256_hex(scale_intent_body) if scale_intent_body is not None else None
        ),
        "topology_mode": topology_mode,
        "topology_before_sha256": topology_before_sha256,
        "topology_after_sha256": topology_after_sha256,
        "operation_intent": (
            dict(operation_intent) if operation_intent is not None else None
        ),
        "operation_intent_sha256": (
            sha256_hex(operation_intent_body)
            if operation_intent_body is not None
            else None
        ),
    }
    body = canonical_json_bytes(receipt)
    receipt_hash = sha256_hex(body)
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
    except OSError as exc:
        try:
            published = read_managed_regular_bytes(receipt_path)
        except (OSError, ValueError):
            raise exc
        if published != body:
            raise exc
        fsync_existing_managed_dir(receipt_path.parent)
        return receipt, receipt_hash
    return receipt, receipt_hash


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
    from omg_cli.contracts.path_keys import read_managed_regular_bytes
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    try:
        parsed = parse_canonical_json_bytes(read_managed_regular_bytes(path))
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
    from omg_cli.contracts.path_keys import read_managed_regular_bytes
    from omg_cli.contracts.state_schemas import require_sha256
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
        sha256_hex,
    )

    launch = _load_team_launch_receipt(root, run_id, meta)
    chain = [launch]
    # Generation-zero launch receipts predate immutable scaled-window handles.
    # Normalize their rows so an upgraded writer can append the stronger shape
    # without invalidating the existing receipt hash.
    previous_rows = [_normalize_identity_row(row) for row in launch["tasks"]]
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
        try:
            body = read_managed_regular_bytes(path)
        except (OSError, ValueError) as exc:
            raise TeamError(
                f"team identity receipt generation {expected_generation} missing"
            ) from exc
        parsed = parse_canonical_json_bytes(body)
        if not isinstance(parsed, dict):
            raise TeamError("team identity receipt must be an object")
        legacy_required = {
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
        v2_required = legacy_required | {
            "scale_intent",
            "scale_intent_sha256",
        }
        v3_required = v2_required | {
            "topology_mode",
            "topology_before_sha256",
            "topology_after_sha256",
            "operation_intent",
            "operation_intent_sha256",
        }
        schema_version = parsed.get("schema_version")
        if (
            (
                schema_version == LEGACY_IDENTITY_RECEIPT_SCHEMA_VERSION
                and set(parsed) != legacy_required
            )
            or (
                schema_version == V2_IDENTITY_RECEIPT_SCHEMA_VERSION
                and set(parsed) != v2_required
            )
            or (
                schema_version == IDENTITY_RECEIPT_SCHEMA_VERSION
                and set(parsed) != v3_required
            )
            or schema_version
            not in {
                LEGACY_IDENTITY_RECEIPT_SCHEMA_VERSION,
                V2_IDENTITY_RECEIPT_SCHEMA_VERSION,
                IDENTITY_RECEIPT_SCHEMA_VERSION,
            }
        ):
            raise TeamError("team identity receipt keys mismatch")
        if (
            parsed["store_kind"] != "team_identity_receipt"
            or parsed["writer"] != CLI_WRITER
            or parsed["run_id"] != run_id
            or parsed["session_name"] != launch["session_name"]
            or parsed["session_id"] != launch["session_id"]
            or parsed["launch_nonce"] != launch["launch_nonce"]
            or parsed["generation"] != expected_generation
            or parsed["previous_receipt_sha256"] != previous_hash
            or parsed["operation"] not in {"add", "remove", "relaunch"}
            or not isinstance(parsed["receipt_nonce"], str)
            or len(parsed["receipt_nonce"]) != 32
            or not isinstance(parsed["tasks_before"], list)
            or not isinstance(parsed["tasks_after"], list)
        ):
            raise TeamError("team identity receipt chain mismatch")
        if schema_version in {
            V2_IDENTITY_RECEIPT_SCHEMA_VERSION,
            IDENTITY_RECEIPT_SCHEMA_VERSION,
        }:
            scale_intent = parsed["scale_intent"]
            scale_intent_sha256 = parsed["scale_intent_sha256"]
            if scale_intent is None or scale_intent_sha256 is None:
                if scale_intent is not None or scale_intent_sha256 is not None:
                    raise TeamError("team identity receipt scale intent mismatch")
            else:
                if not isinstance(scale_intent, Mapping):
                    raise TeamError("team identity receipt scale intent mismatch")
                try:
                    require_sha256(scale_intent_sha256, label="scale_intent_sha256")
                except ValueError as exc:
                    raise TeamError(
                        "team identity receipt scale intent mismatch"
                    ) from exc
                if (
                    sha256_hex(canonical_json_bytes(scale_intent))
                    != scale_intent_sha256
                ):
                    raise TeamError("team identity receipt scale intent mismatch")
        if schema_version == IDENTITY_RECEIPT_SCHEMA_VERSION:
            op_intent = parsed.get("operation_intent")
            op_hash = parsed.get("operation_intent_sha256")
            if op_intent is None or op_hash is None:
                raise TeamError(
                    "team identity receipt v3 requires operation_intent"
                )
            if not isinstance(op_intent, Mapping):
                raise TeamError("team identity receipt operation intent mismatch")
            try:
                require_sha256(op_hash, label="operation_intent_sha256")
            except ValueError as exc:
                raise TeamError(
                    "team identity receipt operation intent mismatch"
                ) from exc
            if sha256_hex(canonical_json_bytes(op_intent)) != op_hash:
                raise TeamError("team identity receipt operation intent mismatch")
            topo_mode = parsed.get("topology_mode")
            if not isinstance(topo_mode, str) or not topo_mode:
                raise TeamError(
                    "team identity receipt v3 requires topology_mode"
                )
        normalized_rows: dict[str, list[dict[str, Any]]] = {}
        for field in ("tasks_before", "tasks_after"):
            for row in parsed[field]:
                if not isinstance(row, Mapping):
                    raise TeamError("team identity receipt task row mismatch")
                keys = set(row)
                if schema_version == LEGACY_IDENTITY_RECEIPT_SCHEMA_VERSION:
                    if keys != _LEGACY_IDENTITY_ROW_KEYS:
                        raise TeamError("team identity receipt task row mismatch")
                elif schema_version == V2_IDENTITY_RECEIPT_SCHEMA_VERSION:
                    if keys not in {_LEGACY_IDENTITY_ROW_KEYS, _V2_IDENTITY_ROW_KEYS}:
                        raise TeamError("team identity receipt task row mismatch")
                else:
                    if keys != _V3_IDENTITY_ROW_KEYS:
                        raise TeamError("team identity receipt task row mismatch")
            normalized_rows[field] = [
                _normalize_identity_row(row) for row in parsed[field]
            ]
        if normalized_rows["tasks_before"] != previous_rows:
            raise TeamError("team identity receipt task continuity mismatch")
        previous_hash = sha256_hex(canonical_json_bytes(parsed))
        previous_rows = normalized_rows["tasks_after"]
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
    if [_normalize_identity_row(t) for t in expected_active] != previous_rows:
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
    topology: str = "windows",
    team_id: str = "team",
    owner_token: str | None = None,
    executor: str | None = None,
    detach: bool = False,
    view_mode: str | None = None,
    worker_topology: str | None = None,
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
    topology:
        ``windows`` (legacy ``new-window`` per task) or ``split`` (same-window
        ``split-window`` tiles for OMX-like shorthand launch).
    team_id:
        Stable team api id recorded into pane env / prompts.
    owner_token:
        Optional launch owner token injected into worker env.
    executor:
        When ``\"fixture\"``, replace every pane_command with the hermetic
        ACK fixture (transport smoke only — not Grok live parity). Default
        ``None`` keeps the production grok / routing pane path.
    view_mode:
        For ``topology=split``: ``same_window`` / ``dedicated_window`` /
        ``detached_session``. Resolved by the CLI when omitted. Non-split
        topologies refuse an explicit view_mode.
    worker_topology:
        ``pane`` (default — existing tmux workers) or ``job`` (durable Jobs
        plane workers; #69 PR4). Independent of split/windows layout topology.

    Returns the written team.json payload.
    """
    from omg_cli.team.launch import (
        WORKER_TOPOLOGY_JOB,
        WORKER_TOPOLOGY_PANE,
        WorkerLaunchError,
        launch_worker,
        normalize_worker_topology,
        stamp_execution_on_task,
    )
    from omg_cli.team.topology import VIEW_MODES, layout_for_view_mode

    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    goal = (goal or "").strip() or "(no goal)"
    tasks = _parse_tasks_json(tasks_json)
    n = _assert_start_gates(tasks, env=env)
    if topology not in ("windows", "split"):
        raise TeamError(f"unsupported topology {topology!r}")
    try:
        worker_topo = normalize_worker_topology(
            worker_topology if worker_topology is not None else WORKER_TOPOLOGY_PANE
        )
    except WorkerLaunchError as exc:
        raise TeamError(str(exc)) from exc
    resolved_view_mode = view_mode
    if resolved_view_mode is not None:
        if topology != "split":
            raise TeamError(
                f"view_mode requires topology='split' (got {topology!r})"
            )
        if resolved_view_mode not in VIEW_MODES:
            raise TeamError(f"unsupported view_mode {resolved_view_mode!r}")
    if worker_topo == WORKER_TOPOLOGY_JOB and resolved_view_mode is not None:
        raise TeamError(
            "view_mode requires worker-topology=pane "
            f"(got worker_topology={worker_topo!r})"
        )
    executor_norm = (executor or "").strip().lower() or None
    if executor_norm is not None and executor_norm != "fixture":
        raise TeamError(
            f"unsupported team executor {executor!r} (supported: None / 'fixture')"
        )
    tid_plane = (team_id or "team").strip() or "team"
    token = owner_token or uuid.uuid4().hex
    use_fixture_executor = executor_norm == "fixture"

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

    # Launch-intent WAL recovery gate: sweep ALL project intents *before*
    # create_run / active-run refusal so crash orphans are reachable even when
    # an active run blocks a new start (or --force creates a different rid).
    # Hold the project launch lock through receipt publish so a concurrent
    # start cannot kill an in-flight unrecepted window.
    from contextlib import ExitStack

    _launch_lock_stack = ExitStack()
    try:
        # Pane topology owns the tmux launch-intent WAL. Job-backed workers
        # never touch tmux, so skip the lock/sweep for worker_topology=job.
        if not dry_run and worker_topo == WORKER_TOPOLOGY_PANE:
            try:
                from omg_cli.team.tmux import (
                    TmuxTeamError,
                    acquire_team_launch_lock,
                    require_clean_team_launch_intents,
                )

                _launch_lock_stack.enter_context(acquire_team_launch_lock(root_path))
                require_clean_team_launch_intents(root_path)
            except TmuxTeamError as exc:
                raise TeamError(str(exc)) from exc

        # Resolve / create run — track created_* for #17 rollback scope.
        # When --run reuses an existing run, never destroy pre-existing worktrees /
        # team dir / ownership (Codex P1 on PR #34).
        created_run = False
        created_worktrees: list[Path] = []
        created_team_dir = False
        ownership_backup: bytes | None = None
        remove_ownership = True
        file_backups: dict[Path, bytes | None] = {}
        tdir: Path | None = None
        # Resolve / create run
        if run_id:
            if load_run(root_path, run_id) is None:
                raise TeamError(f"no run found for --run {run_id!r}")
            rid = run_id
        else:
            note = (
                "experimental multi-CLI tmux team plane "
                f"(default on; kill {DISABLE_ENV}=1); integration isolation only"
                if multi_cli
                else (
                    "experimental grok-only tmux team plane "
                    f"(default on; kill {DISABLE_ENV}=1); multi-CLI via --routing"
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
            created_run = True

        def _fail_start(exc: BaseException, *, extra: Sequence[str] = ()) -> TeamError:
            rb = _rollback_partial_team_start(
                root_path,
                rid,
                created_run=created_run,
                worktrees=created_worktrees,
                team_dir_path=tdir if created_team_dir else None,
                remove_ownership=remove_ownership,
                ownership_backup=ownership_backup,
                file_backups=None if created_team_dir else file_backups,
            )
            details = [str(exc), *list(extra), *rb]
            return TeamError(
                "team start transaction failed (rolled back partial state): "
                + "; ".join(d for d in details if d)
            )

        # Snapshot pre-existing resources before we mutate (existing --run safety).
        from omg_cli.workers import ownership_manifest_path as _own_path

        mpath_pre = _own_path(root_path, rid)
        if mpath_pre.is_file() and not mpath_pre.is_symlink():
            try:
                ownership_backup = mpath_pre.read_bytes()
                remove_ownership = False  # will restore backup, not unlink
            except OSError:
                ownership_backup = None
                remove_ownership = True
        else:
            ownership_backup = None
            remove_ownership = True

        pre_existing_worktrees: set[Path] = set()
        for t in tasks:
            tid_pre = str(t.get("task_id") or t.get("id") or "")
            if not tid_pre:
                continue
            try:
                wt_pre = worktree_dir(root_path, rid, tid_pre)
            except Exception:
                continue
            if wt_pre.exists():
                try:
                    pre_existing_worktrees.add(wt_pre.resolve())
                except OSError:
                    pre_existing_worktrees.add(wt_pre)

        # Ownership + real worktrees + setup + dry_run/live are all inside the
        # #17 rollback boundary (Codex: post-prep failures must also roll back).
        _tx_failed_prefix = "team start transaction failed"
        try:
            manifest = build_ownership_manifest(root_path, rid, tasks)
            # Prepare per-task; register expected path *before* prepare so a
            # create-then-raise inside prepare_task is still rolled back.
            for mtask in list(manifest.get("tasks") or []):
                tid_prep = str(mtask.get("task_id") or "")
                if not tid_prep:
                    continue
                wt_expected = worktree_dir(root_path, rid, tid_prep)
                try:
                    wt_resolved = wt_expected.resolve()
                except OSError:
                    wt_resolved = wt_expected
                tracked_new = False
                if wt_resolved not in pre_existing_worktrees:
                    created_worktrees.append(wt_expected)
                    tracked_new = True
                try:
                    wt = prepare_task(root_path, rid, tid_prep)
                except Exception:
                    raise
                # Prefer the concrete path prepare returned (same location).
                if tracked_new and wt not in created_worktrees:
                    created_worktrees.append(wt)

            tdir = team_dir(root_path, rid)
            created_team_dir = not (tdir.is_dir() and not tdir.is_symlink())
            tdir.mkdir(parents=True, exist_ok=True)

            session = session_name_for_cwd(root_path)
            env_pairs = _pane_env_pairs(
                run_id=rid,
                team_id=tid_plane,
                leader_root=root_path,
                state_root=root_path / ".omg" / "state",
                owner_token=token,
            )

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
                wt = Path(
                    str(mtask.get("worktree_path") or worktree_dir(root_path, rid, tid))
                )
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
                    desc_path = tdir / f"{tid}.provider.json"
                    # Live panes need prepublish before team.json; dry_run
                    # never runs supervisor so skip authority publication.
                    pane_cmd = materialize_supervisor_pane_command(
                        descriptor_path=desc_path,
                        provider=provider,
                        argv=argv,
                        prompt_delivery=prompt_delivery,
                        prompt_file=prompt_path,
                        needs_pty=needs_pty,
                        cwd=wt,
                        identity_basenames=inv.identity_basenames or None,
                        provider_strategy=inv.provider_strategy or None,
                        startup_strategy=inv.startup_strategy or None,
                        leader_root=root_path,
                        run_id=rid,
                        team_id=tid_plane,
                        worker_id=tid,
                        owner_token=token,
                        publish_authority=not dry_run,
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
                    # Extract prompt-file path from argv when present.
                    prompt_path_d1: Path | None = None
                    try:
                        pidx = argv.index("--prompt-file")
                        if pidx + 1 < len(argv):
                            prompt_path_d1 = Path(argv[pidx + 1])
                    except ValueError:
                        prompt_path_d1 = None
                    desc_path = tdir / f"{tid}.provider.json"
                    pane_cmd = materialize_supervisor_pane_command(
                        descriptor_path=desc_path,
                        provider=provider,
                        argv=list(argv),
                        prompt_delivery=prompt_delivery,
                        prompt_file=prompt_path_d1,
                        needs_pty=False,
                        cwd=wt,
                        leader_root=root_path,
                        run_id=rid,
                        team_id=tid_plane,
                        worker_id=tid,
                        owner_token=token,
                        publish_authority=not dry_run,
                    )

                if use_fixture_executor:
                    # Hermetic transport override — keep argv record for diagnostics.
                    desc_path = tdir / f"{tid}.provider.json"
                    pane_cmd = build_fixture_pane_command(
                        descriptor_path=desc_path,
                        leader_root=root_path,
                        run_id=rid,
                        team_id=tid_plane,
                        worker_id=tid,
                        owner_token=token,
                        publish_authority=not dry_run,
                    )
                    provider = "fixture"

                # Job-backed workers may take an explicit jobs-admitted provider
                # from the task dict (fake|antigravity) when not using fixture.
                if worker_topo == WORKER_TOPOLOGY_JOB and not use_fixture_executor:
                    src_prov = str(src_task.get("provider") or "").strip().lower()
                    if src_prov in ("agy", "antigravity"):
                        provider = "antigravity"
                    elif src_prov == "fake":
                        provider = "fake"

                # Persist per-task argv under team/ (mirrors fanout workers/*.argv.json)
                argv_path = tdir / f"{tid}.argv.json"
                # When reusing a team dir, backup prior argv so rollback can restore.
                if not created_team_dir and argv_path not in file_backups:
                    if argv_path.is_file() and not argv_path.is_symlink():
                        try:
                            file_backups[argv_path] = argv_path.read_bytes()
                        except OSError:
                            file_backups[argv_path] = None
                    else:
                        file_backups[argv_path] = None
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
                    "_env_pairs": _pane_env_pairs(
                        run_id=rid,
                        team_id=tid_plane,
                        worker_id=tid,
                        leader_root=root_path,
                        state_root=root_path / ".omg" / "state",
                        owner_token=token,
                    ),
                }
                from omg_cli.team.presentation import stamp_route_on_task

                stamp_route_on_task(
                    rec,
                    executor=executor_norm,
                    provider=provider,
                    role=role,
                    posture=posture,
                )
                task_records.append(rec)

            routing_payload = resolved.to_dict() if resolved is not None else None

            def _public_tasks() -> list[dict[str, Any]]:
                cleaned: list[dict[str, Any]] = []
                for item in task_records:
                    row = {
                        k: v
                        for k, v in item.items()
                        if k not in ("_env_pairs", "_tmux_launch")
                    }
                    cleaned.append(row)
                return cleaned

            if dry_run:
                # HERMETIC: never call tmux_available() / subprocess / start_job
                note = "dry_run skeleton; pid=None; no tmux/subprocess; " + (
                    "multi-CLI per-provider argv recorded"
                    if multi_cli
                    else "grok-only pane argv recorded"
                )
                if worker_topo == WORKER_TOPOLOGY_JOB:
                    note = (
                        "dry_run skeleton; worker_topology=job; "
                        "no pane/subprocess/job; launch descriptors only"
                    )
                dry_view = resolved_view_mode
                if (
                    worker_topo == WORKER_TOPOLOGY_PANE
                    and topology == "split"
                    and dry_view is None
                ):
                    dry_view = (
                        "detached_session" if detach else "same_window"
                    )
                for rec in task_records:
                    try:
                        handle = launch_worker(
                            root_path,
                            worker_id=str(rec["task_id"]),
                            topology=worker_topo,
                            provider=str(rec.get("provider") or "grok"),
                            role=str(rec.get("role") or "executor"),
                            run_id=rid,
                            team_id=tid_plane,
                            task_id=str(rec["task_id"]),
                            attempt=1,
                            launch_generation=1,
                            dry_run=True,
                            executor=executor_norm,
                        )
                        stamp_execution_on_task(rec, handle)
                    except WorkerLaunchError as exc:
                        raise TeamError(str(exc)) from exc
                meta = {
                    "writer": CLI_WRITER,
                    "schema_version": SCHEMA_VERSION,
                    "meta_generation": 0,
                    "run_id": rid,
                    "session": session,
                    "dry_run": True,
                    "workspace_mode": WORKSPACE_MODE,
                    "goal": goal,
                    "task_count": n,
                    "next_worker_index": n,
                    "next_logical_worker_index": n,
                    "created_at": _utc_now(),
                    "tasks": _public_tasks(),
                    "multi_cli": multi_cli,
                    "routing": routing_payload,
                    "linked_ralph": None,
                    "topology": topology,
                    "worker_topology": worker_topo,
                    "team_id": tid_plane,
                    "owner_token": token,
                    "executor": executor_norm,
                    "view_mode": (
                        dry_view
                        if worker_topo == WORKER_TOPOLOGY_PANE and topology == "split"
                        else None
                    ),
                    "layout": (
                        layout_for_view_mode(dry_view)
                        if worker_topo == WORKER_TOPOLOGY_PANE
                        and topology == "split"
                        and dry_view
                        else None
                    ),
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
                        "worker_topology": worker_topo,
                        "note": "team dry_run completed; verified remains false",
                    },
                )
                return meta

            # Live job-backed workers: Jobs plane owns process lifecycle.
            if worker_topo == WORKER_TOPOLOGY_JOB:
                launched_jobs: list[str] = []
                try:
                    for rec in task_records:
                        handle = launch_worker(
                            root_path,
                            worker_id=str(rec["task_id"]),
                            topology=WORKER_TOPOLOGY_JOB,
                            provider=str(rec.get("provider") or "grok"),
                            role=str(rec.get("role") or "executor"),
                            run_id=rid,
                            team_id=tid_plane,
                            task_id=str(rec["task_id"]),
                            attempt=1,
                            launch_generation=1,
                            dry_run=False,
                            executor=executor_norm,
                            prompt_text=(
                                f"Team worker {rec['task_id']} "
                                f"run={rid} team={tid_plane} goal={goal}"
                            ),
                        )
                        stamp_execution_on_task(rec, handle)
                        if handle.job_id:
                            launched_jobs.append(handle.job_id)
                        rec["status"] = "running"
                        rec["pid"] = None
                        rec["pgid"] = None
                        rec["pid_start"] = None
                    meta = {
                        "writer": CLI_WRITER,
                        "schema_version": SCHEMA_VERSION,
                        "meta_generation": 0,
                        "run_id": rid,
                        "session": session,
                        "dry_run": False,
                        "workspace_mode": WORKSPACE_MODE,
                        "goal": goal,
                        "task_count": n,
                        "next_worker_index": n,
                        "next_logical_worker_index": n,
                        "created_at": _utc_now(),
                        "tasks": _public_tasks(),
                        "multi_cli": multi_cli,
                        "routing": routing_payload,
                        "linked_ralph": None,
                        "topology": topology,
                        "worker_topology": worker_topo,
                        "team_id": tid_plane,
                        "owner_token": token,
                        "executor": executor_norm,
                        "view_mode": None,
                        "layout": None,
                        "note": (
                            "experimental job-backed team workers via durable "
                            "jobs plane (#69 PR4); process lifecycle owned by "
                            "Jobs; stop cancels jobs (no tmux)"
                        ),
                    }
                    for idx, task in enumerate(meta["tasks"]):
                        if isinstance(task, dict):
                            task.setdefault(
                                "logical_worker_index",
                                task.get("window_index", idx),
                            )
                            task.setdefault("attempt", 1)
                    _atomic_write_json(team_meta_path(root_path, rid), meta)
                    from omg_cli.team.supervisor import (
                        clear_supervisor_prepublish_authorities,
                    )

                    clear_supervisor_prepublish_authorities(root_path, rid)
                    write_status(
                        root_path,
                        rid,
                        "running",
                        extra={
                            "team": True,
                            "stage": "team_running",
                            "task_count": n,
                            "multi_cli": multi_cli,
                            "worker_topology": worker_topo,
                            "job_ids": launched_jobs,
                        },
                    )
                    return meta
                except (WorkerLaunchError, TeamError, Exception) as exc:
                    # Compensate: cancel any jobs already started.
                    from omg_cli.jobs.runtime import cancel_job as _cancel_job
                    from omg_cli.jobs.models import JobStoreError as _JobStoreError

                    for jid in launched_jobs:
                        try:
                            _cancel_job(root_path, jid)
                        except _JobStoreError:
                            pass
                    if isinstance(exc, TeamError):
                        raise
                    if isinstance(exc, WorkerLaunchError):
                        raise TeamError(str(exc)) from exc
                    raise TeamError(f"job-backed worker launch failed: {exc}") from exc

            # Live pane path: create tmux session + fill pids
            launch_nonce = uuid.uuid4().hex
            # Project-wide intent sweep already ran (fail-closed) before create_run.
            transaction_paths = (
                team_launch_receipt_path(root_path, rid),
                team_meta_path(root_path, rid),
                _run_dir(root_path, rid) / "status.json",
            )
            snapshots = _snapshot_live_start_files(transaction_paths)
            created_handle: tuple[str, str] | None = None
            tmux_launch: dict[str, Any] = {
                "attach_mode": "detached",
                "session_owned": True,
                "leader_pane_id": None,
                "window_id": None,
                "attach_hint": None,
                "view_mode": None,
                "layout": None,
                "leader_pane_pid": None,
            }
            launch_intent_path: str | None = None
            intent_wal_cleared = False
            try:
                if topology == "split":
                    from omg_cli.team.tmux import TmuxTeamError, create_split_team_session

                    try:
                        created_handle = create_split_team_session(
                            session=session,
                            tasks=task_records,
                            env_pairs=env_pairs,
                            detach=detach,
                            env=env,
                            root=root_path,
                            run_id=rid,
                            view_mode=resolved_view_mode,
                        )
                    except TmuxTeamError as exc:
                        raise TeamError(str(exc)) from exc
                    raw_launch = task_records[0].pop("_tmux_launch", None)
                    raw_intent = task_records[0].pop("_tmux_launch_intent", None)
                    if isinstance(raw_intent, str):
                        launch_intent_path = raw_intent
                    if isinstance(raw_launch, Mapping):
                        tmux_launch = {**tmux_launch, **dict(raw_launch)}
                    # Inside mode joins the live session (name may differ from plan).
                    session = created_handle[0]
                else:
                    created_handle = _create_tmux_session(
                        session=session,
                        tasks=task_records,
                        env_pairs=env_pairs,
                    )
                    raw_launch = task_records[0].pop("_tmux_launch", None)
                    if isinstance(raw_launch, Mapping):
                        tmux_launch = {**tmux_launch, **dict(raw_launch)}
                launch_sock, launch_server = _tmux_scope_from_launch(tmux_launch)
                _bind_tmux_launch_nonce(
                    session_id=created_handle[1],
                    launch_nonce=launch_nonce,
                    window_id=(
                        str(tmux_launch.get("window_id"))
                        if isinstance(tmux_launch.get("window_id"), str)
                        else None
                    ),
                    pane_ids=[
                        str(rec.get("pane_id"))
                        for rec in task_records
                        if isinstance(rec.get("pane_id"), str)
                    ],
                    session_owned=bool(tmux_launch.get("session_owned", True)),
                    socket_path=launch_sock,
                    expected_server=launch_server,
                )

                session_identity = _read_tmux_session_identity(
                    session,
                    socket_path=launch_sock,
                    expected_server=launch_server,
                )
                if session_identity != created_handle:
                    raise TeamError("tmux launch identity readback failed")

                if topology == "split" and all(
                    isinstance(rec.get("pane_id"), str)
                    and _TMUX_PANE_ID.fullmatch(str(rec.get("pane_id"))) is not None
                    for rec in task_records
                ):
                    # Final linearization before immutable receipt: one list-panes
                    # snapshot of pane_id+pid+session_id+window_id (+nonce when set).
                    # Never commit from separate PID-only probes after revalidation.
                    snap_window = tmux_launch.get("window_id")
                    snap_window_id = (
                        str(snap_window)
                        if isinstance(snap_window, str)
                        and _TMUX_WINDOW_ID.fullmatch(str(snap_window)) is not None
                        else None
                    )
                    pane_snapshot = _snapshot_launch_pane_identities(
                        expected_session_id=created_handle[1],
                        expected_pane_ids=[str(rec["pane_id"]) for rec in task_records],
                        expected_window_id=snap_window_id,
                        expected_launch_nonce=launch_nonce,
                        socket_path=launch_sock,
                        expected_server=launch_server,
                    )
                    for rec in task_records:
                        pane_id = str(rec["pane_id"])
                        snap = pane_snapshot[pane_id]
                        pid = int(snap["pane_pid"])
                        rec["pid"] = pid
                        rec["pgid"] = _pgid_for_pid(pid)
                        rec["pid_start"] = _pid_start_identity(pid)
                        rec["status"] = (
                            "running"
                            if rec["pgid"] is not None and rec["pid_start"] is not None
                            else "launched"
                        )
                else:
                    pane_identities = _list_pane_identities(
                        created_handle[1],
                        socket_path=launch_sock,
                        expected_server=launch_server,
                    )
                    if len(pane_identities) != len(task_records):
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
                    if launch_server is not None:
                        _require_plane_tmux_server(
                            launch_server,
                            socket_path=launch_sock,
                            action="windows receipt identity commit",
                        )

                intent_nonce: str | None = None
                intent_window_name: str | None = None
                if launch_intent_path:
                    try:
                        intent_raw = json.loads(
                            Path(launch_intent_path).read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise TeamError(
                            f"launch intent unreadable before receipt: {exc}"
                        ) from exc
                    if not isinstance(intent_raw, dict):
                        raise TeamError("launch intent must be an object before receipt")
                    intent_nonce = intent_raw.get("nonce")
                    intent_window_name = intent_raw.get("window_name")
                    intent_session = intent_raw.get("session_id")
                    if (
                        not isinstance(intent_nonce, str)
                        or not intent_nonce
                        or not isinstance(intent_window_name, str)
                        or not intent_window_name
                    ):
                        raise TeamError(
                            "launch intent missing nonce/window_name before receipt"
                        )
                    if intent_session != created_handle[1]:
                        raise TeamError(
                            "launch intent session_id drift before receipt publish"
                        )

                _receipt, launch_receipt_sha256 = _persist_team_launch_receipt(
                    root_path,
                    rid,
                    session=session,
                    session_id=created_handle[1],
                    launch_nonce=launch_nonce,
                    tasks=task_records,
                    intent_nonce=intent_nonce,
                    window_name=intent_window_name,
                    view_mode=(
                        str(tmux_launch["view_mode"])
                        if isinstance(tmux_launch.get("view_mode"), str)
                        else None
                    ),
                    layout=(
                        str(tmux_launch["layout"])
                        if isinstance(tmux_launch.get("layout"), str)
                        else None
                    ),
                    leader_pane_id=(
                        str(tmux_launch["leader_pane_id"])
                        if isinstance(tmux_launch.get("leader_pane_id"), str)
                        else None
                    ),
                    leader_pane_pid=(
                        int(tmux_launch["leader_pane_pid"])
                        if isinstance(tmux_launch.get("leader_pane_pid"), int)
                        and not isinstance(tmux_launch.get("leader_pane_pid"), bool)
                        else None
                    ),
                    window_id=(
                        str(tmux_launch["window_id"])
                        if isinstance(tmux_launch.get("window_id"), str)
                        else None
                    ),
                    session_owned=bool(tmux_launch.get("session_owned", True)),
                    attach_mode=(
                        str(tmux_launch["attach_mode"])
                        if isinstance(tmux_launch.get("attach_mode"), str)
                        else None
                    ),
                )

                meta = {
                    "writer": CLI_WRITER,
                    "schema_version": SCHEMA_VERSION,
                    "meta_generation": 0,
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
                    "next_logical_worker_index": n,
                    "created_at": _utc_now(),
                    "tasks": _public_tasks(),
                    "multi_cli": multi_cli,
                    "routing": routing_payload,
                    "linked_ralph": None,
                    "topology": topology,
                    "worker_topology": WORKER_TOPOLOGY_PANE,
                    "team_id": tid_plane,
                    "owner_token": token,
                    "executor": executor_norm,
                    "attach_mode": tmux_launch.get("attach_mode"),
                    "session_owned": bool(tmux_launch.get("session_owned", True)),
                    "leader_pane_id": tmux_launch.get("leader_pane_id"),
                    "window_id": tmux_launch.get("window_id"),
                    "attach_hint": tmux_launch.get("attach_hint"),
                    "view_mode": tmux_launch.get("view_mode"),
                    "layout": tmux_launch.get("layout"),
                    "leader_pane_pid": tmux_launch.get("leader_pane_pid"),
                    "note": (
                        "experimental fixture tmux team; hermetic ACK transport "
                        "(not Grok live parity); stop via immutable launch identity"
                        if executor_norm == "fixture"
                        else (
                            "experimental multi-CLI tmux team; stop via immutable launch identity"
                            if multi_cli
                            else "experimental grok-only tmux team; stop via immutable launch identity"
                        )
                    ),
                }
                # Stamp logical worker slots + tmux_topology projection (#102).
                for idx, task in enumerate(meta["tasks"]):
                    if isinstance(task, dict):
                        task.setdefault(
                            "logical_worker_index", task.get("window_index", idx)
                        )
                        task.setdefault("attempt", 1)
                        # Unified execution descriptor (#69 PR4) — pane topology.
                        pane_id = task.get("pane_id")
                        if isinstance(pane_id, str) and pane_id:
                            try:
                                handle = launch_worker(
                                    root_path,
                                    worker_id=str(task["task_id"]),
                                    topology=WORKER_TOPOLOGY_PANE,
                                    provider=str(task.get("provider") or "grok"),
                                    role=str(task.get("role") or "executor"),
                                    run_id=rid,
                                    team_id=tid_plane,
                                    task_id=str(task["task_id"]),
                                    attempt=int(task.get("attempt") or 1),
                                    launch_generation=1,
                                    pane_id=pane_id,
                                    dry_run=False,
                                    executor=executor_norm,
                                )
                                stamp_execution_on_task(task, handle)
                            except WorkerLaunchError as exc:
                                raise TeamError(str(exc)) from exc
                # Also stamp on task_records so _public_tasks stays consistent
                # if meta["tasks"] was already snapshotted without execution.
                for rec in task_records:
                    if rec.get("execution"):
                        continue
                    pane_id = rec.get("pane_id")
                    if isinstance(pane_id, str) and pane_id:
                        try:
                            handle = launch_worker(
                                root_path,
                                worker_id=str(rec["task_id"]),
                                topology=WORKER_TOPOLOGY_PANE,
                                provider=str(rec.get("provider") or "grok"),
                                role=str(rec.get("role") or "executor"),
                                run_id=rid,
                                team_id=tid_plane,
                                task_id=str(rec["task_id"]),
                                attempt=int(rec.get("attempt") or 1),
                                launch_generation=1,
                                pane_id=pane_id,
                                dry_run=False,
                                executor=executor_norm,
                            )
                            stamp_execution_on_task(rec, handle)
                        except WorkerLaunchError as exc:
                            raise TeamError(str(exc)) from exc
                meta["tasks"] = _public_tasks()
                meta["tmux_topology"] = _build_launch_tmux_topology(
                    meta, launch_receipt=_receipt
                )
                # Persist WAL tmux server/session scope for stop/rollback kills.
                launch_sid = tmux_launch.get("session_id")
                if isinstance(launch_sid, str) and launch_sid:
                    meta["session_id"] = launch_sid
                else:
                    meta["session_id"] = created_handle[1]
                for _srv_key in (
                    "tmux_socket_path",
                    "tmux_server_pid",
                    "tmux_server_pid_start",
                ):
                    if tmux_launch.get(_srv_key) is not None:
                        meta[_srv_key] = tmux_launch[_srv_key]
                # Commit point = durable receipt AND hash-bound team.json.
                # write_status before WAL clear so a status failure still leaves
                # the intent WAL as a sweep gate. Clear is terminal for the
                # intent — after it succeeds, do not roll back authority files.
                _atomic_write_json(team_meta_path(root_path, rid), meta)
                # Authoritative team.json supersedes prepublish launch intents.
                from omg_cli.team.supervisor import (
                    clear_supervisor_prepublish_authorities,
                )

                clear_supervisor_prepublish_authorities(root_path, rid)
                if launch_intent_path:
                    committed = load_team_meta(root_path, rid)
                    verified = _load_team_launch_receipt(root_path, rid, committed)
                    if (
                        committed.get("launch_receipt_sha256") != launch_receipt_sha256
                        or committed.get("session") != session
                        or committed.get("launch_nonce") != launch_nonce
                        or verified.get("session_id") != created_handle[1]
                        or verified.get("launch_nonce") != launch_nonce
                        or verified.get("session_name") != session
                    ):
                        raise TeamError(
                            "team.json launch binding verification failed "
                            "before intent clear"
                        )
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
                if launch_intent_path:
                    from omg_cli.team.tmux import clear_team_launch_intent

                    try:
                        clear_team_launch_intent(launch_intent_path)
                    except BaseException:
                        # Unlink is the commit point — if the WAL is already gone,
                        # retain authority even when clear raises afterward.
                        try:
                            if not Path(launch_intent_path).is_file():
                                intent_wal_cleared = True
                        except OSError:
                            intent_wal_cleared = True
                        raise
                    intent_wal_cleared = True
                return meta
            except Exception as exc:
                cleanup_error = None
                if intent_wal_cleared:
                    # Durable authority + cleared WAL: keep receipt/team.json and
                    # the live session — rolling back would hide a running team.
                    # Prefix matches outer handler so _fail_start does not rmtree.
                    raise TeamError(
                        f"{_tx_failed_prefix} (durable authority retained; "
                        f"launch-intent WAL cleared): {exc}"
                    ) from exc
                if created_handle is not None:
                    if topology == "split" and not bool(
                        tmux_launch.get("session_owned", True)
                    ):
                        from omg_cli.team.tmux import (
                            _intent_tmux_server,
                            _kill_panes_scoped,
                            _kill_window,
                        )
                        from omg_cli.team.topology import (
                            VIEW_MODE_DEDICATED_WINDOW,
                            VIEW_MODE_SAME_WINDOW,
                            TopologyError,
                            resolve_persisted_view_mode,
                        )

                        try:
                            vm = resolve_persisted_view_mode(tmux_launch)
                        except TopologyError:
                            # Never default to dedicated_window+kill-window —
                            # leader window_id under same_window would wipe the
                            # invoking window. Pane-scoped cleanup only.
                            vm = VIEW_MODE_SAME_WINDOW
                        window_id = tmux_launch.get("window_id")
                        expected_server = _intent_tmux_server(tmux_launch)
                        sock = (
                            str(expected_server["tmux_socket_path"])
                            if expected_server is not None
                            else None
                        )
                        expected_session = tmux_launch.get("session_id")
                        if not isinstance(expected_session, str):
                            expected_session = created_handle[1]
                        pane_ids = [
                            str(rec.get("pane_id"))
                            for rec in task_records
                            if isinstance(rec.get("pane_id"), str)
                        ]
                        if vm == VIEW_MODE_DEDICATED_WINDOW and isinstance(
                            window_id, str
                        ) and window_id:
                            cleanup_error = _kill_window(
                                window_id,
                                socket_path=sock,
                                expected_session_id=expected_session,
                                expected_server=expected_server,
                            )
                        else:
                            # same_window / unknown: never kill-window.
                            cleanup_error = _kill_panes_scoped(
                                pane_ids,
                                expected_server=expected_server,
                                expected_session_id=expected_session,
                                expected_window_id=(
                                    window_id if isinstance(window_id, str) else None
                                ),
                                intent_or_launch_nonce=None,
                                leader_pane_id=(
                                    str(tmux_launch["leader_pane_id"])
                                    if isinstance(
                                        tmux_launch.get("leader_pane_id"), str
                                    )
                                    else None
                                ),
                                socket_path=sock,
                            )
                    else:
                        cleanup_sock, cleanup_server = _tmux_scope_from_launch(
                            tmux_launch
                        )
                        cleanup_error = _cleanup_created_tmux_session(
                            created_handle,
                            socket_path=cleanup_sock,
                            expected_server=cleanup_server,
                        )
                if cleanup_error is not None:
                    # Unproven cleanup → keep receipt/team.json/WAL/team dir so
                    # stop/sweep retain identity authority. Do not rmtree.
                    raise TeamError(
                        f"{_tx_failed_prefix} (tmux cleanup unproven; "
                        f"authority retained): {exc}; {cleanup_error}"
                    ) from exc
                restore_errors = _restore_live_start_files(
                    snapshots, unlink_new=True
                )
                # #17: also undo run/active/worktrees/team-dir created before the
                # narrow file snapshot so a failed launch does not block retries.
                extra = list(restore_errors)
                raise _fail_start(exc, extra=extra) from exc
        except TeamError as exc:
            if str(exc).startswith(_tx_failed_prefix):
                raise
            raise _fail_start(exc) from exc
        except Exception as exc:
            raise _fail_start(exc) from exc

    finally:
        _launch_lock_stack.close()


def _worker_pane_liveness(
    *,
    pane_id: str,
    session: str,
    expected_session_id: str | None,
    launch_nonce: str | None,
    expected_pid_start: str | None,
    expected_pid: int | None,
) -> Literal["alive", "proven_absent", "present_foreign", "unknown"]:
    """Fail-closed pane liveness for status / resume / relaunch.

    States:
    - ``alive`` — exact receipt identity is live
    - ``proven_absent`` — successful complete ``list-panes -a`` missing the pane
    - ``present_foreign`` — pane still addressable but dead/foreign/drifted
    - ``unknown`` — probe inconclusive (never authorize side-effect relaunch)

    Side-effect relaunch may create a replacement pane **only** for
    ``proven_absent``. ``present_foreign`` and ``unknown`` must refuse split.
    """
    if not expected_session_id or not launch_nonce or not session:
        return "unknown"
    try:
        from omg_cli.team.tmux import probe_worker_pane_identity

        probed = probe_worker_pane_identity(pane_id)
        if probed is None:
            absent, _err = _pane_proven_absent(pane_id)
            if absent is True:
                return "proven_absent"
            return "unknown"
        if probed.get("dead"):
            return "present_foreign"
        if probed.get("session_id") != expected_session_id:
            return "present_foreign"
        live_nonce, nonce_ok = _probe_tmux_launch_nonce_for_pane(
            pane_id, session, allow_session_fallback=False
        )
        if not nonce_ok:
            return "unknown"
        if live_nonce != launch_nonce:
            return "present_foreign"
        live_pid = probed.get("pane_pid")
        if not isinstance(live_pid, int) or live_pid <= 0:
            return "present_foreign"
        if expected_pid_start:
            if expected_pid is not None and live_pid != expected_pid:
                return "present_foreign"
            live_start = _pid_start_identity(live_pid)
            if not live_start:
                return "unknown"
            if live_start != expected_pid_start:
                return "present_foreign"
            return "alive"
        if expected_pid is not None:
            if live_pid != expected_pid:
                return "present_foreign"
            return "alive"
        return "alive"
    except OSError:
        return "unknown"


def _status_worker_alive(
    *,
    pane_id: str,
    session: str,
    expected_session_id: str | None,
    launch_nonce: str | None,
    expected_pid_start: str | None,
    expected_pid: int | None,
) -> bool | None:
    """Tri-state wrapper over :func:`_worker_pane_liveness` for status/resume.

    - ``True`` — ``alive``
    - ``False`` — ``proven_absent`` or ``present_foreign``
    - ``None`` — ``unknown``

    Side-effect relaunch must call :func:`_worker_pane_liveness` directly and
    only respawn on ``proven_absent``.
    """
    state = _worker_pane_liveness(
        pane_id=pane_id,
        session=session,
        expected_session_id=expected_session_id,
        launch_nonce=launch_nonce,
        expected_pid_start=expected_pid_start,
        expected_pid=expected_pid,
    )
    if state == "alive":
        return True
    if state == "unknown":
        return None
    return False



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
    launch_nonce = meta.get("launch_nonce")
    expected_session_id: str | None = None
    receipt_path = team_launch_receipt_path(root_path, run_id)
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
    if expected_session_id is None and session and not dry and probe_tmux:
        try:
            ident = _read_tmux_session_identity(session)
        except Exception:
            ident = None
        if ident is not None and ident[0] == session and ident[1]:
            expected_session_id = str(ident[1])

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
        pane_id = raw.get("pane_id")
        # dry_run / never-launched panes are not live
        if dry or st == "dry_run" or raw.get("pid") is None and dry:
            alive = False
        elif not probe_tmux:
            alive = False
        elif isinstance(pane_id, str) and _TMUX_PANE_ID.fullmatch(pane_id) is not None:
            probed_alive = _status_worker_alive(
                pane_id=pane_id,
                session=session,
                expected_session_id=expected_session_id,
                launch_nonce=launch_nonce if isinstance(launch_nonce, str) else None,
                expected_pid_start=raw.get("pid_start")
                if isinstance(raw.get("pid_start"), str)
                else None,
                expected_pid=raw.get("pid") if isinstance(raw.get("pid"), int) else None,
            )
            # Locked status schema keeps bool; unknown → not-alive display.
            alive = bool(probed_alive)
        else:
            # No exact pane identity → never guess via logical window_index (#98).
            alive = False
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
    from contextlib import ExitStack

    from omg_cli.integrate import integrate_results
    from omg_cli.state import RunSchema, classify_run_schema, execution_lease
    from omg_cli.team.scaling import acquire_scale_lock
    from omg_cli.workers import _assert_no_pending_team_scale

    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    if not run_id:
        active = load_active_run(root_path)
        if active is None:
            raise TeamError("no active run (pass --run ID)")
        run_id = str(active["run_id"])

    run_before = load_run(root_path, run_id)
    if run_before is None:
        raise TeamError(f"no run found: {run_id}")
    schema = classify_run_schema(run_before)

    # Lock order matches integrate_results: execution lease, then scale lock.
    # The scale lock spans seal through cherry-pick so a WAL cannot appear
    # between authorization and integration.
    with ExitStack() as stack:
        lease = (
            stack.enter_context(
                execution_lease(root_path, run_id, intent="team-collect")
            )
            if schema is RunSchema.STRICT_V2
            else None
        )
        stack.enter_context(acquire_scale_lock(root_path, run_id))

        # Require CLI-stamped team.json so forged {verified:true} team files
        # cannot be used as a collect authority signal.
        load_team_meta(root_path, run_id)
        try:
            _assert_no_pending_team_scale(root_path, run_id)
        except WorkerError as exc:
            raise TeamError(str(exc)) from exc

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
                lease=lease,
                _scale_lock_held=True,
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
        or parsed.get("schema_version") != RALPH_SCHEMA_VERSION
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


def _team_launch_nonce_matches(
    *,
    session: str,
    receipt: Mapping[str, Any],
    session_owned: bool,
    window_id: str | None,
    pane_ids: Sequence[str] | None = None,
) -> bool:
    """Verify launch nonce using the authority mode for this Team.

    Owned detached sessions may use the session option. Inside mode must match
    pane-scoped (or window-scoped) stamps — never a shared session option that
    a later Team can overwrite.
    """
    expected = receipt.get("launch_nonce")
    if not isinstance(expected, str):
        return False
    if session_owned:
        return _read_tmux_launch_nonce(session) == expected
    if isinstance(window_id, str) and _read_tmux_launch_nonce_for_window(window_id) == expected:
        return True
    if pane_ids:
        for pane_id in pane_ids:
            if (
                isinstance(pane_id, str)
                and _read_tmux_launch_nonce_for_pane(
                    pane_id, session, allow_session_fallback=False
                )
                == expected
            ):
                return True
        return False
    return False


def _tmux_launch_authority_matches(
    session: str,
    *,
    expected_nonce: str,
    session_owned: bool,
    window_id: str | None = None,
    pane_ids: Sequence[str] | None = None,
) -> bool:
    """Same session_owned-aware authority rule for a bare nonce string.

    Thin wrapper around :func:`_team_launch_nonce_matches` for callers (scaling
    / relaunch) that hold a raw ``launch_nonce`` string from a launch/identity
    receipt rather than the full receipt mapping.
    """
    return _team_launch_nonce_matches(
        session=session,
        receipt={"launch_nonce": expected_nonce},
        session_owned=session_owned,
        window_id=window_id,
        pane_ids=pane_ids,
    )


def _resolve_live_signal_target(
    session: str,
    receipt: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    session_owned: bool = True,
    window_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a kill target bound to exact launch-receipt process identity.

    Pane token + launch nonce alone are not authority to adopt a *new* PID/PGID
    (``tmux respawn-pane`` / pane reuse keep ``%N`` while replacing the worker).
    Signal only the receipted pid/pgid/pid_start. PID drift requires a CLI
    identity-chain relaunch receipt (already reflected in ``row``), not a live
    pane rebind.
    """
    window_index = row.get("window_index")
    pane_id = row.get("pane_id")
    if (
        isinstance(window_index, bool)
        or not isinstance(window_index, int)
        or not isinstance(pane_id, str)
        or _TMUX_PANE_ID.fullmatch(pane_id) is None
    ):
        return None
    if _read_tmux_session_identity(session) != (session, receipt.get("session_id")):
        return None
    expected_nonce = receipt.get("launch_nonce")
    live_nonce = _read_tmux_launch_nonce_for_pane(
        pane_id, session, allow_session_fallback=False
    )
    if live_nonce != expected_nonce:
        if session_owned:
            # Owned detached session may still stamp session option; accept
            # only when pane option is absent *and* session matches (no pane
            # option was ever written). Prefer explicit pane authority.
            if live_nonce is not None:
                return None
            if _read_tmux_launch_nonce(session) != expected_nonce:
                return None
        else:
            # Inside mode: never use shared session nonce. Window stamp is the
            # only non-pane fallback when the Team window is known.
            if (
                not isinstance(window_id, str)
                or _read_tmux_launch_nonce_for_window(window_id) != expected_nonce
            ):
                return None

    live_pid: int | None = None
    live_window_id: str | None = None
    # Atomic pane probe: id + pid + session + window (+ pane nonce when supported).
    pane_probe = _tmux_run(
        [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_id}\t#{pane_pid}\t#{session_id}\t#{window_id}",
        ]
    )
    if pane_probe.returncode == 0:
        parts = (pane_probe.stdout or "").strip().split("\t")
        if (
            len(parts) == 4
            and _TMUX_PANE_ID.fullmatch(parts[0]) is not None
            and parts[0] == pane_id
            and parts[2] == receipt.get("session_id")
        ):
            if (
                isinstance(window_id, str)
                and _TMUX_WINDOW_ID.fullmatch(window_id) is not None
                and parts[3] != window_id
            ):
                return None
            if _TMUX_WINDOW_ID.fullmatch(parts[3]) is not None:
                live_window_id = parts[3]
            try:
                live_pid = int(parts[1])
            except ValueError:
                return None
    if live_pid is None:
        return None
    if live_pid <= 0:
        return None
    live_pgid = _pgid_for_pid(live_pid)
    live_start = _pid_start_identity(live_pid)
    if live_pgid is None or live_pgid <= 0 or not live_start:
        return None
    receipt_pid = row.get("pid")
    receipt_pgid = row.get("pgid")
    receipt_start = row.get("pid_start")
    if not (
        receipt_pid == live_pid
        and receipt_pgid == live_pgid
        and receipt_start == live_start
    ):
        # Refuse respawn / reuse rebound — do not signal a foreign PGID.
        return None
    out = dict(row)
    out["pid"] = live_pid
    out["pgid"] = live_pgid
    out["pid_start"] = live_start
    if live_window_id is not None:
        out["window_id"] = live_window_id
    out["identity_rebound"] = False
    return out


def _live_signal_target_matches(
    session: str,
    receipt: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    session_owned: bool = True,
    window_id: str | None = None,
) -> bool:
    """Revalidate session/nonce/pane-bound identity immediately before signal."""
    return (
        _resolve_live_signal_target(
            session,
            receipt,
            row,
            session_owned=session_owned,
            window_id=window_id,
        )
        is not None
    )


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


def _pane_proven_absent(pane_id: str) -> tuple[bool | None, str | None]:
    """Prove pane absence only via a successful complete ``list-panes -a``.

    Returns:
      ``(True, None)`` — pane token explicitly absent from a clean global list
      ``(False, None)`` — pane token present
      ``(None, reason)`` — unknown (OSError / non-zero / malformed); never
        treat unknown as gone (would authorize unsafe SIGKILL escalation)
    """
    if _TMUX_PANE_ID.fullmatch(pane_id) is None:
        return None, f"invalid pane id for absence probe {pane_id!r}"
    try:
        listed = _tmux_run(["list-panes", "-a", "-F", "#{pane_id}"])
    except OSError as exc:
        return None, f"pane absence probe OSError pane={pane_id}: {exc}"
    if listed.returncode != 0:
        err = (listed.stderr or listed.stdout or "").strip()
        return (
            None,
            f"pane absence probe failed pane={pane_id} exit={listed.returncode}"
            + (f" {err}" if err else ""),
        )
    seen: set[str] = set()
    for line in (listed.stdout or "").splitlines():
        token = line.strip()
        if not token:
            continue
        if _TMUX_PANE_ID.fullmatch(token) is None:
            return (
                None,
                f"pane absence probe malformed row {token!r} pane={pane_id}",
            )
        seen.add(token)
    if pane_id in seen:
        return False, None
    return True, None


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


def team_shutdown_request_path(root: Path | str, run_id: str) -> Path:
    return team_dir(root, run_id) / "shutdown-request.json"


def _list_in_progress_api_tasks(
    root: Path, run_id: str, team_id: str
) -> list[dict[str, Any]]:
    """Return API board tasks currently ``in_progress`` (active claims)."""
    from omg_cli.team.api import _list_tasks

    try:
        tasks = _list_tasks(root, run_id, team_id)
    except Exception as exc:
        # Fail closed: unreadable board is treated as unknown active claims.
        raise TeamError(
            f"cannot read team api tasks for stop gate (run={run_id} team={team_id})"
        ) from exc
    active: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        if str(task.get("status") or "") == "in_progress":
            active.append(dict(task))
    return active


def _write_shutdown_request(
    root: Path,
    run_id: str,
    *,
    team_id: str,
    force: bool,
    in_progress: Sequence[Mapping[str, Any]],
) -> Path:
    """Persist a durable shutdown request under the team dir (CLI-stamped)."""
    path = team_shutdown_request_path(root, run_id)
    owners: list[str] = []
    for task in in_progress:
        owner = task.get("owner")
        if not owner and isinstance(task.get("claim"), Mapping):
            owner = (task.get("claim") or {}).get("owner")
        owners.append(str(owner or ""))
    payload = {
        "store_kind": "team_shutdown_request",
        "schema_version": 1,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "team_id": team_id,
        "requested_at": _utc_now(),
        "force": bool(force),
        "in_progress_task_ids": [
            str(t.get("id")) for t in in_progress if t.get("id") is not None
        ],
        "in_progress_owners": owners,
        "note": (
            "graceful shutdown requested; workers should release or complete claims before teardown"
            if not force
            else "forced shutdown; exact pane/session teardown proceeds"
        ),
    }
    _atomic_write_json(path, payload)
    return path


def stop_team(
    root: Path | str | None = None,
    run_id: str | None = None,
    *,
    force: bool = False,
    kill_grace_s: float = 0.0,
) -> dict[str, Any]:
    """Stop only an exact nonce-bound immutable launch identity.

    ``team.json`` alone is never process authority.  The immutable receipt,
    live tmux session/pane identity, pane PID and OS PGID must all agree.

    Always writes a durable ``shutdown-request.json`` first.  When API board
    tasks are ``in_progress``, non-``force`` stops fail closed (no teardown).
    ``force=True`` proceeds with exact pane/session teardown only — never
    ``pkill -f``.  State and worktrees are preserved.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    root_path = root_path.resolve()
    if not run_id:
        active = load_active_run(root_path)
        if active is None:
            raise TeamError("no active run (pass --run ID)")
        run_id = str(active["run_id"])

    # Serialize with scale/relaunch so stop cannot race mid-scale side effects.
    from omg_cli.team.scaling import acquire_scale_lock

    with acquire_scale_lock(root_path, run_id):
        return _stop_team_locked(
            root_path,
            run_id,
            force=force,
            kill_grace_s=kill_grace_s,
        )


def _stop_team_locked(
    root_path: Path,
    run_id: str,
    *,
    force: bool,
    kill_grace_s: float,
) -> dict[str, Any]:
    from omg_cli.workers import _assert_no_pending_team_scale

    meta = load_team_meta(root_path, run_id)
    # A pre-receipt WAL may own a live read-write pane that is intentionally
    # absent from the committed receipt chain.  Do not report or begin a stop
    # until the identical add request reconciles that pane into authority.
    try:
        _assert_no_pending_team_scale(root_path, run_id)
    except WorkerError as exc:
        raise TeamError(str(exc)) from exc
    session = str(meta.get("session") or "")
    dry = bool(meta.get("dry_run"))
    team_id = str(meta.get("team_id") or "team")
    in_progress = _list_in_progress_api_tasks(root_path, run_id, team_id)
    _write_shutdown_request(
        root_path,
        run_id,
        team_id=team_id,
        force=bool(force),
        in_progress=in_progress,
    )
    if in_progress and not force:
        ids = [str(t.get("id")) for t in in_progress]
        owners: list[str] = []
        for task in in_progress:
            owner = task.get("owner")
            if not owner and isinstance(task.get("claim"), Mapping):
                owner = (task.get("claim") or {}).get("owner")
            owners.append(str(owner or "?"))
        raise TeamError(
            "stop refused: in_progress claims active "
            f"(task_ids={ids}, owners={owners}); "
            "pass --force to tear down anyway"
        )

    # Durable stop intent before process side effects. Only a stale-generation
    # CAS loss may be retried; any other publication failure aborts teardown.
    def _mark_stopping(current: dict[str, Any]) -> dict[str, Any]:
        updated = dict(current)
        if updated.get("stop_state") == "stopped":
            return updated
        updated["stop_state"] = "stopping"
        updated["stop_intent_at"] = _utc_now()
        return updated

    try:
        meta = mutate_team_meta(
            root_path,
            run_id,
            _mark_stopping,
            expected_generation=_read_meta_generation(meta),
        )
    except TeamError as exc:
        if "stale team meta generation" not in str(exc):
            raise TeamError(
                f"stop refused: could not publish stop intent before teardown: {exc}"
            ) from exc
        # CAS loss under lifecycle lock: re-load and re-publish intent once.
        meta = load_team_meta(root_path, run_id)
        try:
            meta = mutate_team_meta(
                root_path,
                run_id,
                _mark_stopping,
                expected_generation=_read_meta_generation(meta),
            )
        except TeamError as retry_exc:
            raise TeamError(
                f"stop refused: could not publish stop intent before teardown: {retry_exc}"
            ) from retry_exc

    actions: list[str] = []
    errors: list[str] = []

    # Job-backed workers: cancel via Jobs plane (never signal PIDs directly).
    worker_topo = str(meta.get("worker_topology") or "pane")
    if worker_topo == "job":
        from omg_cli.team.launch import (
            STATUS_CANCELLED,
            cancel_job_backed_worker,
        )

        job_actions: list[str] = []
        cancelled_tids: set[str] = set()
        soft_skip = {
            "not_job_backed",
            "missing_execution",
            "no_job_id",
        }
        for raw in meta.get("tasks") or []:
            if not isinstance(raw, Mapping):
                continue
            result = cancel_job_backed_worker(
                root_path, raw, reason="team_stop"
            )
            tid = str(raw.get("task_id") or "")
            if result.get("ok"):
                job_actions.append(
                    f"cancel job {result.get('job_id')} for {tid}"
                )
                if tid:
                    cancelled_tids.add(tid)
            elif result.get("reason") in soft_skip:
                continue
            else:
                errors.append(
                    f"job cancel {tid}: {result.get('reason')}"
                )

        # Fail closed: never claim stop_state=stopped / blanket cancelled when
        # any Jobs cancel failed (Team must not say cancelled while Job runs).
        hard_fail = bool(errors)

        def _mark_job_stop(current: dict[str, Any]) -> dict[str, Any]:
            updated = dict(current)
            tasks = []
            for task in updated.get("tasks") or []:
                if not isinstance(task, Mapping):
                    continue
                row = dict(task)
                tid_row = str(row.get("task_id") or "")
                if tid_row in cancelled_tids:
                    row["status"] = STATUS_CANCELLED
                tasks.append(row)
            updated["tasks"] = tasks
            if not hard_fail:
                updated["stop_state"] = "stopped"
                updated["stopped_at"] = _utc_now()
            return updated

        try:
            meta = mutate_team_meta(
                root_path,
                run_id,
                _mark_job_stop,
                expected_generation=_read_meta_generation(meta),
            )
        except TeamError as exc:
            errors.append(f"job-backed stop meta update: {exc}")
        actions.extend(job_actions)
        return {
            "run_id": run_id,
            "session": session,
            "dry_run": dry,
            "force": bool(force),
            "worker_topology": worker_topo,
            "actions": actions,
            "errors": errors,
            "ok": not errors,
            "cancelled_task_ids": sorted(cancelled_tids),
            "note": (
                "job-backed team stop: cancelled via Jobs plane; "
                "worktrees preserved; verified untouched"
                if not errors
                else (
                    "job-backed team stop incomplete: Jobs cancel failed; "
                    "Team did not claim stop_state=stopped for failed cancels; "
                    "verified untouched"
                )
            ),
        }

    verified_targets: list[dict[str, Any]] = []
    receipt: dict[str, Any] | None = None
    identity_verified = False
    session_owned = bool(meta.get("session_owned", True))
    window_id = meta.get("window_id")
    window_id_str = (
        str(window_id)
        if isinstance(window_id, str) and _TMUX_WINDOW_ID.fullmatch(window_id)
        else None
    )
    leader_pane_id_str: str | None = (
        str(meta["leader_pane_id"])
        if isinstance(meta.get("leader_pane_id"), str)
        and _TMUX_PANE_ID.fullmatch(str(meta.get("leader_pane_id")))
        else None
    )
    if session and not dry:
        try:
            chain = _load_team_identity_chain(root_path, run_id, meta)
            receipt = chain[0]
            # Prefer receipt-bound topology when schema v3 stamped it.
            if isinstance(receipt.get("session_owned"), bool):
                session_owned = bool(receipt["session_owned"])
            receipt_window = receipt.get("window_id")
            if (
                isinstance(receipt_window, str)
                and _TMUX_WINDOW_ID.fullmatch(receipt_window) is not None
            ):
                window_id = receipt_window
                window_id_str = receipt_window
            receipt_leader = receipt.get("leader_pane_id")
            if (
                isinstance(receipt_leader, str)
                and _TMUX_PANE_ID.fullmatch(receipt_leader) is not None
            ):
                leader_pane_id_str = receipt_leader
            current_rows = (
                receipt["tasks"] if len(chain) == 1 else chain[-1]["tasks_after"]
            )
            if not tmux_available():
                raise TeamError("tmux unavailable for launch identity readback")
            observed_session = _read_tmux_session_identity(session)
            observed_panes = _list_pane_identities(session)
            if observed_session != (session, receipt["session_id"]):
                raise TeamError("live tmux session identity mismatch")
            if not _team_launch_nonce_matches(
                session=session,
                receipt=receipt,
                session_owned=session_owned,
                window_id=window_id_str,
                pane_ids=[
                    str(row.get("pane_id"))
                    for row in current_rows
                    if isinstance(row.get("pane_id"), str)
                ],
            ):
                raise TeamError("live tmux launch nonce mismatch")
            # observed_panes used only as a soft preflight; kill authority is
            # resolved per-pane via exact receipt process identity below.
            _ = observed_panes
            for row in current_rows:
                resolved = _resolve_live_signal_target(
                    session,
                    receipt,
                    row,
                    session_owned=session_owned,
                    window_id=window_id_str,
                )
                if resolved is None:
                    raise TeamError("live tmux pane/process identity mismatch")
                verified_targets.append(resolved)
            identity_verified = True
        except (TeamError, ProcessLookupError, PermissionError, OSError) as exc:
            errors.append(f"identity verification refused signalling: {exc}")

    # 1) Signal each target only while its exact receipt process identity is
    # still live. Do this before killing tmux: after kill-session the pane
    # authority is gone and a recorded PGID could already have been reused.
    signalled: list[dict[str, Any]] = []
    attempted_task_ids: set[str] = set()
    process_disappearance_verified = bool(identity_verified and verified_targets)
    for raw in verified_targets:
        tid = raw.get("task_id")
        try:
            if receipt is None:
                identity_verified = False
                errors.append(
                    f"signal identity drift refused signalling for task={tid}"
                )
                process_disappearance_verified = False
                continue
            resolved = _resolve_live_signal_target(
                session,
                receipt,
                raw,
                session_owned=session_owned,
                window_id=window_id_str,
            )
            if resolved is None:
                # Worker may have already exited (claim→completed) while the
                # owned session/nonce still match. Treat as already stopped
                # only when pane absence is *proven* (not probe-unknown).
                session_ok = _read_tmux_session_identity(session) == (
                    session,
                    receipt.get("session_id"),
                ) and _team_launch_nonce_matches(
                    session=session,
                    receipt=receipt,
                    session_owned=session_owned,
                    window_id=window_id_str,
                    pane_ids=[str(raw["pane_id"])]
                    if isinstance(raw.get("pane_id"), str)
                    else None,
                )
                pane_id = raw.get("pane_id")
                pane_absent: bool | None = None
                pane_probe_error: str | None = None
                if isinstance(pane_id, str) and _TMUX_PANE_ID.fullmatch(pane_id):
                    pane_absent, pane_probe_error = _pane_proven_absent(pane_id)
                if session_ok and pane_absent is True:
                    actions.append(f"process already gone before signal task={tid}")
                    attempted_task_ids.add(str(tid))
                    continue
                identity_verified = False
                process_disappearance_verified = False
                if pane_probe_error:
                    errors.append(
                        f"signal identity drift refused signalling for task={tid}: "
                        f"{pane_probe_error}"
                    )
                else:
                    errors.append(
                        f"signal identity drift refused signalling for task={tid}"
                    )
                continue
            pid = resolved["pid"]
            pgid = resolved["pgid"]
            if not isinstance(pid, int) or not isinstance(pgid, int):
                errors.append(f"verified signal identity became invalid for task={tid}")
                process_disappearance_verified = False
                continue
            target = pgid
            attempted_task_ids.add(str(tid))
            if os.name == "posix":
                try:
                    os.killpg(target, signal.SIGTERM)
                    actions.append(f"killpg:SIGTERM pgid={target} task={tid}")
                    signalled.append({"task_id": tid, "pgid": target, "pid": pid})
                except ProcessLookupError:
                    actions.append(f"process already gone pgid={target} task={tid}")
            else:
                os.kill(pid, signal.SIGTERM)
                actions.append(f"kill:SIGTERM pid={pid} task={tid}")
                signalled.append({"task_id": tid, "pgid": target, "pid": pid})

            # Prefer bounded disappearance poll after SIGTERM when grace is set
            # so live agent processes can exit without forced SIGKILL + identity
            # revalidation races.
            if kill_grace_s and kill_grace_s > 0:
                group_gone, group_error = _wait_process_group_disappearance(
                    pgid, timeout_s=float(kill_grace_s)
                )
                if (
                    not group_gone
                    and group_error
                    and "disappearance timed out" in group_error
                ):
                    # Timeout is not fatal — fall through to SIGKILL escalation.
                    group_error = None
                    group_gone = False
            else:
                group_gone, group_error = _process_group_disappeared(pgid)
            if group_error is not None:
                process_disappearance_verified = False
                errors.append(group_error)
                continue
            if not group_gone:
                # Prefer a fresh exact-identity target. NEVER fall back to a
                # previously cached numeric PGID when the final leader probe
                # returns None (PID gone / PGID reuse risk).
                resolved_kill = _resolve_live_signal_target(
                    session,
                    receipt,
                    resolved,
                    session_owned=session_owned,
                    window_id=window_id_str,
                )
                if resolved_kill is not None:
                    pid = resolved_kill["pid"]
                    pgid = resolved_kill["pgid"]
                    target = pgid
                leader_pgid, leader_error = _receipt_leader_pgid(pid)
                if leader_error is not None:
                    identity_verified = False
                    process_disappearance_verified = False
                    errors.append(leader_error)
                    continue
                session_exact = _read_tmux_session_identity(session) == (
                    session,
                    receipt.get("session_id"),
                ) and _team_launch_nonce_matches(
                    session=session,
                    receipt=receipt,
                    session_owned=session_owned,
                    window_id=window_id_str,
                    pane_ids=[str(resolved.get("pane_id"))]
                    if isinstance(resolved.get("pane_id"), str)
                    else None,
                )
                pane_id = resolved.get("pane_id")
                pane_absent: bool | None = None
                pane_probe_error: str | None = None
                if isinstance(pane_id, str) and _TMUX_PANE_ID.fullmatch(pane_id):
                    pane_absent, pane_probe_error = _pane_proven_absent(pane_id)
                receipt_pid_start = resolved.get("pid_start")
                # Exact live pane identity → SIGKILL that pgid only after a
                # final leader PGID + start-id revalidation. leader_pgid is
                # None must refuse (never authorize from cached numeric PGID).
                if resolved_kill is not None:
                    target = int(resolved_kill["pgid"])
                    pgid = target
                    pid = int(resolved_kill["pid"])
                    start_ok = True
                    if isinstance(receipt_pid_start, str) and receipt_pid_start:
                        live_start = _pid_start_identity(pid)
                        start_ok = live_start == receipt_pid_start
                    escalation_authorized = bool(
                        session_exact
                        and leader_pgid is not None
                        and leader_pgid == pgid
                        and start_ok
                    )
                    if leader_pgid is None:
                        errors.append(
                            f"SIGKILL refused task={tid}: leader gone after "
                            "resolve; refusing PGID-only kill (reuse risk)"
                        )
                    elif not start_ok:
                        errors.append(
                            f"SIGKILL refused task={tid}: pid_start revalidation "
                            "failed after resolve"
                        )
                else:
                    escalation_authorized = False
                    if pane_probe_error:
                        errors.append(
                            f"SIGKILL refused task={tid}: pane probe unknown "
                            f"({pane_probe_error})"
                        )
                    elif pane_absent is True and leader_pgid is None:
                        errors.append(
                            f"SIGKILL refused task={tid}: receipt pane and leader "
                            "gone; refusing PGID-only kill (reuse risk)"
                        )
                    elif pane_absent is False and leader_pgid is None:
                        errors.append(
                            f"SIGKILL refused task={tid}: receipt pane still "
                            "present without exact process identity"
                        )
                    else:
                        errors.append(
                            f"SIGKILL group authority drift refused signalling "
                            f"task={tid}"
                        )
                if not escalation_authorized:
                    identity_verified = False
                    process_disappearance_verified = False
                    if resolved_kill is not None and leader_pgid is not None:
                        errors.append(
                            f"SIGKILL group authority drift refused signalling "
                            f"task={tid}"
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
                    f"leader/group disappearance unproved for task={tid} pid={pid} pgid={pgid}"
                )
        except (PermissionError, OSError) as exc:
            identity_verified = False
            process_disappearance_verified = False
            errors.append(f"signal task={tid}: {exc}")

    # 2) Only after process-group signalling, tear down tmux transport.
    # Owned sessions: kill exact immutable session ID.
    # Inside-tmux (session_owned=False): kill only the team window / panes —
    # never the leader's shared session.
    #
    # When every worker process is already gone, an owned session may have
    # auto-destroyed with its last pane — treat that as verified disappearance
    # instead of requiring a live identity match for kill-session.
    session_disappearance_verified = bool(dry)
    if session and not dry:
        session_still_exact = False
        session_already_gone = False
        try:
            has = _tmux_run(["has-session", "-t", session])
            has_rc = getattr(has, "returncode", None) if has is not None else None
            if (
                has_rc == 1
                and identity_verified
                and process_disappearance_verified
            ):
                # tmux has-session: 0 = exists, 1 = missing. Other codes are
                # probe errors and must not count as verified disappearance.
                session_already_gone = True
                session_disappearance_verified = True
                actions.append(
                    f"tmux session already gone after process teardown {session}"
                )
            elif has_rc not in (None, 0, 1):
                session_still_exact = False
                errors.append(
                    f"tmux has-session readback refused exit {has_rc} for {session}"
                )
            elif has_rc == 0 or has_rc is None:
                session_still_exact = bool(
                    identity_verified
                    and process_disappearance_verified
                    and receipt is not None
                    and _read_tmux_session_identity(session)
                    == (session, receipt.get("session_id"))
                    and _team_launch_nonce_matches(
                        session=session,
                        receipt=receipt,
                        session_owned=session_owned,
                        window_id=window_id_str,
                        pane_ids=[
                            str(t.get("pane_id"))
                            for t in verified_targets
                            if isinstance(t.get("pane_id"), str)
                        ],
                    )
                )
        except OSError as exc:
            session_still_exact = False
            errors.append(f"tmux pre-kill identity readback: {exc}")
        if session_already_gone:
            pass
        elif session_still_exact and receipt is not None:
            session_id = str(receipt["session_id"])
            try:
                if session_owned:
                    stop_sock, stop_server = _tmux_scope_from_launch(meta)
                    if stop_server is not None:
                        from omg_cli.team.tmux import _cleanup_session

                        kill_err = _cleanup_session(
                            (session, session_id),
                            socket_path=stop_sock,
                            expected_server=stop_server,
                        )
                        if kill_err is None:
                            session_disappearance_verified = True
                            actions.append(f"tmux kill-session -t {session_id}")
                            actions.append(
                                f"tmux disappearance verified {session_id}"
                            )
                        else:
                            errors.append(kill_err)
                    else:
                        r = _tmux_run(["kill-session", "-t", session_id])
                        probe = _tmux_run(["has-session", "-t", session_id])
                        if r.returncode == 0 and probe.returncode == 1:
                            session_disappearance_verified = True
                            actions.append(f"tmux kill-session -t {session_id}")
                            actions.append(
                                f"tmux disappearance verified {session_id}"
                            )
                        elif r.returncode != 0:
                            errors.append(
                                f"tmux kill-session failed for {session_id}: "
                                f"exit {r.returncode}"
                            )
                        else:
                            errors.append(
                                "tmux session disappearance unproved "
                                f"for {session_id}: has-session exit "
                                f"{probe.returncode}"
                            )
                else:
                    # Shared session: remove only owned team transport.
                    # same_window: kill worker panes only (never leader window).
                    # dedicated_window: kill-window only with explicit mode.
                    # Ambiguous / missing mode: pane-scoped fail-closed.
                    from omg_cli.team.tmux import (
                        _intent_tmux_server,
                        _kill_panes,
                        _kill_panes_scoped,
                        _kill_window,
                    )
                    from omg_cli.team.topology import (
                        VIEW_MODE_DEDICATED_WINDOW,
                        VIEW_MODE_SAME_WINDOW,
                        TopologyError,
                        resolve_persisted_view_mode,
                    )

                    try:
                        stop_view = resolve_persisted_view_mode(
                            meta, receipt=receipt
                        )
                    except TopologyError as topo_exc:
                        # NEVER default to dedicated_window + kill-window —
                        # same_window runs store the leader window_id.
                        errors.append(
                            f"view_mode unresolved — pane-scoped cleanup only "
                            f"(refuse kill-window): {topo_exc}"
                        )
                        stop_view = VIEW_MODE_SAME_WINDOW
                    expected_server = _intent_tmux_server(meta)
                    if expected_server is None and receipt is not None:
                        expected_server = _intent_tmux_server(receipt)
                    sock = (
                        str(expected_server["tmux_socket_path"])
                        if expected_server is not None
                        else None
                    )
                    if (
                        stop_view == VIEW_MODE_DEDICATED_WINDOW
                        and isinstance(window_id, str)
                        and window_id
                    ):
                        win_err = _kill_window(
                            window_id,
                            socket_path=sock,
                            expected_session_id=session_id,
                            expected_server=expected_server,
                        )
                        if win_err:
                            errors.append(win_err)
                        else:
                            actions.append(f"tmux kill-window -t {window_id}")
                    pane_ids = [
                        str(rec.get("pane_id"))
                        for rec in (meta.get("tasks") or [])
                        if isinstance(rec, Mapping)
                        and isinstance(rec.get("pane_id"), str)
                    ]
                    if stop_view == VIEW_MODE_SAME_WINDOW:
                        leader = leader_pane_id_str or meta.get("leader_pane_id")
                        scoped_err = _kill_panes_scoped(
                            pane_ids,
                            expected_server=expected_server,
                            expected_session_id=session_id,
                            expected_window_id=(
                                window_id if isinstance(window_id, str) else None
                            ),
                            intent_or_launch_nonce=(
                                str(meta.get("launch_nonce"))
                                if isinstance(meta.get("launch_nonce"), str)
                                else None
                            ),
                            leader_pane_id=(
                                str(leader) if isinstance(leader, str) else None
                            ),
                            socket_path=sock,
                        )
                        if scoped_err:
                            errors.append(scoped_err)
                    # Prove worker panes are gone (session may still exist).
                    remaining = []
                    for pane_id in pane_ids:
                        probe = _tmux_run(
                            ["display-message", "-p", "-t", pane_id, "#{pane_id}"]
                        )
                        if (
                            probe.returncode == 0
                            and (probe.stdout or "").strip() == pane_id
                        ):
                            remaining.append(pane_id)
                    if (
                        remaining
                        and stop_view != VIEW_MODE_SAME_WINDOW
                        and not (isinstance(window_id, str) and window_id)
                    ):
                        pane_err = _kill_panes(remaining)
                        if pane_err:
                            errors.append(pane_err)
                        remaining = [
                            pane_id
                            for pane_id in remaining
                            if _tmux_run(
                                [
                                    "display-message",
                                    "-p",
                                    "-t",
                                    pane_id,
                                    "#{pane_id}",
                                ]
                            ).returncode
                            == 0
                        ]
                    if not remaining:
                        session_disappearance_verified = True
                        actions.append(
                            "tmux inside-mode worker panes/window removed (shared session kept)"
                        )
                    else:
                        errors.append(
                            "tmux inside-mode worker panes still live: "
                            + ",".join(remaining)
                        )
            except OSError as exc:
                errors.append(f"tmux teardown: {exc}")
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

    # Cancel linked ralph composition state when present (D4 team+ralph).
    # Done before the locked team.json mutate so a failed ralph cancel still
    # lands stop state; ralph path is non-authoritative for process identity.
    linked_ralph = meta.get("linked_ralph")
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

    # Cancel Team-linked / in-flight ACP session sidecar (#105 PR4).
    # Stop acquires the ACP transaction lock, re-reads the binding, then
    # CAS-unlinks ensuring/no-job_id or cancels job-bearing bindings.
    # Unproven cancel → stop_completed=false; binding retained.
    if stop_completed:
        linked_acp = meta.get("linked_acp_session")
        has_job = isinstance(linked_acp, Mapping) and bool(linked_acp.get("job_id"))
        try:
            from omg_cli.jobs.runtime import (
                cancel_job,
                resolve_acp_binding_for_team_stop,
            )

            resolved = resolve_acp_binding_for_team_stop(
                root_path,
                run_id,
                reason="team_stop",
                force=force,
                linked_acp=linked_acp if isinstance(linked_acp, Mapping) else None,
            )
            for act in resolved.get("actions") or []:
                actions.append(str(act))
            for err in resolved.get("errors") or []:
                errors.append(str(err))

            if not resolved.get("stop_ok", True):
                stop_completed = False
            elif (
                not resolved.get("attempted")
                and not resolved.get("binding_cleared")
                and resolved.get("status") == "no_binding"
                and has_job
                and isinstance(linked_acp, Mapping)
            ):
                # Meta still points at a job but binding was already gone under lock.
                jid = str(linked_acp.get("job_id"))
                try:
                    cancel_job(root_path, jid, reason="team_stop")
                    actions.append(
                        "cancelled linked_acp_session sidecar "
                        f"job_id={jid} "
                        "(sidecar cancellation; not session/close)"
                    )
                except Exception as cancel_exc:  # noqa: BLE001
                    code = getattr(cancel_exc, "code", None) or "E_ACP_CANCEL"
                    if code in {"E_JOB_UNKNOWN", "E_JOB_NOT_FOUND"}:
                        actions.append(
                            "cancelled linked_acp_session sidecar "
                            f"job_id={jid} (already absent {code})"
                        )
                    else:
                        stop_completed = False
                        errors.append(
                            f"linked_acp_session cancel: {cancel_exc} ({code})"
                        )
                        actions.append(
                            "linked_acp_session cancel unproven; "
                            "stop_refused (binding retained)"
                        )
        except Exception as exc:  # noqa: BLE001 — fail closed on stop claim
            stop_completed = False
            errors.append(f"linked_acp_session cancel: {exc}")
            actions.append(
                "linked_acp_session cancel raised; stop_refused (binding retained)"
            )

    # Update team.json without hiding live or uncertain process truth.
    # Locked + generation-fenced publication (#21). Refuse publication when a
    # concurrent scale mutated the task set after we verified identities.
    stop_actions_final = list(actions)
    stop_errors_final = list(errors)
    original_task_ids = frozenset(
        str(t.get("task_id"))
        for t in (meta.get("tasks") or [])
        if isinstance(t, Mapping) and t.get("task_id")
    )
    original_generation = _read_meta_generation(meta)

    def _apply_stop(current: dict[str, Any]) -> dict[str, Any]:
        locked_task_ids = frozenset(
            str(t.get("task_id"))
            for t in (current.get("tasks") or [])
            if isinstance(t, Mapping) and t.get("task_id")
        )
        if locked_task_ids != original_task_ids:
            raise TeamError(
                "team task set changed during stop (concurrent scale?); "
                "re-run stop against the current workers"
            )
        updated = dict(current)
        updated["stop_actions"] = list(stop_actions_final)
        if stop_completed:
            updated["stopped_at"] = _utc_now()
            updated["stop_state"] = "stopped"
            # stop_completed implies ACP gone or never present — drop the link.
            updated.pop("linked_acp_session", None)
            tasks = []
            for rec in updated.get("tasks") or []:
                if isinstance(rec, dict):
                    rec = dict(rec)
                    if rec.get("status") not in ("dry_run",):
                        rec["status"] = "stopped"
                    tasks.append(rec)
                else:
                    tasks.append(rec)
            updated["tasks"] = tasks
        else:
            updated["stop_refused_at"] = _utc_now()
            updated["stop_state"] = "stop_refused"
            updated["stop_refused_reasons"] = list(stop_errors_final) or [
                "exact process/session disappearance was not proved"
            ]
            tasks = []
            for rec in updated.get("tasks") or []:
                if isinstance(rec, dict):
                    rec = dict(rec)
                    if str(rec.get("task_id")) in attempted_task_ids and rec.get(
                        "status"
                    ) not in ("dry_run",):
                        rec["status"] = "launch_unknown"
                    tasks.append(rec)
                else:
                    tasks.append(rec)
            updated["tasks"] = tasks
        return updated

    try:
        mutate_team_meta(
            root_path,
            run_id,
            _apply_stop,
            expected_generation=original_generation,
        )
    except TeamError as exc:
        # Teardown side effects may already be done. Unrelated metadata writers
        # (resume / linked_ralph) can bump generation without changing task IDs;
        # reconcile onto latest doc while keeping the task-set fence.
        if "stale team meta generation" not in str(exc):
            raise
        mutate_team_meta(root_path, run_id, _apply_stop)

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
    ]
    # Shorthand / aggregate extras (not part of status_locked_view).
    if status.get("team_name") is not None:
        lines.append(f"team_name:      {status.get('team_name')}")
    if status.get("topology") is not None:
        lines.append(f"topology:       {status.get('topology')}")
    if status.get("launch_mode") is not None:
        lines.append(f"launch_mode:    {status.get('launch_mode')}")
    if (
        status.get("startup_status") is not None
        or status.get("startup_acks") is not None
    ):
        lines.append(
            f"startup_acks:   {status.get('startup_acks')}/"
            f"{status.get('startup_expected')} "
            f"status={status.get('startup_status')}"
        )
    mailbox = status.get("mailbox")
    if isinstance(mailbox, Mapping):
        msgs = mailbox.get("messages") or []
        lines.append(
            f"mailbox:        leader-fixed count={len(msgs)} "
            f"(metadata only; ACK bodies via startup_acks)"
        )
    summary = status.get("api_summary")
    if isinstance(summary, Mapping):
        lines.append(
            f"api_summary:    workers={summary.get('workerCount')} "
            f"tasks={summary.get('taskCount')} "
            f"open={summary.get('openTaskCount')}"
        )
    worktrees = status.get("worktrees")
    if isinstance(worktrees, list) and worktrees:
        lines.append(f"worktrees:      {len(worktrees)}")
        for row in worktrees:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"  - {row.get('task_id')}: {row.get('worktree')} [{row.get('status')}]"
            )
    lines.extend(
        [
            "",
            f"{'task_id':<20} {'win':>4} {'alive':<6} {'status':<12} worktree",
            "-" * 72,
        ]
    )
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
    "DISABLE_ENV",
    "EXPERIMENTAL_ENV",
    "STATUS_TASK_KEYS",
    "STATUS_TOP_KEYS",
    "TEAM_WORKER_ENV",
    "TeamError",
    "TeamGateError",
    "WORKER_ENV_MARKERS",
    "WORKSPACE_MODE",
    "build_executor_pane_command",
    "build_fixture_pane_command",
    "build_supervisor_prefix",
    "build_worker_ready_prefix",
    "materialize_supervisor_pane_command",
    "wrap_pane_with_supervisor",
    "wrap_pane_with_worker_ready",
    "build_team_task_prompt",
    "collect_team",
    "experimental_enabled",
    "format_status_table",
    "in_spawned_worker_context",
    "LEADER_ONLY_OPERATOR_ACTIONS",
    "load_team_meta",
    "mutate_team_meta",
    "refuse_nested_team_launch",
    "refuse_worker_operator_mutation",
    "start_team",
    "status_locked_view",
    "stop_team",
    "team_dir",
    "team_meta_lock_path",
    "team_meta_path",
    "team_shutdown_request_path",
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
