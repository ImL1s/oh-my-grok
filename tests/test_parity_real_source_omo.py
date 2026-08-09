"""Hermetic real-source OmO discovery fixtures + #78-H plan matrix."""

from __future__ import annotations

import copy
import json
import os
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
from omg_cli.parity_completeness import (
    assert_completeness_promotion,
    build_completeness_proof,
    check_committed_completeness_artifacts,
    reproduce_source_index,
    validate_completeness_mapping,
    validate_completeness_policy,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_FIX = ROOT / "tests" / "fixtures" / "parity" / "completeness" / "real_source" / "OmO"
CANONICAL = ROOT / "docs" / "parity" / "omg-parity.json"
FIXTURE_REPO = "https://example.invalid/fixture-omo-real"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _commit_tree(root: Path) -> str:
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
            "source_paths": paths
            or ["packages/omo-opencode/src/config/schema/agent-names.ts"],
        },
        "omg_paths": ["omg_cli/parity_completeness.py"],
        "runtime_owner": "omg",
        "maturity": {"grok": "catalogued"},
        "evidence": {
            "tests": ["tests/test_parity_real_source_omo.py"],
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
            "repository": FIXTURE_REPO if sid == "OmO" else f"https://example.invalid/{sid}",
            "revision": pin,
            "kind": "commit",
        }
        for sid in SOURCE_STATUS_IDS
    }
    if pins:
        default_pins.update(pins)
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
        "capabilities": capabilities or [],
        "gaps": gaps if gaps is not None else [],
    }


def _materialize_real_omo(dest: Path) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REAL_FIX, dest)
    return _commit_tree(dest)


