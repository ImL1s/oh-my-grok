"""Canonical shorthand launch orchestrator for ``omg team N[:role] "<goal>"``."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    ContractPathError,
    atomic_write_bytes_at,
    ensure_managed_dir,
    exclusive_lock_at,
    open_managed_dir_fd,
    safe_path_key,
)
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
    mutate_team_meta,
    start_team,
    team_status,
)
from omg_cli.team.roles import normalize_role, role_meta

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Bounded wait for worker readiness before reporting launch as running.
# Schema-v2 provider-ready receipts (#99) are the primary gate; mailbox
# body=ACK remains optional enrichment and cannot elevate a dead/unspawned
# provider. Legacy v1 helper receipts are wrapper_ready_legacy only.
READY_TIMEOUT_ENV = "OMG_TEAM_READY_TIMEOUT_MS"
DEFAULT_READY_TIMEOUT_MS = 45_000
_LEADER_ID = "leader-fixed"
_ACK_BODY = "ACK"
_ACK_POLL_S = 0.25
_READY_FILENAME = "ready.json"


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
    """Publish team-name → run_id lookup index atomically (exact 0600).

    Index is non-authoritative for process identity, but still CLI-stamped and
    identity-idempotent: re-writing the same binding is OK; a conflicting
    binding for the same team_name fails closed.
    """
    name = require_safe_id(team_name, label="team_name")
    rid = require_safe_id(run_id, label="run_id")
    tid = require_safe_id(team_id, label="team_id")
    path = team_ref_path(root, name)
    payload = {
        "schema_version": 1,
        "writer": CLI_WRITER,
        "team_name": name,
        "run_id": rid,
        "team_id": tid,
        "note": "lookup index only; mutations stay under .omg/state/runs/<run>/team/",
    }
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        parent_fd = open_managed_dir_fd(path.parent)
    except ContractPathError as exc:
        raise TeamError(
            f"secure team ref directory open refused for {name!r}: {exc}"
        ) from exc
    try:
        # Lock, read, and publish share the pinned ref directory inode.
        with exclusive_lock_at(parent_fd, "ref.lock"):
            try:
                existing_fd = os.open(
                    "ref.json",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                existing_fd = None
            except OSError as exc:
                raise TeamError(
                    f"existing team ref unreadable for {name!r}: {exc}"
                ) from exc
            if existing_fd is not None:
                try:
                    with os.fdopen(
                        existing_fd, "r", encoding="utf-8", closefd=True
                    ) as handle:
                        existing = json.load(handle)
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise TeamError(
                        f"existing team ref unreadable for {name!r}: {exc}"
                    ) from exc
                if not isinstance(existing, dict):
                    raise TeamError(
                        f"existing team ref for {name!r} is not an object"
                    )
                if existing.get("writer") == CLI_WRITER:
                    same = (
                        existing.get("team_name") == name
                        and existing.get("run_id") == rid
                        and existing.get("team_id") == tid
                    )
                    if not same:
                        raise TeamError(
                            f"team ref identity conflict for {name!r}: "
                            f"existing run_id={existing.get('run_id')!r} "
                            f"team_id={existing.get('team_id')!r}; "
                            f"refusing overwrite with run_id={rid!r} "
                            f"team_id={tid!r}"
                        )
            try:
                atomic_write_bytes_at(
                    parent_fd, "ref.json", body, mode=DATA_FILE_MODE, replace=True
                )
            except ContractPathError as exc:
                raise TeamError(
                    f"secure team ref publication refused for {name!r}: {exc}"
                ) from exc
    finally:
        os.close(parent_fd)
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


def worker_ready_path(
    root: Path | str, *, run_id: str, team_id: str, worker_id: str
) -> Path:
    """Per-worker readiness receipt path (v1 legacy or v2 startup)."""
    from omg_cli.team.startup import worker_startup_path

    return worker_startup_path(
        root, run_id=run_id, team_id=team_id, worker_id=worker_id
    )


def write_worker_ready_receipt(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    source: str = "process",
) -> Path:
    """Write legacy v1 process readiness receipt (pane helper).

    **#99:** v1 receipts are classified as ``wrapper_ready_legacy`` and must
    **not** satisfy the production gate for ``startup_status=running``. Prefer
    :func:`omg_cli.team.startup.write_startup_phase` via the supervisor.
    """
    from datetime import datetime, timezone

    from omg_cli.contracts.path_keys import atomic_write_bytes

    path = worker_ready_path(
        root, run_id=run_id, team_id=team_id, worker_id=worker_id
    )
    ensure_managed_dir(path.parent)
    payload = {
        "schema_version": 1,
        "writer": CLI_WRITER,
        "kind": "worker_ready",
        "run_id": require_safe_id(run_id, label="run_id"),
        "team_id": require_safe_id(team_id, label="team_id"),
        "worker_id": require_safe_id(worker_id, label="worker_id"),
        "source": str(source or "process"),
        "ready_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "pid": os.getpid(),
        "note": "legacy v1 helper receipt; not provider-ready (#99)",
    }
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, body, mode=DATA_FILE_MODE)
    return path


def collect_process_ready_workers(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    expected_workers: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> set[str]:
    """Return worker ids with a valid schema-v2 gate-satisfying startup receipt.

    Legacy v1 helper receipts are intentionally excluded (#99 false-green kill).
    Requires distinct live provider identity and phase history.
    """
    from omg_cli.team.startup import (
        classify_startup_payload,
        meets_gate,
        provider_identity_distinct,
        provider_process_alive,
        read_startup_record,
        resolve_gate_phase,
    )

    expected = [
        str(w).strip() for w in (expected_workers or ()) if str(w).strip()
    ]
    gate = resolve_gate_phase(env)
    ready: set[str] = set()
    for wid in expected:
        data = read_startup_record(
            root, run_id=run_id, team_id=team_id, worker_id=wid
        )
        classified = classify_startup_payload(data)
        if classified.get("legacy") or not classified.get("phase"):
            continue
        phases = classified.get("phases") or []
        if not isinstance(phases, list):
            phases = []
        if not meets_gate(
            classified.get("phase"),
            gate=gate,
            phases=[str(p) for p in phases],
        ):
            continue
        if not provider_identity_distinct(
            provider_pid=classified.get("provider_pid")
            if isinstance(classified.get("provider_pid"), int)
            else None,
            supervisor_pid=classified.get("supervisor_pid")
            if isinstance(classified.get("supervisor_pid"), int)
            else None,
            provider_pid_start=(
                str(classified["provider_pid_start"])
                if isinstance(classified.get("provider_pid_start"), str)
                else None
            ),
        ):
            continue
        # Exact live provider identity required for gate satisfaction.
        if not provider_process_alive(
            provider_pid=classified.get("provider_pid")
            if isinstance(classified.get("provider_pid"), int)
            else None,
            provider_pid_start=(
                str(classified["provider_pid_start"])
                if isinstance(classified.get("provider_pid_start"), str)
                else None
            ),
        ):
            continue
        ready.add(wid)
    return ready


def collect_worker_startup_snapshots(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    expected_workers: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic per-worker startup snapshot for JSON/human output."""
    from omg_cli.team.startup import (
        classify_startup_payload,
        meets_gate,
        provider_identity_distinct,
        provider_process_alive,
        read_startup_record,
        resolve_gate_phase,
    )

    gate = resolve_gate_phase(env)
    rows: list[dict[str, Any]] = []
    for wid in expected_workers:
        data = read_startup_record(
            root, run_id=run_id, team_id=team_id, worker_id=wid
        )
        classified = classify_startup_payload(data)
        phase = classified.get("phase")
        phases = classified.get("phases") or []
        if not isinstance(phases, list):
            phases = []
        alive = False
        if isinstance(classified.get("provider_pid"), int) and isinstance(
            classified.get("provider_pid_start"), str
        ):
            alive = provider_process_alive(
                provider_pid=classified["provider_pid"],
                provider_pid_start=classified["provider_pid_start"],
            )
        identity_ok = provider_identity_distinct(
            provider_pid=classified.get("provider_pid")
            if isinstance(classified.get("provider_pid"), int)
            else None,
            supervisor_pid=classified.get("supervisor_pid")
            if isinstance(classified.get("supervisor_pid"), int)
            else None,
            provider_pid_start=(
                str(classified["provider_pid_start"])
                if isinstance(classified.get("provider_pid_start"), str)
                else None
            ),
        )
        gate_ok = bool(
            meets_gate(
                phase if isinstance(phase, str) else None,
                gate=gate,
                phases=[str(p) for p in phases],
            )
            and identity_ok
            and alive
        )
        rows.append(
            {
                "worker_id": wid,
                "phase": phase,
                "evidence_code": classified.get("evidence_code"),
                "blocked_reason": classified.get("blocked_reason"),
                "failure_reason": classified.get("failure_reason"),
                "provider": classified.get("provider"),
                "provider_pid": classified.get("provider_pid"),
                "supervisor_pid": classified.get("supervisor_pid"),
                "provider_alive": alive,
                "identity_ok": identity_ok,
                "legacy": bool(classified.get("legacy")),
                "gate_ok": gate_ok,
            }
        )
    return rows


def collect_startup_ack_workers(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
) -> set[str]:
    """Return worker ids that sent body=ACK to ``leader-fixed`` (mailbox)."""
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
    """Poll until every expected worker reaches the provider-ready gate (#99).

    Schema-v2 startup records with live provider identity are the primary
    gate. Mailbox ``ACK`` is enrichment only — it cannot elevate an
    unspawned/dead/legacy-wrapper worker to ready.

    Returns:
    - ``running`` — every expected worker met the gate with live provider
    - ``degraded`` — some but not all
    - ``failed_start`` — zero gate-ready workers (and none blocked)
    - ``blocked_start`` — at least one worker blocked and none gate-ready
    """
    from omg_cli.team.startup import (
        StartupPhase,
        classify_startup_payload,
        read_startup_record,
        resolve_gate_phase,
        write_startup_phase,
    )

    expected = [str(w).strip() for w in expected_workers if str(w).strip()]
    expected_set = set(expected)
    ms = ready_timeout_ms(env) if timeout_ms is None else int(timeout_ms)
    if ms < 0:
        raise TeamGateError("ready timeout_ms must be >= 0")
    gate = resolve_gate_phase(env)
    deadline = time.monotonic() + (ms / 1000.0)
    acked: set[str] = set()
    process_ready: set[str] = set()
    workers_snap: list[dict[str, Any]] = []

    while True:
        acked = collect_startup_ack_workers(root, run_id=run_id, team_id=team_id)
        acked &= expected_set
        # Optional enrichment: record mailbox_ack only when already gate-ready.
        for wid in sorted(acked):
            data = read_startup_record(
                root, run_id=run_id, team_id=team_id, worker_id=wid
            )
            classified = classify_startup_payload(data)
            phase = classified.get("phase")
            if (
                isinstance(phase, str)
                and phase
                in (
                    StartupPhase.TASK_DISPATCHED.value,
                    StartupPhase.PROVIDER_READY.value,
                )
                and wid in collect_process_ready_workers(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    expected_workers=[wid],
                    env=env,
                )
            ):
                try:
                    write_startup_phase(
                        root,
                        run_id=run_id,
                        team_id=team_id,
                        worker_id=wid,
                        phase=StartupPhase.MAILBOX_ACK,
                        provider=str(classified.get("provider") or "unknown"),
                        evidence_code="mailbox_ack",
                    )
                except Exception:
                    pass

        process_ready = collect_process_ready_workers(
            root,
            run_id=run_id,
            team_id=team_id,
            expected_workers=expected,
            env=env,
        )
        process_ready &= expected_set
        workers_snap = collect_worker_startup_snapshots(
            root,
            run_id=run_id,
            team_id=team_id,
            expected_workers=expected,
            env=env,
        )

        # Early stop: all ready, or all terminal (failed/blocked), or timeout.
        if expected_set and process_ready >= expected_set:
            break
        if not expected:
            break
        terminal_n = 0
        for row in workers_snap:
            phase = row.get("phase")
            if phase in (
                StartupPhase.FAILED.value,
                StartupPhase.BLOCKED.value,
            ):
                terminal_n += 1
        if expected and terminal_n == len(expected) and not process_ready:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.05, float(poll_s)))

    # Final snapshot (mailbox ACK never substitutes for process_ready).
    process_ready = collect_process_ready_workers(
        root,
        run_id=run_id,
        team_id=team_id,
        expected_workers=expected,
        env=env,
    )
    process_ready &= expected_set
    acked = collect_startup_ack_workers(root, run_id=run_id, team_id=team_id)
    acked &= expected_set
    workers_snap = collect_worker_startup_snapshots(
        root,
        run_id=run_id,
        team_id=team_id,
        expected_workers=expected,
        env=env,
    )

    ordered_ready = [w for w in expected if w in process_ready]
    ordered_ack = [w for w in expected if w in acked]
    ordered_proc = list(ordered_ready)
    missing = [w for w in expected if w not in process_ready]
    blocked = [
        w["worker_id"]
        for w in workers_snap
        if w.get("phase") == StartupPhase.BLOCKED.value
    ]
    count = len(ordered_ready)
    if not expected:
        # Vacuous "success" with zero workers is a false claim — fail closed.
        status = "failed_start"
    elif count == len(expected):
        status = "running"
    elif count == 0 and blocked:
        status = "blocked_start"
    elif count == 0:
        status = "failed_start"
    else:
        status = "degraded"
    return {
        "startup_acks": len(ordered_ack),
        "startup_ack_workers": ordered_ack,
        "startup_process_ready": len(ordered_proc),
        "startup_process_ready_workers": ordered_proc,
        "startup_ready_workers": ordered_ready,
        "startup_missing_workers": missing,
        "startup_blocked_workers": blocked,
        "startup_workers": workers_snap,
        "startup_status": status,
        "startup_expected": len(expected),
        "startup_gate_phase": gate,
        "ready_timeout_ms": ms,
    }


