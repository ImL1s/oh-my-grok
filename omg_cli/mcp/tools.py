"""Curated MCP tool handlers for in-session omg ops.

Security (all three mandatory):
1. Allowlist only — never register accept / set_verified / state_write / …
2. Structural refusal lives in acceptance (OMG_MCP_SERVER=1) — not here.
3. Every write path is confined under allowed ``.omg`` subtrees (never state).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from omg_cli.evidence import EvidenceError, validate_identifier
from omg_cli.hud import hud_pack
from omg_cli.lsp_tools import diagnostics_ast, symbols_ast
from omg_cli.note import add_note, read_notes
from omg_cli.resume import build_resume_pack, resume_md_path
from omg_cli.state import load_active_run, load_run, load_run_view
from omg_cli.wiki import WikiError, ingest as wiki_ingest, list_pages, query as wiki_query

# ---------------------------------------------------------------------------
# Path confinement (#3) — write targets must stay under these subtrees
# ---------------------------------------------------------------------------

_PROJECT_MEMORY_NAME = "project-memory.json"
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Names that must NEVER appear in the registered tool set (registry test).
FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "accept",
        "omg_accept",
        "set_verified",
        "omg_set_verified",
        "register_cli_acceptance_token",
        "omg_register_cli_acceptance_token",
        "state_write",
        "omg_state_write",
        "state_clear",
        "omg_state_clear",
        "python_repl",
        "omg_python_repl",
        "ast_grep_replace",
        "omg_ast_grep_replace",
        "shared_memory",
        "omg_shared_memory",
        "session_search",
        "omg_session_search",
        "merge_readiness",
        "omg_merge_readiness",
        # semantic LSP bridge surface (OMC ships; OMG does not)
        "lsp_goto",
        "lsp_hover",
        "lsp_rename",
        "lsp_find_references",
        "omg_lsp_goto",
        "omg_lsp_hover",
        "omg_lsp_rename",
        "omg_lsp_find_references",
    }
)


class PathConfineError(ValueError):
    """Write target escapes an allowed ``.omg`` subtree."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(args: dict[str, Any] | None, default: Path | None) -> Path:
    if default is not None:
        return Path(default).resolve()
    # Handlers never accept a caller-supplied absolute root into state; root is
    # process cwd (or inject for tests). Optional relative "root" is refused.
    return Path.cwd().resolve()


def assert_write_allowed(root: Path, target: Path, *, kind: str) -> Path:
    """Resolve *target* and refuse ``.omg/state/**``, ``..``, and symlink escapes.

    Allowed write subtrees (under project root):
    - ``.omg/notepad.md``
    - ``.omg/wiki/**``
    - ``.omg/artifacts/**``
    - ``.omg/project-memory*`` (json notes file)
    """
    root_r = Path(root).resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root_r / candidate

    try:
        rel = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathConfineError(f"{kind}: cannot resolve path: {exc}") from exc

    try:
        rel.relative_to(root_r)
    except ValueError as exc:
        raise PathConfineError(
            f"{kind}: write target escapes project root: {rel}"
        ) from exc

    # Hard ban: anything under .omg/state
    state_root = (root_r / ".omg" / "state").resolve()
    try:
        rel.relative_to(state_root)
        raise PathConfineError(
            f"{kind}: writes under .omg/state/ are forbidden (got {rel})"
        )
    except ValueError:
        pass

    # Symlink component scan on the unreolved path under root
    probe = root_r
    try:
        parts = rel.relative_to(root_r).parts
    except ValueError:
        parts = ()
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            # After resolve we already checked landing zone; still refuse
            # intermediate symlinks as defense in depth.
            link_target = probe.resolve()
            try:
                link_target.relative_to(root_r)
            except ValueError as exc:
                raise PathConfineError(
                    f"{kind}: symlink escapes project root: {probe}"
                ) from exc
            try:
                link_target.relative_to(state_root)
                raise PathConfineError(
                    f"{kind}: symlink into .omg/state/ refused: {probe}"
                )
            except ValueError:
                pass

    allowed_checks: list[tuple[str, Callable[[Path], bool]]] = [
        (
            "notepad",
            lambda p: p == (root_r / ".omg" / "notepad.md").resolve(),
        ),
        (
            "wiki",
            lambda p: _is_under(p, (root_r / ".omg" / "wiki").resolve()),
        ),
        (
            "artifacts",
            lambda p: _is_under(p, (root_r / ".omg" / "artifacts").resolve()),
        ),
        (
            "project-memory",
            lambda p: p.name.startswith("project-memory")
            and p.parent.resolve() == (root_r / ".omg").resolve(),
        ),
    ]
    for _label, check in allowed_checks:
        if check(rel):
            return rel
    raise PathConfineError(
        f"{kind}: write target not under allowed subtree "
        f"(.omg/notepad.md|.omg/wiki/|.omg/artifacts/|.omg/project-memory*): {rel}"
    )


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def project_memory_path(root: Path) -> Path:
    return Path(root) / ".omg" / _PROJECT_MEMORY_NAME


