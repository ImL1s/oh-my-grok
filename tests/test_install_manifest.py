"""#77 install manifest — runtime/scope, classify, rollback, no home project."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.host_probe import host_report_for_doctor, probe_host_from_fixture
from omg_cli.install_manifest import (
    InstallManifestError,
    apply_manifest,
    build_manifest,
    classify_auth,
    classify_bytes,
    classify_path,
    inspect_install_manifest,
    refuse_home_project,
    rollback_interrupted,
    run_scoped_setup,
)

ROOT = Path(__file__).resolve().parents[1]
HOST_FIXTURE = ROOT / "tests" / "fixtures" / "host" / "0.2.121.json"


def test_classify_missing_exact_stale_user_owned(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    assert classify_path(target, desired=b"x") == "missing"
    target.write_bytes(b"x")
    assert classify_path(target, desired=b"x") == "exact"
    target.write_text("<!-- OMG:START -->\nold\n", encoding="utf-8")
    assert classify_path(target, desired=b"new") == "stale"
    target.write_text("my personal notes\n", encoding="utf-8")
    assert classify_path(target, desired=b"omg") == "user_owned"


def test_classify_bytes_malformed_json() -> None:
    assert classify_bytes(desired=None, actual=b"{not json") == "malformed"


def test_preserve_user_owned_without_force(tmp_path: Path) -> None:
    dest = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("user kept this\n", encoding="utf-8")
    manifest = build_manifest(
        runtime="antigravity",
        scope="project",
        project_root=tmp_path,
        transaction_id="a" * 32,
        plugin=ROOT,
    )
    result = apply_manifest(manifest, project_root=tmp_path, force=False, plugin=ROOT)
    assert result["ok"] is True
    assert result["verified"] is False
    assert dest.read_text(encoding="utf-8") == "user kept this\n"
    assert any(row["class"] == "user_owned" for row in result["skipped"])


def test_force_replaces_user_owned(tmp_path: Path) -> None:
    dest = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("user kept this\n", encoding="utf-8")
    manifest = build_manifest(
        runtime="antigravity",
        scope="project",
        project_root=tmp_path,
        transaction_id="b" * 32,
        plugin=ROOT,
    )
    apply_manifest(manifest, project_root=tmp_path, force=True, plugin=ROOT)
    text = dest.read_text(encoding="utf-8")
    assert "OMG:MANAGED" in text
    assert "user kept this" not in text


def test_rollback_interrupted_restores_backup(tmp_path: Path) -> None:
    dest = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("original\n", encoding="utf-8")
    tx = tmp_path / ".omg" / "install" / "tx"
    backup = tx / "deadbeef"
    backup.mkdir(parents=True)
    (backup / "project.ag.projection.bak").write_text("original\n", encoding="utf-8")
    (backup / "project.ag.projection.json").write_text(
        json.dumps({"target": str(dest)}), encoding="utf-8"
    )
    dest.write_text("partial\n", encoding="utf-8")
    (tx / "current.json").write_text(
        json.dumps(
            {
                "status": "committing",
                "transaction_id": "deadbeef",
                "backup_dir": str(backup),
            }
        ),
        encoding="utf-8",
    )
    result = rollback_interrupted("project", tmp_path)
    assert result["rolled_back"] is True
    assert dest.read_text(encoding="utf-8") == "original\n"


def test_user_scope_does_not_create_project_omg(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(
        "omg_cli.install_manifest.user_store", lambda: fake_home / ".omg-user"
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    result = run_scoped_setup(runtime="grok", scope="user", plugin=ROOT)
    assert result["ok"] is True
    assert not (cwd / ".omg").exists()
    assert (fake_home / ".omg-user" / "install-manifest.json").is_file()


def test_refuse_home_project(tmp_path: Path) -> None:
    with pytest.raises(InstallManifestError, match="E_SETUP_HOME"):
        refuse_home_project(tmp_path, here=False, home=tmp_path)
    refuse_home_project(tmp_path, here=True, home=tmp_path)


def test_auth_never_false_green() -> None:
    assert classify_auth(env={})["ok"] is False
    assert classify_auth(env={"GROK_API_KEY": "invalid"})["state"] == "invalid"
    assert classify_auth(env={"GROK_API_KEY": "sk-fake-not-real"})["ok"] is False
    present = classify_auth(env={"GROK_API_KEY": "sk-not-a-real-production-secret"})
    assert present["ok"] is False
    assert present["state"] == "configured_unproven"
    blob = json.dumps(present)
    assert "GROK_API_KEY" not in blob
    assert "sk-not-a-real-production-secret" not in blob


def test_doctor_host_separates_auth_and_live_evidence() -> None:
    report = probe_host_from_fixture(HOST_FIXTURE)
    host = host_report_for_doctor(report)
    assert "binary" in host
    assert "version" in host
    assert "capabilities" in host
    assert "compatibility" in host
    assert host["auth"]["ok"] is False
    assert host["live_evidence"] is False
    blob = json.dumps(host)
    assert "authorization" not in blob
    assert "tok_live" not in blob


def test_inspect_manifest_unverified(tmp_path: Path) -> None:
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=tmp_path,
        here=True,
        plugin=ROOT,
    )
    payload = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert payload["configured"] is True
    assert payload["verified"] is False
    assert payload["observed"] is False
    assert payload["healthy"] is False
    assert payload["runtime"] == "antigravity"


def test_setup_cli_flags_exist() -> None:
    from omg_cli.main import build_parser

    parser = build_parser()
    help_text = parser.format_help()
    assert "setup" in help_text
    ns = parser.parse_args(["setup", "--runtime", "both", "--scope", "user"])
    assert ns.setup_runtime == "both"
    assert ns.setup_scope == "user"
