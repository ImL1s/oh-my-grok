"""#77 install manifest — runtime/scope, classify, rollback, no home project."""

from __future__ import annotations

import hashlib
import json
import sys
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
    payload = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert payload["ok"] is True
    assert payload.get("drift") == []


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


def test_inspect_hash_mismatch_is_stale_not_user_owned(tmp_path: Path) -> None:
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=tmp_path,
        here=True,
        plugin=ROOT,
    )
    payload = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert payload["ok"] is True
    assert payload.get("drift") == []
    dest = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    dest.write_text("totally different user text\n", encoding="utf-8")
    drifted = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert drifted["ok"] is False
    assert any(row["class"] == "stale" for row in drifted["drift"])


def test_force_replaces_symlink_without_following(tmp_path: Path) -> None:
    dest = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    dest.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        dest.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    manifest = build_manifest(
        runtime="antigravity",
        scope="project",
        project_root=tmp_path,
        transaction_id="c" * 32,
        plugin=ROOT,
    )
    apply_manifest(manifest, project_root=tmp_path, force=True, plugin=ROOT)
    assert dest.is_symlink() is False
    assert "OMG:MANAGED" in dest.read_text(encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_rollback_unlinks_created_artifact(tmp_path: Path) -> None:
    dest = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("new\n", encoding="utf-8")
    tx = tmp_path / ".omg" / "install" / "tx" / ("d" * 32)
    tx.mkdir(parents=True)
    (tx / "art.prev.json").write_text(
        json.dumps({"target": str(dest), "kind": "created"}), encoding="utf-8"
    )
    marker = tmp_path / ".omg" / "install" / "tx" / "current.json"
    marker.write_text(
        json.dumps(
            {
                "status": "committing",
                "transaction_id": "d" * 32,
                "backup_dir": str(tx),
            }
        ),
        encoding="utf-8",
    )
    out = rollback_interrupted("project", tmp_path)
    assert dest.exists() is False
    assert str(dest) in out["removed"]


def test_user_scope_setup_skips_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.main import main
    from omg_cli.project_root import ProjectRootError

    def boom(*_args, **_kwargs):
        raise ProjectRootError("stale OMG_PROJECT_ROOT")

    monkeypatch.setattr("omg_cli.project_root.resolve_project_root", boom)
    monkeypatch.setattr(
        "omg_cli.install_manifest.run_scoped_setup",
        lambda **_kwargs: {"manifest": "x", "written": []},
    )
    rc = main(["setup", "--scope", "user", "--runtime", "antigravity"])
    assert rc == 0


def test_antigravity_runtime_skips_legacy_grok_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli.main import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        "omg_cli.commands.install.project_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        "omg_cli.install_manifest.run_scoped_setup",
        lambda **_kwargs: {"manifest": "x", "written": [], "skipped": []},
    )
    monkeypatch.setattr(
        "omg_cli.install_manifest.refuse_home_project", lambda *_a, **_k: None
    )
    sys.modules.pop("omg_cli.setup_cmd", None)
    rc = main(["setup", "--runtime", "antigravity", "--here"])
    assert rc == 0
    assert "omg_cli.setup_cmd" not in sys.modules


def test_mergeable_agents_records_content_hash(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    body = "<!-- OMG:START -->\nmerged\n<!-- OMG:END -->\n"
    agents.write_text(body, encoding="utf-8")
    run_scoped_setup(
        runtime="grok",
        scope="project",
        project_root=tmp_path,
        here=True,
        plugin=ROOT,
    )
    raw = json.loads(
        (tmp_path / ".omg" / "install" / "manifest.json").read_text(encoding="utf-8")
    )
    agents_row = next(row for row in raw["artifacts"] if row["id"] == "project.agents")
    assert agents_row["content_hash"] == hashlib.sha256(agents.read_bytes()).hexdigest()
    payload = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert payload["ok"] is True
    assert payload.get("drift") == []
    agents.write_text(
        "<!-- OMG:START -->\nchanged\n<!-- OMG:END -->\n", encoding="utf-8"
    )
    drifted = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert drifted["ok"] is False
    assert any(
        row["class"] == "stale" and row["id"] == "project.agents"
        for row in drifted["drift"]
    )


def test_manifest_symlink_replaced_not_followed(tmp_path: Path) -> None:
    dest = tmp_path / ".omg" / "install" / "manifest.json"
    dest.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    try:
        dest.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=tmp_path,
        here=True,
        plugin=ROOT,
    )
    assert dest.is_symlink() is False
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["schema"] == "omg-install-manifest/v1"
    assert outside.read_text(encoding="utf-8") == "{}\n"


def test_failed_commit_rolls_back_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orig = Path.write_text

    def wrapped(self, data="", *args, **kwargs):
        if self.name == "current.json" and "committed" in str(data):
            raise OSError("disk full")
        return orig(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", wrapped)
    with pytest.raises(InstallManifestError, match="E_TX"):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=tmp_path,
            here=True,
            plugin=ROOT,
        )
    dest = tmp_path / ".omg" / "install" / "manifest.json"
    assert dest.exists() is False
    projection = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    assert projection.exists() is False


