"""Acceptance simulations for --release claim gate (#78-C Task 4)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_check import check_parity_inventory
from tests.test_parity_claim_gate import (
    OVERCLAIM_README,
    _bootstrapping_inventory,
    _honest_docs,
    _live_verified_row,
    _load_catalog,
    _minimal_inventory,
    _scaffold_inventory_paths,
    _write_inventory,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_upstream_snapshot(tmp_path: Path, catalog: dict) -> Path:
    snap_dir = tmp_path / "docs" / "parity" / "upstream-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / "omc.json"
    path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    return path


def _assert_release_gate_fails(
    *,
    tmp_path: Path,
    inventory: dict,
    error_pattern: str,
    catalog: dict | None = None,
) -> None:
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    if catalog is not None:
        _write_upstream_snapshot(tmp_path, catalog)

    with pytest.raises(ContractValidationError, match=error_pattern):
        check_parity_inventory(
            inventory_path=inv_path,
            repo_root=tmp_path,
            release=True,
        )


def test_simulate_upstream_add_makes_release_gate_nonzero(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"].append(
        {
            "id": "omc.new.capability",
            "source_paths": ["skills/new/SKILL.md"],
            "promise": "Brand new upstream capability",
        }
    )
    _assert_release_gate_fails(
        tmp_path=tmp_path,
        inventory=inventory,
        catalog=catalog,
        error_pattern="upstream drift",
    )


def test_simulate_upstream_delete_makes_release_gate_nonzero(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    catalog["capabilities"] = [
        c for c in catalog["capabilities"] if c["id"] != "omc.cli.session_surfaces"
    ]
    _assert_release_gate_fails(
        tmp_path=tmp_path,
        inventory=inventory,
        catalog=catalog,
        error_pattern="upstream drift",
    )


def test_simulate_upstream_rename_makes_release_gate_nonzero(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    catalog = _load_catalog()
    catalog = copy.deepcopy(catalog)
    for cap in catalog["capabilities"]:
        if cap["id"] == "omc.cli.session_surfaces":
            cap["id"] = "omc.cli.session_surfaces_v2"
            break
    _assert_release_gate_fails(
        tmp_path=tmp_path,
        inventory=inventory,
        catalog=catalog,
        error_pattern="upstream drift",
    )


def test_simulate_expired_live_evidence_makes_release_gate_nonzero(tmp_path: Path) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    _live_verified_row(inventory["capabilities"][0], days_ago=90)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)

    with pytest.raises(ContractValidationError, match="fresh"):
        check_parity_inventory(
            inventory_path=inv_path,
            repo_root=tmp_path,
            release=True,
        )


def test_simulate_release_overclaim_makes_release_gate_nonzero(tmp_path: Path) -> None:
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
        check_parity_inventory(
            inventory_path=inv_path,
            repo_root=tmp_path,
            release=True,
        )


def test_script_check_parity_inventory_release_flag(tmp_path: Path) -> None:
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

    script = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_parity_inventory.py"),
            "--release",
            "--inventory",
            str(inv_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **dict(__import__("os").environ),
            "PYTHONPATH": str(ROOT),
        },
    )
    assert script.returncode != 0
    assert "overclaim" in script.stderr.lower() or "overclaim" in script.stdout.lower()
