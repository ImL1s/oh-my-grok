"""Canonical shorthand launch orchestrator for ``omg team N[:role] "<goal>"``."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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
from omg_cli.team.api import TeamApiError, _op_create_task, execute_team_api
from omg_cli.team.decomposition import decompose_goal
from omg_cli.team.mailbox import MailboxError, list_messages, read_message
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    SCHEMA_VERSION,
    TeamError,
    TeamGateError,
    experimental_enabled,
    load_team_meta,
    mutate_team_meta,
    start_team,
    team_status,
)
from omg_cli.team.worker_protocol import team_worker_protocol_lines
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
        from omg_cli.team.bootstrap import read_bootstrap_summary

        bootstrap = read_bootstrap_summary(
            root, run_id=run_id, team_id=team_id, worker_id=wid
        )
        row: dict[str, Any] = {
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
        if bootstrap is not None:
            # Descriptor only — never inline the full bootstrap.log (#100).
            row["bootstrap"] = bootstrap
        rows.append(row)
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
    interactive_rows = _interactive_worker_rows(
        root, run_id=run_id, expected_workers=expected
    )
    interactive_ids = [str(row["task_id"]) for row in interactive_rows]
    headless_ids = [wid for wid in expected if wid not in set(interactive_ids)]
    if interactive_ids and not headless_ids:
        return _interactive_startup_payload(
            root,
            run_id=run_id,
            workers=interactive_rows,
            timeout_ms=timeout_ms,
            env=env,
            poll_s=poll_s,
        )
    if interactive_ids and headless_ids:
        tui = _interactive_startup_payload(
            root,
            run_id=run_id,
            workers=interactive_rows,
            timeout_ms=timeout_ms,
            env=env,
            poll_s=poll_s,
        )
        acks = wait_for_startup_acks(
            root,
            run_id=run_id,
            team_id=team_id,
            expected_workers=headless_ids,
            timeout_ms=timeout_ms,
            env=env,
            poll_s=poll_s,
        )
        return _combine_interactive_and_ack(
            expected=expected,
            interactive=tui,
            headless=acks,
        )
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


def _interactive_worker_rows(
    root: Path | str,
    *,
    run_id: str,
    expected_workers: Sequence[str],
) -> list[dict[str, Any]]:
    """Return task rows that the leader must TUI-ready-gate (not ACK-gate)."""
    from omg_cli.team.io_capability import (
        IO_MODE_INTERACTIVE_TTY,
        normalize_worker_io_capability,
    )

    expected_set = {str(w).strip() for w in expected_workers if str(w).strip()}
    try:
        meta = load_team_meta(root, run_id)
    except TeamError:
        return []
    rows: list[dict[str, Any]] = []
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        tid = str(raw.get("task_id") or "").strip()
        if tid not in expected_set:
            continue
        cap = normalize_worker_io_capability(raw)
        if cap.io_mode != IO_MODE_INTERACTIVE_TTY:
            continue
        row = dict(raw)
        if "generation" not in row:
            gen = meta.get("identity_generation", 0)
            if isinstance(gen, int) and not isinstance(gen, bool) and gen >= 0:
                row["generation"] = gen
            else:
                row["generation"] = 0
        rows.append(row)
    return rows


def _capture_interactive_pane(
    pane_id: str, *, socket_path: str | None = None
) -> str:
    """Leader capture for TUI-ready evidence. Capture errors are not ready."""
    from omg_cli.team.tmux import TmuxTeamError, capture_pane

    try:
        return capture_pane(pane_id, socket_path=socket_path)
    except (TmuxTeamError, OSError, TypeError, ValueError):
        return ""


def _promote_interactive_input_ready(
    root: Path | str,
    run_id: str,
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """CLI-only ``input_ready`` promotion after proven TUI-ready evidence."""
    from omg_cli.team.io_capability import (
        IO_MODE_INTERACTIVE_TTY,
        interactive_pane_io_ready,
        normalize_worker_io_capability,
        stamp_io_capability,
    )

    proven = {
        str(tid): dict(ev)
        for tid, ev in evidence_by_id.items()
        if isinstance(tid, str) and isinstance(ev, Mapping)
    }

    def _apply(current: dict[str, Any]) -> dict[str, Any]:
        tasks = current.get("tasks")
        if not isinstance(tasks, list):
            return current
        for raw in tasks:
            if not isinstance(raw, dict):
                continue
            tid = str(raw.get("task_id") or "").strip()
            ev = proven.get(tid)
            if ev is None:
                continue
            cap = normalize_worker_io_capability(raw)
            if cap.io_mode != IO_MODE_INTERACTIVE_TTY:
                continue
            marker = ev.get("ready_marker")
            if not isinstance(marker, str):
                continue
            stamp_io_capability(
                raw,
                interactive_pane_io_ready(
                    ready_marker=marker,
                    pane_id=ev.get("pane_id") if isinstance(ev.get("pane_id"), str) else None,
                    provider_pid=ev.get("provider_pid")
                    if isinstance(ev.get("provider_pid"), int)
                    else None,
                    attempt=ev.get("attempt") if isinstance(ev.get("attempt"), int) else None,
                    generation=ev.get("generation")
                    if isinstance(ev.get("generation"), int)
                    else None,
                ),
            )
        return current

    return mutate_team_meta(root, run_id, _apply)


def _interactive_startup_payload(
    root: Path | str,
    *,
    run_id: str,
    workers: Sequence[Mapping[str, Any]],
    timeout_ms: int | None,
    env: Mapping[str, str] | None,
    poll_s: float,
) -> dict[str, Any]:
    """Bounded TUI-ready wait. Never silently downgrades to headless."""
    from omg_cli.team.interactive import (
        INTERACTIVE_GATE_PHASE,
        wait_for_interactive_tui_ready,
    )

    expected = [
        str(row.get("task_id") or "").strip()
        for row in workers
        if isinstance(row, Mapping) and str(row.get("task_id") or "").strip()
    ]
    ms = ready_timeout_ms(env) if timeout_ms is None else int(timeout_ms)
    socket: str | None = None
    try:
        meta = load_team_meta(root, run_id)
        raw_sock = meta.get("tmux_socket_path")
        if isinstance(raw_sock, str) and raw_sock:
            socket = raw_sock
    except TeamError:
        socket = None

    waited = wait_for_interactive_tui_ready(
        workers,
        timeout_ms=ms,
        poll_s=poll_s,
        capture_fn=lambda pane_id: _capture_interactive_pane(
            pane_id, socket_path=socket
        ),
    )
    ready = list(waited.get("ready_workers") or [])
    missing = list(waited.get("missing_workers") or [])
    evidence = waited.get("evidence") if isinstance(waited.get("evidence"), Mapping) else {}
    if ready and isinstance(evidence, Mapping) and evidence:
        _promote_interactive_input_ready(root, run_id, evidence)
    if expected and not missing:
        status = "running"
        note = (
            f"all {len(ready)} interactive workers TUI-ready "
            f"(gate={INTERACTIVE_GATE_PHASE}) within {ms}ms"
        )
    elif ready:
        status = "degraded"
        note = (
            f"partial TUI-ready {len(ready)}/{len(expected)} "
            f"(gate={INTERACTIVE_GATE_PHASE}) within {ms}ms "
            f"(knob {READY_TIMEOUT_ENV}); not a silent headless downgrade"
        )
    else:
        status = "failed_start"
        note = (
            f"zero TUI-ready signals within {ms}ms "
            f"(gate={INTERACTIVE_GATE_PHASE}; knob {READY_TIMEOUT_ENV}); "
            "interactive launch did not downgrade to headless"
        )
    snap = [
        {
            "worker_id": tid,
            "ready": tid in set(ready),
            "gate": INTERACTIVE_GATE_PHASE,
        }
        for tid in expected
    ]
    return {
        "startup_acks": 0,
        "startup_ack_workers": [],
        "startup_process_ready": len(ready),
        "startup_process_ready_workers": list(ready),
        "startup_ready_workers": list(ready),
        "startup_missing_workers": list(missing),
        "startup_blocked_workers": [],
        "startup_workers": snap,
        "startup_status": status,
        "startup_expected": len(expected),
        "startup_gate_phase": INTERACTIVE_GATE_PHASE,
        "ready_timeout_ms": ms,
        "startup_note": note,
    }


def _combine_interactive_and_ack(
    *,
    expected: Sequence[str],
    interactive: Mapping[str, Any],
    headless: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge TUI-ready + supervisor-ACK subsets. Never drop interactive mode."""
    from omg_cli.team.interactive import INTERACTIVE_GATE_PHASE

    ready: list[str] = []
    seen: set[str] = set()
    for src in (interactive, headless):
        for wid in src.get("startup_ready_workers") or []:
            key = str(wid)
            if key in seen:
                continue
            seen.add(key)
            ready.append(key)
    missing = [wid for wid in expected if wid not in seen]
    blocked = [str(w) for w in (headless.get("startup_blocked_workers") or [])]
    if blocked and not ready:
        status = "blocked_start"
    elif not missing:
        status = "running"
    elif not ready:
        status = "failed_start"
    else:
        status = "degraded"
    ms = interactive.get("ready_timeout_ms")
    if not isinstance(ms, int):
        ms = headless.get("ready_timeout_ms")
    note_i = str(interactive.get("startup_note") or "")
    note_h = str(headless.get("startup_note") or "")
    note = "; ".join(p for p in (note_i, note_h) if p)
    return {
        "startup_acks": headless.get("startup_acks") or 0,
        "startup_ack_workers": list(headless.get("startup_ack_workers") or []),
        "startup_process_ready": len(ready),
        "startup_process_ready_workers": list(ready),
        "startup_ready_workers": list(ready),
        "startup_missing_workers": list(missing),
        "startup_blocked_workers": blocked,
        "startup_workers": list(interactive.get("startup_workers") or [])
        + list(headless.get("startup_workers") or []),
        "startup_status": status,
        "startup_expected": len(expected),
        "startup_gate_phase": INTERACTIVE_GATE_PHASE,
        "ready_timeout_ms": ms,
        "startup_note": note or "mixed interactive/headless readiness",
    }


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
        "tasks",
    ):
        if key in persisted:
            out[key] = persisted[key]
    return out


