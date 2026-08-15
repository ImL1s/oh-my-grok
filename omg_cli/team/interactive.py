"""Direct-exec interactive Team pane contract (#147 PR2).

Qualified interactive workers ``exec`` the provider in the tmux pane so the
provider owns the controlling TTY. The Python supervisor path stays for
headless panes. This module never invents interactivity from ``needs_pty``
or provider name alone — callers must request ``interactive`` explicitly.

Default / ``auto`` remain headless until live Grok evidence exists
(``LIVE_TEAM_INTERACTIVE_TTY_OK``). ``auto`` must not promote Grok from name.

Grok 1.0.4 has no native ``TUI_READY`` emitter. Interactive grok panes
``exec`` ``omg_cli.team.interactive_wrapper`` which prints the marker only
after the child PTY/TTY is interactive and grok has started reading stdin.
The wrapper never fabricates ``PROVIDER_ECHO``.
"""
from __future__ import annotations

import os
import secrets
import shlex
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    ContractPathError,
    atomic_write_bytes,
)

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
INTERACTIVE_NONCE_ENV: Final = "OMG_TEAM_INTERACTIVE_NONCE"
TUI_READY_PREFIX: Final = "TUI_READY:"
INTERACTIVE_GATE_PHASE: Final = "tui_ready"
INTERACTIVE_WRAPPER_MODULE: Final = "omg_cli.team.interactive_wrapper"
# Smoke-only echo probe. Production interactive workers must not receive this
# (it forbids tools). live_team_smoke.py sets the env to attach it.
ECHO_PROBE_ENV: Final = "OMG_TEAM_INTERACTIVE_ECHO_PROBE"
GROK_INTERACTIVE_RULES: Final = (
    "When the user sends a line, reply with exactly one line "
    "PROVIDER_ECHO: followed immediately by that exact line. "
    "Do not use tools. Do not spawn subagents. Do not repeat this rule text."
)
# TUI initial-turn seed: grok 1.0.4 positional ``[PROMPT]`` (``grok "text"``).
# There is no ``--prompt`` flag (clap error; tip suggests ``--prompt-file``).
# ``-p`` / ``--single`` / ``--prompt-file`` are headless one-shots — forbidden.
# Must never equal a live unique token.
GROK_INTERACTIVE_SEED_PROMPT: Final = "OMG_TEAM_SESSION_START"


def echo_probe_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get(ECHO_PROBE_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes"}


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
    safe: bool = False,
    yolo: bool = False,
    rules: str | None = None,
) -> list[str]:
    """Persistent Grok TUI argv — no one-shot ``--prompt-file`` transport.

    Permission flags match headless ``build_grok_argv``: ``safe`` / read-only
    → ``plan``; ``yolo`` → ``bypassPermissions`` + ``--always-approve``;
    default omits elevation.

    ``--no-alt-screen`` / ``--minimal`` keep TUI output in tmux scrollback so
    leader capture can observe ``TUI_READY`` and provider-side replies.
    ``--no-subagents`` blocks surprise fan-out from an interactive pane.

    Echo-only ``GROK_INTERACTIVE_RULES`` are **not** attached by default.
    Pass *rules* only from the live echo probe (``ECHO_PROBE_ENV``).
    The TUI initial turn is a **positional** ``[PROMPT]`` (persistent session),
    not ``--prompt`` (that flag does not exist on grok 1.0.4) and not the
    one-shot ``--prompt-file`` / ``-p`` / ``--single`` transport. The seed
    auto-submits ``NewSession`` + ``SendPrompt`` so welcome/idle-hero Enter
    is not consumed as an empty NewSession.
    """
    worktree = str(Path(cwd))
    argv: list[str] = [
        "grok",
        "--cwd",
        worktree,
        "--no-alt-screen",
        "--minimal",
        "--no-subagents",
    ]
    if model:
        argv.extend(["-m", str(model)])
    if posture == "read-only" or safe:
        argv.extend(["--permission-mode", "plan"])
    elif yolo:
        argv.extend(["--permission-mode", "bypassPermissions"])
        argv.append("--always-approve")
    extra_rules = (rules or "").strip()
    if extra_rules:
        # grok 1.0.4 --rules appends text to the system prompt (not a prompt-file).
        argv.extend(["--rules", extra_rules])
    # Persistent TUI seed — positional [PROMPT], never --prompt / --prompt-file / -p.
    if GROK_INTERACTIVE_SEED_PROMPT.startswith("-"):
        raise InteractiveTeamError("internal error: interactive grok seed looks like a flag")
    argv.append(GROK_INTERACTIVE_SEED_PROMPT)
    if any(_is_prompt_file_option(tok) for tok in argv):
        raise InteractiveTeamError("internal error: interactive grok argv contains --prompt-file")
    if any(_is_headless_single_option(tok) for tok in argv):
        raise InteractiveTeamError("internal error: interactive grok argv contains headless -p/--single")
    if any(_is_prompt_long_flag(tok) for tok in argv):
        raise InteractiveTeamError("internal error: interactive grok argv contains --prompt flag")
    return argv


