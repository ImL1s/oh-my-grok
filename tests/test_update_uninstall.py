"""Tests for omg update / uninstall (spec-first; no real grok/git)."""

from __future__ import annotations

import os
import json
import hashlib
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from omg_cli.setup_cmd import install_package, read_install_receipt


ROOT = Path(__file__).resolve().parents[1]


class _InstalledHost:
    def __init__(self) -> None:
        self.installed: Path | None = None
        self.enabled = False

    def __call__(self, argv, *args, **kwargs):
        command = [str(item) for item in argv]
        if command[:4] == ["grok", "plugin", "list", "--json"]:
            rows = []
            if self.installed is not None:
                version = json.loads((self.installed / "plugin.json").read_text())["version"]
                rows = [{
                    "name": "oh-my-grok",
                    "version": version,
                    "path": str(self.installed),
                    "source": str(self.installed),
                    "enabled": self.enabled,
                }]
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if command[:3] == ["grok", "plugin", "validate"]:
            return SimpleNamespace(returncode=0, stdout="valid\n", stderr="")
        if command[:3] == ["grok", "plugin", "install"]:
            self.installed = Path(command[3]).resolve()
            return SimpleNamespace(returncode=0, stdout="installed\n", stderr="")
        if command[:3] == ["grok", "plugin", "uninstall"]:
            self.installed = None
            self.enabled = False
            return SimpleNamespace(returncode=0, stdout="removed\n", stderr="")
        if command[:3] == ["grok", "plugin", "enable"]:
            self.enabled = True
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if command[:3] == ["grok", "plugin", "disable"]:
            self.enabled = False
            return SimpleNamespace(returncode=0, stdout="disabled\n", stderr="")
        if command[:3] == ["grok", "inspect", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"plugin": "oh-my-grok", "skills": ["omg-autopilot"]}),
                stderr="",
            )
        raise AssertionError(command)


def _doctor_ok(_stage: Path, _env: dict[str, str]):
    return {"argv": ["omg", "doctor", "--strict"], "rc": 0, "stdout": "ok\n", "stderr": "", "valid": True}


