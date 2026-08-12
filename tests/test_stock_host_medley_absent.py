"""Hermetic evidence for docs/architecture/agent-model-routing.md Decision.

Absence of Medley must not disable ordinary OMG operation. This file is
stock-host smoke (setup / package projection, current ``omg doctor``,
ordinary agent/profile discovery, ordinary workflow parser/inventory). It
is not a routing implementation and does not exercise #131 / #134 / #138.

Absence is an explicit import blocker, never inferred from directory names
on ``sys.path`` / ``PYTHONPATH``. An injected installable ``medley`` stays
discoverable until that blocker is installed. Ancestor pathnames may
contain the substring ``medley``; the blocker never inspects them.

The smoke process is an explicit allowlisted environment: fake HOME /
GROK_HOME / XDG dirs, scrubbed credentials, bounded PATH, fail-closed
fake grok, network denial, and subprocess/exec guards. Ambient
site-packages and PYTHONPATH are not inherited.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

from omg_cli import doctor
from omg_cli.hook_install import (
    STANDALONE_BASENAME,
    _stage_file,
    committed_standalone,
    launcher_command,
)
from omg_cli.setup_cmd import compute_package_identity, run_setup
from omg_cli.team.roles import CANONICAL_ROLES
from omg_cli.workflows.schema import compile_workflow

ROOT = Path(__file__).resolve().parents[1]
GEN_SCRIPT = ROOT / "scripts" / "generate_capabilities_lock.py"
HOST_FIXTURE = ROOT / "tests" / "fixtures" / "host" / "0.2.121.json"
WORKFLOW_FIXTURE = (
    ROOT / "tests" / "fixtures" / "workflow" / "production-safety-review-v1.json"
)
_BLOCKER_MSG = "stock-host import blocker"

_REQUIRE_MEDLEY_CLAIMS = (
    "requires medley",
    "require medley",
    "medley required",
    "medley is required",
    "must install medley",
    "need medley",
    "routing-availability",
    "routing availability",
)


class _StockHostMedleyImportBlocker(importlib.abc.MetaPathFinder):
    """Raise for ``medley`` / ``medley.*`` so an installed optional copy is hidden."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname == "medley" or fullname.startswith("medley."):
            raise ModuleNotFoundError(f"{_BLOCKER_MSG}: {fullname}", name=fullname)
        return None


def _this_file_does_not_import_medley() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith(("import ", "from ")):
            assert not stripped.startswith("import medley")
            assert not stripped.startswith("from medley")


_CREDENTIAL_MARKERS = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTHORIZATION",
    "BEARER",
)
_ALLOWED_ENV = frozenset(
    {
        "HOME",
        "GROK_HOME",
        "PATH",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
    }
)
_NETWORK_DENIED = "stock-host smoke: network denied"
_SUBPROCESS_DENIED = "stock-host smoke: subprocess denied"
_EXEC_DENIED = "stock-host smoke: process-exec denied"
_FAKE_GROK_UNEXPECTED = "stock-host fake grok: unexpected argv"


class _StockHostIsolation(NamedTuple):
    home: Path
    grok_home: Path
    bin_dir: Path
    xdg: Path
    grok: Path


def _evict_medley_modules(monkeypatch) -> None:
    for key in [k for k in sys.modules if k == "medley" or k.startswith("medley.")]:
        monkeypatch.delitem(sys.modules, key)


def _is_credential_key(name: str) -> bool:
    upper = name.upper()
    if upper.startswith("MEDLEY"):
        return True
    return any(marker in upper for marker in _CREDENTIAL_MARKERS)


