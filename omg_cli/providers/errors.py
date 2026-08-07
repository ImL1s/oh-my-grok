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


class ProviderRunError(ProviderError, RuntimeError):
    """Headless run request invalid or spawn contract failed before a result.

    Timeout/cancel/nonzero exit return a :class:`ProviderRunResult` instead —
    this error is for bad requests, missing binary, or unlaunchable argv.
    """


__all__ = [
    "ProviderBinaryMissing",
    "ProviderError",
    "ProviderProbeError",
    "ProviderRunError",
    "ProviderVersionError",
]
