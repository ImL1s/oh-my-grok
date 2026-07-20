# tests/test_state.py
import json
import multiprocessing
import os
import signal
import threading

import pytest

from omg_cli.state import (
    ExecutionLeaseBusy,
    FencingError,
    LifecycleLockError,
    LockUnavailableError,
    RunSchema,
    cancel_run,
    classify_run_schema,
    create_run,
    execution_lease,
    is_stale_run,
    load_active_run,
    load_run,
    set_verified,
    transition_guard,
    transition_guard_held,
    write_status,
)


def _transition_crash_worker(
    root: str, run_id: str, conn, *, replace_before_pause: bool
) -> None:
    """Fork target for deterministic guard-owner-death coverage."""
    from pathlib import Path

    import omg_cli.state as state_mod

    root_path = Path(root)
    with state_mod.transition_guard(root_path, run_id):
        if replace_before_pause:
            status = state_mod.load_run(root_path, run_id)
            assert status is not None
            status["crash_probe"] = "committed"
            state_mod._atomic_write_json(
                state_mod._status_path(root_path, run_id), status
            )
        conn.send("guard-held")
        conn.recv()


def test_schema_classifier_exact_dispatch() -> None:
    assert classify_run_schema({}) is RunSchema.LEGACY_V1
    assert classify_run_schema({"schema_version": 1}) is RunSchema.LEGACY_V1
    assert classify_run_schema(
        {"schema_version": 2, "lifecycle_version": 2}
    ) is RunSchema.STRICT_V2


@pytest.mark.parametrize(
    "run",
    [
        {"schema_version": True},
        {"schema_version": None},
        {"schema_version": []},
        {"schema_version": {}},
        {"schema_version": -1},
        {"schema_version": 3},
        {"schema_version": 2},
        {"schema_version": 2, "lifecycle_version": True},
        {"schema_version": 1, "lifecycle_version": 2},
    ],
)
def test_schema_classifier_rejects_malformed_and_future(run: dict) -> None:
    with pytest.raises((TypeError, ValueError), match="schema|version|lifecycle"):
        classify_run_schema(run)


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
    """force supersede kills old run only when starttime matches (fail-closed)."""
    from omg_cli import state as state_mod

    first = create_run(tmp_path, mode="ralph", goal="a")
    write_status(tmp_path, first["run_id"], "running")
    start = "Sun Jul 19 12:00:00 2026"
    pid_json = tmp_path / ".omg" / "state" / "runs" / first["run_id"] / "pid.json"
    pid_json.write_text(
        json.dumps({"pid": 777001, "starttime": start, "pgid": 777001}) + "\n",
        encoding="utf-8",
    )

    killpgs: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))
        # pretend success

    def fake_kill(pid, sig):
        if sig == 0:
            return  # alive

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(state_mod, "process_starttime", lambda pid: start)

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
    """cancel_run prefers killpg(SIGTERM) when starttime matches; ESRCH ignored."""
    from omg_cli import state as state_mod

    run = create_run(tmp_path, mode="ulw", goal="kill me")
    rid = run["run_id"]
    start = "Mon Jul 19 10:00:00 2026"
    pid_json = tmp_path / ".omg" / "state" / "runs" / rid / "pid.json"
    pid_json.write_text(
        json.dumps({"pid": 999999, "starttime": start, "pgid": 999999}) + "\n",
        encoding="utf-8",
    )

    killpgs: list[tuple[int, int]] = []
    kills: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))
        raise ProcessLookupError(f"no process group {pgid}")

    def fake_kill(pid, sig):
        if sig == 0:
            return  # alive for match check
        kills.append((pid, sig))
        raise ProcessLookupError(f"no process {pid}")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(state_mod, "process_starttime", lambda pid: start)

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    # killpg tried first; fallback kill after killpg fails
    assert killpgs == [(999999, signal.SIGTERM)]
    assert kills == [(999999, signal.SIGTERM)]


