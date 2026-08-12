"""#104 Layer B: hermetic real-tmux Team UX regression.

Requires real ``tmux`` on PATH. Uses an isolated ``-S`` socket only.
Never needs Grok/Codex credentials. Marked ``tmux`` + ``tmux_real``.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from omg_cli.madmax import tmux_available
from omg_cli.team.plane import EXPERIMENTAL_ENV, TeamError, stop_team, team_status
from tests.support.team_tmux_harness import (
    ARTIFACT_ROOT,
    FailpointError,
    FailureInjector,
    IsolatedTmuxServer,
    LeaderSession,
    failpoint_in_chain,
    init_git_repo,
    install_fixture_provider,
    launch_team_inside,
    provider_script,
    wait_until,
)

pytestmark = [pytest.mark.tmux, pytest.mark.tmux_real]


@pytest.fixture(autouse=True)
def _require_real_tmux() -> None:
    if not tmux_available() and shutil.which("tmux") is None:
        pytest.skip("tmux not available")


@pytest.fixture
def tmux_server(request: pytest.FixtureRequest):
    with IsolatedTmuxServer(prefix="omg104") as server:
        try:
            yield server
        finally:
            # Pytest does not re-raise test failures into the fixture after
            # yield, so dump unconditionally (bounded) for CI upload-artifact.
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
    monkeypatch.setenv("OMG_TEAM_FIXTURE_HOLD_S", "25")
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "25000")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_HOLD_S", "25")


def _assert_no_running_team(repo: Path) -> None:
    for path in repo.joinpath(".omg").rglob("team.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            assert data.get("startup_status") != "running", path


# ---------------------------------------------------------------------------
# Harness self-check (artifact upload contract)
# ---------------------------------------------------------------------------


def test_artifact_dump_writes_nonempty_tree(
    tmux_server: IsolatedTmuxServer,
) -> None:
    """Prove dump_artifacts populates ARTIFACT_ROOT for CI upload-artifact."""
    leader = _leader(tmux_server)
    assert leader.leader is not None
    dest = ARTIFACT_ROOT / f"selfcheck-{os.getpid()}"
    out = tmux_server.dump_artifacts(reason="selfcheck", dest=dest)
    assert out == dest
    assert (dest / "DUMP_OK").is_file()
    assert (dest / "reason.txt").read_text(encoding="utf-8").startswith("selfcheck")
    assert (dest / "sessions.txt").stat().st_size > 0
    assert (dest / "panes.txt").stat().st_size > 0
    assert leader.leader.session_name in (dest / "sessions.txt").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# A — Leader visibility / same-window focus (#96/#97)
# ---------------------------------------------------------------------------


def test_same_window_launch_keeps_leader_visible_and_focused(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    foreign = leader.create_foreign_window(name="keep-me")
    before = leader.leader
    assert before is not None
    leader.select_window(before.window_id)
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))

    meta = launch_team_inside(root=repo, leader=leader, workers=2)
    try:
        assert meta.get("attach_mode") == "inside"
        assert meta.get("view_mode") == "same_window" or (
            (meta.get("tasks") or [{}])[0].get("view_mode") == "same_window"
            or meta.get("tmux_topology", {}).get("view_mode") == "same_window"
        )
        topo = leader.capture_topology()
        leader_panes = [p for p in topo.panes if p.window_id == before.window_id]
        assert len(leader_panes) == 3, topo.pane_ids
        live_leader = next(p for p in leader_panes if p.pane_id == before.pane_id)
        assert live_leader.pane_pid == before.pane_pid
        assert live_leader.pane_dead is False
        # Session-visible leader window + active pane (not window-local only).
        assert leader.window_is_session_active(before.window_id) is True
        leader.assert_leader_operator_visible(before)
        foreign_alive = tmux_server.tmux(
            "display-message",
            "-p",
            "-t",
            foreign.pane_id,
            "#{pane_id}\t#{pane_dead}",
        )
        assert foreign_alive.returncode == 0
        assert (foreign_alive.stdout or "").startswith(f"{foreign.pane_id}\t0")
        assert meta.get("startup_status") == "running"
        assert int(meta.get("startup_process_ready") or 0) == 2
    finally:
        stop_team(repo, meta["run_id"])


def test_leader_visibility_assertion_fails_when_foreign_window_selected(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative self-check: window-local pane_active must not satisfy visibility.

    After switching to a foreign window, ``capture_focus(leader_window)`` still
    returns the leader pane, but ``assert_leader_operator_visible`` must fail
    because ``window_active != 1`` (#104 B1 mutation).
    """
    leader = _leader(tmux_server)
    foreign = leader.create_foreign_window(name="keep-me")
    before = leader.leader
    assert before is not None
    leader.select_window(before.window_id)
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))

    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    try:
        leader.assert_leader_operator_visible(before)
        leader.select_window(foreign.window_id)
        # Hollow old check still "passes":
        assert leader.capture_focus(before.window_id) == before.pane_id
        assert leader.window_is_session_active(before.window_id) is False
        assert leader.window_is_session_active(foreign.window_id) is True
        with pytest.raises(AssertionError, match="not session-visible"):
            leader.assert_leader_operator_visible(before)
    finally:
        stop_team(repo, meta["run_id"])


