#!/usr/bin/env python3
"""Hermetic line-delimited JSON-RPC ACP peer for #105 PR4/PR5 tests.

Scenarios via ``OMG_ACP_FAKE_SCENARIO``:

  success          — initialize + resume + optional chrome; stay alive
  replay           — conversation update before resume response
  late_replay      — conversation update during quiet window after resume
  chrome           — non-conversation chrome before/after resume
  hang             — never respond to initialize
  overflow         — emit oversized line
  wrong_id         — respond with mismatched JSON-RPC id
  rpc_error        — resume returns RPC error
  malformed        — emit non-JSON frame
  exit_after_resume — exit immediately after resume (transient false success)
  resume_false     — resume result resumed=false (no receipt)
  resume_plus_oversize_suffix — valid resume result + unterminated oversize suffix in one write (OMG_ACP_FAKE_SUFFIX_BYTES, default 400)
  resume_plus_oversize_terminated_frame — same as resume_plus_oversize_suffix but suffix is fully NL-terminated: resume_json + NL + x*n + NL (OMG_ACP_FAKE_SUFFIX_BYTES, default 400)
  resume_plus_under_limit_frames — valid resume result + two complete 200-byte frames (combined payload 400 > typical max_line=256; each frame 200 < 256)
  resume_plus_chrome_plus_suffix — valid resume result + chrome session/update + unterminated oversize suffix in one write (OMG_ACP_FAKE_SUFFIX_BYTES, default 400)
  session_id_mismatch — resume result sessionId ≠ requested UUID
  resume_missing_flag — resume result omits resumed
  session_id_alias — resume result uses session_id alias only
  session_id_dual  — resume result includes both sessionId and session_id (equal)
  stderr_flood     — flood stderr before handshake (PIPE deadlock probe)
  many_small_chrome — many chrome frames before resume result
"""

from __future__ import annotations

import json
import os
import sys
import time


def _read_msg() -> dict | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def _write(obj: dict) -> None:
    sys.stdout.buffer.write((json.dumps(obj) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _notify_update(kind: str, **extra: object) -> None:
    update: dict = {"sessionUpdate": kind, **extra}
    _write(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"update": update},
        }
    )


