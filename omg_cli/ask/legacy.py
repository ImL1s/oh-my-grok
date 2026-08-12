"""Map historical omg-ask meta records onto Slice A advisor facts.

The mapper never probes PATH, binaries, or the network.  It copies no prompt,
argv, cwd, or credential fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omg_cli.ask.registry import resolve_harness_id
from omg_cli.contracts.advisor_contract import validate_advisor_taxonomy
from omg_cli.contracts.state_schemas import ContractValidationError, require_object


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
    }
)


def _family_for_store_kind(store_kind: Any) -> str:
    if not isinstance(store_kind, str) or not store_kind:
        return "native"
    token = store_kind.casefold()
    if token.startswith(("worker", "team")) or "worker" in token or "team" in token:
        return "team"
    return "native"


def map_legacy_ask_record(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Admit write_ask_meta-shaped records only.  Output is an allowlist."""

    payload = require_object(raw, label="legacy ask record")
    team_hits = sorted(key for key in payload if key in _TEAM_KEYS)
    if team_hits:
        raise ContractValidationError(
            f"legacy ask record contains team key(s): {team_hits}"
        )
    if "store_kind" in payload:
        family = _family_for_store_kind(payload.get("store_kind"))
        raise ContractValidationError(
            f"legacy ask record is a {family} store_kind="
            f"{payload.get('store_kind')!r}, not an ask meta document"
        )
    native_hits = sorted(key for key in payload if key in _NATIVE_KEYS)
    if native_hits:
        raise ContractValidationError(
            f"legacy ask record contains native key(s): {native_hits}"
        )
    if payload.get("kind") != "ask" or payload.get("writer") != "omg-cli":
        raise ContractValidationError(
            "legacy ask record must have kind=ask and writer=omg-cli"
        )
    if payload.get("version") != 1:
        raise ContractValidationError("legacy ask record version must be 1")

    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ContractValidationError("legacy ask provider must be a string")
    token = provider.strip().casefold()
    if token not in LEGACY_ASK_PROVIDERS:
        raise ContractValidationError(
            f"legacy ask provider {provider!r} is not a supported ask provider token"
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
        "harness_id": resolve_harness_id(token),
        "runtime_kind": taxonomy["runtime_kind"],
        "purpose": taxonomy["purpose"],
        "lifecycle": taxonomy["lifecycle"],
        "worker_eligible": False,
        "authoritative": False,
        "auto_apply": False,
    }


__all__ = ["LEGACY_ASK_PROVIDERS", "map_legacy_ask_record"]
