"""Hermetic split-pane transport smoke (fixture ACK; not Grok live parity).

Requires real ``tmux`` on PATH. Skips cleanly when absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from omg_cli.madmax import tmux_available
from omg_cli.team.api import execute_team_api
from omg_cli.team.mailbox import _recipient_path
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, stop_team
from omg_cli.team.runtime import launch_team

pytestmark = pytest.mark.tmux

TEAM_ID = "team"

# Stable server identity for hermetic WAL writes / sweep (no live tmux required).
FAKE_TMUX_SERVER = {
    "tmux_socket_path": "/tmp/omg-test-tmux.sock",
    "tmux_server_pid": 424242,
    "tmux_server_pid_start": "ps:omg-test-server",
}


@pytest.fixture(autouse=True)
def _fake_tmux_server_identity(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Pin probe to FAKE_TMUX_SERVER so WAL stamp/sweep stay hermetic.

    Live integration tests that drive a real tmux server keep the real probe.
    """
    from omg_cli.team import tmux as tmux_mod

    if request.node.name.startswith("test_split_transport"):
        return

    def _probe(*, socket_path: str | None = None):
        if socket_path is not None and socket_path != FAKE_TMUX_SERVER["tmux_socket_path"]:
            return {
                "tmux_socket_path": socket_path,
                "tmux_server_pid": 999999,
                "tmux_server_pid_start": "ps:foreign-server",
            }
        return dict(FAKE_TMUX_SERVER)

    monkeypatch.setattr(tmux_mod, "_probe_tmux_server_identity", _probe)


def _write_launch_intent(root: Path, **kwargs):
    from omg_cli.team.tmux import write_team_launch_intent

    kwargs.setdefault("tmux_server", FAKE_TMUX_SERVER)
    return write_team_launch_intent(root, **kwargs)


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


def _pane_current_paths(session: str) -> set[Path]:
    proc = _tmux("list-panes", "-t", session, "-F", "#{pane_current_path}")
    if proc.returncode != 0:
        return set()
    return {
        Path(line.strip()).resolve()
        for line in (proc.stdout or "").splitlines()
        if line.strip()
    }


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
        # #99: pane command is the supervisor; fixture argv lives in descriptor.
        for task in meta.get("tasks") or []:
            cmd = str(task.get("pane_command") or "")
            assert "supervisor" in cmd
            assert "worker-ready" not in cmd
            assert "grok" not in cmd.split()
            tid = str(task.get("task_id") or "")
            desc = (
                tmp_path
                / ".omg"
                / "state"
                / "runs"
                / str(meta["run_id"])
                / "team"
                / f"{tid}.provider.json"
            )
            assert desc.is_file(), desc
            payload = json.loads(desc.read_text(encoding="utf-8"))
            assert payload.get("provider") == "fixture"
            assert any("team_worker_fixture" in str(x) for x in payload.get("argv") or [])

        session = str(meta.get("session") or "")
        run_id = str(meta["run_id"])
        assert session
        assert _pane_count(session) == 2
        expected_worktrees = {
            Path(str(task["worktree"])).resolve() for task in meta.get("tasks") or []
        }
        assert _pane_current_paths(session) == expected_worktrees
        assert meta.get("startup_status") == "running"
        # #99: provider-ready gate is primary; mailbox ACK is enrichment only.
        proc_ready = int(meta.get("startup_process_ready") or 0)
        assert proc_ready == 2, meta
        ready_workers = set(meta.get("startup_ready_workers") or [])
        assert ready_workers == {"w1", "w2"}
        for row in meta.get("startup_workers") or []:
            assert row.get("gate_ok") is True
            assert row.get("provider_alive") is True
            assert row.get("legacy") is False

        acks = _wait_acks(
            tmp_path, run_id=run_id, team_id=TEAM_ID, expected=2, timeout_s=5.0
        )
        # Fixture still sends mailbox ACK for transport proof.
        assert len(acks) == 2, f"expected 2 ACK messages, got {acks!r}"
        senders = {str(m.get("sender_id") or "") for m in acks}
        assert senders == {"w1", "w2"}

        # #100: pane scrollback must not show bootstrap JSON / nested-.omg noise.
        # Capture each pane; first meaningful content belongs to the fixture.
        panes = _tmux("list-panes", "-t", session, "-F", "#{pane_id}")
        assert panes.returncode == 0
        pane_ids = [p for p in (panes.stdout or "").splitlines() if p.strip()]
        assert len(pane_ids) == 2
        for pane_id in pane_ids:
            cap = _tmux("capture-pane", "-p", "-t", pane_id)
            assert cap.returncode == 0, cap.stderr
            scroll = cap.stdout or ""
            assert "shadows ancestor" not in scroll
            assert "nearest .omg" not in scroll
            assert "ready_path" not in scroll
            assert "team.worker-ready" not in scroll
            assert '"schema_version"' not in scroll
            # Strip blank lines; remaining should not start with OMG bootstrap.
            visible = [ln for ln in scroll.splitlines() if ln.strip()]
            if visible:
                assert not visible[0].startswith("OMG worker ")
                assert "BOOTSTRAP_" not in visible[0]
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


