"""Hermetic tests for Security Research Composition Contract V1 (#69 PR8)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from omg_cli.main import main
from omg_cli.state import create_run
from omg_cli.team.compositions.security_research import (
    SECURITY_RESEARCH_KIND,
    SECURITY_RESEARCH_REPORT_KIND,
    SECURITY_RESEARCH_SCHEMA_VERSION,
    SecurityResearchError,
    compile_security_research_v1,
    load_security_research_manifest,
    materialize_security_research_v1,
    parse_security_research_spec_v1,
    security_research_manifest_path,
    validate_security_research_report_v1,
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
GOLDEN_MANIFEST = ROOT / "tests" / "golden" / "team_security_research_v1_manifest.json"
GOLDEN_V1 = ROOT / "tests" / "golden" / "team_operation_catalog_v1.json"
GOLDEN_V2 = ROOT / "tests" / "golden" / "team_operation_catalog_v2.json"
GOLDEN_V3 = ROOT / "tests" / "golden" / "team_operation_catalog_v3.json"

_DIGEST = "a" * 64
_DIGEST_B = "b" * 64


def _base_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "schema_version": 1,
        "target": "Evaluate auth and input surfaces for Security Research V1 scaffolding",
        "attack_surfaces": ["auth", "injection", "secrets"],
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
    status = create_run(tmp_path, mode="team", goal="security-research test")
    return str(status["run_id"])


def _coverage_for(
    manifest: dict[str, Any], *, status: str = "complete", reason: str | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lane in manifest["lanes"]:
        row: dict[str, Any] = {
            "lane_id": lane["lane_id"],
            "status": status,
            "artifact_digest": _DIGEST,
        }
        if reason is not None:
            row["reason"] = reason
        rows.append(row)
    return rows


def _lane_source_digests(manifest: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {"composition": manifest["digest"]}
    for lane in manifest["lanes"]:
        key = f"lane_{lane['lane_id'].replace('.', '_')}"
        out[key] = _DIGEST
    return out


def _finding(
    *,
    finding_id: str = "f1",
    surface: str = "auth",
    severity: str = "medium",
    blocking: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "finding_id": finding_id,
        "surface": surface,
        "severity": severity,
        "blocking": blocking,
        "attacker_capability": "unauthenticated remote client",
        "attack_path": "POST /login with crafted token",
        "reachability": "public HTTPS endpoint",
        "impact": "session fixation",
        "cwe_candidate": "CWE-384",
        "evidence_locations": ["src/auth/login.py:42"],
        "remediation": "rotate session id on privilege change",
        "regression_check": "unit test rejects reused pre-auth cookie",
    }
    row.update(extra)
    return row


def _report_for(
    manifest: dict[str, Any],
    *,
    verdict: str = "pass",
    coverage: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    blockers: list[str] | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cov = coverage if coverage is not None else _coverage_for(manifest)
    return {
        "kind": SECURITY_RESEARCH_REPORT_KIND,
        "schema_version": 1,
        "verdict": verdict,
        "composition_id": manifest["composition_id"],
        "composition_digest": manifest["digest"],
        "lane_coverage": cov,
        "findings": findings if findings is not None else [],
        "rejected_candidates": rejected if rejected is not None else [],
        "incomplete_audit_blockers": blockers if blockers is not None else [],
        "source_artifact_digests": _lane_source_digests(manifest),
        "writer": "omg-cli",
        "limitations": ["execution_supported=false"],
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
    # No catalog v4 / API / MCP security-research operation.
    blob = catalog_document_json()
    assert "security-research" not in blob
    assert "schema_version\": 4" not in blob
    assert '"schema_version": 4' not in blob


def test_compile_matches_golden_and_stable_digest() -> None:
    manifest = compile_security_research_v1(_base_spec())
    golden = json.loads(GOLDEN_MANIFEST.read_text(encoding="utf-8"))
    assert manifest == golden
    assert manifest["kind"] == SECURITY_RESEARCH_KIND
    assert manifest["schema_version"] == SECURITY_RESEARCH_SCHEMA_VERSION
    assert manifest["execution_supported"] is False
    assert manifest["safe_poc_policy"]["immutable"] is True
    assert manifest["safe_poc_policy"]["execution_supported"] is False
    assert "network_access" in manifest["safe_poc_policy"]["forbidden"]
    assert manifest["lane_count"] == 7  # 3 hunters + 4
    assert manifest["hunter_count"] == 3
    again = compile_security_research_v1(
        _base_spec(attack_surfaces=["secrets", "auth", "injection"])
    )
    assert again["digest"] == manifest["digest"]
    assert again["composition_id"] == manifest["composition_id"]


def test_exact_n_plus_4_dag_and_read_only_floors() -> None:
    surfaces = ["a", "b", "c", "d"]
    manifest = compile_security_research_v1(_base_spec(attack_surfaces=surfaces))
    assert manifest["lane_count"] == len(surfaces) + 4
    lanes = {row["lane_id"]: row for row in manifest["lanes"]}
    for surface in sorted(surfaces):
        row = lanes[f"hunt.{surface}"]
        assert row["role"] == "security-reviewer"
        assert row["posture"] == "read-only"
        assert row["requires_code_change"] is False
        assert row["allow_implementation"] is False
        assert row["owned_files"] == []
        assert row["depends_on"] == []
        assert "worktree" not in row
        assert "provider" not in row
        assert "pane" not in row
        assert "command" not in row
    hunter_ids = [f"hunt.{s}" for s in sorted(surfaces)]
    for vid in ("validate.primary", "validate.independent"):
        row = lanes[vid]
        assert row["role"] == "verifier"
        assert row["depends_on"] == hunter_ids
    consolidate = lanes["consolidate"]
    assert consolidate["role"] == "security-reviewer"
    assert consolidate["depends_on"] == [
        *hunter_ids,
        "validate.primary",
        "validate.independent",
    ]
    verify = lanes["verify"]
    assert verify["role"] == "verifier"
    assert verify["depends_on"] == [
        *hunter_ids,
        "validate.primary",
        "validate.independent",
        "consolidate",
    ]


def test_surface_bounds_and_path_attacks() -> None:
    with pytest.raises(SecurityResearchError, match="3–8"):
        parse_security_research_spec_v1(_base_spec(attack_surfaces=["a", "b"]))
    with pytest.raises(SecurityResearchError, match="duplicate"):
        parse_security_research_spec_v1(
            _base_spec(attack_surfaces=["a", "b", "a"])
        )
    with pytest.raises(SecurityResearchError, match="unknown|key mismatch"):
        parse_security_research_spec_v1(_base_spec(extra_field=True))
    with pytest.raises(SecurityResearchError, match="exactly one"):
        parse_security_research_spec_v1(
            {
                "schema_version": 1,
                "attack_surfaces": ["a", "b", "c"],
            }
        )
    with pytest.raises(SecurityResearchError, match="relative safe path"):
        parse_security_research_spec_v1(
            {
                "schema_version": 1,
                "target_artifact": {"path": "/etc/passwd", "digest": _DIGEST},
                "attack_surfaces": ["a", "b", "c"],
            }
        )
    with pytest.raises(SecurityResearchError, match="absolute/unsafe|relative"):
        parse_security_research_spec_v1(
            {
                "schema_version": 1,
                "target_artifact": {
                    "path": "Users/me/secret.json",
                    "digest": _DIGEST,
                },
                "attack_surfaces": ["a", "b", "c"],
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
    rc = main(
        ["team", "security-research", "plan", "--spec", str(spec_path), "--json"]
    )
    assert rc == 0
    after = _tree_digest(tmp_path)
    assert before == after
    payload = json.loads(capsys.readouterr().out)
    body = payload.get("data", payload)
    assert body["execution_supported"] is False
    assert body["lane_count"] == 7


def test_materialize_idempotent_and_no_exec_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    run_id = _make_run(tmp_path)
    import omg_cli.team.compositions.security_research as sr_mod
    import omg_cli.team.plane as plane_mod

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("execution surface touched")

    monkeypatch.setattr(plane_mod, "start_team", _boom, raising=False)
    monkeypatch.setattr("subprocess.run", _boom)
    monkeypatch.setattr("subprocess.Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)

    first = materialize_security_research_v1(tmp_path, run_id, _base_spec())
    assert first["ok"] is True and first["idempotent"] is False
    path = security_research_manifest_path(tmp_path, run_id)
    assert path.is_file() and not path.is_symlink()
    assert "team/compositions/security-research-v1.json" in first["path"]
    loaded = load_security_research_manifest(tmp_path, run_id)
    assert loaded["digest"] == first["manifest"]["digest"]
    assert loaded["execution_supported"] is False
    assert loaded.get("verified") is None

    second = materialize_security_research_v1(tmp_path, run_id, _base_spec())
    assert second["idempotent"] is True
    assert second["manifest"]["digest"] == first["manifest"]["digest"]
    _ = sr_mod


def test_digest_conflict_and_symlink_corrupt_refuse(tmp_path: Path) -> None:
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    path = security_research_manifest_path(tmp_path, run_id)

    with pytest.raises(SecurityResearchError, match="different composition"):
        materialize_security_research_v1(
            tmp_path,
            run_id,
            _base_spec(attack_surfaces=["alpha", "beta", "gamma"]),
        )

    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["lanes"][0]["expected_artifact"]["forged"] = True
    core = {k: v for k, v in forged.items() if k not in {"digest", "run_id"}}
    forged["digest"] = sha256_hex(canonical_json_bytes(core))
    atomic_write_bytes(
        path, canonical_json_bytes(forged), mode=DATA_FILE_MODE, replace=True
    )
    with pytest.raises(SecurityResearchError, match="digest conflict"):
        materialize_security_research_v1(tmp_path, run_id, _base_spec())

    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(SecurityResearchError, match="corrupt"):
        load_security_research_manifest(tmp_path, run_id)

    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(SecurityResearchError, match="symlink"):
        load_security_research_manifest(tmp_path, run_id)


def test_foreign_writer_and_stale_run(tmp_path: Path) -> None:
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    path = security_research_manifest_path(tmp_path, run_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["writer"] = "not-omg"
    core = {k: v for k, v in data.items() if k not in {"digest", "run_id"}}
    data["digest"] = sha256_hex(canonical_json_bytes(core))
    atomic_write_bytes(
        path, canonical_json_bytes(data), mode=DATA_FILE_MODE, replace=True
    )
    with pytest.raises(SecurityResearchError, match="foreign writer"):
        load_security_research_manifest(tmp_path, run_id)

    with pytest.raises(SecurityResearchError, match="missing|stale|cancelled"):
        materialize_security_research_v1(tmp_path, "no-such-run", _base_spec())


def test_report_verdicts_and_severity_proof_gates(tmp_path: Path) -> None:
    run_id = _make_run(tmp_path)
    result = materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = result["manifest"]

    coverage = _coverage_for(manifest)[:-1]
    with pytest.raises(SecurityResearchError, match="omits required lanes"):
        validate_security_research_report_v1(
            tmp_path,
            run_id,
            _report_for(manifest, coverage=coverage),
            persist=False,
        )

    # pass with findings → refuse
    with pytest.raises(SecurityResearchError, match="no surviving findings"):
        validate_security_research_report_v1(
            tmp_path,
            run_id,
            _report_for(manifest, verdict="pass", findings=[_finding()]),
            persist=False,
        )

    # pass_with_findings without findings → refuse
    with pytest.raises(SecurityResearchError, match="at least one"):
        validate_security_research_report_v1(
            tmp_path,
            run_id,
            _report_for(manifest, verdict="pass_with_findings", findings=[]),
            persist=False,
        )

    # high without dual validators → refuse
    with pytest.raises(SecurityResearchError, match="validator"):
        validate_security_research_report_v1(
            tmp_path,
            run_id,
            _report_for(
                manifest,
                verdict="pass_with_findings",
                findings=[_finding(severity="high")],
            ),
            persist=False,
        )

    # high with incomplete CVSS → refuse
    with pytest.raises(SecurityResearchError, match="complete metric vector"):
        validate_security_research_report_v1(
            tmp_path,
            run_id,
            _report_for(
                manifest,
                verdict="pass_with_findings",
                findings=[
                    _finding(
                        severity="high",
                        validator_artifact_refs=[_DIGEST, _DIGEST_B],
                        proof_kind="safe_static_proof",
                        cvss={"attack_vector": "NETWORK"},
                    )
                ],
            ),
            persist=False,
        )

    # valid pass_with_findings medium
    out = validate_security_research_report_v1(
        tmp_path,
        run_id,
        _report_for(
            manifest,
            verdict="pass_with_findings",
            findings=[_finding()],
            rejected=[
                {
                    "candidate_id": "c1",
                    "disposition": "falsified",
                    "reason": "not reachable from public surface",
                }
            ],
        ),
        persist=True,
    )
    assert out["ok"] is True and out["persisted"] is True
    assert out["report"]["verdict"] == "pass_with_findings"

    # block with incomplete audit blocker + blocked lane reason
    blocked = _coverage_for(manifest, status="blocked", reason="verify incomplete")
    out2 = validate_security_research_report_v1(
        tmp_path,
        run_id,
        _report_for(
            manifest,
            verdict="block",
            coverage=blocked,
            blockers=["audit incomplete: verify lane blocked"],
        ),
        persist=True,
    )
    assert out2["report"]["verdict"] == "block"

    # clean pass
    out3 = validate_security_research_report_v1(
        tmp_path,
        run_id,
        _report_for(manifest, verdict="pass"),
        persist=True,
    )
    assert out3["report"]["verdict"] == "pass"


def test_cli_materialize_and_validate_report(
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
            "security-research",
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
    report = _report_for(manifest, verdict="pass")
    rep_path = tmp_path / "report.json"
    rep_path.write_text(json.dumps(report), encoding="utf-8")
    rc2 = main(
        [
            "team",
            "security-research",
            "validate-report",
            "--run",
            run_id,
            "--input",
            str(rep_path),
            "--json",
        ]
    )
    assert rc2 == 0
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
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    team_root = tmp_path / ".omg" / "state" / "runs" / run_id / "team"
    assert (team_root / "compositions" / "security-research-v1.json").is_file()
    assert not list(tmp_path.rglob("verified.json"))
    # Immutable policy cannot be mutated via compile output aliasing.
    manifest = compile_security_research_v1(_base_spec())
    manifest["safe_poc_policy"]["forbidden"].append("should-not-stick")
    again = compile_security_research_v1(_base_spec())
    assert "should-not-stick" not in again["safe_poc_policy"]["forbidden"]
