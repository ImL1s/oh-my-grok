# omg_cli/main.py
"""omg CLI argparse router."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omg_cli.command_registry import KNOWN_SUBCOMMANDS
from omg_cli.commands.agents import (  # #131 — dual-host agent/model policy
    cmd_agents,  # noqa: F401 — re-export for tests
    register_agents_parsers,
)
from omg_cli.commands.inspect import (  # #29 Phase 2+4' — inspect family
    cmd_capabilities,  # noqa: F401 — re-export for tests
    cmd_hud,  # noqa: F401
    cmd_lsp,  # noqa: F401
    cmd_native_status,  # noqa: F401
    cmd_notify,  # noqa: F401
    cmd_parity,  # noqa: F401
    cmd_wiki,  # noqa: F401
    register_inspect_parsers,
)
from omg_cli.commands.provider import (  # #67-A — provider probe
    cmd_provider,  # noqa: F401 — re-export for tests
    register_provider_parsers,
)
from omg_cli.commands.edit import (  # #76 — hash-anchored edit CLI
    cmd_edit,  # noqa: F401 — re-export for tests
    register_edit_parsers,
)
from omg_cli.commands.visual import (  # #75 — visual contract CLI
    cmd_visual,  # noqa: F401 — re-export for tests
    register_visual_parsers,
)
from omg_cli.commands.job import (  # #68 PR1 — durable background jobs
    cmd_job,  # noqa: F401 — re-export for tests
    register_job_parsers,
)
from omg_cli.commands.install import (  # #29 Phase 2+4' — install family
    cmd_doctor,  # noqa: F401
    cmd_install_hook,  # noqa: F401
    cmd_setup,  # noqa: F401
    cmd_uninstall,  # noqa: F401
    cmd_update,  # noqa: F401
    register_install_parsers,
)
from omg_cli.commands.mcp import (  # #29 Phase 2+4' — mcp family
    cmd_mcp_install,  # noqa: F401
    cmd_mcp_server,  # noqa: F401
    register_mcp_parsers,
)
from omg_cli.commands.tools import (  # #73 — tools sidecar
    cmd_tools,  # noqa: F401 — re-export for tests
    register_tools_parsers,
)
from omg_cli.commands.memory import (  # #29 Phase 2+4' — memory family
    cmd_compact,  # noqa: F401 — re-export for tests
    cmd_memory,  # noqa: F401
    cmd_note,  # noqa: F401
    cmd_tracker,  # noqa: F401
    register_memory_parsers,
    register_note_parser,
)
from omg_cli.commands.modes import (  # #29 Phase 2+4' — modes family
    cmd_ask,  # noqa: F401
    cmd_autopilot,  # noqa: F401
    cmd_dual_review,  # noqa: F401
    cmd_mode,  # noqa: F401
    cmd_pipeline,  # noqa: F401
    cmd_qa,  # noqa: F401
    cmd_review,  # noqa: F401
    register_modes_parsers,
)
from omg_cli.commands.run import (  # #29 Phase 2+4' — run family
    _print_state_human,  # noqa: F401 — re-export for tests
    cmd_cancel,  # noqa: F401
    cmd_recover,  # noqa: F401
    cmd_resume,  # noqa: F401
    cmd_session,  # noqa: F401
    cmd_state,  # noqa: F401
    register_run_parsers,
)
from omg_cli.commands.team import (  # #29 Phase 2+4' — team family
    cmd_accept,  # noqa: F401
    cmd_integrate,  # noqa: F401
    cmd_team,  # noqa: F401
    cmd_worker,  # noqa: F401
    register_team_parsers,
)
from omg_cli.commands.workflow import (  # #29 Phase 2+4' — workflow family
    cmd_goal,  # noqa: F401
    cmd_interview,  # noqa: F401
    cmd_workflow,  # noqa: F401
    register_workflow_parsers,
)


def _project_root() -> Path:
    """Canonical project root (#22). Prefer process resolution after argv parse."""
    from omg_cli.project_root import project_root

    return project_root()


def apply_safe_yolo_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> argparse.Namespace:
    """Canonicalize ``--safe`` / ``--yolo`` after multi-layer parent parse.

    Inherited common flags use ``default=argparse.SUPPRESS`` so unset layers do
    not clobber a True set on an outer parser. Position before or after the
    subcommand is therefore equivalent. Both flags together is a usage error
    (exit 2 via ``parser.error``).
    """
    safe = bool(getattr(args, "safe", False))
    yolo = bool(getattr(args, "yolo", False))
    if safe and yolo:
        parser.error(
            "--safe and --yolo are mutually exclusive; pass only one "
            "(either before or after the subcommand)"
        )
    args.safe = safe
    args.yolo = yolo
    return args


def apply_output_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> argparse.Namespace:
    """Canonicalize global ``--json`` (#29/#30).

    Dest is ``json_output`` so command-local ``--json`` (e.g. hud) stays a
    separate attribute but still counts in ``resolve_output_mode``.
    Default output remains human-friendly where the command already is.
    """
    del parser  # reserved for future mutual-exclusion with global --human
    args.json_output = bool(getattr(args, "json_output", False))
    return args


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS: root + every nested subparser share this parent. Plain
    # store_true default=False lets a deeper layer overwrite an outer True
    # with False when the flag was only given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--safe",
        action="store_true",
        default=argparse.SUPPRESS,
        help="prefer safe defaults (modes use later); mutually exclusive with --yolo",
    )
    common.add_argument(
        "--yolo",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "allow elevated permissions for mode launchers (off by default); "
            "mutually exclusive with --safe"
        ),
    )
    common.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "machine JSON envelope on stdout for supported commands (#30); "
            "default remains each command's human-friendly mode"
        ),
    )
    common.add_argument(
        "--project-root",
        dest="project_root",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help=(
            "explicit project root for .omg state (overrides OMG_PROJECT_ROOT "
            "and discovery; see docs/project-root.md)"
        ),
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

    register_install_parsers(sub, common, phase="early")

    register_note_parser(sub, common)

    register_install_parsers(sub, common, phase="late")


    register_run_parsers(sub, common)


    register_memory_parsers(sub, common)


    register_inspect_parsers(sub, common, phase="early")

    register_agents_parsers(sub, common)

    register_workflow_parsers(sub, common, phase="early")

    register_inspect_parsers(sub, common, phase="late")

    register_provider_parsers(sub, common)

    register_edit_parsers(sub, common)
    register_visual_parsers(sub, common)

    register_job_parsers(sub, common)

    register_workflow_parsers(sub, common, phase="late")


    register_team_parsers(sub, common)


    register_modes_parsers(sub, common)


    register_mcp_parsers(sub, common)
    register_tools_parsers(sub, common)


    return parser


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

    from omg_cli.project_root import (
        ProjectRootError,
        clear_resolved_project_root,
        resolve_project_root,
        set_resolved_project_root,
    )

    def _host_launch_root() -> Path | int:
        """Resolve cwd-based root for host launch; map ProjectRootError → exit 2."""
        clear_resolved_project_root()
        try:
            resolution = resolve_project_root()
        except ProjectRootError as exc:
            print(f"omg: {exc}", file=sys.stderr)
            return int(getattr(exc, "exit_code", 2) or 2)
        set_resolved_project_root(resolution)
        return resolution.root

    if has_madmax_flag(raw):
        # Delimiter-aware; GRAM-05 only cares about a recognized *first* token.
        host_root = _host_launch_root()
        if isinstance(host_root, int):
            return host_root
        return int(run_madmax_host(host_root, raw))

    if should_host_launch(raw, KNOWN_SUBCOMMANDS):
        host_root = _host_launch_root()
        if isinstance(host_root, int):
            return host_root
        return int(run_interactive(host_root, raw))

    from omg_cli.team.cli import TeamCliError, normalize_team_argv

    try:
        raw = normalize_team_argv(raw)
    except TeamCliError as exc:
        print(f"omg team: {exc}", file=sys.stderr)
        return int(exc.exit_code)

    from omg_cli.ask.catalog_usage import (
        CATALOG_USAGE_CODE,
        argv_wants_json,
        catalog_forbidden_supplied,
        catalog_usage_message,
        catalog_verb_from_argv,
        normalize_ask_argv,
    )
    from omg_cli.cli_envelope import emit_json, failure

    # CPython <3.12: hoist options between ask positionals (see normalize_ask_argv).
    if "ask" in raw:
        raw = normalize_ask_argv(raw)

    catalog_verb = catalog_verb_from_argv(raw)
    if catalog_verb is not None:
        catalog_forbidden = catalog_forbidden_supplied(raw)
        if catalog_forbidden:
            catalog_command = f"ask.{catalog_verb}"
            catalog_message = catalog_usage_message(catalog_forbidden)
            if argv_wants_json(raw):
                emit_json(
                    failure(catalog_command, CATALOG_USAGE_CODE, catalog_message)
                )
            else:
                print(
                    f"omg ask {catalog_verb}: {catalog_message}",
                    file=sys.stderr,
                )
            return 2

    parser = build_parser()
    args = parser.parse_args(raw)
    args.raw_argv = list(raw)
    # Immediately after parse (normalize already ran). Skip when command is
    # missing/empty so the existing no-command help path is unchanged.
    if getattr(args, "command", None) == "team":
        from omg_cli.team.plane import (
            TeamGateError,
            preflight_team_worker_parsed_argv,
        )

        try:
            team_action = getattr(args, "team_action", None)
            preflight_team_worker_parsed_argv(
                team_action if team_action is None else str(team_action),
                command="team",
                composition_action=(
                    getattr(args, "hyperplan_action", None)
                    or getattr(args, "security_research_action", None)
                ),
            )
        except TeamGateError as exc:
            print(f"omg team: {exc}", file=sys.stderr)
            return 2
    apply_safe_yolo_flags(parser, args)
    apply_output_flags(parser, args)
    # Bridge: legacy dest names used by team/ask handlers
    if bool(getattr(args, "json_output", False)):
        if not hasattr(args, "as_json"):
            args.as_json = True
        if not hasattr(args, "json"):
            args.json = True
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1

    # Install / global surfaces do not consume project root — skip discovery so a
    # stale OMG_PROJECT_ROOT cannot block hook install/update/uninstall (#22 P2).
    _INSTALL_SCOPED = frozenset(
        {
            "install-hook",
            "update",
            "uninstall",
            "mcp-install",
            "version",  # not a command today; harmless
            "provider",  # global binary/version probe (#67-A); no project root
            "visual",  # pure compare() wrapper (#75); no project root / state
        }
    )
    command = str(getattr(args, "command", "") or "")
    team_action = getattr(args, "team_action", None)
    api_op = str(getattr(args, "api_op", "") or "")
    # Pure introspection: versioned Team operation catalog (no project root).
    team_api_catalog = (
        command == "team" and team_action == "api" and api_op == "catalog"
    )
    clear_resolved_project_root()
    root_path: Path | None = None
    if command not in _INSTALL_SCOPED and not team_api_catalog:
        # #100: pane supervisor must use the validated leader root and must
        # NOT walk ancestors from the worktree (avoids nested-.omg warnings
        # and keeps bootstrap silent). Public CLI discovery is unchanged.
        if command == "team" and team_action == "supervisor":
            from omg_cli.team.bootstrap import (
                BootstrapError,
                pane_failure_line,
                resolve_supervisor_project_root,
            )

            try:
                resolution = resolve_supervisor_project_root()
            except BootstrapError as exc:
                worker_id = (os.environ.get("OMG_TEAM_WORKER_ID") or "").strip() or None
                run_id = (os.environ.get("OMG_TEAM_RUN_ID") or "").strip() or None
                print(
                    pane_failure_line(worker_id=worker_id, run_id=run_id),
                    file=sys.stderr,
                )
                return int(getattr(exc, "exit_code", 1) or 1)
            set_resolved_project_root(resolution)
            root_path = resolution.root
        else:
            try:
                resolution = resolve_project_root(
                    explicit=getattr(args, "project_root", None),
                    here=bool(getattr(args, "setup_here", False)),
                )
            except ProjectRootError as exc:
                print(f"omg: {exc}", file=sys.stderr)
                return int(getattr(exc, "exit_code", 2) or 2)
            set_resolved_project_root(resolution)
            root_path = resolution.root
            worker_team_api = False
            if command == "team" and team_action == "api":
                from omg_cli.team.api import team_api_worker_context_present

                worker_team_api = team_api_worker_context_present()
            if (
                resolution.note
                and resolution.shadowed_omg_ancestors
                and not worker_team_api
            ):
                print(f"omg: warning: {resolution.note}", file=sys.stderr)

    from omg_cli.command_context import attach_command_context

    attach_command_context(args, root=root_path)
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
