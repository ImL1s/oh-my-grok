"""Medley inspect JSON consumption. No PATH inference."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.host_capabilities import HOST_TIER_GROK, HOST_TIER_MEDLEY
from omg_cli.medley_inspect import (
    INSPECT_SCHEMA,
    MedleyInspectError,
    apply_receipt_to_view_fields,
    receipt_for_policy,
    resolve_host_snapshot,
)


def _write_inspect(path: Path, **overrides: object) -> Path:
    payload = {
        "schema": INSPECT_SCHEMA,
        "schemaVersion": 1,
        "host": "medley",
        "capabilities": [
            {
                "capability_id": "medley.native-exact-model.v1",
                "state": "supported",
                "version": "v1",
                "reason": "exact",
            },
            {
                "capability_id": "medley.native-ordered-candidates.v1",
                "state": "supported",
                "version": "v1",
                "reason": "ordered",
            },
            {
                "capability_id": "medley.native-route-receipt.v1",
                "state": "supported",
                "version": "v1",
                "reason": "receipt",
            },
            {
                "capability_id": "medley.native-replay-safe-fallback.v1",
                "state": "unsupported",
                "version": None,
                "reason": "not in this slice",
            },
        ],
        "receipts": [
            {
                "schema": "medley.native-route-receipt.v1",
                "consumer_policy_id": "verifier.default",
                "selected_catalog_id": "review-primary-example",
                "route_digest": "a" * 64,
                "attempt": 2,
            }
        ],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_absent_inspect_is_stock_grok() -> None:
    snap, doc = resolve_host_snapshot(env={})
    assert snap.host_tier == HOST_TIER_GROK
    assert doc is None
    assert snap.state_of("medley.native-route-receipt.v1") == "unsupported"


def test_inspect_negotiates_medley_caps(tmp_path: Path) -> None:
    inspect = _write_inspect(tmp_path / "inspect.json")
    snap, doc = resolve_host_snapshot(inspect_path=inspect)
    assert snap.host_tier == HOST_TIER_MEDLEY
    assert snap.is_supported("medley.native-ordered-candidates.v1")
    assert snap.is_supported("medley.native-route-receipt.v1")
    assert snap.is_supported("host.native-exact-model.v1")
    assert snap.state_of("medley.native-replay-safe-fallback.v1") == "unsupported"
    rec = receipt_for_policy(doc, policy_id="verifier.default")
    assert rec is not None
    fields = apply_receipt_to_view_fields(rec)
    assert fields["selected_model_ref"] == "review-primary-example"
    assert fields["route_receipt_digest"] == "a" * 64
    assert fields["attempt"] == 2


def test_inspect_secret_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema": INSPECT_SCHEMA,
                "schemaVersion": 1,
                "host": "medley",
                "capabilities": [],
                "receipts": [{"note": "sk-secret-example"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MedleyInspectError) as exc:
        resolve_host_snapshot(inspect_path=path)
    assert exc.value.code == "E_MEDLEY_INSPECT_SECRET"


def test_inspect_wrong_schema_is_incompatible(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema": "not-medley",
                "schemaVersion": 1,
                "host": "medley",
                "capabilities": [],
                "receipts": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MedleyInspectError) as exc:
        resolve_host_snapshot(inspect_path=path)
    assert exc.value.code == "E_MEDLEY_INSPECT_SCHEMA"


def test_env_path_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inspect = _write_inspect(tmp_path / "from-env.json")
    snap, doc = resolve_host_snapshot(env={"OMG_MEDLEY_INSPECT": str(inspect)})
    assert snap.host_tier == HOST_TIER_MEDLEY
    assert doc is not None


def test_no_path_inference_from_binary_name(tmp_path: Path) -> None:
    (tmp_path / "medley.exe").write_text("", encoding="utf-8")
    snap, doc = resolve_host_snapshot(env={"PATH": str(tmp_path)})
    assert snap.host_tier == HOST_TIER_GROK
    assert doc is None
