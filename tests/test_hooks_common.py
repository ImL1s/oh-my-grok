# tests/test_hooks_common.py
import io
import json
import sys
from pathlib import Path

# hooks/bin is not a package; put it on path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks" / "bin"))
from _common import append_event, ensure_omg_dirs, read_hook_event, workspace_root  # noqa: E402


def test_ensure_and_append(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_WORKSPACE_ROOT", str(tmp_path))
    root = workspace_root()
    assert root == tmp_path
    ensure_omg_dirs(root)
    assert (root / ".omg" / "state").is_dir()
    append_event(root, {"event": "test", "status": "ok"})
    lines = (root / ".omg" / "state" / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "test"


def test_ensure_omg_dirs_creates_subdirs(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_WORKSPACE_ROOT", str(tmp_path))
    root = ensure_omg_dirs()
    for sub in ("state", "state/runs", "plans", "research", "handoffs", "artifacts", "ultragoal"):
        assert (root / ".omg" / sub).is_dir(), sub


def test_multi_append(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("GROK_SESSION_ID", "sess-multi")
    root = ensure_omg_dirs()
    append_event(root, {"event": "a", "n": 1})
    append_event(root, {"event": "b", "n": 2})
    append_event(root, {"event": "c", "n": 3})
    path = root / ".omg" / "state" / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().strip().splitlines()]
    assert len(rows) == 3
    assert [r["event"] for r in rows] == ["a", "b", "c"]
    assert [r["n"] for r in rows] == [1, 2, 3]
    assert all(r["session_id"] == "sess-multi" for r in rows)
    assert all("ts" in r and r["ts"] for r in rows)


def test_payload_cannot_hijack_ts_or_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("GROK_SESSION_ID", "real-session")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    root = ensure_omg_dirs()
    append_event(
        root,
        {
            "event": "hijack",
            "ts": "1999-01-01T00:00:00+00:00",
            "session_id": "attacker-session",
            "status": "ok",
        },
    )
    row = json.loads((root / ".omg" / "state" / "events.jsonl").read_text().strip())
    assert row["session_id"] == "real-session"
    assert row["ts"] != "1999-01-01T00:00:00+00:00"
    assert row["event"] == "hijack"
    assert row["status"] == "ok"


def test_read_hook_event_valid_json(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"toolName": "Bash", "x": 1}'))
    ev = read_hook_event()
    assert ev == {"toolName": "Bash", "x": 1}


def test_read_hook_event_empty_and_invalid(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert read_hook_event() == {}
    monkeypatch.setattr(sys, "stdin", io.StringIO("   "))
    assert read_hook_event() == {}
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json{"))
    assert read_hook_event() == {}
