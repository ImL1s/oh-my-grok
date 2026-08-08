"""Fail-closed completeness promotion proof gate (#78-D)."""

from __future__ import annotations

import copy
import json
import shutil
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
    authenticate_pinned_checkout,
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
FIXTURE_REPO = "https://example.invalid/fixture-omc"
# Placeholder OID for tests that never open an upstream checkout.
_PLACEHOLDER_PIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _bootstrapping_statuses() -> tuple[dict[str, str], dict[str, str]]:
    categories = {c: "bootstrapping" for c in sorted(PARITY_CATEGORY_TAXONOMY)}
    sources = {s: "bootstrapping" for s in SOURCE_STATUS_IDS}
    return categories, sources


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


def _materialize_omc_upstream(dest: Path) -> str:
    """Copy hermetic OMC fixture files into dest and commit; return HEAD sha."""
    src = FIX / "upstream" / "OMC"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return _commit_tree(dest)


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
    pin: str,
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
            "revision": pin,
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
            pin=pin,
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
    return load_json_object(
        FIX / "policies" / "OMC.json"
        if source == "OMC"
        else FIX / "policies" / f"{source}.json"
    )


def _seed(*, pin: str) -> dict:
    seed = copy.deepcopy(load_json_object(FIX / "seeds" / "OMC.json"))
    seed["pin_revision"] = pin
    return seed


def _mappings() -> dict:
    return load_json_object(FIX / "mappings" / "OMC.json")


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


def _omc_world(tmp_path: Path) -> tuple[Path, str, dict, dict]:
    """Pinned OMC checkout + matching inventory/seed."""
    root = tmp_path / "OMC"
    pin = _materialize_omc_upstream(root)
    inventory = _mini_inventory(pin=pin, gaps=[])
    seed = _seed(pin=pin)
    return root, pin, inventory, seed


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
        pin=_PLACEHOLDER_PIN,
        source_status={"OMC": "complete"},
        gaps=[],
    )
    seed = _seed(pin=_PLACEHOLDER_PIN)
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


def test_valid_hermetic_source_proof_passes(tmp_path: Path) -> None:
    upstream, pin, inventory, seed = _omc_world(tmp_path)
    policy = _policy()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=upstream,
        seed=seed,
        surface_mappings=_mappings(),
    )
    assert proof["checkout_provenance"]["observed_revision"] == pin
    assert proof["pin_revision"] == pin
    verified = verify_completeness_proof(
        proof,
        policy=policy,
        inventory=inventory,
        seed=seed,
        upstream_root=upstream,
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
        upstream_roots={"OMC": upstream},
    )
    assert result.completeness_proofs_required is True
    assert result.completeness_proofs_verified == 1
    assert result.promoted_sources == ("OMC",)


def test_proof_against_wrong_head_fails(tmp_path: Path) -> None:
    """Proof pin A cannot authenticate a checkout whose HEAD is B."""
    upstream_a = tmp_path / "A"
    pin_a = _materialize_omc_upstream(upstream_a)
    inventory = _mini_inventory(pin=pin_a, gaps=[])
    seed = _seed(pin=pin_a)
    policy = _policy()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=upstream_a,
        seed=seed,
        surface_mappings=_mappings(),
    )

    upstream_b = tmp_path / "B"
    shutil.copytree(upstream_a, upstream_b)
    shutil.rmtree(upstream_b / ".git")
    (upstream_b / "commands" / "hello.md").write_text("# hello B\n", encoding="utf-8")
    pin_b = _commit_tree(upstream_b)
    assert pin_b != pin_a

    with pytest.raises(ContractValidationError, match="does not match pin_revision"):
        authenticate_pinned_checkout(upstream_b, pin_a)

    with pytest.raises(ContractValidationError, match="does not match pin_revision"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=upstream_b,
        )

    with pytest.raises(ContractValidationError, match="does not match pin_revision"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=upstream_b,
            seed=seed,
            surface_mappings=_mappings(),
        )


