"""Canonical shorthand launch orchestrator for ``omg team N[:role] "<goal>"``."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.contracts.path_keys import ensure_managed_dir, safe_path_key
from omg_cli.contracts.state_schemas import require_safe_id
from omg_cli.evidence import CLI_WRITER
from omg_cli.team.api import execute_team_api
from omg_cli.team.decomposition import decompose_goal
from omg_cli.team.mailbox import MailboxError, list_messages, read_message
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

# Bounded wait for worker ACK messages before reporting launch as running.
READY_TIMEOUT_ENV = "OMG_TEAM_READY_TIMEOUT_MS"
DEFAULT_READY_TIMEOUT_MS = 45_000
_LEADER_ID = "leader-fixed"
_ACK_BODY = "ACK"
_ACK_POLL_S = 0.25


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


def ready_timeout_ms(env: Mapping[str, str] | None = None) -> int:
    """Resolve ``OMG_TEAM_READY_TIMEOUT_MS`` (default 45000). Fail closed on junk."""
    raw = None
    if env is not None and READY_TIMEOUT_ENV in env:
        raw = env.get(READY_TIMEOUT_ENV)
    else:
        raw = os.environ.get(READY_TIMEOUT_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_READY_TIMEOUT_MS
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise TeamGateError(
            f"{READY_TIMEOUT_ENV} must be a non-negative integer (ms)"
        ) from exc
    if value < 0:
        raise TeamGateError(f"{READY_TIMEOUT_ENV} must be >= 0")
    return value


def collect_startup_ack_workers(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
) -> set[str]:
    """Return worker ids that sent body=ACK to ``leader-fixed``."""
    try:
        listing = list_messages(
            root,
            run_id=run_id,
            team_id=team_id,
            recipient_id=_LEADER_ID,
            limit=512,
        )
    except (MailboxError, OSError, ValueError):
        return set()
    senders: set[str] = set()
    for row in listing.get("messages") or []:
        if not isinstance(row, Mapping):
            continue
        message_id = row.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            continue
        try:
            msg = read_message(
                root,
                run_id=run_id,
                team_id=team_id,
                recipient_id=_LEADER_ID,
                message_id=message_id,
            )
        except (MailboxError, OSError, ValueError):
            continue
        if msg.get("body") == _ACK_BODY:
            sender = str(msg.get("sender_id") or "").strip()
            if sender:
                senders.add(sender)
    return senders


def wait_for_startup_acks(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    expected_workers: Sequence[str],
    timeout_ms: int | None = None,
    env: Mapping[str, str] | None = None,
    poll_s: float = _ACK_POLL_S,
) -> dict[str, Any]:
    """Poll leader mailbox until all workers ACK or timeout.

    Returns ``startup_acks``, ``startup_ack_workers``, ``startup_status``:
    - ``running`` — every expected worker ACKed
    - ``degraded`` — some but not all ACKed
    - ``failed_start`` — zero ACKs
    """
    expected = [str(w).strip() for w in expected_workers if str(w).strip()]
    expected_set = set(expected)
    ms = ready_timeout_ms(env) if timeout_ms is None else int(timeout_ms)
    if ms < 0:
        raise TeamGateError("ready timeout_ms must be >= 0")
    deadline = time.monotonic() + (ms / 1000.0)
    acked: set[str] = set()
    while True:
        acked = collect_startup_ack_workers(root, run_id=run_id, team_id=team_id)
        acked &= expected_set
        if expected_set and acked >= expected_set:
            break
        if not expected:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.05, float(poll_s)))

    ordered = [w for w in expected if w in acked]
    count = len(ordered)
    if not expected:
        status = "running"
    elif count == len(expected):
        status = "running"
    elif count == 0:
        status = "failed_start"
    else:
        status = "degraded"
    return {
        "startup_acks": count,
        "startup_ack_workers": ordered,
        "startup_status": status,
        "startup_expected": len(expected),
        "ready_timeout_ms": ms,
    }


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
    executor: str | None = None,
    detach: bool = False,
) -> dict[str, Any]:
    """OMX-like shorthand launch: decompose → start_team(split) → seed api/ref.

    ``executor=\"fixture\"`` swaps pane commands for the hermetic ACK fixture
    (transport smoke only — not Grok live parity).
    """
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
        executor=executor,
        detach=detach,
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

    if dry_run:
        startup = {
            "startup_acks": None,
            "startup_ack_workers": None,
            "startup_status": None,
            "startup_expected": len(tasks),
            "ready_timeout_ms": None,
            "startup_note": (
                f"dry_run skipped ACK wait ({READY_TIMEOUT_ENV} unused)"
            ),
        }
    else:
        expected_workers = [str(t["task_id"]) for t in tasks]
        startup = wait_for_startup_acks(
            root_path,
            run_id=rid,
            team_id=team_id,
            expected_workers=expected_workers,
            env=api_env,
        )
        status = str(startup["startup_status"])
        if status == "running":
            startup["startup_note"] = (
                f"all {startup['startup_acks']} workers ACK'd within "
                f"{startup['ready_timeout_ms']}ms"
            )
        elif status == "degraded":
            startup["startup_note"] = (
                f"partial ACK {startup['startup_acks']}/"
                f"{startup['startup_expected']} within "
                f"{startup['ready_timeout_ms']}ms "
                f"(knob {READY_TIMEOUT_ENV}); state left for diagnosis"
            )
        else:
            startup["startup_note"] = (
                f"zero ACKs within {startup['ready_timeout_ms']}ms "
                f"(knob {READY_TIMEOUT_ENV}); state left for diagnosis"
            )

    meta.update(startup)

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
                **startup,
            }
        )
        note = str(current.get("note") or "").rstrip()
        extra_note = str(startup.get("startup_note") or "")
        if extra_note and extra_note not in note:
            current["note"] = (note + "; " + extra_note).strip("; ").strip()
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        meta = current
    return meta


def status_for_identity(
    root: Path | str, identity: str | None = None
) -> dict[str, Any]:
    """Aggregate shorthand status: locked team_status + mailbox/summary/worktrees.

    Extra keys (``mailbox``, ``api_summary``, ``worktrees``, topology/ACK
    annotations) are explicit and **not** part of ``status_locked_view`` /
    ``--json`` freeze.
    """
    root_path = Path(root).resolve()
    run_id = resolve_team_ref(root_path, identity)
    st = team_status(root_path, run_id)
    team_id = "team"
    try:
        meta = load_team_meta(root_path, run_id)
        st["team_name"] = meta.get("team_name")
        st["team_id"] = meta.get("team_id")
        st["launch_mode"] = meta.get("launch_mode")
        st["topology"] = meta.get("topology")
        st["startup_acks"] = meta.get("startup_acks")
        st["startup_ack_workers"] = meta.get("startup_ack_workers")
        st["startup_status"] = meta.get("startup_status")
        st["startup_expected"] = meta.get("startup_expected")
        if meta.get("team_id"):
            team_id = str(meta["team_id"])
        worktrees: list[dict[str, Any]] = []
        for raw in meta.get("tasks") or []:
            if not isinstance(raw, Mapping):
                continue
            wt = raw.get("worktree")
            tid = raw.get("task_id")
            if not wt and not tid:
                continue
            worktrees.append(
                {
                    "task_id": tid,
                    "worktree": wt,
                    "status": raw.get("status"),
                    "window_index": raw.get("window_index"),
                }
            )
        st["worktrees"] = worktrees
    except TeamError:
        st.setdefault("worktrees", [])

    api_env = {EXPERIMENTAL_ENV: "1"}
    code, envelope = execute_team_api(
        "get-summary",
        {"run_id": run_id, "team_id": team_id},
        root=root_path,
        env=api_env,
    )
    if code == 0 and envelope.get("ok"):
        data = envelope.get("data") or {}
        st["api_summary"] = data.get("summary") if isinstance(data, Mapping) else None
    else:
        st["api_summary"] = None

    try:
        listing = list_messages(
            root_path,
            run_id=run_id,
            team_id=team_id,
            recipient_id=_LEADER_ID,
            limit=512,
        )
        st["mailbox"] = {
            "recipient_id": _LEADER_ID,
            "messages": listing.get("messages") or [],
            "ack_cursor": listing.get("ack_cursor"),
            "next_cursor": listing.get("next_cursor"),
            "has_more": bool(listing.get("has_more")),
        }
    except (MailboxError, OSError, ValueError, TeamError):
        st["mailbox"] = {
            "recipient_id": _LEADER_ID,
            "messages": [],
            "ack_cursor": None,
            "next_cursor": None,
            "has_more": False,
        }
    return st