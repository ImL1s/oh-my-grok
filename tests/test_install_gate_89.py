"""Hermetic locks for release install gate vs coexistence (#89 / Pro dual-pass)."""
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
from scripts.omg_install_classifier import classify_doctor_result


ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]

COEXISTENCE_STRICT_STDOUT = """\
oh-my-grok doctor
------------------------------------------------
[FAIL] effective discovery (foreign orch): foreign orchestration in grok inspect: oh-my-claudecode
[FAIL] compat.claude.settings.hooks: non-empty hooks (settings.json)
[FAIL] compat.claude: risks present under --strict
------------------------------------------------
1 check(s) failed
"""

INTEGRITY_FAIL_STDOUT = """\
oh-my-grok doctor
------------------------------------------------
[FAIL] immutable install identity: pending immutable stage digest differs
------------------------------------------------
1 check(s) failed
"""


class FakeGrok:
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


def _patch_release_archive(monkeypatch, tmp_path: Path) -> Path:
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


def _dual_pass_soft(*, stdout: str = COEXISTENCE_STRICT_STDOUT) -> dict[str, object]:
    return {
        "argv": ["omg", "doctor", "--strict"],
        "argv_strict": ["omg", "doctor", "--strict"],
        "argv_relaxed": ["omg", "doctor"],
        "rc": 2,
        "strict_rc": 1,
        "relaxed_rc": 0,
        "stdout": stdout,
        "stderr": "",
        "relaxed_stdout": "all hard checks passed (soft/compat risks WARN only)\n",
        "relaxed_stderr": "",
        "valid": True,
    }


def _dual_pass_ok() -> dict[str, object]:
    return {
        "argv": ["omg", "doctor", "--strict"],
        "argv_strict": ["omg", "doctor", "--strict"],
        "argv_relaxed": None,
        "rc": 0,
        "strict_rc": 0,
        "relaxed_rc": None,
        "stdout": "ok\n",
        "stderr": "",
        "relaxed_stdout": "",
        "relaxed_stderr": "",
        "valid": True,
    }


def _dual_pass_hard(*, stdout: str = INTEGRITY_FAIL_STDOUT) -> dict[str, object]:
    return {
        "argv": ["omg", "doctor", "--strict"],
        "argv_strict": ["omg", "doctor", "--strict"],
        "argv_relaxed": ["omg", "doctor"],
        "rc": 1,
        "strict_rc": 1,
        "relaxed_rc": 1,
        "stdout": stdout,
        "stderr": "identity readback failed\n",
        "relaxed_stdout": stdout,
        "relaxed_stderr": "identity readback failed\n",
        "valid": True,
    }


def test_doctor_classifier_dual_pass_matrix():
    assert (
        classify_doctor_result(
            mode="release", valid=True, strict_rc=0, relaxed_rc=None, rc=0
        )
        == "installed"
    )
    assert (
        classify_doctor_result(
            mode="release", valid=True, strict_rc=1, relaxed_rc=0, rc=2
        )
        == "completed_with_warning"
    )
    assert (
        classify_doctor_result(
            mode="development", valid=True, strict_rc=1, relaxed_rc=0, rc=2
        )
        == "completed_with_warning"
    )
    assert (
        classify_doctor_result(
            mode="release", valid=True, strict_rc=1, relaxed_rc=1, rc=1
        )
        == "hard_failure"
    )
    # Bare rc=2 without dual-pass evidence must not bypass.
    assert classify_doctor_result(mode="release", valid=True, rc=2) == "hard_failure"
    assert classify_doctor_result(mode="development", valid=True, rc=2) == "hard_failure"
    assert classify_doctor_result(mode="release", valid=True, rc=0) == "installed"
    assert classify_doctor_result(
        mode="release", valid=False, strict_rc=1, relaxed_rc=0, rc=2
    ) == ("hard_failure")
    # Contradictory / malformed dual-pass must not classify as installed (#89 Pro P2).
    assert (
        classify_doctor_result(
            mode="release", valid=True, strict_rc=0, relaxed_rc="0", rc=0
        )
        == "hard_failure"
    )
    assert (
        classify_doctor_result(
            mode="release", valid=True, strict_rc=0, relaxed_rc=1, rc=0
        )
        == "hard_failure"
    )
    assert (
        classify_doctor_result(
            mode="release", valid=True, strict_rc=0, relaxed_rc=None, rc=1
        )
        == "hard_failure"
    )
    assert (
        classify_doctor_result(
            mode="release", valid=True, strict_rc=1, relaxed_rc=0, rc=0
        )
        == "hard_failure"
    )


