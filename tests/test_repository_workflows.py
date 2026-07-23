"""Product-owned repository workflow compiler/runner/replay tests."""
from __future__ import annotations

import copy
import json
import multiprocessing
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from omg_cli.contracts.workflow_contract import WORKFLOW_CAPABILITY_TIERS, workflow_definition_digest
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.workflows.permissions import admit_definition
from omg_cli.workflows.planner import build_plan
from omg_cli.workflows.registry import WorkflowRegistryError, install_workflow, list_workflows, resolve_workflow
from omg_cli.workflows.review import (
    WorkflowReviewError,
    evaluate_review,
    normalize_task_result,
    validate_success_task_receipt,
)
from omg_cli.workflows.replay import assess_replay, verified_effect_receipt
from omg_cli.workflows.runner import (
    _process_group_gone,
    _task_requires_terminable_executor,
    run_workflow,
)
from omg_cli.workflows.schema import WorkflowSchemaError, compile_workflow, validate_workflow_input


FIXTURE = Path(__file__).parent / "fixtures" / "workflow" / "production-safety-review-v1.json"
ALL_PERMISSIONS = (
    "read_repository",
    "write_declared_paths",
    "invoke_declared_mcp",
    "emit_declared_artifact",
    "run_declared_verification",
    "request_cli_transition",
    "reconcile_declared_effect",
)


def _definition() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _input() -> dict:
    return {"candidate_commit": "a" * 40}


def _redigest(definition: dict) -> dict:
    definition["definition_digest"] = workflow_definition_digest(definition)
    return definition


def _provision_receipts(
    root: Path,
    definition: dict,
    workflow_input: dict,
    *,
    run_generation: int = 0,
) -> tuple[dict, dict[str, dict]]:
    plan = build_plan(
        definition, workflow_input, repository_id="OMG", run_generation=run_generation
    )
    stages = {stage["id"]: stage for stage in definition["stages"]}
    receipts: dict[str, dict] = {}
    empty_hash = sha256_hex(b"")
    for index, task in enumerate(plan["tasks"]):
        stage = stages[task["stage_id"]]
        launch_body = {
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
            "launch_id": f"launch-{index}",
            "session_id": f"session-{index}",
            "agent_instance_id": f"agent-{index}",
        }
        launch_hash = sha256_hex(canonical_json_bytes(launch_body))
        launch_path = (
            root
            / ".omg"
            / "artifacts"
            / "workflow-launches"
            / plan["run_id"]
            / f"{task['task_id']}.json"
        )
        launch_path.parent.mkdir(parents=True, exist_ok=True)
        launch_path.write_bytes(
            canonical_json_bytes({**launch_body, "receipt_hash": launch_hash})
        )
        launch_path.chmod(0o400)

        artifact_receipts = []
        for relative in stage["artifact_contract"]["paths"]:
            content = {"task_id": task["task_id"], "verdict": "APPROVE"}
            data = canonical_json_bytes(content)
            artifact_path = root / relative
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(data)
            schema = stage["artifact_contract"]["schema"]
            artifact_receipts.append(
                {
                    "path": relative,
                    "size": len(data),
                    "sha256": sha256_hex(data),
                    "schema": schema,
                    "schema_digest": sha256_hex(canonical_json_bytes(schema)),
                    "content": content,
                    "launch_receipt_hash": launch_hash,
                }
            )
        verifier = task["kind"] in {"verifier", "skeptic"}
        body = {
            "store_kind": "workflow_task_receipt",
            "schema_version": 1,
            "repository_id": plan["repository_id"],
            "run_id": plan["run_id"],
            "definition_digest": plan["definition_digest"],
            "plan_digest": plan["plan_digest"],
            "task_id": task["task_id"],
            "stage_id": task["stage_id"],
            "matrix_index": task["matrix_index"],
            "actor_identity": task["actor_identity"],
            "run_generation": plan["run_generation"],
            "status": "approved" if verifier else "passed",
            "verdict": "APPROVE",
            "output": {"verdict": "APPROVE"},
            "launch_provenance": {
                "provider": "grok",
                "launch_id": launch_body["launch_id"],
                "session_id": launch_body["session_id"],
                "agent_instance_id": launch_body["agent_instance_id"],
                "receipt_path": launch_path.relative_to(root).as_posix(),
                "receipt_hash": launch_hash,
            },
            "verification_receipts": [
                {
                    "argv": argv,
                    "exit_code": 0,
                    "stdout_size": 0,
                    "stdout_sha256": empty_hash,
                    "stderr_size": 0,
                    "stderr_sha256": empty_hash,
                    "launch_receipt_hash": launch_hash,
                }
                for argv in stage["verification_argv"]
            ],
            "artifact_receipts": artifact_receipts,
        }
        receipts[task["task_id"]] = {
            **body,
            "receipt_hash": sha256_hex(canonical_json_bytes(body)),
        }
    return plan, receipts


