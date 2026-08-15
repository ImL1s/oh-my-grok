"""Headless Grok job provider (#69) — ``ProviderAdapter.run`` via prompt-file.

Team panes keep using ``omg_cli.team.providers`` / interactive argv. This
adapter is **jobs-only**: ``grok --prompt-file --cwd --output-format plain``.
It never fabricates interactive TTY ownership and never writes ``verified``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Final, Mapping

from omg_cli.host_probe import parse_host_version
from omg_cli.providers.errors import (
    ProviderBinaryMissing,
    ProviderRunError,
    ProviderVersionError,
)
from omg_cli.providers.models import (
    DoctorReport,
    ProviderCapabilities,
    ProviderLaunchEnvelope,
    ProviderLaunchRequest,
    ProviderOutputFormat,
    ProviderRunRequest,
    ProviderRunResult,
    VersionInfo,
)
from omg_cli.providers.process import (
    DEFAULT_PROBE_TIMEOUT_S,
    DEFAULT_RUN_MAX_OUTPUT_BYTES,
    ProbeProcessError,
    run_probe_process,
    run_provider_process,
)
from omg_cli.redaction import redact_text

PROVIDER_NAME: Final[str] = "grok"
BINARY_NAME: Final[str] = "grok"
ENV_BIN_OVERRIDE: Final[str] = "OMG_GROK_BIN"
# Documented tested window for jobs (not a fail-closed pin).
PIN_REVISION: Final[str] = "unpinned"
_GROK_BASENAMES: Final[frozenset[str]] = frozenset({BINARY_NAME, f"{BINARY_NAME}.exe"})
_ALLOWED_OUTPUT: Final[frozenset[str]] = frozenset({"text", "json", "stream-json"})
_CLI_FORMAT: Final[Mapping[str, str]] = {
    "text": "plain",
    "json": "json",
    "stream-json": "jsonl",
}
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
        ENV_BIN_OVERRIDE,
        "FAKE_GROK_VERSION",
        "FAKE_GROK_VERSION_RC",
        "FAKE_GROK_RUN_RC",
        "FAKE_GROK_RUN_STDOUT",
        "FAKE_GROK_RUN_STDERR",
        "FAKE_GROK_ECHO_CWD",
        "FAKE_GROK_ECHO_PROMPT",
    }
)


def _bounded_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _BOUNDED_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if extra:
        for key, val in extra.items():
            if key in _BOUNDED_ENV_KEYS:
                env[key] = val
    return env


def _require_grok_basename(path: Path, *, source: str) -> None:
    if path.name not in _GROK_BASENAMES:
        raise ProviderBinaryMissing(
            f"{source} basename must be {BINARY_NAME!r}, got {path.name!r}"
        )


def discover_binary(*, env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    override = (source.get(ENV_BIN_OVERRIDE) or "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            _require_grok_basename(path, source=f"{ENV_BIN_OVERRIDE}={override!r}")
            return str(path)
        raise ProviderBinaryMissing(
            f"OMG_GROK_BIN={override!r} is not an executable file"
        )
    path_str = shutil.which(BINARY_NAME, path=source.get("PATH"))
    if not path_str:
        raise ProviderBinaryMissing(
            f"{BINARY_NAME!r} not found on PATH (set {ENV_BIN_OVERRIDE} to override)"
        )
    found = Path(path_str)
    _require_grok_basename(found, source="PATH")
    resolved = found.resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProviderBinaryMissing(
            f"{BINARY_NAME!r} on PATH is not an executable file after resolve"
        )
    # Keep the PATH entry name (`grok`); exec follows a symlink target.
    return str(found)


def probe_version(binary: str | None = None) -> VersionInfo:
    path = binary or discover_binary()
    try:
        proc = run_probe_process(
            [path, "--version"],
            env=_bounded_env(),
            timeout_s=DEFAULT_PROBE_TIMEOUT_S,
        )
    except ProbeProcessError as exc:
        raise ProviderVersionError(f"grok --version failed: {exc}") from exc
    text = (proc.stdout or proc.stderr or "").strip()
    parsed = parse_host_version(text)
    if proc.returncode != 0 or parsed is None:
        raise ProviderVersionError(
            f"unparseable grok --version (rc={proc.returncode})"
        )
    return VersionInfo(raw=text.splitlines()[0][:200], major=parsed[0], minor=parsed[1], patch=parsed[2])


def probe_capabilities(binary: str | None = None) -> ProviderCapabilities:
    path = binary or discover_binary()
    ver = probe_version(path)
    return ProviderCapabilities(
        provider=PROVIDER_NAME,
        binary=path,
        version=ver.raw,
        version_tuple=ver.as_tuple(),
        compat_status="compatible",
        authenticated=False,
        live_call_ready=False,
        output_formats=("text", "json"),
        print_mode=True,
        limitations=(
            "Jobs-only headless --prompt-file; not an interactive TTY owner.",
            "Authentication and live-call readiness are not verified hermetically.",
        ),
    )


def doctor(*, strict: bool = False) -> DoctorReport:
    del strict
    try:
        caps = probe_capabilities()
    except (ProviderBinaryMissing, ProviderVersionError) as exc:
        return DoctorReport(
            ok=False,
            exit_code=1,
            checks=(f"FAIL: grok job provider ({exc})",),
        )
    return DoctorReport(
        ok=True,
        exit_code=0,
        checks=("OK: grok job provider binary+version",),
        capabilities=caps,
    )


def _map_output_format(fmt: str) -> str:
    if fmt not in _ALLOWED_OUTPUT:
        raise ProviderRunError(f"unsupported output_format: {fmt!r}")
    return _CLI_FORMAT[fmt]


def build_run_argv(
    binary: str,
    *,
    prompt_file: str,
    cwd: str,
    output_format: str = "text",
    model: str | None = None,
) -> list[str]:
    argv = [
        binary,
        "--prompt-file",
        prompt_file,
        "--cwd",
        cwd,
        "--output-format",
        _map_output_format(output_format),
    ]
    if model:
        argv.extend(["-m", str(model)])
    return argv


def run(request: ProviderRunRequest) -> ProviderRunResult:
    if not isinstance(request, ProviderRunRequest):
        raise ProviderRunError("request must be ProviderRunRequest")
    if request.timeout_s <= 0:
        raise ProviderRunError("timeout_s must be positive")
    fmt: ProviderOutputFormat = request.output_format  # type: ignore[assignment]
    prompt_file = (request.prompt_file or "").strip()
    if not prompt_file:
        raise ProviderRunError("grok jobs require prompt_file")
    try:
        path = request.binary or discover_binary()
    except ProviderBinaryMissing as exc:
        raise ProviderRunError(str(exc)) from exc
    _require_grok_basename(Path(path), source="binary")
    cwd = request.cwd or os.getcwd()
    argv = build_run_argv(
        path,
        prompt_file=prompt_file,
        cwd=str(cwd),
        output_format=str(fmt),
        model=request.model,
    )
    try:
        proc = run_provider_process(
            argv,
            env=_bounded_env(request.env),
            timeout_s=float(request.timeout_s),
            max_output_bytes=request.max_output_bytes or DEFAULT_RUN_MAX_OUTPUT_BYTES,
            cancel_event=request.cancel_event,
            on_process_started=request.on_process_started,
            cwd=cwd,
            mode="run",
        )
    except ProbeProcessError as exc:
        raise ProviderRunError(
            f"failed to spawn grok run: {redact_text(str(exc))}"
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
    ok = proc.returncode == 0 and not proc.timed_out and not proc.cancelled
    if proc.timed_out:
        exit_class = "timeout"
    elif proc.cancelled:
        exit_class = "cancelled"
    elif proc.overflow:
        exit_class = "overflow"
    elif proc.returncode == 0:
        exit_class = "success"
    else:
        exit_class = "nonzero"
    error_message = ""
    if not ok:
        error_message = redact_text((stderr or stdout or exit_class).strip())[:500]
    return ProviderRunResult(
        ok=ok,
        exit_class=exit_class,
        returncode=int(proc.returncode),
        output=stdout,
        argv=tuple(proc.argv),
        stdout=stdout,
        stderr=stderr,
        timed_out=bool(proc.timed_out),
        cancelled=bool(proc.cancelled),
        partial_output=partial,
        retryable=exit_class in {"timeout", "cancelled", "overflow", "nonzero"},
        overflow=bool(proc.overflow),
        stdout_truncated=bool(proc.stdout_truncated),
        stderr_truncated=bool(proc.stderr_truncated),
        error_message=error_message,
        artifacts=request.artifacts,
    )


class GrokJobProvider:
    """:class:`~omg_cli.providers.base.ProviderAdapter` for headless grok jobs."""

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

    def build_launch_envelope(
        self, request: ProviderLaunchRequest
    ) -> ProviderLaunchEnvelope:
        del request
        raise RuntimeError("GrokJobProvider does not support Team launch envelopes")


def get_adapter() -> GrokJobProvider:
    return GrokJobProvider()


__all__ = [
    "BINARY_NAME",
    "ENV_BIN_OVERRIDE",
    "PIN_REVISION",
    "PROVIDER_NAME",
    "GrokJobProvider",
    "build_run_argv",
    "discover_binary",
    "doctor",
    "get_adapter",
    "probe_capabilities",
    "probe_version",
    "run",
]
