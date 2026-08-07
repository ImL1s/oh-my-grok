"""Antigravity (`agy`) provider — discovery, capabilities, headless run (#67-A/B).

No ask/Team cutover. No live network. Subprocess uses argv arrays only
(``shell=False``) via :mod:`omg_cli.providers.process` with process-group
cleanup and a bounded environment. Headless ``run`` reuses the same process
stack as probes (no second runner).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from omg_cli.providers.errors import (
    ProviderBinaryMissing,
    ProviderProbeError,
    ProviderRunError,
    ProviderVersionError,
)
from omg_cli.providers.models import (
    CompatStatus,
    DoctorReport,
    ProviderCapabilities,
    ProviderExitClass,
    ProviderOutputFormat,
    ProviderRunEvent,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderUsage,
    VersionInfo,
)
from omg_cli.providers.process import (
    DEFAULT_PROBE_TIMEOUT_S,
    DEFAULT_RUN_MAX_OUTPUT_BYTES,
    ProbeProcessError,
    ProbeProcessResult,
    run_probe_process,
    run_provider_process,
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
    "Slice B headless run available via ProviderAdapter.run; ask/Team cutover deferred (#67-C/D).",
    "Authentication and live-call readiness are not verified hermetically.",
)

# Exact ASCII-decimal triple on the first non-empty line — never scoop a
# prefix from prerelease / extra components / trailing child-controlled junk
# (pin-only window must not false-green ``1.1.10-rc.1`` / ``1.1.10.1``).
# Use ``[0-9]`` (not ``\d``) so Unicode digits cannot match.
_VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")
# Parser-owned bounds — do not rely on CPython's global int digit limit.
_VERSION_COMPONENT_MAX_DIGITS: Final[int] = 9
_VERSION_COMPONENT_MAX_VALUE: Final[int] = 999_999_999
# Help identity: the first non-empty line of real ``agy --help`` is exactly
# ``Usage of agy:`` (apart from trailing horizontal whitespace).
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

# Env keys allowed through to child probes/runs (plus PATH / override path).
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
        "FAKE_AGY_RUN_RC",
        "FAKE_AGY_RUN_SLEEP",
        "FAKE_AGY_RUN_STDOUT",
        "FAKE_AGY_RUN_STDERR",
        "FAKE_AGY_RUN_PARTIAL",
        "FAKE_AGY_RUN_AUTH_BLOCK",
        "FAKE_AGY_RUN_AUTH_EXIT0",
        "FAKE_AGY_RUN_SESSION",
        "FAKE_AGY_RUN_RESUME",
        "FAKE_AGY_RUN_MALFORMED",
        "FAKE_AGY_RUN_TRUNCATE_STREAM",
        "FAKE_AGY_ECHO_CWD",
        "FAKE_AGY_ECHO_ENV",
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
        # Fail-closed: extras must also be on the allowlist (no secret injection).
        for key, val in extra.items():
            if key in _BOUNDED_ENV_KEYS:
                env[key] = val
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


def _parse_version_component(raw: str) -> int:
    """Parse one MAJOR/MINOR/PATCH token with canonical ASCII-decimal rules.

    Rejects leading zeros (``01``), oversized digit strings, and values above
    :data:`_VERSION_COMPONENT_MAX_VALUE`. Raises :class:`ProviderVersionError`
    rather than depending on CPython ``int()`` digit limits.
    """
    if not raw or not raw.isascii() or not raw.isdigit():
        raise ProviderVersionError(f"version component invalid: {raw!r}")
    if len(raw) > 1 and raw[0] == "0":
        raise ProviderVersionError(
            f"version component not canonical (leading zero): {raw!r}"
        )
    if len(raw) > _VERSION_COMPONENT_MAX_DIGITS:
        raise ProviderVersionError(
            f"version component exceeds {_VERSION_COMPONENT_MAX_DIGITS} digits: {raw!r}"
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProviderVersionError(
            f"version component overflow or invalid: {raw!r}"
        ) from exc
    except MemoryError as exc:
        raise ProviderVersionError(
            f"version component overflow or invalid: {raw!r}"
        ) from exc
    if value > _VERSION_COMPONENT_MAX_VALUE:
        raise ProviderVersionError(
            f"version component exceeds bound {_VERSION_COMPONENT_MAX_VALUE}: {raw!r}"
        )
    return value


def parse_version(text: str | None) -> VersionInfo | None:
    """Parse a semver triple from ``agy --version`` text.

    Only the first non-empty line is considered, and that line must be an
    exact ``MAJOR.MINOR.PATCH`` (no prerelease / build / extra components).
    Non-canonical forms (leading zeros, oversized components) raise
    :class:`ProviderVersionError` rather than silently normalizing into the
    pin window.
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
    # Exact-line match: raw is the whole first line (no trailing fragment).
    major = _parse_version_component(m.group(1))
    minor = _parse_version_component(m.group(2))
    patch = _parse_version_component(m.group(3))
    return VersionInfo(
        raw=m.group(0),
        major=major,
        minor=minor,
        patch=patch,
    )


