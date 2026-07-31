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
    WORKER_ENV_MARKERS,
    start_team,
)
from omg_cli.workers import worktree_dir


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
    from omg_cli.team.plane import DISABLE_ENV

    run_id = _seed_control_plane(tmp_path, monkeypatch)
    monkeypatch.setenv(DISABLE_ENV, "1")
    code, envelope = execute_team_api(
        "mailbox-list",
        {"run_id": run_id, "team_id": TEAM, "worker": "w1"},
        root=tmp_path,
        env={DISABLE_ENV: "1"},
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"


def test_team_api_refuses_spawned_worker_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    monkeypatch.setenv("OMG_SPAWNED_WORKER", "1")
    code, envelope = execute_team_api(
        "mailbox-list",
        {"run_id": run_id, "team_id": TEAM, "worker": "w1"},
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"
    assert "spawned-worker" in envelope["error"]["message"]


def test_team_worker_can_ack_but_not_create_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    # Register worker-1 on the board first (leader).
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "seed",
            "description": "seed",
            "workers": ["worker-1"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, sent = execute_team_api(
        "send-message",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "from_worker": "worker-1",
            "to_worker": "leader-fixed",
            "body": "ACK",
        },
        root=tmp_path,
    )
    assert code == 0
    assert sent["ok"] is True
    code, denied = execute_team_api(
        "create-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "subject": "nope",
            "description": "nope",
        },
        root=tmp_path,
    )
    assert code == 2
    assert denied["ok"] is False
    assert denied["error"]["code"] == "E_TEAM_API_GATE"


def test_team_worker_payload_bound_to_env_run_and_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {"subject": "seed", "description": "seed", "workers": ["worker-1"]},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    # Wrong run_id in payload must be overwritten or rejected
    code, envelope = execute_team_api(
        "send-message",
        {
            "run_id": "other-run",
            "team_id": TEAM,
            "from_worker": "worker-1",
            "to_worker": "leader-fixed",
            "body": "ACK",
        },
        root=tmp_path,
    )
    assert code != 0 or envelope["data"]["message"]["sender_id"] == "worker-1"
    # Prefer fail-closed on mismatch:
    assert code == 2
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"


def test_team_worker_without_run_id_env_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker identity matrix requires OMG_TEAM_RUN_ID (no soft-open on payload)."""
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {"subject": "seed", "description": "seed", "workers": ["worker-1"]},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    monkeypatch.delenv("OMG_TEAM_RUN_ID", raising=False)
    code, envelope = execute_team_api(
        "send-message",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "from_worker": "worker-1",
            "to_worker": "leader-fixed",
            "body": "ACK",
        },
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"


def _bind_worker_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    worker_id: str = "worker-1",
    owner_token: str | None = None,
) -> None:
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", worker_id)
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    if owner_token is None:
        monkeypatch.delenv("OMG_TEAM_OWNER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OMG_TEAM_OWNER_TOKEN", owner_token)


def _run_worker_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    leader_root: Path | None,
    op: str,
    payload: dict,
    state_root: Path | None = None,
    explicit_root: Path | None = None,
) -> tuple[int, Path]:
    worker_id = "t-a"
    worker_root = worktree_dir(tmp_path, run_id, worker_id)
    _bind_worker_env(
        monkeypatch,
        run_id=run_id,
        worker_id=worker_id,
        owner_token="test-owner-token",
    )
    monkeypatch.setenv(
        "OMG_TEAM_STATE_ROOT",
        str(state_root or (tmp_path / ".omg" / "state")),
    )
    if leader_root is None:
        monkeypatch.delenv("OMG_TEAM_LEADER_ROOT", raising=False)
    else:
        monkeypatch.setenv("OMG_TEAM_LEADER_ROOT", str(leader_root))
    monkeypatch.chdir(worker_root)
    argv = [
        "team",
        "api",
        op,
        "--input",
        json.dumps(payload),
        "--json",
    ]
    if explicit_root is not None:
        argv = ["--project-root", str(explicit_root), *argv]
    return main(argv), worker_root


def test_worker_mailbox_list_forged_worker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "mailbox-list",
        {"run_id": run_id, "team_id": TEAM, "worker": "worker-2"},
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"
    assert envelope["error"]["details"]["error"] == "identity_mismatch"


@pytest.mark.parametrize("marker", [None, "0", "unknown"])
def test_partial_worker_environment_never_falls_through_to_leader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str | None,
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    env = {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_WORKER_ID": "worker-1",
        "OMG_TEAM_RUN_ID": run_id,
        "OMG_TEAM_ID": TEAM,
    }
    if marker is not None:
        env["OMG_TEAM_WORKER"] = marker

    code, envelope = execute_team_api(
        "create-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "subject": "must not gain leader semantics",
            "description": "partial worker context",
        },
        root=tmp_path,
        env=env,
    )

    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"
    assert envelope["error"]["details"]["error"] == "worker_env_incomplete"


def test_worker_claim_task_forged_worker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "claim me",
            "description": "x",
            "workers": ["worker-1", "worker-2"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "claim-task",
        {"run_id": run_id, "team_id": TEAM, "task_id": "1", "worker": "worker-2"},
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "identity_mismatch"


def test_worker_env_transition_binds_worker_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix-level binding: worker env (not leader) owns claim + transition."""
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "owned",
            "description": "x",
            "workers": ["worker-1", "worker-2"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, claimed = execute_team_api(
        "claim-task",
        # Forged worker must be overwritten to env identity, not accepted.
        {"run_id": run_id, "team_id": TEAM, "task_id": "1"},
        root=tmp_path,
    )
    assert code == 0
    token = claimed["data"]["claimToken"]
    assert claimed["data"]["task"]["owner"] == "worker-1"

    code, done = execute_team_api(
        "transition-task-status",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "task_id": "1",
            "from": "in_progress",
            "to": "completed",
            "claim_token": token,
            "result": "ok",
        },
        root=tmp_path,
    )
    assert code == 0
    assert done["data"]["ok"] is True
    assert done["data"]["task"]["status"] == "completed"


def test_worker_owner_token_stripped_when_env_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    meta_path = (
        tmp_path / ".omg" / "state" / "runs" / run_id / "team" / "team.json"
    )
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    real_token = str(data.get("owner_token") or "")
    assert real_token
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "seed",
            "description": "seed",
            "workers": ["worker-1"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1", owner_token=None)
    # Inject forged owner_token in payload; matrix must strip it (env has none).
    code, envelope = execute_team_api(
        "send-message",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "from_worker": "worker-1",
            "to_worker": "leader-fixed",
            "body": "ACK",
            "owner_token": real_token,
        },
        root=tmp_path,
    )
    # send-message succeeds without trusting payload owner_token; strip is the gate.
    assert code == 0
    assert envelope["ok"] is True
    # Forged token must not leak into the stored message payload.
    msg = envelope["data"]["message"]
    assert "owner_token" not in msg


def test_worker_release_claim_forged_worker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "release me",
            "description": "x",
            "workers": ["worker-1", "worker-2"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    code, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    token = claimed["data"]["claimToken"]
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, forged = execute_team_api(
        "release-task-claim",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "task_id": "1",
            "claim_token": token,
            "worker": "worker-2",
        },
        root=tmp_path,
    )
    assert code == 2
    assert forged["error"]["details"]["error"] == "identity_mismatch"


def test_worker_update_heartbeat_forged_worker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "update-worker-heartbeat",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "worker": "worker-2",
            "task_id": "task-7",
            "generation": 0,
            "expected_sequence": 0,
        },
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "identity_mismatch"


