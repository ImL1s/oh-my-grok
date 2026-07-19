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
    try:
        cancelled = cancel_run(root, run_id)
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

    return run_mode(
        mode,
        goal,
        yolo=bool(getattr(args, "yolo", False)),
        safe=bool(getattr(args, "safe", False)),
        root=_project_root(),
        max_iter=int(max_iter),
        dry_run=bool(getattr(args, "dry_run", False)),
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
    p_cancel.set_defaults(func=cmd_cancel)

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
            help="max iterations (ralph default 3; ulw/ralplan default 1)",
        )
        p.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="create run + argv only; do not exec grok",
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
