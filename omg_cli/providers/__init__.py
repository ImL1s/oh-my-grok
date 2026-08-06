"""omg provider adapters — typed probe/launch surface (Antigravity first, #67).

Slice A exposes discovery + capabilities + doctor only. Ask/Team cutover and
headless ``run`` land in later slices.
"""

from __future__ import annotations

from omg_cli.providers import antigravity as antigravity
from omg_cli.providers.base import ProviderAdapter
from omg_cli.providers.errors import (
    ProviderBinaryMissing,
    ProviderError,
    ProviderProbeError,
    ProviderVersionError,
)
from omg_cli.providers.models import (
    CAPABILITIES_SCHEMA,
    CompatStatus,
    DoctorReport,
    ProviderCapabilities,
    VersionInfo,
)

__all__ = [
    "CAPABILITIES_SCHEMA",
    "CompatStatus",
    "DoctorReport",
    "ProviderAdapter",
    "ProviderBinaryMissing",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderProbeError",
    "ProviderVersionError",
    "VersionInfo",
    "antigravity",
]
