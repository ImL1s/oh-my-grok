"""Fail-closed plugin skill catalog (#70 Wave A) — not a routing runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.skills_catalog import (
    ALLOWED_CAPABILITY_MODES,
    ANTIGRAVITY_PROJECTION_ROOT,
    CATALOG_RELATIVE,
    CONTINUATION_OWNERS,
    FORBIDDEN_CAPABILITY_MODES,
    HOST_NATIVE_PROTECTED,
    PLUGIN_SKILL_COUNT,
    PROJECTION_BANNER_NEEDLES,
    SkillsCatalogError,
    check_antigravity_projections,
    check_catalog_markdown,
    inspect_skills_catalog,
    is_informational_question,
    load_skills_catalog,
    plugin_root,
    required_capability_diagnostics,
    resolve_continuation,
    resolve_skill_resource,
    resolve_trigger,
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
        "resources": [],
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
    payload = {
        "schema": "omg-skills-catalog/v1",
        "kind": "read_only_machine_catalog",
        "skills": skills,
    }
    _write(root / CATALOG_RELATIVE, json.dumps(payload, indent=2) + "\n")


def test_repo_catalog_has_16_plugin_skills_and_classifies_minimum_set() -> None:
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
    assert "16 plugin skill" in detail


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


def test_resolve_trigger_requires_token_boundaries() -> None:
    catalog = load_skills_catalog(ROOT)
    assert resolve_trigger(catalog, "task") is None
    assert resolve_trigger(catalog, "steam") is None
    hit = resolve_trigger(catalog, "ask")
    assert hit is not None and hit.id == "omg-ask"
    team = resolve_trigger(catalog, "team ship it")
    assert team is not None and team.id == "omg-team"


def test_write_helpers_are_idempotent(tmp_path: Path) -> None:
    _stub_skill(tmp_path, "omg-using")
    _write_catalog(tmp_path, [_plugin_entry("omg-using")])
    written = write_antigravity_projections(tmp_path)
    assert any(path.endswith("omg-using/SKILL.md") for path in written)
    doc = write_catalog_markdown(tmp_path)
    assert doc.endswith("skills-catalog.md")
    catalog = load_skills_catalog(tmp_path, require_projections=True)
    assert catalog.plugin_skills[0].id == "omg-using"
