"""Antigravity (`agy`) provider probe — discovery, version, capabilities (#67-A).

No ask/Team cutover. No live network. Subprocess uses argv arrays only
(``shell=False``) via :mod:`omg_cli.providers.process` with process-group
cleanup and a bounded environment.
"""

from __future__ import annotations

import os
import re
import shutil
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
from omg_cli.providers.process import (
    DEFAULT_PROBE_TIMEOUT_S,
    ProbeProcessError,
    run_probe_process,
)
from omg_cli.redaction import redact_text

PROVIDER_NAME: Final[str] = "antigravity"
BINARY_NAME: Final[str] = "agy"
ENV_BIN_OVERRIDE: Final[str] = "OMG_AGY_BIN"

# Docs cross-ref to docs/parity/upstream-snapshots/Antigravity.json (compat is
# version-string based, not git SHA).
PIN_REVISION: Final[str] = "bfab12dac5bd090015a89cf82e65093d13b567d9"

# Fixture-backed tested window only (tests/fixtures/antigravity/version.txt).
# Do not widen past captures that lack hermetic evidence.
TESTED_MIN: Final[tuple[int, int, int]] = (1, 1, 10)
TESTED_MAX: Final[tuple[int, int, int]] = (1, 1, 10)
TESTED_MIN_STR: Final[str] = "1.1.10"
TESTED_MAX_STR: Final[str] = "1.1.10"

_AG_LIMITATIONS: Final[tuple[str, ...]] = (
    "Slice A probe only; headless run/ask/Team cutover deferred (#67-B..D).",
    "Authentication and live-call readiness are not verified hermetically.",
)

# Anchored to the start of the first non-empty line — never scoop a semver from
# later prose / error banners.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\b")
# Help identity: real agy --help begins with ``Usage of agy:``.
_AGY_HELP_IDENTITY_RE = re.compile(r"(?im)^Usage of agy\b")
_PROBE_TIMEOUT_S: Final[float] = DEFAULT_PROBE_TIMEOUT_S

# Flag lines: ``  --flag   description`` (two+ spaces before description).
_FLAG_LINE_RE = re.compile(r"^[ \t]+(-{1,2}[\w-]+)[ \t]{2,}(.*)$")
# Parenthetical enum groups in flag descriptions.
_ENUM_GROUP_RE = re.compile(r"\(([^)]+)\)")
_KNOWN_OUTPUT_FORMATS: Final[frozenset[str]] = frozenset(
    {"text", "json", "stream-json"}
)
_KNOWN_EFFORTS: Final[frozenset[str]] = frozenset({"low", "medium", "high"})
_KNOWN_MODES: Final[frozenset[str]] = frozenset({"accept-edits", "plan"})
_AGY_BASENAMES: Final[frozenset[str]] = frozenset({BINARY_NAME, f"{BINARY_NAME}.exe"})

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
        "FAKE_AGY_VERSION_RC",
        "FAKE_AGY_HELP_RC",
        "FAKE_AGY_HELP_EMPTY",
        "FAKE_AGY_VERSION_TEXT",
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


def _require_agy_basename(path: Path, *, source: str) -> None:
    """Fail closed when an override/path does not resolve to an ``agy`` name."""
    if path.name not in _AGY_BASENAMES:
        raise ProviderBinaryMissing(
            f"{source} basename must be {BINARY_NAME!r}, got {path.name!r}"
        )


