"""Fail-closed plugin skill catalog (#70 Wave B/C) — routing from catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.skills_catalog import (
    ALLOWED_CAPABILITY_MODES,
    ANTIGRAVITY_PROJECTION_ROOT,
    CATALOG_DOC_LOCALES,
    CATALOG_RELATIVE,
    CONTINUATION_OWNERS,
    FORBIDDEN_CAPABILITY_MODES,
    HOST_NATIVE_PROTECTED,
    PLUGIN_SKILL_COUNT,
    PROJECTION_BANNER_NEEDLES,
    ROUTING_PRIORITY_HEAD,
    SkillsCatalogError,
    check_antigravity_projections,
    check_catalog_markdown,
    inspect_skills_catalog,
    is_informational_question,
    load_skills_catalog,
    plugin_root,
    render_workflow_routing,
    required_capability_diagnostics,
    resolve_continuation,
    resolve_skill_resource,
    resolve_trigger,
    routing_order,
    write_antigravity_projections,
    write_catalog_markdown,
)

ROOT = Path(__file__).resolve().parents[1]

_MINIMUM_MISSING = (
    "best-practice-research",
    "autoresearch",
    "autoresearch-goal",
    "external-context",
    "deep-dive",
    "trace",
    "sciomc",
    "prometheus-strict",
    "hyperplan",
    "plan",
    "design",
    "tdd",
    "build-fix",
    "code-review",
    "security-review",
    "visual-verdict",
    "visual-ralph",
    "ai-slop-cleaner",
    "comment-checker",
    "security-research",
    "deepinit",
    "init-deep",
    "project-session-manager",
    "psm",
    "mcp-setup",
    "configure-notifications",
    "release",
    "self-improve",
    "skill",
    "skillify",
    "writer-memory",
    "git-master",
    "ralph-init",
    "ecomode",
    "ulw-loop",
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stub_skill(root: Path, skill_id: str) -> None:
    _write(
        root / "skills" / skill_id / "SKILL.md",
        f"---\nname: {skill_id}\n---\n# {skill_id}\n",
    )
    _write(
        root / "skills" / skill_id / "resources" / "contract.json",
        json.dumps(
            {
                "id": skill_id,
                "capability_mode": "read-only",
                "continuation": "none",
                "conflict_policy": "artifact_only",
                "artifacts": [".omg/artifacts/"],
                "evidence_rule": "never set verified",
            }
        )
        + "\n",
    )


def _plugin_entry(skill_id: str) -> dict:
    return {
        "id": skill_id,
        "kind": "canonical",
        "classification": "omg_native",
        "runtime_owner": "omg-cli",
        "file": f"skills/{skill_id}/SKILL.md",
        "aliases": [skill_id.removeprefix("omg-")],
        "sources": [],
        "cli_twin": "doctor",
        "capability_mode": "read-only",
        "continuation": "none",
        "conflict_policy": "artifact_only",
        "implementation_status": "plugin",
        "live_verification": "unproven",
        "verified": False,
        "triggers": [],
        "pipeline_next": [],
        "required_capabilities": [],
        "resources": ["resources/contract.json"],
        "projections": {
            "grok": {
                "kind": "plugin_skill",
                "path": f"skills/{skill_id}/SKILL.md",
            },
            "antigravity": {
                "kind": "skill_md_projection",
                "path": f"{ANTIGRAVITY_PROJECTION_ROOT}/{skill_id}/SKILL.md",
            },
        },
    }


def _write_catalog(root: Path, skills: list[dict]) -> None:
    plugin_n = sum(1 for item in skills if item.get("file"))
    payload = {
        "schema": "omg-skills-catalog/v1",
        "kind": "read_only_machine_catalog",
        "plugin_skill_count": plugin_n,
        "skills": skills,
    }
    _write(root / CATALOG_RELATIVE, json.dumps(payload, indent=2) + "\n")


def test_repo_catalog_plugin_count_and_classifies_minimum_set() -> None:
    catalog = load_skills_catalog(ROOT)
    plugin_ids = [record.id for record in catalog.plugin_skills]
    disk = sorted(
        p.name
        for p in (ROOT / "skills").iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    )
    assert plugin_ids == disk
    assert len(plugin_ids) == PLUGIN_SKILL_COUNT
    for record in catalog.skills:
        assert record.verified is False
        assert record.live_verification in {"unproven", "none"}
        assert record.capability_mode in ALLOWED_CAPABILITY_MODES
        assert record.capability_mode not in FORBIDDEN_CAPABILITY_MODES
    for name in _MINIMUM_MISSING:
        resolved = catalog.resolve(name)
        assert resolved is not None, name
        assert resolved.kind == "canonical"
        assert resolved.verified is False


def test_host_native_plan_and_goal_are_protected_aliases() -> None:
    catalog = load_skills_catalog(ROOT)
    plan = catalog.by_id()["plan"]
    assert plan.kind == "alias"
    assert plan.host_native_protected is True
    assert catalog.resolve("plan").id == "omg-ralplan"
    assert catalog.resolve("goal").id == "omg-ultragoal"
    assert "plan" in HOST_NATIVE_PROTECTED


def test_canonical_cannot_shadow_host_native(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-plan")
    _write_catalog(tmp_path, [_plugin_entry("omg-plan")])
    with pytest.raises(SkillsCatalogError, match="host-native"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_missing_skill_file_fails_closed(tmp_path: Path) -> None:
    _write_catalog(tmp_path, [_plugin_entry("omg-using")])
    with pytest.raises(SkillsCatalogError, match="missing"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_duplicate_id_fails_closed(tmp_path: Path) -> None:
    entry = _plugin_entry("omg-using")
    _stub_skill(tmp_path, "omg-using")
    _write_catalog(tmp_path, [entry, dict(entry)])
    with pytest.raises(SkillsCatalogError, match="duplicate"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_execute_and_all_capability_modes_fail_closed(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    for bad in ("execute", "all"):
        entry = _plugin_entry("omg-using")
        entry["capability_mode"] = bad
        _write_catalog(tmp_path, [entry])
        with pytest.raises(SkillsCatalogError, match="capability_mode"):
            load_skills_catalog(tmp_path, require_projections=False)


def test_verified_true_fails_closed(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    entry = _plugin_entry("omg-using")
    entry["verified"] = True
    _write_catalog(tmp_path, [entry])
    with pytest.raises(SkillsCatalogError, match="verified"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_uncatalogued_plugin_dir_fails_closed(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    _stub_skill(tmp_path, "omg-ralph")
    _write_catalog(tmp_path, [_plugin_entry("omg-using")])
    with pytest.raises(SkillsCatalogError, match="uncatalogued"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_resource_path_traversal_rejected(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    with pytest.raises(SkillsCatalogError, match="relative posix"):
        resolve_skill_resource(tmp_path, "omg-using", "../secret.txt")
    with pytest.raises(SkillsCatalogError, match="relative posix"):
        resolve_skill_resource(tmp_path, "omg-using", "/etc/passwd")


def test_resource_symlink_rejected(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    target = tmp_path / "outside.txt"
    target.write_text("nope", encoding="utf-8")
    link = tmp_path / "skills" / "omg-using" / "escape.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not available")
    with pytest.raises(SkillsCatalogError, match="symlink|escapes"):
        resolve_skill_resource(tmp_path, "omg-using", "escape.txt")


def test_declared_resource_roundtrip(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    res = tmp_path / "skills" / "omg-using" / "templates" / "note.md"
    _write(res, "ok\n")
    entry = _plugin_entry("omg-using")
    entry["resources"] = ["templates/note.md"]
    _write_catalog(tmp_path, [entry])
    catalog = load_skills_catalog(tmp_path, require_projections=False)
    path = resolve_skill_resource(
        tmp_path, "omg-using", "templates/note.md", catalog=catalog
    )
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "ok\n"


def test_empty_resource_allowlist_rejects_undeclared(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    _write_catalog(tmp_path, [_plugin_entry("omg-using")])
    catalog = load_skills_catalog(tmp_path, require_projections=False)
    with pytest.raises(SkillsCatalogError, match="not declared"):
        resolve_skill_resource(
            tmp_path, "omg-using", "SKILL.md", catalog=catalog
        )


def test_plugin_skill_empty_resources_fails_closed(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    entry = _plugin_entry("omg-using")
    entry["resources"] = []
    _write_catalog(tmp_path, [entry])
    with pytest.raises(SkillsCatalogError, match="resource"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_resource_nul_rejected(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    with pytest.raises(SkillsCatalogError, match="NUL"):
        resolve_skill_resource(tmp_path, "omg-using", "bad\x00name")
    entry = _plugin_entry("omg-using")
    entry["resources"] = ["bad\u0000name"]
    _write_catalog(tmp_path, [entry])
    with pytest.raises(SkillsCatalogError, match="NUL"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_in_tree_resource_symlink_rejected(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    real = tmp_path / "skills" / "omg-using" / "templates" / "note.md"
    _write(real, "ok\n")
    link = tmp_path / "skills" / "omg-using" / "alias.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks not available")
    with pytest.raises(SkillsCatalogError, match="symlink"):
        resolve_skill_resource(tmp_path, "omg-using", "alias.md")


def test_continuation_refuse_adopt_artifact() -> None:
    catalog = load_skills_catalog(ROOT)
    assert resolve_continuation("omg-ralph", "omg-autopilot", catalog=catalog) == "refuse"
    assert (
        resolve_continuation("omg-autopilot", "omg-ultraqa", catalog=catalog)
        == "adopt_existing"
    )
    assert (
        resolve_continuation("omg-ralph", "omg-cancel", catalog=catalog)
        == "adopt_existing"
    )
    assert resolve_continuation("omg-ralph", "omg-wiki", catalog=catalog) == "artifact_only"
    assert resolve_continuation(None, "omg-ralph", catalog=catalog) == "none"
    assert resolve_continuation("omg-autopilot", "ralph", catalog=catalog) == "refuse"
    for owner in CONTINUATION_OWNERS:
        assert catalog.by_id()[owner].continuation == "owner"


def test_informational_question_suppressed() -> None:
    catalog = load_skills_catalog(ROOT)
    assert is_informational_question("what is ralph?")
    assert resolve_trigger(catalog, "what is ralph?") is None
    hit = resolve_trigger(catalog, "ralph ship the login rewrite")
    assert hit is not None
    assert hit.id == "omg-ralph"
    assert resolve_trigger(catalog, "task") is None
    assert resolve_trigger(catalog, "steam") is None


def test_required_capability_fail_fast() -> None:
    catalog = load_skills_catalog(ROOT)
    record = catalog.by_id()["omg-wiki"]
    from dataclasses import replace

    needy = replace(record, required_capabilities=("mcp.docs",))
    blocked = required_capability_diagnostics(needy, {"mcp.docs": False})
    assert blocked["blocked"] is True
    assert blocked["verified"] is False
    assert blocked["error"] == "E_SKILL_CAPABILITY_MISSING"
    ok = required_capability_diagnostics(needy, {"mcp.docs": True})
    assert ok["ok"] is True


def test_inspect_payload_never_claims_verified() -> None:
    payload = inspect_skills_catalog(ROOT)
    assert payload["ok"] is True
    assert payload["verified"] is False
    assert payload["healthy"] is False
    assert payload["observed"] is False
    assert payload["plugin_skill_count"] == PLUGIN_SKILL_COUNT
    assert "projections only" in payload["note"]


def test_capabilities_command_embeds_skills_catalog() -> None:
    text = (ROOT / "omg_cli" / "commands" / "inspect.py").read_text(encoding="utf-8")
    assert "inspect_skills_catalog" in text
    assert '"skills_catalog": inspect_skills_catalog' in text
    payload = inspect_skills_catalog(plugin_root())
    assert payload["ok"] is True
    assert payload["verified"] is False


def test_doctor_skills_check_consumes_catalog() -> None:
    from omg_cli.doctor import check_skills_omg_prefix

    name, ok, detail = check_skills_omg_prefix()
    assert name == "skills omg-*"
    assert ok is True
    assert "plugin skill" in detail
    assert str(PLUGIN_SKILL_COUNT) in detail


def test_projections_and_catalog_markdown_match_committed() -> None:
    assert check_antigravity_projections(ROOT) == []
    assert check_catalog_markdown(ROOT) == []
    sample = (ROOT / ANTIGRAVITY_PROJECTION_ROOT / "omg-using" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    lowered = sample.lower()
    for needle in PROJECTION_BANNER_NEEDLES:
        assert needle in lowered


def test_cli_skill_resolve_plan_is_alias(capsys) -> None:
    from omg_cli.main import main

    code = main(["--json", "skill", "resolve", "plan"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["canonical"] == "omg-ralplan"
    assert payload["verified"] is False


def test_cli_skill_show_plan_preserves_alias(capsys) -> None:
    from omg_cli.main import main

    code = main(["--json", "skill", "show", "plan"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    row = payload["skill"]
    assert row["id"] == "plan"
    assert row["kind"] == "alias"
    assert row.get("host_native_protected") is True


def test_cli_skill_list_malformed_catalog_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from omg_cli.main import main

    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "catalog.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr("omg_cli.skills_catalog.plugin_root", lambda: tmp_path)
    code = main(["--json", "skill", "list"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "E_SKILL_CATALOG"


def test_non_utf8_catalog_is_skills_catalog_error(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "catalog.json").write_bytes(b"{\xff\xfe not utf-8")
    with pytest.raises(SkillsCatalogError, match="cannot read catalog"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_resolve_trigger_requires_token_boundaries() -> None:
    catalog = load_skills_catalog(ROOT)
    assert resolve_trigger(catalog, "task") is None
    assert resolve_trigger(catalog, "steam") is None
    hit = resolve_trigger(catalog, "ask")
    assert hit is not None and hit.id == "omg-ask"
    team = resolve_trigger(catalog, "team ship it")
    assert team is not None and team.id == "omg-team"


def test_resolve_trigger_cancel_beats_longer_owner() -> None:
    catalog = load_skills_catalog(ROOT)
    hit = resolve_trigger(catalog, "cancel autopilot")
    assert hit is not None and hit.id == "omg-cancel"
    ralplan = resolve_trigger(catalog, "ralplan then ralph")
    assert ralplan is not None and ralplan.id == "omg-ralplan"


def test_write_helpers_are_idempotent(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    _write_catalog(tmp_path, [_plugin_entry("omg-using")])
    written = write_antigravity_projections(tmp_path)
    assert any(path.endswith("omg-using/SKILL.md") for path in written)
    docs = write_catalog_markdown(tmp_path)
    assert any(path.endswith("skills-catalog.md") for path in docs)
    assert any(path.endswith("skills-catalog.zh.md") for path in docs)
    assert any(path.endswith("skills-catalog.zh-TW.md") for path in docs)
    catalog = load_skills_catalog(tmp_path, require_projections=True)
    assert catalog.plugin_skills[0].id == "omg-using"


_WAVE_BC = (
    "omg-best-practice-research",
    "omg-trace",
    "omg-deep-dive",
    "omg-external-context",
    "omg-tdd",
    "omg-build-fix",
    "omg-security-review",
    "omg-visual-verdict",
    "omg-deepinit",
    "omg-project-session-manager",
    "omg-mcp-setup",
    "omg-configure-notifications",
    "omg-skill",
    "omg-prometheus-strict",
    "omg-hyperplan",
    "omg-autoresearch",
    "omg-autoresearch-goal",
    "omg-parallel-research",
    "omg-self-improve",
    "omg-writer-memory",
    "omg-visual-ralph",
    "omg-ai-slop-cleaner",
    "omg-comment-checker",
    "omg-security-research",
    "omg-design",
    "omg-release",
    "omg-git-master",
    "omg-ralph-init",
    "omg-ecomode",
)


def test_wave_bc_plugin_skills_have_playbooks_projections_resources() -> None:
    catalog = load_skills_catalog(ROOT)
    assert len(catalog.plugin_skills) == PLUGIN_SKILL_COUNT
    by_id = catalog.by_id()
    for skill_id in _WAVE_BC:
        record = by_id[skill_id]
        assert record.file == f"skills/{skill_id}/SKILL.md"
        assert record.implementation_status == "configured"
        assert record.verified is False
        assert record.live_verification == "unproven"
        assert (ROOT / record.file).is_file()
        assert record.resources
        for rel in record.resources:
            path = resolve_skill_resource(ROOT, skill_id, rel, catalog=catalog)
            assert path.is_file()
            assert not path.is_symlink()
        proj = ROOT / record.projections["antigravity"].path
        assert proj.is_file()
        assert not proj.is_symlink()
        text = (ROOT / record.file).read_text(encoding="utf-8")
        assert "name:" in text
        assert "description:" in text
        assert "HARD RULES" in text
        assert "spawn_subagent" in text
        assert "capability_mode" in text
        assert "omg cancel" in text
        assert "verified" in text.lower()


def test_every_plugin_skill_declares_a_resource() -> None:
    catalog = load_skills_catalog(ROOT)
    for record in catalog.plugin_skills:
        assert record.resources, record.id
        assert record.verified is False


def test_render_workflow_routing_priority_and_new_triggers() -> None:
    catalog = load_skills_catalog(ROOT)
    text = render_workflow_routing(catalog)
    assert "cancel" in text
    assert "ralplan" in text
    assert "autopilot" in text
    assert "Priority when several keywords match" in text
    assert ROUTING_PRIORITY_HEAD[0] == "omg-cancel"
    ordered_ids = [record.id for record in routing_order(catalog)]
    assert ordered_ids[: len(ROUTING_PRIORITY_HEAD)] == list(ROUTING_PRIORITY_HEAD)
    head_set = set(ROUTING_PRIORITY_HEAD)
    leftover = [
        record.id
        for record in routing_order(catalog)
        if record.id in CONTINUATION_OWNERS and record.id not in head_set
    ]
    assert leftover == sorted(CONTINUATION_OWNERS - head_set)
    assert "hyperplan" in text
    assert "visual verdict" in text or "visual-verdict" in text
    assert "tdd" in text
    assert "UserPromptSubmit" in text


def test_informational_question_still_none_for_new_triggers() -> None:
    catalog = load_skills_catalog(ROOT)
    assert resolve_trigger(catalog, "what is hyperplan?") is None
    assert resolve_trigger(catalog, "how does tdd work") is None
    hit = resolve_trigger(catalog, "hyperplan the auth slice")
    assert hit is not None and hit.id == "omg-hyperplan"


def test_host_native_plan_still_alias() -> None:
    catalog = load_skills_catalog(ROOT)
    assert catalog.by_id()["plan"].kind == "alias"
    assert catalog.resolve("plan").id == "omg-ralplan"
    assert not (ROOT / "skills" / "plan").exists()
    assert not (ROOT / "skills" / "omg-plan").exists()


def test_plugin_skill_count_mismatch_fails_closed(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    entry = _plugin_entry("omg-using")
    payload = {
        "schema": "omg-skills-catalog/v1",
        "kind": "read_only_machine_catalog",
        "plugin_skill_count": 99,
        "skills": [entry],
    }
    _write(tmp_path / CATALOG_RELATIVE, json.dumps(payload) + "\n")
    with pytest.raises(SkillsCatalogError, match="plugin_skill_count"):
        load_skills_catalog(tmp_path, require_projections=False)


def test_prune_reports_and_removes_obsolete_projection(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    _write_catalog(tmp_path, [_plugin_entry("omg-using")])
    write_antigravity_projections(tmp_path)
    extra = tmp_path / ANTIGRAVITY_PROJECTION_ROOT / "obsolete" / "SKILL.md"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("stale\n", encoding="utf-8")
    errors = check_antigravity_projections(tmp_path)
    assert any("uncatalogued projection" in item for item in errors)
    write_antigravity_projections(tmp_path)
    assert not extra.is_file()
    assert check_antigravity_projections(tmp_path) == []


def test_write_refuses_symlink_dest(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    _write_catalog(tmp_path, [_plugin_entry("omg-using")])
    write_antigravity_projections(tmp_path)
    dest = tmp_path / ANTIGRAVITY_PROJECTION_ROOT / "omg-using" / "SKILL.md"
    target = tmp_path / "outside.md"
    target.write_text("nope\n", encoding="utf-8")
    dest.unlink()
    try:
        dest.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not available")
    with pytest.raises(SkillsCatalogError, match="symlink"):
        write_antigravity_projections(tmp_path)


def test_localized_catalog_docs_check() -> None:
    errors = check_catalog_markdown(ROOT)
    assert errors == []
    for rel in CATALOG_DOC_LOCALES.values():
        path = ROOT / rel
        assert path.is_file(), rel
        text = path.read_text(encoding="utf-8")
        assert "verified" in text.lower()
        assert "live" in text.lower()


def test_catalog_never_sets_verified_true() -> None:
    catalog = load_skills_catalog(ROOT)
    raw = json.loads((ROOT / CATALOG_RELATIVE).read_text(encoding="utf-8"))
    assert raw["plugin_skill_count"] == PLUGIN_SKILL_COUNT
    for record in catalog.skills:
        assert record.verified is False
    for item in raw["skills"]:
        if "verified" in item:
            assert item["verified"] is False
