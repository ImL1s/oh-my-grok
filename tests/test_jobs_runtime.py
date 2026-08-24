"""Hermetic durable job runtime tests (#68 PR1)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.runtime import (
    cancel_job,
    collect_job,
    job_status,
    list_jobs,
    prove_job_processes_gone,
    start_job,
    wait_job,
)
from omg_cli.jobs.store import (
    create_job_dir,
    job_dir,
    job_json_path,
    mark_cancel_requested,
    read_job_record,
    transition_job,
)
from omg_cli.providers.base import ProviderAdapter

pytest_plugins = ["tests.jobs_testutil"]


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
    # Best-effort ownership fingerprint (null only if probe failed).
    assert "pid_starttime" in rec.to_dict()
    # Durability: job.json on disk
    disk = json.loads(job_json_path(root, rec.job_id).read_text(encoding="utf-8"))
    assert disk["state"] == "running"
    assert disk["pid"] == rec.pid
    assert disk.get("pid_starttime") == rec.pid_starttime

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
        sleep_s=1.5,
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
        sleep_s=1.5,
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
        sleep_s=3.0,
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


def test_cancel_requested_beats_racing_success_stamp(root: Path) -> None:
    """Durable cancel_requested remaps runner success → cancelled under lock.

    Reproduces the CI flake where ignore_sigterm fake finishes Adapter.run
    during SIGTERM→grace→SIGKILL and would otherwise stamp succeeded.
    """
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        ignore_sigterm=True,
        sleep_s=0.6,
    )
    jid = started.record.job_id
    # Persist cancel intent without signalling — same window as slow force-kill.
    marked = mark_cancel_requested(root, jid, reason="race-success")
    assert marked.cancel_requested_at
    assert marked.state == JobState.RUNNING

    deadline = time.monotonic() + 5.0
    terminal = marked
    while time.monotonic() < deadline:
        terminal = read_job_record(root, jid)
        if terminal.state in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.LOST,
        }:
            break
        time.sleep(0.05)
    assert terminal.state == JobState.CANCELLED
    assert terminal.cancel_reason == "race-success"
    assert terminal.exit and terminal.exit.get("cancelled") is True
    assert terminal.exit.get("class") == "cancelled"


def test_transition_succeeded_after_cancel_requested_coerces(
    root: Path,
) -> None:
    """Direct store gate: succeeded/failed cannot land after cancel_requested."""
    from omg_cli.jobs.retry import classified_terminal_updates

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
        JobState.RUNNING,
        updates={
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "handle": f"fake:{rec.job_id}:pid={os.getpid()}",
        },
    )
    mark_cancel_requested(root, rec.job_id, reason="coerce")
    out = transition_job(
        root,
        rec.job_id,
        JobState.SUCCEEDED,
        updates=classified_terminal_updates(
            state=JobState.SUCCEEDED,
            exit_obj={"class": "success", "returncode": 0, "ok": True, "cancelled": False},
        ),
    )
    assert out.state == JobState.CANCELLED
    assert out.cancel_reason == "coerce"
    assert out.exit and out.exit.get("class") == "cancelled"
    assert out.exit.get("cancelled") is True
    assert out.retry_class == "manual_only"


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


def test_prove_job_processes_gone_uses_start_identity_when_record_clears_pids(
    root: Path,
) -> None:
    import os
    import subprocess

    from omg_cli.jobs.models import JobRecord
    from omg_cli.jobs.ownership import capture_identity, pid_alive
    from omg_cli.jobs.runtime import identities_from_start_record
    from omg_cli.jobs.store import job_dir, write_job_record

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ident = capture_identity(proc.pid, pgid=os.getpgid(proc.pid))
    rec = JobRecord(
        job_id="20260824T000000Z-cafecafe",
        created_at="2026-08-24T00:00:00Z",
        provider="grok",
        role="researcher",
        state=JobState.SUCCEEDED,
        pid=None,
        pgid=None,
        pid_starttime=None,
        result="artifacts/result.md",
    )
    (job_dir(root, rec.job_id) / "artifacts").mkdir(parents=True, exist_ok=True)
    write_job_record(root, rec)
    start_ns = SimpleNamespace(
        pid=ident.pid, pgid=ident.pgid, pid_starttime=ident.pid_starttime
    )
    extras = identities_from_start_record(start_ns)
    try:
        with pytest.raises(JobStoreError, match="still live"):
            prove_job_processes_gone(
                root, rec.job_id, timeout_s=0.1, extra_identities=extras
            )
        assert pid_alive(proc.pid)
    finally:
        if pid_alive(proc.pid):
            proc.kill()
            proc.wait(timeout=3)


def test_prove_job_processes_gone_missing_record_is_unproven(root: Path) -> None:
    with pytest.raises(JobStoreError, match="cannot prove process exit"):
        prove_job_processes_gone(root, "20260824T000000Z-aaaaaaaa")


def test_prove_missing_record_unproven_even_with_dead_extra(root: Path) -> None:
    from omg_cli.jobs.ownership import ProcessIdentity

    ident = ProcessIdentity(pid=999999, pgid=999999, pid_starttime="lstart:gone")
    with pytest.raises(JobStoreError, match="cannot prove process exit"):
        prove_job_processes_gone(
            root,
            "20260824T000000Z-aaaaaaaa",
            extra_identities=(ident,),
        )


def test_prove_does_not_replace_trusted_extra_with_forged_record(root: Path) -> None:
    import os
    import subprocess

    from omg_cli.jobs.models import JobRecord
    from omg_cli.jobs.ownership import capture_identity, pid_alive
    from omg_cli.jobs.store import write_job_record

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ident = capture_identity(proc.pid, pgid=os.getpgid(proc.pid))
    rec = JobRecord(
        job_id="20260824T000000Z-c0de0002",
        created_at="2026-08-24T00:00:00Z",
        provider="grok",
        role="researcher",
        state=JobState.SUCCEEDED,
        pid=ident.pid,
        pgid=ident.pgid,
        pid_starttime="lstart:forged",
        result="artifacts/result.md",
    )
    (job_dir(root, rec.job_id) / "artifacts").mkdir(parents=True, exist_ok=True)
    write_job_record(root, rec)
    try:
        with pytest.raises(JobStoreError, match="still live"):
            prove_job_processes_gone(
                root, rec.job_id, timeout_s=0.2, extra_identities=(ident,)
            )
        assert pid_alive(proc.pid)
    finally:
        if pid_alive(proc.pid):
            proc.kill()
            proc.wait(timeout=3)


def test_merge_identity_refreshes_pgid_when_starttime_matches() -> None:
    from omg_cli.jobs.ownership import ProcessIdentity, merge_identity

    original = ProcessIdentity(pid=5, pgid=5, pid_starttime="lstart:same")
    detached = ProcessIdentity(pid=5, pgid=9, pid_starttime="lstart:same")
    reused = ProcessIdentity(pid=5, pgid=9, pid_starttime="lstart:other")
    found = {5: original}
    assert merge_identity(found, detached) is True
    assert found[5].pgid == 9
    assert merge_identity(found, reused) is False
    assert found[5].pid_starttime == "lstart:same"


def test_pgid_member_identities_includes_same_session_child() -> None:
    import subprocess
    import sys

    from omg_cli.jobs.ownership import pgid_member_identities, pid_alive

    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, time\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        found = {ident.pid for ident in pgid_member_identities(os.getpgid(parent.pid))}
        assert parent.pid in found
        assert child_pid in found
    finally:
        if pid_alive(parent.pid):
            try:
                os.killpg(os.getpgid(parent.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                parent.kill()
            parent.wait(timeout=3)


def test_prove_job_processes_gone_sees_surviving_pgid_member(root: Path) -> None:
    """Leader PID gone is not proof the captured process group is empty."""
    import subprocess
    import sys

    from omg_cli.jobs.models import JobRecord
    from omg_cli.jobs.ownership import capture_identity, pid_alive
    from omg_cli.jobs.store import write_job_record

    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, time\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    child_pid = 0
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        ident = capture_identity(parent.pid, pgid=os.getpgid(parent.pid))
        rec = JobRecord(
            job_id="20260824T000000Z-c0de0001",
            created_at="2026-08-24T00:00:00Z",
            provider="grok",
            role="researcher",
            state=JobState.SUCCEEDED,
            pid=None,
            pgid=None,
            result="artifacts/result.md",
        )
        (job_dir(root, rec.job_id) / "artifacts").mkdir(parents=True, exist_ok=True)
        write_job_record(root, rec)
        os.kill(parent.pid, 9)
        parent.wait(timeout=3)
        assert not pid_alive(parent.pid)
        assert pid_alive(child_pid)
        with pytest.raises(JobStoreError, match="still live"):
            prove_job_processes_gone(
                root, rec.job_id, timeout_s=0.2, extra_identities=(ident,)
            )
    finally:
        if child_pid and pid_alive(child_pid):
            os.kill(child_pid, 9)
        if pid_alive(parent.pid):
            try:
                os.killpg(os.getpgid(parent.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                parent.kill()
            try:
                parent.wait(timeout=3)
            except Exception:
                pass


def test_child_identities_lists_direct_child() -> None:
    import subprocess
    import sys

    from omg_cli.jobs.ownership import child_identities, pid_alive

    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, time\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        found = {ident.pid for ident in child_identities(parent.pid)}
        assert child_pid in found
    finally:
        if pid_alive(parent.pid):
            try:
                os.killpg(os.getpgid(parent.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                parent.kill()
            parent.wait(timeout=3)


def test_cancel_does_not_signal_forged_succeeded_stamp(root: Path) -> None:
    import os
    import subprocess

    from omg_cli.jobs.models import JobRecord
    from omg_cli.jobs.ownership import capture_identity, pid_alive
    from omg_cli.jobs.store import write_job_record

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ident = capture_identity(proc.pid, pgid=os.getpgid(proc.pid))
    rec = JobRecord(
        job_id="20260824T000000Z-deadbeef",
        created_at="2026-08-24T00:00:00Z",
        provider="grok",
        role="researcher",
        state=JobState.SUCCEEDED,
        pid=ident.pid,
        pgid=ident.pgid,
        pid_starttime=ident.pid_starttime,
        result="artifacts/result.md",
    )
    from omg_cli.jobs.store import job_dir

    (job_dir(root, rec.job_id) / "artifacts").mkdir(parents=True, exist_ok=True)
    write_job_record(root, rec)
    try:
        with pytest.raises(JobStoreError, match="still live"):
            prove_job_processes_gone(root, rec.job_id, timeout_s=0.1)
        assert pid_alive(proc.pid)
        out = cancel_job(root, rec.job_id, grace_s=0.2)
        assert out.state == JobState.SUCCEEDED
        # Terminal stamps must not signal PIDs from job.json (may be forged).
        assert pid_alive(proc.pid)
        with pytest.raises(JobStoreError, match="still live"):
            prove_job_processes_gone(
                root,
                rec.job_id,
                timeout_s=0.1,
                extra_identities=(ident,),
            )
        assert pid_alive(proc.pid)
    finally:
        if pid_alive(proc.pid):
            proc.kill()
            proc.wait(timeout=3)


def test_sibling_isolation_cancel_a_keeps_b(root: Path) -> None:
    from omg_cli.jobs.runtime import _pid_alive

    prompt = _prompt(root, "shared")
    a = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=1.5,
    )
    b = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=1.5,
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


def test_antigravity_provider_preflight_without_binary_is_missing(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a discoverable agy binary, admission fails before materialization."""
    monkeypatch.setenv("PATH", "/nonexistent-omg-path")
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    prompt = _prompt(root)
    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="antigravity",
            role="researcher",
            prompt_file=prompt,
        )
    assert ei.value.code == "E_JOB_PROVIDER_MISSING"


