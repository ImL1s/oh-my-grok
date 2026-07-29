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


__all__ = [
    "_workflow_receipts",
    "cmd_workflow",
    "workflow_receipts",
]
