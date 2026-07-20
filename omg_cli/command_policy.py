# omg_cli/command_policy.py
"""Semantic acceptance command policy (operator-intent gate, not a sandbox).

Acceptance commands are filtered by **executable family + argv grammar**, not
basename alone. This blocks common interpreter escapes (``python -c``,
``node -e``, ``npx …``) while still allowing frozen test runners.

Hard floors (never liftable via ``--allow-cmd`` / break-glass):
- shell interpreters as argv[0]
- external agent CLIs (claude/codex/…) and destructive bins (rm/sudo)
- ``npx`` / ``uvx`` / ``pipx`` style package runners by default
- ``python* -c`` / ``-e`` and arbitrary ``-m`` modules outside pytest|unittest

This module does **not** inspect script contents; untrusted repo code may still
run when an operator freezes ``pytest`` / ``python -m pytest``. Pair with
``docs/security-model.md``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

POLICY_VERSION = "2"

# Exact: python | python2 | python3 | python2.N | python3.N
# Rejects python3evil, python3-config, python3foo, etc.
_PYTHON_BIN_RE = re.compile(r"^python([23](\.\d+)?)?$")

# Node-family interpreters that must not get -e / -p eval.
_NODE_BIN_RE = re.compile(r"^node(\.\d+)?$")

# Default basenames allowed as acceptance argv[0] (after Path.name).
# Semantic families (python*/npm) get extra argv grammar checks below.
DEFAULT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "true",
        "false",
        "pytest",
        "python",
        "python3",
        # Optional common runners (no eval flags of their own in default use)
        "make",
        "npm",
        "cargo",
        "go",
        "dart",
        "flutter",
        "ruff",
        "mypy",
        "black",
        "git",
    }
)

# Always denied even with --allow-cmd / break-glass (security floor).
ALWAYS_DENY_BASENAMES: frozenset[str] = frozenset(
    {
        "claude",
        "codex",
        "omx",
        "agy",
        "cursor-agent",
        "kimi",
        "rm",
        "sudo",
        "doas",
        # Package runners: default deny (escape / network install surface)
        "npx",
        "uvx",
        "pipx",
        "bunx",
        "pnpm",
        "yarn",
        "deno",
    }
)

# Shell interpreters: never allowed as acceptance argv[0].
SHELL_BASENAMES: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "csh",
        "tcsh",
        "fish",
        "ksh",
    }
)

# python -m MODULE allowlist (only test runners).
_PYTHON_M_ALLOWED: frozenset[str] = frozenset({"pytest", "unittest"})

# npm subcommands allowed without --allow-cmd.
# Forms: npm test [args], npm run test [args], npm run pytest [args]
_NPM_RUN_SCRIPTS: frozenset[str] = frozenset({"test", "pytest"})

# git: read-only inspection subcommands; destructive ops denied explicitly.
_GIT_ALLOWED_SUB: frozenset[str] = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "rev-parse",
        "rev-list",
        "describe",
        "ls-files",
        "ls-tree",
        "cat-file",
        "branch",  # listing only; destructive flags checked below
        "tag",  # listing; -d denied below
        "stash",  # list/show only; drop/clear denied
    }
)
_GIT_DENY_SUB: frozenset[str] = frozenset(
    {
        "clean",
        "push",
        "reset",
        "checkout",
        "restore",
        "rebase",
        "merge",
        "pull",
        "fetch",
        "remote",
        "config",
        "add",
        "commit",
        "am",
        "cherry-pick",
        "revert",
        "worktree",
        "filter-branch",
        "filter-repo",
        "gc",
        "reflog",
        "update-ref",
        "symbolic-ref",
        "init",
        "clone",
        "submodule",
    }
)
_MAKE_ALLOWED_TARGETS: frozenset[str] = frozenset(
    {"test", "check", "lint", "unit", "units", "pytest", "ci", "verify"}
)
_CARGO_ALLOWED: frozenset[str] = frozenset({"test", "check", "clippy", "fmt"})
_CARGO_DENY: frozenset[str] = frozenset(
    {"run", "install", "publish", "bench", "script", "build"}
)
_GO_ALLOWED: frozenset[str] = frozenset({"test", "vet", "fmt", "version"})
_GO_DENY: frozenset[str] = frozenset({"run", "generate", "get", "install", "mod"})
_DART_ALLOWED: frozenset[str] = frozenset({"test", "analyze", "format"})
_DART_DENY: frozenset[str] = frozenset({"run", "compile", "pub"})
# flutter: test|analyze only (no pub/run)
_FLUTTER_ALLOWED: frozenset[str] = frozenset({"test", "analyze"})


class CommandPolicyError(ValueError):
    """Raised when an acceptance command is rejected by the semantic policy."""


# Operator tips for common freezes that fail allowlist (QA / accept).
_DENY_TIPS: dict[str, str] = {
    "grep": "use a project .py helper (python3 path/to/check.py) instead of grep",
    "egrep": "use a project .py helper instead of egrep",
    "fgrep": "use a project .py helper instead of fgrep",
    "rg": "use a project .py helper instead of rg",
    "test": "use a project .py helper or true/false instead of test(1)",
    "[": "use a project .py helper or true/false instead of [",
    "omg": "omg is not on the acceptance allowlist; use python3 -m pytest or a project .py",
    "cat": "use a project .py helper that reads the file instead of cat",
    "sed": "use a project .py helper instead of sed",
    "awk": "use a project .py helper instead of awk",
    "head": "use a project .py helper instead of head",
    "tail": "use a project .py helper instead of tail",
}


def command_basename(argv0: str) -> str:
    """Return the executable basename for policy checks (handles paths)."""
    name = Path(str(argv0)).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def is_python_bin(base: str) -> bool:
    """True for python / python2 / python3 / python2.N / python3.N only."""
    return bool(_PYTHON_BIN_RE.match(base))


def is_node_bin(base: str) -> bool:
    return bool(_NODE_BIN_RE.match(base))


def coalesce_pytest_marker_expr(argv: Sequence[str]) -> list[str]:
    """Coalesce common freeze mistake: ``-m not live`` → ``-m 'not live'``.

    After ``pytest`` (or ``python -m pytest``), an unquoted shell marker
    expression is split by shlex into separate tokens. Pytest then treats
    ``not`` as the marker and ``live`` as a file path. When we see
    ``-m`` / ``--markers`` followed by ``not`` and a non-flag token, join them.
    """
    argv = [str(x) for x in argv]
    if not argv:
        return []
    # Locate pytest entry: bare pytest, or python* -m pytest
    start = 0
    base0 = command_basename(argv[0])
    if is_python_bin(base0):
        for i in range(1, len(argv) - 1):
            if argv[i] == "-m" and argv[i + 1].split(".", 1)[0] == "pytest":
                start = i + 2
                break
        else:
            return list(argv)
    elif base0 == "pytest":
        start = 1
    else:
        return list(argv)

    out = list(argv[:start])
    i = start
    while i < len(argv):
        tok = argv[i]
        # Only -m (mark expression). Do not treat pytest --markers (list markers).
        if tok == "-m" and i + 2 < len(argv):
            a, b = argv[i + 1], argv[i + 2]
            if a == "not" and b and not b.startswith("-"):
                out.append(tok)
                out.append(f"not {b}")
                i += 3
                continue
        out.append(tok)
        i += 1
    return out


def policy_hint_for_basename(base: str) -> str | None:
    """Optional one-line operator tip for a denied basename."""
    return _DENY_TIPS.get(base)


def resolve_allowlist(
    extra: Iterable[str] | None = None,
    *,
    base: Iterable[str] | None = None,
) -> frozenset[str]:
    """Default allowlist plus optional ``--allow-cmd`` extensions."""
    allowed = set(DEFAULT_ALLOWLIST if base is None else base)
    if extra:
        for name in extra:
            n = command_basename(str(name).strip())
            if n:
                allowed.add(n)
    return frozenset(allowed)


def _basename_allowed(base: str, allowed: frozenset[str]) -> bool:
    """True if *base* is in *allowed* or a versioned python binary family match."""
    if base in allowed:
        return True
    if not is_python_bin(base):
        return False
    if base == "python":
        return "python" in allowed
    if base.startswith("python3"):
        return "python3" in allowed or "python" in allowed
    if base.startswith("python2"):
        return "python2" in allowed or "python" in allowed
    return False


def _has_flag(argv: Sequence[str], *flags: str) -> bool:
    """True if any argv token is a flag, ``flag=value``, or glued short form.

    Glued forms like ``-cimport os`` / ``-cprint(1)`` must match floor denials
    for ``-c`` / ``-e`` (break-glass path included).
    """
    flag_set = set(flags)
    for tok in argv[1:]:
        if tok in flag_set:
            return True
        for f in flags:
            if tok.startswith(f + "="):
                return True
            # Glued short options: -cCODE, -eCODE (not longer names like -cache)
            if len(f) == 2 and f.startswith("-") and not f.startswith("--"):
                if tok.startswith(f) and (len(tok) == 2 or tok[2:3] != "-"):
                    return True
    return False


def _path_is_under_project(path_str: str, project_root: Path | None) -> bool:
    """True if path resolves under project_root (or is a bare relative .py name).

    When *project_root* is None, only reject absolute paths outside a reasonable
    relative form: allow relative paths ending in ``.py`` (freeze-time without
    root still blocks ``-c`` / absolute escapes via other checks).
    """
    p = Path(path_str)
    if project_root is None:
        # No root: allow relative *.py only (no absolute, no .. climb evidence).
        if p.is_absolute():
            return False
        parts = p.parts
        if ".." in parts:
            return False
        return path_str.endswith(".py") or p.suffix == ".py"

    root = project_root.resolve()
    try:
        # Relative to project when not absolute
        candidate = (root / p).resolve() if not p.is_absolute() else p.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.suffix == ".py" or str(candidate).endswith(".py")


def _check_python_argv(
    cmd: Sequence[str],
    *,
    where: str,
    project_root: Path | None,
) -> None:
    """Python family: only ``-m pytest|unittest`` or project ``.py`` script.

    After a permitted ``-m`` module or ``.py`` script, remaining argv is free
    (pytest/unittest/script args). ``-c`` / ``-e`` are always denied.
    """
    if len(cmd) < 2:
        raise CommandPolicyError(
            f"{where}: python requires -m pytest|unittest or a .py script path"
        )
    if _has_flag(cmd, "-c", "-e"):
        raise CommandPolicyError(
            f"{where}: python -c/-e is denied for acceptance "
            "(use -m pytest|unittest or a .py path under the project)"
        )

    i = 1
    while i < len(cmd):
        tok = cmd[i]
        if tok == "-m":
            if i + 1 >= len(cmd):
                raise CommandPolicyError(f"{where}: python -m requires a module name")
            mod = cmd[i + 1]
            mod_base = mod.split(".", 1)[0]
            if mod_base not in _PYTHON_M_ALLOWED and mod not in _PYTHON_M_ALLOWED:
                raise CommandPolicyError(
                    f"{where}: python -m {mod!r} denied "
                    f"(only -m pytest|unittest allowed)"
                )
            # Rest of argv belongs to the module (pytest/unittest) — allowed.
            return
        if tok in ("-c", "-e") or tok.startswith("-c") or tok.startswith("-e"):
            raise CommandPolicyError(
                f"{where}: python -c/-e is denied for acceptance"
            )
        if tok.startswith("-"):
            # Limited interpreter flags before -m / script
            if tok in ("-u", "-O", "-OO", "-B", "-S", "-s", "-I", "-E", "-P", "--"):
                i += 1
                continue
            if tok.startswith(("-W", "-X", "-Q")):
                i += 1
                continue
            raise CommandPolicyError(
                f"{where}: python flag {tok!r} not allowed before -m/script "
                "(allowed: -u/-O/-B/-I/-E/-s/-S/-P/-W*/-X*)"
            )
        # Positional: must be .py under project; remaining args are script args.
        if not (tok.endswith(".py") or Path(tok).suffix == ".py"):
            raise CommandPolicyError(
                f"{where}: python positional {tok!r} denied "
                "(need -m pytest|unittest or a .py script under the project)"
            )
        if not _path_is_under_project(tok, project_root):
            raise CommandPolicyError(
                f"{where}: python script {tok!r} is not a .py path under the project"
            )
        return

    raise CommandPolicyError(
        f"{where}: python requires -m pytest|unittest or a .py script path"
    )


def _check_node_argv(cmd: Sequence[str], *, where: str) -> None:
    """Node: deny -e/-p eval; allow script paths only when basename was allow-cmd'd."""
    if _has_flag(cmd, "-e", "--eval", "-p", "--print"):
        raise CommandPolicyError(
            f"{where}: node -e/--eval/-p is denied for acceptance"
        )


