"""Provider adapter Protocol — probe + headless run + launch envelope (#67-A–D).

Ask routes ``agy`` through :meth:`ProviderAdapter.run` (#67-C). Team panes use
:meth:`ProviderAdapter.build_launch_envelope` (#67-D) — never
:meth:`ProviderAdapter.run` for interactive PTY workers. Supervisor retains
spawn / PID / PGID / readiness / nonce authority.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omg_cli.providers.models import (
    DoctorReport,
    ProviderCapabilities,
    ProviderLaunchEnvelope,
    ProviderLaunchRequest,
    ProviderRunRequest,
    ProviderRunResult,
    VersionInfo,
)


@runtime_checkable
class ProviderAdapter(Protocol):
    """Probe + headless run + generated launch envelope (no Team spawn APIs)."""

    name: str

    def discover_binary(self) -> str: ...

    def probe_version(self, binary: str | None = None) -> VersionInfo: ...

    def probe_capabilities(self, binary: str | None = None) -> ProviderCapabilities: ...

    def doctor(self, *, strict: bool = False) -> DoctorReport: ...

    def run(self, request: ProviderRunRequest) -> ProviderRunResult: ...

    def build_launch_envelope(
        self, request: ProviderLaunchRequest
    ) -> ProviderLaunchEnvelope: ...


__all__ = ["ProviderAdapter"]
