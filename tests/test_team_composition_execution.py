"""Hermetic tests for Composition Execution V1 (#69)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.evidence import CLI_WRITER
from omg_cli.jobs.lease import release_lease_dict
from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.ownership import capture_identity, pid_alive
from omg_cli.jobs.runtime import absorb_live_job_identities, cancel_job, prove_job_processes_gone
from omg_cli.jobs.store import list_job_ids, read_job_record, write_job_record
from omg_cli.main import main
from omg_cli.state import write_status
from omg_cli.team.compositions.execution import (
    COMPOSITION_EXECUTION_KIND,
    GROK_EXECUTOR,
    GROK_JOB_WAIT_FALLBACK_S,
    CompositionExecutionError,
    _grok_job_wait_s,
    compile_composition_execution_v1,
    composition_execution_path,
    execute_composition_tasks_v1,
    fixture_pane_id,
    grok_job_pane_id,
    parse_composition_execution_v1,
    require_composition_executor,
    require_fixture_executor,
    _assert_existing_matches_admitted,
    _lane_results_from_bundle,
)
from omg_cli.team.compositions.hyperplan import (
    HYPERPLAN_RESULT_BUNDLE_KIND,
    HyperplanError,
    _HyperplanTaskAdapter,
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
    TEAM_OPERATION_CATALOG_V4,
    catalog_document_json,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, start_team

pytest_plugins = ["tests.jobs_grok_testutil"]

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_V1 = ROOT / "tests" / "golden" / "team_operation_catalog_v1.json"
GOLDEN_V2 = ROOT / "tests" / "golden" / "team_operation_catalog_v2.json"
GOLDEN_V3 = ROOT / "tests" / "golden" / "team_operation_catalog_v3.json"
GOLDEN_V4 = ROOT / "tests" / "golden" / "team_operation_catalog_v4.json"
GROK_JOB_ID = "20260101T000000Z-abcd1234"
_REFUSED_EXECUTORS = (
    "agy",
    "antigravity",
    "claude",
    "codex",
    "cursor",
    "cursor-agent",
    "gemini",
    "kimi",
    "omc",
    "omx",
)

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


def _hp_execution_path(root: Path, run_id: str) -> Path:
    return composition_execution_path(root, run_id, "hyperplan_v1")


def _kill_session(proc: subprocess.Popen[Any]) -> None:
    import signal

    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 1:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _stamp_live_identity(rec: Any, ident: Any) -> None:
    rec.pid = ident.pid
    rec.pgid = ident.pgid
    rec.pid_starttime = ident.pid_starttime
    pp = dict(rec.provider_process or {})
    pp.update(
        {
            "state": "bound",
            "pid": ident.pid,
            "pgid": ident.pgid,
            "pid_starttime": ident.pid_starttime,
        }
    )
    rec.provider_process = pp
    rec.owner_lease = release_lease_dict(getattr(rec, "owner_lease", None))


def _absorb_plus(identities: dict[int, Any], extra: Any) -> None:
    absorb_live_job_identities(identities)
    if extra is not None and getattr(extra, "pid", None) not in identities:
        identities[extra.pid] = extra


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


def _hp_lane_ids(manifest: dict[str, Any]) -> list[str]:
    return [str(row["lane_id"]) for row in manifest["lanes"]]


def _parse_hp_execute_bundle(
    bundle: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    return _lane_results_from_bundle(
        bundle,
        adapter=_HyperplanTaskAdapter(),
        manifest=manifest,
        expected_lanes=_hp_lane_ids(manifest),
    )


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
    assert CATALOG_SCHEMA_VERSION == 6
    assert (
        catalog_document_json(operations=TEAM_OPERATION_CATALOG_V4, schema_version=4)
        == GOLDEN_V4.read_text(encoding="utf-8")
    )


def test_require_fixture_executor_refuses_live_providers() -> None:
    assert require_fixture_executor("fixture") == "fixture"
    assert require_fixture_executor("FIXTURE") == "fixture"
    assert require_composition_executor("grok") == "grok"
    assert require_composition_executor("GROK") == GROK_EXECUTOR
    for name in _REFUSED_EXECUTORS:
        with pytest.raises(CompositionExecutionError, match="refused"):
            require_fixture_executor(name)
    with pytest.raises(CompositionExecutionError, match="required"):
        require_fixture_executor("")
    with pytest.raises(CompositionExecutionError, match="unsupported"):
        require_fixture_executor("mystery")


def test_execute_input_accepts_canonical_result_bundle() -> None:
    manifest = compile_hyperplan_v1(_hp_spec())
    parsed = _parse_hp_execute_bundle(_hp_bundle(manifest), manifest)
    assert set(parsed) == set(_hp_lane_ids(manifest))
    for row in parsed.values():
        assert row["status"] == "complete"
        assert "payload" in row


def test_execute_input_refuses_foreign_writer() -> None:
    manifest = compile_hyperplan_v1(_hp_spec())
    bundle = _hp_bundle(manifest)
    bundle["writer"] = "attacker"
    with pytest.raises(CompositionExecutionError, match="foreign writer"):
        _parse_hp_execute_bundle(bundle, manifest)


def test_execute_input_refuses_claimed_digest_mismatch() -> None:
    manifest = compile_hyperplan_v1(_hp_spec())
    bundle = _hp_bundle(manifest)
    bundle["digest"] = "a" * 64
    with pytest.raises(CompositionExecutionError, match="digest mismatch"):
        _parse_hp_execute_bundle(bundle, manifest)


def test_execute_input_refuses_wrong_artifact_kind() -> None:
    manifest = compile_hyperplan_v1(_hp_spec())
    bundle = _hp_bundle(manifest)
    bundle["receipts"][0]["artifact_kind"] = "omg.team.hyperplan.synthesis"
    with pytest.raises(CompositionExecutionError, match="artifact_kind"):
        _parse_hp_execute_bundle(bundle, manifest)


def test_execute_input_refuses_unexpected_fields() -> None:
    manifest = compile_hyperplan_v1(_hp_spec())
    bundle = _hp_bundle(manifest)
    bundle["unexpected"] = True
    with pytest.raises(CompositionExecutionError, match="key mismatch"):
        _parse_hp_execute_bundle(bundle, manifest)


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


def test_grok_job_wait_uses_configured_provider_timeout() -> None:
    from types import SimpleNamespace

    assert (
        _grok_job_wait_s(SimpleNamespace(request={"timeout_s": 3600.0}, worker={}))
        == 3600.0
    )
    assert (
        _grok_job_wait_s(SimpleNamespace(request={"timeout_s": 12.5}, worker={}))
        == 12.5
    )
    assert (
        _grok_job_wait_s(SimpleNamespace(request={}, worker={"timeout_s": 9}))
        == 9.0
    )
    assert _grok_job_wait_s(SimpleNamespace(request={}, worker={})) == GROK_JOB_WAIT_FALLBACK_S
    assert (
        _grok_job_wait_s(SimpleNamespace(request={"timeout_s": 0}, worker={}))
        == GROK_JOB_WAIT_FALLBACK_S
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


def test_compile_grok_stamps_job_pane_and_limitations() -> None:
    row = _evidence_row()
    row["job_id"] = GROK_JOB_ID
    row["pane_id"] = grok_job_pane_id(GROK_JOB_ID)
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
        executor="grok",
    )
    assert doc["executor"] == "grok"
    assert doc["execution_supported"] is True
    assert doc["worker_evidence"][0]["job_id"] == GROK_JOB_ID
    assert doc["worker_evidence"][0]["pane_id"] == f"job-{GROK_JOB_ID}"
    assert doc["limitations"][0] == "executor=grok"
    assert "not_live_verified" in doc["limitations"]
    parsed = parse_composition_execution_v1(doc)
    assert parsed["digest"] == doc["digest"]

    fixture_shaped = _evidence_row()
    with pytest.raises(CompositionExecutionError, match="job_id"):
        compile_composition_execution_v1(
            source_kind="hyperplan_v1",
            run_id="run1",
            team_id=TEAM,
            composition_id="comp1",
            composition_digest=DIGEST_A,
            batch_id="batch1",
            batch_digest=DIGEST_B,
            worker_evidence=[fixture_shaped],
            collected_digest=DIGEST_C,
            executor="grok",
        )
    with_job = _evidence_row()
    with_job["job_id"] = GROK_JOB_ID
    with pytest.raises(CompositionExecutionError, match="must not include job_id"):
        compile_composition_execution_v1(
            source_kind="hyperplan_v1",
            run_id="run1",
            team_id=TEAM,
            composition_id="comp1",
            composition_digest=DIGEST_A,
            batch_id="batch1",
            batch_digest=DIGEST_B,
            worker_evidence=[with_job],
            collected_digest=DIGEST_C,
            executor="fixture",
        )


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
def test_execute_executor_mismatch_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    _patch_no_exec(monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    bundle = _hp_bundle(manifest)
    first = execute_hyperplan_tasks_v1(
        tmp_path,
        run_id,
        TEAM,
        executor="fixture",
        bundle=bundle,
    )
    assert first["execution"]["executor"] == "fixture"
    with pytest.raises(HyperplanError, match="executor conflict"):
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="grok",
            bundle=bundle,
        )


@_POSIX
def test_execute_idempotent_refuses_conflicting_lane_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    _patch_no_exec(monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    bundle = _hp_bundle(manifest)
    first = execute_hyperplan_tasks_v1(
        tmp_path,
        run_id,
        TEAM,
        executor="fixture",
        bundle=bundle,
    )
    assert first["idempotent"] is False
    other = _hp_bundle(manifest)
    for receipt in other["receipts"]:
        if receipt["lane_id"] == "synthesize":
            receipt["payload"]["summary"] = "A different synthetic summary."
            break
    with pytest.raises(HyperplanError, match="lane result digest conflict"):
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="fixture",
            bundle=other,
        )


def test_existing_execution_must_match_admitted_lanes() -> None:
    mapping = {
        "synthesize": "t-synth",
        "critique": "t-crit",
        "decide": "t-decide",
    }
    topo = list(mapping)
    truncated = {
        "worker_evidence": [
            {"lane_id": "synthesize", "task_id": "t-synth"},
        ],
        "lane_result_digests": [{"lane_id": "synthesize", "digest": DIGEST_A}],
    }
    with pytest.raises(CompositionExecutionError, match="admitted topo_order"):
        _assert_existing_matches_admitted(
            truncated, topo_order=topo, mapping=mapping
        )
    wrong_task = {
        "worker_evidence": [
            {"lane_id": lane, "task_id": "t-wrong"} for lane in topo
        ],
        "lane_result_digests": [
            {"lane_id": lane, "digest": DIGEST_A} for lane in topo
        ],
    }
    with pytest.raises(CompositionExecutionError, match="task_id mismatch"):
        _assert_existing_matches_admitted(
            wrong_task, topo_order=topo, mapping=mapping
        )
    ok = {
        "worker_evidence": [
            {"lane_id": lane, "task_id": mapping[lane]} for lane in topo
        ],
        "lane_result_digests": [
            {"lane_id": lane, "digest": DIGEST_A} for lane in topo
        ],
    }
    _assert_existing_matches_admitted(ok, topo_order=topo, mapping=mapping)


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
def test_execute_refuses_codex_before_workers(
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
            executor="codex",
            bundle=_hp_bundle(manifest),
        )


@_POSIX
def test_hyperplan_grok_execute_launches_job_and_stamps_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_grok_path: Path,
) -> None:
    del fake_grok_path
    run_id = _seed(tmp_path, monkeypatch)
    mat = materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    manifest = mat["manifest"]
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    bundle = _hp_bundle(manifest)
    out = execute_hyperplan_tasks_v1(
        tmp_path,
        run_id,
        TEAM,
        executor="grok",
        bundle=bundle,
    )
    assert out["ok"] is True
    assert out["idempotent"] is False
    assert out["execution_supported"] is True
    assert out["manifest_execution_supported"] is False
    execution = out["execution"]
    assert execution["executor"] == "grok"
    assert execution["execution_supported"] is True
    assert "not_live_verified" in execution["limitations"]
    assert execution.get("live_verified") is not True
    jobs = list_job_ids(tmp_path)
    assert len(jobs) == 1
    rec = read_job_record(tmp_path, jobs[0])
    assert rec.provider == "grok"
    assert rec.state == JobState.SUCCEEDED
    prove_job_processes_gone(tmp_path, jobs[0])
    if rec.pid is not None:
        assert not pid_alive(rec.pid)
    for row in execution["worker_evidence"]:
        assert row["job_id"] == jobs[0]
        assert row["pane_id"] == grok_job_pane_id(jobs[0])
        assert row["run_id"] == run_id
        assert row["worker_id"] == WORKER
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
        executor="grok",
        bundle=bundle,
    )
    assert again["idempotent"] is True
    assert again["execution"]["digest"] == execution["digest"]
    assert list_job_ids(tmp_path) == jobs


@_POSIX
def test_hyperplan_grok_execute_job_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_grok_path: Path,
) -> None:
    del fake_grok_path
    run_id = _seed(tmp_path, monkeypatch, worker_topology="job")
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    out = execute_hyperplan_tasks_v1(
        tmp_path,
        run_id,
        TEAM,
        executor="grok",
        bundle=_hp_bundle(manifest),
    )
    assert out["execution"]["executor"] == "grok"
    jobs = list_job_ids(tmp_path)
    assert jobs
    assert read_job_record(tmp_path, jobs[0]).state == JobState.SUCCEEDED


@_POSIX
def test_hyperplan_grok_execute_failed_job_does_not_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_grok_path: Path,
) -> None:
    del fake_grok_path
    monkeypatch.setenv("FAKE_GROK_RUN_RC", "1")
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    with pytest.raises(HyperplanError, match="did not succeed"):
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="grok",
            bundle=_hp_bundle(manifest),
        )
    exec_path = (
        tmp_path
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / "compositions"
        / "hyperplan-v1-execution.json"
    )
    assert not exec_path.exists()
    loaded = load_hyperplan_manifest(tmp_path, run_id)
    assert loaded["execution_supported"] is False


@_POSIX
def test_hyperplan_grok_execute_forged_succeeded_live_process_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_grok_path: Path,
) -> None:
    """Forged SUCCEEDED while a live start identity exists must not stamp."""
    del fake_grok_path
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ident = capture_identity(proc.pid, pgid=os.getpgid(proc.pid))
    assert isinstance(ident.pid_starttime, str) and ident.pid_starttime

    def _status(project_root: Path, job_id: str) -> Any:
        rec = read_job_record(project_root, job_id)
        _stamp_live_identity(rec, ident)
        return rec

    def _wait(project_root: Path, job_id: str, **kwargs: Any) -> tuple[Any, bool]:
        rec = _status(project_root, job_id)
        rec.state = JobState.SUCCEEDED
        write_job_record(project_root, rec)
        on_poll = kwargs.get("on_poll")
        if callable(on_poll):
            on_poll(rec)
        return rec, False

    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.job_status", _status
    )
    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.wait_job", _wait
    )
    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.absorb_live_job_identities",
        lambda idents: _absorb_plus(idents, ident),
    )
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    try:
        with pytest.raises(
            HyperplanError, match="still live|claimed terminal"
        ) as exc_info:
            execute_hyperplan_tasks_v1(
                tmp_path,
                run_id,
                TEAM,
                executor="grok",
                bundle=_hp_bundle(manifest),
            )
        assert exc_info.value.code == "E_TEAM_COMPOSITION_EXEC_JOB"
        assert not pid_alive(proc.pid)
        exec_path = _hp_execution_path(tmp_path, run_id)
        assert not exec_path.exists()
        loaded = load_hyperplan_manifest(tmp_path, run_id)
        assert loaded["execution_supported"] is False
        status = json.loads(
            (tmp_path / ".omg" / "state" / "runs" / run_id / "status.json").read_text(
                encoding="utf-8"
            )
        )
        assert status.get("verified") is not True
        assert not status.get("passes")
    finally:
        if pid_alive(proc.pid):
            _kill_session(proc)


@_POSIX
def test_hyperplan_grok_execute_timeout_cancel_failure_is_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_grok_path: Path,
) -> None:
    """Timeout cancel JobStoreError is not swallowed into success."""
    del fake_grok_path

    def _wait(project_root: Path, job_id: str, **kwargs: Any) -> tuple[Any, bool]:
        del kwargs
        rec = read_job_record(project_root, job_id)
        return rec, True

    def _cancel(*_args: Any, **_kwargs: Any) -> Any:
        raise JobStoreError("cancel unproven", code="E_JOB_CANCEL_UNPROVEN")

    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.wait_job", _wait
    )
    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.cancel_job", _cancel
    )
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    with pytest.raises(HyperplanError, match="cancel failed") as exc_info:
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="grok",
            bundle=_hp_bundle(manifest),
        )
    assert exc_info.value.code == "E_TEAM_COMPOSITION_EXEC_JOB"
    exec_path = _hp_execution_path(tmp_path, run_id)
    assert not exec_path.exists()
    loaded = load_hyperplan_manifest(tmp_path, run_id)
    assert loaded["execution_supported"] is False


@_POSIX
def test_hyperplan_grok_execute_timeout_cancel_unproven_is_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_grok_path: Path,
) -> None:
    """Timeout after a forged SUCCEEDED stamp must prove exit, not succeed."""
    del fake_grok_path
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ident = capture_identity(proc.pid, pgid=os.getpgid(proc.pid))
    assert isinstance(ident.pid_starttime, str) and ident.pid_starttime

    def _status(project_root: Path, job_id: str) -> Any:
        rec = read_job_record(project_root, job_id)
        _stamp_live_identity(rec, ident)
        return rec

    def _wait(project_root: Path, job_id: str, **kwargs: Any) -> tuple[Any, bool]:
        rec = _status(project_root, job_id)
        rec.state = JobState.SUCCEEDED
        write_job_record(project_root, rec)
        on_poll = kwargs.get("on_poll")
        if callable(on_poll):
            on_poll(rec)
        return rec, True

    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.job_status", _status
    )
    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.wait_job", _wait
    )
    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.absorb_live_job_identities",
        lambda idents: _absorb_plus(idents, ident),
    )
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    try:
        with pytest.raises(HyperplanError, match="timed out|unproven") as exc_info:
            execute_hyperplan_tasks_v1(
                tmp_path,
                run_id,
                TEAM,
                executor="grok",
                bundle=_hp_bundle(manifest),
            )
        assert exc_info.value.code == "E_TEAM_COMPOSITION_EXEC_JOB"
        exec_path = _hp_execution_path(tmp_path, run_id)
        assert not exec_path.exists()
        loaded = load_hyperplan_manifest(tmp_path, run_id)
        assert loaded["execution_supported"] is False
    finally:
        if pid_alive(proc.pid):
            _kill_session(proc)


@_POSIX
def test_hyperplan_grok_execute_wait_error_cancels_before_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_grok_path: Path,
) -> None:
    """recovery-required wait must cancel/prove, not skip the cancel path."""
    del fake_grok_path
    cancelled: list[str] = []

    def _wait(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        raise JobStoreError("lease stale live", code="E_JOB_RECOVERY_REQUIRED")

    real_cancel = cancel_job

    def _cancel(project_root: Path, job_id: str, **kwargs: Any) -> Any:
        cancelled.append(job_id)
        return real_cancel(project_root, job_id, **kwargs)

    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.wait_job", _wait
    )
    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.cancel_job", _cancel
    )
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    with pytest.raises(HyperplanError) as exc_info:
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="grok",
            bundle=_hp_bundle(manifest),
        )
    assert exc_info.value.code == "E_TEAM_COMPOSITION_EXEC_JOB"
    assert cancelled
    exec_path = _hp_execution_path(tmp_path, run_id)
    assert not exec_path.exists()
    loaded = load_hyperplan_manifest(tmp_path, run_id)
    assert loaded["execution_supported"] is False


@_POSIX
def test_hyperplan_grok_execute_inner_identity_unproven_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_grok_path: Path,
) -> None:
    """Missing spawn-recovery identity must not stamp execution evidence."""
    del fake_grok_path

    def _wait(project_root: Path, job_id: str, **kwargs: Any) -> tuple[Any, bool]:
        del kwargs
        rec = read_job_record(project_root, job_id)
        rec.state = JobState.SUCCEEDED
        rec.owner_lease = release_lease_dict(getattr(rec, "owner_lease", None))
        write_job_record(project_root, rec)
        return rec, False

    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.wait_job", _wait
    )
    monkeypatch.setattr(
        "omg_cli.team.compositions.execution.absorb_live_job_identities",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "omg_cli.team.compositions.execution._read_spawn_identity_recovery",
        lambda *_args, **_kwargs: None,
    )
    run_id = _seed(tmp_path, monkeypatch)
    materialize_hyperplan_v1(tmp_path, run_id, _hp_spec())
    admit_hyperplan_tasks_v1(tmp_path, run_id, TEAM)
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    with pytest.raises(HyperplanError, match="never captured") as exc_info:
        execute_hyperplan_tasks_v1(
            tmp_path,
            run_id,
            TEAM,
            executor="grok",
            bundle=_hp_bundle(manifest),
        )
    assert exc_info.value.code == "E_TEAM_COMPOSITION_EXEC_JOB"
    exec_path = _hp_execution_path(tmp_path, run_id)
    assert not exec_path.exists()
    loaded = load_hyperplan_manifest(tmp_path, run_id)
    assert loaded["execution_supported"] is False


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
def test_cli_hyperplan_execute_grok_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_grok_path: Path,
) -> None:
    del fake_grok_path
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
            "grok",
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
    assert body["execution"]["executor"] == "grok"
    job_id = body["execution"]["worker_evidence"][0]["job_id"]
    rec = read_job_record(tmp_path, job_id)
    assert rec.provider == "grok"
    assert rec.state == JobState.SUCCEEDED
    prove_job_processes_gone(tmp_path, job_id)
    if rec.pid is not None:
        assert not pid_alive(rec.pid)
    assert body.get("live_verified") is not True


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


@pytest.mark.parametrize("name", _REFUSED_EXECUTORS)
def test_execute_composition_tasks_refuses_foreign_executors(name: str) -> None:
    with pytest.raises(CompositionExecutionError, match="refused"):
        execute_composition_tasks_v1(
            ".",
            "run1",
            TEAM,
            _HyperplanTaskAdapter(),
            executor=name,
            bundle={},
        )
