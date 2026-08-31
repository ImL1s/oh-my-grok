"""Behavior locks for the immutable OMG install transaction."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from omg_cli.setup_cmd import (
    InstallError,
    _copy_package_to_stage,
    _default_doctor_probe,
    _verify_immutable_stage,
    compute_package_identity,
    install_package,
    posix_launcher_bytes,
    read_install_receipt,
    stage_immutable_package,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeGrok:
    """Small host adapter that exposes the plugin snapshot selected by install."""

    def __init__(self) -> None:
        self.installed: Path | None = None
        self.enabled = False
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        args = [str(item) for item in argv]
        self.calls.append(args)
        if args[:4] == ["grok", "plugin", "list", "--json"]:
            rows = []
            if self.installed is not None:
                version = json.loads((self.installed / "plugin.json").read_text())["version"]
                rows.append(
                    {
                        "name": "oh-my-grok",
                        "version": version,
                        "path": str(self.installed),
                        "source": str(self.installed),
                        "enabled": self.enabled,
                    }
                )
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if args[:3] == ["grok", "plugin", "validate"]:
            return SimpleNamespace(returncode=0, stdout="valid\n", stderr="")
        if args[:3] == ["grok", "plugin", "install"]:
            self.installed = Path(args[3]).resolve()
            return SimpleNamespace(returncode=0, stdout="installed\n", stderr="")
        if args[:3] == ["grok", "plugin", "uninstall"]:
            self.installed = None
            self.enabled = False
            return SimpleNamespace(returncode=0, stdout="removed\n", stderr="")
        if args[:3] == ["grok", "plugin", "enable"]:
            self.enabled = True
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if args[:3] == ["grok", "inspect", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"skills": ["omg-autopilot"], "plugin": "oh-my-grok"}),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {args!r}")


class CopyingFakeGrok:
    """Match current Grok: install copies bytes, uninstall deletes that copy."""

    def __init__(self, grok_home: Path) -> None:
        self.grok_home = grok_home
        self.installed: Path | None = None
        self.source: Path | None = None
        self.enabled = False
        self.calls: list[list[str]] = []
        self.reported_path: Path | None = None
        self.install_count = 0
        self.corrupt_install_numbers: set[int] = set()

    def __call__(self, argv, **_kwargs):
        args = [str(item) for item in argv]
        self.calls.append(args)
        if args[:4] == ["grok", "plugin", "list", "--json"]:
            rows = []
            if self.installed is not None:
                version = json.loads((self.installed / "plugin.json").read_text())["version"]
                rows.append(
                    {
                        "name": "oh-my-grok",
                        "version": version,
                        "path": str(self.reported_path or self.installed),
                        "source": str(self.source),
                        "enabled": self.enabled,
                    }
                )
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if args[:3] == ["grok", "plugin", "validate"]:
            return SimpleNamespace(returncode=0, stdout="valid\n", stderr="")
        if args[:3] == ["grok", "plugin", "install"]:
            source = Path(args[3]).resolve()
            if not source.is_dir():
                return SimpleNamespace(returncode=1, stdout="", stderr="source missing\n")
            self.install_count += 1
            destination = self.grok_home / "installed-plugins" / "oh-my-grok-copy"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            for directory in (destination, *destination.rglob("*")):
                if directory.is_dir():
                    directory.chmod(0o755)
            self.source = source
            self.installed = destination.resolve()
            if self.install_count in self.corrupt_install_numbers:
                readme = self.installed / "README.md"
                readme.chmod(0o600)
                readme.write_bytes(readme.read_bytes() + b"\ncorrupt host copy\n")
            self.enabled = False
            return SimpleNamespace(returncode=0, stdout="installed copy\n", stderr="")
        if args[:3] == ["grok", "plugin", "uninstall"]:
            if self.installed is not None and self.installed.exists():
                shutil.rmtree(self.installed)
            self.installed = None
            self.source = None
            self.reported_path = None
            self.enabled = False
            return SimpleNamespace(returncode=0, stdout="removed copy\n", stderr="")
        if args[:3] == ["grok", "plugin", "enable"]:
            if self.installed is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="not installed\n")
            self.enabled = True
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if args[:3] == ["grok", "inspect", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"skills": ["omg-autopilot"], "plugin": "oh-my-grok"}),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {args!r}")


def _doctor_ok(_stage: Path, _env: dict[str, str]) -> dict[str, object]:
    return {"argv": ["omg", "doctor", "--strict"], "rc": 0, "stdout": "ok\n", "stderr": "", "valid": True}


@pytest.mark.parametrize(
    ("mode", "relaxed_rc", "expected_rc", "expected_calls"),
    [
        ("development", 0, 2, 2),
        ("development", 1, 1, 2),
        ("release", 0, 2, 2),
        ("release", 1, 1, 2),
    ],
)
def test_default_doctor_probe_dual_pass_same_matrix_for_both_modes(
    tmp_path,
    monkeypatch,
    mode,
    relaxed_rc,
    expected_rc,
    expected_calls,
):
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append([str(item) for item in argv])
        if "--strict" in argv:
            return SimpleNamespace(returncode=1, stdout="strict risks\n", stderr="")
        return SimpleNamespace(returncode=relaxed_rc, stdout="relaxed\n", stderr="")

    monkeypatch.setattr("omg_cli.setup_cmd.subprocess.run", fake_run)
    result = _default_doctor_probe(
        tmp_path,
        {"OMG_INSTALL_MODE": mode},
    )

    assert result["strict_rc"] == 1
    assert result["relaxed_rc"] == relaxed_rc
    assert result["rc"] == expected_rc
    assert len(calls) == expected_calls
    assert "--strict" in calls[0]
    if expected_calls == 2:
        assert "--strict" not in calls[1]


def test_package_identity_is_deterministic_and_version_aligned():
    first = compute_package_identity(ROOT)
    second = compute_package_identity(ROOT)
    assert first["digest"] == second["digest"]
    assert first["version"] == json.loads((ROOT / "plugin.json").read_text())["version"]
    assert first["inventory"] == sorted(first["inventory"], key=lambda row: row["path"].encode())
    assert any(
        row["path"] == "bin/omg"
        and row["executable"]
        and row["type"] == "regular_file"
        and row["mode"] == "0555"
        and row["source"] == "bin/omg"
        and row["owner"] == "oh-my-grok"
        for row in first["inventory"]
    )


def test_posix_launcher_bytes_strips_cr() -> None:
    crlf = b"#!/usr/bin/env python3\r\nprint(1)\r\n"
    assert posix_launcher_bytes("bin/omg", crlf) == b"#!/usr/bin/env python3\nprint(1)\n"
    assert posix_launcher_bytes("scripts/live_suite.sh", b"#!/bin/bash\r\n") == b"#!/bin/bash\n"
    assert posix_launcher_bytes("omg_cli/main.py", crlf) == crlf


def test_gitattributes_pins_posix_launchers_lf() -> None:
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "bin/omg text eol=lf" in text
    assert "scripts/*.sh text eol=lf" in text


def test_copy_package_to_stage_strips_crlf_from_bin_omg(tmp_path, monkeypatch) -> None:
    from omg_cli import setup_cmd

    src = tmp_path / "src"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "omg").write_bytes(b"#!/usr/bin/env python3\r\nprint(1)\r\n")
    ident = {
        "digest": "same",
        "version": "0.8.0",
        "inventory": [{"path": "bin/omg", "executable": True}],
    }
    monkeypatch.setattr(setup_cmd, "compute_package_identity", lambda *a, **k: ident)
    dest = tmp_path / "stage"
    _copy_package_to_stage(src, dest, ident)
    body = (dest / "bin" / "omg").read_bytes()
    assert b"\r" not in body
    assert body.startswith(b"#!/usr/bin/env python3\n")


def test_verify_immutable_stage_rejects_crlf_tampered_launcher(tmp_path: Path) -> None:
    """Staged launcher verification hashes raw bytes (CRLF must not share the LF digest)."""
    stage, identity = stage_immutable_package(ROOT, tmp_path / "releases")
    omg = stage / "bin" / "omg"
    for path in (stage, stage / "bin", omg):
        path.chmod(0o755 if path.is_dir() else 0o644)
    omg.write_bytes(omg.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(InstallError, match="inventory readback"):
        _verify_immutable_stage(stage, identity)


def test_install_stages_immutable_switches_cli_plugin_and_writes_receipt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    result = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )

    stage = Path(result["stage_path"])
    assert stage.is_dir() and not stage.is_symlink()
    assert (os.stat(stage).st_mode & 0o222) == 0
    assert (grok_home / "omg" / "current").resolve() == stage.resolve()
    assert (home / ".local" / "bin" / "omg").resolve() == (stage / "bin" / "omg").resolve()
    assert b"\r" not in (stage / "bin" / "omg").read_bytes()
    assert (stage / "bin" / "omg").read_bytes().startswith(b"#!/usr/bin/env python3\n")
    assert host.installed == stage.resolve() and host.enabled
    receipt = read_install_receipt(Path(result["receipt_path"]))
    assert receipt["status"] == "installed"
    assert receipt["source"]["package_digest"] == receipt["installed"]["package_digest"]
    assert receipt["installed"]["plugin_realpath"] == str(stage.resolve())
    assert receipt["installed"]["inventory"] == compute_package_identity(stage)["inventory"]
    assert receipt["receipt_hash"] == result["receipt_hash"]

    again = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    assert again["status"] == "already_installed"
    assert again["stage_path"] == result["stage_path"]


def test_exact_idempotent_setup_publishes_receipt_when_hook_wrapper_repaired(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    result = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    wrapper = grok_home / "hooks" / "omg_pretool_deny"
    assert wrapper.is_file()
    wrapper.write_text("#!/bin/sh\necho drifted\n", encoding="utf-8", newline="\n")
    wrapper.chmod(0o755)
    first_hash = result["receipt_hash"]

    again = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    assert again["status"] != "already_installed"
    assert again["receipt_hash"] != first_hash
    receipt = read_install_receipt(Path(again["receipt_path"]))
    owned = {
        str(row.get("path")): str(row.get("identity"))
        for row in receipt.get("owned_inventory", [])
        if isinstance(row, dict) and row.get("kind") == "global_hook"
    }
    import hashlib

    assert owned[str(wrapper)] == hashlib.sha256(wrapper.read_bytes()).hexdigest()
    assert wrapper.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert "echo drifted" not in wrapper.read_text(encoding="utf-8")


def test_install_accepts_and_reuses_host_managed_exact_copy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = CopyingFakeGrok(grok_home)

    result = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )

    stage = Path(result["stage_path"])
    assert host.installed is not None and host.installed != stage
    assert compute_package_identity(host.installed)["digest"] == result["package_digest"]
    receipt = read_install_receipt(Path(result["receipt_path"]))
    assert receipt["installed"]["plugin_realpath"] == str(host.installed)

    mutations_before = [
        call for call in host.calls if call[:3] in (
            ["grok", "plugin", "install"],
            ["grok", "plugin", "uninstall"],
        )
    ]
    again = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    mutations_after = [
        call for call in host.calls if call[:3] in (
            ["grok", "plugin", "install"],
            ["grok", "plugin", "uninstall"],
        )
    ]
    assert again["status"] == "already_installed"
    assert mutations_after == mutations_before


def test_reinstall_hides_prior_receipt_during_pending_doctor_probe(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = CopyingFakeGrok(grok_home)
    prior_source = tmp_path / "prior-source"
    shutil.copytree(
        ROOT,
        prior_source,
        ignore=shutil.ignore_patterns(
            ".git", ".omg", ".omx", ".pytest_cache", "__pycache__", "*.pyc"
        ),
    )
    readme = prior_source / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\nprior receipt fixture\n")
    receipt_pointer = grok_home / "omg" / "current-receipt"

    def transaction_probe(_stage: Path, env: dict[str, str]):
        if "OMG_EXPECTED_INSTALL_DIGEST" in env:
            assert not os.path.lexists(receipt_pointer)
        else:
            assert receipt_pointer.is_symlink()
        return _doctor_ok(_stage, env)

    prior = install_package(
        prior_source,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=transaction_probe,
        mode="development",
    )
    current = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=transaction_probe,
        mode="development",
    )

    assert current["package_digest"] == compute_package_identity(ROOT)["digest"]
    assert current["receipt_path"] != prior["receipt_path"]
    assert receipt_pointer.resolve(strict=True) == Path(current["receipt_path"])


def test_reinstall_preserves_concurrent_receipt_pointer_without_clobber(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = CopyingFakeGrok(grok_home)
    prior_source = tmp_path / "prior-source"
    shutil.copytree(
        ROOT,
        prior_source,
        ignore=shutil.ignore_patterns(
            ".git", ".omg", ".omx", ".pytest_cache", "__pycache__", "*.pyc"
        ),
    )
    readme = prior_source / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\nprior receipt fixture\n")
    prior = install_package(
        prior_source,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    prior_identity = compute_package_identity(prior_source)
    receipt_pointer = grok_home / "omg" / "current-receipt"
    foreign_target = tmp_path / "foreign-receipt.json"
    foreign_target.write_text("{}\n")

    def concurrent_probe(_stage: Path, env: dict[str, str]):
        if "OMG_EXPECTED_INSTALL_DIGEST" in env:
            assert not os.path.lexists(receipt_pointer)
            receipt_pointer.symlink_to(foreign_target)
        return _doctor_ok(_stage, env)

    with pytest.raises(InstallError, match="concurrent") as caught:
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=concurrent_probe,
            mode="development",
        )

    assert "rollback incomplete" in str(caught.value)
    assert receipt_pointer.is_symlink()
    assert os.readlink(receipt_pointer) == str(foreign_target)
    assert (grok_home / "omg" / "current").resolve(strict=True) == Path(
        prior["stage_path"]
    )
    assert (home / ".local" / "bin" / "omg").resolve(strict=True) == Path(
        prior["stage_path"]
    ) / "bin" / "omg"
    assert host.installed is not None and host.enabled
    assert compute_package_identity(host.installed)["digest"] == prior_identity["digest"]


def test_first_install_preserves_concurrent_receipt_pointer_without_clobber(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = CopyingFakeGrok(grok_home)
    receipt_pointer = grok_home / "omg" / "current-receipt"
    foreign_target = tmp_path / "foreign-receipt.json"
    foreign_target.write_text("{}\n")

    def concurrent_probe(_stage: Path, env: dict[str, str]):
        if "OMG_EXPECTED_INSTALL_DIGEST" in env:
            assert not os.path.lexists(receipt_pointer)
            receipt_pointer.symlink_to(foreign_target)
        return _doctor_ok(_stage, env)

    with pytest.raises(InstallError, match="concurrent") as caught:
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=concurrent_probe,
            mode="development",
        )

    assert "rollback incomplete" in str(caught.value)
    assert receipt_pointer.is_symlink()
    assert os.readlink(receipt_pointer) == str(foreign_target)
    assert not os.path.lexists(grok_home / "omg" / "current")
    assert not os.path.lexists(home / ".local" / "bin" / "omg")
    assert host.installed is None


def test_post_install_rejects_invalid_managed_path_even_with_valid_source(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = CopyingFakeGrok(grok_home)
    host.reported_path = grok_home / "installed-plugins" / "missing-managed-copy"

    with pytest.raises(InstallError, match="authoritative installed plugin"):
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=_doctor_ok,
            mode="development",
        )

    assert host.installed is None
    assert not os.path.lexists(grok_home / "omg" / "current")
    assert not os.path.lexists(home / ".local" / "bin" / "omg")


def test_rollback_restores_prior_plugin_after_host_deletes_managed_copy(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = CopyingFakeGrok(grok_home)
    prior_source = tmp_path / "prior-source"
    shutil.copytree(
        ROOT,
        prior_source,
        ignore=shutil.ignore_patterns(
            ".git", ".omg", ".omx", ".pytest_cache", "__pycache__", "*.pyc"
        ),
    )
    readme = prior_source / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\nprior managed copy fixture\n")
    prior_identity = compute_package_identity(prior_source)
    installed = host(
        ["grok", "plugin", "install", str(prior_source), "--trust"]
    )
    assert installed.returncode == 0
    host(["grok", "plugin", "enable", "oh-my-grok"])

    with pytest.raises(InstallError, match="injected") as caught:
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=_doctor_ok,
            mode="development",
            failpoint="before_pointer_switch",
        )

    assert "rollback incomplete" not in str(caught.value)
    assert host.installed is not None and host.enabled
    assert compute_package_identity(host.installed)["digest"] == prior_identity["digest"]
    assert not os.path.lexists(grok_home / "omg" / "current")
    assert not os.path.lexists(home / ".local" / "bin" / "omg")


def test_rollback_marks_incomplete_when_successful_restore_has_corrupt_bytes(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = CopyingFakeGrok(grok_home)
    installed = host(["grok", "plugin", "install", str(ROOT), "--trust"])
    assert installed.returncode == 0
    host(["grok", "plugin", "enable", "oh-my-grok"])
    host.corrupt_install_numbers.add(3)

    with pytest.raises(InstallError, match="rollback incomplete") as caught:
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=_doctor_ok,
            mode="development",
            failpoint="before_pointer_switch",
        )

    assert "prior plugin restore readback differs" in str(caught.value)
    assert host.installed is not None
    assert compute_package_identity(host.installed)["digest"] != compute_package_identity(ROOT)["digest"]
    assert not os.path.lexists(grok_home / "omg" / "current")
    assert not os.path.lexists(home / ".local" / "bin" / "omg")


@pytest.mark.parametrize("failpoint", ["before_pointer_switch", "after_pointer_switch"])
def test_failure_injection_rolls_back_joint_plugin_and_cli(tmp_path, monkeypatch, failpoint):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    with pytest.raises(InstallError, match="injected"):
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=_doctor_ok,
            mode="development",
            failpoint=failpoint,
        )

    assert not (grok_home / "omg" / "current").exists()
    assert not os.path.lexists(home / ".local" / "bin" / "omg")
    assert host.installed is None
    rollback = sorted((grok_home / "omg" / "receipts").glob("*.json"))
    assert rollback and read_install_receipt(rollback[-1])["status"] == "rolled_back"


def test_foreign_cli_is_preserved_and_install_refuses_before_host_mutation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    cli = home / ".local" / "bin" / "omg"
    cli.parent.mkdir(parents=True)
    cli.write_text("foreign\n")
    host = FakeGrok()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    with pytest.raises(InstallError, match="foreign CLI"):
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=_doctor_ok,
            mode="development",
        )
    assert cli.read_text() == "foreign\n"
    assert not any(call[:3] == ["grok", "plugin", "install"] for call in host.calls)


def test_duplicate_plugin_inventory_refuses_before_any_host_mutation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    def duplicate_runner(argv, **kwargs):
        args = [str(item) for item in argv]
        host.calls.append(args)
        if args[:4] == ["grok", "plugin", "list", "--json"]:
            version = json.loads((ROOT / "plugin.json").read_text())["version"]
            row = {
                "name": "oh-my-grok",
                "version": version,
                "path": str(ROOT),
                "source": str(ROOT),
                "enabled": True,
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps([row, dict(row)]), stderr="")
        raise AssertionError(f"unexpected mutation after duplicate inventory: {args!r}")

    with pytest.raises(InstallError, match="multiple"):
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=duplicate_runner,
            doctor_probe=_doctor_ok,
            mode="development",
        )
    assert host.calls == [["grok", "plugin", "list", "--json"]]


def test_symlink_source_resolves_to_same_immutable_package(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    source_link = tmp_path / "source-link"
    source_link.symlink_to(ROOT, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    result = install_package(
        source_link,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    assert result["package_digest"] == compute_package_identity(ROOT)["digest"]
    assert Path(result["stage_path"]).resolve() == host.installed


def test_doctor_hard_failure_never_leaves_candidate_live(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    host = FakeGrok()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    def bad(_stage: Path, _env: dict[str, str]):
        return {"argv": ["omg", "doctor", "--strict"], "rc": 1, "stdout": "", "stderr": "bad", "valid": True}

    with pytest.raises(InstallError, match="doctor"):
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=bad,
            mode="development",
        )
    assert host.installed is None
    assert not (grok_home / "omg" / "current").exists()


def test_post_receipt_doctor_failure_restores_all_pointers_and_audits_rollback(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    host = FakeGrok()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    probes = 0

    def fail_second(_stage: Path, _env: dict[str, str]):
        nonlocal probes
        probes += 1
        return {
            "argv": ["omg", "doctor", "--strict"],
            "rc": 0 if probes == 1 else 1,
            "stdout": "ok\n" if probes == 1 else "",
            "stderr": "post-receipt readback failed" if probes == 2 else "",
            "valid": True,
        }

    with pytest.raises(InstallError, match="doctor"):
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=fail_second,
            mode="development",
        )

    assert probes == 2
    assert host.installed is None
    assert not os.path.lexists(grok_home / "omg" / "current")
    assert not os.path.lexists(grok_home / "omg" / "current-receipt")
    assert not os.path.lexists(home / ".local" / "bin" / "omg")
    receipts = [read_install_receipt(path) for path in (grok_home / "omg" / "receipts").glob("*.json")]
    assert sorted(row["status"] for row in receipts) == ["installed", "rolled_back"]


def test_upgrade_from_legacy_package_missing_docs_parity_succeeds(
    tmp_path,
    monkeypatch,
):
    """Old managed installs lack docs/parity; upgrade must still resolve prior identity."""
    from omg_cli.setup_cmd import (
        LEGACY_TOLERATED_MISSING_SHIPPING_ROOTS,
        SHIPPING_ROOTS,
    )

    assert "docs/parity" in SHIPPING_ROOTS
    assert "docs/parity" in LEGACY_TOLERATED_MISSING_SHIPPING_ROOTS
    assert "hooks.json" in SHIPPING_ROOTS
    assert "hooks.json" in LEGACY_TOLERATED_MISSING_SHIPPING_ROOTS
    assert "mcp_config.json" in SHIPPING_ROOTS
    assert "mcp_config.json" in LEGACY_TOLERATED_MISSING_SHIPPING_ROOTS

    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = CopyingFakeGrok(grok_home)

    legacy = tmp_path / "legacy-without-parity"
    shutil.copytree(
        ROOT,
        legacy,
        ignore=shutil.ignore_patterns(
            ".git", ".omg", ".omx", ".pytest_cache", "__pycache__", "*.pyc"
        ),
    )
    parity = legacy / "docs" / "parity"
    assert parity.is_dir()
    shutil.rmtree(parity)

    with pytest.raises(InstallError, match="required shipping path missing"):
        compute_package_identity(legacy)

    legacy_identity = compute_package_identity(
        legacy,
        tolerate_missing_roots=LEGACY_TOLERATED_MISSING_SHIPPING_ROOTS,
    )
    assert not any(
        str(row["path"]).startswith("docs/parity/")
        for row in legacy_identity["inventory"]
    )

    installed = host(["grok", "plugin", "install", str(legacy), "--trust"])
    assert installed.returncode == 0
    host(["grok", "plugin", "enable", "oh-my-grok"])

    result = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )

    assert result["ok"] is True
    assert result["package_digest"] == compute_package_identity(ROOT)["digest"]
    assert host.installed is not None and host.enabled
    assert compute_package_identity(host.installed)["digest"] == result["package_digest"]
    assert any(
        str(row["path"]).startswith("docs/parity/")
        for row in compute_package_identity(host.installed)["inventory"]
    )