def startup_readiness_payload(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    expected_workers: Sequence[str],
    dry_run: bool = False,
    no_wait: bool = False,
    timeout_ms: int | None = None,
    env: Mapping[str, str] | None = None,
    poll_s: float = _ACK_POLL_S,
) -> dict[str, Any]:
    """Shared readiness contract for ``team launch`` and ``team start`` (#20/#99).

    Status vocabulary:
    - ``running`` — all expected workers reached provider gate with live identity
    - ``degraded`` / ``failed_start`` / ``blocked_start`` — partial / zero / blocked
    - ``unverified_start`` — explicit ``--no-wait`` (no readiness proof)
    - dry-run: ``startup_status=None`` with a skip note (not a live success claim)
    """
    expected = [str(w).strip() for w in expected_workers if str(w).strip()]
    if dry_run:
        return {
            "startup_acks": None,
            "startup_ack_workers": None,
            "startup_missing_workers": None,
            "startup_workers": None,
            "startup_status": None,
            "startup_expected": len(expected),
            "ready_timeout_ms": None,
            "startup_note": (
                f"dry_run skipped ACK wait ({READY_TIMEOUT_ENV} unused)"
            ),
        }
    if no_wait:
        return {
            "startup_acks": None,
            "startup_ack_workers": None,
            "startup_missing_workers": list(expected),
            "startup_workers": None,
            "startup_status": "unverified_start",
            "startup_expected": len(expected),
            "ready_timeout_ms": None,
            "startup_note": (
                "no-wait: readiness not collected; not a proven Team started"
            ),
        }
    startup = wait_for_startup_acks(
        root,
        run_id=run_id,
        team_id=team_id,
        expected_workers=expected,
        timeout_ms=timeout_ms,
        env=env,
        poll_s=poll_s,
    )
    status = str(startup["startup_status"])
    proc_n = int(startup.get("startup_process_ready") or 0)
    ack_n = int(startup.get("startup_acks") or 0)
    ready_n = len(startup.get("startup_ready_workers") or [])
    gate = startup.get("startup_gate_phase")
    if status == "running":
        startup["startup_note"] = (
            f"all {ready_n} workers provider-ready "
            f"(gate={gate}, process={proc_n}, mailbox_ack={ack_n}) within "
            f"{startup['ready_timeout_ms']}ms"
        )
    elif status == "degraded":
        startup["startup_note"] = (
            f"partial provider-ready {ready_n}/"
            f"{startup['startup_expected']} "
            f"(gate={gate}, process={proc_n}, mailbox_ack={ack_n}) within "
            f"{startup['ready_timeout_ms']}ms "
            f"(knob {READY_TIMEOUT_ENV}); state left for diagnosis"
        )
    elif status == "blocked_start":
        blocked = startup.get("startup_blocked_workers") or []
        startup["startup_note"] = (
            f"blocked_start: workers={blocked} require human action "
            f"(auth/trust); gate={gate}"
        )
    else:
        startup["startup_note"] = (
            f"zero provider-ready signals within {startup['ready_timeout_ms']}ms "
            f"(gate={gate}; knob {READY_TIMEOUT_ENV}); "
            "state left for diagnosis"
        )
    return startup