def test_same_window_cli_launch_from_leader_tmux_env(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
) -> None:
    """Out-of-process launch_team driver with controlled TMUX/TMUX_PANE."""
    from tests.support.team_tmux_harness import run_omg_team_launch

    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
    leader.select_window(before.window_id)
    env = {
        EXPERIMENTAL_ENV: "1",
        "OMG_TEAM_FIXTURE_HOLD_S": "20",
        "OMG_TEAM_READY_TIMEOUT_MS": "20000",
    }
    proc = run_omg_team_launch(
        root=repo, leader=leader, workers=2, env=env, timeout_s=90.0
    )
    run_id = None
    try:
        assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
        text = proc.stdout or ""
        meta: dict[str, Any] | None = None
        if "{" in text:
            start = text.find("{")
            end = text.rfind("}")
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and "run_id" in parsed:
                meta = parsed
        if meta is None:
            runs = list((repo / ".omg" / "state" / "runs").glob("*/team/team.json"))
            assert runs, (proc.stdout, proc.stderr)
            meta = json.loads(runs[0].read_text(encoding="utf-8"))
        run_id = str(meta["run_id"])
        topo = leader.capture_topology()
        assert before.pane_id in topo.pane_ids
        assert sum(1 for p in topo.panes if p.window_id == before.window_id) == 3
        leader.assert_leader_operator_visible(before)
    finally:
        if run_id:
            stop_team(repo, run_id)


# ---------------------------------------------------------------------------
# B — Invocation focus race (#97)
# ---------------------------------------------------------------------------


