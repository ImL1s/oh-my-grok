"""Modes-family CLI handlers (#29 Phase 2).

Commands: ulw/ralph/ralplan (via cmd_mode), review, qa, autopilot, ask,
pipeline, dual-review.
Parser construction remains in ``main.build_parser``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from omg_cli.cli_util import project_root


def cmd_mode(args: argparse.Namespace) -> int:
    """Launch ulw / ralph / ralplan via omg_cli.modes.run_mode."""
    from omg_cli.modes import DEFAULT_MAX_ITER, run_mode

    mode = args.command
    goal = " ".join(args.goal or []).strip()
    resume = getattr(args, "resume", None)
    if not goal and not (mode == "ralph" and resume is not None):
        print(f"omg {mode}: goal text required", file=sys.stderr)
        return 2

    max_iter = getattr(args, "max_iter", None)
    if max_iter is None and resume is None:
        max_iter = DEFAULT_MAX_ITER.get(mode, 1)

    require_acceptance = getattr(args, "require_acceptance", None)
    # argparse store_true/store_false with default None via mutually exclusive
    if require_acceptance is None and hasattr(args, "no_require_acceptance"):
        if getattr(args, "no_require_acceptance", False):
            require_acceptance = False

    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        timeout = float(timeout)

    fanout = getattr(args, "fanout", None) or "skill"
    workers = getattr(args, "workers", None)
    if fanout == "process":
        if mode != "ulw":
            print(
                f"omg {mode}: --fanout process is only supported for ulw",
                file=sys.stderr,
            )
            return 2
        # Experimental opt-in only — default isolation story is spawn_subagent.
        if os.environ.get("OMG_EXPERIMENTAL_PROCESS_FANOUT", "").strip() != "1":
            print(
                "omg ulw: --fanout process is experimental and disabled by default.\n"
                "  Set OMG_EXPERIMENTAL_PROCESS_FANOUT=1 to opt in.\n"
                "  Preferred isolation path: default --fanout skill (spawn_subagent).\n"
                "  See README / docs/security-model.md.",
                file=sys.stderr,
            )
            return 2
        from omg_cli.fanout import run_process_fanout

        # require_acceptance: None → False for process fanout unless explicitly set
        ra = require_acceptance if require_acceptance is not None else False
        return run_process_fanout(
            goal,
            workers=workers,
            root=project_root(),
            yolo=bool(getattr(args, "yolo", False)),
            safe=bool(getattr(args, "safe", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            timeout=timeout,
            require_acceptance=bool(ra),
            force=bool(getattr(args, "force", False)),
        )

    existing_run_id = None
    if mode == "ralplan":
        existing_run_id = getattr(args, "run_id", None)

    return run_mode(
        mode,
        goal,
        yolo=bool(getattr(args, "yolo", False)),
        safe=bool(getattr(args, "safe", False)),
        root=project_root(),
        max_iter=int(max_iter) if max_iter is not None else None,
        dry_run=bool(getattr(args, "dry_run", False)),
        timeout=timeout,
        require_acceptance=require_acceptance,
        resume_run_id=resume,
        existing_run_id=existing_run_id,
    )


def cmd_review(args: argparse.Namespace) -> int:
    """Hash-bound structured review gate (code-reviewer + architect)."""
    from omg_cli.review import ReviewError, run_structured_review

    root = project_root()
    try:
        cr = json.loads(args.code_reviewer_json)
        ar = json.loads(args.architect_json)
        result = run_structured_review(
            root,
            args.run_id,
            diff_text=args.diff_text or "",
            code_reviewer_payload=cr,
            architect_payload=ar,
        )
    except (ReviewError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"omg review: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("clean") else 1


def cmd_qa(args: argparse.Namespace) -> int:
    """Bounded UltraQA freeze / cycle / status."""
    from omg_cli.qa import QAError, freeze_scenarios, qa_status, run_qa_cycle

    root = project_root()
    action = getattr(args, "qa_action", None)
    try:
        if action == "freeze":
            scenarios = json.loads(args.scenarios_json)
            result = freeze_scenarios(
                root,
                args.run_id,
                scenarios,
                plan_hash=getattr(args, "plan_hash", None),
                spec_hash=getattr(args, "spec_hash", None),
            )
        elif action == "run":
            result = run_qa_cycle(
                root,
                args.run_id,
                repair_classification=getattr(args, "repair_classification", None),
            )
        elif action == "status":
            result = qa_status(root, args.run_id)
        else:
            print("omg qa: action required", file=sys.stderr)
            return 2
    except (QAError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"omg qa: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if action == "run":
        return 0 if result.get("clean") else 1
    return 0


def cmd_autopilot(args: argparse.Namespace) -> int:
    """Strict Autopilot v2 coordinator."""
    from omg_cli.autopilot import (
        AutopilotError,
        complete_with_acceptance,
        run_autopilot,
        set_awaiting_confirmation,
        start_autopilot,
        status_autopilot,
        transition,
    )

    root = project_root()
    action = getattr(args, "autopilot_action", None)
    try:
        if action == "run":
            goal = " ".join(args.goal or []).strip()
            timeout = getattr(args, "timeout", None)
            if timeout is not None:
                timeout = float(timeout)
            return run_autopilot(
                root,
                goal,
                skip_interview=bool(getattr(args, "skip_interview", False)),
                resume_run_id=getattr(args, "resume", None),
                max_phase_cycles=int(getattr(args, "max_phase_cycles", 5)),
                dry_run=bool(getattr(args, "dry_run", False)),
                timeout=timeout,
                yolo=bool(getattr(args, "yolo", False)),
                safe=bool(getattr(args, "safe", False)),
                force=bool(getattr(args, "force", False)),
                unattended=bool(getattr(args, "unattended", False)),
                max_stall_relaunches=int(
                    getattr(args, "max_stall_relaunches", 32) or 32
                ),
            )
        if action == "start":
            goal = " ".join(args.goal or []).strip()
            result = start_autopilot(
                root,
                goal,
                force=bool(getattr(args, "force", False)),
                skip_interview=bool(getattr(args, "skip_interview", False)),
            )
        elif action == "transition":
            evidence = None
            if getattr(args, "evidence_json", None):
                evidence = json.loads(args.evidence_json)
            result = transition(
                root,
                args.run_id,
                args.phase,
                reason=getattr(args, "reason", None),
                evidence=evidence,
            )
        elif action == "status":
            result = status_autopilot(root, args.run_id)
        elif action == "complete":
            result = complete_with_acceptance(root, args.run_id)
        elif action == "await":
            result = set_awaiting_confirmation(
                root,
                args.run_id,
                not bool(getattr(args, "clear", False)),
                reason=getattr(args, "reason", None),
            )
        else:
            print("omg autopilot: action required", file=sys.stderr)
            return 2
    except (AutopilotError, FileNotFoundError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"omg autopilot: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """User-invoked trusted broker for external advisor CLIs (never product executor)."""
    from omg_cli.ask import run_ask_cli

    prompt_parts = list(args.prompt or [])
    prompt = " ".join(prompt_parts).strip()
    if getattr(args, "prompt_file", None):
        pfile = Path(args.prompt_file)
        try:
            file_text = pfile.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"omg ask: cannot read --prompt-file: {exc}", file=sys.stderr)
            return 2
        prompt = (file_text + ("\n" + prompt if prompt else "")).strip()
    if not prompt:
        print("omg ask: prompt text required (args or --prompt-file)", file=sys.stderr)
        return 2

    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        timeout = float(timeout)

    files = list(getattr(args, "files", None) or [])
    extra = list(getattr(args, "extra", None) or [])
    out = getattr(args, "out", None)
    cwd = getattr(args, "cwd", None)

    return run_ask_cli(
        args.provider,
        prompt,
        root=project_root(),
        cwd=Path(cwd).resolve() if cwd else None,
        timeout=timeout,
        max_bytes=int(getattr(args, "max_bytes", 512 * 1024)),
        out=Path(out) if out else None,
        run_id=getattr(args, "run_id", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        model=getattr(args, "model", None),
        extra=extra or None,
        write_json=bool(getattr(args, "json", True)),
        files=files or None,
    )


def cmd_pipeline(args: argparse.Namespace) -> int:
    """AUTO_PILOT-like FSM: ralplan → implement → dual_review → accept."""
    from omg_cli.pipeline import run_pipeline

    goal = " ".join(args.goal or []).strip()
    if not goal and not getattr(args, "resume", None):
        print("omg pipeline: goal text required (unless --resume)", file=sys.stderr)
        return 2

    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        timeout = float(timeout)

    require_acceptance = True
    if getattr(args, "no_require_acceptance", False):
        require_acceptance = False
    if getattr(args, "require_acceptance", False):
        require_acceptance = True

    dual = True
    if getattr(args, "no_dual_review", False):
        dual = False
    if getattr(args, "dual_review", False):
        dual = True

    return run_pipeline(
        goal or "(resume)",
        root=project_root(),
        implement=str(getattr(args, "implement", "ralph") or "ralph"),
        max_plan_rounds=int(getattr(args, "max_plan_rounds", 3) or 3),
        max_iter=int(getattr(args, "max_iter", 3) or 3),
        skip_plan=bool(getattr(args, "skip_plan", False)),
        plan_only=bool(getattr(args, "plan_only", False)),
        dual_review=dual,
        require_acceptance=require_acceptance,
        yolo=bool(getattr(args, "yolo", False)),
        safe=bool(getattr(args, "safe", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        timeout=timeout,
        resume_run_id=getattr(args, "resume", None),
        force=bool(getattr(args, "force", False)),
    )


def cmd_dual_review(args: argparse.Namespace) -> int:
    """Grok-native critic→verifier. Does NOT set verified."""
    from omg_cli.dual_review import run_dual_review_cli

    goal = " ".join(args.goal or []).strip()
    run_id = getattr(args, "run_id", None)
    if not goal and not run_id:
        print(
            "omg dual-review: goal text required (or pass --run with existing goal)",
            file=sys.stderr,
        )
        return 2
    if not goal:
        from omg_cli.state import load_run

        if not isinstance(run_id, str):
            print("omg dual-review: --run requires a run ID", file=sys.stderr)
            return 2
        data = load_run(project_root(), run_id)
        goal = (data or {}).get("goal") or "(dual-review)"

    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        timeout = float(timeout)

    return run_dual_review_cli(
        goal,
        root=project_root(),
        run_id=run_id,
        dry_run=bool(getattr(args, "dry_run", False)),
        timeout=timeout,
        yolo=bool(getattr(args, "yolo", False)),
        safe=bool(getattr(args, "safe", False)),
        force=bool(getattr(args, "force", False)),
    )

__all__ = [
    "cmd_ask",
    "cmd_autopilot",
    "cmd_dual_review",
    "cmd_mode",
    "cmd_pipeline",
    "cmd_qa",
    "cmd_review",
]
