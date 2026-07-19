# tests/test_state.py
import json
import os
import signal

import pytest

from omg_cli.state import (
    cancel_run,
    create_run,
    is_stale_run,
    load_active_run,
    load_run,
    set_verified,
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


def test_create_run_mutex_blocks_active_non_terminal(tmp_path):
    first = create_run(tmp_path, mode="ralph", goal="first")
    assert first["status"] == "initialized"
    with pytest.raises(RuntimeError, match="active run already exists"):
        create_run(tmp_path, mode="ulw", goal="second")
    # still the first active
    active = load_active_run(tmp_path)
    assert active is not None
    assert active["run_id"] == first["run_id"]


def test_create_run_mutex_allows_after_terminal(tmp_path):
    first = create_run(tmp_path, mode="ralph", goal="done-ish")
    write_status(tmp_path, first["run_id"], "completed")
    second = create_run(tmp_path, mode="ulw", goal="next")
    assert second["run_id"] != first["run_id"]
    assert load_active_run(tmp_path)["run_id"] == second["run_id"]


def test_create_run_mutex_force_overrides(tmp_path):
    """force=True supersedes: cancel/kill old active run before new create."""
    first = create_run(tmp_path, mode="ralph", goal="a")
    write_status(tmp_path, first["run_id"], "running")
    second = create_run(tmp_path, mode="ulw", goal="b", force=True)
    assert second["run_id"] != first["run_id"]
    assert load_active_run(tmp_path)["run_id"] == second["run_id"]
    # Old run must be cancelled (superseded), not left as running
    old = load_run(tmp_path, first["run_id"])
    assert old is not None
    assert old["status"] == "cancelled"


def test_create_run_force_kills_old_pid(tmp_path, monkeypatch):
    """force supersede best-effort kills old run process group via pid file."""
    first = create_run(tmp_path, mode="ralph", goal="a")
    write_status(tmp_path, first["run_id"], "running")
    pid_path = tmp_path / ".omg" / "state" / "runs" / first["run_id"] / "pid"
    pid_path.write_text("777001\n", encoding="utf-8")

    killpgs: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))
        # pretend success

    def fake_kill(pid, sig):
        # signal 0 existence check: report dead so stale path is exercised;
        # force supersede still cancels and uses killpg for SIGTERM
        if sig == 0:
            raise ProcessLookupError(f"no process {pid}")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)

    second = create_run(tmp_path, mode="ulw", goal="b", force=True)
    assert second["run_id"] != first["run_id"]
    assert load_run(tmp_path, first["run_id"])["status"] == "cancelled"
    assert any(pg == 777001 and sig == signal.SIGTERM for pg, sig in killpgs)


def test_create_run_allows_when_stale_pid_esrch(tmp_path, monkeypatch):
    """Active non-terminal run with dead pid (ESRCH) may be superseded without force."""
    first = create_run(tmp_path, mode="ralph", goal="stale")
    write_status(tmp_path, first["run_id"], "running")
    pid_path = tmp_path / ".omg" / "state" / "runs" / first["run_id"] / "pid"
    pid_path.write_text("888002\n", encoding="utf-8")

    def fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError(f"no process {pid}")
        raise ProcessLookupError(f"no process {pid}")

    def fake_killpg(pgid, sig):
        raise ProcessLookupError(f"no pg {pgid}")

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(os, "killpg", fake_killpg)

    assert is_stale_run(tmp_path, first["run_id"]) is True
    second = create_run(tmp_path, mode="ulw", goal="next")  # no force
    assert second["run_id"] != first["run_id"]
    assert load_active_run(tmp_path)["run_id"] == second["run_id"]
    assert load_run(tmp_path, first["run_id"])["status"] == "cancelled"


def test_create_run_mutex_blocks_verifying(tmp_path):
    first = create_run(tmp_path, mode="ralph", goal="v")
    write_status(tmp_path, first["run_id"], "verifying")
    with pytest.raises(RuntimeError, match="verifying"):
        create_run(tmp_path, mode="ulw", goal="nope")


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


def test_cancel_run_sigterms_pid_best_effort(tmp_path, monkeypatch):
    """cancel_run prefers killpg(SIGTERM); ProcessLookupError is ignored."""
    run = create_run(tmp_path, mode="ulw", goal="kill me")
    rid = run["run_id"]
    pid_path = tmp_path / ".omg" / "state" / "runs" / rid / "pid"
    pid_path.write_text("999999\n", encoding="utf-8")

    killpgs: list[tuple[int, int]] = []
    kills: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))
        raise ProcessLookupError(f"no process group {pgid}")

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        raise ProcessLookupError(f"no process {pid}")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    # killpg tried first; fallback kill after killpg fails
    assert killpgs == [(999999, signal.SIGTERM)]
    assert kills == [(999999, signal.SIGTERM)]


def test_cancel_run_killpg_success_skips_single_kill(tmp_path, monkeypatch):
    run = create_run(tmp_path, mode="ulw", goal="pg")
    rid = run["run_id"]
    pid_path = tmp_path / ".omg" / "state" / "runs" / rid / "pid"
    pid_path.write_text("424242\n", encoding="utf-8")

    killpgs: list[tuple[int, int]] = []
    kills: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))
        # success — no raise

    def fake_kill(pid, sig):
        kills.append((pid, sig))

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    assert killpgs == [(424242, signal.SIGTERM)]
    assert kills == []
    assert cancelled.get("kill_actions") == ["killpg:SIGTERM"]


def test_write_status(tmp_path):
    """write_status: reserved keys protected; verified only via set_verified + acceptance."""
    run = create_run(tmp_path, mode="ralph", goal="v")
    rid = run["run_id"]
    created_at = run["created_at"]

    # Normal extra fields are allowed
    updated = write_status(tmp_path, rid, "running", extra={"note": "ok"})
    assert updated["status"] == "running"
    assert updated["verified"] is False
    assert updated["note"] == "ok"

    # extra={"verified": True} must stay False (reserved; use set_verified)
    hijack_v = write_status(tmp_path, rid, "running", extra={"verified": True})
    assert hijack_v["verified"] is False

    # extra={"status": "verified"} cannot hijack the status parameter
    hijack_s = write_status(tmp_path, rid, "running", extra={"status": "verified"})
    assert hijack_s["status"] == "running"
    assert hijack_s["verified"] is False

    # run_id / created_at cannot be rewritten via extra
    hijack_id = write_status(
        tmp_path,
        rid,
        "running",
        extra={"run_id": "evil-id", "created_at": "1970-01-01T00:00:00+00:00"},
    )
    assert hijack_id["run_id"] == rid
    assert hijack_id["created_at"] == created_at

    # set_verified without CLI acceptance result raises
    with pytest.raises(PermissionError, match="acceptance"):
        set_verified(tmp_path, rid)

    # forged {passed:true} without writer stamp is rejected
    accept_path = tmp_path / ".omg" / "state" / "runs" / rid / "acceptance.json"
    accept_path.write_text(
        json.dumps({"passed": True}),
        encoding="utf-8",
    )
    with pytest.raises(PermissionError, match="acceptance"):
        set_verified(tmp_path, rid)

    # real CLI freeze+run stamps writer + sha → set_verified ok
    from omg_cli.acceptance import freeze_and_run

    prd = {
        "version": 1,
        "goal": "v",
        "stories": [
            {"id": "s1", "title": "ok", "commands": [["true"]]}
        ],
        "global_commands": [],
    }
    assert freeze_and_run(tmp_path, rid, prd) is True
    verified = set_verified(tmp_path, rid)
    assert verified["verified"] is True
    assert verified["status"] == "verified"
