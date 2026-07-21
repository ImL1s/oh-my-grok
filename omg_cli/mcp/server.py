"""Stdio MCP JSON-RPC server (stdlib only) for the focused omg tool surface.

Handles ``initialize``, ``tools/list``, ``tools/call``. Framing: prefer
Content-Length headers (MCP stdio); also accept newline-delimited JSON for
hermetic tests and simple pipes.

Never registers accept/verified tools. Sets no verified stamp — that is refused
structurally when ``OMG_MCP_SERVER=1`` (see acceptance.refuse_if_mcp_server).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, BinaryIO

from omg_cli import __version__
from omg_cli.mcp.tools import TOOL_SPECS, dispatch_tool

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "omg"


def server_info() -> dict[str, str]:
    return {"name": SERVER_NAME, "version": __version__}


def handle_message(
    message: dict[str, Any],
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC request dict. Returns response or None for notifications."""
    if not isinstance(message, dict):
        return _error_response(None, -32600, "Invalid Request: expected object")

    msg_id = message.get("id", None)
    method = message.get("method")
    # Notification: no id (or null) and no response required for some methods
    is_notification = "id" not in message

    if message.get("jsonrpc") not in (None, "2.0"):
        return _error_response(msg_id, -32600, "Invalid Request: jsonrpc must be 2.0")

    if not method or not isinstance(method, str):
        if is_notification:
            return None
        return _error_response(msg_id, -32600, "Invalid Request: method required")

    params = message.get("params") or {}
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _error_response(msg_id, -32602, "Invalid params: expected object")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": server_info(),
            "instructions": (
                "Focused in-session read + proposal MCP surface for oh-my-grok. "
                "NOT OMC ~54-tool parity. Exposes reads and non-authoritative "
                "proposal writes only; passes/verified/accept are never MCP tools "
                "(CLI-only AND structurally refused when OMG_MCP_SERVER=1). "
                "LSP tools are local ast probes, not a semantic bridge."
            ),
        }
        return _result_response(msg_id, result)

    if method == "notifications/initialized" or method == "initialized":
        return None

    if method == "ping":
        return _result_response(msg_id, {})

    if method == "tools/list":
        return _result_response(msg_id, {"tools": list(TOOL_SPECS)})

    if method == "tools/call":
        name = params.get("name")
        if not name or not isinstance(name, str):
            return _error_response(msg_id, -32602, "tools/call requires name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error_response(msg_id, -32602, "arguments must be object")
        payload = dispatch_tool(name, arguments, root=root)
        # MCP tools/call result shape: content[] + structuredContent optional
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        is_error = not bool(payload.get("ok", True)) and "error" in payload
        # Treat missing ok with error as error; pure ok True as success
        if payload.get("ok") is False:
            is_error = True
        result = {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": is_error,
        }
        return _result_response(msg_id, result)

    if is_notification:
        return None
    return _error_response(msg_id, -32601, f"Method not found: {method}")


def _result_response(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def encode_message(message: dict[str, Any]) -> bytes:
    """MCP Content-Length framing."""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one framed or newline-delimited JSON-RPC message. None on EOF."""
    # Peek: if first bytes look like Content-Length, use framing; else NDJSON line.
    # We need unbuffered-ish binary reads.
    # Strategy: read until we can decide.
    first = stream.read(1)
    if not first:
        return None
    # Content-Length starts with 'C'; NDJSON typically with '{'
    if first in (b"{", b" ", b"\t", b"\n", b"\r"):
        # NDJSON path — finish the line
        rest = stream.readline()
        line = (first + rest).decode("utf-8", errors="replace").strip()
        if not line:
            return read_message(stream)
        return json.loads(line)

    # Header path
    header_buf = first
    while b"\r\n\r\n" not in header_buf and b"\n\n" not in header_buf:
        chunk = stream.read(1)
        if not chunk:
            break
        header_buf += chunk
        if len(header_buf) > 65536:
            raise ValueError("MCP header too large")
    header_text = header_buf.decode("ascii", errors="replace")
    if "\r\n\r\n" in header_text:
        header_part, _sep, remainder = header_text.partition("\r\n\r\n")
    elif "\n\n" in header_text:
        header_part, _sep, remainder = header_text.partition("\n\n")
    else:
        raise ValueError("incomplete MCP headers")
    length = None
    for line in header_part.splitlines():
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
            break
    if length is None:
        raise ValueError("missing Content-Length")
    body = remainder.encode("utf-8") if remainder else b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise ValueError("truncated MCP body")
        body += chunk
    return json.loads(body.decode("utf-8"))


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    stream.write(encode_message(message))
    stream.flush()


def run_stdio_server(
    *,
    root: Path | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    """Serve until stdin EOF. Returns process exit code."""
    # Ensure marker is set even if caller forgot (main sets it too).
    import os

    from omg_cli.acceptance import MCP_SERVER_ENV

    os.environ[MCP_SERVER_ENV] = "1"

    in_stream = stdin if stdin is not None else sys.stdin.buffer
    out_stream = stdout if stdout is not None else sys.stdout.buffer
    project = Path(root).resolve() if root is not None else Path.cwd().resolve()

    while True:
        try:
            msg = read_message(in_stream)
        except (ValueError, json.JSONDecodeError) as exc:
            err = _error_response(None, -32700, f"Parse error: {exc}")
            write_message(out_stream, err)
            continue
        if msg is None:
            break
        try:
            response = handle_message(msg, root=project)
        except Exception as exc:  # noqa: BLE001 — surface to client, keep server up
            response = _error_response(
                msg.get("id") if isinstance(msg, dict) else None,
                -32603,
                f"Internal error: {exc}",
            )
        if response is not None:
            write_message(out_stream, response)
    return 0


def run_ndjson_roundtrip(
    requests: list[dict[str, Any]],
    *,
    root: Path | None = None,
) -> list[dict[str, Any] | None]:
    """In-process helper for tests: feed request dicts, collect responses."""
    return [handle_message(r, root=root) for r in requests]


__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "encode_message",
    "handle_message",
    "read_message",
    "run_ndjson_roundtrip",
    "run_stdio_server",
    "server_info",
    "write_message",
]
