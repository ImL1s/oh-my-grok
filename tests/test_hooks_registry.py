"""Host-neutral lifecycle registry (#72) — honest Grok mappings, fail-open bus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.hooks_registry import (
    BUS_EVENT_TYPES,
    CANONICAL_EVENTS,
    GROK_EVENT_MAP,
    HOST_HOOK_ALLOWLIST,
    REGISTRY_RELATIVE,
    TIMEOUT_KIND,
    WRAPPER_EMIT_EVENTS,
    WRAPPER_EVENT_SCHEMA,
    WRAPPER_SOURCE,
    HooksRegistryError,
    bus_disabled,
    check_antigravity_projection,
    dispatch,
    emit_wrapper_event,
    inspect_hooks_registry,
    install_antigravity_hook_projection,
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
    assert "emit_wrapper_event" in payload["note"]
    assert "source=wrapper" in payload["note"]


def test_grok_event_map_is_honest() -> None:
    assert GROK_EVENT_MAP["prompt.submit"] == "unsupported"
    assert GROK_EVENT_MAP["tool.pre"] == "native_blocking"
    assert GROK_EVENT_MAP["stop.request"] == "native_blocking"
    assert GROK_EVENT_MAP["session.start"] == "native_passive"
    assert GROK_EVENT_MAP["subagent.stop"] == "native_passive"
    assert GROK_EVENT_MAP["tool.post"] == "unsupported"
    assert GROK_EVENT_MAP["job.terminal"] == "wrapper"
    assert GROK_EVENT_MAP["team.member.transition"] == "wrapper"
    assert GROK_EVENT_MAP["artifact.created"] == "reconciled"
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


def test_unknown_runtime_projection_fails_closed(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["event"] == "session.start":
            hook["runtime_projection"] = "Grok"
            hook["host_capability"] = "native_blocking"
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="runtime_projection"):
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
    state = tmp_path / ".omg" / "state"
    assert not (state / "passes").exists()
    assert not (state / "verified").exists()
    assert not (state / "runs").exists()
    for path in state.rglob("*"):
        if path.is_file():
            assert "passes" not in path.parts
            assert path.name != "verified"


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
    assert row["timeout_kind"] == "post_hoc"
    assert isinstance(row["duration_ms"], int)
    assert row["duration_ms"] >= 1


def test_continuation_policies() -> None:
    assert resolve_continuation("ralph", "autopilot") == "refuse"
    assert resolve_continuation("autopilot", "ultraqa") == "adopt_existing"
    assert resolve_continuation("ralph", "cancel") == "adopt_existing"
    assert resolve_continuation("ralph", "wiki") == "artifact_only"
    assert resolve_continuation(None, "ralph") == "none"
    assert resolve_continuation("autopilot", "ralplan") == "adopt_existing"
    assert resolve_continuation("pipeline", "dual-review") == "adopt_existing"


def test_continuation_catalog_load_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.skills_catalog import SkillsCatalogError

    def boom(*_args, **_kwargs):
        raise SkillsCatalogError("missing catalog")

    monkeypatch.setattr("omg_cli.skills_catalog.load_skills_catalog", boom)
    with pytest.raises(HooksRegistryError, match="continuation catalog"):
        resolve_continuation("ralph", "autopilot")
    result = dispatch(
        "workflow.transition",
        {"active_owner": "ralph", "requested": "autopilot"},
        root=ROOT,
        env={},
    )
    assert result["ok"] is False
    assert result["error"] == "E_HOOK_FAIL_CLOSED"


def test_security_handler_binding_fails_closed(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["id"] == "omg.pretool.deny":
            hook["handler"] = "observe"
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="handler must be"):
        load_hooks_registry(tmp_path)


def test_disabled_security_hook_fails_closed(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["id"] == "omg.pretool.deny":
            hook["enabled"] = False
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="must be enabled"):
        load_hooks_registry(tmp_path)
    stub = load_hooks_registry(tmp_path, allow_incomplete=True)
    assert stub.by_id()["omg.pretool.deny"].enabled is False


def test_continuation_fail_policy_pin_fails_closed(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["id"] == "omg.continuation.guard":
            hook["fail_policy"] = "fail-open"
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="fail_policy must be"):
        load_hooks_registry(tmp_path)


def test_rebound_security_hook_event_fails_closed(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["id"] == "omg.stop.gate":
            hook["event"] = "idle"
            hook["host_hook"] = "Stop"
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="event must be"):
        load_hooks_registry(tmp_path)


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


def test_compact_handoff_refuses_symlink(tmp_path: Path) -> None:
    dest_dir = tmp_path / ".omg" / "artifacts"
    dest_dir.mkdir(parents=True)
    target = tmp_path / "outside.txt"
    target.write_text("secret\n", encoding="utf-8")
    dest = dest_dir / "compact-handoff.json"
    try:
        dest.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not available")
    with pytest.raises(HooksRegistryError, match="symlink"):
        write_compact_handoff(tmp_path, run_id="run-1", session_id="sess-1")
    assert target.read_text(encoding="utf-8") == "secret\n"


def test_compact_handoff_refuses_symlinked_omg(tmp_path: Path) -> None:
    outside = tmp_path / "outside-omg"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    try:
        (tmp_path / ".omg").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not available")
    with pytest.raises(HooksRegistryError, match="refused|symlink"):
        write_compact_handoff(tmp_path, run_id="run-1", session_id="sess-1")
    assert not (outside / "artifacts" / "compact-handoff.json").exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret\n"


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
    assert result["verified"] is False


def test_host_hook_allowlist_covers_canonical_events() -> None:
    assert set(HOST_HOOK_ALLOWLIST) == set(CANONICAL_EVENTS)
    assert set(GROK_EVENT_MAP) == set(CANONICAL_EVENTS)


def test_unknown_host_hook_fails_closed(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["id"] == "omg.pretool.deny":
            hook["host_hook"] = "NotARealHook"
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="host_hook"):
        load_hooks_registry(tmp_path)


def test_reconciled_event_rejects_unknown_host_hook(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    for hook in raw["hooks"]:
        if hook["id"] == "omg.continuation.guard":
            hook["host_hook"] = "Stop"
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="host_hook"):
        load_hooks_registry(tmp_path)


def test_missing_continuation_guard_fails_closed(tmp_path: Path) -> None:
    raw = json.loads((ROOT / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    raw["hooks"] = [hook for hook in raw["hooks"] if hook["id"] != "omg.continuation.guard"]
    _write(tmp_path / REGISTRY_RELATIVE, json.dumps(raw))
    with pytest.raises(HooksRegistryError, match="omg.continuation.guard"):
        load_hooks_registry(tmp_path)
    stub = load_hooks_registry(tmp_path, allow_incomplete=True)
    assert "omg.continuation.guard" not in stub.by_id()


def test_dispatch_appends_monotonic_journal(tmp_path: Path) -> None:
    from omg_cli.runtime_events import BUS_SOURCE, read_runtime_events, source_journal_path

    first = dispatch(
        "session.start",
        {"root": str(tmp_path), "run_id": "run-1", "session_id": "sess-1"},
        root=ROOT,
        env={},
    )
    second = dispatch(
        "session.start",
        {"root": str(tmp_path), "run_id": "run-1", "session_id": "sess-1"},
        root=ROOT,
        env={},
    )
    assert first["journal"]["ok"] is True
    assert first["journal"]["source_sequence"] == 0
    assert second["journal"]["source_sequence"] == 1
    assert first["timeout_kind"] == TIMEOUT_KIND
    assert isinstance(first["duration_ms"], int)
    path = source_journal_path(tmp_path, BUS_SOURCE)
    rows = read_runtime_events(path)
    assert [row["source_sequence"] for row in rows] == [0, 1]
    assert all(row["payload"]["timeout_kind"] == "post_hoc" for row in rows)
    assert all(row["payload"].get("verified") is False for row in rows)
    assert "duration_ms" in rows[0]["payload"]


def test_handler_crash_does_not_corrupt_journal(tmp_path: Path) -> None:
    from omg_cli.runtime_events import BUS_SOURCE, read_runtime_events, source_journal_path

    def boom(_record, _payload):
        raise RuntimeError("hook exploded")

    crashed = dispatch(
        "session.start",
        {"root": str(tmp_path)},
        root=ROOT,
        handlers={"omg.session.start.observe": boom},
        env={},
    )
    assert crashed["ok"] is True
    assert crashed["verified"] is False
    path = source_journal_path(tmp_path, BUS_SOURCE)
    rows = read_runtime_events(path)
    assert len(rows) == 1
    assert rows[0]["source_sequence"] == 0
    follow = dispatch("session.start", {"root": str(tmp_path)}, root=ROOT, env={})
    assert follow["journal"]["ok"] is True
    rows = read_runtime_events(path)
    assert [row["source_sequence"] for row in rows] == [0, 1]


def test_journal_write_failure_is_fail_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("omg_cli.runtime_events.append_bus_event", boom)
    result = dispatch(
        "session.start",
        {"root": str(tmp_path)},
        root=ROOT,
        env={},
    )
    assert result["ok"] is True
    assert result["verified"] is False
    assert result["journal"]["ok"] is False
    assert result["journal"]["fail_open"] is True
    assert not (tmp_path / ".omg" / "state" / "passes").exists()
    assert not (tmp_path / ".omg" / "state" / "verified").exists()


def test_journal_does_not_persist_payload_secrets(tmp_path: Path) -> None:
    from omg_cli.runtime_events import BUS_SOURCE, source_journal_path

    dispatch(
        "session.start",
        {
            "root": str(tmp_path),
            "Authorization": "Bearer raw-token",
            "prompt": "SECRET USER TEXT",
        },
        root=ROOT,
        env={},
    )
    text = source_journal_path(tmp_path, BUS_SOURCE).read_text(encoding="utf-8")
    assert "raw-token" not in text
    assert "SECRET USER TEXT" not in text


def test_disable_and_skip_still_work_with_journal(tmp_path: Path) -> None:
    from omg_cli.runtime_events import BUS_SOURCE, source_journal_path

    disabled = dispatch(
        "tool.pre",
        {"root": str(tmp_path), "toolName": "read_file"},
        root=ROOT,
        env={"OMG_DISABLE_HOOKS": "1"},
    )
    assert disabled["ok"] is True
    assert disabled["skipped"] == "disabled"
    assert disabled["verified"] is False
    assert "journal" not in disabled
    assert not source_journal_path(tmp_path, BUS_SOURCE).exists()
    skipped = dispatch(
        "tool.pre",
        {"root": str(tmp_path), "toolName": "read_file"},
        root=ROOT,
        env={"OMG_SKIP_HOOKS": "omg.pretool.deny"},
    )
    assert skipped["ok"] is True
    assert any(row["status"] == "skipped" for row in skipped["results"])
    assert skipped["verified"] is False


def test_concurrent_dispatch_sequence(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from omg_cli.runtime_events import BUS_SOURCE, read_runtime_events, source_journal_path

    count = 12

    def go(_index: int) -> dict:
        return dispatch("session.start", {"root": str(tmp_path)}, root=ROOT, env={})

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(go, index) for index in range(count)]
        results = [future.result() for future in as_completed(futures)]
    assert all(row["ok"] is True for row in results)
    assert all(row["verified"] is False for row in results)
    ok_journals = [row for row in results if row.get("journal", {}).get("ok")]
    errors = [row.get("journal") for row in results if not row.get("journal", {}).get("ok")]
    assert len(ok_journals) == count, errors
    sequences = sorted(row["journal"]["source_sequence"] for row in ok_journals)
    assert sequences == list(range(count))
    rows = read_runtime_events(source_journal_path(tmp_path, BUS_SOURCE))
    assert sorted(row["source_sequence"] for row in rows) == list(range(count))


def test_install_antigravity_hook_projection(tmp_path: Path) -> None:
    dest = tmp_path / "ag-hooks"
    dry = install_antigravity_hook_projection(dest, root=ROOT, dry_run=True)
    assert dry["ok"] is True
    assert dry["dry_run"] is True
    assert dry["verified"] is False
    assert dry["live_ag"] is False
    assert not dest.exists()
    applied = install_antigravity_hook_projection(dest, root=ROOT, dry_run=False)
    assert applied["live_ag"] is False
    assert applied["verified"] is False
    assert (dest / "README.md").is_file()
    assert (dest / "hooks.json").is_file()
    data = json.loads((dest / "hooks.json").read_text(encoding="utf-8"))
    assert data["live_ag"] is False
    assert data["verified"] is False
    assert "UserPromptSubmit" not in data["hooks"]
    assert "PreToolUse" in data["hooks"]
    assert data["kind"] == "static_projection"
    assert "not proof that agy loaded hooks" in data["note"]


def test_bus_event_types_keep_stop_and_tool_failure_nonterminal() -> None:
    assert BUS_EVENT_TYPES["stop.request"] == "turn_started"
    assert BUS_EVENT_TYPES["idle"] == "turn_started"
    assert BUS_EVENT_TYPES["tool.failure"] == "turn_completed"
    assert BUS_EVENT_TYPES["session.end"] == "agent_closed"
    assert BUS_EVENT_TYPES["subagent.stop"] == "agent_closed"


def _wrapper_rows(tmp_path: Path) -> list[dict]:
    from omg_cli.runtime_events import read_runtime_events, source_journal_path

    return read_runtime_events(source_journal_path(tmp_path, WRAPPER_SOURCE))


def test_wrapper_observe_hooks_are_registered() -> None:
    registry = load_hooks_registry(ROOT)
    ids = registry.by_id()
    assert ids["omg.artifact.created.observe"].host_capability == "reconciled"
    assert ids["omg.job.terminal.observe"].host_capability == "wrapper"
    assert ids["omg.team.member.transition.observe"].host_capability == "wrapper"
    for hook_id in (
        "omg.artifact.created.observe",
        "omg.job.terminal.observe",
        "omg.team.member.transition.observe",
    ):
        hook = ids[hook_id]
        assert hook.enabled is True
        assert hook.host_hook is None
        assert hook.runtime_projection == "omg-cli"
        assert hook.handler == "observe"
        assert hook.fail_policy == "fail-open"
    assert WRAPPER_EMIT_EVENTS <= set(CANONICAL_EVENTS)


def test_emit_wrapper_event_refuses_prompt_submit(tmp_path: Path) -> None:
    with pytest.raises(HooksRegistryError, match="UserPromptSubmit"):
        emit_wrapper_event(
            "prompt.submit",
            {"root": str(tmp_path), "prompt": "do not inject"},
        )
    with pytest.raises(HooksRegistryError, match="not a wrapper-emitted event"):
        emit_wrapper_event("session.end", {"root": str(tmp_path)})


def test_emit_wrapper_event_journals_kinds_schema_and_post_hoc(tmp_path: Path) -> None:
    (tmp_path / ".omg" / "state").mkdir(parents=True)
    for kind, extra in (
        (
            "artifact.created",
            {"kind": "omg.test.artifact.v1", "path": ".omg/artifacts/x.json"},
        ),
        ("job.terminal", {"job_id": "20990101T000000Z-abcd1234", "from": "running", "to": "succeeded"}),
        (
            "team.member.transition",
            {"worker": "w1", "from": "missing", "to": "live", "reason": "heartbeat"},
        ),
    ):
        result = emit_wrapper_event(
            kind,
            {"root": str(tmp_path), "run_id": "run-wrap", **extra},
        )
        assert result["ok"] is True
        assert result["verified"] is False
        assert result["source"] == WRAPPER_SOURCE
        assert result["timeout_kind"] == TIMEOUT_KIND
        assert result["journal"]["ok"] is True
        assert result["schema"] == WRAPPER_EVENT_SCHEMA

    rows = _wrapper_rows(tmp_path)
    kinds = [row["payload"]["canonical_event"] for row in rows]
    assert kinds == ["artifact.created", "job.terminal", "team.member.transition"]
    for row in rows:
        assert row["source"] == WRAPPER_SOURCE
        assert row["payload"]["source"] == WRAPPER_SOURCE
        assert row["payload"]["schema"] == WRAPPER_EVENT_SCHEMA
        assert row["payload"]["timeout_kind"] == "post_hoc"
        assert row["payload"].get("verified") is False
        assert row["payload"].get("passes") is not True
        assert isinstance(row["payload"].get("duration_ms"), int)


def test_emit_wrapper_event_redacts_secrets_and_omits_verified(
    tmp_path: Path,
) -> None:
    from omg_cli.runtime_events import source_journal_path

    (tmp_path / ".omg" / "state").mkdir(parents=True)
    emit_wrapper_event(
        "job.terminal",
        {
            "root": str(tmp_path),
            "job_id": "20990101T000000Z-abcd1234",
            "from": "running",
            "to": "failed",
            "Authorization": "Bearer raw-token",
            "prompt": "SECRET USER TEXT",
            "owner_token": "deadbeefcafebabe",
            "verified": True,
            "passes": True,
        },
    )
    path = source_journal_path(tmp_path, WRAPPER_SOURCE)
    text = path.read_text(encoding="utf-8")
    assert "raw-token" not in text
    assert "SECRET USER TEXT" not in text
    assert "deadbeefcafebabe" not in text
    assert "job.terminal" in text
    row = _wrapper_rows(tmp_path)[0]
    assert row["payload"].get("verified") is False
    assert "passes" not in row["payload"]
    assert row["payload"]["to"] == "failed"


def test_compact_handoff_emits_artifact_created(tmp_path: Path) -> None:
    (tmp_path / ".omg" / "state").mkdir(parents=True)
    payload = write_compact_handoff(
        tmp_path, run_id="run-9", session_id="sess-9", task_ids=["t1"]
    )
    assert payload["verified"] is False
    rows = _wrapper_rows(tmp_path)
    created = [row for row in rows if row["payload"]["canonical_event"] == "artifact.created"]
    assert created
    assert created[0]["source"] == WRAPPER_SOURCE
    assert created[0]["payload"]["path"] == ".omg/artifacts/compact-handoff.json"
    assert created[0]["payload"].get("verified") is False


def test_write_edit_artifact_emits_artifact_created(tmp_path: Path) -> None:
    from omg_cli.edit_hygiene.artifacts import ARTIFACT_KIND, write_edit_artifact

    (tmp_path / ".omg" / "state").mkdir(parents=True)
    rel = write_edit_artifact(
        tmp_path, {"kind": ARTIFACT_KIND, "note": "classified", "secret": "nope"}
    )
    assert rel.startswith(".omg/artifacts/edit/")
    rows = _wrapper_rows(tmp_path)
    created = [row for row in rows if row["payload"]["canonical_event"] == "artifact.created"]
    assert created
    assert created[0]["payload"]["path"] == rel
    assert created[0]["payload"]["kind"] == ARTIFACT_KIND
    text = (
        tmp_path / ".omg" / "state" / "events"
    )
    dumped = "".join(
        path.read_text(encoding="utf-8") for path in text.glob("*.jsonl")
    )
    assert "nope" not in dumped


def test_team_shutdown_ack_emits_member_transition(tmp_path: Path) -> None:
    from omg_cli.team.api import _op_write_shutdown_ack

    envelope = _op_write_shutdown_ack(
        tmp_path, {"run_id": "run-1", "worker": "worker-1"}
    )
    assert envelope["ok"] is True
    rows = _wrapper_rows(tmp_path)
    kinds = [row["payload"]["canonical_event"] for row in rows]
    assert "team.member.transition" in kinds
    row = next(item for item in rows if item["payload"]["canonical_event"] == "team.member.transition")
    assert row["source"] == WRAPPER_SOURCE
    assert row["payload"]["worker"] == "worker-1"
    assert row["payload"]["to"] == "shutdown_acked"
    assert row["payload"].get("verified") is False


def test_team_heartbeat_emits_member_transition_on_status_change(
    tmp_path: Path,
) -> None:
    from omg_cli.team.api import _op_update_worker_heartbeat

    envelope = _op_update_worker_heartbeat(
        tmp_path,
        {
            "run_id": "run-1",
            "team_id": "team-1",
            "worker": "worker-1",
            "task_id": "worker-1",
            "generation": 0,
            "expected_sequence": 0,
        },
    )
    assert envelope["ok"] is True
    rows = _wrapper_rows(tmp_path)
    row = next(
        item
        for item in rows
        if item["payload"]["canonical_event"] == "team.member.transition"
    )
    assert row["payload"]["from"] == "missing"
    assert row["payload"]["to"] == "live"
    assert row["payload"]["reason"] == "heartbeat"
    assert row["source"] == WRAPPER_SOURCE


def test_emit_wrapper_event_disabled_bus_skips_journal(tmp_path: Path) -> None:
    from omg_cli.runtime_events import source_journal_path

    result = emit_wrapper_event(
        "job.terminal",
        {"root": str(tmp_path), "job_id": "20990101T000000Z-abcd1234", "to": "failed"},
        env={"OMG_DISABLE_HOOKS": "1"},
    )
    assert result["ok"] is True
    assert result["skipped"] == "disabled"
    assert result["verified"] is False
    assert not source_journal_path(tmp_path, WRAPPER_SOURCE).exists()

