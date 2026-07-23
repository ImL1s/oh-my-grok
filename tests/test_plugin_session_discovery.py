"""Deterministic plugin session-surface discovery (OMG-EXT-001)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GEN_SCRIPT = ROOT / "scripts" / "generate_capabilities_lock.py"
EXACT_NINE = [
    "run_status.read",
    "trace.timeline",
    "trace.summary",
    "resume_metadata.read",
    "project_memory.search",
    "wiki.read",
    "team_status.read",
    "mailbox.list",
    "proposal.create",
]


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_capabilities_lock_session_test", GEN_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_repository_session_surface_is_complete_and_fail_closed() -> None:
    gen = _load_generator()
    surface = gen.discover_session_surface(ROOT)

    skill_names = {item["name"] for item in surface["skills"]}
    agent_names = {item["name"] for item in surface["agents"]}
    assert {"omg-ask", "omg-dual-review", "omg-ralplan", "omg-lsp"} <= skill_names
    assert {
        "omg-architect",
        "omg-code-reviewer",
        "omg-executor",
        "omg-qa-tester",
        "omg-verifier",
    } <= agent_names
    assert all(item["capability_mode"] in {"read-only", "read-write"} for item in surface["agents"])
    assert not any(item["code"] == "W_AGENT_CAPABILITY_MISMATCH" for item in surface["issues"])

    routing = surface["advisor_routing"]
    assert routing["skills"] == ["omg-ask", "omg-dual-review", "omg-ralplan"]
    assert routing["providers"]["claude"]["aliases"] == ["fable"]
    assert routing["worker_eligible"] is False
    assert routing["auto_apply"] is False
    assert routing["authoritative"] is False
    assert routing["posture"] == "read-only"

    assert surface["mcp"]["operations"] == EXACT_NINE
    assert surface["mcp"]["operation_count"] == 9
    assert surface["mcp"]["authoritative_state_mutation"] is False
    assert surface["lsp"] == {
        "owner": "host",
        "registration_file": ".lsp.json",
        "semantic_proxy_count": 0,
    }
    assert surface["workflow"] == {
        "contract": "repository-workflow/v1",
        "portable_classification": "native_substitute",
        "grok_native_projection": "optional_unclaimed",
    }


def test_discovery_is_deterministic_and_embedded_in_lock(tmp_path: Path) -> None:
    gen = _load_generator()
    _write(
        tmp_path / "skills" / "omg-z" / "SKILL.md",
        "---\nname: omg-z\ndescription: z\n---\n# z\n",
    )
    _write(
        tmp_path / "agents" / "omg-architect.md",
        "---\nname: omg-architect\ncapabilityMode: read-only\n---\n# a\n",
    )
    first = gen.compute_lock_for(tmp_path)
    second = gen.compute_lock_for(tmp_path)
    assert first == second
    assert first["session_surface"] == gen.discover_session_surface(tmp_path)
    assert len(first["session_surface_aggregate"]) == 64


def test_missing_and_malformed_metadata_fail_honestly_with_path_name_fallback(
    tmp_path: Path,
) -> None:
    gen = _load_generator()
    _write(tmp_path / "skills" / "omg-missing" / "SKILL.md", "# no metadata\n")
    _write(
        tmp_path / "agents" / "omg-qa-tester.md",
        "---\nname: omg-qa-tester\ncapabilityMode: read-write\n# no closing fence\n",
    )
    surface = gen.discover_session_surface(tmp_path)
    assert surface["skills"] == [
        {
            "name": "omg-missing",
            "path": "skills/omg-missing/SKILL.md",
            "sha256": surface["skills"][0]["sha256"],
            "metadata_status": "missing",
        }
    ]
    agent = surface["agents"][0]
    assert agent["name"] == "omg-qa-tester"
    assert agent["metadata_status"] == "malformed"
    assert agent["capability_mode"] == "unspecified"
    assert agent["capability_source"] == "unresolved"
    assert surface["claim_status"]["roles"] == "missing"
    assert {
        "W_AGENT_METADATA",
        "W_ROLES_SOURCE_MISSING",
        "W_SKILL_METADATA",
    } <= {item["code"] for item in surface["issues"]}


def test_canonical_role_posture_overrides_conflicting_frontmatter(
    tmp_path: Path,
) -> None:
    gen = _load_generator()
    _write(
        tmp_path / "omg_cli" / "team" / "roles.py",
        "_ROLES = {\n"
        "    'code-reviewer': RoleMeta(\n"
        "        posture='read-only', role_class='reviewer'\n"
        "    )\n"
        "}\n",
    )
    _write(
        tmp_path / "agents" / "omg-code-reviewer.md",
        "---\nname: omg-code-reviewer\ncapabilityMode: read-write\n---\n# bad\n",
    )
    surface = gen.discover_session_surface(tmp_path)
    agent = surface["agents"][0]
    assert agent["declared_capability_mode"] == "read-write"
    assert agent["capability_mode"] == "read-only"
    assert agent["capability_source"] == "role_taxonomy"
    assert "W_AGENT_CAPABILITY_MISMATCH" in {
        item["code"] for item in surface["issues"]
    }