def _runtime_sys_path() -> list[str]:
    """Stdlib + this checkout only. Do not filter names for ``medley``.

    Compare realpaths so Homebrew Cellar vs opt prefixes still keep stdlib.
    Drop inherited ``site-packages`` / ``dist-packages``; the injected vendor
    site is prepended separately and is never inferred from path names.
    """
    prefixes: list[str] = []
    for raw in (
        sys.base_prefix,
        sys.base_exec_prefix,
        sys.prefix,
        sys.exec_prefix,
        str(ROOT),
    ):
        if raw:
            prefixes.append(os.path.realpath(raw))
    kept: list[str] = []
    seen: set[str] = set()
    for part in sys.path:
        if not part:
            continue
        real = os.path.realpath(part)
        if real in seen:
            continue
        base = os.path.basename(real.rstrip(os.sep))
        if base in {"site-packages", "dist-packages"}:
            continue
        if any(real == prefix or real.startswith(prefix + os.sep) for prefix in prefixes):
            kept.append(part)
            seen.add(real)
    return kept


def _allowlisted_env(*, home: Path, grok_home: Path, xdg: Path, bin_dir: Path) -> dict[str, str]:
    tmp = xdg / "tmp"
    runtime = xdg / "runtime"
    for path in (
        xdg / "config",
        xdg / "data",
        xdg / "cache",
        xdg / "state",
        runtime,
        tmp,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "GROK_HOME": str(grok_home),
        "PATH": str(bin_dir),
        "TMPDIR": str(tmp),
        "TMP": str(tmp),
        "TEMP": str(tmp),
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_STATE_HOME": str(xdg / "state"),
        "XDG_RUNTIME_DIR": str(runtime),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _replace_environ(monkeypatch, env: dict[str, str]) -> None:
    for key in list(os.environ):
        if key not in env:
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _link_python(bin_dir: Path) -> None:
    target = Path(sys.executable).resolve()
    python3 = bin_dir / "python3"
    python = bin_dir / "python"
    if not python3.exists():
        python3.symlink_to(target)
    if not python.exists():
        python.symlink_to(target)


def _is_fake_grok_argv0(raw: str, bin_dir: Path) -> bool:
    if raw == "grok":
        return True
    try:
        return Path(raw).resolve() == (bin_dir / "grok").resolve()
    except OSError:
        return False


_STAGE_BASENAME_RE = re.compile(
    r"^\.omg_pretool_deny_standalone\.py\.stage\.[a-z0-9_]+\.tmp$"
)


def _usable_grok_home(grok_home: Path | None) -> Path | None:
    if grok_home is None:
        return None
    try:
        gh = Path(grok_home)
        if not str(gh).strip():
            return None
        return gh
    except (OSError, TypeError, ValueError):
        return None


def _is_reviewed_python_argv(args: Sequence[object], raw: str, grok_home: Path) -> bool:
    """Hook-install staged smoke: exact ``python3 -I -S <stage.tmp>``."""
    if raw != "python3" or len(args) != 4:
        return False
    if str(args[1]) != "-I" or str(args[2]) != "-S":
        return False
    gh = _usable_grok_home(grok_home)
    if gh is None:
        return False
    candidate = Path(str(args[3]))
    if _STAGE_BASENAME_RE.fullmatch(candidate.name) is None:
        return False
    try:
        if candidate.parent.resolve() != (gh / "hooks").resolve():
            return False
        st = os.lstat(candidate)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False
        actual = hashlib.sha256(candidate.read_bytes()).digest()
        expected = hashlib.sha256(committed_standalone().read_bytes()).digest()
    except (OSError, TypeError, ValueError):
        return False
    return actual == expected


def _is_reviewed_hook_shell_argv(args: Sequence[object], raw: str, grok_home: Path) -> bool:
    """Doctor hook smoke: exact ``/bin/sh -c`` + ``launcher_command`` tuple."""
    if raw != "/bin/sh" or len(args) != 3:
        return False
    gh = _usable_grok_home(grok_home)
    if gh is None:
        return False
    try:
        expected = (
            "/bin/sh",
            "-c",
            launcher_command(gh / "hooks" / STANDALONE_BASENAME),
        )
    except (OSError, TypeError, ValueError):
        return False
    return tuple(str(a) for a in args) == expected


def _allowed_subprocess_argv(
    args: Sequence[object] | str | None, bin_dir: Path, grok_home: Path
) -> bool:
    if args is None or isinstance(args, str) or not args:
        return False
    raw = str(args[0])
    if _is_fake_grok_argv0(raw, bin_dir):
        return True
    if _is_reviewed_python_argv(args, raw, grok_home):
        return True
    if _is_reviewed_hook_shell_argv(args, raw, grok_home):
        return True
    return False


def _install_subprocess_guard(monkeypatch, bin_dir: Path, grok_home: Path) -> None:
    real_popen = subprocess.Popen

    def guarded_popen(args, *rest, **kwargs):  # noqa: ANN001
        if not _allowed_subprocess_argv(args, bin_dir, grok_home):
            raise PermissionError(f"{_SUBPROCESS_DENIED}: {args!r}")
        return real_popen(args, *rest, **kwargs)

    def denied_system(cmd: object) -> int:
        raise PermissionError(f"{_SUBPROCESS_DENIED}: {cmd!r}")

    def denied_exec(*_a: object, **_k: object) -> None:
        raise PermissionError(_EXEC_DENIED)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(os, "system", denied_system)
    if hasattr(os, "popen"):
        monkeypatch.setattr(os, "popen", denied_system)
    # Gate children at Popen. Do not replace posix_spawn — CPython 3.14 on
    # Darwin uses it inside Popen for absolute executables (cwd is None).
    for name in (
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    ):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, denied_exec)


