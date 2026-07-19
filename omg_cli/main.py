# omg_cli/main.py
"""omg CLI argparse router."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _project_root() -> Path:
    return Path.cwd().resolve()


def cmd_setup(args: argparse.Namespace) -> int:
    from omg_cli.setup_cmd import run_setup

    return run_setup(_project_root())


def cmd_doctor(args: argparse.Namespace) -> int:
    from omg_cli.doctor import run_doctor

    return run_doctor(strict=bool(getattr(args, "strict", False)))


def cmd_state(args: argparse.Namespace) -> int:
    from omg_cli.state import load_active_run, load_run

    root = _project_root()
    if getattr(args, "run_id", None):
        data = load_run(root, args.run_id)
        if data is None:
            print(f"no run found: {args.run_id}", file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    active = load_active_run(root)
    if active is None:
        print("no active run")
        return 0
    print(json.dumps(active, indent=2, ensure_ascii=False))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    from omg_cli.state import cancel_run

    root = _project_root()
    run_id = getattr(args, "run_id", None)
    grace = float(getattr(args, "grace", 2.0))
    try:
        cancelled = cancel_run(root, run_id, kill_grace_s=grace)
    except FileNotFoundError as e:
        print(f"cancel failed: {e}", file=sys.stderr)
        return 1
    print(f"cancelled run {cancelled['run_id']}")
    print(json.dumps(cancelled, indent=2, ensure_ascii=False))
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    """Launch ulw / ralph / ralplan via omg_cli.modes.run_mode."""
    from omg_cli.modes import DEFAULT_MAX_ITER, run_mode

    mode = args.command
    goal = " ".join(args.goal or []).strip()
    if not goal:
        print(f"omg {mode}: goal text required", file=sys.stderr)
        return 2

    max_iter = getattr(args, "max_iter", None)
    if max_iter is None:
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
        from omg_cli.fanout import run_process_fanout

        # require_acceptance: None → False for process fanout unless explicitly set
        ra = require_acceptance if require_acceptance is not None else False
        return run_process_fanout(
            goal,
            workers=workers,
            root=_project_root(),
            yolo=bool(getattr(args, "yolo", False)),
            safe=bool(getattr(args, "safe", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            timeout=timeout,
            require_acceptance=bool(ra),
            force=bool(getattr(args, "force", False)),
        )

    return run_mode(
        mode,
        goal,
        yolo=bool(getattr(args, "yolo", False)),
        safe=bool(getattr(args, "safe", False)),
        root=_project_root(),
        max_iter=int(max_iter),
        dry_run=bool(getattr(args, "dry_run", False)),
        timeout=timeout,
        require_acceptance=require_acceptance,
    )


def cmd_accept(args: argparse.Namespace) -> int:
    """Freeze PRD acceptance commands and run them for active (or --run) run."""
    from omg_cli.acceptance import (
        CommandAllowlistError,
        freeze_acceptance,
        freeze_and_run,
        format_commands_review,
        load_frozen_commands,
        load_prd,
        result_path,
    )
    from omg_cli.state import load_active_run, load_run, set_verified

    root = _project_root()
    run_id = getattr(args, "run_id", None)
    if not run_id:
        active = load_active_run(root)
        if active is None:
            print("accept failed: no active run (pass --run ID)", file=sys.stderr)
            return 1
        run_id = active["run_id"]

    if load_run(root, run_id) is None:
        print(f"accept failed: no run found: {run_id}", file=sys.stderr)
        return 1

    prd = load_prd(root, run_id)
    if prd is None:
        print(
            f"accept failed: no prd.json under runs/{run_id}/",
            file=sys.stderr,
        )
        return 1

    dry_run = bool(getattr(args, "dry_run", False))
    review = bool(getattr(args, "review", False))
    yes = bool(getattr(args, "yes", False))
    no_allowlist = bool(getattr(args, "no_allowlist", False))
    extra_allow = list(getattr(args, "allow_cmd", None) or [])

    if no_allowlist:
        print(
            "WARNING: --no-allowlist disables the positive acceptance allowlist "
            "(always-deny bins and shells still blocked). Dangerous; use only in "
            "controlled emergencies.",
            file=sys.stderr,
        )

    # Freeze early so --review can print the exact command list.
    try:
        freeze_acceptance(root, run_id, prd)
        commands = load_frozen_commands(root, run_id)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"accept failed: {exc}", file=sys.stderr)
        return 1

    if review or not dry_run:
        print(format_commands_review(commands))

    # Gate: --review always requires --yes to exec; non-tty requires --yes too.
    needs_yes = (review or not sys.stdin.isatty()) and not dry_run
    if needs_yes and not yes:
        if review:
            print(
                "accept --review: pass --yes to execute the commands above",
                file=sys.stderr,
            )
        else:
            print(
                "accept: non-tty stdin requires --yes to execute acceptance commands "
                "(or use --dry-run / --review)",
                file=sys.stderr,
            )
        return 2

    try:
        ok = freeze_and_run(
            root,
            run_id,
            prd,
            dry_run=dry_run,
            extra_allow=extra_allow or None,
            no_allowlist=no_allowlist,
        )
    except CommandAllowlistError as exc:
        print(f"accept allowlist rejected: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"accept failed: {exc}", file=sys.stderr)
        return 1

    rpath = result_path(root, run_id)
    print(f"acceptance result: {rpath}")
    if rpath.is_file():
        print(rpath.read_text(encoding="utf-8"))

    if dry_run:
        print("dry_run: commands not executed; verified not set")
        return 0

    if not ok:
        print("acceptance FAILED", file=sys.stderr)
        return 1

    try:
        verified = set_verified(root, run_id, force=False)
    except PermissionError as exc:
        print(f"set_verified failed: {exc}", file=sys.stderr)
        return 1

    print(f"verified run {verified['run_id']}")
    print(json.dumps(verified, indent=2, ensure_ascii=False))
    return 0


def cmd_integrate(args: argparse.Namespace) -> int:
    """Apply ULW result envelopes (cherry-pick) for active or --run run."""
    from omg_cli.integrate import IntegrateError, integrate_results, result_path
    from omg_cli.state import load_active_run, load_run

    root = _project_root()
    run_id = getattr(args, "run_id", None)
    if not run_id:
        active = load_active_run(root)
        if active is None:
            print("integrate failed: no active run (pass --run ID)", file=sys.stderr)
            return 1
        run_id = active["run_id"]

    if load_run(root, run_id) is None:
        print(f"integrate failed: no run found: {run_id}", file=sys.stderr)
        return 1

    dry_run = bool(getattr(args, "dry_run", False))
    try:
        result = integrate_results(root, run_id, dry_run=dry_run)
    except (FileNotFoundError, OSError, IntegrateError) as exc:
        print(f"integrate failed: {exc}", file=sys.stderr)
        return 1

    rpath = result_path(root, run_id)
    print(f"integrate result: {rpath}")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    status = result.get("status")
    if status == "ok":
        return 0
    if status == "missing":
        # No envelopes yet — not a hard failure for dry-run document path
        return 0 if dry_run else 1
    return 1


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
        root=_project_root(),
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
        root=_project_root(),
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

        data = load_run(_project_root(), run_id)
        goal = (data or {}).get("goal") or "(dual-review)"

    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        timeout = float(timeout)

    return run_dual_review_cli(
        goal,
        root=_project_root(),
        run_id=run_id,
        dry_run=bool(getattr(args, "dry_run", False)),
        timeout=timeout,
        yolo=bool(getattr(args, "yolo", False)),
        safe=bool(getattr(args, "safe", False)),
        force=bool(getattr(args, "force", False)),
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--safe",
        action="store_true",
        help="prefer safe defaults (modes use later)",
    )
    common.add_argument(
        "--yolo",
        action="store_true",
        help="allow elevated permissions for mode launchers (off by default)",
    )

    parser = argparse.ArgumentParser(
        prog="omg",
        description="oh-my-grok CLI — setup, doctor, state, and mode launchers",
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser(
        "setup",
        parents=[common],
        help="ensure .omg dirs, merge AGENTS + gitignore",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_doctor = sub.add_parser(
        "doctor",
        parents=[common],
        help="check plugin + environment health",
    )
    p_doctor.add_argument(
        "--strict",
        action="store_true",
        help="treat compat.claude isolation risks as FAIL (exit 1)",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_state = sub.add_parser(
        "state",
        parents=[common],
        help="show active run (or --run <id>)",
    )
    p_state.add_argument("--run", dest="run_id", default=None, help="specific run_id")
    p_state.set_defaults(func=cmd_state)

    p_cancel = sub.add_parser(
        "cancel",
        parents=[common],
        help="cancel active (or --run) run",
    )
    p_cancel.add_argument("--run", dest="run_id", default=None, help="specific run_id")
    p_cancel.add_argument(
        "--grace",
        dest="grace",
        type=float,
        default=2.0,
        help="seconds after SIGTERM before SIGKILL (default: 2.0; 0=SIGTERM only)",
    )
    p_cancel.set_defaults(func=cmd_cancel)

    p_accept = sub.add_parser(
        "accept",
        parents=[common],
        help="freeze PRD commands and run acceptance for active (or --run) run",
    )
    p_accept.add_argument("--run", dest="run_id", default=None, help="specific run_id")
    p_accept.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="validate/freeze only; do not exec acceptance commands",
    )
    p_accept.add_argument(
        "--review",
        dest="review",
        action="store_true",
        help="print frozen commands; require --yes to execute",
    )
    p_accept.add_argument(
        "--yes",
        dest="yes",
        action="store_true",
        help="confirm execution (required with --review or non-tty stdin)",
    )
    p_accept.add_argument(
        "--allow-cmd",
        dest="allow_cmd",
        action="append",
        default=[],
        metavar="NAME",
        help="extend acceptance basename allowlist (repeatable)",
    )
    p_accept.add_argument(
        "--no-allowlist",
        dest="no_allowlist",
        action="store_true",
        help=(
            "DANGEROUS: skip positive allowlist (shells + always-deny bins "
            "like claude/rm still blocked)"
        ),
    )
    p_accept.set_defaults(func=cmd_accept)

    p_integrate = sub.add_parser(
        "integrate",
        parents=[common],
        help="apply ULW result envelopes via git cherry-pick (active or --run)",
    )
    p_integrate.add_argument(
        "--run", dest="run_id", default=None, help="specific run_id"
    )
    p_integrate.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="validate envelopes / base_sha only; do not cherry-pick",
    )
    p_integrate.set_defaults(func=cmd_integrate)

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
        if mode == "ulw":
            p.add_argument(
                "--fanout",
                dest="fanout",
                choices=("skill", "process"),
                default="skill",
                help=(
                    "parallelism path: skill=spawn_subagent in one grok (default); "
                    "process=N× independent grok -p (no tmux; opt-in)"
                ),
            )
            p.add_argument(
                "--workers",
                dest="workers",
                type=int,
                default=None,
                help=(
                    "process fanout worker count (default 2; hard cap 8 / "
                    "OMG_MAX_WORKERS); ignored for --fanout skill"
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
        help="trusted user broker for external advisors (codex/claude/gemini)",
    )
    p_ask.add_argument(
        "provider",
        help="provider: codex | claude (fable) | gemini (optional)",
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
    p_ask.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=True,
        help="write sidecar meta JSON (default on)",
    )
    p_ask.add_argument("--model", dest="model", default=None, help="optional model pin")
    p_ask.add_argument(
        "--extra",
        dest="extra",
        action="append",
        default=[],
        help="passthrough arg after fixed template (deny elevation flags)",
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
