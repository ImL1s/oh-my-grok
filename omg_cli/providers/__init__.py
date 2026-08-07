"""omg provider adapters — typed probe/launch surface (Antigravity first, #67).

Slice A: discovery + capabilities + doctor.
Slice B: headless ``ProviderAdapter.run`` (json/stream-json).
Slice C: ``omg ask agy`` via ``ProviderAdapter.run``.
Slice D: Team panes via adapter-owned ``build_launch_envelope`` (supervisor
still owns spawn/PTY/PID/readiness — never ``Adapter.run`` for Team).
"""

from __future__ import annotations

from omg_cli.providers import antigravity as antigravity
from omg_cli.providers.base import ProviderAdapter
from omg_cli.providers.errors import (
    ProviderBinaryMissing,
    ProviderError,
    ProviderProbeError,
    ProviderRunError,
    ProviderVersionError,
)
from omg_cli.providers.models import (
    CAPABILITIES_SCHEMA,
    LAUNCH_ENVELOPE_SCHEMA,
    RUN_RESULT_SCHEMA,
    CompatStatus,
    DoctorReport,
    ProviderArtifactRef,
    ProviderCapabilities,
    ProviderExitClass,
    ProviderLaunchEnvelope,
    ProviderLaunchKind,
    ProviderLaunchRequest,
    ProviderOutputFormat,
    ProviderRunEvent,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderUsage,
    VersionInfo,
)
from omg_cli.providers.process import (
    ProbeProcessResult,
    ProviderProcessResult,
    run_probe_process,
    run_provider_process,
)

__all__ = [
    "CAPABILITIES_SCHEMA",
    "LAUNCH_ENVELOPE_SCHEMA",
    "RUN_RESULT_SCHEMA",
    "CompatStatus",
    "DoctorReport",
    "ProbeProcessResult",
    "ProviderAdapter",
    "ProviderArtifactRef",
    "ProviderBinaryMissing",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderExitClass",
    "ProviderLaunchEnvelope",
    "ProviderLaunchKind",
    "ProviderLaunchRequest",
    "ProviderOutputFormat",
    "ProviderProbeError",
    "ProviderProcessResult",
    "ProviderRunError",
    "ProviderRunEvent",
    "ProviderRunRequest",
    "ProviderRunResult",
    "ProviderUsage",
    "ProviderVersionError",
    "VersionInfo",
    "antigravity",
    "run_probe_process",
    "run_provider_process",
]
