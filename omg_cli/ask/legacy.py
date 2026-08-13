"""Map historical omg-ask meta records onto Slice A advisor facts.

The mapper never probes PATH, binaries, or the network.  It copies no prompt,
argv, cwd, or credential fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omg_cli.ask.registry import resolve_harness_id
from omg_cli.contracts.advisor_contract import validate_advisor_taxonomy
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_exact_keys,
    require_integer,
    require_object,
)


LEGACY_ASK_PROVIDERS = frozenset({"codex", "claude", "fable", "gemini", "agy"})

_TEAM_KEYS = frozenset(
    {
        "member",
        "member_id",
        "task",
        "task_id",
        "worktree",
        "worktree_id",
        "token",
        "claim_token",
        "cancellation_token",
        "team_id",
        "team_member",
        "worker_id",
        "mailbox",
        "pane_id",
    }
)
_NATIVE_KEYS = frozenset(
    {
        "catalog",
        "catalog_id",
        "receipt",
        "access",
        "access_lane",
        "model_route",
        "medley",
        "native_provider",
        "native_host",
        "provider_route",
    }
)
_EXPECTED_TAXONOMY = {
    "runtime_kind": "external_cli",
    "purpose": "advisory",
    "lifecycle": "foreground",
}
_EXPECTED_FLAGS = {
    "worker_eligible": False,
    "authoritative": False,
    "auto_apply": False,
}
_ASK_META_REQUIRED = frozenset({"version", "writer", "kind", "provider"})
_ASK_META_OPTIONAL = frozenset(
    {
        "ts",
        "cwd",
        "exit_code",
        "duration_s",
        "argv",
        "artifact",
        "run_id",
        "truncated",
        "bytes_captured",
        "dry_run",
        "advisor_route",
        *_EXPECTED_TAXONOMY,
        *_EXPECTED_FLAGS,
    }
)
_ADVISOR_ROUTE_KEYS = frozenset(
    {
        "skill",
        "requested_role",
        "role_class",
        "provider",
        "posture",
        "worker_eligible",
        "auto_apply",
        "authoritative",
        *_EXPECTED_TAXONOMY,
    }
)


def _family_for_store_kind(store_kind: Any) -> str:
    if not isinstance(store_kind, str) or not store_kind:
        return "native"
    token = store_kind.casefold()
    if token.startswith(("worker", "team")) or "worker" in token or "team" in token:
        return "team"
    return "native"


def _reject_family_keys(payload: Mapping[str, Any], *, label: str) -> None:
    team_hits = sorted(key for key in payload if key in _TEAM_KEYS)
    if team_hits:
        raise ContractValidationError(
            f"{label} contains team key(s): {team_hits}"
        )
    native_hits = sorted(key for key in payload if key in _NATIVE_KEYS)
    if native_hits:
        raise ContractValidationError(
            f"{label} contains native key(s): {native_hits}"
        )


def _reject_contradictory_facts(
    payload: Mapping[str, Any], *, label: str
) -> None:
    expected = {**_EXPECTED_TAXONOMY, **_EXPECTED_FLAGS}
    for key, wanted in expected.items():
        if key not in payload:
            continue
        if payload[key] != wanted:
            raise ContractValidationError(
                f"{label} {key}={payload[key]!r} contradicts {wanted!r}"
            )


def _reject_store_kind(payload: Mapping[str, Any], *, label: str) -> None:
    if "store_kind" not in payload:
        return
    family = _family_for_store_kind(payload.get("store_kind"))
    raise ContractValidationError(
        f"{label} is a {family} store_kind="
        f"{payload.get('store_kind')!r}, not an ask meta document"
    )


def _validate_nested_advisor_route(
    route: Any, *, harness_id: str
) -> None:
    payload = require_object(route, label="legacy advisor_route")
    _reject_family_keys(payload, label="legacy advisor_route")
    _reject_store_kind(payload, label="legacy advisor_route")
    require_exact_keys(
        payload,
        required=set(),
        optional=_ADVISOR_ROUTE_KEYS,
        label="legacy advisor_route",
    )
    _reject_contradictory_facts(payload, label="legacy advisor_route")
    if "provider" not in payload:
        return
    nested = payload["provider"]
    if not isinstance(nested, str) or not nested.strip():
        raise ContractValidationError(
            "legacy advisor_route provider must be a string"
        )
    resolved = resolve_harness_id(nested.strip().casefold())
    if resolved != harness_id:
        raise ContractValidationError(
            f"legacy advisor_route provider {nested!r} contradicts "
            f"harness_id {harness_id!r}"
        )


def map_legacy_ask_record(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Admit write_ask_meta-shaped records only.  Output is an allowlist."""

    payload = require_object(raw, label="legacy ask record")
    _reject_family_keys(payload, label="legacy ask record")
    _reject_store_kind(payload, label="legacy ask record")
    if payload.get("kind") != "ask" or payload.get("writer") != "omg-cli":
        raise ContractValidationError(
            "legacy ask record must have kind=ask and writer=omg-cli"
        )
    version = require_integer(payload.get("version"), label="version", minimum=1)
    if version != 1:
        raise ContractValidationError("legacy ask record version must be 1")
    require_exact_keys(
        payload,
        required=_ASK_META_REQUIRED,
        optional=_ASK_META_OPTIONAL,
        label="legacy ask record",
    )

    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ContractValidationError("legacy ask provider must be a string")
    token = provider.strip().casefold()
    if token not in LEGACY_ASK_PROVIDERS:
        raise ContractValidationError(
            f"legacy ask provider {provider!r} is not a supported ask provider token"
        )

    harness_id = resolve_harness_id(token)
    _reject_contradictory_facts(payload, label="legacy ask record")
    if "advisor_route" in payload:
        _validate_nested_advisor_route(
            payload.get("advisor_route"), harness_id=harness_id
        )

    taxonomy = validate_advisor_taxonomy(
        {
            "runtime_kind": "external_cli",
            "purpose": "advisory",
            "lifecycle": "foreground",
            "worker_eligible": False,
            "authoritative": False,
            "auto_apply": False,
        }
    )
    return {
        "legacy_field": True,
        "source_kind": "ask",
        "source_provider": token,
        "harness_id": harness_id,
        "runtime_kind": taxonomy["runtime_kind"],
        "purpose": taxonomy["purpose"],
        "lifecycle": taxonomy["lifecycle"],
        "worker_eligible": False,
        "authoritative": False,
        "auto_apply": False,
    }


__all__ = ["LEGACY_ASK_PROVIDERS", "map_legacy_ask_record"]