def test_cancel_run_killpg_success_skips_single_kill(tmp_path, monkeypatch):
    from omg_cli import state as state_mod

    run = create_run(tmp_path, mode="ulw", goal="pg")
    rid = run["run_id"]
    start = "Mon Jul 19 11:00:00 2026"
    pid_json = tmp_path / ".omg" / "state" / "runs" / rid / "pid.json"
    pid_json.write_text(
        json.dumps({"pid": 424242, "starttime": start, "pgid": 424242}) + "\n",
        encoding="utf-8",
    )

    killpgs: list[tuple[int, int]] = []
    kills: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))
        # success — no raise

    def fake_kill(pid, sig):
        if sig == 0:
            return
        kills.append((pid, sig))

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(state_mod, "process_starttime", lambda pid: start)

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    assert killpgs == [(424242, signal.SIGTERM)]
    assert kills == []
    assert cancelled.get("kill_actions") == ["leader:killpg:SIGTERM"]


def test_cancel_missing_starttime_does_not_kill(tmp_path, monkeypatch):
    """Legacy plain pid / missing starttime → fail-closed: mark cancelled, no signal."""
    run = create_run(tmp_path, mode="ulw", goal="legacy")
    rid = run["run_id"]
    pid_path = tmp_path / ".omg" / "state" / "runs" / rid / "pid"
    pid_path.write_text("888888\n", encoding="utf-8")

    killpgs: list[tuple[int, int]] = []
    kills: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))

    def fake_kill(pid, sig):
        kills.append((pid, sig))

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    assert killpgs == []
    assert kills == []
    assert any("missing_starttime" in a for a in cancelled.get("kill_actions") or [])


def test_cancel_ps_failed_does_not_kill(tmp_path, monkeypatch):
    """ps starttime unavailable → fail-closed: no kill."""
    from omg_cli import state as state_mod

    run = create_run(tmp_path, mode="ulw", goal="ps-fail")
    rid = run["run_id"]
    pid_json = tmp_path / ".omg" / "state" / "runs" / rid / "pid.json"
    pid_json.write_text(
        json.dumps(
            {
                "pid": 666001,
                "starttime": "Mon Jan  1 00:00:00 2000",
                "pgid": 666001,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    killpgs: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))

    def fake_kill(pid, sig):
        if sig == 0:
            return  # alive

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(state_mod, "process_starttime", lambda pid: None)

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    assert killpgs == []
    assert any("ps_failed" in a for a in cancelled.get("kill_actions") or [])


def test_create_run_flock_serializes_concurrent(tmp_path):
    """fcntl.flock on create.lock: concurrent create_run → one wins, one RuntimeError."""
    import threading

    results: list[str] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(goal: str) -> None:
        try:
            barrier.wait(timeout=5)
            run = create_run(tmp_path, mode="ralph", goal=goal)
            results.append(run["run_id"])
        except RuntimeError as exc:
            errors.append(str(exc))
        except Exception as exc:  # pragma: no cover
            errors.append(f"other:{exc}")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert len(results) == 1, (results, errors)
    assert len(errors) == 1, (results, errors)
    assert "active run already exists" in errors[0]
    lock = tmp_path / ".omg" / "state" / "create.lock"
    assert lock.is_file()


def test_cancel_skips_pid_reuse_when_starttime_mismatches(tmp_path, monkeypatch):
    """pid.json starttime mismatch → do not kill (PID reuse guard)."""
    from omg_cli import state as state_mod

    run = create_run(tmp_path, mode="ulw", goal="reuse")
    rid = run["run_id"]
    pid_json = tmp_path / ".omg" / "state" / "runs" / rid / "pid.json"
    pid_json.write_text(
        json.dumps({"pid": 555001, "starttime": "Mon Jan  1 00:00:00 2000", "pgid": 555001})
        + "\n",
        encoding="utf-8",
    )

    killpgs: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))

    def fake_kill(pid, sig):
        if sig == 0:
            return  # pretend alive
        raise AssertionError("should not single-kill on reuse")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(
        state_mod,
        "process_starttime",
        lambda pid: "Tue Feb  2 12:00:00 2026",  # different
    )

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    assert killpgs == []
    assert any("pid_reuse" in a for a in cancelled.get("kill_actions") or [])


