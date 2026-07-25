# omg_cli/autopilot.py
"""Strict Autopilot v2 coordinator — legal phase transitions only.

Composes interview → ralplan → ultragoal/impl → review → ultraqa → acceptance.
Does not write verified except via same-process set_verified after acceptance.
"""
from __future__ import annotations

import json
import os
import sys
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

        # Gate by DESTINATION phase (not only specific src) so blocked→qa
        # / blocked→implement cannot skip quality or consensus gates.
        if next_phase == "ralplan":
            # First entry from interview needs evidence; recovery from later
            # phases may re-enter ralplan with replan reason.
            if src == "interview" and not (evidence or {}).get("interview_complete"):
                raise AutopilotError("no interview gate → no ralplan handoff")
        if next_phase == "implement":
            if not (evidence or {}).get("consensus"):
                raise AutopilotError("no consensus → no implementation")
        if next_phase == "qa":
            if not stage_review_is_clean(root, run_id):
                raise AutopilotError(
                    "no clean review → no QA "
                    "(requires CLI-stamped stages/structured_review.json clean=true)"
                )
        if next_phase == "acceptance":
            if not stage_qa_is_clean(root, run_id):
                raise AutopilotError(
                    "no clean QA → no acceptance "
                    "(requires CLI-stamped stages/ultraqa.json status=clean)"
                )
        if next_phase == "verified":
            raise AutopilotError(
                "verified only via complete_with_acceptance (same-process)"
            )

        if next_phase == "implement":
            # Any (re-)entry into implement produces new, unreviewed product
            # code. Prior clean review/QA stamps must never remain authoritative
            # for a later qa/acceptance gate — closes the
            # qa→blocked→implement→blocked→qa false-green round-trip.
            invalidate_quality_stages(
                root, run_id, reason=f"(re)implement from {src}"
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
        if next_phase == "review" and src in {"rework", "implement", "blocked"}:
            # Re-entering review (after leaving the linear implement→review
            # edge) requires a fresh structured_review stamp — includes
            # qa→blocked→review so a pre-block clean stamp cannot reopen qa.
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


def _sync_autopilot_verified(
    root: Path,
    run_id: str,
    *,
    lease: Any,
    event: str,
) -> dict[str, Any]:
    """Mark autopilot phase verified + align status.autopilot_phase (lease held).

    Does not re-commit verified status (use set_verified first when needed).
    """
    from omg_cli.state import merge_status_fields

    state = load_autopilot(root, run_id)
    state["phase"] = "verified"
    state["verified"] = True
    state["history"] = list(state.get("history") or []) + [
        {
            "phase": "verified",
            "at": _utc_now(),
            "event": event,
        }
    ]
    _save(root, run_id, state, lease)
    merge_status_fields(
        root,
        run_id,
        {
            "stage": "autopilot",
            "autopilot_phase": "verified",
            "blocker": None,
        },
        lease=lease,
    )
    return status_autopilot(root, run_id)


def _soft_accept_break_glass(*, allow_soft_accept: bool = False) -> bool:
    if allow_soft_accept:
        return True
    if os.environ.get("OMG_ALLOW_SOFT_ACCEPT") != "1":
        return False
    return sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False


def _refuse_analyze_only_autopilot_acceptance(
    root: Path,
    run_id: str,
    prd_obj: dict[str, Any],
    *,
    allow_soft_accept: bool = False,
) -> None:
    """Autopilot runs require goal-bound acceptance (not lint-only false-green)."""
    run = load_run(root, run_id) or {}
    if str(run.get("mode") or "") != "autopilot":
        return
    from omg_cli.acceptance import collect_commands
    from omg_cli.command_policy import GOAL_BOUND_ACCEPT_TIP, is_analyze_only

    if not is_analyze_only(collect_commands(prd_obj)):
        return
    if _soft_accept_break_glass(allow_soft_accept=allow_soft_accept):
        return
    raise AutopilotError(
        "autopilot verified refused: acceptance manifest is analyze-only "
        f"(lint/format/static checks without a goal-bound test run). "
        f"{GOAL_BOUND_ACCEPT_TIP}"
    )


def complete_with_acceptance(
    root: Path | str,
    run_id: str,
    *,
    prd: Mapping[str, Any] | None = None,
    allow_soft_accept: bool = False,
) -> dict[str, Any]:
    """Terminal path: freeze+run acceptance in this process, then set_verified.

    Acceptance runs under the execution lease owner (no transition guard during
    freeze/run). ``set_verified`` then linearizes the terminal status. Disk-only
    stamps from other processes cannot promote.

    Short-circuit: if the run is already ``verified`` (e.g. prior ``omg accept``)
    and autopilot is in ``acceptance`` or ``verified``, sync phase without
    re-running freeze_and_run.
    """
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    run_id = validate_identifier(run_id, label="run_id")
    from omg_cli.acceptance import (
        freeze_and_run,
        is_trusted_acceptance,
        materialize_prd_from_ultraqa,
    )
    from omg_cli.state import set_verified

    pre = load_autopilot(root, run_id)
    phase = str(pre.get("phase") or "")
    run_pre = load_run(root, run_id) or {}
    already_verified = run_pre.get("verified") is True or run_pre.get("status") == "verified"

    # Terminal short-circuit: already verified (idempotent complete).
    if phase == "verified" and already_verified:
        return status_autopilot(root, run_id)

    if phase not in ("acceptance", "verified"):
        raise AutopilotError(
            f"acceptance only from acceptance phase (got {phase!r})"
        )

    with execution_lease(root, run_id, intent="autopilot-accept") as lease:
        state = load_autopilot(root, run_id)
        phase2 = str(state.get("phase") or "")
        run_now = load_run(root, run_id) or {}
        already = run_now.get("verified") is True or run_now.get("status") == "verified"

        if phase2 == "verified" and already:
            return status_autopilot(root, run_id)

        if already and phase2 in ("acceptance", "verified"):
            # omg accept already verified; do not re-run freeze_and_run.
            return _sync_autopilot_verified(
                root,
                run_id,
                lease=lease,
                event="short_circuit_already_verified",
            )

        if phase2 != "acceptance":
            raise AutopilotError(
                f"acceptance only from acceptance phase (got {phase2!r})"
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
            # Auto-build from clean UltraQA scenarios when present.
            try:
                prd_obj = materialize_prd_from_ultraqa(
                    root,
                    run_id,
                    goal=str(state.get("goal") or "") or None,
                    overwrite=False,
                )
            except ValueError as exc:
                raise AutopilotError(
                    "complete_with_acceptance requires prd.json or prd= "
                    f"(or clean ultraqa to materialize): {exc}"
                ) from exc

        _refuse_analyze_only_autopilot_acceptance(
            root,
            run_id,
            prd_obj,
            allow_soft_accept=allow_soft_accept,
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
        return _sync_autopilot_verified(
            root,
            run_id,
            lease=lease,
            event="same_process_acceptance",
        )


def set_awaiting_confirmation(
    root: Path | str,
    run_id: str,
    value: bool,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mirror flag into status via merge_status_fields. Never touch verified."""
    from omg_cli.state import merge_status_fields

    root = Path(root).resolve()
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

    awaiting = bool(value)
    merge_status_fields(
        root,
        run_id,
        {
            "autopilot_awaiting": awaiting,
            # Clearing always wipes reason so --clear --reason cannot leave a stale note.
            "autopilot_awaiting_reason": (reason or "") if awaiting else "",
        },
    )
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


# Phase → skill body for outer-driver prompt injection
_PHASE_SKILL_REL: dict[str, str] = {
    "interview": "skills/omg-deep-interview/SKILL.md",
    "ralplan": "skills/omg-ralplan/SKILL.md",
    "implement": "skills/omg-ultrawork/SKILL.md",
    "rework": "skills/omg-ultrawork/SKILL.md",
    "review": "skills/omg-dual-review/SKILL.md",
    "qa": "skills/omg-ultraqa/SKILL.md",
    "acceptance": "skills/omg-autopilot/SKILL.md",
    "blocked": "skills/omg-autopilot/SKILL.md",
}


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_phase_skill(phase: str, *, root: Path | None = None) -> str:
    rel = _PHASE_SKILL_REL.get(phase) or _PHASE_SKILL_REL["acceptance"]
    base = root if root is not None else _plugin_root()
    path = base / rel
    if not path.is_file():
        path = _plugin_root() / rel
    if not path.is_file():
        return f"(skill missing for phase {phase!r}: {rel})"
    return path.read_text(encoding="utf-8")


def autopilot_context_pack(
    *,
    run_id: str,
    phase: str,
    goal: str,
    next_gate: str,
) -> str:
    """Build autopilot phase context block for prompt injection."""
    lines = [
        "## Autopilot context pack (CLI injection — fresh each phase)",
        f"- run_id: {run_id}",
        f"- phase={phase}",
        f"- goal: {(goal or '').strip() or '(unspecified)'}",
        f"- next_gate: {next_gate}",
        "- Do **not** ask the user mid-phase; record uncertainty under "
        "`.omg/artifacts/` or `omg autopilot transition --phase blocked`.",
        "- Only the omg CLI sets verified; use `omg autopilot complete` at acceptance.",
    ]
    return "\n".join(lines)


def build_phase_prompt(
    phase: str,
    *,
    root: Path | str,
    goal: str,
    run_id: str,
) -> str:
    """Compose grok prompt for one autopilot phase (skill + pack + no-ask rule)."""
    from omg_cli.modes import HARD_RULES_REMINDER
    from omg_cli.stop_gate import continuation_reason

    root_path = Path(root).resolve()
    phase_key = (phase or "").strip() or "unknown"
    reason = continuation_reason(phase_key, goal=goal, run_id=run_id)
    # Extract next gate clause from continuation_reason for the pack header.
    next_gate = "see continuation block"
    if "Next gate:" in reason:
        next_gate = reason.split("Next gate:", 1)[1].split(".", 1)[0].strip()

    skill = _load_phase_skill(phase_key, root=root_path)
    parts = [
        skill,
        "",
        HARD_RULES_REMINDER,
        "",
        autopilot_context_pack(
            run_id=run_id,
            phase=phase_key,
            goal=goal,
            next_gate=next_gate,
        ),
        "",
        "## Phase continuation (hard)",
        reason,
        "",
        "## Goal",
        (goal or "").strip() or "(no goal provided)",
        "",
        "Follow the phase skill above. Do not ask the user mid-phase.",
    ]
    return "\n".join(parts)


def _interview_complete(root: Path, run_id: str) -> bool:
    from omg_cli.stop_gate import _read_interview_status

    status = _read_interview_status(root, run_id)
    return status == "complete"


def _consensus_ready(root: Path, run_id: str) -> bool:
    run = load_run(root, run_id) or {}
    if run.get("ralplan_consensus") is True:
        return True
    from omg_cli.ralplan import ralplan_state_path

    data = _read_stage_json(ralplan_state_path(root, run_id))
    if data and data.get("accepted") is True:
        return True
    marker = (
        Path(root)
        / ".omg"
        / "artifacts"
        / f"ralplan-consensus-{run_id}.json"
    )
    return marker.is_file()


def _try_advance_after_launch(root: Path, run_id: str, phase: str) -> str:
    """Inspect stamps after a grok launch; transition when gates are satisfied."""
    phase = str(phase)
    try:
        if phase == "interview" and _interview_complete(root, run_id):
            transition(
                root,
                run_id,
                "ralplan",
                evidence={"interview_complete": True},
                reason="interview complete",
            )
            return "ralplan"
        if phase == "ralplan" and _consensus_ready(root, run_id):
            transition(
                root,
                run_id,
                "implement",
                evidence={"consensus": True},
                reason="ralplan consensus",
            )
            return "implement"
        if phase == "implement":
            transition(
                root,
                run_id,
                "review",
                reason="implementation ready for review",
            )
            return "review"
        if phase == "review" and stage_review_is_clean(root, run_id):
            transition(root, run_id, "qa", reason="structured review clean")
            return "qa"
        if phase == "qa" and stage_qa_is_clean(root, run_id):
            transition(
                root,
                run_id,
                "acceptance",
                reason="ultraqa clean",
            )
            return "acceptance"
        if phase == "acceptance":
            out = complete_with_acceptance(root, run_id)
            if out.get("phase") == "verified":
                return "verified"
    except AutopilotError:
        pass
    return phase


def run_autopilot(
    root: Path | str,
    goal: str,
    *,
    skip_interview: bool = False,
    resume_run_id: str | None = None,
    max_phase_cycles: int = 5,
    dry_run: bool = False,
    timeout: float | None = None,
    yolo: bool = False,
    safe: bool = False,
    force: bool = False,
    **launch_kw: Any,
) -> int:
    """Outer CLI driver: launch grok per phase until verified or pause/terminal.

    Tertiary cross-turn persistence (beyond in-session Stop pin). Writes RESUME.md
    each phase; pauses at incomplete interview with resume hint.
    """
    import sys

    from omg_cli.modes import (
        _launch_grok,
        _run_dir,
        build_grok_argv,
        resolve_launch_timeout,
    )
    from omg_cli.resume import write_resume_md
    from omg_cli.state import load_active_run

    root_path = Path(root).resolve()
    assert_safe_supervised_parent()
    requested_goal = (goal or "").strip()
    run_id: str

    if resume_run_id is not None:
        if resume_run_id == "__active__":
            run = load_active_run(root_path)
            if run is None:
                print("omg autopilot run: no active run to resume", file=sys.stderr)
                return 1
            resume_run_id = str(run["run_id"])
        else:
            run = load_run(root_path, str(resume_run_id))
        if run is None:
            print(
                f"omg autopilot run: no run found: {resume_run_id!r}",
                file=sys.stderr,
            )
            return 1
        if str(run.get("mode") or "") != "autopilot":
            print(
                f"omg autopilot run: run {resume_run_id!r} is mode="
                f"{run.get('mode')!r}",
                file=sys.stderr,
            )
            return 1
        run_id = str(run["run_id"])
        frozen_goal = str(run.get("goal") or "").strip()
        if requested_goal and requested_goal != frozen_goal:
            print(
                "omg autopilot run: conflicting goal on resume; omit goal text",
                file=sys.stderr,
            )
            return 2
        goal = frozen_goal or requested_goal
    else:
        if not requested_goal:
            print("omg autopilot run: goal text required", file=sys.stderr)
            return 2
        st = start_autopilot(
            root_path,
            requested_goal,
            force=force,
            skip_interview=skip_interview,
        )
        run_id = str(st["run_id"])
        goal = requested_goal

    launch_timeout = resolve_launch_timeout(timeout, dry_run=dry_run)
    run_dir = _run_dir(root_path, run_id)
    phase_cycles: dict[str, int] = {}
    resume_cmd = f"omg autopilot run --resume {run_id}"

    while True:
        st = status_autopilot(root_path, run_id)
        phase = str(st.get("phase") or "")
        run_row = load_run(root_path, run_id) or {}
        if run_row.get("autopilot_awaiting"):
            write_resume_md(root_path, run_id)
            print(resume_cmd)
            return 0
        if phase == "verified":
            write_resume_md(root_path, run_id)
            return 0
        if phase in ("blocked", "cancelled"):
            write_resume_md(root_path, run_id)
            return 1
        if phase == "interview" and not _interview_complete(root_path, run_id):
            write_resume_md(root_path, run_id)
            print(resume_cmd)
            return 0

        phase_cycles[phase] = int(phase_cycles.get(phase, 0)) + 1
        if phase_cycles[phase] > max(1, int(max_phase_cycles)):
            try:
                transition(
                    root_path,
                    run_id,
                    "blocked",
                    reason=f"max_phase_cycles={max_phase_cycles}",
                )
            except AutopilotError:
                pass
            write_resume_md(root_path, run_id)
            return 1

        write_resume_md(root_path, run_id)

        prompt = build_phase_prompt(phase, root=root_path, goal=goal, run_id=run_id)
        argv = build_grok_argv(
            "ralplan",
            goal,
            yolo=yolo,
            safe=safe,
            cwd=root_path,
            project_root=root_path,
            run_id=run_id,
            prompt=prompt,
            skill_root=_plugin_root(),
            **{
                k: v
                for k, v in launch_kw.items()
                if k
                in (
                    "extra",
                    "output_format",
                    "disallow_shell",
                    "new_session_id",
                    "resume_session_id",
                )
            },
        )
        rc = _launch_grok(
            argv,
            cwd=root_path,
            run_dir=run_dir,
            timeout=launch_timeout,
            dry_run=dry_run,
        )
        if rc != 0:
            write_resume_md(root_path, run_id)
            return int(rc)

        new_phase = _try_advance_after_launch(root_path, run_id, phase)
        if dry_run:
            return 0
        if new_phase == phase:
            # No gate progress this launch — stop for cross-turn resume.
            write_resume_md(root_path, run_id)
            print(resume_cmd)
            return 0


__all__ = [
    "LEGAL_TRANSITIONS",
    "AutopilotError",
    "assert_legal_transition",
    "autopilot_context_pack",
    "autopilot_state_path",
    "build_phase_prompt",
    "complete_with_acceptance",
    "invalidate_quality_stages",
    "load_autopilot",
    "run_autopilot",
    "set_awaiting_confirmation",
    "stage_qa_is_clean",
    "stage_review_is_clean",
    "start_autopilot",
    "status_autopilot",
    "transition",
]
