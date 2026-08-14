"""Fail-closed plugin agent catalog (#71) — not a routing runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.agents_catalog import (
    ALLOWED_CAPABILITY_MODES,
    ANTIGRAVITY_PROJECTION_ROOT,
    CATALOG_RELATIVE,
    FORBIDDEN_CAPABILITY_MODES,
    PROJECTION_BANNER_NEEDLES,
    AgentsCatalogError,
    check_antigravity_projections,
    inspect_agents_catalog,
    load_agents_catalog,
    plugin_root,
    render_antigravity_projections,
    write_antigravity_projections,
)
from omg_cli.team.roles import role_posture

ROOT = Path(__file__).resolve().parents[1]

_READ_ONLY_EXPLORE_LIKE = frozenset(
    {
        "omg-analyst",
        "omg-architect",
        "omg-code-reviewer",
        "omg-critic",
        "omg-security-reviewer",
        "omg-verifier",
    }
)
_READ_WRITE_IMPLEMENTERS = frozenset(
    {
        "omg-debugger",
        "omg-designer",
        "omg-executor",
        "omg-qa-tester",
        "omg-test-engineer",
        "omg-writer",
    }
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stub_agent(root: Path, agent_id: str, *, mode: str = "read-only") -> None:
    perm = "plan" if mode == "read-only" else "default"
    _write(
        root / "agents" / f"{agent_id}.md",
        f"---\nname: {agent_id}\ncapabilityMode: {mode}\n"
        f"permissionMode: {perm}\n---\n# {agent_id}\n",
    )


def _projection_rel(agent_id: str) -> str:
    return f"{ANTIGRAVITY_PROJECTION_ROOT}/{agent_id}/agent.md"


def _agent_entry(
    agent_id: str,
    *,
    capability_mode: str,
    permission_mode: str,
    tier: str,
    spawn_policy: str,
) -> dict:
    return {
        "id": agent_id,
        "file": f"agents/{agent_id}.md",
        "capability_mode": capability_mode,
        "permission_mode": permission_mode,
        "tier": tier,
        "spawn_policy": spawn_policy,
        "projections": {
            "grok": {"kind": "plugin_agent", "path": f"agents/{agent_id}.md"},
            "antigravity": {
                "kind": "agent_md_projection",
                "path": _projection_rel(agent_id),
            },
        },
    }


def _write_catalog(root: Path, agents: list[dict]) -> None:
    payload = {
        "schema": "omg-agents-catalog/v1",
        "kind": "read_only_machine_catalog",
        "agents": agents,
    }
    _write(root / CATALOG_RELATIVE, json.dumps(payload, indent=2) + "\n")


def test_catalog_ids_match_plugin_agent_files_on_disk() -> None:
    catalog = load_agents_catalog(ROOT)
    disk = sorted(p.stem for p in (ROOT / "agents").glob("omg-*.md") if p.is_file())
    assert [record.id for record in catalog.agents] == disk
    assert len(catalog.agents) == 13
    assert disk == [
        "omg-analyst",
        "omg-architect",
        "omg-code-reviewer",
        "omg-critic",
        "omg-debugger",
        "omg-designer",
        "omg-executor",
        "omg-orchestrator",
        "omg-qa-tester",
        "omg-security-reviewer",
        "omg-test-engineer",
        "omg-verifier",
        "omg-writer",
    ]


def test_explore_like_roles_are_read_only() -> None:
    catalog = load_agents_catalog(ROOT)
    by_id = catalog.by_id()
    for agent_id in _READ_ONLY_EXPLORE_LIKE:
        record = by_id[agent_id]
        assert record.capability_mode == "read-only"
        assert record.tier in {"reviewer", "verifier", "planner"}
        assert record.spawn_policy == "leaf"
        assert record.capability_mode not in FORBIDDEN_CAPABILITY_MODES


def test_implementers_are_read_write() -> None:
    catalog = load_agents_catalog(ROOT)
    by_id = catalog.by_id()
    for agent_id in _READ_WRITE_IMPLEMENTERS:
        record = by_id[agent_id]
        assert record.capability_mode == "read-write"
        assert record.tier == "implementer"
        assert record.spawn_policy == "leaf"
    orchestrator = by_id["omg-orchestrator"]
    assert orchestrator.capability_mode == "read-write"
    assert orchestrator.tier == "orchestrator"
    assert orchestrator.spawn_policy == "parent"
    for record in catalog.agents:
        assert record.capability_mode in ALLOWED_CAPABILITY_MODES
        assert record.capability_mode not in FORBIDDEN_CAPABILITY_MODES


def test_catalog_capability_matches_team_role_posture() -> None:
    catalog = load_agents_catalog(ROOT)
    for record in catalog.agents:
        short = record.id.removeprefix("omg-")
        assert role_posture(short) == record.capability_mode


def test_missing_agent_file_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="missing agent"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_duplicate_id_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _stub_agent(tmp_path, "omg-executor", mode="read-write")
    _write_catalog(tmp_path, [entry, dict(entry)])
    with pytest.raises(AgentsCatalogError, match="duplicate agent id"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_execute_and_all_capability_modes_fail_closed(tmp_path: Path) -> None:
    _stub_agent(tmp_path, "omg-executor", mode="read-write")
    for bad in ("execute", "all", "write-only"):
        entry = _agent_entry(
            "omg-executor",
            capability_mode=bad,
            permission_mode="default",
            tier="implementer",
            spawn_policy="leaf",
        )
        _write_catalog(tmp_path, [entry])
        with pytest.raises(AgentsCatalogError, match="capability_mode"):
            load_agents_catalog(tmp_path, require_projections=False)


def test_uncatalogued_disk_agent_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _stub_agent(tmp_path, "omg-executor", mode="read-write")
    _stub_agent(tmp_path, "omg-critic", mode="read-only")
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="uncatalogued"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_missing_catalog_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AgentsCatalogError, match="missing agent catalog"):
        load_agents_catalog(tmp_path)


def test_missing_antigravity_projection_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _stub_agent(tmp_path, "omg-executor", mode="read-write")
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="missing antigravity projection"):
        load_agents_catalog(tmp_path, require_projections=True)


def test_antigravity_projections_are_labeled_and_current() -> None:
    catalog = load_agents_catalog(ROOT)
    assert check_antigravity_projections(ROOT) == []
    for record in catalog.agents:
        path = ROOT / record.projections["antigravity"].path
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for needle in PROJECTION_BANNER_NEEDLES:
            assert needle in lowered
        assert "not an installed antigravity plugin" in lowered
        assert "`agy` install" in lowered or "agy install" in lowered
        assert f"omg_capability_mode: {record.capability_mode}" in text
        assert "live_call_ready" not in lowered
        assert record.id in text


def test_projection_renderer_roundtrip(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-verifier",
        capability_mode="read-only",
        permission_mode="plan",
        tier="verifier",
        spawn_policy="leaf",
    )
    _stub_agent(tmp_path, "omg-verifier", mode="read-only")
    _write_catalog(tmp_path, [entry])
    written = write_antigravity_projections(tmp_path)
    assert any(item.endswith("omg-verifier/agent.md") for item in written)
    catalog = load_agents_catalog(tmp_path)
    assert catalog.agents[0].id == "omg-verifier"
    rendered = render_antigravity_projections(tmp_path, catalog=catalog)
    rel = _projection_rel("omg-verifier")
    assert "PROJECTION" in rendered[rel]
    assert check_antigravity_projections(tmp_path) == []


def test_inspect_payload_never_claims_verified() -> None:
    payload = inspect_agents_catalog(ROOT)
    assert payload["ok"] is True
    assert payload["verified"] is False
    assert payload["healthy"] is False
    assert payload["observed"] is False
    assert payload["agent_count"] == 13
    assert "projections only" in payload["note"]


def test_capabilities_command_embeds_agents_catalog() -> None:
    text = (ROOT / "omg_cli" / "commands" / "inspect.py").read_text(encoding="utf-8")
    assert "inspect_agents_catalog" in text
    assert '"agents_catalog": inspect_agents_catalog' in text
    payload = inspect_agents_catalog(plugin_root())
    assert payload["ok"] is True
    assert payload["agent_count"] == 13
    assert payload["verified"] is False


def test_doctor_agents_check_consumes_catalog() -> None:
    from omg_cli.doctor import check_agents_present

    name, ok, detail = check_agents_present()
    assert name == "agents"
    assert ok is True
    assert "13 catalogued present" in detail
