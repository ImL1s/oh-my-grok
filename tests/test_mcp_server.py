"""Hermetic operation-level tests for the exact OMG MCP surface."""
from __future__ import annotations

import concurrent.futures
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

from omg_cli.mcp.server import (
    FRAMING_CONTENT_LENGTH,
    FRAMING_NDJSON,
    MAX_WIRE_BYTES,
    MCPRuntime,
    encode_message,
    handle_message,
    read_message,
    run_stdio_server,
)
from omg_cli.mcp.tools import (
    EXACT_TOOL_NAMES,
    FORBIDDEN_TOOL_NAMES,
    TOOL_HANDLERS,
    TOOL_SPECS,
    dispatch_tool,
    list_tool_names,
)
from omg_cli.project_memory import upsert_fact
from omg_cli.runtime_events import append_runtime_event, normalize_lifecycle_event
from omg_cli.state import create_run
from omg_cli.team.mailbox import send_message


def _error_code(payload: dict) -> str | None:
    error = payload.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _rpc_call(name: str, arguments: dict, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _event(root: Path, run_id: str = "run-a") -> None:
    append_runtime_event(
        root,
        normalize_lifecycle_event(
            source="test-source",
            source_cursor="cursor-1",
            source_sequence=0,
            event_id="event-a",
            event_type="turn_started",
            run_id=run_id,
            session_id="session-a",
            observed_at="2026-07-22T00:00:00Z",
            payload={"token": "secret-value", "detail": "safe"},
        ),
    )


def test_registry_is_exact_nine_operations() -> None:
    assert tuple(list_tool_names()) == EXACT_TOOL_NAMES
    assert tuple(spec["name"] for spec in TOOL_SPECS) == EXACT_TOOL_NAMES
    assert set(TOOL_HANDLERS) == set(EXACT_TOOL_NAMES)
    assert not set(EXACT_TOOL_NAMES) & FORBIDDEN_TOOL_NAMES
    assert all(not name.startswith("lsp.") for name in EXACT_TOOL_NAMES)


def test_run_status_read_operation(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="ralph", goal="inspect")
    result = dispatch_tool("run_status.read", {"run_id": run["run_id"]}, root=tmp_path)
    assert result["ok"] is True and result["found"] is True
    assert result["run"]["run_id"] == run["run_id"]


def test_trace_timeline_operation(tmp_path: Path) -> None:
    _event(tmp_path)
    result = dispatch_tool("trace.timeline", {"run_id": "run-a", "limit": 10}, root=tmp_path)
    assert result["ok"] is True
    assert [row["event_id"] for row in result["events"]] == ["event-a"]
    assert "secret-value" not in json.dumps(result)


def test_trace_summary_operation(tmp_path: Path) -> None:
    _event(tmp_path)
    result = dispatch_tool("trace.summary", {"session_id": "session-a"}, root=tmp_path)
    assert result == {
        "ok": True,
        "count": 1,
        "by_type": {"turn_started": 1},
        "by_source": {"test-source": 1},
        "first_observed_at": "2026-07-22T00:00:00Z",
        "last_observed_at": "2026-07-22T00:00:00Z",
    }


def test_resume_metadata_read_operation(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="ralph", goal="continue this")
    result = dispatch_tool(
        "resume_metadata.read", {"run_id": run["run_id"]}, root=tmp_path
    )
    assert result["ok"] is True
    assert result["metadata"]["run_id"] == run["run_id"]
    assert "resume_md" not in result["metadata"]


def test_project_memory_search_operation(tmp_path: Path) -> None:
    upsert_fact(
        tmp_path,
        key="build.command",
        value="python -m pytest",
        source="user",
        updated_at="2026-07-22T00:00:00Z",
    )
    result = dispatch_tool(
        "project_memory.search", {"query": "pytest", "limit": 4}, root=tmp_path
    )
    assert result["ok"] is True
    assert [row["key"] for row in result["hits"]] == ["build.command"]


def test_wiki_read_operation(tmp_path: Path) -> None:
    wiki = tmp_path / ".omg" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "release-safety.md").write_text("# Release\nverify gates\n", encoding="utf-8")
    page = dispatch_tool("wiki.read", {"slug": "release-safety"}, root=tmp_path)
    assert page["ok"] is True and "verify gates" in page["text"]
    hits = dispatch_tool("wiki.read", {"query": "gates"}, root=tmp_path)
    assert hits["hits"][0]["slug"] == "release-safety"


def test_team_status_read_operation(tmp_path: Path, monkeypatch) -> None:
    from omg_cli.team import plane

    monkeypatch.setattr(
        plane,
        "native_team_status",
        lambda *_a, **kw: {
            "run_id": kw["run_id"],
            "team_id": kw["team_id"],
            "tasks": [],
            "verified": False,
        },
    )
    result = dispatch_tool(
        "team_status.read", {"run_id": "run-a", "team_id": "team-a"}, root=tmp_path
    )
    assert result["ok"] is True and result["found"] is True
    assert result["team"]["verified"] is False