def _rehash_receipt(receipt: dict) -> None:
    body = dict(receipt)
    body.pop("receipt_hash", None)
    receipt["receipt_hash"] = sha256_hex(canonical_json_bytes(body))


def _rewrite_launch_receipt(root: Path, receipt: dict) -> None:
    provenance = receipt["launch_provenance"]
    path = root / provenance["receipt_path"]
    path.chmod(0o600)
    launch = json.loads(path.read_text(encoding="utf-8"))
    launch.update(
        launch_id=provenance["launch_id"],
        session_id=provenance["session_id"],
        agent_instance_id=provenance["agent_instance_id"],
    )
    body = dict(launch)
    body.pop("receipt_hash")
    launch_hash = sha256_hex(canonical_json_bytes(body))
    path.write_bytes(canonical_json_bytes({**body, "receipt_hash": launch_hash}))
    path.chmod(0o400)
    provenance["receipt_hash"] = launch_hash
    for row in receipt["verification_receipts"]:
        row["launch_receipt_hash"] = launch_hash
    for row in receipt["artifact_receipts"]:
        row["launch_receipt_hash"] = launch_hash
    _rehash_receipt(receipt)


def test_compiler_and_input_validator_are_canonical() -> None:
    first = compile_workflow(_definition())
    second = compile_workflow(json.dumps(_definition()))
    assert first == second
    assert validate_workflow_input(first, _input()) == _input()
    with pytest.raises(WorkflowSchemaError, match="candidate_commit"):
        validate_workflow_input(first, {})


def test_registry_is_immutable_and_migration_bound(tmp_path: Path) -> None:
    definition = _definition()
    first = install_workflow(tmp_path, definition)
    duplicate = install_workflow(tmp_path, copy.deepcopy(definition))
    assert first["duplicate"] is False and duplicate["duplicate"] is True

    mutable = copy.deepcopy(definition)
    mutable["stages"][0]["timeout_seconds"] += 1
    _redigest(mutable)
    with pytest.raises(WorkflowRegistryError, match="same workflow version"):
        install_workflow(tmp_path, mutable)

    next_version = copy.deepcopy(definition)
    next_version["workflow_version"] = "1.1.0"
    next_version["migration"] = {
        "supersedes_version": definition["workflow_version"],
        "supersedes_digest": definition["definition_digest"],
        "review_digest": "b" * 64,
    }
    _redigest(next_version)
    install_workflow(tmp_path, next_version)
    assert [row["workflow_version"] for row in list_workflows(tmp_path)] == ["1.0.0", "1.1.0"]
    assert resolve_workflow(tmp_path, definition["name"])["workflow_version"] == "1.1.0"
    assert first["path"].startswith(".omg/workflows/registry/")


def test_registry_semver_orders_prerelease_before_release(tmp_path: Path) -> None:
    prerelease = _definition()
    prerelease["workflow_version"] = "1.0.0-alpha.2"
    _redigest(prerelease)
    install_workflow(tmp_path, prerelease)

    release = _definition()
    release["migration"] = {
        "supersedes_version": prerelease["workflow_version"],
        "supersedes_digest": prerelease["definition_digest"],
        "review_digest": "c" * 64,
    }
    _redigest(release)
    install_workflow(tmp_path, release)

    assert [row["workflow_version"] for row in list_workflows(tmp_path)] == [
        "1.0.0-alpha.2",
        "1.0.0",
    ]
    assert resolve_workflow(tmp_path, release["name"])["workflow_version"] == "1.0.0"


