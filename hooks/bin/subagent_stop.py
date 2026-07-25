#!/usr/bin/env python3
"""SubagentStop: observe-only by policy.

Leaf workers must return to the parent; only the parent Stop gate pins
incomplete autopilot. Never emit decision:block JSON here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_hook_observation, ensure_omg_dirs, hook_disabled, read_hook_event


def main() -> None:
    if hook_disabled("subagent_stop"):
        return
    try:
        root = ensure_omg_dirs()
        ev = read_hook_event()
        append_hook_observation(root, "SubagentStop", ev)
    except Exception:
        # Fail-open: never crash SubagentStop on I/O or unexpected errors
        sys.exit(0)


if __name__ == "__main__":
    main()
