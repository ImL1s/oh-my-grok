"""Versioned dual-host capability registry (#131).

Single source of capability identifiers and negotiation outcomes. Stock
original Grok Build is the product baseline — that is not inferred from an
executable name. Medley capabilities are never auto-detected from PATH,
branding, or state directories.

Only ``supported`` authorizes use. Missing Medley capabilities on stock Grok
Build are ``unsupported``, not installation failures. Claimed-but-missing is
``unavailable``. Discovery performs no paid inference request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SCHEMA = "omg-host-capability-registry/v1"

CAPABILITY_IDS: tuple[str, ...] = (
    "host.native-agent.v1",
    "host.native-exact-model.v1",
    "host.native-inherit-model.v1",
    "medley.native-ordered-candidates.v1",
    "medley.native-route-receipt.v1",
    "medley.native-model-family-metadata.v1",
    "medley.native-replay-safe-fallback.v1",
)

HOST_BASELINE_IDS: frozenset[str] = frozenset(
    {
        "host.native-agent.v1",
        "host.native-inherit-model.v1",
    }
)
HOST_OPTIONAL_IDS: frozenset[str] = frozenset({"host.native-exact-model.v1"})
MEDLEY_CAPABILITY_IDS: frozenset[str] = frozenset(
    cid for cid in CAPABILITY_IDS if cid.startswith("medley.")
)

STATES: tuple[str, ...] = (
    "supported",
    "unsupported",
    "unavailable",
    "incompatible",
    "unknown",
)

HOST_TIER_GROK = "original_grok_build"
HOST_TIER_MEDLEY = "medley"
HOST_TIER_UNKNOWN = "unknown"
HOST_TIERS: frozenset[str] = frozenset(
    {HOST_TIER_GROK, HOST_TIER_MEDLEY, HOST_TIER_UNKNOWN}
)

CURRENT_VERSION = "v1"
_KNOWN_VERSIONS: frozenset[str] = frozenset({"v1", "1", CURRENT_VERSION})

ADVERTISED_CLAIM = "claimed"
ADVERTISED_MISSING = "missing"
ADVERTISED_UNKNOWN = "unknown"
ADVERTISED_UNSUPPORTED = "unsupported"
KNOWN_VERSIONS: frozenset[str] = _KNOWN_VERSIONS


class HostCapabilityError(ValueError):
    """Fail-closed capability negotiation error."""


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """One negotiated capability outcome."""

    capability_id: str
    state: str
    version: str | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "state": self.state,
            "version": self.version,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class HostCapabilitySnapshot:
    """Full registry snapshot for one explicit host tier."""

    schema: str
    host_tier: str
    capabilities: tuple[CapabilityState, ...]

    def by_id(self) -> dict[str, CapabilityState]:
        return {item.capability_id: item for item in self.capabilities}

    def state_of(self, capability_id: str) -> str:
        row = self.by_id().get(capability_id)
        if row is None:
            return "unknown"
        return row.state

    def is_supported(self, capability_id: str) -> bool:
        return self.state_of(capability_id) == "supported"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "host_tier": self.host_tier,
            "capabilities": [item.to_json() for item in self.capabilities],
        }


def stock_grok_snapshot() -> HostCapabilitySnapshot:
    """OMG product baseline: original Grok Build, Medley absent.

    Callers must pass this explicitly. Do not derive the tier from a binary
    name, branding string, or filesystem path.
    """
    return negotiate(host_tier=HOST_TIER_GROK, advertised=None)


def negotiate(
    *,
    host_tier: str,
    advertised: Mapping[str, Any] | None = None,
) -> HostCapabilitySnapshot:
    """Negotiate versioned capabilities for an explicit host tier.

    ``advertised`` maps capability id → version string, ``claimed``,
    ``missing``, or ``unknown``. Unknown ids in advertised are ignored for
    authorization (they cannot become supported). Empty advertised is treated
    as none.
    """
    if host_tier not in HOST_TIERS:
        raise HostCapabilityError(
            f"unknown host_tier {host_tier!r}; expected one of "
            f"{sorted(HOST_TIERS)}"
        )
    claims = _normalize_advertised(advertised)
    rows: list[CapabilityState] = []
    for capability_id in CAPABILITY_IDS:
        rows.append(
            _negotiate_one(
                host_tier=host_tier,
                capability_id=capability_id,
                advertised=claims.get(capability_id),
            )
        )
    return HostCapabilitySnapshot(
        schema=SCHEMA,
        host_tier=host_tier,
        capabilities=tuple(rows),
    )


def _normalize_advertised(
    advertised: Mapping[str, Any] | None,
) -> dict[str, str]:
    if advertised is None:
        return {}
    if not isinstance(advertised, Mapping):
        raise HostCapabilityError("advertised capabilities must be a mapping")
    out: dict[str, str] = {}
    for key, value in advertised.items():
        if not isinstance(key, str) or not key.strip():
            raise HostCapabilityError("advertised capability id must be a string")
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise HostCapabilityError(
                f"advertised {key!r} version must be a non-empty string"
            )
        out[key.strip()] = value.strip()
    return out


def _negotiate_one(
    *,
    host_tier: str,
    capability_id: str,
    advertised: str | None,
) -> CapabilityState:
    if host_tier == HOST_TIER_UNKNOWN:
        return CapabilityState(
            capability_id=capability_id,
            state="unknown",
            version=None,
            reason="host tier is unknown; do not infer support",
        )
    if advertised == ADVERTISED_MISSING:
        return CapabilityState(
            capability_id=capability_id,
            state="unavailable",
            version=None,
            reason="host claimed this capability but runtime evidence is missing",
        )
    if advertised == ADVERTISED_UNKNOWN:
        return CapabilityState(
            capability_id=capability_id,
            state="unknown",
            version=None,
            reason="host reported this capability as unknown",
        )
    if advertised == ADVERTISED_UNSUPPORTED:
        return CapabilityState(
            capability_id=capability_id,
            state="unsupported",
            version=None,
            reason="host reported this capability as unsupported",
        )
    if advertised is not None and advertised not in {ADVERTISED_CLAIM} | _KNOWN_VERSIONS:
        return CapabilityState(
            capability_id=capability_id,
            state="incompatible",
            version=advertised,
            reason=f"unsupported capability version {advertised!r}",
        )

    claimed = advertised is not None
    is_medley = capability_id in MEDLEY_CAPABILITY_IDS
    is_baseline = capability_id in HOST_BASELINE_IDS
    is_optional_host = capability_id in HOST_OPTIONAL_IDS

    if host_tier == HOST_TIER_GROK:
        if is_medley:
            if claimed:
                return CapabilityState(
                    capability_id=capability_id,
                    state="unavailable",
                    version=_version_or_none(advertised),
                    reason=(
                        "stock original Grok Build claimed a Medley capability "
                        "without Medley host evidence"
                    ),
                )
            return CapabilityState(
                capability_id=capability_id,
                state="unsupported",
                version=None,
                reason="optional Medley extension; not exposed by original Grok Build",
            )
        if is_baseline:
            return CapabilityState(
                capability_id=capability_id,
                state="supported",
                version=CURRENT_VERSION,
                reason="original Grok Build baseline",
            )
        if is_optional_host:
            if claimed:
                return CapabilityState(
                    capability_id=capability_id,
                    state="supported",
                    version=CURRENT_VERSION,
                    reason="host advertised a safe exact-model contract",
                )
            return CapabilityState(
                capability_id=capability_id,
                state="unsupported",
                version=None,
                reason=(
                    "stock Grok Build does not expose a safe exact-model contract"
                ),
            )

    if host_tier == HOST_TIER_MEDLEY:
        if not claimed:
            if is_baseline:
                return CapabilityState(
                    capability_id=capability_id,
                    state="supported",
                    version=CURRENT_VERSION,
                    reason="Medley remains a Grok-compatible baseline host",
                )
            return CapabilityState(
                capability_id=capability_id,
                state="unsupported",
                version=None,
                reason="Medley host did not advertise this capability",
            )
        return CapabilityState(
            capability_id=capability_id,
            state="supported",
            version=CURRENT_VERSION,
            reason="Medley advertised this capability",
        )

    return CapabilityState(
        capability_id=capability_id,
        state="unknown",
        version=None,
        reason="unhandled host tier",
    )


def _version_or_none(advertised: str | None) -> str | None:
    if advertised is None or advertised == ADVERTISED_CLAIM:
        return CURRENT_VERSION
    if advertised in _KNOWN_VERSIONS:
        return CURRENT_VERSION
    return advertised


def medley_capability_outcome(snapshot: HostCapabilitySnapshot) -> str:
    """Aggregate Medley-only *capability* outcome (not route-specific facts)."""
    states = [
        snapshot.state_of(cid) for cid in sorted(MEDLEY_CAPABILITY_IDS)
    ]
    if all(state == "unsupported" for state in states):
        return "unsupported"
    if any(state == "unavailable" for state in states):
        return "unavailable"
    if any(state == "incompatible" for state in states):
        return "incompatible"
    if any(state == "unknown" for state in states):
        return "unknown"
    if any(state == "supported" for state in states):
        return "supported"
    return "unsupported"


def route_specific_facts_state(snapshot: HostCapabilitySnapshot) -> str:
    """Receipts / ordered candidates / access facts — never fabricated."""
    if snapshot.is_supported("medley.native-route-receipt.v1"):
        return "supported"
    if snapshot.state_of("medley.native-route-receipt.v1") == "unavailable":
        return "unavailable"
    if snapshot.state_of("medley.native-route-receipt.v1") == "incompatible":
        return "incompatible"
    if snapshot.state_of("medley.native-route-receipt.v1") == "unknown":
        return "unknown"
    if snapshot.host_tier == HOST_TIER_UNKNOWN:
        return "unknown"
    return "unavailable"


__all__ = [
    "ADVERTISED_CLAIM",
    "ADVERTISED_MISSING",
    "ADVERTISED_UNKNOWN",
    "ADVERTISED_UNSUPPORTED",
    "CAPABILITY_IDS",
    "CURRENT_VERSION",
    "CapabilityState",
    "HOST_BASELINE_IDS",
    "HOST_OPTIONAL_IDS",
    "HOST_TIER_GROK",
    "HOST_TIER_MEDLEY",
    "HOST_TIER_UNKNOWN",
    "HOST_TIERS",
    "HostCapabilityError",
    "HostCapabilitySnapshot",
    "KNOWN_VERSIONS",
    "MEDLEY_CAPABILITY_IDS",
    "SCHEMA",
    "STATES",
    "medley_capability_outcome",
    "negotiate",
    "route_specific_facts_state",
    "stock_grok_snapshot",
]
