# tests/test_doctor.py
"""Tests for omg_cli.doctor — hard checks, soft trust inventory, --strict."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omg_cli import doctor


def test_soft_gate_footer_constant():
    assert "fail-open" in doctor.SOFT_GATE_FOOTER.lower()
    assert "soft-gate" in doctor.SOFT_GATE_FOOTER or "soft-gate" in doctor.SOFT_GATE_FOOTER.lower()


def test_check_plugin_trust_unavailable_when_no_grok(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    name, level, detail = doctor.check_plugin_trust()
    assert name == "plugin trust/inventory"
    assert level == "warn"
    assert "inspect unavailable" in detail.lower()


def test_effective_discovery_foreign_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/grok")
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: {
            "plugins": [
                {"name": "oh-my-grok"},
                {"name": "oh-my-claudecode", "path": "/x/omc"},
            ]
        },
    )
    name, level, detail = doctor.check_effective_discovery_foreign()
    assert "foreign" in name
    assert level == "warn"
    assert "oh-my-claudecode" in detail


def test_effective_discovery_clean_ok(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/grok")
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: {"plugins": [{"name": "oh-my-grok"}]},
    )
    name, level, detail = doctor.check_effective_discovery_foreign()
    assert level == "ok"


def test_check_plugin_trust_probe_failure_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/grok")
    monkeypatch.setattr(doctor, "_run_grok_json", lambda *_a, **_k: None)
    name, level, detail = doctor.check_plugin_trust()
    assert level == "warn"
    assert "inspect unavailable" in detail.lower()


def test_check_plugin_trust_parses_details_json(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/grok")

    payload = {
        "name": "oh-my-grok",
        "version": "0.1.0",
        "enabled": True,
        "trusted": True,
        "hooks": {"PreToolUse": []},
    }

    def fake_run(argv, **_k):
        if "details" in argv:
            return payload
        return None

    monkeypatch.setattr(doctor, "_run_grok_json", fake_run)
    name, level, detail = doctor.check_plugin_trust()
    assert level == "ok"
    assert "trusted=True" in detail
    assert "enabled=True" in detail


def test_check_plugin_trust_untrusted_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/grok")
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: {
            "name": "oh-my-grok",
            "enabled": True,
            "trusted": False,
        },
    )
    name, level, detail = doctor.check_plugin_trust()
    assert level == "warn"
    assert "trusted=False" in detail


def test_check_plugin_trust_list_missing_plugin(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/grok")

    def fake_run(argv, **_k):
        if "list" in argv:
            return [{"name": "other-plugin", "enabled": True}]
        return None

    monkeypatch.setattr(doctor, "_run_grok_json", fake_run)
    name, level, detail = doctor.check_plugin_trust()
    assert level == "warn"
    assert "not listed" in detail.lower()


def test_run_doctor_prints_soft_gate_footer(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
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
        lambda: [("plugin trust/inventory", "warn", "inspect unavailable (test)")],
    )

    rc = doctor.run_doctor(strict=False, project_root=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert doctor.SOFT_GATE_FOOTER in out
    assert "inspect unavailable" in out
    assert "[WARN]" in out


def test_run_doctor_strict_soft_warn_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
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
        lambda: [("plugin trust/inventory", "warn", "inspect unavailable (test)")],
    )

    rc = doctor.run_doctor(strict=True, project_root=tmp_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "plugin trust" in out.lower() or "inspect unavailable" in out


def test_run_doctor_strict_with_fake_home_compat(monkeypatch, tmp_path, capsys):
    """doctor --strict with fake HOME containing OMC markers → FAIL."""
    monkeypatch.setenv("HOME", str(tmp_path))
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("oh-my-claudecode Task(\n", encoding="utf-8")

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
        lambda: [("plugin trust/inventory", "ok", "trusted=True")],
    )

    rc = doctor.run_doctor(strict=True, project_root=tmp_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "compat" in out.lower()
    assert doctor.SOFT_GATE_FOOTER in out


def test_summarize_plugin_payload_from_list():
    data = [
        {"name": "other", "enabled": True},
        {"name": "oh-my-grok", "enabled": True, "trusted": True, "version": "0.1.0"},
    ]
    result = doctor._summarize_plugin_payload(data, source="plugin list")
    assert result is not None
    name, level, detail = result
    assert name == "plugin trust/inventory"
    assert level == "ok"
    assert "0.1.0" in detail


def test_check_global_rules_missing_warns(tmp_path, monkeypatch):
    """No rules file under GROK_HOME → soft warn suggesting omg setup."""
    grok_home = tmp_path / ".grokhome"
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    name, level, detail = doctor.check_global_rules()
    assert "global rules" in name
    assert level == "warn"
    assert "omg setup" in detail.lower()


def test_check_global_rules_ok_after_install(tmp_path, monkeypatch):
    """After install_global_rules under GROK_HOME → soft ok."""
    from omg_cli.guidance import install_global_rules

    grok_home = tmp_path / ".grokhome"
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    install_global_rules()
    name, level, detail = doctor.check_global_rules()
    assert "global rules" in name
    assert level == "ok"
    assert "present" in detail.lower() or "v" in detail


def test_check_global_pretool_hook_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    name, ok, detail = doctor.check_global_pretool_hook()
    assert ok is False
    assert "missing" in detail.lower() or "not found" in detail.lower()


def test_check_global_pretool_hook_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hooks = tmp_path / ".grok" / "hooks"
    hooks.mkdir(parents=True)
    # MUST be named pre_tool_use_deny.py so path regex matches
    deny = tmp_path / "pre_tool_use_deny.py"
    deny.write_text("print(1)\n", encoding="utf-8")
    deny.chmod(0o755)
    (hooks / "omg-pretool-deny.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "run_terminal_command|Bash|Shell|spawn_subagent|Task",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'python3 "{deny}"',
                                    "timeout": 5,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    name, ok, detail = doctor.check_global_pretool_hook()
    assert ok is True
    assert str(deny) in detail or "omg-pretool-deny" in detail


def test_check_global_pretool_hook_broken_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hooks = tmp_path / ".grok" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "omg-pretool-deny.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": 'python3 "/no/such/pre_tool_use_deny.py"',
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    name, ok, detail = doctor.check_global_pretool_hook()
    assert ok is False