def test_unknown_provider_still_refused(root: Path) -> None:
    prompt = _prompt(root)
    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="claude",
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


def test_launch_commit_failure_stamps_failed_not_starting(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After Popen succeeds, a failed running-commit must kill + stamp failed."""
    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    real_transition = runtime_mod.transition_job
    spawned: dict[str, int] = {}

    real_popen = runtime_mod.subprocess.Popen

    def tracking_popen(*a, **k):  # noqa: ANN001
        proc = real_popen(*a, **k)
        spawned["pid"] = int(proc.pid)
        return proc

    monkeypatch.setattr("omg_cli.jobs.runtime.subprocess.Popen", tracking_popen)

    def selective(
        project_root: Path,
        job_id: str,
        new_state: JobState,
        *,
        updates: dict | None = None,
    ):
        if new_state == JobState.RUNNING:
            raise JobStoreError("simulated commit failure", code="E_JOB_STORE")
        return real_transition(project_root, job_id, new_state, updates=updates)

    monkeypatch.setattr(runtime_mod, "transition_job", selective)

    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="fake",
            role="researcher",
            prompt_file=prompt,
            sleep_s=2.0,
        )
    assert ei.value.code == "E_JOB_LAUNCH"

    jobs = list_jobs(root)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["state"] == "failed"
    assert j["pid"] is None
    assert j["handle"] is None
    assert j["exit"]["class"] == "spawn_error"
    assert "commit failed" in (j.get("error_message") or "")
    assert "pid" in spawned
    time.sleep(0.1)
    assert not runtime_mod._pid_alive(spawned["pid"])


def test_raw_oserror_during_running_commit_kills_and_fails(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw OSError on RUNNING commit must kill child and stamp failed."""
    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    real_transition = runtime_mod.transition_job
    spawned: dict[str, int] = {}
    real_popen = runtime_mod.subprocess.Popen

    def tracking_popen(*a, **k):  # noqa: ANN001
        proc = real_popen(*a, **k)
        spawned["pid"] = int(proc.pid)
        return proc

    monkeypatch.setattr("omg_cli.jobs.runtime.subprocess.Popen", tracking_popen)

    def boom_oserror(
        project_root: Path,
        job_id: str,
        new_state: JobState,
        *,
        updates: dict | None = None,
    ):
        if new_state == JobState.RUNNING:
            raise OSError(28, "simulated No space left on device")
        return real_transition(project_root, job_id, new_state, updates=updates)

    monkeypatch.setattr(runtime_mod, "transition_job", boom_oserror)

    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="fake",
            role="researcher",
            prompt_file=prompt,
            sleep_s=1.5,
        )
    assert ei.value.code == "E_JOB_LAUNCH"
    j = list_jobs(root)[0]
    assert j["state"] == "failed"
    assert j["state"] != "starting"
    assert j["state"] != "running" or j.get("exit", {}).get("class") == "spawn_error"
    # Must not be stuck starting or durable running without failure stamp.
    assert j["state"] == "failed"
    assert "pid" in spawned
    time.sleep(0.1)
    assert not runtime_mod._pid_alive(spawned["pid"])


def test_post_commit_exception_kills_and_fails_running(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RUNNING commit lands, then exception on the way out → kill + running→failed."""
    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    real_transition = runtime_mod.transition_job
    spawned: dict[str, int] = {}
    real_popen = runtime_mod.subprocess.Popen

    def tracking_popen(*a, **k):  # noqa: ANN001
        proc = real_popen(*a, **k)
        spawned["pid"] = int(proc.pid)
        return proc

    monkeypatch.setattr("omg_cli.jobs.runtime.subprocess.Popen", tracking_popen)

    def commit_then_raise(
        project_root: Path,
        job_id: str,
        new_state: JobState,
        *,
        updates: dict | None = None,
    ):
        out = real_transition(project_root, job_id, new_state, updates=updates)
        if new_state == JobState.RUNNING:
            # Durable running is visible; simulate post-rename fsync/readback failure.
            raise OSError("simulated post-rename fsync failure")
        return out

    monkeypatch.setattr(runtime_mod, "transition_job", commit_then_raise)

    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="fake",
            role="researcher",
            prompt_file=prompt,
            sleep_s=1.5,
        )
    assert ei.value.code == "E_JOB_LAUNCH"
    final = read_job_record(root, list_jobs(root)[0]["job_id"])
    assert final.state == JobState.FAILED
    assert final.exit and final.exit.get("class") == "spawn_error"
    assert "pid" in spawned
    time.sleep(0.1)
    assert not runtime_mod._pid_alive(spawned["pid"])
    # Not left as running with a live child
    assert final.state != JobState.RUNNING


def test_collect_rejects_absolute_and_dotdot_descriptors(root: Path) -> None:
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=0.05,
    )
    wait_job(root, started.record.job_id, timeout_s=10.0)
    jid = started.record.job_id
    path = job_json_path(root, jid)

    # Absolute path escape
    data = json.loads(path.read_text(encoding="utf-8"))
    data["result"] = "/etc/passwd"
    data["artifacts"] = [{"path": "/etc/passwd", "kind": "result"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(JobStoreError) as ei:
        collect_job(root, jid)
    assert ei.value.code == "E_JOB_ARTIFACT"
    assert "escapes" in str(ei.value).lower() or "passwd" in str(ei.value)

    # Relative `..` escape
    data["result"] = "../outside.md"
    data["artifacts"] = [{"path": "../../etc/passwd", "kind": "result"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(JobStoreError) as ei2:
        collect_job(root, jid)
    assert ei2.value.code == "E_JOB_ARTIFACT"


def test_cancel_race_with_terminal_idempotent(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Racing succeeded stamp after cancel_requested is coerced to cancelled."""
    import signal
    import subprocess
    import sys

    from omg_cli.jobs import runtime as runtime_mod
    from omg_cli.jobs.models import TransitionError

    prompt = _prompt(root)
    # Real owned child (never pid/pgid=1); kill it first so cancel skips signals.
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = int(child.pid)
    pgid = int(os.getpgid(pid))
    assert pid > 1 and pgid > 1
    starttime = runtime_mod._probe_pid_starttime(pid)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        child.kill()
    child.wait(timeout=5)

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
        JobState.RUNNING,
        updates={
            "pid": pid,
            "pgid": pgid,
            "handle": f"fake:{rec.job_id}:pid={pid}",
            "pid_starttime": starttime,
        },
    )

    real_transition = runtime_mod.transition_job
    signals: list[int] = []

    def race_on_cancel(
        project_root: Path,
        job_id: str,
        new_state: JobState,
        *,
        updates: dict | None = None,
    ):
        if new_state == JobState.CANCELLED:
            # Runner races a success stamp after cancel_requested — store remaps
            # it to cancelled (persist-before-signal). Then cancel's own
            # transition sees already-terminal and raises.
            cur = read_job_record(project_root, job_id)
            if cur.state == JobState.RUNNING:
                stamped = real_transition(
                    project_root,
                    job_id,
                    JobState.SUCCEEDED,
                    updates={
                        "exit": {"class": "success", "returncode": 0, "ok": True},
                    },
                )
                assert stamped.state == JobState.CANCELLED
            raise TransitionError(
                "illegal job transition cancelled -> cancelled",
                code="E_JOB_TRANSITION",
            )
        return real_transition(project_root, job_id, new_state, updates=updates)

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append(int(signum))
        return False

    monkeypatch.setattr(runtime_mod, "transition_job", race_on_cancel)
    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)
    # Dead owned pid: cancel skip-kills, then hits the race on stamp.
    out = cancel_job(root, rec.job_id, reason="race", grace_s=0.05)
    assert signals == [], "dead-pid cancel must not signal"
    assert out.state == JobState.CANCELLED
    assert out.exit and out.exit.get("class") == "cancelled"
    assert out.exit.get("cancelled") is True