def test_inside_dedicated_window_uses_new_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit dedicated_window path: new-window + split (not same_window).

    #104: formerly named ``test_inside_tmux_splits_current_window`` — that name
    locked the wrong default. Same-window default is covered by
    ``test_inside_same_window_default_never_calls_new_window``.
    """
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    calls: list[list[str]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@7":
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@7\t424242\n", stderr=""
                )
            if target in {"%10", "%11"} and "#{session_id}" in joined:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{target}\t$42\t@7\t424242\n",
                    stderr="",
                )
        if cmd == "if-shell" and "new-window" in joined:
            assert "-d" in joined
            assert "@3" in joined
            return SimpleNamespace(
                returncode=0, stdout="@7\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "new-window":
            raise AssertionError(f"bare new-window forbidden: {args}")
        if cmd == "if-shell" and "split-window" in joined:
            assert "-d" in joined
            assert "@7" in joined or "424242" in joined
            return SimpleNamespace(returncode=0, stdout="%11\n", stderr="")
        if cmd == "split-window":
            raise AssertionError(f"bare split-window forbidden: {args}")
        if cmd == "select-layout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "select-window":
            assert args[-1] == "%9"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "select-pane":
            assert args[-1] == "%9"
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
        view_mode="dedicated_window",
    )
    assert handle == ("leader", "$42")
    assert tasks[0]["pane_id"] == "%10"
    assert tasks[1]["pane_id"] == "%11"
    assert tasks[0]["_tmux_launch"]["attach_mode"] == "inside"
    assert tasks[0]["_tmux_launch"]["session_owned"] is False
    assert tasks[0]["_tmux_launch"]["leader_pane_id"] == "%9"
    assert tasks[0]["_tmux_launch"]["window_id"] == "@7"
    assert tasks[0]["_tmux_launch"]["session_id"] == "$42"
    assert tasks[0]["_tmux_launch"]["attach_hint"] == "tmux select-pane -t %9"
    assert tasks[0]["_tmux_launch"]["view_mode"] == "dedicated_window"
    assert tasks[0]["_tmux_launch"]["tmux_server_pid"] == FAKE_TMUX_SERVER[
        "tmux_server_pid"
    ]
    assert any(
        (c[0] == "new-window" or (c[0] == "if-shell" and "new-window" in " ".join(c)))
        and "-d" in " ".join(c)
        for c in calls
    )
    assert any(
        c[0] == "if-shell" and "split-window" in " ".join(c) for c in calls
    )
    assert not any(c[0] == "split-window" for c in calls)
    assert any(c == ["select-window", "-t", "%9"] for c in calls)
    assert any(c == ["select-pane", "-t", "%9"] for c in calls)
    assert not any(c[0] == "new-session" for c in calls)
    assert not any(c[0] == "kill-session" for c in calls)
    # No untargeted display-message (would retarget current client).
    assert not any(
        c[0] == "display-message" and "-t" not in c for c in calls
    )


def test_inside_same_window_default_never_calls_new_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#96 default inside path: split beside leader; never new-window."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    calls: list[list[str]] = []

    def fake_tmux(args, *, socket_path=None):
        argv = list(args)
        calls.append(argv)
        cmd = argv[0] if argv else ""
        joined = " ".join(argv)
        if cmd == "display-message":
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            fmt = argv[-1]
            if target == "%9" and "pane_active" in fmt:
                return SimpleNamespace(
                    returncode=0,
                    stdout="$42\t@3\t%9\t4242\t1\t1\n",
                    stderr="",
                )
            if target == "%9":
                return SimpleNamespace(
                    returncode=0,
                    stdout="leader\t$42\t@3\t%9\t4242\n",
                    stderr="",
                )
            if target == "@3" and "window_width" in fmt:
                return SimpleNamespace(returncode=0, stdout="120\n", stderr="")
            if target in ("%10", "%11"):
                if "pane_dead" in fmt or "@omg_intent_nonce" in fmt:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"{target}\t$42\t@3\t424242\t0\tnonce\n",
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{target}\t$42\t@3\t424242\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "split-window" in joined and "-h" in joined:
            assert "-d" in joined
            assert "-t %9" in joined or "%9" in joined
            return SimpleNamespace(returncode=0, stdout="%10\n", stderr="")
        if cmd == "if-shell" and "split-window" in joined and "-v" in joined:
            assert "-d" in joined
            assert "%10" in joined
            return SimpleNamespace(returncode=0, stdout="%11\n", stderr="")
        if cmd == "set-window-option" and "main-pane-width" in joined:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "select-layout":
            assert "main-vertical" in joined
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("select-window", "select-pane"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "set-option":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "show-options":
            return SimpleNamespace(returncode=0, stdout="nonce\n", stderr="")
        if cmd == "new-window":
            raise AssertionError("same_window must not call new-window")
        if cmd == "kill-window":
            raise AssertionError("same_window must not call kill-window")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(
        tmux_mod,
        "_publish_intent_nonce_on_pane",
        lambda **kw: True,
    )

    tasks = [
        {"task_id": "w1", "worktree": "/tmp/w1", "pane_command": "true", "_env_pairs": []},
        {"task_id": "w2", "worktree": "/tmp/w2", "pane_command": "true", "_env_pairs": []},
    ]
    handle = tmux_mod.create_split_team_session(
        session="planned",
        tasks=tasks,
        env_pairs=[],
        attach_mode="inside",
    )
    assert handle == ("leader", "$42")
    launch = tasks[0]["_tmux_launch"]
    assert launch["view_mode"] == "same_window"
    assert launch["layout"] == "main-vertical"
    assert launch["window_id"] == "@3"
    assert launch["leader_pane_id"] == "%9"
    assert launch["leader_pane_pid"] == 4242
    assert tasks[0]["pane_id"] == "%10"
    assert tasks[1]["pane_id"] == "%11"
    assert not any(c[0] == "new-window" for c in calls)
    assert not any(c[0] == "kill-window" for c in calls)
    assert any("main-vertical" in " ".join(c) for c in calls)
    joined_all = [" ".join(c) for c in calls]
    assert any("split-window" in j and " -h " in f" {j} " for j in joined_all)
    assert any("split-window" in j and " -v " in f" {j} " for j in joined_all)


def test_inside_same_window_preserves_exact_leader_identity_and_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    select_targets: list[str] = []

    def fake_tmux(args, *, socket_path=None):
        argv = list(args)
        cmd = argv[0] if argv else ""
        joined = " ".join(argv)
        if cmd == "display-message":
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            fmt = argv[-1]
            if "pane_active" in fmt:
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@3\t%9\t777\t1\t1\n", stderr=""
                )
            if target == "%9":
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t777\n", stderr=""
                )
            if target == "@3" and "window_width" in fmt:
                return SimpleNamespace(returncode=0, stdout="80\n", stderr="")
            if target == "%10":
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$42\t@3\t424242\n", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "split-window" in joined:
            return SimpleNamespace(returncode=0, stdout="%10\n", stderr="")
        if cmd == "set-window-option":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "select-layout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("select-window", "select-pane"):
            select_targets.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("set-option", "show-options"):
            return SimpleNamespace(returncode=0, stdout="x\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux_mod, "_publish_intent_nonce_on_pane", lambda **kw: True)

    tasks = [
        {"task_id": "w1", "worktree": "/tmp/w1", "pane_command": "true", "_env_pairs": []},
    ]
    tmux_mod.create_split_team_session(
        session="s", tasks=tasks, env_pairs=[], attach_mode="inside"
    )
    assert select_targets[-1] == "%9"
    assert "%9" in select_targets
    assert tasks[0]["_tmux_launch"]["leader_pane_pid"] == 777


def test_inside_same_window_layout_main_vertical_with_clamped_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply path must set main-pane-width + select-layout main-vertical."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.topology import clamp_main_vertical_leader_width

    assert clamp_main_vertical_leader_width(40, worker_count=1) == 20
    assert clamp_main_vertical_leader_width(100, worker_count=2) == 50
    assert clamp_main_vertical_leader_width(30, worker_count=3) == 10

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    layout_calls: list[list[str]] = []

    def fake_tmux(args, *, socket_path=None):
        argv = list(args)
        layout_calls.append(argv)
        cmd = argv[0] if argv else ""
        joined = " ".join(argv)
        if cmd == "display-message":
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            fmt = argv[-1] if argv else ""
            if target == "%9":
                if "pane_active" in fmt:
                    return SimpleNamespace(
                        returncode=0, stdout="$42\t@3\t%9\t4242\t1\t1\n", stderr=""
                    )
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@3" and "window_width" in fmt:
                return SimpleNamespace(returncode=0, stdout="80\n", stderr="")
            if target in ("%10", "%11"):
                if "pane_dead" in fmt or "@omg_intent_nonce" in fmt:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"{target}\t$42\t@3\t424242\t0\tnonce\n",
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{target}\t$42\t@3\t424242\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "split-window" in joined and "-h" in joined:
            return SimpleNamespace(returncode=0, stdout="%10\n", stderr="")
        if cmd == "if-shell" and "split-window" in joined and "-v" in joined:
            return SimpleNamespace(returncode=0, stdout="%11\n", stderr="")
        if cmd == "set-window-option" and "main-pane-width" in joined:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "select-layout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("select-window", "select-pane", "set-option", "show-options"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("new-window", "kill-window"):
            raise AssertionError(f"forbidden on same_window layout path: {argv}")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux_mod, "_publish_intent_nonce_on_pane", lambda **kw: True)

    tasks = [
        {"task_id": "w1", "worktree": "/tmp/w1", "pane_command": "true", "_env_pairs": []},
        {"task_id": "w2", "worktree": "/tmp/w2", "pane_command": "true", "_env_pairs": []},
    ]
    tmux_mod.create_split_team_session(
        session="s", tasks=tasks, env_pairs=[], attach_mode="inside"
    )
    joined = [" ".join(c) for c in layout_calls]
    assert any(
        "set-window-option" in j and "main-pane-width" in j and "40" in j
        for j in joined
    ), joined
    assert any(
        c[0] == "select-layout" and "main-vertical" in c for c in layout_calls
    ), layout_calls
    assert tasks[0]["_tmux_launch"]["layout"] == "main-vertical"
    assert tasks[0]["_tmux_launch"]["leader_width"] == 40


def test_inside_same_window_failure_after_split_kills_only_created_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    killed: list[str] = []

    def fake_tmux(args, *, socket_path=None):
        argv = list(args)
        cmd = argv[0] if argv else ""
        joined = " ".join(argv)
        if cmd == "display-message":
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            if target == "%9":
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "%10":
                fmt = argv[-1]
                if "pane_dead" in fmt or "@omg_intent_nonce" in fmt:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="%10\t$42\t@3\t424242\t0\t\n",
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$42\t@3\t424242\n", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "split-window" in joined and "-h" in joined:
            return SimpleNamespace(returncode=0, stdout="%10\n", stderr="")
        if cmd == "if-shell" and "split-window" in joined and "-v" in joined:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if cmd == "if-shell" and "kill-pane" in joined:
            # extract -t %N from kill-pane command string
            if "%10" in joined:
                killed.append("%10")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "list-panes":
            return SimpleNamespace(returncode=0, stdout="%9\n", stderr="")
        if cmd == "kill-window":
            raise AssertionError("must not kill-window on same_window rollback")
        if cmd == "set-option":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux_mod, "_publish_intent_nonce_on_pane", lambda **kw: True)
    monkeypatch.setattr(
        tmux_mod, "_verify_worker_pane_membership", lambda **kw: None
    )

    tasks = [
        {"task_id": "w1", "worktree": "/tmp/w1", "pane_command": "true", "_env_pairs": []},
        {"task_id": "w2", "worktree": "/tmp/w2", "pane_command": "true", "_env_pairs": []},
    ]
    with pytest.raises(TmuxTeamError, match="failed to split pane"):
        tmux_mod.create_split_team_session(
            session="s", tasks=tasks, env_pairs=[], attach_mode="inside"
        )
    assert "%10" in killed


def test_inside_dedicated_window_opt_in_preserves_window_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Opt-in dedicated path still uses new-window + window WAL."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import team_launch_intents_dir
    from omg_cli.team.topology import VIEW_MODE_DEDICATED_WINDOW

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "deadbeef")
    rid = "20260806T120000Z-dedicated-wal"
    calls: list[list[str]] = []
    intent_before_create: list[Path] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@7":
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@7\t424242\n", stderr=""
                )
            if target in {"%10", "%11"} and "#{session_id}" in joined:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{target}\t$42\t@7\t424242\n",
                    stderr="",
                )
        if cmd == "if-shell" and "new-window" in joined:
            intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
            assert len(intents) == 1
            intent_before_create.extend(intents)
            assert "-d" in joined
            return SimpleNamespace(
                returncode=0, stdout="@7\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "new-window":
            raise AssertionError(f"bare new-window forbidden: {args}")
        if cmd == "if-shell" and "split-window" in joined:
            return SimpleNamespace(returncode=0, stdout="%11\n", stderr="")
        if cmd == "split-window":
            raise AssertionError(f"bare split-window forbidden: {args}")
        if cmd in ("select-layout", "select-window", "select-pane", "set-option"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    tasks = [
        {"task_id": "w1", "worktree": "/tmp/w1", "pane_command": "true", "_env_pairs": []},
        {"task_id": "w2", "worktree": "/tmp/w2", "pane_command": "true", "_env_pairs": []},
    ]
    handle = tmux_mod.create_split_team_session(
        session="planned",
        tasks=tasks,
        env_pairs=[],
        attach_mode="inside",
        view_mode=VIEW_MODE_DEDICATED_WINDOW,
        root=tmp_path,
        run_id=rid,
    )
    assert handle == ("leader", "$42")
    assert intent_before_create, "WAL must exist before new-window"
    raw = json.loads(intent_before_create[0].read_text(encoding="utf-8"))
    assert raw.get("view_mode") == VIEW_MODE_DEDICATED_WINDOW
    assert str(raw.get("window_name", "")).startswith("omg-team-")
    launch = tasks[0]["_tmux_launch"]
    assert launch["view_mode"] == VIEW_MODE_DEDICATED_WINDOW
    assert launch["window_id"] == "@7"
    assert any(
        c[0] == "if-shell" and "new-window" in " ".join(c) for c in calls
    )
    assert not any(c[0] == "new-window" for c in calls)


def test_detached_session_records_explicit_view_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    def fake_tmux(args, *, socket_path=None):
        argv = list(args)
        cmd = argv[0] if argv else ""
        if cmd == "new-session":
            return SimpleNamespace(
                returncode=0,
                stdout="team\t$1\t@1\t%1\t424242\t/tmp/omg-test-tmux.sock\n",
                stderr="",
            )
        if cmd == "split-window" or (
            cmd == "if-shell" and "split-window" in " ".join(argv)
        ):
            return SimpleNamespace(returncode=0, stdout="%2\n", stderr="")
        if cmd in ("select-layout", "set-option"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(
        tmux_mod,
        "_launch_first_detached",
        lambda **kw: (
            ("team", "$1"),
            "@1",
            "%1",
            dict(FAKE_TMUX_SERVER),
        ),
    )
    monkeypatch.setattr(tmux_mod, "_split_remaining", lambda **kw: ["%2"])
    monkeypatch.setattr(
        tmux_mod, "_require_tmux_server", lambda *a, **k: dict(FAKE_TMUX_SERVER)
    )

    tasks = [
        {"task_id": "w1", "worktree": "/tmp/w1", "pane_command": "true", "_env_pairs": []},
        {"task_id": "w2", "worktree": "/tmp/w2", "pane_command": "true", "_env_pairs": []},
    ]
    tmux_mod.create_split_team_session(
        session="team", tasks=tasks, env_pairs=[], attach_mode="detached"
    )
    assert tasks[0]["_tmux_launch"]["view_mode"] == "detached_session"
    assert tasks[0]["_tmux_launch"]["window_id"] == "@1"


def test_resolve_launch_view_mode_contract() -> None:
    from omg_cli.team.topology import TopologyError, resolve_launch_view_mode

    assert resolve_launch_view_mode(inside_tmux=True) == "same_window"
    assert (
        resolve_launch_view_mode(inside_tmux=True, dedicated_window=True)
        == "dedicated_window"
    )
    assert resolve_launch_view_mode(inside_tmux=False) == "detached_session"
    with pytest.raises(TopologyError):
        resolve_launch_view_mode(inside_tmux=False, dedicated_window=True)
    with pytest.raises(TopologyError):
        resolve_launch_view_mode(
            inside_tmux=True, dedicated_window=True, detach=True
        )


def test_resolve_persisted_view_mode_legacy_dedicated() -> None:
    from omg_cli.team.topology import TopologyError, resolve_persisted_view_mode

    # Unambiguous legacy dedicated: omg-team-* window name + window_id.
    assert (
        resolve_persisted_view_mode(
            {
                "attach_mode": "inside",
                "session_owned": False,
                "window_id": "@1",
                "window_name": "omg-team-abad1dea",
            }
        )
        == "dedicated_window"
    )
    assert (
        resolve_persisted_view_mode({"attach_mode": "detached", "session_owned": True})
        == "detached_session"
    )
    assert (
        resolve_persisted_view_mode({"view_mode": "same_window", "window_id": "@9"})
        == "same_window"
    )
    # Ambiguous leader-window shape must NOT invent dedicated_window.
    with pytest.raises(TopologyError, match="ambiguous"):
        resolve_persisted_view_mode(
            {"attach_mode": "inside", "session_owned": False, "window_id": "@1"}
        )
    # Receipt-bound mode wins over stripped/mutated team.json.
    assert (
        resolve_persisted_view_mode(
            {"session_owned": False, "window_id": "@9"},
            receipt={"view_mode": "same_window", "window_id": "@9"},
        )
        == "same_window"
    )


def test_same_window_stop_stripped_team_json_view_mode_never_kills_leader_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: missing team.json view_mode must not kill-window leader.

    Receipt-bound ``same_window`` is authoritative; stop must prefer it over
    mutable team.json and never fall back to dedicated kill-window.
    """
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.state import create_run
    from omg_cli.team import plane
    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.topology import VIEW_MODE_SAME_WINDOW

    run = create_run(tmp_path, mode="ulw", goal="same-window stop regression")
    rid = str(run["run_id"])
    session = "omg-leader-session"
    launch_nonce = "a" * 32
    tasks = [
        {
            "task_id": "w1",
            "window_index": 0,
            "pane_id": "%10",
            "pid": 10001,
            "pgid": 20001,
            "pid_start": "start-10001",
            "status": "running",
        }
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session=session,
        session_id="$42",
        launch_nonce=launch_nonce,
        tasks=tasks,
        intent_nonce="b" * 16,
        window_name="omg-same-deadbeef",
        view_mode=VIEW_MODE_SAME_WINDOW,
        layout="main-vertical",
        leader_pane_id="%9",
        leader_pane_pid=4242,
        window_id="@3",
        session_owned=False,
        attach_mode="inside",
    )
    meta = {
        "writer": CLI_WRITER,
        "run_id": rid,
        "session": session,
        "launch_nonce": launch_nonce,
        "launch_receipt_sha256": receipt_hash,
        "identity_generation": 0,
        "identity_receipt_sha256": receipt_hash,
        "dry_run": False,
        "workspace_mode": "worktree",
        "tasks": tasks,
        "session_owned": False,
        "attach_mode": "inside",
        "window_id": "@3",
        "leader_pane_id": "%9",
        "tmux_socket_path": FAKE_TMUX_SERVER["tmux_socket_path"],
        "tmux_server_pid": FAKE_TMUX_SERVER["tmux_server_pid"],
        "tmux_server_pid_start": FAKE_TMUX_SERVER["tmux_server_pid_start"],
        # view_mode intentionally omitted from mutable team.json
    }
    plane._atomic_write_json(plane.team_meta_path(tmp_path, rid), meta)

    kill_window_calls: list[Any] = []
    scoped_calls: list[dict[str, Any]] = []

    def forbid_kill_window(*_a: Any, **_kw: Any) -> str | None:
        kill_window_calls.append((_a, _kw))
        return "should not kill-window"

    def capture_scoped(pane_ids: Any, **kwargs: Any) -> str | None:
        scoped_calls.append({"pane_ids": list(pane_ids), **kwargs})
        return None

    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_list_in_progress_api_tasks", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "omg_cli.workers._assert_no_pending_team_scale", lambda *_a, **_k: None
    )
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")
    monkeypatch.setattr(plane, "_pgid_for_pid", lambda pid: 20001 if pid == 10001 else None)
    monkeypatch.setattr(plane.os, "killpg", lambda *_a, **_k: None)
    monkeypatch.setattr(
        plane, "_wait_process_group_disappearance", lambda *_a, **_k: (True, None)
    )
    monkeypatch.setattr(
        plane, "_process_group_disappeared", lambda *_a, **_k: (True, None)
    )
    monkeypatch.setattr(plane, "_receipt_leader_pgid", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(
        plane,
        "_read_tmux_session_identity",
        lambda _s: (session, "$42"),
    )
    monkeypatch.setattr(
        plane,
        "_team_launch_nonce_matches",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        plane,
        "_resolve_live_signal_target",
        lambda *_a, **_k: {
            "task_id": "w1",
            "pane_id": "%10",
            "pid": 10001,
            "pgid": 20001,
            "pid_start": "start-10001",
        },
    )
    monkeypatch.setattr(
        plane, "_list_pane_identities", lambda *_a, **_k: {0: ("%10", 10001)}
    )
    monkeypatch.setattr(
        plane,
        "_pane_proven_absent",
        lambda *_a, **_k: (True, None),
    )

    def fake_tmux(args: Any, **_kw: Any) -> Any:
        from types import SimpleNamespace

        argv = list(args)
        cmd = argv[0] if argv else ""
        if cmd == "has-session":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "display-message":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if cmd == "list-panes":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plane, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "_kill_window", forbid_kill_window)
    monkeypatch.setattr(tmux_mod, "_kill_panes_scoped", capture_scoped)
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda **_k: dict(FAKE_TMUX_SERVER),
    )
    monkeypatch.setattr(
        tmux_mod,
        "_intent_tmux_server",
        lambda raw: dict(FAKE_TMUX_SERVER)
        if isinstance(raw, dict) and raw.get("tmux_socket_path")
        else None,
    )

    result = stop_team(tmp_path, rid, force=True)
    assert kill_window_calls == [], (
        f"must not kill-window leader; actions={result.get('actions')} "
        f"errors={result.get('errors')}"
    )
    assert result.get("identity_verified") is True, result
    assert result.get("process_disappearance_verified") is True, result
    assert scoped_calls, (
        f"expected receipt-bound same_window pane-scoped cleanup; "
        f"actions={result.get('actions')} errors={result.get('errors')}"
    )
    assert scoped_calls[0].get("leader_pane_id") == "%9"
    assert "%10" in scoped_calls[0]["pane_ids"]


# --- end same_window unit coverage ---


