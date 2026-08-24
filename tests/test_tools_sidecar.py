"""#73 tools sidecar — confinement, fake LSP protocol, honest blocked deps."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from omg_cli.mcp.tools import FORBIDDEN_TOOL_NAMES, dispatch_tool
from omg_cli.tools_sidecar import (
    MAX_RESULT_BYTES,
    RESEARCH_TIMEOUT_S,
    RESEARCH_USER_AGENT,
    SIDECAR_TOOL_NAMES,
    FakeLspTransport,
    StdioLspTransport,
    ToolsError,
    _astgrep_bin,
    _workspace_configuration_result,
    ensure_lsp_session,
    ast_replace,
    ast_search,
    codegraph_index,
    codegraph_query,
    codegraph_status,
    confine_path,
    dispatch_sidecar_tool,
    doctor_payload,
    handle_mcp_rpc,
    inspect_tools_sidecar,
    inventory_lsp_servers,
    lsp_operation,
    normalize_lsp_argv,
    media_descriptor,
    research_search,
    research_status,
)

ROOT = Path(__file__).resolve().parents[1]


class _FakeResearchResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.code = status

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._body
        return self._body[:n]

    def __enter__(self) -> "_FakeResearchResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _opensearch_json(query: str = "rust-analyzer") -> bytes:
    return json.dumps(
        [
            query,
            ["Rust (programming language)", "Static program analysis"],
            [
                "Rust is a general-purpose programming language.",
                "Static program analysis is the analysis of computer programs.",
            ],
            [
                "https://en.wikipedia.org/wiki/Rust_(programming_language)",
                "https://en.wikipedia.org/wiki/Static_program_analysis",
            ],
        ]
    ).encode("utf-8")


def _fake_opensearch_urlopen(body: bytes | BaseException):
    def _urlopen(request: urllib.request.Request, *, timeout: float):
        assert timeout == RESEARCH_TIMEOUT_S == 8.0
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers.get("user-agent") == RESEARCH_USER_AGENT
        assert RESEARCH_USER_AGENT == "oh-my-grok-tools-sidecar/research"
        assert "action=opensearch" in request.full_url
        assert "limit=5" in request.full_url
        assert "namespace=0" in request.full_url
        assert "format=json" in request.full_url
        if isinstance(body, BaseException):
            raise body
        return _FakeResearchResponse(body)

    return _urlopen


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
        "code_action",
        "code_action_resolve",
    ):
        result = lsp_operation(
            op, root=tmp_path, path="a.py", transport=transport, query="Fake"
        )
        assert result["ok"] is True
        assert result["verified"] is False
        assert result["truncated"] is False
    transport.close()
    assert transport.closed is True


def test_lsp_servers_inventory_never_claims_healthy() -> None:
    result = lsp_operation("servers", root=ROOT)
    assert result["verified"] is False
    assert result["healthy"] is False
    assert result["observed"] is False
    for row in result["servers"]:
        assert row["ready"] is False
        assert "not started" in row["note"]


def test_detected_language_server_is_not_ready() -> None:
    rows = inventory_lsp_servers()
    assert rows
    for row in rows:
        assert row["ready"] is False
        if row["available"]:
            assert row["path"]
            assert "live-verified" in row["note"]


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


def test_codegraph_index_requires_read_write(tmp_path: Path) -> None:
    with pytest.raises(ToolsError, match="E_READ_ONLY"):
        dispatch_sidecar_tool(
            "omg.tools.codegraph.index",
            {"mode": "local"},
            root=tmp_path,
            capability_mode="read-only",
        )


def test_ast_missing_is_blocked_not_fake(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("omg_cli.tools_sidecar.shutil.which", lambda _name: None)
    monkeypatch.setattr("omg_cli.tools_sidecar.Path.home", lambda: tmp_path)
    monkeypatch.delenv("CARGO_HOME", raising=False)
    with pytest.raises(ToolsError, match="E_ASTGREP_MISSING"):
        ast_search(root=tmp_path, pattern="foo", lang="python")


def test_ast_replace_defaults_dry_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("omg_cli.tools_sidecar.shutil.which", lambda _name: None)
    monkeypatch.setattr("omg_cli.tools_sidecar.Path.home", lambda: tmp_path)
    monkeypatch.delenv("CARGO_HOME", raising=False)
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
    assert off["index_present"] is False
    shared = codegraph_status(root=tmp_path, mode="shared")
    assert shared["effective_mode"] == "shared"
    assert shared["branch_accurate"] is False
    assert "uncommitted" in shared["note"]
    local = codegraph_status(root=tmp_path, mode="local")
    assert local["index_present"] is False
    assert local["branch_accurate"] is False
    assert local["not_scip"] is True
    with pytest.raises(ToolsError, match="E_CODEGRAPH_NO_INDEX"):
        codegraph_query(root=tmp_path, mode="local", query="Foo")
    with pytest.raises(ToolsError, match="E_CODEGRAPH_OFF"):
        codegraph_query(root=tmp_path, mode="off", query="Foo")


def test_research_opt_in_default_off() -> None:
    status = research_status(env={})
    assert status["enabled"] is False
    assert status["credentials_bundled"] is False
    assert status["provider"] is None
    with pytest.raises(ToolsError, match="E_NETWORK_DISABLED"):
        research_search("grok", env={})
    with pytest.raises(ToolsError, match="E_NETWORK_NO_PROVIDER"):
        research_search(
            "grok",
            env={"OMG_TOOLS_NETWORK": "1", "OMG_TOOLS_RESEARCH_PROVIDER": "none"},
        )


def test_research_network_on_provider_off_is_no_provider() -> None:
    with pytest.raises(ToolsError, match="E_NETWORK_NO_PROVIDER"):
        research_search(
            "grok",
            env={"OMG_TOOLS_NETWORK": "1", "OMG_TOOLS_RESEARCH_PROVIDER": "off"},
        )


def test_research_search_wikipedia_opensearch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omg_cli.tools_sidecar._research_urlopen",
        _fake_opensearch_urlopen(_opensearch_json()),
    )
    env = {"OMG_TOOLS_NETWORK": "1"}
    status = research_status(env=env)
    assert status["enabled"] is True
    assert status["provider"] == "wikipedia"
    assert status["credentials_bundled"] is False
    assert status["verified"] is False
    result = research_search("rust-analyzer", env=env)
    assert result["ok"] is True
    assert result["verified"] is False
    assert result.get("passes") is not True
    assert result["query"] == "rust-analyzer"
    assert result["provider"] == "wikipedia"
    assert result["count"] == 2
    assert result["hits"][0]["title"] == "Rust (programming language)"
    assert "wikipedia.org" in result["hits"][0]["url"]
    assert result["hits"][0]["snippet"]
    explicit = research_search(
        "rust-analyzer",
        env={"OMG_TOOLS_NETWORK": "1", "OMG_TOOLS_RESEARCH_PROVIDER": "wikipedia"},
    )
    assert explicit["hits"][0]["url"]


def test_cli_research_search_wikipedia(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omg_cli.main import main

    monkeypatch.setenv("OMG_TOOLS_NETWORK", "1")
    monkeypatch.delenv("OMG_TOOLS_RESEARCH_PROVIDER", raising=False)
    monkeypatch.setattr(
        "omg_cli.tools_sidecar._research_urlopen",
        _fake_opensearch_urlopen(_opensearch_json()),
    )
    assert main(["tools", "research", "search", "--query", "rust-analyzer"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    result = payload["result"]
    assert result["ok"] is True
    assert result["verified"] is False
    assert payload.get("verified") is not True
    assert result["provider"] == "wikipedia"
    assert result["hits"]
    assert result["hits"][0]["title"]
    assert "wikipedia.org" in result["hits"][0]["url"]


@pytest.mark.parametrize("kind", ("http_500", "timeout", "urlerror_timeout"))
def test_research_search_http_failure_is_provider_error(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "http_500":
        body: BaseException = urllib.error.HTTPError(
            "https://en.wikipedia.org/w/api.php",
            500,
            "Internal Server Error",
            None,
            io.BytesIO(b""),
        )
    elif kind == "timeout":
        body = TimeoutError("timed out")
    else:
        body = urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(
        "omg_cli.tools_sidecar._research_urlopen",
        _fake_opensearch_urlopen(body),
    )
    with pytest.raises(ToolsError, match="E_NETWORK_PROVIDER") as caught:
        research_search("rust-analyzer", env={"OMG_TOOLS_NETWORK": "1"})
    assert caught.value.code == "E_NETWORK_PROVIDER"
    if kind == "http_500":
        assert "500" in caught.value.message
    else:
        assert "timed out" in caught.value.message


def test_research_search_non_json_is_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omg_cli.tools_sidecar._research_urlopen",
        _fake_opensearch_urlopen(b"<html>nope</html>"),
    )
    with pytest.raises(ToolsError, match="E_NETWORK_PROVIDER"):
        research_search("rust-analyzer", env={"OMG_TOOLS_NETWORK": "1"})


def test_research_search_unexpected_shape_is_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omg_cli.tools_sidecar._research_urlopen",
        _fake_opensearch_urlopen(json.dumps({"error": "nope"}).encode("utf-8")),
    )
    with pytest.raises(ToolsError, match="E_NETWORK_PROVIDER"):
        research_search("rust-analyzer", env={"OMG_TOOLS_NETWORK": "1"})


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
        root=tmp_path,
        env={"OMG_TOOLS_NETWORK": "1", "OMG_TOOLS_RESEARCH_PROVIDER": "none"},
        strict=True,
    )
    assert payload["ok"] is False
    errors = payload.get("errors") or []
    assert any("no provider" in str(item) for item in errors)


def test_doctor_strict_wikipedia_has_no_provider_error(tmp_path: Path) -> None:
    payload = doctor_payload(
        root=tmp_path, env={"OMG_TOOLS_NETWORK": "1"}, strict=True
    )
    assert payload["ok"] is True
    blob = json.dumps(payload)
    assert "no provider" not in blob
    research = payload["dependencies"]["network_research"]
    assert research["provider"] == "wikipedia"
    assert research["credentials_bundled"] is False
    explicit = doctor_payload(
        root=tmp_path,
        env={"OMG_TOOLS_NETWORK": "1", "OMG_TOOLS_RESEARCH_PROVIDER": "wikipedia"},
        strict=True,
    )
    assert explicit["ok"] is True
    assert "no provider" not in json.dumps(explicit)


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
    assert payload["result"]["truncated"] is False
    assert payload["result"]["result"]["contents"]["value"] == "fake hover"


def _assert_fake_semantic_lsp(op: str, envelope: dict) -> None:
    assert envelope["ok"] is True
    assert envelope["verified"] is False
    result = envelope["result"]
    blob = json.dumps(result)
    assert result
    if op == "definition":
        assert isinstance(result, list) and result
        loc = result[0]
        assert loc.get("uri")
        assert "range" in loc
        assert "a.py" in loc["uri"] or "Fake" in loc["uri"]
    elif op == "references":
        assert isinstance(result, list) and result
        loc = result[0]
        assert loc.get("uri")
        assert "range" in loc
        assert "Fake" in blob
    elif op == "document_symbols":
        assert any(row.get("name") == "Fake" for row in result)
    elif op == "workspace_symbols":
        assert any(row.get("name") == "Fake" for row in result)
        assert "Fake.py" in blob
    else:
        raise AssertionError(op)


@pytest.mark.parametrize(
    "op",
    ("definition", "references", "document_symbols", "workspace_symbols"),
)
def test_lsp_operation_fake_semantic_ops(tmp_path: Path, op: str) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    transport = FakeLspTransport()
    envelope = lsp_operation(
        op,
        root=tmp_path,
        path="a.py",
        transport=transport,
        query="Fake",
    )
    _assert_fake_semantic_lsp(op, envelope)


@pytest.mark.parametrize(
    "op",
    ("definition", "references", "document_symbols", "workspace_symbols"),
)
def test_cli_tools_lsp_fake_semantic_ops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], op: str
) -> None:
    from omg_cli.main import main

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert (
        main(
            [
                "tools",
                "lsp",
                op,
                "--fake-lsp",
                "--path",
                "a.py",
                "--query",
                "Fake",
                "--root",
                str(tmp_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload.get("verified") is not True
    _assert_fake_semantic_lsp(op, payload["result"])


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
    init = next(params for name, params in transport.calls if name == "initialize")
    assert init["capabilities"]["workspace"]["configuration"] is True


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


def test_workspace_configuration_empty_settings() -> None:
    assert _workspace_configuration_result(None) == []
    assert _workspace_configuration_result({}) == []
    assert _workspace_configuration_result({"items": "python"}) == []
    assert _workspace_configuration_result({"items": []}) == []
    assert _workspace_configuration_result(
        {"items": [{"section": "python"}, {"section": "editor"}]}
    ) == [{}, {}]


def test_fake_lsp_workspace_configuration_empty_settings() -> None:
    transport = FakeLspTransport()
    assert transport.request(
        "workspace/configuration", {"items": [{"section": "python"}]}
    ) == [{}]
    assert transport.request("workspace/configuration", {}) == []
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


_CONFIG_LSP_SCRIPT = r"""
import json
import os
import sys