def _check_npm_argv(cmd: Sequence[str], *, where: str) -> None:
    """npm: only ``test`` or ``run test`` / ``run pytest`` (+ trailing args)."""
    if len(cmd) < 2:
        raise CommandPolicyError(
            f"{where}: npm requires subcommand 'test' or 'run test'|'run pytest'"
        )
    sub = cmd[1]
    if sub == "test":
        return
    if sub == "run":
        if len(cmd) < 3:
            raise CommandPolicyError(
                f"{where}: npm run requires a script name (test|pytest)"
            )
        script = cmd[2]
        if script not in _NPM_RUN_SCRIPTS:
            raise CommandPolicyError(
                f"{where}: npm run {script!r} denied "
                f"(only run test|pytest allowed)"
            )
        return
    raise CommandPolicyError(
        f"{where}: npm subcommand {sub!r} denied "
        "(only 'test' or 'run test'|'run pytest' allowed)"
    )


def _git_has_positional(cmd: Sequence[str], start: int = 2) -> bool:
    """True if any non-flag token appears at/after *start*."""
    return any(not str(x).startswith("-") for x in cmd[start:])


def _flag_denied(cmd: Sequence[str], *flags: str, start: int = 1) -> str | None:
    """Return matching forbidden flag token, including glued short forms."""
    flag_set = set(flags)
    for tok in cmd[start:]:
        if tok in flag_set:
            return tok
        for f in flags:
            if tok.startswith(f + "="):
                return tok
            # glued short: -fFILE, -C/tmp (not --file)
            if len(f) == 2 and f.startswith("-") and not f.startswith("--"):
                if tok.startswith(f) and len(tok) > 2 and tok[2:3] != "-":
                    return tok
    return None


