"""Hermetic tests for identity-fenced worker replacement (#69 PR5)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omg_cli.jobs.models import JobState
from omg_cli.jobs.runtime import wait_job
from omg_cli.jobs.store import read_job_record
from omg_cli.team import api as team_api
from omg_cli.team.launch import apply_job_completion, validate_execution_record
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, load_team_meta, start_team
from omg_cli.team.replacement import (
    ReplacementError,
    replace_worker,
    replacement_wal_path,
    seed_worker_binding,
)
from omg_cli.team.runtime import resume_for_identity


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


def _start_job_team(tmp_path: Path, *, team_id: str = "team") -> dict[str, Any]:
    return start_team(
        "replace jobs",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id=team_id,
    )


def test_lost_job_replacement_new_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job_team(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    old_job = meta["tasks"][0]["execution"]["job_id"]
    wait_job(tmp_path, old_job, timeout_s=30.0)

    result = replace_worker(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        worker_id="t1",
        mode="lost",
        expected_attempt=1,
        expected_launch_generation=1,
        idempotency_key="repl-job-1",
    )
    assert result.ok is True
    assert result.attempt == 2
    assert result.launch_generation == 2
    assert result.verified is False
    new_job = (result.execution or {})["job_id"]
    assert new_job != old_job
    record = read_job_record(tmp_path, new_job)
    assert record.request.get("team_id") == team_id
    # Old job remains inspectable historical evidence.
    old = read_job_record(tmp_path, old_job)
    assert old.job_id == old_job
    reloaded = load_team_meta(tmp_path, run_id)
    task = reloaded["tasks"][0]
    assert task["attempt"] == 2
    assert task["execution"]["job_id"] == new_job
    assert task["execution"]["launch_generation"] == 2
    assert len(task["prior_attempts"]) == 1
    assert task["prior_attempts"][0]["execution"]["job_id"] == old_job
    assert "token" not in json.dumps(task["prior_attempts"])
    wait_job(tmp_path, new_job, timeout_s=30.0)


def test_lost_pane_replacement_archives_and_relaunches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "pane replace",
        [{"task_id": "t1", "owned_files": ["a.py"]}],
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="pane",
    )
    run_id = str(meta["run_id"])
    team_id = str(meta.get("team_id") or "team")

    # Materialize a fake live pane handle for hermetic fencing.
    from omg_cli.team.plane import mutate_team_meta

    def _stamp(current: dict[str, Any]) -> dict[str, Any]:
        task = dict(current["tasks"][0])
        task["execution"] = {
            "schema": 1,
            "topology": "pane",
            "launch_generation": 1,
            "pane_id": "%99",
        }
        task["pane_id"] = "%99"
        task["attempt"] = 1
        seed_worker_binding(
            task, run_id=run_id, team_id=team_id, attempt=1, launch_generation=1
        )
        current["tasks"] = [task]
        return current

    mutate_team_meta(tmp_path, run_id, _stamp)

    launches: list[str] = []

    def _launcher(**_k: Any) -> str:
        launches.append("launched")
        return "%200"

    def _fence(_root: Any, _task: Any, *, meta: Any, mode: str) -> dict[str, Any]:
        assert mode == "lost"
        return {"ok": True, "reason": "proven_absent", "pane_id": "%99"}

    result = replace_worker(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        worker_id="t1",
        mode="lost",
        expected_attempt=1,
        expected_launch_generation=1,
        idempotency_key="repl-pane-1",
        pane_launcher=_launcher,
        pane_fence=_fence,
    )
    assert result.ok is True
    assert result.attempt == 2
    assert result.launch_generation == 2
    assert launches == ["launched"]
    assert (result.execution or {}).get("pane_id") == "%200"
    reloaded = load_team_meta(tmp_path, run_id)
    task = reloaded["tasks"][0]
    assert task["prior_attempts"][0]["execution"]["pane_id"] == "%99"
    assert task["execution"]["pane_id"] == "%200"


def test_cancel_failure_zero_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job_team(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    old_job = meta["tasks"][0]["execution"]["job_id"]
    before = {p.name for p in (tmp_path / ".omg" / "jobs").iterdir()}

    def _boom(_root: Any, _task: Any, *, reason: str = "") -> dict[str, Any]:
        return {"ok": False, "reason": "simulated_cancel_failure"}

    monkeypatch.setattr(
        "omg_cli.team.replacement.cancel_job_backed_worker", _boom
    )
    with pytest.raises(ReplacementError) as exc:
        replace_worker(
            tmp_path,
            run_id=run_id,
            team_id=team_id,
            worker_id="t1",
            mode="restart",
            expected_attempt=1,
            expected_launch_generation=1,
            idempotency_key="repl-cancel-fail",
        )
    assert exc.value.code == "E_TEAM_REPLACE_CANCEL"
    after = {p.name for p in (tmp_path / ".omg" / "jobs").iterdir()}
    assert after == before
    reloaded = load_team_meta(tmp_path, run_id)
    assert reloaded["tasks"][0]["execution"]["job_id"] == old_job
    wait_job(tmp_path, old_job, timeout_s=30.0)


def test_late_old_completion_and_renew_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job_team(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    old_job = meta["tasks"][0]["execution"]["job_id"]
    wait_job(tmp_path, old_job, timeout_s=30.0)

    # Seed API board + claim so we can prove token fencing.
    env = {EXPERIMENTAL_ENV: "1"}
    code, created = team_api.execute_team_api(
        "create-task",
        {
            "run_id": run_id,
            "team_id": team_id,
            "subject": "t1",
            "description": "t1",
            "workers": ["t1"],
        },
        root=tmp_path,
        env=env,
    )
    assert code == 0
    api_id = created["data"]["task"]["id"]
    from omg_cli.contracts.path_keys import exclusive_lock
    from omg_cli.team.plane import mutate_team_meta

    path = team_api._task_path(tmp_path, run_id, team_id, api_id)
    with exclusive_lock(path.with_suffix(".lock")):
        task = team_api._read_task(tmp_path, run_id, team_id, api_id)
        assert task is not None
        team_api._write_task(
            tmp_path,
            run_id,
            team_id,
            {
                **task,
                "binding": {
                    "schema": 1,
                    "logical_worker_id": "t1",
                    "api_task_id": api_id,
                    "attempt": 1,
                    "launch_generation": 1,
                },
                "version": task["version"] + 1,
            },
        )

    def _bind(current: dict[str, Any]) -> dict[str, Any]:
        row = dict(current["tasks"][0])
        seed_worker_binding(
            row,
            run_id=run_id,
            team_id=team_id,
            api_task_id=api_id,
            attempt=1,
            launch_generation=1,
        )
        current["tasks"] = [row]
        return current

    mutate_team_meta(tmp_path, run_id, _bind)

    code, claimed = team_api.execute_team_api(
        "claim-task",
        {"run_id": run_id, "team_id": team_id, "task_id": api_id, "worker": "t1"},
        root=tmp_path,
        env=env,
    )
    assert code == 0
    old_token = claimed["data"]["claimToken"]

    result = replace_worker(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        worker_id="t1",
        mode="failed",
        expected_attempt=1,
        expected_launch_generation=1,
        idempotency_key="repl-late-1",
    )
    assert result.ok is True
    new_meta = load_team_meta(tmp_path, run_id)
    task_row = new_meta["tasks"][0]

    decision = apply_job_completion(
        task_row,
        job_id=old_job,
        job_attempt=1,
        job_state=JobState.SUCCEEDED.value,
        claim_token=old_token,
        expected_claim_token=old_token,
        expected_attempt=1,
        expected_worker_id="t1",
        worker_id="t1",
        expected_launch_generation=1,
    )
    assert decision.accepted is False
    assert decision.reason in {"job_id_mismatch", "stale_attempt", "stale_launch_generation"}

    code, renewed = team_api.execute_team_api(
        "renew-task-claim",
        {
            "run_id": run_id,
            "team_id": team_id,
            "task_id": api_id,
            "worker": "t1",
            "claim_token": old_token,
            "attempt": 1,
        },
        root=tmp_path,
        env=env,
    )
    assert code != 0 or renewed.get("ok") is False

    wait_job(tmp_path, (result.execution or {})["job_id"], timeout_s=30.0)


def test_idempotent_retry_adopts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job_team(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    wait_job(tmp_path, meta["tasks"][0]["execution"]["job_id"], timeout_s=30.0)
    first = replace_worker(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        worker_id="t1",
        mode="lost",
        expected_attempt=1,
        expected_launch_generation=1,
        idempotency_key="repl-idem",
    )
    second = replace_worker(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        worker_id="t1",
        mode="lost",
        expected_attempt=1,
        expected_launch_generation=1,
        idempotency_key="repl-idem",
    )
    assert second.adopted is True
    assert second.execution == first.execution
    assert second.attempt == 2
    reloaded = load_team_meta(tmp_path, run_id)
    assert reloaded["tasks"][0]["execution"]["job_id"] == first.execution["job_id"]
    assert len(reloaded["tasks"][0]["prior_attempts"]) == 1
    wait_job(tmp_path, first.execution["job_id"], timeout_s=30.0)


def test_crash_after_intent_recovers_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job_team(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    old_job = meta["tasks"][0]["execution"]["job_id"]
    wait_job(tmp_path, old_job, timeout_s=30.0)

    # Publish intent WAL then stop before fence/launch (simulate crash).
    from omg_cli.team.replacement import WAL_CONTRACT, WAL_KIND, _write_wal

    wal_path = replacement_wal_path(tmp_path, run_id, "repl-crash-1")
    _write_wal(
        wal_path,
        {
            "store_kind": WAL_KIND,
            "writer_contract": WAL_CONTRACT,
            "state": "intent",
            "idempotency_key": "repl-crash-1",
            "run_id": run_id,
            "team_id": team_id,
            "worker_id": "t1",
            "mode": "lost",
            "expected_attempt": 1,
            "expected_launch_generation": 1,
            "new_attempt": 2,
            "new_launch_generation": 2,
            "topology": "job",
            "provider": "fake",
            "role": "executor",
            "api_task_id": None,
            "old_execution": meta["tasks"][0]["execution"],
            "new_execution": None,
            "dry_run": False,
        },
    )
    out = resume_for_identity(tmp_path, run_id, env={EXPERIMENTAL_ENV: "1"})
    assert "replacement_recover" in out
    recovered = out["replacement_recover"]["recovered"]
    assert recovered
    reloaded = load_team_meta(tmp_path, run_id)
    assert reloaded["tasks"][0]["attempt"] == 2
    assert reloaded["tasks"][0]["execution"]["job_id"] != old_job
    wait_job(tmp_path, reloaded["tasks"][0]["execution"]["job_id"], timeout_s=30.0)


def test_dual_handle_and_foreign_team_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job_team(tmp_path, team_id="team-a")
    run_id = str(meta["run_id"])
    from omg_cli.team.plane import mutate_team_meta

    def _corrupt(current: dict[str, Any]) -> dict[str, Any]:
        row = dict(current["tasks"][0])
        row["execution"] = {
            "schema": 1,
            "topology": "job",
            "launch_generation": 1,
            "job_id": row["execution"]["job_id"],
            "pane_id": "%1",
        }
        current["tasks"] = [row]
        return current

    mutate_team_meta(tmp_path, run_id, _corrupt)
    with pytest.raises(ReplacementError) as exc:
        replace_worker(
            tmp_path,
            run_id=run_id,
            team_id="team-a",
            worker_id="t1",
            mode="lost",
            expected_attempt=1,
            expected_launch_generation=1,
            idempotency_key="repl-dual",
        )
    assert exc.value.code == "E_TEAM_EXEC_XOR"

    # Foreign team_id vs meta
    meta_b = start_team(
        "replace jobs b",
        [{"task_id": "t1", "owned_files": ["b.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=False,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
        team_id="team-b",
        force=True,
    )
    with pytest.raises(ReplacementError) as exc2:
        replace_worker(
            tmp_path,
            run_id=str(meta_b["run_id"]),
            team_id="team-WRONG",
            worker_id="t1",
            mode="lost",
            expected_attempt=1,
            expected_launch_generation=1,
            idempotency_key="repl-foreign",
        )
    assert exc2.value.code == "E_TEAM_REPLACE_TEAM"
    wait_job(tmp_path, meta_b["tasks"][0]["execution"]["job_id"], timeout_s=30.0)


def test_stale_generation_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job_team(tmp_path)
    with pytest.raises(ReplacementError) as exc:
        replace_worker(
            tmp_path,
            run_id=str(meta["run_id"]),
            team_id=str(meta["team_id"]),
            worker_id="t1",
            mode="lost",
            expected_attempt=1,
            expected_launch_generation=99,
            idempotency_key="repl-stale-gen",
        )
    assert exc.value.code == "E_TEAM_REPLACE_CAS"
    wait_job(tmp_path, meta["tasks"][0]["execution"]["job_id"], timeout_s=30.0)


def test_dry_run_no_materialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "dry replace",
        [{"task_id": "t1", "owned_files": ["a.py"], "provider": "fake"}],
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
        worker_topology="job",
        executor="fixture",
    )
    run_id = str(meta["run_id"])
    team_id = str(meta.get("team_id") or "team")
    from omg_cli.team.plane import mutate_team_meta

    def _fix(current: dict[str, Any]) -> dict[str, Any]:
        row = dict(current["tasks"][0])
        execution = dict(row.get("execution") or {})
        execution["schema"] = 1
        execution["topology"] = "job"
        execution["launch_generation"] = 1
        execution.pop("job_id", None)
        execution.pop("pane_id", None)
        row["execution"] = execution
        row["attempt"] = 1
        seed_worker_binding(
            row, run_id=run_id, team_id=team_id, attempt=1, launch_generation=1
        )
        current["tasks"] = [row]
        return current

    mutate_team_meta(tmp_path, run_id, _fix)
    jobs_root = tmp_path / ".omg" / "jobs"
    jobs_before = list(jobs_root.glob("*")) if jobs_root.exists() else []
    result = replace_worker(
        tmp_path,
        run_id=run_id,
        team_id=team_id,
        worker_id="t1",
        mode="lost",
        expected_attempt=1,
        expected_launch_generation=1,
        idempotency_key="repl-dry",
        dry_run=True,
    )
    assert result.ok is True
    assert result.dry_run is True
    jobs_after = list(jobs_root.glob("*")) if jobs_root.exists() else []
    assert jobs_after == jobs_before
    assert "job_id" not in (result.execution or {})


def test_replace_worker_api_leader_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = _start_job_team(tmp_path)
    run_id = str(meta["run_id"])
    team_id = str(meta["team_id"])
    wait_job(tmp_path, meta["tasks"][0]["execution"]["job_id"], timeout_s=30.0)
    code, envelope = team_api.execute_team_api(
        "replace-worker",
        {
            "run_id": run_id,
            "team_id": team_id,
            "worker": "t1",
            "mode": "lost",
            "expected_attempt": 1,
            "expected_launch_generation": 1,
            "idempotency_key": "api-repl-1",
        },
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 0
    assert envelope["ok"] is True
    assert envelope["data"]["attempt"] == 2

    # Worker env must be denied.
    worker_env = {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_WORKER": "1",
        "OMG_TEAM_WORKER_ID": "t1",
        "OMG_TEAM_RUN_ID": run_id,
        "OMG_TEAM_ID": team_id,
    }
    code2, env2 = team_api.execute_team_api(
        "replace-worker",
        {
            "run_id": run_id,
            "team_id": team_id,
            "worker": "t1",
            "mode": "lost",
            "expected_attempt": 2,
            "expected_launch_generation": 2,
            "idempotency_key": "api-repl-worker-deny",
        },
        root=tmp_path,
        env=worker_env,
    )
    assert code2 != 0
    assert env2["ok"] is False
    assert env2["error"]["code"] == "E_TEAM_API_GATE"
    wait_job(tmp_path, envelope["data"]["execution"]["job_id"], timeout_s=30.0)


def test_execution_record_still_xor() -> None:
    with pytest.raises(Exception):
        validate_execution_record(
            {
                "schema": 1,
                "topology": "job",
                "launch_generation": 2,
                "job_id": "x",
                "pane_id": "%1",
            }
        )
