"""Typed, JSON-serializable provider models (#67-A/B probe+run, #67-D launch).

Provider-neutral defaults are empty / false / unknown — never Antigravity-positive
claims. Per-provider adapters must fill observed fields explicitly.

Run contracts are provider-neutral execution metadata only — no Team state,
receipts, pane ownership, or worker mapping.

Launch envelopes (#67-D) describe argv/env for interactive Team panes; they do
**not** spawn processes. Supervisor retains PID/PGID/PTY/readiness authority.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

# Non-serializable spawn observer: invoked once after successful Popen.
# Signature receives the live Popen handle (pid/pgid available).
ProcessStartedCallback = Callable[[Any], None]

CompatStatus = Literal["compatible", "too_old", "too_new", "unknown"]

ProviderOutputFormat = Literal["text", "json", "stream-json"]

ProviderExitClass = Literal[
    "success",
    "nonzero",
    "timeout",
    "cancelled",
    "parse_error",
    "spawn_error",
    "overflow",
    "auth_blocked",
    "unknown",
]

# Team interactive vs future kinds — headless stays on ProviderRunRequest.
ProviderLaunchKind = Literal["team"]

CAPABILITIES_SCHEMA = "omg-provider-capabilities/v1"
RUN_RESULT_SCHEMA = "omg-provider-run-result/v1"
LAUNCH_ENVELOPE_SCHEMA = "omg-provider-launch-envelope/v1"


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Parsed provider version."""

    raw: str
    major: int
    minor: int
    patch: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Golden capabilities envelope for one provider probe.

    Defaults are provider-neutral (empty / false). Antigravity-specific ranges,
    formats, and limitations belong in the Antigravity adapter only.
    """

    provider: str
    binary: str
    version: str
    version_tuple: tuple[int, int, int]
    compat_status: CompatStatus
    tested_min: str = ""
    tested_max: str = ""
    pin_revision: str = ""
    authenticated: bool | None = None  # None = not probed (slice A)
    live_call_ready: bool = False
    output_formats: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    print_mode: bool = False
    sandbox: bool = False
    agents_subcommand: bool = False
    models_subcommand: bool = False
    plugins_subcommand: bool = False
    # Extension surfaces — not claimed live in #67-A
    background_tasks: bool = False
    hooks: bool = False
    skills: bool = False
    mcp: bool = False
    subagents: bool = False
    needs_pty: bool = False
    limitations: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        compatible = self.compat_status == "compatible"
        installed = bool(self.binary)
        return {
            "schema": CAPABILITIES_SCHEMA,
            "provider": self.provider,
            "binary": self.binary,
            "version": self.version,
            "version_tuple": list(self.version_tuple),
            "compat": {
                "status": self.compat_status,
                "tested_min": self.tested_min,
                "tested_max": self.tested_max,
                "pin_revision": self.pin_revision,
            },
            "ready": {
                "installed": installed,
                "authenticated": self.authenticated,
                "compatible": compatible,
                "live_call_ready": bool(self.live_call_ready),
            },
            "supports": {
                "output_formats": list(self.output_formats),
                "efforts": list(self.efforts),
                "modes": list(self.modes),
                "print_mode": self.print_mode,
                "sandbox": self.sandbox,
                "agents": self.agents_subcommand,
                "models": self.models_subcommand,
                "plugins": self.plugins_subcommand,
                "background_tasks": self.background_tasks,
                "hooks": self.hooks,
                "skills": self.skills,
                "mcp": self.mcp,
                "subagents": self.subagents,
            },
            "platform": {
                "needs_pty": self.needs_pty,
                "limitations": list(self.limitations),
            },
            "probe": {
                "version_argv": ["--version"],
                "help_argv": ["--help"],
                "destructive": False,
            },
            **({} if not self.extra else {"extra": dict(self.extra)}),
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Result of ``provider … doctor``."""

    ok: bool
    exit_code: int
    checks: tuple[str, ...]
    capabilities: ProviderCapabilities | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "checks": list(self.checks),
        }
        if self.capabilities is not None:
            out["capabilities"] = self.capabilities.to_dict()
        return out


@dataclass(frozen=True, slots=True)
class ProviderArtifactRef:
    """Descriptor for large prompt/result content stored outside argv."""

    path: str
    kind: str = "prompt"  # prompt | result | other
    media_type: str = "text/plain"
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "media_type": self.media_type,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Token / cost usage when the provider emits it."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.extra:
            out["extra"] = dict(self.extra)
        return out


