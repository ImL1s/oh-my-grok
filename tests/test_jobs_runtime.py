"""Hermetic durable job runtime tests (#68 PR1)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.runtime import (
    cancel_job,
    collect_job,
    job_status,
    list_jobs,
    start_job,
    wait_job,
)
from omg_cli.jobs.store import (
    create_job_dir,
    job_dir,
    job_json_path,
    read_job_record,
    transition_job,
)
from omg_cli.providers.base import ProviderAdapter


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".omg").mkdir()
    return tmp_path


def _prompt(root: Path, text: str = "do the thing") -> Path:
    p = root / "prompt.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_fake_provider_is_adapter() -> None:
    from omg_cli.jobs.fake import FakeProvider

    adapter = FakeProvider()
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.name == "fake"


def test_atomic_start_commit_and_running_handle(root: Path) -> None:
    prompt = _prompt(root)
    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=0.05,
    )
    rec = result.record
    assert result.launched is True
    assert rec.state == JobState.RUNNING
    assert rec.pid is not None and rec.pid > 0
    assert rec.pgid is not None and rec.pgid > 0
    assert rec.handle
    # Durability: job.json on disk
    disk = json.loads(job_json_path(root, rec.job_id).read_text(encoding="utf-8"))
    assert disk["state"] == "running"
    assert disk["pid"] == rec.pid

    terminal, timed_out = wait_job(root, rec.job_id, timeout_s=10.0)
    assert timed_out is False
    assert terminal.state == JobState.SUCCEEDED


def test_launch_rollback_never_running_with_null_pid(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = _prompt(root)

    def boom(*_a, **_k):  # noqa: ANN001
        raise OSError("simulated spawn failure")

    monkeypatch.setattr("omg_cli.jobs.runtime.subprocess.Popen", boom)

    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="fake",
            role="researcher",
            prompt_file=prompt,
            sleep_s=0.01,
        )
    assert ei.value.code == "E_JOB_LAUNCH"

    jobs = list_jobs(root)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["state"] == "failed"
    assert j["pid"] is None
    assert j["handle"] is None
    assert j["exit"]["class"] == "spawn_error"


def test_status_unknown_running_terminal(root: Path) -> None:
    with pytest.raises(JobStoreError) as ei:
        job_status(root, "20990101T000000Z-deadbeef")
    assert ei.value.code == "E_JOB_UNKNOWN"

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=2.0,
    )
    running = job_status(root, started.record.job_id)
    assert running.state == JobState.RUNNING

    # Cancel to get a known terminal without waiting full sleep
    cancelled = cancel_job(root, started.record.job_id, reason="test", grace_s=0.5)
    assert cancelled.state == JobState.CANCELLED
    again = job_status(root, started.record.job_id)
    assert again.state == JobState.CANCELLED


def test_wait_timeout_does_not_cancel(root: Path) -> None:
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=3.0,
    )
    rec, timed_out = wait_job(root, started.record.job_id, timeout_s=0.15)
    assert timed_out is True
    assert rec.state == JobState.RUNNING
    # Still running — not auto-cancelled
    mid = job_status(root, started.record.job_id)
    assert mid.state == JobState.RUNNING
    cancel_job(root, started.record.job_id, grace_s=0.5)


def test_wait_success(root: Path) -> None:
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=0.05,
    )
    rec, timed_out = wait_job(root, started.record.job_id, timeout_s=10.0)
    assert timed_out is False
    assert rec.state == JobState.SUCCEEDED


def test_collect_idempotent_descriptor_only_and_large(root: Path) -> None:
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=0.05,
        large_output=True,
    )
    wait_job(root, started.record.job_id, timeout_s=15.0)
    a = collect_job(root, started.record.job_id)
    b = collect_job(root, started.record.job_id)
    assert a == b
    assert a["result"] == "artifacts/result.md"
    assert a["artifacts"]
    # No giant inline blob in collect / job.json
    raw = job_json_path(root, started.record.job_id).read_text(encoding="utf-8")
    assert "x" * 1000 not in raw
    art = job_dir(root, started.record.job_id) / "artifacts" / "result.md"
    assert art.is_file()
    assert art.stat().st_size >= 100 * 1024


def test_cancel_graceful(root: Path) -> None:
    from omg_cli.jobs.runtime import _pid_alive

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=5.0,
    )
    pid = started.record.pid
    assert pid and os.getpgid(pid)
    assert _pid_alive(pid)
    rec = cancel_job(root, started.record.job_id, reason="graceful", grace_s=1.0)
    assert rec.state == JobState.CANCELLED
    assert rec.cancel_reason == "graceful"
    time.sleep(0.1)
    assert not _pid_alive(pid)


def test_cancel_force_ignore_sigterm(root: Path) -> None:
    from omg_cli.jobs.runtime import _pid_alive

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        ignore_sigterm=True,
        sleep_s=30.0,
    )
    pid = started.record.pid
    assert pid
    # Give runner time to install SIG_IGN
    time.sleep(0.3)
    assert _pid_alive(pid)
    rec = cancel_job(root, started.record.job_id, reason="force", grace_s=0.4)
    assert rec.state == JobState.CANCELLED
    time.sleep(0.15)
    assert not _pid_alive(pid)


def test_cancel_idempotent(root: Path) -> None:
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=0.05,
    )
    wait_job(root, started.record.job_id, timeout_s=10.0)
    a = cancel_job(root, started.record.job_id)
    b = cancel_job(root, started.record.job_id)
    assert a.state == JobState.SUCCEEDED
    assert b.state == JobState.SUCCEEDED


def test_sibling_isolation_cancel_a_keeps_b(root: Path) -> None:
    from omg_cli.jobs.runtime import _pid_alive

    prompt = _prompt(root, "shared")
    a = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=4.0,
    )
    b = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=4.0,
    )
    assert a.record.job_id != b.record.job_id
    assert a.record.pgid != b.record.pgid

    cancel_job(root, a.record.job_id, grace_s=0.5)
    time.sleep(0.1)
    assert not _pid_alive(int(a.record.pid or 0))
    assert _pid_alive(int(b.record.pid or 0))
    status_b = job_status(root, b.record.job_id)
    assert status_b.state == JobState.RUNNING
    cancel_job(root, b.record.job_id, grace_s=0.5)


def test_list_state_provider_filters(root: Path) -> None:
    prompt = _prompt(root)
    ok = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=0.05,
        run_id="run-a",
    )
    wait_job(root, ok.record.job_id, timeout_s=10.0)
    fail = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=0.05,
        fail=True,
        run_id="run-b",
    )
    wait_job(root, fail.record.job_id, timeout_s=10.0)

    by_state = list_jobs(root, state="succeeded")
    assert len(by_state) == 1
    assert by_state[0]["job_id"] == ok.record.job_id

    by_prov = list_jobs(root, provider="fake")
    assert len(by_prov) == 2

    by_run = list_jobs(root, run_id="run-b")
    assert len(by_run) == 1
    assert by_run[0]["job_id"] == fail.record.job_id


def test_malformed_job_json_fail_closed(root: Path) -> None:
    prompt = _prompt(root)
    rec = create_job_dir(
        root,
        provider="fake",
        role="researcher",
        prompt_text=prompt.read_text(encoding="utf-8"),
    )
    path = job_json_path(root, rec.job_id)
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(JobStoreError) as ei:
        read_job_record(root, rec.job_id)
    assert ei.value.code == "E_JOB_MALFORMED"


def test_missing_artifact_fail_closed(root: Path) -> None:
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=0.05,
        large_output=True,
    )
    wait_job(root, started.record.job_id, timeout_s=15.0)
    art = job_dir(root, started.record.job_id) / "artifacts" / "result.md"
    art.unlink()
    with pytest.raises(JobStoreError) as ei:
        collect_job(root, started.record.job_id)
    assert ei.value.code == "E_JOB_ARTIFACT"


def test_antigravity_provider_refused(root: Path) -> None:
    prompt = _prompt(root)
    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="antigravity",
            role="researcher",
            prompt_file=prompt,
        )
    assert ei.value.code == "E_JOB_PROVIDER"


def test_immutable_transition(root: Path) -> None:
    prompt = _prompt(root)
    rec = create_job_dir(
        root,
        provider="fake",
        role="researcher",
        prompt_text=prompt.read_text(encoding="utf-8"),
    )
    transition_job(root, rec.job_id, JobState.STARTING)
    transition_job(
        root,
        rec.job_id,
        JobState.FAILED,
        updates={"exit": {"class": "spawn_error", "returncode": 1}},
    )
    with pytest.raises(JobStoreError):
        transition_job(root, rec.job_id, JobState.RUNNING)
