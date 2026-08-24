"""#147 PR2 follow-up: real-tmux interactive TUI-ready + PROVIDER_ECHO.

Requires POSIX tmux. Skip on Windows / missing tmux. Credential-free fixture
only — does not claim LIVE_TEAM_INTERACTIVE_TTY_OK.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from omg_cli.madmax import tmux_available
from omg_cli.team.io_capability import E_OPERATOR_INPUT_NOT_READY
from omg_cli.team.plane import EXPERIMENTAL_ENV, stop_team
from tests.support.team_tmux_harness import (
    IsolatedTmuxServer,
    LeaderSession,
    init_git_repo,
    launch_team_inside,
    wait_until,
)

pytestmark = [pytest.mark.tmux, pytest.mark.tmux_real]


@pytest.fixture(autouse=True)
def _require_posix_tmux() -> None:
    if os.name != "posix" or sys.platform == "win32":
        pytest.skip("POSIX tmux only")
    if not tmux_available() and shutil.which("tmux") is None:
        pytest.skip("tmux not available")


@pytest.fixture
def tmux_server(request: pytest.FixtureRequest):
    with IsolatedTmuxServer(prefix="omg147") as server:
        try:
            yield server
        finally:
            try:
                server.dump_artifacts(reason=f"teardown:{request.node.name}")
            except Exception:
                pass


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    init_git_repo(root)
    return root


def _leader(server: IsolatedTmuxServer) -> LeaderSession:
    sess = LeaderSession(server)
    sess.create()
    return sess


def _bind_leader(monkeypatch: pytest.MonkeyPatch, leader: LeaderSession) -> None:
    for k, v in leader.tmux_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "25000")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_HOLD_S", "40")


def test_interactive_fixture_tui_ready_then_provider_echo(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launch interactive fixture, wait TUI_READY, submit, require PROVIDER_ECHO.

    Plain send-keys echo of the marker is not sufficient: the fixture prints
    PROVIDER_ECHO only after a TTY read with echo disabled.
    """
    from omg_cli.team import operator

    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    meta = launch_team_inside(
        root=repo,
        leader=leader,
        workers=1,
        monkeypatch=monkeypatch,
        io_mode="interactive",
        env={
            "OMG_TEAM_READY_TIMEOUT_MS": "25000",
            "OMG_TEAM_PROVIDER_HOLD_S": "40",
        },
    )
    try:
        task = (meta.get("tasks") or [{}])[0]
        pane_id = str(task.get("pane_id") or "")
        nonce = str(task.get("interactive_nonce") or "")
        worker_id = str(task.get("task_id") or "w1")
        run_id = str(meta["run_id"])
        assert pane_id.startswith("%")
        assert nonce
        assert task.get("io_mode") == "interactive_tty"
        assert task.get("provider_tty_owner") == "provider"
        assert task.get("operator_input_supported") is True
        assert "supervisor" not in str(task.get("pane_command") or "")
        marker = f"TUI_READY:{nonce}"
        wait_until(
            lambda: marker in leader.capture_pane(pane_id),
            timeout_s=20.0,
            label="TUI_READY on pane TTY",
        )
        assert meta.get("startup_status") == "running"
        assert meta.get("startup_gate_phase") == "tui_ready"
        assert task.get("input_ready") is True

        from omg_cli.team.interactive import (
            GROK_INTERACTIVE_SEED_PROMPT,
            capture_contains_provider_echo,
            interactive_inbox_instruction,
        )
        from omg_cli.team.plane import team_dir

        rel = task.get("inbox_path")
        assert isinstance(rel, str) and rel
        inbox = team_dir(repo, run_id) / f"{worker_id}.a1.inbox.txt"
        assert inbox.is_file()
        assert inbox.name.endswith(".a1.inbox.txt")
        instruction = interactive_inbox_instruction(inbox)

        scroll_holder = {"text": ""}

        def _echoed() -> bool:
            scroll_holder["text"] = leader.capture_pane(pane_id)
            return capture_contains_provider_echo(scroll_holder["text"], instruction)

        try:
            wait_until(
                _echoed,
                timeout_s=15.0,
                label="PROVIDER_ECHO of inbox instruction (not seed)",
            )
        except TimeoutError as exc:
            panes = leader.server.tmux(
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}:dead=#{pane_dead}:pid=#{pane_pid}",
            )
            raise TimeoutError(
                f"{exc}; pane={pane_id}; capture={scroll_holder['text']!r}; "
                f"panes={(panes.stdout or '').strip()!r} rc={panes.returncode}"
            ) from exc
        scroll = scroll_holder["text"]
        assert capture_contains_provider_echo(scroll, instruction)
        assert not capture_contains_provider_echo(scroll, GROK_INTERACTIVE_SEED_PROMPT)
        assert marker in scroll
    finally:
        stop_team(repo, meta["run_id"])


