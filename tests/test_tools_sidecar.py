"""#73 tools sidecar — confinement, fake LSP protocol, honest blocked deps."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from omg_cli.mcp.tools import FORBIDDEN_TOOL_NAMES, dispatch_tool
from omg_cli.tools_sidecar import (
    SIDECAR_TOOL_NAMES,
    FakeLspTransport,
    ToolsError,
    ast_replace,
    ast_search,
    codegraph_query,
    codegraph_status,
    confine_path,
    dispatch_sidecar_tool,
    doctor_payload,
    handle_mcp_rpc,
    inspect_tools_sidecar,
    lsp_operation,
    media_descriptor,
    research_search,
    research_status,
)

ROOT = Path(__file__).resolve().parents[1]


def test_sidecar_names_do_not_collide_with_mcp_server_forbid_list() -> None:
    assert not set(SIDECAR_TOOL_NAMES) & FORBIDDEN_TOOL_NAMES
    assert "lsp.hover" not in SIDECAR_TOOL_NAMES


def test_mcp_server_still_forbids_semantic_lsp_names() -> None:
    payload = dispatch_tool("lsp.hover", {})
    error = payload.get("error")
    assert isinstance(error, dict)
    assert error.get("code") == "E_FORBIDDEN_TOOL"


def test_confine_rejects_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    with pytest.raises(ToolsError, match="E_PATH_ESCAPE"):
        confine_path(tmp_path, str(outside))


def test_confine_accepts_workspace_file(tmp_path: Path) -> None:
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("x = 1\n", encoding="utf-8")
    assert confine_path(tmp_path, "src/a.py") == target.resolve()


def test_confine_rejects_nul(tmp_path: Path) -> None:
    with pytest.raises(ToolsError, match="E_PATH"):
        confine_path(tmp_path, "a\x00.py")


def test_media_descriptor_rejects_inline_bytes() -> None:
    with pytest.raises(ToolsError, match="E_MEDIA_INLINE"):
        media_descriptor(mime="image/png", byte_length=10, inline_bytes=b"abc")
    with pytest.raises(ToolsError, match="E_MEDIA_INLINE"):
        media_descriptor(mime="image/png", byte_length=10, base64="aaaa")
    row = media_descriptor(mime="image/png", byte_length=10, relpath="shots/a.png")
    assert row["inline_bytes"] is False
    assert row["base64"] is None


def test_fake_lsp_protocol_operations(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    transport = FakeLspTransport()
    for op in (
        "hover",
        "definition",
        "references",
        "document_symbols",
        "workspace_symbols",
        "diagnostics",
        "prepare_rename",
        "code_action_resolve",
    ):
        result = lsp_operation(
            op, root=tmp_path, path="a.py", transport=transport, query="Fake"
        )
        assert result["ok"] is True
        assert result["verified"] is False
    transport.close()
    assert transport.closed is True


def test_lsp_servers_inventory_never_claims_healthy() -> None:
    result = lsp_operation("servers", root=ROOT)
    assert result["verified"] is False
    assert result["healthy"] is False
    assert result["observed"] is False


def test_lsp_without_transport_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    with pytest.raises(ToolsError, match="E_LSP_NO_SERVER"):
        lsp_operation("hover", root=tmp_path, path="a.py")


def test_rename_apply_requires_read_write(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    transport = FakeLspTransport()
    preview = lsp_operation(
        "rename",
        root=tmp_path,
        path="a.py",
        transport=transport,
        apply=False,
        capability_mode="read-only",
        new_name="Y",
    )
    assert preview["apply"] is False
    with pytest.raises(ToolsError, match="E_READ_ONLY"):
        lsp_operation(
            "rename",
            root=tmp_path,
            path="a.py",
            transport=transport,
            apply=True,
            capability_mode="read-only",
            new_name="Y",
        )
    with pytest.raises(ToolsError, match="E_LSP_APPLY_UNSUPPORTED"):
        lsp_operation(
            "rename",
            root=tmp_path,
            path="a.py",
            transport=transport,
            apply=True,
            capability_mode="read-write",
            new_name="Y",
        )


def test_execute_capability_mode_rejected() -> None:
    with pytest.raises(ToolsError, match="E_CAPABILITY_MODE"):
        dispatch_sidecar_tool(
            "omg.tools.doctor",
            {"capability_mode": "execute"},
            root=ROOT,
            capability_mode="execute",
        )


def test_mcp_cannot_escalate_server_capability_mode(tmp_path: Path) -> None:
    with pytest.raises(ToolsError, match="E_CAPABILITY_MODE"):
        dispatch_sidecar_tool(
            "omg.tools.ast.replace",
            {
                "pattern": "foo",
                "rewrite": "bar",
                "write": True,
                "capability_mode": "read-write",
            },
            root=tmp_path,
            capability_mode="read-only",
        )


def test_ast_missing_is_blocked_not_fake(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("omg_cli.tools_sidecar.shutil.which", lambda _name: None)
    with pytest.raises(ToolsError, match="E_ASTGREP_MISSING"):
        ast_search(root=tmp_path, pattern="foo", lang="python")


def test_ast_replace_defaults_dry_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("omg_cli.tools_sidecar.shutil.which", lambda _name: None)
    with pytest.raises(ToolsError, match="E_ASTGREP_MISSING"):
        ast_replace(root=tmp_path, pattern="foo", rewrite="bar", lang="python", write=False)


def test_ast_replace_write_requires_read_write(tmp_path: Path) -> None:
    with pytest.raises(ToolsError, match="E_READ_ONLY"):
        ast_replace(
            root=tmp_path,
            pattern="foo",
            rewrite="bar",
            lang="python",
            write=True,
            capability_mode="read-only",
        )


def test_ast_rejects_shell_snippet(tmp_path: Path) -> None:
    with pytest.raises(ToolsError, match="E_AST_PATTERN"):
        ast_search(root=tmp_path, pattern="foo && rm -rf /", lang="python")


def test_codegraph_modes_and_honesty(tmp_path: Path) -> None:
    off = codegraph_status(root=tmp_path, mode="off")
    assert off["effective_mode"] == "off"
    assert off["branch_accurate"] is False
    assert off["verified"] is False
    shared = codegraph_status(root=tmp_path, mode="shared")
    assert shared["effective_mode"] == "shared"
    assert shared["branch_accurate"] is False
    assert "uncommitted" in shared["note"]
    local = codegraph_status(root=tmp_path, mode="local")
    assert local["branch_accurate"] is True
    with pytest.raises(ToolsError, match="E_CODEGRAPH_NO_INDEX"):
        codegraph_query(root=tmp_path, mode="local", query="Foo")
    with pytest.raises(ToolsError, match="E_CODEGRAPH_OFF"):
        codegraph_query(root=tmp_path, mode="off", query="Foo")


def test_research_opt_in_default_off() -> None:
    status = research_status(env={})
    assert status["enabled"] is False
    assert status["credentials_bundled"] is False
    with pytest.raises(ToolsError, match="E_NETWORK_DISABLED"):
        research_search("grok", env={})
    with pytest.raises(ToolsError, match="E_NETWORK_NO_PROVIDER"):
        research_search("grok", env={"OMG_TOOLS_NETWORK": "1"})


def test_doctor_never_verified(tmp_path: Path) -> None:
    payload = doctor_payload(root=tmp_path)
    assert payload["verified"] is False
    assert payload["observed"] is False
    assert payload["healthy"] is False
    assert payload["omg_lsp_host_owned"] is True
    inspect = inspect_tools_sidecar(tmp_path)
    assert inspect["verified"] is False


def test_doctor_strict_fails_enabled_network_without_provider(tmp_path: Path) -> None:
    payload = doctor_payload(
        root=tmp_path, env={"OMG_TOOLS_NETWORK": "1"}, strict=True
    )
    assert payload["ok"] is False


def test_mcp_rpc_lists_sidecar_tools_not_forbidden_names(tmp_path: Path) -> None:
    listed = handle_mcp_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, root=tmp_path)
    names = {row["name"] for row in listed["result"]["tools"]}
    assert names == set(SIDECAR_TOOL_NAMES)
    assert "lsp.hover" not in names
    called = handle_mcp_rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "omg.tools.research.search", "arguments": {"query": "x"}},
        },
        root=tmp_path,
        env={},
    )
    assert called["result"]["isError"] is True
    assert called["result"]["verified"] is False


def test_stdio_ndjson_initialize_and_doctor(tmp_path: Path) -> None:
    from omg_cli.tools_sidecar import run_tools_stdio

    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "omg.tools.doctor", "arguments": {}},
            }
        )
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown"})
        + "\n"
    )
    stdout = io.StringIO()
    assert run_tools_stdio(tmp_path, stdin=stdin, stdout=stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
    assert lines[0]["result"]["serverInfo"]["name"] == "omg-tools-sidecar"
    body = json.loads(lines[1]["result"]["content"][0]["text"])
    assert body["verified"] is False


def test_omg_lsp_host_owned_unchanged() -> None:
    from omg_cli.main import main

    code = main(["lsp", "symbols", "sample.py"])
    assert code == 1


def test_cli_tools_doctor_and_codegraph(tmp_path: Path) -> None:
    from omg_cli.main import main

    assert main(["tools", "doctor", "--root", str(tmp_path)]) == 0
    assert main(["tools", "codegraph", "status", "--mode", "shared", "--root", str(tmp_path)]) == 0


def test_cli_tools_lsp_fake_hover(tmp_path: Path, capsys) -> None:
    from omg_cli.main import main

    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    assert (
        main(
            [
                "tools",
                "lsp",
                "hover",
                "--fake-lsp",
                "--path",
                "a.py",
                "--root",
                str(tmp_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["verified"] is False


def test_capabilities_embeds_tools_sidecar() -> None:
    text = (ROOT / "omg_cli" / "commands" / "inspect.py").read_text(encoding="utf-8")
    assert "inspect_tools_sidecar" in text
    assert '"tools_sidecar": inspect_tools_sidecar' in text


def test_lsp_timeout_and_crash_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    crashing = FakeLspTransport(crash_on="textDocument/hover")
    with pytest.raises(ToolsError, match="E_LSP_CRASH"):
        lsp_operation("hover", root=tmp_path, path="a.py", transport=crashing)
    hanging = FakeLspTransport(hang_on="textDocument/hover")
    with pytest.raises(ToolsError, match="E_LSP_TIMEOUT"):
        lsp_operation("hover", root=tmp_path, path="a.py", transport=hanging)


def test_lsp_initialize_precedes_semantic_request(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    transport = FakeLspTransport()
    lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
    names = [name for name, _ in transport.calls]
    assert names[0] == "initialize"
    assert "initialized" in names
    assert "textDocument/didOpen" in names
    assert "textDocument/hover" in names


def test_lsp_forwards_requested_position(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha\nbeta\n", encoding="utf-8")
    transport = FakeLspTransport()
    lsp_operation(
        "hover",
        root=tmp_path,
        path="a.py",
        transport=transport,
        line=1,
        character=2,
    )
    hover = next(params for name, params in transport.calls if name == "textDocument/hover")
    assert hover["position"] == {"line": 1, "character": 2}


def test_lsp_did_open_each_uri(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a=1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b=1\n", encoding="utf-8")
    transport = FakeLspTransport()
    lsp_operation("workspace_symbols", root=tmp_path, transport=transport, query="x")
    lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
    lsp_operation("hover", root=tmp_path, path="b.py", transport=transport)
    opens = [params for name, params in transport.calls if name == "textDocument/didOpen"]
    assert len(opens) == 2
    uris = {item["textDocument"]["uri"] for item in opens}
    assert any(uri.endswith("a.py") for uri in uris)
    assert any(uri.endswith("b.py") for uri in uris)


def test_stdio_missing_command_is_tools_error(tmp_path: Path) -> None:
    from omg_cli.tools_sidecar import StdioLspTransport

    with pytest.raises(ToolsError, match="E_LSP_COMMAND"):
        StdioLspTransport(["omg-missing-language-server-zzz"], cwd=tmp_path)


def test_mcp_lsp_hover_uses_supplied_transport(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    transport = FakeLspTransport()
    called = handle_mcp_rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "omg.tools.lsp.hover", "arguments": {"path": "a.py"}},
        },
        root=tmp_path,
        transport=transport,
    )
    assert called["result"]["isError"] is False
    assert "initialize" in {name for name, _ in transport.calls}


def test_stdio_transport_timeout_does_not_block(tmp_path: Path) -> None:
    import sys

    from omg_cli.tools_sidecar import StdioLspTransport

    script = tmp_path / "hang_lsp.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=0.3
    )
    try:
        with pytest.raises(ToolsError, match="E_LSP_TIMEOUT"):
            transport.request("initialize", {})
    finally:
        transport.close()


def test_stdio_transport_reads_full_frame_from_raw_pipe(tmp_path: Path) -> None:
    from omg_cli.tools_sidecar import StdioLspTransport

    script = tmp_path / "full_frame_lsp.py"
    script.write_text(
        "import json, sys, time\n"
        "raw = json.dumps({'jsonrpc':'2.0','id':1,'result':{'ok':True}}).encode()\n"
        "sys.stdout.buffer.write("
        "f'Content-Length: {len(raw)}\\r\\n\\r\\n'.encode() + raw)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=2.0
    )
    try:
        assert transport.request("initialize", {}) == {"ok": True}
    finally:
        transport.close()


def test_stdio_transport_skips_notifications_until_matching_id(
    tmp_path: Path,
) -> None:
    from omg_cli.tools_sidecar import StdioLspTransport

    script = tmp_path / "notify_then_result.py"
    script.write_text(
        "import json, sys, time\n"
        "def _write(msg):\n"
        "    raw = json.dumps(msg).encode()\n"
        "    sys.stdout.buffer.write("
        "f'Content-Length: {len(raw)}\\r\\n\\r\\n'.encode() + raw)\n"
        "    sys.stdout.buffer.flush()\n"
        "_write({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics',"
        "'params':{'uri':'file:///x'}})\n"
        "_write({'jsonrpc':'2.0','id':1,'result':{'contents':'ok'}})\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=2.0
    )
    try:
        assert transport.request("textDocument/hover", {}) == {"contents": "ok"}
    finally:
        transport.close()


def test_ast_ignores_unrelated_sg_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "sg_help.py"
    helper.write_text(
        "print('Usage: sg group [[-c] command]')\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        fake = tmp_path / "sg.cmd"
        fake.write_text(f'@"{sys.executable}" "{helper}" %*\r\n', encoding="utf-8")
    else:
        fake = tmp_path / "sg"
        fake.write_text(
            f"#!/bin/sh\nexec '{sys.executable}' '{helper}' \"$@\"\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(
        "omg_cli.tools_sidecar.shutil.which",
        lambda name: str(fake) if name == "sg" else None,
    )
    with pytest.raises(ToolsError, match="E_ASTGREP_MISSING"):
        ast_search(root=tmp_path, pattern="foo", lang="python")
