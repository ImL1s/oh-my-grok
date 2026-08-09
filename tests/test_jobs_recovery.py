"""Lease observation + recover reconciliation tests (#68 PR4)."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.jobs.lease import (
    JOB_RECOVERY_CLOCK_SKEW_S,
    acquire_owner_lease,
    format_lease_ts,
)
from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.ownership import IdentityProbeOutcome, ProcessIdentity
from omg_cli.jobs.recovery import (
    JobHealth,
    decide_observation,
    observe_job,
    recover_job,
    recover_jobs,
)
from omg_cli.jobs.runtime import cancel_job, start_job, wait_job
from omg_cli.jobs.store import (
    job_json_path,
    job_lock,
    read_job_record,
    write_job_record,
)

pytest_plugins = ["tests.jobs_testutil"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".omg").mkdir()
    return tmp_path


def _prompt(root: Path, text: str = "recover test") -> Path:
    p = root / "prompt.md"
    p.write_text(text, encoding="utf-8")
    return p


def _expire_lease(rec, *, skew_extra: float = 1.0) -> datetime:
    """Return a `now` past expires_at + skew for *rec*'s lease."""
    assert rec.owner_lease is not None
    expires = datetime.fromisoformat(
        rec.owner_lease["expires_at"].replace("Z", "+00:00")
    )
    return expires + timedelta(seconds=JOB_RECOVERY_CLOCK_SKEW_S + skew_extra)



