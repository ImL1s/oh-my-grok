"""Hermetic tests for Hyperplan Composition Contract V1 (#69 PR7)."""

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
    HYPERPLAN_SCHEMA_VERSION,
    HyperplanError,
    compile_hyperplan_v1,
    hyperplan_manifest_path,
    load_hyperplan_manifest,
    materialize_hyperplan_v1,
    parse_hyperplan_spec_v1,
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
    assert CATALOG_SCHEMA_VERSION == 3
    assert catalog_document_json() == GOLDEN_V3.read_text(encoding="utf-8")
    assert len(TEAM_OPERATION_CATALOG_V3) == 38


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
    with pytest.raises(HyperplanError, match="digest conflict"):
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


def test_no_catalog_v4_and_no_verified_writes(tmp_path: Path) -> None:
    assert CATALOG_SCHEMA_VERSION == 3
    run_id = _make_run(tmp_path)
    materialize_hyperplan_v1(tmp_path, run_id, _base_spec())
    # Ensure state tree only gained compositions artifact under team/
    team_root = tmp_path / ".omg" / "state" / "runs" / run_id / "team"
    assert (team_root / "compositions" / "hyperplan-v1.json").is_file()
    # No spurious verified stamp files
    assert not list(tmp_path.rglob("verified.json"))
