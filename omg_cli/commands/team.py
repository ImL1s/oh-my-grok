"""Team-family CLI handlers (#29 Phase 2).

Commands: accept, integrate, team, worker.
Parser construction: ``register_team_parsers`` (#29 Phase 4').
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omg_cli.cli_envelope import emit_data
from omg_cli.cli_util import project_root


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

    root = project_root()
    run_id = getattr(args, "run_id", None)
    if not run_id:
        active = load_active_run(root)
        if active is None:
            print("accept failed: no active run (pass --run ID)", file=sys.stderr)
            return 1
        run_id = active["run_id"]

    run = load_run(root, run_id)
    if run is None:
        print(f"accept failed: no run found: {run_id}", file=sys.stderr)
        return 1

    # Autopilot sidecar present → always refuse bare accept (even if status.mode
    # was hand-edited away from autopilot). Defense in depth: also refuse when
    # mode still says autopilot.
    from omg_cli.autopilot import autopilot_state_path

    if autopilot_state_path(root, run_id).is_file():
        print(
            "accept failed: autopilot runs must use "
            "`omg autopilot complete` (not bare `omg accept`)",
            file=sys.stderr,
        )
        return 1

    if run.get("mode") == "autopilot":
        print(
            "accept failed: autopilot runs must use "
            "`omg autopilot complete` (not bare `omg accept`)",
            file=sys.stderr,
        )
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
    emit_data(args, "accept", verified)
    return 0


def cmd_integrate(args: argparse.Namespace) -> int:
    """Apply ULW result envelopes (cherry-pick) for active or --run run."""
    from omg_cli.integrate import IntegrateError, integrate_results, result_path
    from omg_cli.state import load_active_run, load_run

    root = project_root()
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
    print(f"integrate result: {rpath}", file=sys.stderr)
    emit_data(args, "integrate", result)

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
    )
    from omg_cli.team.pipeline import (
        TeamPipelineError,
        run_team_pipeline,
    )
    from omg_cli.team.roles import UnknownRoleError
    from omg_cli.team.routing import RoutingError, parse_routing_json
    from omg_cli.team.scaling import scale_team

    root = project_root()
    action = getattr(args, "team_action", None)

    try:
        if action == "launch":
            from omg_cli.team.runtime import launch_team

            routing_raw = getattr(args, "routing", None)
            routing = parse_routing_json(routing_raw) if routing_raw else None
            plan_only = bool(getattr(args, "plan_only", False))
            dry_run = bool(getattr(args, "dry_run", False) or getattr(args, "materialize_only", False))
            if plan_only:
                # #27: side-effect-free preview (no run / worktrees / team dir).
                workers = int(getattr(args, "workers", 0) or 0)
                role = str(getattr(args, "role", None) or "executor")
                goal = getattr(args, "goal", None) or ""
                plan = {
                    "mode": "plan_only",
                    "schema_version": 1,
                    "command": "team.launch",
                    "dry_run": False,
                    "mutates": False,
                    "goal": goal,
                    "project_root": str(root),
                    "workers": workers,
                    "role": role,
                    "routing": routing,
                    "note": (
                        "plan-only: no .omg mutation, no worktrees, no tmux "
                        "(#27). Use --dry-run/--materialize-only to materialize "
                        "without live panes."
                    ),
                }
                emit_data(args, "team", plan)
                print("Team plan-only (no state written)", file=sys.stderr)
                return 0
            meta = launch_team(
                getattr(args, "goal", None) or "",
                workers=int(getattr(args, "workers", 0) or 0),
                role=str(getattr(args, "role", None) or "executor"),
                root=root,
                dry_run=dry_run,
                force=bool(getattr(args, "force", False)),
                routing=routing,
                yolo=bool(getattr(args, "yolo", False)),
                safe=bool(getattr(args, "safe", False)),
                run_id=getattr(args, "run_id", None),
                detach=bool(getattr(args, "detach", False)),
            )
            emit_data(args, "team", meta)
            hint = meta.get("attach_hint")
            if hint and not meta.get("dry_run"):
                print(f"omg team launch: {hint}", file=sys.stderr)
            # Bounded ACK wait: partial/zero ACKs leave state for diagnosis
            # but must not report success (no silent dry-run / ULW fallback).
            startup = meta.get("startup_status")
            if startup in ("failed_start", "degraded"):
                print(
                    f"omg team launch: startup {startup} "
                    f"(acks={meta.get('startup_acks')}/"
                    f"{meta.get('startup_expected')})",
                    file=sys.stderr,
                )
                return 1
            if startup == "running":
                print("Team started", file=sys.stderr)
            elif meta.get("dry_run") or dry_run:
                print(
                    "Team materialized (dry-run/materialize-only: no live workers; "
                    "not started)",
                    file=sys.stderr,
                )
            return 0
        if action == "start":
            from omg_cli.team.runtime import apply_start_readiness

            goal = getattr(args, "goal", None) or ""
            tasks_json = getattr(args, "tasks_json", None)
            if not tasks_json:
                print("omg team start: --tasks-json required", file=sys.stderr)
                return 2
            routing_raw = getattr(args, "routing", None)
            routing = parse_routing_json(routing_raw) if routing_raw else None
            # parse_routing_json returns None for empty; keep None so zero-config
            # stays D1. Non-empty --routing enables multi-CLI floors.
            dry_run = bool(
                getattr(args, "dry_run", False)
                or getattr(args, "materialize_only", False)
            )
            plan_only = bool(getattr(args, "plan_only", False))
            no_wait = bool(getattr(args, "no_wait", False))
            if plan_only:
                # #27: side-effect-free preview (no run / worktrees / team dir).
                from omg_cli.team.plane import _parse_tasks_json

                tasks = _parse_tasks_json(tasks_json)
                plan = {
                    "mode": "plan_only",
                    "schema_version": 1,
                    "command": "team.start",
                    "dry_run": False,
                    "mutates": False,
                    "goal": goal,
                    "project_root": str(root),
                    "task_count": len(tasks),
                    "tasks": tasks,
                    "routing": routing,
                    "note": (
                        "plan-only: no .omg mutation, no worktrees, no tmux "
                        "(#27). Use --dry-run/--materialize-only to materialize "
                        "without live panes."
                    ),
                }
                emit_data(args, "team", plan)
                print("Team plan-only (no state written)", file=sys.stderr)
                return 0
            meta = start_team(
                goal,
                tasks_json,
                root=root,
                run_id=getattr(args, "run_id", None),
                dry_run=dry_run,
                yolo=bool(getattr(args, "yolo", False)),
                safe=bool(getattr(args, "safe", False)),
                force=bool(getattr(args, "force", False)),
                routing=routing,
            )
            # #20: same readiness contract as team launch (shared wait service).
            meta = apply_start_readiness(
                root,
                meta,
                dry_run=dry_run,
                no_wait=no_wait,
            )
            emit_data(args, "team", meta)
            startup = meta.get("startup_status")
            if startup in ("failed_start", "degraded"):
                print(
                    f"omg team start: startup {startup} "
                    f"(acks={meta.get('startup_acks')}/"
                    f"{meta.get('startup_expected')}; "
                    f"missing={meta.get('startup_missing_workers')})",
                    file=sys.stderr,
                )
                return 1
            if startup == "unverified_start":
                print(
                    "omg team start: unverified_start (--no-wait); "
                    "readiness not proven",
                    file=sys.stderr,
                )
                return 0
            if startup == "running":
                print("Team started", file=sys.stderr)
            elif dry_run:
                print(
                    "Team materialized (dry-run/materialize-only: no live workers; "
                    "not started)",
                    file=sys.stderr,
                )
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
            emit_data(args, "team", result)
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
            emit_data(args, "team", result)
            return 0
        if action == "resume":
            from omg_cli.team.runtime import resume_for_identity

            identity = getattr(args, "team_identity", None) or getattr(
                args, "run_id", None
            )
            result = resume_for_identity(root, identity)
            # Always JSON (operator machine-readable); --json kept for symmetry.
            emit_data(args, "team", result)
            return 0
        if action == "status":
            from omg_cli.team.runtime import status_for_identity

            identity = getattr(args, "team_identity", None) or getattr(
                args, "run_id", None
            )
            st = status_for_identity(root, identity)
            if getattr(args, "as_json", False):
                # Default --json stays LOCKED for machine consumers.
                # --full dumps the aggregate (mailbox/api_summary/worktrees/…).
                payload = (
                    st
                    if getattr(args, "full_status", False)
                    else status_locked_view(st)
                )
                emit_data(args, "team", payload)
            elif getattr(args, "full_status", False):
                emit_data(args, "team", st)
            else:
                print(format_status_table(st))
            return 0
        if action == "collect":
            result = collect_team(
                root,
                getattr(args, "run_id", None),
                force_seal=bool(getattr(args, "force", False)),
            )
            emit_data(args, "team", result)
            # Never sets verified; integrate status drives exit
            integrate = result.get("integrate") or {}
            status = integrate.get("status")
            if status == "ok":
                return 0
            if status == "missing":
                return 1
            return 1
        if action == "stop":
            from omg_cli.team.runtime import resolve_team_ref

            identity = getattr(args, "team_identity", None) or getattr(
                args, "run_id", None
            )
            rid = resolve_team_ref(root, identity)
            grace = getattr(args, "kill_grace_s", None)
            if grace is None:
                grace = 0.0
            try:
                grace_f = float(grace)
            except (TypeError, ValueError):
                grace_f = 0.0
            result = stop_team(
                root,
                rid,
                force=bool(getattr(args, "force", False)),
                kill_grace_s=max(0.0, grace_f),
            )
            emit_data(args, "team", result)
            return 0 if not result.get("errors") else 1
        if action == "worker-ready":
            # Process-level readiness (pane wrapper). Env-bound identity only.
            import os

            from omg_cli.team.runtime import write_worker_ready_receipt

            worker_id = (os.environ.get("OMG_TEAM_WORKER_ID") or "").strip()
            run_id = (os.environ.get("OMG_TEAM_RUN_ID") or "").strip()
            team_id = (os.environ.get("OMG_TEAM_ID") or "team").strip() or "team"
            leader = (
                os.environ.get("OMG_TEAM_LEADER_ROOT")
                or os.environ.get("OMG_PROJECT_ROOT")
                or ""
            ).strip()
            if not worker_id or not run_id:
                print(
                    "omg team worker-ready: requires OMG_TEAM_WORKER_ID and "
                    "OMG_TEAM_RUN_ID in the environment",
                    file=sys.stderr,
                )
                return 2
            ready_root = Path(leader).resolve() if leader else root
            path = write_worker_ready_receipt(
                ready_root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                source="process",
            )
            emit_data(
                args,
                "team.worker-ready",
                {
                    "ok": True,
                    "worker_id": worker_id,
                    "run_id": run_id,
                    "team_id": team_id,
                    "ready_path": str(path),
                },
            )
            return 0
        if action == "api":
            from omg_cli.team.api import (
                TeamApiError,
                execute_team_api,
                parse_input_json,
                resolve_team_api_cli_root,
            )

            op = getattr(args, "api_op", None) or ""
            raw_input = getattr(args, "api_input", None)
            if not raw_input:
                print("omg team api: --input JSON required", file=sys.stderr)
                return 2
            try:
                payload = parse_input_json(raw_input)
                api_root = resolve_team_api_cli_root(
                    root,
                    explicit_root=getattr(args, "project_root", None),
                )
            except TeamApiError as exc:
                emit_data(
                    args,
                    "team.api",
                    {
                        "ok": False,
                        "operation": op or "unknown",
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            **(
                                {"details": exc.details}
                                if exc.details
                                else {}
                            ),
                        },
                    },
                )
                return exc.exit_code
            if getattr(args, "run_id", None) and "run_id" not in payload:
                payload["run_id"] = args.run_id
            code, envelope = execute_team_api(op, payload, root=api_root)
            emit_data(args, "team", envelope)
            return code
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

    root = project_root()
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
            emit_data(args, "worker.own", manifest)
            return 0
        if action == "prepare-owned":
            paths = prepare_owned_tasks(root, run_id)
            emit_data(
                args,
                "worker.prepare-owned",
                {"run_id": run_id, "worktrees": [str(p) for p in paths]},
            )
            return 0
        if action == "join":
            result = join_worker_results(root, run_id)
            emit_data(args, "worker.join", result)
            return 0 if result.get("complete") else 1
        if action == "manifest":
            emit_data(args, "worker.manifest", load_ownership_manifest(root, run_id))
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
            print(
                f"omg worker seal: task={task_id} status={env.get('status')}",
                file=sys.stderr,
            )
            emit_data(args, "worker.seal", env)
            return 0 if env.get("status") == "ok" else 1
        print(f"omg worker: unknown action {action!r}", file=sys.stderr)
        return 2
    except (WorkerError, json.JSONDecodeError) as exc:
        print(f"omg worker: {exc}", file=sys.stderr)
        return 1


def register_team_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
) -> None:
    """Register team-family argparse parsers (#29 Phase 4').

    Commands: accept, integrate, worker, team.
    """
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
            'experimental tmux team: omg team [N[:role]] "<goal>" '
            "(default on; OMG_DISABLE_TMUX_TEAM=1 kill-switch); "
            "also start|run|api|worker-ready|…"
        ),
    )
    team_sub = p_team.add_subparsers(dest="team_action")
    p_t_launch = team_sub.add_parser(
        "launch",
        parents=[common],
        help=(
            'OMX-like shorthand launch (also: omg team N[:role] "<goal>"); '
            "split-pane topology; seeds team api board"
        ),
    )
    p_t_launch.add_argument(
        "--workers",
        dest="workers",
        type=int,
        required=True,
        help="number of worker panes (N)",
    )
    p_t_launch.add_argument(
        "--role",
        dest="role",
        default="executor",
        help="canonical team role (default: executor)",
    )
    p_t_launch.add_argument(
        "--goal",
        dest="goal",
        required=True,
        help="shared goal text",
    )
    p_t_launch.add_argument(
        "--routing",
        dest="routing",
        default=None,
        help='optional role→{provider,model?} JSON (same as team start)',
    )
    p_t_launch.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="existing run_id (default: create a new ulw/team run)",
    )
    p_t_launch.add_argument(
        "--plan-only",
        dest="plan_only",
        action="store_true",
        help=(
            "side-effect-free preview (#27): no .omg mutation, worktrees, or "
            "tmux; print plan JSON only"
        ),
    )
    p_t_launch.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "materialize team.json + api board without live tmux/subprocess "
            "(alias of --materialize-only; not side-effect-free — use "
            "--plan-only for pure preview)"
        ),
    )
    p_t_launch.add_argument(
        "--materialize-only",
        dest="materialize_only",
        action="store_true",
        help="same as --dry-run: write control-plane artifacts, no live workers",
    )
    p_t_launch.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="supersede active run when creating a new run",
    )
    p_t_launch.add_argument(
        "--detach",
        dest="detach",
        action="store_true",
        help="allow detached live launch outside an interactive TTY",
    )
    p_t_launch.set_defaults(func=cmd_team, team_action="launch")

    p_t_start = team_sub.add_parser(
        "start",
        parents=[common],
        help=(
            "create run + ownership worktrees + tmux session "
            "(or --plan-only / --dry-run)"
        ),
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
        "--plan-only",
        dest="plan_only",
        action="store_true",
        help=(
            "side-effect-free preview (#27): no .omg mutation, worktrees, or "
            "tmux; print plan JSON only"
        ),
    )
    p_t_start.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "materialize team.json skeleton (pid=None); never call "
            "tmux/subprocess (not side-effect-free — prefer --plan-only for "
            "pure preview; alias of --materialize-only)"
        ),
    )
    p_t_start.add_argument(
        "--materialize-only",
        dest="materialize_only",
        action="store_true",
        help="same as --dry-run: write control-plane artifacts, no live workers",
    )
    p_t_start.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="supersede active run when creating a new run",
    )
    p_t_start.add_argument(
        "--no-wait",
        dest="no_wait",
        action="store_true",
        help=(
            "skip readiness ACK wait; persist startup_status=unverified_start "
            "and do not claim a proven Team started (#20)"
        ),
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
        "team_identity",
        nargs="?",
        default=None,
        help="team name or run_id (optional if --run set)",
    )
    p_t_resume.add_argument(
        "--run", dest="run_id", default=None, help="team run_id"
    )
    # --json inherited from common → json_output (handler maps to as_json)
    p_t_resume.set_defaults(func=cmd_team, team_action="resume")

    p_t_status = team_sub.add_parser(
        "status",
        parents=[common],
        help="read team.json + ownership + optional pane liveness (no state write)",
    )
    p_t_status.add_argument(
        "team_identity",
        nargs="?",
        default=None,
        help="team name or run_id (optional; default active / --run)",
    )
    p_t_status.add_argument(
        "--run", dest="run_id", default=None, help="run_id (default: active)"
    )
    # --json inherited from common → json_output (handler maps to as_json)
    p_t_status.add_argument(
        "--full",
        dest="full_status",
        action="store_true",
        help=(
            "include aggregate extras (topology/startup_acks/mailbox/"
            "api_summary/worktrees); with --json prints full JSON instead "
            "of the locked set"
        ),
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
        "team_identity",
        nargs="?",
        default=None,
        help="team name or run_id (optional if --run set)",
    )
    p_t_stop.add_argument(
        "--run", dest="run_id", default=None, help="run_id (default: active)"
    )
    p_t_stop.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "tear down even when API tasks are in_progress "
            "(default: fail closed and write shutdown-request.json)"
        ),
    )
    p_t_stop.add_argument(
        "--kill-grace",
        dest="kill_grace_s",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "after SIGTERM, poll up to SECONDS for process-group disappearance "
            "before SIGKILL escalation (default 0 = immediate probe)"
        ),
    )
    p_t_stop.set_defaults(func=cmd_team, team_action="stop")

    p_t_ready = team_sub.add_parser(
        "worker-ready",
        parents=[common],
        help=(
            "process-level readiness receipt for a pane wrapper "
            "(reads OMG_TEAM_* env; primary launch gate)"
        ),
    )
    p_t_ready.set_defaults(func=cmd_team, team_action="worker-ready")

    p_t_api = team_sub.add_parser(
        "api",
        parents=[common],
        help=(
            "OMX-shaped team api façade (P0 mailbox/task ops); "
            "default on; set OMG_DISABLE_TMUX_TEAM=1 to refuse"
        ),
    )
    p_t_api.add_argument(
        "api_op",
        metavar="OP",
        help=(
            "operation name (P0/P0′ mailbox+task+heartbeat+shutdown+orphan; "
            "see omg_cli.team.api.P0_OPERATIONS)"
        ),
    )
    p_t_api.add_argument(
        "--input",
        dest="api_input",
        required=True,
        help="JSON object input (OMX-shaped fields + run_id/team_id)",
    )
    p_t_api.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="run_id injected into --input when omitted there",
    )
    # --json inherited from common → json_output
    p_t_api.set_defaults(func=cmd_team, team_action="api")
    p_team.set_defaults(func=cmd_team)


__all__ = [
    "register_team_parsers",
    "cmd_accept",
    "cmd_integrate",
    "cmd_team",
    "cmd_worker",
]