def _make_starting_stale(root: Path, job_id: str) -> None:
    path = job_json_path(root, job_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = format_lease_ts(
        datetime.now(timezone.utc) - timedelta(seconds=120)
    )
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def _force_lease_expired_on_disk(root: Path, job_id: str) -> None:
    """Rewrite lease timestamps so recovery is immediately eligible."""
    with job_lock(root, job_id):
        rec = read_job_record(root, job_id)
        assert rec.owner_lease is not None
        past = datetime.now(timezone.utc) - timedelta(seconds=120)
        lease = dict(rec.owner_lease)
        lease["acquired_at"] = format_lease_ts(past)
        lease["heartbeat_at"] = format_lease_ts(past)
        lease["expires_at"] = format_lease_ts(past + timedelta(seconds=30))
        lease["released_at"] = None
        rec.owner_lease = lease
        write_job_record(root, rec)


def test_fresh_lease_exact_live_runner_is_healthy(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=2.0,
    )
    path = job_json_path(root, result.record.job_id)
    raw_before = path.read_bytes()
    obs = observe_job(root, result.record.job_id)
    assert obs.health == JobHealth.RUNNING_HEALTHY
    assert obs.recoverable is False
    # Observation must not mutate job.json (heartbeat may still run concurrently,
    # so only assert recoverability/health — not generation equality).
    assert read_job_record(root, result.record.job_id).state == JobState.RUNNING
    del raw_before
    cancel_job(root, result.record.job_id, reason="test")


def test_dead_runner_before_expiry_is_suspect_not_lost(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    rec = result.record
    os.kill(int(rec.pid), signal.SIGKILL)
    try:
        os.waitpid(int(rec.pid), 0)
    except ChildProcessError:
        pass
    time.sleep(0.05)
    obs = observe_job(root, rec.job_id)
    assert obs.health == JobHealth.OWNER_MISSING_BEFORE_EXPIRY
    gen0 = read_job_record(root, rec.job_id).generation
    out = recover_job(root, rec.job_id)
    assert out.ok is True
    assert out.action in {"noop_healthy", "noop_not_active"}
    assert read_job_record(root, rec.job_id).state == JobState.RUNNING
    assert read_job_record(root, rec.job_id).generation == gen0


def test_expired_lease_live_runner_is_not_reclaimed(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    far_future = datetime.now(timezone.utc) + timedelta(hours=2)
    gen0 = read_job_record(root, result.record.job_id).generation
    obs = observe_job(root, result.record.job_id, now=far_future)
    assert obs.health == JobHealth.LEASE_STALE_LIVE
    out = recover_job(root, result.record.job_id, now=far_future)
    assert out.ok is False
    assert out.error_code == "E_JOB_RECOVERY_UNPROVEN"
    assert read_job_record(root, result.record.job_id).state == JobState.RUNNING
    # Heartbeat may still advance generation; durable state must remain running.
    assert read_job_record(root, result.record.job_id).state == JobState.RUNNING
    del gen0
    cancel_job(root, result.record.job_id, reason="test")


def test_expired_dead_runner_no_provider_marks_lost(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    rec = result.record
    os.kill(int(rec.pid), signal.SIGKILL)
    try:
        os.waitpid(int(rec.pid), 0)
    except ChildProcessError:
        pass
    _force_lease_expired_on_disk(root, rec.job_id)
    out = recover_job(root, rec.job_id)
    assert out.ok is True
    assert out.action == "marked_lost"
    assert out.before_state == "running"
    assert out.after_state == "lost"
    after = read_job_record(root, rec.job_id)
    assert after.state == JobState.LOST
    assert after.owner_lease is not None
    assert after.owner_lease.get("released_at") is not None
    assert after.exit and after.exit.get("class") == "lease_lost"


def test_expired_reused_pid_marks_old_owner_lost_without_signal(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    rec = result.record
    signals: list[int] = []
    real_kill = os.kill

    def _no_kill(pid: int, sig: int = 0) -> None:
        if sig != 0:
            signals.append(sig)
            raise AssertionError("recovery must not signal reused pid")
        return real_kill(pid, 0)

    monkeypatch.setattr(os, "kill", _no_kill)
    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.REUSED,
    )
    far_future = datetime.now(timezone.utc) + timedelta(hours=2)
    out = recover_job(root, rec.job_id, now=far_future)
    assert out.ok is True
    assert out.action == "marked_lost"
    assert signals == []
    monkeypatch.undo()
    try:
        os.kill(int(rec.pid), signal.SIGKILL)
    except OSError:
        pass


def test_probe_unavailable_blocks_recovery_zero_mutation(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.UNPROVEN,
    )
    far_future = datetime.now(timezone.utc) + timedelta(hours=2)
    out = recover_job(root, result.record.job_id, now=far_future)
    assert out.ok is False
    assert out.error_code == "E_JOB_RECOVERY_UNPROVEN"
    assert read_job_record(root, result.record.job_id).state == JobState.RUNNING
    cancel_job(root, result.record.job_id, reason="test")


def test_getpgid_error_blocks_recovery_zero_mutation(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )

    def _boom(pid: int) -> int:  # noqa: ARG001
        raise OSError("getpgid unavailable")

    monkeypatch.setattr(os, "getpgid", _boom)
    far_future = datetime.now(timezone.utc) + timedelta(hours=2)
    out = recover_job(root, result.record.job_id, now=far_future)
    assert out.ok is False
    assert out.error_code == "E_JOB_RECOVERY_UNPROVEN"
    assert read_job_record(root, result.record.job_id).state == JobState.RUNNING
    monkeypatch.undo()
    cancel_job(root, result.record.job_id, reason="test")


def test_missing_start_fingerprint_live_pid_is_unproven(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    with job_lock(root, result.record.job_id):
        rec = read_job_record(root, result.record.job_id)
        rec.pid_starttime = None
        write_job_record(root, rec)
    far_future = datetime.now(timezone.utc) + timedelta(hours=2)
    obs = observe_job(root, result.record.job_id, now=far_future)
    assert obs.health == JobHealth.IDENTITY_UNPROVEN
    out = recover_job(root, result.record.job_id, now=far_future)
    assert out.ok is False
    assert out.error_code == "E_JOB_RECOVERY_UNPROVEN"
    cancel_job(root, result.record.job_id, reason="test")


def test_live_inner_provider_orphan_blocks_recover(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    _force_lease_expired_on_disk(root, result.record.job_id)

    def _probe(identity: ProcessIdentity) -> IdentityProbeOutcome:
        # Outer gone, inner live — decide by pid match vs provider pid we inject.
        return IdentityProbeOutcome.GONE

    with job_lock(root, result.record.job_id):
        rec = read_job_record(root, result.record.job_id)
        rec.provider_process = {
            "state": "bound",
            "pid": 424242,
            "pgid": 424242,
            "pid_starttime": "lstart:provider",
            "handle": "provider:test",
            "bound_at": format_lease_ts(datetime.now(timezone.utc)),
            "exited_at": None,
        }
        write_job_record(root, rec)

    calls: list[int] = []

    def _probe2(identity: ProcessIdentity) -> IdentityProbeOutcome:
        calls.append(identity.pid)
        if identity.pid == 424242:
            return IdentityProbeOutcome.LIVE
        return IdentityProbeOutcome.GONE

    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        _probe2,
    )
    # Also kill outer so real probe wouldn't matter if patch missed.
    try:
        os.kill(int(result.record.pid), signal.SIGKILL)
    except OSError:
        pass
    far_future = datetime.now(timezone.utc) + timedelta(hours=2)
    out = recover_job(root, result.record.job_id, now=far_future)
    assert out.ok is False
    assert out.error_code == "E_JOB_RECOVERY_ORPHAN_LIVE"
    assert read_job_record(root, result.record.job_id).state == JobState.RUNNING


def test_dead_outer_and_dead_inner_mark_lost(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    with job_lock(root, result.record.job_id):
        rec = read_job_record(root, result.record.job_id)
        rec.provider_process = {
            "state": "bound",
            "pid": 525252,
            "pgid": 525252,
            "pid_starttime": "lstart:provider",
            "handle": "provider:test",
            "bound_at": format_lease_ts(datetime.now(timezone.utc)),
            "exited_at": None,
        }
        write_job_record(root, rec)
    _force_lease_expired_on_disk(root, result.record.job_id)
    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.GONE,
    )
    try:
        os.kill(int(result.record.pid), signal.SIGKILL)
    except OSError:
        pass
    out = recover_job(root, result.record.job_id)
    assert out.ok is True
    assert out.action == "marked_lost"


def test_starting_with_gone_spawn_identity_marks_lost(root: Path) -> None:
    from omg_cli.jobs.store import create_job_dir, transition_job

    rec = create_job_dir(
        root, provider="fake", role="researcher", prompt_text="starting"
    )
    transition_job(root, rec.job_id, JobState.STARTING)
    # Write spawn identity pointing at a dead pid.
    from omg_cli.jobs.store import job_dir
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes

    payload = {
        "job_id": rec.job_id,
        "pid": 999_001,
        "pgid": 999_001,
        "handle": "h",
        "pid_starttime": "lstart:gone",
        "reason": "test",
    }
    atomic_write_bytes(
        job_dir(root, rec.job_id) / "spawn_identity.json",
        (json.dumps(payload) + "\n").encode(),
        mode=DATA_FILE_MODE,
        replace=True,
    )
    _make_starting_stale(root, rec.job_id)
    out = recover_job(root, rec.job_id)
    assert out.ok is True
    assert out.action == "marked_lost"
    assert read_job_record(root, rec.job_id).state == JobState.LOST


def test_starting_without_identity_remains_unproven(root: Path) -> None:
    from omg_cli.jobs.store import create_job_dir, transition_job

    rec = create_job_dir(
        root, provider="fake", role="researcher", prompt_text="no-id"
    )
    transition_job(root, rec.job_id, JobState.STARTING)
    _make_starting_stale(root, rec.job_id)
    out = recover_job(root, rec.job_id)
    assert out.ok is False
    assert out.error_code == "E_JOB_RECOVERY_UNPROVEN"
    assert read_job_record(root, rec.job_id).state == JobState.STARTING


def test_spawn_uncertain_marker_blocks_recovery(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    from omg_cli.jobs.store import job_dir
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes

    atomic_write_bytes(
        job_dir(root, result.record.job_id) / "spawn_uncertain.json",
        b'{"detail":"test"}\n',
        mode=DATA_FILE_MODE,
        replace=True,
    )
    try:
        os.kill(int(result.record.pid), signal.SIGKILL)
    except OSError:
        pass
    _force_lease_expired_on_disk(root, result.record.job_id)
    out = recover_job(root, result.record.job_id)
    assert out.ok is False
    assert out.error_code == "E_JOB_RECOVERY_UNPROVEN"


def test_legacy_live_running_job_is_unmanaged(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs.store import create_job_dir

    rec = create_job_dir(
        root, provider="fake", role="researcher", prompt_text="legacy-live"
    )
    with job_lock(root, rec.job_id):
        cur = read_job_record(root, rec.job_id)
        cur.state = JobState.RUNNING
        cur.pid = 777001
        cur.pgid = 777001
        cur.handle = "legacy"
        cur.pid_starttime = "lstart:legacy"
        cur.owner_lease = None
        write_job_record(root, cur)
    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.LIVE,
    )
    obs = observe_job(root, rec.job_id)
    assert obs.health == JobHealth.LEGACY_UNMANAGED
    out = recover_job(root, rec.job_id)
    assert out.ok is False


def test_legacy_dead_running_job_can_be_marked_lost(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs.store import create_job_dir

    rec = create_job_dir(
        root, provider="fake", role="researcher", prompt_text="legacy-dead"
    )
    with job_lock(root, rec.job_id):
        cur = read_job_record(root, rec.job_id)
        cur.state = JobState.RUNNING
        cur.pid = 777002
        cur.pgid = 777002
        cur.handle = "legacy"
        cur.pid_starttime = "lstart:legacy"
        cur.owner_lease = None
        write_job_record(root, cur)
    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.GONE,
    )
    out = recover_job(root, rec.job_id)
    assert out.ok is True
    assert out.action == "marked_lost"


def test_heartbeat_between_probe_and_cas_wins(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    _force_lease_expired_on_disk(root, result.record.job_id)
    try:
        os.kill(int(result.record.pid), signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.05)

    real_decide = decide_observation

    def _renewing_decide(record, **kwargs):  # noqa: ANN001
        obs = real_decide(record, **kwargs)
        if obs.health == JobHealth.RECOVERABLE_LOST:
            # Simulate heartbeat landing: bump generation + refresh lease.
            with job_lock(root, record.job_id):
                cur = read_job_record(root, record.job_id)
                now = datetime.now(timezone.utc)
                lease = acquire_owner_lease(
                    attempt=int(cur.attempt),
                    now=now,
                    owner_token=cur.owner_lease["owner_token"],
                )
                cur.owner_lease = lease
                # Keep pid so it looks like a live renew race after we already
                # observed gone — generation change alone must conflict.
                write_job_record(root, cur)
        return obs

    monkeypatch.setattr(
        "omg_cli.jobs.recovery.decide_observation",
        _renewing_decide,
    )
    # After renew, identity may still be gone — but generation CAS should conflict
    # if observation used stale generation. Force probe GONE.
    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.GONE,
    )
    out = recover_job(root, result.record.job_id)
    # Either conflict (CAS lost) or noop_healthy if re-read saw fresh lease —
    # both are acceptable fail-closed outcomes vs marking lost on stale evidence.
    assert out.action != "marked_lost" or out.ok is False
    if out.ok is False:
        assert out.error_code == "E_JOB_RECOVERY_CONFLICT"
    else:
        assert read_job_record(root, result.record.job_id).state == JobState.RUNNING


def test_generation_change_between_probe_and_cas_blocks_transition(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    try:
        os.kill(int(result.record.pid), signal.SIGKILL)
    except OSError:
        pass
    _force_lease_expired_on_disk(root, result.record.job_id)

    real_compare = None
    from omg_cli.jobs import store as store_mod

    real_compare = store_mod.compare_and_transition_job

    def _bump_then_compare(*args, **kwargs):  # noqa: ANN001
        jid = args[1] if len(args) > 1 else kwargs.get("job_id")
        with job_lock(root, jid):
            cur = read_job_record(root, jid)
            write_job_record(root, cur)  # bump generation
        return real_compare(*args, **kwargs)

    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.GONE,
    )
    monkeypatch.setattr(
        "omg_cli.jobs.recovery.compare_and_transition_job",
        _bump_then_compare,
    )
    out = recover_job(root, result.record.job_id)
    assert out.ok is False
    assert out.error_code == "E_JOB_RECOVERY_CONFLICT"


def test_two_concurrent_recoverers_have_one_winner(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    try:
        os.kill(int(result.record.pid), signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.05)
    _force_lease_expired_on_disk(root, result.record.job_id)

    results: list = []

    def _run() -> None:
        results.append(recover_job(root, result.record.job_id))

    t1 = threading.Thread(target=_run)
    t2 = threading.Thread(target=_run)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    marked = [r for r in results if r.action == "marked_lost"]
    assert len(marked) == 1
    assert read_job_record(root, result.record.job_id).state == JobState.LOST


def test_recover_dry_run_has_zero_writes_signals_or_launches(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    try:
        os.kill(int(result.record.pid), signal.SIGKILL)
    except OSError:
        pass
    _force_lease_expired_on_disk(root, result.record.job_id)
    before = read_job_record(root, result.record.job_id)
    raw_before = job_json_path(root, result.record.job_id).read_bytes()
    out = recover_job(root, result.record.job_id, dry_run=True)
    assert out.ok is True
    assert out.action == "would_mark_lost"
    assert read_job_record(root, result.record.job_id).generation == before.generation
    assert job_json_path(root, result.record.job_id).read_bytes() == raw_before


def test_recover_all_is_sorted_and_holds_one_lock_at_a_time(root: Path) -> None:
    jobs = []
    for i in range(3):
        r = start_job(
            root,
            provider="fake",
            role="researcher",
            prompt_file=_prompt(root, f"batch-{i}"),
            sleep_s=30.0,
        )
        jobs.append(r.record.job_id)
        try:
            os.kill(int(r.record.pid), signal.SIGKILL)
        except OSError:
            pass
        _force_lease_expired_on_disk(root, r.record.job_id)
    held: list[str] = []
    max_concurrent = [0]
    current = [0]
    lock = threading.Lock()

    from omg_cli.jobs import store as store_mod

    real_lock = store_mod.job_lock

    from contextlib import contextmanager

    @contextmanager
    def _tracking_lock(project_root, job_id):  # noqa: ANN001
        with lock:
            current[0] += 1
            max_concurrent[0] = max(max_concurrent[0], current[0])
            held.append(job_id)
        try:
            with real_lock(project_root, job_id):
                yield
        finally:
            with lock:
                current[0] -= 1


    # Patch at store level used by compare_and_transition
    import omg_cli.jobs.store as s

    s.job_lock = _tracking_lock  # type: ignore[assignment]
    try:
        batch = recover_jobs(root)
    finally:
        s.job_lock = real_lock  # type: ignore[assignment]
    assert batch.ok is True
    assert max_concurrent[0] <= 1
    assert [r.job_id for r in batch.results] == sorted(jobs)


def test_recover_all_reports_partial_without_rollback(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root, "a"),
        sleep_s=30.0,
    )
    b = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root, "b"),
        sleep_s=30.0,
    )
    for rec in (a.record, b.record):
        try:
            os.kill(int(rec.pid), signal.SIGKILL)
        except OSError:
            pass
        _force_lease_expired_on_disk(root, rec.job_id)

    # Make B unproven.
    def _probe(identity: ProcessIdentity) -> IdentityProbeOutcome:
        # Identify by matching pid of B
        if identity.pid == int(b.record.pid):
            return IdentityProbeOutcome.UNPROVEN
        return IdentityProbeOutcome.GONE

    monkeypatch.setattr(
        "omg_cli.jobs.recovery.probe_identity_for_recovery",
        _probe,
    )
    batch = recover_jobs(root)
    assert batch.ok is False
    states = {
        read_job_record(root, a.record.job_id).state,
        read_job_record(root, b.record.job_id).state,
    }
    assert JobState.LOST in states
    assert JobState.RUNNING in states


def test_internal_acp_job_is_publicly_protected(root: Path) -> None:
    from omg_cli.jobs.store import create_job_dir, transition_job

    rec = create_job_dir(
        root,
        provider="grok-acp-session",
        role="acp",
        prompt_text="acp",
    )
    transition_job(root, rec.job_id, JobState.STARTING)
    transition_job(
        root,
        rec.job_id,
        JobState.RUNNING,
        updates={
            "pid": 888001,
            "pgid": 888001,
            "handle": "acp",
            "pid_starttime": "lstart:acp",
            "owner_lease": acquire_owner_lease(attempt=1),
        },
    )
    out = recover_job(root, rec.job_id)
    assert out.ok is False
    assert out.error_code == "E_JOB_PROVIDER_INTERNAL"
    assert out.action == "protected_internal"
    assert read_job_record(root, rec.job_id).state == JobState.RUNNING


def test_status_never_opens_or_executes_result_artifact(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.05,
    )
    terminal, _ = wait_job(root, result.record.job_id, timeout_s=10.0)
    # Observation must not require opening result file contents.
    from omg_cli.jobs.store import job_dir

    result_path = job_dir(root, terminal.job_id) / "artifacts" / "result.md"
    if result_path.is_file():
        # Replace with unreadable sentinel — observation still works.
        result_path.write_bytes(b"x" * 10)
    obs = observe_job(root, terminal.job_id)
    assert obs.health == JobHealth.TERMINAL


def test_fake_runner_killed_then_restart_recover_marks_lost(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    os.kill(int(result.record.pid), signal.SIGKILL)
    try:
        os.waitpid(int(result.record.pid), 0)
    except ChildProcessError:
        pass
    _force_lease_expired_on_disk(root, result.record.job_id)
    # Fresh recovery call (simulates new CLI process).
    out = recover_job(root, result.record.job_id)
    assert out.action == "marked_lost"
    assert read_job_record(root, result.record.job_id).state == JobState.LOST


def test_live_fake_runner_survives_new_cli_process_observation(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=2.0,
    )
    obs = observe_job(root, result.record.job_id)
    assert obs.health == JobHealth.RUNNING_HEALTHY
    still = read_job_record(root, result.record.job_id)
    assert still.state == JobState.RUNNING
    assert still.pid == result.record.pid
    cancel_job(root, result.record.job_id, reason="test")


def test_runner_fenced_after_terminal_or_attempt_change(root: Path) -> None:
    from omg_cli.jobs.store import transition_owned_job

    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    rec = result.record
    token = rec.owner_lease["owner_token"]
    # External cancel wins first.
    cancel_job(root, rec.job_id, reason="test")
    with pytest.raises(JobStoreError) as ei:
        transition_owned_job(
            root,
            rec.job_id,
            JobState.SUCCEEDED,
            expected_attempt=1,
            expected_owner_token=token,
            expected_runner_pid=int(rec.pid),
            updates={"error_message": "stale"},
        )
    assert ei.value.code in {"E_JOB_LEASE_FENCED", "E_JOB_TRANSITION"}


def test_wait_returns_recovery_required_not_timeout(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    _force_lease_expired_on_disk(root, result.record.job_id)
    with pytest.raises(JobStoreError) as ei:
        wait_job(
            root,
            result.record.job_id,
            timeout_s=5.0,
            stop_on_recovery_required=True,
        )
    assert ei.value.code == "E_JOB_RECOVERY_REQUIRED"
    cancel_job(root, result.record.job_id, reason="test")
