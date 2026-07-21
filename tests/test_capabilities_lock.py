"""Tests for scripts/generate_capabilities_lock.py + doctor soft check."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli import doctor

ROOT = Path(__file__).resolve().parents[1]
GEN_SCRIPT = ROOT / "scripts" / "generate_capabilities_lock.py"


def _load_gen_module():
    spec = importlib.util.spec_from_file_location(
        "generate_capabilities_lock", GEN_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "9.9.9"}),
        encoding="utf-8",
    )
    skill = tmp_path / "skills" / "omg-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# skill x\nbody\n", encoding="utf-8")
    agent = tmp_path / "agents" / "omg-y.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("# agent y\n", encoding="utf-8")
    # Non-matching paths must be ignored
    (tmp_path / "skills" / "other" / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "other" / "SKILL.md").write_text("nope\n", encoding="utf-8")
    (tmp_path / "agents" / "readme.md").write_text("ignore\n", encoding="utf-8")
    return tmp_path


def test_compute_lock_files_and_aggregate(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    lock = gen.compute_lock(root)
    assert lock["version"] == "9.9.9"
    assert set(lock["files"]) == {
        "skills/omg-x/SKILL.md",
        "agents/omg-y.md",
    }
    assert isinstance(lock["aggregate"], str) and len(lock["aggregate"]) == 64
    # deterministic
    assert gen.compute_lock(root)["aggregate"] == lock["aggregate"]
    # compute_lock_for is the generalized form; compute_lock delegates to it
    assert gen.compute_lock_for(root) == lock
    assert gen.compute_lock_for(root)["aggregate"] == lock["aggregate"]


def test_compute_lock_for_deterministic_on_tmp_root(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    a = gen.compute_lock_for(root)
    b = gen.compute_lock_for(root)
    assert a == b
    assert a["aggregate"] == b["aggregate"]
    assert set(a["files"]) == {
        "skills/omg-x/SKILL.md",
        "agents/omg-y.md",
    }


def test_editing_file_changes_aggregate(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    before = gen.compute_lock(root)["aggregate"]
    (root / "skills" / "omg-x" / "SKILL.md").write_text("# skill x\nchanged\n", encoding="utf-8")
    after = gen.compute_lock(root)["aggregate"]
    assert after != before


def test_read_lock_round_trip(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    lock = gen.compute_lock(root)
    out = root / "omg_capabilities.lock.json"
    out.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    loaded = gen.read_lock(root)
    assert loaded is not None
    assert loaded["aggregate"] == lock["aggregate"]
    assert loaded["files"] == lock["files"]


def test_generate_writes_valid_json(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    path = gen.write_lock(root)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "version" in data and "files" in data and "aggregate" in data


def test_check_exits_0_when_current_1_when_stale(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    gen.write_lock(root)
    rc0 = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc0.returncode == 0, rc0.stdout + rc0.stderr

    (root / "agents" / "omg-y.md").write_text("# agent y\nstale now\n", encoding="utf-8")
    rc1 = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc1.returncode == 1
    assert rc1.stdout or rc1.stderr  # prints a diff


def test_doctor_check_capabilities_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    monkeypatch.setattr(doctor, "plugin_root", lambda: root)

    name, level, detail = doctor.check_capabilities_lock()
    assert name == "capabilities lock (local checkout)"
    assert level == "warn"
    assert "no omg_capabilities.lock.json" in detail

    gen.write_lock(root)
    name, level, detail = doctor.check_capabilities_lock()
    assert level == "ok"
    assert "local checkout: 2 files match lock" in detail

    (root / "skills" / "omg-x" / "SKILL.md").write_text("drift\n", encoding="utf-8")
    name, level, detail = doctor.check_capabilities_lock()
    assert level == "warn"
    assert "regenerate" in detail.lower()
    assert "commit-hygiene" in detail.lower()


def test_run_soft_checks_includes_capabilities_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor,
        "check_plugin_trust",
        lambda: ("plugin trust/inventory", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_effective_discovery_foreign",
        lambda: ("foreign plugins in discovery", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_global_rules",
        lambda: ("global rules", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_plugin_version_drift",
        lambda: ("plugin version drift", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_plugin_enabled",
        lambda: ("plugin enabled", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_capabilities_lock",
        lambda: (
            "capabilities lock (local checkout)",
            "ok",
            "local checkout: n files match lock",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "check_installed_capabilities_lock",
        lambda: (
            "installed capabilities lock",
            "ok",
            "installed skills/agents match committed lock",
        ),
    )
    soft = doctor.run_soft_checks()
    names = [n for n, _, _ in soft]
    assert "capabilities lock (local checkout)" in names
    assert "installed capabilities lock" in names
    # installed check runs after local-checkout check
    assert names.index("installed capabilities lock") > names.index(
        "capabilities lock (local checkout)"
    )


def _populate_installed_like_checkout(installed: Path, *, skill_body: str = "# skill x\nbody\n") -> None:
    """Minimal skills/agents tree matching _fake_repo lock inputs."""
    skill = installed / "skills" / "omg-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(skill_body, encoding="utf-8")
    agent = installed / "agents" / "omg-y.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("# agent y\n", encoding="utf-8")
    (installed / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "9.9.9"}),
        encoding="utf-8",
    )


def test_doctor_check_installed_capabilities_lock_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed snapshot content identical to checkout lock inputs → ok."""
    gen = _load_gen_module()
    checkout = _fake_repo(tmp_path / "checkout")
    gen.write_lock(checkout)
    installed = tmp_path / "installed"
    _populate_installed_like_checkout(installed)

    monkeypatch.setattr(doctor, "plugin_root", lambda: checkout)
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "source": str(checkout),
                "path": str(installed),
            }
        ],
    )
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "ok"
    assert "match committed lock" in detail


def test_doctor_check_installed_capabilities_lock_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed SKILL.md differs → aggregate mismatch → warn."""
    gen = _load_gen_module()
    checkout = _fake_repo(tmp_path / "checkout")
    gen.write_lock(checkout)
    installed = tmp_path / "installed"
    _populate_installed_like_checkout(
        installed, skill_body="# skill x\nDRIFTED installed body\n"
    )

    monkeypatch.setattr(doctor, "plugin_root", lambda: checkout)
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "source": str(checkout),
                "path": str(installed),
            }
        ],
    )
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "warn"
    assert "INSTALLED skills/agents differ" in detail
    assert "install-plugin" in detail or "plugin update" in detail


def test_doctor_check_installed_capabilities_lock_probe_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe returns None → warn, never crash."""
    monkeypatch.setattr(doctor, "_run_grok_json", lambda *_a, **_k: None)
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "warn"
    assert "cannot locate installed snapshot" in detail


def test_doctor_check_installed_capabilities_lock_missing_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugin list points at a non-existent installed dir → warn."""
    checkout = _fake_repo(tmp_path / "checkout")
    missing = tmp_path / "no-such-installed"
    monkeypatch.setattr(doctor, "plugin_root", lambda: checkout)
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "source": str(checkout),
                "path": str(missing),
            }
        ],
    )
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "warn"
    assert "cannot locate installed snapshot" in detail
