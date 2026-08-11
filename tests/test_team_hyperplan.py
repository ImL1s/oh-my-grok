"""Hermetic tests for Hyperplan Composition Contract V1 (#69 PR7/PR10)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from omg_cli.main import main
from omg_cli.state import create_run
from omg_cli.team.compositions.hyperplan import (
    HYPERPLAN_DECISION_KIND,
    HYPERPLAN_KIND,
    HYPERPLAN_RESULT_BUNDLE_KIND,
    HYPERPLAN_SCHEMA_VERSION,
    HyperplanError,
    compile_hyperplan_decision_v1,
    compile_hyperplan_v1,
    hyperplan_decision_path,
    hyperplan_manifest_path,
    hyperplan_result_bundle_path,
    load_hyperplan_manifest,
    materialize_hyperplan_v1,
    parse_hyperplan_spec_v1,
    produce_hyperplan_decision_v1,
    validate_hyperplan_decision_v1,
)
from omg_cli.team.operation_catalog import (
    CATALOG_SCHEMA_VERSION,
    TEAM_OPERATION_CATALOG_V1,
    TEAM_OPERATION_CATALOG_V2,
    TEAM_OPERATION_CATALOG_V3,
    catalog_document_json,
    serialize_operation_catalog,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_MANIFEST = ROOT / "tests" / "golden" / "team_hyperplan_v1_manifest.json"
GOLDEN_BUNDLE = ROOT / "tests" / "golden" / "team_hyperplan_v1_result_bundle.json"
GOLDEN_DECISION = ROOT / "tests" / "golden" / "team_hyperplan_v1_decision.json"
GOLDEN_V1 = ROOT / "tests" / "golden" / "team_operation_catalog_v1.json"
GOLDEN_V2 = ROOT / "tests" / "golden" / "team_operation_catalog_v2.json"
GOLDEN_V3 = ROOT / "tests" / "golden" / "team_operation_catalog_v3.json"

_DIGEST = "a" * 64


def _base_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "schema_version": 1,
        "goal": "Evaluate plan for Hyperplan V1 scaffolding",
        "critique_dimensions": ["security", "correctness", "operability"],
    }
    spec.update(overrides)
    return spec


def _tree_digest(root: Path) -> str:
    rows: list[str] = []
    base = root / ".omg"
    if not base.exists():
        return hashlib.sha256(b"").hexdigest()
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(root).as_posix()
            body = path.read_bytes()
            rows.append(f"{rel}:{hashlib.sha256(body).hexdigest()}")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")


def _make_run(tmp_path: Path) -> str:
    status = create_run(tmp_path, mode="team", goal="hyperplan test")
    return str(status["run_id"])


def _coverage_for(manifest: dict[str, Any], *, status: str = "complete") -> list[dict[str, Any]]:
    return [
        {
            "lane_id": lane["lane_id"],
            "status": status,
            "artifact_digest": _DIGEST,
        }
        for lane in manifest["lanes"]
    ]


def _decision_for(
    manifest: dict[str, Any],
    *,
    verdict: str = "approved",
    coverage: list[dict[str, Any]] | None = None,
    conflicts: list[str] | None = None,
    repairs: list[str] | None = None,
    risks: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": HYPERPLAN_DECISION_KIND,
        "schema_version": 1,
        "verdict": verdict,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "lane_coverage": coverage if coverage is not None else _coverage_for(manifest),
        "conflicts": conflicts if conflicts is not None else [],
        "required_repairs": repairs if repairs is not None else [],
        "unresolved_risks": risks if risks is not None else [],
        "limitations": ["execution_supported=false"],
        "source_artifact_digests": {"composition": manifest["digest"]},
        "writer": "omg-cli",
    }


def test_catalog_v1_v2_v3_frozen() -> None:
    assert serialize_operation_catalog(
        operations=TEAM_OPERATION_CATALOG_V1, schema_version=1
    ) == json.loads(GOLDEN_V1.read_text(encoding="utf-8"))
    assert serialize_operation_catalog(
        operations=TEAM_OPERATION_CATALOG_V2, schema_version=2
    ) == json.loads(GOLDEN_V2.read_text(encoding="utf-8"))
    assert serialize_operation_catalog(
        operations=TEAM_OPERATION_CATALOG_V3, schema_version=3
    ) == json.loads(GOLDEN_V3.read_text(encoding="utf-8"))
    assert CATALOG_SCHEMA_VERSION == 4
    assert len(TEAM_OPERATION_CATALOG_V3) == 38
    # Hyperplan is not a Team API catalog operation (composition CLI only).
    blob = catalog_document_json()
    assert "hyperplan" not in blob
    assert '"bulk-create-tasks"' in blob


def test_compile_matches_golden_and_stable_digest() -> None:
    manifest = compile_hyperplan_v1(_base_spec())
    golden = json.loads(GOLDEN_MANIFEST.read_text(encoding="utf-8"))
    assert manifest == golden
    assert manifest["kind"] == HYPERPLAN_KIND
    assert manifest["schema_version"] == HYPERPLAN_SCHEMA_VERSION
    assert manifest["execution_supported"] is False
    assert manifest["lane_count"] == 5  # 3 critics + synthesize + verify
    assert manifest["critic_count"] == 3
    again = compile_hyperplan_v1(
        _base_spec(critique_dimensions=["operability", "security", "correctness"])
    )
    assert again["digest"] == manifest["digest"]
    assert again["composition_id"] == manifest["composition_id"]


def test_exact_n_plus_2_dag_and_read_only_floors() -> None:
    dims = ["a", "b", "c", "d"]
    manifest = compile_hyperplan_v1(_base_spec(critique_dimensions=dims))
    assert manifest["lane_count"] == len(dims) + 2
    lanes = {row["lane_id"]: row for row in manifest["lanes"]}
    for dim in sorted(dims):
        row = lanes[f"critic.{dim}"]
        assert row["role"] == "critic"
        assert row["posture"] == "read-only"
        assert row["requires_code_change"] is False
        assert row["allow_implementation"] is False
        assert row["owned_files"] == []
        assert row["depends_on"] == []
        assert "worktree" not in row
        assert "provider" not in row
    synth = lanes["synthesize"]
    assert synth["role"] == "planner"
    assert synth["depends_on"] == [f"critic.{d}" for d in sorted(dims)]
    verify = lanes["verify"]
    assert verify["role"] == "verifier"
    assert verify["depends_on"] == ["synthesize", *[f"critic.{d}" for d in sorted(dims)]]
    assert manifest["dependency_graph"]["verify"] == verify["depends_on"]


def test_dimension_bounds_and_duplicates() -> None:
    with pytest.raises(HyperplanError, match="3–8"):
        parse_hyperplan_spec_v1(_base_spec(critique_dimensions=["a", "b"]))
    with pytest.raises(HyperplanError, match="duplicate"):
        parse_hyperplan_spec_v1(
            _base_spec(critique_dimensions=["a", "b", "a"])
        )
    with pytest.raises(HyperplanError, match="unknown|key mismatch"):
        parse_hyperplan_spec_v1(_base_spec(extra_field=True))
    with pytest.raises(HyperplanError, match="exactly one"):
        parse_hyperplan_spec_v1(
            {
                "schema_version": 1,
                "critique_dimensions": ["a", "b", "c"],
            }
        )
    with pytest.raises(HyperplanError, match="relative safe path"):
        parse_hyperplan_spec_v1(
            {
                "schema_version": 1,
                "plan_artifact": {"path": "/etc/passwd", "digest": _DIGEST},
                "critique_dimensions": ["a", "b", "c"],
            }
        )


def test_plan_cli_zero_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env_on(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_base_spec()), encoding="utf-8")
    before = _tree_digest(tmp_path)
    rc = main(["team", "hyperplan", "plan", "--spec", str(spec_path), "--json"])
    assert rc == 0
    after = _tree_digest(tmp_path)
    assert before == after
    payload = json.loads(capsys.readouterr().out)
    body = payload.get("data", payload)
    assert body["execution_supported"] is False
    assert body["lane_count"] == 5


def test_materialize_idempotent_and_no_exec_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    run_id = _make_run(tmp_path)
    # Patch execution surfaces to raise if accidentally touched.
    import omg_cli.team.compositions.hyperplan as hp_mod
    import omg_cli.team.plane as plane_mod

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("execution surface touched")

    monkeypatch.setattr(plane_mod, "start_team", _boom, raising=False)
    monkeypatch.setattr("subprocess.run", _boom)
    monkeypatch.setattr("subprocess.Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)

    first = materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    assert first["ok"] is True and first["idempotent"] is False
    path = hyperplan_manifest_path(tmp_path, run_id)
    assert path.is_file() and not path.is_symlink()
    assert "team/compositions/hyperplan-v1.json" in first["path"]
    loaded = load_hyperplan_manifest(tmp_path, run_id)
    assert loaded["digest"] == first["manifest"]["digest"]
    assert loaded["execution_supported"] is False
    assert loaded.get("verified") is None

    second = materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    assert second["idempotent"] is True
    assert second["manifest"]["digest"] == first["manifest"]["digest"]
    _ = hp_mod  # imported for clarity / future hooks


def test_digest_conflict_and_symlink_corrupt_refuse(tmp_path: Path) -> None:
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes

    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    path = hyperplan_manifest_path(tmp_path, run_id)

    # Different composition on an already-present file → conflict
    with pytest.raises(HyperplanError, match="different composition"):
        materialize_hyperplan_v1(
            tmp_path,
            run_id,
            _base_spec(critique_dimensions=["alpha", "beta", "gamma"]),
        )

    # Same composition_id, forged digest body → digest conflict
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["lanes"][0]["expected_artifact"]["severity"] = "forged"
    core = {k: v for k, v in forged.items() if k not in {"digest", "run_id"}}
    forged["digest"] = sha256_hex(canonical_json_bytes(core))
    atomic_write_bytes(
        path, canonical_json_bytes(forged), mode=DATA_FILE_MODE, replace=True
    )
    with pytest.raises(HyperplanError, match="derived core drift|digest conflict"):
        materialize_hyperplan_v1(tmp_path, run_id, _base_spec())

    # Corrupt body
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(HyperplanError, match="corrupt"):
        load_hyperplan_manifest(tmp_path, run_id)

    # Symlink refuse
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(HyperplanError, match="symlink"):
        load_hyperplan_manifest(tmp_path, run_id)


def test_foreign_writer_and_stale_run(tmp_path: Path) -> None:
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    path = hyperplan_manifest_path(tmp_path, run_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["writer"] = "not-omg"
    core = {k: v for k, v in data.items() if k not in {"digest", "run_id"}}
    data["digest"] = sha256_hex(canonical_json_bytes(core))
    atomic_write_bytes(
        path, canonical_json_bytes(data), mode=DATA_FILE_MODE, replace=True
    )
    with pytest.raises(HyperplanError, match="foreign writer"):
        load_hyperplan_manifest(tmp_path, run_id)

    with pytest.raises(HyperplanError, match="missing|stale|cancelled"):
        materialize_hyperplan_v1(tmp_path, "no-such-run", _base_spec())


def test_decision_completeness_and_no_silent_approve(tmp_path: Path) -> None:
    run_id = _make_run(tmp_path)
    result = materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = result["manifest"]

    # Missing a lane → refuse
    coverage = _coverage_for(manifest)[:-1]
    with pytest.raises(HyperplanError, match="omits required lanes"):
        validate_hyperplan_decision_v1(
            tmp_path,
            run_id,
            _decision_for(manifest, coverage=coverage),
            persist=False,
        )

    # Approved with conflicts → refuse (never silent approve)
    with pytest.raises(HyperplanError, match="empty conflicts"):
        validate_hyperplan_decision_v1(
            tmp_path,
            run_id,
            _decision_for(manifest, conflicts=["disagreement"]),
            persist=False,
        )

    # Rejected incomplete lane is ok when verdict=rejected
    rejected = _decision_for(
        manifest,
        verdict="rejected",
        coverage=_coverage_for(manifest, status="blocked"),
        repairs=["fix verify lane"],
    )
    out = validate_hyperplan_decision_v1(
        tmp_path, run_id, rejected, persist=True
    )
    assert out["ok"] is True and out["persisted"] is True
    assert out["decision"]["verdict"] == "rejected"

    # Approved complete
    approved = _decision_for(manifest, verdict="approved")
    out2 = validate_hyperplan_decision_v1(
        tmp_path, run_id, approved, persist=True
    )
    assert out2["decision"]["verdict"] == "approved"


def test_cli_materialize_and_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env_on(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    run_id = _make_run(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_base_spec()), encoding="utf-8")

    rc = main(
        [
            "team",
            "hyperplan",
            "materialize",
            "--spec",
            str(spec_path),
            "--run",
            run_id,
            "--json",
        ]
    )
    assert rc == 0
    mat = json.loads(capsys.readouterr().out)
    body = mat.get("data", mat)
    manifest = body["manifest"]
    decision = _decision_for(manifest, verdict="approved")
    dec_path = tmp_path / "decision.json"
    dec_path.write_text(json.dumps(decision), encoding="utf-8")
    rc2 = main(
        [
            "team",
            "hyperplan",
            "validate-decision",
            "--run",
            run_id,
            "--input",
            str(dec_path),
            "--json",
        ]
    )
    assert rc2 == 0
    # No verified/passes authority
    status = json.loads(
        (tmp_path / ".omg" / "state" / "runs" / run_id / "status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status.get("verified") is not True
    assert not status.get("passes")


def test_catalog_v4_does_not_imply_verified_or_hyperplan_api(tmp_path: Path) -> None:
    # Catalog v4 landed for bulk-create-tasks; Hyperplan remains composition CLI only.
    assert CATALOG_SCHEMA_VERSION == 4
    blob = catalog_document_json()
    assert '"bulk-create-tasks"' in blob
    assert "hyperplan" not in blob
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    # Ensure state tree only gained compositions artifact under team/
    team_root = tmp_path / ".omg" / "state" / "runs" / run_id / "team"
    assert (team_root / "compositions" / "hyperplan-v1.json").is_file()
    # No spurious verified stamp files
    assert not list(tmp_path.rglob("verified.json"))


# ---------------------------------------------------------------------------
# #69 PR10 — hermetic result production
# ---------------------------------------------------------------------------


def _critic_receipt(
    dimension: str,
    *,
    findings: list[dict[str, Any]] | None = None,
    severity: str = "info",
    blocking: bool = False,
    status: str = "complete",
    reason: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "lane_id": f"critic.{dimension}",
        "status": status,
        "artifact_kind": "omg.team.hyperplan.critique",
        "payload": {
            "dimension": dimension,
            "findings": findings if findings is not None else [],
            "severity": severity,
            "blocking": blocking,
        },
    }
    if reason is not None:
        row["reason"] = reason
    return row


def _bundle_for(
    manifest: dict[str, Any],
    *,
    findings_by_dim: dict[str, list[dict[str, Any]]] | None = None,
    merged: list[dict[str, Any]] | None = None,
    conflicts: list[str] | None = None,
    synth_verdict: str = "approved",
    verify_verdict: str = "approved",
    verify_blockers: list[str] | None = None,
    incomplete_lane: str | None = None,
) -> dict[str, Any]:
    by_dim = findings_by_dim or {}
    dims = list(manifest["spec"]["critique_dimensions"])
    receipts: list[dict[str, Any]] = [
        _critic_receipt(dim, findings=by_dim.get(dim, [])) for dim in dims
    ]
    receipts.append(
        {
            "lane_id": "synthesize",
            "status": "complete",
            "artifact_kind": "omg.team.hyperplan.synthesis",
            "payload": {
                "summary": "Synthetic summary for hermetic tests.",
                "merged_findings": merged if merged is not None else [],
                "open_conflicts": conflicts if conflicts is not None else [],
                "recommended_verdict": synth_verdict,
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
                "blocking_issues": verify_blockers if verify_blockers is not None else [],
                "verdict": verify_verdict,
            },
        }
    )
    if incomplete_lane is not None:
        for row in receipts:
            if row["lane_id"] == incomplete_lane:
                row["status"] = "blocked"
                row["reason"] = "lane incomplete for test"
                break
    return {
        "kind": HYPERPLAN_RESULT_BUNDLE_KIND,
        "schema_version": 1,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "receipts": receipts,
    }


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
                monkeypatch.setattr(mod, attr, _boom, raising=False)


def test_produce_golden_bundle_and_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    golden_bundle = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    golden_decision = json.loads(GOLDEN_DECISION.read_text(encoding="utf-8"))
    assert golden_bundle["composition_id"] == manifest["composition_id"]
    decision = compile_hyperplan_decision_v1(manifest, golden_bundle)
    assert decision == golden_decision
    assert decision["verdict"] == "approved"
    out = produce_hyperplan_decision_v1(tmp_path, run_id, golden_bundle)
    assert out["ok"] is True and out["idempotent"] is False
    again = produce_hyperplan_decision_v1(tmp_path, run_id, golden_bundle)
    assert again["idempotent"] is True
    assert hyperplan_result_bundle_path(tmp_path, run_id).is_file()
    assert hyperplan_decision_path(tmp_path, run_id).is_file()


def test_validate_decision_refuses_overwrite_when_result_bundle_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    golden_bundle = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    produced = produce_hyperplan_decision_v1(tmp_path, run_id, golden_bundle)
    decision_path = hyperplan_decision_path(tmp_path, run_id)
    before = decision_path.read_bytes()

    forged = dict(produced["decision"])
    forged["notes"] = "forged-via-validate"
    with pytest.raises(HyperplanError, match="result-bundle present"):
        validate_hyperplan_decision_v1(tmp_path, run_id, forged, persist=True)
    assert decision_path.read_bytes() == before

    again = validate_hyperplan_decision_v1(
        tmp_path, run_id, produced["decision"], persist=True
    )
    assert again["ok"] is True
    assert again.get("idempotent") is True
    assert decision_path.read_bytes() == before

    check = validate_hyperplan_decision_v1(
        tmp_path, run_id, forged, persist=False
    )
    assert check["persisted"] is False
    assert decision_path.read_bytes() == before


def test_missing_duplicate_unknown_lanes_and_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    bundle = _bundle_for(manifest)
    bundle["receipts"] = bundle["receipts"][:-1]
    with pytest.raises(HyperplanError, match="omits required lanes"):
        compile_hyperplan_decision_v1(manifest, bundle)

    bundle2 = _bundle_for(manifest)
    bundle2["receipts"].append(dict(bundle2["receipts"][0]))
    with pytest.raises(HyperplanError, match="duplicate receipt"):
        compile_hyperplan_decision_v1(manifest, bundle2)

    finding = {"finding_id": "corr_1", "summary": "edge case"}
    omitted = _bundle_for(
        manifest,
        findings_by_dim={"correctness": [finding]},
        merged=[],
        synth_verdict="rejected",
        verify_verdict="rejected",
    )
    with pytest.raises(HyperplanError, match="omits critic findings"):
        compile_hyperplan_decision_v1(manifest, omitted)

    invented = _bundle_for(
        manifest,
        merged=[{"finding_id": "invented", "disposition": "dismissed"}],
        synth_verdict="rejected",
        verify_verdict="rejected",
    )
    with pytest.raises(HyperplanError, match="invents unknown"):
        compile_hyperplan_decision_v1(manifest, invented)


def test_dimension_spoof_and_artifact_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    bundle = _bundle_for(manifest)
    for row in bundle["receipts"]:
        if row["lane_id"] == "critic.correctness":
            row["payload"]["dimension"] = "security"
            break
    with pytest.raises(HyperplanError, match="dimension must be"):
        compile_hyperplan_decision_v1(manifest, bundle)

    bad_kind = _bundle_for(manifest)
    for row in bad_kind["receipts"]:
        if row["lane_id"] == "synthesize":
            row["artifact_kind"] = "omg.team.hyperplan.critique"
            break
    with pytest.raises(HyperplanError, match="artifact_kind"):
        compile_hyperplan_decision_v1(manifest, bad_kind)


def test_contradictory_synthesis_and_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    with pytest.raises(HyperplanError, match="open_conflicts is contradictory"):
        compile_hyperplan_decision_v1(
            manifest,
            _bundle_for(
                manifest,
                conflicts=["critics disagree on scope"],
                synth_verdict="approved",
            ),
        )
    with pytest.raises(HyperplanError, match="blocking_issues is contradictory"):
        compile_hyperplan_decision_v1(
            manifest,
            _bundle_for(
                manifest,
                verify_verdict="approved",
                verify_blockers=["coverage hole"],
            ),
        )


def test_incomplete_lane_derives_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    bundle = _bundle_for(manifest, incomplete_lane="verify")
    decision = compile_hyperplan_decision_v1(manifest, bundle)
    assert decision["verdict"] == "rejected"
    assert any(row["status"] == "blocked" for row in decision["lane_coverage"])


def test_repairs_risks_and_accepted_blocking_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    finding = {
        "finding_id": "sec_block",
        "summary": "missing authz",
        "blocking": True,
    }
    decision = compile_hyperplan_decision_v1(
        manifest,
        _bundle_for(
            manifest,
            findings_by_dim={"security": [finding]},
            merged=[{"finding_id": "sec_block", "disposition": "accepted"}],
            synth_verdict="approved",
            verify_verdict="approved",
        ),
    )
    assert decision["verdict"] == "rejected"

    repair = compile_hyperplan_decision_v1(
        manifest,
        _bundle_for(
            manifest,
            findings_by_dim={
                "correctness": [{"finding_id": "corr_fix", "summary": "race"}]
            },
            merged=[{"finding_id": "corr_fix", "disposition": "repair"}],
            synth_verdict="rejected",
            verify_verdict="rejected",
        ),
    )
    assert repair["verdict"] == "rejected"
    assert repair["required_repairs"] == ["corr_fix"]


def test_produce_conflict_symlink_foreign_writer_and_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    golden = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    produce_hyperplan_decision_v1(tmp_path, run_id, golden)

    other = _bundle_for(
        manifest,
        findings_by_dim={
            "operability": [{"finding_id": "ops_1", "summary": "other"}]
        },
        merged=[{"finding_id": "ops_1", "disposition": "dismissed"}],
        synth_verdict="approved",
        verify_verdict="approved",
    )
    with pytest.raises(HyperplanError, match="conflict"):
        produce_hyperplan_decision_v1(tmp_path, run_id, other)

    run2_dir = tmp_path / "run2_root"
    run2_dir.mkdir()
    run2 = _make_run(run2_dir)
    materialize_hyperplan_v1(run2_dir, run2, _base_spec())
    decision_path = hyperplan_decision_path(run2_dir, run2)
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    target = run2_dir / "elsewhere-decision.json"
    target.write_text("{}", encoding="utf-8")
    decision_path.symlink_to(target)
    with pytest.raises(HyperplanError, match="symlink"):
        produce_hyperplan_decision_v1(run2_dir, run2, golden)

    run3_dir = tmp_path / "run3_root"
    run3_dir.mkdir()
    run3 = _make_run(run3_dir)
    materialize_hyperplan_v1(run3_dir, run3, _base_spec())
    man3 = load_hyperplan_manifest(run3_dir, run3)
    golden3 = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    assert golden3["composition_id"] == man3["composition_id"]
    produce_hyperplan_decision_v1(run3_dir, run3, golden3)
    bpath = hyperplan_result_bundle_path(run3_dir, run3)
    data = json.loads(bpath.read_text(encoding="utf-8"))
    data["writer"] = "not-omg"
    core = {k: v for k, v in data.items() if k != "digest"}
    data["digest"] = sha256_hex(canonical_json_bytes(core))
    atomic_write_bytes(
        bpath, canonical_json_bytes(data), mode=DATA_FILE_MODE, replace=True
    )
    with pytest.raises(HyperplanError, match="foreign writer"):
        produce_hyperplan_decision_v1(run3_dir, run3, golden3)


def test_failure_between_bundle_and_decision_leaves_no_authoritative_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    golden = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    decision_path = hyperplan_decision_path(tmp_path, run_id)
    bundle_path = hyperplan_result_bundle_path(tmp_path, run_id)

    import omg_cli.team.compositions.hyperplan as hp_mod

    real_atomic = hp_mod.atomic_write_bytes
    calls = {"n": 0}

    def _flaky(path, body, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] >= 2 and Path(path) == decision_path:
            raise hp_mod.ContractPathError("injected failure before decision commit")
        return real_atomic(path, body, **kwargs)

    monkeypatch.setattr(hp_mod, "atomic_write_bytes", _flaky)
    with pytest.raises(HyperplanError, match="commit marker|refused"):
        produce_hyperplan_decision_v1(tmp_path, run_id, golden)
    assert bundle_path.is_file()
    assert not decision_path.exists()


def test_cli_produce_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env_on(monkeypatch)
    _patch_no_exec(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    run_id = _make_run(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_base_spec()), encoding="utf-8")
    assert (
        main(
            [
                "team",
                "hyperplan",
                "materialize",
                "--spec",
                str(spec_path),
                "--run",
                run_id,
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(GOLDEN_BUNDLE.read_text(encoding="utf-8"), encoding="utf-8")
    rc = main(
        [
            "team",
            "hyperplan",
            "produce-decision",
            "--run",
            run_id,
            "--input",
            str(bundle_path),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    body = payload.get("data", payload)
    assert body["decision"]["verdict"] == "approved"
    status = json.loads(
        (tmp_path / ".omg" / "state" / "runs" / run_id / "status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status.get("verified") is not True
    assert not status.get("passes")


def test_source_digests_bind_bundle_and_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    manifest = load_hyperplan_manifest(tmp_path, run_id)
    golden = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    decision = compile_hyperplan_decision_v1(manifest, golden)
    sources = decision["source_artifact_digests"]
    assert sources["composition"] == manifest["digest"]
    assert "result_bundle" in sources
    for lane in manifest["lanes"]:
        key = f"lane_{lane['lane_id'].replace('.', '_')}"
        assert key in sources
    assert "hermetic_result_production_v1" in decision["limitations"]
    assert "execution_supported=false" in decision["limitations"]
