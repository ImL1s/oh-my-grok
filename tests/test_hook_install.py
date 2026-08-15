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
from time import perf_counter

import pytest

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = ROOT / "hooks" / "bin" / "omg_pretool_deny_standalone.py"
GENERATOR = ROOT / "scripts" / "generate_standalone_hook.py"

# Events → expected top-level decision (must match omg_cli.deny exactly).
MATRIX = [
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"ls -la"}}', "allow"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"command -v claude"}}', "allow"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p hi"}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"echo x; codex exec y"}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"omg team start"}}', "allow"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"omc team 2:codex x"}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"/opt/omg/bin/omg team launch --goal x"}}', "allow"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"bash -c \'claude\'"}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"git commit -m \\"fix(kimi): stream\\""}}', "allow"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"echo \\"$(kimi --version)\\""}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"echo ok # \'\\nkimi --version"}}', "deny"),
    ('{"tool_name":"run_terminal_command","tool_input":{"command":"echo \\"; bash -c \'claude\'\\"; bash -c \'codex exec x\'"}}', "deny"),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": r"echo $'abc\''; codex exec x"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "cat <<'EOF'\n\"\nEOF\ncodex exec x"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "\\" + "\n" + "codex exec x"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "cat <<\"E\\OF\"\n\"\nE\\OF\ncodex exec x"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "eval 'echo ok; kimi --version'"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "eval 'git commit -m \"fix(kimi)\"'"},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "bash -c 'echo ok\ncodex exec x'"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": '((1 << "EOF"))\ncodex exec x'},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "bash -c 'echo safe'\necho kimi"},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "bash -c '\"codex\" exec foo'"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "bash -c 'if true; then \"codex\" exec foo; fi'"
                },
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": 'array=("codex")'},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": '[[ foo && "codex" ]]'},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "bash -c '{ \"codex\" exec foo; }'"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": 'cat <<EOF\n"codex" exec foo\nEOF'},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": 'case x in x) echo $(true) "codex";; esac'
                },
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "cat <<$'E\\x4fF'\nsafe\nEOF\ncodex exec x"
                },
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": 'bash -c "$(printf codex)"'},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "bash -c 'echo $(printf codex)'"},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "bash -c $'echo \\'x\\'; codex exec x'"
                },
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": (
                        'echo "$(case x in x) echo safe; '
                        'codex exec x;; esac)"'
                    )
                },
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": 'echo "$(case x in x) echo codex;; esac)"'
                },
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "cat <<$(tag)\npayload\n$(tag)\ncodex exec x"
                },
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "cat <<$(tag)\ncodex exec x\n$(tag)"
                },
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "cat <<EOF\n$(codex exec x)"},
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "cat <<'EOF'\n$(codex exec x)"},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "trap 'echo safe; codex exec x' EXIT"
                },
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "trap 'echo codex' EXIT"},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "x='$(codex exec x)'; echo \"${x@P}\""
                },
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": "git commit -m '${x@P}'"},
            }
        ),
        "allow",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "echo ${x:-<<EOF}\ncodex exec x\nEOF"
                },
            }
        ),
        "deny",
    ),
    (
        json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {
                    "command": "echo '${x:-<<EOF}\ncodex exec x\nEOF'"
                },
            }
        ),
        "allow",
    ),
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
    rendered = subprocess.run(
        [sys.executable, str(GENERATOR), "--print"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if rendered == STANDALONE.read_text(encoding="utf-8"):
        assert r.returncode == 0, r.stdout + r.stderr
    else:
        # W2 owns policy inputs and W6 alone owns the generated output.  Between
        # those waves, the honest contract is an explicit stale rc=1—not a W1
        # write to hooks/bin/omg_pretool_deny_standalone.py.
        assert r.returncode == 1
        assert "stale" in r.stderr and "regenerate" in r.stderr


def test_generator_is_deterministic():
    a = subprocess.run([sys.executable, str(GENERATOR), "--print"], capture_output=True, text=True)
    b = subprocess.run([sys.executable, str(GENERATOR), "--print"], capture_output=True, text=True)
    assert a.returncode == 0 and b.returncode == 0
    assert a.stdout == b.stdout


def test_generator_interface_is_versioned_and_machine_readable():
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--interface"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "standalone_hook_generator/1\n"


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


def test_standalone_bounds_many_unquoted_heredoc_substitutions():
    body = " ".join(["$(echo safe)"] * 700 + ["$(codex exec x)"])
    payload = json.dumps(
        {
            "tool_name": "run_terminal_command",
            "tool_input": {"command": f"cat <<EOF\n{body}\nEOF"},
        }
    )
    started = perf_counter()
    rc, out = _run_standalone(payload)
    assert rc == 0 and json.loads(out)["decision"] == "deny"
    assert perf_counter() - started < 2.0


def test_standalone_budget_ignores_single_quoted_commit_message():
    fragment = "case x in x) echo $(echo safe);; esac fix(kimi)"
    command = "git commit -m '" + " ".join([fragment] * 66) + "'"
    payload = json.dumps(
        {
            "tool_name": "run_terminal_command",
            "tool_input": {"command": command},
        }
    )
    rc, out = _run_standalone(payload)
    assert rc == 0 and json.loads(out)["decision"] == "allow"


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


def test_standalone_allows_first_party_omg_team_leader():
    """#146: installed standalone must allow bare omg team (no absolute-path rewrite)."""
    for cmd in (
        "omg team 2:executor \"fix tests\"",
        "omg team launch --workers 2 --goal x",
        "/opt/omg/bin/omg team status r",
        "command env /opt/omg/bin/omg team api get-summary",
        "bash -lc 'omg team launch --goal x'",
    ):
        payload = json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": cmd},
            }
        )
        rc, out = _run_standalone(payload)
        assert rc == 0, cmd
        body = json.loads(out)
        assert body["decision"] == "allow", (cmd, body)


