# omg_cli/main.py
"""omg CLI argparse router."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _project_root() -> Path:
    return Path.cwd().resolve()


def cmd_setup(args: argparse.Namespace) -> int:
    from omg_cli.setup_cmd import run_setup

    return run_setup(
        _project_root(),
        install_rules=not getattr(args, "no_global_rules", False),
        install_hook=not getattr(args, "no_global_hook", False),
    )


def cmd_install_hook(args: argparse.Namespace) -> int:
    from omg_cli.hook_install import main as hook_install_main

    return hook_install_main(["--remove"] if getattr(args, "remove", False) else [])


def cmd_doctor(args: argparse.Namespace) -> int:
    from omg_cli.doctor import run_doctor

    return run_doctor(strict=bool(getattr(args, "strict", False)))


def cmd_note(args: argparse.Namespace) -> int:
    from omg_cli.note import run_note

    return run_note(
        " ".join(args.text),
        priority=bool(getattr(args, "priority", False)),
        show=bool(getattr(args, "show", False)),
        prune=bool(getattr(args, "prune", False)),
    )


def cmd_update(args: argparse.Namespace) -> int:
    from omg_cli.update_cmd import run_update

    return run_update()


def cmd_uninstall(args: argparse.Namespace) -> int:
    from omg_cli.uninstall_cmd import run_uninstall

    return run_uninstall(yes=bool(getattr(args, "yes", False)))


def _print_state_human(data: dict) -> None:
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


def cmd_state(args: argparse.Namespace) -> int:
    from omg_cli.state import load_active_run, load_run_view

    root = _project_root()
    human = bool(getattr(args, "human", False))
    if getattr(args, "run_id", None):
        data = load_run_view(root, args.run_id)
        if data is None:
            print(f"no run found: {args.run_id}", file=sys.stderr)
            return 1
        if human:
            _print_state_human(data)
        else:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    active = load_active_run(root)
    if active is None:
        print("no active run")
        return 0
    if human:
        _print_state_human(load_run_view(root, str(active["run_id"])) or active)
    else:
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

    root = _project_root()
    if getattr(args, "clear", False):
        removed = clear_resume_md(root)
        print("cleared RESUME.md" if removed else "no RESUME.md to clear")
        return 0
    code, pack = route_resume(
        root,
        run_id=getattr(args, "run_id", None),
        write_md=not getattr(args, "no_write", False),
    )
    if getattr(args, "json", False):
        sys.stdout.write(format_pack_json(pack))
    else:
        sys.stdout.write(format_pack_human(pack))
    return int(code)


def cmd_wiki(args: argparse.Namespace) -> int:
    from omg_cli.wiki import WikiError, ingest, list_pages, query

    root = _project_root()
    action = getattr(args, "wiki_action", None)
    try:
        if action == "ingest":
            tags = []
            raw_tags = getattr(args, "tags", None)
            if raw_tags:
                tags = [t.strip() for t in str(raw_tags).split(",") if t.strip()]
            body = getattr(args, "text", None) or ""
            if getattr(args, "file", None):
                body = Path(args.file).read_text(encoding="utf-8")
            result = ingest(
                root,
                title=str(args.title),
                body=body,
                tags=tags,
                source=getattr(args, "source", None),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if action == "list":
            print(json.dumps(list_pages(root), indent=2, ensure_ascii=False))
            return 0
        if action == "query":
            hits = query(root, str(args.q), limit=int(getattr(args, "limit", 20)))
            print(json.dumps(hits, indent=2, ensure_ascii=False))
            return 0
    except WikiError as e:
        print(f"wiki failed: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"wiki failed: {e}", file=sys.stderr)
        return 1
    print("usage: omg wiki {ingest,list,query} …", file=sys.stderr)
    return 2


def cmd_hud(args: argparse.Namespace) -> int:
    from omg_cli.hud import hud_line, hud_pack

    root = _project_root()
    rid = getattr(args, "run_id", None)
    if getattr(args, "json", False):
        print(json.dumps(hud_pack(root, rid), indent=2, ensure_ascii=False))
    else:
        print(hud_line(root, rid))
    return 0


def cmd_lsp(args: argparse.Namespace) -> int:
    from omg_cli.lsp_tools import probe_tools

    action = getattr(args, "lsp_action", None)
    if action == "status" or action is None:
        print(json.dumps(probe_tools(), indent=2, ensure_ascii=False))
        return 0
    if action in {"check", "symbols", "diagnostics"}:
        status = probe_tools()
        result = {
            "ok": False,
            "ownership": status["ownership"],
            "status": "semantic_proxy_unsupported",
            "operation": action,
            "path": str(Path(args.path)),
            "semantic_proxy_operations": status["semantic_proxy_operations"],
            "error": (
                "semantic LSP operations belong to Grok; OMG only validates "
                "the public .lsp.json registration"
            ),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1
    print(
        "usage: omg lsp {status,check,symbols,diagnostics} …",
        file=sys.stderr,
    )
    return 2


def cmd_interview(args: argparse.Namespace) -> int:
    """Run the deterministic resumable requirements interview primitive."""
    from omg_cli.interview import (
        InterviewError,
        InterviewIncomplete,
        answer_interview,
        close_interview,
        interview_status,
        pressure_pass_interview,
        start_interview,
    )

    root = _project_root()
    action = getattr(args, "interview_action", None)
    try:
        if action == "start":
            result = start_interview(
                root,
                " ".join(args.task or []).strip(),
                profile=args.profile,
                force=bool(getattr(args, "force", False)),
            )
        elif action == "answer":
            result = answer_interview(
                root,
                args.run_id,
                args.text,
                question_id=getattr(args, "question_id", None),
            )
        elif action == "pressure-pass":
            result = pressure_pass_interview(root, args.run_id, args.text)
        elif action == "close":
            result = close_interview(root, args.run_id)
        elif action == "status":
            result = interview_status(root, getattr(args, "run_id", None))
        else:
            print("omg interview: action required", file=sys.stderr)
            return 2
    except InterviewIncomplete as exc:
        print(json.dumps(exc.result, indent=2, ensure_ascii=False))
        return 1
    except (InterviewError, RuntimeError) as exc:
        print(f"omg interview: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_goal(args: argparse.Namespace) -> int:
    """Durable hash-chained goal ledger (ultragoal primitive)."""
    from omg_cli.goals import (
        GoalError,
        GoalRepairRefused,
        block_story,
        checkpoint,
        complete_story,
        init_goal,
        link_run,
        list_goals,
        repair_goal,
        resume_story,
        start_story,
        goal_status,
        verify_goal,
    )

    root = _project_root()
    action = getattr(args, "goal_action", None)
    try:
        if action == "init":
            stories_raw = json.loads(args.stories_json)
            if not isinstance(stories_raw, list):
                raise GoalError("--stories-json must be a JSON array")
            result = init_goal(
                root,
                args.goal_id,
                stories_raw,
                title=getattr(args, "title", None),
                objective=getattr(args, "objective", None),
                source_spec_hash=getattr(args, "source_spec_hash", None),
                source_plan_hash=getattr(args, "source_plan_hash", None),
            )
        elif action == "status":
            if getattr(args, "goal_id", None):
                result = goal_status(root, args.goal_id)
            else:
                result = {"goals": list_goals(root)}
        elif action == "link-run":
            result = link_run(root, args.goal_id, args.run_id)
        elif action == "start-story":
            result = start_story(root, args.goal_id, args.story_id)
        elif action == "checkpoint":
            result = checkpoint(
                root,
                args.goal_id,
                args.story_id,
                evidence_path=args.evidence,
                message=args.message,
            )
        elif action == "block-story":
            result = block_story(
                root,
                args.goal_id,
                args.story_id,
                reason=args.reason,
                next_action=getattr(args, "next_action", None),
            )
        elif action == "resume-story":
            result = resume_story(root, args.goal_id, args.story_id)
        elif action == "complete-story":
            result = complete_story(root, args.goal_id, args.story_id)
        elif action == "verify":
            result = verify_goal(
                root,
                args.goal_id,
                run_id=getattr(args, "run_id", None),
            )
        elif action == "repair":
            result = repair_goal(
                root,
                args.goal_id,
                dry_run=bool(getattr(args, "dry_run", False))
                or not bool(getattr(args, "yes", False)),
                yes=bool(getattr(args, "yes", False)),
            )
        else:
            print("omg goal: action required", file=sys.stderr)
            return 2
    except GoalRepairRefused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1
    except (GoalError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"omg goal: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


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
        max_iter=int(max_iter) if max_iter is not None else None,
        dry_run=bool(getattr(args, "dry_run", False)),
        timeout=timeout,
        require_acceptance=require_acceptance,
        resume_run_id=resume,
    )


def cmd_accept(args: argparse.Namespace) -> int:
    """Freeze PRD acceptance commands and run them for active (or --run) run."""
    from omg_cli.acceptance import (
        CommandPolicyError,
        freeze_acceptance,
        freeze_and_run,
        format_commands_review,
        load_frozen_commands,
        load_prd,
        read_manifest_sha256,
        result_path,
    )
    from omg_cli.state import (
        FencingError,
        LifecycleLockError,
        load_active_run,
        load_run,
        set_verified,
    )

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
        # Prefer materializing from clean UltraQA (autopilot QA → accept path).
        try:
            from omg_cli.acceptance import materialize_prd_from_ultraqa

            prd = materialize_prd_from_ultraqa(root, run_id, overwrite=False)
            print(
                f"accept: materialized prd.json from clean ultraqa for {run_id}",
                file=sys.stderr,
            )
        except ValueError as exc:
            print(
                f"accept failed: no prd.json under runs/{run_id}/ "
                f"(and could not materialize from ultraqa: {exc})",
                file=sys.stderr,
            )
            return 1

    dry_run = bool(getattr(args, "dry_run", False))
    review = bool(getattr(args, "review", False))
    yes = bool(getattr(args, "yes", False))
    no_allowlist = bool(getattr(args, "no_allowlist", False))
    extra_allow = list(getattr(args, "allow_cmd", None) or [])

    # --no-allowlist is TTY-only break-glass; floors still apply at run time.
    if no_allowlist:
        if not sys.stdin.isatty():
            print(
                "accept: --no-allowlist is TTY-only break-glass "
                "(non-tty refuses; always-deny floor cannot be bypassed)",
                file=sys.stderr,
            )
            return 2
        print(
            "WARNING: --no-allowlist is break-glass (positive allowlist skipped). "
            "Shells, agent CLIs, python -c, npx, and always-deny bins still blocked.",
            file=sys.stderr,
        )

    # Freeze early so --review can print the exact frozen command list + sha.
    try:
        freeze_acceptance(
            root,
            run_id,
            prd,
            extra_allow=extra_allow or None,
            no_allowlist=no_allowlist,
        )
        commands = load_frozen_commands(root, run_id)
        manifest_sha = read_manifest_sha256(root, run_id)
    except CommandPolicyError as exc:
        print(f"accept policy rejected: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"accept failed: {exc}", file=sys.stderr)
        return 1

    # Always show review block (sha / cwd / numbered shlex) before exec or dry-run.
    print(
        format_commands_review(
            commands,
            root=root,
            run_id=run_id,
            manifest_sha=manifest_sha,
        )
    )

    if dry_run:
        try:
            ok = freeze_and_run(
                root,
                run_id,
                prd,
                dry_run=True,
                extra_allow=extra_allow or None,
                no_allowlist=no_allowlist,
            )
        except CommandPolicyError as exc:
            print(f"accept policy rejected: {exc}", file=sys.stderr)
            return 1
        except (ValueError, FileNotFoundError, OSError) as exc:
            print(f"accept failed: {exc}", file=sys.stderr)
            return 1
        rpath = result_path(root, run_id)
        print(f"acceptance result: {rpath}")
        if rpath.is_file():
            print(rpath.read_text(encoding="utf-8"))
        print("dry_run: commands not executed; verified not set")
        return 0

    # Confirmation gate (policy already enforced at freeze; --yes never skips policy):
    # - non-TTY: require --yes
    # - TTY + --review without --yes: interactive y/N prompt
    # - TTY without --review: execute (operator already invoked accept)
    # - --yes: skip prompt
    if not yes:
        if not sys.stdin.isatty():
            print(
                "accept: non-tty stdin requires --yes to execute acceptance commands "
                "(or use --dry-run)",
                file=sys.stderr,
            )
            return 2
        if review:
            try:
                answer = input("run frozen acceptance commands? [y/N] ").strip().lower()
            except EOFError:
                print("accept: confirmation aborted (EOF)", file=sys.stderr)
                return 2
            if answer not in ("y", "yes"):
                print("accept: aborted (not confirmed)", file=sys.stderr)
                return 2

    try:
        ok = freeze_and_run(
            root,
            run_id,
            prd,
            dry_run=False,
            extra_allow=extra_allow or None,
            no_allowlist=no_allowlist,
        )
    except CommandPolicyError as exc:
        print(f"accept policy rejected: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"accept failed: {exc}", file=sys.stderr)
        return 1

    rpath = result_path(root, run_id)
    print(f"acceptance result: {rpath}")
    if rpath.is_file():
        print(rpath.read_text(encoding="utf-8"))

    if not ok:
        print("acceptance FAILED", file=sys.stderr)
        return 1

    # set_verified auto-acquires a strict-v2 lease when none is passed.
    # FencingError is a PermissionError subclass; LifecycleLockError covers
    # busy/order failures — never dump a traceback for operator CLI.
    try:
        verified = set_verified(root, run_id, force=False)
    except (PermissionError, FencingError, LifecycleLockError) as exc:
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
    require_squash = bool(getattr(args, "require_squash", False))
    try:
        result = integrate_results(
            root, run_id, dry_run=dry_run, require_squash=require_squash
        )
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


def cmd_team(args: argparse.Namespace) -> int:
    """Experimental tmux team plane (D1/D3) + staged pipeline (D2) + scale/resume/ralph (D4).

    Gate: OMG_EXPERIMENTAL_TMUX_TEAM=1. Pipeline is THIN glue over start/collect;
    never sets verified.
    """
    from omg_cli.team.plane import (
        TeamError,
        TeamGateError,
        collect_team,
        format_status_table,
        start_team,
        status_locked_view,
        stop_team,
        team_status,
    )
    from omg_cli.team.pipeline import (
        TeamPipelineError,
        run_team_pipeline,
    )
    from omg_cli.team.roles import UnknownRoleError
    from omg_cli.team.routing import RoutingError, parse_routing_json
    from omg_cli.team.scaling import resume_team, scale_team

    root = _project_root()
    action = getattr(args, "team_action", None)

    try:
        if action == "start":
            goal = getattr(args, "goal", None) or ""
            tasks_json = getattr(args, "tasks_json", None)
            if not tasks_json:
                print("omg team start: --tasks-json required", file=sys.stderr)
                return 2
            routing_raw = getattr(args, "routing", None)
            routing = parse_routing_json(routing_raw) if routing_raw else None
            # parse_routing_json returns None for empty; keep None so zero-config
            # stays D1. Non-empty --routing enables multi-CLI floors.
            meta = start_team(
                goal,
                tasks_json,
                root=root,
                run_id=getattr(args, "run_id", None),
                dry_run=bool(getattr(args, "dry_run", False)),
                yolo=bool(getattr(args, "yolo", False)),
                safe=bool(getattr(args, "safe", False)),
                force=bool(getattr(args, "force", False)),
                routing=routing,
            )
            print(json.dumps(meta, indent=2, ensure_ascii=False))
            return 0
        if action == "run":
            # Staged FSM driver (plan→prd→exec→verify→fix). Decomposition is
            # the leader's / ralplan's job; this only sequences + gates verify.
            # --ralph wraps exec→verify→fix in a bounded outer max_iter loop.
            goal = getattr(args, "goal", None) or ""
            tasks_json = getattr(args, "tasks_json", None)
            tasks_path = getattr(args, "tasks_path", None)
            if not tasks_json and not tasks_path:
                print(
                    "omg team run: --tasks-json or --tasks-path required",
                    file=sys.stderr,
                )
                return 2
            routing_raw = getattr(args, "routing", None)
            routing = parse_routing_json(routing_raw) if routing_raw else None
            result = run_team_pipeline(
                goal,
                root=root,
                tasks_json=tasks_json,
                tasks_path=tasks_path,
                dry_run=bool(getattr(args, "dry_run", False)),
                max_fix=int(getattr(args, "max_fix", 3) or 3),
                force=bool(getattr(args, "force", False)),
                run_id=getattr(args, "run_id", None),
                yolo=bool(getattr(args, "yolo", False)),
                safe=bool(getattr(args, "safe", False)),
                routing=routing,
                ralph=bool(getattr(args, "ralph", False)),
                max_iter=getattr(args, "max_iter", None),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            phase = str(result.get("phase") or "")
            if phase == "complete":
                return 0
            if phase == "blocked":
                return 2
            # failed (or unexpected) — not verified; exit 1
            return 1
        if action == "scale":
            add = getattr(args, "add", None)
            remove = getattr(args, "remove", None)
            result = scale_team(
                root,
                getattr(args, "run_id", None),
                add=add,
                remove=remove,
                dry_run=bool(getattr(args, "dry_run", False)),
                tasks_json=getattr(args, "tasks_json", None),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if action == "resume":
            result = resume_team(
                root,
                getattr(args, "run_id", None),
            )
            if getattr(args, "as_json", False) or True:
                # Always JSON (operator machine-readable); --json kept for symmetry
                print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if action == "status":
            st = team_status(
                root,
                getattr(args, "run_id", None),
            )
            if getattr(args, "as_json", False):
                print(
                    json.dumps(
                        status_locked_view(st),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print(format_status_table(st))
            return 0
        if action == "collect":
            result = collect_team(
                root,
                getattr(args, "run_id", None),
                force_seal=bool(getattr(args, "force", False)),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            # Never sets verified; integrate status drives exit
            integrate = result.get("integrate") or {}
            status = integrate.get("status")
            if status == "ok":
                return 0
            if status == "missing":
                return 1
            return 1
        if action == "stop":
            result = stop_team(
                root,
                getattr(args, "run_id", None),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if not result.get("errors") else 1
        print(f"omg team: unknown action {action!r}", file=sys.stderr)
        return 2
    except TeamGateError as exc:
        print(f"omg team: {exc}", file=sys.stderr)
        return 2
    except TeamPipelineError as exc:
        print(f"omg team: {exc}", file=sys.stderr)
        return 1
    except (RoutingError, UnknownRoleError) as exc:
        # FLOOR rejections — fail closed at team start (not silent).
        print(f"omg team: {exc}", file=sys.stderr)
        return 2
    except TeamError as exc:
        print(f"omg team: {exc}", file=sys.stderr)
        return 1


def cmd_worker(args: argparse.Namespace) -> int:
    """prepare/seal worktrees and ULW result envelopes (no-shell bridge)."""
    from omg_cli.state import load_active_run, load_run
    from omg_cli.workers import (
        WorkerError,
        build_ownership_manifest,
        join_worker_results,
        load_ownership_manifest,
        prepare_owned_tasks,
        prepare_task,
        seal_all_tasks,
        seal_task,
    )

    root = _project_root()
    action = getattr(args, "worker_action", None)
    task_id = getattr(args, "task_id", None)

    run_id = getattr(args, "run_id", None)
    if not run_id:
        active = load_active_run(root)
        if active is None:
            print(
                "omg worker: no active run (pass --run ID)",
                file=sys.stderr,
            )
            return 1
        run_id = active["run_id"]

    if load_run(root, run_id) is None:
        print(f"omg worker: no run found: {run_id}", file=sys.stderr)
        return 1

    try:
        if action == "own":
            tasks = json.loads(args.tasks_json)
            if not isinstance(tasks, list):
                raise WorkerError("--tasks-json must be a JSON array")
            manifest = build_ownership_manifest(root, run_id, tasks)
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
            return 0
        if action == "prepare-owned":
            paths = prepare_owned_tasks(root, run_id)
            print(
                json.dumps(
                    {"run_id": run_id, "worktrees": [str(p) for p in paths]},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        if action == "join":
            result = join_worker_results(root, run_id)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("complete") else 1
        if action == "manifest":
            print(
                json.dumps(
                    load_ownership_manifest(root, run_id),
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        if action == "seal" and getattr(args, "seal_all", False):
            results = seal_all_tasks(
                root,
                run_id,
                force=bool(getattr(args, "force", False)),
            )
            sealed = already = skipped = failed = errored = 0
            # Per-task table
            print(f"{'task_id':<24} {'status':<22} head_sha/detail")
            print("-" * 72)
            for row in results:
                tid = str(row.get("task_id") or "")
                st = str(row.get("status") or "")
                if st == "sealed":
                    sealed += 1
                    detail = str(row.get("head_sha") or "")
                    if row.get("changed_files_count") is not None:
                        detail = f"{detail} files={row['changed_files_count']}"
                elif st == "already-sealed":
                    already += 1
                    detail = ""
                elif st == "skipped-no-worktree":
                    skipped += 1
                    detail = ""
                elif st == "failed":
                    failed += 1
                    detail = str(
                        row.get("detail") or row.get("error") or row.get("head_sha") or ""
                    )
                elif st == "error":
                    errored += 1
                    detail = str(row.get("error") or "")
                else:
                    detail = str(row.get("error") or row.get("head_sha") or "")
                print(f"{tid:<24} {st:<22} {detail}")
            print(
                f"sealed {sealed}, already {already}, skipped {skipped}, "
                f"failed {failed}, error {errored}"
            )
            # Non-benign: failed envelope or exception path
            return 1 if (failed or errored) else 0
        if not task_id:
            print("omg worker: --task ID required", file=sys.stderr)
            return 2
        if action == "prepare":
            wt = prepare_task(root, run_id, task_id)
            print(f"omg worker prepare: task={task_id} worktree={wt}")
            return 0
        if action == "seal":
            env = seal_task(
                root,
                run_id,
                task_id,
                message=str(getattr(args, "message", None) or "omg seal"),
                status=str(getattr(args, "status", None) or "ok"),
                evidence=str(getattr(args, "evidence", None) or ""),
            )
            print(f"omg worker seal: task={task_id} status={env.get('status')}")
            print(json.dumps(env, indent=2, ensure_ascii=False))
            return 0 if env.get("status") == "ok" else 1
        print(f"omg worker: unknown action {action!r}", file=sys.stderr)
        return 2
    except (WorkerError, json.JSONDecodeError) as exc:
        print(f"omg worker: {exc}", file=sys.stderr)
        return 1


def cmd_review(args: argparse.Namespace) -> int:
    """Hash-bound structured review gate (code-reviewer + architect)."""
    from omg_cli.review import ReviewError, run_structured_review

    root = _project_root()
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

    root = _project_root()
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
        start_autopilot,
        status_autopilot,
        transition,
    )

    root = _project_root()
    action = getattr(args, "autopilot_action", None)
    try:
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
        else:
            print("omg autopilot: action required", file=sys.stderr)
            return 2
    except (AutopilotError, json.JSONDecodeError, RuntimeError) as exc:
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


def cmd_mcp_server(args: argparse.Namespace) -> int:
    """Run focused stdio MCP server (sets OMG_MCP_SERVER=1)."""
    from omg_cli.acceptance import MCP_SERVER_ENV
    from omg_cli.mcp.server import run_stdio_server

    os.environ[MCP_SERVER_ENV] = "1"
    root = _project_root()
    if getattr(args, "root", None):
        root = Path(args.root).resolve()
    return int(run_stdio_server(root=root))


def cmd_mcp_install(args: argparse.Namespace) -> int:
    """Print or run ``grok mcp add omg omg -- mcp-server``."""
    scope = getattr(args, "scope", None) or "user"
    argv = ["grok", "mcp", "add", "omg", "omg", "--", "mcp-server"]
    if scope in ("user", "project"):
        # Insert --scope after add name for readability if grok supports it.
        argv = [
            "grok",
            "mcp",
            "add",
            "omg",
            "omg",
            "--scope",
            scope,
            "--",
            "mcp-server",
        ]
    if getattr(args, "print_only", False) or getattr(args, "dry_run", False):
        print(" ".join(argv))
        return 0
    import shutil
    import subprocess

    grok = shutil.which("grok")
    if not grok:
        print(
            "grok not on PATH; run manually:\n  " + " ".join(argv),
            file=sys.stderr,
        )
        return 1
    # Rebuild with absolute-ish omg entry if available
    omg_bin = shutil.which("omg") or "omg"
    cmd = [
        grok,
        "mcp",
        "add",
        "omg",
        omg_bin,
        "--scope",
        scope,
        "--",
        "mcp-server",
    ]
    print("running:", " ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)


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


def _read_json_path(path: Path | str, *, label: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not readable JSON: {exc}") from exc


def _write_json_path(path: Path | str, value: object) -> Path:
    from omg_cli.contracts.writer_chain import canonical_json_bytes

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_json_bytes(value))
    return target


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

    root = _project_root()
    source = Path(args.source).expanduser()
    destination = getattr(args, "output", None)
    if destination is None:
        source_key = hashlib.sha256(str(source.resolve(strict=False)).encode()).hexdigest()[:16]
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


def cmd_memory(args: argparse.Namespace) -> int:
    """Operate the deterministic, redacted project fact store."""
    from datetime import datetime, timezone

    from omg_cli.project_memory import (
        export_memory,
        import_memory,
        rescan_memory,
        search_memory,
        upsert_fact,
    )

    root = _project_root()
    action = getattr(args, "memory_action", None)
    try:
        if action == "put":
            observed_at = getattr(args, "updated_at", None) or datetime.now(
                timezone.utc
            ).isoformat().replace("+00:00", "Z")
            result: object = upsert_fact(
                root,
                key=args.key,
                value=args.value,
                source="user",
                updated_at=observed_at,
            )
        elif action == "search":
            result = search_memory(root, args.query, limit=args.limit)
        elif action in {"show", "export"}:
            store = export_memory(root)
            result = store
            if getattr(args, "output", None):
                target = _write_json_path(args.output, result)
                print(
                    json.dumps(
                        {"path": str(target), "facts": len(store["facts"])},
                        indent=2,
                    )
                )
                return 0
        elif action == "import":
            value = _read_json_path(args.file, label="memory import")
            if not isinstance(value, dict):
                raise ValueError("memory import must be a JSON object")
            result = import_memory(root, value)
        elif action == "rescan":
            value = _read_json_path(args.file, label="memory rescan")
            facts = value.get("facts") if isinstance(value, dict) else value
            if not isinstance(facts, list) or not all(isinstance(row, dict) for row in facts):
                raise ValueError("memory rescan must contain a JSON fact array")
            observed_at = getattr(args, "observed_at", None) or datetime.now(
                timezone.utc
            ).isoformat().replace("+00:00", "Z")
            result = rescan_memory(root, facts, observed_at=observed_at)
        else:
            print("omg memory: action required", file=sys.stderr)
            return 2
    except (OSError, ValueError) as exc:
        print(f"omg memory: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_tracker(args: argparse.Namespace) -> int:
    """Project passive lifecycle journals into the canonical tracker view."""
    from omg_cli.contracts.state_schemas import ContractValidationError
    from omg_cli.runtime_events import read_all_runtime_events
    from omg_cli.tracker import (
        TrackerError,
        load_tracker_projection,
        project_lifecycle_events,
        reconcile_native_inventory,
    )

    root = _project_root()
    action = getattr(args, "tracker_action", None)
    try:
        if action == "status":
            result = load_tracker_projection(root, args.run_id)
            if result is None:
                result = {
                    "run_id": args.run_id,
                    "status": "not_projected",
                    "authoritative": False,
                }
        elif action == "project":
            if getattr(args, "events", None):
                value = _read_json_path(args.events, label="tracker events")
                if isinstance(value, dict):
                    value = value.get("events")
                if not isinstance(value, list) or not all(
                    isinstance(row, dict) for row in value
                ):
                    raise ValueError("tracker events must be a JSON array")
                events = value
            else:
                events = [
                    row
                    for row in read_all_runtime_events(root)
                    if row.get("run_id") == args.run_id
                ]
            result = project_lifecycle_events(
                root,
                run_id=args.run_id,
                generation=args.generation,
                events=events,
            )
        elif action == "reconcile":
            value = _read_json_path(args.inventory, label="native inventory")
            inventory = value.get("inventory") if isinstance(value, dict) else value
            if not isinstance(inventory, list) or not all(
                isinstance(row, dict) for row in inventory
            ):
                raise ValueError("native inventory must be a JSON array")
            result = reconcile_native_inventory(
                root,
                run_id=args.run_id,
                inventory=inventory,
            )
        else:
            print("omg tracker: action required", file=sys.stderr)
            return 2
    except (OSError, ValueError, TrackerError, ContractValidationError) as exc:
        print(f"omg tracker: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    """Create/read generation-fenced compaction checkpoints."""
    from omg_cli.compaction import (
        CompactionError,
        create_compaction_checkpoint,
        load_compaction_checkpoint,
        render_resume_context,
    )
    from omg_cli.contracts.state_schemas import ContractValidationError
    from omg_cli.contracts.writer_chain import sha256_hex

    action = getattr(args, "compact_action", None)
    try:
        if action == "create":
            receipts_value = _read_json_path(args.receipts, label="compaction receipts")
            receipts = (
                receipts_value.get("receipts")
                if isinstance(receipts_value, dict)
                else receipts_value
            )
            recovery = _read_json_path(
                args.recovery_manifest, label="recovery manifest"
            )
            if not isinstance(receipts, list) or not all(
                isinstance(row, dict) for row in receipts
            ):
                raise ValueError("compaction receipts must be a JSON array")
            if not isinstance(recovery, dict):
                raise ValueError("recovery manifest must be a JSON object")
            result: object = create_compaction_checkpoint(
                _project_root(),
                run_id=args.run_id,
                generation=args.generation,
                guidance=Path(args.guidance_file).read_bytes(),
                receipts=receipts,
                recovery_manifest=recovery,
            )
        elif action == "show":
            result = load_compaction_checkpoint(args.path)
        elif action == "render":
            rendered = render_resume_context(load_compaction_checkpoint(args.path))
            guidance = rendered.pop("guidance")
            target = Path(args.guidance_out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(guidance)
            result = {
                **rendered,
                "guidance_path": str(target),
                "guidance_sha256": sha256_hex(guidance),
            }
        else:
            print("omg compact: action required", file=sys.stderr)
            return 2
    except (OSError, ValueError, CompactionError, ContractValidationError) as exc:
        print(f"omg compact: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _notification_config(path: str | None) -> dict:
    from omg_cli.notify import disabled_notification_config, load_notification_config

    if path is None:
        default = _project_root() / ".omg" / "notifications.json"
        if not default.is_file():
            return disabled_notification_config()
        path = str(default)
    return load_notification_config(path)


def cmd_notify(args: argparse.Namespace) -> int:
    """Operate the outbound-only, non-authoritative notification queue."""
    from omg_cli.notify import (
        create_notification_event,
        enqueue_notification,
        process_notification_queue,
    )

    action = getattr(args, "notify_action", None)
    try:
        if action == "status":
            result: object = {
                "config": _notification_config(getattr(args, "config", None)),
                "inbound_listener": False,
                "authoritative": False,
            }
        else:
            nonce = os.environ.get("OMG_NOTIFICATION_OWNER_NONCE", "")
            if not nonce:
                raise ValueError("OMG_NOTIFICATION_OWNER_NONCE is required")
            owner = {
                "owner_id": args.owner_id,
                "generation": args.generation,
                "owner_nonce": nonce,
            }
            if action == "send":
                event = create_notification_event(
                    severity=args.severity,
                    title=args.title,
                    message=args.message,
                    owner_id=args.owner_id,
                    generation=args.generation,
                    owner_nonce=nonce,
                    stable_source_id=getattr(args, "stable_source_id", None),
                )
                result = enqueue_notification(
                    _project_root(),
                    event,
                    owner=owner,
                    max_attempts=args.max_attempts,
                )
            elif action == "process":
                result = process_notification_queue(
                    _project_root(),
                    _notification_config(getattr(args, "config", None)),
                    owner=owner,
                    max_records=args.max_records,
                    rate_limit_per_second=args.rate_limit,
                )
            else:
                print("omg notify: action required", file=sys.stderr)
                return 2
    except (OSError, ValueError) as exc:
        print(f"omg notify: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_native_status(args: argparse.Namespace) -> int:
    """Report only public native UI/workflow observations."""
    from omg_cli.sidecar import native_dashboard_status
    from omg_cli.workflows.grok_adapter import (
        assess_native_capability,
        safe_headless_probe,
    )

    result = {
        "native_dashboard": native_dashboard_status(),
        "native_workflow": assess_native_capability(_project_root()),
        "headless_probe": safe_headless_probe(
            timeout_seconds=float(getattr(args, "timeout", 5.0))
        )
        if bool(getattr(args, "probe", False))
        else {
            "attempted": False,
            "status": "optional_unclaimed",
            "note": "pass --probe for bounded help-only observation",
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _workflow_receipts(value: object) -> dict[str, dict]:
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        value = value["results"]
    if isinstance(value, list):
        rows = value
        mapped: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
                raise ValueError("workflow receipt rows require task_id")
            if row["task_id"] in mapped:
                raise ValueError("workflow receipt task_id is duplicated")
            mapped[row["task_id"]] = row
        return mapped
    if isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(row, dict) for key, row in value.items()
    ):
        mapped = {}
        for key, row in value.items():
            embedded = row.get("task_id")
            if embedded is not None and embedded != key:
                raise ValueError("workflow receipt map key differs from task_id")
            mapped[key] = row
        return mapped
    raise ValueError("workflow receipts must be a task map or result array")


def cmd_workflow(args: argparse.Namespace) -> int:
    """Compile, install, plan, and reconcile repository-workflow/v1 runs."""
    from omg_cli.workflows import (
        build_plan,
        install_workflow,
        list_workflows,
        resolve_workflow,
        run_workflow,
    )
    from omg_cli.workflows.registry import WorkflowRegistryError
    from omg_cli.workflows.review import (
        validate_success_task_receipt,
        validate_task_receipt_identity,
    )
    from omg_cli.workflows.schema import WorkflowSchemaError

    root = _project_root()
    action = getattr(args, "workflow_action", None)
    try:
        if action == "install":
            result: object = install_workflow(root, Path(args.file))
        elif action == "list":
            result = list_workflows(root, name=getattr(args, "name", None))
        elif action == "show":
            result = resolve_workflow(root, args.name, getattr(args, "version", None))
        elif action in {"plan", "run"}:
            definition = resolve_workflow(
                root, args.name, getattr(args, "version", None)
            )
            workflow_input = _read_json_path(args.input, label="workflow input")
            if not isinstance(workflow_input, dict):
                raise ValueError("workflow input must be a JSON object")
            if action == "plan":
                result = build_plan(
                    definition,
                    workflow_input,
                    repository_id="OMG",
                    run_generation=args.generation,
                )
            else:
                receipt_value = _read_json_path(
                    args.receipts, label="workflow receipts"
                )
                receipts = _workflow_receipts(receipt_value)
                receipt_plan = build_plan(
                    definition,
                    workflow_input,
                    repository_id="OMG",
                    run_generation=args.generation,
                )
                expected_tasks = {
                    task["task_id"]: task for task in receipt_plan["tasks"]
                }
                missing_receipts = sorted(set(expected_tasks) - set(receipts))
                if missing_receipts:
                    raise ValueError(
                        f"missing workflow receipts: {missing_receipts!r}"
                    )
                for task_id, receipt in receipts.items():
                    task = expected_tasks.get(task_id)
                    if task is None:
                        raise ValueError(f"foreign workflow receipt: {task_id}")
                    validate_task_receipt_identity(receipt_plan, task, receipt)
                    validate_success_task_receipt(
                        definition,
                        receipt_plan,
                        task,
                        receipt,
                        root=root,
                    )

                def execute_task(task: dict, _context: dict) -> dict:
                    receipt = receipts.get(task["task_id"])
                    if receipt is None:
                        raise ValueError(f"missing workflow receipt: {task['task_id']}")
                    return receipt

                result = run_workflow(
                    root,
                    definition,
                    workflow_input,
                    execute_task=execute_task,
                    repository_id="OMG",
                    run_generation=args.generation,
                    repository_policy=args.repository_permission,
                    host_capabilities=args.host_capability,
                    launch_receipt_permissions=args.launch_permission,
                    allowed_mcp=args.allow_mcp,
                    allowed_write_paths=args.allow_write_path,
                )
        else:
            print("omg workflow: action required", file=sys.stderr)
            return 2
    except (OSError, ValueError, WorkflowRegistryError, WorkflowSchemaError) as exc:
        print(f"omg workflow: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if action == "run":
        return 0 if isinstance(result, dict) and result.get("terminal") == "ship" else 1
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Report independent capability tiers without inferring host health."""
    import importlib.util

    from omg_cli import __version__
    from omg_cli.contracts.capability_schema import CAPABILITY_TIERS
    from omg_cli.lsp_tools import registration_status
    from omg_cli.sidecar import native_dashboard_status
    from omg_cli.workflows.grok_adapter import assess_native_capability

    root = _project_root()
    lock_path = root / "omg_capabilities.lock.json"
    try:
        lock = (
            _read_json_path(lock_path, label="capability lock")
            if lock_path.is_file()
            else None
        )
        lsp = registration_status(root)
        workflow = assess_native_capability(root)
        notification = _notification_config(
            getattr(args, "notification_config", None)
        )
    except (OSError, ValueError) as exc:
        print(f"omg capabilities: {exc}", file=sys.stderr)
        return 1
    mcp_installed = importlib.util.find_spec("omg_cli.mcp.server") is not None
    workflow_installed = importlib.util.find_spec("omg_cli.workflows.runner") is not None
    result = {
        "schema": "omg-capability-status/v1",
        "tiers": list(CAPABILITY_TIERS),
        "version": __version__,
        "surfaces": {
            "mcp": {
                "configured": (root / ".mcp.json").is_file(),
                "installed": mcp_installed,
                "enabled": False,
                "loadable": mcp_installed,
                "observed": False,
                "healthy": False,
                "verified": False,
                "classification": "native_substitute",
                "note": "fresh Grok session evidence is required above configured/loadable",
            },
            "lsp": {
                "configured": lsp["registered"],
                "installed": any(row["command_available"] for row in lsp["servers"]),
                "enabled": False,
                "loadable": lsp["configuration_valid"],
                "observed": lsp["host_observed"],
                "healthy": lsp["healthy"],
                "verified": lsp["healthy"],
                "classification": "host_owned",
                "status": lsp["status"],
            },
            "repository_workflow": {
                "configured": True,
                "installed": workflow_installed,
                "enabled": True,
                "loadable": workflow_installed,
                "observed": False,
                "healthy": False,
                "verified": False,
                "classification": "native_substitute",
                "scope": "product-owned runner only",
            },
            "grok_native_workflow": {
                "configured": workflow["local_bundle_observed"],
                "installed": workflow["local_bundle_observed"],
                "enabled": False,
                "loadable": False,
                "observed": workflow["fresh_invocation_observed"],
                "healthy": workflow["semantic_claim"],
                "verified": workflow["semantic_claim"],
                "classification": "optional_unclaimed",
                "status": workflow["status"],
            },
            "notifications": {
                "configured": notification["enabled"],
                "installed": True,
                "enabled": notification["enabled"],
                "loadable": True,
                "observed": False,
                "healthy": False,
                "verified": False,
                "authoritative": False,
                "classification": "native_substitute",
            },
            "native_dashboard": {
                "configured": False,
                "installed": False,
                "enabled": False,
                "loadable": False,
                "observed": False,
                "healthy": False,
                "verified": False,
                "classification": "optional_unclaimed",
                "status": native_dashboard_status(),
            },
        },
        "lock": lock,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_parity(args: argparse.Namespace) -> int:
    """Delegate run-manifest operations and release-bundle readback."""
    from omg_cli.contracts.release_transaction import verify_release_bundle_files
    from omg_cli.contracts.run_manifest import main as run_manifest_main
    from omg_cli.contracts.state_schemas import ContractValidationError
    from omg_cli.contracts.writer_chain import sha256_hex

    action = getattr(args, "parity_action", None)
    if action == "run":
        return int(run_manifest_main(list(getattr(args, "manifest_args", None) or [])))
    if action != "release-readback":
        print("omg parity: action required", file=sys.stderr)
        return 2
    root = _project_root()
    try:
        manifest_path = Path(args.manifest).resolve()
        relative = manifest_path.relative_to(root).as_posix()
        manifest = _read_json_path(manifest_path, label="release bundle manifest")
        if not isinstance(manifest, dict):
            raise ValueError("release bundle manifest must be a JSON object")
        registries: object = []
        if getattr(args, "claimed_registries", None):
            registries = _read_json_path(
                args.claimed_registries, label="claimed registries"
            )
            if isinstance(registries, dict):
                registries = registries.get("claimed_registries")
        if not isinstance(registries, list) or not all(
            isinstance(row, dict) for row in registries
        ):
            raise ValueError("claimed registries must be a JSON array")
        verified = verify_release_bundle_files(
            root,
            manifest,
            manifest_relative_path=relative,
            claimed_registries=registries,
        )
        result = {
            "verified": True,
            "manifest_path": relative,
            "manifest_sha256": sha256_hex(manifest_path.read_bytes()),
            "candidate_commit": verified["candidate_commit"],
            "candidate_tree": verified["candidate_tree"],
            "semver": verified["semver"],
            "public_upload_order": verified["public_upload_order"],
            "release_asset_root": verified["release_asset_root"],
        }
    except (OSError, ValueError, ContractValidationError) as exc:
        print(f"omg parity: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


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

    from omg_cli import __version__

    parser = argparse.ArgumentParser(
        prog="omg",
        description=(
            "oh-my-grok CLI — setup, doctor, state, and mode launchers. "
            "Host launch: omg --madmax (full-open Grok in tmux)."
        ),
        parents=[common],
        epilog="Also: omg --madmax [grok args…]  — full-open host launch in tmux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"omg {__version__}",
    )

    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser(
        "setup",
        parents=[common],
        help="ensure .omg dirs, merge AGENTS + gitignore",
    )
    p_setup.add_argument(
        "--no-global-rules",
        action="store_true",
        help="do not install ~/.grok/rules/omg.md global guidance",
    )
    p_setup.add_argument(
        "--no-global-hook",
        action="store_true",
        help="do not install the global PreToolUse soft-gate ($GROK_HOME/hooks/); "
        "doctor will still report it missing",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_install_hook = sub.add_parser(
        "install-hook",
        parents=[common],
        help="install/repair the global PreToolUse soft-gate ($GROK_HOME/hooks/)",
    )
    p_install_hook.add_argument(
        "--remove",
        action="store_true",
        help="uninstall the global hook instead of installing it",
    )
    p_install_hook.set_defaults(func=cmd_install_hook)

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

    p_note = sub.add_parser(
        "note",
        parents=[common],
        help="append a durable project note (.omg/notepad.md)",
    )
    p_note.add_argument(
        "text",
        nargs="*",
        help="note text (omit to show the notepad)",
    )
    p_note.add_argument(
        "--priority",
        action="store_true",
        help="permanent (else 7d TTL tag)",
    )
    p_note.add_argument(
        "--show",
        action="store_true",
        help="print the notepad and exit",
    )
    p_note.add_argument(
        "--prune",
        action="store_true",
        help="remove [7d] notes older than 7 days (permanent kept)",
    )
    p_note.set_defaults(func=cmd_note)

    p_update = sub.add_parser(
        "update",
        parents=[common],
        help="git pull + refresh installed plugin",
    )
    p_update.set_defaults(func=cmd_update)

    p_uninstall = sub.add_parser(
        "uninstall",
        parents=[common],
        help="remove plugin, global hook, and OMG rules block",
    )
    p_uninstall.add_argument(
        "--yes",
        action="store_true",
        help="actually perform removal",
    )
    p_uninstall.set_defaults(func=cmd_uninstall)

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
    p_resume.add_argument(
        "--json",
        action="store_true",
        help="machine-readable pack",
    )
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

    p_memory = sub.add_parser(
        "memory",
        parents=[common],
        help="deterministic redacted project fact memory",
    )
    memory_sub = p_memory.add_subparsers(dest="memory_action")
    p_memory_put = memory_sub.add_parser("put", parents=[common], help="upsert user fact")
    p_memory_put.add_argument("key")
    p_memory_put.add_argument("value")
    p_memory_put.add_argument("--updated-at", default=None)
    p_memory_put.set_defaults(func=cmd_memory, memory_action="put")
    p_memory_search = memory_sub.add_parser(
        "search", parents=[common], help="search fact keys and values"
    )
    p_memory_search.add_argument("query")
    p_memory_search.add_argument("--limit", type=int, default=20)
    p_memory_search.set_defaults(func=cmd_memory, memory_action="search")
    p_memory_show = memory_sub.add_parser(
        "show", parents=[common], help="print canonical fact store"
    )
    p_memory_show.set_defaults(func=cmd_memory, memory_action="show", output=None)
    p_memory_export = memory_sub.add_parser(
        "export", parents=[common], help="write canonical fact store JSON"
    )
    p_memory_export.add_argument("--output", required=True)
    p_memory_export.set_defaults(func=cmd_memory, memory_action="export")
    p_memory_import = memory_sub.add_parser(
        "import", parents=[common], help="merge canonical fact store JSON"
    )
    p_memory_import.add_argument("file")
    p_memory_import.set_defaults(func=cmd_memory, memory_action="import")
    p_memory_rescan = memory_sub.add_parser(
        "rescan", parents=[common], help="replace scanner observations from JSON"
    )
    p_memory_rescan.add_argument("file")
    p_memory_rescan.add_argument("--observed-at", default=None)
    p_memory_rescan.set_defaults(func=cmd_memory, memory_action="rescan")
    p_memory.set_defaults(func=cmd_memory)

    p_tracker = sub.add_parser(
        "tracker",
        parents=[common],
        help="generation-fenced passive lifecycle projection",
    )
    tracker_sub = p_tracker.add_subparsers(dest="tracker_action")
    p_tracker_status = tracker_sub.add_parser(
        "status", parents=[common], help="show a projected run"
    )
    p_tracker_status.add_argument("--run", dest="run_id", required=True)
    p_tracker_status.set_defaults(func=cmd_tracker, tracker_action="status")
    p_tracker_project = tracker_sub.add_parser(
        "project", parents=[common], help="project journal or supplied events"
    )
    p_tracker_project.add_argument("--run", dest="run_id", required=True)
    p_tracker_project.add_argument("--generation", type=int, required=True)
    p_tracker_project.add_argument(
        "--events",
        default=None,
        help="optional JSON event array; otherwise read passive journals",
    )
    p_tracker_project.set_defaults(func=cmd_tracker, tracker_action="project")
    p_tracker_reconcile = tracker_sub.add_parser(
        "reconcile", parents=[common], help="reconcile signed native inventory"
    )
    p_tracker_reconcile.add_argument("--run", dest="run_id", required=True)
    p_tracker_reconcile.add_argument("--inventory", required=True)
    p_tracker_reconcile.set_defaults(func=cmd_tracker, tracker_action="reconcile")
    p_tracker.set_defaults(func=cmd_tracker)

    p_compact = sub.add_parser(
        "compact",
        parents=[common],
        help="lossless generation-fenced runtime compaction",
    )
    compact_sub = p_compact.add_subparsers(dest="compact_action")
    p_compact_create = compact_sub.add_parser(
        "create", parents=[common], help="create or adopt a checkpoint"
    )
    p_compact_create.add_argument("--run", dest="run_id", required=True)
    p_compact_create.add_argument("--generation", type=int, required=True)
    p_compact_create.add_argument("--guidance-file", required=True)
    p_compact_create.add_argument("--receipts", required=True)
    p_compact_create.add_argument("--recovery-manifest", required=True)
    p_compact_create.set_defaults(func=cmd_compact, compact_action="create")
    p_compact_show = compact_sub.add_parser(
        "show", parents=[common], help="validate and print checkpoint"
    )
    p_compact_show.add_argument("path")
    p_compact_show.set_defaults(func=cmd_compact, compact_action="show")
    p_compact_render = compact_sub.add_parser(
        "render", parents=[common], help="restore exact guidance bytes"
    )
    p_compact_render.add_argument("path")
    p_compact_render.add_argument("--guidance-out", required=True)
    p_compact_render.set_defaults(func=cmd_compact, compact_action="render")
    p_compact.set_defaults(func=cmd_compact)

    p_notify = sub.add_parser(
        "notify",
        parents=[common],
        help="outbound-only non-authoritative notification queue",
    )
    notify_sub = p_notify.add_subparsers(dest="notify_action")
    p_notify_status = notify_sub.add_parser(
        "status", parents=[common], help="show validated adapter configuration"
    )
    p_notify_status.add_argument("--config", default=None)
    p_notify_status.set_defaults(func=cmd_notify, notify_action="status")
    p_notify_send = notify_sub.add_parser(
        "send", parents=[common], help="enqueue one bounded notification"
    )
    p_notify_send.add_argument("--owner", dest="owner_id", required=True)
    p_notify_send.add_argument("--generation", type=int, required=True)
    p_notify_send.add_argument(
        "--severity", choices=("info", "success", "warning", "error"), default="info"
    )
    p_notify_send.add_argument("--title", required=True)
    p_notify_send.add_argument("--message", required=True)
    p_notify_send.add_argument("--stable-source-id", default=None)
    p_notify_send.add_argument("--max-attempts", type=int, default=3)
    p_notify_send.set_defaults(func=cmd_notify, notify_action="send")
    p_notify_process = notify_sub.add_parser(
        "process", parents=[common], help="deliver a bounded queue batch"
    )
    p_notify_process.add_argument("--owner", dest="owner_id", required=True)
    p_notify_process.add_argument("--generation", type=int, required=True)
    p_notify_process.add_argument("--config", default=None)
    p_notify_process.add_argument("--max-records", type=int, default=32)
    p_notify_process.add_argument("--rate-limit", type=float, default=10.0)
    p_notify_process.set_defaults(func=cmd_notify, notify_action="process")
    p_notify.set_defaults(func=cmd_notify)

    p_native_status = sub.add_parser(
        "native-status",
        parents=[common],
        help="honest public Grok dashboard/workflow observation tiers",
    )
    p_native_status.add_argument(
        "--probe",
        action="store_true",
        help="run bounded grok --help observation (never invoke slash commands)",
    )
    p_native_status.add_argument("--timeout", type=float, default=5.0)
    p_native_status.set_defaults(func=cmd_native_status)

    p_workflow = sub.add_parser(
        "workflow",
        parents=[common],
        help="repository-workflow/v1 compiler, registry, and receipt runner",
    )
    workflow_sub = p_workflow.add_subparsers(dest="workflow_action")
    p_workflow_install = workflow_sub.add_parser(
        "install", parents=[common], help="install immutable workflow definition"
    )
    p_workflow_install.add_argument("file")
    p_workflow_install.set_defaults(func=cmd_workflow, workflow_action="install")
    p_workflow_list = workflow_sub.add_parser(
        "list", parents=[common], help="list installed workflow versions"
    )
    p_workflow_list.add_argument("--name", default=None)
    p_workflow_list.set_defaults(func=cmd_workflow, workflow_action="list")
    p_workflow_show = workflow_sub.add_parser(
        "show", parents=[common], help="resolve and print one workflow"
    )
    p_workflow_show.add_argument("name")
    p_workflow_show.add_argument("--version", default=None)
    p_workflow_show.set_defaults(func=cmd_workflow, workflow_action="show")
    for workflow_action in ("plan", "run"):
        p_workflow_action = workflow_sub.add_parser(
            workflow_action,
            parents=[common],
            help=(
                "build deterministic task IDs and waves"
                if workflow_action == "plan"
                else "reconcile externally gathered task receipts"
            ),
        )
        p_workflow_action.add_argument("name")
        p_workflow_action.add_argument("--version", default=None)
        p_workflow_action.add_argument("--input", required=True)
        p_workflow_action.add_argument("--generation", type=int, default=0)
        if workflow_action == "run":
            p_workflow_action.add_argument("--receipts", required=True)
            p_workflow_action.add_argument(
                "--repository-permission", action="append", default=[]
            )
            p_workflow_action.add_argument("--host-capability", action="append", default=[])
            p_workflow_action.add_argument(
                "--launch-permission", action="append", default=[]
            )
            p_workflow_action.add_argument("--allow-mcp", action="append", default=[])
            p_workflow_action.add_argument(
                "--allow-write-path", action="append", default=[]
            )
        p_workflow_action.set_defaults(
            func=cmd_workflow,
            workflow_action=workflow_action,
        )
    p_workflow.set_defaults(func=cmd_workflow)

    p_capabilities = sub.add_parser(
        "capabilities",
        parents=[common],
        help="independent configured→verified capability tiers",
    )
    p_capabilities.add_argument("--notification-config", default=None)
    p_capabilities.set_defaults(func=cmd_capabilities)

    p_parity = sub.add_parser(
        "parity",
        parents=[common],
        help="frozen run-manifest and release-bundle verification",
    )
    parity_sub = p_parity.add_subparsers(dest="parity_action")
    p_parity_run = parity_sub.add_parser(
        "run",
        parents=[common],
        help="delegate the exact W0 run-manifest engine",
    )
    p_parity_run.add_argument(
        "manifest_args",
        nargs=argparse.REMAINDER,
        help="run-manifest action and arguments",
    )
    p_parity_run.set_defaults(func=cmd_parity, parity_action="run")
    p_parity_readback = parity_sub.add_parser(
        "release-readback",
        parents=[common],
        help="verify the exact prebuilt release-bundle file set",
    )
    p_parity_readback.add_argument("--manifest", required=True)
    p_parity_readback.add_argument("--claimed-registries", default=None)
    p_parity_readback.set_defaults(func=cmd_parity, parity_action="release-readback")
    p_parity.set_defaults(func=cmd_parity)

    p_wiki = sub.add_parser(
        "wiki",
        parents=[common],
        help="local markdown wiki under .omg/wiki",
    )
    wiki_sub = p_wiki.add_subparsers(dest="wiki_action")
    p_w_ing = wiki_sub.add_parser("ingest", parents=[common], help="append/create page")
    p_w_ing.add_argument("--title", required=True)
    p_w_ing.add_argument("--text", default=None, help="page body text")
    p_w_ing.add_argument("--file", default=None, help="read body from file")
    p_w_ing.add_argument("--tags", default=None, help="comma-separated tags")
    p_w_ing.add_argument("--source", default=None, help="optional source note")
    p_w_ing.set_defaults(func=cmd_wiki)
    p_w_list = wiki_sub.add_parser("list", parents=[common], help="list wiki pages")
    p_w_list.set_defaults(func=cmd_wiki)
    p_w_q = wiki_sub.add_parser("query", parents=[common], help="keyword search")
    p_w_q.add_argument("q", help="search string")
    p_w_q.add_argument("--limit", type=int, default=20)
    p_w_q.set_defaults(func=cmd_wiki)
    p_wiki.set_defaults(func=cmd_wiki)

    p_hud = sub.add_parser(
        "hud",
        parents=[common],
        help="one-line HUD for active (or --run) status",
    )
    p_hud.add_argument("--run", dest="run_id", default=None)
    p_hud.add_argument("--json", action="store_true")
    p_hud.set_defaults(func=cmd_hud)

    p_lsp = sub.add_parser(
        "lsp",
        parents=[common],
        help="inspect host-owned .lsp.json registration (no semantic proxy)",
    )
    lsp_sub = p_lsp.add_subparsers(dest="lsp_action")
    p_lsp_st = lsp_sub.add_parser(
        "status", parents=[common], help="inspect registration and command availability"
    )
    p_lsp_st.set_defaults(func=cmd_lsp)
    p_lsp_ck = lsp_sub.add_parser(
        "check", parents=[common], help="report semantic check as host-owned/unsupported"
    )
    p_lsp_ck.add_argument("path", help="file path")
    p_lsp_ck.set_defaults(func=cmd_lsp)
    p_lsp_sym = lsp_sub.add_parser(
        "symbols",
        parents=[common],
        help="report symbol lookup as host-owned/unsupported",
    )
    p_lsp_sym.add_argument("path", help="Python file path")
    p_lsp_sym.set_defaults(func=cmd_lsp)
    p_lsp_diag = lsp_sub.add_parser(
        "diagnostics",
        parents=[common],
        help="report diagnostics as host-owned/unsupported",
    )
    p_lsp_diag.add_argument("path", help="Python file path")
    p_lsp_diag.set_defaults(func=cmd_lsp)
    p_lsp.set_defaults(func=cmd_lsp)

    p_interview = sub.add_parser(
        "interview",
        parents=[common],
        help="deterministic resumable deep-interview requirements gate",
    )
    interview_sub = p_interview.add_subparsers(dest="interview_action")
    p_i_start = interview_sub.add_parser(
        "start",
        parents=[common],
        help="start one-question-at-a-time requirements convergence",
    )
    p_i_start.add_argument("task", nargs="+", help="task or labeled requirements")
    p_i_start.add_argument(
        "--profile",
        choices=("quick", "standard", "deep"),
        default="standard",
        help="ambiguity profile (quick=.30, standard=.20, deep=.15)",
    )
    p_i_start.add_argument(
        "--force",
        action="store_true",
        help="supersede an existing active run",
    )
    p_i_start.set_defaults(func=cmd_interview, interview_action="start")

    p_i_answer = interview_sub.add_parser(
        "answer",
        parents=[common],
        help="answer the single pending question and persist transcript state",
    )
    p_i_answer.add_argument("--run", dest="run_id", required=True, help="interview run_id")
    p_i_answer.add_argument("--text", required=True, help="answer text")
    p_i_answer.add_argument(
        "--question-id",
        default=None,
        help="optional freshness token from the exact resume command",
    )
    p_i_answer.set_defaults(func=cmd_interview, interview_action="answer")

    p_i_status = interview_sub.add_parser(
        "status",
        parents=[common],
        help="show active or explicit interview state and exact resume command",
    )
    p_i_status.add_argument("--run", dest="run_id", default=None, help="interview run_id")
    p_i_status.set_defaults(func=cmd_interview, interview_action="status")

    p_i_pressure = interview_sub.add_parser(
        "pressure-pass",
        parents=[common],
        help="record the required assumption/trade-off pressure pass",
    )
    p_i_pressure.add_argument("--run", dest="run_id", required=True, help="interview run_id")
    p_i_pressure.add_argument("--text", required=True, help="pressure-pass rationale")
    p_i_pressure.set_defaults(func=cmd_interview, interview_action="pressure-pass")

    p_i_close = interview_sub.add_parser(
        "close",
        parents=[common],
        help="validate readiness and write the authoritative transcript/spec",
    )
    p_i_close.add_argument("--run", dest="run_id", required=True, help="interview run_id")
    p_i_close.set_defaults(func=cmd_interview, interview_action="close")
    p_interview.set_defaults(func=cmd_interview)

    p_goal = sub.add_parser(
        "goal",
        parents=[common],
        help="durable hash-chained ultragoal ledger",
    )
    goal_sub = p_goal.add_subparsers(dest="goal_action")

    p_g_init = goal_sub.add_parser(
        "init",
        parents=[common],
        help="create dependency-valid goal with hash-chained ledger",
    )
    p_g_init.add_argument("--goal", dest="goal_id", required=True, help="goal id")
    p_g_init.add_argument("--title", default=None, help="goal title")
    p_g_init.add_argument("--objective", default=None, help="goal objective")
    p_g_init.add_argument(
        "--stories-json",
        required=True,
        help='JSON array of stories: [{"id","depends_on","acceptance","title"?}]',
    )
    p_g_init.add_argument("--source-spec-hash", default=None)
    p_g_init.add_argument("--source-plan-hash", default=None)
    p_g_init.set_defaults(func=cmd_goal, goal_action="init")

    p_g_status = goal_sub.add_parser(
        "status",
        parents=[common],
        help="show one goal or list all goals",
    )
    p_g_status.add_argument("--goal", dest="goal_id", default=None, help="goal id")
    p_g_status.set_defaults(func=cmd_goal, goal_action="status")

    p_g_link = goal_sub.add_parser(
        "link-run",
        parents=[common],
        help="link a run to a goal for verification coupling",
    )
    p_g_link.add_argument("--goal", dest="goal_id", required=True)
    p_g_link.add_argument("--run", dest="run_id", required=True)
    p_g_link.set_defaults(func=cmd_goal, goal_action="link-run")

    p_g_start = goal_sub.add_parser(
        "start-story",
        parents=[common],
        help="move a ready story to in_progress",
    )
    p_g_start.add_argument("--goal", dest="goal_id", required=True)
    p_g_start.add_argument("--story", dest="story_id", required=True)
    p_g_start.set_defaults(func=cmd_goal, goal_action="start-story")

    p_g_cp = goal_sub.add_parser(
        "checkpoint",
        parents=[common],
        help="append evidence-backed checkpoint for in_progress story",
    )
    p_g_cp.add_argument("--goal", dest="goal_id", required=True)
    p_g_cp.add_argument("--story", dest="story_id", required=True)
    p_g_cp.add_argument("--evidence", required=True, help="path to evidence file")
    p_g_cp.add_argument("--message", required=True, help="checkpoint message")
    p_g_cp.set_defaults(func=cmd_goal, goal_action="checkpoint")

    p_g_block = goal_sub.add_parser(
        "block-story",
        parents=[common],
        help="block a story with reason and optional next action",
    )
    p_g_block.add_argument("--goal", dest="goal_id", required=True)
    p_g_block.add_argument("--story", dest="story_id", required=True)
    p_g_block.add_argument("--reason", required=True)
    p_g_block.add_argument("--next-action", dest="next_action", default=None)
    p_g_block.set_defaults(func=cmd_goal, goal_action="block-story")

    p_g_resume = goal_sub.add_parser(
        "resume-story",
        parents=[common],
        help="resume a blocked story",
    )
    p_g_resume.add_argument("--goal", dest="goal_id", required=True)
    p_g_resume.add_argument("--story", dest="story_id", required=True)
    p_g_resume.set_defaults(func=cmd_goal, goal_action="resume-story")

    p_g_complete = goal_sub.add_parser(
        "complete-story",
        parents=[common],
        help="complete an in_progress story that has checkpoints",
    )
    p_g_complete.add_argument("--goal", dest="goal_id", required=True)
    p_g_complete.add_argument("--story", dest="story_id", required=True)
    p_g_complete.set_defaults(func=cmd_goal, goal_action="complete-story")

    p_g_verify = goal_sub.add_parser(
        "verify",
        parents=[common],
        help="verify goal only when a linked run is CLI-verified",
    )
    p_g_verify.add_argument("--goal", dest="goal_id", required=True)
    p_g_verify.add_argument("--run", dest="run_id", default=None)
    p_g_verify.set_defaults(func=cmd_goal, goal_action="verify")

    p_g_repair = goal_sub.add_parser(
        "repair",
        parents=[common],
        help="diagnose or repair eligible final-tail ledger damage",
    )
    p_g_repair.add_argument("--goal", dest="goal_id", required=True)
    p_g_repair.add_argument(
        "--dry-run",
        action="store_true",
        help="report valid-prefix boundary without mutation (default without --yes)",
    )
    p_g_repair.add_argument(
        "--yes",
        action="store_true",
        help="confirm repair after byte-for-byte hash-named backup",
    )
    p_g_repair.set_defaults(func=cmd_goal, goal_action="repair")
    p_goal.set_defaults(func=cmd_goal)

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
        help="extend acceptance basename allowlist (repeatable; floors still apply)",
    )
    p_accept.add_argument(
        "--no-allowlist",
        dest="no_allowlist",
        action="store_true",
        help=(
            "DANGEROUS TTY-only break-glass: skip positive allowlist "
            "(shells, agent CLIs, python -c, npx still blocked)"
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
    p_integrate.add_argument(
        "--require-squash",
        dest="require_squash",
        action="store_true",
        help="reject envelopes whose base..head range has more than one commit",
    )
    p_integrate.set_defaults(func=cmd_integrate)

    p_worker = sub.add_parser(
        "worker",
        parents=[common],
        help="prepare/seal ULW worktrees and result envelopes (no-shell bridge)",
    )
    worker_sub = p_worker.add_subparsers(dest="worker_action")
    p_w_prep = worker_sub.add_parser(
        "prepare",
        parents=[common],
        help="create .omg/worktrees/<run>/<task> via git worktree add",
    )
    p_w_prep.add_argument(
        "--task", dest="task_id", required=True, help="task_id for worktree"
    )
    p_w_prep.add_argument(
        "--run", dest="run_id", default=None, help="run_id (default: active)"
    )
    p_w_prep.set_defaults(func=cmd_worker, worker_action="prepare")
    p_w_seal = worker_sub.add_parser(
        "seal",
        parents=[common],
        help="git add/commit in worktree and write ulw-results envelope",
    )
    seal_target = p_w_seal.add_mutually_exclusive_group(required=True)
    seal_target.add_argument(
        "--task", dest="task_id", default=None, help="task_id for envelope"
    )
    seal_target.add_argument(
        "--all",
        dest="seal_all",
        action="store_true",
        help="seal every ownership-manifest task with a local worktree",
    )
    p_w_seal.add_argument(
        "--run", dest="run_id", default=None, help="run_id (default: active)"
    )
    p_w_seal.add_argument(
        "--message",
        dest="message",
        default="omg seal",
        help="commit message (default: omg seal)",
    )
    p_w_seal.add_argument(
        "--status",
        dest="status",
        choices=("ok", "failed"),
        default="ok",
        help="envelope status (default: ok)",
    )
    p_w_seal.add_argument(
        "--evidence",
        dest="evidence",
        default="",
        help="optional evidence string on envelope",
    )
    p_w_seal.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "with --all: re-seal even when an envelope already exists "
            "(pick up post-seal commits); without --force, existing "
            "envelope → already-sealed"
        ),
    )
    p_w_seal.set_defaults(func=cmd_worker, worker_action="seal", seal_all=False)

    p_w_own = worker_sub.add_parser(
        "own",
        parents=[common],
        help="write CLI ownership manifest for ULW tasks",
    )
    p_w_own.add_argument("--run", dest="run_id", default=None)
    p_w_own.add_argument(
        "--tasks-json",
        required=True,
        help='JSON array: [{"task_id","owned_files":[...],"capability_mode"?}]',
    )
    p_w_own.set_defaults(func=cmd_worker, worker_action="own", task_id="__own__")

    p_w_po = worker_sub.add_parser(
        "prepare-owned",
        parents=[common],
        help="prepare worktrees for every ownership-manifest task",
    )
    p_w_po.add_argument("--run", dest="run_id", default=None)
    p_w_po.set_defaults(
        func=cmd_worker, worker_action="prepare-owned", task_id="__prepare_owned__"
    )

    p_w_join = worker_sub.add_parser(
        "join",
        parents=[common],
        help="join sealed envelopes against ownership manifest (block if missing)",
    )
    p_w_join.add_argument("--run", dest="run_id", default=None)
    p_w_join.set_defaults(func=cmd_worker, worker_action="join", task_id="__join__")

    p_w_man = worker_sub.add_parser(
        "manifest",
        parents=[common],
        help="show ownership manifest for a run",
    )
    p_w_man.add_argument("--run", dest="run_id", default=None)
    p_w_man.set_defaults(
        func=cmd_worker, worker_action="manifest", task_id="__manifest__"
    )
    p_worker.set_defaults(func=cmd_worker)

    p_team = sub.add_parser(
        "team",
        parents=[common],
        help=(
            "experimental tmux team plane (grok-only zero-config; multi-CLI "
            "via --routing; requires OMG_EXPERIMENTAL_TMUX_TEAM=1)"
        ),
    )
    team_sub = p_team.add_subparsers(dest="team_action")
    p_t_start = team_sub.add_parser(
        "start",
        parents=[common],
        help="create run + ownership worktrees + tmux session (or --dry-run)",
    )
    p_t_start.add_argument(
        "--goal",
        dest="goal",
        required=True,
        help="shared goal text for all task panes",
    )
    p_t_start.add_argument(
        "--tasks-json",
        dest="tasks_json",
        required=True,
        help=(
            'JSON array: [{"task_id","owned_files":[...],"role"?,'
            '"capability_mode"?}]'
        ),
    )
    p_t_start.add_argument(
        "--routing",
        dest="routing",
        default=None,
        help=(
            'JSON object role→{provider,model?}, e.g. '
            '\'{"executor":{"provider":"codex"}}\'; enables multi-CLI floors'
        ),
    )
    p_t_start.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="existing run_id (default: create a new ulw/team run)",
    )
    p_t_start.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="write team.json skeleton (pid=None); never call tmux/subprocess",
    )
    p_t_start.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="supersede active run when creating a new run",
    )
    p_t_start.set_defaults(func=cmd_team, team_action="start")

    p_t_run = team_sub.add_parser(
        "run",
        parents=[common],
        help=(
            "staged team pipeline driver (team-plan→prd→exec→verify→fix); "
            "THIN glue over start/collect + parse_verdict_file gate; "
            "never sets verified"
        ),
    )
    p_t_run.add_argument(
        "--goal",
        dest="goal",
        required=True,
        help="shared goal text",
    )
    p_t_run.add_argument(
        "--tasks-json",
        dest="tasks_json",
        default=None,
        help=(
            'JSON array of tasks (leader/ralplan decomposition); '
            'required unless --tasks-path is set'
        ),
    )
    p_t_run.add_argument(
        "--tasks-path",
        dest="tasks_path",
        default=None,
        help="path to JSON tasks array or {tasks:[...]} (existing ralplan artifact)",
    )
    p_t_run.add_argument(
        "--max-fix",
        dest="max_fix",
        type=int,
        default=3,
        help="max team-fix rounds before terminal failed (default 3)",
    )
    p_t_run.add_argument(
        "--routing",
        dest="routing",
        default=None,
        help='optional role→{provider,model?} JSON (same as team start)',
    )
    p_t_run.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="existing run_id (default: create a new team-pipeline run)",
    )
    p_t_run.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="sequence stages with dry-run start_team; no tmux/subprocess",
    )
    p_t_run.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="supersede active run when creating a new run",
    )
    p_t_run.add_argument(
        "--ralph",
        dest="ralph",
        action="store_true",
        help=(
            "wrap staged pipeline in a bounded ralph persistence loop "
            "(exec→verify→fix up to --max-iter; never sets verified; "
            "links team.json ↔ team-ralph.json)"
        ),
    )
    p_t_run.add_argument(
        "--max-iter",
        dest="max_iter",
        type=int,
        default=None,
        help=(
            "with --ralph: max outer iterations (default 3 from ralph); "
            "stop at team-verify APPROVE or max_iter → failed"
        ),
    )
    p_t_run.set_defaults(func=cmd_team, team_action="run")

    p_t_scale = team_sub.add_parser(
        "scale",
        parents=[common],
        help=(
            "dynamic scale: --add N / --remove N panes on a running team "
            "(cap-bounded; scale lock; no pkill -f; never sets verified)"
        ),
    )
    p_t_scale.add_argument(
        "--run", dest="run_id", required=True, help="team run_id"
    )
    p_t_scale_grp = p_t_scale.add_mutually_exclusive_group(required=True)
    p_t_scale_grp.add_argument(
        "--add",
        dest="add",
        type=int,
        default=None,
        help="add N new task panes (respects max_workers_cap; monotonic indices)",
    )
    p_t_scale_grp.add_argument(
        "--remove",
        dest="remove",
        type=int,
        default=None,
        help=(
            "graceful drain: remove N idle/newest panes (kill recorded pgids + "
            "windows only; preserve worktrees; never below 1)"
        ),
    )
    p_t_scale.add_argument(
        "--tasks-json",
        dest="tasks_json",
        default=None,
        help="optional JSON tasks for --add (length must equal N; else synthetic)",
    )
    p_t_scale.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="append/mark team.json only; no tmux/subprocess",
    )
    p_t_scale.set_defaults(func=cmd_team, team_action="scale")

    p_t_resume = team_sub.add_parser(
        "resume",
        parents=[common],
        help=(
            "reconcile team.json pane liveness after leader restart "
            "(idempotent status write; never sets verified)"
        ),
    )
    p_t_resume.add_argument(
        "--run", dest="run_id", required=True, help="team run_id"
    )
    p_t_resume.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print JSON (default for resume)",
    )
    p_t_resume.set_defaults(func=cmd_team, team_action="resume")

    p_t_status = team_sub.add_parser(
        "status",
        parents=[common],
        help="read team.json + ownership + optional pane liveness (no state write)",
    )
    p_t_status.add_argument(
        "--run", dest="run_id", default=None, help="run_id (default: active)"
    )
    p_t_status.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print LOCKED field set as JSON",
    )
    p_t_status.set_defaults(func=cmd_team, team_action="status")

    p_t_collect = team_sub.add_parser(
        "collect",
        parents=[common],
        help="seal_all_tasks + integrate_results (never sets verified)",
    )
    p_t_collect.add_argument(
        "--run", dest="run_id", default=None, help="run_id (default: active)"
    )
    p_t_collect.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="re-seal even when envelopes already exist",
    )
    p_t_collect.set_defaults(func=cmd_team, team_action="collect")

    p_t_stop = team_sub.add_parser(
        "stop",
        parents=[common],
        help="kill recorded tmux session + killpg recorded pgids (no pkill -f)",
    )
    p_t_stop.add_argument(
        "--run", dest="run_id", default=None, help="run_id (default: active)"
    )
    p_t_stop.set_defaults(func=cmd_team, team_action="stop")
    p_team.set_defaults(func=cmd_team)

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
        help='gate evidence e.g. {"interview_complete":true}',
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
        help=(
            "passthrough arg after fixed template (disabled by default; "
            "set OMG_ASK_ALLOW_EXTRA=1; elevation flags always denied)"
        ),
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

    p_mcp_server = sub.add_parser(
        "mcp-server",
        parents=[common],
        help=(
            "run focused in-session MCP server (stdio JSON-RPC; "
            "reads + proposal writes only; sets OMG_MCP_SERVER=1)"
        ),
    )
    p_mcp_server.add_argument(
        "--root",
        default=None,
        help="project root (default: cwd)",
    )
    p_mcp_server.set_defaults(func=cmd_mcp_server)

    p_mcp_install = sub.add_parser(
        "mcp-install",
        parents=[common],
        help="register with Grok: grok mcp add omg omg -- mcp-server",
    )
    p_mcp_install.add_argument(
        "--scope",
        choices=("user", "project"),
        default="user",
        help="grok mcp add --scope (default: user)",
    )
    p_mcp_install.add_argument(
        "--print-only",
        "--dry-run",
        dest="print_only",
        action="store_true",
        help="print the grok mcp add command without running it",
    )
    p_mcp_install.set_defaults(func=cmd_mcp_install)

    return parser


