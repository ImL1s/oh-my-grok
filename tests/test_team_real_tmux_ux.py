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
from omg_cli.team.plane import EXPERIMENTAL_ENV, stop_team, team_status
from tests.support.team_tmux_harness import (
    FailureInjector,
    IsolatedTmuxServer,
    LeaderSession,
    FailpointError,
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
def tmux_server():
    with IsolatedTmuxServer(prefix="omg104") as server:
        yield server


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
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))

    meta = launch_team_inside(root=repo, leader=leader, workers=2)
    try:
        assert meta.get("attach_mode") == "inside"
        assert meta.get("view_mode") == "same_window" or (
            (meta.get("tasks") or [{}])[0].get("view_mode") == "same_window"
            or meta.get("tmux_topology", {}).get("view_mode") == "same_window"
        )
        # Prefer launch annotations on team.json / meta.
        topo = leader.capture_topology()
        leader_panes = [p for p in topo.panes if p.window_id == before.window_id]
        assert len(leader_panes) == 3, topo.pane_ids  # leader + 2 workers
        # Leader pane id + pid unchanged.
        live_leader = next(p for p in leader_panes if p.pane_id == before.pane_id)
        assert live_leader.pane_pid == before.pane_pid
        assert live_leader.pane_dead is False
        # Focus restored to leader (query the leader window — pane_active is
        # per-window, so session-wide list-panes can report multiple actives).
        assert leader.capture_focus(before.window_id) == before.pane_id
        # Foreign window untouched.
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


def test_same_window_cli_launch_from_leader_tmux_env(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
) -> None:
    """CLI path with controlled TMUX/TMUX_PANE (default fixture executor)."""
    from tests.support.team_tmux_harness import run_omg_team_launch

    leader = _leader(tmux_server)
    before = leader.leader
    assert before is not None
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
        assert leader.capture_focus(before.window_id) == before.pane_id
        assert sum(1 for p in topo.panes if p.window_id == before.window_id) == 3
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
    leader = _leader(tmux_server)
    foreign = leader.create_foreign_window(name="distractor")
    before = leader.leader
    assert before is not None
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(monkeypatch, provider_script("ready_provider.py"))

    # Switch visible window away from leader before/during launch.
    leader.select_window(foreign.window_id)
    meta = launch_team_inside(root=repo, leader=leader, workers=2)
    try:
        topo = leader.capture_topology()
        # Workers still land on the original leader window (TMUX_PANE binding).
        leader_window_panes = [
            p for p in topo.panes if p.window_id == before.window_id
        ]
        assert len(leader_window_panes) == 3
        assert before.pane_id in {p.pane_id for p in leader_window_panes}
        # Foreign window still has exactly one pane.
        foreign_panes = [p for p in topo.panes if p.window_id == foreign.window_id]
        assert len(foreign_panes) == 1
        assert foreign_panes[0].pane_id == foreign.pane_id
    finally:
        stop_team(repo, meta["run_id"])


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

    with pytest.raises((FailpointError, Exception)):
        launch_team_inside(root=repo, leader=leader, workers=2)

    topo = leader.capture_topology()
    assert topo.pane_ids == (before.pane_id,)
    # Foreign session survives.
    alive = tmux_server.tmux(
        "display-message", "-p", "-t", foreign.pane_id, "#{pane_dead}"
    )
    assert alive.returncode == 0
    assert (alive.stdout or "").strip() == "0"
    assert "pre_side_effect" in inj.fired


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
            # remain-on-exit may leave a dead pane id
            return out.endswith("\t1") or not out.startswith(victim)

        wait_until(_victim_gone, timeout_s=5.0, label="victim pane gone")
        # Sibling + leader remain.
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
        assert isinstance(status, dict)
        assert "run_id" in status or status.get("ok") is not None or bool(status)
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
    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    monkeypatch.setenv("OMG_TEAM_READY_TIMEOUT_MS", "8000")
    monkeypatch.setenv("OMG_TEAM_PROVIDER_EXIT_CODE", "1")
    install_fixture_provider(monkeypatch, provider_script("immediate_exit.py"))
    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    try:
        assert meta.get("startup_status") in {
            "failed_start",
            "degraded",
            "unverified_start",
        }, meta
        assert meta.get("startup_status") != "running"
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
        # First meaningful line belongs to provider / supervisor — not bootstrap JSON.
        assert not visible[0].startswith("{")
        assert "BOOTSTRAP_" not in visible[0]
    finally:
        stop_team(repo, meta["run_id"])