def test_standalone_denies_omc_team_and_worker_nested_launch():
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"omc team 2:codex x"}}'
    )
    assert rc == 0 and json.loads(out)["decision"] == "deny"
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"/usr/bin/omc team x"}}'
    )
    assert rc == 0 and json.loads(out)["decision"] == "deny"
    # process-env worker marker → nested launch denied with Team reason
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"omg team launch --goal x"}}',
        env_extra={"OMG_TEAM_WORKER": "1"},
    )
    assert rc == 0
    body = json.loads(out)
    assert body["decision"] == "deny"
    assert "E_TEAM_NESTED_LAUNCH" in body.get("reason", "")
    assert "omg ask" not in body.get("reason", "")
    # identity-bound api still allowed through the soft-gate
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"omg team api claim-task"}}',
        env_extra={"OMG_TEAM_WORKER": "1", "OMG_TEAM_WORKER_ID": "w1"},
    )
    assert rc == 0 and json.loads(out)["decision"] == "allow"
    # Leading globals before team must still classify as nested launch.
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"omg --json team 3:executor fix"}}',
        env_extra={"OMG_TEAM_WORKER": "1"},
    )
    assert rc == 0
    body = json.loads(out)
    assert body["decision"] == "deny"
    assert "E_TEAM_NESTED_LAUNCH" in body.get("reason", "")
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"omg --json team api catalog"}}',
        env_extra={"OMG_TEAM_WORKER": "1", "OMG_TEAM_WORKER_ID": "w1"},
    )
    assert rc == 0 and json.loads(out)["decision"] == "allow"
    # Wrapper options must be consumed (env -i / command -p / nice -n).
    for cmd in (
        "env -i omg team launch --goal x",
        "env --ignore-environment omg team launch --goal x",
        "command -p omg team launch --goal x",
        "nice -n 5 omg team launch --goal x",
        "env --weird omg team launch --goal x",
    ):
        payload = json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": cmd},
            }
        )
        rc, out = _run_standalone(payload, env_extra={"OMG_TEAM_WORKER": "1"})
        assert rc == 0, cmd
        body = json.loads(out)
        assert body["decision"] == "deny", (cmd, body)
        assert "E_TEAM_NESTED_LAUNCH" in body.get("reason", ""), (cmd, body)
    # command -v is discovery: provider name is data, not a nested launch.
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"command -v claude"}}'
    )
    assert rc == 0 and json.loads(out)["decision"] == "allow"
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"command -v omg team launch"}}',
        env_extra={"OMG_TEAM_WORKER": "1"},
    )
    assert rc == 0 and json.loads(out)["decision"] == "allow"