def _install_network_denial(monkeypatch) -> None:
    import urllib.request

    def denied_socket(*_a: object, **_k: object) -> socket.socket:
        raise OSError(_NETWORK_DENIED)

    def denied_connect(*_a: object, **_k: object) -> tuple:
        raise OSError(_NETWORK_DENIED)

    def denied_getaddrinfo(*_a: object, **_k: object) -> list:
        raise OSError(_NETWORK_DENIED)

    def denied_urlopen(*_a: object, **_k: object) -> object:
        raise OSError(_NETWORK_DENIED)

    monkeypatch.setattr(socket, "socket", denied_socket)
    monkeypatch.setattr(socket, "create_connection", denied_connect)
    monkeypatch.setattr(socket, "getaddrinfo", denied_getaddrinfo)
    monkeypatch.setattr(urllib.request, "urlopen", denied_urlopen)


def _isolate_stock_host(monkeypatch, tmp_path: Path) -> _StockHostIsolation:
    """Allowlisted env, bounded PATH, fail-closed grok, network/exec guards."""
    home = tmp_path / "home"
    grok_home = tmp_path / "grok"
    bin_dir = tmp_path / "bin"
    xdg = tmp_path / "xdg"
    home.mkdir()
    grok_home.mkdir()
    bin_dir.mkdir()

    _link_python(bin_dir)
    grok = _install_fake_grok(bin_dir)
    env = _allowlisted_env(home=home, grok_home=grok_home, xdg=xdg, bin_dir=bin_dir)
    _replace_environ(monkeypatch, env)
    _evict_medley_modules(monkeypatch)
    monkeypatch.setattr(sys, "path", _runtime_sys_path())
    monkeypatch.delenv("PYTHONPATH", raising=False)
    _install_network_denial(monkeypatch)
    iso = _StockHostIsolation(home, grok_home, bin_dir, xdg, grok)
    _install_subprocess_guard(monkeypatch, iso.bin_dir, iso.grok_home)
    importlib.invalidate_caches()
    return iso


def _injected_site_packages(tmp_path_factory) -> Path:
    """Vendor site-packages for the injected installable ``medley``.

    Factory basename INTENTIONALLY contains ``medley`` so ancestor paths
    always include the substring; isolation is the import blocker, not a
    pathname filter.
    """
    return tmp_path_factory.mktemp("medley-user") / "lib" / "site-packages"


def _inject_fake_medley_package(monkeypatch, tmp_path_factory) -> Path:
    site = _injected_site_packages(tmp_path_factory)
    pkg = site / "medley"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        '"""Test-only installable medley stand-in."""\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "path", [str(site), *_runtime_sys_path()])
    monkeypatch.setenv("PYTHONPATH", str(site))
    importlib.invalidate_caches()
    return site


def _install_import_blocker(monkeypatch) -> _StockHostMedleyImportBlocker:
    blocker = _StockHostMedleyImportBlocker()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    return blocker