def test_mailbox_list_operation(tmp_path: Path) -> None:
    send_message(
        tmp_path,
        run_id="run-a",
        team_id="team-a",
        sender_id="leader",
        recipient_id="worker-a",
        generation=0,
        kind="task",
        body={"secret": "hidden", "instruction": "review"},
        dedupe_key="delivery-a",
    )
    result = dispatch_tool(
        "mailbox.list",
        {"run_id": "run-a", "team_id": "team-a", "recipient_id": "worker-a"},
        root=tmp_path,
    )
    assert result["ok"] is True and len(result["messages"]) == 1
    assert "body" not in result["messages"][0]


def test_proposal_create_operation_is_immutable_and_non_authoritative(tmp_path: Path) -> None:
    args = {"proposal_id": "proposal-a", "kind": "workflow", "payload": {"ship": False}}
    first = dispatch_tool("proposal.create", args, root=tmp_path)
    second = dispatch_tool("proposal.create", args, root=tmp_path)
    assert first["ok"] is True and first["authoritative"] is False
    assert second["duplicate"] is True
    path = tmp_path / first["path"]
    assert path.parent == tmp_path / ".omg" / "artifacts" / "mcp-proposals"
    assert os.stat(path).st_mode & 0o777 == 0o600
    conflict = dispatch_tool(
        "proposal.create", {**args, "payload": {"ship": True}}, root=tmp_path
    )
    assert _error_code(conflict) == "E_IMMUTABLE_CONFLICT"
    assert not (tmp_path / ".omg" / "state").exists()


def test_schema_invalid_unknown_field_and_non_object() -> None:
    assert _error_code(dispatch_tool("run_status.read", {"root": "/tmp"})) == "E_SCHEMA"
    assert _error_code(dispatch_tool("trace.timeline", {"limit": 0})) == "E_SCHEMA"
    assert _error_code(dispatch_tool("wiki.read", [])) == "E_SCHEMA"  # type: ignore[arg-type]


def test_unknown_and_forbidden_operations_are_structured() -> None:
    assert _error_code(dispatch_tool("missing.tool", {})) == "E_UNKNOWN_TOOL"
    assert _error_code(dispatch_tool("set_verified", {})) == "E_FORBIDDEN_TOOL"
    assert _error_code(dispatch_tool("lsp.hover", {})) == "E_FORBIDDEN_TOOL"


def test_root_escape_is_rejected(tmp_path: Path) -> None:
    result = dispatch_tool("wiki.read", {"slug": "../state"}, root=tmp_path)
    assert _error_code(result) == "E_PATH_ESCAPE"
    result = dispatch_tool(
        "proposal.create",
        {"proposal_id": "../state", "kind": "x", "payload": {}},
        root=tmp_path,
    )
    assert _error_code(result) == "E_OPERATION" or _error_code(result) == "E_SCHEMA"


def test_output_is_bounded(tmp_path: Path) -> None:
    wiki = tmp_path / ".omg" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "large.md").write_text("x" * 200, encoding="utf-8")
    result = dispatch_tool(
        "wiki.read", {"slug": "large"}, root=tmp_path, max_output_bytes=64
    )
    assert _error_code(result) == "E_OUTPUT_BOUND"


def test_cooperative_cancellation_and_deadline() -> None:
    cancelled = threading.Event()
    cancelled.set()
    assert _error_code(dispatch_tool("run_status.read", {}, cancel_event=cancelled)) == "E_CANCELLED"
    assert _error_code(
        dispatch_tool("run_status.read", {}, deadline=time.monotonic() - 1)
    ) == "E_TIMEOUT"


def test_concurrent_requests_are_thread_safe(tmp_path: Path) -> None:
    args = {"proposal_id": "shared", "kind": "test", "payload": {"same": True}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: dispatch_tool("proposal.create", args, root=tmp_path), range(16))
        )
    assert all(row["ok"] is True for row in results)
    assert sum(not row["duplicate"] for row in results) == 1


def test_protocol_initialize_list_call(tmp_path: Path) -> None:
    runtime = MCPRuntime()
    try:
        initialized = handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            root=tmp_path,
            runtime=runtime,
        )
        listed = handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            root=tmp_path,
            runtime=runtime,
        )
        called = handle_message(_rpc_call("trace.summary", {}, 3), root=tmp_path, runtime=runtime)
    finally:
        runtime.close()
    assert initialized and initialized["result"]["serverInfo"]["name"] == "omg"
    assert listed and tuple(row["name"] for row in listed["result"]["tools"]) == EXACT_TOOL_NAMES
    assert called and called["result"]["isError"] is False


