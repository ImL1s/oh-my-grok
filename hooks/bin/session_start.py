#!/usr/bin/env python3
"""SessionStart: ensure dirs, log event, inject RESUME.md (research R2 pillar 2)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_event, ensure_omg_dirs, read_hook_event, workspace_root


def main() -> None:
    try:
        root = ensure_omg_dirs()
        ev = read_hook_event()
        append_event(
            root,
            {
                "event": "SessionStart",
                "status": "ok",
                "raw_keys": list(ev.keys())[:20],
            },
        )
        # Workspace side-effect: louder resume pack for agents
        try:
            # Prefer installed package; fall back to repo checkout layout
            repo_root = Path(__file__).resolve().parents[2]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from omg_cli.resume import write_resume_md

            path = write_resume_md(workspace_root())
            append_event(
                root,
                {
                    "event": "SessionStart",
                    "status": "resume_md",
                    "path": str(path) if path else None,
                },
            )
        except Exception:
            # Fail-open: resume inject must never block session start
            append_event(
                root,
                {"event": "SessionStart", "status": "resume_md_skipped"},
            )
    except Exception:
        # Fail-open: never crash SessionStart on I/O or unexpected errors
        sys.exit(0)


if __name__ == "__main__":
    main()
