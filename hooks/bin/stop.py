#!/usr/bin/env python3
"""Stop hook: record session stop only. NEVER marks runs verified."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_hook_observation, ensure_omg_dirs, hook_disabled, read_hook_event


def main() -> None:
    if hook_disabled("stop"):
        return
    try:
        root = ensure_omg_dirs()
        ev = read_hook_event()
        # CRITICAL: never set verified / acceptance status here — omg CLI is sole writer.
        append_hook_observation(root, "Stop", ev)
    except Exception:
        # Fail-open: never crash Stop on I/O or unexpected errors
        sys.exit(0)


if __name__ == "__main__":
    main()