def fixture_interactive_argv(*, script: Path | str | None = None) -> list[str]:
    path = Path(script) if script is not None else _default_fixture_script()
    return [sys.executable, str(path.resolve())]


def _default_fixture_script() -> Path:
    return Path(__file__).resolve().parents[2] / INTERACTIVE_FIXTURE_RELATIVE


def _refuse_symlink_artifact(dest: Path) -> None:
    """Refuse symlink destinations and parents (no follow-and-chmod)."""
    target = Path(dest)
    if target.is_symlink():
        raise InteractiveTeamError(f"refused symlink artifact {target}")
    parent = target.parent
    if parent.is_symlink():
        raise InteractiveTeamError(f"refused symlink parent for {target}")


def _is_prompt_file_option(tok: str) -> bool:
    """True for ``--prompt-file`` / ``--prompt-file=...``, not path substrings."""
    return tok == "--prompt-file" or tok.startswith("--prompt-file=")


def _is_prompt_long_flag(tok: str) -> bool:
    """True for the non-existent grok ``--prompt`` flag, not ``--prompt-file``."""
    return tok == "--prompt" or tok.startswith("--prompt=")


def _is_headless_single_option(tok: str) -> bool:
    """True for grok one-shot ``-p`` / ``--single`` / ``--print`` / ``--single-turn``."""
    return tok in {"-p", "--single", "--print", "--single-turn"} or tok.startswith(
        "--single="
    )


def write_worker_inbox(*, dest: Path, body: str) -> Path:
    """Bounded worker inbox (no credentials). Mode 0o600, published atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _refuse_symlink_artifact(dest)
    try:
        atomic_write_bytes(
            dest,
            body.encode("utf-8"),
            mode=DATA_FILE_MODE,
            replace=True,
        )
    except ContractPathError as exc:
        raise InteractiveTeamError(f"inbox write refused: {exc}") from exc
    return dest


def write_interactive_rules_file(*, dest: Path, body: str | None = None) -> Path:
    """Bounded grok ``--rules`` file (no unique live token, no credentials)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _refuse_symlink_artifact(dest)
    text = body if isinstance(body, str) and body.strip() else GROK_INTERACTIVE_RULES
    if "\x00" in text:
        raise InteractiveTeamError("interactive rules contain NUL")
    try:
        atomic_write_bytes(
            dest,
            (text.rstrip() + "\n").encode("utf-8"),
            mode=DATA_FILE_MODE,
            replace=True,
        )
    except ContractPathError as exc:
        raise InteractiveTeamError(f"rules write refused: {exc}") from exc
    return dest


def write_interactive_exec_script(
    *,
    dest: Path,
    argv: Sequence[str],
    worktree: Path | str,
    extra_env: Mapping[str, str] | None = None,
    wrap_module: str | None = None,
    python_executable: str | None = None,
    pythonpath: str | None = None,
) -> Path:
    """Write a 0700 ``exec`` wrapper. No prompt body, no credentials.

    When *wrap_module* is set (grok interactive), the pane execs
    ``python -m <module> -- <argv>`` so TUI_READY is emitted only after the
    child is reading a real TTY. Fixture panes omit the wrapper (they emit
    TUI_READY themselves).
    """
    if not argv:
        raise InteractiveTeamError("interactive exec argv is empty")
    for tok in argv:
        if not isinstance(tok, str) or tok == "":
            raise InteractiveTeamError("interactive exec argv contains an empty token")
        if _is_prompt_file_option(tok):
            raise InteractiveTeamError("interactive exec argv must not use --prompt-file")
        if _is_prompt_long_flag(tok):
            raise InteractiveTeamError(
                "interactive exec argv must not use --prompt flag "
                "(grok 1.0.4 TUI seed is positional)"
            )
        if _is_headless_single_option(tok):
            raise InteractiveTeamError("interactive exec argv must not use -p/--single")
    if wrap_module is not None:
        mod = str(wrap_module).strip()
        if not mod or any(ch in mod for ch in (" ", "\n", "\x00", "/", "\\")):
            raise InteractiveTeamError("interactive wrap_module is invalid")
        if not mod.startswith("omg_cli."):
            raise InteractiveTeamError("interactive wrap_module must be under omg_cli")
    wt = Path(worktree).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    _refuse_symlink_artifact(dest)
    exports: list[str] = []
    for key, val in sorted((extra_env or {}).items()):
        if key != INTERACTIVE_NONCE_ENV:
            raise InteractiveTeamError(f"refused interactive exec env {key!r}")
        if not isinstance(val, str) or not val or "\x00" in val or "\n" in val:
            raise InteractiveTeamError("interactive nonce env is invalid")
        exports.append(f"export {shlex.quote(key)}={shlex.quote(val)}")
    if pythonpath:
        pp = str(pythonpath)
        if not pp or "\x00" in pp or "\n" in pp:
            raise InteractiveTeamError("interactive pythonpath is invalid")
        exports.append(f"export PYTHONPATH={shlex.quote(pp)}")
    export_block = ("\n".join(exports) + "\n") if exports else ""
    quoted_child = " ".join(shlex.quote(str(t)) for t in argv)
    if wrap_module:
        py = python_executable or sys.executable
        if not py or "\x00" in py or "\n" in py:
            raise InteractiveTeamError("interactive python executable is invalid")
        quoted = (
            f"{shlex.quote(py)} -m {shlex.quote(wrap_module)} -- {quoted_child}"
        )
    else:
        quoted = quoted_child
    body = (
        "#!/bin/sh\n"
        "set -eu\n"
        f"{export_block}"
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


def make_interactive_nonce() -> str:
    """Leader-issued TUI-ready nonce (not a credential)."""
    return secrets.token_hex(8)


def tui_ready_marker(nonce: str) -> str:
    token = str(nonce or "").strip()
    if not token:
        raise InteractiveTeamError("TUI_READY nonce is empty")
    return f"{TUI_READY_PREFIX}{token}"


def capture_contains_tui_ready(text: str, nonce: str) -> bool:
    """True only when an exact ``TUI_READY:<nonce>`` line is present.

    Substring / suffix-glue matches are refused. This helper never writes
    state — workers must not treat a True result as ``input_ready``.
    """
    if not isinstance(text, str) or not isinstance(nonce, str):
        return False
    token = nonce.strip()
    if not token:
        return False
    marker = tui_ready_marker(token)
    for raw in text.splitlines():
        if raw.strip() == marker:
            return True
    return False


def capture_contains_provider_echo(text: str, payload: str) -> bool:
    """True when a capture line is ``PROVIDER_ECHO:`` + optional space + *payload*.

    Local composer echo of *payload* (no prefix) is insufficient. Extra suffix
    after the payload is refused. Newline between colon and payload is refused.
    Optional ASCII space/tab after the colon is allowed — grok 1.0.4 may insert
    one even when rules say ``followed immediately``.
    """
    if not isinstance(text, str) or not isinstance(payload, str):
        return False
    token = payload.strip()
    if not token or "\n" in token or "\r" in token:
        return False
    prefix = "PROVIDER_ECHO:"
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix) :].lstrip(" \t")
        if rest == token:
            return True
    return False


