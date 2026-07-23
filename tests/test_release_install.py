"""Release archive, checksum, and bootstrap UX behavior locks."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from omg_cli.setup_cmd import (
    InstallError,
    compute_package_identity,
    extract_release_archive,
    read_install_receipt,
    verify_release_archive,
)


ROOT = Path(__file__).resolve().parents[1]


def _archive(tmp_path: Path) -> tuple[Path, Path]:
    version = json.loads((ROOT / "plugin.json").read_text())["version"]
    asset = tmp_path / f"oh-my-grok-{version}.tar.gz"
    with tarfile.open(asset, "w:gz") as tf:
        for rel in (
            "plugin.json",
            ".mcp.json",
            ".lsp.json",
            "pyproject.toml",
            "omg_capabilities.lock.json",
            "README.md",
            "README.zh-TW.md",
            "LICENSE",
            "bin",
            "omg_cli",
            "hooks",
            "agents",
            "skills",
            "templates",
            "scripts",
        ):
            tf.add(ROOT / rel, arcname=f"oh-my-grok-{version}/{rel}", recursive=True)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{digest}  {asset.name}\n", encoding="utf-8")
    return asset, sums


def test_manual_offline_archive_uses_checksum_and_same_package_identity(tmp_path):
    asset, sums = _archive(tmp_path)
    verified = verify_release_archive(asset, sums)
    assert verified["asset_sha256"] == hashlib.sha256(asset.read_bytes()).hexdigest()
    extracted = extract_release_archive(asset, tmp_path / "unpack")
    identity = compute_package_identity(extracted)
    assert identity["digest"] == compute_package_identity(ROOT)["digest"]
    inventory = {row["path"] for row in identity["inventory"]}
    assert {".mcp.json", ".lsp.json"} <= inventory


def test_checksum_mismatch_rejects_before_extraction(tmp_path):
    asset, sums = _archive(tmp_path)
    sums.write_text(f"{'0' * 64}  {asset.name}\n", encoding="utf-8")
    with pytest.raises(InstallError, match="checksum"):
        verify_release_archive(asset, sums)


def test_duplicate_checksum_entry_is_rejected(tmp_path):
    asset, sums = _archive(tmp_path)
    line = sums.read_text()
    sums.write_text(line + line)
    with pytest.raises(InstallError, match="exactly one"):
        verify_release_archive(asset, sums)


def test_tar_path_traversal_and_links_are_rejected(tmp_path):
    asset = tmp_path / "oh-my-grok-1.0.0.tar.gz"
    with tarfile.open(asset, "w:gz") as tf:
        data = b"owned"
        info = tarfile.TarInfo("../escape")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(InstallError, match="unsafe archive"):
        extract_release_archive(asset, tmp_path / "out")


def test_release_attest_cli_emits_verified_identity(tmp_path):
    asset, sums = _archive(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "release_attest.py"), "--asset", str(asset), "--checksums", str(sums)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["verified"] is True
    assert result["asset_sha256"] == hashlib.sha256(asset.read_bytes()).hexdigest()
    assert len(result["package_digest"]) == 64


def test_install_script_exposes_online_and_manual_same_engine_paths():
    script = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "api.github.com/repos/${REPOSITORY}/releases/latest" in script
    assert 'DOWNLOAD_BASE="${RELEASES_URL%/}/download/${SOURCE_TAG}"' in script
    assert 'download "$BASE_URL/SHA256SUMS"' not in script
    assert "--archive" in script and "--checksums" in script and "--offline" in script
    assert "omg_cli.setup_cmd" in script and "install-release" in script
    assert 'SOURCE_TAG="v${BASH_REMATCH[1]}"' in script
    assert "installed and exactly verified" in script
    assert "set -euo pipefail" in script


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _sealed_tool_path(tmp_path: Path) -> tuple[Path, Path]:
    """PATH with only explicit local tools; all network/package tools fail+audit."""

    tools = tmp_path / "sealed-bin"
    tools.mkdir()
    os.symlink(sys.executable, tools / "python3")
    for name in ("basename", "dirname", "mkdir", "mktemp", "rm"):
        target = shutil.which(name)
        assert target is not None
        os.symlink(target, tools / name)

    audit = tmp_path / "forbidden-tools.log"
    for name in ("curl", "wget", "pip", "pip3", "npm", "npx"):
        _write_executable(
            tools / name,
            f"""
            #!/bin/sh
            printf '%s\\n' {name!r} >> "$OMG_FORBIDDEN_TOOL_AUDIT"
            exit 97
            """,
        )

    _write_executable(
        tools / "grok",
        r'''
        #!/usr/bin/env python3
        import json
        import os
        import sys
        from pathlib import Path

        home = Path(os.environ["GROK_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        state_path = home / "fake-grok-state.json"

        def load():
            if not state_path.is_file():
                return {"installed": None, "enabled": False}
            return json.loads(state_path.read_text(encoding="utf-8"))

        def save(value):
            state_path.write_text(json.dumps(value), encoding="utf-8")

        def entry(value):
            root = Path(value["installed"])
            plugin = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
            return {
                "name": "oh-my-grok",
                "version": plugin["version"],
                "path": str(root),
                "source": str(root),
                "installPath": str(root),
                "enabled": bool(value["enabled"]),
                "trusted": True,
            }

        args = sys.argv[1:]
        state = load()
        if args == ["plugin", "list", "--json"]:
            print(json.dumps([entry(state)] if state["installed"] else []))
        elif args[:2] == ["plugin", "details"] and args[-1:] == ["--json"]:
            if not state["installed"]:
                raise SystemExit(1)
            print(json.dumps(entry(state)))
        elif args[:2] == ["plugin", "inspect"] and args[-1:] == ["--json"]:
            if not state["installed"]:
                raise SystemExit(1)
            print(json.dumps(entry(state)))
        elif args[:2] == ["plugin", "validate"]:
            root = Path(args[2])
            if not (root / "plugin.json").is_file():
                raise SystemExit(1)
            print("valid")
        elif args[:2] == ["plugin", "install"]:
            state = {"installed": str(Path(args[2]).resolve()), "enabled": False}
            save(state)
            print("installed")
        elif args[:2] == ["plugin", "uninstall"]:
            save({"installed": None, "enabled": False})
            print("uninstalled")
        elif args[:2] == ["plugin", "enable"]:
            if not state["installed"]:
                raise SystemExit(1)
            state["enabled"] = True
            save(state)
            if os.environ.get("OMG_FAKE_SKIP_CONFIG") != "1":
                (home / "config.toml").write_text(
                    '[plugins]\nenabled = ["oh-my-grok"]\n', encoding="utf-8"
                )
            print("enabled")
        elif args == ["inspect", "--json"]:
            payload = {"plugins": [entry(state)] if state["installed"] else [],
                       "skills": ["omg-autopilot"] if state["installed"] else []}
            print(json.dumps(payload))
        else:
            print(f"unsupported fake grok argv: {args!r}", file=sys.stderr)
            raise SystemExit(2)
        ''',
    )
    return tools, audit


def _run_no_checkout_offline_install(
    tmp_path: Path,
    *,
    break_strict_doctor: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    asset, sums = _archive(tmp_path)
    launcher_dir = tmp_path / "no-checkout-launcher"
    launcher_dir.mkdir()
    launcher = launcher_dir / "install.sh"
    shutil.copyfile(ROOT / "scripts" / "install.sh", launcher)
    launcher.chmod(0o755)
    work = tmp_path / "unrelated-cwd"
    work.mkdir()
    home = tmp_path / "isolated-home"
    grok_home = tmp_path / "isolated-grok-home"
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    tools, audit = _sealed_tool_path(tmp_path)
    env = {
        "HOME": str(home),
        "GROK_HOME": str(grok_home),
        "PATH": str(tools),
        "TMPDIR": str(temp_dir),
        "OMG_FORBIDDEN_TOOL_AUDIT": str(audit),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if break_strict_doctor:
        env["OMG_FAKE_SKIP_CONFIG"] = "1"
    proc = subprocess.run(
        [
            "/bin/bash",
            str(launcher),
            "--offline",
            "--archive",
            str(asset),
            "--checksums",
            str(sums),
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc, home, grok_home, audit


def _run_no_checkout_online_install(
    tmp_path: Path,
    *,
    release_tag: str,
    archive_version: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, list[str]]:
    """Run the no-argument bootstrap against a hermetic fake HTTPS surface."""

    asset, _original_sums = _archive(tmp_path)
    if archive_version is not None:
        renamed = tmp_path / f"oh-my-grok-{archive_version}.tar.gz"
        renamed.write_bytes(asset.read_bytes())
        asset = renamed
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    sums = tmp_path / "online-SHA256SUMS"
    sums.write_text(f"{digest}  {asset.name}\n", encoding="utf-8")
    release_json = tmp_path / "latest-release.json"
    release_json.write_text(json.dumps({"tag_name": release_tag}), encoding="utf-8")

    launcher_dir = tmp_path / "no-checkout-online-launcher"
    launcher_dir.mkdir()
    launcher = launcher_dir / "install.sh"
    shutil.copyfile(ROOT / "scripts" / "install.sh", launcher)
    launcher.chmod(0o755)
    work = tmp_path / "unrelated-online-cwd"
    work.mkdir()
    home = tmp_path / "isolated-online-home"
    grok_home = tmp_path / "isolated-online-grok-home"
    temp_dir = tmp_path / "online-tmp"
    temp_dir.mkdir()
    tools, forbidden_audit = _sealed_tool_path(tmp_path)
    network_audit = tmp_path / "network-urls.log"
    _write_executable(
        tools / "curl",
        r'''
        #!/usr/bin/env python3
        import os
        import shutil
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        try:
            destination = Path(args[args.index("--output") + 1])
        except (ValueError, IndexError):
            print("fake curl requires --output", file=sys.stderr)
            raise SystemExit(2)
        url = args[-1]
        Path(os.environ["OMG_NETWORK_URL_AUDIT"]).open("a", encoding="utf-8").write(url + "\n")
        fixtures = {
            os.environ["OMG_TEST_API_URL"]: Path(os.environ["OMG_TEST_RELEASE_JSON"]),
            os.environ["OMG_TEST_SUMS_URL"]: Path(os.environ["OMG_TEST_SUMS"]),
            os.environ["OMG_TEST_ASSET_URL"]: Path(os.environ["OMG_TEST_ASSET"]),
        }
        source = fixtures.get(url)
        if source is None:
            print(f"unexpected fake-network URL: {url}", file=sys.stderr)
            raise SystemExit(96)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        ''',
    )
    api_url = "https://api.test/repos/ImL1s/oh-my-grok/releases/latest"
    releases_url = "https://download.test/ImL1s/oh-my-grok/releases"
    tagged_base = f"{releases_url}/download/{release_tag}"
    env = {
        "HOME": str(home),
        "GROK_HOME": str(grok_home),
        "PATH": str(tools),
        "TMPDIR": str(temp_dir),
        "OMG_FORBIDDEN_TOOL_AUDIT": str(forbidden_audit),
        "OMG_NETWORK_URL_AUDIT": str(network_audit),
        "OMG_INSTALL_LATEST_API_URL": api_url,
        "OMG_INSTALL_RELEASES_URL": releases_url,
        "OMG_TEST_API_URL": api_url,
        "OMG_TEST_RELEASE_JSON": str(release_json),
        "OMG_TEST_SUMS_URL": f"{tagged_base}/SHA256SUMS",
        "OMG_TEST_SUMS": str(sums),
        "OMG_TEST_ASSET_URL": f"{tagged_base}/{asset.name}",
        "OMG_TEST_ASSET": str(asset),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = subprocess.run(
        ["/bin/bash", str(launcher)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    urls = network_audit.read_text(encoding="utf-8").splitlines() if network_audit.exists() else []
    assert not forbidden_audit.exists(), (
        forbidden_audit.read_text(encoding="utf-8") if forbidden_audit.exists() else ""
    )
    return proc, home, grok_home, asset, urls


def test_copied_bootstrap_installs_release_without_checkout_or_network(tmp_path):
    proc, home, grok_home, audit = _run_no_checkout_offline_install(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "installed and exactly verified" in proc.stdout
    assert not audit.exists(), audit.read_text(encoding="utf-8") if audit.exists() else ""

    current = grok_home / "omg" / "current"
    current_receipt = grok_home / "omg" / "current-receipt"
    cli = home / ".local" / "bin" / "omg"
    assert current.is_symlink() and current_receipt.is_symlink() and cli.is_symlink()
    stage = current.resolve(strict=True)
    assert grok_home.resolve() in stage.parents
    assert ROOT.resolve() not in stage.parents
    assert (stage / ".mcp.json").is_file()
    assert (stage / ".lsp.json").is_file()
    receipt = read_install_receipt(current_receipt.resolve(strict=True))
    version_value = json.loads((stage / "plugin.json").read_text(encoding="utf-8"))["version"]
    assert receipt["source"]["tag"] == f"v{version_value}"
    assert receipt["source"]["asset_name"] == f"oh-my-grok-{version_value}.tar.gz"

    env = {
        "HOME": str(home),
        "GROK_HOME": str(grok_home),
        "PATH": str((tmp_path / "sealed-bin")),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    version = subprocess.run(
        [str(cli), "--version"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert version.returncode == 0, version.stderr
    assert version_value in version.stdout


def test_bootstrap_never_prints_success_when_strict_doctor_rolls_back(tmp_path):
    proc, home, grok_home, audit = _run_no_checkout_offline_install(
        tmp_path,
        break_strict_doctor=True,
    )
    assert proc.returncode != 0
    assert "installed and exactly verified" not in proc.stdout
    assert "doctor" in (proc.stdout + proc.stderr).lower()
    assert not (grok_home / "omg" / "current").exists()
    assert not os.path.lexists(home / ".local" / "bin" / "omg")
    assert not audit.exists(), audit.read_text(encoding="utf-8") if audit.exists() else ""


def test_online_bootstrap_resolves_once_and_downloads_both_files_from_exact_tag(tmp_path):
    version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
    tag = f"v{version}"
    proc, home, grok_home, asset, urls = _run_no_checkout_online_install(
        tmp_path,
        release_tag=tag,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "installed and exactly verified" in proc.stdout
    assert urls == [
        "https://api.test/repos/ImL1s/oh-my-grok/releases/latest",
        f"https://download.test/ImL1s/oh-my-grok/releases/download/{tag}/SHA256SUMS",
        f"https://download.test/ImL1s/oh-my-grok/releases/download/{tag}/{asset.name}",
    ]
    receipt = read_install_receipt((grok_home / "omg" / "current-receipt").resolve(strict=True))
    assert receipt["source"]["tag"] == tag
    assert receipt["source"]["uri"] == (
        f"https://download.test/ImL1s/oh-my-grok/releases/tag/{tag}"
    )
    assert (home / ".local" / "bin" / "omg").is_symlink()


def test_online_bootstrap_rejects_tag_archive_mismatch_without_success_or_asset_fetch(tmp_path):
    proc, home, grok_home, _asset, urls = _run_no_checkout_online_install(
        tmp_path,
        release_tag="v9.9.9",
    )
    assert proc.returncode != 0
    assert "differs from checksum archive" in (proc.stdout + proc.stderr)
    assert "installed and exactly verified" not in proc.stdout
    assert urls == [
        "https://api.test/repos/ImL1s/oh-my-grok/releases/latest",
        "https://download.test/ImL1s/oh-my-grok/releases/download/v9.9.9/SHA256SUMS",
    ]
    assert not (grok_home / "omg" / "current").exists()
    assert not os.path.lexists(home / ".local" / "bin" / "omg")


def test_online_bootstrap_rejects_archive_plugin_version_mismatch_before_mutation(tmp_path):
    proc, home, grok_home, asset, urls = _run_no_checkout_online_install(
        tmp_path,
        release_tag="v9.9.9",
        archive_version="9.9.9",
    )
    assert proc.returncode != 0
    assert "archive filename version differs from package identity" in (proc.stdout + proc.stderr)
    assert "installed and exactly verified" not in proc.stdout
    assert urls[-2:] == [
        "https://download.test/ImL1s/oh-my-grok/releases/download/v9.9.9/SHA256SUMS",
        f"https://download.test/ImL1s/oh-my-grok/releases/download/v9.9.9/{asset.name}",
    ]
    assert not (grok_home / "omg" / "current").exists()
    assert not (grok_home / "omg" / "releases").exists()
    assert not os.path.lexists(home / ".local" / "bin" / "omg")
