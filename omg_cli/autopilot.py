# omg_cli/autopilot.py
"""Strict Autopilot v2 coordinator — legal phase transitions only.

Composes interview → ralplan → ultragoal/impl → review → ultraqa → acceptance.
Does not write verified except via same-process set_verified after acceptance.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from omg_cli.evidence import CLI_WRITER, assert_safe_supervised_parent, validate_identifier
from omg_cli.state import (
    RunSchema,
    classify_run_schema,
    create_run,
    execution_lease,
    load_run,
    write_status,
)


# Legal forward edges for strict v2 autopilot phases
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "init": frozenset({"interview", "ralplan"}),  # interview skip only if forced clear
    "interview": frozenset({"ralplan", "blocked", "cancelled"}),
    "ralplan": frozenset({"implement", "blocked", "cancelled"}),
    "implement": frozenset({"review", "blocked", "cancelled"}),
    "review": frozenset({"qa", "rework", "ralplan", "blocked", "cancelled"}),
    "rework": frozenset({"review", "blocked", "cancelled"}),
    "qa": frozenset({"acceptance", "ralplan", "rework", "blocked", "cancelled"}),
    "acceptance": frozenset({"verified", "blocked", "cancelled"}),
    "verified": frozenset(),
    "blocked": frozenset({"interview", "ralplan", "implement", "review", "qa", "cancelled"}),
    "cancelled": frozenset(),
}


class AutopilotError(ValueError):
    """Illegal transition or corrupt autopilot state."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_stage_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def stage_review_is_clean(root: Path | str, run_id: str) -> bool:
    """True only when CLI-stamped structured_review.json is clean for this run."""
    from omg_cli.review import review_state_path

    data = _read_stage_json(review_state_path(root, run_id))
    if not data:
        return False
    if data.get("writer") != CLI_WRITER:
        return False
    if data.get("run_id") != run_id:
        return False
    if data.get("invalidated") is True:
        return False
    return data.get("clean") is True


def stage_qa_is_clean(root: Path | str, run_id: str) -> bool:
    """True only when CLI-stamped ultraqa.json is clean (never implies verified)."""
    from omg_cli.qa import qa_state_path

    data = _read_stage_json(qa_state_path(root, run_id))
    if not data:
        return False
    if data.get("writer") != CLI_WRITER:
        return False
    if data.get("run_id") != run_id:
        return False
    if data.get("invalidated") is True:
        return False
    return data.get("clean") is True and data.get("status") == "clean"


def invalidate_quality_stages(root: Path | str, run_id: str, *, reason: str) -> None:
    """Mark review/QA stage stamps stale after rework or replan (CLI write)."""
    from omg_cli.qa import qa_state_path
    from omg_cli.review import review_state_path

    root = Path(root).resolve()
    for path in (review_state_path(root, run_id), qa_state_path(root, run_id)):
        data = _read_stage_json(path)
        if not data:
            continue
        data["clean"] = False
        data["invalidated"] = True
        data["invalidated_reason"] = reason
        data["invalidated_at"] = _utc_now()
        data["writer"] = CLI_WRITER
        if "status" in data and data.get("status") == "clean":
            data["status"] = "invalidated"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def autopilot_state_path(root: Path | str, run_id: str) -> Path:
    run_id = validate_identifier(run_id, label="run_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
        / "autopilot.json"
    )


