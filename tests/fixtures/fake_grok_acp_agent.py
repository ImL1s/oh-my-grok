#!/usr/bin/env python3
"""Hermetic line-delimited JSON-RPC ACP peer for #105 PR4/PR5 tests.

Scenarios via ``OMG_ACP_FAKE_SCENARIO``:

  success          — initialize + resume result {} + optional chrome; stay alive
  resume_populated — fully populated allowed keys (modes SessionModeState, models {}, configOptions [], _meta {})
  replay           — conversation update before resume response
  late_replay      — conversation update during quiet window after resume
  chrome           — non-conversation chrome before/after resume
  vendor_chrome    — Grok ``_x.ai/*`` notifications + available_commands_update
                     before resume result (secret in params must not leak)
  hang             — never respond to initialize
  hang_after_init  — initialize OK, then sleep forever (never read/respond to resume)
  overflow         — emit oversized line
  wrong_id         — respond with mismatched JSON-RPC id
  rpc_error        — resume returns RPC error
  malformed        — emit non-JSON frame
  exit_after_resume — exit immediately after resume (transient false success)
  resume_false     — unknown top-level resumed (no receipt)
  resume_nonempty_messages — unknown top-level messages (no receipt)
  resume_nested_replay — unknown top-level history (no receipt)
  resume_contradictory_restore — unknown top-level restoreCode/noReplay (no receipt)
  resume_unknown_field — unknown top-level futureReplayBag (no receipt)
  resume_empty_messages — unknown top-level messages (no receipt)
  resume_explicit_noreplay — unknown top-level noReplay/restoreCode (no receipt)
  resume_modes_wrong_type — modes is a string (no receipt)
  resume_models_wrong_type — models is an array (no receipt)
  resume_config_options_wrong_type — configOptions is an object (no receipt)
  resume_meta_wrong_type — _meta is a string (no receipt)
  resume_unknown_pad — unknown top-level pad inside result (no receipt)
  resume_plus_oversize_suffix — valid resume result {} + unterminated oversize suffix in one write (OMG_ACP_FAKE_SUFFIX_BYTES, default 400)
  resume_plus_oversize_terminated_frame — same as resume_plus_oversize_suffix but suffix is fully NL-terminated: resume_json + NL + x*n + NL (OMG_ACP_FAKE_SUFFIX_BYTES, default 400)
  resume_plus_oversize_suffix_then_exit — resume_plus_oversize_suffix then exit (no stay-alive)
  resume_plus_oversize_terminated_frame_then_exit — resume_plus_oversize_terminated_frame then exit
  resume_plus_under_limit_frames — valid resume result {} + two complete 200-byte chrome frames (combined payload 400 > typical max_line=256; each frame 200 < 256)
  resume_plus_chrome_plus_suffix — valid resume result {} + chrome session/update + unterminated oversize suffix in one write (OMG_ACP_FAKE_SUFFIX_BYTES, default 400)
  resume_plus_replay_coalesced — valid resume result {} + forbidden agent_message_chunk in one write
  resume_plus_replay_coalesced_then_exit — resume_plus_replay_coalesced then exit
  resume_plus_malformed_coalesced — valid resume result {} + non-JSON complete frame in one write
  resume_plus_unknown_coalesced — valid resume result {} + unknown session/update in one write
  resume_exact_chunk_plus_oversize_suffix — resume JSON-RPC envelope padded to exactly 4096 bytes (result {}) + unterminated suffix (OMG_ACP_FAKE_SUFFIX_BYTES)
  resume_plus_under_limit_suffix_then_exit — resume {} + small unterminated suffix then exit
  resume_plus_chrome_then_exit — resume {} + complete current_mode_update chrome then exit
  session_id_mismatch — unknown top-level sessionId (no receipt)
  resume_missing_flag — unknown top-level sessionId only (no receipt)
  session_id_alias — unknown top-level session_id (no receipt)
  session_id_dual  — unknown top-level sessionId and session_id (no receipt)
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


def _padded_chrome_frame(pad_char: str, payload_len: int) -> bytes:
    """Compact chrome session/update whose JSON payload is exactly *payload_len*."""

    def _encode(pad: str) -> bytes:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "current_mode_update",
                        "mode": "default",
                        "pad": pad,
                    }
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")

    empty = _encode("")
    need = payload_len - len(empty)
    if need < 0:
        raise RuntimeError(
            f"chrome frame empty len {len(empty)} exceeds target {payload_len}"
        )
    out = _encode(pad_char * need)
    if len(out) != payload_len:
        raise RuntimeError(
            f"padded chrome frame len {len(out)} != target {payload_len}"
        )
    return out


def _close_stdout() -> None:
    """Publish pipe EOF before process teardown (no poll-reap race)."""
    try:
        sys.stdout.buffer.flush()
        sys.stdout.buffer.close()
    except (BrokenPipeError, ValueError, OSError):
        return


def _padded_resume_frame(rpc_id: object, _session_id: str, target_len: int) -> bytes:
    """Compact resume JSON-RPC envelope whose NL-terminated frame is *target_len*.

    Pad lives next to jsonrpc/id/result. Result is official empty ``{}``.
    """

    def _encode(pad: str) -> bytes:
        return (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {},
                    "pad": pad,
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    empty = _encode("")
    need = target_len - len(empty)
    if need < 0:
        raise RuntimeError(
            f"resume frame empty len {len(empty)} exceeds target {target_len}"
        )
    out = _encode("p" * need)
    if len(out) != target_len:
        raise RuntimeError(
            f"padded resume frame len {len(out)} != target {target_len}"
        )
    return out


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

    if scenario == "hang_after_init":
        while True:
            time.sleep(1.0)

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

    if scenario == "vendor_chrome":
        _write(
            {
                "jsonrpc": "2.0",
                "method": "_x.ai/mcp/init_progress",
                "params": {"phase": "start"},
            }
        )
        _write(
            {
                "jsonrpc": "2.0",
                "method": "_x.ai/mcp/servers_updated",
                "params": {
                    "mcpServers": [
                        {
                            "name": "x",
                            "env": [
                                {
                                    "name": "API_TOKEN",
                                    "value": "ACP_VENDOR_SECRET_TOKEN_DO_NOT_ECHO",
                                }
                            ],
                        }
                    ]
                },
            }
        )
        _write(
            {
                "jsonrpc": "2.0",
                "method": "_x.ai/session_notification",
                "params": {"kind": "ready"},
            }
        )
        _notify_update("available_commands_update")

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
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + b"x" * max(0, n)
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "resume_plus_oversize_suffix_then_exit":
        try:
            n = int(os.environ.get("OMG_ACP_FAKE_SUFFIX_BYTES") or "400")
        except ValueError:
            n = 400
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + b"x" * max(0, n)
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
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
                    "result": {},
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

    if scenario == "resume_plus_oversize_terminated_frame_then_exit":
        try:
            n = int(os.environ.get("OMG_ACP_FAKE_SUFFIX_BYTES") or "400")
        except ValueError:
            n = 400
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + b"x" * max(0, n)
            + b"\n"
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return 0

    if scenario == "resume_plus_under_limit_frames":
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + _padded_chrome_frame("y", 200)
            + b"\n"
            + _padded_chrome_frame("z", 200)
            + b"\n"
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "resume_exact_chunk_plus_oversize_suffix":
        try:
            n = int(os.environ.get("OMG_ACP_FAKE_SUFFIX_BYTES") or "400")
        except ValueError:
            n = 400
        payload = _padded_resume_frame(resume["id"], sid, 4096) + b"x" * max(0, n)
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "resume_plus_under_limit_suffix_then_exit":
        try:
            n = int(os.environ.get("OMG_ACP_FAKE_SUFFIX_BYTES") or "50")
        except ValueError:
            n = 50
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + b"x" * max(0, n)
        )
        sys.stdout.buffer.write(payload)
        _close_stdout()
        return 0

    if scenario == "resume_plus_chrome_then_exit":
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
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(chrome).encode("utf-8")
            + b"\n"
        )
        sys.stdout.buffer.write(payload)
        _close_stdout()
        return 0

    if scenario == "resume_plus_replay_coalesced_then_exit":
        replay = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "SECRET_REPLAY_BODY"},
                }
            },
        }
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(replay).encode("utf-8")
            + b"\n"
        )
        sys.stdout.buffer.write(payload)
        _close_stdout()
        return 0

    if scenario == "resume_plus_replay_coalesced":
        replay = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "SECRET_REPLAY_BODY"},
                }
            },
        }
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(replay).encode("utf-8")
            + b"\n"
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "resume_plus_malformed_coalesced":
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + b"not-json{{{\n"
        )
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0

    if scenario == "resume_plus_unknown_coalesced":
        unknown = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"update": {"sessionUpdate": "not_a_real_kind"}},
        }
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": resume["id"],
                    "result": {},
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(unknown).encode("utf-8")
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
                    "result": {},
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

    if scenario == "resume_nonempty_messages":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {
                    "sessionId": sid,
                    "resumed": True,
                    "messages": [{"text": "RESUME_SECRET_REPLAY"}],
                },
            }
        )
        time.sleep(60)
        return 0

    if scenario == "resume_nested_replay":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {
                    "sessionId": sid,
                    "resumed": True,
                    "history": {"content": "RESUME_SECRET_NESTED"},
                },
            }
        )
        time.sleep(60)
        return 0

    if scenario == "resume_contradictory_restore":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {
                    "sessionId": sid,
                    "resumed": True,
                    "restoreCode": True,
                    "noReplay": False,
                },
            }
        )
        time.sleep(60)
        return 0

    if scenario == "resume_unknown_field":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {
                    "sessionId": sid,
                    "resumed": True,
                    "futureReplayBag": {"transcript": "RESUME_SECRET_UNKNOWN"},
                },
            }
        )
        time.sleep(60)
        return 0

    if scenario == "resume_empty_messages":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {"sessionId": sid, "resumed": True, "messages": []},
            }
        )
        time.sleep(60)
        return 0

    if scenario == "resume_explicit_noreplay":
        _write(
            {
                "jsonrpc": "2.0",
                "id": resume["id"],
                "result": {
                    "sessionId": sid,
                    "resumed": True,
                    "noReplay": True,
                    "restoreCode": False,
                },
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
        resume_result: dict = {"session_id": sid}
    elif scenario == "session_id_dual":
        resume_result = {"sessionId": sid, "session_id": sid}
    elif scenario == "resume_populated":
        resume_result = {
            "modes": {
                "currentModeId": "default",
                "availableModes": [{"id": "default", "name": "Default"}],
            },
            "models": {},
            "configOptions": [],
            "_meta": {},
        }
    elif scenario == "resume_modes_wrong_type":
        resume_result = {"modes": "x"}
    elif scenario == "resume_models_wrong_type":
        resume_result = {"models": []}
    elif scenario == "resume_config_options_wrong_type":
        resume_result = {"configOptions": {}}
    elif scenario == "resume_meta_wrong_type":
        resume_result = {"_meta": "x"}
    elif scenario == "resume_unknown_pad":
        resume_result = {"pad": "xxxx"}
    else:
        resume_result = {}
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
        _close_stdout()
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
