"""Direct-exec interactive Team pane contract (#147 PR2).

Qualified interactive workers ``exec`` the provider in the tmux pane so the
provider owns the controlling TTY. The Python supervisor path stays for
headless panes. This module never invents interactivity from ``needs_pty``
or provider name alone — callers must request ``interactive`` explicitly.

Default / ``auto`` remain headless until live Grok evidence exists
(``LIVE_TEAM_INTERACTIVE_TTY_OK``). ``auto`` must not promote Grok from name.
"""
from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any, Final, Sequence

REQUESTED_HEADLESS: Final = "headless"
REQUESTED_INTERACTIVE: Final = "interactive"
REQUESTED_AUTO: Final = "auto"
REQUESTED_IO_MODES: Final[frozenset[str]] = frozenset(
    {REQUESTED_HEADLESS, REQUESTED_INTERACTIVE, REQUESTED_AUTO}
)

E_TEAM_IO_MODE_UNSUPPORTED: Final = "E_TEAM_IO_MODE_UNSUPPORTED"

# Providers that may be launched with --io-mode interactive in this slice.
# Grok argv is built without --prompt-file; fixture proves TTY ownership in CI.
QUALIFIED_INTERACTIVE_PROVIDERS: Final[frozenset[str]] = frozenset({"grok", "fixture"})

INTERACTIVE_FIXTURE_RELATIVE: Final = "tests/fixtures/providers/interactive_tty.py"


class InteractiveTeamError(Exception):
    """Fail-closed interactive I/O selection (no silent downgrade)."""

    def __init__(self, message: str, *, code: str = E_TEAM_IO_MODE_UNSUPPORTED) -> None:
        text = message if message.startswith("E_") else f"{code}: {message}"
        super().__init__(text)
        self.code = code


def normalize_requested_io_mode(raw: str | None) -> str:
    """Return a requested I/O mode. Empty/None → headless (current default)."""
    if raw is None:
        return REQUESTED_HEADLESS
    key = str(raw).strip().lower()
    if key == "":
        return REQUESTED_HEADLESS
    if key not in REQUESTED_IO_MODES:
        raise InteractiveTeamError(
            f"unknown --io-mode {raw!r} (want auto|interactive|headless)"
        )
    return key


def resolve_effective_io_mode(
    *,
    requested: str | None,
    worker_topology: str | None,
    provider: str | None,
    executor: str | None = None,
) -> str:
    """Map a request onto headless vs interactive_tty.

    ``auto`` is headless until live Grok qualification exists.
    ``interactive`` fails closed on job topology or unqualified providers.
    """
    req = normalize_requested_io_mode(requested)
    topo = str(worker_topology or "pane").strip().lower()
    if req in {REQUESTED_HEADLESS, REQUESTED_AUTO}:
        return REQUESTED_HEADLESS
    if topo in {"job", "background", "background_job"}:
        raise InteractiveTeamError(
            "interactive TTY is unavailable for job topology; use --io-mode headless"
        )
    prov = str(executor or provider or "grok").strip().lower()
    if prov in {"agy", "antigravity"}:
        prov = "antigravity"
    if prov not in QUALIFIED_INTERACTIVE_PROVIDERS:
        raise InteractiveTeamError(
            f"provider {prov!r} is not qualified for interactive_tty "
            "(explicit --io-mode interactive never silently downgrades)"
        )
    return REQUESTED_INTERACTIVE


def grok_interactive_argv(
    *,
    cwd: Path | str,
    posture: str = "read-write",
    model: str | None = None,
) -> list[str]:
    """Persistent Grok TUI argv — no one-shot ``--prompt-file`` transport."""
    worktree = str(Path(cwd))
    argv: list[str] = ["grok", "--cwd", worktree]
    if model:
        argv.extend(["-m", str(model)])
    if posture == "read-only":
        argv.extend(["--permission-mode", "plan"])
    else:
        argv.extend(["--permission-mode", "bypassPermissions"])
    if "--prompt-file" in argv:
        raise InteractiveTeamError("internal error: interactive grok argv contains --prompt-file")
    return argv


def fixture_interactive_argv(*, script: Path | str | None = None) -> list[str]:
    path = Path(script) if script is not None else _default_fixture_script()
    return [sys.executable, str(path.resolve())]


def _default_fixture_script() -> Path:
    return Path(__file__).resolve().parents[2] / INTERACTIVE_FIXTURE_RELATIVE


def write_worker_inbox(*, dest: Path, body: str) -> Path:
    """Bounded worker inbox (no credentials). Mode 0o600."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def write_interactive_exec_script(
    *,
    dest: Path,
    argv: Sequence[str],
    worktree: Path | str,
) -> Path:
    """Write a 0700 ``exec`` wrapper. No prompt body, no credentials."""
    if not argv:
        raise InteractiveTeamError("interactive exec argv is empty")
    for tok in argv:
        if not isinstance(tok, str) or tok == "":
            raise InteractiveTeamError("interactive exec argv contains an empty token")
        if "--prompt-file" in tok:
            raise InteractiveTeamError("interactive exec argv must not use --prompt-file")
    wt = Path(worktree).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    quoted = " ".join(shlex.quote(str(t)) for t in argv)
    body = (
        "#!/bin/sh\n"
        "set -eu\n"
        f"cd -- {shlex.quote(str(wt))}\n"
        f"exec {quoted}\n"
    )
    dest.write_text(body, encoding="utf-8")
    try:
        os.chmod(dest, stat.S_IRWXU)  # 0o700
    except OSError:
        pass
    return dest


def pane_command_for_exec_script(script: Path | str) -> str:
    """tmux pane command: exec the wrapper so the provider can become PID 1."""
    path = Path(script)
    return f"exec /bin/sh {shlex.quote(str(path))}"


def interactive_inbox_instruction(inbox_path: Path | str) -> str:
    """Concise post-ready instruction — not the full leader transcript."""
    return f"Read {inbox_path} and execute the assigned task now."


def assert_not_supervisor_pane_command(pane_command: str) -> None:
    blob = pane_command.lower()
    if "team supervisor" in blob or "omg_cli.main" in blob:
        raise InteractiveTeamError(
            "interactive pane_command still launches team supervisor"
        )


def public_interactive_facts(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "io_mode": row.get("io_mode"),
        "provider_tty_owner": row.get("provider_tty_owner"),
        "input_ready": row.get("input_ready"),
        "operator_input_supported": row.get("operator_input_supported"),
    }