def test_interactive_input_refuses_before_ready_even_with_override(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the leader has not promoted input_ready, submit stays not-ready."""
    from unittest.mock import MagicMock

    from omg_cli.team import operator
    from omg_cli.team.operator import OperatorError
    from omg_cli.team.plane import load_team_meta, mutate_team_meta

    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    meta = launch_team_inside(
        root=repo,
        leader=leader,
        workers=1,
        monkeypatch=monkeypatch,
        io_mode="interactive",
        env={
            "OMG_TEAM_READY_TIMEOUT_MS": "25000",
            "OMG_TEAM_PROVIDER_HOLD_S": "40",
        },
    )
    try:
        run_id = str(meta["run_id"])
        task = (meta.get("tasks") or [{}])[0]
        worker_id = str(task.get("task_id") or "w1")

        def _demote(current: dict) -> dict:
            for row in current.get("tasks") or []:
                if str(row.get("task_id")) == worker_id:
                    row["input_ready"] = False
                    row["interaction_evidence"] = None
            return current

        mutate_team_meta(repo, run_id, _demote)
        demoted = load_team_meta(repo, run_id)
        assert demoted["tasks"][0]["input_ready"] is False

        send_literal = MagicMock(side_effect=AssertionError("send_literal must not run"))
        monkeypatch.setattr(operator, "send_literal", send_literal)
        with pytest.raises(OperatorError) as exc:
            operator.input_worker(
                repo,
                run_id,
                worker_id,
                "should-not-send",
                submit=True,
                operator_override=True,
                is_tty=True,
            )
        assert exc.value.code == E_OPERATOR_INPUT_NOT_READY
        send_literal.assert_not_called()
    finally:
        stop_team(repo, meta["run_id"])


def test_interactive_resize_and_ctrl_c_are_observable(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tmux resize-pane delivers SIGWINCH; team key C-c prints INT: (leader lives)."""
    from omg_cli.team import operator
    from omg_cli.team.plane import load_team_meta

    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    monkeypatch.setenv("OMG_TEAM_PROVIDER_LINGER_S", "30")
    meta = launch_team_inside(
        root=repo,
        leader=leader,
        workers=1,
        monkeypatch=monkeypatch,
        io_mode="interactive",
        env={
            "OMG_TEAM_READY_TIMEOUT_MS": "25000",
            "OMG_TEAM_PROVIDER_HOLD_S": "40",
            "OMG_TEAM_PROVIDER_LINGER_S": "30",
        },
    )
    try:
        task = (meta.get("tasks") or [{}])[0]
        pane_id = str(task.get("pane_id") or "")
        worker_id = str(task.get("task_id") or "w1")
        run_id = str(meta["run_id"])
        assert pane_id.startswith("%")
        leader.server.require_ok("resize-pane", "-t", pane_id, "-x", "88", "-y", "28")
        wait_until(
            lambda: "WINCH:" in leader.capture_pane(pane_id),
            timeout_s=10.0,
            label="WINCH after resize-pane",
        )
        operator.key_worker(
            repo,
            run_id,
            worker_id,
            "C-c",
            operator_override=True,
            is_tty=True,
        )
        wait_until(
            lambda: "INT:" in leader.capture_pane(pane_id),
            timeout_s=10.0,
            label="INT: after team key C-c",
        )
        live = load_team_meta(repo, run_id)
        assert live.get("stop_state") not in {"stopped", "stopping"}
        assert str(live.get("session") or "")
        leader_pane = leader.leader
        assert leader_pane is not None
        probe = leader.server.tmux(
            "display-message", "-p", "-t", leader_pane.pane_id, "#{pane_dead}"
        )
        assert (probe.stdout or "").strip() != "1"
    finally:
        stop_team(repo, meta["run_id"])
