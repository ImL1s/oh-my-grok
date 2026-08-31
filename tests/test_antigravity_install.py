"""Live Antigravity plugin install/discovery evidence for #77."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from omg_cli.install_manifest import (
    InstallManifestError,
    inspect_install_manifest,
    rollback_interrupted,
    run_scoped_setup,
)
from omg_cli.antigravity_install import (
    AntigravityInstallError,
    _package_digest,
    installed_plugin_path,
    persist_recovery_snapshot,
    plugin_registry_identity,
    restore_recovery_snapshot,
    uninstall_owned_plugin,
)
from omg_cli.uninstall_cmd import run_uninstall


ROOT = Path(__file__).resolve().parents[1]


def _install_fake_agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "agy"
    executable.write_text(
        """#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path

home = Path(os.environ["HOME"])
config = home / ".gemini" / "config"
plugins = config / "plugins"
manifest = config / "import_manifest.json"
settings = config / "config.json"
args = sys.argv[1:]

def rows():
    if not manifest.is_file():
        return []
    return json.loads(manifest.read_text()).get("imports", [])

if args[:2] == ["plugin", "validate"]:
    target = Path(args[2])
    sys.exit(0 if (target / "plugin.json").is_file() else 1)
if args[:2] == ["plugin", "install"]:
    if os.environ.get("AGY_FAIL_INSTALL") == "1":
        sys.exit(9)
    source = Path(args[2])
    destination = plugins / "oh-my-grok"
    config.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", ".omg"))
    components = []
    if (source / "skills").is_dir(): components.append("skills")
    if (source / "agents").is_dir(): components.append("agents")
    if (source / "hooks.json").is_file() and os.environ.get("AGY_OMIT_HOOK") != "1": components.append("hooks")
    if (source / "mcp_config.json").is_file(): components.append("mcpServers")
    manifest.write_text(json.dumps({"imports": [{"name": "oh-my-grok", "source": "antigravity", "components": components}]}))
    print("installed")
    sys.exit(0)
if args[:3] == ["plugin", "enable", "oh-my-grok"]:
    config.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"plugins": {"oh-my-grok": {"enabled": True}}}))
    sys.exit(0)
if args[:3] == ["plugin", "disable", "oh-my-grok"]:
    config.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"plugins": {"oh-my-grok": {"enabled": False}}}))
    sys.exit(0)
if args[:3] == ["plugin", "uninstall", "oh-my-grok"]:
    destination = plugins / "oh-my-grok"
    if destination.exists():
        shutil.rmtree(destination)
    config.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"imports": []}))
    sys.exit(0)
if args[:2] == ["plugin", "list"]:
    print(json.dumps({"imports": rows()}))
    sys.exit(0)
if args == ["agent"]:
    if os.environ.get("AGY_FAIL_AGENT") == "1":
        sys.exit(8)
    if any(row.get("name") == "oh-my-grok" for row in rows()):
        print("omg-explore\\nomg-executor\\nomg-vision")
        sys.exit(0)
    sys.exit(1)
if "--print" in args:
    if os.environ.get("AGY_FAIL_LIVE") == "1":
        sys.exit(7)
    if os.environ.get("AGY_OMIT_MCP") != "1":
        print(json.dumps({"type": "tool_call", "tool": "omg.tools.doctor"}))
    print(json.dumps({"type": "result", "result": "OMG_INSTALL_LIVE_PROBE_OK"}))
    sys.exit(0)
sys.exit(2)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return home


@pytest.mark.parametrize("scope", ["project", "user"])
def test_antigravity_setup_installs_and_proves_live_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    result = run_scoped_setup(
        runtime="antigravity",
        scope=scope,
        project_root=project if scope == "project" else None,
        here=True,
        plugin=ROOT,
        install_antigravity=True,
    )
    assert result["observed"] is True
    assert result["healthy"] is True
    assert result["live_verified"] is True
    assert (home / ".gemini/config/plugins/oh-my-grok/plugin.json").is_file()
    inspected = inspect_install_manifest(
        project_root=project if scope == "project" else None,
        scope=scope,
    )
    assert inspected["enabled"] is True
    assert inspected["loadable"] is True
    assert inspected["observed"] is True
    assert inspected["healthy"] is True
    assert inspected["live_verified"] is True
    if scope == "project":
        from omg_cli import doctor as doctor_mod

        monkeypatch.setattr("omg_cli.cli_util.project_root", lambda: project)
        name, level, detail = doctor_mod.check_install_manifest()
        assert name == "install manifest"
        assert level == "ok"
        assert "observed=True" in detail
        assert "healthy=True" in detail
        assert "live_verified=True" in detail


