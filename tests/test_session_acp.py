"""#74 leftover: ``omg session acp-resume`` via host_acp + fake ACP peer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from omg_cli.host_acp import hash_cwd, hash_session_id, validate_receipt
from omg_cli.main import build_parser, main
from omg_cli.session_acp import (
    SESSION_CLI_PARENT_RUN_ID,
    session_acp_resume,
)
from omg_cli.state_root import ENV_STATE_DIR

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "fake_grok_acp_agent.py"
OMG_BIN = ROOT / "bin" / "omg"
BANNED_CONTENT_KEYS = (
    "transcript",
    "messages",
    "authorization",
    "token",
    "raw",
    "stdout",
    "stderr",
)


def _clear_state_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_STATE_DIR, raising=False)
    monkeypatch.delenv("OMG_WORKSPACE_MARKER", raising=False)
    monkeypatch.delenv("OMG_DISABLE_WORKSPACE_MARKER", raising=False)


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _clear_state_env(monkeypatch)
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".omg").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(root))
    return root


def _acp_env(monkeypatch: pytest.MonkeyPatch, *, scenario: str = "success") -> None:
    monkeypatch.setenv("OMG_ACP_BIN", str(FIXTURE))
    monkeypatch.setenv("OMG_ACP_FAKE_SCENARIO", scenario)
    monkeypatch.setenv("OMG_ACP_QUIET_WINDOW_S", "0.05")


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def _domain(payload: dict) -> dict:
    if "data" in payload and isinstance(payload["data"], dict):
        return payload["data"]
    return payload


def _collect_keys(obj: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(str(key).lower())
            keys |= _collect_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _collect_keys(item)
    return keys


def _assert_content_free(obj: object) -> None:
    keys = _collect_keys(obj)
    for banned in BANNED_CONTENT_KEYS:
        assert banned not in keys
    blob = json.dumps(obj, ensure_ascii=False).lower()
    assert "transcript" not in blob
    assert "messages" not in blob
    assert '"verified"' not in blob


def _cli_env(*, cwd: Path, scenario: str = "success") -> dict[str, str]:
    env = dict(os.environ)
    env["OMG_ACP_BIN"] = str(FIXTURE)
    env["OMG_ACP_FAKE_SCENARIO"] = scenario
    env["OMG_ACP_QUIET_WINDOW_S"] = "0.05"
    env["OMG_PROJECT_ROOT"] = str(cwd)
    env["PYTHONPATH"] = str(ROOT)
    return env


def test_session_nested_actions_include_acp_resume() -> None:
    parser = build_parser()
    sid = str(uuid.uuid4())
    ns = parser.parse_args(
        ["session", "acp-resume", "--session-id", sid, "--cwd", "."]
    )
    assert ns.session_action == "acp-resume"
    assert ns.acp_session_id == sid
    assert ns.acp_cwd == "."
    assert ns.func.__name__ == "cmd_session"
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["session", "acp-resume"])
    assert int(ei.value.code) == 2


def test_acp_resume_documented_in_skills() -> None:
    for name in ("skills.md", "skills.zh.md", "skills.zh-TW.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert "acp-resume" in text


def test_session_acp_resume_happy_receipt_no_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    _acp_env(monkeypatch, scenario="success")
    sid = str(uuid.uuid4())
    rc = main(
        [
            "--json",
            "session",
            "acp-resume",
            "--session-id",
            sid,
            "--cwd",
            str(root),
        ]
    )
    assert rc == 0
    envelope = _out(capsys)
    assert envelope["ok"] is True
    assert envelope["command"] == "session.acp-resume"
    payload = _domain(envelope)
    assert payload["resume_matched"] is True
    assert payload["initialized"] is True
    assert payload["no_replay_observed"] is True
    assert payload["restore_code_requested"] is False
    assert payload["session_close"] is False
    assert payload["ag_history_imported"] is False
    assert payload["live_grok"] is False
    assert payload["durable_sidecar"] is False
    assert payload["peer"] == "fake_fixture"
    assert payload["kind"] == "grok_acp_resume_receipt/v1"
    receipt = payload["receipt"]
    assert receipt["resume_matched"] is True
    assert receipt["restore_code_requested"] is False
    for banned in BANNED_CONTENT_KEYS:
        assert banned not in receipt
        assert banned not in payload
    _assert_content_free(envelope)
    validated = validate_receipt(
        receipt,
        session_id_hash=hash_session_id(sid),
        cwd_hash=hash_cwd(root),
        parent_run_id=SESSION_CLI_PARENT_RUN_ID,
    )
    assert validated.resume_matched is True
    assert validated.receipt_sha256 == receipt["receipt_sha256"]


def test_session_acp_resume_vendor_chrome_cli_content_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    _acp_env(monkeypatch, scenario="vendor_chrome")
    sid = str(uuid.uuid4())
    rc = main(
        [
            "--json",
            "session",
            "acp-resume",
            "--session-id",
            sid,
            "--cwd",
            str(root),
        ]
    )
    assert rc == 0
    envelope = _out(capsys)
    assert envelope["ok"] is True
    blob = json.dumps(envelope)
    assert "ACP_VENDOR_SECRET_TOKEN_DO_NOT_ECHO" not in blob
    assert envelope["command"] == "session.acp-resume"
    payload = _domain(envelope)
    assert payload["resume_matched"] is True
    _assert_content_free(envelope)


def test_session_acp_resume_malformed_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    _acp_env(monkeypatch, scenario="malformed")
    sid = str(uuid.uuid4())
    rc = main(
        [
            "--json",
            "session",
            "acp-resume",
            "--session-id",
            sid,
            "--cwd",
            str(root),
        ]
    )
    assert rc == 1
    envelope = _out(capsys)
    assert envelope["ok"] is False
    assert envelope["command"] == "session.acp-resume"
    err = envelope.get("error") or {}
    assert err.get("code") == "E_ACP_MALFORMED" or envelope.get("error_code") == "E_ACP_MALFORMED"
    blob = json.dumps(envelope).lower()
    assert "transcript" not in blob
    assert "messages" not in blob
    assert envelope.get("data", {}).get("resume_matched") is not True


def test_session_acp_resume_restore_code_refused_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    _acp_env(monkeypatch, scenario="success")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ACP spawn must not run when restore-code is refused")

    monkeypatch.setattr("omg_cli.session_acp.spawn_acp_stdio", _boom)
    sid = str(uuid.uuid4())
    rc = main(
        [
            "--json",
            "session",
            "acp-resume",
            "--session-id",
            sid,
            "--cwd",
            str(root),
            "--restore-code",
        ]
    )
    assert rc == 1
    envelope = _out(capsys)
    assert envelope["ok"] is False
    err = envelope.get("error") or {}
    assert err.get("code") == "E_ACP_RESTORE_CODE" or envelope.get("error_code") == "E_ACP_RESTORE_CODE"
    message = (err.get("message") or envelope.get("message") or "").lower()
    assert "restore" in message


def test_session_acp_resume_invalid_uuid_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    _acp_env(monkeypatch, scenario="success")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ACP spawn must not run for an invalid UUID")

    monkeypatch.setattr("omg_cli.session_acp.spawn_acp_stdio", _boom)
    rc = main(
        [
            "--json",
            "session",
            "acp-resume",
            "--session-id",
            "not-a-uuid",
            "--cwd",
            str(root),
        ]
    )
    assert rc == 1
    envelope = _out(capsys)
    err = envelope.get("error") or {}
    assert err.get("code") == "E_ACP_SESSION_ID" or envelope.get("error_code") == "E_ACP_SESSION_ID"


def test_sanitize_acp_cli_error_never_echoes_peer_text() -> None:
    from omg_cli.session_acp import sanitize_acp_cli_error

    leaked = "sk-live-secret SECRET_REPLAY /Users/iml1s/.ssh/id_rsa"
    out = sanitize_acp_cli_error(leaked, code="E_ACP_RPC")
    assert "sk-live-secret" not in out
    assert "SECRET_REPLAY" not in out
    assert ".ssh" not in out
    assert out == "ACP peer rejected initialize or session/resume"
    restore = sanitize_acp_cli_error(leaked, code="E_ACP_RESTORE_CODE")
    assert "sk-live-secret" not in restore
    assert "restore" in restore.lower()


def test_session_acp_resume_function_happy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _acp_env(monkeypatch, scenario="success")
    sid = str(uuid.uuid4())
    payload = session_acp_resume(session_id=sid, cwd=tmp_path)
    assert payload["resume_matched"] is True
    assert payload["session_close"] is False
    assert payload["live_grok"] is False
    _assert_content_free(payload)


def test_session_acp_resume_shipped_cli_happy(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".omg").mkdir()
    sid = str(uuid.uuid4())
    proc = subprocess.run(
        [
            sys.executable,
            str(OMG_BIN),
            "--json",
            "session",
            "acp-resume",
            "--session-id",
            sid,
            "--cwd",
            str(root),
        ],
        cwd=str(root),
        env=_cli_env(cwd=root, scenario="success"),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    envelope = json.loads(proc.stdout)
    payload = _domain(envelope)
    assert payload["resume_matched"] is True
    assert payload["session_close"] is False
    assert payload["ag_history_imported"] is False
    assert payload["live_grok"] is False
    _assert_content_free(envelope)
    validate_receipt(
        payload["receipt"],
        session_id_hash=hash_session_id(sid),
        cwd_hash=hash_cwd(root),
        parent_run_id=SESSION_CLI_PARENT_RUN_ID,
    )


def test_session_acp_resume_shipped_cli_malformed(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".omg").mkdir()
    sid = str(uuid.uuid4())
    proc = subprocess.run(
        [
            sys.executable,
            str(OMG_BIN),
            "--json",
            "session",
            "acp-resume",
            "--session-id",
            sid,
            "--cwd",
            str(root),
        ],
        cwd=str(root),
        env=_cli_env(cwd=root, scenario="malformed"),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert proc.returncode == 1, proc.stderr + proc.stdout
    envelope = json.loads(proc.stdout)
    assert envelope["ok"] is False
    err = envelope.get("error") or {}
    assert err.get("code") == "E_ACP_MALFORMED" or envelope.get("error_code") == "E_ACP_MALFORMED"
    blob = json.dumps(envelope).lower()
    assert "transcript" not in blob
    assert "messages" not in blob
