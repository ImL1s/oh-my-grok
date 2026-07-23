"""Read-only one-shot, JSON, and bounded-watch HUD projections.

The HUD is a CLI-side substitute, not host-rendered Grok chrome.  It reads the
same bounded W4 MCP read models used by in-session tools and never writes
authoritative state.  Adapter availability is diagnostic only and cannot make
an otherwise available run/team core unavailable.
"""
from __future__ import annotations

import time
import math
import signal
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Literal

from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.mcp.tools import dispatch_tool
from omg_cli.redaction import redact_text


HUD_SCHEMA = "omg_hud_snapshot"
HUD_SOURCES = ["run_status.read", "team_status.read", "trace.summary", "trace.timeline"]
MIN_WATCH_INTERVAL_SECONDS = 0.05
MAX_WATCH_INTERVAL_SECONDS = 60.0
MAX_WATCH_ITERATIONS = 10_000
MAX_ADAPTERS = 16
MAX_TEXT_BYTES = 2_048
DEFAULT_STALE_AFTER_SECONDS = 30.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("collected_at must be a canonical UTC timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("collected_at must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("collected_at must be a canonical UTC timestamp")
    return value


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _bounded_text(value: object, maximum: int = MAX_TEXT_BYTES) -> str:
    text = "".join(
        " " if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F else char
        for char in redact_text(str(value))
    )
    body = text.encode("utf-8")
    if len(body) <= maximum:
        return text
    return body[:maximum].decode("utf-8", errors="ignore")


def _run_view(result: Mapping[str, Any]) -> dict[str, Any]:
    if not result.get("ok") or not result.get("found") or not isinstance(result.get("run"), Mapping):
        return {"found": False, "status": "unavailable"}
    run = result["run"]
    return {
        "found": True,
        "run_id": _bounded_text(run.get("run_id") or "?", 128),
        "mode": _bounded_text(run.get("mode") or "-", 128),
        "status": _bounded_text(run.get("status") or "-", 128),
        "stage": _bounded_text(run.get("stage") or run.get("phase") or "-", 128),
        "verified": bool(run.get("verified")),
        "terminal": bool(run.get("terminal")),
        "schema_classification": _bounded_text(run.get("schema_classification") or "unknown", 128),
        "created_at": run.get("created_at") if _timestamp(run.get("created_at")) else None,
        "updated_at": run.get("updated_at") if _timestamp(run.get("updated_at")) else None,
        "host_session": {
            "status": "available" if isinstance(run.get("grok_session_id"), str) else "unavailable",
            "session_id": _bounded_text(run.get("grok_session_id"), 128)
            if isinstance(run.get("grok_session_id"), str)
            else None,
            "state": _bounded_text(run.get("grok_session_state") or "unknown", 64),
            "attempts": run.get("grok_session_attempts")
            if isinstance(run.get("grok_session_attempts"), int)
            and not isinstance(run.get("grok_session_attempts"), bool)
            else 0,
        },
    }


def _team_view(result: Mapping[str, Any]) -> dict[str, Any]:
    if not result.get("ok") or not result.get("found") or not isinstance(result.get("team"), Mapping):
        return {
            "found": False,
            "status": "unavailable",
            "task_count": 0,
            "completed_count": 0,
            "active_count": 0,
            "blocked_count": 0,
        }
    team = result["team"]
    raw_tasks = team.get("tasks")
    tasks = list(raw_tasks) if isinstance(raw_tasks, list) else []
    completed = 0
    active = 0
    blocked = 0
    agents: list[dict[str, str]] = []
    for raw in tasks[:256]:
        if not isinstance(raw, Mapping):
            continue
        state = str(raw.get("state") or raw.get("status") or "unknown")
        if state in {"complete", "completed"}:
            completed += 1
        elif state in {"failed", "cancelled", "blocked", "blocked_permission", "needs_collect"}:
            blocked += 1
        elif state not in {"dry_run", "unknown"}:
            active += 1
        raw_agent = raw.get("worker_id", raw.get("agent_id", raw.get("task_id")))
        if isinstance(raw_agent, str) and raw_agent:
            agents.append(
                {
                    "agent_id": _bounded_text(raw_agent, 128),
                    "state": _bounded_text(state, 64),
                }
            )
    return {
        "found": True,
        "status": "available",
        "transport": _bounded_text(team.get("transport") or "tmux", 128),
        "task_count": min(len(tasks), 256),
        "completed_count": completed,
        "active_count": active,
        "blocked_count": blocked,
        "terminal": bool(team.get("terminal")),
        # Team completion is informational only.  It never grants OMG verification.
        "complete": bool(team.get("complete")),
        "verified": False,
        "agents": agents,
    }


def _adapter_views(adapters: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if adapters is None:
        return []
    if isinstance(adapters, (str, bytes)) or len(adapters) > MAX_ADAPTERS:
        raise ValueError("HUD adapters exceed bounds")
    allowed = {"sidecar", "native_dashboard", "notifications", "tmux"}
    non_promoting_statuses = {
        "configured",
        "disabled",
        "failed",
        "optional_unclaimed",
        "unavailable",
    }
    rows: dict[str, dict[str, Any]] = {}
    for raw in adapters:
        if not isinstance(raw, Mapping):
            raise ValueError("HUD adapter entries must be objects")
        name = str(raw.get("adapter") or "")
        if name not in allowed or name in rows:
            raise ValueError("HUD adapter name is invalid or duplicated")
        requested_status = _bounded_text(raw.get("status") or "optional_unclaimed", 128)
        rows[name] = {
            "adapter": name,
            # Adapter rows are caller-supplied diagnostics, not signed evidence.
            # They can describe configuration/failure but can never self-promote
            # native observation or a capability tier.
            "status": (
                requested_status
                if requested_status in non_promoting_statuses
                else "optional_unclaimed"
            ),
            "enabled": raw.get("enabled") is True,
            "observed": False,
            "evidence_tier": "T0",
        }
    return [rows[name] for name in sorted(rows, key=lambda item: item.encode("utf-8"))]


def collect_hud_snapshot(
    root: Path | str,
    run_id: str | None = None,
    *,
    team_id: str | None = None,
    collected_at: str | None = None,
    adapters: Sequence[Mapping[str, Any]] | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Collect one bounded read-only HUD snapshot from W3/W4 read models."""

    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, (int, float))
        or not math.isfinite(stale_after_seconds)
        or stale_after_seconds <= 0
        or stale_after_seconds > 86_400
    ):
        raise ValueError("HUD stale threshold is invalid")
    project = Path(root).resolve()
    collected = _canonical_timestamp(collected_at or _utc_now())
    collected_time = _timestamp(collected)
    assert collected_time is not None
    run_args = {} if run_id is None else {"run_id": run_id}
    run_result = dispatch_tool("run_status.read", run_args, root=project)
    run = _run_view(run_result)
    resolved_run_id = run.get("run_id") if run.get("found") else run_id
    team_args: dict[str, Any] = {}
    if resolved_run_id:
        team_args["run_id"] = resolved_run_id
    if team_id is not None:
        team_args["team_id"] = team_id
    team_result = dispatch_tool("team_status.read", team_args, root=project)
    team = _team_view(team_result)
    trace_args = {"run_id": resolved_run_id} if resolved_run_id else {}
    trace_summary = dispatch_tool("trace.summary", trace_args, root=project)
    trace_count = trace_summary.get("count") if trace_summary.get("ok") else None
    latest_event: dict[str, Any] | None = None
    if isinstance(trace_count, int) and not isinstance(trace_count, bool) and trace_count > 0:
        trace_result = dispatch_tool(
            "trace.timeline",
            {**trace_args, "cursor": trace_count - 1, "limit": 1},
            root=project,
        )
        rows = trace_result.get("events") if trace_result.get("ok") else None
        if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
            row = rows[0]
            latest_event = {
                "event_id": _bounded_text(row.get("event_id") or "unknown", 128),
                "event_type": _bounded_text(row.get("event_type") or "unknown", 64),
                "source": _bounded_text(row.get("source") or "unknown", 64),
                "observed_at": row.get("observed_at")
                if _timestamp(row.get("observed_at")) is not None
                else None,
            }
    created_time = _timestamp(run.get("created_at"))
    updated_time = _timestamp(run.get("updated_at"))
    elapsed_seconds = (
        max(0, int((collected_time - created_time).total_seconds()))
        if created_time is not None
        else None
    )
    stale = bool(
        run.get("found")
        and not run.get("terminal")
        and (
            updated_time is None
            or (collected_time - updated_time).total_seconds() > float(stale_after_seconds)
        )
    )
    partial = bool(
        not run.get("found")
        or not team.get("found")
        or not trace_summary.get("ok")
        or not isinstance(trace_count, int)
    )
    warnings = [code for flag, code in ((stale, "HUD_STALE"), (partial, "HUD_PARTIAL")) if flag]
    raw_host_session = run.get("host_session")
    host_session: dict[str, Any]
    if isinstance(raw_host_session, Mapping):
        host_session = {str(key): value for key, value in raw_host_session.items()}
    else:
        host_session = {
            "status": "unavailable",
            "session_id": None,
            "state": "unknown",
            "attempts": 0,
        }
    agents = team.get("agents") if isinstance(team.get("agents"), list) else []
    return {
        "store_kind": HUD_SCHEMA,
        "schema_version": 1,
        "repository_id": "OMG",
        "collected_at": collected,
        "sources": list(HUD_SOURCES),
        "run": run,
        "team": team,
        "host_session": host_session,
        "agents": agents,
        "tasks": {
            "total": int(team.get("task_count", 0)),
            "active": int(team.get("active_count", 0)),
            "completed": int(team.get("completed_count", 0)),
            "blocked": int(team.get("blocked_count", 0)),
        },
        "latest_event": latest_event,
        "elapsed_seconds": elapsed_seconds,
        "stale": stale,
        "partial": partial,
        "warnings": warnings,
        "adapters": _adapter_views(adapters),
        "core_available": bool(run.get("found") or team.get("found")),
        "authoritative": False,
        "read_only": True,
    }


def render_hud(snapshot: Mapping[str, Any], format: Literal["text", "json"] = "text") -> str:
    """Render one snapshot as a bounded line or canonical JSON."""

    if format == "json":
        return canonical_json_bytes(dict(snapshot)).decode("utf-8")
    if format != "text":
        raise ValueError("HUD format must be text or json")
    raw_run = snapshot.get("run")
    raw_team = snapshot.get("team")
    run: Mapping[str, Any] = raw_run if isinstance(raw_run, Mapping) else {}
    team: Mapping[str, Any] = raw_team if isinstance(raw_team, Mapping) else {}
    if run.get("found"):
        run_text = f"run={run.get('mode')}|{run.get('status')}|{run.get('stage')}"
    else:
        run_text = "run=unavailable"
    if team.get("found"):
        team_text = (
            f"team={team.get('completed_count', 0)}/{team.get('task_count', 0)}"
            f":blocked={team.get('blocked_count', 0)}"
        )
    else:
        team_text = "team=unavailable"
    adapters = snapshot.get("adapters") if isinstance(snapshot.get("adapters"), list) else []
    adapter_text = "adapters=disabled" if not adapters else "adapters=" + ",".join(
        f"{row.get('adapter')}:{row.get('status')}" for row in adapters if isinstance(row, Mapping)
    )
    session = snapshot.get("host_session")
    session_text = (
        f"session={session.get('state')}"
        if isinstance(session, Mapping) and session.get("status") == "available"
        else "session=unavailable"
    )
    flags = "flags=" + (",".join(snapshot.get("warnings", [])) or "none")
    elapsed = snapshot.get("elapsed_seconds")
    elapsed_text = f"elapsed={int(elapsed)}s" if isinstance(elapsed, (int, float)) else "elapsed=unknown"
    latest = snapshot.get("latest_event")
    latest_text = (
        f"latest={latest.get('event_type')}"
        if isinstance(latest, Mapping)
        else "latest=unavailable"
    )
    return _bounded_text(
        f"omg-hud {run_text} {team_text} {session_text} {latest_text} {elapsed_text} {flags} {adapter_text}",
        MAX_TEXT_BYTES,
    )


def watch_hud(
    root: Path | str,
    run_id: str | None = None,
    *,
    team_id: str | None = None,
    adapters: Sequence[Mapping[str, Any]] | None = None,
    interval_seconds: float = 1.0,
    max_iterations: int = 10,
    stop_event: Event | None = None,
    sink: Callable[[dict[str, Any], int], Any] | None = None,
    collected_at: Callable[[], str] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    handle_signals: bool = True,
    is_tty: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Poll read models with explicit time/iteration bounds and no state writes."""

    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or max_iterations < 1
        or max_iterations > MAX_WATCH_ITERATIONS
        or not isinstance(interval_seconds, (int, float))
        or isinstance(interval_seconds, bool)
        or not math.isfinite(interval_seconds)
        or interval_seconds < MIN_WATCH_INTERVAL_SECONDS
        or interval_seconds > MAX_WATCH_INTERVAL_SECONDS
    ):
        raise ValueError("HUD watch bounds are invalid")
    if not isinstance(handle_signals, bool):
        raise ValueError("HUD signal handling flag is invalid")
    presentation = "watch" if (is_tty or sys.stdout.isatty)() else "plain"
    iterations = 0
    last_snapshot: dict[str, Any] | None = None
    stopped_by = "max_iterations"
    local_stop = stop_event or Event()
    previous_handlers: dict[signal.Signals, Any] = {}
    signal_name: str | None = None

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal signal_name
        signal_name = signal.Signals(signum).name
        local_stop.set()

    can_install = handle_signals and threading.current_thread() is threading.main_thread()
    try:
        if can_install:
            for watched_signal in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[watched_signal] = signal.getsignal(watched_signal)
                signal.signal(watched_signal, request_stop)
        while iterations < max_iterations:
            if local_stop.is_set():
                stopped_by = signal_name or "cancelled"
                break
            last_snapshot = collect_hud_snapshot(
                root,
                run_id,
                team_id=team_id,
                collected_at=collected_at() if collected_at is not None else None,
                adapters=adapters,
            )
            iterations += 1
            if sink is not None:
                sink(last_snapshot, iterations)
            if iterations >= max_iterations:
                break
            sleep(float(interval_seconds))
    finally:
        for watched_signal, previous in previous_handlers.items():
            signal.signal(watched_signal, previous)
    return {
        "iterations": iterations,
        "stopped_by": stopped_by,
        "last_snapshot": last_snapshot,
        "presentation": presentation,
        "signals_restored": bool(not previous_handlers or can_install),
        "read_only": True,
    }


def hud_line(root: Path | str, run_id: str | None = None) -> str:
    """Compatibility one-liner retained for ``omg hud``."""

    snapshot = collect_hud_snapshot(root, run_id)
    run = snapshot["run"]
    if not run.get("found"):
        return "omg-hud: no-active-run"
    rid = str(run.get("run_id") or "?")[:12]
    verified = "V" if run.get("verified") else "-"
    return (
        f"omg-hud: {run.get('mode')}|{run.get('status')}|{run.get('stage')}|"
        f"run={rid}|{verified}"
    )


def hud_pack(root: Path | str, run_id: str | None = None) -> dict[str, Any]:
    """Compatibility JSON pack backed by the structured snapshot."""

    snapshot = collect_hud_snapshot(root, run_id)
    pack = dict(snapshot)
    pack["ok"] = snapshot["core_available"]
    pack["line"] = hud_line(root, run_id)
    if snapshot["run"].get("found"):
        pack.update(snapshot["run"])
    return pack


hud_once = collect_hud_snapshot
bounded_watch = watch_hud

__all__ = [
    "HUD_SCHEMA",
    "MAX_WATCH_ITERATIONS",
    "bounded_watch",
    "collect_hud_snapshot",
    "hud_line",
    "hud_once",
    "hud_pack",
    "render_hud",
    "watch_hud",
]