def test_registry_rejects_path_escape_and_symlink_escape(tmp_path: Path) -> None:
    with pytest.raises(WorkflowRegistryError, match="name"):
        resolve_workflow(tmp_path, "../outside", "1.0.0")
    with pytest.raises(WorkflowRegistryError, match="version"):
        resolve_workflow(tmp_path, "review", "../../outside")

    outside = tmp_path / "outside"
    outside.mkdir()
    registry = tmp_path / ".omg" / "workflows" / "registry"
    registry.mkdir(parents=True)
    (registry / "review").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkflowRegistryError, match="escapes"):
        resolve_workflow(tmp_path, "review", "1.0.0")


def test_planner_is_deterministic_topological_and_bounded() -> None:
    one = build_plan(_definition(), _input(), run_generation=3)
    two = build_plan(_definition(), _input(), run_generation=3)
    assert one == two
    assert one["stage_order"] == [
        "scope",
        "secrets",
        "deploy-gates",
        "cron-r2",
        "api-ops-docs",
        "verify",
        "skeptic",
    ]
    assert [len(wave) for wave in one["waves"]] == [1, 4, 1, 1]
    assert one["max_parallelism"] == 4
    assert one["run_id"].startswith("workflow-")


def test_permission_gate_requires_exact_declared_intersection() -> None:
    definition = _definition()
    admitted = admit_definition(
        definition,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )
    assert admitted["allowed"] is True
    denied = admit_definition(
        definition,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=("read_repository", "emit_declared_artifact"),
        launch_receipt_permissions=ALL_PERMISSIONS,
    )
    assert denied["allowed"] is False
    assert "run_declared_verification" in denied["stages"][0]["missing"]


def test_runner_executes_bounded_parallel_checks_but_cannot_self_authorize_ship(
    tmp_path: Path,
) -> None:
    _plan, receipts = _provision_receipts(tmp_path, _definition(), _input())

    def execute(task: dict, context: dict) -> dict:
        assert context["plan_digest"] == context["plan"]["plan_digest"]
        time.sleep(0.015)
        return receipts[task["task_id"]]

    summary = run_workflow(
        tmp_path,
        _definition(),
        _input(),
        execute_task=execute,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )
    assert summary["terminal"] == "no_ship"
    assert len(summary["results"]) == 7
    assert all(row["status"] in {"passed", "approved"} for row in summary["results"])
    assert summary["review"]["verifier_approved"] is False
    assert summary["review"]["skeptic_approved"] is False
    assert summary["review"]["identities_independent"] is False
    assert summary["review"]["product_authority_verified"] is False
    assert (
        summary["review"]["authority_error"]
        == "E_WORKFLOW_PRODUCT_AUTHORITY_UNAVAILABLE"
    )
    assert summary["replay"]["adoptable_task_ids"] == []
    artifact = tmp_path / ".omg" / "artifacts" / "workflow-runs" / summary["plan"]["run_id"]
    assert (artifact / "journal.jsonl").is_file()
    assert (artifact / "summary.json").is_file()
    assert not (tmp_path / ".omg" / "state").exists()


def test_locally_minted_distinct_receipts_with_fake_rc_zero_never_ship(
    tmp_path: Path,
) -> None:
    definition = _definition()
    marker = tmp_path / "verification-command-executed"
    for stage in definition["stages"]:
        stage["verification_argv"] = [
            [
                "python3",
                "-c",
                f"open({str(marker)!r},'a').write('executed\\n')",
            ]
        ]
    _redigest(definition)
    plan, receipts = _provision_receipts(tmp_path, definition, _input())

    summary = run_workflow(
        tmp_path,
        definition,
        _input(),
        execute_task=lambda task, _context: receipts[task["task_id"]],
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )

    assert len(summary["results"]) == len(plan["tasks"]) == 7
    assert all(row["status"] in {"passed", "approved"} for row in summary["results"])
    assert marker.exists() is False  # 0/7 declared commands were actually executed.
    assert summary["terminal"] == "no_ship"
    assert summary["review"]["product_authority_verified"] is False
    assert summary["review"]["verifier_approved"] is False
    assert summary["replay"]["adoptable_task_ids"] == []


