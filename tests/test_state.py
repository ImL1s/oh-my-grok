# tests/test_state.py
from pathlib import Path

from omg_cli.state import (
    cancel_run,
    create_run,
    load_active_run,
    write_status,
)


def test_create_run_atomic(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="test")
    assert run["status"] == "initialized"
    assert (tmp_path / ".omg" / "state" / "runs" / run["run_id"]).is_dir()
    active = load_active_run(tmp_path)
    assert active["run_id"] == run["run_id"]
    write_status(tmp_path, run["run_id"], "running")
    assert load_active_run(tmp_path)["status"] == "running"


def test_status_json_atomic_and_fields(tmp_path):
    run = create_run(tmp_path, mode="ulw", goal="ship it")
    status_path = tmp_path / ".omg" / "state" / "runs" / run["run_id"] / "status.json"
    assert status_path.is_file()
    assert run["mode"] == "ulw"
    assert run["goal"] == "ship it"
    assert run["verified"] is False
    assert "run_id" in run
    # no leftover temp files from atomic write
    run_dir = status_path.parent
    temps = list(run_dir.glob("*.tmp")) + list(run_dir.glob(".*.tmp"))
    assert temps == []


def test_load_active_run_none_when_missing(tmp_path):
    assert load_active_run(tmp_path) is None


def test_cancel_run_clears_active(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="cancel me")
    rid = run["run_id"]
    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    assert cancelled["verified"] is False
    assert load_active_run(tmp_path) is None
    # status file still on disk for post-mortem
    status_path = tmp_path / ".omg" / "state" / "runs" / rid / "status.json"
    assert status_path.is_file()


def test_cancel_active_without_run_id(tmp_path):
    run = create_run(tmp_path, mode="ralplan", goal="x")
    cancelled = cancel_run(tmp_path)
    assert cancelled["run_id"] == run["run_id"]
    assert cancelled["status"] == "cancelled"
    assert load_active_run(tmp_path) is None


def test_write_status_does_not_set_verified(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="v")
    updated = write_status(tmp_path, run["run_id"], "running", extra={"note": "ok"})
    assert updated["status"] == "running"
    assert updated["verified"] is False
    assert updated["note"] == "ok"
