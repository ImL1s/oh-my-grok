from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.integrate import integrate_native_team_delivery
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex
from omg_cli.team.plane import (
    TeamError,
    create_native_team,
    prepare_native_spawn,
    reconcile_native_spawn,
)
from omg_cli.team.recovery import recover_native_task
from omg_cli.team.worktree import (
    TeamWorktreeError,
    cancel_owned_worktree,
    cleanup_owned_worktree,
    load_worktree_receipt,
    worktree_receipt_path,
)
from omg_cli.team import worktree as worktree_module
from omg_cli.workers import (
    prepare_native_team_worktree,
    seal_native_team_worktree,
)


def _git(
    root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "OMG Tests")
    _git(root, "config", "user.email", "omg@example.invalid")
    for name, body in (files or {"owned.txt": "base\n"}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD").stdout.strip()


def _prepare(
    root: Path, base: str, *, task: str = "task-1", owned: list[str] | None = None
):
    return prepare_native_team_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id=task,
        generation=0,
        base_sha=base,
        owned_paths=owned or ["owned.txt"],
    )


def test_owned_worktree_seal_integrate_fresh_test_and_cleanup(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    receipt = _prepare(root, base)
    assert _prepare(root, base) == receipt
    worktree = Path(receipt["worktree_path"])
    (worktree / "owned.txt").write_text("worker result\n", encoding="utf-8")

    sealed = seal_native_team_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="task-1",
        generation=0,
        verification_commands=[["git", "diff", "--check", "HEAD^", "HEAD"]],
    )
    assert sealed["duplicate"] is False
    assert (
        seal_native_team_worktree(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="task-1",
            generation=0,
        )["duplicate"]
        is True
    )

    integrated = integrate_native_team_delivery(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="task-1",
        generation=0,
        delivery_hash=sealed["delivery_hash"],
        post_integration_commands=[["git", "diff", "--check", "HEAD^", "HEAD"]],
    )
    assert integrated["status"] == "integrated"
    assert integrated["duplicate"] is False
    assert (root / "owned.txt").read_text(encoding="utf-8") == "worker result\n"
    assert (
        integrate_native_team_delivery(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="task-1",
            generation=0,
            delivery_hash=sealed["delivery_hash"],
        )["duplicate"]
        is True
    )

    cleaned = cleanup_owned_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="task-1",
        generation=0,
    )
    assert cleaned["state"] == "cleaned"
    assert not worktree.exists()


def test_worktree_rejects_dirty_base_authority_paths_and_unowned_changes(
    tmp_path: Path,
) -> None:
    root, base = _repo(tmp_path, {"owned.txt": "a\n", "foreign.txt": "b\n"})
    with pytest.raises(TeamWorktreeError, match="canonical authority"):
        _prepare(root, base, task="authority", owned=[".omg/state"])

    (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(TeamWorktreeError, match="dirty"):
        _prepare(root, base, task="dirty")
    (root / "dirty.txt").unlink()

    receipt = _prepare(root, base, task="unowned")
    worktree = Path(receipt["worktree_path"])
    (worktree / "foreign.txt").write_text("not owned\n", encoding="utf-8")
    with pytest.raises(TeamWorktreeError, match="unowned"):
        seal_native_team_worktree(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="unowned",
            generation=0,
        )
    cancel_owned_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="unowned",
        generation=0,
    )


def test_integration_conflict_blocks_and_preserves_receipt(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    receipt = _prepare(root, base, task="conflict")
    worktree = Path(receipt["worktree_path"])
    (worktree / "owned.txt").write_text("worker\n", encoding="utf-8")
    sealed = seal_native_team_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="conflict",
        generation=0,
    )

    (root / "owned.txt").write_text("leader\n", encoding="utf-8")
    _git(root, "add", "owned.txt")
    _git(root, "commit", "-m", "leader conflict")
    outcome = integrate_native_team_delivery(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="conflict",
        generation=0,
        delivery_hash=sealed["delivery_hash"],
    )
    assert outcome["status"] == "conflict"
    assert (
        load_worktree_receipt(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="conflict",
        )["state"]
        == "conflict"
    )


def test_failed_post_integration_test_rolls_back_leader(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    receipt = _prepare(root, base, task="post-test")
    (Path(receipt["worktree_path"]) / "owned.txt").write_text(
        "candidate\n", encoding="utf-8"
    )
    sealed = seal_native_team_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="post-test",
        generation=0,
    )
    with pytest.raises(TeamWorktreeError, match="verification failed"):
        integrate_native_team_delivery(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="post-test",
            generation=0,
            delivery_hash=sealed["delivery_hash"],
            post_integration_commands=[["false"]],
        )
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == base
    assert (root / "owned.txt").read_text(encoding="utf-8") == "base\n"
    assert (
        load_worktree_receipt(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="post-test",
        )["state"]
        == "sealed"
    )