def test_pure_approval_without_commands_artifacts_or_launch_proof_never_ships(
    tmp_path: Path,
) -> None:
    definition = _definition()
    plan = build_plan(definition, _input())
    task = plan["tasks"][0]
    pure = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "actor_identity": task["actor_identity"],
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
        "status": "passed",
        "verdict": "APPROVE",
        "output": {"verdict": "APPROVE"},
    }
    with pytest.raises(WorkflowReviewError, match="successful task receipt keys mismatch"):
        normalize_task_result(definition, plan, task, pure, root=tmp_path)

    summary = run_workflow(
        tmp_path,
        definition,
        _input(),
        execute_task=lambda _task, _context: pure,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )
    assert summary["terminal"] == "no_ship"
    assert summary["results"][0]["status"] == "failed"
    assert summary["review"]["verifier_approved"] is False


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("missing_binding", "successful task receipt keys mismatch"),
        ("verification_argv", "argv mismatch"),
        ("artifact_path", "path mismatch"),
        ("artifact_hash", "bytes mismatch"),
        ("artifact_schema", "schema mismatch"),
        ("stale_plan", "plan_digest does not match launch plan"),
    ],
)
def test_success_receipt_rejects_missing_foreign_or_stale_evidence(
    tmp_path: Path, mutation: str, match: str
) -> None:
    definition = _definition()
    plan, receipts = _provision_receipts(tmp_path, definition, _input())
    task = plan["tasks"][0]
    receipt = copy.deepcopy(receipts[task["task_id"]])
    if mutation == "missing_binding":
        del receipt["plan_digest"]
    elif mutation == "verification_argv":
        receipt["verification_receipts"][0]["argv"] = ["python3", "--version"]
        _rehash_receipt(receipt)
    elif mutation == "artifact_path":
        receipt["artifact_receipts"][0]["path"] = ".omg/artifacts/workflow/foreign.json"
        _rehash_receipt(receipt)
    elif mutation == "artifact_hash":
        receipt["artifact_receipts"][0]["sha256"] = "f" * 64
        _rehash_receipt(receipt)
    elif mutation == "artifact_schema":
        receipt["artifact_receipts"][0]["schema"] = {"type": "array"}
        receipt["artifact_receipts"][0]["schema_digest"] = sha256_hex(
            canonical_json_bytes({"type": "array"})
        )
        _rehash_receipt(receipt)
    else:
        receipt["plan_digest"] = "e" * 64
        _rehash_receipt(receipt)
    with pytest.raises(WorkflowReviewError, match=match):
        validate_success_task_receipt(
            definition, plan, task, receipt, root=tmp_path
        )


def test_success_receipt_rejects_symlinked_artifact(tmp_path: Path) -> None:
    definition = _definition()
    plan, receipts = _provision_receipts(tmp_path, definition, _input())
    task = plan["tasks"][0]
    receipt = receipts[task["task_id"]]
    artifact = tmp_path / receipt["artifact_receipts"][0]["path"]
    outside = tmp_path / "outside-artifact.json"
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(outside)
    with pytest.raises(WorkflowReviewError, match="symlink"):
        validate_success_task_receipt(
            definition, plan, task, receipt, root=tmp_path
        )


def test_success_receipt_requires_immutable_product_launch_proof(
    tmp_path: Path,
) -> None:
    definition = _definition()
    plan, receipts = _provision_receipts(tmp_path, definition, _input())
    task = plan["tasks"][0]
    receipt = receipts[task["task_id"]]
    (tmp_path / receipt["launch_provenance"]["receipt_path"]).chmod(0o600)
    with pytest.raises(WorkflowReviewError, match="must be immutable"):
        validate_success_task_receipt(
            definition, plan, task, receipt, root=tmp_path
        )


