"""Optional local language-tool probes (research P2) — honest thin surface.

Grok has no OMC MCP LSP bridge. Prefer host ``read_file`` / ``grep``. This module
only reports available local CLIs and offers a best-effort symbol listing via
``pyright`` when installed.
"""
from __future__ import annotations

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


__all__ = ["PROBE_TOOLS", "probe_tools", "symbols_pyright"]