def test_protocol_timeout_and_cancel(tmp_path: Path, monkeypatch) -> None:
    original = TOOL_HANDLERS["run_status.read"]

    def slow(args, root, context):
        time.sleep(0.2)
        return original(args, root, context)

    monkeypatch.setitem(TOOL_HANDLERS, "run_status.read", slow)
    runtime = MCPRuntime(call_timeout_seconds=0.03)
    timed = handle_message(_rpc_call("run_status.read", {}, 8), root=tmp_path, runtime=runtime)
    assert timed and timed["result"]["structuredContent"]["error"]["code"] == "E_TIMEOUT"

    runtime2 = MCPRuntime(call_timeout_seconds=1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(handle_message, _rpc_call("run_status.read", {}, 9), root=tmp_path, runtime=runtime2)
        time.sleep(0.02)
        handle_message(
            {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 9}},
            root=tmp_path,
            runtime=runtime2,
        )
        cancelled = future.result(timeout=1)
    runtime.close()
    runtime2.close()
    assert cancelled and cancelled["result"]["structuredContent"]["error"]["code"] == "E_CANCELLED"


def test_ndjson_and_content_length_framing() -> None:
    message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    for framing in (FRAMING_NDJSON, FRAMING_CONTENT_LENGTH):
        holder: list[str] = []
        decoded = read_message(io.BytesIO(encode_message(message, framing)), framing_out=holder)
        assert decoded == message and holder == [framing]


def test_ndjson_wire_limit_boundary_is_accepted() -> None:
    prefix = b'{"jsonrpc":"2.0","id":1}'
    body = prefix + (b" " * (MAX_WIRE_BYTES - len(prefix)))
    assert read_message(io.BytesIO(body + b"\n")) == {
        "jsonrpc": "2.0",
        "id": 1,
    }


def test_ndjson_oversize_and_missing_newline_fail_closed() -> None:
    prefix = b'{"jsonrpc":"2.0","id":1}'
    oversize = prefix + (b" " * (MAX_WIRE_BYTES + 1 - len(prefix))) + b"\n"
    with pytest.raises(ValueError, match="bounded wire limit"):
        read_message(io.BytesIO(oversize))
    with pytest.raises(ValueError, match="newline"):
        read_message(io.BytesIO(prefix))


def test_stdio_server_matches_ndjson_framing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OMG_MCP_SERVER", raising=False)
    inbound = io.BytesIO(encode_message({"jsonrpc": "2.0", "id": 1, "method": "ping"}, FRAMING_NDJSON))
    outbound = io.BytesIO()
    assert run_stdio_server(root=tmp_path, stdin=inbound, stdout=outbound) == 0
    assert outbound.getvalue().startswith(b"{") and outbound.getvalue().endswith(b"\n")
    assert "OMG_MCP_SERVER" not in os.environ


def test_stdio_server_restores_existing_guard_marker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OMG_MCP_SERVER", "outer")
    inbound = io.BytesIO(
        encode_message({"jsonrpc": "2.0", "id": 1, "method": "ping"}, FRAMING_NDJSON)
    )
    assert run_stdio_server(root=tmp_path, stdin=inbound, stdout=io.BytesIO()) == 0
    assert os.environ["OMG_MCP_SERVER"] == "outer"


def test_stdio_server_processes_wire_cancellation_while_call_runs(
    tmp_path: Path, monkeypatch
) -> None:
    original = TOOL_HANDLERS["run_status.read"]

    def cooperative_slow(args, root, context):
        while context.cancel_event is not None and not context.cancel_event.wait(0.005):
            context.checkpoint()
        return original(args, root, context)

    monkeypatch.setitem(TOOL_HANDLERS, "run_status.read", cooperative_slow)
    call = _rpc_call("run_status.read", {}, 77)
    cancel = {
        "jsonrpc": "2.0",
        "method": "$/cancelRequest",
        "params": {"id": 77},
    }
    inbound = io.BytesIO(
        encode_message(call, FRAMING_NDJSON)
        + encode_message(cancel, FRAMING_NDJSON)
    )
    outbound = io.BytesIO()
    assert run_stdio_server(root=tmp_path, stdin=inbound, stdout=outbound) == 0
    rows = [json.loads(line) for line in outbound.getvalue().splitlines()]
    assert len(rows) == 1
    assert rows[0]["id"] == 77
    assert rows[0]["result"]["structuredContent"]["error"]["code"] == "E_CANCELLED"


def test_runtime_pre_cancellation_memory_is_bounded() -> None:
    runtime = MCPRuntime()
    try:
        for request_id in range(2048):
            runtime.cancel(request_id)
        assert len(runtime._pre_cancelled) == 1024
    finally:
        runtime.close()
