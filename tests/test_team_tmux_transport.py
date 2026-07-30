"""Hermetic split-pane transport smoke (fixture ACK; not Grok live parity).

Requires real ``tmux`` on PATH. Skips cleanly when absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from omg_cli.madmax import tmux_available
from omg_cli.team.api import execute_team_api
from omg_cli.team.mailbox import _recipient_path
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, stop_team
from omg_cli.team.runtime import launch_team

pytestmark = pytest.mark.tmux

TEAM_ID = "team"


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=False,
        capture_output=True,
        text=True,
    )


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


def _cleanup_session(session: str | None) -> None:
    """Best-effort session teardown (fixture panes may already have exited)."""
    if not session:
        return
    _tmux("kill-session", "-t", session)


def _pane_count(session: str) -> int:
    proc = _tmux(
        "list-panes",
        "-t",
        session,
        "-F",
        "#{pane_id}",
    )
    if proc.returncode != 0:
        return 0
    return len([line for line in (proc.stdout or "").splitlines() if line.strip()])


def _leader_ack_messages(root: Path, *, run_id: str, team_id: str) -> list[dict]:
    """Read durable leader mailbox (list API omits bodies by design)."""
    path = _recipient_path(root, run_id, team_id, "leader-fixed")
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        m
        for m in (data.get("messages") or [])
        if isinstance(m, dict) and m.get("body") == "ACK"
    ]


def _wait_acks(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    expected: int,
    timeout_s: float = 15.0,
) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    last: list[dict] = []
    while time.monotonic() < deadline:
        # API surface: mailbox-list proves leader can see deliveries.
        code, envelope = execute_team_api(
            "mailbox-list",
            {"run_id": run_id, "team_id": team_id, "worker": "leader-fixed"},
            root=root,
            env={EXPERIMENTAL_ENV: "1"},
        )
        listed = 0
        if code == 0 and envelope.get("ok"):
            listed = int((envelope.get("data") or {}).get("count") or 0)
        last = _leader_ack_messages(root, run_id=run_id, team_id=team_id)
        if len(last) >= expected and listed >= expected:
            return last
        time.sleep(0.25)
    return last


@pytest.fixture(autouse=True)
def _require_tmux() -> None:
    if not tmux_available() and shutil.which("tmux") is None:
        pytest.skip("tmux not available")


def test_split_transport_two_panes_and_acks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real tmux split + fixture ACK — hermetic transport only, not Grok parity."""
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.setenv("OMG_TEAM_FIXTURE_HOLD_S", "20")
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "20000")
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)
    _init_repo(tmp_path)

    session: str | None = None
    run_id: str | None = None
    try:
        meta = launch_team(
            "1. lane one\n2. lane two",
            workers=2,
            role="executor",
            root=tmp_path,
            dry_run=False,
            check_binary=False,
            env={EXPERIMENTAL_ENV: "1"},
            team_id=TEAM_ID,
            executor="fixture",
            detach=True,
        )
        assert meta.get("dry_run") is False
        assert meta.get("topology") == "split"
        assert meta.get("executor") == "fixture"
        assert meta.get("attach_mode") == "detached"
        assert meta.get("session_owned") is True
        assert meta.get("task_count") == 2
        # Honesty: fixture path must not look like a grok live claim.
        for task in meta.get("tasks") or []:
            cmd = str(task.get("pane_command") or "")
            assert "team_worker_fixture" in cmd
            assert "grok" not in cmd.split()

        session = str(meta.get("session") or "")
        run_id = str(meta["run_id"])
        assert session
        assert _pane_count(session) == 2
        assert meta.get("startup_status") == "running"
        # Process-level ready is primary; mailbox ACK remains enrichment.
        proc_ready = int(meta.get("startup_process_ready") or 0)
        acks_n = int(meta.get("startup_acks") or 0)
        assert proc_ready == 2 or acks_n == 2, meta
        ready_workers = set(meta.get("startup_ready_workers") or [])
        if not ready_workers:
            ready_workers = set(meta.get("startup_ack_workers") or [])
        assert ready_workers == {"w1", "w2"}

        acks = _wait_acks(
            tmp_path, run_id=run_id, team_id=TEAM_ID, expected=2, timeout_s=5.0
        )
        # Fixture still sends mailbox ACK for transport proof.
        assert len(acks) == 2, f"expected 2 ACK messages, got {acks!r}"
        senders = {str(m.get("sender_id") or "") for m in acks}
        assert senders == {"w1", "w2"}
    finally:
        if run_id:
            try:
                stop_team(tmp_path, run_id)
            except Exception:
                pass
        _cleanup_session(session)


