"""Hermetic P0 contract for OMX-shaped ``omg team api``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omg_cli.main import main
from omg_cli.team.api import (
    P0_OPERATIONS,
    TEAM_API_OPERATIONS,
    execute_team_api,
)
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    TEAM_WORKER_ENV,
    WORKER_ENV_MARKERS,
    start_team,
)


TEAM = "team-api"
SEED_TASKS = [{"task_id": "t-a", "owned_files": ["a.py"]}]


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
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)


def _seed_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Dry-run team start → CLI-stamped team.json for this run."""
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "team-api seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    return str(meta["run_id"])


def _exec(
    root: Path,
    op: str,
    payload: dict,
    *,
    run_id: str,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[int, dict]:
    if monkeypatch is not None:
        _env_on(monkeypatch)
    body = {"run_id": run_id, "team_id": TEAM, **payload}
    return execute_team_api(op, body, root=root)


def test_team_api_unknown_op_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    code, envelope = _exec(
        tmp_path, "not-a-real-op", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["operation"] == "not-a-real-op"
    assert envelope["error"]["code"] == "E_TEAM_API_UNKNOWN"


def test_team_api_non_p0_op_unimplemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    non_p0 = sorted(set(TEAM_API_OPERATIONS) - set(P0_OPERATIONS))[0]
    code, envelope = _exec(
        tmp_path,
        non_p0,
        {"team_name": TEAM},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_UNIMPLEMENTED"


def test_team_api_requires_experimental_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    code, envelope = execute_team_api(
        "mailbox-list",
        {"run_id": run_id, "team_id": TEAM, "worker": "w1"},
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"


def test_team_api_refuses_spawned_worker_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    code, envelope = execute_team_api(
        "mailbox-list",
        {"run_id": run_id, "team_id": TEAM, "worker": "w1"},
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"
    assert "spawned-worker" in envelope["error"]["message"]


def test_team_api_requires_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    code, envelope = execute_team_api(
        "create-task",
        {
            "run_id": "no-such-run",
            "team_id": TEAM,
            "subject": "x",
            "description": "y",
        },
        root=tmp_path,
    )
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "team_not_found"


def test_send_message_and_mailbox_list_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    code, sent = _exec(
        tmp_path,
        "send-message",
        {
            "from_worker": "leader",
            "to_worker": "worker-1",
            "body": "hello pane",
            "dedupe_key": "d1",
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert sent["ok"] is True
    message = sent["data"]["message"]
    assert message["recipient_id"] == "worker-1"
    assert message["duplicate"] is False

    code, listing = _exec(
        tmp_path,
        "mailbox-list",
        {"worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert listing["data"]["count"] == 1
    assert listing["data"]["messages"][0]["message_id"] == message["message_id"]

    code, ack = _exec(
        tmp_path,
        "mailbox-mark-delivered",
        {"worker": "worker-1", "message_id": message["message_id"]},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert ack["data"]["updated"] is True
    assert ack["data"]["message_id"] == message["message_id"]


def test_claim_task_requires_token_for_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    code, created = _exec(
        tmp_path,
        "create-task",
        {
            "subject": "ship mailbox",
            "description": "implement P0 api",
            "workers": ["worker-1"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    task = created["data"]["task"]
    assert task["id"] == "1"
    assert task["status"] == "pending"

    code, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert claimed["data"]["ok"] is True
    token = claimed["data"]["claimToken"]
    assert isinstance(token, str) and token

    # Register worker-2 then expect claim_conflict on already-claimed task.
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "register",
            "description": "w2",
            "workers": ["worker-2"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    code, conflict = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-2"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert conflict["ok"] is False
    assert conflict["error"]["details"]["error"] == "claim_conflict"

    code, bad = _exec(
        tmp_path,
        "transition-task-status",
        {
            "task_id": "1",
            "from": "in_progress",
            "to": "completed",
            "claim_token": "wrong-token",
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert bad["ok"] is False
    assert bad["error"]["code"] == "E_TEAM_API_FAILED"
    assert bad["error"].get("details", {}).get("error") == "claim_conflict"

    code, good = _exec(
        tmp_path,
        "transition-task-status",
        {
            "task_id": "1",
            "from": "in_progress",
            "to": "completed",
            "claim_token": token,
            "result": "done",
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert good["data"]["ok"] is True
    assert good["data"]["task"]["status"] == "completed"

    code, listed = _exec(
        tmp_path, "list-tasks", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0
    assert listed["data"]["count"] == 2
    assert any(t["status"] == "completed" for t in listed["data"]["tasks"])


def test_claim_without_config_reports_team_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    # Plant orphan task file without api-config.
    task_dir = (
        tmp_path
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / TEAM
        / "tasks"
    )
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task-9.json").write_text(
        json.dumps(
            {
                "id": "9",
                "subject": "orphan",
                "description": "x",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00Z",
                "depends_on": [],
                "blocked_by": [],
                "version": 1,
                "owner": None,
                "claim": None,
                "requires_code_change": False,
            }
        ),
        encoding="utf-8",
    )
    code, envelope = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "9", "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "team_not_found"


def test_release_task_claim_returns_to_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "a",
            "description": "b",
            "workers": ["worker-1"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    _, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    token = claimed["data"]["claimToken"]
    code, released = _exec(
        tmp_path,
        "release-task-claim",
        {"task_id": "1", "claim_token": token, "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert released["data"]["ok"] is True
    assert released["data"]["task"]["status"] == "pending"
    assert released["data"]["task"].get("claim") in (None, {})


def test_read_config_get_summary_and_write_worker_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "s",
            "description": "d",
            "workers": ["worker-1", "worker-2"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    code, cfg = _exec(
        tmp_path, "read-config", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0
    assert cfg["data"]["config"]["team_id"] == TEAM
    assert cfg["data"]["config"]["next_task_id"] == 2
    assert cfg["data"]["plane"]["run_id"] == run_id
    names = {w["name"] for w in cfg["data"]["config"]["workers"]}
    assert names == {"worker-1", "worker-2"}

    code, summary = _exec(
        tmp_path, "get-summary", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0
    assert summary["data"]["summary"]["tasks"]["total"] == 1
    assert summary["data"]["summary"]["tasks"]["pending"] == 1
    assert summary["data"]["summary"]["workerCount"] == 2

    code, inbox = _exec(
        tmp_path,
        "write-worker-inbox",
        {"worker": "worker-1", "content": "# prompt\nDo the thing.\n"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    written = Path(inbox["data"]["path"])
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "# prompt\nDo the thing.\n"
    assert written.name == "inbox.md"


def test_cli_team_api_json_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    payload = {
        "run_id": run_id,
        "team_id": TEAM,
        "from_worker": "leader",
        "to_worker": "worker-1",
        "body": "via-cli",
        "dedupe_key": "cli-1",
    }
    rc = main(
        [
            "team",
            "api",
            "send-message",
            "--input",
            json.dumps(payload),
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["operation"] == "send-message"
    assert out["data"]["message"]["kind"] == "message"


def test_cli_team_api_gate_without_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "team",
            "api",
            "mailbox-list",
            "--input",
            json.dumps({"run_id": run_id, "team_id": TEAM, "worker": "w1"}),
        ]
    )
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == "E_TEAM_API_GATE"


def test_path_traversal_team_id_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    code, envelope = execute_team_api(
        "mailbox-list",
        {"run_id": run_id, "team_id": "../evil", "worker": "w1"},
        root=tmp_path,
    )
    assert code != 0
    assert envelope["ok"] is False


def test_p0_operations_subset_of_omx_names() -> None:
    assert set(P0_OPERATIONS) <= set(TEAM_API_OPERATIONS)
    for name in (
        "send-message",
        "mailbox-list",
        "mailbox-mark-delivered",
        "create-task",
        "list-tasks",
        "claim-task",
        "transition-task-status",
        "release-task-claim",
        "get-summary",
        "read-config",
        "write-worker-inbox",
    ):
        assert name in P0_OPERATIONS
