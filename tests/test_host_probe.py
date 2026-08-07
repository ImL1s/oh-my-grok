# tests/test_host_probe.py
"""Hermetic tests for omg_cli.host_probe (#105 PR2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.host_models import CAPABILITY_KEYS
from omg_cli.host_probe import (
    HostProbeInputs,
    evaluate_feature_gate,
    host_report_for_doctor,
    load_fixture,
    parse_host_version,
    probe_host,
    probe_host_from_fixture,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "host"


def _fixture(name: str) -> Path:
    return FIXTURES / name


def test_parse_host_version_text_and_json():
    assert parse_host_version("grok 0.2.112 (abc)") == (0, 2, 112)
    assert parse_host_version('{"currentVersion":"0.2.107"}') == (0, 2, 107)
    assert parse_host_version('{"version":"0.2.121"}') == (0, 2, 121)
    assert parse_host_version("garbage") is None


def test_legacy_0_2_107_gates():
    report = probe_host_from_fixture(_fixture("legacy-0.2.107.json"))
    assert report.version == "0.2.107"
    assert report.compatibility == "legacy"
    assert report.capabilities.session_resume is False
    assert report.capabilities.session_close is False
    gates = {g.capability: g.state for g in report.gates}
    assert gates["session_resume"] == "LEGACY"
    assert gates["session_close"] == "BLOCKED"
    assert gates["restore_code_explicit"] == "BLOCKED"
    assert gates["uuid_search"] == "LEGACY"


def test_modern_0_2_121_all_available():
    report = probe_host_from_fixture(_fixture("0.2.121.json"))
    assert report.version == "0.2.121"
    assert report.compatibility == "compatible"
    caps = report.capabilities
    assert caps.session_resume is True
    assert caps.session_close is True
    assert caps.restore_code_explicit is True
    assert caps.uuid_search is True
    assert all(g.state == "AVAILABLE" for g in report.gates)
    # Behavior layer wins truth source when present.
    assert caps.source_for("session_resume") == "behavior"


def test_version_lies_does_not_grant_resume():
    report = probe_host_from_fixture(_fixture("version-lies.json"))
    assert report.version == "0.2.121"
    assert report.capabilities.session_resume is False
    assert report.capabilities.session_close is False
    assert report.capabilities.source_for("session_resume") == "behavior"
    gates = {g.capability: g.state for g in report.gates}
    assert gates["session_resume"] == "LEGACY"
    assert gates["session_close"] == "BLOCKED"


def test_advertisement_beats_version():
    report = probe_host_from_fixture(_fixture("advertisement-beats-version.json"))
    assert report.version == "0.2.120"
    assert report.compatibility == "legacy"
    assert report.capabilities.session_resume is True
    assert report.capabilities.session_close is True
    assert report.capabilities.source_for("session_resume") == "advertisement"


def test_malformed_fail_closed():
    report = probe_host_from_fixture(_fixture("malformed.json"))
    assert report.version == "0.2.121"
    # Malformed layers poison version fallback.
    assert report.capabilities.session_resume is False
    assert report.capabilities.session_close is False
    assert any("malformed" in o.lower() for o in report.observations)


def test_legacy_no_resume_inspect_layer():
    report = probe_host_from_fixture(_fixture("legacy-no-resume.json"))
    assert report.capabilities.session_resume is False
    assert report.capabilities.source_for("session_resume") in {
        "inspect",
        "version",
        "none",
    }
    # Inspect explicitly set false → inspect source.
    assert report.capabilities.source_for("session_resume") == "inspect"


def test_required_gate_blocks_when_missing():
    report = probe_host_from_fixture(_fixture("legacy-0.2.107.json"))
    blocked = evaluate_feature_gate(
        "session_resume", report.capabilities, required=True
    )
    assert blocked.state == "BLOCKED"
    assert blocked.required is True
    assert blocked.next_action


def test_doctor_host_block_redacts_sensitive_material():
    inputs = HostProbeInputs(
        binary="grok",
        binary_found=True,
        version_text="0.2.121",
        version_json={"currentVersion": "0.2.121"},
        inspect_json={
            "capabilities": {"session_resume": True},
            "session_id": "sess-secret-should-not-appear",
            "auth": {"token": "tok_live_abc"},
            "cwd": "/Users/someone/secret-project",
            "transcript": "user said hello",
        },
        acp_advertisement={
            "methods": ["session/resume"],
            "home": "/Users/someone",
        },
        behavior={"session_resume": True},
        observations=["path=/Users/alice/.grok/sessions/x"],
    )
    report = probe_host(inputs)
    blob = json.dumps(host_report_for_doctor(report))
    for banned in (
        "sess-secret",
        "tok_live",
        "/Users/someone",
        "/Users/alice",
        "user said hello",
        "transcript",
    ):
        assert banned not in blob
    assert report.capabilities.session_resume is True


def test_load_fixture_roundtrip():
    loaded = load_fixture(_fixture("0.2.121.json"))
    assert loaded.binary == "grok"
    assert loaded.behavior is not None
    assert loaded.behavior["session_resume"] is True


@pytest.mark.parametrize("key", CAPABILITY_KEYS)
def test_capability_keys_stable(key: str):
    report = probe_host_from_fixture(_fixture("0.2.121.json"))
    assert key in report.capabilities.to_dict()
    assert key in host_report_for_doctor(report)["capabilities"]
