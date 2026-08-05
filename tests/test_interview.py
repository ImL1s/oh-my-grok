import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.evidence import sha256_bytes
from omg_cli.interview import (
    BROWNFIELD_WEIGHTS,
    InterviewError,
    InterviewIncomplete,
    ambiguity_score,
    answer_interview,
    close_interview,
    interview_spec_path,
    interview_state_path,
    interview_status,
    interview_transcript_path,
    pressure_pass_interview,
    start_interview,
)
from omg_cli.state import create_run


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"


def _state(root: Path, run_id: str) -> dict:
    return json.loads(interview_state_path(root, run_id).read_text(encoding="utf-8"))


def _clear_task() -> str:
    return """Intent: Replace fragile manual release work with a deterministic audited workflow for maintainers.
Outcome: Users run one command and receive explicit verified blocked or cancelled terminal evidence.
Scope: Implement only local command lifecycle state and artifacts required for a safe handoff.
Constraints: Preserve backward compatibility use the standard library and never weaken CLI authority.
Success: Unit integration and adversarial tests demonstrate deterministic resume and failure closure.
Context: Existing repository has Python CLI state tests documentation and atomic evidence helpers.
Non-goals: Do not build a chat interface remote service model router or publishing automation.
Decision boundaries: The agent may choose file layout test cases and naming without further approval.
Acceptance: Targeted and full tests pass while corrupt stale and wrong-run inputs fail closed."""