def test_worker_update_heartbeat_forged_task_id_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "update-worker-heartbeat",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "task_id": "task-7",
            "generation": 0,
            "expected_sequence": 0,
        },
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"
    assert envelope["error"]["details"] == {
        "error": "identity_mismatch",
        "field": "task_id",
    }


def test_worker_update_heartbeat_injects_worker_and_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "update-worker-heartbeat",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "generation": 0,
            "expected_sequence": 0,
        },
        root=tmp_path,
    )
    assert code == 0, envelope
    assert envelope["data"]["worker"] == "worker-1"
    assert envelope["data"]["task_id"] == "worker-1"

    code, heartbeat = execute_team_api(
        "read-worker-heartbeat",
        {"run_id": run_id, "team_id": TEAM, "task_id": "worker-1"},
        root=tmp_path,
    )
    assert code == 0, heartbeat
    assert heartbeat["data"]["row"]["worker_id"] == "worker-1"
    assert heartbeat["data"]["row"]["task_id"] == "worker-1"


def test_worker_reads_team_visible_status_and_heartbeat_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")

    code, status = execute_team_api(
        "read-worker-status",
        {"run_id": run_id, "team_id": TEAM, "worker": "t-a"},
        root=tmp_path,
    )
    assert code == 0, status
    assert status["data"]["worker"] == "t-a"
    assert status["data"]["process_ready"] is False

    code, heartbeat = execute_team_api(
        "read-worker-heartbeat",
        {"run_id": run_id, "team_id": TEAM, "task_id": "t-a"},
        root=tmp_path,
    )
    assert code == 0, heartbeat
    assert heartbeat["data"]["task_id"] == "t-a"
    assert heartbeat["data"]["present"] is False


