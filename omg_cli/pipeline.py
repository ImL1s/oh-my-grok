"""AUTO_PILOT-like pipeline FSM: plan → implement → integrate → dual_review → accept → report.

Grok-native workers only. Never sets OMG_ALLOW_EXTERNAL_CLI.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from omg_cli.evidence import assert_safe_supervised_parent
from omg_cli.integrate import default_envelopes_dir
from omg_cli.modes import resolve_launch_timeout
from omg_cli.state import create_run, load_run, write_status

DEFAULT_MAX_PLAN_ROUNDS = 3
DEFAULT_MAX_ITER = 3
DEFAULT_MAX_DUAL_REVIEW_ROUNDS = 2
CLI_WRITER = "omg-cli"

# Stage order for resume and report
STAGE_ORDER = ["plan", "implement", "integrate", "dual_review", "accept"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / ".omg" / "state" / "runs" / run_id


def pipeline_state_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "pipeline.json"


def report_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "report.json"


def load_pipeline_state(root: Path, run_id: str) -> dict[str, Any] | None:
    path = pipeline_state_path(root, run_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_pipeline_state(root: Path, run_id: str, state: dict[str, Any]) -> Path:
    path = pipeline_state_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updated_at"] = _utc_now()
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def write_pipeline_report(
    root: Path,
    run_id: str,
    state: dict[str, Any],
    *,
    integrate_status: str | None = None,
    exit_code: int | None = None,
) -> Path:
    """Write ``runs/<id>/report.json`` with stages history and verified flag.

    Always ``writer: omg-cli``. Called on success and terminal failure paths.
    """
    root = Path(root)
    run = load_run(root, run_id) or {}
    verified = bool(run.get("verified") is True)
    integ = integrate_status
    if integ is None:
        integ = state.get("integrate_status")
    if integ is None:
        integ = run.get("integrate_status")

    report: dict[str, Any] = {
        "version": 1,
        "writer": CLI_WRITER,
        "run_id": run_id,
        "goal": state.get("goal") or run.get("goal"),
        "status": state.get("status") or run.get("status"),
        "stage": state.get("stage"),
        "implement": state.get("implement"),
        "stages": list(state.get("history") or []),
        "integrate_status": integ,
        "verified": verified,
        "exit_code": exit_code,
        "dual_review": bool(state.get("dual_review")),
        "plan_accepted": bool(state.get("plan_accepted")),
        "created_at": state.get("created_at"),
        "finished_at": _utc_now(),
        "note": (
            "pipeline report; Grok-native workers only; "
            "never sets OMG_ALLOW_EXTERNAL_CLI"
        ),
    }
    path = report_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def initial_pipeline_state(
    *,
    run_id: str,
    goal: str,
    implement: str = "ralph",
    max_plan_rounds: int = DEFAULT_MAX_PLAN_ROUNDS,
    max_iter: int = DEFAULT_MAX_ITER,
    dual_review: bool = True,
    max_dual_review_rounds: int = DEFAULT_MAX_DUAL_REVIEW_ROUNDS,
    skip_plan: bool = False,
    plan_only: bool = False,
    require_acceptance: bool = True,
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "version": 1,
        "run_id": run_id,
        "goal": goal,
        "status": "running",
        "stage": "initialized",
        "implement": implement,
        "max_plan_rounds": int(max_plan_rounds),
        "max_iter": int(max_iter),
        "dual_review": bool(dual_review),
        "dual_review_round": 0,
        "max_dual_review_rounds": int(max_dual_review_rounds),
        "plan_accepted": False,
        "plan_artifact": None,
        "skip_plan": bool(skip_plan),
        "plan_only": bool(plan_only),
        "require_acceptance": bool(require_acceptance),
        "integrate_status": None,
        "history": [],
        "note": (
            "CLI-owned pipeline FSM; Grok-native workers only; "
            "never sets OMG_ALLOW_EXTERNAL_CLI"
        ),
        "created_at": now,
        "updated_at": now,
    }


def _history(
    state: dict[str, Any],
    stage: str,
    event: str,
    detail: str = "",
) -> None:
    state.setdefault("history", []).append(
        {
            "ts": _utc_now(),
            "stage": stage,
            "event": event,
            "detail": detail,
        }
    )


def _assert_no_allow_env() -> None:
    """Hard guard: pipeline path must never export OMG_ALLOW_EXTERNAL_CLI."""
    assert_safe_supervised_parent(os.environ)


def _envelopes_exist(root: Path, run_id: str) -> bool:
    env_dir = default_envelopes_dir(Path(root), run_id)
    if not env_dir.is_dir():
        return False
    return any(env_dir.glob("*.json"))


def _should_integrate(implement: str, root: Path, run_id: str) -> bool:
    """Integrate after implement when ULW mode or ULW envelopes are present."""
    if (implement or "").strip().lower() == "ulw":
        return True
    return _envelopes_exist(root, run_id)


def _envelope_heads(root: Path, run_id: str) -> list[str]:
    """Return head_sha values from current ULW envelope JSON files."""
    env_dir = default_envelopes_dir(Path(root), run_id)
    if not env_dir.is_dir():
        return []
    heads: list[str] = []
    for path in sorted(env_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        h = data.get("head_sha")
        if isinstance(h, str) and h.strip():
            heads.append(h.strip().lower())
    return heads


def _integrate_stale(state: dict[str, Any], root: Path, run_id: str) -> bool:
    """True when envelopes exist and their heads are not all in last_integrated_heads.

    Used on resume so we never skip integrate after re-implement/reseal (AC4).
    """
    current = _envelope_heads(root, run_id)
    if not current:
        return False
    last = state.get("last_integrated_heads") or []
    if not isinstance(last, list):
        last = []
    last_norm = {str(x).strip().lower() for x in last if x}
    return any(h not in last_norm for h in current)


def _execute_integrate_stage(
    *,
    root_path: Path,
    run_id: str,
    state: dict[str, Any],
    implement: str,
    dry_run: bool,
    do_integrate: Callable[[], dict[str, Any]],
    finish: Callable[[int], int],
    reason: str = "",
) -> int | None:
    """Run integrate stage. Returns exit code if pipeline must stop, else None.

    Call after every implement (including re-implement after dual_review
    REQUEST_CHANGES) so resealed ULW envelopes are never skipped (AC4).
    """
    if not _should_integrate(implement, root_path, run_id):
        return None

    detail = f"implement={implement}"
    if reason:
        detail = f"{detail}; {reason}"
    state["stage"] = "integrate"
    _history(state, "integrate", "enter", detail=detail)
    save_pipeline_state(root_path, run_id, state)
    write_status(root_path, run_id, "running", extra={"stage": "integrate"})
    if dry_run:
        print(f"omg pipeline dry-run: stage=integrate reason={reason or 'initial'}")

    try:
        integ = do_integrate()
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        integ = {"status": "failed", "error": str(exc)}

    integ_status = str(integ.get("status") or "failed")
    state["integrate_status"] = integ_status
    # Track last integrated head(s) for diagnostics / tests
    applied = integ.get("applied") if isinstance(integ, dict) else None
    if isinstance(applied, list):
        heads = [
            a.get("head_sha") or a.get("pick")
            for a in applied
            if isinstance(a, dict) and a.get("status") in ("applied", "dry_run_ok", "ok")
        ]
        if heads:
            state["last_integrated_heads"] = heads
    _history(
        state,
        "integrate",
        "exit",
        detail=f"status={integ_status}" + (f"; {reason}" if reason else ""),
    )
    save_pipeline_state(root_path, run_id, state)

    if integ_status == "failed":
        state["status"] = "failed"
        state["stage"] = "failed"
        save_pipeline_state(root_path, run_id, state)
        write_status(
            root_path,
            run_id,
            "failed",
            extra={
                "stage": "integrate",
                "integrate_status": "failed",
                "integrate_error": integ.get("error"),
            },
        )
        print(
            f"omg pipeline: integrate failed run={run_id}: {integ.get('error')}",
            file=sys.stderr,
        )
        return finish(1)

    if integ_status == "missing":
        if implement == "ulw" and not dry_run:
            state["status"] = "failed"
            state["stage"] = "failed"
            save_pipeline_state(root_path, run_id, state)
            write_status(
                root_path,
                run_id,
                "failed",
                extra={
                    "stage": "integrate",
                    "integrate_status": "missing",
                    "note": (
                        "ULW expected envelopes under "
                        f".omg/artifacts/ulw-results/{run_id}/"
                    ),
                },
            )
            print(
                f"omg pipeline: ULW missing envelopes run={run_id}",
                file=sys.stderr,
            )
            return finish(1)
        _history(
            state,
            "integrate",
            "note",
            detail="missing envelopes; continuing (ralph or dry_run)",
        )
        save_pipeline_state(root_path, run_id, state)

    return None


def run_pipeline(
    goal: str,
    *,
    root: Path | str | None = None,
    implement: str = "ralph",
    max_plan_rounds: int = DEFAULT_MAX_PLAN_ROUNDS,
    max_iter: int = DEFAULT_MAX_ITER,
    skip_plan: bool = False,
    plan_only: bool = False,
    dual_review: bool = True,
    max_dual_review_rounds: int = DEFAULT_MAX_DUAL_REVIEW_ROUNDS,
    require_acceptance: bool | None = True,
    yolo: bool = False,
    safe: bool = False,
    dry_run: bool = False,
    timeout: float | None = None,
    resume_run_id: str | None = None,
    force: bool = False,
    require_squash: bool = False,
    # Test hooks
    plan_fn: Callable[..., int] | None = None,
    implement_fn: Callable[..., int] | None = None,
    integrate_fn: Callable[..., dict[str, Any]] | None = None,
    dual_review_fn: Callable[..., str] | None = None,
    accept_fn: Callable[..., bool] | None = None,
) -> int:
    """Run pipeline FSM. Returns exit code (0 verified/plan-only accepted/etc).

    Stage order: plan → implement → integrate → dual_review → accept → report.

    Never sets OMG_ALLOW_EXTERNAL_CLI. Never product-executes external advisors.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    goal = (goal or "").strip() or "(no goal)"
    implement = (implement or "ralph").strip().lower()
    if implement not in ("ralph", "ulw"):
        print(
            f"omg pipeline: implement must be ralph|ulw, got {implement!r}",
            file=sys.stderr,
        )
        return 2
    if skip_plan and plan_only:
        print(
            "omg pipeline: --skip-plan and --plan-only are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    if require_acceptance is None:
        require_acceptance = True

    launch_timeout = resolve_launch_timeout(timeout, dry_run=dry_run)
    _assert_no_allow_env()

    # Resume or create
    if resume_run_id:
        state = load_pipeline_state(root_path, resume_run_id)
        if state is None:
            print(
                f"omg pipeline: no pipeline.json for run {resume_run_id}",
                file=sys.stderr,
            )
            return 1
        run_id = resume_run_id
        if load_run(root_path, run_id) is None:
            print(f"omg pipeline: no run {run_id}", file=sys.stderr)
            return 1
        write_status(
            root_path,
            run_id,
            "running",
            extra={"stage": state.get("stage"), "pipeline_resume": True},
        )
    else:
        try:
            run = create_run(
                root_path,
                mode="pipeline",
                goal=goal,
                extra={
                    "implement": implement,
                    "max_plan_rounds": max_plan_rounds,
                    "max_iter": max_iter,
                    "dual_review": dual_review,
                    "note": "pipeline FSM; Grok-native only",
                },
                force=force,
            )
        except RuntimeError as exc:
            print(f"omg pipeline: {exc}", file=sys.stderr)
            return 1
        run_id = run["run_id"]
        state = initial_pipeline_state(
            run_id=run_id,
            goal=goal,
            implement=implement,
            max_plan_rounds=max_plan_rounds,
            max_iter=max_iter,
            dual_review=dual_review,
            max_dual_review_rounds=max_dual_review_rounds,
            skip_plan=skip_plan,
            plan_only=plan_only,
            require_acceptance=bool(require_acceptance),
        )
        save_pipeline_state(root_path, run_id, state)
        write_status(
            root_path, run_id, "running", extra={"stage": "initialized"}
        )

    # Import stage modules lazily
    from omg_cli.dual_review import run_dual_review
    from omg_cli.integrate import integrate_results
    from omg_cli.modes import run_mode
    from omg_cli.ralplan import load_ralplan_state, run_ralplan

    def _default_plan(**kw: Any) -> int:
        return run_ralplan(
            goal,
            root=root_path,
            max_rounds=int(state["max_plan_rounds"]),
            yolo=yolo,
            safe=safe,
            dry_run=dry_run,
            timeout=launch_timeout,
            existing_run_id=run_id,
            force=False,
        )

    def _default_implement(**kw: Any) -> int:
        return run_mode(
            implement,
            goal,
            yolo=yolo,
            safe=safe,
            root=root_path,
            max_iter=int(state["max_iter"]),
            dry_run=dry_run,
            timeout=launch_timeout,
            require_acceptance=False,  # pipeline owns accept stage
            existing_run_id=run_id,
            force=False,
        )

    def _default_integrate(**kw: Any) -> dict[str, Any]:
        return integrate_results(
            root_path,
            run_id,
            dry_run=dry_run,
            require_squash=require_squash,
        )

    def _default_dual(**kw: Any) -> str:
        return run_dual_review(
            goal,
            root=root_path,
            run_id=run_id,
            round_n=int(state.get("dual_review_round", 0)) + 1,
            dry_run=dry_run,
            timeout=launch_timeout,
            yolo=yolo,
            safe=safe,
            create_if_missing=False,
        )

    def _default_accept(**kw: Any) -> bool:
        from omg_cli.acceptance import (
            freeze_and_run,
            load_prd,
            prd_has_acceptance_commands,
        )
        from omg_cli.state import set_verified

        prd = load_prd(root_path, run_id)
        if prd is None or not prd_has_acceptance_commands(prd):
            return False
        ok = freeze_and_run(root_path, run_id, prd, dry_run=dry_run)
        if dry_run:
            return False
        if not ok:
            return False
        try:
            set_verified(root_path, run_id, force=False)
            return True
        except (PermissionError, FileNotFoundError):
            return False

    do_plan = plan_fn or _default_plan
    do_implement = implement_fn or _default_implement
    do_integrate = integrate_fn or _default_integrate
    do_dual = dual_review_fn or _default_dual
    do_accept = accept_fn or _default_accept

    def _finish(exit_code: int) -> int:
        """Write report.json then return exit_code."""
        try:
            write_pipeline_report(
                root_path,
                run_id,
                state,
                integrate_status=state.get("integrate_status"),
                exit_code=exit_code,
            )
        except OSError as exc:
            print(f"omg pipeline: report write failed: {exc}", file=sys.stderr)
        return exit_code

    # Determine start stage on resume
    start_stage = str(state.get("stage") or "initialized")
    stages_done = {h.get("stage") for h in state.get("history", []) if h.get("event") == "exit"}

    def _should_run(stage_name: str) -> bool:
        if resume_run_id is None:
            return True
        # AC4: if ULW envelopes changed after last integrate, force re-integrate
        # even when history already has an integrate exit (resume after re-implement).
        if stage_name == "integrate" and _integrate_stale(state, root_path, run_id):
            return True
        # Resume: skip stages already exited successfully
        if stage_name in stages_done and start_stage not in (stage_name, "initialized"):
            # If current stage is this one and not exited, re-run
            if start_stage == stage_name:
                return True
            return False
        # Order: only run from current stage forward
        if start_stage in ("initialized", "running"):
            return True
        if start_stage not in STAGE_ORDER:
            return True
        return STAGE_ORDER.index(stage_name) >= STAGE_ORDER.index(start_stage)

    # --- plan ---
    plan_ok = bool(state.get("plan_accepted"))
    if not state.get("skip_plan") and _should_run("plan") and not plan_ok:
        state["stage"] = "plan"
        _history(state, "plan", "enter")
        save_pipeline_state(root_path, run_id, state)
        write_status(root_path, run_id, "running", extra={"stage": "plan"})

        if dry_run:
            print(f"omg pipeline dry-run: stage=plan run={run_id}")

        rc = int(do_plan())
        # Check ralplan state if embedded
        rp = load_ralplan_state(root_path, run_id)
        if rp is not None:
            plan_ok = bool(rp.get("accepted"))
        else:
            plan_ok = rc == 0

        # dry_run ralplan without APPROVE fails — for pipeline dry_run treat
        # plan stage as recorded-only success so implement order is testable.
        if dry_run and not plan_ok:
            plan_ok = True
            _history(
                state,
                "plan",
                "exit",
                detail="dry_run: plan stage recorded (verifier APPROVE not required)",
            )
        else:
            _history(
                state,
                "plan",
                "exit",
                detail=f"rc={rc} accepted={plan_ok}",
            )

        state["plan_accepted"] = plan_ok
        if rp is not None:
            state["plan_artifact"] = "stages/"
        save_pipeline_state(root_path, run_id, state)

        if not plan_ok:
            state["status"] = "failed"
            state["stage"] = "failed"
            save_pipeline_state(root_path, run_id, state)
            write_status(
                root_path,
                run_id,
                "failed",
                extra={"stage": "plan", "note": "plan not accepted"},
            )
            print(f"omg pipeline: plan failed run={run_id}", file=sys.stderr)
            return _finish(1)

        if state.get("plan_only"):
            state["status"] = "completed"
            state["stage"] = "plan"
            save_pipeline_state(root_path, run_id, state)
            write_status(
                root_path,
                run_id,
                "completed",
                extra={
                    "stage": "plan",
                    "plan_only": True,
                    "note": "plan-only pipeline; not product verified",
                },
            )
            print(f"omg pipeline: plan-only complete run={run_id}")
            return _finish(0)
    elif state.get("skip_plan"):
        plan_ok = True
        state["plan_accepted"] = True
        _history(state, "plan", "skip", detail="--skip-plan")
        save_pipeline_state(root_path, run_id, state)

    # --- implement ---
    if _should_run("implement"):
        state["stage"] = "implement"
        _history(state, "implement", "enter", detail=implement)
        save_pipeline_state(root_path, run_id, state)
        write_status(
            root_path, run_id, "running", extra={"stage": "implement", "implement": implement}
        )
        if dry_run:
            print(f"omg pipeline dry-run: stage=implement mode={implement}")

        rc_impl = int(do_implement())
        _history(state, "implement", "exit", detail=f"rc={rc_impl}")
        save_pipeline_state(root_path, run_id, state)

        if rc_impl != 0 and not dry_run:
            state["status"] = "failed"
            state["stage"] = "failed"
            save_pipeline_state(root_path, run_id, state)
            write_status(
                root_path,
                run_id,
                "failed",
                extra={"stage": "implement", "exit_code": rc_impl},
            )
            return _finish(rc_impl)

    # --- integrate (ULW mode or envelopes present) ---
    # Also re-run after every re-implement (see dual_review loop below).
    if _should_run("integrate"):
        stop = _execute_integrate_stage(
            root_path=root_path,
            run_id=run_id,
            state=state,
            implement=implement,
            dry_run=dry_run,
            do_integrate=do_integrate,
            finish=_finish,
            reason="after-implement",
        )
        if stop is not None:
            return stop

    # --- dual_review (optional loop) ---
    if state.get("dual_review") and _should_run("dual_review"):
        max_dr = int(state.get("max_dual_review_rounds") or 1)
        for dr_i in range(1, max_dr + 1):
            state["stage"] = "dual_review"
            state["dual_review_round"] = dr_i
            _history(state, "dual_review", "enter", detail=f"round={dr_i}")
            save_pipeline_state(root_path, run_id, state)
            write_status(
                root_path,
                run_id,
                "running",
                extra={"stage": "dual_review", "dual_review_round": dr_i},
            )
            if dry_run:
                print(f"omg pipeline dry-run: stage=dual_review round={dr_i}")

            try:
                verdict = str(do_dual())
            except RuntimeError as exc:
                # OMG_DUAL_REVIEW_REQUIRE_NATIVE=1 etc. — fail closed, exit 2
                print(f"omg pipeline: dual_review gate: {exc}", file=sys.stderr)
                state["status"] = "failed"
                state["stage"] = "failed"
                _history(
                    state,
                    "dual_review",
                    "exit",
                    detail=f"gate_error={exc}",
                )
                save_pipeline_state(root_path, run_id, state)
                write_status(
                    root_path,
                    run_id,
                    "failed",
                    extra={"stage": "dual_review", "error": str(exc)},
                )
                return _finish(2)
            _history(
                state,
                "dual_review",
                "exit",
                detail=f"verdict={verdict}",
            )
            save_pipeline_state(root_path, run_id, state)

            if verdict == "APPROVE":
                break
            # dry_run stubs omit APPROVE (NEEDS_REVIEW) — continue FSM like plan dry_run.
            if dry_run:
                break
            if verdict == "FAILED":
                state["status"] = "failed"
                state["stage"] = "failed"
                save_pipeline_state(root_path, run_id, state)
                write_status(
                    root_path,
                    run_id,
                    "failed",
                    extra={"stage": "dual_review", "verdict": verdict},
                )
                return _finish(1)
            # REQUEST_CHANGES / UNKNOWN → re-implement if budget remains
            if dr_i >= max_dr:
                state["status"] = "failed"
                state["stage"] = "failed"
                save_pipeline_state(root_path, run_id, state)
                write_status(
                    root_path,
                    run_id,
                    "failed",
                    extra={
                        "stage": "dual_review",
                        "verdict": verdict,
                        "note": "dual_review rounds exhausted",
                    },
                )
                return _finish(1)
            # re-implement then RE-INTEGRATE before next dual_review (AC4)
            state["stage"] = "implement"
            _history(
                state,
                "implement",
                "enter",
                detail="re-implement after REQUEST_CHANGES",
            )
            save_pipeline_state(root_path, run_id, state)
            write_status(
                root_path, run_id, "running", extra={"stage": "implement"}
            )
            rc_impl = int(do_implement())
            _history(state, "implement", "exit", detail=f"rc={rc_impl}")
            if rc_impl != 0 and not dry_run:
                state["status"] = "failed"
                state["stage"] = "failed"
                save_pipeline_state(root_path, run_id, state)
                write_status(
                    root_path, run_id, "failed", extra={"stage": "implement"}
                )
                return _finish(rc_impl)
            stop = _execute_integrate_stage(
                root_path=root_path,
                run_id=run_id,
                state=state,
                implement=implement,
                dry_run=dry_run,
                do_integrate=do_integrate,
                finish=_finish,
                reason="after-re-implement",
            )
            if stop is not None:
                return stop

    # --- accept ---
    if _should_run("accept"):
        state["stage"] = "accept"
        _history(state, "accept", "enter")
        save_pipeline_state(root_path, run_id, state)
        write_status(root_path, run_id, "verifying", extra={"stage": "accept"})
        if dry_run:
            print("omg pipeline dry-run: stage=accept")

        verified = bool(do_accept())
        _history(state, "accept", "exit", detail=f"verified={verified}")

        current = load_run(root_path, run_id) or {}
        if verified or current.get("verified") is True:
            state["status"] = "verified"
            state["stage"] = "verified"
            save_pipeline_state(root_path, run_id, state)
            # set_verified already wrote status
            print(f"omg pipeline: verified run={run_id}")
            return _finish(0)

        # No acceptance commands or failed
        if state.get("require_acceptance"):
            state["status"] = "completed"
            state["stage"] = "accept"
            save_pipeline_state(root_path, run_id, state)
            write_status(
                root_path,
                run_id,
                "completed",
                extra={
                    "stage": "accept",
                    "note": "completed without verified; require_acceptance",
                    "require_acceptance": True,
                },
            )
            print(
                f"omg pipeline: not verified run={run_id} (require_acceptance)",
                file=sys.stderr,
            )
            return _finish(1 if not dry_run else 0)

        state["status"] = "completed"
        state["stage"] = "completed"
        save_pipeline_state(root_path, run_id, state)
        write_status(
            root_path,
            run_id,
            "completed",
            extra={"stage": "completed", "require_acceptance": False},
        )
        return _finish(0)

    save_pipeline_state(root_path, run_id, state)
    return _finish(0)


__all__ = [
    "DEFAULT_MAX_DUAL_REVIEW_ROUNDS",
    "DEFAULT_MAX_ITER",
    "DEFAULT_MAX_PLAN_ROUNDS",
    "STAGE_ORDER",
    "initial_pipeline_state",
    "load_pipeline_state",
    "pipeline_state_path",
    "report_path",
    "run_pipeline",
    "save_pipeline_state",
    "write_pipeline_report",
]
