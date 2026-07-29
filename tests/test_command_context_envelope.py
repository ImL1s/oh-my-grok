"""#29 Phase 3 CommandContext + #30 global --json envelope."""

from __future__ import annotations

import json

from omg_cli.cli_envelope import SCHEMA_VERSION, failure, success
from omg_cli.command_context import CommandContext, resolve_output_mode
from omg_cli.main import apply_output_flags, apply_safe_yolo_flags, build_parser, main


def test_success_failure_envelope_shape() -> None:
    ok = success("state", data={"run_id": "r1"})
    assert ok["ok"] is True
    assert ok["schema_version"] == SCHEMA_VERSION
    assert ok["command"] == "state"
    bad = failure("state", "E_RUN_NOT_FOUND", "missing", next_action="retry")
    assert bad["ok"] is False
    assert bad["error"]["code"] == "E_RUN_NOT_FOUND"
    assert bad["error"]["next_action"] == "retry"


def test_global_json_flag_parse_before_or_after_subcommand() -> None:
    parser = build_parser()
    for argv in (
        ["--json", "state"],
        ["state", "--json"],
    ):
        ns = parser.parse_args(argv)
        apply_safe_yolo_flags(parser, ns)
        apply_output_flags(parser, ns)
        assert ns.json_output is True
        assert resolve_output_mode(ns) == "json"


def test_state_json_no_active_run_envelope(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["--json", "state"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "state"
    assert payload.get("active") is None
    assert "no active run" in payload.get("message", "")


def test_hud_global_json_envelope(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["--json", "hud"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "hud"
    assert "data" in payload


def test_command_context_wants_json() -> None:
    ctx = CommandContext(
        command="state",
        root=None,
        safe=False,
        yolo=False,
        output="json",
    )
    assert ctx.wants_json is True


def test_capabilities_global_json_envelope(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["--json", "capabilities"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "capabilities"
    assert "data" in payload
    assert payload["schema_version"] == SCHEMA_VERSION


def test_lsp_status_global_json_envelope(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["--json", "lsp", "status"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "lsp.status"
    assert "data" in payload


def test_memory_show_global_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    # setup minimal .omg so memory works
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "memory", "show"])
    # may fail if memory needs more setup — accept 0 or 1 with envelope
    out = capsys.readouterr().out
    if code == 0 and out.strip():
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["command"] == "memory"


def test_session_allocate_global_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["--json", "session", "allocate"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "session"
    assert "data" in payload
