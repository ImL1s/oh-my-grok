"""Broadcast as N DMs (#69 catalog v5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.team.api import execute_team_api
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, start_team

TEAM = "team-api"
SEED_TASKS = [{"task_id": "t-a", "owned_files": ["a.py"]}]


def _git(cwd: Path, *args: str) -> None:
    import subprocess

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
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)


def test_broadcast_sends_n_dms_and_is_leader_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "broadcast seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    run_id = str(meta["run_id"])
    for worker in ("t-a", "t-b"):
        code, created = execute_team_api(
            "create-task",
            {
                "run_id": run_id,
                "team_id": TEAM,
                "subject": f"seed-{worker}",
                "description": f"seed-{worker}",
                "workers": [worker],
            },
            root=tmp_path,
        )
        assert code == 0, created
    code, env = execute_team_api(
        "broadcast",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "from_worker": "leader",
            "body": "hello-all",
            "dedupe_key": "bcast-1",
        },
        root=tmp_path,
    )
    assert code == 0
    assert env["data"]["count"] == 2
    assert set(env["data"]["recipients"]) == {"t-a", "t-b"}
    for worker in ("t-a", "t-b"):
        code, listing = execute_team_api(
            "mailbox-list",
            {"run_id": run_id, "team_id": TEAM, "worker": worker},
            root=tmp_path,
        )
        assert code == 0
        assert listing["data"]["count"] == 1

    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "t-a")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, denied = execute_team_api(
        "broadcast",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "from_worker": "t-a",
            "body": "nope",
        },
        root=tmp_path,
    )
    assert code == 2
    assert denied["error"]["code"] == "E_TEAM_API_GATE"


def test_broadcast_omitted_dedupe_key_is_retry_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "broadcast seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    run_id = str(meta["run_id"])
    for worker in ("t-a", "t-b"):
        code, created = execute_team_api(
            "create-task",
            {
                "run_id": run_id,
                "team_id": TEAM,
                "subject": f"seed-{worker}",
                "description": f"seed-{worker}",
                "workers": [worker],
            },
            root=tmp_path,
        )
        assert code == 0, created
    payload = {
        "run_id": run_id,
        "team_id": TEAM,
        "from_worker": "leader",
        "body": "hello-retry",
    }
    code1, env1 = execute_team_api("broadcast", payload, root=tmp_path)
    assert code1 == 0
    assert env1["data"]["count"] == 2
    first_ids = {msg["message_id"] for msg in env1["data"]["messages"]}
    code2, env2 = execute_team_api("broadcast", payload, root=tmp_path)
    assert code2 == 0
    second_ids = {msg["message_id"] for msg in env2["data"]["messages"]}
    assert first_ids == second_ids
    for worker in ("t-a", "t-b"):
        code, listing = execute_team_api(
            "mailbox-list",
            {"run_id": run_id, "team_id": TEAM, "worker": worker},
            root=tmp_path,
        )
        assert code == 0
        assert listing["data"]["count"] == 1


def test_broadcast_long_dedupe_key_stays_within_safe_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "broadcast seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    run_id = str(meta["run_id"])
    for worker in ("t-a", "t-b"):
        code, created = execute_team_api(
            "create-task",
            {
                "run_id": run_id,
                "team_id": TEAM,
                "subject": f"seed-{worker}",
                "description": f"seed-{worker}",
                "workers": [worker],
            },
            root=tmp_path,
        )
        assert code == 0, created
    long_key = "k" + ("a" * 127)
    assert len(long_key) == 128
    payload = {
        "run_id": run_id,
        "team_id": TEAM,
        "from_worker": "leader",
        "body": "hello-long-key",
        "dedupe_key": long_key,
    }
    code1, env1 = execute_team_api("broadcast", payload, root=tmp_path)
    assert code1 == 0, env1
    assert env1["data"]["count"] == 2
    for msg in env1["data"]["messages"]:
        assert len(msg["dedupe_key"]) <= 128
        assert msg["dedupe_key"].startswith("bd-")
    first_ids = {msg["message_id"] for msg in env1["data"]["messages"]}
    code2, env2 = execute_team_api("broadcast", payload, root=tmp_path)
    assert code2 == 0
    assert {msg["message_id"] for msg in env2["data"]["messages"]} == first_ids
    for worker in ("t-a", "t-b"):
        code, listing = execute_team_api(
            "mailbox-list",
            {"run_id": run_id, "team_id": TEAM, "worker": worker},
            root=tmp_path,
        )
        assert code == 0
        assert listing["data"]["count"] == 1


def test_broadcast_omitted_key_hashes_redacted_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "broadcast seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    run_id = str(meta["run_id"])
    for worker in ("t-a", "t-b"):
        code, created = execute_team_api(
            "create-task",
            {
                "run_id": run_id,
                "team_id": TEAM,
                "subject": f"seed-{worker}",
                "description": f"seed-{worker}",
                "workers": [worker],
            },
            root=tmp_path,
        )
        assert code == 0, created
    payload = {
        "run_id": run_id,
        "team_id": TEAM,
        "from_worker": "leader",
        "body": {"token": "123456", "note": "hi"},
    }
    code1, env1 = execute_team_api("broadcast", payload, root=tmp_path)
    assert code1 == 0, env1
    first_ids = {msg["message_id"] for msg in env1["data"]["messages"]}
    bodies = [msg["body"] for msg in env1["data"]["messages"]]
    assert all(item.get("token") != "123456" for item in bodies)
    code2, env2 = execute_team_api("broadcast", payload, root=tmp_path)
    assert code2 == 0
    assert {msg["message_id"] for msg in env2["data"]["messages"]} == first_ids
