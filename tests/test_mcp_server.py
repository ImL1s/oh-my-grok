"""Hermetic tests for focused in-session MCP server (omg mcp-server).

No live grok. Covers allowlist registry, path confinement, protocol round-trip,
wire framing (NDJSON vs Content-Length), and handlers over a tmp .omg root.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from omg_cli.mcp.server import (
    FRAMING_CONTENT_LENGTH,
    FRAMING_NDJSON,
    encode_message,
    handle_message,
    read_message,
    run_ndjson_roundtrip,
    run_stdio_server,
)
from omg_cli.mcp.tools import (
    FORBIDDEN_TOOL_NAMES,
    TOOL_HANDLERS,
    TOOL_SPECS,
    PathConfineError,
    assert_write_allowed,
    dispatch_tool,
    list_tool_names,
)
from omg_cli.state import create_run


# ---------------------------------------------------------------------------
# #1 Registry allowlist (fail-closed)
# ---------------------------------------------------------------------------


def test_registry_has_no_forbidden_tools() -> None:
    names = set(list_tool_names())
    assert names == set(TOOL_HANDLERS)
    bad = names & FORBIDDEN_TOOL_NAMES
    assert not bad, f"registry contains forbidden tools: {sorted(bad)}"
    # Explicit never-list from the brief
    for banned in (
        "accept",
        "omg_accept",
        "set_verified",
        "register_cli_acceptance_token",
        "state_write",
        "state_clear",
        "python_repl",
        "ast_grep_replace",
        "shared_memory",
        "session_search",
        "merge_readiness",
    ):
        assert banned not in names
        assert banned not in TOOL_HANDLERS


def test_registry_includes_expected_read_and_write_tools() -> None:
    names = set(list_tool_names())
    for expected in (
        "omg_state_status",
        "omg_state_read",
        "omg_state_list_active",
        "omg_note_read",
        "omg_note_write",
        "omg_wiki_query",
        "omg_wiki_list",
        "omg_wiki_ingest",
        "omg_project_memory_read",
        "omg_project_memory_add_note",
        "omg_artifact_write",
        "omg_lsp_symbols",
        "omg_lsp_diagnostics",
        "omg_resume_context",
    ):
        assert expected in names
    assert len(TOOL_SPECS) == len(names)


# ---------------------------------------------------------------------------
# #3 Path confinement
# ---------------------------------------------------------------------------


def test_assert_write_allowed_accepts_notepad_and_artifacts(tmp_path: Path) -> None:
    (tmp_path / ".omg").mkdir()
    (tmp_path / ".omg" / "artifacts").mkdir()
    (tmp_path / ".omg" / "wiki").mkdir()
    note = tmp_path / ".omg" / "notepad.md"
    art = tmp_path / ".omg" / "artifacts" / "proposal.md"
    wiki = tmp_path / ".omg" / "wiki" / "page.md"
    mem = tmp_path / ".omg" / "project-memory.json"
    for p in (note, art, wiki, mem):
        p.parent.mkdir(parents=True, exist_ok=True)
        assert_write_allowed(tmp_path, p, kind="test")


def test_assert_write_allowed_rejects_state(tmp_path: Path) -> None:
    target = tmp_path / ".omg" / "state" / "status.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(PathConfineError, match=r"\.omg/state"):
        assert_write_allowed(tmp_path, target, kind="probe")


def test_assert_write_allowed_rejects_dotdot(tmp_path: Path) -> None:
    (tmp_path / ".omg" / "artifacts").mkdir(parents=True)
    (tmp_path / ".omg" / "state").mkdir(parents=True)
    # Path that resolves into state via ..
    sneaky = tmp_path / ".omg" / "artifacts" / ".." / "state" / "status.json"
    with pytest.raises(PathConfineError):
        assert_write_allowed(tmp_path, sneaky, kind="probe")


def test_artifact_write_rejects_path_escape_names(tmp_path: Path) -> None:
    (tmp_path / ".omg" / "artifacts").mkdir(parents=True)
    for name in (
        "../state/status.json",
        "../../.omg/state/active.json",
        "foo/bar",
        "..",
        "acceptance.token",
        "status.json",
    ):
        out = dispatch_tool(
            "omg_artifact_write",
            {"name": name, "body": "forged"},
            root=tmp_path,
        )
        assert out.get("ok") is False, name
        assert out.get("confined") is True or "error" in out


def test_note_write_stays_on_notepad(tmp_path: Path) -> None:
    out = dispatch_tool(
        "omg_note_write",
        {"text": "hello from mcp"},
        root=tmp_path,
    )
    assert out.get("ok") is True
    path = Path(out["path"])
    assert path.name == "notepad.md"
    assert ".omg" in path.parts
    assert "state" not in path.parts
    text = path.read_text(encoding="utf-8")
    assert "hello from mcp" in text


def test_artifact_write_only_under_artifacts(tmp_path: Path) -> None:
    out = dispatch_tool(
        "omg_artifact_write",
        {"name": "plan-proposal.md", "body": "# proposal\n"},
        root=tmp_path,
    )
    assert out.get("ok") is True
    path = Path(out["path"])
    assert path.parent.name == "artifacts"
    assert path.read_text(encoding="utf-8").startswith("# proposal")


# ---------------------------------------------------------------------------
# READ / WRITE handlers over tmp root
# ---------------------------------------------------------------------------


def test_state_handlers(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="ralph", goal="mcp test")
    rid = run["run_id"]
    st = dispatch_tool("omg_state_status", {}, root=tmp_path)
    assert st.get("ok") is True
    assert "pack" in st
    one = dispatch_tool("omg_state_read", {"run_id": rid}, root=tmp_path)
    assert one.get("ok") is True
    assert one["run"]["run_id"] == rid
    listed = dispatch_tool("omg_state_list_active", {}, root=tmp_path)
    assert listed.get("ok") is True
    assert listed.get("active") is not None or any(
        r["run_id"] == rid for r in listed.get("runs", [])
    )


def test_wiki_and_memory_and_resume(tmp_path: Path) -> None:
    ing = dispatch_tool(
        "omg_wiki_ingest",
        {"title": "Auth", "body": "use tokens"},
        root=tmp_path,
    )
    assert ing.get("ok") is True
    pages = dispatch_tool("omg_wiki_list", {}, root=tmp_path)
    assert pages.get("ok") is True
    assert any(p["slug"] == "auth" for p in pages.get("pages", []))
    hits = dispatch_tool("omg_wiki_query", {"needle": "tokens"}, root=tmp_path)
    assert hits.get("ok") is True
    assert hits.get("hits")

    mem = dispatch_tool(
        "omg_project_memory_add_note",
        {"text": "prefer path confinement"},
        root=tmp_path,
    )
    assert mem.get("ok") is True
    read_m = dispatch_tool("omg_project_memory_read", {}, root=tmp_path)
    assert read_m.get("ok") is True
    assert read_m.get("exists") is True
    assert any("path confinement" in n.get("text", "") for n in read_m.get("notes", []))

    create_run(tmp_path, mode="ulw", goal="resume me")
    resume = dispatch_tool("omg_resume_context", {}, root=tmp_path)
    assert "pack" in resume


def test_lsp_handlers(tmp_path: Path) -> None:
    py = tmp_path / "sample.py"
    py.write_text("def hello():\n    return 1\n\nclass Foo:\n    pass\n", encoding="utf-8")
    sym = dispatch_tool("omg_lsp_symbols", {"path": str(py)}, root=tmp_path)
    assert sym.get("ok") is True
    names = {s["name"] for s in sym.get("symbols", [])}
    assert "hello" in names
    assert "Foo" in names
    diag = dispatch_tool("omg_lsp_diagnostics", {"path": str(py)}, root=tmp_path)
    assert diag.get("ok") is True
    assert diag.get("diagnostics") == []
    assert "syntax" in (diag.get("honesty") or "").lower() or "ast" in (
        diag.get("honesty") or ""
    ).lower()


def test_dispatch_unknown_and_forbidden() -> None:
    assert dispatch_tool("not_a_tool", {})["ok"] is False
    out = dispatch_tool("set_verified", {"run_id": "x"})
    assert out.get("ok") is False
    assert "forbidden" in out.get("error", "").lower() or "unknown" in out.get(
        "error", ""
    ).lower() or "forbidden" in out.get("error", "")


# ---------------------------------------------------------------------------
# MCP protocol in-process round-trip
# ---------------------------------------------------------------------------


def test_protocol_initialize_list_call(tmp_path: Path) -> None:
    responses = run_ndjson_roundtrip(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "omg_note_write",
                    "arguments": {"text": "from protocol"},
                },
            },
        ],
        root=tmp_path,
    )
    init, listed, called = responses
    assert init is not None and "result" in init
    assert init["result"]["serverInfo"]["name"] == "omg"
    assert "tools" in init["result"]["capabilities"]
    tools = listed["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "omg_note_write" in names
    assert "set_verified" not in names
    assert "accept" not in names
    assert called["result"]["isError"] is False
    structured = called["result"]["structuredContent"]
    assert structured.get("ok") is True
    assert "from protocol" in (tmp_path / ".omg" / "notepad.md").read_text(
        encoding="utf-8"
    )


def test_handle_message_method_not_found() -> None:
    resp = handle_message(
        {"jsonrpc": "2.0", "id": 9, "method": "nope/thing", "params": {}},
    )
    assert resp is not None
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_tools_call_forbidden_name_is_error(tmp_path: Path) -> None:
    resp = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "python_repl", "arguments": {}},
        },
        root=tmp_path,
    )
    assert resp is not None
    assert resp["result"]["isError"] is True


# ---------------------------------------------------------------------------
# Wire framing: respond in the client's framing (NDJSON vs Content-Length)
# Regression: Grok Build CLI sends NDJSON initialize and cannot parse
# Content-Length replies → "MCP server omg timed out after 30s".
# ---------------------------------------------------------------------------


def _initialize_request(req_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "g", "version": "1"},
        },
    }


def _tools_list_request(req_id: int = 2) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/list",
        "params": {},
    }


def _split_ndjson_responses(raw: bytes) -> list[dict]:
    """Parse bare NDJSON response bytes; fail if Content-Length headers appear."""
    assert b"Content-Length:" not in raw, (
        f"NDJSON client must not receive Content-Length framing, got: {raw[:200]!r}"
    )
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    assert lines, f"expected at least one NDJSON response line, got: {raw!r}"
    out: list[dict] = []
    for ln in lines:
        # Each response must be a single JSON object line (no headers).
        assert ln.lstrip().startswith(b"{"), f"expected bare JSON object, got: {ln!r}"
        out.append(json.loads(ln.decode("utf-8")))
    return out


def _split_content_length_responses(raw: bytes) -> list[dict]:
    """Parse Content-Length framed response bytes."""
    assert raw.startswith(b"Content-Length:") or b"Content-Length:" in raw
    msgs: list[dict] = []
    buf = io.BytesIO(raw)
    while True:
        framing: list[str] = []
        msg = read_message(buf, framing_out=framing)
        if msg is None:
            break
        assert framing == [FRAMING_CONTENT_LENGTH]
        msgs.append(msg)
    assert msgs, f"expected Content-Length framed responses, got: {raw[:200]!r}"
    return msgs


def test_encode_message_ndjson_has_no_content_length_header() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    raw = encode_message(msg, framing=FRAMING_NDJSON)
    assert raw.endswith(b"\n")
    assert b"Content-Length:" not in raw
    assert json.loads(raw.decode("utf-8").strip()) == msg


def test_encode_message_content_length_default_back_compat() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    raw = encode_message(msg)  # default framing
    assert raw.startswith(b"Content-Length:")
    body = raw.split(b"\r\n\r\n", 1)[1]
    assert json.loads(body.decode("utf-8")) == msg


def _run_stdio_server_isolated(
    *,
    root: Path,
    stdin: io.BytesIO,
    stdout: io.BytesIO,
) -> int:
    """Call run_stdio_server without leaking OMG_MCP_SERVER=1 into later tests."""
    from omg_cli.acceptance import MCP_SERVER_ENV

    prev = os.environ.get(MCP_SERVER_ENV)
    try:
        return run_stdio_server(root=root, stdin=stdin, stdout=stdout)
    finally:
        if prev is None:
            os.environ.pop(MCP_SERVER_ENV, None)
        else:
            os.environ[MCP_SERVER_ENV] = prev


def test_ndjson_stdio_roundtrip_responses_are_ndjson(tmp_path: Path) -> None:
    """Client sends NDJSON (Grok shape) → server must reply NDJSON, not headers.

    This is the exact live-capture mismatch: grok sends `{...}\\n` and previously
    received `Content-Length: N\\r\\n\\r\\n{...}` which it cannot parse.
    """
    reqs = [_initialize_request(1), _tools_list_request(2)]
    stdin_bytes = b"".join(
        (json.dumps(r, separators=(",", ":")) + "\n").encode("utf-8") for r in reqs
    )
    stdin = io.BytesIO(stdin_bytes)
    stdout = io.BytesIO()
    code = _run_stdio_server_isolated(root=tmp_path, stdin=stdin, stdout=stdout)
    assert code == 0
    raw = stdout.getvalue()
    responses = _split_ndjson_responses(raw)
    assert len(responses) == 2
    init, listed = responses
    assert init["id"] == 1
    assert "result" in init
    assert init["result"]["serverInfo"]["name"] == "omg"
    assert listed["id"] == 2
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "omg_note_write" in names
    assert "set_verified" not in names


def test_content_length_stdio_roundtrip_responses_are_content_length(
    tmp_path: Path,
) -> None:
    """Client sends Content-Length → server keeps Content-Length framing."""
    reqs = [_initialize_request(1), _tools_list_request(2)]
    stdin_bytes = b"".join(encode_message(r, framing=FRAMING_CONTENT_LENGTH) for r in reqs)
    stdin = io.BytesIO(stdin_bytes)
    stdout = io.BytesIO()
    code = _run_stdio_server_isolated(root=tmp_path, stdin=stdin, stdout=stdout)
    assert code == 0
    raw = stdout.getvalue()
    responses = _split_content_length_responses(raw)
    assert len(responses) == 2
    assert responses[0]["id"] == 1
    assert "result" in responses[0]
    assert responses[1]["id"] == 2
    assert "tools" in responses[1]["result"]


def test_read_message_detects_ndjson_framing() -> None:
    payload = b'{"jsonrpc":"2.0","id":0,"method":"ping"}\n'
    holder: list[str] = []
    msg = read_message(io.BytesIO(payload), framing_out=holder)
    assert msg == {"jsonrpc": "2.0", "id": 0, "method": "ping"}
    assert holder == [FRAMING_NDJSON]


def test_read_message_detects_content_length_framing() -> None:
    payload = encode_message(
        {"jsonrpc": "2.0", "id": 0, "method": "ping"},
        framing=FRAMING_CONTENT_LENGTH,
    )
    holder: list[str] = []
    msg = read_message(io.BytesIO(payload), framing_out=holder)
    assert msg == {"jsonrpc": "2.0", "id": 0, "method": "ping"}
    assert holder == [FRAMING_CONTENT_LENGTH]


# ---------------------------------------------------------------------------
# CLI smoke (optional subprocess-free via main)
# ---------------------------------------------------------------------------


def test_mcp_install_print_only(capsys: pytest.CaptureFixture[str]) -> None:
    from omg_cli.main import main

    code = main(["mcp-install", "--print-only"])
    assert code == 0
    out = capsys.readouterr().out
    assert "grok mcp add" in out
    assert "mcp-server" in out


def test_known_subcommands_include_mcp() -> None:
    from omg_cli.main import KNOWN_SUBCOMMANDS, build_parser

    assert "mcp-server" in KNOWN_SUBCOMMANDS
    assert "mcp-install" in KNOWN_SUBCOMMANDS
    parser = build_parser()
    # ensure parsers exist
    help_txt = parser.format_help()
    assert "mcp-server" in help_txt
    assert "mcp-install" in help_txt