def _assert_medley_discoverable(site: Path) -> None:
    spec = importlib.util.find_spec("medley")
    assert spec is not None
    assert spec.origin is not None
    origin = Path(spec.origin).resolve()
    assert site.resolve() in origin.parents


def _assert_blocker_raises() -> None:
    with pytest.raises(ModuleNotFoundError, match=_BLOCKER_MSG):
        importlib.util.find_spec("medley")
    with pytest.raises(ModuleNotFoundError, match=_BLOCKER_MSG):
        importlib.import_module("medley")
    with pytest.raises(ModuleNotFoundError, match=_BLOCKER_MSG):
        importlib.util.find_spec("medley.native")
    with pytest.raises(ModuleNotFoundError, match=_BLOCKER_MSG):
        importlib.import_module("medley.native")


def _install_fake_grok(bin_dir: Path) -> Path:
    """Tiny local grok: version / --version only. Unexpected argv exits 2."""
    path = bin_dir / "grok"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "allowed = {('--version',), ('version',), ('version', '--json')}\n"
        "if tuple(args) not in allowed:\n"
        "    print('stock-host fake grok: unexpected argv', args, file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
        "if '--json' in args:\n"
        "    print('{\"currentVersion\":\"0.2.121\"}')\n"
        "else:\n"
        "    print('0.2.121')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _assert_hermetic_env(iso: _StockHostIsolation) -> None:
    assert os.environ.get("HOME") == str(iso.home)
    assert os.environ.get("GROK_HOME") == str(iso.grok_home)
    assert os.environ.get("PATH") == str(iso.bin_dir)
    assert os.environ.get("XDG_CONFIG_HOME") == str(iso.xdg / "config")
    assert os.environ.get("XDG_DATA_HOME") == str(iso.xdg / "data")
    assert os.environ.get("XDG_CACHE_HOME") == str(iso.xdg / "cache")
    assert os.environ.get("XDG_STATE_HOME") == str(iso.xdg / "state")
    leaked = [key for key in os.environ if _is_credential_key(key)]
    assert not leaked, f"credential env survived isolation: {leaked}"
    extra = [key for key in os.environ if key not in _ALLOWED_ENV]
    assert not extra, f"non-allowlisted env survived isolation: {extra}"
    found = shutil.which("grok")
    assert found is not None
    assert Path(found).resolve() == iso.grok.resolve()
    assert shutil.which("curl") is None
    assert shutil.which("ssh") is None


def _assert_medley_absent(home: Path) -> None:
    """Env/config/module isolation after blocker — not sys.path directory names."""
    _this_file_does_not_import_medley()
    leaked = [k for k in sys.modules if k == "medley" or k.startswith("medley.")]
    assert not leaked, f"medley leaked into sys.modules: {leaked}"
    assert not (home / ".medley").exists()
    assert not (home / "medley").exists()
    assert not any(k.upper().startswith("MEDLEY") for k in os.environ)


def _fake_host_report():
    from omg_cli.host_probe import host_report_for_doctor, probe_host_from_fixture

    report = probe_host_from_fixture(HOST_FIXTURE)
    return report, host_report_for_doctor(report)


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_capabilities_lock_stock_host_test", GEN_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_injected_medley_is_discoverable_when_ancestor_path_contains_medley(
    monkeypatch, tmp_path, tmp_path_factory
) -> None:
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    _assert_hermetic_env(iso)
    site = _inject_fake_medley_package(monkeypatch, tmp_path_factory)
    assert "medley" in str(site).lower()
    _assert_medley_discoverable(site)
    assert os.environ.get("PYTHONPATH") == str(site)


def test_isolation_and_blocker_work_when_tmpdir_ancestor_contains_medley(
    monkeypatch, tmp_path_factory
) -> None:
    base = tmp_path_factory.mktemp("user-medley")
    iso = _isolate_stock_host(monkeypatch, base)
    assert "medley" in str(iso.home).lower()
    assert "medley" in str(iso.grok_home).lower()
    _assert_hermetic_env(iso)
    site = _inject_fake_medley_package(monkeypatch, tmp_path_factory)
    assert "medley" in str(site).lower()
    _assert_medley_discoverable(site)
    _evict_medley_modules(monkeypatch)
    _install_import_blocker(monkeypatch)
    _assert_blocker_raises()
    _assert_medley_absent(iso.home)


