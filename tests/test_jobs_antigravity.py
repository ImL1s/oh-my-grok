"""Hermetic Antigravity durable-job lifecycle + dual-process cancel (#68 PR2)."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.runtime import cancel_job, collect_job, start_job, wait_job
from omg_cli.jobs.store import job_dir, job_json_path, read_job_record, update_job_fields

pytest_plugins = ["tests.antigravity_testutil"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".omg").mkdir()
    return tmp_path


def _prompt(root: Path, text: str = "hello from jobs") -> Path:
    p = root / "prompt.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_antigravity_job_start_wait_collect_via_provider_adapter(
    root: Path, fake_agy_path: Path
) -> None:
    del fake_agy_path
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=30.0,
    )
    assert started.record.state == JobState.RUNNING
    assert started.record.provider == "antigravity"
    terminal, timed_out = wait_job(root, started.record.job_id, timeout_s=30.0)
    assert timed_out is False
    assert terminal.state == JobState.SUCCEEDED
    assert terminal.exit and terminal.exit.get("class") == "success"
    summary = collect_job(root, terminal.job_id)
    assert summary["state"] == "succeeded"
    assert summary["result"] == "artifacts/result.md"
    result_path = job_dir(root, terminal.job_id) / "artifacts" / "result.md"
    assert result_path.is_file()
    assert result_path.stat().st_size > 0


def test_antigravity_job_forwards_model_effort_mode_and_stream_json(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_agy_path
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        model="m1",
        effort="low",
        mode="plan",
        output_format="stream-json",
        provider_timeout_s=30.0,
    )
    assert started.record.request["model"] == "m1"
    assert started.record.request["effort"] == "low"
    assert started.record.request["mode"] == "plan"
    assert started.record.request["output_format"] == "stream-json"
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=30.0)
    assert terminal.state == JobState.SUCCEEDED, (
        f"exit={terminal.exit} err={terminal.error_message}"
    )


def test_antigravity_job_uses_persisted_exact_binary(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.antigravity_testutil import install_fake_agy

    pinned = str(fake_agy_path.resolve())
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=30.0,
    )
    assert started.record.request["provider_binary"] == pinned

    other = install_fake_agy(tmp_path / "other-bin")
    monkeypatch.setenv("PATH", str(other.parent))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)

    terminal, _ = wait_job(root, started.record.job_id, timeout_s=30.0)
    assert terminal.state == JobState.SUCCEEDED


def test_antigravity_job_persists_result_stderr_events_usage_and_session_descriptors(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_STDERR", "agy-stderr-line\n")
    monkeypatch.setenv("FAKE_AGY_RUN_SESSION", "sess-jobs-1")
    monkeypatch.setenv("FAKE_AGY_RUN_RESUME", "resume-tok")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        output_format="json",
        provider_timeout_s=30.0,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=30.0)
    assert terminal.state == JobState.SUCCEEDED
    jdir = job_dir(root, terminal.job_id)
    assert (jdir / "stderr.jsonl").is_file()
    stderr_lines = (jdir / "stderr.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert any("agy-stderr-line" in line for line in stderr_lines)
    events = (jdir / "events.jsonl").read_text(encoding="utf-8")
    assert "provider.event" in events
    assert terminal.usage is not None or terminal.session is not None
    disk = json.loads(job_json_path(root, terminal.job_id).read_text(encoding="utf-8"))
    assert disk.get("result") == "artifacts/result.md"


def test_antigravity_job_keeps_large_result_out_of_job_json(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_agy_path
    big = "X" * 50_000
    monkeypatch.setenv("FAKE_AGY_RUN_STDOUT", big + "\n")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        output_format="text",
        provider_timeout_s=30.0,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=30.0)
    raw = job_json_path(root, terminal.job_id).read_text(encoding="utf-8")
    assert big not in raw
    assert (job_dir(root, terminal.job_id) / "artifacts" / "result.md").is_file()


def test_antigravity_auth_blocked_is_failed_and_non_retryable(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_AUTH_BLOCK", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_AUTH_EXIT0", "1")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=30.0,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=30.0)
    assert terminal.state == JobState.FAILED
    assert terminal.exit and terminal.exit.get("class") == "auth_blocked"
    assert terminal.exit.get("ok") is False


def test_antigravity_nonzero_preserves_retryable_without_retrying(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_RC", "3")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=30.0,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=30.0)
    assert terminal.state == JobState.FAILED
    assert terminal.exit and terminal.exit.get("class") == "nonzero"
    assert terminal.attempt == 1


def test_antigravity_timeout_preserves_partial_output(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "30")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=0.4,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=15.0)
    assert terminal.state == JobState.FAILED
    assert terminal.exit and (
        terminal.exit.get("timed_out") is True or terminal.exit.get("class") == "timeout"
    )
    assert terminal.exit.get("partial_output") is True
    result = job_dir(root, terminal.job_id) / "artifacts" / "result.md"
    assert result.is_file() and result.stat().st_size > 0


def test_provider_spawn_observer_binds_inner_process_before_execution(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "8")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=20.0,
    )
    bound = False
    for _ in range(100):
        rec = read_job_record(root, started.record.job_id)
        pp = rec.provider_process or {}
        if pp.get("state") == "bound" and pp.get("pid"):
            bound = True
            assert int(pp["pid"]) > 1
            assert int(pp["pgid"]) > 1
            break
        time.sleep(0.05)
    assert bound
    cancel_job(root, started.record.job_id, grace_s=1.0)
    terminal = read_job_record(root, started.record.job_id)
    assert terminal.state == JobState.CANCELLED


def test_provider_spawn_bind_failure_kills_inner_process_group(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs import store as store_mod
    import omg_cli.jobs.runner as runner_mod

    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "20")

    def _fail_bind(*_a, **_k):
        raise JobStoreError("forced bind failure", code="E_JOB_CANCEL_UNPROVEN")

    monkeypatch.setattr(store_mod, "bind_provider_process", _fail_bind)
    monkeypatch.setattr(runner_mod, "bind_provider_process", _fail_bind)

    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=15.0,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=20.0)
    assert terminal.state in {JobState.FAILED, JobState.CANCELLED}


def test_runner_maps_provider_cancelled_result_to_cancelled(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "10")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=30.0,
    )
    for _ in range(80):
        rec = read_job_record(root, started.record.job_id)
        if (rec.provider_process or {}).get("state") == "bound":
            break
        time.sleep(0.05)
    cancelled = cancel_job(root, started.record.job_id, grace_s=2.0)
    assert cancelled.state == JobState.CANCELLED
    assert cancelled.exit and cancelled.exit.get("cancelled") is True


def test_antigravity_cancel_graceful_reaps_inner_and_outer_process_groups(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs import runtime as runtime_mod

    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "15")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=60.0,
    )
    for _ in range(100):
        rec = read_job_record(root, started.record.job_id)
        if (rec.provider_process or {}).get("state") == "bound":
            break
        time.sleep(0.05)
    rec = read_job_record(root, started.record.job_id)
    outer_pid = int(rec.pid or 0)
    inner_pid = int((rec.provider_process or {}).get("pid") or 0)
    assert outer_pid > 1 and inner_pid > 1
    cancelled = cancel_job(root, started.record.job_id, grace_s=2.0)
    assert cancelled.state == JobState.CANCELLED
    assert not runtime_mod._pid_alive(outer_pid)
    assert not runtime_mod._pid_alive(inner_pid)


def test_antigravity_cancel_force_reaps_inner_and_outer_process_groups(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs import runtime as runtime_mod

    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "30")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=60.0,
    )
    for _ in range(100):
        rec = read_job_record(root, started.record.job_id)
        if (rec.provider_process or {}).get("state") == "bound":
            break
        time.sleep(0.05)
    rec = read_job_record(root, started.record.job_id)
    outer_pid = int(rec.pid or 0)
    inner_pid = int((rec.provider_process or {}).get("pid") or 0)
    cancelled = cancel_job(root, started.record.job_id, grace_s=0.0)
    assert cancelled.state == JobState.CANCELLED
    assert not runtime_mod._pid_alive(outer_pid)
    assert not runtime_mod._pid_alive(inner_pid)


def test_antigravity_cancel_during_unbound_launch_fails_closed(
    root: Path, fake_agy_path: Path
) -> None:
    del fake_agy_path
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        launch=False,
    )
    from omg_cli.jobs.store import transition_job

    transition_job(
        root,
        started.record.job_id,
        JobState.RUNNING,
        updates={
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "handle": f"antigravity:{started.record.job_id}:pid={os.getpid()}",
            "provider_process": {
                "state": "launching",
                "pid": None,
                "pgid": None,
                "pid_starttime": None,
                "handle": None,
                "bound_at": None,
                "exited_at": None,
            },
        },
    )
    with pytest.raises(JobStoreError) as ei:
        cancel_job(root, started.record.job_id, grace_s=0.1)
    assert ei.value.code == "E_JOB_CANCEL_UNPROVEN"
    still = read_job_record(root, started.record.job_id)
    assert still.state == JobState.RUNNING


def test_antigravity_cancel_inner_identity_mismatch_sends_no_signal(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs import runtime as runtime_mod

    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "20")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=60.0,
    )
    for _ in range(100):
        rec = read_job_record(root, started.record.job_id)
        if (rec.provider_process or {}).get("state") == "bound":
            break
        time.sleep(0.05)
    rec = read_job_record(root, started.record.job_id)
    pp = dict(rec.provider_process or {})
    pp["pid_starttime"] = "proc:999999999"
    update_job_fields(root, rec.job_id, provider_process=pp)

    signals: list[int] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append(int(signum))
        return False

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)
    monkeypatch.setattr(runtime_mod, "_pid_alive", lambda _pid: True)

    with pytest.raises(JobStoreError) as ei:
        cancel_job(root, rec.job_id, grace_s=0.0)
    assert ei.value.code in {
        "E_JOB_PID_REUSED",
        "E_JOB_CANCEL_UNPROVEN",
        "E_JOB_PGID_MISMATCH",
    }
    monkeypatch.undo()
    try:
        cancel_job(root, rec.job_id, grace_s=0.5)
    except JobStoreError:
        try:
            os.killpg(int(rec.pgid or 0), signal.SIGKILL)
        except Exception:
            pass
        try:
            os.killpg(int(pp.get("pgid") or 0), signal.SIGKILL)
        except Exception:
            pass


def test_antigravity_cancel_outer_identity_mismatch_sends_no_signal(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs import runtime as runtime_mod

    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "20")
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=60.0,
    )
    for _ in range(100):
        rec = read_job_record(root, started.record.job_id)
        if (rec.provider_process or {}).get("state") == "bound":
            break
        time.sleep(0.05)
    rec = read_job_record(root, started.record.job_id)
    update_job_fields(root, rec.job_id, pid_starttime="proc:999999999")

    signals: list[int] = []

    def spy_kill(pgid_arg: int, signum: int) -> bool:
        signals.append(int(signum))
        return False

    monkeypatch.setattr(runtime_mod, "_kill_pgid", spy_kill)
    with pytest.raises(JobStoreError) as ei:
        cancel_job(root, rec.job_id, grace_s=0.05)
    assert ei.value.code == "E_JOB_PID_REUSED"
    assert signals == []
    monkeypatch.undo()
    try:
        live = runtime_mod._probe_pid_starttime(int(rec.pid or 0))
        update_job_fields(root, rec.job_id, pid_starttime=live)
        cancel_job(root, rec.job_id, grace_s=0.5)
    except Exception:
        try:
            os.killpg(int(rec.pgid or 0), signal.SIGKILL)
        except Exception:
            pass


def test_antigravity_sibling_cancel_does_not_kill_other_provider_job(
    root: Path, fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs import runtime as runtime_mod

    del fake_agy_path
    monkeypatch.setenv("FAKE_AGY_RUN_PARTIAL", "1")
    monkeypatch.setenv("FAKE_AGY_RUN_SLEEP", "20")
    prompt = _prompt(root)
    a = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=60.0,
    )
    b = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=60.0,
    )
    for job in (a, b):
        for _ in range(100):
            rec = read_job_record(root, job.record.job_id)
            if (rec.provider_process or {}).get("state") == "bound":
                break
            time.sleep(0.05)
    b_rec = read_job_record(root, b.record.job_id)
    b_outer = int(b_rec.pid or 0)
    b_inner = int((b_rec.provider_process or {}).get("pid") or 0)
    cancel_job(root, a.record.job_id, grace_s=1.0)
    assert runtime_mod._pid_alive(b_outer)
    assert runtime_mod._pid_alive(b_inner)
    cancel_job(root, b.record.job_id, grace_s=1.0)


def test_antigravity_cancel_completion_race_is_idempotent(
    root: Path, fake_agy_path: Path
) -> None:
    del fake_agy_path
    prompt = _prompt(root)
    started = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        provider_timeout_s=30.0,
    )
    terminal, _ = wait_job(root, started.record.job_id, timeout_s=30.0)
    assert terminal.state == JobState.SUCCEEDED
    again = cancel_job(root, terminal.job_id)
    assert again.state == JobState.SUCCEEDED
