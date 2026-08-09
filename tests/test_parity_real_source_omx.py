"""Hermetic real-source OMX discovery fixtures + #78-G plan matrix."""

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
from omg_cli.parity_check import check_parity_inventory
from omg_cli.parity_completeness import (
    assert_completeness_promotion,
    build_completeness_proof,
    check_committed_completeness_artifacts,
    reproduce_source_index,
    validate_completeness_mapping,
    validate_completeness_policy,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_FIX = ROOT / "tests" / "fixtures" / "parity" / "completeness" / "real_source" / "OMX"
CANONICAL = ROOT / "docs" / "parity" / "omg-parity.json"
FIXTURE_REPO = "https://example.invalid/fixture-omx-real"


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
            "source_paths": paths or ["templates/catalog-manifest.json"],
        },
        "omg_paths": ["omg_cli/parity_completeness.py"],
        "runtime_owner": "omg",
        "maturity": {"grok": "catalogued"},
        "evidence": {
            "tests": ["tests/test_parity_real_source_omx.py"],
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
            "repository": FIXTURE_REPO if sid == "OMX" else f"https://example.invalid/{sid}",
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


def _materialize_real_omx(dest: Path) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REAL_FIX, dest)
    return _commit_tree(dest)


def _v2_policy(*, repository: str = FIXTURE_REPO) -> dict:
    return {
        "store_kind": "parity-completeness-policy",
        "schema_version": 1,
        "source": "OMX",
        "repository": repository,
        "discovery_rules": {
            "version": 2,
            "authoritative_registries": [
                {
                    "id": "omx-catalog",
                    "path": "templates/catalog-manifest.json",
                    "extraction_method": "omx_catalog_manifest_v1",
                    "options": {"skills_dir": "skills", "prompts_dir": "prompts"},
                },
                {
                    "id": "omx-help-cli",
                    "path": "src/cli/index.ts",
                    "extraction_method": "omx_help_surface_v1",
                    "options": {},
                },
                {
                    "id": "omx-launcher",
                    "path": "src/cli/omx.ts",
                    "extraction_method": "omx_launcher_bin_v1",
                    "options": {"bin_name": "omx"},
                },
                {
                    "id": "codex-plugin",
                    "path": "plugins/oh-my-codex/.codex-plugin/plugin.json",
                    "extraction_method": "codex_plugin_manifest_v1",
                    "options": {},
                },
                {
                    "id": "codex-hooks",
                    "path": "plugins/oh-my-codex/hooks/hooks.json",
                    "extraction_method": "claude_hooks_manifest_v1",
                    "options": {"plugin_root": "plugins/oh-my-codex"},
                },
                {
                    "id": "package-surface",
                    "path": "package.json",
                    "extraction_method": "package_surface_v1",
                    "options": {
                        "include_bins": False,
                        "governance_scripts": [
                            "sync:plugin:check",
                            "verify:native-agents",
                            "verify:plugin-bundle",
                        ],
                        "required_files_roots": [
                            ".agents/plugins/marketplace.json",
                            "plugins",
                            "prompts",
                            "skills",
                            "templates",
                        ],
                    },
                },
            ],
            "category_assignment": {
                "agent": "agents_routing",
                "agent_alias": "agents_routing",
                "agent_catalog": "agents_routing",
                "agent_deprecated": "agents_routing",
                "agent_internal": "agents_routing",
                "agent_merged": "agents_routing",
                "bin": "runtime_orchestration",
                "catalog": "skills",
                "cli": "runtime_orchestration",
                "hook": "hooks",
                "npm-script": "parity_governance",
                "skill": "skills",
                "skill_alias": "skills",
                "skill_deprecated": "skills",
                "skill_internal": "skills",
                "skill_merged": "skills",
            },
            "non_surface_exceptions": [
                {
                    "path": "README.md",
                    "rationale": "Narrative README is not a registered surface",
                    "issue": "#78",
                },
                {
                    "path": "prompts/explore-harness.md",
                    "rationale": "Harness prompt not catalogued",
                    "issue": "#78",
                },
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
        cap_id = "fixture.omx." + sid.replace(":", ".").replace("*", "star").replace(
            "+", "plus"
        ).replace("|", "pipe")
        caps.append(
            _capability(
                cap_id=cap_id,
                source="OMX",
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
            "OMX": {
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
            "source": "OMX",
            "surfaces": mapping_surfaces,
        }
    )
    return inventory, mapping


def _synthetic_world(tmp_path: Path) -> tuple[Path, str, dict, dict, dict, list[dict]]:
    root = tmp_path / "OMX"
    pin = _materialize_real_omx(root)
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


def test_omx_v2_policy_discovers_catalog_cli_hooks(tmp_path: Path) -> None:
    root, pin, policy, _inv, _map, surfaces = _synthetic_world(tmp_path)
    kinds = {s["kind"] for s in surfaces}
    assert kinds >= {
        "skill",
        "skill_alias",
        "skill_merged",
        "catalog",
        "agent",
        "cli",
        "hook",
        "bin",
        "npm-script",
    }
    assert any(s["surface_id"] == "skill.demo" for s in surfaces)
    assert any(s["surface_id"] == "cli.launch" for s in surfaces)
    assert any(s["surface_id"] == "bin.omx" for s in surfaces)
    assert any(s["surface_id"] == "catalog.skills" for s in surfaces)
    assert any(s["surface_id"] == "catalog.agents" for s in surfaces)
    assert any(s["surface_id"].startswith("hook.") for s in surfaces)
    assert pin == _git(root, "rev-parse", "HEAD").stdout.strip().lower()


def test_omx_v2_proof_is_byte_deterministic(tmp_path: Path) -> None:
    root, _pin, policy, inventory, mapping, _surfaces = _synthetic_world(tmp_path)
    proof_a = build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    proof_b = build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    assert _canonical_proof_bytes(proof_a) == _canonical_proof_bytes(proof_b)


def test_committed_omx_artifacts_validate_without_network() -> None:
    inventory = load_json_object(CANONICAL)
    artifacts = check_committed_completeness_artifacts(inventory, repo_root=ROOT)
    assert artifacts["completeness_artifacts_checked"] is True
    assert artifacts["completeness_artifacts_verified"] >= 2
    assert "OMX" in artifacts["completeness_artifact_sources"]
    assert "OMC" in artifacts["completeness_artifact_sources"]
    assert artifacts["promoted_sources"] == []

    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check_parity_completeness.py"),
            "--check",
            "--source",
            "OMX",
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


def test_committed_omx_proof_is_source_promotion_sufficient() -> None:
    inventory = load_json_object(CANONICAL)
    before = copy.deepcopy(inventory["source_status"])
    assert before["OMX"] == "bootstrapping"

    promoted = copy.deepcopy(inventory)
    promoted["source_status"] = dict(promoted["source_status"])
    promoted["source_status"]["OMX"] = "complete"

    result = assert_completeness_promotion(promoted, repo_root=ROOT)
    assert result.completeness_gate_checked is True
    assert "OMX" in result.promoted_sources

    on_disk = load_json_object(CANONICAL)
    assert on_disk["source_status"]["OMX"] == "bootstrapping"
    assert on_disk["source_status"] == before


def test_undeclared_skill_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    hidden = root / "skills" / "hidden" / "SKILL.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("# hidden\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="undeclared skill"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_undeclared_prompt_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (root / "prompts" / "ghost.md").write_text("# ghost\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="undeclared prompt"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_duplicate_normalized_skill_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    catalog = root / "templates" / "catalog-manifest.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    payload["skills"].append(
        {
            "name": "demo",
            "category": "execution",
            "status": "deprecated",
            "core": False,
            "internalRequired": False,
        }
    )
    catalog.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="duplicate normalized skill"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_invalid_catalog_status_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    catalog = root / "templates" / "catalog-manifest.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    payload["skills"][0]["status"] = "weird"
    catalog.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="invalid catalog status"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_alias_without_canonical_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    catalog = root / "templates" / "catalog-manifest.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    for skill in payload["skills"]:
        if skill["name"] == "git-master":
            skill.pop("canonical", None)
    catalog.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="without canonical"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_merged_without_canonical_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    catalog = root / "templates" / "catalog-manifest.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    for skill in payload["skills"]:
        if skill["name"] == "legacy-merged":
            skill.pop("canonical", None)
    catalog.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="without canonical"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_canonical_target_missing_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    catalog = root / "templates" / "catalog-manifest.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    for skill in payload["skills"]:
        if skill["name"] == "legacy-merged":
            skill["canonical"] = "no-such-target"
    catalog.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="canonical target missing"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_canonical_target_not_installable_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    catalog = root / "templates" / "catalog-manifest.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    for skill in payload["skills"]:
        if skill["name"] == "legacy-merged":
            skill["canonical"] = "old-skill"  # deprecated — not installable
    catalog.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="canonical target not installable"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_missing_hook_target_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (root / "plugins" / "oh-my-codex" / "hooks" / "codex-native-hook.mjs").unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="hook script missing"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_hook_traversal_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    hooks = root / "plugins" / "oh-my-codex" / "hooks" / "hooks.json"
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": 'node "${PLUGIN_ROOT}/../outside/hook.mjs"',
                                }
                            ]
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


def test_hook_symlink_fails(tmp_path: Path) -> None:
    root = tmp_path / "OMX-symlink"
    pin = _materialize_real_omx(root)
    policy = validate_completeness_policy(_v2_policy())
    script = root / "plugins" / "oh-my-codex" / "hooks" / "codex-native-hook.mjs"
    script.unlink()
    script.symlink_to("/tmp/omg-omx-hook-escape")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="symlink"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_missing_plugin_manifest_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (
        root / "plugins" / "oh-my-codex" / ".codex-plugin" / "plugin.json"
    ).unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="authoritative registry missing"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_help_parser_dynamic_mutation_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    path = root / "src" / "cli" / "index.ts"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nHELP = HELP + 'x';\n",
        encoding="utf-8",
    )
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="HELP parser dynamic mutation"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_package_required_roots_missing_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    pkg = root / "package.json"
    payload = json.loads(pkg.read_text(encoding="utf-8"))
    payload["files"] = ["skills/"]
    pkg.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="missing required root"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_mapping_unknown_capability_fails(tmp_path: Path) -> None:
    root, pin, policy, inventory, mapping, surfaces = _synthetic_world(tmp_path)
    bad = copy.deepcopy(mapping)
    bad["surfaces"][0]["capability_ids"] = ["fixture.omx.does.not.exist"]
    with pytest.raises(ContractValidationError, match="unknown capability"):
        build_completeness_proof(
            policy=policy, inventory=inventory, upstream_root=root, mapping=bad
        )