def _canonical_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _run_omg(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, str(BIN_OMG), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def test_quick_and_standard_lock_expected_topology(tmp_path: Path) -> None:
    green = tmp_path / "green"
    green.mkdir()
    quick = start_interview(green, "Build a small tool", profile="quick")
    quick_state = _state(green, quick["run_id"])
    assert quick_state["threshold"] == 0.30
    assert quick_state["max_rounds"] == 5
    assert quick_state["topology"]["locked"] is True
    assert quick_state["topology"]["active_dimensions"] == [
        "intent",
        "outcome",
        "scope",
        "constraints",
        "success",
    ]
    assert quick_state["topology"]["deferred_dimensions"] == ["context"]

    brown = tmp_path / "brown"
    brown.mkdir()
    (brown / ".git").mkdir()
    standard = start_interview(brown, "Improve this repository", profile="standard")
    standard_state = _state(brown, standard["run_id"])
    assert standard_state["threshold"] == 0.20
    assert standard_state["max_rounds"] == 12
    assert standard_state["topology"]["active_dimensions"] == list(
        BROWNFIELD_WEIGHTS
    )
    assert standard_state["topology"]["repo_evidence"] == [".git"]


def test_brownfield_ambiguity_uses_all_six_dimensions() -> None:
    scores = {name: 1.0 for name in BROWNFIELD_WEIGHTS}
    assert ambiguity_score(scores, context_type="brownfield") == 0.0
    scores["context"] = 0.0
    assert ambiguity_score(scores, context_type="brownfield") == 0.1
    with pytest.raises(InterviewError, match="topology mismatch"):
        ambiguity_score({"intent": 1.0}, context_type="brownfield")


def test_only_one_question_is_pending_and_resume_is_exact(tmp_path: Path) -> None:
    result = start_interview(tmp_path, "Fix the app", context_type="greenfield")
    question = result["pending_question"]
    assert isinstance(question, dict)
    assert question["text"].count("?") == 1
    assert question["dimension"] == "intent"
    assert result["status"] == "waiting_input"
    assert result["resume_command"] == (
        f"omg interview answer --run {result['run_id']} "
        f"--question-id {question['question_id']} --text TEXT"
    )


def test_answer_scores_are_monotonic_and_transcript_resumes(tmp_path: Path) -> None:
    started = start_interview(tmp_path, "Fix the app", context_type="greenfield")
    before = dict(started["scores"])
    question = started["pending_question"]
    answered = answer_interview(
        tmp_path,
        started["run_id"],
        "The current manual flow repeatedly loses user work and must become reliable.",
        question_id=question["question_id"],
    )
    assert all(answered["scores"][key] >= before[key] for key in before)
    assert answered["scores"][question["dimension"]] > before[question["dimension"]]
    resumed = interview_status(tmp_path, started["run_id"])
    assert resumed["rounds_completed"] == 1
    persisted = _state(tmp_path, started["run_id"])
    assert persisted["rounds"][0]["run_id"] == started["run_id"]
    assert persisted["rounds"][0]["session_id"] == started["session_id"]
    assert persisted["rounds"][0]["invocation_id"]


def test_clear_task_can_close_with_zero_questions_after_pressure_pass(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    started = start_interview(tmp_path, _clear_task(), profile="standard")
    assert started["rounds_completed"] == 0
    assert started["pending_question"] is None
    assert started["status"] == "ready_for_pressure_pass"
    pressured = pressure_pass_interview(
        tmp_path,
        started["run_id"],
        "The assumption is that a deterministic CLI is sufficient; reject an automatic LLM engine to preserve auditable authority.",
    )
    assert pressured["status"] == "ready_to_close"
    closed = close_interview(tmp_path, started["run_id"])
    assert closed["status"] == "complete"
    assert closed["rounds_completed"] == 0


def test_close_before_ready_stays_waiting_and_cannot_handoff(tmp_path: Path) -> None:
    started = start_interview(tmp_path, "Fix it", context_type="greenfield")
    with pytest.raises(InterviewIncomplete) as caught:
        close_interview(tmp_path, started["run_id"])
    result = caught.value.result
    assert result["status"] == "waiting_input"
    assert result["pending_question"] is not None
    assert result["resume_command"].startswith("omg interview answer --run ")
    assert result["spec_path"] is None
    assert not interview_spec_path(tmp_path, started["run_id"]).exists()


def test_wrong_run_and_corrupt_state_fail_closed(tmp_path: Path) -> None:
    wrong = create_run(
        tmp_path,
        mode="ralph",
        goal="not an interview",
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    with pytest.raises(InterviewError, match="wrong run mode"):
        interview_status(tmp_path, wrong["run_id"])

    other = tmp_path / "other"
    other.mkdir()
    started = start_interview(other, "Clarify this", context_type="greenfield")
    path = interview_state_path(other, started["run_id"])
    path.write_text("{not-json", encoding="utf-8")
    raw = path.read_bytes()
    with pytest.raises(InterviewError, match="corrupt interview state"):
        interview_status(other, started["run_id"])
    assert path.read_bytes() == raw


def test_stale_question_id_is_rejected_without_transcript_mutation(tmp_path: Path) -> None:
    started = start_interview(tmp_path, "Clarify this", context_type="greenfield")
    old_id = started["pending_question"]["question_id"]
    answer_interview(
        tmp_path,
        started["run_id"],
        "This prevents repeated loss of data in the current workflow.",
        question_id=old_id,
    )
    path = interview_state_path(tmp_path, started["run_id"])
    before = path.read_bytes()
    with pytest.raises(InterviewError, match="stale question_id"):
        answer_interview(
            tmp_path,
            started["run_id"],
            "This is an answer to an obsolete prompt.",
            question_id=old_id,
        )
    assert path.read_bytes() == before


def test_authoritative_spec_and_transcript_are_identity_and_hash_bound(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    started = start_interview(tmp_path, _clear_task())
    pressure_pass_interview(
        tmp_path,
        started["run_id"],
        "Pressure test confirms compatibility and CLI authority outweigh automatic conversational convenience.",
    )
    closed = close_interview(tmp_path, started["run_id"])
    state = _state(tmp_path, started["run_id"])
    assert interview_transcript_path(tmp_path, started["run_id"]) == interview_state_path(
        tmp_path, started["run_id"]
    )
    artifact = json.loads(
        interview_spec_path(tmp_path, started["run_id"]).read_text(encoding="utf-8")
    )
    assert artifact["stamp"]["writer"] == "omg-cli"
    assert artifact["content"]["run_id"] == started["run_id"]
    assert artifact["content"]["session_id"] == started["session_id"]
    assert artifact["stamp"]["invocation_id"] == state["closed_by_invocation_id"]
    assert artifact["stamp"]["content_sha256"] == sha256_bytes(
        _canonical_bytes(artifact["content"])
    )
    assert artifact["content"]["transcript"] == state["rounds"]
    spec = artifact["content"]
    for key in (
        "intent",
        "desired_outcome",
        "in_scope",
        "constraints",
        "success_criteria",
        "context",
        "non_goals",
        "decision_boundaries",
        "acceptance",
        "ambiguity",
        "execution_contract",
    ):
        assert spec[key]
    assert closed["spec_path"] == state["spec_path"]


def test_cli_routes_start_status_pressure_and_close(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    started = _run_omg(
        "interview",
        "start",
        _clear_task(),
        "--profile",
        "standard",
        cwd=tmp_path,
    )
    assert started.returncode == 0, started.stderr + started.stdout
    start_data = json.loads(started.stdout)
    run_id = start_data["run_id"]
    assert start_data["status"] == "ready_for_pressure_pass"

    status = _run_omg("interview", "status", "--run", run_id, cwd=tmp_path)
    assert status.returncode == 0, status.stderr + status.stdout
    assert json.loads(status.stdout)["run_id"] == run_id

    pressured = _run_omg(
        "interview",
        "pressure-pass",
        "--run",
        run_id,
        "--text",
        "The explicit trade-off keeps the primitive deterministic and rejects hidden model authority.",
        cwd=tmp_path,
    )
    assert pressured.returncode == 0, pressured.stderr + pressured.stdout
    assert json.loads(pressured.stdout)["status"] == "ready_to_close"

    closed = _run_omg("interview", "close", "--run", run_id, cwd=tmp_path)
    assert closed.returncode == 0, closed.stderr + closed.stdout
    assert json.loads(closed.stdout)["status"] == "complete"


def test_interview_attach_run_writes_envelope_for_autopilot(tmp_path: Path) -> None:
    """R4-2: ``start_interview(..., attach_run_id=...)`` seeds interview.json
    under an existing autopilot run_id (phase=="interview") instead of
    creating a separate mode=="interview" run, and the real close path
    produces a genuine envelope that ``_interview_complete`` (and thus
    ``transition(..., "ralplan")``) accepts."""
    from omg_cli.autopilot import _interview_complete, start_autopilot, status_autopilot, transition
    from omg_cli.state import load_active_run, load_run

    (tmp_path / ".git").mkdir()
    st = start_autopilot(tmp_path, _clear_task(), skip_interview=False)
    rid = st["run_id"]
    assert st["phase"] == "interview"

    started = start_interview(tmp_path, "", attach_run_id=rid)
    assert started["run_id"] == rid
    assert started["status"] == "ready_for_pressure_pass"

    run = load_run(tmp_path, rid)
    assert run["mode"] == "autopilot"
    persisted = _state(tmp_path, rid)
    assert persisted["task"] == _clear_task()

    pressured = pressure_pass_interview(
        tmp_path,
        rid,
        "Pressure test confirms compatibility and CLI authority outweigh automatic conversational convenience.",
    )
    assert pressured["status"] == "ready_to_close"

    closed = close_interview(tmp_path, rid)
    assert closed["status"] == "complete"
    assert _interview_complete(tmp_path, rid) is True

    # Attaching to an autopilot run must not clear its active pointer or
    # touch its own phase file — only ``transition()`` may advance phase.
    active = load_active_run(tmp_path)
    assert active is not None and active["run_id"] == rid
    assert status_autopilot(tmp_path, rid)["phase"] == "interview"

    out = transition(tmp_path, rid, "ralplan")
    assert out["phase"] == "ralplan"


def test_attach_close_resume_command_targets_same_autopilot_run(
    tmp_path: Path,
) -> None:
    """R6-2/R7-0/R9-3: closing an attached (mode=="autopilot") interview
    must advertise a resume_command that keeps the same run_id — bare
    `omg ralplan <goal>` would start a brand-new run and orphan the
    autopilot run's evidence. It must be the single idempotent
    `omg autopilot run --resume <rid>` entry, which itself advances the
    phase sidecar from "interview" to "ralplan" and launches — not the
    older two-step `transition && ralplan`."""
    from omg_cli.autopilot import start_autopilot

    (tmp_path / ".git").mkdir()
    st = start_autopilot(tmp_path, _clear_task(), skip_interview=False)
    rid = st["run_id"]

    start_interview(tmp_path, "", attach_run_id=rid)
    pressure_pass_interview(
        tmp_path,
        rid,
        "Pressure test confirms compatibility and CLI authority outweighs automatic conversational convenience.",
    )
    closed = close_interview(tmp_path, rid)
    assert closed["status"] == "complete"
    assert closed["resume_command"] == f"omg autopilot run --resume {rid}"


def test_close_interview_migrates_stale_autopilot_resume_command_on_reclose(
    tmp_path: Path,
) -> None:
    """R9-3: a pre-R7 autopilot-attached ``interview.json`` may still carry
    the old two-step ``transition && ralplan`` (or even older bare
    ``ralplan``) resume_command on disk. Re-closing an already-complete run
    (the early-return path) must migrate the stale hint to the current
    single idempotent ``omg autopilot run --resume <rid>`` form and persist
    that migration on disk, not just paper over it in the returned dict."""
    from omg_cli.autopilot import start_autopilot

    (tmp_path / ".git").mkdir()
    st = start_autopilot(tmp_path, _clear_task(), skip_interview=False)
    rid = st["run_id"]

    start_interview(tmp_path, "", attach_run_id=rid)
    pressure_pass_interview(
        tmp_path,
        rid,
        "Pressure test confirms compatibility and CLI authority outweighs automatic conversational convenience.",
    )
    closed = close_interview(tmp_path, rid)
    assert closed["status"] == "complete"

    stale_state = _state(tmp_path, rid)
    stale_state["resume_command"] = f"omg ralplan {shlex.quote(_clear_task())}"
    interview_state_path(tmp_path, rid).write_text(
        json.dumps(stale_state), encoding="utf-8"
    )

    expected = f"omg autopilot run --resume {rid}"
    reclosed = close_interview(tmp_path, rid)
    assert reclosed["resume_command"] == expected
    assert _state(tmp_path, rid)["resume_command"] == expected


def test_bare_interview_close_resume_command_has_no_run_flag(
    tmp_path: Path,
) -> None:
    """Standalone (mode=="interview") runs are not autopilot-attached, so
    their resume_command stays a bare `omg ralplan <goal>` with no --run."""
    started = start_interview(tmp_path, _clear_task(), profile="standard")
    (tmp_path / ".git").mkdir()
    pressure_pass_interview(
        tmp_path,
        started["run_id"],
        "The assumption is that a deterministic CLI is sufficient; reject an automatic LLM engine to preserve auditable authority.",
    )
    closed = close_interview(tmp_path, started["run_id"])
    assert closed["status"] == "complete"
    assert closed["resume_command"] == f"omg ralplan {shlex.quote(_clear_task())}"
    assert "--run" not in closed["resume_command"]


def test_interview_attach_rejects_wrong_phase_and_mode(tmp_path: Path) -> None:
    """Fail-closed: attaching requires an autopilot run currently parked at
    phase=="interview" — a later phase or a non-autopilot mode is refused."""
    from omg_cli.autopilot import start_autopilot

    ralplan_phase = start_autopilot(tmp_path, "skip to ralplan", skip_interview=True)
    with pytest.raises(InterviewError, match="interview phase"):
        start_interview(tmp_path, "some task", attach_run_id=ralplan_phase["run_id"])

    wrong_mode = create_run(
        tmp_path,
        mode="ralph",
        goal="not autopilot",
        extra={"schema_version": 2, "lifecycle_version": 2},
        force=True,
    )
    with pytest.raises(InterviewError, match="requires an autopilot run"):
        start_interview(tmp_path, "some task", attach_run_id=wrong_mode["run_id"])

    # Answering/closing on a non-interview, non-attachable run is likewise refused.
    with pytest.raises(InterviewError, match="wrong run mode"):
        interview_status(tmp_path, wrong_mode["run_id"])


def test_interview_attach_rejects_mismatched_task(tmp_path: Path) -> None:
    """CRITICAL: a non-empty --attach-run task that disagrees with the
    autopilot run's goal must fail fast, before any interview.json is
    written — attach always uses the run's own goal, never a custom task."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "the real autopilot goal", skip_interview=False)
    rid = st["run_id"]

    with pytest.raises(InterviewError, match="must match the run's goal"):
        start_interview(tmp_path, "a different task text", attach_run_id=rid)

    assert not interview_state_path(tmp_path, rid).exists()

    # The matching-task and omitted-task forms both succeed and seed the
    # real run goal.
    started = start_interview(tmp_path, "the real autopilot goal", attach_run_id=rid)
    assert started["run_id"] == rid
    persisted = _state(tmp_path, rid)
    assert persisted["task"] == "the real autopilot goal"


def test_interview_attach_reseed_guard_still_enforced_under_lease(tmp_path: Path) -> None:
    """IMPORTANT TOCTOU fix: the reseed guard now runs inside the execution
    lease. Functionally it must still refuse a second unforced attach and
    still allow a forced reseed."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "reseed guard task", skip_interview=False)
    rid = st["run_id"]

    start_interview(tmp_path, "", attach_run_id=rid)
    with pytest.raises(InterviewError, match="already started"):
        start_interview(tmp_path, "", attach_run_id=rid)

    reseeded = start_interview(tmp_path, "", attach_run_id=rid, force=True)
    assert reseeded["run_id"] == rid


def test_interview_attach_reauthorizes_phase_under_lease_on_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5-3: a concurrent phase transition landing between the pre-lease
    ``_attach_interview_run`` check and the lease-protected write must still
    be caught — ``start_interview`` re-authorizes mode/phase fresh under the
    lease before writing anything."""
    import omg_cli.interview as interview_mod
    from omg_cli.autopilot import start_autopilot, transition

    st = start_autopilot(tmp_path, "phase drift task", skip_interview=False)
    rid = st["run_id"]

    original_attach = interview_mod._attach_interview_run

    def racy_attach(root, attach_run_id, task, *, force):
        result = original_attach(root, attach_run_id, task, force=force)
        # Simulate a concurrent transition landing after the pre-lease check
        # passed but before the lease-protected re-check below runs.
        transition(root, attach_run_id, "blocked", reason="race")
        return result

    monkeypatch.setattr(interview_mod, "_attach_interview_run", racy_attach)

    with pytest.raises(InterviewError, match="interview phase"):
        interview_mod.start_interview(tmp_path, "", attach_run_id=rid)

    assert not interview_state_path(tmp_path, rid).exists()


def test_interview_attach_reauthorize_rejects_terminal_run_under_lease(
    tmp_path: Path,
) -> None:
    """R5-3: a run cancelled between the pre-lease attach check and the
    lease-protected write must be rejected — status and autopilot phase are
    independent, so a still-``phase=="interview"`` run can be terminal."""
    from omg_cli.autopilot import start_autopilot
    from omg_cli.state import cancel_run

    st = start_autopilot(tmp_path, "terminal drift task", skip_interview=False)
    rid = st["run_id"]

    cancel_run(tmp_path, rid, kill_grace_s=0)

    with pytest.raises(InterviewError, match="terminal under lease"):
        start_interview(tmp_path, "", attach_run_id=rid)

    assert not interview_state_path(tmp_path, rid).exists()


def test_reauthorize_attach_rejects_goal_drift_under_lease(tmp_path: Path) -> None:
    """R5-3: ``_reauthorize_attach`` defends in depth against a stale task
    snapshot disagreeing with a fresh reload of the run's goal — the same
    check ``start_interview`` re-runs under the execution lease."""
    import omg_cli.interview as interview_mod
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "original goal text", skip_interview=False)
    rid = st["run_id"]

    with pytest.raises(InterviewError, match="goal drifted"):
        interview_mod._reauthorize_attach(tmp_path, rid, "a different stale task text")


def _write_pending_cancel_request(tmp_path: Path, run_id: str) -> None:
    request_path = (
        tmp_path / ".omg" / "state" / "runs" / run_id / "cancel.request.json"
    )
    request_path.write_text(
        json.dumps(
            {
                "writer": "omg-cli",
                "run_id": run_id,
                "request_id": "pending-request",
                "requested_at": "2026-08-05T00:00:00+00:00",
                "observed_generation": 0,
            }
        ),
        encoding="utf-8",
    )


def test_interview_attach_reauthorize_rejects_pending_cancellation_request_under_lease(
    tmp_path: Path,
) -> None:
    """R8-3 (P2-2): a cancellation request committed (but not yet finalized
    to a terminal ``status``) between the pre-lease attach check and the
    lease-protected write must also be rejected — ``omg cancel`` commits
    its request under the distinct transition lock, so there is a real
    window where the request exists but status has not flipped to
    cancelled yet. Fail closed in that window too, not just once terminal."""
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "cancel request drift task", skip_interview=False)
    rid = st["run_id"]

    _write_pending_cancel_request(tmp_path, rid)

    with pytest.raises(InterviewError, match="cancellation"):
        start_interview(tmp_path, "", attach_run_id=rid)

    assert not interview_state_path(tmp_path, rid).exists()


def test_reauthorize_attach_rejects_pending_cancellation_request(tmp_path: Path) -> None:
    """R8-3 (P2-2): unit-level check that ``_reauthorize_attach`` itself
    (the helper both the attach path and ``_assert_run_writable`` share)
    rejects a pending cancellation request, not just a terminal status."""
    import omg_cli.interview as interview_mod
    from omg_cli.autopilot import start_autopilot

    st = start_autopilot(tmp_path, "cancel unit task", skip_interview=False)
    rid = st["run_id"]

    _write_pending_cancel_request(tmp_path, rid)

    with pytest.raises(InterviewError, match="cancellation"):
        interview_mod._reauthorize_attach(tmp_path, rid, "cancel unit task")


def test_close_interview_rejects_terminal_run_under_lease(tmp_path: Path) -> None:
    """R8-3 (P2-2), TDD core case: a run cancelled between the outer
    readiness snapshot and the lease-protected write must be rejected —
    close_interview must not leave a complete spec/status envelope on disk
    for a run that is terminal by the time the write would happen."""
    from omg_cli.state import cancel_run

    (tmp_path / ".git").mkdir()
    started = start_interview(tmp_path, _clear_task(), profile="standard")
    rid = started["run_id"]
    pressure_pass_interview(
        tmp_path,
        rid,
        "The assumption is that a deterministic CLI is sufficient; reject an automatic LLM engine to preserve auditable authority.",
    )

    cancel_run(tmp_path, rid, kill_grace_s=0)

    with pytest.raises(InterviewError, match="terminal under lease"):
        close_interview(tmp_path, rid)

    assert not interview_spec_path(tmp_path, rid).exists()
    assert _state(tmp_path, rid)["status"] != "complete"


def test_close_interview_rejects_pending_cancellation_request_under_lease(
    tmp_path: Path,
) -> None:
    """R8-3 (P2-2), TDD core case: a committed-but-not-yet-finalized
    cancellation request must also block close — the same window
    ``_authorize_autopilot_embedding`` (R8-2) defends against. No complete
    envelope (spec artifact or status) may land on disk."""
    (tmp_path / ".git").mkdir()
    started = start_interview(tmp_path, _clear_task(), profile="standard")
    rid = started["run_id"]
    pressure_pass_interview(
        tmp_path,
        rid,
        "The assumption is that a deterministic CLI is sufficient; reject an automatic LLM engine to preserve auditable authority.",
    )

    _write_pending_cancel_request(tmp_path, rid)

    with pytest.raises(InterviewError, match="cancellation"):
        close_interview(tmp_path, rid)

    assert not interview_spec_path(tmp_path, rid).exists()
    assert _state(tmp_path, rid)["status"] != "complete"


def test_authorize_run_mode_fails_closed_on_corrupt_autopilot_state(tmp_path: Path) -> None:
    """IMPORTANT: a corrupt/unreadable autopilot state file must raise a
    clean InterviewError instead of an unhandled json.JSONDecodeError."""
    from omg_cli.autopilot import autopilot_state_path, start_autopilot

    st = start_autopilot(tmp_path, "corrupt state task", skip_interview=False)
    rid = st["run_id"]

    autopilot_state_path(tmp_path, rid).write_text("{not valid json", encoding="utf-8")

    with pytest.raises(InterviewError, match="cannot verify autopilot interview phase"):
        start_interview(tmp_path, "", attach_run_id=rid)


def test_bare_start_without_attach_still_creates_interview_mode_run(
    tmp_path: Path,
) -> None:
    started = start_interview(tmp_path, "Fix it", context_type="greenfield")
    from omg_cli.state import load_run

    run = load_run(tmp_path, started["run_id"])
    assert run["mode"] == "interview"


def test_cli_attach_run_writes_interview_envelope_for_autopilot(tmp_path: Path) -> None:
    from omg_cli.autopilot import _interview_complete, start_autopilot

    (tmp_path / ".git").mkdir()
    st = start_autopilot(tmp_path, _clear_task(), skip_interview=False)
    rid = st["run_id"]

    started = _run_omg("interview", "start", "--attach-run", rid, cwd=tmp_path)
    assert started.returncode == 0, started.stderr + started.stdout
    start_data = json.loads(started.stdout)
    assert start_data["run_id"] == rid
    assert start_data["status"] == "ready_for_pressure_pass"

    pressured = _run_omg(
        "interview",
        "pressure-pass",
        "--run",
        rid,
        "--text",
        "The explicit trade-off keeps the primitive deterministic and rejects hidden model authority.",
        cwd=tmp_path,
    )
    assert pressured.returncode == 0, pressured.stderr + pressured.stdout
    assert json.loads(pressured.stdout)["status"] == "ready_to_close"

    closed = _run_omg("interview", "close", "--run", rid, cwd=tmp_path)
    assert closed.returncode == 0, closed.stderr + closed.stdout
    assert json.loads(closed.stdout)["status"] == "complete"

    assert _interview_complete(tmp_path, rid) is True


def test_cli_help_lists_consistent_interview_actions(tmp_path: Path) -> None:
    help_result = _run_omg("interview", "--help", cwd=tmp_path)
    assert help_result.returncode == 0
    output = help_result.stdout + help_result.stderr
    for action in ("start", "answer", "status", "pressure-pass", "close"):
        assert action in output
