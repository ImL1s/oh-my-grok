"""Hermetic evidence for docs/architecture/agent-model-routing.md Decision.

Absence of Medley must not disable ordinary OMG operation. This file is
stock-host smoke (setup / package projection, current ``omg doctor``,
ordinary agent/profile discovery, ordinary workflow parser/inventory). It
is not a routing implementation and does not exercise #131 / #134 / #138.

Absence is an explicit import blocker, never inferred from directory names
on ``sys.path`` / ``PYTHONPATH``. An injected installable ``medley`` on a
neutral path stays discoverable until that blocker is installed.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import pytest

from omg_cli import doctor
from omg_cli.setup_cmd import compute_package_identity, run_setup
from omg_cli.team.roles import CANONICAL_ROLES
from omg_cli.workflows.schema import compile_workflow

ROOT = Path(__file__).resolve().parents[1]
GEN_SCRIPT = ROOT / "scripts" / "generate_capabilities_lock.py"
HOST_FIXTURE = ROOT / "tests" / "fixtures" / "host" / "0.2.121.json"
WORKFLOW_FIXTURE = (
    ROOT / "tests" / "fixtures" / "workflow" / "production-safety-review-v1.json"
)
_BLOCKER_MSG = "stock-host import blocker"

_REQUIRE_MEDLEY_CLAIMS = (
    "requires medley",
    "require medley",
    "medley required",
    "medley is required",
    "must install medley",
    "need medley",
    "routing-availability",
    "routing availability",
)


class _StockHostMedleyImportBlocker(importlib.abc.MetaPathFinder):
    """Raise for ``medley`` / ``medley.*`` so an installed optional copy is hidden."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname == "medley" or fullname.startswith("medley."):
            raise ModuleNotFoundError(f"{_BLOCKER_MSG}: {fullname}", name=fullname)
        return None


def _this_file_does_not_import_medley() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith(("import ", "from ")):
            assert not stripped.startswith("import medley")
            assert not stripped.startswith("from medley")


def _evict_medley_modules(monkeypatch) -> None:
    for key in [k for k in sys.modules if k == "medley" or k.startswith("medley.")]:
        monkeypatch.delitem(sys.modules, key)


def _isolate_stock_host(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """Fake/temp HOME + GROK_HOME; scrub MEDLEY* env; evict medley*; local grok."""
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    grok_home.mkdir()
    bin_dir.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    for key in [k for k in os.environ if k.upper().startswith("MEDLEY")]:
        monkeypatch.delenv(key, raising=False)

    _evict_medley_modules(monkeypatch)

    monkeypatch.setenv(
        "PATH",
        str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
    )
    _install_fake_grok(bin_dir)
    return home, grok_home


def _neutral_site_packages(tmp_path: Path, tmp_path_factory) -> Path:
    """Install parent whose pathname does not contain ``medley``.

    Pytest names ``tmp_path`` after the test function, so the required
    smoke/discoverability names embed ``medley``. Use a factory temp
    whose basename is ``vendor`` — still under pytest cleanup.
    """
    site = tmp_path / "opt" / "lib" / "site-packages"
    if "medley" not in str(site).lower():
        return site
    site = tmp_path_factory.mktemp("vendor") / "lib" / "site-packages"
    assert "medley" not in str(site).lower(), site
    return site


def _inject_fake_medley_package(monkeypatch, tmp_path: Path, tmp_path_factory) -> Path:
    site = _neutral_site_packages(tmp_path, tmp_path_factory)
    pkg = site / "medley"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        '"""Test-only installable medley stand-in."""\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "path", [str(site), *sys.path])
    prior = os.environ.get("PYTHONPATH", "")
    prepended = str(site) if not prior else str(site) + os.pathsep + prior
    monkeypatch.setenv("PYTHONPATH", prepended)
    importlib.invalidate_caches()
    return site


def _install_import_blocker(monkeypatch) -> _StockHostMedleyImportBlocker:
    blocker = _StockHostMedleyImportBlocker()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    return blocker


def _assert_medley_discoverable(site: Path) -> None:
    assert "medley" not in str(site).lower(), site
    spec = importlib.util.find_spec("medley")
    assert spec is not None
    assert spec.origin is not None
    origin = Path(spec.origin).resolve()
    assert site.resolve() in origin.parents


def _assert_blocker_raises() -> None:
    with pytest.raises(ModuleNotFoundError, match=_BLOCKER_MSG):
        importlib.util.find_spec("medley")
    with pytest.raises(ModuleNotFoundError, match=_BLOCKER_MSG):
        importlib.import_module("medley")
    with pytest.raises(ModuleNotFoundError, match=_BLOCKER_MSG):
        importlib.util.find_spec("medley.native")
    with pytest.raises(ModuleNotFoundError, match=_BLOCKER_MSG):
        importlib.import_module("medley.native")


def _install_fake_grok(bin_dir: Path) -> Path:
    """Tiny local grok: --version / version → 0.2.121; otherwise exit 0. No network."""
    path = bin_dir / "grok"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args or (args and args[0] == 'version'):\n"
        "    if '--json' in args:\n"
        "        print('{\"currentVersion\":\"0.2.121\"}')\n"
        "    else:\n"
        "        print('0.2.121')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _assert_medley_absent(home: Path) -> None:
    """Env/config/module isolation after blocker — not sys.path directory names."""
    _this_file_does_not_import_medley()
    leaked = [k for k in sys.modules if k == "medley" or k.startswith("medley.")]
    assert not leaked, f"medley leaked into sys.modules: {leaked}"
    assert not (home / ".medley").exists()
    assert not (home / "medley").exists()
    if home.is_dir():
        for child in home.iterdir():
            assert "medley" not in child.name.lower(), child
    assert not any(k.upper().startswith("MEDLEY") for k in os.environ)


def _fake_host_report():
    from omg_cli.host_probe import host_report_for_doctor, probe_host_from_fixture

    report = probe_host_from_fixture(HOST_FIXTURE)
    return report, host_report_for_doctor(report)


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_capabilities_lock_stock_host_test", GEN_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_injected_medley_on_neutral_path_is_discoverable_without_blocker(
    monkeypatch, tmp_path, tmp_path_factory
) -> None:
    _isolate_stock_host(monkeypatch, tmp_path)
    site = _inject_fake_medley_package(monkeypatch, tmp_path, tmp_path_factory)
    _assert_medley_discoverable(site)


