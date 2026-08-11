"""Hermetic tests for Security Research Composition Contract V1 (#69 PR8/PR9)."""

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
    SECURITY_RESEARCH_RESULT_BUNDLE_KIND,
    SECURITY_RESEARCH_SCHEMA_VERSION,
    SecurityResearchError,
    compile_security_research_report_v1,
    compile_security_research_v1,
    load_security_research_manifest,
    materialize_security_research_v1,
    parse_security_research_spec_v1,
    produce_security_research_report_v1,
    security_research_manifest_path,
    security_research_report_path,
    security_research_result_bundle_path,
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
GOLDEN_BUNDLE = ROOT / "tests" / "golden" / "team_security_research_v1_result_bundle.json"
GOLDEN_REPORT = ROOT / "tests" / "golden" / "team_security_research_v1_report.json"
GOLDEN_V1 = ROOT / "tests" / "golden" / "team_operation_catalog_v1.json"
GOLDEN_V2 = ROOT / "tests" / "golden" / "team_operation_catalog_v2.json"
GOLDEN_V3 = ROOT / "tests" / "golden" / "team_operation_catalog_v3.json"

_DIGEST = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64


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
    manifest: dict[str, Any],
    *,
    status: str = "complete",
    reason: str | None = None,
    digests: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, lane in enumerate(manifest["lanes"]):
        lane_id = lane["lane_id"]
        digest = (digests or {}).get(lane_id)
        if digest is None:
            # Distinct per-lane digests so high/critical can bind the
            # validate.primary / validate.independent unordered pair.
            digest = f"{idx:064x}"
        row: dict[str, Any] = {
            "lane_id": lane_id,
            "status": status,
            "artifact_digest": digest,
        }
        if reason is not None:
            row["reason"] = reason
        rows.append(row)
    return rows


