"""Small deterministic, resumable requirements interview state machine."""
from __future__ import annotations
import json
import re
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from omg_cli.evidence import (
    CLI_WRITER,
    _atomic_write_json,
    assert_safe_supervised_parent,
    sha256_bytes,
    validate_identifier,
)
from omg_cli.state import (
    RunSchema,
    classify_run_schema,
    clear_active,
    create_run,
    execution_lease,
    load_active_run,
    load_run,
    write_status,
)
PROFILE_CONFIG = {
    "quick": {"threshold": 0.30, "max_rounds": 5},
    "standard": {"threshold": 0.20, "max_rounds": 12},
    "deep": {"threshold": 0.15, "max_rounds": 20},
}
BROWNFIELD_WEIGHTS = {
    "intent": 0.25,
    "outcome": 0.20,
    "scope": 0.20,
    "constraints": 0.15,
    "success": 0.10,
    "context": 0.10,
}
GREENFIELD_WEIGHTS = {
    "intent": 0.30,
    "outcome": 0.25,
    "scope": 0.20,
    "constraints": 0.15,
    "success": 0.10,
}
COMPONENT_DIMENSION = {
    "intent": "intent",
    "outcome": "outcome",
    "scope": "scope",
    "non_goals": "scope",
    "decision_boundaries": "constraints",
    "constraints": "constraints",
    "success": "success",
    "acceptance": "success",
    "context": "context",
}
COMPONENT_STAGES = (
    ("intent", "outcome", "scope", "non_goals", "decision_boundaries"),
    ("constraints", "success", "acceptance"),
    ("context",),
)
QUESTIONS = {
    "intent": "Why is this change needed, beyond the immediate symptom?",
    "outcome": "What observable end state should this work produce?",
    "scope": "What exact behavior or deliverable is in scope?",
    "non_goals": "What must this work explicitly not change or build?",
    "decision_boundaries": "Which decisions may the agent make without asking again?",
    "constraints": "Which technical, business, compatibility, or safety constraint is binding?",
    "success": "How should a reviewer judge that the desired outcome was achieved?",
    "acceptance": "What concrete checks must pass before implementation may begin?",
    "context": "Which existing system facts are essential for a correct change?",
}
REQUIRED = tuple(COMPONENT_DIMENSION)
ALIASES = {
    "intent": "intent",
    "outcome": "outcome",
    "scope": "scope",
    "constraint": "constraints",
    "constraints": "constraints",
    "success": "success",
    "success criteria": "success",
    "context": "context",
    "non goal": "non_goals",
    "non goals": "non_goals",
    "non-goal": "non_goals",
    "non-goals": "non_goals",
    "decision boundary": "decision_boundaries",
    "decision boundaries": "decision_boundaries",
    "acceptance": "acceptance",
    "acceptance criteria": "acceptance",
}
class InterviewError(ValueError):
    pass
class InterviewIncomplete(InterviewError):
    def __init__(self, result: dict[str, Any]):
        super().__init__("interview is not ready to close")
        self.result = result
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
def _run_dir(root: Path, run_id: str) -> Path:
    from omg_cli.state import _safe_run_id

    return root / ".omg" / "state" / "runs" / _safe_run_id(run_id)
def interview_state_path(root: Path | str, run_id: str) -> Path:
    run_id = validate_identifier(run_id, label="run_id")
    return _run_dir(Path(root), run_id) / "interview.json"
def interview_spec_path(root: Path | str, run_id: str) -> Path:
    run_id = validate_identifier(run_id, label="run_id")
    return _run_dir(Path(root), run_id) / "stages" / "interview-spec.json"
def interview_transcript_path(root: Path | str, run_id: str) -> Path:
    """The single state JSON is also the authoritative transcript."""
    return interview_state_path(root, run_id)
