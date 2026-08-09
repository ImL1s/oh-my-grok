"""Modes-family CLI handlers (#29 Phase 2).

Commands: ulw/ralph/ralplan (via cmd_mode), review, qa, autopilot, ask,
pipeline, dual-review.
Parser construction: ``register_modes_parsers`` (#29 Phase 4').
"""

from __future__ import annotations

from omg_cli.cli_envelope import emit_data

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
    diff_text = args.diff_text or ""
    if not diff_text.strip():
        print(
            "omg review: --diff-text is required and must be non-empty "
            "(a clean stamp bound to an empty diff hash is not a real "
            "review of product changes)",
            file=sys.stderr,
        )
        return 2
    try:
        cr = json.loads(args.code_reviewer_json)
        ar = json.loads(args.architect_json)
        result = run_structured_review(
            root,
            args.run_id,
            diff_text=diff_text,
            code_reviewer_payload=cr,
            architect_payload=ar,
        )
    except (ReviewError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"omg review: {exc}", file=sys.stderr)
        return 1
    emit_data(args, "review", result)
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
    emit_data(args, "qa", result)
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
    emit_data(args, "autopilot", result)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """User-invoked trusted broker for external advisor CLIs (never product executor)."""
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

    # #68 PR3 — thin durable-job seam (sync ask unchanged by default).
    if bool(getattr(args, "background", False)):
        return _cmd_ask_background(args, prompt)

    from omg_cli.ask import run_ask_cli

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


def _cmd_ask_background(args: argparse.Namespace, prompt: str) -> int:
    """Create a durable job and return job_id immediately (no sync broker)."""
    from omg_cli.cli_envelope import emit_json, failure, success, wants_json
    from omg_cli.jobs.models import JobStoreError
    from omg_cli.jobs.runtime import start_job
    from omg_cli.redaction import redact_text, redact_value

    cmd = "ask.background"
    provider_raw = str(getattr(args, "provider", "") or "").strip().lower()
    # Background path admits ask aliases → jobs providers only.
    if provider_raw == "fake":
        job_provider = "fake"
    elif provider_raw in {"agy", "antigravity"}:
        job_provider = "antigravity"
    else:
        emit_json(
            failure(
                cmd,
                "E_JOB_PROVIDER",
                f"ask --background admits only fake|agy (got {provider_raw!r})",
                next_action="Use provider fake or agy, or omit --background",
            )
        )
        return 2

    root = project_root()
    # Materialize prompt under a temp file for start_job (copied into job dir).
    # Never writes ask artifacts / never invokes the sync broker.
    prompt_path = root / ".omg" / "jobs" / ".ask-background-prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")

    role = str(getattr(args, "role", None) or "researcher")
    budget = int(getattr(args, "attempt_budget", 1) or 1)
    timeout = getattr(args, "timeout", None)
    provider_timeout = float(timeout) if timeout is not None else None

    try:
        result = start_job(
            root,
            provider=job_provider,
            role=role,
            prompt_file=prompt_path,
            run_id=getattr(args, "run_id", None) or None,
            model=getattr(args, "model", None) or None,
            provider_timeout_s=provider_timeout,
            attempt_budget=budget,
        )
    except JobStoreError as exc:
        emit_json(
            failure(
                cmd,
                getattr(exc, "code", None) or "E_JOB_START",
                redact_text(str(exc)),
            )
        )
        return 1

    rec = result.record
    payload = {
        "job_id": rec.job_id,
        "state": rec.state.value,
        "provider": rec.provider,
        "role": rec.role,
        "attempt": rec.attempt,
        "attempt_budget": rec.attempt_budget,
        "background": True,
    }
    if wants_json(args):
        emit_json(success(cmd, **redact_value(payload)))
    else:
        print(f"ask background job_id={rec.job_id} state={rec.state.value}")
    return 0


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