@dataclass(frozen=True, slots=True)
class ProviderRunEvent:
    """One ordered structured event (stream-json line or json payload)."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    index: int = 0
    malformed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "payload": dict(self.payload),
            "raw": self.raw,
            "index": self.index,
            "malformed": self.malformed,
        }


@dataclass(frozen=True, slots=True)
class ProviderRunRequest:
    """Provider-neutral headless execution request (no Team coupling)."""

    prompt: str = ""
    prompt_file: str | None = None
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    timeout_s: float = 120.0
    cancel_event: threading.Event | None = None
    on_process_started: ProcessStartedCallback | None = None
    output_format: ProviderOutputFormat = "text"
    session_id: str | None = None
    resume_id: str | None = None
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    artifacts: tuple[ProviderArtifactRef, ...] = ()
    max_output_bytes: int | None = None
    binary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize request metadata (prompt truncated; no cancel_event/observer)."""
        prompt = self.prompt or ""
        return {
            "prompt_len": len(prompt),
            "prompt_preview": prompt[:120],
            "prompt_file": self.prompt_file,
            "cwd": self.cwd,
            "timeout_s": self.timeout_s,
            "output_format": self.output_format,
            "session_id": self.session_id,
            "resume_id": self.resume_id,
            "model": self.model,
            "effort": self.effort,
            "mode": self.mode,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "max_output_bytes": self.max_output_bytes,
            "binary": self.binary,
            "has_cancel_event": self.cancel_event is not None,
            "has_on_process_started": self.on_process_started is not None,
            "env_keys": sorted(self.env.keys()) if self.env else [],
        }


@dataclass(frozen=True, slots=True)
class ProviderRunResult:
    """Normalized headless run outcome; preserves partial output on timeout/cancel."""

    ok: bool
    exit_class: ProviderExitClass
    returncode: int
    output: str
    events: tuple[ProviderRunEvent, ...] = ()
    usage: ProviderUsage | None = None
    argv: tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    partial_output: bool = False
    retryable: bool = False
    session_id: str | None = None
    resume_token: str | None = None
    resume_supported: bool = False
    overflow: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error_message: str = ""
    artifacts: tuple[ProviderArtifactRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": RUN_RESULT_SCHEMA,
            "ok": self.ok,
            "exit_class": self.exit_class,
            "returncode": self.returncode,
            "output": self.output,
            "events": [e.to_dict() for e in self.events],
            "usage": self.usage.to_dict() if self.usage is not None else None,
            "argv": list(self.argv),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "partial_output": self.partial_output,
            "retryable": self.retryable,
            "session": {
                "session_id": self.session_id,
                "resume_token": self.resume_token,
                "resume_supported": self.resume_supported,
            },
            "overflow": self.overflow,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "error_message": self.error_message,
            "artifacts": [a.to_dict() for a in self.artifacts],
        }
        return out


@dataclass(frozen=True, slots=True)
class ProviderLaunchRequest:
    """Adapter-owned launch input for interactive panes (no spawn / no Team IDs).

    Team Antigravity (#67-D) uses ``launch_kind="team"`` with a prompt-file
    *path placeholder* (body substituted by the supervisor/pane layer). This is
    distinct from headless :class:`ProviderRunRequest` / :meth:`ProviderAdapter.run`.
    """

    provider: str = ""
    launch_kind: ProviderLaunchKind = "team"
    prompt_file: str | None = None
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    needs_pty: bool = True
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    posture: str | None = None  # "read-only" | "read-write"
    session_id: str | None = None
    resume_id: str | None = None
    binary: str | None = None
    artifacts: tuple[ProviderArtifactRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "launch_kind": self.launch_kind,
            "prompt_file": self.prompt_file,
            "cwd": self.cwd,
            "needs_pty": self.needs_pty,
            "model": self.model,
            "effort": self.effort,
            "mode": self.mode,
            "posture": self.posture,
            "session_id": self.session_id,
            "resume_id": self.resume_id,
            "binary": self.binary,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "env_keys": sorted(self.env.keys()) if self.env else [],
        }


@dataclass(frozen=True, slots=True)
class ProviderLaunchEnvelope:
    """Generated launch plan: argv array only (never a shell string).

    Supervisor consumes this to spawn; the adapter never calls
    :func:`run_provider_process` for interactive Team panes.
    """

    provider: str
    argv: tuple[str, ...]
    needs_pty: bool
    prompt_delivery: str
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    identity_basenames: tuple[str, ...] = ()
    startup_strategy: str = "supervisor"
    provider_strategy: str = ""
    posture: str | None = None
    binary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": LAUNCH_ENVELOPE_SCHEMA,
            "provider": self.provider,
            "argv": list(self.argv),
            "needs_pty": self.needs_pty,
            "prompt_delivery": self.prompt_delivery,
            "cwd": self.cwd,
            "env": dict(self.env),
            "identity_basenames": list(self.identity_basenames),
            "startup_strategy": self.startup_strategy,
            "provider_strategy": self.provider_strategy,
            "posture": self.posture,
            "binary": self.binary,
        }


__all__ = [
    "CAPABILITIES_SCHEMA",
    "CompatStatus",
    "DoctorReport",
    "LAUNCH_ENVELOPE_SCHEMA",
    "ProviderArtifactRef",
    "ProviderCapabilities",
    "ProviderExitClass",
    "ProviderLaunchEnvelope",
    "ProviderLaunchKind",
    "ProviderLaunchRequest",
    "ProviderOutputFormat",
    "ProcessStartedCallback",
    "ProviderRunEvent",
    "ProviderRunRequest",
    "ProviderRunResult",
    "ProviderUsage",
    "RUN_RESULT_SCHEMA",
    "VersionInfo",
]
