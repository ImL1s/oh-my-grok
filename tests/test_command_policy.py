# tests/test_command_policy.py
"""Semantic acceptance command policy."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omg_cli.command_policy import (
    CommandPolicyError,
    check_command_policy,
    coalesce_pytest_marker_expr,
    is_python_bin,
    resolve_allowlist,
    _basename_allowed,
)


def test_true_false_pytest_allowed():
    check_command_policy(["true"])
    check_command_policy(["false"])
    check_command_policy(["pytest", "tests/", "-q"])
    check_command_policy(["/usr/bin/pytest", "-q"])


def test_coalesce_pytest_not_marker():
    raw = ["python3", "-m", "pytest", "-q", "-m", "not", "live"]
    fixed = coalesce_pytest_marker_expr(raw)
    assert fixed == ["python3", "-m", "pytest", "-q", "-m", "not live"]
    check_command_policy(fixed)
    # already quoted stays stable
    assert coalesce_pytest_marker_expr(
        ["python3", "-m", "pytest", "-m", "not live"]
    ) == ["python3", "-m", "pytest", "-m", "not live"]


def test_grep_deny_includes_tip():
    with pytest.raises(CommandPolicyError, match="project .py|grep"):
        check_command_policy(["grep", "-q", "x", "f"])


def test_python_c_denied():
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy([sys.executable, "-c", "pass"])
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(["python3", "-c", "import os; os.system('claude')"])
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(["python", "-c", "print(1)"])


def test_python_m_pytest_allowed():
    check_command_policy(["python3", "-m", "pytest", "tests/", "-q"])
    check_command_policy(["python", "-m", "unittest", "discover"])
    check_command_policy(["python3.12", "-m", "pytest"])
    check_command_policy([sys.executable, "-m", "pytest", "-q"])


def test_python_m_other_denied():
    with pytest.raises(CommandPolicyError, match="-m"):
        check_command_policy(["python3", "-m", "http.server"])
    with pytest.raises(CommandPolicyError, match="-m"):
        check_command_policy(["python3", "-m", "pip", "install", "x"])


def test_python3evil_denied():
    with pytest.raises(CommandPolicyError, match="not in acceptance allowlist"):
        check_command_policy(["python3evil", "-m", "pytest"])
    allowed = resolve_allowlist()
    assert _basename_allowed("python3evil", allowed) is False
    assert _basename_allowed("python3-config", allowed) is False
    assert is_python_bin("python3.12") is True
    assert is_python_bin("python3evil") is False


def test_python_script_under_project(tmp_path):
    script = tmp_path / "tests" / "t.py"
    script.parent.mkdir(parents=True)
    script.write_text("print(1)\n", encoding="utf-8")
    check_command_policy(
        ["python3", "tests/t.py"],
        project_root=tmp_path,
    )
    # absolute outside project
    with pytest.raises(CommandPolicyError, match="not a .py path under the project"):
        check_command_policy(
            ["python3", "/etc/passwd.py"],
            project_root=tmp_path,
        )


def test_npx_denied():
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["npx", "eslint"])
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["npx", "claude"])


def test_npm_only_test_scripts():
    check_command_policy(["npm", "test"])
    check_command_policy(["npm", "run", "test"])
    check_command_policy(["npm", "run", "pytest", "--", "-q"])
    with pytest.raises(CommandPolicyError, match="npm"):
        check_command_policy(["npm", "install"])
    with pytest.raises(CommandPolicyError, match="npm"):
        check_command_policy(["npm", "run", "build"])


def test_shell_and_agent_always_denied():
    with pytest.raises(CommandPolicyError, match="shell interpreter"):
        check_command_policy(["bash", "-c", "true"])
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["claude", "--version"])
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["codex", "exec", "hi"])
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["rm", "-rf", "/"])


def test_no_allowlist_still_denies_floor():
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["claude"], no_allowlist=True)
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(["python3", "-c", "pass"], no_allowlist=True)
    with pytest.raises(CommandPolicyError, match="shell"):
        check_command_policy(["bash", "-c", "true"], no_allowlist=True)
    # break-glass can run non-default bins that are not on the floor
    check_command_policy(["curl", "https://example.com"], no_allowlist=True)


def test_allow_cmd_extends_but_not_floor():
    allowed = resolve_allowlist(["hello"])
    check_command_policy(["hello", "world"], allowlist=allowed)
    with pytest.raises(CommandPolicyError, match="permanently denied"):
        check_command_policy(["claude"], allowlist=resolve_allowlist(["claude"]))


def test_node_eval_denied_even_if_allowed():
    allowed = resolve_allowlist(["node"])
    with pytest.raises(CommandPolicyError, match="-e"):
        check_command_policy(["node", "-e", "console.log(1)"], allowlist=allowed)

def test_glued_python_c_denied_even_with_no_allowlist():
    from omg_cli.command_policy import check_command_policy, CommandPolicyError
    from pathlib import Path
    import pytest
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(CommandPolicyError):
        check_command_policy(["python3", "-cimport os"], no_allowlist=True, project_root=root)
    with pytest.raises(CommandPolicyError):
        check_command_policy(["python3", "-c", "print(1)"], no_allowlist=True, project_root=root)


def test_git_safe_subcommands_allowed():
    check_command_policy(["git", "status"])
    check_command_policy(["git", "diff", "--stat"])
    check_command_policy(["git", "rev-parse", "HEAD"])
    check_command_policy(["git", "log", "-1", "--oneline"])


def test_git_destructive_denied():
    for cmd in (
        ["git", "clean", "-fdx"],
        ["git", "push", "origin", "main"],
        ["git", "reset", "--hard"],
        ["git", "checkout", "."],
        ["git", "restore", "."],
        ["git", "branch", "-D", "x"],
        ["git", "tag", "-d", "v1"],
        ["git", "remote", "add", "x", "y"],
        ["git", "config", "user.email", "x"],
        ["git", "rebase", "main"],
        ["git", "merge", "x"],
    ):
        with pytest.raises(CommandPolicyError, match="git"):
            check_command_policy(cmd)


def test_make_target_allowlist():
    check_command_policy(["make", "test"])
    check_command_policy(["make", "check"])
    with pytest.raises(CommandPolicyError, match="make"):
        check_command_policy(["make", "pwn"])
    with pytest.raises(CommandPolicyError, match="make"):
        check_command_policy(["make"])


def test_cargo_go_dart_flutter_grammar():
    check_command_policy(["cargo", "test"])
    check_command_policy(["cargo", "check"])
    with pytest.raises(CommandPolicyError, match="cargo"):
        check_command_policy(["cargo", "run"])
    with pytest.raises(CommandPolicyError, match="cargo"):
        check_command_policy(["cargo", "build"])
    with pytest.raises(CommandPolicyError, match="cargo"):
        check_command_policy(["cargo", "test", "--manifest-path", "/tmp/evil/Cargo.toml"])
    check_command_policy(["go", "test", "./..."])
    with pytest.raises(CommandPolicyError, match="go"):
        check_command_policy(["go", "run", "."])
    with pytest.raises(CommandPolicyError, match="go"):
        check_command_policy(["go", "test", "-exec", "/tmp/pwn", "./..."])
    with pytest.raises(CommandPolicyError, match="go"):
        check_command_policy(["go", "test", "-toolexec", "/tmp/pwn", "./..."])
    with pytest.raises(CommandPolicyError, match="go"):
        check_command_policy(["go", "test", "--exec", "/tmp/pwn", "./..."])
    with pytest.raises(CommandPolicyError, match="go"):
        check_command_policy(["go", "test", "--toolexec=/tmp/pwn", "./..."])
    check_command_policy(["dart", "test"])
    with pytest.raises(CommandPolicyError, match="dart"):
        check_command_policy(["dart", "run", "bin/x.dart"])
    check_command_policy(["flutter", "test"])
    with pytest.raises(CommandPolicyError, match="flutter"):
        check_command_policy(["flutter", "run"])


def test_git_list_only_no_create_or_bare_stash():
    with pytest.raises(CommandPolicyError, match="git"):
        check_command_policy(["git", "stash"])  # bare ≡ push
    with pytest.raises(CommandPolicyError, match="git"):
        check_command_policy(["git", "stash", "push"])
    check_command_policy(["git", "stash", "list"])
    with pytest.raises(CommandPolicyError, match="git"):
        check_command_policy(["git", "branch", "new-branch"])
    with pytest.raises(CommandPolicyError, match="git"):
        check_command_policy(["git", "tag", "v1.0"])
    check_command_policy(["git", "branch", "-a"])
    check_command_policy(["git", "tag", "-l"])


def test_make_file_and_directory_overrides_denied():
    with pytest.raises(CommandPolicyError, match="make"):
        check_command_policy(["make", "-f/tmp/evil.mk", "test"])
    with pytest.raises(CommandPolicyError, match="make"):
        check_command_policy(["make", "-f", "/tmp/evil.mk", "test"])
    with pytest.raises(CommandPolicyError, match="make"):
        check_command_policy(["make", "-C/tmp", "test"])
    with pytest.raises(CommandPolicyError, match="make"):
        check_command_policy(["make", "--directory=/tmp", "test"])
    with pytest.raises(CommandPolicyError, match="make"):
        check_command_policy(["make", "--eval=evil", "test"])


def test_no_allowlist_blocks_combined_short_flags():
    """Break-glass floor must still deny python code exec via combined short
    options like ``-ic`` (CPython treats it as ``-i -c``)."""
    root = Path(__file__).resolve().parents[1]
    # Normal path already denies it.
    with pytest.raises(CommandPolicyError):
        check_command_policy(
            ["python3", "-ic", "print(1)"], project_root=root
        )
    # Break-glass must ALSO deny it — the -c floor is never liftable.
    with pytest.raises(CommandPolicyError):
        check_command_policy(
            ["python3", "-ic", "import os; os.system('id')"],
            no_allowlist=True,
            project_root=root,
        )
    with pytest.raises(CommandPolicyError):
        check_command_policy(
            ["python3", "-Ic", "print(1)"], no_allowlist=True, project_root=root
        )
    # A cluster whose arg-consuming option is NOT c/e (e.g. -Om == -O -m) must
    # not be false-flagged by the floor (it reaches the -m grammar instead).
    # -m with an allowed module stays permitted.
    check_command_policy(
        ["python3", "-m", "pytest", "-q"], no_allowlist=True, project_root=root
    )


def test_floor_still_denies_interpreter_c_e_region():
    """Floor must still deny interpreter-owned -c/-e (before script/-m)."""
    root = Path(__file__).resolve().parents[1]
    for no_al in (False, True):
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-c", "x"],
                no_allowlist=no_al,
                project_root=root,
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-cimport os"],
                no_allowlist=no_al,
                project_root=root,
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-ic", "x"],
                no_allowlist=no_al,
                project_root=root,
            )
    # node floor: -e / -pe (cluster)
    with pytest.raises(CommandPolicyError, match="-e"):
        check_command_policy(["node", "-e", "x"], no_allowlist=True)
    with pytest.raises(CommandPolicyError, match="-e|-p"):
        check_command_policy(["node", "-pe", "x"], no_allowlist=True)


def test_floor_ignores_script_and_module_args():
    """Tokens after script path or -m belong to the script/module, not python.

    ``-vc`` / ``-rc`` / pytest ``-c`` must not trip the interpreter -c/-e floor.
    """
    root = Path(__file__).resolve().parents[1]
    # Real project .py so path grammar passes; -vc is check.py's arg.
    check_command_policy(
        ["python3", "scripts/check_docs_links.py", "-vc"],
        project_root=root,
    )
    # pytest module args (including its own -c config) after -m
    check_command_policy(
        ["python3", "-m", "pytest", "-rc"],
        project_root=root,
    )
    check_command_policy(
        ["python3", "-m", "pytest", "-q", "-c", "pytest.ini"],
        project_root=root,
    )


def test_floor_denies_c_after_arg_consuming_wxq_options():
    """Bare arg-consuming interpreter options must not end the flag region.

    Separate-token values (``python3 -W ignore -c`` / ``node -r ./foo -e``)
    previously looked like bare script boundaries, so ``-c``/``-e`` escaped the
    floor. Glued forms (``-Wignore``, ``--require=x``) stay one token.
    """
    root = Path(__file__).resolve().parents[1]
    node_allowed = resolve_allowlist(["node"])
    bypass_python = [
        ["python3", "-W", "ignore", "-c", "x"],
        ["python3", "-W", "ignore", "-c", 'import os; os.system("id")'],
        ["python3", "-X", "importtime", "-c", "x"],
        ["python3", "-Q", "new", "-c", "x"],
        ["python3", "-W", "a", "-W", "b", "-c", "x"],
        ["python3", "-Wignore", "-c", "x"],  # glued -W; still must see -c
    ]
    for argv in bypass_python:
        for no_al in (False, True):
            with pytest.raises(CommandPolicyError, match="-c"):
                check_command_policy(
                    argv, no_allowlist=no_al, project_root=root
                )

    # -W value that looks like a .py script: normal grammar must NOT treat it as
    # the script path and allow following -c (pin NORMAL mode explicitly).
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(
            ["python3", "-W", "x.py", "-c", 'import os; os.system("id")'],
            project_root=root,
        )
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(
            ["python3", "-W", "x.py", "-c", 'import os; os.system("id")'],
            no_allowlist=True,
            project_root=root,
        )

    # Explicit break-glass assertions (floor is the only check there).
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(
            ["python3", "-W", "ignore", "-c", 'import os; os.system("id")'],
            no_allowlist=True,
            project_root=root,
        )
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(
            ["python3", "-X", "importtime", "-c", "x"],
            no_allowlist=True,
            project_root=root,
        )
    with pytest.raises(CommandPolicyError, match="-c"):
        check_command_policy(
            ["python3", "-Q", "new", "-c", "x"],
            no_allowlist=True,
            project_root=root,
        )

    # Node: -r / --require (and long loaders) consume the next token.
    bypass_node = [
        ["node", "-r", "./foo", "-e", "x"],
        ["node", "--require", "./foo", "-e", "x"],
        ["node", "-r", "a", "-r", "b", "-e", "x"],
    ]
    for argv in bypass_node:
        with pytest.raises(CommandPolicyError, match="-e"):
            check_command_policy(argv, no_allowlist=True)
        with pytest.raises(CommandPolicyError, match="-e"):
            check_command_policy(argv, allowlist=node_allowed)

    # Unchanged must-deny baselines (interpreter-owned -c/-e).
    for no_al in (False, True):
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-c", "x"], no_allowlist=no_al, project_root=root
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-cimport os"],
                no_allowlist=no_al,
                project_root=root,
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-ic", "x"], no_allowlist=no_al, project_root=root
            )
    with pytest.raises(CommandPolicyError, match="-e"):
        check_command_policy(["node", "-e", "x"], no_allowlist=True)
    with pytest.raises(CommandPolicyError, match="-e|-p"):
        check_command_policy(["node", "-pe", "x"], no_allowlist=True)

    # FP fix preserved: script/module args after real boundary stay allowed.
    check_command_policy(
        ["python3", "-m", "pytest", "-rc"],
        project_root=root,
    )
    check_command_policy(
        ["python3", "-m", "pytest", "-q", "-c", "pytest.ini"],
        project_root=root,
    )
    check_command_policy(
        ["python3", "scripts/check_docs_links.py", "-vc"],
        project_root=root,
    )


def test_floor_fail_closed_unknown_arg_options_before_eval():
    """Unknown ``--flag value`` must not end the interpreter region (fail closed).

    Enumeration of arg-consuming options is incomplete: any unlisted option with
    a space-separated value previously treated that value as a bare script
    boundary, so a following ``-c``/``-e`` escaped the floor under break-glass.
    """
    root = Path(__file__).resolve().parents[1]
    node_allowed = resolve_allowlist(["node"])

    # The 6 live bypass repros (must DENY both normal and no_allowlist).
    bypass_repros = [
        (
            ["python3", "--check-hash-based-pycs", "always", "-c",
             'import os; os.system("id")'],
            "-c",
        ),
        (["node", "--max-old-space-size", "100", "-e", "x"], "-e"),
        (["node", "--stack-size", "1000", "-e", "x"], "-e"),
        (["node", "--title", "x", "-e", "x"], "-e"),
        (["node", "--v8-pool-size", "4", "-e", "x"], "-e"),
        (["node", "--report-dir", "/tmp", "-e", "x"], "-e"),
    ]
    for argv, match in bypass_repros:
        for no_al in (False, True):
            kwargs: dict = {"no_allowlist": no_al, "project_root": root}
            if argv[0] == "node" and not no_al:
                kwargs = {"allowlist": node_allowed, "project_root": root}
            with pytest.raises(CommandPolicyError, match=match):
                check_command_policy(argv, **kwargs)

    # Known-option path still works (incl. .py value of -W).
    for no_al in (False, True):
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-W", "ignore", "-c", "x"],
                no_allowlist=no_al,
                project_root=root,
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-W", "x.py", "-c", "x"],
                no_allowlist=no_al,
                project_root=root,
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-X", "importtime", "-c", "x"],
                no_allowlist=no_al,
                project_root=root,
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-Q", "new", "-c", "x"],
                no_allowlist=no_al,
                project_root=root,
            )
    with pytest.raises(CommandPolicyError, match="-e"):
        check_command_policy(
            ["node", "-r", "./foo", "-e", "x"], no_allowlist=True
        )

    # Baselines.
    for no_al in (False, True):
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-c", "x"], no_allowlist=no_al, project_root=root
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-cimport os"],
                no_allowlist=no_al,
                project_root=root,
            )
        with pytest.raises(CommandPolicyError, match="-c"):
            check_command_policy(
                ["python3", "-ic", "x"], no_allowlist=no_al, project_root=root
            )
    with pytest.raises(CommandPolicyError, match="-e"):
        check_command_policy(["node", "-e", "x"], no_allowlist=True)
    with pytest.raises(CommandPolicyError, match="-e|-p"):
        check_command_policy(["node", "-pe", "x"], no_allowlist=True)

    # FP: real script / -m boundaries still allow trailing -c-looking args.
    check_command_policy(
        ["python3", "-m", "pytest", "-rc"],
        project_root=root,
    )
    check_command_policy(
        ["python3", "-m", "pytest", "-q", "-c", "pytest.ini"],
        project_root=root,
    )
    check_command_policy(
        ["python3", "scripts/check_docs_links.py", "-vc"],
        project_root=root,
    )
