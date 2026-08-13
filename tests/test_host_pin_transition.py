"""GROK_BUILD pin-transition host-baseline review enforcement (#105 PR1)."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
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
    _ensure_fixture_git_commit,
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


def _commit_host_review(
    tmp_path: Path,
    *,
    from_pin: str,
    to_pin: str,
    previous_snapshot: dict | None = None,
) -> Path:
    snapshot = load_host_baseline_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    plan = build_host_baseline_refresh_plan(
        from_revision=from_pin,
        to_revision=to_pin,
        host_snapshot=snapshot,
        previous_snapshot=previous_snapshot,
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
    _ensure_fixture_git_commit(tmp_path, "commit current host review")
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


def _prepare_pin_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, dict, Path]:
    inventory = _bootstrapping_inventory(tmp_path)
    base = copy.deepcopy(inventory)
    base["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = OLD_PIN
    inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = NEW_PIN
    monkeypatch.setitem(FROZEN_PINS, HOST_BASELINE_PIN_ID, NEW_PIN)
    _write_inventory(tmp_path, inventory)
    _scaffold_inventory_paths(tmp_path, inventory)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, inventory)
    path = _commit_host_review(tmp_path, from_pin=OLD_PIN, to_pin=NEW_PIN)
    _init_git_repo(tmp_path)
    _git_commit_all(tmp_path, "commit host review")
    return inventory, base, path


def _recommit_review(tmp_path: Path, path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _ensure_fixture_git_commit(tmp_path, "mutate pin-transition review")


def _expected_content_binding(
    tmp_path: Path,
    *,
    from_pin: str,
    to_pin: str,
    previous_snapshot: dict | None = None,
) -> tuple[str, str, Path]:
    snapshot = load_host_baseline_snapshot(tmp_path)
    docs_hash = generated_docs_content_hash(tmp_path, snapshot["generated"]["docs"])
    plan = build_host_baseline_refresh_plan(
        from_revision=from_pin,
        to_revision=to_pin,
        host_snapshot=snapshot,
        previous_snapshot=previous_snapshot,
        snapshot_hash=host_snapshot_content_hash(snapshot),
        generated_docs_hash=docs_hash,
    )
    changes_digest = canonical_changes_digest(
        [c for c in plan.get("changes", []) if isinstance(c, dict)]
    )
    identity = host_baseline_receipt_digest(
        change_digest=changes_digest,
        snapshot_hash=str(plan["host_baseline"]["snapshot_hash"]),
        generated_docs_hash=docs_hash,
    )
    path = committed_review_path(
        tmp_path,
        source=HOST_BASELINE_PIN_ID,
        from_revision=from_pin,
        to_revision=to_pin,
        change_digest=identity,
    )
    return changes_digest, identity, path


def _commit_c0_old_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, str]:
    inventory = _bootstrapping_inventory(tmp_path)
    monkeypatch.setitem(FROZEN_PINS, HOST_BASELINE_PIN_ID, NEW_PIN)
    c0 = copy.deepcopy(inventory)
    c0["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = OLD_PIN
    _write_inventory(tmp_path, c0)
    _scaffold_inventory_paths(tmp_path, c0)
    _honest_docs(tmp_path)
    _write_required_snapshots(tmp_path, c0)
    reviews = tmp_path / "docs" / "parity" / "reviews"
    if reviews.is_dir():
        for leftover in reviews.glob("GROK_BUILD-*.json"):
            leftover.unlink()
    _write_host_baseline_snapshot(tmp_path, c0, write_binding_review=False)
    _init_git_repo(tmp_path)
    c0_sha = _git_commit_all(tmp_path, "C0 old GROK_BUILD pin")
    return c0, c0_sha


def test_to_ref_dag_happy_path_with_content_bound_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c0, c0_sha = _commit_c0_old_pin(tmp_path, monkeypatch)
    c0_snapshot = copy.deepcopy(load_host_baseline_snapshot(tmp_path))
    c1 = copy.deepcopy(c0)
    c1["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = NEW_PIN
    _write_inventory(tmp_path, c1)
    _write_host_baseline_snapshot(tmp_path, c1, write_binding_review=True)
    _commit_host_review(
        tmp_path,
        from_pin=OLD_PIN,
        to_pin=NEW_PIN,
        previous_snapshot=c0_snapshot,
    )
    _git_commit_all(tmp_path, "C1 new pin + content-bound receipt")
    payload = assert_host_baseline_gate(
        inventory=c1,
        repo_root=tmp_path,
        base_inventory=c0,
        base_ref=c0_sha,
    )
    assert payload["ok"] is True


def test_to_ref_noncanonical_docs_list_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c0, c0_sha = _commit_c0_old_pin(tmp_path, monkeypatch)
    c1 = copy.deepcopy(c0)
    c1["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = NEW_PIN
    _write_inventory(tmp_path, c1)
    _write_host_baseline_snapshot(tmp_path, c1, write_binding_review=False)
    snap_path = tmp_path / "docs" / "parity" / "upstream-snapshots" / "grok-build.json"
    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    snapshot["generated"]["docs"] = ["docs/evil.md"]
    snap_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    _git_commit_all(tmp_path, "C1 noncanonical generated.docs")

    # Repair HEAD so current-content would pass; historical C1 must still fail.
    _write_host_baseline_snapshot(tmp_path, c1, write_binding_review=True)
    _commit_host_review(tmp_path, from_pin=OLD_PIN, to_pin=NEW_PIN)
    _git_commit_all(tmp_path, "C2 repair + self-declared receipt")

    with pytest.raises(
        ContractValidationError,
        match="generated.docs must be exactly",
    ):
        assert_host_baseline_gate(
            inventory=c1,
            repo_root=tmp_path,
            base_inventory=c0,
            base_ref=c0_sha,
        )


def test_to_ref_self_declared_docs_hash_does_not_authorize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c0, c0_sha = _commit_c0_old_pin(tmp_path, monkeypatch)
    c0_snapshot = copy.deepcopy(load_host_baseline_snapshot(tmp_path))
    c1 = copy.deepcopy(c0)
    c1["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = NEW_PIN
    _write_inventory(tmp_path, c1)
    _write_host_baseline_snapshot(tmp_path, c1, write_binding_review=True)
    snapshot = load_host_baseline_snapshot(tmp_path)
    fake_docs = "0" * 64
    plan = build_host_baseline_refresh_plan(
        from_revision=OLD_PIN,
        to_revision=NEW_PIN,
        host_snapshot=snapshot,
        previous_snapshot=c0_snapshot,
        generated_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
        snapshot_hash=host_snapshot_content_hash(snapshot),
        generated_docs_hash=fake_docs,
    )
    write_committed_host_baseline_review(tmp_path, plan)
    _git_commit_all(tmp_path, "C1 self-declared docs hash receipt only")

    _changes, identity, expected = _expected_content_binding(
        tmp_path,
        from_pin=OLD_PIN,
        to_pin=NEW_PIN,
        previous_snapshot=c0_snapshot,
    )
    with pytest.raises(
        ContractValidationError,
        match="GROK_BUILD pin transition missing committed host baseline review",
    ) as exc:
        assert_host_baseline_gate(
            inventory=c1,
            repo_root=tmp_path,
            base_inventory=c0,
            base_ref=c0_sha,
        )
    message = str(exc.value)
    assert identity in message
    assert expected.name in message
    assert f"{fake_docs}.json" not in message


def test_sole_legacy_change_digest_candidate_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    changes_digest = canonical_changes_digest(payload["changes"])
    identity = payload["content_binding_digest"]
    legacy = path.with_name(
        f"{HOST_BASELINE_PIN_ID}-{OLD_PIN}-{NEW_PIN}-{changes_digest}.json"
    )
    legacy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    _ensure_fixture_git_commit(tmp_path, "leave only legacy change-digest receipt")

    with pytest.raises(
        ContractValidationError,
        match="GROK_BUILD pin transition missing committed host baseline review",
    ) as exc:
        assert_host_baseline_gate(
            inventory=inventory,
            repo_root=tmp_path,
            base_inventory=base,
        )
    message = str(exc.value)
    assert identity in message
    assert f"{identity}.json" in message
    assert f"{changes_digest}.json" not in message


def test_untracked_victim_receipt_hostile_git_dir_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = tmp_path / "victim"
    foreign = tmp_path / "foreign"
    victim.mkdir()
    inventory = _bootstrapping_inventory(victim)
    base = copy.deepcopy(inventory)
    base["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = OLD_PIN
    inventory["upstream_pins"][HOST_BASELINE_PIN_ID]["revision"] = NEW_PIN
    monkeypatch.setitem(FROZEN_PINS, HOST_BASELINE_PIN_ID, NEW_PIN)
    _write_inventory(victim, inventory)
    _scaffold_inventory_paths(victim, inventory)
    _honest_docs(victim)
    _write_required_snapshots(victim, inventory)
    _init_git_repo(victim)
    _git_commit_all(victim, "victim without review")
    _commit_host_review(victim, from_pin=OLD_PIN, to_pin=NEW_PIN)

    shutil.copytree(victim, foreign, ignore=shutil.ignore_patterns(".git"))
    _init_git_repo(foreign)
    _git_commit_all(foreign, "foreign committed copy of victim receipt")

    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(victim))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(foreign / ".git" / "objects"))
    monkeypatch.setenv(
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", str(foreign / ".git" / "objects")
    )

    with pytest.raises(ContractValidationError, match=r"not tracked by git"):
        assert_host_baseline_gate(
            inventory=inventory,
            repo_root=victim,
            base_inventory=base,
        )


def test_replace_object_attack_does_not_authorize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    valid = path.read_bytes()
    payload = json.loads(valid.decode("utf-8"))
    payload["store_kind"] = "forged_review"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _ensure_fixture_git_commit(tmp_path, "commit invalid store_kind")

    ls = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-tree", "HEAD", "--", path.relative_to(tmp_path).as_posix()],
        check=True,
        capture_output=True,
        text=True,
    )
    invalid_blob = ls.stdout.strip().split("\t", 1)[0].split()[2]
    hashed = subprocess.run(
        ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
        input=valid,
        check=True,
        capture_output=True,
    )
    valid_blob = hashed.stdout.decode("ascii").strip()
    subprocess.run(
        ["git", "-C", str(tmp_path), "replace", invalid_blob, valid_blob],
        check=True,
        capture_output=True,
        text=True,
    )
    path.write_bytes(valid)

    with pytest.raises(
        ContractValidationError,
        match="differs from HEAD blob|store_kind",
    ):
        assert_host_baseline_gate(
            inventory=inventory,
            repo_root=tmp_path,
            base_inventory=base,
        )


def test_claim_gate_git_env_drops_repository_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.parity_claim_gate import _claim_gate_git_env

    monkeypatch.setenv("GIT_DIR", "/tmp/evil.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/evil-wt")
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/evil.index")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/evil-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/evil-alt")
    monkeypatch.setenv("GIT_COMMON_DIR", "/tmp/evil-common")
    env = _claim_gate_git_env()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    ):
        assert key not in env
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_pin_transition_one_field_store_kind_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["store_kind"] = "forged_review"
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="store_kind"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )


def test_pin_transition_one_field_bool_schema_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = True
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="schema_version"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )


def test_pin_transition_one_field_source_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source"] = "OMC"
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="source"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )


def test_pin_transition_one_field_from_revision_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["from_revision"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="from_revision"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )


def test_pin_transition_one_field_snapshot_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["host_baseline"]["snapshot_path"] = "docs/parity/forged.json"
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="snapshot_path"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )


def test_pin_transition_one_field_previous_pin_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["host_baseline"]["previous_pin"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="previous_pin"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )


def test_pin_transition_one_field_change_digest_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["change_digest"] = "0" * 64
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="change_digest"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )


def test_pin_transition_one_field_content_binding_digest_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["content_binding_digest"] = "a" * 64
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="content_binding_digest"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )


def test_pin_transition_one_field_generated_docs_hash_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, base, path = _prepare_pin_transition(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["host_baseline"]["generated_docs_hash"] = "f" * 64
    _recommit_review(tmp_path, path, payload)
    with pytest.raises(ContractValidationError, match="generated_docs_hash"):
        assert_host_baseline_gate(
            inventory=inventory, repo_root=tmp_path, base_inventory=base
        )
