"""Real-tmux scale topology primitives (#102).

Proves same_window spawn never creates a second window, owner-nonce bind
survives, and layout reconcile is non-fatal for process identity.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from omg_cli.madmax import tmux_available

pytestmark = pytest.mark.tmux


def _require_tmux() -> None:
    if not tmux_available() or shutil.which("tmux") is None:
        pytest.skip("tmux not available")


def _tmux(sock: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", sock, *args],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def private_tmux(tmp_path: Path):
    """Isolated tmux server on a private socket."""
    _require_tmux()
    # tmux sockets have a short path-length limit; keep under /tmp.
    sock = f"/tmp/omg102-{secrets.token_hex(4)}.sock"
    session = f"omg102-{secrets.token_hex(4)}"
    created = _tmux(
        sock,
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        "leader",
        "sleep",
        "120",
    )
    if created.returncode != 0:
        pytest.skip(f"could not start private tmux: {created.stderr}")
    try:
        yield sock, session
    finally:
        _tmux(sock, "kill-server")
        try:
            Path(sock).unlink(missing_ok=True)
        except OSError:
            pass


def test_same_window_spawn_keeps_single_window_and_binds_nonce(
    private_tmux, tmp_path: Path
) -> None:
    from omg_cli.team import tmux as tmux_mod

    sock, session = private_tmux
    probe = tmux_mod._probe_tmux_server_identity(socket_path=sock)
    assert probe is not None

    meta = _tmux(
        sock,
        "display-message",
        "-p",
        "#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}",
    )
    assert meta.returncode == 0
    session_id, window_id, leader_pane, leader_pid_s = meta.stdout.strip().split("\t")
    leader_pid = int(leader_pid_s)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    nonce = secrets.token_hex(16)

    spawned = tmux_mod.spawn_worker_same_window(
        target_pane_id=leader_pane,
        team_window_id=window_id,
        worktree=str(worktree),
        pane_command="sleep 120",
        env_pairs=[],
        horizontal=True,
        expected_server=probe,
        expected_session_id=session_id,
        pane_owner_nonce=nonce,
        leader_pane_id=leader_pane,
        socket_path=sock,
    )
    assert spawned.window_id == window_id
    assert spawned.pane_id != leader_pane
    assert spawned.pane_owner_nonce == nonce

    windows = _tmux(sock, "list-windows", "-t", session, "-F", "#{window_id}")
    assert windows.returncode == 0
    assert windows.stdout.strip().splitlines() == [window_id]

    panes = _tmux(sock, "list-panes", "-t", window_id, "-F", "#{pane_id}")
    assert panes.returncode == 0
    pane_ids = panes.stdout.strip().splitlines()
    assert leader_pane in pane_ids
    assert spawned.pane_id in pane_ids
    assert len(pane_ids) == 2

    found = tmux_mod.discover_worker_pane_by_owner_nonce(
        session_id=session_id,
        window_id=window_id,
        pane_owner_nonce=nonce,
        socket_path=sock,
    )
    assert found == spawned.pane_id

    layout = tmux_mod.reconcile_layout(
        mode="same_window",
        team_window_id=window_id,
        leader_pane_id=leader_pane,
        leader_pane_pid=leader_pid,
        session_id=session_id,
        worker_count=1,
        expected_server=probe,
        socket_path=sock,
    )
    assert layout.status == "clean"

    # Scale a second worker against the worker stack (vertical), still one window.
    nonce2 = secrets.token_hex(16)
    spawned2 = tmux_mod.spawn_worker_same_window(
        target_pane_id=spawned.pane_id,
        team_window_id=window_id,
        worktree=str(worktree),
        pane_command="sleep 120",
        env_pairs=[],
        horizontal=False,
        expected_server=probe,
        expected_session_id=session_id,
        pane_owner_nonce=nonce2,
        leader_pane_id=leader_pane,
        socket_path=sock,
    )
    windows2 = _tmux(sock, "list-windows", "-t", session, "-F", "#{window_id}")
    assert windows2.stdout.strip().splitlines() == [window_id]
    panes2 = _tmux(sock, "list-panes", "-t", window_id, "-F", "#{pane_id}")
    assert len(panes2.stdout.strip().splitlines()) == 3
    assert spawned2.pane_id in panes2.stdout

    err = tmux_mod.kill_exact_worker_pane(
        pane_id=spawned2.pane_id,
        expected_server=probe,
        expected_session_id=session_id,
        expected_window_id=window_id,
        leader_pane_id=leader_pane,
        socket_path=sock,
    )
    assert err is None
    # Leader must survive scale-down of a worker pane.
    still = _tmux(
        sock,
        "display-message",
        "-t",
        leader_pane,
        "-p",
        "#{pane_id}\t#{pane_pid}",
    )
    assert still.returncode == 0
    assert still.stdout.strip().startswith(leader_pane)
    time.sleep(0.05)
