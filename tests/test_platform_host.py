"""Real-host OS-sensitive contracts for the #25 macOS (and any *nix) lane.

Hermetic: no network / Grok account. Uses the live OS ``ps`` path for PID
start-time identity — mocks are intentionally avoided here.
"""

from __future__ import annotations

import os

import pytest

from omg_cli import state as state_mod

pytestmark = pytest.mark.platform


def test_live_process_starttime_stable_for_self() -> None:
    pid = os.getpid()
    a = state_mod.process_starttime(pid)
    b = state_mod.process_starttime(pid)
    assert a is not None and a.strip()
    assert a == b


def test_live_process_starttime_rejects_missing_pid() -> None:
    # Extremely unlikely live PID; must not invent a starttime.
    assert state_mod.process_starttime(2_000_000_001) is None
    assert state_mod.process_starttime(0) is None
    assert state_mod.process_starttime(-1) is None


def test_cancel_never_targets_current_process_group(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation must not killpg the agent/test process group."""
    from omg_cli.state import cancel_run, create_run

    run = create_run(tmp_path, mode="ralph", goal="no self-kill")
    rid = str(run["run_id"])
    our_pgid = os.getpgrp()
    # Plant a leader record that *claims* our real pgid — fail-closed path
    # must refuse to signal it when starttime does not match a real worker.
    pid_path = tmp_path / ".omg" / "state" / "runs" / rid / "leader.pid.json"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    pid_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "pgid": our_pgid,
                # Deliberately wrong starttime so cancel must not signal.
                "starttime": "Mon Jan  1 00:00:00 1970",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    killpgs: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killpgs.append((pgid, sig))
        if pgid == our_pgid:
            raise AssertionError("cancel must not killpg the current process group")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    # Real process_starttime for our pid will not match the planted 1970 stamp.
    cancelled = cancel_run(tmp_path, rid)
    assert cancelled["status"] == "cancelled"
    for pgid, _sig in killpgs:
        assert pgid != our_pgid
    # Prefer no kill at all when starttime mismatches.
    assert killpgs == [] or all(p != our_pgid for p, _ in killpgs)