def test_omx_artifacts_do_not_promote_maturity_or_live_evidence() -> None:
    before_inv = load_json_object(CANONICAL)
    before = _inventory_claim_projection(before_inv)
    check_committed_completeness_artifacts(before_inv, repo_root=ROOT)
    check_parity_inventory(inventory_path=CANONICAL, repo_root=ROOT, strict=True)
    after_inv = load_json_object(CANONICAL)
    after = _inventory_claim_projection(after_inv)
    assert after == before
    assert after["source_status"]["OMX"] == "bootstrapping"
    assert after["source_status"]["OMC"] == "bootstrapping"


def test_networkless_omx_check_never_claims_reproduction() -> None:
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check_parity_completeness.py"),
            "--check",
            "--source",
            "OMX",
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
    assert payload["source_reproduced"] is False
    assert payload["artifact_consistency_verified"] is True


@pytest.mark.skipif(
    not Path("/private/tmp/omg-omx-pin").is_dir(),
    reason="pinned OMX checkout not present",
)
def test_upstream_root_reproduces_committed_omx_proof() -> None:
    """Optional local pin reproduction (not required in CI)."""
    upstream = Path("/private/tmp/omg-omx-pin")
    head = _git(upstream, "rev-parse", "HEAD").stdout.strip().lower()
    if head != "435d4a9cc982ffaf83fabbfbb8711ae6c178ffca":
        pytest.skip("OMX pin checkout not at expected revision")
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check_parity_completeness.py"),
            "--check",
            "--source",
            "OMX",
            "--upstream-root",
            str(upstream),
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
    assert payload["artifact_consistency_verified"] is True
    assert payload["source_reproduced"] is True
    assert payload["promotion_performed"] is False
