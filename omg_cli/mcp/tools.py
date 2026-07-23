"""Exact, bounded MCP operations exposed by oh-my-grok.

The MCP surface is intentionally smaller than the CLI.  Eight operations are
read-only; ``proposal.create`` may only create an immutable, non-authoritative
proposal below ``.omg/artifacts/mcp-proposals``.  No operation can mutate run
state, acceptance, ``passes`` or ``verified``.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
)
from omg_cli.contracts.state_schemas import ContractValidationError, require_safe_id
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.project_memory import search_memory
from omg_cli.redaction import redact_value
from omg_cli.resume import build_resume_pack
from omg_cli.runtime_events import read_all_runtime_events
from omg_cli.state import load_active_run, load_run, load_run_view


EXACT_TOOL_NAMES: tuple[str, ...] = (
    "run_status.read",
    "trace.timeline",
    "trace.summary",
    "resume_metadata.read",
    "project_memory.search",
    "wiki.read",
    "team_status.read",
    "mailbox.list",
    "proposal.create",
)

FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "accept",
        "set_verified",
        "state.write",
        "state.clear",
        "python.repl",
        "shell.run",
        "workflow.ship",
        "lsp.hover",
        "lsp.definition",
        "lsp.references",
        "lsp.symbols",
        "lsp.diagnostics",
        "lsp.actions",
        "lsp.rename",
    }
)

MAX_OUTPUT_BYTES = 262_144
MAX_PROPOSAL_BYTES = 131_072
MAX_WIKI_PAGE_BYTES = 131_072
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


class ToolError(RuntimeError):
    """Stable structured MCP operation failure."""

    def __init__(self, code: str, message: str, *, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"ok": False, "error": error}


class PathConfineError(ToolError):
    """A caller attempted to address data outside the frozen project root."""

    def __init__(self, message: str):
        super().__init__("E_PATH_ESCAPE", message)


class ToolContext:
    """Cooperative cancellation/deadline context passed to handlers."""

    def __init__(
        self,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> None:
        self.cancel_event = cancel_event
        self.deadline = deadline

    def checkpoint(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ToolError("E_CANCELLED", "MCP operation cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ToolError("E_TIMEOUT", "MCP operation timed out")


def _object_schema(
    properties: dict[str, dict[str, Any]], *, required: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_RUN_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_SAFE_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_LIMIT_256 = {"type": "integer", "minimum": 1, "maximum": 256}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "run_status.read",
        "description": "Read the active or named OMG run view; never mutates state.",
        "inputSchema": _object_schema({"run_id": _RUN_ID}),
    },
    {
        "name": "trace.timeline",
        "description": "Read a bounded, redacted lifecycle-event timeline.",
        "inputSchema": _object_schema(
            {
                "run_id": _RUN_ID,
                "session_id": _SAFE_ID,
                "cursor": {"type": "integer", "minimum": 0},
                "limit": _LIMIT_256,
            }
        ),
    },
    {
        "name": "trace.summary",
        "description": "Summarize bounded lifecycle events by type and source.",
        "inputSchema": _object_schema({"run_id": _RUN_ID, "session_id": _SAFE_ID}),
    },
    {
        "name": "resume_metadata.read",
        "description": "Read bounded resume routing metadata without session transcript bodies.",
        "inputSchema": _object_schema({"run_id": _RUN_ID}),
    },
    {
        "name": "project_memory.search",
        "description": "Search redacted project facts in the current repository.",
        "inputSchema": _object_schema(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 4096},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            required=("query",),
        ),
    },
    {
        "name": "wiki.read",
        "description": "List, read, or search local wiki pages without creating files.",
        "inputSchema": _object_schema(
            {
                "slug": {"type": "string", "minLength": 1, "maxLength": 128},
                "query": {"type": "string", "minLength": 1, "maxLength": 4096},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }
        ),
    },
    {
        "name": "team_status.read",
        "description": "Read a bounded native-team or tmux-team status projection.",
        "inputSchema": _object_schema({"run_id": _RUN_ID, "team_id": _SAFE_ID}),
    },
    {
        "name": "mailbox.list",
        "description": "List bounded mailbox metadata; message bodies are not exposed.",
        "inputSchema": _object_schema(
            {
                "run_id": _RUN_ID,
                "team_id": _SAFE_ID,
                "recipient_id": _SAFE_ID,
                "after": {
                    "oneOf": [
                        {"type": "string", "minLength": 1, "maxLength": 32},
                        {"type": "integer", "minimum": -1},
                    ]
                },
                "generation": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 256},
            },
            required=("run_id", "team_id", "recipient_id"),
        ),
    },
    {
        "name": "proposal.create",
        "description": (
            "Create an immutable non-authoritative JSON proposal under "
            ".omg/artifacts/mcp-proposals only."
        ),
        "inputSchema": _object_schema(
            {
                "proposal_id": _SAFE_ID,
                "kind": _SAFE_ID,
                "payload": {
                    "type": ["object", "array", "string", "integer", "boolean", "null"]
                },
            },
            required=("proposal_id", "kind", "payload"),
        ),
    },
]


def _validate_value(value: Any, schema: Mapping[str, Any], label: str) -> None:
    if "oneOf" in schema:
        failures = 0
        for option in schema["oneOf"]:
            try:
                _validate_value(value, option, label)
                return
            except ToolError:
                failures += 1
        raise ToolError("E_SCHEMA", f"{label} does not match any allowed type")
    allowed = schema.get("type")
    allowed_types = [allowed] if isinstance(allowed, str) else list(allowed or [])
    type_map: dict[str, type | tuple[type, ...]] = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if allowed_types:
        matches = any(
            isinstance(value, type_map[item])
            and not (item == "integer" and isinstance(value, bool))
            for item in allowed_types
        )
        if not matches:
            raise ToolError("E_SCHEMA", f"{label} has invalid type")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise ToolError("E_SCHEMA", f"{label} is too short")
        if len(value) > int(schema.get("maxLength", len(value))):
            raise ToolError("E_SCHEMA", f"{label} is too long")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < int(schema["minimum"]):
            raise ToolError("E_SCHEMA", f"{label} is below minimum")
        if "maximum" in schema and value > int(schema["maximum"]):
            raise ToolError("E_SCHEMA", f"{label} exceeds maximum")


def _validate_arguments(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ToolError("E_SCHEMA", "arguments must be an object")
    spec = next(item for item in TOOL_SPECS if item["name"] == name)
    schema = spec["inputSchema"]
    properties = schema["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ToolError("E_SCHEMA", "unknown argument fields", details=unknown)
    missing = sorted(set(schema["required"]) - set(arguments))
    if missing:
        raise ToolError("E_SCHEMA", "required argument fields missing", details=missing)
    for key, value in arguments.items():
        _validate_value(value, properties[key], key)
    return dict(arguments)


def _require_id(value: Any, *, label: str) -> str:
    try:
        return require_safe_id(value, label=label)
    except ContractValidationError as exc:
        raise ToolError("E_SCHEMA", str(exc)) from exc


def _run_status(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    ctx.checkpoint()
    run_id = args.get("run_id")
    if run_id is None:
        run = load_active_run(root)
    else:
        rid = _require_id(run_id, label="run_id")
        run = load_run_view(root, rid) or load_run(root, rid)
    if run is None:
        return {"ok": True, "found": False, "run": None}
    safe = redact_value(run)
    return {"ok": True, "found": True, "run": safe}


def _filtered_events(args: dict[str, Any], root: Path, ctx: ToolContext) -> list[dict[str, Any]]:
    ctx.checkpoint()
    run_id = args.get("run_id")
    session_id = args.get("session_id")
    if run_id is not None:
        run_id = _require_id(run_id, label="run_id")
    if session_id is not None:
        session_id = _require_id(session_id, label="session_id")
    rows: list[dict[str, Any]] = []
    for event in read_all_runtime_events(root):
        ctx.checkpoint()
        if run_id is not None and event["run_id"] != run_id:
            continue
        if session_id is not None and event["session_id"] != session_id:
            continue
        rows.append(event)
    return rows


def _trace_timeline(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    rows = _filtered_events(args, root, ctx)
    cursor = int(args.get("cursor", 0))
    limit = int(args.get("limit", 100))
    selected = rows[cursor : cursor + limit]
    return {
        "ok": True,
        "cursor": cursor,
        "next_cursor": cursor + len(selected),
        "has_more": cursor + len(selected) < len(rows),
        "events": redact_value(selected),
    }


def _trace_summary(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    rows = _filtered_events(args, root, ctx)
    return {
        "ok": True,
        "count": len(rows),
        "by_type": dict(sorted(Counter(row["event_type"] for row in rows).items())),
        "by_source": dict(sorted(Counter(row["source"] for row in rows).items())),
        "first_observed_at": rows[0]["observed_at"] if rows else None,
        "last_observed_at": rows[-1]["observed_at"] if rows else None,
    }


def _resume_metadata(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    ctx.checkpoint()
    run_id = args.get("run_id")
    if run_id is not None:
        run_id = _require_id(run_id, label="run_id")
    pack = build_resume_pack(root, run_id)
    # Deliberately omit transcript/path bodies.  This is routing metadata only.
    allowed = {
        "ok",
        "reason",
        "run_id",
        "mode",
        "status",
        "stage",
        "goal",
        "verified",
        "terminal",
        "resumable",
        "grok_session_id",
        "commands",
        "view_keys",
        "hint",
        "generated_at",
    }
    return {"ok": bool(pack.get("ok")), "metadata": redact_value({k: v for k, v in pack.items() if k in allowed})}


def _project_memory_search(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    ctx.checkpoint()
    hits = search_memory(root, args["query"], limit=int(args.get("limit", 20)))
    return {"ok": True, "hits": redact_value(hits)}


def _wiki_read(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    ctx.checkpoint()
    wiki_root = root / ".omg" / "wiki"
    slug = args.get("slug")
    query = args.get("query")
    if slug is not None and query is not None:
        raise ToolError("E_SCHEMA", "wiki.read accepts slug or query, not both")
    pages = sorted(wiki_root.glob("*.md")) if wiki_root.is_dir() else []
    pages = [path for path in pages if path.name != "INDEX.md" and not path.is_symlink()]
    if slug is not None:
        if not _SLUG_RE.fullmatch(slug):
            raise PathConfineError("wiki slug must be canonical lowercase kebab-case")
        path = wiki_root / f"{slug}.md"
        if not path.is_file() or path.is_symlink():
            return {"ok": True, "found": False, "slug": slug}
        body = path.read_bytes()
        if len(body) > MAX_WIKI_PAGE_BYTES:
            raise ToolError("E_OUTPUT_BOUND", "wiki page exceeds bounded read limit")
        return {"ok": True, "found": True, "slug": slug, "text": body.decode("utf-8")}
    limit = int(args.get("limit", 20))
    if query is None:
        return {"ok": True, "pages": [{"slug": path.stem} for path in pages[:limit]]}
    needle = query.casefold()
    hits: list[dict[str, str]] = []
    for path in pages:
        ctx.checkpoint()
        body = path.read_bytes()
        if len(body) > MAX_WIKI_PAGE_BYTES:
            continue
        text = body.decode("utf-8")
        if needle not in text.casefold():
            continue
        line = next((line.strip()[:240] for line in text.splitlines() if needle in line.casefold()), "")
        hits.append({"slug": path.stem, "snippet": line})
        if len(hits) >= limit:
            break
    return {"ok": True, "hits": hits}


def _team_status_read(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    ctx.checkpoint()
    run_id = args.get("run_id")
    if run_id is None:
        active = load_active_run(root)
        if active is None:
            return {"ok": True, "found": False, "team": None}
        run_id = active.get("run_id")
    rid = _require_id(run_id, label="run_id")
    team_id = args.get("team_id")
    try:
        if team_id is not None:
            from omg_cli.team.plane import native_team_status

            status = native_team_status(root, run_id=rid, team_id=_require_id(team_id, label="team_id"))
        else:
            from omg_cli.team.plane import status_locked_view, team_status

            status = status_locked_view(team_status(root, rid, probe_tmux=False))
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": True, "found": False, "team": None, "reason": str(exc)}
    return {"ok": True, "found": True, "team": redact_value(status)}


def _mailbox_list(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    ctx.checkpoint()
    from omg_cli.team.mailbox import list_messages

    result = list_messages(
        root,
        run_id=_require_id(args["run_id"], label="run_id"),
        team_id=_require_id(args["team_id"], label="team_id"),
        recipient_id=_require_id(args["recipient_id"], label="recipient_id"),
        after=args.get("after"),
        generation=args.get("generation"),
        limit=int(args.get("limit", 100)),
    )
    return {"ok": True, **redact_value(result)}


def _proposal_path(root: Path, proposal_id: str) -> Path:
    return root / ".omg" / "artifacts" / "mcp-proposals" / f"{proposal_id}.json"


def assert_write_allowed(root: Path, target: Path, *, kind: str = "proposal.create") -> Path:
    """Confine the sole MCP write to the proposal directory, rejecting symlinks."""
    project = Path(root).resolve()
    allowed = project / ".omg" / "artifacts" / "mcp-proposals"
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = project / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(allowed.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathConfineError(f"{kind}: target escapes mcp-proposals") from exc
    probe = project
    try:
        relative_parts = candidate.absolute().relative_to(project).parts
    except ValueError as exc:
        raise PathConfineError(f"{kind}: target escapes project root") from exc
    for part in relative_parts:
        probe = probe / part
        if probe.is_symlink():
            raise PathConfineError(f"{kind}: symlink target refused")
    return resolved


def _proposal_create(args: dict[str, Any], root: Path, ctx: ToolContext) -> dict[str, Any]:
    ctx.checkpoint()
    proposal_id = _require_id(args["proposal_id"], label="proposal_id")
    kind = _require_id(args["kind"], label="kind")
    payload = redact_value(args["payload"])
    proposal = {
        "store_kind": "mcp_proposal",
        "schema_version": 1,
        "proposal_id": proposal_id,
        "kind": kind,
        "payload": payload,
        "authoritative": False,
    }
    body = canonical_json_bytes(proposal)
    if len(body) > MAX_PROPOSAL_BYTES:
        raise ToolError("E_OUTPUT_BOUND", "proposal exceeds bounded byte limit")
    path = assert_write_allowed(root, _proposal_path(root, proposal_id))
    ensure_managed_dir(path.parent)
    lock = path.with_suffix(".lock")
    with exclusive_lock(lock):
        ctx.checkpoint()
        if path.exists():
            current = path.read_bytes()
            if current != body:
                raise ToolError("E_IMMUTABLE_CONFLICT", "proposal_id already exists with different bytes")
            duplicate = True
        else:
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE, replace=False)
            duplicate = False
        os.chmod(path, DATA_FILE_MODE)
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "kind": kind,
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_hex(body),
        "duplicate": duplicate,
        "authoritative": False,
    }


Handler = Callable[[dict[str, Any], Path, ToolContext], dict[str, Any]]
TOOL_HANDLERS: dict[str, Handler] = {
    "run_status.read": _run_status,
    "trace.timeline": _trace_timeline,
    "trace.summary": _trace_summary,
    "resume_metadata.read": _resume_metadata,
    "project_memory.search": _project_memory_search,
    "wiki.read": _wiki_read,
    "team_status.read": _team_status_read,
    "mailbox.list": _mailbox_list,
    "proposal.create": _proposal_create,
}


def list_tool_names() -> list[str]:
    return [spec["name"] for spec in TOOL_SPECS]


def dispatch_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    root: Path | None = None,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Validate and execute one allowlisted operation with bounded output."""
    if name not in TOOL_HANDLERS:
        code = "E_FORBIDDEN_TOOL" if name in FORBIDDEN_TOOL_NAMES else "E_UNKNOWN_TOOL"
        return ToolError(code, f"tool is not registered: {name}").payload()
    context = ToolContext(cancel_event=cancel_event, deadline=deadline)
    try:
        context.checkpoint()
        args = _validate_arguments(name, arguments)
        project = Path(root).resolve() if root is not None else Path.cwd().resolve()
        payload = TOOL_HANDLERS[name](args, project, context)
        context.checkpoint()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > max_output_bytes:
            raise ToolError("E_OUTPUT_BOUND", "MCP operation output exceeds bounded byte limit")
        return payload
    except ToolError as exc:
        return exc.payload()
    except (ContractValidationError, OSError, UnicodeError, ValueError, TypeError) as exc:
        return ToolError("E_OPERATION", str(exc)).payload()


if tuple(list_tool_names()) != EXACT_TOOL_NAMES or set(TOOL_HANDLERS) != set(EXACT_TOOL_NAMES):
    raise RuntimeError("MCP registry must expose exactly the frozen nine operations")
if set(EXACT_TOOL_NAMES) & FORBIDDEN_TOOL_NAMES:
    raise RuntimeError("MCP registry intersects forbidden surface")


__all__ = [
    "EXACT_TOOL_NAMES",
    "FORBIDDEN_TOOL_NAMES",
    "MAX_OUTPUT_BYTES",
    "PathConfineError",
    "TOOL_HANDLERS",
    "TOOL_SPECS",
    "ToolContext",
    "ToolError",
    "assert_write_allowed",
    "dispatch_tool",
    "list_tool_names",
]