def test_inside_commit_refuses_when_worker_pane_leaves_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-split pane membership mismatch must abort before stamp/meta."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    killed: list[str] = []
    window_alive = True

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        nonlocal window_alive
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@7":
                if "#{socket_path}" in joined:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="$42\t@7\t/tmp/omg-test-tmux.sock\t424242\n",
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@7\t424242\n", stderr=""
                )
            if target == "%10" and "#{session_id}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$99\t@7\t424242\n", stderr=""
                )
        if cmd == "if-shell" and "new-window" in joined:
            return SimpleNamespace(
                returncode=0, stdout="@7\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "new-window":
            return SimpleNamespace(
                returncode=0, stdout="@7\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "list-windows":
            if "-a" in args:
                # Global id probe used by _kill_window absence proof.
                if window_alive:
                    return SimpleNamespace(returncode=0, stdout="@7\n", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if window_alive:
                return SimpleNamespace(
                    returncode=0,
                    stdout="@7\tomg-team-deadbeef\t$42\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "kill-window" in joined:
            killed.append("@7")
            window_alive = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            killed.append(args[-1])
            window_alive = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "deadbeef")

    tasks = [
        {
            "task_id": "w1",
            "worktree": "/tmp/w1",
            "pane_command": "true",
            "_env_pairs": [],
        }
    ]
    with pytest.raises(TmuxTeamError, match="left the invoking session"):
        tmux_mod.create_split_team_session(
            session="planned-name",
            tasks=tasks,
            env_pairs=[],
            attach_mode="inside",
            view_mode="dedicated_window",
        )
    assert "@7" in killed
    assert "_tmux_launch" not in tasks[0]


def test_resolve_invoking_pane_requires_exact_tmux_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team.tmux import TmuxTeamError, resolve_invoking_pane

    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert resolve_invoking_pane(pane="%42") == "%42"
    assert resolve_invoking_pane(env={"TMUX_PANE": "%7"}) == "%7"
    with pytest.raises(TmuxTeamError, match="TMUX_PANE"):
        resolve_invoking_pane(env={"TMUX": "/tmp/tmux-1000/default,1,0"})
    with pytest.raises(TmuxTeamError, match="invalid TMUX_PANE"):
        resolve_invoking_pane(env={"TMUX_PANE": "not-a-pane"})


def test_inside_launch_refuses_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError

    monkeypatch.setenv("TMUX_PANE", "%9")
    calls: list[list[str]] = []
    snaps = iter(
        [
            "leader\t$42\t@3\t%9\t4242\n",
            "other\t$99\t@3\t%9\t4242\n",  # drifted session before mutate
        ]
    )

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        if args[0] == "display-message" and "#{pane_pid}" in " ".join(args):
            return SimpleNamespace(returncode=0, stdout=next(snaps), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="should not mutate")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    with pytest.raises(TmuxTeamError, match="identity drifted"):
        tmux_mod.create_split_team_session(
            session="planned",
            tasks=[
                {
                    "task_id": "w1",
                    "worktree": "/tmp/w1",
                    "pane_command": "true",
                    "_env_pairs": [],
                }
            ],
            env_pairs=[],
            attach_mode="inside",
            view_mode="dedicated_window",
        )
    assert not any(c[0] == "new-window" for c in calls)


def test_inside_new_window_malformed_stdout_kills_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc=0 + empty/malformed stdout must discover-by-name and kill-window."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "deadbeef")
    killed: list[str] = []
    listed_windows = 0
    residual = {"@77"}

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        nonlocal listed_windows
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
        if cmd == "if-shell" and "new-window" in joined:
            # Side effect succeeded; result publication failed (empty stdout).
            return SimpleNamespace(returncode=0, stdout="\n", stderr="")
        if cmd == "new-window":
            # Side effect succeeded; result publication failed.
            return SimpleNamespace(returncode=0, stdout="\n", stderr="")
        if cmd == "list-windows":
            listed_windows += 1
            assert "-a" in args
            assert "-t" not in args
            # Name discovery / proof uses window_name in -F; id absence does not.
            if "window_name" in joined:
                if residual:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="@77\tomg-team-deadbeef\t$42\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            # Global window-id absence proof from _kill_window.
            ids = "".join(f"{wid}\n" for wid in sorted(residual))
            return SimpleNamespace(returncode=0, stdout=ids, stderr="")
        if cmd == "if-shell" and "kill-window" in joined:
            if "@77" in joined:
                killed.append("@77")
            if "omg-team-deadbeef" in joined:
                killed.append("$42:omg-team-deadbeef")
            residual.clear()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            killed.append(args[-1])
            residual.clear()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    with pytest.raises(TmuxTeamError, match="absence proven|did not return window/pane ids"):
        tmux_mod.create_split_team_session(
            session="planned-name",
            tasks=[
                {
                    "task_id": "w1",
                    "worktree": "/tmp/w1",
                    "pane_command": "true",
                    "_env_pairs": [],
                }
            ],
            env_pairs=[],
            attach_mode="inside",
            view_mode="dedicated_window",
        )
    assert listed_windows >= 2  # discover + absence proof
    assert "@77" in killed
    assert "$42:omg-team-deadbeef" in killed


@pytest.mark.parametrize(
    "list_windows_behavior",
    ["nonzero", "oserror", "malformed", "ambiguous"],
)
def test_inside_new_window_readback_failure_modes_kill_by_name(
    monkeypatch: pytest.MonkeyPatch,
    list_windows_behavior: str,
) -> None:
    """list-windows non-zero/OSError/malformed/ambiguous must still kill by name."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "cafebabe")
    killed: list[str] = []
    residual_windows = {"@77", "@88"} if list_windows_behavior == "ambiguous" else {"@77"}
    list_phase = {"n": 0}

    def _list_stdout() -> str:
        if list_windows_behavior == "ambiguous":
            rows = []
            if "@77" in residual_windows:
                rows.append("@77\tomg-team-cafebabe\t$42")
            if "@88" in residual_windows:
                rows.append("@88\tomg-team-cafebabe\t$42")
            return "\n".join(rows) + ("\n" if rows else "")
        if residual_windows:
            return "@77\tomg-team-cafebabe\t$42\n"
        return ""

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
        if cmd == "new-window":
            return SimpleNamespace(returncode=0, stdout="garbage", stderr="")
        if cmd == "list-windows":
            list_phase["n"] += 1
            joined_lw = " ".join(args)
            # Id-absence probes (-a without window_name) stay separate from
            # server-global name discovery/proof (-a + window_name).
            if "-a" in args and "window_name" not in joined_lw:
                ids = "".join(f"{wid}\n" for wid in sorted(residual_windows))
                return SimpleNamespace(returncode=0, stdout=ids, stderr="")
            # First discovery may be unknown; after kill, absence proof for
            # unknown modes stays unknown (fail closed). Ambiguous/found can
            # prove absence once residuals are cleared.
            if list_windows_behavior == "nonzero" and not killed:
                return SimpleNamespace(returncode=2, stdout="", stderr="server error")
            if list_windows_behavior == "oserror" and not killed:
                raise OSError("tmux list-windows pipe broken")
            if list_windows_behavior == "malformed" and not killed:
                return SimpleNamespace(
                    returncode=0, stdout="not-a-window-row\n", stderr=""
                )
            if list_windows_behavior in ("nonzero", "oserror", "malformed"):
                # Absence proof still unknown after name-target kill attempt.
                if list_windows_behavior == "nonzero":
                    return SimpleNamespace(
                        returncode=2, stdout="", stderr="server error"
                    )
                if list_windows_behavior == "oserror":
                    raise OSError("tmux list-windows pipe broken")
                return SimpleNamespace(
                    returncode=0, stdout="not-a-window-row\n", stderr=""
                )
            return SimpleNamespace(
                returncode=0, stdout=_list_stdout(), stderr=""
            )
        if cmd == "if-shell":
            # Atomic @N kill from discover→bind path.
            for wid in ("@77", "@88"):
                if wid in joined and wid in residual_windows:
                    killed.append(wid)
                    residual_windows.discard(wid)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            target = args[-1]
            killed.append(target)
            if target.startswith("@"):
                residual_windows.discard(target)
            elif target == "$42:omg-team-cafebabe":
                residual_windows.clear()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    with pytest.raises(TmuxTeamError, match="did not return window/pane ids"):
        tmux_mod.create_split_team_session(
            session="planned-name",
            tasks=[
                {
                    "task_id": "w1",
                    "worktree": "/tmp/w1",
                    "pane_command": "true",
                    "_env_pairs": [],
                }
            ],
            env_pairs=[],
            attach_mode="inside",
            view_mode="dedicated_window",
        )

    assert "$42:omg-team-cafebabe" in killed
    if list_windows_behavior == "ambiguous":
        assert "@77" in killed and "@88" in killed
        assert not residual_windows
    elif list_windows_behavior in ("nonzero", "oserror", "malformed"):
        # Kill attempted but absence unproven → cleanup failure in message.
        pass


def test_kill_inside_windows_absence_proof_rejects_still_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill-window rc=1 with window still listed must fail cleanup."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            if "window_name" in joined:
                return SimpleNamespace(
                    returncode=0,
                    stdout="@77\tomg-team-stillhere\t$42\n",
                    stderr="",
                )
            if "-a" in args:
                return SimpleNamespace(returncode=0, stdout="@77\n", stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout="@77\tomg-team-stillhere\t$42\n",
                stderr="",
            )
        if cmd == "if-shell":
            # Predicate matched but window remains (kill ineffective).
            return SimpleNamespace(
                returncode=0, stdout="@77\t$42\t1\n", stderr=""
            )
        if cmd == "kill-window":
            # tmux often returns 1 for "no such window" — must not treat as gone
            # when list-windows still shows the name.
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find window")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42", window_name="omg-team-stillhere"
    )
    assert err is not None
    assert "still present" in err


def test_kill_inside_windows_absence_proof_ok_when_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful cleanup requires post-kill list-windows proving absent."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    residual = {"@77"}

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "list-windows":
            if residual:
                if "-a" in args:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="".join(f"{wid}\n" for wid in sorted(residual)),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout="@77\tomg-team-gone\t$42\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell":
            # Atomic session-scoped kill + absence list in one queue.
            residual.clear()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            residual.clear()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42", window_name="omg-team-gone"
    )
    assert err is None


def test_inside_launch_writes_and_clears_intent_on_successful_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Intent WAL is written before new-window and cleared when absence proven."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError, team_launch_intents_dir

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "abad1dea")
    rid = "20260806T120000Z-intent"
    residual = {"@77"}
    intent_seen: list[Path] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
        if cmd == "if-shell" and "new-window" in joined:
            # Intent must already exist before side effect.
            intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
            assert len(intents) == 1
            intent_seen.extend(intents)
            return SimpleNamespace(returncode=0, stdout="\n", stderr="")
        if cmd == "new-window":
            # Intent must already exist before side effect.
            intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
            assert len(intents) == 1
            intent_seen.extend(intents)
            return SimpleNamespace(returncode=0, stdout="\n", stderr="")
        if cmd == "list-windows":
            if residual:
                if "window_name" in joined:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="@77\tomg-team-abad1dea\t$42\n",
                        stderr="",
                    )
                if "-a" in args:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="".join(f"{wid}\n" for wid in sorted(residual)),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout="@77\tomg-team-abad1dea\t$42\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "kill-window" in joined:
            residual.clear()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            residual.clear()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    with pytest.raises(TmuxTeamError, match="absence proven"):
        tmux_mod.create_split_team_session(
            session="planned-name",
            tasks=[
                {
                    "task_id": "w1",
                    "worktree": "/tmp/w1",
                    "pane_command": "true",
                    "_env_pairs": [],
                }
            ],
            env_pairs=[],
            attach_mode="inside",
            root=tmp_path,
            run_id=rid,
            view_mode="dedicated_window",
        )
    assert intent_seen
    assert not intent_seen[0].is_file()


def test_require_clean_launch_intents_refuses_when_sweep_unproven(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sweep ok=False is a launch gate — start_team must not reach new-window."""
    import subprocess

    from omg_cli.team import plane, tmux as tmux_mod
    from omg_cli.team.tmux import (
        require_clean_team_launch_intents,
    )

    intent = _write_launch_intent(
        tmp_path,
        run_id="20260806T120000Z-oldrun",
        session_id="$99",
        window_name="omg-team-orphan",
        nonce="deadbeefdeadbeefdeadbeefdeadbeef",
    )
    assert intent.is_file()
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        tmux_mod,
        "_kill_inside_windows_by_name",
        lambda **_kw: "window still present after kill",
    )
    with pytest.raises(tmux_mod.TmuxTeamError, match="not proven cleaned"):
        require_clean_team_launch_intents(tmp_path)

    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(
        "omg_cli.team.tmux.create_split_team_session",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_split must not run when intents unclean")
        ),
    )
    monkeypatch.setattr(
        "omg_cli.team.plane._create_tmux_session",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("windows create must not run")
        ),
    )

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    with pytest.raises(plane.TeamError, match="not proven cleaned"):
        plane.start_team(
            "gate",
            [{"task_id": "t1", "title": "one", "owned_files": ["README.md"]}],
            root=tmp_path,
            dry_run=False,
            detach=True,
            check_binary=False,
            executor="fixture",
            topology="split",
        )
    assert intent.is_file()  # uncleared on failed sweep


def _authoritative_v2_launch_receipt_fixture(
    root: Path,
    *,
    run_id: str,
    session_id: str,
    intent_nonce: str,
    window_name: str,
    launch_nonce: str = "a" * 32,
    session_name: str = "omg-workers",
) -> dict[str, object]:
    """Persist a real schema-v2 launch receipt + matching team.json authority."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane

    tasks = [
        {
            "task_id": "t1",
            "window_index": 0,
            "pane_id": "%10",
            "pid": 10001,
            "pgid": 20001,
            "pid_start": "start-10001",
            "status": "running",
        }
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        root,
        run_id,
        session=session_name,
        session_id=session_id,
        launch_nonce=launch_nonce,
        tasks=tasks,
        intent_nonce=intent_nonce,
        window_name=window_name,
    )
    meta = {
        "run_id": run_id,
        "created_at": "2026-08-06T12:00:00Z",
        "session": session_name,
        "launch_nonce": launch_nonce,
        "launch_receipt_sha256": receipt_hash,
        "identity_generation": 0,
        "identity_receipt_sha256": receipt_hash,
        "workspace_mode": "worktree",
        "writer": CLI_WRITER,
        "dry_run": False,
        "tasks": tasks,
    }
    plane._atomic_write_json(plane.team_meta_path(root, run_id), meta)
    return meta


def test_sweep_refuses_adopt_when_identity_generation_skips_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-zero/malformed identity_generation without chain must not clear WAL."""
    from omg_cli.team import plane
    from omg_cli.team.tmux import (
        _intent_receipt_matches,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-gen-skip"
    nonce = "cafebabecafebabecafebabecafebabe"
    window_name = "omg-team-worker"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name=window_name,
        nonce=nonce,
    )
    meta = _authoritative_v2_launch_receipt_fixture(
        tmp_path,
        run_id=rid,
        session_id="$42",
        intent_nonce=nonce,
        window_name=window_name,
    )
    # Attack shape: gen=1 + empty tasks, no generation-1 identity receipt.
    poisoned = dict(meta)
    poisoned["identity_generation"] = 1
    poisoned["tasks"] = []
    plane._atomic_write_json(plane.team_meta_path(tmp_path, rid), poisoned)

    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    assert _intent_receipt_matches(tmp_path, raw) is False

    kills: list[dict[str, str]] = []

    def boom_kill(**kwargs):
        kills.append(dict(kwargs))
        return "should not kill"

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert kills == []
    assert intent.is_file()


@pytest.mark.parametrize("bad_generation", [True, "1", -1, 1.5])
def test_sweep_refuses_adopt_on_malformed_identity_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_generation: object
) -> None:
    """Bool/str/negative identity_generation must not skip continuity / adopt."""
    from omg_cli.team import plane
    from omg_cli.team.tmux import (
        _intent_receipt_matches,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-malgen" + str(abs(hash(repr(bad_generation))))[:8]
    nonce = "dddddddddddddddddddddddddddddddd"
    window_name = "omg-team-worker"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name=window_name,
        nonce=nonce,
    )
    meta = _authoritative_v2_launch_receipt_fixture(
        tmp_path,
        run_id=rid,
        session_id="$42",
        intent_nonce=nonce,
        window_name=window_name,
    )
    poisoned = dict(meta)
    poisoned["identity_generation"] = bad_generation
    poisoned["tasks"] = []
    plane._atomic_write_json(plane.team_meta_path(tmp_path, rid), poisoned)

    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    assert _intent_receipt_matches(tmp_path, raw) is False
    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name",
        lambda **_k: "should not kill",
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert intent.is_file()


