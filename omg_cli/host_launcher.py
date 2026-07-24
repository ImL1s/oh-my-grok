"""OMX/Sol-aligned root host launcher for oh-my-grok.

Owns delimiter-aware grammar + launch policy. Madmax authority normalization
stays in ``omg_cli.madmax``.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from omg_cli import madmax as madmax_mod
from omg_cli.madmax import MadmaxUsageError

END_OF_OPTIONS = "--"
DIRECT_FLAG = "--direct"
TMUX_FLAG = "--tmux"
LAUNCHER_ONLY_FLAGS = frozenset({DIRECT_FLAG, TMUX_FLAG, "--madmax"})
POLICY_ENV = "OMG_LAUNCH_POLICY"
WRAPPER_OWNED_FIRST = frozenset({"-h", "--help", "-V", "--version"})


class HostLaunchUsageError(ValueError):
    """User-facing launcher error."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def split_at_end_of_options(argv: list[str]) -> tuple[list[str], list[str]]:
    if END_OF_OPTIONS not in argv:
        return list(argv), []
    idx = argv.index(END_OF_OPTIONS)
    return argv[:idx], argv[idx:]  # suffix keeps leading `--`


def policy_from_env(env: dict[str, str] | None = None) -> str | None:
    raw = (env or os.environ).get(POLICY_ENV, "").strip().lower()
    if not raw:
        return None
    if raw == "auto":
        return "auto"
    if raw == "direct":
        return "direct"
    if raw in {"tmux", "detached-tmux"}:
        return "tmux"
    raise HostLaunchUsageError(
        f"omg: invalid {POLICY_ENV}={raw!r} (expected auto|direct|tmux|detached-tmux)"
    )


def resolve_launch_policy(
    argv: list[str],
    env: dict[str, str] | None = None,
) -> tuple[str, list[str], list[str]]:
    head, suffix = split_at_end_of_options(argv)
    policy = policy_from_env(env) or "auto"
    rest: list[str] = []
    for arg in head:
        if arg == DIRECT_FLAG:
            policy = "direct"
            continue
        if arg == TMUX_FLAG:
            policy = "tmux"
            continue
        rest.append(arg)
    return policy, rest, suffix


def reject_launcher_flags_after_subcommand(
    argv: list[str],
    known_subcommands: frozenset[str],
) -> None:
    """GRAM-05: launcher-only flags after a recognized first token → usage/2."""
    head, _suffix = split_at_end_of_options(argv)
    if not head:
        return
    first = head[0]
    if first not in known_subcommands and first not in WRAPPER_OWNED_FIRST:
        return
    for tok in head[1:]:
        if tok in LAUNCHER_ONLY_FLAGS:
            raise HostLaunchUsageError(
                f"omg: E_LAUNCH_USAGE — {tok} is a host launcher flag and cannot "
                f"follow subcommand {first!r}"
            )


def should_host_launch(argv: list[str], known_subcommands: frozenset[str]) -> bool:
    if madmax_mod.has_madmax_flag(argv):
        return False  # handled separately via madmax path
    if not argv:
        return True
    head, _suffix = split_at_end_of_options(argv)
    if not head:
        return True
    first = head[0]
    if first in WRAPPER_OWNED_FIRST:
        return False
    if first in known_subcommands:
        return False
    if first.startswith("-") and first not in {DIRECT_FLAG, TMUX_FLAG}:
        return False
    return True


def _is_interactive_tty() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)() and getattr(sys.stdout, "isatty", lambda: False)())


def _session_exists(name: str) -> bool:
    listed = subprocess.run(
        ["tmux", "has-session", "-t", name],
        check=False,
        capture_output=True,
    )
    return listed.returncode == 0


