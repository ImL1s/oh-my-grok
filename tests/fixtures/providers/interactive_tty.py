#!/usr/bin/env python3
"""#147 fixture: own the controlling TTY and echo only after a TTY read.

Never treats local terminal echo as provider consumption. Prints
``TUI_READY:<nonce>`` then ``PROVIDER_ECHO:<line>`` only after reading a
complete line from the tty (echo disabled when termios is available).

PTY startup junk (stray CR, DA1/CSI) is drained before TUI_READY and
ignored if it arrives as empty/CSI-only lines afterward — otherwise a
leftover newline would consume the hold and the operator payload would
never be echoed (false-red on a live send, or false-green if tests
stopped at TUI_READY).
"""
from __future__ import annotations

import os
import re
import select
import signal
import sys
import time

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def sanitize_tty_payload(raw: str) -> str:
    """Return operator-visible text; empty means terminal chatter, not a line."""
    text = _CSI_RE.sub("", raw)
    kept: list[str] = []
    for ch in text:
        if ch == "\t" or ch.isprintable():
            kept.append(ch)
    return "".join(kept).strip("\r\n")


def _open_tty_fd() -> int:
    if sys.stdin.isatty():
        return os.dup(sys.stdin.fileno())
    try:
        return os.open("/dev/tty", os.O_RDWR)
    except OSError:
        print("E_FIXTURE_NO_TTY", flush=True)
        raise SystemExit(2)


def _disable_echo(fd: int) -> object | None:
    try:
        import termios
    except ImportError:
        return None
    try:
        old = termios.tcgetattr(fd)
        new = list(old)
        new[3] = new[3] & ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        return old
    except (termios.error, OSError):
        return None


def _restore_echo(fd: int, old: object | None) -> None:
    if old is None:
        return
    try:
        import termios

        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (termios.error, OSError, ImportError):
        return


def _drain_tty(fd: int) -> None:
    """Drop unread PTY bytes (DA1 / stray CR) before advertising TUI_READY."""
    try:
        import termios

        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except (termios.error, OSError):
            pass
    except ImportError:
        pass
    try:
        import fcntl
    except ImportError:
        return
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
        except OSError:
            pass
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except OSError:
        return


def _split_complete_lines(buf: bytearray) -> tuple[list[str], bytearray]:
    text = buf.decode("utf-8", errors="replace")
    lines: list[str] = []
    while True:
        cr = text.find("\r")
        lf = text.find("\n")
        if cr < 0 and lf < 0:
            break
        if cr < 0:
            idx = lf
            skip = 1
        elif lf < 0:
            idx = cr
            skip = 1
        elif lf == cr + 1:
            idx = cr
            skip = 2
        else:
            idx = min(cr, lf)
            skip = 1
        lines.append(text[:idx])
        text = text[idx + skip :]
    return lines, bytearray(text.encode("utf-8"))


def _read_operator_line(fd: int, deadline: float) -> str | None:
    """Read until a non-empty sanitized line or *deadline* (monotonic)."""
    buf = bytearray()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([fd], [], [], min(0.25, remaining))
        except (OSError, ValueError):
            time.sleep(min(0.05, remaining))
            continue
        if not ready:
            continue
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            continue
        if not chunk:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            continue
        buf.extend(chunk)
        lines, buf = _split_complete_lines(buf)
        for raw in lines:
            payload = sanitize_tty_payload(raw)
            if payload:
                return payload
    return None


def main() -> int:
    hold = float(os.environ.get("OMG_TEAM_PROVIDER_HOLD_S") or "30")
    nonce = os.environ.get("OMG_TEAM_INTERACTIVE_NONCE") or "fixture"
    winsize = {"rows": 0, "cols": 0, "sigwinch": 0}

    def _on_winch(_signum, _frame) -> None:
        winsize["sigwinch"] += 1
        try:
            import shutil

            size = shutil.get_terminal_size(fallback=(0, 0))
            winsize["cols"] = int(size.columns)
            winsize["rows"] = int(size.lines)
        except OSError:
            pass

    try:
        signal.signal(signal.SIGWINCH, _on_winch)
    except (AttributeError, ValueError, OSError):
        pass

    fd = _open_tty_fd()
    old = _disable_echo(fd)
    try:
        _drain_tty(fd)
        print(f"TUI_READY:{nonce}", flush=True)
        deadline = time.monotonic() + max(1.0, hold)
        payload = _read_operator_line(fd, deadline)
        if payload is None:
            print("E_FIXTURE_TIMEOUT", flush=True)
            return 1
        print(f"PROVIDER_ECHO:{payload}", flush=True)
        print(
            f"WINCH:{winsize['sigwinch']}:{winsize['rows']}x{winsize['cols']}",
            flush=True,
        )
        return 0
    finally:
        _restore_echo(fd, old)
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
