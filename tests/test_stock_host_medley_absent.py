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
installs the import blocker before any omg_cli import.

The smoke process is an explicit allowlisted environment: fake HOME /
GROK_HOME / XDG dirs, scrubbed credentials, bounded PATH, fail-closed
fake grok, network denial, and subprocess/exec guards. Ambient
site-packages and PYTHONPATH are not inherited.
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
from pathlib import Path

import pytest

from tests.stock_host_medley_absent_support import (
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
)

BOOTSTRAP = Path(__file__).resolve().parent / "stock_host_medley_absent_smoke_bootstrap.py"


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
    assert "stock_host_medley_absent_smoke_bootstrap" not in sys.modules


def test_ordinary_omg_surfaces_work_with_medley_absent(tmp_path) -> None:
    here = Path(__file__)
    assert _module_level_omg_cli_imports(here) == []
    assert BOOTSTRAP.is_file()
    _assert_blocker_before_first_omg_cli_import(BOOTSTRAP)
    assert "stock_host_medley_absent_smoke_bootstrap" not in sys.modules

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


def test_reviewed_hook_shell_argv_accepts_exact_launcher_tuple(
    tmp_path, monkeypatch
) -> None:
    from omg_cli.hook_install import STANDALONE_BASENAME, launcher_command

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
    from omg_cli.hook_install import STANDALONE_BASENAME, launcher_command

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
