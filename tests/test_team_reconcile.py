"""Hermetic leader-resume task-claim reconciliation (#69 PR3)."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import omg_cli.team.api as team_api
from omg_cli.contracts.path_keys import exclusive_lock
from omg_cli.contracts.writer_chain import canonical_json_bytes, parse_canonical_json_bytes
from omg_cli.team.api import (
    TeamApiError,
    _task_path,
    _write_task,
    execute_team_api,
    reconcile_task_claims,
)
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    WORKER_ENV_MARKERS,
    start_team,
)


TEAM = "team"
SEED_TASKS = [{"task_id": "t-a", "owned_files": ["a.py"]}]
FIXED_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


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


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "reconcile seed",
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
) -> tuple[int, dict]:
    body = {"run_id": run_id, "team_id": TEAM, **payload}
    return execute_team_api(op, body, root=root)


def _create_and_claim(
    root: Path,
    run_id: str,
    *,
    subject: str = "task",
    worker: str = "worker-1",
) -> tuple[str, dict]:
    code, created = _exec(
        root,
        "create-task",
        {
            "subject": subject,
            "description": subject,
            "workers": ["worker-1", "worker-2"],
        },
        run_id=run_id,
    )
    assert code == 0
    task_id = str(created["data"]["task"]["id"])
    code, claimed = _exec(
        root,
        "claim-task",
        {"task_id": task_id, "worker": worker},
        run_id=run_id,
    )
    assert code == 0
    return claimed["data"]["claimToken"], claimed["data"]["task"]


def _task_bytes(root: Path, run_id: str, task_id: str) -> bytes:
    return _task_path(root, run_id, TEAM, task_id).read_bytes()


def _read_task_row(root: Path, run_id: str, task_id: str) -> dict:
    return parse_canonical_json_bytes(_task_bytes(root, run_id, task_id))


def _write_task_row(root: Path, run_id: str, task_id: str, row: dict) -> None:
    _task_path(root, run_id, TEAM, task_id).write_bytes(canonical_json_bytes(row))


def _expire_claim(root: Path, run_id: str, task_id: str, *, at: datetime) -> None:
    row = _read_task_row(root, run_id, task_id)
    claim = dict(row["claim"])
    claim["leased_until"] = at.isoformat().replace("+00:00", "Z")
    row["claim"] = claim
    _write_task_row(root, run_id, task_id, row)


def test_leader_reconcile_preserves_unexpired_claim_bytes_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _token, task = _create_and_claim(tmp_path, run_id)
    before = _task_bytes(tmp_path, run_id, task["id"])
    version = task["version"]

    out = reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert out["status"] == "ok"
    assert out["preserved_unexpired"] == [task["id"]]
    assert out["released_expired"] == []
    assert _task_bytes(tmp_path, run_id, task["id"]) == before
    after = _read_task_row(tmp_path, run_id, task["id"])
    assert after["version"] == version
    assert after["claim"]["token"] == task["claim"]["token"]


def test_leader_reconcile_releases_expired_claim_once_and_fences_old_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    token, task = _create_and_claim(tmp_path, run_id)
    deadline = datetime.fromisoformat(
        task["claim"]["leased_until"].replace("Z", "+00:00")
    )
    version = task["version"]

    monkeypatch.setattr(team_api, "_now_utc", lambda: deadline)
    out = reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert out["released_expired"] == [task["id"]]
    disk = _read_task_row(tmp_path, run_id, task["id"])
    assert disk["status"] == "pending"
    assert disk["owner"] is None
    assert disk["claim"] is None
    assert disk["version"] == version + 1

    out2 = reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert out2["unchanged"] == [task["id"]]
    assert _read_task_row(tmp_path, run_id, task["id"])["version"] == version + 1

    code, renew = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": task["id"], "worker": "worker-1", "claim_token": token},
        run_id=run_id,
    )
    assert code == 1
    assert renew["error"]["details"]["error"] in {
        "claim_conflict",
        "lease_expired",
    }

    code, _done = _exec(
        tmp_path,
        "transition-task-status",
        {
            "task_id": task["id"],
            "from": "in_progress",
            "to": "completed",
            "worker": "worker-1",
            "claim_token": token,
            "result": "stale",
        },
        run_id=run_id,
    )
    assert code != 0


def test_leader_reconcile_malformed_deadline_is_expired_not_immortal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _token, task = _create_and_claim(tmp_path, run_id)
    row = _read_task_row(tmp_path, run_id, task["id"])
    claim = dict(row["claim"])
    claim["leased_until"] = "not-a-timestamp"
    row["claim"] = claim
    _write_task_row(tmp_path, run_id, task["id"], row)

    out = reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert out["released_expired"] == [task["id"]]
    disk = _read_task_row(tmp_path, run_id, task["id"])
    assert disk["status"] == "pending"
    assert disk["claim"] is None


def test_leader_reconcile_refuses_owner_claim_mismatch_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _t1, task1 = _create_and_claim(tmp_path, run_id, subject="one")
    _t2, task2 = _create_and_claim(tmp_path, run_id, subject="two")
    before1 = _task_bytes(tmp_path, run_id, task1["id"])

    row = _read_task_row(tmp_path, run_id, task2["id"])
    claim = dict(row["claim"])
    claim["owner"] = "worker-2"
    row["claim"] = claim
    _write_task_row(tmp_path, run_id, task2["id"], row)
    before2 = _task_bytes(tmp_path, run_id, task2["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert excinfo.value.details.get("task_id") == task2["id"]
    assert "owner_claim_mismatch" in str(excinfo.value)
    assert _task_bytes(tmp_path, run_id, task1["id"]) == before1
    assert _task_bytes(tmp_path, run_id, task2["id"]) == before2


def test_leader_reconcile_refuses_claim_on_terminal_task_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    token, task = _create_and_claim(tmp_path, run_id)
    code, _ = _exec(
        tmp_path,
        "transition-task-status",
        {
            "task_id": task["id"],
            "from": "in_progress",
            "to": "completed",
            "worker": "worker-1",
            "claim_token": token,
            "result": "done",
        },
        run_id=run_id,
    )
    assert code == 0
    row = _read_task_row(tmp_path, run_id, task["id"])
    row["claim"] = {
        "owner": "worker-1",
        "token": "stale",
        "leased_until": (FIXED_NOW + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    _write_task_row(tmp_path, run_id, task["id"], row)
    before = _task_bytes(tmp_path, run_id, task["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert "completed_has_claim" in str(excinfo.value)
    assert _task_bytes(tmp_path, run_id, task["id"]) == before


def test_leader_reconcile_missing_api_store_is_non_materializing_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_on(monkeypatch)
    out = reconcile_task_claims(tmp_path, run_id="run-x", team_id=TEAM)
    assert out == {
        "status": "not_materialized",
        "scanned": 0,
        "preserved_unexpired": [],
        "released_expired": [],
        "unchanged": [],
    }
    assert not (tmp_path / ".omg").exists()


def test_leader_reconcile_crash_before_first_commit_leaves_all_claims_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _t1, task1 = _create_and_claim(tmp_path, run_id, subject="a")
    _t2, task2 = _create_and_claim(tmp_path, run_id, subject="b")
    past = FIXED_NOW - timedelta(seconds=1)
    _expire_claim(tmp_path, run_id, task1["id"], at=past)
    _expire_claim(tmp_path, run_id, task2["id"], at=past)
    before = {
        tid: _task_bytes(tmp_path, run_id, tid)
        for tid in (task1["id"], task2["id"])
    }

    def crash_before_write(*_args: object, **_kwargs: object) -> dict:
        raise SystemExit("simulated leader crash before atomic task commit")

    monkeypatch.setattr(team_api, "_write_task", crash_before_write)
    with pytest.raises(SystemExit, match="before atomic task commit"):
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)

    for tid, blob in before.items():
        assert _task_bytes(tmp_path, run_id, tid) == blob


def test_leader_reconcile_crash_after_one_atomic_commit_resumes_without_double_version_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _t1, task1 = _create_and_claim(tmp_path, run_id, subject="a")
    _t2, task2 = _create_and_claim(tmp_path, run_id, subject="b")
    past = FIXED_NOW - timedelta(seconds=1)
    _expire_claim(tmp_path, run_id, task1["id"], at=past)
    _expire_claim(tmp_path, run_id, task2["id"], at=past)
    v1 = _read_task_row(tmp_path, run_id, task1["id"])["version"]
    v2 = _read_task_row(tmp_path, run_id, task2["id"])["version"]

    real_write = team_api._write_task
    writes = {"n": 0}

    def crash_after_first(*args: object, **kwargs: object) -> dict:
        writes["n"] += 1
        updated = real_write(*args, **kwargs)
        if writes["n"] == 1:
            raise SystemExit("simulated leader crash after atomic task commit")
        return updated

    monkeypatch.setattr(team_api, "_write_task", crash_after_first)
    with pytest.raises(SystemExit, match="after atomic task commit"):
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)

    disk1 = _read_task_row(tmp_path, run_id, task1["id"])
    disk2 = _read_task_row(tmp_path, run_id, task2["id"])
    assert disk1["status"] == "pending"
    assert disk1["version"] == v1 + 1
    assert disk2["status"] == "in_progress"
    assert disk2["version"] == v2

    monkeypatch.setattr(team_api, "_write_task", real_write)
    out = reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert out["released_expired"] == [task2["id"]]
    assert out["unchanged"] == [task1["id"]]
    assert _read_task_row(tmp_path, run_id, task1["id"])["version"] == v1 + 1
    assert _read_task_row(tmp_path, run_id, task2["id"])["version"] == v2 + 1


def test_leader_reconcile_crash_after_last_commit_is_idempotent_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _token, task = _create_and_claim(tmp_path, run_id)
    past = FIXED_NOW - timedelta(seconds=1)
    _expire_claim(tmp_path, run_id, task["id"], at=past)
    version = _read_task_row(tmp_path, run_id, task["id"])["version"]

    real_write = team_api._write_task

    def crash_after_commit(*args: object, **kwargs: object) -> dict:
        real_write(*args, **kwargs)
        raise SystemExit("simulated leader crash after last task commit")

    monkeypatch.setattr(team_api, "_write_task", crash_after_commit)
    with pytest.raises(SystemExit, match="after last task commit"):
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)

    disk = _read_task_row(tmp_path, run_id, task["id"])
    assert disk["status"] == "pending"
    assert disk["version"] == version + 1

    monkeypatch.setattr(team_api, "_write_task", real_write)
    out = reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert out["unchanged"] == [task["id"]]
    assert _read_task_row(tmp_path, run_id, task["id"])["version"] == version + 1


def test_leader_reconcile_refuses_non_string_owner_and_token_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _t1, task1 = _create_and_claim(tmp_path, run_id, subject="clean")
    _t2, task2 = _create_and_claim(tmp_path, run_id, subject="poison")
    before1 = _task_bytes(tmp_path, run_id, task1["id"])

    row = _read_task_row(tmp_path, run_id, task2["id"])
    row["owner"] = 7
    row["claim"] = {
        "owner": 7,
        "token": False,
        "leased_until": (FIXED_NOW + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    _write_task_row(tmp_path, run_id, task2["id"], row)
    before2 = _task_bytes(tmp_path, run_id, task2["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert excinfo.value.details.get("error") == "corrupt_claim"
    assert excinfo.value.details.get("invariant") == "non_string_owner"
    assert _task_bytes(tmp_path, run_id, task1["id"]) == before1
    assert _task_bytes(tmp_path, run_id, task2["id"]) == before2


def test_leader_reconcile_refuses_unsafe_owner_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _token, task = _create_and_claim(tmp_path, run_id)
    row = _read_task_row(tmp_path, run_id, task["id"])
    claim = dict(row["claim"])
    claim["owner"] = "../worker"
    row["owner"] = "../worker"
    row["claim"] = claim
    _write_task_row(tmp_path, run_id, task["id"], row)
    before = _task_bytes(tmp_path, run_id, task["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert excinfo.value.details.get("invariant") == "unsafe_owner"
    assert _task_bytes(tmp_path, run_id, task["id"]) == before


def test_leader_reconcile_refuses_non_string_token_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _token, task = _create_and_claim(tmp_path, run_id)
    row = _read_task_row(tmp_path, run_id, task["id"])
    claim = dict(row["claim"])
    claim["token"] = False
    row["claim"] = claim
    _write_task_row(tmp_path, run_id, task["id"], row)
    before = _task_bytes(tmp_path, run_id, task["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert excinfo.value.details.get("invariant") == "non_string_token"
    assert _task_bytes(tmp_path, run_id, task["id"]) == before


def test_leader_reconcile_refuses_padded_token_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _token, task = _create_and_claim(tmp_path, run_id)
    row = _read_task_row(tmp_path, run_id, task["id"])
    claim = dict(row["claim"])
    claim["token"] = f" {_token} "
    row["claim"] = claim
    _write_task_row(tmp_path, run_id, task["id"], row)
    before = _task_bytes(tmp_path, run_id, task["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert excinfo.value.details.get("error") == "corrupt_claim"
    assert excinfo.value.details.get("invariant") == "non_string_token"
    assert _task_bytes(tmp_path, run_id, task["id"]) == before


def test_leader_reconcile_refuses_whitespace_only_token_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _token, task = _create_and_claim(tmp_path, run_id)
    row = _read_task_row(tmp_path, run_id, task["id"])
    claim = dict(row["claim"])
    claim["token"] = "   "
    row["claim"] = claim
    _write_task_row(tmp_path, run_id, task["id"], row)
    before = _task_bytes(tmp_path, run_id, task["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert excinfo.value.details.get("error") == "corrupt_claim"
    assert excinfo.value.details.get("invariant") == "non_string_token"
    assert _task_bytes(tmp_path, run_id, task["id"]) == before


def test_leader_reconcile_refuses_filename_body_id_mismatch_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _t1, task1 = _create_and_claim(tmp_path, run_id, subject="ok")
    _t2, task2 = _create_and_claim(tmp_path, run_id, subject="mismatch")
    before1 = _task_bytes(tmp_path, run_id, task1["id"])

    row = _read_task_row(tmp_path, run_id, task2["id"])
    row["id"] = "999"
    _write_task_row(tmp_path, run_id, task2["id"], row)
    before2 = _task_bytes(tmp_path, run_id, task2["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert excinfo.value.details.get("invariant") == "filename_body_id_mismatch"
    assert _task_bytes(tmp_path, run_id, task1["id"]) == before1
    assert _task_bytes(tmp_path, run_id, task2["id"]) == before2


def test_leader_reconcile_refuses_duplicate_embedded_ids_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _t1, task1 = _create_and_claim(tmp_path, run_id, subject="a")
    _t2, task2 = _create_and_claim(tmp_path, run_id, subject="b")
    before1 = _task_bytes(tmp_path, run_id, task1["id"])
    before2 = _task_bytes(tmp_path, run_id, task2["id"])

    # Both files embed the same id (task1's); duplicate check runs before stem bind.
    row2 = _read_task_row(tmp_path, run_id, task2["id"])
    row2["id"] = task1["id"]
    _write_task_row(tmp_path, run_id, task2["id"], row2)
    after_poison2 = _task_bytes(tmp_path, run_id, task2["id"])

    with pytest.raises(TeamApiError) as excinfo:
        reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert excinfo.value.details.get("invariant") == "duplicate_task_id"
    assert _task_bytes(tmp_path, run_id, task1["id"]) == before1
    assert _task_bytes(tmp_path, run_id, task2["id"]) == after_poison2
    assert after_poison2 != before2


def test_leader_reconcile_renew_wins_task_lock_and_preserves_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    token, task = _create_and_claim(tmp_path, run_id)
    path = _task_path(tmp_path, run_id, TEAM, task["id"])
    _expire_claim(
        tmp_path,
        run_id,
        task["id"],
        at=FIXED_NOW - timedelta(seconds=30),
    )

    lock_path = path.with_suffix(".lock")
    waiting_for_lock = threading.Event()
    result: dict[str, object] = {}
    error: list[BaseException] = []
    real_lock = team_api.exclusive_lock

    def gated_lock(target: Path | str, *args: object, **kwargs: object):
        if Path(target) == lock_path:
            waiting_for_lock.set()
        return real_lock(target, *args, **kwargs)

    monkeypatch.setattr(team_api, "exclusive_lock", gated_lock)

    def run_reconcile() -> None:
        try:
            result.update(
                reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
            )
        except BaseException as exc:  # noqa: BLE001 — capture for main thread
            error.append(exc)

    # Hold via path_keys (unpatched) so only reconcile's acquire is gated.
    holder = exclusive_lock(lock_path)
    holder.__enter__()
    thread = threading.Thread(target=run_reconcile)
    thread.start()
    assert waiting_for_lock.wait(5)

    monkeypatch.setattr(
        team_api,
        "_now_utc",
        lambda: FIXED_NOW + timedelta(seconds=1),
    )
    renewed_deadline = FIXED_NOW + timedelta(seconds=team_api.CLAIM_LEASE_SECONDS)
    current = _read_task_row(tmp_path, run_id, task["id"])
    claim = dict(current["claim"])
    claim["leased_until"] = renewed_deadline.isoformat().replace("+00:00", "Z")
    current["claim"] = claim
    current["version"] = int(current["version"]) + 1
    _write_task(tmp_path, run_id, TEAM, current)
    before = path.read_bytes()

    holder.__exit__(None, None, None)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert error == []
    assert result.get("preserved_unexpired") == [task["id"]]
    assert path.read_bytes() == before
    assert token == json.loads(before.decode("utf-8"))["claim"]["token"]


def test_leader_reconcile_public_reclaim_wins_task_lock_against_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public claim-task reclaim (new owner/token) wins over resume release."""
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _old_token, task = _create_and_claim(tmp_path, run_id, worker="worker-1")
    path = _task_path(tmp_path, run_id, TEAM, task["id"])
    lock_path = path.with_suffix(".lock")
    _expire_claim(
        tmp_path,
        run_id,
        task["id"],
        at=FIXED_NOW - timedelta(seconds=30),
    )

    claim_done = threading.Event()
    reconcile_waiting = threading.Event()
    result: dict[str, object] = {}
    error: list[BaseException] = []
    real_lock = team_api.exclusive_lock
    recon_thread_box: dict[str, threading.Thread | None] = {"t": None}

    def gated_lock(target: Path | str, *args: object, **kwargs: object):
        if (
            Path(target) == lock_path
            and threading.current_thread() is recon_thread_box["t"]
        ):
            reconcile_waiting.set()
            assert claim_done.wait(5)
        return real_lock(target, *args, **kwargs)

    monkeypatch.setattr(team_api, "exclusive_lock", gated_lock)

    def run_reconcile() -> None:
        try:
            result.update(
                reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
            )
        except BaseException as exc:  # noqa: BLE001 — capture for main thread
            error.append(exc)

    recon_thread = threading.Thread(target=run_reconcile)
    recon_thread_box["t"] = recon_thread
    recon_thread.start()
    assert reconcile_waiting.wait(5)

    monkeypatch.setattr(
        team_api,
        "_now_utc",
        lambda: FIXED_NOW + timedelta(seconds=1),
    )
    code, claimed = _exec(
        tmp_path,
        "claim-task",
        {"task_id": task["id"], "worker": "worker-2"},
        run_id=run_id,
    )
    assert code == 0
    new_token = claimed["data"]["claimToken"]
    assert new_token
    assert claimed["data"]["task"]["owner"] == "worker-2"
    before = path.read_bytes()
    claim_done.set()

    recon_thread.join(timeout=5)
    assert not recon_thread.is_alive()
    assert error == []
    assert result.get("preserved_unexpired") == [task["id"]]
    assert result.get("released_expired") == []
    assert path.read_bytes() == before
    disk = _read_task_row(tmp_path, run_id, task["id"])
    assert disk["owner"] == "worker-2"
    assert disk["claim"]["owner"] == "worker-2"
    assert disk["claim"]["token"] == new_token


