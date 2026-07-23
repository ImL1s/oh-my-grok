"""Wiki plus the read-only one-shot/JSON/bounded-watch HUD surface."""
from __future__ import annotations

import json
import signal

import pytest

from omg_cli.hud import (
    collect_hud_snapshot,
    hud_line,
    hud_pack,
    render_hud,
    watch_hud,
)
from omg_cli import hud as hud_module
from omg_cli.lsp_tools import probe_tools
from omg_cli.state import create_run
from omg_cli.wiki import ingest, list_pages, query


def test_wiki_ingest_list_query(tmp_path):
    r = ingest(
        tmp_path,
        title="Auth Notes",
        body="Use OAuth PKCE for mobile.",
        tags=["auth", "mobile"],
    )
    assert "auth-notes" in r["slug"]
    pages = list_pages(tmp_path)
    assert any(p["slug"] == "auth-notes" for p in pages)
    hits = query(tmp_path, "PKCE")
    assert hits and "PKCE" in hits[0]["snippet"]


def test_hud_no_run(tmp_path):
    assert "no-active-run" in hud_line(tmp_path)


def test_hud_with_run(tmp_path):
    run = create_run(tmp_path, mode="autopilot", goal="x")
    line = hud_line(tmp_path, run["run_id"])
    assert "autopilot" in line
    assert "omg-hud:" in line
    pack = hud_pack(tmp_path, run["run_id"])
    assert pack["line"].startswith("omg-hud:")


def test_hud_snapshot_uses_bounded_authoritative_read_models(tmp_path):
    run = create_run(tmp_path, mode="autopilot", goal="token=private")
    snapshot = collect_hud_snapshot(
        tmp_path,
        run_id=run["run_id"],
        collected_at="2026-07-22T00:00:00Z",
        adapters=[
            {
                "adapter": "native_dashboard",
                "status": "optional_unclaimed token=private",
                "enabled": False,
                "observed": False,
                "evidence_tier": "T0",
            }
        ],
    )
    assert snapshot["store_kind"] == "omg_hud_snapshot"
    assert snapshot["core_available"] is True
    assert snapshot["run"]["mode"] == "autopilot"
    assert snapshot["sources"] == [
        "run_status.read",
        "team_status.read",
        "trace.summary",
        "trace.timeline",
    ]
    assert set(snapshot) >= {
        "host_session",
        "agents",
        "tasks",
        "latest_event",
        "elapsed_seconds",
        "stale",
        "partial",
        "warnings",
    }
    assert "private" not in json.dumps(snapshot)
    assert render_hud(snapshot, "json") == render_hud(snapshot, "json")
    assert render_hud(snapshot, "text").startswith("omg-hud ")


def test_hud_adapter_diagnostics_cannot_self_promote_capability_evidence(tmp_path):
    snapshot = collect_hud_snapshot(
        tmp_path,
        collected_at="2026-07-22T00:00:00Z",
        adapters=[
            {
                "adapter": "native_dashboard",
                "status": "available",
                "enabled": True,
                "observed": True,
                "evidence_tier": "T4",
            }
        ],
    )
    assert snapshot["adapters"] == [
        {
            "adapter": "native_dashboard",
            "status": "optional_unclaimed",
            "enabled": True,
            "observed": False,
            "evidence_tier": "T0",
        }
    ]


def test_hud_watch_is_bounded_and_read_only(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="x")
    run_dir = tmp_path / ".omg" / "state" / "runs" / run["run_id"]
    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    seen: list[dict] = []
    result = watch_hud(
        tmp_path,
        run_id=run["run_id"],
        max_iterations=3,
        interval_seconds=0.05,
        collected_at=lambda: "2026-07-22T00:00:00Z",
        sleep=lambda _seconds: None,
        sink=lambda snapshot, _iteration: seen.append(snapshot),
    )
    after = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    assert result["iterations"] == 3
    assert result["stopped_by"] == "max_iterations"
    assert len(seen) == 3
    assert before == after


