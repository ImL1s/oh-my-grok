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
        from omg_cli.resume import write_resume_md
        from omg_cli.state import load_active_run

        active = load_active_run(root)
        if active and str(active.get("mode") or "") == "autopilot":
            phase = str(active.get("autopilot_phase") or "")
            if phase not in ("verified", "cancelled"):
                write_resume_md(root, str(active.get("run_id") or "") or None)
    except Exception:
        # Fail-open: never crash SessionStart on I/O or unexpected errors
        sys.exit(0)


if __name__ == "__main__":
    main()