def test_leader_reconcile_release_wins_task_lock_and_late_renew_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    token, task = _create_and_claim(tmp_path, run_id)
    _expire_claim(
        tmp_path,
        run_id,
        task["id"],
        at=FIXED_NOW - timedelta(seconds=1),
    )

    out = reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert out["released_expired"] == [task["id"]]

    code, renew = _exec(
        tmp_path,
        "renew-task-claim",
        {"task_id": task["id"], "worker": "worker-1", "claim_token": token},
        run_id=run_id,
    )
    assert code == 1
    assert renew["error"]["details"]["error"] in {
        "claim_conflict",
        "lease_expired",
    }
    disk = _read_task_row(tmp_path, run_id, task["id"])
    assert disk["status"] == "pending"
    assert disk["claim"] is None


def test_leader_reconcile_uses_no_tmux_or_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_api, "_now_utc", lambda: FIXED_NOW)
    run_id = _seed(tmp_path, monkeypatch)
    _create_and_claim(tmp_path, run_id)

    def forbid_run(*_a: object, **_k: object) -> None:
        raise AssertionError("subprocess must not be invoked")

    monkeypatch.setattr("subprocess.run", forbid_run)
    monkeypatch.setattr("subprocess.Popen", forbid_run)

    out = reconcile_task_claims(tmp_path, run_id=run_id, team_id=TEAM)
    assert out["status"] == "ok"
    assert out["scanned"] == 1
