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

    return run_doctor()


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


def cmd_mode_stub(args: argparse.Namespace) -> int:
    """Stub for Task 6 mode launchers (ulw / ralph / ralplan)."""
    mode = args.command
    goal = " ".join(args.goal or []).strip() or "(no goal)"
    safe = getattr(args, "safe", False)
    yolo = getattr(args, "yolo", False)
    print(
        f"omg {mode}: not implemented yet (Task 6).\n"
        f"  goal={goal!r}\n"
        f"  --safe={safe} --yolo={yolo}\n"
        f"Mode launchers will create run state and invoke grok -p with skill bodies.",
        file=sys.stderr,
    )
    return 2


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
        ("ulw", "ultrawork parallel mode (stub — Task 6)"),
        ("ralph", "ralph persistence loop (stub — Task 6)"),
        ("ralplan", "ralplan consensus planning (stub — Task 6)"),
    ):
        p = sub.add_parser(mode, parents=[common], help=help_text)
        p.add_argument("goal", nargs="*", help="goal text")
        p.set_defaults(func=cmd_mode_stub)

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
