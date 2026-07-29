"""Workflow-family CLI handlers (#29 Phase 2).

Commands: workflow (install/list/show/plan/run).
Parser construction remains in ``main.build_parser``.
"""

from __future__ import annotations

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
    print(json.dumps(result, indent=2, ensure_ascii=False))
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
            if getattr(args, "json", False):
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(result["handoff_markdown"])
            return 0
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



__all__ = [
    "_workflow_receipts",
    "cmd_goal",
    "cmd_interview",
    "cmd_workflow",
    "workflow_receipts",
]