def test_list_pane_identities_split_vs_windows_vs_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hermetic mapping for pane_index split (no real tmux)."""
    from types import SimpleNamespace

    from omg_cli.team import plane

    def _ok(stdout: str) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    # Windows topology: one pane per window (legacy 3-field).
    monkeypatch.setattr(
        plane,
        "_tmux_run",
        lambda args, **kw: _ok("0\t%0\t1001\n1\t%1\t1002\n"),
    )
    assert plane._list_pane_identities("s") == {
        0: ("%0", 1001),
        1: ("%1", 1002),
    }

    # Split topology: single window, key by pane_index (4-field).
    monkeypatch.setattr(
        plane,
        "_tmux_run",
        lambda args, **kw: _ok("0\t0\t%10\t2001\n0\t1\t%11\t2002\n"),
    )
    assert plane._list_pane_identities("s") == {
        0: ("%10", 2001),
        1: ("%11", 2002),
    }

    # Mixed multi-window multi-pane → fail closed.
    monkeypatch.setattr(
        plane,
        "_tmux_run",
        lambda args, **kw: _ok(
            "0\t0\t%20\t3001\n0\t1\t%21\t3002\n1\t0\t%22\t3003\n"
        ),
    )
    assert plane._list_pane_identities("s") == {}


def test_resolve_attach_mode_inside_detached_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team.tmux import TmuxTeamError, resolve_attach_mode

    monkeypatch.delenv("TMUX", raising=False)
    assert resolve_attach_mode(detach=True, env={}, isatty=lambda: False) == "detached"
    assert resolve_attach_mode(detach=False, env={}, isatty=lambda: True) == "detached"
    assert (
        resolve_attach_mode(
            detach=False, env={"TMUX": "/tmp/tmux-1000/default,123,0"}, isatty=lambda: False
        )
        == "inside"
    )
    with pytest.raises(TmuxTeamError, match="--detach"):
        resolve_attach_mode(detach=False, env={}, isatty=lambda: False)


def test_inside_tmux_splits_current_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TMUX is set, create uses new-window + split-window (not new-session)."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    calls: list[list[str]] = []

    def fake_tmux(args: list[str]) -> SimpleNamespace:
        calls.append(list(args))
        cmd = args[0]
        if cmd == "display-message" and "#{session_name}" in " ".join(args):
            return SimpleNamespace(returncode=0, stdout="leader\t$42\n", stderr="")
        if cmd == "display-message" and "#{pane_id}" in " ".join(args) and "-t" not in args:
            return SimpleNamespace(returncode=0, stdout="%9\n", stderr="")
        if cmd == "new-window":
            return SimpleNamespace(returncode=0, stdout="@7\t%10\n", stderr="")
        if cmd == "split-window":
            return SimpleNamespace(returncode=0, stdout="%11\n", stderr="")
        if cmd in ("select-layout", "set-option"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    tasks = [
        {
            "task_id": "w1",
            "worktree": "/tmp/w1",
            "pane_command": "true",
            "_env_pairs": [],
        },
        {
            "task_id": "w2",
            "worktree": "/tmp/w2",
            "pane_command": "true",
            "_env_pairs": [],
        },
    ]
    handle = tmux_mod.create_split_team_session(
        session="planned-name",
        tasks=tasks,
        env_pairs=[],
        attach_mode="inside",
    )
    assert handle == ("leader", "$42")
    assert tasks[0]["pane_id"] == "%10"
    assert tasks[1]["pane_id"] == "%11"
    assert tasks[0]["_tmux_launch"]["attach_mode"] == "inside"
    assert tasks[0]["_tmux_launch"]["session_owned"] is False
    assert tasks[0]["_tmux_launch"]["leader_pane_id"] == "%9"
    assert tasks[0]["_tmux_launch"]["window_id"] == "@7"
    assert any(c[0] == "new-window" for c in calls)
    assert any(c[0] == "split-window" for c in calls)
    assert not any(c[0] == "new-session" for c in calls)
    assert not any(c[0] == "kill-session" for c in calls)