def test_kill_window_requires_global_absence_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill-window rc 0/1 alone is not success — list-windows -a must omit id."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    still_present = True

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        # Unscoped path combines kill + list in one client queue.
        if cmd == "kill-window" and ";" in args:
            if still_present:
                return SimpleNamespace(returncode=0, stdout="@9\n@12\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="@9\n", stderr="")
        if cmd == "kill-window":
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        if cmd == "list-windows" and "-a" in args:
            if still_present:
                return SimpleNamespace(returncode=0, stdout="@9\n@12\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="@9\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    err = tmux_mod._kill_window("@12")
    assert err is not None
    assert "still present" in err

    still_present = False
    assert tmux_mod._kill_window("@12") is None


def test_kill_inside_name_absence_does_not_override_unproven_window_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Renamed window: name gone must not clear when discovered @N still lives."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    # After kill attempts, the launch name disappears (rename) but @77 remains.
    name_visible = True

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        nonlocal name_visible
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            if "window_name" in joined:
                if name_visible:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="@77\tomg-team-renamed-away\t$42\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "-a" in args:
                # Immutable id still globally present throughout.
                return SimpleNamespace(returncode=0, stdout="@77\n", stderr="")
            if name_visible:
                return SimpleNamespace(
                    returncode=0,
                    stdout="@77\tomg-team-renamed-away\t$42\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell":
            # Atomic kill "runs" but @77 remains (rename hides name only).
            name_visible = False
            return SimpleNamespace(
                returncode=0, stdout="@77\t$42\t1\n", stderr=""
            )
        if cmd == "kill-window":
            # Simulate rename: original name no longer addressable, id lives.
            name_visible = False
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find window")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42", window_name="omg-team-renamed-away"
    )
    assert err is not None
    assert "unproven absent" in err or "still present" in err
    assert "@77" in err


def test_create_inside_retains_wal_when_window_id_kill_unproven(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exact-ID kill unproven + name absent must not clear launch-intent WAL."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError, team_launch_intents_dir

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "c0ffee00")
    rid = "20260806T120000Z-idkill"
    cleared: list[object] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@77":
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@77\t424242\n", stderr=""
                )
            if target == "%10" and "#{session_id}" in joined:
                # Force exception path after window_id is known.
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$99\t@77\t424242\n", stderr=""
                )
        if cmd == "if-shell" and "new-window" in joined:
            return SimpleNamespace(
                returncode=0, stdout="@77\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "new-window":
            return SimpleNamespace(
                returncode=0, stdout="@77\t%10\t$42\t424242\n", stderr=""
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(
        tmux_mod,
        "_kill_window",
        lambda wid, **_kw: f"kill-window {wid}: still present after kill",
    )
    monkeypatch.setattr(
        tmux_mod,
        "_kill_inside_windows_by_name",
        lambda **_kw: None,  # name-only absence (rename) — must NOT authorize WAL clear
    )
    real_clear = tmux_mod.clear_team_launch_intent

    def track_clear(path):
        cleared.append(path)
        return real_clear(path)

    monkeypatch.setattr(tmux_mod, "clear_team_launch_intent", track_clear)

    with pytest.raises(TmuxTeamError, match="still present"):
        tmux_mod.create_split_team_session(
            session="planned-name",
            tasks=[
                {
                    "task_id": "w1",
                    "worktree": "/tmp/w1",
                    "pane_command": "true",
                    "_env_pairs": [],
                }
            ],
            env_pairs=[],
            attach_mode="inside",
            root=tmp_path,
            run_id=rid,
            view_mode="dedicated_window",
        )
    assert cleared == []  # must not clear on unproven ID kill
    intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
    assert len(intents) == 1
    assert intents[0].is_file()  # WAL retained — launch authority recoverable


def test_bind_team_launch_intent_window_id_stamps_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """new-window handle must be durable on the WAL before receipt."""
    from omg_cli.team.tmux import (
        TmuxTeamError,
        bind_team_launch_intent_window_id,
    )

    intent = _write_launch_intent(
        tmp_path,
        run_id="20260806T120000Z-bind",
        session_id="$7",
        window_name="omg-team-bind",
        nonce="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    raw = json.loads(intent.read_text(encoding="utf-8"))
    assert "window_id" not in raw
    bind_team_launch_intent_window_id(intent, "@42")
    bound = json.loads(intent.read_text(encoding="utf-8"))
    assert bound["window_id"] == "@42"
    bind_team_launch_intent_window_id(intent, "@42")  # idempotent
    with pytest.raises(TmuxTeamError, match="already bound"):
        bind_team_launch_intent_window_id(intent, "@99")
    with pytest.raises(TmuxTeamError, match="invalid id"):
        bind_team_launch_intent_window_id(intent, "not-an-id")


def test_inside_new_window_stamps_window_id_on_launch_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Successful new-window must bind @N onto the intent WAL before return."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import team_launch_intents_dir

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "deadbeef")
    rid = "20260806T120000Z-stamp"
    stamped: list[dict] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@77":
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@77\t424242\n", stderr=""
                )
            if target == "%10":
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$42\t@77\t424242\n", stderr=""
                )
        if cmd == "if-shell" and "new-window" in joined:
            intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
            assert len(intents) == 1
            # Pre-side-effect WAL must not yet have window_id.
            pre = json.loads(intents[0].read_text(encoding="utf-8"))
            assert "window_id" not in pre
            return SimpleNamespace(
                returncode=0, stdout="@77\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "new-window":
            intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
            assert len(intents) == 1
            # Pre-side-effect WAL must not yet have window_id.
            pre = json.loads(intents[0].read_text(encoding="utf-8"))
            assert "window_id" not in pre
            return SimpleNamespace(
                returncode=0, stdout="@77\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "select-layout":
            # After new-window returns, WAL must already carry @77.
            intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
            assert len(intents) == 1
            stamped.append(json.loads(intents[0].read_text(encoding="utf-8")))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("select-window", "select-pane"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    tmux_mod.create_split_team_session(
        session="planned-name",
        tasks=[
            {
                "task_id": "w1",
                "worktree": "/tmp/w1",
                "pane_command": "true",
                "_env_pairs": [],
            }
        ],
        env_pairs=[],
        attach_mode="inside",
        root=tmp_path,
        run_id=rid,
        view_mode="dedicated_window",
    )
    assert stamped and stamped[0].get("window_id") == "@77"


def test_kill_inside_known_window_id_survives_rename_before_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WAL-stamped @N must be killed even when the launch name never appears."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    residual = {"@77"}

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            if "-a" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout="".join(f"{wid}\n" for wid in sorted(residual)),
                    stderr="",
                )
            # Name already renamed away before first discovery.
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "@77" in joined:
            residual.discard("@77")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            target = args[args.index("-t") + 1]
            if target == "@77":
                residual.discard("@77")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            # Name target misses; id path must still succeed.
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find window")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-renamed-before-discovery",
        known_window_ids=["@77"],
    )
    assert err is None
    assert residual == set()


def test_kill_inside_known_window_id_refuses_when_renamed_still_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name absent + durable @N still present must not report cleanup success."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "list-windows":
            if "-a" in args:
                return SimpleNamespace(returncode=0, stdout="@77\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell":
            return SimpleNamespace(
                returncode=0, stdout="@77\t$42\t1\n", stderr=""
            )
        if cmd == "kill-window":
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find window")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-gone-name",
        known_window_ids=["@77"],
    )
    assert err is not None
    assert "@77" in err
    assert "unproven absent" in err or "still present" in err


def test_sweep_retains_wal_when_bound_window_renamed_before_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Crash-recovery: WAL with @N + renamed window must not clear on name absence."""
    from omg_cli.team.tmux import (
        bind_team_launch_intent_window_id,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-renamed-wal"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name="omg-team-original",
        nonce="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    bind_team_launch_intent_window_id(intent, "@77")
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name",
        lambda **kwargs: (
            "window id @77 unproven absent after name "
            f"{kwargs['window_name']!r} gone; still present"
            if kwargs.get("known_window_ids") == ["@77"]
            else None  # name-only would wrongly succeed — must not be used
        ),
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert intent.is_file()
    assert "@77" in str(results[0].get("error") or "")


def test_sweep_clears_wal_when_bound_window_id_proven_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WAL with @N clears only after ID absence is proven (name may already be gone)."""
    from omg_cli.team.tmux import (
        bind_team_launch_intent_window_id,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-bound-gone"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name="omg-team-original",
        nonce="cccccccccccccccccccccccccccccccc",
    )
    bind_team_launch_intent_window_id(intent, "@77")
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_kill(**kwargs):
        seen.append(list(kwargs.get("known_window_ids") or []))
        return None

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", fake_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is True
    assert seen == [["@77"]]
    assert not intent.is_file()


def test_sweep_refuses_foreign_server_same_numbered_window_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1/P2: foreign server must not be killed; unbound/bound WAL is abandoned."""
    from omg_cli.team.tmux import (
        bind_team_launch_intent_window_id,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-server-scope"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$0",
        window_name="omg-team-server-a",
        nonce="serverscope0000000000000000000001",
    )
    bind_team_launch_intent_window_id(intent, "@1")
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    killed: list[str] = []

    def boom_kill(**kwargs):
        killed.append("called")
        raise AssertionError("must not kill when server identity mismatches")

    from omg_cli.team import tmux as tmux_mod

    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: {
            "tmux_socket_path": FAKE_TMUX_SERVER["tmux_socket_path"],
            "tmux_server_pid": 111111,
            "tmux_server_pid_start": "ps:restarted-foreign",
        },
    )
    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is True
    assert results[0].get("abandoned_server") is True
    assert killed == []
    assert not intent.is_file()


def test_kill_window_refuses_foreign_session_same_window_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: @N present in a different session must not be torn down."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "if-shell":
            # Atomic path: predicate false → no kill; list still shows foreign @1.
            assert "kill-window -t @1" in joined
            return SimpleNamespace(
                returncode=0,
                stdout="@1\t$9\t424242\n@2\t$0\t424242\n",
                stderr="",
            )
        if cmd == "kill-window":
            raise AssertionError("must not bare-kill foreign-session @N")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    err = tmux_mod._kill_window(
        "@1",
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_session_id="$0",
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is not None
    assert "refuse" in err
    assert "$9" in err or "foreign" in err


def test_kill_window_atomic_refuses_post_probe_server_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: probe→kill TOCTOU — identity+kill must be one server-side if-shell."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    calls: list[list[str]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        cmd = args[0]
        if cmd == "if-shell":
            # Shell predicate (pid + start-id), not -F-only.
            assert socket_path == FAKE_TMUX_SERVER["tmux_socket_path"]
            assert args[1:3] == ["-t", "@1"]
            assert "-F" not in args[:4]
            assert "kill-window -t @1" in args
            assert "ps:omg-test-server" in " ".join(args) or "424242" in " ".join(args)
            assert ";" in args
            assert "list-windows" in args
            return SimpleNamespace(
                returncode=0,
                stdout="@1\t$0\t111111\n",  # pid != WAL 424242
                stderr="",
            )
        if cmd in ("display-message", "kill-window"):
            raise AssertionError(
                f"scoped kill must not use separate {cmd} client call"
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    # Pre-check start-id still matches (probe before atomic queue).
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: dict(FAKE_TMUX_SERVER),
    )
    err = tmux_mod._kill_window(
        "@1",
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_session_id="$0",
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is not None
    assert "refuse" in err or "foreign" in err
    assert len(calls) == 1
    assert calls[0][0] == "if-shell"
    assert "kill-window" not in {c[0] for c in calls}


def test_kill_window_atomic_success_single_client_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: matching identity kills and proves absence in one tmux client call."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    calls: list[list[str]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        if args[0] == "if-shell":
            # Kill succeeded — @1 absent from list-windows half of the queue.
            return SimpleNamespace(
                returncode=0, stdout="@2\t$0\t424242\n", stderr=""
            )
        raise AssertionError(f"unexpected tmux call {args}")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: dict(FAKE_TMUX_SERVER),
    )
    assert (
        tmux_mod._kill_window(
            "@1",
            socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
            expected_session_id="$0",
            expected_server=FAKE_TMUX_SERVER,
        )
        is None
    )
    assert len(calls) == 1
    assert calls[0][0] == "if-shell"
    assert ";" in calls[0]
    # Shell predicate embeds pid start-id (not -F-only pid match).
    assert "-F" not in calls[0][:4]
    assert "omg-test-server" in " ".join(calls[0])


def test_kill_window_atomic_predicate_includes_pid_start_id() -> None:
    """P1: same-PID replacement must be refused by start-id in the shell predicate."""
    from omg_cli.team.tmux import _tmux_identity_shell_predicate

    pred = _tmux_identity_shell_predicate(
        expected_server=FAKE_TMUX_SERVER,
        window_id="@1",
        expected_session_id="$0",
    )
    assert "#{pid}" in pred
    assert "omg-test-server" in pred
    assert "ps -o lstart=" in pred or "/proc/" in pred
    assert "@1" in pred
    assert "$0" in pred
    # After tmux expands #{session_id}→$N, double quotes would treat $N as a
    # positional parameter and fail every session-scoped gate.
    assert "['#{session_id}' =" in pred.replace(" ", "") or (
        "'#{session_id}'" in pred and "\"#{session_id}\"" not in pred
    )
    assert "'#{window_id}'" in pred
    assert "\"#{session_id}\"" not in pred
    assert "\"#{window_id}\"" not in pred
    assert "\"#{pid}\"" not in pred


def test_kill_inside_name_cleanup_uses_identity_gated_if_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: name-target cleanup must not bare-kill when expected_server is set."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    calls: list[list[str]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        cmd = args[0]
        if cmd == "list-windows":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell":
            assert "kill-window -t $42:omg-team-nameonly" in " ".join(args)
            assert "ps:omg-test-server" in " ".join(args) or "424242" in " ".join(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            raise AssertionError("bare kill-window forbidden with expected_server")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: dict(FAKE_TMUX_SERVER),
    )
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-nameonly",
        known_window_ids=[],
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
        require_durable_window_id=False,
    )
    assert err is None
    assert any(c[0] == "if-shell" for c in calls)
    assert not any(c[0] == "kill-window" for c in calls)


def test_new_window_gated_by_wal_server_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1: new-window must run under if-shell server identity + pid readback."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "srvgate01")
    rid = "20260806T120000Z-srvgate"
    calls: list[list[str]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@7":
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@7\t424242\n", stderr=""
                )
            if target == "%10":
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$42\t@7\t424242\n", stderr=""
                )
        if cmd == "if-shell" and "new-window" in joined:
            assert socket_path == FAKE_TMUX_SERVER["tmux_socket_path"]
            assert "ps:omg-test-server" in joined or "424242" in joined
            return SimpleNamespace(
                returncode=0, stdout="@7\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "select-layout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("select-window", "select-pane"):
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
        }
    ]
    tmux_mod.create_split_team_session(
        session="planned",
        tasks=tasks,
        env_pairs=[],
        attach_mode="inside",
        root=tmp_path,
        run_id=rid,
        view_mode="dedicated_window",
    )
    assert any(c[0] == "if-shell" and "new-window" in " ".join(c) for c in calls)
    assert not any(c[0] == "new-window" for c in calls)
    assert tasks[0]["_tmux_launch"]["tmux_server_pid"] == 424242


def test_sweep_abandons_unbound_side_effect_wal_on_dead_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P2: mark-then-crash unbound WAL must not permanently gate after server death.

    Abandon requires OS-proven PID death (or a live mismatched probe) — not
    probe→None alone.
    """
    from omg_cli.team.tmux import (
        mark_team_launch_intent_side_effect,
        require_clean_team_launch_intents,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-dead-srv"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name="omg-team-pre-create-crash",
        nonce="precreatecrash000000000000000001",
    )
    mark_team_launch_intent_side_effect(intent)
    raw = json.loads(intent.read_text(encoding="utf-8"))
    assert raw.get("side_effect_started") is True
    assert "window_id" not in raw
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    from omg_cli.team import tmux as tmux_mod

    wal_pid = int(FAKE_TMUX_SERVER["tmux_server_pid"])

    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: None,  # socket unreadable
    )

    def fake_kill_sig(pid: int, _sig: int) -> None:
        if pid == wal_pid:
            raise ProcessLookupError(pid)
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", fake_kill_sig)

    def boom_kill(**_kwargs):
        raise AssertionError("must not kill when server is unreachable")

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is True
    assert results[0].get("abandoned_server") is True
    assert not intent.is_file()
    # Project launch gate must clear.
    assert require_clean_team_launch_intents(tmp_path) == []


