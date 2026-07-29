"""MCP-family CLI handlers (#29 Phase 2).

Commands: mcp-server, mcp-install.
Parser construction: ``register_mcp_parsers`` (#29 Phase 4').
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


def register_mcp_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register mcp-family argparse parsers (#29 Phase 4').

    Commands: mcp-server, mcp-install.
    """
    p_mcp_server = sub.add_parser(
        "mcp-server",
        parents=[common],
        help=(
            "run focused in-session MCP server (stdio JSON-RPC; "
            "reads + proposal writes only; sets OMG_MCP_SERVER=1)"
        ),
    )
    p_mcp_server.add_argument(
        "--root",
        default=None,
        help="project root (default: cwd)",
    )
    p_mcp_server.set_defaults(func=cmd_mcp_server)

    p_mcp_install = sub.add_parser(
        "mcp-install",
        parents=[common],
        help="register with Grok: grok mcp add omg omg -- mcp-server",
    )
    p_mcp_install.add_argument(
        "--scope",
        choices=("user", "project"),
        default="user",
        help="grok mcp add --scope (default: user)",
    )
    p_mcp_install.add_argument(
        "--print-only",
        "--dry-run",
        dest="print_only",
        action="store_true",
        help="print the grok mcp add command without running it",
    )
    p_mcp_install.set_defaults(func=cmd_mcp_install)


__all__ = [
    "register_mcp_parsers",
    "cmd_mcp_install",
    "cmd_mcp_server",
]