def test_dirty_checkout_fails(tmp_path: Path) -> None:
    upstream, pin, inventory, seed = _omc_world(tmp_path)
    policy = _policy()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=upstream,
        seed=seed,
        surface_mappings=_mappings(),
    )

    (upstream / "commands" / "hello.md").write_text("# hello DIRTY\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="dirty"):
        authenticate_pinned_checkout(upstream, pin)
    with pytest.raises(ContractValidationError, match="dirty"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=upstream,
        )

    assert _git(upstream, "checkout", "--", "commands/hello.md").returncode == 0
    (upstream / "untracked-extra.txt").write_text("nope\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="dirty"):
        authenticate_pinned_checkout(upstream, pin)


def test_source_proof_binds_repository_pin_policy_seed_and_inventory(
    tmp_path: Path,
) -> None:
    upstream, _pin, inventory, seed = _omc_world(tmp_path)
    policy = _policy()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=upstream,
        seed=seed,
        surface_mappings=_mappings(),
    )

    drifted = copy.deepcopy(inventory)
    other = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    drifted["upstream_pins"]["OMC"]["revision"] = other
    drifted["capabilities"][0]["upstream"]["revision"] = other
    with pytest.raises(ContractValidationError, match="pin_revision|coverage_digest"):
        verify_completeness_proof(
            proof, policy=policy, inventory=drifted, seed=seed
        )

    bad_policy = copy.deepcopy(policy)
    bad_policy["discovery_rules"]["non_surface_exceptions"][0]["rationale"] = "changed"
    with pytest.raises(ContractValidationError, match="policy_digest"):
        verify_completeness_proof(
            proof, policy=bad_policy, inventory=inventory, seed=seed
        )

    bad_seed = copy.deepcopy(seed)
    bad_seed["capabilities"][0]["promise"] = "changed promise"
    with pytest.raises(ContractValidationError, match="seed_digest"):
        verify_completeness_proof(
            proof, policy=policy, inventory=inventory, seed=bad_seed
        )

    bad_inv = copy.deepcopy(inventory)
    bad_inv["capabilities"][0]["upstream"]["source_paths"] = ["commands/other.md"]
    with pytest.raises(ContractValidationError, match="coverage_digest"):
        verify_completeness_proof(
            proof, policy=policy, inventory=bad_inv, seed=seed
        )

    assert digest_seed_catalog(seed) == proof["seed_digest"]


def test_added_removed_or_changed_surface_breaks_reproduction(tmp_path: Path) -> None:
    upstream, pin, inventory, seed = _omc_world(tmp_path)
    policy = _policy()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=upstream,
        seed=seed,
        surface_mappings=_mappings(),
    )

    # Mutate bytes at the same HEAD without committing → dirty fails closed
    # (cannot present B's bytes under A's pin).
    (upstream / "commands" / "hello.md").write_text("# hello MUTATED\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="dirty"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=upstream,
        )
    assert _git(upstream, "checkout", "--", "commands/hello.md").returncode == 0

    # New commit with added surface → HEAD != pin.
    added = tmp_path / "added"
    _write_source_tree(
        added,
        surfaces=[
            {"id": "cmd.hello", "path": "commands/hello.md"},
            {"id": "cmd.extra", "path": "commands/extra.md"},
        ],
    )
    _commit_tree(added)
    with pytest.raises(ContractValidationError, match="does not match pin_revision"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=added,
        )

    removed = tmp_path / "removed"
    _write_source_tree(removed, surfaces=[])
    _commit_tree(removed)
    with pytest.raises(ContractValidationError, match="does not match pin_revision"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=removed,
        )

    # Commit a content change as a new revision, then force inventory+proof pin
    # to that new HEAD and show surface digests differ from the original proof
    # when verifying the old proof against... already covered by wrong HEAD.
    # Additionally: rebuild proof at pin then change tree via orphan commit at
    # same tree? Skip — dirty + wrong-head cover the attack.
    assert pin == proof["pin_revision"]


def test_unmapped_discovered_surface_fails_closed(tmp_path: Path) -> None:
    upstream, _pin, inventory, seed = _omc_world(tmp_path)
    policy = _policy()
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=upstream,
        seed=seed,
        surface_mappings={},
    )
    assert proof["unresolved_surfaces"] == ["cmd.hello"]
    with pytest.raises(ContractValidationError, match="unresolved surfaces"):
        verify_completeness_proof(
            proof,
            policy=policy,
            inventory=inventory,
            seed=seed,
            upstream_root=upstream,
            require_no_unresolved=True,
        )


