"""omg tools — OMG-owned LSP/AST-grep/CodeGraph/research sidecar (#73).

Not Grok-native. Does not replace ``omg lsp`` host-owned inspection.
Does not add ``lsp.*`` tools to ``omg mcp-server``.
Parser construction: ``register_tools_parsers``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from omg_cli.cli_envelope import emit_json, failure, success
from omg_cli.cli_util import project_root
from omg_cli.tools_sidecar import (
    FakeLspTransport,
    StdioLspTransport,
    ToolsError,
    ast_replace,
    lsp_command_argv,
    ast_search,
    codegraph_index,
    codegraph_query,
    codegraph_status,
    doctor_payload,
    lsp_operation,
    research_search,
    research_status,
    run_tools_stdio,
)


def normalize_tools_argv(argv: Sequence[str]) -> list[str]:
    """Rewrite ``omg tools … -- <server-flags>`` into ``--lsp-extra=<flag>``.

    argparse ``--lsp-command nargs=+`` cannot take server flags such as
    ``--stdio``: on ``serve`` that token is the sidecar flag, and on ``lsp``
    it is an unrecognized option. Tokens after ``--`` are packed as
    ``--lsp-extra=<token>`` so they reach the language server.
    """
    raw = list(argv)
    i = 0
    tools_at: int | None = None
    value_opts = {"--project-root", "--root"}
    while i < len(raw):
        tok = raw[i]
        if tok == "--":
            break
        if tok.startswith("-"):
            name = tok.split("=", 1)[0]
            if name in value_opts and "=" not in tok:
                i += 2
                continue
            i += 1
            continue
        tools_at = i if tok == "tools" else None
        break
    if tools_at is None:
        return raw
    try:
        dash = raw.index("--", tools_at + 1)
    except ValueError:
        return raw
    extras = raw[dash + 1 :]
    return raw[:dash] + [f"--lsp-extra={token}" for token in extras]


def _fail(command: str, exc: ToolsError) -> int:
    emit_json(
        failure(
            command,
            exc.code,
            exc.message,
            details=exc.details if isinstance(exc.details, dict) else None,
            next_action="See docs/tools-sidecar.md; omg lsp remains host-owned",
        )
    )
    return 1


def _transport_from_args(args: argparse.Namespace, root: Path):
    fake = bool(getattr(args, "fake_lsp", False))
    command = list(getattr(args, "lsp_command", None) or [])
    extra = list(getattr(args, "lsp_extra", None) or [])
    if fake:
        return FakeLspTransport()
    if command or extra:
        return StdioLspTransport(lsp_command_argv(command, extra), cwd=root)
    return None


def cmd_tools(args: argparse.Namespace) -> int:
    action = getattr(args, "tools_action", None)
    if getattr(args, "root", None):
        root = Path(args.root).resolve()
    else:
        root = project_root()
    mode = getattr(args, "capability_mode", None) or "read-only"
    try:
        if action == "doctor":
            payload = doctor_payload(
                root=root, strict=bool(getattr(args, "strict", False))
            )
            payload["verified"] = False
            payload["observed"] = False
            payload["healthy"] = False
            if not payload.get("ok"):
                errors = payload.get("errors")
                if isinstance(errors, list) and errors:
                    message = "; ".join(str(item) for item in errors)
                else:
                    message = "tools sidecar doctor checks failed"
                raise ToolsError(
                    "E_TOOLS_DOCTOR",
                    message,
                    details=payload,
                )
            emit_json(success("tools.doctor", result=payload))
            return 0
        if action == "serve":
            if not getattr(args, "stdio", False):
                emit_json(
                    failure(
                        "tools.serve",
                        "E_USAGE",
                        "require --stdio",
                        next_action="omg tools serve --stdio",
                    )
                )
                return 2
            transport = _transport_from_args(args, root)
            try:
                return int(
                    run_tools_stdio(
                        root, capability_mode=mode, transport=transport
                    )
                )
            finally:
                if transport is not None:
                    transport.close()
        if action == "lsp":
            op = getattr(args, "lsp_op", None) or "servers"
            transport = _transport_from_args(args, root)
            try:
                result = lsp_operation(
                    op,
                    root=root,
                    path=getattr(args, "path", None),
                    capability_mode=mode,
                    apply=bool(getattr(args, "apply", False)),
                    transport=transport,
                    query=getattr(args, "query", None),
                    new_name=getattr(args, "new_name", None),
                    line=getattr(args, "line", None),
                    character=getattr(args, "character", None),
                    end_line=getattr(args, "end_line", None),
                    end_character=getattr(args, "end_character", None),
                )
            finally:
                if transport is not None:
                    transport.close()
            emit_json(success(f"tools.lsp.{op}", result=result))
            return 0
        if action == "ast":
            ast_op = getattr(args, "ast_op", None) or "search"
            if ast_op == "replace":
                result = ast_replace(
                    root=root,
                    pattern=str(getattr(args, "pattern", "") or ""),
                    rewrite=str(getattr(args, "rewrite", "") or ""),
                    lang=str(getattr(args, "lang", None) or "python"),
                    path=getattr(args, "path", None),
                    write=bool(getattr(args, "write", False)),
                    capability_mode=mode,
                )
            else:
                result = ast_search(
                    root=root,
                    pattern=str(getattr(args, "pattern", "") or ""),
                    lang=str(getattr(args, "lang", None) or "python"),
                    path=getattr(args, "path", None),
                )
            emit_json(success(f"tools.ast.{ast_op}", result=result))
            return 0
        if action == "codegraph":
            cg_op = getattr(args, "codegraph_op", None) or "status"
            cg_mode = getattr(args, "mode", None) or "auto"
            if cg_op == "query":
                result = codegraph_query(
                    root=root,
                    mode=cg_mode,
                    query=str(getattr(args, "query", "") or ""),
                )
            elif cg_op == "index":
                result = codegraph_index(root=root, mode=cg_mode)
            else:
                result = codegraph_status(root=root, mode=cg_mode)
            emit_json(success(f"tools.codegraph.{cg_op}", result=result))
            return 0
        if action == "research":
            research_op = getattr(args, "research_op", None) or "status"
            if research_op == "search":
                result = research_search(str(getattr(args, "query", "") or ""))
            else:
                result = research_status()
            emit_json(success(f"tools.research.{research_op}", result=result))
            return 0
    except ToolsError as exc:
        command = f"tools.{action or 'unknown'}"
        return _fail(command, exc)
    print(
        "usage: omg tools {doctor,serve,lsp,ast,codegraph,research}",
        file=sys.stderr,
    )
    return 2


def register_tools_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register mcp-family ``tools`` parsers (#73)."""
    p_tools = sub.add_parser(
        "tools",
        parents=[common],
        help=(
            "OMG-owned LSP/AST-grep/CodeGraph/research sidecar "
            "(not Grok-native; #73)"
        ),
    )
    tools_sub = p_tools.add_subparsers(dest="tools_action")

    p_doctor = tools_sub.add_parser(
        "doctor",
        parents=[common],
        help="dependency/readiness report (never live-verified)",
    )
    p_doctor.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when required sidecar checks fail",
    )
    p_doctor.add_argument("--root", default=None, help="workspace root")
    p_doctor.set_defaults(func=cmd_tools, tools_action="doctor")

    p_serve = tools_sub.add_parser(
        "serve",
        parents=[common],
        help="stdio MCP sidecar (separate from omg mcp-server)",
    )
    p_serve.add_argument("--stdio", action="store_true", help="required stdio mode")
    p_serve.add_argument("--root", default=None, help="workspace root")
    p_serve.add_argument(
        "--capability-mode",
        dest="capability_mode",
        choices=("read-only", "read-write"),
        default="read-only",
    )
    p_serve.add_argument("--fake-lsp", action="store_true", help="in-process fake protocol")
    p_serve.add_argument(
        "--lsp-command",
        nargs="+",
        default=None,
        help=(
            "language server argv wired into MCP omg.tools.lsp.* tools; "
            "rust-analyzer uses default stdio (do not pass --stdio); "
            "other servers that need a flag: -- --stdio"
        ),
    )
    p_serve.add_argument(
        "--lsp-extra",
        action="append",
        default=None,
        help=argparse.SUPPRESS,
    )
    p_serve.set_defaults(func=cmd_tools, tools_action="serve")

    p_lsp = tools_sub.add_parser(
        "lsp",
        parents=[common],
        help="sidecar semantic LSP (not omg lsp host probe)",
    )
    p_lsp.add_argument(
        "lsp_op",
        nargs="?",
        default="servers",
        choices=(
            "servers",
            "hover",
            "definition",
            "references",
            "document_symbols",
            "workspace_symbols",
            "diagnostics",
            "prepare_rename",
            "rename",
            "code_action",
            "code_action_resolve",
        ),
    )
    p_lsp.add_argument("--path", default=None)
    p_lsp.add_argument("--line", type=int, default=None)
    p_lsp.add_argument("--character", type=int, default=None)
    p_lsp.add_argument("--end-line", dest="end_line", type=int, default=None)
    p_lsp.add_argument("--end-character", dest="end_character", type=int, default=None)
    p_lsp.add_argument("--query", default=None)
    p_lsp.add_argument("--new-name", dest="new_name", default=None)
    p_lsp.add_argument("--apply", action="store_true")
    p_lsp.add_argument("--fake-lsp", action="store_true", help="in-process fake protocol")
    p_lsp.add_argument(
        "--lsp-command",
        nargs="+",
        default=None,
        help=(
            "language server argv (not auto-installed); "
            "rust-analyzer uses default stdio (do not pass --stdio); "
            "other servers that need a flag: -- --stdio"
        ),
    )
    p_lsp.add_argument(
        "--lsp-extra",
        action="append",
        default=None,
        help=argparse.SUPPRESS,
    )
    p_lsp.add_argument(
        "--capability-mode",
        dest="capability_mode",
        choices=("read-only", "read-write"),
        default="read-only",
    )
    p_lsp.add_argument("--root", default=None)
    p_lsp.set_defaults(func=cmd_tools, tools_action="lsp")

    p_ast = tools_sub.add_parser(
        "ast",
        parents=[common],
        help="ast-grep search/replace (default dry-run)",
    )
    p_ast.add_argument("ast_op", nargs="?", default="search", choices=("search", "replace"))
    p_ast.add_argument("--pattern", default="")
    p_ast.add_argument("--rewrite", default="")
    p_ast.add_argument("--lang", default="python")
    p_ast.add_argument("--path", default=None)
    p_ast.add_argument("--write", action="store_true", help="apply replace (read-write)")
    p_ast.add_argument(
        "--capability-mode",
        dest="capability_mode",
        choices=("read-only", "read-write"),
        default="read-only",
    )
    p_ast.add_argument("--root", default=None)
    p_ast.set_defaults(func=cmd_tools, tools_action="ast")

    p_cg = tools_sub.add_parser(
        "codegraph",
        parents=[common],
        help="CodeGraph status/query (off|auto|shared|local)",
    )
    p_cg.add_argument(
        "codegraph_op", nargs="?", default="status", choices=("status", "query", "index")
    )
    p_cg.add_argument(
        "--mode",
        choices=("off", "auto", "shared", "local"),
        default="auto",
    )
    p_cg.add_argument("--query", default="")
    p_cg.add_argument("--root", default=None)
    p_cg.set_defaults(func=cmd_tools, tools_action="codegraph")

    p_research = tools_sub.add_parser(
        "research",
        parents=[common],
        help="opt-in network research (disabled by default)",
    )
    p_research.add_argument(
        "research_op", nargs="?", default="status", choices=("status", "search")
    )
    p_research.add_argument("--query", default="")
    p_research.set_defaults(func=cmd_tools, tools_action="research")

    p_tools.set_defaults(func=cmd_tools)


__all__ = ["cmd_tools", "normalize_tools_argv", "register_tools_parsers"]
