"""Agent/model policy overlay and resolution (#131)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omg_cli.agent_policy import (
    POLICY_RELATIVE,
    POLICY_SCHEMA,
    ROUTE_KIND_EXTERNAL,
    ROUTE_KIND_NATIVE,
    AgentPolicyError,
    filter_policy_views,
    list_agent_policies,
    load_policy_bundle,
    parse_policy_route,
    policy_digest,
    render_stock_host_projection,
    resolve_agent_policy,
    resume_pin,
    spawn_admitted,
)
from omg_cli.agents_catalog import load_agents_catalog
from omg_cli.host_capabilities import (
    HOST_TIER_MEDLEY,
    HOST_TIER_UNKNOWN,
    negotiate,
    stock_grok_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="agent catalog pin requires POSIX O_NOFOLLOW/dir_fd",
)
_SECRET_NEEDLES = (
    "api_key",
    "sk-",
    "bearer ",
    "account_id",
    "acct_",
    "authorization",
    "access_token",
)


def test_overlay_covers_catalog_and_builtins() -> None:
    catalog = load_agents_catalog(ROOT, require_projections=False)
    bundle = load_policy_bundle(ROOT)
    catalog_ids = {record.id for record in catalog.agents}
    policy_ids = {item.agent_id for item in bundle.identities if item.is_catalog_agent}
    assert catalog_ids == policy_ids
    names = {item.agent_id for item in bundle.identities}
    assert {"explore", "plan", "general-purpose"} <= names
    assert bundle.schema == POLICY_SCHEMA


def test_stock_orchestrator_executor_verifier_explore_inherit() -> None:
    snap = stock_grok_snapshot()
    for name in ("omg-orchestrator", "omg-executor", "omg-verifier", "explore"):
        view = resolve_agent_policy(name, root=ROOT, host=snap)
        assert view.baseline_mode == "inherit"
        assert view.selected_model_ref is None
        assert view.route_kind == ROUTE_KIND_NATIVE
        assert view.route_receipt_digest is None
        assert view.attempt is None
        assert view.inspect_source == "absent"
        assert view.status == "ready"
        assert spawn_admitted(view)
        assert view.host_facts["medley_capability_outcome"] == "unsupported"
        assert view.host_facts["route_specific_facts"] == "unavailable"
        assert view.to_json()["effective_route"] is None
        assert view.to_json()["inspect_source"] == "absent"
        assert any(r.code == "E_MEDLEY_INSPECT_ABSENT" for r in view.reasons)
        pin = resume_pin(view)
        assert pin["attempt"] is None
        assert pin["route_receipt_digest"] is None


def test_verifier_extension_not_flattened_on_stock() -> None:
    view = resolve_agent_policy("verifier", root=ROOT)
    assert view.agent_id == "omg-verifier"
    assert view.requested_extension == "medley.native-ordered-candidates.v1"
    assert view.candidate_ids[0] == "review-primary-example"
    assert view.selected_model_ref is None
    assert view.baseline_mode == "inherit"
    assert any(r.code == "E_EXTENSION_NOT_AUTHORIZED" for r in view.reasons)
    stock = render_stock_host_projection(view)
    blob = json.dumps(stock)
    assert "review-primary-example" not in blob
    assert "review-fallback-example" not in blob
    assert stock["baseline_mode"] == "inherit"


def test_exact_never_silently_inherits() -> None:
    view = resolve_agent_policy(
        "omg-executor",
        root=ROOT,
        per_run={"model": "grok-example-1"},
    )
    assert view.baseline_mode == "exact"
    assert view.baseline_model == "grok-example-1"
    assert view.selected_model_ref is None
    assert view.status == "blocked"
    assert not spawn_admitted(view)
    assert any(r.code == "E_EXACT_UNSUPPORTED" for r in view.reasons)
    pin = resume_pin(view)
    assert pin["selected_model_ref"] is None
    assert pin["policy_digest"] == view.policy_digest


def test_exact_admitted_when_host_exposes_contract() -> None:
    host = negotiate(
        host_tier=HOST_TIER_MEDLEY,
        advertised={"host.native-exact-model.v1": "v1"},
    )
    view = resolve_agent_policy(
        "omg-executor",
        root=ROOT,
        per_run={"model": "grok-example-1"},
        host=host,
    )
    assert view.status == "ready"
    assert view.selected_model_ref == "grok-example-1"
    assert spawn_admitted(view)
    route = view.to_json()["effective_route"]
    assert route is not None
    assert route["kind"] == ROUTE_KIND_NATIVE
    assert route["selected_model_ref"] == "grok-example-1"
    assert route["route_receipt_digest"] is None


def test_requires_capability_rejects_on_stock() -> None:
    view = resolve_agent_policy(
        "omg-verifier",
        root=ROOT,
        per_run={"baseline": {"mode": "requires_capability"}},
    )
    assert view.status == "blocked"
    assert view.selected_model_ref is None
    assert not spawn_admitted(view)


def test_unknown_host_does_not_authorize() -> None:
    view = resolve_agent_policy(
        "omg-orchestrator",
        root=ROOT,
        host=negotiate(host_tier=HOST_TIER_UNKNOWN),
    )
    assert view.status == "unknown"
    assert not spawn_admitted(view)


def test_digest_changes_with_profile_and_candidate_order() -> None:
    bundle = load_policy_bundle(ROOT)
    ident = bundle.resolve_name("omg-verifier")
    base = policy_digest(
        policy=ident.policy,
        prompt_profile_version=bundle.prompt_profile_version,
        capability_floor=ident.capability_mode,
    )
    tweaked = dict(ident.policy)
    tweaked["prompt_profile"] = "gpt-family"
    assert (
        policy_digest(
            policy=tweaked,
            prompt_profile_version=bundle.prompt_profile_version,
            capability_floor=ident.capability_mode,
        )
        != base
    )
    ext = dict(ident.policy["extensions"])
    ordered = dict(ext["medley.native-ordered-candidates.v1"])
    ordered["candidates"] = list(reversed(ordered["candidates"]))
    ext["medley.native-ordered-candidates.v1"] = ordered
    reordered = dict(ident.policy)
    reordered["extensions"] = ext
    assert (
        policy_digest(
            policy=reordered,
            prompt_profile_version=bundle.prompt_profile_version,
            capability_floor=ident.capability_mode,
        )
        != base
    )


def test_unknown_agent_fails_visibly() -> None:
    with pytest.raises(AgentPolicyError, match="unknown agent") as exc:
        resolve_agent_policy("not-an-agent", root=ROOT)
    assert exc.value.code == "E_AGENT_NOT_FOUND"


def test_model_and_models_conflict(tmp_path: Path) -> None:
    with pytest.raises(AgentPolicyError, match="cannot both be set"):
        resolve_agent_policy(
            "omg-executor",
            root=ROOT,
            per_run={"model": "a", "models": ["b"]},
        )


def test_empty_candidates_invalid() -> None:
    with pytest.raises(AgentPolicyError, match="non-empty array"):
        resolve_agent_policy(
            "omg-verifier",
            root=ROOT,
            per_run={
                "extensions": {
                    "medley.native-ordered-candidates.v1": {"candidates": []}
                }
            },
        )


def test_override_cannot_widen_capability(tmp_path: Path) -> None:
    with pytest.raises(AgentPolicyError, match="cannot widen"):
        resolve_agent_policy(
            "omg-verifier",
            root=ROOT,
            per_run={"capability_mode": "read-write"},
        )


def test_project_override_precedence(tmp_path: Path) -> None:
    policy_dir = tmp_path / ".omg"
    policy_dir.mkdir()
    (policy_dir / "agent-policies.json").write_text(
        json.dumps(
            {
                "schema": "omg-agent-model-policy-override/v1",
                "agents": {
                    "omg-executor": {"prompt_profile": "gpt-family"},
                },
            }
        ),
        encoding="utf-8",
    )
    view = resolve_agent_policy(
        "omg-executor", root=ROOT, project_root=tmp_path
    )
    assert view.policy_source == "project"
    assert view.prompt_profile == "gpt-family"


def test_secret_sentinels_rejected(tmp_path: Path) -> None:
    policy_dir = tmp_path / ".omg"
    policy_dir.mkdir()
    (policy_dir / "agent-policies.json").write_text(
        json.dumps(
            {
                "schema": "omg-agent-model-policy-override/v1",
                "agents": {
                    "omg-executor": {"prompt_profile": "generic"},
                },
                "api_key": "sk-example",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AgentPolicyError) as exc:
        resolve_agent_policy("omg-executor", root=ROOT, project_root=tmp_path)
    assert exc.value.code == "E_AGENT_POLICY_SECRET"


def test_native_and_external_routes_cannot_be_confused() -> None:
    native = parse_policy_route(
        {
            "kind": ROUTE_KIND_NATIVE,
            "policy_id": "executor.default",
            "baseline_mode": "inherit",
            "model_ref": None,
        }
    )
    external = parse_policy_route(
        {
            "kind": ROUTE_KIND_EXTERNAL,
            "executor": "codex",
            "provider": "codex",
            "model_flag": "gpt-example-1",
        }
    )
    assert native.kind == ROUTE_KIND_NATIVE
    assert external.kind == ROUTE_KIND_EXTERNAL
    with pytest.raises(AgentPolicyError, match="executor"):
        parse_policy_route(
            {
                "kind": ROUTE_KIND_NATIVE,
                "policy_id": "x",
                "baseline_mode": "inherit",
                "executor": "codex",
            }
        )
    with pytest.raises(AgentPolicyError, match="catalog"):
        parse_policy_route(
            {
                "kind": ROUTE_KIND_EXTERNAL,
                "executor": "codex",
                "catalog": "review-primary-example",
            }
        )
    with pytest.raises(AgentPolicyError, match="unknown keys"):
        parse_policy_route(
            {
                "kind": ROUTE_KIND_EXTERNAL,
                "executor": "codex",
                "policy_id": "executor.default",
            }
        )


def test_override_models_without_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AgentPolicyError) as exc:
        resolve_agent_policy(
            "omg-executor",
            root=ROOT,
            per_run={"models": ["grok-example-1"]},
        )
    assert exc.value.code == "E_AGENT_POLICY_CONFLICT"


def test_list_filter_and_deterministic_order() -> None:
    rows = list_agent_policies(root=ROOT, catalog_only=True)
    ids = [row.agent_id for row in rows]
    assert ids == sorted(ids)
    assert "omg-verifier" in ids
    filtered = filter_policy_views(rows, category="verifier")
    assert [row.agent_id for row in filtered] == ["omg-verifier"]


def test_overlay_and_views_have_no_secret_sentinels() -> None:
    text = (ROOT / POLICY_RELATIVE).read_text(encoding="utf-8").lower()
    for needle in _SECRET_NEEDLES:
        assert needle not in text
    view = resolve_agent_policy("omg-verifier", root=ROOT)
    blob = json.dumps(view.to_json()).lower()
    for needle in _SECRET_NEEDLES:
        assert needle not in blob


def test_unknown_prompt_family_rejected() -> None:
    with pytest.raises(AgentPolicyError, match="unknown"):
        resolve_agent_policy(
            "omg-executor",
            root=ROOT,
            per_run={"prompt_profile": "mystery-family"},
        )


def test_deny_module_does_not_import_agent_policy() -> None:
    """Standalone PreToolUse hook must stay stdlib-only; policy lives in CLI."""
    deny = (ROOT / "omg_cli" / "deny.py").read_text(encoding="utf-8")
    assert "agent_policy" not in deny
    assert "host_capabilities" not in deny
    assert "model_policies" not in deny


def test_policy_view_schema_requires_full_projection() -> None:
    schema = json.loads(
        (ROOT / "docs" / "schemas" / "omg.agent_policy_view.v1.json").read_text(
            encoding="utf-8"
        )
    )
    view = resolve_agent_policy("omg-verifier", root=ROOT).to_json()
    assert set(schema["required"]) == set(view)
    assert schema["properties"]["effective_route"]["type"] == ["object", "null"]
    assert view["effective_route"] is None
    assert view["requested_policy"]["binding"] == "inherit"
    assert view["inspect_source"] == "absent"
    assert view["attempt"] is None
    assert schema["properties"]["inspect_source"]["enum"] == ["absent", "document"]


def test_inspect_absent_does_not_attempt_medley_18_fallback() -> None:
    view = resolve_agent_policy("omg-verifier", root=ROOT)
    assert view.inspect_source == "absent"
    assert view.attempt is None
    assert view.route_receipt_digest is None
    assert view.selected_model_ref is None
    assert view.candidate_ids
    assert view.selected_model_ref not in view.candidate_ids
    assert view.host_facts["medley_capability_outcome"] == "unsupported"
    assert view.host_facts["route_specific_facts"] == "unavailable"
    absent = next(r for r in view.reasons if r.code == "E_MEDLEY_INSPECT_ABSENT")
    assert "not attempted" in absent.message
    assert absent.next_action == "omg agents list --host-inspect PATH"
    assert view.reasons[0].code == "E_MEDLEY_INSPECT_ABSENT"


def test_inspect_receipt_overlays_verifier_effective_route(tmp_path: Path) -> None:
    from omg_cli.medley_inspect import (
        INSPECT_SCHEMA,
        load_inspect_document,
        snapshot_from_inspect,
    )

    baseline = resolve_agent_policy("omg-verifier", root=ROOT)
    path = tmp_path / "inspect.json"
    path.write_text(
        json.dumps(
            {
                "schema": INSPECT_SCHEMA,
                "schemaVersion": 1,
                "host": "medley",
                "capabilities": [
                    {
                        "capability_id": "medley.native-exact-model.v1",
                        "state": "supported",
                        "version": "v1",
                    },
                    {
                        "capability_id": "medley.native-ordered-candidates.v1",
                        "state": "supported",
                        "version": "v1",
                    },
                    {
                        "capability_id": "medley.native-route-receipt.v1",
                        "state": "supported",
                        "version": "v1",
                    },
                ],
                "receipts": [
                    {
                        "schema": "medley.native-route-receipt.v1",
                        "consumer_policy_id": "verifier.default",
                        "consumer_policy_digest": baseline.policy_digest,
                        "selected_catalog_id": "review-primary-example",
                        "route_digest": "b" * 64,
                        "attempt": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    doc = load_inspect_document(path)
    snap = snapshot_from_inspect(doc)
    view = resolve_agent_policy(
        "omg-verifier",
        root=ROOT,
        host=snap,
        inspect_doc=doc,
    )
    assert snap.host_tier == HOST_TIER_MEDLEY
    assert view.selected_model_ref == "review-primary-example"
    assert view.route_receipt_digest == "b" * 64
    assert view.attempt == 3
    assert view.inspect_source == "document"
    assert not any(r.code == "E_MEDLEY_INSPECT_ABSENT" for r in view.reasons)
    assert view.host_facts["route_specific_facts"] == "supported"
    assert view.to_json()["effective_route"]["selected_model_ref"] == (
        "review-primary-example"
    )


def test_inspect_receipt_does_not_overlay_stale_policy_digest(tmp_path: Path) -> None:
    from omg_cli.medley_inspect import (
        INSPECT_SCHEMA,
        load_inspect_document,
        snapshot_from_inspect,
    )

    path = tmp_path / "inspect.json"
    path.write_text(
        json.dumps(
            {
                "schema": INSPECT_SCHEMA,
                "schemaVersion": 1,
                "host": "medley",
                "capabilities": [
                    {
                        "capability_id": "medley.native-exact-model.v1",
                        "state": "supported",
                        "version": "v1",
                    },
                    {
                        "capability_id": "medley.native-ordered-candidates.v1",
                        "state": "supported",
                        "version": "v1",
                    },
                    {
                        "capability_id": "medley.native-route-receipt.v1",
                        "state": "supported",
                        "version": "v1",
                    },
                ],
                "receipts": [
                    {
                        "schema": "medley.native-route-receipt.v1",
                        "consumer_policy_id": "verifier.default",
                        "consumer_policy_digest": "c" * 64,
                        "selected_catalog_id": "review-primary-example",
                        "route_digest": "b" * 64,
                        "attempt": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    doc = load_inspect_document(path)
    snap = snapshot_from_inspect(doc)
    view = resolve_agent_policy(
        "omg-verifier",
        root=ROOT,
        host=snap,
        inspect_doc=doc,
    )
    assert view.selected_model_ref != "review-primary-example"
    assert view.route_receipt_digest is None
    assert view.attempt is None
    assert view.inspect_source == "document"
    assert view.to_json()["effective_route"] is None
