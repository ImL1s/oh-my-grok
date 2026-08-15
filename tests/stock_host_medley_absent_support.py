"""Stdlib-only helpers for stock-host Medley-absent smoke.

No ``omg_cli`` import at module level. Hook-install symbols are imported
only inside the argv reviewers, and only after early rejects.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.abc
import importlib.util
import os
import re
import shutil
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

# Identity fields still match the 0.2.121 fixture-contract, but smoke now
# proves them via isolation fake grok live collect (version fallback;
# inspect stays unexpected). Extra host keys are ok.
EXPECTED_DOCTOR_HOST_IDENTITY = {
    "binary": "grok",
    "version": "0.2.121",
    "tested_min": "0.2.107",
    "tested_max": "0.2.121",
    "compatibility": "compatible",
    "binary_found": True,
    "schema": "omg-host-capabilities/v1",
}

EXPECTED_LIVE_SESSION_CAPS = {
    "session_resume": True,
    "session_close": True,
    "restore_code_explicit": True,
    "uuid_search": True,
}
EXPECTED_LIVE_CAPABILITY_SOURCES = {
    "session_resume": "version",
    "session_close": "version",
    "restore_code_explicit": "version",
    "uuid_search": "version",
}
LIVE_PROBE_VERSION_OBSERVATION = "version from CLI version --json"


def doctor_host_identity_matches(host: object) -> bool:
    """True only when *host* is grok with the exact 0.2.121 fixture identity."""
    if not isinstance(host, Mapping):
        return False
    if host.get("binary") != "grok":
        return False
    return all(
        host.get(key) == expected
        for key, expected in EXPECTED_DOCTOR_HOST_IDENTITY.items()
    )


def doctor_host_live_session_matches(host: object) -> bool:
    """True when identity plus live version-fallback session caps match."""
    if not doctor_host_identity_matches(host):
        return False
    if not isinstance(host, Mapping):
        return False
    caps = host.get("capabilities")
    if not isinstance(caps, Mapping):
        return False
    if not all(
        caps.get(key) == expected
        for key, expected in EXPECTED_LIVE_SESSION_CAPS.items()
    ):
        return False
    sources = host.get("capability_sources")
    if not isinstance(sources, Mapping):
        return False
    if not all(
        sources.get(key) == expected
        for key, expected in EXPECTED_LIVE_CAPABILITY_SOURCES.items()
    ):
        return False
    raw_obs = host.get("observations")
    if isinstance(raw_obs, (list, tuple)):
        blob = " ".join(str(item) for item in raw_obs)
    elif isinstance(raw_obs, str):
        blob = raw_obs
    else:
        return False
    if LIVE_PROBE_VERSION_OBSERVATION not in blob:
        return False
    gates = host.get("gates")
    if isinstance(gates, Mapping):
        for key in EXPECTED_LIVE_SESSION_CAPS:
            gate = gates.get(key)
            if not isinstance(gate, Mapping) or gate.get("state") != "AVAILABLE":
                return False
    return True


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
ISOLATION_GROK_IDENTITY = "stock-host-isolation-grok-inode"
ISOLATION_PYTHON3_IDENTITY = "stock-host-isolation-python3-inode"

_STAGE_BASENAME_RE = re.compile(
    r"^\.omg_pretool_deny_standalone\.py\.stage\.[a-z0-9_]+\.tmp$"
)


class _StockHostIsolation(NamedTuple):
    home: Path
    grok_home: Path
    bin_dir: Path
    xdg: Path
    grok: Path


class _IsolationExecIdentity(NamedTuple):
    bin_dir: Path
    grok: Path
    python3: Path
    grok_digest: bytes
    python3_digest: bytes
    path_snapshot: str
    # Absolute durable wrapper interpreter (not isolation-owned). Empty if none.
    durable_python3: str


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


_POSIX_SPAWN_NAMES = frozenset({"posix_spawn", "posix_spawnp"})


def imported_omg_posix_spawn_calls() -> list[str]:
    """Call sites of posix_spawn in *already imported* omg_cli modules.

    String literals (deny lists) do not count. posix_spawn stays unpatched so
    Darwin Popen works; this smoke is not a universal sandbox.
    """
    hits: list[str] = []
    for name, mod in list(sys.modules.items()):
        if name != "omg_cli" and not name.startswith("omg_cli."):
            continue
        path = getattr(mod, "__file__", None)
        if not path or not str(path).endswith(".py"):
            continue
        try:
            src = Path(path).read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError, TypeError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = None
            if isinstance(func, ast.Attribute):
                attr = func.attr
            elif isinstance(func, ast.Name):
                attr = func.id
            if attr in _POSIX_SPAWN_NAMES:
                hits.append(f"{name}:{getattr(node, 'lineno', 0)}")
    return hits


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
        python3.write_text(
            f"#!{target}\n"
            "import os\n"
            "import sys\n"
            f"REAL = {str(target)!r}\n"
            "if sys.argv[1:] == ['--isolation-identity']:\n"
            f"    print({ISOLATION_PYTHON3_IDENTITY!r})\n"
            "    raise SystemExit(0)\n"
            "os.execv(REAL, [REAL, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        python3.chmod(0o755)
    if not python.exists():
        python.symlink_to(target)


def _is_fake_grok_argv0(raw: str, bin_dir: Path) -> bool:
    """Shape helper: bare ``grok`` or an absolute path whose realpath is bin_dir/grok."""
    if raw == "grok":
        return True
    if not os.path.isabs(raw):
        return False
    try:
        return os.path.realpath(raw) == os.path.realpath(bin_dir / "grok")
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


def _is_isolation_python_identity_argv(args: Sequence[object], raw: str) -> bool:
    return raw == "python3" and len(args) == 2 and str(args[1]) == "--isolation-identity"


def _durable_wrapper_python3() -> str:
    """Absolute interpreter ``hook_install`` embeds; empty if not a live file.

    Install staging smoke uses this path (not a bare ``python3`` on PATH). Do
    not ``realpath``: Homebrew's stable launcher must stay the Cellar-facing
    name, matching ``python3_executable()``.
    """
    try:
        from omg_cli.hook_install import python3_executable

        selected = python3_executable()
    except Exception:
        return ""
    if not selected or not os.path.isabs(selected):
        return ""
    try:
        if os.path.isfile(selected) and os.access(selected, os.X_OK):
            return os.path.normpath(selected)
    except OSError:
        return ""
    return ""


def _python_argv0_as_bare(raw: str, bin_dir: Path) -> str | None:
    """Map isolation python wrapper argv0 to the bare name, else None."""
    if raw == "python3":
        return "python3"
    if not os.path.isabs(raw):
        return None
    try:
        if os.path.realpath(raw) == os.path.realpath(bin_dir / "python3"):
            return "python3"
    except OSError:
        pass
    durable = _durable_wrapper_python3()
    if durable and os.path.normpath(raw) == durable:
        return "python3"
    return None


def _is_reviewed_python_launch(
    args: Sequence[object], raw: str, bin_dir: Path, grok_home: Path
) -> bool:
    """Bare, isolation-owned, or durable wrapper python3 plus reviewed argv."""
    if _python_argv0_as_bare(raw, bin_dir) is None:
        return False
    rest = tuple(args[1:])
    rewritten = ("python3", *rest)
    return _is_reviewed_python_argv(rewritten, "python3", grok_home) or (
        _is_isolation_python_identity_argv(rewritten, "python3")
    )


def _is_reviewed_hook_shell_argv(args: Sequence[object], raw: str, grok_home: Path) -> bool:
    """Doctor hook smoke: grok 1.0.4 execvp()s the wrapper path (no shell).

    Authorizes only ``(launcher_command(final_path),)`` when both the committed
    standalone and the execvp wrapper at ``$GROK_HOME/hooks/`` are regular
    non-symlink files (``os.lstat``) and their bytes match committed /
    ``render_wrapper``.
    """
    gh = _usable_grok_home(grok_home)
    if gh is None:
        return False
    try:
        from omg_cli.hook_install import (
            STANDALONE_BASENAME,
            WRAPPER_BASENAME,
            committed_standalone,
            launcher_command,
            render_wrapper,
        )

        expected_path = gh / "hooks" / STANDALONE_BASENAME
        wrapper_path = gh / "hooks" / WRAPPER_BASENAME
        expected_cmd = launcher_command(expected_path)
        if (
            len(args) != 1
            or str(args[0]) != expected_cmd
            or raw != expected_cmd
            or str(wrapper_path) != expected_cmd
        ):
            return False
        st = os.lstat(expected_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False
        wst = os.lstat(wrapper_path)
        if stat.S_ISLNK(wst.st_mode) or not stat.S_ISREG(wst.st_mode):
            return False
        actual = hashlib.sha256(expected_path.read_bytes()).digest()
        wanted = hashlib.sha256(committed_standalone().read_bytes()).digest()
        wrapper_actual = hashlib.sha256(wrapper_path.read_bytes()).digest()
        wrapper_wanted = hashlib.sha256(
            render_wrapper(expected_path).encode("utf-8")
        ).digest()
    except (OSError, TypeError, ValueError):
        return False
    return actual == wanted and wrapper_actual == wrapper_wanted


def _allowed_subprocess_argv(
    args: Sequence[object] | str | None, bin_dir: Path, grok_home: Path
) -> bool:
    if args is None or isinstance(args, str) or not args:
        return False
    raw = str(args[0])
    if _is_fake_grok_argv0(raw, bin_dir):
        return True
    if _is_reviewed_python_launch(args, raw, bin_dir, grok_home):
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


def _reviewed_smoke_env(env: object, path_snapshot: str) -> bool:
    """Allow inherit or the PATH-only mapping when PATH equals the frozen snapshot."""
    if env is None:
        return os.environ.get("PATH") == path_snapshot
    if not isinstance(env, Mapping):
        return False
    if set(env.keys()) != {"PATH"}:
        return False
    return env["PATH"] == path_snapshot


def _safe_popen_kwargs(kwargs: Mapping[str, object], path_snapshot: str) -> bool:
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
    if not _reviewed_smoke_env(kwargs.get("env"), path_snapshot):
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


def _regular_non_symlink_digest(path: Path) -> bytes:
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return b""
        return hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        return b""


def _freeze_exec_identity(bin_dir: Path) -> _IsolationExecIdentity:
    grok = bin_dir / "grok"
    python3 = bin_dir / "python3"
    return _IsolationExecIdentity(
        bin_dir=bin_dir,
        grok=grok,
        python3=python3,
        grok_digest=_regular_non_symlink_digest(grok),
        python3_digest=_regular_non_symlink_digest(python3),
        path_snapshot=str(bin_dir),
        durable_python3=_durable_wrapper_python3(),
    )


def _effective_child_path(
    kwargs: Mapping[str, object], identity: _IsolationExecIdentity
) -> str | None:
    env = kwargs.get("env")
    if env is None:
        # Inherit is allowed only when live PATH still equals the frozen snapshot.
        if os.environ.get("PATH") != identity.path_snapshot:
            return None
        return identity.path_snapshot
    if not isinstance(env, Mapping):
        return None
    if "PATH" not in env:
        return None
    if env["PATH"] != identity.path_snapshot:
        return None
    return identity.path_snapshot


def _argv0_has_separator(raw: str) -> bool:
    if os.sep in raw or "/" in raw:
        return True
    alt = os.altsep
    return alt is not None and alt in raw


def _matches_isolation_regular_file(raw: str, expected: Path, digest: bytes) -> bool:
    if not digest:
        return False
    try:
        if os.path.realpath(raw) != os.path.realpath(expected):
            return False
        st = os.lstat(raw)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False
        return hashlib.sha256(Path(raw).read_bytes()).digest() == digest
    except (OSError, TypeError, ValueError):
        return False


def _bare_resolves_to_isolation(
    name: str, expected: Path, digest: bytes, child_path: str
) -> bool:
    found = shutil.which(name, path=child_path)
    if not found:
        return False
    if os.path.normpath(found) != os.path.normpath(str(expected)):
        return False
    return _matches_isolation_regular_file(found, expected, digest)


def _child_will_exec_isolation_inode(
    args: Sequence[object],
    kwargs: Mapping[str, object],
    identity: _IsolationExecIdentity,
) -> bool:
    argv0 = str(args[0])
    # /bin/sh is not isolation-owned; hook argv stays a shape+digest review.
    if argv0 == "/bin/sh":
        return True
    # grok 1.0.4 execvp()s the wrapper path; already shape+digest reviewed.
    from omg_cli.hook_install import WRAPPER_BASENAME

    if os.path.isabs(argv0) and Path(argv0).name == WRAPPER_BASENAME:
        return True
    if _argv0_has_separator(argv0) and not os.path.isabs(argv0):
        return False
    child_path = _effective_child_path(kwargs, identity)
    if argv0 == "grok":
        if child_path is None:
            return False
        return _bare_resolves_to_isolation(
            "grok", identity.grok, identity.grok_digest, child_path
        )
    if argv0 == "python3":
        if child_path is None:
            return False
        return _bare_resolves_to_isolation(
            "python3", identity.python3, identity.python3_digest, child_path
        )
    durable = identity.durable_python3 or _durable_wrapper_python3()
    if durable and os.path.normpath(argv0) == os.path.normpath(durable):
        try:
            st = os.lstat(argv0)
        except OSError:
            return False
        # Debian/Homebrew python3 is often a symlink; do not follow to Cellar.
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            return False
        return os.access(argv0, os.X_OK)
    if not os.path.isabs(argv0):
        return False
    try:
        real = os.path.realpath(argv0)
    except OSError:
        return False
    if real == os.path.realpath(identity.grok):
        return _matches_isolation_regular_file(
            argv0, identity.grok, identity.grok_digest
        )
    if real == os.path.realpath(identity.python3):
        return _matches_isolation_regular_file(
            argv0, identity.python3, identity.python3_digest
        )
    return False


def _allowed_subprocess_launch(
    args: Sequence[object] | str | None,
    kwargs: Mapping[str, object],
    bin_dir: Path,
    grok_home: Path,
    identity: _IsolationExecIdentity,
) -> bool:
    """Authorize argv, launch kwargs, and the isolation inode the child will exec."""
    if not _allowed_subprocess_argv(args, bin_dir, grok_home):
        return False
    if not _safe_popen_kwargs(kwargs, identity.path_snapshot):
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
    if not (
        _is_fake_grok_argv0(argv0, bin_dir)
        or _is_reviewed_python_launch(args, argv0, bin_dir, grok_home)
        or _is_reviewed_hook_shell_argv(args, argv0, grok_home)
    ):
        return False
    return _child_will_exec_isolation_inode(args, kwargs, identity)


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
    """Replace Popen and exec wrappers. Does not patch posix_spawn.

    CPython 3.14 Darwin uses os.posix_spawn inside real_popen for absolute
    executables when cwd is None. posix_spawn / posix_spawnp stay the
    builtins; they are outside the exercised surface. This smoke is not a
    universal sandbox.
    """
    apply = assign if assign is not None else _setattr_live
    real_popen = subprocess.Popen
    identity = _freeze_exec_identity(bin_dir)

    def guarded_popen(args, *rest, **kwargs):  # noqa: ANN001
        launch_kwargs = _merge_popen_launch_kwargs(rest, kwargs)
        if not _allowed_subprocess_launch(
            args, launch_kwargs, bin_dir, grok_home, identity
        ):
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
    # Gate children at Popen. Do not replace posix_spawn / posix_spawnp —
    # CPython 3.14 on Darwin uses posix_spawn inside real_popen for
    # absolute executables when cwd is None. posix_spawn is outside the
    # exercised surface; this smoke is not a universal sandbox.
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
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
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
    """Tiny local grok: version / --version / identity. Unexpected argv exits 2."""
    path = bin_dir / "grok"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "allowed = {('--version',), ('version',), ('version', '--json'), "
        "('--isolation-identity',)}\n"
        "if tuple(args) not in allowed:\n"
        "    print('stock-host fake grok: unexpected argv', args, file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
        "if args == ['--isolation-identity']:\n"
        f"    print({ISOLATION_GROK_IDENTITY!r})\n"
        "    raise SystemExit(0)\n"
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
