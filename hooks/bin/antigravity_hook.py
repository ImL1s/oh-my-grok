#!/usr/bin/env python3
"""Antigravity lifecycle adapter for the installed OMG plugin.

Antigravity sends camelCase protojson payloads and expects host-specific JSON
results.  This adapter translates only the events Antigravity documents.  It
does not read transcripts, does not implement a fictional UserPromptSubmit,
and never writes ``passes`` or ``verified``.  Diagnostics are bounded by the
existing redacted lifecycle journal and fail open if the journal is unavailable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_ROOT))

from omg_cli.hooks_registry import dispatch  # noqa: E402


MAX_INPUT_CHARS = 1_048_576
JOURNAL_SOURCE = "antigravity-hook"
SUPPORTED_EVENTS = frozenset({"PreToolUse", "PostToolUse", "Stop"})


def _read_payload() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read(MAX_INPUT_CHARS + 1)
        if len(raw) > MAX_INPUT_CHARS:
            return None
        payload = json.loads(raw) if raw.strip() else {}
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _workspace(payload: Mapping[str, Any]) -> Path | None:
    paths = payload.get("workspacePaths")
    if not isinstance(paths, list) or not paths or not isinstance(paths[0], str):
        return None
    try:
        root = Path(paths[0]).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return root if root.is_dir() else None


def _identity_fields(payload: Mapping[str, Any], root: Path) -> dict[str, Any]:
    conversation = payload.get("conversationId")
    body: dict[str, Any] = {"root": str(root)}
    if isinstance(conversation, str) and conversation:
        body["session_id"] = conversation
    step = payload.get("stepIdx")
    if isinstance(step, int) and not isinstance(step, bool):
        body["step_idx"] = step
    return body


def _pre_tool_payload(payload: Mapping[str, Any], root: Path) -> dict[str, Any]:
    body = _identity_fields(payload, root)
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return body
    name = tool_call.get("name")
    args = tool_call.get("args")
    if not isinstance(name, str):
        return body
    tool_input = dict(args) if isinstance(args, dict) else {}
    # Antigravity's shell tool is ``run_command`` and its documented argument
    # is ``CommandLine``.  Reuse the existing canonical shell safety gate.
    if name == "run_command":
        name = "run_terminal_command"
        command = tool_input.get("CommandLine")
        if isinstance(command, str):
            tool_input = {"command": command}
        else:
            tool_input = {}
    body["toolName"] = name
    body["toolInput"] = tool_input
    return body


def _result_output(result: Mapping[str, Any], hook_id: str) -> dict[str, Any]:
    for row in result.get("results") or []:
        if isinstance(row, dict) and row.get("id") == hook_id:
            output = row.get("output")
            if isinstance(output, dict):
                return output
    return {}


def _pre_tool(payload: Mapping[str, Any], root: Path) -> dict[str, Any]:
    result = dispatch(
        "tool.pre",
        _pre_tool_payload(payload, root),
        root=PLUGIN_ROOT,
        journal_source=JOURNAL_SOURCE,
    )
    output = _result_output(result, "omg.pretool.deny")
    decision = output.get("decision")
    if decision not in {"allow", "deny"}:
        return {"decision": "allow", "reason": "OMG Antigravity hook failed open"}
    response = {"decision": decision}
    reason = output.get("reason")
    if isinstance(reason, str) and reason:
        response["reason"] = reason
    return response


def _post_tool(payload: Mapping[str, Any], root: Path) -> dict[str, Any]:
    event = "tool.failure" if payload.get("error") else "tool.post"
    dispatch(
        event,
        _identity_fields(payload, root),
        root=PLUGIN_ROOT,
        journal_source=JOURNAL_SOURCE,
    )
    return {}


def _stop(payload: Mapping[str, Any], root: Path) -> dict[str, Any]:
    body = _identity_fields(payload, root)
    if payload.get("terminationReason") == "model_stop":
        body["reason"] = "end_turn"
    if payload.get("fullyIdle") is False:
        body["backgroundTasks"] = ["antigravity-not-idle"]
    result = dispatch(
        "stop.request",
        body,
        root=PLUGIN_ROOT,
        journal_source=JOURNAL_SOURCE,
    )
    output = _result_output(result, "omg.stop.gate")
    if output.get("decision") == "block":
        reason = output.get("reason")
        response: dict[str, Any] = {"decision": "continue"}
        if isinstance(reason, str) and reason:
            response["reason"] = reason
        return response
    return {"decision": "allow"}


def run(event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate one supported event; malformed/unbound input fails open."""
    if event not in SUPPORTED_EVENTS:
        return {}
    root = _workspace(payload)
    if root is None:
        return {"decision": "allow"} if event in {"PreToolUse", "Stop"} else {}
    if event == "PreToolUse":
        return _pre_tool(payload, root)
    if event == "PostToolUse":
        return _post_tool(payload, root)
    return _stop(payload, root)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    event = args[0] if len(args) == 1 else ""
    payload = _read_payload()
    try:
        result = run(event, payload or {})
    except Exception:
        result = {"decision": "allow"} if event in {"PreToolUse", "Stop"} else {}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