def test_sweep_refuses_abandon_when_server_probe_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1: probe→None is UNKNOWN — must not delete WAL while server may live."""
    from omg_cli.team.tmux import (
        mark_team_launch_intent_side_effect,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-probe-unk"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name="omg-team-probe-unknown",
        nonce="probeunknown00000000000000000001",
    )
    mark_team_launch_intent_side_effect(intent)
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    from omg_cli.team import tmux as tmux_mod

    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: None,
    )
    # WAL pid still looks alive with matching start-id → not proven gone.
    wal_pid = int(FAKE_TMUX_SERVER["tmux_server_pid"])
    wal_start = str(FAKE_TMUX_SERVER["tmux_server_pid_start"])

    def fake_kill_sig(pid: int, _sig: int) -> None:
        if pid == wal_pid:
            return None
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", fake_kill_sig)
    monkeypatch.setattr(
        tmux_mod,
        "_process_start_identity",
        lambda pid: wal_start if pid == wal_pid else None,
    )

    def boom_kill(**_kwargs):
        raise AssertionError("must not kill when probe is unknown")

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert "probe unknown" in str(results[0].get("error") or "")
    assert intent.is_file()


def test_detached_create_stamps_and_scopes_cleanup_to_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: detached create must stamp server identity and scope rollback kill."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError

    calls: list[list[str]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "new-session":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"team-sess\t$9\t@9\t%10\t{FAKE_TMUX_SERVER['tmux_server_pid']}\t"
                    f"{FAKE_TMUX_SERVER['tmux_socket_path']}\n"
                ),
                stderr="",
            )
        if cmd == "if-shell" and "split-window" in joined:
            assert socket_path == FAKE_TMUX_SERVER["tmux_socket_path"]
            assert "$9" in joined or "424242" in joined
            return SimpleNamespace(returncode=0, stdout="%11\n", stderr="")
        if cmd == "split-window":
            raise AssertionError(f"bare split-window forbidden: {args}")
        if cmd == "select-layout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "set-option" and "mouse" in joined:
            return SimpleNamespace(returncode=1, stdout="", stderr="mouse boom")
        if cmd == "if-shell" and "kill-session" in joined:
            assert socket_path == FAKE_TMUX_SERVER["tmux_socket_path"]
            assert "$9" in joined
            assert "omg-test-server" in joined or "424242" in joined
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # gone
        if cmd == "kill-session":
            raise AssertionError(f"bare kill-session forbidden: {args}")
        raise AssertionError(f"unexpected {args}")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux_mod, "_process_start_identity", lambda _pid: None)

    with pytest.raises(TmuxTeamError, match="configure created tmux session"):
        tmux_mod.create_split_team_session(
            session="team-sess",
            tasks=[
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
            ],
            env_pairs=[],
            attach_mode="detached",
        )
    assert any(c[0] == "if-shell" and "kill-session" in " ".join(c) for c in calls)
    assert not any(c[0] == "kill-session" for c in calls)


def test_detached_success_stamps_tmux_server_on_launch_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: successful detached create publishes server identity on _tmux_launch."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "new-session":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"team-ok\t$5\t@5\t%10\t{FAKE_TMUX_SERVER['tmux_server_pid']}\t"
                    f"{FAKE_TMUX_SERVER['tmux_socket_path']}\n"
                ),
                stderr="",
            )
        if cmd in ("select-layout", "set-option"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected {args}")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux_mod, "_process_start_identity", lambda _pid: None)

    tasks = [
        {
            "task_id": "w1",
            "worktree": "/tmp/w1",
            "pane_command": "true",
            "_env_pairs": [],
        }
    ]
    handle = tmux_mod.create_split_team_session(
        session="team-ok",
        tasks=tasks,
        env_pairs=[],
        attach_mode="detached",
    )
    assert handle == ("team-ok", "$5")
    meta = tasks[0]["_tmux_launch"]
    assert meta["session_owned"] is True
    assert meta["window_id"] == "@5"
    assert meta["tmux_socket_path"] == FAKE_TMUX_SERVER["tmux_socket_path"]
    assert meta["tmux_server_pid"] == FAKE_TMUX_SERVER["tmux_server_pid"]
    assert meta["tmux_server_pid_start"] == FAKE_TMUX_SERVER["tmux_server_pid_start"]


def test_sweep_retains_unbound_side_effect_when_nonce_publication_unacked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P2: unbound side_effect + nonce absent without ack must retain WAL."""
    from omg_cli.team.tmux import (
        mark_team_launch_intent_side_effect,
        require_clean_team_launch_intents,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-unbound-se"
    nonce = "sideeffect00000000000000000000001"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name="omg-team-never-dispatched",
        nonce=nonce,
    )
    mark_team_launch_intent_side_effect(intent)
    raw = json.loads(intent.read_text(encoding="utf-8"))
    assert raw.get("side_effect_started") is True
    assert raw.get("nonce_published") is False
    assert "window_id" not in raw
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    def fake_kill(**kwargs):
        assert kwargs.get("require_durable_window_id") is True
        assert list(kwargs.get("known_window_ids") or []) == []
        assert kwargs.get("intent_nonce") == nonce
        assert kwargs.get("nonce_published") is False
        # Unacked publication — refuse clear even when scans are empty.
        return (
            "unbound side_effect WAL: create-time nonce publication "
            "unacknowledged — refuse clear on nonce absence alone"
        )

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", fake_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert "unacknowledged" in str(results[0].get("error"))
    assert intent.is_file()
    from omg_cli.team.tmux import TmuxTeamError

    with pytest.raises(TmuxTeamError, match="unacknowledged|stale team launch"):
        require_clean_team_launch_intents(tmp_path)


def test_kill_inside_unbound_side_effect_refuses_name_only_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: unbound side_effect without intent nonce must not clear on name gone."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "list-windows":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "kill-window" in " ".join(args):
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        if cmd == "kill-window":
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: dict(FAKE_TMUX_SERVER),
    )
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-renamed-before-bind",
        known_window_ids=[],
        require_durable_window_id=True,
        intent_nonce=None,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is not None
    assert "intent nonce" in err


def test_kill_inside_unbound_side_effect_refuses_when_intent_nonce_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: rename-before-bind — name gone but intent nonce on @N must keep WAL."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "renamedbeforebind000000000000001"
    residual = {"@7"}
    nonce_present = True

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        nonlocal nonce_present
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            if "@omg_intent_nonce" in joined:
                # Name gone; rename-surviving nonce still on @7.
                if nonce_present:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"@7\t{nonce}\t$42\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "-a" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout="".join(f"{wid}\n" for wid in sorted(residual)),
                    stderr="",
                )
            # Launch name absent (renamed).
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell":
            # @N kill appears to succeed for absence list, but nonce option
            # remains discoverable (rename-before-bind recovery key).
            residual.discard("@7")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            residual.discard("@7")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: dict(FAKE_TMUX_SERVER),
    )
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-renamed-before-bind",
        known_window_ids=[],
        require_durable_window_id=True,
        intent_nonce=nonce,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is not None
    assert "intent nonce still present" in err
    assert nonce_present is True


def test_kill_inside_unbound_refuses_nonce_absence_without_publication_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: async stamp pending — nonce absence without ack must retain WAL."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "pendingstamp00000000000000000001"

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "list-windows":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "kill-window" in " ".join(args):
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        if cmd == "kill-window":
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: dict(FAKE_TMUX_SERVER),
    )
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-rename-before-stamp",
        known_window_ids=[],
        require_durable_window_id=True,
        intent_nonce=nonce,
        nonce_published=False,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is not None
    assert "unacknowledged" in err
    assert "nonce absence alone" in err


def test_kill_inside_unbound_clears_when_nonce_publication_acked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: after durable nonce_published ack, name+nonce absent may clear."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "ackedpub000000000000000000000001"

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "list-windows":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "kill-window" in " ".join(args):
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        if cmd == "kill-window":
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: dict(FAKE_TMUX_SERVER),
    )
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-acked-gone",
        known_window_ids=[],
        require_durable_window_id=True,
        intent_nonce=nonce,
        nonce_published=True,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is None


def test_kill_inside_unbound_clears_after_observed_nonce_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: positively observing then removing nonce is a happens-before for clear."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "observedthenkill0000000000000001"
    residual = {"@9"}
    nonce_present = True

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        nonlocal nonce_present
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            if "@omg_intent_nonce" in joined:
                if nonce_present:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"@9\t{nonce}\t$42\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "-a" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout="".join(f"{wid}\n" for wid in sorted(residual)),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell":
            residual.discard("@9")
            nonce_present = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            residual.discard("@9")
            nonce_present = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        tmux_mod,
        "_probe_tmux_server_identity",
        lambda *, socket_path=None: dict(FAKE_TMUX_SERVER),
    )
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-observed-nonce",
        known_window_ids=[],
        require_durable_window_id=True,
        intent_nonce=nonce,
        nonce_published=False,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is None
    assert nonce_present is False


