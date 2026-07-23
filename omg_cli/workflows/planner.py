"""Deterministic task expansion and bounded parallel-wave planning."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omg_cli.contracts.workflow_contract import deterministic_topological_order, workflow_task_id
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

from .schema import compile_workflow, input_digest, validate_workflow_input


PLAN_CONTRACT = "repository-workflow-plan/v1"


def build_plan(
    definition: Mapping[str, Any],
    workflow_input: Mapping[str, Any],
    *,
    repository_id: str = "OMG",
    run_generation: int = 0,
) -> dict[str, Any]:
    compiled = compile_workflow(definition)
    canonical_input = validate_workflow_input(compiled, workflow_input)
    digest = input_digest(canonical_input)
    order = deterministic_topological_order(compiled["stages"])
    by_stage = {stage["id"]: stage for stage in compiled["stages"]}
    tasks: list[dict[str, Any]] = []
    task_ids_by_stage: dict[str, list[str]] = {}
    for stage_id in order:
        stage = by_stage[stage_id]
        stage_ids: list[str] = []
        dependency_ids = [
            task_id
            for dependency in stage["depends_on"]
            for task_id in task_ids_by_stage[dependency]
        ]
        for matrix_index, matrix in enumerate(stage["matrix"]):
            task_id = workflow_task_id(
                repository_id=repository_id,
                workflow_name=compiled["name"],
                workflow_version=compiled["workflow_version"],
                definition_digest=compiled["definition_digest"],
                input_digest=digest,
                stage_id=stage_id,
                matrix_index=matrix_index,
                run_generation=run_generation,
            )
            stage_ids.append(task_id)
            tasks.append(
                {
                    "task_id": task_id,
                    "stage_id": stage_id,
                    "matrix_index": matrix_index,
                    "matrix": matrix,
                    "dependencies": dependency_ids,
                    "kind": stage["kind"],
                    "role": stage["role"],
                    "actor_identity": stage["actor_identity"],
                    "capability_mode": stage["capability_mode"],
                    "permissions": stage["permissions"],
                    "mcp_allowlist": stage["mcp_allowlist"],
                    "write_paths": stage["write_paths"],
                    "timeout_seconds": stage["timeout_seconds"],
                    "retry_budget": stage["retry_budget"],
                    "effect_type": stage["effect_type"],
                }
            )
        task_ids_by_stage[stage_id] = stage_ids
    level_by_stage: dict[str, int] = {}
    for stage_id in order:
        deps = by_stage[stage_id]["depends_on"]
        level_by_stage[stage_id] = 0 if not deps else max(level_by_stage[item] for item in deps) + 1
    waves: list[list[str]] = []
    for level in range(max(level_by_stage.values(), default=-1) + 1):
        wave = [task["task_id"] for task in tasks if level_by_stage[task["stage_id"]] == level]
        if wave:
            waves.append(wave)
    body = {
        "contract": PLAN_CONTRACT,
        "repository_id": repository_id,
        "workflow_name": compiled["name"],
        "workflow_version": compiled["workflow_version"],
        "definition_digest": compiled["definition_digest"],
        "input": canonical_input,
        "input_digest": digest,
        "run_generation": run_generation,
        "max_parallelism": compiled["max_parallelism"],
        "stage_order": order,
        "tasks": tasks,
        "waves": waves,
    }
    plan_digest = sha256_hex(canonical_json_bytes(body))
    return {**body, "plan_digest": plan_digest, "run_id": f"workflow-{plan_digest[:32]}"}


__all__ = ["PLAN_CONTRACT", "build_plan"]
