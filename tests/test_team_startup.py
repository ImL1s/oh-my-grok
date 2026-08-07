"""#99 provider-ready Team startup — false-green kill + schema v2."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omg_cli.team.mailbox import send_message
from omg_cli.team.provider_ready import (
    FakeBlockedStrategy,
    FakeExitStrategy,
    FakeReadyStrategy,
    FakeTimeoutStrategy,
    UnknownStrategy,
    get_readiness_strategy,
)
from omg_cli.team.runtime import (
    collect_process_ready_workers,
    wait_for_startup_acks,
    write_worker_ready_receipt,
)
from omg_cli.team.startup import (
    EvidenceCode,
    StartupError,
    StartupPhase,
    append_startup_diagnostics,
    classify_startup_payload,
    meets_gate,
    provider_process_alive,
    read_startup_record,
    redact_diagnostics_line,
    write_startup_phase,
)
from omg_cli.team.supervisor import (
    load_provider_descriptor,
    run_supervisor,
    write_provider_descriptor,
)


def _hold_script(path: Path, *, seconds: float = 30.0) -> Path:
    path.write_text(
        "import time,sys\n"
        "print('TEAM_PROVIDER_READY_OK', flush=True)\n"
        f"time.sleep({seconds})\n",
        encoding="utf-8",
    )
    return path


def _exit_script(path: Path, *, code: int = 0) -> Path:
    path.write_text(
        f"import sys\nsys.exit({code})\n",
        encoding="utf-8",
    )
    return path


def _auth_script(path: Path) -> Path:
    path.write_text(
        "import time\n"
        "print('authentication required: please log in', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    return path


def _bind_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    worker_id: str = "w1",
    run_id: str = "run-sup",
) -> None:
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", worker_id)
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", "team")
    monkeypatch.setenv("OMG_TEAM_LEADER_ROOT", str(tmp_path))
    monkeypatch.setenv("OMG_TEAM_SUPERVISOR_READY_S", "5")


def test_legacy_v1_receipt_cannot_make_running(tmp_path: Path) -> None:
    """Mutation target: v1 helper receipt must not satisfy the gate."""
    write_worker_ready_receipt(
        tmp_path, run_id="run-legacy", team_id="team", worker_id="w1"
    )
    write_worker_ready_receipt(
        tmp_path, run_id="run-legacy", team_id="team", worker_id="w2"
    )
    out = wait_for_startup_acks(
        tmp_path,
        run_id="run-legacy",
        team_id="team",
        expected_workers=["w1", "w2"],
        timeout_ms=80,
        poll_s=0.01,
    )
    assert out["startup_status"] == "failed_start"
    assert out["startup_process_ready"] == 0
    assert collect_process_ready_workers(
        tmp_path,
        run_id="run-legacy",
        team_id="team",
        expected_workers=["w1", "w2"],
    ) == set()
    raw = read_startup_record(
        tmp_path, run_id="run-legacy", team_id="team", worker_id="w1"
    )
    classified = classify_startup_payload(raw)
    assert classified["legacy"] is True
    assert classified["ok_for_gate"] is False
    assert classified["evidence_code"] == EvidenceCode.WRAPPER_READY_LEGACY.value


def test_mailbox_ack_alone_cannot_elevate(tmp_path: Path) -> None:
    send_message(
        tmp_path,
        run_id="run-ack-only",
        team_id="team",
        sender_id="w1",
        recipient_id="leader-fixed",
        body="ACK",
        generation=0,
        kind="ack",
        dedupe_key="ack-w1",
    )
    out = wait_for_startup_acks(
        tmp_path,
        run_id="run-ack-only",
        team_id="team",
        expected_workers=["w1"],
        timeout_ms=80,
        poll_s=0.01,
    )
    assert out["startup_status"] == "failed_start"
    assert out["startup_acks"] == 1
    assert out["startup_process_ready"] == 0


def test_monotonic_phase_rejects_downgrade(tmp_path: Path) -> None:
    write_startup_phase(
        tmp_path,
        run_id="run-mono",
        team_id="team",
        worker_id="w1",
        phase=StartupPhase.PROVIDER_READY,
        provider="fake-ready",
        supervisor_pid=1,
        provider_pid=2,
        provider_pgid=2,
        provider_pid_start="ps:test",
        evidence_code=EvidenceCode.FAKE_READY,
    )
    with pytest.raises(StartupError, match="non-monotonic"):
        write_startup_phase(
            tmp_path,
            run_id="run-mono",
            team_id="team",
            worker_id="w1",
            phase=StartupPhase.PROVIDER_SPAWNED,
            provider="fake-ready",
        )


def test_meets_gate_requires_task_dispatched_by_default() -> None:
    assert not meets_gate(StartupPhase.PROVIDER_READY.value)
    assert meets_gate(StartupPhase.TASK_DISPATCHED.value)
    assert meets_gate(
        StartupPhase.PROVIDER_READY.value,
        gate=StartupPhase.PROVIDER_READY.value,
    )


def test_redaction_strips_secrets() -> None:
    line = "Authorization: Bearer super-secret-token api_key=abc123"
    out = redact_diagnostics_line(line)
    assert "super-secret-token" not in out
    assert "abc123" not in out
    assert "<redacted>" in out


def test_unknown_strategy_never_ready() -> None:
    obs = UnknownStrategy().observe(
        provider_pid=1,
        alive=True,
        capture_lines=[],
        elapsed_s=10.0,
    )
    assert obs.status == "unknown"
    assert obs.evidence_code == EvidenceCode.UNKNOWN_PROVIDER.value


def test_fake_blocked_and_exit_strategies() -> None:
    blocked = FakeBlockedStrategy().observe(
        provider_pid=1, alive=True, capture_lines=[], elapsed_s=0.1
    )
    assert blocked.status == "blocked"
    exited = FakeExitStrategy().observe(
        provider_pid=1, alive=False, capture_lines=[], elapsed_s=0.1
    )
    assert exited.status == "failed"


def test_supervisor_ready_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind_env(monkeypatch, tmp_path)
    script = _hold_script(tmp_path / "hold.py", seconds=8.0)
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="fake-ready",
        argv=[sys.executable, str(script)],
    )
    # Run supervisor in a child so we can still assert receipts while it holds.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            "supervisor",
            "--descriptor",
            str(desc),
            "--ready-timeout",
            "3",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id="run-sup", team_id="team", worker_id="w1"
            )
            if (
                record
                and record.get("phase") == StartupPhase.TASK_DISPATCHED.value
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record["schema_version"] == 2
        assert record["phase"] == StartupPhase.TASK_DISPATCHED.value
        assert record["provider_pid"] != record["supervisor_pid"]
        assert provider_process_alive(
            provider_pid=record["provider_pid"],
            provider_pid_start=record["provider_pid_start"],
        )
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-sup",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=200,
            poll_s=0.02,
        )
        assert out["startup_status"] == "running"
        assert out["startup_workers"][0]["gate_ok"] is True
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_supervisor_immediate_exit_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind_env(monkeypatch, tmp_path, run_id="run-exit")
    script = _exit_script(tmp_path / "exit.py", code=0)
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="fixture",
        argv=[sys.executable, str(script)],
    )
    rc = run_supervisor(descriptor_path=desc, ready_timeout_s=2.0, poll_s=0.05)
    assert rc == 0  # child exit 0, but readiness failed before ready
    record = read_startup_record(
        tmp_path, run_id="run-exit", team_id="team", worker_id="w1"
    )
    assert record is not None
    # Either failed before ready, or if fixture raced to ready then child
    # already exited — wait must not claim running with dead provider.
    out = wait_for_startup_acks(
        tmp_path,
        run_id="run-exit",
        team_id="team",
        expected_workers=["w1"],
        timeout_ms=100,
        poll_s=0.01,
    )
    assert out["startup_status"] != "running"
    assert out["startup_process_ready"] == 0


def test_supervisor_auth_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind_env(monkeypatch, tmp_path, run_id="run-auth")
    # Use grok strategy so auth regex applies (not fake-ready).
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "grok")
    script = _auth_script(tmp_path / "auth.py")
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="grok",
        argv=[sys.executable, str(script)],
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            "supervisor",
            "--descriptor",
            str(desc),
            "--ready-timeout",
            "3",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id="run-auth", team_id="team", worker_id="w1"
            )
            if record and record.get("phase") == StartupPhase.BLOCKED.value:
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] == StartupPhase.BLOCKED.value
        assert record["blocked_reason"]
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-auth",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=100,
            poll_s=0.01,
        )
        assert out["startup_status"] == "blocked_start"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_supervisor_signal_forwards_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind_env(monkeypatch, tmp_path, run_id="run-sig")
    script = _hold_script(tmp_path / "hold.py", seconds=60.0)
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="fake-ready",
        argv=[sys.executable, str(script)],
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            "supervisor",
            "--descriptor",
            str(desc),
            "--ready-timeout",
            "5",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5.0
    record = None
    while time.monotonic() < deadline:
        record = read_startup_record(
            tmp_path, run_id="run-sig", team_id="team", worker_id="w1"
        )
        if record and record.get("provider_pid"):
            break
        time.sleep(0.05)
    assert record is not None
    provider_pid = int(record["provider_pid"])
    os.kill(proc.pid, signal.SIGTERM)
    proc.wait(timeout=5)
    # Provider should be gone (no orphan group).
    time.sleep(0.2)
    alive = True
    try:
        os.kill(provider_pid, 0)
    except OSError:
        alive = False
    assert alive is False


def test_diagnostics_separate_and_bounded(tmp_path: Path) -> None:
    path = append_startup_diagnostics(
        tmp_path,
        run_id="run-diag",
        team_id="team",
        worker_id="w1",
        lines=["password=hunter2", "ok line", "x" * 2000],
    )
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert "password=<redacted>" in text or "<redacted>" in text
    # Receipt path must not contain diagnostics content.
    write_startup_phase(
        tmp_path,
        run_id="run-diag",
        team_id="team",
        worker_id="w1",
        phase=StartupPhase.PANE_CREATED,
        provider="fixture",
        evidence_code=EvidenceCode.PANE_BOUND,
    )
    receipt = read_startup_record(
        tmp_path, run_id="run-diag", team_id="team", worker_id="w1"
    )
    assert receipt is not None
    assert "hunter2" not in json.dumps(receipt)


def test_descriptor_rejects_empty_argv(tmp_path: Path) -> None:
    from omg_cli.team.supervisor import SupervisorError

    with pytest.raises(SupervisorError):
        write_provider_descriptor(
            tmp_path / "bad.json", provider="grok", argv=[]
        )


def test_wrap_pane_uses_supervisor_not_worker_ready(tmp_path: Path) -> None:
    from omg_cli.team.plane import materialize_supervisor_pane_command

    cmd = materialize_supervisor_pane_command(
        descriptor_path=tmp_path / "w1.provider.json",
        provider="fixture",
        argv=[sys.executable, "-c", "print(1)"],
    )
    assert "supervisor" in cmd
    assert "worker-ready" not in cmd
    load_provider_descriptor(tmp_path / "w1.provider.json")


def test_get_readiness_strategy_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "fake-timeout")
    strat = get_readiness_strategy("grok")
    assert isinstance(strat, FakeTimeoutStrategy)
    ready = FakeReadyStrategy().observe(
        provider_pid=1, alive=True, capture_lines=[], elapsed_s=0
    )
    assert ready.status == "ready"