def test_repository_global_integration_lock_prevents_failed_rollback_erasing_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, base = _repo(tmp_path, {"a.txt": "base-a\n", "b.txt": "base-b\n"})
    failed_receipt = _prepare(root, base, task="failed", owned=["a.txt"])
    successful_receipt = _prepare(root, base, task="successful", owned=["b.txt"])
    (Path(failed_receipt["worktree_path"]) / "a.txt").write_text(
        "failed-candidate\n", encoding="utf-8"
    )
    (Path(successful_receipt["worktree_path"]) / "b.txt").write_text(
        "successful\n", encoding="utf-8"
    )
    failed = seal_native_team_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="failed",
        generation=0,
    )
    successful = seal_native_team_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="successful",
        generation=0,
    )
    verification_entered = threading.Event()
    release_failure = threading.Event()
    original_verify = worktree_module._run_verification

    def gated_verify(cwd: Path, commands, *, timeout: int = 300):
        if commands == [["false"]]:
            verification_entered.set()
            assert release_failure.wait(3.0)
            raise TeamWorktreeError("verification failed rc=1: ['false']")
        return original_verify(cwd, commands, timeout=timeout)

    monkeypatch.setattr(worktree_module, "_run_verification", gated_verify)
    outcomes: dict[str, object] = {}

    def run_failed() -> None:
        try:
            integrate_native_team_delivery(
                root,
                run_id="run-worktree",
                team_id="team-worktree",
                task_id="failed",
                generation=0,
                delivery_hash=failed["delivery_hash"],
                post_integration_commands=[["false"]],
            )
        except TeamWorktreeError as exc:
            outcomes["failed"] = exc

    def run_successful() -> None:
        outcomes["successful"] = integrate_native_team_delivery(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="successful",
            generation=0,
            delivery_hash=successful["delivery_hash"],
        )

    failed_thread = threading.Thread(target=run_failed)
    successful_thread = threading.Thread(target=run_successful)
    failed_thread.start()
    assert verification_entered.wait(3.0)
    successful_thread.start()
    time.sleep(0.1)
    assert successful_thread.is_alive(), "second integration bypassed repository lock"
    release_failure.set()
    failed_thread.join(5.0)
    successful_thread.join(5.0)

    assert isinstance(outcomes.get("failed"), TeamWorktreeError)
    assert outcomes["successful"]["status"] == "integrated"  # type: ignore[index]
    assert (root / "a.txt").read_text(encoding="utf-8") == "base-a\n"
    assert (root / "b.txt").read_text(encoding="utf-8") == "successful\n"


