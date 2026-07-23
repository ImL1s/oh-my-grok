"""Exact nine-operation MCP surface for oh-my-grok."""
from __future__ import annotations

from omg_cli.mcp.server import MCPRuntime, handle_message, run_stdio_server
from omg_cli.mcp.tools import (
    EXACT_TOOL_NAMES,
    FORBIDDEN_TOOL_NAMES,
    TOOL_HANDLERS,
    TOOL_SPECS,
    list_tool_names,
)

__all__ = [
    "EXACT_TOOL_NAMES",
    "FORBIDDEN_TOOL_NAMES",
    "MCPRuntime",
    "TOOL_HANDLERS",
    "TOOL_SPECS",
    "handle_message",
    "list_tool_names",
    "run_stdio_server",
]
