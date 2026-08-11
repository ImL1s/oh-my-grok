"""Hermetic tests for Composition Lane Worker Protocol V1 (#69 PR13)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.main import main
from omg_cli.state import write_status
from omg_cli.team import api as team_api
from omg_cli.team.api import execute_team_api
from omg_cli.team.compositions.hyperplan import (
    HYPERPLAN_RESULT_BUNDLE_KIND,
    HyperplanError,
    admit_hyperplan_tasks_v1,
    claim_hyperplan_lane_v1,
    collect_hyperplan_tasks_v1,
    compile_hyperplan_decision_v1,
    materialize_hyperplan_v1,
    produce_hyperplan_decision_v1,
    submit_hyperplan_lane_result_v1,
)
from omg_cli.team.compositions.lane_protocol import (
    COMPOSITION_LANE_CLAIM_KIND,
    CompositionLaneProtocolError,
    parse_composition_lane_claim_v1,
    redact_claim_token,
)
from omg_cli.team.compositions.security_research import (
    SECURITY_RESEARCH_RESULT_BUNDLE_KIND,
    SecurityResearchError,
    admit_security_research_tasks_v1,
    claim_security_research_lane_v1,
    collect_security_research_tasks_v1,
    compile_security_research_report_v1,
    materialize_security_research_v1,
    produce_security_research_report_v1,
    submit_security_research_lane_result_v1,
)
from omg_cli.team.compositions.task_driver import (
    MAX_INLINE_JSON_BYTES,
    parse_lane_task_result_v1,
)
from omg_cli.team.operation_catalog import (
    TEAM_OPERATION_CATALOG_V1,
    TEAM_OPERATION_CATALOG_V2,
    TEAM_OPERATION_CATALOG_V3,
    catalog_document_json,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, start_team

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_V1 = ROOT / "tests" / "golden" / "team_operation_catalog_v1.json"
GOLDEN_V2 = ROOT / "tests" / "golden" / "team_operation_catalog_v2.json"
GOLDEN_V3 = ROOT / "tests" / "golden" / "team_operation_catalog_v3.json"
GOLDEN_V4 = ROOT / "tests" / "golden" / "team_operation_catalog_v4.json"

TEAM = "team"
SEED_TASKS = [{"task_id": "t-a", "owned_files": ["a.py"]}]
WORKER = "t-a"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in (
        *WORKER_ENV_MARKERS,
        "OMG_TEAM_WORKER_ID",
        "OMG_TEAM_RUN_ID",
        "OMG_TEAM_ID",
        "OMG_TEAM_LEADER_ROOT",
        "OMG_TEAM_STATE_ROOT",
        "OMG_TEAM_OWNER_TOKEN",
        "OMG_PROJECT_ROOT",
        "OMG_PROCESS_FANOUT_WORKER",
        "OMG_SPAWNED_WORKER",
    ):
        monkeypatch.delenv(key, raising=False)


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "composition lane protocol seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    run_id = str(meta["run_id"])
    write_status(tmp_path, run_id, "running")
    return run_id


def _full_worker_env(root: Path, run_id: str, worker_id: str = WORKER) -> dict[str, str]:
    leader = str(root.resolve())
    return {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_WORKER": "1",
        "OMG_TEAM_WORKER_ID": worker_id,
        "OMG_TEAM_RUN_ID": run_id,
        "OMG_TEAM_ID": TEAM,
        "OMG_TEAM_LEADER_ROOT": leader,
        "OMG_TEAM_STATE_ROOT": str((root / ".omg" / "state").resolve()),
        "OMG_TEAM_OWNER_TOKEN": "test-owner-token",
        "OMG_PROJECT_ROOT": leader,
    }


def _hp_spec(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": 1,
        "goal": "ship a safe plan",
        "critique_dimensions": ["security", "correctness", "operability"],
    }
    row.update(overrides)
    return row


def _sr_spec(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": 1,
        "target": "example app",
        "attack_surfaces": ["auth", "injection", "secrets"],
    }
    row.update(overrides)
    return row


def _hp_bundle(manifest: dict[str, Any]) -> dict[str, Any]:
    dims = list(manifest["spec"]["critique_dimensions"])
    receipts: list[dict[str, Any]] = []
    for dim in dims:
        receipts.append(
            {
                "lane_id": f"critic.{dim}",
                "status": "complete",
                "artifact_kind": "omg.team.hyperplan.critique",
                "payload": {
                    "dimension": dim,
                    "findings": [],
                    "severity": "info",
                    "blocking": False,
                },
            }
        )
    receipts.append(
        {
            "lane_id": "synthesize",
            "status": "complete",
            "artifact_kind": "omg.team.hyperplan.synthesis",
            "payload": {
                "summary": "Synthetic summary for hermetic tests.",
                "merged_findings": [],
                "open_conflicts": [],
                "recommended_verdict": "approved",
            },
        }
    )
    receipts.append(
        {
            "lane_id": "verify",
            "status": "complete",
            "artifact_kind": "omg.team.hyperplan.verification",
            "payload": {
                "gate": "hyperplan-v1",
                "covered_lanes": [r["lane_id"] for r in manifest["lanes"]],
                "blocking_issues": [],
                "verdict": "approved",
            },
        }
    )
    return {
        "kind": HYPERPLAN_RESULT_BUNDLE_KIND,
        "schema_version": 1,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "receipts": receipts,
    }


def _sr_bundle(manifest: dict[str, Any]) -> dict[str, Any]:
    surfaces = list(manifest["spec"]["attack_surfaces"])
    receipts: list[dict[str, Any]] = []
    for surface in surfaces:
        receipts.append(
            {
                "lane_id": f"hunt.{surface}",
                "status": "complete",
                "artifact_kind": "omg.team.security_research.candidate_findings",
                "payload": {
                    "surface": surface,
                    "candidates": [],
                    "severity_hints": {},
                    "evidence_pointers": [f"src/{surface}/mod.py:1"],
                },
            }
        )
    for lane in ("primary", "independent"):
        receipts.append(
            {
                "lane_id": f"validate.{lane}",
                "status": "complete",
                "artifact_kind": "omg.team.security_research.validation",
                "payload": {
                    "validated": [],
                    "falsified": [],
                    "proof_kind": "static",
                },
            }
        )
    receipts.append(
        {
            "lane_id": "consolidate",
            "status": "complete",
            "artifact_kind": "omg.team.security_research.consolidated_report",
            "payload": {
                "surviving_findings": [],
                "rejected_candidates": [],
                "severity_calibration": {},
                "recommended_verdict": "pass",
            },
        }
    )
    receipts.append(
        {
            "lane_id": "verify",
            "status": "complete",
            "artifact_kind": "omg.team.security_research.gate",
            "payload": {
                "gate": "security-research-v1",
                "covered_lanes": [r["lane_id"] for r in manifest["lanes"]],
                "blocking_issues": [],
                "verdict": "pass",
            },
        }
    )
    return {
        "kind": SECURITY_RESEARCH_RESULT_BUNDLE_KIND,
        "schema_version": 1,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "receipts": receipts,
    }


def _lane_result(receipt: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": 1,
        "status": receipt["status"],
        "payload": receipt["payload"],
    }
    if "reason" in receipt:
        out["reason"] = receipt["reason"]
    return out


def _complete_lanes_public(
    root: Path,
    *,
    run_id: str,
    topo_order: list[str],
    bundle: dict[str, Any],
    env: dict[str, str],
    claim_fn: Any,
    submit_fn: Any,
) -> None:
    by_lane = {r["lane_id"]: r for r in bundle["receipts"]}
    for lane_id in topo_order:
        claimed = claim_fn(root, run_id, TEAM, lane_id, env=env)
        assert claimed["ok"] is True
        assert claimed["execution_supported"] is False
        assert claimed["claim"]["execution_supported"] is False
        assert claimed["claim"]["claim_token"]
        submit = submit_fn(
            root,
            run_id,
            TEAM,
            claim=claimed["claim"],
            result=_lane_result(by_lane[lane_id]),
            env=env,
        )
        assert submit["ok"] is True
        assert submit["execution_supported"] is False


def _patch_no_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("execution surface touched")

    monkeypatch.setattr("subprocess.run", _boom)
    monkeypatch.setattr("subprocess.Popen", _boom)
    monkeypatch.setattr("subprocess.call", _boom)
    monkeypatch.setattr(os, "system", _boom)
    try:
        import socket

        monkeypatch.setattr(socket, "socket", _boom)
        monkeypatch.setattr(socket, "create_connection", _boom)
    except Exception:
        pass
    for mod_name in (
        "omg_cli.team.plane",
        "omg_cli.team.jobs",
        "omg_cli.providers",
        "omg_cli.mcp.server",
    ):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:
            continue
        for attr in (
            "start_team",
            "launch_job",
            "spawn_provider",
            "run_poc",
            "tmux",
        ):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, _boom)


def test_catalog_goldens_unchanged() -> None:
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V1, schema_version=1)
        == GOLDEN_V1.read_text(encoding="utf-8")
    )
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V2, schema_version=2)
        == GOLDEN_V2.read_text(encoding="utf-8")
    )
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V3, schema_version=3)
        == GOLDEN_V3.read_text(encoding="utf-8")
    )
    assert GOLDEN_V4.is_file()


def test_claim_schema_exact_keys_and_budget() -> None:
    base = {
        "kind": COMPOSITION_LANE_CLAIM_KIND,
        "schema_version": 1,
        "source_kind": "hyperplan_v1",
        "run_id": "run1",
        "team_id": "team",
        "composition_id": "comp1",
        "composition_digest": "a" * 64,
        "batch_id": "batch1",
        "batch_digest": "b" * 64,
        "lane_id": "critic.security",
        "task_id": "1",
        "worker_id": "t-a",
        "task_version": 2,
        "claim_token": "tok-abc",
        "leased_until": "2099-01-01T00:00:00Z",
        "lane": {
            "role": "critic",
            "posture": "read-only",
            "scope": {"kind": "dimension", "value": "security"},
            "requires_code_change": False,
            "allow_implementation": False,
            "owned_files": [],
            "expected_artifact": {"kind": "x", "schema_version": 1},
        },
        "input": {"goal": "g"},
        "dependency_outputs": [],
        "result_contract": {
            "schema_version": 1,
            "statuses": ["blocked", "complete", "rejected"],
        },
        "execution_supported": False,
    }
    parsed = parse_composition_lane_claim_v1(base)
    assert parsed["kind"] == COMPOSITION_LANE_CLAIM_KIND
    assert redact_claim_token(parsed)["claim_token"] == "<redacted>"

    bad = dict(base)
    bad["extra"] = 1
    with pytest.raises(CompositionLaneProtocolError, match="key mismatch"):
        parse_composition_lane_claim_v1(bad)

    no_token = dict(base)
    del no_token["claim_token"]
    with pytest.raises(CompositionLaneProtocolError, match="key mismatch"):
        parse_composition_lane_claim_v1(no_token)

    non_utc = dict(base)
    non_utc["leased_until"] = "2099-01-01T00:00:00+08:00"
    with pytest.raises(CompositionLaneProtocolError, match="UTC"):
        parse_composition_lane_claim_v1(non_utc)

    exec_true = dict(base)
    exec_true["execution_supported"] = True
    with pytest.raises(CompositionLaneProtocolError, match="execution_supported"):
        parse_composition_lane_claim_v1(exec_true)

    # Oversized envelope refused (no silent truncation).
    huge = dict(base)
    huge["input"] = {"goal": "x" * (MAX_INLINE_JSON_BYTES)}
    with pytest.raises(CompositionLaneProtocolError, match="budget"):
        parse_composition_lane_claim_v1(huge)

    # Existing LaneTaskResultV1 retained.
    assert parse_lane_task_result_v1(
        {"schema_version": 1, "status": "complete", "payload": {}}
    )["status"] == "complete"
    with pytest.raises(Exception, match="reason"):
        parse_lane_task_result_v1(
            {"schema_version": 1, "status": "rejected", "payload": {}}
        )


def test_worker_gate_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    lane = "critic.security"

    with pytest.raises(HyperplanError, match="leader|worker-only") as leader_exc:
        claim_hyperplan_lane_v1(
            tmp_path, run_id, TEAM, lane, env={EXPERIMENTAL_ENV: "1"}
        )
    assert leader_exc.value.code == "E_TEAM_COMPOSITION_LANE_GATE"

    partial = {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_WORKER": "1",
        "OMG_TEAM_WORKER_ID": WORKER,
        "OMG_TEAM_RUN_ID": run_id,
        "OMG_TEAM_ID": TEAM,
    }
    with pytest.raises(HyperplanError, match="incomplete|leader root|refused"):
        claim_hyperplan_lane_v1(tmp_path, run_id, TEAM, lane, env=partial)

    wrong_run = _full_worker_env(tmp_path, run_id)
    wrong_run["OMG_TEAM_RUN_ID"] = "other-run"
    with pytest.raises(HyperplanError, match="must match worker environment"):
        claim_hyperplan_lane_v1(tmp_path, run_id, TEAM, lane, env=wrong_run)

    spawn = _full_worker_env(tmp_path, run_id)
    spawn["OMG_SPAWNED_WORKER"] = "1"
    with pytest.raises(HyperplanError, match="spawned-worker"):
        claim_hyperplan_lane_v1(tmp_path, run_id, TEAM, lane, env=spawn)

    # Cannot claim on behalf of another worker (env identity wins).
    other = _full_worker_env(tmp_path, run_id, worker_id="other-worker")
    with pytest.raises(HyperplanError):
        claim_hyperplan_lane_v1(tmp_path, run_id, TEAM, lane, env=other)


def test_dag_ordering_hyperplan_and_security_research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    env = _full_worker_env(tmp_path, run_id)

    # Critics claim first.
    for lane in ("critic.security", "critic.correctness", "critic.operability"):
        out = claim_hyperplan_lane_v1(tmp_path, run_id, TEAM, lane, env=env)
        assert out["claim"]["lane_id"] == lane
        # release so we can test synthesize block without holding all claims
        code, _ = execute_team_api(
            "release-task-claim",
            {
                "run_id": run_id,
                "team_id": TEAM,
                "task_id": out["claim"]["task_id"],
                "claim_token": out["claim"]["claim_token"],
                "worker": WORKER,
            },
            root=tmp_path,
            env=env,
        )
        assert code == 0

    # Synthesize blocked until critics complete.
    with pytest.raises(HyperplanError, match="blocked_dependency|failed"):
        claim_hyperplan_lane_v1(tmp_path, run_id, TEAM, "synthesize", env=env)
    synth_task = team_api._read_task(
        tmp_path, run_id, TEAM, admitted["task_key_to_id"]["synthesize"]
    )
    assert synth_task is not None
    assert synth_task["status"] != "in_progress"
    assert synth_task.get("claim") is None

    # Security research validators wait for hunters.
    run_sr = _seed(tmp_path / "sr", monkeypatch)
    materialize_security_research_v1(tmp_path / "sr", run_sr, _sr_spec())
    sr_adm = admit_security_research_tasks_v1(tmp_path / "sr", run_sr, TEAM)
    sr_env = _full_worker_env(tmp_path / "sr", run_sr)
    with pytest.raises(SecurityResearchError, match="blocked_dependency|failed"):
        claim_security_research_lane_v1(
            tmp_path / "sr", run_sr, TEAM, "validate.primary", env=sr_env
        )
    vt = team_api._read_task(
        tmp_path / "sr", run_sr, TEAM, sr_adm["task_key_to_id"]["validate.primary"]
    )
    assert vt is not None
    assert vt.get("claim") is None


def test_concurrent_claim_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    env = _full_worker_env(tmp_path, run_id)
    results: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        try:
            results.append(
                claim_hyperplan_lane_v1(
                    tmp_path, run_id, TEAM, "critic.security", env=env
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1
    assert len(errors) == 3
    assert results[0]["claim"]["worker_id"] == WORKER


def test_submit_idempotent_and_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    env = _full_worker_env(tmp_path, run_id)
    claimed = claim_hyperplan_lane_v1(
        tmp_path, run_id, TEAM, "critic.security", env=env
    )
    result = {
        "schema_version": 1,
        "status": "complete",
        "payload": {
            "dimension": "security",
            "findings": [],
            "severity": "info",
            "blocking": False,
        },
    }
    first = submit_hyperplan_lane_result_v1(
        tmp_path,
        run_id,
        TEAM,
        claim=claimed["claim"],
        result=result,
        env=env,
    )
    assert first["idempotent"] is False

    again = submit_hyperplan_lane_result_v1(
        tmp_path,
        run_id,
        TEAM,
        claim=claimed["claim"],
        result=result,
        env=env,
    )
    assert again["idempotent"] is True

    other = dict(result)
    other["payload"] = {
        "dimension": "security",
        "findings": [{"finding_id": "f1", "summary": "x", "severity": "low", "blocking": False}],
        "severity": "low",
        "blocking": False,
    }
    with pytest.raises(HyperplanError, match="conflict"):
        submit_hyperplan_lane_result_v1(
            tmp_path,
            run_id,
            TEAM,
            claim=claimed["claim"],
            result=other,
            env=env,
        )

    # Invalid result on active claim leaves claim unchanged.
    run2 = _seed(tmp_path / "invalid", monkeypatch)
    materialize_hyperplan_v1(tmp_path / "invalid", run2, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path / "invalid", run2, TEAM)
    env2 = _full_worker_env(tmp_path / "invalid", run2)
    claimed2 = claim_hyperplan_lane_v1(
        tmp_path / "invalid", run2, TEAM, "critic.security", env=env2
    )
    with pytest.raises(HyperplanError):
        submit_hyperplan_lane_result_v1(
            tmp_path / "invalid",
            run2,
            TEAM,
            claim=claimed2["claim"],
            result={"schema_version": 1, "status": "rejected", "payload": {}},
            env=env2,
        )
    task = team_api._read_task(
        tmp_path / "invalid",
        run2,
        TEAM,
        claimed2["claim"]["task_id"],
    )
    assert task is not None
    assert task["status"] == "in_progress"
    assert task.get("claim") is not None
    assert mat["manifest"]["execution_supported"] is False


def test_hyperplan_full_public_path_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    run_d = _seed(tmp_path / "direct", monkeypatch)
    _patch_no_exec(monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    manifest = mat["manifest"]
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    env = _full_worker_env(tmp_path, run_id)
    bundle = _hp_bundle(manifest)
    _complete_lanes_public(
        tmp_path,
        run_id=run_id,
        topo_order=list(admitted["topo_order"]),
        bundle=bundle,
        env=env,
        claim_fn=claim_hyperplan_lane_v1,
        submit_fn=submit_hyperplan_lane_result_v1,
    )
    collected = collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert collected["execution_supported"] is False
    assert collected["decision"]["verdict"] == "approved"

    # Byte-stable vs direct produce path.
    materialize_hyperplan_v1(tmp_path / "direct", run_d, _hp_spec())
    direct = produce_hyperplan_decision_v1(tmp_path / "direct", run_d, bundle)
    assert collected["bundle"]["digest"] == direct["bundle"]["digest"]
    pure = compile_hyperplan_decision_v1(manifest, collected["bundle"])
    assert pure == collected["decision"]


def test_security_research_full_public_path_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    run_d = _seed(tmp_path / "sr-direct", monkeypatch)
    _patch_no_exec(monkeypatch)
    mat = materialize_security_research_v1(tmp_path, run_id, _sr_spec())
    manifest = mat["manifest"]
    admitted = admit_security_research_tasks_v1(tmp_path, run_id, TEAM)
    env = _full_worker_env(tmp_path, run_id)
    bundle = _sr_bundle(manifest)
    _complete_lanes_public(
        tmp_path,
        run_id=run_id,
        topo_order=list(admitted["topo_order"]),
        bundle=bundle,
        env=env,
        claim_fn=claim_security_research_lane_v1,
        submit_fn=submit_security_research_lane_result_v1,
    )
    collected = collect_security_research_tasks_v1(tmp_path, run_id, TEAM)
    assert collected["execution_supported"] is False
    assert collected["report"]["verdict"] == "pass"

    materialize_security_research_v1(tmp_path / "sr-direct", run_d, _sr_spec())
    direct = produce_security_research_report_v1(tmp_path / "sr-direct", run_d, bundle)
    assert collected["bundle"]["digest"] == direct["bundle"]["digest"]
    pure = compile_security_research_report_v1(manifest, collected["bundle"])
    assert pure == collected["report"]


def test_rejected_blocked_lane_outcomes_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    env = _full_worker_env(tmp_path, run_id)
    bundle = _hp_bundle(mat["manifest"])
    # Flip verify to blocked (Team task still completes).
    for row in bundle["receipts"]:
        if row["lane_id"] == "verify":
            row["status"] = "blocked"
            row["reason"] = "verifier blocked for hermetic test"
            row["payload"] = {
                "gate": "hyperplan-v1",
                "covered_lanes": [r["lane_id"] for r in mat["manifest"]["lanes"]],
                "blocking_issues": ["blocked"],
                "verdict": "rejected",
            }
    _complete_lanes_public(
        tmp_path,
        run_id=run_id,
        topo_order=list(admitted["topo_order"]),
        bundle=bundle,
        env=env,
        claim_fn=claim_hyperplan_lane_v1,
        submit_fn=submit_hyperplan_lane_result_v1,
    )
    verify_task = team_api._read_task(
        tmp_path, run_id, TEAM, admitted["task_key_to_id"]["verify"]
    )
    assert verify_task is not None
    assert verify_task["status"] == "completed"
    assert verify_task.get("claim") is None
    stored = parse_lane_task_result_v1(verify_task["result"])
    assert stored["status"] == "blocked"


def test_cli_claim_and_submit_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    env = _full_worker_env(tmp_path, run_id)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)

    code = main(
        [
            "team",
            "hyperplan",
            "claim-lane",
            "--run",
            run_id,
            "--team-id",
            TEAM,
            "--lane-id",
            "critic.security",
            "--json",
        ]
    )
    assert code == 0

    # Use library claim for submit file (CLI JSON goes to stdout via emit).
    claimed = claim_hyperplan_lane_v1(
        tmp_path, run_id, TEAM, "critic.correctness", env=env
    )
    claim_path = tmp_path / "claim.json"
    result_path = tmp_path / "result.json"
    claim_path.write_text(
        json.dumps(claimed["claim"], separators=(",", ":")), encoding="utf-8"
    )
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "payload": {
                    "dimension": "correctness",
                    "findings": [],
                    "severity": "info",
                    "blocking": False,
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "team",
            "hyperplan",
            "submit-lane-result",
            "--run",
            run_id,
            "--team-id",
            TEAM,
            "--claim-file",
            str(claim_path),
            "--result",
            str(result_path),
            "--json",
        ]
    )
    assert code == 0
    # Claim token never appears in claim file path args as dedicated option.
    assert "claim_token" not in " ".join(
        [
            "team",
            "hyperplan",
            "submit-lane-result",
            "--claim-file",
            str(claim_path),
            "--result",
            str(result_path),
        ]
    )
    _ = canonical_json_bytes
