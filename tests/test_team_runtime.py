"""Hermetic dry-run coverage for shorthand ``launch_team``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS
from omg_cli.team import runtime, scaling
from omg_cli.team.runtime import launch_team, resolve_team_ref, team_ref_path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


@pytest.mark.parametrize(
    ("pending_operation", "expected_order"),
    [
        ("relaunch", ["relaunch", "resume", "claims"]),
        (None, ["resume", "relaunch", "claims"]),
    ],
)
def test_resume_for_identity_uses_one_lock_and_operation_aware_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_operation: str | None,
    expected_order: list[str],
) -> None:
    events: list[str] = []
    lock_held = False

    class LifecycleLock:
        def __enter__(self) -> None:
            nonlocal lock_held
            assert not lock_held
            lock_held = True
            events.append("lock-enter")

        def __exit__(self, *_args: object) -> None:
            nonlocal lock_held
            assert lock_held
            events.append("lock-exit")
            lock_held = False

    monkeypatch.setattr(runtime, "resolve_team_ref", lambda *_args: "run-1")
    monkeypatch.setattr(
        runtime,
        "load_team_meta",
        lambda *_args: {"identity_generation": 0, "team_id": "team"},
    )
    monkeypatch.setattr(
        scaling,
        "acquire_scale_lock",
        lambda *_args: LifecycleLock(),
    )
    monkeypatch.setattr(
        scaling,
        "pending_identity_wal_operation",
        lambda *_args: pending_operation,
    )

    def recover(*_args: object, **_kwargs: object) -> dict[str, object]:
        assert lock_held
        events.append("relaunch")
        return {
            "relaunched": [{"task_id": "w2"}],
            "blocked": [],
            "skipped": [],
            "identity_generation": 1,
            "note": "relaunch recovered",
        }

    def reconcile(*_args: object, **_kwargs: object) -> dict[str, object]:
        assert lock_held
        events.append("resume")
        return {
            "identity_generation": 1,
            "note": "resume reconciled",
        }

    def claims(*_args: object, **_kwargs: object) -> dict[str, object]:
        assert lock_held
        events.append("claims")
        return {
            "status": "not_materialized",
            "scanned": 0,
            "preserved_unexpired": [],
            "released_expired": [],
            "unchanged": [],
        }

    monkeypatch.setattr(
        scaling,
        "_relaunch_dead_incomplete_workers_locked",
        recover,
    )
    monkeypatch.setattr(scaling, "_resume_team_locked_impl", reconcile)
    monkeypatch.setattr(
        "omg_cli.team.api.reconcile_task_claims",
        claims,
    )

    out = runtime.resume_for_identity(tmp_path, "team-name")

    assert events == ["lock-enter", *expected_order, "lock-exit"]
    assert out["identity_generation"] == 1
    assert out["relaunched"] == [{"task_id": "w2"}]
    assert out["claim_reconcile"]["status"] == "not_materialized"


def test_resume_for_identity_reconciles_claims_inside_existing_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_held = False
    saw_claims_under_lock = False

    class LifecycleLock:
        def __enter__(self) -> None:
            nonlocal lock_held
            lock_held = True
            return None

        def __exit__(self, *_args: object) -> None:
            nonlocal lock_held
            lock_held = False

    monkeypatch.setattr(runtime, "resolve_team_ref", lambda *_args: "run-1")
    monkeypatch.setattr(
        runtime,
        "load_team_meta",
        lambda *_args: {"identity_generation": 0, "team_id": "team"},
    )
    monkeypatch.setattr(
        scaling,
        "acquire_scale_lock",
        lambda *_args: LifecycleLock(),
    )
    monkeypatch.setattr(
        scaling,
        "pending_identity_wal_operation",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        scaling,
        "_resume_team_locked_impl",
        lambda *_a, **_k: {"identity_generation": 0, "note": "ok"},
    )
    monkeypatch.setattr(
        scaling,
        "_relaunch_dead_incomplete_workers_locked",
        lambda *_a, **_k: {
            "relaunched": [],
            "blocked": [],
            "skipped": [],
            "identity_generation": 0,
            "note": "none",
        },
    )

    def claims(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal saw_claims_under_lock
        saw_claims_under_lock = lock_held
        return {
            "status": "ok",
            "scanned": 0,
            "preserved_unexpired": [],
            "released_expired": [],
            "unchanged": [],
        }

    monkeypatch.setattr("omg_cli.team.api.reconcile_task_claims", claims)
    runtime.resume_for_identity(tmp_path, "team-name")
    assert saw_claims_under_lock is True


def test_resume_for_identity_preserves_operation_aware_wal_order_before_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class LifecycleLock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(runtime, "resolve_team_ref", lambda *_args: "run-1")
    monkeypatch.setattr(
        runtime,
        "load_team_meta",
        lambda *_args: {"identity_generation": 1, "team_id": "team"},
    )
    monkeypatch.setattr(
        scaling,
        "acquire_scale_lock",
        lambda *_args: LifecycleLock(),
    )
    monkeypatch.setattr(
        scaling,
        "pending_identity_wal_operation",
        lambda *_args: "relaunch",
    )
    monkeypatch.setattr(
        scaling,
        "_relaunch_dead_incomplete_workers_locked",
        lambda *_a, **_k: (
            events.append("relaunch"),
            {
                "relaunched": [],
                "blocked": [],
                "skipped": [],
                "identity_generation": 1,
                "note": "wal",
            },
        )[1],
    )
    monkeypatch.setattr(
        scaling,
        "_resume_team_locked_impl",
        lambda *_a, **_k: (
            events.append("resume"),
            {"identity_generation": 1, "note": "ok"},
        )[1],
    )
    monkeypatch.setattr(
        "omg_cli.team.api.reconcile_task_claims",
        lambda *_a, **_k: (
            events.append("claims"),
            {
                "status": "ok",
                "scanned": 0,
                "preserved_unexpired": [],
                "released_expired": [],
                "unchanged": [],
            },
        )[1],
    )
    runtime.resume_for_identity(tmp_path, "x")
    assert events == ["relaunch", "resume", "claims"]


def test_resume_for_identity_reports_claim_reconcile_additively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LifecycleLock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(runtime, "resolve_team_ref", lambda *_args: "run-1")
    monkeypatch.setattr(
        runtime,
        "load_team_meta",
        lambda *_args: {"identity_generation": 0, "team_id": "team"},
    )
    monkeypatch.setattr(
        scaling,
        "acquire_scale_lock",
        lambda *_args: LifecycleLock(),
    )
    monkeypatch.setattr(
        scaling,
        "pending_identity_wal_operation",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        scaling,
        "_resume_team_locked_impl",
        lambda *_a, **_k: {"identity_generation": 2, "note": "pane ok"},
    )
    monkeypatch.setattr(
        scaling,
        "_relaunch_dead_incomplete_workers_locked",
        lambda *_a, **_k: {
            "relaunched": [{"task_id": "w1"}],
            "blocked": [{"task_id": "w2"}],
            "skipped": [{"task_id": "w3"}],
            "identity_generation": 3,
            "note": "relaunched one",
        },
    )
    claim_obj = {
        "status": "ok",
        "scanned": 2,
        "preserved_unexpired": ["1"],
        "released_expired": ["2"],
        "unchanged": [],
    }
    monkeypatch.setattr(
        "omg_cli.team.api.reconcile_task_claims",
        lambda *_a, **_k: claim_obj,
    )
    out = runtime.resume_for_identity(tmp_path, "team-name")
    assert out["claim_reconcile"] == claim_obj
    assert out["relaunched"] == [{"task_id": "w1"}]
    assert out["blocked"] == [{"task_id": "w2"}]
    assert out["skipped"] == [{"task_id": "w3"}]
    # Claims stay separate from process relaunch fields.
    assert out["claim_reconcile"] is not out["relaunched"]
    assert "preserved_unexpired" not in out["relaunched"][0]
    assert "released_expired" not in (out["blocked"][0] if out["blocked"] else {})


def test_resume_for_identity_claim_corruption_exits_without_claim_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team.api import TeamApiError

    class LifecycleLock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(runtime, "resolve_team_ref", lambda *_args: "run-1")
    monkeypatch.setattr(
        runtime,
        "load_team_meta",
        lambda *_args: {"identity_generation": 0, "team_id": "team"},
    )
    monkeypatch.setattr(
        scaling,
        "acquire_scale_lock",
        lambda *_args: LifecycleLock(),
    )
    monkeypatch.setattr(
        scaling,
        "pending_identity_wal_operation",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        scaling,
        "_resume_team_locked_impl",
        lambda *_a, **_k: {"identity_generation": 0, "note": "ok"},
    )
    monkeypatch.setattr(
        scaling,
        "_relaunch_dead_incomplete_workers_locked",
        lambda *_a, **_k: {
            "relaunched": [],
            "blocked": [],
            "skipped": [],
            "identity_generation": 0,
            "note": "none",
        },
    )

    def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise TeamApiError(
            "E_TEAM_API_FAILED",
            "task '1': owner_claim_mismatch",
            details={
                "error": "corrupt_claim",
                "task_id": "1",
                "invariant": "owner_claim_mismatch",
            },
        )

    monkeypatch.setattr("omg_cli.team.api.reconcile_task_claims", boom)
    with pytest.raises(TeamApiError, match="owner_claim_mismatch"):
        runtime.resume_for_identity(tmp_path, "team-name")


def test_launch_team_dry_run_seeds_ref_and_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    _init_repo(tmp_path)

    meta = launch_team(
        "1. lane one\n2. lane two",
        workers=2,
        role="executor",
        root=tmp_path,
        dry_run=True,
        check_binary=False,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert meta["dry_run"] is True
    assert meta["launch_mode"] == "shorthand"
    assert meta["topology"] == "split"
    assert meta["task_count"] == 2
    team_name = meta["team_name"]
    assert team_ref_path(tmp_path, team_name).is_file()
    assert resolve_team_ref(tmp_path, team_name) == meta["run_id"]
    lane = tmp_path / ".omg" / "team-lanes" / "w1" / ".gitkeep"
    assert lane.is_file()
    # API board inboxes
    inboxes = list(tmp_path.joinpath(".omg", "state", "runs", meta["run_id"]).rglob(
        "inbox.md"
    ))
    assert len(inboxes) >= 2
    # Control plane still CLI-stamped
    team_json = json.loads(
        (
            tmp_path
            / ".omg"
            / "state"
            / "runs"
            / meta["run_id"]
            / "team"
            / "team.json"
        ).read_text(encoding="utf-8")
    )
    assert team_json["writer"] == "omg-cli"
    assert team_json["topology"] == "split"
    assert team_json.get("startup_acks") is None
    assert team_json.get("startup_status") is None
    assert meta.get("startup_acks") is None
    assert meta.get("startup_status") is None
    assert "dry_run skipped ACK wait" in str(meta.get("startup_note") or "")
    inbox_text = "\n".join(p.read_text(encoding="utf-8") for p in inboxes)
    assert "claim-task" in inbox_text
    assert '"task_id":"1"' in inbox_text
    prompts = list(
        tmp_path.joinpath(".omg", "worktrees", meta["run_id"]).rglob("*.prompt.md")
    )
    assert prompts, "expected materialized worker prompts before spawn"
    prompt_text = "\n".join(p.read_text(encoding="utf-8") for p in prompts)
    assert "## First actions (required, this turn)" in prompt_text
    assert "claim-task" in prompt_text
    assert '"task_id":"1"' in prompt_text
    assert "not `w1`" in prompt_text or "not w1" in prompt_text.lower()


def test_wait_for_startup_acks_full_partial_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.mailbox import send_message
    from omg_cli.team.runtime import wait_for_startup_acks

    run_id = "run-ack-wait"
    team_id = "team"
    # #99: mailbox ACK alone cannot satisfy the provider-ready gate.
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "200")

    # Zero signals → failed_start
    zero = wait_for_startup_acks(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        expected_workers=["w1", "w2"],
        timeout_ms=50,
        poll_s=0.01,
    )
    assert zero["startup_status"] == "failed_start"
    assert zero["startup_acks"] == 0
    assert zero["startup_process_ready"] == 0

    send_message(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        sender_id="w1",
        recipient_id="leader-fixed",
        body="ACK",
        generation=0,
        kind="ack",
        dedupe_key="ack-w1",
    )
    partial = wait_for_startup_acks(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        expected_workers=["w1", "w2"],
        timeout_ms=50,
        poll_s=0.01,
    )
    # ACK-only is still failed_start (not degraded) — ACK cannot elevate.
    assert partial["startup_status"] == "failed_start"
    assert partial["startup_acks"] == 1
    assert partial["startup_ack_workers"] == ["w1"]
    assert partial["startup_process_ready"] == 0

    send_message(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        sender_id="w2",
        recipient_id="leader-fixed",
        body="ACK",
        generation=0,
        kind="ack",
        dedupe_key="ack-w2",
    )
    full = wait_for_startup_acks(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        expected_workers=["w1", "w2"],
        timeout_ms=200,
        poll_s=0.01,
    )
    assert full["startup_status"] == "failed_start"
    assert full["startup_acks"] == 2
    assert full["startup_process_ready"] == 0
    assert full["startup_ack_workers"] == ["w1", "w2"]


def test_process_ready_alone_makes_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy v1 helper receipts must NOT satisfy readiness (#99)."""
    from omg_cli.team.runtime import (
        wait_for_startup_acks,
        write_worker_ready_receipt,
    )

    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "200")
    run_id = "run-proc-ready"
    team_id = "team"
    write_worker_ready_receipt(
        tmp_path, run_id=run_id, team_id=team_id, worker_id="w1"
    )
    write_worker_ready_receipt(
        tmp_path, run_id=run_id, team_id=team_id, worker_id="w2"
    )
    out = wait_for_startup_acks(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        expected_workers=["w1", "w2"],
        timeout_ms=100,
        poll_s=0.01,
    )
    assert out["startup_status"] == "failed_start"
    assert out["startup_acks"] == 0
    assert out["startup_process_ready"] == 0
    assert out["startup_ready_workers"] == []


