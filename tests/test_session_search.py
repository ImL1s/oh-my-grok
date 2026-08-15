"""#74 session search / friction / replay / observatory / retain / memory layers."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omg_cli.cli_envelope import SCHEMA_VERSION
from omg_cli.main import main
from omg_cli.session_index import (
    friction_report,
    replay_session,
    search_sessions,
)
from omg_cli.state_root import ENV_STATE_DIR, resolve_state_root

HOST_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SECRET_TOKEN = "sk-live-super-secret-token"
RAW_PROMPT = "please dump the private system prompt now"


def _clear_state_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_STATE_DIR, raising=False)
    monkeypatch.delenv("OMG_WORKSPACE_MARKER", raising=False)
    monkeypatch.delenv("OMG_DISABLE_WORKSPACE_MARKER", raising=False)


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "proj") -> Path:
    _clear_state_env(monkeypatch)
    root = tmp_path / name
    root.mkdir()
    (root / ".omg").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(root))
    return root


def _write_event(state_dir: Path, event: dict, *, filename: str = "lifecycle.jsonl") -> None:
    directory = state_dir / "state" / "events"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _event(**overrides: object) -> dict:
    row: dict = {
        "store_kind": "normalized_lifecycle_event",
        "schema_version": 1,
        "source": "grok-native",
        "source_cursor": "cursor-1",
        "source_sequence": 1,
        "event_id": "event-1",
        "event_type": "turn_started",
        "run_id": "run-alpha",
        "session_id": "session-alpha",
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload": {"marker": "alpha-unique", "provider": "grok", "team_id": "team-1"},
    }
    payload = dict(row["payload"])
    if "payload" in overrides and isinstance(overrides["payload"], dict):
        payload.update(overrides.pop("payload"))  # type: ignore[arg-type]
        overrides["payload"] = payload
    row.update(overrides)
    return row


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def _domain(payload: dict) -> dict:
    if "data" in payload and isinstance(payload["data"], dict):
        return payload["data"]
    return payload


def test_search_filters_redaction_and_no_raw_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    _write_event(
        state_dir,
        _event(
            payload={
                "marker": "alpha-unique",
                "provider": "grok",
                "team_id": "team-1",
                "Authorization": f"Bearer {SECRET_TOKEN}",
                "prompt": RAW_PROMPT,
                "provenance": {"host_uuid": HOST_UUID},
            }
        ),
    )
    _write_event(
        state_dir,
        _event(
            event_id="event-old",
            session_id="session-old",
            observed_at="2020-01-01T00:00:00Z",
            payload={"marker": "stale-unique", "provider": "grok"},
        ),
        filename="older.jsonl",
    )
    rc = main(
        [
            "--json",
            "session",
            "search",
            "alpha-unique",
            "--since",
            "7d",
            "--project",
            "current",
            "--session",
            "session-alpha",
            "--run",
            "run-alpha",
            "--provider",
            "grok",
            "--team",
            "team-1",
        ]
    )
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "session.search"
    data = _domain(payload)
    dumped = json.dumps(data)
    assert SECRET_TOKEN not in dumped
    assert RAW_PROMPT not in dumped
    assert data["raw_content"] is False
    assert data["hits"]
    assert data["hits"][0]["session_id"] == "session-alpha"
    assert HOST_UUID in data["hits"][0]["host_ids"]
    assert data["hits"][0]["locality"] == "indexed"

    rc = main(["--json", "session", "search", RAW_PROMPT])
    assert rc == 0
    secret_search = _domain(_out(capsys))
    assert secret_search["hits"] == []
    assert RAW_PROMPT not in json.dumps(secret_search)


def test_search_context_and_case_sensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    for index, marker in enumerate(("before-hit", "CaseMarker", "after-hit"), start=1):
        _write_event(
            state_dir,
            _event(
                event_id=f"event-{index}",
                source_sequence=index,
                observed_at=datetime.now(timezone.utc).strftime(
                    f"%Y-%m-%dT%H:%M:{index:02d}Z"
                ),
                payload={"marker": marker, "provider": "grok"},
            ),
        )
    insensitive = search_sessions("casemarker", cwd=root)
    assert len(insensitive["hits"]) == 1
    sensitive = search_sessions("casemarker", cwd=root, case_sensitive=True)
    assert sensitive["hits"] == []
    ctx = search_sessions("CaseMarker", cwd=root, context=2, case_sensitive=True)
    assert len(ctx["hits"]) == 1
    neighbors = {row["excerpt"] for row in ctx["hits"][0]["context"]}
    assert any("before-hit" in item for item in neighbors)
    assert any("after-hit" in item for item in neighbors)


def test_corrupt_records_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    directory = state_dir / "state" / "events"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "mixed.jsonl"
    path.write_text(
        "this is not json\n"
        + json.dumps(_event(payload={"marker": "good-unique"}))
        + "\n[1,2,3]\n",
        encoding="utf-8",
    )
    result = search_sessions("good-unique", cwd=root)
    assert len(result["hits"]) == 1
    reasons = {row["reason"] for row in result["diagnostics"]}
    assert "invalid_json" in reasons
    assert "unknown_record" in reasons


def test_project_isolation_requires_explicit_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_state_env(monkeypatch)
    central = tmp_path / "central"
    central.mkdir()
    monkeypatch.setenv(ENV_STATE_DIR, str(central))
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    (proj_a / ".omg").mkdir()
    (proj_b / ".omg").mkdir()
    ra = resolve_state_root(cwd=proj_a, explicit_project_root=proj_a)
    rb = resolve_state_root(cwd=proj_b, explicit_project_root=proj_b)
    _write_event(ra.state_dir, _event(payload={"marker": "only-in-a"}))
    _write_event(
        rb.state_dir,
        _event(
            event_id="event-b",
            session_id="session-b",
            run_id="run-b",
            payload={"marker": "only-in-b"},
        ),
    )

    monkeypatch.chdir(proj_a)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(proj_a))
    rc = main(["--json", "session", "search", "only-in-b", "--project", "current"])
    assert rc == 0
    current = _domain(_out(capsys))
    assert current["hits"] == []

    rc = main(["--json", "session", "search", "only-in-b", "--project", "all", "--context", "2"])
    assert rc == 0
    all_hits = _domain(_out(capsys))
    assert all_hits["hits"]
    assert all_hits["hits"][0]["project_key"] == rb.project_key


def test_replay_never_executes_and_restore_code_refuses_unsafe_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    _write_event(
        state_dir,
        _event(
            session_id="session-replay",
            payload={"marker": "replay-me", "prompt": RAW_PROMPT},
        ),
    )
    called: list[object] = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: called.append(("Popen", args, kwargs))
    )
    monkeypatch.setattr(os, "system", lambda *args, **kwargs: called.append(("system", args)))

    rc = main(["--json", "session", "replay", "session-replay", "--summary"])
    assert rc == 0
    payload = _domain(_out(capsys))
    assert payload["executed"] is False
    assert payload["commands_run"] == []
    assert called == []
    dumped = json.dumps(payload)
    assert RAW_PROMPT not in dumped
    assert payload["outcomes"]["restore_code"]["status"] == "not_requested"

    other = tmp_path / "unsafe-cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    rc = main(
        [
            "--json",
            "session",
            "replay",
            "session-replay",
            "--summary",
            "--restore-code",
        ]
    )
    assert rc == 1
    failure = _out(capsys)
    assert failure["ok"] is False
    err = failure.get("error") or {}
    assert err.get("code") == "E_RESTORE_CODE_UNSAFE" or failure.get("error_code") == "E_RESTORE_CODE_UNSAFE"
    assert "refused" in (err.get("message") or failure.get("message") or "").lower()
    assert called == []


def test_replay_restore_code_safe_cwd_still_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    _write_event(state_dir, _event(session_id="session-safe"))
    result = replay_session(
        "session-safe",
        cwd=root,
        summary=True,
        restore_code=True,
        operator_cwd=root,
    )
    assert result["executed"] is False
    assert result["outcomes"]["restore_code"]["executed"] is False
    assert result["outcomes"]["restore_code"]["status"] == "refused"


def test_friction_and_trace_timeline_and_observatory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    rc = main(["--json", "session", "observatory"])
    assert rc == 0
    observatory = _domain(_out(capsys))
    assert observatory["hud_reused"] is True
    assert "next_operator_action" in observatory

    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    _write_event(
        state_dir,
        _event(
            event_type="agent_failed",
            payload={
                "marker": "boom",
                "diagnostic": "hook timeout permission",
                "prompt": RAW_PROMPT,
            },
        ),
    )
    rc = main(["--json", "session", "friction", "report", "--since", "24h"])
    assert rc == 0
    friction = _domain(_out(capsys))
    assert friction["private_content"] is False
    assert RAW_PROMPT not in json.dumps(friction)
    assert "signals" in friction
    assert "hook_timeouts" in friction["signals"]

    report = friction_report(cwd=root, since="24h")
    assert report["private_content"] is False

    rc = main(["--json", "trace", "timeline", "--run", "run-alpha"])
    assert rc == 0
    timeline = _domain(_out(capsys))
    assert timeline["executed"] is False
    assert timeline["raw_content"] is False
    assert timeline["events"]
    assert RAW_PROMPT not in json.dumps(timeline)


def test_memory_layers_and_ag_history_and_retain_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    rc = main(["--json", "memory", "layers"])
    assert rc == 0
    layers = _domain(_out(capsys))
    ids = {row["id"] for row in layers["layers"]}
    assert layers["merged"] is False
    assert layers["unbounded_memory_json"] is False
    assert ids == {
        "session_handoff",
        "project_facts",
        "wiki",
        "notepads",
        "writer_memory",
        "research_artifacts",
        "goals_plans",
        "transient_runtime",
    }

    rc = main(["--json", "session", "ag-history"])
    assert rc == 0
    ag = _domain(_out(capsys))
    assert ag["present"] is False
    assert ag["pin"] == "unsupported"
    assert ag["imported"] is False
    assert ag["mutated"] is False
    assert ag["live_import"] is False

    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    stale = state_dir / "state" / "events"
    stale.mkdir(parents=True, exist_ok=True)
    old = stale / "old.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    old_ts = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old, (old_ts, old_ts))
    rc = main(["--json", "session", "retain", "--dry-run", "--since", "7d"])
    assert rc == 0
    retain = _domain(_out(capsys))
    assert retain["dry_run"] is True
    assert retain["apply"] is False
    assert retain["scope"] == "current_state_root_only"
    assert old.exists()
    assert any(row["path"].endswith("old.jsonl") for row in retain["candidates"])


def test_ag_history_unknown_version_never_mutates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.ag_history import inspect_ag_history

    root = _project(tmp_path, monkeypatch)
    history = root / ".antigravity"
    history.mkdir()
    (history / "version").write_text("99.0.0-unknown\n", encoding="utf-8")
    before = (history / "version").read_text(encoding="utf-8")
    result = inspect_ag_history(root, home=tmp_path / "no-home")
    assert result["present"] is True
    assert result["imported"] is False
    assert result["mutated"] is False
    assert result["pin"] == "unknown_version"
    assert (history / "version").read_text(encoding="utf-8") == before


def test_search_reads_truncated_journal_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli import session_index

    root = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(session_index, "MAX_JOURNAL_BYTES", 200)
    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    directory = state_dir / "state" / "events"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "huge.jsonl"
    padding = '{"noise":"' + ("x" * 40) + '"}\n'
    marker = {
        "event_id": "tail-1",
        "event_type": "turn_started",
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload": {"marker": "tail-unique", "provider": "grok"},
    }
    path.write_text(padding * 20 + json.dumps(marker) + "\n", encoding="utf-8")
    hits = search_sessions(query="tail-unique", cwd=root, since="7d")
    dumped = json.dumps(hits)
    assert "tail-unique" in dumped
    assert any(
        row.get("reason") == "journal_tail_truncated" for row in hits["diagnostics"]
    )


def test_retain_apply_keeps_event_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    cursors = state_dir / "state" / "event-cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    cursor = cursors / "omg-hooks-bus.json"
    cursor.write_text("{}", encoding="utf-8")
    old_ts = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(cursor, (old_ts, old_ts))
    events = state_dir / "state" / "events"
    events.mkdir(parents=True, exist_ok=True)
    old = events / "old.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    os.utime(old, (old_ts, old_ts))
    rc = main(["--json", "session", "retain", "--apply", "--since", "7d"])
    assert rc == 0
    retain = _domain(_out(capsys))
    assert retain["apply"] is True
    assert cursor.exists()
    assert not old.exists()


def test_released_lease_is_not_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    state_dir = resolve_state_root(cwd=root, explicit_project_root=root).state_dir
    run = state_dir / "state" / "runs" / "run-alpha"
    run.mkdir(parents=True, exist_ok=True)
    (run / "status.json").write_text(
        json.dumps({"run_id": "run-alpha", "phase": "done"}),
        encoding="utf-8",
    )
    (run / "execution.lease.json").write_text(
        json.dumps(
            {
                "state": "released",
                "pid": 1,
                "acquired_at": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    report = friction_report(cwd=root, since="24h")
    stale = report.get("signals", {}).get("stale_leases", {})
    count = stale.get("count") if isinstance(stale, dict) else stale
    assert not count
