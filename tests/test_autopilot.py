"""U-11 strict Autopilot v2 transitions."""
from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.acceptance import clear_cli_acceptance_tokens
from omg_cli.autopilot import (
    AutopilotError,
    LEGAL_TRANSITIONS,
    assert_legal_transition,
    complete_with_acceptance,
    start_autopilot,
    status_autopilot,
    transition,
)
from omg_cli.qa import freeze_scenarios, run_qa_cycle
from omg_cli.review import run_structured_review
from omg_cli.state import create_run, load_run


def _stamp_review_clean(root: Path, run_id: str, diff: str = "diff body") -> None:
    run_structured_review(
        root,
        run_id,
        diff_text=diff,
        code_reviewer_payload={"verdict": "APPROVE", "findings": []},
        architect_payload={"verdict": "CLEAR", "findings": []},
    )


def _stamp_qa_clean(root: Path, run_id: str) -> None:
    freeze_scenarios(
        root,
        run_id,
        [{"id": "s1", "check": "always_pass"}],
        allow_always_pass=True,
    )
    out = run_qa_cycle(root, run_id)
    assert out["clean"] is True


def _walk_to_acceptance(root: Path, rid: str) -> None:
    transition(root, rid, "implement", evidence={"consensus": True})
    transition(root, rid, "review")
    _stamp_review_clean(root, rid)
    transition(root, rid, "qa")
    _stamp_qa_clean(root, rid)
    transition(root, rid, "acceptance")


def test_legal_transition_table() -> None:
    assert_legal_transition("interview", "ralplan")
    with pytest.raises(AutopilotError):
        assert_legal_transition("interview", "qa")
    with pytest.raises(AutopilotError):
        assert_legal_transition("init", "verified")
    assert "acceptance" in LEGAL_TRANSITIONS["qa"]


def test_start_and_gated_transitions(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "ship parity core")
    rid = st["run_id"]
    assert st["phase"] == "interview"
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("schema_version") == 2

    with pytest.raises(AutopilotError, match="interview"):
        transition(tmp_path, rid, "ralplan")

    transition(
        tmp_path,
        rid,
        "ralplan",
        evidence={"interview_complete": True},
    )
    with pytest.raises(AutopilotError, match="consensus"):
        transition(tmp_path, rid, "implement")

    transition(
        tmp_path,
        rid,
        "implement",
        evidence={"consensus": True},
    )
    transition(tmp_path, rid, "review")

    # evidence_json alone cannot open QA — needs staged structured_review
    with pytest.raises(AutopilotError, match="structured_review"):
        transition(
            tmp_path,
            rid,
            "qa",
            evidence={"review_clean": True},
        )

    _stamp_review_clean(tmp_path, rid)
    transition(tmp_path, rid, "qa")

    with pytest.raises(AutopilotError, match="ultraqa"):
        transition(
            tmp_path,
            rid,
            "acceptance",
            evidence={"qa_clean": True},
        )

    _stamp_qa_clean(tmp_path, rid)
    transition(tmp_path, rid, "acceptance")
    st2 = status_autopilot(tmp_path, rid)
    assert st2["phase"] == "acceptance"
    assert st2["verified"] is False


def test_complete_without_prd_refuses(tmp_path: Path) -> None:
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "verify path", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid)
    with pytest.raises(AutopilotError, match="prd"):
        complete_with_acceptance(tmp_path, rid)


def test_complete_happy_path_same_process_acceptance(tmp_path: Path) -> None:
    """Happy path: freeze_and_run in-process then set_verified → verified."""
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "happy accept", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid)

    prd = {
        "version": 1,
        "goal": "happy accept",
        "stories": [
            {"id": "s1", "title": "ok", "commands": [["true"]]}
        ],
        "global_commands": [],
    }
    out = complete_with_acceptance(tmp_path, rid, prd=prd)
    assert out["phase"] == "verified"
    assert out["verified"] is True
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is True
    assert run.get("status") == "verified"


def test_rework_invalidates_review_stamp(tmp_path: Path) -> None:
    """After rework, a previous clean structured_review must not open QA."""
    from omg_cli.autopilot import stage_review_is_clean

    st = start_autopilot(tmp_path, "rework stamp", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "review")
    _stamp_review_clean(tmp_path, rid)
    assert stage_review_is_clean(tmp_path, rid) is True
    transition(tmp_path, rid, "rework", reason="findings")
    assert stage_review_is_clean(tmp_path, rid) is False
    transition(tmp_path, rid, "review")
    with pytest.raises(AutopilotError, match="structured_review"):
        transition(tmp_path, rid, "qa")
    # Fresh stamp required
    _stamp_review_clean(tmp_path, rid, diff="new-diff-after-rework")
    transition(tmp_path, rid, "qa")


def test_legacy_v1_refused(tmp_path: Path) -> None:
    run = create_run(tmp_path, mode="autopilot", goal="legacy")
    with pytest.raises(AutopilotError):
        transition(tmp_path, run["run_id"], "interview")