def test_pre_side_effect_wal_may_clear_on_name_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Crash before new-window (side_effect_started=false) may clear on name gone."""
    from omg_cli.team.tmux import sweep_stale_team_launch_intents

    rid = "20260806T120000Z-pre-se"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name="omg-team-never-created",
        nonce="presideffect0000000000000000001",
    )
    raw = json.loads(intent.read_text(encoding="utf-8"))
    assert raw.get("side_effect_started") is False
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    def fake_kill(**kwargs):
        assert kwargs.get("require_durable_window_id") is False
        return None

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", fake_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is True
    assert not intent.is_file()


def test_bind_failure_still_publishes_window_id_for_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P2: synchronous bind failure must not lose the exact @N for cleanup."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "bindfail0")
    rid = "20260806T120000Z-bindfail"
    killed_ids: list[str] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@88" and "#{socket_path}" in joined:
                return SimpleNamespace(
                    returncode=0,
                    stdout="$42\t@88\t/tmp/omg-test-tmux.sock\t424242\n",
                    stderr="",
                )
            if target == "@88":
                return SimpleNamespace(returncode=0, stdout="$42\t@88\t424242\n", stderr="")
        if cmd == "if-shell" and "new-window" in joined:
            assert socket_path == FAKE_TMUX_SERVER["tmux_socket_path"]
            return SimpleNamespace(
                returncode=0, stdout="@88\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "new-window":
            assert socket_path == FAKE_TMUX_SERVER["tmux_socket_path"]
            return SimpleNamespace(
                returncode=0, stdout="@88\t%10\t$42\t424242\n", stderr=""
            )
        if cmd == "list-windows":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "kill-window -t @88" in joined:
            # Atomic scoped kill of the published @88.
            killed_ids.append("@88")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            killed_ids.append(args[args.index("-t") + 1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "select-layout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("select-window", "select-pane"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    def boom_bind(path, window_id):
        raise TmuxTeamError(f"launch intent window_id bind refused: boom {window_id}")

    monkeypatch.setattr(tmux_mod, "bind_team_launch_intent_window_id", boom_bind)

    with pytest.raises(TmuxTeamError, match="bind refused"):
        tmux_mod.create_split_team_session(
            session="planned-name",
            tasks=[
                {
                    "task_id": "w1",
                    "worktree": "/tmp/w1",
                    "pane_command": "true",
                    "_env_pairs": [],
                }
            ],
            env_pairs=[],
            attach_mode="inside",
            root=tmp_path,
            run_id=rid,
            view_mode="dedicated_window",
        )
    assert "@88" in killed_ids


def test_new_window_nonzero_unmarks_side_effect_when_name_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P2: new-window rc!=0 with no created window must unmark side_effect_started."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import TmuxTeamError, team_launch_intents_dir

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "nwfail001")
    rid = "20260806T120000Z-nwfail"

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
        if cmd == "new-window":
            return SimpleNamespace(
                returncode=1, stdout="", stderr="can't find window"
            )
        if cmd == "list-windows":
            # Launch name never created.
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "kill-window":
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    with pytest.raises(TmuxTeamError, match="failed to create team window"):
        tmux_mod.create_split_team_session(
            session="planned-name",
            tasks=[
                {
                    "task_id": "w1",
                    "worktree": "/tmp/w1",
                    "pane_command": "true",
                    "_env_pairs": [],
                }
            ],
            env_pairs=[],
            attach_mode="inside",
            root=tmp_path,
            run_id=rid,
            view_mode="dedicated_window",
        )
    # WAL cleared after unmark + proven name absence (not permanently wedged).
    intents = list(team_launch_intents_dir(tmp_path).glob("*.json"))
    assert intents == []


def test_new_window_nonzero_with_atN_stdout_binds_and_keeps_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P2-1a: hook-rename stamp failure must not unmark when @N was emitted.

    P1-NEW: create queue must not use targetless set-option; ACK requires
    exact-handle stamp+readback after durable @N bind.
    """
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import team_launch_intents_dir

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "hookrn001")
    rid = "20260806T120000Z-hookrn"
    bound: list[str] = []
    stamps: list[list[str]] = []
    acks: list[str] = []
    events: list[str] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@88":
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@88\t424242\n", stderr=""
                )
            if target == "%10":
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$42\t@88\t424242\n", stderr=""
                )
        if cmd == "if-shell" and "new-window" in joined:
            # Detached create only — no targetless create-queue nonce stamp.
            assert "new-window -d" in joined
            assert "select-window -t @3" not in joined
            # Pane self-stamp remains inside the pane command string.
            assert "tmux set-option -wq @omg_intent_nonce" in joined
            # Targetless queue stamps must not appear as tmux list items.
            assert " ; set-option -wq @omg_intent_nonce" not in joined
            assert " ; set-option -pq @omg_intent_nonce" not in joined
            assert "set-option -w -t $42:" not in joined
            assert "set-option -p -t $42:" not in joined
            return SimpleNamespace(
                returncode=1,
                stdout="@88\t%10\t$42\t424242\n",
                stderr="can't find window: omg-team-hookrn",
            )
        if cmd == "if-shell" and "set-option" in joined:
            stamps.append(list(args))
            events.append("stamp")
            assert "-t @88" in joined or "-t %10" in joined
            # Must never be targetless.
            assert "set-option -wq @omg_intent_nonce" not in joined
            assert "set-option -pq @omg_intent_nonce" not in joined
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "show-options":
            target = args[args.index("-t") + 1]
            assert target in ("@88", "%10")
            events.append(f"readback:{target}")
            return SimpleNamespace(
                returncode=0, stdout="hookrn001\n", stderr=""
            )
        if cmd == "select-layout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("select-window", "select-pane"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "list-windows":
            return SimpleNamespace(returncode=0, stdout="@88\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    real_bind = tmux_mod.bind_team_launch_intent_window_id
    real_ack = tmux_mod.ack_team_launch_intent_nonce_published

    def track_bind(path, window_id):
        events.append("bind")
        bound.append(window_id)
        return real_bind(path, window_id)

    def track_ack(path):
        events.append("ack")
        acks.append(str(path))
        return real_ack(path)

    monkeypatch.setattr(tmux_mod, "bind_team_launch_intent_window_id", track_bind)
    monkeypatch.setattr(
        tmux_mod, "ack_team_launch_intent_nonce_published", track_ack
    )

    tasks = [
        {
            "task_id": "w1",
            "worktree": "/tmp/w1",
            "pane_command": "true",
            "_env_pairs": [],
        }
    ]
    tmux_mod.create_split_team_session(
        session="planned-name",
        tasks=tasks,
        env_pairs=[],
        attach_mode="inside",
        root=tmp_path,
        run_id=rid,
        view_mode="dedicated_window",
    )
    assert bound == ["@88"]
    assert tasks[0]["pane_id"] == "%10"
    # WAL retains durable @N — must not have been unmarked/cleared.
    intents = list(team_launch_intents_dir(tmp_path).glob("*.json"))
    assert len(intents) == 1
    raw = json.loads(intents[0].read_text(encoding="utf-8"))
    assert raw["window_id"] == "@88"
    assert raw["side_effect_started"] is True
    assert raw.get("nonce_published") is True
    assert acks  # proven publication
    # Bind before stamp/readback/ack — never ACK on -P parse alone.
    assert events.index("bind") < events.index("stamp")
    assert events.index("stamp") < events.index("readback:@88")
    assert events.index("readback:%10") < events.index("ack")
    # Post-handle @N stamp via identity-gated set-option.
    assert any("set-option -w -t @88" in " ".join(c) for c in stamps)
    assert any("set-option -p -t %10" in " ".join(c) for c in stamps)


def test_nonce_ack_refused_when_readback_misses_created_handles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1-NEW: -P handle alone must not ACK when stamp/readback misses worker."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import team_launch_intents_dir

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(tmux_mod.secrets, "token_hex", lambda _n: "noreadback01")
    rid = "20260806T120000Z-noread"

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
            if target == "@88":
                return SimpleNamespace(
                    returncode=0, stdout="$42\t@88\t424242\n", stderr=""
                )
            if target == "%10":
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$42\t@88\t424242\n", stderr=""
                )
        if cmd == "if-shell" and "new-window" in joined:
            assert " ; set-option -wq @omg_intent_nonce" not in joined
            return SimpleNamespace(
                returncode=0,
                stdout="@88\t%10\t$42\t424242\n",
                stderr="",
            )
        if cmd == "if-shell" and "set-option" in joined:
            # Stamp "succeeds" client-side but readback will miss — models a
            # retarget where options never landed on @88/%10.
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "show-options":
            # Created handles do not carry the nonce (leader may have been
            # stamped by a buggy targetless path — we must not ACK).
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "select-layout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("select-window", "select-pane"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "list-windows":
            return SimpleNamespace(returncode=0, stdout="@88\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)

    tmux_mod.create_split_team_session(
        session="planned-name",
        tasks=[
            {
                "task_id": "w1",
                "worktree": "/tmp/w1",
                "pane_command": "true",
                "_env_pairs": [],
            }
        ],
        env_pairs=[],
        attach_mode="inside",
        root=tmp_path,
        run_id=rid,
        view_mode="dedicated_window",
    )
    intents = list(team_launch_intents_dir(tmp_path).glob("*.json"))
    assert len(intents) == 1
    raw = json.loads(intents[0].read_text(encoding="utf-8"))
    assert raw["window_id"] == "@88"
    assert raw.get("nonce_published") is not True


def test_split_transport_after_new_window_move_stamps_created_not_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-NEW live: after-new-window move-window must not stamp the leader.

    Models the Pro interleaving: create in source session, hook moves the new
    window to a destination session (invalidating targetless queue current
    target). Publication must land on the created ``@N``/``%pane`` only;
    leader must never receive ``@omg_intent_nonce``, and ACK requires that
    handle-targeted readback.
    """
    from omg_cli.team import tmux as tmux_mod

    # tmux socket paths have a low OS length limit — avoid pytest's deep tmp.
    sock = f"/tmp/omg-p1-move-{os.getpid()}.sock"
    src = "omg-p1-src"
    dst = "omg-p1-dst"
    work = tmp_path / "wt"
    work.mkdir()

    def t(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", sock, *args],
            check=False,
            capture_output=True,
            text=True,
        )

    try:
        # Isolated server with two sessions + an extra source window (survives
        # if a buggy kill hit the leader alone).
        assert (
            t("new-session", "-d", "-s", src, "-n", "leader", "sleep", "60").returncode
            == 0
        )
        assert (
            t("new-window", "-d", "-t", src, "-n", "extra", "sleep", "60").returncode
            == 0
        )
        assert (
            t("new-session", "-d", "-s", dst, "-n", "park", "sleep", "60").returncode
            == 0
        )
        # Move every newly created window into dst (invalidates source winlink).
        # Synchronous hook (not run-shell -b): must run before the create client
        # returns so post-handle stamp sees the moved @N.
        assert (
            t(
                "set-hook", "-g", "after-new-window", f"move-window -t {dst}:"
            ).returncode
            == 0
        )

        leader = t(
            "display-message",
            "-p",
            "-t",
            f"{src}:leader",
            "#{session_id}\t#{window_id}\t#{pane_id}\t#{pid}\t#{socket_path}",
        )
        assert leader.returncode == 0
        sid, leader_wid, leader_pane, pid_s, sock_out = (
            leader.stdout or ""
        ).strip().split("\t")
        assert sock_out == sock
        server = tmux_mod._server_identity_from_create(
            pid=int(pid_s), socket_path=sock
        )

        # Pin ambient identity for inside helpers that read TMUX/TMUX_PANE.
        monkeypatch.setenv("TMUX", f"{sock},{pid_s},0")
        monkeypatch.setenv("TMUX_PANE", leader_pane)

        nonce = "movewindownonce000000000000000001"
        rid = "20260806T120000Z-movep1"
        intent = tmux_mod.write_team_launch_intent(
            tmp_path,
            run_id=rid,
            session_id=sid,
            window_name="omg-team-movep1",
            nonce=nonce,
            tmux_server=server,
        )

        window_id, pane_id = tmux_mod._launch_first_inside(
            task={
                "task_id": "w1",
                "worktree": str(work),
                "pane_command": "sleep 60",
                "_env_pairs": [],
            },
            env_pairs=[],
            window_name="omg-team-movep1",
            target_window=leader_wid,
            expected_session_id=sid,
            intent_path=intent,
            socket_path=sock,
            expected_server=server,
        )

        raw = json.loads(intent.read_text(encoding="utf-8"))
        assert raw["window_id"] == window_id
        assert raw.get("nonce_published") is True

        # Created worker (possibly in dst) carries the nonce.
        win_opt = t("show-options", "-wv", "-t", window_id, "@omg_intent_nonce")
        pane_opt = t("show-options", "-pv", "-t", pane_id, "@omg_intent_nonce")
        assert win_opt.returncode == 0 and (win_opt.stdout or "").strip() == nonce
        assert pane_opt.returncode == 0 and (pane_opt.stdout or "").strip() == nonce

        # Leader must never have been stamped (targetless retarget failure mode).
        leader_opt = t("show-options", "-wv", "-t", leader_wid, "@omg_intent_nonce")
        leader_val = (
            (leader_opt.stdout or "").strip() if leader_opt.returncode == 0 else ""
        )
        assert leader_val != nonce
        assert leader_val == ""

        # Worker left source session (hook move); still reachable by @N.
        where = t(
            "display-message",
            "-p",
            "-t",
            window_id,
            "#{session_name}\t#{window_id}",
        )
        assert where.returncode == 0
        sess_name, wid_check = (where.stdout or "").strip().split("\t")
        assert wid_check == window_id
        assert sess_name == dst

        # Source still has leader + extra — cleanup must not have killed leader.
        src_wins = t("list-windows", "-t", src, "-F", "#{window_name}")
        assert src_wins.returncode == 0
        names = {(line or "").strip() for line in (src_wins.stdout or "").splitlines()}
        assert "leader" in names
        assert "extra" in names
    finally:
        t("kill-server")
        try:
            os.unlink(sock)
        except OSError:
            pass


def test_kill_inside_bound_relocated_clears_with_nonce_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: durable @N moved sessions but carrying intent nonce must clear WAL."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "boundreloc00000000000000000000001"
    residual = {"@7"}
    kill_calls: list[dict[str, object]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            if "@omg_intent_nonce" in joined:
                if residual:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"@7\t{nonce}\t$99\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            # Name discovery / proof in WAL session $42 — absent (moved away).
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("if-shell", "kill-window"):
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    def track_kill(wid: str, **kwargs: object) -> str | None:
        kill_calls.append({"wid": wid, **kwargs})
        if kwargs.get("expected_session_id") is not None:
            return (
                f"kill-window {wid}: belongs to session '$99', not WAL session "
                f"{kwargs['expected_session_id']!r} — refuse foreign/restarted @N"
            )
        # Session-free (nonce-proven): kill if present; already-absent is success.
        residual.discard(wid)
        return None

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "_kill_window", track_kill)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-bound-moved",
        known_window_ids=["@7"],
        require_durable_window_id=True,
        intent_nonce=nonce,
        nonce_published=True,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is None
    assert residual == set()
    assert any(
        c["wid"] == "@7" and c.get("expected_session_id") is None for c in kill_calls
    )


def test_kill_inside_bound_foreign_session_without_nonce_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 safety: moved durable @N without matching intent nonce stays refused."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "boundforeign00000000000000000001"
    kill_calls: list[dict[str, object]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            if "@omg_intent_nonce" in joined:
                # Live @7 has a different nonce (or none) — not our intent.
                return SimpleNamespace(
                    returncode=0,
                    stdout="@7\tothernonce000000000000000000001\t$99\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("if-shell", "kill-window"):
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    def track_kill(wid: str, **kwargs: object) -> str | None:
        kill_calls.append({"wid": wid, **kwargs})
        if kwargs.get("expected_session_id") is not None:
            return (
                f"kill-window {wid}: belongs to session '$99', not WAL session "
                f"{kwargs['expected_session_id']!r} — refuse foreign/restarted @N"
            )
        # Must never reach session-free kill for non-matching nonce.
        raise AssertionError("session-free kill without nonce match")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "_kill_window", track_kill)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-bound-foreign",
        known_window_ids=["@7"],
        require_durable_window_id=True,
        intent_nonce=nonce,
        nonce_published=True,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is not None
    assert "refuse" in err or "unproven" in err
    assert all(c.get("expected_session_id") == "$42" for c in kill_calls)


def test_split_transport_create_inside_recovers_bound_moved_after_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 live: post-bind session move must kill orphan + clear WAL (no start gate).

    after-new-window moves @W to $D after create+bind+nonce ACK. Full
    create_split_team_session rejects session drift, removes the nonce-proven
    moved @W, leaves the leader live, clears the launch WAL, and the next
    require_clean / start gate must not stay blocked.
    """
    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import (
        TmuxTeamError,
        require_clean_team_launch_intents,
        team_launch_intents_dir,
    )

    sock = f"/tmp/omg-p2-bound-move-{os.getpid()}.sock"
    src = "omg-p2-src"
    dst = "omg-p2-dst"
    work = tmp_path / "wt"
    work.mkdir()
    rid = "20260806T120000Z-boundmove"

    def t(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", sock, *args],
            check=False,
            capture_output=True,
            text=True,
        )

    try:
        assert (
            t("new-session", "-d", "-s", src, "-n", "leader", "sleep", "60").returncode
            == 0
        )
        assert (
            t("new-window", "-d", "-t", src, "-n", "extra", "sleep", "60").returncode
            == 0
        )
        assert (
            t("new-session", "-d", "-s", dst, "-n", "park", "sleep", "60").returncode
            == 0
        )
        assert (
            t(
                "set-hook", "-g", "after-new-window", f"move-window -t {dst}:"
            ).returncode
            == 0
        )

        leader = t(
            "display-message",
            "-p",
            "-t",
            f"{src}:leader",
            "#{session_id}\t#{window_id}\t#{pane_id}\t#{pid}\t#{socket_path}",
        )
        assert leader.returncode == 0
        _sid, _leader_wid, leader_pane, pid_s, sock_out = (
            leader.stdout or ""
        ).strip().split("\t")
        assert sock_out == sock

        monkeypatch.setenv("TMUX", f"{sock},{pid_s},0")
        monkeypatch.setenv("TMUX_PANE", leader_pane)

        with pytest.raises(TmuxTeamError, match="not in the invoking session"):
            tmux_mod.create_split_team_session(
                session=src,
                tasks=[
                    {
                        "task_id": "w1",
                        "worktree": str(work),
                        "pane_command": "sleep 60",
                        "_env_pairs": [],
                    }
                ],
                env_pairs=[],
                attach_mode="inside",
                root=tmp_path,
                run_id=rid,
                view_mode="dedicated_window",
            )

        # WAL cleared — must not permanently block later starts.
        intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
        assert intents == []

        # Moved worker orphan gone; destination only has the park window.
        dst_wins = t("list-windows", "-t", dst, "-F", "#{window_name}")
        assert dst_wins.returncode == 0
        dst_names = {
            (line or "").strip() for line in (dst_wins.stdout or "").splitlines()
        }
        assert dst_names == {"park"}
        assert not any(n.startswith("omg-team-") for n in dst_names)

        # No intent-nonce residue on the server.
        nonce_scan = t(
            "list-windows",
            "-a",
            "-F",
            "#{window_id}\t#{@omg_intent_nonce}\t#{session_id}",
        )
        assert nonce_scan.returncode == 0
        for line in (nonce_scan.stdout or "").splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[1]:
                pytest.fail(f"intent nonce still present after cleanup: {line!r}")

        # Leader + extra remain in source.
        src_wins = t("list-windows", "-t", src, "-F", "#{window_name}")
        assert src_wins.returncode == 0
        src_names = {
            (line or "").strip() for line in (src_wins.stdout or "").splitlines()
        }
        assert "leader" in src_names
        assert "extra" in src_names

        # Drop the move hook so a subsequent start is not forced to fail again.
        assert t("set-hook", "-gu", "after-new-window").returncode == 0
        # Start gate must be clear (the permanent-block failure mode).
        require_clean_team_launch_intents(tmp_path)
    finally:
        t("kill-server")
        try:
            os.unlink(sock)
        except OSError:
            pass


def test_split_transport_sweep_clears_after_moved_window_and_vanished_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 live: kill WAL $session after moved-@N ACK; sweep must clear + unblock.

    Interleaving: create+bind+nonce ACK, after-new-window moves @W to $D, leave
    the WAL, kill source session while $D/park keeps the server alive. Stale
    sweep must kill the nonce-proven @W, remove the WAL, and leave
    require_clean_team_launch_intents clear (not permanently blocked by
    session-scoped name-proof unknown).
    """
    from omg_cli.team import tmux as tmux_mod
    from omg_cli.team.tmux import (
        require_clean_team_launch_intents,
        sweep_stale_team_launch_intents,
        team_launch_intents_dir,
    )

    sock = f"/tmp/omg-p2-vanish-{os.getpid()}.sock"
    src = "omg-p2-vanish-src"
    dst = "omg-p2-vanish-dst"
    work = tmp_path / "wt"
    work.mkdir()
    rid = "20260806T120000Z-vanish"
    win_name = "omg-team-vanish"

    def t(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", sock, *args],
            check=False,
            capture_output=True,
            text=True,
        )

    try:
        assert (
            t("new-session", "-d", "-s", src, "-n", "leader", "sleep", "60").returncode
            == 0
        )
        assert (
            t("new-window", "-d", "-t", src, "-n", "extra", "sleep", "60").returncode
            == 0
        )
        assert (
            t("new-session", "-d", "-s", dst, "-n", "park", "sleep", "60").returncode
            == 0
        )
        assert (
            t(
                "set-hook", "-g", "after-new-window", f"move-window -t {dst}:"
            ).returncode
            == 0
        )

        leader = t(
            "display-message",
            "-p",
            "-t",
            f"{src}:leader",
            "#{session_id}\t#{window_id}\t#{pane_id}\t#{pid}\t#{socket_path}",
        )
        assert leader.returncode == 0
        sid, leader_wid, leader_pane, pid_s, sock_out = (
            leader.stdout or ""
        ).strip().split("\t")
        assert sock_out == sock
        server = tmux_mod._server_identity_from_create(
            pid=int(pid_s), socket_path=sock
        )

        monkeypatch.setenv("TMUX", f"{sock},{pid_s},0")
        monkeypatch.setenv("TMUX_PANE", leader_pane)

        nonce = "vanishsesslive0000000000000000001"
        intent = tmux_mod.write_team_launch_intent(
            tmp_path,
            run_id=rid,
            session_id=sid,
            window_name=win_name,
            nonce=nonce,
            tmux_server=server,
        )

        window_id, pane_id = tmux_mod._launch_first_inside(
            task={
                "task_id": "w1",
                "worktree": str(work),
                "pane_command": "sleep 60",
                "_env_pairs": [],
            },
            env_pairs=[],
            window_name=win_name,
            target_window=leader_wid,
            expected_session_id=sid,
            intent_path=intent,
            socket_path=sock,
            expected_server=server,
        )

        raw = json.loads(intent.read_text(encoding="utf-8"))
        assert raw["window_id"] == window_id
        assert raw.get("nonce_published") is True
        assert raw.get("side_effect_started") is True

        # Worker lives under destination after the move hook.
        where = t(
            "display-message",
            "-p",
            "-t",
            window_id,
            "#{session_name}\t#{window_id}",
        )
        assert where.returncode == 0
        sess_name, wid_check = (where.stdout or "").strip().split("\t")
        assert wid_check == window_id
        assert sess_name == dst

        # Crash-before-receipt: leave the bound WAL with a dead owner stamp,
        # then kill WAL $session. Destination park keeps the same tmux server.
        raw["owner_pid_start"] = "stale-owner"
        intent.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        assert t("kill-session", "-t", src).returncode == 0
        gone = t("list-windows", "-t", sid, "-F", "#{window_id}")
        assert gone.returncode != 0  # session-scoped list fails (the old wedge)

        # Server still alive via destination.
        alive = t("list-windows", "-t", dst, "-F", "#{window_name}")
        assert alive.returncode == 0
        assert "park" in (alive.stdout or "")

        results = sweep_stale_team_launch_intents(tmp_path)
        assert results and all(r.get("ok") for r in results), results

        # Exact moved @W gone; destination only has park.
        dst_wins = t("list-windows", "-t", dst, "-F", "#{window_id}\t#{window_name}")
        assert dst_wins.returncode == 0
        dst_rows = [
            line.strip() for line in (dst_wins.stdout or "").splitlines() if line.strip()
        ]
        assert all(window_id not in row for row in dst_rows)
        assert not any(win_name in row for row in dst_rows)
        assert any("park" in row for row in dst_rows)

        nonce_scan = t(
            "list-windows",
            "-a",
            "-F",
            "#{window_id}\t#{@omg_intent_nonce}\t#{session_id}",
        )
        assert nonce_scan.returncode == 0
        for line in (nonce_scan.stdout or "").splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[1]:
                pytest.fail(f"intent nonce still present after sweep: {line!r}")

        assert list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json")) == []
        assert t("set-hook", "-gu", "after-new-window").returncode == 0
        require_clean_team_launch_intents(tmp_path)
    finally:
        t("kill-server")
        try:
            os.unlink(sock)
        except OSError:
            pass


def test_intent_nonce_discovery_is_server_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-1b: moved-window nonce on another session still counts as present."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "movedwindow000000000000000000001"

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            assert "-a" in args
            assert "-t" not in args
            if "@omg_intent_nonce" in joined:
                # Original session $42 empty of nonce; window lives under $99.
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"@7\t{nonce}\t$99\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    status, matches, _detail = tmux_mod._discover_inside_windows_by_intent_nonce(
        session_id="$42",
        intent_nonce=nonce,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
    )
    assert status == "found"
    assert matches == ["@7"]


def test_name_discovery_absent_when_wal_session_vanished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: list-windows -a with no WAL-session rows is absent (not unknown)."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            assert "-a" in args
            assert "-t" not in args
            assert "window_name" in joined
            # WAL $42 gone; destination session still has park + moved worker.
            return SimpleNamespace(
                returncode=0,
                stdout="@1\tpark\t$99\n@7\tomg-team-vanished\t$99\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    status, matches, detail = tmux_mod._discover_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-vanished",
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
    )
    assert status == "absent"
    assert matches == []
    assert detail is None


def test_kill_inside_clears_when_wal_session_vanished_after_moved_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: vanished WAL $session must not wedge clear after nonce-proven @N kill."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "vanishsess00000000000000000000001"
    residual = {"@7"}
    kill_calls: list[dict[str, object]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "list-windows":
            assert "-a" in args
            assert "-t" not in args
            if "@omg_intent_nonce" in joined:
                if residual:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"@7\t{nonce}\t$99\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "window_name" in joined:
                # Session-scoped name rows: WAL $42 gone; only $99 remains.
                # Must count as name-absent (not list-windows -t unknown).
                rows = ["@1\tpark\t$99"]
                if residual:
                    rows.append("@7\tomg-team-vanished\t$99")
                return SimpleNamespace(
                    returncode=0,
                    stdout="\n".join(rows) + "\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd in ("if-shell", "kill-window"):
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    def track_kill(wid: str, **kwargs: object) -> str | None:
        kill_calls.append({"wid": wid, **kwargs})
        if kwargs.get("expected_session_id") is not None:
            return (
                f"kill-window {wid}: belongs to session '$99', not WAL session "
                f"{kwargs['expected_session_id']!r} — refuse foreign/restarted @N"
            )
        residual.discard(wid)
        return None

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "_kill_window", track_kill)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-vanished",
        known_window_ids=["@7"],
        require_durable_window_id=True,
        intent_nonce=nonce,
        nonce_published=True,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is None
    assert residual == set()
    assert any(
        c["wid"] == "@7" and c.get("expected_session_id") is None for c in kill_calls
    )


def test_kill_inside_refuses_clear_when_server_swaps_after_absence_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-1c: revalidate PID+start after final probes before authorizing clear."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    nonce = "serverswap0000000000000000000001"
    probes = {"n": 0}

    def fake_probe(*, socket_path: str | None = None):
        probes["n"] += 1
        # Initial check sees expected server; final revalidation sees swap.
        if probes["n"] >= 2:
            return {
                "tmux_socket_path": FAKE_TMUX_SERVER["tmux_socket_path"],
                "tmux_server_pid": 999999,
                "tmux_server_pid_start": "ps:replacement-B",
            }
        return dict(FAKE_TMUX_SERVER)

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "list-windows":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == "if-shell" and "kill-window" in " ".join(args):
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        if cmd == "kill-window":
            return SimpleNamespace(returncode=1, stdout="", stderr="can't find")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "_probe_tmux_server_identity", fake_probe)
    err = tmux_mod._kill_inside_windows_by_name(
        session_id="$42",
        window_name="omg-team-server-swap",
        known_window_ids=[],
        require_durable_window_id=True,
        intent_nonce=nonce,
        nonce_published=True,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    assert err is not None
    assert "after absence proof" in err


def test_respawn_worker_pane_uses_identity_if_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-NEW: relaunch split-window must be PID+start gated."""
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    calls: list[list[str]] = []

    def fake_tmux(args: list[str], *, socket_path: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        assert socket_path == FAKE_TMUX_SERVER["tmux_socket_path"]
        return SimpleNamespace(returncode=0, stdout="%99\n", stderr="")

    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "tmux_available", lambda: True)
    pane = tmux_mod.respawn_worker_pane(
        target="@7",
        worktree="/tmp/w1",
        pane_command="true",
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
        expected_session_id="$7",
        expected_window_id="@7",
    )
    assert pane == "%99"
    assert any(c[0] == "if-shell" and "split-window" in " ".join(c) for c in calls)
    assert not any(c[0] == "split-window" for c in calls)


def test_unmark_side_effect_refuses_when_window_id_bound(
    tmp_path: Path,
) -> None:
    """P2: unmark must not drop durable @N protection."""
    from omg_cli.team.tmux import (
        bind_team_launch_intent_window_id,
        mark_team_launch_intent_side_effect,
        unmark_team_launch_intent_side_effect,
        TmuxTeamError,
    )

    intent = _write_launch_intent(
        tmp_path,
        run_id="20260806T120000Z-nounmark",
        session_id="$42",
        window_name="omg-team-bound",
        nonce="nounmark000000000000000000000001",
    )
    mark_team_launch_intent_side_effect(intent)
    bind_team_launch_intent_window_id(intent, "@55")
    with pytest.raises(TmuxTeamError, match="durable window_id"):
        unmark_team_launch_intent_side_effect(intent)
    raw = json.loads(intent.read_text(encoding="utf-8"))
    assert raw["side_effect_started"] is True
    assert raw["window_id"] == "@55"


def test_launch_intent_stamps_tmux_server_identity(tmp_path: Path) -> None:
    """WAL write must carry socket + server pid start-id before new-window."""
    intent = _write_launch_intent(
        tmp_path,
        run_id="20260806T120000Z-srvstamp",
        session_id="$3",
        window_name="omg-team-srv",
        nonce="srvstamp000000000000000000000001",
    )
    raw = json.loads(intent.read_text(encoding="utf-8"))
    assert raw["tmux_socket_path"] == FAKE_TMUX_SERVER["tmux_socket_path"]
    assert raw["tmux_server_pid"] == FAKE_TMUX_SERVER["tmux_server_pid"]
    assert raw["tmux_server_pid_start"] == FAKE_TMUX_SERVER["tmux_server_pid_start"]
    assert raw["side_effect_started"] is False


def test_clear_intent_fsync_failure_after_unlink_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Parent fsync failure after unlink must not raise (WAL already gone)."""
    from omg_cli.team.tmux import clear_team_launch_intent

    intent = _write_launch_intent(
        tmp_path,
        run_id="20260806T120000Z-fsync",
        session_id="$7",
        window_name="omg-team-fsync",
        nonce="ffffffffffffffffffffffffffffffff",
    )
    assert intent.is_file()

    def boom_fsync(_fd: int) -> None:
        raise OSError("injected parent fsync failure")

    monkeypatch.setattr(os, "fsync", boom_fsync)
    clear_team_launch_intent(intent)  # must not raise
    assert not intent.is_file()


def test_sweep_adopts_receipt_bound_intent_without_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Authoritative v2 receipt with matching intent identity → clear WAL, no kill."""
    from omg_cli.team.tmux import (
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-adopt"
    nonce = "cafebabecafebabecafebabecafebabe"
    window_name = "omg-team-worker"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name=window_name,
        nonce=nonce,
    )
    _authoritative_v2_launch_receipt_fixture(
        tmp_path,
        run_id=rid,
        session_id="$42",
        intent_nonce=nonce,
        window_name=window_name,
    )
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    kills: list[dict[str, str]] = []

    def boom_kill(**kwargs):
        kills.append(dict(kwargs))
        return "should not kill"

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is True
    assert results[0].get("adopted") is True
    assert kills == []
    assert not intent.is_file()


def test_sweep_refuses_receipt_only_without_team_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Receipt without team.json must not adopt/clear intent (crash window)."""
    from omg_cli.team import plane
    from omg_cli.team.tmux import (
        _intent_receipt_matches,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-receiptonly"
    nonce = "cafebabecafebabecafebabecafebabe"
    window_name = "omg-team-worker"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name=window_name,
        nonce=nonce,
    )
    tasks = [
        {
            "task_id": "t1",
            "window_index": 0,
            "pane_id": "%10",
            "pid": 10001,
            "pgid": 20001,
            "pid_start": "start-10001",
            "status": "running",
        }
    ]
    plane._persist_team_launch_receipt(
        tmp_path,
        rid,
        session="omg-workers",
        session_id="$42",
        launch_nonce="a" * 32,
        tasks=tasks,
        intent_nonce=nonce,
        window_name=window_name,
    )
    assert plane.team_launch_receipt_path(tmp_path, rid).is_file()
    assert not plane.team_meta_path(tmp_path, rid).exists()

    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    assert _intent_receipt_matches(tmp_path, raw) is False

    kills: list[dict[str, str]] = []

    def boom_kill(**kwargs):
        kills.append(dict(kwargs))
        return "should not kill"

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert "unbound" in str(results[0].get("error"))
    assert kills == []
    assert intent.is_file()


def test_sweep_refuses_forged_minimal_matching_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Copyable-field stub receipt must not adopt or clear intent (no kill)."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane
    from omg_cli.team.tmux import (
        _intent_receipt_matches,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-forged"
    nonce = "fefefefefefefefefefefefefefefefe"
    window_name = "omg-team-worker"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name=window_name,
        nonce=nonce,
    )
    # Minimal matching stub — lacks exact key set / hash / tasks authority.
    plane._atomic_write_json(
        plane.team_launch_receipt_path(tmp_path, rid),
        {
            "store_kind": "team_launch_receipt",
            "schema_version": plane.LAUNCH_RECEIPT_SCHEMA_VERSION,
            "writer": CLI_WRITER,
            "run_id": rid,
            "session_id": "$42",
            "launch_nonce": "a" * 32,
            "intent_nonce": nonce,
            "window_name": window_name,
        },
    )
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    assert _intent_receipt_matches(tmp_path, raw) is False

    kills: list[dict[str, str]] = []

    def boom_kill(**kwargs):
        kills.append(dict(kwargs))
        return "should not kill"

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert "unbound" in str(results[0].get("error"))
    assert kills == []
    assert intent.is_file()


def test_sweep_refuses_unbound_receipt_for_new_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Old receipt without matching intent identity must not adopt a new WAL."""
    from omg_cli.team.tmux import (
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-orphan"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name="omg-team-new",
        nonce="dddddddddddddddddddddddddddddddd",
    )
    _authoritative_v2_launch_receipt_fixture(
        tmp_path,
        run_id=rid,
        session_id="$42",
        intent_nonce="oldoldoldoldoldoldoldoldoldoldold",
        window_name="omg-team-old",
    )
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    kills: list[dict[str, str]] = []

    def boom_kill(**kwargs):
        kills.append(dict(kwargs))
        return "should not kill"

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert "unbound" in str(results[0].get("error"))
    assert kills == []
    assert intent.is_file()


def test_sweep_refuses_schema_v1_receipt_for_intent_adoption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Legacy v1 launch receipts cannot prove intent identity — never adopt."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane
    from omg_cli.team.tmux import (
        _intent_receipt_matches,
        sweep_stale_team_launch_intents,
    )

    rid = "20260806T120000Z-v1legacy"
    nonce = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    window_name = "omg-team-worker"
    intent = _write_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name=window_name,
        nonce=nonce,
    )
    # Exact #106 key set — no intent_nonce / window_name.
    plane._atomic_write_json(
        plane.team_launch_receipt_path(tmp_path, rid),
        {
            "store_kind": "team_launch_receipt",
            "schema_version": plane.LEGACY_LAUNCH_RECEIPT_SCHEMA_VERSION,
            "writer": CLI_WRITER,
            "run_id": rid,
            "session_name": "omg-workers",
            "session_id": "$42",
            "launch_nonce": "a" * 32,
            "generation": 0,
            "previous_receipt_sha256": None,
            "tasks": [],
        },
    )
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    intent.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    assert _intent_receipt_matches(tmp_path, raw) is False

    kills: list[dict[str, str]] = []

    def boom_kill(**kwargs):
        kills.append(dict(kwargs))
        return "should not kill"

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert kills == []
    assert intent.is_file()


