"""P0′ team API: heartbeat, shutdown request/ack, worker status, orphan-cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omg_cli.team.api import P0_OPERATIONS, TEAM_API_OPERATIONS, execute_team_api
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, start_team
from omg_cli.team.runtime import write_worker_ready_receipt


SEED_TASKS = [{"task_id": "w1", "owned_files": ["a.py"]}]


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


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    _init_repo(tmp_path)
    meta = start_team(
        "reliability seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    return str(meta["run_id"])


def test_p0_prime_ops_are_catalog_subset() -> None:
    assert set(P0_OPERATIONS) <= set(TEAM_API_OPERATIONS)


def test_heartbeat_and_status_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    team_id = "team"
    env = {EXPERIMENTAL_ENV: "1"}
    write_worker_ready_receipt(
        tmp_path, run_id=run_id, team_id=team_id, worker_id="w1", source="test"
    )
    code, envelope = execute_team_api(
        "update-worker-heartbeat",
        {
            "run_id": run_id,
            "team_id": team_id,
            "worker": "w1",
            "task_id": "w1",
            "generation": 0,
            "expected_sequence": 0,
        },
        root=tmp_path,
        env=env,
    )
    assert code == 0, envelope
    assert envelope["ok"] is True
    code2, env2 = execute_team_api(
        "read-worker-heartbeat",
        {"run_id": run_id, "team_id": team_id, "task_id": "w1"},
        root=tmp_path,
        env=env,
    )
    assert code2 == 0 and env2["data"]["present"] is True
    code3, env3 = execute_team_api(
        "read-worker-status",
        {"run_id": run_id, "team_id": team_id, "worker": "w1"},
        root=tmp_path,
        env=env,
    )
    assert code3 == 0 and env3["data"]["process_ready"] is True


def test_shutdown_request_and_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    team_id = "team"
    env = {EXPERIMENTAL_ENV: "1"}
    code, envelope = execute_team_api(
        "write-shutdown-request",
        {"run_id": run_id, "team_id": team_id, "force": False},
        root=tmp_path,
        env=env,
    )
    assert code == 0, envelope
    code_r, env_r = execute_team_api(
        "read-shutdown-request",
        {"run_id": run_id, "team_id": team_id},
        root=tmp_path,
        env=env,
    )
    assert code_r == 0 and env_r["data"]["present"] is True
    code_a, env_a = execute_team_api(
        "write-shutdown-ack",
        {"run_id": run_id, "team_id": team_id, "worker": "w1"},
        root=tmp_path,
        env=env,
    )
    assert code_a == 0, env_a
    code_ra, env_ra = execute_team_api(
        "read-shutdown-ack",
        {"run_id": run_id, "team_id": team_id, "worker": "w1"},
        root=tmp_path,
        env=env,
    )
    assert code_ra == 0 and env_ra["data"]["present"] is True


def test_orphan_cleanup_no_panic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    env = {EXPERIMENTAL_ENV: "1"}
    code, envelope = execute_team_api(
        "orphan-cleanup",
        {"run_id": run_id, "team_id": "team"},
        root=tmp_path,
        env=env,
    )
    assert code == 0, envelope
    assert envelope["ok"] is True
    assert "cleaned_task_ids" in envelope["data"]


def test_doctor_team_plane_soft_check() -> None:
    from omg_cli.doctor import check_team_plane

    name, level, detail = check_team_plane()
    assert name == "team plane"
    assert level in {"ok", "warn"}
    assert "api handlers=" in detail
    assert "tmux=" in detail