def test_integration_rejects_dirty_leader_and_receipt_path_tampering(
    tmp_path: Path,
) -> None:
    root, base = _repo(tmp_path)
    receipt = _prepare(root, base, task="tamper")
    (Path(receipt["worktree_path"]) / "owned.txt").write_text(
        "candidate\n", encoding="utf-8"
    )
    sealed = seal_native_team_worktree(
        root,
        run_id="run-worktree",
        team_id="team-worktree",
        task_id="tamper",
        generation=0,
    )
    (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(TeamWorktreeError, match="dirty"):
        integrate_native_team_delivery(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="tamper",
            generation=0,
            delivery_hash=sealed["delivery_hash"],
        )
    (root / "dirty.txt").unlink()

    path = worktree_receipt_path(root, "run-worktree", "team-worktree", "tamper")
    tampered = dict(
        load_worktree_receipt(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="tamper",
        )
    )
    tampered["worktree_path"] = "/tmp/not-the-owned-worktree"
    tampered["worktree_path_hash"] = sha256_hex(
        str(Path(tampered["worktree_path"]).resolve()).encode("utf-8")
    )
    path.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(TeamWorktreeError, match="exact allocation"):
        load_worktree_receipt(
            root,
            run_id="run-worktree",
            team_id="team-worktree",
            task_id="tamper",
        )


def test_read_write_native_spawn_requires_the_exact_receipted_worktree(
    tmp_path: Path,
) -> None:
    root, base = _repo(tmp_path)
    create_native_team(
        root,
        run_id="run-write-spawn",
        team_id="team-write-spawn",
        leader_id="leader",
        parent_session_id="parent-session",
        base_sha=base,
        tasks=[
            {
                "task_id": "writer",
                "role": "executor",
                "write_scope": ["owned.txt"],
                "prompt": "write the owned file",
            }
        ],
    )
    with pytest.raises(TeamError, match="exact owned worktree"):
        prepare_native_spawn(
            root,
            run_id="run-write-spawn",
            team_id="team-write-spawn",
            task_id="writer",
            expected_sequence=0,
            expected_generation=0,
            lease_generation=0,
            description="write owned file",
            expires_at="2099-01-01T00:00:00Z",
        )
    receipt = prepare_native_team_worktree(
        root,
        run_id="run-write-spawn",
        team_id="team-write-spawn",
        task_id="writer",
        generation=0,
        base_sha=base,
        owned_paths=["owned.txt"],
    )
    prepared = prepare_native_spawn(
        root,
        run_id="run-write-spawn",
        team_id="team-write-spawn",
        task_id="writer",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="write owned file",
        worktree=receipt["worktree_path"],
        expires_at="2099-01-01T00:00:00Z",
    )
    assert prepared["invocation"]["tool_input"]["cwd"] == receipt["worktree_path"]
    assert prepared["invocation"]["tool_input"]["capability_mode"] == "read-write"


def test_stale_write_worker_cancels_and_recreates_worktree_at_generation_plus_one(
    tmp_path: Path,
) -> None:
    root, base = _repo(tmp_path)
    create_native_team(
        root,
        run_id="run-write-recovery",
        team_id="team-write-recovery",
        leader_id="leader",
        parent_session_id="parent-session",
        base_sha=base,
        tasks=[
            {
                "task_id": "writer",
                "role": "executor",
                "write_scope": ["owned.txt"],
                "prompt": "write the owned file",
            }
        ],
    )
    first_worktree = prepare_native_team_worktree(
        root,
        run_id="run-write-recovery",
        team_id="team-write-recovery",
        task_id="writer",
        generation=0,
        base_sha=base,
        owned_paths=["owned.txt"],
    )
    prepared = prepare_native_spawn(
        root,
        run_id="run-write-recovery",
        team_id="team-write-recovery",
        task_id="writer",
        expected_sequence=0,
        expected_generation=0,
        lease_generation=0,
        description="write owned file",
        worktree=first_worktree["worktree_path"],
        expires_at="2099-01-01T00:00:00Z",
    )
    pair = prepared["receipt_pair"]
    t0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
    reconcile_native_spawn(
        root,
        run_id="run-write-recovery",
        team_id="team-write-recovery",
        task_id="writer",
        expected_state="spawn_requested",
        expected_sequence=1,
        expected_generation=0,
        now=t0,
        inventory=[
            {
                "spawn_receipt_hash": pair["spawn_receipt_hash"],
                "role_receipt_hash": pair["role_receipt_hash"],
                "run_id": "run-write-recovery",
                "task_id": "writer",
                "parent_id": "leader",
                "host_spawn_id": "host-writer-1",
                "observed_session_id": "session-writer-1",
            }
        ],
    )
    recovered = recover_native_task(
        root,
        run_id="run-write-recovery",
        team_id="team-write-recovery",
        task_id="writer",
        expected_state="running",
        expected_sequence=2,
        expected_generation=0,
        now=t0 + timedelta(seconds=400),
    )
    assert (recovered["state"], recovered["sequence"], recovered["generation"]) == (
        "ready",
        3,
        1,
    )
    assert not Path(first_worktree["worktree_path"]).exists()
    assert (
        load_worktree_receipt(
            root,
            run_id="run-write-recovery",
            team_id="team-write-recovery",
            task_id="writer",
        )["state"]
        == "cancelled"
    )

    second_worktree = prepare_native_team_worktree(
        root,
        run_id="run-write-recovery",
        team_id="team-write-recovery",
        task_id="writer",
        generation=1,
        base_sha=base,
        owned_paths=["owned.txt"],
    )
    assert second_worktree["generation"] == 1
    retried = prepare_native_spawn(
        root,
        run_id="run-write-recovery",
        team_id="team-write-recovery",
        task_id="writer",
        expected_sequence=3,
        expected_generation=1,
        lease_generation=1,
        description="retry owned write",
        worktree=second_worktree["worktree_path"],
        expires_at="2099-01-01T00:00:00Z",
    )
    assert retried["task"]["generation"] == 1
    assert retried["task"]["receipt_id"] != prepared["task"]["receipt_id"]
