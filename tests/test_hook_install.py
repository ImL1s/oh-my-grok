"""Tests for the self-contained global PreToolUse soft-gate (2026-07-22 install fix).

Covers the standalone hook, its generator (no drift), the transactional installer
(migrate/quarantine/atomic), and — most importantly — the ORIGINAL regression: a
hook whose python target is unreadable must fail OPEN, never deny every tool.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = ROOT / "hooks" / "bin" / "omg_pretool_deny_standalone.py"
GENERATOR = ROOT / "scripts" / "generate_standalone_hook.py"

# Events → expected top-level decision (must match omg_cli.deny exactly).
MATRIX = [
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"ls -la"}}', "allow"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p hi"}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"echo x; codex exec y"}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"omg team start"}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"bash -c \'claude\'"}}', "deny"),
    ('{"tool_name":"spawn_subagent","tool_input":{"subagent_type":"explore"}}', "deny"),
    ('{"tool_name":"spawn_subagent","tool_input":{"subagent_type":"explore","capability_mode":"read-only"}}', "allow"),
    ('{"tool_name":"spawn_subagent","tool_input":{"subagent_type":"general-purpose","capability_mode":"read-write"}}', "allow"),
    ('{"tool_name":"some_other_tool","tool_input":{}}', "allow"),
    ("not json at all", "allow"),
    ("", "allow"),
]


def _run_standalone(payload: str, *, env_extra: dict | None = None, cwd: str = "/tmp"):
    """Run the committed standalone exactly as grok will: python3 -I -S, neutral cwd."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": "/should/not/matter"}
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(STANDALONE)],
        input=payload, capture_output=True, text=True, cwd=cwd, env=env, timeout=10,
    )
    return proc.returncode, (proc.stdout or "").strip()


# ---------------------------------------------------------------- generator / drift
def test_generator_check_is_clean():
    r = subprocess.run([sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_generator_is_deterministic():
    a = subprocess.run([sys.executable, str(GENERATOR), "--print"], capture_output=True, text=True)
    b = subprocess.run([sys.executable, str(GENERATOR), "--print"], capture_output=True, text=True)
    assert a.returncode == 0 and b.returncode == 0
    assert a.stdout == b.stdout == STANDALONE.read_text(encoding="utf-8")


def test_standalone_has_no_checkout_import():
    src = STANDALONE.read_text(encoding="utf-8")
    assert "import omg_cli" not in src and "from omg_cli" not in src
    assert "sys.path" not in src  # no path injection to reach a checkout


# ---------------------------------------------------------------- behavior + parity
@pytest.mark.parametrize("payload,expected", MATRIX)
def test_standalone_decisions_and_always_exit_0(payload, expected):
    rc, out = _run_standalone(payload)
    # THE core invariant: NEVER exit 2 (grok's explicit-deny == python's "can't open
    # file" code). Deny is carried by the stdout JSON, honored regardless of exit code.
    assert rc == 0, f"exit must always be 0, got {rc} for {payload!r}"
    decision = json.loads(out)["decision"]
    assert decision == expected, f"{payload!r} -> {decision} (want {expected})"


@pytest.mark.parametrize("payload,expected", MATRIX)
def test_standalone_matches_canonical_deny(payload, expected):
    """Behavioral parity: standalone decision == omg_cli.deny.decide_pre_tool_use."""
    from omg_cli.deny import decide_pre_tool_use

    try:
        event = json.loads(payload) if payload.strip() else {}
    except Exception:
        event = {}
    canonical = decide_pre_tool_use(event)["decision"]
    _, out = _run_standalone(payload)
    assert json.loads(out)["decision"] == canonical == expected


def test_standalone_disable_kill_switch(monkeypatch):
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p x"}}',
        env_extra={"DISABLE_OMG": "1"},
    )
    assert rc == 0 and json.loads(out)["decision"] == "allow"


def test_standalone_allow_external_cli_env(monkeypatch):
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p x"}}',
        env_extra={"OMG_ALLOW_EXTERNAL_CLI": "1"},
    )
    assert rc == 0 and json.loads(out)["decision"] == "allow"