def _rewrite_receipt(path: Path, mutate) -> dict:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    mutate(receipt)
    material = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    body = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    receipt["receipt_hash"] = hashlib.sha256(body).hexdigest()
    path.chmod(0o600)
    path.write_text(
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    path.chmod(0o400)
    return receipt


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
    assert code == 1  # hard failure is surfaced; no false success

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "your plugin may be REMOVED" in combined
    assert "grok plugin install" in combined
    assert "install-plugin.sh exited" in combined
    assert "rc=1" in combined
    assert loud_stdout in captured.out or loud_stdout in combined


def test_run_update_refuses_dirty_source_before_fetch_or_install(tmp_path: Path, capsys):
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    install_sh = scripts / "install-plugin.sh"
    install_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    install_sh.chmod(0o755)
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        calls.append(list(argv))
        if "status" in argv:
            return SimpleNamespace(returncode=0, stdout=" M local.txt\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert run_update(root=tmp_path, runner=runner) == 2
    assert not any("fetch" in call or "pull" in call for call in calls)
    assert not any("install-plugin.sh" in str(item) for call in calls for item in call)
    assert "preserved" in capsys.readouterr().err


def test_development_update_uses_receipt_original_source_not_immutable_stage(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=f"{ROOT}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 0
    git_roots = {Path(call[2]) for call in calls if call[:2] == ["git", "-C"]}
    assert git_roots == {ROOT}
    assert stage not in git_roots
    assert [str(ROOT / "scripts" / "install-plugin.sh")] in calls


def test_development_update_refuses_drifted_receipt_source_before_git(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(".git", ".omg", "__pycache__", "*.pyc"),
    )
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    host = _InstalledHost()
    install_package(
        source,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    (source / "plugin.json").write_text(
        (source / "plugin.json").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        calls.append([str(item) for item in argv])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 1
    assert calls == []


def test_installed_cli_update_runs_from_proven_source_checkout(tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    shutil.copytree(
        ROOT,
        seed,
        ignore=shutil.ignore_patterns(".git", ".omg", "__pycache__", "*.pyc"),
    )
    marker = tmp_path / "installer-cwd.txt"
    installer = seed / "scripts" / "install-plugin.sh"
    installer.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$PWD\" > \"$OMG_TEST_MARKER\"\n",
        encoding="utf-8",
    )
    installer.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.name=OMG Test",
            "-c",
            "user.email=omg@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True)
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(remote), str(source)], check=True)

    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    host = _InstalledHost()
    installed = install_package(
        source,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "GROK_HOME": str(grok_home),
            "OMG_TEST_MARKER": str(marker),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [str(home / ".local" / "bin" / "omg"), "update"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8").strip() == str(source)
    assert str(source) in result.stdout
    assert str(stage) not in marker.read_text(encoding="utf-8")


def test_receipt_owned_uninstall_preserves_user_rules_and_project_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    project_state = tmp_path / "project" / ".omg" / "state.json"
    project_state.parent.mkdir(parents=True)
    project_state.write_text("keep\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    rules = grok_home / "rules" / "omg.md"
    rules.write_text(
        rules.read_text(encoding="utf-8")
        + f"\n{USER_POLICY_START}\nkeep user text byte-for-byte\n{USER_POLICY_END}\n",
        encoding="utf-8",
    )

    assert run_uninstall(yes=True, runner=host, home=grok_home) == 0
    assert host.installed is None
    assert not stage.exists()
    assert not os.path.lexists(grok_home / "omg" / "current")
    assert not os.path.lexists(grok_home / "omg" / "current-receipt")
    assert not os.path.lexists(home / ".local" / "bin" / "omg")
    assert not (grok_home / "hooks" / "omg-pretool-deny.json").exists()
    assert "keep user text byte-for-byte" in rules.read_text(encoding="utf-8")
    assert OMG_START not in rules.read_text(encoding="utf-8")
    assert project_state.read_text(encoding="utf-8") == "keep\n"
    terminal = [
        read_install_receipt(path)
        for path in (grok_home / "omg" / "receipts").glob("*.json")
        if read_install_receipt(path)["status"] == "uninstalled"
    ]
    assert len(terminal) == 1
    assert run_uninstall(yes=True, runner=host, home=grok_home) == 0


def test_receipt_uninstall_refuses_drifted_managed_guidance_before_host_mutation(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    rules = grok_home / "rules" / "omg.md"
    rules.write_text(
        rules.read_text(encoding="utf-8").replace(
            "# oh-my-grok",
            "# locally changed oh-my-grok",
            1,
        ),
        encoding="utf-8",
    )

    assert run_uninstall(yes=True, runner=host, home=grok_home) == 1
    assert host.installed == stage
    assert (grok_home / "omg" / "current").resolve(strict=True) == stage
    assert (home / ".local" / "bin" / "omg").is_symlink()


def test_receipt_uninstall_refuses_out_of_store_receipt_without_touching_victim(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    pointer = grok_home / "omg" / "current-receipt"
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    forged = victim / "forged.json"
    forged.write_bytes(Path(installed["receipt_path"]).read_bytes())
    forged.chmod(0o400)
    pointer.unlink()
    pointer.symlink_to(forged)

    assert run_uninstall(yes=True, runner=host, home=grok_home) == 1
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert host.installed == stage
    assert pointer.resolve(strict=True) == forged


@pytest.mark.parametrize("attack", ["out_of_store", "symlink_escape", "plugin_mismatch"])
def test_receipt_uninstall_confines_stage_and_plugin_before_host_mutation(
    tmp_path,
    monkeypatch,
    attack,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    original_stage = Path(installed["stage_path"])
    receipt_path = Path(installed["receipt_path"])
    victim = tmp_path / "victim-stage"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")

    if attack == "symlink_escape":
        forged_stage = grok_home / "omg" / "releases" / "forged"
        forged_stage.symlink_to(victim, target_is_directory=True)
    else:
        forged_stage = victim

    def mutate(receipt):
        if attack == "plugin_mismatch":
            receipt["installed"]["plugin_realpath"] = str(victim)
        else:
            receipt["installed"]["stage_realpath"] = str(forged_stage)
            receipt["installed"]["plugin_realpath"] = str(forged_stage)

    _rewrite_receipt(receipt_path, mutate)

    assert run_uninstall(yes=True, runner=host, home=grok_home) == 1
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert host.installed == original_stage
    assert receipt_path.exists()


def test_release_update_never_executes_foreign_receipt_install_script(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    pointer = grok_home / "omg" / "current-receipt"
    foreign = tmp_path / "foreign"
    (foreign / "scripts").mkdir(parents=True)
    script = foreign / "scripts" / "install.sh"
    script.write_text("#!/bin/sh\ntouch should-not-exist\n", encoding="utf-8")
    script.chmod(0o755)
    forged = foreign / "forged.json"
    forged.write_bytes(Path(installed["receipt_path"]).read_bytes())
    forged.chmod(0o400)
    _rewrite_receipt(forged, lambda receipt: receipt.__setitem__("mode", "release"))
    pointer.unlink()
    pointer.symlink_to(forged)
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        calls.append([str(item) for item in argv])
        return SimpleNamespace(returncode=1, stdout="", stderr="refused")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 1
    assert not any(call[:2] == ["bash", str(script)] for call in calls)
    assert not (tmp_path / "should-not-exist").exists()


def test_release_update_executes_only_verified_stage_installer(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    _rewrite_receipt(
        Path(installed["receipt_path"]),
        lambda receipt: receipt.__setitem__("mode", "release"),
    )
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        calls.append([str(item) for item in argv])
        return SimpleNamespace(returncode=1, stdout="", stderr="injected stop")

    assert run_update(runner=runner, home=home, grok_home=grok_home) == 1
    assert calls == [["bash", str(stage / "scripts" / "install.sh")]]


@pytest.mark.parametrize("pointer_name", ["current", "cli"])
def test_receipt_uninstall_requires_exact_owned_pointer_targets(
    tmp_path,
    monkeypatch,
    pointer_name,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    victim = tmp_path / "victim-target"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    pointer = (
        grok_home / "omg" / "current"
        if pointer_name == "current"
        else home / ".local" / "bin" / "omg"
    )
    pointer.unlink()
    pointer.symlink_to(victim)

    assert run_uninstall(yes=True, runner=host, home=grok_home) == 1
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert host.installed == stage
    assert pointer.resolve(strict=True) == victim


@pytest.mark.parametrize("failure_surface", ["hook", "guidance"])
def test_receipt_uninstall_rolls_back_managed_files_on_unlink_failure(
    tmp_path,
    monkeypatch,
    failure_surface,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    current = grok_home / "omg" / "current"
    receipt_pointer = grok_home / "omg" / "current-receipt"
    cli_pointer = home / ".local" / "bin" / "omg"
    hook_json = grok_home / "hooks" / "omg-pretool-deny.json"
    hook_py = grok_home / "hooks" / "omg_pretool_deny_standalone.py"
    rules = grok_home / "rules" / "omg.md"
    snapshots = {
        path: (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in (hook_json, hook_py, rules)
    }
    failed_path = hook_py if failure_surface == "hook" else rules
    real_unlink = Path.unlink

    def fail_one_unlink(path: Path, *args, **kwargs):
        if path == failed_path:
            raise OSError("injected unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one_unlink)
    assert run_uninstall(yes=True, runner=host, home=grok_home) == 1

    for path, (content, mode) in snapshots.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode
    assert stage.is_dir()
    assert current.resolve(strict=True) == stage
    assert receipt_pointer.resolve(strict=True).is_file()
    assert cli_pointer.resolve(strict=True) == (stage / "bin" / "omg").resolve()
    assert not any(
        read_install_receipt(path)["status"] == "uninstalled"
        for path in (grok_home / "omg" / "receipts").glob("*.json")
    )

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert run_uninstall(yes=True, runner=host, home=grok_home) == 0
    assert not stage.exists()


def test_receipt_uninstall_cli_unlink_failure_is_hard_and_restores_plugin(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    receipt = Path(installed["receipt_path"])
    current = grok_home / "omg" / "current"
    receipt_pointer = grok_home / "omg" / "current-receipt"
    cli_pointer = home / ".local" / "bin" / "omg"
    managed = (
        grok_home / "hooks" / "omg-pretool-deny.json",
        grok_home / "hooks" / "omg_pretool_deny_standalone.py",
        grok_home / "rules" / "omg.md",
    )
    snapshots = {
        path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in managed
    }
    real_unlink = Path.unlink

    def fail_cli_unlink(path: Path, *args, **kwargs):
        if path == cli_pointer:
            raise OSError("injected CLI collision")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cli_unlink)
    assert run_uninstall(yes=True, runner=host, home=grok_home) == 1

    assert host.installed == stage
    assert host.enabled is True
    assert stage.is_dir()
    assert receipt.is_file()
    assert current.resolve(strict=True) == stage
    assert receipt_pointer.resolve(strict=True) == receipt
    assert cli_pointer.is_symlink()
    assert cli_pointer.resolve(strict=True) == stage / "bin" / "omg"
    for path, (content, mode) in snapshots.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode
    assert not any(
        read_install_receipt(path)["status"] == "uninstalled"
        for path in (grok_home / "omg" / "receipts").glob("*.json")
    )


def test_receipt_uninstall_rollback_preserves_disabled_plugin_state(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _InstalledHost()
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    host.enabled = False
    rules = grok_home / "rules" / "omg.md"
    real_unlink = Path.unlink

    def fail_rules_unlink(path: Path, *args, **kwargs):
        if path == rules:
            raise OSError("injected rules failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_rules_unlink)
    assert run_uninstall(yes=True, runner=host, home=grok_home) == 1
    assert host.installed == stage
    assert host.enabled is False


# ---------------------------------------------------------------------------
# Grok-managed host-copy install model (plugin path != immutable stage)
# ---------------------------------------------------------------------------


class _CopyingHost:
    """Match current Grok: install copies bytes, uninstall deletes that copy."""

    def __init__(self, grok_home: Path) -> None:
        self.grok_home = grok_home
        self.installed: Path | None = None
        self.enabled = False

    def __call__(self, argv, *args, **kwargs):
        command = [str(item) for item in argv]
        if command[:4] == ["grok", "plugin", "list", "--json"]:
            rows = []
            if self.installed is not None:
                version = json.loads((self.installed / "plugin.json").read_text())[
                    "version"
                ]
                rows = [{
                    "name": "oh-my-grok",
                    "version": version,
                    "path": str(self.installed),
                    "source": str(self.installed),
                    "enabled": self.enabled,
                }]
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if command[:3] == ["grok", "plugin", "validate"]:
            return SimpleNamespace(returncode=0, stdout="valid\n", stderr="")
        if command[:3] == ["grok", "plugin", "install"]:
            source = Path(command[3]).resolve()
            if not source.is_dir():
                return SimpleNamespace(returncode=1, stdout="", stderr="missing\n")
            destination = self.grok_home / "installed-plugins" / "oh-my-grok"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            for directory in (destination, *destination.rglob("*")):
                if directory.is_dir():
                    directory.chmod(0o755)
            self.installed = destination.resolve()
            self.enabled = False
            return SimpleNamespace(returncode=0, stdout="installed copy\n", stderr="")
        if command[:3] == ["grok", "plugin", "uninstall"]:
            if self.installed is not None and self.installed.exists():
                shutil.rmtree(self.installed)
            self.installed = None
            self.enabled = False
            return SimpleNamespace(returncode=0, stdout="removed copy\n", stderr="")
        if command[:3] == ["grok", "plugin", "enable"]:
            if self.installed is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="absent\n")
            self.enabled = True
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if command[:3] == ["grok", "plugin", "disable"]:
            self.enabled = False
            return SimpleNamespace(returncode=0, stdout="disabled\n", stderr="")
        if command[:3] == ["grok", "inspect", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"plugin": "oh-my-grok", "skills": ["omg-autopilot"]}),
                stderr="",
            )
        raise AssertionError(command)


def test_receipt_uninstall_succeeds_for_grok_managed_host_copy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _CopyingHost(grok_home)
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    copy_path = host.installed
    assert copy_path is not None
    assert copy_path.parent == (grok_home / "installed-plugins").resolve()
    assert copy_path != stage

    assert run_uninstall(yes=True, runner=host, home=grok_home) == 0
    assert host.installed is None
    assert not stage.exists()
    assert not os.path.lexists(grok_home / "omg" / "current")
    assert not os.path.lexists(grok_home / "omg" / "current-receipt")
    terminal = [
        read_install_receipt(path)
        for path in (grok_home / "omg" / "receipts").glob("*.json")
        if read_install_receipt(path)["status"] == "uninstalled"
    ]
    assert len(terminal) == 1


def test_receipt_uninstall_rollback_reinstalls_host_copy_from_stage(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    grok_home = tmp_path / "grok-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    host = _CopyingHost(grok_home)
    installed = install_package(
        ROOT,
        home=home,
        grok_home=grok_home,
        runner=host,
        doctor_probe=_doctor_ok,
        mode="development",
    )
    stage = Path(installed["stage_path"])
    receipt = Path(installed["receipt_path"])
    copy_path = host.installed
    assert copy_path is not None and copy_path != stage
    enabled_before = host.enabled
    cli_pointer = home / ".local" / "bin" / "omg"
    real_unlink = Path.unlink

    def fail_cli_unlink(path: Path, *args, **kwargs):
        if path == cli_pointer:
            raise OSError("injected CLI collision")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cli_unlink)
    assert run_uninstall(yes=True, runner=host, home=grok_home) == 1

    assert host.installed == copy_path
    assert host.enabled is enabled_before
    assert stage.is_dir()
    assert receipt.is_file()
    assert (grok_home / "omg" / "current").resolve(strict=True) == stage
    assert cli_pointer.is_symlink()
    assert not any(
        read_install_receipt(path)["status"] == "uninstalled"
        for path in (grok_home / "omg" / "receipts").glob("*.json")
    )