def _lane_source_digests(
    manifest: dict[str, Any], coverage: list[dict[str, Any]] | None = None
) -> dict[str, str]:
    cov = coverage if coverage is not None else _coverage_for(manifest)
    out: dict[str, str] = {"composition": manifest["digest"]}
    for row in cov:
        key = f"lane_{row['lane_id'].replace('.', '_')}"
        out[key] = row["artifact_digest"]
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
        "source_artifact_digests": _lane_source_digests(manifest, cov),
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
    with pytest.raises(SecurityResearchError, match="derived core drift|digest conflict"):
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
    cov = _coverage_for(manifest)
    by_lane = {row["lane_id"]: row["artifact_digest"] for row in cov}
    with pytest.raises(SecurityResearchError, match="complete metric vector"):
        validate_security_research_report_v1(
            tmp_path,
            run_id,
            _report_for(
                manifest,
                verdict="pass_with_findings",
                coverage=cov,
                findings=[
                    _finding(
                        severity="high",
                        validator_artifact_refs=[
                            by_lane["validate.primary"],
                            by_lane["validate.independent"],
                        ],
                        proof_kind="safe_static_proof",
                        cvss={"attack_vector": "NETWORK"},
                    )
                ],
            ),
            persist=False,
        )

    # high with spoofed validator refs (not the coverage digests) → refuse
    with pytest.raises(SecurityResearchError, match="unordered pair|exactly"):
        validate_security_research_report_v1(
            tmp_path,
            run_id,
            _report_for(
                manifest,
                verdict="pass_with_findings",
                coverage=cov,
                findings=[
                    _finding(
                        severity="high",
                        validator_artifact_refs=[_DIGEST, _DIGEST_B],
                        proof_kind="safe_static_proof",
                    )
                ],
            ),
            persist=False,
        )

    # high with invalid CVSS enum → refuse
    with pytest.raises(SecurityResearchError, match="CVSS 3.1 enum"):
        validate_security_research_report_v1(
            tmp_path,
            run_id,
            _report_for(
                manifest,
                verdict="pass_with_findings",
                coverage=cov,
                findings=[
                    _finding(
                        severity="high",
                        validator_artifact_refs=[
                            by_lane["validate.primary"],
                            by_lane["validate.independent"],
                        ],
                        proof_kind="safe_static_proof",
                        cvss={
                            "attack_vector": "WIFI",
                            "attack_complexity": "LOW",
                            "privileges_required": "NONE",
                            "user_interaction": "NONE",
                            "scope": "UNCHANGED",
                            "confidentiality": "HIGH",
                            "integrity": "HIGH",
                            "availability": "HIGH",
                        },
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


# ---------------------------------------------------------------------------
# #69 PR9 — hermetic result production
# ---------------------------------------------------------------------------


def _candidate(
    candidate_id: str = "auth_session_fixation",
    *,
    severity_hint: str = "medium",
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "candidate_id": candidate_id,
        "severity_hint": severity_hint,
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


def _hunt_receipt(
    surface: str, candidates: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    cands = candidates if candidates is not None else []
    return {
        "lane_id": f"hunt.{surface}",
        "status": "complete",
        "artifact_kind": "omg.team.security_research.candidate_findings",
        "payload": {
            "surface": surface,
            "candidates": cands,
            "severity_hints": {
                c["candidate_id"]: c["severity_hint"] for c in cands
            },
            "evidence_pointers": [f"src/{surface}/mod.py:1"],
        },
    }


def _validate_receipt(
    lane: str,
    *,
    validated: list[dict[str, Any]] | None = None,
    falsified: list[dict[str, Any]] | None = None,
    proof_kind: str = "static",
    status: str = "complete",
    reason: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "lane_id": f"validate.{lane}",
        "status": status,
        "artifact_kind": "omg.team.security_research.validation",
        "payload": {
            "validated": validated or [],
            "falsified": falsified or [],
            "proof_kind": proof_kind,
        },
    }
    if reason is not None:
        row["reason"] = reason
    return row


def _bundle_for(
    manifest: dict[str, Any],
    *,
    candidates: list[dict[str, Any]] | None = None,
    validated_primary: list[dict[str, Any]] | None = None,
    validated_independent: list[dict[str, Any]] | None = None,
    falsified_primary: list[dict[str, Any]] | None = None,
    falsified_independent: list[dict[str, Any]] | None = None,
    surviving: list[dict[str, Any]] | None = None,
    rejected: list[dict[str, Any]] | None = None,
    calibration: dict[str, str] | None = None,
    recommended_verdict: str = "pass",
    incomplete_lane: str | None = None,
    primary_proof: str = "static",
    independent_proof: str = "static",
) -> dict[str, Any]:
    cands = candidates if candidates is not None else []
    # Place all candidates on hunt.auth for simplicity unless surface encoded.
    by_surface: dict[str, list[dict[str, Any]]] = {
        "auth": [],
        "injection": [],
        "secrets": [],
    }
    for cand in cands:
        surface = "auth"
        for name in ("injection", "secrets", "auth"):
            if cand["candidate_id"].startswith(name):
                surface = name
                break
        by_surface[surface].append(cand)

    receipts = [
        _hunt_receipt("auth", by_surface["auth"]),
        _hunt_receipt("injection", by_surface["injection"]),
        _hunt_receipt("secrets", by_surface["secrets"]),
        _validate_receipt(
            "primary",
            validated=validated_primary,
            falsified=falsified_primary,
            proof_kind=primary_proof,
        ),
        _validate_receipt(
            "independent",
            validated=validated_independent,
            falsified=falsified_independent,
            proof_kind=independent_proof,
        ),
        {
            "lane_id": "consolidate",
            "status": "complete",
            "artifact_kind": "omg.team.security_research.consolidated_report",
            "payload": {
                "surviving_findings": surviving or [],
                "rejected_candidates": rejected or [],
                "severity_calibration": calibration or {},
                "recommended_verdict": recommended_verdict,
            },
        },
        {
            "lane_id": "verify",
            "status": "complete",
            "artifact_kind": "omg.team.security_research.gate",
            "payload": {
                "gate": "security-research-v1",
                "covered_lanes": [r["lane_id"] for r in manifest["lanes"]],
                "blocking_issues": [],
                "verdict": recommended_verdict,
            },
        },
    ]
    if incomplete_lane is not None:
        for row in receipts:
            if row["lane_id"] == incomplete_lane:
                row["status"] = "blocked"
                row["reason"] = "lane incomplete for test"
                break
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


def test_produce_golden_bundle_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    golden_bundle = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    golden_report = json.loads(GOLDEN_REPORT.read_text(encoding="utf-8"))
    assert golden_bundle["composition_id"] == manifest["composition_id"]
    report = compile_security_research_report_v1(manifest, golden_bundle)
    assert report == golden_report
    assert report["verdict"] == "pass"
    out = produce_security_research_report_v1(tmp_path, run_id, golden_bundle)
    assert out["ok"] is True and out["idempotent"] is False
    again = produce_security_research_report_v1(tmp_path, run_id, golden_bundle)
    assert again["idempotent"] is True
    assert security_research_result_bundle_path(tmp_path, run_id).is_file()
    assert security_research_report_path(tmp_path, run_id).is_file()


def test_validate_report_refuses_overwrite_when_result_bundle_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Produce owns the report commit marker; validate-report must not desync it."""
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    golden_bundle = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    produced = produce_security_research_report_v1(tmp_path, run_id, golden_bundle)
    report_path = security_research_report_path(tmp_path, run_id)
    before = report_path.read_bytes()

    forged = dict(produced["report"])
    forged["notes"] = "forged-via-validate"
    with pytest.raises(SecurityResearchError, match="result-bundle present"):
        validate_security_research_report_v1(
            tmp_path, run_id, forged, persist=True
        )
    assert report_path.read_bytes() == before

    # Idempotent re-check of the exact produce report is allowed.
    again = validate_security_research_report_v1(
        tmp_path, run_id, produced["report"], persist=True
    )
    assert again["ok"] is True
    assert again.get("idempotent") is True
    assert report_path.read_bytes() == before

    # In-memory validation of a different report still works without persist.
    check = validate_security_research_report_v1(
        tmp_path, run_id, forged, persist=False
    )
    assert check["persisted"] is False
    assert report_path.read_bytes() == before


def test_produce_high_finding_binds_validator_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    cand = _candidate(severity_hint="high", blocking=False)
    bundle = _bundle_for(
        manifest,
        candidates=[cand],
        validated_primary=[
            {"candidate_id": cand["candidate_id"], "proof_mode": "local_fixture"}
        ],
        validated_independent=[
            {"candidate_id": cand["candidate_id"], "proof_mode": "local_fixture"}
        ],
        surviving=[
            {
                "candidate_id": cand["candidate_id"],
                "severity": "high",
                "blocking": False,
            }
        ],
        calibration={cand["candidate_id"]: "high"},
        recommended_verdict="pass_with_findings",
        primary_proof="local_fixture",
        independent_proof="local_fixture",
    )
    report = compile_security_research_report_v1(manifest, bundle)
    assert report["verdict"] == "pass_with_findings"
    finding = report["findings"][0]
    assert finding["proof_kind"] == "reproduced"
    by_lane = {r["lane_id"]: r["artifact_digest"] for r in report["lane_coverage"]}
    assert sorted(finding["validator_artifact_refs"]) == sorted(
        [by_lane["validate.primary"], by_lane["validate.independent"]]
    )


def test_validator_disagreement_and_spoofed_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    cand = _candidate(severity_hint="high")
    disagree = _bundle_for(
        manifest,
        candidates=[cand],
        validated_primary=[
            {"candidate_id": cand["candidate_id"], "proof_mode": "static"}
        ],
        falsified_independent=[
            {"candidate_id": cand["candidate_id"], "reason": "not reachable"}
        ],
        surviving=[{"candidate_id": cand["candidate_id"], "severity": "high"}],
        calibration={cand["candidate_id"]: "high"},
        recommended_verdict="pass_with_findings",
    )
    with pytest.raises(SecurityResearchError, match="disagreement"):
        compile_security_research_report_v1(manifest, disagree)

    # Direct validate-report spoof already covered; ensure produce refuses
    # missing dual validation for high.
    missing = _bundle_for(
        manifest,
        candidates=[cand],
        validated_primary=[
            {"candidate_id": cand["candidate_id"], "proof_mode": "static"}
        ],
        surviving=[{"candidate_id": cand["candidate_id"], "severity": "high"}],
        calibration={cand["candidate_id"]: "high"},
        recommended_verdict="pass_with_findings",
    )
    with pytest.raises(SecurityResearchError, match="both validators"):
        compile_security_research_report_v1(manifest, missing)


def test_missing_duplicate_lanes_and_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    bundle = _bundle_for(manifest)
    bundle["receipts"] = bundle["receipts"][:-1]
    with pytest.raises(SecurityResearchError, match="omits required lanes"):
        compile_security_research_report_v1(manifest, bundle)

    bundle2 = _bundle_for(manifest)
    bundle2["receipts"].append(dict(bundle2["receipts"][0]))
    with pytest.raises(SecurityResearchError, match="duplicate receipt"):
        compile_security_research_report_v1(manifest, bundle2)

    dup = _candidate("auth_dup")
    bad = _bundle_for(
        manifest,
        candidates=[dup, dict(dup)],
    )
    with pytest.raises(SecurityResearchError, match="duplicate candidate"):
        compile_security_research_report_v1(manifest, bad)


def test_incomplete_lane_derives_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    bundle = _bundle_for(manifest, incomplete_lane="verify")
    report = compile_security_research_report_v1(manifest, bundle)
    assert report["verdict"] == "block"
    assert report["incomplete_audit_blockers"]
    assert any(row["status"] == "blocked" for row in report["lane_coverage"])


def test_produce_conflict_symlink_foreign_writer_and_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex

    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    golden = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    produce_security_research_report_v1(tmp_path, run_id, golden)

    other = _bundle_for(
        manifest,
        candidates=[_candidate("auth_other", severity_hint="low")],
        surviving=[{"candidate_id": "auth_other", "severity": "low", "blocking": False}],
        calibration={"auth_other": "low"},
        recommended_verdict="pass_with_findings",
        validated_primary=[
            {"candidate_id": "auth_other", "proof_mode": "static"}
        ],
        validated_independent=[
            {"candidate_id": "auth_other", "proof_mode": "static"}
        ],
    )
    with pytest.raises(SecurityResearchError, match="conflict"):
        produce_security_research_report_v1(tmp_path, run_id, other)

    # Symlink report path refused on load/produce paths.
    run2_dir = tmp_path / "run2_root"
    run2_dir.mkdir()
    run2 = _make_run(run2_dir)
    materialize_security_research_v1(run2_dir, run2, _base_spec())
    report_path = security_research_report_path(run2_dir, run2)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    target = run2_dir / "elsewhere-report.json"
    target.write_text("{}", encoding="utf-8")
    report_path.symlink_to(target)
    with pytest.raises(SecurityResearchError, match="symlink"):
        produce_security_research_report_v1(run2_dir, run2, golden)
    if report_path.exists():
        assert report_path.is_symlink()

    # Foreign writer on existing bundle.
    run3_dir = tmp_path / "run3_root"
    run3_dir.mkdir()
    run3 = _make_run(run3_dir)
    materialize_security_research_v1(run3_dir, run3, _base_spec())
    # Rebind golden composition to this run's manifest.
    man3 = load_security_research_manifest(run3_dir, run3)
    golden3 = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    assert golden3["composition_id"] == man3["composition_id"]
    produce_security_research_report_v1(run3_dir, run3, golden3)
    bpath = security_research_result_bundle_path(run3_dir, run3)
    data = json.loads(bpath.read_text(encoding="utf-8"))
    data["writer"] = "not-omg"
    core = {k: v for k, v in data.items() if k != "digest"}
    data["digest"] = sha256_hex(canonical_json_bytes(core))
    atomic_write_bytes(
        bpath, canonical_json_bytes(data), mode=DATA_FILE_MODE, replace=True
    )
    with pytest.raises(SecurityResearchError, match="foreign writer"):
        produce_security_research_report_v1(run3_dir, run3, golden3)


def test_failure_between_bundle_and_report_leaves_no_authoritative_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    golden = json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8"))
    report_path = security_research_report_path(tmp_path, run_id)
    bundle_path = security_research_result_bundle_path(tmp_path, run_id)

    import omg_cli.team.compositions.security_research as sr_mod

    real_atomic = sr_mod.atomic_write_bytes
    calls = {"n": 0}

    def _flaky(path, body, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        # First write is bundle; second is report — fail the report commit.
        if calls["n"] >= 2 and Path(path) == report_path:
            raise sr_mod.ContractPathError("injected failure before report commit")
        return real_atomic(path, body, **kwargs)

    monkeypatch.setattr(sr_mod, "atomic_write_bytes", _flaky)
    with pytest.raises(SecurityResearchError, match="commit marker|refused"):
        produce_security_research_report_v1(tmp_path, run_id, golden)
    assert bundle_path.is_file()
    assert not report_path.exists()


def test_cli_produce_report(
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
                "security-research",
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
            "security-research",
            "produce-report",
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
    assert body["report"]["verdict"] == "pass"
    status = json.loads(
        (tmp_path / ".omg" / "state" / "runs" / run_id / "status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status.get("verified") is not True
    assert not status.get("passes")


def test_deterministic_ordering_and_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    manifest = compile_security_research_v1(_base_spec())
    cand_a = _candidate("auth_a", severity_hint="low")
    cand_b = _candidate("auth_b", severity_hint="low")
    bundle = _bundle_for(
        manifest,
        candidates=[cand_b, cand_a],
        surviving=[
            {"candidate_id": "auth_b", "severity": "low", "blocking": False},
            {"candidate_id": "auth_a", "severity": "low", "blocking": False},
        ],
        calibration={"auth_a": "low", "auth_b": "low"},
        recommended_verdict="pass_with_findings",
        validated_primary=[
            {"candidate_id": "auth_a", "proof_mode": "static"},
            {"candidate_id": "auth_b", "proof_mode": "static"},
        ],
        validated_independent=[
            {"candidate_id": "auth_b", "proof_mode": "static"},
            {"candidate_id": "auth_a", "proof_mode": "static"},
        ],
    )
    r1 = compile_security_research_report_v1(manifest, bundle)
    r2 = compile_security_research_report_v1(manifest, bundle)
    assert r1 == r2
    assert [f["finding_id"] for f in r1["findings"]] == ["auth_a", "auth_b"]


def test_codex_p1_blocking_issues_force_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    bundle = _bundle_for(manifest)
    for row in bundle["receipts"]:
        if row["lane_id"] == "verify":
            row["payload"]["blocking_issues"] = ["unresolved exploit path"]
            row["payload"]["verdict"] = "block"
            break
    report = compile_security_research_report_v1(manifest, bundle)
    assert report["verdict"] == "block"
    assert "unresolved exploit path" in report["incomplete_audit_blockers"]


def test_codex_p1_verify_covered_lanes_must_match_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    bundle = _bundle_for(manifest)
    for row in bundle["receipts"]:
        if row["lane_id"] == "verify":
            row["payload"]["covered_lanes"] = ["hunt.auth"]
            break
    with pytest.raises(SecurityResearchError, match="covered_lanes"):
        compile_security_research_report_v1(manifest, bundle)


def test_codex_p1_every_hunted_candidate_needs_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_exec(monkeypatch)
    run_id = _make_run(tmp_path)
    materialize_security_research_v1(tmp_path, run_id, _base_spec())
    manifest = load_security_research_manifest(tmp_path, run_id)
    dropped = _candidate("auth_dropped", severity_hint="critical")
    kept = _candidate("auth_kept", severity_hint="low")
    bundle = _bundle_for(
        manifest,
        candidates=[dropped, kept],
        surviving=[{"candidate_id": "auth_kept", "severity": "low", "blocking": False}],
        calibration={"auth_kept": "low"},
        recommended_verdict="pass_with_findings",
    )
    with pytest.raises(SecurityResearchError, match="lack consolidate disposition"):
        compile_security_research_report_v1(manifest, bundle)