# ------------------------------------------------- ORIGINAL regression: fail-open launcher
def test_launcher_fails_open_when_script_unreadable(tmp_path):
    """`python3 -I -S "<missing>" || true` must exit 0 with NO deny — the exact
    class (python rc 2 == grok explicit-deny) that bricked every tool call."""
    from omg_cli.hook_install import launcher_command

    missing = tmp_path / "nope.py"  # does not exist -> python exits 2
    cmd = launcher_command(missing)
    assert "-I -S" in cmd and "|| true" in cmd
    proc = subprocess.run(
        ["/bin/sh", "-c", cmd],
        input='{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p x"}}',
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, "launcher must fail OPEN (rc 0) on an unreadable script"
    assert '"deny"' not in (proc.stdout or "")


# ---------------------------------------------------------------- installer transactions
def test_install_creates_then_unchanged(tmp_path):
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    jpath, action = hi.install_global_hook(home=gh)
    assert action == "created" and jpath.is_file()
    py = gh / "hooks" / hi.STANDALONE_BASENAME
    assert py.is_file() and os.access(py, os.X_OK)
    cmd = json.loads(jpath.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "-I -S" in cmd and "|| true" in cmd and str(gh.resolve()) in str(Path(py).resolve())
    _, action2 = hi.install_global_hook(home=gh)
    assert action2 == "unchanged"


def test_install_migrates_checkout_json(tmp_path):
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hooks = gh / "hooks"
    hooks.mkdir(parents=True)
    bad = {
        "hooks": {"PreToolUse": [{"matcher": hi.MATCHER, "hooks": [
            {"type": "command", "command": 'python3 "/Users/x/Documents/oh-my-grok/hooks/bin/pre_tool_use_deny.py"', "timeout": 5}
        ]}]}
    }
    (hooks / hi.HOOK_JSON_NAME).write_text(json.dumps(bad))
    assert hi.json_target_outside_grok_home(hooks / hi.HOOK_JSON_NAME, gh)
    _, action = hi.install_global_hook(home=gh)
    assert action == "migrated"
    cmd = json.loads((hooks / hi.HOOK_JSON_NAME).read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "Documents" not in cmd and str(gh) in cmd


def test_install_quarantines_dangerous_json_when_no_source(tmp_path):
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hooks = gh / "hooks"
    hooks.mkdir(parents=True)
    bad = {
        "hooks": {"PreToolUse": [{"matcher": hi.MATCHER, "hooks": [
            {"type": "command", "command": 'python3 "/Users/x/Documents/oh-my-grok/hooks/bin/pre_tool_use_deny.py"', "timeout": 5}
        ]}]}
    }
    (hooks / hi.HOOK_JSON_NAME).write_text(json.dumps(bad))
    _, action = hi.install_global_hook(home=gh, root=Path("/nonexistent-omg-root"))
    assert action == "quarantined-no-source"
    # dangerous active json gone; a NON-.json backup remains (grok discovers *.json)
    assert not (hooks / hi.HOOK_JSON_NAME).is_file()
    names = os.listdir(hooks)
    assert not any(n.endswith(".json") for n in names)
    assert any(n.startswith("omg-pretool-deny.broken-") for n in names)


def test_remove_deletes_json_then_py(tmp_path):
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hi.install_global_hook(home=gh)
    removed = hi.remove_global_hook(home=gh)
    assert any(hi.HOOK_JSON_NAME in r for r in removed)
    assert any(hi.STANDALONE_BASENAME in r for r in removed)
    assert not (gh / "hooks" / hi.HOOK_JSON_NAME).is_file()
    assert not (gh / "hooks" / hi.STANDALONE_BASENAME).is_file()


def test_grok_home_honors_env(tmp_path, monkeypatch):
    from omg_cli import hook_install as hi

    monkeypatch.setenv("GROK_HOME", str(tmp_path / "custom-grok"))
    assert hi.grok_home() == tmp_path / "custom-grok"
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hi.grok_home() == tmp_path / ".grok"