def main() -> int:
    scenario = (os.environ.get("OMG_ACP_FAKE_SCENARIO") or "success").strip().lower()
    delay = float(os.environ.get("OMG_ACP_FAKE_DELAY_S") or "0")

    if scenario == "stderr_flood":
        # Fill an undrained stderr PIPE before reading stdin so handshake
        # deadlocks unless the client drains. Never write this to stdout.
        blob = b"x" * 65536
        for _ in range(64):
            try:
                sys.stderr.buffer.write(blob)
                sys.stderr.buffer.flush()
            except BrokenPipeError:
                break

    if scenario == "hang":
        while True:
            time.sleep(1.0)

    if scenario == "overflow":
        # Wait for initialize then flood.
        msg = _read_msg()
        if msg and msg.get("id") is not None:
            _write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        sys.stdout.buffer.write(b"x" * (300_000) + b"\n")
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "malformed":
        msg = _read_msg()
        if msg and msg.get("id") is not None:
            sys.stdout.buffer.write(b"not-json{{{\n")
            sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    # Standard initialize
    init = _read_msg()
    if init is None:
        return 1
    if init.get("method") != "initialize":
        _write(
            {
                "jsonrpc": "2.0",
                "id": init.get("id"),
                "error": {"code": -32601, "message": "expected initialize first"},
            }
        )
        return 1
    if delay:
        time.sleep(delay)
    if scenario == "wrong_id":
        _write({"jsonrpc": "2.0", "id": "nope", "result": {"protocolVersion": 1}})
    else:
        _write(
            {
                "jsonrpc": "2.0",
                "id": init["id"],
                "result": {"protocolVersion": 1, "agentInfo": {"name": "fake-acp"}},
            }
        )

    if scenario == "chrome":
        _notify_update("current_mode_update", mode="default")

    resume = _read_msg()
    if resume is None:
        return 1
    if resume.get("method") != "session/resume":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume.get("id"),
                "error": {"code": -32601, "message": "expected session/resume"},
            }
        )
        return 1

    params = resume.get("params") or {}
    # Echo validated binding for tests (not written to OMG receipts).
    _ = params.get("sessionId"), params.get("cwd")

    if scenario == "replay":
        _notify_update(
            "agent_message_chunk",
            content={"type": "text", "text": "SECRET_REPLAY_BODY"},
        )

    if scenario == "rpc_error":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "error": {"code": -32000, "message": "resume refused"},
            }
        )
        time.sleep(60)
        return 0

    sid = params.get("sessionId")

    if scenario == "resume_false":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {"sessionId": sid, "resumed": False},
            }
        )
        time.sleep(60)
        return 0

    if scenario == "resume_plus_oversize_suffix":
        try:
            n = int(os.environ.get("OMG_ACP_FAKE_SUFFIX_BYTES") or "400")
        except ValueError:
            n = 400
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {"sessionId": sid, "resumed": True},
                }
            ).encode("utf-8")
            + b"\n"
            + b"x" * max(0, n)
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "resume_plus_oversize_terminated_frame":
        try:
            n = int(os.environ.get("OMG_ACP_FAKE_SUFFIX_BYTES") or "400")
        except ValueError:
            n = 400
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {"sessionId": sid, "resumed": True},
                }
            ).encode("utf-8")
            + b"\n"
            + b"x" * max(0, n)
            + b"\n"
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "resume_plus_under_limit_frames":
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {"sessionId": sid, "resumed": True},
                }
            ).encode("utf-8")
            + b"\n"
            + b"y" * 200
            + b"\n"
            + b"z" * 200
            + b"\n"
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "resume_plus_chrome_plus_suffix":
        try:
            n = int(os.environ.get("OMG_ACP_FAKE_SUFFIX_BYTES") or "400")
        except ValueError:
            n = 400
        # Same chrome shape as _notify_update("current_mode_update", mode="default").
        chrome = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {"sessionUpdate": "current_mode_update", "mode": "default"}
            },
        }
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {"sessionId": sid, "resumed": True},
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(chrome).encode("utf-8")
            + b"\n"
            + b"x" * max(0, n)
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "session_id_mismatch":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {
                    "sessionId": "00000000-0000-0000-0000-000000000000",
                    "resumed": True,
                },
            }
        )
        time.sleep(60)
        return 0

    if scenario == "resume_missing_flag":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {"sessionId": sid},
            }
        )
        time.sleep(60)
        return 0

    if scenario == "many_small_chrome":
        try:
            count = int(os.environ.get("OMG_ACP_FAKE_CHROME_COUNT") or "80")
        except ValueError:
            count = 80
        for i in range(max(0, count)):
            _notify_update("current_mode_update", mode=f"m{i:04d}")

    if delay:
        time.sleep(delay)
    if scenario == "session_id_alias":
        resume_result: dict = {"session_id": sid, "resumed": True}
    elif scenario == "session_id_dual":
        resume_result = {"sessionId": sid, "session_id": sid, "resumed": True}
    else:
        resume_result = {"sessionId": sid, "resumed": True}
    _write(
        {
            "jsonrpc": "2.0",
            "id": resume["id"],
            "result": resume_result,
        }
    )

    if scenario == "late_replay":
        # Emit immediately so the client's quiet window observes replay even
        # under slow CI scheduling (no sleep race before the notification).
        _notify_update(
            "agent_message_chunk",
            content={"type": "text", "text": "LATE_SECRET_REPLAY"},
        )

    if scenario == "chrome":
        _notify_update("available_commands_update", commands=[])
        _notify_update("session_info_update", title="t")

    if scenario == "exit_after_resume":
        return 0

    # Stay alive; drain stdin until EOF; emit periodic chrome.
    while True:
        msg = _read_msg()
        if msg is None:
            break
        # Ignore further client methods; stay connected.
        if msg.get("id") is not None:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32601, "message": "method not supported"},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