def test_ordinary_omg_surfaces_work_with_medley_absent(
    monkeypatch, tmp_path, tmp_path_factory, capsys
) -> None:
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    _assert_hermetic_env(iso)
    home = iso.home
    site = _inject_fake_medley_package(monkeypatch, tmp_path_factory)
    _assert_medley_discoverable(site)
    _evict_medley_modules(monkeypatch)
    _install_import_blocker(monkeypatch)
    _assert_blocker_raises()
    _assert_medley_absent(home)

    # 1. Package projection / setup
    identity = compute_package_identity(ROOT)
    assert identity.get("digest")
    assert identity.get("version")
    inventory_paths = {row["path"] for row in identity["inventory"]}
    assert "bin/omg" in inventory_paths

    project = tmp_path / "project"
    project.mkdir()
    assert run_setup(project, install_rules=True, install_hook=True) == 0
    assert (project / ".omg").is_dir()
    capsys.readouterr()

    # 2. Current doctor (fixture host probe; no live grok inspect / network)
    monkeypatch.setattr(doctor, "_canonical_host_probe", _fake_host_report)
    rc = doctor.run_doctor(strict=False, project_root=project, json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["command"] == "doctor"
    host = payload.get("host") or {}
    assert host.get("binary") == "grok" or "binary" in host
    blob = out.lower()
    for banned in _REQUIRE_MEDLEY_CLAIMS:
        assert banned not in blob, banned

    hard = {row["name"]: row for row in payload.get("checks") or []}
    for name in (
        "plugin.json",
        "skills omg-*",
        "agents",
        "deny module",
        "hooks scripts",
        "PreToolUse hook",
        "global PreToolUse soft-gate",
    ):
        assert name in hard, name
        assert hard[name]["ok"] is True, hard[name]

    assert doctor.check_plugin_json()[1] is True
    assert doctor.check_agents_present()[1] is True
    assert doctor.check_skills_omg_prefix()[1] is True
    assert doctor.check_deny_importable()[1] is True

    # 3. Ordinary agent / profile discovery (taxonomy only — no routing)
    gen = _load_generator()
    surface = gen.discover_session_surface(ROOT)
    agent_names = {item["name"] for item in surface["agents"]}
    skill_names = {item["name"] for item in surface["skills"]}
    assert "omg-executor" in agent_names or "omg-verifier" in agent_names
    assert "omg-using" in skill_names or "omg-ralph" in skill_names
    assert "executor" in CANONICAL_ROLES
    assert "verifier" in CANONICAL_ROLES

    # 4. Ordinary workflow parser / inventory (no network)
    compiled = compile_workflow(WORKFLOW_FIXTURE)
    assert compiled.get("stages")
    assert compiled.get("name") or compiled.get("contract") or compiled.get("definition")

    _assert_medley_absent(home)
    _assert_blocker_raises()
    assert str(site) in sys.path
    _assert_hermetic_env(iso)


def test_ambient_site_packages_and_pythonpath_are_not_inherited(
    monkeypatch, tmp_path, tmp_path_factory
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/tmp/ambient-extra-site:/usr/local/lib/python")
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    _assert_hermetic_env(iso)
    assert "PYTHONPATH" not in os.environ
    inherited_sites = [
        part
        for part in sys.path
        if os.path.basename(os.path.realpath(part).rstrip(os.sep))
        in {"site-packages", "dist-packages"}
    ]
    assert inherited_sites == [], inherited_sites
    site = _inject_fake_medley_package(monkeypatch, tmp_path_factory)
    sites = [
        Path(os.path.realpath(part))
        for part in sys.path
        if os.path.basename(os.path.realpath(part).rstrip(os.sep))
        in {"site-packages", "dist-packages"}
    ]
    assert sites == [site.resolve()]
    assert os.environ.get("PYTHONPATH") == str(site)
    _assert_medley_discoverable(site)


def test_credential_env_is_scrubbed_by_isolation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XAI_API_KEY", "secret-should-not-survive")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-plant")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-plant")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-test-plant")
    monkeypatch.setenv("MEDLEY_API_TOKEN", "medley-test-plant")
    monkeypatch.setenv("MEDLEY_HOME", str(tmp_path / "ambient-medley"))
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    _assert_hermetic_env(iso)
    for key in (
        "XAI_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "MEDLEY_API_TOKEN",
        "MEDLEY_HOME",
    ):
        assert key not in os.environ, key
    assert os.environ.get("XDG_CONFIG_HOME") == str(iso.xdg / "config")
    assert not (iso.xdg / "config" / "medley").exists()


