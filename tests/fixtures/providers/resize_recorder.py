#!/usr/bin/env python3
"""Record SIGWINCH / PTY size changes after READY."""

from __future__ import annotations

import os
import signal
import sys
import time

_HOLD = float(os.environ.get("OMG_TEAM_PROVIDER_HOLD_S") or "30")
_LOG = os.environ.get("OMG_TEAM_RESIZE_LOG") or ""


def _record(msg: str) -> None:
    line = f"{time.time():.3f} {msg}\n"
    if _LOG:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    sys.stdout.write(line)
    sys.stdout.flush()


def _on_winch(_signum: int, _frame: object | None) -> None:
    try:
        rows, cols = os.get_terminal_size(0)
        _record(f"SIGWINCH rows={rows} cols={cols}")
    except OSError:
        _record("SIGWINCH size=unknown")


def main() -> int:
    signal.signal(signal.SIGWINCH, _on_winch)
    print("TEAM_PROVIDER_READY_OK", flush=True)
    try:
        rows, cols = os.get_terminal_size(0)
        _record(f"start rows={rows} cols={cols}")
    except OSError:
        _record("start size=unknown")
    if _HOLD > 0:
        time.sleep(_HOLD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