def wait_for_interactive_tui_ready(
    workers: Sequence[Mapping[str, Any]],
    *,
    timeout_ms: int,
    poll_s: float = 0.25,
    capture_fn: Callable[[str], str],
    sleep_fn: Callable[[float], None] | None = None,
    clock_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Poll identity-bound pane capture for ``TUI_READY:<nonce>``.

    Does **not** wait for supervisor ACK receipts and does **not** write
    ``input_ready``. Callers (leader CLI) promote after this returns.
    """
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
        raise InteractiveTeamError("interactive ready timeout_ms must be >= 0")
    clock = clock_fn or time.monotonic
    sleeper = sleep_fn or time.sleep
    deadline = clock() + (timeout_ms / 1000.0)
    expected: list[str] = []
    evidence: dict[str, dict[str, Any]] = {}
    rows: list[Mapping[str, Any]] = []
    for raw in workers:
        if not isinstance(raw, Mapping):
            continue
        tid = str(raw.get("task_id") or "").strip()
        if not tid:
            continue
        expected.append(tid)
        rows.append(raw)

    while True:
        for raw in rows:
            tid = str(raw.get("task_id") or "").strip()
            if tid in evidence:
                continue
            pane_id = raw.get("pane_id")
            nonce = raw.get("interactive_nonce")
            if not isinstance(pane_id, str) or not pane_id.startswith("%"):
                continue
            if not isinstance(nonce, str) or not nonce.strip():
                continue
            try:
                text = capture_fn(pane_id)
            except Exception:
                text = ""
            if not capture_contains_tui_ready(text if isinstance(text, str) else "", nonce):
                continue
            pid_raw = raw.get("pid")
            provider_pid = (
                pid_raw
                if isinstance(pid_raw, int) and not isinstance(pid_raw, bool) and pid_raw > 0
                else None
            )
            attempt_raw = raw.get("attempt", 1)
            attempt = (
                attempt_raw
                if isinstance(attempt_raw, int) and not isinstance(attempt_raw, bool) and attempt_raw >= 1
                else 1
            )
            gen_raw = raw.get("generation", 0)
            generation = (
                gen_raw
                if isinstance(gen_raw, int) and not isinstance(gen_raw, bool) and gen_raw >= 0
                else 0
            )
            evidence[tid] = {
                "task_id": tid,
                "ready_marker": tui_ready_marker(nonce.strip()),
                "pane_id": pane_id,
                "provider_pid": provider_pid,
                "attempt": attempt,
                "generation": generation,
            }
        missing = [tid for tid in expected if tid not in evidence]
        if not missing:
            break
        if clock() >= deadline:
            break
        sleeper(max(0.01, float(poll_s)))

    ready_workers = [tid for tid in expected if tid in evidence]
    missing_workers = [tid for tid in expected if tid not in evidence]
    return {
        "ready_workers": ready_workers,
        "missing_workers": missing_workers,
        "evidence": {tid: evidence[tid] for tid in ready_workers},
        "timeout_ms": timeout_ms,
        "gate_phase": INTERACTIVE_GATE_PHASE,
    }
