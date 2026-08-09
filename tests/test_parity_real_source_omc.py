"""Hermetic real-source OMC discovery fixtures + #78-F plan matrix."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import (
    PARITY_CATEGORY_TAXONOMY,
    SOURCE_STATUS_IDS,
    load_json_object,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_check import check_parity_inventory
from omg_cli.parity_completeness import (
    assert_completeness_promotion,
    build_completeness_proof,
    check_committed_completeness_artifacts,
    reproduce_source_index,
    validate_completeness_mapping,
    validate_completeness_policy,
    verify_completeness_proof,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_FIX = ROOT / "tests" / "fixtures" / "parity" / "completeness" / "real_source" / "OMC"
CANONICAL = ROOT / "docs" / "parity" / "omg-parity.json"
FIXTURE_REPO = "https://example.invalid/fixture-omc-real"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _commit_tree(root: Path) -> str:
    """Initialize an independent git work-tree root and commit all files."""
    init = _git(root, "init")
    assert init.returncode == 0, init.stderr
    assert _git(root, "config", "user.email", "fixture@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "fixture").returncode == 0
    assert _git(root, "add", "-A").returncode == 0
    commit = _git(root, "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    assert commit.returncode == 0, commit.stderr
    head = _git(root, "rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    sha = head.stdout.strip().lower()
    assert len(sha) == 40
    return sha


def _capability(
    *,
    cap_id: str,
    source: str,
    category: str,
    pin: str,
    classification: str = "omg_native",
    paths: list[str] | None = None,
    alias_of: str | None = None,
) -> dict:
    row: dict = {
        "id": cap_id,
        "category": category,
        "promise": f"promise for {cap_id}",
        "classification": classification,
        "upstream": {
            "source": source,
            "revision": pin,
            "source_paths": paths or ["commands/hello.md"],
        },
        "omg_paths": ["omg_cli/parity_completeness.py"],
        "runtime_owner": "omg",
        "maturity": {"grok": "catalogued"},
        "evidence": {
            "tests": ["tests/test_parity_real_source_omc.py"],
            "docs": ["docs/parity/completeness-schema-v1.md"],
            "live": [],
        },
        "issues": ["#78"],
        "gap": "fixture gap",
    }
    if alias_of is not None:
        row["alias_of"] = alias_of
    return row


def _mini_inventory(
    *,
    pin: str,
    capabilities: list[dict] | None = None,
    inventory_status: str = "bootstrapping",
    source_status: dict[str, str] | None = None,
    category_status: dict[str, str] | None = None,
    gaps: list[dict] | None = None,
    pins: dict[str, dict] | None = None,
) -> dict:
    categories = {c: "bootstrapping" for c in sorted(PARITY_CATEGORY_TAXONOMY)}
    sources = {s: "bootstrapping" for s in SOURCE_STATUS_IDS}
    if category_status:
        categories.update(category_status)
    if source_status:
        sources.update(source_status)
    default_pins = {
        sid: {
            "repository": FIXTURE_REPO if sid == "OMC" else f"https://example.invalid/{sid}",
            "revision": pin,
            "kind": "commit",
        }
        for sid in SOURCE_STATUS_IDS
    }
    if pins:
        default_pins.update(pins)
    caps = capabilities or []
    return {
        "store_kind": "parity_inventory",
        "schema_version": 2,
        "repository_id": "OMG",
        "inventory_status": inventory_status,
        "ownership_manifest_id": "dual-parity-writers-v1",
        "live_evidence_max_age_days": 30,
        "upstream_pins": default_pins,
        "category_status": categories,
        "source_status": sources,
        "capabilities": caps,
        "gaps": gaps if gaps is not None else [],
    }


def _materialize_real_omc(dest: Path) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REAL_FIX, dest)
    return _commit_tree(dest)


def _v2_policy(*, repository: str = FIXTURE_REPO) -> dict:
    return {
        "store_kind": "parity-completeness-policy",
        "schema_version": 1,
        "source": "OMC",
        "repository": repository,
        "discovery_rules": {
            "version": 2,
            "authoritative_registries": [
                {
                    "id": "plugin-skills",
                    "path": ".claude-plugin/plugin.json",
                    "extraction_method": "claude_plugin_skills_v1",
                    "options": {},
                },
                {
                    "id": "session-commands",
                    "path": "commands",
                    "extraction_method": "markdown_command_tree_v1",
                    "options": {},
                },
                {
                    "id": "agent-registry",
                    "path": "src/agents/definitions.ts",
                    "extraction_method": "typescript_agent_registry_v1",
                    "options": {"prompt_dir": "agents"},
                },
                {
                    "id": "terminal-cli",
                    "path": "src/cli/index.ts",
                    "extraction_method": "commander_command_graph_v1",
                    "options": {},
                },
                {
                    "id": "lifecycle-hooks",
                    "path": "hooks/hooks.json",
                    "extraction_method": "claude_hooks_manifest_v1",
                    "options": {},
                },
                {
                    "id": "mcp-tool-families",
                    "path": "src/mcp/tool-registry.ts",
                    "extraction_method": "typescript_tool_family_graph_v1",
                    "options": {},
                },
                {
                    "id": "package-surface",
                    "path": "package.json",
                    "extraction_method": "package_surface_v1",
                    "options": {
                        "governance_scripts": [
                            "plugin:shipping:verify",
                            "sync-metadata:verify",
                        ],
                        "required_files_roots": [
                            ".claude-plugin",
                            "agents",
                            "bin",
                            "commands",
                            "hooks",
                            "skills",
                        ],
                    },
                },
            ],
            "category_assignment": {
                "agent": "agents_routing",
                "agent_catalog": "agents_routing",
                "bin": "runtime_orchestration",
                "catalog": "skills",
                "cli": "runtime_orchestration",
                "command": "runtime_orchestration",
                "hook": "hooks",
                "mcp-family": "tools_mcp",
                "npm-script": "parity_governance",
                "skill": "skills",
            },
            "non_surface_exceptions": [
                {
                    "path": "README.md",
                    "rationale": "Narrative README is not a registered surface",
                    "issue": "#78",
                }
            ],
        },
    }


def _canonical_proof_bytes(proof: dict) -> bytes:
    return json.dumps(
        proof, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _inventory_and_mapping_for_surfaces(
    *,
    pin: str,
    surfaces: list[dict],
    repository: str = FIXTURE_REPO,
) -> tuple[dict, dict]:
    """Build a mini inventory + v2 mapping covering every discovered surface."""
    caps: list[dict] = []
    mapping_surfaces: list[dict] = []
    for surface in surfaces:
        sid = surface["surface_id"]
        # Stable fixture capability id from surface id (safe dotted form).
        cap_id = "fixture.omc." + sid.replace(":", ".").replace("*", "star")
        caps.append(
            _capability(
                cap_id=cap_id,
                source="OMC",
                category=surface["category"],
                pin=pin,
                paths=[surface["source_path"]],
            )
        )
        mapping_surfaces.append(
            {
                "surface_id": sid,
                "category": surface["category"],
                "capability_ids": [cap_id],
            }
        )
    mapping_surfaces.sort(key=lambda item: item["surface_id"])
    inventory = _mini_inventory(
        pin=pin,
        capabilities=caps,
        gaps=[],
        pins={
            "OMC": {
                "repository": repository,
                "revision": pin,
                "kind": "commit",
            }
        },
    )
    mapping = validate_completeness_mapping(
        {
            "store_kind": "parity-completeness-mapping",
            "schema_version": 1,
            "source": "OMC",
            "surfaces": mapping_surfaces,
        }
    )
    return inventory, mapping


def _synthetic_world(tmp_path: Path) -> tuple[Path, str, dict, dict, dict, list[dict]]:
    root = tmp_path / "OMC"
    pin = _materialize_real_omc(root)
    policy = validate_completeness_policy(_v2_policy())
    index = reproduce_source_index(policy, root, pin_revision=pin)
    surfaces = index["discovered_surfaces"]
    inventory, mapping = _inventory_and_mapping_for_surfaces(pin=pin, surfaces=surfaces)
    return root, pin, policy, inventory, mapping, surfaces


def _recommit(root: Path) -> str:
    assert _git(root, "add", "-A").returncode == 0
    commit = _git(root, "-c", "commit.gpgsign=false", "commit", "-m", "mutate")
    assert commit.returncode == 0, commit.stderr
    head = _git(root, "rev-parse", "HEAD")
    assert head.returncode == 0
    return head.stdout.strip().lower()


def _inventory_claim_projection(inventory: dict) -> dict:
    """Statuses + maturity + live evidence — promotion must not mutate these."""
    return {
        "inventory_status": inventory.get("inventory_status"),
        "source_status": copy.deepcopy(inventory.get("source_status")),
        "category_status": copy.deepcopy(inventory.get("category_status")),
        "capabilities": [
            {
                "id": row.get("id"),
                "maturity": copy.deepcopy(row.get("maturity")),
                "evidence_live": copy.deepcopy((row.get("evidence") or {}).get("live")),
            }
            for row in inventory.get("capabilities", [])
            if isinstance(row, dict)
        ],
    }


# ---------------------------------------------------------------------------
# Happy-path matrix
# ---------------------------------------------------------------------------


def test_omc_v2_policy_discovers_all_registry_kinds(tmp_path: Path) -> None:
    root, pin, policy, _inv, _map, surfaces = _synthetic_world(tmp_path)
    kinds = {s["kind"] for s in surfaces}
    assert kinds >= {
        "skill",
        "catalog",
        "command",
        "agent",
        "cli",
        "hook",
        "mcp-family",
        "bin",
        "npm-script",
    }
    by_kind = {k: [s for s in surfaces if s["kind"] == k] for k in sorted(kinds)}
    assert any(s["surface_id"] == "skill.demo" for s in by_kind["skill"])
    assert any(s["surface_id"].startswith("command.") for s in by_kind["command"])
    assert any(s["surface_id"] == "agent.explore" for s in by_kind["agent"])
    assert any(s["surface_id"].startswith("cli.") for s in by_kind["cli"])
    assert any(s["surface_id"].startswith("hook.") for s in by_kind["hook"])
    assert any(s["surface_id"].startswith("mcp-family.") for s in by_kind["mcp-family"])
    assert any(s["surface_id"].startswith("bin.") for s in by_kind["bin"])
    assert any(s["surface_id"].startswith("npm-script.") for s in by_kind["npm-script"])
    assert any(s["surface_id"] == "catalog.skills" for s in surfaces)
    assert any(s["surface_id"] == "catalog.agents" for s in surfaces)
    # Pin authentication still binds.
    assert pin == _git(root, "rev-parse", "HEAD").stdout.strip().lower()


def test_omc_v2_proof_is_byte_deterministic(tmp_path: Path) -> None:
    root, _pin, policy, inventory, mapping, _surfaces = _synthetic_world(tmp_path)
    proof_a = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=root,
        seed=None,
        mapping=mapping,
    )
    proof_b = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=root,
        seed=None,
        mapping=mapping,
    )
    assert _canonical_proof_bytes(proof_a) == _canonical_proof_bytes(proof_b)


def test_committed_omc_artifacts_validate_without_network() -> None:
    inventory = load_json_object(CANONICAL)
    artifacts = check_committed_completeness_artifacts(inventory, repo_root=ROOT)
    assert artifacts["completeness_artifacts_checked"] is True
    assert artifacts["completeness_artifacts_verified"] == 1
    assert artifacts["completeness_artifact_sources"] == ["OMC"]
    assert artifacts["promoted_sources"] == []

    # Maintainer --check path (no --upstream-root) must never claim reproduction.
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check_parity_completeness.py"),
            "--check",
            "--source",
            "OMC",
            "--json",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["artifact_consistency_verified"] is True
    assert payload["source_reproduced"] is False
    assert payload["promotion_performed"] is False


def test_committed_omc_proof_is_source_promotion_sufficient() -> None:
    inventory = load_json_object(CANONICAL)
    before = copy.deepcopy(inventory["source_status"])
    assert before["OMC"] == "bootstrapping"

    promoted = copy.deepcopy(inventory)
    promoted["source_status"] = dict(promoted["source_status"])
    promoted["source_status"]["OMC"] = "complete"

    result = assert_completeness_promotion(promoted, repo_root=ROOT)
    assert result.completeness_gate_checked is True
    assert result.completeness_proofs_required is True
    assert result.completeness_proofs_verified >= 1
    assert "OMC" in result.promoted_sources

    # Canonical file unchanged.
    on_disk = load_json_object(CANONICAL)
    assert on_disk["source_status"]["OMC"] == "bootstrapping"
    assert on_disk["source_status"] == before


def test_strict_bootstrapping_check_verifies_committed_proof() -> None:
    payload = check_parity_inventory(
        inventory_path=CANONICAL,
        repo_root=ROOT,
        strict=True,
    )
    assert payload["ok"] is True
    assert payload["completeness_artifacts_checked"] is True
    assert payload["completeness_artifacts_verified"] == 1
    assert payload["promoted_sources"] == []
    assert payload["completeness_proofs_required"] is False
    inventory = load_json_object(CANONICAL)
    assert inventory["source_status"]["OMC"] == "bootstrapping"


# ---------------------------------------------------------------------------
# Discovery failure modes (mutate synthetic checkout)
# ---------------------------------------------------------------------------


def test_manifest_declared_skill_missing_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (root / "skills" / "demo" / "SKILL.md").unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="declared skill missing|missing at pin"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_unlisted_skill_file_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    hidden = root / "skills" / "hidden" / "SKILL.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("# hidden\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="undeclared skill"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_unregistered_agent_prompt_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (root / "agents" / "ghost.md").write_text("# ghost\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="unregistered agent"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_dynamic_agent_registry_key_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "src" / "agents" / "definitions.ts"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "explore: exploreAgent,",
            "...spreadAgents,\n    explore: exploreAgent,",
        ),
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="computed/spread|rejected"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_dynamic_commander_command_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "src" / "cli" / "index.ts"
    # Non-literal command name (identifier) is rejected by the static graph.
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '.command("hello")',
            ".command(dynName)",
        ),
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="dynamic command"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_unresolved_commander_import_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "src" / "cli" / "index.ts"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "program.parse();",
            "program.addCommand(unknownCmd);\nprogram.parse();",
        ),
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="unresolved addCommand|unresolved import"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_unresolved_mcp_family_spread_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "src" / "mcp" / "tool-registry.ts"
    path.write_text(
        """import type { ToolDef } from "./types.js";

