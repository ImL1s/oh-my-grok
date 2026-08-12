import json
import os
import subprocess
import sys
from pathlib import Path
from time import perf_counter

import pytest
from omg_cli.deny import (
    decide_pre_tool_use,
    decide_spawn_subagent,
    should_deny_command,
)

ROOT = Path(__file__).resolve().parents[1]
PRE_TOOL = ROOT / "hooks" / "bin" / "pre_tool_use_deny.py"


@pytest.mark.parametrize("cmd", [
    "claude -p 'hi'",
    "codex exec foo",
    "/usr/local/bin/claude",
    "env claude -p x",
    "OMG_ALLOW_EXTERNAL_CLI=1 claude -p x",  # must STILL deny if process env unset
    "sh -c 'claude -p x'",
    "command codex exec",
    "xargs claude",
    # foreign orchestration (omc team) — bare + path + wrappers + shell-c
    "omc team 2:codex 'x'",
    "/usr/bin/omc team 2:codex 'x'",
    "command omc team x",
    "env omc team x",
    "command /usr/bin/omc team x",
    "env /opt/bin/omc team x",
    "nohup omc team x",
    "exec omc team x",
    "bash -lc 'omc team x'",
    "agy -p x",
    "cursor-agent -p x",
    # -lc / login-command forms
    "bash -lc 'claude -p x'",
    "zsh -lc \"codex exec foo\"",
    "bash -cl 'claude -p x'",
    # unquoted sh -c
    "sh -c claude",
    "bash -c claude -p x",
    "zsh -c /usr/bin/codex exec",
    # common wrappers
    "nohup claude -p x",
    "nice claude -p x",
    "sudo claude -p x",
    "time claude -p x",
    "nohup nice claude -p x",
    "sudo /usr/local/bin/codex exec",
    "env nohup claude -p x",
    # path-prefixed env / shell / eval
    "/usr/bin/env claude -p x",
    "/bin/bash -c 'claude -p x'",
    "/bin/bash -lc \"codex exec foo\"",
    "eval claude -p x",
    "exec claude -p x",
    "/usr/bin/env bash -c 'claude -p x'",
])
def test_deny_external_cli(cmd):
    assert should_deny_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "grok -p 'hi'",
    "pytest tests/",
    "python3 scripts/foo.py",
    "git status",
    "echo claude is a word",  # word not as executable head
    "echo bash -c claude",  # narrative mention after echo
    "ls -lc /tmp",  # -lc is not shell login-command for ls
    "nice pytest tests/",
    "nohup sleep 1",
    "sudo apt update",
    "time make",
    # omg subcommands (including first-party Team) must stay allowed
    "omg doctor",
    "omg accept --run x",
    "omg setup",
    "omg worker seal",
    "omg team start --goal x",
    "omg team 2:executor \"fix tests\"",
    "omg team launch --workers 2 --role executor --goal \"fix tests\"",
    "omg team status run-1",
    "omg team stop run-1",
    "omg team api get-summary",
    "/opt/omg/bin/omg team 2:executor \"x\"",
    "./venv/bin/omg team launch --workers 1 --goal x",
    "command /opt/omg/bin/omg team status r",
    "env /opt/omg/bin/omg team api get-summary",
    "bash -lc '/opt/omg/bin/omg team launch --workers 1 --goal x'",
    # command-text env must NOT grant/deny Team (process env owns worker gate)
    "OMG_TEAM_WORKER=0 omg team start --goal x",
    "OMG_TEAM_WORKER=1 omg team api claim-task",
    # quoted/arg mention of omg team (mirror: echo "run omc team" is not denied)
    'echo "run omg team"',
])
def test_allow_benign(cmd):
    assert should_deny_command(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "git commit -m 'fix(kimi): stream large histories'",
        'git commit -m "fix(kimi): stream large histories"',
        "git commit -m 'docs: mention claude; kimi; codex'",
        'git commit -m "fix: mention (kimi) and omg team"',
        'git commit -m "fix:\\nkimi remains supported"',
        "echo '$(kimi --version)'",
        'echo "\\$(kimi --version)"',
        'echo "(bash -c \'kimi --version\')"',
    ],
)
def test_allow_denied_cli_names_in_literal_shell_arguments(cmd):
    assert should_deny_command(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "(kimi --version)",
        'echo "$(kimi --version)"',
        'echo "$(echo ok; kimi --version)"',
        'git commit -m "test $(kimi --version)"',
    ],
)
def test_deny_cli_execution_from_shell_substitutions(cmd):
    assert should_deny_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "echo ok # '\nkimi --version",
        'echo ok # "\ncodex exec x',
        "echo \"; bash -c 'claude'\"; bash -c 'codex exec x'",
        r"echo $'abc\''; codex exec x",
        "cat <<'EOF'\n\"\nEOF\ncodex exec x",
        "cat <<EOF\n\"\nEOF\ncodex exec x",
        "cat <<$'EOF'\n\"\nEOF\ncodex exec x",
        "cat <<<\"'\"\ncodex exec x",
        "cat <<EOF\n$(codex exec x)\nEOF",
        "cat <<EOF\n$(codex exec x)",
        "cat <<EOF\n`codex exec x`",
        "\\" + "\n" + "codex exec x",
        "echo ok; " + "\\" + "\n" + "codex exec x",
        "env " + "\\" + "\n" + "codex exec x",
        "bash -c " + "\\" + "\n" + "'codex exec x'",
        "\\" + "\n" + "\\" + "\n" + "codex exec x",
        "co" + "\\" + "\n" + "dex exec x",
        "ba" + "\\" + "\n" + "sh -c 'codex exec x'",
        "echo ok # " + "\\" + "\n" + "codex exec x",
        "cat <<\"E\\OF\"\n\"\nE\\OF\ncodex exec x",
        "eval 'echo ok; kimi --version'",
        "eval '$(kimi --version)'",
        "eval 'echo ok && codex exec x'",
        'eval "echo ok; kimi --version"',
        "eval echo ok \\; kimi --version",
        "eval $'echo ok; kimi --version'",
        "eval 'eval \"kimi --version\"'",
        "trap 'echo safe; codex exec x' EXIT",
        "trap -- $'echo safe; codex exec x' EXIT",
        "echo \"$(eval 'echo ok; kimi --version')\"",
        "bash -c 'echo ok\ncodex exec x'",
        "cat <<EOF\n$(co" + "\\" + "\n" + "dex exec x)\nEOF",
        "cat <<$(tag)\npayload\n$(tag)\ncodex exec x",
        '((1 << "EOF"))\ncodex exec x',
        "echo $((1 + $(kimi --version)))",
        "bash -c '\"codex\" exec foo'",
        "bash -c 'command \"codex\" exec foo'",
        "bash -c 'co\"dex\" exec foo'",
        '"codex" exec foo',
        'command "codex" exec foo',
        "bash -c 'if true; then codex exec x; fi'",
        "bash -c 'for x in one; do \"codex\" exec x; done'",
        "bash -c 'case x in x) co\"dex\" exec x;; esac'",
        "bash -c '{ codex exec x; }'",
        "bash -c '{ command \"codex\" exec x; }'",
        "bash -c 'f() { \"codex\" exec x; }; f'",
        "bash -c 'function f { codex exec x; }; f'",
        "bash -c 'coproc codex exec x'",
        "case y in x) echo safe;; y) co\"dex\" exec x;; esac",
        'case o in o) case i in i) "codex" exec x;; esac;; esac',
        'array=($("codex" exec x))',
        '[[ x == $("codex" exec x) ]]',
        "cat <<EOF\n'$(codex exec x)'\nEOF",
        "cat <<EOF\n$(case x in x) co\"dex\" exec x;; esac)\nEOF",
        "cat <<$'E\\x4fF'\nsafe\nEOF\ncodex exec x",
        "cat <<$'E\\117F'\nsafe\nEOF\ncodex exec x",
        'bash -c "$(printf codex)"',
        "bash -c '$(printf codex)'",
        "bash -c 'if $(printf codex); then :; fi'",
        "bash -c 'co$(printf dex) exec x'",
        'eval "$(printf codex)"',
        "eval '$(printf codex)'",
        "bash -c 'case x in x) $(printf codex);; esac'",
        'echo "$(case x in x) echo safe; codex exec x;; esac)"',
        'echo "$(case x in a) echo safe;; x) codex exec x;; esac)"',
        "bash -c $'echo \\'x\\'; codex exec x'",
        "bash -c $'co\\x64ex exec x'",
        "eval $'echo \\'x\\'; codex exec x'",
        "x='$(codex exec x)'; echo \"${x@P}\"",
        "cat <<EOF\n${x@P}\nEOF",
        "echo ${x:-<<EOF}\ncodex exec x\nEOF",
        "echo ${x:-$(codex exec x)}",
    ],
)
def test_deny_cli_after_inert_shell_text(cmd):
    assert should_deny_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "echo ok # ' kimi --version",
        'echo ok # " codex exec x',
        r"echo $'literal; codex exec x'",
        "cat <<'EOF'\ncodex exec x\nEOF",
        "cat <<'EOF'\n$(codex exec x)\nEOF",
        "cat <<'EOF'\n$(codex exec x)",
        "cat <<EOF\ncodex exec x",
        "echo foo" + "\\" + "\n" + "codex exec x",
        "echo " + "\\" + "\n" + "codex exec x",
        "echo '" + "\\" + "\n" + "codex exec x'",
        "echo \"foo" + "\\" + "\n" + "codex exec x\"",
        "cat <<'EOF'\nE\\\nOF\ncodex exec x\nEOF",
        "cat <<'EOF'\n$(co\\\ndex exec x)\nEOF",
        "eval 'echo kimi is configured'",
        "eval 'git commit -m \"fix(kimi)\"'",
        "trap 'echo codex' EXIT",
        "trap -p EXIT",
        "echo \"trap 'codex exec x' EXIT\"",
        "eval 'echo ok'; echo '(kimi)'",
        "echo \"eval 'kimi --version'\"",
        "bash -c 'echo safe'\necho kimi",
        "bash -c 'echo codex'",
        "((kimi << 1))",
        "echo $((kimi << 1))",
        'array=("codex")',
        "array=(codex)",
        'items=(co"dex")',
        '[[ foo && "codex" ]]',
        "[[ foo && codex ]]",
        "bash -c 'array=(\"codex\")'",
        "bash -c '[[ foo && \"codex\" ]]'",
        'echo $(true) "codex"',
        "case x in codex) echo safe;; esac",
        'case x in x) echo $(true) "codex";; esac',
        'echo "$(case x in x) echo codex;; esac)"',
        "git commit -m '${x@P}'",
        "cat <<'EOF'\n${x@P}\nEOF",
        "echo '${x:-<<EOF}\ncodex exec x\nEOF'",
        "echo ${x:-<<EOF}",
        'cat <<EOF\n"codex" exec x\nEOF',
        'cat <<EOF\nco"dex" exec x\nEOF',
        'cat <<EOF\n$(case x in x) echo "codex";; esac)\nEOF',
        "cat <<$(tag)\ncodex exec x\n$(tag)",
        "cat <<$'E\\x4fF'\n\"codex\" exec x\nEOF",
        "bash -c 'echo $(printf codex)'",
        'bash -c "echo $(printf codex)"',
        "eval 'echo $(printf codex)'",
        "bash -c $'echo \\'codex\\''",
    ],
)
def test_allow_denied_cli_names_inside_inert_shell_text(cmd):
    assert should_deny_command(cmd) is False


