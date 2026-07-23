from __future__ import annotations

import json
import stat

import pytest

from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.runtime_events import (
    append_hook_event,
    append_runtime_event,
    normalize_lifecycle_event,
    read_runtime_events,
    source_journal_path,
)


def _event(**overrides):
    row = {
        "source": "grok-native",
        "source_cursor": "cursor-1",
        "source_sequence": 1,
        "event_id": "event-1",
        "event_type": "session_started",
        "run_id": "run-1",
        "session_id": "session-1",
        "observed_at": "2026-07-22T00:00:00Z",
        "payload": {"Authorization": "Bearer raw-token", "status": "ok"},
    }
    row.update(overrides)
    return normalize_lifecycle_event(**row)


def test_normalized_journal_is_private_redacted_and_exactly_idempotent(tmp_path) -> None:
    event = _event()
    first = append_runtime_event(tmp_path, event)
    second = append_runtime_event(tmp_path, event)
    assert first == second
    path = source_journal_path(tmp_path, "grok-native")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rows = read_runtime_events(path)
    assert len(rows) == 1
    assert "raw-token" not in json.dumps(rows[0])

    changed = _event(payload={"status": "different"})
    with pytest.raises(ContractValidationError, match="identity"):
        append_runtime_event(tmp_path, changed)


def test_hook_route_is_excluded_from_dedupe_identity_and_alias_is_normalized(tmp_path) -> None:
    first = append_hook_event(
        tmp_path,
        hook_event="SubagentEnd",
        payload={
            "route": "plugin",
            "host_spawn_id": "spawn-1",
            "bound": True,
            "spawn_receipt_hash": "a" * 64,
            "role_receipt_hash": "b" * 64,
        },
        run_id="run-1",
        session_id="session-1",
        event_id="subagent-end-1",
        observed_at="2026-07-22T00:00:00Z",
    )
    second = append_hook_event(
        tmp_path,
        hook_event="SubagentEnd",
        payload={
            "route": "global",
            "host_spawn_id": "spawn-1",
            "bound": True,
            "spawn_receipt_hash": "a" * 64,
            "role_receipt_hash": "b" * 64,
        },
        run_id="run-1",
        session_id="session-1",
        event_id="subagent-end-1",
        observed_at="2026-07-22T00:00:01Z",
    )
    assert first["event_hash"] == second["event_hash"]
    rows = read_runtime_events(first["journal_path"])
    assert len(rows) == 1
    assert rows[0]["event_type"] == "agent_closed"
    assert rows[0]["payload"]["hook_event"] == "SubagentStop"
    assert "route" not in rows[0]["payload"]


def test_event_payload_is_bounded_before_journal_mutation(tmp_path) -> None:
    event = _event(payload={"value": "x" * 70_000})
    with pytest.raises(ContractValidationError, match="bounded"):
        append_runtime_event(tmp_path, event)
    assert not source_journal_path(tmp_path, "grok-native").exists()
