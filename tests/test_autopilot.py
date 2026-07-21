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


def test_complete_without_prd_materializes_from_ultraqa(tmp_path: Path) -> None:
    """Clean ultraqa always_pass scenarios materialize to prd (true) then verify."""
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "verify path", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid)
    out = complete_with_acceptance(tmp_path, rid)
    assert out["phase"] == "verified"
    assert out["verified"] is True
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is True
    assert run.get("autopilot_phase") == "verified"
    assert (tmp_path / ".omg" / "state" / "runs" / rid / "prd.json").is_file()


def test_complete_without_prd_or_ultraqa_refuses(tmp_path: Path) -> None:
    """No prd and no materializable ultraqa → AutopilotError."""
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "no prd no qa", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "review")
    _stamp_review_clean(tmp_path, rid)
    transition(tmp_path, rid, "qa")
    # Frozen but never run → not clean; transition to acceptance requires clean
    # so stamp clean then wipe ultraqa file after entering acceptance.
    _stamp_qa_clean(tmp_path, rid)
    transition(tmp_path, rid, "acceptance")
    qa_path = (
        tmp_path / ".omg" / "state" / "runs" / rid / "stages" / "ultraqa.json"
    )
    qa_path.unlink()
    with pytest.raises(AutopilotError, match="prd|ultraqa"):
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
    assert run.get("autopilot_phase") == "verified"


def test_complete_short_circuit_when_already_verified(tmp_path: Path) -> None:
    """If omg accept already verified, complete syncs phase without re-accept."""
    clear_cli_acceptance_tokens()
    from omg_cli.acceptance import freeze_and_run
    from omg_cli.state import set_verified

    st = start_autopilot(tmp_path, "short circuit", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid)
    prd = {
        "version": 1,
        "goal": "short circuit",
        "stories": [{"id": "s1", "title": "ok", "commands": [["true"]]}],
        "global_commands": [],
    }
    assert freeze_and_run(tmp_path, rid, prd) is True
    set_verified(tmp_path, rid, force=False)
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is True
    # Autopilot still on acceptance until complete
    assert status_autopilot(tmp_path, rid)["phase"] == "acceptance"

    out = complete_with_acceptance(tmp_path, rid, prd=prd)
    assert out["phase"] == "verified"
    assert out["verified"] is True
    run2 = load_run(tmp_path, rid)
    assert run2 is not None
    assert run2.get("autopilot_phase") == "verified"
    # Second complete is idempotent
    out2 = complete_with_acceptance(tmp_path, rid)
    assert out2["phase"] == "verified"


def test_blocked_to_qa_still_requires_review(tmp_path: Path) -> None:
    """Destination gates apply even when recovering from blocked."""
    st = start_autopilot(tmp_path, "blocked qa", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "review")
    transition(tmp_path, rid, "blocked", reason="ops")
    with pytest.raises(AutopilotError, match="structured_review"):
        transition(tmp_path, rid, "qa")


def test_blocked_to_implement_requires_consensus(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "blocked impl", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "blocked", reason="wait")
    with pytest.raises(AutopilotError, match="consensus"):
        transition(tmp_path, rid, "implement")


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


def test_blocked_implement_roundtrip_invalidates_stale_stamps(tmp_path: Path) -> None:
    """qa→blocked→implement→blocked→qa must NOT reuse the stale clean review
    stamp — re-entering implement produces new, unreviewed code."""
    st = start_autopilot(tmp_path, "roundtrip", skip_interview=True)
    rid = st["run_id"]
    # Reach a clean qa the legitimate way.
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "review")
    _stamp_review_clean(tmp_path, rid)
    transition(tmp_path, rid, "qa")
    _stamp_qa_clean(tmp_path, rid)
    # Detour that used to smuggle new code past review/QA:
    transition(tmp_path, rid, "blocked", reason="infra hiccup")
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "blocked", reason="another hiccup")
    # The qa gate must now reject: the review stamp was invalidated on implement.
    with pytest.raises(AutopilotError, match="review"):
        transition(tmp_path, rid, "qa")


def test_qa_blocked_review_roundtrip_invalidates_review_stamp(tmp_path: Path) -> None:
    """qa→blocked→review must invalidate the prior clean review stamp so a
    later qa entry cannot reuse it without a fresh structured_review."""
    from omg_cli.autopilot import stage_review_is_clean

    st = start_autopilot(tmp_path, "qa-blocked-review", skip_interview=True)
    rid = st["run_id"]
    # Reach a clean qa the legitimate way.
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "review")
    _stamp_review_clean(tmp_path, rid)
    assert stage_review_is_clean(tmp_path, rid) is True
    transition(tmp_path, rid, "qa")
    # Detour that re-enters review without new product code, but still must
    # not reopen qa on a pre-block stamp.
    transition(tmp_path, rid, "blocked", reason="ops hiccup")
    transition(tmp_path, rid, "review")
    assert stage_review_is_clean(tmp_path, rid) is False
    with pytest.raises(AutopilotError, match="review"):
        transition(tmp_path, rid, "qa")
    # Fresh stamp required after invalidation.
    _stamp_review_clean(tmp_path, rid, diff="new-diff-after-blocked-review")
    transition(tmp_path, rid, "qa")