# Keep in sync with build_parser() subcommands (madmax intercept policy).
KNOWN_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "setup",
        "doctor",
        "update",
        "uninstall",
        "note",
        "state",
        "cancel",
        "resume",
        "session",
        "recover",
        "memory",
        "tracker",
        "compact",
        "notify",
        "native-status",
        "workflow",
        "capabilities",
        "parity",
        "wiki",
        "hud",
        "lsp",
        "interview",
        "goal",
        "accept",
        "integrate",
        "worker",
        "team",
        "review",
        "qa",
        "autopilot",
        "ulw",
        "ralph",
        "ralplan",
        "ask",
        "pipeline",
        "dual-review",
        "mcp-server",
        "mcp-install",
    }
)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    from omg_cli.host_launcher import (
        HostLaunchUsageError,
        reject_launcher_flags_after_subcommand,
        run_interactive,
        run_madmax_host,
        should_host_launch,
    )
    from omg_cli.madmax import has_madmax_flag

    try:
        reject_launcher_flags_after_subcommand(raw, KNOWN_SUBCOMMANDS)
    except HostLaunchUsageError as exc:
        print(str(exc), file=sys.stderr)
        return int(exc.exit_code)

    if has_madmax_flag(raw):
        # Delimiter-aware; GRAM-05 only cares about a recognized *first* token.
        return int(run_madmax_host(_project_root(), raw))

    if should_host_launch(raw, KNOWN_SUBCOMMANDS):
        return int(run_interactive(_project_root(), raw))

    parser = build_parser()
    args = parser.parse_args(raw)
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