def test_unexpected_grok_argv_fails_closed(monkeypatch, tmp_path) -> None:
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    version = subprocess.run(
        [str(iso.grok), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert version.returncode == 0
    assert "0.2.121" in (version.stdout or "")
    unexpected = subprocess.run(
        [str(iso.grok), "inspect", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unexpected.returncode != 0
    assert _FAKE_GROK_UNEXPECTED in (unexpected.stderr or "")
    plugin = subprocess.run(
        ["grok", "plugin", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert plugin.returncode != 0


def test_unexpected_network_is_denied(monkeypatch, tmp_path) -> None:
    import urllib.request

    _isolate_stock_host(monkeypatch, tmp_path)
    with pytest.raises(OSError, match="network denied"):
        socket.create_connection(("example.com", 443), timeout=0.1)
    with pytest.raises(OSError, match="network denied"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(OSError, match="network denied"):
        socket.socket()
    with pytest.raises(OSError, match="network denied"):
        urllib.request.urlopen("https://example.com", timeout=0.1)


def test_unexpected_subprocess_and_exec_are_denied(monkeypatch, tmp_path) -> None:
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    with pytest.raises(PermissionError, match="subprocess denied"):
        subprocess.run(["curl", "https://example.com"], check=False)
    with pytest.raises(PermissionError, match="subprocess denied"):
        subprocess.run(["/usr/bin/grok", "--version"], check=False)
    with pytest.raises(PermissionError, match="subprocess denied"):
        subprocess.run(["python3", "-c", "import socket; socket.create_connection(('example.com', 443))"])
    with pytest.raises(PermissionError, match="subprocess denied"):
        subprocess.run(["/bin/sh", "-c", "curl https://example.com"])
    with pytest.raises(PermissionError, match="subprocess denied"):
        subprocess.run([sys.executable, "-c", "print(1)"])
    with pytest.raises(PermissionError, match="subprocess denied"):
        subprocess.Popen(["ssh", "example.com"])
    with pytest.raises(PermissionError, match="subprocess denied"):
        os.system("curl https://example.com")
    with pytest.raises(PermissionError, match="process-exec denied"):
        os.execv("/usr/bin/curl", ["curl", "https://example.com"])
    allowed = subprocess.run(
        [str(iso.grok), "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0


def test_reviewed_hook_shell_argv_accepts_exact_launcher_tuple(
    tmp_path, monkeypatch
) -> None:
    grok_home = tmp_path / "grok"
    grok_home.mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    expected = launcher_command(grok_home / "hooks" / STANDALONE_BASENAME)
    args = ["/bin/sh", "-c", expected]
    assert _is_reviewed_hook_shell_argv(args, "/bin/sh", grok_home) is True
    assert _allowed_subprocess_argv(args, tmp_path / "bin", grok_home) is True


def test_reviewed_hook_shell_argv_rejects_injections_and_lookalikes(
    tmp_path, monkeypatch
) -> None:
    grok_home = tmp_path / "grok"
    grok_home.mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    installed = grok_home / "hooks" / STANDALONE_BASENAME
    expected = launcher_command(installed)
    assert expected.endswith(" || true")
    quoted_path = expected[len("python3 -I -S ") : -len(" || true")]
    cases: list[tuple[str, list[str]]] = [
        ("prefix true", ["/bin/sh", "-c", "true; " + expected]),
        ("prefix echo", ["/bin/sh", "-c", "echo hi; " + expected]),
        ("suffix", ["/bin/sh", "-c", expected + "; echo pwned"]),
        ("semicolon appended", ["/bin/sh", "-c", expected + ";"]),
        ("semicolon inside", ["/bin/sh", "-c", expected.replace(" || true", "; || true")]),
        ("newline", ["/bin/sh", "-c", expected + "\necho pwned"]),
        ("cmd subst suffix", ["/bin/sh", "-c", expected + " $(echo pwned)"]),
        ("cmd subst path", ["/bin/sh", "-c", expected.replace(quoted_path, "$(echo pwned)")]),
        ("backticks", ["/bin/sh", "-c", expected + " `echo pwned`"]),
        ("altered path", ["/bin/sh", "-c", launcher_command(grok_home / "hooks" / "other.py")]),
        ("missing || true", ["/bin/sh", "-c", expected[: -len(" || true")]]),
        ("extra || true", ["/bin/sh", "-c", expected + " || true"]),
        ("usr bin sh", ["/usr/bin/sh", "-c", expected]),
        ("bash", ["/bin/bash", "-c", expected]),
    ]
    for label, argv in cases:
        assert _is_reviewed_hook_shell_argv(argv, str(argv[0]), grok_home) is False, label


def test_reviewed_python_argv_accepts_real_stage_file(tmp_path, monkeypatch) -> None:
    grok_home = tmp_path / "grok"
    grok_home.mkdir()
    (grok_home / "hooks").mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    staged = _stage_file(
        grok_home / "hooks" / STANDALONE_BASENAME,
        committed_standalone().read_text(encoding="utf-8"),
        mode=0o644,
    )
    argv = ["python3", "-I", "-S", str(staged)]
    assert _is_reviewed_python_argv(argv, "python3", grok_home) is True
    assert _allowed_subprocess_argv(argv, bin_dir, grok_home) is True


def test_reviewed_python_argv_rejects_unrelated_and_lookalikes(
    tmp_path, monkeypatch
) -> None:
    grok_home = tmp_path / "grok"
    grok_home.mkdir()
    hooks = grok_home / "hooks"
    hooks.mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    committed = committed_standalone().read_bytes()
    staged = _stage_file(
        grok_home / "hooks" / STANDALONE_BASENAME,
        committed_standalone().read_text(encoding="utf-8"),
        mode=0o644,
    )
    other = tmp_path / "other.py"
    other.write_bytes(committed)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / ".omg_pretool_deny_standalone.py.stage.abc123xy.tmp"
    outside.write_bytes(committed)
    link = hooks / ".omg_pretool_deny_standalone.py.stage.symlink1.tmp"
    link.symlink_to(committed_standalone().resolve())
    wrong = hooks / ".omg_pretool_deny_standalone.py.stage.wrongbyte.tmp"
    wrong.write_text("not the committed standalone\n", encoding="utf-8")
    final = hooks / STANDALONE_BASENAME
    final.write_bytes(committed)
    cases: list[tuple[str, list[str]]] = [
        ("unrelated", ["python3", "-I", "-S", str(other)]),
        ("devnull", ["python3", "-I", "-S", "/dev/null"]),
        ("outside same name", ["python3", "-I", "-S", str(outside)]),
        ("symlink", ["python3", "-I", "-S", str(link)]),
        ("wrong bytes", ["python3", "-I", "-S", str(wrong)]),
        ("python argv0", ["python", "-I", "-S", str(staged)]),
        ("sys.executable", [sys.executable, "-I", "-S", str(staged)]),
        ("/usr/bin/python3", ["/usr/bin/python3", "-I", "-S", str(staged)]),
        ("extra flags", ["python3", "-I", "-S", "-B", str(staged)]),
        ("extra args", ["python3", "-I", "-S", str(staged), "extra"]),
        ("final hook", ["python3", "-I", "-S", str(final)]),
    ]
    for label, argv in cases:
        assert _is_reviewed_python_argv(argv, str(argv[0]), grok_home) is False, label
