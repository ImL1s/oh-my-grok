"""``omg --madmax``: full-open Grok host launch (OMC-style break-glass).

Maps to Grok permission bypass flags and **requires** a dedicated **tmux**
session when interactive and outside tmux (all platforms).

Product contract (Fable 5 2026-07-20 + residual cleanup):
- Not a mode FSM; does not touch state/verified/acceptance.
- Root ``--yolo`` remains mode elevation only (not a madmax alias).
- Conflicting ``--permission-mode`` / ``--safe`` → hard error (exit 2).
- New tmux session every launch (timestamp + nonce); continuity via grok --continue.
- Env for tmux panes via ``new-session -e`` (not shell-export in pane argv).
- Workers remain Grok ``spawn_subagent``; no multi-CLI tmux team control plane.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MADMAX_FLAG = "--madmax"
# Closest Grok surface to Claude --dangerously-skip-permissions.
GROK_OPEN_FLAGS: tuple[str, ...] = (
    "--always-approve",
    "--permission-mode",
    "bypassPermissions",
)

# Env keys/prefixes forwarded into tmux panes via ``-e`` (not pane command text).
_ENV_PREFIXES = ("GROK_", "XAI_")
_ENV_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TERM",
        "COLORTERM",
        "SSH_AUTH_SOCK",
    }
)


class MadmaxUsageError(ValueError):
    """User-facing madmax argv/policy error (maps to exit 2)."""


def has_madmax_flag(argv: list[str]) -> bool:
    return MADMAX_FLAG in argv


def is_print_mode(argv: list[str]) -> bool:
    """Headless / stdout modes must not wrap tmux (preserve piping)."""
    for a in argv:
        if a in (
            "-p",
            "--single",
            "--prompt-file",
            "--prompt-json",
            "-h",
            "--help",
            "-V",
            "--version",
        ):
            return True
        if a.startswith("--single=") or a.startswith("--prompt-file=") or a.startswith(
            "--prompt-json="
        ):
            return True
    return False


def strip_madmax_flags(argv: list[str]) -> list[str]:
    return [a for a in argv if a != MADMAX_FLAG]


def normalize_grok_args(argv: list[str]) -> list[str]:
    """Validate + strip madmax; ensure exactly one open-permission pair.

    Raises MadmaxUsageError on --safe or conflicting --permission-mode.
    Strips root-only --yolo with stderr note (madmax is a strict superset).
    """
    notes: list[str] = []
    stripped: list[str] = []
    for a in argv:
        if a == MADMAX_FLAG:
            continue
        if a == "--yolo":
            notes.append(
                "omg madmax: ignoring --yolo (madmax is already full-open; "
                "--yolo is for mode subcommands only)"
            )
            continue
        if a == "--safe":
            raise MadmaxUsageError(
                "omg madmax: --safe contradicts full-open host launch"
            )
        stripped.append(a)

    for n in notes:
        print(n, file=sys.stderr)

    out: list[str] = []
    i = 0
    user_permission_mode: str | None = None
    while i < len(stripped):
        a = stripped[i]
        # Drop user --always-approve copies; we inject exactly one in prefix.
        if a == "--always-approve":
            i += 1
            continue
        if a == "--permission-mode":
            mode = stripped[i + 1] if i + 1 < len(stripped) else ""
            if not mode or mode.startswith("-"):
                raise MadmaxUsageError(
                    "omg madmax: --permission-mode requires a value"
                )
            if user_permission_mode is not None and mode != user_permission_mode:
                raise MadmaxUsageError(
                    "omg madmax: conflicting --permission-mode values"
                )
            user_permission_mode = mode
            i += 2
            continue
        if a.startswith("--permission-mode="):
            mode = a.split("=", 1)[1]
            if user_permission_mode is not None and mode != user_permission_mode:
                raise MadmaxUsageError(
                    "omg madmax: conflicting --permission-mode values"
                )
            user_permission_mode = mode
            i += 1
            continue
        out.append(a)
        i += 1

    if user_permission_mode is not None and user_permission_mode != "bypassPermissions":
        raise MadmaxUsageError(
            "omg madmax: --permission-mode must be bypassPermissions "
            f"(got {user_permission_mode!r}); omit the flag to use full-open defaults"
        )

    # Exactly one open-flag pair at front.
    return ["--always-approve", "--permission-mode", "bypassPermissions", *out]


def session_name_for_cwd(
    cwd: Path | str,
    *,
    now: datetime | None = None,
    nonce: str | None = None,
) -> str:
    """Unique per launch: omg-<base>-<digest8>-<UTC ts>-<nonce4>."""
    resolved = str(Path(cwd).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    base = Path(resolved).name or "root"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in base)[:20]
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    # Nonce avoids same-second collision (P3).
    token = (nonce if nonce is not None else secrets.token_hex(2)).lower()
    return f"omg-{safe}-{digest}-{ts}-{token}"


def cwd_digest(cwd: Path | str) -> str:
    resolved = str(Path(cwd).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def grok_available() -> bool:
    return shutil.which("grok") is not None


def _inside_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def forwarded_env() -> list[tuple[str, str]]:
    """Allowlisted env pairs for tmux ``-e`` injection."""
    pairs: list[tuple[str, str]] = []
    for key, val in os.environ.items():
        if key in _ENV_EXACT or any(key.startswith(p) for p in _ENV_PREFIXES):
            pairs.append((key, val))
    pairs.sort(key=lambda kv: kv[0])
    return pairs


def build_pane_command(
    grok_args: list[str],
    *,
    shell: str | None = None,
    da1_drain: bool = True,
) -> str:
    """Login-shell wrapped pane command (no secret exports in command text).

    Env is passed via ``tmux new-session -e`` so values are not embedded in the
    pane start-command string visible to ``ps``.
    """
    shell = shell or os.environ.get("SHELL") or "/bin/zsh"
    # Optional DA1 drain (OMC parity): terminal Device Attributes reply can
    # land in the pty before Grok TUI reads input.
    drain = (
        "perl -e 'use POSIX; tcflush(0, TCIFLUSH)' 2>/dev/null; "
        if da1_drain
        else ""
    )
    inner_body = f"sleep 0.2; {drain}exec {shlex.join(['grok', *grok_args])}"
    return f"exec {shlex.quote(shell)} -lc {shlex.quote(inner_body)}"


def tmux_env_args(env_pairs: list[tuple[str, str]] | None = None) -> list[str]:
    """Build repeated ``-e KEY=value`` args for ``tmux new-session``."""
    pairs = env_pairs if env_pairs is not None else forwarded_env()
    out: list[str] = []
    for key, val in pairs:
        # tmux -e takes VARIABLE=value; reject keys that would break parsing.
        if not key or "=" in key or "\x00" in key or "\x00" in val:
            continue
        out.extend(["-e", f"{key}={val}"])
    return out


def _list_previous_sessions(digest: str) -> list[str]:
    if not tmux_available():
        return []
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []
    names = []
    needle = f"-{digest}-"
    for line in (r.stdout or "").splitlines():
        name = line.strip()
        if name.startswith("omg-") and needle in name:
            names.append(name)
    return names


def _run_grok_direct(cwd: Path, grok_args: list[str]) -> int:
    """Replace process with grok when possible (clean signals for TUI)."""
    cmd = ["grok", *grok_args]
    try:
        os.chdir(str(cwd))
        os.execvp("grok", cmd)
    except FileNotFoundError:
        print("omg madmax: grok not on PATH", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"omg madmax: failed to exec grok: {exc}", file=sys.stderr)
        return 1
    return 1  # unreachable if exec succeeds


def _run_grok_in_tmux(cwd: Path, grok_args: list[str]) -> int:
    if not tmux_available():
        print(
            "omg madmax: tmux is required but not installed.\n"
            "  Install: brew install tmux",
            file=sys.stderr,
        )
        return 1

    digest = cwd_digest(cwd)
    prev = _list_previous_sessions(digest)
    if prev:
        print(
            "omg madmax: previous sessions for this directory "
            f"(attach with tmux attach -t <name>): {', '.join(prev[:5])}"
            + (" …" if len(prev) > 5 else ""),
            file=sys.stderr,
        )

    name = session_name_for_cwd(cwd)
    pane = build_pane_command(grok_args)
    env_args = tmux_env_args()
    create = subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            str(cwd),
            *env_args,
            pane,
        ],
        check=False,
    )
    if create.returncode != 0:
        print(
            f"omg madmax: failed to create tmux session {name!r} "
            f"(exit {create.returncode})",
            file=sys.stderr,
        )
        return 1
    subprocess.run(
        ["tmux", "set-option", "-t", name, "mouse", "on"],
        check=False,
        capture_output=True,
    )
    print(f"omg madmax: attaching tmux session {name}", file=sys.stderr)
    attach = subprocess.run(["tmux", "attach-session", "-t", name], check=False)
    return int(attach.returncode)


def run_madmax(cwd: Path | str | None, argv: list[str]) -> int:
    """Launch Grok full-open; tmux required when interactive and outside tmux."""
    root = Path(cwd) if cwd is not None else Path.cwd()
    root = root.resolve()
    try:
        grok_args = normalize_grok_args(argv)
    except MadmaxUsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not grok_available():
        print(
            "omg madmax: grok not on PATH. "
            "Install: curl -fsSL https://x.ai/cli/install.sh | bash",
            file=sys.stderr,
        )
        return 127

    # Honest banner: actual final argv, not a constant slogan only.
    print(
        f"omg madmax: full-open host launch → grok {shlex.join(grok_args)}",
        file=sys.stderr,
    )

    if is_print_mode(grok_args):
        return _run_grok_direct(root, grok_args)

    if _inside_tmux():
        return _run_grok_direct(root, grok_args)

    return _run_grok_in_tmux(root, grok_args)


def should_host_launch(argv: list[str], known_subcommands: frozenset[str]) -> bool:
    """OMX-aligned: bare / prompt argv launches interactive Grok (not madmax)."""
    if has_madmax_flag(argv):
        return False
    if not argv:
        return True
    head = argv[0]
    if head in {"-h", "--help", "-V", "--version"}:
        return False
    if head in known_subcommands:
        return False
    # Global CLI flags (--safe/--yolo/…) stay with argparse.
    if head.startswith("-"):
        return False
    return True


def run_interactive(cwd: Path | str | None, argv: list[str]) -> int:
    """Launch interactive Grok without permission bypass (OMX bare-entry analogue)."""
    root = Path(cwd) if cwd is not None else Path.cwd()
    root = root.resolve()
    if not grok_available():
        print(
            "omg: grok not on PATH. "
            "Install: curl -fsSL https://x.ai/cli/install.sh | bash",
            file=sys.stderr,
        )
        return 127
    grok_args = list(argv)
    print(f"omg: interactive host launch → grok {shlex.join(grok_args) or '(no args)'}", file=sys.stderr)
    if is_print_mode(grok_args):
        return _run_grok_direct(root, grok_args)
    if _inside_tmux():
        return _run_grok_direct(root, grok_args)
    if not tmux_available():
        print("omg: tmux unavailable; falling back to direct launch", file=sys.stderr)
        return _run_grok_direct(root, grok_args)
    return _run_grok_in_tmux(root, grok_args)
