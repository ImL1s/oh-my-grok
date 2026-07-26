"""U-11 strict Autopilot v2 transitions."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omg_cli.acceptance import clear_cli_acceptance_tokens
from omg_cli.autopilot import (
    AutopilotError,
    LEGAL_TRANSITIONS,
    assert_legal_transition,
    autopilot_context_pack,
    build_phase_prompt,
    complete_with_acceptance,
    run_autopilot,
    set_awaiting_confirmation,
    start_autopilot,
    status_autopilot,
    transition,
)
from omg_cli.main import main
from omg_cli.state import create_run, load_active_run, load_run, merge_status_fields
from omg_cli.stop_gate import decide_stop
from omg_cli.qa import freeze_scenarios, run_qa_cycle
from omg_cli.review import run_structured_review

ROOT = Path(__file__).resolve().parents[1]


def _goal_bound_prd(tmp_path: Path, goal: str) -> dict:
    test_file = tmp_path / "tests" / "test_ok.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return {
        "version": 1,
        "goal": goal,
        "stories": [
            {
                "id": "s1",
                "title": "ok",
                "commands": [
                    [sys.executable, "-m", "pytest", str(test_file), "-q"]
                ],
            }
        ],
        "global_commands": [],
    }


def _stamp_review_clean(root: Path, run_id: str, diff: str = "diff body") -> None:
    run_structured_review(
        root,
        run_id,
        diff_text=diff,
        code_reviewer_payload={"verdict": "APPROVE", "findings": []},
        architect_payload={"verdict": "CLEAR", "findings": []},
    )


def _stamp_qa_clean(root: Path, run_id: str, *, tmp_path: Path | None = None) -> None:
    if tmp_path is not None:
        test_file = tmp_path / "tests" / "test_ok.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        freeze_scenarios(
            root,
            run_id,
            [
                {
                    "id": "s1",
                    "check": "command",
                    "command": [
                        sys.executable,
                        "-m",
                        "pytest",
                        str(test_file),
                        "-q",
                    ],
                }
            ],
        )
    else:
        freeze_scenarios(
            root,
            run_id,
            [{"id": "s1", "check": "always_pass"}],
            allow_always_pass=True,
        )
    out = run_qa_cycle(root, run_id)
    assert out["clean"] is True


def _walk_to_acceptance(root: Path, rid: str, *, tmp_path: Path | None = None) -> None:
    transition(root, rid, "implement", evidence={"consensus": True})
    transition(root, rid, "review")
    _stamp_review_clean(root, rid)
    transition(root, rid, "qa")
    _stamp_qa_clean(root, rid, tmp_path=tmp_path)
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
    _walk_to_acceptance(tmp_path, rid, tmp_path=tmp_path)
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
    _walk_to_acceptance(tmp_path, rid, tmp_path=tmp_path)

    prd = _goal_bound_prd(tmp_path, "happy accept")
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
    _walk_to_acceptance(tmp_path, rid, tmp_path=tmp_path)
    prd = _goal_bound_prd(tmp_path, "short circuit")
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


def test_autopilot_complete_rejects_analyze_only_acceptance(tmp_path: Path) -> None:
    clear_cli_acceptance_tokens()
    st = start_autopilot(tmp_path, "analyze only", skip_interview=True)
    rid = st["run_id"]
    _walk_to_acceptance(tmp_path, rid)
    prd = {
        "version": 1,
        "goal": "analyze only",
        "stories": [
            {
                "id": "s1",
                "title": "lint",
                "commands": [["flutter", "analyze", "lib"]],
            }
        ],
        "global_commands": [],
    }
    with pytest.raises(AutopilotError, match="analyze-only|goal-bound"):
        complete_with_acceptance(tmp_path, rid, prd=prd)


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


def test_set_awaiting_mirrors_flag_into_status(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    set_awaiting_confirmation(
        tmp_path, st["run_id"], True, reason="interview:waiting_input"
    )
    run = load_run(tmp_path, st["run_id"])
    assert run is not None
    assert run["autopilot_awaiting"] is True
    assert run["autopilot_awaiting_reason"] == "interview:waiting_input"


def test_clear_awaiting(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    rid = st["run_id"]
    set_awaiting_confirmation(tmp_path, rid, True, reason="interview:waiting_input")
    set_awaiting_confirmation(tmp_path, rid, False)
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("autopilot_awaiting") is False
    assert run.get("autopilot_awaiting_reason") == ""


def test_set_awaiting_never_touches_verified(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    rid = st["run_id"]
    set_awaiting_confirmation(tmp_path, rid, True, reason="permission:destructive")
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("verified") is not True
    assert run.get("status") not in ("verified", "cancelled", "completed")
    assert run.get("autopilot_phase") == "interview"


def test_cli_autopilot_await_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    rid = st["run_id"]
    monkeypatch.chdir(tmp_path)
    rc = main(["autopilot", "await", "--run", rid, "--reason", "cli:pause"])
    assert rc == 0
    run = load_run(tmp_path, rid)
    assert run is not None
    assert run.get("autopilot_awaiting") is True
    assert run.get("autopilot_awaiting_reason") == "cli:pause"
    rc_clear = main(
        ["autopilot", "await", "--run", rid, "--clear", "--reason", "should-ignore"]
    )
    assert rc_clear == 0
    run2 = load_run(tmp_path, rid)
    assert run2 is not None
    assert run2.get("autopilot_awaiting") is False
    assert run2.get("autopilot_awaiting_reason") == ""


def test_set_awaiting_allows_stop_gate(tmp_path: Path) -> None:
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    rid = st["run_id"]
    event = {"reason": "end_turn", "stopHookActive": False, "backgroundTasks": []}
    assert decide_stop(tmp_path, event) is not None
    set_awaiting_confirmation(tmp_path, rid, True, reason="interview:waiting_input")
    assert decide_stop(tmp_path, event) is None


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


def _rid(root: Path) -> str:
    run = load_active_run(root)
    assert run is not None
    return str(run["run_id"])


def _stamp_gate_for(root: Path, kw: dict) -> int:
    """Simulate grok completing the current phase gate (test helper)."""
    run_dir = kw["run_dir"]
    run_id = run_dir.name
    phase = status_autopilot(root, run_id)["phase"]
    if phase == "ralplan":
        merge_status_fields(root, run_id, {"ralplan_consensus": True})
    elif phase == "review":
        _stamp_review_clean(root, run_id)
    elif phase == "qa":
        _stamp_qa_clean(root, run_id, tmp_path=root)
    return 0


def test_autopilot_context_pack_names_phase_and_gate() -> None:
    pack = autopilot_context_pack(
        run_id="r1",
        phase="review",
        goal="g",
        next_gate="CLI stages/structured_review.json clean",
    )
    assert "phase=review" in pack and "structured_review.json" in pack


def test_build_phase_prompt_maps_skill_and_forbids_questions(tmp_path: Path) -> None:
    text = build_phase_prompt("implement", root=tmp_path, goal="g", run_id="r1")
    assert "ultrawork" in text.lower() or "implement" in text.lower()
    assert "do not ask" in text.lower()


def test_build_phase_prompt_ralplan_binds_to_autopilot_run(tmp_path: Path) -> None:
    text = build_phase_prompt("ralplan", root=tmp_path, goal="g", run_id="ap-run-9")
    assert "Autopilot-bound ralplan" in text
    assert "--run ap-run-9" in text
    assert "Do **not** edit `.omg/state/`" in text
    assert "accepted: true" in text  # forbidden forge called out
    assert "ralplan-consensus-ap-run-9.json" not in text
    assert "Do **not** start a standalone" in text or "do **not** start a standalone" in text.lower()


def test_consensus_ready_ignores_artifact_marker(tmp_path: Path) -> None:
    from omg_cli.autopilot import _consensus_ready

    st = start_autopilot(tmp_path, "artifact alone", skip_interview=True)
    rid = st["run_id"]
    marker = tmp_path / ".omg" / "artifacts" / f"ralplan-consensus-{rid}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    assert _consensus_ready(tmp_path, rid) is False
    merge_status_fields(tmp_path, rid, {"ralplan_consensus": True})
    assert _consensus_ready(tmp_path, rid) is True


def test_try_advance_after_launch_skips_when_implement_became_blocked(
    tmp_path: Path,
) -> None:
    """Stale phase=implement must not force review after launch left blocked."""
    from omg_cli.autopilot import _try_advance_after_launch

    st = start_autopilot(tmp_path, "block mid implement", skip_interview=True)
    rid = st["run_id"]
    merge_status_fields(tmp_path, rid, {"ralplan_consensus": True})
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "blocked", reason="ops")
    assert status_autopilot(tmp_path, rid)["phase"] == "blocked"
    out = _try_advance_after_launch(tmp_path, rid, "implement")
    assert out == "blocked"
    assert status_autopilot(tmp_path, rid)["phase"] == "blocked"


def test_run_autopilot_walks_to_verified_with_mocked_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_cli_acceptance_tokens()
    launches: list[dict] = []

    def _fake_launch(argv, **kw):
        launches.append({**kw, "argv": argv})
        _stamp_gate_for(tmp_path, kw)
        return 0

    monkeypatch.setattr("omg_cli.modes._launch_grok", _fake_launch)
    rc = run_autopilot(
        tmp_path, "add pure add(a,b) with test", skip_interview=True
    )
    assert rc == 0
    assert status_autopilot(tmp_path, _rid(tmp_path))["phase"] == "verified"
    assert launches


def test_run_autopilot_pauses_at_interview(tmp_path: Path) -> None:
    rc = run_autopilot(tmp_path, "vague idea")
    assert rc == 0
    assert status_autopilot(tmp_path, _rid(tmp_path))["phase"] == "interview"


def test_run_autopilot_pauses_when_awaiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    st = start_autopilot(tmp_path, "ship it", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "review")
    set_awaiting_confirmation(tmp_path, rid, True, reason="cli:pause")
    launched: list[bool] = []

    def _fake_launch(argv, **kw):
        launched.append(True)
        return 0

    monkeypatch.setattr("omg_cli.modes._launch_grok", _fake_launch)
    rc = run_autopilot(tmp_path, "", resume_run_id=rid)
    assert rc == 0
    assert not launched
    out = capsys.readouterr().out
    assert f"omg autopilot await --clear --run {rid}" in out
    assert f"omg autopilot run --resume {rid}" in out
    assert out.index(f"omg autopilot await --clear --run {rid}") < out.index(
        f"omg autopilot run --resume {rid}"
    )
    assert (tmp_path / ".omg" / "state" / "RESUME.md").is_file()


def test_run_resume_reenters_current_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = start_autopilot(tmp_path, "resume goal", skip_interview=True)
    rid = st["run_id"]
    transition(tmp_path, rid, "implement", evidence={"consensus": True})
    transition(tmp_path, rid, "review")
    phases_seen: list[str] = []

    def _fake_launch(argv, **kw):
        run_id = kw["run_dir"].name
        phases_seen.append(status_autopilot(tmp_path, run_id)["phase"])
        return 0

    monkeypatch.setattr("omg_cli.modes._launch_grok", _fake_launch)
    rc = run_autopilot(tmp_path, "", resume_run_id=rid)
    assert rc == 0
    assert phases_seen == ["review"]
    assert status_autopilot(tmp_path, rid)["phase"] == "review"

    phases_seen.clear()
    rc2 = run_autopilot(tmp_path, "", resume_run_id=rid)
    assert rc2 == 0
    assert phases_seen == ["review"]
    assert status_autopilot(tmp_path, rid)["phase"] == "review"


def test_cli_autopilot_run_listed_in_skills_md() -> None:
    assert "omg autopilot run" in (ROOT / "docs" / "skills.md").read_text()