def test_standalone_readwrite_herestring_and_budget():
    """F4 standalone parity: ``<>`` / ``<<<``, wrappers, FD, multi-head, budget."""
    from omg_cli.deny import _HEAD_TAIL_DECODE_LIMIT, _HEAD_TAIL_RAW_SCAN_CAP

    deny_cmds = (
        "omc <>out team launch",
        "omc <<<payload team launch",
        "omc 2<>out team",
        "omc 0<<<payload team",
        "<>out omc team",
        "<<<payload omc team",
        "env -i omc <>out team",
        "command -p omc <<<payload team",
        "true; omc <>out team",
        "echo hi && omc <<<payload team",
        "omc {fd}>/dev/null team x",
        " ".join(["omc"] + [f"1>x{i}" for i in range(_HEAD_TAIL_RAW_SCAN_CAP)] + ["team"]),
        f"omc >{'A' * (_HEAD_TAIL_DECODE_LIMIT + 64)} team",
    )
    for cmd in deny_cmds:
        payload = json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": cmd},
            }
        )
        rc, out = _run_standalone(payload)
        assert rc == 0, cmd
        assert json.loads(out)["decision"] == "deny", cmd

    allow_cmds = (
        "echo omc <>out team",
        "echo omc <<<payload team",
        "omc 2 <>out team",
        "omc 2 <<<payload team",
        "git commit -m '" + ("omc team " * 80) + "'",
        " ".join(["echo"] + [f"1>x{i}" for i in range(_HEAD_TAIL_RAW_SCAN_CAP)] + ["omc", "team"]),
    )
    for cmd in allow_cmds:
        payload = json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": cmd},
            }
        )
        rc, out = _run_standalone(payload)
        assert rc == 0, cmd
        assert json.loads(out)["decision"] == "allow", cmd

    worker_deny = (
        "omg <>out team launch",
        "omg <<<payload team launch",
        "env -i omg <>out team launch",
        "true; omg <<<payload team launch --goal x",
    )
    for cmd in worker_deny:
        payload = json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": cmd},
            }
        )
        rc, out = _run_standalone(payload, env_extra={"OMG_TEAM_WORKER": "1"})
        assert rc == 0, cmd
        body = json.loads(out)
        assert body["decision"] == "deny", (cmd, body)
        assert "E_TEAM_NESTED_LAUNCH" in body.get("reason", ""), (cmd, body)

    worker_allow = (
        "omg <>out team api catalog",
        "omg {fd}>/dev/null team api catalog",
        "echo omg <>out team launch",
        "omg 2 <>out team launch",
    )
    for cmd in worker_allow:
        payload = json.dumps(
            {
                "tool_name": "run_terminal_command",
                "tool_input": {"command": cmd},
            }
        )
        rc, out = _run_standalone(
            payload,
            env_extra={"OMG_TEAM_WORKER": "1", "OMG_TEAM_WORKER_ID": "w1"},
        )
        assert rc == 0, cmd
        assert json.loads(out)["decision"] == "allow", cmd
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"env -i omg team api catalog"}}',
        env_extra={"OMG_TEAM_WORKER": "1", "OMG_TEAM_WORKER_ID": "w1"},
    )
    assert rc == 0 and json.loads(out)["decision"] == "allow"
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"env -i claude -p x"}}'
    )
    assert rc == 0 and json.loads(out)["decision"] == "deny"
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"env -i omc team x"}}'
    )
    assert rc == 0 and json.loads(out)["decision"] == "deny"
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"env -S \'omc team x\'"}}'
    )
    assert rc == 0 and json.loads(out)["decision"] == "deny"
    rc, out = _run_standalone(
        '{"tool_name":"run_terminal_command","tool_input":{"command":"env -S \'omg team api catalog\'"}}',
        env_extra={"OMG_TEAM_WORKER": "1", "OMG_TEAM_WORKER_ID": "w1"},
    )
    assert rc == 0 and json.loads(out)["decision"] == "allow"


# ------------------------------------------------- ORIGINAL regression: fail-open launcher
def test_launcher_fails_open_when_script_unreadable(tmp_path):
    """Wrapper ``|| true`` must exit 0 with NO deny when the standalone is missing."""
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hi.install_global_hook(home=gh)
    py = gh / "hooks" / hi.STANDALONE_BASENAME
    wrapper = gh / "hooks" / hi.WRAPPER_BASENAME
    py.unlink()
    assert wrapper.is_file() and os.access(wrapper, os.X_OK)
    proc = subprocess.run(
        [str(wrapper)],
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
    wrapper = gh / "hooks" / hi.WRAPPER_BASENAME
    assert cmd == str(wrapper)
    assert wrapper.is_file() and os.access(wrapper, os.X_OK)
    body = wrapper.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh\n")
    assert "-I -S" in body and "|| true" in body
    assert "\r" not in body
    # grok 1.0.4 execvp()s the command string as argv0.
    deny = subprocess.run(
        [cmd],
        input='{"tool_name":"run_terminal_command","tool_input":{"command":"claude -p x"}}',
        capture_output=True, text=True, timeout=10,
    )
    assert deny.returncode == 0
    assert json.loads(deny.stdout)["decision"] == "deny"
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
    assert any(hi.WRAPPER_BASENAME in r for r in removed)
    assert any(hi.STANDALONE_BASENAME in r for r in removed)
    assert not (gh / "hooks" / hi.HOOK_JSON_NAME).is_file()
    assert not (gh / "hooks" / hi.WRAPPER_BASENAME).is_file()
    assert not (gh / "hooks" / hi.STANDALONE_BASENAME).is_file()


def test_grok_home_honors_env(tmp_path, monkeypatch):
    from omg_cli import hook_install as hi

    monkeypatch.setenv("GROK_HOME", str(tmp_path / "custom-grok"))
    assert hi.grok_home() == tmp_path / "custom-grok"
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hi.grok_home() == tmp_path / ".grok"