def discover_binary(*, env: Mapping[str, str] | None = None) -> str:
    """Resolve ``agy`` path from ``OMG_AGY_BIN`` override or PATH.

    ``OMG_AGY_BIN`` must point at an executable whose basename is ``agy``
    (not an arbitrary binary). Product identity is further bound by help
    output during capability probes.
    """
    source = env if env is not None else os.environ
    override = (source.get(ENV_BIN_OVERRIDE) or "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            _require_agy_basename(path, source=f"{ENV_BIN_OVERRIDE}={override!r}")
            return str(path.resolve())
        raise ProviderBinaryMissing(
            f"OMG_AGY_BIN={override!r} is not an executable file"
        )
    path_str = shutil.which(BINARY_NAME, path=source.get("PATH"))
    if not path_str:
        raise ProviderBinaryMissing(
            f"{BINARY_NAME!r} not found on PATH (set {ENV_BIN_OVERRIDE} to override)"
        )
    resolved = Path(path_str).resolve()
    _require_agy_basename(resolved, source="PATH")
    return str(resolved)


def parse_version(text: str | None) -> VersionInfo | None:
    """Parse a semver triple from ``agy --version`` text.

    Only the first non-empty line is considered, and the version must be
    anchored at the start of that line (``raw`` and the tuple always agree).
    """
    if not text or not str(text).strip():
        return None
    first = ""
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    if not first:
        return None
    m = _VERSION_RE.match(first)
    if not m:
        return None
    # Store only the matched semver fragment — never the rest of a
    # child-controlled line (which may carry credentials / junk).
    return VersionInfo(
        raw=m.group(0),
        major=int(m.group(1)),
        minor=int(m.group(2)),
        patch=int(m.group(3)),
    )


def _verify_agy_help_identity(binary: str, help_text: str) -> None:
    """Require Antigravity help identity so impostor ``agy`` names cannot green."""
    if not _AGY_HELP_IDENTITY_RE.search(help_text or ""):
        raise ProviderProbeError(
            f"binary {binary!r} help output lacks Antigravity identity "
            f"(expected a 'Usage of agy' header)"
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


def _run_probe_argv(argv: list[str]):
    """Run a probe argv with cancel_event wired for Ctrl-C / SIGINT cleanup."""
    import signal
    import threading

    cancel = threading.Event()
    previous = None
    if (
        os.name == "posix"
        and threading.current_thread() is threading.main_thread()
    ):
        def _on_sigint(signum, frame):  # noqa: ARG001
            cancel.set()

        previous = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, _on_sigint)
        except (ValueError, OSError):
            previous = None

    try:
        result = run_probe_process(
            argv,
            env=_bounded_env(),
            timeout_s=_PROBE_TIMEOUT_S,
            cancel_event=cancel,
        )
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except (ValueError, OSError):
                pass

    if cancel.is_set():
        # Preserve Ctrl-C semantics after process-group cleanup. cancel_event
        # may be set during wait/join even when the direct child already exited.
        raise KeyboardInterrupt()
    return result


def probe_version(binary: str | None = None) -> VersionInfo:
    """Run ``[binary, --version]`` via argv (no shell) and parse stdout.

    Fail-closed: non-zero exit is a version error even when stderr embeds a
    parseable semver (init failure must not count as a successful probe).
    """
    path = binary or discover_binary()
    argv = [path, "--version"]
    try:
        result = _run_probe_argv(argv)
    except ProbeProcessError as exc:
        raise ProviderVersionError(
            f"version probe failed for {path}: {redact_text(str(exc))}"
        ) from exc
    if result.timed_out:
        raise ProviderVersionError(f"version probe timed out for {path}")
    if result.cancelled:
        raise ProviderVersionError(f"version probe cancelled for {path}")
    if result.overflow or result.stdout_truncated or result.stderr_truncated:
        raise ProviderVersionError(f"version probe output truncated for {path}")
    if result.returncode != 0:
        detail = redact_text((result.stderr or result.stdout or "").strip())
        raise ProviderVersionError(
            f"version probe exit {result.returncode} for {path}"
            + (f": {detail}" if detail else "")
        )
    out = (result.stdout or result.stderr or "").strip()
    if not out:
        raise ProviderVersionError(f"version probe exit 0 with empty output: {path}")
    info = parse_version(out)
    if info is None:
        raise ProviderVersionError(
            f"cannot parse agy version from {redact_text(out)!r}"
        )
    return info


def _extract_flag_map(help_text: str) -> dict[str, str]:
    """Map exact flag tokens (``--print``, ``-p``, …) to their descriptions."""
    flags: dict[str, str] = {}
    for line in help_text.splitlines():
        m = _FLAG_LINE_RE.match(line)
        if not m:
            continue
        flags[m.group(1)] = m.group(2).strip()
    return flags


def _extract_subcommands(help_text: str) -> frozenset[str]:
    """Parse names under an ``Available subcommands:`` section only."""
    names: set[str] = set()
    in_section = False
    for line in help_text.splitlines():
        stripped = line.strip()
        if not in_section:
            if stripped.lower().startswith("available subcommands"):
                in_section = True
            continue
        if not stripped:
            if names:
                break
            continue
        # Leave the section on a non-indented header.
        if line[:1] not in (" ", "\t"):
            break
        token = stripped.split(None, 1)[0].lower()
        if token:
            names.add(token)
    return frozenset(names)


def _enum_values_from_desc(desc: str, allowed: frozenset[str]) -> tuple[str, ...]:
    """Pull allowed enum tokens from parenthetical groups; ignore ``default …``."""
    found: list[str] = []
    seen: set[str] = set()
    for group in _ENUM_GROUP_RE.findall(desc):
        inner = group.strip()
        if inner.lower().startswith("default"):
            continue
        for part in re.split(r"[|,]", inner):
            token = part.strip().lower()
            if token in allowed and token not in seen:
                seen.add(token)
                found.append(token)
    return tuple(found)


def _parse_help_supports(help_text: str) -> dict[str, object]:
    """Derive supports from structured flag/enum/subcommand evidence only.

    Fail-closed: loose prose / unrelated substrings (``allows``, ``plan``,
    ``low``, ``-p`` inside ``--project``, ``json`` inside ``--json-schema``)
    must never invent capability claims.
    """
    if not help_text or not str(help_text).strip():
        return {
            "output_formats": (),
            "efforts": (),
            "modes": (),
            "print_mode": False,
            "sandbox": False,
            "agents_subcommand": False,
            "models_subcommand": False,
            "plugins_subcommand": False,
        }

    flags = _extract_flag_map(help_text)
    subcommands = _extract_subcommands(help_text)

    formats = _enum_values_from_desc(
        flags.get("--output-format", ""), _KNOWN_OUTPUT_FORMATS
    )
    efforts = _enum_values_from_desc(flags.get("--effort", ""), _KNOWN_EFFORTS)
    modes = _enum_values_from_desc(flags.get("--mode", ""), _KNOWN_MODES)

    print_mode = "--print" in flags
    if not print_mode and "-p" in flags:
        # Accept ``-p`` only when its description aliases ``--print``.
        print_mode = "--print" in flags["-p"].lower()

    return {
        "output_formats": formats,
        "efforts": efforts,
        "modes": modes,
        "print_mode": print_mode,
        "sandbox": "--sandbox" in flags,
        "agents_subcommand": bool(subcommands & {"agent", "agents"}),
        "models_subcommand": "models" in subcommands,
        "plugins_subcommand": bool(subcommands & {"plugin", "plugins"}),
    }


def _probe_help_text(binary: str) -> str:
    """Run ``[binary, --help]``; require successful exit and non-empty evidence."""
    argv = [binary, "--help"]
    try:
        result = _run_probe_argv(argv)
    except ProbeProcessError as exc:
        raise ProviderProbeError(
            f"help probe failed for {binary}: {redact_text(str(exc))}"
        ) from exc
    if result.timed_out:
        raise ProviderProbeError(f"help probe timed out for {binary}")
    if result.cancelled:
        raise ProviderProbeError(f"help probe cancelled for {binary}")
    if result.overflow or result.stdout_truncated or result.stderr_truncated:
        # Partial help must not feed strict doctor a truncated capability surface.
        raise ProviderProbeError(f"help probe output truncated for {binary}")
    if result.returncode != 0:
        detail = redact_text((result.stderr or result.stdout or "").strip())
        raise ProviderProbeError(
            f"help probe exit {result.returncode} for {binary}"
            + (f": {detail}" if detail else "")
        )
    text = (result.stdout or result.stderr or "").strip()
    if not text:
        raise ProviderProbeError(f"help probe exit 0 with empty output: {binary}")
    return text


def probe_capabilities(binary: str | None = None) -> ProviderCapabilities:
    """Build the golden capabilities envelope for the discovered binary."""
    path = binary or discover_binary()
    # Basename gate even when callers pass an explicit path.
    _require_agy_basename(Path(path), source="binary")
    info = probe_version(path)
    help_text = _probe_help_text(path)
    _verify_agy_help_identity(path, help_text)
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
        limitations=_AG_LIMITATIONS,
    )


