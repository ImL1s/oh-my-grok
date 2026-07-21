"""Focused in-session MCP surface for oh-my-grok (reads + proposal writes).

Not OMC ~54-tool parity. ``passes`` / ``verified`` / accept are never tools;
see ``omg_cli.mcp.tools`` for the curated allowlist and path confinement.
"""
from __future__ import annotations

from omg_cli.mcp.server import handle_message, run_stdio_server
from omg_cli.mcp.tools import (
    FORBIDDEN_TOOL_NAMES,
    TOOL_HANDLERS,
    TOOL_SPECS,
    list_tool_names,
)

__all__ = [
    "FORBIDDEN_TOOL_NAMES",
    "TOOL_HANDLERS",
    "TOOL_SPECS",
    "handle_message",
    "list_tool_names",
    "run_stdio_server",
]