def test_foreign_client_window_switch_does_not_redirect_launch(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second attached client switches away; launch still binds TMUX_PANE window."""
    leader = _leader(tmux_server)
    foreign = leader.create_foreign_window(name="distractor")
    before = leader.leader
    assert before is not None
    leader.select_window(before.window_id)
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))

    client = leader.attach_second_client()
    try:
        client.select_window(foreign.window_id)
        wait_until(
            lambda: leader.window_is_session_active(foreign.window_id),
            timeout_s=3.0,
            label="foreign window session-active",
        )
        assert leader.window_is_session_active(before.window_id) is False

        meta = launch_team_inside(root=repo, leader=leader, workers=2)
        try:
            topo = leader.capture_topology()
            leader_window_panes = [
                p for p in topo.panes if p.window_id == before.window_id
            ]
            assert len(leader_window_panes) == 3
            assert before.pane_id in {p.pane_id for p in leader_window_panes}
            foreign_panes = [
                p for p in topo.panes if p.window_id == foreign.window_id
            ]
            assert len(foreign_panes) == 1
            assert foreign_panes[0].pane_id == foreign.pane_id
            leader.assert_leader_operator_visible(before)
        finally:
            stop_team(repo, meta["run_id"])
    finally:
        client.close()


def test_pre_side_effect_failpoint_creates_zero_workers(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
    foreign = leader.create_foreign_session()
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))

    inj = FailureInjector()
    inj.arm("pre_side_effect")
    inj.install(monkeypatch)

    with pytest.raises((FailpointError, TeamError)) as ei:
        launch_team_inside(root=repo, leader=leader, workers=2)

    assert "pre_side_effect" in inj.fired
    assert failpoint_in_chain(ei.value, "pre_side_effect") or isinstance(
        ei.value, FailpointError
    )

    topo = leader.capture_topology()
    assert topo.pane_ids == (before.pane_id,)
    alive = tmux_server.tmux(
        "display-message", "-p", "-t", foreign.pane_id, "#{pane_dead}"
    )
    assert alive.returncode == 0
    assert (alive.stdout or "").strip() == "0"


# ---------------------------------------------------------------------------
# C — Exact status / liveness (#98)
# ---------------------------------------------------------------------------


def test_one_worker_death_does_not_kill_sibling_or_leader(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))

    meta = launch_team_inside(root=repo, leader=leader, workers=2)
    try:
        tasks = list(meta.get("tasks") or [])
        assert len(tasks) == 2
        victim_id = str(tasks[0]["task_id"])
        sibling_id = str(tasks[1]["task_id"])
        victim = str(tasks[0]["pane_id"])
        sibling = str(tasks[1]["pane_id"])
        leader.kill_pane(victim)

        def _victim_gone() -> bool:
            proc = tmux_server.tmux(
                "display-message",
                "-p",
                "-t",
                victim,
                "#{pane_id}\t#{pane_dead}",
            )
            if proc.returncode != 0:
                return True
            out = (proc.stdout or "").strip()
            return out.endswith("\t1") or not out.startswith(victim)

        wait_until(_victim_gone, timeout_s=5.0, label="victim pane gone")
        sib = tmux_server.tmux(
            "display-message",
            "-p",
            "-t",
            sibling,
            "#{pane_id}\t#{pane_dead}",
        )
        assert sib.returncode == 0
        assert (sib.stdout or "").startswith(f"{sibling}\t0")
        lead = tmux_server.tmux(
            "display-message",
            "-p",
            "-t",
            before.pane_id,
            "#{pane_id}\t#{pane_pid}\t#{pane_dead}",
        )
        assert lead.returncode == 0
        parts = (lead.stdout or "").strip().split("\t")
        assert parts[0] == before.pane_id
        assert int(parts[1]) == before.pane_pid
        assert parts[2] == "0"

        status = team_status(repo, meta["run_id"])
        by_id = {
            str(t["task_id"]): t
            for t in (status.get("tasks") or [])
            if isinstance(t, dict)
        }
        assert victim_id in by_id and sibling_id in by_id
        assert by_id[victim_id]["alive"] is False
        assert by_id[sibling_id]["alive"] is True
    finally:
        stop_team(repo, meta["run_id"])


# ---------------------------------------------------------------------------
# D — Provider readiness (#99)
# ---------------------------------------------------------------------------


def test_ready_provider_reaches_running(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))
    meta = launch_team_inside(root=repo, leader=leader, workers=2)
    try:
        assert meta.get("startup_status") == "running"
        assert set(meta.get("startup_ready_workers") or []) == {"w1", "w2"}
        for row in meta.get("startup_workers") or []:
            assert row.get("gate_ok") is True
            assert row.get("provider_alive") is True
    finally:
        stop_team(repo, meta["run_id"])


def test_blocked_auth_provider_is_blocked_start(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    monkeypatch.setenv("OMG_TEAM_PROVIDER_STRATEGY", "grok")
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "8000")
    install_fixture_provider(
        monkeypatch,
        provider_script("blocked_auth.py"),
        provider="grok",
    )
    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    try:
        assert meta.get("startup_status") == "blocked_start", meta
    finally:
        stop_team(repo, meta["run_id"])


def test_immediate_exit_provider_fails_start(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider exits before ready → fail-closed (never ``running``).

    On macOS the worker pane can vanish before post-split identity snapshot,
    so ``launch_team`` may raise TeamError instead of returning meta. Both
    outcomes are fail-closed; do not probe mid-kill pane identity.
    """
    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
    _bind_leader(monkeypatch, leader)
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "5000")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_EXIT_CODE", "1")
    install_fixture_provider(monkeypatch, provider_script("immediate_exit.py"))

    meta: dict[str, Any] | None = None
    try:
        meta = launch_team_inside(root=repo, leader=leader, workers=1)
    except TeamError as exc:
        msg = str(exc).lower()
        assert (
            "missing worker pane" in msg
            or "cleanup unproven" in msg
            or "identity" in msg
            or "failed_start" in msg
            or "transaction failed" in msg
        ), exc
        _assert_no_running_team(repo)
        # Leader pane must survive the aborted launch.
        lead = tmux_server.tmux(
            "display-message",
            "-p",
            "-t",
            before.pane_id,
            "#{pane_id}\t#{pane_dead}",
        )
        assert lead.returncode == 0
        assert (lead.stdout or "").startswith(f"{before.pane_id}\t0")
        return

    try:
        assert meta.get("startup_status") == "failed_start", meta
        assert meta.get("startup_status") != "running"
        assert int(meta.get("startup_process_ready") or 0) == 0
    finally:
        stop_team(repo, meta["run_id"])


