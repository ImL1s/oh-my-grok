"""Canonical shorthand launch orchestrator for ``omg team N[:role] "<goal>"``."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.contracts.path_keys import ensure_managed_dir, safe_path_key
from omg_cli.contracts.state_schemas import require_safe_id
from omg_cli.evidence import CLI_WRITER
from omg_cli.team.api import execute_team_api
from omg_cli.team.decomposition import decompose_goal
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    SCHEMA_VERSION,
    TeamError,
    TeamGateError,
    load_team_meta,
    start_team,
    team_meta_path,
    team_status,
)
from omg_cli.team.roles import normalize_role, role_meta

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug_team_name(goal: str, run_id: str) -> str:
    base = _SLUG_RE.sub("-", (goal or "team").strip().lower()).strip("-")
    if not base:
        base = "team"
    base = base[:40].strip("-") or "team"
    suffix = re.sub(r"[^a-zA-Z0-9_-]", "", run_id)[-8:] or secrets.token_hex(4)
    return f"{base}-{suffix}"


def team_ref_path(root: Path | str, team_name: str) -> Path:
    name = require_safe_id(team_name, label="team_name")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "team"
        / safe_path_key(name, namespace="team")
        / "ref.json"
    )


def write_team_ref(
    root: Path | str,
    *,
    team_name: str,
    run_id: str,
    team_id: str,
) -> Path:
    path = team_ref_path(root, team_name)
    ensure_managed_dir(path.parent)
    payload = {
        "schema_version": 1,
        "writer": CLI_WRITER,
        "team_name": team_name,
        "run_id": run_id,
        "team_id": team_id,
        "note": "lookup index only; mutations stay under .omg/state/runs/<run>/team/",
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def resolve_team_ref(root: Path | str, identity: str | None) -> str:
    """Resolve team-name or run_id to a run_id.

    Lookup index is non-authoritative: only used for identity resolution.
    """
    root_path = Path(root).resolve()
    if not identity or not str(identity).strip():
        from omg_cli.state import load_active_run

        active = load_active_run(root_path)
        if active is None:
            raise TeamError("no active team run (pass team name or --run)")
        return str(active["run_id"])

    token = str(identity).strip()
    # Prefer run-scoped team.json when identity looks like an existing run.
    try:
        load_team_meta(root_path, token)
        return token
    except TeamError:
        pass

    ref = team_ref_path(root_path, token)
    if not ref.is_file():
        raise TeamError(f"unknown team identity {token!r} (no run / no ref.json)")
    try:
        data = json.loads(ref.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TeamError(f"team ref unreadable for {token!r}: {exc}") from exc
    if data.get("writer") != CLI_WRITER:
        raise TeamError(f"team ref lacks CLI writer authority for {token!r}")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise TeamError(f"team ref missing run_id for {token!r}")
    # Confirm control plane still exists (index is not authoritative).
    load_team_meta(root_path, run_id)
    return run_id


def _ensure_lane_dirs(root: Path, tasks: Sequence[Mapping[str, Any]]) -> None:
    for task in tasks:
        for rel in task.get("owned_files") or []:
            if str(rel).endswith("/") or Path(str(rel)).suffix == "":
                ensure_managed_dir(root / str(rel).rstrip("/"))
                gitkeep = root / str(rel).rstrip("/") / ".gitkeep"
                if not gitkeep.exists():
                    gitkeep.write_text("", encoding="utf-8")
            else:
                path = root / str(rel)
                ensure_managed_dir(path.parent)
                if not path.exists():
                    path.write_text("", encoding="utf-8")


def _seed_api_board(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    tasks: Sequence[Mapping[str, Any]],
    env: Mapping[str, str],
) -> None:
    worker_names = [str(t["task_id"]) for t in tasks]
    # Register workers via first create-task workers= list, then one task each.
    for index, task in enumerate(tasks):
        subject = str(task.get("subject") or task.get("description") or task["task_id"])
        payload: dict[str, Any] = {
            "run_id": run_id,
            "team_id": team_id,
            "subject": subject,
            "description": subject,
            "workers": worker_names if index == 0 else [str(task["task_id"])],
        }
        code, envelope = execute_team_api(
            "create-task", payload, root=root, env=env
        )
        if code != 0 or not envelope.get("ok"):
            raise TeamError(
                f"failed to seed team api task for {task['task_id']}: {envelope}"
            )
        # Write inbox for this worker
        from omg_cli.team.api import _worker_dir  # noqa: PLC0415 — internal seed

        inbox = _worker_dir(root, run_id, team_id, str(task["task_id"])) / "inbox.md"
        ensure_managed_dir(inbox.parent)
        inbox.write_text(
            "\n".join(
                [
                    f"# Worker inbox — {task['task_id']}",
                    "",
                    f"Team: {team_id}",
                    f"Run: {run_id}",
                    f"Role: {task.get('role')}",
                    "",
                    "## Assignment",
                    subject,
                    "",
                    "## Protocol",
                    "1. ACK leader via `omg team api send-message` "
                    f"(from_worker={task['task_id']}, to_worker=leader-fixed, body=ACK).",
                    "2. Claim your task with `omg team api claim-task`.",
                    "3. Work only in your worktree / owned paths; commit when done.",
                    "4. Transition task to completed; never set verified.",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def launch_team(
    goal: str,
    *,
    workers: int,
    role: str = "executor",
    root: Path | str | None = None,
    dry_run: bool = False,
    force: bool = False,
    routing: Mapping[str, Any] | None = None,
    yolo: bool = False,
    safe: bool = False,
    run_id: str | None = None,
    env: Mapping[str, str] | None = None,
    team_id: str = "team",
    check_binary: bool = True,
) -> dict[str, Any]:
    """OMX-like shorthand launch: decompose → start_team(split) → seed api/ref."""
    root_path = Path(root).resolve() if root is not None else Path.cwd().resolve()
    role_n = normalize_role(role)
    role_meta(role_n)  # fail closed
    if workers < 1:
        raise TeamGateError("workers must be >= 1")

    tasks = decompose_goal(goal, workers=workers, role=role_n)
    _ensure_lane_dirs(root_path, tasks)

    meta = start_team(
        goal,
        tasks,
        root=root_path,
        run_id=run_id,
        dry_run=dry_run,
        force=force,
        routing=routing,
        yolo=yolo,
        safe=safe,
        env=env,
        check_binary=check_binary,
        topology="split",
        team_id=team_id,
    )
    rid = str(meta["run_id"])
    name = _slug_team_name(goal, rid)
    write_team_ref(root_path, team_name=name, run_id=rid, team_id=team_id)

    api_env = dict(env or {})
    api_env.setdefault(EXPERIMENTAL_ENV, "1")
    # Leader seeds the board — strip worker markers for this process.
    for key in (
        "OMG_TEAM_WORKER",
        "OMG_PROCESS_FANOUT_WORKER",
        "OMG_SPAWNED_WORKER",
    ):
        api_env.pop(key, None)

    try:
        _seed_api_board(
            root_path,
            run_id=rid,
            team_id=team_id,
            tasks=tasks,
            env=api_env,
        )
    except Exception as exc:
        # State was already written by start_team; surface seed failure clearly.
        raise TeamError(f"team api board seed failed: {exc}") from exc

    meta = dict(meta)
    meta["team_name"] = name
    meta["team_id"] = team_id
    meta["launch_mode"] = "shorthand"
    meta["topology"] = "split"
    meta["schema_version"] = meta.get("schema_version", SCHEMA_VERSION)
    # Persist annotations onto team.json compatibility view
    path = team_meta_path(root_path, rid)
    if path.is_file():
        current = json.loads(path.read_text(encoding="utf-8"))
        current.update(
            {
                "team_name": name,
                "team_id": team_id,
                "launch_mode": "shorthand",
                "topology": "split",
            }
        )
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        meta = current
    return meta


def status_for_identity(
    root: Path | str, identity: str | None = None
) -> dict[str, Any]:
    run_id = resolve_team_ref(root, identity)
    st = team_status(root, run_id)
    try:
        meta = load_team_meta(root, run_id)
        st["team_name"] = meta.get("team_name")
        st["team_id"] = meta.get("team_id")
        st["launch_mode"] = meta.get("launch_mode")
        st["topology"] = meta.get("topology")
    except TeamError:
        pass
    return st
