"""Provider adapter Protocol (future launch surface)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omg_cli.providers.models import DoctorReport, ProviderCapabilities, VersionInfo


@runtime_checkable
class ProviderAdapter(Protocol):
    """Minimal probe surface for slice A; run/ask/Team land in later slices."""

    name: str

    def discover_binary(self) -> str: ...

    def probe_version(self, binary: str | None = None) -> VersionInfo: ...

    def probe_capabilities(self, binary: str | None = None) -> ProviderCapabilities: ...

    def doctor(self, *, strict: bool = False) -> DoctorReport: ...


__all__ = ["ProviderAdapter"]