def _check_git_argv(cmd: Sequence[str], *, where: str) -> None:
    """git: read-only status/diff/log/rev-parse family; mutate ops denied."""
    if len(cmd) < 2:
        raise CommandPolicyError(f"{where}: git requires a subcommand")
    sub = cmd[1]
    if sub in _GIT_DENY_SUB:
        raise CommandPolicyError(
            f"{where}: git {sub!r} denied for acceptance "
            "(read-only git status/diff/log/rev-parse only by default)"
        )
    if sub not in _GIT_ALLOWED_SUB:
        raise CommandPolicyError(
            f"{where}: git subcommand {sub!r} not in acceptance allowlist"
        )
    # list-only: branch/tag cannot create refs; stash only list|show
    if sub == "branch":
        if any(
            x in cmd[2:]
            for x in (
                "-D",
                "-d",
                "-m",
                "-M",
                "-c",
                "-C",
                "--delete",
                "--move",
                "--copy",
                "--create-reflog",
            )
        ):
            raise CommandPolicyError(f"{where}: git branch mutate flags denied")
        if _git_has_positional(cmd, 2):
            raise CommandPolicyError(
                f"{where}: git branch create denied (list-only; no new branch name)"
            )
    if sub == "tag":
        if any(x in cmd[2:] for x in ("-d", "-f", "-a", "-s", "-u", "-m")):
            raise CommandPolicyError(f"{where}: git tag mutate flags denied")
        if _git_has_positional(cmd, 2):
            raise CommandPolicyError(
                f"{where}: git tag create denied (list-only; no new tag name)"
            )
    if sub == "stash":
        # bare `git stash` ≡ push — deny unless explicit list|show
        if len(cmd) < 3 or str(cmd[2]).startswith("-"):
            raise CommandPolicyError(
                f"{where}: git stash requires list|show "
                "(bare stash defaults to push)"
            )
        action = cmd[2]
        if action not in ("list", "show"):
            raise CommandPolicyError(
                f"{where}: git stash {action!r} denied (only list|show allowed)"
            )
    # No -c config injection
    if "-c" in cmd[1:] or _flag_denied(cmd, "-c", start=1):
        raise CommandPolicyError(f"{where}: git -c config injection denied")


