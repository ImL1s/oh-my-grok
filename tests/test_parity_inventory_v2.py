"""Parity inventory schema v2 claimability gates (#78-A)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import (
    PARITY_MATURITY_LEVELS,
    PARITY_V2_CLASSIFICATIONS,
    claim_marker_for_capability,
    inventory_completion_claims_allowed,
    load_json_object,
    maturity_rank,
    validate_parity_inventory,
)
from omg_cli.contracts.state_schemas import ContractValidationError


ROOT = Path(__file__).resolve().parents[1]
V1_FIXTURE = ROOT / "tests" / "fixtures" / "parity" / "omg-parity-v1.json"


def _fresh_iso(*, days_ago: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _touch(path: Path, body: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _upstream_roots(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "upstream" / "mirror"
    for relative in (
        "src/adapter.ts",
        "src/jobs.ts",
        "src/team.ts",
        "docs/parity.md",
    ):
        _touch(root / relative)
    return {
        "Antigravity": root,
        "OMX": root,
        "OMC": root,
        "OmO": root,
        "GROK_BUILD": root,
    }


def _base_v2_inventory(tmp_path: Path) -> dict:
    """Minimal valid bootstrapping v2 inventory for mutation tests."""
    _touch(tmp_path / "omg_cli" / "ask" / "providers.py", "# stub\n")
    _touch(tmp_path / "plugin.json", "{}\n")
    _touch(tmp_path / "hooks" / "hooks.json", "{}\n")
    _touch(tmp_path / "omg_cli" / "__init__.py", "")
    _touch(tmp_path / "docs" / "parity" / "omg-parity.json", "{}\n")
    _touch(tmp_path / "docs" / "parity" / "README.md", "# parity\n")
    _touch(tmp_path / "tests" / "test_parity_inventory_v2.py", "# stub\n")
    _upstream_roots(tmp_path)
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
                "repository": "https://github.com/example/oh-my-claudecode",
                "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "kind": "commit",
            },
            "OMX": {
                "repository": "https://github.com/example/oh-my-codex",
                "revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "kind": "commit",
            },
            "OmO": {
                "repository": "https://github.com/example/oh-my-opencode",
                "revision": "cccccccccccccccccccccccccccccccccccccccc",
                "kind": "commit",
            },
            "Antigravity": {
                "repository": "https://github.com/example/antigravity",
                "revision": "dddddddddddddddddddddddddddddddddddddddd",
                "kind": "commit",
            },
            "GROK_BUILD": {
                "repository": "https://github.com/example/grok-build",
                "revision": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                "kind": "commit",
            },
        },
        "category_status": {
            "antigravity": "bootstrapping",
            "jobs": "bootstrapping",
            "team": "bootstrapping",
            "parity_governance": "bootstrapping",
        },
        "live_evidence_max_age_days": 30,
        "capabilities": [
            {
                "id": "antigravity.provider.adapter",
                "category": "antigravity",
                "promise": "First-class Antigravity provider adapter",
                "classification": "antigravity_native",
                "upstream": {
                    "source": "Antigravity",
                    "revision": "dddddddddddddddddddddddddddddddddddddddd",
                    "source_paths": ["src/adapter.ts"],
                },
                "omg_paths": ["omg_cli/ask/providers.py"],
                "runtime_owner": "omg",
                "maturity": {"grok": "catalogued", "antigravity": "catalogued"},
                "evidence": {"tests": [], "docs": ["docs/parity/README.md"], "live": []},
                "issues": ["#67"],
                "gap": "Adapter not yet implemented; tracked by #67.",
            },
            {
                "id": "jobs.durable_background",
                "category": "jobs",
                "promise": "Durable background job plane",
                "classification": "omg_native",
                "upstream": {
                    "source": "OMX",
                    "revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "source_paths": ["src/jobs.ts"],
                },
                "omg_paths": ["omg_cli/ask/providers.py"],
                "runtime_owner": "omg",
                "maturity": {"grok": "catalogued"},
                "evidence": {"tests": [], "docs": [], "live": []},
                "issues": ["#68"],
                "gap": "Durable jobs not yet implemented; tracked by #68.",
            },
            {
                "id": "team.plane_v3",
                "category": "team",
                "promise": "Team plane v3 with job-backed panes",
                "classification": "omg_native",
                "upstream": {
                    "source": "OMC",
                    "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "source_paths": ["src/team.ts"],
                },
                "omg_paths": ["omg_cli/ask/providers.py"],
                "runtime_owner": "omg",
                "maturity": {"grok": "catalogued"},
                "evidence": {"tests": [], "docs": [], "live": []},
                "issues": ["#69"],
                "gap": "Team v3 blocked on #67/#68; tracked by #69.",
            },
            {
                "id": "parity.inventory.governance",
                "category": "parity_governance",
                "promise": "Canonical parity inventory with claimability gates",
                "classification": "omg_native",
                "upstream": {
                    "source": "OMC",
                    "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "source_paths": ["docs/parity.md"],
                },
                "omg_paths": ["omg_cli/ask/providers.py"],
                "runtime_owner": "omg",
                "maturity": {"grok": "catalogued"},
                "evidence": {"tests": [], "docs": [], "live": []},
                "issues": ["#78"],
                "gap": "#78-B/#78-C remaining after inventory v2 substrate.",
            },
        ],
        "gaps": [
            {
                "id": "gap.antigravity.provider",
                "priority": "P0",
                "status": "open",
                "issues": ["#67"],
                "capability_ids": ["antigravity.provider.adapter"],
                "summary": "Antigravity adapter missing",
            },
            {
                "id": "gap.jobs.durable",
                "priority": "P0",
                "status": "open",
                "issues": ["#68"],
                "capability_ids": ["jobs.durable_background"],
                "summary": "Durable jobs missing",
            },
            {
                "id": "gap.team.v3",
                "priority": "P0",
                "status": "open",
                "issues": ["#69"],
                "capability_ids": ["team.plane_v3"],
                "summary": "Team v3 missing",
            },
            {
                "id": "gap.parity.governance.remaining",
                "priority": "P0",
                "status": "open",
                "issues": ["#78"],
                "capability_ids": ["parity.inventory.governance"],
                "summary": "#78-B/#78-C remaining",
            },
        ],
    }


def test_v1_fixture_still_validates() -> None:
    inventory = validate_parity_inventory(load_json_object(V1_FIXTURE))
    assert inventory["schema_version"] == 1
    assert "OMG" in inventory["frozen_pins"]


def _validate(inventory: dict, tmp_path: Path):
    return validate_parity_inventory(
        inventory,
        repo_root=tmp_path,
        upstream_roots=_upstream_roots(tmp_path),
    )


def test_v2_uses_user_observable_capability_ids(tmp_path: Path) -> None:
    inventory = _base_v2_inventory(tmp_path)
    # Requirement-style IDs are not user-observable capability IDs.
    inventory["capabilities"][0]["id"] = "DUAL-001"
    with pytest.raises(ContractValidationError, match="user-observable"):
        _validate(inventory, tmp_path)

    inventory = _base_v2_inventory(tmp_path)
    validated = _validate(inventory, tmp_path)
    assert all("." in row["id"] for row in validated["capabilities"])
    assert "OMG" not in validated["upstream_pins"]


def test_duplicate_capability_id_rejected(tmp_path: Path) -> None:
    inventory = _base_v2_inventory(tmp_path)
    inventory["capabilities"].append(copy.deepcopy(inventory["capabilities"][0]))
    with pytest.raises(ContractValidationError, match="duplicate capability id"):
        _validate(inventory, tmp_path)


def test_upstream_pin_requires_exact_revision_and_existing_source_paths(
    tmp_path: Path,
) -> None:
    inventory = _base_v2_inventory(tmp_path)
    inventory["upstream_pins"]["Antigravity"]["revision"] = "main"
    with pytest.raises(ContractValidationError, match="exact revision"):
        _validate(inventory, tmp_path)

    inventory = _base_v2_inventory(tmp_path)
    inventory["capabilities"][0]["upstream"]["source_paths"] = ["missing/nope.ts"]
    with pytest.raises(ContractValidationError, match="source path"):
        _validate(inventory, tmp_path)


def test_alias_requires_existing_canonical_target(tmp_path: Path) -> None:
    inventory = _base_v2_inventory(tmp_path)
    inventory["capabilities"].append(
        {
            "id": "agy.provider.alias",
            "category": "antigravity",
            "promise": "Alias for Antigravity adapter",
            "classification": "alias",
            "alias_of": "antigravity.provider.adapter.missing",
            "upstream": {
                "source": "Antigravity",
                "revision": "dddddddddddddddddddddddddddddddddddddddd",
                "source_paths": ["src/adapter.ts"],
            },
            "omg_paths": ["omg_cli/ask/providers.py"],
            "runtime_owner": "omg",
            "maturity": {"grok": "catalogued"},
            "evidence": {"tests": [], "docs": [], "live": []},
            "issues": ["#67"],
            "gap": "alias target missing",
        }
    )
    with pytest.raises(ContractValidationError, match="alias"):
        _validate(inventory, tmp_path)

    inventory = _base_v2_inventory(tmp_path)
    inventory["capabilities"].append(
        {
            "id": "agy.provider.alias",
            "category": "antigravity",
            "promise": "Alias for Antigravity adapter",
            "classification": "alias",
            "alias_of": "antigravity.provider.adapter",
            "upstream": {
                "source": "Antigravity",
                "revision": "dddddddddddddddddddddddddddddddddddddddd",
                "source_paths": ["src/adapter.ts"],
            },
            "omg_paths": ["omg_cli/ask/providers.py"],
            "runtime_owner": "omg",
            "maturity": {"grok": "catalogued"},
            "evidence": {"tests": [], "docs": [], "live": []},
            "issues": ["#67"],
            "gap": "",
        }
    )
    _validate(inventory, tmp_path)


def test_maturity_prerequisites_are_monotonic(tmp_path: Path) -> None:
    assert list(PARITY_MATURITY_LEVELS) == [
        "catalogued",
        "configured",
        "installed",
        "enabled",
        "loadable",
        "observed",
        "healthy",
        "live_verified",
    ]
    assert maturity_rank("catalogued") < maturity_rank("live_verified")

    inventory = _base_v2_inventory(tmp_path)
    # Claiming healthy without prior-level evidence fields (configured path etc.)
    # must fail — single enum still requires monotonic prerequisites.
    inventory["capabilities"][0]["maturity"] = {"grok": "healthy"}
    inventory["capabilities"][0]["evidence"] = {
        "tests": ["tests/test_parity_inventory_v2.py"],
        "docs": [],
        "live": [],
        "configured_paths": [],
        "install_evidence": [],
    }
    with pytest.raises(ContractValidationError, match="maturity prerequisite"):
        _validate(inventory, tmp_path)


def test_live_verified_requires_fresh_runtime_platform_evidence(tmp_path: Path) -> None:
    inventory = _base_v2_inventory(tmp_path)
    inventory["capabilities"][0]["maturity"] = {"grok": "live_verified"}
    inventory["capabilities"][0]["evidence"] = {
        "tests": ["tests/test_parity_inventory_v2.py"],
        "docs": ["docs/parity/README.md"],
        "configured_paths": ["omg_cli/ask/providers.py"],
        "install_evidence": ["plugin.json"],
        "enabled_evidence": ["hooks/hooks.json"],
        "loadable_evidence": ["omg_cli/__init__.py"],
        "observed_evidence": ["docs/parity/omg-parity.json"],
        "healthy_evidence": ["tests/test_parity_inventory_v2.py"],
        "live": [
            {
                "runtime": "grok",
                "platform": "darwin-arm64",
                "version": "0.2.107",
                "observed_at": _fresh_iso(days_ago=90),
                "marker": "LIVE_ANTIGRAVITY_PROVIDER_OK",
            }
        ],
    }
    with pytest.raises(ContractValidationError, match="fresh"):
        _validate(inventory, tmp_path)

    inventory["capabilities"][0]["evidence"]["live"][0]["observed_at"] = _fresh_iso(
        days_ago=1
    )
    _validate(inventory, tmp_path)


def test_optional_unclaimed_cannot_generate_positive_claim(tmp_path: Path) -> None:
    """Pro PR85c P2: optional_unclaimed must not emit healthy/live_verified markers."""
    inventory = _base_v2_inventory(tmp_path)
    inventory["inventory_status"] = "complete"
    for cat in inventory["category_status"]:
        inventory["category_status"][cat] = "complete"
    inventory["capabilities"][0]["classification"] = "optional_unclaimed"
    inventory["capabilities"][0]["maturity"] = {"grok": "catalogued"}
    validated = _validate(inventory, tmp_path)
    marker = claim_marker_for_capability(validated["capabilities"][0], inventory=validated)
    assert marker == "optional_unclaimed"

    inventory["capabilities"][0]["maturity"] = {"grok": "healthy"}
    inventory["capabilities"][0]["evidence"] = {
        "tests": ["tests/test_parity_inventory_v2.py"],
        "docs": ["docs/parity/README.md"],
        "configured_paths": ["omg_cli/ask/providers.py"],
        "install_evidence": ["plugin.json"],
        "enabled_evidence": ["hooks/hooks.json"],
        "loadable_evidence": ["omg_cli/__init__.py"],
        "observed_evidence": ["docs/parity/omg-parity.json"],
        "healthy_evidence": ["tests/test_parity_inventory_v2.py"],
        "live": [],
    }
    with pytest.raises(ContractValidationError, match="positive claim"):
        _validate(inventory, tmp_path)

    # Alias of optional_unclaimed healthy-canonical must not recurse to healthy.
    inventory["capabilities"][0]["maturity"] = {"grok": "catalogued"}
    inventory["capabilities"][0]["evidence"] = {
        "tests": [],
        "docs": ["docs/parity/README.md"],
        "live": [],
    }
    inventory["capabilities"].append(
        {
            "id": "agy.provider.alias",
            "category": "antigravity",
            "promise": "Alias of optional_unclaimed",
            "classification": "alias",
            "alias_of": "antigravity.provider.adapter",
            "upstream": {
                "source": "Antigravity",
                "revision": "dddddddddddddddddddddddddddddddddddddddd",
                "source_paths": ["src/adapter.ts"],
            },
            "omg_paths": [],
            "runtime_owner": "omg",
            "maturity": {"grok": "catalogued"},
            "evidence": {"tests": [], "docs": ["docs/parity/README.md"], "live": []},
            "issues": ["#67"],
            "gap": "alias of optional_unclaimed",
        }
    )
    validated = _validate(inventory, tmp_path)
    alias = next(c for c in validated["capabilities"] if c["id"] == "agy.provider.alias")
    assert claim_marker_for_capability(alias, inventory=validated) == "optional_unclaimed"


def test_host_impossible_cannot_generate_positive_claim(tmp_path: Path) -> None:
    inventory = _base_v2_inventory(tmp_path)
    inventory["capabilities"][0]["classification"] = "host_impossible"
    inventory["capabilities"][0]["maturity"] = {"grok": "catalogued"}
    validated = _validate(inventory, tmp_path)
    marker = claim_marker_for_capability(validated["capabilities"][0], inventory=validated)
    assert marker is None or marker in {"—", "n/a", "host_impossible"}
    assert marker not in {"✅", "✓", "complete", "implemented", "live_verified"}

    # Explicit positive claim attempt via healthy maturity is rejected.
    inventory["capabilities"][0]["maturity"] = {"grok": "healthy"}
    inventory["capabilities"][0]["evidence"] = {
        "tests": ["tests/test_parity_inventory_v2.py"],
        "docs": ["docs/parity/README.md"],
        "configured_paths": ["omg_cli/ask/providers.py"],
        "install_evidence": ["plugin.json"],
        "enabled_evidence": ["hooks/hooks.json"],
        "loadable_evidence": ["omg_cli/__init__.py"],
        "observed_evidence": ["docs/parity/omg-parity.json"],
        "healthy_evidence": ["tests/test_parity_inventory_v2.py"],
        "live": [],
    }
    with pytest.raises(ContractValidationError, match="positive claim"):
        _validate(inventory, tmp_path)


def test_incomplete_inventory_cannot_emit_percentage_or_checkmark(tmp_path: Path) -> None:
    inventory = _base_v2_inventory(tmp_path)
    validated = _validate(inventory, tmp_path)
    assert validated["inventory_status"] == "bootstrapping"
    assert inventory_completion_claims_allowed(validated) is False
    for row in validated["capabilities"]:
        marker = claim_marker_for_capability(row, inventory=validated)
        assert "%" not in str(marker)
        assert marker not in {"✅", "✓"}


def test_alias_maturity_cannot_outrank_canonical(tmp_path: Path) -> None:
    inventory = _base_v2_inventory(tmp_path)
    inventory["capabilities"][0]["classification"] = "host_impossible"
    inventory["capabilities"][0]["maturity"] = {"grok": "catalogued"}
    inventory["capabilities"].append(
        {
            "id": "agy.provider.alias",
            "category": "antigravity",
            "promise": "Alias that overclaims vs host_impossible canonical",
            "classification": "alias",
            "alias_of": "antigravity.provider.adapter",
            "upstream": {
                "source": "Antigravity",
                "revision": "dddddddddddddddddddddddddddddddddddddddd",
                "source_paths": ["src/adapter.ts"],
            },
            "omg_paths": [],
            "runtime_owner": "omg",
            "maturity": {"grok": "healthy"},
            "evidence": {
                "tests": ["tests/test_parity_inventory_v2.py"],
                "docs": ["docs/parity/README.md"],
                "live": [],
                "configured_paths": ["omg_cli/ask/providers.py"],
                "install_evidence": ["plugin.json"],
                "enabled_evidence": ["hooks/hooks.json"],
                "loadable_evidence": ["omg_cli/__init__.py"],
                "observed_evidence": ["docs/parity/omg-parity.json"],
                "healthy_evidence": ["tests/test_parity_inventory_v2.py"],
            },
            "issues": ["#67"],
            "gap": "alias overclaim must be rejected",
        }
    )
    with pytest.raises(ContractValidationError, match="alias"):
        _validate(inventory, tmp_path)

    # Even if schema were bypassed, markers derive from the canonical target.
    inventory["capabilities"][-1]["maturity"] = {"grok": "catalogued"}
    inventory["capabilities"][-1]["evidence"] = {"tests": [], "docs": [], "live": []}
    validated = _validate(inventory, tmp_path)
    alias = next(row for row in validated["capabilities"] if row["id"] == "agy.provider.alias")
    marker = claim_marker_for_capability(alias, inventory=validated)
    assert marker == "host_impossible"


def test_alias_maturity_cannot_exceed_canonical_runtime_rank(tmp_path: Path) -> None:
    inventory = _base_v2_inventory(tmp_path)
    inventory["capabilities"][0]["maturity"] = {"grok": "configured"}
    inventory["capabilities"][0]["evidence"] = {
        "tests": [],
        "docs": [],
        "live": [],
        "configured_paths": ["omg_cli/ask/providers.py"],
    }
    inventory["capabilities"].append(
        {
            "id": "agy.provider.alias",
            "category": "antigravity",
            "promise": "Alias with higher maturity than canonical",
            "classification": "alias",
            "alias_of": "antigravity.provider.adapter",
            "upstream": {
                "source": "Antigravity",
                "revision": "dddddddddddddddddddddddddddddddddddddddd",
                "source_paths": ["src/adapter.ts"],
            },
            "omg_paths": [],
            "runtime_owner": "omg",
            "maturity": {"grok": "installed"},
            "evidence": {
                "tests": [],
                "docs": [],
                "live": [],
                "configured_paths": ["omg_cli/ask/providers.py"],
                "install_evidence": ["plugin.json"],
            },
            "issues": ["#67"],
            "gap": "",
        }
    )
    with pytest.raises(ContractValidationError, match="exceeds canonical"):
        _validate(inventory, tmp_path)