def _create_api_tasks_and_inboxes(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    tasks: Sequence[Mapping[str, Any]],
    env: Mapping[str, str],
) -> dict[str, str]:
    """Seed board tasks + worker inboxes. Safe before pane spawn / team.json.

    Public ``execute_team_api`` requires a published control plane, so the
    leader seed calls ``_op_create_task`` directly. Workers still claim only
    through the gated API after ``team.json`` exists.
    """
    if not experimental_enabled(env):
        raise TeamGateError(
            f"team api seed refused ({EXPERIMENTAL_ENV}=0 or kill switch)"
        )
    worker_names = [str(t["task_id"]) for t in tasks]
    seeded: dict[str, str] = {}
    for index, task in enumerate(tasks):
        subject = str(task.get("subject") or task.get("description") or task["task_id"])
        logical = str(task["task_id"])
        payload: dict[str, Any] = {
            "run_id": run_id,
            "team_id": team_id,
            "subject": subject,
            "description": subject,
            "workers": worker_names if index == 0 else [logical],
        }
        try:
            envelope = _op_create_task(root, payload)
        except TeamApiError as exc:
            raise TeamError(
                f"failed to seed team api task for {logical}: {exc.message}"
            ) from exc
        if not envelope.get("ok"):
            raise TeamError(
                f"failed to seed team api task for {logical}: {envelope}"
            )
        api_task = (envelope.get("data") or {}).get("task") or {}
        api_task_id = str(api_task.get("id") or "")
        if not api_task_id:
            raise TeamError(f"create-task returned no id for {logical}")
        seeded[logical] = api_task_id
        from omg_cli.contracts.path_keys import exclusive_lock
        from omg_cli.team import api as team_api

        path = team_api._task_path(root, run_id, team_id, api_task_id)
        with exclusive_lock(path.with_suffix(".lock")):
            current = team_api._read_task(root, run_id, team_id, api_task_id)
            if current is not None:
                team_api._write_task(
                    root,
                    run_id,
                    team_id,
                    {
                        **current,
                        "binding": {
                            "schema": 1,
                            "logical_worker_id": logical,
                            "api_task_id": api_task_id,
                            "attempt": 1,
                            "launch_generation": 1,
                        },
                        "version": int(current["version"]) + 1,
                    },
                )
        from omg_cli.team.api import _worker_dir  # noqa: PLC0415 — internal seed

        inbox = _worker_dir(root, run_id, team_id, logical) / "inbox.md"
        ensure_managed_dir(inbox.parent)
        inbox.write_text(
            "\n".join(
                [
                    f"# Worker inbox — {logical}",
                    "",
                    f"Team: {team_id}",
                    f"Run: {run_id}",
                    f"Role: {task.get('role')}",
                    f"Board task id: {api_task_id}",
                    "",
                    "## Assignment",
                    subject,
                    "",
                    *team_worker_protocol_lines(
                        run_id=run_id,
                        team_id=team_id,
                        worker_id=logical,
                        api_task_id=api_task_id,
                    ),
                ]
            ),
            encoding="utf-8",
        )
    return seeded


