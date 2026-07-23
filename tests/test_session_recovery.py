from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from omg_cli.contracts.resume_contract import RECOVERY_CAPS, WARNING_ORDER
from omg_cli.session_recovery import SessionRecoveryError, recover_session


FIXTURES = Path(__file__).parent / "fixtures" / "recovery"


def test_golden_913_line_recovery_is_immutable_partial_and_124_turns(tmp_path) -> None:
    source = FIXTURES / "source-913-lines-broken-chain-v1.jsonl"
    result = recover_session(source, tmp_path / "recovery")
    manifest = result["manifest"]

    assert manifest["counters"]["physical_lines_seen"] == 913
    assert manifest["counters"]["physical_lines_retained"] == 900
    assert manifest["counters"]["physical_lines_omitted_oldest"] == 13
    assert manifest["counters"]["recognized_records_retained"] == 897
    assert manifest["counters"]["unknown_records_retained"] == 3
    assert manifest["counters"]["complete_turns_retained"] == 124
    assert manifest["warnings"] == [
        "W_BROKEN_CHAIN",
        "W_PARTIAL_RECOVERY",
        "W_TRUNCATED_SOURCE",
        "W_UNKNOWN_RECORD_TYPE",
    ]
    assert manifest["partial"] is True
    copy_path = Path(result["immutable_copy_path"])
    assert stat.S_IMODE(copy_path.stat().st_mode) == 0o400
    assert copy_path.read_bytes() == (
        FIXTURES / "bounded-900-lines-broken-chain-v1.jsonl"
    ).read_bytes()
    context = Path(result["context_path"]).read_text(encoding="utf-8")
    assert "opaque" not in context and "future_alpha" not in context
    assert [warning for warning in WARNING_ORDER if warning in manifest["warnings"]] == manifest[
        "warnings"
    ]

    replay = recover_session(source, tmp_path / "recovery")
    assert replay["manifest_path"] != result["manifest_path"]
    assert replay["immutable_copy_path"] != result["immutable_copy_path"]
    assert Path(replay["immutable_copy_path"]).read_bytes() == copy_path.read_bytes()
    assert stat.S_IMODE(Path(result["manifest_path"]).stat().st_mode) == 0o400
    assert stat.S_IMODE(Path(result["receipt_path"]).stat().st_mode) == 0o400


def test_recovery_rejects_symlink_source_without_fallback(tmp_path) -> None:
    real = tmp_path / "real.jsonl"
    real.write_text('{}\n', encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    with pytest.raises(SessionRecoveryError, match="E_RESUME_SOURCE_NOT_REGULAR"):
        recover_session(link, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_frozen_caps_are_consumed_not_redefined() -> None:
    assert RECOVERY_CAPS == {
        "source_bytes": 16_777_216,
        "physical_line_bytes": 1_048_576,
        "physical_lines": 900,
        "parsed_records": 900,
        "complete_turns": 256,
        "context_bytes": 2_097_152,
    }


def test_single_complete_turn_over_context_cap_persists_manifest_without_prompt(tmp_path) -> None:
    user_text = "u" * 800_000
    assistant_text = "a" * 700_000
    rows = [
        {"event_id": "s", "prev_event_id": None, "type": "turn_start", "payload": {"turn_id": "t"}},
        {"event_id": "u", "prev_event_id": "s", "type": "user_message", "payload": {"turn_id": "t", "text": user_text}},
        {"event_id": "a1", "prev_event_id": "u", "type": "assistant_message", "payload": {"turn_id": "t", "text": assistant_text}},
        {"event_id": "a2", "prev_event_id": "a1", "type": "assistant_message", "payload": {"turn_id": "t", "text": assistant_text}},
        {"event_id": "e", "prev_event_id": "a2", "type": "turn_end", "payload": {"turn_id": "t"}},
    ]
    source = tmp_path / "huge.jsonl"
    source.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    result = recover_session(source, tmp_path / "out")
    assert result["error"] == "E_RESUME_CONTEXT_OVER_CAP"
    assert result["context_path"] is None
    assert Path(result["manifest_path"]).is_file()


def test_zero_complete_turns_is_explicit_error_without_prompt(tmp_path) -> None:
    source = tmp_path / "fragment.jsonl"
    source.write_text(
        '{"event_id":"a","prev_event_id":null,"type":"assistant_message",'
        '"payload":{"turn_id":"incomplete","text":"fragment"}}\n',
        encoding="utf-8",
    )
    result = recover_session(source, tmp_path / "out")
    assert result["error"] == "E_RESUME_NO_COMPLETE_TURNS"
    assert result["context_path"] is None
    assert Path(result["manifest_path"]).is_file()


def test_illegal_event_order_is_partial_and_never_invents_a_complete_turn(
    tmp_path,
) -> None:
    rows = [
        {"event_id": "s", "prev_event_id": None, "type": "turn_start", "payload": {"turn_id": "t"}},
        {"event_id": "a", "prev_event_id": "s", "type": "assistant_message", "payload": {"turn_id": "t", "text": "too early"}},
        {"event_id": "u", "prev_event_id": "a", "type": "user_message", "payload": {"turn_id": "t", "text": "late"}},
        {"event_id": "e", "prev_event_id": "u", "type": "turn_end", "payload": {"turn_id": "t"}},
    ]
    source = tmp_path / "illegal.jsonl"
    source.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    result = recover_session(source, tmp_path / "out")
    assert result["error"] == "E_RESUME_NO_COMPLETE_TURNS"
    assert result["manifest"]["counters"]["complete_turns_seen"] == 0
    assert result["manifest"]["warnings"] == ["W_PARTIAL_RECOVERY"]