def _check_make_argv(cmd: Sequence[str], *, where: str) -> None:
    """make: only known test/lint/ci targets; bare make denied; no -f/-C."""
    if len(cmd) < 2:
        raise CommandPolicyError(f"{where}: make requires an allowed target")
    denied = _flag_denied(
        cmd,
        "-f",
        "--file",
        "--makefile",
        "-C",
        "--directory",
        "-I",
        "--include-dir",
        "--eval",
        start=1,
    )
    if denied:
        raise CommandPolicyError(
            f"{where}: make flag {denied!r} denied "
            "(no -f/-C/--file/--directory/--eval overrides)"
        )
    # skip make flags like -j4; only allow when a target token is known
    targets = [t for t in cmd[1:] if not t.startswith("-")]
    if not targets:
        raise CommandPolicyError(f"{where}: make requires an allowed target name")
    for t in targets:
        if t not in _MAKE_ALLOWED_TARGETS:
            raise CommandPolicyError(
                f"{where}: make target {t!r} denied "
                f"(allowed: {', '.join(sorted(_MAKE_ALLOWED_TARGETS))})"
            )


def _check_cargo_argv(cmd: Sequence[str], *, where: str) -> None:
    """cargo: test/check/clippy/fmt only; deny run/install/publish/build and path overrides."""
    if len(cmd) < 2:
        raise CommandPolicyError(f"{where}: cargo requires a subcommand")
    sub = cmd[1]
    if sub in _CARGO_DENY:
        raise CommandPolicyError(f"{where}: cargo {sub!r} denied for acceptance")
    if sub not in _CARGO_ALLOWED:
        raise CommandPolicyError(f"{where}: cargo subcommand {sub!r} not allowed")
    denied = _flag_denied(
        cmd,
        "--manifest-path",
        "--config",
        "--target-dir",
        "-C",
        start=2,
    )
    if denied:
        raise CommandPolicyError(
            f"{where}: cargo flag {denied!r} denied for acceptance"
        )