def test_immediate_child_exit_stamps_failed_never_running(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful Popen of a process that exits immediately → failed, not running."""
    import subprocess
    import sys

    prompt = _prompt(root)
    real_popen = subprocess.Popen

    def immediate_exit(*_a, **_k):  # noqa: ANN001
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(42)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    monkeypatch.setattr("omg_cli.jobs.runtime.subprocess.Popen", immediate_exit)

    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="fake",
            role="researcher",
            prompt_file=prompt,
            sleep_s=1.0,
        )
    assert ei.value.code == "E_JOB_LAUNCH"
    jobs = list_jobs(root)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "failed"
    assert jobs[0]["pid"] is None
    assert jobs[0]["handle"] is None
    assert jobs[0]["exit"]["returncode"] == 42
    assert jobs[0]["exit"]["class"] == "spawn_error"


def test_runner_never_transitions_to_running(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Child stamps only running→terminal; parent alone owns starting→running."""
    from omg_cli.jobs import runner as runner_mod
    from omg_cli.providers.models import ProviderRunResult

    prompt = _prompt(root)
    rec = create_job_dir(
        root,
        provider="fake",
        role="researcher",
        prompt_text=prompt.read_text(encoding="utf-8"),
        worker={"sleep_s": 0.01},
    )
    transition_job(root, rec.job_id, JobState.STARTING)
    # Parent commit (this test process is the "runner" PID).
    transition_job(
        root,
        rec.job_id,
        JobState.RUNNING,
        updates={
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "handle": f"fake:{rec.job_id}:pid={os.getpid()}",
        },
    )

    seen: list[JobState] = []
    real_transition = runner_mod.transition_job

    def spy(
        project_root: Path,
        job_id: str,
        new_state: JobState,
        *,
        updates: dict | None = None,
    ):
        seen.append(new_state)
        assert new_state != JobState.RUNNING, "child must not transition to running"
        return real_transition(project_root, job_id, new_state, updates=updates)

    monkeypatch.setattr(runner_mod, "transition_job", spy)

    class InstantFake:
        name = "fake"

        def discover_binary(self) -> str:
            return "fake"

        def probe_version(self, binary=None):  # noqa: ANN001
            raise NotImplementedError

        def probe_capabilities(self, binary=None):  # noqa: ANN001
            raise NotImplementedError

        def doctor(self, *, strict: bool = False):  # noqa: ANN001
            raise NotImplementedError

        def build_launch_envelope(self, request):  # noqa: ANN001
            raise NotImplementedError

        def run(self, request):  # noqa: ANN001
            return ProviderRunResult(
                ok=True,
                exit_class="success",
                returncode=0,
                output="ok\n",
                stdout="ok\n",
            )

    monkeypatch.setattr(runner_mod, "resolve_adapter", lambda _p: InstantFake())
    rc = runner_mod.run_job(root, rec.job_id)
    assert rc == 0
    assert JobState.RUNNING not in seen
    assert JobState.SUCCEEDED in seen
    assert read_job_record(root, rec.job_id).state == JobState.SUCCEEDED


def test_run_job_restores_env_after_inprocess_call(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process run_job must not leak OMG_JOB_* / OMG_PROJECT_ROOT into later tests."""
    from omg_cli.jobs import runner as runner_mod
    from omg_cli.providers.models import ProviderRunResult

    # Ensure previously unset.
    for key in (
        "OMG_JOB_ID",
        "OMG_JOB_DIR",
        "OMG_PROJECT_ROOT",
        "OMG_JOB_FAKE_FAIL",
        "OMG_JOB_FAKE_SLEEP",
    ):
        monkeypatch.delenv(key, raising=False)

    prior_project = os.environ.get("OMG_PROJECT_ROOT")
    prior_job_id = os.environ.get("OMG_JOB_ID")
    assert prior_project is None
    assert prior_job_id is None

    prompt = _prompt(root)
    rec = create_job_dir(
        root,
        provider="fake",
        role="researcher",
        prompt_text=prompt.read_text(encoding="utf-8"),
        worker={"sleep_s": 0.01, "fail": True},
    )
    transition_job(root, rec.job_id, JobState.STARTING)
    transition_job(
        root,
        rec.job_id,
        JobState.RUNNING,
        updates={
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "handle": f"fake:{rec.job_id}:pid={os.getpid()}",
        },
    )

    class InstantFake:
        name = "fake"

        def discover_binary(self) -> str:
            return "fake"

        def probe_version(self, binary=None):  # noqa: ANN001
            raise NotImplementedError

        def probe_capabilities(self, binary=None):  # noqa: ANN001
            raise NotImplementedError

        def doctor(self, *, strict: bool = False):  # noqa: ANN001
            raise NotImplementedError

        def build_launch_envelope(self, request):  # noqa: ANN001
            raise NotImplementedError

        def run(self, request):  # noqa: ANN001
            # Env must be visible during the run.
            assert os.environ.get("OMG_JOB_ID") == rec.job_id
            assert os.environ.get("OMG_PROJECT_ROOT") == str(root)
            return ProviderRunResult(
                ok=True,
                exit_class="success",
                returncode=0,
                output="ok\n",
                stdout="ok\n",
            )

    monkeypatch.setattr(runner_mod, "resolve_adapter", lambda _p: InstantFake())
    assert runner_mod.run_job(root, rec.job_id) == 0

    assert os.environ.get("OMG_JOB_ID") is None
    assert os.environ.get("OMG_JOB_DIR") is None
    assert os.environ.get("OMG_PROJECT_ROOT") is None
    assert os.environ.get("OMG_JOB_FAKE_FAIL") is None
    assert os.environ.get("OMG_JOB_FAKE_SLEEP") is None


def test_run_job_restores_preexisting_env(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing OMG_PROJECT_ROOT is restored after in-process run_job."""
    from omg_cli.jobs import runner as runner_mod
    from omg_cli.providers.models import ProviderRunResult

    sentinel = "/tmp/omg-job-env-sentinel-should-restore"
    monkeypatch.setenv("OMG_PROJECT_ROOT", sentinel)
    monkeypatch.setenv("OMG_JOB_ID", "preexisting-id")

    prompt = _prompt(root)
    rec = create_job_dir(
        root,
        provider="fake",
        role="researcher",
        prompt_text=prompt.read_text(encoding="utf-8"),
        worker={"sleep_s": 0.01},
    )
    transition_job(root, rec.job_id, JobState.STARTING)
    transition_job(
        root,
        rec.job_id,
        JobState.RUNNING,
        updates={
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "handle": f"fake:{rec.job_id}:pid={os.getpid()}",
        },
    )

    class InstantFake:
        name = "fake"

        def discover_binary(self) -> str:
            return "fake"

        def probe_version(self, binary=None):  # noqa: ANN001
            raise NotImplementedError

        def probe_capabilities(self, binary=None):  # noqa: ANN001
            raise NotImplementedError

        def doctor(self, *, strict: bool = False):  # noqa: ANN001
            raise NotImplementedError

        def build_launch_envelope(self, request):  # noqa: ANN001
            raise NotImplementedError

        def run(self, request):  # noqa: ANN001
            return ProviderRunResult(
                ok=True,
                exit_class="success",
                returncode=0,
                output="ok\n",
                stdout="ok\n",
            )

    monkeypatch.setattr(runner_mod, "resolve_adapter", lambda _p: InstantFake())
    assert runner_mod.run_job(root, rec.job_id) == 0
    assert os.environ.get("OMG_PROJECT_ROOT") == sentinel
    assert os.environ.get("OMG_JOB_ID") == "preexisting-id"


def test_barrier_blocks_adapter_until_running_committed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adapter.run must not run while job is still starting (no threads — fork-safe)."""
    from omg_cli.jobs import runner as runner_mod
    from omg_cli.providers.models import ProviderRunResult

    prompt = _prompt(root)
    rec = create_job_dir(
        root,
        provider="fake",
        role="researcher",
        prompt_text=prompt.read_text(encoding="utf-8"),
        worker={"ready_timeout_s": 5.0},
    )
    transition_job(root, rec.job_id, JobState.STARTING)

    events: list[str] = []
    run_states: list[str] = []
    committed = {"done": False}

    class GatedFake:
        name = "fake"

        def discover_binary(self) -> str:
            return "fake"

        def probe_version(self, binary=None):  # noqa: ANN001
            raise NotImplementedError

        def probe_capabilities(self, binary=None):  # noqa: ANN001
            raise NotImplementedError

        def doctor(self, *, strict: bool = False):  # noqa: ANN001
            raise NotImplementedError

        def build_launch_envelope(self, request):  # noqa: ANN001
            raise NotImplementedError

        def run(self, request):  # noqa: ANN001
            cur = read_job_record(root, rec.job_id)
            run_states.append(cur.state.value)
            events.append("adapter.run")
            assert cur.state == JobState.RUNNING
            assert cur.pid == os.getpid()
            assert cur.handle
            return ProviderRunResult(
                ok=True,
                exit_class="success",
                returncode=0,
                output="gated\n",
                stdout="gated\n",
            )

    def sleep_then_commit(_seconds: float) -> None:
        # First barrier poll sleep: parent commits running before Adapter.run.
        if not committed["done"]:
            committed["done"] = True
            events.append("parent.commit")
            transition_job(
                root,
                rec.job_id,
                JobState.RUNNING,
                updates={
                    "pid": os.getpid(),
                    "pgid": os.getpgid(0),
                    "handle": f"fake:{rec.job_id}:pid={os.getpid()}",
                },
            )

    monkeypatch.setattr(runner_mod, "resolve_adapter", lambda _p: GatedFake())
    monkeypatch.setattr(runner_mod.time, "sleep", sleep_then_commit)

    rc = runner_mod.run_job(root, rec.job_id)
    assert rc == 0
    assert committed["done"] is True
    assert events == ["parent.commit", "adapter.run"]
    assert run_states == ["running"]


def test_cancel_before_handle_commit_no_running_without_handle(root: Path) -> None:
    """launch=False leaves starting; cancel → cancelled with no durable running handle."""
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        launch=False,
    )
    assert started.launched is False
    assert started.record.state == JobState.STARTING
    assert started.record.pid is None
    assert started.record.handle is None

    cancelled = cancel_job(root, started.record.job_id, reason="pre-handle")
    assert cancelled.state == JobState.CANCELLED
    assert cancelled.pid is None
    assert cancelled.handle is None
    # Never became running
    disk = json.loads(job_json_path(root, started.record.job_id).read_text(encoding="utf-8"))
    assert disk["state"] == "cancelled"
    assert disk.get("pid") is None


def test_cancel_during_uncommitted_window_aborts_running_commit(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel while still starting: parent must not leave running; orphan killed."""
    import subprocess
    import sys

    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    real_transition = runtime_mod.transition_job
    real_popen = subprocess.Popen

    # Long-sleep child so it stays alive until kill after commit failure.
    def long_child(*_a, **_k):  # noqa: ANN001
        return real_popen(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    monkeypatch.setattr("omg_cli.jobs.runtime.subprocess.Popen", long_child)

    def cancel_then_running(
        project_root: Path,
        job_id: str,
        new_state: JobState,
        *,
        updates: dict | None = None,
    ):
        if new_state == JobState.RUNNING:
            # Simulate concurrent cancel winning the uncommitted window.
            real_transition(
                project_root,
                job_id,
                JobState.CANCELLED,
                updates={
                    "cancel_reason": "inject",
                    "exit": {"class": "cancelled", "returncode": -1},
                },
            )
            from omg_cli.jobs.models import TransitionError

            raise TransitionError(
                "illegal job transition cancelled -> running",
                code="E_JOB_TRANSITION",
            )
        return real_transition(project_root, job_id, new_state, updates=updates)

    monkeypatch.setattr(runtime_mod, "transition_job", cancel_then_running)

    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="fake",
            role="researcher",
            prompt_file=prompt,
            sleep_s=1.5,
        )
    assert ei.value.code == "E_JOB_LAUNCH"
    final = read_job_record(root, list_jobs(root)[0]["job_id"])
    assert final.state == JobState.CANCELLED
    assert final.pid is None or final.handle is None or final.state != JobState.RUNNING
    # Durable state is cancelled — never running with a live handle from this start.
    assert final.state != JobState.RUNNING


def test_pid_alive_fail_open_on_ps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """After kill(0) succeeds, ps TimeoutExpired must not mark the pid dead."""
    import subprocess

    from omg_cli.jobs import runtime as runtime_mod

    live = os.getpid()
    real_run = subprocess.run

    def _ps_timeout(*args, **kwargs):  # noqa: ANN001, ANN002
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]) == "ps":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _ps_timeout)
    assert runtime_mod._pid_alive(live) is True


