"""Versioned CLI JSON envelopes (#30 Phase 1).

See ``docs/cli-contract.md``. Handlers may still print legacy human text by
default; when ``CommandContext.wants_json`` is true, prefer these helpers.
"""

from __future__ import annotations

import json
import sys
from typing import Any

SCHEMA_VERSION = 1


def success(command: str, **data: Any) -> dict[str, Any]:
    """Build a success envelope (domain fields at top level for compat)."""
    out: dict[str, Any] = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "command": command,
    }
    out.update(data)
    return out


def failure(
    command: str,
    code: str,
    message: str,
    *,
    next_action: str | None = None,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    """Build a failure envelope with nested error object."""
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": bool(retryable),
    }
    if details:
        err["details"] = details
    if next_action:
        err["next_action"] = next_action
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "error": err,
        # Flat code for simple jq filters during migration
        "error_code": code,
        "message": message,
    }


def emit_json(payload: dict[str, Any], *, stream=None) -> None:
    """Print one JSON document to stdout (or stream). Never mix prose."""
    out = stream if stream is not None else sys.stdout
    print(json.dumps(payload, indent=2, ensure_ascii=False), file=out)


def exit_for_ok(ok: bool, *, usage: bool = False) -> int:
    """Map outcome to contract exit classes."""
    if usage:
        return 2
    return 0 if ok else 1


__all__ = [
    "SCHEMA_VERSION",
    "emit_json",
    "exit_for_ok",
    "failure",
    "success",
]
