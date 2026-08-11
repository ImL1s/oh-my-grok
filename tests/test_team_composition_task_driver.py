"""Hermetic tests for Shared Composition Task Driver V1 (#69 PR12)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omg_cli.contracts.path_keys import exclusive_lock
from omg_cli.main import main
from omg_cli.team import api as team_api
from omg_cli.team import task_batch as tb
from omg_cli.team.api import execute_team_api
from omg_cli.team.compositions.hyperplan import (
    HYPERPLAN_RESULT_BUNDLE_KIND,
    HyperplanError,
    admit_hyperplan_tasks_v1,
    collect_hyperplan_tasks_v1,
    compile_hyperplan_decision_v1,
    compile_hyperplan_v1,
    hyperplan_decision_path,
    hyperplan_result_bundle_path,
    materialize_hyperplan_v1,
    produce_hyperplan_decision_v1,
)
from omg_cli.team.compositions.security_research import (
    SECURITY_RESEARCH_RESULT_BUNDLE_KIND,
    SecurityResearchError,
    admit_security_research_tasks_v1,
    collect_security_research_tasks_v1,
    compile_security_research_report_v1,
    compile_security_research_v1,
    materialize_security_research_v1,
    produce_security_research_report_v1,
    security_research_report_path,
    security_research_result_bundle_path,
)
from omg_cli.team.compositions.task_driver import (
    CompositionTaskDriverError,
    SOURCE_KIND_HYPERPLAN,
    SOURCE_KIND_SECURITY_RESEARCH,
    compile_composition_task_batch_v1,
    composition_batch_ids,
    parse_lane_task_result_v1,
)
from omg_cli.team.operation_catalog import (
    CATALOG_SCHEMA_VERSION,
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

TEAM = "team"  # must match start_team dry_run control-plane team_id
SEED_TASKS = [{"task_id": "t-a", "owned_files": ["a.py"]}]
WRONG_TEAM = "team-api"


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
    ):
        monkeypatch.delenv(key, raising=False)


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "composition task driver seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    return str(meta["run_id"])


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


def _complete_lane_tasks(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    mapping: dict[str, str],
    bundle: dict[str, Any],
) -> None:
    by_lane = {r["lane_id"]: r for r in bundle["receipts"]}
    for lane_id, task_id in mapping.items():
        receipt = by_lane[lane_id]
        lane_result: dict[str, Any] = {
            "schema_version": 1,
            "status": receipt["status"],
            "payload": receipt["payload"],
        }
        if "reason" in receipt:
            lane_result["reason"] = receipt["reason"]
        task = team_api._read_task(root, run_id, team_id, task_id)
        assert task is not None
        # Mirror transition-task-status: claim cleared, owner retained.
        team_api._write_task(
            root,
            run_id,
            team_id,
            {
                **task,
                "status": "completed",
                "owner": "w1",
                "claim": None,
                "result": json.dumps(lane_result, separators=(",", ":")),
                "completed_at": "2026-08-11T00:00:00Z",
                "version": int(task["version"]) + 1,
            },
        )


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


@pytest.fixture(autouse=True)
def _clear_crash_hook() -> None:
    tb._crash_hook = None
    yield
    tb._crash_hook = None


def test_catalog_v1_v4_goldens_unchanged() -> None:
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
    assert CATALOG_SCHEMA_VERSION == 4
    assert catalog_document_json() == GOLDEN_V4.read_text(encoding="utf-8")


def test_compile_min_max_hyperplan_and_security_research_stable() -> None:
    hp_min = compile_hyperplan_v1(_hp_spec())
    hp_max = compile_hyperplan_v1(
        _hp_spec(
            critique_dimensions=[
                "security",
                "correctness",
                "operability",
                "privacy",
                "reliability",
                "performance",
                "ux",
                "cost",
            ]
        )
    )
    assert hp_min["execution_supported"] is False
    assert hp_max["execution_supported"] is False
    assert hp_min["lane_count"] == 5
    assert hp_max["lane_count"] == 10

    batch_min = compile_composition_task_batch_v1(
        hp_min, run_id="run1", team_id=TEAM, source_kind=SOURCE_KIND_HYPERPLAN
    )
    batch_max = compile_composition_task_batch_v1(
        hp_max, run_id="run1", team_id=TEAM, source_kind=SOURCE_KIND_HYPERPLAN
    )
    assert batch_min["source"]["kind"] == SOURCE_KIND_HYPERPLAN
    assert batch_min["source"]["source_id"] == hp_min["composition_id"]
    assert batch_min["source"]["digest"] == hp_min["digest"]
    assert set(batch_min["topo_order"]) == {lane["lane_id"] for lane in hp_min["lanes"]}
    assert batch_min["topo_order"][-1] == "verify"
    assert all(t["requires_code_change"] is False for t in batch_min["tasks"])
    for task, lane in zip(
        sorted(batch_min["tasks"], key=lambda t: t["task_key"]),
        sorted(hp_min["lanes"], key=lambda lane: lane["lane_id"]),
        strict=True,
    ):
        assert task["task_key"] == lane["lane_id"]
        assert task["depends_on"] == lane["depends_on"]
        assert task["expected_artifact"] == lane["expected_artifact"]

    # Reordered equivalent dimensions → identical digests.
    hp_reordered = compile_hyperplan_v1(
        _hp_spec(critique_dimensions=["operability", "security", "correctness"])
    )
    assert hp_reordered["digest"] == hp_min["digest"]
    batch_re = compile_composition_task_batch_v1(
        hp_reordered, run_id="run1", team_id=TEAM, source_kind=SOURCE_KIND_HYPERPLAN
    )
    assert batch_re["digest"] == batch_min["digest"]
    assert batch_max["digest"] != batch_min["digest"]

    sr = compile_security_research_v1(_sr_spec())
    assert sr["execution_supported"] is False
    sr_batch = compile_composition_task_batch_v1(
        sr, run_id="run1", team_id=TEAM, source_kind=SOURCE_KIND_SECURITY_RESEARCH
    )
    assert sr_batch["source"]["kind"] == SOURCE_KIND_SECURITY_RESEARCH
    assert sr_batch["topo_order"][-1] == "verify"
    assert "hunt.auth" in sr_batch["topo_order"]
    bid, ikey = composition_batch_ids(
        SOURCE_KIND_SECURITY_RESEARCH, sr["composition_id"]
    )
    assert sr_batch["batch_id"] == bid
    assert sr_batch["idempotency_key"] == ikey


def test_admit_idempotent_conflict_and_crash_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    first = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert first["ok"] is True
    assert first["idempotent"] is False
    assert first["execution_supported"] is False
    assert first["state"] == "committed"
    mapping = dict(first["task_key_to_id"])
    assert set(mapping) == {
        "critic.security",
        "critic.correctness",
        "critic.operability",
        "synthesize",
        "verify",
    }

    second = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert second["idempotent"] is True
    assert second["task_key_to_id"] == mapping
    assert second["digest"] == first["digest"]

    # Digest conflict under same composition key: tamper description via crash
    # is not possible through admit (recompiles from manifest). Force via
    # direct batch admit with same idempotency key + different digest.
    manifest = compile_hyperplan_v1(_hp_spec())
    batch_id, idem = composition_batch_ids(
        SOURCE_KIND_HYPERPLAN, manifest["composition_id"]
    )
    conflict = {
        "schema_version": 1,
        "run_id": run_id,
        "team_id": TEAM,
        "batch_id": batch_id,
        "idempotency_key": idem,
        "source": {
            "kind": SOURCE_KIND_HYPERPLAN,
            "source_id": manifest["composition_id"],
            "digest": "b" * 64,
        },
        "tasks": [
            {
                "task_key": "solo",
                "subject": "solo",
                "description": "solo",
                "depends_on": [],
                "requires_code_change": False,
                "expected_artifact": {
                    "kind": "omg.team.test.artifact",
                    "schema_version": 1,
                    "required_fields": ["summary"],
                },
            }
        ],
    }
    with pytest.raises(tb.TaskBatchError, match="conflicts"):
        tb.admit_task_batch_v1(tmp_path, conflict)

    # Crash before commit → invisible; retry restores same mapping.
    run_id2 = _seed(tmp_path / "crash", monkeypatch)
    materialize_hyperplan_v1(tmp_path / "crash", run_id2, _hp_spec())

    def before_commit(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError("injected crash before commit")

    tb._crash_hook = before_commit
    with pytest.raises(RuntimeError, match="before commit"):
        admit_hyperplan_tasks_v1(tmp_path / "crash", run_id2, TEAM)

    code, listed = execute_team_api(
        "list-tasks",
        {"run_id": run_id2, "team_id": TEAM},
        root=tmp_path / "crash",
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 0
    # Seed task may exist; no batch-bound lane tasks visible.
    subjects = {t.get("subject") for t in listed["data"]["tasks"]}
    assert not any(str(s).startswith("lane critic.") for s in subjects)

    tb._crash_hook = None
    recovered = admit_hyperplan_tasks_v1(tmp_path / "crash", run_id2, TEAM)
    assert recovered["state"] == "committed"
    assert recovered["idempotent"] is False


def test_dag_claimability_hyperplan_and_security_research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    mapping = admitted["task_key_to_id"]

    # Register a worker via create-task.
    execute_team_api(
        "create-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "subject": "seed-worker",
            "description": "register worker",
            "workers": ["w1"],
        },
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )

    # Root critics claimable; synthesize blocked.
    for lane in ("critic.security", "critic.correctness", "critic.operability"):
        code, claim = execute_team_api(
            "claim-task",
            {
                "run_id": run_id,
                "team_id": TEAM,
                "task_id": mapping[lane],
                "worker": "w1",
            },
            root=tmp_path,
            env={EXPERIMENTAL_ENV: "1"},
        )
        assert code == 0
        assert claim["data"]["ok"] is True
        token = claim["data"]["claimToken"]
        code, tr = execute_team_api(
            "transition-task-status",
            {
                "run_id": run_id,
                "team_id": TEAM,
                "task_id": mapping[lane],
                "from": "in_progress",
                "to": "completed",
                "claim_token": token,
                "worker": "w1",
                "result": json.dumps(
                    {
                        "schema_version": 1,
                        "status": "complete",
                        "payload": {
                            "dimension": lane.split(".", 1)[1],
                            "findings": [],
                            "severity": "info",
                            "blocking": False,
                        },
                    }
                ),
            },
            root=tmp_path,
            env={EXPERIMENTAL_ENV: "1"},
        )
        assert code == 0
        assert tr["data"]["ok"] is True

    code, blocked = execute_team_api(
        "claim-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "task_id": mapping["synthesize"],
            "worker": "w1",
        },
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    # After all critics complete, synthesize should be claimable.
    assert code == 0
    assert blocked["data"]["ok"] is True

    # Fresh SR graph: dependent validate blocked until hunts complete.
    run_sr = _seed(tmp_path / "sr", monkeypatch)
    materialize_security_research_v1(tmp_path / "sr", run_sr, _sr_spec())
    sr_adm = admit_security_research_tasks_v1(tmp_path / "sr", run_sr, TEAM)
    sr_map = sr_adm["task_key_to_id"]
    execute_team_api(
        "create-task",
        {
            "run_id": run_sr,
            "team_id": TEAM,
            "subject": "seed-worker",
            "description": "register worker",
            "workers": ["w1"],
        },
        root=tmp_path / "sr",
        env={EXPERIMENTAL_ENV: "1"},
    )
    code, dep = execute_team_api(
        "claim-task",
        {
            "run_id": run_sr,
            "team_id": TEAM,
            "task_id": sr_map["validate.primary"],
            "worker": "w1",
        },
        root=tmp_path / "sr",
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 1
    assert dep["ok"] is False
    assert dep["error"]["details"]["error"] == "blocked_dependency"

    code, root_ok = execute_team_api(
        "claim-task",
        {
            "run_id": run_sr,
            "team_id": TEAM,
            "task_id": sr_map["hunt.auth"],
            "worker": "w1",
        },
        root=tmp_path / "sr",
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 0
    assert root_ok["data"]["ok"] is True


def test_collection_parity_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    manifest = mat["manifest"]
    bundle = _hp_bundle(manifest)
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    _complete_lane_tasks(
        tmp_path,
        run_id=run_id,
        team_id=TEAM,
        mapping=admitted["task_key_to_id"],
        bundle=bundle,
    )
    collected = collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert collected["ok"] is True
    assert collected["execution_supported"] is False
    assert collected["decision"]["verdict"] == "approved"

    # Direct produce on a sibling run yields the same decision core.
    run_b = _seed(tmp_path / "direct", monkeypatch)
    materialize_hyperplan_v1(tmp_path / "direct", run_b, _hp_spec())
    direct = produce_hyperplan_decision_v1(tmp_path / "direct", run_b, bundle)
    assert direct["decision"]["verdict"] == collected["decision"]["verdict"]
    assert (
        direct["decision"]["source_artifact_digests"]["composition"]
        == collected["decision"]["source_artifact_digests"]["composition"]
    )
    assert direct["bundle"]["digest"] == collected["bundle"]["digest"]
    # Pure compile matches collected decision (normalized).
    pure = compile_hyperplan_decision_v1(manifest, collected["bundle"])
    assert pure == collected["decision"]

    again = collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert again["idempotent"] is True
    assert again["decision"] == collected["decision"]

    # Security research parity.
    run_sr = _seed(tmp_path / "sr", monkeypatch)
    mat_sr = materialize_security_research_v1(tmp_path / "sr", run_sr, _sr_spec())
    sr_manifest = mat_sr["manifest"]
    sr_bundle = _sr_bundle(sr_manifest)
    sr_adm = admit_security_research_tasks_v1(tmp_path / "sr", run_sr, TEAM)
    _complete_lane_tasks(
        tmp_path / "sr",
        run_id=run_sr,
        team_id=TEAM,
        mapping=sr_adm["task_key_to_id"],
        bundle=sr_bundle,
    )
    sr_collected = collect_security_research_tasks_v1(tmp_path / "sr", run_sr, TEAM)
    assert sr_collected["report"]["verdict"] == "pass"
    run_sr_d = _seed(tmp_path / "sr-direct", monkeypatch)
    materialize_security_research_v1(tmp_path / "sr-direct", run_sr_d, _sr_spec())
    sr_direct = produce_security_research_report_v1(
        tmp_path / "sr-direct", run_sr_d, sr_bundle
    )
    assert sr_direct["bundle"]["digest"] == sr_collected["bundle"]["digest"]
    pure_sr = compile_security_research_report_v1(sr_manifest, sr_collected["bundle"])
    assert pure_sr == sr_collected["report"]


def test_fail_closed_matrix_leaves_no_authoritative_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    manifest = mat["manifest"]
    bundle = _hp_bundle(manifest)
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    mapping = admitted["task_key_to_id"]

    def decision_exists() -> bool:
        return hyperplan_decision_path(tmp_path, run_id).exists()

    # Pending task → refuse.
    with pytest.raises(HyperplanError, match="must be completed"):
        collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert not decision_exists()

    _complete_lane_tasks(
        tmp_path, run_id=run_id, team_id=TEAM, mapping=mapping, bundle=bundle
    )
    # Corrupt one result with forbidden key.
    tid = mapping["verify"]
    task = team_api._read_task(tmp_path, run_id, TEAM, tid)
    assert task is not None
    team_api._write_task(
        tmp_path,
        run_id,
        TEAM,
        {
            **task,
            "result": json.dumps(
                {
                    "schema_version": 1,
                    "status": "complete",
                    "payload": bundle["receipts"][-1]["payload"],
                    "lane_id": "verify",
                }
            ),
        },
    )
    with pytest.raises(HyperplanError, match="forbids|lane_id"):
        collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert not decision_exists()

    # Restore good verify result but leave claim attached.
    team_api._write_task(
        tmp_path,
        run_id,
        TEAM,
        {
            **task,
            "status": "completed",
            "owner": "w1",
            "claim": {"owner": "w1", "token": "tok", "leased_until": "2099-01-01T00:00:00Z"},
            "result": json.dumps(
                {
                    "schema_version": 1,
                    "status": "complete",
                    "payload": bundle["receipts"][-1]["payload"],
                }
            ),
        },
    )
    with pytest.raises(HyperplanError, match="still claimed"):
        collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert not decision_exists()

    # Honest transition shape: owner retained, claim cleared → collect ok.
    _complete_lane_tasks(
        tmp_path, run_id=run_id, team_id=TEAM, mapping=mapping, bundle=bundle
    )
    for task_id in mapping.values():
        row = team_api._read_task(tmp_path, run_id, TEAM, task_id)
        assert row is not None
        assert row["owner"] == "w1"
        assert row["claim"] is None
        assert row["status"] == "completed"
    collected_owned = collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert collected_owned["ok"] is True
    assert collected_owned["decision"]["verdict"] == "approved"
    assert decision_exists()

    # Restore clean completions then conflict with manual produce via mutated
    # payload against an already-produced decision (same digests → idempotent
    # unless we mutate first on a fresh run). Use sibling run for conflict.
    run_conflict = _seed(tmp_path / "conflict-owner", monkeypatch)
    mat_c = materialize_hyperplan_v1(tmp_path / "conflict-owner", run_conflict, _hp_spec())
    bundle_c = _hp_bundle(mat_c["manifest"])
    adm_c = admit_hyperplan_tasks_v1(tmp_path / "conflict-owner", run_conflict, TEAM)
    mapping_c = adm_c["task_key_to_id"]
    _complete_lane_tasks(
        tmp_path / "conflict-owner",
        run_id=run_conflict,
        team_id=TEAM,
        mapping=mapping_c,
        bundle=bundle_c,
    )
    produce_hyperplan_decision_v1(tmp_path / "conflict-owner", run_conflict, bundle_c)
    tid2 = mapping_c["synthesize"]
    t2 = team_api._read_task(tmp_path / "conflict-owner", run_conflict, TEAM, tid2)
    assert t2 is not None
    mutated = {
        "schema_version": 1,
        "status": "complete",
        "payload": {
            "summary": "Different summary to force conflict.",
            "merged_findings": [],
            "open_conflicts": [],
            "recommended_verdict": "approved",
        },
    }
    team_api._write_task(
        tmp_path / "conflict-owner",
        run_conflict,
        TEAM,
        {**t2, "result": json.dumps(mutated)},
    )
    with pytest.raises(HyperplanError, match="conflict"):
        collect_hyperplan_tasks_v1(tmp_path / "conflict-owner", run_conflict, TEAM)

    # Parser unit matrix.
    with pytest.raises(CompositionTaskDriverError):
        parse_lane_task_result_v1("{")
    with pytest.raises(CompositionTaskDriverError, match="forbids"):
        parse_lane_task_result_v1(
            {"schema_version": 1, "status": "complete", "payload": {}, "digest": "a" * 64}
        )
    with pytest.raises(CompositionTaskDriverError, match="reason"):
        parse_lane_task_result_v1(
            {"schema_version": 1, "status": "blocked", "payload": {}}
        )
    with pytest.raises(CompositionTaskDriverError, match="payload"):
        parse_lane_task_result_v1(
            {"schema_version": 1, "status": "complete", "payload": "x"}
        )


def test_collect_allows_retained_owner_but_refuses_active_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """transition-task-status keeps owner; collect must only require claim-free."""
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    bundle = _hp_bundle(mat["manifest"])
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    mapping = admitted["task_key_to_id"]
    _complete_lane_tasks(
        tmp_path, run_id=run_id, team_id=TEAM, mapping=mapping, bundle=bundle
    )
    for task_id in mapping.values():
        row = team_api._read_task(tmp_path, run_id, TEAM, task_id)
        assert row is not None
        assert row["owner"] == "w1"
        assert row["claim"] is None
    out = collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert out["ok"] is True
    assert out["decision"]["verdict"] == "approved"

    run2 = _seed(tmp_path / "still-claimed", monkeypatch)
    mat2 = materialize_hyperplan_v1(tmp_path / "still-claimed", run2, _hp_spec())
    bundle2 = _hp_bundle(mat2["manifest"])
    adm2 = admit_hyperplan_tasks_v1(tmp_path / "still-claimed", run2, TEAM)
    mapping2 = adm2["task_key_to_id"]
    _complete_lane_tasks(
        tmp_path / "still-claimed",
        run_id=run2,
        team_id=TEAM,
        mapping=mapping2,
        bundle=bundle2,
    )
    tid = mapping2["verify"]
    task = team_api._read_task(tmp_path / "still-claimed", run2, TEAM, tid)
    assert task is not None
    team_api._write_task(
        tmp_path / "still-claimed",
        run2,
        TEAM,
        {
            **task,
            "claim": {
                "owner": "w1",
                "token": "tok",
                "leased_until": "2099-01-01T00:00:00Z",
            },
        },
    )
    with pytest.raises(HyperplanError, match="still claimed"):
        collect_hyperplan_tasks_v1(tmp_path / "still-claimed", run2, TEAM)
    assert not hyperplan_decision_path(tmp_path / "still-claimed", run2).exists()


def test_collection_blocks_transition_and_concurrent_collectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    manifest = mat["manifest"]
    bundle = _hp_bundle(manifest)
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    mapping = admitted["task_key_to_id"]
    _complete_lane_tasks(
        tmp_path, run_id=run_id, team_id=TEAM, mapping=mapping, bundle=bundle
    )

    # Hold one mapped task lock while attempting transition → blocks.
    hold = threading.Event()
    released = threading.Event()
    results: list[str] = []

    path = team_api._task_path(tmp_path, run_id, TEAM, mapping["verify"])

    def holder() -> None:
        with exclusive_lock(path.with_suffix(".lock")):
            hold.set()
            released.wait(timeout=5)
            time.sleep(0.05)

    def collector() -> None:
        hold.wait(timeout=5)
        try:
            collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
            results.append("ok")
        except Exception as exc:  # noqa: BLE001
            results.append(f"err:{exc}")
        finally:
            released.set()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=collector)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert results == ["ok"]
    assert hyperplan_decision_path(tmp_path, run_id).exists()

    # Concurrent collectors → one write + one idempotent.
    run2 = _seed(tmp_path / "conc", monkeypatch)
    mat2 = materialize_hyperplan_v1(tmp_path / "conc", run2, _hp_spec())
    b2 = _hp_bundle(mat2["manifest"])
    adm2 = admit_hyperplan_tasks_v1(tmp_path / "conc", run2, TEAM)
    _complete_lane_tasks(
        tmp_path / "conc",
        run_id=run2,
        team_id=TEAM,
        mapping=adm2["task_key_to_id"],
        bundle=b2,
    )
    outs: list[dict[str, Any]] = []
    errs: list[BaseException] = []

    def worker() -> None:
        try:
            outs.append(collect_hyperplan_tasks_v1(tmp_path / "conc", run2, TEAM))
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    assert len(outs) == 4
    assert sum(1 for o in outs if o.get("idempotent")) >= 1
    assert sum(1 for o in outs if not o.get("idempotent")) >= 1
    digests = {o["bundle"]["digest"] for o in outs}
    assert len(digests) == 1


def test_hermetic_no_exec_surfaces_on_admit_collect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    _patch_no_exec(monkeypatch)
    # Re-enable start_team patch would break — admit/collect must not call it.
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    bundle = _hp_bundle(compile_hyperplan_v1(_hp_spec()))
    # Need unpatched write for completing tasks; temporarily restore write path
    # by completing before patch? Already admitted under patch. Complete with
    # direct _write_task (no subprocess).
    monkeypatch.undo()
    _env_on(monkeypatch)
    _complete_lane_tasks(
        tmp_path,
        run_id=run_id,
        team_id=TEAM,
        mapping=admitted["task_key_to_id"],
        bundle=bundle,
    )
    _patch_no_exec(monkeypatch)
    collected = collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert collected["ok"] is True
    assert collected["execution_supported"] is False


def test_cli_admit_and_collect_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    spec = tmp_path / "hp.json"
    spec.write_text(json.dumps(_hp_spec()), encoding="utf-8")
    assert main(["team", "hyperplan", "materialize", "--spec", str(spec), "--run", run_id, "--json"]) == 0
    capsys.readouterr()
    rc = main(
        [
            "team",
            "hyperplan",
            "admit-tasks",
            "--run",
            run_id,
            "--team-id",
            TEAM,
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    body = out.get("data") or out
    assert body.get("execution_supported") is False
    mapping = body["task_key_to_id"]
    manifest = compile_hyperplan_v1(_hp_spec())
    bundle = _hp_bundle(manifest)
    _complete_lane_tasks(
        tmp_path, run_id=run_id, team_id=TEAM, mapping=mapping, bundle=bundle
    )
    rc = main(
        [
            "team",
            "hyperplan",
            "collect-tasks",
            "--run",
            run_id,
            "--team-id",
            TEAM,
            "--json",
        ]
    )
    assert rc == 0
    out2 = json.loads(capsys.readouterr().out)
    body2 = out2.get("data") or out2
    assert body2["decision"]["verdict"] == "approved"
    assert hyperplan_result_bundle_path(tmp_path, run_id).exists()

    # Security research CLI.
    run_sr = _seed(tmp_path / "sr-cli", monkeypatch)
    monkeypatch.chdir(tmp_path / "sr-cli")
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path / "sr-cli"))
    sr_spec = tmp_path / "sr-cli" / "sr.json"
    sr_spec.write_text(json.dumps(_sr_spec()), encoding="utf-8")
    assert (
        main(
            [
                "team",
                "security-research",
                "materialize",
                "--spec",
                str(sr_spec),
                "--run",
                run_sr,
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "team",
                "security-research",
                "admit-tasks",
                "--run",
                run_sr,
                "--team-id",
                TEAM,
                "--json",
            ]
        )
        == 0
    )
    sr_body = json.loads(capsys.readouterr().out)
    sr_data = sr_body.get("data") or sr_body
    assert sr_data["execution_supported"] is False
    sr_manifest = compile_security_research_v1(_sr_spec())
    sr_bundle = _sr_bundle(sr_manifest)
    _complete_lane_tasks(
        tmp_path / "sr-cli",
        run_id=run_sr,
        team_id=TEAM,
        mapping=sr_data["task_key_to_id"],
        bundle=sr_bundle,
    )
    assert (
        main(
            [
                "team",
                "security-research",
                "collect-tasks",
                "--run",
                run_sr,
                "--team-id",
                TEAM,
                "--json",
            ]
        )
        == 0
    )
    sr_out = json.loads(capsys.readouterr().out)
    sr_col = sr_out.get("data") or sr_out
    assert sr_col["report"]["verdict"] == "pass"
    assert security_research_report_path(tmp_path / "sr-cli", run_sr).exists()
    assert security_research_result_bundle_path(tmp_path / "sr-cli", run_sr).exists()


def test_uncommitted_batch_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())

    def before_commit(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError("stop before commit")

    tb._crash_hook = before_commit
    with pytest.raises(RuntimeError):
        admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    tb._crash_hook = None
    with pytest.raises(HyperplanError, match="committed|missing"):
        collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert not hyperplan_decision_path(tmp_path, run_id).exists()


def _worker_env(run_id: str) -> dict[str, str]:
    return {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_WORKER": "1",
        "OMG_TEAM_WORKER_ID": "w1",
        "OMG_TEAM_RUN_ID": run_id,
        "OMG_TEAM_ID": TEAM,
    }


def test_worker_env_cannot_admit_or_collect_composition_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leader-only gate: workers must not bypass bulk-create-tasks denial."""
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    manifest = mat["manifest"]
    worker_env = _worker_env(run_id)

    with pytest.raises(HyperplanError, match="cannot admit/collect|leader-only") as exc_info:
        admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM, env=worker_env)
    assert exc_info.value.code == "E_TEAM_COMPOSITION_TASK_GATE"

    _, idem = composition_batch_ids(SOURCE_KIND_HYPERPLAN, manifest["composition_id"])
    record = tb._load_batch_record(
        tb.batch_record_path(tmp_path, run_id, TEAM, idem),
        run_id=run_id,
        team_id=TEAM,
    )
    assert record is None

    # Leader admits, then worker collect is refused (no decision written).
    leader = admit_hyperplan_tasks_v1(
        tmp_path, run_id, TEAM, env={EXPERIMENTAL_ENV: "1"}
    )
    assert leader["ok"] is True
    bundle = _hp_bundle(manifest)
    _complete_lane_tasks(
        tmp_path,
        run_id=run_id,
        team_id=TEAM,
        mapping=leader["task_key_to_id"],
        bundle=bundle,
    )
    with pytest.raises(HyperplanError, match="cannot admit/collect|leader-only") as col_exc:
        collect_hyperplan_tasks_v1(tmp_path, run_id, TEAM, env=worker_env)
    assert col_exc.value.code == "E_TEAM_COMPOSITION_TASK_GATE"
    assert not hyperplan_decision_path(tmp_path, run_id).exists()

    # Security research worker admit refused similarly.
    run_sr = _seed(tmp_path / "sr-gate", monkeypatch)
    materialize_security_research_v1(tmp_path / "sr-gate", run_sr, _sr_spec())
    with pytest.raises(
        SecurityResearchError, match="cannot admit/collect|leader-only"
    ) as sr_exc:
        admit_security_research_tasks_v1(
            tmp_path / "sr-gate", run_sr, TEAM, env=_worker_env(run_sr)
        )
    assert sr_exc.value.code == "E_TEAM_COMPOSITION_TASK_GATE"