def _stamp_worker_bindings(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    seeded: Mapping[str, str],
) -> None:
    """Stamp logical→board task ids onto team.json after it exists."""
    if not seeded:
        return
    from omg_cli.team.replacement import seed_worker_binding

    def _mutator(meta: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for raw in meta.get("tasks") or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            tid = str(row.get("task_id") or "")
            if tid in seeded:
                gen = 1
                execution = row.get("execution")
                if isinstance(execution, Mapping) and execution.get(
                    "launch_generation"
                ):
                    try:
                        gen = int(execution["launch_generation"])
                    except (TypeError, ValueError):
                        gen = 1
                seed_worker_binding(
                    row,
                    run_id=run_id,
                    team_id=team_id,
                    api_task_id=seeded[tid],
                    attempt=int(row.get("attempt") or 1),
                    launch_generation=max(gen, 1),
                )
            rows.append(row)
        meta["tasks"] = rows
        return meta

    mutate_team_meta(root, run_id, _mutator)


def _seed_api_board(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    tasks: Sequence[Mapping[str, Any]],
    env: Mapping[str, str],
) -> dict[str, str]:
    """Create API tasks + inboxes and stamp team.json bindings when present."""
    seeded = _create_api_tasks_and_inboxes(
        root, run_id=run_id, team_id=team_id, tasks=tasks, env=env
    )
    _stamp_worker_bindings(root, run_id=run_id, team_id=team_id, seeded=seeded)
    return seeded


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
    worker_topology: str | None = None,
    io_mode: str | None = None,
) -> dict[str, Any]:
    """OMX-like shorthand launch: seed API board → start_team(split) → ref.

    The API board (numeric task ids + inbox + exact claim CLI) is seeded
    **before** pane spawn so headless grok ``--prompt-file`` (single-turn)
    can claim. ``executor=\"fixture\"`` swaps pane commands for the hermetic
    ACK fixture (transport smoke only — not Grok live parity).

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

    api_env = dict(env or {})
    api_env.setdefault(EXPERIMENTAL_ENV, "1")
    # Leader seeds the board — strip worker markers for this process.
    for key in (
        "OMG_TEAM_WORKER",
        "OMG_PROCESS_FANOUT_WORKER",
        "OMG_SPAWNED_WORKER",
    ):
        api_env.pop(key, None)

    seeded: dict[str, str] = {}

    def _before_spawn(rid: str) -> dict[str, str]:
        seeded.update(
            _create_api_tasks_and_inboxes(
                root_path,
                run_id=rid,
                team_id=team_id,
                tasks=tasks,
                env=api_env,
            )
        )
        return seeded

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
        worker_topology=worker_topology,
        io_mode=io_mode,
        before_spawn=_before_spawn,
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

        phase = "api_board_seed"
        _stamp_worker_bindings(
            root_path, run_id=rid, team_id=team_id, seeded=seeded
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
        st["worker_topology"] = meta.get("worker_topology") or "pane"
        st["view_mode"] = meta.get("view_mode")
        st["startup_acks"] = meta.get("startup_acks")
        st["startup_ack_workers"] = meta.get("startup_ack_workers")
        st["startup_status"] = meta.get("startup_status")
        st["startup_expected"] = meta.get("startup_expected")
        topo = meta.get("tmux_topology")
        if isinstance(topo, Mapping):
            st["topology_generation"] = topo.get("identity_generation")
            layout = topo.get("layout")
            if isinstance(layout, Mapping):
                st["layout_status"] = layout.get("status")
            else:
                st["layout_status"] = None
        else:
            st["topology_generation"] = meta.get("identity_generation")
            st["layout_status"] = None
        if meta.get("team_id"):
            team_id = str(meta["team_id"])
        from omg_cli.team.launch import worker_status_view

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
                    "logical_worker_index": raw.get(
                        "logical_worker_index", raw.get("window_index")
                    ),
                    "attempt": raw.get("attempt", 1),
                    "worker": worker_status_view(raw),
                }
            )
        st["worktrees"] = worktrees
        # Full-status workers annotation (#102 / #69 PR4 / #147 I/O) —
        # not part of locked schema. ``io`` is fail-closed projection only.
        workers_out: list[dict[str, Any]] = []
        for w in worktrees:
            worker_slice = w.get("worker") if isinstance(w.get("worker"), Mapping) else {}
            io_block = None
            if isinstance(worker_slice, Mapping):
                raw_io = worker_slice.get("io")
                if isinstance(raw_io, Mapping):
                    io_block = dict(raw_io)
            workers_out.append(
                {
                    "task_id": w.get("task_id"),
                    "logical_worker_index": w.get("logical_worker_index"),
                    "attempt": w.get("attempt"),
                    "worker": w.get("worker"),
                    "io": io_block,
                }
            )
        st["workers"] = workers_out
    except TeamError:
        st.setdefault("worktrees", [])
        st.setdefault("workers", [])

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
    increments only when at least one worker is safely respawned. After pane
    reconcile/relaunch, reconciles Team API task claims under the same lock.
    """
    from omg_cli.team.api import reconcile_task_claims
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
        team_id_early = str(meta.get("team_id") or "team")
        # #69 PR5: recover pending replacement intents before claim reconcile /
        # job bind so a crashed replace-worker cannot be skipped by generic
        # resume paths.
        from omg_cli.team.replacement import recover_pending_replacement

        replacement_recover = recover_pending_replacement(
            root_path,
            run_id,
            team_id=team_id_early,
            env=env,
            already_locked=True,
        )
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
        # Claim reconciliation shares the lifecycle lock; never materializes
        # a Team API board. team_id comes only from persisted meta (legacy
        # default "team").
        meta_for_claims = load_team_meta(root_path, run_id)
        team_id = str(meta_for_claims.get("team_id") or "team")
        claim_reconcile = reconcile_task_claims(
            root_path,
            run_id=run_id,
            team_id=team_id,
        )
        # Job-backed workers: bind existing Jobs without relaunch (#69 PR4).
        job_bind: dict[str, Any] | None = None
        if str(meta_for_claims.get("worker_topology") or "") == "job":
            from omg_cli.team.launch import resume_bind_job_workers

            job_bind = resume_bind_job_workers(
                root_path,
                list(meta_for_claims.get("tasks") or []),
                team_id=team_id,
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
            "claim_reconcile": claim_reconcile,
            "replacement_recover": replacement_recover,
        }
    )
    if job_bind is not None:
        out["job_bind"] = job_bind
        # Never relaunch job-backed workers on resume — Jobs owns process life.
        if job_bind.get("bound") or job_bind.get("unproven"):
            out["note"] = (
                f"{out.get('note') or ''}; "
                f"job-backed resume bound={len(job_bind.get('bound') or [])} "
                f"unproven={len(job_bind.get('unproven') or [])} "
                "(no relaunch)"
            ).strip("; ")
    # Prefer combined note when workers were touched.
    if relaunch.get("relaunched") or relaunch.get("blocked"):
        out["note"] = (
            f"{reconciled.get('note')}; {relaunch.get('note')}"
        ).strip("; ")
    # #102: retry cosmetic layout repair without bumping identity generation.
    try:
        from omg_cli.team.scaling import _reconcile_lifecycle_layout

        meta_after = load_team_meta(root_path, run_id)
        layout_info = _reconcile_lifecycle_layout(
            meta_after,
            active_count=len(
                [
                    t
                    for t in (meta_after.get("tasks") or [])
                    if isinstance(t, Mapping) and t.get("status") != "scaled_down"
                ]
            ),
        )
        out["layout_status"] = layout_info.get("layout_status")
        out["layout_repair_needed"] = bool(layout_info.get("layout_repair_needed"))
        out["view_mode"] = meta_after.get("view_mode")
    except TeamError:
        out.setdefault("layout_repair_needed", False)
    return out


