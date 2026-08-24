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
    """v6 implements every named catalog op; unknown names stay fail-closed."""
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    leftover = set(TEAM_API_OPERATIONS) - set(P0_OPERATIONS)
    assert leftover == set()
    code, envelope = _exec(
        tmp_path,
        "not-in-catalog",
        {"team_name": TEAM},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_TEAM_API_UNKNOWN"


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


def _task_file(root: Path, run_id: str, task_id: str = "1") -> Path:
    from omg_cli.team.api import _task_path

    return _task_path(root, run_id, TEAM, task_id)


def _claim_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
    *,
    worker: str = "worker-1",
) -> tuple[str, dict]:
    _exec(
        tmp_path,
        "create-task",
        {
            "subject": "renew me",
            "description": "lease",
            "workers": ["worker-1", "worker-2"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    code, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": worker},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    token = claimed["data"]["claimToken"]
    return token, claimed["data"]["task"]


def test_renew_task_claim_extends_deadline_preserves_token_and_increments_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)

    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, task = _claim_one(tmp_path, monkeypatch, run_id)
    before = task["claim"]["leased_until"]
    version = task["version"]

    later = fixed + timedelta(minutes=5)
    monkeypatch.setattr(team_api, "_now_utc", lambda: later)
    code, renewed = _exec(
        tmp_path,
        "renew-task-claim",
        {
            "task_id": "1",
            "worker": "worker-1",
            "claim_token": token,
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert renewed["operation"] == "renew-task-claim"
    assert renewed["data"]["ok"] is True
    assert renewed["data"]["claimToken"] == token
    after_task = renewed["data"]["task"]
    assert after_task["status"] == "in_progress"
    assert after_task["owner"] == "worker-1"
    assert after_task["claim"]["token"] == token
    assert after_task["claim"]["owner"] == "worker-1"
    assert after_task["version"] == version + 1
    assert after_task["claim"]["leased_until"] > before
    expected_deadline = (
        later + timedelta(seconds=team_api.CLAIM_LEASE_SECONDS)
    ).isoformat().replace("+00:00", "Z")
    assert after_task["claim"]["leased_until"] == expected_deadline


def test_renew_task_claim_again_further_extends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    t0 = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: t0)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, _ = _claim_one(tmp_path, monkeypatch, run_id)

    t1 = t0 + timedelta(minutes=3)
    monkeypatch.setattr(team_api, "_now_utc", lambda: t1)
    code, first = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": "1", "worker": "worker-1", "claim_token": token},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    first_deadline = first["data"]["task"]["claim"]["leased_until"]
    first_version = first["data"]["task"]["version"]

    t2 = t0 + timedelta(minutes=6)
    monkeypatch.setattr(team_api, "_now_utc", lambda: t2)
    code, second = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": "1", "worker": "worker-1", "claim_token": token},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert second["data"]["claimToken"] == token
    assert second["data"]["task"]["claim"]["leased_until"] > first_deadline
    assert second["data"]["task"]["version"] == first_version + 1


def test_renew_task_claim_expected_version_conflict_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, task = _claim_one(tmp_path, monkeypatch, run_id)
    path = _task_file(tmp_path, run_id)
    before_bytes = path.read_bytes()

    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=2)
    )
    code, envelope = _exec(
        tmp_path,
        "renew-task-claim",
        {
            "task_id": "1",
            "worker": "worker-1",
            "claim_token": token,
            "expected_version": task["version"] + 99,
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "version_conflict"
    assert path.read_bytes() == before_bytes


def test_renew_task_claim_wrong_token_fails_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, task = _claim_one(tmp_path, monkeypatch, run_id)
    path = _task_file(tmp_path, run_id)
    before = path.read_bytes()

    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=1)
    )
    code, envelope = _exec(
        tmp_path,
        "renew-task-claim",
        {
            "task_id": "1",
            "worker": "worker-1",
            "claim_token": "not-" + token,
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert envelope["error"]["details"]["error"] == "claim_conflict"
    assert path.read_bytes() == before
    assert task["claim"]["leased_until"]


def test_renew_task_claim_wrong_worker_fails_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, _ = _claim_one(tmp_path, monkeypatch, run_id)
    path = _task_file(tmp_path, run_id)
    before = path.read_bytes()

    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=1)
    )
    code, envelope = _exec(
        tmp_path,
        "renew-task-claim",
        {
            "task_id": "1",
            "worker": "worker-2",
            "claim_token": token,
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert envelope["error"]["details"]["error"] == "claim_conflict"
    assert path.read_bytes() == before


def test_worker_renew_task_claim_binds_env_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, task = _claim_one(tmp_path, monkeypatch, run_id)
    version = task["version"]

    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=2)
    )
    code, renewed = execute_team_api(
        "renew-task-claim",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "task_id": "1",
            "claim_token": token,
        },
        root=tmp_path,
    )
    assert code == 0
    assert renewed["data"]["ok"] is True
    assert renewed["data"]["task"]["owner"] == "worker-1"
    assert renewed["data"]["claimToken"] == token
    assert renewed["data"]["task"]["version"] == version + 1


