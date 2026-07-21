# tests/test_hooks_common.py
import io
import json
import os
import sys
from pathlib import Path

# hooks/bin is not a package; put it on path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks" / "bin"))
from _common import (  # noqa: E402
    append_event,
    ensure_omg_dirs,
    hook_disabled,
    read_hook_event,
    workspace_root,
)


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
    for sub in (
        "state",
        "state/runs",
        "plans",
        "research",
        "handoffs",
        "artifacts",
        "ultragoal",
        "wiki",
    ):
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


def test_hook_scripts_never_import_set_verified():
    """Product contract: only omg CLI sets verified — hooks must not import it."""
    hooks = Path(__file__).resolve().parents[1] / "hooks" / "bin"
    forbidden = ("set_verified", "run_acceptance", "freeze_and_run")
    for path in hooks.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # allow the word only in comments that say NEVER
        code_lines = [
            ln
            for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        body = "\n".join(code_lines)
        for token in forbidden:
            assert token not in body, f"{path.name} must not reference {token}"


def test_stop_hook_does_not_set_verified(tmp_path, monkeypatch):
    """Behavioral: Stop hook leaves run verified=false."""
    import subprocess

    monkeypatch.setenv("GROK_WORKSPACE_ROOT", str(tmp_path))
    from omg_cli.state import create_run, ensure_omg_dirs, load_run

    ensure_omg_dirs(tmp_path)
    run = create_run(tmp_path, mode="ralph", goal="hook contract", force=True)
    rid = run["run_id"]
    stop = Path(__file__).resolve().parents[1] / "hooks" / "bin" / "stop.py"
    env = {**os.environ, "GROK_WORKSPACE_ROOT": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    proc = subprocess.run(
        [sys.executable, str(stop)],
        input="{}",
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert load_run(tmp_path, rid)["verified"] is False


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


# --- kill switches: DISABLE_OMG / OMG_SKIP_HOOKS ---


def test_hook_disabled_disable_omg_truthy():
    for val in ("1", "true", "TRUE", "yes", "on", "On", " Yes "):
        assert hook_disabled("stop", {"DISABLE_OMG": val}) is True, val
    for val in ("0", "", "false", "no", "off"):
        assert hook_disabled("stop", {"DISABLE_OMG": val}) is False, val


def test_hook_disabled_omg_skip_hooks_list():
    env = {"OMG_SKIP_HOOKS": "stop, pre_tool_use"}
    assert hook_disabled("stop", env) is True
    assert hook_disabled("session_start", env) is False
    assert hook_disabled("pre_tool_use", env) is True
    # case-insensitive + space-separated
    env2 = {"OMG_SKIP_HOOKS": "Stop Pre_Tool_Use"}
    assert hook_disabled("stop", env2) is True
    assert hook_disabled("pre_tool_use", env2) is True


def test_hook_disabled_empty_env():
    assert hook_disabled("stop", {}) is False
    assert hook_disabled("session_start", {}) is False
    assert hook_disabled("subagent_stop", {}) is False
    assert hook_disabled("pre_tool_use", {}) is False


def test_pre_tool_use_deny_disabled_allows(monkeypatch):
    """With DISABLE_OMG=1, deny hook must allow (fail-open kill switch)."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    pre = root / "hooks" / "bin" / "pre_tool_use_deny.py"
    payload = json.dumps(
        {
            "toolName": "run_terminal_command",
            "toolInput": {"command": "claude -p x"},
        }
    )
    env = {
        **os.environ,
        "DISABLE_OMG": "1",
        "PYTHONPATH": str(root),
    }
    env.pop("OMG_ALLOW_EXTERNAL_CLI", None)
    proc = subprocess.run(
        [sys.executable, str(pre)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        cwd=str(root),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["decision"] == "allow"


def test_pre_tool_use_deny_still_denies_without_kill_switch(monkeypatch):
    """Regression: kill switch must not leak into normal deny path."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    pre = root / "hooks" / "bin" / "pre_tool_use_deny.py"
    payload = json.dumps(
        {
            "toolName": "run_terminal_command",
            "toolInput": {"command": "claude -p x"},
        }
    )
    env = {**os.environ, "PYTHONPATH": str(root)}
    env.pop("DISABLE_OMG", None)
    env.pop("OMG_SKIP_HOOKS", None)
    env.pop("OMG_ALLOW_EXTERNAL_CLI", None)
    proc = subprocess.run(
        [sys.executable, str(pre)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        cwd=str(root),
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    out = json.loads(proc.stdout.strip())
    assert out["decision"] == "deny"
