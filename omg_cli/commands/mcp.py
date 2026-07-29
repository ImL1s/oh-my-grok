"""MCP-family CLI handlers (#29 Phase 2).

Commands: mcp-server, mcp-install.
Parser construction remains in ``main.build_parser``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omg_cli.cli_util import project_root


def cmd_mcp_server(args: argparse.Namespace) -> int:
    """Run focused stdio MCP server (sets OMG_MCP_SERVER=1)."""
    from omg_cli.acceptance import MCP_SERVER_ENV
    from omg_cli.mcp.server import run_stdio_server

    os.environ[MCP_SERVER_ENV] = "1"
    root = project_root()
    if getattr(args, "root", None):
        root = Path(args.root).resolve()
    return int(run_stdio_server(root=root))


def cmd_mcp_install(args: argparse.Namespace) -> int:
    """Print or run ``grok mcp add omg omg -- mcp-server``."""
    scope = getattr(args, "scope", None) or "user"
    argv = ["grok", "mcp", "add", "omg", "omg", "--", "mcp-server"]
    if scope in ("user", "project"):
        # Insert --scope after add name for readability if grok supports it.
        argv = [
            "grok",
            "mcp",
            "add",
            "omg",
            "omg",
            "--scope",
            scope,
            "--",
            "mcp-server",
        ]
    if getattr(args, "print_only", False) or getattr(args, "dry_run", False):
        print(" ".join(argv))
        return 0
    import shutil
    import subprocess

    grok = shutil.which("grok")
    if not grok:
        print(
            "grok not on PATH; run manually:\n  " + " ".join(argv),
            file=sys.stderr,
        )
        return 1
    # Rebuild with absolute-ish omg entry if available
    omg_bin = shutil.which("omg") or "omg"
    cmd = [
        grok,
        "mcp",
        "add",
        "omg",
        omg_bin,
        "--scope",
        scope,
        "--",
        "mcp-server",
    ]
    print("running:", " ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)

__all__ = [
    "cmd_mcp_install",
    "cmd_mcp_server",
]