def test_cancel_still_signals_when_ps_probe_times_out(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_job must not skip kill when _pid_alive's ps STAT probe times out."""
    import subprocess

    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        sleep_s=2.0,
    )
    pid = int(started.record.pid or 0)
    assert pid > 0
    assert runtime_mod._pid_alive(pid)
    recorded_fp = started.record.pid_starttime

    real_run = subprocess.run
    signals_sent: list[int] = []
    real_kill_pgid = runtime_mod._kill_pgid

    def _ps_stat_timeout(*args, **kwargs):  # noqa: ANN001, ANN002
        cmd = args[0] if args else kwargs.get("args")
        # Only the zombie STAT probe times out; leave other ps calls alone.
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 2
            and str(cmd[0]) == "ps"
            and any(str(part) == "stat=" or str(part).startswith("stat=") for part in cmd)
        ):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)
        return real_run(*args, **kwargs)

    def _spy_kill(pgid: int, signum: int) -> bool:
        signals_sent.append(int(signum))
        return real_kill_pgid(pgid, signum)

    # Keep ownership fingerprint stable even if a parallel ps probe flakes.
    monkeypatch.setattr(
        runtime_mod,
        "_probe_pid_starttime",
        lambda _pid: recorded_fp,
    )
    monkeypatch.setattr(subprocess, "run", _ps_stat_timeout)
    monkeypatch.setattr(runtime_mod, "_kill_pgid", _spy_kill)

    rec = cancel_job(root, started.record.job_id, reason="ps-timeout", grace_s=0.3)
    assert signals_sent, "cancel must still send signals when STAT probe times out"
    assert rec.state == JobState.CANCELLED
    time.sleep(0.15)
    monkeypatch.setattr(subprocess, "run", real_run)
    assert not runtime_mod._pid_alive(pid)


def test_cleanup_does_not_kill_unregistered_runner_cmdline(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown must not kill processes merely matching omg_cli.jobs.runner in argv."""
    import signal
    import subprocess
    import sys

    from tests import jobs_testutil

    # Dummy whose cmdline contains the runner module path but was never registered.
    decoy = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)  # omg_cli.jobs.runner decoy",
        ],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert decoy.poll() is None
        # Ensure spawn registry is empty (no job.json union path).
        jobs_testutil._SPAWNED.clear()
        jobs_testutil.kill_registered_jobs()
        time.sleep(0.05)
        assert decoy.poll() is None, "unregistered decoy must survive scoped cleanup"
        try:
            os.kill(decoy.pid, 0)
        except ProcessLookupError as exc:  # pragma: no cover
            raise AssertionError("decoy was killed by scoped cleanup") from exc
    finally:
        try:
            os.killpg(os.getpgid(decoy.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                decoy.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            decoy.wait(timeout=2)
        except Exception:
            pass


def test_cancel_fingerprint_mismatch_does_not_signal(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded pid_starttime mismatch → E_JOB_PID_REUSED, no kill signals."""
    import subprocess
    import sys

    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = int(child.pid)
    pgid = int(os.getpgid(pid))
    assert pid > 1
    live_fp = runtime_mod._probe_pid_starttime(pid)
    assert live_fp  # need a real fingerprint to mismatch against
    signals: list[int] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append(int(signum))
        return False

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)

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
        JobState.RUNNING,
        updates={
            "pid": pid,
            "pgid": pgid,
            "handle": f"fake:{rec.job_id}:pid={pid}",
            "pid_starttime": "proc:999999999999",  # deliberate mismatch
        },
    )
    try:
        with pytest.raises(JobStoreError) as ei:
            cancel_job(root, rec.job_id, reason="reuse", grace_s=0.05)
        assert ei.value.code == "E_JOB_PID_REUSED"
        assert signals == []
        assert runtime_mod._pid_alive(pid)
        still = read_job_record(root, rec.job_id)
        assert still.state == JobState.RUNNING
    finally:
        import signal as signal_mod

        try:
            os.killpg(pgid, signal_mod.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            child.kill()
        try:
            child.wait(timeout=2)
        except Exception:
            pass


def test_cancel_refuses_pid_one(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pid=1 / pgid=1 must never be signalled (E_JOB_PID_REUSED)."""
    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    signals: list[tuple[int, int]] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append((int(pgid_arg), int(signum)))
        return False

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)
    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda _pid: True)

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
        JobState.RUNNING,
        updates={
            "pid": 1,
            "pgid": 1,
            "handle": "fake:forbidden",
            "pid_starttime": None,
        },
    )
    with pytest.raises(JobStoreError) as ei:
        cancel_job(root, rec.job_id, reason="init", grace_s=0.05)
    assert ei.value.code == "E_JOB_PID_REUSED"
    assert signals == []
    assert read_job_record(root, rec.job_id).state == JobState.RUNNING


def test_cancel_refuses_pgid_one_even_if_pid_ok(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pid>1 with pgid=1 must never be signalled."""
    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    signals: list[tuple[int, int]] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append((int(pgid_arg), int(signum)))
        return False

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)
    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda _pid: True)
    # Bypass getpgid so we exercise the pgid<=1 gate first.
    monkeypatch.setattr(os, "getpgid", lambda _pid: 1)

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
        JobState.RUNNING,
        updates={
            "pid": 4242,
            "pgid": 1,
            "handle": "fake:bad-pgid",
            "pid_starttime": None,
        },
    )
    with pytest.raises(JobStoreError) as ei:
        cancel_job(root, rec.job_id, reason="bad-pgid", grace_s=0.05)
    assert ei.value.code == "E_JOB_PID_REUSED"
    assert signals == []
    assert read_job_record(root, rec.job_id).state == JobState.RUNNING