def register_modes_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register modes-family argparse parsers (#29 Phase 4').

    Commands: review, qa, autopilot, ulw, ralph, ralplan, ask, pipeline, dual-review.
    """
    p_review = sub.add_parser(
        "review",
        parents=[common],
        help="hash-bound structured review gate (code-reviewer + architect)",
    )
    p_review.add_argument("--run", dest="run_id", required=True)
    p_review.add_argument(
        "--diff-text",
        dest="diff_text",
        default="",
        help="current diff text whose hash binds both lanes",
    )
    p_review.add_argument(
        "--code-reviewer-json",
        required=True,
        help='JSON payload e.g. {"verdict":"APPROVE","findings":[]}',
    )
    p_review.add_argument(
        "--architect-json",
        required=True,
        help='JSON payload e.g. {"verdict":"CLEAR","findings":[]}',
    )
    p_review.set_defaults(func=cmd_review)

    p_qa = sub.add_parser(
        "qa",
        parents=[common],
        help="bounded UltraQA freeze/run/status (never sets verified)",
    )
    qa_sub = p_qa.add_subparsers(dest="qa_action")
    p_qa_f = qa_sub.add_parser("freeze", parents=[common], help="freeze scenarios")
    p_qa_f.add_argument("--run", dest="run_id", required=True)
    p_qa_f.add_argument(
        "--scenarios-json",
        required=True,
        help='[{"id","command"}] or {"id","check":"always_pass"}',
    )
    p_qa_f.add_argument("--plan-hash", default=None)
    p_qa_f.add_argument("--spec-hash", default=None)
    p_qa_f.set_defaults(func=cmd_qa, qa_action="freeze")
    p_qa_r = qa_sub.add_parser("run", parents=[common], help="run one QA cycle")
    p_qa_r.add_argument("--run", dest="run_id", required=True)
    p_qa_r.add_argument(
        "--repair-classification",
        choices=("product_change", "test_harness_correction"),
        default=None,
    )
    p_qa_r.set_defaults(func=cmd_qa, qa_action="run")
    p_qa_s = qa_sub.add_parser("status", parents=[common], help="QA status")
    p_qa_s.add_argument("--run", dest="run_id", required=True)
    p_qa_s.set_defaults(func=cmd_qa, qa_action="status")
    p_qa.set_defaults(func=cmd_qa)

    p_ap = sub.add_parser(
        "autopilot",
        parents=[common],
        help="strict Autopilot v2 phase coordinator",
    )
    ap_sub = p_ap.add_subparsers(dest="autopilot_action")
    p_ap_start = ap_sub.add_parser("start", parents=[common], help="start autopilot run")
    p_ap_start.add_argument("goal", nargs="+", help="goal text")
    p_ap_start.add_argument("--force", action="store_true")
    p_ap_start.add_argument(
        "--skip-interview",
        action="store_true",
        help="start at ralplan only when interview already complete (evidence later)",
    )
    p_ap_start.set_defaults(func=cmd_autopilot, autopilot_action="start")
    p_ap_tr = ap_sub.add_parser(
        "transition", parents=[common], help="legal phase transition"
    )
    p_ap_tr.add_argument("--run", dest="run_id", required=True)
    p_ap_tr.add_argument("--phase", required=True, help="next phase")
    p_ap_tr.add_argument("--reason", default=None)
    p_ap_tr.add_argument(
        "--evidence-json",
        default=None,
        help=(
            "gate evidence JSON; bare interview_complete/consensus booleans "
            'require break_glass=true (prefer CLI stamps). '
            'e.g. {"consensus":true,"break_glass":true}'
        ),
    )
    p_ap_tr.set_defaults(func=cmd_autopilot, autopilot_action="transition")
    p_ap_st = ap_sub.add_parser("status", parents=[common], help="autopilot status")
    p_ap_st.add_argument("--run", dest="run_id", required=True)
    p_ap_st.set_defaults(func=cmd_autopilot, autopilot_action="status")
    p_ap_c = ap_sub.add_parser(
        "complete",
        parents=[common],
        help="same-process acceptance → verified only",
    )
    p_ap_c.add_argument("--run", dest="run_id", required=True)
    p_ap_c.set_defaults(func=cmd_autopilot, autopilot_action="complete")
    p_ap_await = ap_sub.add_parser(
        "await",
        parents=[common],
        help="set/clear autopilot awaiting-confirmation pause",
    )
    p_ap_await.add_argument("--run", dest="run_id", required=True)
    p_ap_await.add_argument("--reason", default=None, help="pause reason label")
    p_ap_await.add_argument(
        "--clear",
        action="store_true",
        help="clear awaiting flag (default sets awaiting=true)",
    )
    p_ap_await.set_defaults(func=cmd_autopilot, autopilot_action="await")
    p_ap_run = ap_sub.add_parser(
        "run",
        parents=[common],
        help="outer CLI driver (cross-turn/headless persistence)",
    )
    p_ap_run.add_argument("goal", nargs="*", help="goal text (omit when resuming)")
    p_ap_run.add_argument("--force", action="store_true")
    p_ap_run.add_argument(
        "--skip-interview",
        action="store_true",
        help="start at ralplan when creating a new run",
    )
    p_ap_run.add_argument(
        "--resume",
        dest="resume",
        nargs="?",
        const="__active__",
        default=None,
        metavar="RUN",
        help="resume active or explicit autopilot run",
    )
    p_ap_run.add_argument(
        "--max-phase-cycles",
        dest="max_phase_cycles",
        type=int,
        default=5,
        help="max grok launches per phase before blocked (default 5)",
    )
    p_ap_run.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="create run + argv only; do not exec grok",
    )
    p_ap_run.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="seconds per grok launch (default 3600); 0 = unlimited",
    )
    p_ap_run.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "hands-off outer loop (#40): re-launch on host-turn stalls without "
            "printing a human go prompt (still pauses on interview/await)"
        ),
    )
    p_ap_run.add_argument(
        "--max-stall-relaunches",
        dest="max_stall_relaunches",
        type=int,
        default=32,
        help="unattended: max re-launches after no phase advance (default 32)",
    )
    p_ap_run.set_defaults(func=cmd_autopilot, autopilot_action="run")
    p_ap.set_defaults(func=cmd_autopilot)

    for mode, help_text in (
        ("ulw", "ultrawork parallel mode (spawn_subagent fan-out)"),
        ("ralph", "ralph persistence loop (one story per iteration)"),
        ("ralplan", "ralplan consensus planning (no implementation)"),
    ):
        p = sub.add_parser(mode, parents=[common], help=help_text)
        p.add_argument("goal", nargs="*", help="goal text")
        p.add_argument(
            "--max-iter",
            dest="max_iter",
            type=int,
            default=None,
            help=(
                "max iterations (ralph default 3; ulw default 1) "
                "or max_rounds for ralplan verifier attempts (default 3)"
            ),
        )
        p.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="create run + argv only; do not exec grok",
        )
        p.add_argument(
            "--require-acceptance",
            dest="require_acceptance",
            action="store_true",
            default=None,
            help="exit non-zero if not verified (default on for ralph)",
        )
        p.add_argument(
            "--no-require-acceptance",
            dest="no_require_acceptance",
            action="store_true",
            default=False,
            help="allow completed-without-verified exit 0",
        )
        p.add_argument(
            "--timeout",
            dest="timeout",
            type=float,
            default=None,
            help=(
                "seconds per grok launch (default 3600); "
                "0 = unlimited; dry-run ignores"
            ),
        )
        if mode == "ralph":
            p.add_argument(
                "--resume",
                dest="resume",
                nargs="?",
                const="__active__",
                default=None,
                metavar="RUN",
                help=(
                    "resume active Ralph run, or explicit RUN, with its "
                    "persisted Grok session and cumulative ceiling"
                ),
            )
        if mode == "ralplan":
            p.add_argument(
                "--run",
                dest="run_id",
                default=None,
                metavar="RUN",
                help=(
                    "reuse an existing run_id (pipeline/autopilot embedding); "
                    "skips create_run so active pointer stays on that run"
                ),
            )
        if mode == "ulw":
            p.add_argument(
                "--fanout",
                dest="fanout",
                choices=("skill", "process"),
                default="skill",
                help=(
                    "parallelism path: skill=spawn_subagent in one grok (default); "
                    "process=N× independent grok -p (experimental; requires "
                    "OMG_EXPERIMENTAL_PROCESS_FANOUT=1)"
                ),
            )
            p.add_argument(
                "--workers",
                dest="workers",
                type=int,
                default=None,
                help=(
                    "process fanout worker count (default 2; hard cap 8 / "
                    "OMG_MAX_WORKERS); ignored for --fanout skill; process path "
                    "requires OMG_EXPERIMENTAL_PROCESS_FANOUT=1"
                ),
            )
            p.add_argument(
                "--force",
                dest="force",
                action="store_true",
                help="supersede active run when creating (process fanout)",
            )
        p.set_defaults(func=cmd_mode)

    # --- Phase 2: ask / pipeline / dual-review ---
    p_ask = sub.add_parser(
        "ask",
        parents=[common],
        help="trusted user broker for external advisors (codex/claude/gemini/agy)",
    )
    p_ask.add_argument(
        "provider",
        help="provider: codex | claude (fable) | gemini (optional) | agy (Antigravity adapter)",
    )
    p_ask.add_argument("prompt", nargs="*", help="prompt text")
    p_ask.add_argument(
        "--prompt-file",
        dest="prompt_file",
        default=None,
        help="read prompt from file (appended with positional prompt)",
    )
    p_ask.add_argument(
        "--file",
        dest="files",
        action="append",
        default=[],
        help="extra context file to inline (repeatable)",
    )
    p_ask.add_argument("--cwd", dest="cwd", default=None, help="child cwd (default: project root)")
    p_ask.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=600.0,
        help="seconds (default 600; 0 = unlimited)",
    )
    p_ask.add_argument(
        "--max-bytes",
        dest="max_bytes",
        type=int,
        default=512 * 1024,
        help="truncate captured output (default 512KiB)",
    )
    p_ask.add_argument(
        "--out",
        dest="out",
        default=None,
        help="artifact path (default .omg/artifacts/ask-<ts>-<provider>.md)",
    )
    p_ask.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="optional existing run_id to link artifact",
    )
    p_ask.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="print argv + env keys; do not exec provider",
    )
    # Global --json optional; ask still defaults write_json=True in handler
    p_ask.add_argument("--model", dest="model", default=None, help="optional model pin")
    p_ask.add_argument(
        "--extra",
        dest="extra",
        action="append",
        default=[],
        help=(
            "passthrough arg after fixed template (disabled by default; "
            "set OMG_ASK_ALLOW_EXTRA=1; elevation flags always denied)"
        ),
    )
    p_ask.add_argument(
        "--background",
        dest="background",
        action="store_true",
        help=(
            "create a durable job and return job_id immediately "
            "(providers: fake|agy only; sync ask unchanged by default)"
        ),
    )
    p_ask.add_argument(
        "--attempt-budget",
        dest="attempt_budget",
        type=int,
        default=1,
        help="immutable max attempts for --background jobs (default 1)",
    )
    p_ask.add_argument(
        "--role",
        dest="role",
        default="researcher",
        help="job role label for --background (default researcher)",
    )
    p_ask.set_defaults(func=cmd_ask)

    p_pipe = sub.add_parser(
        "pipeline",
        parents=[common],
        help="plan → implement → dual-review → accept (Grok-native FSM)",
    )
    p_pipe.add_argument("goal", nargs="*", help="goal text")
    p_pipe.add_argument(
        "--plan-only",
        dest="plan_only",
        action="store_true",
        help="stop after ralplan accepted",
    )
    p_pipe.add_argument(
        "--skip-plan",
        dest="skip_plan",
        action="store_true",
        help="start at implement (user already has a plan)",
    )
    p_pipe.add_argument(
        "--implement",
        dest="implement",
        choices=("ralph", "ulw"),
        default="ralph",
        help="implement stage mode (default: ralph)",
    )
    p_pipe.add_argument(
        "--max-plan-rounds",
        dest="max_plan_rounds",
        type=int,
        default=3,
        help="ralplan max_rounds (default 3)",
    )
    p_pipe.add_argument(
        "--max-iter",
        dest="max_iter",
        type=int,
        default=3,
        help="ralph max_iter / ulw iters (default 3)",
    )
    p_pipe.add_argument(
        "--require-acceptance",
        dest="require_acceptance",
        action="store_true",
        default=False,
        help="exit non-zero if not verified (default on)",
    )
    p_pipe.add_argument(
        "--no-require-acceptance",
        dest="no_require_acceptance",
        action="store_true",
        default=False,
        help="allow completed-without-verified exit 0",
    )
    p_pipe.add_argument(
        "--dual-review",
        dest="dual_review",
        action="store_true",
        default=False,
        help="enable dual-review stage (default on unless --no-dual-review)",
    )
    p_pipe.add_argument(
        "--no-dual-review",
        dest="no_dual_review",
        action="store_true",
        default=False,
        help="skip Grok-native dual-review stage",
    )
    p_pipe.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="seconds per grok launch",
    )
    p_pipe.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="FSM + argv artifacts only; no live grok",
    )
    p_pipe.add_argument(
        "--resume",
        dest="resume",
        default=None,
        metavar="RUN_ID",
        help="resume pipeline from pipeline.json stage",
    )
    p_pipe.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="supersede active run when creating",
    )
    p_pipe.set_defaults(func=cmd_pipeline)

    p_dual = sub.add_parser(
        "dual-review",
        parents=[common],
        help="Grok-native critic→verifier (does not set verified)",
    )
    p_dual.add_argument("goal", nargs="*", help="goal / review scope")
    p_dual.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="attach to existing run_id (or create dual-review run)",
    )
    p_dual.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="write stage prompts only; no grok exec",
    )
    p_dual.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="seconds per grok launch",
    )
    p_dual.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="supersede active run when creating",
    )
    p_dual.set_defaults(func=cmd_dual_review)


__all__ = [
    "register_modes_parsers",
    "cmd_ask",
    "cmd_autopilot",
    "cmd_dual_review",
    "cmd_mode",
    "cmd_pipeline",
    "cmd_qa",
    "cmd_review",
]
