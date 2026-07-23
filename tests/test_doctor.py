# tests/test_doctor.py
"""Tests for omg_cli.doctor — hard checks, soft trust inventory, --strict."""
from __future__ import annotations

import json
from pathlib import Path
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
    # New contract: the global hook must be the self-contained standalone under
    # $GROK_HOME/hooks (installed transactionally), and the check runs a real
    # behavioral smoke. Install via the shared installer, then it must PASS.
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    from omg_cli import hook_install

    gh = tmp_path / ".grok"
    _json_path, action = hook_install.install_global_hook(home=gh)
    assert action in ("created", "updated", "migrated"), action
    name, ok, detail = doctor.check_global_pretool_hook()
    assert ok is True, detail
    assert "omg-pretool-deny" in detail and "smoke" in detail


def test_check_global_pretool_hook_rejects_checkout_path(tmp_path, monkeypatch):
    # A checkout-path script (outside $GROK_HOME) is the pre-fix bug — MUST FAIL.
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    hooks = tmp_path / ".grok" / "hooks"
    hooks.mkdir(parents=True)
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
                                    "command": 'python3 "/Users/x/Documents/mine/oh-my-grok/hooks/bin/pre_tool_use_deny.py"',
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
    assert ok is False
    assert "escapes" in detail.lower()


def test_check_global_pretool_hook_rejects_extra_command(tmp_path, monkeypatch):
    # A second command hook is a second chance to exit 2 and block — MUST FAIL.
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    from omg_cli import hook_install

    gh = tmp_path / ".grok"
    hook_install.install_global_hook(home=gh)
    jpath = gh / "hooks" / "omg-pretool-deny.json"
    data = json.loads(jpath.read_text())
    data["hooks"]["PreToolUse"][0]["hooks"].append(
        {"type": "command", "command": 'python3 "/tmp/evil.py"', "timeout": 5}
    )
    jpath.write_text(json.dumps(data))
    name, ok, detail = doctor.check_global_pretool_hook()
    assert ok is False
    assert "command hooks" in detail


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


# --- plugin version drift + enabled soft checks (Batch 3) ---


def _local_plugin_version() -> str:
    data = json.loads(
        (Path(doctor.plugin_root()) / "plugin.json").read_text(encoding="utf-8")
    )
    return str(data["version"])


def test_check_plugin_version_drift_ok_when_match(monkeypatch):
    local = _local_plugin_version()
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "version": local,
                "source": "/tmp/oh-my-grok",
            }
        ],
    )
    name, level, detail = doctor.check_plugin_version_drift()
    assert name == "plugin version drift"
    assert level == "ok"
    assert local in detail
    assert "installed == local" in detail


def test_check_plugin_version_drift_warn_on_mismatch(monkeypatch):
    local = _local_plugin_version()
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "version": "0.0.1",
                "source": "/tmp/oh-my-grok",
            }
        ],
    )
    name, level, detail = doctor.check_plugin_version_drift()
    assert name == "plugin version drift"
    assert level == "warn"
    assert "0.0.1" in detail
    assert local in detail
    assert "!=" in detail or "mismatch" in detail.lower()


def test_check_plugin_version_drift_warn_on_duplicate_sources(monkeypatch):
    local = _local_plugin_version()
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "id": "abc/oh-my-grok",
                "version": local,
                "source": "/path/a/oh-my-grok",
            },
            {
                "name": "oh-my-grok",
                "id": "def/oh-my-grok",
                "version": local,
                "source": "/path/b/oh-my-grok",
            },
        ],
    )
    name, level, detail = doctor.check_plugin_version_drift()
    assert name == "plugin version drift"
    assert level == "warn"
    assert "duplicate" in detail.lower() or "uninstall" in detail.lower()


def test_check_plugin_enabled_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[plugins]\nenabled = ["oh-my-grok"]\n',
        encoding="utf-8",
    )
    name, level, detail = doctor.check_plugin_enabled()
    assert name == "plugin enabled ([plugins].enabled)"
    assert level == "ok"
    assert "oh-my-grok" in detail