pending = bytearray()

def read_more():
    chunk = os.read(sys.stdin.fileno(), 4096)
    if not chunk:
        return False
    pending.extend(chunk)
    return True

def read_msg():
    global pending
    while b"\r\n\r\n" not in pending:
        if not read_more():
            return None
    head, rest = bytes(pending).split(b"\r\n\r\n", 1)
    length = None
    for line in head.decode("ascii", "replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    if length is None:
        return None
    pending = bytearray(rest)
    while len(pending) < length:
        if not read_more():
            return None
    body = bytes(pending[:length])
    del pending[:length]
    return json.loads(body.decode("utf-8"))

def write_msg(msg):
    raw = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(
        ("Content-Length: %s\r\n\r\n" % len(raw)).encode("ascii") + raw
    )
    sys.stdout.buffer.flush()

def answer_configuration(req_id, items):
    write_msg({
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "workspace/configuration",
        "params": {"items": items},
    })
    reply = read_msg()
    if not isinstance(reply, dict) or reply.get("id") != req_id:
        raise SystemExit("configuration reply missing")
    return reply.get("result")

while True:
    msg = read_msg()
    if msg is None:
        break
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        answer_configuration("cfg-init", [{"section": "python"}])
        write_msg({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "capabilities": {
                    "hoverProvider": True,
                    "definitionProvider": True,
                }
            },
        })
        continue
    if method in {
        "initialized",
        "textDocument/didOpen",
        "textDocument/didChange",
        "shutdown",
    }:
        continue
    if method in {"textDocument/hover", "textDocument/definition"}:
        config = answer_configuration(
            "cfg-semantic", [{"section": "python"}, {"section": "editor"}]
        )
        if method == "textDocument/hover":
            write_msg({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"contents": "ok", "config_reply": config},
            })
        else:
            write_msg({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"config_reply": config},
            })
        continue
