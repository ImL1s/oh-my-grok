"""Dual-host capability registry (#131)."""

from __future__ import annotations

import pytest

from omg_cli.host_capabilities import (
    CAPABILITY_IDS,
    HOST_TIER_GROK,
    HOST_TIER_MEDLEY,
    HOST_TIER_UNKNOWN,
    HostCapabilityError,
    MEDLEY_CAPABILITY_IDS,
    SCHEMA,
    STATES,
    medley_capability_outcome,
    negotiate,
    route_specific_facts_state,
    stock_grok_snapshot,
)


def test_capability_ids_are_single_source() -> None:
    assert CAPABILITY_IDS == (
        "host.native-agent.v1",
        "host.native-exact-model.v1",
        "host.native-inherit-model.v1",
        "medley.native-ordered-candidates.v1",
        "medley.native-route-receipt.v1",
        "medley.native-model-family-metadata.v1",
        "medley.native-replay-safe-fallback.v1",
    )
    assert SCHEMA == "omg-host-capability-registry/v1"
    assert set(STATES) == {
        "supported",
        "unsupported",
        "unavailable",
        "incompatible",
        "unknown",
    }


def test_stock_grok_medley_caps_are_unsupported_not_failed() -> None:
    snap = stock_grok_snapshot()
    assert snap.host_tier == HOST_TIER_GROK
    assert snap.is_supported("host.native-agent.v1")
    assert snap.is_supported("host.native-inherit-model.v1")
    assert snap.state_of("host.native-exact-model.v1") == "unsupported"
    for cid in MEDLEY_CAPABILITY_IDS:
        assert snap.state_of(cid) == "unsupported"
    assert medley_capability_outcome(snap) == "unsupported"
    assert route_specific_facts_state(snap) == "unavailable"


def test_unknown_host_never_counts_as_supported() -> None:
    snap = negotiate(host_tier=HOST_TIER_UNKNOWN)
    for cid in CAPABILITY_IDS:
        assert snap.state_of(cid) == "unknown"
        assert not snap.is_supported(cid)


def test_incompatible_version_is_not_authorized() -> None:
    snap = negotiate(
        host_tier=HOST_TIER_MEDLEY,
        advertised={"medley.native-route-receipt.v1": "v99"},
    )
    assert snap.state_of("medley.native-route-receipt.v1") == "incompatible"
    assert not snap.is_supported("medley.native-route-receipt.v1")


def test_advertised_unknown_is_not_unsupported() -> None:
    snap = negotiate(
        host_tier=HOST_TIER_MEDLEY,
        advertised={"medley.native-route-receipt.v1": "unknown"},
    )
    assert snap.state_of("medley.native-route-receipt.v1") == "unknown"
    assert not snap.is_supported("medley.native-route-receipt.v1")


def test_claimed_but_missing_is_unavailable() -> None:
    snap = negotiate(
        host_tier=HOST_TIER_GROK,
        advertised={"medley.native-route-receipt.v1": "missing"},
    )
    assert snap.state_of("medley.native-route-receipt.v1") == "unavailable"
    assert medley_capability_outcome(snap) == "unavailable"


def test_stock_grok_claiming_medley_is_unavailable() -> None:
    snap = negotiate(
        host_tier=HOST_TIER_GROK,
        advertised={"medley.native-ordered-candidates.v1": "v1"},
    )
    assert snap.state_of("medley.native-ordered-candidates.v1") == "unavailable"


def test_medley_advertised_exact_and_candidates_supported() -> None:
    snap = negotiate(
        host_tier=HOST_TIER_MEDLEY,
        advertised={
            "host.native-exact-model.v1": "v1",
            "medley.native-ordered-candidates.v1": "v1",
            "medley.native-route-receipt.v1": "v1",
        },
    )
    assert snap.is_supported("host.native-agent.v1")
    assert snap.is_supported("host.native-exact-model.v1")
    assert snap.is_supported("medley.native-ordered-candidates.v1")
    assert snap.is_supported("medley.native-route-receipt.v1")
    assert snap.state_of("medley.native-replay-safe-fallback.v1") == "unsupported"


def test_unknown_tier_rejected() -> None:
    with pytest.raises(HostCapabilityError, match="unknown host_tier"):
        negotiate(host_tier="grok-binary")


def test_medley_explicit_unsupported_baseline_is_not_restored() -> None:
    snap = negotiate(
        host_tier=HOST_TIER_MEDLEY,
        advertised={"host.native-agent.v1": "unsupported"},
    )
    assert snap.state_of("host.native-agent.v1") == "unsupported"
    assert not snap.is_supported("host.native-agent.v1")
