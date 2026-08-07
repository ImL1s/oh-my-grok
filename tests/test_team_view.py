"""Team view/attach planner and resume --view semantics (#103)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omg_cli.main import build_parser
from omg_cli.team import operator, plane, tmux
from omg_cli.team.operator import OperatorError, plan_and_execute_team_view
from omg_cli.team.plane import EXPERIMENTAL_ENV, team_meta_path
from omg_cli.team.runtime import resume_with_view, view_team
from omg_cli.team.topology import (
    TopologyAnchor,
    TopologyError,
    TopologySnapshot,
    normalize_persisted_topology,
    resolve_view_target,
)
from omg_cli.team.view import (
    ACTION_ATTACH,
    ACTION_NONE,
    ACTION_PRINT,
    ACTION_REFUSE,
    ACTION_SELECT,
    ACTION_SWITCH_CLIENT,
    MODE_NONE,
    MODE_PRINT,
    MODE_VIEW,
    ViewRequest,
    plan_team_view,
    provider_session_stub,
)


TASKS_ONE = [{"task_id": "w1", "owned_files": ["lane_a/"]}]


def _req(**kwargs: Any) -> ViewRequest:
    base = dict(
        mode=MODE_VIEW,
        inside_tmux=False,
        is_tty=True,
        current_session_id=None,
        target_session_id="$42",
        target_session_name="omg-team-sess",
        target_window_id="@9",
        target_pane_id="%1",
        as_json=False,
        takeover=False,
    )
    base.update(kwargs)
    return ViewRequest(**base)


def test_planner_same_session_selects() -> None:
    plan = plan_team_view(
        _req(inside_tmux=True, current_session_id="$42")
    )
    assert plan.action == ACTION_SELECT
    assert "select-pane" in plan.argv
    assert "%1" in plan.argv
    assert "@9" in plan.argv
    assert "attach-session" not in plan.argv


def test_planner_different_session_switch_client() -> None:
    plan = plan_team_view(
        _req(inside_tmux=True, current_session_id="$99")
    )
    assert plan.action == ACTION_SWITCH_CLIENT
    assert "switch-client" in plan.argv
    assert "$42" in plan.argv
    assert "attach-session" not in plan.argv


def test_planner_outside_tty_attach_no_detach() -> None:
    plan = plan_team_view(_req(inside_tmux=False, is_tty=True))
    assert plan.action == ACTION_ATTACH
    assert "attach-session" in plan.argv
    assert "-d" not in plan.argv


def test_planner_takeover_adds_detach() -> None:
    plan = plan_team_view(_req(takeover=True))
    assert plan.action == ACTION_ATTACH
    assert "-d" in plan.argv


def test_planner_outside_non_tty_refuses() -> None:
    plan = plan_team_view(_req(is_tty=False))
    assert plan.action == ACTION_REFUSE
    assert plan.argv  # still provides hint argv


def test_planner_json_never_attaches() -> None:
    plan = plan_team_view(
        _req(as_json=True, inside_tmux=True, current_session_id="$42", is_tty=True)
    )
    assert plan.action == ACTION_NONE


def test_planner_print_only() -> None:
    plan = plan_team_view(_req(mode=MODE_PRINT, inside_tmux=False, is_tty=True))
    assert plan.action == ACTION_PRINT
    assert "attach-session" in plan.argv


def test_planner_mode_none() -> None:
    plan = plan_team_view(_req(mode=MODE_NONE, is_tty=True))
    assert plan.action == ACTION_NONE


def test_planner_refuses_bad_session_id() -> None:
    plan = plan_team_view(_req(target_session_id="not-an-id"))
    assert plan.action == ACTION_REFUSE


def test_provider_session_stub_separates_outcomes() -> None:
    idle = provider_session_stub(requested=False)
    assert idle["status"] == "not_requested"
    assert idle["no_replay"] is True
    assert idle["restore_code"] is False
    blocked = provider_session_stub(requested=True)
    assert blocked["status"] == "unsupported"
    assert blocked["no_replay"] is True


def test_resolve_view_target_from_snapshot() -> None:
    snap = TopologySnapshot(
        mode="detached_session",
        topology_string="split",
        anchor=TopologyAnchor(
            mode="detached_session",
            session_name="omg-team-sess",
            session_id="$42",
            launch_nonce="a" * 32,
            session_owned=True,
            team_window_id="@9",
            leader_pane_id="%1",
            leader_pane_pid=111,
        ),
    )
    target = resolve_view_target(snap)
    assert target.leader_pane_id == "%1"
    assert target.window_id == "@9"
    assert target.session_id == "$42"


def test_resolve_view_target_requires_leader() -> None:
    snap = TopologySnapshot(
        mode="detached_session",
        topology_string="split",
        anchor=TopologyAnchor(
            mode="detached_session",
            session_name="omg-team-sess",
            session_id="$42",
            launch_nonce="a" * 32,
            session_owned=True,
            team_window_id="@9",
            leader_pane_id=None,
            leader_pane_pid=None,
        ),
    )
    with pytest.raises(TopologyError, match="leader_pane_id"):
        resolve_view_target(snap)


def _git(cwd: Path, *args: str) -> None:
    import subprocess

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


def _enable_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)


def _write_live_team(
    root: Path,
    meta: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str = "$42",
    nonce: str = "b" * 32,
    leader_pane_id: str = "%1",
    view_mode: str = "detached_session",
    session_owned: bool = True,
) -> dict[str, Any]:
    live = dict(meta)
    live["dry_run"] = False
    live["session"] = "omg-view-session"
    live["session_id"] = session_id
    live["leader_pane_id"] = leader_pane_id
    live["leader_pane_pid"] = 111
    live["window_id"] = "@9"
    live["view_mode"] = view_mode
    live["session_owned"] = session_owned
    live["topology"] = "split"
    live["tasks"] = [
        {
            **task,
            "pane_id": f"%{index + 10}",
            "pid": 7000 + index,
            "pgid": 8000 + index,
            "pid_start": f"ps:start-{7000 + index}",
            "status": "running",
            "attempt": 1,
            "role": task.get("role") or "executor",
            "provider": task.get("provider") or "grok",
            "posture": task.get("posture") or "read-write",
            "worktree": str(root / ".omg" / "worktrees" / f"w{index}"),
            "pane_command": "python3 tests/fixtures/team_worker_fixture.py",
        }
        for index, task in enumerate(meta["tasks"])
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        root,
        str(meta["run_id"]),
        session=live["session"],
        session_id=session_id,
        launch_nonce=nonce,
        tasks=live["tasks"],
        intent_nonce="c" * 32,
        window_name="omg-team-test",
        view_mode=view_mode,
        layout="tiled",
        leader_pane_id=leader_pane_id,
        leader_pane_pid=111,
        window_id="@9",
        session_owned=session_owned,
        attach_mode="detached" if session_owned else "inside",
    )
    live["launch_nonce"] = nonce
    live["launch_receipt_sha256"] = receipt_hash
    live["identity_generation"] = 0
    live["identity_receipt_sha256"] = receipt_hash
    starts = {task["pid"]: task["pid_start"] for task in live["tasks"]}
    starts[111] = "ps:start-111"

    def _start_for(pid: int) -> str | None:
        return starts.get(pid) or f"ps:start-{pid}"

    monkeypatch.setattr(plane, "_pid_start_identity", _start_for)
    plane._atomic_write_json(team_meta_path(root, str(meta["run_id"])), live)
    return live


def _install_view_tmux(
    monkeypatch: pytest.MonkeyPatch,
    live: dict[str, Any],
    *,
    session_id: str = "$42",
    nonce: str | None = None,
    foreign_session: str | None = None,
    effects: list[list[str]] | None = None,
    mutate_pane_after: str | None = None,
) -> list[list[str]]:
    expected_nonce = nonce if nonce is not None else str(live["launch_nonce"])
    commands = effects if effects is not None else []
    leader = str(live["leader_pane_id"])
    window_id = str(live.get("window_id") or "@9")
    session_name = str(live["session"])
    swapped = {"done": False}

    def run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        if command and command[0] == "tmux":
            command = command[1:]
        commands.append(command)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if not command:
            return result
        op = command[0]
        if op == "display-message":
            target = None
            if "-t" in command:
                target = command[command.index("-t") + 1]
            fmt = command[-1] if command else ""
            # Current session probe (no -t or TMUX_PANE).
            if "#{session_id}" in str(fmt) and "pane_id" not in str(fmt):
                result.stdout = f"{foreign_session or session_id}\n"
                return result
            if target == leader or target is None:
                sid = foreign_session or session_id
                # After first authorize, TOCTOU race swaps pane identity.
                if mutate_pane_after and swapped["done"]:
                    result.returncode = 1
                    result.stderr = "can't find pane"
                    return result
                if mutate_pane_after and not swapped["done"]:
                    # First successful probe, then mark for failure.
                    pass
                result.stdout = (
                    f"{leader}\t{sid}\t{session_name}\t{window_id}\t"
                    f"111\t0\n"
                )
            else:
                result.returncode = 1
                result.stderr = "can't find pane"
        elif op == "show-options":
            option = command[-1] if command else ""
            if "omg_launch_nonce" in str(option):
                result.stdout = expected_nonce + "\n"
            else:
                result.stdout = expected_nonce + "\n"
        elif op in {"select-pane", "select-window", "switch-client"}:
            if mutate_pane_after:
                swapped["done"] = True
            result.returncode = 0
        elif op == "attach-session":
            result.returncode = 0
        else:
            result.returncode = 0
        return result

    monkeypatch.setattr(tmux, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux, "_tmux_run", run)
    monkeypatch.setattr(operator, "tmux_available", lambda: True)
    return commands


def test_view_print_no_tmux_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_team(monkeypatch)
    _init_repo(tmp_path)
    meta = plane.start_team(
        "view print",
        TASKS_ONE,
        root=tmp_path,
        dry_run=True,
    )
    live = _write_live_team(tmp_path, meta, monkeypatch)
    effects = _install_view_tmux(monkeypatch, live)
    out = view_team(
        tmp_path,
        str(meta["run_id"]),
        print_only=True,
        as_json=False,
        is_tty=True,
    )
    assert out["ok"] is True
    assert out["view"]["status"] == "print"
    assert out["print_hint"]
    assert "attach-session" in out["print_hint"]
    # No client-changing ops.
    assert not any(
        c and c[0] in {"select-pane", "select-window", "switch-client", "attach-session"}
        for c in effects
    )


def test_view_json_never_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_team(monkeypatch)
    _init_repo(tmp_path)
    meta = plane.start_team(
        "view json",
        TASKS_ONE,
        root=tmp_path,
        dry_run=True,
    )
    live = _write_live_team(tmp_path, meta, monkeypatch)
    effects = _install_view_tmux(monkeypatch, live)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%99")
    out = view_team(
        tmp_path,
        str(meta["run_id"]),
        as_json=True,
        is_tty=True,
    )
    assert out["view"]["status"] == "none"
    assert out["view"]["executed"] is False
    assert not any(
        c and c[0] in {"select-pane", "switch-client", "attach-session"}
        for c in effects
    )


def test_view_same_session_selects_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_team(monkeypatch)
    _init_repo(tmp_path)
    meta = plane.start_team(
        "view same",
        TASKS_ONE,
        root=tmp_path,
        dry_run=True,
    )
    live = _write_live_team(
        tmp_path,
        meta,
        monkeypatch,
        view_mode="same_window",
        session_owned=False,
    )
    effects = _install_view_tmux(monkeypatch, live)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%50")
    # Current session matches target.
    out = plan_and_execute_team_view(
        tmp_path,
        str(meta["run_id"]),
        mode=MODE_VIEW,
        is_tty=True,
    )
    assert out["ok"] is True
    assert out["view"]["status"] == "selected"
    assert any(c and c[0] == "select-pane" for c in effects)
    assert any(c and c[0] == "select-window" for c in effects)
    assert not any(c and c[0] == "attach-session" for c in effects)


def test_view_different_session_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_team(monkeypatch)
    _init_repo(tmp_path)
    meta = plane.start_team(
        "view switch",
        TASKS_ONE,
        root=tmp_path,
        dry_run=True,
    )
    live = _write_live_team(tmp_path, meta, monkeypatch)
    effects: list[list[str]] = []
    expected_nonce = str(live["launch_nonce"])
    leader = str(live["leader_pane_id"])
    window_id = str(live.get("window_id") or "@9")
    session_name = str(live["session"])
    target_sid = "$42"
    current_sid = "$99"

    def run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        if command and command[0] == "tmux":
            command = command[1:]
        effects.append(command)
        result = MagicMock(returncode=0, stdout="", stderr="")
        op = command[0] if command else ""
        if op == "display-message":
            target = command[command.index("-t") + 1] if "-t" in command else None
            fmt = command[-1] if command else ""
            if "#{session_id}" in str(fmt) and "pane_id" not in str(fmt):
                # Current client session (different).
                result.stdout = f"{current_sid}\n"
            elif target == leader:
                result.stdout = (
                    f"{leader}\t{target_sid}\t{session_name}\t{window_id}\t"
                    f"111\t0\n"
                )
            else:
                result.returncode = 1
        elif op == "show-options":
            result.stdout = expected_nonce + "\n"
        return result

    monkeypatch.setattr(tmux, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux, "_tmux_run", run)
    monkeypatch.setattr(operator, "tmux_available", lambda: True)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%50")

    out = plan_and_execute_team_view(
        tmp_path,
        str(meta["run_id"]),
        mode=MODE_VIEW,
        is_tty=True,
    )
    assert out["ok"] is True
    assert out["view"]["status"] == "switched"
    assert any(c and c[0] == "switch-client" for c in effects)
    assert not any(c and c[0] == "attach-session" for c in effects)


def test_view_stale_nonce_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_team(monkeypatch)
    _init_repo(tmp_path)
    meta = plane.start_team(
        "view stale",
        TASKS_ONE,
        root=tmp_path,
        dry_run=True,
    )
    live = _write_live_team(tmp_path, meta, monkeypatch)
    _install_view_tmux(monkeypatch, live, nonce="d" * 32)  # wrong nonce
    with pytest.raises(OperatorError, match="authorize|nonce"):
        plan_and_execute_team_view(
            tmp_path,
            str(meta["run_id"]),
            mode=MODE_VIEW,
            is_tty=True,
        )


def test_resume_with_view_separates_reconcile_and_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_team(monkeypatch)
    _init_repo(tmp_path)
    meta = plane.start_team(
        "resume view",
        TASKS_ONE,
        root=tmp_path,
        dry_run=True,
    )
    live = _write_live_team(tmp_path, meta, monkeypatch)
    _install_view_tmux(monkeypatch, live)

    # Force reconcile path without full resume machinery: stub resume_for_identity.
    from omg_cli.team import runtime as runtime_mod

    monkeypatch.setattr(
        runtime_mod,
        "resume_for_identity",
        lambda *a, **k: {
            "run_id": str(meta["run_id"]),
            "note": "reconciled",
            "relaunched": ["w1"],
            "blocked": [],
            "layout_repair_needed": False,
            "view_mode": "detached_session",
        },
    )
    monkeypatch.delenv("TMUX", raising=False)
    out = resume_with_view(
        tmp_path,
        str(meta["run_id"]),
        view=True,
        as_json=False,
        is_tty=True,
        request_provider_session=True,
    )
    assert out["reconcile"]["status"] == "ok"
    assert out["reconcile"]["relaunched"] == ["w1"]
    assert out["provider_session"]["requested"] is True
    assert out["provider_session"]["status"] == "unsupported"
    assert out["provider_session"]["no_replay"] is True
    assert out["provider_session"]["restore_code"] is False
    assert out["view"]["requested"] is True
    # View may succeed or fail independently; envelope keeps both.
    assert "status" in out["view"]


def test_resume_json_skips_view_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.team import runtime as runtime_mod

    _enable_team(monkeypatch)
    monkeypatch.setattr(
        runtime_mod,
        "resume_for_identity",
        lambda *a, **k: {
            "run_id": "run-x",
            "note": "ok",
            "relaunched": [],
            "blocked": [],
        },
    )
    out = resume_with_view(
        tmp_path,
        "run-x",
        view=True,
        as_json=True,
        is_tty=True,
    )
    assert out["view"]["requested"] is False
    assert out["view"]["status"] == "none"
    assert out["ok"] is True


def test_cli_registers_view_and_resume_flags() -> None:
    parser = build_parser()
    resume = parser.parse_args(
        ["team", "resume", "--run", "r1", "--view", "--print", "--json"]
    )
    assert resume.team_action == "resume"
    assert resume.resume_view is True
    assert resume.view_print is True
    view = parser.parse_args(["team", "view", "--run", "r1", "--takeover"])
    assert view.team_action == "view"
    assert view.view_takeover is True


def test_normalize_topology_feeds_view_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_team(monkeypatch)
    _init_repo(tmp_path)
    meta = plane.start_team(
        "topo view",
        TASKS_ONE,
        root=tmp_path,
        dry_run=True,
    )
    live = _write_live_team(tmp_path, meta, monkeypatch)
    snap = normalize_persisted_topology(live)
    target = resolve_view_target(snap)
    assert target.session_id == live["session_id"]
    assert target.leader_pane_id == live["leader_pane_id"]