def test_surface_mapping_rejects_alias_only_or_cross_source_target(
    tmp_path: Path,
) -> None:
    upstream, pin, _inv, seed = _omc_world(tmp_path)
    caps = [
        _capability(
            cap_id="fixture.omc.hello",
            source="OMC",
            category="runtime_orchestration",
            pin=pin,
        ),
        _capability(
            cap_id="fixture.omc.hello.alias",
            source="OMC",
            category="runtime_orchestration",
            classification="alias",
            alias_of="fixture.omc.hello",
            pin=pin,
        ),
        _capability(
            cap_id="fixture.omx.other",
            source="OMX",
            category="runtime_orchestration",
            paths=["commands/other.md"],
            pin=pin,
        ),
    ]
    inventory = _mini_inventory(pin=pin, capabilities=caps, gaps=[])
    policy = _policy()

    with pytest.raises(ContractValidationError, match="alias-only"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=upstream,
            seed=seed,
            surface_mappings={"cmd.hello": ["fixture.omc.hello.alias"]},
        )

    with pytest.raises(ContractValidationError, match="cross-source"):
        build_completeness_proof(
            policy=policy,
            inventory=inventory,
            upstream_root=upstream,
            seed=seed,
            surface_mappings={"cmd.hello": ["fixture.omx.other"]},
        )


def test_category_promotion_requires_every_source_partition(tmp_path: Path) -> None:
    policies: dict[str, dict] = {}
    proofs: dict[str, dict] = {}
    roots: dict[str, Path] = {}
    seeds: dict[str, dict] = {}
    caps: list[dict] = []
    pins: dict[str, str] = {}

    for source in SOURCE_STATUS_IDS:
        repo = f"https://example.invalid/{source}"
        up = tmp_path / source
        surface_id = f"cmd.{source.lower()}"
        _write_source_tree(
            up,
            surfaces=[{"id": surface_id, "path": f"commands/{source}.md"}],
        )
        pin = _commit_tree(up)
        pins[source] = pin
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
                pin=pin,
            )
        )
        seeds[source] = {
            "source": source,
            "pin_revision": pin,
            "capabilities": [
                {
                    "id": cap_id,
                    "source_paths": [f"commands/{source}.md"],
                    "promise": f"promise {source}",
                }
            ],
        }

    inventory = _mini_inventory(
        pin=pins["OMC"],
        capabilities=caps,
        gaps=[],
        pins={
            sid: {
                "repository": f"https://example.invalid/{sid}",
                "revision": pins[sid],
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
    pin = _commit_tree(up)
    policy = _policy_for("OMC", FIXTURE_REPO, category="runtime_orchestration")
    inventory = _mini_inventory(pin=pin, gaps=[])
    seed = _seed(pin=pin)
    proof = build_completeness_proof(
        policy=policy,
        inventory=inventory,
        upstream_root=up,
        seed=seed,
        surface_mappings={"cmd.hello": ["fixture.omc.hello"]},
    )
    assert "skills" in proof["empty_category_partitions"]
    assert "runtime_orchestration" not in proof["empty_category_partitions"]

    reproduced = reproduce_source_index(policy, up, pin_revision=pin)
    assert "skills" in reproduced["empty_category_partitions"]
    verify_completeness_proof(
        proof,
        policy=policy,
        inventory=inventory,
        seed=seed,
        upstream_root=up,
    )


def test_source_paths_are_confined_and_symlinks_rejected(tmp_path: Path) -> None:
    policy = validate_completeness_policy(_policy_for("OMC", FIXTURE_REPO))

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
    escape_pin = _commit_tree(escape)
    with pytest.raises(ContractValidationError, match="relative POSIX|escapes"):
        reproduce_source_index(policy, escape, pin_revision=escape_pin)

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
    # Prefer committing without following the symlink as blob content: git may
    # store the symlink; reproduce must still reject it as a surface path.
    link_pin = _commit_tree(link_root)
    with pytest.raises(ContractValidationError, match="symlink"):
        reproduce_source_index(policy, link_root, pin_revision=link_pin)


def test_plan_mode_never_mutates_inventory_or_proof(tmp_path: Path) -> None:
    upstream, _pin, inventory, seed = _omc_world(tmp_path)
    inventory_path = tmp_path / "inv.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    before = inventory_path.read_bytes()
    proof_path = tmp_path / "proof.json"
    proof_path.write_text("{}\n", encoding="utf-8")
    proof_before = proof_path.read_bytes()
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")

    plan = plan_completeness_proof(
        policy=_policy(),
        inventory=inventory,
        upstream_root=upstream,
        seed=seed,
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
            str(upstream),
            "--seed",
            str(seed_path),
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
