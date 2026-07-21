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


def test_plugins_scan_ignores_non_plugin_like_and_denylist(tmp_path):
    """Only _is_plugin_like dirs are risks; cache/marketplaces/tmp are denylisted."""
    plugins = tmp_path / ".claude" / "plugins"
    plugins.mkdir(parents=True)

    # Denylist bookkeeping dirs (even if they contain random files)
    for name in ("cache", "marketplaces", "tmp", "temp"):
        d = plugins / name
        d.mkdir()
        (d / "random.bin").write_bytes(b"x")

    # Plain empty / non-plugin directory must NOT be reported
    (plugins / "not-a-plugin").mkdir()
    (plugins / "not-a-plugin" / "readme.txt").write_text("hi", encoding="utf-8")

    # Real plugin-like install must still be reported
    real = plugins / "real-plugin"
    real.mkdir()
    (real / "plugin.json").write_text('{"name":"real-plugin"}', encoding="utf-8")

    findings = scan_claude_plugins(home=tmp_path)
    risks = [f for f in findings if f.level != "ok" and f.code == "claude.plugins"]
    assert len(risks) == 1
    detail = risks[0].detail
    assert "real-plugin" in detail
    for name in ("cache", "marketplaces", "tmp", "temp", "not-a-plugin"):
        assert name not in detail


def test_plugins_scan_denylist_only_is_ok(tmp_path):
    """plugins/ with only cache/marketplaces/tmp → ok, not a risk."""
    plugins = tmp_path / ".claude" / "plugins"
    for name in ("cache", "marketplaces", "tmp"):
        (plugins / name).mkdir(parents=True)
    findings = scan_claude_plugins(home=tmp_path)
    assert not any(f.level != "ok" and f.code == "claude.plugins" for f in findings)
    assert any(
        f.level == "ok" and "no plugin-like" in f.detail for f in findings
    )


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


def test_spawn_subagent_bare_mention_is_not_a_risk(tmp_path):
    """Descriptive Grok tool-name mention must not false-positive as OMC routing."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text(
        "enforces `capability_mode` on `spawn_subagent`. It is not a sandbox.",
        encoding="utf-8",
    )
    findings = scan_claude_md(home=tmp_path / "empty-home", project_root=proj)
    by_path = {f.path: f for f in findings}
    assert str(proj / "CLAUDE.md") in by_path
    assert by_path[str(proj / "CLAUDE.md")].level == "ok"


def test_spawn_subagent_call_syntax_still_flagged(tmp_path):
    """Call-shape spawn_subagent(...) remains a routing-risk marker."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text(
        "call spawn_subagent(prompt=...) now",
        encoding="utf-8",
    )
    findings = scan_claude_md(home=tmp_path / "empty-home", project_root=proj)
    risks = [f for f in findings if f.level != "ok" and f.code == "claude.md.markers"]
    assert risks
    assert "spawn_subagent" in risks[0].detail


def test_spawn_subagent_arrow_trigger_still_flagged(tmp_path):
    """Keyword→action arrow form remains a routing-risk marker."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text(
        'Keyword triggers: "spawn_subagent"→spawn',
        encoding="utf-8",
    )
    findings = scan_claude_md(home=tmp_path / "empty-home", project_root=proj)
    risks = [f for f in findings if f.level != "ok" and f.code == "claude.md.markers"]
    assert risks
    assert "spawn_subagent" in risks[0].detail


def test_repo_own_claude_md_is_not_flagged_by_compat_scan():
    """Canary: repo's own CLAUDE.md (Grok-plugin docs) must scan OK."""
    from omg_cli.doctor import plugin_root

    root = plugin_root()
    claude_md = root / "CLAUDE.md"
    assert claude_md.is_file(), f"missing canary file: {claude_md}"
    findings = scan_claude_md(
        home=Path("/nonexistent-omg-compat-home"),
        project_root=root,
    )
    project_hits = [f for f in findings if f.path == str(claude_md)]
    assert project_hits, f"no finding for {claude_md}: {findings!r}"
    assert project_hits[0].level == "ok", project_hits[0]