def test_leader_env_still_admits_and_collects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    leader_env = {EXPERIMENTAL_ENV: "1"}
    admitted = admit_hyperplan_tasks_v1(
        tmp_path, run_id, TEAM, env=leader_env
    )
    assert admitted["ok"] is True
    assert admitted["execution_supported"] is False
    bundle = _hp_bundle(mat["manifest"])
    _complete_lane_tasks(
        tmp_path,
        run_id=run_id,
        team_id=TEAM,
        mapping=admitted["task_key_to_id"],
        bundle=bundle,
    )
    collected = collect_hyperplan_tasks_v1(
        tmp_path, run_id, TEAM, env=leader_env
    )
    assert collected["ok"] is True
    assert collected["decision"]["verdict"] == "approved"


def test_admit_seeds_api_workers_and_claim_task_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control-plane task_ids must be registered so pane claim-task works."""
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admitted = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    cfg = team_api._load_config(tmp_path, run_id, TEAM)
    assert cfg is not None
    worker_names = {row["name"] for row in cfg["workers"]}
    assert "t-a" in worker_names
    assert cfg["team_id"] == TEAM

    root_key = admitted["topo_order"][0]
    assert root_key.startswith("critic.")
    # Real pane env: worker marker + identity + control-plane team id.
    pane_env = {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_WORKER": "1",
        "OMG_TEAM_WORKER_ID": "t-a",
        "OMG_TEAM_ID": TEAM,
        "OMG_TEAM_RUN_ID": run_id,
    }
    code, claim = execute_team_api(
        "claim-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "task_id": admitted["task_key_to_id"][root_key],
            "worker": "t-a",
        },
        root=tmp_path,
        env=pane_env,
    )
    assert code == 0
    assert claim["data"]["ok"] is True
    assert claim["data"]["task"]["owner"] == "t-a"
    assert claim["data"]["claimToken"]

    # Idempotent re-admit repairs/preserves the seeded registry.
    again = admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    assert again["idempotent"] is True
    cfg2 = team_api._load_config(tmp_path, run_id, TEAM)
    assert cfg2 is not None
    assert "t-a" in {row["name"] for row in cfg2["workers"]}


def test_admit_refuses_team_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    with pytest.raises(HyperplanError, match="team_id mismatch") as exc_info:
        admit_hyperplan_tasks_v1(tmp_path, run_id, WRONG_TEAM)
    assert exc_info.value.code == "E_TEAM_COMPOSITION_TASK_TEAM_ID"

    # No batch under the wrong team id.
    _, idem = composition_batch_ids(
        SOURCE_KIND_HYPERPLAN, mat["manifest"]["composition_id"]
    )
    wrong_record = tb._load_batch_record(
        tb.batch_record_path(tmp_path, run_id, WRONG_TEAM, idem),
        run_id=run_id,
        team_id=WRONG_TEAM,
    )
    assert wrong_record is None
    # And none under the correct id either (admit never ran successfully).
    good_record = tb._load_batch_record(
        tb.batch_record_path(tmp_path, run_id, TEAM, idem),
        run_id=run_id,
        team_id=TEAM,
    )
    assert good_record is None

    with pytest.raises(HyperplanError, match="team_id mismatch") as col_exc:
        collect_hyperplan_tasks_v1(tmp_path, run_id, WRONG_TEAM)
    assert col_exc.value.code == "E_TEAM_COMPOSITION_TASK_TEAM_ID"