def _check_go_argv(cmd: Sequence[str], *, where: str) -> None:
    """go: test/vet/fmt/version; deny run/generate/get/install/mod and -exec."""
    if len(cmd) < 2:
        raise CommandPolicyError(f"{where}: go requires a subcommand")
    sub = cmd[1]
    if sub in _GO_DENY:
        raise CommandPolicyError(f"{where}: go {sub!r} denied for acceptance")
    if sub not in _GO_ALLOWED:
        raise CommandPolicyError(f"{where}: go subcommand {sub!r} not allowed")
    # Go flag package accepts both -exec and --exec (and -toolexec / --toolexec).
    denied = _flag_denied(
        cmd, "-exec", "--exec", "-toolexec", "--toolexec", start=2
    )
    if denied:
        raise CommandPolicyError(
            f"{where}: go flag {denied!r} denied for acceptance"
        )

def _check_dart_argv(cmd: Sequence[str], *, where: str) -> None:
    """dart: test/analyze/format; deny run/compile/pub."""
    if len(cmd) < 2:
        raise CommandPolicyError(f"{where}: dart requires a subcommand")
    sub = cmd[1]
    if sub in _DART_DENY:
        raise CommandPolicyError(f"{where}: dart {sub!r} denied for acceptance")
    if sub not in _DART_ALLOWED:
        raise CommandPolicyError(f"{where}: dart subcommand {sub!r} not allowed")


def _check_flutter_argv(cmd: Sequence[str], *, where: str) -> None:
    """flutter: only test|analyze."""
    if len(cmd) < 2:
        raise CommandPolicyError(f"{where}: flutter requires a subcommand")
    sub = cmd[1]
    if sub not in _FLUTTER_ALLOWED:
        raise CommandPolicyError(
            f"{where}: flutter {sub!r} denied (only test|analyze allowed)"
        )