def test_cancel_live_pgid_mismatch_does_not_signal(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live getpgid(pid) ≠ recorded pgid → E_JOB_PGID_MISMATCH, no signals."""
    import subprocess
    import sys

    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = int(child.pid)
    real_pgid = int(os.getpgid(pid))
    assert pid > 1 and real_pgid > 1
    wrong_pgid = real_pgid + 777
    signals: list[int] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append(int(signum))
        return False

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)

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
        JobState.RUNNING,
        updates={
            "pid": pid,
            "pgid": wrong_pgid,
            "handle": f"fake:{rec.job_id}:pid={pid}",
            "pid_starttime": None,
        },
    )
    try:
        with pytest.raises(JobStoreError) as ei:
            cancel_job(root, rec.job_id, reason="pgid-drift", grace_s=0.05)
        assert ei.value.code == "E_JOB_PGID_MISMATCH"
        assert signals == []
        assert runtime_mod._pid_alive(pid)
        assert read_job_record(root, rec.job_id).state == JobState.RUNNING
    finally:
        import signal as signal_mod

        try:
            os.killpg(real_pgid, signal_mod.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            child.kill()
        try:
            child.wait(timeout=2)
        except Exception:
            pass


def test_cancel_revalidates_before_sigkill_after_term(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership OK before SIGTERM; mismatch before SIGKILL → no SIGKILL."""
    import signal as signal_mod

    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        ignore_sigterm=True,
        sleep_s=2.5,
    )
    pid = int(started.record.pid or 0)
    pgid = int(started.record.pgid or 0)
    assert pid > 1 and pgid > 1
    time.sleep(0.25)  # allow SIG_IGN install

    signals: list[int] = []
    assert_calls = {"n": 0}
    real_assert = runtime_mod._assert_cancel_ownership
    real_kill = runtime_mod._kill_pgid

    def counting_assert(record, target_pid, target_pgid):  # noqa: ANN001
        assert_calls["n"] += 1
        if assert_calls["n"] == 1:
            outcome = real_assert(record, target_pid, target_pgid)
            assert outcome is runtime_mod.CancelOwnership.OK
            return outcome
        raise JobStoreError(
            "simulated pre-KILL fingerprint mismatch",
            code="E_JOB_PID_REUSED",
        )

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append(int(signum))
        return real_kill(pgid_arg, signum)

    monkeypatch.setattr(runtime_mod, "_assert_cancel_ownership", counting_assert)
    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)

    with pytest.raises(JobStoreError) as ei:
        cancel_job(root, started.record.job_id, reason="pre-kill", grace_s=0.35)
    assert ei.value.code == "E_JOB_PID_REUSED"
    assert signal_mod.SIGTERM in signals
    assert signal_mod.SIGKILL not in signals
    assert assert_calls["n"] >= 2
    # Child may still be alive (TERM ignored); clean up exactly.
    try:
        os.killpg(pgid, signal_mod.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    time.sleep(0.1)


def test_cancel_ownership_gone_skips_all_signals(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outer alive True but ownership returns GONE → no SIGTERM/SIGKILL."""
    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    signals: list[int] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append(int(signum))
        return False

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)
    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        runtime_mod,
        "_assert_cancel_ownership",
        lambda *_a, **_k: runtime_mod.CancelOwnership.GONE,
    )

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
        JobState.RUNNING,
        updates={
            "pid": 4242,
            "pgid": 4242,
            "handle": "fake:gone",
            "pid_starttime": None,
        },
    )
    out = cancel_job(root, rec.job_id, reason="gone", grace_s=0.05)
    assert signals == [], "GONE must never call _kill_pgid"
    assert out.state == JobState.CANCELLED


def test_cancel_getpgid_lookup_error_is_gone_no_signal(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """getpgid ProcessLookupError → GONE, no signal, cancel still stamps."""
    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    signals: list[int] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append(int(signum))
        return False

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)
    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda _pid: True)

    def boom_getpgid(_pid: int) -> int:
        raise ProcessLookupError("simulated gone between alive and getpgid")

    monkeypatch.setattr(os, "getpgid", boom_getpgid)

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
        JobState.RUNNING,
        updates={
            "pid": 4243,
            "pgid": 4243,
            "handle": "fake:lookup-gone",
            "pid_starttime": None,
        },
    )
    out = cancel_job(root, rec.job_id, reason="lookup-gone", grace_s=0.05)
    assert signals == []
    assert out.state == JobState.CANCELLED


def test_cancel_provisional_cancelled_stamp_requires_disappearance(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CANCELLED stamped while runner still alive must not return success.

    Simulates the runner writing CANCELLED while ``_pid_alive`` stays True and
    ``_wait_until_gone`` returns False — cancel_job must raise
    ``E_JOB_CANCEL_UNPROVEN`` (or force-kill to real disappearance), never
    treat the durable stamp alone as proof.
    """
    import signal as signal_mod

    from omg_cli.jobs import runtime as runtime_mod

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
        JobState.RUNNING,
        updates={
            "pid": 4242,
            "pgid": 4242,
            "handle": f"fake:{rec.job_id}:pid=4242",
            "pid_starttime": None,
            "provider_process": {
                "state": "exited",  # mark_provider_exited cleared "bound"
                "pid": 4243,
                "pgid": 4243,
                "pid_starttime": None,
                "handle": "provider:x",
                "bound_at": "t0",
                "exited_at": "t1",
            },
        },
    )

    # Mid-cancel: runner stamps CANCELLED while processes still "alive".
    def stamp_cancelled_on_mark(*_a, **_k):
        from omg_cli.jobs.store import transition_job as real_tj

        # Already running → cancelled (provisional stamp).
        return real_tj(
            root,
            rec.job_id,
            JobState.CANCELLED,
            updates={
                "cancel_reason": "runner",
                "exit": {"class": "cancelled", "cancelled": True},
            },
        )

    monkeypatch.setattr(runtime_mod, "mark_cancel_requested", stamp_cancelled_on_mark)
    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(runtime_mod, "_wait_until_gone", lambda *_a, **_k: False)
    monkeypatch.setattr(
        runtime_mod,
        "_assert_cancel_ownership",
        lambda *_a, **_k: runtime_mod.CancelOwnership.OK,
    )
    signals: list[tuple[int, int]] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append((int(pgid_arg), int(signum)))
        return True

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)

    with pytest.raises(JobStoreError) as ei:
        cancel_job(root, rec.job_id, reason="provisional", grace_s=0.0)
    assert ei.value.code == "E_JOB_CANCEL_UNPROVEN"
    # Full inner-then-outer force sequence: outer SIGKILL must still run even
    # when inner wait_until_gone fails (must not raise before outer force).
    assert (4243, signal_mod.SIGKILL) in signals, (
        f"expected inner provider SIGKILL; got {signals}"
    )
    assert (4242, signal_mod.SIGKILL) in signals, (
        f"expected outer runner SIGKILL after inner wait failure; got {signals}"
    )