function tagCategory(tools: ToolDef[], category: string): ToolDef[] {
  return tools.map((t) => ({ ...t, category }));
}

export const allTools: ToolDef[] = [
  ...tagCategory(missingTools as unknown as ToolDef[], "lsp"),
];
""",
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="unresolved import"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_hook_command_escape_or_symlink_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)

    # Path traversal via .. in $CLAUDE_PLUGIN_ROOT relative segment.
    hooks = root / "hooks" / "hooks.json"
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        'node "$CLAUDE_PLUGIN_ROOT/../outside/hook.js"'
                                    ),
                                }
                            ],
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="relative POSIX|hook"):
        reproduce_source_index(policy, root, pin_revision=pin)

    # Symlink blob in pin tree is also rejected.
    root2 = tmp_path / "OMC-symlink"
    pin2 = _materialize_real_omc(root2)
    script = root2 / "scripts" / "hook.js"
    script.unlink()
    script.symlink_to("/tmp/omg-hook-escape-target")
    pin2 = _recommit(root2)
    with pytest.raises(ContractValidationError, match="symlink"):
        reproduce_source_index(policy, root2, pin_revision=pin2)


def test_stale_non_surface_exception_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (root / "README.md").unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="non_surface_exception path absent"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_duplicate_normalized_surface_id_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    # Two declared skill dirs that normalize to the same surface id (demo).
    # (Avoid case-only collisions — macOS default FS is case-insensitive.)
    nested = root / "skills" / "nested" / "demo"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("# nested demo\n", encoding="utf-8")
    plugin = root / ".claude-plugin" / "plugin.json"
    plugin.write_text(
        json.dumps(
            {"name": "fixture-omc-real", "skills": ["./skills/demo/", "./skills/nested/demo/"]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="duplicate normalized skill"):
        reproduce_source_index(policy, root, pin_revision=pin)


# ---------------------------------------------------------------------------
# Mapping / artifact failure modes
# ---------------------------------------------------------------------------


def test_mapping_unknown_or_alias_only_target_fails(tmp_path: Path) -> None:
    root, pin, policy, inventory, mapping, surfaces = _synthetic_world(tmp_path)
    # Unknown capability.
    bad_unknown = copy.deepcopy(mapping)
    bad_unknown["surfaces"][0]["capability_ids"] = ["fixture.omc.does.not.exist"]
    with pytest.raises(ContractValidationError, match="unknown capability"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=root,
            mapping=bad_unknown,
        )

    # Alias-only target.
    alias_id = "fixture.omc.hello.alias"
    canon = inventory["capabilities"][0]
    inventory2 = copy.deepcopy(inventory)
    inventory2["capabilities"].append(
        _capability(
            cap_id=alias_id,
            source="OMC",
            category=canon["category"],
            pin=pin,
            classification="alias",
            alias_of=canon["id"],
            paths=list(canon["upstream"]["source_paths"]),
        )
    )
    bad_alias = copy.deepcopy(mapping)
    for entry in bad_alias["surfaces"]:
        if entry["capability_ids"][0] == canon["id"]:
            entry["capability_ids"] = [alias_id]
            break
    with pytest.raises(ContractValidationError, match="alias-only"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory2,
            upstream_root=root,
            mapping=bad_alias,
        )


def test_mapping_cross_source_or_cross_category_fails(tmp_path: Path) -> None:
    root, pin, policy, inventory, mapping, surfaces = _synthetic_world(tmp_path)
    target = surfaces[0]
    # Cross-source: invent an OMX capability and point mapping at it.
    inventory_x = copy.deepcopy(inventory)
    inventory_x["capabilities"].append(
        _capability(
            cap_id="fixture.omx.other",
            source="OMX",
            category=target["category"],
            pin=pin,
            paths=[target["source_path"]],
        )
    )
    bad_src = copy.deepcopy(mapping)
    for entry in bad_src["surfaces"]:
        if entry["surface_id"] == target["surface_id"]:
            entry["capability_ids"] = ["fixture.omx.other"]
            break
    with pytest.raises(ContractValidationError, match="cross-source"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory_x,
            upstream_root=root,
            mapping=bad_src,
        )

    # Cross-category: keep capability but change mapping category.
    other_cat = next(c for c in sorted(PARITY_CATEGORY_TAXONOMY) if c != target["category"])
    bad_cat = copy.deepcopy(mapping)
    for entry in bad_cat["surfaces"]:
        if entry["surface_id"] == target["surface_id"]:
            entry["category"] = other_cat
            break
    with pytest.raises(ContractValidationError, match="cross-category"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=root,
            mapping=bad_cat,
        )


def test_surface_path_not_declared_by_capability_fails(tmp_path: Path) -> None:
    root, pin, policy, inventory, mapping, surfaces = _synthetic_world(tmp_path)
    # Wipe source_paths so declared path check fails.
    for row in inventory["capabilities"]:
        row["upstream"]["source_paths"] = ["README.md"]
    with pytest.raises(ContractValidationError, match="not declared by capability"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=root,
            mapping=mapping,
        )


def test_uncovered_non_alias_omc_row_fails(tmp_path: Path) -> None:
    root, pin, policy, inventory, mapping, surfaces = _synthetic_world(tmp_path)
    # Add an extra non-alias OMC row that no surface maps to.
    inventory["capabilities"].append(
        _capability(
            cap_id="fixture.omc.orphan.row",
            source="OMC",
            category="skills",
            pin=pin,
            paths=["skills/demo/SKILL.md"],
        )
    )
    with pytest.raises(ContractValidationError, match="uncovered non-alias"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=root,
            mapping=mapping,
        )


def test_mapping_and_proof_surface_projection_must_match(tmp_path: Path) -> None:
    """Editing committed mapping alone must fail closed against the proof."""
    inventory = load_json_object(CANONICAL)
    policy = load_json_object(ROOT / "docs/parity/completeness/policies/OMC.json")
    proof = load_json_object(ROOT / "docs/parity/completeness/proofs/OMC.json")
    mapping = load_json_object(ROOT / "docs/parity/completeness/mappings/OMC.json")
    seed_path = ROOT / "docs/parity/upstream-snapshots/OMC.json"
    seed = load_json_object(seed_path) if seed_path.is_file() else None

    mutated = copy.deepcopy(mapping)
    # Drop the last mapped surface → projection mismatch / incomplete mapping.
    assert len(mutated["surfaces"]) > 1
    mutated["surfaces"] = mutated["surfaces"][:-1]
    with pytest.raises(
        ContractValidationError,
        match=(
            "incomplete mapping|do not exactly match|undiscovered|unresolved|"
            "coverage_digest"
        ),
    ):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=None,
            require_no_unresolved=True,
            mapping=mutated,
        )


def test_orphan_policy_mapping_or_proof_fails(tmp_path: Path) -> None:
    staging = tmp_path / "repo"
    # Minimal docs tree with OMC triple + inventory.
    for rel in (
        "docs/parity/completeness/policies",
        "docs/parity/completeness/mappings",
        "docs/parity/completeness/proofs",
        "docs/parity/upstream-snapshots",
    ):
        (staging / rel).mkdir(parents=True)
    shutil.copy2(CANONICAL, staging / "docs/parity/omg-parity.json")
    for kind in ("policies", "mappings", "proofs"):
        shutil.copy2(
            ROOT / f"docs/parity/completeness/{kind}/OMC.json",
            staging / f"docs/parity/completeness/{kind}/OMC.json",
        )
    seed = ROOT / "docs/parity/upstream-snapshots/OMC.json"
    if seed.is_file():
        shutil.copy2(seed, staging / "docs/parity/upstream-snapshots/OMC.json")

    inventory = load_json_object(staging / "docs/parity/omg-parity.json")
    # Sanity: intact triple works.
    check_committed_completeness_artifacts(inventory, repo_root=staging)

    (staging / "docs/parity/completeness/mappings/OMC.json").unlink()
    with pytest.raises(ContractValidationError, match="orphan completeness"):
        check_committed_completeness_artifacts(inventory, repo_root=staging)


def test_networkless_check_never_claims_reproduction() -> None:
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check_parity_completeness.py"),
            "--check",
            "--source",
            "OMC",
            "--json",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={
            **{k: v for k, v in __import__("os").environ.items()},
            "PYTHONPATH": str(ROOT),
        },
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["source_reproduced"] is False
    assert payload["artifact_consistency_verified"] is True


def test_omc_artifacts_do_not_promote_maturity_or_live_evidence() -> None:
    before_inv = load_json_object(CANONICAL)
    before = _inventory_claim_projection(before_inv)

    check_committed_completeness_artifacts(before_inv, repo_root=ROOT)
    check_parity_inventory(inventory_path=CANONICAL, repo_root=ROOT, strict=True)

    after_inv = load_json_object(CANONICAL)
    after = _inventory_claim_projection(after_inv)
    assert after == before
    assert after["source_status"]["OMC"] == "bootstrapping"
    for row in after["capabilities"]:
        # No live evidence arrays mutated by artifact checks.
        assert row["evidence_live"] == [] or isinstance(row["evidence_live"], list)
