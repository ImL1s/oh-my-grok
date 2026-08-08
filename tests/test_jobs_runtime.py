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
        sleep_s=2.0,
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
    """If runner stamps succeeded before cancel's transition, return that record."""
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
            # Runner wins: stamp succeeded, then cancel's transition would fail.
            cur = read_job_record(project_root, job_id)
            if cur.state == JobState.RUNNING:
                real_transition(
                    project_root,
                    job_id,
                    JobState.SUCCEEDED,
                    updates={
                        "exit": {"class": "success", "returncode": 0, "ok": True},
                    },
                )
            raise TransitionError(
                "illegal job transition succeeded -> cancelled",
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
    assert out.state == JobState.SUCCEEDED
    assert out.exit and out.exit.get("class") == "success"


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