def test_classify_doctor_probe_rejects_contradictory_dual_pass_success():
    """Outer probe wrapper must hard-fail contradictory success-shaped evidence."""

    with pytest.raises(InstallError, match="malformed"):
        classify_doctor_probe(
            "release",
            {
                "valid": True,
                "strict_rc": 0,
                "relaxed_rc": "0",
                "rc": 1,
                "stdout": "",
                "stderr": "",
            },
        )
    with pytest.raises(InstallError, match="malformed"):
        classify_doctor_probe(
            "release",
            {
                "valid": True,
                "strict_rc": 0,
                "relaxed_rc": None,
                "rc": 1,
                "stdout": "",
                "stderr": "",
            },
        )


def test_release_install_gate_allows_coexistence_warns(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    asset = _patch_release_archive(monkeypatch, tmp_path)
    host = FakeGrok()

    def coexistence_probe(stage: Path, env: dict[str, str]) -> dict[str, object]:
        assert env.get("OMG_INSTALL_MODE") == "release"
        assert env.get("OMG_DOCTOR_INSTALL_PROBE") == "1"
        return _dual_pass_soft()

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
        return _dual_pass_hard()

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
    with pytest.raises(InstallError, match="doctor gate rejected"):
        classify_doctor_probe("release", _dual_pass_hard())
    err = capsys.readouterr().err
    assert "install doctor gate failed" in err
    assert "immutable install identity" in err
    assert "identity readback failed" in err


def test_malformed_dual_pass_fields_do_not_legacy_succeed():
    """Keys present but non-int must hard-fail — never coerce into legacy rc=0 (#89 Pro)."""

    with pytest.raises(InstallError, match="malformed|rejected"):
        classify_doctor_probe(
            "release",
            {
                "valid": True,
                "strict_rc": "1",
                "relaxed_rc": "0",
                "rc": 0,
                "stdout": "",
                "stderr": "",
            },
        )


def test_doctor_transcript_keeps_failure_past_1k_chars(capsys):
    """Transcript redaction must not truncate to 1000 before the 64 KiB limit (#89 Pro)."""

    prefix = "x" * 1500
    probe = _dual_pass_hard(stdout=prefix + INTEGRITY_FAIL_STDOUT)
    with pytest.raises(InstallError, match="doctor gate rejected"):
        classify_doctor_probe("release", probe)
    err = capsys.readouterr().err
    assert "immutable install identity" in err
    assert len(err) > 1000


def test_success_receipt_records_final_probe_and_stricter_status(tmp_path, monkeypatch):
    """Authoritative receipt must include post-publication probe + stricter status (#89 Pro)."""

    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    asset = _patch_release_archive(monkeypatch, tmp_path)
    host = FakeGrok()
    calls = {"n": 0}

    def split_probe(_stage: Path, env: dict[str, str]) -> dict[str, object]:
        calls["n"] += 1
        if env.get("OMG_EXPECTED_INSTALL_DIGEST"):
            # Pending pass: clean strict success.
            return {
                "argv": ["omg", "doctor", "--strict"],
                "argv_strict": ["omg", "doctor", "--strict"],
                "argv_relaxed": None,
                "rc": 0,
                "strict_rc": 0,
                "relaxed_rc": None,
                "stdout": "pending ok\n",
                "stderr": "",
                "relaxed_stdout": "",
                "relaxed_stderr": "",
                "valid": True,
            }
        # Final env-free pass: coexistence soft only.
        return _dual_pass_soft()

    result = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=split_probe,
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    assert calls["n"] == 2
    assert result["status"] == "completed_with_warning"
    receipt = read_install_receipt(Path(result["receipt_path"]))
    assert receipt["status"] == "completed_with_warning"
    doctor_cmds = [c for c in receipt["commands"] if "doctor" in " ".join(c.get("argv") or [])]
    assert len(doctor_cmds) >= 2
    assert any(c.get("strict_rc") == 0 for c in doctor_cmds)
    assert any(c.get("strict_rc") == 1 and c.get("relaxed_rc") == 0 for c in doctor_cmds)


def test_default_doctor_probe_release_soft_only_returns_dual_pass_evidence(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append([str(item) for item in argv])
        if "--strict" in argv:
            return SimpleNamespace(returncode=1, stdout=COEXISTENCE_STRICT_STDOUT, stderr="")
        return SimpleNamespace(returncode=0, stdout="relaxed ok\n", stderr="")

    monkeypatch.setattr("omg_cli.setup_cmd.subprocess.run", fake_run)
    result = _default_doctor_probe(tmp_path, {"OMG_INSTALL_MODE": "release"})
    assert result["strict_rc"] == 1
    assert result["relaxed_rc"] == 0
    assert result["rc"] == 2
    assert result["valid"] is True
    assert len(calls) == 2
    assert "--strict" in calls[0]
    assert "--strict" not in calls[1]
    assert classify_doctor_probe("release", result) == "completed_with_warning"


def test_omg_update_uses_release_transaction_from_managed_install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_soft(),
        mode="development",
    )
    stage = Path(installed["stage_path"])
    assert installed["status"] == "completed_with_warning"

    import omg_cli.update_cmd as update_mod

    monkeypatch.setattr(
        update_mod,
        "_development_source_checkout",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("development receipt source checkout drifted from installed bytes")
        ),
    )
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="release ok\n", stderr="")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 0
    assert calls == [["bash", str(stage / "scripts" / "install.sh")]]


