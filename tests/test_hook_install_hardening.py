"""Hardening regression tests — each encodes a Codex delta-review blocker fix.

B1 doctor false-green on an exit-2 launcher; B2 installer must smoke the STAGING
copy before replacing the live script; B3 shlex.quote neutralizes $GROK_HOME shell
injection; B4 generator must reject nested/relative/late non-stdlib imports;
B5 label 'repaired' when the script was missing but the json was canonical.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_GEN_PATH = ROOT / "scripts" / "generate_standalone_hook.py"


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_standalone_test", _GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- B4: generator import guard
def test_generator_rejects_nested_nonstdlib_import():
    gen = _load_gen()
    src = "import os\n\ndef f():\n    import requests\n    return requests\n"
    with pytest.raises(SystemExit):
        gen._validate_stdlib_only(src, "test")


def test_generator_rejects_relative_import():
    gen = _load_gen()
    with pytest.raises(SystemExit):
        gen._validate_stdlib_only("from . import evil\n", "test")


def test_generator_rejects_toplevel_nonstdlib_import():
    gen = _load_gen()
    with pytest.raises(SystemExit):
        gen._validate_stdlib_only("import requests\n", "test")


def test_generator_rejects_late_toplevel_import():
    # A late top-level import would otherwise silently strip preceding globals.
    gen = _load_gen()
    with pytest.raises(SystemExit):
        gen._deny_body_after_imports("import os\n\nX = 1\n\nimport re\n")


# ---------------------------------------------------------------- B3: shlex.quote injection
def test_launcher_neutralizes_shell_injection_path():
    from omg_cli.hook_install import launcher_command

    evil = Path('/tmp/a"; exit 2; #/x.py')  # would break naive double-quotes → inject exit 2
    cmd = launcher_command(evil)
    assert "|| true" in cmd
    # Running it must NOT exit 2 from the injected `exit 2`: the path is a single
    # nonexistent arg → python rc 2 → `|| true` → rc 0. If injection succeeded, rc 2.
    r = subprocess.run(["/bin/sh", "-c", cmd], input="{}", capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"injection not neutralized: {cmd!r}"


# ---------------------------------------------------------------- B2: stage → smoke → publish
def test_install_rejects_deny_for_benign_candidate(tmp_path):
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    _p, a = hi.install_global_hook(home=gh)
    assert a == "created"
    good_py = (gh / "hooks" / hi.STANDALONE_BASENAME).read_bytes()

    # A malicious/broken "committed" standalone that denies EVERY input (deny JSON at
    # rc 0 — which `|| true` cannot neutralize). It must fail the staging smoke and
    # NEVER replace the live good script.
    badbin = tmp_path / "badrepo" / "hooks" / "bin"
    badbin.mkdir(parents=True)
    (badbin / hi.STANDALONE_BASENAME).write_text(
        'import json, sys\nprint(json.dumps({"decision": "deny"}))\nsys.exit(0)\n'
    )
    _p, a2 = hi.install_global_hook(home=gh, root=tmp_path / "badrepo")
    assert a2.startswith("failed"), a2
    assert (gh / "hooks" / hi.STANDALONE_BASENAME).read_bytes() == good_py  # live script untouched


# ---------------------------------------------------------------- B5: repaired label
def test_install_repaired_when_script_missing(tmp_path):
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hi.install_global_hook(home=gh)
    (gh / "hooks" / hi.STANDALONE_BASENAME).unlink()  # canonical json, but script gone
    _p, a = hi.install_global_hook(home=gh)
    assert a == "repaired"
    assert (gh / "hooks" / hi.STANDALONE_BASENAME).is_file()


# ---------------------------------------------------------------- B1: doctor catches exit-2 launcher
def test_doctor_rejects_exit2_injected_command(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    from omg_cli import doctor, hook_install as hi

    gh = tmp_path / ".grok"
    hi.install_global_hook(home=gh)
    jpath = gh / "hooks" / hi.HOOK_JSON_NAME
    data = json.loads(jpath.read_text())
    data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] += "; exit 2"  # Codex's injection
    jpath.write_text(json.dumps(data, indent=2) + "\n")
    name, ok, detail = doctor.check_global_pretool_hook()
    assert ok is False, detail  # non-canonical command (and the rc-0 smoke would also catch it)


def test_doctor_rejects_noncanonical_command(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    from omg_cli import doctor, hook_install as hi

    gh = tmp_path / ".grok"
    hi.install_global_hook(home=gh)
    jpath = gh / "hooks" / hi.HOOK_JSON_NAME
    data = json.loads(jpath.read_text())
    # a bare `python3 "<path>"` (no -I -S, no || true) — the OLD brittle style, still
    # under $GROK_HOME so the escape check passes; must fail the canonical-command check.
    py = gh / "hooks" / hi.STANDALONE_BASENAME
    data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = f'python3 "{py}"'
    jpath.write_text(json.dumps(data, indent=2) + "\n")
    name, ok, detail = doctor.check_global_pretool_hook()
    assert ok is False and "canonical" in detail


# --- Codex re-verify round: 4 remaining blockers ---

def test_install_quarantine_left_active_reported(tmp_path, monkeypatch):
    # B1: if quarantine can't remove a dangerous json on a failed install, report it.
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hooks = gh / "hooks"
    hooks.mkdir(parents=True)
    bad = {"hooks": {"PreToolUse": [{"matcher": hi.MATCHER, "hooks": [
        {"type": "command", "command": 'python3 "/Users/x/Documents/oh-my-grok/hooks/bin/pre_tool_use_deny.py"', "timeout": 5}
    ]}]}}
    (hooks / hi.HOOK_JSON_NAME).write_text(json.dumps(bad))
    # bad candidate → smoke fails → except path → quarantine, but pretend it can't remove
    badbin = tmp_path / "badrepo" / "hooks" / "bin"
    badbin.mkdir(parents=True)
    (badbin / hi.STANDALONE_BASENAME).write_text(
        'import json,sys\nprint(json.dumps({"decision":"deny"}))\nsys.exit(0)\n'
    )
    monkeypatch.setattr(hi, "_quarantine", lambda p: (p, False))
    _p, a = hi.install_global_hook(home=gh, root=tmp_path / "badrepo")
    assert a == "failed:QuarantineLeftActive", a


def test_generator_rejects_import_alias():
    gen = _load_gen()
    with pytest.raises(SystemExit):
        gen._validate_stdlib_only("import os as operating_system\n", "test")


def test_generator_rejects_from_import_alias():
    gen = _load_gen()
    with pytest.raises(SystemExit):
        gen._validate_stdlib_only("from typing import Any as A\n", "test")


def test_generator_rejects_dynamic_import():
    gen = _load_gen()
    with pytest.raises(SystemExit):
        gen._validate_stdlib_only('x = __import__("requests")\n', "test")


def test_doctor_passes_metacharacter_grok_home(tmp_path, monkeypatch):
    # B3: a canonical install under a $GROK_HOME containing shell metacharacters must
    # PASS — doctor compares the exact canonical command (no regex path extraction).
    gh = tmp_path / 'gh"weird'
    monkeypatch.setenv("GROK_HOME", str(gh))
    from omg_cli import doctor, hook_install as hi

    _p, action = hi.install_global_hook(home=gh)
    assert action == "created", action
    name, ok, detail = doctor.check_global_pretool_hook()
    assert ok is True, detail


def test_install_repaired_on_mode_change(tmp_path):
    # B4: same script bytes but a repaired mode/type is 'repaired', not 'unchanged'.
    import os
    import stat as _stat
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hi.install_global_hook(home=gh)
    py = gh / "hooks" / hi.STANDALONE_BASENAME
    os.chmod(py, 0o600)  # wrong mode, identical bytes
    _p, a = hi.install_global_hook(home=gh)
    assert a == "repaired", a
    assert _stat.S_IMODE(os.stat(py).st_mode) == 0o755


# --- Codex re-verify round 2: 3 remaining edge cases ---

def test_install_quarantines_dangling_symlink_json(tmp_path):
    # B1(lexists): a DANGLING symlink json is 'present' and must be quarantined, not ignored.
    import os
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hooks = gh / "hooks"
    hooks.mkdir(parents=True)
    jpath = hooks / hi.HOOK_JSON_NAME
    os.symlink(str(tmp_path / "no-such-target.json"), str(jpath))  # dangling
    assert os.path.lexists(jpath) and not jpath.is_file()
    _p, a = hi.install_global_hook(home=gh, root=tmp_path / "no-such-root")
    assert a == "quarantined-no-source", a
    assert not os.path.lexists(jpath)  # dangling symlink renamed away (grok won't discover it)


def test_generator_rejects_unprovided_from_binding():
    # B2: `from os import environ` is stdlib but binds a name the header doesn't provide;
    # stripping it would leave `environ` unbound in the body.
    gen = _load_gen()
    with pytest.raises(SystemExit):
        gen._deny_body_after_imports("from os import environ\n\nX = environ\n")


def test_generator_rejects_rebound_dynamic_import():
    # B2: `loader = __import__; loader(...)` rebinds the dynamic-import builtin.
    gen = _load_gen()
    with pytest.raises(SystemExit):
        gen._validate_stdlib_only("loader = __import__\n", "test")


def test_install_repaired_on_json_mode_change(tmp_path):
    # B4: the json's own mode/type repair is also 'repaired', not 'unchanged'.
    import os
    import stat as _stat
    from omg_cli import hook_install as hi

    gh = tmp_path / ".grok"
    hi.install_global_hook(home=gh)
    jp = gh / "hooks" / hi.HOOK_JSON_NAME
    os.chmod(jp, 0o600)  # wrong mode, canonical content
    _p, a = hi.install_global_hook(home=gh)
    assert a == "repaired", a
    assert _stat.S_IMODE(os.stat(jp).st_mode) == 0o644
