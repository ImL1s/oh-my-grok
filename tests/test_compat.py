# tests/test_compat.py
"""Tests for omg_cli.compat — Claude isolation scanner."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omg_cli.compat import (
    ISOLATION_ADVICE,
    CompatReport,
    compat_exit_should_fail,
    format_compat_lines,
    format_isolation_banner,
    home_dir,
    scan_claude_md,
    scan_claude_plugins,
    scan_claude_settings,
    scan_compat,
)


def test_home_dir_uses_env_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert home_dir() == tmp_path


def test_clean_home_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    report = scan_compat(home=tmp_path, project_root=tmp_path / "proj")
    assert isinstance(report, CompatReport)
    assert report.has_risks is False
    assert all(f.level == "ok" for f in report.findings)


def test_settings_hooks_detected(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "*"}]}}),
        encoding="utf-8",
    )
    findings = scan_claude_settings(home=tmp_path)
    risks = [f for f in findings if f.level != "ok"]
    assert risks
    assert any(f.code == "claude.settings.hooks" for f in risks)
    assert "hooks" in risks[0].detail.lower()


def test_settings_local_hooks_detected(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.local.json").write_text(
        json.dumps({"hooks": {"Stop": []}}),
        encoding="utf-8",
    )
    # empty list is empty — should NOT warn for empty hooks list
    findings = scan_claude_settings(home=tmp_path)
    # Stop: [] means key present but empty list → _hooks_nonempty is True for
    # dict with key Stop even if list empty? Our impl: dict with keys = non-empty.
    # Plan: "non-empty hooks" — a hooks object with keys is non-empty even if
    # matcher lists are empty. That's intentional isolation advice.
    risks = [f for f in findings if f.code == "claude.settings.hooks"]
    assert risks


def test_settings_empty_hooks_ok(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {}}),
        encoding="utf-8",
    )
    findings = scan_claude_settings(home=tmp_path)
    assert not any(f.code == "claude.settings.hooks" for f in findings)


def test_plugins_dir_detected(tmp_path):
    plugins = tmp_path / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    omc = plugins / "oh-my-claudecode"
    omc.mkdir()
    (omc / "plugin.json").write_text('{"name":"oh-my-claudecode"}', encoding="utf-8")
    findings = scan_claude_plugins(home=tmp_path)
    risks = [f for f in findings if f.level != "ok"]
    assert risks
    assert "oh-my-claudecode" in risks[0].detail


def test_project_plugins_detected(tmp_path):
    proj = tmp_path / "proj"
    pdir = proj / ".claude" / "plugins" / "some-plugin"
    pdir.mkdir(parents=True)
    (pdir / "skills").mkdir()
    findings = scan_claude_plugins(home=tmp_path / "home", project_root=proj)
    risks = [f for f in findings if f.level != "ok" and "some-plugin" in f.detail]
    assert risks


def test_claude_md_omc_markers(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text(
        "# OMC\n"
        "oh-my-claudecode orchestration\n"
        'Keyword: "ralph"→ralph\n'
        "Use Task( for subagents\n"
        "ulw mode\n",
        encoding="utf-8",
    )
    findings = scan_claude_md(home=tmp_path)
    risks = [f for f in findings if f.code == "claude.md.markers"]
    assert risks
    detail = risks[0].detail
    assert "oh-my-claudecode" in detail
    assert "Task(" in detail


def test_project_claude_md_markers(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text(
        "spawn_subagent via Claude Task(\n",
        encoding="utf-8",
    )
    findings = scan_claude_md(home=tmp_path / "empty-home", project_root=proj)
    risks = [f for f in findings if f.level != "ok"]
    assert risks
    assert "Task(" in risks[0].detail or "spawn_subagent" in risks[0].detail


def test_isolation_advice_constant():
    text = format_isolation_banner()
    assert text == ISOLATION_ADVICE
    assert "skills = false" in text
    assert "hooks = false" in text
    assert "[compat.claude]" in text


def test_format_compat_lines_warn_vs_strict(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [1]}}),
        encoding="utf-8",
    )
    report = scan_compat(home=tmp_path)
    assert report.has_risks

    warn_lines = format_compat_lines(report, strict=False)
    assert any("[WARN]" in ln for ln in warn_lines)
    assert not any(
        ln.startswith("[FAIL]") and "compat.claude.settings" in ln for ln in warn_lines
    )

    strict_lines = format_compat_lines(report, strict=True)
    assert any("[FAIL]" in ln for ln in strict_lines)
    assert compat_exit_should_fail(report, strict=True) is True
    assert compat_exit_should_fail(report, strict=False) is False


def test_scan_compat_full_risk_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"cmd": "x"}]}}),
        encoding="utf-8",
    )
    plugins = claude / "plugins" / "foo"
    plugins.mkdir(parents=True)
    (plugins / "plugin.json").write_text("{}", encoding="utf-8")
    (claude / "CLAUDE.md").write_text("oh-my-claudecode\nTask(\n", encoding="utf-8")

    report = scan_compat()
    assert report.has_risks
    codes = {f.code for f in report.risk_findings}
    assert "claude.settings.hooks" in codes
    assert "claude.plugins" in codes
    assert "claude.md.markers" in codes


def test_doctor_compat_warn_default_and_strict_fail(monkeypatch, tmp_path):
    """doctor with fake HOME containing OMC markers: default WARN/exit0, --strict FAIL."""
    from omg_cli import doctor

    monkeypatch.setenv("HOME", str(tmp_path))
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text(
        "oh-my-claudecode and Task( routing\n",
        encoding="utf-8",
    )

    # Keep hard + soft checks green so only compat risks matter for exit code
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda: [
            ("grok on PATH", True, "fake"),
            ("plugin.json", True, "ok"),
            ("hooks scripts", True, "ok"),
            ("PreToolUse hook", True, "ok"),
            ("skills omg-*", True, "ok"),
            ("agents", True, "ok"),
            ("deny module", True, "ok"),
        ],
    )
    monkeypatch.setattr(
        doctor,
        "run_soft_checks",
        lambda: [("plugin trust/inventory", "ok", "trusted=True (test)")],
    )

    rc_default = doctor.run_doctor(strict=False, project_root=tmp_path)
    assert rc_default == 0

    rc_strict = doctor.run_doctor(strict=True, project_root=tmp_path)
    assert rc_strict == 1