def test_reused_product_launch_identity_cannot_ship(tmp_path: Path) -> None:
    definition = _definition()
    plan, receipts = _provision_receipts(tmp_path, definition, _input())
    verifier = next(task for task in plan["tasks"] if task["kind"] == "verifier")
    skeptic = next(task for task in plan["tasks"] if task["kind"] == "skeptic")
    verifier_provenance = receipts[verifier["task_id"]]["launch_provenance"]
    skeptic_receipt = receipts[skeptic["task_id"]]
    skeptic_receipt["launch_provenance"].update(
        launch_id=verifier_provenance["launch_id"],
        session_id=verifier_provenance["session_id"],
        agent_instance_id=verifier_provenance["agent_instance_id"],
    )
    _rewrite_launch_receipt(tmp_path, skeptic_receipt)
    normalized = [
        normalize_task_result(
            definition,
            plan,
            task,
            receipts[task["task_id"]],
            root=tmp_path,
        )
        for task in plan["tasks"]
    ]
    review = evaluate_review(definition, plan, normalized, root=tmp_path)
    assert review["terminal"] == "no_ship"
    assert review["identities_independent"] is False
    assert review["provenance_independent"] is False
    assert review["verifier_approved"] is False
    assert review["skeptic_approved"] is False


def test_runner_rejection_is_no_ship_and_never_self_approves(tmp_path: Path) -> None:
    called: list[str] = []
    _plan, receipts = _provision_receipts(tmp_path, _definition(), _input())

    def execute(task: dict, context: dict) -> dict:
        called.append(task["stage_id"])
        if task["stage_id"] == "deploy-gates":
            return {
                **receipts[task["task_id"]],
                "actor_identity": task["actor_identity"],
                "status": "failed",
                "verdict": "NO_SHIP",
                "output": {"verdict": "NO_SHIP"},
            }
        return receipts[task["task_id"]]

    summary = run_workflow(
        tmp_path,
        _definition(),
        _input(),
        execute_task=execute,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )
    assert summary["terminal"] == "no_ship"
    assert "verify" not in called and "skeptic" not in called
    assert summary["review"]["verifier_approved"] is False


def test_task_result_status_verdict_and_output_must_agree(tmp_path: Path) -> None:
    definition = _definition()
    plan = build_plan(definition, _input())
    task = next(item for item in plan["tasks"] if item["kind"] == "verifier")
    _provisioned_plan, receipts = _provision_receipts(
        tmp_path, definition, _input()
    )
    binding = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "actor_identity": task["actor_identity"],
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
    }
    inconsistent = {
        **binding,
        "status": "approved",
        "verdict": "NO_SHIP",
        "output": {"verdict": "NO_SHIP"},
    }
    with pytest.raises(WorkflowReviewError, match="inconsistent"):
        normalize_task_result(definition, plan, task, inconsistent, root=tmp_path)

    divergent = {
        **inconsistent,
        "verdict": "APPROVE",
        "output": {"verdict": "NO_SHIP"},
    }
    with pytest.raises(WorkflowReviewError, match="differs from output.verdict"):
        normalize_task_result(definition, plan, task, divergent, root=tmp_path)

    passed_receipt = {
        **receipts[task["task_id"]],
        "status": "passed",
    }
    passed_body = dict(passed_receipt)
    passed_body.pop("receipt_hash")
    passed_receipt["receipt_hash"] = sha256_hex(canonical_json_bytes(passed_body))
    passed_approval = normalize_task_result(
        definition,
        plan,
        task,
        passed_receipt,
        root=tmp_path,
    )
    passed_review = evaluate_review(definition, plan, [passed_approval], root=tmp_path)
    assert passed_review["verifier_approved"] is False
    assert passed_review["terminal"] != "ship"

    forged = {
        **inconsistent,
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
    }
    review = evaluate_review(definition, plan, [forged])
    assert review["terminal"] != "ship"
    assert review["verifier_approved"] is False


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("task_id", "f" * 64),
        ("stage_id", "foreign-stage"),
        ("matrix_index", 99),
        ("actor_identity", "foreign-actor"),
        ("plan_digest", "e" * 64),
        ("definition_digest", "d" * 64),
        ("run_generation", 99),
    ],
)
def test_task_result_rejects_stale_or_foreign_binding_before_normalization(
    field: str, foreign: object
) -> None:
    definition = _definition()
    plan = build_plan(definition, _input(), run_generation=7)
    task = plan["tasks"][0]
    raw = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "actor_identity": task["actor_identity"],
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
        "status": "passed",
        "verdict": "APPROVE",
        "output": {"verdict": "APPROVE"},
    }
    raw[field] = foreign
    with pytest.raises(WorkflowReviewError, match=field):
        normalize_task_result(definition, plan, task, raw)


