#!/usr/bin/env python3
"""#147 fixture: own the controlling TTY and echo only after a TTY read.

Never treats local terminal echo as provider consumption. Prints
``TUI_READY:<nonce>`` then ``PROVIDER_ECHO:<line>`` only after reading a
complete line from the tty (echo disabled when termios is available).
"""
from __future__ import annotations

import os
import signal
import sys
import time

_HOLD = float(os.environ.get("OMG_TEAM_PROVIDER_HOLD_S") or "30")
_NONCE = os.environ.get("OMG_TEAM_INTERACTIVE_NONCE") or "fixture"


def _open_tty():
    if sys.stdin.isatty():
        return sys.stdin
    try:
        return open("/dev/tty", "r", encoding="utf-8", errors="replace")
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


def main() -> int:
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

    tty = _open_tty()
    fd = getattr(tty, "fileno", lambda: 0)()
    old = _disable_echo(fd)
    try:
        print(f"TUI_READY:{_NONCE}", flush=True)
        deadline = time.monotonic() + max(1.0, _HOLD)
        while time.monotonic() < deadline:
            line = tty.readline()
            if not line:
                time.sleep(0.05)
                continue
            payload = line.rstrip("\r\n")
            print(f"PROVIDER_ECHO:{payload}", flush=True)
            print(
                f"WINCH:{winsize['sigwinch']}:{winsize['rows']}x{winsize['cols']}",
                flush=True,
            )
            break
        else:
            print("E_FIXTURE_TIMEOUT", flush=True)
            return 1
        return 0
    finally:
        _restore_echo(fd, old)
        if tty is not sys.stdin:
            tty.close()


if __name__ == "__main__":
    raise SystemExit(main())
