#!/usr/bin/env python3
"""Stop hook: record session stop; block while autopilot incomplete (fail-open)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_hook_observation, ensure_omg_dirs, hook_disabled, read_hook_event  # noqa: E402
from omg_cli.stop_gate import decide_stop  # noqa: E402


def main() -> None:
    if hook_disabled("stop"):
        return
    try:
        root = ensure_omg_dirs()
        ev = read_hook_event()
        # CRITICAL: never set verified / acceptance status here — omg CLI is sole writer.
        append_hook_observation(root, "Stop", ev)
        decision = decide_stop(root, ev)
        if decision is not None:
            sys.stdout.write(json.dumps(decision))
        sys.exit(0)
    except Exception:
        # Fail-open: never crash Stop on I/O or unexpected errors
        sys.exit(0)


if __name__ == "__main__":
    main()