def test_cancel_workers_pid_json_skeleton(tmp_path, monkeypatch):
    """cancel_run also signals workers/*.pid.json when starttime matches."""
    from omg_cli import state as state_mod

    run = create_run(tmp_path, mode="ulw", goal="workers")
    rid = run["run_id"]
    start = "Tue Jul 19 13:00:00 2026"
    workers = tmp_path / ".omg" / "state" / "runs" / rid / "workers"
    workers.mkdir(parents=True, exist_ok=True)
    (workers / "w1.pid.json").write_text(
        json.dumps({"pid": 700001, "starttime": start, "pgid": 700001}) + "\n",
        encoding="utf-8",
    )

    killpgs: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpgs.append((pgid, sig))

    def fake_kill(pid, sig):
        if sig == 0:
            return  # alive

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(state_mod, "process_starttime", lambda pid: start)

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    assert any(pg == 700001 for pg, _ in killpgs)
    assert any("worker:w1" in a for a in cancelled.get("kill_actions") or [])


def test_cancel_workers_missing_starttime_no_kill(tmp_path, monkeypatch):
    """workers/*.pid.json without starttime → fail-closed skip."""
    run = create_run(tmp_path, mode="ulw", goal="workers-legacy")
    rid = run["run_id"]
    workers = tmp_path / ".omg" / "state" / "runs" / rid / "workers"
    workers.mkdir(parents=True, exist_ok=True)
    (workers / "w1.pid.json").write_text(
        json.dumps({"pid": 700002, "starttime": None, "pgid": 700002}) + "\n",
        encoding="utf-8",
    )

    killpgs: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda *a, **k: killpgs.append(a))
    monkeypatch.setattr(os, "kill", lambda *a, **k: None)

    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    assert killpgs == []
    assert any("missing_starttime" in a for a in cancelled.get("kill_actions") or [])

def test_write_pid_metadata_shape(tmp_path, monkeypatch):
    from omg_cli.state import write_pid_metadata

    monkeypatch.setattr(
        "omg_cli.state.process_starttime",
        lambda pid: "Sun Jul 19 12:00:00 2026",
    )
    path = tmp_path / "pid.json"
    meta = write_pid_metadata(path, pid=12345, pgid=12345)
    assert meta["pid"] == 12345
    assert meta["pgid"] == 12345
    assert meta["starttime"] == "Sun Jul 19 12:00:00 2026"
    assert path.is_file()
    assert (tmp_path / "pid").read_text(encoding="utf-8").strip() == "12345"


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


def _strict_run(tmp_path, *, goal="strict locks"):
    return create_run(
        tmp_path,
        mode="ralph",
        goal=goal,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )


def test_execution_lease_is_bounded_and_contender_mutates_no_status(tmp_path):
    run = _strict_run(tmp_path)
    rid = run["run_id"]
    status_path = tmp_path / ".omg" / "state" / "runs" / rid / "status.json"

    with execution_lease(tmp_path, rid, intent="winner") as winner:
        before = status_path.read_bytes()
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def contend() -> None:
            barrier.wait(timeout=5)
            try:
                with execution_lease(
                    tmp_path, rid, intent="loser", timeout_s=0.05
                ):
                    raise AssertionError("contender unexpectedly acquired execution lease")
            except ExecutionLeaseBusy as exc:
                errors.append(exc)

        thread = threading.Thread(target=contend)
        thread.start()
        barrier.wait(timeout=5)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors and "owner=" in str(errors[0])
        assert status_path.read_bytes() == before
        assert winner.generation == 1


