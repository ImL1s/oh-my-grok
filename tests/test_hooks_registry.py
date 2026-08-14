"""Host-neutral lifecycle registry (#72) — honest Grok mappings, fail-open bus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.hooks_registry import (
    CANONICAL_EVENTS,
    GROK_EVENT_MAP,
    REGISTRY_RELATIVE,
    HooksRegistryError,
    bus_disabled,
    check_antigravity_projection,
    dispatch,
    inspect_hooks_registry,
    load_hooks_registry,
    resolve_continuation,
    skipped_hook_ids,
    write_antigravity_projection,
    write_compact_handoff,
)

ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_repo_registry_loads_and_never_claims_verified() -> None:
    registry = load_hooks_registry(ROOT)
    ids = {hook.id for hook in registry.hooks}
    assert "omg.pretool.deny" in ids
    assert "omg.stop.gate" in ids
    assert "omg.prompt.submit.unsupported" in ids
    prompt = registry.by_id()["omg.prompt.submit.unsupported"]
    assert prompt.enabled is False
    assert prompt.host_capability == "unsupported"
    payload = inspect_hooks_registry(ROOT)
    assert payload["ok"] is True
    assert payload["verified"] is False
    assert payload["observed"] is False
    assert payload["healthy"] is False
    assert "UserPromptSubmit" in payload["note"]


def test_grok_event_map_is_honest() -> None:
    assert GROK_EVENT_MAP["prompt.submit"] == "unsupported"
    assert GROK_EVENT_MAP["tool.pre"] == "native_blocking"
    assert GROK_EVENT_MAP["stop.request"] == "native_blocking"
    assert GROK_EVENT_MAP["session.start"] == "native_passive"
    assert GROK_EVENT_MAP["subagent.stop"] == "native_passive"
    assert GROK_EVENT_MAP["tool.post"] == "unsupported"
    assert set(GROK_EVENT_MAP) == set(CANONICAL_EVENTS)


def test_prompt_submit_cannot_be_enabled_on_grok(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["id"] == "omg.prompt.submit.unsupported":
            hook["enabled"] = True
            hook["host_capability"] = "native_blocking"
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="UserPromptSubmit"):
        load_hooks_registry(tmp_path)


def test_duplicate_hook_id_fails_closed(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    raw["hooks"].append(dict(raw["hooks"][0]))
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="duplicate"):
        load_hooks_registry(tmp_path)


def test_bus_kill_switches() -> None:
    assert bus_disabled({"OMG_DISABLE_HOOKS": "1"}) is True
    assert bus_disabled({"DISABLE_OMG": "true"}) is True
    assert bus_disabled({}) is False
    assert skipped_hook_ids({"OMG_SKIP_HOOKS": "omg.pretool.deny, stop.request"}) == {
        "omg.pretool.deny",
        "stop.request",
    }


def test_inspect_reports_kill_switched_hooks_as_disabled() -> None:
    payload = inspect_hooks_registry(ROOT, env={})
    assert payload["ok"] is True
    assert payload["enabled"] is True
    assert payload["loadable"] is True
    disabled = inspect_hooks_registry(ROOT, env={"OMG_DISABLE_HOOKS": "1"})
    assert disabled["enabled"] is False
    assert disabled["loadable"] is True
    assert disabled["configured"] is True


def test_grok_passive_cannot_claim_blocking(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["event"] == "session.start":
            hook["host_capability"] = "native_blocking"
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="native_passive"):
        load_hooks_registry(tmp_path)


def test_dispatch_disabled_skips_all() -> None:
    result = dispatch("tool.pre", {}, root=ROOT, env={"OMG_DISABLE_HOOKS": "1"})
    assert result["ok"] is True
    assert result["skipped"] == "disabled"
    assert result["verified"] is False


def test_dispatch_skip_named_hook() -> None:
    result = dispatch(
        "tool.pre",
        {"toolName": "read_file"},
        root=ROOT,
        env={"OMG_SKIP_HOOKS": "omg.pretool.deny"},
    )
    assert result["ok"] is True
    assert any(row["status"] == "skipped" for row in result["results"])


def test_dispatch_legacy_skip_names() -> None:
    stop = dispatch("stop.request", {}, root=ROOT, env={"OMG_SKIP_HOOKS": "stop"})
    assert any(row["id"] == "omg.stop.gate" and row["status"] == "skipped" for row in stop["results"])
    pre = dispatch(
        "tool.pre",
        {"toolName": "read_file"},
        root=ROOT,
        env={"OMG_SKIP_HOOKS": "pre_tool_use"},
    )
    assert any(
        row["id"] == "omg.pretool.deny" and row["status"] == "skipped" for row in pre["results"]
    )


def test_aggregate_budget_fail_closed_does_not_false_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses
    import time

    from omg_cli import hooks_registry as hr

    monkeypatch.setattr(hr, "AGGREGATE_BUDGET_MS", 10)
    registry = load_hooks_registry(ROOT)
    guard = registry.by_id()["omg.continuation.guard"]
    decoy = dataclasses.replace(
        guard,
        id="omg.workflow.decoy",
        fail_policy="fail-open",
        priority=1,
        timeout_ms=5_000,
        handler="observe",
    )
    cloned = hr.HooksRegistry(schema=registry.schema, hooks=(decoy, guard))

    def slow(_record, _payload):
        time.sleep(0.05)
        return {"status": "ok"}

    def unused(_record, _payload):
        return {"status": "ok"}

    result = dispatch(
        "workflow.transition",
        {"active_owner": "ralph", "requested": "autopilot"},
        registry=cloned,
        handlers={"omg.workflow.decoy": slow, "omg.continuation.guard": unused},
        env={},
    )
    assert result["ok"] is False
    assert result["error"] == "E_HOOK_FAIL_CLOSED"
    row = next(item for item in result["results"] if item["id"] == "omg.continuation.guard")
    assert row["status"] == "budget_exceeded"
    assert row.get("fail_open") is not True


def test_dispatch_pretool_delegates_to_deny_without_weakening() -> None:
    result = dispatch(
        "tool.pre",
        {"toolName": "read_file"},
        root=ROOT,
        env={},
    )
    assert result["ok"] is True
    pre = next(row for row in result["results"] if row["id"] == "omg.pretool.deny")
    assert pre["status"] == "ok"
    assert pre["output"]["decision"] == "allow"


def test_dispatch_spawn_missing_capability_mode_still_denied() -> None:
    result = dispatch(
        "tool.pre",
        {
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "general-purpose", "prompt": "do work"},
        },
        root=ROOT,
        env={},
    )
    pre = next(row for row in result["results"] if row["id"] == "omg.pretool.deny")
    assert pre["status"] == "ok"
    assert pre["output"]["decision"] == "deny"
    assert "capability_mode" in pre["output"].get("reason", "")
    assert "RETRY IMMEDIATELY" in pre["output"].get("reason", "")


def test_dispatch_does_not_write_run_state(tmp_path: Path) -> None:
    dispatch("session.start", {"root": str(tmp_path)}, root=ROOT, env={})
    assert not (tmp_path / ".omg" / "state").exists()


def test_dispatch_fail_open_on_handler_crash() -> None:
    def boom(_record, _payload):
        raise RuntimeError("hook exploded")

    registry = load_hooks_registry(ROOT)
    result = dispatch(
        "session.start",
        {},
        root=ROOT,
        registry=registry,
        handlers={"omg.session.start.observe": boom},
        env={},
    )
    assert result["ok"] is True
    row = next(item for item in result["results"] if item["id"] == "omg.session.start.observe")
    assert row["fail_open"] is True
    assert row["status"] == "error"


def test_dispatch_fail_closed_continuation_error() -> None:
    def boom(_record, _payload):
        raise RuntimeError("guard failed")

    result = dispatch(
        "workflow.transition",
        {"active_owner": "ralph", "requested": "autopilot"},
        root=ROOT,
        handlers={"omg.continuation.guard": boom},
        env={},
    )
    assert result["ok"] is False
    assert result["error"] == "E_HOOK_FAIL_CLOSED"
    assert result["verified"] is False


def test_untrusted_hook_output_rejected() -> None:
    def bad(_record, _payload):
        return {"command": "rm -rf /", "decision": "allow"}

    result = dispatch(
        "session.start",
        {},
        root=ROOT,
        handlers={"omg.session.start.observe": bad},
        env={},
    )
    assert result["ok"] is True
    row = next(item for item in result["results"] if item["id"] == "omg.session.start.observe")
    assert row["status"] == "error"
    assert "command" in row["error"]


def test_oversized_hook_output_rejected() -> None:
    def huge(_record, _payload):
        return {"blob": "x" * 20_000}

    result = dispatch(
        "session.start",
        {},
        root=ROOT,
        handlers={"omg.session.start.observe": huge},
        env={},
    )
    row = next(item for item in result["results"] if item["id"] == "omg.session.start.observe")
    assert row["status"] == "error"


def test_timeout_fail_open() -> None:
    import dataclasses
    import time

    from omg_cli.hooks_registry import HooksRegistry

    def slow(_record, _payload):
        time.sleep(0.05)
        return {"status": "ok"}

    registry = load_hooks_registry(ROOT)
    observe = registry.by_id()["omg.session.start.observe"]
    fast = dataclasses.replace(observe, timeout_ms=1)
    others = [hook for hook in registry.hooks if hook.id != observe.id]
    cloned = HooksRegistry(schema=registry.schema, hooks=tuple(others + [fast]))
    result = dispatch(
        "session.start",
        {},
        registry=cloned,
        handlers={"omg.session.start.observe": slow},
        env={},
    )
    row = next(item for item in result["results"] if item["id"] == "omg.session.start.observe")
    assert row["status"] == "error"
    assert row["fail_open"] is True


def test_continuation_policies() -> None:
    assert resolve_continuation("ralph", "autopilot") == "refuse"
    assert resolve_continuation("autopilot", "ultraqa") == "adopt_existing"
    assert resolve_continuation("ralph", "cancel") == "adopt_existing"
    assert resolve_continuation("ralph", "wiki") == "artifact_only"
    assert resolve_continuation(None, "ralph") == "none"


def test_compact_handoff_refuses_transcript(tmp_path: Path) -> None:
    with pytest.raises(HooksRegistryError, match="transcript"):
        write_compact_handoff(
            tmp_path,
            run_id="run-1",
            session_id="sess-1",
            transcript="SECRET full chat",
        )
    payload = write_compact_handoff(
        tmp_path, run_id="run-1", session_id="sess-1", task_ids=["t1"]
    )
    assert payload["transcript_included"] is False
    assert payload["verified"] is False
    text = (tmp_path / ".omg" / "artifacts" / "compact-handoff.json").read_text(
        encoding="utf-8"
    )
    assert "SECRET" not in text
    assert "run-1" in text


def test_dispatch_compact_pre_writes_handoff(tmp_path: Path) -> None:
    result = dispatch(
        "compact.pre",
        {"root": str(tmp_path), "run_id": "run-9", "session_id": "sess-9"},
        root=ROOT,
        env={},
    )
    assert result["ok"] is True
    assert (tmp_path / ".omg" / "artifacts" / "compact-handoff.json").is_file()


def test_capabilities_command_embeds_hooks_registry() -> None:
    text = (ROOT / "omg_cli" / "commands" / "inspect.py").read_text(encoding="utf-8")
    assert "inspect_hooks_registry" in text
    assert '"hooks_registry": inspect_hooks_registry' in text


def test_projection_matches_committed() -> None:
    assert check_antigravity_projection(ROOT) == []
    write_antigravity_projection(ROOT)
    assert check_antigravity_projection(ROOT) == []


def test_hook_cannot_set_verified() -> None:
    def green(_record, _payload):
        return {"verified": True}

    result = dispatch(
        "session.start",
        {},
        root=ROOT,
        handlers={"omg.session.start.observe": green},
        env={},
    )
    row = next(item for item in result["results"] if item["id"] == "omg.session.start.observe")
    assert row["status"] == "error"
