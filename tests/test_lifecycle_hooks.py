from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from omg_cli.capability_discovery import hook_capability_inventory
from omg_cli.contracts.tracker_contract import make_role_receipt
from omg_cli.deny import decide_spawn_subagent
from omg_cli.runtime_events import read_all_runtime_events


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks" / "bin"


def _run_hook(name: str, tmp_path: Path, payload: str = "{}") -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "GROK_WORKSPACE_ROOT": str(tmp_path),
        "GROK_SESSION_ID": "session-1",
        "OMG_RUN_ID": "run-1",
    }
    return subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        cwd=tmp_path,
        timeout=10,
    )


def test_inventory_remains_exact_4_baseline_5_eligible_5_unavailable() -> None:
    rows = hook_capability_inventory(probe_timestamp="2026-07-22T00:00:00Z")
    assert [row["group"] for row in rows].count("baseline") == 4
    assert [row["group"] for row in rows].count("eligible_unclaimed") == 5
    assert [row["group"] for row in rows].count("unavailable") == 5


def test_session_start_is_passive_and_does_not_write_mutable_resume_pointer(tmp_path) -> None:
    proc = _run_hook("session_start.py", tmp_path, '{"event_id":"session-start-1"}')
    assert proc.returncode == 0
    assert not (tmp_path / ".omg" / "state" / "RESUME.md").exists()
    events = read_all_runtime_events(tmp_path)
    assert any(row["event_type"] == "session_started" for row in events)


def test_stop_and_subagent_alias_are_fail_open_bounded_and_non_authoritative(tmp_path) -> None:
    assert _run_hook("stop.py", tmp_path, "not-json{" * 200_000).returncode == 0
    payload = json.dumps(
        {
            "event_id": "child-1",
            "host_spawn_id": "spawn-1",
            "bound": True,
            "spawn_receipt_hash": "a" * 64,
            "role_receipt_hash": "b" * 64,
            "generation": 3,
            "receipt_generation": 3,
        }
    )
    assert _run_hook("subagent_stop.py", tmp_path, payload).returncode == 0
    events = read_all_runtime_events(tmp_path)
    assert any(
        row["event_type"] == "agent_closed"
        and row["payload"].get("hook_event") == "SubagentStop"
        for row in events
    )
    assert all("verified" not in row["payload"] for row in events)


def test_unbound_or_stale_subagent_completion_never_closes_child(tmp_path) -> None:
    payload = json.dumps(
        {
            "event_id": "child-unbound",
            "host_spawn_id": "spawn-1",
            "bound": True,
            "spawn_receipt_hash": "a" * 64,
            "role_receipt_hash": "b" * 64,
            "generation": 4,
            "receipt_generation": 3,
        }
    )
    assert _run_hook("subagent_stop.py", tmp_path, payload).returncode == 0
    rows = read_all_runtime_events(tmp_path)
    assert rows[-1]["event_type"] == "agent_failed"
    assert rows[-1]["payload"]["diagnostic"] == "E_UNBOUND_SUBAGENT_COMPLETION"


def _receipt_bound_spawn_input() -> dict:
    spawn = {
        "store_kind": "spawn_receipt",
        "schema_version": 1,
        "receipt_id": "receipt-1",
        "run_id": "run-1",
        "team_id": "team-1",
        "task_id": "task-1",
        "parent_id": "parent-1",
        "parent_session_id": "session-1",
        "requested_role": "omg-executor",
        "capability_mode": "read-write",
        "depth": 1,
        "attempt": 1,
        "receipt_generation": 3,
        "lease_generation": 4,
        "dispatch_nonce": "nonce-1",
        "expires_at": "2099-07-22T01:00:00Z",
        "expected_state": "spawn-requested",
        "expected_sequence": 7,
    }
    expectation = {
        field: spawn[field]
        for field in (
            "run_id",
            "team_id",
            "task_id",
            "parent_id",
            "parent_session_id",
            "attempt",
            "receipt_generation",
            "lease_generation",
            "dispatch_nonce",
            "expected_state",
            "expected_sequence",
        )
    }
    expectation["observed_at"] = "2026-07-22T00:00:00Z"
    return {
        "subagent_type": "omg-executor",
        "capability_mode": "read-write",
        "depth": 1,
        "spawn_receipt": spawn,
        "role_receipt": make_role_receipt(spawn),
        "receipt_expectation": expectation,
    }


def test_spawn_policy_binds_w0_receipts_and_rejects_stale_foreign_or_tampered() -> None:
    valid = _receipt_bound_spawn_input()
    assert decide_spawn_subagent(valid)["decision"] == "allow"

    stale = deepcopy(valid)
    stale["spawn_receipt"]["expires_at"] = "2025-07-22T00:00:00Z"
    stale["role_receipt"] = make_role_receipt(stale["spawn_receipt"])
    assert decide_spawn_subagent(stale)["decision"] == "deny"

    foreign = deepcopy(valid)
    foreign["receipt_expectation"]["run_id"] = "foreign-run"
    assert decide_spawn_subagent(foreign)["decision"] == "deny"

    wrong_generation = deepcopy(valid)
    wrong_generation["receipt_expectation"]["receipt_generation"] = 2
    assert decide_spawn_subagent(wrong_generation)["decision"] == "deny"

    tampered = deepcopy(valid)
    tampered["role_receipt"]["expected_sequence"] += 1
    assert decide_spawn_subagent(tampered)["decision"] == "deny"


def test_rendered_standalone_preserves_receipt_validation_under_isolated_python(
    tmp_path,
) -> None:
    rendered = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_standalone_hook.py"), "--print"],
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=10,
        check=True,
    ).stdout
    standalone = tmp_path / "omg_pretool_deny_standalone.py"
    standalone.write_text(rendered, encoding="utf-8")

    def decide(tool_input: dict) -> str:
        proc = subprocess.run(
            [sys.executable, "-I", "-S", str(standalone)],
            input=json.dumps(
                {"toolName": "spawn_subagent", "toolInput": tool_input},
                sort_keys=True,
            ),
            text=True,
            capture_output=True,
            cwd=tmp_path,
            timeout=10,
            check=True,
        )
        return json.loads(proc.stdout)["decision"]

    valid = _receipt_bound_spawn_input()
    assert decide(valid) == "allow"

    stale = deepcopy(valid)
    stale["spawn_receipt"]["expires_at"] = "2025-07-22T00:00:00Z"
    stale["role_receipt"] = make_role_receipt(stale["spawn_receipt"])
    assert decide(stale) == "deny"

    foreign = deepcopy(valid)
    foreign["receipt_expectation"]["run_id"] = "foreign-run"
    assert decide(foreign) == "deny"

    tampered = deepcopy(valid)
    tampered["role_receipt"]["expected_sequence"] += 1
    assert decide(tampered) == "deny"
