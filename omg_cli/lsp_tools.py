"""Optional local language-tool probes (research P2) — honest thin surface.

Grok has no host LSP MCP. Prefer host ``read_file`` / ``grep``. This module
reports available local CLIs, offers a best-effort pyright check when installed,
and pure-stdlib ``ast`` probes for Python symbols / syntax diagnostics.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

PROBE_TOOLS = (
    "pyright",
    "basedpyright",
    "typescript-language-server",
    "gopls",
    "rust-analyzer",
    "clangd",
)

_AST_HONESTY = "syntax-only (ast.parse); NOT type-checking"


def probe_tools() -> dict[str, Any]:
    found: dict[str, str | None] = {}
    for name in PROBE_TOOLS:
        found[name] = shutil.which(name)
    available = [k for k, v in found.items() if v]
    return {
        "available": available,
        "paths": found,
        "honesty": (
            "OMG has no host LSP MCP. Use Grok read_file/grep by default; "
            "local CLIs listed here are optional extras."
        ),
    }


def symbols_pyright(path: Path, *, cwd: Path | None = None) -> dict[str, Any]:
    """Best-effort: run pyright --outputjson if available."""
    bin_name = shutil.which("basedpyright") or shutil.which("pyright")
    if not bin_name:
        return {
            "ok": False,
            "error": "pyright/basedpyright not on PATH",
            "fallback": "use grep / read_file",
        }
    path = Path(path)
    if not path.is_file():
        return {"ok": False, "error": f"not a file: {path}"}
    try:
        proc = subprocess.run(
            [bin_name, "--outputjson", str(path)],
            cwd=str(cwd or path.parent),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    # pyright json is diagnostics-heavy; surface summary only
    raw = (proc.stdout or "").strip()
    summary: dict[str, Any] = {
        "ok": proc.returncode in (0, 1),  # 1 often means diagnostics present
        "tool": bin_name,
        "exit_code": proc.returncode,
        "path": str(path),
    }
    if raw:
        try:
            data = json.loads(raw)
            summary["diagnostics_count"] = len(
                (data.get("generalDiagnostics") or data.get("diagnostics") or [])
            )
            summary["version"] = data.get("version")
        except json.JSONDecodeError:
            summary["raw_preview"] = raw[:500]
    else:
        summary["stderr_preview"] = (proc.stderr or "")[:500]
    return summary


def _symbol_entry(name: str, node: ast.AST) -> dict[str, Any]:
    return {
        "name": name,
        "lineno": getattr(node, "lineno", None),
        "col_offset": getattr(node, "col_offset", None),
        "end_lineno": getattr(node, "end_lineno", None),
    }


def symbols_ast(path: Path | str) -> dict[str, Any]:
    """List Python symbols via stdlib ``ast`` (no type-checker, Python source only).

    Collects ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef`` (including
    methods) and ``Import`` / ``ImportFrom`` names with source locations.
    """
    path = Path(path)
    if not path.is_file():
        return {"ok": False, "error": {"msg": f"not a file: {path}"}}
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": {"msg": str(exc)}}
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return {
            "ok": False,
            "error": {
                "msg": exc.msg or "SyntaxError",
                "line": exc.lineno,
                "col": exc.offset,
                "text": exc.text,
            },
        }

    symbols: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(_symbol_entry(node.name, node))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                symbols.append(_symbol_entry(alias.asname or alias.name, node))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    base = node.module or "*"
                    symbols.append(_symbol_entry(f"{base}.*", node))
                else:
                    symbols.append(_symbol_entry(alias.asname or alias.name, node))

    return {
        "ok": True,
        "path": str(path),
        "symbols": symbols,
        "language": "python",
        "honesty": (
            "stdlib ast only (Python source); names/locations from parse tree, "
            "not a semantic language server"
        ),
    }


def diagnostics_ast(path: Path | str) -> dict[str, Any]:
    """Syntax diagnostics via ``ast.parse`` only — not type-checking.

    Always returns ``honesty`` describing the syntax-only scope. ``ok`` is True
    when the probe ran (file readable); parse failures land in ``diagnostics``.
    """
    path = Path(path)
    if not path.is_file():
        return {
            "ok": False,
            "error": {"msg": f"not a file: {path}"},
            "honesty": _AST_HONESTY,
        }
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "error": {"msg": str(exc)},
            "honesty": _AST_HONESTY,
        }
    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return {
            "ok": True,
            "path": str(path),
            "diagnostics": [
                {
                    "line": exc.lineno,
                    "col": exc.offset,
                    "msg": exc.msg or "SyntaxError",
                }
            ],
            "honesty": _AST_HONESTY,
        }
    return {
        "ok": True,
        "path": str(path),
        "diagnostics": [],
        "honesty": _AST_HONESTY,
    }


__all__ = [
    "PROBE_TOOLS",
    "probe_tools",
    "symbols_pyright",
    "symbols_ast",
    "diagnostics_ast",
]