def _verify_agy_help_identity(binary: str, help_text: str) -> None:
    """Require Antigravity help identity so impostor ``agy`` names cannot green."""
    for line in (help_text or "").splitlines():
        if not line.strip():
            continue
        if line.rstrip(" \t") == "Usage of agy:":
            return
        break
    raise ProviderProbeError(
        f"binary {binary!r} help output lacks Antigravity identity "
        f"(expected first non-empty line 'Usage of agy:')"
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
    return _run_provider_argv(argv, mode="probe", timeout_s=_PROBE_TIMEOUT_S)


def _run_provider_argv(
    argv: list[str],
    *,
    mode: str,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    cwd: str | None = None,
    max_output_bytes: int | None = None,
) -> ProbeProcessResult:
    """Shared argv executor (probe or run) with optional SIGINT cancel wiring."""
    import signal

    cancel = cancel_event if cancel_event is not None else threading.Event()
    own_cancel = cancel_event is None
    previous = None
    result = None
    result_tree_kill_attempted = False

    def _kill_result_tree() -> None:
        """Reap a returned process's POSIX process group, if it has one."""
        nonlocal result_tree_kill_attempted
        pgid = int(getattr(result, "pid", 0) or 0)
        if os.name == "posix" and pgid > 0 and not result_tree_kill_attempted:
            result_tree_kill_attempted = True
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def _raise_if_cancelled() -> None:
        if cancel.is_set() and own_cancel:
            # Caller-owned cancel_event: return cancelled result; do not raise.
            _kill_result_tree()
            raise KeyboardInterrupt()

    if (
        own_cancel
        and os.name == "posix"
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
        if mode == "probe":
            result = run_probe_process(
                argv,
                env=_bounded_env(env),
                timeout_s=timeout_s,
                cancel_event=cancel,
            )
        else:
            result = run_provider_process(
                argv,
                env=_bounded_env(env),
                timeout_s=timeout_s,
                max_output_bytes=max_output_bytes,
                cancel_event=cancel,
                cwd=cwd,
                mode="run",
            )
        if own_cancel:
            _raise_if_cancelled()
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
                previous = None
            _raise_if_cancelled()
        return result
    except BaseException:
        _kill_result_tree()
        raise
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except BaseException:
                _kill_result_tree()
                raise
            if own_cancel:
                _raise_if_cancelled()


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
    if (
        result.overflow
        or result.stdout_truncated
        or result.stderr_truncated
        or result.stdout_read_error
        or result.stderr_read_error
    ):
        raise ProviderVersionError(f"version probe output truncated or unreadable for {path}")
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
    if (
        result.overflow
        or result.stdout_truncated
        or result.stderr_truncated
        or result.stdout_read_error
        or result.stderr_read_error
    ):
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
    probe in slice B — callers must use ``omg provider antigravity doctor``.
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

    checks.append("OK: live_call_ready=false (slice B; no live claim)")
    checks.append("OK: authenticated=null (not probed in slice B)")

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


_OUTPUT_FORMATS: Final[frozenset[str]] = frozenset({"text", "json", "stream-json"})
# Standalone CLI auth/login *banner* lines — not prose that merely mentions login.
_AUTH_BANNER_LINE_RE = re.compile(
    r"^(?:"
    r"please\s+(?:sign|log)[\s-]?in(?:\s+to\s+continue)?(?:\s*\([^)]*\))?"
    r"|(?:sign|log)[\s-]?in\s+to\s+continue(?:\s*\([^)]*\))?"
    r"|authentication(?:\s+is)?\s+required(?:\s*\([^)]*\))?"
    r"|auth(?:entication)?\s+required(?:\s*\([^)]*\))?"
    r"|please\s+login(?:\s+to\s+continue)?(?:\s*\([^)]*\))?"
    r")\.?$",
    re.IGNORECASE,
)


def _resolve_prompt(request: ProviderRunRequest) -> str:
    """Load prompt text from request fields / artifact descriptors."""
    if request.prompt_file:
        path = Path(request.prompt_file)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderRunError(
                f"cannot read prompt_file {request.prompt_file!r}: {exc}"
            ) from exc
    if request.artifacts:
        for art in request.artifacts:
            if art.kind == "prompt" and art.path:
                try:
                    return Path(art.path).read_text(encoding="utf-8")
                except OSError as exc:
                    raise ProviderRunError(
                        f"cannot read prompt artifact {art.path!r}: {exc}"
                    ) from exc
    if request.prompt:
        return request.prompt
    raise ProviderRunError("run requires prompt, prompt_file, or prompt artifact")


def build_run_argv(
    binary: str,
    prompt: str,
    *,
    output_format: ProviderOutputFormat = "text",
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    resume_id: str | None = None,
    session_id: str | None = None,  # noqa: ARG001 — metadata only; not argv
) -> list[str]:
    """Assemble headless ``agy`` argv (list only; never a shell string).

    Go flag semantics: flags after the first positional are ignored, and a
    prompt that itself starts with ``-`` would be parsed as a flag. Therefore
    every flag precedes ``--``, and the prompt is the sole token after
    end-of-options: ``agy --print [flags...] -- <prompt>``.

    Least-permissive by default: no ``--dangerously-skip-permissions``.
    ``session_id`` is execution metadata and is not injected into argv;
    use ``resume_id`` → ``--conversation`` for resume.
    """
    if output_format not in _OUTPUT_FORMATS:
        raise ProviderRunError(f"unsupported output_format: {output_format!r}")
    argv: list[str] = [binary, "--print"]
    if output_format != "text":
        argv.extend(["--output-format", output_format])
    if model:
        argv.extend(["--model", model])
    if effort:
        argv.extend(["--effort", effort])
    if mode:
        argv.extend(["--mode", mode])
    if resume_id:
        argv.extend(["--conversation", resume_id])
    # End-of-options so leading-dash prompts cannot be parsed as flags.
    argv.extend(["--", prompt])
    return argv


def assert_flags_before_prompt(argv: Sequence[str], prompt: str) -> None:
    """Require ``… -- <prompt>`` with no flags after end-of-options.

    The prompt is the final argv token and must be immediately preceded by
    ``--``. This keeps Go flag parsing from treating ``--help``-shaped prompts
    as options and rejects flags after the first positional.
    """
    if len(argv) < 3:
        raise ProviderRunError("argv too short for --print … -- <prompt>")
    if argv[-1] != prompt:
        raise ProviderRunError(
            "prompt must be the final argv positional "
            f"(tail={list(argv[-3:])!r})"
        )
    if argv[-2] != "--":
        raise ProviderRunError(
            "expected end-of-options '--' immediately before prompt "
            f"(tail={list(argv[-3:])!r})"
        )
    # Nothing may appear after the prompt; nothing but the prompt after '--'.
    try:
        ddash = list(argv).index("--")
    except ValueError as exc:
        raise ProviderRunError("missing end-of-options '--' before prompt") from exc
    if ddash != len(argv) - 2:
        raise ProviderRunError(
            "only the prompt may follow end-of-options '--' "
            f"(after={list(argv[ddash + 1 :])!r})"
        )
    for tok in argv[ddash + 1 : -1]:
        if tok.startswith("-"):
            raise ProviderRunError(
                f"flag {tok!r} appears after end-of-options (Go flag order)"
            )


def _usage_from_mapping(raw: Any) -> ProviderUsage | None:
    if not isinstance(raw, dict):
        return None

    def _as_int(key: str) -> int | None:
        val = raw.get(key)
        if isinstance(val, bool) or val is None:
            return None
        if isinstance(val, int):
            return val
        if isinstance(val, float) and val == int(val):
            return int(val)
        return None

    extra = {
        k: v
        for k, v in raw.items()
        if k not in {"input_tokens", "output_tokens", "total_tokens"}
    }
    return ProviderUsage(
        input_tokens=_as_int("input_tokens"),
        output_tokens=_as_int("output_tokens"),
        total_tokens=_as_int("total_tokens"),
        extra=extra,
    )


def _text_from_payload(payload: dict[str, Any]) -> str:
    for key in ("result", "message", "content", "text", "output"):
        val = payload.get(key)
        if isinstance(val, str):
            return val
    return ""


def parse_json_result(
    text: str,
) -> tuple[str, tuple[ProviderRunEvent, ...], ProviderUsage | None, dict[str, Any]]:
    """Parse a single JSON object from headless ``--output-format json`` stdout.

    Returns ``(normalized_output, events, usage, meta)``. Raises
    :class:`ProviderRunError` on empty/malformed JSON.
    """
    body = (text or "").strip()
    if not body:
        raise ProviderRunError("empty json output")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderRunError(f"malformed json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProviderRunError("json root must be an object")
    event = ProviderRunEvent(
        type=str(payload.get("type") or "result"),
        payload=dict(payload),
        raw=body,
        index=0,
        malformed=False,
    )
    usage = _usage_from_mapping(payload.get("usage"))
    meta = {
        "session_id": payload.get("session_id") or payload.get("sessionId"),
        "resume_token": payload.get("resume_token") or payload.get("resumeToken"),
    }
    return _text_from_payload(payload) or body, (event,), usage, meta


def parse_stream_json(
    text: str,
    *,
    truncated: bool = False,
) -> tuple[
    str, tuple[ProviderRunEvent, ...], ProviderUsage | None, dict[str, Any], bool
]:
    """Line-oriented stream-json parser; preserves order and partial EOF.

    Returns ``(output, events, usage, meta, had_malformed)``. Does not
    ``json.loads`` the full buffer as one document.
    """
    del truncated  # reserved for callers; truncation is not a parse error by itself
    events: list[ProviderRunEvent] = []
    usage: ProviderUsage | None = None
    meta: dict[str, Any] = {}
    output = ""
    had_malformed = False
    raw_lines = (text or "").splitlines()
    for idx, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            had_malformed = True
            events.append(
                ProviderRunEvent(
                    type="parse_error",
                    payload={},
                    raw=line,
                    index=idx,
                    malformed=True,
                )
            )
            continue
        if not isinstance(payload, dict):
            had_malformed = True
            events.append(
                ProviderRunEvent(
                    type="parse_error",
                    payload={"value": payload},
                    raw=line,
                    index=idx,
                    malformed=True,
                )
            )
            continue
        etype = str(payload.get("type") or "event")
        events.append(
            ProviderRunEvent(
                type=etype,
                payload=dict(payload),
                raw=line,
                index=idx,
                malformed=False,
            )
        )
        if etype in {"result", "final", "completion"} or "result" in payload:
            chunk = _text_from_payload(payload)
            if chunk:
                output = chunk
            u = _usage_from_mapping(payload.get("usage"))
            if u is not None:
                usage = u
            sid = payload.get("session_id") or payload.get("sessionId")
            if sid:
                meta["session_id"] = sid
            tok = payload.get("resume_token") or payload.get("resumeToken")
            if tok:
                meta["resume_token"] = tok
        elif not output:
            chunk = _text_from_payload(payload)
            if chunk:
                output = chunk
    if not output and events and not had_malformed:
        parts = [
            _text_from_payload(e.payload)
            for e in events
            if not e.malformed and _text_from_payload(e.payload)
        ]
        output = "\n".join(parts)
    return output, tuple(events), usage, meta, had_malformed


def _line_is_auth_banner(line: str) -> bool:
    return bool(_AUTH_BANNER_LINE_RE.match((line or "").strip()))


def _looks_auth_blocked(
    stdout: str,
    stderr: str,
    *,
    has_valid_structured_result: bool = False,
    returncode: int = 0,
) -> bool:
    """True for CLI auth/login *banners*, not prose that mentions signing in.

    Prefer stderr banners (typical CLI auth path). Stdout banners only count
    when there is no valid structured result — so a successful json/text answer
    that says "please log in to your bank" is not false-blocked.
    """
    del returncode
    for line in (stderr or "").splitlines():
        if _line_is_auth_banner(line):
            return True
    if has_valid_structured_result:
        return False
    non_empty = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    if not non_empty:
        return False
    # Standalone banner: every non-empty stdout line is an auth prompt shape.
    return all(_line_is_auth_banner(ln) for ln in non_empty)


def _classify_exit(
    *,
    proc: ProbeProcessResult,
    parse_error: bool,
    auth_blocked: bool,
) -> ProviderExitClass:
    if proc.cancelled:
        return "cancelled"
    if proc.timed_out:
        return "timeout"
    if proc.overflow:
        return "overflow"
    if auth_blocked:
        return "auth_blocked"
    if parse_error:
        return "parse_error"
    if proc.returncode == 0:
        return "success"
    if proc.returncode != 0:
        return "nonzero"
    return "unknown"


def run(request: ProviderRunRequest) -> ProviderRunResult:
    """Headless Antigravity execution via :func:`run_provider_process`.

    Timeout/cancel preserve partial stdout/stderr and any parsed events.
    Session/resume fields are execution metadata only (no Team coupling).
    """
    if not isinstance(request, ProviderRunRequest):
        raise ProviderRunError("request must be ProviderRunRequest")
    if request.timeout_s <= 0:
        raise ProviderRunError("timeout_s must be positive")
    fmt: ProviderOutputFormat = request.output_format  # type: ignore[assignment]
    if fmt not in _OUTPUT_FORMATS:
        raise ProviderRunError(f"unsupported output_format: {fmt!r}")

    prompt = _resolve_prompt(request)
    try:
        path = request.binary or discover_binary()
    except ProviderBinaryMissing as exc:
        raise ProviderRunError(str(exc)) from exc
    _require_agy_basename(Path(path), source="binary")

    argv = build_run_argv(
        path,
        prompt,
        output_format=fmt,
        model=request.model,
        effort=request.effort,
        mode=request.mode,
        resume_id=request.resume_id,
        session_id=request.session_id,
    )
    # Contract: no --flag after the prompt positional (Go flag semantics).
    assert_flags_before_prompt(argv, prompt)

    try:
        proc = _run_provider_argv(
            argv,
            mode="run",
            timeout_s=float(request.timeout_s),
            env=request.env,
            cancel_event=request.cancel_event,
            cwd=request.cwd,
            max_output_bytes=request.max_output_bytes or DEFAULT_RUN_MAX_OUTPUT_BYTES,
        )
    except ProbeProcessError as exc:
        raise ProviderRunError(
            f"failed to spawn agy run: {redact_text(str(exc))}"
        ) from exc

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    partial = bool(
        proc.timed_out
        or proc.cancelled
        or proc.overflow
        or proc.stdout_truncated
        or proc.stderr_truncated
    )

    events: tuple[ProviderRunEvent, ...] = ()
    usage: ProviderUsage | None = None
    meta: dict[str, Any] = {}
    output = stdout
    parse_error = False
    error_message = ""

    if fmt == "json":
        # Fail closed: empty/whitespace structured output is never success.
        if not stdout.strip():
            parse_error = True
            error_message = "empty json output"
            events = (
                ProviderRunEvent(
                    type="parse_error",
                    payload={},
                    raw=stdout,
                    index=0,
                    malformed=True,
                ),
            )
        else:
            try:
                output, events, usage, meta = parse_json_result(stdout)
            except ProviderRunError as exc:
                parse_error = True
                error_message = str(exc)
                output = stdout
                events = (
                    ProviderRunEvent(
                        type="parse_error",
                        payload={},
                        raw=stdout,
                        index=0,
                        malformed=True,
                    ),
                )
    elif fmt == "stream-json":
        if not stdout.strip():
            # Same fail-closed contract as json (even on returncode 0).
            parse_error = True
            error_message = "empty stream-json output"
            events = (
                ProviderRunEvent(
                    type="parse_error",
                    payload={},
                    raw=stdout,
                    index=0,
                    malformed=True,
                ),
            )
        else:
            output, events, usage, meta, had_malformed = parse_stream_json(
                stdout, truncated=proc.stdout_truncated or partial
            )
            if had_malformed:
                parse_error = True
                error_message = "malformed stream-json event(s)"
    elif fmt == "text":
        output = stdout

    has_valid_structured_result = (
        fmt in {"json", "stream-json"}
        and not parse_error
        and bool(events)
        and not any(e.malformed for e in events)
    )
    auth_blocked = _looks_auth_blocked(
        stdout,
        stderr,
        has_valid_structured_result=has_valid_structured_result,
        returncode=proc.returncode,
    )

    exit_class = _classify_exit(
        proc=proc, parse_error=parse_error, auth_blocked=auth_blocked
    )
    ok = (
        exit_class == "success"
        and not parse_error
        and not auth_blocked
        and not proc.timed_out
        and not proc.cancelled
    )
    retryable = exit_class in {"timeout", "cancelled", "overflow"} or (
        exit_class == "nonzero" and not auth_blocked
    )

    session_raw = meta.get("session_id") or request.session_id
    resume_raw = meta.get("resume_token")
    session_id_out = session_raw if isinstance(session_raw, str) else request.session_id
    resume_token_out = resume_raw if isinstance(resume_raw, str) else None
    # Resume is an execution-metadata capability of the CLI surface (#67-B);
    # Team receipt/pane ownership remains out of scope.
    resume_supported = True

    if not error_message and not ok:
        error_message = redact_text((stderr or stdout or exit_class).strip())[:500]

    return ProviderRunResult(
        ok=ok,
        exit_class=exit_class,
        returncode=int(proc.returncode),
        output=output,
        events=events,
        usage=usage,
        argv=tuple(proc.argv),
        stdout=stdout,
        stderr=stderr,
        timed_out=bool(proc.timed_out),
        cancelled=bool(proc.cancelled),
        partial_output=partial,
        retryable=retryable,
        session_id=session_id_out,
        resume_token=resume_token_out,
        resume_supported=resume_supported,
        overflow=bool(proc.overflow),
        stdout_truncated=bool(proc.stdout_truncated),
        stderr_truncated=bool(proc.stderr_truncated),
        error_message=error_message,
        artifacts=request.artifacts,
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

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        return run(request)


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
    "build_run_argv",
    "assert_flags_before_prompt",
    "classify_compat",
    "discover_binary",
    "doctor",
    "get_adapter",
    "parse_json_result",
    "parse_stream_json",
    "parse_version",
    "probe_capabilities",
    "probe_version",
    "run",
]
