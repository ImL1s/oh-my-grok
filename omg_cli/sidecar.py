"""Honest status for Grok's public native-dashboard surface.

OMG does not connect to undocumented localhost/private sidecars.  This module
reports the public surface as ``optional_unclaimed`` at T0 until a future
independent verifier is explicitly bound by the integration owner.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_SURFACE = "grok_public_native_dashboard"


def _base_status() -> dict[str, Any]:
    return {
        "store_kind": "omg_native_dashboard_status",
        "schema_version": 1,
        "repository_id": "OMG",
        "surface": _SURFACE,
        "status": "optional_unclaimed",
        "enabled": False,
        "observed": False,
        "attempted": False,
        "evidence_tier": "T0",
        "detail_code": "PUBLIC_NATIVE_DASHBOARD_UNCLAIMED",
    }


def native_dashboard_status(evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return public native-dashboard status without performing any probe."""

    # Self-declared hashes cannot promote a capability tier.  W6 may bind a
    # future signed public-schema observer, but W5 deliberately has no verifier
    # key or host-private transport and therefore remains T0.
    _ = evidence
    return _base_status()


inspect_sidecar_status = native_dashboard_status
public_native_dashboard_status = native_dashboard_status

__all__ = [
    "inspect_sidecar_status",
    "native_dashboard_status",
    "public_native_dashboard_status",
]