def test_permission_denial_blocks_before_executor(tmp_path: Path) -> None:
    called = False

    def execute(task: dict, context: dict) -> dict:
        nonlocal called
        called = True
        raise AssertionError("permission gate must block callback")

    summary = run_workflow(
        tmp_path,
        _definition(),
        _input(),
        execute_task=execute,
        repository_policy=("read_repository",),
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )
    assert summary["terminal"] == "blocked"
    assert called is False


def test_runner_terminates_noncooperative_task_process(tmp_path: Path) -> None:
    definition = _definition()
    definition["stages"][0]["timeout_seconds"] = 1
    _redigest(definition)
    def execute(task: dict, context: dict) -> dict:
        if task["stage_id"] == "scope":
            while True:
                time.sleep(1)
        raise AssertionError("only the timed scope callback may run")

    started = time.monotonic()
    summary = run_workflow(
        tmp_path,
        definition,
        _input(),
        execute_task=execute,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2
    assert summary["terminal"] == "effect_unknown"
    assert summary["results"][0]["error"] == "E_WORKFLOW_TIMEOUT"
    assert summary["results"][0]["status"] == "effect_unknown"
    assert (
        summary["results"][0]["task_id"]
        in summary["replay"]["effect_unknown_task_ids"]
    )
    assert summary["replay"]["adoptable_task_ids"] == []
    assert summary["review"]["verifier_approved"] is False


def test_process_group_disappearance_polls_transient_eperm_but_fails_closed(
    monkeypatch,
) -> None:
    attempts = iter(
        [
            PermissionError("transient"),
            PermissionError("transient"),
            ProcessLookupError("gone"),
        ]
    )

    def transient_then_gone(_pgid: int, _signal: int) -> None:
        error = next(attempts)
        raise error

    monkeypatch.setattr("omg_cli.workflows.runner.os.killpg", transient_then_gone)
    assert _process_group_gone(12345, timeout=0.1) is True

    monkeypatch.setattr(
        "omg_cli.workflows.runner.os.killpg",
        lambda _pgid, _signal: (_ for _ in ()).throw(PermissionError("persistent")),
    )
    assert _process_group_gone(12345, timeout=0.02) is False


@pytest.mark.parametrize("inherited", ["file", "socket"])
def test_forked_receipt_resolver_closes_inherited_writable_authority(
    tmp_path: Path, inherited: str
) -> None:
    definition = _definition()
    _plan, receipts = _provision_receipts(tmp_path, definition, _input())
    protected = tmp_path / "preopened.txt"
    protected.write_bytes(b"safe")
    file_handle = protected.open("r+b")
    left, right = socket.socketpair()
    right.setblocking(False)
    try:
        def execute(task: dict, _context: dict) -> dict:
            if inherited == "file":
                os.write(file_handle.fileno(), b"forged")
            else:
                left.send(b"forged")
            return receipts[task["task_id"]]

        summary = run_workflow(
            tmp_path,
            definition,
            _input(),
            execute_task=execute,
            repository_policy=ALL_PERMISSIONS,
            host_capabilities=ALL_PERMISSIONS,
            launch_receipt_permissions=ALL_PERMISSIONS,
        )
        assert summary["terminal"] == "no_ship"
        assert summary["results"][0]["status"] == "failed"
        assert "Bad file descriptor" in summary["results"][0]["error"]
        assert protected.read_bytes() == b"safe"
        with pytest.raises(BlockingIOError):
            right.recv(16)
    finally:
        file_handle.close()
        left.close()
        right.close()


@pytest.mark.parametrize("authority", ["artifact", "write_paths", "mcp"])
def test_receipt_resolver_rejects_authority_use_before_effect(
    tmp_path: Path, authority: str
) -> None:
    definition = _definition()
    effect_stage = next(
        stage for stage in definition["stages"] if stage["id"] == "deploy-gates"
    )
    effect_stage["permissions"] = ["read_repository"]
    if authority == "artifact":
        effect_stage["permissions"].append("emit_declared_artifact")
    elif authority == "write_paths":
        effect_stage["capability_mode"] = "read-write"
        effect_stage["write_paths"] = ["tmp/workflow-output.json"]
        effect_stage["permissions"].append("write_declared_paths")
    else:
        effect_stage["mcp_allowlist"] = ["repository.read"]
        effect_stage["permissions"].append("invoke_declared_mcp")
    effect_stage["timeout_seconds"] = 1
    _redigest(definition)
    marker = tmp_path / f"late-effect-{authority}"
    _plan, receipts = _provision_receipts(tmp_path, definition, _input())

    def execute(task: dict, context: dict) -> dict:
        if task["stage_id"] == "deploy-gates":
            if authority == "mcp":
                socket.socket()
            marker.write_text("late", encoding="utf-8")
        return receipts[task["task_id"]]

    summary = run_workflow(
        tmp_path,
        definition,
        _input(),
        execute_task=execute,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )

    assert summary["terminal"] in {"blocked", "no_ship"}
    effect_result = next(
        result for result in summary["results"] if result["stage_id"] == "deploy-gates"
    )
    assert effect_result["error"].startswith("E_WORKFLOW_EXECUTOR_AUTHORITY:")
    assert effect_result["status"] == "failed"
    assert effect_result["task_id"] not in summary["replay"]["adoptable_task_ids"]
    assert marker.exists() is False


def test_side_effect_authority_requires_terminable_executor() -> None:
    base = {
        "effect_type": None,
        "write_paths": [],
        "mcp_allowlist": [],
        "permissions": ["read_repository"],
    }
    assert _task_requires_terminable_executor(base) is False
    assert _task_requires_terminable_executor({**base, "effect_type": "gate"})
    assert _task_requires_terminable_executor({**base, "write_paths": ["out"]})
    assert _task_requires_terminable_executor({**base, "mcp_allowlist": ["read"]})
    for permission in (
        "write_declared_paths",
        "invoke_declared_mcp",
        "emit_declared_artifact",
        "run_declared_verification",
        "request_cli_transition",
        "reconcile_declared_effect",
    ):
        assert _task_requires_terminable_executor(
            {**base, "permissions": ["read_repository", permission]}
        )


@pytest.mark.parametrize("authority", ["artifact", "write_paths", "mcp"])
def test_authority_timeout_receipt_is_never_replayable(authority: str) -> None:
    definition = _definition()
    stage = definition["stages"][0]
    stage["permissions"] = ["read_repository"]
    if authority == "artifact":
        stage["permissions"].append("emit_declared_artifact")
    elif authority == "write_paths":
        stage["capability_mode"] = "read-write"
        stage["write_paths"] = ["tmp/workflow-output.json"]
        stage["permissions"].append("write_declared_paths")
    else:
        stage["mcp_allowlist"] = ["repository.read"]
        stage["permissions"].append("invoke_declared_mcp")
    _redigest(definition)
    plan = build_plan(definition, _input())
    task = plan["tasks"][0]
    timeout = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "actor_identity": task["actor_identity"],
        "status": "effect_unknown",
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
        "effect_receipt": None,
    }
    replay = assess_replay(plan, [timeout])
    assert replay["terminal"] == "effect_unknown"
    assert replay["effect_unknown_task_ids"] == [task["task_id"]]
    assert replay["adoptable_task_ids"] == []