def test_worker_write_shutdown_ack_forged_worker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "write-shutdown-ack",
        {"run_id": run_id, "team_id": TEAM, "worker": "worker-2"},
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "identity_mismatch"


def test_worker_write_shutdown_ack_injects_env_worker_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "write-shutdown-ack",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
    )
    assert code == 0, envelope
    assert envelope["data"]["worker"] == "worker-1"

    code, persisted = _exec(
        tmp_path,
        "read-shutdown-ack",
        {"worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, persisted
    assert persisted["data"]["ack"]["worker"] == "worker-1"


def test_worker_append_event_forged_worker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "append-event",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "kind": "worker-progress",
            "worker": "worker-2",
            "body": {"step": 1},
        },
        root=tmp_path,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "identity_mismatch"


def test_worker_append_event_injects_env_worker_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    code, envelope = execute_team_api(
        "append-event",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "kind": "worker-progress",
            "body": {"step": 1},
        },
        root=tmp_path,
    )
    assert code == 0, envelope
    assert envelope["data"]["event"]["worker"] == "worker-1"
    code, persisted = execute_team_api(
        "read-events",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
    )
    assert code == 0, persisted
    assert persisted["data"]["events"][0]["worker"] == "worker-1"


def test_team_api_rejects_forged_minimal_team_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """writer-only team.json must not unlock API state materialization."""
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    run_id = "forged-run"
    team_dir = tmp_path / ".omg" / "state" / "runs" / run_id / "team"
    team_dir.mkdir(parents=True)
    path = team_dir / "team.json"
    path.write_text(json.dumps({"writer": "omg-cli"}), encoding="utf-8")
    path.chmod(0o600)
    code, envelope = execute_team_api(
        "create-task",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "subject": "x",
            "description": "y",
        },
        root=tmp_path,
    )
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "team_not_found"


