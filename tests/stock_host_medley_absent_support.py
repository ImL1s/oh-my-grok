"""Stdlib-only helpers for stock-host Medley-absent smoke.

No ``omg_cli`` import at module level. Hook-install symbols are imported
only inside the argv reviewers, and only after early rejects.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

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

REQUIRED_DOCTOR_CHECKS = (
    "plugin.json",
    "skills omg-*",
    "agents",
    "deny module",
    "hooks scripts",
    "PreToolUse hook",
    "global PreToolUse soft-gate",
)
SMOKE_SURFACES = (
    "package_identity",
    "setup",
    "doctor",
    "agent_profile_discovery",
    "workflow_parser",
)
SMOKE_IMPORTED = (
    "omg_cli.doctor",
    "omg_cli.setup_cmd",
    "omg_cli.team.roles",
    "omg_cli.workflows.schema",
)

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

_STAGE_BASENAME_RE = re.compile(
    r"^\.omg_pretool_deny_standalone\.py\.stage\.[a-z0-9_]+\.tmp$"
)


class _StockHostIsolation(NamedTuple):
    home: Path
    grok_home: Path
    bin_dir: Path
    xdg: Path
    grok: Path


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


def install_blocker() -> _StockHostMedleyImportBlocker:
    """Insert the Medley import blocker at the front of ``sys.meta_path``."""
    blocker = _StockHostMedleyImportBlocker()
    sys.meta_path.insert(0, blocker)
    return blocker


def evict_medley_modules() -> None:
    for key in [k for k in sys.modules if k == "medley" or k.startswith("medley.")]:
        del sys.modules[key]


def assert_blocker_raises() -> None:
    """Raise AssertionError unless ``medley`` / ``medley.native`` hit the blocker."""
    for name in ("medley", "medley.native"):
        for action in (
            lambda n=name: importlib.util.find_spec(n),
            lambda n=name: importlib.import_module(n),
        ):
            try:
                action()
            except ModuleNotFoundError as exc:
                if _BLOCKER_MSG not in str(exc):
                    raise AssertionError(
                        f"ModuleNotFoundError for {name} lacked blocker message: {exc}"
                    ) from exc
            else:
                raise AssertionError(f"expected blocker to raise for {name}")


def _is_credential_key(name: str) -> bool:
    upper = name.upper()
    if upper.startswith("MEDLEY"):
        return True
    return any(marker in upper for marker in _CREDENTIAL_MARKERS)


def _runtime_sys_path(root: Path | None = None) -> list[str]:
    """Stdlib + this checkout only. Do not filter names for ``medley``.

    Compare realpaths so Homebrew Cellar vs opt prefixes still keep stdlib.
    Drop inherited ``site-packages`` / ``dist-packages``; the injected vendor
    site is prepended separately and is never inferred from path names.
    """
    checkout = Path(root) if root is not None else ROOT
    prefixes: list[str] = []
    for raw in (
        sys.base_prefix,
        sys.base_exec_prefix,
        sys.prefix,
        sys.exec_prefix,
        str(checkout),
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


def _replace_environ(env: dict[str, str]) -> None:
    os.environ.clear()
    os.environ.update(env)


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
        from omg_cli.hook_install import committed_standalone

        actual = hashlib.sha256(candidate.read_bytes()).digest()
        expected = hashlib.sha256(committed_standalone().read_bytes()).digest()
    except (OSError, TypeError, ValueError):
        return False
    return actual == expected


def _is_reviewed_hook_shell_argv(args: Sequence[object], raw: str, grok_home: Path) -> bool:
    """Doctor hook smoke: exact ``/bin/sh -c`` launcher plus matching installed standalone."""
    if raw != "/bin/sh" or len(args) != 3:
        return False
    gh = _usable_grok_home(grok_home)
    if gh is None:
        return False
    try:
        from omg_cli.hook_install import STANDALONE_BASENAME, committed_standalone, launcher_command

        expected_path = gh / "hooks" / STANDALONE_BASENAME
        expected = ("/bin/sh", "-c", launcher_command(expected_path))
        if tuple(str(a) for a in args) != expected:
            return False
        if expected_path.name != STANDALONE_BASENAME:
            return False
        if expected_path.parent.resolve() != (gh / "hooks").resolve():
            return False
        st = os.lstat(expected_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False
        actual = hashlib.sha256(expected_path.read_bytes()).digest()
        wanted = hashlib.sha256(committed_standalone().read_bytes()).digest()
    except (OSError, TypeError, ValueError):
        return False
    return actual == wanted


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


def _reviewed_smoke_cwd(cwd: object) -> bool:
    """Allow inherit, ``tempfile.gettempdir()``, or ``/tmp`` when that dir exists."""
    if cwd is None:
        return True
    try:
        actual = Path(cwd).resolve()  # type: ignore[arg-type]
        allowed = [Path(tempfile.gettempdir()).resolve()]
        tmp = Path("/tmp")
        if tmp.is_dir():
            allowed.append(tmp.resolve())
    except (OSError, TypeError, ValueError):
        return False
    return actual in allowed


def _reviewed_smoke_env(env: object) -> bool:
    """Allow inherit or the exact PATH-only reviewed smoke mapping."""
    if env is None:
        return True
    if not isinstance(env, Mapping):
        return False
    if set(env.keys()) != {"PATH"}:
        return False
    return env["PATH"] == os.environ.get("PATH", "/usr/bin:/bin")


def _safe_popen_kwargs(kwargs: Mapping[str, object]) -> bool:
    """Reject launch overrides that can retarget an argv-allowed binary."""
    if kwargs.get("executable") is not None:
        return False
    if kwargs.get("shell"):
        return False
    if kwargs.get("preexec_fn") is not None:
        return False
    if kwargs.get("pass_fds"):
        return False
    if kwargs.get("start_new_session"):
        return False
    if kwargs.get("process_group") is not None:
        return False
    for key in ("user", "group", "extra_groups", "umask", "startupinfo"):
        if kwargs.get(key) is not None:
            return False
    if kwargs.get("creationflags"):
        return False
    if kwargs.get("close_fds") is False:
        return False
    if not _reviewed_smoke_cwd(kwargs.get("cwd")):
        return False
    if not _reviewed_smoke_env(kwargs.get("env")):
        return False
    return True


def _effective_executable(
    args: Sequence[object] | str | None, kwargs: Mapping[str, object]
) -> object:
    """Binary Popen would exec after ``shell`` / ``executable`` overrides."""
    if kwargs.get("shell"):
        return "/bin/sh"
    executable = kwargs.get("executable")
    if executable is not None:
        return executable
    if args is None or isinstance(args, str) or not args:
        return None
    return args[0]


def _allowed_subprocess_launch(
    args: Sequence[object] | str | None,
    kwargs: Mapping[str, object],
    bin_dir: Path,
    grok_home: Path,
) -> bool:
    """Authorize argv, launch kwargs, and the normalized effective executable."""
    if not _allowed_subprocess_argv(args, bin_dir, grok_home):
        return False
    if not _safe_popen_kwargs(kwargs):
        return False
    # Safe kwargs imply executable is None and shell is false. Deny anyway
    # if an override still produced a different effective executable.
    if kwargs.get("shell") or kwargs.get("executable") is not None:
        return False
    if args is None or isinstance(args, str) or not args:
        return False
    argv0 = str(args[0])
    if str(_effective_executable(args, kwargs)) != argv0:
        return False
    return (
        _is_fake_grok_argv0(argv0, bin_dir)
        or _is_reviewed_python_argv(args, argv0, grok_home)
        or _is_reviewed_hook_shell_argv(args, argv0, grok_home)
    )


def _setattr_live(module: object, name: str, value: object) -> None:
    setattr(module, name, value)


_POPEN_POSITIONAL = (
    "bufsize",
    "executable",
    "stdin",
    "stdout",
    "stderr",
    "preexec_fn",
    "close_fds",
    "shell",
    "cwd",
    "env",
    "universal_newlines",
    "startupinfo",
    "creationflags",
    "restore_signals",
    "start_new_session",
    "pass_fds",
)


def _merge_popen_launch_kwargs(rest: Sequence[object], kwargs: Mapping[str, object]) -> dict[str, object]:
    """Fold positional Popen args into the kwargs the launch guard sees."""
    merged = dict(kwargs)
    for name, value in zip(_POPEN_POSITIONAL, rest, strict=False):
        if name in merged:
            raise PermissionError(f"{_SUBPROCESS_DENIED}: duplicate Popen argument {name}")
        merged[name] = value
    return merged


def _install_subprocess_guard(
    bin_dir: Path,
    grok_home: Path,
    *,
    assign: Callable[..., None] | None = None,
) -> None:
    apply = assign if assign is not None else _setattr_live
    real_popen = subprocess.Popen

    def guarded_popen(args, *rest, **kwargs):  # noqa: ANN001
        launch_kwargs = _merge_popen_launch_kwargs(rest, kwargs)
        if not _allowed_subprocess_launch(args, launch_kwargs, bin_dir, grok_home):
            raise PermissionError(f"{_SUBPROCESS_DENIED}: {args!r}")
        return real_popen(args, *rest, **kwargs)

    def denied_system(cmd: object) -> int:
        raise PermissionError(f"{_SUBPROCESS_DENIED}: {cmd!r}")

    def denied_exec(*_a: object, **_k: object) -> None:
        raise PermissionError(_EXEC_DENIED)

    apply(subprocess, "Popen", guarded_popen)
    apply(os, "system", denied_system)
    if hasattr(os, "popen"):
        apply(os, "popen", denied_system)
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
            apply(os, name, denied_exec)


def _install_network_denial(*, assign: Callable[..., None] | None = None) -> None:
    import urllib.request

    apply = assign if assign is not None else _setattr_live

    def denied_socket(*_a: object, **_k: object) -> socket.socket:
        raise OSError(_NETWORK_DENIED)

    def denied_connect(*_a: object, **_k: object) -> tuple:
        raise OSError(_NETWORK_DENIED)

    def denied_getaddrinfo(*_a: object, **_k: object) -> list:
        raise OSError(_NETWORK_DENIED)

    def denied_urlopen(*_a: object, **_k: object) -> object:
        raise OSError(_NETWORK_DENIED)

    apply(socket, "socket", denied_socket)
    apply(socket, "create_connection", denied_connect)
    apply(socket, "getaddrinfo", denied_getaddrinfo)
    apply(urllib.request, "urlopen", denied_urlopen)


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


def create_isolation(work: Path, *, root: Path | None = None) -> _StockHostIsolation:
    """Allowlisted env, bounded PATH, fail-closed grok, network/exec guards."""
    work.mkdir(parents=True, exist_ok=True)
    home = work / "home"
    grok_home = work / "grok"
    bin_dir = work / "bin"
    xdg = work / "xdg"
    home.mkdir()
    grok_home.mkdir()
    bin_dir.mkdir()

    _link_python(bin_dir)
    grok = _install_fake_grok(bin_dir)
    env = _allowlisted_env(home=home, grok_home=grok_home, xdg=xdg, bin_dir=bin_dir)
    _replace_environ(env)
    evict_medley_modules()
    sys.path[:] = _runtime_sys_path(root)
    os.environ.pop("PYTHONPATH", None)
    _install_network_denial()
    iso = _StockHostIsolation(home, grok_home, bin_dir, xdg, grok)
    _install_subprocess_guard(iso.bin_dir, iso.grok_home)
    importlib.invalidate_caches()
    return iso


def inject_medley_under_work(work: Path, *, root: Path | None = None) -> Path:
    """Vendor site whose ancestor basename contains ``medley`` (F8 in the child)."""
    site = work / "medley-user" / "lib" / "site-packages"
    pkg = site / "medley"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        '"""Test-only installable medley stand-in."""\n',
        encoding="utf-8",
    )
    sys.path[:] = [str(site), *_runtime_sys_path(root)]
    os.environ["PYTHONPATH"] = str(site)
    importlib.invalidate_caches()
    return site