def test_many_literal_cli_mentions_do_not_stall_hook():
    command = "git commit -m '" + " ".join(["(kimi)"] * 4000) + "'"
    started = perf_counter()
    assert should_deny_command(command) is False
    assert perf_counter() - started < 2.0


def test_many_case_branches_do_not_stall_hook():
    command = "case x in " + " ".join(
        f"p{index}) echo safe;;" for index in range(2000)
    ) + " esac"
    started = perf_counter()
    assert should_deny_command(command) is False
    assert perf_counter() - started < 2.0


def test_many_command_substitutions_do_not_stall_hook():
    command = "echo " + " ".join("$(echo safe)" for _ in range(2000))
    started = perf_counter()
    assert should_deny_command(command) is False
    assert perf_counter() - started < 2.0


def test_deep_case_substitutions_fail_closed_before_hook_timeout():
    command = "codex exec x"
    for _ in range(150):
        command = f'echo "$(case x in x) {command};; esac)"'
    started = perf_counter()
    assert should_deny_command(command) is True
    assert perf_counter() - started < 2.0


def test_many_unquoted_heredoc_substitutions_fail_closed_before_timeout():
    body = " ".join(["$(echo safe)"] * 700 + ["$(codex exec x)"])
    command = f"cat <<EOF\n{body}\nEOF"
    started = perf_counter()
    assert should_deny_command(command) is True
    assert perf_counter() - started < 2.0