def test_wrap_pane_with_worker_ready_prefixes_command() -> None:
    """Legacy wrap still exists but new launches use supervisor (#99)."""
    from omg_cli.team.plane import wrap_pane_with_worker_ready

    wrapped = wrap_pane_with_worker_ready("echo hi")
    assert "team" in wrapped and "worker-ready" in wrapped
    assert wrapped.endswith("echo hi")
    assert "&&" in wrapped


def test_ready_timeout_ms_rejects_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.team.plane import TeamGateError
    from omg_cli.team.runtime import ready_timeout_ms

    monkeypatch.delenv("OMG_TEAM_READY_TIMEOUT_MS", raising=False)
    assert ready_timeout_ms() == 45_000
    assert ready_timeout_ms({"OMG_TEAM_READY_TIMEOUT_MS": "1000"}) == 1000
    with pytest.raises(TeamGateError):
        ready_timeout_ms({"OMG_TEAM_READY_TIMEOUT_MS": "nope"})



def test_startup_readiness_payload_no_wait_and_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.runtime import startup_readiness_payload

    payload = startup_readiness_payload(
        tmp_path,
        run_id="r1",
        team_id="team",
        expected_workers=["t1", "t2"],
        no_wait=True,
    )
    assert payload["startup_status"] == "unverified_start"
    assert payload["startup_missing_workers"] == ["t1", "t2"]
    assert "no-wait" in str(payload["startup_note"])

    dry = startup_readiness_payload(
        tmp_path,
        run_id="r1",
        team_id="team",
        expected_workers=["t1"],
        dry_run=True,
    )
    assert dry["startup_status"] is None
    assert "dry_run skipped" in str(dry["startup_note"])


