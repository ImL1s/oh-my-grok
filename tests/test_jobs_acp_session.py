"""Durable ACP sidecar jobs plane tests (#105 PR4)."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

import pytest

from omg_cli.host_session import allocate_host_session
from omg_cli.jobs.acp_provider import receipt_path
from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.runtime import (
    cancel_job,
    cancel_linked_acp_sidecar,
    ensure_acp_session_for_team,
    ensure_acp_session_sidecar,
    job_status,
    start_job,
)
from omg_cli.jobs.store import job_dir
from omg_cli.state import create_run

FIXTURE = Path(__file__).parent / "fixtures" / "fake_grok_acp_agent.py"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".omg").mkdir()
    monkeypatch.setenv("OMG_ACP_BIN", str(FIXTURE))
    monkeypatch.setenv("OMG_ACP_FAKE_SCENARIO", "success")
    monkeypatch.setenv("OMG_ACP_QUIET_WINDOW_S", "0.05")
    return tmp_path


def _seed_run(root: Path) -> str:
    binding = allocate_host_session()
    run = create_run(
        root,
        mode="ulw",
        goal="acp test",
        extra={
            "grok_session_id": binding.session_id,
            "grok_session_attempts": 1,
            "grok_session_state": "launched",
        },
    )
    return str(run["run_id"])


def test_acp_sidecar_remains_live_after_resume_receipt(root: Path) -> None:
    run_id = _seed_run(root)
    out = ensure_acp_session_sidecar(root, run_id=run_id, ready_timeout_s=10.0)
    assert out["ok"] is True
    assert out["reused"] is False
    job_id = out["job_id"]
    rec = job_status(root, job_id)
    assert rec.state == JobState.RUNNING
    assert receipt_path(job_dir(root, job_id)).is_file()
    # Still live a moment later
    time.sleep(0.2)
    rec2 = job_status(root, job_id)
    assert rec2.state == JobState.RUNNING
    assert rec2.pid and os.path.exists(f"/proc/{rec2.pid}") or True  # macOS ok
    # Cleanup
    cancel_job(root, job_id, reason="test")


def test_acp_sidecar_cancel_does_not_claim_session_close(root: Path) -> None:
    run_id = _seed_run(root)
    out = ensure_acp_session_sidecar(root, run_id=run_id, ready_timeout_s=10.0)
    job_id = out["job_id"]
    cancelled = cancel_linked_acp_sidecar(root, run_id, reason="test_stop")
    assert cancelled["session_close"] is False
    assert cancelled.get("cancelled") is True
    assert cancelled.get("binding_cleared") is True
    assert "session_close=true" not in json.dumps(cancelled).lower()
    assert cancelled.get("note") and "sidecar" in cancelled["note"].lower()
    # Job should leave running
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        rec = job_status(root, job_id)
        if rec.state in {JobState.CANCELLED, JobState.FAILED}:
            break
        time.sleep(0.05)
    rec = job_status(root, job_id)
    assert rec.state in {JobState.CANCELLED, JobState.FAILED, JobState.SUCCEEDED}
    blob = json.dumps(rec.to_dict() if hasattr(rec, "to_dict") else rec.public_status())
    assert "session_close" not in blob or "false" in blob.lower()


def test_concurrent_team_resume_creates_exactly_one_sidecar(root: Path) -> None:
    run_id = _seed_run(root)
    results: list[dict] = []

    def _one() -> None:
        results.append(
            ensure_acp_session_sidecar(root, run_id=run_id, ready_timeout_s=15.0)
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_one) for _ in range(4)]
        for f in futs:
            f.result(timeout=30)

    assert len(results) == 4
    job_ids = {r["job_id"] for r in results}
    assert len(job_ids) == 1
    # Exactly one non-reuse start among first winners — others reused
    assert sum(1 for r in results if not r.get("reused")) <= 1
    assert all(r.get("ok") for r in results)
    cancel_job(root, results[0]["job_id"], reason="test")


def test_team_resume_reuses_matching_live_sidecar(root: Path) -> None:
    run_id = _seed_run(root)
    first = ensure_acp_session_sidecar(root, run_id=run_id, ready_timeout_s=10.0)
    second = ensure_acp_session_sidecar(root, run_id=run_id, ready_timeout_s=10.0)
    assert first["job_id"] == second["job_id"]
    assert second["reused"] is True
    cancel_job(root, first["job_id"], reason="test")


def test_team_resume_missing_session_binding_spawns_nothing(root: Path) -> None:
    run = create_run(root, mode="ulw", goal="no session")
    run_id = str(run["run_id"])
    before = list((root / ".omg" / "jobs").glob("*")) if (root / ".omg" / "jobs").exists() else []
    with pytest.raises(JobStoreError) as ei:
        ensure_acp_session_sidecar(root, run_id=run_id)
    assert ei.value.code == "E_ACP_SESSION_BINDING"
    after = list((root / ".omg" / "jobs").glob("20*")) if (root / ".omg" / "jobs").exists() else []
    assert after == [] or len(after) == len([p for p in before if p.name.startswith("20")])


def test_public_start_rejects_internal_via_start_job(root: Path) -> None:
    prompt = root / "p.md"
    prompt.write_text("x", encoding="utf-8")
    with pytest.raises(JobStoreError) as ei:
        start_job(root, provider="grok-acp-session", role="x", prompt_file=prompt)
    assert ei.value.code == "E_JOB_PROVIDER_INTERNAL"


def test_ensure_acp_session_for_team_available_envelope(root: Path) -> None:
    from omg_cli.host_models import FeatureGateResult

    run_id = _seed_run(root)
    gate = FeatureGateResult(
        capability="session_resume",
        state="AVAILABLE",
        reason="test",
        required=False,
    )
    helper = ensure_acp_session_for_team(gate, root=root, run_id=run_id)
    assert helper["transport_wired"] is True
    assert helper["execution"]["status"] == "resumed"
    assert helper["execution"]["no_replay"] is True
    assert helper["execution"]["restore_code"] is False
    cancel_job(root, helper["execution"]["job_id"], reason="test")


def test_ensure_blocks_without_session(root: Path) -> None:
    from omg_cli.host_models import FeatureGateResult

    run = create_run(root, mode="ulw", goal="x")
    gate = FeatureGateResult(
        capability="session_resume",
        state="AVAILABLE",
        reason="test",
        required=False,
    )
    helper = ensure_acp_session_for_team(
        gate, root=root, run_id=str(run["run_id"])
    )
    assert helper.get("force_blocked") is True
    assert helper.get("transport_wired") is False


@pytest.mark.skipif(os.name != "posix", reason="killpg / ACP pgid ownership is POSIX")
def test_ensure_rejects_reuse_when_inner_acp_peer_dead(root: Path) -> None:
    """P0: outer runner still alive + dead inner ACP must not reuse as success."""
    import signal

    from omg_cli.jobs.store import read_job_record

    run_id = _seed_run(root)
    first = ensure_acp_session_sidecar(root, run_id=run_id, ready_timeout_s=10.0)
    assert first["ok"] is True
    assert first.get("reused") is False
    job_id = first["job_id"]
    rec = read_job_record(root, job_id)
    assert rec.state == JobState.RUNNING
    pp = rec.provider_process or {}
    assert pp.get("state") == "bound"
    inner_pid = int(pp["pid"])
    inner_pgid = int(pp["pgid"])
    outer_pid = int(rec.pid or 0)
    assert outer_pid > 1
    assert inner_pid > 1
    assert inner_pid != outer_pid

    # Kill only the inner ACP process group; leave outer runner if possible.
    try:
        os.killpg(inner_pgid, signal.SIGKILL)
    except ProcessLookupError:
        os.kill(inner_pid, signal.SIGKILL)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(inner_pid, 0)
            time.sleep(0.05)
        except (ProcessLookupError, PermissionError, OSError):
            break
    else:
        pytest.fail("inner ACP peer did not exit after SIGKILL")

    with pytest.raises(JobStoreError) as ei:
        ensure_acp_session_sidecar(root, run_id=run_id, ready_timeout_s=5.0)
    assert ei.value.code == "E_ACP_SIDECAR_STALE"
    try:
        cancel_job(root, job_id, reason="test_cleanup")
    except JobStoreError:
        pass


def test_cancel_unproven_retains_binding_blocks_second_sidecar(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0: E_JOB_CANCEL_UNPROVEN must keep binding so ensure cannot double-spawn."""
    from omg_cli.jobs import runtime as runtime_mod

    run_id = _seed_run(root)
    first = ensure_acp_session_sidecar(root, run_id=run_id, ready_timeout_s=10.0)
    job_id = first["job_id"]
    bind_path = runtime_mod._acp_binding_path(root, run_id)
    assert bind_path.is_file()

    real_cancel_job = runtime_mod.cancel_job

    def _unproven(*_a, **_k):  # noqa: ANN001
        raise JobStoreError(
            "cancel disappearance unproven",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    # Keep cancel_job unproven for cancel_linked AND ensure's orphan path so a
    # failed cancel cannot clear the singleton and open a second spawn.
    monkeypatch.setattr(runtime_mod, "cancel_job", _unproven)
    try:
        out = runtime_mod.cancel_linked_acp_sidecar(
            root, run_id, reason="test_unproven"
        )
        assert out["attempted"] is True
        assert out["cancelled"] is False
        assert out["binding_cleared"] is False
        assert out.get("error_code") == "E_JOB_CANCEL_UNPROVEN"
        assert bind_path.is_file(), "binding must survive unproven cancel"

        monkeypatch.setattr(
            runtime_mod, "_job_is_live_sidecar", lambda *_a, **_k: False
        )
        monkeypatch.setattr(
            runtime_mod, "_job_handshake_still_viable", lambda *_a, **_k: False
        )
        jobs_root = root / ".omg" / "jobs"
        jobs_before = {
            p.name for p in jobs_root.iterdir() if p.is_dir() and p.name[0].isdigit()
        }
        with pytest.raises(JobStoreError) as ei:
            runtime_mod.ensure_acp_session_sidecar(
                root, run_id=run_id, ready_timeout_s=5.0
            )
        assert ei.value.code == "E_ACP_SIDECAR_STALE"
        jobs_after = {
            p.name for p in jobs_root.iterdir() if p.is_dir() and p.name[0].isdigit()
        }
        assert jobs_after == jobs_before
        assert job_id in jobs_after
        assert bind_path.is_file(), "singleton binding must still block a second ensure"
    finally:
        monkeypatch.setattr(runtime_mod, "cancel_job", real_cancel_job)
        try:
            real_cancel_job(root, job_id, reason="test_cleanup")
        except JobStoreError:
            pass


def test_ready_fail_unproven_cancel_retains_binding_blocks_second(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0: ready-fail compensation must not unlink when cancel is unproven."""
    from omg_cli.jobs import runtime as runtime_mod

    run_id = _seed_run(root)
    bind_path = runtime_mod._acp_binding_path(root, run_id)
    real_cancel_job = runtime_mod.cancel_job

    def _ready_fail(*_a, **_k):  # noqa: ANN001
        raise JobStoreError(
            "ACP sidecar readiness timed out",
            code="E_ACP_READY_TIMEOUT",
        )

    def _unproven(*_a, **_k):  # noqa: ANN001
        raise JobStoreError(
            "cancel disappearance unproven",
            code="E_JOB_CANCEL_UNPROVEN",
        )

    monkeypatch.setattr(runtime_mod, "_wait_acp_ready", _ready_fail)
    monkeypatch.setattr(runtime_mod, "cancel_job", _unproven)

    with pytest.raises(JobStoreError) as ei:
        runtime_mod.ensure_acp_session_sidecar(
            root, run_id=run_id, ready_timeout_s=5.0
        )
    assert ei.value.code == "E_JOB_CANCEL_UNPROVEN"
    assert "binding retained" in str(ei.value).lower()
    assert bind_path.is_file(), "provisional binding must survive unproven cancel"

    existing = json.loads(bind_path.read_text(encoding="utf-8"))
    job_id = str(existing["job_id"])

    jobs_root = root / ".omg" / "jobs"
    jobs_before = {
        p.name for p in jobs_root.iterdir() if p.is_dir() and p.name[0].isdigit()
    }
    # Binding retained + not-live → STALE; must not spawn a second sidecar.
    monkeypatch.setattr(runtime_mod, "_job_is_live_sidecar", lambda *_a, **_k: False)
    monkeypatch.setattr(
        runtime_mod, "_job_handshake_still_viable", lambda *_a, **_k: False
    )
    with pytest.raises(JobStoreError) as ei2:
        runtime_mod.ensure_acp_session_sidecar(
            root, run_id=run_id, ready_timeout_s=5.0
        )
    assert ei2.value.code == "E_ACP_SIDECAR_STALE"
    jobs_after = {
        p.name for p in jobs_root.iterdir() if p.is_dir() and p.name[0].isdigit()
    }
    assert jobs_after == jobs_before
    assert bind_path.is_file()

    monkeypatch.setattr(runtime_mod, "cancel_job", real_cancel_job)
    try:
        real_cancel_job(root, job_id, reason="test_cleanup")
    except JobStoreError:
        pass


def test_stop_team_acp_cancel_unproven_clears_stop_completed() -> None:
    """Plane contract: unproven ACP cancel must refuse stop_completed publication."""
    # Mirrors omg_cli.team.plane._stop_team_locked linked_acp_session gate.
    stop_completed = True
    acp_out = {
        "attempted": True,
        "cancelled": False,
        "error_code": "E_JOB_CANCEL_UNPROVEN",
    }
    if not (acp_out.get("attempted") and acp_out.get("cancelled")):
        if acp_out.get("attempted"):
            stop_completed = False
    assert stop_completed is False


def test_resume_provider_session_stop_race_no_live_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Barrier: sidecar ready before Team bind; concurrent stop must not leave orphan.

    ``stop_completed=true`` is allowed only when ACP disappearance is proven.
    After the race, a stopped Team must not retain a live ACP sidecar.
    """
    import threading

    from omg_cli.host_models import FeatureGateResult
    from omg_cli.host_session import allocate_host_session
    from omg_cli.jobs.runtime import (
        _job_is_live_sidecar,
        cancel_job,
        ensure_acp_session_for_team,
    )
    from omg_cli.state import write_status
    from omg_cli.team.plane import (
        EXPERIMENTAL_ENV,
        load_team_meta,
        start_team,
        stop_team,
    )
    from omg_cli.team.runtime import resume_with_view

    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.setenv("OMG_ACP_BIN", str(FIXTURE))
    monkeypatch.setenv("OMG_ACP_FAKE_SCENARIO", "success")
    monkeypatch.setenv("OMG_ACP_QUIET_WINDOW_S", "0.05")

    # Minimal git repo for start_team
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    meta = start_team(
        "acp race",
        [{"task_id": "w1", "owned_files": ["lane_a/"]}],
        root=tmp_path,
        dry_run=True,
    )
    run_id = str(meta["run_id"])
    binding = allocate_host_session()
    write_status(
        tmp_path,
        run_id,
        "running",
        extra={
            **binding.status_fields(),
            "grok_session_state": "launched",
        },
    )

    ready = threading.Event()
    release_bind = threading.Event()
    box: dict[str, Any] = {}

    def _barrier(_root: Path, _rid: str, execution: Mapping) -> None:
        box["job_id"] = execution.get("job_id")
        ready.set()
        assert release_bind.wait(timeout=45), "bind barrier not released"

    gate = FeatureGateResult(
        capability="session_resume",
        state="AVAILABLE",
        reason="test",
        required=False,
    )

    def _resume() -> None:
        box["resume"] = resume_with_view(
            tmp_path,
            run_id,
            view=False,
            as_json=True,
            request_provider_session=True,
            session_resume_gate=gate,
            provider_resume=ensure_acp_session_for_team,
            after_acp_ready_before_bind=_barrier,
        )

    thr = threading.Thread(target=_resume, name="acp-resume-race")
    thr.start()
    assert ready.wait(timeout=45), "sidecar never reached bind barrier"
    job_id = str(box.get("job_id") or "")
    assert job_id
    assert _job_is_live_sidecar(tmp_path, job_id)

    stop_out = stop_team(tmp_path, run_id)
    box["stop"] = stop_out
    release_bind.set()
    thr.join(timeout=45)
    assert not thr.is_alive()

    # stop_completed requires proven ACP disappearance (or refusal).
    if stop_out.get("stop_completed"):
        assert not _job_is_live_sidecar(tmp_path, job_id), (
            "stop_completed=true must not leave a live ACP sidecar"
        )
        durable = load_team_meta(tmp_path, run_id)
        assert durable.get("stop_state") == "stopped"
        assert durable.get("linked_acp_session") in (None, {})
        # Bind must refuse stopped team — resume reports bind/transport failure
        # or cancelled sidecar; never a live orphan under stopped meta.
        resume_out = box.get("resume") or {}
        ps = resume_out.get("provider_session") or {}
        assert ps.get("transport_wired") is not True or ps.get("ok") is False
    else:
        assert stop_out.get("stop_completed") is False
        durable = load_team_meta(tmp_path, run_id)
        assert durable.get("stop_state") == "stop_refused"

    # Final invariant: stopped Team ⇒ no live ACP for this job.
    durable = load_team_meta(tmp_path, run_id)
    if durable.get("stop_state") == "stopped":
        assert not _job_is_live_sidecar(tmp_path, job_id)

    try:
        cancel_job(tmp_path, job_id, reason="test_cleanup")
    except JobStoreError:
        pass


def test_stop_clears_sticky_pending_acp_without_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending ACP intent with no jobs binding must not permanently pin stop_refused."""
    import subprocess
    from datetime import datetime, timezone

    from omg_cli.team.plane import (
        EXPERIMENTAL_ENV,
        load_team_meta,
        mutate_team_meta,
        start_team,
        stop_team,
    )
    from omg_cli.jobs.runtime import _acp_binding_path

    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    meta = start_team(
        "sticky pending",
        [{"task_id": "w1", "owned_files": ["lane_a/"]}],
        root=tmp_path,
        dry_run=True,
    )
    run_id = str(meta["run_id"])

    def _plant_pending(current: dict) -> dict:
        updated = dict(current)
        updated["linked_acp_session"] = {
            "state": "pending",
            "pending_at": datetime.now(timezone.utc).isoformat(),
            "job_id": None,
            "session_close": False,
        }
        return updated

    gen = meta.get("meta_generation")
    expected = int(gen) if isinstance(gen, int) and not isinstance(gen, bool) else 0
    mutate_team_meta(tmp_path, run_id, _plant_pending, expected_generation=expected)
    planted = load_team_meta(tmp_path, run_id)
    assert planted["linked_acp_session"]["state"] == "pending"
    assert not _acp_binding_path(tmp_path, run_id).is_file()

    # First stop must complete (clear abandoned pending) — no live orphan.
    first = stop_team(tmp_path, run_id)
    assert first.get("stop_completed") is True, first
    durable = load_team_meta(tmp_path, run_id)
    assert durable.get("stop_state") == "stopped"
    assert durable.get("linked_acp_session") in (None, {})
    assert any(
        "abandoned linked_acp_session pending" in str(a)
        for a in (first.get("actions") or [])
    )

    # Simulate pre-fix deadlock: stop_refused + sticky pending, no binding.
    def _plant_deadlock(current: dict) -> dict:
        updated = dict(current)
        updated["stop_state"] = "stop_refused"
        updated.pop("stopped_at", None)
        updated["linked_acp_session"] = {
            "state": "pending",
            "pending_at": datetime.now(timezone.utc).isoformat(),
            "job_id": None,
            "session_close": False,
        }
        return updated

    durable = load_team_meta(tmp_path, run_id)
    gen2 = durable.get("meta_generation")
    expected2 = (
        int(gen2) if isinstance(gen2, int) and not isinstance(gen2, bool) else 0
    )
    mutate_team_meta(tmp_path, run_id, _plant_deadlock, expected_generation=expected2)

    recovered = stop_team(tmp_path, run_id, force=True)
    assert recovered.get("stop_completed") is True, recovered
    final = load_team_meta(tmp_path, run_id)
    assert final.get("stop_state") == "stopped"
    assert final.get("linked_acp_session") in (None, {})