def _reconcile_envelope(reconcile: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize resume_for_identity output into the #103 reconcile object."""
    status = "ok"
    if reconcile.get("layout_repair_needed"):
        status = "ok_layout_repair_needed"
    return {
        "status": status,
        "relaunched": list(reconcile.get("relaunched") or []),
        "blocked": list(reconcile.get("blocked") or []),
        "skipped": list(reconcile.get("skipped") or []),
        "note": reconcile.get("note"),
        "layout_status": reconcile.get("layout_status"),
        "layout_repair_needed": bool(reconcile.get("layout_repair_needed")),
        "view_mode": reconcile.get("view_mode"),
        "identity_generation": reconcile.get("identity_generation"),
        "raw": dict(reconcile),
    }


def _provider_session_ok(provider_session: Mapping[str, Any]) -> bool:
    """Provider blocked+required fails closed; view/tmux must not override."""
    if not provider_session.get("requested"):
        return True
    status = str(provider_session.get("status") or "")
    if status == "blocked":
        return False
    if status == "available":
        if provider_session.get("ok") is False:
            return False
        execution = provider_session.get("execution")
        if isinstance(execution, dict) and execution.get("status") == "failed":
            return False
    return True


_STOP_STATES_BLOCKING_ACP = frozenset({"stopping", "stopped", "stop_refused"})


def _publish_acp_ensure_intent(root: Path, run_id: str) -> None:
    """Publish pending ACP ensure under the scale lock before spawn.

    Stop must observe this intent (or the jobs binding) before claiming
    ``stop_completed``. Refuses when the team is already stopping/stopped.
    """
    from datetime import datetime, timezone

    from omg_cli.team.scaling import acquire_scale_lock

    def _utc() -> str:
        return datetime.now(timezone.utc).isoformat()

    with acquire_scale_lock(root, run_id):
        meta = load_team_meta(root, run_id)
        stop_state = str(meta.get("stop_state") or "")
        if stop_state in _STOP_STATES_BLOCKING_ACP or meta.get("stopped_at"):
            raise TeamError(
                f"refuse ACP ensure intent: team is stopping/stopped "
                f"(stop_state={stop_state!r})"
            )
        gen = meta.get("meta_generation")
        expected = int(gen) if isinstance(gen, int) and not isinstance(gen, bool) else 0

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            st = str(current.get("stop_state") or "")
            if st in _STOP_STATES_BLOCKING_ACP or current.get("stopped_at"):
                raise TeamError(
                    f"refuse ACP ensure intent: team is stopping/stopped "
                    f"(stop_state={st!r})"
                )
            updated = dict(current)
            updated["linked_acp_session"] = {
                "state": "pending",
                "pending_at": _utc(),
                "job_id": None,
                "session_close": False,
            }
            return updated

        mutate_team_meta(root, run_id, _mutate, expected_generation=expected)


def _clear_acp_ensure_intent(root: Path, run_id: str) -> None:
    """Best-effort clear of a pending ACP intent (ensure failed before bind)."""
    try:
        meta = load_team_meta(root, run_id)
    except TeamError:
        return
    linked = meta.get("linked_acp_session")
    if not isinstance(linked, Mapping) or linked.get("state") != "pending":
        return
    gen = meta.get("meta_generation")
    expected = int(gen) if isinstance(gen, int) and not isinstance(gen, bool) else 0

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        updated = dict(current)
        cur = updated.get("linked_acp_session")
        if isinstance(cur, Mapping) and cur.get("state") == "pending":
            updated.pop("linked_acp_session", None)
        return updated

    try:
        mutate_team_meta(root, run_id, _mutate, expected_generation=expected)
    except TeamError:
        pass


def _wrap_provider_resume(
    provider_resume: Any | None,
    *,
    root: Path,
    run_id: str,
) -> Any | None:
    """Bind root/run_id into the Team-injected ACP ensure callable."""
    if provider_resume is None:
        return None

    def _helper(gate: Any) -> Mapping[str, Any]:
        # Signature: ensure_acp_session_for_team(gate, *, root, run_id)
        try:
            return provider_resume(gate, root=root, run_id=run_id)
        except TypeError:
            # Legacy test helpers: (gate) -> dict
            return provider_resume(gate)

    return _helper


def view_team(
    root: Path | str,
    identity: str | None = None,
    *,
    print_only: bool = False,
    takeover: bool = False,
    as_json: bool = False,
    worker_id: str | None = None,
    is_tty: bool | None = None,
    execute_effects: bool = True,
    request_provider_session: bool = False,
    session_resume_gate: Any | None = None,
    provider_resume: Any | None = None,
) -> dict[str, Any]:
    """Restore Team interactive view without reconcile/relaunch (#103).

    Provider-session outcome is independent of tmux view (gate injected by CLI).
    """
    from omg_cli.team.operator import plan_and_execute_team_view
    from omg_cli.team.view import MODE_PRINT, MODE_VIEW, provider_session_result

    mode = MODE_PRINT if print_only else MODE_VIEW
    view_out = plan_and_execute_team_view(
        root,
        identity,
        mode=mode,
        as_json=as_json,
        takeover=takeover,
        is_tty=is_tty,
        worker_id=worker_id,
        execute_effects=execute_effects and not print_only,
    )
    run_id = view_out.get("run_id") or resolve_team_ref(root, identity)
    root_path = Path(root).resolve()
    provider_session = provider_session_result(
        requested=bool(request_provider_session),
        gate=session_resume_gate,
        provider_resume=_wrap_provider_resume(
            provider_resume, root=root_path, run_id=str(run_id)
        ),
    )
    view_ok = bool(view_out.get("ok"))
    return {
        "run_id": run_id,
        "reconcile": {
            "status": "skipped",
            "relaunched": [],
            "blocked": [],
            "note": "team view does not reconcile or relaunch",
        },
        "provider_session": provider_session,
        "view": view_out.get("view") or {},
        # View success never implies provider resume; blocked provider fails closed.
        "ok": view_ok and _provider_session_ok(provider_session),
        "command": "team.view",
        "plan": view_out.get("plan"),
        "print_hint": view_out.get("print_hint"),
        "error": view_out.get("error"),
        "delegated": view_out.get("delegated"),
        "focus": view_out.get("focus"),
        "effect": view_out.get("effect"),
    }


def resume_with_view(
    root: Path | str,
    identity: str | None = None,
    *,
    view: bool = False,
    print_only: bool = False,
    takeover: bool = False,
    as_json: bool = False,
    worker_id: str | None = None,
    is_tty: bool | None = None,
    env: Mapping[str, str] | None = None,
    request_provider_session: bool = False,
    session_resume_gate: Any | None = None,
    provider_resume: Any | None = None,
    after_acp_ready_before_bind: Callable[[Path, str, Mapping[str, Any]], None]
    | None = None,
) -> dict[str, Any]:
    """Reconcile under lifecycle lock, then optionally restore view (#103/#105).

    View/attach never runs while the scale lock is held.
    ``session_resume_gate`` must be injected by the CLI (or tests) — this
    function does not probe the host or re-parse versions.

    When ``--provider-session`` will invoke ACP ensure (AVAILABLE gate), a
    **pending** ``linked_acp_session`` intent is published under the scale lock
    **before** spawn so concurrent ``stop`` cannot claim ``stop_completed``
    while an in-flight sidecar is invisible. ACP ensure still runs after the
    scale lock is released (spawn must not hold the lifecycle lock).
    ``after_acp_ready_before_bind`` is a test-only barrier hook.
    """
    from omg_cli.host_models import FeatureGateResult
    from omg_cli.team.operator import plan_and_execute_team_view
    from omg_cli.team.view import (
        MODE_PRINT,
        MODE_VIEW,
        provider_session_result,
    )

    reconcile = resume_for_identity(root, identity, env=env)
    run_id = str(reconcile.get("run_id") or resolve_team_ref(root, identity))
    root_path = Path(root).resolve()

    will_ensure = (
        bool(request_provider_session)
        and isinstance(session_resume_gate, FeatureGateResult)
        and session_resume_gate.state == "AVAILABLE"
        and provider_resume is not None
    )
    intent_error: str | None = None
    published_pending = False
    if will_ensure:
        try:
            load_team_meta(root_path, run_id)
        except TeamError:
            # Unit tests / non-team callers: no stop race surface without meta.
            pass
        else:
            try:
                _publish_acp_ensure_intent(root_path, run_id)
                published_pending = True
            except TeamError as exc:
                intent_error = str(exc)
                will_ensure = False

    provider_session = provider_session_result(
        requested=bool(request_provider_session),
        gate=session_resume_gate,
        provider_resume=(
            None
            if intent_error
            else _wrap_provider_resume(
                provider_resume, root=root_path, run_id=run_id
            )
        ),
    )
    if intent_error:
        provider_session = dict(provider_session)
        provider_session["status"] = "available"
        provider_session["ok"] = False
        provider_session["transport_wired"] = False
        provider_session["invoked"] = False
        provider_session["execution"] = {
            "status": "failed",
            "transport": "acp_stdio_job",
            "error": intent_error,
            "error_code": "E_ACP_ENSURE_REFUSED_STOP",
            "connection_owned": False,
            "no_replay": True,
            "restore_code": False,
        }

    # Bind linked ACP job id into team meta when execution resumed.
    bound_ok = False
    if (
        request_provider_session
        and isinstance(provider_session, dict)
        and provider_session.get("status") == "available"
        and provider_session.get("transport_wired") is True
    ):
        execution = provider_session.get("execution") or {}
        job_id = execution.get("job_id") if isinstance(execution, dict) else None
        if job_id:
            if after_acp_ready_before_bind is not None:
                after_acp_ready_before_bind(
                    root_path, run_id, execution if isinstance(execution, dict) else {}
                )
            try:
                _bind_acp_job_to_team_meta(root_path, run_id, str(job_id), execution)
                bound_ok = True
            except Exception as bind_exc:
                # Compensate: cancel sidecar if Team metadata binding fails
                # (including refuse-on-stopped). Prefer linked cancel so the
                # binding is retained when cancel is unproven.
                from omg_cli.jobs.runtime import cancel_linked_acp_sidecar

                cancel_linked_acp_sidecar(
                    root_path, run_id, reason="team_meta_bind_failed"
                )
                provider_session = dict(provider_session)
                provider_session["ok"] = False
                provider_session["transport_wired"] = False
                provider_session["execution"] = {
                    **(execution if isinstance(execution, dict) else {}),
                    "status": "failed",
                    "error": f"team metadata bind failed: {bind_exc}",
                }

    if published_pending and not bound_ok:
        # Keep pending when the team entered a stop state so a retrying stop
        # still observes the in-flight/sidecar intent. Clear only when the
        # team remains active and ensure/bind did not complete.
        try:
            cur_meta = load_team_meta(root_path, run_id)
            st = str(cur_meta.get("stop_state") or "")
        except TeamError:
            st = ""
        if st not in _STOP_STATES_BLOCKING_ACP:
            _clear_acp_ensure_intent(root_path, run_id)

    envelope: dict[str, Any] = {
        "run_id": run_id,
        "reconcile": _reconcile_envelope(reconcile),
        "provider_session": provider_session,
        # Preserve legacy flat fields for callers that read resume_for_identity shape.
        **{k: v for k, v in reconcile.items() if k != "run_id"},
    }

    if as_json or (not view and not print_only):
        envelope["view"] = {
            "requested": False,
            "status": "none",
            "mode": reconcile.get("view_mode"),
            "action": "none",
            "hint": None,
            "executed": False,
        }
        # Reconcile-only / --json: still fail closed on blocked provider gate.
        envelope["ok"] = _provider_session_ok(provider_session)
        return envelope

    mode = MODE_PRINT if print_only else MODE_VIEW
    view_out = plan_and_execute_team_view(
        root,
        identity,
        mode=mode,
        as_json=False,
        takeover=takeover,
        is_tty=is_tty,
        worker_id=worker_id,
        execute_effects=not print_only,
    )
    envelope["view"] = view_out.get("view") or {}
    envelope["plan"] = view_out.get("plan")
    envelope["print_hint"] = view_out.get("print_hint")
    envelope["effect"] = view_out.get("effect")
    if view_out.get("error"):
        envelope["view_error"] = view_out["error"]
    # Partial outcomes: reconcile/view/provider stay independent; blocked
    # provider must not become overall ok because tmux attached.
    envelope["ok"] = bool(view_out.get("ok")) and _provider_session_ok(
        provider_session
    )
    return envelope


def _bind_acp_job_to_team_meta(
    root: Path,
    run_id: str,
    job_id: str,
    execution: Mapping[str, Any],
) -> None:
    """Bind a live ACP job into team meta; refuse stopping/stopped teams."""
    meta = load_team_meta(root, run_id)
    gen = meta.get("meta_generation")
    expected = int(gen) if isinstance(gen, int) and not isinstance(gen, bool) else 0

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        stop_state = str(current.get("stop_state") or "")
        if stop_state in _STOP_STATES_BLOCKING_ACP or current.get("stopped_at"):
            raise TeamError(
                f"refuse ACP bind into stopping/stopped team "
                f"(stop_state={stop_state!r})"
            )
        updated = dict(current)
        updated["linked_acp_session"] = {
            "state": "bound",
            "job_id": job_id,
            "attempt": execution.get("attempt"),
            "receipt_sha256": execution.get("receipt_sha256"),
            "transport": execution.get("transport"),
            "session_close": False,
        }
        return updated

    mutate_team_meta(root, run_id, _mutate, expected_generation=expected)


def worker_pane_descriptors(
    root: Path | str,
    run_id: str,
    *,
    probe: bool = True,
) -> list[dict[str, Any]]:
    """Bounded worker descriptors for ``omg team panes`` (#101).

    Never includes argv, prompt, env, tokens, or unredacted secrets.
    Authorization flags use the same #102/#98 exact-pane classifier as
    operator capture/key/input.
    """
    from omg_cli.redaction import redact_text
    from omg_cli.team.operator import classify_exact_pane_live
    from omg_cli.team.plane import (
        _TMUX_PANE_ID,
        _load_team_launch_receipt,
        _normalize_identity_row,
        team_launch_receipt_path,
    )
    from omg_cli.team.tmux import tmux_available

    root_path = Path(root).resolve()
    meta = load_team_meta(root_path, run_id)
    dry = bool(meta.get("dry_run"))
    generation = meta.get("identity_generation", 0)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        generation = 0

    session = str(meta.get("session") or "")
    session_id = meta.get("session_id")
    launch_nonce = meta.get("launch_nonce")
    leader_pane_id = meta.get("leader_pane_id")
    if team_launch_receipt_path(root_path, run_id).is_file():
        try:
            receipt = _load_team_launch_receipt(root_path, run_id, meta)
            if isinstance(receipt.get("session_id"), str):
                session_id = receipt["session_id"]
            if isinstance(receipt.get("launch_nonce"), str):
                launch_nonce = receipt["launch_nonce"]
            if isinstance(receipt.get("session_name"), str) and not session:
                session = str(receipt["session_name"])
            if leader_pane_id is None and isinstance(
                receipt.get("leader_pane_id"), str
            ):
                leader_pane_id = receipt["leader_pane_id"]
        except TeamError:
            pass

    expected_session_id = (
        str(session_id) if isinstance(session_id, str) and session_id else None
    )
    expected_nonce = (
        str(launch_nonce) if isinstance(launch_nonce, str) and launch_nonce else None
    )

    rows: list[dict[str, Any]] = []
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        tid = str(raw.get("task_id") or "")
        if not tid:
            continue
        pane_id = raw.get("pane_id")
        exact_pane = (
            isinstance(pane_id, str) and _TMUX_PANE_ID.fullmatch(pane_id) is not None
        )
        is_leader = (
            isinstance(leader_pane_id, str)
            and exact_pane
            and pane_id == leader_pane_id
        )
        attempt_raw = raw.get("attempt", 1)
        attempt = (
            int(attempt_raw)
            if isinstance(attempt_raw, int) and not isinstance(attempt_raw, bool)
            else 1
        )
        status_label = "unknown"
        capture_allowed = False
        focus_allowed = False
        input_allowed = False
        key_allowed = False
        window_id = (
            raw.get("window_id")
            if isinstance(raw.get("window_id"), str)
            else meta.get("window_id")
        )
        if not isinstance(window_id, str):
            window_id = None
        normalized = _normalize_identity_row(raw)
        pane_owner_nonce = normalized.get("pane_owner_nonce")
        if not isinstance(pane_owner_nonce, str) or not pane_owner_nonce:
            pane_owner_nonce = None
        if dry or raw.get("status") == "dry_run":
            status_label = "gone"
        elif is_leader:
            status_label = "identity_mismatch"
        elif not exact_pane:
            status_label = "gone"
        elif not probe or not tmux_available():
            status_label = "unknown"
        elif not expected_session_id or not expected_nonce:
            status_label = "unknown"
        else:
            status_label = classify_exact_pane_live(
                pane_id=str(pane_id),
                session=session,
                session_id=expected_session_id,
                launch_nonce=expected_nonce,
                expected_pid_start=raw.get("pid_start")
                if isinstance(raw.get("pid_start"), str)
                else None,
                expected_pid=raw.get("pid")
                if isinstance(raw.get("pid"), int)
                else None,
                window_id=window_id,
                pane_owner_nonce=pane_owner_nonce,
                socket_path=(
                    str(meta["tmux_socket_path"])
                    if isinstance(meta.get("tmux_socket_path"), str)
                    and meta.get("tmux_socket_path")
                    else None
                ),
            )
            if status_label == "live":
                capture_allowed = True
                focus_allowed = True
                input_allowed = True
                key_allowed = True

        worktree = raw.get("worktree")
        safe_worktree = None
        if isinstance(worktree, str) and worktree:
            # Basename only — never leak full home paths.
            safe_worktree = Path(worktree).name

        cmd_base = None
        pane_command = raw.get("pane_command")
        if isinstance(pane_command, str) and pane_command.strip():
            # First token basename only.
            first = pane_command.strip().split()[0]
            cmd_base = Path(first).name
            cmd_base = redact_text(cmd_base)[:64]

        rows.append(
            {
                "worker_id": tid,
                "task_id": tid,
                "provider": raw.get("provider"),
                "role": raw.get("role"),
                "posture": raw.get("posture"),
                "attempt": attempt,
                "generation": generation,
                "state": raw.get("status"),
                "ready": None,
                "pane_id": pane_id if exact_pane and not is_leader else None,
                "window_id": window_id,
                "pane_owner_bound": bool(pane_owner_nonce),
                "worktree": safe_worktree,
                "command_basename": cmd_base,
                "liveness": status_label,
                "capture_allowed": capture_allowed,
                "focus_allowed": focus_allowed,
                "input_allowed": input_allowed,
                "key_allowed": key_allowed,
            }
        )
    return rows