def _v2_policy(*, repository: str = FIXTURE_REPO) -> dict:
    return {
        "store_kind": "parity-completeness-policy",
        "schema_version": 1,
        "source": "OmO",
        "repository": repository,
        "discovery_rules": {
            "version": 2,
            "authoritative_registries": [
                {
                    "id": "builtin-agents-skills",
                    "path": "packages/omo-opencode/src/config/schema/agent-names.ts",
                    "extraction_method": "omo_agent_names_schema_v1",
                    "options": {},
                },
                {
                    "id": "lifecycle-hooks",
                    "path": "packages/omo-opencode/src/config/schema/hooks.ts",
                    "extraction_method": "omo_zod_string_enum_v1",
                    "options": {
                        "export_name": "HookNameSchema",
                        "kind": "hook",
                        "surface_prefix": "hook",
                        "emit_catalog": False,
                    },
                },
                {
                    "id": "mcp-families",
                    "path": "packages/omo-opencode/src/mcp/types.ts",
                    "extraction_method": "omo_zod_string_enum_v1",
                    "options": {
                        "export_name": "McpNameSchema",
                        "kind": "mcp-family",
                        "surface_prefix": "mcp",
                        "emit_catalog": False,
                    },
                },
                {
                    "id": "session-commands",
                    "path": ".agents/command",
                    "extraction_method": "omo_command_tree_v1",
                    "options": {},
                },
                {
                    "id": "terminal-cli",
                    "path": "packages/omo-opencode/src/cli/cli-program.ts",
                    "extraction_method": "commander_command_graph_v1",
                    "options": {},
                },
                {
                    "id": "terminal-cli-cleanup",
                    "path": "packages/omo-opencode/src/cli/cleanup-command.ts",
                    "extraction_method": "commander_command_graph_v1",
                    "options": {},
                },
                {
                    "id": "terminal-cli-runtime",
                    "path": "packages/omo-opencode/src/cli/runtime-commands.ts",
                    "extraction_method": "commander_command_graph_v1",
                    "options": {},
                },
                {
                    "id": "terminal-cli-mcp-oauth",
                    "path": "packages/omo-opencode/src/cli/mcp-oauth/index.ts",
                    "extraction_method": "commander_command_graph_v1",
                    "options": {},
                },
                {
                    "id": "package-surface",
                    "path": "package.json",
                    "extraction_method": "package_surface_v1",
                    "options": {
                        "include_bins": True,
                        "governance_scripts": [
                            "build:schema",
                            "build:shared-skills-assets",
                            "typecheck",
                        ],
                        "required_files_roots": [
                            ".agents/command",
                            ".agents/skills",
                            "bin",
                            "packages/shared-skills",
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
    caps: list[dict] = []
    mapping_surfaces: list[dict] = []
    for surface in surfaces:
        sid = surface["surface_id"]
        cap_id = "fixture.omo." + sid.replace(":", ".").replace("*", "star").replace(
            "+", "plus"
        ).replace("|", "pipe")
        caps.append(
            _capability(
                cap_id=cap_id,
                source="OmO",
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
            "OmO": {
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
            "source": "OmO",
            "surfaces": mapping_surfaces,
        }
    )
    return inventory, mapping


def _synthetic_world(tmp_path: Path) -> tuple[Path, str, dict, dict, dict, list[dict]]:
    root = tmp_path / "OmO"
    pin = _materialize_real_omo(root)
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


def test_omo_v2_policy_discovers_schema_cli_hooks(tmp_path: Path) -> None:
    root, pin, policy, _inv, _map, surfaces = _synthetic_world(tmp_path)
    kinds = {s["kind"] for s in surfaces}
    assert kinds >= {
        "agent",
        "skill",
        "hook",
        "mcp-family",
        "command",
        "cli",
        "bin",
        "npm-script",
        "catalog",
    }
    assert any(s["surface_id"] == "agent.sisyphus" for s in surfaces)
    assert any(s["surface_id"] == "skill.git-master" for s in surfaces)
    assert any(s["surface_id"] == "hook.todo-continuation-enforcer" for s in surfaces)
    assert any(s["surface_id"] == "mcp.lsp" for s in surfaces)
    assert any(s["surface_id"] == "cli.ulw-loop" for s in surfaces)
    assert any(s["surface_id"] == "cli.oauth" for s in surfaces)
    assert any(s["surface_id"] == "cli.mcp" for s in surfaces)
    assert any(s["surface_id"] == "cli.setup" for s in surfaces)
    assert any(s["surface_id"] == "cli.uninstall" for s in surfaces)
    assert any(s["surface_id"] == "command.security-research" for s in surfaces)
    assert any(s["surface_id"] == "bin.omo" for s in surfaces)
    assert any(s["surface_id"] == "catalog.agent" for s in surfaces)
    assert pin == _git(root, "rev-parse", "HEAD").stdout.strip().lower()


def test_omo_v2_proof_is_byte_deterministic(tmp_path: Path) -> None:
    root, _pin, policy, inventory, mapping, _surfaces = _synthetic_world(tmp_path)
    proof_a = build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    proof_b = build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    assert _canonical_proof_bytes(proof_a) == _canonical_proof_bytes(proof_b)


def test_committed_omo_artifacts_validate_without_network() -> None:
    inventory = load_json_object(CANONICAL)
    artifacts = check_committed_completeness_artifacts(inventory, repo_root=ROOT)
    assert artifacts["completeness_artifacts_checked"] is True
    assert artifacts["completeness_artifacts_verified"] >= 3
    assert "OmO" in artifacts["completeness_artifact_sources"]
    assert "OMC" in artifacts["completeness_artifact_sources"]
    assert "OMX" in artifacts["completeness_artifact_sources"]
    assert artifacts["promoted_sources"] == []

    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check_parity_completeness.py"),
            "--check",
            "--source",
            "OmO",
            "--json",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["artifact_consistency_verified"] is True
    assert payload["source_reproduced"] is False
    assert payload["promotion_performed"] is False


def test_committed_omo_proof_is_source_promotion_sufficient() -> None:
    inventory = load_json_object(CANONICAL)
    before = copy.deepcopy(inventory["source_status"])
    assert before["OmO"] == "bootstrapping"

    promoted = copy.deepcopy(inventory)
    promoted["source_status"] = dict(promoted["source_status"])
    promoted["source_status"]["OmO"] = "complete"

    result = assert_completeness_promotion(promoted, repo_root=ROOT)
    assert result.completeness_gate_checked is True
    assert "OmO" in result.promoted_sources

    on_disk = load_json_object(CANONICAL)
    assert on_disk["source_status"]["OmO"] == "bootstrapping"
    assert on_disk["source_status"] == before


def test_duplicate_agent_enum_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "packages/omo-opencode/src/config/schema/agent-names.ts"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        'export const BuiltinAgentNameSchema = z.enum([\n  "sisyphus",\n  "oracle",\n  "explore",\n])',
        'export const BuiltinAgentNameSchema = z.enum([\n  "sisyphus",\n  "Sisyphus",\n  "oracle",\n  "explore",\n])',
    )
    path.write_text(text, encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="duplicate normalized enum"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_missing_hook_enum_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "packages/omo-opencode/src/config/schema/hooks.ts"
    path.write_text('import { z } from "zod"\nexport const Other = z.enum(["x"])\n', encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="HookNameSchema"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_factory_form_addcommand_unresolved_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "packages/omo-opencode/src/cli/cli-program.ts"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "program.addCommand(createMcpOAuthCommand())",
            "program.addCommand(unknownFactory())",
        ),
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(
        ContractValidationError,
        match="unresolved addCommand factory|factory .* not found|non-static addCommand",
    ):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_computed_zod_enum_member_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "packages/omo-opencode/src/config/schema/hooks.ts"
    text = path.read_text(encoding="utf-8")
    # Keep a known literal plus a computed identifier — must reject entirely.
    path.write_text(
        text.replace(
            'export const HookNameSchema = z.enum([',
            'const COMPUTED_VALUE = "computed"\n'
            'export const HookNameSchema = z.enum([',
        ).replace(
            '"todo-continuation-enforcer",',
            '"todo-continuation-enforcer",\n  COMPUTED_VALUE,',
        ),
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(
        ContractValidationError,
        match="non-string-literal enum element",
    ):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_readme_only_not_authoritative(tmp_path: Path) -> None:
    root, pin, policy, inventory, mapping, surfaces = _synthetic_world(tmp_path)
    assert not any(s["source_path"] == "README.md" for s in surfaces)
    proof = build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    assert all(s["source_path"] != "README.md" for s in proof["discovered_surfaces"])


def test_proof_refresh_does_not_mutate_inventory_claims(tmp_path: Path) -> None:
    root, _pin, policy, inventory, mapping, _surfaces = _synthetic_world(tmp_path)
    before = _inventory_claim_projection(inventory)
    build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    assert _inventory_claim_projection(inventory) == before


def test_missing_registry_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (root / "packages/omo-opencode/src/mcp/types.ts").unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="authoritative registry missing"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_empty_command_tree_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    for path in (root / ".agents" / "command").glob("*.md"):
        path.unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="no commands under"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_pin_mismatch_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    wrong = "0" * 40
    with pytest.raises(ContractValidationError, match="does not match pin_revision"):
        reproduce_source_index(policy, root, pin_revision=wrong)


def test_check_script_artifact_only_omo() -> None:
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check_parity_completeness.py"),
            "--check",
            "--source",
            "OmO",
            "--json",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["source_reproduced"] is False