def test_sweep_refuses_kill_when_intent_owner_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In-flight launch intent with live owner must not be kill-swept."""
    from omg_cli.team.tmux import (
        sweep_stale_team_launch_intents,
    )

    intent = _write_launch_intent(
        tmp_path,
        run_id="20260806T120000Z-live",
        session_id="$7",
        window_name="omg-team-inflight",
        nonce="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    kills: list[dict[str, str]] = []

    def boom_kill(**kwargs):
        kills.append(dict(kwargs))
        return "should not kill"

    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name", boom_kill
    )
    results = sweep_stale_team_launch_intents(tmp_path)
    assert results and results[0].get("ok") is False
    assert "in-flight" in str(results[0].get("error"))
    assert kills == []
    assert intent.is_file()


def test_start_team_sweeps_all_run_ids_before_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Project-wide sweep runs before create_run (not only the new rid)."""
    import subprocess

    from omg_cli.team import plane

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    old = _write_launch_intent(
        tmp_path,
        run_id="20260806T110000Z-prior",
        session_id="$1",
        window_name="omg-team-prior",
        nonce="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    assert old.is_file()
    raw = json.loads(old.read_text(encoding="utf-8"))
    raw["owner_pid"] = 999999999
    raw["owner_pid_start"] = "stale-owner"
    old.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    swept: list[dict[str, object]] = []

    def capture_require(root):
        from omg_cli.team.tmux import TmuxTeamError, sweep_stale_team_launch_intents

        # Must see the prior-run intent (run_id=None scan).
        results = sweep_stale_team_launch_intents(root, run_id=None)
        swept.extend(results)
        failures = [r for r in results if not r.get("ok")]
        if failures:
            raise TmuxTeamError("stale team launch intents not proven cleaned")
        return results

    monkeypatch.setenv("OMG_EXPERIMENTAL_TMUX_TEAM", "1")
    monkeypatch.setattr(
        "omg_cli.team.tmux.require_clean_team_launch_intents", capture_require
    )
    monkeypatch.setattr(
        "omg_cli.team.tmux._kill_inside_windows_by_name",
        lambda **_kw: None,  # absence proven
    )
    # Stop after sweep: assert sweep saw prior-run intent.
    monkeypatch.setattr(
        plane,
        "create_run",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop after sweep")),
    )

    with pytest.raises(plane.TeamError, match="stop after sweep"):
        plane.start_team(
            "sweep-all",
            [{"task_id": "t1", "title": "one", "owned_files": ["README.md"]}],
            root=tmp_path,
            dry_run=False,
            detach=True,
            check_binary=False,
            executor="fixture",
        )
    assert swept
    assert any("20260806T110000Z-prior" in str(r.get("path")) for r in swept)
    assert not old.is_file()


def test_team_status_prefers_exact_pane_alive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status uses pane+session+nonce identity; never logical window_index."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane

    rid = "20260806T000000Z-status98"
    meta = {
        "run_id": rid,
        "session": "team-sess",
        "launch_nonce": "nonce-abc",
        "dry_run": False,
        "workspace_mode": "worktree",
        "writer": CLI_WRITER,
        "tasks": [
            {
                "task_id": "w1",
                "window_index": 0,
                "worktree": str(tmp_path / "w1"),
                "status": "running",
                "pid": 1234,
                "pid_start": "ps:start-w1",
                "pane_id": "%81",
            },
            {
                "task_id": "w2",
                "window_index": 1,
                "worktree": str(tmp_path / "w2"),
                "status": "running",
                "pid": 1235,
                "pid_start": "ps:start-w2",
                "pane_id": "%82",
            },
            {
                "task_id": "w3-legacy",
                "window_index": 0,
                "worktree": str(tmp_path / "w3"),
                "status": "running",
                "pid": 99,
                # missing pane_id → must be dead, not window_index guess
            },
        ],
    }
    plane._atomic_write_json(plane.team_meta_path(tmp_path, rid), meta)
    plane._atomic_write_json(
        plane.team_launch_receipt_path(tmp_path, rid),
        {
            "writer": CLI_WRITER,
            "session": "team-sess",
            "session_id": "$42",
            "launch_nonce": "nonce-abc",
        },
    )

    def fake_probe(pane_id: str):
        if pane_id == "%81":
            return {
                "pane_id": "%81",
                "dead": False,
                "session_id": "$42",
                "pane_pid": 1234,
            }
        if pane_id == "%82":
            # Same pane id still "alive" but PID start will mismatch → dead
            return {
                "pane_id": "%82",
                "dead": False,
                "session_id": "$42",
                "pane_pid": 9999,
            }
        return None

    def boom_window(_session: str, _widx: int) -> bool | None:
        raise AssertionError("_window_alive must not run for status liveness")

    monkeypatch.setattr("omg_cli.team.tmux.probe_worker_pane_identity", fake_probe)
    monkeypatch.setattr(
        plane,
        "_probe_tmux_launch_nonce_for_pane",
        lambda _pane, _s, **_kw: ("nonce-abc", True),
    )
    monkeypatch.setattr(
        plane,
        "_pid_start_identity",
        lambda pid: "ps:start-w1" if pid == 1234 else "ps:replaced",
    )
    monkeypatch.setattr(plane, "_window_alive", boom_window)
    monkeypatch.setattr(plane, "tmux_available", lambda: True)

    st = plane.team_status(tmp_path, rid)
    by_id = {t["task_id"]: t["alive"] for t in st["tasks"]}
    assert by_id == {"w1": True, "w2": False, "w3-legacy": False}


def test_team_status_same_start_id_different_pid_is_not_alive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pid_start collision across PIDs must not false-alive a respawn (AND gate)."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane

    rid = "20260806T000000Z-status-start-collide"
    meta = {
        "run_id": rid,
        "session": "team-sess",
        "launch_nonce": "nonce-abc",
        "dry_run": False,
        "workspace_mode": "worktree",
        "writer": CLI_WRITER,
        "tasks": [
            {
                "task_id": "w1",
                "window_index": 0,
                "worktree": str(tmp_path / "w1"),
                "status": "running",
                "pid": 1111,
                "pid_start": "collide-start",
                "pane_id": "%81",
            }
        ],
    }
    plane._atomic_write_json(plane.team_meta_path(tmp_path, rid), meta)
    plane._atomic_write_json(
        plane.team_launch_receipt_path(tmp_path, rid),
        {
            "writer": CLI_WRITER,
            "session": "team-sess",
            "session_id": "$42",
            "launch_nonce": "nonce-abc",
        },
    )

    def fake_probe(pane_id: str):
        assert pane_id == "%81"
        return {
            "pane_id": "%81",
            "dead": False,
            "session_id": "$42",
            # Respawned foreign PID that collides on start-id.
            "pane_pid": 2222,
        }

    monkeypatch.setattr("omg_cli.team.tmux.probe_worker_pane_identity", fake_probe)
    monkeypatch.setattr(
        plane,
        "_probe_tmux_launch_nonce_for_pane",
        lambda _pane, _s, **_kw: ("nonce-abc", True),
    )
    # Same start-id for any PID — models clock-resolution collision.
    monkeypatch.setattr(plane, "_pid_start_identity", lambda _pid: "collide-start")
    monkeypatch.setattr(plane, "tmux_available", lambda: True)

    st = plane.team_status(tmp_path, rid)
    assert st["tasks"][0]["alive"] is False
    assert plane._status_worker_alive(
        pane_id="%81",
        session="team-sess",
        expected_session_id="$42",
        launch_nonce="nonce-abc",
        expected_pid_start="collide-start",
        expected_pid=1111,
    ) is False


def test_team_status_probe_oserror_is_fail_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane

    rid = "20260806T000000Z-status-os"
    meta = {
        "run_id": rid,
        "session": "team-sess",
        "launch_nonce": "nonce-abc",
        "dry_run": False,
        "workspace_mode": "worktree",
        "writer": CLI_WRITER,
        "tasks": [
            {
                "task_id": "w1",
                "window_index": 0,
                "worktree": str(tmp_path / "w1"),
                "status": "running",
                "pid": 1,
                "pane_id": "%81",
            }
        ],
    }
    plane._atomic_write_json(plane.team_meta_path(tmp_path, rid), meta)
    plane._atomic_write_json(
        plane.team_launch_receipt_path(tmp_path, rid),
        {
            "writer": CLI_WRITER,
            "session": "team-sess",
            "session_id": "$42",
            "launch_nonce": "nonce-abc",
        },
    )

    def boom_probe(_pane: str):
        raise OSError("tmux missing")

    monkeypatch.setattr("omg_cli.team.tmux.probe_worker_pane_identity", boom_probe)
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    st = plane.team_status(tmp_path, rid)
    assert st["tasks"][0]["alive"] is False


@pytest.mark.parametrize(
    "pane_nonce_behavior",
    ["oserror", "nonzero", "malformed", "missing"],
)
def test_team_status_pane_nonce_read_fail_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch, pane_nonce_behavior: str
) -> None:
    """Pane nonce OSError / non-zero / malformed must not adopt session nonce."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane

    rid = f"20260806T000000Z-nonce-{pane_nonce_behavior}"
    nonce = "a" * 32
    meta = {
        "run_id": rid,
        "session": "team-sess",
        "launch_nonce": nonce,
        "dry_run": False,
        "workspace_mode": "worktree",
        "writer": CLI_WRITER,
        "tasks": [
            {
                "task_id": "w1",
                "window_index": 0,
                "worktree": str(tmp_path / "w1"),
                "status": "running",
                "pid": 1234,
                "pid_start": "ps:start-w1",
                "pane_id": "%81",
            }
        ],
    }
    plane._atomic_write_json(plane.team_meta_path(tmp_path, rid), meta)
    plane._atomic_write_json(
        plane.team_launch_receipt_path(tmp_path, rid),
        {
            "writer": CLI_WRITER,
            "session": "team-sess",
            "session_id": "$42",
            "launch_nonce": nonce,
        },
    )

    def fake_probe(pane_id: str):
        return {
            "pane_id": pane_id,
            "dead": False,
            "session_id": "$42",
            "pane_pid": 1234,
        }

    def fake_tmux(args, **_kw):
        from unittest.mock import MagicMock

        cmd = list(args)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if cmd[:1] == ["show-options"] and "-p" in cmd:
            if pane_nonce_behavior == "oserror":
                raise OSError("pane options unavailable")
            if pane_nonce_behavior == "nonzero":
                result.returncode = 1
                return result
            if pane_nonce_behavior == "malformed":
                result.stdout = "not-a-nonce\n"
                return result
            result.stdout = "\n"
            return result
        if cmd[:1] == ["show-options"]:
            # Session nonce still matches — must not rescue status.
            result.stdout = nonce + "\n"
            return result
        return result

    monkeypatch.setattr("omg_cli.team.tmux.probe_worker_pane_identity", fake_probe)
    monkeypatch.setattr(plane, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        plane, "_pid_start_identity", lambda pid: "ps:start-w1" if pid == 1234 else None
    )
    monkeypatch.setattr(plane, "tmux_available", lambda: True)

    st = plane.team_status(tmp_path, rid)
    assert st["tasks"][0]["alive"] is False


def test_bind_launch_nonce_skips_session_when_not_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team import plane

    calls: list[list[str]] = []

    def fake_tmux(args, **_kw):
        from unittest.mock import MagicMock

        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plane, "_tmux_run", fake_tmux)
    plane._bind_tmux_launch_nonce(
        session_id="$9",
        launch_nonce="c" * 32,
        window_id="@3",
        pane_ids=["%81"],
        session_owned=False,
    )
    assert any(c[:2] == ["set-option", "-p"] for c in calls)
    assert any(c[:2] == ["set-option", "-w"] for c in calls)
    assert not any(
        c[:1] == ["set-option"] and "-p" not in c and "-w" not in c for c in calls
    )


def test_bind_launch_nonce_uses_identity_if_shell_when_server_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: set-option must be if-shell gated so replacement B cannot keep nonce."""
    from omg_cli.team import plane
    from omg_cli.team import tmux as tmux_mod

    calls: list[list[str]] = []

    def fake_tmux(args, **_kw):
        from unittest.mock import MagicMock

        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plane, "_tmux_run", fake_tmux)
    monkeypatch.setattr(tmux_mod, "_tmux_run", fake_tmux)
    monkeypatch.setattr(
        plane,
        "_require_plane_tmux_server",
        lambda *_a, **_k: dict(FAKE_TMUX_SERVER),
    )
    plane._bind_tmux_launch_nonce(
        session_id="$9",
        launch_nonce="e" * 32,
        window_id="@3",
        pane_ids=["%81"],
        session_owned=True,
        socket_path=FAKE_TMUX_SERVER["tmux_socket_path"],
        expected_server=FAKE_TMUX_SERVER,
    )
    if_shells = [c for c in calls if c and c[0] == "if-shell"]
    assert len(if_shells) >= 3  # pane + window + session
    joined = "\n".join(" ".join(c) for c in if_shells)
    assert "set-option -p -t %81" in joined
    assert "set-option -w -t @3" in joined
    assert "set-option -t $9" in joined
    assert "424242" in joined or "ps:omg-test-server" in joined
    # No bare set-option mutation outside if-shell.
    assert not any(c[0] == "set-option" for c in calls)