def _canonical(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InterviewError(f"corrupt {label}: {path}") from exc
    if not isinstance(value, dict):
        raise InterviewError(f"corrupt {label}: expected object")
    return value
def ambiguity_score(scores: Mapping[str, Any], *, context_type: str) -> float:
    weights = (
        BROWNFIELD_WEIGHTS
        if context_type == "brownfield"
        else GREENFIELD_WEIGHTS
        if context_type == "greenfield"
        else None
    )
    if weights is None or set(scores) != set(weights):
        raise InterviewError("score topology mismatch")
    clarity = 0.0
    for name, weight in weights.items():
        value = scores[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise InterviewError(f"invalid score: {name}")
        clarity += float(value) * weight
    return round(1.0 - clarity, 6)
def _repo_evidence(root: Path) -> list[str]:
    names = [name for name in (".git", "AGENTS.md", "README.md", "pyproject.toml") if (root / name).exists()]
    if any(p.is_dir() for p in root.iterdir() if not p.name.startswith(".")):
        names.append("source_tree")
    return names
def detect_context_type(root: Path | str) -> str:
    return "brownfield" if _repo_evidence(Path(root).resolve()) else "greenfield"
def build_topology(root: Path | str, *, context_type: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    kind = context_type or detect_context_type(root)
    if kind not in {"brownfield", "greenfield"}:
        raise InterviewError("context_type must be brownfield or greenfield")
    return {
        "context_type": kind,
        "active_dimensions": list(BROWNFIELD_WEIGHTS if kind == "brownfield" else GREENFIELD_WEIGHTS),
        "deferred_dimensions": [] if kind == "brownfield" else ["context"],
        "repo_evidence": _repo_evidence(root),
        "locked": True,
    }
def _labeled(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in re.split(r"[\n;]+", text):
        if ":" not in part:
            continue
        label, value = part.split(":", 1)
        key = ALIASES.get(" ".join(label.strip().lower().split()))
        value = " ".join(value.strip().split())
        if key and value:
            result[key] = value
    return result
def _words(text: str) -> int:
    return len(re.findall(r"[^\W_]+", text, flags=re.UNICODE))
def _improve(previous: float, text: str, *, structured: bool = False) -> float:
    quality = min(0.97, 0.45 + min(_words(text), 20) * 0.025)
    if structured:
        quality = max(quality, min(0.996, 0.78 + min(_words(text), 12) * 0.018))
    return round(previous + (1.0 - previous) * quality, 6)
def _required(state: Mapping[str, Any]) -> tuple[str, ...]:
    return REQUIRED if state["context_type"] == "brownfield" else tuple(x for x in REQUIRED if x != "context")
def _missing(state: Mapping[str, Any]) -> list[str]:
    return [name for name in _required(state) if not str(state["sections"].get(name, "")).strip()]
def _next_component(state: Mapping[str, Any]) -> str | None:
    missing = set(_missing(state))
    for stage in COMPONENT_STAGES:
        available = [x for x in stage if x in missing and COMPONENT_DIMENSION[x] in state["scores"]]
        if available:
            return min(available, key=lambda x: (state["scores"][COMPONENT_DIMENSION[x]], stage.index(x)))
    if state["ambiguity"] > state["threshold"]:
        return min(state["scores"], key=state["scores"].get)
    return None
def _readiness(state: Mapping[str, Any]) -> list[str]:
    reasons = []
    if _missing(state):
        reasons.append("missing sections: " + ", ".join(_missing(state)))
    if state["ambiguity"] > state["threshold"]:
        reasons.append(f"ambiguity {state['ambiguity']:.3f} exceeds threshold {state['threshold']:.2f}")
    if state["context_type"] == "brownfield" and not state["topology"]["repo_evidence"]:
        reasons.append("brownfield repo evidence is missing")
    if not state["pressure_passes"]:
        reasons.append("explicit pressure pass is required")
    return reasons
def _refresh(state: dict[str, Any], invocation_id: str) -> None:
    state["ambiguity"] = ambiguity_score(state["scores"], context_type=state["context_type"])
    reasons = _readiness(state)
    if not reasons:
        state.update(status="ready_to_close", pending_question=None, blocker=None)
        state["resume_command"] = f"omg interview close --run {state['run_id']}"
    elif reasons == ["explicit pressure pass is required"]:
        state.update(status="ready_for_pressure_pass", pending_question=None)
        state["blocker"] = {"code": "pressure_pass_required", "reasons": reasons}
        state["resume_command"] = f"omg interview pressure-pass --run {state['run_id']} --text RATIONALE"
    else:
        component = _next_component(state)
        question = None
        if component and len(state["rounds"]) < state["max_rounds"]:
            pressure = component not in _missing(state)
            question = {
                "question_id": str(uuid.uuid4()),
                "run_id": state["run_id"],
                "session_id": state["session_id"],
                "invocation_id": invocation_id,
                "round": len(state["rounds"]) + 1,
                "dimension": COMPONENT_DIMENSION.get(component, component),
                "component": component,
                "text": (
                    f"What concrete example or tradeoff would make the current {component} statement testable?"
                    if pressure
                    else QUESTIONS[component]
                ),
            }
        state.update(status="waiting_input", pending_question=question)
        if question:
            state["resume_command"] = (
                f"omg interview answer --run {state['run_id']} --question-id "
                f"{question['question_id']} --text TEXT"
            )
            code = "interview_waiting_input"
        else:
            state["resume_command"] = f"omg interview pressure-pass --run {state['run_id']} --text RATIONALE"
            code = "interview_round_cap_reached"
        state["blocker"] = {"code": code, "reasons": reasons}
    state["revision"] += 1
    state["updated_at"] = _now()
def _validate(root: Path, run: Mapping[str, Any], state: dict[str, Any]) -> None:
    if run.get("mode") != "interview":
        raise InterviewError(f"wrong run mode: {run.get('mode')!r}")
    if state.get("writer") != CLI_WRITER or state.get("schema_version") != 2:
        raise InterviewError("invalid interview authority/schema")
    if state.get("run_id") != run.get("run_id") or state.get("task") != run.get("goal"):
        raise InterviewError("stale interview run binding")
    try:
        if str(uuid.UUID(str(state.get("session_id")))) != state.get("session_id"):
            raise ValueError
    except ValueError as exc:
        raise InterviewError("invalid interview session binding") from exc
    profile = state.get("profile")
    if profile not in PROFILE_CONFIG or any(
        state.get(key) != PROFILE_CONFIG[profile][key]
        for key in ("threshold", "max_rounds")
    ):
        raise InterviewError("stale interview profile")
    expected = list(BROWNFIELD_WEIGHTS if state.get("context_type") == "brownfield" else GREENFIELD_WEIGHTS)
    if state.get("topology", {}).get("locked") is not True or state["topology"].get("active_dimensions") != expected:
        raise InterviewError("stale interview topology")
    if state.get("ambiguity") != ambiguity_score(state.get("scores", {}), context_type=state["context_type"]):
        raise InterviewError("corrupt ambiguity score")
    question = state.get("pending_question")
    if question and (question.get("run_id") != state["run_id"] or question.get("session_id") != state["session_id"]):
        raise InterviewError("stale pending question")
    if state.get("status") == "complete":
        expected_path = interview_spec_path(root, str(run["run_id"]))
        if state.get("spec_path") != str(expected_path.relative_to(root)):
            raise InterviewError("interview spec path mismatch")
        artifact = _read_json(expected_path, "interview spec")
        content, stamp = artifact.get("content"), artifact.get("stamp")
        if not isinstance(content, dict) or not isinstance(stamp, dict):
            raise InterviewError("corrupt interview spec envelope")
        if stamp.get("writer") != CLI_WRITER or not stamp.get("invocation_id"):
            raise InterviewError("interview spec lacks CLI invocation stamp")
        if sha256_bytes(_canonical(content)) != stamp.get("content_sha256"):
            raise InterviewError("interview spec hash mismatch")
        if any(content.get(k) != state.get(k) or stamp.get(k) != state.get(k) for k in ("run_id", "session_id")):
            raise InterviewError("interview spec identity mismatch")
def _load(root: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = validate_identifier(run_id, label="run_id")
    run = load_run(root, run_id)
    if run is None:
        raise InterviewError(f"no or corrupt run found: {run_id}")
    try:
        schema = classify_run_schema(run)
    except (TypeError, ValueError) as exc:
        raise InterviewError(f"invalid run schema: {exc}") from exc
    if schema is not RunSchema.STRICT_V2:
        raise InterviewError("interview requires strict-v2 run")
    if run.get("mode") != "interview":
        raise InterviewError(f"wrong run mode: {run.get('mode')!r}")
    state = _read_json(interview_state_path(root, run_id), "interview state")
    _validate(root, run, state)
    return run, state
def _save(root: Path, state: dict[str, Any], lease: Any) -> None:
    lease.assert_current()
    _atomic_write_json(interview_state_path(root, state["run_id"]), state)
def _result(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: state.get(key) for key in (
        "writer", "run_id", "session_id", "profile", "context_type", "status",
        "scores", "ambiguity", "threshold", "pending_question", "resume_command",
        "blocker", "spec_path", "transcript_path",
    )} | {
        "rounds_completed": len(state["rounds"]),
        "max_rounds": state["max_rounds"],
        "weakest_dimension": min(state["scores"], key=state["scores"].get),
    }
def _status_extra(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stage": "interview",
        "interview_status": state["status"],
        "interview_session_id": state["session_id"],
        "ambiguity": state["ambiguity"],
        "blocker": state.get("blocker"),
        "next_action": state.get("resume_command"),
    }
def start_interview(root: Path | str, task: str, *, profile: str = "standard", context_type: str | None = None, force: bool = False) -> dict[str, Any]:
    root = Path(root).resolve()
    task = (task or "").strip()
    if not task or profile not in PROFILE_CONFIG:
        raise InterviewError("valid task text and profile are required")
    assert_safe_supervised_parent()
    try:
        run = create_run(root, mode="interview", goal=task, force=force, extra={
            "schema_version": 2, "lifecycle_version": 2, "interview_profile": profile,
        })
    except RuntimeError as exc:
        raise InterviewError(str(exc)) from exc
    run_id, session_id = run["run_id"], str(uuid.uuid4())
    with execution_lease(root, run_id, intent="interview-start") as lease:
        topology = build_topology(root, context_type=context_type)
        scores = {name: 0.0 for name in topology["active_dimensions"]}
        sections = {name: "" for name in REQUIRED}
        if topology["context_type"] == "brownfield":
            sections["context"] = "Repository evidence: " + ", ".join(topology["repo_evidence"])
            scores["context"] = 0.90
        else:
            sections["context"] = "Greenfield task."
        for key, value in _labeled(task).items():
            sections[key] = value
            dimension = COMPONENT_DIMENSION[key]
            if dimension in scores:
                scores[dimension] = _improve(scores[dimension], value, structured=True)
        state = {
            "writer": CLI_WRITER, "schema_version": 2, "run_id": run_id,
            "session_id": session_id, "task": task, "profile": profile,
            "context_type": topology["context_type"], "topology": topology,
            "threshold": PROFILE_CONFIG[profile]["threshold"],
            "max_rounds": PROFILE_CONFIG[profile]["max_rounds"], "scores": scores,
            "sections": sections, "rounds": [], "pressure_passes": [],
            "ambiguity": ambiguity_score(scores, context_type=topology["context_type"]),
            "status": "initializing", "pending_question": None,
            "revision": 0, "created_at": _now(), "spec_path": None,
            "transcript_path": str(interview_state_path(root, run_id).relative_to(root)),
        }
        _refresh(state, lease.invocation_id)
        _save(root, state, lease)
        write_status(root, run_id, "blocked", extra=_status_extra(state), lease=lease)
    return _result(state)
def _apply_text(state: dict[str, Any], text: str, component: str) -> None:
    labeled = _labeled(text)
    updates = labeled or {component: " ".join(text.split())}
    for key, value in updates.items():
        old = state["sections"].get(key, "")
        state["sections"][key] = value if not old else f"{old}\n\n{value}"
        dimension = COMPONENT_DIMENSION[key]
        if dimension in state["scores"]:
            state["scores"][dimension] = _improve(state["scores"][dimension], value, structured=bool(labeled))
def answer_interview(root: Path | str, run_id: str, text: str, *, question_id: str | None = None) -> dict[str, Any]:
    root, text = Path(root).resolve(), (text or "").strip()
    if not text:
        raise InterviewError("answer text is required")
    assert_safe_supervised_parent()
    _, before = _load(root, run_id)
    pending = before.get("pending_question")
    if not pending:
        raise InterviewError(f"no pending question; resume with: {before.get('resume_command')}")
    if question_id is not None and question_id != pending["question_id"]:
        raise InterviewError(f"stale question_id {question_id!r}; current is {pending['question_id']!r}")
    with execution_lease(root, run_id, intent="interview-answer") as lease:
        _, state = _load(root, run_id)
        question = state.get("pending_question")
        if not question or (question_id is not None and question_id != question["question_id"]):
            raise InterviewError("stale pending question")
        score_before, ambiguity_before = dict(state["scores"]), state["ambiguity"]
        _apply_text(state, text, question["component"])
        state["rounds"].append({
            "run_id": run_id, "session_id": state["session_id"],
            "invocation_id": lease.invocation_id, "question": question, "answer": text,
            "scores_before": score_before, "ambiguity_before": ambiguity_before,
        })
        state["pending_question"] = None
        _refresh(state, lease.invocation_id)
        state["rounds"][-1]["scores_after"] = dict(state["scores"])
        state["rounds"][-1]["ambiguity_after"] = state["ambiguity"]
        _save(root, state, lease)
        write_status(root, run_id, "blocked", extra=_status_extra(state), lease=lease)
    return _result(state)
def pressure_pass_interview(root: Path | str, run_id: str, text: str) -> dict[str, Any]:
    root, text = Path(root).resolve(), (text or "").strip()
    if not text:
        raise InterviewError("pressure-pass rationale is required")
    assert_safe_supervised_parent()
    _load(root, run_id)
    with execution_lease(root, run_id, intent="interview-pressure-pass") as lease:
        _, state = _load(root, run_id)
        for key, value in _labeled(text).items():
            _apply_text(state, f"{key.replace('_', ' ')}: {value}", key)
        state["pressure_passes"].append({
            "run_id": run_id, "session_id": state["session_id"],
            "invocation_id": lease.invocation_id, "rationale": text, "at": _now(),
        })
        state["pending_question"] = None
        _refresh(state, lease.invocation_id)
        _save(root, state, lease)
        write_status(root, run_id, "blocked", extra=_status_extra(state), lease=lease)
    return _result(state)
def close_interview(root: Path | str, run_id: str) -> dict[str, Any]:
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    _, current = _load(root, run_id)
    if current["status"] == "complete":
        return _result(current)
    with execution_lease(root, run_id, intent="interview-close") as lease:
        _, state = _load(root, run_id)
        reasons = _readiness(state)
        if reasons:
            state["pending_question"] = None
            _refresh(state, lease.invocation_id)
            _save(root, state, lease)
            write_status(root, run_id, "blocked", extra=_status_extra(state), lease=lease)
            result = _result(state)
            result["readiness_failures"] = reasons
            raise InterviewIncomplete(result)
        content = {
            "schema_version": 2, "run_id": run_id, "session_id": state["session_id"],
            "profile": state["profile"], "context_type": state["context_type"],
            "topology": state["topology"], "scores": state["scores"],
            "ambiguity": state["ambiguity"], "threshold": state["threshold"],
            "intent": state["sections"]["intent"], "desired_outcome": state["sections"]["outcome"],
            "in_scope": state["sections"]["scope"], "constraints": state["sections"]["constraints"],
            "success_criteria": state["sections"]["success"], "context": state["sections"]["context"],
            "non_goals": state["sections"]["non_goals"],
            "decision_boundaries": state["sections"]["decision_boundaries"],
            "acceptance": state["sections"]["acceptance"],
            "transcript": state["rounds"], "pressure_passes": state["pressure_passes"],
            "execution_contract": {
                "version": 1, "source": "deep-interview", "allow_task_shrink": False,
                "completion_unit": state["task"],
                "stop_condition": "Acceptance must pass or a concrete resumable blocker must be recorded.",
            },
        }
        artifact = {"content": content, "stamp": {
            "writer": CLI_WRITER, "schema_version": 2, "run_id": run_id,
            "session_id": state["session_id"], "invocation_id": lease.invocation_id,
            "content_sha256": sha256_bytes(_canonical(content)), "stamped_at": _now(),
        }}
        path = interview_spec_path(root, run_id)
        _atomic_write_json(path, artifact)
        state.update(status="complete", pending_question=None, blocker=None)
        state["spec_path"] = str(path.relative_to(root))
        state["resume_command"] = f"omg ralplan {shlex.quote(state['task'])}"
        state["closed_by_invocation_id"] = lease.invocation_id
        state["revision"] += 1
        state["updated_at"] = _now()
        _save(root, state, lease)
        write_status(root, run_id, "running", extra={**_status_extra(state), "stage": "interview_complete"}, lease=lease)
    clear_active(root, run_id)
    return _result(state)
def interview_status(root: Path | str, run_id: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    if run_id is None:
        active = load_active_run(root)
        if active is None:
            raise InterviewError("no active interview run; pass --run RUN")
        run_id = str(active["run_id"])
    _, state = _load(root, run_id)
    return _result(state)
__all__ = [
    "BROWNFIELD_WEIGHTS", "InterviewError", "InterviewIncomplete", "PROFILE_CONFIG",
    "ambiguity_score", "answer_interview", "build_topology", "close_interview",
    "detect_context_type", "interview_spec_path", "interview_state_path",
    "interview_status", "interview_transcript_path", "pressure_pass_interview",
    "start_interview",
]
