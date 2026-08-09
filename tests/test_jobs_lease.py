"""Owner lease schema / fencing / heartbeat tests (#68 PR4)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omg_cli.jobs.lease import (
    acquire_owner_lease,
    public_lease_summary,
    release_lease_dict,
    renew_lease_dict,
    validate_owner_lease,
)
from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.runtime import cancel_job, start_job, wait_job
from omg_cli.jobs.store import (
    job_json_path,
    read_job_record,
    renew_owner_lease,
    update_owned_job_fields,
)

pytest_plugins = ["tests.jobs_testutil"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".omg").mkdir()
    return tmp_path


def _prompt(root: Path, text: str = "lease test") -> Path:
    p = root / "prompt.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_running_commit_atomically_binds_owner_lease(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.2,
    )
    rec = result.record
    assert rec.state == JobState.RUNNING
    assert rec.pid and rec.pgid and rec.handle
    assert rec.owner_lease is not None
    lease = validate_owner_lease(rec.owner_lease, expected_attempt=1, require_active=True)
    assert lease["released_at"] is None
    assert lease["attempt"] == 1
    disk = json.loads(job_json_path(root, rec.job_id).read_text(encoding="utf-8"))
    assert disk["owner_lease"]["owner_token"]
    assert disk["pid"] == rec.pid
    wait_job(root, rec.job_id, timeout_s=10.0)


def test_runner_heartbeat_advances_lease(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Short interval via renew API (inject now) — no real 30s wait.
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=2.0,
    )
    rec = result.record
    assert rec.owner_lease is not None
    token = rec.owner_lease["owner_token"]
    before = rec.owner_lease["heartbeat_at"]
    gen0 = rec.generation
    later = datetime.now(timezone.utc) + timedelta(seconds=10)
    renewed = renew_owner_lease(
        root,
        rec.job_id,
        expected_attempt=1,
        expected_owner_token=token,
        expected_runner_pid=int(rec.pid),
        now=later,
    )
    assert renewed.generation > gen0
    assert renewed.owner_lease is not None
    assert renewed.owner_lease["heartbeat_at"] != before
    assert renewed.owner_lease["expires_at"] > renewed.owner_lease["heartbeat_at"]
    cancel_job(root, rec.job_id, reason="test")


def test_wrong_owner_token_is_fenced_zero_mutation(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=2.0,
    )
    rec = result.record
    gen0 = rec.generation
    with pytest.raises(JobStoreError) as ei:
        renew_owner_lease(
            root,
            rec.job_id,
            expected_attempt=1,
            expected_owner_token="0" * 32,
            expected_runner_pid=int(rec.pid),
        )
    assert ei.value.code == "E_JOB_LEASE_FENCED"
    after = read_job_record(root, rec.job_id)
    assert after.generation == gen0
    cancel_job(root, rec.job_id, reason="test")


def test_wrong_attempt_or_runner_pid_is_fenced(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=2.0,
    )
    rec = result.record
    token = rec.owner_lease["owner_token"]
    gen0 = rec.generation
    with pytest.raises(JobStoreError) as ei:
        update_owned_job_fields(
            root,
            rec.job_id,
            expected_attempt=99,
            expected_owner_token=token,
            expected_runner_pid=int(rec.pid),
            error_message="should not land",
        )
    assert ei.value.code == "E_JOB_LEASE_FENCED"
    with pytest.raises(JobStoreError) as ei2:
        update_owned_job_fields(
            root,
            rec.job_id,
            expected_attempt=1,
            expected_owner_token=token,
            expected_runner_pid=int(rec.pid) + 99999,
            error_message="should not land",
        )
    assert ei2.value.code == "E_JOB_LEASE_FENCED"
    assert read_job_record(root, rec.job_id).generation == gen0
    cancel_job(root, rec.job_id, reason="test")


def test_terminal_transition_releases_lease(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.05,
    )
    terminal, timed_out = wait_job(root, result.record.job_id, timeout_s=10.0)
    assert timed_out is False
    assert terminal.state == JobState.SUCCEEDED
    assert terminal.owner_lease is not None
    assert terminal.owner_lease.get("released_at") is not None


def test_cancel_releases_lease_after_disappearance_proof(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=5.0,
    )
    cancelled = cancel_job(root, result.record.job_id, reason="test-cancel")
    assert cancelled.state == JobState.CANCELLED
    assert cancelled.owner_lease is not None
    assert cancelled.owner_lease.get("released_at") is not None


def test_retry_archives_and_clears_prior_lease(root: Path) -> None:
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import attempt_dir

    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root, "retry lease"),
        sleep_s=0.05,
        attempt_budget=3,
        fail=True,
    )
    failed, _ = wait_job(root, result.record.job_id, timeout_s=10.0)
    assert failed.state == JobState.FAILED
    old_lease = dict(failed.owner_lease or {})
    assert old_lease.get("released_at") is not None
    retried = retry_job(root, failed.job_id, attempt=2, launch=False)
    assert retried.record.state == JobState.STARTING
    assert retried.record.attempt == 2
    assert retried.record.owner_lease is None
    archived = json.loads(
        (attempt_dir(root, failed.job_id, 1) / "attempt.json").read_text(
            encoding="utf-8"
        )
    )
    assert archived.get("owner_lease", {}).get("owner_token") == old_lease.get(
        "owner_token"
    )
    assert retried.record.owner_lease is None


def test_legacy_schema_v1_without_lease_loads_unmanaged(root: Path) -> None:
    from omg_cli.jobs.recovery import JobHealth, observe_job
    from omg_cli.jobs.store import create_job_dir, write_job_record

    rec = create_job_dir(
        root,
        provider="fake",
        role="researcher",
        prompt_text="legacy",
    )
    # Simulate legacy running without lease (bypass transition lease mint).
    rec.state = JobState.RUNNING
    rec.pid = 1_000_001
    rec.pgid = 1_000_001
    rec.handle = "legacy"
    rec.pid_starttime = "lstart:fake"
    rec.owner_lease = None
    from omg_cli.jobs.store import job_lock

    with job_lock(root, rec.job_id):
        write_job_record(root, rec)
    loaded = read_job_record(root, rec.job_id)
    assert loaded.owner_lease is None
    obs = observe_job(root, rec.job_id)
    assert obs.health in {
        JobHealth.LEGACY_UNMANAGED,
        JobHealth.IDENTITY_UNPROVEN,
        JobHealth.RECOVERABLE_LOST,
    }
    assert obs.health != JobHealth.RUNNING_HEALTHY


def test_malformed_lease_token_attempt_and_timestamps_fail_closed() -> None:
    base = acquire_owner_lease(attempt=1)
    bad_token = dict(base, owner_token="not-hex")
    with pytest.raises(JobStoreError) as e1:
        validate_owner_lease(bad_token)
    assert e1.value.code == "E_JOB_LEASE_MALFORMED"

    bad_attempt = dict(base, attempt=0)
    with pytest.raises(JobStoreError) as e2:
        validate_owner_lease(bad_attempt)
    assert e2.value.code == "E_JOB_LEASE_MALFORMED"

    bad_order = dict(
        base,
        acquired_at=base["expires_at"],
        heartbeat_at=base["acquired_at"],
    )
    with pytest.raises(JobStoreError) as e3:
        validate_owner_lease(bad_order)
    assert e3.value.code == "E_JOB_LEASE_MALFORMED"

    naive = dict(base, acquired_at="2020-01-01T00:00:00")
    with pytest.raises(JobStoreError) as e4:
        validate_owner_lease(naive)
    assert e4.value.code == "E_JOB_LEASE_MALFORMED"


def test_public_status_never_exposes_owner_token(root: Path) -> None:
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.05,
    )
    pub = result.record.public_status()
    blob = json.dumps(pub)
    assert "owner_token" not in blob
    if result.record.owner_lease:
        assert result.record.owner_lease["owner_token"] not in blob
    summary = public_lease_summary(result.record.owner_lease)
    assert summary is not None
    assert "owner_token" not in summary
    wait_job(root, result.record.job_id, timeout_s=10.0)


def test_renew_and_release_helpers_roundtrip() -> None:
    lease = acquire_owner_lease(attempt=2)
    now = datetime.now(timezone.utc) + timedelta(seconds=3)
    renewed = renew_lease_dict(lease, now=now)
    assert renewed["owner_token"] == lease["owner_token"]
    assert renewed["attempt"] == 2
    released = release_lease_dict(renewed, now=now + timedelta(seconds=1))
    assert released is not None
    assert released["released_at"] is not None
