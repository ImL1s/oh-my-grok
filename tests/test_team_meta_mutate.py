"""Hermetic tests for #21 locked atomic team.json / ref.json mutations."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from omg_cli.contracts.path_keys import DATA_FILE_MODE, mode_bits
from omg_cli.evidence import CLI_WRITER
from omg_cli.team.plane import (
    TeamError,
    _atomic_write_json,
    load_team_meta,
    mutate_team_meta,
    team_meta_lock_path,
    team_meta_path,
)
from omg_cli.team.runtime import write_team_ref


def _seed_team_meta(
    root: Path,
    run_id: str = "run-meta-1",
    *,
    extra: dict | None = None,
) -> dict:
    path = team_meta_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "writer": CLI_WRITER,
        "schema_version": 1,
        "meta_generation": 0,
        "run_id": run_id,
        "session": f"omg-{run_id}",
        "workspace_mode": "worktree",
        "created_at": "2026-07-28T00:00:00+00:00",
        "goal": "seed",
        "task_count": 0,
        "tasks": [],
        "note": "seed",
    }
    if extra:
        meta.update(extra)
    _atomic_write_json(path, meta)
    assert mode_bits(path) == DATA_FILE_MODE
    return meta


def test_mutate_team_meta_bumps_generation_and_mode(tmp_path: Path) -> None:
    run_id = "run-bump"
    _seed_team_meta(tmp_path, run_id)

    def mutator(current: dict) -> dict:
        current["note"] = "first-mutate"
        return current

    out = mutate_team_meta(tmp_path, run_id, mutator)
    assert out["meta_generation"] == 1
    assert out["note"] == "first-mutate"
    assert out["writer"] == CLI_WRITER
    assert "verified" not in out

    disk = load_team_meta(tmp_path, run_id)
    assert disk["meta_generation"] == 1
    assert mode_bits(team_meta_path(tmp_path, run_id)) == DATA_FILE_MODE
    assert team_meta_lock_path(tmp_path, run_id).is_file()


def test_mutate_team_meta_cas_conflict(tmp_path: Path) -> None:
    run_id = "run-cas"
    _seed_team_meta(tmp_path, run_id)

    mutate_team_meta(tmp_path, run_id, lambda c: {**c, "note": "g1"})
    assert load_team_meta(tmp_path, run_id)["meta_generation"] == 1

    with pytest.raises(TeamError, match="stale team meta generation"):
        mutate_team_meta(
            tmp_path,
            run_id,
            lambda c: {**c, "note": "stale"},
            expected_generation=0,
        )

    # Correct CAS still wins.
    out = mutate_team_meta(
        tmp_path,
        run_id,
        lambda c: {**c, "note": "g2"},
        expected_generation=1,
    )
    assert out["meta_generation"] == 2
    assert out["note"] == "g2"


def test_mutate_team_meta_introduces_generation_when_missing(tmp_path: Path) -> None:
    run_id = "run-legacy"
    path = team_meta_path(tmp_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "writer": CLI_WRITER,
        "schema_version": 1,
        "run_id": run_id,
        "session": "s",
        "workspace_mode": "worktree",
        "created_at": "2026-07-28T00:00:00+00:00",
        "tasks": [],
    }
    _atomic_write_json(path, legacy)
    assert "meta_generation" not in load_team_meta(tmp_path, run_id)

    out = mutate_team_meta(tmp_path, run_id, lambda c: {**c, "note": "adopt"})
    assert out["meta_generation"] == 1


def test_mutate_team_meta_rejects_immutable_identity_change(tmp_path: Path) -> None:
    run_id = "run-immut"
    _seed_team_meta(
        tmp_path,
        run_id,
        extra={
            "launch_nonce": "nonce-abc",
            "launch_receipt_sha256": "a" * 64,
        },
    )

    with pytest.raises(TeamError, match="immutable field 'launch_nonce'"):
        mutate_team_meta(
            tmp_path,
            run_id,
            lambda c: {**c, "launch_nonce": "tampered"},
        )

    with pytest.raises(TeamError, match="immutable field 'run_id'|run_id mismatch"):
        mutate_team_meta(
            tmp_path,
            run_id,
            lambda c: {**c, "run_id": "other-run"},
        )


def test_mutate_team_meta_strips_verified_passes(tmp_path: Path) -> None:
    run_id = "run-strip"
    _seed_team_meta(tmp_path, run_id)

    def mutator(current: dict) -> dict:
        current["verified"] = True
        current["passes"] = True
        current["note"] = "no-forged"
        return current

    out = mutate_team_meta(tmp_path, run_id, mutator)
    assert "verified" not in out
    assert "passes" not in out
    assert out["note"] == "no-forged"


def test_mutate_team_meta_concurrent_serialized_generation(tmp_path: Path) -> None:
    run_id = "run-conc"
    _seed_team_meta(tmp_path, run_id)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        def mutator(current: dict) -> dict:
            hits = list(current.get("hits") or [])
            hits.append(idx)
            current["hits"] = hits
            return current

        try:
            barrier.wait(timeout=5)
            mutate_team_meta(tmp_path, run_id, mutator)
        except BaseException as exc:  # noqa: BLE001 — collect for assertion
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    disk = load_team_meta(tmp_path, run_id)
    assert disk["meta_generation"] == 8
    assert sorted(disk["hits"]) == list(range(8))
    # Document remains valid JSON object (no truncation mid-write).
    raw = team_meta_path(tmp_path, run_id).read_text(encoding="utf-8")
    assert json.loads(raw)["meta_generation"] == 8


def test_mutate_team_meta_preserves_previous_on_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team import plane

    run_id = "run-preserve"
    _seed_team_meta(tmp_path, run_id)
    before = load_team_meta(tmp_path, run_id)

    def boom(*_a, **_k):
        raise plane.TeamError("secure team.json publication refused: simulated")

    # mutate_team_meta publishes via the pinned-descriptor helper.
    monkeypatch.setattr(plane, "_atomic_write_json_at", boom)

    with pytest.raises(TeamError, match="publication refused"):
        mutate_team_meta(tmp_path, run_id, lambda c: {**c, "note": "lost"})

    after = load_team_meta(tmp_path, run_id)
    assert after == before
    assert after["note"] == "seed"
    assert after["meta_generation"] == 0


def test_mutate_team_meta_rejects_non_cli_writer(tmp_path: Path) -> None:
    run_id = "run-forged"
    path = team_meta_path(tmp_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"writer": "agent", "run_id": run_id, "tasks": []}) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, DATA_FILE_MODE)
    with pytest.raises(TeamError, match="CLI writer"):
        mutate_team_meta(tmp_path, run_id, lambda c: c)


def test_write_team_ref_atomic_mode_and_idempotent(tmp_path: Path) -> None:
    path = write_team_ref(
        tmp_path, team_name="alpha-team", run_id="run-a", team_id="team"
    )
    assert path.is_file()
    assert mode_bits(path) == DATA_FILE_MODE
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["writer"] == CLI_WRITER
    assert data["run_id"] == "run-a"
    assert data["team_name"] == "alpha-team"

    # Idempotent same identity.
    path2 = write_team_ref(
        tmp_path, team_name="alpha-team", run_id="run-a", team_id="team"
    )
    assert path2 == path
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run-a"


def test_write_team_ref_rejects_identity_conflict(tmp_path: Path) -> None:
    write_team_ref(tmp_path, team_name="beta-team", run_id="run-1", team_id="team")
    with pytest.raises(TeamError, match="identity conflict"):
        write_team_ref(
            tmp_path, team_name="beta-team", run_id="run-2", team_id="team"
        )


def test_write_team_ref_rejects_symlink_parent(tmp_path: Path) -> None:
    """Managed path under a symlink component must fail closed (#16 confinement)."""
    from omg_cli.contracts.path_keys import ContractPathError
    from omg_cli.contracts.state_schemas import require_safe_id
    from omg_cli.contracts.path_keys import safe_path_key

    # Build a hostile layout: .omg/state/team is a symlink outside.
    outside = tmp_path / "outside"
    outside.mkdir()
    omg = tmp_path / ".omg"
    (omg / "state").mkdir(parents=True)
    (omg / "state" / "team").symlink_to(outside, target_is_directory=True)

    name = require_safe_id("evil-team", label="team_name")
    # write_team_ref goes through ensure_managed_dir / atomic_write_bytes.
    with pytest.raises((TeamError, ContractPathError, OSError)):
        write_team_ref(tmp_path, team_name=name, run_id="run-x", team_id="team")

    # Outside must not have been written as team ref.
    key = safe_path_key(name, namespace="team")
    assert not (outside / key / "ref.json").is_file()


def test_mutate_rejects_symlink_team_json(tmp_path: Path) -> None:
    run_id = "run-symlink"
    tdir = team_meta_path(tmp_path, run_id).parent
    tdir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.json"
    victim.write_text('{"writer":"omg-cli"}', encoding="utf-8")
    os.chmod(victim, DATA_FILE_MODE)
    team_meta_path(tmp_path, run_id).symlink_to(victim)

    with pytest.raises(TeamError):
        mutate_team_meta(tmp_path, run_id, lambda c: c)
