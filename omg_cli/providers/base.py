"""Provider adapter Protocol — probe + headless run (#67-A/B).

Ask / Team routing still lands in later slices; all future consumers must
call :meth:`ProviderAdapter.run` rather than inventing parallel launchers.
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