"""


def test_stdio_replies_to_workspace_configuration_during_hover_and_definition(
    tmp_path: Path,
) -> None:
    from omg_cli.tools_sidecar import StdioLspTransport

    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    script = tmp_path / "config_lsp.py"
    script.write_text(_CONFIG_LSP_SCRIPT, encoding="utf-8")
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=3.0
    )
    try:
        hover = lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
        assert hover["ok"] is True
        assert hover["verified"] is False
        assert hover["result"]["contents"] == "ok"
        assert hover["result"]["config_reply"] == [{}, {}]
        definition = lsp_operation(
            "definition", root=tmp_path, path="a.py", transport=transport
        )
        assert definition["ok"] is True
        assert definition["verified"] is False
        assert definition["result"]["config_reply"] == [{}, {}]
    finally:
        transport.close()


def test_lsp_refuses_truncated_document_semantic_ops(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(b"x = 1\n" + b"a" * MAX_RESULT_BYTES)
    transport = FakeLspTransport()
    for op in ("hover", "definition", "rename", "code_action"):
        with pytest.raises(ToolsError, match="E_LSP_TRUNCATED") as excinfo:
            lsp_operation(op, root=tmp_path, path="a.py", transport=transport)
        assert excinfo.value.details["truncated"] is True
        assert excinfo.value.details["max_bytes"] == MAX_RESULT_BYTES
    names = [name for name, _ in transport.calls]
    assert "textDocument/hover" not in names
    assert "textDocument/definition" not in names
    assert "textDocument/rename" not in names
    assert "textDocument/codeAction" not in names
    assert "textDocument/didOpen" not in names


def test_lsp_exact_size_bound_is_not_truncated(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"a" * MAX_RESULT_BYTES)
    transport = FakeLspTransport()
    result = lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
    assert result["ok"] is True
    assert result["truncated"] is False
    assert "textDocument/hover" in {name for name, _ in transport.calls}


def test_workspace_symbols_stamps_truncated_without_prefix_did_open(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_bytes(b"a" * (MAX_RESULT_BYTES + 1))
    transport = FakeLspTransport()
    result = lsp_operation(
        "workspace_symbols",
        root=tmp_path,
        path="a.py",
        transport=transport,
        query="x",
    )
    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["verified"] is False
    names = [name for name, _ in transport.calls]
    assert "workspace/symbol" in names
    assert "textDocument/didOpen" not in names


def test_cli_lsp_hover_truncated_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omg_cli.main import main

    (tmp_path / "a.py").write_bytes(b"a" * (MAX_RESULT_BYTES + 1))
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
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_LSP_TRUNCATED"
    assert payload["error"]["details"]["truncated"] is True
    assert payload.get("verified") is not True


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
    monkeypatch.setattr("omg_cli.tools_sidecar.Path.home", lambda: tmp_path)
    monkeypatch.delenv("CARGO_HOME", raising=False)
    with pytest.raises(ToolsError, match="E_ASTGREP_MISSING"):
        ast_search(root=tmp_path, pattern="foo", lang="python")


def test_astgrep_discovers_cargo_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cargo_bin = tmp_path / ".cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    helper = tmp_path / "ag_help.py"
    helper.write_text(
        "print('ast-grep is a CLI tool for code structural search')\n"
        "print('--pattern')\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        fake = cargo_bin / "ast-grep.cmd"
        fake.write_text(f'@"{sys.executable}" "{helper}" %*\r\n', encoding="utf-8")
    else:
        fake = cargo_bin / "ast-grep"
        fake.write_text(
            f"#!/bin/sh\nexec '{sys.executable}' '{helper}' \"$@\"\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr("omg_cli.tools_sidecar.shutil.which", lambda _name: None)
    monkeypatch.setattr("omg_cli.tools_sidecar.Path.home", lambda: tmp_path)
    monkeypatch.delenv("CARGO_HOME", raising=False)
    assert _astgrep_bin() == str(fake)


def test_ast_search_live_python_snippet(tmp_path: Path) -> None:
    binary = _astgrep_bin()
    if not binary:
        pytest.skip("ast-grep not installed")
    sample = tmp_path / "sample.py"
    sample.write_text("def hello():\n    return 1\n", encoding="utf-8")
    result = ast_search(
        root=tmp_path, pattern="def $NAME(): $$$", lang="python", path="sample.py"
    )
    assert result["ok"] is True
    assert result["verified"] is False
    assert result["binary"] == binary
    assert result["count"] >= 1
    blob = json.dumps(result["matches"])
    assert "hello" in blob


def test_lsp_code_action_includes_range(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    transport = FakeLspTransport()
    lsp_operation(
        "code_action",
        root=tmp_path,
        path="a.py",
        transport=transport,
        line=1,
        character=2,
        end_line=1,
        end_character=4,
    )
    params = next(
        body for name, body in transport.calls if name == "textDocument/codeAction"
    )
    assert params["range"] == {
        "start": {"line": 1, "character": 2},
        "end": {"line": 1, "character": 4},
    }
    assert "position" not in params


def test_lsp_did_change_after_disk_edit(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x=1\n", encoding="utf-8")
    transport = FakeLspTransport()
    lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
    target.write_text("x=22\nmore\n", encoding="utf-8")
    lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
    names = [name for name, _ in transport.calls]
    assert names.count("textDocument/didOpen") == 1
    assert "textDocument/didChange" in names
    change = next(
        body for name, body in transport.calls if name == "textDocument/didChange"
    )
    assert change["textDocument"]["version"] == 2
    assert change["contentChanges"][0]["text"].startswith("x=22")


def test_codegraph_local_index_and_query(tmp_path: Path) -> None:
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "mod.py").write_text(
        "import json\n\nclass Greeter:\n    pass\n\ndef hello():\n    return 1\n",
        encoding="utf-8",
    )
    built = codegraph_index(root=tmp_path, mode="local")
    assert built["ok"] is True
    assert built["verified"] is False
    assert built["index_present"] is True
    assert built["effective_mode"] == "local"
    assert built["branch_accurate"] is True
    assert built["not_scip"] is False
    assert built["scip_path"] == ".omg/artifacts/codegraph/local-index.scip"
    assert built["indexer"] == "import_symbol_scan"
    status = codegraph_status(root=tmp_path, mode="local")
    assert status["index_present"] is True
    hits = codegraph_query(root=tmp_path, mode="local", query="hello")
    assert hits["answered_by"] == "local"
    assert hits["branch_accurate"] is True
    assert hits["not_scip"] is False
    assert hits["verified"] is False
    assert any(row["name"] == "hello" for row in hits["hits"])
    imports = codegraph_query(root=tmp_path, mode="local", query="json")
    assert any(row["kind"] == "import" for row in imports["hits"])


def test_codegraph_shared_dirty_is_not_branch_accurate(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def shared_sym():\n    return 0\n", encoding="utf-8")
    codegraph_index(root=tmp_path, mode="shared")
    status = codegraph_status(root=tmp_path, mode="shared")
    assert status["index_present"] is True
    assert status["branch_accurate"] is False
    assert status["not_scip"] is False
    if status["worktree_dirty"]:
        assert "dirty" in status["note"]


def test_codegraph_index_confined_to_workspace(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("def inside():\n    return 1\n", encoding="utf-8")
    outside = tmp_path.parent / "escape.py"
    outside.write_text("def leaked():\n    return 1\n", encoding="utf-8")
    built = codegraph_index(root=tmp_path, mode="local")
    assert built["index_present"] is True
    blob = json.dumps(
        codegraph_query(root=tmp_path, mode="local", query="leaked")["hits"]
    )
    assert "leaked" not in blob
    assert any(
        row["name"] == "inside"
        for row in codegraph_query(root=tmp_path, mode="local", query="inside")["hits"]
    )


def test_lsp_command_remainder_keeps_server_stdio() -> None:
    from omg_cli.commands.tools import normalize_tools_argv
    from omg_cli.main import build_parser

    rewritten = normalize_tools_argv(
        [
            "tools",
            "serve",
            "--stdio",
            "--lsp-command",
            "rust-analyzer",
            "--",
            "--stdio",
        ]
    )
    assert rewritten[-1] == "--lsp-extra=--stdio"
    assert "--" not in rewritten
    parser = build_parser()
    args = parser.parse_args(rewritten)
    assert args.stdio is True
    assert args.lsp_command == ["rust-analyzer"]
    assert args.lsp_extra == ["--stdio"]

    lsp_rewritten = normalize_tools_argv(
        [
            "tools",
            "lsp",
            "hover",
            "--path",
            "a.py",
            "--lsp-command",
            "rust-analyzer",
            "--",
            "--stdio",
        ]
    )
    lsp_args = parser.parse_args(lsp_rewritten)
    assert lsp_args.lsp_command == ["rust-analyzer"]
    assert lsp_args.lsp_extra == ["--stdio"]
    assert lsp_args.path == "a.py"
    json_rewritten = normalize_tools_argv(
        [
            "--json",
            "tools",
            "serve",
            "--stdio",
            "--lsp-command",
            "rust-analyzer",
            "--",
            "--stdio",
        ]
    )
    json_args = parser.parse_args(json_rewritten)
    assert json_args.stdio is True
    assert json_args.lsp_extra == ["--stdio"]
    assert normalize_tools_argv(["ask", "tools", "--", "--stdio"]) == [
        "ask",
        "tools",
        "--",
        "--stdio",
    ]


def test_cli_doctor_strict_failure_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.main import main

    monkeypatch.setenv("OMG_TOOLS_NETWORK", "1")
    monkeypatch.setenv("OMG_TOOLS_RESEARCH_PROVIDER", "none")
    assert main(["tools", "doctor", "--strict", "--root", str(tmp_path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["command"] == "tools.doctor"
    assert payload["error"]["code"] == "E_TOOLS_DOCTOR"
    details = payload["error"]["details"]
    assert details["ok"] is False
    assert details["verified"] is False
    assert details["observed"] is False
    assert details["healthy"] is False
    assert payload.get("verified") is not True


def test_cli_doctor_non_strict_success_envelope_stays_consistent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.main import main

    monkeypatch.setenv("OMG_TOOLS_NETWORK", "1")
    monkeypatch.setenv("OMG_TOOLS_RESEARCH_PROVIDER", "none")
    assert main(["tools", "doctor", "--root", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["ok"] is True
    assert payload["result"]["verified"] is False
    assert payload["result"]["observed"] is False
    assert payload["result"]["healthy"] is False
    assert "warnings" in payload["result"]


def test_cli_codegraph_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from omg_cli.main import main

    (tmp_path / "a.py").write_text("def indexed():\n    return 1\n", encoding="utf-8")
    assert main(
        ["tools", "codegraph", "index", "--mode", "local", "--root", str(tmp_path)]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["index_present"] is True
    assert payload["result"]["verified"] is False
    assert payload["result"]["not_scip"] is False


def test_codegraph_scip_lite_occurrences(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "import json\n\ndef hello():\n    return json.dumps({})\n",
        encoding="utf-8",
    )
    built = codegraph_index(root=tmp_path, mode="local")
    assert built["ok"] is True
    assert built["verified"] is False
    assert built["not_scip"] is False
    index_path = tmp_path / ".omg" / "artifacts" / "codegraph" / "local-index.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["not_scip"] is True
    assert payload.get("verified") is not True
    assert "SCIP protobuf" in payload["note"]
    occs = payload["occurrences"]
    assert isinstance(occs, list) and occs
    assert all(
        {"path", "name", "role", "line", "symbol_id"} <= set(row)
        for row in occs
    )
    roles = {(row["name"], row["role"]) for row in occs}
    assert ("hello", "definition") in roles
    assert ("json", "reference") in roles
    hello_hits = codegraph_query(root=tmp_path, mode="local", query="hello")
    assert hello_hits["ok"] is True
    assert hello_hits["verified"] is False
    assert hello_hits["not_scip"] is False
    assert any(
        row.get("kind") == "occurrence"
        and row.get("role") == "definition"
        and row.get("name") == "hello"
        for row in hello_hits["hits"]
    )
    json_hits = codegraph_query(root=tmp_path, mode="local", query="json")
    assert any(
        row.get("kind") == "occurrence"
        and row.get("role") == "reference"
        and row.get("name") == "json"
        for row in json_hits["hits"]
    )
    symbol_id = next(row["symbol_id"] for row in occs if row["name"] == "hello")
    sid_hits = codegraph_query(root=tmp_path, mode="local", query=symbol_id)
    assert any(row.get("symbol_id") == symbol_id for row in sid_hits["hits"])
    from omg_cli.scip_codec import decode_index

    scip_path = tmp_path / ".omg" / "artifacts" / "codegraph" / "local-index.scip"
    assert scip_path.is_file()
    decoded = decode_index(scip_path.read_bytes())
    assert any(
        row["name"] == "hello" and row["role"] == "definition" for row in decoded
    )


def test_codegraph_query_reads_scip_protobuf_not_json_occurrences(
    tmp_path: Path,
) -> None:
    (tmp_path / "mod.py").write_text(
        "def hello():\n    return 1\n",
        encoding="utf-8",
    )
    built = codegraph_index(root=tmp_path, mode="local")
    assert built["not_scip"] is False
    index_path = tmp_path / ".omg" / "artifacts" / "codegraph" / "local-index.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["occurrences"] = []
    for row in payload.get("files") or []:
        if isinstance(row, dict):
            row["symbols"] = []
            row["imports"] = []
    index_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    hits = codegraph_query(root=tmp_path, mode="local", query="hello")
    assert hits["not_scip"] is False
    assert any(
        row.get("kind") == "occurrence"
        and row.get("role") == "definition"
        and row.get("name") == "hello"
        for row in hits["hits"]
    )


def test_doctor_reports_scip_dependency_and_protobuf_after_index(
    tmp_path: Path,
) -> None:
    payload = doctor_payload(root=tmp_path)
    scip = payload["dependencies"]["scip"]
    assert scip["required"] is False
    assert scip["not_scip"] is True
    assert "protobuf" in payload["note"]
    (tmp_path / "mod.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    codegraph_index(root=tmp_path, mode="local")
    local = codegraph_status(root=tmp_path, mode="local")
    assert local["not_scip"] is False
    assert "SCIP protobuf index loaded" in local["note"]
    after = doctor_payload(root=tmp_path)
    assert after["verified"] is False
    assert "protobuf" in after["note"]


def test_cli_codegraph_index_query_occurrences(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omg_cli.main import main

    (tmp_path / "mod.py").write_text(
        "import json\n\ndef hello():\n    return json.dumps({})\n",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "tools",
                "codegraph",
                "index",
                "--mode",
                "local",
                "--root",
                str(tmp_path),
            ]
        )
        == 0
    )
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["ok"] is True
    assert indexed["result"]["verified"] is False
    assert indexed["result"]["not_scip"] is False
    assert (
        main(
            [
                "tools",
                "codegraph",
                "query",
                "--mode",
                "local",
                "--query",
                "hello",
                "--root",
                str(tmp_path),
            ]
        )
        == 0
    )
    hello_payload = json.loads(capsys.readouterr().out)
    hello_blob = json.dumps(hello_payload)
    assert hello_payload["ok"] is True
    assert hello_payload["result"]["verified"] is False
    assert hello_payload.get("verified") is not True
    assert hello_payload["result"]["not_scip"] is False
    assert "definition" in hello_blob
    assert "hello" in hello_blob
    assert (
        main(
            [
                "tools",
                "codegraph",
                "query",
                "--mode",
                "local",
                "--query",
                "json",
                "--root",
                str(tmp_path),
            ]
        )
        == 0
    )
    json_payload = json.loads(capsys.readouterr().out)
    json_blob = json.dumps(json_payload)
    assert json_payload["ok"] is True
    assert json_payload["result"]["verified"] is False
    assert "reference" in json_blob
    assert "json" in json_blob


def test_local_index_stale_after_second_edit(tmp_path: Path) -> None:
    src = tmp_path / "a.py"
    src.write_text("def hello():\n    return 0\n", encoding="utf-8")
    first = codegraph_index(root=tmp_path, mode="local")
    assert first["index_stale"] is False
    src.write_text("def hello():\n    return 1\n def extra():\n    return 2\n", encoding="utf-8")
    status = codegraph_status(root=tmp_path, mode="local")
    assert status["index_stale"] is True


def test_shared_index_refuses_dirty_git_worktree(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "a.py").write_text("def shared_sym():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "a.py").write_text("def shared_sym():\n    return 1\n", encoding="utf-8")
    with pytest.raises(ToolsError, match="E_CODEGRAPH_DIRTY"):
        codegraph_index(root=tmp_path, mode="shared")


def test_fake_lsp_answers_workspace_folders(tmp_path: Path) -> None:
    transport = FakeLspTransport()
    src = tmp_path / "a.py"
    src.write_text("x = 1\n", encoding="utf-8")
    ensure_lsp_session(transport, root=tmp_path, path="a.py")
    folders = transport.request("workspace/workspaceFolders", {})
    assert isinstance(folders, list)
    assert folders
    assert folders[0]["uri"].startswith("file:")
    assert "name" in folders[0]


def test_stdio_replies_workspace_folders_request() -> None:
    written: list[dict] = []
    dummy = types.SimpleNamespace(
        _omg_workspace_folders=[{"uri": "file:///ws", "name": "ws"}],
        _write_message=written.append,
    )
    StdioLspTransport._reply_server_request(
        dummy,
        {"jsonrpc": "2.0", "id": 9, "method": "workspace/workspaceFolders"},
    )
    assert len(written) == 1
    assert written[0]["id"] == 9
    assert written[0]["result"] == [{"uri": "file:///ws", "name": "ws"}]


def test_stdio_request_deadline_is_not_session_sticky(tmp_path: Path) -> None:
    script = tmp_path / "echo_init.py"
    script.write_text(
        "import json, os, sys, time\n"
        "pending = bytearray()\n"
        "def read_more():\n"
        "    chunk = os.read(sys.stdin.fileno(), 4096)\n"
        "    if not chunk:\n"
        "        return False\n"
        "    pending.extend(chunk)\n"
        "    return True\n"
        "def read_msg():\n"
        "    global pending\n"
        "    while b'\\r\\n\\r\\n' not in pending:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    head, rest = bytes(pending).split(b'\\r\\n\\r\\n', 1)\n"
        "    length = None\n"
        "    for line in head.decode('ascii', 'replace').split('\\r\\n'):\n"
        "        if line.lower().startswith('content-length:'):\n"
        "            length = int(line.split(':', 1)[1].strip())\n"
        "    pending = bytearray(rest)\n"
        "    while len(pending) < length:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    body = bytes(pending[:length]); del pending[:length]\n"
        "    return json.loads(body.decode('utf-8'))\n"
        "def write_msg(msg):\n"
        "    raw = json.dumps(msg).encode('utf-8')\n"
        "    sys.stdout.buffer.write(\n"
        "        ('Content-Length: %s\\r\\n\\r\\n' % len(raw)).encode('ascii') + raw)\n"
        "    sys.stdout.buffer.flush()\n"
        "while True:\n"
        "    msg = read_msg()\n"
        "    if msg is None:\n"
        "        break\n"
        "    if msg.get('method') == 'initialize':\n"
        "        write_msg({'jsonrpc':'2.0','id':msg.get('id'),'result':{}})\n",
        encoding="utf-8",
    )
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=2.0
    )
    try:
        transport._omg_lsp_deadline = 0.0  # expired session deadline
        remaining = transport._request_deadline() - time.monotonic()
        assert remaining > 1.0
        assert transport.request("initialize", {}) == {}
    finally:
        transport.close()


def test_normalize_lsp_argv_drops_stdio_for_rust_analyzer() -> None:
    assert normalize_lsp_argv(["rust-analyzer", "--stdio"]) == ["rust-analyzer"]
    assert normalize_lsp_argv(["/usr/bin/rust-analyzer", "--stdio", "foo"]) == [
        "/usr/bin/rust-analyzer",
        "foo",
    ]
    assert normalize_lsp_argv(["pylsp", "--stdio"]) == ["pylsp", "--stdio"]


def test_stdio_replies_unknown_server_request_with_null() -> None:
    written: list[dict] = []
    dummy = types.SimpleNamespace(_write_message=written.append)
    StdioLspTransport._reply_server_request(
        dummy,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "window/workDoneProgress/create",
            "params": {"token": "t"},
        },
    )
    assert written[0]["id"] == 3
    assert written[0]["result"] is None


def test_stdio_peer_progress_request_then_nonnull_hover(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = tmp_path / "progress_then_hover.py"
    script.write_text(
        "import json, os, sys\n"
        "pending = bytearray()\n"
        "def read_more():\n"
        "    chunk = os.read(sys.stdin.fileno(), 4096)\n"
        "    if not chunk:\n"
        "        return False\n"
        "    pending.extend(chunk)\n"
        "    return True\n"
        "def read_msg():\n"
        "    global pending\n"
        "    while b'\\r\\n\\r\\n' not in pending:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    head, rest = bytes(pending).split(b'\\r\\n\\r\\n', 1)\n"
        "    length = None\n"
        "    for line in head.decode('ascii', 'replace').split('\\r\\n'):\n"
        "        if line.lower().startswith('content-length:'):\n"
        "            length = int(line.split(':', 1)[1].strip())\n"
        "    pending = bytearray(rest)\n"
        "    while len(pending) < length:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    body = bytes(pending[:length]); del pending[:length]\n"
        "    return json.loads(body.decode('utf-8'))\n"
        "def write_msg(msg):\n"
        "    raw = json.dumps(msg).encode('utf-8')\n"
        "    sys.stdout.buffer.write(\n"
        "        ('Content-Length: %s\\r\\n\\r\\n' % len(raw)).encode('ascii') + raw)\n"
        "    sys.stdout.buffer.flush()\n"
        "hover_id = None\n"
        "while True:\n"
        "    msg = read_msg()\n"
        "    if msg is None:\n"
        "        break\n"
        "    method = msg.get('method')\n"
        "    msg_id = msg.get('id')\n"
        "    if method == 'initialize':\n"
        "        write_msg({'jsonrpc':'2.0','id':99,'method':"
        "'window/workDoneProgress/create','params':{'token':'t'}})\n"
        "        reply = read_msg()\n"
        "        if not isinstance(reply, dict) or reply.get('id') != 99:\n"
        "            raise SystemExit('progress reply missing')\n"
        "        if reply.get('result') is not None:\n"
        "            raise SystemExit('progress result must be null')\n"
        "        write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'capabilities':{'hoverProvider':True}}})\n"
        "        continue\n"
        "    if method in {'initialized','textDocument/didOpen',"
        "'textDocument/didChange','shutdown'}:\n"
        "        continue\n"
        "    if method == 'textDocument/hover':\n"
        "        write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'contents':{'kind':'markdown','value':'**hello**'}}})\n"
        "        continue\n",
        encoding="utf-8",
    )
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=3.0
    )
    try:
        hover = lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
        assert hover["ok"] is True
        assert hover["verified"] is False
        assert hover["result"] is not None
        assert hover["result"]["contents"]["value"] == "**hello**"
    finally:
        transport.close()


def test_stdio_peer_rejects_stdio_flag(tmp_path: Path) -> None:
    script = tmp_path / "no_stdio.py"
    script.write_text(
        "import sys\n"
        "if '--stdio' in sys.argv:\n"
        "    sys.stderr.write('unexpected flag: `--stdio`\\n')\n"
        "    raise SystemExit(2)\n"
        "import time; time.sleep(30)\n",
        encoding="utf-8",
    )
    transport = StdioLspTransport(
        [sys.executable, str(script), "--stdio"], cwd=tmp_path, timeout_s=1.0
    )
    try:
        with pytest.raises(ToolsError, match="E_LSP_CRASH"):
            transport.request("initialize", {})
    finally:
        transport.close()
    stripped = normalize_lsp_argv(["rust-analyzer", "--stdio"])
    assert "--stdio" not in stripped


def test_lsp_operation_retries_null_hover_until_payload(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = tmp_path / "null_then_hover.py"
    script.write_text(
        "import json, os, sys\n"
        "pending = bytearray()\n"
        "hovers = 0\n"
        "def read_more():\n"
        "    chunk = os.read(sys.stdin.fileno(), 4096)\n"
        "    if not chunk:\n"
        "        return False\n"
        "    pending.extend(chunk)\n"
        "    return True\n"
        "def read_msg():\n"
        "    global pending\n"
        "    while b'\\r\\n\\r\\n' not in pending:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    head, rest = bytes(pending).split(b'\\r\\n\\r\\n', 1)\n"
        "    length = None\n"
        "    for line in head.decode('ascii', 'replace').split('\\r\\n'):\n"
        "        if line.lower().startswith('content-length:'):\n"
        "            length = int(line.split(':', 1)[1].strip())\n"
        "    pending = bytearray(rest)\n"
        "    while len(pending) < length:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    body = bytes(pending[:length]); del pending[:length]\n"
        "    return json.loads(body.decode('utf-8'))\n"
        "def write_msg(msg):\n"
        "    raw = json.dumps(msg).encode('utf-8')\n"
        "    sys.stdout.buffer.write(\n"
        "        ('Content-Length: %s\\r\\n\\r\\n' % len(raw)).encode('ascii') + raw)\n"
        "    sys.stdout.buffer.flush()\n"
        "while True:\n"
        "    msg = read_msg()\n"
        "    if msg is None:\n"
        "        break\n"
        "    method = msg.get('method')\n"
        "    msg_id = msg.get('id')\n"
        "    if method == 'initialize':\n"
        "        write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'capabilities':{'hoverProvider':True}}})\n"
        "        continue\n"
        "    if method in {'initialized','textDocument/didOpen',"
        "'textDocument/didChange','shutdown'}:\n"
        "        continue\n"
        "    if method == 'textDocument/hover':\n"
        "        hovers += 1\n"
        "        if hovers == 1:\n"
        "            write_msg({'jsonrpc':'2.0','id':msg_id,'result':None})\n"
        "        else:\n"
        "            write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'contents':'indexed'}})\n"
        "        continue\n",
        encoding="utf-8",
    )
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=3.0
    )
    try:
        hover = lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
        assert hover["ok"] is True
        assert hover["result"] == {"contents": "indexed"}
        assert hover["verified"] is False
    finally:
        transport.close()


def test_lsp_operation_retries_content_modified_until_payload(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = tmp_path / "content_modified_then_hover.py"
    script.write_text(
        "import json, os, sys\n"
        "pending = bytearray()\n"
        "hovers = 0\n"
        "definitions = 0\n"
        "def read_more():\n"
        "    chunk = os.read(sys.stdin.fileno(), 4096)\n"
        "    if not chunk:\n"
        "        return False\n"
        "    pending.extend(chunk)\n"
        "    return True\n"
        "def read_msg():\n"
        "    global pending\n"
        "    while b'\\r\\n\\r\\n' not in pending:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    head, rest = bytes(pending).split(b'\\r\\n\\r\\n', 1)\n"
        "    length = None\n"
        "    for line in head.decode('ascii', 'replace').split('\\r\\n'):\n"
        "        if line.lower().startswith('content-length:'):\n"
        "            length = int(line.split(':', 1)[1].strip())\n"
        "    pending = bytearray(rest)\n"
        "    while len(pending) < length:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    body = bytes(pending[:length]); del pending[:length]\n"
        "    return json.loads(body.decode('utf-8'))\n"
        "def write_msg(msg):\n"
        "    raw = json.dumps(msg).encode('utf-8')\n"
        "    sys.stdout.buffer.write(\n"
        "        ('Content-Length: %s\\r\\n\\r\\n' % len(raw)).encode('ascii') + raw)\n"
        "    sys.stdout.buffer.flush()\n"
        "while True:\n"
        "    msg = read_msg()\n"
        "    if msg is None:\n"
        "        break\n"
        "    method = msg.get('method')\n"
        "    msg_id = msg.get('id')\n"
        "    if method == 'initialize':\n"
        "        write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'capabilities':{'hoverProvider':True,'definitionProvider':True}}})\n"
        "        continue\n"
        "    if method in {'initialized','textDocument/didOpen',"
        "'textDocument/didChange','shutdown'}:\n"
        "        continue\n"
        "    if method == 'textDocument/hover':\n"
        "        hovers += 1\n"
        "        if hovers == 1:\n"
        "            write_msg({'jsonrpc':'2.0','id':msg_id,'error':"
        "{'code':-32801,'message':'content modified'}})\n"
        "        else:\n"
        "            write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'contents':'indexed'}})\n"
        "        continue\n"
        "    if method == 'textDocument/definition':\n"
        "        definitions += 1\n"
        "        if definitions == 1:\n"
        "            write_msg({'jsonrpc':'2.0','id':msg_id,'error':"
        "{'code':-32801,'message':'content modified'}})\n"
        "        else:\n"
        "            write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'uri':'file:///indexed.rs'}})\n"
        "        continue\n",
        encoding="utf-8",
    )
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=3.0
    )
    try:
        hover = lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
        assert hover["ok"] is True
        assert hover["result"] == {"contents": "indexed"}
        assert hover["verified"] is False
        definition = lsp_operation(
            "definition", root=tmp_path, path="a.py", transport=transport
        )
        assert definition["ok"] is True
        assert definition["result"] == {"uri": "file:///indexed.rs"}
        assert definition["verified"] is False
    finally:
        transport.close()


def test_lsp_operation_does_not_retry_unrelated_rpc_error(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = tmp_path / "method_not_found.py"
    script.write_text(
        "import json, os, sys\n"
        "pending = bytearray()\n"
        "hovers = 0\n"
        "def read_more():\n"
        "    chunk = os.read(sys.stdin.fileno(), 4096)\n"
        "    if not chunk:\n"
        "        return False\n"
        "    pending.extend(chunk)\n"
        "    return True\n"
        "def read_msg():\n"
        "    global pending\n"
        "    while b'\\r\\n\\r\\n' not in pending:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    head, rest = bytes(pending).split(b'\\r\\n\\r\\n', 1)\n"
        "    length = None\n"
        "    for line in head.decode('ascii', 'replace').split('\\r\\n'):\n"
        "        if line.lower().startswith('content-length:'):\n"
        "            length = int(line.split(':', 1)[1].strip())\n"
        "    pending = bytearray(rest)\n"
        "    while len(pending) < length:\n"
        "        if not read_more():\n"
        "            return None\n"
        "    body = bytes(pending[:length]); del pending[:length]\n"
        "    return json.loads(body.decode('utf-8'))\n"
        "def write_msg(msg):\n"
        "    raw = json.dumps(msg).encode('utf-8')\n"
        "    sys.stdout.buffer.write(\n"
        "        ('Content-Length: %s\\r\\n\\r\\n' % len(raw)).encode('ascii') + raw)\n"
        "    sys.stdout.buffer.flush()\n"
        "while True:\n"
        "    msg = read_msg()\n"
        "    if msg is None:\n"
        "        break\n"
        "    method = msg.get('method')\n"
        "    msg_id = msg.get('id')\n"
        "    if method == 'initialize':\n"
        "        write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'capabilities':{'hoverProvider':True}}})\n"
        "        continue\n"
        "    if method in {'initialized','textDocument/didOpen',"
        "'textDocument/didChange','shutdown'}:\n"
        "        continue\n"
        "    if method == 'textDocument/hover':\n"
        "        hovers += 1\n"
        "        if hovers == 1:\n"
        "            write_msg({'jsonrpc':'2.0','id':msg_id,'error':"
        "{'code':-32601,'message':'Method not found'}})\n"
        "        else:\n"
        "            write_msg({'jsonrpc':'2.0','id':msg_id,'result':"
        "{'contents':'indexed'}})\n"
        "        continue\n",
        encoding="utf-8",
    )
    transport = StdioLspTransport(
        [sys.executable, str(script)], cwd=tmp_path, timeout_s=3.0
    )
    try:
        with pytest.raises(ToolsError, match="E_LSP_RPC") as excinfo:
            lsp_operation("hover", root=tmp_path, path="a.py", transport=transport)
        assert excinfo.value.details["code"] == -32601
        assert "Method not found" in str(excinfo.value.details["message"])
    finally:
        transport.close()
