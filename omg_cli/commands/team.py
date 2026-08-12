"""Team-family CLI handlers (#29 Phase 2).

Commands: accept, integrate, team, worker.
Parser construction: ``register_team_parsers`` (#29 Phase 4').
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

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


def _format_startup_worker_lines(meta: object) -> list[str]:
    """Human-readable per-worker startup lines for stderr (#99)."""
    lines: list[str] = []
    if not isinstance(meta, dict):
        return lines
    workers = meta.get("startup_workers")
    if not isinstance(workers, list):
        return lines
    for row in workers:
        if not isinstance(row, dict):
            continue
        wid = row.get("worker_id") or "?"
        phase = row.get("phase") or "missing"
        provider = row.get("provider") or ""
        blocked = row.get("blocked_reason")
        failure = row.get("failure_reason")
        if phase == "blocked" or blocked:
            detail = blocked or failure or "blocked"
            lines.append(f"{wid} blocked_start ({detail})")
        elif row.get("gate_ok"):
            suffix = f" ({provider})" if provider else ""
            lines.append(f"{wid} {phase}{suffix}")
        elif row.get("legacy"):
            lines.append(f"{wid} wrapper_ready_legacy (not provider-ready)")
        else:
            detail = failure or blocked or phase
            lines.append(f"{wid} {phase}: {detail}")
    return lines


def _emit_startup_human(meta: object, *, command: str) -> int | None:
    """Print startup summary; return 1 when start failed/degraded/blocked."""
    if not isinstance(meta, dict):
        return None
    for line in _format_startup_worker_lines(meta):
        print(line, file=sys.stderr)
    startup = meta.get("startup_status")
    ready_n = len(meta.get("startup_ready_workers") or [])
    expected = meta.get("startup_expected")
    if startup in ("failed_start", "degraded", "blocked_start"):
        print(
            f"Team startup {startup}: {ready_n}/{expected} ready",
            file=sys.stderr,
        )
        print(
            f"omg team {command}: startup {startup} "
            f"(provider_ready={meta.get('startup_process_ready')}/"
            f"{expected}; "
            f"mailbox_ack={meta.get('startup_acks')}; "
            f"missing={meta.get('startup_missing_workers')})",
            file=sys.stderr,
        )
        return 1
    if startup == "unverified_start":
        print(
            "Team startup unverified_start (--no-wait; not proven)",
            file=sys.stderr,
        )
        return None
    if startup == "running":
        print("Team started", file=sys.stderr)
    return None


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

    action = getattr(args, "team_action", None)
    # Introspection-only: versioned operation catalog (no project root, team
    # state, tmux, .omg, or subprocess).
    if action == "api" and (getattr(args, "api_op", None) or "") == "catalog":
        from omg_cli.team.operation_catalog import catalog_document_json

        print(catalog_document_json(), end="")
        return 0
    # #100: supervisor must not trigger generic project-root discovery via
    # project_root(); it consumes the validated leader root from env only.
    if action == "supervisor":
        root = None
    else:
        root = project_root()

    try:
        if action == "launch":
            from omg_cli.team.runtime import launch_team
            from omg_cli.team.topology import (
                TopologyError,
                resolve_launch_view_mode,
            )

            routing_raw = getattr(args, "routing", None)
            routing = parse_routing_json(routing_raw) if routing_raw else None
            plan_only = bool(getattr(args, "plan_only", False))
            dry_run = bool(getattr(args, "dry_run", False) or getattr(args, "materialize_only", False))
            detach = bool(getattr(args, "detach", False))
            dedicated = bool(getattr(args, "dedicated_window", False))
            inside_tmux = bool(os.environ.get("TMUX"))
            try:
                resolved_view = resolve_launch_view_mode(
                    inside_tmux=inside_tmux,
                    dedicated_window=dedicated,
                    detach=detach,
                )
            except TopologyError as exc:
                print(f"omg team launch: {exc}", file=sys.stderr)
                return 2
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
                    "view_mode": resolved_view,
                    "detach": detach,
                    "worker_topology": getattr(args, "worker_topology", None) or "pane",
                    "note": (
                        "plan-only: no .omg mutation, no worktrees, no tmux "
                        "(#27). Use --dry-run/--materialize-only to materialize "
                        "without live panes."
                    ),
                }
                emit_data(args, "team", plan)
                print(
                    f"Team plan-only view={resolved_view} (no state written)",
                    file=sys.stderr,
                )
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
                detach=detach,
                view_mode=resolved_view,
                worker_topology=getattr(args, "worker_topology", None),
            )
            emit_data(args, "team", meta)
            hint = meta.get("attach_hint")
            view = meta.get("view_mode") or resolved_view
            leader = meta.get("leader_pane_id")
            window = meta.get("window_id")
            if not meta.get("dry_run"):
                bits = [f"view={view}"]
                if isinstance(leader, str) and leader:
                    bits.append(f"leader={leader}")
                if isinstance(window, str) and window:
                    bits.append(f"window={window}")
                print("omg team launch: " + " ".join(bits), file=sys.stderr)
            if hint and not meta.get("dry_run"):
                print(f"omg team launch: {hint}", file=sys.stderr)
            # Provider-ready gate (#99): partial/zero/blocked leave state for
            # diagnosis but must not report success.
            code = _emit_startup_human(meta, command="launch")
            if code is not None:
                return code
            if meta.get("dry_run") or dry_run:
                if meta.get("startup_status") is None:
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
                    "worker_topology": getattr(args, "worker_topology", None) or "pane",
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
                worker_topology=getattr(args, "worker_topology", None),
            )
            # #20: same readiness contract as team launch (shared wait service).
            meta = apply_start_readiness(
                root,
                meta,
                dry_run=dry_run,
                no_wait=no_wait,
            )
            emit_data(args, "team", meta)
            code = _emit_startup_human(meta, command="start")
            if code is not None:
                return code
            if dry_run and meta.get("startup_status") is None:
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
                worker_topology=getattr(args, "worker_topology", None),
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
            if result.get("layout_repair_needed"):
                print(
                    "warning: team scale committed but layout_repair_needed=true "
                    f"(status={result.get('layout_status')!r}); "
                    "retry omg team resume to repair projection",
                    file=sys.stderr,
                )
            return 0
        if action == "resume":
            from omg_cli.cli_envelope import wants_json
            from omg_cli.host_probe import evaluate_feature_gate, probe_host
            from omg_cli.team.api import TeamApiError
            from omg_cli.team.operator import OperatorError
            from omg_cli.team.runtime import resume_for_identity, resume_with_view

            identity = getattr(args, "team_identity", None) or getattr(
                args, "run_id", None
            )
            as_json = bool(
                getattr(args, "as_json", False)
                or getattr(args, "json_output", False)
            )
            want_view = bool(getattr(args, "resume_view", False))
            print_only = bool(getattr(args, "view_print", False))
            takeover = bool(getattr(args, "view_takeover", False))
            worker = getattr(args, "worker_id", None)
            want_provider = bool(getattr(args, "provider_session", False))
            session_resume_gate = None
            provider_resume = None
            if want_provider:
                # CLI owns probe → gate; runtime must not re-parse versions.
                # required=False so absent session_resume yields LEGACY (safe
                # conversation-load path), not BLOCKED — #105 host-compat.
                host_report = probe_host()
                session_resume_gate = evaluate_feature_gate(
                    "session_resume",
                    host_report.capabilities,
                    required=False,
                )
                # Inject real ACP sidecar ensure only after AVAILABLE is possible;
                # LEGACY/BLOCKED never call ensure (provider_session_result short-circuits).
                from omg_cli.jobs.runtime import ensure_acp_session_for_team

                provider_resume = ensure_acp_session_for_team
            if print_only and not want_view:
                # resume --print implies view print without reconcile? No —
                # --print on resume only makes sense with --view.
                print(
                    "omg team resume: --print requires --view "
                    "(or use omg team view --print)",
                    file=sys.stderr,
                )
                return 2
            if takeover and not want_view:
                print(
                    "omg team resume: --takeover requires --view",
                    file=sys.stderr,
                )
                return 2
            try:
                if want_view or print_only or want_provider:
                    result = resume_with_view(
                        root,
                        identity,
                        view=want_view or print_only,
                        print_only=print_only,
                        takeover=takeover,
                        as_json=as_json,
                        worker_id=str(worker) if worker else None,
                        request_provider_session=want_provider,
                        session_resume_gate=session_resume_gate,
                        provider_resume=provider_resume,
                    )
                else:
                    # Default: reconcile-only; never touches tmux clients.
                    result = resume_for_identity(root, identity)
            except OperatorError as exc:
                emit_data(
                    args,
                    "team.resume",
                    {
                        "ok": False,
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            **({"status": exc.status} if exc.status else {}),
                        },
                    },
                )
                return exc.exit_code
            except TeamApiError as exc:
                emit_data(
                    args,
                    "team.resume",
                    {
                        "ok": False,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            **(
                                {"details": exc.details} if exc.details else {}
                            ),
                        },
                    },
                )
                if not wants_json(args) and not as_json:
                    print(f"omg team resume: {exc.message}", file=sys.stderr)
                return int(exc.exit_code)
            # Always JSON (operator machine-readable); --json kept for symmetry.
            emit_data(args, "team", result)
            claim_reconcile = result.get("claim_reconcile")
            if not isinstance(claim_reconcile, dict):
                nested = result.get("reconcile")
                if isinstance(nested, dict):
                    claim_reconcile = nested.get("claim_reconcile")
            if (
                isinstance(claim_reconcile, dict)
                and not wants_json(args)
                and not as_json
            ):
                print(
                    "claims: preserved="
                    f"{len(claim_reconcile.get('preserved_unexpired') or [])} "
                    "released_expired="
                    f"{len(claim_reconcile.get('released_expired') or [])} "
                    f"scanned={int(claim_reconcile.get('scanned') or 0)}",
                    file=sys.stderr,
                )
            if result.get("layout_repair_needed") or (
                isinstance(result.get("reconcile"), dict)
                and result["reconcile"].get("layout_repair_needed")
            ):
                layout_status = result.get("layout_status")
                if isinstance(result.get("reconcile"), dict):
                    layout_status = result["reconcile"].get(
                        "layout_status", layout_status
                    )
                print(
                    "warning: resume completed with layout_repair_needed=true "
                    f"(status={layout_status!r})",
                    file=sys.stderr,
                )
            provider = result.get("provider_session") or {}
            if (
                isinstance(provider, dict)
                and provider.get("requested")
                and (
                    provider.get("status") == "blocked"
                    or provider.get("ok") is False
                    or (
                        isinstance(provider.get("execution"), dict)
                        and provider["execution"].get("status") == "failed"
                    )
                )
            ):
                # Fail closed: required provider resume refused by host gate
                # or ACP transport execution failed. tmux view success must
                # not flip this to success.
                return 1
            if want_view or print_only:
                view = result.get("view") or {}
                if print_only and result.get("print_hint"):
                    print(result["print_hint"])
                if as_json:
                    # --json never attaches; still honor provider fail-closed above.
                    return 0 if result.get("ok", True) else 1
                if not result.get("ok", True):
                    return 2 if view.get("status") == "refused" else 1
            return 0 if result.get("ok", True) else 1

        if action == "view":
            from omg_cli.team.operator import OperatorError
            from omg_cli.team.runtime import view_team

            identity = getattr(args, "team_identity", None) or getattr(
                args, "run_id", None
            )
            as_json = bool(
                getattr(args, "as_json", False)
                or getattr(args, "json_output", False)
            )
            print_only = bool(getattr(args, "view_print", False))
            takeover = bool(getattr(args, "view_takeover", False))
            worker = getattr(args, "worker_id", None)
            try:
                result = view_team(
                    root,
                    identity,
                    print_only=print_only,
                    takeover=takeover,
                    as_json=as_json,
                    worker_id=str(worker) if worker else None,
                )
            except OperatorError as exc:
                emit_data(
                    args,
                    "team.view",
                    {
                        "ok": False,
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            **({"status": exc.status} if exc.status else {}),
                        },
                    },
                )
                return exc.exit_code
            if as_json:
                emit_data(args, "team.view", result)
                if not result.get("ok", True):
                    view = result.get("view") or {}
                    return 2 if view.get("status") == "refused" else 1
                return 0
            if print_only and result.get("print_hint"):
                print(result["print_hint"])
                if not result.get("ok", True):
                    return 2
                return 0
            emit_data(args, "team.view", result)
            if not result.get("ok", True):
                view = result.get("view") or {}
                return 2 if view.get("status") == "refused" else 1
            return 0

        if action == "status":
            from omg_cli.team.runtime import status_for_identity

            identity = getattr(args, "team_identity", None) or getattr(
                args, "run_id", None
            )
            if getattr(args, "presentation_status", False):
                from omg_cli.team.presentation import (
                    PresentationError,
                    build_team_presentation_v1,
                )
                from omg_cli.team.runtime import resolve_team_ref

                try:
                    rid = resolve_team_ref(root, identity)
                    presentation = build_team_presentation_v1(root, rid)
                except PresentationError as exc:
                    print(
                        f"team status --presentation failed: {exc.code}: {exc}",
                        file=sys.stderr,
                    )
                    return 1
                except (OSError, RuntimeError, ValueError) as exc:
                    print(
                        f"team status --presentation failed: {exc}",
                        file=sys.stderr,
                    )
                    return 1
                if getattr(args, "as_json", False):
                    emit_data(args, "team.presentation", presentation)
                else:
                    print(
                        json.dumps(
                            presentation, indent=2, ensure_ascii=False, sort_keys=True
                        )
                    )
                return 0
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
            # Legacy v1 helper (#99). Writes wrapper_ready_legacy only — does
            # NOT prove provider readiness for new Team launches.
            # Public CLI still emits JSON (#100); internal path is silent.
            from omg_cli.team.bootstrap import (
                pane_failure_line,
                worker_ready_internal,
            )

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
            result = worker_ready_internal(
                ready_root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                source="process",
            )
            if not result.ok:
                print(
                    pane_failure_line(worker_id=worker_id, run_id=run_id),
                    file=sys.stderr,
                )
                return 1
            emit_data(
                args,
                "team.worker-ready",
                {
                    "ok": True,
                    "legacy": True,
                    "worker_id": worker_id,
                    "run_id": run_id,
                    "team_id": team_id,
                    "ready_written": True,
                    "note": (
                        "v1 wrapper receipt only; cannot prove provider_ready (#99)"
                    ),
                },
            )
            return 0
        if action == "supervisor":
            from omg_cli.team.bootstrap import (
                BootstrapError,
                append_bootstrap_log,
                bootstrap_env_identity,
                classify_bootstrap_exception,
                pane_failure_line,
            )
            from omg_cli.team.supervisor import SupervisorError, run_supervisor

            desc = getattr(args, "supervisor_descriptor", None)
            if not desc:
                # Missing descriptor is a CLI usage error (not a pane bootstrap).
                print(
                    "omg team supervisor: --descriptor PATH required",
                    file=sys.stderr,
                )
                return 2
            worker_id: str | None = None
            run_id: str | None = None
            try:
                run_id, team_id, worker_id, leader = bootstrap_env_identity()
                append_bootstrap_log(
                    leader,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase="BOOTSTRAP_BEGIN",
                    code="SUPERVISOR",
                )
                append_bootstrap_log(
                    leader,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase="ROOT_VALIDATED",
                    code="SUPERVISOR",
                )
                # run_supervisor owns pane-facing failure lines for its errors.
                return int(
                    run_supervisor(
                        descriptor_path=desc,
                        ready_timeout_s=getattr(
                            args, "supervisor_ready_timeout_s", None
                        ),
                    )
                )
            except (BootstrapError, SupervisorError) as exc:
                code = classify_bootstrap_exception(exc)
                if run_id and worker_id:
                    try:
                        leader_root = (
                            os.environ.get("OMG_TEAM_LEADER_ROOT")
                            or os.environ.get("OMG_PROJECT_ROOT")
                            or ""
                        ).strip()
                        if leader_root:
                            append_bootstrap_log(
                                Path(leader_root),
                                run_id=run_id,
                                team_id=(
                                    os.environ.get("OMG_TEAM_ID") or "team"
                                ).strip()
                                or "team",
                                worker_id=worker_id,
                                phase="BOOTSTRAP_FAIL",
                                code=code.value,
                                summary=str(exc),
                            )
                    except Exception:  # noqa: BLE001 — never poison pane
                        pass
                print(
                    pane_failure_line(worker_id=worker_id, run_id=run_id),
                    file=sys.stderr,
                )
                return int(getattr(exc, "exit_code", 1) or 1)
        if action == "hyperplan":
            from omg_cli.cli_envelope import wants_json
            from omg_cli.team.compositions.hyperplan import (
                HyperplanError,
                admit_hyperplan_tasks_v1,
                claim_hyperplan_lane_v1,
                collect_hyperplan_tasks_v1,
                compile_hyperplan_v1,
                materialize_hyperplan_v1,
                produce_hyperplan_decision_v1,
                submit_hyperplan_lane_result_v1,
                validate_hyperplan_decision_v1,
            )
            from omg_cli.team.plane import TeamGateError, experimental_enabled

            if not experimental_enabled():
                raise TeamGateError("team plane disabled by kill-switch")

            hp_action = getattr(args, "hyperplan_action", None)
            spec_path = getattr(args, "hyperplan_spec", None)
            decision_path = getattr(args, "hyperplan_decision", None)
            bundle_path = getattr(args, "hyperplan_bundle", None)
            run_id = getattr(args, "run_id", None)

            def _load_json_file(path_s: str, *, label: str) -> Any:
                path = Path(path_s)
                if path.is_symlink() or not path.is_file():
                    raise HyperplanError(
                        f"{label} must be a regular non-symlink file",
                        code="E_TEAM_HYPERPLAN_SPEC",
                    )
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HyperplanError(
                        f"{label} unreadable JSON: {exc}",
                        code="E_TEAM_HYPERPLAN_SPEC",
                    ) from exc

            try:
                if hp_action == "plan":
                    if not spec_path:
                        print(
                            "omg team hyperplan plan: --spec required",
                            file=sys.stderr,
                        )
                        return 2
                    manifest = compile_hyperplan_v1(
                        _load_json_file(str(spec_path), label="--spec")
                    )
                    emit_data(args, "team.hyperplan", manifest)
                    if not wants_json(args):
                        print(
                            f"hyperplan plan composition_id={manifest['composition_id']} "
                            f"lanes={manifest['lane_count']} "
                            f"execution_supported={manifest['execution_supported']}",
                            file=sys.stderr,
                        )
                    return 0
                if hp_action == "materialize":
                    if not spec_path or not run_id:
                        print(
                            "omg team hyperplan materialize: --spec and --run required",
                            file=sys.stderr,
                        )
                        return 2
                    result = materialize_hyperplan_v1(
                        root,
                        str(run_id),
                        _load_json_file(str(spec_path), label="--spec"),
                    )
                    emit_data(args, "team.hyperplan", result)
                    if not wants_json(args):
                        tag = "idempotent" if result.get("idempotent") else "wrote"
                        print(
                            f"hyperplan materialize {tag} "
                            f"path={result.get('path')} "
                            f"composition_id={result['manifest']['composition_id']}",
                            file=sys.stderr,
                        )
                    return 0
                if hp_action == "validate-decision":
                    if not run_id or not decision_path:
                        print(
                            "omg team hyperplan validate-decision: "
                            "--run and --input required",
                            file=sys.stderr,
                        )
                        return 2
                    result = validate_hyperplan_decision_v1(
                        root,
                        str(run_id),
                        _load_json_file(str(decision_path), label="--input"),
                        persist=True,
                    )
                    emit_data(args, "team.hyperplan", result)
                    if not wants_json(args):
                        decision = result.get("decision") or {}
                        print(
                            f"hyperplan decision ok verdict={decision.get('verdict')} "
                            f"path={result.get('path')}",
                            file=sys.stderr,
                        )
                    return 0
                if hp_action == "produce-decision":
                    if not run_id or not bundle_path:
                        print(
                            "omg team hyperplan produce-decision: "
                            "--run and --input required",
                            file=sys.stderr,
                        )
                        return 2
                    result = produce_hyperplan_decision_v1(
                        root,
                        str(run_id),
                        _load_json_file(str(bundle_path), label="--input"),
                    )
                    emit_data(args, "team.hyperplan", result)
                    if not wants_json(args):
                        tag = "idempotent" if result.get("idempotent") else "wrote"
                        decision = result.get("decision") or {}
                        print(
                            f"hyperplan produce-decision {tag} "
                            f"verdict={decision.get('verdict')} "
                            f"path={result.get('path')}",
                            file=sys.stderr,
                        )
                    return 0
                if hp_action == "admit-tasks":
                    team_id = getattr(args, "team_id", None)
                    if not run_id or not team_id:
                        print(
                            "omg team hyperplan admit-tasks: "
                            "--run and --team-id required",
                            file=sys.stderr,
                        )
                        return 2
                    result = admit_hyperplan_tasks_v1(
                        root, str(run_id), str(team_id)
                    )
                    emit_data(args, "team.hyperplan", result)
                    if not wants_json(args):
                        tag = "idempotent" if result.get("idempotent") else "admitted"
                        print(
                            f"hyperplan admit-tasks {tag} "
                            f"batch={result.get('batch_id')} "
                            f"tasks={len(result.get('task_key_to_id') or {})} "
                            f"execution_supported={result.get('execution_supported')}",
                            file=sys.stderr,
                        )
                    return 0
                if hp_action == "collect-tasks":
                    team_id = getattr(args, "team_id", None)
                    if not run_id or not team_id:
                        print(
                            "omg team hyperplan collect-tasks: "
                            "--run and --team-id required",
                            file=sys.stderr,
                        )
                        return 2
                    result = collect_hyperplan_tasks_v1(
                        root, str(run_id), str(team_id)
                    )
                    emit_data(args, "team.hyperplan", result)
                    if not wants_json(args):
                        tag = "idempotent" if result.get("idempotent") else "wrote"
                        decision = result.get("decision") or {}
                        print(
                            f"hyperplan collect-tasks {tag} "
                            f"verdict={decision.get('verdict')} "
                            f"path={result.get('path')}",
                            file=sys.stderr,
                        )
                    return 0
                if hp_action == "claim-lane":
                    from omg_cli.team.compositions.lane_protocol import (
                        redact_claim_token,
                    )

                    team_id = getattr(args, "team_id", None)
                    lane_id = getattr(args, "lane_id", None)
                    if not run_id or not team_id or not lane_id:
                        print(
                            "omg team hyperplan claim-lane: "
                            "--run, --team-id, and --lane-id required",
                            file=sys.stderr,
                        )
                        return 2
                    result = claim_hyperplan_lane_v1(
                        root, str(run_id), str(team_id), str(lane_id)
                    )
                    emit_data(args, "team.hyperplan", result)
                    if not wants_json(args):
                        claim = redact_claim_token(result.get("claim") or {})
                        print(
                            f"hyperplan claim-lane lane={claim.get('lane_id')} "
                            f"task={claim.get('task_id')} "
                            f"worker={claim.get('worker_id')} "
                            f"claim_token=<redacted> "
                            f"execution_supported={result.get('execution_supported')}",
                            file=sys.stderr,
                        )
                    return 0
                if hp_action == "submit-lane-result":
                    team_id = getattr(args, "team_id", None)
                    claim_file = getattr(args, "claim_file", None)
                    result_file = getattr(args, "result_file", None)
                    if not run_id or not team_id or not claim_file or not result_file:
                        print(
                            "omg team hyperplan submit-lane-result: "
                            "--run, --team-id, --claim-file, and --result required",
                            file=sys.stderr,
                        )
                        return 2
                    claim_doc = _load_json_file(
                        str(claim_file), label="claim-file"
                    )
                    result_doc = _load_json_file(
                        str(result_file), label="result"
                    )
                    result = submit_hyperplan_lane_result_v1(
                        root,
                        str(run_id),
                        str(team_id),
                        claim=claim_doc,
                        result=result_doc,
                    )
                    emit_data(args, "team.hyperplan", result)
                    if not wants_json(args):
                        tag = "idempotent" if result.get("idempotent") else "submitted"
                        print(
                            f"hyperplan submit-lane-result {tag} "
                            f"lane={result.get('lane_id')} "
                            f"task={result.get('task_id')} "
                            f"lane_status={result.get('lane_result_status')} "
                            f"execution_supported={result.get('execution_supported')}",
                            file=sys.stderr,
                        )
                    return 0
                print(
                    f"omg team hyperplan: unknown action {hp_action!r}",
                    file=sys.stderr,
                )
                return 2
            except HyperplanError as exc:
                emit_data(
                    args,
                    "team.hyperplan",
                    {
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message},
                    },
                )
                print(f"omg team hyperplan: {exc.code}: {exc}", file=sys.stderr)
                return 2

        if action == "security-research":
            from omg_cli.cli_envelope import wants_json
            from omg_cli.team.compositions.security_research import (
                SecurityResearchError,
                admit_security_research_tasks_v1,
                claim_security_research_lane_v1,
                collect_security_research_tasks_v1,
                compile_security_research_v1,
                materialize_security_research_v1,
                produce_security_research_report_v1,
                submit_security_research_lane_result_v1,
                validate_security_research_report_v1,
            )
            from omg_cli.team.plane import TeamGateError, experimental_enabled

            if not experimental_enabled():
                raise TeamGateError("team plane disabled by kill-switch")

            sr_action = getattr(args, "security_research_action", None)
            spec_path = getattr(args, "security_research_spec", None)
            report_path = getattr(args, "security_research_report", None)
            bundle_path = getattr(args, "security_research_bundle", None)
            run_id = getattr(args, "run_id", None)

            def _load_sr_json_file(path_s: str, *, label: str) -> Any:
                path = Path(path_s)
                if path.is_symlink() or not path.is_file():
                    raise SecurityResearchError(
                        f"{label} must be a regular non-symlink file",
                        code="E_TEAM_SECURITY_RESEARCH_SPEC",
                    )
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SecurityResearchError(
                        f"{label} unreadable JSON: {exc}",
                        code="E_TEAM_SECURITY_RESEARCH_SPEC",
                    ) from exc

            try:
                if sr_action == "plan":
                    if not spec_path:
                        print(
                            "omg team security-research plan: --spec required",
                            file=sys.stderr,
                        )
                        return 2
                    manifest = compile_security_research_v1(
                        _load_sr_json_file(str(spec_path), label="--spec")
                    )
                    emit_data(args, "team.security_research", manifest)
                    if not wants_json(args):
                        print(
                            f"security-research plan "
                            f"composition_id={manifest['composition_id']} "
                            f"lanes={manifest['lane_count']} "
                            f"execution_supported={manifest['execution_supported']}",
                            file=sys.stderr,
                        )
                    return 0
                if sr_action == "materialize":
                    if not spec_path or not run_id:
                        print(
                            "omg team security-research materialize: "
                            "--spec and --run required",
                            file=sys.stderr,
                        )
                        return 2
                    result = materialize_security_research_v1(
                        root,
                        str(run_id),
                        _load_sr_json_file(str(spec_path), label="--spec"),
                    )
                    emit_data(args, "team.security_research", result)
                    if not wants_json(args):
                        tag = "idempotent" if result.get("idempotent") else "wrote"
                        print(
                            f"security-research materialize {tag} "
                            f"path={result.get('path')} "
                            f"composition_id={result['manifest']['composition_id']}",
                            file=sys.stderr,
                        )
                    return 0
                if sr_action == "validate-report":
                    if not run_id or not report_path:
                        print(
                            "omg team security-research validate-report: "
                            "--run and --input required",
                            file=sys.stderr,
                        )
                        return 2
                    result = validate_security_research_report_v1(
                        root,
                        str(run_id),
                        _load_sr_json_file(str(report_path), label="--input"),
                        persist=True,
                    )
                    emit_data(args, "team.security_research", result)
                    if not wants_json(args):
                        report = result.get("report") or {}
                        print(
                            f"security-research report ok "
                            f"verdict={report.get('verdict')} "
                            f"path={result.get('path')}",
                            file=sys.stderr,
                        )
                    return 0
                if sr_action == "produce-report":
                    if not run_id or not bundle_path:
                        print(
                            "omg team security-research produce-report: "
                            "--run and --input required",
                            file=sys.stderr,
                        )
                        return 2
                    result = produce_security_research_report_v1(
                        root,
                        str(run_id),
                        _load_sr_json_file(str(bundle_path), label="--input"),
                    )
                    emit_data(args, "team.security_research", result)
                    if not wants_json(args):
                        report = result.get("report") or {}
                        tag = "idempotent" if result.get("idempotent") else "wrote"
                        print(
                            f"security-research produce-report {tag} "
                            f"verdict={report.get('verdict')} "
                            f"path={result.get('path')}",
                            file=sys.stderr,
                        )
                    return 0
                if sr_action == "admit-tasks":
                    team_id = getattr(args, "team_id", None)
                    if not run_id or not team_id:
                        print(
                            "omg team security-research admit-tasks: "
                            "--run and --team-id required",
                            file=sys.stderr,
                        )
                        return 2
                    result = admit_security_research_tasks_v1(
                        root, str(run_id), str(team_id)
                    )
                    emit_data(args, "team.security_research", result)
                    if not wants_json(args):
                        tag = "idempotent" if result.get("idempotent") else "admitted"
                        print(
                            f"security-research admit-tasks {tag} "
                            f"batch={result.get('batch_id')} "
                            f"tasks={len(result.get('task_key_to_id') or {})} "
                            f"execution_supported={result.get('execution_supported')}",
                            file=sys.stderr,
                        )
                    return 0
                if sr_action == "collect-tasks":
                    team_id = getattr(args, "team_id", None)
                    if not run_id or not team_id:
                        print(
                            "omg team security-research collect-tasks: "
                            "--run and --team-id required",
                            file=sys.stderr,
                        )
                        return 2
                    result = collect_security_research_tasks_v1(
                        root, str(run_id), str(team_id)
                    )
                    emit_data(args, "team.security_research", result)
                    if not wants_json(args):
                        report = result.get("report") or {}
                        tag = "idempotent" if result.get("idempotent") else "wrote"
                        print(
                            f"security-research collect-tasks {tag} "
                            f"verdict={report.get('verdict')} "
                            f"path={result.get('path')}",
                            file=sys.stderr,
                        )
                    return 0
                if sr_action == "claim-lane":
                    from omg_cli.team.compositions.lane_protocol import (
                        redact_claim_token,
                    )

                    team_id = getattr(args, "team_id", None)
                    lane_id = getattr(args, "lane_id", None)
                    if not run_id or not team_id or not lane_id:
                        print(
                            "omg team security-research claim-lane: "
                            "--run, --team-id, and --lane-id required",
                            file=sys.stderr,
                        )
                        return 2
                    result = claim_security_research_lane_v1(
                        root, str(run_id), str(team_id), str(lane_id)
                    )
                    emit_data(args, "team.security_research", result)
                    if not wants_json(args):
                        claim = redact_claim_token(result.get("claim") or {})
                        print(
                            f"security-research claim-lane "
                            f"lane={claim.get('lane_id')} "
                            f"task={claim.get('task_id')} "
                            f"worker={claim.get('worker_id')} "
                            f"claim_token=<redacted> "
                            f"execution_supported={result.get('execution_supported')}",
                            file=sys.stderr,
                        )
                    return 0
                if sr_action == "submit-lane-result":
                    team_id = getattr(args, "team_id", None)
                    claim_file = getattr(args, "claim_file", None)
                    result_file = getattr(args, "result_file", None)
                    if not run_id or not team_id or not claim_file or not result_file:
                        print(
                            "omg team security-research submit-lane-result: "
                            "--run, --team-id, --claim-file, and --result required",
                            file=sys.stderr,
                        )
                        return 2
                    claim_doc = _load_sr_json_file(
                        str(claim_file), label="claim-file"
                    )
                    result_doc = _load_sr_json_file(
                        str(result_file), label="result"
                    )
                    result = submit_security_research_lane_result_v1(
                        root,
                        str(run_id),
                        str(team_id),
                        claim=claim_doc,
                        result=result_doc,
                    )
                    emit_data(args, "team.security_research", result)
                    if not wants_json(args):
                        tag = "idempotent" if result.get("idempotent") else "submitted"
                        print(
                            f"security-research submit-lane-result {tag} "
                            f"lane={result.get('lane_id')} "
                            f"task={result.get('task_id')} "
                            f"lane_status={result.get('lane_result_status')} "
                            f"execution_supported={result.get('execution_supported')}",
                            file=sys.stderr,
                        )
                    return 0
                print(
                    f"omg team security-research: unknown action {sr_action!r}",
                    file=sys.stderr,
                )
                return 2
            except SecurityResearchError as exc:
                emit_data(
                    args,
                    "team.security_research",
                    {
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message},
                    },
                )
                print(
                    f"omg team security-research: {exc.code}: {exc}",
                    file=sys.stderr,
                )
                return 2

        if action == "api":
            from omg_cli.team.api import (
                TeamApiError,
                execute_team_api,
                parse_input_json,
                resolve_team_api_cli_root,
            )

            op = getattr(args, "api_op", None) or ""
            # ``catalog`` handled above (before project_root); keep a guard.
            if op == "catalog":
                from omg_cli.team.operation_catalog import catalog_document_json

                print(catalog_document_json(), end="")
                return 0
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

        # --- #101 identity-fenced operator pane control -------------------
        if action in {"panes", "capture", "focus", "key", "input", "watch"}:
            from omg_cli.team.operator import (
                OperatorError,
                capture_worker,
                focus_worker,
                input_worker,
                key_worker,
                list_panes,
                watch_worker,
            )

            identity = getattr(args, "team_identity", None) or getattr(
                args, "run_id", None
            )
            as_json = bool(
                getattr(args, "as_json", False)
                or getattr(args, "json_output", False)
            )
            try:
                if action == "panes":
                    result = list_panes(root, identity, probe=True)
                    emit_data(args, "team.panes", result)
                    return 0
                worker = getattr(args, "worker_id", None)
                if action != "watch" and not worker:
                    print(
                        f"omg team {action}: --worker required",
                        file=sys.stderr,
                    )
                    return 2
                if action == "capture":
                    result = capture_worker(
                        root,
                        identity,
                        str(worker),
                        lines=getattr(args, "capture_lines", None),
                        raw=bool(getattr(args, "capture_raw", False)),
                    )
                    if as_json:
                        emit_data(args, "team.capture", result)
                    elif not result.get("ok"):
                        print(
                            f"omg team capture: status={result.get('status')}",
                            file=sys.stderr,
                        )
                    else:
                        text = result.get("text") or ""
                        sys.stdout.write(text)
                        if text and not str(text).endswith("\n"):
                            sys.stdout.write("\n")
                    if not result.get("ok"):
                        status = str(result.get("status") or "")
                        return 2 if status == "identity_mismatch" else 1
                    return 0
                if action == "focus":
                    result = focus_worker(
                        root,
                        identity,
                        str(worker),
                        as_json=as_json,
                        execute=bool(getattr(args, "focus_execute", False)),
                    )
                    if as_json:
                        emit_data(args, "team.focus", result)
                    elif not result.get("focused"):
                        hint = result.get("attach_hint")
                        if hint:
                            print(hint)
                    return 0
                if action == "key":
                    result = key_worker(
                        root,
                        identity,
                        str(worker),
                        str(getattr(args, "key_name", "") or ""),
                        as_json=as_json,
                        operator_override=bool(
                            getattr(args, "operator_override", False)
                        ),
                    )
                    emit_data(args, "team.key", result)
                    return 0
                if action == "input":
                    result = input_worker(
                        root,
                        identity,
                        str(worker),
                        str(getattr(args, "input_text", "") or ""),
                        submit=bool(getattr(args, "input_submit", False)),
                        as_json=as_json,
                        operator_override=bool(
                            getattr(args, "operator_override", False)
                        ),
                    )
                    emit_data(args, "team.input", result)
                    return 0
                # watch
                interval = getattr(args, "watch_interval", 1.0)
                try:
                    interval_f = float(interval)
                except (TypeError, ValueError):
                    print(
                        "omg team watch: --interval must be a number of seconds",
                        file=sys.stderr,
                    )
                    return 2
                result = watch_worker(
                    root,
                    identity,
                    str(worker) if worker else None,
                    interval_s=interval_f,
                    lines=getattr(args, "capture_lines", None),
                    as_json=as_json,
                    max_iterations=int(
                        getattr(args, "watch_max_iterations", 3600) or 3600
                    ),
                )
                if not as_json:
                    emit_data(args, "team.watch", result)
                return 0 if result.get("ok") else 1
            except OperatorError as exc:
                emit_data(
                    args,
                    f"team.{action}",
                    {
                        "ok": False,
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            **({"status": exc.status} if exc.status else {}),
                            **(
                                {"details": exc.details}
                                if exc.details
                                else {}
                            ),
                        },
                    },
                )
                if not as_json:
                    print(f"omg team {action}: {exc}", file=sys.stderr)
                return int(exc.exit_code)

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
            "also start|run|api|supervisor|worker-ready|…"
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
    p_t_launch.add_argument(
        "--dedicated-window",
        dest="dedicated_window",
        action="store_true",
        help=(
            "inside tmux: place workers in a dedicated omg-team window "
            "(default keeps leader + workers in the same window)"
        ),
    )
    p_t_launch.add_argument(
        "--worker-topology",
        dest="worker_topology",
        choices=("pane", "job"),
        default="pane",
        help=(
            "worker execution topology (#69 PR4): pane (default tmux) or "
            "job (durable Jobs plane; requires fake|antigravity provider)"
        ),
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
    p_t_start.add_argument(
        "--worker-topology",
        dest="worker_topology",
        choices=("pane", "job"),
        default="pane",
        help=(
            "worker execution topology (#69 PR4): pane (default tmux) or "
            "job (durable Jobs plane; requires fake|antigravity provider)"
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
    p_t_run.add_argument(
        "--worker-topology",
        dest="worker_topology",
        choices=("pane", "job"),
        default="pane",
        help=(
            "worker execution topology for team-exec (#69 PR4): pane "
            "(default) or job (durable Jobs plane)"
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
            "(idempotent status write; never sets verified; "
            "default never attaches — pass --view to restore tmux view)"
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
    p_t_resume.add_argument(
        "--view",
        dest="resume_view",
        action="store_true",
        help=(
            "after reconcile, restore exact Team window/leader pane "
            "(never implied by TTY or --json)"
        ),
    )
    p_t_resume.add_argument(
        "--print",
        dest="view_print",
        action="store_true",
        help="with --view: print exact tmux argv only (no client effect)",
    )
    p_t_resume.add_argument(
        "--takeover",
        dest="view_takeover",
        action="store_true",
        help="with --view: attach-session -d (detaches other clients)",
    )
    p_t_resume.add_argument(
        "--worker",
        dest="worker_id",
        default=None,
        help="with --view: focus exact worker pane via #101 instead of leader",
    )
    p_t_resume.add_argument(
        "--provider-session",
        dest="provider_session",
        action="store_true",
        help=(
            "request host ACP provider-session resume gated by host_probe "
            "(independent of --view; missing cap → LEGACY next_action; "
            "BLOCKED fails closed; does not imply tmux attach success)"
        ),
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
    p_t_status.add_argument(
        "--presentation",
        dest="presentation_status",
        action="store_true",
        help=(
            "emit Team Presentation State V1 (read-only; identical to "
            "catalog read-presentation-state / MCP projection=presentation.v1); "
            "does not change default --json / --full schemas"
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
            "legacy v1 wrapper receipt only (#99); cannot prove provider_ready "
            "(reads OMG_TEAM_* env)"
        ),
    )
    p_t_ready.set_defaults(func=cmd_team, team_action="worker-ready")

    p_t_sup = team_sub.add_parser(
        "supervisor",
        parents=[common],
        help=(
            "pane supervisor: spawn provider from vetted --descriptor JSON and "
            "write schema-v2 startup phases (#99)"
        ),
    )
    p_t_sup.add_argument(
        "--descriptor",
        dest="supervisor_descriptor",
        required=True,
        help="path to provider argv descriptor JSON (schema_version=1)",
    )
    p_t_sup.add_argument(
        "--ready-timeout",
        dest="supervisor_ready_timeout_s",
        type=float,
        default=None,
        metavar="SECONDS",
        help="bounded provider readiness wait (default: OMG_TEAM_SUPERVISOR_READY_S or 30)",
    )
    p_t_sup.set_defaults(func=cmd_team, team_action="supervisor")

    # --- #101 identity-fenced operator pane control -----------------------
    def _add_team_identity_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "team_identity",
            nargs="?",
            default=None,
            help="team name or run_id (optional; default active / --run)",
        )
        parser.add_argument(
            "--run", dest="run_id", default=None, help="run_id (default: active)"
        )

    def _add_worker_arg(parser: argparse.ArgumentParser, *, required: bool) -> None:
        parser.add_argument(
            "--worker",
            dest="worker_id",
            required=required,
            help="worker / task id (exact Team worker)",
        )

    p_t_view = team_sub.add_parser(
        "view",
        parents=[common],
        help=(
            "restore exact Team tmux view without reconcile/relaunch (#103); "
            "--json never attaches; --print prints argv only"
        ),
    )
    _add_team_identity_args(p_t_view)
    p_t_view.add_argument(
        "--print",
        dest="view_print",
        action="store_true",
        help="print exact tmux attach/switch/select argv without executing",
    )
    p_t_view.add_argument(
        "--takeover",
        dest="view_takeover",
        action="store_true",
        help="outside tmux: attach-session -d (detaches other clients)",
    )
    p_t_view.add_argument(
        "--worker",
        dest="worker_id",
        default=None,
        help="focus exact worker pane via #101 instead of leader",
    )
    p_t_view.set_defaults(func=cmd_team, team_action="view")

    p_t_panes = team_sub.add_parser(
        "panes",
        parents=[common],
        help=(
            "list exact live Team worker panes with authorization flags (#101); "
            "no argv/prompt/env/tokens"
        ),
    )
    _add_team_identity_args(p_t_panes)
    p_t_panes.set_defaults(func=cmd_team, team_action="panes")

    p_t_capture = team_sub.add_parser(
        "capture",
        parents=[common],
        help=(
            "bounded identity-fenced pane capture (#101); redacted; "
            "status live|gone|identity_mismatch|unknown"
        ),
    )
    _add_team_identity_args(p_t_capture)
    _add_worker_arg(p_t_capture, required=True)
    p_t_capture.add_argument(
        "--lines",
        dest="capture_lines",
        type=int,
        default=200,
        help="max history lines (default 200, hard cap 2000)",
    )
    p_t_capture.add_argument(
        "--raw",
        dest="capture_raw",
        action="store_true",
        help="skip ANSI strip / line-join normalize (still bounded + redacted)",
    )
    p_t_capture.set_defaults(func=cmd_team, team_action="capture")

    p_t_focus = team_sub.add_parser(
        "focus",
        parents=[common],
        help=(
            "focus exact worker pane (#101); --json never focuses; "
            "outside tmux prints attach argv (use --execute to attach)"
        ),
    )
    _add_team_identity_args(p_t_focus)
    _add_worker_arg(p_t_focus, required=True)
    p_t_focus.add_argument(
        "--execute",
        dest="focus_execute",
        action="store_true",
        help="outside tmux: actually run attach argv (TTY required)",
    )
    p_t_focus.set_defaults(func=cmd_team, team_action="focus")

    p_t_key = team_sub.add_parser(
        "key",
        parents=[common],
        help=(
            "send one allowlisted key to an exact worker pane (#101); "
            "--json never delivers; requires TTY or --operator-override"
        ),
    )
    _add_team_identity_args(p_t_key)
    _add_worker_arg(p_t_key, required=True)
    p_t_key.add_argument(
        "--key",
        dest="key_name",
        required=True,
        help="allowlisted key (Enter, Escape, Tab, arrows, C-c, …)",
    )
    p_t_key.add_argument(
        "--operator-override",
        dest="operator_override",
        action="store_true",
        help="allow non-TTY key delivery (still audited; not for agents)",
    )
    p_t_key.set_defaults(func=cmd_team, team_action="key")

    p_t_input = team_sub.add_parser(
        "input",
        parents=[common],
        help=(
            "send bounded literal text via send-keys -l (#101); "
            "audit stores length/hash only; prefer team api for automation"
        ),
    )
    _add_team_identity_args(p_t_input)
    _add_worker_arg(p_t_input, required=True)
    p_t_input.add_argument(
        "--text",
        dest="input_text",
        required=True,
        help="literal UTF-8 text (no key-name interpretation)",
    )
    p_t_input.add_argument(
        "--submit",
        dest="input_submit",
        action="store_true",
        help="also send Enter after the literal text",
    )
    p_t_input.add_argument(
        "--operator-override",
        dest="operator_override",
        action="store_true",
        help="allow non-TTY operator input (still audited; not for agents)",
    )
    p_t_input.set_defaults(func=cmd_team, team_action="input")

    p_t_watch = team_sub.add_parser(
        "watch",
        parents=[common],
        help=(
            "poll bounded capture for a worker (#101); observation only — "
            "never auto-input/focus/execute"
        ),
    )
    _add_team_identity_args(p_t_watch)
    _add_worker_arg(p_t_watch, required=False)
    p_t_watch.add_argument(
        "--interval",
        dest="watch_interval",
        type=float,
        default=1.0,
        help="poll interval seconds (default 1; min 0.5; max 60)",
    )
    p_t_watch.add_argument(
        "--lines",
        dest="capture_lines",
        type=int,
        default=200,
        help="capture lines per poll (default 200)",
    )
    p_t_watch.add_argument(
        "--max-iterations",
        dest="watch_max_iterations",
        type=int,
        default=3600,
        help="stop after N polls (default 3600)",
    )
    p_t_watch.set_defaults(func=cmd_team, team_action="watch")

    p_t_api = team_sub.add_parser(
        "api",
        parents=[common],
        help=(
            "OMX-shaped team api façade (P0 mailbox/task ops); "
            "OP=catalog dumps versioned operation catalog (no --input); "
            "default on; set OMG_DISABLE_TMUX_TEAM=1 to refuse"
        ),
    )
    p_t_api.add_argument(
        "api_op",
        metavar="OP",
        help=(
            "operation name, or 'catalog' for the versioned operation catalog "
            "(see omg_cli.team.operation_catalog / docs/team-operation-catalog-v4.md)"
        ),
    )
    p_t_api.add_argument(
        "--input",
        dest="api_input",
        required=False,
        default=None,
        help="JSON object input (required except for OP=catalog)",
    )
    p_t_api.add_argument(
        "--run",
        dest="run_id",
        default=None,
        help="run_id injected into --input when omitted there",
    )
    # --json inherited from common → json_output
    p_t_api.set_defaults(func=cmd_team, team_action="api")

    p_t_hp = team_sub.add_parser(
        "hyperplan",
        parents=[common],
        help=(
            "Hyperplan Composition Contract V1 (hermetic produce + task driver + "
            "lane worker protocol; non-executing): plan|materialize|"
            "validate-decision|produce-decision|admit-tasks|collect-tasks|"
            "claim-lane|submit-lane-result (#69 PR13)"
        ),
    )
    hp_sub = p_t_hp.add_subparsers(dest="hyperplan_action")
    p_hp_plan = hp_sub.add_parser(
        "plan",
        parents=[common],
        help="compile HyperplanSpecV1 → ManifestV1 (zero filesystem mutation)",
    )
    p_hp_plan.add_argument(
        "--spec",
        dest="hyperplan_spec",
        required=True,
        help="path to HyperplanSpecV1 JSON",
    )
    p_hp_plan.set_defaults(func=cmd_team, team_action="hyperplan", hyperplan_action="plan")

    p_hp_mat = hp_sub.add_parser(
        "materialize",
        parents=[common],
        help=(
            "atomically persist manifest under "
            ".omg/state/runs/<run>/team/compositions/hyperplan-v1.json"
        ),
    )
    p_hp_mat.add_argument(
        "--spec",
        dest="hyperplan_spec",
        required=True,
        help="path to HyperplanSpecV1 JSON",
    )
    p_hp_mat.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="existing run_id under .omg/state/runs/",
    )
    p_hp_mat.set_defaults(
        func=cmd_team, team_action="hyperplan", hyperplan_action="materialize"
    )

    p_hp_dec = hp_sub.add_parser(
        "validate-decision",
        parents=[common],
        help=(
            "validate + persist HyperplanDecisionV1 against materialized manifest "
            "(never silent-approves)"
        ),
    )
    p_hp_dec.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with materialized hyperplan-v1.json",
    )
    p_hp_dec.add_argument(
        "--input",
        dest="hyperplan_decision",
        required=True,
        help="path to HyperplanDecisionV1 JSON",
    )
    p_hp_dec.set_defaults(
        func=cmd_team,
        team_action="hyperplan",
        hyperplan_action="validate-decision",
    )

    p_hp_produce = hp_sub.add_parser(
        "produce-decision",
        parents=[common],
        help=(
            "derive + persist HyperplanDecisionV1 from HyperplanResultBundleV1 "
            "(hermetic; decision is commit marker)"
        ),
    )
    p_hp_produce.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with materialized hyperplan-v1.json",
    )
    p_hp_produce.add_argument(
        "--input",
        dest="hyperplan_bundle",
        required=True,
        help="path to HyperplanResultBundleV1 JSON",
    )
    p_hp_produce.set_defaults(
        func=cmd_team,
        team_action="hyperplan",
        hyperplan_action="produce-decision",
    )

    p_hp_admit = hp_sub.add_parser(
        "admit-tasks",
        parents=[common],
        help=(
            "admit materialized Hyperplan lanes as a committed Team task batch "
            "(execution_supported=false; no auto workers)"
        ),
    )
    p_hp_admit.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with materialized hyperplan-v1.json",
    )
    p_hp_admit.add_argument(
        "--team-id",
        dest="team_id",
        required=True,
        help="Team API team_id for task-batch admission",
    )
    p_hp_admit.set_defaults(
        func=cmd_team,
        team_action="hyperplan",
        hyperplan_action="admit-tasks",
    )

    p_hp_collect = hp_sub.add_parser(
        "collect-tasks",
        parents=[common],
        help=(
            "collect completed Hyperplan lane tasks into produce-decision "
            "(fail-closed; no auto workers)"
        ),
    )
    p_hp_collect.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with admitted Hyperplan task batch",
    )
    p_hp_collect.add_argument(
        "--team-id",
        dest="team_id",
        required=True,
        help="Team API team_id for task-batch collection",
    )
    p_hp_collect.set_defaults(
        func=cmd_team,
        team_action="hyperplan",
        hyperplan_action="collect-tasks",
    )

    p_hp_claim = hp_sub.add_parser(
        "claim-lane",
        parents=[common],
        help=(
            "worker-only: claim one Hyperplan lane via claim-task "
            "(CompositionLaneClaimV1; execution_supported=false)"
        ),
    )
    p_hp_claim.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with admitted Hyperplan task batch",
    )
    p_hp_claim.add_argument(
        "--team-id",
        dest="team_id",
        required=True,
        help="Team API team_id (must match worker env)",
    )
    p_hp_claim.add_argument(
        "--lane-id",
        dest="lane_id",
        required=True,
        help="composition lane_id (e.g. critic.security)",
    )
    p_hp_claim.set_defaults(
        func=cmd_team,
        team_action="hyperplan",
        hyperplan_action="claim-lane",
    )

    p_hp_submit = hp_sub.add_parser(
        "submit-lane-result",
        parents=[common],
        help=(
            "worker-only: submit LaneTaskResultV1 via transition-task-status "
            "(consumes claim-file; no --claim-token argv)"
        ),
    )
    p_hp_submit.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with admitted Hyperplan task batch",
    )
    p_hp_submit.add_argument(
        "--team-id",
        dest="team_id",
        required=True,
        help="Team API team_id (must match worker env)",
    )
    p_hp_submit.add_argument(
        "--claim-file",
        dest="claim_file",
        required=True,
        help="path to CompositionLaneClaimV1 JSON from claim-lane",
    )
    p_hp_submit.add_argument(
        "--result",
        dest="result_file",
        required=True,
        help="path to LaneTaskResultV1 JSON",
    )
    p_hp_submit.set_defaults(
        func=cmd_team,
        team_action="hyperplan",
        hyperplan_action="submit-lane-result",
    )

    p_t_sr = team_sub.add_parser(
        "security-research",
        parents=[common],
        help=(
            "Security Research Composition Contract V1 (hermetic produce + task "
            "driver + lane worker protocol; non-executing): plan|materialize|"
            "validate-report|produce-report|admit-tasks|collect-tasks|"
            "claim-lane|submit-lane-result (#69 PR13)"
        ),
    )
    sr_sub = p_t_sr.add_subparsers(dest="security_research_action")
    p_sr_plan = sr_sub.add_parser(
        "plan",
        parents=[common],
        help=(
            "compile SecurityResearchSpecV1 → ManifestV1 "
            "(zero filesystem mutation)"
        ),
    )
    p_sr_plan.add_argument(
        "--spec",
        dest="security_research_spec",
        required=True,
        help="path to SecurityResearchSpecV1 JSON",
    )
    p_sr_plan.set_defaults(
        func=cmd_team,
        team_action="security-research",
        security_research_action="plan",
    )

    p_sr_mat = sr_sub.add_parser(
        "materialize",
        parents=[common],
        help=(
            "atomically persist manifest under "
            ".omg/state/runs/<run>/team/compositions/security-research-v1.json"
        ),
    )
    p_sr_mat.add_argument(
        "--spec",
        dest="security_research_spec",
        required=True,
        help="path to SecurityResearchSpecV1 JSON",
    )
    p_sr_mat.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="existing run_id under .omg/state/runs/",
    )
    p_sr_mat.set_defaults(
        func=cmd_team,
        team_action="security-research",
        security_research_action="materialize",
    )

    p_sr_rep = sr_sub.add_parser(
        "validate-report",
        parents=[common],
        help=(
            "validate + persist SecurityResearchReportV1 against materialized "
            "manifest (never writes passes/verified)"
        ),
    )
    p_sr_rep.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with materialized security-research-v1.json",
    )
    p_sr_rep.add_argument(
        "--input",
        dest="security_research_report",
        required=True,
        help="path to SecurityResearchReportV1 JSON",
    )
    p_sr_rep.set_defaults(
        func=cmd_team,
        team_action="security-research",
        security_research_action="validate-report",
    )

    p_sr_prod = sr_sub.add_parser(
        "produce-report",
        parents=[common],
        help=(
            "hermetic SecurityResearchResultBundleV1 → report "
            "(writes result-bundle then report commit marker; "
            "execution_supported=false; never writes passes/verified)"
        ),
    )
    p_sr_prod.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with materialized security-research-v1.json",
    )
    p_sr_prod.add_argument(
        "--input",
        dest="security_research_bundle",
        required=True,
        help="path to SecurityResearchResultBundleV1 JSON",
    )
    p_sr_prod.set_defaults(
        func=cmd_team,
        team_action="security-research",
        security_research_action="produce-report",
    )

    p_sr_admit = sr_sub.add_parser(
        "admit-tasks",
        parents=[common],
        help=(
            "admit materialized Security Research lanes as a committed Team "
            "task batch (execution_supported=false; no auto workers/PoC)"
        ),
    )
    p_sr_admit.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with materialized security-research-v1.json",
    )
    p_sr_admit.add_argument(
        "--team-id",
        dest="team_id",
        required=True,
        help="Team API team_id for task-batch admission",
    )
    p_sr_admit.set_defaults(
        func=cmd_team,
        team_action="security-research",
        security_research_action="admit-tasks",
    )

    p_sr_collect = sr_sub.add_parser(
        "collect-tasks",
        parents=[common],
        help=(
            "collect completed Security Research lane tasks into produce-report "
            "(fail-closed; no auto workers/PoC)"
        ),
    )
    p_sr_collect.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with admitted Security Research task batch",
    )
    p_sr_collect.add_argument(
        "--team-id",
        dest="team_id",
        required=True,
        help="Team API team_id for task-batch collection",
    )
    p_sr_collect.set_defaults(
        func=cmd_team,
        team_action="security-research",
        security_research_action="collect-tasks",
    )

    p_sr_claim = sr_sub.add_parser(
        "claim-lane",
        parents=[common],
        help=(
            "worker-only: claim one Security Research lane via claim-task "
            "(CompositionLaneClaimV1; execution_supported=false)"
        ),
    )
    p_sr_claim.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with admitted Security Research task batch",
    )
    p_sr_claim.add_argument(
        "--team-id",
        dest="team_id",
        required=True,
        help="Team API team_id (must match worker env)",
    )
    p_sr_claim.add_argument(
        "--lane-id",
        dest="lane_id",
        required=True,
        help="composition lane_id (e.g. hunt.auth)",
    )
    p_sr_claim.set_defaults(
        func=cmd_team,
        team_action="security-research",
        security_research_action="claim-lane",
    )

    p_sr_submit = sr_sub.add_parser(
        "submit-lane-result",
        parents=[common],
        help=(
            "worker-only: submit LaneTaskResultV1 via transition-task-status "
            "(consumes claim-file; no --claim-token argv)"
        ),
    )
    p_sr_submit.add_argument(
        "--run",
        dest="run_id",
        required=True,
        help="run_id with admitted Security Research task batch",
    )
    p_sr_submit.add_argument(
        "--team-id",
        dest="team_id",
        required=True,
        help="Team API team_id (must match worker env)",
    )
    p_sr_submit.add_argument(
        "--claim-file",
        dest="claim_file",
        required=True,
        help="path to CompositionLaneClaimV1 JSON from claim-lane",
    )
    p_sr_submit.add_argument(
        "--result",
        dest="result_file",
        required=True,
        help="path to LaneTaskResultV1 JSON",
    )
    p_sr_submit.set_defaults(
        func=cmd_team,
        team_action="security-research",
        security_research_action="submit-lane-result",
    )

    p_team.set_defaults(func=cmd_team)


__all__ = [
    "register_team_parsers",
    "cmd_accept",
    "cmd_integrate",
    "cmd_team",
    "cmd_worker",
]
