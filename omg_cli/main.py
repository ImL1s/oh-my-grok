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
    from omg_cli.acceptance import freeze_and_run, load_prd, result_path
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
    try:
        ok = freeze_and_run(root, run_id, prd, dry_run=dry_run)
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
        p.set_defaults(func=cmd_mode)

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
