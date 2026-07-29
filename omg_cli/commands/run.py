"""Run-family CLI handlers + parsers (#29 Phase 2 / 4').

Commands: state, cancel, resume, session, recover.
Parser construction: ``register_run_parsers`` (#29 Phase 4').
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omg_cli.cli_envelope import emit_data
from omg_cli.cli_util import project_root


def print_state_human(data: dict) -> None:
    """One-screen human summary (Codex P1-5 lightweight HUD substitute)."""
    rid = data.get("run_id") or "?"
    mode = data.get("mode") or "?"
    status = data.get("status") or "?"
    verified = data.get("verified")
    goal = (data.get("goal") or "").strip()
    if len(goal) > 120:
        goal = goal[:117] + "..."
    print(f"run:      {rid}")
    print(f"mode:     {mode}")
    print(f"status:   {status}")
    print(f"verified: {verified}")
    if goal:
        print(f"goal:     {goal}")
    for key in (
        "schema_classification",
        "stage",
        "iteration",
        "iterations_completed",
        "passes",
        "exit_code",
        "grok_session_id",
        "grok_session_state",
        "note",
        "integrate_status",
    ):
        if key in data and data[key] is not None:
            print(f"{key + ':':<10}{data[key]}")
    lease = data.get("execution_lease")
    if isinstance(lease, dict):
        print(
            "lease:    "
            f"{lease.get('state', '?')} owner={lease.get('invocation_id', '?')} "
            f"generation={lease.get('generation', '?')} pid={lease.get('pid', '?')}"
        )
    request = data.get("cancellation_request")
    if isinstance(request, dict):
        print(
            "cancel:   requested "
            f"id={request.get('request_id', '?')} "
            f"generation={request.get('observed_generation', '?')}"
        )
    if data.get("blocker"):
        print(f"blocker:  {json.dumps(data['blocker'], ensure_ascii=False)}")
    next_hint = "none"
    if verified is True:
        next_hint = "done (verified)"
    elif status == "cancelled":
        next_hint = "none (cancelled)"
    elif isinstance(data.get("next_action"), str) and data["next_action"].strip():
        next_hint = data["next_action"].strip()
    elif status in ("failed",):
        next_hint = "inspect logs / omg cancel / fix and re-run"
    elif mode == "ulw":
        next_hint = "omg integrate (if envelopes) → omg accept"
    elif mode == "ralph":
        next_hint = f"omg ralph --resume {rid}"
    elif mode == "pipeline":
        next_hint = "omg pipeline --resume <run>"
    print(f"next:     {next_hint}")


# Compat alias used by tests importing from main
_print_state_human = print_state_human


def cmd_state(args: argparse.Namespace) -> int:
    from omg_cli.cli_envelope import emit_json, failure, success
    from omg_cli.command_context import get_context
    from omg_cli.state import load_active_run, load_run_view

    root = project_root()
    ctx = get_context(args)
    # Explicit --human (command or global) forces human summary.
    force_human = bool(getattr(args, "human", False) or getattr(args, "human_output", False))
    wants_json = bool(ctx and ctx.wants_json) and not force_human
    # Legacy: without --human, state already prints JSON for present runs.
    human = force_human

    if getattr(args, "run_id", None):
        data = load_run_view(root, args.run_id)
        if data is None:
            if wants_json:
                emit_json(
                    failure(
                        "state",
                        "E_RUN_NOT_FOUND",
                        f"no run found: {args.run_id}",
                        next_action="pass a valid --run ID or omg state",
                    )
                )
            else:
                print(f"no run found: {args.run_id}", file=sys.stderr)
            return 1
        if human:
            print_state_human(data)
        elif wants_json:
            emit_json(success("state", data=data))
        else:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    active = load_active_run(root)
    if active is None:
        if wants_json:
            emit_json(
                success(
                    "state",
                    active=None,
                    message="no active run",
                )
            )
        else:
            print("no active run")
        return 0
    if human:
        print_state_human(load_run_view(root, str(active["run_id"])) or active)
    elif wants_json:
        emit_json(success("state", data=active))
    else:
        print(json.dumps(active, indent=2, ensure_ascii=False))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    from omg_cli.cli_envelope import failure, wants_json
    from omg_cli.state import cancel_run

    root = project_root()
    run_id = getattr(args, "run_id", None)
    grace = float(getattr(args, "grace", 2.0))
    try:
        cancelled = cancel_run(root, run_id, kill_grace_s=grace)
    except FileNotFoundError as e:
        if wants_json(args):
            emit_data(
                args,
                "cancel",
                failure(
                    "cancel",
                    "E_RUN_NOT_FOUND",
                    str(e),
                    next_action="start a run or pass --run <id>",
                ),
            )
        else:
            print(f"cancel failed: {e}", file=sys.stderr)
        return 1
    outcome = str(cancelled.get("cancel_outcome") or "cancelled")
    if not wants_json(args):
        if outcome == "already complete":
            print(f"run {cancelled['run_id']} already complete; no cancellation requested")
        elif outcome == "cancellation requested":
            print(f"cancellation requested for run {cancelled['run_id']}")
        else:
            print(f"cancelled run {cancelled['run_id']}")
    emit_data(args, "cancel", cancelled)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Smart resume routing + RESUME.md (research R2 three pillars)."""
    from omg_cli.resume import (
        clear_resume_md,
        format_pack_human,
        format_pack_json,
        route_resume,
    )

    root = project_root()
    if getattr(args, "clear", False):
        removed = clear_resume_md(root)
        print("cleared RESUME.md" if removed else "no RESUME.md to clear")
        return 0
    code, pack = route_resume(
        root,
        run_id=getattr(args, "run_id", None),
        write_md=not getattr(args, "no_write", False),
    )
    as_json = bool(
        getattr(args, "json", False)
        or getattr(args, "json_output", False)
    )
    if as_json:
        sys.stdout.write(format_pack_json(pack))
    else:
        sys.stdout.write(format_pack_human(pack))
    return int(code)


