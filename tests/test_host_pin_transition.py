"""GROK_BUILD pin-transition host-baseline review enforcement (#105 PR1)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import FROZEN_PINS, HOST_BASELINE_PIN_ID
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_claim_gate import (
    assert_host_baseline_gate,
    check_parity_release_claims,
    load_host_baseline_snapshot,
)
from omg_cli.parity_refresh import (
    build_host_baseline_refresh_plan,
    canonical_changes_digest,
    committed_review_path,
    generated_docs_content_hash,
    host_baseline_receipt_digest,
    host_snapshot_content_hash,
    write_committed_host_baseline_review,
)
from tests.test_parity_claim_gate import (
    FIXED_NOW,
    _bootstrapping_inventory,
    _git_commit_all,
    _honest_docs,
    _init_git_repo,
    _scaffold_inventory_paths,
    _write_host_baseline_snapshot,
    _write_inventory,
    _write_required_snapshots,
)

OLD_PIN = "7cfcb20d2b50b0d18801a6c0af2e401c0e060894"
NEW_PIN = "cccccccccccccccccccccccccccccccccccccccc"


def _commit_host_review(tmp_path: Path, *, from_pin: str, to_pin: str) -> Path:
    snapshot = load_host_baseline_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    plan = build_host_baseline_refresh_plan(
        from_revision=from_pin,
        to_revision=to_pin,
        host_snapshot=snapshot,
        previous_snapshot=None,
        generated_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
        snapshot_hash=host_snapshot_content_hash(snapshot),
        generated_docs_hash=docs_hash,
    )
    return write_committed_host_baseline_review(tmp_path, plan)


def test_host_pin_transition_requires_review_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    base["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = OLD_PIN
    inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = NEW_PIN
    monkeypatch.setitem(FROZEN_PINS, HOST_BASELINE_PIN_ID, NEW_PIN)
    _scaffold_inventory_paths(tmp_path, inventory)
    _write_host_baseline_snapshot(tmp_path, inventory)
    with pytest.raises(
        ContractValidationError,
        match="GROK_BUILD pin transition missing committed host baseline review",
    ):
        assert_host_baseline_gate(
            inventory=inventory,
            repo_root=tmp_path,
            base_inventory=base,
        )


def test_host_pin_transition_passes_with_committed_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    base["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = OLD_PIN
    inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = NEW_PIN
    monkeypatch.setitem(FROZEN_PINS, HOST_BASELINE_PIN_ID, NEW_PIN)
    inv_path = _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _commit_host_review(tmp_path, from_pin=OLD_PIN, to_pin=NEW_PIN)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "commit host review")
    payload = check_parity_release_claims(
        inventory_path=inv_path,
        repo_root=tmp_path,
        base_inventory=base,
        now=FIXED_NOW,
    )
    assert payload["ok"] is True
    assert payload["host_baseline_checked"] is True


def test_host_pin_transition_rejects_untracked_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    base["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = OLD_PIN
    inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = NEW_PIN
    monkeypatch.setitem(FROZEN_PINS, HOST_BASELINE_PIN_ID, NEW_PIN)
    _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "base without review")
    _commit_host_review(tmp_path, from_pin=OLD_PIN, to_pin=NEW_PIN)
    with pytest.raises(ContractValidationError, match=r"not tracked by git"):
        assert_host_baseline_gate(
            inventory=inventory,
            repo_root=tmp_path,
            base_inventory=base,
        )


def test_write_committed_host_review_uses_content_binding_filename(
    tmp_path: Path,
) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    _scaffold_inventory_paths(tmp_path, inventory)
    _write_host_baseline_snapshot(tmp_path, inventory, write_binding_review=False)
    path = _commit_host_review(tmp_path, from_pin=OLD_PIN, to_pin=FROZEN_PINS[HOST_BASELINE_PIN_ID])
    payload = json.loads(path.read_text(encoding="utf-8"))
    changes_digest = canonical_changes_digest(payload["changes"])
    identity = host_baseline_receipt_digest(
        change_digest=changes_digest,
        snapshot_hash=payload["host_baseline"]["snapshot_hash"],
        generated_docs_hash=payload["host_baseline"]["generated_docs_hash"],
    )
    assert payload["change_digest"] == changes_digest
    assert payload["content_binding_digest"] == identity
    assert path.name.endswith(f"{identity}.json")
    changes_only = committed_review_path(
        tmp_path,
        source=HOST_BASELINE_PIN_ID,
        from_revision=OLD_PIN,
        to_revision=FROZEN_PINS[HOST_BASELINE_PIN_ID],
        change_digest=changes_digest,
    )
    if changes_only != path:
        assert not changes_only.exists()


def test_canonical_repo_host_pin_transition_review_exists() -> None:
    """Repo tip must include a content-bound 7cfcb20→a5589e9 host review."""
    root = Path(__file__).resolve().parents[1]
    reviews = root / "docs" / "parity" / "reviews"
    matches = list(
        reviews.glob(
            "GROK_BUILD-7cfcb20d2b50b0d18801a6c0af2e401c0e060894-"
            "a5589e958437d79e13db026eedcb1720bffd4063-*.json"
        )
    )
    assert matches, "missing committed GROK_BUILD host baseline review ledger"
    snapshot = load_host_baseline_snapshot(root)
    snapshot_hash = host_snapshot_content_hash(snapshot)
    docs_hash = generated_docs_content_hash(root, snapshot["generated"]["docs"])
    bound = []
    for path in matches:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["source"] == HOST_BASELINE_PIN_ID
        host_meta = payload["host_baseline"]
        assert host_meta["reviewed_pin"] == FROZEN_PINS[HOST_BASELINE_PIN_ID]
        if (
            host_meta.get("snapshot_hash") == snapshot_hash
            and host_meta.get("generated_docs_hash") == docs_hash
        ):
            bound.append(path)
    assert bound, "no GROK_BUILD receipt binds current snapshot_hash/generated_docs_hash"