def test_antigravity_failed_discovery_rolls_back_new_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("AGY_FAIL_AGENT", "1")
    with pytest.raises(InstallManifestError, match="E_TX"):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=project,
            here=True,
            plugin=ROOT,
            install_antigravity=True,
        )
    assert not (home / ".gemini/config/plugins/oh-my-grok").exists()
    assert not (project / ".omg/install/manifest.json").exists()


@pytest.mark.parametrize("failure", ["AGY_FAIL_LIVE", "AGY_OMIT_MCP", "AGY_OMIT_HOOK"])
def test_antigravity_live_verified_fails_closed_without_execution_tool_or_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv(failure, "1")
    with pytest.raises(InstallManifestError, match="E_TX"):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=project,
            here=True,
            plugin=ROOT,
            install_antigravity=True,
        )
    assert not (home / ".gemini/config/plugins/oh-my-grok").exists()


def test_antigravity_foreign_same_name_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    destination = home / ".gemini/config/plugins/oh-my-grok"
    destination.mkdir(parents=True)
    (destination / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "foreign"}), encoding="utf-8"
    )
    config = home / ".gemini/config"
    (config / "import_manifest.json").write_text(
        json.dumps({"imports": [{"name": "oh-my-grok", "source": "foreign"}]}),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(InstallManifestError, match="E_CONFLICT"):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=project,
            here=True,
            plugin=ROOT,
            install_antigravity=True,
        )
    assert json.loads((destination / "plugin.json").read_text())["version"] == "foreign"


def test_project_manifest_does_not_own_machine_global_antigravity_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=project,
        here=True,
        plugin=ROOT,
        install_antigravity=True,
    )
    assert (home / ".gemini/config/plugins/oh-my-grok").is_dir()
    assert run_uninstall(yes=True, project_root=project, home=home / ".grok") == 0
    assert (home / ".gemini/config/plugins/oh-my-grok").exists()


def test_user_manifest_centrally_owns_and_uninstalls_antigravity_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    run_scoped_setup(
        runtime="antigravity",
        scope="user",
        project_root=None,
        here=True,
        plugin=ROOT,
        install_antigravity=True,
    )
    assert (
        run_uninstall(
            yes=True,
            include_user_manifest=True,
            project_root=None,
            home=home / ".grok",
        )
        == 0
    )
    assert not (home / ".gemini/config/plugins/oh-my-grok").exists()


def test_manifest_preserves_drifted_antigravity_plugin_on_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=project,
        here=True,
        plugin=ROOT,
        install_antigravity=True,
    )
    plugin_json = home / ".gemini/config/plugins/oh-my-grok/plugin.json"
    plugin_json.write_text(plugin_json.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert run_uninstall(yes=True, project_root=project, home=home / ".grok") == 0
    assert plugin_json.is_file()


def test_owned_upgrade_refreshes_stale_antigravity_registry_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=project,
        here=True,
        plugin=ROOT,
        install_antigravity=True,
    )
    candidate = tmp_path / "candidate"
    shutil.copytree(ROOT, candidate, ignore=shutil.ignore_patterns(".git", ".omg"))
    (candidate / "hooks.json").write_text("{}\n", encoding="utf-8")
    result = run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=project,
        here=True,
        plugin=candidate,
        install_antigravity=True,
    )
    ag = result["runtime_evidence"]["antigravity"]
    assert ag["registry_refreshed"] is True
    assert "hooks" in ag["registry_components"]
    registry = json.loads(
        (home / ".gemini/config/import_manifest.json").read_text(encoding="utf-8")
    )
    assert "hooks" in registry["imports"][0]["components"]


def test_failed_registry_refresh_restores_prior_plugin_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    run_scoped_setup(
        runtime="antigravity",
        scope="project",
        project_root=project,
        here=True,
        plugin=ROOT,
        install_antigravity=True,
    )
    registry_path = home / ".gemini/config/import_manifest.json"
    registry_before = registry_path.read_bytes()
    plugin_before = (home / ".gemini/config/plugins/oh-my-grok/plugin.json").read_bytes()
    candidate = tmp_path / "candidate"
    shutil.copytree(ROOT, candidate, ignore=shutil.ignore_patterns(".git", ".omg"))
    (candidate / "hooks.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("AGY_FAIL_INSTALL", "1")
    with pytest.raises(InstallManifestError, match="E_TX"):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=project,
            here=True,
            plugin=candidate,
            install_antigravity=True,
        )
    assert registry_path.read_bytes() == registry_before
    assert (home / ".gemini/config/plugins/oh-my-grok/plugin.json").read_bytes() == plugin_before