def test_cancel_inner_wait_failure_still_force_kills_outer(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inner SIGKILL wait failure must not skip outer force-kill."""
    import signal as signal_mod

    from omg_cli.jobs import runtime as runtime_mod

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
        JobState.RUNNING,
        updates={
            "pid": 5252,
            "pgid": 5252,
            "handle": f"fake:{rec.job_id}:pid=5252",
            "pid_starttime": None,
            "provider_process": {
                "state": "bound",
                "pid": 5253,
                "pgid": 5253,
                "pid_starttime": None,
                "handle": "provider:y",
                "bound_at": "t0",
                "exited_at": None,
            },
        },
    )

    killed_outer = {"done": False}
    signals: list[tuple[int, int]] = []

    def selective_wait(pid: int, *, timeout_s: float = 2.0, poll_s: float = 0.05) -> bool:
        del timeout_s, poll_s
        # Outer only disappears after its force SIGKILL; inner never does.
        return int(pid) == 5252 and killed_outer["done"]

    def pid_alive_after_outer_kill(pid: int) -> bool:
        if int(pid) == 5252 and killed_outer["done"]:
            return False
        return True

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append((int(pgid_arg), int(signum)))
        if int(pgid_arg) == 5252 and int(signum) == int(signal_mod.SIGKILL):
            killed_outer["done"] = True
        return True

    monkeypatch.setattr(runtime_mod, "_wait_until_gone", selective_wait)
    monkeypatch.setattr(runtime_mod, "_pid_alive", pid_alive_after_outer_kill)
    monkeypatch.setattr(
        runtime_mod,
        "_assert_cancel_ownership",
        lambda *_a, **_k: runtime_mod.CancelOwnership.OK,
    )
    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)

    with pytest.raises(JobStoreError) as ei:
        cancel_job(root, rec.job_id, reason="inner-stuck", grace_s=0.0)
    assert ei.value.code == "E_JOB_CANCEL_UNPROVEN"
    assert (5253, signal_mod.SIGKILL) in signals
    assert (5252, signal_mod.SIGKILL) in signals, (
        f"outer SIGKILL must run after inner wait failure; got {signals}"
    )
    # Inner force before outer force.
    inner_i = signals.index((5253, signal_mod.SIGKILL))
    outer_i = signals.index((5252, signal_mod.SIGKILL))
    assert inner_i < outer_i, f"inner-then-outer order required; got {signals}"


# ---------------------------------------------------------------------------
# #68 PR3 — explicit retry / attempt budget
# ---------------------------------------------------------------------------


def test_retry_requires_exact_next_attempt(root: Path) -> None:
    from omg_cli.jobs.runtime import retry_job

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, timed_out = wait_job(root, started.record.job_id, timeout_s=15)
    assert timed_out is False
    assert terminal.state == JobState.FAILED
    with pytest.raises(JobStoreError) as ei:
        retry_job(root, terminal.job_id, attempt=terminal.attempt + 2)
    assert ei.value.code == "E_JOB_RETRY_ATTEMPT"
    with pytest.raises(JobStoreError) as ei2:
        retry_job(root, terminal.job_id, attempt=terminal.attempt)
    assert ei2.value.code == "E_JOB_RETRY_ATTEMPT"


def test_retry_budget_exhaustion_fail_closed(root: Path) -> None:
    from omg_cli.jobs.runtime import retry_job

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=1,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    assert terminal.attempt_budget == 1
    with pytest.raises(JobStoreError) as ei:
        retry_job(root, terminal.job_id, attempt=2)
    assert ei.value.code == "E_JOB_RETRY_BUDGET"


def test_retry_archives_attempt_history(root: Path) -> None:
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import (
        _ATTEMPT_ARCHIVE_COMPLETE_NAME,
        _ATTEMPT_ARCHIVE_COMPLETE_VERSION,
        attempt_dir,
    )

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    assert terminal.retry_class == "automatic"
    retried = retry_job(root, terminal.job_id, attempt=2)
    assert retried.record.attempt == 2
    archive = attempt_dir(root, terminal.job_id, 1)
    assert (archive / "attempt.json").is_file()
    assert (archive / _ATTEMPT_ARCHIVE_COMPLETE_NAME).is_file()
    assert (archive / "stdout.jsonl").is_file()
    assert (archive / "events.jsonl").is_file()
    snap = json.loads((archive / "attempt.json").read_text(encoding="utf-8"))
    assert snap["attempt"] == 1
    assert snap["state"] == "failed"
    marker = json.loads(
        (archive / _ATTEMPT_ARCHIVE_COMPLETE_NAME).read_text(encoding="utf-8")
    )
    assert marker["archived_attempt"] == 1
    assert marker["version"] == _ATTEMPT_ARCHIVE_COMPLETE_VERSION
    # Active prompt.md immutable at job root.
    assert (job_dir(root, terminal.job_id) / "prompt.md").is_file()
    wait_job(root, retried.record.job_id, timeout_s=15)


def test_retry_recovers_from_partial_attempt_archive(root: Path) -> None:
    """Incomplete attempts/NNNN/ must not permanently block retry (E_JOB_RETRY_ARCHIVE)."""
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import (
        _ATTEMPT_ARCHIVE_COMPLETE_NAME,
        attempt_dir,
        attempts_dir,
    )

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    assert terminal.state == JobState.FAILED

    # Simulate crash after creating final-looking attempts/0001/ but before
    # attempt.json publish (legacy partial / incomplete archive).
    partial = attempt_dir(root, terminal.job_id, 1)
    partial.mkdir(parents=True)
    (partial / "artifacts").mkdir()
    (partial / "stdout.jsonl").write_text("partial\n", encoding="utf-8")
    assert not (partial / "attempt.json").is_file()
    assert not (partial / _ATTEMPT_ARCHIVE_COMPLETE_NAME).is_file()

    retried = retry_job(root, terminal.job_id, attempt=2)
    assert retried.record.attempt == 2
    archive = attempt_dir(root, terminal.job_id, 1)
    assert (archive / "attempt.json").is_file()
    assert (archive / _ATTEMPT_ARCHIVE_COMPLETE_NAME).is_file()
    snap = json.loads((archive / "attempt.json").read_text(encoding="utf-8"))
    assert snap["archived_attempt"] == 1
    assert snap["state"] == "failed"
    # Staging leftovers must not look like published attempts.
    leftover = [
        p
        for p in attempts_dir(root, terminal.job_id).iterdir()
        if p.name.startswith(".staging-")
    ]
    assert leftover == []
    wait_job(root, retried.record.job_id, timeout_s=15)


def test_retry_recovers_legacy_attempt_json_without_complete_marker(
    root: Path,
) -> None:
    """attempt.json alone must not classify an archive complete (REV4 P0).

    Legacy mid-copy could leave matching ``archived_attempt`` while later
    archive files / the completion marker are missing. That must be treated as
    incomplete/recoverable — never as idempotent reuse that would wipe intact
    active evidence without a real archive.
    """
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import (
        _ATTEMPT_ARCHIVE_COMPLETE_NAME,
        _attempt_archive_complete,
        attempt_dir,
    )

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    assert terminal.state == JobState.FAILED

    jdir = job_dir(root, terminal.job_id)
    active_stdout = (jdir / "stdout.jsonl").read_text(encoding="utf-8")
    assert active_stdout  # intact active evidence to protect

    # Legacy partial: attempt.json published early, later file + marker absent.
    partial = attempt_dir(root, terminal.job_id, 1)
    partial.mkdir(parents=True)
    (partial / "artifacts").mkdir()
    (partial / "attempt.json").write_text(
        json.dumps(
            {
                "archived_attempt": 1,
                "attempt": 1,
                "state": "failed",
                "job_id": terminal.job_id,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (partial / "stdout.jsonl").write_text("stale-partial-only\n", encoding="utf-8")
    # Deliberately omit events.jsonl and archive.complete.
    assert not (partial / _ATTEMPT_ARCHIVE_COMPLETE_NAME).is_file()
    assert not (partial / "events.jsonl").is_file()
    assert not _attempt_archive_complete(partial, 1)

    retried = retry_job(root, terminal.job_id, attempt=2)
    assert retried.record.attempt == 2

    archive = attempt_dir(root, terminal.job_id, 1)
    assert (archive / _ATTEMPT_ARCHIVE_COMPLETE_NAME).is_file()
    assert (archive / "attempt.json").is_file()
    assert (archive / "events.jsonl").is_file()
    # Active evidence was archived (not wiped under a false-complete reuse).
    archived_stdout = (archive / "stdout.jsonl").read_text(encoding="utf-8")
    assert archived_stdout == active_stdout
    assert "stale-partial-only" not in archived_stdout
    wait_job(root, retried.record.job_id, timeout_s=15)


def test_retry_preserves_previous_artifacts(root: Path) -> None:
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import attempt_dir

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        large_output=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    # Large result should exist before retry.
    assert (job_dir(root, terminal.job_id) / "artifacts" / "result.md").is_file() or any(
        (a.get("path") or "").endswith("result.md") for a in (terminal.artifacts or [])
    )
    retry_job(root, terminal.job_id, attempt=2)
    archived = attempt_dir(root, terminal.job_id, 1) / "artifacts" / "result.md"
    assert archived.is_file()
    assert archived.stat().st_size >= 100 * 1024
    # Active artifacts cleared for next attempt (history not overwritten).
    active_art = job_dir(root, terminal.job_id) / "artifacts" / "result.md"
    assert not active_art.is_file()


def test_retry_reuses_existing_runner_path(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs import runtime as runtime_mod
    from omg_cli.jobs.runtime import retry_job

    calls: list[str] = []
    real_launch = runtime_mod.launch_job_runner

    def _wrap(project_root: Path, job_id: str, **kwargs: object):
        calls.append(str(job_id))
        return real_launch(project_root, job_id, **kwargs)

    monkeypatch.setattr(runtime_mod, "launch_job_runner", _wrap)
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    assert started.record.job_id in calls
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    calls.clear()
    retried = retry_job(root, terminal.job_id, attempt=2)
    assert calls == [retried.record.job_id]
    wait_job(root, retried.record.job_id, timeout_s=15)


def test_retry_rejects_internal_acp_provider(root: Path) -> None:
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import job_lock, read_job_record

    prompt = _prompt(root)
    # Materialize a fake job then forge provider to internal ACP (unit only).
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
        launch=False,
    )

    with job_lock(root, started.record.job_id):
        rec = read_job_record(root, started.record.job_id)
        data = rec.to_dict()
        data["provider"] = "grok-acp-session"
        data["state"] = "failed"
        data["exit"] = {"class": "nonzero", "retryable": True, "ok": False}
        data["attempt"] = 1
        path = job_json_path(root, rec.job_id)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(JobStoreError) as ei:
        retry_job(root, started.record.job_id, attempt=2)
    assert ei.value.code == "E_JOB_PROVIDER_INTERNAL"


def test_retry_rejects_live_process_identity(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs.ownership import IdentityProbeOutcome
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import job_lock, write_job_record

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)

    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.pid = 424242
        rec.pgid = 424242
        rec.pid_starttime = "fingerprint"
        write_job_record(root, rec)

    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.LIVE,
    )
    monkeypatch.setattr(
        "omg_cli.jobs.runtime._wait_until_gone",
        lambda *a, **k: False,
    )
    with pytest.raises(JobStoreError) as ei:
        retry_job(root, terminal.job_id, attempt=2)
    assert ei.value.code == "E_JOB_RETRY_LIVE"


def test_retry_auth_blocked_is_manual_only() -> None:
    from omg_cli.jobs.retry import classify_retry

    cls, reason = classify_retry(
        state=JobState.FAILED,
        exit_obj={"class": "auth_blocked", "ok": False, "retryable": False},
    )
    assert cls == "manual_only"
    assert reason == "auth_blocked"


# ---------------------------------------------------------------------------
# #68 PR3 P0 — GC race / identity / binding + retry_class fail-closed
# ---------------------------------------------------------------------------


def test_gc_retry_race_never_deletes_requeued_job(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If retry requeues under the lock before GC quarantines, GC must skip."""
    from datetime import datetime, timedelta, timezone

    from omg_cli.jobs.runtime import gc_jobs, retry_job
    from omg_cli.jobs.store import job_dir, job_lock, write_job_record

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    assert terminal.state == JobState.FAILED
    jid = terminal.job_id

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with job_lock(root, jid):
        rec = read_job_record(root, jid)
        rec.terminal_at = old
        write_job_record(root, rec)

    # Retry first (wins the lock transaction), then GC must see nonterminal.
    retried = retry_job(root, jid, attempt=2, launch=False)
    assert retried.record.state == JobState.STARTING
    assert job_dir(root, jid).is_dir()

    result = gc_jobs(root, retention_days=0)
    assert jid not in result.deleted
    assert job_dir(root, jid).is_dir()
    assert (job_dir(root, jid) / "job.json").is_file()
    # Requeued jobs leave gc_candidates (nonterminal) — may not appear in skipped.
    cur = read_job_record(root, jid)
    assert cur.state not in {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.LOST,
    }


def test_gc_never_deletes_terminal_job_with_live_identity(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    from omg_cli.jobs.ownership import IdentityProbeOutcome
    from omg_cli.jobs.runtime import gc_jobs
    from omg_cli.jobs.store import job_dir, job_lock, write_job_record

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=1,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    jid = terminal.job_id

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with job_lock(root, jid):
        rec = read_job_record(root, jid)
        rec.terminal_at = old
        rec.pid = 424242
        rec.pgid = 424242
        rec.pid_starttime = "fingerprint"
        write_job_record(root, rec)

    monkeypatch.setattr("omg_cli.jobs.runtime._pid_alive", lambda pid: True)
    monkeypatch.setattr(
        "omg_cli.jobs.runtime._probe_gc_identity",
        lambda *_a, **_k: IdentityProbeOutcome.LIVE,
    )

    result = gc_jobs(root, retention_days=0)
    assert jid not in result.deleted
    assert job_dir(root, jid).is_dir()
    assert any(
        s.get("job_id") == jid and str(s.get("reason", "")).startswith("live_identity")
        for s in result.skipped
    )


def test_gc_live_identity_fingerprint_probe_unavailable_is_not_deleted(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fingerprint probe None must be UNPROVEN — not treated as PID reuse for GC."""
    from datetime import datetime, timedelta, timezone

    import omg_cli.jobs.ownership as ownership_mod
    import omg_cli.jobs.runtime as runtime_mod
    from omg_cli.jobs.runtime import gc_jobs
    from omg_cli.jobs.store import job_dir, job_lock, write_job_record

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=1,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    jid = terminal.job_id

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with job_lock(root, jid):
        rec = read_job_record(root, jid)
        rec.terminal_at = old
        rec.pid = 424242
        rec.pgid = 424242
        rec.pid_starttime = "fingerprint"
        write_job_record(root, rec)

    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ownership_mod, "pid_alive", lambda pid: True)
    monkeypatch.setattr(
        ownership_mod.os, "getpgid", lambda pid: 424242
    )
    monkeypatch.setattr(ownership_mod, "probe_pid_starttime", lambda pid: None)
    monkeypatch.setattr(runtime_mod, "_probe_pid_starttime", lambda pid: None)

    result = gc_jobs(root, retention_days=0)
    assert jid not in result.deleted
    assert job_dir(root, jid).is_dir()
    assert (job_dir(root, jid) / "job.json").is_file()
    assert any(
        s.get("job_id") == jid
        and str(s.get("reason", "")).startswith("identity_unproven")
        for s in result.skipped
    )


def test_gc_live_identity_getpgid_error_is_not_deleted(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.getpgid OSError must be UNPROVEN — not E_JOB_PGID_MISMATCH reclaim."""
    from datetime import datetime, timedelta, timezone

    import omg_cli.jobs.ownership as ownership_mod
    import omg_cli.jobs.runtime as runtime_mod
    from omg_cli.jobs.runtime import gc_jobs
    from omg_cli.jobs.store import job_dir, job_lock, write_job_record

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=1,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    jid = terminal.job_id

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with job_lock(root, jid):
        rec = read_job_record(root, jid)
        rec.terminal_at = old
        rec.pid = 424242
        rec.pgid = 424242
        rec.pid_starttime = "fingerprint"
        write_job_record(root, rec)

    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ownership_mod, "pid_alive", lambda pid: True)

    def _boom(_pid: int) -> int:
        raise OSError("getpgid probe failed")

    monkeypatch.setattr(ownership_mod.os, "getpgid", _boom)

    result = gc_jobs(root, retention_days=0)
    assert jid not in result.deleted
    assert job_dir(root, jid).is_dir()
    assert (job_dir(root, jid) / "job.json").is_file()
    assert any(
        s.get("job_id") == jid
        and str(s.get("reason", "")).startswith("identity_unproven")
        for s in result.skipped
    )


def test_gc_terminal_runner_cannot_recreate_job_dir_after_quarantine(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live+unproven identity blocks quarantine so a late runner append cannot
    recreate a ghost ``.omg/jobs/<id>/`` (events only, no job.json).
    """
    from datetime import datetime, timedelta, timezone

    import omg_cli.jobs.ownership as ownership_mod
    import omg_cli.jobs.runtime as runtime_mod
    from omg_cli.jobs.runtime import gc_jobs
    from omg_cli.jobs.store import append_jsonl, job_dir, job_lock, write_job_record

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=1,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    jid = terminal.job_id
    jdir = job_dir(root, jid)

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with job_lock(root, jid):
        rec = read_job_record(root, jid)
        rec.terminal_at = old
        rec.pid = 424242
        rec.pgid = 424242
        rec.pid_starttime = "fingerprint"
        write_job_record(root, rec)

    # Simulate the terminal→exit window: pid still "alive", fingerprint probe down.
    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ownership_mod, "pid_alive", lambda pid: True)
    monkeypatch.setattr(ownership_mod.os, "getpgid", lambda pid: 424242)
    monkeypatch.setattr(ownership_mod, "probe_pid_starttime", lambda pid: None)

    result = gc_jobs(root, retention_days=0)
    assert jid not in result.deleted
    assert jdir.is_dir()
    assert (jdir / "job.json").is_file()

    # Late runner.terminal-style append must land on the real tree, not a ghost.
    events = jdir / "events.jsonl"
    append_jsonl(events, {"event": "runner.terminal", "job_id": jid, "state": "failed"})
    assert jdir.is_dir()
    assert (jdir / "job.json").is_file()
    assert events.is_file()
    assert "runner.terminal" in events.read_text(encoding="utf-8")


def test_gc_binding_created_during_collection_is_protected(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACP binding appearing before quarantine must protect the job from GC."""
    import json
    from datetime import datetime, timedelta, timezone

    import omg_cli.jobs.runtime as runtime_mod
    from omg_cli.jobs.runtime import gc_jobs
    from omg_cli.jobs.store import (
        ensure_jobs_root,
        job_dir,
        job_lock,
        write_job_record,
    )

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=1,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    jid = terminal.job_id

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with job_lock(root, jid):
        rec = read_job_record(root, jid)
        rec.terminal_at = old
        write_job_record(root, rec)

    bind_dir = ensure_jobs_root(root) / "acp_bindings"
    bind_dir.mkdir(parents=True, exist_ok=True)
    bind_path = bind_dir / "run-gc-race.json"

    def _inject_binding(project_root, record):  # noqa: ANN001
        # Appear after the first under-lock binding check, before final recheck.
        del project_root  # unused; signature matches production helper
        bind_path.write_text(
            json.dumps(
                {"run_id": "run-gc-race", "job_id": record.job_id, "state": "ready"}
            ),
            encoding="utf-8",
        )
        # Force path through to the final binding recheck (ignore live pid noise).
        return None

    monkeypatch.setattr(runtime_mod, "_gc_identities_block_reason", _inject_binding)

    result = gc_jobs(root, retention_days=0)
    assert jid not in result.deleted
    assert job_dir(root, jid).is_dir()
    assert any(
        s.get("job_id") == jid and s.get("reason") == "acp_binding"
        for s in result.skipped
    )

def test_gc_malformed_acp_binding_protects_job(root: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from omg_cli.jobs.runtime import gc_jobs
    from omg_cli.jobs.store import (
        ensure_jobs_root,
        job_dir,
        job_lock,
        write_job_record,
    )

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    jid = terminal.job_id
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with job_lock(root, jid):
        rec = read_job_record(root, jid)
        rec.terminal_at = old
        write_job_record(root, rec)

    bind_dir = ensure_jobs_root(root) / "acp_bindings"
    bind_dir.mkdir(parents=True, exist_ok=True)
    (bind_dir / "corrupt.json").write_text("{not-json", encoding="utf-8")

    result = gc_jobs(root, retention_days=0)
    assert jid not in result.deleted
    assert job_dir(root, jid).is_dir()
    assert any(s.get("job_id") == jid and s.get("reason") == "acp_binding" for s in result.skipped)


def test_gc_rejects_nonfinite_retention_days(root: Path) -> None:
    from omg_cli.jobs.runtime import gc_jobs

    with pytest.raises(JobStoreError) as ei:
        gc_jobs(root, retention_days=float("nan"))
    assert ei.value.code == "E_JOB_GC"
    with pytest.raises(JobStoreError) as ei2:
        gc_jobs(root, retention_days=float("inf"))
    assert ei2.value.code == "E_JOB_GC"


def test_retry_rejects_missing_retry_class(root: Path) -> None:
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import job_lock, write_job_record

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    with job_lock(root, terminal.job_id):
        rec = read_job_record(root, terminal.job_id)
        rec.retry_class = None
        rec.retry_reason = None
        write_job_record(root, rec)

    with pytest.raises(JobStoreError) as ei:
        retry_job(root, terminal.job_id, attempt=2)
    assert ei.value.code == "E_JOB_RETRY_CLASS"


def test_spawn_failure_stamps_explicit_retry_class(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    # Create job in starting without launch, then force Popen failure on launch.
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        attempt_budget=3,
        launch=False,
    )
    jid = started.record.job_id

    def _boom(*a, **k):
        raise OSError("spawn denied")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    with pytest.raises(JobStoreError) as ei:
        runtime_mod.launch_job_runner(root, jid)
    assert ei.value.code == "E_JOB_LAUNCH"
    rec = read_job_record(root, jid)
    assert rec.state == JobState.FAILED
    assert rec.retry_class == "unknown"
    assert rec.retry_reason == "spawn_error"
    assert rec.terminal_at
    assert (rec.exit or {}).get("class") == "spawn_error"


def test_launch_commit_failure_stamps_terminal_retry_metadata(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs import runtime as runtime_mod

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=prompt,
        attempt_budget=3,
        launch=False,
    )
    jid = started.record.job_id

    # Stamp via the shared helper used by launch-commit recovery.
    runtime_mod._best_effort_stamp_failed(
        root, jid, message="launch commit failed after spawn: synthetic"
    )
    rec = read_job_record(root, jid)
    assert rec.state == JobState.FAILED
    assert rec.retry_class == "unknown"
    assert rec.retry_reason == "spawn_error"
    assert rec.terminal_at is not None
    assert (rec.exit or {}).get("class") == "spawn_error"

    # Missing class cannot retry even with budget remaining.
    from omg_cli.jobs.runtime import retry_job

    # Class is present (unknown) — admission allows unknown; prove spawn_error path.
    # Clear class to ensure fail-closed on missing.
    from omg_cli.jobs.store import job_lock, write_job_record

    with job_lock(root, jid):
        rec2 = read_job_record(root, jid)
        rec2.retry_class = None
        write_job_record(root, rec2)
    with pytest.raises(JobStoreError) as ei:
        retry_job(root, jid, attempt=2)
    assert ei.value.code == "E_JOB_RETRY_CLASS"


def test_retry_allows_verified_reused_prior_identity(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs.ownership import IdentityProbeOutcome
    from omg_cli.jobs.runtime import retry_job

    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.05,
        attempt_budget=3,
        fail=True,
    )
    failed, _ = wait_job(root, result.record.job_id, timeout_s=10.0)
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.REUSED,
    )
    retried = retry_job(root, failed.job_id, attempt=2, launch=False)
    assert retried.record.attempt == 2
    assert retried.record.state == JobState.STARTING


def test_retry_blocks_live_prior_identity(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs.ownership import IdentityProbeOutcome
    from omg_cli.jobs.runtime import retry_job

    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.05,
        attempt_budget=3,
        fail=True,
    )
    failed, _ = wait_job(root, result.record.job_id, timeout_s=10.0)
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.LIVE,
    )
    monkeypatch.setattr(
        "omg_cli.jobs.runtime._wait_until_gone",
        lambda *a, **k: False,
    )
    with pytest.raises(JobStoreError) as ei:
        retry_job(root, failed.job_id, attempt=2, launch=False)
    assert ei.value.code == "E_JOB_RETRY_LIVE"


def test_retry_blocks_unproven_prior_identity(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs.ownership import IdentityProbeOutcome
    from omg_cli.jobs.runtime import retry_job

    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.05,
        attempt_budget=3,
        fail=True,
    )
    failed, _ = wait_job(root, result.record.job_id, timeout_s=10.0)
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_for_recovery",
        lambda identity: IdentityProbeOutcome.UNPROVEN,
    )
    with pytest.raises(JobStoreError) as ei:
        retry_job(root, failed.job_id, attempt=2, launch=False)
    assert ei.value.code == "E_JOB_CANCEL_UNPROVEN"


def test_gc_still_blocks_active_or_unproven_owner_lease(root: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from omg_cli.jobs.runtime import gc_jobs

    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.05,
    )
    terminal, _ = wait_job(root, result.record.job_id, timeout_s=10.0)
    # Force retention eligible but re-activate lease (malformed for from_dict —
    # write raw JSON instead).
    path = job_json_path(root, terminal.job_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["terminal_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    lease = data.get("owner_lease") or {}
    lease["released_at"] = None
    data["owner_lease"] = lease
    # Use raw write to bypass from_dict terminal+active check on write path
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # GC candidate scan uses read_job_record which may reject — either skip or block
    try:
        out = gc_jobs(root, retention_days=1)
    except JobStoreError:
        return
    assert terminal.job_id not in out.deleted


def test_gc_accepts_released_lost_job_only_after_identity_proof(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    from omg_cli.jobs.ownership import IdentityProbeOutcome
    from omg_cli.jobs.recovery import recover_job
    from omg_cli.jobs.runtime import gc_jobs
    from omg_cli.jobs.store import job_lock, read_job_record, write_job_record

    result = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=30.0,
    )
    import os
    import signal

    os.kill(int(result.record.pid), signal.SIGKILL)
    try:
        os.waitpid(int(result.record.pid), 0)
    except ChildProcessError:
        pass
    # Expire lease
    with job_lock(root, result.record.job_id):
        rec = read_job_record(root, result.record.job_id)
        past = datetime.now(timezone.utc) - timedelta(seconds=120)
        from omg_cli.jobs.lease import format_lease_ts

        lease = dict(rec.owner_lease)
        lease["acquired_at"] = format_lease_ts(past)
        lease["heartbeat_at"] = format_lease_ts(past)
        lease["expires_at"] = format_lease_ts(past + timedelta(seconds=30))
        rec.owner_lease = lease
        write_job_record(root, rec)
    assert recover_job(root, result.record.job_id).action == "marked_lost"
    with job_lock(root, result.record.job_id):
        rec = read_job_record(root, result.record.job_id)
        rec.terminal_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        write_job_record(root, rec)
    monkeypatch.setattr(
        "omg_cli.jobs.ownership.probe_identity_liveness",
        lambda identity: IdentityProbeOutcome.GONE,
    )
    out = gc_jobs(root, retention_days=1)
    assert result.record.job_id in out.deleted


def test_explicit_retry_still_defaults_to_explicit_intent(root: Path) -> None:
    """#68 PR5: public retry_job defaults remain RetryIntent.EXPLICIT."""
    from omg_cli.jobs.runtime import retry_job
    from omg_cli.jobs.store import attempt_dir

    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    retried = retry_job(root, terminal.job_id, attempt=2, launch=False)
    assert retried.record.attempt == 2
    snap = json.loads(
        (attempt_dir(root, terminal.job_id, 1) / "attempt.json").read_text(
            encoding="utf-8"
        )
    )
    assert snap["retry_dispatch"]["intent"] == "explicit"


def test_preflight_retry_job_is_side_effect_free(root: Path) -> None:
    from omg_cli.jobs.runtime import preflight_retry_job
    from omg_cli.jobs.store import attempt_dir, job_json_path

    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        fail=True,
        sleep_s=0.02,
        attempt_budget=3,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15)
    before = job_json_path(root, terminal.job_id).read_bytes()
    preflight_retry_job(root, terminal.job_id, attempt=2)
    assert job_json_path(root, terminal.job_id).read_bytes() == before
    assert not attempt_dir(root, terminal.job_id, 1).exists()


def test_fake_job_terminal_emits_wrapper_event(root: Path) -> None:
    from omg_cli.hooks_registry import WRAPPER_SOURCE
    from omg_cli.runtime_events import read_runtime_events, source_journal_path

    started = start_job(
        root,
        provider="fake",
        role="researcher",
        prompt_file=_prompt(root),
        sleep_s=0.05,
    )
    rec, timed_out = wait_job(root, started.record.job_id, timeout_s=10.0)
    assert timed_out is False
    assert rec.state == JobState.SUCCEEDED
    path = source_journal_path(root, WRAPPER_SOURCE)
    rows = read_runtime_events(path)
    terminals = [
        row for row in rows if row["payload"].get("canonical_event") == "job.terminal"
    ]
    assert terminals
    row = terminals[-1]
    assert row["source"] == WRAPPER_SOURCE
    assert row["event_type"] == "turn_completed"
    assert row["payload"]["canonical_event"] == "job.terminal"
    assert row["payload"]["to"] == "succeeded"
    assert row["payload"]["job_id"] == rec.job_id
    assert row["payload"]["timeout_kind"] == "post_hoc"
    assert row["payload"].get("verified") is False
    text = path.read_text(encoding="utf-8")
    assert "verified" not in text or '"verified":false' in text.replace(" ", "")
    assert rec.to_dict().get("verified") is not True


def test_cli_fake_job_terminal_journals_wrapper_event(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omg_cli.hooks_registry import WRAPPER_SOURCE
    from omg_cli.main import main
    from omg_cli.runtime_events import read_runtime_events, source_journal_path

    monkeypatch.chdir(root)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(root))
    prompt = _prompt(root)
    rc = main(
        [
            "--json",
            "job",
            "start",
            "--provider",
            "fake",
            "--prompt-file",
            str(prompt),
            "--sleep",
            "0.05",
        ]
    )
    assert rc == 0
    start = json.loads(capsys.readouterr().out)
    assert start["ok"] is True
    assert start.get("verified") is not True
    job_id = start["job_id"]
    rc = main(["--json", "job", "wait", job_id, "--timeout", "15"])
    assert rc == 0
    waited = json.loads(capsys.readouterr().out)
    assert waited["ok"] is True
    assert waited["job"]["state"] == "succeeded"
    assert waited.get("verified") is not True
    path = source_journal_path(root, WRAPPER_SOURCE)
    rows = read_runtime_events(path)
    assert any(row["payload"].get("canonical_event") == "job.terminal" for row in rows)
    assert all(
        row["event_type"] == "turn_completed"
        for row in rows
        if row["payload"].get("canonical_event") == "job.terminal"
    )
    assert all(row["source"] == WRAPPER_SOURCE for row in rows)
    assert all(row["payload"].get("verified") is False for row in rows)
    text = path.read_text(encoding="utf-8")
    assert "job.terminal" in text
    assert "SECRET" not in text