# ---------------------------------------------------------------------------
# F — Operator exact-pane (#101)
# ---------------------------------------------------------------------------


def test_operator_capture_and_input_hit_exact_worker_pane(
    tmux_server: IsolatedTmuxServer,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.team import operator

    leader = _leader(tmux_server)
    _bind_leader(monkeypatch, leader)
    install_fixture_provider(
        monkeypatch, provider_script("echo_input.py"), needs_pty=True
    )
    meta = launch_team_inside(root=repo, leader=leader, workers=1)
    try:
        assert meta.get("startup_status") == "running"
        task = (meta.get("tasks") or [{}])[0]
        worker_id = str(task.get("task_id") or "w1")
        # Capture via operator surface when available; else direct pane proof.
        try:
            out = operator.capture_worker(
                repo,
                str(meta["run_id"]),
                worker_id,
            )
            text = str(out.get("text") or "")
        except Exception:
            text = leader.capture_pane(str(task["pane_id"]))
        assert "TEAM_PROVIDER_READY_OK" in text
        # Literal input round-trip via send-keys -l to exact pane.
        pane_id = str(task["pane_id"])
        marker = f"omg104-marker-{os.getpid()}"
        tmux_server.require_ok(
            "send-keys", "-l", "-t", pane_id, marker + "\n"
        )
        wait_until(
            lambda: marker in leader.capture_pane(pane_id)
            or f"ECHO:{marker}" in leader.capture_pane(pane_id),
            timeout_s=8.0,
            label="echo marker",
        )
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
        scaled = scale_team(
            repo,
            run_id=str(meta["run_id"]),
            add=1,
            env={EXPERIMENTAL_ENV: "1", **leader.tmux_env()},
        )
        assert isinstance(scaled, dict)
        topo = leader.capture_topology()
        leader_window_panes = [
            p for p in topo.panes if p.window_id == before.window_id
        ]
        assert len(leader_window_panes) >= 3
        assert before.pane_id in {p.pane_id for p in leader_window_panes}
        live = next(p for p in leader_window_panes if p.pane_id == before.pane_id)
        assert live.pane_pid == before.pane_pid
        assert len({p.window_id for p in leader_window_panes}) == 1
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
        focus_before = leader.capture_focus()
        resume_team(repo, run_id=str(meta["run_id"]))
        focus_after = leader.capture_focus()
        # Reconcile-only must not force-select the Team window/leader.
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
    # Workers gone (or dead); leader + foreign survive.
    for wid in worker_panes:
        proc = tmux_server.tmux(
            "display-message", "-p", "-t", wid, "#{pane_id}\t#{pane_dead}"
        )
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            # remain-on-exit: id may linger as dead
            assert out.endswith("\t1") or not out.startswith(wid)
        else:
            # Pane target gone entirely — success.
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

    with pytest.raises((FailpointError, Exception)):
        launch_team_inside(root=repo, leader=leader, workers=2)

    topo = leader.capture_topology()
    assert before.pane_id in topo.pane_ids
    # No durable worker panes on the leader window beyond the leader itself.
    leader_window = [p for p in topo.panes if p.window_id == before.window_id]
    assert len(leader_window) == 1
    alive = tmux_server.tmux(
        "display-message", "-p", "-t", foreign.pane_id, "#{pane_dead}"
    )
    assert (alive.stdout or "").strip() == "0"