def _install_dev_managed(tmp_path, monkeypatch, host):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_soft(),
        mode="development",
    )
    return home, grok_home, Path(installed["stage_path"])


def test_managed_development_dirty_falls_back_to_stage_installer(tmp_path, monkeypatch, capsys):
    """Exact identity but dirty worktree must stage-fallback, not refuse (#89 Pro P1)."""

    host = FakeGrok()
    home, grok_home, stage = _install_dev_managed(tmp_path, monkeypatch, host)
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        if command[:2] == ["git", "-C"] and "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=f"{ROOT}\n", stderr="")
        if command[:2] == ["git", "-C"] and "status" in command:
            return SimpleNamespace(returncode=0, stdout=" M local.txt\n", stderr="")
        if command[:1] == ["bash"]:
            return SimpleNamespace(returncode=0, stdout="release ok\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 0
    assert ["bash", str(stage / "scripts" / "install.sh")] in calls
    assert not any("fetch" in c or "pull" in c for c in calls)
    err = capsys.readouterr().err
    assert "source checkout preserved" in err
    assert "cannot be safely refreshed" in err


def test_managed_development_non_git_falls_back_to_stage_installer(tmp_path, monkeypatch):
    """Exact identity but not a proven Git worktree root → stage fallback (#89 Pro P1)."""

    host = FakeGrok()
    home, grok_home, stage = _install_dev_managed(tmp_path, monkeypatch, host)
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        if command[:2] == ["git", "-C"] and "rev-parse" in command:
            return SimpleNamespace(returncode=128, stdout="", stderr="not a git repository")
        if command[:1] == ["bash"]:
            return SimpleNamespace(returncode=0, stdout="release ok\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 0
    assert calls[-1] == ["bash", str(stage / "scripts" / "install.sh")]
    assert not any("fetch" in c or "pull" in c for c in calls)


def test_managed_development_status_failure_falls_back_to_stage_installer(tmp_path, monkeypatch):
    """Exact identity but git status unprovable → stage fallback (#89 Pro P1)."""

    host = FakeGrok()
    home, grok_home, stage = _install_dev_managed(tmp_path, monkeypatch, host)
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        if command[:2] == ["git", "-C"] and "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=f"{ROOT}\n", stderr="")
        if command[:2] == ["git", "-C"] and "status" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="status failed")
        if command[:1] == ["bash"]:
            return SimpleNamespace(returncode=0, stdout="release ok\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 0
    assert ["bash", str(stage / "scripts" / "install.sh")] in calls
    assert not any("fetch" in c or "pull" in c for c in calls)


def test_same_digest_development_to_release_reattests_with_checksum_evidence(
    tmp_path, monkeypatch
):
    """Same package bytes: development receipt must not be reused for release (#89 Pro P2)."""

    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    first = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_soft(),
        mode="development",
    )
    first_receipt = read_install_receipt(Path(first["receipt_path"]))
    assert first_receipt["mode"] == "development"
    assert first_receipt["status"] == "completed_with_warning"
    assert not first_receipt["source"].get("asset_sha256")

    asset = _patch_release_archive(monkeypatch, tmp_path)

    # Promotion probe is clean install — prior warning must not be demoted.
    second = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_ok(),
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    assert second["status"] == "completed_with_warning"
    assert second["status"] != "already_installed"
    assert second["receipt_path"] != first["receipt_path"]
    receipt = read_install_receipt(Path(second["receipt_path"]))
    assert receipt["mode"] == "release"
    assert receipt["status"] == "completed_with_warning"
    assert receipt["source"]["asset_sha256"] == "a" * 64
    assert receipt["source"]["checksums_sha256"] == "b" * 64
    assert receipt["source"]["asset_name"] == asset.name
    assert receipt["installed"]["package_digest"] == first["package_digest"]

    # Matching release authority on the same digest is truly idempotent.
    third = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_ok(),
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    assert third["status"] == "already_installed"
    assert third["receipt_path"] == second["receipt_path"]


def test_same_digest_release_reattests_when_probe_is_stricter(tmp_path, monkeypatch):
    """Existing clean release receipt must not mask a fresh soft-warning probe."""

    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    asset = _patch_release_archive(monkeypatch, tmp_path)
    host = FakeGrok()

    first = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_ok(),
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    assert first["status"] == "installed"
    first_path = first["receipt_path"]

    again = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_soft(),
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    assert again["status"] == "completed_with_warning"
    assert again["receipt_path"] != first_path
    receipt = read_install_receipt(Path(again["receipt_path"]))
    assert receipt["mode"] == "release"
    assert receipt["status"] == "completed_with_warning"
    assert receipt["source"]["asset_sha256"] == "a" * 64


def test_same_digest_development_to_release_reattests_receipt(tmp_path, monkeypatch):
    """Same package bytes must not reuse a development receipt for release (#89 Pro P2)."""

    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = FakeGrok()

    first = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_soft(),
        mode="development",
    )
    first_receipt = read_install_receipt(Path(first["receipt_path"]))
    assert first_receipt["mode"] == "development"
    assert first_receipt["status"] == "completed_with_warning"
    assert first_receipt["source"].get("asset_sha256") in (None, "")

    asset = _patch_release_archive(monkeypatch, tmp_path)
    mutations_before = [
        call
        for call in host.calls
        if call[:3]
        in (
            ["grok", "plugin", "install"],
            ["grok", "plugin", "uninstall"],
        )
    ]

    promoted = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_ok(),
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    mutations_after = [
        call
        for call in host.calls
        if call[:3]
        in (
            ["grok", "plugin", "install"],
            ["grok", "plugin", "uninstall"],
        )
    ]
    assert mutations_after == mutations_before
    assert promoted["status"] == "completed_with_warning"
    assert promoted["receipt_path"] != first["receipt_path"]
    assert promoted["stage_path"] == first["stage_path"]

    receipt = read_install_receipt(Path(promoted["receipt_path"]))
    assert receipt["mode"] == "release"
    assert receipt["status"] == "completed_with_warning"
    assert receipt["source"]["asset_name"] == asset.name
    assert receipt["source"]["asset_sha256"] == "a" * 64
    assert receipt["source"]["checksums_sha256"] == "b" * 64
    assert receipt["installed"]["package_digest"] == first_receipt["installed"]["package_digest"]
    current_receipt = (grok_home / "omg" / "current-receipt").resolve()
    assert current_receipt == Path(promoted["receipt_path"]).resolve()


def test_same_digest_release_idempotent_reuses_compatible_receipt(tmp_path, monkeypatch):
    """Release→release with matching evidence may keep already_installed."""

    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    asset = _patch_release_archive(monkeypatch, tmp_path)
    host = FakeGrok()

    first = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_ok(),
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    again = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=lambda *_a, **_k: _dual_pass_ok(),
        mode="release",
        asset=asset,
        source_uri=f"https://github.com/ImL1s/oh-my-grok/releases/download/v{VERSION}/x.tar.gz",
        source_tag=f"v{VERSION}",
    )
    assert again["status"] == "already_installed"
    assert again["receipt_path"] == first["receipt_path"]
    assert again["receipt_hash"] == first["receipt_hash"]