def test_case_budget_ignores_single_quoted_commit_message():
    fragment = "case x in x) echo $(echo safe);; esac fix(kimi)"
    command = "git commit -m '" + " ".join([fragment] * 66) + "'"
    assert should_deny_command(command) is False


def test_case_budget_ignores_quoted_heredoc_body():
    fragment = "case x in x) echo $(echo safe);; esac codex"
    command = "cat <<'EOF'\n" + "\n".join([fragment] * 65) + "\nEOF"
    assert should_deny_command(command) is False


def test_process_env_allow_only_when_set(monkeypatch):
    monkeypatch.delenv("OMG_ALLOW_EXTERNAL_CLI", raising=False)
    d = decide_pre_tool_use({"toolName": "run_terminal_command", "toolInput": {"command": "claude -p x"}})
    assert d["decision"] == "deny"
    monkeypatch.setenv("OMG_ALLOW_EXTERNAL_CLI", "1")
    d = decide_pre_tool_use({"toolName": "run_terminal_command", "toolInput": {"command": "claude -p x"}})
    assert d["decision"] == "allow"


def test_spawn_missing_capability_mode_denied():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "general-purpose",
                "prompt": "do work",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "capability_mode" in reason
    # Model must be told to retry, not abandon multi-agent
    assert "RETRY IMMEDIATELY" in reason
    assert "read-write" in reason
    assert "Do NOT abandon multi-agent" in reason


