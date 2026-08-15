#!/usr/bin/env python3
"""Fail-closed grok interactive TTY wrapper (#147).

Grok 1.0.4 does not emit ``TUI_READY:<nonce>``. This wrapper is the pane
foreground process: it ``fork``/``exec``s grok onto the **same** controlling
TTY (not a PTY proxy, not the Team supervisor) and prints
``TUI_READY:<nonce>`` **only** when:

1. stdin is a real TTY;
2. the child is alive;
3. the child's fd 0 is that TTY;
4. the child has started waiting on stdin (blocking read on fd 0, or
   poll/select/epoll while sleeping **and** the shared TTY is already
   raw/non-canonical). A poll sleep on some other fd is not enough.
   Zombies are not live: ``kill(pid, 0)`` success plus leftover raw TTY
   must not emit TUI_READY.

It never prints TUI_READY because the child merely spawned. It never
echoes operator bytes (PROVIDER_ECHO must come from the child). If the
child dies before stdin-wait is proven, the wrapper exits nonzero with
no ready marker.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import Final

from omg_cli.team.interactive import INTERACTIVE_NONCE_ENV, TUI_READY_PREFIX

# Linux x86_64 syscall numbers that mean "waiting for I/O".
_X86_64_READ: Final = 0
_X86_64_POLL: Final = 7
_X86_64_PREAD64: Final = 17
_X86_64_READV: Final = 19
_X86_64_SELECT: Final = 23
_X86_64_RECVFROM: Final = 45
_X86_64_EPOLL_WAIT: Final = 232
_X86_64_PSELECT6: Final = 270
_X86_64_PPOLL: Final = 271
_X86_64_EPOLL_PWAIT: Final = 281
_X86_64_EPOLL_PWAIT2: Final = 441
_X86_64_STDIN_WAIT: Final = frozenset(
    {
        _X86_64_READ,
        _X86_64_POLL,
        _X86_64_PREAD64,
        _X86_64_READV,
        _X86_64_SELECT,
        _X86_64_RECVFROM,
        _X86_64_EPOLL_WAIT,
        _X86_64_PSELECT6,
        _X86_64_PPOLL,
        _X86_64_EPOLL_PWAIT,
        _X86_64_EPOLL_PWAIT2,
    }
)
# Linux aarch64 (WSL-on-ARM / native). asm-generic: epoll_pwait=22,
# ppoll=73, pselect6=72. 232/281 are x86_64 epoll_wait/epoll_pwait and
# mean mincore/execveat on aarch64 — do not reuse them.
_AARCH64_READ: Final = 63
_AARCH64_PREAD64: Final = 67
_AARCH64_READV: Final = 65
_AARCH64_PPOLL: Final = 73
_AARCH64_PSELECT6: Final = 72
_AARCH64_EPOLL_PWAIT: Final = 22
_AARCH64_EPOLL_PWAIT2: Final = 441
_AARCH64_STDIN_WAIT: Final = frozenset(
    {
        _AARCH64_READ,
        _AARCH64_PREAD64,
        _AARCH64_READV,
        _AARCH64_PPOLL,
        _AARCH64_PSELECT6,
        _AARCH64_EPOLL_PWAIT,
        _AARCH64_EPOLL_PWAIT2,
    }
)

_READY_HOLD_POLLS: Final = 2
_POLL_S: Final = 0.05
E_WRAPPER_NO_TTY: Final = "E_WRAPPER_NO_TTY"
E_WRAPPER_NO_CHILD: Final = "E_WRAPPER_NO_CHILD"
E_WRAPPER_ARGV: Final = "E_WRAPPER_ARGV"
E_WRAPPER_NONCE: Final = "E_WRAPPER_NONCE"


def _machine() -> str:
    return os.uname().machine.lower() if hasattr(os, "uname") else ""


def _stdin_wait_syscalls() -> frozenset[int]:
    mach = _machine()
    if mach in {"aarch64", "arm64"}:
        return _AARCH64_STDIN_WAIT
    return _X86_64_STDIN_WAIT


def _read_syscalls() -> frozenset[int]:
    mach = _machine()
    if mach in {"aarch64", "arm64"}:
        return frozenset({_AARCH64_READ, _AARCH64_PREAD64, _AARCH64_READV})
    return frozenset({_X86_64_READ, _X86_64_PREAD64, _X86_64_READV})


def controlling_tty_path() -> str | None:
    """Return the stdin TTY path, or None when stdin is not a TTY."""
    try:
        if not sys.stdin.isatty():
            return None
        return os.ttyname(sys.stdin.fileno())
    except OSError:
        return None


def _proc_fd0_target(pid: int) -> str | None:
    link = Path(f"/proc/{pid}/fd/0")
    try:
        return os.readlink(link)
    except OSError:
        return None


def _parse_syscall_line(text: str) -> tuple[int | None, int | None]:
    """Return (syscall_nr, arg0_int) from a ``/proc/*/syscall`` body."""
    line = (text or "").strip().splitlines()[0] if text else ""
    if not line or line in {"running", "blocked"}:
        return None, None
    parts = line.split()
    if not parts:
        return None, None
    try:
        nr = int(parts[0], 0)
    except ValueError:
        return None, None
    arg0: int | None = None
    if len(parts) >= 2:
        try:
            arg0 = int(parts[1], 0)
        except ValueError:
            arg0 = None
    return nr, arg0


def _iter_task_syscall_paths(pid: int) -> list[Path]:
    task_root = Path(f"/proc/{pid}/task")
    paths: list[Path] = [Path(f"/proc/{pid}/syscall")]
    try:
        for entry in task_root.iterdir():
            paths.append(entry / "syscall")
    except OSError:
        pass
    return paths


def child_waiting_on_stdin(pid: int, expected_tty: str) -> bool:
    """True when *pid* is alive, fd0 is *expected_tty*, and stdin wait is proven.

    Never returns True solely because the process exists. Missing ``/proc``
    falls back to TTY raw/non-canonical mode (the child put the shared TTY
    into TUI input mode) **only** when the child is still live (not a zombie).
    poll/select/epoll sleeps are not stdin-wait unless that TTY is already
    raw/non-canonical — syscall args for those calls are userspace pointers,
    so a network/timer poll must not promote the pane.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not expected_tty:
        return False
    if not _process_is_live(pid):
        return False
    fd0 = _proc_fd0_target(pid)
    if fd0 is not None:
        # Accept the exact tty and the common ``/dev/pts/N`` vs ``pts/N`` forms.
        if os.path.normpath(fd0) != os.path.normpath(expected_tty) and not (
            fd0.endswith(expected_tty) or expected_tty.endswith(fd0)
        ):
            return False
        wait_set = _stdin_wait_syscalls()
        read_set = _read_syscalls()
        tty_raw = _tty_in_raw_or_noncanonical()
        for path in _iter_task_syscall_paths(pid):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            nr, arg0 = _parse_syscall_line(raw)
            if nr is None:
                continue
            if nr in read_set and arg0 == 0:
                return True
            if nr in wait_set:
                # poll/select/epoll: require S-state AND TUI input mode.
                # Arg0 is a pointer/epfd, not fd 0; sleeping in poll is not
                # proof the child is waiting on stdin.
                if _proc_state_sleeping(pid) and tty_raw:
                    return True
        return False
    return _tty_in_raw_or_noncanonical()


def _proc_state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # comm can contain spaces/parens; state is the first char after the last ") ".
    rparen = stat.rfind(")")
    if rparen < 0 or rparen + 2 >= len(stat):
        return None
    return stat[rparen + 2 : rparen + 3]


def _proc_state_sleeping(pid: int) -> bool:
    return _proc_state(pid) == "S"


def _process_is_live(pid: int) -> bool:
    """False for dead pids and Linux zombies (``kill(pid, 0)`` still succeeds).

    Missing ``/proc`` (macOS) is unknown, not dead — fall back to ``kill``.
    """
    state = _proc_state(pid)
    if state in {"Z", "X"}:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _tty_in_raw_or_noncanonical() -> bool:
    try:
        import termios
    except ImportError:
        return False
    try:
        attrs = termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, OSError, ValueError):
        return False
    lflag = attrs[3]
    return (lflag & termios.ICANON) == 0


def _emit_tui_ready(nonce: str) -> None:
    marker = f"{TUI_READY_PREFIX}{nonce}"
    # Exact line; capture_contains_tui_ready requires strip() equality.
    sys.stdout.write(marker + "\n")
    sys.stdout.flush()


def _child_alive(pid: int) -> bool:
    """True only for a still-running child. Reaps zombies via WNOHANG."""
    wnohang = getattr(os, "WNOHANG", None)
    if wnohang is not None:
        try:
            waited, _status = os.waitpid(pid, wnohang)
        except OSError:
            waited = 0
        else:
            if waited == pid:
                return False
    return _process_is_live(pid)


def _wait_for_stdin_ready(pid: int, tty: str, *, timeout_s: float | None) -> bool:
    deadline = None if timeout_s is None or timeout_s <= 0 else time.monotonic() + timeout_s
    hits = 0
    while True:
        if not _child_alive(pid):
            return False
        if child_waiting_on_stdin(pid, tty):
            hits += 1
            if hits >= _READY_HOLD_POLLS:
                return True
        else:
            hits = 0
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)


def _forward_signal(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except OSError:
        try:
            os.killpg(pid, signum)
        except OSError:
            pass


def _spawn_same_tty(argv: list[str]) -> int:
    """Fork/exec *argv* on the current TTY; return child pid (process group)."""
    pid = os.fork()
    if pid == 0:
        try:
            os.setpgrp()
        except OSError:
            pass
        try:
            os.execvp(argv[0], argv)
        except OSError:
            os._exit(127)
    try:
        os.setpgid(pid, pid)
    except OSError:
        pass
    try:
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        signal.signal(signal.SIGTTIN, signal.SIG_IGN)
        os.tcsetpgrp(sys.stdin.fileno(), pid)
    except (OSError, ValueError, AttributeError):
        pass
    return pid


def run_wrapper(argv: list[str], *, nonce: str, timeout_s: float | None = None) -> int:
    """Spawn *argv* on this TTY and emit TUI_READY only after stdin wait."""
    if not argv or not argv[0]:
        print(E_WRAPPER_ARGV, file=sys.stderr, flush=True)
        return 2
    token = (nonce or "").strip()
    if not token:
        print(E_WRAPPER_NONCE, file=sys.stderr, flush=True)
        return 2
    tty = controlling_tty_path()
    if tty is None:
        print(E_WRAPPER_NO_TTY, file=sys.stderr, flush=True)
        return 2

    child = _spawn_same_tty(argv)

    def _on_signal(signum: int, _frame: object) -> None:
        _forward_signal(child, signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGWINCH"):
        try:
            signal.signal(signal.SIGWINCH, _on_signal)
        except (ValueError, OSError):
            pass

    ready = _wait_for_stdin_ready(child, tty, timeout_s=timeout_s)
    if not ready or not _process_is_live(child):
        if _child_alive(child):
            _forward_signal(child, signal.SIGTERM)
            try:
                os.waitpid(child, 0)
            except OSError:
                _forward_signal(child, signal.SIGKILL)
        else:
            try:
                os.waitpid(child, 0)
            except OSError:
                pass
        print(E_WRAPPER_NO_CHILD, file=sys.stderr, flush=True)
        return 3

    _emit_tui_ready(token)
    try:
        _pid, status = os.waitpid(child, 0)
    except OSError:
        return 1
    if os.WIFEXITED(status):
        return int(os.WEXITSTATUS(status))
    if os.WIFSIGNALED(status):
        return 128 + int(os.WTERMSIG(status))
    return 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="omg-team-interactive-wrapper")
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=0,
        help="optional stdin-wait timeout (0 = wait until child exits)",
    )
    parser.add_argument("child", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if os.name != "posix":
        print("E_WRAPPER_POSIX", file=sys.stderr, flush=True)
        return 2
    args = _parse_args(argv)
    child = list(args.child or [])
    if child and child[0] == "--":
        child = child[1:]
    nonce = os.environ.get(INTERACTIVE_NONCE_ENV) or ""
    timeout_ms = args.timeout_ms
    env_timeout = os.environ.get("OMG_TEAM_WRAPPER_READY_TIMEOUT_MS")
    if env_timeout not in (None, ""):
        try:
            timeout_ms = int(env_timeout)
        except ValueError:
            timeout_ms = 0
    timeout_s = None if timeout_ms <= 0 else timeout_ms / 1000.0
    return run_wrapper(child, nonce=nonce, timeout_s=timeout_s)


if __name__ == "__main__":
    raise SystemExit(main())