def test_claimed_symlink_is_drift(tmp_path: Path) -> None:
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=tmp_path,
        here=True,
        plugin=ROOT,
    )
    dest = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x\n", encoding="utf-8")
    dest.unlink()
    try:
        dest.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    payload = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert payload["ok"] is False
    assert any(row["class"] == "foreign" for row in payload["drift"])


def test_doctor_probes_user_scope_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omg_cli import doctor as doctor_mod

    seen: list[str] = []

    def fake_inspect(*, project_root, scope):
        seen.append(scope)
        if scope == "user":
            return {
                "ok": False,
                "configured": True,
                "installed": True,
                "observed": False,
                "healthy": False,
                "verified": False,
                "runtime": "grok",
                "drift": [{"id": "user.manifest.marker", "class": "stale"}],
            }
        return {
            "ok": True,
            "configured": False,
            "installed": False,
            "observed": False,
            "healthy": False,
            "verified": False,
            "drift": [],
        }

    monkeypatch.setattr("omg_cli.cli_util.project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "omg_cli.install_manifest.inspect_install_manifest", fake_inspect
    )
    name, level, detail = doctor_mod.check_install_manifest()
    assert name == "install manifest"
    assert seen == ["project", "user"]
    assert level == "warn"
    assert "user_configured=True" in detail
    assert "project_configured=False" in detail


def test_rollback_ignores_escape_target(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("keep\n", encoding="utf-8")
    tx_root = project / ".omg" / "install" / "tx"
    backup = tx_root / ("e" * 32)
    backup.mkdir(parents=True)
    (backup / "evil.prev.json").write_text(
        json.dumps({"target": str(secret), "kind": "created"}), encoding="utf-8"
    )
    (tx_root / "current.json").write_text(
        json.dumps(
            {
                "status": "committing",
                "transaction_id": "e" * 32,
                "backup_dir": str(backup),
            }
        ),
        encoding="utf-8",
    )
    rollback_interrupted("project", project)
    assert secret.read_text(encoding="utf-8") == "keep\n"


def test_refuses_symlinked_omg_parent(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (project / ".omg").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    with pytest.raises(InstallManifestError, match="E_SYMLINK"):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=project,
            here=True,
            plugin=ROOT,
        )
    assert not (outside / "projections").exists()


def test_oversize_file_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omg_cli.install_manifest.MAX_BACKUP_BYTES", 8)
    dest = tmp_path / ".omg" / "projections" / "antigravity" / "README.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("0123456789 extra\n", encoding="utf-8")
    with pytest.raises(InstallManifestError, match="E_TX"):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=tmp_path,
            here=True,
            force=True,
            plugin=ROOT,
        )
    assert dest.read_text(encoding="utf-8") == "0123456789 extra\n"


def test_inspect_rejects_empty_json_manifest(tmp_path: Path) -> None:
    dest = tmp_path / ".omg" / "install" / "manifest.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("{}\n", encoding="utf-8")
    payload = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert payload["ok"] is False
    assert payload.get("installed") is False


def test_rollback_skips_symlinked_parent_target(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    link = project / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    tx_root = project / ".omg" / "install" / "tx"
    backup = tx_root / ("f" * 32)
    backup.mkdir(parents=True)
    (backup / "evil.prev.json").write_text(
        json.dumps({"target": str(link / "victim.txt"), "kind": "created"}),
        encoding="utf-8",
    )
    (tx_root / "current.json").write_text(
        json.dumps(
            {
                "status": "committing",
                "transaction_id": "f" * 32,
                "backup_dir": str(backup),
            }
        ),
        encoding="utf-8",
    )
    rollback_interrupted("project", project)
    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_rollback_does_not_follow_symlink_file_target(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    dest = project / ".omg" / "projections" / "antigravity" / "README.md"
    dest.parent.mkdir(parents=True)
    try:
        dest.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    tx_root = project / ".omg" / "install" / "tx"
    backup = tx_root / ("g" * 32)
    backup.mkdir(parents=True)
    bak = backup / "art.bak"
    bak.write_text("attacker\n", encoding="utf-8")
    (backup / "art.prev.json").write_text(
        json.dumps(
            {"target": str(dest), "kind": "file", "backup": str(bak)}
        ),
        encoding="utf-8",
    )
    (tx_root / "current.json").write_text(
        json.dumps(
            {
                "status": "committing",
                "transaction_id": "g" * 32,
                "backup_dir": str(backup),
            }
        ),
        encoding="utf-8",
    )
    rollback_interrupted("project", project)
    assert dest.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_inspect_rejects_empty_artifact_list(tmp_path: Path) -> None:
    dest = tmp_path / ".omg" / "install" / "manifest.json"
    dest.parent.mkdir(parents=True)
    dest.write_text(
        json.dumps(
            {
                "schema": "omg-install-manifest/v1",
                "kind": "omg_install_manifest",
                "scope": "project",
                "artifacts": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = inspect_install_manifest(project_root=tmp_path, scope="project")
    assert payload["ok"] is False
    assert payload.get("installed") is False


def test_inspect_reports_symlinked_omg_parent(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=project,
        here=True,
        plugin=ROOT,
    )
    real = tmp_path / "moved-omg"
    omg = project / ".omg"
    omg.rename(real)
    try:
        omg.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    payload = inspect_install_manifest(project_root=project, scope="project")
    assert payload["ok"] is False
    assert payload.get("error")