def persist_startup_annotations(
    root: Path | str,
    run_id: str,
    annotations: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically merge startup / launch annotations into team.json (#21)."""
    root_path = Path(root).resolve()
    extra_note = str(annotations.get("startup_note") or "")
    payload = dict(annotations)

    def _apply(current: dict[str, Any]) -> dict[str, Any]:
        current.update(payload)
        note = str(current.get("note") or "").rstrip()
        if extra_note and extra_note not in note:
            current["note"] = (note + "; " + extra_note).strip("; ").strip()
        return current

    return mutate_team_meta(root_path, run_id, _apply)


def apply_start_readiness(
    root: Path | str,
    meta: Mapping[str, Any],
    *,
    dry_run: bool = False,
    no_wait: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Attach shared readiness fields after a successful ``start_team`` (#20)."""
    root_path = Path(root).resolve()
    rid = str(meta.get("run_id") or "")
    if not rid:
        raise TeamError("start readiness requires meta.run_id")
    team_id = str(meta.get("team_id") or "team")
    expected: list[str] = []
    for raw in meta.get("tasks") or []:
        if isinstance(raw, Mapping) and raw.get("task_id"):
            expected.append(str(raw["task_id"]))
    startup = startup_readiness_payload(
        root_path,
        run_id=rid,
        team_id=team_id,
        expected_workers=expected,
        dry_run=dry_run or bool(meta.get("dry_run")),
        no_wait=no_wait,
        env=env,
    )
    out = dict(meta)
    out.update(startup)
    persisted = persist_startup_annotations(
        root_path,
        rid,
        {
            "team_id": team_id,
            "launch_mode": out.get("launch_mode") or "explicit",
            **startup,
        },
    )
    # Prefer locked meta as source of truth for returned fields.
    for key in (
        "startup_acks",
        "startup_ack_workers",
        "startup_process_ready",
        "startup_process_ready_workers",
        "startup_ready_workers",
        "startup_missing_workers",
        "startup_blocked_workers",
        "startup_workers",
        "startup_status",
        "startup_expected",
        "startup_gate_phase",
        "ready_timeout_ms",
        "startup_note",
        "team_id",
        "launch_mode",
        "note",
    ):
        if key in persisted:
            out[key] = persisted[key]
    return out


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


def remove_team_ref(root: Path | str, team_name: str) -> None:
    """Best-effort delete a team-name lookup ref (transaction compensation).

    Uses the same managed-dir open as :func:`write_team_ref` so a symlinked
    team key directory cannot redirect unlink outside the project.
    """
    name = require_safe_id(team_name, label="team_name")
    path = team_ref_path(root, name)
    try:
        parent_fd = open_managed_dir_fd(path.parent)
    except (ContractPathError, OSError):
        return
    try:
        try:
            os.unlink("ref.json", dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError:
            return
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def compensate_failed_launch(
    root: Path | str,
    run_id: str,
    *,
    team_name: str | None = None,
    dry_run: bool = False,
    created_run: bool = True,
    phase: str = "post-start",
) -> list[str]:
    """Reverse-order cleanup after start_team committed but launch aborts (#17).

    Order: stop live team (if any) → remove team ref written by this launch.
    Dry-run clears the active pointer **only** when this launch created the run
    (not when ``--run`` reuses an existing run).
    Returns diagnostic strings (never raises). Callers may promote incomplete
    stop into TeamError.
    """
    errors: list[str] = []
    root_path = Path(root).resolve()
    rid = str(run_id)
    if not dry_run:
        try:
            from omg_cli.team.plane import stop_team

            stop_result = stop_team(root_path, rid, force=True)
            if not bool((stop_result or {}).get("stop_completed")):
                stop_errors = list((stop_result or {}).get("errors") or [])
                detail = "; ".join(str(e) for e in stop_errors if e) or (
                    "exact process/session disappearance was not proved"
                )
                errors.append(f"compensating stop incomplete: {detail}")
        except Exception as stop_exc:  # noqa: BLE001 — surface in caller
            errors.append(f"compensating stop also failed: {stop_exc}")
    elif created_run:
        # Dry-run of a *new* run leaves active + skeleton; clear so retries work.
        # Never clear active when --run reuses a pre-existing run.
        try:
            from omg_cli.state import clear_active

            clear_active(root_path, rid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"clear active after dry_run compensate: {exc}")
    if team_name:
        try:
            remove_team_ref(root_path, team_name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"team ref remove {team_name!r}: {exc}")
    if errors:
        errors.insert(0, f"phase={phase}")
    return errors


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
    view_mode: str | None = None,
) -> dict[str, Any]:
    """OMX-like shorthand launch: decompose → start_team(split) → seed api/ref.

    ``executor=\"fixture\"`` swaps pane commands for the hermetic ACK fixture
    (transport smoke only — not Grok live parity).

    Commit point (#17): ``start_team`` success is the control-plane commit
    (tmux identity + team.json). ACK readiness is post-commit health (#20).
    Failures after start_team trigger compensating stop + ref removal.
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
        view_mode=view_mode,
    )
    rid = str(meta["run_id"])
    # start_team creates a new run unless --run was supplied.
    created_run = run_id is None
    name = _slug_team_name(goal, rid)
    ref_written = False
    phase = "write_team_ref"

    def _abort(exc: BaseException, *, label: str) -> TeamError:
        rb = compensate_failed_launch(
            root_path,
            rid,
            team_name=name if ref_written else None,
            dry_run=dry_run,
            created_run=created_run,
            phase=label,
        )
        details = [str(exc), *rb]
        return TeamError(
            f"team launch transaction failed ({label}): "
            + "; ".join(d for d in details if d)
        )

    try:
        write_team_ref(root_path, team_name=name, run_id=rid, team_id=team_id)
        ref_written = True

        api_env = dict(env or {})
        api_env.setdefault(EXPERIMENTAL_ENV, "1")
        # Leader seeds the board — strip worker markers for this process.
        for key in (
            "OMG_TEAM_WORKER",
            "OMG_PROCESS_FANOUT_WORKER",
            "OMG_SPAWNED_WORKER",
        ):
            api_env.pop(key, None)

        phase = "api_board_seed"
        _seed_api_board(
            root_path,
            run_id=rid,
            team_id=team_id,
            tasks=tasks,
            env=api_env,
        )

        meta = dict(meta)
        meta["team_name"] = name
        meta["team_id"] = team_id
        meta["launch_mode"] = "shorthand"
        meta["topology"] = "split"
        meta["schema_version"] = meta.get("schema_version", SCHEMA_VERSION)

        expected_workers = [str(t["task_id"]) for t in tasks]
        phase = "startup_readiness"
        startup = startup_readiness_payload(
            root_path,
            run_id=rid,
            team_id=team_id,
            expected_workers=expected_workers,
            dry_run=dry_run,
            env=api_env,
        )
        meta.update(startup)

        # Persist annotations onto team.json via locked atomic mutate (#21).
        phase = "startup_annotations"
        meta = persist_startup_annotations(
            root_path,
            rid,
            {
                "team_name": name,
                "team_id": team_id,
                "launch_mode": "shorthand",
                "topology": "split",
                **startup,
            },
        )
        return meta
    except TeamError as exc:
        if "team launch transaction failed" in str(exc):
            raise
        raise _abort(exc, label=phase) from exc
    except Exception as exc:
        raise _abort(exc, label=phase) from exc


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

def resume_for_identity(
    root: Path | str,
    identity: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Reconcile and relaunch under one lifecycle transaction lock.

    Combines :func:`omg_cli.team.scaling.resume_team` with
    :func:`omg_cli.team.scaling.relaunch_dead_incomplete_workers`. Generation
    increments only when at least one worker is safely respawned.
    """
    from omg_cli.team.scaling import (
        _relaunch_dead_incomplete_workers_locked,
        _resume_team_locked_impl,
        acquire_scale_lock,
        pending_identity_wal_operation,
    )

    root_path = Path(root).resolve()
    run_id = resolve_team_ref(root_path, identity)
    with acquire_scale_lock(root_path, run_id):
        meta = load_team_meta(root_path, run_id)
        pending_operation = pending_identity_wal_operation(
            root_path,
            run_id,
            meta,
        )
        if pending_operation == "relaunch":
            # A crash-safe relaunch WAL is the sole mutation authority. Recover
            # it before raw liveness reconciliation, which intentionally gates
            # every pending identity transaction.
            relaunch = _relaunch_dead_incomplete_workers_locked(
                root_path,
                run_id,
                env=env,
            )
            reconciled = _resume_team_locked_impl(
                root_path,
                run_id,
                env=env,
            )
        else:
            reconciled = _resume_team_locked_impl(
                root_path,
                run_id,
                env=env,
            )
            relaunch = _relaunch_dead_incomplete_workers_locked(
                root_path,
                run_id,
                env=env,
            )
    out = dict(reconciled)
    out.update(
        {
            "relaunched": relaunch.get("relaunched") or [],
            "blocked": relaunch.get("blocked") or [],
            "skipped": relaunch.get("skipped") or [],
            "identity_generation": relaunch.get(
                "identity_generation",
                reconciled.get("identity_generation"),
            ),
            "relaunch_note": relaunch.get("note"),
        }
    )
    # Prefer combined note when workers were touched.
    if relaunch.get("relaunched") or relaunch.get("blocked"):
        out["note"] = (
            f"{reconciled.get('note')}; {relaunch.get('note')}"
        ).strip("; ")
    return out