def test_team_api_rejects_mismatched_run_id_in_team_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    meta_path = (
        tmp_path / ".omg" / "state" / "runs" / run_id / "team" / "team.json"
    )
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["run_id"] = "other-run"
    meta_path.write_text(json.dumps(data), encoding="utf-8")
    meta_path.chmod(0o600)
    code, envelope = execute_team_api(
        "mailbox-list",
        {"run_id": run_id, "team_id": TEAM, "worker": "w1"},
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
            "worker": "worker-1",
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
            "worker": "worker-1",
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


def test_transition_requires_worker_match_claim_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-worker token theft must not complete another worker's claim."""
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "owned by w1",
            "description": "x",
            "workers": ["worker-1", "worker-2"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    code, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    token = claimed["data"]["claimToken"]

    # worker-2 presents the stolen token → claim_conflict
    code, stolen = _exec(
        tmp_path,
        "transition-task-status",
        {
            "task_id": "1",
            "from": "in_progress",
            "to": "completed",
            "claim_token": token,
            "worker": "worker-2",
            "result": "hijack",
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert stolen["ok"] is False
    assert stolen["error"].get("details", {}).get("error") == "claim_conflict"

    # Missing worker → invalid input
    code, missing = _exec(
        tmp_path,
        "transition-task-status",
        {
            "task_id": "1",
            "from": "in_progress",
            "to": "completed",
            "claim_token": token,
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 2
    assert missing["ok"] is False


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


def test_cli_team_api_worker_routes_from_worktree_to_leader_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    rc, worker_root = _run_worker_cli(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        leader_root=tmp_path,
        op="send-message",
        payload={"to_worker": "leader-fixed", "body": "ACK-from-worktree"},
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "shadows ancestor control plane" not in captured.err
    out = json.loads(captured.out)
    assert out["data"]["message"]["body"] == "ACK-from-worktree"
    code, inbox = execute_team_api(
        "mailbox-list",
        {"run_id": run_id, "team_id": TEAM, "worker": "leader-fixed"},
        root=tmp_path,
        env={EXPERIMENTAL_ENV: "1"},
    )
    assert code == 0
    assert inbox["data"]["count"] == 1
    assert inbox["data"]["messages"][0]["sender_id"] == "t-a"
    shadow_mailbox = (
        worker_root
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "team"
        / TEAM
        / "mailbox"
        / "leader-fixed.json"
    )
    assert not shadow_mailbox.exists()


def test_cli_team_api_worker_without_leader_root_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    rc, _ = _run_worker_cli(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        leader_root=None,
        op="send-message",
        payload={"to_worker": "leader-fixed", "body": "must-not-send"},
    )

    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "E_TEAM_API_GATE"
    assert out["error"]["details"]["error"] == "worker_leader_root_invalid"


@pytest.mark.parametrize("invalid_kind", ["missing", "file", "relative"])
def test_cli_team_api_worker_rejects_invalid_leader_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid_kind: str,
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    invalid_root = tmp_path / "missing-leader"
    if invalid_kind == "file":
        invalid_root.write_text("not a directory\n", encoding="utf-8")
    elif invalid_kind == "relative":
        invalid_root = Path("relative-leader")
    rc, _ = _run_worker_cli(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        leader_root=invalid_root,
        op="mailbox-list",
        payload={"worker": "t-a"},
    )

    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "E_TEAM_API_GATE"
    assert out["error"]["details"]["error"] == "worker_leader_root_invalid"


def test_cli_team_api_worker_rejects_symlinked_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    fake_root = tmp_path / "fake-leader"
    fake_root.mkdir()
    (fake_root / ".omg").symlink_to(tmp_path / ".omg", target_is_directory=True)
    rc, _ = _run_worker_cli(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        leader_root=fake_root,
        op="mailbox-list",
        payload={"worker": "t-a"},
    )

    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "E_TEAM_API_GATE"
    assert out["error"]["details"]["error"] == "worker_leader_root_invalid"


@pytest.mark.parametrize("state_kind", ["mismatch", "symlink"])
def test_cli_team_api_worker_rejects_invalid_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state_kind: str,
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    state_root = tmp_path / "other-state"
    if state_kind == "mismatch":
        state_root.mkdir()
    else:
        state_root.symlink_to(tmp_path / ".omg" / "state", target_is_directory=True)
    rc, _ = _run_worker_cli(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        leader_root=tmp_path,
        state_root=state_root,
        op="mailbox-list",
        payload={"worker": "t-a"},
    )

    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "E_TEAM_API_GATE"
    assert out["error"]["details"]["error"] == "worker_leader_root_invalid"


def test_cli_team_api_worker_accepts_canonical_leader_root_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    leader_link = tmp_path.parent / f"{tmp_path.name}-leader-link"
    leader_link.symlink_to(tmp_path, target_is_directory=True)
    rc, _ = _run_worker_cli(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        leader_root=leader_link,
        op="send-message",
        payload={"to_worker": "leader-fixed", "body": "ACK-via-link"},
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize("override_source", ["argument", "environment"])
def test_cli_team_api_worker_rejects_project_root_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override_source: str,
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    worker_root = worktree_dir(tmp_path, run_id, "t-a")
    explicit_root = worker_root if override_source == "argument" else None
    if override_source == "environment":
        monkeypatch.setenv("OMG_PROJECT_ROOT", str(worker_root))
    rc, _ = _run_worker_cli(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        leader_root=tmp_path,
        explicit_root=explicit_root,
        op="mailbox-list",
        payload={"worker": "t-a"},
    )

    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "E_TEAM_API_GATE"
    assert out["error"]["details"]["error"] == "worker_leader_root_invalid"


def test_cli_team_api_worker_cross_repo_run_does_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    other_root = tmp_path.parent / f"{tmp_path.name}-other-repo"
    assert _seed_control_plane(other_root, monkeypatch) != run_id
    rc, _ = _run_worker_cli(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        leader_root=other_root,
        state_root=other_root / ".omg" / "state",
        op="send-message",
        payload={"to_worker": "leader-fixed", "body": "must-not-send"},
    )

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "E_TEAM_API_FAILED"
    assert out["error"]["details"]["error"] == "team_not_found"
    assert not (other_root / ".omg" / "state" / "runs" / run_id).exists()


def test_cli_team_api_gate_with_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omg_cli.team.plane import DISABLE_ENV

    run_id = _seed_control_plane(tmp_path, monkeypatch)
    monkeypatch.setenv(DISABLE_ENV, "1")
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
