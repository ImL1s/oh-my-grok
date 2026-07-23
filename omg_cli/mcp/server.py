"""Stdio MCP JSON-RPC server for the exact bounded OMG tool surface.

Handles ``initialize``, ``tools/list``, ``tools/call``. Framing: auto-detect
the client's framing on the first message (Content-Length headers **or**
newline-delimited JSON) and respond in the **same** framing for the whole
connection. Grok Build CLI sends NDJSON and cannot parse Content-Length replies.

Never registers shell, semantic-LSP, accept or verified tools.  Tool execution
is concurrency-bounded and supports cooperative cancellation and deadlines.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

from omg_cli import __version__
from omg_cli.mcp.tools import TOOL_SPECS, ToolError, dispatch_tool

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "omg"

# Wire framing. First inbound message locks the connection framing.
FRAMING_CONTENT_LENGTH = "content-length"
FRAMING_NDJSON = "ndjson"
MAX_WIRE_BYTES = 1_048_576
DEFAULT_CALL_TIMEOUT_SECONDS = 10.0
MAX_CALL_TIMEOUT_SECONDS = 30.0
MAX_CONCURRENT_CALLS = 8
MAX_OUTSTANDING_CALLS = 64
MAX_PRE_CANCELLED_REQUESTS = 1024


class MCPRuntime:
    """Bounded request executor shared by one MCP connection."""

    def __init__(
        self,
        *,
        max_workers: int = MAX_CONCURRENT_CALLS,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        if not 1 <= int(max_workers) <= MAX_CONCURRENT_CALLS:
            raise ValueError("MCP max_workers must be between 1 and 8")
        if not 0 < float(call_timeout_seconds) <= MAX_CALL_TIMEOUT_SECONDS:
            raise ValueError("MCP timeout must be in (0, 30]")
        self.call_timeout_seconds = float(call_timeout_seconds)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=int(max_workers), thread_name_prefix="omg-mcp"
        )
        self._lock = threading.Lock()
        self._inflight: dict[Any, threading.Event] = {}
        self._pre_cancelled: set[Any] = set()
        self._slots = threading.BoundedSemaphore(MAX_OUTSTANDING_CALLS)
        self._closed = False

    def cancel(self, request_id: Any) -> None:
        try:
            hash(request_id)
        except TypeError:
            return
        with self._lock:
            event = self._inflight.get(request_id)
            if event is None:
                if len(self._pre_cancelled) >= MAX_PRE_CANCELLED_REQUESTS:
                    self._pre_cancelled.pop()
                self._pre_cancelled.add(request_id)
            else:
                event.set()

    def call(
        self,
        request_id: Any,
        name: str,
        arguments: dict[str, Any],
        *,
        root: Path,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        try:
            hash(request_id)
        except TypeError:
            return ToolError("E_SCHEMA", "MCP request id must be scalar").payload()
        if not self._slots.acquire(blocking=False):
            return ToolError(
                "E_SERVER_BUSY",
                "MCP outstanding request bound reached",
            ).payload()
        timeout = self.call_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        timeout = min(max(timeout, 0.001), MAX_CALL_TIMEOUT_SECONDS)
        deadline = time.monotonic() + timeout
        event = threading.Event()
        with self._lock:
            if self._closed:
                self._slots.release()
                return ToolError("E_SERVER_CLOSED", "MCP runtime is closed").payload()
            if request_id in self._pre_cancelled:
                self._pre_cancelled.discard(request_id)
                event.set()
            self._inflight[request_id] = event
        try:
            future = self._executor.submit(
                dispatch_tool,
                name,
                arguments,
                root=root,
                cancel_event=event,
                deadline=deadline,
            )
        except RuntimeError:
            with self._lock:
                self._inflight.pop(request_id, None)
            self._slots.release()
            return ToolError("E_SERVER_CLOSED", "MCP runtime is closed").payload()
        try:
            while True:
                if event.is_set():
                    future.cancel()
                    return ToolError("E_CANCELLED", "MCP operation cancelled").payload()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    event.set()
                    future.cancel()
                    return ToolError("E_TIMEOUT", "MCP operation timed out").payload()
                try:
                    return future.result(timeout=min(remaining, 0.02))
                except concurrent.futures.TimeoutError:
                    continue
                except Exception as exc:  # pragma: no cover - dispatch is defensive
                    return ToolError("E_INTERNAL", str(exc)).payload()
        finally:
            with self._lock:
                self._inflight.pop(request_id, None)
            self._slots.release()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            events = list(self._inflight.values())
        for event in events:
            event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)


_DEFAULT_RUNTIME = MCPRuntime()


def server_info() -> dict[str, str]:
    return {"name": SERVER_NAME, "version": __version__}


def handle_message(
    message: dict[str, Any],
    *,
    root: Path | None = None,
    runtime: MCPRuntime | None = None,
    call_timeout_seconds: float | None = None,
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
        result: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": server_info(),
            "instructions": (
                "Exact nine-operation read + proposal MCP surface for oh-my-grok. "
                "Only proposal.create writes, and only below mcp-proposals. "
                "State, passes, verified, shell and semantic LSP operations are absent."
            ),
        }
        return _result_response(msg_id, result)

    if method == "notifications/initialized" or method == "initialized":
        return None

    if method in {"notifications/cancelled", "$/cancelRequest"}:
        request_id = params.get("requestId", params.get("id"))
        (runtime or _DEFAULT_RUNTIME).cancel(request_id)
        return None

    if method == "ping":
        return _result_response(msg_id, {})

    if method == "tools/list":
        return _result_response(msg_id, {"tools": list(TOOL_SPECS)})

    if method == "tools/call":
        if is_notification:
            return None
        name = params.get("name")
        if not name or not isinstance(name, str):
            return _error_response(msg_id, -32602, "tools/call requires name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error_response(msg_id, -32602, "arguments must be object")
        project = Path(root).resolve() if root is not None else Path.cwd().resolve()
        payload = (runtime or _DEFAULT_RUNTIME).call(
            msg_id,
            name,
            arguments,
            root=project,
            timeout_seconds=call_timeout_seconds,
        )
        # MCP tools/call result shape: content[] + structuredContent optional
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        is_error = payload.get("ok") is False
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


def encode_message(
    message: dict[str, Any],
    framing: str = FRAMING_CONTENT_LENGTH,
) -> bytes:
    """Encode a JSON-RPC message with the given wire framing.

    Default ``content-length`` preserves back-compat for existing callers/tests.
    ``ndjson`` emits a single JSON line with a trailing newline (no headers) —
    the shape Grok Build CLI expects.
    """
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if framing == FRAMING_NDJSON:
        return body + b"\n"
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _record_framing(holder: list[str] | None, framing: str) -> None:
    """Record framing on first detection (connection-level lock)."""
    if holder is not None and not holder:
        holder.append(framing)


def read_message(
    stream: BinaryIO,
    *,
    framing_out: list[str] | None = None,
) -> dict[str, Any] | None:
    """Read one framed or newline-delimited JSON-RPC message. None on EOF.

    Auto-detects framing from the first byte:
    - ``{`` / whitespace → NDJSON line
    - else → Content-Length header framing

    When ``framing_out`` is provided, the detected framing is appended once
    (empty list → first detection wins). Callers should lock responses to that
    framing for the rest of the connection.
    """
    # Peek: if first bytes look like Content-Length, use framing; else NDJSON line.
    first = stream.read(1)
    if not first:
        return None
    # Content-Length starts with 'C'; NDJSON typically with '{'
    if first in (b"{", b" ", b"\t", b"\n", b"\r"):
        # NDJSON path — finish the line (record framing before parse so parse
        # errors still let the server reply in NDJSON).
        _record_framing(framing_out, FRAMING_NDJSON)
        rest = stream.readline(MAX_WIRE_BYTES + 2)
        wire = first + rest
        if not wire.endswith(b"\n"):
            if len(wire) > MAX_WIRE_BYTES:
                raise ValueError("MCP body exceeds bounded wire limit")
            raise ValueError("incomplete MCP NDJSON line: newline required")
        body = wire[:-1]
        if body.endswith(b"\r"):
            body = body[:-1]
        if len(body) > MAX_WIRE_BYTES:
            raise ValueError("MCP body exceeds bounded wire limit")
        line = body.decode("utf-8", errors="replace").strip()
        if not line:
            return read_message(stream, framing_out=framing_out)
        return json.loads(line)

    # Header path
    _record_framing(framing_out, FRAMING_CONTENT_LENGTH)
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
    if length < 0 or length > MAX_WIRE_BYTES:
        raise ValueError("MCP body exceeds bounded wire limit")
    body = remainder.encode("utf-8") if remainder else b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise ValueError("truncated MCP body")
        body += chunk
    return json.loads(body.decode("utf-8"))


def write_message(
    stream: BinaryIO,
    message: dict[str, Any],
    framing: str = FRAMING_CONTENT_LENGTH,
) -> None:
    stream.write(encode_message(message, framing=framing))
    stream.flush()


def run_stdio_server(
    *,
    root: Path | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    """Serve until stdin EOF. Returns process exit code.

    Response framing matches the client's framing (first message locks it).
    """
    # Ensure the guard marker covers the in-process server lifetime even if the
    # caller forgot (main sets it too), then restore the caller environment.
    # Tests and embedding hosts may run the server in-process; leaking this
    # process-wide marker would incorrectly disable later CLI acceptance.
    from omg_cli.acceptance import MCP_SERVER_ENV

    marker_present = MCP_SERVER_ENV in os.environ
    marker_previous = os.environ.get(MCP_SERVER_ENV)
    os.environ[MCP_SERVER_ENV] = "1"
    runtime: MCPRuntime | None = None
    request_pool: concurrent.futures.ThreadPoolExecutor | None = None
    try:
        in_stream = stdin if stdin is not None else sys.stdin.buffer
        out_stream = stdout if stdout is not None else sys.stdout.buffer
        project = Path(root).resolve() if root is not None else Path.cwd().resolve()
        timeout_raw = os.environ.get("OMG_MCP_CALL_TIMEOUT_SECONDS", "").strip()
        try:
            timeout = (
                float(timeout_raw) if timeout_raw else DEFAULT_CALL_TIMEOUT_SECONDS
            )
        except ValueError:
            timeout = DEFAULT_CALL_TIMEOUT_SECONDS
        timeout = min(max(timeout, 0.001), MAX_CALL_TIMEOUT_SECONDS)
        runtime = MCPRuntime(call_timeout_seconds=timeout)
        request_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_CALLS,
            thread_name_prefix="omg-mcp-request",
        )

        # First successful framing detection locks the connection framing.
        # Default only used if a parse error somehow precedes any detection.
        framing_holder: list[str] = []

        def _conn_framing() -> str:
            return framing_holder[0] if framing_holder else FRAMING_CONTENT_LENGTH

        write_lock = threading.Lock()
        pending: set[concurrent.futures.Future[None]] = set()

        def _write(response: dict[str, Any]) -> None:
            with write_lock:
                write_message(out_stream, response, framing=_conn_framing())

        def _serve(message: dict[str, Any]) -> None:
            try:
                response = handle_message(message, root=project, runtime=runtime)
            except Exception as exc:  # noqa: BLE001 — keep server up
                response = _error_response(
                    message.get("id") if isinstance(message, dict) else None,
                    -32603,
                    f"Internal error: {exc}",
                )
            if response is not None:
                _write(response)

        while True:
            finished = {future for future in pending if future.done()}
            for future in finished:
                pending.remove(future)
                future.result()
            try:
                msg = read_message(in_stream, framing_out=framing_holder)
            except (ValueError, json.JSONDecodeError) as exc:
                err = _error_response(None, -32700, f"Parse error: {exc}")
                _write(err)
                continue
            if msg is None:
                break
            is_tool_call = (
                isinstance(msg, dict)
                and msg.get("method") == "tools/call"
                and "id" in msg
            )
            if is_tool_call:
                if len(pending) >= MAX_OUTSTANDING_CALLS:
                    _write(
                        _error_response(
                            msg.get("id"),
                            -32000,
                            "MCP outstanding request bound reached",
                        )
                    )
                else:
                    pending.add(request_pool.submit(_serve, msg))
                continue
            _serve(msg)
        for future in concurrent.futures.as_completed(pending):
            future.result()
    finally:
        if request_pool is not None:
            request_pool.shutdown(wait=True, cancel_futures=True)
        if runtime is not None:
            runtime.close()
        if marker_present:
            assert marker_previous is not None
            os.environ[MCP_SERVER_ENV] = marker_previous
        else:
            os.environ.pop(MCP_SERVER_ENV, None)
    return 0


def run_ndjson_roundtrip(
    requests: list[dict[str, Any]],
    *,
    root: Path | None = None,
) -> list[dict[str, Any] | None]:
    """In-process helper for tests: feed request dicts, collect responses."""
    runtime = MCPRuntime()
    try:
        return [handle_message(r, root=root, runtime=runtime) for r in requests]
    finally:
        runtime.close()


__all__ = [
    "FRAMING_CONTENT_LENGTH",
    "FRAMING_NDJSON",
    "MCPRuntime",
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
