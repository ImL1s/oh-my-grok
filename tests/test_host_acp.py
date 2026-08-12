"""Hermetic ACP stdio wire tests (#105 PR4/PR5)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from omg_cli.host_acp import (
    AcpError,
    AcpResumeReceipt,
    AcpStdioSession,
    _incomplete_line_len,
    _max_buffered_frame_len,
    _raise_if_buffered_limits,
    _read_line,
    acp_stdio_argv,
    allowlisted_acp_env,
    bind_constructor_identity,
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


def _session(
    proc, argv, cwd: Path, *, quiet: float = 0.12, session_id: str | None = None
) -> AcpStdioSession:
    sid = session_id if session_id is not None else str(uuid.uuid4())
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


def _peer_response_frame(*, rpc_id: int, result: dict) -> bytes:
    # Match fixture _write: default json.dumps (spaces), not host compact separators.
    return (json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}) + "\n").encode(
        "utf-8"
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


def test_handshake_rejects_forged_session_id_hash_writes_no_receipt(
    tmp_path: Path,
) -> None:
    proc, argv = _spawn("success", tmp_path)
    sid = str(uuid.uuid4())
    forged = "a" * 64
    assert forged != hash_session_id(sid)
    sess = AcpStdioSession(
        proc=proc,
        argv=argv,
        session_id=sid,
        cwd=str(tmp_path.resolve()),
        job_id="20260101T000000Z-deadbeef",
        attempt=1,
        parent_run_id="run-1",
        session_id_hash=forged,
        cwd_hash=hash_cwd(tmp_path),
        quiet_window_s=0.05,
    )
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_IDENTITY"
        assert "session identity hash" in str(ei.value)
        assert sess._receipt is None
    finally:
        sess.close()


def test_handshake_rejects_forged_cwd_hash_writes_no_receipt(tmp_path: Path) -> None:
    proc, argv = _spawn("success", tmp_path)
    sid = str(uuid.uuid4())
    forged = "b" * 64
    assert forged != hash_cwd(tmp_path)
    sess = AcpStdioSession(
        proc=proc,
        argv=argv,
        session_id=sid,
        cwd=str(tmp_path.resolve()),
        job_id="20260101T000000Z-deadbeef",
        attempt=1,
        parent_run_id="run-1",
        session_id_hash=hash_session_id(sid),
        cwd_hash=forged,
        quiet_window_s=0.05,
    )
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_IDENTITY"
        assert "cwd identity hash" in str(ei.value)
        assert sess._receipt is None
    finally:
        sess.close()


def test_bind_constructor_identity_match_and_mismatch(tmp_path: Path) -> None:
    sid = str(uuid.uuid4())
    cwd = str(tmp_path.resolve())
    sid_hash = hash_session_id(sid)
    cwd_h = hash_cwd(cwd)
    assert bind_constructor_identity(sid, sid_hash, cwd, cwd_h) == (sid_hash, cwd_h)
    with pytest.raises(AcpError) as sid_mismatch:
        bind_constructor_identity(sid, "a" * 64, cwd, cwd_h)
    assert sid_mismatch.value.code == "E_ACP_IDENTITY"
    with pytest.raises(AcpError) as cwd_mismatch:
        bind_constructor_identity(sid, sid_hash, cwd, "b" * 64)
    assert cwd_mismatch.value.code == "E_ACP_IDENTITY"


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


def test_acp_resume_dual_identity_keys_rejected(tmp_path: Path) -> None:
    proc, argv = _spawn("session_id_dual", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0.05)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_IDENTITY"
        assert sess._receipt is None
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
        assert sess2._byte_budget[0] + len(sess2._rx_buf) > sess2.max_total_bytes
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


def test_validate_receipt_rejects_forged_resume_matched_wrong_session_hash() -> None:
    # test_cancel_unproven_retains_binding_blocks_second_sidecar is not reopened.
    rec = AcpResumeReceipt(
        job_id="20260101T000000Z-deadbeef",
        attempt=1,
        parent_run_id="run-1",
        session_id_hash=hash_session_id("sid"),
        cwd_hash=hash_cwd("."),
        resume_matched=True,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    expected_sid = rec.session_id_hash
    expected_cwd = rec.cwd_hash
    expected_parent = rec.parent_run_id
    forged_sid = "a" * 64
    forged_cwd = "b" * 64
    assert forged_sid != expected_sid
    assert forged_cwd != expected_cwd
    for field, forged in (
        ("session_id_hash", forged_sid),
        ("cwd_hash", forged_cwd),
        ("parent_run_id", "run-forged"),
    ):
        body = rec.to_dict()
        assert body["resume_matched"] is True
        body[field] = forged
        core = {k: v for k, v in body.items() if k != "receipt_sha256"}
        body["receipt_sha256"] = hashlib.sha256(
            json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with pytest.raises(AcpError) as ei:
            validate_receipt(
                body,
                session_id_hash=expected_sid,
                cwd_hash=expected_cwd,
                parent_run_id=expected_parent,
            )
        assert ei.value.code == "E_ACP_RECEIPT", field
        assert field in str(ei.value)


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
    with pytest.raises(AcpError) as dual_equal:
        validate_resume_result(
            {"sessionId": sid, "session_id": sid, "resumed": True}, sid
        )
    assert dual_equal.value.code == "E_ACP_IDENTITY"
    with pytest.raises(AcpError) as dual_unequal:
        validate_resume_result(
            {
                "sessionId": sid,
                "session_id": "00000000-0000-0000-0000-000000000000",
                "resumed": True,
            },
            sid,
        )
    assert dual_unequal.value.code == "E_ACP_IDENTITY"
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


def test_read_line_coalesced_valid_plus_oversize_suffix_overflows_before_timeout() -> None:
    """One OS read: valid under-cap line + oversized unterminated suffix.

    The leftover must raise E_ACP_OVERFLOW on the next read before timeout.
    """
    max_bytes = 32
    valid = b'{"id":1,"result":{}}\n'
    assert len(valid) - 1 < max_bytes
    suffix = b"x" * (max_bytes + 1)
    r_fd, w_fd = os.pipe()
    # Default buffering makes read(4096) wait for 4096/EOF.
    r_file = os.fdopen(r_fd, "rb", buffering=0)
    w_file = os.fdopen(w_fd, "wb", buffering=0)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = r_file

        def poll(self) -> None:
            return None

    proc = _FakeProc()
    rx_buf = bytearray()
    byte_budget = [0]
    try:
        w_file.write(valid + suffix)
        w_file.flush()
        w_file.close()
        first = _read_line(
            proc,
            max_bytes=max_bytes,
            deadline=time.monotonic() + 5.0,
            byte_budget=byte_budget,
            rx_buf=rx_buf,
        )
        assert first == valid[:-1]
        assert bytes(rx_buf) == suffix
        assert len(rx_buf) > max_bytes
        t0 = time.monotonic()
        with pytest.raises(AcpError) as ei:
            _read_line(
                proc,
                max_bytes=max_bytes,
                deadline=time.monotonic() + 5.0,
                byte_budget=byte_budget,
                rx_buf=rx_buf,
            )
        elapsed = time.monotonic() - t0
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
        assert elapsed < 0.5
    finally:
        r_file.close()
        w_file.close()


def test_allowlisted_acp_env_preserves_exact_suffix_bytes() -> None:
    forwarded = allowlisted_acp_env(
        {
            "OMG_ACP_FAKE_SUFFIX_BYTES": "317",
            "AWS_SECRET_ACCESS_KEY": "nope",
        }
    )
    assert forwarded["OMG_ACP_FAKE_SUFFIX_BYTES"] == "317"
    assert "317" != "400"
    assert "AWS_SECRET_ACCESS_KEY" not in forwarded
    empty = allowlisted_acp_env({"OMG_ACP_FAKE_SUFFIX_BYTES": ""})
    assert "OMG_ACP_FAKE_SUFFIX_BYTES" not in empty


def test_handshake_coalesced_oversize_suffix_writes_no_receipt(tmp_path: Path) -> None:
    # Cap must admit initialize/resume frames but reject the leftover suffix.
    # Non-default 317: fixture default is 400; missing allowlist would hide this.
    assert 317 != 400
    assert 317 > 256
    assert (
        allowlisted_acp_env({"OMG_ACP_FAKE_SUFFIX_BYTES": "317"})[
            "OMG_ACP_FAKE_SUFFIX_BYTES"
        ]
        == "317"
    )
    proc, argv = _spawn(
        "resume_plus_oversize_suffix",
        tmp_path,
        env={"OMG_ACP_FAKE_SUFFIX_BYTES": "317"},
    )
    sess = _session(proc, argv, tmp_path, quiet=0.3)
    sess.max_line_bytes = 256
    try:
        t0 = time.monotonic()
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        elapsed = time.monotonic() - t0
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
        assert sess._receipt is None
        assert elapsed < 2.0
    finally:
        sess.close()


def test_read_line_committed_plus_buffered_suffix_overflows_before_timeout() -> None:
    """Valid NL line + under-line-cap leftover still trips max_total_bytes."""
    max_bytes = 64
    valid = b'{"id":1,"result":{}}\n'
    suffix = b"x" * 20
    max_total = 40
    assert len(valid) - 1 < max_bytes
    assert len(suffix) < max_bytes
    assert len(valid) + len(suffix) > max_total
    r_fd, w_fd = os.pipe()
    r_file = os.fdopen(r_fd, "rb", buffering=0)
    w_file = os.fdopen(w_fd, "wb", buffering=0)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = r_file

        def poll(self) -> None:
            return None

    proc = _FakeProc()
    rx_buf = bytearray()
    byte_budget = [0]
    try:
        w_file.write(valid + suffix)
        w_file.flush()
        w_file.close()
        first = _read_line(
            proc,
            max_bytes=max_bytes,
            deadline=time.monotonic() + 5.0,
            byte_budget=byte_budget,
            rx_buf=rx_buf,
            max_total_bytes=max_total,
        )
        assert first == valid[:-1]
        assert bytes(rx_buf) == suffix
        assert len(rx_buf) < max_bytes
        t0 = time.monotonic()
        with pytest.raises(AcpError) as ei:
            _read_line(
                proc,
                max_bytes=max_bytes,
                deadline=time.monotonic() + 5.0,
                byte_budget=byte_budget,
                rx_buf=rx_buf,
                max_total_bytes=max_total,
            )
        elapsed = time.monotonic() - t0
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "byte overflow" in str(ei.value)
        assert "line overflow" not in str(ei.value)
        assert elapsed < 0.5
    finally:
        r_file.close()
        w_file.close()


def test_read_line_exactly_at_total_limit_succeeds() -> None:
    """Exactly-at-limit uses ``>`` (combined size == max_total_bytes is ok)."""
    max_bytes = 64
    line1 = b'{"id":1,"result":{}}\n'
    line2 = b'{"id":2,"result":{}}\n'
    max_total = len(line1) + len(line2)
    r_fd, w_fd = os.pipe()
    r_file = os.fdopen(r_fd, "rb", buffering=0)
    w_file = os.fdopen(w_fd, "wb", buffering=0)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = r_file

        def poll(self) -> None:
            return None

    proc = _FakeProc()
    rx_buf = bytearray()
    byte_budget = [0]
    try:
        w_file.write(line1 + line2)
        w_file.flush()
        w_file.close()
        first = _read_line(
            proc,
            max_bytes=max_bytes,
            deadline=time.monotonic() + 5.0,
            byte_budget=byte_budget,
            rx_buf=rx_buf,
            max_total_bytes=max_total,
        )
        second = _read_line(
            proc,
            max_bytes=max_bytes,
            deadline=time.monotonic() + 5.0,
            byte_budget=byte_budget,
            rx_buf=rx_buf,
            max_total_bytes=max_total,
        )
        assert first == line1[:-1]
        assert second == line2[:-1]
        assert byte_budget[0] == max_total
        assert not rx_buf
    finally:
        r_file.close()
        w_file.close()

    # leftover-at-limit: committed + leftover == max_total must not overflow.
    line_a = b'{"id":1,"result":{}}\n'
    suffix = b"y" * (max_total - len(line_a))
    assert len(line_a) + len(suffix) == max_total
    assert len(suffix) < max_bytes
    r_fd, w_fd = os.pipe()
    r_file = os.fdopen(r_fd, "rb", buffering=0)
    w_file = os.fdopen(w_fd, "wb", buffering=0)
    proc = _FakeProc()
    proc.stdout = r_file
    rx_buf = bytearray()
    byte_budget = [0]
    try:
        w_file.write(line_a + suffix)
        w_file.flush()
        w_file.close()
        first = _read_line(
            proc,
            max_bytes=max_bytes,
            deadline=time.monotonic() + 5.0,
            byte_budget=byte_budget,
            rx_buf=rx_buf,
            max_total_bytes=max_total,
        )
        assert first == line_a[:-1]
        assert bytes(rx_buf) == suffix
        assert byte_budget[0] + len(rx_buf) == max_total
        with pytest.raises(AcpError) as ei:
            _read_line(
                proc,
                max_bytes=max_bytes,
                deadline=time.monotonic() + 0.15,
                byte_budget=byte_budget,
                rx_buf=rx_buf,
                max_total_bytes=max_total,
            )
        assert ei.value.code != "E_ACP_OVERFLOW"
        assert ei.value.code == "E_ACP_TIMEOUT"
    finally:
        r_file.close()
        w_file.close()


def test_read_line_completed_buffered_frame_counted_once() -> None:
    """Leftover prefix is not double-counted when the frame later completes."""
    max_bytes = 64
    line1 = b'{"id":1,"result":{}}\n'
    line2_prefix = b'{"id":2'
    line2_rest = b',"result":{}}\n'
    line2 = line2_prefix + line2_rest
    max_total = len(line1) + len(line2)
    assert len(line1) - 1 < max_bytes
    assert len(line2) - 1 < max_bytes
    r_fd, w_fd = os.pipe()
    r_file = os.fdopen(r_fd, "rb", buffering=0)
    w_file = os.fdopen(w_fd, "wb", buffering=0)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = r_file

        def poll(self) -> None:
            return None

    proc = _FakeProc()
    rx_buf = bytearray()
    byte_budget = [0]
    try:
        w_file.write(line1 + line2_prefix)
        w_file.flush()
        first = _read_line(
            proc,
            max_bytes=max_bytes,
            deadline=time.monotonic() + 5.0,
            byte_budget=byte_budget,
            rx_buf=rx_buf,
            max_total_bytes=max_total,
        )
        assert first == line1[:-1]
        assert bytes(rx_buf) == line2_prefix
        assert byte_budget[0] == len(line1)
        w_file.write(line2_rest)
        w_file.flush()
        second = _read_line(
            proc,
            max_bytes=max_bytes,
            deadline=time.monotonic() + 5.0,
            byte_budget=byte_budget,
            rx_buf=rx_buf,
            max_total_bytes=max_total,
        )
        assert second == line2[:-1]
        assert not rx_buf
        assert byte_budget[0] == len(line1) + len(line2)
    finally:
        r_file.close()
        w_file.close()


def test_handshake_committed_plus_buffered_suffix_writes_no_receipt(
    tmp_path: Path,
) -> None:
    # Derive max_total from the same initialize/resume response encoding the
    # fake peer writes so leftover suffix trips the cumulative cap, not a guess.
    sid = str(uuid.uuid4())
    suffix_env = "80"
    suffix_len = int(suffix_env)
    max_line_bytes = 256
    init_frame = _peer_response_frame(
        rpc_id=1,
        result={"protocolVersion": 1, "agentInfo": {"name": "fake-acp"}},
    )
    resume_frame = _peer_response_frame(
        rpc_id=2,
        result={"sessionId": sid, "resumed": True},
    )
    completed = len(init_frame) + len(resume_frame)
    max_total = completed + suffix_len - 1
    assert completed <= max_total
    assert completed + suffix_len > max_total
    assert suffix_len < max_line_bytes
    assert 400 > max_line_bytes
    assert (
        allowlisted_acp_env({"OMG_ACP_FAKE_SUFFIX_BYTES": suffix_env})[
            "OMG_ACP_FAKE_SUFFIX_BYTES"
        ]
        == "80"
    )
    proc, argv = _spawn(
        "resume_plus_oversize_suffix",
        tmp_path,
        env={"OMG_ACP_FAKE_SUFFIX_BYTES": suffix_env},
    )
    sess = _session(proc, argv, tmp_path, quiet=0.3, session_id=sid)
    sess.max_line_bytes = max_line_bytes
    sess.max_total_bytes = max_total
    try:
        t0 = time.monotonic()
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        elapsed = time.monotonic() - t0
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "byte overflow" in str(ei.value)
        assert "line overflow" not in str(ei.value)
        assert sess._receipt is None
        assert elapsed < 2.0
        assert sess._byte_budget[0] == completed
        assert bytes(sess._rx_buf) == b"x" * suffix_len
        assert sess._byte_budget[0] + len(sess._rx_buf) > sess.max_total_bytes
    finally:
        sess.close()


def test_handshake_zero_quiet_window_oversize_suffix_writes_no_receipt(
    tmp_path: Path,
) -> None:
    proc, argv = _spawn(
        "resume_plus_oversize_suffix",
        tmp_path,
        env={"OMG_ACP_FAKE_SUFFIX_BYTES": "400"},
    )
    sess = _session(proc, argv, tmp_path, quiet=0)
    sess.max_line_bytes = 256
    try:
        t0 = time.monotonic()
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        elapsed = time.monotonic() - t0
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
        assert sess._receipt is None
        assert elapsed < 2.0
    finally:
        sess.close()


def test_try_read_message_expired_deadline_rejects_line_overflow() -> None:
    max_line = 256
    r_fd, w_fd = os.pipe()
    r_file = os.fdopen(r_fd, "rb", buffering=0)
    w_file = os.fdopen(w_fd, "wb", buffering=0)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = r_file
            self.stdin = None
            self.pid = None

        def poll(self) -> None:
            return None

    proc = _FakeProc()
    sid = str(uuid.uuid4())
    sess = AcpStdioSession(
        proc=proc,
        argv=("fake",),
        session_id=sid,
        cwd=".",
        job_id="20260101T000000Z-deadbeef",
        attempt=1,
        parent_run_id="run-1",
        session_id_hash=hash_session_id(sid),
        cwd_hash=hash_cwd("."),
        max_line_bytes=max_line,
        max_total_bytes=50_000,
    )
    sess._rx_buf.extend(b"x" * (max_line + 1))
    assert sess._byte_budget[0] + len(sess._rx_buf) < sess.max_total_bytes
    try:
        with pytest.raises(AcpError) as ei:
            sess._try_read_message(
                deadline=time.monotonic() - 1.0,
                allow_timeout=True,
                cancel_event=None,
            )
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
    finally:
        r_file.close()
        w_file.close()


def test_try_read_message_expired_deadline_rejects_either_buffered_limit() -> None:
    for kind in ("line", "total"):
        for allow_timeout in (True, False):
            r_fd, w_fd = os.pipe()
            r_file = os.fdopen(r_fd, "rb", buffering=0)
            w_file = os.fdopen(w_fd, "wb", buffering=0)

            class _FakeProc:
                def __init__(self) -> None:
                    self.stdout = r_file
                    self.stdin = None
                    self.pid = None

                def poll(self) -> None:
                    return None

            proc = _FakeProc()
            sid = str(uuid.uuid4())
            if kind == "line":
                max_line = 256
                max_total = 50_000
                leftover = b"x" * (max_line + 1)
            else:
                max_line = 256
                max_total = 100
                leftover = b"x" * 80
            sess = AcpStdioSession(
                proc=proc,
                argv=("fake",),
                session_id=sid,
                cwd=".",
                job_id="20260101T000000Z-deadbeef",
                attempt=1,
                parent_run_id="run-1",
                session_id_hash=hash_session_id(sid),
                cwd_hash=hash_cwd("."),
                max_line_bytes=max_line,
                max_total_bytes=max_total,
            )
            if kind == "total":
                sess._byte_budget[0] = 40
            sess._rx_buf.extend(leftover)
            if kind == "line":
                assert sess._byte_budget[0] + len(sess._rx_buf) < sess.max_total_bytes
            else:
                assert len(sess._rx_buf) <= sess.max_line_bytes
                assert sess._byte_budget[0] + len(sess._rx_buf) > sess.max_total_bytes
            try:
                deadline = time.monotonic() - 1.0
                with pytest.raises(AcpError) as ei:
                    sess._try_read_message(
                        deadline=deadline,
                        allow_timeout=allow_timeout,
                        cancel_event=None,
                    )
                assert ei.value.code == "E_ACP_OVERFLOW", (kind, allow_timeout, ei.value)
                assert ei.value.code != "E_ACP_TIMEOUT", (kind, allow_timeout)
                if kind == "line":
                    assert "line overflow" in str(ei.value)
                else:
                    assert "byte overflow" in str(ei.value)
                    assert "line overflow" not in str(ei.value)
            finally:
                r_file.close()
                w_file.close()


def test_handshake_expired_deadline_oversize_suffix_writes_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli import host_acp as host_acp_mod

    proc, argv = _spawn(
        "resume_plus_oversize_suffix",
        tmp_path,
        env={"OMG_ACP_FAKE_SUFFIX_BYTES": "400"},
    )
    sess = _session(proc, argv, tmp_path, quiet=0.5)
    sess.max_line_bytes = 256
    orig_await = sess._await_result
    jump = {"on": False}

    def _await_wrapper(*args: object, **kwargs: object):
        result = orig_await(*args, **kwargs)
        expect_id = args[0] if args else kwargs.get("expect_id")
        if expect_id == 2:
            jump["on"] = True
        return result

    monkeypatch.setattr(sess, "_await_result", _await_wrapper)
    real_mono = host_acp_mod.time.monotonic

    def _jumped_mono() -> float:
        now = real_mono()
        if jump["on"]:
            return now + 10.0
        return now

    monkeypatch.setattr(host_acp_mod.time, "monotonic", _jumped_mono)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
        assert sess._receipt is None
    finally:
        sess.close()


def test_handshake_final_chrome_exhausts_quiet_window_oversize_suffix_writes_no_receipt(
    tmp_path: Path,
) -> None:
    proc, argv = _spawn(
        "resume_plus_chrome_plus_suffix",
        tmp_path,
        env={"OMG_ACP_FAKE_SUFFIX_BYTES": "400"},
    )
    sess = _session(proc, argv, tmp_path, quiet=0.001)
    sess.max_line_bytes = 256
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
        assert sess._receipt is None
    finally:
        sess.close()


def test_handshake_zero_quiet_window_under_limit_succeeds(tmp_path: Path) -> None:
    proc, argv = _spawn("success", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0)
    try:
        receipt = sess.handshake(timeout_s=5.0)
        assert receipt.resume_matched is True
        assert sess._receipt is not None
    finally:
        sess.close()


def test_handshake_zero_quiet_window_oversize_terminated_frame_writes_no_receipt(
    tmp_path: Path,
) -> None:
    proc, argv = _spawn(
        "resume_plus_oversize_terminated_frame",
        tmp_path,
        env={"OMG_ACP_FAKE_SUFFIX_BYTES": "400"},
    )
    sess = _session(proc, argv, tmp_path, quiet=0)
    sess.max_line_bytes = 256
    try:
        t0 = time.monotonic()
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        elapsed = time.monotonic() - t0
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
        assert "byte overflow" not in str(ei.value)
        assert sess._receipt is None
        assert elapsed < 2.0
    finally:
        sess.close()


def test_handshake_expired_deadline_oversize_terminated_frame_writes_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli import host_acp as host_acp_mod

    proc, argv = _spawn(
        "resume_plus_oversize_terminated_frame",
        tmp_path,
        env={"OMG_ACP_FAKE_SUFFIX_BYTES": "400"},
    )
    sess = _session(proc, argv, tmp_path, quiet=0.5)
    sess.max_line_bytes = 256
    orig_await = sess._await_result
    jump = {"on": False}

    def _await_wrapper(*args: object, **kwargs: object):
        result = orig_await(*args, **kwargs)
        expect_id = args[0] if args else kwargs.get("expect_id")
        if expect_id == 2:
            jump["on"] = True
        return result

    monkeypatch.setattr(sess, "_await_result", _await_wrapper)
    real_mono = host_acp_mod.time.monotonic

    def _jumped_mono() -> float:
        now = real_mono()
        if jump["on"]:
            return now + 10.0
        return now

    monkeypatch.setattr(host_acp_mod.time, "monotonic", _jumped_mono)
    try:
        with pytest.raises(AcpError) as ei:
            sess.handshake(timeout_s=5.0)
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
        assert "byte overflow" not in str(ei.value)
        assert sess._receipt is None
    finally:
        sess.close()


def test_try_read_message_expired_deadline_rejects_terminated_oversize_frame() -> None:
    max_line = 256
    r_fd, w_fd = os.pipe()
    r_file = os.fdopen(r_fd, "rb", buffering=0)
    w_file = os.fdopen(w_fd, "wb", buffering=0)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = r_file
            self.stdin = None
            self.pid = None

        def poll(self) -> None:
            return None

    proc = _FakeProc()
    sid = str(uuid.uuid4())
    sess = AcpStdioSession(
        proc=proc,
        argv=("fake",),
        session_id=sid,
        cwd=".",
        job_id="20260101T000000Z-deadbeef",
        attempt=1,
        parent_run_id="run-1",
        session_id_hash=hash_session_id(sid),
        cwd_hash=hash_cwd("."),
        max_line_bytes=max_line,
        max_total_bytes=50_000,
    )
    sess._rx_buf.extend(b"x" * (max_line + 1) + b"\n")
    assert _incomplete_line_len(sess._rx_buf) == 0
    assert sess._byte_budget[0] + len(sess._rx_buf) < sess.max_total_bytes
    try:
        for allow_timeout in (True, False):
            with pytest.raises(AcpError) as ei:
                sess._try_read_message(
                    deadline=time.monotonic() - 1.0,
                    allow_timeout=allow_timeout,
                    cancel_event=None,
                )
            assert ei.value.code == "E_ACP_OVERFLOW"
            assert ei.value.code != "E_ACP_TIMEOUT"
            assert "line overflow" in str(ei.value)
            assert "byte overflow" not in str(ei.value)
    finally:
        r_file.close()
        w_file.close()


def test_handshake_zero_quiet_window_under_limit_multiple_frames_succeeds(
    tmp_path: Path,
) -> None:
    proc, argv = _spawn("resume_plus_under_limit_frames", tmp_path)
    sess = _session(proc, argv, tmp_path, quiet=0)
    sess.max_line_bytes = 256
    try:
        receipt = sess.handshake(timeout_s=5.0)
        assert receipt.resume_matched is True
        assert sess._receipt is not None
        leftover = bytes(sess._rx_buf)
        assert leftover == b"y" * 200 + b"\n" + b"z" * 200 + b"\n"
        assert len(leftover) - leftover.count(b"\n") == 400
        assert 400 > sess.max_line_bytes
        assert _max_buffered_frame_len(sess._rx_buf) == 200
        assert _incomplete_line_len(sess._rx_buf) == 0
    finally:
        sess.close()


def test_read_line_coalesced_valid_plus_terminated_oversize_extracts_first() -> None:
    """One OS read: valid under-cap line + fully NL-terminated oversized frame.

    Extract-first must return the valid frame; the leftover complete frame
    overflows on the next read (not as a combined line on the first).
    """
    max_bytes = 32
    valid = b'{"id":1,"result":{}}\n'
    assert len(valid) - 1 < max_bytes
    suffix = b"x" * (max_bytes + 1) + b"\n"
    r_fd, w_fd = os.pipe()
    r_file = os.fdopen(r_fd, "rb", buffering=0)
    w_file = os.fdopen(w_fd, "wb", buffering=0)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = r_file

        def poll(self) -> None:
            return None

    proc = _FakeProc()
    rx_buf = bytearray()
    byte_budget = [0]
    try:
        w_file.write(valid + suffix)
        w_file.flush()
        w_file.close()
        first = _read_line(
            proc,
            max_bytes=max_bytes,
            deadline=time.monotonic() + 5.0,
            byte_budget=byte_budget,
            rx_buf=rx_buf,
        )
        assert first == valid[:-1]
        assert bytes(rx_buf) == suffix
        assert _incomplete_line_len(rx_buf) == 0
        assert _max_buffered_frame_len(rx_buf) == max_bytes + 1
        t0 = time.monotonic()
        with pytest.raises(AcpError) as ei:
            _read_line(
                proc,
                max_bytes=max_bytes,
                deadline=time.monotonic() + 5.0,
                byte_budget=byte_budget,
                rx_buf=rx_buf,
            )
        elapsed = time.monotonic() - t0
        assert ei.value.code == "E_ACP_OVERFLOW"
        assert "line overflow" in str(ei.value)
        assert elapsed < 0.5
    finally:
        r_file.close()
        w_file.close()


def test_raise_if_buffered_limits_complete_frame_flag() -> None:
    max_line = 256
    max_total = 50_000
    terminated = bytearray(b"x" * (max_line + 1) + b"\n")
    budget = [0]
    assert _incomplete_line_len(terminated) == 0
    assert _max_buffered_frame_len(terminated) == max_line + 1
    assert _max_buffered_frame_len(bytearray()) == 0
    # leftover-only documents the hole: complete oversized frame is invisible.
    _raise_if_buffered_limits(
        budget,
        terminated,
        max_line_bytes=max_line,
        max_total_bytes=max_total,
        include_complete_frames=False,
    )
    with pytest.raises(AcpError) as ei:
        _raise_if_buffered_limits(
            budget,
            terminated,
            max_line_bytes=max_line,
            max_total_bytes=max_total,
            include_complete_frames=True,
        )
    assert ei.value.code == "E_ACP_OVERFLOW"
    assert "line overflow" in str(ei.value)
    assert "byte overflow" not in str(ei.value)

    two = bytearray(b"y" * 200 + b"\n" + b"z" * 200 + b"\n")
    assert _max_buffered_frame_len(two) == 200
    _raise_if_buffered_limits(
        [0],
        two,
        max_line_bytes=256,
        max_total_bytes=50_000,
        include_complete_frames=True,
    )
    with pytest.raises(AcpError) as ei2:
        _raise_if_buffered_limits(
            [0],
            two,
            max_line_bytes=256,
            max_total_bytes=len(two) - 1,
            include_complete_frames=True,
        )
    assert ei2.value.code == "E_ACP_OVERFLOW"
    assert "byte overflow" in str(ei2.value)
    assert "line overflow" not in str(ei2.value)