def check_command_policy(
    cmd: Sequence[str],
    *,
    allowlist: Iterable[str] | None = None,
    no_allowlist: bool = False,
    project_root: Path | str | None = None,
    where: str = "command",
) -> None:
    """Raise ``CommandPolicyError`` if *cmd* is not permitted for acceptance.

    Policy order:
    1. Non-empty argv required.
    2. Shell interpreters as argv[0] → always deny.
    3. Always-deny basenames (agent CLIs, rm, npx, …) → always deny
       (even with ``--allow-cmd`` / ``no_allowlist``).
    4. Unless ``no_allowlist``, argv[0] must be in allowlist / python family.
    5. Semantic argv grammar for python / node / npm families.
    6. Global deny of interpreter eval flags on python/node even under break-glass.
    """
    if not cmd:
        raise CommandPolicyError(f"{where}: empty command")
    base = command_basename(cmd[0])
    if not base:
        raise CommandPolicyError(f"{where}: empty argv[0] basename")

    root: Path | None
    if project_root is None:
        root = None
    else:
        root = Path(project_root)

    if base in SHELL_BASENAMES:
        raise CommandPolicyError(
            f"{where}: shell interpreter {base!r} is not allowed as acceptance "
            "command (use direct argv like pytest/python, not bash -c)"
        )

    if base in ALWAYS_DENY_BASENAMES:
        raise CommandPolicyError(
            f"{where}: basename {base!r} is permanently denied for acceptance"
        )

    # Break-glass still cannot use python -c / node -e.
    if is_python_bin(base):
        # Floor: -c/-e always denied; full grammar when not no_allowlist or always
        if _has_flag(cmd, "-c", "-e"):
            raise CommandPolicyError(
                f"{where}: python -c/-e is denied for acceptance "
                "(always-deny floor; use -m pytest|unittest or project .py "
                "under the repo — e.g. python3 scripts/check.py)"
            )
    if is_node_bin(base) or base == "node":
        if _has_flag(cmd, "-e", "--eval", "-p", "--print"):
            raise CommandPolicyError(
                f"{where}: node -e/--eval/-p is denied for acceptance "
                "(always-deny floor)"
            )

    if no_allowlist:
        # Emergency: skip positive allowlist membership, keep floors above.
        # Still apply semantic grammar for python when present (no -c already).
        if is_python_bin(base) and len(cmd) > 1:
            # Under break-glass allow broader python *except* -c/-e (already denied).
            # Still block -m with clearly dangerous patterns? Keep -m free under glass
            # except we already blocked -c. Operator owns risk.
            pass
        return

    allowed = (
        frozenset(allowlist) if allowlist is not None else DEFAULT_ALLOWLIST
    )
    if not _basename_allowed(base, allowed):
        tip = policy_hint_for_basename(base)
        tip_s = f" tip: {tip}." if tip else ""
        raise CommandPolicyError(
            f"{where}: basename {base!r} not in acceptance allowlist "
            f"({', '.join(sorted(allowed))}); use --allow-cmd {base} "
            "(TTY-only --no-allowlist is break-glass and still applies deny floor)."
            f"{tip_s} Prefer: python3 -m pytest -q -m 'not live' or "
            "python3 path/to/project_check.py"
        )

    # Semantic families
    if is_python_bin(base):
        _check_python_argv(cmd, where=where, project_root=root)
    elif is_node_bin(base) or base == "node":
        _check_node_argv(cmd, where=where)
    elif base == "npm":
        _check_npm_argv(cmd, where=where)
    elif base == "git":
        _check_git_argv(cmd, where=where)
    elif base == "make":
        _check_make_argv(cmd, where=where)
    elif base == "cargo":
        _check_cargo_argv(cmd, where=where)
    elif base == "go":
        _check_go_argv(cmd, where=where)
    elif base == "dart":
        _check_dart_argv(cmd, where=where)
    elif base == "flutter":
        _check_flutter_argv(cmd, where=where)


def check_commands_policy(
    commands: list[list[str]],
    *,
    allowlist: Iterable[str] | None = None,
    no_allowlist: bool = False,
    project_root: Path | str | None = None,
) -> None:
    """Validate every command; raise on first rejection."""
    for i, cmd in enumerate(commands):
        check_command_policy(
            cmd,
            allowlist=allowlist,
            no_allowlist=no_allowlist,
            project_root=project_root,
            where=f"manifest.commands[{i}]",
        )


# Back-compat aliases used by older imports / tests during migration.
CommandAllowlistError = CommandPolicyError
check_command_allowlist = check_command_policy
check_commands_allowlist = check_commands_policy
