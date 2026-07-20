import json
import os
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


def test_cli_help_lists_consistent_interview_actions(tmp_path: Path) -> None:
    help_result = _run_omg("interview", "--help", cwd=tmp_path)
    assert help_result.returncode == 0
    output = help_result.stdout + help_result.stderr
    for action in ("start", "answer", "status", "pressure-pass", "close"):
        assert action in output