def test_effect_task_fails_closed_before_callback_starts(tmp_path: Path) -> None:
    definition = _definition()
    effect_stage = definition["stages"][0]
    effect_stage["effect_type"] = "deployment-gate"
    effect_stage["permissions"].append("reconcile_declared_effect")
    _redigest(definition)
    context = multiprocessing.get_context("fork")
    callback_started = context.Value("i", 0)

    def execute(task: dict, context: dict) -> dict:
        if task["stage_id"] == "scope":
            callback_started.value = 1
        raise AssertionError("effect task callback must not run")

    summary = run_workflow(
        tmp_path,
        definition,
        _input(),
        execute_task=execute,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )

    assert callback_started.value == 0
    assert summary["results"][0]["error"] == "E_WORKFLOW_EFFECT_EXECUTOR_UNSAFE"
    assert summary["terminal"] == "effect_unknown"
    assert summary["replay"]["adoptable_task_ids"] == []


def test_runner_kills_descendant_process_group_before_return(tmp_path: Path) -> None:
    definition = _definition()
    definition["stages"][0]["timeout_seconds"] = 1
    _redigest(definition)
    late_marker = tmp_path / "descendant-late-effect"
    def execute(task: dict, context: dict) -> dict:
        if task["stage_id"] == "scope":
            subprocess.Popen(
                [
                    "/bin/sh",
                    "-c",
                    "sleep 1.4; printf late > \"$1\"",
                    "omg-detached-child",
                    str(late_marker),
                ],
                start_new_session=True,
            )
        raise AssertionError("scope callback must fail before returning")

    summary = run_workflow(
        tmp_path,
        definition,
        _input(),
        execute_task=execute,
        repository_policy=ALL_PERMISSIONS,
        host_capabilities=ALL_PERMISSIONS,
        launch_receipt_permissions=ALL_PERMISSIONS,
    )

    assert summary["terminal"] == "no_ship"
    assert summary["results"][0]["error"].startswith(
        "E_WORKFLOW_EXECUTOR_AUTHORITY:"
    )
    assert summary["results"][0]["status"] == "failed"
    time.sleep(1.6)
    assert late_marker.exists() is False


