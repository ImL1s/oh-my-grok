# hooks/bin/_common.py
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def workspace_root() -> Path:
    for key in ("GROK_WORKSPACE_ROOT", "CLAUDE_PROJECT_DIR", "PWD"):
        v = os.environ.get(key)
        if v:
            return Path(v).resolve()
    return Path.cwd().resolve()


def ensure_omg_dirs(root: Path | None = None) -> Path:
    root = root or workspace_root()
    for sub in (
        "state",
        "state/runs",
        "plans",
        "research",
        "handoffs",
        "artifacts",
        "ultragoal",
        "wiki",
    ):
        (root / ".omg" / sub).mkdir(parents=True, exist_ok=True)
    return root


def append_event(root: Path, payload: dict) -> None:
    ensure_omg_dirs(root)
    path = root / ".omg" / "state" / "events.jsonl"
    # Force system fields AFTER payload so callers cannot hijack ts/session_id
    row = {
        **payload,
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": os.environ.get("GROK_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_hook_event() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}
