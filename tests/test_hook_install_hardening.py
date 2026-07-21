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