def test_replay_is_generation_fenced_and_effects_fail_closed() -> None:
    definition = _definition()
    definition["stages"][0]["effect_type"] = "deployment-gate"
    definition["stages"][0]["permissions"].append("reconcile_declared_effect")
    _redigest(definition)
    plan = build_plan(definition, _input(), run_generation=7)
    task = plan["tasks"][0]
    raw = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "actor_identity": task["actor_identity"],
        "status": "passed",
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
        "effect_receipt": None,
    }
    unknown = assess_replay(plan, [raw])
    assert unknown["terminal"] == "effect_unknown"
    assert unknown["adoptable_task_ids"] == []

    failed = assess_replay(
        plan,
        [{**raw, "status": "failed", "verdict": "NO_SHIP"}],
    )
    assert failed["terminal"] == "effect_unknown"
    assert failed["adoptable_task_ids"] == []

    invalid = assess_replay(plan, [{**raw, "effect_receipt": {"verified": True}}])
    assert invalid["terminal"] == "effect_unknown"
    assert invalid["adoptable_task_ids"] == []

    receipt = verified_effect_receipt(task=task, plan=plan, effect_id="effect-a")
    adopted = assess_replay(plan, [{**raw, "effect_receipt": receipt}])
    assert adopted["terminal"] == "effect_unknown"
    assert adopted["adoptable_task_ids"] == []

    cancelled = assess_replay(
        plan,
        [{**raw, "status": "cancelled", "effect_receipt": receipt}],
    )
    assert cancelled["terminal"] == "effect_unknown"
    assert cancelled["adoptable_task_ids"] == []

    stale = assess_replay(plan, [{**raw, "run_generation": 8, "effect_receipt": receipt}])
    assert stale["terminal"] == "blocked"


def test_replay_rejects_pure_non_effect_approval_without_product_receipt() -> None:
    definition = _definition()
    plan = build_plan(definition, _input())
    task = plan["tasks"][0]
    forged = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "actor_identity": task["actor_identity"],
        "status": "passed",
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
    }
    replay = assess_replay(plan, [forged])
    assert replay["terminal"] == "blocked"
    assert replay["adoptable_task_ids"] == []
    assert replay["ambiguous_task_ids"] == [task["task_id"]]


def test_capability_tiers_and_terminals_remain_exact() -> None:
    assert list(WORKFLOW_CAPABILITY_TIERS) == ["T0", "T1", "T2", "T3", "T4", "T5"]
    assert WORKFLOW_CAPABILITY_TIERS["T2"] == "validated_runner"
    assert WORKFLOW_CAPABILITY_TIERS["T5"] == "recoverable_effects"
