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
    publish_supervisor_authority,
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


def _compiled_sleeper(path: Path) -> Path:
    """Build a real binary at *path* so argv[0]/exe basename match identity."""
    import shutil
    import tempfile

    src = Path(
        tempfile.mkdtemp(prefix="omg99-sleeper-")
    ) / "sleeper.c"
    src.write_text(
        "#include <stdlib.h>\n"
        "#include <unistd.h>\n"
        "int main(int argc, char **argv) {\n"
        "  unsigned long n = 30;\n"
        "  if (argc > 1) n = strtoul(argv[1], 0, 10);\n"
        "  sleep((unsigned int)n);\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    assert cc, "C compiler required for provider identity fixture"
    subprocess.run(
        [cc, "-o", str(path), str(src)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    path.chmod(0o755)
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
    owner_token: str = "owner-token-test",
) -> None:
    (tmp_path / ".omg").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".omg" / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", worker_id)
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", "team")
    monkeypatch.setenv("OMG_TEAM_LEADER_ROOT", str(tmp_path))
    monkeypatch.setenv("OMG_TEAM_STATE_ROOT", str(tmp_path / ".omg" / "state"))
    monkeypatch.setenv("OMG_TEAM_OWNER_TOKEN", owner_token)
    monkeypatch.setenv("OMG_TEAM_SUPERVISOR_READY_S", "5")


def _prepublish(
    tmp_path: Path,
    *,
    desc: Path,
    worker_id: str = "w1",
    run_id: str = "run-sup",
    owner_token: str = "owner-token-test",
) -> Path:
    """CLI prepublish required for ``omg team supervisor`` when team.json absent."""
    return publish_supervisor_authority(
        leader_root=tmp_path,
        run_id=run_id,
        team_id="team",
        worker_id=worker_id,
        owner_token=owner_token,
        descriptor_path=desc,
    )


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
    history = [
        StartupPhase.PANE_CREATED.value,
        StartupPhase.PROVIDER_SPAWNED.value,
        StartupPhase.PROVIDER_READY.value,
        StartupPhase.TASK_DISPATCHED.value,
    ]
    assert not meets_gate(
        StartupPhase.PROVIDER_READY.value,
        phases=history[:3],
    )
    assert meets_gate(
        StartupPhase.TASK_DISPATCHED.value,
        phases=history,
    )
    assert meets_gate(
        StartupPhase.PROVIDER_READY.value,
        gate=StartupPhase.PROVIDER_READY.value,
        phases=history[:3],
    )
    # Skip-to-dispatched without spawn/ready history must fail closed.
    assert not meets_gate(
        StartupPhase.TASK_DISPATCHED.value,
        phases=[StartupPhase.TASK_DISPATCHED.value],
    )
    # gate=provider_ready still requires provider_ready in phases history.
    assert not meets_gate(
        StartupPhase.PROVIDER_READY.value,
        gate=StartupPhase.PROVIDER_READY.value,
        phases=[
            StartupPhase.PANE_CREATED.value,
            StartupPhase.PROVIDER_SPAWNED.value,
        ],
    )


def test_gate_provider_ready_requires_ready_in_phases(tmp_path: Path) -> None:
    """Forge spawn-only history with gate=provider_ready → not running."""
    from omg_cli.contracts.path_keys import atomic_write_bytes, ensure_managed_dir
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.startup import worker_startup_path
    import json as _json

    path = worker_startup_path(
        tmp_path, run_id="run-pr-gate", team_id="team", worker_id="w1"
    )
    ensure_managed_dir(path.parent)
    hold = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        from omg_cli.team.plane import _pid_start_identity

        start = _pid_start_identity(hold.pid)
        assert start
        payload = {
            "schema_version": 2,
            "writer": CLI_WRITER,
            "kind": "team_worker_startup",
            "run_id": "run-pr-gate",
            "team_id": "team",
            "worker_id": "w1",
            "phase": "provider_ready",
            "phases": ["pane_created", "provider_spawned"],
            "provider": "grok",
            "supervisor_pid": 1,
            "provider_pid": hold.pid,
            "provider_pgid": hold.pid,
            "provider_pid_start": start,
            "evidence_code": "process_stable",
            "blocked_reason": None,
            "failure_reason": None,
            "observed_at": "2026-01-01T00:00:00Z",
        }
        atomic_write_bytes(
            path,
            (_json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-pr-gate",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=50,
            poll_s=0.01,
            env={"OMG_TEAM_STARTUP_GATE_PHASE": "provider_ready"},
        )
        assert out["startup_status"] != "running"
        assert out["startup_process_ready"] == 0
        assert not meets_gate(
            "provider_ready",
            gate="provider_ready",
            phases=["pane_created", "provider_spawned"],
        )
    finally:
        hold.terminate()
        try:
            hold.wait(timeout=2)
        except subprocess.TimeoutExpired:
            hold.kill()
            hold.wait(timeout=1)


def test_post_stable_zero_env_still_catches_delayed_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMG_TEAM_POST_STABLE_OBSERVE_S=0 must not nullify post-stable observe."""
    _bind_env(monkeypatch, tmp_path, run_id="run-post0")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "grok")
    monkeypatch.setenv("OMG_TEAM_POST_STABLE_OBSERVE_S", "0")
    script = tmp_path / "delayed_auth0.py"
    script.write_text(
        "import time\n"
        "time.sleep(1.0)\n"
        "print('authentication required: please log in', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="grok",
        argv=[sys.executable, str(script)],
    )
    _prepublish(tmp_path, desc=desc, run_id="run-post0")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["OMG_TEAM_POST_STABLE_OBSERVE_S"] = "0"
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
            "8",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id="run-post0", team_id="team", worker_id="w1"
            )
            if record and record.get("phase") in (
                StartupPhase.BLOCKED.value,
                StartupPhase.TASK_DISPATCHED.value,
                StartupPhase.FAILED.value,
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] != StartupPhase.TASK_DISPATCHED.value, record
        assert record["phase"] == StartupPhase.BLOCKED.value, record
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-post0",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=100,
            poll_s=0.01,
        )
        assert out["startup_status"] != "running"
        assert out["startup_status"] == "blocked_start"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_skip_to_task_dispatched_cannot_gate(tmp_path: Path) -> None:
    """Forged task_dispatched without prior phases must not yield running."""
    from omg_cli.team.startup import provider_identity_distinct

    # Manually write a skip receipt (bypass write_startup_phase monotonicity
    # by writing JSON directly — adversarial forged artifact).
    from omg_cli.team.startup import worker_startup_path
    from omg_cli.contracts.path_keys import atomic_write_bytes, ensure_managed_dir
    from omg_cli.evidence import CLI_WRITER
    import json as _json

    path = worker_startup_path(
        tmp_path, run_id="run-skip", team_id="team", worker_id="w1"
    )
    ensure_managed_dir(path.parent)
    payload = {
        "schema_version": 2,
        "writer": CLI_WRITER,
        "kind": "team_worker_startup",
        "run_id": "run-skip",
        "team_id": "team",
        "worker_id": "w1",
        "phase": "task_dispatched",
        "phases": ["task_dispatched"],
        "provider": "grok",
        "supervisor_pid": 10,
        "provider_pid": 11,
        "provider_pgid": 11,
        "provider_pid_start": "ps:fake",
        "evidence_code": "prompt_contract_accepted",
        "blocked_reason": None,
        "failure_reason": None,
        "observed_at": "2026-01-01T00:00:00Z",
    }
    atomic_write_bytes(
        path,
        (_json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    assert provider_identity_distinct(
        provider_pid=11, supervisor_pid=10, provider_pid_start="ps:fake"
    )
    out = wait_for_startup_acks(
        tmp_path,
        run_id="run-skip",
        team_id="team",
        expected_workers=["w1"],
        timeout_ms=50,
        poll_s=0.01,
    )
    assert out["startup_status"] != "running"
    assert out["startup_process_ready"] == 0


def test_gate_rejects_provider_pid_equals_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate-level: provider_pid == supervisor_pid cannot count as ready."""
    from omg_cli.contracts.path_keys import atomic_write_bytes, ensure_managed_dir
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.startup import worker_startup_path
    import json as _json

    path = worker_startup_path(
        tmp_path, run_id="run-samepid", team_id="team", worker_id="w1"
    )
    ensure_managed_dir(path.parent)
    # Hold a real process so provider_process_alive would otherwise pass.
    hold = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        from omg_cli.team.plane import _pid_start_identity

        start = _pid_start_identity(hold.pid)
        assert start
        payload = {
            "schema_version": 2,
            "writer": CLI_WRITER,
            "kind": "team_worker_startup",
            "run_id": "run-samepid",
            "team_id": "team",
            "worker_id": "w1",
            "phase": "task_dispatched",
            "phases": [
                "pane_created",
                "provider_spawned",
                "provider_ready",
                "task_dispatched",
            ],
            "provider": "grok",
            "supervisor_pid": hold.pid,  # same as provider — invalid
            "provider_pid": hold.pid,
            "provider_pgid": hold.pid,
            "provider_pid_start": start,
            "evidence_code": "process_stable",
            "blocked_reason": None,
            "failure_reason": None,
            "observed_at": "2026-01-01T00:00:00Z",
        }
        atomic_write_bytes(
            path,
            (_json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-samepid",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=50,
            poll_s=0.01,
        )
        assert out["startup_status"] != "running"
        assert out["startup_process_ready"] == 0
        assert out["startup_workers"][0]["identity_ok"] is False
    finally:
        hold.terminate()
        try:
            hold.wait(timeout=2)
        except subprocess.TimeoutExpired:
            hold.kill()
            hold.wait(timeout=1)


def test_gate_requires_provider_process_alive(tmp_path: Path) -> None:
    """Dead provider_pid must not satisfy the gate even with full history."""
    from omg_cli.contracts.path_keys import atomic_write_bytes, ensure_managed_dir
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team.startup import worker_startup_path
    import json as _json

    path = worker_startup_path(
        tmp_path, run_id="run-dead", team_id="team", worker_id="w1"
    )
    ensure_managed_dir(path.parent)
    payload = {
        "schema_version": 2,
        "writer": CLI_WRITER,
        "kind": "team_worker_startup",
        "run_id": "run-dead",
        "team_id": "team",
        "worker_id": "w1",
        "phase": "task_dispatched",
        "phases": [
            "pane_created",
            "provider_spawned",
            "provider_ready",
            "task_dispatched",
        ],
        "provider": "grok",
        "supervisor_pid": 1,
        "provider_pid": 999999,  # almost certainly dead
        "provider_pgid": 999999,
        "provider_pid_start": "ps:dead",
        "evidence_code": "process_stable",
        "blocked_reason": None,
        "failure_reason": None,
        "observed_at": "2026-01-01T00:00:00Z",
    }
    atomic_write_bytes(
        path,
        (_json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    out = wait_for_startup_acks(
        tmp_path,
        run_id="run-dead",
        team_id="team",
        expected_workers=["w1"],
        timeout_ms=50,
        poll_s=0.01,
    )
    assert out["startup_status"] != "running"
    assert out["startup_process_ready"] == 0


def test_delayed_auth_after_process_stable_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent then auth must not false-green via early process_stable (#99)."""
    _bind_env(monkeypatch, tmp_path, run_id="run-delayed-auth")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "grok")
    # Short stability + long post-stable window so auth arrives during observe.
    monkeypatch.setenv("OMG_TEAM_POST_STABLE_OBSERVE_S", "3")
    script = tmp_path / "delayed_auth.py"
    script.write_text(
        "import time\n"
        "time.sleep(1.0)\n"
        "print('authentication required: please log in', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="grok",
        argv=[sys.executable, str(script)],
    )
    _prepublish(tmp_path, desc=desc, run_id="run-delayed-auth")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["OMG_TEAM_POST_STABLE_OBSERVE_S"] = "3"
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
            "8",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id="run-delayed-auth", team_id="team", worker_id="w1"
            )
            if record and record.get("phase") in (
                StartupPhase.BLOCKED.value,
                StartupPhase.TASK_DISPATCHED.value,
                StartupPhase.FAILED.value,
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] == StartupPhase.BLOCKED.value, record
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-delayed-auth",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=100,
            poll_s=0.01,
        )
        assert out["startup_status"] == "blocked_start"
        assert out["startup_status"] != "running"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


@pytest.mark.parametrize(
    "idle_line,run_id",
    [
        (">", "run-tui-gt"),
        ("grok>", "run-tui-grokgt"),
    ],
)
def test_delayed_auth_after_tui_idle_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    idle_line: str,
    run_id: str,
) -> None:
    """Weak TUI idle must not skip post-stable; delayed auth → blocked (#99)."""
    _bind_env(monkeypatch, tmp_path, run_id=run_id)
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "grok")
    monkeypatch.setenv("OMG_TEAM_POST_STABLE_OBSERVE_S", "3")
    script = tmp_path / "tui_idle_auth.py"
    script.write_text(
        "import time\n"
        f"print({idle_line!r}, flush=True)\n"
        "time.sleep(1.0)\n"
        "print('authentication required: please log in', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="grok",
        argv=[sys.executable, str(script)],
    )
    _prepublish(tmp_path, desc=desc, run_id=run_id)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["OMG_TEAM_POST_STABLE_OBSERVE_S"] = "3"
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
            "8",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id=run_id, team_id="team", worker_id="w1"
            )
            if record and record.get("phase") in (
                StartupPhase.BLOCKED.value,
                StartupPhase.TASK_DISPATCHED.value,
                StartupPhase.FAILED.value,
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] != StartupPhase.TASK_DISPATCHED.value, record
        assert record["phase"] == StartupPhase.BLOCKED.value, record
        out = wait_for_startup_acks(
            tmp_path,
            run_id=run_id,
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=100,
            poll_s=0.01,
        )
        assert out["startup_status"] == "blocked_start"
        assert out["startup_status"] != "running"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_grok_tui_idle_markers_are_provisional() -> None:
    from omg_cli.team.provider_ready import GrokStrategy

    for line in (">", "grok>", "  >  "):
        obs = GrokStrategy().observe(
            provider_pid=1,
            alive=True,
            capture_lines=[line],
            elapsed_s=0.1,
            identity_matched=True,
        )
        assert obs.status == "provisional", line
        assert obs.evidence_code == EvidenceCode.TUI_IDLE_PROMPT.value
    # Without provider binary identity, idle glyphs must not provisional-green.
    denied = GrokStrategy().observe(
        provider_pid=1,
        alive=True,
        capture_lines=[">"],
        elapsed_s=0.1,
        identity_matched=False,
    )
    assert denied.status == "pending"


def test_process_stable_requires_identity_match() -> None:
    from omg_cli.team.provider_ready import GrokStrategy

    matched = GrokStrategy().observe(
        provider_pid=1,
        alive=True,
        capture_lines=[],
        elapsed_s=1.0,
        identity_matched=True,
    )
    assert matched.status == "provisional"
    assert matched.evidence_code == EvidenceCode.PROCESS_STABLE.value
    mismatched = GrokStrategy().observe(
        provider_pid=1,
        alive=True,
        capture_lines=[],
        elapsed_s=1.0,
        identity_matched=False,
    )
    assert mismatched.status == "pending"


def test_post_stable_rejects_nan_inf() -> None:
    from omg_cli.team.supervisor import (
        DEFAULT_POST_STABLE_OBSERVE_S,
        POST_STABLE_OBSERVE_ENV,
        _resolve_post_stable_observe_s,
    )

    for raw in ("nan", "NaN", "inf", "+inf", "-inf", "Infinity"):
        assert (
            _resolve_post_stable_observe_s({POST_STABLE_OBSERVE_ENV: raw})
            == DEFAULT_POST_STABLE_OBSERVE_S
        ), raw


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
    _prepublish(tmp_path, desc=desc, run_id="run-sup")
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
    _prepublish(tmp_path, desc=desc, run_id="run-exit")
    rc = run_supervisor(descriptor_path=desc, ready_timeout_s=2.0, poll_s=0.05)
    assert rc == 0  # child exit 0
    record = read_startup_record(
        tmp_path, run_id="run-exit", team_id="team", worker_id="w1"
    )
    assert record is not None
    assert record["phase"] == StartupPhase.FAILED.value
    assert record.get("evidence_code") == EvidenceCode.PROVIDER_EXITED.value
    out = wait_for_startup_acks(
        tmp_path,
        run_id="run-exit",
        team_id="team",
        expected_workers=["w1"],
        timeout_ms=100,
        poll_s=0.01,
    )
    assert out["startup_status"] == "failed_start"
    assert out["startup_process_ready"] == 0


def test_supervisor_installs_signal_forwarding_before_spawn_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observable spawn receipt must never precede forwarding handlers."""
    import omg_cli.team.supervisor as supervisor

    _bind_env(monkeypatch, tmp_path, run_id="run-signal-order")
    script = _hold_script(tmp_path / "hold.py", seconds=0.2)
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="fake-ready",
        argv=[sys.executable, str(script)],
    )
    events: list[tuple[str, object]] = []
    real_write_startup_phase = supervisor.write_startup_phase

    def record_forwarding(
        child_pgid: int | None,
        child_pid: int | None,
        *,
        wrapper_pid: int | None = None,
    ) -> None:
        events.append(("forward", (child_pgid, child_pid, wrapper_pid)))

    def record_phase(*args: object, **kwargs: object) -> Path:
        phase = kwargs.get("phase")
        events.append(("phase", phase))
        return real_write_startup_phase(*args, **kwargs)

    monkeypatch.setattr(supervisor, "_forward_signals", record_forwarding)
    monkeypatch.setattr(supervisor, "write_startup_phase", record_phase)

    assert run_supervisor(
        descriptor_path=desc, ready_timeout_s=2.0, poll_s=0.01
    ) == 0

    spawned_index = events.index(("phase", StartupPhase.PROVIDER_SPAWNED))
    forwarding = [
        (index, payload)
        for index, (kind, payload) in enumerate(events)
        if kind == "forward"
    ]
    assert len(forwarding) == 2
    assert all(index < spawned_index for index, _payload in forwarding)
    initial = forwarding[0][1]
    refined = forwarding[1][1]
    assert isinstance(initial, tuple)
    assert initial[0] == initial[1]
    assert initial[2] is None
    assert isinstance(refined, tuple)
    assert refined[0] == refined[1]
    assert refined[1] == refined[2]


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
    _prepublish(tmp_path, desc=desc, run_id="run-auth")
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
    _prepublish(tmp_path, desc=desc, run_id="run-sig")
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
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(provider_pid, 0)
        except OSError:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"provider remained alive after supervisor exit: pid={provider_pid}")


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


def test_labeled_grok_python_sleep_not_running_via_process_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: argv=[python,-c,sleep] labeled provider=grok must NOT reach running."""
    _bind_env(monkeypatch, tmp_path, run_id="run-fake-grok-sleep")
    monkeypatch.setenv("OMG_TEAM_SUPERVISOR_READY_S", "2")
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="grok",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    _prepublish(tmp_path, desc=desc, run_id="run-fake-grok-sleep")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["OMG_TEAM_SUPERVISOR_READY_S"] = "2"
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
            "2",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 6.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path,
                run_id="run-fake-grok-sleep",
                team_id="team",
                worker_id="w1",
            )
            if record and record.get("phase") in (
                StartupPhase.FAILED.value,
                StartupPhase.BLOCKED.value,
                StartupPhase.TASK_DISPATCHED.value,
            ):
                break
            time.sleep(0.05)
        assert record is not None, "no startup record"
        assert record["phase"] != StartupPhase.TASK_DISPATCHED.value, record
        assert record["phase"] == StartupPhase.FAILED.value, record
        assert record.get("evidence_code") == EvidenceCode.TIMEOUT.value
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-fake-grok-sleep",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=100,
            poll_s=0.01,
        )
        assert out["startup_status"] != "running"
        assert out["startup_process_ready"] == 0
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_unknown_strategy_silent_hang_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4: unknown provider silent hang → timeout / not running."""
    _bind_env(monkeypatch, tmp_path, run_id="run-unknown-hang")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "unknown")
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="unknown-cli",
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    _prepublish(tmp_path, desc=desc, run_id="run-unknown-hang")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["OMG_TEAM_PROVIDER_STRATEGY"] = "unknown"
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
            "1.5",
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
                tmp_path, run_id="run-unknown-hang", team_id="team", worker_id="w1"
            )
            if record and record.get("phase") in (
                StartupPhase.FAILED.value,
                StartupPhase.TASK_DISPATCHED.value,
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] == StartupPhase.FAILED.value
        assert record.get("evidence_code") == EvidenceCode.TIMEOUT.value
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-unknown-hang",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=80,
            poll_s=0.01,
        )
        assert out["startup_status"] != "running"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_provider_ready_then_dies_before_wait_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4: ready then provider dies before leader wait → fail/degrade."""
    _bind_env(monkeypatch, tmp_path, run_id="run-die-after-ready")
    script = _hold_script(tmp_path / "hold.py", seconds=60.0)
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="fake-ready",
        argv=[sys.executable, str(script)],
    )
    _prepublish(tmp_path, desc=desc, run_id="run-die-after-ready")
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
                tmp_path,
                run_id="run-die-after-ready",
                team_id="team",
                worker_id="w1",
            )
            if (
                record
                and record.get("phase") == StartupPhase.TASK_DISPATCHED.value
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] == StartupPhase.TASK_DISPATCHED.value
        provider_pid = int(record["provider_pid"])
        os.kill(provider_pid, signal.SIGKILL)
        # Allow supervisor/OS to reap.
        deadline2 = time.monotonic() + 3.0
        while time.monotonic() < deadline2:
            if not provider_process_alive(
                provider_pid=provider_pid,
                provider_pid_start=record["provider_pid_start"],
            ):
                break
            time.sleep(0.05)
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-die-after-ready",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=200,
            poll_s=0.02,
        )
        assert out["startup_status"] != "running"
        assert out["startup_process_ready"] == 0
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_trailing_grok_cmdline_token_does_not_match_identity() -> None:
    """argv[2+] basename ``grok`` must not satisfy identity (trailing-token closed)."""
    from omg_cli.team.supervisor import provider_binary_identity_matches

    hold = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "grok"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.05)
        assert provider_binary_identity_matches(hold.pid, {"grok"}) is False
    finally:
        hold.terminate()
        try:
            hold.wait(timeout=2)
        except subprocess.TimeoutExpired:
            hold.kill()
            hold.wait(timeout=1)


