"""Release claim gate: overclaim, live evidence freshness, upstream drift (#78-C Task 3)."""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import (
    FROZEN_PINS,
    HOST_BASELINE_GENERATED_RELATIVE,
    HOST_BASELINE_PIN_ID,
    PARITY_CATEGORY_TAXONOMY,
    PARITY_MATURITY_LEVELS,
    PARITY_V2_CLASSIFICATIONS,
    load_json_object,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_claim_gate import check_parity_release_claims, load_host_baseline_snapshot
from omg_cli.parity_refresh import (
    build_host_baseline_refresh_plan,
    build_refresh_plan,
    generated_docs_content_hash,
    host_snapshot_content_hash,
    write_committed_host_baseline_review,
)

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "parity" / "omg-parity.json"
CATALOG_V1 = ROOT / "tests" / "fixtures" / "parity" / "upstream_catalog_v1.json"
OVERCLAIM_README = ROOT / "tests" / "fixtures" / "parity" / "claims" / "readme_overclaim.md"
HONEST_README = ROOT / "tests" / "fixtures" / "parity" / "claims" / "readme_honest.md"
OMC_PIN = "67dddfc05ff29900d8251dcec0ed9dee3c947ffa"
NEW_PIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FIXED_NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


def _fresh_iso(*, days_ago: float = 0.0, now: datetime = FIXED_NOW) -> str:
    moment = now - timedelta(days=days_ago)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _minimal_inventory() -> dict:
    full = load_json_object(INVENTORY)
    omc_ids = {
        "team.plane_v3",
        "parity.inventory.governance",
        "omc.cli.session_surfaces",
    }
    inv = copy.deepcopy(full)
    inv["capabilities"] = [
        row
        for row in inv["capabilities"]
        if row.get("upstream", {}).get("source") == "OMC"
        and row["id"] in omc_ids
    ]
    cap_ids = {row["id"] for row in inv["capabilities"]}
    inv["gaps"] = [
        gap
        for gap in inv.get("gaps", [])
        if isinstance(gap, dict)
        and all(
            cid in cap_ids
            for cid in gap.get("capability_ids", [])
            if isinstance(cid, str)
        )
    ]
    return inv


def _bootstrapping_inventory(tmp_path: Path) -> dict:
    """Minimal honest bootstrapping inventory for pass-case tests."""
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    return {
        "store_kind": "parity_inventory",
        "schema_version": 2,
        "repository_id": "OMG",
        "ownership_manifest_id": "dual-parity-writers-v1",
        "inventory_status": "bootstrapping",
        "maturity_levels": list(PARITY_MATURITY_LEVELS),
        "classifications": list(PARITY_V2_CLASSIFICATIONS),
        "upstream_pins": {
            "OMC": {
                "repository": "https://github.com/example/omc",
                "revision": OMC_PIN,
                "kind": "commit",
            },
            "OMX": {
                "repository": "https://github.com/example/omx",
                "revision": "435d4a9cc982ffaf83fabbfbb8711ae6c178ffca",
                "kind": "commit",
            },
            "OmO": {
                "repository": "https://github.com/example/omo",
                "revision": "4ca872b57e45281a9a81190bb73637729288ffc3",
                "kind": "commit",
            },
            "Antigravity": {
                "repository": "https://github.com/example/ag",
                "revision": "bfab12dac5bd090015a89cf82e65093d13b567d9",
                "kind": "commit",
            },
            "GROK_BUILD": {
                "repository": "https://github.com/example/grok-build",
                "revision": FROZEN_PINS[HOST_BASELINE_PIN_ID],
                "kind": "commit",
            },
        },
        "category_status": {cat: "bootstrapping" for cat in sorted(PARITY_CATEGORY_TAXONOMY)},
        "source_status": {
            "OMC": "bootstrapping",
            "OMX": "bootstrapping",
            "OmO": "bootstrapping",
            "Antigravity": "bootstrapping",
        },
        "live_evidence_max_age_days": 30,
        "capabilities": [
            {
                "id": "team.plane_v3",
                "category": "team",
                "promise": "Team plane v3",
                "classification": "omg_native",
                "upstream": {
                    "source": "OMC",
                    "revision": OMC_PIN,
                    "source_paths": ["README.md"],
                },
                "omg_paths": ["omg_cli/team/__init__.py"],
                "runtime_owner": "omg",
                "maturity": {"grok": "catalogued"},
                "evidence": {"tests": [], "docs": [], "live": []},
                "issues": ["#69"],
                "gap": "Not yet implemented.",
            },
            {
                "id": "parity.inventory.governance",
                "category": "parity_governance",
                "promise": "Parity inventory governance",
                "classification": "omg_native",
                "upstream": {
                    "source": "OMC",
                    "revision": OMC_PIN,
                    "source_paths": ["README.md"],
                },
                "omg_paths": ["omg_cli/parity_check.py"],
                "runtime_owner": "omg",
                "maturity": {"grok": "catalogued"},
                "evidence": {"tests": [], "docs": [], "live": []},
                "issues": ["#78"],
                "gap": "Release gate in progress.",
            },
        ],
        "gaps": [
            {
                "id": "gap.team.v3",
                "priority": "P0",
                "status": "open",
                "issues": ["#69"],
                "capability_ids": ["team.plane_v3"],
                "summary": "Team v3 missing",
            }
        ],
    }


def _write_inventory(tmp_path: Path, inventory: dict) -> Path:
    path = tmp_path / "docs" / "parity" / "omg-parity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    # Release gate always requires host-baseline snapshot alongside inventory.
    host_path = tmp_path / "docs" / "parity" / "upstream-snapshots" / "grok-build.json"
    if not host_path.is_file():
        _write_host_baseline_snapshot(tmp_path, inventory)
    return path


def _write_catalog(tmp_path: Path, catalog: dict) -> Path:
    path = tmp_path / "upstream_catalog.json"
    path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    return path


def _load_catalog(*, pin_revision: str = NEW_PIN) -> dict:
    catalog = load_json_object(CATALOG_V1)
    catalog["pin_revision"] = pin_revision
    return catalog


def _scaffold_inventory_paths(tmp_path: Path, inventory: dict) -> None:
    """Create stub files for omg_paths and evidence paths under tmp_path."""
    paths: set[str] = set()
    for row in inventory.get("capabilities", []):
        if not isinstance(row, dict):
            continue
        for rel in row.get("omg_paths", []):
            if isinstance(rel, str):
                paths.add(rel)
        evidence = row.get("evidence", {})
        if isinstance(evidence, dict):
            for key, values in evidence.items():
                if key == "live":
                    continue
                if isinstance(values, list):
                    for rel in values:
                        if isinstance(rel, str):
                            paths.add(rel)
    for rel in paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("", encoding="utf-8")


def _live_verified_row(row: dict, *, days_ago: float = 1.0) -> None:
    row["maturity"] = {"grok": "live_verified"}
    row["evidence"] = {
        "tests": ["tests/test_parity_claim_gate.py"],
        "docs": ["docs/parity/README.md"],
        "configured_paths": ["omg_cli/parity_check.py"],
        "install_evidence": ["plugin.json"],
        "enabled_evidence": ["hooks/hooks.json"],
        "loadable_evidence": ["omg_cli/__init__.py"],
        "observed_evidence": ["docs/parity/omg-parity.json"],
        "healthy_evidence": ["tests/test_parity_claim_gate.py"],
        "live": [
            {
                "runtime": "grok",
                "platform": "darwin-arm64",
                "version": "0.2.107",
                "observed_at": _fresh_iso(days_ago=days_ago),
                "marker": "LIVE_OK",
            }
        ],
    }


def _honest_docs(tmp_path: Path) -> None:
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(HONEST_README.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\nHonest release notes without live overclaims.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "parity" / "SUMMARY.md").write_text(
        "# Parity summary\n\nInventory status: **bootstrapping**.\n\n"
        "Capabilities catalogued only; no percentage claimed.\n",
        encoding="utf-8",
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "parity@example.invalid")
    _git(root, "config", "user.name", "Parity Fixture")
    _git(root, "config", "commit.gpgsign", "false")


def _git_commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _bump_omc_pin(inventory: dict, new_pin: str) -> None:
    inventory["upstream_pins"]["OMC"]["revision"] = new_pin
    for row in inventory["capabilities"]:
        if row.get("upstream", {}).get("source") == "OMC":
            row["upstream"]["revision"] = new_pin


REQUIRED_SNAPSHOT_SOURCES = ("OMC", "OMX", "OmO", "Antigravity")


def _write_required_snapshots(
    tmp_path: Path,
    inventory: dict,
    *,
    override: dict[str, dict] | None = None,
) -> None:
    """Seed all required upstream-snapshots/{Source}.json files from inventory."""
    snap_dir = tmp_path / "docs" / "parity" / "upstream-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    pins = inventory["upstream_pins"]
    override = override or {}
    for source in REQUIRED_SNAPSHOT_SOURCES:
        if source in override:
            catalog = copy.deepcopy(override[source])
        else:
            caps = []
            for row in inventory.get("capabilities", []):
                if not isinstance(row, dict):
                    continue
                upstream = row.get("upstream")
                if not isinstance(upstream, dict) or upstream.get("source") != source:
                    continue
                caps.append(
                    {
                        "id": row["id"],
                        "source_paths": list(upstream.get("source_paths", [])),
                        "promise": row.get("promise", ""),
                    }
                )
            catalog = {
                "source": source,
                "pin_revision": pins[source]["revision"],
                "capabilities": caps,
            }
        (snap_dir / f"{source}.json").write_text(
            json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
        )
    _write_host_baseline_snapshot(tmp_path, inventory)


def _minimal_host_capability(pin: str) -> dict:
    return {
        "id": "grok.host.baseline.probe",
        "category": "reliability",
        "classification": "irrelevant",
        "owner": "host",
        "runtime": "grok",
        "status": "catalogued",
        "maturity": "catalogued",
        "promise": "Minimal host baseline probe capability for hermetic tests",
        "evidence": {
            "source_commit": pin,
            "source_paths": ["CHANGELOG.md"],
            "notes": "Test fixture capability.",
        },
        "downstream_issues": [],
    }


_HOST_REVIEW_SENTINEL_FROM = "0000000000000000000000000000000000000001"


def _write_binding_host_review(tmp_path: Path) -> Path | None:
    """Mint a content-bound GROK_BUILD receipt for the fixture snapshot."""
    try:
        snapshot = load_host_baseline_snapshot(tmp_path)
    except (OSError, ContractValidationError):
        return None
    pin = snapshot["public_commit"]
    previous = _HOST_REVIEW_SENTINEL_FROM
    if previous == pin:
        previous = "0000000000000000000000000000000000000002"
    try:
        docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
        plan = build_host_baseline_refresh_plan(
            from_revision=previous,
            to_revision=pin,
            host_snapshot=snapshot,
            previous_snapshot=None,
            generated_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
            snapshot_hash=host_snapshot_content_hash(snapshot),
            generated_docs_hash=docs_hash,
        )
        return write_committed_host_baseline_review(tmp_path, plan)
    except (OSError, ContractValidationError):
        return None


def _write_host_baseline_snapshot(
    tmp_path: Path,
    inventory: dict,
    *,
    snapshot_override: dict | None = None,
    write_generated_docs: bool = True,
    write_binding_review: bool = True,
) -> Path:
    pin = inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"]
    snap_dir = tmp_path / "docs" / "parity" / "upstream-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    if snapshot_override is not None:
        snapshot = copy.deepcopy(snapshot_override)
    else:
        snapshot = {
            "store_kind": "host_baseline_snapshot",
            "schema_version": 1,
            "host_id": HOST_BASELINE_PIN_ID,
            "repository": inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["repository"],
            "public_commit": pin,
            "source_revision": "4d6d11372ab8f73026a78c45a7b7e7b1310eb39f",
            "release": "0.2.121",
            "observed_version": "0.2.121",
            "platform": "test-fixture",
            "capabilities": [_minimal_host_capability(pin)],
            "review": {
                "status": "catalogued",
                "reviewed_pin": pin,
                "notes": "Hermetic test host baseline.",
            },
            "generated": {
                "docs": list(HOST_BASELINE_GENERATED_RELATIVE),
            },
            "issues": ["#105"],
            "maturity_floor": "catalogued",
        }
    path = snap_dir / "grok-build.json"
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    if write_generated_docs:
        for relative in HOST_BASELINE_GENERATED_RELATIVE:
            doc = tmp_path / relative
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text(
                f"<!-- GENERATED test fixture for {relative} pin={pin} -->\n",
                encoding="utf-8",
            )
        if write_binding_review:
            _write_binding_host_review(tmp_path)
    return path


def _bump_grok_build_pin(inventory: dict, new_pin: str) -> None:
    inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = new_pin


def _ack_review(
    plan: dict,
    *,
    indices: list[int] | None = None,
    use_acknowledgments_key: bool = False,
    source: str | None = None,
    from_revision: str | None = None,
    to_revision: str | None = None,
    mutate_detail: bool = False,
) -> dict:
    """Build review artifact with acknowledged dispositions for plan changes."""
    changes = plan["changes"]
    if indices is None:
        indices = list(range(len(changes)))
    acked = []
    for i in indices:
        entry = {**changes[i], "disposition": "acknowledged"}
        if mutate_detail:
            entry["detail"] = {"fields": ["tampered"]}
        acked.append(entry)
    payload: dict = {
        "store_kind": "parity_refresh_review",
        "schema_version": 1,
        "source": source if source is not None else plan["source"],
        "from_revision": (
            from_revision if from_revision is not None else plan["from_revision"]
        ),
        "to_revision": to_revision if to_revision is not None else plan["to_revision"],
        "generated_at": plan.get("generated_at", "2026-08-05T12:00:00Z"),
    }
    if use_acknowledgments_key:
        payload["acknowledgments"] = acked
    else:
        payload["changes"] = acked
    return payload


def test_release_gate_rejects_expired_live_evidence(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    _live_verified_row(inventory["capabilities"][0], days_ago=90)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)

    with pytest.raises(ContractValidationError, match="fresh"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_readme_overclaim(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(
        OVERCLAIM_README.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "docs" / "parity" / "SUMMARY.md").write_text(
        "Bootstrapping inventory — **parity 95%** complete.\n", encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_unresolved_upstream_add(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    cat_path = _write_catalog(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_unresolved_upstream_delete(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"] = [
        c for c in catalog["capabilities"] if c["id"] != "omc.cli.session_surfaces"
    ]
    cat_path = _write_catalog(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_unresolved_upstream_rename(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "omc.cli.session_surfaces":
            cap["id"] = "omc.cli.session_surfaces_v2"
            break
    cat_path = _write_catalog(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_unresolved_upstream_changed(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Updated promise text"
            cap["source_paths"] = ["README.md", "skills/team/SKILL.md"]
            break
    cat_path = _write_catalog(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            now=FIXED_NOW,
        )


def test_release_gate_passes_honest_bootstrapping_inventory(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        now=FIXED_NOW,
    )

    assert payload["ok"] is True
    assert payload["inventory_status"] == "bootstrapping"
    assert payload["overclaims"] == 0
    assert payload["upstream_drift_checked"] is True
    assert payload["upstream_drift_resolved"] is True


def test_release_gate_rejects_missing_required_upstream_snapshot(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json").unlink()

    with pytest.raises(ContractValidationError, match="missing required upstream snapshot"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_missing_upstream_snapshots_directory(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)

    with pytest.raises(ContractValidationError, match="upstream-snapshots"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_parity_readme_overclaim(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "docs" / "parity" / "README.md").write_text(
        "# Parity\n\nWe claim **complete parity** and **full 1:1** coverage.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_snapshot_pin_mismatch(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    bad = {
        "source": "OMC",
        "pin_revision": NEW_PIN,
        "capabilities": [
            {
                "id": "team.plane_v3",
                "source_paths": ["README.md"],
                "promise": "Team plane v3",
            },
            {
                "id": "parity.inventory.governance",
                "source_paths": ["README.md"],
                "promise": "Parity inventory governance",
            },
        ],
    }
    _write_required_snapshots(tmp_path, inventory, override={"OMC": bad})

    with pytest.raises(ContractValidationError, match="pin_revision"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_missing_healthy_evidence_path(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    _live_verified_row(inventory["capabilities"][0], days_ago=1.0)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    healthy = tmp_path / "tests/test_parity_claim_gate.py"
    if healthy.is_file():
        healthy.unlink()
    _honest_docs(tmp_path)

    with pytest.raises(ContractValidationError, match="healthy_evidence"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_case_insensitive_full_one_to_one(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(
        "# oh-my-grok\n\nWe target FULL 1:1 coverage.\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "parity" / "SUMMARY.md").write_text(
        "Bootstrapping inventory.\n", encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_passes_when_acknowledged(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(_ack_review(plan), indent=2), encoding="utf-8")

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        upstream_catalog_path=cat_path,
        review_artifact_path=review_path,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["upstream_drift_checked"] is True
    assert payload["upstream_drift_resolved"] is True


def test_upstream_drift_passes_when_rename_acknowledged(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "omc.cli.session_surfaces":
            cap["id"] = "omc.cli.session_surfaces_v2"
            break
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(_ack_review(plan), indent=2), encoding="utf-8")

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        upstream_catalog_path=cat_path,
        review_artifact_path=review_path,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["upstream_drift_checked"] is True
    assert payload["upstream_drift_resolved"] is True


def test_upstream_drift_passes_when_acknowledgments_key_used(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_ack_review(plan, use_acknowledgments_key=True), indent=2),
        encoding="utf-8",
    )

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        upstream_catalog_path=cat_path,
        review_artifact_path=review_path,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["upstream_drift_resolved"] is True


def test_upstream_drift_rejects_ack_with_wrong_source(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_ack_review(plan, source="OMX"), indent=2), encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_rejects_ack_with_wrong_revision(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_ack_review(plan, to_revision=NEW_PIN), indent=2), encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_rejects_ack_with_tampered_detail(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    catalog = _load_catalog(pin_revision=OMC_PIN)
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Updated promise text"
            break
    cat_path = _write_catalog(tmp_path, catalog)
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_ack_review(plan, mutate_detail=True), indent=2), encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def test_doc_scan_covers_authoritative_parity_paths() -> None:
    from omg_cli.parity_claim_gate import _DOC_SCAN_RELATIVE

    required = {
        "README.md",
        "CHANGELOG.md",
        "docs/skills.md",
        "docs/parity/README.md",
        "docs/parity/schema-v2.md",
        "docs/parity/FEATURE-MATRIX.md",
        "docs/parity/GAPS.md",
        "docs/parity/MATRIX-OMC.md",
        "docs/parity/MATRIX-OMX.md",
        "docs/parity/MATRIX-OmO.md",
        "docs/parity/MATRIX-Antigravity.md",
        "docs/parity/SUMMARY.md",
        "docs/parity/SUMMARY.zh.md",
        "docs/parity/SUMMARY.zh-TW.md",
    }
    assert required.issubset(set(_DOC_SCAN_RELATIVE))


def test_release_gate_rejects_changelog_overclaim(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Achieved **complete parity** with upstream.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_skills_md_overclaim(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "skills.md").write_text(
        "# Skills\n\nOMG now offers **full 1:1** parity skill coverage.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def _mark_healthy(row: dict) -> None:
    row["maturity"] = {"grok": "healthy"}
    row["evidence"] = {
        "tests": ["tests/test_parity_claim_gate.py"],
        "docs": ["docs/parity/README.md"],
        "configured_paths": ["omg_cli/parity_check.py"],
        "install_evidence": ["plugin.json"],
        "enabled_evidence": ["hooks/hooks.json"],
        "loadable_evidence": ["omg_cli/__init__.py"],
        "observed_evidence": ["docs/parity/omg-parity.json"],
        "healthy_evidence": ["tests/test_parity_claim_gate.py"],
        "live": [],
    }


def test_release_gate_keeps_forbidden_scan_when_category_or_source_bootstrapping(
    tmp_path: Path,
) -> None:
    """P1-2: inventory_status=complete + healthy caps must not disable global scan
    while category_status / source_status remain bootstrapping."""
    inventory = _bootstrapping_inventory(tmp_path)
    inventory["inventory_status"] = "complete"
    for row in inventory["capabilities"]:
        _mark_healthy(row)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "README.md").write_text(
        "# oh-my-grok\n\nWe claim **complete parity** already.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match="overclaim"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_mislabeled_upstream_snapshot_source(tmp_path: Path) -> None:
    """P2-1: OMC.json must declare source==OMC; mislabeling must fail closed."""
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    pins = inventory["upstream_pins"]
    # Valid OMX catalogue written into OMC.json — would skip OMC coverage if
    # the gate only checked filename presence.
    mislabeled = {
        "source": "OMX",
        "pin_revision": pins["OMX"]["revision"],
        "capabilities": [],
    }
    _write_required_snapshots(tmp_path, inventory, override={"OMC": mislabeled})

    with pytest.raises(ContractValidationError, match=r"source.*OMC|expected.*OMC"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_rejects_stale_ack_after_promise_mutates(tmp_path: Path) -> None:
    """P2-2: ack for promise A→B must not clear drift when promise becomes C."""
    inventory = _minimal_inventory()
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)

    catalog_b = _load_catalog(pin_revision=OMC_PIN)
    catalog_b = copy.deepcopy(catalog_b)
    for cap in catalog_b["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Promise revision B"
            break
    plan_b = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog_b,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    stale_ack = _ack_review(plan_b)

    catalog_c = copy.deepcopy(catalog_b)
    for cap in catalog_c["capabilities"]:
        if cap["id"] == "team.plane_v3":
            cap["promise"] = "Promise revision C"
            break
    cat_path = _write_catalog(tmp_path, catalog_c)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(stale_ack, indent=2), encoding="utf-8")

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def test_upstream_snapshots_match_inventory_pins() -> None:
    """Each required upstream snapshot pin equals upstream_pins[source].revision."""
    from omg_cli.parity_claim_gate import REQUIRED_UPSTREAM_SNAPSHOT_SOURCES

    inventory = load_json_object(INVENTORY)
    pins = inventory["upstream_pins"]
    snap_dir = ROOT / "docs" / "parity" / "upstream-snapshots"
    assert snap_dir.is_dir(), "upstream-snapshots directory missing"
    seen: set[str] = set()
    for source in REQUIRED_UPSTREAM_SNAPSHOT_SOURCES:
        path = snap_dir / f"{source}.json"
        assert path.is_file(), f"missing required snapshot {path.name}"
        snapshot = load_json_object(path)
        assert snapshot["source"] == source
        assert source in pins, f"{path.name}: unknown source {source!r}"
        assert snapshot["pin_revision"] == pins[source]["revision"], (
            f"{path.name}: pin_revision {snapshot['pin_revision']!r} "
            f"!= upstream_pins[{source!r}].revision {pins[source]['revision']!r}"
        )
        seen.add(source)
    assert seen == set(REQUIRED_UPSTREAM_SNAPSHOT_SOURCES)


def test_readme_documents_catalog_update_before_refresh_plan() -> None:
    """New-pin flow: update snapshot pin_revision first, then run --plan."""
    readme = (ROOT / "docs" / "parity" / "README.md").read_text(encoding="utf-8")
    assert "update the snapshot catalogue **first**" in readme
    assert "pin must match the catalogue pin_revision" in readme
    first = readme.index("update the snapshot catalogue **first**")
    plan = readme.index("omg parity refresh")
    assert first < plan


def test_release_gate_rejects_live_proven_phrase(tmp_path: Path) -> None:
    """P2: live-proven is a forbidden maturity synonym while bootstrapping."""
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    (tmp_path / "docs" / "parity").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(
        "Honest bootstrapping inventory.\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "Live-proven: grok inspect loads the contract.\n", encoding="utf-8"
    )

    with pytest.raises(ContractValidationError, match=r"overclaim|live.?proven"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_duplicate_upstream_capability_ids(tmp_path: Path) -> None:
    """P2: duplicate capability ids in a snapshot must fail closed (no LWW)."""
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    pins = inventory["upstream_pins"]
    dup = {
        "source": "OMC",
        "pin_revision": pins["OMC"]["revision"],
        "capabilities": [
            {
                "id": "team.plane_v3",
                "source_paths": ["README.md"],
                "promise": "new promise",
            },
            {
                "id": "team.plane_v3",
                "source_paths": ["README.md"],
                "promise": "Team plane v3",
            },
        ],
    }
    _write_required_snapshots(tmp_path, inventory, override={"OMC": dup})

    with pytest.raises(ContractValidationError, match="duplicate upstream capability"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_release_gate_rejects_malformed_upstream_capability_row(tmp_path: Path) -> None:
    """P2: missing promise / non-object rows must fail closed."""
    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    pins = inventory["upstream_pins"]
    bad = {
        "source": "OMC",
        "pin_revision": pins["OMC"]["revision"],
        "capabilities": [
            {"id": "team.plane_v3", "source_paths": ["README.md"]},
        ],
    }
    _write_required_snapshots(tmp_path, inventory, override={"OMC": bad})

    with pytest.raises(ContractValidationError, match=r"key mismatch|promise"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_pin_transition_requires_committed_review_even_when_zero_drift(
    tmp_path: Path,
) -> None:
    """P1: synced pin bump (inventory==snapshot) still needs docs/parity/reviews."""
    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "cccccccccccccccccccccccccccccccccccccccc"
    inventory["upstream_pins"]["OMC"]["revision"] = new_pin
    for row in inventory["capabilities"]:
        if row.get("upstream", {}).get("source") == "OMC":
            row["upstream"]["revision"] = new_pin
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)

    with pytest.raises(
        ContractValidationError, match="pin transition missing committed refresh review"
    ):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            base_inventory=base,
            now=FIXED_NOW,
        )


def test_pin_transition_passes_with_committed_review(tmp_path: Path) -> None:
    """P1: matching docs/parity/reviews ledger clears pin-transition gate."""
    from omg_cli.parity_refresh import write_committed_refresh_review

    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "dddddddddddddddddddddddddddddddddddddddd"
    _bump_omc_pin(inventory, new_pin)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)

    catalog = load_json_object(
        tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json"
    )
    plan = build_refresh_plan(
        inventory=base,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=new_pin,
        generated_at=FIXED_NOW,
    )
    write_committed_refresh_review(tmp_path, plan)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "commit review ledger")

    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        base_inventory=base,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["pin_transitions_reviewed"] is True


def test_pin_transition_rejects_untracked_review_ledger(tmp_path: Path) -> None:
    """P2: worktree-only review file is not 'committed'."""
    from omg_cli.parity_refresh import write_committed_refresh_review

    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    _bump_omc_pin(inventory, new_pin)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "base without review")

    catalog = load_json_object(
        tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json"
    )
    plan = build_refresh_plan(
        inventory=base,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=new_pin,
        generated_at=FIXED_NOW,
    )
    write_committed_refresh_review(tmp_path, plan)

    with pytest.raises(ContractValidationError, match=r"not tracked by git"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            base_inventory=base,
            now=FIXED_NOW,
        )


def test_pin_transition_rejects_tampered_review_worktree(tmp_path: Path) -> None:
    """P2: HEAD blob must match worktree bytes for the review ledger."""
    from omg_cli.parity_refresh import write_committed_refresh_review

    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "ffffffffffffffffffffffffffffffffffffffff"
    _bump_omc_pin(inventory, new_pin)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)

    catalog = load_json_object(
        tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json"
    )
    plan = build_refresh_plan(
        inventory=base,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=new_pin,
        generated_at=FIXED_NOW,
    )
    path = write_committed_refresh_review(tmp_path, plan)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "commit review")
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ContractValidationError, match=r"differs from HEAD blob"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            base_inventory=base,
            now=FIXED_NOW,
        )


def test_durable_base_catches_pin_transition_masked_by_head_caret(
    tmp_path: Path,
) -> None:
    """P1: C0 pin→C1 bump (no ledger)→C2 noise; HEAD^ masks, previous tag catches."""
    from omg_cli.parity_claim_gate import (
        assert_pin_transitions_reviewed,
        resolve_base_inventory,
    )

    inventory = _bootstrapping_inventory(tmp_path)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "C0 old pin")
    _git(tmp_path, "tag", "v0.0.1")

    bumped = copy.deepcopy(inventory)
    new_pin = "1111111111111111111111111111111111111111"
    _bump_omc_pin(bumped, new_pin)
    _write_inventory(tmp_path, bumped)
    _write_required_snapshots(tmp_path, bumped)
    _git_commit_all(tmp_path, "C1 pin bump without review")

    (tmp_path / "README.md").write_text(
        (tmp_path / "README.md").read_text(encoding="utf-8") + "\nnoise\n",
        encoding="utf-8",
    )
    _git_commit_all(tmp_path, "C2 unrelated")

    catalogs = {
        source: load_json_object(
            tmp_path / "docs" / "parity" / "upstream-snapshots" / f"{source}.json"
        )
        for source in REQUIRED_SNAPSHOT_SOURCES
    }
    candidate = load_json_object(tmp_path / "docs" / "parity" / "omg-parity.json")

    head_parent = resolve_base_inventory(tmp_path, base_ref="HEAD^", require=False)
    assert head_parent is not None
    assert (
        head_parent.inventory["upstream_pins"]["OMC"]["revision"]
        == candidate["upstream_pins"]["OMC"]["revision"]
    )

    durable = resolve_base_inventory(tmp_path, require=True)
    assert durable is not None
    assert durable.git_ref == "v0.0.1"

    with pytest.raises(
        ContractValidationError, match="pin transition missing committed refresh review"
    ):
        assert_pin_transitions_reviewed(
            inventory=candidate,
            base_inventory=durable.inventory,
            repo_root=tmp_path,
            catalogs_by_source=catalogs,
            base_ref=durable.git_ref,
        )


def test_resolve_base_inventory_require_skips_head_caret(tmp_path: Path) -> None:
    """Release require=True must not prefer HEAD^ over durable tags/main."""
    from omg_cli.parity_claim_gate import resolve_base_inventory

    inventory = _bootstrapping_inventory(tmp_path)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "C0")
    _git(tmp_path, "tag", "v9.9.9")
    (tmp_path / "README.md").write_text("second\n", encoding="utf-8")
    _git_commit_all(tmp_path, "C1")

    resolved = resolve_base_inventory(tmp_path, require=True)
    assert resolved is not None
    assert resolved.git_ref == "v9.9.9"
    assert resolved.git_ref != "HEAD^"


def test_pin_transition_catches_bump_revert_hidden_by_merge(tmp_path: Path) -> None:
    """P1: side-branch A→B→A merged back must still require intermediate ledgers."""
    from omg_cli.parity_claim_gate import assert_pin_transitions_reviewed

    pin_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    pin_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    inventory = _bootstrapping_inventory(tmp_path)
    _bump_omc_pin(inventory, pin_a)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    # Default branch name varies; create an explicit main.
    _git(tmp_path, "checkout", "-b", "main")
    base_sha = _git_commit_all(tmp_path, "base pin A")

    _git(tmp_path, "checkout", "-b", "feature")
    bumped = copy.deepcopy(inventory)
    _bump_omc_pin(bumped, pin_b)
    _write_inventory(tmp_path, bumped)
    _write_required_snapshots(tmp_path, bumped)
    _git_commit_all(tmp_path, "feature bump A→B without review")

    reverted = copy.deepcopy(inventory)
    _bump_omc_pin(reverted, pin_a)
    _write_inventory(tmp_path, reverted)
    _write_required_snapshots(tmp_path, reverted)
    _git_commit_all(tmp_path, "feature revert B→A without review")

    _git(tmp_path, "checkout", "main")
    _git(tmp_path, "merge", "--no-ff", "-m", "merge feature (final still A)", "feature")

    # Path-simplified git log hides the bump/revert; DAG walk must not.
    simplified = subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "log",
            "--reverse",
            "--format=%H",
            f"{base_sha}..HEAD",
            "--",
            "docs/parity/omg-parity.json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert simplified.stdout.strip() == ""

    catalogs = {
        source: load_json_object(
            tmp_path / "docs" / "parity" / "upstream-snapshots" / f"{source}.json"
        )
        for source in REQUIRED_SNAPSHOT_SOURCES
    }
    candidate = load_json_object(tmp_path / "docs" / "parity" / "omg-parity.json")
    assert candidate["upstream_pins"]["OMC"]["revision"] == pin_a

    with pytest.raises(
        ContractValidationError, match="pin transition missing committed refresh review"
    ):
        assert_pin_transitions_reviewed(
            inventory=candidate,
            base_inventory=inventory,
            repo_root=tmp_path,
            catalogs_by_source=catalogs,
            base_ref=base_sha,
        )


def test_pin_transition_dag_walk_hard_fails_on_git_error(tmp_path: Path) -> None:
    """P1: invalid base_ref must hard-fail (not fail-open to empty transition list)."""
    from omg_cli.parity_claim_gate import assert_pin_transitions_reviewed

    inventory = _bootstrapping_inventory(tmp_path)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "C0")

    catalogs = {
        source: load_json_object(
            tmp_path / "docs" / "parity" / "upstream-snapshots" / f"{source}.json"
        )
        for source in REQUIRED_SNAPSHOT_SOURCES
    }
    with pytest.raises(ContractValidationError, match=r"ancestor|git|base"):
        assert_pin_transitions_reviewed(
            inventory=inventory,
            base_inventory=inventory,
            repo_root=tmp_path,
            catalogs_by_source=catalogs,
            base_ref="definitely-not-a-real-ref",
        )


def test_pin_transition_hard_fails_on_malformed_historical_catalog(
    tmp_path: Path,
) -> None:
    """P1: invalid catalog at pin-transition child must not soft-fallback to worktree."""
    from omg_cli.parity_claim_gate import assert_pin_transitions_reviewed
    from omg_cli.parity_refresh import write_committed_refresh_review

    inventory = _bootstrapping_inventory(tmp_path)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    base_sha = _git_commit_all(tmp_path, "C0 base pin")

    new_pin = "cccccccccccccccccccccccccccccccccccccccc"
    bumped = copy.deepcopy(inventory)
    _bump_omc_pin(bumped, new_pin)
    _write_inventory(tmp_path, bumped)
    _write_required_snapshots(tmp_path, bumped)
    # Commit a malformed OMC catalog at the transition child.
    omc_catalog = tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json"
    omc_catalog.write_text("{not-valid-json\n", encoding="utf-8")
    child_sha = _git_commit_all(tmp_path, "C1 pin bump + malformed catalog")

    # Repair worktree catalog + write a matching review so soft-fallback would pass.
    _write_required_snapshots(tmp_path, bumped)
    worktree_catalogs = {
        source: load_json_object(
            tmp_path / "docs" / "parity" / "upstream-snapshots" / f"{source}.json"
        )
        for source in REQUIRED_SNAPSHOT_SOURCES
    }
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=worktree_catalogs["OMC"],
        source="OMC",
        new_pin=new_pin,
        generated_at=FIXED_NOW,
    )
    write_committed_refresh_review(tmp_path, plan)
    _git_commit_all(tmp_path, "C2 repair catalog + review (must not mask C1)")

    candidate = load_json_object(tmp_path / "docs" / "parity" / "omg-parity.json")
    with pytest.raises(
        ContractValidationError,
        match=r"invalid catalog|catalog JSON|git show failed",
    ):
        assert_pin_transitions_reviewed(
            inventory=candidate,
            base_inventory=inventory,
            repo_root=tmp_path,
            catalogs_by_source=worktree_catalogs,
            base_ref=base_sha,
        )
    # Sanity: the malformed blob is still reachable at the transition child.
    bad = subprocess.run(
        ["git", "-C", str(tmp_path), "show", f"{child_sha}:docs/parity/upstream-snapshots/OMC.json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 0
    assert "not-valid-json" in bad.stdout


def test_resolve_base_inventory_hard_fails_on_invalid_selected_tag(
    tmp_path: Path,
) -> None:
    """P1: invalid inventory at previous v* tag must not advance base to main."""
    from omg_cli.parity_claim_gate import resolve_base_inventory

    inventory = _bootstrapping_inventory(tmp_path)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "main")
    # C0 tagged release with malformed inventory JSON.
    inv_path = tmp_path / "docs" / "parity" / "omg-parity.json"
    inv_path.write_text("{not-valid-inventory\n", encoding="utf-8")
    _git_commit_all(tmp_path, "C0 tagged with bad inventory")
    _git(tmp_path, "tag", "v0.0.1")

    # Later main tip is valid — soft fallback would wrongly select it.
    _write_inventory(tmp_path, inventory)
    _git_commit_all(tmp_path, "C1 main repaired inventory")

    with pytest.raises(
        ContractValidationError,
        match=r"invalid inventory JSON|empty inventory blob",
    ):
        resolve_base_inventory(tmp_path, require=True)


def test_resolve_base_inventory_hard_fails_on_explicit_invalid_base_ref(
    tmp_path: Path,
) -> None:
    """P1: explicit --base-ref with invalid inventory must hard-fail (no main hop)."""
    from omg_cli.parity_claim_gate import resolve_base_inventory

    inventory = _bootstrapping_inventory(tmp_path)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "main")
    inv_path = tmp_path / "docs" / "parity" / "omg-parity.json"
    inv_path.write_text("{not-valid-inventory\n", encoding="utf-8")
    bad_sha = _git_commit_all(tmp_path, "bad inventory commit")
    _write_inventory(tmp_path, inventory)
    _git_commit_all(tmp_path, "repaired tip")

    with pytest.raises(
        ContractValidationError,
        match=r"invalid inventory JSON|empty inventory blob",
    ):
        resolve_base_inventory(tmp_path, base_ref=bad_sha, require=True)


def test_resolve_base_inventory_skips_missing_ref_then_uses_next(
    tmp_path: Path,
) -> None:
    """Missing candidate refs remain soft; only present-but-invalid hard-fails."""
    from omg_cli.parity_claim_gate import resolve_base_inventory

    inventory = _bootstrapping_inventory(tmp_path)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "main")
    _git_commit_all(tmp_path, "C0 on main")

    resolved = resolve_base_inventory(tmp_path, require=True)
    assert resolved is not None
    assert resolved.git_ref == "main"


def test_pin_transition_rejects_symlink_review_ledger(tmp_path: Path) -> None:
    """P2: symlink at expected ledger path must not satisfy HEAD blob verify."""
    from omg_cli.parity_refresh import write_committed_refresh_review

    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "2222222222222222222222222222222222222222"
    _bump_omc_pin(inventory, new_pin)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)

    catalog = load_json_object(
        tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json"
    )
    plan = build_refresh_plan(
        inventory=base,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=new_pin,
        generated_at=FIXED_NOW,
    )
    path = write_committed_refresh_review(tmp_path, plan)
    # Decoy tracked file with identical bytes (symlink target).
    decoy = tmp_path / "docs" / "parity" / "decoy-review.json"
    decoy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "commit review + decoy")

    path.unlink()
    path.symlink_to(decoy)

    with pytest.raises(
        ContractValidationError,
        match=r"symlink|regular file|type|differs|git diff",
    ):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            base_inventory=base,
            now=FIXED_NOW,
        )


def test_pin_transition_rejects_parent_directory_symlink(tmp_path: Path) -> None:
    """P2: parent-dir symlink must not redirect ledger verification."""
    from omg_cli.parity_refresh import (
        COMMITTED_REVIEWS_RELATIVE,
        write_committed_refresh_review,
    )

    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "3333333333333333333333333333333333333333"
    _bump_omc_pin(inventory, new_pin)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)

    catalog = load_json_object(
        tmp_path / "docs" / "parity" / "upstream-snapshots" / "OMC.json"
    )
    plan = build_refresh_plan(
        inventory=base,
        upstream_catalog=catalog,
        source="OMC",
        new_pin=new_pin,
        generated_at=FIXED_NOW,
    )
    path = write_committed_refresh_review(tmp_path, plan)
    reviews_dir = tmp_path / COMMITTED_REVIEWS_RELATIVE
    real_store = tmp_path / "docs" / "parity" / "reviews-real"
    real_store.mkdir(parents=True, exist_ok=True)
    # Move committed ledger into real store, then replace reviews/ with symlink.
    moved = real_store / path.name
    path.replace(moved)
    for child in list(reviews_dir.iterdir()):
        child.unlink()
    reviews_dir.rmdir()
    # Also keep a decoy copy under real_store that matches for content tricks.
    _init_git_repo(tmp_path)
    # Commit the real file at the expected path first.
    reviews_dir.mkdir(parents=True, exist_ok=True)
    committed = reviews_dir / path.name
    committed.write_text(moved.read_text(encoding="utf-8"), encoding="utf-8")
    _git_commit_all(tmp_path, "commit review at expected path")

    # Replace reviews/ with symlink to real_store (contains matching file).
    for child in reviews_dir.iterdir():
        child.unlink()
    reviews_dir.rmdir()
    reviews_dir.symlink_to(real_store)
    # Ensure target file exists via the symlink.
    assert (reviews_dir / path.name).is_file()

    with pytest.raises(
        ContractValidationError,
        match=r"symlink|regular file|type|differs|git diff",
    ):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            base_inventory=base,
            now=FIXED_NOW,
        )


def test_complete_healthy_still_rejects_live_proven_phrase(tmp_path: Path) -> None:
    """P2: complete + all-healthy must keep live-* phrase scan active."""
    inventory = _bootstrapping_inventory(tmp_path)
    inventory["inventory_status"] = "complete"
    inventory["category_status"] = {
        cat: "complete" for cat in sorted(PARITY_CATEGORY_TAXONOMY)
    }
    inventory["source_status"] = {
        "OMC": "complete",
        "OMX": "complete",
        "OmO": "complete",
        "Antigravity": "complete",
    }
    for row in inventory["capabilities"]:
        _mark_healthy(row)
    for gap in inventory.get("gaps", []):
        if isinstance(gap, dict):
            gap["status"] = "closed"
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    (tmp_path / "CHANGELOG.md").write_text(
        "All runtime paths are live-proven.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractValidationError, match=r"overclaim|live.?proven"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            now=FIXED_NOW,
        )


def test_upstream_drift_rejects_stale_delete_ack_after_fingerprint_mutates(
    tmp_path: Path,
) -> None:
    """P2: delete ack bound to fingerprint A must not clear delete of fingerprint B."""
    inventory = _minimal_inventory()
    target = next(
        row for row in inventory["capabilities"] if row["id"] == "omc.cli.session_surfaces"
    )
    # Ack deletion of original fingerprint.
    catalog_missing = _load_catalog(pin_revision=OMC_PIN)
    catalog_missing = copy.deepcopy(catalog_missing)
    catalog_missing["capabilities"] = [
        c for c in catalog_missing["capabilities"] if c["id"] != "omc.cli.session_surfaces"
    ]
    plan = build_refresh_plan(
        inventory=inventory,
        upstream_catalog=catalog_missing,
        source="OMC",
        new_pin=OMC_PIN,
        generated_at=FIXED_NOW,
    )
    stale_ack = _ack_review(plan)

    # Mutate inventory row fingerprint, then delete again — stale ack must fail.
    target["promise"] = "Mutated promise after prior delete ack"
    target["upstream"]["source_paths"] = ["README.md", "skills/mutated/SKILL.md"]
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    cat_path = _write_catalog(tmp_path, catalog_missing)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(stale_ack, indent=2), encoding="utf-8")

    with pytest.raises(ContractValidationError, match="upstream drift"):
        check_parity_release_claims(
            inventory_path=inv_path,
            repo_root=tmp_path,
            upstream_catalog_path=cat_path,
            review_artifact_path=review_path,
            now=FIXED_NOW,
        )


def _aba_pin_history(tmp_path: Path) -> tuple[dict, str, Path]:
    """Build A→B→A merge history; return (base_inventory_A, base_sha, inv_path)."""
    pin_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    pin_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    inventory = _bootstrapping_inventory(tmp_path)
    _bump_omc_pin(inventory, pin_a)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    inv_path = _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "main")
    base_sha = _git_commit_all(tmp_path, "base pin A")

    _git(tmp_path, "checkout", "-b", "feature")
    bumped = copy.deepcopy(inventory)
    _bump_omc_pin(bumped, pin_b)
    _write_inventory(tmp_path, bumped)
    _write_required_snapshots(tmp_path, bumped)
    _git_commit_all(tmp_path, "feature bump A→B without review")

    reverted = copy.deepcopy(inventory)
    _bump_omc_pin(reverted, pin_a)
    _write_inventory(tmp_path, reverted)
    _write_required_snapshots(tmp_path, reverted)
    _git_commit_all(tmp_path, "feature revert B→A without review")

    _git(tmp_path, "checkout", "main")
    _git(tmp_path, "merge", "--no-ff", "-m", "merge feature (final still A)", "feature")
    return inventory, base_sha, inv_path


def test_release_file_only_base_inventory_rejects_aba_mask(tmp_path: Path) -> None:
    """P2: --release + file-only --base-inventory must not false-pass A→B→A.

    Endpoint-only compare sees A→A and would skip the unreviewed B ledger;
    release mode must refuse file-only base (no git provenance / DAG walk).
    """
    from omg_cli.parity_check import check_parity_inventory

    inventory, _base_sha, inv_path = _aba_pin_history(tmp_path)
    base_file = tmp_path / "base-A.json"
    base_file.write_text(json.dumps(inventory, indent=2), encoding="utf-8")

    with pytest.raises(
        ContractValidationError,
        match=r"git base provenance|base-ref|--base-ref|insufficient for --release",
    ):
        check_parity_inventory(
            inventory_path=inv_path,
            repo_root=tmp_path,
            release=True,
            base_inventory_path=base_file,
        )


def test_release_base_inventory_bound_to_base_ref_still_walks_aba(
    tmp_path: Path,
) -> None:
    """P2: --base-inventory + matching --base-ref retains DAG walk (catches mid B)."""
    from omg_cli.parity_check import check_parity_inventory

    inventory, base_sha, inv_path = _aba_pin_history(tmp_path)
    del inventory  # base content comes from the git blob at base_ref
    base_file = tmp_path / "base-A.json"
    # File must match the git blob at base_ref (canonical inventory at that commit).
    blob = subprocess.run(
        ["git", "-C", str(tmp_path), "show", f"{base_sha}:docs/parity/omg-parity.json"],
        check=True,
        capture_output=True,
        text=True,
    )
    base_file.write_text(blob.stdout, encoding="utf-8")

    with pytest.raises(
        ContractValidationError,
        match="pin transition missing committed refresh review",
    ):
        check_parity_inventory(
            inventory_path=inv_path,
            repo_root=tmp_path,
            release=True,
            base_inventory_path=base_file,
            base_ref=base_sha,
        )


def test_resolve_base_inventory_require_rejects_file_only(tmp_path: Path) -> None:
    """P2: require=True + file/dict base without git_ref must hard-fail."""
    from omg_cli.parity_claim_gate import resolve_base_inventory

    inventory = _bootstrapping_inventory(tmp_path)
    inv_path = _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "C0")

    with pytest.raises(
        ContractValidationError,
        match=r"git base provenance|base-ref|insufficient for --release",
    ):
        resolve_base_inventory(
            tmp_path, base_inventory_path=inv_path, require=True
        )
    with pytest.raises(
        ContractValidationError,
        match=r"git base provenance|base-ref|insufficient for --release",
    ):
        resolve_base_inventory(tmp_path, base_inventory=inventory, require=True)


def test_resolve_base_inventory_binds_file_to_matching_base_ref(
    tmp_path: Path,
) -> None:
    """File + matching --base-ref keeps git_ref for DAG walk."""
    from omg_cli.parity_claim_gate import resolve_base_inventory

    inventory = _bootstrapping_inventory(tmp_path)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    base_sha = _git_commit_all(tmp_path, "C0")
    blob = subprocess.run(
        ["git", "-C", str(tmp_path), "show", f"{base_sha}:docs/parity/omg-parity.json"],
        check=True,
        capture_output=True,
        text=True,
    )
    base_file = tmp_path / "export.json"
    base_file.write_text(blob.stdout, encoding="utf-8")

    resolved = resolve_base_inventory(
        tmp_path,
        base_inventory_path=base_file,
        base_ref=base_sha,
        require=True,
    )
    assert resolved is not None
    assert resolved.git_ref == base_sha
    assert resolved.inventory == inventory


def test_resolve_base_inventory_rejects_mismatched_file_and_base_ref(
    tmp_path: Path,
) -> None:
    """Never silently prefer a divergent --base-inventory over --base-ref."""
    from omg_cli.parity_claim_gate import resolve_base_inventory

    inventory = _bootstrapping_inventory(tmp_path)
    _write_inventory(tmp_path, inventory)
    _init_git_repo(tmp_path)
    base_sha = _git_commit_all(tmp_path, "C0")

    divergent = copy.deepcopy(inventory)
    _bump_omc_pin(divergent, "ffffffffffffffffffffffffffffffffffffffff")
    bad_file = tmp_path / "divergent.json"
    bad_file.write_text(json.dumps(divergent, indent=2), encoding="utf-8")

    with pytest.raises(
        ContractValidationError,
        match=r"does not match git blob|base-inventory.*base_ref|mismatch",
    ):
        resolve_base_inventory(
            tmp_path,
            base_inventory_path=bad_file,
            base_ref=base_sha,
            require=True,
        )


def test_resolve_base_inventory_rejects_conflicting_file_authorities(
    tmp_path: Path,
) -> None:
    """Dict + path base authorities must not silently override each other."""
    from omg_cli.parity_claim_gate import resolve_base_inventory

    inventory = _bootstrapping_inventory(tmp_path)
    path = _write_inventory(tmp_path, inventory)
    with pytest.raises(ContractValidationError, match=r"conflicting base inventory"):
        resolve_base_inventory(
            tmp_path,
            base_inventory=inventory,
            base_inventory_path=path,
            require=False,
        )
