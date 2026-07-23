"""Fail-closed journal replay and verified-effect receipt adoption."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omg_cli.contracts.workflow_contract import task_requires_terminable_executor
from omg_cli.contracts.writer_chain import canonical_json_bytes, parse_canonical_json_bytes, sha256_hex


class WorkflowReplayError(ValueError):
    pass


def verified_effect_receipt(
    *,
    task: Mapping[str, Any],
    plan: Mapping[str, Any],
    effect_id: str,
) -> dict[str, Any]:
    body = {
        "store_kind": "workflow_effect_receipt",
        "schema_version": 1,
        "task_id": task["task_id"],
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
        "effect_type": task["effect_type"],
        "effect_id": effect_id,
        "status": "applied",
        "verified": True,
    }
    return {**body, "receipt_hash": sha256_hex(canonical_json_bytes(body))}


def validate_effect_receipt(
    receipt: Mapping[str, Any], *, task: Mapping[str, Any], plan: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise WorkflowReplayError("effect receipt must be an object")
    row = dict(receipt)
    required = {
        "store_kind",
        "schema_version",
        "task_id",
        "plan_digest",
        "definition_digest",
        "run_generation",
        "effect_type",
        "effect_id",
        "status",
        "verified",
        "receipt_hash",
    }
    if set(row) != required:
        raise WorkflowReplayError("effect receipt keys mismatch")
    body = dict(row)
    receipt_hash = body.pop("receipt_hash")
    expected = {
        "store_kind": "workflow_effect_receipt",
        "schema_version": 1,
        "task_id": task["task_id"],
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
        "effect_type": task["effect_type"],
        "status": "applied",
        "verified": True,
    }
    for field, value in expected.items():
        if body.get(field) != value:
            raise WorkflowReplayError(f"effect receipt {field} binding mismatch")
    if not isinstance(body.get("effect_id"), str) or not body["effect_id"]:
        raise WorkflowReplayError("effect receipt effect_id required")
    if receipt_hash != sha256_hex(canonical_json_bytes(body)):
        raise WorkflowReplayError("effect receipt hash mismatch")
    return row


def load_journal(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in source.read_bytes().splitlines():
        parsed = parse_canonical_json_bytes(line)
        if not isinstance(parsed, dict):
            raise WorkflowReplayError("workflow journal row must be object")
        rows.append(parsed)
    return rows


def assess_replay(
    plan: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    adoptable: list[str] = []
    ambiguous: list[str] = []
    effect_unknown: list[str] = []
    seen: set[str] = set()
    for raw in results:
        row = dict(raw)
        task_id = str(row.get("task_id") or "")
        task = tasks.get(task_id)
        if task is None or task_id in seen:
            ambiguous.append(task_id or "<missing>")
            continue
        seen.add(task_id)
        if (
            row.get("plan_digest") != plan["plan_digest"]
            or row.get("definition_digest") != plan["definition_digest"]
            or row.get("run_generation") != plan["run_generation"]
        ):
            ambiguous.append(task_id)
            continue
        if task_requires_terminable_executor(task):
            if row.get("status") == "effect_unknown":
                effect_unknown.append(task_id)
                continue
            if task["effect_type"] is not None:
                if row.get("status") not in {"passed", "approved"}:
                    effect_unknown.append(task_id)
                    continue
                receipt = row.get("effect_receipt")
                if receipt is None:
                    effect_unknown.append(task_id)
                    continue
                try:
                    validate_effect_receipt(receipt, task=task, plan=plan)
                except WorkflowReplayError:
                    effect_unknown.append(task_id)
                    continue
        if row.get("status") in {"passed", "approved"}:
            # Structural replay remains inspectable, but neither caller files
            # nor plain hashes are product authority. Until Grok exposes a
            # host-authenticated receipt API, successful work is unadoptable.
            if task["effect_type"] is not None:
                effect_unknown.append(task_id)
            else:
                ambiguous.append(task_id)
    terminal = "effect_unknown" if effect_unknown else "blocked" if ambiguous else "replayable"
    return {
        "terminal": terminal,
        "adoptable_task_ids": adoptable,
        "ambiguous_task_ids": ambiguous,
        "effect_unknown_task_ids": effect_unknown,
    }


__all__ = [
    "WorkflowReplayError",
    "assess_replay",
    "load_journal",
    "validate_effect_receipt",
    "verified_effect_receipt",
]