def test_interrupted_fresh_install_receipt_recovers_exact_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    from omg_cli.antigravity_install import install_plugin, persist_recovery_snapshot

    project = tmp_path / "project"
    backup = project / ".omg/install/tx/deadbeef"
    persist_recovery_snapshot(backup)
    install_plugin(ROOT)
    marker = backup.parent / "current.json"
    marker.write_text(
        json.dumps(
            {
                "status": "committing",
                "transaction_id": "deadbeef",
                "backup_dir": str(backup),
                "runtime": "antigravity",
                "scope": "project",
                "agy_recovery_snapshot": True,
            }
        ),
        encoding="utf-8",
    )
    recovered = rollback_interrupted("project", project)
    assert recovered["ok"] is True
    assert recovered["rolled_back"] is True
    assert not (home / ".gemini/config/plugins/oh-my-grok").exists()


def test_interrupted_recovery_uses_original_config_root_after_home_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    backup = project / ".omg/install/tx/deadbeef"
    from omg_cli.antigravity_install import install_plugin, persist_recovery_snapshot

    persist_recovery_snapshot(backup)
    install_plugin(ROOT)
    marker = backup.parent / "current.json"
    marker.write_text(
        json.dumps(
            {
                "status": "committing",
                "transaction_id": "deadbeef",
                "backup_dir": str(backup),
                "runtime": "antigravity",
                "scope": "project",
                "agy_recovery_snapshot": True,
            }
        ),
        encoding="utf-8",
    )
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    monkeypatch.setenv("HOME", str(other_home))
    recovered = rollback_interrupted("project", project)
    assert recovered["ok"] is True
    assert not (original_home / ".gemini/config/plugins/oh-my-grok").exists()
    assert not (other_home / ".gemini/config/plugins/oh-my-grok").exists()


def test_config_symlink_ancestor_is_rejected_before_host_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    real = tmp_path / "real-config"
    real.mkdir()
    (home / ".gemini").mkdir()
    (home / ".gemini/config").symlink_to(real, target_is_directory=True)
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(InstallManifestError, match="E_TX"):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=project,
            here=True,
            plugin=ROOT,
            install_antigravity=True,
        )
    assert not (real / "plugins/oh-my-grok").exists()


def test_snapshot_rejects_symlink_inside_installed_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    from omg_cli.antigravity_install import install_plugin

    install_plugin(ROOT)
    target = installed_plugin_path(home)
    (target / "unsafe-link").symlink_to(target / "plugin.json")

    with pytest.raises(AntigravityInstallError, match="symlink"):
        persist_recovery_snapshot(tmp_path / "project/.omg/install/tx/unsafe")


def test_restore_rejects_recorded_target_with_symlink_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_agy(tmp_path, monkeypatch)
    real_config = tmp_path / "real-config"
    real_config.mkdir()
    linked_config = tmp_path / "linked-config"
    linked_config.symlink_to(real_config, target_is_directory=True)
    backup = tmp_path / "project/.omg/install/tx/unsafe"
    backup.mkdir(parents=True)
    (backup / "current.json").write_text(
        json.dumps(
            {
                "schema": "omg-agy-recovery/v1",
                "config_root": str(linked_config),
                "target": str(linked_config / "plugins/oh-my-grok"),
                "previous_plugin_present": False,
                "previous_plugin_digest": None,
                "registry": [],
            }
        ),
        encoding="utf-8",
    )

    assert restore_recovery_snapshot(backup) is False
    assert not (real_config / "plugins/oh-my-grok").exists()


def test_uninstall_revalidates_plugin_tree_immediately_before_host_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    from omg_cli.antigravity_install import install_plugin

    install_plugin(ROOT)
    target = installed_plugin_path(home)
    expected_digest = _package_digest(target)
    expected_registry = plugin_registry_identity(home=home)
    assert expected_digest is not None
    assert expected_registry is not None
    (target / "plugin.json").write_text("{}\n", encoding="utf-8")

    assert not uninstall_owned_plugin(
        expected_digest=expected_digest,
        expected_registry_identity=expected_registry,
        home=home,
    )
    assert target.exists()


def test_uninstall_revalidates_registry_identity_immediately_before_host_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    from omg_cli.antigravity_install import install_plugin

    install_plugin(ROOT)
    target = installed_plugin_path(home)
    expected_digest = _package_digest(target)
    expected_registry = plugin_registry_identity(home=home)
    assert expected_digest is not None
    assert expected_registry is not None
    registry = home / ".gemini/config/import_manifest.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["imports"][0]["review_race"] = True
    registry.write_text(json.dumps(payload), encoding="utf-8")

    assert not uninstall_owned_plugin(
        expected_digest=expected_digest,
        expected_registry_identity=expected_registry,
        home=home,
    )
    assert target.exists()