def test_worker_renew_task_claim_forged_worker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, _ = _claim_one(tmp_path, monkeypatch, run_id)
    path = _task_file(tmp_path, run_id)
    before = path.read_bytes()

    _bind_worker_env(monkeypatch, run_id=run_id, worker_id="worker-1")
    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=2)
    )
    code, forged = execute_team_api(
        "renew-task-claim",
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
    assert path.read_bytes() == before


def test_renew_task_claim_expired_lease_cannot_be_resurrected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    import omg_cli.team.api as team_api

    t0 = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: t0)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, task = _claim_one(tmp_path, monkeypatch, run_id)
    path = _task_file(tmp_path, run_id)
    before = path.read_bytes()
    deadline = datetime.fromisoformat(
        task["claim"]["leased_until"].replace("Z", "+00:00")
    )

    monkeypatch.setattr(team_api, "_now_utc", lambda: deadline)
    code, expired = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": "1", "worker": "worker-1", "claim_token": token},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert expired["error"]["details"]["error"] == "lease_expired"
    assert path.read_bytes() == before

    # Another registered worker can still recover via claim-task after expiry.
    code, recovered = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-2"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert recovered["data"]["ok"] is True
    assert recovered["data"]["task"]["owner"] == "worker-2"
    assert recovered["data"]["claimToken"] != token


@pytest.mark.parametrize(
    "setup",
    ["released", "terminal"],
)
def test_renew_task_claim_released_or_terminal_task_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup: str
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, _ = _claim_one(tmp_path, monkeypatch, run_id)

    if setup == "released":
        code, _ = _exec(
            tmp_path,
            "release-task-claim",
            {"task_id": "1", "claim_token": token, "worker": "worker-1"},
            run_id=run_id,
            monkeypatch=monkeypatch,
        )
        assert code == 0
        expected = "claim_conflict"
    else:
        code, _ = _exec(
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
        expected = "already_terminal"

    path = _task_file(tmp_path, run_id)
    before = path.read_bytes()
    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=1)
    )
    code, envelope = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": "1", "worker": "worker-1", "claim_token": token},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert envelope["error"]["details"]["error"] == expected
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "bad_deadline",
    [None, "not-a-timestamp", "2026-08-08T12:00:00"],
)
def test_renew_task_claim_missing_or_malformed_deadline_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_deadline: str | None,
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, task = _claim_one(tmp_path, monkeypatch, run_id)
    path = _task_file(tmp_path, run_id)
    from omg_cli.contracts.writer_chain import (
        canonical_json_bytes,
        parse_canonical_json_bytes,
    )

    data = parse_canonical_json_bytes(path.read_bytes())
    assert isinstance(data, dict)
    claim = dict(data["claim"])
    if bad_deadline is None:
        claim.pop("leased_until", None)
    else:
        claim["leased_until"] = bad_deadline
    data["claim"] = claim
    path.write_bytes(canonical_json_bytes(data))
    before = path.read_bytes()

    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=1)
    )
    code, envelope = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": "1", "worker": "worker-1", "claim_token": token},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["details"]["error"] == "lease_expired"
    assert path.read_bytes() == before
    assert task["status"] == "in_progress"