def test_delayed_ready_provider_bounded_wait(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    monkeypatch.setenv("OMG_TEAM_PROVIDER_DELAY_S", "0.5")
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "15000")
    install_fixture_provider(monkeypatch, provider_script("delayed_ready.py"))
    t0 = time.monotonic()
    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    elapsed = time.monotonic() - t0
    try:
        assert meta.get("startup_status") == "running"
        assert elapsed < 14.0
    finally:
        stop_team(repo, meta["run_id"])


# ---------------------------------------------------------------------------
# E — Bootstrap cleanliness (#100)
# ---------------------------------------------------------------------------


def test_pane_scrollback_starts_with_provider_not_bootstrap_json(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))
    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    try:
        pane_id = str((meta.get("tasks") or [{}])[0].get("pane_id") or "")
        assert pane_id.startswith("%")
        wait_until(
            lambda: "TEAM_PROVIDER_READY_OK" in leader.capture_pane(pane_id),
            timeout_s=10.0,
            label="provider ready in scrollback",
        )
        scroll = leader.capture_pane(pane_id)
        assert "shadows ancestor" not in scroll
        assert "nearest .omg" not in scroll
        assert "team.worker-ready" not in scroll
        assert '"schema_version"' not in scroll
        visible = [ln for ln in scroll.splitlines() if ln.strip()]
        assert visible, scroll
        assert not visible[0].startswith("{")
        assert "BOOTSTRAP_" not in visible[0]
    finally:
        stop_team(repo, meta["run_id"])


# ---------------------------------------------------------------------------
# F — Operator exact-pane (#101) + I/O capability refuse (#147 PR1)
# ---------------------------------------------------------------------------


