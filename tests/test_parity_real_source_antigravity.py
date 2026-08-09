"""Hermetic real-source Antigravity discovery fixtures + #78-I plan matrix."""

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
REAL_FIX = (
    ROOT / "tests" / "fixtures" / "parity" / "completeness" / "real_source" / "Antigravity"
)
CANONICAL = ROOT / "docs" / "parity" / "omg-parity.json"
FIXTURE_REPO = "https://example.invalid/fixture-antigravity-real"
PIN_REPO = "https://github.com/google-antigravity/antigravity-cli"


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
) -> dict:
    return {
        "id": cap_id,
        "category": category,
        "promise": f"promise for {cap_id}",
        "classification": classification,
        "upstream": {
            "source": source,
            "revision": pin,
            "source_paths": paths or ["README.md"],
        },
        "omg_paths": ["omg_cli/parity_completeness.py"],
        "runtime_owner": "omg",
        "maturity": {"grok": "catalogued"},
        "evidence": {
            "tests": ["tests/test_parity_real_source_antigravity.py"],
            "docs": ["docs/parity/completeness-schema-v1.md"],
            "live": [],
        },
        "issues": ["#78"],
        "gap": "fixture gap",
    }


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
            "repository": FIXTURE_REPO if sid == "Antigravity" else f"https://example.invalid/{sid}",
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


def _materialize_real_ag(dest: Path) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REAL_FIX, dest)
    return _commit_tree(dest)


