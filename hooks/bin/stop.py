#!/usr/bin/env python3
"""Stop hook: record session stop only. NEVER marks runs verified."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_event, ensure_omg_dirs, read_hook_event


def main() -> None:
    try:
        root = ensure_omg_dirs()
        ev = read_hook_event()
        # CRITICAL: never set verified / acceptance status here — omg CLI is sole writer.
        append_event(
            root,
            {"event": "Stop", "status": "ok", "raw_keys": list(ev.keys())[:20]},
        )
    except Exception:
        # Fail-open: never crash Stop on I/O or unexpected errors
        sys.exit(0)


if __name__ == "__main__":
    main()