def test_operator_capture_exact_worker_pane_still_identity_fenced(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture remains identity-fenced; readiness is provider stdout, not typing."""
    from omg_cli.team import operator

    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))
    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    try:
        assert meta.get("startup_status") == "running"
        task = (meta.get("tasks") or [{}])[0]
        worker_id = str(task.get("task_id") or "w1")
        # Task rows from live launch must stamp fail-closed I/O (#147).
        assert task.get("io_mode") == "headless_stream"
        assert task.get("provider_tty_owner") == "supervisor"
        assert task.get("operator_input_supported") is False
        assert task.get("input_ready") is False
        assert task.get("interaction_evidence") is None
        out = operator.capture_worker(
            repo,
            str(meta["run_id"]),
            worker_id,
        )
        text = str(out.get("text") or "")
        if "TEAM_PROVIDER_READY_OK" not in text:
            text = leader.capture_pane(str(task["pane_id"]))
        assert "TEAM_PROVIDER_READY_OK" in text
        assert out.get("ok") is True
        assert out.get("worker_id") == worker_id
        assert out.get("pane_id") == task.get("pane_id")
    finally:
        stop_team(repo, meta["run_id"])


def test_operator_input_and_key_refuse_headless_real_tmux(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#147 PR1: headless supervisor panes refuse operator input/key.

    Must not treat local terminal echo / bare marker-in-capture as provider
    delivery. ``send_literal`` / ``send_key`` must never be invoked.
    """
    from unittest.mock import MagicMock

    from omg_cli.team import operator
    from omg_cli.team.io_capability import (
        E_OPERATOR_INPUT_UNSUPPORTED,
        E_OPERATOR_KEY_UNSUPPORTED,
    )
    from omg_cli.team.operator import OperatorError

    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    # needs_pty fixture still supervisor-owned / headless for operator input.
    install_fixture_provider(
        monkeypatch, provider_script("echo_input.py"), needs_pty=True
    )
    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    try:
        assert meta.get("startup_status") == "running"
        task = (meta.get("tasks") or [{}])[0]
        worker_id = str(task.get("task_id") or "w1")
        run_id = str(meta["run_id"])

        # Prove capture still works (identity fence independent of input).
        cap = operator.capture_worker(repo, run_id, worker_id)
        assert cap.get("ok") is True

        send_literal = MagicMock(side_effect=AssertionError("send_literal must not run"))
        send_key = MagicMock(side_effect=AssertionError("send_key must not run"))
        monkeypatch.setattr(operator, "send_literal", send_literal)
        monkeypatch.setattr(operator, "send_key", send_key)

        with pytest.raises(OperatorError) as e_in:
            operator.input_worker(
                repo,
                run_id,
                worker_id,
                f"omg147-marker-{os.getpid()}",
                submit=True,
                operator_override=True,
                is_tty=False,
            )
        assert e_in.value.code == E_OPERATOR_INPUT_UNSUPPORTED

        with pytest.raises(OperatorError) as e_key:
            operator.key_worker(
                repo,
                run_id,
                worker_id,
                "Enter",
                operator_override=True,
                is_tty=False,
            )
        assert e_key.value.code == E_OPERATOR_KEY_UNSUPPORTED

        send_literal.assert_not_called()
        send_key.assert_not_called()

        # JSON noop still precedes any capability/send path.
        with pytest.raises(OperatorError) as e_json:
            operator.input_worker(
                repo,
                run_id,
                worker_id,
                "noop",
                as_json=True,
                operator_override=True,
            )
        assert e_json.value.code == "E_OPERATOR_JSON_NOOP"
        send_literal.assert_not_called()
    finally:
        stop_team(repo, meta["run_id"])


# ---------------------------------------------------------------------------
# G — Scale / relaunch topology (#102)
# ---------------------------------------------------------------------------


def test_scale_up_preserves_same_window_and_leader(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team.scaling import scale_team

    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))
    meta = launch_team_inside(root=repo, leader=leader, workers=2)
    try:
        assert meta.get("startup_status") == "running"
        assert meta.get("executor") == "fixture"
        assert meta.get("view_mode") == "same_window" or (
            (meta.get("tmux_topology") or {}).get("view_mode") == "same_window"
        )
        team_window = str(meta.get("window_id") or before.window_id)
        assert team_window == before.window_id, (team_window, before.window_id)
        # Re-pin geometry after launch layout (CI macOS: tiny windows).
        leader.ensure_window_geometry(team_window, width=160, height=48)
        before_topo = leader.capture_topology()
        before_panes = {
            p.pane_id for p in before_topo.panes if p.window_id == team_window
        }
        assert len(before_panes) == 3, before_topo.pane_ids

        scaled = scale_team(
            repo,
            run_id=str(meta["run_id"]),
            add=1,
            env={EXPERIMENTAL_ENV: "1", **leader.tmux_env()},
        )
        assert isinstance(scaled, dict)
        assert scaled.get("op") == "add", scaled
        assert scaled.get("added") == 1, scaled
        assert scaled.get("dry_run") is not True, scaled
        assert int(scaled.get("active_panes") or 0) == 3, scaled
        tasks_added = scaled.get("tasks_added") or []
        assert len(tasks_added) == 1, scaled
        added = tasks_added[0]
        assert added.get("provider") == "fixture", added
        new_pane = str(added.get("pane_id") or "")
        assert new_pane and new_pane not in before_panes, (
            new_pane,
            before_panes,
            tasks_added,
        )
        # Immediate live proof (not only meta): pane must exist in Team window.
        # If this fails, dump scale result + topology for CI diagnosis.
        topo_now = leader.capture_topology()
        size = tmux_server.tmux(
            "display-message",
            "-p",
            "-t",
            team_window,
            "#{window_width}x#{window_height}",
        )
        diag = {
            "scaled": {
                "op": scaled.get("op"),
                "added": scaled.get("added"),
                "active_panes": scaled.get("active_panes"),
                "tasks_added": [
                    {
                        "task_id": t.get("task_id"),
                        "pane_id": t.get("pane_id"),
                        "window_id": t.get("window_id"),
                        "provider": t.get("provider"),
                        "status": t.get("status"),
                        "pid": t.get("pid"),
                    }
                    for t in tasks_added
                    if isinstance(t, dict)
                ],
            },
            "team_window": team_window,
            "window_size": (size.stdout or "").strip(),
            "pane_rows": [
                {
                    "pane_id": p.pane_id,
                    "window_id": p.window_id,
                    "dead": p.pane_dead,
                    "pid": p.pane_pid,
                }
                for p in topo_now.panes
            ],
        }
        live_ids = {p.pane_id for p in topo_now.panes if p.window_id == team_window}
        assert new_pane in live_ids, diag
        assert len(live_ids) == 4, diag
        live_new = next(p for p in topo_now.panes if p.pane_id == new_pane)
        assert live_new.pane_dead is False, diag
        assert isinstance(live_new.pane_pid, int) and live_new.pane_pid > 0, diag
        wait_until(
            lambda: "TEAM_PROVIDER_READY_OK" in leader.capture_pane(new_pane),
            timeout_s=15.0,
            label="scaled pane provider ready",
        )

        live = next(p for p in topo_now.panes if p.pane_id == before.pane_id)
        assert live.pane_pid == before.pane_pid
        assert live.window_id == team_window
    finally:
        stop_team(repo, meta["run_id"])


