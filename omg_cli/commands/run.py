"""Run-family CLI handlers (#29 Phase 2).

Commands: state, cancel, resume, session, recover.
Parser construction remains in ``main.build_parser``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
    from omg_cli.state import cancel_run

    root = project_root()
    run_id = getattr(args, "run_id", None)
    grace = float(getattr(args, "grace", 2.0))
    try:
        cancelled = cancel_run(root, run_id, kill_grace_s=grace)
    except FileNotFoundError as e:
        print(f"cancel failed: {e}", file=sys.stderr)
        return 1
    outcome = str(cancelled.get("cancel_outcome") or "cancelled")
    if outcome == "already complete":
        print(f"run {cancelled['run_id']} already complete; no cancellation requested")
    elif outcome == "cancellation requested":
        print(f"cancellation requested for run {cancelled['run_id']}")
    else:
        print(f"cancelled run {cancelled['run_id']}")
    print(json.dumps(cancelled, indent=2, ensure_ascii=False))
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
    print(json.dumps(result, indent=2, ensure_ascii=False))
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
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("error") is None else 1


__all__ = [
    "_print_state_human",
    "cmd_cancel",
    "cmd_recover",
    "cmd_resume",
    "cmd_session",
    "cmd_state",
    "print_state_human",
]