def test_check_plugin_enabled_warn_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[plugins]\nenabled = ["other"]\n',
        encoding="utf-8",
    )
    name, level, detail = doctor.check_plugin_enabled()
    assert name == "plugin enabled ([plugins].enabled)"
    assert level == "warn"
    assert "NOT in" in detail or "enable" in detail.lower()


def test_check_plugin_enabled_warn_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    # no config.toml
    name, level, detail = doctor.check_plugin_enabled()
    assert name == "plugin enabled ([plugins].enabled)"
    assert level == "warn"
    assert "config.toml" in detail.lower() or "cannot confirm" in detail.lower()


# --- installed capabilities lock (OMX-parity installed drift) ---


def test_check_installed_capabilities_lock_ok(tmp_path, monkeypatch):
    """Fake installed dir matching checkout lock → ok (no real grok)."""
    import importlib.util
    import sys

    gen_script = Path(doctor.plugin_root()) / "scripts" / "generate_capabilities_lock.py"
    spec = importlib.util.spec_from_file_location("generate_capabilities_lock", gen_script)
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)

    checkout = tmp_path / "checkout"
    checkout.mkdir(parents=True)
    (checkout / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "1.0.0"}),
        encoding="utf-8",
    )
    skill = checkout / "skills" / "omg-a" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill-a\n", encoding="utf-8")
    agent = checkout / "agents" / "omg-b.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("agent-b\n", encoding="utf-8")
    gen.write_lock(checkout)

    installed = tmp_path / "installed-plugins" / "oh-my-grok-key"
    (installed / "skills" / "omg-a").mkdir(parents=True)
    (installed / "skills" / "omg-a" / "SKILL.md").write_text("skill-a\n", encoding="utf-8")
    (installed / "agents").mkdir(parents=True)
    (installed / "agents" / "omg-b.md").write_text("agent-b\n", encoding="utf-8")
    (installed / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "1.0.0"}),
        encoding="utf-8",
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
    assert level == "ok"
    assert "match committed lock" in detail


def test_check_installed_capabilities_lock_mismatch(tmp_path, monkeypatch):
    """Installed skill content differs from committed lock → warn."""
    import importlib.util
    import sys

    gen_script = Path(doctor.plugin_root()) / "scripts" / "generate_capabilities_lock.py"
    spec = importlib.util.spec_from_file_location("generate_capabilities_lock", gen_script)
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)

    checkout = tmp_path / "checkout"
    checkout.mkdir(parents=True)
    (checkout / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "1.0.0"}),
        encoding="utf-8",
    )
    skill = checkout / "skills" / "omg-a" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill-a\n", encoding="utf-8")
    agent = checkout / "agents" / "omg-b.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("agent-b\n", encoding="utf-8")
    gen.write_lock(checkout)

    installed = tmp_path / "installed-plugins" / "oh-my-grok-key"
    (installed / "skills" / "omg-a").mkdir(parents=True)
    (installed / "skills" / "omg-a" / "SKILL.md").write_text(
        "skill-a-MODIFIED\n", encoding="utf-8"
    )
    (installed / "agents").mkdir(parents=True)
    (installed / "agents" / "omg-b.md").write_text("agent-b\n", encoding="utf-8")

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


def test_check_installed_capabilities_lock_unavailable(monkeypatch):
    """Probe None → warn without crash."""
    monkeypatch.setattr(doctor, "_run_grok_json", lambda *_a, **_k: None)
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "warn"
    assert "cannot locate installed snapshot" in detail


def test_check_installed_release_identity_absent_is_honest_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    name, level, detail = doctor.check_installed_release_identity()
    assert name == "immutable install identity"
    assert level == "warn"
    assert "development/source" in detail


def test_check_installed_release_identity_corrupt_pointer_is_hard_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    current = grok_home / "omg" / "current"
    current.parent.mkdir(parents=True)
    current.write_text("foreign", encoding="utf-8")
    name, level, detail = doctor.check_installed_release_identity()
    assert name == "immutable install identity"
    assert level == "fail"
    assert "pointers" in detail
