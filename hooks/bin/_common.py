# hooks/bin/_common.py
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def hook_disabled(name: str, env: dict | None = None) -> bool:
    """True if OMG hooks are globally disabled or this hook name is skipped.

    DISABLE_OMG in {"1","true","yes","on"} (case-insensitive) -> all hooks off.
    OMG_SKIP_HOOKS is a comma/space-separated list of logical hook names; if
    `name` matches any entry (case-insensitive, trimmed) -> this hook off.
    """
    e = env if env is not None else os.environ
    flag = str(e.get("DISABLE_OMG", "")).strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    raw = str(e.get("OMG_SKIP_HOOKS", ""))
    skip = {t.strip().lower() for chunk in raw.split(",") for t in chunk.split()}
    return name.strip().lower() in skip if name else False


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