def doctor(*, strict: bool = False, binary: str | None = None) -> DoctorReport:
    """Classify install/compat readiness. ``--strict`` fails closed on soft issues.

    Slice A never claims ``live_call_ready`` or authenticated=true without a
    later live probe (#67-B+). Successful version+help probes with real observed
    support evidence are required before ``ok`` / strict exit 0.

    Note: top-level ``omg doctor --strict`` does **not** run this Antigravity
    probe in slice A — callers must use ``omg provider antigravity doctor``.
    """
    checks: list[str] = []
    try:
        path = binary or discover_binary()
        checks.append(f"OK: installed {path}")
    except ProviderBinaryMissing as exc:
        checks.append(f"FAIL: missing binary ({redact_text(str(exc))})")
        return DoctorReport(ok=False, exit_code=1, checks=tuple(checks))

    try:
        caps = probe_capabilities(path)
    except (ProviderVersionError, ProviderProbeError, ProviderBinaryMissing) as exc:
        checks.append(f"FAIL: probe error ({redact_text(str(exc))})")
        return DoctorReport(ok=False, exit_code=1, checks=tuple(checks))

    status = caps.compat_status
    if status == "compatible":
        checks.append(
            f"OK: compatible version {caps.version} "
            f"(tested {TESTED_MIN_STR}..{TESTED_MAX_STR})"
        )
    elif status == "unknown":
        msg = f"cannot classify version {caps.version!r}"
        checks.append(f"{'FAIL' if strict else 'WARN'}: {msg}")
    else:
        msg = (
            f"version {caps.version} is {status} for tested range "
            f"{TESTED_MIN_STR}..{TESTED_MAX_STR}"
        )
        checks.append(f"{'FAIL' if strict else 'WARN'}: {msg}")

    # Observed evidence only — empty support claims mean the help surface did
    # not prove formats/efforts/modes (fail-closed vs inventing).
    if not caps.output_formats and not caps.efforts and not caps.modes:
        msg = "no observed capability evidence from help probe"
        checks.append(f"{'FAIL' if strict else 'WARN'}: {msg}")
        evidence_ok = False
    else:
        checks.append(
            "OK: observed supports "
            f"formats={list(caps.output_formats)} "
            f"efforts={list(caps.efforts)} modes={list(caps.modes)}"
        )
        evidence_ok = True

    checks.append("OK: live_call_ready=false (slice A; no live claim)")
    checks.append("OK: authenticated=null (not probed in slice A)")

    hard_fail = status != "compatible" or not evidence_ok
    if strict and hard_fail:
        return DoctorReport(
            ok=False, exit_code=1, checks=tuple(checks), capabilities=caps
        )
    # Non-strict: soft incompat still reports ok=False for callers, exit 0.
    return DoctorReport(
        ok=(status == "compatible" and evidence_ok),
        exit_code=0 if not (strict and hard_fail) else 1,
        checks=tuple(checks),
        capabilities=caps,
    )


class AntigravityProvider:
    """:class:`~omg_cli.providers.base.ProviderAdapter` for Antigravity."""

    name: str = PROVIDER_NAME

    def discover_binary(self) -> str:
        return discover_binary()

    def probe_version(self, binary: str | None = None) -> VersionInfo:
        return probe_version(binary)

    def probe_capabilities(self, binary: str | None = None) -> ProviderCapabilities:
        return probe_capabilities(binary)

    def doctor(self, *, strict: bool = False) -> DoctorReport:
        return doctor(strict=strict)


def get_adapter() -> AntigravityProvider:
    """Return the Antigravity :class:`ProviderAdapter` implementation."""
    return AntigravityProvider()


__all__ = [
    "BINARY_NAME",
    "ENV_BIN_OVERRIDE",
    "PIN_REVISION",
    "PROVIDER_NAME",
    "TESTED_MAX",
    "TESTED_MAX_STR",
    "TESTED_MIN",
    "TESTED_MIN_STR",
    "AntigravityProvider",
    "classify_compat",
    "discover_binary",
    "doctor",
    "get_adapter",
    "parse_version",
    "probe_capabilities",
    "probe_version",
]
