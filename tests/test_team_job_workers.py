"""Hermetic tests for job-backed Team workers (#69 PR4)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omg_cli.jobs.models import JobState
from omg_cli.jobs.runtime import cancel_job, wait_job
from omg_cli.jobs.store import read_job_record
from omg_cli.team.launch import (
    WorkerExecutionHandle,
    WorkerLaunchError,
    apply_job_completion,
    cancel_job_backed_worker,
    claim_launch_or_release,
    launch_worker,
    resume_bind_job_workers,
    stamp_execution_on_task,
    validate_execution_record,
    worker_status_view,
)
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    WORKER_ENV_MARKERS,
    start_team,
    stop_team,
)
from omg_cli.team.runtime import resume_for_identity, status_for_identity


SEED_TASKS = [
    {"task_id": "t-a", "owned_files": ["a.py"], "provider": "fake", "role": "executor"},
    {"task_id": "t-b", "owned_files": ["b.py"], "provider": "fake", "role": "executor"},
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in (
        *WORKER_ENV_MARKERS,
        "OMG_TEAM_WORKER_ID",
        "OMG_TEAM_RUN_ID",
        "OMG_TEAM_ID",
        "OMG_TEAM_LEADER_ROOT",
        "OMG_TEAM_STATE_ROOT",
        "OMG_TEAM_OWNER_TOKEN",
        "OMG_PROJECT_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_pane_topology_unchanged_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "pane dry",
        [{"task_id": "t1", "owned_files": ["a.py"]}],
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="pane",
    )
    assert meta["worker_topology"] == "pane"
    assert meta["dry_run"] is True
    task = meta["tasks"][0]
    assert task["execution"]["topology"] == "pane"
    assert "job_id" not in task["execution"]
    assert "pane_id" not in task["execution"]


def test_job_topology_launches_durable_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "job live",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
    )
    assert meta["worker_topology"] == "job"
    assert meta["dry_run"] is False
    task = meta["tasks"][0]
    execution = validate_execution_record(task["execution"])
    assert execution["topology"] == "job"
    assert execution["job_id"]
    assert "pane_id" not in execution
    record = read_job_record(tmp_path, execution["job_id"])
    assert record.job_id == execution["job_id"]
    wait_job(tmp_path, execution["job_id"], timeout_s=30.0)


def test_execution_descriptor_serialized() -> None:
    handle = WorkerExecutionHandle(
        topology="job",
        worker_id="w1",
        provider="fake",
        launch_generation=2,
        job_id="20260809T120000Z-abcd1234",
        attempt=1,
    )
    record = handle.to_execution_record()
    assert record == {
        "schema": 1,
        "topology": "job",
        "launch_generation": 2,
        "job_id": "20260809T120000Z-abcd1234",
    }
    assert validate_execution_record(record)["job_id"] == record["job_id"]
    view = handle.to_status_view()
    assert view == {"topology": "job", "job_id": "20260809T120000Z-abcd1234"}


def test_failed_job_creation_releases_claim() -> None:
    released: list[str] = []

    def _boom() -> WorkerExecutionHandle:
        raise WorkerLaunchError("job creation failed", code="E_TEAM_JOB_CREATE")

    with pytest.raises(WorkerLaunchError):
        claim_launch_or_release(
            launch=_boom,
            release_claim=lambda: released.append("released"),
        )
    assert released == ["released"]


def test_missing_job_metadata_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)

    def _fake_start(*_a: Any, **_k: Any) -> Any:
        class _R:
            record = type(
                "Rec",
                (),
                {"job_id": "20260809T120000Z-deadbeef"},
            )()

        return _R()

    monkeypatch.setattr("omg_cli.team.launch.start_job", _fake_start)

    def _missing(_root: Path, _jid: str) -> None:
        from omg_cli.jobs.models import JobStoreError

        raise JobStoreError("gone", code="E_JOB_MISSING")

    monkeypatch.setattr("omg_cli.team.launch.read_job_record", _missing)
    with pytest.raises(WorkerLaunchError) as exc:
        launch_worker(
            tmp_path,
            worker_id="w1",
            topology="job",
            provider="fake",
            dry_run=False,
        )
    assert exc.value.code == "E_TEAM_JOB_MISSING"


def test_duplicate_execution_handle_rejected() -> None:
    task: dict[str, Any] = {
        "task_id": "t1",
        "execution": {
            "schema": 1,
            "topology": "job",
            "launch_generation": 1,
            "job_id": "20260809T120000Z-aaaa1111",
        },
    }
    handle = WorkerExecutionHandle(
        topology="job",
        worker_id="t1",
        provider="fake",
        launch_generation=1,
        job_id="20260809T120000Z-bbbb2222",
    )
    with pytest.raises(WorkerLaunchError) as exc:
        stamp_execution_on_task(task, handle)
    assert exc.value.code == "E_TEAM_EXEC_DUP"


def test_xor_both_ids_rejected() -> None:
    with pytest.raises(WorkerLaunchError) as exc:
        WorkerExecutionHandle(
            topology="job",
            worker_id="w1",
            provider="fake",
            launch_generation=1,
            job_id="20260809T120000Z-aaaa1111",
            pane_id="%1",
        )
    assert exc.value.code == "E_TEAM_EXEC_XOR"


def test_stamp_refuses_corrupt_dual_id_prior() -> None:
    task: dict[str, Any] = {
        "task_id": "t1",
        "execution": {
            "schema": 1,
            "topology": "job",
            "launch_generation": 1,
            "job_id": "20260809T120000Z-aaaa1111",
            "pane_id": "%1",
        },
    }
    handle = WorkerExecutionHandle(
        topology="job",
        worker_id="t1",
        provider="fake",
        launch_generation=2,
        job_id="20260809T120000Z-bbbb2222",
    )
    with pytest.raises(WorkerLaunchError) as exc:
        stamp_execution_on_task(task, handle)
    assert exc.value.code == "E_TEAM_EXEC_XOR"
    # Prior corrupt record must remain untouched (no heal-by-overwrite).
    assert task["execution"]["job_id"] == "20260809T120000Z-aaaa1111"
    assert task["execution"]["pane_id"] == "%1"


def test_topology_drift_refused() -> None:
    task: dict[str, Any] = {
        "task_id": "t1",
        "execution": {
            "schema": 1,
            "topology": "pane",
            "launch_generation": 1,
            "pane_id": "%1",
        },
    }
    handle = WorkerExecutionHandle(
        topology="job",
        worker_id="t1",
        provider="fake",
        launch_generation=2,
        job_id="20260809T120000Z-aaaa1111",
    )
    with pytest.raises(WorkerLaunchError) as exc:
        stamp_execution_on_task(task, handle)
    assert exc.value.code == "E_TEAM_TOPOLOGY_DRIFT"


def test_leader_restart_binds_existing_job_no_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "resume jobs",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
    )
    run_id = str(meta["run_id"])
    job_id = meta["tasks"][0]["execution"]["job_id"]
    before = list((tmp_path / ".omg" / "jobs").iterdir())
    out = resume_for_identity(tmp_path, run_id, env={EXPERIMENTAL_ENV: "1"})
    assert "job_bind" in out
    bound = out["job_bind"]["bound"]
    assert len(bound) == 1
    assert bound[0]["job_id"] == job_id
    assert bound[0]["relaunched"] is False
    assert out["job_bind"]["relaunched"] == []
    after = list((tmp_path / ".omg" / "jobs").iterdir())
    assert {p.name for p in before} == {p.name for p in after}
    wait_job(tmp_path, job_id, timeout_s=30.0)


def test_stale_attempt_completion_ignored() -> None:
    task = {
        "task_id": "t1",
        "execution": {
            "schema": 1,
            "topology": "job",
            "launch_generation": 1,
            "job_id": "20260809T120000Z-aaaa1111",
        },
    }
    decision = apply_job_completion(
        task,
        job_id="20260809T120000Z-aaaa1111",
        job_attempt=1,
        job_state=JobState.SUCCEEDED.value,
        claim_token="tok-new",
        expected_claim_token="tok-new",
        expected_attempt=2,
        expected_worker_id="t1",
        worker_id="t1",
    )
    assert decision.accepted is False
    assert decision.reason == "stale_attempt"


def test_claim_token_mismatch_ignored() -> None:
    task = {
        "task_id": "t1",
        "execution": {
            "schema": 1,
            "topology": "job",
            "launch_generation": 1,
            "job_id": "20260809T120000Z-aaaa1111",
        },
    }
    decision = apply_job_completion(
        task,
        job_id="20260809T120000Z-aaaa1111",
        job_attempt=1,
        job_state=JobState.SUCCEEDED.value,
        claim_token="old",
        expected_claim_token="new",
        expected_attempt=1,
        expected_worker_id="t1",
        worker_id="t1",
    )
    assert decision.accepted is False
    assert decision.reason == "claim_token_mismatch"


def test_claim_tokens_none_none_rejected() -> None:
    task = {
        "task_id": "t1",
        "execution": {
            "schema": 1,
            "topology": "job",
            "launch_generation": 1,
            "job_id": "20260809T120000Z-aaaa1111",
        },
    }
    decision = apply_job_completion(
        task,
        job_id="20260809T120000Z-aaaa1111",
        job_attempt=1,
        job_state=JobState.SUCCEEDED.value,
        claim_token=None,
        expected_claim_token=None,
        expected_attempt=1,
        expected_worker_id="t1",
        worker_id="t1",
    )
    assert decision.accepted is False
    assert decision.reason == "claim_token_required"


def test_cancel_job_cancels_task_no_succeeded_after_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    handle = launch_worker(
        tmp_path,
        worker_id="t1",
        topology="job",
        provider="fake",
        dry_run=False,
        sleep_s=60.0,
        prompt_text="slow worker",
    )
    task = {"task_id": "t1"}
    stamp_execution_on_task(task, handle)
    result = cancel_job_backed_worker(tmp_path, task, reason="test")
    assert result["ok"] is True
    assert result["task_status"] == "cancelled"
    # Late completion with wrong attempt must not promote.
    decision2 = apply_job_completion(
        task,
        job_id=handle.job_id or "",
        job_attempt=1,
        job_state=JobState.SUCCEEDED.value,
        claim_token="tok",
        expected_claim_token="tok",
        expected_attempt=99,
        expected_worker_id="t1",
        worker_id="t1",
    )
    assert decision2.accepted is False
    cancel_job(tmp_path, handle.job_id or "")


def test_stop_team_cancels_job_backed_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "stop jobs",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
    )
    run_id = str(meta["run_id"])
    job_id = meta["tasks"][0]["execution"]["job_id"]
    out = stop_team(tmp_path, run_id, force=True)
    assert out.get("worker_topology") == "job"
    assert out.get("ok") is True
    record = read_job_record(tmp_path, job_id)
    assert record.state in {
        JobState.CANCELLED,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.LOST,
    }
    reloaded = json.loads(
        (tmp_path / ".omg" / "state" / "runs" / run_id / "team" / "team.json").read_text(
            encoding="utf-8"
        )
    )
    assert reloaded.get("stop_state") == "stopped"
    assert reloaded["tasks"][0]["status"] == "cancelled"


def test_stop_team_cancel_failure_does_not_claim_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "stop fail",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id="team-stop",
    )
    run_id = str(meta["run_id"])
    prior_status = meta["tasks"][0].get("status")

    def _boom(_root: Path, _task: Any, *, reason: str = "team_stop") -> dict[str, Any]:
        return {"ok": False, "reason": "simulated_cancel_failure", "job_id": "x"}

    monkeypatch.setattr(
        "omg_cli.team.launch.cancel_job_backed_worker", _boom
    )
    out = stop_team(tmp_path, run_id, force=True)
    assert out.get("ok") is False
    assert any("simulated_cancel_failure" in e for e in out.get("errors") or [])
    reloaded = json.loads(
        (tmp_path / ".omg" / "state" / "runs" / run_id / "team" / "team.json").read_text(
            encoding="utf-8"
        )
    )
    assert reloaded.get("stop_state") != "stopped"
    assert reloaded["tasks"][0].get("status") == prior_status


def test_job_launch_stamps_team_id_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "own",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id="team-own",
    )
    job_id = meta["tasks"][0]["execution"]["job_id"]
    record = read_job_record(tmp_path, job_id)
    assert record.request.get("team_id") == "team-own"
    wait_job(tmp_path, job_id, timeout_s=30.0)


def test_two_teams_worker_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    a = tmp_path / "team-a"
    b = tmp_path / "team-b"
    _init_repo(a)
    _init_repo(b)
    meta_a = start_team(
        "A",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=a,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id="team-a",
    )
    meta_b = start_team(
        "B",
        [{"task_id": "t1", "owned_files": ["b.py"], "provider": "fake"}],
        root=b,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id="team-b",
    )
    job_a = meta_a["tasks"][0]["execution"]["job_id"]
    job_b = meta_b["tasks"][0]["execution"]["job_id"]
    assert job_a != job_b
    bind = resume_bind_job_workers(
        a,
        meta_b["tasks"],
        team_id="team-a",
    )
    assert bind["bound"] == []
    assert any(u.get("reason") == "unknown_job" for u in bind["unproven"])
    wait_job(a, job_a, timeout_s=30.0)
    wait_job(b, job_b, timeout_s=30.0)


def test_same_root_cross_team_bind_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same project root: team-a must not bind team-b's job_id."""
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta_a = start_team(
        "A",
        [{"task_id": "t-a", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id="team-a",
    )
    meta_b = start_team(
        "B",
        [{"task_id": "t-b", "owned_files": ["b.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id="team-b",
        force=True,
    )
    job_b = meta_b["tasks"][0]["execution"]["job_id"]
    assert read_job_record(tmp_path, job_b).request.get("team_id") == "team-b"
    # Poison team-a's view with team-b's job handle (same root).
    foreign_tasks = [
        {
            "task_id": "t-a",
            "execution": {
                "schema": 1,
                "topology": "job",
                "launch_generation": 1,
                "job_id": job_b,
            },
        }
    ]
    bind = resume_bind_job_workers(
        tmp_path, foreign_tasks, team_id="team-a"
    )
    assert bind["bound"] == []
    assert any(u.get("reason") == "foreign_team_job" for u in bind["unproven"])
    wait_job(tmp_path, meta_a["tasks"][0]["execution"]["job_id"], timeout_s=30.0)
    wait_job(tmp_path, job_b, timeout_s=30.0)


def test_dry_run_job_topology_no_job_no_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "dry job",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
    )
    assert meta["worker_topology"] == "job"
    assert meta["dry_run"] is True
    jobs_root = tmp_path / ".omg" / "jobs"
    assert not jobs_root.exists() or not any(jobs_root.iterdir())
    for task in meta["tasks"]:
        execution = task["execution"]
        assert execution["topology"] == "job"
        assert "job_id" not in execution
        assert "pane_id" not in execution
        assert task.get("pid") is None


def test_pane_and_job_dry_run_parity_except_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    pane_root = tmp_path / "pane"
    job_root = tmp_path / "job"
    _init_repo(pane_root)
    _init_repo(job_root)
    tasks = [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}]
    pane_meta = start_team(
        "parity",
        tasks,
        root=pane_root,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="pane",
        executor="fixture",
    )
    job_meta = start_team(
        "parity",
        tasks,
        root=job_root,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
    )

    def _strip(meta: dict[str, Any]) -> dict[str, Any]:
        out = {
            k: v
            for k, v in meta.items()
            if k
            not in {
                "run_id",
                "created_at",
                "session",
                "owner_token",
                "note",
                "view_mode",
                "layout",
                "worker_topology",
            }
        }
        tasks_out = []
        for t in out.get("tasks") or []:
            row = {
                k: v
                for k, v in t.items()
                if k
                not in {
                    "execution",
                    "worker_topology",
                    "job_id",
                    "pane_id",
                    "pane_command",
                    "argv",
                    "argv_path",
                    "worktree",
                    # Topology-specific I/O stamps (#147); shared refuse flags stay.
                    "io_mode",
                    "provider_tty_owner",
                }
            }
            tasks_out.append(row)
        out["tasks"] = tasks_out
        return out

    assert _strip(pane_meta) == _strip(job_meta)
    assert pane_meta["worker_topology"] == "pane"
    assert job_meta["worker_topology"] == "job"
    assert pane_meta["tasks"][0]["execution"]["topology"] == "pane"
    assert job_meta["tasks"][0]["execution"]["topology"] == "job"


def test_status_exposes_worker_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "status",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
    )
    st = status_for_identity(tmp_path, str(meta["run_id"]))
    assert st["worker_topology"] == "job"
    assert st["workers"][0]["worker"]["topology"] == "job"


def test_unknown_job_is_unproven(tmp_path: Path) -> None:
    task = {
        "task_id": "t1",
        "execution": {
            "schema": 1,
            "topology": "job",
            "launch_generation": 1,
            "job_id": "20260809T120000Z-missing1",
        },
    }
    bind = resume_bind_job_workers(tmp_path, [task], team_id="team")
    assert bind["bound"] == []
    assert bind["unproven"][0]["reason"] == "unknown_job"
    assert bind["unproven"][0]["health"] == "unproven"


def test_worker_status_view_legacy_pane() -> None:
    view = worker_status_view({"task_id": "t1", "pane_id": "%42"})
    assert view == {
        "topology": "pane",
        "pane_id": "%42",
        "io": {
            "io_mode": "unproven",
            "provider_tty_owner": "unknown",
            "input_ready": False,
            "operator_input_supported": False,
            "interaction_evidence": None,
        },
    }


def test_cli_worker_topology_choices() -> None:
    from omg_cli.main import build_parser

    parser = build_parser()
    ns = parser.parse_args(
        [
            "team",
            "start",
            "--goal",
            "x",
            "--tasks-json",
            "[]",
            "--worker-topology",
            "job",
            "--dry-run",
        ]
    )
    assert ns.worker_topology == "job"
    ns2 = parser.parse_args(
        ["team", "run", "--goal", "x", "--tasks-json", "[]", "--worker-topology", "job"]
    )
    assert ns2.worker_topology == "job"