def _safe_artifact_name(name: str) -> str:
    raw = (name or "").strip()
    if not raw or not _ARTIFACT_NAME_RE.fullmatch(raw):
        raise PathConfineError(
            f"invalid artifact name {name!r}; expected safe basename "
            "([A-Za-z0-9][A-Za-z0-9._-]{0,127}), no path separators"
        )
    if ".." in raw or "/" in raw or "\\" in raw:
        raise PathConfineError(f"artifact name must not contain path elements: {name!r}")
    # Block names that look like they escape into state
    low = raw.lower()
    if low in {"status.json", "active.json"} or low.startswith("acceptance"):
        raise PathConfineError(
            f"artifact name reserved / looks like state: {name!r}"
        )
    return raw


# ---------------------------------------------------------------------------
# Handlers (thin over omg_cli modules)
# ---------------------------------------------------------------------------

Handler = Callable[[dict[str, Any], Path], dict[str, Any]]


def omg_state_status(args: dict[str, Any], root: Path) -> dict[str, Any]:
    run_id = args.get("run_id")
    rid = str(run_id).strip() if run_id else None
    pack = hud_pack(root, rid)
    return {"ok": True, "pack": pack}


def omg_state_read(args: dict[str, Any], root: Path) -> dict[str, Any]:
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return {"ok": False, "error": "run_id required"}
    try:
        validate_identifier(run_id, label="run_id")
    except EvidenceError as exc:
        return {"ok": False, "error": str(exc)}
    data = load_run_view(root, run_id) or load_run(root, run_id)
    if data is None:
        return {"ok": False, "error": f"run not found: {run_id}"}
    return {"ok": True, "run": data}


def omg_state_list_active(args: dict[str, Any], root: Path) -> dict[str, Any]:
    _ = args
    active = load_active_run(root)
    runs_dir = root / ".omg" / "state" / "runs"
    listed: list[dict[str, Any]] = []
    if runs_dir.is_dir():
        for child in sorted(runs_dir.iterdir()):
            if not child.is_dir():
                continue
            st = load_run(root, child.name)
            if st is None:
                continue
            listed.append(
                {
                    "run_id": child.name,
                    "status": st.get("status"),
                    "mode": st.get("mode"),
                    "verified": bool(st.get("verified")),
                }
            )
    return {
        "ok": True,
        "active": active,
        "runs": listed,
    }


def omg_note_read(args: dict[str, Any], root: Path) -> dict[str, Any]:
    _ = args
    return {"ok": True, "text": read_notes(root)}


def omg_note_write(args: dict[str, Any], root: Path) -> dict[str, Any]:
    text = str(args.get("text") or "")
    if not text.strip():
        return {"ok": False, "error": "text required"}
    priority = bool(args.get("priority"))
    # Fixed destination — never accept a path from the caller.
    from omg_cli.note import notepad_path

    dest = notepad_path(root)
    assert_write_allowed(root, dest, kind="omg_note_write")
    path = add_note(root, text, priority=priority)
    # Re-check final path after write
    assert_write_allowed(root, path, kind="omg_note_write")
    return {
        "ok": True,
        "path": str(path),
        "priority": priority,
        "ttl": "permanent" if priority else "7d",
    }


def omg_wiki_query(args: dict[str, Any], root: Path) -> dict[str, Any]:
    needle = str(args.get("needle") or args.get("q") or "").strip()
    if not needle:
        return {"ok": False, "error": "needle required"}
    try:
        hits = wiki_query(root, needle, limit=int(args.get("limit") or 20))
    except WikiError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "hits": hits}


def omg_wiki_list(args: dict[str, Any], root: Path) -> dict[str, Any]:
    _ = args
    return {"ok": True, "pages": list_pages(root)}


def omg_wiki_ingest(args: dict[str, Any], root: Path) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or args.get("text") or "")
    if not title or not body.strip():
        return {"ok": False, "error": "title and body required"}
    try:
        result = wiki_ingest(root, title=title, body=body)
    except WikiError as exc:
        return {"ok": False, "error": str(exc)}
    path = Path(result["path"])
    assert_write_allowed(root, path, kind="omg_wiki_ingest")
    return {"ok": True, **result}


def omg_project_memory_read(args: dict[str, Any], root: Path) -> dict[str, Any]:
    _ = args
    path = project_memory_path(root)
    if not path.is_file():
        return {"ok": True, "exists": False, "notes": [], "text": ""}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {"notes": []}
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}
    notes = data.get("notes") if isinstance(data, dict) else []
    if not isinstance(notes, list):
        notes = []
    return {
        "ok": True,
        "exists": True,
        "path": str(path),
        "notes": notes,
        "text": raw,
    }


