"""#30 golden envelope + exit-code matrix for key JSON surfaces."""

from __future__ import annotations

import json
from pathlib import Path

from omg_cli.cli_envelope import SCHEMA_VERSION
from omg_cli.main import main


def _load_out(capsys) -> dict:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def test_state_empty_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    # No active run — still exit 0 with envelope under --json
    code = main(["--json", "state"])
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "state"
    assert payload.get("active") is None


def test_state_missing_run_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "state", "--run", "does-not-exist-zzzz"])
    assert code == 1
    payload = _load_out(capsys)
    assert payload["ok"] is False
    assert payload["schema_version"] == SCHEMA_VERSION
    err = payload.get("error") or {}
    assert err.get("code") == "E_RUN_NOT_FOUND" or payload.get("error_code") == "E_RUN_NOT_FOUND"


def test_capabilities_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "capabilities"])
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "capabilities"
    assert "data" in payload


def test_lsp_status_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["--json", "lsp", "status"])
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "lsp.status"
    assert "data" in payload


def test_lsp_validate_missing_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["--json", "lsp", "validate"])
    assert code == 1
    payload = _load_out(capsys)
    assert payload["ok"] is False
    assert payload["command"] == "lsp.validate"
    assert payload.get("error") == "E_LSP_MISSING" or (
        isinstance(payload.get("error"), dict)
        and payload["error"].get("code") == "E_LSP_MISSING"
    )


def test_hud_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "hud"])
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "hud"
    assert "data" in payload


def test_wiki_list_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "wiki", "list"])
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "wiki.list"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload.get("data") == []


def test_memory_show_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "memory", "show"])
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "memory"
    assert "data" in payload


def test_native_status_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "native-status"])
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "native-status"
    assert "data" in payload


def test_workflow_list_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "workflow", "list"])
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "workflow"
    assert payload.get("data") == []


def test_team_plan_only_success_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(
        [
            "--json",
            "team",
            "launch",
            "--workers",
            "2",
            "--goal",
            "noop",
            "--plan-only",
        ]
    )
    assert code == 0
    payload = _load_out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "team"
    assert payload["schema_version"] == SCHEMA_VERSION
    data = payload.get("data") or {}
    assert data.get("mode") == "plan_only"
    assert data.get("mutates") is False


def test_cancel_no_run_failure_envelope(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    code = main(["--json", "cancel"])
    assert code == 1
    payload = _load_out(capsys)
    assert payload["ok"] is False
    assert payload["command"] == "cancel"
    assert payload["schema_version"] == SCHEMA_VERSION
    err = payload.get("error") or {}
    assert err.get("code") == "E_RUN_NOT_FOUND" or payload.get("error_code") == "E_RUN_NOT_FOUND"


def test_usage_error_exit_2() -> None:
    # Unknown option on a real subcommand → argparse usage (exit 2).
    # Note: an unrecognized *top-level* token is host-launch, not usage error.
    try:
        code = main(["state", "--not-a-real-flag-xyz"])
    except SystemExit as ei:
        code = ei.code
    assert int(code or 0) == 2


def test_safe_yolo_mutual_exclusion_exit_2() -> None:
    try:
        code = main(["--safe", "--yolo", "state"])
    except SystemExit as ei:
        code = ei.code
    assert int(code or 0) == 2


def test_exit_for_ok_mapping() -> None:
    from omg_cli.cli_envelope import exit_for_ok

    assert exit_for_ok(True) == 0
    assert exit_for_ok(False) == 1
    assert exit_for_ok(False, usage=True) == 2
