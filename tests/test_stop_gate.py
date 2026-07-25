"""Stop-gate pure predicate tests (Task 1)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from omg_cli.stop_gate import decide_stop, continuation_reason

RUN_ID = "run-1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk(
    tmp: Path,
    phase: str,
    mode: str = "autopilot",
    awaiting: bool = False,
    reason: str = "end_turn",
    stop_hook_active: bool = False,
    last_msg: str | None = None,
    bg: list | None = None,
) -> dict:
    state_dir = tmp / ".omg" / "state"
    run_dir = state_dir / "runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    now = _utc_now()
    status = {
        "run_id": RUN_ID,
        "mode": mode,
        "goal": "test goal",
        "status": "running",
        "verified": False,
        "passes": 0,
        "created_at": now,
        "updated_at": now,
        "autopilot_phase": phase,
    }
    if awaiting:
        status["autopilot_awaiting"] = True
    (state_dir / "active.json").write_text(
        json.dumps({"run_id": RUN_ID, "updated_at": now}),
        encoding="utf-8",
    )
    (run_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    if phase == "interview":
        (run_dir / "interview.json").write_text(
            json.dumps({"run_id": RUN_ID, "status": "waiting_input"}),
            encoding="utf-8",
        )
    return {
        "reason": reason,
        "stopHookActive": stop_hook_active,
        "lastAssistantMessage": last_msg,
        "backgroundTasks": bg or [],
    }


def test_session_end_fire_never_blocks(tmp_path):
    ev = _mk(tmp_path, "implement", reason="channel_closed")
    assert decide_stop(tmp_path, ev) is None


def test_no_active_run_allows_stop(tmp_path):
    assert decide_stop(tmp_path, {"reason": "end_turn"}) is None


def test_non_autopilot_mode_allows_stop(tmp_path):
    ev = _mk(tmp_path, "implement", mode="ralph")
    assert decide_stop(tmp_path, ev) is None


def test_terminal_phases_allow_stop(tmp_path):
    for ph in ("verified", "cancelled"):
        assert decide_stop(tmp_path, _mk(tmp_path, ph)) is None


def test_awaiting_confirmation_allows_stop(tmp_path):
    ev = _mk(tmp_path, "implement", awaiting=True)
    assert decide_stop(tmp_path, ev) is None


def test_interview_waiting_input_allows_stop(tmp_path):
    ev = _mk(tmp_path, "interview")
    assert decide_stop(tmp_path, ev) is None


def test_background_tasks_in_flight_allow_stop(tmp_path):
    ev = _mk(
        tmp_path,
        "implement",
        bg=[{"id": "t1", "type": "shell", "status": "running"}],
    )
    assert decide_stop(tmp_path, ev) is None


def test_active_incomplete_blocks_with_phase_reason(tmp_path):
    ev = _mk(tmp_path, "review")
    d = decide_stop(tmp_path, ev)
    assert d is not None
    assert d["decision"] == "block"
    assert "review" in d["reason"] and "do not ask" in d["reason"].lower()


def test_stop_hook_active_escalates_message(tmp_path):
    base = decide_stop(tmp_path, _mk(tmp_path, "implement", stop_hook_active=False))
    escalated = decide_stop(tmp_path, _mk(tmp_path, "implement", stop_hook_active=True))
    assert base is not None and escalated is not None
    assert "already" not in base["reason"].lower()
    assert "already" in escalated["reason"].lower()


def test_shutdown_reason_allows_stop(tmp_path):
    assert decide_stop(tmp_path, _mk(tmp_path, "implement", reason="shutdown")) is None


def test_interview_not_waiting_blocks(tmp_path):
    ev = _mk(tmp_path, "interview")
    run_dir = tmp_path / ".omg" / "state" / "runs" / RUN_ID
    (run_dir / "interview.json").write_text(
        json.dumps({"run_id": RUN_ID, "status": "in_progress"}),
        encoding="utf-8",
    )
    d = decide_stop(tmp_path, ev)
    assert d is not None
    assert d["decision"] == "block"


def test_garbage_event_never_raises(tmp_path):
    assert decide_stop(tmp_path, None) is None  # type: ignore[arg-type]
    assert decide_stop(tmp_path, {"reason": "end_turn", "backgroundTasks": "bad"}) is None


def test_continuation_reason_names_next_gate():
    assert "structured_review.json" in continuation_reason("review", goal="g", run_id="r1")
    assert "ultraqa.json" in continuation_reason("qa", goal="g", run_id="r1")


def test_drift_guard_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OMG_STOP_DRIFT_GUARD", raising=False)
    ev = {"reason": "end_turn", "lastAssistantMessage": "Want me to keep going?"}
    assert decide_stop(tmp_path, ev, env=os.environ) is None


def test_drift_guard_blocks_chatty_question_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_STOP_DRIFT_GUARD", "1")
    ev = _mk(
        tmp_path,
        "implement",
        last_msg="Should I continue with the tests?",
        stop_hook_active=False,
    )
    d = decide_stop(tmp_path, ev, env=os.environ)
    assert d is not None
    assert d["decision"] == "block"
    assert "do not ask" in d["reason"].lower()


def test_drift_guard_does_not_refire_when_stop_hook_active(tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_STOP_DRIFT_GUARD", "1")
    ev = _mk(
        tmp_path,
        "implement",
        last_msg="Shall I proceed?",
        stop_hook_active=True,
    )
    d = decide_stop(tmp_path, ev, env=os.environ)
    assert d is not None
    assert "already" in d["reason"].lower()


def test_drift_guard_blocks_without_autopilot(tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_STOP_DRIFT_GUARD", "1")
    ev = {
        "reason": "end_turn",
        "stopHookActive": False,
        "lastAssistantMessage": "Should I run the tests now?",
    }
    d = decide_stop(tmp_path, ev, env=os.environ)
    assert d is not None
    assert d["decision"] == "block"
    assert "do not ask" in d["reason"].lower()
    assert "keep working" in d["reason"].lower()