def omg_project_memory_add_note(args: dict[str, Any], root: Path) -> dict[str, Any]:
    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text required"}
    path = project_memory_path(root)
    assert_write_allowed(root, path, kind="omg_project_memory_add_note")
    path.parent.mkdir(parents=True, exist_ok=True)
    notes: list[dict[str, Any]] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict) and isinstance(data.get("notes"), list):
                notes = list(data["notes"])
        except (OSError, json.JSONDecodeError):
            notes = []
    notes.append({"text": text, "at": _utc_now()})
    body = json.dumps({"notes": notes}, indent=2, ensure_ascii=False) + "\n"
    # Final confinement before write
    assert_write_allowed(root, path, kind="omg_project_memory_add_note")
    path.write_text(body, encoding="utf-8")
    return {"ok": True, "path": str(path), "count": len(notes)}


def omg_artifact_write(args: dict[str, Any], root: Path) -> dict[str, Any]:
    """Write a non-authoritative proposal under ``.omg/artifacts/`` only."""
    name = _safe_artifact_name(str(args.get("name") or ""))
    body = str(args.get("body") or "")
    if not body:
        return {"ok": False, "error": "body required"}
    # Reject path-shaped names already handled by _safe_artifact_name; also
    # refuse if the *resolved* path would leave artifacts/ (e.g. weird names).
    dest = (root / ".omg" / "artifacts" / name).resolve()
    try:
        assert_write_allowed(root, dest, kind="omg_artifact_write")
    except PathConfineError:
        raise
    # Double-check we are still under artifacts after resolve
    art_root = (root / ".omg" / "artifacts").resolve()
    if not _is_under(dest, art_root) and dest != art_root:
        # dest is a file under art_root — _is_under works for files
        if dest.parent != art_root and not _is_under(dest.parent, art_root):
            raise PathConfineError(
                f"omg_artifact_write: resolved path not under artifacts: {dest}"
            )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    assert_write_allowed(root, dest, kind="omg_artifact_write")
    return {
        "ok": True,
        "path": str(dest),
        "name": name,
        "note": "proposal only — not authoritative state",
    }


def omg_lsp_symbols(args: dict[str, Any], root: Path) -> dict[str, Any]:
    path_s = str(args.get("path") or "").strip()
    if not path_s:
        return {"ok": False, "error": "path required"}
    path = Path(path_s)
    if not path.is_absolute():
        path = root / path
    return symbols_ast(path)


def omg_lsp_diagnostics(args: dict[str, Any], root: Path) -> dict[str, Any]:
    path_s = str(args.get("path") or "").strip()
    if not path_s:
        return {"ok": False, "error": "path required"}
    path = Path(path_s)
    if not path.is_absolute():
        path = root / path
    return diagnostics_ast(path)


def omg_resume_context(args: dict[str, Any], root: Path) -> dict[str, Any]:
    run_id = args.get("run_id")
    rid = str(run_id).strip() if run_id else None
    pack = build_resume_pack(root, rid)
    md_path = resume_md_path(root)
    resume_md = ""
    if md_path.is_file():
        try:
            resume_md = md_path.read_text(encoding="utf-8")
        except OSError:
            resume_md = ""
    return {
        "ok": bool(pack.get("ok")),
        "pack": pack,
        "resume_md_path": str(md_path),
        "resume_md": resume_md,
    }


# ---------------------------------------------------------------------------
# Registry (allowlist is the source of truth for tools/list)
# ---------------------------------------------------------------------------

TOOL_HANDLERS: dict[str, Handler] = {
    "omg_state_status": omg_state_status,
    "omg_state_read": omg_state_read,
    "omg_state_list_active": omg_state_list_active,
    "omg_note_read": omg_note_read,
    "omg_note_write": omg_note_write,
    "omg_wiki_query": omg_wiki_query,
    "omg_wiki_list": omg_wiki_list,
    "omg_wiki_ingest": omg_wiki_ingest,
    "omg_project_memory_read": omg_project_memory_read,
    "omg_project_memory_add_note": omg_project_memory_add_note,
    "omg_artifact_write": omg_artifact_write,
    "omg_lsp_symbols": omg_lsp_symbols,
    "omg_lsp_diagnostics": omg_lsp_diagnostics,
    "omg_resume_context": omg_resume_context,
}


