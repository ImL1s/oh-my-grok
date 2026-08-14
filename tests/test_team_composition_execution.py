"""Hermetic tests for Composition Execution V1 (#69 PR14)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.evidence import CLI_WRITER
from omg_cli.main import main
from omg_cli.state import write_status
from omg_cli.team.compositions.execution import (
    COMPOSITION_EXECUTION_KIND,
    CompositionExecutionError,
    compile_composition_execution_v1,
    execute_composition_tasks_v1,
    fixture_pane_id,
    parse_composition_execution_v1,
    require_fixture_executor,
)
from omg_cli.team.compositions.hyperplan import (
    HYPERPLAN_RESULT_BUNDLE_KIND,
    HyperplanError,
    admit_hyperplan_tasks_v1,
    compile_hyperplan_v1,
    execute_hyperplan_tasks_v1,
    load_hyperplan_manifest,
    materialize_hyperplan_v1,
)
from omg_cli.team.compositions.security_research import (
    SECURITY_RESEARCH_RESULT_BUNDLE_KIND,
    admit_security_research_tasks_v1,
    compile_security_research_v1,
    execute_security_research_tasks_v1,
    load_security_research_manifest,
    materialize_security_research_v1,
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

TEAM = "team"
SEED_TASKS = [{"task_id": "t-a", "owned_files": ["a.py"]}]
WORKER = "t-a"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64

_POSIX = pytest.mark.skipif(
    os.name != "posix",
    reason="managed-store exclusive_lock / atomic_write requires POSIX",
)


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


def _seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_topology: str | None = None,
) -> str:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    kwargs: dict[str, Any] = {
        "root": tmp_path,
        "dry_run": True,
        "env": {EXPERIMENTAL_ENV: "1"},
        "check_binary": False,
    }
    if worker_topology is not None:
        kwargs["worker_topology"] = worker_topology
    meta = start_team(
        "composition execution seed",
        SEED_TASKS,
        **kwargs,
    )
    run_id = str(meta["run_id"])
    write_status(tmp_path, run_id, "running")
    return run_id


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


def _patch_no_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("execution surface touched")

    monkeypatch.setattr("subprocess.Popen", _boom)
    try:
        import socket

        monkeypatch.setattr(socket, "socket", _boom)
        monkeypatch.setattr(socket, "create_connection", _boom)
    except Exception:
        pass
    for mod_name in (
        "omg_cli.team.jobs",
        "omg_cli.providers",
        "omg_cli.mcp.server",
    ):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:
            continue
        for attr in ("launch_job", "spawn_provider", "run_poc", "tmux"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, _boom)


def _evidence_row(
    *,
    lane_id: str = "critic.security",
    worker_id: str = WORKER,
    run_id: str = "run1",
    result_digest: str = DIGEST_A,
    claim_digest: str = DIGEST_B,
    task_id: str = "1",
) -> dict[str, Any]:
    return {
        "lane_id": lane_id,
        "task_id": task_id,
        "worker_id": worker_id,
        "run_id": run_id,
        "pane_id": fixture_pane_id(worker_id),
        "result_digest": result_digest,
        "claim_digest": claim_digest,
    }


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


def test_require_fixture_executor_refuses_live_providers() -> None:
    assert require_fixture_executor("fixture") == "fixture"
    assert require_fixture_executor("FIXTURE") == "fixture"
    for name in ("grok", "agy", "antigravity", "cursor", "codex", "claude"):
        with pytest.raises(CompositionExecutionError, match="refused"):
            require_fixture_executor(name)
    with pytest.raises(CompositionExecutionError, match="required"):
        require_fixture_executor("")
    with pytest.raises(CompositionExecutionError, match="unsupported"):
        require_fixture_executor("mystery")


def test_parse_rejects_forged_execution_supported_true() -> None:
    forged = {
        "kind": COMPOSITION_EXECUTION_KIND,
        "schema_version": 1,
        "source_kind": "hyperplan_v1",
        "run_id": "run1",
        "team_id": TEAM,
        "composition_id": "comp1",
        "composition_digest": DIGEST_A,
        "batch_id": "batch1",
        "batch_digest": DIGEST_B,
        "executor": "fixture",
        "execution_supported": True,
        "worker_evidence": [],
        "lane_result_digests": [],
        "collected_digest": DIGEST_C,
        "limitations": [
            "executor=fixture",
            "no_live_providers",
            "no_poc_execution",
            "compile_execution_supported=false",
        ],
        "writer": CLI_WRITER,
        "digest": DIGEST_A,
    }
    with pytest.raises(CompositionExecutionError, match="worker_evidence"):
        parse_composition_execution_v1(forged)

    flag_only = dict(forged)
    del flag_only["worker_evidence"]
    del flag_only["lane_result_digests"]
    with pytest.raises(CompositionExecutionError, match="key mismatch"):
        parse_composition_execution_v1(flag_only)


def test_parse_rejects_true_without_pane_evidence() -> None:
    row = _evidence_row()
    del row["pane_id"]
    with pytest.raises(CompositionExecutionError, match="key mismatch"):
        compile_composition_execution_v1(
            source_kind="hyperplan_v1",
            run_id="run1",
            team_id=TEAM,
            composition_id="comp1",
            composition_digest=DIGEST_A,
            batch_id="batch1",
            batch_digest=DIGEST_B,
            worker_evidence=[row],
            collected_digest=DIGEST_C,
        )


def test_parse_rejects_wrong_fixture_pane_id() -> None:
    row = _evidence_row()
    row["pane_id"] = "not-a-fixture"
    with pytest.raises(CompositionExecutionError, match="fx-"):
        compile_composition_execution_v1(
            source_kind="hyperplan_v1",
            run_id="run1",
            team_id=TEAM,
            composition_id="comp1",
            composition_digest=DIGEST_A,
            batch_id="batch1",
            batch_digest=DIGEST_B,
            worker_evidence=[row],
            collected_digest=DIGEST_C,
        )


def test_compile_stamps_true_only_with_complete_evidence() -> None:
    row = _evidence_row()
    doc = compile_composition_execution_v1(
        source_kind="hyperplan_v1",
        run_id="run1",
        team_id=TEAM,
        composition_id="comp1",
        composition_digest=DIGEST_A,
        batch_id="batch1",
        batch_digest=DIGEST_B,
        worker_evidence=[row],
        collected_digest=DIGEST_C,
        executor="fixture",
    )
    assert doc["kind"] == COMPOSITION_EXECUTION_KIND
    assert doc["execution_supported"] is True
    assert doc["executor"] == "fixture"
    assert doc["writer"] == CLI_WRITER
    assert doc["worker_evidence"][0]["pane_id"] == "fx-t-a"
    assert doc["worker_evidence"][0]["run_id"] == "run1"
    assert doc["lane_result_digests"][0]["digest"] == DIGEST_A
    parsed = parse_composition_execution_v1(doc)
    assert parsed["digest"] == doc["digest"]

    tampered = dict(doc)
    tampered["execution_supported"] = True
    tampered["digest"] = "0" * 64
    with pytest.raises(CompositionExecutionError, match="digest"):
        parse_composition_execution_v1(tampered)

    foreign = dict(doc)
    foreign["writer"] = "model"
    core = {k: v for k, v in foreign.items() if k != "digest"}
    foreign["digest"] = sha256_hex(canonical_json_bytes(core))
    with pytest.raises(CompositionExecutionError, match="foreign writer"):
        parse_composition_execution_v1(foreign)


def test_compile_rejects_empty_evidence() -> None:
    with pytest.raises(CompositionExecutionError, match="worker_evidence"):
        compile_composition_execution_v1(
            source_kind="hyperplan_v1",
            run_id="run1",
            team_id=TEAM,
            composition_id="comp1",
            composition_digest=DIGEST_A,
            batch_id="batch1",
            batch_digest=DIGEST_B,
            worker_evidence=[],
            collected_digest=DIGEST_C,
        )


def test_compile_hyperplan_stays_execution_supported_false() -> None:
    manifest = compile_hyperplan_v1(_hp_spec())
    assert manifest["execution_supported"] is False
    sr = compile_security_research_v1(_sr_spec())
    assert sr["execution_supported"] is False
    assert sr["safe_poc_policy"]["execution_supported"] is False


@_POSIX
def test_hyperplan_fixture_execute_collects_and_stamps_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    _patch_no_exec(monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    manifest = mat["manifest"]
    assert manifest["execution_supported"] is False
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    bundle = _hp_bundle(manifest)
    out = execute_hyperplan_tasks_v1(
        tmp_path,
        run_id,
        TEAM,
        executor="fixture",
        bundle=bundle,
    )
    assert out["ok"] is True
    assert out["idempotent"] is False
    assert out["execution_supported"] is True
    assert out["manifest_execution_supported"] is False
    execution = out["execution"]
    assert execution["kind"] == COMPOSITION_EXECUTION_KIND
    assert execution["execution_supported"] is True
    assert execution["executor"] == "fixture"
    lanes = [row["lane_id"] for row in execution["worker_evidence"]]
    assert set(lanes) == {lane["lane_id"] for lane in manifest["lanes"]}
    for row in execution["worker_evidence"]:
        assert row["run_id"] == run_id
        assert row["pane_id"] == fixture_pane_id(WORKER)
        assert row["worker_id"] == WORKER
        assert len(row["result_digest"]) == 64
    collected = out["collected"]
    assert collected["execution_supported"] is False
    assert collected["decision"]["verdict"] == "approved"
    loaded = load_hyperplan_manifest(tmp_path, run_id)
    assert loaded["execution_supported"] is False
    status = json.loads(
        (tmp_path / ".omg" / "state" / "runs" / run_id / "status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status.get("verified") is not True
    assert not status.get("passes")

    again = execute_hyperplan_tasks_v1(
        tmp_path,
        run_id,
        TEAM,
        executor="fixture",
        bundle=bundle,
    )
    assert again["idempotent"] is True
    assert again["execution"]["digest"] == execution["digest"]


@_POSIX
def test_security_research_fixture_execute_no_poc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    _patch_no_exec(monkeypatch)
    mat = materialize_security_research_v1(tmp_path, run_id, _sr_spec())
    manifest = mat["manifest"]
    assert manifest["execution_supported"] is False
    assert manifest["safe_poc_policy"]["execution_supported"] is False
    admit_security_research_tasks_v1(tmp_path, run_id, TEAM)
    out = execute_security_research_tasks_v1(
        tmp_path,
        run_id,
        TEAM,
        executor="fixture",
        bundle=_sr_bundle(manifest),
    )
    assert out["execution_supported"] is True
    assert out["manifest_execution_supported"] is False
    assert out["collected"]["execution_supported"] is False
    assert out["collected"]["report"]["verdict"] == "pass"
    loaded = load_security_research_manifest(tmp_path, run_id)
    assert loaded["execution_supported"] is False
    assert loaded["safe_poc_policy"]["execution_supported"] is False


@_POSIX
def test_execute_refuses_grok_before_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    with pytest.raises(HyperplanError, match="refused"):
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="grok",
            bundle=_hp_bundle(manifest),
        )


@_POSIX
def test_execute_refuses_job_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch, worker_topology="job")
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    with pytest.raises(HyperplanError, match="job-backed"):
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="fixture",
            bundle=_hp_bundle(manifest),
        )


@_POSIX
def test_execute_refuses_worker_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    env = {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_WORKER": "1",
        "OMG_TEAM_WORKER_ID": WORKER,
        "OMG_TEAM_RUN_ID": run_id,
        "OMG_TEAM_ID": TEAM,
        "OMG_TEAM_LEADER_ROOT": str(tmp_path.resolve()),
        "OMG_TEAM_STATE_ROOT": str((tmp_path / ".omg" / "state").resolve()),
        "OMG_TEAM_OWNER_TOKEN": "x",
        "OMG_PROJECT_ROOT": str(tmp_path.resolve()),
    }
    with pytest.raises(HyperplanError, match="worker"):
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="fixture",
            bundle=_hp_bundle(manifest),
            env=env,
        )


@_POSIX
def test_cli_hyperplan_execute_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    spec = tmp_path / "hp.json"
    spec.write_text(json.dumps(_hp_spec()), encoding="utf-8")
    assert (
        main(
            [
                "team",
                "hyperplan",
                "materialize",
                "--spec",
                str(spec),
                "--run",
                run_id,
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
                "hyperplan",
                "admit-tasks",
                "--run",
                run_id,
                "--team-id",
                TEAM,
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(_hp_bundle(manifest)), encoding="utf-8")
    rc = main(
        [
            "team",
            "hyperplan",
            "execute",
            "--run",
            run_id,
            "--team-id",
            TEAM,
            "--executor",
            "fixture",
            "--input",
            str(bundle_path),
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    body = out.get("data") or out
    assert body.get("execution_supported") is True
    assert body.get("manifest_execution_supported") is False
    assert body["execution"]["kind"] == COMPOSITION_EXECUTION_KIND


@_POSIX
def test_cli_execute_refuses_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    spec = tmp_path / "hp.json"
    spec.write_text(json.dumps(_hp_spec()), encoding="utf-8")
    main(
        [
            "team",
            "hyperplan",
            "materialize",
            "--spec",
            str(spec),
            "--run",
            run_id,
            "--json",
        ]
    )
    capsys.readouterr()
    main(
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
    capsys.readouterr()
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(_hp_bundle(manifest)), encoding="utf-8")
    rc = main(
        [
            "team",
            "hyperplan",
            "execute",
            "--run",
            run_id,
            "--team-id",
            TEAM,
            "--executor",
            "cursor",
            "--input",
            str(bundle_path),
            "--json",
        ]
    )
    assert rc == 2
    err = capsys.readouterr()
    assert "E_TEAM_COMPOSITION_EXEC_EXECUTOR" in err.err


def test_execute_composition_tasks_refuses_agy_without_run() -> None:
    from omg_cli.team.compositions.hyperplan import _HyperplanTaskAdapter

    with pytest.raises(CompositionExecutionError, match="refused"):
        execute_composition_tasks_v1(
            ".",
            "run1",
            TEAM,
            _HyperplanTaskAdapter(),
            executor="agy",
            bundle={},
        )
