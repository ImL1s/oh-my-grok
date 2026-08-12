"""Hermetic ACP stdio wire tests (#105 PR4/PR5)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from omg_cli.host_acp import (
    AcpError,
    AcpResumeReceipt,
    AcpStdioSession,
    acp_stdio_argv,
    build_receipt_from_dict,
    classify_session_update,
    hash_cwd,
    hash_session_id,
    spawn_acp_stdio,
    validate_receipt,
    validate_resume_result,
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


def test_acp_resume_false_does_not_write_receipt(tmp_path: Path) -> None:
    proc, argv = _spawn("resume_false", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_RESUME"
        assert sess._receipt is None
    finally:
        sess.close()


def test_acp_resume_session_id_mismatch_does_not_match(tmp_path: Path) -> None:
    proc, argv = _spawn("session_id_mismatch", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_IDENTITY"
        assert sess._receipt is None
    finally:
        sess.close()


def test_acp_resume_missing_resumed_does_not_write_receipt(tmp_path: Path) -> None:
    proc, argv = _spawn("resume_missing_flag", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_RESUME"
        assert sess._receipt is None
    finally:
        sess.close()


def test_acp_resume_session_id_alias_matches(tmp_path: Path) -> None:
    proc, argv = _spawn("session_id_alias", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        receipt = sess.handshake(timeout_s=5.0)
        assert receipt.resume_matched is True
        assert receipt.session_id_hash == sess.session_id_hash
    finally:
        sess.close()


def test_acp_stderr_flood_does_not_timeout_handshake(tmp_path: Path) -> None:
    proc, argv = _spawn("stderr_flood", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        receipt = sess.handshake(timeout_s=8.0)
        assert receipt.resume_matched is True
        assert receipt.initialized is True
        body = receipt.to_dict()
        assert "stderr" not in body
    finally:
        sess.close()


def test_acp_cumulative_chrome_uses_max_total_bytes(tmp_path: Path) -> None:
    # Path A: many small chrome lines exceed max_line_bytes cumulatively but
    # stay under max_total_bytes — must succeed (old bug failed at line cap).
    proc, argv = _spawn(
        "many_small_chrome", tmp_path, env={"OMG_ACP_FAKE_CHROME_COUNT": "8"}
    )
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    sess.max_line_bytes = 256
    sess.max_total_bytes = 50_000
    try:
        receipt = sess.handshake(timeout_s=5.0)
        assert receipt.no_replay_observed is True
        assert sess._byte_budget[0] > sess.max_line_bytes
    finally:
        sess.close()

    # Path B: same per-line cap; cumulative ceiling is enforced.
    proc2, argv2 = _spawn(
        "many_small_chrome", tmp_path, env={"OMG_ACP_FAKE_CHROME_COUNT": "80"}
    )
    sess2 = _session(proc2, argv2, tmp_path, quiet=0.05)
    sess2.max_line_bytes = 256
    sess2.max_total_bytes = 800
    try:
        with pytest.raises(AcpError) as ei:
            sess2.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert sess2._receipt is None
        assert sess2._byte_budget[0] > sess2.max_total_bytes
    finally:
        sess2.close()


def test_validate_receipt_rejects_missing_resume_matched() -> None:
    rec = AcpResumeReceipt(
        job_id="20260101T000000Z-deadbeef",
        attempt=1,
        parent_run_id="run-1",
        session_id_hash=hash_session_id("sid"),
        cwd_hash=hash_cwd("."),
        resume_matched=True,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    body = rec.to_dict()
    del body["resume_matched"]
    core = {k: v for k, v in body.items() if k != "receipt_sha256"}
    body["receipt_sha256"] = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with pytest.raises(AcpError) as ei:
        validate_receipt(
            body,
            session_id_hash=rec.session_id_hash,
            cwd_hash=rec.cwd_hash,
            parent_run_id=rec.parent_run_id,
        )
    assert ei.value.code == "E_ACP_RECEIPT"


def test_build_receipt_from_dict_missing_resume_matched_is_not_true() -> None:
    rec = build_receipt_from_dict(
        {
            "job_id": "j",
            "attempt": 1,
            "parent_run_id": "run-1",
            "session_id_hash": "abc",
            "cwd_hash": "def",
        }
    )
    assert rec.resume_matched is False
    rec_truthy = build_receipt_from_dict(
        {
            "job_id": "j",
            "attempt": 1,
            "parent_run_id": "run-1",
            "session_id_hash": "abc",
            "cwd_hash": "def",
            "resume_matched": 1,
        }
    )
    assert rec_truthy.resume_matched is False


def test_validate_resume_result_typed_errors() -> None:
    sid = str(uuid.uuid4())
    validate_resume_result({"sessionId": sid, "resumed": True}, sid)
    validate_resume_result({"session_id": sid, "resumed": True}, sid)
    with pytest.raises(AcpError) as missing:
        validate_resume_result({"sessionId": sid}, sid)
    assert missing.value.code == "E_ACP_RESUME"
    with pytest.raises(AcpError) as false_flag:
        validate_resume_result({"sessionId": sid, "resumed": False}, sid)
    assert false_flag.value.code == "E_ACP_RESUME"
    with pytest.raises(AcpError) as truthy:
        validate_resume_result({"sessionId": sid, "resumed": "true"}, sid)
    assert truthy.value.code == "E_ACP_RESUME"
    with pytest.raises(AcpError) as mismatch:
        validate_resume_result(
            {"sessionId": "00000000-0000-0000-0000-000000000000", "resumed": True},
            sid,
        )
    assert mismatch.value.code == "E_ACP_IDENTITY"
    with pytest.raises(AcpError) as not_obj:
        validate_resume_result(None, sid)
    assert not_obj.value.code == "E_ACP_RESUME"
    with pytest.raises(AcpError) as null_flag:
        validate_resume_result({"sessionId": sid, "resumed": None}, sid)
    assert null_flag.value.code == "E_ACP_RESUME"
    with pytest.raises(AcpError) as num_flag:
        validate_resume_result({"sessionId": sid, "resumed": 1}, sid)
    assert num_flag.value.code == "E_ACP_RESUME"
    with pytest.raises(AcpError) as null_sid:
        validate_resume_result({"sessionId": None, "resumed": True}, sid)
    assert null_sid.value.code == "E_ACP_IDENTITY"
    with pytest.raises(AcpError) as num_sid:
        validate_resume_result({"sessionId": 1, "resumed": True}, sid)
    assert num_sid.value.code == "E_ACP_IDENTITY"
    with pytest.raises(AcpError) as list_result:
        validate_resume_result([], sid)
    assert list_result.value.code == "E_ACP_RESUME"