def _schema_props(**props: dict[str, Any]) -> dict[str, Any]:
    required = [k for k, v in props.items() if v.pop("_required", False)]
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "omg_state_status",
        "description": (
            "Read HUD/status pack for the active or named run (side-effect-free)."
        ),
        "inputSchema": _schema_props(
            run_id={"type": "string", "description": "optional run_id"},
        ),
    },
    {
        "name": "omg_state_read",
        "description": "Load one run's status view (read-only).",
        "inputSchema": _schema_props(
            run_id={
                "type": "string",
                "description": "run_id",
                "_required": True,
            },
        ),
    },
    {
        "name": "omg_state_list_active",
        "description": "List active pointer and known runs under .omg/state/runs (read-only).",
        "inputSchema": _schema_props(),
    },
    {
        "name": "omg_note_read",
        "description": "Read .omg/notepad.md contents.",
        "inputSchema": _schema_props(),
    },
    {
        "name": "omg_note_write",
        "description": (
            "Append a non-authoritative note to .omg/notepad.md "
            "(path-confined; never writes under .omg/state/)."
        ),
        "inputSchema": _schema_props(
            text={"type": "string", "description": "note text", "_required": True},
            priority={
                "type": "boolean",
                "description": "permanent if true (else 7d TTL tag)",
            },
        ),
    },
    {
        "name": "omg_wiki_query",
        "description": "Keyword search under .omg/wiki (read-only).",
        "inputSchema": _schema_props(
            needle={"type": "string", "description": "search string", "_required": True},
            limit={"type": "integer", "description": "max hits (default 20)"},
        ),
    },
    {
        "name": "omg_wiki_list",
        "description": "List wiki pages under .omg/wiki.",
        "inputSchema": _schema_props(),
    },
    {
        "name": "omg_wiki_ingest",
        "description": (
            "Ingest a wiki page under .omg/wiki/ (proposal knowledge; path-confined)."
        ),
        "inputSchema": _schema_props(
            title={"type": "string", "description": "page title", "_required": True},
            body={"type": "string", "description": "page body", "_required": True},
        ),
    },
    {
        "name": "omg_project_memory_read",
        "description": "Read .omg/project-memory.json if present.",
        "inputSchema": _schema_props(),
    },
    {
        "name": "omg_project_memory_add_note",
        "description": (
            "Append a note to .omg/project-memory.json (path-confined; not state)."
        ),
        "inputSchema": _schema_props(
            text={"type": "string", "description": "memory note", "_required": True},
        ),
    },
    {
        "name": "omg_artifact_write",
        "description": (
            "Write a PROPOSAL file under .omg/artifacts/ only "
            "(never .omg/state/; not authoritative)."
        ),
        "inputSchema": _schema_props(
            name={
                "type": "string",
                "description": "safe basename under .omg/artifacts/",
                "_required": True,
            },
            body={"type": "string", "description": "file contents", "_required": True},
        ),
    },
    {
        "name": "omg_lsp_symbols",
        "description": (
            "List Python symbols via stdlib ast (syntax-only probe; NOT a semantic LSP bridge)."
        ),
        "inputSchema": _schema_props(
            path={"type": "string", "description": "Python file path", "_required": True},
        ),
    },
    {
        "name": "omg_lsp_diagnostics",
        "description": (
            "Syntax diagnostics via ast.parse (local probe; not type-checking)."
        ),
        "inputSchema": _schema_props(
            path={"type": "string", "description": "Python file path", "_required": True},
        ),
    },
    {
        "name": "omg_resume_context",
        "description": "Resolve resume pack + RESUME.md contents (read-only).",
        "inputSchema": _schema_props(
            run_id={"type": "string", "description": "optional run_id"},
        ),
    },
]


def list_tool_names() -> list[str]:
    return [spec["name"] for spec in TOOL_SPECS]


def dispatch_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Call an allowlisted handler. Unknown tools → error dict."""
    if name in FORBIDDEN_TOOL_NAMES:
        return {
            "ok": False,
            "error": f"tool {name!r} is forbidden (authoritative / excluded surface)",
        }
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    args = arguments if isinstance(arguments, dict) else {}
    project = Path(root).resolve() if root is not None else Path.cwd().resolve()
    try:
        return handler(args, project)
    except PathConfineError as exc:
        return {"ok": False, "error": str(exc), "confined": True}
    except EvidenceError as exc:
        return {"ok": False, "error": str(exc)}
    except (OSError, ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}


# Fail-closed: registry must not contain forbidden names at import time.
_reg = set(list_tool_names())
_bad = _reg & FORBIDDEN_TOOL_NAMES
if _bad:
    raise RuntimeError(f"MCP tool registry contains forbidden names: {sorted(_bad)}")
_handler_names = set(TOOL_HANDLERS)
if _handler_names != _reg:
    raise RuntimeError(
        f"TOOL_HANDLERS/TOOL_SPECS mismatch: "
        f"handlers={sorted(_handler_names)} specs={sorted(_reg)}"
    )


__all__ = [
    "FORBIDDEN_TOOL_NAMES",
    "PathConfineError",
    "TOOL_HANDLERS",
    "TOOL_SPECS",
    "assert_write_allowed",
    "dispatch_tool",
    "list_tool_names",
    "project_memory_path",
]
