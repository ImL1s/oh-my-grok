"""Hermetic evidence for docs/architecture/agent-model-routing.md Decision.

Absence of Medley must not disable ordinary OMG operation. This file is
stock-host smoke (setup / package projection, current ``omg doctor``,
ordinary agent/profile discovery, ordinary workflow parser/inventory). It
is not a routing implementation and does not exercise #131 / #134 / #138.

Absence is an explicit import blocker, never inferred from directory names
on ``sys.path`` / ``PYTHONPATH``. An injected installable ``medley`` stays
discoverable until that blocker is installed. Ancestor pathnames may
contain the substring ``medley``; the blocker never inspects them.

The four ordinary OMG surfaces run in an isolated subprocess that
installs the import blocker and network/subprocess guards before any
omg_cli import. An import-time probe records whether those guards deny
socket/urlopen and Popen at import.

The smoke process is an explicit allowlisted environment: fake HOME /
GROK_HOME / XDG dirs, scrubbed credentials, bounded PATH, fail-closed
fake grok, network denial, and subprocess/exec guards. Ambient
site-packages and PYTHONPATH are not inherited. posix_spawn stays the
builtin (not a universal sandbox).
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.stock_host_medley_absent_support import (
    EXPECTED_DOCTOR_HOST_IDENTITY,
    ISOLATION_GROK_IDENTITY,
    ISOLATION_PYTHON3_IDENTITY,
    REQUIRED_DOCTOR_CHECKS,
    ROOT,
    SMOKE_IMPORTED,
    SMOKE_SURFACES,
    _ALLOWED_ENV,
    _BLOCKER_MSG,
    _CREDENTIAL_MARKERS,
    _FAKE_GROK_UNEXPECTED,
    _StockHostIsolation,
    _StockHostMedleyImportBlocker,
    _allowlisted_env,
    _allowed_subprocess_argv,
    _install_fake_grok,
    _install_network_denial,
    _install_subprocess_guard,
    _is_reviewed_hook_shell_argv,
    _is_reviewed_python_argv,
    _link_python,
    _runtime_sys_path,
    assert_blocker_raises,
    doctor_host_identity_matches,
)

BOOTSTRAP = Path(__file__).resolve().parent / "stock_host_medley_absent_smoke_bootstrap.py"
IMPORT_PROBE = Path(__file__).resolve().parent / "stock_host_medley_absent_import_probe.py"
IMPORT_PROBE_MODULE = "tests.stock_host_medley_absent_import_probe"


def _this_file_does_not_import_medley() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith(("import ", "from ")):
            assert not stripped.startswith("import medley")
            assert not stripped.startswith("from medley")


def _module_level_omg_cli_imports(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    hits: list[str] = []
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "omg_cli" or alias.name.startswith("omg_cli."):
                    hits.append(f"{node.lineno}:import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "omg_cli" or mod.startswith("omg_cli."):
                hits.append(f"{node.lineno}:from {mod}")
    for lineno, line in enumerate(src.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if code.startswith("import omg_cli") or code.startswith("from omg_cli"):
            hits.append(f"{lineno}:{code.strip()}")
    return hits


def _assert_blocker_before_first_omg_cli_import(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    blocker_line: int | None = None
    omg_line: int | None = None
    for lineno, line in enumerate(src.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if blocker_line is None and (
            "install_blocker(" in code
            or ("meta_path" in code and "insert" in code)
        ):
            blocker_line = lineno
        stripped = code.lstrip()
        if omg_line is None and (
            stripped.startswith("import omg_cli") or stripped.startswith("from omg_cli")
        ):
            omg_line = lineno
    assert blocker_line is not None, f"no install_blocker/meta_path insert in {path}"
    assert omg_line is not None, f"no omg_cli import in {path}"
    assert blocker_line < omg_line, (
        f"omg_cli import at line {omg_line} before blocker at {blocker_line} in {path}"
    )

    tree = ast.parse(src)
    blocker_pos: tuple[int, int] | None = None
    omg_pos: tuple[int, int] | None = None
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", 0)
        if lineno is None:
            continue
        pos = (lineno, col)
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
                if name == "insert":
                    value = func.value
                    if isinstance(value, ast.Attribute) and value.attr == "meta_path":
                        if blocker_pos is None or pos < blocker_pos:
                            blocker_pos = pos
            if name == "install_blocker":
                if blocker_pos is None or pos < blocker_pos:
                    blocker_pos = pos
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "omg_cli" or alias.name.startswith("omg_cli."):
                    if omg_pos is None or pos < omg_pos:
                        omg_pos = pos
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "omg_cli" or mod.startswith("omg_cli."):
                if omg_pos is None or pos < omg_pos:
                    omg_pos = pos
    assert blocker_pos is not None
    assert omg_pos is not None
    assert blocker_pos < omg_pos, (
        f"AST omg_cli import {omg_pos} is not after blocker {blocker_pos} in {path}"
    )


def _assert_guards_before_first_omg_cli_import(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    guard_line: int | None = None
    omg_line: int | None = None
    for lineno, line in enumerate(src.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if guard_line is None and (
            "create_isolation(" in code
            or "_install_network_denial(" in code
            or "_install_subprocess_guard(" in code
        ):
            guard_line = lineno
        stripped = code.lstrip()
        if omg_line is None and (
            stripped.startswith("import omg_cli") or stripped.startswith("from omg_cli")
        ):
            omg_line = lineno
    assert guard_line is not None, f"no create_isolation/guard install in {path}"
    assert omg_line is not None, f"no omg_cli import in {path}"
    assert guard_line < omg_line, (
        f"omg_cli import at line {omg_line} before guards at {guard_line} in {path}"
    )

    tree = ast.parse(src)
    guard_pos: tuple[int, int] | None = None
    omg_pos: tuple[int, int] | None = None
    for node in ast.walk(tree):
        node_lineno = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", 0)
        if node_lineno is None:
            continue
        pos = (node_lineno, col)
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in {
                "create_isolation",
                "_install_network_denial",
                "_install_subprocess_guard",
            }:
                if guard_pos is None or pos < guard_pos:
                    guard_pos = pos
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "omg_cli" or alias.name.startswith("omg_cli."):
                    if omg_pos is None or pos < omg_pos:
                        omg_pos = pos
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "omg_cli" or mod.startswith("omg_cli."):
                if omg_pos is None or pos < omg_pos:
                    omg_pos = pos
    assert guard_pos is not None
    assert omg_pos is not None
    assert guard_pos < omg_pos, (
        f"AST omg_cli import {omg_pos} is not after guards {guard_pos} in {path}"
    )


def _is_credential_key(name: str) -> bool:
    upper = name.upper()
    if upper.startswith("MEDLEY"):
        return True
    return any(marker in upper for marker in _CREDENTIAL_MARKERS)


def _replace_environ(monkeypatch, env: dict[str, str]) -> None:
    for key in list(os.environ):
        if key not in env:
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _evict_medley_modules(monkeypatch) -> None:
    for key in [k for k in sys.modules if k == "medley" or k.startswith("medley.")]:
        monkeypatch.delitem(sys.modules, key)


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
    _install_network_denial(assign=monkeypatch.setattr)
    iso = _StockHostIsolation(home, grok_home, bin_dir, xdg, grok)
    _install_subprocess_guard(iso.bin_dir, iso.grok_home, assign=monkeypatch.setattr)
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
    assert_blocker_raises()


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


def test_stock_host_module_has_no_collection_time_omg_cli_import() -> None:
    here = Path(__file__)
    support = here.with_name("stock_host_medley_absent_support.py")
    assert _module_level_omg_cli_imports(here) == []
    assert _module_level_omg_cli_imports(support) == []
    assert _module_level_omg_cli_imports(BOOTSTRAP) == []
    assert IMPORT_PROBE.is_file()
    assert _module_level_omg_cli_imports(IMPORT_PROBE) == []
    assert "stock_host_medley_absent_smoke_bootstrap" not in sys.modules
    assert IMPORT_PROBE_MODULE not in sys.modules


def test_doctor_host_identity_requires_exact_grok_binary() -> None:
    good = dict(EXPECTED_DOCTOR_HOST_IDENTITY)
    assert doctor_host_identity_matches(good) is True

    for binary in ("claude", "cursor-agent", "not-grok"):
        bad = dict(EXPECTED_DOCTOR_HOST_IDENTITY)
        bad["binary"] = binary
        assert doctor_host_identity_matches(bad) is False

    missing_binary = {
        key: value
        for key, value in EXPECTED_DOCTOR_HOST_IDENTITY.items()
        if key != "binary"
    }
    assert doctor_host_identity_matches(missing_binary) is False

    # Old hole: any dict with a binary key (even non-grok) was accepted.
    assert doctor_host_identity_matches({"binary": "cursor-agent"}) is False

    for key, wrong in (
        ("version", "0.0.0"),
        ("compatibility", "incompatible"),
        ("binary_found", False),
        ("schema", "wrong-schema"),
    ):
        bad = dict(EXPECTED_DOCTOR_HOST_IDENTITY)
        bad[key] = wrong
        assert doctor_host_identity_matches(bad) is False

    assert doctor_host_identity_matches(None) is False
    assert doctor_host_identity_matches([]) is False


def test_ordinary_omg_surfaces_work_with_medley_absent(tmp_path) -> None:
    here = Path(__file__)
    assert _module_level_omg_cli_imports(here) == []
    assert _module_level_omg_cli_imports(BOOTSTRAP) == []
    assert BOOTSTRAP.is_file()
    _assert_blocker_before_first_omg_cli_import(BOOTSTRAP)
    _assert_guards_before_first_omg_cli_import(BOOTSTRAP)
    assert "stock_host_medley_absent_smoke_bootstrap" not in sys.modules
    assert IMPORT_PROBE_MODULE not in sys.modules

    result_path = tmp_path / "result.json"
    work = tmp_path / "work"
    proc = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--root",
            str(ROOT),
            "--work",
            str(work),
            "--result",
            str(result_path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, (
        f"bootstrap rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert result_path.is_file(), proc.stderr
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["blocker_installed_before_omg_cli"] is True
    assert payload["imported"] == list(SMOKE_IMPORTED)
    assert payload["surfaces"] == list(SMOKE_SURFACES)
    assert payload["doctor_checks_ok"] == list(REQUIRED_DOCTOR_CHECKS)
    assert payload["setup_omg_dir"] is True
    assert payload["blocker_raises"] is True
    assert payload["guards_installed_before_omg_cli"] is True
    assert payload["import_probe_network"] is True
    assert payload["import_probe_subprocess"] is True
    assert payload["captured_system_popen_guarded"] is True
    assert payload["posix_spawn_unpatched"] is True
    assert payload["imported_posix_spawn_calls"] == []
    if payload["integrate_imported"]:
        assert payload["captured_real_popen_guarded"] is True
    else:
        assert payload["captured_real_popen_guarded"] is None
    assert IMPORT_PROBE_MODULE not in sys.modules
    assert "stock_host_medley_absent_smoke_bootstrap" not in sys.modules


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


def test_guarded_subprocess_rejects_executable_and_launch_overrides(
    monkeypatch, tmp_path
) -> None:
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    denied = "subprocess denied"

    with pytest.raises(PermissionError, match=denied):
        subprocess.run(["grok", "-c", "printf GUARD_BYPASS"], executable="/bin/sh")
    with pytest.raises(PermissionError, match=denied):
        subprocess.Popen(["grok", "-c", "printf GUARD_BYPASS"], -1, "/bin/sh")

    grok_version = ["grok", "version"]
    # Isolated: positional executable on an otherwise-allowed argv.
    with pytest.raises(PermissionError, match=denied):
        subprocess.Popen(grok_version, -1, "/bin/sh")
    # Isolated: duplicate positional + keyword executable (fail-closed merge).
    with pytest.raises(PermissionError, match=denied):
        subprocess.Popen(  # type: ignore[call-overload]
            grok_version, -1, "/bin/sh", executable="/bin/sh"
        )
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, executable="/bin/sh")
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, executable="")
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, executable="grok")
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, shell=True)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, preexec_fn=lambda: None)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, pass_fds=(1,))
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, start_new_session=True)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, process_group=0)

    unreviewed = tmp_path / "unreviewed-cwd"
    unreviewed.mkdir()
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, cwd=str(unreviewed))
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(
            grok_version,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GUARD": "1"},
        )
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_version, env={"PATH": "/tmp/not-the-isolated-path"})

    reviewed = subprocess.run(
        [str(iso.grok), "version"],
        cwd=tempfile.gettempdir(),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert reviewed.returncode == 0
    assert "0.2.121" in (reviewed.stdout or "")


def test_reviewed_hook_shell_argv_accepts_exact_launcher_tuple(
    tmp_path, monkeypatch
) -> None:
    from omg_cli.hook_install import STANDALONE_BASENAME, committed_standalone, launcher_command

    grok_home = tmp_path / "grok"
    grok_home.mkdir()
    hooks = grok_home / "hooks"
    hooks.mkdir()
    installed = hooks / STANDALONE_BASENAME
    installed.write_bytes(committed_standalone().read_bytes())
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    expected = launcher_command(installed)
    args = ["/bin/sh", "-c", expected]
    assert _is_reviewed_hook_shell_argv(args, "/bin/sh", grok_home) is True
    assert _allowed_subprocess_argv(args, tmp_path / "bin", grok_home) is True


def test_reviewed_hook_shell_argv_rejects_injections_and_lookalikes(
    tmp_path, monkeypatch
) -> None:
    from omg_cli.hook_install import STANDALONE_BASENAME, committed_standalone, launcher_command

    grok_home = tmp_path / "grok"
    grok_home.mkdir()
    hooks = grok_home / "hooks"
    hooks.mkdir()
    installed = hooks / STANDALONE_BASENAME
    installed.write_bytes(committed_standalone().read_bytes())
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    expected = launcher_command(installed)
    valid = ["/bin/sh", "-c", expected]
    bin_dir = tmp_path / "bin"

    # Later denials are exact-argv isolation, not a missing installed hook.
    assert _is_reviewed_hook_shell_argv(valid, "/bin/sh", grok_home) is True
    assert _allowed_subprocess_argv(valid, bin_dir, grok_home) is True

    assert expected.endswith(" || true")
    quoted_path = expected[len("python3 -I -S ") : -len(" || true")]

    wrong_path = ["/bin/sh", "-c", launcher_command(grok_home / "hooks" / "other.py")]
    assert _is_reviewed_hook_shell_argv(wrong_path, "/bin/sh", grok_home) is False, "wrong path"
    assert _is_reviewed_hook_shell_argv(valid, "/bin/sh", grok_home) is True, "valid after wrong path"

    usr_bin_sh = ["/usr/bin/sh", "-c", expected]
    assert _is_reviewed_hook_shell_argv(usr_bin_sh, "/usr/bin/sh", grok_home) is False, "usr bin sh"
    bash = ["/bin/bash", "-c", expected]
    assert _is_reviewed_hook_shell_argv(bash, "/bin/bash", grok_home) is False, "bash"

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
        ("missing || true", ["/bin/sh", "-c", expected[: -len(" || true")]]),
        ("extra || true", ["/bin/sh", "-c", expected + " || true"]),
    ]
    for label, argv in cases:
        assert _is_reviewed_hook_shell_argv(argv, str(argv[0]), grok_home) is False, label
        assert _is_reviewed_hook_shell_argv(valid, "/bin/sh", grok_home) is True, f"valid after {label}"


def test_reviewed_hook_shell_argv_rejects_bad_installed_hook(
    tmp_path, monkeypatch
) -> None:
    from omg_cli.hook_install import STANDALONE_BASENAME, committed_standalone, launcher_command

    grok_home = tmp_path / "grok"
    grok_home.mkdir()
    hooks = grok_home / "hooks"
    hooks.mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    installed = hooks / STANDALONE_BASENAME
    args = ["/bin/sh", "-c", launcher_command(installed)]
    bin_dir = tmp_path / "bin"

    def reject(label: str) -> None:
        assert _is_reviewed_hook_shell_argv(args, "/bin/sh", grok_home) is False, label
        assert _allowed_subprocess_argv(args, bin_dir, grok_home) is False, label

    reject("missing")

    installed.symlink_to(committed_standalone().resolve())
    reject("symlink")
    installed.unlink()

    installed.mkdir()
    reject("directory")
    installed.rmdir()

    try:
        os.mkfifo(installed)
    except (AttributeError, OSError, NotImplementedError):
        pass
    else:
        try:
            reject("fifo")
        finally:
            installed.unlink()

    installed.write_text("not the committed standalone\n", encoding="utf-8")
    reject("stale bytes")


def test_reviewed_python_argv_accepts_real_stage_file(tmp_path, monkeypatch) -> None:
    from omg_cli.hook_install import STANDALONE_BASENAME, _stage_file, committed_standalone

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
    from omg_cli.hook_install import STANDALONE_BASENAME, _stage_file, committed_standalone

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


def _legacy_string_only_grok_argv0(raw: str, bin_dir: Path) -> bool:
    """Prior string-only argv0 predicate. Not the live allow."""
    if raw == "grok":
        return True
    try:
        return Path(raw).resolve() == (bin_dir / "grok").resolve()
    except OSError:
        return False


def test_guarded_subprocess_binds_isolation_exec_inode(monkeypatch, tmp_path) -> None:
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    denied = "subprocess denied"
    wrapper = iso.bin_dir / "python3"
    assert wrapper.is_file()
    assert wrapper.is_symlink() is False

    absolute = subprocess.run(
        [str(iso.grok), "--isolation-identity"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert absolute.returncode == 0
    assert ISOLATION_GROK_IDENTITY in (absolute.stdout or "")

    bare = subprocess.run(
        ["grok", "--isolation-identity"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bare.returncode == 0
    assert ISOLATION_GROK_IDENTITY in (bare.stdout or "")

    python_marker = subprocess.run(
        ["python3", "--isolation-identity"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert python_marker.returncode == 0
    assert ISOLATION_PYTHON3_IDENTITY in (python_marker.stdout or "")

    absolute_python = subprocess.run(
        [str(wrapper), "--isolation-identity"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert absolute_python.returncode == 0
    assert ISOLATION_PYTHON3_IDENTITY in (absolute_python.stdout or "")

    snapshot_env = subprocess.run(
        [str(iso.grok), "--isolation-identity"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert snapshot_env.returncode == 0
    assert ISOLATION_GROK_IDENTITY in (snapshot_env.stdout or "")

    alias_dir = tmp_path / "alias"
    alias_dir.mkdir()
    grok_link = alias_dir / "grok"
    grok_link.symlink_to(iso.grok)
    assert _legacy_string_only_grok_argv0(str(grok_link), iso.bin_dir) is True
    with pytest.raises(PermissionError, match=denied):
        subprocess.run([str(grok_link), "version"], check=False)

    attacker = tmp_path / "attacker"
    attacker.mkdir()
    evil = attacker / "grok"
    evil.write_text("#!/bin/sh\necho PWNED\n", encoding="utf-8")
    evil.chmod(0o755)
    poisoned = f"{attacker}{os.pathsep}{iso.bin_dir}"

    assert _legacy_string_only_grok_argv0("grok", iso.bin_dir) is True
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(["grok", "version"], env={"PATH": "/usr/bin:/bin"}, check=False)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(["grok", "version"], env={"PATH": poisoned}, check=False)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(["grok", "version"], env={}, check=False)

    monkeypatch.delenv("PATH", raising=False)
    assert _legacy_string_only_grok_argv0("grok", iso.bin_dir) is True
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(["grok", "version"], check=False)

    monkeypatch.setenv("PATH", poisoned)
    assert _legacy_string_only_grok_argv0("grok", iso.bin_dir) is True
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(["grok", "version"], check=False)
    with pytest.raises(PermissionError, match=denied):
        subprocess.Popen(["grok", "version"])
    monkeypatch.setenv("PATH", str(iso.bin_dir))

    monkeypatch.chdir(iso.bin_dir)
    assert _legacy_string_only_grok_argv0("./grok", iso.bin_dir) is True
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(
            ["./grok", "version"],
            cwd=tempfile.gettempdir(),
            check=False,
        )

    wrapper.write_text("#!/bin/sh\necho PWNED\n", encoding="utf-8")
    wrapper.chmod(0o755)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run([str(wrapper), "--isolation-identity"], check=False)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(["python3", "--isolation-identity"], check=False)


def test_import_probe_denied_only_after_isolation(monkeypatch, tmp_path) -> None:
    assert IMPORT_PROBE_MODULE not in sys.modules
    _isolate_stock_host(monkeypatch, tmp_path)
    probe = importlib.import_module(IMPORT_PROBE_MODULE)
    try:
        assert probe.NETWORK_DENIED is True
        assert probe.SUBPROCESS_DENIED is True
    finally:
        sys.modules.pop(IMPORT_PROBE_MODULE, None)
    assert IMPORT_PROBE_MODULE not in sys.modules


def test_isolation_leaves_posix_spawn_unpatched_and_popen_works(
    monkeypatch, tmp_path
) -> None:
    real_spawn = getattr(os, "posix_spawn", None)
    real_spawnp = getattr(os, "posix_spawnp", None)
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    assert getattr(os, "posix_spawn", None) is real_spawn
    if real_spawnp is not None:
        assert getattr(os, "posix_spawnp", None) is real_spawnp
    allowed = subprocess.run(
        [str(iso.grok), "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0
    assert "0.2.121" in (allowed.stdout or "")
    with pytest.raises(PermissionError, match="process-exec denied"):
        os.execv("/usr/bin/curl", ["curl", "https://example.com"])
    if hasattr(os, "spawnl"):
        with pytest.raises(PermissionError, match="process-exec denied"):
            os.spawnl(os.P_WAIT, "/bin/sh", "sh", "-c", "true")


def _install_isolation_hook(iso: _StockHostIsolation) -> tuple[Path, str]:
    from omg_cli.hook_install import STANDALONE_BASENAME, committed_standalone, launcher_command

    hooks = iso.grok_home / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    installed = hooks / STANDALONE_BASENAME
    installed.write_bytes(committed_standalone().read_bytes())
    return installed, launcher_command(installed)


def test_live_hook_shell_injections_raise_permission_error(
    monkeypatch, tmp_path
) -> None:
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    installed, expected = _install_isolation_hook(iso)
    valid = ["/bin/sh", "-c", expected]
    allowed = subprocess.run(
        valid,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr

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
        ("missing || true", ["/bin/sh", "-c", expected[: -len(" || true")]]),
        ("extra || true", ["/bin/sh", "-c", expected + " || true"]),
        ("usr bin sh", ["/usr/bin/sh", "-c", expected]),
        ("bash", ["/bin/bash", "-c", expected]),
    ]
    denied = "subprocess denied"
    for label, argv in cases:
        with pytest.raises(PermissionError, match=denied):
            subprocess.run(argv, check=False)
        with pytest.raises(PermissionError, match=denied):
            subprocess.Popen(argv)
        assert installed.is_file(), label


def test_live_hook_and_grok_path_cwd_retarget_denied(monkeypatch, tmp_path) -> None:
    iso = _isolate_stock_host(monkeypatch, tmp_path)
    _installed, expected = _install_isolation_hook(iso)
    hook_argv = ["/bin/sh", "-c", expected]
    grok_argv = ["grok", "version"]
    denied = "subprocess denied"

    with pytest.raises(PermissionError, match=denied):
        subprocess.run(hook_argv, env={"PATH": "/usr/bin:/bin"}, check=False)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_argv, env={"PATH": "/usr/bin:/bin"}, check=False)
    with pytest.raises(PermissionError, match=denied):
        subprocess.Popen(grok_argv, env={"PATH": "/usr/bin:/bin"})

    unreviewed = tmp_path / "unreviewed-cwd"
    unreviewed.mkdir()
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(hook_argv, cwd=str(unreviewed), check=False)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(grok_argv, cwd=str(unreviewed), check=False)
    with pytest.raises(PermissionError, match=denied):
        subprocess.Popen(hook_argv, cwd=str(unreviewed))

    monkeypatch.chdir(iso.bin_dir)
    with pytest.raises(PermissionError, match=denied):
        subprocess.run(
            ["./grok", "version"],
            cwd=tempfile.gettempdir(),
            check=False,
        )
    with pytest.raises(PermissionError, match=denied):
        subprocess.Popen(["./grok", "version"])
