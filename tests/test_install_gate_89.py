"""Hermetic locks for release install gate vs coexistence (#89)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omg_cli.setup_cmd import (
    InstallError,
    _default_doctor_probe,
    classify_doctor_probe,
    compute_package_identity,
    install_package,
    read_install_receipt,
)
from omg_cli.update_cmd import run_update
from scripts.omg_install_classifier import (
    classify_doctor_result,
    classify_doctor_stdout_buckets,
)


ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]

COEXISTENCE_STRICT_STDOUT = """\
oh-my-grok doctor
------------------------------------------------
[OK  ] grok on PATH: present
------------------------------------------------
plugin trust / inventory (best-effort)
[FAIL] effective discovery (foreign orch): foreign orchestration in grok inspect: oh-my-claudecode
------------------------------------------------
compat.claude isolation
[FAIL] compat.claude.settings.hooks: non-empty hooks (settings.json)
[FAIL] compat.claude.md.markers: OMC/ralph markers present (CLAUDE.md)
[FAIL] compat.claude: risks present under --strict
------------------------------------------------
1 check(s) failed
"""

INTEGRITY_FAIL_STDOUT = """\
oh-my-grok doctor
------------------------------------------------
[OK  ] grok on PATH: present
------------------------------------------------
plugin trust / inventory (best-effort)
[FAIL] immutable install identity: pending immutable stage digest differs
------------------------------------------------
1 check(s) failed
"""


class FakeGrok:
    def __init__(self) -> None:
        self.installed: Path | None = None
        self.enabled = False

    def __call__(self, argv, **_kwargs):
        args = [str(item) for item in argv]
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


def _patch_release_archive(monkeypatch, tmp_path: Path) -> Path:
    """Bypass dirty-checkout + archive verify so release install_package is hermetic."""

    asset = tmp_path / f"oh-my-grok-{VERSION}.tar.gz"
    asset.write_bytes(b"unused-bytes")
    monkeypatch.setattr("omg_cli.setup_cmd._source_is_dirty", lambda _p: False)
    monkeypatch.setattr(
        "omg_cli.setup_cmd.verify_release_archive",
        lambda *_a, **_k: {
            "asset_name": asset.name,
            "asset_sha256": "a" * 64,
            "checksums_sha256": "b" * 64,
        },
    )
    return asset


def test_doctor_stdout_buckets_separate_coexistence_from_integrity():
    soft = classify_doctor_stdout_buckets(COEXISTENCE_STRICT_STDOUT)
    assert soft["coexistence"]
    assert not soft["integrity"]
    assert soft["bucket"] == "coexistence_only"

    hard = classify_doctor_stdout_buckets(INTEGRITY_FAIL_STDOUT)
    assert hard["integrity"]
    assert hard["bucket"] == "integrity"


def test_classify_doctor_result_release_allows_soft_warning():
    assert classify_doctor_result(mode="release", rc=2, valid=True) == "completed_with_warning"
    assert classify_doctor_result(mode="development", rc=2, valid=True) == "completed_with_warning"
    assert classify_doctor_result(mode="release", rc=1, valid=True) == "hard_failure"


def test_release_install_gate_allows_coexistence_warns(tmp_path, monkeypatch):
    """Release install must not roll back solely for foreign-orch / compat.claude."""

    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    asset = _patch_release_archive(monkeypatch, tmp_path)
    host = FakeGrok()

    def coexistence_probe(stage: Path, env: dict[str, str]) -> dict[str, object]:
        assert env.get("OMG_INSTALL_MODE") == "release"
        assert env.get("OMG_DOCTOR_INSTALL_PROBE") == "1"
        # Soft-only coexistence: production probe remaps strict-fail+relaxed-ok → rc=2.
        return {
            "argv": ["omg", "doctor", "--strict"],
            "rc": 2,
            "stdout": COEXISTENCE_STRICT_STDOUT,
            "stderr": "",
            "valid": True,
        }

    result = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=coexistence_probe,
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    assert result["ok"] is True
    assert result["status"] == "completed_with_warning"
    receipt = read_install_receipt(Path(result["receipt_path"]))
    assert receipt["mode"] == "release"
    assert receipt["status"] == "completed_with_warning"
    assert host.installed is not None and host.enabled
    assert receipt["installed"]["package_digest"] == compute_package_identity(ROOT)["digest"]


def test_release_install_gate_rejects_integrity_failure(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    asset = _patch_release_archive(monkeypatch, tmp_path)
    host = FakeGrok()

    def integrity_probe(_stage: Path, _env: dict[str, str]) -> dict[str, object]:
        return {
            "argv": ["omg", "doctor", "--strict"],
            "rc": 1,
            "stdout": INTEGRITY_FAIL_STDOUT,
            "stderr": "identity readback failed\n",
            "valid": True,
        }

    with pytest.raises(InstallError, match="doctor gate rejected"):
        install_package(
            ROOT,
            home=home,
            grok_home=grok_home,
            runner=host,
            doctor_probe=integrity_probe,
            mode="release",
            asset=asset,
            source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
            source_tag=f"v{VERSION}",
        )
    assert host.installed is None
    assert not (grok_home / "omg" / "current").exists()


def test_doctor_gate_failure_prints_transcript(capsys):
    probe = {
        "argv": ["omg", "doctor", "--strict"],
        "rc": 1,
        "stdout": INTEGRITY_FAIL_STDOUT,
        "stderr": "identity readback failed\n",
        "valid": True,
    }
    with pytest.raises(InstallError, match="doctor gate rejected"):
        classify_doctor_probe("release", probe)
    err = capsys.readouterr().err
    assert "immutable install identity" in err
    assert "identity readback failed" in err
    assert "doctor gate transcript" in err.lower()


def test_default_doctor_probe_soft_relaxes_release_coexistence(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append([str(item) for item in argv])
        if "--strict" in argv:
            return SimpleNamespace(
                returncode=1,
                stdout=COEXISTENCE_STRICT_STDOUT,
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="relaxed ok\n", stderr="")

    monkeypatch.setattr("omg_cli.setup_cmd.subprocess.run", fake_run)
    result = _default_doctor_probe(
        tmp_path,
        {"OMG_INSTALL_MODE": "release"},
    )
    assert result["rc"] == 2
    assert len(calls) == 2
    assert "--strict" in calls[0]
    assert "--strict" not in calls[1]


def test_classify_doctor_probe_coexistence_only_rc1_softens_for_release():
    """Bucket path: rc=1 with only coexistence FAIL lines → soft success."""

    status = classify_doctor_probe(
        "release",
        {
            "argv": ["omg", "doctor", "--strict"],
            "rc": 1,
            "stdout": COEXISTENCE_STRICT_STDOUT,
            "stderr": "",
            "valid": True,
        },
    )
    assert status == "completed_with_warning"


def test_omg_update_uses_release_transaction_from_managed_install(tmp_path, monkeypatch):
    """Development completed_with_warning without clean source → release install.sh."""

    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    def warn_probe(_stage: Path, _env: dict[str, str]) -> dict[str, object]:
        return {
            "argv": ["omg", "doctor", "--strict"],
            "rc": 2,
            "stdout": COEXISTENCE_STRICT_STDOUT,
            "stderr": "",
            "valid": True,
        }

    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=warn_probe,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    assert installed["status"] == "completed_with_warning"

    import omg_cli.update_cmd as update_mod

    monkeypatch.setattr(
        update_mod,
        "_development_source_checkout",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("no proven clean original development checkout")
        ),
    )
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="release ok\n", stderr="")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 0
    assert calls == [["bash", str(stage / "scripts" / "install.sh")]]