def test_execution_generation_increments_and_old_token_is_fenced(tmp_path):
    run = _strict_run(tmp_path)
    rid = run["run_id"]
    with execution_lease(tmp_path, rid, intent="first") as old:
        write_status(tmp_path, rid, "running", lease=old)
        first_generation = old.generation

    with execution_lease(tmp_path, rid, intent="second") as current:
        assert current.generation == first_generation + 1
        with pytest.raises(FencingError, match="not held|stale"):
            write_status(
                tmp_path,
                rid,
                "running",
                lease=old,
                extra={"iteration": 99},
            )
        updated = write_status(
            tmp_path, rid, "running", lease=current, extra={"iteration": 1}
        )
        assert updated["execution_generation"] == current.generation


def test_stale_pid_starttime_recovery_records_and_increments_generation(tmp_path):
    run = _strict_run(tmp_path)
    rid = run["run_id"]
    lease_path = tmp_path / ".omg" / "state" / "runs" / rid / "execution.lease.json"
    lease_path.write_text(
        json.dumps(
            {
                "generation": 7,
                "invocation_id": "dead-owner",
                "pid": 99999999,
                "process_starttime": "Mon Jan  1 00:00:00 2000",
                "state": "held",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with execution_lease(tmp_path, rid, intent="recover") as recovered:
        assert recovered.generation == 8
        assert recovered.stale_owner_recovered is True


def test_transition_then_execution_order_is_rejected(tmp_path):
    run = _strict_run(tmp_path)
    rid = run["run_id"]
    with transition_guard(tmp_path, rid):
        assert transition_guard_held() is True
        with pytest.raises(LifecycleLockError, match="lock-order"):
            with execution_lease(tmp_path, rid, intent="wrong-order", timeout_s=0):
                pass


def test_strict_locking_unavailable_fails_before_lock_path_mutation(
    tmp_path, monkeypatch
):
    import omg_cli.state as state_mod

    run = _strict_run(tmp_path)
    rid = run["run_id"]
    lock_path = tmp_path / ".omg" / "state" / "runs" / rid / "execution.lock"
    monkeypatch.setattr(state_mod, "fcntl", None)
    with pytest.raises(LockUnavailableError, match="POSIX"):
        with execution_lease(tmp_path, rid, intent="unsupported"):
            pass
    assert not lock_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process/flock semantics")
@pytest.mark.parametrize("replace_before_pause", [False, True])
def test_transition_guard_owner_death_releases_lock_and_preserves_replace(
    tmp_path, replace_before_pause
):
    run = _strict_run(tmp_path)
    rid = run["run_id"]
    before = load_run(tmp_path, rid)
    assert before is not None

    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    proc = ctx.Process(
        target=_transition_crash_worker,
        args=(str(tmp_path), rid, child),
        kwargs={"replace_before_pause": replace_before_pause},
    )
    proc.start()
    assert parent.poll(5), "child did not reach deterministic transition barrier"
    assert parent.recv() == "guard-held"
    proc.terminate()
    proc.join(timeout=5)
    assert not proc.is_alive()

    # OS owner death, not pathname cleanup, releases the advisory guard.
    with transition_guard(tmp_path, rid, timeout_s=1):
        observed = load_run(tmp_path, rid)
    assert observed is not None
    if replace_before_pause:
        assert observed["crash_probe"] == "committed"
    else:
        assert observed == before


def test_acceptance_capability_names_are_never_serialized_in_strict_status(tmp_path):
    run = _strict_run(tmp_path)
    rid = run["run_id"]
    with execution_lease(tmp_path, rid, intent="no-token-on-disk") as lease:
        updated = write_status(
            tmp_path,
            rid,
            "running",
            lease=lease,
            extra={
                "acceptance_capability": "forged",
                "acceptance_token": "forged",
            },
        )
    assert "acceptance_capability" not in updated
    assert "acceptance_token" not in updated
