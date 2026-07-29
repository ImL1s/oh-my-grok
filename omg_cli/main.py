# omg_cli/main.py
"""omg CLI argparse router."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omg_cli.command_registry import KNOWN_SUBCOMMANDS
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
from omg_cli.commands.install import (  # #29 Phase 2+4' — install family
    cmd_doctor,  # noqa: F401
    cmd_install_hook,  # noqa: F401
    cmd_setup,  # noqa: F401
    cmd_uninstall,  # noqa: F401
    cmd_update,  # noqa: F401
    register_install_parsers,
)
from omg_cli.commands.mcp import (  # #29 Phase 2 — mcp family
    cmd_mcp_install,
    cmd_mcp_server,
)
from omg_cli.commands.memory import (  # #29 Phase 2+4' — memory family
    cmd_compact,  # noqa: F401 — re-export for tests
    cmd_memory,  # noqa: F401
    cmd_note,  # still wired for early ``note`` parser in main
    cmd_tracker,  # noqa: F401
    register_memory_parsers,
)
from omg_cli.commands.modes import (  # #29 Phase 2 — modes family
    cmd_ask,
    cmd_autopilot,
    cmd_dual_review,
    cmd_mode,
    cmd_pipeline,
    cmd_qa,
    cmd_review,
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
from omg_cli.commands.team import (  # #29 Phase 2 — team family
    cmd_accept,
    cmd_integrate,
    cmd_team,
    cmd_worker,
)
from omg_cli.commands.workflow import (  # #29 Phase 2 — workflow family
    cmd_goal,
    cmd_interview,
    cmd_workflow,
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

    register_install_parsers(sub, common, phase="late")


    register_run_parsers(sub, common)


    register_memory_parsers(sub, common)


    register_inspect_parsers(sub, common, phase="early")


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

    register_inspect_parsers(sub, common, phase="late")


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

    p_g_set_host = goal_sub.add_parser(
        "set-host",
        parents=[common],
        help="print host /goal handoff text (does not mutate host goal)",
    )
    p_g_set_host.add_argument("--goal", dest="goal_id", required=True)
    # --json from common (json_output); also accepted as omg --json goal set-host
    p_g_set_host.set_defaults(func=cmd_goal, goal_action="set-host")

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
            'experimental tmux team: omg team [N[:role]] "<goal>" '
            "(requires OMG_EXPERIMENTAL_TMUX_TEAM=1); also start|run|api|…"
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
    p_t_stop.set_defaults(func=cmd_team, team_action="stop")

    p_t_api = team_sub.add_parser(
        "api",
        parents=[common],
        help=(
            "OMX-shaped team api façade (P0 mailbox/task ops); "
            "requires OMG_EXPERIMENTAL_TMUX_TEAM=1"
        ),
    )
    p_t_api.add_argument(
        "api_op",
        metavar="OP",
        help=(
            "operation name (P0: send-message, mailbox-list, "
            "mailbox-mark-delivered, create-task, list-tasks, claim-task, "
            "transition-task-status, release-task-claim, get-summary, "
            "read-config, write-worker-inbox)"
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
    p_ap_await = ap_sub.add_parser(
        "await",
        parents=[common],
        help="set/clear autopilot awaiting-confirmation pause",
    )
    p_ap_await.add_argument("--run", dest="run_id", required=True)
    p_ap_await.add_argument("--reason", default=None, help="pause reason label")
    p_ap_await.add_argument(
        "--clear",
        action="store_true",
        help="clear awaiting flag (default sets awaiting=true)",
    )
    p_ap_await.set_defaults(func=cmd_autopilot, autopilot_action="await")
    p_ap_run = ap_sub.add_parser(
        "run",
        parents=[common],
        help="outer CLI driver (cross-turn/headless persistence)",
    )
    p_ap_run.add_argument("goal", nargs="*", help="goal text (omit when resuming)")
    p_ap_run.add_argument("--force", action="store_true")
    p_ap_run.add_argument(
        "--skip-interview",
        action="store_true",
        help="start at ralplan when creating a new run",
    )
    p_ap_run.add_argument(
        "--resume",
        dest="resume",
        nargs="?",
        const="__active__",
        default=None,
        metavar="RUN",
        help="resume active or explicit autopilot run",
    )
    p_ap_run.add_argument(
        "--max-phase-cycles",
        dest="max_phase_cycles",
        type=int,
        default=5,
        help="max grok launches per phase before blocked (default 5)",
    )
    p_ap_run.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="create run + argv only; do not exec grok",
    )
    p_ap_run.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="seconds per grok launch (default 3600); 0 = unlimited",
    )
    p_ap_run.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "hands-off outer loop (#40): re-launch on host-turn stalls without "
            "printing a human go prompt (still pauses on interview/await)"
        ),
    )
    p_ap_run.add_argument(
        "--max-stall-relaunches",
        dest="max_stall_relaunches",
        type=int,
        default=32,
        help="unattended: max re-launches after no phase advance (default 32)",
    )
    p_ap_run.set_defaults(func=cmd_autopilot, autopilot_action="run")
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
        if mode == "ralplan":
            p.add_argument(
                "--run",
                dest="run_id",
                default=None,
                metavar="RUN",
                help=(
                    "reuse an existing run_id (pipeline/autopilot embedding); "
                    "skips create_run so active pointer stays on that run"
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
    # Global --json optional; ask still defaults write_json=True in handler
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

    parser = build_parser()
    args = parser.parse_args(raw)
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
        }
    )
    command = str(getattr(args, "command", "") or "")
    clear_resolved_project_root()
    root_path: Path | None = None
    if command not in _INSTALL_SCOPED:
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
        if resolution.note and resolution.shadowed_omg_ancestors:
            print(f"omg: warning: {resolution.note}", file=sys.stderr)

    from omg_cli.command_context import attach_command_context

    attach_command_context(args, root=root_path)
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
