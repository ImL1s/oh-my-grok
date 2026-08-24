"""Isolated Medley-absent smoke. Not collected by pytest.

Installs the import blocker at module level. Network/subprocess guards
are installed via create_isolation before any omg_cli import, then the
four ordinary OMG surfaces run and a JSON result file is written.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import traceback
from pathlib import Path

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

import tests.stock_host_medley_absent_support as support

support.install_blocker()


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


def _popen_is_guarded(popen) -> bool:
    try:
        popen(["curl", "https://example.com"])
    except PermissionError as exc:
        return support._SUBPROCESS_DENIED in str(exc)
    except OSError:
        return False
    return False


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
        preexisting = [
            k for k in sys.modules if k == "omg_cli" or k.startswith("omg_cli.")
        ]
        if preexisting:
            return _fail(f"omg_cli imported before isolation: {preexisting}")

        real_posix_spawn = getattr(os, "posix_spawn", None)
        iso = support.create_isolation(work, root=root)
        guards_installed_before_omg_cli = True
        posix_spawn_unpatched = getattr(os, "posix_spawn", None) is real_posix_spawn

        import tests.stock_host_medley_absent_import_probe as import_probe

        import_probe_network = bool(import_probe.NETWORK_DENIED)
        import_probe_subprocess = bool(import_probe.SUBPROCESS_DENIED)
        if not import_probe_network:
            return _fail(
                f"import-time network not denied: {import_probe.NETWORK_ERROR!r}"
            )
        if not import_probe_subprocess:
            return _fail(
                f"import-time subprocess not denied: {import_probe.SUBPROCESS_ERROR!r}"
            )

        from omg_cli import doctor
        from omg_cli.setup_cmd import compute_package_identity, run_setup
        from omg_cli.team.roles import CANONICAL_ROLES
        from omg_cli.workflows.schema import compile_workflow
        import omg_cli.state as _st
        from omg_cli.host_models import HostProbeReport
        from omg_cli.host_probe import host_report_for_doctor, probe_host

        captured_system_popen_guarded = _popen_is_guarded(_st._SYSTEM_POPEN)
        if not captured_system_popen_guarded:
            return _fail("omg_cli.state._SYSTEM_POPEN is not the guarded Popen")

        integrate_imported = "omg_cli.integrate" in sys.modules
        captured_real_popen_guarded = None
        if integrate_imported:
            import omg_cli.integrate as _ig

            captured_real_popen_guarded = _popen_is_guarded(_ig._REAL_POPEN)
            if not captured_real_popen_guarded:
                return _fail("omg_cli.integrate._REAL_POPEN is not the guarded Popen")

        missing_smoke = [
            name for name in support.SMOKE_IMPORTED if name not in sys.modules
        ]
        if missing_smoke:
            return _fail(f"expected smoke modules missing: {missing_smoke}")

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

        # Isolation fake grok: version fallback only; inspect remains unexpected.
        live_report = probe_host()
        if not isinstance(live_report, HostProbeReport):
            return _fail(f"probe_host type={type(live_report)!r}")
        live_host = host_report_for_doctor(live_report)
        if not support.doctor_host_live_session_matches(live_host):
            return _fail(f"live host session mismatch: {live_host!r}")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = doctor.run_doctor(strict=False, project_root=project, json_output=True)
        out = buf.getvalue()
        if rc != 0:
            return _fail(f"run_doctor rc={rc} stdout={out!r}")
        payload = json.loads(out)
        if payload.get("command") != "doctor":
            return _fail(f"doctor payload command={payload.get('command')!r}")
        if payload.get("inspect_source") != "absent":
            return _fail(
                f"doctor inspect_source={payload.get('inspect_source')!r}"
            )
        host = payload.get("host") or {}
        if not support.doctor_host_identity_matches(host):
            return _fail(f"doctor host identity mismatch: {host!r}")
        if not support.doctor_host_live_session_matches(host):
            return _fail(f"doctor host live session mismatch: {host!r}")
        for key in (
            "binary",
            "version",
            "compatibility",
            "binary_found",
            "schema",
            "capabilities",
            "capability_sources",
        ):
            if live_host.get(key) != host.get(key):
                return _fail(
                    f"live vs doctor host {key} mismatch: "
                    f"{live_host.get(key)!r} != {host.get(key)!r}"
                )
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

        from omg_cli.agent_policy import list_agent_policies
        from omg_cli.agent_policy_ux import (
            INSPECT_ABSENT_DOCTOR_LINE,
            format_doctor_routing_human,
        )
        from omg_cli.medley_inspect import inspect_source_for, resolve_host_snapshot

        snap, inspect_doc = resolve_host_snapshot()
        source = inspect_source_for(inspect_doc)
        if source != "absent":
            return _fail(f"stock inspect_source={source!r}")
        doctor_human = format_doctor_routing_human(snap, inspect_source=source)
        if INSPECT_ABSENT_DOCTOR_LINE not in doctor_human:
            return _fail(f"doctor missing inspect-absent line: {doctor_human!r}")
        if "not installation failed" not in doctor_human:
            return _fail("doctor missing not-installation-failed")
        rows = list_agent_policies(
            root=root,
            project_root=project,
            host=snap,
            inspect_doc=inspect_doc,
        )
        if not rows:
            return _fail("list_agent_policies returned no rows")
        for row in rows:
            payload_row = row.to_json()
            facts = payload_row.get("host_facts") or {}
            if payload_row.get("inspect_source") != "absent":
                return _fail(
                    f"{row.agent_id} inspect_source="
                    f"{payload_row.get('inspect_source')!r}"
                )
            if payload_row.get("attempt") is not None:
                return _fail(
                    f"{row.agent_id} fabricated attempt="
                    f"{payload_row.get('attempt')!r}"
                )
            if payload_row.get("route_receipt_digest") is not None:
                return _fail(
                    f"{row.agent_id} fabricated receipt="
                    f"{payload_row.get('route_receipt_digest')!r}"
                )
            if facts.get("medley_capability_outcome") != "unsupported":
                return _fail(f"{row.agent_id} medley outcome not unsupported")
            if facts.get("route_specific_facts") != "unavailable":
                return _fail(f"{row.agent_id} route facts not unavailable")

        support.assert_blocker_raises()
        if "medley" in sys.modules or any(k.startswith("medley.") for k in sys.modules):
            return _fail("medley appeared in sys.modules after surfaces")
        if os_home_mismatch(iso):
            return _fail("isolation HOME/GROK_HOME drifted")
        if "omg_cli.integrate" in sys.modules:
            import omg_cli.integrate as _ig

            captured_real_popen_guarded = _popen_is_guarded(_ig._REAL_POPEN)
            if not captured_real_popen_guarded:
                return _fail("omg_cli.integrate._REAL_POPEN is not the guarded Popen")
            integrate_imported = True
        # madmax may be imported via doctor → team.plane. Its os.execvp is
        # denied at call time. posix_spawn stays unpatched; fail if an
        # imported OMG module actually calls it.
        spawn_hits = support.imported_omg_posix_spawn_calls()
        if spawn_hits:
            return _fail(f"imported omg_cli calls posix_spawn: {spawn_hits}")

        result = {
            "ok": True,
            "blocker_installed_before_omg_cli": True,
            "guards_installed_before_omg_cli": guards_installed_before_omg_cli,
            "imported": list(support.SMOKE_IMPORTED),
            "surfaces": list(support.SMOKE_SURFACES),
            "doctor_checks_ok": list(support.REQUIRED_DOCTOR_CHECKS),
            "setup_omg_dir": True,
            "blocker_raises": True,
            "import_probe_network": import_probe_network,
            "import_probe_subprocess": import_probe_subprocess,
            "captured_system_popen_guarded": captured_system_popen_guarded,
            "posix_spawn_unpatched": posix_spawn_unpatched,
            "imported_posix_spawn_calls": spawn_hits,
            "madmax_imported": "omg_cli.madmax" in sys.modules,
            "integrate_imported": integrate_imported,
            "captured_real_popen_guarded": captured_real_popen_guarded,
            "live_canonical_host_probe": True,
            "live_session_caps_ok": True,
            "capability_sources": dict(support.EXPECTED_LIVE_CAPABILITY_SOURCES),
            "inspect_source": "absent",
            "agents_attempt_null": True,
            "doctor_inspect_absent": True,
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