def test_renew_task_claim_never_shortens_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, task = _claim_one(tmp_path, monkeypatch, run_id)
    path = _task_file(tmp_path, run_id)
    before = path.read_bytes()
    long_deadline = task["claim"]["leased_until"]

    # Advance a little but shrink the lease constant so now+lease < existing.
    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=1)
    )
    monkeypatch.setattr(team_api, "CLAIM_LEASE_SECONDS", 30)
    code, envelope = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": "1", "worker": "worker-1", "claim_token": token},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert envelope["error"]["details"]["error"] == "lease_not_advanced"
    assert path.read_bytes() == before
    code, reread = _exec(
        tmp_path, "read-task", {"task_id": "1"}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0
    assert reread["data"]["task"]["claim"]["leased_until"] == long_deadline
    assert reread["data"]["task"]["version"] == task["version"]


def test_cli_team_api_renew_task_claim_json_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, _ = _claim_one(tmp_path, monkeypatch, run_id)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=2)
    )
    rc = main(
        [
            "team",
            "api",
            "renew-task-claim",
            "--input",
            json.dumps(
                {
                    "run_id": run_id,
                    "team_id": TEAM,
                    "task_id": "1",
                    "worker": "worker-1",
                    "claim_token": token,
                }
            ),
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["operation"] == "renew-task-claim"
    assert out["data"]["ok"] is True
    assert out["data"]["claimToken"] == token
    assert out["data"]["task"]["status"] == "in_progress"


def test_renew_task_claim_does_not_set_passes_or_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import omg_cli.team.api as team_api
    from omg_cli.state import load_run

    fixed = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(team_api, "_now_utc", lambda: fixed)
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    token, _ = _claim_one(tmp_path, monkeypatch, run_id)
    before = load_run(tmp_path, run_id) or {}
    before_verified = before.get("verified")
    before_passes = before.get("passes")

    monkeypatch.setattr(
        team_api, "_now_utc", lambda: fixed + timedelta(minutes=2)
    )
    code, renewed = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": "1", "worker": "worker-1", "claim_token": token},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert renewed["data"]["task"]["status"] == "in_progress"
    assert renewed["data"]["task"].get("completed_at") in (None, "")
    after = load_run(tmp_path, run_id) or {}
    assert after.get("verified") == before_verified
    assert after.get("passes") == before_passes
    assert after.get("verified") is not True


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
    assert set(P0_OPERATIONS) == set(TEAM_API_OPERATIONS)
    for name in (
        "send-message",
        "mailbox-list",
        "mailbox-mark-delivered",
        "create-task",
        "list-tasks",
        "claim-task",
        "transition-task-status",
        "release-task-claim",
        "renew-task-claim",
        "get-summary",
        "read-config",
        "write-worker-inbox",
        "mailbox-mark-notified",
        "await-event",
        "cleanup",
    ):
        assert name in P0_OPERATIONS


def _register_worker(tmp_path: Path, run_id: str, worker: str = "worker-1") -> None:
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


def test_mailbox_mark_notified_happy_and_worker_self_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _register_worker(tmp_path, run_id)
    code, sent = _exec(
        tmp_path,
        "send-message",
        {
            "from_worker": "leader",
            "to_worker": "worker-1",
            "body": "hello-notify",
            "dedupe_key": "n1",
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    message_id = sent["data"]["message"]["message_id"]
    mailbox_path = None
    mailbox_keys = None
    for path in tmp_path.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("store_kind") == "team_mailbox":
            mailbox_path = path
            mailbox_keys = set(data)
            break
    assert mailbox_path is not None
    code, marked = _exec(
        tmp_path,
        "mailbox-mark-notified",
        {"worker": "worker-1", "message_id": message_id},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, marked
    assert marked["data"]["updated"] is True
    after = json.loads(mailbox_path.read_text(encoding="utf-8"))
    assert set(after) == mailbox_keys
    assert "notify_cursor" not in after
    code, missing = _exec(
        tmp_path,
        "mailbox-mark-notified",
        {"worker": "worker-1", "message_id": "msg-does-not-exist"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert missing["ok"] is False
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, denied = execute_team_api(
        "mailbox-mark-notified",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "worker": "other-worker",
            "message_id": message_id,
        },
        root=tmp_path,
    )
    assert code == 2
    assert denied["error"]["code"] == "E_TEAM_API_GATE"


def test_write_worker_identity_leader_only_and_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _register_worker(tmp_path, run_id)
    code, env = _exec(
        tmp_path,
        "write-worker-identity",
        {
            "worker": "worker-1",
            "role": "executor",
            "generation": 1,
            "attributes": {"token": "super-secret", "pane": "p1"},
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, env
    identity = env["data"]["identity"]
    assert identity["worker_id"] == "worker-1"
    assert identity["attributes"]["token"] == "[REDACTED]"
    assert identity["attributes"]["pane"] == "p1"
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, denied = execute_team_api(
        "write-worker-identity",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "worker": "worker-1",
            "role": "executor",
        },
        root=tmp_path,
    )
    assert code == 2
    assert denied["error"]["code"] == "E_TEAM_API_GATE"
    code, other = execute_team_api(
        "write-worker-identity",
        {
            "run_id": run_id,
            "team_id": TEAM,
            "worker": "other-worker",
            "role": "executor",
        },
        root=tmp_path,
    )
    assert other["error"]["code"] == "E_TEAM_API_GATE"


def test_await_event_snapshot_kind_filter_and_unknown_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team.api import AWAIT_EVENT_TIMEOUT_CAP_MS, _bounded_timeout_ms

    run_id = _seed_control_plane(tmp_path, monkeypatch)
    code, appended = _exec(
        tmp_path,
        "append-event",
        {"kind": "tick", "body": {"n": 1}, "worker": "leader"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, appended
    event_id = appended["data"]["event"]["event_id"]
    code, awaited = _exec(
        tmp_path,
        "await-event",
        {"timeout_ms": 0, "kind": "tick"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert awaited["data"]["matched"] is True
    assert awaited["data"]["count"] == 1
    code, missed = _exec(
        tmp_path,
        "await-event",
        {"timeout_ms": 0, "kind": "other"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert missed["data"]["matched"] is False
    code, after = _exec(
        tmp_path,
        "await-event",
        {"timeout_ms": 0, "after": event_id},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert after["data"]["count"] == 0
    code, bad = _exec(
        tmp_path,
        "await-event",
        {"timeout_ms": -1},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 2
    assert bad["error"]["code"] == "E_TEAM_API_INVALID_INPUT"
    assert _bounded_timeout_ms({"timeout_ms": 5000}) == AWAIT_EVENT_TIMEOUT_CAP_MS
    assert AWAIT_EVENT_TIMEOUT_CAP_MS == 1000
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, worker_ok = execute_team_api(
        "await-event",
        {"run_id": run_id, "team_id": TEAM, "timeout_ms": 0, "kind": "tick"},
        root=tmp_path,
    )
    assert code == 0
    assert worker_ok["data"]["matched"] is True


def test_idle_and_stall_from_heartbeat_and_task_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    from omg_cli.team import api as team_api_mod
    from omg_cli.team.liveness import initialize_liveness, record_heartbeat

    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _register_worker(tmp_path, run_id)
    code, idle = _exec(
        tmp_path, "read-idle-state", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0, idle
    assert idle["data"]["tmux_probed"] is False
    assert idle["data"]["idle"] is True
    assert "worker-1" in idle["data"]["idle_workers"]
    code, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, claimed
    code, busy = _exec(
        tmp_path, "read-idle-state", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0
    assert busy["data"]["idle"] is False
    assert "worker-1" in busy["data"]["busy_workers"]
    future = datetime.now(timezone.utc) + timedelta(minutes=16)
    monkeypatch.setattr(team_api_mod, "_now_utc", lambda: future)
    code, stalled = _exec(
        tmp_path, "read-stall-state", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0, stalled
    assert stalled["data"]["stalled"] is True
    assert stalled["data"]["tmux_probed"] is False
    assert "worker-1" in stalled["data"]["stalled_workers"]
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    initialize_liveness(
        tmp_path,
        run_id=run_id,
        team_id=TEAM,
        task_id="worker-1",
        worker_id="worker-1",
        generation=0,
        now=past,
        claim_lease_seconds=1,
    )
    record_heartbeat(
        tmp_path,
        run_id=run_id,
        team_id=TEAM,
        task_id="worker-1",
        worker_id="worker-1",
        generation=0,
        expected_sequence=0,
        now=datetime.now(timezone.utc),
    )
    code, live_stall = _exec(
        tmp_path, "read-stall-state", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0
    assert live_stall["data"]["stalled"] is True


def test_cleanup_requires_shutdown_ack_and_refuses_running_or_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    run_id = _seed_control_plane(tmp_path, monkeypatch)
    code, too_soon = _exec(
        tmp_path, "cleanup", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 1
    assert too_soon["error"]["details"]["error"] == "E_TEAM_CLEANUP_NO_SHUTDOWN"
    code, req = _exec(
        tmp_path,
        "write-shutdown-request",
        {"force": True},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, req
    code, missing_ack = _exec(
        tmp_path, "cleanup", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 1
    assert missing_ack["error"]["details"]["error"] == "E_TEAM_CLEANUP_ACK_MISSING"
    code, ack = _exec(
        tmp_path,
        "write-shutdown-ack",
        {"worker": "t-a"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, ack
    meta_path = tmp_path / ".omg" / "state" / "runs" / run_id / "team" / "team.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["tasks"][0]["pid"] = os.getpid()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    meta_path.chmod(0o600)
    code, running = _exec(
        tmp_path, "cleanup", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 1
    assert running["error"]["details"]["error"] == "E_TEAM_CLEANUP_RUNNING"
    meta["tasks"][0]["pid"] = None
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    meta_path.chmod(0o600)
    _register_worker(tmp_path, run_id)
    code, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, claimed
    code, ack_w = _exec(
        tmp_path,
        "write-shutdown-ack",
        {"worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, ack_w
    code, claims = _exec(
        tmp_path, "cleanup", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 1
    assert claims["error"]["details"]["error"] == "E_TEAM_CLEANUP_CLAIMS"
    code, released = _exec(
        tmp_path,
        "release-task-claim",
        {
            "task_id": "1",
            "worker": "worker-1",
            "claim_token": claimed["data"]["claimToken"],
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, released
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, denied = execute_team_api(
        "cleanup",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
    )
    assert code == 2
    assert denied["error"]["code"] == "E_TEAM_API_GATE"
    for key in (
        "OMG_TEAM_WORKER",
        "OMG_TEAM_WORKER_ID",
        "OMG_TEAM_RUN_ID",
        "OMG_TEAM_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")
    code, cleaned = _exec(
        tmp_path, "cleanup", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0, cleaned
    assert cleaned["data"]["never_sets_verified"] is True
    assert cleaned["data"]["distinct_from"] == "orphan-cleanup"
    code, listed = _exec(
        tmp_path, "list-tasks", {}, run_id=run_id, monkeypatch=monkeypatch
    )
    assert code == 0
    assert listed["data"]["count"] == 0
    assert meta_path.is_file()
    assert not list(tmp_path.rglob("verified.json"))


def test_monitor_snapshot_redacted_and_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _register_worker(tmp_path, run_id)
    code, missing = _exec(
        tmp_path,
        "read-monitor-snapshot",
        {},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert missing["data"]["present"] is False
    code, written = _exec(
        tmp_path,
        "write-monitor-snapshot",
        {"snapshot": {"token": "hidden", "ok": True}},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, written
    snap = written["data"]["snapshot"]
    assert snap["redacted"] is True
    assert snap["tmux_probed"] is False
    assert snap["snapshot"]["token"] == "[REDACTED]"
    assert snap["snapshot"]["ok"] is True
    code, derived = _exec(
        tmp_path,
        "write-monitor-snapshot",
        {},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, derived
    body = derived["data"]["snapshot"]["snapshot"]
    assert "token" not in json.dumps(body)
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, worker_read = execute_team_api(
        "read-monitor-snapshot",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
    )
    assert code == 0
    assert worker_read["data"]["present"] is True
    code, worker_write = execute_team_api(
        "write-monitor-snapshot",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
    )
    assert code == 2
    assert worker_write["error"]["code"] == "E_TEAM_API_GATE"


def test_task_approval_terminal_override_and_never_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _register_worker(tmp_path, run_id)
    code, missing = _exec(
        tmp_path,
        "read-task-approval",
        {"task_id": "1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert missing["data"]["present"] is False
    code, written = _exec(
        tmp_path,
        "write-task-approval",
        {
            "task_id": "1",
            "decision": "approved",
            "note": "ok token=secret",
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, written
    assert written["data"]["never_sets_verified"] is True
    assert written["data"]["approval"]["never_sets_verified"] is True
    code, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": "1", "worker": "worker-1"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, claimed
    code, done = _exec(
        tmp_path,
        "transition-task-status",
        {
            "task_id": "1",
            "worker": "worker-1",
            "claim_token": claimed["data"]["claimToken"],
            "from": "in_progress",
            "to": "completed",
            "result": "done",
        },
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, done
    code, refused = _exec(
        tmp_path,
        "write-task-approval",
        {"task_id": "1", "decision": "approved"},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert refused["error"]["details"]["error"] == "approval_error"
    code, forced = _exec(
        tmp_path,
        "write-task-approval",
        {"task_id": "1", "decision": "approved", "allow_terminal": True},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    assert code == 0, forced
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, worker_read = execute_team_api(
        "read-task-approval",
        {"run_id": run_id, "team_id": TEAM, "task_id": "1"},
        root=tmp_path,
    )
    assert code == 0
    assert worker_read["data"]["present"] is True
    code, worker_write = execute_team_api(
        "write-task-approval",
        {"run_id": run_id, "team_id": TEAM, "task_id": "1", "decision": "rejected"},
        root=tmp_path,
    )
    assert code == 2
    assert worker_write["error"]["code"] == "E_TEAM_API_GATE"
    assert not list(tmp_path.rglob("verified.json"))
    status_path = tmp_path / ".omg" / "state" / "runs" / run_id / "status.json"
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert status.get("verified") is not True
        assert not status.get("passes")
