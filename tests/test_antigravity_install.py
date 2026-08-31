"""Live Antigravity plugin install/discovery evidence for #77."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    _ensure_mcp_registered,
    _mark_recovery_phase,
    _package_digest,
    _restore_plugin_tree_atomic,
    installed_plugin_path,
    load_ownership_receipt,
    mcp_registry_identity,
    persist_recovery_snapshot,
    persist_ownership_receipt,
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
mcp_settings = config / "mcp_config.json"
args = sys.argv[1:]

def rows():
    if not manifest.is_file():
        return []
    return json.loads(manifest.read_text()).get("imports", []) or []

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
    manifest.write_text(json.dumps({"imports": None}))
    sys.exit(0)
if args[:3] == ["mcp", "remove", "omg-tools"]:
    payload = json.loads(mcp_settings.read_text()) if mcp_settings.is_file() else {"mcpServers": {}}
    payload.setdefault("mcpServers", {}).pop("omg-tools", None)
    mcp_settings.write_text(json.dumps(payload))
    sys.exit(0)
if args[:3] == ["mcp", "enable", "omg-tools"]:
    payload = json.loads(mcp_settings.read_text()) if mcp_settings.is_file() else {"mcpServers": {}}
    row = payload.setdefault("mcpServers", {}).get("omg-tools")
    if not isinstance(row, dict): sys.exit(1)
    row["disabled"] = False
    mcp_settings.write_text(json.dumps(payload))
    sys.exit(0)
if args[:2] == ["mcp", "add"]:
    name_index = args.index("omg-tools")
    command = args[name_index + 1]
    command_args = args[name_index + 2:]
    config.mkdir(parents=True, exist_ok=True)
    payload = json.loads(mcp_settings.read_text()) if mcp_settings.is_file() else {"mcpServers": {}}
    payload.setdefault("mcpServers", {})["omg-tools"] = {"args": command_args, "command": command, "disabled": False, "env": {"OMG_TOOLS_NETWORK": "0"}}
    mcp_settings.write_text(json.dumps(payload))
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
if "-p" in args:
    prompt_index = args.index("-p") + 1
    if prompt_index >= len(args) or args[prompt_index].startswith("-"):
        sys.exit(2)
    if os.environ.get("AGY_FAIL_LIVE") == "1":
        sys.exit(7)
    if os.environ.get("AGY_OMIT_MCP") != "1":
        state = "ERROR" if os.environ.get("AGY_MCP_ERROR") == "1" else "DONE"
        info = {"parameters": {"ServerName": "omg-tools", "ToolName": "omg.tools.doctor"}}
        if state == "ERROR": info["error"] = {"message": "not enabled"}
        print(json.dumps({"event": "step_update", "step_update": {"state": state, "step_type": "tool", "tool_name": "call_mcp_tool", "tool_info": info}}))
    print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "OMG_INSTALL_LIVE_PROBE_OK"}}))
    sys.exit(0)
if "--print" in args:
    sys.exit(2)
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


@pytest.mark.parametrize(
    "failure", ["AGY_FAIL_LIVE", "AGY_OMIT_MCP", "AGY_MCP_ERROR", "AGY_OMIT_HOOK"]
)
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


def test_project_only_install_releases_last_central_antigravity_reference(
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
    assert not (home / ".gemini/config/plugins/oh-my-grok").exists()


def test_project_uninstall_releases_shared_reference_then_last_owner_removes_globals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()

    for project in (project_a, project_b):
        run_scoped_setup(
            runtime="antigravity",
            scope="project",
            project_root=project,
            here=True,
            plugin=ROOT,
            install_antigravity=True,
        )

    receipt = load_ownership_receipt(home)
    assert receipt is not None
    assert receipt["references"] == sorted(
        {
            str((project_a / ".omg/install/manifest.json").absolute()),
            str((project_b / ".omg/install/manifest.json").absolute()),
        }
    )
    stale_manifest = (project_a / ".omg/install/manifest.json").read_bytes()

    assert run_uninstall(yes=True, project_root=project_a, home=home / ".grok") == 0
    assert (home / ".gemini/config/plugins/oh-my-grok").is_dir()
    receipt = load_ownership_receipt(home)
    assert receipt is not None
    assert receipt["references"] == [
        str((project_b / ".omg/install/manifest.json").absolute())
    ]
    stale_path = project_a / ".omg/install/manifest.json"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(stale_manifest)
    stale_inspection = inspect_install_manifest(project_root=project_a, scope="project")
    assert stale_inspection["healthy"] is False
    assert stale_inspection["verified"] is False
    stale_path.unlink()

    assert run_uninstall(yes=True, project_root=project_b, home=home / ".grok") == 0
    assert not (home / ".gemini/config/plugins/oh-my-grok").exists()
    assert "omg-tools" not in (home / ".gemini/config/mcp_config.json").read_text()
    assert load_ownership_receipt(home) is None


@pytest.mark.parametrize("crash_after", ["mcp", "plugin", "receipt"])
def test_antigravity_uninstall_resumes_after_crash_between_mutation_and_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_after: str
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
    receipt = load_ownership_receipt(home)
    assert receipt is not None

    original_clear = __import__(
        "omg_cli.antigravity_install", fromlist=["clear_ownership_receipt"]
    ).clear_ownership_receipt

    def crash_runner(argv, **kwargs):
        result = subprocess.run(argv, **kwargs)
        command = list(argv)
        if crash_after == "mcp" and command[:3] == ["agy", "mcp", "remove"]:
            raise SystemExit("crash after MCP removal")
        if crash_after == "plugin" and command[:3] == ["agy", "plugin", "uninstall"]:
            raise SystemExit("crash after plugin removal")
        return result

    if crash_after == "receipt":
        def crash_clear(**kwargs):
            assert original_clear(**kwargs) is True
            raise SystemExit("crash after receipt removal")

        monkeypatch.setattr("omg_cli.antigravity_install.clear_ownership_receipt", crash_clear)

    with pytest.raises(SystemExit):
        uninstall_owned_plugin(
            expected_digest=receipt["plugin_digest"],
            expected_registry_identity=receipt["registry_identity"],
            expected_mcp_registry_identity=receipt["mcp_registry_identity"],
            runner=crash_runner,
            home=home,
        )

    if crash_after == "receipt":
        monkeypatch.setattr(
            "omg_cli.antigravity_install.clear_ownership_receipt", original_clear
        )
    assert uninstall_owned_plugin(
        expected_digest=receipt["plugin_digest"],
        expected_registry_identity=receipt["registry_identity"],
        expected_mcp_registry_identity=receipt["mcp_registry_identity"],
        home=home,
    )
    assert not installed_plugin_path(home).exists()
    assert load_ownership_receipt(home) is None


@pytest.mark.parametrize(
    "crash_after", ["plugin_install", "plugin_enable", "mcp_add", "receipt_write"]
)
def test_antigravity_install_rollback_accepts_write_ahead_crash_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_after: str
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    recovery = home / ".gemini/config/.omg-transactions/install-crash"
    persist_recovery_snapshot(recovery, home=home)
    digest = _package_digest(ROOT)
    assert digest is not None

    _mark_recovery_phase(recovery, "installing_plugin", intended_plugin_digest=digest)
    subprocess.run(["agy", "plugin", "install", str(ROOT)], check=True)
    if crash_after != "plugin_install":
        _mark_recovery_phase(recovery, "enabling_plugin", intended_plugin_digest=digest)
        subprocess.run(["agy", "plugin", "enable", "oh-my-grok"], check=True)
    if crash_after in {"mcp_add", "receipt_write"}:
        _mark_recovery_phase(recovery, "registering_mcp", intended_plugin_digest=digest)
        _ensure_mcp_registered(home=home, runner=subprocess.run)
    if crash_after == "receipt_write":
        registry = plugin_registry_identity(home=home)
        mcp = mcp_registry_identity(home)
        assert registry is not None and mcp is not None
        _mark_recovery_phase(recovery, "writing_receipt", intended_plugin_digest=digest)
        persist_ownership_receipt(
            plugin_digest=digest,
            registry_identity=registry,
            mcp_registry_identity=mcp,
            references=[],
            home=home,
        )

    assert restore_recovery_snapshot(recovery) is True
    assert not installed_plugin_path(home).exists()
    assert plugin_registry_identity(home=home) is None
    assert mcp_registry_identity(home) is None
    assert load_ownership_receipt(home) is None
    for name in ("import_manifest.json", "config.json", "mcp_config.json"):
        assert not (home / ".gemini/config" / name).exists()


def test_antigravity_install_crash_recovery_preserves_unrelated_registry_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    recovery = home / ".gemini/config/.omg-transactions/install-crash"
    persist_recovery_snapshot(recovery, home=home)
    digest = _package_digest(ROOT)
    assert digest is not None
    _mark_recovery_phase(recovery, "installing_plugin", intended_plugin_digest=digest)
    subprocess.run(["agy", "plugin", "install", str(ROOT)], check=True)
    _mark_recovery_phase(recovery, "enabling_plugin", intended_plugin_digest=digest)
    subprocess.run(["agy", "plugin", "enable", "oh-my-grok"], check=True)
    config_path = home / ".gemini/config/config.json"
    config = json.loads(config_path.read_text())
    config["unrelated_user_key"] = {"preserve": True}
    config_path.write_text(json.dumps(config))

    assert restore_recovery_snapshot(recovery) is True
    restored = json.loads(config_path.read_text())
    assert restored["unrelated_user_key"] == {"preserve": True}
    assert "oh-my-grok" not in restored.get("plugins", {})


def test_antigravity_install_crash_recovery_preserves_unrelated_mcp_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    recovery = home / ".gemini/config/.omg-transactions/install-crash"
    persist_recovery_snapshot(recovery, home=home)
    digest = _package_digest(ROOT)
    assert digest is not None
    _mark_recovery_phase(recovery, "installing_plugin", intended_plugin_digest=digest)
    subprocess.run(["agy", "plugin", "install", str(ROOT)], check=True)
    _mark_recovery_phase(recovery, "enabling_plugin", intended_plugin_digest=digest)
    subprocess.run(["agy", "plugin", "enable", "oh-my-grok"], check=True)
    _mark_recovery_phase(recovery, "registering_mcp", intended_plugin_digest=digest)
    _ensure_mcp_registered(home=home, runner=subprocess.run)
    mcp_path = home / ".gemini/config/mcp_config.json"
    mcp = json.loads(mcp_path.read_text())
    mcp["mcpServers"]["foreign-tools"] = {"command": "/usr/bin/true"}
    mcp_path.write_text(json.dumps(mcp))

    assert restore_recovery_snapshot(recovery) is True
    restored = json.loads(mcp_path.read_text())
    assert restored["mcpServers"]["foreign-tools"] == {"command": "/usr/bin/true"}
    assert "omg-tools" not in restored["mcpServers"]


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
    from omg_cli.antigravity_install import install_plugin

    project = tmp_path / "project"
    backup = project / ".omg/install/tx/deadbeef"
    install_plugin(ROOT, recovery_dir=backup)
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


def test_recovery_preserves_unsealed_partial_plugin_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    backup = tmp_path / "project/.omg/install/tx/partial"
    persist_recovery_snapshot(backup)
    partial = installed_plugin_path(home)
    partial.mkdir(parents=True)
    (partial / "plugin.json").write_text("{", encoding="utf-8")

    assert restore_recovery_snapshot(backup) is False
    assert partial.exists()


def test_plugin_tree_restore_retries_after_staging_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    from omg_cli.antigravity_install import install_plugin

    install_plugin(ROOT)
    target = installed_plugin_path(home)
    digest = _package_digest(target)
    assert digest is not None
    backup = tmp_path / "plugin-backup"
    shutil.copytree(target, backup)
    shutil.rmtree(target)
    real_copytree = shutil.copytree
    calls = {"n": 0}

    def crash_once(src: Path, dst: Path, *args: object, **kwargs: object):
        source = Path(src)
        destination = Path(dst)
        top_level = (
            source == backup
            and destination.parent == target.parent
            and destination.name.startswith(f".{target.name}.restore-")
        )
        copied = real_copytree(src, dst, *args, **kwargs)
        if top_level:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("crash after staging copy")
        return copied

    monkeypatch.setattr(shutil, "copytree", crash_once)
    with pytest.raises(OSError, match="crash after staging copy"):
        _restore_plugin_tree_atomic(backup, target, digest)
    leftovers = [
        path
        for path in target.parent.iterdir()
        if path.name.startswith(f".{target.name}.restore-")
    ]
    assert leftovers == []
    assert not target.exists()
    _restore_plugin_tree_atomic(backup, target, digest)
    assert _package_digest(target) == digest


def test_incomplete_fresh_install_tree_is_removed_on_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    backup = tmp_path / "project/.omg/install/tx/partial-install"
    persist_recovery_snapshot(backup)
    digest = _package_digest(ROOT)
    assert digest is not None
    _mark_recovery_phase(backup, "installing_plugin", intended_plugin_digest=digest)
    partial = installed_plugin_path(home)
    partial.mkdir(parents=True)
    (partial / "plugin.json").write_text("{", encoding="utf-8")

    assert restore_recovery_snapshot(backup) is True
    assert not partial.exists()
    leftovers = [
        path
        for path in partial.parent.iterdir()
        if path.name.startswith(f".{partial.name}.")
    ]
    assert leftovers == []


def test_recovery_allows_host_imported_at_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    backup = tmp_path / "project/.omg/install/tx/imported-at"
    persist_recovery_snapshot(backup)
    digest = _package_digest(ROOT)
    assert digest is not None
    subprocess.run(["agy", "plugin", "install", str(ROOT)], check=True)
    subprocess.run(["agy", "plugin", "enable", "oh-my-grok"], check=True)
    _mark_recovery_phase(backup, "registering_mcp", intended_plugin_digest=digest)
    manifest_path = home / ".gemini/config/import_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["imports"][0]["importedAt"] = "2026-08-31T00:00:00Z"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert restore_recovery_snapshot(backup) is True
    assert not installed_plugin_path(home).exists()


def test_recovery_resumes_after_one_registry_row_was_already_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _install_fake_agy(tmp_path, monkeypatch)
    backup = tmp_path / "project/.omg/install/tx/resume"
    from omg_cli.antigravity_install import install_plugin

    install_plugin(ROOT, recovery_dir=backup)
    # Simulate a crash after recovery restored the originally-absent MCP file,
    # while the plugin tree/import registry are still transaction post-state.
    (home / ".gemini/config/mcp_config.json").unlink()

    assert restore_recovery_snapshot(backup)
    assert not installed_plugin_path(home).exists()


def test_runtime_both_does_not_overclaim_top_level_live_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    result = run_scoped_setup(
        runtime="both",
        scope="project",
        project_root=project,
        here=True,
        plugin=ROOT,
        install_antigravity=True,
        install_hook=False,
        install_rules=False,
    )

    assert result["observed"] is True
    assert result["healthy"] is True
    assert result["verified"] is True
    assert result["live_verified"] is False
    assert result["runtime_evidence"]["antigravity"]["live_verified"] is True
    inspected = inspect_install_manifest(project_root=project)
    assert inspected["healthy"] is True
    assert inspected["live_verified"] is False
    assert inspected["runtime_evidence"]["antigravity"]["live_verified"] is True


def test_interrupted_recovery_uses_original_config_root_after_home_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_home = _install_fake_agy(tmp_path, monkeypatch)
    project = tmp_path / "project"
    backup = project / ".omg/install/tx/deadbeef"
    from omg_cli.antigravity_install import install_plugin

    install_plugin(ROOT, recovery_dir=backup)
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
                "previous_target_state": "absent",
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
    expected_mcp_registry = mcp_registry_identity(home)
    assert expected_digest is not None
    assert expected_registry is not None
    assert expected_mcp_registry is not None
    (target / "plugin.json").write_text("{}\n", encoding="utf-8")

    assert not uninstall_owned_plugin(
        expected_digest=expected_digest,
        expected_registry_identity=expected_registry,
        expected_mcp_registry_identity=expected_mcp_registry,
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
    expected_mcp_registry = mcp_registry_identity(home)
    assert expected_digest is not None
    assert expected_registry is not None
    assert expected_mcp_registry is not None
    registry = home / ".gemini/config/import_manifest.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["imports"][0]["review_race"] = True
    registry.write_text(json.dumps(payload), encoding="utf-8")

    assert not uninstall_owned_plugin(
        expected_digest=expected_digest,
        expected_registry_identity=expected_registry,
        expected_mcp_registry_identity=expected_mcp_registry,
        home=home,
    )
    assert target.exists()
