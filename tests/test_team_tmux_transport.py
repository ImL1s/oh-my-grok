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
        for task in meta.get("tasks") or []:
            cmd = str(task.get("pane_command") or "")
            assert "team_worker_fixture" in cmd
            assert "grok" not in cmd.split()

        session = str(meta.get("session") or "")
        run_id = str(meta["run_id"])
        assert session
        assert _pane_count(session) == 2
        expected_worktrees = {
            Path(str(task["worktree"])).resolve() for task in meta.get("tasks") or []
        }
        assert _pane_current_paths(session) == expected_worktrees
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
    """When TMUX is set, create uses new-window + split-window (not new-session).

    Launch binds TMUX_PANE, creates workers with -d, and restores leader focus.
    """
    from types import SimpleNamespace

    from omg_cli.team import tmux as tmux_mod

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    calls: list[list[str]] = []

    def fake_tmux(args: list[str]) -> SimpleNamespace:
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
                return SimpleNamespace(returncode=0, stdout="$42\t@7\n", stderr="")
            if target in {"%10", "%11"} and "#{session_id}" in joined:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{target}\t$42\t@7\n",
                    stderr="",
                )
        if cmd == "new-window":
            assert "-d" in args
            assert "-t" in args and args[args.index("-t") + 1] == "@3"
            return SimpleNamespace(returncode=0, stdout="@7\t%10\n", stderr="")
        if cmd == "split-window":
            assert "-d" in args
            return SimpleNamespace(returncode=0, stdout="%11\n", stderr="")
        if cmd == "select-layout":
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
    )
    assert handle == ("leader", "$42")
    assert tasks[0]["pane_id"] == "%10"
    assert tasks[1]["pane_id"] == "%11"
    assert tasks[0]["_tmux_launch"]["attach_mode"] == "inside"
    assert tasks[0]["_tmux_launch"]["session_owned"] is False
    assert tasks[0]["_tmux_launch"]["leader_pane_id"] == "%9"
    assert tasks[0]["_tmux_launch"]["window_id"] == "@7"
    assert tasks[0]["_tmux_launch"]["attach_hint"] == "tmux select-pane -t %9"
    assert any(c[0] == "new-window" and "-d" in c for c in calls)
    assert any(c[0] == "split-window" and "-d" in c for c in calls)
    assert any(c == ["select-pane", "-t", "%9"] for c in calls)
    assert not any(c[0] == "new-session" for c in calls)
    assert not any(c[0] == "kill-session" for c in calls)
    # No untargeted display-message (would retarget current client).
    assert not any(
        c[0] == "display-message" and "-t" not in c for c in calls
    )


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

    def fake_tmux(args: list[str]) -> SimpleNamespace:
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
                return SimpleNamespace(returncode=0, stdout="$42\t@7\n", stderr="")
            if target == "%10" and "#{session_id}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="%10\t$99\t@7\n", stderr=""
                )
        if cmd == "new-window":
            return SimpleNamespace(returncode=0, stdout="@7\t%10\n", stderr="")
        if cmd == "list-windows":
            if window_alive:
                return SimpleNamespace(
                    returncode=0,
                    stdout="@7\tomg-team-deadbeef\t$42\n",
                    stderr="",
                )
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

    def fake_tmux(args: list[str]) -> SimpleNamespace:
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

    def fake_tmux(args: list[str]) -> SimpleNamespace:
        nonlocal listed_windows
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
        if cmd == "new-window":
            # Side effect succeeded; result publication failed.
            return SimpleNamespace(returncode=0, stdout="\n", stderr="")
        if cmd == "list-windows":
            listed_windows += 1
            assert args[args.index("-t") + 1] == "$42"
            if residual:
                return SimpleNamespace(
                    returncode=0,
                    stdout="@77\tomg-team-deadbeef\t$42\n",
                    stderr="",
                )
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

    def fake_tmux(args: list[str]) -> SimpleNamespace:
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

    def fake_tmux(args: list[str]) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "list-windows":
            return SimpleNamespace(
                returncode=0,
                stdout="@77\tomg-team-stillhere\t$42\n",
                stderr="",
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

    def fake_tmux(args: list[str]) -> SimpleNamespace:
        cmd = args[0]
        if cmd == "list-windows":
            if residual:
                return SimpleNamespace(
                    returncode=0,
                    stdout="@77\tomg-team-gone\t$42\n",
                    stderr="",
                )
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

    def fake_tmux(args: list[str]) -> SimpleNamespace:
        cmd = args[0]
        joined = " ".join(args)
        if cmd == "display-message" and "-t" in args:
            target = args[args.index("-t") + 1]
            if target == "%9" and "#{pane_pid}" in joined:
                return SimpleNamespace(
                    returncode=0, stdout="leader\t$42\t@3\t%9\t4242\n", stderr=""
                )
        if cmd == "new-window":
            # Intent must already exist before side effect.
            intents = list(team_launch_intents_dir(tmp_path).glob(f"{rid}-*.json"))
            assert len(intents) == 1
            intent_seen.extend(intents)
            return SimpleNamespace(returncode=0, stdout="\n", stderr="")
        if cmd == "list-windows":
            if residual:
                return SimpleNamespace(
                    returncode=0,
                    stdout="@77\tomg-team-abad1dea\t$42\n",
                    stderr="",
                )
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
        write_team_launch_intent,
    )

    intent = write_team_launch_intent(
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


def test_sweep_adopts_receipt_bound_intent_without_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Durable receipt with matching intent identity → clear WAL, no kill."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane
    from omg_cli.team.tmux import (
        sweep_stale_team_launch_intents,
        write_team_launch_intent,
    )

    rid = "20260806T120000Z-adopt"
    nonce = "cafebabecafebabecafebabecafebabe"
    window_name = "omg-team-worker"
    intent = write_team_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name=window_name,
        nonce=nonce,
    )
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


def test_sweep_refuses_unbound_receipt_for_new_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Old receipt without matching intent identity must not adopt a new WAL."""
    from omg_cli.evidence import CLI_WRITER
    from omg_cli.team import plane
    from omg_cli.team.tmux import (
        sweep_stale_team_launch_intents,
        write_team_launch_intent,
    )

    rid = "20260806T120000Z-orphan"
    intent = write_team_launch_intent(
        tmp_path,
        run_id=rid,
        session_id="$42",
        window_name="omg-team-new",
        nonce="dddddddddddddddddddddddddddddddd",
    )
    plane._atomic_write_json(
        plane.team_launch_receipt_path(tmp_path, rid),
        {
            "store_kind": "team_launch_receipt",
            "schema_version": plane.LAUNCH_RECEIPT_SCHEMA_VERSION,
            "writer": CLI_WRITER,
            "run_id": rid,
            "session_id": "$42",
            "launch_nonce": "a" * 32,
            "intent_nonce": "oldoldoldoldoldoldoldoldoldoldold",
            "window_name": "omg-team-old",
        },
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
        write_team_launch_intent,
    )

    rid = "20260806T120000Z-v1legacy"
    nonce = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    window_name = "omg-team-worker"
    intent = write_team_launch_intent(
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
        write_team_launch_intent,
    )

    intent = write_team_launch_intent(
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
    from omg_cli.team.tmux import write_team_launch_intent

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

    old = write_team_launch_intent(
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