"""Workflow-family CLI handlers (#29 Phase 2).

Commands: workflow (install/list/show/plan/run).
Parser construction: ``register_workflow_parsers`` (#29 Phase 4').
"""

from __future__ import annotations

from omg_cli.cli_envelope import emit_data

import argparse
import json
import sys
from pathlib import Path

from omg_cli.cli_util import project_root, read_json_path


def workflow_receipts(value: object) -> dict[str, dict]:
    """Normalize receipt array or map into task_id → receipt dict."""
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


# Compat alias if anything imported private name from main
_workflow_receipts = workflow_receipts


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

    root = project_root()
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
            workflow_input = read_json_path(args.input, label="workflow input")
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
                receipt_value = read_json_path(
                    args.receipts, label="workflow receipts"
                )
                receipts = workflow_receipts(receipt_value)
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
                        raise ValueError(
                            f"missing workflow receipt: {task['task_id']}"
                        )
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
    emit_data(args, "workflow", result)
    if action == "run":
        return 0 if isinstance(result, dict) and result.get("terminal") == "ship" else 1
    return 0

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

    root = project_root()
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
        emit_data(args, "interview", exc.result)
        return 1
    except (InterviewError, RuntimeError) as exc:
        print(f"omg interview: {exc}", file=sys.stderr)
        return 1
    emit_data(args, "interview", result)
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
        set_host_goal_handoff,
        start_story,
        goal_status,
        verify_goal,
    )

    root = project_root()
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
        elif action == "set-host":
            result = set_host_goal_handoff(root, args.goal_id)
            if bool(getattr(args, "json", False) or getattr(args, "json_output", False)):
                emit_data(args, "goal.set-host", result)
            else:
                print(result["handoff_markdown"])
            return 0
        else:
            print("omg goal: action required", file=sys.stderr)
            return 2
    except GoalRepairRefused as exc:
        emit_data(args, "goal", {"ok": False, "error": str(exc)})
        return 1
    except (GoalError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"omg goal: {exc}", file=sys.stderr)
        return 1
    emit_data(args, "goal", result)
    return 0




def register_workflow_parsers(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
    *,
    phase: str = "all",
) -> None:
    """Register workflow-family argparse parsers (#29 Phase 4').

    ``phase``:
      - ``early``: workflow (before inspect late in help order)
      - ``late``: interview, goal
      - ``all``: both
    """
    if phase not in {"early", "late", "all"}:
        raise ValueError(f"unknown workflow register phase: {phase!r}")
    if phase in ("early", "all"):
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

    if phase in ("late", "all"):
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


__all__ = [
    "register_workflow_parsers",
    "_workflow_receipts",
    "cmd_goal",
    "cmd_interview",
    "cmd_workflow",
    "workflow_receipts",
]
