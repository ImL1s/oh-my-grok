"""Staged team pipeline FSM (D2): team-plan → team-prd → team-exec → team-verify → team-fix.

THIN orchestration glue over the experimental team plane. Sequences existing
lanes; does **not** reimplement ralplan / dual_review / a planner or verifier.

- ``team-plan`` / ``team-prd`` — pass-through stage markers (leader/ralplan
  decomposition is consumed via ``--tasks-json`` or a path to existing tasks).
- ``team-exec`` — ``start_team`` then ``collect_team`` (dry-run: start only).
- ``team-verify`` — gates a durable verifier artifact under the run dir via
  POST-A2 ``parse_verdict_file`` (never fakes a verdict).
- ``team-fix`` — bounded re-entry into ``team-exec`` (``--max-fix``, default 3).

Strict transitions + stale verify-stamp invalidation mirror autopilot
discipline. ``verified`` is never written here — only via ``omg accept``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.evidence import CLI_WRITER, assert_safe_supervised_parent, validate_identifier
from omg_cli.state import (
    create_run,
    execution_lease,
    load_run,
    write_status,
)
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    TeamError,
    TeamGateError,
    collect_team,
    experimental_enabled,
    in_spawned_worker_context,
    start_team,
)
from omg_cli.verdict import parse_verdict_file

# ---------------------------------------------------------------------------
# Stages / transitions
# ---------------------------------------------------------------------------

STAGES = (
    "team-plan",
    "team-prd",
    "team-exec",
    "team-verify",
    "team-fix",
)
TERMINAL = frozenset({"complete", "failed", "blocked"})

LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "init": frozenset({"team-plan", "blocked"}),
    "team-plan": frozenset({"team-prd", "blocked", "failed"}),
    "team-prd": frozenset({"team-exec", "blocked", "failed"}),
    "team-exec": frozenset({"team-verify", "blocked", "failed"}),
    "team-verify": frozenset({"complete", "team-fix", "blocked", "failed"}),
    "team-fix": frozenset({"team-exec", "failed", "blocked"}),
    "complete": frozenset(),
    "failed": frozenset(),
    "blocked": frozenset(
        {
            "team-plan",
            "team-prd",
            "team-exec",
            "team-verify",
            "team-fix",
            "failed",
        }
    ),
}

DEFAULT_MAX_FIX = 3
SCHEMA_VERSION = 1

# Severity ranks for cross-artifact aggregation (mirror ralplan / verdict).
_SEVERITY_RANK = {
    "FAILED": 3,
    "REQUEST_CHANGES": 2,
    "APPROVE": 1,
}


class TeamPipelineError(ValueError):
    """Illegal transition or corrupt team-pipeline state."""


# ---------------------------------------------------------------------------
# Paths / IO
# ---------------------------------------------------------------------------


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


def team_pipeline_state_path(root: Path | str, run_id: str) -> Path:
    run_id = validate_identifier(run_id, label="run_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
        / "team-pipeline.json"
    )


def team_verify_stamp_path(root: Path | str, run_id: str) -> Path:
    """CLI-stamped verify gate result (invalidated on re-exec/fix)."""
    run_id = validate_identifier(run_id, label="run_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
        / "team-verify.json"
    )


def team_verifier_artifact_paths(root: Path | str, run_id: str) -> tuple[Path, Path]:
    """Convention: leader/team produces ``stages/team-verifier.{md,json}``."""
    run_id = validate_identifier(run_id, label="run_id")
    stages = (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
    )
    return stages / "team-verifier.md", stages / "team-verifier.json"


def assert_legal_transition(src: str, dst: str) -> None:
    allowed = LEGAL_TRANSITIONS.get(src)
    if allowed is None:
        raise TeamPipelineError(f"unknown phase {src!r}")
    if dst not in allowed:
        raise TeamPipelineError(f"illegal transition {src!r} -> {dst!r}")


# ---------------------------------------------------------------------------
# Verify gate (POST-A2 parse_verdict_file only — never fake a verdict)
# ---------------------------------------------------------------------------


def parse_team_verify_verdict(root: Path | str, run_id: str) -> str:
    """Aggregate severity across ``team-verifier.md`` + ``.json`` siblings.

    FAILED > REQUEST_CHANGES > APPROVE. A run_id-less stray APPROVE next to a
    real REQUEST_CHANGES does **not** pass (POST-A2 ``parse_verdict_file``).
    Missing artifacts → UNKNOWN.
    """
    best: str | None = None
    best_rank = 0
    for path in team_verifier_artifact_paths(root, run_id):
        v = parse_verdict_file(path, expected_run_id=run_id)
        rank = _SEVERITY_RANK.get(v, 0)
        if rank > best_rank:
            best = v
            best_rank = rank
    return best if best is not None else "UNKNOWN"


def stage_verify_is_approve(root: Path | str, run_id: str) -> bool:
    """True only when CLI-stamped team-verify.json is APPROVE and not invalidated."""
    data = _read_stage_json(team_verify_stamp_path(root, run_id))
    if not data:
        return False
    if data.get("writer") != CLI_WRITER:
        return False
    if data.get("run_id") != run_id:
        return False
    if data.get("invalidated") is True:
        return False
    # Forged clean/true alone is never enough — require real verdict field.
    if data.get("verdict") != "APPROVE":
        return False
    if data.get("clean") is not True:
        return False
    return True


def invalidate_team_verify_stamp(
    root: Path | str,
    run_id: str,
    *,
    reason: str,
) -> None:
    """Mark prior verify APPROVE stale (mirror autopilot.invalidate_quality_stages)."""
    path = team_verify_stamp_path(root, run_id)
    data = _read_stage_json(path)
    if not data:
        return
    data["clean"] = False
    data["invalidated"] = True
    data["invalidated_reason"] = reason
    data["invalidated_at"] = _utc_now()
    data["writer"] = CLI_WRITER
    if data.get("verdict") == "APPROVE":
        # Keep historical verdict visible but stamp no longer authoritative.
        data["status"] = "invalidated"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_verify_stamp(
    root: Path,
    run_id: str,
    *,
    verdict: str,
    lease: Any,
) -> Path:
    lease.assert_current()
    path = team_verify_stamp_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = verdict == "APPROVE"
    payload = {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "verdict": verdict,
        "clean": clean,
        "invalidated": False,
        "status": "clean" if clean else "not_clean",
        "updated_at": _utc_now(),
        "execution_generation": getattr(lease, "generation", None),
        "note": (
            "team-verify gate stamp; durable APPROVE required for complete; "
            "never implies verified (use omg accept)"
        ),
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# State load / save
# ---------------------------------------------------------------------------


def _save(root: Path, run_id: str, state: dict[str, Any], lease: Any) -> None:
    lease.assert_current()
    path = team_pipeline_state_path(root, run_id)
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


def load_team_pipeline(root: Path | str, run_id: str) -> dict[str, Any]:
    path = team_pipeline_state_path(root, run_id)
    if not path.is_file():
        raise TeamPipelineError(f"team-pipeline state missing: {run_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TeamPipelineError("team-pipeline state must be a JSON object")
    if data.get("writer") != CLI_WRITER:
        raise TeamPipelineError("team-pipeline state lacks CLI writer")
    return data


def status_team_pipeline(root: Path | str, run_id: str) -> dict[str, Any]:
    state = load_team_pipeline(root, run_id)
    run = load_run(root, run_id) or {}
    phase = str(state.get("phase") or "init")
    return {
        "run_id": run_id,
        "phase": phase,
        "goal": state.get("goal"),
        "fix_round": int(state.get("fix_round") or 0),
        "max_fix": int(state.get("max_fix") or DEFAULT_MAX_FIX),
        "blocker": state.get("blocker"),
        "dry_run": bool(state.get("dry_run")),
        # Never trust pipeline-local verified; surface run truth only.
        "verified": bool(run.get("verified") is True),
        "run_status": run.get("status"),
        "legal_next": sorted(LEGAL_TRANSITIONS.get(phase, frozenset())),
        "history": list(state.get("history") or []),
        "note": state.get("note"),
    }


# ---------------------------------------------------------------------------
# Tasks helpers
# ---------------------------------------------------------------------------


def _parse_tasks(
    tasks_json: str | Sequence[Mapping[str, Any]] | None,
    *,
    tasks_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Load tasks from JSON string/list or a path (ralplan/leader artifact)."""
    if tasks_path is not None:
        p = Path(tasks_path)
        if not p.is_file():
            raise TeamPipelineError(f"tasks path not found: {p}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TeamPipelineError(f"tasks path unreadable: {exc}") from exc
        # Accept bare array or {"tasks": [...]}
        if isinstance(raw, dict) and "tasks" in raw:
            raw = raw["tasks"]
        if not isinstance(raw, list):
            raise TeamPipelineError("tasks path must be a JSON array or {tasks:[...]}")
        return [dict(x) for x in raw if isinstance(x, Mapping)]

    if tasks_json is None:
        raise TeamPipelineError("--tasks-json or --tasks-path required")

    if isinstance(tasks_json, str):
        try:
            raw = json.loads(tasks_json)
        except json.JSONDecodeError as exc:
            raise TeamPipelineError(f"--tasks-json is not valid JSON: {exc}") from exc
    else:
        raw = list(tasks_json)
    if not isinstance(raw, list):
        raise TeamPipelineError("--tasks-json must be a JSON array")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise TeamPipelineError("each task must be a JSON object")
        out.append(dict(item))
    if not out:
        raise TeamPipelineError("tasks list is empty")
    return out


def _augment_tasks_for_fix(
    tasks: list[dict[str, Any]],
    *,
    findings: str | None,
    fix_round: int,
) -> list[dict[str, Any]]:
    """Re-enter exec with fix findings as added task context (no new planner)."""
    note = f"[team-fix round {fix_round}]"
    if findings:
        note = f"{note} {findings}"
    out: list[dict[str, Any]] = []
    for t in tasks:
        nt = dict(t)
        prev = str(nt.get("context") or nt.get("prompt_extra") or "")
        extra = f"{prev}\n{note}".strip() if prev else note
        nt["context"] = extra
        out.append(nt)
    return out


# ---------------------------------------------------------------------------
# Transitions / driver
# ---------------------------------------------------------------------------


def transition(
    root: Path | str,
    run_id: str,
    next_phase: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Advance phase when legal; requires execution lease. Never sets verified."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    run_id = validate_identifier(run_id, label="run_id")
    if load_run(root, run_id) is None:
        raise TeamPipelineError(f"run not found: {run_id}")

    with execution_lease(root, run_id, intent=f"team-pipeline-{next_phase}") as lease:
        state = load_team_pipeline(root, run_id)
        src = str(state.get("phase") or "init")
        assert_legal_transition(src, next_phase)

        if next_phase == "complete":
            # Terminal complete requires a real, non-invalidated APPROVE stamp.
            if not stage_verify_is_approve(root, run_id):
                raise TeamPipelineError(
                    "complete requires durable team-verify APPROVE stamp "
                    "(forged clean/true or missing artifact refused)"
                )

        if next_phase in ("team-exec", "team-fix"):
            invalidate_team_verify_stamp(
                root,
                run_id,
                reason=f"(re)enter {next_phase} from {src}",
            )

        if next_phase == "team-fix" and src == "team-verify":
            # Round is incremented when *entering* fix from verify.
            state["fix_round"] = int(state.get("fix_round") or 0) + 1

        state["phase"] = next_phase
        state["history"] = list(state.get("history") or []) + [
            {
                "from": src,
                "phase": next_phase,
                "reason": reason,
                "at": _utc_now(),
            }
        ]
        # Strict-v2 run statuses: initialized|running|blocked|cancelled|verified.
        # Pipeline phase complete/failed live in team-pipeline.json — never set
        # verified here (omg accept only). Map terminals onto allowed statuses.
        if next_phase == "blocked":
            state["blocker"] = {"reason": reason or "blocked", "from": src}
            status = "blocked"
        elif next_phase == "failed":
            state["blocker"] = {"reason": reason or "failed", "from": src}
            status = "blocked"
        elif next_phase == "complete":
            state["blocker"] = None
            # Keep running: pipeline gate done; acceptance still open.
            status = "running"
        else:
            state["blocker"] = None
            status = "running"

        _save(root, run_id, state, lease)
        write_status(
            root,
            run_id,
            status,
            extra={
                "stage": "team-pipeline",
                "team_pipeline_phase": next_phase,
                "blocker": state.get("blocker"),
            },
            lease=lease,
        )
    return status_team_pipeline(root, run_id)


def start_team_pipeline(
    root: Path | str,
    goal: str,
    tasks: Sequence[Mapping[str, Any]],
    *,
    dry_run: bool = False,
    max_fix: int = DEFAULT_MAX_FIX,
    force: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create team-pipeline run at ``team-plan`` (CLI-stamped state + lease)."""
    root = Path(root).resolve()
    assert_safe_supervised_parent()
    goal = (goal or "").strip()
    if not goal:
        raise TeamPipelineError("goal text required")
    if not experimental_enabled():
        raise TeamGateError(f"team pipeline requires {EXPERIMENTAL_ENV}=1")
    if in_spawned_worker_context():
        raise TeamGateError(
            "refusing team pipeline inside a spawned-worker context "
            "(depth-1; nested team not allowed)"
        )
    task_list = [dict(t) for t in tasks if isinstance(t, Mapping)]
    if not task_list:
        raise TeamPipelineError("tasks list is empty")
    max_fix_i = int(max_fix)
    if max_fix_i < 0:
        raise TeamPipelineError("--max-fix must be >= 0")

    if run_id:
        if load_run(root, run_id) is None:
            raise TeamPipelineError(f"no run found for --run {run_id!r}")
        rid = validate_identifier(run_id, label="run_id")
    else:
        run = create_run(
            root,
            mode="ulw",
            goal=goal,
            force=force,
            extra={
                # Strict-v2 so execution_lease + write_status fencing apply
                # (same pattern as autopilot).
                "schema_version": 2,
                "lifecycle_version": 2,
                "team": True,
                "team_pipeline": True,
                "stage": "team-pipeline",
                "note": (
                    "staged team pipeline driver (plan→prd→exec→verify→fix); "
                    "sequences team plane; never sets verified"
                ),
            },
        )
        rid = str(run["run_id"])

    phase = "team-plan"
    with execution_lease(root, rid, intent="team-pipeline-start") as lease:
        state = {
            "writer": CLI_WRITER,
            "schema_version": SCHEMA_VERSION,
            "run_id": rid,
            "goal": goal,
            "phase": phase,
            "tasks": task_list,
            "fix_round": 0,
            "max_fix": max_fix_i,
            "dry_run": bool(dry_run),
            "history": [{"phase": phase, "at": _utc_now(), "event": "start"}],
            "blocker": None,
            "exec_meta": None,
            "last_verify_verdict": None,
            "findings": None,
            "created_at": _utc_now(),
            "note": (
                "THIN staged driver over omg team plane; decomposition is the "
                "leader's / ralplan's job (consumed via --tasks-json); "
                "verify gates durable artifacts via parse_verdict_file; "
                "verified only via omg accept"
            ),
        }
        _save(root, rid, state, lease)
        write_status(
            root,
            rid,
            "running",
            extra={
                "stage": "team-pipeline",
                "team_pipeline_phase": phase,
            },
            lease=lease,
        )
    return status_team_pipeline(root, rid)


def _plane_write_status_compat(
    root: Path | str,
    run_id: str,
    status: str,
    *,
    extra: dict[str, Any] | None = None,
    lease: Any | None = None,
) -> dict[str, Any]:
    """Adapt plane write_status onto strict-v2 runs used by the pipeline.

    ``start_team(..., dry_run=True)`` historically writes status ``completed``
    (legacy v1). Strict-v2 only allows initialized/running/blocked/cancelled/
    verified, and requires an execution lease. Map terminals and take a lease
    when the plane omits one — without reimplementing the plane.
    """
    from omg_cli.state import write_status as _ws

    mapped = status
    if status == "completed":
        mapped = "running"
    elif status == "failed":
        mapped = "blocked"
    if lease is not None:
        return _ws(root, run_id, mapped, extra=extra, lease=lease)
    with execution_lease(Path(root), run_id, intent="team-plane-status") as owned:
        return _ws(root, run_id, mapped, extra=extra, lease=owned)


def _run_exec_stage(
    root: Path,
    run_id: str,
    state: dict[str, Any],
    *,
    dry_run: bool,
    yolo: bool,
    safe: bool,
    routing: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """team-exec body: start_team (+ collect when not dry-run). Never verified."""
    import omg_cli.team.plane as plane_mod

    tasks = list(state.get("tasks") or [])
    findings = state.get("findings")
    fix_round = int(state.get("fix_round") or 0)
    if fix_round > 0:
        tasks = _augment_tasks_for_fix(
            tasks, findings=str(findings) if findings else None, fix_round=fix_round
        )
    goal = str(state.get("goal") or "")
    # Invalidate before launching work so a prior APPROVE cannot be reused.
    invalidate_team_verify_stamp(
        root, run_id, reason=f"team-exec entry (fix_round={fix_round})"
    )

    # Plane was written for legacy-v1 dry-run status tokens; bridge for v2.
    prev_ws = plane_mod.write_status
    plane_mod.write_status = _plane_write_status_compat  # type: ignore[assignment]
    try:
        meta = start_team(
            goal,
            tasks,
            root=root,
            run_id=run_id,
            dry_run=bool(dry_run),
            yolo=yolo,
            safe=safe,
            force=False,
            routing=routing,
        )
        collect_result: dict[str, Any] | None = None
        if not dry_run:
            collect_result = collect_team(root, run_id)
            # Defensive: collect never sets verified (plane contract).
            _ = collect_result.get("verified")
    finally:
        plane_mod.write_status = prev_ws  # type: ignore[assignment]

    return {
        "start": {
            "run_id": meta.get("run_id"),
            "dry_run": meta.get("dry_run"),
            "task_count": len(meta.get("tasks") or []),
            "writer": meta.get("writer"),
        },
        "collect": collect_result,
    }


def _run_verify_stage(root: Path, run_id: str, lease: Any) -> str:
    """Parse durable verifier artifact; write stamp; return verdict token."""
    verdict = parse_team_verify_verdict(root, run_id)
    _write_verify_stamp(root, run_id, verdict=verdict, lease=lease)
    return verdict


def run_team_pipeline(
    goal: str,
    *,
    root: Path | str | None = None,
    tasks_json: str | Sequence[Mapping[str, Any]] | None = None,
    tasks_path: Path | str | None = None,
    dry_run: bool = False,
    max_fix: int = DEFAULT_MAX_FIX,
    force: bool = False,
    run_id: str | None = None,
    yolo: bool = False,
    safe: bool = False,
    routing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive the staged FSM to a terminal phase. Never sets verified/passes.

    Returns status_team_pipeline dict (includes phase, fix_round, verified=False
    unless an independent ``omg accept`` already verified the run).
    """
    root_path = Path(root) if root is not None else Path.cwd()
    root_path = root_path.resolve()
    assert_safe_supervised_parent()

    if not experimental_enabled():
        raise TeamGateError(f"team pipeline requires {EXPERIMENTAL_ENV}=1")
    if in_spawned_worker_context():
        raise TeamGateError(
            "refusing team pipeline inside a spawned-worker context"
        )

    tasks = _parse_tasks(tasks_json, tasks_path=tasks_path)
    st = start_team_pipeline(
        root_path,
        goal,
        tasks,
        dry_run=dry_run,
        max_fix=max_fix,
        force=force,
        run_id=run_id,
    )
    rid = str(st["run_id"])
    max_fix_i = int(max_fix)

    # ---- team-plan (pass-through marker; tasks already recorded) ----
    transition(root_path, rid, "team-prd", reason="plan recorded (leader decomposition)")

    # ---- team-prd (pass-through; no planner) ----
    transition(root_path, rid, "team-exec", reason="prd marker (no new planner)")

    # Main exec → verify → (fix → exec)* loop
    while True:
        state = load_team_pipeline(root_path, rid)
        phase = str(state.get("phase") or "")

        if phase in TERMINAL:
            return status_team_pipeline(root_path, rid)

        if phase == "team-exec":
            try:
                exec_meta = _run_exec_stage(
                    root_path,
                    rid,
                    state,
                    dry_run=bool(state.get("dry_run") or dry_run),
                    yolo=yolo,
                    safe=safe,
                    routing=routing,
                )
            except (TeamError, TeamGateError) as exc:
                transition(
                    root_path,
                    rid,
                    "failed",
                    reason=f"team-exec failed: {exc}",
                )
                return status_team_pipeline(root_path, rid)
            with execution_lease(root_path, rid, intent="team-pipeline-exec-meta") as lease:
                state = load_team_pipeline(root_path, rid)
                state["exec_meta"] = exec_meta
                _save(root_path, rid, state, lease)
            transition(root_path, rid, "team-verify", reason="exec collected (or dry-run start)")
            continue

        if phase == "team-verify":
            with execution_lease(root_path, rid, intent="team-pipeline-verify") as lease:
                verdict = _run_verify_stage(root_path, rid, lease)
                state = load_team_pipeline(root_path, rid)
                state["last_verify_verdict"] = verdict
                if verdict != "APPROVE":
                    state["findings"] = (
                        f"team-verify verdict={verdict}; "
                        "REQUEST_CHANGES/FAILED/UNKNOWN require fix"
                    )
                _save(root_path, rid, state, lease)

            if verdict == "APPROVE" and stage_verify_is_approve(root_path, rid):
                transition(
                    root_path,
                    rid,
                    "complete",
                    reason="team-verify APPROVE (durable artifact)",
                )
                return status_team_pipeline(root_path, rid)

            # Not approve → fix (or fail if budget exhausted at fix entry)
            transition(
                root_path,
                rid,
                "team-fix",
                reason=f"verify={verdict}",
            )
            continue

        if phase == "team-fix":
            state = load_team_pipeline(root_path, rid)
            fix_round = int(state.get("fix_round") or 0)
            # fix_round already incremented on transition into team-fix
            if fix_round > max_fix_i:
                transition(
                    root_path,
                    rid,
                    "failed",
                    reason=f"max-fix exceeded ({fix_round}>{max_fix_i})",
                )
                return status_team_pipeline(root_path, rid)
            transition(
                root_path,
                rid,
                "team-exec",
                reason=f"fix round {fix_round} re-enter exec",
            )
            continue

        # Unexpected phase
        transition(
            root_path,
            rid,
            "blocked",
            reason=f"unexpected phase {phase!r}",
        )
        return status_team_pipeline(root_path, rid)


__all__ = [
    "DEFAULT_MAX_FIX",
    "LEGAL_TRANSITIONS",
    "STAGES",
    "TERMINAL",
    "TeamPipelineError",
    "assert_legal_transition",
    "invalidate_team_verify_stamp",
    "load_team_pipeline",
    "parse_team_verify_verdict",
    "run_team_pipeline",
    "stage_verify_is_approve",
    "start_team_pipeline",
    "status_team_pipeline",
    "team_pipeline_state_path",
    "team_verifier_artifact_paths",
    "team_verify_stamp_path",
    "transition",
]
