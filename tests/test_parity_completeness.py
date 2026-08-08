"""Fail-closed completeness promotion proof gate (#78-D)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
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
    check_completeness_promotion_gate,
    digest_seed_catalog,
    plan_completeness_proof,
    reproduce_source_index,
    validate_completeness_policy,
    verify_completeness_proof,
)


ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "parity" / "completeness"
CANONICAL = ROOT / "docs" / "parity" / "omg-parity.json"
FIXTURE_PIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FIXTURE_REPO = "https://example.invalid/fixture-omc"


def _bootstrapping_statuses() -> tuple[dict[str, str], dict[str, str]]:
    categories = {c: "bootstrapping" for c in sorted(PARITY_CATEGORY_TAXONOMY)}
    sources = {s: "bootstrapping" for s in SOURCE_STATUS_IDS}
    return categories, sources


def _capability(
    *,
    cap_id: str,
    source: str,
    category: str,
    classification: str = "omg_native",
    paths: list[str] | None = None,
    alias_of: str | None = None,
    pin: str = FIXTURE_PIN,
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
            "tests": ["tests/test_parity_completeness.py"],
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
    capabilities: list[dict] | None = None,
    inventory_status: str = "bootstrapping",
    source_status: dict[str, str] | None = None,
    category_status: dict[str, str] | None = None,
    gaps: list[dict] | None = None,
    pins: dict[str, dict] | None = None,
) -> dict:
    categories, sources = _bootstrapping_statuses()
    if category_status:
        categories.update(category_status)
    if source_status:
        sources.update(source_status)
    default_pins = {
        sid: {
            "repository": FIXTURE_REPO if sid == "OMC" else f"https://example.invalid/{sid}",
            "revision": FIXTURE_PIN,
            "kind": "commit",
        }
        for sid in SOURCE_STATUS_IDS
    }
    if pins:
        default_pins.update(pins)
    caps = capabilities or [
        _capability(
            cap_id="fixture.omc.hello",
            source="OMC",
            category="runtime_orchestration",
        )
    ]
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
        "gaps": gaps
        if gaps is not None
        else [
            {
                "id": "gap.fixture.p0",
                "priority": "P0",
                "status": "open",
                "issues": ["#78"],
                "capability_ids": [caps[0]["id"]],
                "summary": "fixture open P0",
            }
        ],
    }


def _policy(source: str = "OMC") -> dict:
    return load_json_object(FIX / "policies" / "OMC.json" if source == "OMC" else FIX / "policies" / f"{source}.json")


def _seed(source: str = "OMC") -> dict:
    return load_json_object(FIX / "seeds" / "OMC.json")


def _mappings() -> dict:
    return load_json_object(FIX / "mappings" / "OMC.json")


def _upstream(source: str = "OMC") -> Path:
    return FIX / "upstream" / source


def _write_source_tree(
    root: Path,
    *,
    surfaces: list[dict],
    kind: str = "command",
) -> None:
    reg_dir = root / "registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for surface in surfaces:
        path = surface["path"]
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(
            surface.get("content", f"# {surface['id']}\n"), encoding="utf-8"
        )
        entries.append(
            {
                "id": surface["id"],
                "path": path,
                "anchor": surface.get("anchor", surface["id"]),
            }
        )
    (reg_dir / "commands.json").write_text(
        json.dumps({"kind": kind, "entries": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")


def _policy_for(
    source: str,
    repository: str,
    *,
    category: str = "runtime_orchestration",
) -> dict:
    return {
        "store_kind": "parity-completeness-policy",
        "schema_version": 1,
        "source": source,
        "repository": repository,
        "discovery_rules": {
            "version": 1,
            "authoritative_registries": [
                {
                    "path": "registry/commands.json",
                    "extraction_method": "json_registry_v1",
                }
            ],
            "category_assignment": {"command": category},
            "non_surface_exceptions": [
                {
                    "path": "README.md",
                    "rationale": "README is not a registered surface",
                    "issue": "#78",
                }
            ],
        },
    }


def test_bootstrapping_inventory_needs_no_completeness_proof() -> None:
    inventory = load_json_object(CANONICAL)
    result = check_completeness_promotion_gate(inventory, repo_root=ROOT)
    assert result["completeness_gate_checked"] is True
    assert result["completeness_proofs_required"] is False
    assert result["completeness_proofs_verified"] == 0
    assert result["promoted_sources"] == []
    assert result["promoted_categories"] == []

    payload = check_parity_inventory(
        inventory_path=CANONICAL, repo_root=ROOT, strict=True
    )
    assert payload["ok"] is True
    assert payload["completeness_proofs_required"] is False


def test_status_promotion_without_proof_fails_even_after_p0_gaps_close(
    tmp_path: Path,
) -> None:
    inventory = load_json_object(CANONICAL)
    broken = copy.deepcopy(inventory)
    for gap in broken["gaps"]:
        if gap.get("priority") == "P0":
            gap["status"] = "closed"
    broken["inventory_status"] = "complete"
    broken["category_status"] = {k: "complete" for k in broken["category_status"]}
    broken["source_status"] = {k: "complete" for k in broken["source_status"]}
    path = tmp_path / "omg-parity.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(ContractValidationError, match="completeness proof"):
        check_parity_inventory(inventory_path=path, repo_root=ROOT, strict=True)

    with pytest.raises(ContractValidationError, match="completeness proof"):
        assert_completeness_promotion(broken, repo_root=ROOT)


def test_seed_catalogue_is_not_a_completeness_proof() -> None:
    inventory = _mini_inventory(
        source_status={"OMC": "complete"},
        gaps=[],
    )
    seed = _seed()
    # Offering the seed in place of a proof must fail closed.
    with pytest.raises(ContractValidationError, match="not a completeness proof"):
        assert_completeness_promotion(
            inventory,
            proofs_by_source={"OMC": seed},  # type: ignore[arg-type]
            policies_by_source={"OMC": _policy()},
            seeds_by_source={"OMC": seed},
            allow_seed_as_proof=True,
        )
    with pytest.raises(ContractValidationError, match="completeness_proof|store_kind"):
        assert_completeness_promotion(
            inventory,
            proofs_by_source={"OMC": seed},  # type: ignore[arg-type]
            policies_by_source={"OMC": _policy()},
            seeds_by_source={"OMC": seed},
        )


def test_valid_hermetic_source_proof_passes() -> None:
    inventory = _mini_inventory(gaps=[])
    policy = _policy()
    seed = _seed()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=_upstream(),
        seed=seed,
        surface_mappings=_mappings(),
    )
    verified = verify_completeness_proof(
        proof,
        policy=policy,
        inventory=inventory,
        seed=seed,
        upstream_root=_upstream(),
        require_no_unresolved=True,
    )
    assert verified["ok"] is True
    assert verified["surfaces"] == 1

    inventory["source_status"]["OMC"] = "complete"
    result = assert_completeness_promotion(
        inventory,
        proofs_by_source={"OMC": proof},
        policies_by_source={"OMC": policy},
        seeds_by_source={"OMC": seed},
        upstream_roots={"OMC": _upstream()},
    )
    assert result.completeness_proofs_required is True
    assert result.completeness_proofs_verified == 1
    assert result.promoted_sources == ("OMC",)


def test_source_proof_binds_repository_pin_policy_seed_and_inventory() -> None:
    inventory = _mini_inventory(gaps=[])
    policy = _policy()
    seed = _seed()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=_upstream(),
        seed=seed,
        surface_mappings=_mappings(),
    )

    # Pin drift
    drifted = copy.deepcopy(inventory)
    drifted["upstream_pins"]["OMC"]["revision"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    drifted["capabilities"][0]["upstream"]["revision"] = (
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )
    with pytest.raises(ContractValidationError, match="pin_revision|coverage_digest"):
        verify_completeness_proof(
            proof, policy=policy, inventory=drifted, seed=seed
        )

    # Policy digest drift
    bad_policy = copy.deepcopy(policy)
    bad_policy["discovery_rules"]["non_surface_exceptions"][0]["rationale"] = "changed"
    with pytest.raises(ContractValidationError, match="policy_digest"):
        verify_completeness_proof(
            proof, policy=bad_policy, inventory=inventory, seed=seed
        )

    # Seed digest drift
    bad_seed = copy.deepcopy(seed)
    bad_seed["capabilities"][0]["promise"] = "changed promise"
    with pytest.raises(ContractValidationError, match="seed_digest"):
        verify_completeness_proof(
            proof, policy=policy, inventory=inventory, seed=bad_seed
        )

    # Inventory coverage drift (path change)
    bad_inv = copy.deepcopy(inventory)
    bad_inv["capabilities"][0]["upstream"]["source_paths"] = ["commands/other.md"]
    with pytest.raises(ContractValidationError, match="coverage_digest"):
        verify_completeness_proof(
            proof, policy=policy, inventory=bad_inv, seed=seed
        )

    assert digest_seed_catalog(seed) == proof["seed_digest"]


def test_added_removed_or_changed_surface_breaks_reproduction(tmp_path: Path) -> None:
    inventory = _mini_inventory(gaps=[])
    policy = _policy()
    seed = _seed()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=_upstream(),
        seed=seed,
        surface_mappings=_mappings(),
    )

    # Content change
    changed = tmp_path / "changed"
    _write_source_tree(
        changed,
        surfaces=[
            {
                "id": "cmd.hello",
                "path": "commands/hello.md",
                "content": "# hello MUTATED\n",
            }
        ],
    )
    with pytest.raises(ContractValidationError, match="reproduction drift"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=changed,
        )

    # Added surface
    added = tmp_path / "added"
    _write_source_tree(
        added,
        surfaces=[
            {"id": "cmd.hello", "path": "commands/hello.md"},
            {"id": "cmd.extra", "path": "commands/extra.md"},
        ],
    )
    with pytest.raises(ContractValidationError, match="reproduction drift"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=added,
        )

    # Removed surface
    removed = tmp_path / "removed"
    _write_source_tree(removed, surfaces=[])
    with pytest.raises(ContractValidationError, match="reproduction drift"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=removed,
        )


def test_unmapped_discovered_surface_fails_closed() -> None:
    inventory = _mini_inventory(gaps=[])
    policy = _policy()
    seed = _seed()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=_upstream(),
        seed=seed,
        surface_mappings={},  # omit mapping
    )
    assert proof["unresolved_surfaces"] == ["cmd.hello"]
    with pytest.raises(ContractValidationError, match="unresolved surfaces"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=_upstream(),
            require_no_unresolved=True,
        )


def test_surface_mapping_rejects_alias_only_or_cross_source_target() -> None:
    caps = [
        _capability(
            cap_id="fixture.omc.hello",
            source="OMC",
            category="runtime_orchestration",
        ),
        _capability(
            cap_id="fixture.omc.hello.alias",
            source="OMC",
            category="runtime_orchestration",
            classification="alias",
            alias_of="fixture.omc.hello",
        ),
        _capability(
            cap_id="fixture.omx.other",
            source="OMX",
            category="runtime_orchestration",
            paths=["commands/other.md"],
        ),
    ]
    inventory = _mini_inventory(capabilities=caps, gaps=[])
    policy = _policy()

    with pytest.raises(ContractValidationError, match="alias-only"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=_upstream(),
            seed=_seed(),
            surface_mappings={"cmd.hello": ["fixture.omc.hello.alias"]},
        )

    with pytest.raises(ContractValidationError, match="cross-source"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=_upstream(),
            seed=_seed(),
            surface_mappings={"cmd.hello": ["fixture.omx.other"]},
        )


def test_category_promotion_requires_every_source_partition(tmp_path: Path) -> None:
    # Build four tiny upstreams + proofs; promote one category.
    policies: dict[str, dict] = {}
    proofs: dict[str, dict] = {}
    roots: dict[str, Path] = {}
    seeds: dict[str, dict] = {}
    caps: list[dict] = []

    for source in SOURCE_STATUS_IDS:
        repo = f"https://example.invalid/{source}"
        up = tmp_path / source
        surface_id = f"cmd.{source.lower()}"
        _write_source_tree(
            up,
            surfaces=[{"id": surface_id, "path": f"commands/{source}.md"}],
        )
        policy = _policy_for(source, repo)
        policies[source] = policy
        roots[source] = up
        cap_id = f"fixture.{source.lower()}.cmd"
        caps.append(
            _capability(
                cap_id=cap_id,
                source=source,
                category="runtime_orchestration",
                paths=[f"commands/{source}.md"],
            )
        )
        seed = {
            "source": source,
            "pin_revision": FIXTURE_PIN,
            "capabilities": [
                {
                    "id": cap_id,
                    "source_paths": [f"commands/{source}.md"],
                    "promise": f"promise {source}",
                }
            ],
        }
        seeds[source] = seed

    inventory = _mini_inventory(
        capabilities=caps,
        gaps=[],
        pins={
            sid: {
                "repository": f"https://example.invalid/{sid}",
                "revision": FIXTURE_PIN,
                "kind": "commit",
            }
            for sid in SOURCE_STATUS_IDS
        },
    )
    for source in SOURCE_STATUS_IDS:
        cap_id = f"fixture.{source.lower()}.cmd"
        surface_id = f"cmd.{source.lower()}"
        proofs[source] = build_completeness_proof(
            policy=policies[source],
            inventory=inventory,
            upstream_root=roots[source],
            seed=seeds[source],
            surface_mappings={surface_id: [cap_id]},
        )

    # Missing one source proof → category promotion fails.
    incomplete_proofs = {k: v for k, v in proofs.items() if k != "Antigravity"}
    inventory["category_status"]["runtime_orchestration"] = "complete"
    with pytest.raises(ContractValidationError, match="every source|missing Antigravity"):
        assert_completeness_promotion(
            inventory,
            proofs_by_source=incomplete_proofs,
            policies_by_source=policies,
            seeds_by_source=seeds,
            upstream_roots=roots,
        )

    # All four present → pass.
    result = assert_completeness_promotion(
        inventory,
        proofs_by_source=proofs,
        policies_by_source=policies,
        seeds_by_source=seeds,
        upstream_roots=roots,
    )
    assert result.promoted_categories == ("runtime_orchestration",)
    assert result.completeness_proofs_verified == 4


def test_explicit_empty_category_partition_is_reproducible(tmp_path: Path) -> None:
    up = tmp_path / "OMC"
    _write_source_tree(
        up,
        surfaces=[{"id": "cmd.hello", "path": "commands/hello.md"}],
    )
    policy = _policy_for("OMC", FIXTURE_REPO, category="runtime_orchestration")
    inventory = _mini_inventory(gaps=[])
    seed = _seed()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=up,
        seed=seed,
        surface_mappings={"cmd.hello": ["fixture.omc.hello"]},
    )
    assert "skills" in proof["empty_category_partitions"]
    assert "runtime_orchestration" not in proof["empty_category_partitions"]

    reproduced = reproduce_source_index(policy, up)
    assert "skills" in reproduced["empty_category_partitions"]
    verify_completeness_proof(
        proof,
        policy=policy,
        inventory=inventory,
        seed=seed,
        upstream_root=up,
    )


def test_source_paths_are_confined_and_symlinks_rejected(tmp_path: Path) -> None:
    up = tmp_path / "OMC"
    _write_source_tree(
        up,
        surfaces=[{"id": "cmd.hello", "path": "commands/hello.md"}],
    )
    policy = validate_completeness_policy(_policy_for("OMC", FIXTURE_REPO))

    # Escape via .. in registry entry
    escape = tmp_path / "escape"
    _write_source_tree(
        escape,
        surfaces=[{"id": "cmd.hello", "path": "commands/hello.md"}],
    )
    evil = {
        "kind": "command",
        "entries": [
            {
                "id": "cmd.evil",
                "path": "../outside.md",
                "anchor": "x",
            }
        ],
    }
    (escape / "registry" / "commands.json").write_text(
        json.dumps(evil), encoding="utf-8"
    )
    (tmp_path / "outside.md").write_text("nope\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="relative POSIX|escapes"):
        reproduce_source_index(policy, escape)

    # Symlink rejected
    link_root = tmp_path / "linkroot"
    _write_source_tree(
        link_root,
        surfaces=[{"id": "cmd.hello", "path": "commands/hello.md"}],
    )
    target = link_root / "commands" / "hello.md"
    linked = link_root / "commands" / "linked.md"
    linked.symlink_to(target)
    (link_root / "registry" / "commands.json").write_text(
        json.dumps(
            {
                "kind": "command",
                "entries": [
                    {"id": "cmd.link", "path": "commands/linked.md", "anchor": "x"}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractValidationError, match="symlink"):
        reproduce_source_index(policy, link_root)


def test_plan_mode_never_mutates_inventory_or_proof(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inv.json"
    inventory = _mini_inventory(gaps=[])
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    before = inventory_path.read_bytes()
    proof_path = tmp_path / "proof.json"
    proof_path.write_text("{}\n", encoding="utf-8")
    proof_before = proof_path.read_bytes()

    plan = plan_completeness_proof(
        policy=_policy(),
        inventory=inventory,
        upstream_root=_upstream(),
        seed=_seed(),
        surface_mappings=_mappings(),
    )
    assert plan["mutates_inventory"] is False
    assert plan["mutates_proof_artifact"] is False
    assert "candidate_proof" in plan
    assert inventory_path.read_bytes() == before
    assert proof_path.read_bytes() == proof_before

    script = subprocess.run(
        [
            sys.executable,
            "scripts/check_parity_completeness.py",
            "--plan",
            "--source",
            "OMC",
            "--inventory",
            str(inventory_path),
            "--policy",
            str(FIX / "policies" / "OMC.json"),
            "--upstream-root",
            str(_upstream()),
            "--seed",
            str(FIX / "seeds" / "OMC.json"),
            "--mappings",
            str(FIX / "mappings" / "OMC.json"),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert script.returncode == 0, script.stderr
    payload = json.loads(script.stdout)
    assert payload["mutates_inventory"] is False
    assert inventory_path.read_bytes() == before
    assert proof_path.read_bytes() == proof_before
