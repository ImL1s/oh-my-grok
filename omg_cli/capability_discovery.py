"""Independent capability tiers, deterministic origins, and hook truth rows."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from omg_cli.contracts.capability_schema import CAPABILITY_TIERS, validate_capability_record
from omg_cli.redaction import redact_value


BASELINE_HOOKS = ("SessionStart", "PreToolUse", "Stop", "SubagentEnd")
ELIGIBLE_UNCLAIMED_HOOKS = (
    "PostToolUse",
    "SessionEnd",
    "Notification",
    "UserPromptSubmit",
    "SubagentStart",
)
UNAVAILABLE_HOOKS = (
    "StopFailure",
    "PostToolUseFailure",
    "PermissionDenied",
    "PreCompact",
    "PostCompact",
)


def discover_capabilities(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in records:
        record = dict(validate_capability_record(value))
        grouped[record["canonical_name"]].append(record)
    discovered: list[dict[str, Any]] = []
    for name in sorted(grouped, key=lambda item: item.encode("utf-8")):
        ordered = sorted(
            grouped[name],
            key=lambda row: (
                row["resolution_priority"],
                row["origin"].encode("utf-8"),
                row["digest"],
            ),
        )
        winner = ordered[0]
        restart_required = bool(
            winner["configured"]
            and winner["enabled"]
            and winner["loadable"]
            and not winner["observed"]
        )
        discovered.append(
            {
                "canonical_name": name,
                "winner": winner,
                "shadows": ordered[1:],
                "restart_required": restart_required,
            }
        )
    return discovered


def _hook_record(
    name: str,
    *,
    group: str,
    observed: bool,
    probe_timestamp: str,
) -> dict[str, Any]:
    baseline = group == "baseline"
    eligible = group == "eligible_unclaimed"
    digest = hashlib.sha256(f"grok-hook:{name}:{group}".encode()).hexdigest()
    record = {
        "store_kind": "capability_evidence",
        "schema_version": 1,
        "canonical_name": f"grok.hook.{name}",
        "aliases": ["SubagentStop"] if name == "SubagentEnd" else [],
        "origin": "oh-my-grok-plugin",
        "resolution_priority": 10,
        "version": "hook-contract-v1",
        "digest": digest,
        "probe_timestamp": probe_timestamp,
        "bounded_result": {
            "group": group,
            "fresh_observation": observed,
            "claimable": baseline,
        },
        "redacted_diagnostic": (
            "baseline route" if baseline else "eligible but unclaimed" if eligible else "unavailable"
        ),
        "configured": baseline,
        "installed": baseline,
        "enabled": baseline,
        "loadable": baseline,
        "observed": bool(observed and baseline),
        "healthy": bool(observed and baseline),
        "verified": bool(observed and baseline),
    }
    return dict(validate_capability_record(record))


def hook_capability_inventory(
    *,
    observed: set[str] | None = None,
    probe_timestamp: str,
) -> list[dict[str, Any]]:
    seen = observed or set()
    rows: list[dict[str, Any]] = []
    for group, names in (
        ("baseline", BASELINE_HOOKS),
        ("eligible_unclaimed", ELIGIBLE_UNCLAIMED_HOOKS),
        ("unavailable", UNAVAILABLE_HOOKS),
    ):
        for name in names:
            rows.append(
                {
                    "group": group,
                    "record": _hook_record(
                        name,
                        group=group,
                        observed=name in seen,
                        probe_timestamp=probe_timestamp,
                    ),
                }
            )
    return rows


def bounded_probe_result(value: Mapping[str, Any], *, max_chars: int = 8192) -> dict[str, Any]:
    redacted = redact_value(dict(value))
    text = repr(redacted)
    if len(text) <= max_chars:
        return redacted
    return {
        "truncated": True,
        "original_chars": len(text),
        "preview": text[:max_chars],
    }


__all__ = [
    "BASELINE_HOOKS",
    "CAPABILITY_TIERS",
    "ELIGIBLE_UNCLAIMED_HOOKS",
    "UNAVAILABLE_HOOKS",
    "bounded_probe_result",
    "discover_capabilities",
    "hook_capability_inventory",
]
