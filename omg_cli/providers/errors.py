"""Provider adapter errors (fail-closed)."""

from __future__ import annotations


class ProviderError(Exception):
    """Base error for omg provider adapters."""


class ProviderBinaryMissing(ProviderError, FileNotFoundError):
    """Provider binary not found on PATH / override."""


class ProviderVersionError(ProviderError, ValueError):
    """Version probe failed or output was unparseable."""


class ProviderProbeError(ProviderError, RuntimeError):
    """Capabilities / readiness probe failed."""


__all__ = [
    "ProviderBinaryMissing",
    "ProviderError",
    "ProviderProbeError",
    "ProviderVersionError",
]
