"""Isolated Medley-absent smoke. Not collected by pytest.

Installs the import blocker before any omg_cli import, then exercises the
four ordinary OMG surfaces and writes a JSON result file.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import traceback
from pathlib import Path

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

import tests.stock_host_medley_absent_support as support

support.install_blocker()

from omg_cli import doctor
from omg_cli.host_probe import host_report_for_doctor, probe_host_from_fixture
from omg_cli.setup_cmd import compute_package_identity, run_setup
from omg_cli.team.roles import CANONICAL_ROLES
from omg_cli.workflows.schema import compile_workflow


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _load_generator(root: Path):
    spec = importlib.util.spec_from_file_location(
        "generate_capabilities_lock_stock_host_smoke",
        root / "scripts" / "generate_capabilities_lock.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load generate_capabilities_lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_host_report(host_fixture: Path):
    report = probe_host_from_fixture(host_fixture)
    return report, host_report_for_doctor(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    work = args.work.resolve()
    result_path = args.result.resolve()

    try:
        iso = support.create_isolation(work, root=root)
        site = support.inject_medley_under_work(work, root=root)
        if "medley" not in str(site).lower():
            return _fail(f"injected site ancestor must contain medley: {site}")
        support.assert_blocker_raises()
        leaked = [k for k in sys.modules if k == "medley" or k.startswith("medley.")]
        if leaked:
            return _fail(f"medley leaked into sys.modules: {leaked}")

        identity = compute_package_identity(root)
        if not identity.get("digest") or not identity.get("version"):
            return _fail(f"package identity missing digest/version: {identity!r}")
        inventory_paths = {row["path"] for row in identity["inventory"]}
        if "bin/omg" not in inventory_paths:
            return _fail("package identity inventory missing bin/omg")

        project = work / "project"
        project.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            setup_rc = run_setup(project, install_rules=True, install_hook=True)
        if setup_rc != 0:
            return _fail(f"run_setup rc={setup_rc}")
        if not (project / ".omg").is_dir():
            return _fail("run_setup did not create .omg")

        host_fixture = root / "tests" / "fixtures" / "host" / "0.2.121.json"
        doctor._canonical_host_probe = lambda: _fake_host_report(host_fixture)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = doctor.run_doctor(strict=False, project_root=project, json_output=True)
        out = buf.getvalue()
        if rc != 0:
            return _fail(f"run_doctor rc={rc} stdout={out!r}")
        payload = json.loads(out)
        if payload.get("command") != "doctor":
            return _fail(f"doctor payload command={payload.get('command')!r}")
        host = payload.get("host") or {}
        if not (host.get("binary") == "grok" or "binary" in host):
            return _fail(f"doctor host block missing binary: {host!r}")
        blob = out.lower()
        for banned in support._REQUIRE_MEDLEY_CLAIMS:
            if banned in blob:
                return _fail(f"doctor output claimed {banned!r}")
        hard = {row["name"]: row for row in payload.get("checks") or []}
        for name in support.REQUIRED_DOCTOR_CHECKS:
            row = hard.get(name)
            if row is None or row.get("ok") is not True:
                return _fail(f"doctor hard check failed: {name}={row!r}")
        if doctor.check_plugin_json()[1] is not True:
            return _fail("check_plugin_json failed")
        if doctor.check_agents_present()[1] is not True:
            return _fail("check_agents_present failed")
        if doctor.check_skills_omg_prefix()[1] is not True:
            return _fail("check_skills_omg_prefix failed")
        if doctor.check_deny_importable()[1] is not True:
            return _fail("check_deny_importable failed")

        gen = _load_generator(root)
        surface = gen.discover_session_surface(root)
        agent_names = {item["name"] for item in surface["agents"]}
        skill_names = {item["name"] for item in surface["skills"]}
        if "omg-executor" not in agent_names and "omg-verifier" not in agent_names:
            return _fail(f"agent names missing executor/verifier: {sorted(agent_names)}")
        if "omg-using" not in skill_names and "omg-ralph" not in skill_names:
            return _fail(f"skill names missing using/ralph: {sorted(skill_names)}")
        if "executor" not in CANONICAL_ROLES or "verifier" not in CANONICAL_ROLES:
            return _fail(f"CANONICAL_ROLES missing executor/verifier: {CANONICAL_ROLES!r}")

        workflow_fixture = (
            root / "tests" / "fixtures" / "workflow" / "production-safety-review-v1.json"
        )
        compiled = compile_workflow(workflow_fixture)
        if not compiled.get("stages"):
            return _fail("compile_workflow missing stages")
        if not (
            compiled.get("name") or compiled.get("contract") or compiled.get("definition")
        ):
            return _fail("compile_workflow missing name/contract/definition")

        support.assert_blocker_raises()
        if "medley" in sys.modules or any(k.startswith("medley.") for k in sys.modules):
            return _fail("medley appeared in sys.modules after surfaces")
        if os_home_mismatch(iso):
            return _fail("isolation HOME/GROK_HOME drifted")

        result = {
            "ok": True,
            "blocker_installed_before_omg_cli": True,
            "imported": list(support.SMOKE_IMPORTED),
            "surfaces": list(support.SMOKE_SURFACES),
            "doctor_checks_ok": list(support.REQUIRED_DOCTOR_CHECKS),
            "setup_omg_dir": True,
            "blocker_raises": True,
        }
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


def os_home_mismatch(iso: support._StockHostIsolation) -> bool:
    import os

    return os.environ.get("HOME") != str(iso.home) or os.environ.get("GROK_HOME") != str(
        iso.grok_home
    )


if __name__ == "__main__":
    raise SystemExit(main())