def test_hud_exposes_host_agents_latest_elapsed_stale_and_partial(monkeypatch, tmp_path):
    def fake_dispatch(name, arguments, *, root):
        if name == "run_status.read":
            return {
                "ok": True,
                "found": True,
                "run": {
                    "run_id": "run-1",
                    "mode": "autopilot",
                    "status": "running",
                    "stage": "execution",
                    "verified": False,
                    "terminal": False,
                    "created_at": "2026-07-22T00:00:00Z",
                    "updated_at": "2026-07-22T00:00:10Z",
                    "grok_session_id": "4b2d0b8b-ccdf-4abd-9821-7ccf9f28799b",
                    "grok_session_state": "launched",
                    "grok_session_attempts": 1,
                },
            }
        if name == "team_status.read":
            return {
                "ok": True,
                "found": True,
                "team": {
                    "transport": "native",
                    "tasks": [
                        {"task_id": "task-1", "worker_id": "agent-1", "state": "running"},
                        {"task_id": "task-2", "worker_id": "agent-2", "state": "complete"},
                    ],
                },
            }
        if name == "trace.summary":
            return {"ok": True, "count": 1}
        assert name == "trace.timeline"
        return {
            "ok": True,
            "events": [
                {
                    "event_id": "event-1",
                    "event_type": "turn_completed",
                    "source": "grok-hook",
                    "observed_at": "2026-07-22T00:00:15Z",
                    "payload": {"prompt": "must-not-leak"},
                }
            ],
        }

    monkeypatch.setattr(hud_module, "dispatch_tool", fake_dispatch)
    snapshot = collect_hud_snapshot(
        tmp_path,
        run_id="run-1",
        collected_at="2026-07-22T00:01:00Z",
        stale_after_seconds=30,
    )
    assert snapshot["host_session"]["state"] == "launched"
    assert snapshot["agents"] == [
        {"agent_id": "agent-1", "state": "running"},
        {"agent_id": "agent-2", "state": "complete"},
    ]
    assert snapshot["tasks"] == {"total": 2, "active": 1, "completed": 1, "blocked": 0}
    assert snapshot["latest_event"]["event_type"] == "turn_completed"
    assert "payload" not in snapshot["latest_event"]
    assert snapshot["elapsed_seconds"] == 60
    assert snapshot["stale"] is True
    assert snapshot["partial"] is False
    assert snapshot["warnings"] == ["HUD_STALE"]


def test_hud_watch_restores_term_int_handlers_and_non_tty_falls_back(monkeypatch, tmp_path):
    installed: dict[int, object] = {}
    restored: list[int] = []

    def fake_getsignal(signum):
        return f"old-{signum}"

    def fake_signal(signum, handler):
        if callable(handler):
            installed[signum] = handler
        else:
            restored.append(signum)

    monkeypatch.setattr(hud_module.signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(hud_module.signal, "signal", fake_signal)

    def stop_on_sleep(_seconds):
        installed[signal.SIGTERM](signal.SIGTERM, None)

    result = watch_hud(
        tmp_path,
        max_iterations=3,
        interval_seconds=0.05,
        sleep=stop_on_sleep,
        is_tty=lambda: False,
    )
    assert result["iterations"] == 1
    assert result["stopped_by"] == "SIGTERM"
    assert result["presentation"] == "plain"
    assert result["signals_restored"] is True
    assert restored == [signal.SIGINT, signal.SIGTERM]


@pytest.mark.parametrize(
    ("interval", "iterations"),
    [(0.001, 1), (61.0, 1), (float("nan"), 1), (0.05, 0), (0.05, 10_001)],
)
def test_hud_watch_rejects_unbounded_inputs(tmp_path, interval, iterations):
    with pytest.raises(ValueError, match="bounds"):
        watch_hud(
            tmp_path,
            interval_seconds=interval,
            max_iterations=iterations,
            sleep=lambda _seconds: None,
        )


def test_lsp_probe_structure():
    data = probe_tools()
    assert "available" in data
    assert "honesty" in data
    assert isinstance(data["available"], list)
