#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_event, ensure_omg_dirs, read_hook_event


def main() -> None:
    root = ensure_omg_dirs()
    ev = read_hook_event()
    append_event(
        root,
        {"event": "SubagentStop", "status": "ok", "raw_keys": list(ev.keys())[:20]},
    )


if __name__ == "__main__":
    main()