def test_ordinary_omg_surfaces_work_with_medley_absent(
    monkeypatch, tmp_path, tmp_path_factory, capsys
) -> None:
    home, _grok_home = _isolate_stock_host(monkeypatch, tmp_path)
    site = _inject_fake_medley_package(monkeypatch, tmp_path, tmp_path_factory)
    _assert_medley_discoverable(site)
    _evict_medley_modules(monkeypatch)
    _install_import_blocker(monkeypatch)
    _assert_blocker_raises()
    _assert_medley_absent(home)

    # 1. Package projection / setup
    identity = compute_package_identity(ROOT)
    assert identity.get("digest")
    assert identity.get("version")
    inventory_paths = {row["path"] for row in identity["inventory"]}
    assert "bin/omg" in inventory_paths

    project = tmp_path / "project"
    project.mkdir()
    assert run_setup(project, install_rules=True, install_hook=True) == 0
    assert (project / ".omg").is_dir()
    capsys.readouterr()

    # 2. Current doctor (fixture host probe; no live grok inspect / network)
    monkeypatch.setattr(doctor, "_canonical_host_probe", _fake_host_report)
    rc = doctor.run_doctor(strict=False, project_root=project, json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["command"] == "doctor"
    host = payload.get("host") or {}
    assert host.get("binary") == "grok" or "binary" in host
    blob = out.lower()
    for banned in _REQUIRE_MEDLEY_CLAIMS:
        assert banned not in blob, banned

    hard = {row["name"]: row for row in payload.get("checks") or []}
    for name in (
        "plugin.json",
        "skills omg-*",
        "agents",
        "deny module",
        "hooks scripts",
        "PreToolUse hook",
        "global PreToolUse soft-gate",
    ):
        assert name in hard, name
        assert hard[name]["ok"] is True, hard[name]

    assert doctor.check_plugin_json()[1] is True
    assert doctor.check_agents_present()[1] is True
    assert doctor.check_skills_omg_prefix()[1] is True
    assert doctor.check_deny_importable()[1] is True

    # 3. Ordinary agent / profile discovery (taxonomy only — no routing)
    gen = _load_generator()
    surface = gen.discover_session_surface(ROOT)
    agent_names = {item["name"] for item in surface["agents"]}
    skill_names = {item["name"] for item in surface["skills"]}
    assert "omg-executor" in agent_names or "omg-verifier" in agent_names
    assert "omg-using" in skill_names or "omg-ralph" in skill_names
    assert "executor" in CANONICAL_ROLES
    assert "verifier" in CANONICAL_ROLES

    # 4. Ordinary workflow parser / inventory (no network)
    compiled = compile_workflow(WORKFLOW_FIXTURE)
    assert compiled.get("stages")
    assert compiled.get("name") or compiled.get("contract") or compiled.get("definition")

    _assert_medley_absent(home)
    _assert_blocker_raises()
    assert "medley" not in str(site).lower()
    assert str(site) in sys.path