def test_spawn_missing_mode_explore_suggests_read_only():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "prompt": "map repo",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "RETRY IMMEDIATELY" in reason
    assert "capability_mode='read-only'" in reason or 'capability_mode="read-only"' in reason


def test_spawn_executor_read_write_allowed():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "omg-executor",
                "capability_mode": "read-write",
                "prompt": "implement",
            },
        }
    )
    assert d["decision"] == "allow"


def test_spawn_general_purpose_requires_read_write():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "general-purpose",
                "capability_mode": "read-only",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "read-write" in reason
    assert "RETRY IMMEDIATELY" in reason


def test_spawn_explore_requires_read_only():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "capability_mode": "read-write",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    assert "RETRY IMMEDIATELY" in d.get("reason", "")
    d2 = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "capability_mode": "read-only",
                "prompt": "x",
            },
        }
    )
    assert d2["decision"] == "allow"


def test_spawn_task_alias_and_camel_case_keys():
    d = decide_pre_tool_use(
        {
            "tool_name": "Task",
            "tool_input": {
                "subagentType": "omg-critic",
                "capabilityMode": "read-only",
                "prompt": "review",
            },
        }
    )
    assert d["decision"] == "allow"


def test_spawn_execute_mode_denied():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "general-purpose",
                "capability_mode": "execute",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "RETRY IMMEDIATELY" in reason
    assert "read-write" in reason


def test_spawn_all_mode_denied_with_retry():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "capability_mode": "all",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    reason = d.get("reason", "")
    assert "RETRY IMMEDIATELY" in reason
    assert "read-only" in reason


def test_spawn_empty_type_missing_mode_denied():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {"prompt": "x"},
        }
    )
    assert d["decision"] == "deny"
    assert "RETRY IMMEDIATELY" in d.get("reason", "")


def test_spawn_invalid_mode_denied():
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "capability_mode": "write-only",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "deny"
    assert "RETRY IMMEDIATELY" in d.get("reason", "")
    assert "read-only" in d.get("reason", "")


def test_spawn_unsafe_env_allows_missing_mode(monkeypatch):
    monkeypatch.setenv("OMG_ALLOW_UNSAFE_SPAWN", "1")
    d = decide_pre_tool_use(
        {
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "explore",
                "prompt": "x",
            },
        }
    )
    assert d["decision"] == "allow"
    assert "OMG_ALLOW_UNSAFE_SPAWN" in d.get("reason", "")


