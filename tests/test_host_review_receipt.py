"""Canonical immutable GROK_BUILD host-review receipt validator (PR158 H1)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omg_cli.contracts.parity_schema import FROZEN_PINS, HOST_BASELINE_PIN_ID
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.parity_claim_gate import (
    assert_canonical_immutable_host_review_receipt,
    assert_host_baseline_gate,
    assert_host_review_binds_current_content,
    parse_committed_review_filename,
)
from omg_cli.parity_refresh import (
    build_host_baseline_refresh_plan,
    generated_docs_content_hash,
    host_snapshot_content_hash,
    write_committed_host_baseline_review,
)
from tests.test_parity_claim_gate import (
    _bootstrapping_inventory,
    _ensure_fixture_git_commit,
    _git_commit_all,
    _honest_docs,
    _init_git_repo,
    _scaffold_inventory_paths,
    _write_host_baseline_snapshot,
    _write_required_snapshots,
)
from tests.test_host_pin_transition import OLD_PIN

ROOT = Path(__file__).resolve().parents[1]
FROM_PIN = "7cfcb20d2b50b0d18801a6c0af2e401c0e060894"


def _mint_current_receipt(tmp_path: Path, *, from_pin: str = FROM_PIN) -> Path:
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    plan = build_host_baseline_refresh_plan(
        from_revision=from_pin,
        to_revision=snapshot["public_commit"],
        host_snapshot=snapshot,
        previous_snapshot=None,
        generated_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
        snapshot_hash=host_snapshot_content_hash(snapshot),
        generated_docs_hash=docs_hash,
    )
    return write_committed_host_baseline_review(tmp_path, plan)


def load_snapshot(tmp_path: Path):
    from omg_cli.parity_claim_gate import load_host_baseline_snapshot

    return load_host_baseline_snapshot(tmp_path)


def _clear_host_reviews(tmp_path: Path) -> None:
    reviews = tmp_path / "docs" / "parity" / "reviews"
    if not reviews.is_dir():
        return
    for path in reviews.glob("GROK_BUILD-*.json"):
        path.unlink()


def _prepare_tree(tmp_path: Path, *, write_review: bool = False) -> dict:
    inventory = _bootstrapping_inventory(tmp_path)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _write_host_baseline_snapshot(
        tmp_path, inventory, write_binding_review=write_review
    )
    if not write_review:
        _clear_host_reviews(tmp_path)
    return inventory


def _current_plan(tmp_path: Path, from_pin: str) -> tuple[dict, str, str]:
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    snapshot_hash = host_snapshot_content_hash(snapshot)
    plan = build_host_baseline_refresh_plan(
        from_revision=from_pin,
        to_revision=snapshot["public_commit"],
        host_snapshot=snapshot,
        previous_snapshot=None,
        snapshot_hash=snapshot_hash,
        generated_docs_hash=docs_hash,
    )
    return plan, snapshot_hash, docs_hash


def _recommit(tmp_path: Path, path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _ensure_fixture_git_commit(tmp_path, "mutate host review")


def test_parse_committed_review_filename_binds_source_pins_digest() -> None:
    source, from_rev, to_rev, digest = parse_committed_review_filename(
        "GROK_BUILD-"
        "7cfcb20d2b50b0d18801a6c0af2e401c0e060894-"
        "a5589e958437d79e13db026eedcb1720bffd4063-"
        "731c4c273f4539e587e56feb6693c7994efc22d3d816db04220eff1530be026a.json"
    )
    assert source == HOST_BASELINE_PIN_ID
    assert from_rev == FROM_PIN
    assert to_rev == FROZEN_PINS[HOST_BASELINE_PIN_ID]
    assert digest == "731c4c273f4539e587e56feb6693c7994efc22d3d816db04220eff1530be026a"


def test_parse_committed_review_filename_rejects_malformed() -> None:
    with pytest.raises(ContractValidationError, match="not canonical"):
        parse_committed_review_filename("GROK_BUILD-not-a-receipt.txt")


def test_valid_committed_current_receipt_happy_path(tmp_path: Path) -> None:
    inventory = _prepare_tree(tmp_path, write_review=False)
    path = _mint_current_receipt(tmp_path)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "valid current receipt")
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    bound = assert_host_review_binds_current_content(
        repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
    )
    assert bound == path
    payload = assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)
    assert payload["ok"] is True
    plan, snapshot_hash, docs = _current_plan(tmp_path, FROM_PIN)
    review = assert_canonical_immutable_host_review_receipt(
        repo_root=tmp_path,
        path=path,
        expected_source=HOST_BASELINE_PIN_ID,
        expected_from_revision=FROM_PIN,
        expected_to_revision=snapshot["public_commit"],
        expected_plan=plan,
        expected_snapshot_hash=snapshot_hash,
        expected_docs_hash=docs,
    )
    assert review["store_kind"] == "parity_refresh_review"
    assert review["schema_version"] == 1


def test_untracked_matching_receipt_never_authorizes(tmp_path: Path) -> None:
    inventory = _prepare_tree(tmp_path, write_review=False)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "base without review")
    _mint_current_receipt(tmp_path)
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="not tracked by git"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )
    with pytest.raises(ContractValidationError, match="not tracked by git"):
        assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)


def _committed_mutated(
    tmp_path: Path, mutator
) -> tuple[Path, dict, str, str]:
    _prepare_tree(tmp_path, write_review=False)
    path = _mint_current_receipt(tmp_path)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "valid receipt")
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutator(payload)
    _recommit(tmp_path, path, payload)
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    return path, payload, snapshot["public_commit"], docs_hash


def test_committed_wrong_store_kind_rejected(tmp_path: Path) -> None:
    _committed_mutated(tmp_path, lambda p: p.__setitem__("store_kind", "forged_review"))
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="store_kind"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_committed_bool_schema_version_rejected(tmp_path: Path) -> None:
    _committed_mutated(tmp_path, lambda p: p.__setitem__("schema_version", True))
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="schema_version"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_committed_wrong_source_rejected(tmp_path: Path) -> None:
    _committed_mutated(tmp_path, lambda p: p.__setitem__("source", "OMC"))
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="source"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_committed_wrong_from_to_previous_snapshot_path_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict) -> None:
        payload["from_revision"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        payload["host_baseline"]["previous_pin"] = payload["from_revision"]
        payload["host_baseline"]["snapshot_path"] = "docs/parity/forged.json"

    _committed_mutated(tmp_path, mutate)
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(
        ContractValidationError,
        match="from_revision|previous_pin|snapshot_path|filename",
    ):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_committed_wrong_change_digest_rejected(tmp_path: Path) -> None:
    _committed_mutated(tmp_path, lambda p: p.__setitem__("change_digest", "0" * 64))
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="change_digest"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_committed_malformed_changes_rejected(tmp_path: Path) -> None:
    _committed_mutated(tmp_path, lambda p: p.__setitem__("changes", ["not-an-object"]))
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="changes"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_committed_wrong_content_binding_digest_rejected(tmp_path: Path) -> None:
    _committed_mutated(
        tmp_path, lambda p: p.__setitem__("content_binding_digest", "a" * 64)
    )
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="content_binding_digest"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_committed_wrong_filename_digest_rejected(tmp_path: Path) -> None:
    inventory = _prepare_tree(tmp_path, write_review=False)
    path = _mint_current_receipt(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    source, from_rev, to_rev, _digest = parse_committed_review_filename(path.name)
    forged = path.with_name(f"{source}-{from_rev}-{to_rev}-{'b' * 64}.json")
    forged.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.unlink()
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "forged filename digest")
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(
        ContractValidationError,
        match="filename digest|content_binding_digest|binds current",
    ):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )
    with pytest.raises(ContractValidationError):
        assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)


def test_missing_acknowledgment_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict) -> None:
        if payload["changes"]:
            payload["changes"][0]["disposition"] = "noted"

    _committed_mutated(tmp_path, mutate)
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="acknowledgment"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_wrong_acknowledgment_detail_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict) -> None:
        if payload["changes"]:
            payload["changes"][0]["detail"] = {"fields": ["tampered"]}

    _committed_mutated(tmp_path, mutate)
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    with pytest.raises(ContractValidationError, match="change_digest|acknowledgment"):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )


def test_nested_hash_only_forged_receipt_rejected(tmp_path: Path) -> None:
    """HIGH reproduction: matching host_baseline hashes must not authorize."""
    inventory = _prepare_tree(tmp_path, write_review=False)
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    snapshot_hash = host_snapshot_content_hash(snapshot)
    pin = snapshot["public_commit"]
    reviews = tmp_path / "docs" / "parity" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    forged = reviews / f"GROK_BUILD-{FROM_PIN}-{pin}-{'c' * 64}.json"
    forged.write_text(
        json.dumps(
            {
                "store_kind": "parity_refresh_review",
                "schema_version": 1,
                "source": HOST_BASELINE_PIN_ID,
                "from_revision": FROM_PIN,
                "to_revision": pin,
                "change_digest": "d" * 64,
                "content_binding_digest": "c" * 64,
                "changes": [],
                "host_baseline": {
                    "snapshot_path": "docs/parity/upstream-snapshots/grok-build.json",
                    "snapshot_hash": snapshot_hash,
                    "generated_docs_hash": docs_hash,
                    "reviewed_pin": pin,
                    "previous_pin": FROM_PIN,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "forged nested-hash receipt")
    with pytest.raises(
        ContractValidationError,
        match="change_digest|content_binding_digest|acknowledgment",
    ):
        assert_host_review_binds_current_content(
            repo_root=tmp_path, snapshot=snapshot, docs_hash=docs_hash
        )
    with pytest.raises(ContractValidationError):
        assert_host_baseline_gate(inventory=inventory, repo_root=tmp_path)


def test_canonical_repo_current_receipt_validates() -> None:
    from omg_cli.parity_claim_gate import (
        assert_host_generated_docs_consistent,
        load_host_baseline_snapshot,
    )

    snapshot = load_host_baseline_snapshot(ROOT)
    docs_hash = assert_host_generated_docs_consistent(
        repo_root=ROOT, host_snapshot=snapshot
    )
    bound = assert_host_review_binds_current_content(
        repo_root=ROOT, snapshot=snapshot, docs_hash=docs_hash
    )
    assert bound.name.endswith(
        "731c4c273f4539e587e56feb6693c7994efc22d3d816db04220eff1530be026a.json"
    )


def test_legacy_change_digest_filename_does_not_authorize_current() -> None:
    from omg_cli.parity_claim_gate import (
        assert_host_generated_docs_consistent,
        load_host_baseline_snapshot,
    )

    snapshot = load_host_baseline_snapshot(ROOT)
    docs_hash = assert_host_generated_docs_consistent(
        repo_root=ROOT, host_snapshot=snapshot
    )
    bound = assert_host_review_binds_current_content(
        repo_root=ROOT, snapshot=snapshot, docs_hash=docs_hash
    )
    assert "81e709b16d44b7c162d757bce71a22ebdacadb21533d4f0ac7b9c691026c1d08" not in (
        bound.name
    )


def test_pin_transition_gate_still_requires_content_bound_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    new_pin = "cccccccccccccccccccccccccccccccccccccccc"
    base["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = OLD_PIN
    inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = new_pin
    monkeypatch.setitem(FROZEN_PINS, HOST_BASELINE_PIN_ID, new_pin)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    _write_host_baseline_snapshot(tmp_path, inventory, write_binding_review=True)
    snapshot = load_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    plan = build_host_baseline_refresh_plan(
        from_revision=OLD_PIN,
        to_revision=new_pin,
        host_snapshot=snapshot,
        previous_snapshot=None,
        generated_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
        snapshot_hash=host_snapshot_content_hash(snapshot),
        generated_docs_hash=docs_hash,
    )
    write_committed_host_baseline_review(tmp_path, plan)
    _ensure_fixture_git_commit(tmp_path, "pin transition receipt")
    payload = assert_host_baseline_gate(
        inventory=inventory,
        repo_root=tmp_path,
        base_inventory=base,
    )
    assert payload["ok"] is True
