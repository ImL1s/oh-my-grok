"""Antigravity (`agy`) provider probe — discovery, version, capabilities (#67-A).

No ask/Team cutover. No live network. Subprocess uses argv arrays only
(``shell=False``) with a bounded environment.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final, Mapping

from omg_cli.providers.errors import (
    ProviderBinaryMissing,
    ProviderProbeError,
    ProviderVersionError,
)
from omg_cli.providers.models import (
    CompatStatus,
    DoctorReport,
    ProviderCapabilities,
    VersionInfo,
)

PROVIDER_NAME: Final[str] = "antigravity"
BINARY_NAME: Final[str] = "agy"
ENV_BIN_OVERRIDE: Final[str] = "OMG_AGY_BIN"

# Docs cross-ref to docs/parity/upstream-snapshots/Antigravity.json (compat is
# version-string based, not git SHA).
PIN_REVISION: Final[str] = "bfab12dac5bd090015a89cf82e65093d13b567d9"

# Tested compatibility window for hermetic fixtures (pinned capture 1.1.10).
TESTED_MIN: Final[tuple[int, int, int]] = (1, 1, 0)
TESTED_MAX: Final[tuple[int, int, int]] = (1, 1, 99)
TESTED_MIN_STR: Final[str] = "1.1.0"
TESTED_MAX_STR: Final[str] = "1.1.99"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_PROBE_TIMEOUT_S: Final[float] = 8.0

# Env keys allowed through to child probes (plus PATH / override path).
_BOUNDED_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "FAKE_AGY_VERSION",
        "FAKE_AGY_HELP",
        ENV_BIN_OVERRIDE,
    }
)


def _bounded_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _BOUNDED_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if extra:
        env.update(extra)
    return env


def discover_binary(*, env: Mapping[str, str] | None = None) -> str:
    """Resolve ``agy`` path from ``OMG_AGY_BIN`` override or PATH."""
    source = env if env is not None else os.environ
    override = (source.get(ENV_BIN_OVERRIDE) or "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise ProviderBinaryMissing(
            f"OMG_AGY_BIN={override!r} is not an executable file"
        )
    path_str = shutil.which(BINARY_NAME, path=source.get("PATH"))
    if not path_str:
        raise ProviderBinaryMissing(
            f"{BINARY_NAME!r} not found on PATH (set {ENV_BIN_OVERRIDE} to override)"
        )
    return str(Path(path_str).resolve())


def parse_version(text: str | None) -> VersionInfo | None:
    """Parse a semver triple from ``agy --version`` text."""
    if not text or not str(text).strip():
        return None
    m = _VERSION_RE.search(str(text).strip())
    if not m:
        return None
    return VersionInfo(
        raw=str(text).strip().splitlines()[0].strip(),
        major=int(m.group(1)),
        minor=int(m.group(2)),
        patch=int(m.group(3)),
    )


def classify_compat(version: VersionInfo | tuple[int, int, int] | None) -> CompatStatus:
    """Classify version against :data:`TESTED_MIN` / :data:`TESTED_MAX`."""
    if version is None:
        return "unknown"
    tup = version.as_tuple() if isinstance(version, VersionInfo) else version
    if tup < TESTED_MIN:
        return "too_old"
    if tup > TESTED_MAX:
        return "too_new"
    return "compatible"


def probe_version(binary: str | None = None) -> VersionInfo:
    """Run ``[binary, --version]`` via argv (no shell) and parse stdout."""
    path = binary or discover_binary()
    argv = [path, "--version"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
            shell=False,
            env=_bounded_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderVersionError(f"version probe failed for {path}: {exc}") from exc
    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        raise ProviderVersionError(
            f"version probe exit {proc.returncode} with empty output: {path}"
        )
    info = parse_version(out)
    if info is None:
        raise ProviderVersionError(f"cannot parse agy version from {out!r}")
    return info


def _parse_help_supports(help_text: str) -> dict[str, object]:
    """Derive support flags from pinned/help probe text (no live claims)."""
    low = help_text.lower()
    formats: list[str] = []
    if "text" in low and "output-format" in low:
        formats.append("text")
    if "json" in low:
        formats.append("json")
    if "stream-json" in low:
        formats.append("stream-json")
    if not formats:
        formats = ["text", "json", "stream-json"]
    efforts = []
    for e in ("low", "medium", "high"):
        if e in low:
            efforts.append(e)
    if not efforts:
        efforts = ["low", "medium", "high"]
    modes = []
    if "accept-edits" in low:
        modes.append("accept-edits")
    if "plan" in low:
        modes.append("plan")
    if not modes:
        modes = ["accept-edits", "plan"]
    return {
        "output_formats": tuple(formats),
        "efforts": tuple(efforts),
        "modes": tuple(modes),
        "print_mode": "--print" in low or "-p" in low,
        "sandbox": "--sandbox" in low,
        "agents_subcommand": "agent" in low,
        "models_subcommand": "models" in low,
        "plugins_subcommand": "plugin" in low,
    }


def _probe_help_text(binary: str) -> str:
    argv = [binary, "--help"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
            shell=False,
            env=_bounded_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderProbeError(f"help probe failed for {binary}: {exc}") from exc
    return (proc.stdout or proc.stderr or "").strip()


def probe_capabilities(binary: str | None = None) -> ProviderCapabilities:
    """Build the golden capabilities envelope for the discovered binary."""
    path = binary or discover_binary()
    info = probe_version(path)
    help_text = _probe_help_text(path)
    supports = _parse_help_supports(help_text)
    status = classify_compat(info)
    return ProviderCapabilities(
        provider=PROVIDER_NAME,
        binary=path,
        version=info.raw,
        version_tuple=info.as_tuple(),
        compat_status=status,
        tested_min=TESTED_MIN_STR,
        tested_max=TESTED_MAX_STR,
        pin_revision=PIN_REVISION,
        authenticated=None,
        live_call_ready=False,
        output_formats=supports["output_formats"],  # type: ignore[arg-type]
        efforts=supports["efforts"],  # type: ignore[arg-type]
        modes=supports["modes"],  # type: ignore[arg-type]
        print_mode=bool(supports["print_mode"]),
        sandbox=bool(supports["sandbox"]),
        agents_subcommand=bool(supports["agents_subcommand"]),
        models_subcommand=bool(supports["models_subcommand"]),
        plugins_subcommand=bool(supports["plugins_subcommand"]),
        background_tasks=False,
        hooks=False,
        skills=False,
        mcp=False,
        subagents=False,
        needs_pty=True,
    )


def doctor(*, strict: bool = False, binary: str | None = None) -> DoctorReport:
    """Classify install/compat readiness. ``--strict`` fails closed on soft issues.

    Slice A never claims ``live_call_ready`` or authenticated=true without a
    later live probe (#67-B+).
    """
    checks: list[str] = []
    try:
        path = binary or discover_binary()
        checks.append(f"OK: installed {path}")
    except ProviderBinaryMissing as exc:
        checks.append(f"FAIL: missing binary ({exc})")
        return DoctorReport(ok=False, exit_code=1, checks=tuple(checks))

    try:
        caps = probe_capabilities(path)
    except (ProviderVersionError, ProviderProbeError, ProviderBinaryMissing) as exc:
        checks.append(f"FAIL: probe error ({exc})")
        return DoctorReport(ok=False, exit_code=1, checks=tuple(checks))

    status = caps.compat_status
    if status == "compatible":
        checks.append(f"OK: compatible version {caps.version} "
                      f"(tested {TESTED_MIN_STR}..{TESTED_MAX_STR})")
    elif status == "unknown":
        msg = f"cannot classify version {caps.version!r}"
        checks.append(f"{'FAIL' if strict else 'WARN'}: {msg}")
    else:
        msg = f"version {caps.version} is {status} for tested range " \
              f"{TESTED_MIN_STR}..{TESTED_MAX_STR}"
        checks.append(f"{'FAIL' if strict else 'WARN'}: {msg}")

    checks.append("OK: live_call_ready=false (slice A; no live claim)")
    checks.append("OK: authenticated=null (not probed in slice A)")

    hard_fail = status != "compatible"
    if strict and hard_fail:
        return DoctorReport(
            ok=False, exit_code=1, checks=tuple(checks), capabilities=caps
        )
    # Non-strict: soft incompat still reports ok=False for callers, exit 0.
    return DoctorReport(
        ok=(status == "compatible"),
        exit_code=0 if not (strict and hard_fail) else 1,
        checks=tuple(checks),
        capabilities=caps,
    )


__all__ = [
    "BINARY_NAME",
    "ENV_BIN_OVERRIDE",
    "PIN_REVISION",
    "PROVIDER_NAME",
    "TESTED_MAX",
    "TESTED_MAX_STR",
    "TESTED_MIN",
    "TESTED_MIN_STR",
    "classify_compat",
    "discover_binary",
    "doctor",
    "parse_version",
    "probe_capabilities",
    "probe_version",
]
