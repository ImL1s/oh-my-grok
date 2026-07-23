from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.workflow_contract import (
    WORKFLOW_CAPABILITY_TIERS,
    decide_terminal,
    deterministic_topological_order,
    effective_permissions,
    ensure_immutable_same_version,
    validate_workflow_definition,
    workflow_definition_digest,
    workflow_replay_id,
    workflow_task_id,
)
from omg_cli.workflows.schema import WorkflowSchemaError, compile_workflow, validate_workflow_input


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "workflow"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _redigest(value: dict) -> dict:
    value["definition_digest"] = workflow_definition_digest(value)
    return value


def test_product_workflow_has_fixed_parallel_dag_and_unclaimed_native_projection() -> None:
    workflow = validate_workflow_definition(_load("production-safety-review-v1.json"))
    assert deterministic_topological_order(workflow["stages"]) == [
        "scope",
        "secrets",
        "deploy-gates",
        "cron-r2",
        "api-ops-docs",
        "verify",
        "skeptic",
    ]
    assert workflow["max_parallelism"] == 4
    assert workflow["native_projection"]["status"] == "optional_unclaimed"
    assert WORKFLOW_CAPABILITY_TIERS["T0"] == "unavailable"
    assert WORKFLOW_CAPABILITY_TIERS["T5"] == "recoverable_effects"


@pytest.mark.parametrize(
    "fixture,match",
    [
        ("invalid-nested-permission.json", "unsupported/privileged"),
        ("invalid-self-verifier.json", "author identity"),
        ("invalid-cycle.json", "cycle"),
    ],
)
def test_workflow_negative_fixtures_fail_closed(fixture: str, match: str) -> None:
    with pytest.raises(ContractValidationError, match=match):
        validate_workflow_definition(_load(fixture))


def test_permissions_are_exact_intersection_in_declared_order() -> None:
    declared = ["read_repository", "run_declared_verification", "emit_declared_artifact"]
    effective = effective_permissions(
        declared,
        repository_policy=declared,
        host_capabilities=["emit_declared_artifact", "read_repository"],
        launch_receipt_permissions=["read_repository", "emit_declared_artifact"],
    )
    assert effective == ("read_repository", "emit_declared_artifact")
    with pytest.raises(ContractValidationError, match="unsupported"):
        effective_permissions(["publish_release"], [], [], [])


def test_task_and_replay_identity_bind_generation_and_verified_receipt() -> None:
    workflow = _load("production-safety-review-v1.json")
    kwargs = {
        "repository_id": "OMG",
        "workflow_name": workflow["name"],
        "workflow_version": workflow["workflow_version"],
        "definition_digest": workflow["definition_digest"],
        "input_digest": "a" * 64,
        "stage_id": "secrets",
        "matrix_index": 0,
        "run_generation": 1,
    }
    task = workflow_task_id(**kwargs)
    assert len(task) == 64 and workflow_task_id(**kwargs) == task
    assert workflow_task_id(**{**kwargs, "run_generation": 2}) != task
    assert workflow_replay_id(task, None) != workflow_replay_id(task, "b" * 64)


def test_terminal_gate_requires_every_stage_verifier_and_skeptic() -> None:
    results = {"author": "passed", "checks": "passed", "verify": "approved", "skeptic": "approved"}
    assert decide_terminal(
        required_stage_results=results, verifier_approved=True, skeptic_approved=True
    ) == "ship"
    assert decide_terminal(
        required_stage_results=results, verifier_approved=True, skeptic_approved=False
    ) == "no_ship"
    assert decide_terminal(
        required_stage_results=results,
        verifier_approved=True,
        skeptic_approved=True,
        permission_denied=True,
    ) == "blocked"
    assert decide_terminal(
        required_stage_results=results,
        verifier_approved=True,
        skeptic_approved=True,
        external_effect_without_receipt=True,
    ) == "effect_unknown"


def test_same_version_is_immutable_and_shell_or_native_claims_are_rejected() -> None:
    installed = _load("production-safety-review-v1.json")
    ensure_immutable_same_version(installed, copy.deepcopy(installed))

    mutable = copy.deepcopy(installed)
    mutable["stages"][1]["timeout_seconds"] += 1
    _redigest(mutable)
    with pytest.raises(ContractValidationError, match="same workflow version"):
        ensure_immutable_same_version(installed, mutable)

    shell = copy.deepcopy(installed)
    shell["stages"][1]["verification_argv"] = [["bash", "-lc", "pytest"]]
    _redigest(shell)
    with pytest.raises(ContractValidationError, match="shell interpreter"):
        validate_workflow_definition(shell)

    claimed = copy.deepcopy(installed)
    claimed["native_projection"].update(
        status="claimed",
        syntax=".grok/workflows/production-safety-review.rhai",
        probe_evidence="public-probe",
        fresh_invocation_evidence="fresh-session",
    )
    _redigest(claimed)
    with pytest.raises(ContractValidationError, match="NATIVE_UNSUPPORTED"):
        validate_workflow_definition(claimed)


@pytest.mark.parametrize("invalid_properties", [None, [], "not-an-object"])
def test_public_compile_rejects_malformed_input_schema_without_type_error(
    invalid_properties: object,
) -> None:
    definition = _load("production-safety-review-v1.json")
    definition["input_schema"]["properties"] = invalid_properties
    _redigest(definition)
    with pytest.raises(WorkflowSchemaError, match="properties must be an object"):
        compile_workflow(definition)
    with pytest.raises(WorkflowSchemaError, match="properties must be an object"):
        validate_workflow_input(definition, {"candidate_commit": "a" * 40})
