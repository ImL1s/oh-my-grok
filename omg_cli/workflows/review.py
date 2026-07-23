"""Independent verifier/skeptic workflow result gate."""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omg_cli.contracts.workflow_contract import decide_terminal
from omg_cli.contracts.writer_chain import (
    canonical_json_bytes,
    parse_canonical_json_bytes,
    sha256_hex,
)

from .schema import (
    WorkflowSchemaError,
    compile_workflow,
    validate_json_value,
    validate_stage_output,
)


RESULT_STATUSES = frozenset(
    {"passed", "approved", "failed", "blocked", "cancelled", "interrupted", "effect_unknown"}
)


class WorkflowReviewError(ValueError):
    pass


_STATUS_VERDICTS = {
    "passed": frozenset({"APPROVE"}),
    "approved": frozenset({"APPROVE"}),
    "failed": frozenset({"REJECT", "REQUEST_CHANGES", "NO_SHIP"}),
    "blocked": frozenset({"BLOCKED", "NO_SHIP"}),
    "cancelled": frozenset({"CANCELLED"}),
    "interrupted": frozenset({"INTERRUPTED"}),
    "effect_unknown": frozenset({"EFFECT_UNKNOWN"}),
}
_VERDICT_DEFAULT_STATUS = {
    "REJECT": "failed",
    "REQUEST_CHANGES": "failed",
    "NO_SHIP": "failed",
    "BLOCKED": "blocked",
    "CANCELLED": "cancelled",
    "INTERRUPTED": "interrupted",
    "EFFECT_UNKNOWN": "effect_unknown",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_MAX_RECEIPT_STREAM_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_SUCCESS_RECEIPT_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "repository_id",
        "run_id",
        "definition_digest",
        "plan_digest",
        "task_id",
        "stage_id",
        "matrix_index",
        "actor_identity",
        "run_generation",
        "status",
        "verdict",
        "output",
        "launch_provenance",
        "verification_receipts",
        "artifact_receipts",
        "receipt_hash",
    }
)
_LAUNCH_PROVENANCE_KEYS = frozenset(
    {
        "provider",
        "launch_id",
        "session_id",
        "agent_instance_id",
        "receipt_path",
        "receipt_hash",
    }
)
_LAUNCH_RECEIPT_KEYS = frozenset(
    {
        "store_kind",
        "schema_version",
        "provider",
        "repository_id",
        "run_id",
        "definition_digest",
        "plan_digest",
        "task_id",
        "stage_id",
        "matrix_index",
        "actor_identity",
        "run_generation",
        "launch_id",
        "session_id",
        "agent_instance_id",
        "receipt_hash",
    }
)
_VERIFICATION_RECEIPT_KEYS = frozenset(
    {
        "argv",
        "exit_code",
        "stdout_size",
        "stdout_sha256",
        "stderr_size",
        "stderr_sha256",
        "launch_receipt_hash",
    }
)
_ARTIFACT_RECEIPT_KEYS = frozenset(
    {
        "path",
        "size",
        "sha256",
        "schema",
        "schema_digest",
        "content",
        "launch_receipt_hash",
    }
)


def _require_exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], *, label: str
) -> None:
    if set(value) != expected:
        missing = sorted(str(item) for item in expected - set(value))
        extra = sorted(str(item) for item in set(value) - expected)
        raise WorkflowReviewError(
            f"{label} keys mismatch (missing={missing!r}, extra={extra!r})"
        )


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise WorkflowReviewError(f"{label} must be lowercase SHA256")
    return value


def _require_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise WorkflowReviewError(f"{label} must be a bounded identifier")
    return value


