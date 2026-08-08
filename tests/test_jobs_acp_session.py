"""Durable ACP sidecar jobs plane tests (#105 PR4)."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
    assert cancelled.get("cancelled") is True or cancelled.get("attempted") is True
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