def _v2_policy(*, repository: str = FIXTURE_REPO) -> dict:
    return {
        "store_kind": "parity-completeness-policy",
        "schema_version": 1,
        "source": "Antigravity",
        "repository": repository,
        "proof_kind": "documentation_catalog_seed",
        "promotion_sufficient": False,
        "discovery_rules": {
            "version": 2,
            "authoritative_registries": [
                {
                    "id": "readme-catalog",
                    "path": "README.md",
                    "extraction_method": "antigravity_readme_catalog_v1",
                    "options": {},
                },
                {
                    "id": "changelog-releases",
                    "path": "CHANGELOG.md",
                    "extraction_method": "antigravity_changelog_releases_v1",
                    "options": {},
                },
                {
                    "id": "example-title",
                    "path": "examples/title",
                    "extraction_method": "antigravity_examples_tree_v1",
                    "options": {},
                },
                {
                    "id": "example-statusline",
                    "path": "examples/statusline",
                    "extraction_method": "antigravity_examples_tree_v1",
                    "options": {},
                },
                {
                    "id": "issue-templates",
                    "path": ".github/ISSUE_TEMPLATE",
                    "extraction_method": "antigravity_issue_templates_v1",
                    "options": {},
                },
            ],
            "category_assignment": {
                "doc-binary": "antigravity",
                "doc-catalog": "antigravity",
                "doc-feature": "antigravity",
                "doc-section": "antigravity",
                "example": "antigravity",
                "issue-template": "parity_governance",
                "release": "platform_live_evidence",
            },
            "non_surface_exceptions": [],
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
    # One fixture capability per discovered category so mapping categories bind.
    caps_by_category: dict[str, str] = {}
    caps: list[dict] = []
    mapping_surfaces: list[dict] = []
    for surface in surfaces:
        cat = surface["category"]
        if cat not in caps_by_category:
            cap_id = f"fixture.ag.{cat}"
            caps_by_category[cat] = cap_id
            # Collect all source_paths used by surfaces in this category later.
            caps.append(
                _capability(
                    cap_id=cap_id,
                    source="Antigravity",
                    category=cat,
                    pin=pin,
                    paths=[],
                )
            )
        mapping_surfaces.append(
            {
                "surface_id": surface["surface_id"],
                "category": cat,
                "capability_ids": [caps_by_category[cat]],
            }
        )
    # Fill source_paths from surfaces.
    paths_by_cap: dict[str, set[str]] = {c["id"]: set() for c in caps}
    for surface in surfaces:
        cap_id = caps_by_category[surface["category"]]
        paths_by_cap[cap_id].add(surface["source_path"])
    for cap in caps:
        cap["upstream"]["source_paths"] = sorted(paths_by_cap[cap["id"]])

    mapping_surfaces.sort(key=lambda item: item["surface_id"])
    inventory = _mini_inventory(
        pin=pin,
        capabilities=caps,
        gaps=[],
        pins={
            "Antigravity": {
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
            "source": "Antigravity",
            "surfaces": mapping_surfaces,
        }
    )
    return inventory, mapping


def _synthetic_world(tmp_path: Path) -> tuple[Path, str, dict, dict, dict, list[dict]]:
    root = tmp_path / "Antigravity"
    pin = _materialize_real_ag(root)
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


def test_antigravity_discovers_docs_only_surfaces(tmp_path: Path) -> None:
    root, pin, policy, _inv, _map, surfaces = _synthetic_world(tmp_path)
    kinds = {s["kind"] for s in surfaces}
    assert kinds == {
        "doc-binary",
        "doc-catalog",
        "doc-feature",
        "doc-section",
        "example",
        "issue-template",
        "release",
    }
    assert any(s["surface_id"] == "doc.catalog.readme" for s in surfaces)
    assert any(s["surface_id"] == "doc.catalog.changelog" for s in surfaces)
    assert any(s["surface_id"] == "doc.binary.agy" for s in surfaces)
    assert any(s["surface_id"] == "example.title" for s in surfaces)
    assert any(s["surface_id"] == "example.statusline" for s in surfaces)
    assert any(s["surface_id"] == "issue-template.bug_report" for s in surfaces)
    assert any(s["surface_id"].startswith("release.") for s in surfaces)
    # No invented implementation registries.
    assert not any(
        s["source_path"].endswith("package.json")
        or "src/" in s["source_path"]
        or s["source_path"].endswith(".ts")
        for s in surfaces
    )
    assert pin == _git(root, "rev-parse", "HEAD").stdout.strip().lower()


def test_antigravity_proof_is_byte_deterministic(tmp_path: Path) -> None:
    root, _pin, policy, inventory, mapping, _surfaces = _synthetic_world(tmp_path)
    proof_a = build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    proof_b = build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    assert _canonical_proof_bytes(proof_a) == _canonical_proof_bytes(proof_b)


def test_committed_antigravity_artifacts_validate_without_network() -> None:
    inventory = load_json_object(CANONICAL)
    artifacts = check_committed_completeness_artifacts(inventory, repo_root=ROOT)
    assert artifacts["completeness_artifacts_checked"] is True
    assert artifacts["completeness_artifacts_verified"] >= 4
    assert "Antigravity" in artifacts["completeness_artifact_sources"]
    assert "OMC" in artifacts["completeness_artifact_sources"]
    assert "OMX" in artifacts["completeness_artifact_sources"]
    assert "OmO" in artifacts["completeness_artifact_sources"]
    assert artifacts["promoted_sources"] == []
    assert inventory["source_status"]["Antigravity"] == "bootstrapping"


def test_check_script_artifact_only_antigravity() -> None:
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "check_parity_completeness.py"),
            "--check",
            "--source",
            "Antigravity",
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
    assert payload["artifact_consistency_verified"] is True
    assert payload["source_reproduced"] is False
    assert payload["promotion_performed"] is False


def test_committed_antigravity_proof_is_not_source_promotion_sufficient() -> None:
    """Docs/catalog seed verifies as an artifact but must refuse promotion (#78)."""
    inventory = load_json_object(CANONICAL)
    before = copy.deepcopy(inventory["source_status"])
    assert before["Antigravity"] == "bootstrapping"

    policy = validate_completeness_policy(
        load_json_object(
            ROOT / "docs" / "parity" / "completeness" / "policies" / "Antigravity.json"
        )
    )
    proof = load_json_object(
        ROOT / "docs" / "parity" / "completeness" / "proofs" / "Antigravity.json"
    )
    assert policy["proof_kind"] == "documentation_catalog_seed"
    assert policy["promotion_sufficient"] is False
    assert proof["proof_kind"] == "documentation_catalog_seed"
    assert proof["promotion_sufficient"] is False

    promoted = copy.deepcopy(inventory)
    promoted["source_status"] = dict(promoted["source_status"])
    promoted["source_status"]["Antigravity"] = "complete"

    with pytest.raises(
        ContractValidationError, match="not promotion-sufficient|documentation"
    ):
        assert_completeness_promotion(promoted, repo_root=ROOT)

    on_disk = load_json_object(CANONICAL)
    assert on_disk["source_status"]["Antigravity"] == "bootstrapping"
    assert on_disk["source_status"] == before


def test_missing_readme_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (root / "README.md").unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="authoritative registry missing"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_missing_changelog_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    (root / "CHANGELOG.md").unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="authoritative registry missing"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_unexpected_package_json_registry_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    bad = copy.deepcopy(policy)
    bad["discovery_rules"]["authoritative_registries"].append(
        {
            "id": "fake-package",
            "path": "package.json",
            "extraction_method": "package_surface_v1",
            "options": {
                "include_bins": False,
                "governance_scripts": ["build"],
                "required_files_roots": ["README.md"],
            },
        }
    )
    bad["discovery_rules"]["category_assignment"]["npm-script"] = "parity_governance"
    bad["discovery_rules"]["category_assignment"]["bin"] = "runtime_orchestration"
    policy = validate_completeness_policy(bad)
    with pytest.raises(ContractValidationError, match="authoritative registry missing"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_synthetic_typescript_registry_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    bad = copy.deepcopy(policy)
    bad["discovery_rules"]["authoritative_registries"].append(
        {
            "id": "fake-agents",
            "path": "src/agents/definitions.ts",
            "extraction_method": "typescript_agent_registry_v1",
            "options": {"prompt_dir": "agents"},
        }
    )
    bad["discovery_rules"]["category_assignment"]["agent"] = "agents_routing"
    policy = validate_completeness_policy(bad)
    with pytest.raises(ContractValidationError, match="authoritative registry missing"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_empty_issue_templates_fails(tmp_path: Path) -> None:
    root, pin, policy, *_ = _synthetic_world(tmp_path)
    for path in (root / ".github" / "ISSUE_TEMPLATE").glob("*.yml"):
        path.unlink()
    pin = _recommit(root)
    with pytest.raises(ContractValidationError, match="no templates under"):
        reproduce_source_index(policy, root, pin_revision=pin)


def test_coverage_digest_mismatch_fails_check() -> None:
    inventory = load_json_object(CANONICAL)
    proof = load_json_object(
        ROOT / "docs" / "parity" / "completeness" / "proofs" / "Antigravity.json"
    )
    policy = validate_completeness_policy(
        load_json_object(
            ROOT / "docs" / "parity" / "completeness" / "policies" / "Antigravity.json"
        )
    )
    mapping = validate_completeness_mapping(
        load_json_object(
            ROOT / "docs" / "parity" / "completeness" / "mappings" / "Antigravity.json"
        )
    )
    seed = load_json_object(
        ROOT / "docs" / "parity" / "upstream-snapshots" / "Antigravity.json"
    )
    from omg_cli.parity_completeness import verify_completeness_proof

    tampered = copy.deepcopy(proof)
    tampered["coverage_digest"] = "0" * 64
    with pytest.raises(ContractValidationError, match="coverage_digest"):
        verify_completeness_proof(
            tampered,
            policy=policy,
            inventory=inventory,
            seed=seed,
            mapping=mapping,
            require_no_unresolved=True,
        )


def test_proof_refresh_does_not_mutate_inventory_claims(tmp_path: Path) -> None:
    root, _pin, policy, inventory, mapping, _surfaces = _synthetic_world(tmp_path)
    before = _inventory_claim_projection(inventory)
    build_completeness_proof(
        policy=policy, inventory=inventory, upstream_root=root, seed=None, mapping=mapping
    )
    assert _inventory_claim_projection(inventory) == before


def test_committed_policy_repository_matches_inventory_pin() -> None:
    inventory = load_json_object(CANONICAL)
    policy = load_json_object(
        ROOT / "docs" / "parity" / "completeness" / "policies" / "Antigravity.json"
    )
    pin = inventory["upstream_pins"]["Antigravity"]
    assert policy["repository"] == pin["repository"] == PIN_REPO
    assert pin["revision"] == "bfab12dac5bd090015a89cf82e65093d13b567d9"
    proof = load_json_object(
        ROOT / "docs" / "parity" / "completeness" / "proofs" / "Antigravity.json"
    )
    assert proof["pin_revision"] == pin["revision"]
    assert inventory["source_status"]["Antigravity"] == "bootstrapping"
