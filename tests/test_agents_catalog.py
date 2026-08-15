"""Fail-closed plugin agent catalog (#71) — not a routing runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omg_cli.agents_catalog import (
    ALLOWED_CAPABILITY_MODES,
    ANTIGRAVITY_PROJECTION_ROOT,
    CATALOG_RELATIVE,
    FORBIDDEN_CAPABILITY_MODES,
    PROJECTION_BANNER_NEEDLES,
    SCHEMA,
    AgentRecord,
    AgentsCatalog,
    AgentsCatalogError,
    HostProjection,
    YAML_RELATIVE,
    assert_agent_capability,
    check_antigravity_projections,
    inspect_agents_catalog,
    load_agents_catalog,
    load_yaml_catalog_document,
    lookup_agent,
    plugin_root,
    render_antigravity_projections,
    render_handoff,
    resolve_category,
    write_antigravity_projections,
    _read_plugin_regular_text,
)
from omg_cli.catalog_yaml import dump_yaml, parse_yaml
from omg_cli.team.roles import role_posture

ROOT = Path(__file__).resolve().parents[1]
_PIN_READY = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


@pytest.fixture(autouse=True)
def _skip_without_agent_pin(request: pytest.FixtureRequest) -> None:
    name = request.node.name
    if name.startswith("test_catalog_pin_unavailable"):
        return
    if name.startswith(
        (
            "test_yaml_",
            "test_resolve_category",
            "test_assert_agent_",
            "test_render_handoff",
            "test_alias_",
            "test_reviewer_cannot",
            "test_write_antigravity",
        )
    ):
        return
    if not _PIN_READY:
        pytest.skip("agent file pin requires POSIX O_NOFOLLOW/dir_fd")


_READ_ONLY_EXPLORE_LIKE = frozenset(
    {
        "omg-analyst",
        "omg-architect",
        "omg-code-reviewer",
        "omg-critic",
        "omg-document-specialist",
        "omg-explore",
        "omg-planner",
        "omg-product-manager",
        "omg-scientist",
        "omg-security-reviewer",
        "omg-tracer",
        "omg-verifier",
        "omg-vision",
    }
)
_READ_WRITE_IMPLEMENTERS = frozenset(
    {
        "omg-build-fixer",
        "omg-code-simplifier",
        "omg-debugger",
        "omg-designer",
        "omg-executor",
        "omg-git-master",
        "omg-qa-tester",
        "omg-test-engineer",
        "omg-writer",
    }
)
_EXPECTED_IDS = tuple(
    sorted(_READ_ONLY_EXPLORE_LIKE | _READ_WRITE_IMPLEMENTERS | {"omg-orchestrator"})
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
    assert [record.id for record in catalog.agents] == list(_EXPECTED_IDS)
    assert len(catalog.agents) == 23
    assert disk == list(_EXPECTED_IDS)


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


def test_frontmatter_snake_capability_mode_alias_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _write(
        tmp_path / "agents" / "omg-executor.md",
        "---\nname: omg-executor\ncapability_mode: read-write\n"
        "permissionMode: default\n---\n# x\n",
    )
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="must use capabilityMode"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_frontmatter_snake_permission_mode_alias_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _write(
        tmp_path / "agents" / "omg-executor.md",
        "---\nname: omg-executor\ncapabilityMode: read-write\n"
        "permission_mode: default\n---\n# x\n",
    )
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="must use permissionMode"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_catalog_pin_unavailable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omg_cli.agents_catalog._posix_nofollow_ready", lambda: False
    )
    with pytest.raises(AgentsCatalogError, match="O_NOFOLLOW"):
        load_agents_catalog(ROOT, require_projections=False)


def test_read_plugin_regular_text_rejects_symlink(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: omg-executor\ncapabilityMode: read-write\n"
        "permissionMode: default\n---\n# leaked\n",
        encoding="utf-8",
    )
    (agents / "omg-executor.md").symlink_to(outside)
    with pytest.raises(AgentsCatalogError, match="missing agent"):
        _read_plugin_regular_text(tmp_path, "agents/omg-executor.md")


def test_read_plugin_regular_text_rejects_fifo(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    os.mkfifo(agents / "omg-executor.md")
    with pytest.raises(AgentsCatalogError, match="missing agent"):
        _read_plugin_regular_text(tmp_path, "agents/omg-executor.md")


def test_frontmatter_omitted_capability_mode_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _write(
        tmp_path / "agents" / "omg-executor.md",
        "---\nname: omg-executor\npermissionMode: default\n---\n# x\n",
    )
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="missing capabilityMode"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_frontmatter_capability_mode_mismatch_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _stub_agent(tmp_path, "omg-executor", mode="read-only")
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="does not match catalog"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_frontmatter_forbidden_capability_mode_fails_closed(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-executor",
        capability_mode="read-write",
        permission_mode="default",
        tier="implementer",
        spawn_policy="leaf",
    )
    _write(
        tmp_path / "agents" / "omg-executor.md",
        "---\nname: omg-executor\ncapabilityMode: execute\n"
        "permissionMode: default\n---\n# x\n",
    )
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="forbidden"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_plugin_orchestrator_frontmatter_matches_catalog() -> None:
    catalog = load_agents_catalog(ROOT)
    text = (ROOT / "agents" / "omg-orchestrator.md").read_text(encoding="utf-8")
    assert "capabilityMode: read-write" in text
    assert catalog.by_id()["omg-orchestrator"].capability_mode == "read-write"


def test_inspect_payload_never_claims_verified() -> None:
    payload = inspect_agents_catalog(ROOT)
    assert payload["ok"] is True
    assert payload["verified"] is False
    assert payload["healthy"] is False
    assert payload["observed"] is False
    assert payload["agent_count"] == 23
    assert "projections only" in payload["note"]
    assert payload["yaml_source"] == "agents/catalog.yaml"
    routed = {row["category"]: row["role_id"] for row in payload["category_routing"]}
    assert routed["quick"] == "omg-explore"
    assert routed["review"] == "omg-code-reviewer"
    assert payload["aliases"]["sisyphus"] == "omg-orchestrator"


def test_capabilities_command_embeds_agents_catalog() -> None:
    text = (ROOT / "omg_cli" / "commands" / "inspect.py").read_text(encoding="utf-8")
    assert "inspect_agents_catalog" in text
    assert '"agents_catalog": inspect_agents_catalog' in text
    payload = inspect_agents_catalog(plugin_root())
    assert payload["ok"] is True
    assert payload["agent_count"] == 23
    assert payload["verified"] is False


def test_doctor_agents_check_consumes_catalog() -> None:
    from omg_cli.doctor import check_agents_catalog, check_agents_present

    name, ok, detail = check_agents_present()
    assert name == "agents"
    assert ok is True
    assert "23 catalogued present" in detail
    assert "not live" in detail
    catalog_name, catalog_ok, catalog_detail = check_agents_catalog()
    assert catalog_name == "agents"
    assert catalog_ok is True
    assert catalog_detail == detail


def _record(
    agent_id: str,
    *,
    mode: str,
    tier: str,
    spawn: str = "leaf",
    aliases: tuple[str, ...] = (),
    profile: str = "",
) -> AgentRecord:
    perm = "plan" if mode == "read-only" else "default"
    grok = HostProjection(kind="plugin_agent", path=f"agents/{agent_id}.md")
    ag = HostProjection(
        kind="agent_md_projection",
        path=_projection_rel(agent_id),
    )
    return AgentRecord(
        id=agent_id,
        file=f"agents/{agent_id}.md",
        capability_mode=mode,
        permission_mode=perm,
        tier=tier,
        spawn_policy=spawn,
        projections={"grok": grok, "antigravity": ag},
        aliases=aliases,
        profile=profile,
    )


def _routing_catalog() -> AgentsCatalog:
    return AgentsCatalog(
        schema=SCHEMA,
        agents=(
            _record("omg-analyst", mode="read-only", tier="planner"),
            _record("omg-architect", mode="read-only", tier="planner"),
            _record(
                "omg-code-reviewer",
                mode="read-only",
                tier="reviewer",
                aliases=("style-reviewer", "quality-reviewer", "api-reviewer"),
            ),
            _record("omg-critic", mode="read-only", tier="reviewer"),
            _record("omg-designer", mode="read-write", tier="implementer"),
            _record(
                "omg-document-specialist",
                mode="read-only",
                tier="planner",
                aliases=("librarian",),
            ),
            _record(
                "omg-executor",
                mode="read-write",
                tier="implementer",
                aliases=("hephaestus",),
            ),
            _record(
                "omg-explore",
                mode="read-only",
                tier="planner",
                aliases=("explore",),
                profile="explore-high",
            ),
            _record(
                "omg-orchestrator",
                mode="read-write",
                tier="orchestrator",
                spawn="parent",
                aliases=("sisyphus",),
            ),
            _record("omg-planner", mode="read-only", tier="planner"),
            _record("omg-scientist", mode="read-only", tier="planner"),
            _record(
                "omg-security-reviewer",
                mode="read-only",
                tier="reviewer",
                aliases=("security-reviewer-high",),
            ),
            _record("omg-tracer", mode="read-only", tier="planner"),
            _record("omg-verifier", mode="read-only", tier="verifier"),
            _record("omg-vision", mode="read-only", tier="reviewer"),
        ),
    )


def test_yaml_roundtrip_golden() -> None:
    text = (ROOT / YAML_RELATIVE).read_text(encoding="utf-8")
    parsed = parse_yaml(text)
    again = parse_yaml(dump_yaml(parsed))
    assert again == parsed
    assert parsed["schema"] == SCHEMA
    ids = [row["id"] for row in parsed["agents"]]
    assert ids == sorted(ids)
    assert "omg-explore" in ids
    assert len(ids) == 23


def test_alias_lookup_maps_branded_names() -> None:
    catalog = _routing_catalog()
    assert lookup_agent("sisyphus", catalog).id == "omg-orchestrator"
    assert lookup_agent("hephaestus", catalog).id == "omg-executor"
    assert lookup_agent("explore", catalog).id == "omg-explore"
    assert lookup_agent("librarian", catalog).id == "omg-document-specialist"
    assert lookup_agent("style-reviewer", catalog).id == "omg-code-reviewer"
    assert lookup_agent("security-reviewer-high", catalog).id == "omg-security-reviewer"


@pytest.mark.parametrize(
    "category,role_id,mode",
    [
        ("quick", "omg-explore", "read-only"),
        ("deep", "omg-planner", "read-only"),
        ("ultrabrain", "omg-scientist", "read-only"),
        ("visual-engineering", "omg-vision", "read-only"),
        ("research", "omg-document-specialist", "read-only"),
        ("review", "omg-code-reviewer", "read-only"),
    ],
)
def test_resolve_category_primary(category: str, role_id: str, mode: str) -> None:
    resolved = resolve_category(category, catalog=_routing_catalog())
    assert resolved["role_id"] == role_id
    assert resolved["capability_mode"] == mode
    assert resolved["fallbacks"] == []
    if category == "quick":
        assert resolved["profile"] == "explore-high"


def test_resolve_category_unavailable_fallback() -> None:
    catalog = _routing_catalog()
    resolved = resolve_category(
        "quick",
        catalog=catalog,
        available={"omg-analyst", "omg-planner", "omg-architect"},
    )
    assert resolved["role_id"] == "omg-analyst"
    assert resolved["capability_mode"] == "read-only"
    assert "omg-explore" in resolved["fallbacks"]


def test_resolve_category_never_downgrades_readonly_to_write() -> None:
    catalog = _routing_catalog()
    resolved = resolve_category(
        "visual-engineering",
        required_mode="read-only",
        available={"omg-vision", "omg-designer"},
        catalog=catalog,
    )
    assert resolved["role_id"] == "omg-vision"
    assert resolved["capability_mode"] == "read-only"
    resolved = resolve_category(
        "visual-engineering",
        required_mode="read-only",
        available={"omg-designer", "omg-code-reviewer"},
        catalog=catalog,
    )
    assert resolved["role_id"] == "omg-code-reviewer"
    assert resolved["capability_mode"] == "read-only"
    with pytest.raises(AgentsCatalogError, match="no compatible agent"):
        resolve_category(
            "review",
            required_mode="read-only",
            available={"omg-executor", "omg-designer"},
            catalog=catalog,
        )


def test_assert_agent_capability_blocks_reviewer_write() -> None:
    catalog = _routing_catalog()
    assert_agent_capability("omg-code-reviewer", "read-only", catalog)
    with pytest.raises(AgentsCatalogError, match="cannot receive"):
        assert_agent_capability("omg-code-reviewer", "read-write", catalog)
    with pytest.raises(AgentsCatalogError, match="cannot receive"):
        assert_agent_capability("omg-planner", "read-write", catalog)
    with pytest.raises(AgentsCatalogError, match="forbidden"):
        assert_agent_capability("omg-executor", "execute", catalog)


def test_reviewer_cannot_be_catalogued_read_write(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-code-reviewer",
        capability_mode="read-write",
        permission_mode="default",
        tier="reviewer",
        spawn_policy="leaf",
    )
    _stub_agent(tmp_path, "omg-code-reviewer", mode="read-write")
    _write_catalog(tmp_path, [entry])
    with pytest.raises(AgentsCatalogError, match="requires capability_mode=read-only"):
        load_agents_catalog(tmp_path, require_projections=False, pin_files=False)


def test_render_handoff_is_bounded_and_no_self_approval() -> None:
    catalog = _routing_catalog()
    blob = render_handoff(
        "omg-code-reviewer",
        task="review the slice",
        artifacts=[".omg/artifacts/diff.patch"],
        decisions=["keep capability_mode=read-only"],
        catalog=catalog,
    )
    assert "Bounded context handoff" in blob
    assert "full leader conversation" in blob
    assert "cannot self-approve" in blob
    assert "capability_mode: `read-only`" in blob
    assert "review the slice" in blob
    assert ".omg/artifacts/diff.patch" in blob
    huge = "history " * 400
    clipped = render_handoff("omg-explore", task=huge, catalog=catalog)
    assert len(clipped) < len(huge)
    assert "…" in clipped


def test_write_antigravity_projections_prunes_obsolete(tmp_path: Path) -> None:
    entry = _agent_entry(
        "omg-verifier",
        capability_mode="read-only",
        permission_mode="plan",
        tier="verifier",
        spawn_policy="leaf",
    )
    _stub_agent(tmp_path, "omg-verifier", mode="read-only")
    _write_catalog(tmp_path, [entry])
    stale = tmp_path / ANTIGRAVITY_PROJECTION_ROOT / "omg-stale" / "agent.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("obsolete\n", encoding="utf-8")
    write_antigravity_projections(tmp_path)
    assert not stale.exists()
    assert check_antigravity_projections(tmp_path) == []


def test_alias_canonical_id_cannot_steal_prior(tmp_path: Path) -> None:
    first = _agent_entry(
        "omg-alpha",
        capability_mode="read-only",
        permission_mode="plan",
        tier="planner",
        spawn_policy="leaf",
    )
    first["aliases"] = ["omg-beta"]
    second = _agent_entry(
        "omg-beta",
        capability_mode="read-only",
        permission_mode="plan",
        tier="planner",
        spawn_policy="leaf",
    )
    _stub_agent(tmp_path, "omg-alpha", mode="read-only")
    _stub_agent(tmp_path, "omg-beta", mode="read-only")
    _write_catalog(tmp_path, [first, second])
    with pytest.raises(AgentsCatalogError, match="collides with alias"):
        load_agents_catalog(tmp_path, require_projections=False)


def test_yaml_decode_error_is_catalog_error(tmp_path: Path) -> None:
    yaml_path = tmp_path / YAML_RELATIVE
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(AgentsCatalogError, match="cannot read YAML"):
        load_yaml_catalog_document(tmp_path)


def test_yaml_block_scalar_keeps_blank_and_hash_lines() -> None:
    parsed = parse_yaml("note: |-\n  first\n\n  # heading\n  last\n")
    assert parsed["note"] == "first\n\n# heading\nlast"
