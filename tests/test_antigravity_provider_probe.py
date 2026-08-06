"""#67-A: Antigravity provider discovery / capabilities / doctor (hermetic)."""

from __future__ import annotations

import json
import os
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
    py = sys.executable
    if version is None:
        script = (
            f"#!{py}\n"
            "import runpy, sys\n"
            f"sys.argv[0] = {str(target)!r}\n"
            f"raise SystemExit(runpy.run_path({str(FAKE_AGY)!r}, run_name='__main__'))\n"
        )
    else:
        script = (
            f"#!{py}\n"
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
    from typing import get_args

    from omg_cli.providers.models import CompatStatus, ProviderCapabilities

    assert set(get_args(CompatStatus)) == {
        "compatible",
        "too_old",
        "too_new",
        "unknown",
    }
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


def test_provider_capabilities_defaults_are_neutral() -> None:
    """Neutral model must not invent Antigravity-positive claims."""
    from omg_cli.providers.models import ProviderCapabilities

    caps = ProviderCapabilities(
        provider="example",
        binary="/bin/example",
        version="0.0.1",
        version_tuple=(0, 0, 1),
        compat_status="unknown",
    )
    assert caps.tested_min == ""
    assert caps.tested_max == ""
    assert caps.output_formats == ()
    assert caps.efforts == ()
    assert caps.modes == ()
    assert caps.print_mode is False
    assert caps.sandbox is False
    assert caps.agents_subcommand is False
    assert caps.needs_pty is False
    assert caps.limitations == ()
    payload = caps.to_dict()
    assert payload["supports"]["output_formats"] == []
    assert payload["platform"]["needs_pty"] is False
    assert payload["platform"]["limitations"] == []


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


def test_omg_agy_bin_rejects_non_agy_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMG_AGY_BIN must not accept an arbitrary executable name."""
    from omg_cli.providers.antigravity import discover_binary
    from omg_cli.providers.errors import ProviderBinaryMissing

    impostor = tmp_path / "not-agy"
    impostor.write_text("#!/bin/sh\necho 1.1.10\n", encoding="utf-8")
    impostor.chmod(impostor.stat().st_mode | 0o111)
    monkeypatch.setenv("OMG_AGY_BIN", str(impostor))
    with pytest.raises(ProviderBinaryMissing, match="basename"):
        discover_binary()


def test_parse_version_anchors_first_line_only() -> None:
    from omg_cli.providers.antigravity import parse_version

    assert parse_version("1.1.10\n") is not None
    assert parse_version("1.1.10\n").as_tuple() == (1, 1, 10)
    assert parse_version("1.1.10\n").raw == "1.1.10"
    # Buried semver on a later line must not parse / must not disagree with raw.
    assert parse_version("init failed\nsee 1.1.10 docs\n") is None
    # Leading junk on the version line must not match.
    assert parse_version("agy version 1.1.10") is None
    assert parse_version("prefix 1.1.10") is None


def test_parse_version_raw_is_matched_fragment_only() -> None:
    """Trailing child-controlled junk after an anchored semver must not enter raw."""
    from omg_cli.providers.antigravity import classify_compat, parse_version

    info = parse_version("1.1.10 Authorization: Bearer raw-secret-token")
    assert info is not None
    assert info.as_tuple() == (1, 1, 10)
    assert info.raw == "1.1.10"
    assert "Bearer" not in info.raw
    assert "raw-secret-token" not in info.raw
    assert classify_compat(info) == "compatible"


def test_doctor_strict_rejects_impostor_agy_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-named binary without Antigravity help identity must not strict-green."""
    from omg_cli.providers.antigravity import doctor

    path = _install_fake_agy(tmp_path / "bin")
    junk = tmp_path / "impostor-help.txt"
    # Looks like structured evidence but lacks ``Usage of agy`` identity header.
    junk.write_text(
        "Usage of other-cli:\n"
        "  --effort                        Reasoning effort (low|medium|high)\n"
        "  --output-format                 Output format (text, json, stream-json)\n"
        "  --mode                          Mode (accept-edits, plan)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("FAKE_AGY_HELP", str(junk))

    report = doctor(strict=True, binary=str(path))
    assert report.ok is False
    assert report.exit_code == 1
    assert any("identity" in c.lower() or "probe error" in c.lower() for c in report.checks)


def test_probe_version_parse(fake_agy_path: Path) -> None:
    from omg_cli.providers.antigravity import discover_binary, probe_version

    binary = discover_binary()
    info = probe_version(binary)
    assert info.raw == FIXTURES.joinpath("version.txt").read_text(encoding="utf-8").strip()
    assert info.as_tuple() == (1, 1, 10)


def test_probe_version_nonzero_with_semver_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc!=0 must fail closed even when stderr embeds a parseable semver."""
    from omg_cli.providers.antigravity import probe_version
    from omg_cli.providers.errors import ProviderVersionError

    path = _install_fake_agy(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("FAKE_AGY_VERSION_RC", "1")
    monkeypatch.setenv(
        "FAKE_AGY_VERSION_TEXT", "agy 1.1.10: initialization failed\n"
    )
    with pytest.raises(ProviderVersionError, match="exit 1"):
        probe_version(str(path))


def test_probe_help_nonzero_or_empty_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import _parse_help_supports, _probe_help_text
    from omg_cli.providers.errors import ProviderProbeError

    path = _install_fake_agy(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("OMG_AGY_BIN", str(path))

    monkeypatch.setenv("FAKE_AGY_HELP_RC", "1")
    monkeypatch.setenv("FAKE_AGY_HELP_EMPTY", "1")
    with pytest.raises(ProviderProbeError, match="exit 1"):
        _probe_help_text(str(path))

    monkeypatch.setenv("FAKE_AGY_HELP_RC", "0")
    monkeypatch.setenv("FAKE_AGY_HELP_EMPTY", "1")
    with pytest.raises(ProviderProbeError, match="empty"):
        _probe_help_text(str(path))

    # Empty help must not invent formats/efforts/modes.
    invented = _parse_help_supports("")
    assert invented["output_formats"] == ()
    assert invented["efforts"] == ()
    assert invented["modes"] == ()
    assert invented["print_mode"] is False


def test_parse_help_ignores_unrelated_substrings() -> None:
    """Loose help prose must not forge formats/efforts/modes/print_mode."""
    from omg_cli.providers.antigravity import _parse_help_supports

    colliding = """\
Usage of agy:
  --project                       Project ID allows explanation of plan at low cost
  --json-schema                   Optional JSON schema string for structured output
  --prompt-interactive            Run an initial prompt interactively
"""
    parsed = _parse_help_supports(colliding)
    assert parsed["output_formats"] == ()
    assert parsed["efforts"] == ()
    assert parsed["modes"] == ()
    assert parsed["print_mode"] is False
    assert parsed["sandbox"] is False
    assert parsed["agents_subcommand"] is False
    assert parsed["models_subcommand"] is False
    assert parsed["plugins_subcommand"] is False


def test_parse_help_reads_structured_option_enums() -> None:
    """Only option/enum/subcommand boundaries count as observed evidence."""
    from omg_cli.providers.antigravity import _parse_help_supports

    help_text = FIXTURES.joinpath("help.txt").read_text(encoding="utf-8")
    parsed = _parse_help_supports(help_text)
    assert parsed["output_formats"] == ("text", "json", "stream-json")
    assert parsed["efforts"] == ("low", "medium", "high")
    assert parsed["modes"] == ("accept-edits", "plan")
    assert parsed["print_mode"] is True
    assert parsed["sandbox"] is True
    assert parsed["agents_subcommand"] is True
    assert parsed["models_subcommand"] is True
    assert parsed["plugins_subcommand"] is True


def test_doctor_strict_rejects_colliding_help_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compatible version + colliding prose help must not strict false-green."""
    from omg_cli.providers.antigravity import doctor

    path = _install_fake_agy(tmp_path / "bin")
    junk = tmp_path / "junk-help.txt"
    junk.write_text(
        "Usage of agy:\n"
        "  --project                       Project ID allows explanation of plan at low cost\n"
        "  --json-schema                   Optional JSON schema\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("FAKE_AGY_HELP", str(junk))

    report = doctor(strict=True, binary=str(path))
    assert report.ok is False
    assert report.exit_code == 1
    assert any("no observed capability evidence" in c for c in report.checks)
    assert report.capabilities is not None
    assert report.capabilities.output_formats == ()
    assert report.capabilities.efforts == ()
    assert report.capabilities.modes == ()
    assert report.capabilities.print_mode is False


def test_doctor_strict_false_green_on_failed_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """doctor --strict must not green a binary that fails version/help probes."""
    from omg_cli.providers.antigravity import doctor

    path = _install_fake_agy(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("FAKE_AGY_VERSION_RC", "1")
    monkeypatch.setenv(
        "FAKE_AGY_VERSION_TEXT", "agy 1.1.10: initialization failed\n"
    )
    report = doctor(strict=True, binary=str(path))
    assert report.ok is False
    assert report.exit_code == 1
    assert any("probe error" in c.lower() or "FAIL" in c for c in report.checks)

    monkeypatch.delenv("FAKE_AGY_VERSION_RC", raising=False)
    monkeypatch.delenv("FAKE_AGY_VERSION_TEXT", raising=False)
    monkeypatch.setenv("FAKE_AGY_HELP_RC", "1")
    monkeypatch.setenv("FAKE_AGY_HELP_EMPTY", "1")
    help_fail = doctor(strict=True, binary=str(path))
    assert help_fail.ok is False
    assert help_fail.exit_code == 1


def test_compat_classification_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.providers.antigravity import (
        TESTED_MAX,
        TESTED_MIN,
        classify_compat,
        parse_version,
    )

    # Compat window is fixture-backed (version.txt pin only).
    assert TESTED_MIN == TESTED_MAX == (1, 1, 10)
    pin = FIXTURES.joinpath("version.txt").read_text(encoding="utf-8").strip()
    assert pin == "1.1.10"
    assert classify_compat(parse_version(pin)) == "compatible"
    assert classify_compat(parse_version("1.1.0")) == "too_old"
    assert classify_compat(parse_version("1.1.99")) == "too_new"
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
    body = data.get("capabilities", data)
    assert body["schema"] == "omg-provider-capabilities/v1"
    assert body["provider"] == "antigravity"
    assert body["ready"]["live_call_ready"] is False


def test_cli_routes_through_provider_adapter(
    fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.commands import provider as provider_cmd
    from omg_cli.main import main
    from omg_cli.providers.antigravity import AntigravityProvider
    from omg_cli.providers.base import ProviderAdapter

    seen: list[ProviderAdapter] = []
    real = provider_cmd._resolve_adapter

    def tracking(name: str) -> ProviderAdapter:
        adapter = real(name)
        seen.append(adapter)
        assert isinstance(adapter, ProviderAdapter)
        assert isinstance(adapter, AntigravityProvider)
        assert adapter.name == "antigravity"
        return adapter

    monkeypatch.setattr(provider_cmd, "_resolve_adapter", tracking)
    assert main(["provider", "antigravity", "capabilities", "--json"]) == 0
    assert main(["provider", "antigravity", "doctor", "--strict"]) == 0
    assert len(seen) >= 2


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


def test_cli_doctor_json_failure_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Strict doctor failure must use failure() envelope (error code, ok=false)."""
    from omg_cli.cli_envelope import SCHEMA_VERSION
    from omg_cli.main import main

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    rc = main(["provider", "antigravity", "doctor", "--strict", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "provider.antigravity.doctor"
    err = payload.get("error") or {}
    assert err.get("code") == "E_PROVIDER_DOCTOR" or payload.get("error_code") == (
        "E_PROVIDER_DOCTOR"
    )
    assert "message" in payload or (isinstance(err, dict) and err.get("message"))


def test_provider_json_redacts_child_secret_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Child stderr/stdout secrets must not leak into JSON failure envelopes."""
    from omg_cli.main import main
    from omg_cli.redaction import REDACTED

    path = _install_fake_agy(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("FAKE_AGY_VERSION_RC", "1")
    monkeypatch.setenv(
        "FAKE_AGY_VERSION_TEXT",
        "init failed Authorization: Bearer raw-secret-token api_key=leak-me\n",
    )
    rc = main(["provider", "antigravity", "capabilities", "--json"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "raw-secret-token" not in out
    assert "leak-me" not in out
    assert REDACTED in out

    rc = main(["provider", "antigravity", "doctor", "--strict", "--json"])
    assert rc == 1
    doc_out = capsys.readouterr().out
    assert "raw-secret-token" not in doc_out
    assert "leak-me" not in doc_out
    assert REDACTED in doc_out


def test_capabilities_success_and_doctor_text_redact_version_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Success capabilities + text doctor must redact child-controlled version text."""
    import dataclasses

    from omg_cli.main import main
    from omg_cli.providers import antigravity as agy_mod
    from omg_cli.redaction import REDACTED

    path = _install_fake_agy(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("OMG_AGY_BIN", str(path))

    real_probe = agy_mod.probe_capabilities

    def probe_with_secret_version(*args, **kwargs):
        caps = real_probe(*args, **kwargs)
        return dataclasses.replace(
            caps,
            version="1.1.10 Authorization: Bearer raw-secret-token",
        )

    monkeypatch.setattr(agy_mod, "probe_capabilities", probe_with_secret_version)
    monkeypatch.setattr(
        agy_mod.AntigravityProvider,
        "probe_capabilities",
        lambda self: probe_with_secret_version(),
    )

    rc = main(["provider", "antigravity", "capabilities", "--json"])
    assert rc == 0
    caps_out = capsys.readouterr().out
    assert "raw-secret-token" not in caps_out
    assert REDACTED in caps_out

    rc = main(["provider", "antigravity", "doctor"])
    assert rc == 0
    doc_out = capsys.readouterr().out
    assert "raw-secret-token" not in doc_out
    assert REDACTED in doc_out


def test_doctor_json_preserves_supports_models_boolean(
    fake_agy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recursive doctor redaction must not coerce supports.models bool → string."""
    from omg_cli.main import main

    rc = main(["provider", "antigravity", "doctor", "--strict", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    doctor = payload.get("doctor") or {}
    caps = doctor.get("capabilities") or {}
    supports = caps.get("supports") or {}
    assert supports.get("models") is True
    assert isinstance(supports.get("models"), bool)


def test_help_truncated_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Truncated help must not feed partial enums into strict doctor."""
    from omg_cli.providers import antigravity as agy_mod
    from omg_cli.providers.errors import ProviderProbeError
    from omg_cli.providers.process import ProbeProcessResult

    def trunc_help(argv, **kwargs):
        text = (
            "Usage of agy:\n"
            "  --effort                        Reasoning effort (low|medium|high)\n"
        )
        return ProbeProcessResult(
            argv=tuple(argv),
            returncode=0,
            stdout=text,
            stderr="",
            stdout_truncated=True,
        )

    monkeypatch.setattr(agy_mod, "run_probe_process", trunc_help)
    with pytest.raises(ProviderProbeError, match="truncated"):
        agy_mod._probe_help_text("/tmp/agy")


def test_cli_doctor_json_success_envelope(
    fake_agy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omg_cli.cli_envelope import SCHEMA_VERSION
    from omg_cli.main import main

    rc = main(["provider", "antigravity", "doctor", "--strict", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "provider.antigravity.doctor"
    assert "error" not in payload
    doctor = payload.get("doctor") or payload.get("data") or {}
    assert doctor.get("ready") is True or payload.get("ready") is True


def test_cli_doctor_json_nonstrict_degraded_keeps_success_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-strict soft incompat: exit 0 + outer ok=true; readiness nested false."""
    from omg_cli.cli_envelope import SCHEMA_VERSION
    from omg_cli.main import main

    _install_fake_agy(tmp_path / "old-bin", version="0.1.0")
    monkeypatch.setenv("PATH", str(tmp_path / "old-bin"))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    rc = main(["provider", "antigravity", "doctor", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "provider.antigravity.doctor"
    assert payload.get("ready") is False
    doctor = payload.get("doctor") or {}
    assert doctor.get("ok") is False


def test_provider_ignores_stale_project_root(
    fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """provider is install/global scoped — stale OMG_PROJECT_ROOT must not block."""
    from omg_cli.main import main

    missing = tmp_path / "no-such-project-root"
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(missing))
    rc = main(["provider", "antigravity", "doctor", "--strict"])
    assert rc == 0


def test_no_shell_in_version_probe(fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """probe_version must use argv process runner, never shell=True."""
    import subprocess

    from omg_cli.providers import antigravity as agy_mod
    from omg_cli.providers import process as process_mod

    popen_calls: list[dict] = []
    real_popen = subprocess.Popen

    class TrackingPopen(real_popen):  # type: ignore[valid-type,misc]
        def __init__(self, *args, **kwargs):
            popen_calls.append({"args": args, "kwargs": dict(kwargs)})
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", TrackingPopen)
    monkeypatch.setattr(process_mod.subprocess, "Popen", TrackingPopen)

    binary = agy_mod.discover_binary()
    agy_mod.probe_version(binary)
    assert popen_calls, "expected subprocess.Popen"
    for call in popen_calls:
        kwargs = call["kwargs"]
        assert kwargs.get("shell") in (None, False)
        if os.name == "posix":
            assert kwargs.get("start_new_session") is True
        argv = kwargs.get("args") or (call["args"][0] if call["args"] else None)
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


def test_antigravity_provider_satisfies_adapter_protocol() -> None:
    from omg_cli.providers.antigravity import AntigravityProvider
    from omg_cli.providers.base import ProviderAdapter

    adapter = AntigravityProvider()
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.name == "antigravity"
