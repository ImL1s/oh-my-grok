"""#67-A: Antigravity provider discovery / capabilities / doctor (hermetic)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity"
FAKE_AGY = FIXTURES / "fake_agy.py"


def _install_fake_agy(bin_dir: Path, *, version: str | None = None) -> Path:
    """Place an executable named ``agy`` that runs the hermetic stub."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "agy"
    if version is None:
        script = (
            "#!/usr/bin/env python3\n"
            "import runpy, sys\n"
            f"sys.argv[0] = {str(target)!r}\n"
            f"raise SystemExit(runpy.run_path({str(FAKE_AGY)!r}, run_name='__main__'))\n"
        )
    else:
        script = (
            "#!/usr/bin/env python3\n"
            "import os, runpy, sys\n"
            f"os.environ['FAKE_AGY_VERSION'] = {version!r}\n"
            f"sys.argv[0] = {str(target)!r}\n"
            f"raise SystemExit(runpy.run_path({str(FAKE_AGY)!r}, run_name='__main__'))\n"
        )
    target.write_text(script, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


@pytest.fixture
def fake_agy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    path = _install_fake_agy(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    return path


# ---------------------------------------------------------------------------
# Models / package surface
# ---------------------------------------------------------------------------


def test_models_export_compat_and_capabilities() -> None:
    from omg_cli.providers.models import CompatStatus, ProviderCapabilities

    assert "compatible" in CompatStatus.__args__  # type: ignore[attr-defined]
    caps = ProviderCapabilities(
        provider="antigravity",
        binary="/tmp/agy",
        version="1.1.10",
        version_tuple=(1, 1, 10),
        compat_status="compatible",
    )
    payload = caps.to_dict()
    assert payload["schema"] == "omg-provider-capabilities/v1"
    assert payload["provider"] == "antigravity"
    json.dumps(payload)  # must be JSON-serializable


def test_discover_binary_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from omg_cli.providers.antigravity import discover_binary
    from omg_cli.providers.errors import ProviderBinaryMissing

    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    with pytest.raises(ProviderBinaryMissing):
        discover_binary()


def test_discover_binary_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.providers.antigravity import discover_binary

    path = _install_fake_agy(tmp_path / "bin")
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("PATH", str(tmp_path / "missing"))
    assert Path(discover_binary()) == path.resolve()


def test_probe_version_parse(fake_agy_path: Path) -> None:
    from omg_cli.providers.antigravity import discover_binary, probe_version

    binary = discover_binary()
    info = probe_version(binary)
    assert info.raw == FIXTURES.joinpath("version.txt").read_text(encoding="utf-8").strip()
    assert info.as_tuple() == (1, 1, 10)


def test_compat_classification_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.providers.antigravity import (
        TESTED_MAX,
        TESTED_MIN,
        classify_compat,
        parse_version,
    )

    assert TESTED_MIN <= (1, 1, 10) <= TESTED_MAX
    assert classify_compat(parse_version("1.1.10")) == "compatible"
    assert classify_compat(parse_version("0.9.0")) == "too_old"
    assert classify_compat(parse_version("9.9.9")) == "too_new"
    assert classify_compat(None) == "unknown"


def test_probe_capabilities_golden_schema(fake_agy_path: Path) -> None:
    from omg_cli.providers.antigravity import probe_capabilities

    caps = probe_capabilities()
    data = caps.to_dict()
    assert data["schema"] == "omg-provider-capabilities/v1"
    assert data["provider"] == "antigravity"
    assert data["binary"]
    assert data["version"] == "1.1.10"
    assert data["compat"]["status"] == "compatible"
    assert data["compat"]["pin_revision"].startswith("bfab12da")
    assert data["ready"]["installed"] is True
    assert data["ready"]["compatible"] is True
    assert data["ready"]["live_call_ready"] is False  # never claim in #67-A
    assert "json" in data["supports"]["output_formats"]
    assert "stream-json" in data["supports"]["output_formats"]
    assert data["supports"]["print_mode"] is True
    assert data["platform"]["needs_pty"] is True
    # Round-trip JSON
    assert json.loads(json.dumps(data)) == data


def test_doctor_strict_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.providers.antigravity import doctor

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)

    report = doctor(strict=True)
    assert report.ok is False
    assert report.exit_code == 1
    assert any("install" in c.lower() or "missing" in c.lower() for c in report.checks)

    # Compatible fake → strict ok
    _install_fake_agy(tmp_path / "ok-bin")
    monkeypatch.setenv("PATH", str(tmp_path / "ok-bin"))
    ok = doctor(strict=True)
    assert ok.ok is True
    assert ok.exit_code == 0

    # Too-old → strict fails; non-strict soft
    _install_fake_agy(tmp_path / "old-bin", version="0.1.0")
    monkeypatch.setenv("PATH", str(tmp_path / "old-bin"))
    strict_old = doctor(strict=True)
    assert strict_old.exit_code == 1
    soft_old = doctor(strict=False)
    assert soft_old.exit_code == 0
    assert soft_old.ok is False or any("WARN" in c or "too_old" in c for c in soft_old.checks)


def test_cli_capabilities_json(fake_agy_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from omg_cli.main import main

    rc = main(["provider", "antigravity", "capabilities", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    # Envelope may wrap domain payload; accept either shape.
    body = data.get("capabilities") or data
    if "ok" in data and data.get("command"):
        body = {k: v for k, v in data.items() if k not in {"ok", "schema_version", "command"}}
        if "capabilities" in data:
            body = data["capabilities"]
    assert body["schema"] == "omg-provider-capabilities/v1"
    assert body["provider"] == "antigravity"
    assert body["ready"]["live_call_ready"] is False


def test_cli_doctor_strict_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omg_cli.main import main

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    rc = main(["provider", "antigravity", "doctor", "--strict"])
    assert rc == 1


def test_no_shell_in_version_probe(fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """probe_version must use argv subprocess, never shell=True."""
    import subprocess

    from omg_cli.providers import antigravity as agy_mod

    calls: list[dict] = []
    real_run = subprocess.run

    def tracking_run(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", tracking_run)
    # Also patch the module-level binding if imported
    if hasattr(agy_mod, "subprocess"):
        monkeypatch.setattr(agy_mod.subprocess, "run", tracking_run)

    binary = agy_mod.discover_binary()
    agy_mod.probe_version(binary)
    assert calls, "expected subprocess.run"
    for call in calls:
        kwargs = call["kwargs"]
        assert kwargs.get("shell") in (None, False)
        argv = call["args"][0] if call["args"] else kwargs.get("args")
        assert isinstance(argv, (list, tuple))
        assert all(isinstance(x, str) for x in argv)


def test_ask_agy_still_fail_closed() -> None:
    """#67-A must not cut over ask."""
    from omg_cli.ask.providers import AskProviderError, normalize_provider

    with pytest.raises(AskProviderError):
        normalize_provider("agy")


def test_registry_lists_provider() -> None:
    from omg_cli.command_registry import KNOWN_SUBCOMMANDS
    from omg_cli.main import build_parser

    assert "provider" in KNOWN_SUBCOMMANDS
    parser = build_parser()
    ns = parser.parse_args(["provider", "antigravity", "capabilities", "--json"])
    assert callable(getattr(ns, "func", None))
