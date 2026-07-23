"""Public native-dashboard status only; no private sidecar probing."""
from __future__ import annotations

import inspect

from omg_cli import sidecar


def test_native_dashboard_is_optional_unclaimed_without_public_proof():
    status = sidecar.native_dashboard_status()
    assert status == {
        "store_kind": "omg_native_dashboard_status",
        "schema_version": 1,
        "repository_id": "OMG",
        "surface": "grok_public_native_dashboard",
        "status": "optional_unclaimed",
        "enabled": False,
        "observed": False,
        "attempted": False,
        "evidence_tier": "T0",
        "detail_code": "PUBLIC_NATIVE_DASHBOARD_UNCLAIMED",
    }


def test_native_dashboard_does_not_promote_self_declared_public_evidence():
    proof = {
        "surface": "grok_public_native_dashboard",
        "stable_public_schema": True,
        "status": "available",
        "schema_sha256": "a" * 64,
        "receipt_sha256": "b" * 64,
        "observed_at": "2026-07-22T00:00:00Z",
    }
    status = sidecar.native_dashboard_status(proof)
    assert status["status"] == "optional_unclaimed"
    assert status["enabled"] is False
    assert status["observed"] is False
    assert status["evidence_tier"] == "T0"

    invalid = sidecar.native_dashboard_status({**proof, "receipt_sha256": "bad"})
    assert invalid["status"] == "optional_unclaimed"
    assert invalid["evidence_tier"] == "T0"


def test_sidecar_module_has_no_network_or_listener_surface():
    source = inspect.getsource(sidecar)
    assert "import socket" not in source
    assert "import http" not in source
    assert "listen(" not in source
    assert "connect(" not in source
    assert not any(
        word in name.lower()
        for name in sidecar.__all__
        for word in ("listen", "server", "reply", "inbound")
    )