def test_apply_start_readiness_failed_start_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit start path: zero ACKs → failed_start on team.json (#20)."""
    from omg_cli.team.plane import EXPERIMENTAL_ENV, start_team
    from omg_cli.team.runtime import apply_start_readiness

    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    _init_repo(tmp_path)
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "50")

    tasks = [
        {"task_id": "t1", "owned_files": ["README.md"]},
        {"task_id": "t2", "owned_files": ["docs/x.md"]},
    ]
    meta = start_team(
        "readiness",
        tasks,
        root=tmp_path,
        dry_run=True,
        force=True,
    )
    # dry_run still applies readiness skip notes
    out = apply_start_readiness(tmp_path, meta, dry_run=True)
    assert out.get("startup_status") is None
    assert "dry_run skipped" in str(out.get("startup_note") or "")

    # Live-shaped meta + short timeout + no ACKs → failed_start (no tmux).
    live_meta = dict(meta)
    live_meta["dry_run"] = False
    failed = apply_start_readiness(
        tmp_path,
        live_meta,
        dry_run=False,
        no_wait=False,
        env={"OMG_TEAM_READY_TIMEOUT_MS": "50"},
    )
    assert failed["startup_status"] == "failed_start"
    assert failed["startup_acks"] == 0
    assert set(failed.get("startup_missing_workers") or []) == {"t1", "t2"}
    team_json = json.loads(
        (
            tmp_path
            / ".omg"
            / "state"
            / "runs"
            / meta["run_id"]
            / "team"
            / "team.json"
        ).read_text(encoding="utf-8")
    )
    assert team_json["startup_status"] == "failed_start"
    assert team_json.get("launch_mode") == "explicit"
