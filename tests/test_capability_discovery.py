from __future__ import annotations

from omg_cli.capability_discovery import (
    BASELINE_HOOKS,
    ELIGIBLE_UNCLAIMED_HOOKS,
    UNAVAILABLE_HOOKS,
    discover_capabilities,
    hook_capability_inventory,
)


def _record(name: str, origin: str, priority: int, **tiers) -> dict:
    row = {
        "store_kind": "capability_evidence",
        "schema_version": 1,
        "canonical_name": name,
        "aliases": [],
        "origin": origin,
        "resolution_priority": priority,
        "version": "v1",
        "digest": ("a" if priority == 10 else "b") * 64,
        "probe_timestamp": "2026-07-22T00:00:00Z",
        "bounded_result": {"status": "ok"},
        "redacted_diagnostic": "safe",
        "configured": False,
        "installed": False,
        "enabled": False,
        "loadable": False,
        "observed": False,
        "healthy": False,
        "verified": False,
    }
    row.update(tiers)
    return row


def test_hook_truth_inventory_is_exact_4_plus_5_plus_5() -> None:
    rows = hook_capability_inventory(
        observed={"SessionStart", "PreToolUse", "Stop", "SubagentEnd"},
        probe_timestamp="2026-07-22T00:00:00Z",
    )
    assert len(BASELINE_HOOKS) == 4
    assert len(ELIGIBLE_UNCLAIMED_HOOKS) == 5
    assert len(UNAVAILABLE_HOOKS) == 5
    assert [row["group"] for row in rows].count("baseline") == 4
    assert [row["group"] for row in rows].count("eligible_unclaimed") == 5
    assert [row["group"] for row in rows].count("unavailable") == 5
    assert all(not row["record"]["verified"] for row in rows if row["group"] != "baseline")


def test_discovery_keeps_independent_tiers_and_visible_deterministic_shadows() -> None:
    low = _record("grok.hook.SessionStart", "repo", 20, configured=True, installed=True)
    winner = _record("grok.hook.SessionStart", "plugin", 10, observed=True)
    result = discover_capabilities([low, winner])
    assert result[0]["winner"]["origin"] == "plugin"
    assert result[0]["shadows"][0]["origin"] == "repo"
    assert result[0]["winner"]["observed"] is True
    assert result[0]["winner"]["installed"] is False
    assert result[0]["restart_required"] is False