def cmd_session(args: argparse.Namespace) -> int:
    """Expose Grok's exact create/resume/continue/fork argv contract."""
    from omg_cli.host_session import (
        HostSessionError,
        allocate_host_session,
        session_route_argv,
    )

    action = getattr(args, "session_action", None)
    try:
        if action == "allocate":
            binding = allocate_host_session()
            result: object = {
                "session_id": binding.session_id,
                "argv": binding.launch_argv(),
                "route": "create",
            }
        elif action == "route":
            route = session_route_argv(
                create_session_id=getattr(args, "session_id", None),
                resume_session_id=getattr(args, "resume_session_id", None),
                continue_best_effort=bool(getattr(args, "continue_best_effort", False)),
                fork_session=bool(getattr(args, "fork_session", False)),
                new_session_id=getattr(args, "new_session_id", None),
                existing_session_ids=getattr(args, "existing_session_ids", None) or (),
            )
            result = {
                "argv": route,
                "best_effort": route[:1] == ["--continue"],
                "named_fork": "--fork-session" in route,
            }
        else:
            print("omg session: action required", file=sys.stderr)
            return 2
    except HostSessionError as exc:
        print(f"omg session: {exc}", file=sys.stderr)
        return 1
    emit_data(args, "session", result)
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    """Create an immutable bounded session recovery pack."""
    import hashlib

    from omg_cli.session_recovery import SessionRecoveryError, recover_session

    root = project_root()
    source = Path(args.source).expanduser()
    destination = getattr(args, "output", None)
    if destination is None:
        source_key = hashlib.sha256(
            str(source.resolve(strict=False)).encode()
        ).hexdigest()[:16]
        destination_path = root / ".omg" / "state" / "recovery" / f"manual-{source_key}"
    else:
        destination_path = Path(destination).expanduser()
        if not destination_path.is_absolute():
            destination_path = root / destination_path
    try:
        result = recover_session(
            source,
            destination_path,
            repository_id="OMG",
            host="grok",
        )
    except (OSError, ValueError, SessionRecoveryError) as exc:
        print(f"omg recover: {exc}", file=sys.stderr)
        return 1
    emit_data(args, "recover", result)
    return 0 if result.get("error") is None else 1



def register_run_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register run-family argparse parsers (#29 Phase 4').

    Commands: state, cancel, resume, session, recover.
    """
    p_state = sub.add_parser(
        "state",
        parents=[common],
        help="show active run (or --run <id>)",
    )
    p_state.add_argument("--run", dest="run_id", default=None, help="specific run_id")
    p_state.add_argument(
        "--human",
        action="store_true",
        help="one-screen human summary (mode/status/verified/next)",
    )
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

    p_resume = sub.add_parser(
        "resume",
        parents=[common],
        help="smart resume routing + write/clear .omg/state/RESUME.md",
    )
    p_resume.add_argument("--run", dest="run_id", default=None, help="specific run_id")
    p_resume.add_argument(
        "--clear",
        action="store_true",
        help="delete RESUME.md after successful continuation",
    )
    p_resume.add_argument(
        "--no-write",
        action="store_true",
        help="print pack only; do not write RESUME.md",
    )
    # --json inherited from common (json_output)
    p_resume.set_defaults(func=cmd_resume)

    p_session = sub.add_parser(
        "session",
        parents=[common],
        help="build exact Grok create/resume/continue/fork session argv",
    )
    session_sub = p_session.add_subparsers(dest="session_action")
    p_session_allocate = session_sub.add_parser(
        "allocate",
        parents=[common],
        help="allocate a new canonical Grok session UUID",
    )
    p_session_allocate.set_defaults(func=cmd_session, session_action="allocate")
    p_session_route = session_sub.add_parser(
        "route",
        parents=[common],
        help="validate one exact Grok host-session route",
    )
    route = p_session_route.add_mutually_exclusive_group(required=True)
    route.add_argument("--session-id", help="new session UUID")
    route.add_argument("--resume", dest="resume_session_id", help="existing session UUID")
    route.add_argument(
        "--continue",
        dest="continue_best_effort",
        action="store_true",
        help="use Grok's best-effort continuation route",
    )
    p_session_route.add_argument(
        "--fork-session",
        action="store_true",
        help="fork the selected resume/continue route",
    )
    p_session_route.add_argument(
        "--new-session-id",
        help="new child UUID required for a fork",
    )
    p_session_route.add_argument(
        "--existing-session-id",
        dest="existing_session_ids",
        action="append",
        default=[],
        help="known UUID that the child must not reuse (repeatable)",
    )
    p_session_route.set_defaults(func=cmd_session, session_action="route")
    p_session.set_defaults(func=cmd_session)

    p_recover = sub.add_parser(
        "recover",
        parents=[common],
        help="recover a bounded immutable session JSONL suffix",
    )
    p_recover.add_argument("source", help="regular JSONL source file (symlinks refused)")
    p_recover.add_argument(
        "--output",
        default=None,
        help="recovery directory (default: .omg/state/recovery/manual-<hash>)",
    )
    p_recover.set_defaults(func=cmd_recover)


__all__ = [
    "register_run_parsers",
    "_print_state_human",
    "cmd_cancel",
    "cmd_recover",
    "cmd_resume",
    "cmd_session",
    "cmd_state",
    "print_state_human",
]
