# hooks/bin/_common.py
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from omg_cli.contracts.path_keys import append_locked_jsonl, ensure_managed_dir
from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.redaction import redact_value


MAX_HOOK_INPUT_CHARS = 1_048_576


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
        ensure_managed_dir(root / ".omg" / sub)
    return root


def append_event(root: Path, payload: dict) -> None:
    ensure_omg_dirs(root)
    path = root / ".omg" / "state" / "events.jsonl"
    # Force system fields AFTER payload so callers cannot hijack ts/session_id
    safe_payload = redact_value(payload)
    if not isinstance(safe_payload, dict):
        safe_payload = {"status": "redacted"}
    row = {
        **safe_payload,
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": os.environ.get("GROK_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID"),
    }
    append_locked_jsonl(path, canonical_json_bytes(row))


def append_hook_observation(root: Path, hook_event: str, event: dict) -> None:
    """Persist legacy diagnostics plus one normalized, deduped observation."""

    safe_keys = {
        "host_spawn_id",
        "bound",
        "status",
        "toolName",
        "tool_name",
        "subagent_type",
        "capability_mode",
        "generation",
        "receipt_generation",
        "lease_generation",
        "spawn_receipt_hash",
        "role_receipt_hash",
        "parent_session_id",
    }
    payload = {key: event[key] for key in safe_keys if key in event}
    payload.update(
        {
            "event": hook_event,
            "status": payload.get("status", "ok"),
            "raw_keys": sorted(str(key) for key in event)[:20],
        }
    )
    duplicate = False
    try:
        from omg_cli.runtime_events import append_hook_event

        result = append_hook_event(
            root,
            hook_event=hook_event,
            payload=payload,
            run_id=os.environ.get("OMG_RUN_ID"),
            session_id=os.environ.get("GROK_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID"),
            event_id=str(event.get("event_id") or event.get("hook_event_id") or "") or None,
            observed_at=event.get("observed_at")
            if isinstance(event.get("observed_at"), str)
            else None,
        )
        duplicate = bool(result.get("duplicate"))
    except Exception:
        # Lifecycle normalization is additive diagnostics and never blocks a
        # host hook. The legacy append below remains the local failure trace.
        pass
    if not duplicate:
        append_event(root, payload)


def read_hook_event() -> dict:
    try:
        raw = sys.stdin.read(MAX_HOOK_INPUT_CHARS + 1)
        if len(raw) > MAX_HOOK_INPUT_CHARS:
            return {}
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}