def _run_pre_tool(event: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    run_env["PYTHONPATH"] = str(ROOT) + os.pathsep + run_env.get("PYTHONPATH", "")
    # Ensure allow-env is controlled by caller
    run_env.pop("OMG_ALLOW_EXTERNAL_CLI", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(PRE_TOOL)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=run_env,
        cwd=str(ROOT),
        check=False,
    )


def test_pre_tool_use_deny_exit_codes_cmd_string_env_still_deny():
    """OMG_ALLOW_EXTERNAL_CLI=1 inside command string must NOT allow; process env only."""
    event = {
        "toolName": "run_terminal_command",
        "toolInput": {"command": "OMG_ALLOW_EXTERNAL_CLI=1 claude -p x"},
    }
    proc = _run_pre_tool(event)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["decision"] == "deny"


def test_pre_tool_use_allow_exit_zero():
    event = {
        "toolName": "run_terminal_command",
        "toolInput": {"command": "git status"},
    }
    proc = _run_pre_tool(event)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["decision"] == "allow"


def test_executor_nested_spawn_tools_denied():
    d = decide_spawn_subagent(
        {
            "subagent_type": "omg-executor",
            "capability_mode": "read-write",
            "tools": ["read_file", "spawn_subagent"],
        }
    )
    assert d["decision"] == "deny"
    assert "depth" in d["reason"].lower() or "spawn" in d["reason"].lower()


def test_depth_gt_1_denied():
    d = decide_spawn_subagent(
        {
            "subagent_type": "explore",
            "capability_mode": "read-only",
            "depth": 2,
        }
    )
    assert d["decision"] == "deny"


def test_multiline_command_deny_not_bypassed_by_newline():
    """A denied bin on its own line (newline, not ';') must still be denied.

    Multi-line shell scripts (heredocs, setup + run) are the common shape of
    real Bash/run_terminal_command payloads; the deny must not require a shell
    operator on the same line as the binary.
    """
    assert should_deny_command("echo start\nclaude -p 'hi'") is True
    assert should_deny_command("cd /tmp\ncodex exec foo") is True
    assert should_deny_command("set -e\n\n  agy -p go") is True
    assert should_deny_command("echo a\r\ncursor-agent --print x") is True
    # first-party omg team allowed; foreign omc team still denied on its own line
    assert should_deny_command("echo hi\nomg team start --goal x") is False
    assert should_deny_command("echo hi\nomc team 2:codex 'x'") is True
    # sanity: a plain multi-line script with no denied bin is still allowed
    assert should_deny_command("echo start\necho done") is False
    # the denied word as a mere argument mid-line stays allowed
    assert should_deny_command("echo run claude later\ntrue") is False
    # end-to-end through the PreToolUse decision
    ev = {
        "toolName": "run_terminal_command",
        "toolInput": {"command": "echo x\nagy -p go"},
    }
    assert decide_pre_tool_use(ev)["decision"] == "deny"


def test_omg_team_first_party_allowed_omc_team_denied():
    """#146: first-party omg team is not an external CLI; omc team still is."""
    assert should_deny_command("omg team start --goal x") is False
    assert should_deny_command("omg team") is False
    assert should_deny_command("echo hi\nomg team start --goal x") is False
    assert should_deny_command("/opt/omg/bin/omg team launch --goal x") is False
    # foreign orchestration remains denied across equivalent heads
    assert should_deny_command("omc team 2:codex 'x'") is True
    assert should_deny_command("/usr/bin/omc team x") is True
    assert should_deny_command("command omc team x") is True
    assert should_deny_command("env /opt/bin/omc team x") is True
    assert should_deny_command("bash -lc 'omc team x'") is True
    # other omg subcommands not denied
    assert should_deny_command("omg doctor") is False
    assert should_deny_command("omg accept --run x") is False
    assert should_deny_command("omg setup") is False
    assert should_deny_command("omg worker seal") is False
    # arg/quoted mention stays allowed
    assert should_deny_command('echo "run omg team"') is False
    assert should_deny_command('echo "run omc team"') is False


def test_omc_team_shell_metachar_boundary_no_space_denied():
    """#146 STOP-6 / P1: shlex must not miss ``team`` glued to shell ops.

    Real shells treat ``omc team>out`` as ``omc team`` + redirect; a pure
    shlex tail decode sees one token ``team>out`` and would allow the command.
    """
    deny_cmds = (
        "omc team>out 2:codex x",
        "omc team>/dev/null 2:codex x",
        "omc team;echo",
        "omc team|cat",
        "omc team&&x",
        "omc team||true",
        "omc team</dev/null 2:codex x",
        "omc team>out",
        # with-space redirections still denied
        "omc team >out 2:codex x",
        "omc team >/dev/null 2:codex x",
        # path / wrapper / shell-c with no-space team boundary
        "/usr/bin/omc team>out x",
        "command omc team;echo hi",
        "env omc team|cat",
        "bash -lc 'omc team>out 2:codex x'",
        "bash -lc 'omc team;echo'",
    )
    for cmd in deny_cmds:
        assert should_deny_command(cmd) is True, cmd

    # Harmless argument / quoted mentions remain allowed.
    allow_cmds = (
        'echo "run omc team"',
        "echo omc team>not-a-command",  # omc not in command position
        "true; echo omc team",
        "printf '%s' 'omc team>out'",
    )
    for cmd in allow_cmds:
        assert should_deny_command(cmd) is False, cmd


def test_worker_nested_team_shell_metachar_boundary(monkeypatch):
    """#146 P1 minor: worker DiD still sees nested launch through redirections."""
    from omg_cli.deny import is_first_party_team_nested_launch

    monkeypatch.setenv("OMG_TEAM_WORKER", "1")

    deny_cmds = (
        "omg team>out launch --goal x",
        "omg team>/dev/null launch --goal x",
        "omg team;launch --goal x",  # bare ``omg team`` is launch surface
        "omg team|cat",
        "omg team&&launch --goal x",
        "omg team</dev/null launch --goal x",
        "omg team>out 2:executor \"x\"",
        "bash -lc 'omg team>out launch --goal x'",
        "/opt/omg/bin/omg team>/dev/null start --goal x",
        "command omg team;scale --add 1",
    )
    for cmd in deny_cmds:
        assert is_first_party_team_nested_launch(cmd) is True, cmd
        d = decide_pre_tool_use(
            {"toolName": "run_terminal_command", "toolInput": {"command": cmd}}
        )
        assert d["decision"] == "deny", cmd
        assert "E_TEAM_NESTED_LAUNCH" in (d.get("reason") or ""), cmd

    # Safe ops + arg mentions still allowed through soft-gate.
    allow_cmds = (
        "omg team api catalog",
        "omg team status r",
        "omg team>out api catalog",  # redir only; op remains api
        "omg team>/dev/null status",
        'echo "omg team launch"',
    )
    for cmd in allow_cmds:
        assert is_first_party_team_nested_launch(cmd) is False, cmd
        d = decide_pre_tool_use(
            {"toolName": "run_terminal_command", "toolInput": {"command": cmd}}
        )
        assert d["decision"] == "allow", cmd


def test_first_party_team_not_mislabeled_as_external_cli():
    """decide_pre_tool_use must allow bare omg team without advisor messaging."""
    ev = {
        "toolName": "run_terminal_command",
        "toolInput": {"command": "omg team 2:executor \"fix tests\""},
    }
    decision = decide_pre_tool_use(ev)
    assert decision["decision"] == "allow"


def test_worker_nested_team_launch_denied_with_team_reason(monkeypatch):
    """Process-env worker markers block nested launch; reason is Team-specific."""
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    # command-text cannot clear the process marker
    ev = {
        "toolName": "run_terminal_command",
        "toolInput": {
            "command": "OMG_TEAM_WORKER=0 omg team launch --workers 2 --goal x"
        },
    }
    decision = decide_pre_tool_use(ev)
    assert decision["decision"] == "deny"
    reason = decision.get("reason") or ""
    assert "E_TEAM_NESTED_LAUNCH" in reason
    assert "omg ask" not in reason
    # identity-bound api still allowed through the soft-gate
    api_ev = {
        "toolName": "run_terminal_command",
        "toolInput": {"command": "omg team api claim-task --input '{}'"},
    }
    assert decide_pre_tool_use(api_ev)["decision"] == "allow"
    # path/wrapper/shell-c forms of nested launch also blocked
    for cmd in (
        "/opt/omg/bin/omg team start --goal x",
        "command omg team scale --add 1",
        "bash -lc 'omg team launch --goal x'",
        "omg team 2:executor \"x\"",
    ):
        d = decide_pre_tool_use(
            {"toolName": "run_terminal_command", "toolInput": {"command": cmd}}
        )
        assert d["decision"] == "deny", cmd
        assert "E_TEAM_NESTED_LAUNCH" in (d.get("reason") or "")


def test_worker_safe_first_team_head_does_not_mask_later_launch(monkeypatch):
    """#146 STOP-5: safe/api/status first head must not mask later launch/start.

    Independently reproduced bypass matrix under real process OMG_TEAM_WORKER=1.
    """
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")

    # Four independently reproduced allow-bypass commands (must deny).
    deny_cmds = (
        "omg team api catalog; OMG_TEAM_WORKER=0 omg team launch --workers 1 --goal x",
        "omg team status r; OMG_TEAM_WORKER=0 omg team start --goal x",
        "omg team api catalog; env OMG_TEAM_WORKER=0 omg team launch --workers 1 --goal x",
        "bash -lc 'omg team api catalog; OMG_TEAM_WORKER=0 omg team launch --workers 1 --goal x'",
        # reversed order (launch first still denies)
        "omg team launch --workers 1 --goal x; omg team api catalog",
        "OMG_TEAM_WORKER=0 omg team start --goal x; omg team status r",
        # multiple safe heads then forbidden
        "omg team api catalog; omg team status r; omg team api claim-task; omg team launch --goal x",
        # separators / newlines
        "omg team api catalog\nomg team launch --workers 1 --goal x",
        "omg team status r && OMG_TEAM_WORKER=0 omg team run --goal x",
        "omg team api catalog || omg team resume",
        # path / wrapper heads after safe
        "omg team api catalog; /opt/omg/bin/omg team launch --goal x",
        "omg team status r; command omg team scale --add 1",
        "omg team api catalog; env omg team stop",
        "omg team api catalog; exec omg team collect",
        # shorthand after safe
        "omg team api catalog; omg team 2:executor \"x\"",
        # nested shell with newline-separated multi-head body
        "bash -c 'omg team status r\nOMG_TEAM_WORKER=0 omg team supervisor'",
    )
    for cmd in deny_cmds:
        d = decide_pre_tool_use(
            {"toolName": "run_terminal_command", "toolInput": {"command": cmd}}
        )
        assert d["decision"] == "deny", cmd
        reason = d.get("reason") or ""
        assert "E_TEAM_NESTED_LAUNCH" in reason, cmd
        assert "omg ask" not in reason

    # Preserve identity-bound api + genuine status-only when no forbidden head.
    allow_cmds = (
        "omg team api catalog",
        "omg team api claim-task --input '{}'",
        "omg team status r",
        "omg team status",
        "omg team api catalog; omg team status r",
        "omg team api catalog\nomg team api claim-task",
        "bash -lc 'omg team api catalog; omg team status r'",
        "/opt/omg/bin/omg team api catalog",
        "command omg team status r",
        "env omg team api catalog",
    )
    for cmd in allow_cmds:
        d = decide_pre_tool_use(
            {"toolName": "run_terminal_command", "toolInput": {"command": cmd}}
        )
        assert d["decision"] == "allow", cmd


def test_worker_multi_head_nested_launch_classifier_and_budget(monkeypatch):
    """Direct classifier coverage: multi-head + bounded adversarial size."""
    from omg_cli.deny import is_first_party_team_nested_launch

    monkeypatch.setenv("OMG_TEAM_WORKER", "1")

    assert is_first_party_team_nested_launch(
        "omg team api catalog; omg team launch --goal x"
    )
    assert is_first_party_team_nested_launch(
        "omg team status r; omg team start --goal x"
    )
    assert not is_first_party_team_nested_launch("omg team api catalog")
    assert not is_first_party_team_nested_launch(
        "omg team api catalog; omg team status r"
    )
    # Command-text env never changes classification when process is worker
    # (caller gates on process env; classifier itself is command-only).
    assert is_first_party_team_nested_launch(
        "OMG_TEAM_WORKER=0 omg team launch --goal x"
    )

    # Bounded/adversarial size: many safe heads then one launch (linear scan).
    many_safe = "; ".join(["omg team api catalog"] * 64)
    assert not is_first_party_team_nested_launch(many_safe)
    assert is_first_party_team_nested_launch(
        many_safe + "; omg team launch --workers 1 --goal x"
    )
    # Oversized shell-c body still fail-closes via parse budget (or classifies).
    huge = "omg team api catalog; " * 2000 + "omg team launch --goal x"
    assert is_first_party_team_nested_launch(huge) is True
