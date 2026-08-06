"""Typed, JSON-serializable provider probe models (#67-A)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CompatStatus = Literal["compatible", "too_old", "too_new", "unknown"]

CAPABILITIES_SCHEMA = "omg-provider-capabilities/v1"


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
    """Golden capabilities envelope for one provider probe."""

    provider: str
    binary: str
    version: str
    version_tuple: tuple[int, int, int]
    compat_status: CompatStatus
    tested_min: str = "1.1.0"
    tested_max: str = "1.1.99"
    pin_revision: str = ""
    authenticated: bool | None = None  # None = not probed (slice A)
    live_call_ready: bool = False
    output_formats: tuple[str, ...] = ("text", "json", "stream-json")
    efforts: tuple[str, ...] = ("low", "medium", "high")
    modes: tuple[str, ...] = ("accept-edits", "plan")
    print_mode: bool = True
    sandbox: bool = True
    agents_subcommand: bool = True
    models_subcommand: bool = True
    plugins_subcommand: bool = True
    # Extension surfaces — not claimed live in #67-A
    background_tasks: bool = False
    hooks: bool = False
    skills: bool = False
    mcp: bool = False
    subagents: bool = False
    needs_pty: bool = True
    limitations: tuple[str, ...] = (
        "Slice A probe only; headless run/ask/Team cutover deferred (#67-B..D).",
        "Authentication and live-call readiness are not verified hermetically.",
    )
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


__all__ = [
    "CAPABILITIES_SCHEMA",
    "CompatStatus",
    "DoctorReport",
    "ProviderCapabilities",
    "VersionInfo",
]