def test_resolve_live_signal_target_refuses_respawn_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team import plane

    receipt = {"session_id": "$9", "launch_nonce": "d" * 32}
    row = {
        "task_id": "t1",
        "window_index": 0,
        "pane_id": "%81",
        "pid": 100,
        "pgid": 100,
        "pid_start": "start-100",
    }

    def fake_tmux(args, **_kw):
        from unittest.mock import MagicMock

        cmd = list(args)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if cmd[0] == "display-message" and "#{session_name}" in str(cmd[-1]):
            result.stdout = "sess\t$9\n"
        elif cmd[0] == "display-message":
            # Respawned pane: same %81, new pid.
            result.stdout = "%81\t999\t$9\t@1\n"
        elif cmd[0] == "show-options":
            result.stdout = "d" * 32 + "\n"
        return result

    monkeypatch.setattr(plane, "_tmux_run", fake_tmux)
    monkeypatch.setattr(plane, "_read_tmux_session_identity", lambda s: (s, "$9"))
    monkeypatch.setattr(plane, "_pgid_for_pid", lambda pid: pid)
    monkeypatch.setattr(plane, "_pid_start_identity", lambda pid: f"start-{pid}")

    assert (
        plane._resolve_live_signal_target(
            "sess", receipt, row, session_owned=True, window_id=None
        )
        is None
    )