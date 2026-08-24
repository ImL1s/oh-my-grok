"""Same-window scale must stamp pane-scoped @omg_launch_nonce (#147)."""

from __future__ import annotations

import pytest


class _TmuxResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_bind_worker_pane_owner_stamps_pane_scoped_launch_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-window scale must set pane @omg_launch_nonce, not inherit window."""
    from omg_cli.team import tmux as tmux_mod

    pane_opts: dict[str, str] = {}
    set_calls: list[list[str]] = []
    show_calls: list[list[str]] = []

    def fake_if_identity(argv, **_kwargs):
        command = list(argv)
        set_calls.append(command)
        if command[:3] == ["set-option", "-p", "-t"] and len(command) >= 6:
            pane_opts[command[4]] = command[5]
        return _TmuxResult()

    def fake_run(argv, **_kwargs):
        command = list(argv)
        if command[:4] == ["show-options", "-p", "-v", "-t"]:
            show_calls.append(command)
            opt = command[5]
            val = pane_opts.get(opt, "")
            if not val:
                return _TmuxResult(returncode=1, stdout="", stderr="unknown option")
            return _TmuxResult(stdout=val + "\n")
        if command and command[0] == "display-message":
            worker = pane_opts.get(tmux_mod.WORKER_PANE_NONCE_OPTION, "")
            launch = pane_opts.get(tmux_mod.LAUNCH_NONCE_OPTION, "")
            return _TmuxResult(stdout=f"%7\t@3\t$42\t{worker}\t{launch}\n")
        return _TmuxResult()

    monkeypatch.setattr(tmux_mod, "_tmux_run_if_identity", fake_if_identity)
    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_run)
    server = {
        "tmux_socket_path": "/tmp/omg-test-tmux.sock",
        "tmux_server_pid": 424242,
        "tmux_server_pid_start": "ps:omg-test-server",
    }
    owner = "a" * 32
    launch = "b" * 32
    tmux_mod.bind_worker_pane_owner(
        pane_id="%7",
        pane_owner_nonce=owner,
        expected_server=server,
        expected_session_id="$42",
        expected_window_id="@3",
        launch_nonce=launch,
        socket_path="/tmp/omg-test-tmux.sock",
    )
    assert [
        "set-option",
        "-p",
        "-t",
        "%7",
        tmux_mod.LAUNCH_NONCE_OPTION,
        launch,
    ] in set_calls
    assert any(
        c[:6]
        == ["show-options", "-p", "-v", "-t", "%7", tmux_mod.LAUNCH_NONCE_OPTION]
        for c in show_calls
    )
    assert pane_opts[tmux_mod.LAUNCH_NONCE_OPTION] == launch


def test_bind_worker_pane_owner_refuses_window_inherited_launch_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """show-options -p empty must fail even if display-message inherits window."""
    from omg_cli.team import tmux as tmux_mod

    pane_opts: dict[str, str] = {}
    window_launch = "c" * 32

    def fake_if_identity(argv, **_kwargs):
        command = list(argv)
        if command[:3] == ["set-option", "-p", "-t"] and len(command) >= 6:
            opt = command[4]
            if opt == tmux_mod.WORKER_PANE_NONCE_OPTION:
                pane_opts[opt] = command[5]
            # Launch nonce "set" succeeds but does not land as a pane option.
        return _TmuxResult()

    def fake_run(argv, **_kwargs):
        command = list(argv)
        if command[:4] == ["show-options", "-p", "-v", "-t"]:
            opt = command[5]
            val = pane_opts.get(opt, "")
            if not val:
                return _TmuxResult(returncode=1, stdout="", stderr="unknown option")
            return _TmuxResult(stdout=val + "\n")
        if command and command[0] == "display-message":
            worker = pane_opts.get(tmux_mod.WORKER_PANE_NONCE_OPTION, "")
            return _TmuxResult(stdout=f"%7\t@3\t$42\t{worker}\t{window_launch}\n")
        return _TmuxResult()

    monkeypatch.setattr(tmux_mod, "_tmux_run_if_identity", fake_if_identity)
    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_run)
    server = {
        "tmux_socket_path": "/tmp/omg-test-tmux.sock",
        "tmux_server_pid": 424242,
        "tmux_server_pid_start": "ps:omg-test-server",
    }
    with pytest.raises(tmux_mod.TmuxTeamError, match="launch nonce readback"):
        tmux_mod.bind_worker_pane_owner(
            pane_id="%7",
            pane_owner_nonce="a" * 32,
            expected_server=server,
            expected_session_id="$42",
            expected_window_id="@3",
            launch_nonce=window_launch,
            socket_path="/tmp/omg-test-tmux.sock",
        )