def _relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise WorkflowReviewError(f"{label} must be a repository-relative path")
    path = Path(value)
    if path.is_absolute() or "\\" in value or value != path.as_posix():
        raise WorkflowReviewError(f"{label} must be a normalized repository-relative path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise WorkflowReviewError(f"{label} must not contain dot segments")
    return value


def _read_confined_regular(
    root: Path,
    relative: str,
    *,
    label: str,
    max_bytes: int,
    immutable: bool = False,
) -> bytes:
    root = root.resolve(strict=True)
    relative = _relative_path(relative, label=label)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current_fd = os.open(root, directory_flags)
        descriptors.append(current_fd)
        parts = Path(relative).parts
        for part in parts[:-1]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            descriptors.append(current_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise WorkflowReviewError(f"{label} must be a single-link regular file")
        if immutable and before.st_mode & 0o222:
            raise WorkflowReviewError(f"{label} must be immutable")
        if before.st_size > max_bytes:
            raise WorkflowReviewError(f"{label} exceeds bounded size")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(file_fd)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if len(data) > max_bytes:
            raise WorkflowReviewError(f"{label} exceeds bounded size")
        if not stable or len(data) != before.st_size:
            raise WorkflowReviewError(f"{label} changed while being read")
        return data
    except WorkflowReviewError:
        raise
    except OSError as exc:
        raise WorkflowReviewError(
            f"{label} is missing, symlinked, or inaccessible: {relative}"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_launch_provenance(
    root: Path,
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    provenance: Any,
) -> dict[str, Any]:
    if not isinstance(provenance, Mapping):
        raise WorkflowReviewError("launch_provenance must be an object")
    row = dict(provenance)
    _require_exact_keys(row, _LAUNCH_PROVENANCE_KEYS, label="launch_provenance")
    if row["provider"] != "grok":
        raise WorkflowReviewError("launch provenance provider must be grok")
    for field in ("launch_id", "session_id", "agent_instance_id"):
        _require_id(row[field], label=f"launch_provenance.{field}")
    expected_path = (
        f".omg/artifacts/workflow-launches/{plan['run_id']}/"
        f"{task['task_id']}.json"
    )
    if row["receipt_path"] != expected_path:
        raise WorkflowReviewError("launch provenance receipt path mismatch")
    expected_hash = _require_sha256(
        row["receipt_hash"], label="launch_provenance.receipt_hash"
    )
    data = _read_confined_regular(
        root,
        expected_path,
        label="launch provenance receipt",
        max_bytes=64 * 1024,
        immutable=True,
    )
    try:
        persisted = parse_canonical_json_bytes(data)
    except Exception as exc:
        raise WorkflowReviewError("launch provenance receipt must be canonical JSON") from exc
    if not isinstance(persisted, dict):
        raise WorkflowReviewError("launch provenance receipt must be an object")
    _require_exact_keys(
        persisted, _LAUNCH_RECEIPT_KEYS, label="launch provenance receipt"
    )
    body = dict(persisted)
    receipt_hash = _require_sha256(
        body.pop("receipt_hash"), label="launch provenance receipt.receipt_hash"
    )
    if (
        not isinstance(body.get("schema_version"), int)
        or isinstance(body.get("schema_version"), bool)
        or not isinstance(body.get("matrix_index"), int)
        or isinstance(body.get("matrix_index"), bool)
        or not isinstance(body.get("run_generation"), int)
        or isinstance(body.get("run_generation"), bool)
    ):
        raise WorkflowReviewError("launch provenance receipt integer fields are invalid")
    expected = {
        "store_kind": "workflow_launch_receipt",
        "schema_version": 1,
        "provider": "grok",
        "repository_id": plan["repository_id"],
        "run_id": plan["run_id"],
        "definition_digest": plan["definition_digest"],
        "plan_digest": plan["plan_digest"],
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "actor_identity": task["actor_identity"],
        "run_generation": plan["run_generation"],
    }
    for field, value in expected.items():
        if body.get(field) != value:
            raise WorkflowReviewError(
                f"launch provenance receipt {field} binding mismatch"
            )
    for field in ("launch_id", "session_id", "agent_instance_id"):
        if body.get(field) != row[field]:
            raise WorkflowReviewError(f"launch provenance {field} mismatch")
    canonical_hash = sha256_hex(canonical_json_bytes(body))
    if receipt_hash != canonical_hash or expected_hash != canonical_hash:
        raise WorkflowReviewError("launch provenance receipt hash mismatch")
    return row


def _validate_verification_receipts(
    stage: Mapping[str, Any],
    value: Any,
    *,
    launch_receipt_hash: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WorkflowReviewError("verification_receipts must be an array")
    declared = stage["verification_argv"]
    if len(value) != len(declared):
        raise WorkflowReviewError(
            "verification receipts must exactly cover declared verification_argv"
        )
    receipts: list[dict[str, Any]] = []
    for index, (raw, argv) in enumerate(zip(value, declared, strict=True)):
        if not isinstance(raw, Mapping):
            raise WorkflowReviewError(f"verification_receipts[{index}] must be an object")
        row = dict(raw)
        _require_exact_keys(
            row,
            _VERIFICATION_RECEIPT_KEYS,
            label=f"verification_receipts[{index}]",
        )
        if row["argv"] != argv:
            raise WorkflowReviewError(
                f"verification_receipts[{index}] argv mismatch"
            )
        if (
            not isinstance(row["exit_code"], int)
            or isinstance(row["exit_code"], bool)
            or row["exit_code"] != 0
        ):
            raise WorkflowReviewError(
                f"verification_receipts[{index}] did not pass"
            )
        for stream in ("stdout", "stderr"):
            size = row[f"{stream}_size"]
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > _MAX_RECEIPT_STREAM_BYTES
            ):
                raise WorkflowReviewError(
                    f"verification_receipts[{index}] {stream} size is unbounded"
                )
            _require_sha256(
                row[f"{stream}_sha256"],
                label=f"verification_receipts[{index}].{stream}_sha256",
            )
        if row["launch_receipt_hash"] != launch_receipt_hash:
            raise WorkflowReviewError(
                f"verification_receipts[{index}] launch binding mismatch"
            )
        receipts.append(row)
    return receipts


def _validate_artifact_receipts(
    root: Path,
    stage: Mapping[str, Any],
    value: Any,
    *,
    launch_receipt_hash: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WorkflowReviewError("artifact_receipts must be an array")
    contract = stage["artifact_contract"]
    declared_paths = contract["paths"] if contract["required"] else []
    if len(value) != len(declared_paths):
        raise WorkflowReviewError(
            "artifact receipts must exactly cover required artifact paths"
        )
    receipts: list[dict[str, Any]] = []
    schema = contract["schema"]
    schema_digest = sha256_hex(canonical_json_bytes(schema))
    for index, (raw, expected_path) in enumerate(
        zip(value, declared_paths, strict=True)
    ):
        if not isinstance(raw, Mapping):
            raise WorkflowReviewError(f"artifact_receipts[{index}] must be an object")
        row = dict(raw)
        _require_exact_keys(
            row, _ARTIFACT_RECEIPT_KEYS, label=f"artifact_receipts[{index}]"
        )
        if row["path"] != expected_path:
            raise WorkflowReviewError(f"artifact_receipts[{index}] path mismatch")
        if row["schema"] != schema or row["schema_digest"] != schema_digest:
            raise WorkflowReviewError(f"artifact_receipts[{index}] schema mismatch")
        if row["launch_receipt_hash"] != launch_receipt_hash:
            raise WorkflowReviewError(
                f"artifact_receipts[{index}] launch binding mismatch"
            )
        data = _read_confined_regular(
            root,
            expected_path,
            label=f"artifact_receipts[{index}] artifact",
            max_bytes=_MAX_ARTIFACT_BYTES,
        )
        if (
            not isinstance(row["size"], int)
            or isinstance(row["size"], bool)
            or row["size"] != len(data)
            or _require_sha256(
                row["sha256"], label=f"artifact_receipts[{index}].sha256"
            )
            != sha256_hex(data)
        ):
            raise WorkflowReviewError(f"artifact_receipts[{index}] bytes mismatch")
        _require_sha256(
            row["schema_digest"],
            label=f"artifact_receipts[{index}].schema_digest",
        )
        try:
            current = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowReviewError(
                f"artifact_receipts[{index}] artifact must be JSON"
            ) from exc
        try:
            canonical = validate_json_value(
                current, schema, label=f"stage {stage['id']} artifact"
            )
        except (TypeError, ValueError, WorkflowSchemaError) as exc:
            raise WorkflowReviewError(
                f"artifact_receipts[{index}] artifact violates declared schema"
            ) from exc
        if row["content"] != canonical:
            raise WorkflowReviewError(f"artifact_receipts[{index}] content is stale")
        receipts.append(row)
    return receipts


def validate_success_task_receipt(
    definition: Mapping[str, Any],
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    root: Path | str | None,
) -> dict[str, Any]:
    """Validate a successful receipt and re-read all product-rooted evidence."""
    if root is None:
        raise WorkflowReviewError("product-rooted workflow evidence is required")
    _require_exact_keys(raw, _SUCCESS_RECEIPT_KEYS, label="successful task receipt")
    row = dict(raw)
    if (
        row["store_kind"] != "workflow_task_receipt"
        or row["schema_version"] != 1
        or isinstance(row["schema_version"], bool)
    ):
        raise WorkflowReviewError("successful task receipt header mismatch")
    for field in ("matrix_index", "run_generation"):
        if not isinstance(row[field], int) or isinstance(row[field], bool):
            raise WorkflowReviewError(
                f"successful task receipt {field} must be an integer"
            )
    validate_task_receipt_identity(plan, task, row)
    if row["repository_id"] != plan["repository_id"] or row["run_id"] != plan["run_id"]:
        raise WorkflowReviewError("successful task receipt repository/run binding mismatch")
    stage = next(item for item in definition["stages"] if item["id"] == task["stage_id"])
    provenance = _validate_launch_provenance(Path(root), plan, task, row["launch_provenance"])
    launch_hash = provenance["receipt_hash"]
    _validate_verification_receipts(
        stage, row["verification_receipts"], launch_receipt_hash=launch_hash
    )
    _validate_artifact_receipts(
        Path(root),
        stage,
        row["artifact_receipts"],
        launch_receipt_hash=launch_hash,
    )
    body = dict(row)
    receipt_hash = _require_sha256(
        body.pop("receipt_hash"), label="successful task receipt.receipt_hash"
    )
    if receipt_hash != sha256_hex(canonical_json_bytes(body)):
        raise WorkflowReviewError("successful task receipt hash mismatch")
    return row


def validate_task_receipt_identity(
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    raw: Mapping[str, Any],
) -> None:
    """Reject stale or foreign receipt bindings before normalization."""
    if not isinstance(raw, Mapping):
        raise WorkflowReviewError("task result must be an object")
    expected = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "actor_identity": task["actor_identity"],
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
    }
    for field, expected_value in expected.items():
        if field not in raw:
            raise WorkflowReviewError(f"task result {field} binding is required")
        if raw[field] != expected_value:
            raise WorkflowReviewError(
                f"task result {field} does not match launch plan"
            )


def _normalized_status_verdict(
    task: Mapping[str, Any],
    raw: Mapping[str, Any],
    output: Any,
) -> tuple[str, str]:
    if not isinstance(output, Mapping) or not isinstance(output.get("verdict"), str):
        raise WorkflowReviewError("task result output.verdict must be a string")
    output_verdict = output["verdict"].upper()
    raw_verdict = raw.get("verdict")
    if raw_verdict is not None and not isinstance(raw_verdict, str):
        raise WorkflowReviewError("task result verdict must be a string")
    verdict = str(raw_verdict or output_verdict).upper()
    if verdict != output_verdict:
        raise WorkflowReviewError("task result verdict differs from output.verdict")

    status = raw.get("status")
    if status is None:
        if verdict == "APPROVE":
            status = "approved" if task["kind"] in {"verifier", "skeptic"} else "passed"
        else:
            status = _VERDICT_DEFAULT_STATUS.get(verdict)
    if not isinstance(status, str) or status not in RESULT_STATUSES:
        raise WorkflowReviewError("task result status is unsupported")
    if verdict not in _STATUS_VERDICTS[status]:
        raise WorkflowReviewError("task result status and verdict are inconsistent")
    if task["kind"] in {"verifier", "skeptic"} and status == "approved" and verdict != "APPROVE":
        raise WorkflowReviewError("verifier/skeptic approval requires APPROVE")
    return status, verdict


def normalize_task_result(
    definition: Mapping[str, Any],
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    validate_task_receipt_identity(plan, task, raw)
    stage = next(item for item in definition["stages"] if item["id"] == task["stage_id"])
    actor = raw.get("actor_identity")
    output = raw.get("output")
    try:
        output = validate_stage_output(stage, output)
    except WorkflowSchemaError as exc:
        raise WorkflowReviewError(str(exc)) from exc
    status, verdict = _normalized_status_verdict(task, raw, output)
    success_receipt: dict[str, Any] | None = None
    if status in {"passed", "approved"}:
        success_receipt = validate_success_task_receipt(
            definition, plan, task, raw, root=root
        )
    result = {
        "store_kind": "workflow_task_result",
        "schema_version": 1,
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "kind": task["kind"],
        "actor_identity": actor,
        "status": status,
        "verdict": verdict,
        "output": output,
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
        "effect_type": task["effect_type"],
        "effect_receipt": raw.get("effect_receipt"),
        "error": raw.get("error"),
        "launch_provenance": (
            success_receipt["launch_provenance"] if success_receipt else None
        ),
        "verification_receipts": (
            success_receipt["verification_receipts"] if success_receipt else []
        ),
        "artifact_receipts": (
            success_receipt["artifact_receipts"] if success_receipt else []
        ),
        "receipt_hash": success_receipt["receipt_hash"] if success_receipt else None,
        "task_receipt": success_receipt,
    }
    result["result_hash"] = sha256_hex(canonical_json_bytes(result))
    return result


def evaluate_review(
    definition: Mapping[str, Any],
    plan: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    *,
    permission_denied: bool = False,
    ambiguous_receipt: bool = False,
    external_effect_without_receipt: bool = False,
    root: Path | str | None = None,
) -> dict[str, Any]:
    compiled = compile_workflow(definition)
    expected_tasks = {task["task_id"]: task for task in plan["tasks"]}
    actual: dict[str, Mapping[str, Any]] = {}
    invalid: list[str] = []
    for result in results:
        task_id = str(result.get("task_id") or "")
        if task_id not in expected_tasks or task_id in actual:
            invalid.append(task_id or "<missing>")
            continue
        task = expected_tasks[task_id]
        if (
            result.get("actor_identity") != task["actor_identity"]
            or result.get("stage_id") != task["stage_id"]
            or result.get("matrix_index") != task["matrix_index"]
            or result.get("plan_digest") != plan["plan_digest"]
            or result.get("definition_digest") != plan["definition_digest"]
            or result.get("run_generation") != plan["run_generation"]
        ):
            invalid.append(task_id)
            continue
        try:
            _normalized_status_verdict(task, result, result.get("output"))
            if result.get("status") in {"passed", "approved"}:
                task_receipt = result.get("task_receipt")
                if not isinstance(task_receipt, Mapping):
                    raise WorkflowReviewError("normalized task receipt is missing")
                validate_success_task_receipt(
                    compiled, plan, task, task_receipt, root=root
                )
        except WorkflowReviewError:
            invalid.append(task_id)
            continue
        actual[task_id] = result
    stage_results: dict[str, str] = {}
    for stage_id in plan["stage_order"]:
        task_rows = [task for task in plan["tasks"] if task["stage_id"] == stage_id]
        statuses = [actual.get(task["task_id"], {}).get("status", "missing") for task in task_rows]
        stage = next(item for item in compiled["stages"] if item["id"] == stage_id)
        accepted = {"approved"} if stage["kind"] in {"verifier", "skeptic"} else {"passed", "approved"}
        stage_results[stage_id] = (
            "approved" if stage["kind"] in {"verifier", "skeptic"} and all(s in accepted for s in statuses)
            else "passed" if all(s in accepted for s in statuses)
            else "failed"
        )
    # Caller-authored files, IDs, modes and hashes are structural evidence only.
    # Grok currently exposes no host-authenticated spawn/command receipt API
    # that OMG can verify. Never turn caller-selected provenance into authority.
    product_authority_verified = False
    identities_independent = False
    verifier_approved = False
    skeptic_approved = False
    if any(row.get("status") == "effect_unknown" for row in actual.values()):
        external_effect_without_receipt = True
    if invalid:
        ambiguous_receipt = True
    terminal = decide_terminal(
        required_stage_results=stage_results,
        verifier_approved=verifier_approved,
        skeptic_approved=skeptic_approved,
        permission_denied=permission_denied,
        ambiguous_receipt=ambiguous_receipt,
        external_effect_without_receipt=external_effect_without_receipt,
    )
    return {
        "terminal": terminal,
        "stage_results": stage_results,
        "verifier_approved": verifier_approved,
        "skeptic_approved": skeptic_approved,
        "identities_independent": identities_independent,
        "provenance_independent": identities_independent,
        "product_authority_verified": product_authority_verified,
        "authority_error": "E_WORKFLOW_PRODUCT_AUTHORITY_UNAVAILABLE",
        "invalid_results": invalid,
        "missing_task_ids": sorted(set(expected_tasks) - set(actual)),
    }


__all__ = [
    "RESULT_STATUSES",
    "WorkflowReviewError",
    "evaluate_review",
    "normalize_task_result",
    "validate_success_task_receipt",
    "validate_task_receipt_identity",
]