def test_interpreter_script_basename_matches_identity() -> None:
    """Production shape: python|node argv0 + script argv1 basename ``grok``."""
    import tempfile

    from omg_cli.team.supervisor import provider_binary_identity_matches

    td = Path(tempfile.mkdtemp(prefix="omg99-interp-"))
    script = td / "grok"
    script.write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    hold = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.05)
        assert provider_binary_identity_matches(hold.pid, {"grok"}) is True
        assert provider_binary_identity_matches(hold.pid, {"not-grok"}) is False
    finally:
        hold.terminate()
        try:
            hold.wait(timeout=2)
        except subprocess.TimeoutExpired:
            hold.kill()
            hold.wait(timeout=1)


def test_process_stable_with_interpreter_script_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interpreter+script launcher can still process_stable (production shape)."""
    _bind_env(monkeypatch, tmp_path, run_id="run-interp-stable")
    monkeypatch.setenv("OMG_TEAM_POST_STABLE_OBSERVE_S", "0.5")
    script = tmp_path / "bin" / "grok"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="grok",
        argv=[sys.executable, str(script)],
    )
    _prepublish(tmp_path, desc=desc, run_id="run-interp-stable")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["OMG_TEAM_POST_STABLE_OBSERVE_S"] = "0.5"
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
    )
    try:
        deadline = time.monotonic() + 8.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id="run-interp-stable", team_id="team", worker_id="w1"
            )
            if record and record.get("phase") in (
                StartupPhase.TASK_DISPATCHED.value,
                StartupPhase.FAILED.value,
                StartupPhase.BLOCKED.value,
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] == StartupPhase.TASK_DISPATCHED.value, record
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-interp-stable",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=200,
            poll_s=0.02,
        )
        assert out["startup_status"] == "running"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_env_interpreter_script_identity_matches() -> None:
    """``env python script`` / ``env script`` forms; flags stay fail-closed."""
    from omg_cli.team.supervisor import provider_binary_identity_matches
    import tempfile

    td = Path(tempfile.mkdtemp(prefix="omg99-env-"))
    script = td / "grok"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    # env <interpreter> <script>
    hold = subprocess.Popen(
        ["/usr/bin/env", sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.05)
        assert provider_binary_identity_matches(hold.pid, {"grok"}) is True
    finally:
        hold.terminate()
        try:
            hold.wait(timeout=2)
        except subprocess.TimeoutExpired:
            hold.kill()
            hold.wait(timeout=1)
    # env with flags must not open a hole via skipping into argv[2+]
    hold2 = subprocess.Popen(
        ["/usr/bin/env", "-i", sys.executable, "-c", "import time; time.sleep(30)", "grok"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.05)
        assert provider_binary_identity_matches(hold2.pid, {"grok"}) is False
    finally:
        hold2.terminate()
        try:
            hold2.wait(timeout=2)
        except subprocess.TimeoutExpired:
            hold2.kill()
            hold2.wait(timeout=1)


def test_process_stable_with_matching_binary_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compiled provider basename may still use process_stable after identity match."""
    _bind_env(monkeypatch, tmp_path, run_id="run-real-identity")
    monkeypatch.setenv("OMG_TEAM_POST_STABLE_OBSERVE_S", "0.5")
    fake_grok = _compiled_sleeper(tmp_path / "bin" / "grok")
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="grok",
        argv=[str(fake_grok), "30"],
    )
    _prepublish(tmp_path, desc=desc, run_id="run-real-identity")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["OMG_TEAM_POST_STABLE_OBSERVE_S"] = "0.5"
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
    )
    try:
        deadline = time.monotonic() + 8.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id="run-real-identity", team_id="team", worker_id="w1"
            )
            if record and record.get("phase") in (
                StartupPhase.TASK_DISPATCHED.value,
                StartupPhase.FAILED.value,
                StartupPhase.BLOCKED.value,
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] == StartupPhase.TASK_DISPATCHED.value, record
        assert StartupPhase.PROVIDER_READY.value in (record.get("phases") or [])
        out = wait_for_startup_acks(
            tmp_path,
            run_id="run-real-identity",
            team_id="team",
            expected_workers=["w1"],
            timeout_ms=200,
            poll_s=0.02,
        )
        assert out["startup_status"] == "running"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_needs_pty_records_real_child_not_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3/B4: needs_pty provider_pid must be the real child, not pty.spawn wrapper."""
    _bind_env(monkeypatch, tmp_path, run_id="run-pty-child")
    monkeypatch.setenv("OMG_TEAM_POST_STABLE_OBSERVE_S", "0.5")
    fake_agy = _compiled_sleeper(tmp_path / "bin" / "agy")
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="agy",
        argv=[str(fake_agy), "30"],
        needs_pty=True,
    )
    _prepublish(tmp_path, desc=desc, run_id="run-pty-child")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["OMG_TEAM_POST_STABLE_OBSERVE_S"] = "0.5"
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
            "6",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 8.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id="run-pty-child", team_id="team", worker_id="w1"
            )
            if record and record.get("provider_pid") and record.get("phase") in (
                StartupPhase.PROVIDER_SPAWNED.value,
                StartupPhase.PROVIDER_READY.value,
                StartupPhase.TASK_DISPATCHED.value,
                StartupPhase.FAILED.value,
            ):
                break
            time.sleep(0.05)
        assert record is not None
        assert record.get("provider_pid")
        assert record["phase"] != StartupPhase.FAILED.value, record
        provider_pid = int(record["provider_pid"])
        assert provider_pid != int(record["supervisor_pid"])
        # Provider must not be the python -c pty.spawn wrapper.
        from omg_cli.team.supervisor import (
            _cmdline_tokens,
            provider_binary_identity_matches,
        )

        tokens = _cmdline_tokens(provider_pid)
        joined = " ".join(tokens)
        assert "pty.spawn" not in joined
        assert provider_binary_identity_matches(provider_pid, {"agy"})
        assert provider_pid != proc.pid
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_auth_after_finalize_window_documented_out_of_scope() -> None:
    """B2 regression note: auth after finalize is intentionally out of window.

    Post-stable observe + identity match shrink the false-green surface;
    infinite post-finalize watch is out of scope (#101). This test locks the
    helper floor so OMG_TEAM_POST_STABLE_OBSERVE_S=0 cannot nullify the window.
    """
    from omg_cli.team.supervisor import (
        DEFAULT_POST_STABLE_OBSERVE_S,
        MIN_POST_STABLE_OBSERVE_S,
        MIN_TUI_IDLE_POST_STABLE_S,
        POST_STABLE_OBSERVE_ENV,
        _resolve_post_stable_observe_s,
    )

    assert (
        _resolve_post_stable_observe_s({POST_STABLE_OBSERVE_ENV: "0"})
        == DEFAULT_POST_STABLE_OBSERVE_S
    )
    assert (
        _resolve_post_stable_observe_s({POST_STABLE_OBSERVE_ENV: "0.1"})
        == MIN_POST_STABLE_OBSERVE_S
    )
    assert MIN_TUI_IDLE_POST_STABLE_S >= MIN_POST_STABLE_OBSERVE_S


def test_supervisor_tees_provider_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3: provider stdout is teed (not swallowed into PIPE only)."""
    _bind_env(monkeypatch, tmp_path, run_id="run-tee")
    script = tmp_path / "tee_auth.py"
    script.write_text(
        "import time\n"
        "print('authentication required: please log in', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "grok")
    desc = write_provider_descriptor(
        tmp_path / "desc.json",
        provider="grok",
        argv=[sys.executable, str(script)],
    )
    _prepublish(tmp_path, desc=desc, run_id="run-tee")
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Allow blocked phase + tee.
        deadline = time.monotonic() + 5.0
        record = None
        while time.monotonic() < deadline:
            record = read_startup_record(
                tmp_path, run_id="run-tee", team_id="team", worker_id="w1"
            )
            if record and record.get("phase") == StartupPhase.BLOCKED.value:
                break
            time.sleep(0.05)
        assert record is not None
        assert record["phase"] == StartupPhase.BLOCKED.value
        # Collect teed stdout while supervisor still holds the blocked child.
        import select

        chunks: list[bytes] = []
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                if chunks:
                    break
                continue
            try:
                piece = os.read(fd, 4096)
            except OSError:
                break
            if not piece:
                break
            chunks.append(piece)
            if b"authentication required" in b"".join(chunks).lower():
                break
        text = b"".join(chunks).decode("utf-8", errors="replace")
        assert "authentication required" in text.lower()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