# ---------------------------------------------------------------------------
# H — Resume / view (#103)
# ---------------------------------------------------------------------------


def test_resume_reconcile_does_not_move_focus(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team.scaling import resume_team

    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
    foreign = leader.create_foreign_window(name="other")
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))
    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    try:
        leader.select_window(foreign.window_id)
        focus_before = leader.capture_focus(foreign.window_id)
        resume_team(repo, run_id=str(meta["run_id"]))
        focus_after = leader.capture_focus(foreign.window_id)
        assert focus_after == focus_before
        lead = tmux_server.tmux(
            "display-message",
            "-p",
            "-t",
            before.pane_id,
            "#{pane_pid}",
        )
        assert lead.returncode == 0
        assert int((lead.stdout or "0").strip()) == before.pane_pid
    finally:
        stop_team(repo, meta["run_id"])


# ---------------------------------------------------------------------------
# I — Stop / rollback safety
# ---------------------------------------------------------------------------


def test_stop_removes_workers_preserves_leader_and_foreign(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
    foreign = leader.create_foreign_window(name="survive")
    other = leader.create_foreign_session()
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))
    meta = launch_team_inside(root=repo, leader=leader, workers=2)
    worker_panes = [str(t["pane_id"]) for t in (meta.get("tasks") or [])]
    assert len(worker_panes) == 2
    stop_team(repo, meta["run_id"])
    for wid in worker_panes:
        proc = tmux_server.tmux(
            "display-message", "-p", "-t", wid, "#{pane_id}\t#{pane_dead}"
        )
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            assert out.endswith("\t1") or not out.startswith(wid)
        else:
            assert proc.returncode != 0 or not out
    lead = tmux_server.tmux(
        "display-message",
        "-p",
        "-t",
        before.pane_id,
        "#{pane_id}\t#{pane_pid}\t#{pane_dead}",
    )
    assert lead.returncode == 0
    parts = (lead.stdout or "").strip().split("\t")
    assert parts[0] == before.pane_id and parts[2] == "0"
    assert int(parts[1]) == before.pane_pid
    for pane in (foreign.pane_id, other.pane_id):
        alive = tmux_server.tmux(
            "display-message", "-p", "-t", pane, "#{pane_dead}"
        )
        assert alive.returncode == 0
        assert (alive.stdout or "").strip() == "0"


def test_first_split_failpoint_rolls_back_workers(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
    foreign = leader.create_foreign_session()
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))

    inj = FailureInjector()
    inj.arm("first_worker_split")
    inj.install(monkeypatch)

    with pytest.raises((FailpointError, TeamError)) as ei:
        launch_team_inside(root=repo, leader=leader, workers=2)

    assert "first_worker_split" in inj.fired
    assert failpoint_in_chain(ei.value, "first_worker_split") or isinstance(
        ei.value, FailpointError
    )

    topo = leader.capture_topology()
    assert before.pane_id in topo.pane_ids
    leader_window = [p for p in topo.panes if p.window_id == before.window_id]
    assert len(leader_window) == 1
    alive = tmux_server.tmux(
        "display-message", "-p", "-t", foreign.pane_id, "#{pane_dead}"
    )
    assert (alive.stdout or "").strip() == "0"