def _run_in_tmux(cwd: Path, grok_args: list[str], *, required: bool, label: str) -> int:
    if not madmax_mod.tmux_available():
        if required:
            print(
                f"{label}: E_LAUNCH_TMUX_UNAVAILABLE — tmux requested but not installed "
                "(brew install tmux)",
                file=sys.stderr,
            )
            return 1
        print(f"{label}: tmux unavailable; falling back to direct launch", file=sys.stderr)
        return madmax_mod._run_grok_direct(cwd, grok_args)

    if required and not _is_interactive_tty() and not madmax_mod._inside_tmux():
        print(
            f"{label}: E_LAUNCH_TTY_REQUIRED — explicit --tmux needs a TTY outside tmux",
            file=sys.stderr,
        )
        return 1

    digest = madmax_mod.cwd_digest(cwd)
    prev: list[str] = []
    listed = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode == 0:
        prev = [
            line.strip()
            for line in listed.stdout.splitlines()
            if "omg-" in line and digest in line
        ]
    if prev:
        print(
            f"{label}: previous sessions (tmux attach -t <name>): "
            + ", ".join(prev[:5])
            + (" …" if len(prev) > 5 else ""),
            file=sys.stderr,
        )

    name = madmax_mod.session_name_for_cwd(cwd)
    # LIFE-01: capture pane exit when the session dies; detach-while-alive keeps attach rc.
    exit_path = Path(tempfile.gettempdir()) / f"omg-host-exit-{os.getpid()}-{name}.code"
    try:
        exit_path.unlink(missing_ok=True)
    except OSError:
        pass
    shell = os.environ.get("SHELL") or "/bin/zsh"
    drain = "perl -e 'use POSIX; tcflush(0, TCIFLUSH)' 2>/dev/null; "
    # Do not `exec` grok so we can record the pane exit code after it returns.
    inner_body = (
        f"sleep 0.2; {drain}grok {shlex.join(grok_args)}; "
        f"ec=$?; printf '%s' \"$ec\" > {shlex.quote(str(exit_path))}; exit \"$ec\""
    )
    pane = f"exec {shlex.quote(shell)} -lc {shlex.quote(inner_body)}"
    env_args = madmax_mod.tmux_env_args(madmax_mod.forwarded_env())
    create = subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(cwd), *env_args, pane],
        check=False,
    )
    if create.returncode != 0:
        print(
            f"{label}: failed to create tmux session {name!r} (exit {create.returncode})",
            file=sys.stderr,
        )
        return 1
    subprocess.run(
        ["tmux", "set-option", "-t", name, "mouse", "on"],
        check=False,
        capture_output=True,
    )
    print(
        f"{label}: created detached session {name}; attaching "
        f"(reattach: tmux attach -t {name})",
        file=sys.stderr,
    )
    attach = subprocess.run(["tmux", "attach-session", "-t", name], check=False)
    attach_rc = int(attach.returncode)
    host_rc: int | None = None
    if exit_path.is_file():
        try:
            host_rc = int(exit_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            host_rc = None
    if attach_rc != 0:
        # Never mask a failed attach with a recorded 0 (LIFE-01).
        if host_rc is not None and host_rc != 0:
            return host_rc
        return attach_rc
    if host_rc is not None:
        return host_rc
    if _session_exists(name):
        return attach_rc
    return 1


def launch_grok(
    cwd: Path | str,
    grok_args: list[str],
    *,
    policy: str,
    label: str,
) -> int:
    root = Path(cwd).resolve()
    if not madmax_mod.grok_available():
        print(
            f"{label}: grok not on PATH. "
            "Install: curl -fsSL https://x.ai/cli/install.sh | bash",
            file=sys.stderr,
        )
        return 127

    print(f"{label}: grok {shlex.join(grok_args) or '(no args)'}", file=sys.stderr)

    # POL-02: already inside tmux → always direct (even if --tmux was supplied).
    if madmax_mod._inside_tmux() or policy == "direct":
        return madmax_mod._run_grok_direct(root, grok_args)
    # POL-03: native Windows auto → direct.
    if policy == "auto" and sys.platform == "win32":
        return madmax_mod._run_grok_direct(root, grok_args)
    # POL-05: explicit tmux is strict before print/headless shortcuts.
    if policy == "tmux":
        return _run_in_tmux(root, grok_args, required=True, label=label)
    if madmax_mod.is_print_mode(grok_args):
        return madmax_mod._run_grok_direct(root, grok_args)
    if policy == "auto" and not _is_interactive_tty():
        return madmax_mod._run_grok_direct(root, grok_args)
    return _run_in_tmux(root, grok_args, required=False, label=label)


def run_interactive(cwd: Path | str | None, argv: list[str]) -> int:
    root = Path(cwd) if cwd is not None else Path.cwd()
    try:
        policy, rest, suffix = resolve_launch_policy(argv)
    except HostLaunchUsageError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    grok_args = [*rest, *suffix]
    return launch_grok(root, grok_args, policy=policy, label="omg")


def run_madmax_host(cwd: Path | str | None, argv: list[str]) -> int:
    root = Path(cwd) if cwd is not None else Path.cwd()
    try:
        policy, rest, suffix = resolve_launch_policy(argv)
        if not madmax_mod.has_madmax_flag(rest):
            rest = ["--madmax", *rest]
        grok_args = [*madmax_mod.normalize_grok_args(rest), *suffix]
    except (HostLaunchUsageError, MadmaxUsageError) as exc:
        print(str(exc), file=sys.stderr)
        code = getattr(exc, "exit_code", 2)
        return int(code)
    return launch_grok(root, grok_args, policy=policy, label="omg madmax")
