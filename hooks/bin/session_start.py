#!/usr/bin/env python3
"""Passive SessionStart lifecycle observation."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_hook_observation, ensure_omg_dirs, hook_disabled, read_hook_event


def main() -> None:
    if hook_disabled("session_start"):
        return
    try:
        root = ensure_omg_dirs()
        ev = read_hook_event()
        append_hook_observation(root, "SessionStart", ev)
    except Exception:
        # Fail-open: never crash SessionStart on I/O or unexpected errors
        sys.exit(0)


if __name__ == "__main__":
    main()
