"""Typed Grok host probe models (#105 PR2 / sequence D).

Capability truth is *never* assumed from version alone when a higher-priority
source (behavior / ACP advertisement / CLI inspect) is present. Defaults are
fail-closed (capabilities false) until a probe observes otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

GateState = Literal["AVAILABLE", "LEGACY", "BLOCKED"]
HostCompatStatus = Literal[
    "compatible",
    "legacy",
    "too_old",
    "too_new",
    "unknown",
    "incompatible",
]
CapabilityTruthSource = Literal[
    "behavior",
    "advertisement",
    "inspect",
    "version",
    "none",
]

HOST_CAPABILITIES_SCHEMA = "omg-host-capabilities/v1"

# Tested host window for OMG doctor (pin ≠ forced minimum).
TESTED_MIN_STR = "0.2.107"
TESTED_MAX_STR = "0.2.121"
TESTED_MIN = (0, 2, 107)
TESTED_MAX = (0, 2, 121)
# First public line that advertises ACP resume/close in release notes.
MODERN_CAPS_MIN = (0, 2, 121)

CAPABILITY_KEYS = (
    "session_resume",
    "session_close",
    "restore_code_explicit",
    "uuid_search",
)


@dataclass(frozen=True, slots=True)
class FeatureGateResult:
    """Three-state gate for one host capability."""

    capability: str
    state: GateState
    reason: str
    next_action: str | None = None
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "capability": self.capability,
            "state": self.state,
            "reason": self.reason,
            "required": self.required,
        }
        if self.next_action:
            out["next_action"] = self.next_action
        return out


@dataclass(frozen=True, slots=True)
class HostCapabilitySet:
    """Bounded, redaction-safe host capability inventory."""

    session_resume: bool = False
    session_close: bool = False
    restore_code_explicit: bool = False
    uuid_search: bool = False
    sources: dict[str, CapabilityTruthSource] = field(default_factory=dict)

    def get(self, key: str) -> bool:
        if key not in CAPABILITY_KEYS:
            raise KeyError(key)
        return bool(getattr(self, key))

    def source_for(self, key: str) -> CapabilityTruthSource:
        raw = self.sources.get(key, "none")
        if raw not in ("behavior", "advertisement", "inspect", "version", "none"):
            return "none"
        return raw  # type: ignore[return-value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_resume": self.session_resume,
            "session_close": self.session_close,
            "restore_code_explicit": self.restore_code_explicit,
            "uuid_search": self.uuid_search,
            "sources": {k: self.source_for(k) for k in CAPABILITY_KEYS},
        }


@dataclass(frozen=True, slots=True)
class HostProbeReport:
    """Canonical result of :func:`omg_cli.host_probe.probe_host`."""

    binary: str
    version: str | None
    version_tuple: tuple[int, int, int] | None
    tested_min: str
    tested_max: str
    compatibility: HostCompatStatus
    capabilities: HostCapabilitySet
    observations: tuple[str, ...] = ()
    gates: tuple[FeatureGateResult, ...] = ()
    binary_found: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable host block for ``omg doctor --json``.

        Never includes session ids, auth, transcripts, cwd, or home paths.
        """
        caps = self.capabilities.to_dict()
        # Flatten booleans for the compact doctor envelope (plan shape).
        flat_caps = {
            "session_resume": caps["session_resume"],
            "session_close": caps["session_close"],
            "restore_code_explicit": caps["restore_code_explicit"],
            "uuid_search": caps["uuid_search"],
        }
        return {
            "schema": HOST_CAPABILITIES_SCHEMA,
            "binary": self.binary,
            "binary_found": self.binary_found,
            "version": self.version,
            "version_tuple": (
                list(self.version_tuple) if self.version_tuple is not None else None
            ),
            "tested_min": self.tested_min,
            "tested_max": self.tested_max,
            "compatibility": self.compatibility,
            "capabilities": flat_caps,
            "capability_sources": caps["sources"],
            "gates": {g.capability: g.to_dict() for g in self.gates},
            "observations": list(self.observations),
        }


__all__ = [
    "CAPABILITY_KEYS",
    "CapabilityTruthSource",
    "FeatureGateResult",
    "GateState",
    "HOST_CAPABILITIES_SCHEMA",
    "HostCapabilitySet",
    "HostCompatStatus",
    "HostProbeReport",
    "MODERN_CAPS_MIN",
    "TESTED_MAX",
    "TESTED_MAX_STR",
    "TESTED_MIN",
    "TESTED_MIN_STR",
]