def _save(root: Path, run_id: str, state: dict[str, Any], lease: Any) -> None:
    lease.assert_current()
    path = autopilot_state_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["writer"] = CLI_WRITER
    state["updated_at"] = _utc_now()
    state["execution_generation"] = getattr(lease, "generation", None)
    state["execution_owner_invocation_id"] = getattr(lease, "invocation_id", None)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_autopilot(root: Path | str, run_id: str) -> dict[str, Any]:
    path = autopilot_state_path(root, run_id)
    if not path.is_file():
        raise AutopilotError(f"autopilot state missing: {run_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("writer") != CLI_WRITER:
        raise AutopilotError("autopilot state lacks CLI writer")
    return data


def assert_legal_transition(src: str, dst: str) -> None:
    allowed = LEGAL_TRANSITIONS.get(src)
    if allowed is None:
        raise AutopilotError(f"unknown phase {src!r}")
    if dst not in allowed:
        raise AutopilotError(f"illegal transition {src!r} -> {dst!r}")


def start_autopilot(
    root: Path | str,
    goal: str,
    *,
    force: bool = False,
    skip_interview: bool = False,
) -> dict[str, Any]:
    """Create strict-v2 autopilot run at interview or ralplan phase."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal = (goal or "").strip()
    if not goal:
        raise AutopilotError("goal text required")
    run = create_run(
        root,
        mode="autopilot",
        goal=goal,
        force=force,
        extra={
            "schema_version": 2,
            "lifecycle_version": 2,
            "stage": "autopilot",
        },
    )
    run_id = run["run_id"]
    phase = "ralplan" if skip_interview else "interview"
    with execution_lease(root, run_id, intent="autopilot-start") as lease:
        state = {
            "writer": CLI_WRITER,
            "schema_version": 2,
            "lifecycle_version": 2,
            "run_id": run_id,
            "goal": goal,
            "phase": phase,
            "cycles": {"review": 0, "qa": 0, "ralplan": 0},
            "history": [{"phase": phase, "at": _utc_now(), "event": "start"}],
            "blocker": None,
            "verified": False,
            "created_at": _utc_now(),
        }
        _save(root, run_id, state, lease)
        write_status(
            root,
            run_id,
            "running",
            extra={
                "stage": "autopilot",
                "autopilot_phase": phase,
            },
            lease=lease,
        )
    return status_autopilot(root, run_id)


def transition(
    root: Path | str,
    run_id: str,
    next_phase: str,
    *,
    reason: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance phase when legal; requires execution lease."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    run_id = validate_identifier(run_id, label="run_id")
    run = load_run(root, run_id)
    if run is None:
        raise AutopilotError(f"run not found: {run_id}")
    try:
        schema = classify_run_schema(run)
    except (TypeError, ValueError) as exc:
        raise AutopilotError(f"refusing malformed/unknown schema: {exc}") from exc
    if schema is not RunSchema.STRICT_V2:
        raise AutopilotError(
            f"autopilot v2 requires strict-v2 run (got {schema})"
        )
    if run.get("mode") != "autopilot":
        raise AutopilotError(f"wrong mode: {run.get('mode')!r}")

    with execution_lease(root, run_id, intent=f"autopilot-{next_phase}") as lease:
        state = load_autopilot(root, run_id)
        src = str(state.get("phase") or "init")
        assert_legal_transition(src, next_phase)

        # Gate predicates — compose completed stage primitives (CLI stamps)
        if next_phase == "ralplan" and src == "interview":
            if not (evidence or {}).get("interview_complete"):
                raise AutopilotError("no interview gate → no ralplan handoff")
        if next_phase == "implement" and src == "ralplan":
            if not (evidence or {}).get("consensus"):
                raise AutopilotError("no consensus → no implementation")
        if next_phase == "qa" and src == "review":
            # Prefer staged structured_review; bare evidence_json cannot satisfy.
            if not stage_review_is_clean(root, run_id):
                raise AutopilotError(
                    "no clean review → no QA "
                    "(requires CLI-stamped stages/structured_review.json clean=true)"
                )
        if next_phase == "acceptance" and src == "qa":
            if not stage_qa_is_clean(root, run_id):
                raise AutopilotError(
                    "no clean QA → no acceptance "
                    "(requires CLI-stamped stages/ultraqa.json status=clean)"
                )
        if next_phase == "verified":
            raise AutopilotError(
                "verified only via complete_with_acceptance (same-process)"
            )

        if next_phase == "ralplan" and src in {"review", "qa"}:
            state["cycles"]["ralplan"] = int(state["cycles"].get("ralplan") or 0) + 1
            # Stale clean stamps must not open QA/acceptance after replan
            invalidate_quality_stages(
                root, run_id, reason=f"replan from {src}"
            )
        if next_phase == "rework":
            state["cycles"]["review"] = int(state["cycles"].get("review") or 0) + 1
            invalidate_quality_stages(
                root, run_id, reason="rework invalidates review/qa stamps"
            )
        if next_phase == "review" and src in {"rework", "implement"}:
            # Re-entering review requires a fresh structured_review stamp
            invalidate_quality_stages(
                root, run_id, reason=f"re-enter review from {src}"
            )
        if next_phase == "qa" and src == "review":
            pass
        if src == "qa" and next_phase == "ralplan":
            state["cycles"]["qa"] = int(state["cycles"].get("qa") or 0) + 1

        state["phase"] = next_phase
        state["history"] = list(state.get("history") or []) + [
            {
                "from": src,
                "phase": next_phase,
                "reason": reason,
                "at": _utc_now(),
            }
        ]
        if next_phase == "blocked":
            state["blocker"] = {"reason": reason or "blocked", "from": src}
            status = "blocked"
        elif next_phase == "cancelled":
            status = "cancelled"
        else:
            state["blocker"] = None
            status = "running"
        _save(root, run_id, state, lease)
        write_status(
            root,
            run_id,
            status,
            extra={
                "stage": "autopilot",
                "autopilot_phase": next_phase,
                "blocker": state.get("blocker"),
            },
            lease=lease,
        )
    return status_autopilot(root, run_id)


def complete_with_acceptance(
    root: Path | str,
    run_id: str,
    *,
    prd: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Terminal path: freeze+run acceptance in this process, then set_verified.

    Acceptance runs under the execution lease owner (no transition guard during
    freeze/run). ``set_verified`` then linearizes the terminal status. Disk-only
    stamps from other processes cannot promote.
    """
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    run_id = validate_identifier(run_id, label="run_id")
    from omg_cli.acceptance import freeze_and_run, is_trusted_acceptance
    from omg_cli.state import set_verified

    # Phase check before host acceptance work
    pre = load_autopilot(root, run_id)
    if pre.get("phase") != "acceptance":
        raise AutopilotError(
            f"acceptance only from acceptance phase (got {pre.get('phase')!r})"
        )

    with execution_lease(root, run_id, intent="autopilot-accept") as lease:
        state = load_autopilot(root, run_id)
        if state.get("phase") != "acceptance":
            raise AutopilotError(
                f"acceptance only from acceptance phase (got {state.get('phase')!r})"
            )

        prd_obj: dict[str, Any] | None = dict(prd) if prd is not None else None
        if prd_obj is None:
            prd_path = (
                Path(root)
                / ".omg"
                / "state"
                / "runs"
                / run_id
                / "prd.json"
            )
            if prd_path.is_file():
                try:
                    loaded = json.loads(prd_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        prd_obj = loaded
                except (OSError, json.JSONDecodeError) as exc:
                    raise AutopilotError(f"prd.json unreadable: {exc}") from exc
        if prd_obj is None:
            raise AutopilotError(
                "complete_with_acceptance requires prd.json or prd= "
                "so freeze_and_run can execute in this process"
            )

        # Same-process freeze + run (registers process-local acceptance token)
        try:
            passed = freeze_and_run(root, run_id, prd_obj)
        except Exception as exc:
            raise AutopilotError(
                f"same-process freeze_and_run failed: {exc}"
            ) from exc
        if not passed:
            raise AutopilotError(
                "verified requires same-process acceptance pass "
                "(freeze_and_run returned false)"
            )
        if not is_trusted_acceptance(root, run_id):
            raise AutopilotError(
                "verified requires same-process acceptance pass "
                "(disk/cross-process stamps cannot promote)"
            )

        try:
            set_verified(root, run_id, force=False, lease=lease)
        except PermissionError as exc:
            raise AutopilotError(
                "set_verified refused; re-run freeze/run acceptance in this process"
            ) from exc
        run = load_run(root, run_id)
        if not run or not (
            run.get("verified") is True or run.get("status") == "verified"
        ):
            raise AutopilotError(
                "set_verified refused; re-run freeze/run acceptance in this process"
            )
        state["phase"] = "verified"
        state["verified"] = True
        state["history"] = list(state.get("history") or []) + [
            {
                "phase": "verified",
                "at": _utc_now(),
                "event": "same_process_acceptance",
            }
        ]
        _save(root, run_id, state, lease)
    return status_autopilot(root, run_id)


def status_autopilot(root: Path | str, run_id: str) -> dict[str, Any]:
    state = load_autopilot(root, run_id)
    run = load_run(root, run_id) or {}
    return {
        "run_id": run_id,
        "phase": state.get("phase"),
        "goal": state.get("goal"),
        "cycles": state.get("cycles"),
        "blocker": state.get("blocker"),
        "verified": bool(run.get("verified") or state.get("verified")),
        "run_status": run.get("status"),
        "legal_next": sorted(LEGAL_TRANSITIONS.get(str(state.get("phase")), frozenset())),
    }


__all__ = [
    "LEGAL_TRANSITIONS",
    "AutopilotError",
    "assert_legal_transition",
    "autopilot_state_path",
    "complete_with_acceptance",
    "invalidate_quality_stages",
    "load_autopilot",
    "stage_qa_is_clean",
    "stage_review_is_clean",
    "start_autopilot",
    "status_autopilot",
    "transition",
]
