"""Tests for omg update / uninstall (spec-first; no real grok/git)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

from omg_cli.guidance import (
    OMG_END,
    OMG_START,
    USER_POLICY_END,
    USER_POLICY_START,
    install_global_rules,
    uninstall_global_rules,
)
from omg_cli.uninstall_cmd import run_uninstall
from omg_cli.update_cmd import run_update


def _fake_runner():
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_uninstall_global_rules_removed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    path, _ = install_global_rules(version="1.0.0", home=tmp_path)
    assert path.exists()
    assert OMG_START in path.read_text(encoding="utf-8")

    out_path, action = uninstall_global_rules(home=tmp_path)
    assert out_path == path
    assert action == "removed"
    assert not path.exists()


def test_uninstall_global_rules_kept_user(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    path, _ = install_global_rules(version="1.0.0", home=tmp_path)
    user_block = (
        f"{USER_POLICY_START}\n"
        "keep my custom policy\n"
        f"{USER_POLICY_END}\n"
    )
    original = path.read_text(encoding="utf-8")
    path.write_text(original.rstrip("\n") + "\n\n" + user_block, encoding="utf-8")

    out_path, action = uninstall_global_rules(home=tmp_path)
    assert out_path == path
    assert action == "kept-user"
    assert path.exists()
    final = path.read_text(encoding="utf-8")
    assert "keep my custom policy" in final
    assert USER_POLICY_START in final
    assert OMG_START not in final
    assert OMG_END not in final


def test_uninstall_global_rules_absent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    path, action = uninstall_global_rules(home=tmp_path)
    assert action == "absent"
    assert path == tmp_path / "rules" / "omg.md"
    assert not path.exists()


def test_run_uninstall_without_yes_is_noop(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    fake = _fake_runner()
    code = run_uninstall(yes=False, runner=fake, home=tmp_path)
    assert code == 0
    # No grok uninstall when dry/no-op
    grok_calls = [c for c in fake.calls if c and c[0] == "grok"]
    assert grok_calls == []
    out = capsys.readouterr().out
    assert "--yes" in out


def test_run_uninstall_yes_removes_hook_and_rules(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    hooks = tmp_path / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "omg-pretool-deny.json"
    hook.write_text("{}", encoding="utf-8")
    rules_path, _ = install_global_rules(version="1.0.0", home=tmp_path)
    assert rules_path.exists()

    fake = _fake_runner()
    code = run_uninstall(yes=True, runner=fake, home=tmp_path)
    assert code == 0

    grok_uninstall = [
        c
        for c in fake.calls
        if c[:3] == ["grok", "plugin", "uninstall"]
        or (len(c) >= 2 and c[0] == "grok" and "uninstall" in c)
    ]
    assert grok_uninstall, f"expected grok plugin uninstall in {fake.calls}"
    assert not hook.exists()
    assert not rules_path.exists()


def test_run_update_calls_git_pull_and_install_script(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    install_sh = scripts / "install-plugin.sh"
    install_sh.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    install_sh.chmod(install_sh.stat().st_mode | stat.S_IXUSR)

    fake = _fake_runner()
    code = run_update(root=tmp_path, runner=fake)
    assert code == 0

    pull_calls = [
        c
        for c in fake.calls
        if "pull" in c and ("git" in c[0] or c[0].endswith("git"))
    ]
    assert pull_calls, f"expected git pull in {fake.calls}"

    script_calls = [
        c for c in fake.calls if any("install-plugin.sh" in str(x) for x in c)
    ]
    assert script_calls, f"expected install-plugin.sh in {fake.calls}"


def test_run_update_surfaces_install_plugin_output_on_nonzero(tmp_path: Path, capsys):
    """install-plugin.sh recovery stderr must reach the user via omg update."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    install_sh = scripts / "install-plugin.sh"
    install_sh.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    install_sh.chmod(install_sh.stat().st_mode | stat.S_IXUSR)

    loud_stderr = (
        "LOUD: your plugin may be REMOVED — re-run: "
        "grok plugin install /path --trust"
    )
    loud_stdout = "refreshing…"

    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        calls.append(list(argv))
        # install-plugin.sh is the only non-git invocation
        if any("install-plugin.sh" in str(x) for x in argv):
            return SimpleNamespace(
                returncode=1,
                stdout=loud_stdout,
                stderr=loud_stderr,
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    code = run_update(root=tmp_path, runner=runner)
    assert code == 0  # update continues; surfaces script failure

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "your plugin may be REMOVED" in combined
    assert "grok plugin install" in combined
    assert "install-plugin.sh exited" in combined
    assert "rc=1" in combined
    assert loud_stdout in captured.out or loud_stdout in combined
