"""Provider adapter Protocol — probe + headless run (#67-A/B/C).

Ask routes ``agy`` through :meth:`ProviderAdapter.run` (#67-C). Team routing
still lands in #67-D; all Antigravity launches must call this surface rather
than inventing parallel launchers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omg_cli.providers.models import (
    DoctorReport,
    ProviderCapabilities,
    ProviderRunRequest,
    ProviderRunResult,
    VersionInfo,
)


@runtime_checkable
class ProviderAdapter(Protocol):
    """Probe + unique headless execution surface (no ask/Team-specific APIs)."""

    name: str

    def discover_binary(self) -> str: ...

    def probe_version(self, binary: str | None = None) -> VersionInfo: ...

    def probe_capabilities(self, binary: str | None = None) -> ProviderCapabilities: ...

    def doctor(self, *, strict: bool = False) -> DoctorReport: ...

    def run(self, request: ProviderRunRequest) -> ProviderRunResult: ...


__all__ = ["ProviderAdapter"]
