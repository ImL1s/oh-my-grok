"""Hermetic ACP stdio wire tests (#105 PR4)."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

from omg_cli.host_acp import (
    AcpError,
    AcpStdioSession,
    acp_stdio_argv,
    classify_session_update,
    hash_cwd,
    hash_session_id,
    spawn_acp_stdio,
    validate_receipt,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_grok_acp_agent.py"


def _spawn(scenario: str, cwd: Path, env: dict | None = None):
    e = dict(os.environ)
    e["OMG_ACP_FAKE_SCENARIO"] = scenario
    if env:
        e.update(env)
    proc, argv = spawn_acp_stdio(
        binary=sys.executable,
        cwd=cwd,
        env=e,
        # -u: unbuffered peer stdout so quiet-window notifications are not held
        argv_override=[sys.executable, "-u", str(FIXTURE)],
    )
    return proc, argv


def _session(proc, argv, cwd: Path, *, quiet: float = 0.12) -> AcpStdioSession:
    sid = str(uuid.uuid4())
    return AcpStdioSession(
        proc=proc,
        argv=argv,
        session_id=sid,
        cwd=str(cwd.resolve()),
        job_id="20260101T000000Z-deadbeef",
        attempt=1,
        parent_run_id="run-1",
        session_id_hash=hash_session_id(sid),
        cwd_hash=hash_cwd(cwd),
        quiet_window_s=quiet,
    )


def test_acp_initialize_precedes_session_resume(tmp_path: Path) -> None:
    """Fake peer rejects non-initialize first; success path initializes first."""
    proc, argv = _spawn("success", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        receipt = sess.handshake(timeout_s=5.0)
        assert receipt.initialized is True
        assert receipt.resume_matched is True
        assert argv[-2:] != ("session", "resume")  # argv is agent stdio / python script
    finally:
        sess.close()


def test_acp_resume_uses_exact_uuid_and_canonical_cwd(tmp_path: Path) -> None:
    proc, argv = _spawn("success", tmp_path)
    sid = str(uuid.uuid4())
    sess = AcpStdioSession(
        proc=proc,
        argv=argv,
        session_id=sid,
        cwd=str(tmp_path.resolve()),
        job_id="20260101T000000Z-deadbeef",
        attempt=1,
        parent_run_id="run-1",
        session_id_hash=hash_session_id(sid),
        cwd_hash=hash_cwd(tmp_path),
        quiet_window_s=0.05,
    )
    try:
        receipt = sess.handshake(timeout_s=5.0)
        assert receipt.session_id_hash == hash_session_id(sid)
        assert receipt.cwd_hash == hash_cwd(tmp_path)
        assert receipt.restore_code_requested is False
        assert receipt.no_replay_observed is True
    finally:
        sess.close()


def test_acp_resume_rejects_conversation_replay_before_response(tmp_path: Path) -> None:
    proc, argv = _spawn("replay", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_REPLAY"
    finally:
        sess.close()


def test_acp_resume_rejects_late_replay_during_quiet_window(tmp_path: Path) -> None:
    proc, argv = _spawn("late_replay", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.2)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_REPLAY"
    finally:
        sess.close()


def test_acp_resume_allows_non_conversation_chrome(tmp_path: Path) -> None:
    proc, argv = _spawn("chrome", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.08)
    try:
        receipt = sess.handshake(timeout_s=5.0)
        assert receipt.no_replay_observed is True
    finally:
        sess.close()


def test_acp_wrong_id_rpc_error_malformed_json_fail_closed(tmp_path: Path) -> None:
    for scenario, code in (
        ("wrong_id", "E_ACP_PROTOCOL"),
        ("rpc_error", "E_ACP_RPC"),
        ("malformed", "E_ACP_MALFORMED"),
    ):
        proc, argv = _spawn(scenario, tmp_path)
        sess = _session(proc, argv, tmp_path, quiet=0.05)
        try:
            with pytest.raises(AcpError) as ei:
                sess.handshake(timeout_s=5.0)
            assert ei.value.code == code, (scenario, ei.value)
        finally:
            sess.close()


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_acp_timeout_and_overflow_kill_exact_inner_process_group(tmp_path: Path) -> None:
    # Timeout
    proc, argv = _spawn("hang", tmp_path)
    sess = _session(proc, argv, tmp_path)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=0.3)
        assert ei.value.code in {"E_ACP_TIMEOUT", "E_ACP_EOF"}
    finally:
        sess.close()
        assert proc.poll() is not None

    # Overflow
    proc2, argv2 = _spawn("overflow", tmp_path)
    sess2 = _session(proc2, argv2, tmp_path)
    try:
        with pytest.raises(AcpError) as ei2:
            sess2.handshake(timeout_s=5.0)
        assert ei2.value.code == "E_ACP_OVERFLOW"
    finally:
        sess2.close()
        assert proc2.poll() is not None


def test_acp_ready_receipt_is_atomic_bounded_and_content_free(tmp_path: Path) -> None:
    proc, argv = _spawn("success", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        receipt = sess.handshake(timeout_s=5.0)
        body = receipt.to_dict()
        assert body["kind"] == "grok_acp_resume_receipt/v1"
        assert "transcript" not in body
        assert "messages" not in body
        assert body["no_replay_observed"] is True
        assert body["restore_code_requested"] is False
        assert len(body["receipt_sha256"]) == 64
        validated = validate_receipt(
            body,
            session_id_hash=receipt.session_id_hash,
            cwd_hash=receipt.cwd_hash,
            parent_run_id=receipt.parent_run_id,
        )
        assert validated.receipt_sha256 == body["receipt_sha256"]
    finally:
        sess.close()


def test_acp_stdio_argv_no_always_approve() -> None:
    argv = acp_stdio_argv("/usr/bin/grok")
    assert argv == ["/usr/bin/grok", "agent", "stdio"]
    assert "--always-approve" not in argv


def test_classify_session_update_forbidden_vs_chrome() -> None:
    assert (
        classify_session_update(
            {"update": {"sessionUpdate": "agent_message_chunk"}}
        )
        == "forbidden"
    )
    assert (
        classify_session_update(
            {"update": {"sessionUpdate": "current_mode_update"}}
        )
        == "chrome"
    )
